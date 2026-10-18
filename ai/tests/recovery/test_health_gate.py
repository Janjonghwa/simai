"""재시작 직후 System Health (명세 3.2: 재관측·시간 동기화 확보 전 UNSAFE_UNKNOWN, 부록 D-G3-09, 15.1 G3)."""

from simai_ai.recovery import RecoveryHealthGate
from simai_ai.tracking.system_health import CalibrationState, HealthReason, SystemHealth, evaluate_health
from simai_ai.tracking.time_model import SAME_CLOCK, CameraClock, ClockOffset

NOW = 1_760_342_400_000
CAMS = ("cam1", "cam2", "cam3", "cam4")
OFFSET = ClockOffset(offset_ms=0.0, uncertainty_ms=5.0)


def _report(last_capture, capture_clock, cams=CAMS, now=NOW):
    clocks = [CameraClock(c, last_capture, True) for c in cams]
    return evaluate_health(clocks, CalibrationState.CALIBRATED, True, True, now, capture_clock=capture_clock)


def _frames(gate, capture_clock, ticks=3, cams=CAMS, start=NOW):
    """100ms 마다 새 프레임(촬영 시각이 앞으로 감)을 처리한 보고를 차례로 게이트에 넣는다."""
    out = []
    for k in range(ticks):
        now = start + 100 * k
        out.append(gate.apply(_report(now - 50, capture_clock, cams, now), capture_clock=capture_clock))
    return out


def test_before_any_frame_is_unsafe():
    gate = RecoveryHealthGate()
    out = gate.apply(_report(None, OFFSET), capture_clock=OFFSET)
    assert out.system_health is SystemHealth.UNSAFE_UNKNOWN and HealthReason.RECOVERING in out.reasons
    assert not gate.released


def test_frames_without_clock_sync_stay_unsafe_even_if_report_says_nominal():
    """반례(조립 실수): LIVE 인데 capture_clock 을 SAME_CLOCK 으로 넘겨 evaluate_health 가 NOMINAL 을 낸다.
    시계 차이 추정이 없으면 시간 동기화 확보가 아니므로 UNSAFE_UNKNOWN 이다."""
    for clock in (SAME_CLOCK, None):
        gate = RecoveryHealthGate()
        outs = _frames(gate, clock, ticks=5)
        assert all(o.system_health is SystemHealth.UNSAFE_UNKNOWN for o in outs)
        assert all(HealthReason.RECOVERING in o.reasons for o in outs) and not gate.released
    assert _report(NOW - 50, SAME_CLOCK).system_health is SystemHealth.NOMINAL  # 게이트가 없으면 NOMINAL 이었다


def test_restored_capture_time_without_new_frames_is_not_reobservation():
    """반례(조립 실수): 체크포인트의 마지막 촬영 시각을 카메라 상태에 되살렸다. 오래된 시각(lag UNSAFE)이든 최근
    시각(100ms 전, 리뷰 R1 재현)이든 새 프레임이 없으면 촬영 시각이 앞으로 가지 않으므로 풀리지 않는다."""
    for restored in (NOW - 10_000, NOW - 100):
        gate = RecoveryHealthGate()
        for k in range(5):  # 판정 시계만 가고 촬영 시각은 그대로
            out = gate.apply(_report(restored, OFFSET, now=NOW + 10 * k), capture_clock=OFFSET)
            assert out.system_health is SystemHealth.UNSAFE_UNKNOWN and not gate.released
        fresh = gate.apply(_report(NOW + 100, OFFSET, now=NOW + 150), capture_clock=OFFSET)  # 새 프레임
        assert fresh.system_health is SystemHealth.NOMINAL and gate.released


def test_released_once_reobserved_and_clock_synced_then_follows_report():
    gate = RecoveryHealthGate()
    first, partial = _frames(gate, OFFSET, ticks=2, cams=("cam1",))  # 1대만: 커버리지 부족(원래 UNSAFE)
    assert HealthReason.RECOVERING in first.reasons  # 첫 보고는 기준값일 뿐 재관측 증거가 아니다
    assert partial.system_health is SystemHealth.UNSAFE_UNKNOWN and gate.released
    assert HealthReason.RECOVERING not in partial.reasons  # 남은 UNSAFE 는 보고의 원인(커버리지)이다
    (ok,) = _frames(gate, OFFSET, ticks=1, start=NOW + 1000)
    assert ok.system_health is SystemHealth.NOMINAL
    # 풀린 뒤에는 다시 묶지 않는다(이후 저하는 evaluate_health 가 원인별로 낸다)
    lost = gate.apply(_report(NOW + 1950, None, now=NOW + 2000), capture_clock=None)
    assert lost.system_health is SystemHealth.UNSAFE_UNKNOWN and HealthReason.RECOVERING not in lost.reasons


def test_advancing_but_stale_frames_are_not_reobservation():
    """반례: 재시작 전에 쌓인 오래된 프레임이 뒤늦게 차례로 처리돼 촬영 시각은 앞으로 가지만 지연이 UNSAFE 기준을
    넘는다. 지금 현장을 본 것이 아니므로 풀지 않는다."""
    gate = RecoveryHealthGate()
    for k in range(5):
        out = gate.apply(_report(NOW - 10_000 + 100 * k, OFFSET, now=NOW + 10 * k), capture_clock=OFFSET)
        assert out.system_health is SystemHealth.UNSAFE_UNKNOWN and not gate.released
