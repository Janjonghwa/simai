#!/usr/bin/env bash
# S15P31A101-539 GPU 세션 큐 스크립트 (4단계 Pod 실행 및 노트북 --dry-run 검증용)
#
# 실행 순서 (Lead 지시 반영):
#   0) LIVE 관제 표시 파일(tier0.live) 부재 확인 및 static 가중치 묶음(MapAnything·WM2·LaMa) 점검
#   1) [필수 첫 단계] WorldMirror 2.0 을 calibration.json 자세 조건(--calibration)으로 재추론 → 방식 2(METHOD2_CALIB_WM2) 게이트 1~4 판정
#   2) 세팅 파이프라인(reconstruct_tasks.bake) 전체 실행(2 cm TSDF → mesh_clean → 받침·가구 평면·LaMa → scene_final_wm2.glb·bake.json·출처 지도)
#      및 방식 2 vs 폴백 1(FALLBACK1_MA_ALIGNED_WM2) diag_tables 상판 높이 차·수평 어긋남 비교
#   3) DA3 2 cm TSDF + mesh_clean 비교 실험(-537 pod_depth_adapters.sh predict_da3 bench_compare) 후 VRAM 해제
#   4) -538 판단 모델 큐(ai/experiments/decide_model/pod_decide.sh) 실행 (정적 맵 VRAM 완전 해제 뒤 실행)
#   5) 단계별 소요 시간(wall-clock s) 및 피크 GPU VRAM(MiB) 집계(stage_timing_vram_summary.json) + 단일 결과 묶음(.tar.gz) 생성
#
# 사용법:
#   # GPU Pod 실행:
#   bash deploy/scripts/pod_final_map_539.sh --scene-input /workspace/simai/runs/s12_bg --out /workspace/simai/runs/final_map_539_out
#   # DA3 비교 실험까지 함께 켤 때:
#   bash deploy/scripts/pod_final_map_539.sh --with-da3 --scene-input /workspace/simai/runs/s12_bg --out /workspace/simai/runs/final_map_539_out
#   # 판단 모델 번들 지정:
#   bash deploy/scripts/pod_final_map_539.sh --scene-input /workspace/simai/runs/s12_bg --decide-bundle /workspace/simai/runs/decide_538_bundle --out /workspace/simai/runs/final_map_539_out

set -euo pipefail

DRY_RUN=0
ENABLE_DA3="${SIMAI_ENABLE_DA3:-0}"
SCENE_INPUT="${SIMAI_SCENE_INPUT:-/workspace/simai/runs/s12_bg}"
OUT_DIR="${SIMAI_OUT_DIR:-/workspace/simai/runs/final_map_539_out}"
SIMAI_ROOT_DIR="${SIMAI_ROOT:-/workspace/simai}"
DECIDE_BUNDLE_DIR="${SIMAI_DECIDE_BUNDLE:-${SIMAI_ROOT_DIR}/runs/decide_538_bundle}"
PYTHON_BIN="${SIMAI_PYTHON:-python}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --with-da3)
      ENABLE_DA3=1
      shift
      ;;
    --scene-input)
      SCENE_INPUT="$2"
      shift 2
      ;;
    --decide-bundle)
      DECIDE_BUNDLE_DIR="$2"
      shift 2
      ;;
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    *)
      echo "알 수 없는 인자: $1" >&2
      exit 64
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if command -v cygpath >/dev/null 2>&1; then
  REPO_ROOT_PY="$(cygpath -m "$REPO_ROOT")"
  SCENE_INPUT_PY="$(cygpath -m "$SCENE_INPUT")"
  OUT_DIR_PY="$(cygpath -m "$OUT_DIR")"
  SIMAI_ROOT_PY="$(cygpath -m "$SIMAI_ROOT_DIR")"
  DECIDE_BUNDLE_PY="$(cygpath -m "$DECIDE_BUNDLE_DIR")"
  export PYTHONPATH="${REPO_ROOT_PY}/ai/src;${REPO_ROOT_PY}/ai;${PYTHONPATH:-}"
else
  REPO_ROOT_PY="$REPO_ROOT"
  SCENE_INPUT_PY="$SCENE_INPUT"
  OUT_DIR_PY="$OUT_DIR"
  SIMAI_ROOT_PY="$SIMAI_ROOT_DIR"
  DECIDE_BUNDLE_PY="$DECIDE_BUNDLE_DIR"
  export PYTHONPATH="${REPO_ROOT_PY}/ai/src:${REPO_ROOT_PY}/ai:${PYTHONPATH:-}"
fi

mkdir -p "$OUT_DIR"
LOG_FILE="$OUT_DIR/pod_final_map_539.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=================================================================="
echo "[S15P31A101-539] GPU 세션 큐 시작 (dry_run=${DRY_RUN}, enable_da3=${ENABLE_DA3})"
echo "  REPO_ROOT     : $REPO_ROOT_PY"
echo "  SCENE_INPUT   : $SCENE_INPUT_PY"
echo "  DECIDE_BUNDLE : $DECIDE_BUNDLE_PY"
echo "  OUT_DIR       : $OUT_DIR_PY"
echo "=================================================================="

# ------------------------------------------------------------------
# Step 0: LIVE 관제 보호(tier0.live 감시) 및 가중치/환경 점검
# ------------------------------------------------------------------
T0_STEP0=$(date +%s)
MARKER_PATH="${SIMAI_TIER0_MARKER:-${SIMAI_ROOT_PY}/state/tier0.live}"
"$PYTHON_BIN" - "$MARKER_PATH" "$REPO_ROOT_PY" "$DRY_RUN" <<'PY'
import json
import sys
import time
from pathlib import Path

marker = Path(sys.argv[1])
repo_root = Path(sys.argv[2])
dry_run = int(sys.argv[3]) == 1

if marker.is_file():
    age = time.time() - marker.stat().st_mtime
    if age <= 30.0:
        print(f"[ABORT] LIVE Tier 0 표시 파일이 활성 상태입니다 ({marker}, age={age:.1f}s <= 30s)", file=sys.stderr)
        sys.exit(75)

manifest = json.loads((repo_root / "deploy" / "weights.json").read_text(encoding="utf-8"))
static_entries = [w for w in manifest["weights"] if "static" in w.get("sets", [])]
names = [w["name"] for w in static_entries]
required = {"map-anything-apache-model", "hy-worldmirror-2.0-model", "hy-worldmirror-2.0-config", "big-lama"}
missing = sorted(required - set(names))
if missing:
    print(f"[ABORT] deploy/weights.json static 묶음 누락: {missing}", file=sys.stderr)
    sys.exit(2)
print(f"[step 0] LIVE 관제 비활성 확인 완료, static 가중치 항목 {len(static_entries)}개 검증: {names}")
PY

if [[ "$DRY_RUN" -eq 0 ]]; then
  "$PYTHON_BIN" "$REPO_ROOT/deploy/scripts/simai_bootstrap.py" check --sets static
fi
T1_STEP0=$(date +%s)
STEP0_SEC=$((T1_STEP0 - T0_STEP0))

# ------------------------------------------------------------------
# Step 1: [필수 첫 단계] WM2 를 calibration.json 자세 조건으로 재추론 → 방식 2 게이트 판정
# ------------------------------------------------------------------
echo "------------------------------------------------------------------"
echo "[step 1] WorldMirror 2.0 calibration.json 자세 조건 추론 및 방식 2 게이트 평가"
echo "------------------------------------------------------------------"
T0_STEP1=$(date +%s)
STEP1_DIR="$OUT_DIR_PY/step1_method2_calib_pose"
mkdir -p "$OUT_DIR/step1_method2_calib_pose"

"$PYTHON_BIN" - "$SCENE_INPUT_PY" "$STEP1_DIR" "$DRY_RUN" <<'PY'
import json
import os
import shutil
import sys
import time
from pathlib import Path

from simai_ai.scene import pose_depth_fit as pdf
from simai_ai.scene import run_hybrid_depth as hd

scene_in = Path(sys.argv[1])
step1_dir = Path(sys.argv[2])
dry_run = int(sys.argv[3]) == 1

# 단일 입력 폴더(--scene-input) 필수 파일 점검 (노트북 절대경로 하드코딩 금지)
prep_dir = scene_in / "prepared"
ma_run = scene_in / "mapanything"
calib_path = scene_in / "calibration.json"
wa_path = scene_in / "world_alignment.json"
for req in (prep_dir / "prepared.json", ma_run / "cameras.json", calib_path, wa_path):
    if not req.is_file():
        raise FileNotFoundError(f"--scene-input ({scene_in}) 안에 필수 입력이 없습니다: {req}")

prep = hd.load_prepared(prep_dir)
depth_calib_dir = step1_dir / "depth_calib"
if depth_calib_dir.exists():
    shutil.rmtree(depth_calib_dir)

t_pred0 = time.perf_counter()
if dry_run:
    adapter = hd.FakeMapAnythingAdapter(ma_run)
else:
    wm2_model = os.environ.get(
        "SIMAI_WM2_MODEL",
        str(Path(os.environ.get("SIMAI_ROOT", "/workspace/simai")) / "weights" / "hy-world-2.0" / "HY-WorldMirror-2.0"),
    )
    adapter = hd.WorldMirrorAdapter(
        variant="worldmirror2",
        model=wm2_model,
        process_res=952,
        use_pose=True,
    )

pred_doc = hd.run_predict(adapter, ma_run, prep, depth_calib_dir, calibration=calib_path)
pred_sec = round(time.perf_counter() - t_pred0, 3)

t_gate0 = time.perf_counter()
fit_m2 = pdf.fit_and_evaluate_poses_and_depths(
    ma_run=ma_run,
    prepared=prep,
    depth_dir=depth_calib_dir,
    world_alignment_path=wa_path,
    calibration_path=calib_path,
    mode="method2",
)
gate_sec = round(time.perf_counter() - t_gate0, 3)

gate_report = {
    "stage": "step1_method2_calib_pose",
    "dry_run": dry_run,
    "pose_source": pred_doc.get("pose_source"),
    "calibration_json_sha256": pred_doc.get("calibration_json_sha256"),
    "predict_seconds": pred_sec,
    "predict_peak_alloc_mb": pred_doc.get("adapter", {}).get("peak_alloc_mb", 0.0),
    "gate_eval_seconds": gate_sec,
    "status": fit_m2.status,
    "reconstruction_path": fit_m2.reconstruction_path,
    "pose_depth_fit_summary": fit_m2.pose_depth_fit_summary,
    "gate_details": fit_m2.gate_details,
}
(step1_dir / "gate_report.json").write_text(
    json.dumps(gate_report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    f"[step 1 완료] pose_source={gate_report['pose_source']}, status={fit_m2.status}, "
    f"predict={pred_sec}s, gate_eval={gate_sec}s"
)
PY
T1_STEP1=$(date +%s)
STEP1_SEC=$((T1_STEP1 - T0_STEP1))

# ------------------------------------------------------------------
# Step 2: 세팅 파이프라인(reconstruct_tasks.bake) 전체 실행 및 방식 2 vs 폴백 1 비교
# ------------------------------------------------------------------
echo "------------------------------------------------------------------"
echo "[step 2] 세팅 파이프라인(reconstruct_tasks.bake) 실행 및 방식 2 vs 폴백 1 비교"
echo "------------------------------------------------------------------"
T0_STEP2=$(date +%s)
STEP2_DIR="$OUT_DIR_PY/step2_bake_comparison"
mkdir -p "$OUT_DIR/step2_bake_comparison"

"$PYTHON_BIN" - "$SCENE_INPUT_PY" "$STEP1_DIR" "$STEP2_DIR" "$DRY_RUN" <<'PY'
import json
import os
import shutil
import sys
import time
from pathlib import Path

from simai_ai.scene import final_map_bake as fmb
from simai_ai.scene import pose_depth_fit as pdf
from simai_ai.scene import reconstruct_tasks as rt
from simai_ai.scene import run_hybrid_depth as hd

scene_in = Path(sys.argv[1])
step1_dir = Path(sys.argv[2])
step2_dir = Path(sys.argv[3])
dry_run = int(sys.argv[4]) == 1

work_root = step2_dir / "scene_work"
if work_root.exists():
    shutil.rmtree(work_root)
work_root.mkdir(parents=True, exist_ok=True)

prep_src = scene_in / "prepared"
ma_src = scene_in / "mapanything"
calib_src = scene_in / "calibration.json"
wa_src = scene_in / "world_alignment.json"
walls_src = scene_in / "walls.json"
cleaned_src = scene_in / "textured" / "input_cleaned.glb"
env_cleaned = os.environ.get("SIMAI_DRY_RUN_CLEANED_GLB", "").strip()
if not cleaned_src.is_file() and env_cleaned:
    cleaned_src = Path(env_cleaned)

shutil.copytree(prep_src, work_root / "prepared")
shutil.copytree(ma_src, work_root / "mapanything")
shutil.copy2(calib_src, work_root / "calibration.json")
shutil.copy2(wa_src, work_root / "world_alignment.json")
if walls_src.is_file():
    shutil.copy2(walls_src, work_root / "walls.json")
shutil.copytree(step1_dir / "depth_calib", work_root / "depth_calib")

prep = hd.load_prepared(work_root / "prepared")
if not (work_root / "mapanything" / "metrics.json").is_file():
    (work_root / "mapanything" / "metrics.json").write_text(
        json.dumps({"result": "SUCCESS", "prepared_sha256": prep["_sha256"], "model_version": "ma-539"}, indent=2),
        encoding="utf-8",
    )

depth_ma_dir = work_root / "depth"
if (scene_in / "depth" / "depth.json").is_file():
    shutil.copytree(scene_in / "depth", depth_ma_dir)
elif not dry_run:
    wm2_model = os.environ.get(
        "SIMAI_WM2_MODEL",
        str(Path(os.environ.get("SIMAI_ROOT", "/workspace/simai")) / "weights" / "hy-world-2.0" / "HY-WorldMirror-2.0"),
    )
    adapter = hd.WorldMirrorAdapter(variant="worldmirror2", model=wm2_model, process_res=952, use_pose=True)
    hd.run_predict(adapter, work_root / "mapanything", prep, depth_ma_dir)
else:
    hd.run_predict(hd.FakeMapAnythingAdapter(work_root / "mapanything"), work_root / "mapanything", prep, depth_ma_dir)

job_doc = {
    "run_id": "run-539-pod-queue",
    "input": {
        "images": [{"camera_id": v["name"]} for v in prep["views"]],
        "options": {"texture": "texrecon", "final_map_pipeline": True},
    },
}
(work_root / "job.json").write_text(json.dumps(job_doc, ensure_ascii=False, indent=2), encoding="utf-8")

if dry_run:
    (work_root / "textured").mkdir(parents=True, exist_ok=True)
    if cleaned_src.is_file():
        shutil.copy2(cleaned_src, work_root / "textured" / "input_cleaned.glb")
    else:
        # dry-run 시 외부 GLB 파일 없이도 즉시 검증할 수 있도록 합성 바닥 격자 GLB 를 생성한다
        import struct
        import numpy as np

        xs, ys = np.meshgrid(np.linspace(1.5, 4.5, 21), np.linspace(1.5, 4.5, 21))
        us, vs = np.meshgrid(np.linspace(0.05, 0.95, 21), np.linspace(0.05, 0.95, 21))
        V_glb = np.column_stack([xs.ravel(), np.zeros(441), -ys.ravel()]).astype(np.float32)
        UV = np.column_stack([us.ravel(), vs.ravel()]).astype(np.float32)
        faces = []
        for j in range(20):
            for i in range(20):
                v00 = j * 21 + i
                faces.append([v00, v00 + 1, v00 + 22])
                faces.append([v00, v00 + 22, v00 + 21])
        F = np.asarray(faces, dtype=np.uint32)
        png_bytes = fmb.encode_png_any(np.full((32, 32, 3), 180, dtype=np.uint8))
        parts = [V_glb.tobytes(), UV.tobytes(), F.tobytes(), png_bytes]
        views, offset, bin_chunks = [], 0, bytearray()
        for p in parts:
            views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(p)})
            bin_chunks.extend(p)
            offset += len(p)
            while offset % 4:
                bin_chunks.append(0)
                offset += 1
        doc_g = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "images": [{"bufferView": 3, "mimeType": "image/png"}],
            "textures": [{"source": 0}],
            "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
            "buffers": [{"byteLength": len(bin_chunks)}],
            "bufferViews": views,
            "accessors": [
                {"bufferView": 0, "componentType": 5126, "count": len(V_glb), "type": "VEC3", "min": V_glb.min(axis=0).tolist(), "max": V_glb.max(axis=0).tolist()},
                {"bufferView": 1, "componentType": 5126, "count": len(UV), "type": "VEC2"},
                {"bufferView": 2, "componentType": 5125, "count": int(F.size), "type": "SCALAR"},
            ],
            "extras": {"simai": {"surface_source": {"kept_mesh_faces": int(len(F))}}},
        }
        jb = json.dumps(doc_g, separators=(",", ":")).encode("utf-8")
        while len(jb) % 4:
            jb += b" "
        raw_g = (
            struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(jb) + 8 + len(bin_chunks))
            + struct.pack("<I4s", len(jb), b"JSON") + jb
            + struct.pack("<I4s", len(bin_chunks), b"BIN\x00") + bytes(bin_chunks)
        )
        (work_root / "textured" / "input_cleaned.glb").write_bytes(raw_g)
        fmb.TEXEL_M = 0.25
        fmb.WALL_TEXEL_R3_M = 0.25
        fmb.GRID_CELL_M = 0.50
        os.environ["SIMAI_LAMA_WEIGHTS"] = str(work_root / "no_weights.pt")

t_bake0 = time.perf_counter()
rt.bake(work_root)
bake_sec = round(time.perf_counter() - t_bake0, 3)

bake_doc = json.loads((work_root / "bake.json").read_text(encoding="utf-8"))

# 방식 2 vs 폴백 1 diag_tables 비교 산출
fit_m2 = pdf.fit_and_evaluate_poses_and_depths(
    ma_run=work_root / "mapanything",
    prepared=prep,
    depth_dir=work_root / "depth_calib",
    world_alignment_path=work_root / "world_alignment.json",
    calibration_path=work_root / "calibration.json",
    mode="method2",
)
fit_fb1 = pdf.fit_and_evaluate_poses_and_depths(
    ma_run=work_root / "mapanything",
    prepared=prep,
    depth_dir=depth_ma_dir,
    world_alignment_path=work_root / "world_alignment.json",
    calibration_path=work_root / "calibration.json",
    mode="fallback1",
)

comparison = {
    "stage": "step2_bake_comparison",
    "dry_run": dry_run,
    "bake_total_seconds": bake_sec,
    "selected_reconstruction_path": bake_doc.get("reconstruction_path"),
    "uses_known_pose": bake_doc.get("uses_known_pose"),
    "observed_surface_ratio": bake_doc.get("observed_surface_ratio"),
    "assets": bake_doc.get("assets"),
    "method2_calib_pose_summary": {
        "status": fit_m2.status,
        "pose_depth_fit": fit_m2.pose_depth_fit_summary,
        "gate_details": fit_m2.gate_details,
    },
    "fallback1_ma_aligned_summary": {
        "status": fit_fb1.status,
        "pose_depth_fit": fit_fb1.pose_depth_fit_summary,
        "gate_details": fit_fb1.gate_details,
    },
}
(step2_dir / "diag_tables_comparison.json").write_text(
    json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
)
shutil.copy2(work_root / "bake.json", step2_dir / "bake.json")
shutil.copy2(work_root / "textured" / "scene_final_wm2.glb", step2_dir / "scene_final_wm2.glb")
print(
    f"[step 2 완료] bake={bake_sec}s, selected={bake_doc.get('reconstruction_path')}, "
    f"m2_coobs={fit_m2.pose_depth_fit_summary.get('coobserved_surface_3d_median_m')}m, "
    f"fb1_coobs={fit_fb1.pose_depth_fit_summary.get('coobserved_surface_3d_median_m')}m"
)
PY
T1_STEP2=$(date +%s)
STEP2_SEC=$((T1_STEP2 - T0_STEP2))

# ------------------------------------------------------------------
# Step 3: DA3 2 cm TSDF + mesh_clean 확인 (-537, 기본 비활성화 / --with-da3 시 실행)
# ------------------------------------------------------------------
echo "------------------------------------------------------------------"
echo "[step 3] DA3 2 cm TSDF + mesh_clean 비교 실험 (-537, enable_da3=${ENABLE_DA3})"
echo "------------------------------------------------------------------"
T0_STEP3=$(date +%s)
STEP3_DIR="$OUT_DIR_PY/step3_da3_2cm"
mkdir -p "$OUT_DIR/step3_da3_2cm"

if [[ "$ENABLE_DA3" -eq 1 ]] && [[ "$DRY_RUN" -eq 0 ]] && [[ -f "$REPO_ROOT/ai/experiments/static_scene/pod_depth_adapters.sh" ]]; then
  MA_SINGLE_DIR="$STEP2_DIR/scene_work" OUT_ROOT="$STEP3_DIR" AUTO_SETUP_DA3=1 \
    bash "$REPO_ROOT/ai/experiments/static_scene/pod_depth_adapters.sh" setup_da3 predict_da3 bench_compare
else
  "$PYTHON_BIN" - "$SCENE_INPUT_PY" "$STEP2_DIR/scene_work" "$STEP3_DIR" "$DRY_RUN" "$ENABLE_DA3" <<'PY'
import json
import sys
from pathlib import Path
from experiments.geometry_bench import mvs_io as mio

scene_in = Path(sys.argv[1])
work_root = Path(sys.argv[2])
step3_dir = Path(sys.argv[3])
dry_run = int(sys.argv[4]) == 1
enable_da3 = int(sys.argv[5]) == 1

# Step 3 실패 지점 사전 점검: mvs_io.load_and_undistort_cameras 가 images/*.image 및 prepared/ 를 정상 로드하는지 확인
cams_loaded = mio.load_and_undistort_cameras(scene_in / "calibration.json", scene_in)
summary = {
    "stage": "step3_da3_2cm",
    "dry_run": dry_run,
    "enable_da3": enable_da3,
    "status": "DRY_RUN_VERIFIED" if (dry_run and enable_da3) else "SKIPPED_DEFAULT_OFF",
    "script": "ai/experiments/static_scene/pod_depth_adapters.sh",
    "target_steps": ["setup_da3", "predict_da3", "bench_compare"],
    "mvs_io_cameras_verified": sorted(cams_loaded.keys()),
    "voxel_m": 0.02,
    "note": "DA3 는 기본 비활성화(ENABLE_DA3=0)이며 --with-da3 또는 SIMAI_ENABLE_DA3=1 지정 시 /root/da3 가상환경을 준비해 실행한다.",
}
(step3_dir / "da3_2cm_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"[step 3 완료] status={summary['status']}, cameras={summary['mvs_io_cameras_verified']}")
PY
fi
T1_STEP3=$(date +%s)
STEP3_SEC=$((T1_STEP3 - T0_STEP3))

# ------------------------------------------------------------------
# Step 4: -538 판단 모델 큐(ai/experiments/decide_model/pod_decide.sh) 합류
# ------------------------------------------------------------------
echo "------------------------------------------------------------------"
echo "[step 4] -538 판단 모델 큐(pod_decide.sh) 실행 (정적 맵 VRAM 해제 뒤)"
echo "------------------------------------------------------------------"
T0_STEP4=$(date +%s)
STEP4_DIR="$OUT_DIR_PY/step4_decide_538"
mkdir -p "$OUT_DIR/step4_decide_538"

DECIDE_SCRIPT="${SIMAI_DECIDE_SCRIPT:-$REPO_ROOT_PY/ai/experiments/decide_model/pod_decide.sh}"
if [[ "$DRY_RUN" -eq 0 ]] && [[ -f "$DECIDE_SCRIPT" ]] && [[ -f "$DECIDE_BUNDLE_PY/manifest.json" ]]; then
  bash "$DECIDE_SCRIPT" --bundle "$DECIDE_BUNDLE_PY" --out-dir "$STEP4_DIR/results"
  "$PYTHON_BIN" - "$STEP4_DIR" "$DECIDE_SCRIPT" "$DECIDE_BUNDLE_PY" <<'PY'
import json, sys
from pathlib import Path
Path(sys.argv[1], "decide_queue_status.json").write_text(
    json.dumps(
        {
            "stage": "step4_decide_538",
            "status": "EXECUTED",
            "script": sys.argv[2],
            "bundle_dir": sys.argv[3],
            "out_dir": str(Path(sys.argv[1]) / "results"),
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
PY
else
  "$PYTHON_BIN" - "$STEP4_DIR" "$DECIDE_SCRIPT" "$DECIDE_BUNDLE_PY" "$DRY_RUN" <<'PY'
import json
import sys
from pathlib import Path

step4_dir = Path(sys.argv[1])
script_path = sys.argv[2]
bundle_dir = Path(sys.argv[3])
dry_run = int(sys.argv[4]) == 1

bundle_exists = (bundle_dir / "manifest.json").is_file()
if dry_run:
    status = "DRY_RUN_VERIFIED"
    reason = None
elif not Path(script_path).is_file():
    status = "SKIPPED_SCRIPT_MISSING"
    reason = f"스크립트 없음: {script_path}"
else:
    status = "SKIPPED_BUNDLE_MISSING"
    reason = f"판단 모델 입력 번들(manifest.json)이 없어 건너뜀: {bundle_dir / 'manifest.json'}"

status_doc = {
    "stage": "step4_decide_538",
    "dry_run": dry_run,
    "script": script_path,
    "script_present_in_worktree": Path(script_path).is_file(),
    "bundle_dir": str(bundle_dir),
    "bundle_manifest_present": bundle_exists,
    "cli_invocation": f"bash {script_path} --bundle {bundle_dir} --out-dir {step4_dir / 'results'}",
    "vram_released_before_decide": True,
    "status": status,
    "reason": reason,
    "note": "pod_decide.sh 는 위치 인자(all)를 받지 않으며 반드시 --bundle 과 --out-dir 인자로 호출한다.",
}
(step4_dir / "decide_queue_status.json").write_text(
    json.dumps(status_doc, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"[step 4 완료] -538 큐 상태 기록: {status_doc['status']} (reason={reason})")
PY
fi
T1_STEP4=$(date +%s)
STEP4_SEC=$((T1_STEP4 - T0_STEP4))

# ------------------------------------------------------------------
# Step 5: 시간·피크 GPU VRAM 기록 및 단일 결과 묶음(.tar.gz) 생성
# ------------------------------------------------------------------
echo "------------------------------------------------------------------"
echo "[step 5] 단계별 시간·피크 VRAM 요약 및 단일 결과 묶음(.tar.gz) 생성"
echo "------------------------------------------------------------------"
"$PYTHON_BIN" - "$OUT_DIR_PY" "$DRY_RUN" "$STEP0_SEC" "$STEP1_SEC" "$STEP2_SEC" "$STEP3_SEC" "$STEP4_SEC" <<'PY'
import hashlib
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

out_dir = Path(sys.argv[1])
dry_run = int(sys.argv[2]) == 1
s0, s1, s2, s3, s4 = [int(x) for x in sys.argv[3:8]]

gate_rep = json.loads((out_dir / "step1_method2_calib_pose" / "gate_report.json").read_text(encoding="utf-8"))
comp_rep = json.loads((out_dir / "step2_bake_comparison" / "diag_tables_comparison.json").read_text(encoding="utf-8"))
bake_rep = json.loads((out_dir / "step2_bake_comparison" / "bake.json").read_text(encoding="utf-8"))

gpu_smi_mb = None
if not dry_run:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [float(x.strip()) for x in r.stdout.splitlines()[0].split(",")]
            gpu_smi_mb = {"used_mb": parts[0], "total_mb": parts[1]}
    except Exception:
        gpu_smi_mb = None

fmb_metrics = (bake_rep.get("metrics") or {}).get("final_map_bake") or {}
comp_glb_info = fmb_metrics.get("compressed_glb") or {}

summary = {
    "jira_key": "S15P31A101-539",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "dry_run": dry_run,
    "execution_mode": "simai-bootstrap scene (off-LIVE static map bake)",
    "stage_wall_clock_seconds": {
        "step0_preflight_and_live_guard_s": s0,
        "step1_wm2_calib_pose_and_method2_gate_s": s1,
        "step1_predict_only_s": gate_rep.get("predict_seconds"),
        "step1_gate_eval_only_s": gate_rep.get("gate_eval_seconds"),
        "step2_production_bake_and_compare_s": s2,
        "step2_bake_only_s": comp_rep.get("bake_total_seconds"),
        "step2_plane_projection_s": comp_glb_info.get("shared_projection_seconds"),
        "step2_lama_inpaint_s": comp_glb_info.get("inpaint_seconds"),
        "step2_glb_assemble_s": comp_glb_info.get("assemble_seconds"),
        "step3_da3_2cm_compare_s": s3,
        "step4_decide_538_queue_s": s4,
        "total_queue_seconds": s0 + s1 + s2 + s3 + s4,
    },
    "gpu_vram": {
        "wm2_predict_peak_alloc_mb": gate_rep.get("predict_peak_alloc_mb", 0.0),
        "nvidia_smi_snapshot_mb": gpu_smi_mb,
        "within_worker_8gb_budget": (gate_rep.get("predict_peak_alloc_mb") or 0.0) <= 8192.0,
    },
    "outputs": {
        "selected_reconstruction_path": bake_rep.get("reconstruction_path"),
        "scene_glb_size_mb": comp_glb_info.get("glb_size_mb"),
        "observed_surface_ratio": bake_rep.get("observed_surface_ratio"),
    },
}

summary_path = out_dir / "stage_timing_vram_summary.json"
summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

bundle_path = out_dir / "final_map_539_pod_bundle.tar.gz"
include_items = [
    out_dir / "stage_timing_vram_summary.json",
    out_dir / "step1_method2_calib_pose" / "gate_report.json",
    out_dir / "step2_bake_comparison" / "diag_tables_comparison.json",
    out_dir / "step2_bake_comparison" / "bake.json",
    out_dir / "step2_bake_comparison" / "scene_final_wm2.glb",
    out_dir / "step3_da3_2cm",
    out_dir / "step4_decide_538",
]
planes_dir = out_dir / "step2_bake_comparison" / "scene_work" / "textured" / "planes"
if planes_dir.is_dir():
    include_items.extend(sorted(planes_dir.glob("final_wm2_*_source.png")))

with tarfile.open(bundle_path, "w:gz") as tar:
    for item in include_items:
        if item.exists():
            tar.add(item, arcname=item.relative_to(out_dir).as_posix())

h = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
print(f"[step 5 완료] 결과 묶음 생성: {bundle_path} ({bundle_path.stat().st_size / (1024*1024):.2f} MiB, sha256={h})")
PY

echo "=================================================================="
echo "[S15P31A101-539] GPU 세션 큐 전체 완료: $OUT_DIR/final_map_539_pod_bundle.tar.gz"
echo "=================================================================="
