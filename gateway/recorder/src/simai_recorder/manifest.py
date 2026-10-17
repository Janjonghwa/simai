"""녹화 manifest: 세션 하나의 파일 목록·해시·프레임 수·시각 범위·카메라 설정·동의 범위·저장 위치.

형식 정본은 docs/data/recording-manifest.md 다(계약 스키마는 S15P31A101-20 머지 뒤 별도 티켓에서 contracts/ 에 넣는다).
영상은 저장소에 넣지 않고 Network Volume 에 두며, 저장소에는 manifest 만 둔다(명세 21.1).

manifest 는 세션 디렉터리의 파일에서 다시 계산할 수 있다(write_manifest). 올린 뒤에는 verify 로 해시를 다시 확인한다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import statistics
from pathlib import Path
from typing import Any

from simai_recorder.recorder import EVENTS_FILE, SESSION_FILE, SESSION_ID
from simai_recorder.segment import SegmentPaths, camera_segments, read_frames

MANIFEST_FILE = "manifest.json"
MANIFEST_VERSION = "1.0.0"
GAP_FACTOR = 1.5  # 도착 간격이 공칭 프레임 간격의 1.5배를 넘으면 camera_to_gateway_gap (-9 측정 도구와 같은 기준)
STORAGE_URI = re.compile(r"^(volume|s3)://[^\s]+$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _file_entry(path: Path, root: Path) -> dict[str, Any]:
    return {"path": _rel(path, root), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q / 100 * len(s)))]


def segment_stats(paths: SegmentPaths, root: Path) -> dict[str, Any]:
    codec = json.loads(paths.codec_info.read_text(encoding="utf-8"))
    rows = list(read_frames(paths.frames))
    headers = [r["header"] for r in rows]
    h264_size = paths.h264.stat().st_size
    seqs = [h["frame_sequence"] for h in headers]
    capture = [h["estimated_capture_time_ms"] for h in headers]
    received = [h["received_at_ms"] for h in headers if h.get("received_at_ms") is not None]
    uncertainty = [h["timestamp_uncertainty_ms"] for h in headers]

    fps = codec.get("nominal_fps")
    arrivals = [b - a for a, b in zip(received, received[1:], strict=False)]
    nominal_ms = 1000 / fps if fps else statistics.median(arrivals) if arrivals else None
    threshold = round(nominal_ms * GAP_FACTOR, 1) if nominal_ms else None
    gaps = [x for x in arrivals if threshold is not None and x > threshold]
    key_capture = [h["estimated_capture_time_ms"] for h in headers if h["keyframe"]]
    key_iv = [(b - a) / 1000 for a, b in zip(key_capture, key_capture[1:], strict=False)]

    return {
        "stream_epoch": codec["stream_epoch"],
        "video": _file_entry(paths.h264, root),
        "frames": _file_entry(paths.frames, root),
        "codec_info": _file_entry(paths.codec_info, root),
        "frame_count": len(headers),
        "keyframe_count": len(key_capture),
        "frame_sequence_range": [seqs[0], seqs[-1]] if seqs else None,
        "capture_time_range_ms": [min(capture), max(capture)] if capture else None,
        "received_time_range_ms": [min(received), max(received)] if received else None,
        "timestamp_sources": sorted({h["timestamp_source"] for h in headers}),
        "timestamp_uncertainty_ms_max": max(uncertainty) if uncertainty else None,
        "keyframe_interval_s_p50": round(statistics.median(key_iv), 3) if key_iv else None,
        "camera_to_gateway_gap": {
            "threshold_ms": threshold,
            "count": len(gaps),
            "max_ms": max(arrivals) if arrivals else None,
            "arrival_interval_ms_p95": _pct(arrivals, 95),
        },
        "integrity": {
            "sequence_contiguous": seqs == list(range(len(seqs))),
            "first_is_keyframe": bool(headers) and headers[0]["keyframe"],
            "bytes_covered": (rows[-1]["offset"] + rows[-1]["size"] == h264_size) if rows else h264_size == 0,
        },
    }


def _camera_events(camera_dir: Path) -> list[dict[str, Any]]:
    path = camera_dir / EVENTS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def camera_entry(session_dir: Path, cam: dict[str, Any]) -> dict[str, Any]:
    camera_dir = session_dir / cam["camera_id"]
    seg_paths = camera_segments(camera_dir) if camera_dir.exists() else []
    segments = [segment_stats(p, session_dir) for p in seg_paths]
    codecs = [json.loads(p.codec_info.read_text(encoding="utf-8")) for p in seg_paths]
    first_codec = codecs[0] if codecs else None
    resolutions = {(c["width"], c["height"]) for c in codecs}
    events = _camera_events(camera_dir)
    capture = [s["capture_time_range_ms"] for s in segments if s["capture_time_range_ms"]]
    entry: dict[str, Any] = {
        "camera_id": cam["camera_id"],
        "source": cam["source"],
        "settings": {
            "codec": first_codec["codec"] if first_codec else None,
            "profile": first_codec.get("profile") if first_codec else None,
            "width": first_codec["width"] if first_codec else None,
            "height": first_codec["height"] if first_codec else None,
            "nominal_fps": first_codec.get("nominal_fps") if first_codec else None,
            "firmware": cam.get("firmware"),
            "resolution_changed": len(resolutions) > 1,
        },
        "timestamp_source": cam["capture_time_model"]["timestamp_source"],
        "capture_time_model": cam["capture_time_model"],
        "frame_count": sum(s["frame_count"] for s in segments),
        "capture_time_range_ms": [min(c[0] for c in capture), max(c[1] for c in capture)] if capture else None,
        "reconnects": sum(1 for e in events if e["event"] == "disconnected"),
        "open_failures": sum(1 for e in events if e["event"] == "open_failed"),
        "dropped_before_keyframe": sum(e.get("dropped_before_keyframe", 0) for e in events
                                       if e["event"] == "segment_started"),
        "errors": [e["detail"] for e in events if e["event"] == "error"],
        "events": _file_entry(camera_dir / EVENTS_FILE, session_dir) if (camera_dir / EVENTS_FILE).exists() else None,
        "segments": segments,
    }
    return entry


def build_manifest(session_dir: Path) -> dict[str, Any]:
    session = json.loads((session_dir / SESSION_FILE).read_text(encoding="utf-8"))
    cameras = [camera_entry(session_dir, c) for c in session["cameras"]]
    kinds = sorted({c["source"]["kind"] for c in cameras})
    ranges = [c["capture_time_range_ms"] for c in cameras]
    common = None
    if ranges and all(ranges):
        lo, hi = max(r[0] for r in ranges), min(r[1] for r in ranges)
        common = [lo, hi] if lo <= hi else None
    storage = session["storage_uri"].rstrip("/") + "/" + session["session_id"] + "/"
    return {
        "manifest_version": MANIFEST_VERSION,
        "session_id": session["session_id"],
        "scenario": session["scenario"],
        "description": session.get("description", ""),
        "input_kinds": kinds,
        # RTSP 가 아닌 입력(로컬 파일·합성)이 섞인 세션은 시험 입력이다. 제품 성능·게이트 판정에 쓰지 않는다.
        "is_test_input": kinds != ["RTSP"],
        "started_at_ms": session["started_at_ms"],
        "ended_at_ms": session.get("ended_at_ms"),
        "requested_duration_s": session.get("requested_duration_s"),
        "common_capture_window_ms": common,
        "calibration_version": session.get("calibration_version"),
        "consent": session.get("consent"),
        "storage": {"uri": storage, "layout": "<camera_id>/epoch_NNNN.{h264,frames.jsonl,codec_info.json}"},
        "recorded_by": session.get("recorded_by", {}),
        "notes": session.get("notes", ""),
        "cameras": cameras,
    }


def validate_consent(consent: dict[str, Any] | None, kinds: list[str]) -> list[str]:
    """동의·보관 규칙(명세 11.5).

    실제 카메라(RTSP) 녹화는 보관 기한이 있어야 하고, 사람이 찍히면 동의서 ID 와 동의 범위가 있어야 한다.
    """
    errors: list[str] = []
    if consent is None:
        if "RTSP" in kinds:
            errors.append("consent: RTSP 녹화는 consent(보관 기한·사람 포함 여부)가 있어야 한다")
        return errors
    if not isinstance(consent.get("contains_people"), bool):
        errors.append("consent.contains_people: true/false 로 적는다")
    retention = consent.get("retention_until")
    try:
        dt.date.fromisoformat(str(retention))
    except ValueError:
        errors.append(f"consent.retention_until: YYYY-MM-DD 날짜여야 한다 (받은 값 {retention!r})")
    if consent.get("contains_people"):
        if not consent.get("consent_ref"):
            errors.append("consent.consent_ref: 사람이 찍히는 녹화는 참여자 동의서 ID 가 있어야 한다")
        if not consent.get("scope"):
            errors.append("consent.scope: 사람이 찍히는 녹화는 동의 범위(용도·전송 리전)가 있어야 한다")
    return errors


def validate_manifest(m: dict[str, Any]) -> list[str]:
    """manifest 자체 검증. 오류 문장 목록(비면 통과)."""
    errors: list[str] = []
    if not SESSION_ID.fullmatch(str(m.get("session_id", ""))):
        errors.append("session_id: 영문 소문자·숫자·- 만 쓴다")
    if not m.get("scenario"):
        errors.append("scenario: 비어 있다")
    if not STORAGE_URI.match(str(m.get("storage", {}).get("uri", ""))):
        errors.append("storage.uri: volume://<datacenter>/<path> 또는 s3:// 여야 한다 (로컬 경로 금지)")
    errors += validate_consent(m.get("consent"), list(m.get("input_kinds", [])))
    for cam in m.get("cameras", []):
        cid = cam.get("camera_id")
        src = cam.get("source", {})
        if "url" in src and "@" in str(src["url"]):
            errors.append(f"{cid}: source.url 에 계정이 들어 있다")
        for seg in cam.get("segments", []):
            for key in ("video", "frames", "codec_info"):
                f = seg[key]
                if not SHA256.match(f["sha256"]):
                    errors.append(f"{cid}: {f['path']} sha256 형식이 아니다")
                if Path(f["path"]).is_absolute() or ".." in Path(f["path"]).parts:
                    errors.append(f"{cid}: {f['path']} 는 세션 디렉터리 기준 상대 경로여야 한다")
            for k, ok in seg["integrity"].items():
                if not ok:
                    errors.append(f"{cid}: epoch {seg['stream_epoch']} 무결성 검사 실패: {k}")
    return errors


def write_manifest(session_dir: Path) -> dict[str, Any]:
    manifest = build_manifest(session_dir)
    path = session_dir / MANIFEST_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return manifest


def verify(session_dir: Path) -> list[str]:
    """올린 뒤·내려받은 뒤 확인: manifest 의 해시·크기와 실제 파일을 비교한다. 불일치 문장 목록."""
    manifest = json.loads((session_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    problems = validate_manifest(manifest)
    for cam in manifest["cameras"]:
        entries = [cam["events"]] if cam.get("events") else []
        for seg in cam["segments"]:
            entries += [seg["video"], seg["frames"], seg["codec_info"]]
        for e in entries:
            p = session_dir / e["path"]
            if not p.exists():
                problems.append(f"없음: {e['path']}")
            elif p.stat().st_size != e["bytes"] or sha256_file(p) != e["sha256"]:
                problems.append(f"해시·크기 불일치: {e['path']}")
    return problems
