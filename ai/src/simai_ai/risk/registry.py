"""규칙 등록부. (규칙 ID, 버전) → 규칙을 만드는 함수.

공통 테스트 벡터는 규칙을 (ID, 버전)으로만 적고, 실행기는 등록부에서 규칙을 만든다.
두 배포(Tier 0, EC2 컨테이너)가 같은 등록부를 쓰므로 같은 벡터에 같은 결과가 나와야 한다.
구체 규칙은 default_registry 에 등록한다. 현재 R1(S15P31A101-71), R2·R3(S15P31A101-93), R4(S15P31A101-96), R5(S15P31A101-98), R7(S15P31A101-73).
높이 규칙(R2·R3)과 그 가장자리를 예측하는 R5 는 운영 프로파일(기본 버전, 높이차 2 m)과 교실 시험 프로파일(기본 버전 + 1000,
높이차 0.35 m, 명세 7.4 '별도 rule_version')을 함께 등록한다. 배포는 구성 묶음 rules 의 rule_versions 로 하나를 고른다. R6 은 자기 ID 의 이벤트를 만들지
않고 구역 규칙을 감싸는 방식이라(rules.r6_observation_loss.ObservationLossHold) 따로 등록하지 않는다. 구역 규칙(R1 등)은
`lambda: ObservationLossHold(규칙())` 로 등록한다.
"""

from collections.abc import Callable, Iterator

from simai_ai.risk.rule import Rule
from simai_ai.risk.rules.height_common import CLASSROOM_TEST_PROFILE, OPERATIONAL_HEIGHT_PROFILE
from simai_ai.risk.rules.r1_zone_entry import R1_RULE_ID, R1_RULE_VERSION, ZoneEntryRule
from simai_ai.risk.rules.r2_edge_proximity import R2_RULE_ID, R2_RULE_VERSION, EdgeProximityRule
from simai_ai.risk.rules.r3_elevated_position import R3_RULE_ID, R3_RULE_VERSION, ElevatedPositionRule
from simai_ai.risk.rules.r4_fall_suspect import R4_RULE_ID, R4_RULE_VERSION, FallSuspectRule
from simai_ai.risk.rules.r5_entry_prediction import R5_RULE_ID, R5_RULE_VERSION, EntryPredictionRule
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
    # R2·R3·R5 는 구역 규칙이 아니라 R6 으로 감싸지 않는다(관측 상실 중 활성 이벤트는 lifecycle 이 UNVERIFIABLE 로 유지).
    # 운영 프로파일(기본 버전)과 교실 시험 프로파일(기본 버전 + 1000)을 함께 등록한다(명세 7.4).
    for profile in (OPERATIONAL_HEIGHT_PROFILE, CLASSROOM_TEST_PROFILE):
        registry.register(
            R2_RULE_ID, profile.rule_version(R2_RULE_VERSION), lambda p=profile: EdgeProximityRule(profile=p)
        )
        registry.register(
            R3_RULE_ID, profile.rule_version(R3_RULE_VERSION), lambda p=profile: ElevatedPositionRule(profile=p)
        )
        registry.register(
            R5_RULE_ID, profile.rule_version(R5_RULE_VERSION), lambda p=profile: EntryPredictionRule(profile=p)
        )
    # R4 는 사람별 이력을 규칙 객체가 아니라 엔진이 넘기는 RuleContext.state 에 둔다(구성 전환·출처 분리)
    registry.register(R4_RULE_ID, R4_RULE_VERSION, FallSuspectRule)
    registry.register(R7_RULE_ID, R7_RULE_VERSION, ZoneMonitoringRule)
    return registry
