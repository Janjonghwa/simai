"""시간 모델: latest_global, 객체별 gap·lag·staleness, 관측 상태 전환과 불확실성 확대 (명세 6.1).

    T_gap       = t_latest_global - t_last_valid
    T_lag       = t_risk_now      - t_latest_global
    T_staleness = T_gap + T_lag

- 모든 시각은 UTC int64 ms다. 현재 시각(t_risk_now)은 호출자가 주입한다(직접 읽지 않음).
- 촬영 시각은 노트북 시계, t_risk_now 는 Tier 0 시계다. LIVE 에서는 노트북–Pod 시계 차이 추정값
  (ClockOffset, clock_sync.py)으로 t_latest_global 을 Tier 0 시계로 옮겨 T_lag 를 계산하고 그 불확실성을 함께 쓴다
  (명세 3.9, 6.1). gap 은 두 시각이 모두 노트북 시계라 보정하지 않는다.
  SIM·REPLAY·FILE 처럼 촬영 시각이 이미 판정 시계와 같은 시계면 SAME_CLOCK(기본값)을 쓴다.
- 관측 상태는 gap으로 정한다. lag는 System Health(system_health.py)가 본다.
- 위치 예측과 불확실성 확대는 staleness로 한다. OBSERVED도 최신이라는 뜻이 아니므로 함께 확대한다.
- 예측 위치를 관측처럼 다루지 않는다. 이 모듈은 t_last_valid를 바꾸지 않고, 시각은 advance_ms로만 앞으로 간다.

필드 이름은 contracts/messages/world_state.schema.json 을 따른다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Final, TypeAlias


class ObservationState(StrEnum):
    """객체 관측 상태 (contracts common observation_state, 명세 6.1)."""

    OBSERVED = "OBSERVED"
    PREDICTED = "PREDICTED"
    UNKNOWN = "UNKNOWN"


def require_ms(name: str, value: int) -> int:
    """UTC int64 ms 시각인지 확인한다. float·bool·음수는 받지 않는다."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name}: 시각은 int ms 여야 한다 (받은 값 {value!r})")
    if value < 0:
        raise ValueError(f"{name}: 시각은 0 이상이어야 한다 (받은 값 {value})")
    return value


def advance_ms(previous: int | None, candidate: int) -> int:
    """시각을 앞으로만 옮긴다. 지연 프레임·역순 도착이 시각을 되돌리지 않게 한다.

    카메라별 마지막 처리 촬영 시각과 객체의 t_last_valid 갱신에 쓴다.
    t_last_valid 는 실제 유효 관측으로만 갱신한다(예측으로 유효기간을 늘리지 않음).
    """
    require_ms("candidate", candidate)
    if previous is None:
        return candidate
    return max(require_ms("previous", previous), candidate)


@dataclass(frozen=True)
class CameraClock:
    """카메라 한 대의 시각 상태.

    last_capture: 실제 처리된 최신 촬영 시각(UTC ms). 아직 처리한 프레임이 없으면 None.
    sync_valid: 시간 동기화가 유효한지. 판정은 상류(동기화 측정)가 하고 여기서는 받기만 한다.
    """

    camera_id: str
    last_capture: int | None
    sync_valid: bool

    def __post_init__(self) -> None:
        if self.last_capture is not None:
            require_ms("last_capture", self.last_capture)


def compute_t_latest_global(cameras: Iterable[CameraClock]) -> int | None:
    """시간 동기화가 유효한 카메라들 중 실제 처리된 최신 촬영 시각.

    카메라별 기준이 아니라 전체 기준이다. 카메라 A가 멈춰도 B·C·D의 시각으로 gap이 커진다.
    동기화가 무효한 카메라는 빼고, 남은 카메라에 처리된 프레임이 없으면 None(모름)이다.
    """
    times = [c.last_capture for c in cameras if c.sync_valid and c.last_capture is not None]
    return max(times) if times else None


@dataclass(frozen=True)
class ClockOffset:
    """노트북 시계 - Tier 0 시계 차이 추정값 (health.laptop_clock_offset 의 offset_ms, uncertainty_ms).

    offset_ms: 노트북이 빠르면 양수(ms). uncertainty_ms: 불확실성 반폭(ms), 참값이 offset_ms ± uncertainty_ms 안.
    """

    offset_ms: float
    uncertainty_ms: float

    def __post_init__(self) -> None:
        for name in ("offset_ms", "uncertainty_ms"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"{name}: 유한한 수여야 한다 (받은 값 {v!r})")
        if self.uncertainty_ms < 0:
            raise ValueError(f"uncertainty_ms 는 0 이상이어야 한다 (받은 값 {self.uncertainty_ms})")


class ClockDomain(Enum):
    """촬영 시각이 판정 시계(Tier 0)와 같은 시계라는 표시. SIM·REPLAY·FILE, 같은 호스트."""

    SAME = "SAME"


SAME_CLOCK: Final = ClockDomain.SAME

# 촬영 시각 시계와 Tier 0 시계의 관계.
#   SAME_CLOCK   같은 시계(보정 없음)
#   ClockOffset  노트북 시계, 차이 추정값으로 보정
#   None         노트북 시계인데 차이를 모름(표본 없음·오래됨·노트북 연결 끊김). 0 으로 채우지 않는다
CaptureClock: TypeAlias = ClockOffset | ClockDomain | None


def to_tier0_ms(t_laptop_ms: int, offset: ClockOffset) -> int:
    """노트북 시계 시각을 Tier 0 시계로 옮긴다: t - offset_ms (반올림, 오차 0.5ms 이하)."""
    require_ms("t_laptop_ms", t_laptop_ms)
    return math.floor(t_laptop_ms - offset.offset_ms + 0.5)


class LagStatus(StrEnum):
    """T_lag 를 어떻게 얻었는지."""

    SAME_CLOCK = "SAME_CLOCK"  # 같은 시계, 보정 없음
    CORRECTED = "CORRECTED"  # 시계 차이로 보정
    OFFSET_UNKNOWN = "OFFSET_UNKNOWN"  # 시계 차이를 몰라 보정 후 lag 를 모름
    OFFSET_INCONSISTENT = "OFFSET_INCONSISTENT"  # 보정 후 lag 가 불확실성 밖으로 음수(추정 뒤 시계 점프 등)


@dataclass(frozen=True)
class LagEstimate:
    """T_lag 의 보정 전후 값.

    raw_lag_ms: t_risk_now - t_latest_global (보정 전, 시계가 다르면 음수일 수 있음). 기록·비교용.
    lag_ms: Tier 0 시계 기준 T_lag (보정 후). 모르면 None. 보정 후 음수가 불확실성 안이면 0 으로 자른다
        (참 lag 는 0 이상이고 [0, lag + uncertainty] 안에 있으므로 상한은 그대로 보수적이다).
    uncertainty_ms: lag_ms 의 불확실성 반폭. SAME_CLOCK 이면 0, 모르면 None.
    """

    raw_lag_ms: int
    lag_ms: int | None
    uncertainty_ms: float | None
    status: LagStatus

    @property
    def upper_ms(self) -> float | None:
        """T_lag 상한(lag + 불확실성). 임계값 비교에 쓴다. 모르면 None."""
        if self.lag_ms is None or self.uncertainty_ms is None:
            return None
        return self.lag_ms + self.uncertainty_ms


def estimate_lag(t_risk_now: int, t_latest_global: int, capture_clock: CaptureClock = SAME_CLOCK) -> LagEstimate:
    """T_lag = t_risk_now - t_latest_global 를 Tier 0 시계 기준으로 계산한다 (명세 6.1, 3.9).

    - SAME_CLOCK: 보정 없음. lag 가 음수면 입력 불일치라 ValueError (판정 시계가 촬영 시각보다 뒤).
    - ClockOffset: t_latest_global 을 to_tier0_ms 로 옮겨 계산한다. 결과가 -uncertainty 보다 작으면
      시계 차이 추정이 맞지 않는 것(추정 뒤 시계 점프 등)이라 lag_ms 를 비우고 OFFSET_INCONSISTENT 로 둔다.
    - None: 시계 차이를 모르므로 lag_ms 를 비운다(OFFSET_UNKNOWN). raw_lag_ms 는 남긴다.
    """
    require_ms("t_risk_now", t_risk_now)
    require_ms("t_latest_global", t_latest_global)
    raw = t_risk_now - t_latest_global
    if capture_clock is SAME_CLOCK:
        if raw < 0:
            raise ValueError(f"lag 가 음수다: t_latest_global={t_latest_global} > t_risk_now={t_risk_now}")
        return LagEstimate(raw_lag_ms=raw, lag_ms=raw, uncertainty_ms=0.0, status=LagStatus.SAME_CLOCK)
    if capture_clock is None:
        return LagEstimate(raw_lag_ms=raw, lag_ms=None, uncertainty_ms=None, status=LagStatus.OFFSET_UNKNOWN)
    if not isinstance(capture_clock, ClockOffset):
        raise TypeError(f"capture_clock: SAME_CLOCK, ClockOffset, None 중 하나여야 한다 (받은 값 {capture_clock!r})")
    corrected = t_risk_now - to_tier0_ms(t_latest_global, capture_clock)
    u = float(capture_clock.uncertainty_ms)
    if corrected + u < 0:
        return LagEstimate(raw_lag_ms=raw, lag_ms=None, uncertainty_ms=None, status=LagStatus.OFFSET_INCONSISTENT)
    return LagEstimate(raw_lag_ms=raw, lag_ms=max(corrected, 0), uncertainty_ms=u, status=LagStatus.CORRECTED)


@dataclass(frozen=True)
class TimeAges:
    """객체 한 개의 시간 경과(ms). 명세 6.1 정의 그대로.

    lag_ms 는 Tier 0 시계 기준(보정 후)이고 lag_uncertainty_ms 는 그 불확실성 반폭이다(같은 시계면 0).
    """

    gap_ms: int
    lag_ms: int
    staleness_ms: int
    lag_uncertainty_ms: float = 0.0


def _require_gap(t_last_valid: int, t_latest_global: int) -> int:
    require_ms("t_last_valid", t_last_valid)
    require_ms("t_latest_global", t_latest_global)
    gap = t_latest_global - t_last_valid
    if gap < 0:
        raise ValueError(f"gap 이 음수다: t_last_valid={t_last_valid} > t_latest_global={t_latest_global}")
    return gap


def _ages(gap: int, lag: LagEstimate) -> TimeAges | None:
    if lag.lag_ms is None or lag.uncertainty_ms is None:
        return None
    return TimeAges(gap_ms=gap, lag_ms=lag.lag_ms, staleness_ms=gap + lag.lag_ms, lag_uncertainty_ms=lag.uncertainty_ms)


def compute_ages(
    t_last_valid: int, t_latest_global: int, t_risk_now: int, capture_clock: CaptureClock = SAME_CLOCK
) -> TimeAges:
    """gap·lag·staleness를 계산한다.

    gap 이 음수면 t_last_valid 가 동기화 유효 카메라의 최신 시각보다 뒤라는 뜻이고,
    lag 가 음수면 판정 시계가 촬영 시각보다 뒤라는 뜻이다. 둘 다 입력 불일치이므로
    0으로 덮지 않고 ValueError 로 알린다. 시계 차이를 몰라 lag 를 모를 때도 ValueError 다
    (먼저 estimate_lag 로 확인하거나 evaluate_track 을 쓴다).
    """
    gap = _require_gap(t_last_valid, t_latest_global)
    lag = estimate_lag(t_risk_now, t_latest_global, capture_clock)
    ages = _ages(gap, lag)
    if ages is None:
        raise ValueError(f"lag 를 모른다: {lag.status} (raw_lag_ms={lag.raw_lag_ms})")
    return ages


@dataclass(frozen=True)
class TimeModelConfig:
    """시간 모델과 System Health 의 설정을 한 곳에 둔다.

    임계값은 실험용 초기값이며 객체 유형·카메라 주기·구역에 따라 조정한다(명세 6.1).

    observed_max_gap_ms: 이 값 이하의 gap 은 OBSERVED (표 0~200ms).
    predicted_max_gap_ms: 이 값 이하의 gap 은 PREDICTED, 넘으면 UNKNOWN (표 200~500ms, 500ms 초과).
    max_speed_mps: 불확실성 확대에 쓰는 수평 이동 속도 상한(m/s). 명세에 수치가 없는 제안값이다.
    lag_degraded_ms: 이 값을 넘는 lag 는 DEGRADED. 6.6 capture_to_risk p95 600ms 를 초기값으로 쓴다.
    lag_unsafe_ms: 이 값을 넘는 lag 는 UNSAFE_UNKNOWN. 명세에 수치가 없는 제안값이다.
        시계 차이로 보정한 lag 는 상한(lag + 불확실성)을 두 임계값과 비교한다(명세 6.1 "불확실성을 함께 쓴다").
    clock_uncertainty_degraded_ms: 노트북–Pod 시계 차이 불확실성이 이 값을 넘으면 DEGRADED (명세 6.5
        "노트북–Pod 시계 차이 불확실성 증가"). 수치는 명세에 없는 제안값이다(docs/design/tier0-clock-offset.md).
    camera_stall_ms: t_latest_global - 카메라 last_capture 가 이 값을 넘으면 수신 중단으로 본다.
        객체 UNKNOWN 기준(500ms)과 같게 둔다.
    min_cameras: 다시점 3D 에 필요한 최소 카메라 수(명세 5.2, 2대 이상).
    fusion_max_lag_ms: 수신 중이지만 t_latest_global 보다 이 값 넘게 늦은 카메라는 그 tick 의 융합 시각 계산에서 뺀다
        (choose_fusion_time). 명세에 없는 제안값이다(docs/design/tier0-fusion-time.md).
    """

    observed_max_gap_ms: int = 200
    predicted_max_gap_ms: int = 500
    max_speed_mps: float = 2.0
    lag_degraded_ms: int = 600
    lag_unsafe_ms: int = 2000
    clock_uncertainty_degraded_ms: float = 100.0
    camera_stall_ms: int = 500
    min_cameras: int = 2
    fusion_max_lag_ms: int = 150

    def __post_init__(self) -> None:
        if not 0 <= self.observed_max_gap_ms <= self.predicted_max_gap_ms:
            raise ValueError("0 <= observed_max_gap_ms <= predicted_max_gap_ms 여야 한다")
        if self.max_speed_mps < 0:
            raise ValueError("max_speed_mps 는 0 이상이어야 한다")
        if not 0 <= self.lag_degraded_ms <= self.lag_unsafe_ms:
            raise ValueError("0 <= lag_degraded_ms <= lag_unsafe_ms 여야 한다")
        if not self.clock_uncertainty_degraded_ms >= 0:
            raise ValueError("clock_uncertainty_degraded_ms 는 0 이상이어야 한다")
        if self.camera_stall_ms < 0:
            raise ValueError("camera_stall_ms 는 0 이상이어야 한다")
        if self.min_cameras < 1:
            raise ValueError("min_cameras 는 1 이상이어야 한다")
        if self.fusion_max_lag_ms < 0:
            raise ValueError("fusion_max_lag_ms 는 0 이상이어야 한다")


@dataclass(frozen=True)
class FusionTime:
    """한 tick 의 융합 시각과 그 시각 계산에서 뺀 늦은 카메라.

    lagging: 수신 중(camera_stall_ms 안)이지만 fusion_max_lag_ms 넘게 늦어 뺀 카메라(이름 순). 이 카메라들에는 t 이후
        프레임이 없으므로(외삽 금지) 그 tick 의 관측에 들어가지 않는다.
    """

    t: int
    lagging: tuple[str, ...] = ()


def choose_fusion_time(cameras: Iterable[CameraClock], config: TimeModelConfig) -> FusionTime | None:
    """World Track 공통 시각: 융합에 넣을 카메라들의 최신 촬영 시각 중 가장 이른 값. 모르면 None.

    외삽하지 않으므로 t 는 넣을 카메라 모두에 t 이후 프레임이 있는 시각이어야 한다(최솟값). 기준(latest)은 t_latest_global 과
    같은 동기화 유효 카메라의 최댓값이다. 멈춘 카메라(camera_stall_ms 초과)는 빼고, 수신 중이어도 fusion_max_lag_ms 넘게 늦은
    카메라는 뺀다(늦은 카메라 하나가 모든 객체의 gap 을 그만큼 키우지 않게). 빼고 남은 카메라가 min_cameras 보다 적으면
    빼지 않는다(다시점에 모자라게 서두르지 않고 이전처럼 늦은 카메라를 기다린다).
    """
    times = {c.camera_id: c.last_capture for c in cameras if c.sync_valid and c.last_capture is not None}
    if not times:
        return None
    latest = max(times.values())
    fresh = {c: t for c, t in times.items() if latest - t <= config.camera_stall_ms}
    prompt = {c: t for c, t in fresh.items() if latest - t <= config.fusion_max_lag_ms}
    if len(prompt) < config.min_cameras:
        return FusionTime(min(fresh.values()))
    return FusionTime(min(prompt.values()), tuple(sorted(c for c in fresh if c not in prompt)))


def classify_observation(gap_ms: int | None, config: TimeModelConfig = TimeModelConfig()) -> ObservationState:
    """observation_gap 으로 객체 관측 상태를 정한다 (명세 6.1 표).

    | gap               | 상태      |
    | 0 ~ 200ms         | OBSERVED  |
    | 200 초과 ~ 500ms  | PREDICTED |
    | 500ms 초과        | UNKNOWN   |

    경계값은 상한 포함으로 읽는다(표의 '500ms 초과'가 UNKNOWN 이므로 500ms 는 PREDICTED).
    gap 을 모르면(t_latest_global 없음) UNKNOWN 이다.
    """
    if gap_ms is None:
        return ObservationState.UNKNOWN
    if gap_ms < 0:
        raise ValueError(f"gap_ms 가 음수다: {gap_ms}")
    if gap_ms <= config.observed_max_gap_ms:
        return ObservationState.OBSERVED
    if gap_ms <= config.predicted_max_gap_ms:
        return ObservationState.PREDICTED
    return ObservationState.UNKNOWN


@dataclass(frozen=True)
class TrackTimeInput:
    """객체 한 개의 마지막 유효 관측. 필드 이름은 world_state 의 world_track 을 따른다.

    position_xy, position_xy_sigma: t_last_valid 시각의 수평 위치와 표준편차(m).
    velocity: World 속도 [vx, vy, vz] (m/s). 없으면 None 이고 위치를 옮기지 않는다.
    """

    world_id: str
    t_last_valid: int
    position_xy: tuple[float, float]
    position_xy_sigma: tuple[float, float]
    velocity: tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        require_ms("t_last_valid", self.t_last_valid)
        if any(s < 0 for s in self.position_xy_sigma):
            raise ValueError("position_xy_sigma 는 0 이상이어야 한다")


@dataclass(frozen=True)
class TrackTimeState:
    """판정 시각(t_risk_now) 기준의 객체 상태.

    UNKNOWN 이면 정확한 위치를 쓰지 않으므로 position_xy·position_xy_sigma 가 None 이다.
    ages 는 t_latest_global 이나 (시계 차이를 몰라) lag 를 모르면 None 이다. t_last_valid 는 입력 그대로 둔다.
    gap_ms 는 t_latest_global 을 모르면 None 이다.
    time_reference: 판정 시각까지의 경과(lag)를 아는지. False 면 위치·표준편차는 t_latest_global 시점까지만
        예측한 값이다(현재 시각으로 옮기지 않음). 이때는 현재 위치가 필요한 판단, 특히 해소 확인을 하지 않는다
        (decision_time.ClockAwareVerification).
    """

    world_id: str
    t_last_valid: int
    t_latest_global: int | None
    observation_state: ObservationState
    ages: TimeAges | None
    position_xy: tuple[float, float] | None
    position_xy_sigma: tuple[float, float] | None
    gap_ms: int | None = None
    time_reference: bool = True


def evaluate_track(
    track: TrackTimeInput,
    t_latest_global: int | None,
    t_risk_now: int,
    config: TimeModelConfig = TimeModelConfig(),
    capture_clock: CaptureClock = SAME_CLOCK,
) -> TrackTimeState:
    """관측 상태를 정하고 staleness 만큼 위치를 예측하며 불확실성을 키운다.

    - OBSERVED·PREDICTED: 속도가 있으면 position_xy + velocity_xy * staleness 로 옮기고,
      표준편차는 축마다 max_speed_mps * (staleness + lag 불확실성) 만큼 더한다(최악 이동 거리, 보수적).
    - UNKNOWN: 정확한 위치 사용을 멈춘다(위치·표준편차 None).
    - capture_clock 으로 lag 를 Tier 0 시계 기준으로 보정한다. 시계 차이를 몰라 lag 를 모르면(None,
      OFFSET_INCONSISTENT) 관측 상태는 그대로 gap 으로 정하고(gap 은 촬영 시각끼리라 시계 차이와 무관),
      위치는 t_latest_global 시점까지만 예측한다(time_reference False, ages None). 현재 시각으로 옮기지 않는다.
    """
    require_ms("t_risk_now", t_risk_now)
    ages: TimeAges | None = None
    gap: int | None = None
    if t_latest_global is not None:
        gap = _require_gap(track.t_last_valid, t_latest_global)
        ages = _ages(gap, estimate_lag(t_risk_now, t_latest_global, capture_clock))
    state = ObservationState.UNKNOWN if gap is None else classify_observation(gap, config)

    position: tuple[float, float] | None = None
    sigma: tuple[float, float] | None = None
    if state is not ObservationState.UNKNOWN:
        assert gap is not None
        # lag 를 알면 판정 시각까지(staleness), 모르면 촬영 시각 t_latest_global 까지(gap)만 옮긴다
        span_ms = ages.staleness_ms if ages is not None else gap
        grow_ms = ages.staleness_ms + ages.lag_uncertainty_ms if ages is not None else gap
        dt_s = span_ms / 1000.0
        x, y = track.position_xy
        if track.velocity is not None:
            x += track.velocity[0] * dt_s
            y += track.velocity[1] * dt_s
        position = (x, y)
        grow = config.max_speed_mps * grow_ms / 1000.0
        sigma = (track.position_xy_sigma[0] + grow, track.position_xy_sigma[1] + grow)

    return TrackTimeState(
        world_id=track.world_id,
        t_last_valid=track.t_last_valid,
        t_latest_global=t_latest_global,
        observation_state=state,
        ages=ages,
        position_xy=position,
        position_xy_sigma=sigma,
        gap_ms=gap,
        time_reference=ages is not None,
    )
