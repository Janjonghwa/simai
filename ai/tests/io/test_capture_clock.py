"""깨진 PTS 시각선의 프레임 시계 (S15P31A101-315)와 단조 보장 (S15P31A101-316): 노트북 중계·녹화 도구와 같은 값인지, 녹화
재생이 다시 매기는지.

같은 값: gateway/recorder 가 만든 시험 벡터(tests/data/capture_clock_vector.json)를 이 구현으로 다시 계산해 비교한다.
녹화 재생: 합성 녹화(영상 바이트는 임의 값, 디코딩하지 않음)의 헤더만 본다.
"""

import json
import random
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from recording_session import Segment, codec_doc, header, write_session  # noqa: E402

from simai_ai.io import I0Frame, RecordingReplay  # noqa: E402
from simai_ai.io.capture_clock import CaptureClockModel, EpochMonotonic, repair_segment  # noqa: E402

VECTOR = HERE.parents[2] / "gateway" / "recorder" / "tests" / "data" / "capture_clock_vector.json"
T0 = 1792552246000


def _final(result, recorded) -> list[list]:
    return [list(header if t is None else t) for t, header in zip(result.times, recorded)]


def test_same_values_as_the_gateway_estimator() -> None:
    """단조 보장 전(-316 전) 녹화 도구의 헤더(recorded)에서 지금 노트북 중계·녹화 도구와 같은 값(expected)을 낸다. 깨짐을 보기 전
    PTS 식 값의 올림, 전환 순간의 올림(벡터 26장째)과 올린 횟수·최대 올림량까지 같다."""
    doc = json.loads(VECTOR.read_text(encoding="utf-8"))
    rows = [(received, pts, doc["pts_timebase"]) for received, pts in doc["frames"]]
    model = CaptureClockModel.from_manifest(doc["capture_time_model"])
    recorded = [tuple(h) for h in doc["recorded"]]
    result = repair_segment(rows, doc["nominal_fps"], model, recorded)
    k = doc["broken_since"]
    assert result.broken_since == k and k is not None
    assert _final(result, recorded) == doc["expected"]
    assert (result.raised, result.max_raise_ms) == (doc["raised"], doc["max_raise_ms"])
    assert result.times[k] is not None and result.times[k][0] == doc["expected"][k - 1][0] + 1  # 전환 순간 올림
    # 이 수정 뒤 녹화(헤더가 이미 expected)는 한 프레임도 바뀌지 않는다
    again = repair_segment(rows, doc["nominal_fps"], model, [tuple(h) for h in doc["expected"]])
    assert all(t is None for t in again.times)


def test_epoch_monotonic_matches_the_gateway_rule() -> None:
    g = EpochMonotonic()
    assert [g.apply(t, 20.0) for t in (100, 140, 120, 141, 141, 300)] == [
        (100, 20.0), (140, 20.0), (141, 41.0), (142, 21.0), (143, 22.0), (300, 20.0)]
    assert (g.raised, g.max_raise_ms) == (3, 21)
    assert EpochMonotonic().apply(5, None) == (5, None)  # 불확실성을 모르면 지어내지 않는다


def _mixed_segment(seed: int, jumps: bool, n: int = 1500):
    """수신 ±80 ms 흔들림·2~3 s 멈춤 2회·빠진 프레임·(jumps 면) 묶음 점프. 녹화 헤더 값은 -316 전 녹화처럼 PTS 식(창 최솟값)이라
    뒤로 가거나 같은 값이 섞인다. (rows, recorded)."""
    rng = random.Random(seed)
    a = rng.randrange(60, 400)
    stalls = ((a, rng.uniform(2000, 3000)), (a + rng.randrange(40, 200), rng.uniform(2000, 3000)))
    drops = set(rng.sample(range(50, n), 6))
    rows, recorded, prev, window, cam_i = [], [], None, [], 0
    while len(rows) < n:
        if cam_i in drops:
            cam_i += 1
            continue
        shift = sum(ms for at, ms in stalls if cam_i >= at)
        true_ms = T0 + cam_i * 40.08 + shift
        pts_ms = 1000 + cam_i * 40.0 + shift - (400.0 if jumps and (cam_i // 13) % 2 else 0.0)
        received = round(true_ms + 100 + rng.uniform(-80, 80))
        received = received if prev is None else max(prev, received)
        prev = received
        pts = round(pts_ms * 90)
        window = [(r, o) for r, o in window if r >= received - 30_000] + [(received, received - pts / 90)]
        recorded.append((round(pts / 90 + min(o for _, o in window)), 20.0))
        rows.append((received, pts, "1/90000"))
        cam_i += 1
    return rows, recorded


@pytest.mark.parametrize("jumps", [True, False])
@pytest.mark.parametrize("seed", range(6))
def test_replayed_capture_time_never_goes_back(seed, jumps) -> None:
    """녹화 재생도 노트북 중계와 같은 성질: 흔들림·멈춤·빠짐·묶음 점프를 섞어도 세그먼트 안 촬영 시각은 매 프레임 1 ms 이상 앞으로
    가고, 올린 프레임은 올린 양만큼 불확실성이 크다. 멀쩡한 PTS 의 멈춤 두 번은 깨짐으로 보지 않는다."""
    rows, recorded = _mixed_segment(seed, jumps)
    model = CaptureClockModel(latency_ms=0.0, uncertainty_ms=20.0, uncertainty_no_pts_ms=45.0, window_ms=30_000)
    result = repair_segment(rows, 25.0, model, recorded)
    final = _final(result, recorded)
    assert all(b[0] >= a[0] + 1 for a, b in zip(final, final[1:]))
    assert min(u for _, u in final) >= 20.0
    assert (result.broken_since is not None and result.broken_since <= 26) if jumps else result.broken_since is None


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
    assert (a.camera_id, a.stream_epoch, a.broken_since, a.repaired, a.skipped, a.raised) == ("cam_A", 0, 13, 287, None, 0)
    # 공칭 fps 를 모르면 프레임 시계는 쓰지 않고 단조 보장만 한다: 녹화 헤더가 뒤로 간 걸음(묶음마다)을 직전 + 1 ms 로 올린다
    assert (b.broken_since, b.skipped) == (None, "no_nominal_fps") and b.repaired == b.raised > 0
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
