"""규칙 공통 형식 (명세 7.1).

규칙 하나 = RuleSpec(규칙 ID·버전·읽는 상태·근거 조항) + evaluate(RuleContext) → Finding 목록.
구체 규칙(R1~R7)은 후속 이슈(S15P31A101-71, -73 등)에서 이 형식으로 만들고 registry 에 등록한다.

- 규칙은 판정 출처(Origin)를 보지 못한다. RuleContext 에 출처가 없으므로, 엔진이 출처에서 정하는
  RuleContext.monitoring_expected(카메라 감시 상태 개념이 있는 입력인지, R7 이 씀)를 빼면 같은 입력은 출처와
  관계없이 같은 판정을 낸다(명세 7장: 같은 규칙을 네 가지 입력에 적용). 알림 여부는 엔진이 출처로만 정한다.
- 판정 시각은 RuleContext.now_ms(주입 시계)만 쓴다. 규칙이 시계를 직접 읽지 않는다.
- 규칙은 상태를 갖지 않는다. 발생·해소 임계값 분리(히스테리시스, 명세 6.4·7.1)는 Finding.holding 으로 낸다.
  발생 임계값을 넘으면 holding=False, 해소 임계값 안쪽(띠 안)이면 holding=True 로 낸다. 활성 이벤트가
  있는지는 규칙이 몰라도 되고, 이벤트 생성·유지·해소 시간 확인은 lifecycle 이 한다.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import ConfidenceReason, Severity, normalize_confidence
from simai_ai.risk.monitoring import MonitoringState
from simai_ai.risk.work_surface import WorkSurface
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
      "확인 필요"는 confidence_reasons 가 비어 있지 않은 상태라(7.1) 생성 시 둘을 맞춘다(common.normalize_confidence).
      이유가 있으면 needs_check=True 가 되고, needs_check=True 인데 이유가 없으면(하위 호환) UNSPECIFIED 를 붙인다.
    - confidence_reasons: "확인 필요"의 이유(ConfidenceReason, 7.1). 규칙은 아는 이유를 직접 적는다.
    - reasons: 판정 근거 코드(예: "xy_inside_polygon", "z_unknown"). 순서를 지킨다.
    - metrics: 수치 근거(예: edge_distance_m). 단위는 키 이름에 붙인다.
    - holding: 발생 임계값은 넘지 않았지만 해소 임계값 안쪽이다(히스테리시스 띠, 불확실성 범위 포함).
      활성 이벤트를 유지만 하고 새 이벤트를 만들지 않는다(lifecycle). 예: R2 발생 1.0m / 해소 1.3m 이면
      1.0~1.3m 에서 holding=True.
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
    holding: bool = False
    confidence_reasons: tuple[ConfidenceReason, ...] = ()

    def __post_init__(self) -> None:
        if not self.world_ids and self.zone_id is None:
            raise RiskInputError(f"Finding {self.rule_id}: world_ids 와 zone_id 가 모두 비었다(판정 대상이 없다)")
        try:
            needs_check, confidence_reasons = normalize_confidence(
                self.needs_check, self.confidence_reasons, f"Finding {self.rule_id}"
            )
        except ValueError as e:
            raise RiskInputError(f"Finding {self.rule_id}: confidence_reasons 에 모르는 값이 있다 ({e})") from e
        object.__setattr__(self, "needs_check", needs_check)
        object.__setattr__(self, "confidence_reasons", confidence_reasons)
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

    monitoring: 같은 판정 시각의 카메라 상태·구역별 관측 카메라(R7 입력). world_state 에 없는 감시 상태라
    따로 받는다. 넘기지 않으면 None 이고, 이 값을 읽는 규칙은 None 을 '모름'으로 다룬다(정상으로 채우지 않음).

    monitoring_expected: 이 입력에 카메라 감시 상태라는 개념이 있는지. 카메라 영상에서 온 입력(LIVE·REPLAY)은 True,
    카메라가 없는 계획·가상 입력(PLANNED·WHAT_IF)은 False 다(엔진이 출처에서 정한다). False 이고 monitoring 이
    None 이면 감시 상태 규칙(R7)은 판정하지 않는다. 출처 자체는 넣지 않는다(규칙은 출처와 관계없이 같게 판정한다).

    work_surfaces: 판정 시각의 등록 작업면(명세 7.5, 활성 구성 묶음의 Operational Geometry). R2·R3 와 R5 의 가장자리
    예측이 읽는다. 비어 있으면 등록 작업면이 없다는 뜻이고, 그 위의 가장자리·고소 위치는 판정할 근거가 없다.
    """

    now_ms: int
    world: WorldState
    zones: tuple[Zone, ...]
    monitoring: MonitoringState | None = None
    monitoring_expected: bool = True
    work_surfaces: tuple[WorkSurface, ...] = ()

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
