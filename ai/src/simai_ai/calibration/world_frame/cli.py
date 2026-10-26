"""MapAnything 입력 준비와 World 좌표계 고정 실행 도구 (명세 4.3 1·2단계, S15P31A101-102). `ai/` 에서:

    # 1) 노트북: 왜곡 펴기 + 펴진 K + 사람 마스크 → GPU 세션에 올릴 입력 폴더
    uv run --extra calibration --extra detection python -m simai_ai.calibration.world_frame.cli prepare \\
        --image cam_A=<사진> … --intrinsics experiments/recordings/intrinsics/intrinsics-….json \\
        --segmenter <yolo26x-seg.pt> --segmenter-sha256 sha256:… --out experiments/recordings/mapanything-inputs/<이름>
    # 2) GPU 세션: run_mapanything.py --mode prepared --prepared <폴더> (ai/experiments/static_scene/session3_jobs.py)
    # 3) 노트북: MapAnything 결과 → World 고정 → -214 의 --world-alignment 입력
    uv run --extra calibration python -m simai_ai.calibration.world_frame.cli fix --run-dir <결과 폴더> \\
        --intrinsics <같은 기록> --prepared <같은 입력 폴더> [--scale-evidence …] [--foot-points …] \\
        [--floor-points <바닥 두 점.json>] [--camera-heights <실측 카메라 높이.json>]

- 축 우선순위(-277): 선택 입력 `--floor-points`(설치자가 카메라 하나의 원본 그림에서 찍은 바닥 두 점: 원점, +X 방향 점,
  MANUAL_FLOOR_POINTS, `--prepared` 실행에서만) > 수직 경계 벽 두 방향(WALLS) > 경계 벽 한 방향 + 바닥(WALL_AND_FLOOR, 원점은
  벽·바닥 교선 위 카메라 중심 평균의 수선 발) > 실패. 선택 입력 `--camera-heights`(줄자로 잰 바닥 → 렌즈 중심 높이, m)는
  스케일 근거 MEASURED_CAMERA_HEIGHT 이고, 없으면 기존 근거(없으면 MODEL_ESTIMATE)다.
- `fix` 결과(`simai.world_alignment`)는 status OK 일 때만 `mapanything_to_world`(4×4 [sR | T])와 PROVISIONAL 보정 문서
  (`calibration`, 계약 calibration 2.1.0, `world_axes_source` 포함)를 싣는다. 실패(바닥 없음·축 모호·발 접지점 불일치·
  바닥 두 점 무효)면 사유만 싣고 변환은 비운다.
- 출처: 영상·내부 파라미터 기록·스케일 근거·발 접지점의 출처를 -214 `combine_input_provenance` 로 합친다.
  input_provenance.calibration 은 MODEL_ESTIMATE(`mapanything-<run_id>`, 카메라를 모델이 추정)이고 입력 중 하나라도
  SIM·DATASET_GT 면 그 출처(결과 전체 상한)다. provider_source 는 G1_AUTO, 상한이면 그 출처.
- 연결 확인: 입력 폴더를 만든 내부 파라미터 기록 sha256 과 MapAnything metrics 의 prepared_sha256 이 이번 입력과 다르면 거절.
  prepared 모드 실행인데 --prepared 가 없거나, --prepared 를 줬는데 그 폴더로 돈 기록이 없어도(images 모드) 거절.
- 결과는 언제나 PROVISIONAL 이다(독립 검증점이 없다, 4.4).
- 기본 출력: `ai/experiments/recordings/world-frame/world-frame-<UTC>.json`(.gitignore). 있으면 덮어쓰지 않는다.
- 종료 코드: 0 고정함, 2 인자·입력 오류, 3 고정 못 함(결과 파일에 사유).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

AI_DIR = Path(__file__).resolve().parents[4]
DEFAULT_OUT_DIR = AI_DIR / "experiments" / "recordings" / "world-frame"
RESULT_FORMAT = "simai.world_alignment"  # -214 calibration.extrinsics.initial.WORLD_ALIGNMENT_FORMAT
RESULT_VERSION = "0.2.0"  # 0.2.0: axes_source·origin_source, inputs.floor_points·camera_heights (-277)
PROVIDER_VERSION = "world-frame-0.2.0"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_NOT_FIXED = 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m simai_ai.calibration.world_frame.cli", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("prepare", help="사진 → 왜곡 편 영상·펴진 K·사람 마스크 (MapAnything known-K 입력)")
    pr.add_argument("--image", action="append", required=True, metavar="CAM=PATH", help="카메라마다 한 장")
    pr.add_argument("--intrinsics", type=Path, required=True, help="내부 파라미터 기록(simai.intrinsics_estimate)")
    pr.add_argument("--out", type=Path, required=True, help="새 출력 폴더(커밋하지 않는 위치)")
    pr.add_argument("--segmenter", help="사람 분할 가중치(YOLO seg .pt)")
    pr.add_argument("--segmenter-sha256", help="sha256:<hex64>")
    pr.add_argument("--no-person-mask", action="store_true", help="마스크를 만들지 않는다(기록에 남는다)")
    pr.add_argument("--device", default=None)
    pr.add_argument("--dilate-px", type=int, default=15)
    pr.add_argument("--frames-source", choices=["FILE", "LIVE", "SIM", "DATASET"], default="FILE")
    pr.add_argument("--dataset-id", help="예: classroom-stills-20261021")

    fx = sub.add_parser("fix", help="MapAnything 결과 → World 고정 → PROVISIONAL 보정과 -214 World 정렬 입력")
    fx.add_argument("--run-dir", type=Path, required=True, help="run_mapanything 결과 폴더(cameras.json, raw.npz)")
    fx.add_argument("--intrinsics", type=Path, required=True, help="보정 문서 K·왜곡(펴기에 쓴 같은 기록)")
    fx.add_argument("--view", action="append", default=[], metavar="CAM=VIEW")
    fx.add_argument("--prepared", type=Path, help="prepare 출력 폴더(사람 마스크·입력 출처). 없으면 마스크 없이(경고)")
    fx.add_argument("--scale-evidence", type=Path, help='{"estimates": [{"basis","scale","sample_count","label"}]}')
    fx.add_argument("--foot-points", type=Path, help='{"format": "simai.foot_contacts", "points_mapanything": [...]}')
    fx.add_argument("--floor-points", type=Path,
                    help='바닥 두 점 {"format": "simai.floor_points", "camera_id", "pixel_space": "ORIGINAL"|"PREPARED", '
                         '"origin_px": [u, v], "x_axis_px": [u, v]} (축 출처 MANUAL_FLOOR_POINTS)')
    fx.add_argument("--camera-heights", type=Path,
                    help='실측 카메라 높이(m, 바닥 → 렌즈 중심) {"format": "simai.camera_heights", '
                         '"heights_m": {"cam_A": 2.71, …}} 또는 {"cam_A": 2.71, …}')
    fx.add_argument("--frames-source", choices=["FILE", "LIVE", "SIM", "DATASET"], help="prepared 가 없을 때 입력 영상 출처")
    fx.add_argument("--dataset-id")
    fx.add_argument("--site-id", default=None)
    fx.add_argument("--calibration-version", help="기본 cal-wf-<UTC>")
    fx.add_argument("--out", type=Path, help=f"결과 JSON (기본 {DEFAULT_OUT_DIR}/world-frame-<UTC>.json)")
    return p


def main(argv: Sequence[str] | None = None, *, now_ms: int | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    created_at = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    try:
        if args.command == "prepare":
            return _prepare(args)
        return _fix(args, created_at)
    except (ValueError, KeyError, FileNotFoundError, FileExistsError, ImportError) as e:
        print(f"입력 오류: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_CONFIG


def _frames(source: str, dataset_id: str | None, camera_ids: Sequence[str]) -> dict[str, Any]:
    frames: dict[str, Any] = {"source": source, "camera_ids": sorted(camera_ids)}
    if dataset_id:
        frames["dataset_id"] = dataset_id
    return frames


def _prepare(args: argparse.Namespace) -> int:
    from simai_ai.calibration.intrinsics.estimate import WeightsRef
    from simai_ai.calibration.world_frame.prepare import UltralyticsPersonSegmenter, prepare_inputs
    from simai_ai.providers.provenance import build_input_provenance

    images = dict(_pair(v, "--image") for v in args.image)
    if args.no_person_mask:
        segmenter = None
    else:
        if not (args.segmenter and args.segmenter_sha256):
            raise ValueError("사람 마스크에는 --segmenter 와 --segmenter-sha256 이 필요하다(없이 하려면 --no-person-mask)")
        segmenter = UltralyticsPersonSegmenter(WeightsRef(args.segmenter, args.segmenter_sha256), device=args.device)
    raw = args.intrinsics.read_bytes()
    doc = prepare_inputs(
        {k: Path(v) for k, v in images.items()},
        json.loads(raw.decode("utf-8")),
        args.out,
        segmenter=segmenter,
        input_provenance=build_input_provenance(_frames(args.frames_source, args.dataset_id, images)),
        dilate_px=args.dilate_px,
        intrinsics_ref={"path": args.intrinsics.name, "sha256": _sha256(raw)},
    )
    for v in doc["views"]:
        print(f"{v['camera_id']}: K' fx={v['K_undistorted'][0][0]:.1f} 사람 {v.get('person_count', '-')}명 "
              f"마스크 {v.get('person_mask_ratio', '-')}")
    print(f"입력 폴더: {args.out}")
    return EXIT_OK


def _fix(args: argparse.Namespace, created_at: int) -> int:
    import numpy as np

    from simai_ai.calibration.criteria import CalibrationJudgement, ScaleBasis, ScaleDecision, ScaleEstimate
    from simai_ai.calibration.document import CameraCalibration, build_calibration_doc
    from simai_ai.calibration.extrinsics.initial import (
        WorldAlignment,
        cam2world_to_extrinsics,
        checked_provenance,
        combine_input_provenance,
    )
    from simai_ai.calibration.intrinsics.record import bundle_init_from_record
    from simai_ai.calibration.world_frame.frame import STATUS_FAILED, STATUS_OK, WorldFrameConfig, fix_world_frame
    from simai_ai.calibration.world_frame.inputs import (
        cross_view_reprojection,
        floor_point_rays,
        load_camera_heights,
        load_floor_points,
        load_foot_points,
        load_mapanything_run,
        load_prepared,
    )
    from simai_ai.providers.contract import check_calibration, check_input_provenance
    from simai_ai.providers.provenance import build_input_provenance
    from simai_ai.tracking.system_health import CalibrationState

    stamp = _utc_stamp(created_at)
    out = args.out or DEFAULT_OUT_DIR / f"world-frame-{stamp}.json"
    if out.exists():
        raise ValueError(f"결과 파일이 이미 있다(덮어쓰지 않는다): {out}")
    intr_raw = args.intrinsics.read_bytes()
    intr_doc = json.loads(intr_raw.decode("utf-8"))
    intrinsics = bundle_init_from_record(intr_doc)
    prepared = load_prepared(args.prepared) if args.prepared else None
    if prepared and (prepared.get("intrinsics") or {}).get("sha256") not in (None, _sha256(intr_raw)):
        raise ValueError("입력 폴더를 만든(왜곡을 편) 내부 파라미터 기록과 --intrinsics 가 다르다")
    view_map = dict(_pair(v, "--view") for v in args.view) or None
    cfg = WorldFrameConfig()
    run = load_mapanything_run(args.run_dir, view_map=view_map, prepared=prepared, conf_percentile=cfg.conf_percentile)
    camera_ids = [v.camera_id for v in run.views]
    unknown = sorted(set(camera_ids) - {c.camera_id for c in intrinsics.cameras})
    if unknown:
        raise ValueError(f"내부 파라미터 기록에 없는 카메라: {unknown}")
    # 입력 폴더와 MapAnything 실행을 묶는다: prepared 모드 실행이면 같은 --prepared 가 있어야 하고(없으면 마스크·출처·
    # 내부 파라미터 확인이 빠진다), --prepared 를 주면 그 실행이 이 prepared.json 으로 돈 기록(metrics.json)이 있어야 한다
    ma = run.record.get("mapanything")
    used = (ma or {}).get("prepared_sha256")
    if not prepared and (used or (ma or {}).get("mode") == "prepared"):
        raise ValueError("MapAnything 이 prepared 모드로 돌았다. 그 입력 폴더를 --prepared 로 준다")
    if prepared and used != prepared["_sha256"]:
        raise ValueError(
            "MapAnything 이 다른 입력 폴더(prepared.json)로 돌았다" if used else
            "MapAnything 실행 기록(metrics.json)에 이 입력 폴더로 돈 근거(prepared_sha256)가 없다(images 모드 등)"
        )

    if prepared:
        base = checked_provenance(prepared.get("input_provenance"), "prepared.json")
        if base is None:
            raise ValueError("입력 폴더(prepared.json)에 input_provenance 가 없다")
        if args.frames_source and args.frames_source != base["frames"]["source"]:
            raise ValueError(f"--frames-source {args.frames_source} 가 입력 폴더 출처 {base['frames']['source']} 와 다르다")
    else:
        if not args.frames_source:
            raise ValueError("--prepared 가 없으면 --frames-source 로 입력 영상 출처를 적는다")
        base = build_input_provenance(_frames(args.frames_source, args.dataset_id, camera_ids))
    run_id = (run.record.get("cameras_json") or {}).get("run_id") or run.record["raw_npz_sha256"][7:19]

    estimates, scale_input = (), None
    if args.scale_evidence:
        raw = args.scale_evidence.read_bytes()
        sdoc = json.loads(raw.decode("utf-8"))
        estimates = tuple(
            ScaleEstimate(ScaleBasis(e["basis"]), float(e["scale"]), int(e.get("sample_count", 1)), e.get("label"))
            for e in sdoc.get("estimates", [])
        )
        scale_input = {"path": args.scale_evidence.name, "sha256": _sha256(raw),
                       "input_provenance": checked_provenance(sdoc.get("input_provenance"), args.scale_evidence.name)}
    feet, feet_input = None, None
    if args.foot_points:
        feet, feet_input = load_foot_points(args.foot_points)
        feet_input["input_provenance"] = checked_provenance(feet_input["input_provenance"], args.foot_points.name)
    rays, points_input = None, None
    if args.floor_points:
        if not prepared:
            raise ValueError("바닥 두 점(--floor-points)은 known-K 입력 폴더(--prepared)로 돈 실행에서만 받는다"
                             "(픽셀 → 광선 규약)")
        spec, points_input = load_floor_points(args.floor_points)
        points_input["input_provenance"] = checked_provenance(points_input["input_provenance"], args.floor_points.name)
        if spec["camera_id"] not in camera_ids:
            raise ValueError(f"바닥 두 점의 카메라 {spec['camera_id']!r} 가 MapAnything 결과에 없다({camera_ids})")
        cam = intrinsics.camera(spec["camera_id"]).camera
        rays = floor_point_rays(run, prepared, spec, K=np.asarray(cam["K"], dtype=float),
                                dist=np.asarray(cam["distortion"]["coeffs"], dtype=float))
    heights, heights_input = None, None
    if args.camera_heights:
        heights, heights_input = load_camera_heights(args.camera_heights)
        heights_input["input_provenance"] = checked_provenance(heights_input["input_provenance"], args.camera_heights.name)
        unknown_h = sorted(set(heights) - set(camera_ids))
        if unknown_h:
            raise ValueError(f"카메라 높이에 MapAnything 결과에 없는 카메라가 있다: {unknown_h} (결과 카메라 {camera_ids})")
    # 모든 입력(영상·내부 파라미터·스케일·발)의 출처를 -214 와 같은 규칙으로 합친다: 하나라도 SIM·DATASET_GT 면
    # calibration 출처가 그 값이 되어 결과 전체가 상한이고(is_upper_bound 가 출처와 맞음) 보정 문서도 그 출처다
    provenance, sources = combine_input_provenance(
        base,
        {
            "intrinsics": checked_provenance(intr_doc.get("input_provenance"), args.intrinsics.name),
            "scale_evidence": scale_input and scale_input["input_provenance"],
            "foot_points": feet_input and feet_input["input_provenance"],
            "floor_points": points_input and points_input["input_provenance"],
            "camera_heights": heights_input and heights_input["input_provenance"],
        },
        f"mapanything-{run_id}",
    )
    problems = check_input_provenance(provenance)
    if problems:  # pragma: no cover - 합치는 규칙이 출처와 상한 표시를 함께 정한다
        raise ValueError(f"합친 input_provenance 가 계약에 맞지 않다: {problems}")
    # combine_input_provenance 는 첫 입력을 keypoints 로 부른다. 여기서는 영상(입력 폴더) 출처다
    upper = {("frames" if k == "keypoints" else k): v for k, v in sources["upper_bound_inputs"].items()}

    result = fix_world_frame(run.all_points(), run.cam2world(), scale_estimates=estimates, foot_points=feet,
                             floor_points=rays, camera_heights=heights, config=cfg)
    widths = {c.camera_id: int(c.camera["image_size"][0]) for c in intrinsics.cameras}
    record: dict[str, Any] = {
        "format": RESULT_FORMAT,
        "format_version": RESULT_VERSION,
        "status": result.status,
        "reason": result.reason,
        "detail": result.detail,
        "axes_source": result.axes_source,
        "origin_source": (result.evidence.get("origin") or {}).get("source") if result.status == STATUS_OK else None,
        "created_at": created_at,
        "input_provenance": provenance,
        "cameras_json_sha256": run.record["cameras_json"]["sha256"],
        "upper_bound_inputs": dict(upper),
        "inputs": {
            "mapanything": run.record,
            "intrinsics": {"path": args.intrinsics.name, "sha256": _sha256(intr_raw)},
            "scale_evidence": scale_input,
            "foot_points": feet_input,
            "floor_points": points_input,
            "camera_heights": heights_input,
        },
        "evidence": result.evidence,
        "reprojection": cross_view_reprojection(run, original_width=widths),
        "warnings": list(result.warnings),
    }
    if not run.record["person_masks_applied"]:
        record["warnings"].append("사람 마스크를 적용하지 않았다(사람 점이 바닥·벽 추정에 섞일 수 있다)")
    if result.status == STATUS_OK:
        M = result.matrix()
        record["mapanything_to_world"] = M.tolist()
        alignment = WorldAlignment(result.rotation, M[:3, 3], result.scale, "WORLD_FRAME_102")
        ext = alignment.apply({v.camera_id: tuple(x.tolist() for x in cam2world_to_extrinsics(v.cam2world)) for v in run.views})
        cams = []
        for cid in sorted(ext):
            cam = intrinsics.camera(cid).camera
            R, t = ext[cid]
            cams.append(CameraCalibration(
                cid, tuple(cam["image_size"]), cam["K"], R, t,
                distortion_model=cam["distortion"]["model"], distortion_coeffs=cam["distortion"]["coeffs"],
            ))
        judgement = CalibrationJudgement(
            state=CalibrationState.PROVISIONAL,
            failures=(),
            scale=ScaleDecision(None, None, tuple(ScaleBasis(b) for b in result.scale_cross_checks), ScaleBasis(result.scale_basis)),
        )
        doc = build_calibration_doc(
            calibration_version=args.calibration_version or f"cal-wf-{stamp}",
            provider_source=_provider_source(provenance),
            provider_version=PROVIDER_VERSION,
            created_at=created_at,
            cameras=cams,
            judgement=judgement,
            fallback_scale_basis=ScaleBasis.MODEL_ESTIMATE,
            site_id=args.site_id,
            world_axes_source=result.axes_source,
        )
        problems = check_calibration(doc)
        if problems:
            record.update(status=STATUS_FAILED, reason="CALIBRATION_CHECK", detail="; ".join(problems))
            record.pop("mapanything_to_world")
        else:
            record["calibration"] = doc
            record["note"] = (f"World 고정({result.axes_source}): {result.evidence['axes']['rule']}. "
                              f"스케일 {result.scale_basis} "
                              f"{result.scale:.4f}. PROVISIONAL(독립 검증 전)")
    _write(out, record)
    _print_summary(record)
    print(f"결과: {out}")
    if record["status"] != STATUS_OK:
        print(f"World 고정 못 함: {record['reason']}: {record['detail']}", file=sys.stderr)
        return EXIT_NOT_FIXED
    return EXIT_OK


def _provider_source(provenance: dict[str, Any]) -> str:
    """G1_AUTO. 합친 출처가 상한(SIM·DATASET_GT)이면 그 값이라 CALIBRATED 가 될 수 없다(-214 와 같은 규칙)."""
    source = provenance["calibration"]["source"]
    return source if source in ("SIM", "DATASET_GT") else "G1_AUTO"


def _print_summary(record: dict[str, Any]) -> None:
    ev = record.get("evidence") or {}
    floor, room, scale = ev.get("floor") or {}, ev.get("room") or {}, ev.get("scale") or {}
    print(f"상태 {record['status']} {record.get('reason') or ''} 축 출처 {record.get('axes_source')}")
    if floor:
        print(f"바닥: 지지 {floor.get('inliers')} rms {floor.get('rms_ma')} 발 확인 {floor.get('foot_check', {}).get('status')}")
    if scale:
        print(f"스케일: {scale.get('basis')} {scale.get('scale')} 교차 확인 {scale.get('cross_checks')}")
    if scale.get("camera_heights"):
        h = scale["camera_heights"]
        print(f"카메라 높이(모델 단위): {h.get('model_height_ma')} 비 {h.get('ratio')} 퍼짐 {h.get('spread')} "
              f"{h.get('rejected') or ''}")
    fp = (ev.get("axes") or {}).get("floor_points")
    if fp:
        print(f"바닥 두 점({fp.get('camera_id')}): {fp.get('points')} 거리 {fp.get('separation_m')} m")
    if room:
        print(f"카메라 높이(m): {room.get('camera_heights_m')} 바닥 XY p2 {room.get('floor_xy_p2_m')} p98 {room.get('floor_xy_p98_m')}")
    for cid, rep in (record.get("reprojection") or {}).get("cameras", {}).items():
        print(f"{cid} 카메라 간 재투영: {rep}")


def _pair(value: str, name: str) -> tuple[str, str]:
    key, sep, val = value.partition("=")
    if not sep or not key or not val:
        raise ValueError(f"{name} 는 CAM=VALUE 형식이다: {value!r}")
    return key, val


def _write(out: Path, record: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as f:  # 덮어쓰지 않는다(AGENTS 6)
        f.write(json.dumps(record, ensure_ascii=False, indent=1, allow_nan=False))


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _utc_stamp(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y%m%d-%H%M%Sz")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
