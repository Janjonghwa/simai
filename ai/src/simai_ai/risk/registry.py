"""규칙 등록부. (규칙 ID, 버전) → 규칙을 만드는 함수.

공통 테스트 벡터는 규칙을 (ID, 버전)으로만 적고, 실행기는 등록부에서 규칙을 만든다.
두 배포(Tier 0, EC2 컨테이너)가 같은 등록부를 쓰므로 같은 벡터에 같은 결과가 나와야 한다.
구체 규칙은 default_registry 에 등록한다. 현재 R1(S15P31A101-71), R7(S15P31A101-73). R6 은 자기 ID 의 이벤트를 만들지
않고 구역 규칙을 감싸는 방식이라(rules.r6_observation_loss.ObservationLossHold) 따로 등록하지 않는다. 구역 규칙(R1 등)은
`lambda: ObservationLossHold(규칙())` 로 등록한다.
"""

from collections.abc import Callable, Iterator

from simai_ai.risk.rule import Rule
from simai_ai.risk.rules.r1_zone_entry import R1_RULE_ID, R1_RULE_VERSION, ZoneEntryRule
from simai_ai.risk.rules.r6_observation_loss import ObservationLossHold
from simai_ai.risk.rules.r7_monitoring import R7_RULE_ID, R7_RULE_VERSION, ZoneMonitoringRule

RuleFactory = Callable[[], Rule]


class UnknownRuleError(LookupError):
    """등록되지 않은 (규칙 ID, 버전)."""


class RuleRegistry:
    def __init__(self) -> None:
        self._factories: dict[tuple[str, int], RuleFactory] = {}

    def register(self, rule_id: str, rule_version: int, factory: RuleFactory) -> None:
        key = (rule_id, rule_version)
        if key in self._factories:
            raise ValueError(f"이미 등록된 규칙이다: {rule_id} v{rule_version}")
        self._factories[key] = factory

    def create(self, rule_id: str, rule_version: int) -> Rule:
        key = (rule_id, rule_version)
        try:
            factory = self._factories[key]
        except KeyError:
            raise UnknownRuleError(f"등록되지 않은 규칙이다: {rule_id} v{rule_version}") from None
        rule = factory()
        if rule.spec.key != key:
            made = f"{rule.spec.rule_id} v{rule.spec.rule_version}"
            raise ValueError(f"{rule_id} v{rule_version} 로 등록된 함수가 {made} 를 만들었다")
        return rule

    def __iter__(self) -> Iterator[tuple[str, int]]:
        return iter(sorted(self._factories))

    def __len__(self) -> int:
        return len(self._factories)


def default_registry() -> RuleRegistry:
    """배포에서 쓰는 등록부. 두 배포(Tier 0, EC2 컨테이너)가 같은 등록부를 쓴다."""
    registry = RuleRegistry()
    registry.register(R1_RULE_ID, R1_RULE_VERSION, lambda: ObservationLossHold(ZoneEntryRule()))
    registry.register(R7_RULE_ID, R7_RULE_VERSION, ZoneMonitoringRule)
    return registry
