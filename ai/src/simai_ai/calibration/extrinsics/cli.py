"""녹화 세션 하나로 카메라 외부 파라미터 보정을 끝까지 돌리는 실행 도구 (명세 4.3 1~3단계 연결, FR-23). `ai/` 에서:

    # 1) keypoint 추출 (GPU 세션: 운영 모델, 노트북: 작은 모델로 짧게)
    uv run --extra io --extra detection python -m simai_ai.calibration.extrinsics.cli extract \\
        --session experiments/recordings/rec-… --model yolo26x-pose.pt --device cuda:0
    # 2) 번들 조정 (노트북 CPU)
    uv run python -m simai_ai.calibration.extrinsics.cli adjust --keypoints <추출 결과>.jsonl \\
        --intrinsics experiments/recordings/intrinsics/intrinsics-….json --cameras-json <MapAnything>/cameras.json \\
        [--world-alignment <-102 결과>.json] [--measured-objects <정물 관측>.json] [--walkable-area "x,y;x,y;…"] \\
        [--lens per_camera|shared|fixed [--fixed-lens F,K1,K2]] [--camera-heights <실측 카메라 높이>.json]

단계: 녹화 → YOLO pose keypoint(카메라별, `extract`) → 촬영 시각 바로잡기(`sync`, -313) →
한 명 걷기 대응·공통 시각 PersonObservation(`keypoints`) →
cameras.json → World 기준 초기 외부 파라미터(`initial`) → `adjust_human_bundle` → 계약 camera_calibration 카메라 문서·
판정 근거(재투영 오차, 커버리지, 퇴화 표시) → 보정 버전 저장소 제안(`HumanBundleResult.propose_to`, 판정은 criteria).

- 판정·출처 규칙은 번들 조정 라이브러리 그대로다(SIM 입력 → provider_source SIM, CALIBRATED 불가. 정물 없음 → MODEL_ESTIMATE).
- World 좌표계를 -102 정렬 없이 임시로 세웠으면(INTERIM_PERSON_VERTICAL) 조정 결과는 쓰지만 버전으로 제안하지 않는다
  (World 축·원점은 첫 CALIBRATED 때 한 번 정한다, 4.1). 이 도구의 버전 저장소는 실행마다 새로 만든 메모리 저장소이고,
  결과 파일의 proposal.doc 이 저장소가 받아 준 보정 문서다(운영 저장소 반영은 이 도구 밖).
- 출처: keypoint 파일·내부 파라미터 기록·World 정렬·실측 정물의 input_provenance 를 합쳐 결과와 번들 조정에 넘긴다
  (`initial.combine_input_provenance`). 하나라도 SIM·DATASET_GT 면 상한이고 provider_source 도 그 출처다(CALIBRATED 불가).
- 렌즈(-321): `--lens` 기본 per_camera(카메라별 f·k1·k2), shared(4대 공통 f·k1·k2, 카메라별 R·t 만), fixed(렌즈를 바꾸지
  않고 R·t 만. `--fixed-lens` 가 없으면 내부 파라미터 기록 값 그대로). 결과 bundle.lens.
- 실측 카메라 높이(-321, 선택): `--camera-heights` 는 world_frame 과 같은 `simai.camera_heights` 형식(바닥 → 렌즈 중심 m).
  2대 이상·카메라끼리 3% 안이면 중앙값 배율로 World 축척을 정한다(스케일 근거 MEASURED_CAMERA_HEIGHT, 실측 정물이 있으면
  교차 확인만). 결과 bundle.scale.camera_heights 에 카메라별 비·잔차·버린 이유. keypoint 파일에 없는 카메라는 입력 오류다.
- 출력 기본 위치: `ai/experiments/recordings/extrinsics/` (.gitignore 의 recordings/ 로 제외). 영상·이미지는 쓰지 않는다.
  결과 파일이 이미 있으면 덮어쓰지 않고 오류(종료 2)다.
- 종료 코드: 0 제안이 받아들여짐, 2 인자·입력 오류, 3 조정 불확실·실패·대응 불가·제안 거절, 4 임시 World 라 제안하지 않음.
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
DEFAULT_OUT_DIR = AI_DIR / "experiments" / "recordings" / "extrinsics"
RUN_FORMAT = "simai.extrinsics_run"
RUN_VERSION = "0.1.0"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_NOT_USABLE = 3
EXIT_NOT_PROPOSED = 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m simai_ai.calibration.extrinsics.cli", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract", help="녹화 세션 → keypoint 시퀀스 파일")
    ex.add_argument("--session", type=Path, required=True, help="gateway/recorder 녹화 세션 디렉터리(manifest.json)")
    ex.add_argument("--model", required=True, help="YOLO pose 가중치·엔진 (운영 yolo26x-pose, 노트북은 작은 모델 가능)")
    ex.add_argument("--imgsz", type=int, default=1280)
    ex.add_argument("--device", default=None, help="cpu, cuda:0 … (기본 ultralytics 선택)")
    ex.add_argument("--cameras", help="쓸 camera_id 목록(쉼표). 없으면 모두")
    ex.add_argument("--start-s", type=float, default=0.0, help="공통 촬영 창 시작에서 몇 초 뒤부터")
    ex.add_argument("--duration-s", type=float, default=None, help="몇 초 동안(없으면 창 끝까지)")
    ex.add_argument("--every-n", type=int, default=3, help="창 안 프레임 n 장마다 한 장 검출(25 fps 에서 3 → 약 8 Hz)")
    ex.add_argument("--batch", type=int, default=8)
    ex.add_argument("--out", type=Path, help=f"keypoint 파일 (기본 {DEFAULT_OUT_DIR}/keypoints-<세션>-<UTC>.jsonl)")

    ad = sub.add_parser("adjust", help="keypoint 파일 + 내부 파라미터 + cameras.json → 번들 조정 → 보정 제안")
    ad.add_argument("--keypoints", type=Path, required=True)
    ad.add_argument("--intrinsics", type=Path, required=True, help="내부 파라미터 기록(simai.intrinsics_estimate)")
    ad.add_argument("--cameras-json", type=Path, required=True, help="MapAnything cameras.json (초기 외부 파라미터)")
    ad.add_argument("--view", action="append", default=[], metavar="CAM=VIEW", help="camera_id ↔ cameras.json view 이름")
    ad.add_argument("--world-alignment", type=Path, help="MapAnything world → World 닮음 변환(-102). 없으면 임시 정렬")
    ad.add_argument("--measured-objects", type=Path, help="실측 정물 끝점 관측 JSON (-100)")
    ad.add_argument("--walkable-area", help="걸을 수 있는 바닥 다각형 World XY 'x,y;x,y;…' (바닥 커버리지)")
    ad.add_argument("--min-person-score", type=float, default=0.5)
    ad.add_argument("--sample-period-ms", type=int, default=300)
    ad.add_argument(
        "--no-time-sync",
        action="store_true",
        help="촬영 시각 바로잡기(깨진 PTS 시각선 다시 매기기·카메라별 시각 보정값)를 하지 않는다(비교용)",
    )
    ad.add_argument(
        "--lens",
        choices=("per_camera", "shared", "fixed"),
        default="per_camera",
        help="렌즈 변수: per_camera 카메라별 f·k1·k2(기본), shared 4대 공통 f·k1·k2, fixed 렌즈 고정(R·t 만)",
    )
    ad.add_argument("--fixed-lens", metavar="F,K1,K2", help="--lens fixed 에서 4대에 쓸 초점(px)·k1·k2. 없으면 내부 파라미터 기록")
    ad.add_argument(
        "--camera-heights",
        type=Path,
        help='실측 카메라 높이(m, 바닥 → 렌즈 중심) {"format": "simai.camera_heights", "heights_m": {"cam_A": 2.21, …}}',
    )
    ad.add_argument("--out", type=Path, help=f"결과 JSON (기본 {DEFAULT_OUT_DIR}/extrinsics-<UTC>.json)")
    se = sub.add_parser("session", help="회차의 여러 사람 궤적 + 직전 보정 → 새 외부 파라미터")
    se.add_argument("--recording", type=Path, required=True)
    se.add_argument("--previous", type=Path, required=True)
    se.add_argument("--camera-heights", type=Path, help="직전 World 정렬 뒤 높이 검산용(선택)")
    se.add_argument("--lens", choices=("fixed",), default="fixed")
    se.add_argument("--fixed-lens", default="1284.27,-0.29198,0.05982", metavar="F,K1,K2")
    se.add_argument("--out", type=Path, required=True, help="새 출력 디렉터리(덮어쓰기 금지)")
    se.add_argument("--keypoints", type=Path, help="같은 회차의 추출 캐시(없으면 --model로 추출)")
    se.add_argument("--model", help="로컬 YOLO pose 가중치/엔진 경로")
    se.add_argument("--imgsz", type=int, default=1280)
    se.add_argument("--device", default=None)
    se.add_argument("--start-s", type=float, default=5.0, help="공통 촬영 창 시작에서 건너뛸 초(기본 5초: 시각 수렴 전 제외)")
    se.add_argument("--duration-s", type=float)
    se.add_argument("--window-step-s", type=float, default=24.0, help="0은 연속 추출, 양수는 회차 전체에 짧은 창을 분산(기본 24초)")
    se.add_argument("--window-duration-s", type=float, default=9.0, help="분산 추출 창 길이(기본 9초)")
    se.add_argument("--every-n", type=int, default=5, help="창 안 프레임 n장마다 한 장 검출(25fps에서 5 → 200ms 간격, 보간 창 이내)")
    se.add_argument("--batch", type=int, default=8)
    se.add_argument("--sample-period-ms", type=int, default=300)
    se.add_argument("--rounds", type=int, choices=(2, 3), default=3)
    se.add_argument("--anchor-cameras", help="World 정렬 기준 카메라 목록(쉼표). 없으면 자동으로 고른다")
    return p


def main(argv: Sequence[str] | None = None, *, now_ms: int | None = None) -> int:
    args = build_parser().parse_args(argv)
    created_at = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    try:
        if args.command == "extract":
            return _extract(args, created_at)
        if args.command == "session":
            from simai_ai.calibration.extrinsics.session_cli import run_session

            return run_session(args, created_at)
        return _adjust(args, created_at)
    except (ValueError, KeyError, OSError, RuntimeError) as e:  # RuntimeError: 검출기 extras 없음
        print(f"설정·입력 오류: {e}", file=sys.stderr)
        return EXIT_CONFIG


# --------------------------------------------------------------------------- extract


def _extract(args: argparse.Namespace, created_at: int) -> int:
    from simai_ai.calibration.extrinsics.extract import (
        UltralyticsPose,
        extract_keypoints,
    )
    from simai_ai.calibration.extrinsics.keypoints import write_keypoint_file
    from simai_ai.io.pyav_decoder import PyAvDecoder
    from simai_ai.io.sources import load_recording_manifest

    manifest = load_recording_manifest(args.session)
    window = manifest["common_capture_window_ms"]
    if window is None:
        raise ValueError("녹화 manifest 에 공통 촬영 창이 없다(카메라 간 겹치는 시간이 없다)")
    start = window[0] + round(args.start_s * 1000)
    end = window[1] if args.duration_s is None else min(window[1], start + round(args.duration_s * 1000))
    out = args.out or DEFAULT_OUT_DIR / f"keypoints-{manifest['session_id']}-{_utc_stamp(created_at)}.jsonl"
    _require_new(out)
    detector = UltralyticsPose(args.model, imgsz=args.imgsz, device=args.device)
    cameras = [c.strip() for c in args.cameras.split(",") if c.strip()] if args.cameras else None
    header, rows = extract_keypoints(
        args.session,
        detector,
        created_at=created_at,
        decoder_factory=PyAvDecoder,
        cameras=cameras,
        start_ms=start,
        end_ms=end,
        every_n=args.every_n,
        batch=args.batch,
    )
    collected = list(rows)
    write_keypoint_file(out, header, collected)
    for cam in header["cameras"]:
        people = sum(1 for r in collected if r["camera_id"] == cam["camera_id"] and r["detections"])
        print(
            f"{cam['camera_id']}: 창 안 {cam.get('frames_in_window', 0)} 장, 검출 {cam.get('frames_inferred', 0)} 장, "
            f"사람 보인 장 {people}, 디코딩 실패 {cam.get('decode_errors', 0)}"
        )
    if header["is_test_input"]:
        print("주의: 시험 입력 세션(is_test_input)이다. 제품 성능 근거로 쓰지 않는다", file=sys.stderr)
    print(f"keypoint 파일: {out}")
    return EXIT_OK


# --------------------------------------------------------------------------- adjust


def _adjust(args: argparse.Namespace, created_at: int) -> int:
    from simai_ai.calibration.bundle import (
        STATUS_CONVERGED,
        BundleConfig,
        MeasuredObjectObservation,
        adjust_human_bundle,
    )
    from simai_ai.calibration.extrinsics.initial import (
        SOURCE_INTERIM,
        alignment_from_people,
        checked_provenance,
        combine_input_provenance,
        load_mapanything_cameras,
        load_world_alignment,
    )
    from simai_ai.calibration.extrinsics.keypoints import (
        CorrespondenceConfig,
        CorrespondenceError,
        build_observations,
        read_keypoint_file,
        single_person_samples,
    )
    from simai_ai.calibration.extrinsics.sync import estimate_time_offsets, repair_timelines, rigs_from_initial
    from simai_ai.calibration.intrinsics.record import bundle_init_from_record
    from simai_ai.calibration.versions import CalibrationVersionStore
    from simai_ai.calibration.world_frame.inputs import load_camera_heights

    out = args.out or DEFAULT_OUT_DIR / f"extrinsics-{_utc_stamp(created_at)}.json"
    _require_new(out)
    header, rows = read_keypoint_file(args.keypoints)
    intr_raw = args.intrinsics.read_bytes()
    intr_doc = json.loads(intr_raw.decode("utf-8"))
    intrinsics = bundle_init_from_record(intr_doc)
    sizes = {c["camera_id"]: tuple(c["image_size"]) for c in header["cameras"]}
    for cam in intrinsics.cameras:
        if cam.camera_id in sizes and tuple(cam.camera["image_size"]) != sizes[cam.camera_id]:
            raise ValueError(
                f"{cam.camera_id}: 내부 파라미터 영상 크기 {cam.camera['image_size']} 와 keypoint 영상 크기 "
                f"{list(sizes[cam.camera_id])} 가 다르다"
            )
    view_map = dict(_pair(v, "--view") for v in args.view) or None
    initial = load_mapanything_cameras(args.cameras_json, view_map)
    objects, objects_input = (), None
    if args.measured_objects:
        objects, objects_input = _measured_objects(args.measured_objects, MeasuredObjectObservation, checked_provenance)
    provided = load_world_alignment(args.world_alignment) if args.world_alignment else None
    expected = None if provided is None else provided.record.get("cameras_json_sha256")
    if expected and expected != initial.record["sha256"]:
        raise ValueError("World 정렬이 다른 MapAnything 실행(cameras.json)으로 만든 것이다")
    expected = None if provided is None else provided.record.get("intrinsics_sha256")
    if expected and expected != _sha256(intr_raw):
        raise ValueError("World 정렬이 다른 내부 파라미터 기록(--intrinsics)으로 만든 것이다")
    if args.fixed_lens and args.lens != "fixed":
        raise ValueError("--fixed-lens 는 --lens fixed 와 함께 준다")
    heights, heights_input = None, None
    if args.camera_heights:
        heights, heights_input = load_camera_heights(args.camera_heights)
        heights_input["input_provenance"] = checked_provenance(heights_input["input_provenance"], args.camera_heights.name)
        unknown = sorted(set(heights) - set(sizes))
        if unknown:
            raise ValueError(f"카메라 높이에 keypoint 파일에 없는 카메라가 있다: {unknown} (keypoint 카메라 {sorted(sizes)})")
    config = BundleConfig(
        walkable_area_xy=_polygon(args.walkable_area) if args.walkable_area else (),
        lens_mode=args.lens,
        fixed_lens=_lens(args.fixed_lens) if args.fixed_lens else None,
    )
    # 모든 입력의 출처를 합친다: 하나라도 SIM·DATASET_GT 면 결과 전체가 상한이다(CALIBRATED 불가)
    provenance, provenance_record = combine_input_provenance(
        header["input_provenance"],
        {
            "intrinsics": checked_provenance(intr_doc.get("input_provenance"), args.intrinsics.name),
            "world_alignment": None if provided is None else provided.record.get("input_provenance"),
            "measured_objects": None if objects_input is None else objects_input["input_provenance"],
            "camera_heights": None if heights_input is None else heights_input["input_provenance"],
        },
        f"mapanything-{initial.record.get('run_id') or initial.record['sha256'][7:19]}",
    )

    record: dict[str, Any] = {
        "format": RUN_FORMAT,
        "format_version": RUN_VERSION,
        "created_at": created_at,
        "input_provenance": provenance,
        "input_sources": provenance_record,
        "inputs": {
            "keypoints": {
                "path": args.keypoints.name,
                "sha256": _sha256(args.keypoints.read_bytes()),
                "session_id": header.get("session_id"),
                "is_test_input": header.get("is_test_input"),
                "detector": header.get("detector"),
            },
            "intrinsics": {"path": args.intrinsics.name, "sha256": _sha256(intr_raw), "flags": {
                c.camera_id: list(c.flags) for c in intrinsics.cameras
            }},
            "cameras_json": dict(initial.record),
            "measured_objects": objects_input,
            "camera_heights": heights_input,
        },
        "warnings": [],
        "proposal": None,
        "proposal_skipped": None,
    }
    # 촬영 시각 바로잡기(-313): 깨진 PTS 시각선은 frame_sequence 시계로 다시 매기고, 카메라별 상수 보정값을 초기 카메라로 잰다
    time_offsets = None
    if args.no_time_sync:
        record["timing"] = None
    else:
        rows, timeline = repair_timelines(rows)
        retimed = sorted(k for k, v in timeline.items() if v.get("retimed"))
        if retimed:
            record["warnings"].append(
                f"{', '.join(retimed)}: 촬영 시각이 frame_sequence 순서와 어긋나(PTS 묶음 어긋남) frame_sequence 시계로 다시 매겼다"
            )
        samples, _ = single_person_samples(header, rows, args.min_person_score)
        offsets = estimate_time_offsets(samples, rigs_from_initial(intrinsics, initial.extrinsics))
        if offsets.usable:
            time_offsets = dict(offsets.offsets_ms)
        else:
            why = offsets.record.get("reason") or {
                c: v["reason"] for c, v in offsets.record.get("cameras", {}).items() if v.get("reason")
            }
            record["warnings"].append(f"카메라별 시각 보정값을 정하지 못해 보정하지 않았다: {why}")
        record["timing"] = {"timeline": timeline, "offsets": dict(offsets.record), "applied_offsets_ms": time_offsets}
    try:
        report = build_observations(
            header,
            rows,
            CorrespondenceConfig(min_person_score=args.min_person_score, sample_period_ms=args.sample_period_ms),
            time_offsets_ms=time_offsets,
        )
    except CorrespondenceError as e:
        record["correspondence_error"] = str(e)
        _write(out, record)
        print(f"사람 대응·시각 정렬 부족: {e}", file=sys.stderr)
        return EXIT_NOT_USABLE
    record["correspondence"] = dict(report.record)
    record["warnings"] += list(report.warnings)

    if provided is not None:
        alignment = provided
    else:
        try:
            alignment = alignment_from_people(
                report.observations,
                intrinsics,
                initial.extrinsics,
                min_confidence=config.min_keypoint_confidence,
                ankle_contact_height_m=config.ankle_contact_height_m,
            )
        except ValueError as e:  # 관측은 있지만 상방을 정할 표본이 부족하다(데이터 부족, 설정 오류가 아님)
            record["world_frame_error"] = str(e)
            _write(out, record)
            print(f"임시 World 정렬 불가: {e}", file=sys.stderr)
            return EXIT_NOT_USABLE
        record["warnings"].append("World 좌표계를 사람 세로축으로 임시로 세웠다(벽 방향·원점 미정, -102). 버전으로 제안하지 않는다")
    record["world_frame"] = dict(alignment.record)

    result = adjust_human_bundle(
        report.observations,
        intrinsics,
        alignment.apply(initial.extrinsics),
        input_provenance=provenance,
        created_at=created_at,
        measured_objects=objects,
        measured_camera_heights=heights,
        config=config,
    )
    record["bundle"] = dict(result.record)
    record["cameras"] = [c.to_contract() for c in result.cameras]
    for w in record["warnings"]:
        print(f"경고: {w}", file=sys.stderr)
    _print_summary(result)

    if result.status != STATUS_CONVERGED:
        record["proposal_skipped"] = f"번들 조정 {result.status}: {', '.join(result.flags)}"
        _write(out, record)
        return EXIT_NOT_USABLE
    if alignment.source == SOURCE_INTERIM:
        record["proposal_skipped"] = "World 좌표계 미고정(INTERIM_PERSON_VERTICAL)"
        _write(out, record)
        return EXIT_NOT_PROPOSED
    store = CalibrationVersionStore(camera_ids=[c["camera_id"] for c in header["cameras"]])
    # World 축 출처(-102 결과의 axes_source)를 보정 문서에 옮긴다(AGENTS 7). 없으면 비운다
    proposal = result.propose_to(store, at_ms=created_at, world_axes_source=alignment.record.get("axes_source"))
    record["proposal"] = {
        "adopted": proposal.adopted,
        "state": str(proposal.judgement.state),
        "failed_criteria": sorted(str(c) for c in proposal.judgement.failed_criteria),
        "reasons": [f.message for f in proposal.judgement.failures],
        "reject_reason": None if proposal.reject_reason is None else str(proposal.reject_reason),
        "violations": list(proposal.violations),
        "doc": proposal.doc,
    }
    _write(out, record)
    print(f"제안: adopted={proposal.adopted} state={proposal.judgement.state} 결과: {out}")
    return EXIT_OK if proposal.adopted else EXIT_NOT_USABLE


def _measured_objects(path: Path, cls, checked_provenance) -> tuple[tuple, dict[str, Any]]:
    """정물 끝점 관측 JSON `{"objects": [{label, length_m, endpoints: {cam: [[u,v],[u,v]]}}], "input_provenance"?}`."""
    raw = Path(path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    out = []
    for o in doc.get("objects", []):
        endpoints = {cid: (tuple(map(float, e[0])), tuple(map(float, e[1]))) for cid, e in o["endpoints"].items()}
        out.append(cls(o["label"], float(o["length_m"]), endpoints))
    info = {
        "path": Path(path).name,
        "sha256": _sha256(raw),
        "labels": [o.label for o in out],
        "input_provenance": checked_provenance(doc.get("input_provenance"), Path(path).name),
    }
    return tuple(out), info


def _lens(text: str) -> tuple[float, float, float]:
    try:
        values = tuple(float(v) for v in text.split(","))
    except ValueError:
        values = ()
    if len(values) != 3:
        raise ValueError(f"--fixed-lens 는 'F,K1,K2' (수 3개)다: {text!r}")
    return values  # type: ignore[return-value]


def _polygon(text: str) -> tuple[tuple[float, float], ...]:
    pts = tuple(tuple(float(v) for v in p.split(",")) for p in text.split(";") if p.strip())
    if len(pts) < 3 or any(len(p) != 2 for p in pts):
        raise ValueError(f"--walkable-area 는 'x,y;x,y;x,y…' (3점 이상)이다: {text!r}")
    return pts  # type: ignore[return-value]


def _pair(value: str, name: str) -> tuple[str, str]:
    key, sep, val = value.partition("=")
    if not sep or not key or not val:
        raise ValueError(f"{name} 는 CAM=VALUE 형식이다: {value!r}")
    return key, val


def _require_new(out: Path) -> None:
    if out.exists():
        raise ValueError(f"결과 파일이 이미 있다(덮어쓰지 않는다): {out}")


def _write(out: Path, record: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as f:  # 덮어쓰지 않는다(AGENTS 6)
        f.write(json.dumps(record, ensure_ascii=False, indent=1, allow_nan=False))


def _print_summary(result) -> None:
    print(f"번들 조정: {result.status} 출처={result.provider_source} 표시={list(result.flags)} 기록={list(result.notes)}")
    for cam in result.record.get("cameras", []):
        e = cam["reprojection_error_px"]
        print(
            f"  {cam['camera_id']}: 재투영 중앙값 {e['median']} px, p95 {e['p95']} px, 높이 {cam['camera_height_m']} m, "
            f"덮음 {cam['image_coverage']}, 초점 {cam['focal_px']} px, k {cam['k']}"
        )
    lens = result.record.get("lens") or {}
    if lens:
        print(f"렌즈: {lens.get('mode')} {lens.get('shared') or lens.get('fixed_lens') or ''}")
    heights = (result.record.get("scale") or {}).get("camera_heights")
    if heights:
        if heights.get("rejected"):
            print(f"실측 카메라 높이 축척: 쓰지 않음 — {heights['rejected']} {heights.get('exceeding') or ''}")
        else:
            print(
                f"실측 카메라 높이 축척: 배율 {heights['scale']} (적용 {heights['applied']}), 비 {heights['ratio']}, "
                f"잔차 m {heights['residual_m']}"
            )


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _utc_stamp(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y%m%d-%H%M%Sz")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
