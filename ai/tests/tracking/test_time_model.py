"""시간 모델 시험 (명세 6.1). 관측 상태 표의 모든 구간과 경계, gap·lag·staleness 정의를 확인한다."""

import pytest

from simai_ai.tracking.time_model import (
    SAME_CLOCK,
    CameraClock,
    ClockOffset,
    FusionTime,
    LagStatus,
    ObservationState,
    TimeAges,
    TimeModelConfig,
    TrackTimeInput,
    advance_ms,
    choose_fusion_time,
    classify_observation,
    compute_ages,
    compute_t_latest_global,
    estimate_lag,
    evaluate_track,
    require_ms,
    to_tier0_ms,
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
        {"fusion_max_lag_ms": -1},
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


# --- 노트북–Pod 시계 차이로 T_lag 보정 (명세 6.1, 3.9) ---


def test_to_tier0_ms_subtracts_offset_and_rounds():
    assert to_tier0_ms(T0, ClockOffset(300.0, 10.0)) == T0 - 300
    assert to_tier0_ms(T0, ClockOffset(-12.5, 10.0)) == T0 + 13  # 12.5 반올림
    assert to_tier0_ms(T0, ClockOffset(12.4, 10.0)) == T0 - 12


def test_estimate_lag_records_raw_and_corrected():
    # 노트북이 300ms 빠르다: 촬영 시각(노트북 시계)이 판정 시계보다 앞서 보정 전 lag 는 음수
    lag = estimate_lag(T0 + 150, T0 + 300, ClockOffset(300.0, 20.0))
    assert lag.status is LagStatus.CORRECTED
    assert lag.raw_lag_ms == -150
    assert lag.lag_ms == 150
    assert lag.uncertainty_ms == 20.0
    assert lag.upper_ms == 170.0


def test_estimate_lag_same_clock_is_unchanged():
    lag = estimate_lag(T0 + 400, T0 + 250)
    assert (lag.raw_lag_ms, lag.lag_ms, lag.uncertainty_ms, lag.status) == (150, 150, 0.0, LagStatus.SAME_CLOCK)
    assert estimate_lag(T0 + 400, T0 + 250, SAME_CLOCK) == lag
    with pytest.raises(ValueError, match="lag"):
        estimate_lag(T0, T0 + 1)


def test_estimate_lag_unknown_offset_is_left_empty():
    lag = estimate_lag(T0 + 400, T0 + 250, None)
    assert lag.status is LagStatus.OFFSET_UNKNOWN
    assert lag.raw_lag_ms == 150
    assert lag.lag_ms is None and lag.uncertainty_ms is None and lag.upper_ms is None


def test_estimate_lag_negative_within_uncertainty_is_clamped_to_zero():
    lag = estimate_lag(T0, T0 + 10, ClockOffset(0.0, 15.0))
    assert lag.status is LagStatus.CORRECTED
    assert lag.lag_ms == 0
    assert lag.upper_ms == 15.0


def test_estimate_lag_negative_beyond_uncertainty_is_inconsistent():
    lag = estimate_lag(T0, T0 + 100, ClockOffset(0.0, 15.0))
    assert lag.status is LagStatus.OFFSET_INCONSISTENT
    assert lag.lag_ms is None
    assert lag.raw_lag_ms == -100


def test_estimate_lag_rejects_other_types():
    with pytest.raises(TypeError):
        estimate_lag(T0, T0, 12.0)
    with pytest.raises(ValueError):
        ClockOffset(float("nan"), 1.0)
    with pytest.raises(ValueError):
        ClockOffset(0.0, -1.0)


def test_compute_ages_with_offset():
    ages = compute_ages(T0, T0 + 250, T0 + 100, ClockOffset(300.0, 20.0))
    assert ages == TimeAges(gap_ms=250, lag_ms=150, staleness_ms=400, lag_uncertainty_ms=20.0)
    with pytest.raises(ValueError, match="lag"):
        compute_ages(T0, T0 + 250, T0 + 100, None)


def test_evaluate_track_uses_corrected_lag_and_its_uncertainty():
    track = _track(velocity=(1.0, 0.0, 0.0))
    state = evaluate_track(track, t_latest_global=T0 + 100, t_risk_now=T0 - 200, capture_clock=ClockOffset(500.0, 50.0))
    assert state.observation_state is OBSERVED
    assert state.ages == TimeAges(gap_ms=100, lag_ms=200, staleness_ms=300, lag_uncertainty_ms=50.0)
    assert state.position_xy == pytest.approx((3.3, 5.0))
    # 불확실성 확대는 staleness + lag 불확실성 (2.0 m/s * 0.35 s)
    assert state.position_xy_sigma == pytest.approx((0.1 + 0.7, 0.1 + 0.7))


def test_evaluate_track_unknown_offset_keeps_gap_state_without_time_reference():
    """시계 차이를 모르면 관측 상태는 gap 그대로, 위치는 t_latest_global 시점까지만 예측한다(현재로 옮기지 않음)."""
    track = _track(velocity=(1.0, 0.0, 0.0))
    for clock in (None, ClockOffset(0.0, 5.0)):  # 모름, 맞지 않음(보정 후 lag -100)
        state = evaluate_track(track, t_latest_global=T0 + 100, t_risk_now=T0, capture_clock=clock)
        assert state.observation_state is OBSERVED
        assert state.gap_ms == 100 and state.ages is None and not state.time_reference
        assert state.position_xy == pytest.approx((3.1, 5.0))  # gap 100ms 만큼만
        assert state.position_xy_sigma == pytest.approx((0.1 + 0.2, 0.1 + 0.2))
    lost = evaluate_track(_track(), t_latest_global=T0 + 600, t_risk_now=T0, capture_clock=None)
    assert lost.observation_state is UNKNOWN and lost.position_xy is None
    known = evaluate_track(_track(), t_latest_global=T0 + 100, t_risk_now=T0 + 200)
    assert known.time_reference and known.gap_ms == 100
    assert not evaluate_track(_track(), t_latest_global=None, t_risk_now=T0).time_reference
    with pytest.raises(ValueError, match="gap"):
        evaluate_track(_track(T0 + 200), t_latest_global=T0 + 100, t_risk_now=T0, capture_clock=None)


# --- 융합 시각 (S15P31A101-219, docs/design/tier0-fusion-time.md) ---


def _clocks(**times):
    return [CameraClock(c, None if t is None else T0 + t, True) for c, t in times.items()]


def test_fusion_time_is_oldest_of_prompt_cameras():
    assert choose_fusion_time(_clocks(cam1=1000, cam2=960, cam3=900, cam4=None), TimeModelConfig()) == FusionTime(T0 + 900)
    assert choose_fusion_time(_clocks(cam1=None), TimeModelConfig()) is None
    assert choose_fusion_time([], TimeModelConfig()) is None


def test_fusion_lag_boundary_150_kept_151_left_out():
    """경계: t_latest_global 보다 정확히 150 ms 늦으면 넣고(외삽 없이 그 시각을 기다린다), 151 ms 면 뺀다."""
    config = TimeModelConfig()
    assert choose_fusion_time(_clocks(cam1=1000, cam2=1000, cam3=850), config) == FusionTime(T0 + 850)
    assert choose_fusion_time(_clocks(cam1=1000, cam2=990, cam3=849), config) == FusionTime(T0 + 990, ("cam3",))


def test_stalled_camera_is_not_reported_as_lagging():
    """멈춘 카메라(500 ms 초과)는 이전처럼 빠지고, 늦은 카메라 목록에는 수신 중인 카메라만 싣는다."""
    got = choose_fusion_time(_clocks(cam1=1000, cam2=980, cam3=600, cam4=499), TimeModelConfig())
    assert got == FusionTime(T0 + 980, ("cam3",))


def test_fewer_than_min_cameras_left_waits_for_the_late_ones():
    """반례: 빼고 남은 카메라가 min_cameras(2)보다 적으면 빼지 않는다. 앞선 카메라 한 대(시계가 앞선 경우 포함)에 맞춰
    서두르면 다른 카메라가 모두 빠져 다시점이 안 된다."""
    config = TimeModelConfig()
    assert choose_fusion_time(_clocks(cam1=1000, cam2=600, cam3=620), config) == FusionTime(T0 + 600)
    assert choose_fusion_time(_clocks(cam1=1000, cam2=980, cam3=600), config) == FusionTime(T0 + 980, ("cam3",))
    one = TimeModelConfig(min_cameras=1)
    assert choose_fusion_time(_clocks(cam1=1000, cam2=600), one) == FusionTime(T0 + 1000, ("cam2",))


def test_one_clock_ahead_camera_with_normal_phase_spread_pushes_normal_cameras_out():
    """리뷰 F2 반례: 되돌림은 남은 카메라가 2대 미만일 때만이다. cam1 만 +140 ms 앞서고 정상 세 대가 40 ms 위상차면 cam1·cam2 가
    남아 정상 cam3·cam4 가 빠진다(동기화 측정이 없는 동안의 제한, 감시 저하는 R7 이 드러낸다)."""
    got = choose_fusion_time(_clocks(cam1=1000, cam2=860, cam3=840, cam4=820), TimeModelConfig())
    assert got == FusionTime(T0 + 860, ("cam3", "cam4"))


def test_sync_invalid_camera_does_not_set_the_reference():
    """동기화가 무효한 카메라는 t_latest_global 처럼 기준에서 빠진다(시계가 앞선 카메라가 나머지를 늦은 쪽으로 만들지 않음)."""
    clocks = _clocks(cam2=700, cam3=690) + [CameraClock("cam1", T0 + 1000, False)]
    assert choose_fusion_time(clocks, TimeModelConfig()) == FusionTime(T0 + 690)


def test_fusion_time_does_not_depend_on_camera_order():
    clocks = _clocks(cam4=1000, cam2=980, cam1=700, cam3=650)
    got = choose_fusion_time(clocks, TimeModelConfig())
    assert got == choose_fusion_time(list(reversed(clocks)), TimeModelConfig()) == FusionTime(T0 + 980, ("cam1", "cam3"))
