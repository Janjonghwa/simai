"""YOLO26-pose TensorRT FP16 엔진 하나를 내보낸다. det_common.load_backend 가 별도 프로세스로 부른다.

같은 프로세스에서 내보낸 뒤 바로 재면 빌더가 남긴 장치 메모리 때문에 6GB GPU 가 공유 메모리로 넘쳐 지연이 몇 배로 늘었다.
그래서 내보내기는 이 스크립트로 프로세스를 나눈다. 동적 프로파일은 det_common.engine_profile 로 바꿔 끼운다
(ultralytics 8.4.178 의 onnx2engine 은 프로파일을 인자로 받지 않아 IOptimizationProfile.set_shape 를 감싼다).

  python ai/experiments/detection_baseline/export_engine.py --size s --mode e2e --imgsz 1280
"""
from __future__ import annotations

import argparse
import shutil

import det_common as C


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", required=True, choices=C.SIZES)
    ap.add_argument("--mode", required=True, choices=["nms", "e2e"])
    ap.add_argument("--imgsz", type=int, required=True)
    ap.add_argument("--max-batch", type=int, default=4)
    a = ap.parse_args()

    eng = C.engine_path(a.size, a.mode, a.imgsz, a.max_batch)
    prof = C.engine_profile(a.imgsz, a.max_batch)
    import tensorrt as trt

    set_shape = trt.IOptimizationProfile.set_shape

    def fixed_set_shape(self, name, min, opt, max):  # noqa: A002 - TensorRT 인자 이름을 그대로 쓴다
        return set_shape(self, name, min=tuple(prof["min"]), opt=tuple(prof["opt"]), max=tuple(prof["max"]))

    trt.IOptimizationProfile.set_shape = fixed_set_shape
    model, _ = C.load_model(a.size)
    # nms=False → one-to-one(NMS-free) 헤드, None → one-to-many 원출력(후처리에서 NMS). True(엔진 안 NMS)는 쓰지 않는다
    out = model.export(format="engine", imgsz=a.imgsz, quantize=16, dynamic=True, batch=a.max_batch,
                       nms=False if a.mode == "e2e" else None, device=0, verbose=False)
    eng.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(out, eng)
    print(f"엔진 → {eng} 프로파일 {prof}")


if __name__ == "__main__":
    main()
