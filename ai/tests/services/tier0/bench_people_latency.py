"""GPU 없는 SIM 벤치: 가짜 검출/COPE + 실제 인지·판정·저널 제출·I1 offer.

ai/에서 python tests/services/tier0/bench_people_latency.py --output <JSON> --work-dir <임시 경로>.
촬영 시각은 100 ms 간격, 입력 생성은 측정 밖. COPE 지연은 crop당 20 ms(sleep, GIL 해제).
실제 CUDA/GIL/디코더 성능을 주장하는 벤치가 아니다.
"""

from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import sys
import threading
import time

import numpy as np

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2], HERE.parents[1] / "geometry"):
    sys.path.insert(0, str(path))

import bench_tier0_geometry as geo  # noqa: E402
import tier0_site as ts  # noqa: E402
from services.tier0.judge import JudgmentLoop  # noqa: E402
from services.tier0.perception import DetectorFeed, Perception  # noqa: E402
from simai_ai.config import BundleVerifier, ConfigSync  # noqa: E402
from simai_ai.eventlog import EventJournal, JournalWriter  # noqa: E402
from simai_ai.providers.base import Frame  # noqa: E402
from simai_ai.reid.collector import AsyncReidCollector, ReidCollectorConfig  # noqa: E402
from simai_ai.risk import FixedClock, LifecycleTracker, Origin, default_registry  # noqa: E402
from simai_ai.tracking.local_tracker import (  # noqa: E402
    CameraDetections, Observation2DFrame, PoseDetection, TrackedDetection,
)


class FakeEmbedder:
    def __init__(self, delay_s=0.020):
        self.delay_s = delay_s
        self.calls = 0
        self.crops = 0

    def embed(self, crops):
        self.calls += 1
        self.crops += len(crops)
        time.sleep(self.delay_s * len(crops))
        return np.tile(np.array([1.0, 0.0], dtype=np.float32), (len(crops), 1))


class FakeDetector:
    def __init__(self, detections):
        self.detections = detections

    def detect(self, frames):
        time.sleep(0.002)  # TRT 대신 고정 지연
        return [CameraDetections(
            f.camera_id, 1, f.frame_ref["frame_sequence"], f.estimated_capture_time,
            "SIM", 0.0, self.detections[f.camera_id],
        ) for f in frames]


class FakeTracker:
    def update_batch(self, results):
        return [Observation2DFrame(
            r.camera_id, r.stream_epoch, r.frame_sequence, r.estimated_capture_time_ms,
            r.timestamp_source, r.timestamp_uncertainty_ms,
            tuple(TrackedDetection(f"{r.camera_id}-p{i}", d) for i, d in enumerate(r.detections)),
        ) for r in results]


def run(people: int, ticks: int, work_dir: Path, *, legacy_order: bool = False):
    rig = geo.make_rig(coeffs=())
    rng = np.random.default_rng(499)
    persons = geo.make_people(people, rng)
    detections = {}
    # 모두 고신뢰도·영상 내부에 있는 동일 입력. 실제 연관·삼각측량은 아래 Perception이 수행한다.
    for cid, cam in rig.cameras.items():
        dets = []
        for person in persons:
            uv, inside = geo.project(cam, person.joints())
            kps = tuple((float(u), float(v), 0.95) if ok else None for (u, v), ok in zip(uv, inside))
            lo, hi = uv.min(axis=0) - 20, uv.max(axis=0) + 20
            dets.append(PoseDetection((float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])), 0.95, kps))
        detections[cid] = tuple(dets)
    model = FakeEmbedder()
    collector = AsyncReidCollector(ReidCollectorConfig(), embedder=model)
    pending = deque()
    feed = DetectorFeed(camera_ids=tuple(rig.cameras), tickets=lambda: pending.popleft() if pending else [],
                        detector=FakeDetector(detections), tracker=FakeTracker(), reid_collector=collector,
                        async_mode=True)
    perception = Perception(rig=rig, feed=feed, scene_version="bench", session="bench",
                            input_provenance={"is_upper_bound": True})
    clock = FixedClock(geo.T0)
    message, fetcher = ts.bundle_message(geo.calibration_doc(coeffs=()), "cfg-bench", geo.T0 - 1, rules=(("R1", 1),))
    verifier = BundleVerifier(fetcher, default_registry(), lambda _v: True, clock)
    sync = ConfigSync(verifier, clock, source_id=ts.GATEWAY, restored=verifier.verify(message),
                      restored_applied_at=geo.T0)
    journal = EventJournal.open(work_dir / str(people) / ("legacy" if legacy_order else "current"), clock=clock)
    writer = JournalWriter(journal)
    writer.start()
    offered = []
    judge = JudgmentLoop(lock=threading.RLock(), now_ms=clock.now_ms, config_sync=sync, perception=perception,
                         tracker=LifecycleTracker(origin=Origin.REPLAY, gateway_id=ts.GATEWAY, clock=clock),
                         journal=journal, writer=writer, calibration_state=lambda: "PROVISIONAL",
                         offer_world_state=lambda payload, **kw: offered.append(payload))
    image = np.zeros((geo.SIZE[1], geo.SIZE[0], 3), dtype=np.uint8)
    # 이전 순서: 검출 스레드에서 crop 제출. 같은 입력/모델로 순서만 비교한다.
    if legacy_order:
        feed.reid_collector = None
    times, end_to_end, cpu = [], [], []
    wall_start = time.perf_counter()
    try:
        for k in range(ticks):
            # 실제 10 Hz 입력: rate cap 전후를 같은 도착률로 비교한다.
            time.sleep(max(0, wall_start + k * .1 - time.perf_counter()))
            cap = geo.T0 + k * 100
            clock.set(cap + 60)
            frames = [Frame(c, cap, "SIM", {"stream_epoch": 1, "frame_sequence": k}, image=image)
                      for c in rig.cameras]
            pending.append(frames)
            start = time.perf_counter()
            feed.detect_once()
            if legacy_order:
                for f in frames:
                    result = Observation2DFrame(f.camera_id, 1, k, cap, "SIM", 0., tuple(
                        TrackedDetection(f"{f.camera_id}-p{i}", d) for i, d in enumerate(detections[f.camera_id])))
                    collector.submit_observation(f, result)
            s, c = time.perf_counter(), time.thread_time()
            result = judge.tick()
            times.append((time.perf_counter() - s) * 1000)
            cpu.append((time.thread_time() - c) * 1000)
            end_to_end.append(60 + (time.perf_counter() - start) * 1000)
            assert result.perceived is not None
        # 제출 작업 및 모델이 완결된 다음 호출 수를 센다(측정 시간에는 넣지 않는다).
        if hasattr(feed, "_reid_queue"):
            feed._reid_queue.join()
        assert collector.flush(20)
        return {"people": people, "ticks": ticks, "legacy_order": legacy_order,
                "tick_p95_ms": round(float(np.percentile(times, 95)), 3),
                "tick_cpu_p95_ms": round(float(np.percentile(cpu, 95)), 3),
                "capture_to_offer_p95_ms": round(float(np.percentile(end_to_end, 95)), 3),
                "reid_calls": model.calls, "reid_crops": model.crops, "reid_status": collector.status(),
                "offered": len(offered),
                "stage_timing": getattr(perception, "timing", None).summary()
                if hasattr(perception, "timing") else {}}
    finally:
        feed.close()
        collector.close()
        writer.stop(5)
        journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--ticks", type=int, default=40)
    parser.add_argument("--legacy-order", action="store_true")
    args = parser.parse_args()
    rows = []
    for n in range(1, 7):
        row = run(n, args.ticks, args.work_dir, legacy_order=args.legacy_order)
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if k not in ("reid_status", "stage_timing")}), flush=True)
    args.output.write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
