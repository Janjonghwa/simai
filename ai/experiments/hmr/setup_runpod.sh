#!/usr/bin/env bash
# EXP-02 Fast SAM 3D Body 환경을 RunPod Network Volume(/workspace)에 만든다. 다시 실행해도 안전하다.
#   bash ai/experiments/hmr/setup_runpod.sh            # 환경만(토큰 불필요). GPU 없는 Pod 에서는 CUDA_ARCH=9.0 bash ...
#   bash ai/experiments/hmr/setup_runpod.sh weights   # 승인 가중치(facebook/sam-3d-body-dinov3). 토큰: HF_TOKEN 또는 /workspace/.simai-private/hf.env
# 공식 setup_env.sh(@d72aa36)를 따르되: conda 대신 micromamba, Detectron2 는 현재 GPU 아키텍처로 빌드(A40 8.6, H100 9.0), TensorRT·pyrealsense2 제외(E-GATE-0 범위 밖),
# setuptools<81(Detectron2 LazyConfig), torch 2.5.1+cu124 재고정(MoGe 설치가 올리지 않게) — 선행 PoC와 같은 조치.
set -euo pipefail
ROOT=${SIMAI_ROOT:-/workspace/simai}
FS_COMMIT=${FS_COMMIT:-d72aa36}
REPO=$ROOT/third_party/Fast-SAM-3D-Body
export MAMBA_ROOT_PREFIX=$ROOT/tools/micromamba
MM=$ROOT/tools/bin/micromamba
ENV=$ROOT/envs/fast_sam3db
log(){ echo "[$(date -u +%H:%M:%S)] $*"; }
mkdir -p "$ROOT"/{tools/bin,envs,third_party,weights,datasets,runs,logs}

if [ "${1:-env}" = "weights" ]; then
  # 토큰은 환경변수나 볼륨의 비공개 파일(/workspace/.simai-private/hf.env, HF_TOKEN=... 한 줄)에서 읽는다
  [ -n "${HF_TOKEN:-}" ] || { set -a; . /workspace/.simai-private/hf.env 2>/dev/null || true; set +a; }
  : "${HF_TOKEN:?HF_TOKEN 필요 (facebook/sam-3d-body-dinov3 접근 승인 계정)}"; export HF_TOKEN
  "$ENV/bin/python" - "$ROOT/weights/sam-3d-body-dinov3" <<'PY'
import os, sys
from huggingface_hub import snapshot_download
snapshot_download("facebook/sam-3d-body-dinov3", local_dir=sys.argv[1], token=os.environ["HF_TOKEN"])
PY
  # Fast 저장소 config(FP16_TYPE float16) + 같은 가중치 링크 = sam-3d-body-dinov3-fastcfg. HF 원본 폴더(bfloat16 config)는 그대로 둔다
  F=$ROOT/weights/sam-3d-body-dinov3-fastcfg; mkdir -p "$F"
  ln -sfn "$ROOT/weights/sam-3d-body-dinov3/model.ckpt" "$F/model.ckpt"
  ln -sfn "$ROOT/weights/sam-3d-body-dinov3/assets" "$F/assets"
  cp "$REPO/checkpoints/sam-3d-body-dinov3/model_config.yaml" "$F/model_config.yaml"
  cd "$ROOT/weights" && mkdir -p yolo && cd yolo && "$ENV/bin/python" -c "from ultralytics.utils.downloads import attempt_download_asset as d; d('yolo26x-pose.pt')"
  sha256sum "$ROOT/weights/sam-3d-body-dinov3/"*.ckpt "$ROOT/weights/sam-3d-body-dinov3/"*.pt "$ROOT/weights/yolo/yolo26x-pose.pt" > "$ROOT/weights/hmr_weights.sha256"
  log "weights done"; exit 0
fi

if [ ! -x "$MM" ]; then
  log "micromamba"
  curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj -C "$ROOT/tools" bin/micromamba
fi
if [ ! -d "$REPO/.git" ]; then git clone -q https://github.com/yangtiming/Fast-SAM-3D-Body "$REPO"; fi
git -C "$REPO" checkout -q "$FS_COMMIT"
log "Fast-SAM-3D-Body @ $(git -C "$REPO" rev-parse --short HEAD)"

if [ ! -x "$ENV/bin/python" ]; then
  log "env python 3.11 + cuda-toolkit 12.4"
  "$MM" create -y -p "$ENV" -c conda-forge python=3.11 >/dev/null
  "$MM" install -y -p "$ENV" -c nvidia/label/cuda-12.4.0 cuda-toolkit >/dev/null
fi
PIP="$ENV/bin/python -m pip"
TORCH="torch==2.5.1+cu124 torchvision==0.20.1+cu124 --extra-index-url https://download.pytorch.org/whl/cu124"
log "torch"; $PIP install -q $TORCH
log "python deps"
$PIP install -q pytorch-lightning pyrender opencv-python yacs scikit-image einops timm dill pandas rich hydra-core \
  hydra-submitit-launcher hydra-colorlog pyrootutils webdataset networkx==3.2.1 roma joblib seaborn appdirs ffmpeg cython \
  jsonlines pytest xtcocotools loguru optree fvcore pycocotools huggingface_hub "setuptools<81" smplx numpy scipy tqdm pyzmq plyfile trimesh \
  websockets==14.1
$PIP install -q chumpy --no-build-isolation
# GPU 가 바뀌면(A40→H100) 다시 빌드해야 한다. 빌드한 아키텍처를 기록해 두고 다르면 강제 재설치
# GPU 없는 Pod(CPU Pod 등)에서 설치할 때는 CUDA_ARCH=9.0 처럼 대상 GPU 아키텍처를 직접 준다
ARCH=${CUDA_ARCH:-$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1)}
: "${ARCH:?GPU 가 없으면 CUDA_ARCH 를 지정 (A40 8.6, H100 9.0)}"
MARK=$ENV/.detectron2_arch; FORCE=""
[ "$(cat "$MARK" 2>/dev/null)" = "$ARCH" ] || FORCE="--force-reinstall"
log "detectron2 (arch $ARCH${FORCE:+, rebuild})"
CUDA_HOME=$ENV TORCH_CUDA_ARCH_LIST="$ARCH" $PIP install -q $FORCE 'git+https://github.com/facebookresearch/detectron2.git@a1ce2f9' --no-build-isolation --no-deps
echo "$ARCH" > "$MARK"
log "ultralytics, MoGe"
$PIP install -q ultralytics "git+https://github.com/microsoft/MoGe.git@74fbce0"
$PIP install -q $TORCH   # MoGe 의존성이 torch 를 바꾸지 않게 재고정
# pyrender(EGL) 렌더: 컨테이너에는 NVIDIA EGL 벤더 라이브러리만 있고 glvnd 로더가 없다. PyOpenGL 은 무버전 libEGL.so 를 찾는다
"$MM" install -y -p "$ENV" -c conda-forge libegl libglvnd >/dev/null
ln -sfn libEGL.so.1 "$ENV/lib/libEGL.so"
"$ENV/bin/python" -c "import torch, detectron2, moge, ultralytics; print('ok', torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'GPU 없음(설치만)')"
LOCK=$ROOT/envs/fast_sam3db.lock.txt
$PIP freeze > "$LOCK"
log "lock: $LOCK sha256=$(sha256sum "$LOCK" | cut -c1-16)"
log "done"
