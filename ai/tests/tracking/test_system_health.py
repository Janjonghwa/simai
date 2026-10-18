"""System Health 시험 (명세 6.2, 6.1 사례 표, 6.5, 4.4, 3.2 재시작 복구)."""

import pytest

from simai_ai.tracking.system_health import (
    CalibrationState,
    HealthReason,
    ProgressWatchdog,
    SystemHealth,
    evaluate_health,
)
from simai_ai.tracking.time_model import (
    CameraClock,
    ClockOffset,
    ObservationState,
    TimeModelConfig,
    TrackTimeInput,
    evaluate_track,
)

T0 = 1_760_342_400_000
CAMS = ("cam1", "cam2", "cam3", "cam4")
NOMINAL, DEGRADED, UNSAFE = SystemHealth.NOMINAL, SystemHealth.DEGRADED, SystemHealth.UNSAFE_UNKNOWN


def _cams(times, sync=None):
    """카메라 4대의 last_capture(T0 기준 ms 또는 None)와 동기화 유효 여부."""
    sync = sync or {}
    return [
        CameraClock(cid, None if t is None else T0 + t, sync.get(cid, True)) for cid, t in zip(CAMS, times, strict=True)
    ]


def _health(cams, now_offset, calibration=CalibrationState.CALIBRATED, gateway=True, ec2=True, config=None):
    kwargs = {} if config is None else {"config": config}
    return evaluate_health(cams, calibration, gateway, ec2, T0 + now_offset, **kwargs)


def _track(t_last_valid):
    return TrackTimeInput("w-1", T0 + t_last_valid, (1.0, 2.0), (0.1, 0.1))


# --- 정상 ---


def test_all_nominal():
    report = _health(_cams([100, 90, 80, 100]), 200)
    assert report.system_health is NOMINAL
    assert report.reasons == ()
    assert report.t_latest_global == T0 + 100
    assert report.lag_ms == 100
    assert report.active_cameras == CAMS


# --- lag (명세 6.1, 6.6) ---


@pytest.mark.parametrize(
    ("lag", "expected", "reasons"),
    [
        (0, NOMINAL, ()),
        (600, NOMINAL, ()),
        (601, DEGRADED, (HealthReason.LAG_DEGRADED,)),
        (2000, DEGRADED, (HealthReason.LAG_DEGRADED,)),
        (2001, UNSAFE, (HealthReason.LAG_UNSAFE,)),
    ],
)
def test_lag_thresholds(lag, expected, reasons):
    report = _health(_cams([0, 0, 0, 0]), lag)
    assert report.lag_ms == lag
    assert report.system_health is expected
    assert report.reasons == reasons


def test_negative_lag_is_rejected():
    with pytest.raises(ValueError, match="lag"):
        _health(_cams([100, 100, 100, 100]), 99)


# --- 카메라 커버리지 (명세 6.2, 6.5) ---


@pytest.mark.parametrize(
    ("behind_ms", "active"),
    [(500, True), (501, False)],
)
def test_camera_stall_boundary(behind_ms, active):
    report = _health(_cams([1000, 1000, 1000, 1000 - behind_ms]), 1100)
    assert ("cam4" in report.active_cameras) is active
    assert ("cam4" in report.stalled_cameras) is not active


@pytest.mark.parametrize(
    ("times", "expected"),
    [
        pytest.param([1000, 1000, 1000, 1000], NOMINAL, id="4-active"),
        pytest.param([1000, 1000, 1000, 0], DEGRADED, id="3-active"),
        pytest.param([1000, 1000, 0, 0], DEGRADED, id="2-active"),
        pytest.param([1000, 0, 0, 0], UNSAFE, id="1-active"),
        pytest.param([1000, None, None, None], UNSAFE, id="1-active-others-never"),
    ],
)
def test_coverage(times, expected):
    report = _health(_cams(times), 1100)
    assert report.system_health is expected


def test_one_camera_down_is_not_unsafe():
    """카메라 1대가 끊겨도 나머지가 관측하면 전체를 UNSAFE_UNKNOWN 으로 만들지 않는다."""
    report = _health(_cams([1000, 1000, 1000, 0]), 1100)
    assert report.system_health is DEGRADED
    assert report.reasons == (HealthReason.CAMERA_STALLED,)
    assert report.stalled_cameras == ("cam4",)


def test_coverage_lost_reason():
    report = _health(_cams([1000, 0, 0, 0]), 1100)
    assert HealthReason.CAMERA_COVERAGE_LOST in report.reasons
    assert report.active_cameras == ("cam1",)


def test_min_cameras_from_config():
    report = _health(_cams([1000, 0, 0, 0]), 1100, config=TimeModelConfig(min_cameras=1))
    assert report.system_health is DEGRADED


def test_no_frames_is_unsafe():
    report = _health(_cams([None, None, None, None]), 0)
    assert report.system_health is UNSAFE
    assert report.t_latest_global is None
    assert report.lag_ms is None
    assert HealthReason.NO_VALID_FRAMES in report.reasons
    assert report.stalled_cameras == CAMS


# --- 시간 동기화 (명세 6.1, 6.5) ---


def test_sync_invalid_camera_is_excluded_and_degraded():
    report = _health(_cams([1000, 1000, 1000, 1500], sync={"cam4": False}), 1100)
    assert report.system_health is DEGRADED
    assert report.t_latest_global == T0 + 1000  # cam4 의 늦은 시각은 쓰지 않는다
    assert report.sync_invalid_cameras == ("cam4",)
    assert "cam4" not in report.active_cameras
    assert report.reasons == (HealthReason.SYNC_INVALID,)


def test_sync_invalid_majority_is_unsafe():
    report = _health(_cams([1000, 1000, 1000, 1000], sync={"cam2": False, "cam3": False, "cam4": False}), 1100)
    assert report.system_health is UNSAFE
    assert HealthReason.CAMERA_COVERAGE_LOST in report.reasons


def test_all_sync_invalid_is_unsafe():
    report = _health(_cams([1000] * 4, sync={c: False for c in CAMS}), 1100)
    assert report.system_health is UNSAFE
    assert report.t_latest_global is None
    assert HealthReason.NO_VALID_FRAMES in report.reasons


# --- 보정 상태 (명세 4.4) ---


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (CalibrationState.UNCALIBRATED, UNSAFE),
        (CalibrationState.PROVISIONAL, DEGRADED),
        (CalibrationState.CALIBRATED, NOMINAL),
        (CalibrationState.DRIFTED, DEGRADED),
    ],
)
def test_calibration_state(state, expected):
    report = _health(_cams([100] * 4), 200, calibration=state)
    assert report.system_health is expected


def test_calibration_state_accepts_contract_string():
    report = _health(_cams([100] * 4), 200, calibration="DRIFTED")
    assert report.reasons == (HealthReason.CALIBRATION_DRIFTED,)


def test_unknown_calibration_string_is_rejected():
    with pytest.raises(ValueError):
        _health(_cams([100] * 4), 200, calibration="FINE")


# --- 게이트웨이·EC2 연결 (명세 6.5) ---


def test_gateway_link_lost_is_unsafe():
    report = _health(_cams([100] * 4), 200, gateway=False)
    assert report.system_health is UNSAFE
    assert report.reasons == (HealthReason.GATEWAY_LINK_LOST,)


def test_ec2_link_lost_is_degraded():
    """EC2 만 끊기면 판정과 현장 경보는 계속되므로 UNSAFE_UNKNOWN 이 아니다."""
    report = _health(_cams([100] * 4), 200, ec2=False)
    assert report.system_health is DEGRADED
    assert report.reasons == (HealthReason.EC2_LINK_LOST,)


def test_worst_reason_wins_and_all_reasons_kept():
    report = _health(_cams([1000, 1000, 1000, 0]), 1700, calibration=CalibrationState.UNCALIBRATED, ec2=False)
    assert report.system_health is UNSAFE
    assert set(report.reasons) == {
        HealthReason.LAG_DEGRADED,
        HealthReason.CAMERA_STALLED,
        HealthReason.CALIBRATION_UNCALIBRATED,
        HealthReason.EC2_LINK_LOST,
    }


# --- 명세 6.1 사례 표 ---


def test_case_slow_processing():
    """사람 정상 검출, 처리만 느림 → 객체 OBSERVED, lag 기준 초과 시 DEGRADED."""
    cams = _cams([1000] * 4)
    report = _health(cams, 1800)
    state = evaluate_track(_track(980), report.t_latest_global, T0 + 1800)
    assert state.observation_state is ObservationState.OBSERVED
    assert report.system_health is DEGRADED
    assert report.reasons == (HealthReason.LAG_DEGRADED,)


def test_case_person_missed_300ms():
    """영상 정상, 사람이 300ms 미검출 → 객체 PREDICTED, NOMINAL 가능."""
    cams = _cams([1000] * 4)
    report = _health(cams, 1050)
    state = evaluate_track(_track(700), report.t_latest_global, T0 + 1050)
    assert state.observation_state is ObservationState.PREDICTED
    assert report.system_health is NOMINAL


def test_case_camera_link_down():
    """카메라 통신 중단 → 그 카메라만 보던 사람은 예측 후 UNKNOWN, 커버리지에 따라 DEGRADED 또는 UNSAFE_UNKNOWN.

    cam1 만 보던 사람이 T0+1000 에 마지막으로 관측되고 cam1 이 그때 멈춘다.
    """
    states, healths = [], []
    for t in (1100, 1300, 1600):
        cams = _cams([1000, t, t, t])
        report = _health(cams, t + 50)
        states.append(evaluate_track(_track(1000), report.t_latest_global, T0 + t + 50).observation_state)
        healths.append(report.system_health)
    assert states == [ObservationState.OBSERVED, ObservationState.PREDICTED, ObservationState.UNKNOWN]
    assert healths == [NOMINAL, NOMINAL, DEGRADED]  # cam1 이 500ms 넘게 뒤처지면 수신 중단, 3대 남음

    # 3대가 끊기면 다시점 관측이 불가능하므로 UNSAFE_UNKNOWN
    report = _health(_cams([1000, 1000, 1000, 1600]), 1650)
    assert report.system_health is UNSAFE


def test_case_all_video_stopped():
    """영상이 모두 멈춰도 판정 시계는 진행해 lag 가 커지고, staleness 로 불확실성이 커진다."""
    cams = _cams([1000] * 4)
    sigmas = []
    healths = []
    for now in (1100, 1700, 3100):
        report = _health(cams, now)
        state = evaluate_track(_track(1000), report.t_latest_global, T0 + now)
        assert state.observation_state is ObservationState.OBSERVED  # gap 은 그대로
        sigmas.append(state.position_xy_sigma[0])
        healths.append(report.system_health)
    assert healths == [NOMINAL, DEGRADED, UNSAFE]
    assert sigmas == sorted(sigmas) and sigmas[0] < sigmas[-1]


# --- 재시작 복구 (명세 3.2, G3) ---


def test_restart_recovery_sequence():
    """재관측 전 UNSAFE_UNKNOWN → 일부 유효 관측 후 DEGRADED → 정상화 후 NOMINAL. 복원 Track 은 OBSERVED 가 아니다."""
    restored = _track(-5000)  # 체크포인트의 마지막 유효 시각

    # 재시작 직후: 처리된 프레임 없음
    report = _health(_cams([None] * 4), 0)
    assert report.system_health is UNSAFE
    assert evaluate_track(restored, report.t_latest_global, T0).observation_state is ObservationState.UNKNOWN

    # 일부 카메라 재관측
    report = _health(_cams([100, 100, None, None]), 150)
    assert report.system_health is DEGRADED
    assert evaluate_track(restored, report.t_latest_global, T0 + 150).observation_state is ObservationState.UNKNOWN

    # 전체 복구, 아직 그 사람은 재관측 전
    report = _health(_cams([300] * 4), 350)
    assert report.system_health is NOMINAL
    assert evaluate_track(restored, report.t_latest_global, T0 + 350).observation_state is ObservationState.UNKNOWN

    # 재관측으로 t_last_valid 가 갱신된 뒤에만 OBSERVED
    assert evaluate_track(_track(300), report.t_latest_global, T0 + 350).observation_state is ObservationState.OBSERVED


# --- 노트북–Pod 시계 차이 (명세 3.9, 6.1, 6.5) ---


def _clock_health(now_offset, capture_clock, config=None):
    kwargs = {} if config is None else {"config": config}
    return evaluate_health(
        _cams([100] * 4),
        CalibrationState.CALIBRATED,
        True,
        True,
        T0 + now_offset,
        capture_clock=capture_clock,
        **kwargs,
    )


def test_corrected_lag_allows_negative_raw_lag():
    # 노트북이 300ms 빠르면 보정 전 lag 는 음수지만 오류가 아니다
    report = _clock_health(0, ClockOffset(300.0, 20.0))
    assert report.raw_lag_ms == -100
    assert report.lag_ms == 200
    assert report.lag_uncertainty_ms == 20.0
    assert report.system_health is NOMINAL


def test_corrected_lag_removes_false_degraded():
    # 노트북이 500ms 늦으면 보정 전 lag 700ms(DEGRADED)지만 실제는 200ms
    report = _clock_health(800, ClockOffset(-500.0, 20.0))
    assert report.raw_lag_ms == 700
    assert report.lag_ms == 200
    assert report.system_health is NOMINAL


@pytest.mark.parametrize(
    ("lag", "uncertainty", "expected", "reasons"),
    [
        (560, 40.0, NOMINAL, ()),
        (561, 40.0, DEGRADED, (HealthReason.LAG_DEGRADED,)),  # 상한 601 > 600
        (1960, 40.0, DEGRADED, (HealthReason.LAG_DEGRADED,)),
        (1961, 40.0, UNSAFE, (HealthReason.LAG_UNSAFE,)),
    ],
)
def test_lag_thresholds_use_upper_bound(lag, uncertainty, expected, reasons):
    report = _clock_health(100 + lag, ClockOffset(0.0, uncertainty))
    assert report.lag_ms == lag
    assert report.system_health is expected
    assert report.reasons == reasons


def test_large_clock_uncertainty_is_degraded():
    assert _clock_health(200, ClockOffset(0.0, 100.0)).system_health is NOMINAL
    report = _clock_health(200, ClockOffset(0.0, 100.5))
    assert report.system_health is DEGRADED
    assert report.reasons == (HealthReason.CLOCK_OFFSET_UNCERTAIN,)
    config = TimeModelConfig(clock_uncertainty_degraded_ms=200.0)
    assert _clock_health(200, ClockOffset(0.0, 150.0), config).system_health is NOMINAL


def test_unknown_offset_is_unsafe_and_lag_left_empty():
    """시계 차이를 모르면 판정 시각 기준 지연을 알 수 없다: 판정 불가를 숨기지 않는다(UNSAFE_UNKNOWN)."""
    report = _clock_health(200, None)
    assert report.system_health is UNSAFE
    assert report.reasons == (HealthReason.CLOCK_OFFSET_UNKNOWN,)
    assert report.lag_ms is None and report.lag_uncertainty_ms is None
    assert report.raw_lag_ms == 100  # 기록만 한다


def test_raw_lag_is_not_used_for_judgement():
    # 노트북이 37s 늦으면 raw lag 는 크지만 판정에 쓰지 않는다(LAG_* 원인 없음)
    report = _clock_health(100 + 37_100, None)
    assert report.reasons == (HealthReason.CLOCK_OFFSET_UNKNOWN,)


def test_inconsistent_offset_is_unsafe_without_error():
    report = _clock_health(0, ClockOffset(0.0, 10.0))  # 보정 후 lag -100, 불확실성 10
    assert report.system_health is UNSAFE
    assert report.reasons == (HealthReason.CLOCK_OFFSET_INCONSISTENT,)
    assert report.lag_ms is None and report.raw_lag_ms == -100


@pytest.mark.parametrize("laptop_offset", [37_000.0, -37_000.0, 300.0, 0.0])
@pytest.mark.parametrize("capture_known", [True, False])
@pytest.mark.parametrize(("stall_ms", "lag_reason"), [(0, None), (700, "LAG_DEGRADED"), (2500, "LAG_UNSAFE")])
def test_stall_watchdog_is_independent_of_offset(laptop_offset, capture_known, stall_ms, lag_reason):
    """영상 전체 정체를 Tier 0 단조 시계로 감시한다. 시계 차이의 부호·크기, 시계 차이를 아는지와 무관하다."""
    true_lag = 100  # 정체 전 처리 지연
    dog = ProgressWatchdog()
    mono = 5_000_000_000
    capture = T0 + int(laptop_offset)  # 노트북 시계 촬영 시각
    dog.observe(capture, mono)
    mono += stall_ms * 1_000_000
    dog.observe(capture, mono)  # 정체: 값이 늘지 않는다
    t_risk_now = T0 + true_lag + stall_ms  # Tier 0 시계
    clock = ClockOffset(laptop_offset, 20.0) if capture_known else None
    cams = [CameraClock(c, capture, True) for c in CAMS]
    report = evaluate_health(
        cams,
        CalibrationState.CALIBRATED,
        True,
        True,
        t_risk_now,
        capture_clock=clock,
        progress_age_ms=dog.age_ms(mono),
    )
    assert report.progress_age_ms == stall_ms
    lag_reasons = [r for r in report.reasons if r.startswith("LAG_")]
    if capture_known:
        # 보정 lag 상한(100 + stall + 20)과 정체 하한 중 큰 값
        upper = true_lag + stall_ms + 20
        expected = "LAG_UNSAFE" if upper > 2000 else "LAG_DEGRADED" if upper > 600 else None
        assert lag_reasons == ([] if expected is None else [expected])
    else:
        assert lag_reasons == ([] if lag_reason is None else [lag_reason])
        assert HealthReason.CLOCK_OFFSET_UNKNOWN in report.reasons


def test_progress_watchdog():
    dog = ProgressWatchdog()
    assert dog.age_ms(0) is None
    dog.observe(None, 0)
    assert dog.age_ms(10) is None
    dog.observe(T0, 1_000_000)
    dog.observe(T0 - 50, 2_000_000)  # 늦게 온 작은 값은 진행이 아니다
    assert dog.age_ms(3_000_000) == 2.0
    dog.observe(T0 + 40, 3_000_000)
    assert dog.age_ms(3_500_000) == 0.5
    with pytest.raises(ValueError, match="단조"):
        dog.observe(T0 + 80, 1)


def test_same_clock_default_reports_raw_lag():
    report = _health(_cams([100] * 4), 300)
    assert (report.lag_ms, report.raw_lag_ms, report.lag_uncertainty_ms) == (200, 200, 0.0)


# --- 구성 묶음 없음 (S15P31A101-171) ---


def test_no_active_config_is_unsafe():
    """적용된 구성 묶음이 없으면 위험 판정을 하지 못하므로 다른 조건이 모두 정상이어도 UNSAFE_UNKNOWN 이다."""
    report = evaluate_health(_cams([100] * 4), CalibrationState.CALIBRATED, True, True, T0 + 200, config_active=False)
    assert report.system_health is UNSAFE
    assert report.reasons == (HealthReason.NO_ACTIVE_CONFIG,)


def test_config_active_default_keeps_previous_behaviour():
    assert _health(_cams([100] * 4), 200).system_health is NOMINAL
