import random
from fractions import Fraction

import pytest

from simai_recorder.capture_time import CaptureTimeModel, ReceiveTimeEstimator

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
