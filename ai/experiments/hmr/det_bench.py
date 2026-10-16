# EXP-02 E-GATE-0 (S15P31A101-86): 검출기(YOLO26x-Pose) 처리 시간을 잰다. Tier 0 에서 카메라 4대 × 실시간으로 감당되는지 보는 용도.
"""2K(2304×1296, C110 stream1) 크기로 키운 다인원 사진으로 imgsz 640/1280, batch 1(카메라 1대)/4(카메라 4대 동시) 지연과 검출 인원을 기록한다.

  envs/fast_sam3db/bin/python setup/hmr/det_bench.py --run-id H6_det_yolo26x
"""
import argparse
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--run-id", required=True); ap.add_argument("--yolo", default=None)
ap.add_argument("--image", default="t3_multi_f050"); ap.add_argument("--sizes", type=int, nargs="+", default=[640, 1280])
ap.add_argument("--batches", type=int, nargs="+", default=[1, 4]); ap.add_argument("--warmup", type=int, default=5)
ap.add_argument("--repeat", type=int, default=30); ap.add_argument("--half", action="store_true", help="FP16 (Fast 저장소 기본과 같음)")
a = ap.parse_args()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fs3db_common as C  # noqa: E402
import cv2  # noqa: E402
import torch  # noqa: E402
from ultralytics import YOLO  # noqa: E402
import ultralytics  # noqa: E402

w = str(a.yolo or C.YOLO)
img = cv2.resize(cv2.imread(str(C.INPUTS / f"{a.image}.jpg")), (2304, 1296), interpolation=cv2.INTER_CUBIC)
model = YOLO(w)
res = {"run_id": a.run_id, "weights": Path(w).name, "ultralytics": ultralytics.__version__, "input": f"{a.image} → 2304x1296",
       "half": a.half, "env": C.env_summary(), "rows": []}
for s in a.sizes:
    for b in a.batches:
        frames = [img] * b
        for _ in range(a.warmup):
            model.predict(frames, imgsz=s, half=a.half, verbose=False)
        ts = []
        torch.cuda.reset_peak_memory_stats()
        for _ in range(a.repeat):
            C.sync(); t0 = time.perf_counter()
            r = model.predict(frames, imgsz=s, half=a.half, verbose=False)
            C.sync(); ts.append((time.perf_counter() - t0) * 1000)
        n = len(r[0].boxes)
        row = {"imgsz": s, "batch": b, "ms": C.pct(ts), "ms_per_frame_p50": round(C.pct(ts)["p50"] / b, 2), "people_detected": n,
               "peak_alloc_mb": C.mb(torch.cuda.max_memory_allocated())}
        res["rows"].append(row)
        print(f"[{a.run_id}] imgsz={s} batch={b} p50={row['ms']['p50']} ms ({row['ms_per_frame_p50']} ms/frame) people={n} peak={row['peak_alloc_mb']} MB", flush=True)
C.write_json(C.OUT / a.run_id / "metrics.json", res)
print(f"[{a.run_id}] RESULT SUCCESS")
