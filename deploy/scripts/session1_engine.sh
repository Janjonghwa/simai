#!/usr/bin/env bash
# GPU 1차 세션 큐 b 항목: Tier 0 이미지(S15P31A101-32) Pod 검증과 TensorRT 엔진 생성. deploy/README.md "Pod 검증 큐" 1)~7) 을
# 한 Job 으로 묶었다. Tier 0 이미지로 띄운 Pod(시작 명령 sleep infinity) 안에서 GPU 세션 큐가 부른다.
#   bash deploy/scripts/session1_engine.sh
# 환경: SIMAI_ROOT(기본 /workspace/simai), SIMAI_PY(기본 python), SIMAI_NVIDIA_SMI(기본 nvidia-smi),
#       SIMAI_TIER0_WEIGHTS·SIMAI_TIER0_WEIGHTS_SHA256·SIMAI_ENGINE_CACHE, SIMAI_RUN_DIR(결과 폴더, 큐가 준다).
# 단계마다 종료 코드·시간을 steps.tsv 에, 요약을 metrics.json(스칼라)에 쓴다. 한 단계가 실패해도 다음 단계를 돈다.
# 앞 단계가 깨져 뒤 단계가 의미 없으면(GPU·TensorRT 없음) 뒤 단계는 SKIP 으로 남기고 바로 끝낸다(멈추지 않는다).
# 종료 코드: 0 필수 단계 모두 통과, 1 실패한 필수 단계가 있음.
set -uo pipefail

ROOT=${SIMAI_ROOT:-/workspace/simai}
PY=${SIMAI_PY:-python}
SMI=${SIMAI_NVIDIA_SMI:-nvidia-smi}
OUT=${SIMAI_RUN_DIR:-$PWD/session1-engine}
WEIGHTS=${SIMAI_TIER0_WEIGHTS:-$ROOT/weights/yolo/yolo26x-pose.pt}
CACHE=${SIMAI_ENGINE_CACHE:-$ROOT/engines}
mkdir -p "$OUT" || { echo "결과 폴더를 만들지 못함: $OUT" >&2; exit 1; }
STEPS=$OUT/steps.tsv
printf 'step\trequired\tstatus\texit\tseconds\n' > "$STEPS" || exit 1
FAILS=0
GPU_OK=1

# step <이름> <필수 1|0> <명령...> : 출력은 job.log 로, 결과 한 줄은 steps.tsv 로
step() {
  local name=$1 required=$2; shift 2
  local t0 t1 rc status
  echo "=== $name: $*"
  t0=$(date +%s.%N)
  "$@"
  rc=$?
  t1=$(date +%s.%N)
  if [ "$rc" -eq 0 ]; then status=PASS; else status=FAIL; fi
  if [ "$status" = FAIL ] && [ "$required" = 1 ]; then FAILS=$((FAILS + 1)); fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$required" "$status" "$rc" "$(awk "BEGIN{printf \"%.3f\", $t1-$t0}")" >> "$STEPS"
  echo "=== $name → $status (exit $rc)"
  return "$rc"
}
skip() { printf '%s\t%s\tSKIP\t\t0\n' "$1" "$2" >> "$STEPS"; echo "=== $1 → SKIP ($3)"; }
expect_code() {  # expect_code <기대 코드> <명령...> : 반례는 정해진 코드로 끝나야 통과
  local want=$1; shift
  "$@"; local rc=$?
  echo "exit=$rc (기대 $want)"
  [ "$rc" -eq "$want" ]
}

# 1) GPU·드라이버·CUDA 호환. 드라이버 580 미만이면 순방향 호환이 실제로 되는지 torch 로 판정한다
# shellcheck disable=SC2086  # SMI 는 드라이런에서 'python fake.py' 처럼 여러 낱말이다
step gpu_query 1 $SMI --query-gpu=name,driver_version,compute_cap,memory.total --format=csv || GPU_OK=0
step torch_cuda 1 "$PY" -c "
import torch, sys
ok = torch.cuda.is_available()
print(torch.__version__, torch.version.cuda, ok, torch.cuda.get_device_capability(0) if ok else None)
sys.exit(0 if ok else 1)" || GPU_OK=0
step tensorrt_import 1 "$PY" -c "import tensorrt as t; print(t.__version__)" || GPU_OK=0

if [ "$GPU_OK" -ne 1 ]; then
  for s in engine_build engine_cache_hit engine_infer entrypoint weights_hash_mismatch weights_missing plan_other_gpu; do
    skip "$s" 1 "GPU·torch CUDA·TensorRT 확인 실패"
  done
  FAILS=$((FAILS + 7))
else
  # 2) 엔진 첫 빌드 (H100 에서 수 분 예상)
  EXPECT=()
  [ -n "${SIMAI_TIER0_WEIGHTS_SHA256:-}" ] && EXPECT=(--expect-sha256 "$SIMAI_TIER0_WEIGHTS_SHA256")
  step engine_build 1 "$PY" -m simai_ai.detection.engine_cache ensure --weights "$WEIGHTS" --cache-root "$CACHE" "${EXPECT[@]}"
  mkdir -p "$OUT/engine-meta"
  cp "$CACHE"/yolo26x-pose/sm_*/trt*/*.json "$OUT/engine-meta/" 2>/dev/null || true
  # 3) 다시 실행하면 캐시를 쓴다 ("엔진 캐시 사용", 1초대)
  step engine_cache_hit 1 "$PY" -m simai_ai.detection.engine_cache ensure --weights "$WEIGHTS" --cache-root "$CACHE"
  # 4) 엔진으로 추론 1회 (batch 4, 2304x1296)
  step engine_infer 1 "$PY" - "$CACHE" <<'PY'
import glob, sys, time
import numpy as np
from ultralytics import YOLO
eng = sorted(glob.glob(f"{sys.argv[1]}/yolo26x-pose/sm_*/trt*/*.engine"))[-1]
m = YOLO(eng, task="pose")
frames = [np.zeros((1296, 2304, 3), np.uint8)] * 4
m.predict(frames, imgsz=1280, half=True, verbose=False)  # 예열
t = time.perf_counter()
r = m.predict(frames, imgsz=1280, half=True, verbose=False)
print(eng, len(r), r[0].orig_shape, f"batch4 {1000 * (time.perf_counter() - t):.1f} ms")
PY
  # 5) 진입점 전체: 설정 파일이 있으면 15 초 돌려 본다(LIVE 입력 디코더가 없으면 종료 코드 4, -182 뒤에는 0)
  if [ -f "$ROOT/tier0.json" ] && command -v simai-tier0 >/dev/null 2>&1; then
    step entrypoint 0 simai-tier0 --config "$ROOT/tier0.json" --run-seconds 15
  else
    skip entrypoint 0 "$ROOT/tier0.json 또는 simai-tier0 없음"
  fi
  # 6) 반례: 가중치 해시 불일치·가중치 없음은 GPU 를 쓰지 않고 종료 코드 2
  if command -v simai-tier0 >/dev/null 2>&1; then
    step weights_hash_mismatch 1 expect_code 2 env SIMAI_TIER0_WEIGHTS_SHA256="$(printf '0%.0s' {1..64})" simai-tier0
    step weights_missing 1 expect_code 2 env SIMAI_TIER0_WEIGHTS="$ROOT/weights/none.pt" simai-tier0
  else
    skip weights_hash_mismatch 1 "simai-tier0 없음(Tier 0 이미지가 아니다)"; FAILS=$((FAILS + 1))
    skip weights_missing 1 "simai-tier0 없음"; FAILS=$((FAILS + 1))
  fi
  # 7) 다른 GPU(sm_86) 캐시 경로는 sm_90 과 다르다
  TRT=$("$PY" -c 'import tensorrt as t; print(t.__version__)' 2>/dev/null)
  step plan_other_gpu 1 "$PY" -m simai_ai.detection.engine_cache plan --weights "$WEIGHTS" --cache-root "$CACHE" \
    --gpu-arch sm_86 --tensorrt "$TRT" --ultralytics 8.4.178
fi

# 요약(스칼라만): 단계별 상태와 시간
"$PY" - "$STEPS" "$OUT/metrics.json" "$FAILS" <<'PY' || echo "metrics.json 을 쓰지 못함" >&2
import csv, json, sys
rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8"), delimiter="\t"))
m = {"failed_required": int(sys.argv[3])}
for r in rows:
    m[f"{r['step']}_status"] = r["status"]
    if r["status"] != "SKIP":
        m[f"{r['step']}_s"] = float(r["seconds"])
json.dump(m, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PY
echo "session1_engine: 필수 단계 실패 $FAILS"
[ "$FAILS" -eq 0 ]
