"""보정 상태 운영 의미·전이 시험 (명세 4.4, 4.3 동결). System Health 매핑은 tracking.system_health 와 대조한다."""

import pytest

from simai_ai.calibration import CalibrationState, check_transition, operation_for
from simai_ai.calibration.state import ALLOWED_TRANSITIONS, OPERATIONS
from simai_ai.tracking.system_health import SystemHealth, evaluate_health
from simai_ai.tracking.time_model import CameraClock

T0 = 1_760_342_400_000
S = CalibrationState


def _health_with_only_calibration(state):
    """보정 외의 원인이 없는 조건(카메라 4대 정상, 연결 정상, lag 0)에서 System Health."""
    cams = [CameraClock(f"cam{i}", T0, True) for i in range(1, 5)]
    return evaluate_health(cams, state, True, True, T0).system_health


@pytest.mark.parametrize("state", list(S))
def test_system_health_matches_tracking(state):
    assert operation_for(state).system_health is _health_with_only_calibration(state)


def test_operations_table():
    assert set(OPERATIONS) == set(S)
    unc, prov, cal, drift = (operation_for(s) for s in (S.UNCALIBRATED, S.PROVISIONAL, S.CALIBRATED, S.DRIFTED))
    assert unc.system_health is SystemHealth.UNSAFE_UNKNOWN and not unc.position_usable
    assert prov.system_health is SystemHealth.DEGRADED and prov.r1_wide_uncertainty
    assert prov.needs_confirmation_rules == {"R2", "R3", "R4"}
    assert cal.system_health is SystemHealth.NOMINAL and cal.frozen and not cal.needs_confirmation_rules
    assert drift.system_health is SystemHealth.DEGRADED and drift.auto_recalibration and not drift.frozen


def test_operation_for_accepts_string():
    assert operation_for("CALIBRATED").state is S.CALIBRATED


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (S.UNCALIBRATED, S.PROVISIONAL),
        (S.UNCALIBRATED, S.CALIBRATED),
        (S.PROVISIONAL, S.PROVISIONAL),
        (S.PROVISIONAL, S.CALIBRATED),
        (S.PROVISIONAL, S.DRIFTED),
        (S.CALIBRATED, S.DRIFTED),
        (S.DRIFTED, S.PROVISIONAL),
        (S.DRIFTED, S.CALIBRATED),
    ],
)
def test_allowed_transitions(current, new):
    check_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (S.CALIBRATED, S.CALIBRATED),  # 동결
        (S.CALIBRATED, S.PROVISIONAL),
        (S.CALIBRATED, S.UNCALIBRATED),
        (S.UNCALIBRATED, S.DRIFTED),
        (S.DRIFTED, S.DRIFTED),
        (S.PROVISIONAL, S.UNCALIBRATED),
    ],
)
def test_rejected_transitions(current, new):
    with pytest.raises(ValueError):
        check_transition(current, new)


def test_no_transition_back_to_uncalibrated():
    assert all(S.UNCALIBRATED not in targets for targets in ALLOWED_TRANSITIONS.values())
