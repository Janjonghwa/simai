"""I0 내부 메시지 형식 (명세 12.7). 시각은 UTC int64 ms, 모르는 값은 None."""

import pytest

from simai_ai.io import I0CodecInfo, I0FrameHeader

T0 = 1760342400000


def _header(**kw):
    fields = {
        "camera_id": "cam_A",
        "stream_epoch": 12,
        "frame_sequence": 10324,
        "estimated_capture_time_ms": T0 + 100,
        "timestamp_source": "RECEIVE_ESTIMATED",
        "keyframe": True,
        "timestamp_uncertainty_ms": 18,
        "source_pts": 9876543,
        "pts_timebase": "1/90000",
    }
    return I0FrameHeader(**(fields | kw))


def test_spec_example_header():
    """명세 12.7 의 헤더 예시를 그대로 받는다."""
    h = _header()
    assert h.key == (12, 10324)
    assert h.estimated_capture_time_ms == 1760342400100


def test_unknown_uncertainty_stays_none():
    assert _header(timestamp_uncertainty_ms=None).timestamp_uncertainty_ms is None


@pytest.mark.parametrize(
    "kw",
    [
        pytest.param({"camera_id": ""}, id="empty-camera"),
        pytest.param({"camera_id": "cam A"}, id="camera-pattern"),  # common/defs camera_id ^[A-Za-z0-9_-]+$
        pytest.param({"estimated_capture_time_ms": 2**63}, id="over-int64"),
        pytest.param({"stream_epoch": -1}, id="negative-epoch"),
        pytest.param({"stream_epoch": True}, id="bool-epoch"),
        pytest.param({"frame_sequence": 1.0}, id="float-seq"),
        pytest.param({"estimated_capture_time_ms": 1760342400.1}, id="float-seconds"),
        pytest.param({"estimated_capture_time_ms": -1}, id="negative-time"),
        pytest.param({"timestamp_source": "NTP"}, id="unknown-ts-source"),
        pytest.param({"keyframe": 1}, id="int-keyframe"),
        pytest.param({"timestamp_uncertainty_ms": -1}, id="negative-uncertainty"),
        pytest.param({"timestamp_uncertainty_ms": float("inf")}, id="inf-uncertainty"),
    ],
)
def test_header_rejects(kw):
    with pytest.raises((TypeError, ValueError)):
        _header(**kw)


@pytest.mark.parametrize("kw", [{"codec": ""}, {"stream_epoch": -1}, {"camera_id": ""}, {"camera_id": "cam/1"}])
def test_codec_info_rejects(kw):
    fields = {"camera_id": "cam_A", "stream_epoch": 0, "codec": "h264"}
    with pytest.raises(ValueError):
        I0CodecInfo(**(fields | kw))
