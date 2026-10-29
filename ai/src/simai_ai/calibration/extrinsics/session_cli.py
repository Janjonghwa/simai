"""session CLI의 파일 입출력. 영상은 읽기만 하고 결과는 새 디렉터리에 쓴다."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from simai_ai.calibration.extrinsics.initial import checked_provenance, combine_input_provenance
from simai_ai.calibration.extrinsics.keypoints import read_keypoint_file, write_keypoint_file
from simai_ai.calibration.extrinsics.session import adjust_session, select_anchor_cameras
from simai_ai.calibration.extrinsics.session_tracks import track_session
from simai_ai.calibration.world_frame.inputs import load_camera_heights
from simai_ai.io.sources import load_recording_manifest
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.providers.provenance import build_input_provenance


def _hash(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def run_session(args, created_at):
    from simai_ai.calibration.extrinsics.cli import EXIT_NOT_USABLE, EXIT_OK, _lens, _write

    manifest = load_recording_manifest(args.recording)
    provider = FileCalibrationProvider(args.previous)
    previous = provider.current()
    heights, height_input = load_camera_heights(args.camera_heights)
    height_prov = checked_provenance(height_input["input_provenance"], "camera_heights")
    if args.out.exists():
        raise ValueError(f"결과 디렉터리가 이미 있다(덮어쓰지 않는다): {args.out}")
    fixed_lens = _lens(args.fixed_lens)
    # 유한성/양수 렌즈 검증은 추출 전에 한다.
    from simai_ai.calibration.bundle import BundleConfig

    BundleConfig(lens_mode="fixed", fixed_lens=fixed_lens)
    anchor_cameras = None
    if getattr(args, "anchor_cameras", None):
        anchor_cameras = [c.strip() for c in args.anchor_cameras.split(",") if c.strip()]
        select_anchor_cameras(previous, previous["cameras"], anchor_cameras=anchor_cameras)
    if args.start_s < 0 or (args.duration_s is not None and args.duration_s <= 0):
        raise ValueError("시작은 0 이상, 추출 길이는 양수다")
    if args.window_step_s < 0 or args.window_duration_s <= 0:
        raise ValueError("분산 창 간격은 0 이상, 길이는 양수다")
    if args.window_step_s and args.window_step_s < args.window_duration_s:
        raise ValueError("분산 창은 서로 겹치지 않아야 한다")
    if not args.keypoints and not args.model:
        raise ValueError("--keypoints 또는 로컬 --model이 필요하다")
    known = {c["camera_id"] for c in previous["cameras"]}
    recording_sizes = {c["camera_id"]: [c["settings"]["width"], c["settings"]["height"]]
                       for c in manifest["cameras"]}
    if set(recording_sizes) != known or set(heights) - known:
        raise ValueError("녹화/직전 보정/실측 높이의 카메라가 맞지 않는다")
    for c in previous["cameras"]:
        if recording_sizes[c["camera_id"]] != c["image_size"]:
            raise ValueError("해상도가 바뀌었다. 내부 파라미터부터 다시 추정해야 한다")
    extract_elapsed_s = None
    if args.keypoints:
        header, rows = read_keypoint_file(args.keypoints)
    else:
        from simai_ai.calibration.extrinsics.extract import UltralyticsPose
        from simai_ai.io.pyav_decoder import PyAvDecoder

        if not Path(args.model).is_file():
            raise ValueError("session --model은 이미 있는 로컬 가중치 파일이어야 한다")
        window = manifest["common_capture_window_ms"]
        if window is None:
            raise ValueError("공통 촬영 창이 없다")
        start = window[0] + round(args.start_s * 1000)
        end = window[1] if args.duration_s is None else min(window[1], start + round(args.duration_s * 1000))
        t_extract0 = time.monotonic()
        detector = UltralyticsPose(args.model, imgsz=args.imgsz, device=args.device)
        if args.window_step_s:
            windows = [(lo, min(end, lo + round(args.window_duration_s * 1000)))
                       for lo in range(start, end, round(args.window_step_s * 1000))]
            windows = [(lo, hi) for lo, hi in windows if hi - lo >= max(1000, args.sample_period_ms * 2)]
        else:
            windows = [(start, end)]
        header, rows = extract_windows(
            args.recording, detector, windows, created_at=created_at, decoder_factory=PyAvDecoder,
            every_n=args.every_n, batch=args.batch,
        )
        extract_elapsed_s = round(time.monotonic() - t_extract0, 2)
        header["extraction_config"] = {
            "start_s": args.start_s,
            "duration_s": args.duration_s,
            "window_step_s": args.window_step_s,
            "window_duration_s": args.window_duration_s,
            "every_n": args.every_n,
            "batch": args.batch,
            "window_count": len(windows),
            "elapsed_s": extract_elapsed_s,
        }
    if header.get("session_id") != manifest["session_id"]:
        raise ValueError("keypoint 캐시가 다른 회차다")
    if {c["camera_id"]: c["image_size"] for c in header["cameras"]} != recording_sizes:
        raise ValueError("keypoint 캐시와 녹화의 카메라/해상도가 다르다")
    # 직전 보정과 실측 높이 중 상한 입력이 있으면 결과도 상한이다.
    provenance, sources = combine_input_provenance(
        header["input_provenance"], {"previous": build_input_provenance(
            header["input_provenance"]["frames"], provider.provenance()), "camera_heights": height_prov},
        previous["calibration_version"],
    )
    if not provenance["is_upper_bound"]:
        provenance = build_input_provenance(header["input_provenance"]["frames"], provider.provenance())
    args.out.mkdir(parents=True)
    if not args.keypoints:
        write_keypoint_file(args.out / "keypoints.jsonl", header, rows)
    tracks = track_session(header, rows, sample_period_ms=args.sample_period_ms)
    print(f"로컬 추적: {tracks.record['local_tracks']}개 트랙, {len(tracks.ticks)}개 공통 시각", flush=True)
    t_adjust0 = time.monotonic()
    outcome = adjust_session(
        tracks, previous, measured_camera_heights=heights, input_provenance=provenance,
        created_at=created_at, rounds=args.rounds, fixed_lens=fixed_lens,
        anchor_cameras=anchor_cameras,
        progress=lambda message: print(message, flush=True),
    )
    adjust_elapsed_s = round(time.monotonic() - t_adjust0, 2)
    report = dict(outcome.report)
    report["inputs"] = {
        "session_id": manifest["session_id"],
        "recording": str(args.recording),
        "previous_sha256": _hash(args.previous),
        "camera_heights": height_input,
        "input_sources": sources,
        "detector": header.get("detector"),
        "every_n": header.get("every_n"),
        "window_ms": header.get("window_ms"),
        "sample_windows_ms": header.get("sample_windows_ms"),
        "extraction_config": header.get("extraction_config"),
        "extraction_elapsed_s": extract_elapsed_s if extract_elapsed_s is not None else (header.get("extraction_config") or {}).get("elapsed_s"),
        "adjust_elapsed_s": adjust_elapsed_s,
        "keypoints_sha256": _hash(args.keypoints or (args.out / "keypoints.jsonl")),
    }
    _write(args.out / "report.json", report)
    if outcome.calibration is None:
        print(f"새 보정 미생성: {report.get('failure_reason')}. 진단: {args.out / 'report.json'}")
        return EXIT_NOT_USABLE
    _write(args.out / "calibration.json", dict(outcome.calibration))
    print(f"새 보정: {outcome.calibration['calibration_version']} PROVISIONAL — {args.out / 'calibration.json'}")
    return EXIT_OK


def extract_windows(recording, detector, windows, **kwargs):
    """분산된 짧은 연속 구간들을 추출한다. 구간 사이의 빈 시각이나 관측은 메우지 않는다."""
    from simai_ai.calibration.extrinsics.extract import extract_keypoints

    header, rows = None, []
    if not windows or any(lo >= hi for lo, hi in windows):
        raise ValueError("추출 창이 비었거나 길이가 없다")
    if any(windows[i][1] >= windows[i + 1][0] for i in range(len(windows) - 1)):
        raise ValueError("추출 창은 증가 순서이며 서로 겹치지 않아야 한다")
    for idx, (lo, hi) in enumerate(windows):
        head, iterator = extract_keypoints(recording, detector, start_ms=lo, end_ms=hi, **kwargs)
        rows.extend(iterator)
        if header is None:
            header = head
        else:
            for base, cam in zip(header["cameras"], head["cameras"], strict=True):
                for key in ("frames_in_window", "frames_inferred", "decode_errors", "segments"):
                    base[key] += cam[key]
        print(f"관절 추출 창 {idx + 1}/{len(windows)}: 누적 {len(rows)}개 카메라 프레임", flush=True)
    header["window_ms"] = [windows[0][0], windows[-1][1]]
    header["sample_windows_ms"] = [list(w) for w in windows]
    return header, rows
