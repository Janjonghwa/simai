#!/usr/bin/env bash
# S15P31A101-538: kanana-2-v-decide-3b-2611 + T1-v4-ALL YOLO26x(PpeDetector) GPU Pod 실행 큐 스크립트
#
# 특징 및 주의사항:
# 1. 별도 가상환경(기본 /workspace/venv-kanana-decide)을 만들어 transformers==5.5.3, torch==2.10.0 등
#    모델 요구 사양을 격리 설치하고, 운영 PpeDetector(YOLO26x) 실행을 위해 ultralytics==8.4.178 을 --no-deps 로 추가한다
#    (Tier 0/워커 통합 venv 인 /opt/simai/ai/.venv 를 건드리지 않고, torch==2.10.0 및 opencv-python-headless 도 덮어쓰지 않음).
# 2. 가중치 준비:
#    - PPE YOLO26x(T1-v4-ALL-seed20261021-epoch08.pt): SIMAI_PPE_WEIGHTS 또는 $SIMAI_ROOT/weights/ppe/... 가 있으면 그대로 쓰고,
#      없으면 simai-bootstrap(묶음 ppe) 또는 HF_TOKEN 으로 비공개 저장소(simai-s15p31a101/simai-ppe-helmet-vest @36e3ebb1...)에서 받아
#      크기(471,779,845 B)와 sha256(e373a104805765417502e34d21586be2607126bbdd8499b935896ca333eb103c)을 검증한다.
#    - kanana-2-v-decide-3b-2611: HF_TOKEN 으로 snapshot_download 한다.
#    - 토큰 값(HF_TOKEN)은 절대 표준출력/로그에 찍지 않는다(set +x 유지).
# 3. LIVE 관제 시간에는 실행하지 않는다(SIMAI_TIER0_MARKER 파일이 running 이면 즉시 중단, AGENTS.md 규칙).
#
# 사용법 (Pod 안):
#   export HF_TOKEN="<토큰>"
#   bash ai/experiments/decide_model/pod_decide.sh \
#     --bundle /workspace/decide-bench/input_bundle \
#     --out-dir /workspace/decide-bench/results

set -euo pipefail
set +x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REPO_DIR="$(cd "${AI_DIR}/.." && pwd)"

SIMAI_ROOT_DIR="${SIMAI_ROOT:-/srv/simai}"
VENV_DIR="${DECIDE_VENV_DIR:-/workspace/venv-kanana-decide}"
MODEL_ID="${DECIDE_MODEL_ID:-kakaocorp/kanana-2-v-decide-3b-2611}"
WEIGHTS_DIR="${DECIDE_WEIGHTS_DIR:-/workspace/models/kanana-2-v-decide-3b-2611}"

PPE_REPO_ID="simai-s15p31a101/simai-ppe-helmet-vest"
PPE_REVISION="36e3ebb10d44c38b46ab430d769b8d4334bdb5f4"
PPE_FILENAME="T1-v4-ALL-seed20261021-epoch08.pt"
PPE_SHA256="e373a104805765417502e34d21586be2607126bbdd8499b935896ca333eb103c"
PPE_SIZE_BYTES="471779845"
PPE_WEIGHTS_PATH="${SIMAI_PPE_WEIGHTS:-${SIMAI_ROOT_DIR}/weights/ppe/${PPE_FILENAME}}"

BUNDLE_DIR="${DECIDE_BUNDLE_DIR:-${SCRIPT_DIR}/runs/input_bundle}"
OUT_DIR="${DECIDE_OUT_DIR:-${SCRIPT_DIR}/runs/pod_out}"
DTYPE="${DECIDE_DTYPE:-bfloat16}"
TIER0_MARKER="${SIMAI_TIER0_MARKER:-${SIMAI_ROOT_DIR}/runs/current/state/tier0.live}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)
      BUNDLE_DIR="$2"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="$2"
      shift 2
      ;;
    --venv)
      VENV_DIR="$2"
      shift 2
      ;;
    --weights-dir)
      WEIGHTS_DIR="$2"
      shift 2
      ;;
    --ppe-weights)
      PPE_WEIGHTS_PATH="$2"
      shift 2
      ;;
    --dtype)
      DTYPE="$2"
      shift 2
      ;;
    --tier0-marker)
      TIER0_MARKER="$2"
      shift 2
      ;;
    *)
      echo "[pod_decide] 알 수 없는 인자: $1" >&2
      exit 2
      ;;
  esac
done

# 1. LIVE 관제 보호 확인
if [[ -f "${TIER0_MARKER}" ]]; then
  if grep -Eq '"phase"[[:space:]]*:[[:space:]]*"(running|starting|live)"' "${TIER0_MARKER}"; then
    echo "[pod_decide] 오류: Tier 0 LIVE 표시 파일(${TIER0_MARKER})이 활성 상태입니다. LIVE 관제 중에는 오프라인 벤치마크를 돌리지 않습니다." >&2
    exit 1
  fi
fi

# 2. HF_TOKEN 확인 (값은 절대 출력하지 않음)
if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "[pod_decide] 오류: HF_TOKEN 환경변수가 비어 있습니다. (.env 또는 실행 환경에서 주입 필요)" >&2
  exit 1
fi
echo "[pod_decide] HF_TOKEN 확인 완료 (길이: ${#HF_TOKEN}자, 값은 출력하지 않음)"

# 3. 별도 venv 생성 및 고정 요구사항 설치 (ultralytics 는 --no-deps 로 설치해 torch==2.10.0 및 headless OpenCV 보존)
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  echo "[pod_decide] 별도 가상환경 생성: ${VENV_DIR}"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.11 "${VENV_DIR}"
  else
    python3 -m venv "${VENV_DIR}"
  fi
fi

echo "[pod_decide] 고정 의존성 설치: ${SCRIPT_DIR}/requirements.txt + ultralytics==8.4.178(--no-deps)"
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "${VENV_DIR}/bin/python" -r "${SCRIPT_DIR}/requirements.txt"
  uv pip install --python "${VENV_DIR}/bin/python" --no-deps "ultralytics==8.4.178"
else
  "${VENV_DIR}/bin/pip" install --upgrade pip
  "${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"
  "${VENV_DIR}/bin/pip" install --no-deps "ultralytics==8.4.178"
fi

# 4. T1-v4-ALL YOLO26x PPE 가중치 확인/다운로드 및 sha256 검증
if [[ ! -f "${PPE_WEIGHTS_PATH}" ]]; then
  if command -v simai-bootstrap >/dev/null 2>&1; then
    echo "[pod_decide] simai-bootstrap 으로 ppe 가중치 묶음을 받습니다."
    SIMAI_WEIGHTS_SETS="ppe" SIMAI_WEIGHTS_MANIFEST="${REPO_DIR}/deploy/weights.json" simai-bootstrap || true
  fi
fi

if [[ ! -f "${PPE_WEIGHTS_PATH}" ]]; then
  echo "[pod_decide] HF 비공개 저장소(${PPE_REPO_ID}@${PPE_REVISION:0:8})에서 PPE 가중치를 받습니다: ${PPE_WEIGHTS_PATH}"
  mkdir -p "$(dirname "${PPE_WEIGHTS_PATH}")"
  PYTHONPATH="${AI_DIR}/src:${PYTHONPATH:-}" \
    "${VENV_DIR}/bin/python" - <<'PY' "${PPE_REPO_ID}" "${PPE_REVISION}" "${PPE_FILENAME}" "${PPE_WEIGHTS_PATH}" "${PPE_SIZE_BYTES}" "${PPE_SHA256}"
import os
import shutil
import sys
from pathlib import Path
from huggingface_hub import hf_hub_download
from simai_ai.ppe.detector import verify_checkpoint

repo_id, revision, filename, dest_str, size_str, sha256 = sys.argv[1:7]
token = os.environ.get("HF_TOKEN")
if not token:
    raise SystemExit("HF_TOKEN 환경변수가 없습니다.")
dest = Path(dest_str)
cached = Path(
    hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        revision=revision,
        token=token,
    )
)
if cached.resolve() != dest.resolve():
    shutil.copy2(cached, dest)
verify_checkpoint(dest, int(size_str), sha256)
print(f"[pod_decide] PPE YOLO26x 가중치 검증 완료: {dest} (sha256={sha256[:12]}...)")
PY
else
  PYTHONPATH="${AI_DIR}/src:${PYTHONPATH:-}" \
    "${VENV_DIR}/bin/python" - <<'PY' "${PPE_WEIGHTS_PATH}" "${PPE_SIZE_BYTES}" "${PPE_SHA256}"
import sys
from pathlib import Path
from simai_ai.ppe.detector import verify_checkpoint

dest, size_str, sha256 = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
verify_checkpoint(dest, size_str, sha256)
print(f"[pod_decide] 기존 PPE YOLO26x 가중치 검증 완료: {dest} (sha256={sha256[:12]}...)")
PY
fi

# 5. kanana-2-v-decide-3b-2611 가중치 다운로드 (이미 있으면 재사용)
mkdir -p "${WEIGHTS_DIR}"
echo "[pod_decide] Decide 모델 가중치 확인/다운로드: ${MODEL_ID} -> ${WEIGHTS_DIR}"
"${VENV_DIR}/bin/python" - <<'PY' "${MODEL_ID}" "${WEIGHTS_DIR}"
import os
import sys
from huggingface_hub import snapshot_download

model_id = sys.argv[1]
local_dir = sys.argv[2]
token = os.environ.get("HF_TOKEN")
if not token:
    raise SystemExit("HF_TOKEN 환경변수가 없습니다.")
snapshot_download(
    repo_id=model_id,
    local_dir=local_dir,
    token=token,
)
print(f"[pod_decide] Decide 가중치 준비 완료: {local_dir}")
PY

# 6. 입력 묶음이 없으면 기본 생성기 실행
if [[ ! -f "${BUNDLE_DIR}/manifest.json" ]]; then
  echo "[pod_decide] 입력 묶음이 없어 prepare_inputs.py 로 생성합니다: ${BUNDLE_DIR}"
  PYTHONPATH="${AI_DIR}:${AI_DIR}/src:${PYTHONPATH:-}" \
    "${VENV_DIR}/bin/python" -m experiments.decide_model.prepare_inputs \
    --out "${BUNDLE_DIR}"
fi

# 7. 추론 실행 (run_decide.py: PpeDetector + kanana-2-v-decide-3b-2611) 및 비교 보고서 생성 (compare.py)
mkdir -p "${OUT_DIR}"
RESULTS_JSON="${OUT_DIR}/results_${DTYPE}.json"
COMP_JSON="${OUT_DIR}/comparison_${DTYPE}.json"
COMP_MD="${OUT_DIR}/comparison_${DTYPE}.md"

echo "[pod_decide] 추론 시작 (YOLO26x PpeDetector + Decide-3B dtype=${DTYPE})"
PYTHONPATH="${AI_DIR}:${AI_DIR}/src:${PYTHONPATH:-}" \
  "${VENV_DIR}/bin/python" -m experiments.decide_model.run_decide \
  --bundle "${BUNDLE_DIR}" \
  --out "${RESULTS_JSON}" \
  --model "${WEIGHTS_DIR}" \
  --ppe-weights "${PPE_WEIGHTS_PATH}" \
  --ppe-sha256 "${PPE_SHA256}" \
  --device cuda \
  --dtype "${DTYPE}" \
  --tier0-marker "${TIER0_MARKER}"

echo "[pod_decide] [YOLO26x(기준) | decide-3b | 둘 결합] 비교 표 및 확률 교정 곡선 산출"
PYTHONPATH="${AI_DIR}:${AI_DIR}/src:${PYTHONPATH:-}" \
  "${VENV_DIR}/bin/python" -m experiments.decide_model.compare \
  --results "${RESULTS_JSON}" \
  --out-json "${COMP_JSON}" \
  --out-md "${COMP_MD}"

echo "[pod_decide] 완료. 산출물:"
echo "  - ${RESULTS_JSON}"
echo "  - ${COMP_JSON}"
echo "  - ${COMP_MD}"
echo "[pod_decide] 인스턴스 삭제 전 위 산출물 JSON/MD 를 노트북으로 회수하고 Pod 를 즉시 Stop 하십시오."
