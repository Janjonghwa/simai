#!/usr/bin/env bash
# 노트북 RTX 4050 측정 큐 (S15P31A101-18). 저장소 최상위에서 Git Bash 로 실행한다. AC 전원을 연결하고 다른 GPU 작업을 끈다.
#   PY=<측정 venv 의 python> bash ai/experiments/detection_baseline/queue_4050.sh
set -euo pipefail
PY=${PY:-python}
D=ai/experiments/detection_baseline
export PYTHONUTF8=1
TAG=$(git rev-parse --short HEAD)

"$PY" $D/prepare_coco_subset.py --n 500 --seed 0
"$PY" $D/bench_latency.py --run-id lat-4050-pt-fp16-$TAG --backend pt --warmup 30 --repeat 100
# TensorRT 엔진은 GPU 를 다른 프로세스가 잡지 않은 상태에서 미리 만든다(6GB 에서 x·1280 은 측정 프로세스가 떠 있으면 빌드가 메모리 부족으로 실패했다)
for s in n s m l x; do for m in nms e2e; do for i in 640 1280; do
  [ -f ai/experiments/recordings/weights/engines/yolo26$s-pose_${m}_${i}_b4_fp16.engine ] || "$PY" $D/export_engine.py --size $s --mode $m --imgsz $i
done; done; done
"$PY" $D/bench_latency.py --run-id lat-4050-trt-fp16-$TAG --backend engine --warmup 30 --repeat 100
"$PY" $D/eval_coco.py --run-id acc-4050-coco500-pt-$TAG --backend pt
"$PY" $D/eval_coco.py --run-id acc-4050-coco500-trt-$TAG --backend engine
"$PY" $D/conf_sweep.py --run-id acc-4050-coco500-pt-$TAG
"$PY" $D/summarize.py --tag $TAG
