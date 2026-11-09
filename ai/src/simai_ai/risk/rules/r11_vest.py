"""R11 작업 중 조끼 미착용 (명세 7.2, 5.5, 8.10, S15P31A101-548).

R9(안전모, `r9_helmet`)와 같은 규칙을 조끼에 쓴다. 요구는 작업 기준이다. 활성 구성 묶음의 `ppe_policy.vest`(1.1.0)가
REQUIRED(진행 중 작업 있음)일 때만 판정하고, 사람의 위치·구역은 보지 않는다. NOT_REQUIRED 이거나 `vest` 가 없거나 묶음에
`ppe_policy` 가 없으면 조끼 요구 없음으로 보고 판정하지 않는다.

요구 REQUIRED 일 때 사람(World 트랙)마다 PPE 프로세스의 최신 조끼 상태(같은 World ID·identity_revision, 만료 전)로:
| 조끼 | 판정 |
|---|---|
| NOT_WORN | 발생 판정(CAUTION, holding=False). 발생 지속 조건은 일반 규칙(6.4)을 그대로 쓴다 |
| UNKNOWN(상태 없음·만료·identity_revision 불일치 포함) | 유지 판정(holding=True, 확인 필요 PPE_UNKNOWN). 새 이벤트는 내지 않고 이미 열린 R11 만 “확인 필요”로 유지한다 |
| WORN | 판정 없음. 해소 근거(clear_world_ids)로 돌려준다 |

해소·이벤트 단위·해소 유지 시간(0)·관측 상실 예외는 R9 와 같다(lifecycle 기본 정책에 R11 도 넣는다). 검출 없음은 미착용이
아니다(PPE 프로세스가 근거 없으면 UNKNOWN 으로 보낸다, 5.5). 심각도는 CAUTION 하나라 관제 화면 안내이고 현장 경보가 없다(7.3).

Lead 시연 예외 승인(2026-11-09, S15P31A101-556)으로 시드에 포함한다. G9 조끼·G6-R11 통과는 미확정이다.
G9(조끼)·G6-R11 통과 뒤 켠다. 명세 7.2 규칙 표의 R11 행은 명세 수정 이슈에서 더한다.
"""

from collections.abc import Iterator

from simai_ai.risk.common import ConfidenceReason, Severity
from simai_ai.risk.ppe import WearState
from simai_ai.risk.rule import Finding, RuleContext, RuleSpec

R11_RULE_ID = "R11"
R11_RULE_VERSION = 1
R11_SEVERITY = Severity.CAUTION
R11_LEGAL_BASIS = "산업안전보건기준규칙 제32조"
"""제32조(보호구의 지급 등): 작업 조건에 맞는 보호구 지급·착용. 안전조끼 요구는 현장 작업 기준(명세 8.10 개정 예정)."""

REASON_NOT_WORN = "vest_not_worn"
REASON_UNKNOWN = "vest_unknown"


class VestRequiredRule:
    """R11 v1. 조끼 요구 REQUIRED 인데 조끼 NOT_WORN 이면 사람 대상 CAUTION 판정을 낸다."""

    def __init__(self) -> None:
        self._spec = RuleSpec(
            rule_id=R11_RULE_ID,
            rule_version=R11_RULE_VERSION,
            reads=("tracks.world_id", "tracks.identity_revision", "t_latest_global", "ppe_policy.vest", "ppe_state.vest"),
            legal_basis=R11_LEGAL_BASIS,
        )

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    @staticmethod
    def _required(ctx: RuleContext) -> bool:
        return ctx.ppe_policy is not None and ctx.ppe_policy.vest_required

    def evaluate(self, ctx: RuleContext) -> Iterator[Finding]:
        if not self._required(ctx):
            return
        t = ctx.world.t_latest_global
        for track in ctx.world.tracks:
            state = ctx.ppe.vest_state(track, t)
            if state is WearState.NOT_WORN:
                yield Finding(
                    rule_id=R11_RULE_ID,
                    rule_version=R11_RULE_VERSION,
                    severity=R11_SEVERITY,
                    world_ids=(track.world_id,),
                    reasons=(REASON_NOT_WORN,),
                )
            elif state is WearState.UNKNOWN:
                yield Finding(
                    rule_id=R11_RULE_ID,
                    rule_version=R11_RULE_VERSION,
                    severity=R11_SEVERITY,
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
            if ctx.ppe.vest_state(track, t) is WearState.WORN:
                yield track.world_id

    def clears_all(self, ctx: RuleContext) -> bool:
        return not self._required(ctx)
