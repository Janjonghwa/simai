"""PTS 없는 raw H.264 파일을 설정 → 실제 페이싱 → I0 헤더까지 재생한다(-353)."""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path

import pytest
from simai_recorder.capture_time import CaptureTimeModel, ReceiveTimeEstimator
from simai_recorder.clock import FakeClock
from videos import make_video

from simai_relay.config import load_cameras
from simai_relay.relay import CameraRelay
from simai_relay.sink import MemorySink


def _config(tmp_path: Path, common: str = "", camera: str = "") -> Path:
    path = tmp_path / "file.toml"
    path.write_text(common + '\n[[cameras]]\ncamera_id = "cam_A"\nkind = "file"\n'
                    'file = "input.h264"\nloop = true\nfps = 25\n' + camera, encoding="utf-8")
    return path


def test_paced_file_without_pts_stays_below_tier0_uncertainty_limit(tmp_path, contract_validator):
    make_video(tmp_path / "input.h264", frames=10)
    clock = FakeClock()
    start_ms = clock.now_ms()
    [spec] = load_cameras(_config(tmp_path), clock, {})
    sink = MemorySink("cam_A")
    relay = CameraRelay(spec, sink, clock, threading.Event(), clock.monotonic() + 1.15)
    relay.run()
    assert relay.error is None
    assert len(sink.frames) == 30  # 파일을 세 번 순환해도 같은 페이싱 시각선이다
    for i, frame in enumerate(sink.frames):
        h = frame.header
        contract_validator("frame_header", h)
        assert h["source_pts"] is None  # PTS 를 지어내지 않는다
        assert h["estimated_capture_time_ms"] == h["received_at_ms"] == start_ms + 40 * i
        # cross_view.InterpolationConfig 는 geometry.interpolation 에서 가져오며 기본 문턱은 25 ms 이다.
        assert h["timestamp_uncertainty_ms"] == 20.0
        assert h["timestamp_uncertainty_ms"] < 25.0
        assert h["timestamp_source"] == "FILE"


@pytest.mark.parametrize(("common", "camera", "expected"), [
    ("", "", 20.0),
    ("[capture_time]\nuncertainty_no_pts_ms = 18\n", "", 18.0),
    ("[capture_time]\nuncertainty_no_pts_ms = 18\n",
     "capture_time = { uncertainty_no_pts_ms = 12 }\n", 12.0),
    ("[capture_time]\nuncertainty_no_pts_ms = 45\n", "", 45.0),
    ("", "realtime = false\n", 45.0),
])
def test_file_uncertainty_defaults_and_explicit_overrides(tmp_path, common, camera, expected):
    [spec] = load_cameras(_config(tmp_path, common, camera), FakeClock(), {})
    assert spec.capture_time.uncertainty_no_pts_ms == expected


def test_rtsp_capture_time_model_and_estimates_are_unchanged(tmp_path):
    path = tmp_path / "rtsp.toml"
    path.write_text('[[cameras]]\ncamera_id = "cam_A"\nkind = "rtsp"\nhost = "127.0.0.1"\n', encoding="utf-8")
    [spec] = load_cameras(path, FakeClock(), {"RTSP_USER": "test", "RTSP_PASS": "test"})
    assert spec.capture_time == CaptureTimeModel()
    estimator = ReceiveTimeEstimator(spec.capture_time, Fraction(1, 90000), 25)
    assert estimator.estimate(1000, 0).timestamp_uncertainty_ms == 20.0
    no_pts = estimator.estimate(1040, None)
    assert no_pts.estimated_capture_time_ms == 1040
    assert no_pts.timestamp_uncertainty_ms == 45.0
