"""사이드카 촬영 시각 재생과 재생 불확실성 기동 점검 (S15P31A101-721)."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

import pytest
from simai_recorder.clock import FakeClock
from simai_recorder.config import ConfigError
from simai_recorder.sources import FileSource
from videos import make_video

from simai_relay.config import load_cameras
from simai_relay.relay import CameraRelay
from simai_relay.replay_sidecar import (
    ANCHOR_LEAD_MS,
    TIER0_MAX_TIMESTAMP_UNCERTAINTY_MS,
    SidecarError,
    SidecarFileSource,
    cut_sidecar,
)
from simai_relay.sink import MemorySink

REPO_ROOT = Path(__file__).resolve().parents[3]
REC0 = 1_794_459_400_000


def _write_sidecar(path: Path, cid: str, times: list[int], unc: list[float] | None = None) -> Path:
    unc = unc or [20.0] * len(times)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for i, (t, u) in enumerate(zip(times, unc)):
            h = {"camera_id": cid, "stream_epoch": 1, "frame_sequence": i, "source_pts": None, "pts_timebase": "1/90000",
                 "estimated_capture_time_ms": t, "timestamp_source": "RECEIVE_ESTIMATED", "timestamp_uncertainty_ms": u,
                 "keyframe": i == 0, "received_at_ms": t}
            f.write(json.dumps({"header": h, "offset": 0, "size": 1}) + "\n")
    return path


def _config(tmp_path: Path, body: str, common: str = '[replay]\ntiming = "sidecar"\n') -> Path:
    path = tmp_path / "replay.toml"
    path.write_text(common + body, encoding="utf-8")
    return path


def _cam(cid: str, file: str, extra: str = "", loop: bool = True) -> str:
    return f'\n[[cameras]]\ncamera_id = "{cid}"\nkind = "file"\nfile = "{file}"\nloop = {str(loop).lower()}\nfps = 25\n{extra}'


def _run(spec, clock: FakeClock, seconds: float) -> MemorySink:
    sink = MemorySink(spec.camera_id)
    relay = CameraRelay(spec, sink, clock, threading.Event(), clock.monotonic() + seconds)
    relay.run()
    assert relay.error is None
    return sink


def test_cameras_share_recorded_capture_timeline(tmp_path, contract_validator):
    """카메라마다 실제 속도(40.1 / 40.0 ms)와 시작(30 ms 차)이 달라도 원본 촬영 시각 차를 그대로 재현하고, 순환해도 공통 길이다."""
    make_video(tmp_path / "cam_A.h264", frames=10)
    make_video(tmp_path / "cam_B.h264", frames=10)
    ta = [REC0 + round(40.1 * i) for i in range(10)]
    tb = [REC0 + 30 + 40 * i for i in range(10)]
    _write_sidecar(tmp_path / "cam_A.frames.jsonl", "cam_A", ta, [45.0] + [20.0] * 9)
    _write_sidecar(tmp_path / "cam_B.frames.jsonl", "cam_B", tb)
    clock = FakeClock()
    start_ms = clock.now_ms()
    a, b = load_cameras(_config(tmp_path, _cam("cam_A", "cam_A.h264") + _cam("cam_B", "cam_B.h264")), clock, {})
    assert isinstance(a.source, SidecarFileSource) and isinstance(b.source, SidecarFileSource)
    period = max(ta[-1] - REC0 + 40, tb[-1] - REC0 + 40)  # 가장 긴 카메라 마지막 offset + 간격 중앙값
    anchor = start_ms + ANCHOR_LEAD_MS
    sink_a = _run(a, clock, 2.5)
    assert len(sink_a.frames) >= 20
    for i, frame in enumerate(sink_a.frames):
        h = frame.header
        contract_validator("frame_header", h)
        want = anchor + (i // 10) * period + ta[i % 10] - REC0
        assert h["estimated_capture_time_ms"] == want
        assert h["received_at_ms"] == want  # 예정 시각에 맞춰 내보냈다
        assert h["timestamp_uncertainty_ms"] == (45.0 if i % 10 == 0 else 20.0)  # 녹화 원본 값 그대로
        assert h["source_pts"] is None and h["timestamp_source"] == "FILE"
    sink_b = _run(b, clock, 0.6)  # 늦게 시작해도 촬영 시각은 같은 시각축(원점은 첫 카메라가 정한 값)
    for i, frame in enumerate(sink_b.frames[:10]):
        assert frame.header["estimated_capture_time_ms"] == anchor + tb[i] - REC0
    assert (sink_b.frames[0].header["estimated_capture_time_ms"]
            - sink_a.frames[0].header["estimated_capture_time_ms"]) == 30


def test_video_longer_than_sidecar_drops_untimed_frames(tmp_path, caplog):
    make_video(tmp_path / "cam_A.h264", frames=10)
    _write_sidecar(tmp_path / "cam_A.frames.jsonl", "cam_A", [REC0 + 40 * i for i in range(6)])
    clock = FakeClock()
    [spec] = load_cameras(_config(tmp_path, _cam("cam_A", "cam_A.h264", loop=False)), clock, {})
    with caplog.at_level(logging.WARNING, logger="simai_relay"):
        sink = _run(spec, clock, 5.0)
    assert len(sink.frames) == 6  # 촬영 시각을 모르는 4장은 보내지 않는다
    assert "sidecar_frame_count_mismatch" in caplog.text


def test_missing_sidecar_warns_and_falls_back_to_fps(tmp_path, caplog):
    make_video(tmp_path / "cam_A.h264", frames=10)
    with caplog.at_level(logging.WARNING, logger="simai_relay"):
        [spec] = load_cameras(_config(tmp_path, _cam("cam_A", "cam_A.h264")), FakeClock(), {})
    assert type(spec.source) is FileSource
    assert "sidecar_missing" in caplog.text


def test_explicit_sidecar_path_and_cut_offsets(tmp_path):
    """sidecar-cut 결과(replay_offset_ms)를 재생 offset 으로 쓴다. 구간 경계가 카메라 공통이라 gap 을 건너뛰어도 정렬된다."""
    times = [REC0 + 40 * i for i in range(100)]  # 0 ~ 3960 ms
    src = _write_sidecar(tmp_path / "cam_A.frames.jsonl", "cam_A", times)
    out = tmp_path / "cut"
    out.mkdir()
    report = cut_sidecar(src, out / "cam_A.frames.jsonl", [(REC0 + 2000, REC0 + 2200), (REC0 + 400, REC0 + 600)])
    assert [(s["first_index"], s["end_index"], s["frames"]) for s in report] == [(10, 15, 5), (50, 55, 5)]
    rows = [json.loads(line) for line in (out / "cam_A.frames.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["replay_offset_ms"] for r in rows] == [0, 40, 80, 120, 160, 200, 240, 280, 320, 360]
    assert rows[5]["header"]["estimated_capture_time_ms"] == REC0 + 2000  # 원본 촬영 시각은 그대로 둔다
    with pytest.raises(FileExistsError):  # 덮어쓰지 않는다
        cut_sidecar(src, out / "cam_A.frames.jsonl", [(REC0, REC0 + 100)])
    with pytest.raises(SidecarError):
        cut_sidecar(src, out / "other.jsonl", [(REC0, REC0 + 500), (REC0 + 400, REC0 + 900)])

    make_video(tmp_path / "clip.h264", frames=10)
    clock = FakeClock()
    start_ms = clock.now_ms()
    [spec] = load_cameras(_config(tmp_path, _cam("cam_A", "clip.h264", 'sidecar = "cut/cam_A.frames.jsonl"\n')), clock, {})
    sink = _run(spec, clock, 1.0)
    assert [f.header["estimated_capture_time_ms"] - start_ms - ANCHOR_LEAD_MS for f in sink.frames[:10]] == \
        [0, 40, 80, 120, 160, 200, 240, 280, 320, 360]


def test_mixing_cut_and_original_sidecars_is_rejected(tmp_path):
    for cid in ("cam_A", "cam_B"):
        make_video(tmp_path / f"{cid}.h264", frames=4)
        _write_sidecar(tmp_path / f"{cid}.frames.jsonl", cid, [REC0 + 40 * i for i in range(4)])
    (tmp_path / "cut").mkdir()
    cut_sidecar(tmp_path / "cam_A.frames.jsonl", tmp_path / "cut/cam_A.frames.jsonl", [(REC0, REC0 + 1000)])
    body = _cam("cam_A", "cam_A.h264", 'sidecar = "cut/cam_A.frames.jsonl"\n') + _cam("cam_B", "cam_B.h264")
    with pytest.raises(ConfigError, match="섞지"):
        load_cameras(_config(tmp_path, body), FakeClock(), {})


def test_sidecar_time_going_backwards_is_rejected(tmp_path):
    make_video(tmp_path / "cam_A.h264", frames=4)
    _write_sidecar(tmp_path / "cam_A.frames.jsonl", "cam_A", [REC0, REC0 + 40, REC0 + 40, REC0 + 80])
    with pytest.raises(ConfigError, match="앞으로"):
        load_cameras(_config(tmp_path, _cam("cam_A", "cam_A.h264")), FakeClock(), {})


def test_file_uncertainty_over_tier0_limit_is_rejected_unless_allowed(tmp_path, caplog):
    make_video(tmp_path / "cam_A.h264", frames=4)
    common = "[capture_time]\nuncertainty_no_pts_ms = 45\n"
    with pytest.raises(ConfigError, match="Tier 0"):
        load_cameras(_config(tmp_path, _cam("cam_A", "cam_A.h264"), common), FakeClock(), {})
    allowed = common + "[replay]\nallow_uncertainty_over_tier0 = true\n"
    with caplog.at_level(logging.WARNING, logger="simai_relay"):
        [spec] = load_cameras(_config(tmp_path, _cam("cam_A", "cam_A.h264"), allowed), FakeClock(), {})
    assert spec.capture_time.uncertainty_no_pts_ms == 45.0
    assert "allow_uncertainty_over_tier0" in caplog.text


def test_tier0_limit_matches_ai_interpolation_default():
    src = (REPO_ROOT / "ai/src/simai_ai/geometry/interpolation.py").read_text(encoding="utf-8")
    m = re.search(r"^DEFAULT_MAX_TIMESTAMP_UNCERTAINTY_MS\s*=\s*([0-9.]+)", src, re.MULTILINE)
    assert m is not None and float(m.group(1)) == TIER0_MAX_TIMESTAMP_UNCERTAINTY_MS
