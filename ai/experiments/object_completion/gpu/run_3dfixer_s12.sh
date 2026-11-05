#!/usr/bin/env bash
# 12차 로비 입력 번들(`s12_3dfixer_bundle.tar.gz`)로 GPU Pod(H100)에서 3D-Fixer 물체 완성을 한 번에 실행하는 스크립트.
set -euo pipefail

BUNDLE_TAR="${1:-/workspace/s12_3dfixer_bundle.tar.gz}"
WORK_DIR="${2:-/workspace/s12_3dfixer_run}"
export SIMAI_3DFIXER_REPO_DIR="${SIMAI_3DFIXER_REPO_DIR:-/workspace/3D-Fixer}"
export SIMAI_3DFIXER_MODEL_DIR="${SIMAI_3DFIXER_MODEL_DIR:-/workspace/weights/3D-Fixer}"
export SIMAI_OBJECT_BACKEND="3d_fixer"

mkdir -p "${WORK_DIR}"
if [ -f "${BUNDLE_TAR}" ]; then
    echo "[1/3] Extracting ${BUNDLE_TAR} into ${WORK_DIR}..."
    tar -xzf "${BUNDLE_TAR}" -C "${WORK_DIR}"
    BUNDLE_DIR="${WORK_DIR}/s12_3dfixer_bundle"
elif [ -d "${BUNDLE_TAR}" ]; then
    BUNDLE_DIR="${BUNDLE_TAR}"
else
    echo "Error: Bundle not found at ${BUNDLE_TAR}" >&2
    exit 1
fi

OUT_GLB="${WORK_DIR}/session12-3dfixer-completed.glb"
OUT_REPORT="${WORK_DIR}/session12-3dfixer-report.json"

echo "[2/3] Running simai_ai.scene.object_completion (--backend 3d_fixer)..."
python3 -m simai_ai.scene.object_completion \
    --in "${BUNDLE_DIR}/current-scene-textured.glb" \
    --out "${OUT_GLB}" \
    --report "${OUT_REPORT}" \
    --calibration "${BUNDLE_DIR}/calibration.json" \
    --image "cam_A=${BUNDLE_DIR}/images_undistorted/cam_A.png" \
    --image "cam_B=${BUNDLE_DIR}/images_undistorted/cam_B.png" \
    --image "cam_C=${BUNDLE_DIR}/images_undistorted/cam_C.png" \
    --image "cam_D=${BUNDLE_DIR}/images_undistorted/cam_D.png" \
    --backend 3d_fixer

echo "[3/3] Completed! Outputs:"
echo "  GLB:    ${OUT_GLB}"
echo "  Report: ${OUT_REPORT}"
