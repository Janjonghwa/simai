#!/usr/bin/env bash
# S15P31A101-515 / -514: H100 GPU Pod 일괄 실행 스크립트
# 한 번의 GPU 세션에서:
#   (a) 3D-Fixer 및 확산 인페인팅(8-bit 양자화) 의존성 설치
#   (b) 12차 번들로 물체 완성(3d_fixer) 실행 + 전후/물체별 비교 이미지 + 검증/메모리/시간 JSON 저장 (별도 프로세스)
#   (c) 같은 12차 맵에 -514 평면 인페인팅(SIMAI_GLB_INPAINT=diffusion, FLUX.1-Fill-dev 8-bit) 순차 실행 (별도 프로세스)
#       + 평면 전후 비교 이미지 + 평면·물체 통합 GLB 생성
#   (d) 모든 산출물(GLB, 비교 이미지, JSON 보고서)을 단일 .tar.gz로 묶어 회수 준비
#
# 사용법:
#   bash ai/experiments/object_completion/gpu/run_all_s12.sh [/workspace/s12_gpu_poc_input.tar.gz] [/workspace/s12_gpu_poc_run]
#   bash ai/experiments/object_completion/gpu/run_all_s12.sh --dry-run [입력번들.tar.gz] [출력폴더]
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi

BUNDLE_TAR="${1:-/workspace/s12_gpu_poc_input.tar.gz}"
WORK_DIR="${2:-/workspace/s12_gpu_poc_run}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

export THREE_D_FIXER_DIR="${THREE_D_FIXER_DIR:-/workspace/3D-Fixer}"
export THREE_D_FIXER_WEIGHTS="${THREE_D_FIXER_WEIGHTS:-/workspace/weights/3D-Fixer}"
export SIMAI_3DFIXER_REPO_DIR="${THREE_D_FIXER_DIR}"
export SIMAI_3DFIXER_MODEL_DIR="${THREE_D_FIXER_WEIGHTS}"

# -514 평면 인페인팅 기본 환경 변수 (외부에서 덮어쓰기 가능)
export SIMAI_GLB_INPAINT="${SIMAI_GLB_INPAINT:-diffusion}"
export SIMAI_INPAINT_MODEL_ID="${SIMAI_INPAINT_MODEL_ID:-black-forest-labs/FLUX.1-Fill-dev}"
export SIMAI_INPAINT_QUANTIZATION="${SIMAI_INPAINT_QUANTIZATION:-8bit}"
export SIMAI_INPAINT_STEPS="${SIMAI_INPAINT_STEPS:-20}"
export SIMAI_INPAINT_GUIDANCE="${SIMAI_INPAINT_GUIDANCE:-30.0}"
export SIMAI_INPAINT_TILE_SIZE="${SIMAI_INPAINT_TILE_SIZE:-1024}"
export SIMAI_INPAINT_TILE_OVERLAP="${SIMAI_INPAINT_TILE_OVERLAP:-128}"
export SIMAI_INPAINT_SEED="${SIMAI_INPAINT_SEED:-42}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  OBJECT_BACKEND="convex_hull"
  INPAINT_MODE="classic"
  INPAINT_QUANT="none"
  echo "[run_all_s12] DRY-RUN 모드: OBJECT_BACKEND=${OBJECT_BACKEND}, INPAINT_MODE=${INPAINT_MODE}"
else
  OBJECT_BACKEND="${SIMAI_OBJECT_BACKEND:-3d_fixer}"
  INPAINT_MODE="${SIMAI_GLB_INPAINT}"
  INPAINT_QUANT="${SIMAI_INPAINT_QUANTIZATION}"
  echo "[run_all_s12] GPU 본선 모드: OBJECT_BACKEND=${OBJECT_BACKEND}, INPAINT_MODE=${INPAINT_MODE} (${SIMAI_INPAINT_MODEL_ID}, quant=${INPAINT_QUANT})"
fi

if [[ ! -f "${BUNDLE_TAR}" ]]; then
  echo "[ERROR] 입력 번들을 찾을 수 없다: ${BUNDLE_TAR}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# (a) 3D-Fixer 및 확산 모델(8-bit 양자화) 의존성 설치
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" -eq 0 && "${SKIP_INSTALL:-0}" != "1" ]]; then
  echo "======================================================================"
  echo "[Step a] 3D-Fixer 및 FLUX.1-Fill-dev 8-bit 의존성 확인·설치"
  echo "======================================================================"
  if [[ ! -d "${THREE_D_FIXER_DIR}" || ! -d "${THREE_D_FIXER_WEIGHTS}" ]]; then
    bash "${SCRIPT_DIR}/install_3dfixer.sh"
  else
    echo "[Step a] 기존 3D-Fixer 설치 확인됨: ${THREE_D_FIXER_DIR}"
  fi
  pip install --quiet --upgrade "diffusers>=0.31.0" "transformers>=4.44.0" "accelerate>=0.33.0" bitsandbytes sentencepiece protobuf
fi

mkdir -p "${WORK_DIR}"
UNPACK_DIR="${WORK_DIR}/input_unpacked"
rm -rf "${UNPACK_DIR}"
mkdir -p "${UNPACK_DIR}"
tar -xzf "${BUNDLE_TAR}" -C "${UNPACK_DIR}"

# 압축 최상위 폴더 이름(s12_gpu_poc_input 또는 s12_3dfixer_bundle) 자동 탐색
BUNDLE_DIR="$(find "${UNPACK_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [[ -z "${BUNDLE_DIR}" ]]; then
  BUNDLE_DIR="${UNPACK_DIR}"
fi
echo "[run_all_s12] 입력 번들 경로: ${BUNDLE_DIR}"

OUT_ROOT="${WORK_DIR}/s12_gpu_poc_results"
rm -rf "${OUT_ROOT}"
mkdir -p "${OUT_ROOT}/objects" "${OUT_ROOT}/inpaint" "${OUT_ROOT}/combined"

if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  PY_BIN="${VIRTUAL_ENV}/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
else
  PY_BIN="python"
fi

export PYTHONPATH="${AI_DIR}/src:${AI_DIR}:${THREE_D_FIXER_DIR}:${PYTHONPATH:-}"
export WORK_DIR BUNDLE_DIR OUT_ROOT OBJECT_BACKEND INPAINT_MODE INPAINT_QUANT DRY_RUN

# ---------------------------------------------------------------------------
# (b) 12차 물체 완성(3d_fixer) 실행 — 독립 프로세스 #1 (종료 시 GPU VRAM 100% 반환)
# ---------------------------------------------------------------------------
echo "======================================================================"
echo "[Step b] 12차 로비 물체 완성 실행 (backend=${OBJECT_BACKEND})"
echo "======================================================================"
"${PY_BIN}" - <<'PY'
import gc
import json
import os
import time
from pathlib import Path

from simai_ai.scene import glb_enhance as ge
from simai_ai.scene import object_completion as oc
from experiments.object_completion import verify_session12_notebook as viz

bundle_dir = Path(os.environ["BUNDLE_DIR"])
out_dir = Path(os.environ["OUT_ROOT"]) / "objects"
backend_name = os.environ["OBJECT_BACKEND"]

glb_path = bundle_dir / "current-scene-baseline.glb"
if not glb_path.is_file():
    glb_path = bundle_dir / "current-scene-textured.glb"
raw_glb = glb_path.read_bytes()

calib_path = bundle_dir / "calibration.json"
calib = json.loads(calib_path.read_text(encoding="utf-8"))

raw_img_dir = bundle_dir / "images_raw"
undist_img_dir = bundle_dir / "images_undistorted"
if raw_img_dir.is_dir() and any(raw_img_dir.glob("cam_*.jpg")):
    photos = {cid: raw_img_dir / f"{cid}.jpg" for cid in ("cam_A", "cam_B", "cam_C", "cam_D")}
    cameras = ge.cameras_from_raw(calib, photos)
else:
    photos = {cid: undist_img_dir / f"{cid}.png" for cid in ("cam_A", "cam_B", "cam_C", "cam_D")}
    cameras = ge.cameras_from_raw(calib, photos)

cfg = oc.ObjectCompletionConfig.from_env(
    options={"object_backend": backend_name}
)
cfg = oc.replace(cfg, backend=backend_name, mask_backend="color_refined", texture_size=512 if backend_name == "3d_fixer" else 256)

t0 = time.monotonic()
res = oc.complete_glb_objects(raw_glb, config=cfg, cameras=cameras)
elapsed_s = round(time.monotonic() - t0, 3)

out_glb_bytes = res.glb if res.glb is not None else raw_glb
out_glb_path = out_dir / f"session12-objects-{backend_name}.glb"
out_rep_path = out_dir / f"session12-objects-{backend_name}-report.json"
out_glb_path.write_bytes(out_glb_bytes)
res.report["wall_clock_seconds"] = elapsed_s
out_rep_path.write_text(json.dumps(res.report, ensure_ascii=False, indent=2), encoding="utf-8")

# 전/후 비교 및 후보·마스크·출처 시각화 이미지 생성
g = ge.parse_glb(raw_glb)
model = oc.load_scene_for_objects(g)
merged = oc.build_merged_scene_mesh(g, model)

viz._save_candidates_bev(merged, res.candidates, res.report["candidate_summary"], out_dir / "01_candidates_bev.png")
viz._save_camera_masks_grid(cameras, res.candidates, out_dir / "02_camera_masks_grid.jpg")
viz._save_before_after_4cams(raw_glb, out_glb_bytes, cameras, out_dir / "03_before_after_4cams.jpg")
viz._save_zoom_and_provenance(
    raw_glb,
    out_glb_bytes,
    res,
    out_dir / "04_cone_and_objects_zoom.png",
    out_dir / "05_surface_source_provenance.png",
)

print(json.dumps({
    "step": "object_completion",
    "backend": backend_name,
    "applied": res.report.get("applied"),
    "candidates_found": res.report.get("candidate_summary", {}).get("candidates_found"),
    "by_category": res.report.get("candidate_summary", {}).get("by_category"),
    "accepted_count": res.report.get("accepted_count"),
    "rejected_count": res.report.get("rejected_count"),
    "gpu_max_memory_allocated_mb": res.report.get("gpu_max_memory_allocated_mb", 0.0),
    "seconds": res.report.get("seconds"),
}, ensure_ascii=False, indent=2))

gc.collect()
try:
    import torch
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
except Exception:
    pass
PY

# ---------------------------------------------------------------------------
# (c) 12차 평면 인페인팅(-514 FLUX.1-Fill-dev 8-bit) 및 통합 GLB 생성 — 독립 프로세스 #2
# ---------------------------------------------------------------------------
echo "======================================================================"
echo "[Step c] 12차 로비 평면 인페인팅 실행 (mode=${INPAINT_MODE}, quant=${INPAINT_QUANT})"
echo "======================================================================"
"${PY_BIN}" - <<'PY'
import gc
import json
import os
import shutil
import time
from pathlib import Path

import cv2
import numpy as np

from simai_ai.scene import glb_enhance as ge
from simai_ai.scene import glb_fill as gf
from simai_ai.scene import glb_inpaint as gi
from simai_ai.scene import object_completion as oc
from experiments.object_completion import verify_session12_notebook as viz

bundle_dir = Path(os.environ["BUNDLE_DIR"])
out_root = Path(os.environ["OUT_ROOT"])
inpaint_dir = out_root / "inpaint"
comb_dir = out_root / "combined"
backend_name = os.environ["OBJECT_BACKEND"]
inpaint_mode = os.environ["INPAINT_MODE"]
inpaint_quant = os.environ["INPAINT_QUANT"]

base_glb_path = bundle_dir / "current-scene-baseline.glb"
if not base_glb_path.is_file():
    base_glb_path = bundle_dir / "current-scene-textured.glb"
raw_base = viz._strip_enhanced_nodes(base_glb_path.read_bytes())

calib_path = bundle_dir / "calibration.json"
calib = json.loads(calib_path.read_text(encoding="utf-8"))
raw_img_dir = bundle_dir / "images_raw"
undist_img_dir = bundle_dir / "images_undistorted"
if raw_img_dir.is_dir() and any(raw_img_dir.glob("cam_*.jpg")):
    photos = {cid: raw_img_dir / f"{cid}.jpg" for cid in ("cam_A", "cam_B", "cam_C", "cam_D")}
else:
    photos = {cid: undist_img_dir / f"{cid}.png" for cid in ("cam_A", "cam_B", "cam_C", "cam_D")}
cameras = ge.cameras_from_raw(calib, photos)

# 1) -511 평면 채움 (inpaint=off 로 회색 바탕 및 사이드카 생성)
fill_cfg = gf.FillConfig.from_env(options={"inpaint": "off"})
sidecar_dir = inpaint_dir / "session12-plane-inpainted_fill"
if sidecar_dir.exists():
    shutil.rmtree(sidecar_dir)

t_fill0 = time.monotonic()
fill_res = gf.fill_glb_bytes(raw_base, fill_cfg, cameras=cameras, sidecar_dir=sidecar_dir)
fill_seconds = round(time.monotonic() - t_fill0, 3)
assert fill_res.glb is not None, f"Plane fill failed: {fill_res.report}"

gray_glb_path = inpaint_dir / "session12-plane-gray-before-inpaint.glb"
inpainted_glb_path = inpaint_dir / "session12-plane-inpainted.glb"
gray_glb_path.write_bytes(fill_res.glb)
inpainted_glb_path.write_bytes(fill_res.glb)

# 2) -514 평면 인페인팅 (diffusion FLUX.1-Fill-dev 8-bit 또는 dry-run classic)
inpaint_cfg = gi.InpaintConfig.from_env(
    options={
        "inpaint": inpaint_mode,
        "inpaint_quantization": inpaint_quant,
    }
)
t_inp0 = time.monotonic()
inpaint_rep = gi.inpaint_sidecar_to_glb(
    inpainted_glb_path,
    sidecar_dir,
    config=inpaint_cfg,
    fill_report=fill_res.report,
)
inpaint_wall_s = round(time.monotonic() - t_inp0, 3)
inpaint_rep["wall_clock_seconds"] = inpaint_wall_s
inpaint_rep["fill_seconds"] = fill_seconds
gi.unload_diffusion_pipeline()

(inpaint_dir / "session12-plane-inpaint-report.json").write_text(
    json.dumps(inpaint_rep, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
(inpaint_dir / "session12-plane-fill-report.json").write_text(
    json.dumps(fill_res.report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

# 3) 평면 인페인팅 전/후 비교 이미지 생성
comp_dir = inpaint_dir / "comparisons"
comp_dir.mkdir(parents=True, exist_ok=True)
for role in ("floor", "wall_0", "wall_1", "wall_2"):
    alb_p = sidecar_dir / f"{role}_albedo.png"
    msk_p = sidecar_dir / f"{role}_inpaint_mask.png"
    inp_p = sidecar_dir / f"{role}_inpainted.png"
    if alb_p.is_file() and msk_p.is_file() and inp_p.is_file():
        alb = gi.decode_png(alb_p.read_bytes())[:, :, ::-1].copy()
        msk = gi.decode_png(msk_p.read_bytes())
        inp = gi.decode_png(inp_p.read_bytes())[:, :, ::-1].copy()
        msk_vis = cv2.cvtColor(msk, cv2.COLOR_GRAY2BGR)
        h, w = alb.shape[:2]
        scale = min(1.0, 560.0 / max(h, w))
        th, tw = max(1, round(h * scale)), max(1, round(w * scale))
        p1 = cv2.resize(alb, (tw, th))
        p2 = cv2.resize(msk_vis, (tw, th))
        p3 = cv2.resize(inp, (tw, th))
        cv2.putText(p1, f"{role} BEFORE (Projected + Gray)", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (180, 220, 255), 2)
        cv2.putText(p2, f"{role} Inpaint Mask (255=Unobserved)", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (120, 220, 255), 2)
        cv2.putText(p3, f"{role} AFTER ({inpaint_rep.get('backend_used')})", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (120, 255, 150), 2)
        cv2.imwrite(str(comp_dir / f"{role}_compare.png"), np.hstack([p1, p2, p3]))

viz._save_before_after_4cams(
    gray_glb_path.read_bytes(),
    inpainted_glb_path.read_bytes(),
    cameras,
    comp_dir / "scene_4cam_inpaint_compare.jpg",
)

# 4) 평면 인페인팅 GLB 위에 Step b에서 완성한 물체 노드를 결합한 통합 GLB 생성
obj_glb_path = out_root / "objects" / f"session12-objects-{backend_name}.glb"
if obj_glb_path.is_file():
    # inpainted_glb_path 에는 estimated_floor/estimated_wall_* 노드가 들어 있으므로,
    # 그 위에 Step b와 동일한 설정으로 물체 완성을 결합하거나 이미 생성된 물체 노드를 이식한다.
    inp_bytes = inpainted_glb_path.read_bytes()
    g_inp = ge.parse_glb(inp_bytes)
    g_obj = ge.parse_glb(obj_glb_path.read_bytes())
    # g_inp의 estimated_* 노드를 g_obj에 추가해 한 장면에 평면 인페인팅 + 물체 완성을 모두 담는다.
    doc_comb = g_obj.doc
    scene_comb = doc_comb["scenes"][doc_comb.get("scene", 0)].setdefault("nodes", [])

    for node in g_inp.doc.get("nodes", []):
        if not str(node.get("name", "")).startswith("estimated_"):
            continue
        mesh_idx_src = node.get("mesh")
        if mesh_idx_src is None:
            continue
        src_mesh = g_inp.doc["meshes"][mesh_idx_src]
        new_prims = []
        for prim in src_mesh.get("primitives", []):
            new_attrs = {}
            for k_attr, acc_i in prim.get("attributes", {}).items():
                acc = dict(g_inp.doc["accessors"][acc_i])
                bv = dict(g_inp.doc["bufferViews"][acc["bufferView"]])
                raw_slice = bytes(g_inp.bin[bv["byteOffset"] : bv["byteOffset"] + bv["byteLength"]])
                new_bv = ge._add_view(g_obj, raw_slice, bv.get("target"))
                acc["bufferView"] = new_bv
                doc_comb["accessors"].append(acc)
                new_attrs[k_attr] = len(doc_comb["accessors"]) - 1
            idx_acc_i = prim["indices"]
            idx_acc = dict(g_inp.doc["accessors"][idx_acc_i])
            idx_bv = dict(g_inp.doc["bufferViews"][idx_acc["bufferView"]])
            idx_slice = bytes(g_inp.bin[idx_bv["byteOffset"] : idx_bv["byteOffset"] + idx_bv["byteLength"]])
            new_idx_bv = ge._add_view(g_obj, idx_slice, idx_bv.get("target"))
            idx_acc["bufferView"] = new_idx_bv
            doc_comb["accessors"].append(idx_acc)
            new_idx_acc = len(doc_comb["accessors"]) - 1

            new_mat_idx = prim.get("material")
            if new_mat_idx is not None:
                src_mat = dict(g_inp.doc["materials"][new_mat_idx])
                pbr = dict(src_mat.get("pbrMetallicRoughness", {}))
                if "baseColorTexture" in pbr:
                    tex_i = pbr["baseColorTexture"]["index"]
                    src_tex = g_inp.doc["textures"][tex_i]
                    src_img = dict(g_inp.doc["images"][src_tex["source"]])
                    img_bv = g_inp.doc["bufferViews"][src_img["bufferView"]]
                    img_slice = bytes(g_inp.bin[img_bv["byteOffset"] : img_bv["byteOffset"] + img_bv["byteLength"]])
                    new_img_bv = ge._add_view(g_obj, img_slice, None)
                    src_img["bufferView"] = new_img_bv
                    doc_comb.setdefault("images", []).append(src_img)
                    new_img_i = len(doc_comb["images"]) - 1
                    doc_comb.setdefault("textures", []).append({"source": new_img_i})
                    pbr["baseColorTexture"] = {"index": len(doc_comb["textures"]) - 1}
                src_mat["pbrMetallicRoughness"] = pbr
                doc_comb.setdefault("materials", []).append(src_mat)
                new_mat_idx = len(doc_comb["materials"]) - 1

            new_prims.append({
                "attributes": new_attrs,
                "indices": new_idx_acc,
                "material": new_mat_idx,
                "mode": prim.get("mode", 4),
            })
        doc_comb.setdefault("meshes", []).append({"name": src_mesh.get("name", "estimated"), "primitives": new_prims})
        new_mesh_i = len(doc_comb["meshes"]) - 1
        new_node = dict(node)
        new_node["mesh"] = new_mesh_i
        doc_comb.setdefault("nodes", []).append(new_node)
        scene_comb.append(len(doc_comb["nodes"]) - 1)

    simai_comb = doc_comb.setdefault("extras", {}).setdefault("simai", {})
    simai_inp = (g_inp.doc.get("extras") or {}).get("simai") or {}
    if "enhanced" in simai_inp:
        simai_comb["enhanced"] = simai_inp["enhanced"]
    comb_bytes = ge.serialize_glb(g_obj)
    comb_glb_path = comb_dir / "session12-combined-planes-and-objects.glb"
    comb_glb_path.write_bytes(comb_bytes)
    viz._save_before_after_4cams(raw_base, comb_bytes, cameras, comb_dir / "session12-combined-4cams.jpg")

# 5) 전체 GPU 세션 요약 보고서 작성
obj_rep_file = out_root / "objects" / f"session12-objects-{backend_name}-report.json"
obj_rep = json.loads(obj_rep_file.read_text(encoding="utf-8")) if obj_rep_file.is_file() else {}
session_summary = {
    "session": "session12-lobby",
    "sequential_execution": True,
    "step_b_object_completion": {
        "backend": backend_name,
        "applied": obj_rep.get("applied"),
        "candidates_found": (obj_rep.get("candidate_summary") or {}).get("candidates_found"),
        "by_category": (obj_rep.get("candidate_summary") or {}).get("by_category"),
        "accepted_count": obj_rep.get("accepted_count"),
        "rejected_count": obj_rep.get("rejected_count"),
        "gpu_max_memory_allocated_mb": obj_rep.get("gpu_max_memory_allocated_mb", 0.0),
        "seconds": obj_rep.get("seconds"),
        "wall_clock_seconds": obj_rep.get("wall_clock_seconds"),
    },
    "step_c_plane_inpainting": {
        "mode": inpaint_rep.get("mode"),
        "backend_used": inpaint_rep.get("backend_used"),
        "quantization": inpaint_quant,
        "applied": inpaint_rep.get("applied"),
        "gpu_max_memory_allocated_mb": inpaint_rep.get("gpu_max_memory_allocated_mb", 0.0),
        "gpu_max_memory_reserved_mb": inpaint_rep.get("gpu_max_memory_reserved_mb", 0.0),
        "fill_seconds": inpaint_rep.get("fill_seconds"),
        "inpaint_seconds": inpaint_rep.get("seconds"),
        "wall_clock_seconds": inpaint_rep.get("wall_clock_seconds"),
        "surface_source": inpaint_rep.get("surface_source"),
    },
}
(out_root / "gpu_session_summary.json").write_text(
    json.dumps(session_summary, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(json.dumps(session_summary, ensure_ascii=False, indent=2))
PY

# ---------------------------------------------------------------------------
# (d) 모든 산출물(GLB, 비교 이미지, JSON 보고서)을 단일 .tar.gz로 압축
# ---------------------------------------------------------------------------
RESULTS_TAR="${WORK_DIR}/s12_gpu_poc_results.tar.gz"
tar -czf "${RESULTS_TAR}" -C "${WORK_DIR}" "s12_gpu_poc_results"
echo "======================================================================"
echo "[Step d] 완료! 회수용 단일 압축본 생성됨:"
echo "  ${RESULTS_TAR}"
echo "======================================================================"
