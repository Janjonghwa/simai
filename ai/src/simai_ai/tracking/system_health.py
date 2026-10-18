"""System Health 계산: NOMINAL / DEGRADED / UNSAFE_UNKNOWN (명세 6.2, 6.1, 6.5, 4.4, 3.2).

카메라, 시간 동기화, 보정, 게이트웨이·EC2 연결의 운영 신뢰도를 원인별로 매기고 가장 나쁜 값을 쓴다.

| 원인                                         | 상태            | 근거      |
| t_latest_global 없음(처리된 유효 프레임 없음) | UNSAFE_UNKNOWN  | 6.1, 3.2  |
| lag > lag_unsafe_ms                          | UNSAFE_UNKNOWN  | 6.1, 6.5  |
| lag > lag_degraded_ms                        | DEGRADED        | 6.1, 6.6  |
| 처리 정체: progress_age > lag 임계값          | lag 와 같음     | 6.1, 6.5  |
| 노트북–Pod 시계 차이 모름(표본 없음·유지 상한 초과·끊김) | UNSAFE_UNKNOWN | 3.9, 6.5 |
| 시계 차이 추정과 촬영 시각이 맞지 않음        | UNSAFE_UNKNOWN  | 3.9, 6.5  |
| 시계 차이 불확실성 > clock_uncertainty_degraded_ms | DEGRADED   | 6.5       |
| 수신 중인 동기화 유효 카메라 < min_cameras    | UNSAFE_UNKNOWN  | 6.1, 6.2  |
| 일부 카메라 수신 중단(나머지로 관측 가능)     | DEGRADED        | 6.2, 6.5  |
| 시간 동기화 무효 카메라 있음(융합 제외)       | DEGRADED        | 6.5       |
| 보정 UNCALIBRATED                            | UNSAFE_UNKNOWN  | 4.4       |
| 보정 PROVISIONAL / DRIFTED                   | DEGRADED        | 4.4       |
| 게이트웨이(노트북) 연결 끊김                  | UNSAFE_UNKNOWN  | 6.5       |
| EC2 연결 끊김(판정·현장 경보는 계속)          | DEGRADED        | 6.5       |
| 재시작 뒤 재관측·시간 동기화 확보 전(RECOVERING, recovery.RecoveryHealthGate) | UNSAFE_UNKNOWN | 3.2 |

현재 시각(t_risk_now)은 주입받는다. 카메라 수신 중단은 t_latest_global 대비 그 카메라의 경과로 본다.
전체가 멈추면 카메라끼리의 차이는 없지만 판정 시계가 계속 가므로 lag 로 잡힌다(명세 6.1).

lag 는 capture_clock 으로 Tier 0 시계 기준으로 보정한다(time_model.estimate_lag).
- 보정한 lag 는 상한(lag + 불확실성)을 임계값과 비교한다. 확인할 수 없는 지연을 작다고 보지 않는다.
- 시계 차이를 모르거나 맞지 않으면 보정 후 lag 를 비우고(lag_ms None) UNSAFE_UNKNOWN 으로 둔다. 판정 시각
  기준 지연을 알 수 없으므로 판정 불가를 숨기지 않는다. 보정 전 lag(raw_lag_ms)는 서로 다른 시계의 차이라
  판정에 쓰지 않고 기록만 한다.
- 처리 정체는 시계 차이와 무관하게 ProgressWatchdog(Tier 0 단조 시계로 잰 "t_latest_global 이 마지막으로 늘어난
  뒤 경과", progress_age_ms)로 본다. 촬영은 처리보다 먼저이므로 참 lag >= progress_age 이고, 이 하한을 lag 임계값과
  비교한다. 벽시계 차이에 기대지 않으므로 offset 부호·크기와 상관없이 정체 감지가 늦어지지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from simai_ai.tracking.time_model import (
    SAME_CLOCK,
    CameraClock,
    CaptureClock,
    LagStatus,
    TimeModelConfig,
    compute_t_latest_global,
    estimate_lag,
    require_ms,
)


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
    CLOCK_OFFSET_UNKNOWN = "CLOCK_OFFSET_UNKNOWN"
    CLOCK_OFFSET_INCONSISTENT = "CLOCK_OFFSET_INCONSISTENT"
    CLOCK_OFFSET_UNCERTAIN = "CLOCK_OFFSET_UNCERTAIN"
    # 재시작 복구 중: 재관측·시간 동기화를 아직 확보하지 못했다(3.2, recovery.RecoveryHealthGate 가 붙인다)
    RECOVERING = "RECOVERING"


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
    HealthReason.CLOCK_OFFSET_UNKNOWN: SystemHealth.UNSAFE_UNKNOWN,
    HealthReason.CLOCK_OFFSET_INCONSISTENT: SystemHealth.UNSAFE_UNKNOWN,
    HealthReason.CLOCK_OFFSET_UNCERTAIN: SystemHealth.DEGRADED,
    HealthReason.RECOVERING: SystemHealth.UNSAFE_UNKNOWN,
}

_CALIBRATION_REASON = {
    CalibrationState.UNCALIBRATED: HealthReason.CALIBRATION_UNCALIBRATED,
    CalibrationState.PROVISIONAL: HealthReason.CALIBRATION_PROVISIONAL,
    CalibrationState.DRIFTED: HealthReason.CALIBRATION_DRIFTED,
}


@dataclass
class ProgressWatchdog:
    """처리 진행 감시: t_latest_global 이 마지막으로 늘어난 Tier 0 단조 시각을 기억한다 (명세 6.1, 6.5, 3.2).

    Tier 0 정상은 처리 진행(t_latest_global 증가)으로 판정한다(3.2). 시각은 단조 시계 ns 로 주입받는다
    (clock_sync.Tier0Clock.now().mono_ns). 노트북 시계와 무관하므로 시계 차이를 몰라도 쓸 수 있다.
    """

    _last_value: int | None = field(default=None, init=False)
    _last_progress_ns: int | None = field(default=None, init=False)
    _last_seen_ns: int | None = field(default=None, init=False)

    def observe(self, t_latest_global: int | None, now_mono_ns: int) -> None:
        """현재 t_latest_global 을 본다. 값이 늘었으면 진행 시각을 지금으로 옮긴다."""
        if self._last_seen_ns is not None and now_mono_ns < self._last_seen_ns:
            raise ValueError(f"단조 시계가 뒤로 갔다: {self._last_seen_ns} -> {now_mono_ns}")
        self._last_seen_ns = now_mono_ns
        if t_latest_global is None:
            return
        require_ms("t_latest_global", t_latest_global)
        if self._last_value is None or t_latest_global > self._last_value:
            self._last_value = t_latest_global
            self._last_progress_ns = now_mono_ns

    def age_ms(self, now_mono_ns: int) -> float | None:
        """마지막 진행 뒤 경과(ms). 진행을 본 적이 없으면 None."""
        if self._last_progress_ns is None:
            return None
        return max(now_mono_ns - self._last_progress_ns, 0) / 1_000_000


@dataclass(frozen=True)
class HealthReport:
    """System Health 계산 결과.

    lag_ms 는 Tier 0 시계 기준 T_lag(보정 후)다. t_latest_global 이나 시계 차이를 모르면 None 이다.
    raw_lag_ms 는 보정 전 t_risk_now - t_latest_global(시계가 다르면 음수일 수 있음), lag_uncertainty_ms 는
    lag_ms 의 불확실성 반폭(같은 시계면 0)이다. 둘 다 t_latest_global 을 모르면 None 이다.
    progress_age_ms 는 입력으로 받은 처리 정체 경과(없으면 None)다.
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
    raw_lag_ms: int | None = None
    lag_uncertainty_ms: float | None = None
    progress_age_ms: float | None = None


def evaluate_health(
    cameras: Iterable[CameraClock],
    calibration_state: CalibrationState,
    gateway_link_ok: bool,
    ec2_link_ok: bool,
    t_risk_now: int,
    config: TimeModelConfig = TimeModelConfig(),
    capture_clock: CaptureClock = SAME_CLOCK,
    progress_age_ms: float | None = None,
) -> HealthReport:
    """System Health 전체 값을 계산한다. 원인을 모두 모으고 가장 나쁜 상태를 쓴다.

    capture_clock: LIVE 는 노트북–Pod 시계 차이 추정값(ClockOffset, 모르면 None), 촬영 시각이
    판정 시계와 같은 시계면 SAME_CLOCK(기본값, SIM·REPLAY·FILE). LIVE 조립은 반드시 명시한다.
    progress_age_ms: ProgressWatchdog.age_ms. 처리 정체 감시(lag 하한). 없으면 보지 않는다.
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
    raw_lag_ms: int | None = None
    lag_uncertainty_ms: float | None = None
    if t_latest_global is None:
        reasons.append(HealthReason.NO_VALID_FRAMES)
    else:
        lag = estimate_lag(t_risk_now, t_latest_global, capture_clock)  # 같은 시계에서 음수면 ValueError
        lag_ms, raw_lag_ms, lag_uncertainty_ms = lag.lag_ms, lag.raw_lag_ms, lag.uncertainty_ms
        clock_reasons: list[HealthReason] = []
        judged: float | None = lag.upper_ms
        if judged is not None:
            if lag.uncertainty_ms is not None and lag.uncertainty_ms > config.clock_uncertainty_degraded_ms:
                clock_reasons.append(HealthReason.CLOCK_OFFSET_UNCERTAIN)
        else:
            # 서로 다른 시계의 raw lag 는 판정에 쓰지 않는다(모듈 설명)
            clock_reasons.append(
                HealthReason.CLOCK_OFFSET_INCONSISTENT
                if lag.status is LagStatus.OFFSET_INCONSISTENT
                else HealthReason.CLOCK_OFFSET_UNKNOWN
            )
        if progress_age_ms is not None:  # 참 lag 의 하한 (처리 정체)
            judged = progress_age_ms if judged is None else max(judged, progress_age_ms)
        if judged is not None and judged > config.lag_unsafe_ms:
            reasons.append(HealthReason.LAG_UNSAFE)
        elif judged is not None and judged > config.lag_degraded_ms:
            reasons.append(HealthReason.LAG_DEGRADED)
        reasons.extend(clock_reasons)

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
        raw_lag_ms=raw_lag_ms,
        lag_uncertainty_ms=lag_uncertainty_ms,
        progress_age_ms=progress_age_ms,
    )
