#!/usr/bin/env bash
# S15P31A101-537: WorldMirror 2.0 / 1.0 · Pi3X · DA3 깊이 어댑터 & -528 기하·텍스처 비교(복셀 2 cm) Pod 스크립트
#
# 기본 Pod 경로:
#   REPO_DIR=/workspace/S15P31A101
#   MA_SINGLE_DIR=/workspace/out/ma_single   (mapanything/, prepared/)
#   WM2_WEIGHTS_DIR=/workspace/weights/hy-world-2.0
#   PI3_WEIGHTS_DIR=/workspace/weights/pi3x
#   WM1_WEIGHTS_DIR=/workspace/weights/worldmirror1
#   DA3_VENV=/root/da3
#   DA3_WEIGHTS_DIR=/workspace/weights/da3nested-giant-large-1.1
#   OUT_ROOT=/workspace/out/depth_adapters_537
#
# 사용법 (Pod 안):
#   # 1) 환경 설치 (가중치가 이미 /workspace/weights/* 에 있으면 다운로드 생략)
#   bash ai/experiments/static_scene/pod_depth_adapters.sh setup_wm2 setup_pi3x
#   bash ai/experiments/static_scene/pod_depth_adapters.sh setup_wm1
#
#   # 2) 깊이 추론 (후보별 predict)
#   bash ai/experiments/static_scene/pod_depth_adapters.sh predict_wm2 predict_wm2_depthprior
#   bash ai/experiments/static_scene/pod_depth_adapters.sh predict_pi3x predict_pi3x_depthprior
#   bash ai/experiments/static_scene/pod_depth_adapters.sh predict_wm1 predict_wm1_depthprior
#   bash ai/experiments/static_scene/pod_depth_adapters.sh predict_da3
#
#   # 3) -528 (run_bench.py, v2_clean.py, metrics.py) 복셀 2 cm 메시·정리·지표 비교
#   bash ai/experiments/static_scene/pod_depth_adapters.sh bench_compare
#
#   # 전체 일괄 실행 (하나가 실패해도 다음 후보 계속 진행)
#   bash ai/experiments/static_scene/pod_depth_adapters.sh all

set -uo pipefail

# ---------------------------------------------------------------------------
# 1. 컨테이너 PID 1 환경변수(VIRTUAL_ENV, PATH, HF_TOKEN 등) 로드 — 토큰 값은 절대 출력하지 않는다
# ---------------------------------------------------------------------------
if [[ -r /proc/1/environ ]]; then
    while IFS='=' read -r -d '' k v; do
        case "$k" in
            VIRTUAL_ENV)
                if [[ -z "${VIRTUAL_ENV:-}" && -n "$v" ]]; then
                    export VIRTUAL_ENV="$v"
                fi
                ;;
            HF_TOKEN|HUGGING_FACE_HUB_TOKEN|CUDA_VISIBLE_DEVICES)
                if [[ -z "${!k:-}" && -n "$v" ]]; then
                    export "$k=$v"
                fi
                ;;
            PATH)
                export PATH="$v:$PATH"
                ;;
        esac
    done < /proc/1/environ
fi
if [[ -n "${VIRTUAL_ENV:-}" && -d "${VIRTUAL_ENV}/bin" ]]; then
    export PATH="${VIRTUAL_ENV}/bin:$PATH"
fi
export PATH="/root/.local/bin:/root/.cargo/bin:$PATH"
export HF_HUB_ENABLE_HF_TRANSFER=0

BASE_PY="${BASE_PY:-${VIRTUAL_ENV:+${VIRTUAL_ENV}/bin/python}}"
if [[ -z "$BASE_PY" || ! -x "$BASE_PY" ]]; then
    BASE_PY="$(command -v python3 || command -v python)"
fi

# ---------------------------------------------------------------------------
# 2. 고정 커밋·리비전·Pod 기본 경로 상수
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-/workspace/S15P31A101}"
AI_DIR="${AI_DIR:-${REPO_DIR}/ai}"
RUNNER_PY="${RUNNER_PY:-${SCRIPT_DIR}/run_hybrid_depth.py}"

MA_SINGLE_DIR="${MA_SINGLE_DIR:-/workspace/out/ma_single}"
MA_RUN_DIR="${MA_RUN_DIR:-${MA_SINGLE_DIR}/mapanything}"
PREPARED_DIR="${PREPARED_DIR:-${MA_SINGLE_DIR}/prepared}"
OUT_ROOT="${OUT_ROOT:-/workspace/out/depth_adapters_537}"
VOXEL_M="${VOXEL_M:-0.02}"

WM_VENV="${WM_VENV:-/root/wm2}"
PI3X_VENV="${PI3X_VENV:-/root/pi3x}"
DA3_VENV="${DA3_VENV:-/root/da3}"

WM2_REPO_URL="https://github.com/Tencent-Hunyuan/HY-World-2.0.git"
WM2_COMMIT="df9988efb87bfc0f4947eb3889411cf957478b06"
WM2_REPO_DIR="${WM2_REPO_DIR:-/root/HY-World-2.0}"
WM2_HF_REPO="tencent/HY-World-2.0"
WM2_HF_REV="d78a16c91c7a56488894a1c8de4f5c7cc28aa8b0"
WM2_WEIGHTS_DIR="${WM2_WEIGHTS_DIR:-/workspace/weights/hy-world-2.0}"

WM1_REPO_URL="https://github.com/Tencent-Hunyuan/HunyuanWorld-Mirror.git"
WM1_COMMIT="c61300e47ce06db2f02c8e2551aae68be7ae3a0b"
WM1_REPO_DIR="${WM1_REPO_DIR:-/root/HunyuanWorld-Mirror}"
WM1_HF_REPO="tencent/HunyuanWorld-Mirror"
WM1_HF_REV="5574b7b0d5ac9d80e8a92976222370a0d20a57a0"
WM1_WEIGHTS_DIR="${WM1_WEIGHTS_DIR:-/workspace/weights/worldmirror1}"

PI3_REPO_URL="https://github.com/yyfz/Pi3.git"
PI3_COMMIT="9fa3ddb3f8d53041f8b2738df404f62223bbaa7b"
PI3_REPO_DIR="${PI3_REPO_DIR:-/root/Pi3}"
PI3_HF_REPO="yyfz233/Pi3X"
PI3_HF_REV="bb1deea4d7423de5b30691739cb451a3f57dc1d5"
PI3_WEIGHTS_DIR="${PI3_WEIGHTS_DIR:-/workspace/weights/pi3x}"

DA3_COMMIT="${DA3_COMMIT:-3d835ec1a5802d64a8b8b15f817a1ab54809bfe4}"
DA3_WEIGHTS_DIR="${DA3_WEIGHTS_DIR:-/workspace/weights/da3nested-giant-large-1.1}"

WM2_PROCESS_RES="${WM2_PROCESS_RES:-952}"
WM1_PROCESS_RES="${WM1_PROCESS_RES:-518}"
PI3X_PROCESS_RES="${PI3X_PROCESS_RES:-672}"
DA3_PROCESS_RES="${DA3_PROCESS_RES:-1008}"

mkdir -p "$OUT_ROOT" /workspace/weights
SUMMARY_LOG="${OUT_ROOT}/pod_runs.jsonl"

log_info() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

gpu_mem_snapshot() {
    if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | tr '\n' ';'
    else
        echo "no-nvidia-smi"
    fi
}

# ---------------------------------------------------------------------------
# 3. 기본 시스템 도구 & uv 준비
# ---------------------------------------------------------------------------
ensure_base_tools() {
    local missing=()
    for cmd in curl bzip2 git; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if (( ${#missing[@]} > 0 )); then
        log_info "apt-get 으로 필수 도구 설치: ${missing[*]}"
        apt-get update -qq && apt-get install -y --no-install-recommends curl bzip2 git ca-certificates
    fi
    if ! command -v uv >/dev/null 2>&1; then
        log_info "uv 설치 중..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="/root/.local/bin:$PATH"
    fi
    log_info "BASE_PY=${BASE_PY} (VIRTUAL_ENV=${VIRTUAL_ENV:-none})"
    if [[ -n "${HF_TOKEN:-}" ]]; then
        log_info "HF_TOKEN 확인됨 (길이: ${#HF_TOKEN})"
    else
        log_info "경고: HF_TOKEN 이 비어 있습니다"
    fi
}

clone_at_commit() {
    local repo_url="$1"
    local target_dir="$2"
    local commit_sha="$3"
    if [[ ! -d "${target_dir}/.git" ]]; then
        log_info "클론: ${repo_url} -> ${target_dir}"
        git clone --no-checkout "$repo_url" "$target_dir"
    fi
    git -C "$target_dir" fetch --depth 1 origin "$commit_sha" || git -C "$target_dir" fetch origin
    git -C "$target_dir" checkout --force "$commit_sha"
    log_info "고정 커밋 체크아웃 완료: ${target_dir} @ $(git -C "$target_dir" rev-parse HEAD)"
}

ensure_wm_venv() {
    ensure_base_tools
    if [[ ! -x "${WM_VENV}/bin/python" ]]; then
        log_info "가상환경 생성: ${WM_VENV}"
        uv venv "$WM_VENV" --python "$BASE_PY"
    fi
    log_info "WorldMirror 가상환경(${WM_VENV}) 패키지 설치..."
    uv pip install --python "${WM_VENV}/bin/python" \
        --index-url https://download.pytorch.org/whl/cu124 \
        "torch==2.5.1" \
        "torchvision==0.20.1"
    uv pip install --python "${WM_VENV}/bin/python" \
        "numpy==1.26.4" \
        "einops==0.8.1" \
        "pillow==11.1.0" \
        "opencv-python-headless==4.11.0.86" \
        "scipy==1.15.2" \
        "safetensors==0.5.3" \
        "huggingface_hub==0.30.2" \
        "omegaconf==2.3.0" \
        "lightning-utilities==0.14.0"
}

# ---------------------------------------------------------------------------
# 4. 모델별 Setup 단계 (가중치가 이미 /workspace/weights/* 에 있으면 재다운로드 생략)
# ---------------------------------------------------------------------------
setup_wm2() {
    log_info "=== [setup_wm2] WorldMirror 2.0 환경 및 가중치 준비 ==="
    ensure_wm_venv
    clone_at_commit "$WM2_REPO_URL" "$WM2_REPO_DIR" "$WM2_COMMIT"
    if [[ -s "${WM2_WEIGHTS_DIR}/HY-WorldMirror-2.0/model.safetensors" && -s "${WM2_WEIGHTS_DIR}/HY-WorldMirror-2.0/config.json" ]]; then
        log_info "WorldMirror 2.0 가중치 이미 존재: ${WM2_WEIGHTS_DIR}/HY-WorldMirror-2.0"
    elif [[ -s "${WM2_WEIGHTS_DIR}/model.safetensors" && -s "${WM2_WEIGHTS_DIR}/config.json" ]]; then
        log_info "WorldMirror 2.0 가중치 이미 존재(루트): ${WM2_WEIGHTS_DIR}"
    else
        log_info "WorldMirror 2.0 가중치 다운로드: ${WM2_HF_REPO}@${WM2_HF_REV} -> ${WM2_WEIGHTS_DIR}"
        "${WM_VENV}/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='${WM2_HF_REPO}',
    revision='${WM2_HF_REV}',
    local_dir='${WM2_WEIGHTS_DIR}',
    allow_patterns=['HY-WorldMirror-2.0/*'],
)
"
    fi
    # import 스모크 점검 (모델 가중치 적재 전 모듈/shim 점검)
    PYTHONPATH="${WM2_REPO_DIR}/hyworld2/worldrecon:${WM2_REPO_DIR}:${SCRIPT_DIR}" \
        "${WM_VENV}/bin/python" -c "
import run_hybrid_depth as hd
hd._install_worldmirror_runtime_shims()
from hyworldmirror.models.models.worldmirror import WorldMirror
print('WorldMirror 2.0 import OK:', WorldMirror.__name__)
"
    log_info "[setup_wm2] 완료"
}

setup_pi3x() {
    log_info "=== [setup_pi3x] Pi3X 환경 및 가중치 준비 ==="
    ensure_base_tools
    if [[ ! -x "${PI3X_VENV}/bin/python" ]]; then
        log_info "가상환경 생성: ${PI3X_VENV}"
        uv venv "$PI3X_VENV" --python "$BASE_PY"
    fi
    log_info "Pi3X 가상환경(${PI3X_VENV}) 패키지 설치..."
    uv pip install --python "${PI3X_VENV}/bin/python" \
        --index-url https://download.pytorch.org/whl/cu124 \
        "torch==2.5.1" \
        "torchvision==0.20.1"
    uv pip install --python "${PI3X_VENV}/bin/python" \
        "numpy==1.26.4" \
        "einops==0.8.1" \
        "timm==1.0.15" \
        "pillow==11.1.0" \
        "opencv-python-headless==4.11.0.86" \
        "scipy==1.15.2" \
        "safetensors==0.5.3" \
        "huggingface_hub==0.30.2" \
        "omegaconf==2.3.0" \
        "plyfile==1.1"
    clone_at_commit "$PI3_REPO_URL" "$PI3_REPO_DIR" "$PI3_COMMIT"
    if [[ -s "${PI3_WEIGHTS_DIR}/model.safetensors" && -s "${PI3_WEIGHTS_DIR}/config.json" ]]; then
        log_info "Pi3X 가중치 이미 존재: ${PI3_WEIGHTS_DIR}"
    else
        log_info "Pi3X 가중치 다운로드: ${PI3_HF_REPO}@${PI3_HF_REV} -> ${PI3_WEIGHTS_DIR}"
        "${PI3X_VENV}/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='${PI3_HF_REPO}',
    revision='${PI3_HF_REV}',
    local_dir='${PI3_WEIGHTS_DIR}',
    allow_patterns=['*.json', '*.safetensors'],
)
"
    fi
    PYTHONPATH="${PI3_REPO_DIR}:${SCRIPT_DIR}" \
        "${PI3X_VENV}/bin/python" -c "
from pi3.models.pi3x import Pi3X
print('Pi3X import OK:', Pi3X.__name__)
"
    log_info "[setup_pi3x] 완료"
}

setup_wm1() {
    log_info "=== [setup_wm1] WorldMirror 1.0 환경 및 가중치 준비 ==="
    ensure_wm_venv
    clone_at_commit "$WM1_REPO_URL" "$WM1_REPO_DIR" "$WM1_COMMIT"
    if [[ -s "${WM1_WEIGHTS_DIR}/model.safetensors" && -s "${WM1_WEIGHTS_DIR}/config.json" ]]; then
        log_info "WorldMirror 1.0 가중치 이미 존재: ${WM1_WEIGHTS_DIR}"
    else
        log_info "WorldMirror 1.0 가중치 다운로드: ${WM1_HF_REPO}@${WM1_HF_REV} -> ${WM1_WEIGHTS_DIR}"
        "${WM_VENV}/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='${WM1_HF_REPO}',
    revision='${WM1_HF_REV}',
    local_dir='${WM1_WEIGHTS_DIR}',
    allow_patterns=['*.json', '*.safetensors'],
)
"
    fi
    PYTHONPATH="${WM1_REPO_DIR}:${WM1_REPO_DIR}/src:${SCRIPT_DIR}" \
        "${WM_VENV}/bin/python" -c "
import run_hybrid_depth as hd
hd._install_worldmirror_runtime_shims()
from src.models.models.worldmirror import WorldMirror
print('WorldMirror 1.0 import OK:', WorldMirror.__name__)
"
    log_info "[setup_wm1] 완료"
}

# ---------------------------------------------------------------------------
# 5. 모델별 Predict 실행 헬퍼 (시간·GPU 메모리 기록)
# ---------------------------------------------------------------------------
run_predict_job() {
    local job_name="$1"
    local py_bin="$2"
    local out_dir="${OUT_ROOT}/${job_name}"
    shift 2

    if [[ ! -d "$MA_RUN_DIR" || ! -d "$PREPARED_DIR" ]]; then
        log_info "오류: 입력 폴더가 없습니다 (MA_RUN_DIR=${MA_RUN_DIR}, PREPARED_DIR=${PREPARED_DIR})"
        return 2
    fi
    rm -rf "$out_dir"

    local t0 t1 elapsed rc gpu_before gpu_after
    gpu_before="$(gpu_mem_snapshot)"
    t0="$(date +%s)"
    log_info ">>> [${job_name}] 시작 (GPU: ${gpu_before}) -> ${out_dir}"

    "$py_bin" "$RUNNER_PY" predict \
        --ma-run "$MA_RUN_DIR" \
        --prepared "$PREPARED_DIR" \
        --out "$out_dir" \
        "$@"
    rc=$?

    t1="$(date +%s)"
    elapsed=$(( t1 - t0 ))
    gpu_after="$(gpu_mem_snapshot)"
    log_info "<<< [${job_name}] 종료 (rc=${rc}, elapsed=${elapsed}s, GPU=${gpu_after})"

    "$py_bin" -c "
import json, pathlib
out_dir = pathlib.Path('${out_dir}')
depth_json = out_dir / 'depth.json'
peak_mb = None
total_ms = None
ic_status = None
if depth_json.is_file():
    doc = json.loads(depth_json.read_text(encoding='utf-8'))
    peak_mb = (doc.get('adapter') or {}).get('peak_alloc_mb')
    total_ms = doc.get('total_ms')
    ic_status = ((doc.get('adapter') or {}).get('intrinsics_check') or {}).get('status')
rec = {
    'job': '${job_name}',
    'rc': int(${rc}),
    'wall_s': int(${elapsed}),
    'total_ms': total_ms,
    'peak_alloc_mb': peak_mb,
    'intrinsics_check': ic_status,
    'out_dir': str(out_dir),
}
with open('${SUMMARY_LOG}', 'a', encoding='utf-8') as f:
    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
print('SUMMARY_ROW:', json.dumps(rec, ensure_ascii=False))
" || true
    return "$rc"
}

predict_wm2() {
    SIMAI_WM2_COMMIT="$WM2_COMMIT" SIMAI_WM2_REPO="$WM2_REPO_DIR" \
        run_predict_job "wm2_pose" "${WM_VENV}/bin/python" \
        --adapter worldmirror2 \
        --model "$WM2_WEIGHTS_DIR" \
        --repo-dir "$WM2_REPO_DIR" \
        --process-res "$WM2_PROCESS_RES"
}

predict_wm2_depthprior() {
    SIMAI_WM2_COMMIT="$WM2_COMMIT" SIMAI_WM2_REPO="$WM2_REPO_DIR" \
        run_predict_job "wm2_pose_depthprior" "${WM_VENV}/bin/python" \
        --adapter worldmirror2 \
        --model "$WM2_WEIGHTS_DIR" \
        --repo-dir "$WM2_REPO_DIR" \
        --process-res "$WM2_PROCESS_RES" \
        --prior-depth
}

predict_wm2_nopose() {
    SIMAI_WM2_COMMIT="$WM2_COMMIT" SIMAI_WM2_REPO="$WM2_REPO_DIR" \
        run_predict_job "wm2_nopose" "${WM_VENV}/bin/python" \
        --adapter worldmirror2 \
        --model "$WM2_WEIGHTS_DIR" \
        --repo-dir "$WM2_REPO_DIR" \
        --process-res "$WM2_PROCESS_RES" \
        --no-pose
}

predict_wm1() {
    SIMAI_WM1_COMMIT="$WM1_COMMIT" SIMAI_WM1_REPO="$WM1_REPO_DIR" \
        run_predict_job "wm1_pose" "${WM_VENV}/bin/python" \
        --adapter worldmirror1 \
        --model "$WM1_WEIGHTS_DIR" \
        --repo-dir "$WM1_REPO_DIR" \
        --process-res "$WM1_PROCESS_RES"
}

predict_wm1_depthprior() {
    SIMAI_WM1_COMMIT="$WM1_COMMIT" SIMAI_WM1_REPO="$WM1_REPO_DIR" \
        run_predict_job "wm1_pose_depthprior" "${WM_VENV}/bin/python" \
        --adapter worldmirror1 \
        --model "$WM1_WEIGHTS_DIR" \
        --repo-dir "$WM1_REPO_DIR" \
        --process-res "$WM1_PROCESS_RES" \
        --prior-depth
}

predict_pi3x() {
    SIMAI_PI3_COMMIT="$PI3_COMMIT" SIMAI_PI3_REPO="$PI3_REPO_DIR" \
        run_predict_job "pi3x_pose" "${PI3X_VENV}/bin/python" \
        --adapter pi3x \
        --model "$PI3_WEIGHTS_DIR" \
        --repo-dir "$PI3_REPO_DIR" \
        --process-res "$PI3X_PROCESS_RES"
}

predict_pi3x_depthprior() {
    SIMAI_PI3_COMMIT="$PI3_COMMIT" SIMAI_PI3_REPO="$PI3_REPO_DIR" \
        run_predict_job "pi3x_pose_depthprior" "${PI3X_VENV}/bin/python" \
        --adapter pi3x \
        --model "$PI3_WEIGHTS_DIR" \
        --repo-dir "$PI3_REPO_DIR" \
        --process-res "$PI3X_PROCESS_RES" \
        --prior-depth
}

predict_pi3x_nopose() {
    SIMAI_PI3_COMMIT="$PI3_COMMIT" SIMAI_PI3_REPO="$PI3_REPO_DIR" \
        run_predict_job "pi3x_nopose" "${PI3X_VENV}/bin/python" \
        --adapter pi3x \
        --model "$PI3_WEIGHTS_DIR" \
        --repo-dir "$PI3_REPO_DIR" \
        --process-res "$PI3X_PROCESS_RES" \
        --no-pose
}

predict_da3() {
    if [[ ! -x "${DA3_VENV}/bin/python" ]]; then
        log_info "경고: ${DA3_VENV}/bin/python 이 없어 predict_da3 를 건너뜁니다"
        return 1
    fi
    SIMAI_DA3_COMMIT="$DA3_COMMIT" \
        run_predict_job "da3_1008" "${DA3_VENV}/bin/python" \
        --adapter da3 \
        --model "$DA3_WEIGHTS_DIR" \
        --process-res "$DA3_PROCESS_RES"
}

# ---------------------------------------------------------------------------
# 6. -528 geometry_bench/{run_bench.py,v2_clean.py,metrics.py} 기반 복셀 2 cm 비교
# ---------------------------------------------------------------------------
bench_compare() {
    log_info "=== [bench_compare] -528 geometry_bench 파이프라인(복셀 ${VOXEL_M} m = 2 cm) 후보별 메시·정리·지표 비교 ==="
    if [[ ! -d "${AI_DIR}/experiments/geometry_bench" ]]; then
        log_info "오류: ${AI_DIR}/experiments/geometry_bench 가 없습니다 (REPO_DIR=${REPO_DIR})"
        return 2
    fi
    PYTHONPATH="${AI_DIR}/src:${AI_DIR}" \
    MA_SINGLE_DIR="$MA_SINGLE_DIR" \
    OUT_ROOT="$OUT_ROOT" \
    VOXEL_M="$VOXEL_M" \
    "$BASE_PY" - <<'PYEOF'
import json
import os
import pathlib
import time
import numpy as np

from simai_ai.scene import reconstruct_tasks as rt
from simai_ai.scene import run_hybrid_depth as hd
from simai_ai.scene import texture_mesh as tm
from experiments.geometry_bench import metrics as bm
from experiments.geometry_bench import mvs_io as mio
from experiments.geometry_bench import run_bench as rb
from experiments.geometry_bench import v2_clean as vc

ma_single_root = pathlib.Path(os.environ["MA_SINGLE_DIR"])
out_root = pathlib.Path(os.environ["OUT_ROOT"])
voxel_m = float(os.environ.get("VOXEL_M", "0.02"))
target_faces = int(os.environ.get("TARGET_FACES", "550000"))
cam_scale = float(os.environ.get("CAM_SCALE", "0.5"))

bench_dir = out_root / "bench_2cm"
bench_dir.mkdir(parents=True, exist_ok=True)

calib_path = ma_single_root / "calibration.json"
images_dir = ma_single_root / "images"
if not images_dir.is_dir():
    images_dir = ma_single_root / "prepared"

cameras = mio.load_and_undistort_cameras(calib_path, images_dir)
prep = hd.load_prepared(ma_single_root / "prepared")
cams_ma, cam_sha = hd.read_cameras(ma_single_root / "mapanything")
w_align_path = ma_single_root / "world_alignment.json"
if not w_align_path.is_file():
    rt.align_to_calibration(ma_single_root, max_center_m=0.2)
M_geom, wa_rec = hd.load_world_alignment(w_align_path, cam_sha)
if M_geom is None:
    raise RuntimeError(f"world_alignment failed: {wa_rec}")

# 후보 목록: MapAnything 단일 기본선 + 생성된 깊이 폴더들
candidates = [("ma_518_single", None)]
for cand_name in (
    "da3_1008",
    "wm2_pose",
    "wm2_pose_depthprior",
    "wm2_nopose",
    "wm1_pose",
    "wm1_pose_depthprior",
    "pi3x_pose",
    "pi3x_pose_depthprior",
    "pi3x_nopose",
):
    cdir = out_root / cand_name
    if (cdir / "depth.json").is_file():
        doc = json.loads((cdir / "depth.json").read_text(encoding="utf-8"))
        if doc.get("result") == "SUCCESS":
            candidates.append((cand_name, cdir))

print(f"[bench_compare] voxel_m={voxel_m}m, candidates={[c[0] for c in candidates]}")

variant_evals = {}
variant_renders = {}
variant_loo_renders = {}

for cand_name, depth_dir in candidates:
    t0 = time.perf_counter()
    print(f">>> Evaluating candidate: {cand_name} (depth_dir={depth_dir})")
    c_out = bench_dir / cand_name
    c_out.mkdir(parents=True, exist_ok=True)

    dviews, reg_rec = tm.load_depth_views(
        ma_single_root / "mapanything",
        prep,
        cams_ma,
        depth_dir,
        cam_sha,
        "scale",
    )
    scale_m = tm.unit_scale(M_geom)
    voxel_ma = float(voxel_m) / max(scale_m, 1e-6)
    max_depth_ma = float(tm.MAX_DEPTH_M) / max(scale_m, 1e-6)
    is_hybrid = depth_dir is not None
    cfg = tm.Config(
        voxel_m=voxel_ma,
        trunc_voxels=tm.TRUNC_VOXELS,
        max_depth_m=max_depth_ma,
        min_cluster_faces=tm.MIN_CLUSTER_FACES,
        target_faces=target_faces,
        align_views="sim3" if is_hybrid else "off",
        plane_cleanup=bool(is_hybrid),
    )
    warnings_list = []
    _dv_out, _poses_out, V_ma, F_out, _plane_faces, geom_rec = tm.geometry(
        dviews, cams_ma, M_geom, cfg, warnings_list
    )
    V_world = np.asarray(hd.transform_points(M_geom, V_ma), dtype=np.float64)
    F_world = np.asarray(F_out, dtype=np.int32)
    geom_s = round(time.perf_counter() - t0, 2)

    raw_dir = c_out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_ply = raw_dir / "mesh.ply"
    rb.write_ascii_ply(raw_ply, V_world, F_world)
    tex_res = rb.run_texrecon_on_mesh(raw_dir, V_world, F_world, cameras, run_loo=True)
    raw_glb = pathlib.Path(tex_res["glb_path"])

    # v2_clean 4-stage cleanup at voxel_m=0.02
    clean_dir = c_out / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    clean_glb = clean_dir / "textured.glb"
    clean_ply = clean_dir / "mesh.ply"
    parsed_raw = bm.parse_glb_primitives_world(raw_glb)
    stage_res = vc.compute_v2_cleanup_stage_masks(parsed_raw, cameras, voxel_m=voxel_m)
    keep_final = stage_res["masks"]["d_small_components"]
    glb_filter_info = vc.filter_glb_triangles_in_place(raw_glb, clean_glb, keep_final)
    ply_filter_info = vc.filter_ply_triangles(raw_ply, clean_ply, keep_final)

    for tag, gpath, lpaths in (
        (f"{cand_name}_raw", raw_glb, tex_res["loo_glb_paths"]),
        (f"{cand_name}_clean", clean_glb, None),
    ):
        rend = bm.render_glb_views(gpath, cameras, cam_scale=cam_scale)
        self_eval = bm.evaluate_camera_renders(rend["cam_renders"], cameras)
        if lpaths is not None:
            loo_eval, loo_rgb = bm.evaluate_loo_glbs(lpaths, cameras, cam_scale=cam_scale)
        else:
            # Clean mesh LOO evaluation: filter each LOO GLB with the same keep_final or mask clean self-render
            loo_eval = {}
            loo_rgb = {}
        variant_renders[tag] = rend
        if loo_rgb:
            variant_loo_renders[tag] = loo_rgb
        variant_evals[tag] = {
            "candidate": cand_name,
            "stage": "clean" if tag.endswith("_clean") else "raw",
            "title": f"{cand_name} ({'clean' if tag.endswith('_clean') else 'raw'}, voxel={voxel_m*100:.1f}cm)",
            "glb_path": str(gpath),
            "mesh": rend["parsed_summary"],
            "self_view": self_eval,
            "loo": loo_eval,
            "registration": reg_rec,
            "geometry_record": geom_rec,
            "cleanup_filter": glb_filter_info if tag.endswith("_clean") else None,
            "stage_seconds": {"geometry_s": geom_s, "texture_s": tex_res["seconds"]},
        }

compare_dir = bench_dir / "compare"
panel_paths = bm.save_variant_comparison_panels(
    compare_dir,
    cameras,
    variant_evals,
    variant_renders,
    variant_loo_renders=variant_loo_renders,
)
summary = {
    "ticket": "S15P31A101-537",
    "voxel_m": voxel_m,
    "variants": variant_evals,
    "comparison_panels": [str(p) for p in panel_paths],
}
summary_json = bench_dir / "bench_537_summary.json"
summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"[bench_compare] wrote {summary_json}")
PYEOF
}

run_all() {
    local failures=0
    if setup_wm2; then
        predict_wm2 || ((failures++))
        predict_wm2_depthprior || ((failures++))
    else
        log_info "경고: setup_wm2 실패 -> 다음 후보로 계속 진행"
        ((failures++))
    fi

    if setup_pi3x; then
        predict_pi3x || ((failures++))
        predict_pi3x_depthprior || ((failures++))
    else
        log_info "경고: setup_pi3x 실패 -> 다음 후보로 계속 진행"
        ((failures++))
    fi

    if setup_wm1; then
        predict_wm1 || ((failures++))
        predict_wm1_depthprior || ((failures++))
    else
        log_info "경고: setup_wm1 실패 -> 다음 후보로 계속 진행"
        ((failures++))
    fi

    if [[ -x "${DA3_VENV}/bin/python" && -d "$DA3_WEIGHTS_DIR" ]]; then
        predict_da3 || ((failures++))
    fi

    bench_compare || ((failures++))

    log_info "=== 전체 실행 완료 (실패 단계 수: ${failures}, 요약 로그: ${SUMMARY_LOG}) ==="
    if [[ -f "$SUMMARY_LOG" ]]; then
        cat "$SUMMARY_LOG"
    fi
    return "$failures"
}

# ---------------------------------------------------------------------------
# 7. CLI 디스패치
# ---------------------------------------------------------------------------
steps=("${@:-all}")
overall_rc=0
for step in "${steps[@]}"; do
    case "$step" in
        setup_wm2|setup_wm1|setup_pi3x|\
        predict_wm2|predict_wm2_depthprior|predict_wm2_nopose|\
        predict_wm1|predict_wm1_depthprior|\
        predict_pi3x|predict_pi3x_depthprior|predict_pi3x_nopose|\
        predict_da3|bench_compare)
            "$step" || overall_rc=$?
            ;;
        all)
            run_all || overall_rc=$?
            ;;
        *)
            echo "알 수 없는 단계: $step" >&2
            echo "지원 단계: all setup_wm2 setup_pi3x setup_wm1 predict_wm2 predict_wm2_depthprior predict_wm2_nopose predict_pi3x predict_pi3x_depthprior predict_pi3x_nopose predict_wm1 predict_wm1_depthprior predict_da3 bench_compare" >&2
            exit 2
            ;;
    esac
done
exit "$overall_rc"
