#!/usr/bin/env bash
# HorizonRobotics/3D-Fixer (CVPR 2026) GPU Pod(Linux, CUDA >= 11.8, VRAM >= 24 GB) 설치 스크립트.
# 노트북(Windows RTX 4050 6 GB)에서는 실행하지 않는다.
set -euo pipefail

FIXER_REPO_DIR="${SIMAI_3DFIXER_REPO_DIR:-/workspace/3D-Fixer}"
FIXER_MODEL_DIR="${SIMAI_3DFIXER_MODEL_DIR:-/workspace/weights/3D-Fixer}"
FIXER_MODEL_ID="${SIMAI_3DFIXER_MODEL_ID:-HorizonRobotics/3D-Fixer}"

echo "[1/4] Checking GPU availability and VRAM..."
python3 - <<'PY'
import torch
assert torch.cuda.is_available(), "CUDA GPU is required for 3D-Fixer"
vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
print(f"Detected GPU: {torch.cuda.get_device_name(0)} ({vram_gb:.1f} GB VRAM)")
assert vram_gb >= 20.0, f"VRAM {vram_gb:.1f} GB < 20 GB minimum requirement"
PY

echo "[2/4] Cloning HorizonRobotics/3D-Fixer into ${FIXER_REPO_DIR}..."
if [ ! -d "${FIXER_REPO_DIR}" ]; then
    git clone --recurse-submodules https://github.com/HorizonRobotics/3D-Fixer.git "${FIXER_REPO_DIR}"
fi

echo "[3/4] Running upstream setup.sh (xformers, flash-attn, kaolin, nvdiffrast, diff-gaussian-rasterization, utils3d)..."
pushd "${FIXER_REPO_DIR}" >/dev/null
bash setup.sh --basic --xformers --flash-attn --diffoctreerast --spconv --mipgaussian --kaolin --nvdiffrast
pip install -e . --no-build-isolation || true
popd >/dev/null

echo "[4/4] Downloading Hugging Face weights (${FIXER_MODEL_ID}) -> ${FIXER_MODEL_DIR}..."
python3 - <<PY
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="${FIXER_MODEL_ID}",
    local_dir="${FIXER_MODEL_DIR}",
    local_dir_use_symlinks=False,
)
print("Downloaded 3D-Fixer weights to ${FIXER_MODEL_DIR}")
PY

echo "Done! Export these before running:"
echo "  export SIMAI_3DFIXER_REPO_DIR=${FIXER_REPO_DIR}"
echo "  export SIMAI_3DFIXER_MODEL_DIR=${FIXER_MODEL_DIR}"
