"""판정 시각 어댑터: Tier 0 시계를 Risk Engine·생명주기에 안전하게 넘긴다 (명세 3.9, 6.1, 6.4).

- DecisionClock: Tier0Clock 의 UTC 는 벽시계 step 때 기준점을 다시 잡아 뒤로 갈 수 있다. Risk 생명주기는
  판정 시각 역행을 거부하므로(risk.lifecycle), 판정 시각은 직전 값 이상으로 고정해 앞으로만 가게 하고
  기준점 전환(epoch)을 기록한다. 고정된 동안 판정 시각은 Tier 0 UTC 보다 최대 step 만큼 앞서므로
  이 시각으로 잰 lag 는 그만큼 크게(보수적으로) 나온다. risk.Clock 프로토콜(now_ms)을 따른다.
- ClockAwareVerification: risk.ObservedTracksPolicy 는 lag 를 now_ms - world.t_latest_global 로 보는데,
  LIVE 에서 now_ms 는 Tier 0 시계이고 t_latest_global 은 노트북 시계다. 판정 시각을 시계 차이 추정값의
  상한(now + offset + uncertainty)으로 노트북 시계에 옮겨 넘긴다. 절대 시각을 믿을 수 없는 모든 상태에서는
  해소를 확인하지 않는다(UNVERIFIABLE, 해소 확정 금지): 시계 차이 모름(표본 없음·유지 상한 초과·연결 끊김)과,
  시계 차이는 있으나 이 World 의 촬영 시각과 맞지 않음(time_model.estimate_lag 의 OFFSET_INCONSISTENT, 마지막
  표본 뒤 노트북 시계 점프 등). 시간 모델·System Health 와 같은 판정 함수를 써서 어긋나지 않게 한다.
  또 시계 세대(generation_source, Tier0ClockSync.generation)가 바뀐 뒤 각 이벤트의 첫 확인은 False 로 둔다.
  해소 유지 시간은 노트북 시계 observation_time 으로 재므로(risk.lifecycle), 노트북 시계가 앞으로 뛴 직후 교환이
  먼저 점프를 감지해 offset 이 맞아지면 점프 폭만큼 유지 시간을 건너뛰기 때문이다. 한 번 False 면 생명주기가
  해소 확인을 처음부터 다시 센다(점프 뒤 관측으로 유지 시간을 다시 채움). 구간이 겹칠 만큼 작은 점프는
  감지되지 않아 그 폭(왕복 시간 수준)만큼 유지 시간이 줄 수 있다.
  새 이벤트 발생 판정은 막지 않는다.

risk 패키지는 다른 기능 패키지를 import 하지 않으므로 어댑터는 tracking 쪽에 둔다(lifecycle 은 바꾸지 않는다).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from simai_ai.risk.lifecycle import EventKey, ObservedTracksPolicy, VerificationPolicy
from simai_ai.risk.world import WorldState
from simai_ai.tracking.clock_sync import Tier0Clock
from simai_ai.tracking.time_model import ClockOffset, LagStatus, estimate_lag


@dataclass(frozen=True)
class EpochChange:
    """Tier0Clock 기준점 전환 기록. held_ms 는 판정 시각을 고정한 폭(역행 크기, 앞으로 뛰면 0)."""

    epoch: int
    mono_ns: int
    previous_ms: int
    utc_ms: int
    held_ms: int


class DecisionClock:
    """판정 시각(t_risk_now)을 앞으로만 가게 하는 risk.Clock 어댑터."""

    def __init__(self, clock: Tier0Clock) -> None:
        self._clock = clock
        self._last: int | None = None
        self._epoch: int | None = None
        self.epoch_changes: list[EpochChange] = []

    def now_ms(self) -> int:
        now = self._clock.now()
        value = now.utc_ms if self._last is None else max(now.utc_ms, self._last)
        if self._epoch is not None and self._clock.epoch != self._epoch:
            assert self._last is not None
            self.epoch_changes.append(
                EpochChange(
                    epoch=self._clock.epoch,
                    mono_ns=now.mono_ns,
                    previous_ms=self._last,
                    utc_ms=now.utc_ms,
                    held_ms=max(self._last - now.utc_ms, 0),
                )
            )
        self._epoch = self._clock.epoch
        self._last = value
        return value


_MAX_TRACKED_KEYS = 1024


@dataclass
class ClockAwareVerification:
    """시계 차이를 반영하는 해소 확인 정책 (risk.VerificationPolicy).

    offset_source: 지금의 노트북–Pod 시계 차이(Tier0ClockSync.capture_clock(링크 상태)). 모르면 None.
    base: 실제 확인 정책(기본 ObservedTracksPolicy). now_ms 는 노트북 시계로 옮긴 상한으로 넘긴다.
    generation_source: 시계 세대(Tier0ClockSync.generation). 바뀐 뒤 각 이벤트의 첫 확인은 False. 없으면 보지 않는다.
    """

    offset_source: Callable[[], ClockOffset | None]
    base: VerificationPolicy = field(default_factory=ObservedTracksPolicy)
    generation_source: Callable[[], int] | None = None
    _seen: dict[EventKey, int] = field(default_factory=dict, init=False, repr=False)

    def _generation_changed(self, key: EventKey) -> bool:
        """이 이벤트를 마지막으로 확인한 뒤 시계 세대가 바뀌었는지. 확인한 세대를 기록한다."""
        if self.generation_source is None:
            return False
        generation = self.generation_source()
        previous = self._seen.get(key)
        self._seen[key] = generation
        if len(self._seen) > _MAX_TRACKED_KEYS:
            # 현재 세대 기록은 지워도 같다(다음 확인 때 새로 기록하고 그대로 진행). 옛 세대 기록만 남긴다
            self._seen = {k: g for k, g in self._seen.items() if g != generation}
        return previous is not None and previous != generation

    def verifiable(self, key: EventKey, world_ids: tuple[str, ...], world: WorldState, now_ms: int) -> bool:
        if self._generation_changed(key):
            return False  # 시계 점프·기준점 전환 직후: 해소 유지 시간을 처음부터 다시 채운다
        offset = self.offset_source()
        if offset is None:
            return False  # 판정 시각 기준 지연을 모른다: 해소 확정 금지
        if estimate_lag(now_ms, world.t_latest_global, offset).status is not LagStatus.CORRECTED:
            return False  # 시계 차이가 이 촬영 시각과 맞지 않는다(OFFSET_INCONSISTENT): 해소 확정 금지
        now_capture = math.ceil(now_ms + offset.offset_ms + offset.uncertainty_ms)
        return self.base.verifiable(key, world_ids, world, now_capture)
