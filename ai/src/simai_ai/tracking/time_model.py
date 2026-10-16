"""시간 모델: latest_global, 객체별 gap·lag·staleness, 관측 상태 전환과 불확실성 확대 (명세 6.1).

    T_gap       = t_latest_global - t_last_valid
    T_lag       = t_risk_now      - t_latest_global
    T_staleness = T_gap + T_lag

- 모든 시각은 UTC int64 ms다. 현재 시각(t_risk_now)은 호출자가 주입한다(직접 읽지 않음).
- 관측 상태는 gap으로 정한다. lag는 System Health(system_health.py)가 본다.
- 위치 예측과 불확실성 확대는 staleness로 한다. OBSERVED도 최신이라는 뜻이 아니므로 함께 확대한다.
- 예측 위치를 관측처럼 다루지 않는다. 이 모듈은 t_last_valid를 바꾸지 않고, 시각은 advance_ms로만 앞으로 간다.

필드 이름은 contracts/messages/world_state.schema.json 을 따른다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


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
class TimeAges:
    """객체 한 개의 시간 경과(ms). 명세 6.1 정의 그대로."""

    gap_ms: int
    lag_ms: int
    staleness_ms: int


def compute_ages(t_last_valid: int, t_latest_global: int, t_risk_now: int) -> TimeAges:
    """gap·lag·staleness를 계산한다.

    gap 이 음수면 t_last_valid 가 동기화 유효 카메라의 최신 시각보다 뒤라는 뜻이고,
    lag 가 음수면 판정 시계가 촬영 시각보다 뒤라는 뜻이다. 둘 다 입력 불일치이므로
    0으로 덮지 않고 ValueError 로 알린다.
    """
    require_ms("t_last_valid", t_last_valid)
    require_ms("t_latest_global", t_latest_global)
    require_ms("t_risk_now", t_risk_now)
    gap = t_latest_global - t_last_valid
    lag = t_risk_now - t_latest_global
    if gap < 0:
        raise ValueError(f"gap 이 음수다: t_last_valid={t_last_valid} > t_latest_global={t_latest_global}")
    if lag < 0:
        raise ValueError(f"lag 가 음수다: t_latest_global={t_latest_global} > t_risk_now={t_risk_now}")
    return TimeAges(gap_ms=gap, lag_ms=lag, staleness_ms=gap + lag)


@dataclass(frozen=True)
class TimeModelConfig:
    """시간 모델과 System Health 의 설정을 한 곳에 둔다.

    임계값은 실험용 초기값이며 객체 유형·카메라 주기·구역에 따라 조정한다(명세 6.1).

    observed_max_gap_ms: 이 값 이하의 gap 은 OBSERVED (표 0~200ms).
    predicted_max_gap_ms: 이 값 이하의 gap 은 PREDICTED, 넘으면 UNKNOWN (표 200~500ms, 500ms 초과).
    max_speed_mps: 불확실성 확대에 쓰는 수평 이동 속도 상한(m/s). 명세에 수치가 없는 제안값이다.
    lag_degraded_ms: 이 값을 넘는 lag 는 DEGRADED. 6.6 capture_to_risk p95 600ms 를 초기값으로 쓴다.
    lag_unsafe_ms: 이 값을 넘는 lag 는 UNSAFE_UNKNOWN. 명세에 수치가 없는 제안값이다.
    camera_stall_ms: t_latest_global - 카메라 last_capture 가 이 값을 넘으면 수신 중단으로 본다.
        객체 UNKNOWN 기준(500ms)과 같게 둔다.
    min_cameras: 다시점 3D 에 필요한 최소 카메라 수(명세 5.2, 2대 이상).
    """

    observed_max_gap_ms: int = 200
    predicted_max_gap_ms: int = 500
    max_speed_mps: float = 2.0
    lag_degraded_ms: int = 600
    lag_unsafe_ms: int = 2000
    camera_stall_ms: int = 500
    min_cameras: int = 2

    def __post_init__(self) -> None:
        if not 0 <= self.observed_max_gap_ms <= self.predicted_max_gap_ms:
            raise ValueError("0 <= observed_max_gap_ms <= predicted_max_gap_ms 여야 한다")
        if self.max_speed_mps < 0:
            raise ValueError("max_speed_mps 는 0 이상이어야 한다")
        if not 0 <= self.lag_degraded_ms <= self.lag_unsafe_ms:
            raise ValueError("0 <= lag_degraded_ms <= lag_unsafe_ms 여야 한다")
        if self.camera_stall_ms < 0:
            raise ValueError("camera_stall_ms 는 0 이상이어야 한다")
        if self.min_cameras < 1:
            raise ValueError("min_cameras 는 1 이상이어야 한다")


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
    ages 는 t_latest_global 을 모르면 None 이다. t_last_valid 는 입력 그대로 둔다.
    """

    world_id: str
    t_last_valid: int
    t_latest_global: int | None
    observation_state: ObservationState
    ages: TimeAges | None
    position_xy: tuple[float, float] | None
    position_xy_sigma: tuple[float, float] | None


def evaluate_track(
    track: TrackTimeInput,
    t_latest_global: int | None,
    t_risk_now: int,
    config: TimeModelConfig = TimeModelConfig(),
) -> TrackTimeState:
    """관측 상태를 정하고 staleness 만큼 위치를 예측하며 불확실성을 키운다.

    - OBSERVED·PREDICTED: 속도가 있으면 position_xy + velocity_xy * staleness 로 옮기고,
      표준편차는 축마다 max_speed_mps * staleness 만큼 더한다(최악 이동 거리, 보수적).
    - UNKNOWN: 정확한 위치 사용을 멈춘다(위치·표준편차 None).
    """
    require_ms("t_risk_now", t_risk_now)
    if t_latest_global is None:
        ages = None
        state = ObservationState.UNKNOWN
    else:
        ages = compute_ages(track.t_last_valid, t_latest_global, t_risk_now)
        state = classify_observation(ages.gap_ms, config)

    position: tuple[float, float] | None = None
    sigma: tuple[float, float] | None = None
    if state is not ObservationState.UNKNOWN:
        assert ages is not None
        dt_s = ages.staleness_ms / 1000.0
        x, y = track.position_xy
        if track.velocity is not None:
            x += track.velocity[0] * dt_s
            y += track.velocity[1] * dt_s
        position = (x, y)
        grow = config.max_speed_mps * dt_s
        sigma = (track.position_xy_sigma[0] + grow, track.position_xy_sigma[1] + grow)

    return TrackTimeState(
        world_id=track.world_id,
        t_last_valid=track.t_last_valid,
        t_latest_global=t_latest_global,
        observation_state=state,
        ages=ages,
        position_xy=position,
        position_xy_sigma=sigma,
    )
