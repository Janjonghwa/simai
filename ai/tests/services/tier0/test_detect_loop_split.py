"""S15P31A101-383: Tier 0 검출·로컬추적 루프 분리 검증.

- 10 Hz 판정 루프(tick_ms=100)와 분리된 tier0-detect 루프(detect_tick_ms=50)가 25 fps 카메라 입력에서
  카메라당 12 FPS 이상 관측(health fps 및 처리 프레임 수)을 달성하는지 확인한다.
- 검출(PoseDetector.detect)과 로컬추적(MultiCameraLocalTracker.update_batch)이 판정 lock(judge_lock) 밖에서
  실행되어, 느린 추론 중에도 다른 스레드(health·site_ack 등)가 judge_lock 을 즉시 획득하는지 확인한다.
- 판정 루프가 일시 지연될 때 DetectorFeed 의 카메라별 유계 대기열(max_queued_per_camera)이 오래된 관측을 버리고
  최신 관측만 유지하는지 확인한다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import json
from pathlib import Path
import statistics
import sys
import threading
import time

import pytest

np = pytest.importorskip("numpy")

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from test_live_input import OneTrackBackend  # noqa: E402
from test_tier0_service import _settings  # noqa: E402
import tier0_site as ts  # noqa: E402

from services.tier0.perception import DetectorFeed  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
from services.tier0.settings import (  # noqa: E402
    CleanBackgroundSettings,
    LiveCalibrationSettings,
    Tier0ConfigError,
    load_settings,
)
from simai_ai.io import (  # noqa: E402
    DecodedPicture,
    FreshnessConfig,
    I0CodecInfo,
    I0Frame,
    I0FrameHeader,
    I0Ingest,
    SimObservationFrameSource,
)
from simai_ai.providers.base import Frame  # noqa: E402
from simai_ai.risk import SystemClock  # noqa: E402
from simai_ai.tracking.local_tracker import CameraDetections, MultiCameraLocalTracker, PoseDetection  # noqa: E402


@pytest.fixture(scope="module")
def sim_scene(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("detect_split")
    run = ts.run_sim(tmp, ts.scenario(12_000))
    src = SimObservationFrameSource(run)
    by_cam: dict[str, list[Frame]] = {c: [] for c in src.camera_ids}
    for f in src.frames():
        by_cam[f.camera_id].append(f)
    return run, by_cam


class _InstantDecoder:
    """압축 바이트에 들어 있는 2바이트 프레임 인덱스를 그대로 화소에 적어 내는 경량 디코더."""

    def configure(self, codec: I0CodecInfo) -> None:
        self._codec = codec

    def decode(self, payload: bytes, key: tuple[int, int], keyframe: bool = False) -> list[DecodedPicture]:
        idx = int.from_bytes(payload[:2], "big")
        img = np.zeros((16, 16, 3), dtype=np.uint8)
        img[0, 0, 0] = (idx >> 8) & 0xFF
        img[0, 0, 1] = idx & 0xFF
        return [DecodedPicture(key=key, image=img)]

    def flush(self) -> list[DecodedPicture]:
        return []

    def reset(self) -> None:
        pass


class _SimIndexDetector:
    """화소에 적힌 프레임 인덱스로 SIM 2D keypoint 를 돌려주고 추론 시각·간격과 lock 보유 여부를 기록한다."""

    def __init__(
        self,
        by_cam: dict[str, list[Frame]],
        shift_ms: int,
        *,
        infer_sleep_s: float = 0.0,
        judge_lock: threading.RLock | None = None,
    ) -> None:
        self.by_cam = by_cam
        self.shift_ms = shift_ms
        self.infer_sleep_s = infer_sleep_s
        self.judge_lock = judge_lock
        self._lock = threading.Lock()
        self.inferred_captures: dict[str, list[int]] = {c: [] for c in by_cam}
        self.batch_sizes: list[int] = []
        self.judge_lock_free_checks: list[bool] = []

    def _check_judge_lock_free(self) -> None:
        if self.judge_lock is None:
            return
        free: list[bool] = []

        def probe() -> None:
            acquired = self.judge_lock.acquire(timeout=0.02)
            if acquired:
                self.judge_lock.release()
            free.append(acquired)

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(0.1)
        with self._lock:
            self.judge_lock_free_checks.append(bool(free and free[0]))

    def detect(self, batch):
        self._check_judge_lock_free()
        if self.infer_sleep_s > 0:
            time.sleep(self.infer_sleep_s)
        out = []
        with self._lock:
            self.batch_sizes.append(len(batch))
        for f in batch:
            idx = (int(f.image[0, 0, 0]) << 8) | int(f.image[0, 0, 1])
            sim = self.by_cam[f.camera_id][idx]
            dets = []
            for d in sim.detections or ():
                confs = [k[2] for k in d.keypoints_2d if k is not None and k[2] > 0]
                score = sum(confs) / len(confs) if confs else 0.0
                dets.append(PoseDetection(tuple(d.bbox), min(1.0, score), tuple(d.keypoints_2d)))
            with self._lock:
                self.inferred_captures[f.camera_id].append(f.estimated_capture_time)
            out.append(
                CameraDetections(
                    f.camera_id,
                    f.frame_ref["stream_epoch"],
                    f.frame_ref["frame_sequence"],
                    f.estimated_capture_time,
                    f.timestamp_source,
                    f.timestamp_uncertainty_ms,
                    tuple(dets),
                )
            )
        return out


def test_detect_loop_achieves_12_fps_per_camera_while_judge_runs_at_10_hz(tmp_path, sim_scene) -> None:
    """25 fps 카메라 입력 + 10 Hz 판정 루프(tick_ms=100)에서 tier0-detect(detect_tick_ms=40)가 카메라당 12 FPS 이상을 낸다."""
    run, by_cam = sim_scene
    cams = tuple(by_cam)
    anchor_ms = int(time.time_ns() // 1_000_000) + 400
    base_settings = _settings(tmp_path, run, anchor_ms=anchor_ms)
    settings = replace(
        base_settings,
        tick_ms=100,
        detect_tick_ms=40,
        drift_watch_enabled=False,
        live_calibration=LiveCalibrationSettings(enabled=False),
        clean_background=CleanBackgroundSettings(enabled=False),
    )

    start_sim = min(fs[0].estimated_capture_time for fs in by_cam.values())
    shift_ms = anchor_ms - start_sim

    ingest = I0Ingest(
        cams,
        lambda _c: _InstantDecoder(),
        clock=SystemClock(),
        config=FreshnessConfig(queue_depth=1, max_queue_wait_ms=150, reorder_window_ms=0),
    )
    detector = _SimIndexDetector(by_cam, shift_ms, infer_sleep_s=0.012)
    tracker = MultiCameraLocalTracker(camera_ids=cams, backend_factory=OneTrackBackend)
    feed = DetectorFeed(
        camera_ids=cams,
        tickets=ingest.next_batch,
        detector=detector,
        tracker=tracker,
        source="LIVE",
        async_mode=True,
    )
    service = Tier0Service(settings, feed=feed)
    detector.judge_lock = service.judge_lock

    for cam in cams:
        ingest.receive(I0CodecInfo(camera_id=cam, stream_epoch=1, codec="h264"))

    stop_sender = threading.Event()

    def stream_frames() -> None:
        max_len = min(len(fs) for fs in by_cam.values())
        for idx in range(max_len):
            if stop_sender.is_set():
                return
            target_ms = by_cam[cams[0]][idx].estimated_capture_time + shift_ms
            wait_s = (target_ms - (time.time_ns() // 1_000_000)) / 1000.0
            if wait_s > 0:
                if stop_sender.wait(wait_s):
                    return
            payload = idx.to_bytes(2, "big")
            for cam in cams:
                sim_f = by_cam[cam][idx]
                cap_ms = sim_f.estimated_capture_time + shift_ms
                ingest.receive(
                    I0Frame(
                        I0FrameHeader(
                            camera_id=cam,
                            stream_epoch=1,
                            frame_sequence=int(sim_f.frame_ref["frame_sequence"]),
                            estimated_capture_time_ms=cap_ms,
                            timestamp_source="CAMERA_STREAM",
                            keyframe=(idx % 10 == 0),
                            timestamp_uncertainty_ms=5.0,
                        ),
                        payload=payload,
                    )
                )

    sender = threading.Thread(target=stream_frames, name="test-25fps-sender", daemon=True)
    service.start()
    sender.start()
    try:
        # 웜업 구간 뒤 1.5초 동안 실제 처리 FPS 와 health.update_fps() 값을 잰다
        assert ts.wait_until(lambda: service.judge.stats.perceived >= 3, timeout=5.0)
        service.health.update_fps()
        before_counts = {c: len(detector.inferred_captures[c]) for c in cams}
        before_ticks = service.judge.stats.ticks
        t0 = time.monotonic()
        time.sleep(1.5)
        elapsed_s = time.monotonic() - t0
        service.health.update_fps()
        after_counts = {c: len(detector.inferred_captures[c]) for c in cams}
        after_ticks = service.judge.stats.ticks
        health_payload = service.health.payload()
    finally:
        stop_sender.set()
        sender.join(2.0)
        assert service.stop(timeout_s=10.0) is True

    measured_fps = {c: (after_counts[c] - before_counts[c]) / elapsed_s for c in cams}
    health_fps = {entry["id"]: entry["fps"] for entry in health_payload["cameras"]}
    judge_hz = (after_ticks - before_ticks) / elapsed_s

    # 1) 카메라당 관측 FPS >= 12.0 (기존 10 Hz 판정 루프 결합 시 상한 10 FPS 돌파)
    for cam in cams:
        assert measured_fps[cam] >= 12.0, f"{cam} measured_fps={measured_fps[cam]:.2f}"
        assert health_fps[cam] >= 12.0, f"{cam} health_fps={health_fps[cam]:.2f}"

    # 2) 판정 루프는 여전히 ~10 Hz (tick_ms=100)로 돌고, 검출 배치는 그보다 자주 돈다
    assert 7.0 <= judge_hz <= 13.0, f"judge_hz={judge_hz:.2f}"
    assert feed.stats.batches > service.judge.stats.ticks

    # 3) 프레임 간 촬영 시각 간격(result_interval_ms): 중앙값 <= 80 ms (기존 10 Hz 는 >= 100 ms)
    for cam in cams:
        caps = detector.inferred_captures[cam]
        intervals = [b - a for a, b in zip(caps, caps[1:], strict=False)]
        assert intervals and statistics.median(intervals) <= 80.0, f"{cam} intervals={intervals[:10]}"

    # 4) 검출 실행 중 판정 lock(judge_lock)은 한 번도 잡히지 않았다
    assert detector.judge_lock_free_checks and all(detector.judge_lock_free_checks)


def test_judge_lock_is_not_held_during_slow_detection_or_local_tracking(tmp_path, sim_scene) -> None:
    """검출(60 ms)·Local Tracker(15 ms)가 느려도 judge_lock 밖에서 돌아 다른 스레드의 lock 대기 시간이 짧다."""
    run, by_cam = sim_scene
    cams = tuple(by_cam)
    anchor_ms = int(time.time_ns() // 1_000_000) + 200
    base_settings = _settings(tmp_path, run, anchor_ms=anchor_ms)
    settings = replace(
        base_settings,
        tick_ms=100,
        detect_tick_ms=40,
        drift_watch_enabled=False,
        live_calibration=LiveCalibrationSettings(enabled=False),
        clean_background=CleanBackgroundSettings(enabled=False),
    )

    start_sim = min(fs[0].estimated_capture_time for fs in by_cam.values())
    shift_ms = anchor_ms - start_sim

    ticket_seq = [0]
    ticket_lock = threading.Lock()

    def next_tickets():
        with ticket_lock:
            idx = ticket_seq[0]
            if idx >= len(by_cam[cams[0]]):
                return []
            ticket_seq[0] += 1
        img = np.zeros((16, 16, 3), dtype=np.uint8)
        img[0, 0, 0] = (idx >> 8) & 0xFF
        img[0, 0, 1] = idx & 0xFF
        out = []
        for cam in cams:
            sim_f = by_cam[cam][idx]
            out.append(
                Frame(
                    cam,
                    sim_f.estimated_capture_time + shift_ms,
                    "CAMERA_STREAM",
                    {"stream_epoch": 1, "frame_sequence": idx},
                    timestamp_uncertainty_ms=5.0,
                    image=img,
                )
            )
        return out

    tracker_lock_free: list[bool] = []
    service_ref: list[Tier0Service] = []

    class _SlowVerifyBackend(OneTrackBackend):
        def update(self, bboxes, scores, features=None):
            if service_ref:
                lock = service_ref[0].judge_lock
                free: list[bool] = []

                def probe() -> None:
                    ok = lock.acquire(timeout=0.02)
                    if ok:
                        lock.release()
                    free.append(ok)

                t = threading.Thread(target=probe, daemon=True)
                t.start()
                t.join(0.1)
                tracker_lock_free.append(bool(free and free[0]))
            time.sleep(0.004)  # 4대 카메라 합산 약 16 ms Local Tracker 부하
            return super().update(bboxes, scores, features)

    detector = _SimIndexDetector(by_cam, shift_ms, infer_sleep_s=0.060)
    tracker = MultiCameraLocalTracker(camera_ids=cams, backend_factory=_SlowVerifyBackend)
    feed = DetectorFeed(
        camera_ids=cams,
        tickets=next_tickets,
        detector=detector,
        tracker=tracker,
        source="LIVE",
        async_mode=True,
    )
    service = Tier0Service(settings, feed=feed)
    service_ref.append(service)
    detector.judge_lock = service.judge_lock

    lock_wait_ms: list[float] = []
    stop_probe = threading.Event()

    def lock_probe() -> None:
        while not stop_probe.is_set():
            t0 = time.perf_counter()
            with service.judge_lock:
                waited = (time.perf_counter() - t0) * 1000.0
            lock_wait_ms.append(waited)
            time.sleep(0.015)

    probe_thread = threading.Thread(target=lock_probe, name="test-lock-probe", daemon=True)
    service.start()
    probe_thread.start()
    try:
        assert ts.wait_until(lambda: feed.stats.batches >= 6 and len(lock_wait_ms) >= 15, timeout=5.0)
    finally:
        stop_probe.set()
        probe_thread.join(1.0)
        assert service.stop(timeout_s=10.0) is True

    assert detector.judge_lock_free_checks and all(detector.judge_lock_free_checks)
    assert tracker_lock_free and all(tracker_lock_free)
    # 60 ms 검출 + 16 ms 로컬추적이 도는 동안에도 judge_lock 대기 중앙값은 5 ms 미만, 최대치도 추론 시간(60 ms)보다 훨씬 작다
    assert statistics.median(lock_wait_ms) < 5.0, f"median={statistics.median(lock_wait_ms):.2f} ms"
    assert max(lock_wait_ms) < 35.0, f"max={max(lock_wait_ms):.2f} ms"


def test_direct_judge_tick_also_runs_detect_outside_judge_lock(tmp_path, sim_scene, monkeypatch) -> None:
    """비동기 스레드가 없는 단건 tick 경로(async_mode=False)에서도 detect_once 는 judge_lock 진입 전에 실행된다."""
    monkeypatch.setattr(Tier0Service, "_run_judge", lambda self: None)
    monkeypatch.setattr(Tier0Service, "_run_housekeeping", lambda self: None)
    run, by_cam = sim_scene
    cams = tuple(by_cam)
    anchor_ms = 1_000_000
    settings = replace(
        _settings(tmp_path, run, anchor_ms=anchor_ms),
        drift_watch_enabled=False,
        live_calibration=LiveCalibrationSettings(enabled=False),
        clean_background=CleanBackgroundSettings(enabled=False),
    )
    start_sim = min(fs[0].estimated_capture_time for fs in by_cam.values())
    shift_ms = anchor_ms - start_sim
    img = np.zeros((16, 16, 3), dtype=np.uint8)
    tickets = [
        Frame(c, by_cam[c][0].estimated_capture_time + shift_ms, "FILE", {"stream_epoch": 1, "frame_sequence": 0}, image=img)
        for c in cams
    ]
    detector = _SimIndexDetector(by_cam, shift_ms)
    feed = DetectorFeed(
        camera_ids=cams,
        tickets=lambda: list(tickets),
        detector=detector,
        tracker=MultiCameraLocalTracker(camera_ids=cams, backend_factory=OneTrackBackend),
        source="FILE",
        async_mode=False,
    )
    service = Tier0Service(settings, feed=feed)
    detector.judge_lock = service.judge_lock
    service.start()
    try:
        service.judge.tick()
    finally:
        assert service.stop() is True
    assert feed.stats.batches == 1
    assert detector.judge_lock_free_checks == [True]


def test_bounded_observation_queue_evicts_oldest_when_judge_is_stalled(sim_scene) -> None:
    """판정 루프 소비가 멈춰도 DetectorFeed 관측 대기열은 max_queued_per_camera 를 넘지 않고 최신 관측만 남긴다."""
    _, by_cam = sim_scene
    cams = tuple(by_cam)
    queue: deque[list[Frame]] = deque()
    for seq in range(10):
        img = np.zeros((16, 16, 3), dtype=np.uint8)
        img[0, 0, 1] = seq
        queue.append(
            [
                Frame(
                    c,
                    1000 + seq * 40,
                    "CAMERA_STREAM",
                    {"stream_epoch": 1, "frame_sequence": seq},
                    image=img,
                )
                for c in cams
            ]
        )
    detector = _SimIndexDetector(by_cam, 0)
    feed = DetectorFeed(
        camera_ids=cams,
        tickets=lambda: queue.popleft() if queue else [],
        detector=detector,
        tracker=MultiCameraLocalTracker(camera_ids=cams, backend_factory=OneTrackBackend),
        max_queued_per_camera=3,
        async_mode=True,
    )
    for _ in range(10):
        assert feed.detect_once() == len(cams)
        assert feed.pending_observations() <= 3 * len(cams)

    assert feed.pending_observations() == 3 * len(cams)
    assert feed.stats.dropped_observations == (10 - 3) * len(cams)

    polled = feed.poll(2000)
    assert len(polled) == 3 * len(cams)
    assert feed.pending_observations() == 0 and feed.drained()
    for cam in cams:
        cam_seqs = [o.frame_sequence for o in polled if o.camera_id == cam]
        assert cam_seqs == [7, 8, 9]


def test_settings_parse_and_validate_detect_loop_options(tmp_path, sim_scene) -> None:
    run, _ = sim_scene
    default_settings = _settings(tmp_path, run, anchor_ms=1000)
    assert default_settings.detect_tick_ms == 50
    assert default_settings.detect_queue_depth == 8

    cfg = tmp_path / "tier0.json"
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    doc["detect_tick_ms"] = 40
    doc["detect_queue_depth"] = 6
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    custom = load_settings(cfg, env={})
    assert custom.detect_tick_ms == 40 and custom.detect_queue_depth == 6

    for key, bad in (("detect_tick_ms", 0), ("detect_tick_ms", True), ("detect_queue_depth", 0), ("detect_queue_depth", -1)):
        bad_doc = dict(doc)
        bad_doc[key] = bad
        cfg.write_text(json.dumps(bad_doc), encoding="utf-8")
        with pytest.raises(Tier0ConfigError):
            load_settings(cfg, env={})


def test_event_frame_buffer_offer_and_snapshot_from_two_threads_do_not_race():
    """-383 뒤 offer 는 검출 스레드, snapshot 은 판정 스레드다. 동시에 불러도 예외 없이 끝난다."""
    import threading

    import numpy as np

    from services.tier0.event_frame_buffer import EventFrameBuffer
    from simai_ai.providers.base import Frame

    buf = EventFrameBuffer(("cam_A",))
    image = np.zeros((2, 2, 3), dtype=np.uint8)
    errors: list[BaseException] = []
    stop = threading.Event()

    def produce():
        try:
            for t in range(1, 20_000):
                buf.offer(Frame("cam_A", t * 10, "RECEIVE_ESTIMATED", {"stream_epoch": 1, "frame_sequence": t}, image=image))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    def consume():
        try:
            while not stop.is_set():
                buf.snapshot(("cam_A",), 100_000, 2)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=produce), threading.Thread(target=consume)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert not errors
