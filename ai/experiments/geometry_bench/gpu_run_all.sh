#!/usr/bin/env bash
# S15P31A101-528: Single-command GPU Pod runner for Variants (0)~(4) Geometry & Texture Benchmark
#
# Dry-run check mode (on laptop or Pod before GPU run):
#   bash ai/experiments/geometry_bench/gpu_run_all.sh --check
#
# Full GPU Pod execution:
#   bash ai/experiments/geometry_bench/gpu_run_all.sh \
#     /workspace/s12_geometry_bench_input \
#     /workspace/s12_geometry_bench_out \
#     [/workspace/multiframe_stills]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_DIR="$(cd "$AI_DIR/.." && pwd)"

if [[ "${1:-}" == "--check" ]]; then
  echo "=================================================================="
  echo "[CHECK MODE] Verifying all Python entrypoints, signatures & CLIs"
  echo "=================================================================="
  cd "$AI_DIR"
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if command -v uv >/dev/null 2>&1; then
    uv run --with opencv-python-headless python -m experiments.geometry_bench.run_bench --mode check
  elif command -v uv.exe >/dev/null 2>&1; then
    uv.exe run --with opencv-python-headless python -m experiments.geometry_bench.run_bench --mode check
  elif [[ -x "$AI_DIR/.venv/Scripts/python.exe" ]]; then
    "$AI_DIR/.venv/Scripts/python.exe" -m experiments.geometry_bench.run_bench --mode check
  elif [[ -x "$AI_DIR/.venv/bin/python" ]]; then
    "$AI_DIR/.venv/bin/python" -m experiments.geometry_bench.run_bench --mode check
  else
    python -m experiments.geometry_bench.run_bench --mode check
  fi
  echo "[CHECK MODE] All entrypoints verified OK."
  exit 0
fi

INPUT_DIR="${1:-/workspace/s12_geometry_bench_input}"
OUT_DIR="${2:-/workspace/s12_geometry_bench_out}"
MULTIFRAME_DIR="${3:-$INPUT_DIR/multiframe_stills}"
DA3_VENV="${DA3_VENV:-/root/da3}"
DA3_WEIGHTS_DIR="${DA3_WEIGHTS_DIR:-/srv/simai/weights/da3nested-giant-large-1.1}"
DA3_COMMIT="3d835ec1a5802d64a8b8b15f817a1ab54809bfe4"
OPENMVS_PREFIX="${OPENMVS_PREFIX:-/opt/openmvs}"

mkdir -p "$OUT_DIR"
T_START=$(date +%s)

echo "=================================================================="
echo "[S15P31A101-528] Geometry & Texture Benchmark (Pod Runner)"
echo "  REPO_DIR       : $REPO_DIR"
echo "  INPUT_DIR      : $INPUT_DIR"
echo "  OUT_DIR        : $OUT_DIR"
echo "  MULTIFRAME_DIR : $MULTIFRAME_DIR"
echo "=================================================================="

# ------------------------------------------------------------------
# Step 0 (~60s if not cached): Install native OpenMVS (conda-forge)
# & texrecon inside the Pod container (no docker inside Pod)
# ------------------------------------------------------------------
echo "[Step 0/5] Ensuring native OpenMVS (conda-forge openmvs) & texrecon in Pod..."
if ! command -v TextureMesh >/dev/null 2>&1 && [[ ! -x "$OPENMVS_PREFIX/bin/OpenMVS/TextureMesh" ]] && [[ ! -x "$OPENMVS_PREFIX/bin/TextureMesh" ]]; then
  if command -v micromamba >/dev/null 2>&1; then
    micromamba create -y -p "$OPENMVS_PREFIX" -c conda-forge openmvs
  elif command -v mamba >/dev/null 2>&1; then
    mamba create -y -p "$OPENMVS_PREFIX" -c conda-forge openmvs
  elif command -v conda >/dev/null 2>&1; then
    conda create -y -p "$OPENMVS_PREFIX" -c conda-forge openmvs
  else
    echo "  Installing standalone micromamba to /tmp/bin/micromamba..."
    mkdir -p /tmp/bin
    curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj -C /tmp/bin --strip-components=1 bin/micromamba
    /tmp/bin/micromamba create -y -p "$OPENMVS_PREFIX" -c conda-forge openmvs
  fi
fi

if [[ -x "$OPENMVS_PREFIX/bin/OpenMVS/TextureMesh" ]]; then
  export SIMAI_OPENMVS_BIN_DIR="$OPENMVS_PREFIX/bin/OpenMVS"
  export PATH="$OPENMVS_PREFIX/bin/OpenMVS:$OPENMVS_PREFIX/bin:$PATH"
elif [[ -x "$OPENMVS_PREFIX/bin/TextureMesh" ]]; then
  export SIMAI_OPENMVS_BIN_DIR="$OPENMVS_PREFIX/bin"
  export PATH="$OPENMVS_PREFIX/bin:$PATH"
fi

if ! command -v texrecon >/dev/null 2>&1 && [[ ! -x /usr/local/bin/texrecon ]]; then
  echo "  Building native texrecon (/usr/local/bin/texrecon) from mvs-texturing..."
  apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git libpng-dev libjpeg-dev libtiff-dev libeigen3-dev libtbb-dev
  git clone https://github.com/nmoehrle/mvs-texturing.git /tmp/mvs-texturing
  cd /tmp/mvs-texturing && git checkout f3374298ac959cb5afe47a14e4d35d2ac7fbdbb1
  cmake -B /tmp/mvs-texturing/build -DCMAKE_BUILD_TYPE=Release
  cmake --build /tmp/mvs-texturing/build -j"$(nproc)"
  cp /tmp/mvs-texturing/build/apps/texrecon/texrecon /usr/local/bin/texrecon
fi
export SIMAI_TEXRECON="${SIMAI_TEXRECON:-$(command -v texrecon || echo /usr/local/bin/texrecon)}"

cd "$AI_DIR"
python -m experiments.geometry_bench.run_bench --mode check \
  --calib "$INPUT_DIR/calibration.json" \
  --bundle "$INPUT_DIR/clean_bg_bundle" \
  --out "$OUT_DIR"

# ------------------------------------------------------------------
# Step 1 (~10s): MapAnything 518 single-frame via reconstruct_tasks
# ------------------------------------------------------------------
echo "[Step 1/5] Running MapAnything 518 single-frame (reconstruct_tasks prepare + mapanything)..."
MA_SINGLE_ROOT="$OUT_DIR/ma_single"
if [[ ! -f "$MA_SINGLE_ROOT/mapanything/raw.npz" ]]; then
  python -m experiments.geometry_bench.run_bench \
    --mode setup-ma-root \
    --calib "$INPUT_DIR/calibration.json" \
    --bundle "$INPUT_DIR/clean_bg_bundle" \
    --out "$MA_SINGLE_ROOT" \
    --run-id "s12_single"
  python -m simai_ai.scene.reconstruct_tasks prepare "$MA_SINGLE_ROOT"
  python -m simai_ai.scene.reconstruct_tasks mapanything "$MA_SINGLE_ROOT"
fi
python -c "
from pathlib import Path
from simai_ai.scene import reconstruct_tasks as rt
rt.align_to_calibration(Path('$MA_SINGLE_ROOT'), max_center_m=float(__import__('os').environ.get('BENCH_ALIGN_MAX_CENTER_M', '0.2')))
"

# ------------------------------------------------------------------
# Step 2 (~45s if multi-frame stills exist; skipped for single-frame)
# ------------------------------------------------------------------
MA_MULTI_DIR="$OUT_DIR/ma_multi"
mkdir -p "$MA_MULTI_DIR"
if [[ -d "$MULTIFRAME_DIR" ]]; then
  shopt -s nullglob
  FRAME_DIRS=("$MULTIFRAME_DIR"/*/)
  shopt -u nullglob
else
  FRAME_DIRS=()
fi

if [[ ${#FRAME_DIRS[@]} -gt 0 ]]; then
  echo "[Step 2/5] Running MapAnything 518 on ${#FRAME_DIRS[@]} multi-frame background directories..."
  for fdir in "${FRAME_DIRS[@]}"; do
    fname="$(basename "$fdir")"
    froot="$MA_MULTI_DIR/$fname"
    if [[ ! -f "$froot/mapanything/raw.npz" ]]; then
      python -m experiments.geometry_bench.run_bench \
        --mode setup-ma-root \
        --calib "$INPUT_DIR/calibration.json" \
        --bundle "$fdir" \
        --out "$froot" \
        --run-id "s12_mf_${fname}"
      python -m simai_ai.scene.reconstruct_tasks prepare "$froot"
      python -m simai_ai.scene.reconstruct_tasks mapanything "$froot"
    fi
  done
else
  echo "[Step 2/5] No multi-frame directory found at $MULTIFRAME_DIR (12th session input is single-frame) -> skipping multi-frame MapAnything loop."
fi

# ------------------------------------------------------------------
# Step 3 (~22s): DA3 1008 Hybrid Depth via run_hybrid_depth.py predict
# (session4_jobs.py h0/h1 installation & weight verification pattern)
# ------------------------------------------------------------------
echo "[Step 3/6] Running Depth Anything 3 (1008px) via run_hybrid_depth.py predict (session4_jobs style)..."
DA3_OUT_DIR="$OUT_DIR/da3_1008"
if [[ ! -f "$DA3_OUT_DIR/depth.json" ]]; then
  if ! "$DA3_VENV/bin/python" -c "import torch; from depth_anything_3.api import DepthAnything3" >/dev/null 2>&1; then
    echo "  Creating DA3 virtualenv at $DA3_VENV (session4_jobs h0 pattern)..."
    rm -rf "$DA3_VENV"
    uv venv "$DA3_VENV" --python 3.11
    uv pip install --python "$DA3_VENV/bin/python" \
      torch==2.11.0 torchvision==0.26.0 numpy==2.3.5 einops huggingface_hub safetensors \
      omegaconf addict opencv-python-headless pillow imageio matplotlib scipy tqdm \
      trimesh plyfile moviepy==1.0.3 pycolmap evo
    uv pip install --python "$DA3_VENV/bin/python" --no-deps \
      "git+https://github.com/ByteDance-Seed/Depth-Anything-3@${DA3_COMMIT}"
  fi
  if [[ ! -f "$DA3_WEIGHTS_DIR/model.safetensors" ]]; then
    SIMAI_WEIGHTS_MANIFEST="$REPO_DIR/ai/experiments/static_scene/da3_weights.json" \
    SIMAI_WEIGHTS_SET="da3" \
    SIMAI_DA3_COMMIT="$DA3_COMMIT" \
    python "$REPO_DIR/deploy/scripts/simai_bootstrap.py" check
  fi
  cd "$AI_DIR"
  PYTHONPATH="$AI_DIR/src" "$DA3_VENV/bin/python" "$AI_DIR/experiments/static_scene/run_hybrid_depth.py" predict \
    --ma-run "$MA_SINGLE_ROOT/mapanything" \
    --prepared "$MA_SINGLE_ROOT/prepared" \
    --adapter da3 \
    --model "$DA3_WEIGHTS_DIR" \
    --process-res 1008 \
    --out "$DA3_OUT_DIR"
fi

# ------------------------------------------------------------------
# Step 4 (~400s): Run Variants 0..4 Benchmark & Comparison Renders
# ------------------------------------------------------------------
echo "[Step 4/6] Running Variants (0)~(4) mesh fusion, refinement, texturing, LOO & metrics..."
cd "$AI_DIR"
python -m experiments.geometry_bench.run_bench \
  --mode full \
  --variants 0,1,2,3,4 \
  --calib "$INPUT_DIR/calibration.json" \
  --bundle "$INPUT_DIR/clean_bg_bundle" \
  --ma-single-root "$MA_SINGLE_ROOT" \
  --ma-multi-dir "$MA_MULTI_DIR" \
  --da3-dir "$DA3_OUT_DIR" \
  --out "$OUT_DIR"

# ------------------------------------------------------------------
# Step 5 (~75s): MapAnything Mask Options (A/B/C/D), TSDF Surface-Area
# Loss Analysis & Per-Camera Hole Classification PNGs (-527 overlay)
# ------------------------------------------------------------------
echo "[Step 5/6] Running MapAnything Mask Options (A/B/C/D), TSDF Loss Waterfall & Camera Hole Classification..."
FINAL_GLB="$OUT_DIR/full_variants/v0_baseline/textured.glb"
if [[ ! -f "$FINAL_GLB" ]]; then
  FINAL_GLB="$INPUT_DIR/current-scene-textured.glb"
fi
python -m experiments.geometry_bench.run_bench \
  --mode hole-analysis \
  --calib "$INPUT_DIR/calibration.json" \
  --bundle "$INPUT_DIR/clean_bg_bundle" \
  --ma-single-root "$MA_SINGLE_ROOT" \
  --mesh-glb "$FINAL_GLB" \
  --run-gpu-mask-infer \
  --out "$OUT_DIR/hole_analysis"

# ------------------------------------------------------------------
# Step 6 (~10s): Package results tarball for SCP download to laptop
# ------------------------------------------------------------------
echo "[Step 6/6] Packaging results archive..."
T_END=$(date +%s)
ELAPSED=$((T_END - T_START))
tar -czf "$OUT_DIR/s12_geometry_bench_results.tar.gz" -C "$OUT_DIR" full_variants hole_analysis check_dryrun_report.json
echo "=================================================================="
echo "Done in ${ELAPSED}s!"
echo "Results archive: $OUT_DIR/s12_geometry_bench_results.tar.gz"
echo "IMPORTANT: Copy the archive to your laptop and STOP the GPU Pod immediately!"
echo "=================================================================="
