"""규칙 공통 형식 (명세 7.1).

규칙 하나 = RuleSpec(규칙 ID·버전·읽는 상태·근거 조항) + evaluate(RuleContext) → Finding 목록.
구체 규칙(R1~R7)은 후속 이슈(S15P31A101-71, -73 등)에서 이 형식으로 만들고 registry 에 등록한다.

- 규칙은 판정 출처(Origin)를 보지 못한다. RuleContext 에 출처가 없으므로 같은 입력은 출처와 관계없이
  같은 판정을 낸다(명세 7장: 같은 규칙을 네 가지 입력에 적용). 알림 여부는 엔진이 출처로만 정한다.
- 판정 시각은 RuleContext.now_ms(주입 시계)만 쓴다. 규칙이 시계를 직접 읽지 않는다.
- 발생·해소 히스테리시스와 RiskEvent 생명주기(RAISED/UPDATED/RESOLVED)는 이 골격 범위 밖이다(명세 6.4).
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import Severity
from simai_ai.risk.world import WorldState, WorldTrack
from simai_ai.risk.zone import Zone


@dataclass(frozen=True)
class RuleSpec:
    """규칙 ID + 버전, 읽는 상태, 근거 조항 (명세 7.1). 이벤트에는 rule_id·rule_version 을 기록한다."""

    rule_id: str
    rule_version: int
    reads: tuple[str, ...]
    legal_basis: str | None = None

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise RiskInputError("RuleSpec: rule_id 가 비었다")
        if isinstance(self.rule_version, bool) or not isinstance(self.rule_version, int) or self.rule_version < 1:
            raise RiskInputError(f"RuleSpec {self.rule_id}: rule_version 은 1 이상 정수여야 한다")
        object.__setattr__(self, "reads", tuple(self.reads))

    @property
    def key(self) -> tuple[str, int]:
        return (self.rule_id, self.rule_version)


class FindingTarget(StrEnum):
    """판정 대상. PERSON = 사람(world_ids), ZONE = 사람 없는 구역·시스템 판정(zone_id 만)."""

    PERSON = "PERSON"
    ZONE = "ZONE"


@dataclass(frozen=True)
class Finding:
    """규칙 하나가 낸 판정 결과와 근거.

    판정 대상은 둘 중 하나다(target).
    - 사람 대상(PERSON): world_ids 가 하나 이상. zone_id 는 있을 수도 없을 수도 있다(R1~R6).
    - 구역·시스템 대상(ZONE): world_ids 가 비고 zone_id 가 있다. 사람이 없어도 나는 판정이다
      (예: R7 구역 감시 능력 저하, 명세 7.2). 가짜 World ID 를 만들지 않는다.
    둘 다 비면 무엇에 대한 판정인지 알 수 없으므로 막는다.

    - severity: 위험 정도. 신뢰도가 낮아도 내리지 않는다(명세 7.3).
    - needs_check: "확인 필요". 모르면 진입하지 않았다고 확정하지 않고 이 표시를 붙인다(명세 8.2, 7.2).
    - reasons: 판정 근거 코드(예: "xy_inside_polygon", "z_unknown"). 순서를 지킨다.
    - metrics: 수치 근거(예: edge_distance_m). 단위는 키 이름에 붙인다.
    """

    rule_id: str
    rule_version: int
    severity: Severity
    world_ids: tuple[str, ...]
    zone_id: str | None = None
    needs_check: bool = False
    reasons: tuple[str, ...] = ()
    # 읽기 전용 mapping 은 해시가 안 되므로 해시에서만 뺀다(비교에는 들어간다).
    metrics: Mapping[str, float] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if not self.world_ids and self.zone_id is None:
            raise RiskInputError(f"Finding {self.rule_id}: world_ids 와 zone_id 가 모두 비었다(판정 대상이 없다)")
        metrics = {}
        for name, value in sorted(self.metrics.items()):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise RiskInputError(f"Finding {self.rule_id}: metrics.{name} 가 유한한 수가 아니다")
            metrics[name] = float(value)
        object.__setattr__(self, "severity", Severity(self.severity))
        object.__setattr__(self, "world_ids", tuple(sorted(self.world_ids)))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "metrics", MappingProxyType(metrics))

    @property
    def target(self) -> FindingTarget:
        return FindingTarget.PERSON if self.world_ids else FindingTarget.ZONE

    @property
    def sort_key(self) -> tuple[str, int, str, tuple[str, ...]]:
        return (self.rule_id, self.rule_version, self.zone_id or "", self.world_ids)


@dataclass(frozen=True)
class RuleContext:
    """규칙이 읽는 입력. 출처(Origin)는 일부러 넣지 않는다.

    시간 모델(명세 6.1): T_gap = t_latest_global - t_last_valid, T_lag = now - t_latest_global,
    T_staleness = T_gap + T_lag. 상태 임계값(200·500ms)은 규칙·후속 이슈에서 정한다.
    """

    now_ms: int
    world: WorldState
    zones: tuple[Zone, ...]

    @property
    def lag_ms(self) -> int:
        return self.now_ms - self.world.t_latest_global

    def gap_ms(self, track: WorldTrack) -> int:
        return self.world.t_latest_global - track.t_last_valid

    def staleness_ms(self, track: WorldTrack) -> int:
        return self.gap_ms(track) + self.lag_ms


@runtime_checkable
class Rule(Protocol):
    @property
    def spec(self) -> RuleSpec: ...

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]: ...
