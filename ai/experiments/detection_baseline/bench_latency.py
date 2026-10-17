"""YOLO26-pose 크기별 지연·VRAM 측정 (G0 검출 기준선, S15P31A101-18).

2304×1296(C110 stream1) 프레임을 batch 1(카메라 1대)·4(카메라 4대 동시)로 넣어 model.predict 한 번의
벽시계 지연(전처리 리사이즈 + 추론 + 후처리, CUDA 동기화 포함)과 ultralytics 단계별 시간, 최대 VRAM 을 잰다.
NMS 경로(nms=True: one-to-many 헤드 + NMS)와 NMS-free 경로(nms=False: one-to-one 헤드)를 나눠 잰다.
--backend engine 은 TensorRT FP16 엔진(dynamic, 최대 batch 4)을 크기·경로·imgsz 마다 내보내 재며,
NMS 경로 엔진은 one-to-many 원출력을 내고 NMS 는 ultralytics 후처리(torchvision)가 한다.

  python ai/experiments/detection_baseline/bench_latency.py --run-id lat-4050-pt-fp16
  python ai/experiments/detection_baseline/bench_latency.py --run-id lat-4050-trt-fp16 --backend engine
"""
from __future__ import annotations

import argparse
import gc
import json
import time

import cv2
import numpy as np
import torch

import det_common as C

MODES = {"nms": True, "e2e": False}  # predict(nms=...) 값. ultralytics 8.4: False → NMS-free 헤드, True → NMS 헤드


def bench_image(subset_path: str) -> tuple[np.ndarray, dict]:
    """부분집합에서 사람 주석이 가장 많은 이미지를 2304×1296 으로 늘린다(지연 측정 전용, 정확도에는 쓰지 않음)."""
    ids = set(json.loads(open(subset_path, encoding="utf-8").read())["image_ids"])
    ann = json.loads(C.COCO_ANN.read_text(encoding="utf-8"))
    count: dict[int, int] = {}
    for a in ann["annotations"]:
        if a["image_id"] in ids and not a["iscrowd"]:
            count[a["image_id"]] = count.get(a["image_id"], 0) + 1
    img_id = max(sorted(count), key=lambda k: count[k])
    src = cv2.imread(str(C.COCO_IMG / f"{img_id:012d}.jpg"))
    img = cv2.resize(src, (C.CAM_W, C.CAM_H), interpolation=cv2.INTER_CUBIC)
    return img, {"coco_image_id": img_id, "gt_people": count[img_id], "src_hw": list(src.shape[:2]),
                 "resized_wh": [C.CAM_W, C.CAM_H], "resized_sha256": C.sha256_bytes(img.tobytes())}


def gpu_temp() -> str:
    return C._run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"])


def device_mem_mb() -> tuple[float | None, float | None]:
    """장치 전체 (사용, 총) 메모리(nvidia-smi). TensorRT 엔진 메모리는 torch 통계에 잡히지 않아 함께 남긴다. 다른 프로세스 몫도 들어간다."""
    v = C._run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"])
    if not v:
        return None, None
    used, total = (float(x) for x in v.split(","))
    return used, total


def run_cfg(model, frames, imgsz: int, nms: bool, warmup: int, repeat: int, quantize: int) -> dict:
    kw = dict(imgsz=imgsz, quantize=quantize, nms=nms, verbose=False)
    for _ in range(warmup):
        model.predict(frames, **kw)
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    wall, pre, inf, post = [], [], [], []
    for _ in range(repeat):
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        r = model.predict(frames, **kw)
        torch.cuda.synchronize()
        wall.append((time.perf_counter() - t0) * 1000)
        # Results.speed 는 배치 평균(장당) ms 다. 호출 한 번 기준으로 되돌린다
        b = len(frames)
        pre.append(r[0].speed["preprocess"] * b)
        inf.append(r[0].speed["inference"] * b)
        post.append(r[0].speed["postprocess"] * b)
    p = C.pct(wall)
    used, total = device_mem_mb()
    return {
        "end2end_head": bool(model.predictor.model.end2end),
        "input_tensor_shape": list(model.predictor.preprocess(frames).shape),
        "wall_ms": p,
        "stage_ms": {"preprocess": C.pct(pre), "inference": C.pct(inf), "postprocess": C.pct(post)},
        "calls_per_s_p50": round(1000 / p["p50"], 2),  # batch 4 이면 카메라당 FPS 와 같다
        "frames_per_s_p50": round(len(frames) * 1000 / p["p50"], 2),
        "people_detected": len(r[0].boxes),
        "vram_peak_alloc_mb": round(torch.cuda.max_memory_allocated() / 2**20, 1),
        "vram_peak_reserved_mb": round(torch.cuda.max_memory_reserved() / 2**20, 1),
        "vram_device_used_mb": used,
        # Windows(WDDM)는 장치 메모리가 차면 공유 시스템 메모리로 넘겨 지연이 몇 배로 는다. 이 행은 지연 비교에 쓰지 않는다
        "vram_near_full": bool(used and total and used >= 0.95 * total),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--sizes", nargs="+", default=list(C.SIZES), choices=C.SIZES)
    ap.add_argument("--imgsz", type=int, nargs="+", default=[640, 1280])
    ap.add_argument("--batches", type=int, nargs="+", default=[1, 4])
    ap.add_argument("--modes", nargs="+", default=list(MODES), choices=list(MODES))
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--repeat", type=int, default=50)
    ap.add_argument("--quantize", type=int, default=16, choices=[16, 32], help="16 = FP16 (pt). engine 은 늘 FP16")
    ap.add_argument("--backend", default="pt", choices=["pt", "engine"])
    ap.add_argument("--subset", default=str(C.RUNS / "coco_subset_500_s0.json"))
    a = ap.parse_args()

    img, img_meta = bench_image(a.subset)
    out = C.RUNS / a.run_id / "metrics.json"
    res = {"run_id": a.run_id, "jira_key": "S15P31A101-18", "kind": "latency", "started_at": C.now_ms(),
           "git": C.git_info(), "env": C.env_info(), "input": {"source": "DATASET", **img_meta},
           "config": {k: v for k, v in vars(a).items() if k != "run_id"}, "weights": {}, "rows": []}
    for size in a.sizes:
        for mode in a.modes:
            for s in a.imgsz:
                model, w = C.load_backend(size, mode, s, a.backend, max(a.batches))
                res["weights"][f"{size}_{mode}_{s}"] = w
                for b in a.batches:
                    row = {"size": size, "mode": mode, "imgsz": s, "batch": b, "temp_c_before": gpu_temp()}
                    try:
                        row |= {"status": "OK", **run_cfg(model, [img] * b, s, MODES[mode], a.warmup, a.repeat, a.quantize)}
                    except torch.cuda.OutOfMemoryError as e:
                        row |= {"status": "OOM", "error": str(e).splitlines()[0]}
                        torch.cuda.empty_cache()
                    row["temp_c_after"] = gpu_temp()
                    res["rows"].append(row)
                    msg = (f"p50={row['wall_ms']['p50']} p95={row['wall_ms']['p95']} ms vram={row['vram_peak_alloc_mb']} MB "
                           f"people={row['people_detected']} e2e_head={row['end2end_head']}") if row["status"] == "OK" else row["status"]
                    print(f"[{a.run_id}] {size} {mode} imgsz={s} batch={b} {msg}", flush=True)
                    C.write_json(out, res)  # 중간에 죽어도 지난 행은 남긴다
                del model
                gc.collect()
                torch.cuda.empty_cache()
    res["finished_at"] = C.now_ms()
    res["status"] = "SUCCEEDED"
    C.write_json(out, res)
    print(f"[{a.run_id}] 완료 → {out}")


if __name__ == "__main__":
    main()
