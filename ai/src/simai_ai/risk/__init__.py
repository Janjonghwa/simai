"""Risk Engine (Tier 0·EC2 컨테이너 공통, 명세 3.2·7장).

simai_ai.contracts 의 생성 타입만 입력으로 받고 다른 기능 패키지를 import 하지 않는다.
extras 없는 기본 설치만으로 import·테스트가 돌아야 한다. 현재 시각은 주입받는다(clock).

| 모듈 | 내용 |
|---|---|
| clock | Clock 인터페이스, SystemClock(LIVE), FixedClock(재생·벡터) |
| common | 판정 출처 Origin(LIVE 만 알림), 심각도 Severity |
| world | I1 world_state payload 최소 dataclass. contracts 생성 타입(S15P31A101-24)이 오면 교체 |
| zone | 구역 임시 형식. 구성 묶음 계약이 생기면 교체 |
| rule | 규칙 공통 형식: RuleSpec, RuleContext, Finding(사람 대상·구역 대상), Rule |
| engine | RiskEngine, Evaluation, AlertCandidate(LIVE 만 생성 가능) |
| registry | (규칙 ID, 버전) → 규칙 등록부 |
| event_id | event_id(UUIDv7) 생성기 EventIdFactory |
| alerting | 심각도 × 신뢰도 → 채널(7.3 표), 현장 경보 신호 LocalAlert·AlarmStop·AlertClear(LIVE 만) |
| lifecycle | RiskEvent 생명주기 LifecycleTracker, 전이 기록 EventTransition(저널 재료) |
| vectors | 공통 테스트 벡터 형식과 실행기 (`python -m simai_ai.risk.vectors <경로>`) |

vectors 는 `python -m` 으로 실행하므로 여기서 import 하지 않는다.
"""

from simai_ai.risk.alerting import (
    AckActor,
    AlarmStop,
    AlertClear,
    Channel,
    ChannelPlan,
    Confidence,
    LocalAlert,
    LocalSignal,
    channels_for,
)
from simai_ai.risk.clock import Clock, FixedClock, SystemClock
from simai_ai.risk.common import Origin, Severity
from simai_ai.risk.engine import AlertCandidate, Evaluation, NonLiveAlertError, RiskEngine, RuleContractError
from simai_ai.risk.event_id import EventIdFactory
from simai_ai.risk.lifecycle import (
    AckRecord,
    Change,
    EventKey,
    EventState,
    EventTransition,
    IdentityCorrection,
    Lifecycle,
    LifecycleError,
    LifecyclePolicy,
    LifecycleStep,
    LifecycleTracker,
    ObservedTracksPolicy,
    TrackEvidence,
    Verification,
    VerificationPolicy,
)
from simai_ai.risk.registry import RuleRegistry, UnknownRuleError, default_registry
from simai_ai.risk.rule import Finding, FindingTarget, Rule, RuleContext, RuleSpec
from simai_ai.risk.world import WorldState, WorldTrack
from simai_ai.risk.zone import Zone, ZoneJudgeType

__all__ = [
    "AckActor",
    "AckRecord",
    "AlarmStop",
    "AlertCandidate",
    "AlertClear",
    "Change",
    "Channel",
    "ChannelPlan",
    "Clock",
    "Confidence",
    "Evaluation",
    "EventIdFactory",
    "EventKey",
    "EventState",
    "EventTransition",
    "Finding",
    "FindingTarget",
    "FixedClock",
    "IdentityCorrection",
    "Lifecycle",
    "LifecycleError",
    "LifecyclePolicy",
    "LifecycleStep",
    "LifecycleTracker",
    "LocalAlert",
    "LocalSignal",
    "NonLiveAlertError",
    "ObservedTracksPolicy",
    "Origin",
    "RiskEngine",
    "Rule",
    "RuleContext",
    "RuleContractError",
    "RuleRegistry",
    "RuleSpec",
    "Severity",
    "SystemClock",
    "TrackEvidence",
    "UnknownRuleError",
    "Verification",
    "VerificationPolicy",
    "WorldState",
    "WorldTrack",
    "Zone",
    "ZoneJudgeType",
    "channels_for",
    "default_registry",
]
