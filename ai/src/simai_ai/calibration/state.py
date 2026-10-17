"""보정 상태의 운영 의미와 허용 전이 (명세 4.4, 4.3 동결, 6.2).

| 상태          | 위치 판단 | R1            | R2·R3·R4  | System Health   | 비고                 |
| UNCALIBRATED  | 불가      | -             | -         | UNSAFE_UNKNOWN  | 연결 직후, 추정 전    |
| PROVISIONAL   | 가능      | 넓은 불확실성 | 확인 필요 | DEGRADED        | AI 초기 추정·기준 미달 |
| CALIBRATED    | 가능      | 정상          | 정상      | NOMINAL         | 기준 충족, 동결       |
| DRIFTED       | 가능      | 넓은 불확실성 | 확인 필요 | DEGRADED        | 자동 재보정 시작      |

상태 enum 과 System Health 는 `tracking.system_health` 의 것을 그대로 쓴다. System Health 값은 그 모듈의
원인별 매핑과 같아야 한다(tests/calibration 이 evaluate_health 로 대조한다).
DRIFTED 의 R1~R4 동작은 명세에 없다. 틀어진 보정이므로 PROVISIONAL 과 같게 보수적으로 둔다.

전이는 보정 버전 전환(새 버전 추가)과 DRIFTED 표시뿐이다. CALIBRATED 는 동결이라 새 버전으로 바뀌지 않고,
틀어짐이 확인되어 DRIFTED 가 된 뒤에만 재보정 버전으로 바뀐다.
"""

from __future__ import annotations

from dataclasses import dataclass

from simai_ai.tracking.system_health import CalibrationState, SystemHealth

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CalibrationOperation",
    "CalibrationState",
    "OPERATIONS",
    "check_transition",
    "operation_for",
]


@dataclass(frozen=True)
class CalibrationOperation:
    """보정 상태 하나의 운영 의미 (명세 4.4 운영 열).

    position_usable: 보정으로 World 위치를 계산해 판단에 쓸 수 있는지.
    r1_wide_uncertainty: R1 을 넓은 불확실성으로 돌리는지.
    needs_confirmation_rules: 결과에 "확인 필요"를 붙이는 규칙.
    frozen: 새 보정 버전으로 바꾸지 않는지(감시만).
    auto_recalibration: 자동 재보정을 시작하는지.
    """

    state: CalibrationState
    system_health: SystemHealth
    position_usable: bool
    r1_wide_uncertainty: bool
    needs_confirmation_rules: frozenset[str]
    frozen: bool
    auto_recalibration: bool


_CONFIRM_RULES = frozenset({"R2", "R3", "R4"})

OPERATIONS: dict[CalibrationState, CalibrationOperation] = {
    CalibrationState.UNCALIBRATED: CalibrationOperation(
        state=CalibrationState.UNCALIBRATED,
        system_health=SystemHealth.UNSAFE_UNKNOWN,
        position_usable=False,
        r1_wide_uncertainty=False,
        needs_confirmation_rules=frozenset(),
        frozen=False,
        auto_recalibration=False,
    ),
    CalibrationState.PROVISIONAL: CalibrationOperation(
        state=CalibrationState.PROVISIONAL,
        system_health=SystemHealth.DEGRADED,
        position_usable=True,
        r1_wide_uncertainty=True,
        needs_confirmation_rules=_CONFIRM_RULES,
        frozen=False,
        auto_recalibration=False,
    ),
    CalibrationState.CALIBRATED: CalibrationOperation(
        state=CalibrationState.CALIBRATED,
        system_health=SystemHealth.NOMINAL,
        position_usable=True,
        r1_wide_uncertainty=False,
        needs_confirmation_rules=frozenset(),
        frozen=True,
        auto_recalibration=False,
    ),
    CalibrationState.DRIFTED: CalibrationOperation(
        state=CalibrationState.DRIFTED,
        system_health=SystemHealth.DEGRADED,
        position_usable=True,
        r1_wide_uncertainty=True,
        needs_confirmation_rules=_CONFIRM_RULES,
        frozen=False,
        auto_recalibration=True,
    ),
}

# 현재 상태 → 다음 상태. PROVISIONAL → PROVISIONAL 은 더 나은 후보 버전으로의 전환이다.
ALLOWED_TRANSITIONS: dict[CalibrationState, frozenset[CalibrationState]] = {
    CalibrationState.UNCALIBRATED: frozenset({CalibrationState.PROVISIONAL, CalibrationState.CALIBRATED}),
    CalibrationState.PROVISIONAL: frozenset(
        {CalibrationState.PROVISIONAL, CalibrationState.CALIBRATED, CalibrationState.DRIFTED}
    ),
    CalibrationState.CALIBRATED: frozenset({CalibrationState.DRIFTED}),
    CalibrationState.DRIFTED: frozenset({CalibrationState.PROVISIONAL, CalibrationState.CALIBRATED}),
}


def operation_for(state: CalibrationState | str) -> CalibrationOperation:
    return OPERATIONS[CalibrationState(state)]


def check_transition(current: CalibrationState | str, new: CalibrationState | str) -> None:
    """허용되지 않는 전이면 ValueError."""
    current, new = CalibrationState(current), CalibrationState(new)
    if new not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"보정 상태 {current} → {new} 전이는 허용되지 않는다")
