"""세팅 보조 촬영 오프라인 비교(S15P31A101-605): 같은 고정 카메라 입력으로 보조 촬영 없이·넣고 맵을 만들어 비교한다.

GPU 세션에서 한 번 돌린다(워커와 같은 단계 프로세스 `ProcessStages`, 같은 시간 상한). 노트북에서는 `--reuse-base` 로 이미 있는
prepared/·mapanything/ 를 써서 MapAnything 없이 단계 연결만 볼 수 있다(보조 촬영 단계는 MapAnything 이 필요하다).

    # Pod: 이미지 안 Python 에 새 코드만 얹는다(설계 문서 8장)
    export PYTHONPATH=/workspace/simai-605/ai/src:/workspace/simai-605/ai
    python -m simai_ai.scene.supplement_runner \\
        --base /workspace/s14/setup-root \\
        --media /workspace/s14/phone/walk1.mp4 --media /workspace/s14/phone/photos/ \\
        --out /workspace/s14/supplement-cmp

입력:
- `--bundle <배경 묶음 폴더> --calibration <보정 문서>`: Pod 의 Tier 0 사람 지운 배경 묶음(SIMAI_STATE_DIR/clean-background/
  <보정 버전>/bundle-…)과 그 보정 문서로 base 를 만든다(세팅 initialize 와 같은 검사·같은 파일). `--base` 대신 쓴다.
- `--base`: static_reconstruct·site_setup 작업 폴더 레이아웃. calibration.json + images/<카메라>.image(사람 지운 배경 원본) 필수,
  job.json 은 있으면 쓰고 없으면 images/ 로 만든다. `--reuse-base` 면 base 의 prepared/·mapanything/ 를 그대로 쓴다.
- `--media`: 동영상·사진 파일 또는 폴더(여러 번). 동영상 확장자(.mp4 .mov .m4v .avi .mkv .webm .3gp)는 VIDEO, 나머지는 IMAGE.
  media_id 는 파일 이름(영문·숫자·_- 만, 나머지는 _). `--device-make/--device-model` 은 출처 기록용.

출력(`--out`): baseline/·supplement/ 작업 폴더, comparison.json(커버리지·구멍 면적·고정 카메라 재투영·중심 잔차·등록 프레임 수·
단계별 시간), run_record.json(contracts/experiments/run_record, VAL-01). 영상·GLB 는 저장소에 넣지 않는다(결과 묶음은 노트북으로).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif"}
STAGE_TIMEOUTS_S = {"prepare": 180.0, "mapanything": 1200.0, "supplement": 1200.0, "bake": 1800.0, "fallback": 120.0}
COVERAGE_CELL_M = 0.10  # 바닥 투영 격자(구멍 면적)
VOXEL_M = 0.05  # 관측 표면 부피 격자


def media_items(paths: Sequence[str]) -> list[tuple[str, str, Path]]:
    """파일·폴더 → (media_id, kind, path). 폴더는 이름 순서, 모르는 확장자는 뺀다. media_id 는 겹치지 않게 번호를 붙인다."""
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT | IMAGE_EXT))
        elif p.is_file():
            files.append(p)
        else:
            raise SystemExit(f"보조 촬영 파일 없음: {p}")
    out, seen = [], set()
    for f in files:
        base = re.sub(r"[^A-Za-z0-9_-]", "_", f.stem)[:56] or "media"
        mid, n = base, 1
        while mid in seen:
            n += 1
            mid = f"{base}-{n}"
        seen.add(mid)
        out.append((mid, "VIDEO" if f.suffix.lower() in VIDEO_EXT else "IMAGE", f))
    return out


def base_from_bundle(bundle: Path, calibration: Path, out: Path) -> Path:
    """Tier 0 배경 묶음 + 보정 문서 → base 폴더(calibration.json, images/<카메라>.image, job.json). 세팅 initialize 와 같은 파일."""
    from . import clean_bg_job as cbj

    cal = json.loads(calibration.read_text(encoding="utf-8"))
    prepared = cbj.prepare_from_bundle(bundle, cal)
    out.mkdir(parents=True, exist_ok=False)
    (out / "images").mkdir()
    for im in prepared.images:
        (out / "images" / f"{im.camera_id}.image").write_bytes(im.raw_bytes)
    (out / "calibration.json").write_text(json.dumps(cal, ensure_ascii=False, indent=1), encoding="utf-8")
    job = {"run_id": out.name, "input": {
        "calibration_version": prepared.calibration_version,
        "images": [{"camera_id": im.camera_id, "kind": im.kind} for im in prepared.images],
        "input_provenance": {"frames": {"source": "LIVE", "dataset_id": bundle.name}, "is_upper_bound": False},
        "options": {}}}
    (out / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _copy_inputs(base: Path, root: Path, *, reuse: bool) -> None:
    root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(base / "calibration.json", root / "calibration.json")
    shutil.copytree(base / "images", root / "images")
    if (base / "job.json").is_file():
        shutil.copy2(base / "job.json", root / "job.json")
    else:
        cams = sorted(p.stem for p in (base / "images").glob("*.image"))
        job = {"run_id": root.name, "input": {
            "images": [{"camera_id": c, "kind": "CLEAN_BACKGROUND"} for c in cams],
            "input_provenance": {"frames": {"source": "FILE", "dataset_id": base.name}, "is_upper_bound": False},
            "options": {}}}
        (root / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    if reuse:
        for name in ("prepared", "mapanything"):
            shutil.copytree(base / name, root / name)


def _set_options(root: Path, options: dict[str, Any]) -> None:
    job = json.loads((root / "job.json").read_text(encoding="utf-8"))
    job["input"].setdefault("options", {}).update(options)
    (root / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")


def _run(stages: Any, stage: str, root: Path, times: dict[str, float]) -> str | None:
    from .reconstruct import ReconstructionError

    t0 = time.perf_counter()
    try:
        stages.run(stage, root, timeout_s=STAGE_TIMEOUTS_S[stage], cancelled=threading.Event())
        return None
    except ReconstructionError as exc:
        return f"{exc.code}: {exc}"
    finally:
        times[stage] = round(time.perf_counter() - t0, 2)


def coverage_metrics(root: Path, bbox_xy: Sequence[float] | None = None) -> dict[str, Any]:
    """MapAnything 점(World)의 바닥 투영 커버리지·구멍 면적과 5 cm 관측 표면 부피. bbox 가 없으면 이 Run 의 1~99 백분위 XY."""
    from simai_ai.calibration.world_frame.inputs import load_mapanything_run, load_prepared

    from .run_hybrid_depth import transform_points

    run = load_mapanything_run(root / "mapanything", prepared=load_prepared(root / "prepared"))
    M = np.asarray(json.loads((root / "world_alignment.json").read_text(encoding="utf-8"))["mapanything_to_world"], float)
    pts = transform_points(M, run.all_points())
    if len(pts) == 0:
        return {"points": 0}
    if bbox_xy is None:
        lo, hi = np.percentile(pts[:, :2], 1, axis=0), np.percentile(pts[:, :2], 99, axis=0)
        bbox_xy = [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])]
    x0, y0, x1, y1 = bbox_xy
    nx, ny = max(1, int(np.ceil((x1 - x0) / COVERAGE_CELL_M))), max(1, int(np.ceil((y1 - y0) / COVERAGE_CELL_M)))
    inside = (pts[:, 0] >= x0) & (pts[:, 0] < x1) & (pts[:, 1] >= y0) & (pts[:, 1] < y1)
    ix = ((pts[inside, 0] - x0) / COVERAGE_CELL_M).astype(int).clip(0, nx - 1)
    iy = ((pts[inside, 1] - y0) / COVERAGE_CELL_M).astype(int).clip(0, ny - 1)
    occ = np.zeros((ny, nx), bool)
    occ[iy, ix] = True
    floor = inside & (np.abs(pts[:, 2]) <= 0.05)
    focc = np.zeros((ny, nx), bool)
    focc[((pts[floor, 1] - y0) / COVERAGE_CELL_M).astype(int).clip(0, ny - 1),
         ((pts[floor, 0] - x0) / COVERAGE_CELL_M).astype(int).clip(0, nx - 1)] = True
    vox = np.unique(np.floor(pts / VOXEL_M).astype(np.int64), axis=0)
    cell_area = COVERAGE_CELL_M ** 2
    return {
        "bbox_xy_m": [round(v, 3) for v in bbox_xy], "cell_m": COVERAGE_CELL_M, "points": int(len(pts)),
        "views": len(run.views),
        "xy_coverage_ratio": round(float(occ.mean()), 4), "xy_hole_area_m2": round(float((~occ).sum() * cell_area), 3),
        "floor_coverage_ratio": round(float(focc.mean()), 4),
        "floor_hole_area_m2": round(float((~focc).sum() * cell_area), 3),
        "surface_voxels_5cm": int(len(vox)),
    }


def bake_metrics(root: Path) -> dict[str, Any]:
    path = root / "bake.json"
    if not path.is_file():
        return {"baked": False}
    doc = json.loads(path.read_text(encoding="utf-8"))
    ratios = next((a.get("ratios") for a in doc.get("assets", []) if a.get("kind") == "SURFACE_SOURCE_MAP"), None)
    glb = next((a for a in doc.get("assets", []) if a.get("kind") == "GLTF_VISUAL"), None)
    return {"baked": bool(glb), "texture": doc.get("texture"), "fallback_reason": doc.get("fallback_reason"),
            "reconstruction_path": doc.get("reconstruction_path"), "observed_surface_ratio": doc.get("observed_surface_ratio"),
            "surface_ratios": ratios, "triangle_count": (glb or {}).get("triangle_count"),
            "glb_bytes": (root / glb["path"]).stat().st_size if glb and (root / glb["path"]).is_file() else None}


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                             cwd=Path(__file__).resolve().parent).stdout.strip()
    except OSError:
        out = ""
    return out if re.fullmatch(r"[0-9a-f]{7,40}", out) else "0000000"


def run_record(out: Path, cmp: dict[str, Any], args: argparse.Namespace, started_ms: int) -> dict[str, Any]:
    """contracts/experiments/run_record(VAL-01). 지표는 평평한 이름 → 수·문자열."""
    from .reconstruct import sha256

    ma_metrics = {}
    for cand in (out / "baseline" / "mapanything" / "metrics.json", out / "baseline" / "mapanything_base" / "metrics.json"):
        if cand.is_file():
            ma_metrics = json.loads(cand.read_text(encoding="utf-8"))
            break
    gpu = ma_metrics.get("gpu") or {}
    cfg_raw = json.dumps(cmp["config"], sort_keys=True).encode()
    (out / "config.json").write_bytes(cfg_raw)
    s = cmp["supplement"]["summary"] or {}
    metrics: dict[str, Any] = {"supplement_status": s.get("status"), "supplement_reason": s.get("reason"),
                               "frames_selected": s.get("frames_selected"), "frames_registered": s.get("frames_registered")}
    for k, v in (s.get("alignment") or {}).items():
        metrics[k] = v
    for variant in ("baseline", "supplement"):
        for k, v in (cmp[variant].get("coverage") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                metrics[f"{variant}_{k}"] = v
        for k, v in (cmp[variant].get("seconds") or {}).items():
            metrics[f"{variant}_seconds_{k}"] = v
    run_id = re.sub(r"[^A-Za-z0-9._-]", "_", f"val01-605-{out.name}")
    return {
        "schema_version": "2.1.0", "run_id": run_id, "experiment_id": "VAL-01", "jira_key": "S15P31A101-605",
        "owner": args.owner, "git_commit": _git_commit(), "branch": "S15P31A101-605-map-supplement-capture",
        "env_hash": os.environ.get("SIMAI_IMAGE_DIGEST") or "unknown", "model_version": ma_metrics.get("model_version") or "unknown",
        "config": {"path": "config.json", "sha256": sha256(cfg_raw)},
        "input_provenance": {"frames": {"source": "FILE", "dataset_id": Path(args.base).name}, "is_upper_bound": False},
        "gpu": {"model": gpu.get("model") or "unknown", "driver": gpu.get("driver") or "unknown",
                "cuda": gpu.get("cuda") or "unknown", "provider": args.gpu_provider},
        "started_at": started_ms, "status": "SUCCEEDED" if cmp["ok"] else "FAILED", "experiment_state": "BENCHMARK",
        "metrics": metrics, "results": [{"kind": "REPORT", "uri": f"file://{(out / 'comparison.json').as_posix()}"}],
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="고정 카메라 작업 폴더(calibration.json, images/)")
    ap.add_argument("--bundle", help="Tier 0 사람 지운 배경 묶음 폴더(--calibration 과 함께, --base 대신)")
    ap.add_argument("--calibration", help="--bundle 의 보정 문서(JSON)")
    ap.add_argument("--media", action="append", required=True, help="동영상·사진 파일 또는 폴더(여러 번)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--reuse-base", action="store_true", help="base 의 prepared/·mapanything/ 를 그대로 쓴다")
    ap.add_argument("--max-frames", type=int, default=None, help="총 프레임 상한(기본 40 = 묶음 하나)")
    ap.add_argument("--chunk-frames", type=int, default=None, help="묶음 하나의 보조 프레임 수(기본 40)")
    ap.add_argument("--time-budget-s", type=float, default=None, help="묶음 추가를 멈출 시간(기본 600)")
    ap.add_argument("--texture", choices=["texrecon", "none"], default=None, help="굽기 방식(기본: 운영 기본)")
    ap.add_argument("--skip-bake", action="store_true", help="굽지 않고 보조 촬영 단계까지만(관문·등록·커버리지)")
    ap.add_argument("--device-make")
    ap.add_argument("--device-model")
    ap.add_argument("--source", choices=["PHONE_UPLOAD", "FILE_UPLOAD"], default=None)
    ap.add_argument("--owner", default="S15P31A101-605")
    ap.add_argument("--gpu-provider", choices=["RUNPOD", "VAST", "OTHER", "LOCAL"], default="OTHER")
    args = ap.parse_args(argv)

    from . import supplement_capture as sc
    from .reconstruct_process import ProcessStages

    started_ms = time.time_ns() // 1_000_000
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} 가 비어 있지 않다(결과를 덮어쓰지 않는다)")
    out.mkdir(parents=True, exist_ok=True)
    if bool(args.base) == bool(args.bundle) or (args.bundle and not args.calibration) or (args.bundle and args.reuse_base):
        raise SystemExit("--base 또는 --bundle(+--calibration) 중 하나만 준다(--reuse-base 는 --base 에서만)")
    if args.bundle:
        args.base = str(base_from_bundle(Path(args.bundle).resolve(), Path(args.calibration).resolve(), out / "base"))
    base = Path(args.base).resolve()
    items = media_items(args.media)
    stages = ProcessStages()
    root_b, root_s = out / "baseline", out / "supplement"
    _copy_inputs(base, root_b, reuse=args.reuse_base)
    if args.texture:
        _set_options(root_b, {"texture": args.texture})
    times_b: dict[str, float] = {}
    times_s: dict[str, float] = {}
    errors: dict[str, str] = {}
    if not args.reuse_base:
        for stage in ("prepare", "mapanything"):
            err = _run(stages, stage, root_b, times_b)
            if err:
                errors[f"baseline.{stage}"] = err
                break
    if errors:
        cmp = {"ok": False, "errors": errors}
        (out / "comparison.json").write_text(json.dumps(cmp, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(cmp, ensure_ascii=False, indent=1))
        return 1
    # 보조 촬영 쪽은 같은 고정 카메라 prepare·mapanything 결과에서 시작한다(GPU 한 번)
    _copy_inputs(base, root_s, reuse=False)
    shutil.copy2(root_b / "job.json", root_s / "job.json")
    for name in ("prepared", "mapanything"):
        shutil.copytree(root_b / name, root_s / name)
    media_dir = root_s / sc.SUPPLEMENT_DIR / "media"
    media_dir.mkdir(parents=True)
    rows = []
    for mid, kind, path in items:
        dst = media_dir / f"{mid}{path.suffix.lower()}"
        shutil.copy2(path, dst)
        row: dict[str, Any] = {"media_id": mid, "kind": kind, "path": f"media/{dst.name}"}
        if args.source:
            row["source"] = args.source
        device = {k: v for k, v in (("make", args.device_make), ("model", args.device_model)) if v}
        if device:
            row["device"] = device
        rows.append(row)
    config = {k: v for k, v in (("max_frames", args.max_frames), ("chunk_frames", args.chunk_frames),
                                ("time_budget_s", args.time_budget_s)) if v is not None}
    sc.write_request(root_s, rows, config)
    err = _run(stages, "supplement", root_s, times_s)
    if err:
        errors["supplement.supplement"] = err
    result_path = root_s / sc.SUPPLEMENT_DIR / sc.RESULT_FILE
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    # 커버리지는 굽기 전에 잰다(같은 bbox: 보조 촬영 없는 Run 의 1~99 백분위 XY). 기준 정렬은 고정 카메라만
    from .reconstruct_tasks import align_to_calibration

    cov_b = cov_s = None
    try:
        align_to_calibration(root_b)
        cov_b = coverage_metrics(root_b)
        if not (root_s / "world_alignment.json").is_file():
            align_to_calibration(root_s)
        cov_s = coverage_metrics(root_s, cov_b.get("bbox_xy_m"))
    except Exception as exc:  # noqa: BLE001 - 비교 지표 실패는 기록만
        errors["coverage"] = f"{type(exc).__name__}: {exc}"
    if not args.skip_bake:
        for root, times, label in ((root_b, times_b, "baseline"), (root_s, times_s, "supplement")):
            if label == "supplement" and (result.get("summary") or {}).get("status") != "APPLIED":
                continue  # 적용되지 않았으면 기준 맵과 같다
            e = _run(stages, "bake", root, times)
            if not e and json.loads((root / "bake.json").read_text(encoding="utf-8")).get("fallback_reason") and \
                    not json.loads((root / "bake.json").read_text(encoding="utf-8")).get("assets"):
                e = _run(stages, "fallback", root, times)
            if e:
                errors[f"{label}.bake"] = e
            if label == "supplement":
                sc.annotate_bake(root, result.get("summary"))
    cmp = {
        "ok": not errors, "errors": errors, "base": str(base), "media": [{"media_id": m, "kind": k, "file": str(p)} for m, k, p in items],
        "config": {**sc.SupplementConfig.from_env(os.environ, **config).__dict__, "texture": args.texture,
                   "skip_bake": args.skip_bake, "reuse_base": args.reuse_base},
        "baseline": {"coverage": cov_b, "bake": bake_metrics(root_b), "seconds": times_b},
        "supplement": {"summary": result.get("summary"), "chunks": (result.get("detail") or {}).get("chunks"),
                       "coverage": cov_s, "bake": bake_metrics(root_s), "seconds": {**times_s, "supplement_inner": result.get("seconds")}},
    }
    if cov_b and cov_s and "xy_hole_area_m2" in cov_b and "xy_hole_area_m2" in cov_s:
        cmp["delta"] = {"xy_hole_area_m2": round(cov_s["xy_hole_area_m2"] - cov_b["xy_hole_area_m2"], 3),
                        "floor_hole_area_m2": round(cov_s["floor_hole_area_m2"] - cov_b["floor_hole_area_m2"], 3),
                        "surface_voxels_5cm": cov_s["surface_voxels_5cm"] - cov_b["surface_voxels_5cm"]}
    (out / "comparison.json").write_text(json.dumps(cmp, ensure_ascii=False, indent=1), encoding="utf-8")
    rec = run_record(out, cmp, args, started_ms)
    (out / "run_record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    s = cmp["supplement"]["summary"] or {}
    print(f"보조 촬영: {s.get('status')} {s.get('reason') or ''} 프레임 {s.get('frames_registered')}/{s.get('frames_selected')} 등록")
    print(f"정렬·재투영: {json.dumps(s.get('alignment'), ensure_ascii=False)}")
    print(f"구멍 면적 변화: {json.dumps(cmp.get('delta'), ensure_ascii=False)}")
    print(f"시간(s): 기준 {times_b} / 보조 {times_s}")
    if errors:
        print(f"오류: {json.dumps(errors, ensure_ascii=False)}", file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
