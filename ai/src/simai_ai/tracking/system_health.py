"""System Health 계산: NOMINAL / DEGRADED / UNSAFE_UNKNOWN (명세 6.2, 6.1, 6.5, 4.4, 3.2).

카메라, 시간 동기화, 보정, 게이트웨이·EC2 연결의 운영 신뢰도를 원인별로 매기고 가장 나쁜 값을 쓴다.

| 원인                                         | 상태            | 근거      |
| t_latest_global 없음(처리된 유효 프레임 없음) | UNSAFE_UNKNOWN  | 6.1, 3.2  |
| lag > lag_unsafe_ms                          | UNSAFE_UNKNOWN  | 6.1, 6.5  |
| lag > lag_degraded_ms                        | DEGRADED        | 6.1, 6.6  |
| 수신 중인 동기화 유효 카메라 < min_cameras    | UNSAFE_UNKNOWN  | 6.1, 6.2  |
| 일부 카메라 수신 중단(나머지로 관측 가능)     | DEGRADED        | 6.2, 6.5  |
| 시간 동기화 무효 카메라 있음(융합 제외)       | DEGRADED        | 6.5       |
| 보정 UNCALIBRATED                            | UNSAFE_UNKNOWN  | 4.4       |
| 보정 PROVISIONAL / DRIFTED                   | DEGRADED        | 4.4       |
| 게이트웨이(노트북) 연결 끊김                  | UNSAFE_UNKNOWN  | 6.5       |
| EC2 연결 끊김(판정·현장 경보는 계속)          | DEGRADED        | 6.5       |

현재 시각(t_risk_now)은 주입받는다. 카메라 수신 중단은 t_latest_global 대비 그 카메라의 경과로 본다.
전체가 멈추면 카메라끼리의 차이는 없지만 판정 시계가 계속 가므로 lag 로 잡힌다(명세 6.1).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from simai_ai.tracking.time_model import CameraClock, TimeModelConfig, compute_t_latest_global, require_ms


class SystemHealth(StrEnum):
    """System Health (명세 6.2). 정의 순서가 심각도 순서다."""

    NOMINAL = "NOMINAL"
    DEGRADED = "DEGRADED"
    UNSAFE_UNKNOWN = "UNSAFE_UNKNOWN"


_SEVERITY = {SystemHealth.NOMINAL: 0, SystemHealth.DEGRADED: 1, SystemHealth.UNSAFE_UNKNOWN: 2}


class CalibrationState(StrEnum):
    """보정 상태 (contracts common calibration_state, 명세 4.4)."""

    UNCALIBRATED = "UNCALIBRATED"
    PROVISIONAL = "PROVISIONAL"
    CALIBRATED = "CALIBRATED"
    DRIFTED = "DRIFTED"


class HealthReason(StrEnum):
    """System Health 를 낮춘 원인. 계약 필드가 아니라 표시·기록용 내부 값이다."""

    NO_VALID_FRAMES = "NO_VALID_FRAMES"
    LAG_UNSAFE = "LAG_UNSAFE"
    LAG_DEGRADED = "LAG_DEGRADED"
    CAMERA_COVERAGE_LOST = "CAMERA_COVERAGE_LOST"
    CAMERA_STALLED = "CAMERA_STALLED"
    SYNC_INVALID = "SYNC_INVALID"
    CALIBRATION_UNCALIBRATED = "CALIBRATION_UNCALIBRATED"
    CALIBRATION_PROVISIONAL = "CALIBRATION_PROVISIONAL"
    CALIBRATION_DRIFTED = "CALIBRATION_DRIFTED"
    GATEWAY_LINK_LOST = "GATEWAY_LINK_LOST"
    EC2_LINK_LOST = "EC2_LINK_LOST"


_REASON_HEALTH = {
    HealthReason.NO_VALID_FRAMES: SystemHealth.UNSAFE_UNKNOWN,
    HealthReason.LAG_UNSAFE: SystemHealth.UNSAFE_UNKNOWN,
    HealthReason.LAG_DEGRADED: SystemHealth.DEGRADED,
    HealthReason.CAMERA_COVERAGE_LOST: SystemHealth.UNSAFE_UNKNOWN,
    HealthReason.CAMERA_STALLED: SystemHealth.DEGRADED,
    HealthReason.SYNC_INVALID: SystemHealth.DEGRADED,
    HealthReason.CALIBRATION_UNCALIBRATED: SystemHealth.UNSAFE_UNKNOWN,
    HealthReason.CALIBRATION_PROVISIONAL: SystemHealth.DEGRADED,
    HealthReason.CALIBRATION_DRIFTED: SystemHealth.DEGRADED,
    HealthReason.GATEWAY_LINK_LOST: SystemHealth.UNSAFE_UNKNOWN,
    HealthReason.EC2_LINK_LOST: SystemHealth.DEGRADED,
}

_CALIBRATION_REASON = {
    CalibrationState.UNCALIBRATED: HealthReason.CALIBRATION_UNCALIBRATED,
    CalibrationState.PROVISIONAL: HealthReason.CALIBRATION_PROVISIONAL,
    CalibrationState.DRIFTED: HealthReason.CALIBRATION_DRIFTED,
}


@dataclass(frozen=True)
class HealthReport:
    """System Health 계산 결과.

    lag_ms 는 t_latest_global 을 모르면 None 이다.
    active_cameras: 동기화 유효하고 수신 중인 카메라. stalled_cameras: 동기화 유효하지만 수신 중단.
    sync_invalid_cameras: 동기화 무효라 융합에서 뺀 카메라.
    """

    system_health: SystemHealth
    reasons: tuple[HealthReason, ...]
    t_latest_global: int | None
    lag_ms: int | None
    active_cameras: tuple[str, ...]
    stalled_cameras: tuple[str, ...]
    sync_invalid_cameras: tuple[str, ...]


def evaluate_health(
    cameras: Iterable[CameraClock],
    calibration_state: CalibrationState,
    gateway_link_ok: bool,
    ec2_link_ok: bool,
    t_risk_now: int,
    config: TimeModelConfig = TimeModelConfig(),
) -> HealthReport:
    """System Health 전체 값을 계산한다. 원인을 모두 모으고 가장 나쁜 상태를 쓴다.

    구역별 감시 능력 저하(R7)는 여기서 계산하지 않는다.
    """
    require_ms("t_risk_now", t_risk_now)
    cameras = tuple(cameras)
    calibration_state = CalibrationState(calibration_state)
    t_latest_global = compute_t_latest_global(cameras)
    reasons: list[HealthReason] = []

    sync_invalid = tuple(c.camera_id for c in cameras if not c.sync_valid)
    active: list[str] = []
    stalled: list[str] = []
    for c in cameras:
        if not c.sync_valid:
            continue
        fresh = (
            t_latest_global is not None
            and c.last_capture is not None
            and t_latest_global - c.last_capture <= config.camera_stall_ms
        )
        (active if fresh else stalled).append(c.camera_id)

    lag_ms: int | None = None
    if t_latest_global is None:
        reasons.append(HealthReason.NO_VALID_FRAMES)
    else:
        lag_ms = t_risk_now - t_latest_global
        if lag_ms < 0:
            raise ValueError(f"lag 가 음수다: t_latest_global={t_latest_global} > t_risk_now={t_risk_now}")
        if lag_ms > config.lag_unsafe_ms:
            reasons.append(HealthReason.LAG_UNSAFE)
        elif lag_ms > config.lag_degraded_ms:
            reasons.append(HealthReason.LAG_DEGRADED)

    if len(active) < config.min_cameras:
        reasons.append(HealthReason.CAMERA_COVERAGE_LOST)
    if stalled:
        reasons.append(HealthReason.CAMERA_STALLED)
    if sync_invalid:
        reasons.append(HealthReason.SYNC_INVALID)

    if calibration_state in _CALIBRATION_REASON:
        reasons.append(_CALIBRATION_REASON[calibration_state])
    if not gateway_link_ok:
        reasons.append(HealthReason.GATEWAY_LINK_LOST)
    if not ec2_link_ok:
        reasons.append(HealthReason.EC2_LINK_LOST)

    health = max((_REASON_HEALTH[r] for r in reasons), key=_SEVERITY.__getitem__, default=SystemHealth.NOMINAL)
    return HealthReport(
        system_health=health,
        reasons=tuple(reasons),
        t_latest_global=t_latest_global,
        lag_ms=lag_ms,
        active_cameras=tuple(active),
        stalled_cameras=tuple(stalled),
        sync_invalid_cameras=sync_invalid,
    )
