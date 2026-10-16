"""시간 모델 시험 (명세 6.1). 관측 상태 표의 모든 구간과 경계, gap·lag·staleness 정의를 확인한다."""

import pytest

from simai_ai.tracking.time_model import (
    CameraClock,
    ObservationState,
    TimeAges,
    TimeModelConfig,
    TrackTimeInput,
    advance_ms,
    classify_observation,
    compute_ages,
    compute_t_latest_global,
    evaluate_track,
    require_ms,
)

T0 = 1_760_342_400_000
OBSERVED, PREDICTED, UNKNOWN = ObservationState.OBSERVED, ObservationState.PREDICTED, ObservationState.UNKNOWN


def _track(t_last_valid=T0, velocity=None, sigma=(0.1, 0.1)):
    return TrackTimeInput(
        world_id="w-0012",
        t_last_valid=t_last_valid,
        position_xy=(3.0, 5.0),
        position_xy_sigma=sigma,
        velocity=velocity,
    )


# --- 시각 형식 (UTC int64 ms) ---


@pytest.mark.parametrize("value", [1.5, True, "1000"])
def test_require_ms_rejects_non_int(value):
    with pytest.raises(TypeError):
        require_ms("t", value)


def test_require_ms_rejects_negative():
    with pytest.raises(ValueError):
        require_ms("t", -1)


def test_camera_clock_rejects_float_time():
    with pytest.raises(TypeError):
        CameraClock("cam1", last_capture=float(T0), sync_valid=True)


# --- 지연 프레임·역순 도착 ---


def test_advance_ms_starts_from_none():
    assert advance_ms(None, T0) == T0


def test_advance_ms_moves_forward():
    assert advance_ms(T0, T0 + 40) == T0 + 40


def test_advance_ms_ignores_late_frame():
    """역순으로 늦게 도착한 프레임이 시각을 되돌리지 않는다."""
    assert advance_ms(T0 + 80, T0 + 40) == T0 + 80


# --- latest_global ---


def test_latest_global_is_max_over_sync_valid_cameras():
    cams = [
        CameraClock("cam1", T0 + 100, True),
        CameraClock("cam2", T0 + 60, True),
        CameraClock("cam3", T0 + 90, True),
    ]
    assert compute_t_latest_global(cams) == T0 + 100


def test_latest_global_excludes_sync_invalid_camera():
    """동기화가 무효한 카메라는 가장 늦은 시각이어도 쓰지 않는다."""
    cams = [CameraClock("cam1", T0 + 100, True), CameraClock("cam2", T0 + 900, False)]
    assert compute_t_latest_global(cams) == T0 + 100


def test_latest_global_ignores_camera_without_frames():
    cams = [CameraClock("cam1", None, True), CameraClock("cam2", T0, True)]
    assert compute_t_latest_global(cams) == T0


@pytest.mark.parametrize(
    "cams",
    [
        pytest.param([], id="no-cameras"),
        pytest.param([CameraClock("cam1", None, True)], id="no-frames"),
        pytest.param([CameraClock("cam1", T0, False)], id="all-sync-invalid"),
    ],
)
def test_latest_global_unknown(cams):
    assert compute_t_latest_global(cams) is None


def test_latest_global_is_not_per_camera():
    """카메라 A가 멈춰도 B·C·D의 시각으로 latest_global 이 진행한다."""
    before = [CameraClock(c, T0, True) for c in ("camA", "camB", "camC", "camD")]
    after = [CameraClock("camA", T0, True)] + [CameraClock(c, T0 + 300, True) for c in ("camB", "camC", "camD")]
    assert compute_t_latest_global(before) == T0
    assert compute_t_latest_global(after) == T0 + 300


# --- gap·lag·staleness ---


def test_compute_ages_follows_definition():
    ages = compute_ages(t_last_valid=T0, t_latest_global=T0 + 250, t_risk_now=T0 + 400)
    assert ages == TimeAges(gap_ms=250, lag_ms=150, staleness_ms=400)


def test_staleness_equals_gap_plus_lag():
    ages = compute_ages(T0 + 10, T0 + 130, T0 + 777)
    assert ages.staleness_ms == ages.gap_ms + ages.lag_ms == (T0 + 777) - (T0 + 10)


def test_compute_ages_rejects_negative_gap():
    with pytest.raises(ValueError, match="gap"):
        compute_ages(t_last_valid=T0 + 1, t_latest_global=T0, t_risk_now=T0 + 10)


def test_compute_ages_rejects_negative_lag():
    with pytest.raises(ValueError, match="lag"):
        compute_ages(t_last_valid=T0, t_latest_global=T0 + 10, t_risk_now=T0 + 9)


# --- 관측 상태 표 (명세 6.1): 모든 구간과 경계 ---


@pytest.mark.parametrize(
    ("gap_ms", "expected"),
    [
        (0, OBSERVED),
        (1, OBSERVED),
        (199, OBSERVED),
        (200, OBSERVED),  # 0~200ms
        (201, PREDICTED),
        (350, PREDICTED),
        (499, PREDICTED),
        (500, PREDICTED),  # 200~500ms ('500ms 초과'가 UNKNOWN)
        (501, UNKNOWN),
        (10_000, UNKNOWN),  # 500ms 초과
        (None, UNKNOWN),  # latest_global 을 모름
    ],
)
def test_observation_table(gap_ms, expected):
    assert classify_observation(gap_ms) is expected


def test_classify_rejects_negative_gap():
    with pytest.raises(ValueError):
        classify_observation(-1)


def test_thresholds_come_from_config():
    """임계값은 객체 유형·카메라 주기·구역에 따라 조정할 수 있다."""
    config = TimeModelConfig(observed_max_gap_ms=100, predicted_max_gap_ms=300)
    assert classify_observation(100, config) is OBSERVED
    assert classify_observation(101, config) is PREDICTED
    assert classify_observation(300, config) is PREDICTED
    assert classify_observation(301, config) is UNKNOWN


def test_default_config_values():
    config = TimeModelConfig()
    assert (config.observed_max_gap_ms, config.predicted_max_gap_ms) == (200, 500)
    assert (config.lag_degraded_ms, config.lag_unsafe_ms) == (600, 2000)
    assert (config.camera_stall_ms, config.min_cameras, config.max_speed_mps) == (500, 2, 2.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"observed_max_gap_ms": 600, "predicted_max_gap_ms": 500},
        {"observed_max_gap_ms": -1},
        {"max_speed_mps": -0.1},
        {"lag_degraded_ms": 3000, "lag_unsafe_ms": 2000},
        {"camera_stall_ms": -1},
        {"min_cameras": 0},
    ],
)
def test_config_rejects_inconsistent_values(kwargs):
    with pytest.raises(ValueError):
        TimeModelConfig(**kwargs)


# --- 객체 평가: 예측과 불확실성 확대 ---


def test_observed_still_expands_by_staleness():
    """OBSERVED도 최신이 아니다. gap 이 작아도 lag 만큼 불확실성을 키운다."""
    state = evaluate_track(_track(), t_latest_global=T0 + 50, t_risk_now=T0 + 550)
    assert state.observation_state is OBSERVED
    assert state.ages == TimeAges(gap_ms=50, lag_ms=500, staleness_ms=550)
    # 0.1 m + 2.0 m/s * 0.55 s
    assert state.position_xy_sigma == pytest.approx((1.2, 1.2))


def test_zero_staleness_keeps_observation():
    state = evaluate_track(_track(velocity=(1.0, 1.0, 0.0)), t_latest_global=T0, t_risk_now=T0)
    assert state.observation_state is OBSERVED
    assert state.position_xy == pytest.approx((3.0, 5.0))
    assert state.position_xy_sigma == pytest.approx((0.1, 0.1))


def test_predicted_moves_by_velocity_and_expands():
    state = evaluate_track(_track(velocity=(1.0, -0.5, 0.3)), t_latest_global=T0 + 300, t_risk_now=T0 + 400)
    assert state.observation_state is PREDICTED
    assert state.position_xy == pytest.approx((3.0 + 0.4, 5.0 - 0.2))
    assert state.position_xy_sigma == pytest.approx((0.9, 0.9))


def test_without_velocity_position_is_not_moved():
    state = evaluate_track(_track(), t_latest_global=T0 + 300, t_risk_now=T0 + 300)
    assert state.observation_state is PREDICTED
    assert state.position_xy == pytest.approx((3.0, 5.0))
    assert state.position_xy_sigma == pytest.approx((0.7, 0.7))


def test_unknown_stops_using_position():
    state = evaluate_track(_track(velocity=(1.0, 0.0, 0.0)), t_latest_global=T0 + 501, t_risk_now=T0 + 520)
    assert state.observation_state is UNKNOWN
    assert state.position_xy is None
    assert state.position_xy_sigma is None
    assert state.ages == TimeAges(gap_ms=501, lag_ms=19, staleness_ms=520)


def test_unknown_when_latest_global_missing():
    state = evaluate_track(_track(), t_latest_global=None, t_risk_now=T0 + 10)
    assert state.observation_state is UNKNOWN
    assert state.ages is None
    assert state.position_xy is None


def test_evaluate_does_not_extend_validity():
    """예측은 t_last_valid 를 갱신하지 않는다(유효기간을 늘리지 않음)."""
    track = _track()
    state = evaluate_track(track, t_latest_global=T0 + 300, t_risk_now=T0 + 400)
    assert state.t_last_valid == track.t_last_valid == T0


def test_uncertainty_grows_monotonically_with_time():
    sigmas = []
    for dt in range(0, 501, 50):
        state = evaluate_track(_track(), t_latest_global=T0 + dt, t_risk_now=T0 + dt + 20)
        sigmas.append(state.position_xy_sigma[0])
    assert sigmas == sorted(sigmas)
    assert sigmas[0] < sigmas[-1]


def test_max_speed_from_config():
    config = TimeModelConfig(max_speed_mps=0.0)
    state = evaluate_track(_track(), t_latest_global=T0 + 300, t_risk_now=T0 + 400, config=config)
    assert state.position_xy_sigma == pytest.approx((0.1, 0.1))


def test_track_input_rejects_negative_sigma():
    with pytest.raises(ValueError):
        _track(sigma=(-0.1, 0.1))


# --- 객체 상태 전환 순서 ---


def test_state_transitions_as_gap_grows():
    """사람이 계속 안 보이면 OBSERVED → PREDICTED → UNKNOWN 순서로만 바뀐다."""
    track = _track()
    states = [
        evaluate_track(track, t_latest_global=T0 + gap, t_risk_now=T0 + gap + 30).observation_state
        for gap in range(0, 1001, 40)
    ]
    order = {OBSERVED: 0, PREDICTED: 1, UNKNOWN: 2}
    assert [order[s] for s in states] == sorted(order[s] for s in states)
    assert set(states) == {OBSERVED, PREDICTED, UNKNOWN}


def test_reobservation_returns_to_observed():
    """새 유효 관측으로 t_last_valid 가 갱신되면 다시 OBSERVED 가 된다."""
    stale = evaluate_track(_track(), t_latest_global=T0 + 700, t_risk_now=T0 + 720)
    assert stale.observation_state is UNKNOWN
    renewed = _track(t_last_valid=advance_ms(T0, T0 + 700))
    fresh = evaluate_track(renewed, t_latest_global=T0 + 700, t_risk_now=T0 + 720)
    assert fresh.observation_state is OBSERVED
