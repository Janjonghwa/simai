"""4대 동시 녹화와 manifest. 실제 시계로 약 3초 돈다(합성 3대 + 로컬 파일 1대, 지연·끊김 흉내)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from videos import count_decoded, make_video

from simai_recorder.clock import SystemClock
from simai_recorder.contract import schema_errors
from simai_recorder.config import load_session
from simai_recorder.manifest import MANIFEST_FILE, validate_manifest, verify, write_manifest
from simai_recorder.recorder import record_session
from simai_recorder.segment import read_frames

CONFIG = """
[session]
scenario = "dry-run"
description = "카메라 없이 녹화 경로 시험"
storage_uri = "volume://AP-JP-1/simai/recordings"
calibration_version = "cal-3"

[capture_time]
latency_ms = 0

[[cameras]]
camera_id = "cam_A"
kind = "synthetic"
width = 96
height = 64

[[cameras]]
camera_id = "cam_B"
kind = "synthetic"
width = 96
height = 64
faults = {{ jitter_ms = 40, stalls = {{ 20 = 300 }}, seed = 3 }}

[[cameras]]
camera_id = "cam_C"
kind = "synthetic"
width = 96
height = 64
keyframe_interval_s = 0.4
faults = {{ disconnect_after = [25], failed_reopens = 1 }}

[[cameras]]
camera_id = "cam_D"
kind = "file"
file = "{video}"
loop = true
"""


@pytest.fixture(scope="module")
def session_dir(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("session")
    video = make_video(tmp / "loop.mp4", frames=50, gop=10)
    config = tmp / "dry-run.toml"
    config.write_text(CONFIG.format(video=video.as_posix()), encoding="utf-8")
    clock = SystemClock()
    spec = load_session(config, clock, env={}, session_id="rec-test-dry-run")
    return record_session(spec, tmp / "out", clock, duration_s=3.0, recorded_by={"tool": "test"},
                          reconnect_delays_s=(0.3,))


@pytest.fixture(scope="module")
def manifest(session_dir: Path) -> dict:
    return json.loads((session_dir / MANIFEST_FILE).read_text(encoding="utf-8"))


def test_four_cameras_recorded_concurrently(manifest):
    cams = {c["camera_id"]: c for c in manifest["cameras"]}
    assert list(cams) == ["cam_A", "cam_B", "cam_C", "cam_D"]
    for c in cams.values():
        assert c["frame_count"] >= 40, c["camera_id"]
        assert c["errors"] == []
    # 네 카메라가 같은 시간대를 함께 찍었다
    lo, hi = manifest["common_capture_window_ms"]
    assert hi - lo >= 1500


def test_faults_show_up_in_manifest(manifest):
    cams = {c["camera_id"]: c for c in manifest["cameras"]}
    assert cams["cam_A"]["reconnects"] == 0 and len(cams["cam_A"]["segments"]) == 1
    assert cams["cam_B"]["segments"][0]["camera_to_gateway_gap"]["count"] >= 1  # 300ms 멈춤
    c = cams["cam_C"]
    assert c["reconnects"] == 1 and c["open_failures"] == 1
    assert [s["stream_epoch"] for s in c["segments"]] == [0, 1]
    assert c["segments"][0]["frame_count"] == 25
    assert cams["cam_D"]["source"] == {"kind": "FILE", "file_name": "loop.mp4", "realtime": True, "loop": True}


def test_manifest_fields(manifest, session_dir):
    assert manifest["session_id"] == "rec-test-dry-run"
    assert manifest["input_kinds"] == ["FILE", "SYNTHETIC"]
    assert manifest["is_test_input"] is True
    assert manifest["calibration_version"] == "cal-3"
    assert manifest["storage"]["uri"] == "volume://AP-JP-1/simai/recordings/rec-test-dry-run/"
    assert validate_manifest(manifest) == []
    for cam in manifest["cameras"]:
        s = cam["settings"]
        assert s["codec"] == "H264" and s["width"] and s["height"] and s["nominal_fps"]
        assert s["firmware"] is None  # 모르면 비운다
        assert cam["timestamp_source"] == "RECEIVE_ESTIMATED"
        for seg in cam["segments"]:
            assert all(seg["integrity"].values())
            assert seg["video"]["sha256"].startswith("sha256:")
            lo, hi = seg["capture_time_range_ms"]
            assert manifest["started_at_ms"] - 1000 <= lo <= hi <= manifest["ended_at_ms"]
            headers = [r["header"] for r in read_frames(session_dir / seg["frames"]["path"])]
            assert len(headers) == seg["frame_count"]
            assert count_decoded(session_dir / seg["video"]["path"]) == seg["frame_count"]


def test_sidecar_lines_match_contract(manifest, session_dir):
    """frames.jsonl 한 줄은 계약의 frame_line(header = I0 frame_header) 형식이다."""
    for cam in manifest["cameras"]:
        for seg in cam["segments"]:
            for row in read_frames(session_dir / seg["frames"]["path"]):
                assert schema_errors(row, "#/$defs/frame_line") == []


def test_file_loop_keeps_pts_increasing(manifest, session_dir):
    (cam,) = [c for c in manifest["cameras"] if c["camera_id"] == "cam_D"]
    assert cam["frame_count"] > 50  # 50 프레임 파일을 한 번 넘게 돌았다
    pts = [r["header"]["source_pts"] for r in read_frames(session_dir / cam["segments"][0]["frames"]["path"])]
    assert all(b > a for a, b in zip(pts, pts[1:], strict=False))


def test_manifest_rebuild_and_verify(session_dir, manifest, tmp_path):
    assert write_manifest(session_dir)["cameras"] == manifest["cameras"]
    assert verify(session_dir) == []


def test_verify_detects_tampering(session_dir, manifest):
    seg = manifest["cameras"][0]["segments"][0]
    video = session_dir / seg["video"]["path"]
    original = video.read_bytes()
    try:
        video.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
        assert verify(session_dir) == [f"해시·크기 불일치: {seg['video']['path']}"]
    finally:
        video.write_bytes(original)


def test_existing_session_is_not_overwritten(session_dir):
    from simai_recorder.recorder import CameraSpec, SessionSpec
    from simai_recorder.sources import SyntheticSource

    clock = SystemClock()
    spec = SessionSpec("rec-test-dry-run", "dry-run", [CameraSpec("cam_A", SyntheticSource(clock))],
                       "volume://AP-JP-1/simai/recordings")
    with pytest.raises(FileExistsError):
        record_session(spec, session_dir.parent, clock, duration_s=0.1)
