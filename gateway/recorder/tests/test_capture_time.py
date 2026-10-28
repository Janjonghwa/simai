import json
import random
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from simai_recorder.capture_time import CaptureTimeModel, EpochMonotonic, ReceiveTimeEstimator, lower_envelope_line

T0 = 1_760_342_400_000
FPS = 25


def _arrivals(n: int, base_delay: float, jitter: float, seed: int = 0, drift_ppm: float = 0.0):
    rng = random.Random(seed)
    for i in range(n):
        true_ms = T0 + i * 1000 / FPS
        pts = i * 90000 // FPS
        received = true_ms * (1 + drift_ppm * 1e-6) - T0 * drift_ppm * 1e-6 + base_delay + rng.uniform(0, jitter)
        yield true_ms, pts, round(received)


def test_pts_anchor_removes_arrival_jitter():
    """도착 지터(0~80ms)가 있어도 PTS 로 추정하면 오차가 지터보다 훨씬 작다. 고정 지연은 latency_ms 로 뺀다."""
    model = CaptureTimeModel(latency_ms=30)
    est = ReceiveTimeEstimator(model, Fraction(1, 90000))
    errors_pts, errors_recv = [], []
    for i, (true_ms, pts, received) in enumerate(_arrivals(500, base_delay=30, jitter=80)):
        t = est.estimate(received, pts)
        assert t.timestamp_uncertainty_ms == model.uncertainty_ms
        if i >= 50:  # 창에 덜 늦게 온 프레임이 쌓인 뒤
            errors_pts.append(abs(t.estimated_capture_time_ms - true_ms))
            errors_recv.append(abs(received - 30 - true_ms))
    assert max(errors_pts) <= 3
    assert max(errors_recv) > 60


def test_window_follows_clock_drift():
    """카메라 RTP 시계와 노트북 시계의 표류(200ppm)를 창 안 최솟값으로 따라간다."""
    est = ReceiveTimeEstimator(CaptureTimeModel(window_ms=5_000), Fraction(1, 90000))
    last_err = None
    for true_ms, pts, received in _arrivals(25 * 120, base_delay=0, jitter=20, drift_ppm=200):
        t = est.estimate(received, pts)
        last_err = t.estimated_capture_time_ms - (true_ms * (1 + 200e-6) - T0 * 200e-6)
    # 2분 동안 24ms 표류하지만 창 안에서 다시 맞춘다
    assert abs(last_err) <= 5


def test_without_pts_falls_back_to_receive_time():
    model = CaptureTimeModel(latency_ms=12)
    est = ReceiveTimeEstimator(model, None)
    t = est.estimate(T0 + 100, None)
    assert t.estimated_capture_time_ms == T0 + 88
    assert t.timestamp_uncertainty_ms == model.uncertainty_no_pts_ms
    t = ReceiveTimeEstimator(model, Fraction(1, 90000)).estimate(T0, None)
    assert t.timestamp_uncertainty_ms == model.uncertainty_no_pts_ms


@pytest.mark.parametrize("kwargs", [{"timestamp_source": "GUESS"}, {"latency_ms": -1}, {"uncertainty_ms": float("inf")},
                                    {"window_ms": 0}])
def test_model_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        CaptureTimeModel(**kwargs)


# --------------------------------------------------------------------------- 깨진 PTS 시각선 (S15P31A101-315)

VECTOR = Path(__file__).resolve().parent / "data" / "capture_clock_vector.json"
TRUE_INTERVAL_MS = 40.08  # C110 실측: 노트북 시계로 24.95 fps (공칭 25)


def _broken_stream(n: int, *, seed: int = 1, drops: tuple[int, ...] = (), jump_ms: float = 400.0, block: int = 13,
                   base_delay: float = 30.0, jitter: float = 60.0, first_pts_none: bool = False):
    """C110 처럼 PTS 가 block 장 묶음마다 jump_ms 만큼 번갈아 뛰는 스트림 (정답 촬영 시각, PTS, 수신 시각).

    묶음 안 PTS 간격은 공칭 40 ms, 실제 촬영 간격은 40.08 ms. drops 는 카메라가 보내지 않은 프레임 번호(PTS 가 80 ms 뛴다).
    """
    rng = random.Random(seed)
    out, cam_i = [], 0
    while len(out) < n:
        if cam_i in drops:
            cam_i += 1
            continue
        true_ms = T0 + cam_i * TRUE_INTERVAL_MS
        pts_ms = 1000 + cam_i * 40.0 - (jump_ms if (cam_i // block) % 2 else 0.0)
        pts = None if (first_pts_none and not out) else round(pts_ms * 90)
        out.append((true_ms, pts, round(true_ms + base_delay + rng.uniform(0, jitter))))
        cam_i += 1
    return out


def _run(est: ReceiveTimeEstimator, frames):
    return [est.estimate(received, pts) for _, pts, received in frames]


def test_broken_pts_switches_to_frame_clock():
    """묶음 점프(±400 ms) PTS 는 첫 뒤로 간 점프에서 프레임 시계로 바꾸고, 그 뒤 촬영 시각은 앞으로만 가며 정답에 붙는다.

    PTS 식(바꾸기 전 방식)은 같은 입력에서 묶음째 약 400 ms 틀린다. 빠진 프레임(PTS 80 ms)도 센다.
    """
    model = CaptureTimeModel(latency_ms=30)
    frames = _broken_stream(25 * 120, drops=(700, 1500, 2200))
    est = ReceiveTimeEstimator(model, Fraction(1, 90000), nominal_fps=FPS)
    out = _run(est, frames)
    assert est.broken_since == 13 and est.mode == "frame_clock"  # 첫 묶음 경계(뒤로 400 ms)에서 바로
    after = out[13:]
    assert all(b.estimated_capture_time_ms > a.estimated_capture_time_ms for a, b in zip(after, after[1:]))
    warm = [i for i, t in enumerate(out) if i >= 13 and t.timestamp_uncertainty_ms == model.uncertainty_ms]
    assert warm and warm[0] <= 13 + 6 * FPS  # 하한 직선이 서기 전(약 5 s)은 PTS 없음 불확실성
    assert {t.timestamp_uncertainty_ms for t in out[13:warm[0]]} == {model.uncertainty_no_pts_ms}
    errors = [abs(out[i].estimated_capture_time_ms - frames[i][0]) for i in warm]
    assert max(errors) <= 12

    old = _run(ReceiveTimeEstimator(model, Fraction(1, 90000)), frames)  # 공칭 fps 없음 = 바꾸기 전 PTS 식
    assert max(abs(t.estimated_capture_time_ms - f[0]) for t, f in zip(old[250:], frames[250:])) > 300


def test_forward_jump_first_needs_a_second_broken_step():
    """처음 점프가 앞으로면(카메라 멈춤 뒤 재개일 수도 있다) 바로 바꾸지 않고, 10 s 안 두 번째 깨진 걸음에서 바꾼다."""
    frames = _broken_stream(200, jump_ms=-400.0)  # 묶음 1 이 +400 ms
    est = ReceiveTimeEstimator(CaptureTimeModel(), Fraction(1, 90000), nominal_fps=FPS)
    out = _run(est, frames)
    assert est.broken_since == 26
    assert all(t.timestamp_uncertainty_ms == 20.0 for t in out[:26])


def _normal_stream(n: int, *, reorder: bool, stall_at: int | None = None, seed: int = 2):
    """멀쩡한 PTS: 0~80 ms 도착 흔들림, B 프레임 2장 재정렬(표시 I0 B1 B2 P3 → 디코딩 I0 P3 B1 B2, PTS 간격 +120·−80·+40·+160),
    한 번의 1 s 멈춤(PTS·수신 모두)."""
    rng = random.Random(seed)
    order: list[int] = [0]
    for g in range(1, n, 3):
        order += [g + 2, g, g + 1] if reorder else [g, g + 1, g + 2]
    out = []
    for i in order[:n]:
        shift = 1000.0 if stall_at is not None and i >= stall_at else 0.0
        true_ms = T0 + i * 40.0 + shift
        received = round(T0 + len(out) * 40.0 + 120 + shift + rng.uniform(0, 80))
        out.append((true_ms, round((i * 40.0 + shift) * 90), received))
    return out


@pytest.mark.parametrize("reorder, stall_at", [(False, None), (True, None), (False, 300), (True, 301)])  # 301: B 묶음 경계
def test_healthy_pts_is_left_unchanged(reorder, stall_at):
    """흔들림·B 프레임 재정렬·한 번의 멈춤은 깨짐이 아니다: 공칭 fps 를 줘도 결과가 PTS 식(단조 보장 포함)과 같다."""
    frames = _normal_stream(1500, reorder=reorder, stall_at=stall_at)
    model = CaptureTimeModel()
    with_fps = ReceiveTimeEstimator(model, Fraction(1, 90000), nominal_fps=FPS)
    assert _run(with_fps, frames) == _run(ReceiveTimeEstimator(model, Fraction(1, 90000)), frames)
    assert with_fps.broken_since is None and with_fps.mode == "pts"


def test_unknown_nominal_fps_or_timebase_never_switches():
    frames = _broken_stream(300)
    for est in (ReceiveTimeEstimator(CaptureTimeModel(), Fraction(1, 90000), nominal_fps=None),
                ReceiveTimeEstimator(CaptureTimeModel(), Fraction(1, 90000), nominal_fps=0.0),
                ReceiveTimeEstimator(CaptureTimeModel(), None, nominal_fps=FPS)):
        _run(est, frames)
        assert est.broken_since is None


def test_new_epoch_starts_again_in_pts_mode():
    """재접속(새 epoch)은 새 추정기다: PTS 식으로 시작해 다시 감지한다(이전 epoch 의 시계·하한을 잇지 않는다)."""
    frames = _broken_stream(400)
    first = ReceiveTimeEstimator(CaptureTimeModel(), Fraction(1, 90000), nominal_fps=FPS)
    _run(first, frames[:200])
    second = ReceiveTimeEstimator(CaptureTimeModel(), Fraction(1, 90000), nominal_fps=FPS)
    out = _run(second, frames[200:])
    assert first.broken_since == 13 and second.mode == "frame_clock"
    k = second.broken_since
    assert k is not None and 0 < k <= 30
    assert out[0].timestamp_uncertainty_ms == 20.0 and out[k].timestamp_uncertainty_ms == 45.0


def test_lower_envelope_line_is_the_supporting_hull_edge():
    pts = [(0.0, 5.0), (1.0, 1.0), (2.0, 3.0), (3.0, 2.0), (4.0, 6.0)]
    slope, intercept = lower_envelope_line(pts)
    assert all(y >= slope * x + intercept - 1e-9 for x, y in pts)
    assert (slope, intercept) == pytest.approx((0.5, 0.5))  # x 평균 2 를 품은 아래 껍질 변 (1,1)-(3,2)
    assert lower_envelope_line([(1.0, 1.0)]) is None


def _vector_frames():
    """처음 점프가 앞으로(+400 ms)이고 그 묶음(13~25장)이 100 ms 늦게 온 깨진 스트림. 그 묶음의 PTS 식 값이 전환 순간(26장)
    프레임 시계 값보다 커서 전환 순간에 단조 보장으로 올린다(S15P31A101-316). 빠진 프레임·첫 PTS 없음도 담는다."""
    frames = _broken_stream(600, seed=7, drops=(150, 151, 400), first_pts_none=True, jump_ms=-400.0)
    return _in_arrival_order([(t, p, r + (100 if 13 <= i < 26 else 0)) for i, (t, p, r) in enumerate(frames)])


def _vector_doc() -> dict:
    model = CaptureTimeModel(latency_ms=10)
    frames = _vector_frames()
    est = ReceiveTimeEstimator(model, Fraction(1, 90000), nominal_fps=FPS)
    out = _run(est, frames)
    unguarded = ReceiveTimeEstimator(model, Fraction(1, 90000), nominal_fps=FPS)  # -316 전 녹화 도구가 남긴 헤더 값
    recorded = [unguarded._estimate(received, pts) for _, pts, received in frames]
    return {
        "description": "S15P31A101-315·316 프레임 시계·단조 보장 시험 벡터. expected 는 gateway/recorder "
                       "capture_time.ReceiveTimeEstimator 출력, recorded 는 단조 보장 전(-316 전) 녹화 도구의 헤더 값이다. "
                       "ai simai_ai.io.capture_clock 이 recorded 헤더에서 expected 와 같은 값을 내야 한다. 다시 만들기: "
                       "gateway/recorder 에서 uv run python tests/test_capture_time.py --write-vector",
        "nominal_fps": FPS,
        "pts_timebase": "1/90000",
        "capture_time_model": model.to_dict(),
        "broken_since": est.broken_since,
        "raised": est.raised,
        "max_raise_ms": est.max_raise_ms,
        "frames": [[received, pts] for _, pts, received in frames],
        "recorded": [[t.estimated_capture_time_ms, t.timestamp_uncertainty_ms] for t in recorded],
        "expected": [[t.estimated_capture_time_ms, t.timestamp_uncertainty_ms] for t in out],
    }


def test_shared_vector_matches_this_implementation():
    """ai 녹화 재생과 같이 쓰는 벡터. 이 구현을 바꾸면 벡터를 다시 만들고 ai 쪽(capture_clock)도 같이 고친다."""
    doc = json.loads(VECTOR.read_text(encoding="utf-8"))
    assert doc == _vector_doc()
    k = doc["broken_since"]
    (t0, _), (t1, u1) = doc["expected"][k - 1], doc["expected"][k]
    assert k == 26 and t1 == t0 + 1 and u1 > 45.0  # 전환 순간 올림을 담는다(올린 양만큼 불확실성이 크다)
    assert doc["raised"] >= 2 and doc["max_raise_ms"] > 40


# --------------------------------------------------------------------------- 단조 보장·멈춤 (S15P31A101-316)

def _in_arrival_order(frames):
    """수신 시각이 줄지 않게(한 연결의 도착 순서) 고친다."""
    out, prev = [], None
    for t, p, r in frames:
        prev = r if prev is None else max(prev, r)
        out.append((t, p, prev))
    return out


def _mixed_stream(n: int, *, seed: int, jumps: bool, stalls: tuple[tuple[int, float], ...] = (),
                  drops: tuple[int, ...] = (), jitter: float = 80.0):
    """(정답 촬영 시각, PTS, 수신 시각). 수신은 정답 + 100 ms ± jitter(도착 순서 유지). stalls 는 (카메라 프레임 번호, 멈춘 ms)로
    PTS·촬영·수신이 같이 건너뛴다. jumps 면 C110 처럼 PTS 가 13장 묶음마다 400 ms 번갈아 당겨진다. drops 는 보내지 않은 프레임."""
    rng = random.Random(seed)
    out, cam_i = [], 0
    while len(out) < n:
        if cam_i in drops:
            cam_i += 1
            continue
        shift = sum(ms for at, ms in stalls if cam_i >= at)
        true_ms = T0 + cam_i * TRUE_INTERVAL_MS + shift
        pts_ms = 1000 + cam_i * 40.0 + shift - (400.0 if jumps and (cam_i // 13) % 2 else 0.0)
        out.append((true_ms, round(pts_ms * 90), round(true_ms + 100 + rng.uniform(-jitter, jitter))))
        cam_i += 1
    return _in_arrival_order(out)


def test_epoch_monotonic_raises_to_previous_plus_one_and_widens_uncertainty():
    g = EpochMonotonic()
    assert [g.apply(t, 20.0) for t in (100, 140, 120, 141, 141, 300)] == [
        (100, 20.0), (140, 20.0), (141, 41.0), (142, 21.0), (143, 22.0), (300, 20.0)]
    assert (g.raised, g.max_raise_ms) == (3, 21)


@pytest.mark.parametrize("jumps", [True, False])
@pytest.mark.parametrize("seed", range(8))
def test_capture_time_never_goes_back_within_an_epoch(seed, jumps):
    """수신 ±80 ms 흔들림·2~3 s 멈춤 2회(10 s 안)·빠진 프레임·묶음 점프를 섞어도 epoch 안 촬영 시각은 매 프레임 1 ms 이상
    앞으로 간다. 올린 프레임은 올린 양만큼 불확실성이 커서 기본값 아래로 내려가지 않는다."""
    rng = random.Random(100 + seed)
    a = rng.randrange(60, 400)
    stalls = ((a, rng.uniform(2000, 3000)), (a + rng.randrange(40, 200), rng.uniform(2000, 3000)))
    drops = tuple(sorted(rng.sample(range(50, 1500), 6)))
    frames = _mixed_stream(1500, seed=seed, jumps=jumps, stalls=stalls, drops=drops)
    model = CaptureTimeModel()
    est = ReceiveTimeEstimator(model, Fraction(1, 90000), nominal_fps=FPS)
    out = _run(est, frames)
    times = [t.estimated_capture_time_ms for t in out]
    assert all(b >= a + 1 for a, b in zip(times, times[1:]))
    assert min(t.timestamp_uncertainty_ms for t in out) >= model.uncertainty_ms
    if jumps:
        assert est.broken_since is not None and est.broken_since <= 26
    else:  # 멀쩡한 PTS 의 멈춤 두 번은 깨짐이 아니다(아래 시험)
        assert est.broken_since is None


@pytest.mark.parametrize("seed", range(5))
def test_two_stalls_on_healthy_pts_are_not_broken(seed):
    """멀쩡한 PTS 카메라가 10 s 안에 2~3 s 멈췄다 이어지기를 두 번 겪어도 프레임 시계로 바꾸지 않는다(S15P31A101-316).

    멈춤은 PTS 와 수신 시각이 같이 건너뛰므로 수신 간격도 같이 벌어진 걸음은 깨진 걸음으로 세지 않는다. -315 규칙은 두 번째
    멈춤에서 바꾸어 그 순간 촬영 시각이 약 2 s 뒤로 갔다(Tier 0 OUT_OF_ORDER 약 49장, 하한 계단으로 30 s 동안 최대 5 s 이름)."""
    frames = _mixed_stream(1500, seed=seed, jumps=False, stalls=((100, 2000.0), (160, 3000.0)))
    model = CaptureTimeModel()
    with_fps = ReceiveTimeEstimator(model, Fraction(1, 90000), nominal_fps=FPS)
    out = _run(with_fps, frames)
    assert with_fps.broken_since is None and with_fps.mode == "pts"
    assert out == _run(ReceiveTimeEstimator(model, Fraction(1, 90000)), frames)  # 공칭 fps 없는 PTS 식과 같다
    times = [t.estimated_capture_time_ms for t in out]
    assert all(b > a for a, b in zip(times, times[1:]))
    # 흔들림 80 ms + 30 s 창 안 시계 비율 차(0.2%) 60 ms 안. -315 규칙은 같은 입력에서 최대 약 5 s 틀렸다
    assert max(abs(t.estimated_capture_time_ms - f[0] - 100) for t, f in zip(out[300:], frames[300:])) <= 140


def test_reconnect_on_healthy_pts_starts_a_new_epoch_in_pts_mode():
    """재연결(새 epoch)은 새 추정기다. 멀쩡한 카메라는 두 epoch 모두 PTS 식이고 epoch 안에서 앞으로만 간다.
    재접속 뒤 PTS 원점이 바뀌어도(카메라가 0 부터 다시 셈) 이전 epoch 와 잇지 않는다."""
    frames = _mixed_stream(600, seed=3, jumps=False, stalls=((200, 2500.0),))
    second = [(t, p - frames[300][1], r) for t, p, r in frames[300:]]  # 새 연결: PTS 원점이 바뀐다
    for part in (frames[:300], second):
        est = ReceiveTimeEstimator(CaptureTimeModel(), Fraction(1, 90000), nominal_fps=FPS)
        times = [t.estimated_capture_time_ms for t in _run(est, part)]
        assert est.broken_since is None
        assert all(b > a for a, b in zip(times, times[1:]))


def test_stall_on_broken_pts_still_switches_at_the_first_backward_jump():
    """묶음 점프 카메라는 멈춤을 겪어도 첫 뒤로 간 점프(13장째)에서 바꾼다. 멈춤 판별은 앞으로 간 걸음만 거른다."""
    frames = _mixed_stream(400, seed=5, jumps=True, stalls=((5, 2000.0),), jitter=20.0)
    est = ReceiveTimeEstimator(CaptureTimeModel(), Fraction(1, 90000), nominal_fps=FPS)
    times = [t.estimated_capture_time_ms for t in _run(est, frames)]
    assert est.broken_since == 13
    assert all(b > a for a, b in zip(times, times[1:]))


if __name__ == "__main__" and "--write-vector" in sys.argv:
    VECTOR.parent.mkdir(exist_ok=True)
    VECTOR.write_text(json.dumps(_vector_doc(), ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
