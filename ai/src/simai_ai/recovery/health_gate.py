"""재시작 직후 System Health 하한 (명세 3.2 재시작 복구, 6.2, 부록 D-G3-09, 15.1 G3).

복구 중 System Health 는 재관측·시간 동기화가 확보되기 전 UNSAFE_UNKNOWN 이다(3.2). evaluate_health 도 처리된 유효
프레임이 없거나(NO_VALID_FRAMES) 시계 차이를 모르면 UNSAFE_UNKNOWN 을 내지만, 조립 실수(체크포인트의 촬영 시각을
카메라 상태에 되살림, LIVE 인데 capture_clock 기본값 SAME_CLOCK)가 있으면 재시작 직후에도 NOMINAL 이 될 수 있다.
이 게이트는 그런 실수와 상관없이 아래 두 조건이 한 보고에서 함께 확인될 때까지 UNSAFE_UNKNOWN(RECOVERING)을 덧씌운다.

- 재관측: 재시작 뒤 새 유효 프레임을 처리했다. 게이트가 처음 본 t_latest_global(기준값)보다 큰 값이 나와야 한다
  (재시작 뒤 촬영 시각이 실제로 앞으로 간 증거, 리뷰 R1). 그 보고에서 수신 중인 동기화 유효 카메라가 1대 이상이고
  지연이 UNSAFE 기준 안이어야 한다. 체크포인트의 촬영 시각을 카메라 상태에 되살린 것만으로는(최근 시각이어도) 값이
  앞으로 가지 않으므로 풀리지 않는다. 그 대가로 해제가 프레임 하나(초기 100ms)만큼 늦다.
- 시간 동기화: 노트북–Pod 시계 차이 추정값이 있다(capture_clock 이 ClockOffset). SAME_CLOCK·None 은 확보가 아니다.
  보고에 시계 차이 모름·불일치 원인이 있거나 보정 후 lag 를 모르면 확보가 아니다.

한 번 풀리면 다시 묶지 않는다(그 뒤의 저하는 evaluate_health 가 원인별로 낸다). 일부 관측만 있으면 evaluate_health 가
DEGRADED, 커버리지·보정·지연이 정상이면 NOMINAL 을 낸다(3.2).
"""

import threading
from dataclasses import replace

from simai_ai.tracking.system_health import HealthReason, HealthReport, SystemHealth
from simai_ai.tracking.time_model import CaptureClock, ClockOffset

_CLOCK_UNKNOWN = frozenset({HealthReason.CLOCK_OFFSET_UNKNOWN, HealthReason.CLOCK_OFFSET_INCONSISTENT})


class RecoveryHealthGate:
    """Tier 0 프로세스 시작 때 하나 만든다(재시작이든 첫 시작이든). 풀리기 전에는 늘 UNSAFE_UNKNOWN."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._released = False
        self._baseline: int | None = None  # 처음 본 t_latest_global

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def _reobserved(self, report: HealthReport) -> bool:
        t = report.t_latest_global
        if t is not None and self._baseline is None:
            self._baseline = t
        return (
            t is not None
            and t > self._baseline
            and bool(report.active_cameras)
            and HealthReason.NO_VALID_FRAMES not in report.reasons
            and HealthReason.LAG_UNSAFE not in report.reasons
        )

    @staticmethod
    def _clock_synced(report: HealthReport, capture_clock: CaptureClock) -> bool:
        return (
            isinstance(capture_clock, ClockOffset)
            and report.lag_ms is not None
            and not (_CLOCK_UNKNOWN & set(report.reasons))
        )

    def apply(self, report: HealthReport, *, capture_clock: CaptureClock) -> HealthReport:
        """evaluate_health 결과에 복구 하한을 덧씌운다. capture_clock 은 그 보고를 만들 때 넘긴 값 그대로."""
        with self._lock:
            if not self._released and self._reobserved(report) and self._clock_synced(report, capture_clock):
                self._released = True
            if self._released:
                return report
        reasons = report.reasons
        if HealthReason.RECOVERING not in reasons:
            reasons = (*reasons, HealthReason.RECOVERING)
        return replace(report, system_health=SystemHealth.UNSAFE_UNKNOWN, reasons=reasons)
