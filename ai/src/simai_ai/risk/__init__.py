"""Risk Engine (Tier 0·EC2 컨테이너 공통, 명세 3.2·7장).

simai_ai.contracts 의 생성 타입만 입력으로 받고 다른 기능 패키지를 import 하지 않는다.
extras 없는 기본 설치만으로 import·테스트가 돌아야 한다. 현재 시각은 주입받는다(clock).

| 모듈 | 내용 |
|---|---|
| clock | Clock 인터페이스, SystemClock(LIVE), FixedClock(재생·벡터) |
| common | 판정 출처 Origin(LIVE 만 알림), 심각도 Severity, 확인 필요 이유 ConfidenceReason(7.1) |
| world | I1 world_state payload 최소 dataclass. contracts 생성 타입(S15P31A101-24)이 오면 교체 |
| monitoring | 감시 상태 입력(카메라 상태·구역별 관측 카메라). world_state 에 없는 R7 입력 |
| zone | 구역 임시 형식. 구성 묶음 계약이 생기면 교체 |
| zone_geometry | 구역 수평 기하(점·다각형), 위치 불확실성 범위 HorizontalRange(k=2 타원 + 점유 반경 0.3 m, 7.1) |
| rule | 규칙 공통 형식: RuleSpec, RuleContext, Finding(사람 대상·구역 대상), Rule |
| engine | RiskEngine, Evaluation, AlertCandidate(LIVE 만 생성 가능) |
| registry | (규칙 ID, 버전) → 규칙 등록부 |
| rules | 구체 규칙: R1(ZoneEntryRule), R6(ObservationLossHold, 구역 규칙을 감쌈), R7(ZoneMonitoringRule) |
| event_id | event_id(UUIDv7) 생성기 EventIdFactory |
| alerting | 심각도 × 신뢰도 → 채널(7.3 표), 현장 경보 신호 LocalAlert·AlarmStop·AlertClear(LIVE 만) |
| lifecycle | RiskEvent 생명주기 LifecycleTracker(발생 지속 조건·재개 포함), 전이 기록 EventTransition(저널 재료) |
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
from simai_ai.risk.common import ConfidenceReason, Origin, Severity
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
    Persistence,
    TrackEvidence,
    Verification,
    VerificationPolicy,
)
from simai_ai.risk.monitoring import CameraMonitor, CameraStatus, MonitoringState
from simai_ai.risk.registry import RuleRegistry, UnknownRuleError, default_registry
from simai_ai.risk.rule import Finding, FindingTarget, Rule, RuleContext, RuleSpec
from simai_ai.risk.rules.r1_zone_entry import ZoneEntryRule
from simai_ai.risk.rules.r6_observation_loss import ObservationLossHold
from simai_ai.risk.rules.r7_monitoring import ZoneMonitoringRule
from simai_ai.risk.world import WorldState, WorldTrack
from simai_ai.risk.zone import Zone, ZoneJudgeType
from simai_ai.risk.zone_geometry import HorizontalRange

__all__ = [
    "AckActor",
    "AckRecord",
    "AlarmStop",
    "AlertCandidate",
    "AlertClear",
    "CameraMonitor",
    "CameraStatus",
    "Change",
    "Channel",
    "ChannelPlan",
    "Clock",
    "Confidence",
    "ConfidenceReason",
    "Evaluation",
    "EventIdFactory",
    "EventKey",
    "EventState",
    "EventTransition",
    "Finding",
    "FindingTarget",
    "FixedClock",
    "HorizontalRange",
    "IdentityCorrection",
    "Lifecycle",
    "LifecycleError",
    "LifecyclePolicy",
    "LifecycleStep",
    "LifecycleTracker",
    "LocalAlert",
    "LocalSignal",
    "MonitoringState",
    "NonLiveAlertError",
    "ObservationLossHold",
    "ObservedTracksPolicy",
    "Origin",
    "Persistence",
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
    "ZoneEntryRule",
    "ZoneJudgeType",
    "ZoneMonitoringRule",
    "channels_for",
    "default_registry",
]
