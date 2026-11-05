"""수동 벤치(ai/): PYTHONPATH=. python tests/services/tier0/bench_rotation_recovery.py --temp-root <외부 임시 경로>."""

import argparse
import copy
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from services.tier0.clean_background import BackgroundCleanBackground
from services.tier0.rotation_recovery import BackgroundRotationRecovery
from services.tier0.settings import RotationRecoverySettings
from simai_ai.calibration.world_frame.clean_background import _Sample


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--budget-ms", type=int, default=500)
    parser.add_argument("--side", type=int, default=960)
    args = parser.parse_args()
    args.temp_root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(494)
    images = [cv2.resize(rng.integers(0, 256, (270, 480, 3), dtype=np.uint8), (2304, 1296), interpolation=cv2.INTER_NEAREST) for _ in range(4)]
    mask = np.full((1296, 2304), 255, np.uint8)
    camera = {"image_size": [2304, 1296], "K": [[1500, 0, 1152], [0, 1500, 648], [0, 0, 1]],
              "R": np.eye(3).tolist(), "t": [0, 0, 0],
              "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [0, 0, 0, 0]}}
    cameras = [{**copy.deepcopy(camera), "camera_id": str(i)} for i in range(4)]
    document = {"calibration_version": "bench494", "cameras": cameras}
    with tempfile.TemporaryDirectory(dir=args.temp_root) as root:
        root = Path(root)
        worker = BackgroundCleanBackground.__new__(BackgroundCleanBackground)
        worker._rotation_side = None
        worker._rotation_lock = threading.Lock()
        worker._rotation_samples = {}
        worker._rotation_views = {}
        worker._reference = {}
        worker._active = SimpleNamespace(generations={str(i): 0 for i in range(4)})
        worker._context = worker._active
        worker.accumulator = SimpleNamespace(_samples={}, config=SimpleNamespace(max_unrevealed_ratio=.1))
        worker.settings = SimpleNamespace(min_samples_per_camera=20)
        worker.reset_camera = lambda cid: None
        recovery = BackgroundRotationRecovery(document, {}, worker,
            settings=RotationRecoverySettings(cpu_budget_ms=args.budget_ms, thresholds={"max_image_side": args.side}),
            now_ms=lambda: 10000, stop=threading.Event(), propose=lambda *_: False)
        for i, image in enumerate(images):
            cid = str(i)
            cv2.imwrite(str(root / f"{cid}.png"), image)
            cv2.imwrite(str(root / f"{cid}.mask.png"), mask)
            worker._reference[cid] = (root, {"camera_generations": {cid: 0}, "cameras": {
                cid: {"image": f"{cid}.png", "valid_mask": f"{cid}.mask.png"}}})
            worker.accumulator._samples[cid] = [
                _Sample(frame_bgr=image, reveal_mask=mask.astype(bool), capture_time_ms=j,
                        person_cover_ratio=0, byte_size=0) for j in range(20)]
        try:
            for cycle in range(args.cycles + 1):
                start = time.thread_time()
                for i in range(4):
                    cid = str(i)
                    if cycle:
                        q = worker.accumulator._samples[cid]
                        q.pop(0)
                        q.append(_Sample(frame_bgr=images[i], reveal_mask=mask.astype(bool),
                            capture_time_ms=20 + cycle, person_cover_ratio=0, byte_size=0))
                    worker._publish_rotation(cid)
                prepare_ms = (time.thread_time() - start) * 1000
                wall = time.perf_counter()
                report = recovery.run_once()
                print(json.dumps({"report_reason": report.get("reason"),"cycle": cycle, "status": report["status"],
                    "worker_pid": recovery.worker_pid, "parent_prepare_cpu_ms": prepare_ms,
                    "feature_sides": recovery.feature_sides,
                    "matches": {cid: fit.get("matches") for cid, fit in report.get("rotations", {}).items()},
                    "wall_ms": (time.perf_counter() - wall) * 1000,
                    "worker_cpu_ms": recovery.last_cpu_ms}), flush=True)
                if cycle and report["status"] != "STABLE":
                    raise AssertionError(report)
            assert max(recovery.last_cpu_ms.values()) <= args.budget_ms
        finally:
            recovery.close()


if __name__ == "__main__":
    main()
