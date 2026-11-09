"""R9 작업 중 안전모 미착용 (명세 7.2 R9, 5.5, 8.10, 15.1 G9·G6-R9, S15P31A101-519).

요구는 작업 기준이다. 활성 구성 묶음의 `ppe_policy` 가 안전모 REQUIRED(진행 중 작업 있음)일 때만 판정하고, 사람의 위치·구역은
보지 않는다. NOT_REQUIRED 이거나 묶음에 `ppe_policy` 가 없으면(“PPE 요구 미수신”) 요구 없음으로 보고 판정하지 않는다.

요구 REQUIRED 일 때 사람(World 트랙)마다 PPE 프로세스의 최신 안전모 상태(같은 World ID·identity_revision, 만료 전)로:
| 안전모 | 판정 |
|---|---|
| NOT_WORN | 발생 판정(CAUTION, holding=False). 발생 지속 조건은 일반 규칙(6.4)을 그대로 쓴다 |
| UNKNOWN(상태 없음·만료·identity_revision 불일치 포함) | 유지 판정(holding=True, 확인 필요 PPE_UNKNOWN). 새 이벤트는 내지 않고 이미 열린 R9 만 “확인 필요”로 유지한다 |
| WORN | 판정 없음. 해소 근거(clear_world_ids)로 돌려준다 |

해소 근거(rule.PersonClearEvidence): 요구 중 WORN 인 사람(착용 관측), 요구가 없으면 모든 사람(clears_all, 작업 종료 요구 해제).
lifecycle 은 이 근거로 사람 위치 관측과 관계없이 VERIFIED 해소를 하고, R9 의 해소 유지 시간은 0 이라 그 판정에 닫힌다(7.2,
v1.3 수정 67). 이벤트는 사람(World ID)당 하나다(lifecycle 의 사람 대상 키, zone_id 없음).

조끼는 R9 에 쓰지 않는다(R11 이 따로 판정, 7.2). 심각도는 CAUTION 하나라 7.3 표대로 관제 화면 안내이고 현장 경보(노트북 로컬 경보)가 없다.
LIVE 가 아니면 알림 채널이 없다(엔진·alerting).

Lead 시연 예외 승인(2026-11-09, S15P31A101-556)으로 시드에 포함한다. G9·G6-R9 통과는 미확정이다.
Tier 0 는 묶음에 적힌 규칙만 돌린다(15.1, 8.10).
"""

from collections.abc import Iterator

from simai_ai.risk.common import ConfidenceReason, Severity
from simai_ai.risk.ppe import WearState
from simai_ai.risk.rule import Finding, RuleContext, RuleSpec

R9_RULE_ID = "R9"
R9_RULE_VERSION = 1
R9_SEVERITY = Severity.CAUTION
R9_LEGAL_BASIS = "산업안전보건기준규칙 제32조"
"""제32조(보호구의 지급 등) ①1호: 물체가 떨어지거나 날아올 위험 또는 근로자가 추락할 위험이 있는 작업의 안전모(7.2)."""

REASON_NOT_WORN = "helmet_not_worn"
REASON_UNKNOWN = "helmet_unknown"


class HelmetRequiredRule:
    """R9 v1. 안전모 요구 REQUIRED 인데 안전모 NOT_WORN 이면 사람 대상 CAUTION 판정을 낸다."""

    def __init__(self) -> None:
        self._spec = RuleSpec(
            rule_id=R9_RULE_ID,
            rule_version=R9_RULE_VERSION,
            reads=("tracks.world_id", "tracks.identity_revision", "t_latest_global", "ppe_policy.helmet", "ppe_state.helmet"),
            legal_basis=R9_LEGAL_BASIS,
        )

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    @staticmethod
    def _required(ctx: RuleContext) -> bool:
        return ctx.ppe_policy is not None and ctx.ppe_policy.helmet_required

    def evaluate(self, ctx: RuleContext) -> Iterator[Finding]:
        if not self._required(ctx):
            return
        t = ctx.world.t_latest_global
        for track in ctx.world.tracks:
            state = ctx.ppe.helmet_state(track, t)
            if state is WearState.NOT_WORN:
                yield Finding(
                    rule_id=R9_RULE_ID,
                    rule_version=R9_RULE_VERSION,
                    severity=R9_SEVERITY,
                    world_ids=(track.world_id,),
                    reasons=(REASON_NOT_WORN,),
                )
            elif state is WearState.UNKNOWN:
                yield Finding(
                    rule_id=R9_RULE_ID,
                    rule_version=R9_RULE_VERSION,
                    severity=R9_SEVERITY,
                    world_ids=(track.world_id,),
                    reasons=(REASON_UNKNOWN,),
                    holding=True,
                    confidence_reasons=(ConfidenceReason.PPE_UNKNOWN,),
                )

    def clear_world_ids(self, ctx: RuleContext) -> Iterator[str]:
        if not self._required(ctx):
            return
        t = ctx.world.t_latest_global
        for track in ctx.world.tracks:
            if ctx.ppe.helmet_state(track, t) is WearState.WORN:
                yield track.world_id

    def clears_all(self, ctx: RuleContext) -> bool:
        return not self._required(ctx)
