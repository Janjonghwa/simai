#!/usr/bin/env bash
# EXP-01 MapAnything 환경을 RunPod Network Volume(/workspace)에 만든다. 다시 실행해도 안전하다.
#   bash ai/experiments/static_scene/setup_runpod.sh
# 볼륨 구조(기획서 §3.12): /workspace/simai/{envs,third_party,weights,datasets,runs}
# E-GATE-0 은 Docker 이미지 대신 볼륨 venv + 잠금 파일을 쓴다(env_hash = 잠금 파일 sha256). Docker 이미지는 E-GATE-1 전에 만든다.
set -euo pipefail
ROOT=${SIMAI_ROOT:-/workspace/simai}
MA_COMMIT=${MA_COMMIT:-3d10cf7}        # 선행 PoC 와 같은 커밋
TORCH=${TORCH:-2.7.1}; TORCHVISION=${TORCHVISION:-0.22.1}; CU=${CU:-cu126}
ENV=$ROOT/envs/mapanything
REPO=$ROOT/third_party/map-anything
log(){ echo "[$(date -u +%H:%M:%S)] $*"; }

mkdir -p "$ROOT"/{envs,third_party,weights,datasets,runs,logs}

if [ ! -d "$REPO/.git" ]; then
  log "clone map-anything"
  git clone -q https://github.com/facebookresearch/map-anything "$REPO"
fi
git -C "$REPO" fetch -q --all && git -C "$REPO" checkout -q "$MA_COMMIT"
log "map-anything @ $(git -C "$REPO" rev-parse --short HEAD)"

if [ ! -x "$ENV/bin/python" ]; then
  log "create venv (python 3.12)"
  uv venv -q --python 3.12 "$ENV"
fi
PY=$ENV/bin/python
log "torch $TORCH $CU"
uv pip install -q --python "$PY" "torch==$TORCH" "torchvision==$TORCHVISION" --index-url "https://download.pytorch.org/whl/$CU"
log "map-anything (pip install -e .) + 측정용 패키지"
uv pip install -q --python "$PY" -e "$REPO" plyfile opencv-python-headless matplotlib huggingface_hub trimesh
"$PY" -c "import torch, mapanything; from mapanything.models import MapAnything; assert torch.cuda.is_available(); print('ok', torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0))"

LOCK=$ROOT/envs/mapanything.lock.txt
uv pip freeze --python "$PY" > "$LOCK"
log "lock: $LOCK sha256=$(sha256sum "$LOCK" | cut -c1-16)"

for m in map-anything map-anything-apache; do
  if [ ! -f "$ROOT/weights/$m/.done" ]; then
    log "weights facebook/$m"
    "$PY" - "$m" "$ROOT/weights/$m" <<'EOF'
import sys
from huggingface_hub import snapshot_download
snapshot_download(f"facebook/{sys.argv[1]}", local_dir=sys.argv[2])
EOF
    touch "$ROOT/weights/$m/.done"
  fi
done
log "done"
