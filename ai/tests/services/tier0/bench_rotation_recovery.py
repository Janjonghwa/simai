"""회전 복구 한 주기의 작업자 CPU(단계·카메라별)와 쓰러짐 감지 지연 수동 벤치(S15P31A101-494, -746).

ai/ 에서: PYTHONPATH=. .venv/Scripts/python tests/services/tier0/bench_rotation_recovery.py [--images DIR] [--crowd]
    [--cycles 8] [--budget-ms 1000] [--cpu-scale 3] [--fall cam_C --fall-cycle 3]

- 입력: 2304x1296 4대(18차 크기). --images 는 background_cam_?.png 4장(실제 로비 배경), 없으면 합성 질감.
- 표본: 카메라마다 5 s 간격, 15 s 주기마다 3장씩 새로 들어온다(관제 기본값). 부모가 960 px 로 줄여 넘기는 것과 같다.
- --crowd: 표본마다 사람 5명 크기의 가림(마스크 0)과 그 아래 그림자 변화(마스크 1)를 넣는다.
- --cpu-scale 3: 작업자를 같은 프로세스에서 돌리며 CPU 시계를 3배로 센다(느린 서버 코어에서 예산·이월 동작 확인).
- --fall: 그 주기부터 해당 카메라 장면을 광축 기준 30° 돌린다(허용각 15° 초과 → REQUIRES_SETUP 2회 확인 뒤 확정).
합성·실험 자료의 CPU 측정이며 G1/G2 제품 정확도 근거로 쓰지 않는다. 시험(test_rotation_recovery_budget.py)도 이 하네스를 쓴다.
"""

from __future__ import annotations

import argparse
import copy
import glob
import json
import tempfile
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from services.tier0.clean_background import omit_rotation_samples
from services.tier0.rotation_recovery import BackgroundRotationRecovery
from services.tier0.settings import RotationRecoverySettings
from simai_ai.calibration.rotation_worker import resize_samples

SIZE = (2304, 1296)
SAMPLE_INTERVAL_MS = 5_000
WINDOW = 10
K = [[1700.0, 0.0, 1152.0], [0.0, 1700.0, 648.0], [0.0, 0.0, 1.0]]


def synthetic_images(seed: int = 746, count: int = 4) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    return [cv2.resize(rng.integers(0, 256, (270, 480, 3), dtype=np.uint8), SIZE, interpolation=cv2.INTER_NEAREST)
            for _ in range(count)]


def roll(image: np.ndarray, degrees: float) -> np.ndarray:
    """광축 기준 회전(순수 회전 호모그래피의 한 경우). 쓰러짐 흉내."""
    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return cv2.warpAffine(image, M, (w, h), borderMode=cv2.BORDER_REFLECT)


class BenchBackground:
    """BackgroundCleanBackground 의 회전 복구 면만 흉내 낸다(카메라별 960 px 축소 표본 창·세대·기준 배경 목록)."""

    def __init__(self, root: Path, images: dict[str, np.ndarray], *, crowd: bool = False, seed: int = 746) -> None:
        self.settings = SimpleNamespace(min_samples_per_camera=WINDOW)
        self.side = 960
        self.crowd = crowd
        self.rng = np.random.default_rng(seed)
        self.scene = dict(images)
        self.generations = {cid: 0 for cid in images}
        self.samples: dict[str, deque] = {cid: deque(maxlen=WINDOW) for cid in images}
        self._reference = {}
        mask = np.full(SIZE[::-1], 255, np.uint8)
        for cid, image in images.items():
            cv2.imwrite(str(root / f"{cid}.png"), image)
            cv2.imwrite(str(root / f"{cid}.mask.png"), mask)
            self._reference[cid] = (root, {"camera_generations": {cid: 0}, "cameras": {
                cid: {"image": f"{cid}.png", "valid_mask": f"{cid}.mask.png"}}})
        self.resets: list[str] = []
        # 분리 켬 시험에서 검출 프로세스 곁가지 상태(ChildSideWork.status)가 읽는 칸
        self.dropped, self.disabled, self.last_error, self.worker_restarts = 0, False, None, 0

    def configure_rotation_samples(self, side: int) -> None:
        self.side = int(side)

    def add_sample(self, cid: str, at_ms: int) -> None:
        frame = self.scene[cid].astype(np.int16) + self.rng.integers(-3, 4, self.scene[cid].shape)
        frame = np.clip(frame, 0, 255).astype(np.uint8)
        reveal = np.ones(SIZE[::-1], bool)
        cover = 0.0
        if self.crowd:
            for _ in range(5):
                y, x = int(self.rng.integers(0, SIZE[1] - 340)), int(self.rng.integers(0, SIZE[0] - 200))
                reveal[y:y + 300, x:x + 120] = False
                frame[y + 300:y + 340, x:x + 200] = int(self.rng.integers(0, 255))
            cover = float(1.0 - reveal.mean())
        small, mask = resize_samples([(frame, reveal)], self.side)[0]
        self.samples[cid].append((at_ms, small, mask, cover))

    def recent_signature(self, cid: str):
        q = self.samples[cid]
        if len(q) < WINDOW:
            return None
        return self.generations[cid], q[0][0], q[-1][0]

    def rotation_snapshot(self, exclude_cameras=(), omit_samples=None):
        views = {}
        for cid in self.samples:
            if cid in set(exclude_cameras or ()):
                continue
            sig = self.recent_signature(cid)
            if sig is None:
                return None
            q = list(self.samples[cid])
            revealed = np.logical_or.reduce([s[2] for s in q])
            views[cid] = {"signature": sig, "size": SIZE, "sample_times": [s[0] for s in q],
                          "samples": [(s[1], s[2]) for s in q],
                          "mean_person_cover_ratio": round(float(np.mean([s[3] for s in q])), 6),
                          "never_revealed_ratio": round(float(1.0 - revealed.mean()), 6)}
        return omit_rotation_samples(views, omit_samples) if views else None

    def reset_camera(self, cid: str) -> None:
        self.generations[cid] += 1
        self.samples[cid].clear()
        self.resets.append(cid)


def make_rig(root: Path, images: list[np.ndarray], *, budget_ms: int, crowd: bool = False,
             interval_ms: int = 15_000, clock=None):
    """4대 하네스: (회전 복구, 배경 흉내, 현재 시각 상자). clock 은 작업자 CPU 시계를 바꿀 때만 준다."""
    cids = [f"cam_{c}" for c in "ABCD"[:len(images)]]
    camera = {"image_size": list(SIZE), "K": K, "R": np.eye(3).tolist(), "t": [0.0, 0.0, 0.0],
              "distortion": {"model": "OPENCV", "coeffs": [0.0, 0.0, 0.0, 0.0, 0.0]}}
    document = {"calibration_version": "bench746", "cameras": [{**copy.deepcopy(camera), "camera_id": c} for c in cids]}
    background = BenchBackground(root, dict(zip(cids, images)), crowd=crowd)
    now = [1_763_200_000_000]
    recovery = BackgroundRotationRecovery(
        document, {}, background,
        settings=RotationRecoverySettings(interval_ms=interval_ms, cpu_budget_ms=budget_ms),
        now_ms=lambda: now[0], stop=threading.Event(), propose=lambda *_: False)
    if clock is not None:
        from simai_ai.calibration.rotation_worker import RotationComputation

        cv2.setNumThreads(1)  # 작업자 프로세스와 같게(그렇지 않으면 OpenCV 스레드 CPU 가 합쳐진다)
        recovery._compute = RotationComputation(cpu_clock=clock).compute
    return recovery, background, now


def advance(background: BenchBackground, now: list[int], interval_ms: int = 15_000) -> None:
    """한 주기 동안 카메라마다 5 s 간격 표본을 넣는다(창이 비었으면 지난 시각으로 먼저 채운다)."""
    per = interval_ms // SAMPLE_INTERVAL_MS
    for cid, q in background.samples.items():
        for i in range(WINDOW - per - len(q), 0, -1):
            background.add_sample(cid, now[0] - i * SAMPLE_INTERVAL_MS)
    for _ in range(per):
        now[0] += SAMPLE_INTERVAL_MS
        for cid in background.samples:
            background.add_sample(cid, now[0])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", type=Path)
    parser.add_argument("--crowd", action="store_true")
    parser.add_argument("--cycles", type=int, default=8)
    parser.add_argument("--budget-ms", type=int, default=RotationRecoverySettings().cpu_budget_ms)
    parser.add_argument("--fall")
    parser.add_argument("--fall-cycle", type=int, default=3)
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--cpu-scale", type=float, default=0.0,
                        help="0 이 아니면 작업자를 같은 프로세스에서 돌리고 CPU 시계를 이 배수로 늘린다(느린 서버 코어 흉내)")
    args = parser.parse_args()
    if args.images:
        files = sorted(glob.glob(str(args.images / "background_cam_?.png")))
        images = [cv2.imread(f) for f in files]
        assert len(images) == 4 and all(i.shape[1::-1] == SIZE for i in images), files
    else:
        images = synthetic_images()
    if args.temp_root:
        args.temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.temp_root) as root:
        clock = None
        if args.cpu_scale:
            import time

            clock = lambda: time.process_time() * args.cpu_scale  # noqa: E731
        recovery, background, now = make_rig(Path(root), images, budget_ms=args.budget_ms, crowd=args.crowd,
                                             clock=clock)
        fall_ms = None
        try:
            for cycle in range(args.cycles):
                if args.fall and cycle == args.fall_cycle:
                    background.scene[args.fall] = roll(background.scene[args.fall], 30.0)
                    fall_ms = now[0]
                advance(background, now)
                report = recovery.run_once() or {}
                row = {
                    "cycle": cycle, "status": report.get("status"), "last_status": recovery.last_status,
                    "cpu_ms": {k: round(v, 1) for k, v in recovery.last_cpu_ms.items()},
                    "cpu_ms_by_camera": {c: round(sum(r.values()), 1)
                                         for c, r in sorted(recovery.last_cpu_ms_by_camera.items())},
                    "refreshed": recovery.last_refreshed, "carried": recovery.last_carried,
                    "pending": recovery.last_pending, "feature_sides": recovery.feature_sides,
                    "samples_sent": recovery.samples_sent, "samples_omitted": recovery.samples_omitted,
                    "rotations": {c: (r["status"], r["angle_deg"]) for c, r in sorted(recovery.last_rotations.items())},
                }
                if fall_ms is not None and args.fall in background.resets:
                    row["fall_confirmed_after_s"] = round((now[0] - fall_ms) / 1000, 1)
                    fall_ms = None
                print(json.dumps(row, ensure_ascii=False, default=list), flush=True)
        finally:
            recovery.close()


if __name__ == "__main__":
    main()
