"""깨진 PTS 시각선의 프레임 시계 (S15P31A101-315): 노트북 중계·녹화 도구와 같은 값인지, 녹화 재생이 다시 매기는지.

같은 값: gateway/recorder 가 만든 시험 벡터(tests/data/capture_clock_vector.json)를 이 구현으로 다시 계산해 비교한다.
녹화 재생: 합성 녹화(영상 바이트는 임의 값, 디코딩하지 않음)의 헤더만 본다.
"""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from recording_session import Segment, codec_doc, header, write_session  # noqa: E402

from simai_ai.io import I0Frame, RecordingReplay  # noqa: E402
from simai_ai.io.capture_clock import CaptureClockModel, repair_segment  # noqa: E402

VECTOR = HERE.parents[2] / "gateway" / "recorder" / "tests" / "data" / "capture_clock_vector.json"
T0 = 1792552246000


def test_same_values_as_the_gateway_estimator() -> None:
    doc = json.loads(VECTOR.read_text(encoding="utf-8"))
    rows = [(received, pts, doc["pts_timebase"]) for received, pts in doc["frames"]]
    result = repair_segment(rows, doc["nominal_fps"], CaptureClockModel.from_manifest(doc["capture_time_model"]))
    k = doc["broken_since"]
    assert result.broken_since == k and k is not None
    assert all(t is None for t in result.times[:k])  # 그 전은 녹화 헤더(PTS 식) 그대로
    assert [list(t) for t in result.times[k:]] == doc["expected"][k:]


def _broken_segment(cam: str, epoch: int, n: int, *, lag: int = 30, nominal_fps: float | None = 25) -> Segment:
    """C110 녹화처럼 PTS 가 13장 묶음마다 400 ms 번갈아 당겨지고, 녹화 헤더 촬영 시각이 그 PTS 를 따른 세그먼트."""
    frames = []
    for i in range(n):
        shift = 400 if (i // 13) % 2 else 0
        received = T0 + i * 40 + lag
        h = header(cam, epoch, i, T0 + i * 40 - shift, i % 50 == 0, received_ms=received)
        h["source_pts"] = (1000 + i * 40 - shift) * 90
        frames.append((h, bytes([i % 256]) * 4))
    codec = codec_doc(cam, epoch)
    if nominal_fps is None:
        del codec["nominal_fps"]
    return Segment(epoch, codec, frames)


def _frames(replay: RecordingReplay, cam: str) -> list[I0Frame]:
    return [m.message for m in replay.pop_due(10**15) if m.camera_id == cam and isinstance(m.message, I0Frame)]


def test_replay_repairs_a_broken_pts_recording(tmp_path) -> None:
    session = write_session(tmp_path, "rec-test-broken-pts", {
        "cam_A": [_broken_segment("cam_A", 0, 300)],
        "cam_B": [_broken_segment("cam_B", 0, 300, nominal_fps=None)],
    })
    replay = RecordingReplay(session)
    (a, b) = replay.capture_time_repairs
    assert (a.camera_id, a.stream_epoch, a.broken_since, a.repaired, a.skipped) == ("cam_A", 0, 13, 287, None)
    assert (b.broken_since, b.repaired, b.skipped) == (None, 0, "no_nominal_fps")  # 공칭 fps 를 모르면 고치지 않는다
    frames = _frames(replay, "cam_A")
    times = [f.header.estimated_capture_time_ms for f in frames]
    assert all(y > x for x, y in zip(times[13:], times[14:]))  # 고친 뒤 앞으로만
    # 수신 하한(지연 30 ms, 흔들림 없음)에 붙는다. latency_ms 0 이므로 촬영 시각 = 수신 시각
    assert all(f.header.estimated_capture_time_ms == T0 + f.header.frame_sequence * 40 + 30 for f in frames[13:])
    assert frames[14].header.source_pts - frames[12].header.source_pts == 2 * 3600 - 36000  # 원본 PTS 그대로
    assert {f.header.timestamp_source for f in frames} == {"RECEIVE_ESTIMATED"}
    assert frames[12].header.estimated_capture_time_ms == T0 + 12 * 40  # 바꾸기 전은 녹화 헤더 그대로
    assert {f.header.timestamp_uncertainty_ms for f in frames[13:]} == {45.0, 20.0}  # 하한 직선이 서기 전은 넓게
    replay.close()


def test_replay_can_keep_recorded_times(tmp_path) -> None:
    session = write_session(tmp_path, "rec-test-broken-keep", {"cam_A": [_broken_segment("cam_A", 0, 60)]})
    replay = RecordingReplay(session, repair_capture_time=False)
    assert [r.skipped for r in replay.capture_time_repairs] == ["disabled"]
    times = [f.header.estimated_capture_time_ms for f in _frames(replay, "cam_A")]
    assert times[13] == T0 + 13 * 40 - 400


@pytest.mark.parametrize("received", [True, False])
def test_healthy_or_unreceived_recording_is_left_as_recorded(tmp_path, received) -> None:
    frames = [(header("cam_A", 0, i, T0 + i * 40, i == 0, received_ms=(T0 + i * 40 + 30) if received else None),
               b"\x00\x01") for i in range(80)]
    session = write_session(tmp_path, f"rec-test-healthy-{int(received)}",
                            {"cam_A": [Segment(0, codec_doc("cam_A", 0), frames)]})
    replay = RecordingReplay(session)
    (r,) = replay.capture_time_repairs
    assert r.broken_since is None and r.skipped == (None if received else "no_received_at_ms")
    assert [f.header.estimated_capture_time_ms for f in _frames(replay, "cam_A")] == [T0 + i * 40 for i in range(80)]
