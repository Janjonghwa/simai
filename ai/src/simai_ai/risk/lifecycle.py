"""RiskEvent 생명주기 (명세 3.2 이벤트 소유권·식별자 규칙·저널 계약, 6.4, 7.3, 7.4, 5.2 ID 교정).

엔진의 판정(Evaluation)과 같은 시각의 World 상태를 받아 이벤트 전이를 확정한다. 저장·전송은 하지 않고,
저널에 그대로 넘길 수 있는 순수 데이터(EventTransition)와 현장 경보 신호(alerting.LocalSignal)를 돌려준다.
LIVE 이벤트의 생명주기 정본은 RunPod Tier 0 의 이 추적기 하나다. EC2 는 전이를 새로 만들지 않는다.

**생명주기.** RAISED → UPDATED → RESOLVED (→ 재개 시 UPDATED).
- 발생: 이벤트 키(EventKey)에 해당하는 발생 판정(Finding.holding=False)이 발생 지속 조건(6.4)을 채우면 만든다.
  초기값은 판정 프레임 최근 3개 중 2개(정상 10Hz 에서 +100~200ms 를 더한다. 운영 p95 는 실측으로 판정한다).
  재개 시간은 지속 조건을 채운(이벤트를 여는) 판정의 관측 시각으로 잰다. 프레임은 관측 시각
  (observation_time)이 앞으로 간 판정 하나다. 같은 관측을 다시 판정해도 프레임을 세지 않는다(멈춘 영상 하나로
  지속 조건을 채우지 않게). 규칙별로 바꿀 수 있고 R7 은 즉시(1프레임)다(아래 LifecyclePolicy).
- 지속 대기 중 관측 상실·정지: 발생 판정이 한 번 이상 있었는데(지속 조건 미충족) 이후 판정 주기에 새 관측의 발생
  판정이 없거나 관측 시각이 멈췄고(영상 정지) 대상을 유효하게 관측할 수 없으면(UNKNOWN·트랙 없음·gap·lag 초과)
  그 자리에서 발생시킨다. 프레임 집계와 달리 이 검사는 관측 시각이 멈춰도 판정 주기마다 한다(주입 시계는 간다, 6.1).
  마지막 발생 판정으로 만들고 검증 상태는 UNVERIFIABLE, 확인 필요 이유에 PREDICTED 를 더한다. 진입 직후 관측을 잃은 사람이 R6 holding(유지만 함)만 남아
  경보 없이 끝나는 틈을 막는 안전 쪽 처리다(S15P31A101-73 리뷰).
- 재개(6.4, 7.4): 같은 키가 해소 후 재개 시간(초기값 10초, 관측 시각 기준) 안에 다시 발생 조건을 채우면 새 이벤트를
  만들지 않고 해소된 이벤트를 다시 활성으로 돌린다. 같은 event_id 로 UPDATED 전이(변경 사유 RESUMED, I1 resumed=true)를
  내고 event_sequence 를 이어 올린다. 재개는 새 위험 발생처럼 다룬다: 확인 기록(acks)을 비워 미확인으로 되돌리고
  (이전 확인은 저널에 남는다) CRITICAL·HIGH 면 다시 울린다. 재개 시간이 지난 재발생은 새 이벤트다.
- 유지: 판정이 있으면(holding=True 포함) 해소 확인을 처음부터 다시 센다. holding 은 유지만 하므로 새 이벤트의
  등급을 정하지 않고, 활성 이벤트의 등급을 올리지도 내리지도 않는다(7.2 R6 '원래 규칙 유지', 7.3).
- 해소: 판정이 없고 대상이 유효하게 관측되는(VERIFIED) 상태가 해소 유지 시간 동안 이어질 때만 확정한다.
  시간은 관측 시각(observation_time)으로 잰다. 영상이 멈추면 관측 시각이 멈추므로 해소도 멈춘다.
- 관측 상실: 대상을 유효하게 관측할 수 없으면 검증 상태만 UNVERIFIABLE 이 되고 자동 해소하지 않는다(6.4, G3).
- 구역 대상 해소 근거(6.4, S15P31A101-180): 구역 대상 이벤트(R7 형태)는 사람이 없어 재관측으로 해소를 확인할 수 없다.
  그래서 판정이 사라진 판정에서는 zone_clear_evidence 에 (규칙, 구역) 근거(R7: 그 구역의 정상 관측 카메라 확보)가 있을 때만
  VERIFIED 로 보고 해소를 센다. 구성 묶음 전환으로 규칙·구역이 빠져 판정이 사라진 것, 감시 상태 입력이 없는 것은 근거가
  아니다(UNVERIFIABLE, 자동 해소 없음). 판정이 계속 오는 동안의 검증 상태는 기존 정책(lag)이다. 정상 경로(규칙이 판정에
  남아 있고 조건이 사라지면 근거가 함께 나온다)의 해소 시각·전이는 그대로다.
- 복원 보호(3.2, D-G3-09, S15P31A101-174): 재시작 뒤 restore 로 넣은 구역 대상 이벤트는 해소될 때까지 판정이 오는 동안에도
  근거가 있는 판정에서만 VERIFIED 로 본다(복원 직후 UNVERIFIABLE 이 저하 판정만으로 VERIFIED 로 돌아가지 않게). 사람 대상
  복원 이벤트는 기존 정책대로 대상 World ID 의 재관측으로 확인한다. 복원 보호는 체크포인트·저널에 싣지 않는다. restore 가
  복원하는 모든 활성 이벤트에 다시 걸고 해소 때만 풀리므로 다시 재시작해도 같은 상태가 된다.
- 운영자 종료(S15P31A101-200, close): 근거가 오지 않아 남은 구역 대상 이벤트를 관제 화면 관리자가 닫는다. RESOLVED 전이
  (변경 사유 OPERATOR_CLOSED, close 기록에 누가·왜)로 남기고 검증 상태는 바꾸지 않는다(관측 해소가 아니다). 사람 대상·
  HIGH·CRITICAL·마지막 판정에 조건이 있는 이벤트는 거부한다. 종료한 이벤트는 재개하지 않는다(재발생은 새 이벤트).
- 운영자 상향(S15P31A101-272, escalate): 관리자가 승인한 VLM 상향 의견을 같은 event_id 의 UPDATED 전이(escalation)로 남기고
  규칙 상향과 같이 다시 울린다(3.10). 지금 등급보다 높을 때만 받는다. 올린 등급은 해소될 때까지 바닥값이라 이후 규칙 판정이
  낮추지 않는다(5.4 하향 불가, 명세에 규칙 판정과의 관계가 없어 정한 기본안). 재개하면 바닥값을 비운다.
- 확인 필요(7.1): 이벤트의 confidence_reasons 가 비어 있지 않은 상태다. needs_check 는 그 결과로 맞춰 둔다
  (common.normalize_confidence). 이유 목록이 바뀌면 CONFIDENCE 변경 사유로 UPDATED 를 낸다.
- ACKNOWLEDGED 는 상태가 아니라 속성이다. 확인은 UPDATED 전이(변경 사유 ACKNOWLEDGED)로 저널에 남고
  해소가 아니다. 누가 확인하든(관리자·반장·현장 버튼) 반복 소리 정지(AlarmStop)를 내고 표시는 해소까지 둔다.
  확인은 대상 상태(target_sequence, 확인한 사람이 본 event_sequence)와 함께 기록한다. 현장 경보는 지금 경보 단계
  (마지막 발생·등급 상향·재개, site_alarm_sequence) 이후 상태를 대상으로 한 확인으로만 멈춘다(site_acknowledged, 3.10).
  상향 전 상태에 대한 늦은 확인은 저널에 남지만 상향된 경보 소리를 멈추지 않는다(S15P31A101-161).
- ID 교정(identity_revision 증가, 명시적 교정)은 해소 확인을 처음부터 다시 세게 할 뿐 해소하지 않는다(5.2, G3).
  이벤트에는 발생 당시의 원본 World ID(origin_world_ids)를 보존한다.
- ID 분할(S15P31A101-201, split_identity): 한 World ID W 가 두 사람이었다고 분할되면 W 의 활성 이벤트는 W 에 그대로 두고
  (원본 World ID 보존, 키를 옮기지 않음) 갈라져 나간 새 ID N 을 그 이벤트의 분할 상대로 기억한다. 분할 전 이벤트가 둘 중
  누구의 것인지 모르므로, 그 이벤트는 W 와 N 이 모두 유효하게 관측되고 같은 규칙의 판정이 W·N 어느 쪽에도 없을 때만 해소를
  센다(한쪽 관측만으로 해소하지 않음). N 에 같은 위험이 판정되면 N 의 새 이벤트가 생긴다. 분할 상대는 분할 계보 전체다:
  상대 N 이 다시 M 으로 갈라지면 M 도 상대가 된다(전이적, 리뷰 R1). 상대가 병합되면 남는 ID 로 잇고, 이벤트 자신의 ID 가
  되면 뺀다. 상대가 사라지면(LOST) 관측할 수 없어 해소하지 않는다. 분할 상대는 체크포인트·저널에 싣지 않는다(재시작하면 W 가
  다시 관측되지 않아 복원 이벤트는 UNVERIFIABLE 로 남는다).

**병합(7.4).** 같은 규칙·같은 사람(World ID 묶음)은 이벤트 하나를 갱신한다. 사람 대상은 구역이 바뀌어도 같은
이벤트다(zone_id 는 발생 판정으로 갱신). holding 판정은 활성 이벤트를 유지만 하므로 구역을 바꾸지 않는다.
대표 판정을 고를 때 이벤트의 현재 구역과 같은 holding 을 먼저 고르고, 다른 구역의 holding 만 있으면 유지·확인 필요·
근거만 반영하고 구역과 그 구역에서 나온 등급·수치는 그대로 둔다(R6 이 인접 구역에 걸칠 때). 사람 없는 구역·시스템 판정(R7 형태)은 (규칙, 구역)이 키다. 한 사람에게 걸린 여러
규칙은 group_key 가 같아 화면에서 한 묶음으로 보일 수 있다.

**식별자(3.2).** event_id 는 UUIDv7(event_id.EventIdFactory), gateway_id 는 함께 싣는다. event_sequence 는 이벤트별로
RAISED 가 1 이고 전이마다 1 씩 오른다. 체크포인트 복원(restore) 뒤에도 이어진다.

**출처(7.4).** 추적기 하나는 한 출처만 다룬다. PLANNED·WHAT_IF·REPLAY 도 같은 규칙으로 전이를 만들지만
(보고서·시뮬레이션 화면용) 현장 경보 신호는 절대 내지 않는다. 채널은 alerting.channels_for(7.3 표)로만 정한다.

**확장 지점.** S15P31A101-73 이 아래 지점으로 R6(rules.r6_observation_loss)·R7(rules.r7_monitoring)을 붙였다.
- VerificationPolicy: 검증 가능 판단을 바꿔 끼운다. 기본 ObservedTracksPolicy 는 상태·신원·최신성(6.1)만 본다.
  규칙에 필요한 관측 필드(예: R3 의 Z)가 있으면 그 규칙이 불확실할 때 holding·needs_check 판정을 내거나,
  규칙별 정책으로 바꿔 끼운다.
- 구역 대상 판정(world_ids 없음)은 (규칙, 구역) 키 이벤트가 된다. R7 이 그대로 쓴다.
- R6 처럼 관측을 잃은 사람에게 원래 규칙 판정을 유지하려면 규칙이 holding 판정을 내면 된다.
"""

import logging
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from simai_ai.risk._parse import (
    RiskInputError,
    as_bool,
    as_enum,
    as_int,
    as_mapping,
    as_number,
    as_opt_str,
    as_str,
    as_timestamp_ms,
    reject_unknown_keys,
    require,
)
from simai_ai.risk.alerting import (
    AckActor,
    AlarmStop,
    AlertClear,
    Channel,
    Confidence,
    LocalAlert,
    LocalSignal,
    channels_for,
)
from simai_ai.risk.clock import Clock
from simai_ai.risk.common import ConfidenceReason, Origin, Severity, normalize_confidence
from simai_ai.risk.engine import Evaluation
from simai_ai.risk.event_id import EventIdFactory
from simai_ai.risk.rule import Finding, FindingTarget
from simai_ai.risk.rules.r7_monitoring import R7_RULE_ID
from simai_ai.risk.world import IdentityConfidence, ObservationState, WorldState, WorldTrack

_log = logging.getLogger(__name__)

# 지속 대기 중 관측 상실로 발생시킨 이벤트의 근거 코드(Finding.reasons 끝에 붙인다).
REASON_PENDING_OBSERVATION_LOST = "observation_lost_before_persistence"

_SEVERITY_RANK = {Severity.INFO: 0, Severity.CAUTION: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


class Lifecycle(StrEnum):
    """RiskEvent 생명주기(명세 6.2). ACKNOWLEDGED 는 여기 없다(속성이다)."""

    RAISED = "RAISED"
    UPDATED = "UPDATED"
    RESOLVED = "RESOLVED"


class Verification(StrEnum):
    """해소 여부를 관측으로 확인할 수 있는지(명세 6.2)."""

    VERIFIED = "VERIFIED"
    UNVERIFIABLE = "UNVERIFIABLE"


class Change(StrEnum):
    """전이 사유. 한 전이에 여러 개가 붙을 수 있다(정해진 순서로 정렬)."""

    RAISED = "RAISED"
    RESUMED = "RESUMED"  # 해소 후 재개 시간 안 재발생으로 같은 이벤트를 다시 활성으로 돌림(6.4)
    RESTORED = "RESTORED"
    IDENTITY_REVISED = "IDENTITY_REVISED"  # world_state 의 identity_revision 증가
    IDENTITY_CORRECTED = "IDENTITY_CORRECTED"  # 명시적 교정으로 World ID 가 바뀜
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"  # 교정 대상 키에 이미 이벤트가 있어 연결만 남김
    SEVERITY = "SEVERITY"
    CONFIDENCE = "CONFIDENCE"
    VERIFICATION = "VERIFICATION"
    ZONE = "ZONE"
    METRICS = "METRICS"
    VERSION = "VERSION"  # 규칙·구성·보정·Scene 버전이 바뀜(근거 감사)
    ACKNOWLEDGED = "ACKNOWLEDGED"
    CLEARED = "CLEARED"  # 해소 확정
    OPERATOR_CLOSED = "OPERATOR_CLOSED"  # 관리자의 구역 대상 이벤트 종료(event_close, S15P31A101-200). 관측 해소가 아니다
    ESCALATED = "ESCALATED"  # 관리자가 승인한 VLM 상향 의견으로 등급을 올림(event_escalate, S15P31A101-272). 판정이 아니다


_CHANGE_ORDER = {c: i for i, c in enumerate(Change)}


@dataclass(frozen=True)
class EventKey:
    """이벤트 병합 키(명세 7.4). 사람 대상은 (규칙, World ID 묶음), 구역 대상은 (규칙, 구역)."""

    rule_id: str
    world_ids: tuple[str, ...]
    zone_id: str | None

    @classmethod
    def of(cls, finding: Finding) -> "EventKey":
        if finding.target is FindingTarget.PERSON:
            return cls(finding.rule_id, finding.world_ids, None)
        return cls(finding.rule_id, (), finding.zone_id)

    @property
    def target(self) -> FindingTarget:
        return FindingTarget.PERSON if self.world_ids else FindingTarget.ZONE

    @property
    def group_key(self) -> str:
        """화면 묶음 키. 한 사람에게 걸린 여러 규칙이 같은 값을 갖는다."""
        if self.world_ids:
            return "person:" + ",".join(self.world_ids)
        return f"zone:{self.zone_id}"

    @property
    def sort_key(self) -> tuple[str, tuple[str, ...], str]:
        return (self.rule_id, self.world_ids, self.zone_id or "")


@dataclass(frozen=True)
class AckRecord:
    """확인 기록(수정 6). acked_at 은 확인한 시각(보낸 쪽 기준), recorded_at 은 Tier 0 가 기록한 시각."""

    actor: AckActor
    actor_id: str
    acked_at: int
    recorded_at: int
    # 확인 대상 상태의 event_sequence(확인한 사람이 본 상태). 현장 확인은 site_ack.event_sequence, 대상이 없으면
    # 기록할 때의 마지막 전이 sequence. 현장 경보 소리를 멈추는지 판단할 때 쓴다(EventState.site_acknowledged).
    target_sequence: int | None = None
    # 확인이 들어온 계약 채널(CONTROL_ROOM·FOREMAN_PWA·SITE_BUTTON). 받은 값을 그대로 남긴다. 모르면 None.
    channel: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "actor": str(self.actor),
            "actor_id": self.actor_id,
            "acked_at": self.acked_at,
            "recorded_at": self.recorded_at,
            "target_sequence": self.target_sequence,
            "channel": self.channel,
        }

    @classmethod
    def from_payload(cls, doc: Any, where: str = "AckRecord") -> "AckRecord":
        """to_payload 의 역(체크포인트·저널 복원용). 모르는 필드·빠진 필드·형식 위반은 RiskInputError."""
        doc = as_mapping(doc, where)
        reject_unknown_keys(doc, set(_ACK_KEYS), where)
        for key in _ACK_KEYS:
            require(doc, key, where)
        target = doc["target_sequence"]
        return cls(
            actor=as_enum(AckActor, doc["actor"], f"{where}.actor"),
            actor_id=as_str(doc["actor_id"], f"{where}.actor_id"),
            acked_at=as_timestamp_ms(doc["acked_at"], f"{where}.acked_at"),
            recorded_at=as_timestamp_ms(doc["recorded_at"], f"{where}.recorded_at"),
            target_sequence=None if target is None else as_int(target, f"{where}.target_sequence", minimum=1),
            channel=as_opt_str(doc["channel"], f"{where}.channel"),
        )


_ACK_KEYS = ("actor", "actor_id", "acked_at", "recorded_at", "target_sequence", "channel")

# 운영자 종료를 받는 계약 채널·역할(contracts/messages/event_close.schema.json). 반장 PWA 의 '조치 완료'는 해소가 아니다(8.4).
CLOSE_CHANNEL = "CONTROL_ROOM"
CLOSE_ACTOR_ROLE = "ADMIN"
CLOSE_REASON_MAX = 500  # 계약 event_close.close.reason maxLength


class CloseReject(StrEnum):
    """운영자 종료 거부 사유(계약 event_close_result.reject_reason)."""

    NOT_ACTIVE = "NOT_ACTIVE"  # 활성 이벤트가 아니다(모르는 ID, 이미 해소·종료)
    TARGET_NOT_ZONE = "TARGET_NOT_ZONE"  # 사람 대상 이벤트는 재관측으로만 해소한다
    SEVERITY_NOT_ALLOWED = "SEVERITY_NOT_ALLOWED"  # HIGH·CRITICAL 은 닫지 않는다(명세에 기준 없음, 안전 쪽)
    CONDITION_PRESENT = "CONDITION_PRESENT"  # 마지막 판정에 그 이벤트의 위험 판정이 있다(지금 조건이 성립)


@dataclass(frozen=True)
class CloseRequest:
    """I1 event_close 하나(관리자가 구역 대상 이벤트를 닫는 명령). close_id 는 EC2 가 발급한 멱등 키다."""

    event_id: str
    close_id: str
    actor_id: str
    requested_at: int
    reason: str

    @classmethod
    def from_contract(cls, payload: Any) -> "CloseRequest":
        """event_close payload → CloseRequest. 계약이 받지 않는 채널·역할(반장 PWA 등)·빈 값은 RiskInputError."""
        payload = as_mapping(payload, "event_close")
        close = as_mapping(payload.get("close"), "event_close.close")
        if close.get("channel") != CLOSE_CHANNEL or close.get("actor_role") != CLOSE_ACTOR_ROLE:
            raise RiskInputError(
                f"event_close: 채널·역할 ({close.get('channel')}, {close.get('actor_role')}) 은 받지 않는다(관제 화면 관리자만)"
            )
        reason = as_str(close.get("reason"), "event_close.close.reason")
        if len(reason) > CLOSE_REASON_MAX:
            raise RiskInputError(f"event_close.close.reason: {CLOSE_REASON_MAX}자를 넘는다 ({len(reason)}자)")
        return cls(
            event_id=as_str(payload.get("event_id"), "event_close.event_id"),
            close_id=as_str(close.get("close_id"), "event_close.close.close_id"),
            actor_id=as_str(close.get("actor_id"), "event_close.close.actor_id"),
            requested_at=as_timestamp_ms(close.get("requested_at"), "event_close.close.requested_at"),
            reason=reason,
        )


@dataclass(frozen=True)
class CloseRecord:
    """운영자 종료 기록(종료 전이에 싣는다). requested_at 은 보낸 쪽 기준, recorded_at 은 Tier 0 가 기록한 시각."""

    close_id: str
    actor_id: str
    requested_at: int
    recorded_at: int
    reason: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "close_id": self.close_id,
            "channel": CLOSE_CHANNEL,
            "actor_role": CLOSE_ACTOR_ROLE,
            "actor_id": self.actor_id,
            "requested_at": self.requested_at,
            "recorded_at": self.recorded_at,
            "reason": self.reason,
        }


class EscalateReject(StrEnum):
    """운영자 상향 거부 사유(계약 event_escalate_result.reject_reason 중 추적기가 정하는 것. JOURNAL_UNAVAILABLE 은 저널 쪽)."""

    NOT_ACTIVE = "NOT_ACTIVE"  # 활성 이벤트가 아니다(모르는 ID, 이미 해소·종료)
    NOT_UPWARD = "NOT_UPWARD"  # target_severity 가 지금 등급보다 높지 않다(같은 등급, 그사이 규칙이 이미 올림)


# 계약 operator_escalation(contracts/common/defs.schema.json). 값은 받은 그대로 전이 escalation 에 싣는다.
_ESCALATION_REQUIRED = (
    "escalation_id",
    "basis",
    "assessment_job_id",
    "target_severity",
    "channel",
    "actor_role",
    "actor_id",
    "requested_at",
)
_ESCALATION_KEYS = frozenset(_ESCALATION_REQUIRED) | {"note"}
ESCALATION_BASIS = "VLM_PROPOSAL"
ESCALATION_TARGETS = (Severity.CRITICAL, Severity.HIGH, Severity.CAUTION)
ESCALATION_NOTE_MAX = 500  # 계약 operator_escalation.note maxLength


@dataclass(frozen=True)
class EscalationRequest:
    """I1 event_escalate 하나(관리자가 승인한 VLM 상향 의견). escalation 은 계약 operator_escalation 을 검사한 사본이다."""

    event_id: str
    escalation: Mapping[str, Any] = field(hash=False)

    @property
    def escalation_id(self) -> str:
        return str(self.escalation["escalation_id"])

    @property
    def target_severity(self) -> Severity:
        return Severity(self.escalation["target_severity"])

    @classmethod
    def from_contract(cls, payload: Any) -> "EscalationRequest":
        """event_escalate payload → EscalationRequest. 계약이 받지 않는 값(반장 PWA, 모르는 근거·필드, INFO 등)은 RiskInputError."""
        payload = as_mapping(payload, "event_escalate")
        doc = as_mapping(payload.get("escalation"), "event_escalate.escalation")
        reject_unknown_keys(doc, set(_ESCALATION_KEYS), "event_escalate.escalation")
        for key in _ESCALATION_REQUIRED:
            require(doc, key, "event_escalate.escalation")
        if doc["channel"] != CLOSE_CHANNEL or doc["actor_role"] != CLOSE_ACTOR_ROLE:
            raise RiskInputError(
                f"event_escalate: 채널·역할 ({doc['channel']}, {doc['actor_role']}) 은 받지 않는다(관제 화면 관리자만)"
            )
        if doc["basis"] != ESCALATION_BASIS:
            raise RiskInputError(f"event_escalate.escalation.basis: {ESCALATION_BASIS} 만 받는다 ({doc['basis']!r})")
        target = as_enum(Severity, doc["target_severity"], "event_escalate.escalation.target_severity")
        if target not in ESCALATION_TARGETS:
            raise RiskInputError(f"event_escalate.escalation.target_severity: {target} 로는 올리지 않는다")
        out: dict[str, Any] = {
            "escalation_id": as_str(doc["escalation_id"], "event_escalate.escalation.escalation_id"),
            "basis": ESCALATION_BASIS,
            "assessment_job_id": as_str(doc["assessment_job_id"], "event_escalate.escalation.assessment_job_id"),
            "target_severity": str(target),
            "channel": CLOSE_CHANNEL,
            "actor_role": CLOSE_ACTOR_ROLE,
            "actor_id": as_str(doc["actor_id"], "event_escalate.escalation.actor_id"),
            "requested_at": as_timestamp_ms(doc["requested_at"], "event_escalate.escalation.requested_at"),
        }
        if "note" in doc:
            note = as_str(doc["note"], "event_escalate.escalation.note")
            if len(note) > ESCALATION_NOTE_MAX:
                raise RiskInputError(f"event_escalate.escalation.note: {ESCALATION_NOTE_MAX}자를 넘는다 ({len(note)}자)")
            out["note"] = note
        return cls(event_id=as_str(payload.get("event_id"), "event_escalate.event_id"), escalation=MappingProxyType(out))


@dataclass(frozen=True)
class TrackEvidence:
    """전이 근거(명세 6.4): 대상 한 명의 카메라·좌표·관측 상태. 대상이 World 상태에 없으면 present=False."""

    world_id: str
    present: bool
    observation_state: ObservationState | None = None
    identity_revision: int | None = None
    identity_confidence: IdentityConfidence | None = None
    t_last_valid: int | None = None
    cameras: tuple[str, ...] = ()
    position_xy: tuple[float, ...] | None = None
    pelvis_xyz: tuple[float, ...] | None = None
    ground_anchor_xyz: tuple[float, ...] | None = None
    head_z: float | None = None

    @classmethod
    def of(cls, world_id: str, track: WorldTrack | None) -> "TrackEvidence":
        if track is None:
            return cls(world_id=world_id, present=False)
        return cls(
            world_id=world_id,
            present=True,
            observation_state=track.observation_state,
            identity_revision=track.identity_revision,
            identity_confidence=track.identity_confidence,
            t_last_valid=track.t_last_valid,
            cameras=track.cameras,
            position_xy=track.position_xy,
            pelvis_xyz=track.pelvis_xyz,
            ground_anchor_xyz=track.ground_anchor_xyz,
            head_z=track.head_z,
        )

    def to_payload(self) -> dict[str, Any]:
        def vec(v: tuple[float, ...] | None) -> list[float] | None:
            return None if v is None else list(v)

        return {
            "world_id": self.world_id,
            "present": self.present,
            "observation_state": None if self.observation_state is None else str(self.observation_state),
            "identity_revision": self.identity_revision,
            "identity_confidence": None if self.identity_confidence is None else str(self.identity_confidence),
            "t_last_valid": self.t_last_valid,
            "cameras": list(self.cameras),
            "position_xy": vec(self.position_xy),
            "pelvis_xyz": vec(self.pelvis_xyz),
            "ground_anchor_xyz": vec(self.ground_anchor_xyz),
            "head_z": self.head_z,
        }


@dataclass(frozen=True)
class EventState:
    """활성 이벤트의 현재 상태. 체크포인트와 event_state_snapshot(3.2 재동기화)의 재료다."""

    event_id: str
    gateway_id: str
    origin: Origin
    event_sequence: int
    lifecycle: Lifecycle
    key: EventKey
    rule_version: int
    severity: Severity
    needs_check: bool
    verification: Verification
    world_ids: tuple[str, ...]
    origin_world_ids: tuple[str, ...]
    zone_id: str | None
    metrics: Mapping[str, float] = field(hash=False)
    reasons: tuple[str, ...]
    raised_at: int
    evaluated_at: int
    observation_time: int
    config_version: str
    calibration_version: str
    scene_version: str
    identity_revisions: Mapping[str, int] = field(hash=False)
    acks: tuple[AckRecord, ...] = ()
    related_event_id: str | None = None
    local_alerted: bool = False
    confidence_reasons: tuple[ConfidenceReason, ...] = ()
    resume_count: int = 0  # 해소 후 재개한 횟수(6.4)
    # 현장 경보(소리)를 마지막으로 (다시) 시작한 전이의 event_sequence: 발생·등급 상향·재개(3.10). 현장 경보를 낸 적
    # 없으면 None.
    site_alarm_sequence: int | None = None
    # 운영자 상향 바닥값(S15P31A101-272): 이 이벤트가 해소될 때까지 규칙 판정이 등급을 이보다 낮추지 않는다. 재개하면 비운다.
    escalation_floor: Severity | None = None

    def __post_init__(self) -> None:
        # 확인 필요 = 이유 목록이 비어 있지 않음(7.1). 이유 없는 needs_check(이전 체크포인트)도 여기서 맞춘다.
        needs_check, reasons = normalize_confidence(
            self.needs_check, self.confidence_reasons, f"EventState {self.key.rule_id}"
        )
        object.__setattr__(self, "needs_check", needs_check)
        object.__setattr__(self, "confidence_reasons", reasons)

    @property
    def rule_id(self) -> str:
        return self.key.rule_id

    @property
    def confidence(self) -> Confidence:
        return Confidence.from_needs_check(self.needs_check)

    @property
    def acknowledged(self) -> bool:
        return bool(self.acks)

    @property
    def site_acknowledged(self) -> bool:
        """현장 경보 소리를 멈출 확인이 있는지(health.active_site_alerts·local_alert 의 acknowledged, 3.10).

        지금 경보 단계(site_alarm_sequence) 이후 상태를 대상으로 한 확인만 센다. 등급이 올라가기 전 상태에 대한 확인은
        저널에 남고 acknowledged(I1)에는 반영되지만 상향된 경보 소리는 멈추지 않는다.
        """
        if self.site_alarm_sequence is None:
            return self.acknowledged
        return any(a.target_sequence is None or a.target_sequence >= self.site_alarm_sequence for a in self.acks)

    @property
    def group_key(self) -> str:
        return self.key.group_key

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "gateway_id": self.gateway_id,
            "source": str(self.origin),
            "event_sequence": self.event_sequence,
            "lifecycle": str(self.lifecycle),
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "severity": str(self.severity),
            "confidence": str(self.confidence),
            "needs_check": self.needs_check,
            "confidence_reasons": [str(r) for r in self.confidence_reasons],
            "verification": str(self.verification),
            "world_ids": list(self.world_ids),
            "origin_world_ids": list(self.origin_world_ids),
            "zone_id": self.zone_id,
            "group_key": self.group_key,
            "metrics": dict(self.metrics),
            "reasons": list(self.reasons),
            "resume_count": self.resume_count,
            "raised_at": self.raised_at,
            "evaluated_at": self.evaluated_at,
            "observation_time": self.observation_time,
            "config_version": self.config_version,
            "calibration_version": self.calibration_version,
            "scene_version": self.scene_version,
            "identity_revisions": dict(self.identity_revisions),
            "acknowledged": self.acknowledged,
            "acks": [a.to_payload() for a in self.acks],
            "related_event_id": self.related_event_id,
            "local_alerted": self.local_alerted,
            # 저널·체크포인트에서 되살린 상태로도 site_acknowledged 가 맞도록 싣는다(상향 전 확인이 상향 경보를 멈추지
            # 않게). 복원하는 쪽은 이 값과 acks[].target_sequence 를 함께 읽어야 한다(S15P31A101-161).
            "site_alarm_sequence": self.site_alarm_sequence,
        }

    def to_record(self) -> dict[str, Any]:
        """체크포인트·저널 meta 에 남기는 완전한 상태(S15P31A101-51). to_payload 에 병합 키(key)를 더한 것이다.

        from_record 로 같은 EventState 를 되살린다. 계약 형식이 아니라 Tier 0 내부 형식이다.
        """
        doc = self.to_payload()
        doc["key"] = {"rule_id": self.key.rule_id, "world_ids": list(self.key.world_ids), "zone_id": self.key.zone_id}
        if self.escalation_floor is not None:  # 상향이 없으면 싣지 않는다(이전 체크포인트·저널 기록과 같은 형식)
            doc["escalation_floor"] = str(self.escalation_floor)
        return doc

    @classmethod
    def from_record(cls, doc: Any, where: str = "EventState") -> "EventState":
        """to_record 의 역. 빠진 필드·모르는 필드·형식 위반·파생 필드 불일치는 RiskInputError(지어내지 않는다).

        되살린 상태를 다시 to_record 해 입력과 같은지 본다. 그래서 파생 필드(confidence, acknowledged, group_key,
        rule_id, source)와 확인 필요 이유가 원래 값과 어긋난 기록(손상)은 받지 않는다.
        """
        doc = as_mapping(doc, where)
        reject_unknown_keys(doc, set(_STATE_KEYS) | {"escalation_floor"}, where)
        for key in _STATE_KEYS:
            require(doc, key, where)
        key_doc = as_mapping(doc["key"], f"{where}.key")
        reject_unknown_keys(key_doc, {"rule_id", "world_ids", "zone_id"}, f"{where}.key")
        for name in ("rule_id", "world_ids", "zone_id"):
            require(key_doc, name, f"{where}.key")
        key = EventKey(
            rule_id=as_str(key_doc["rule_id"], f"{where}.key.rule_id"),
            world_ids=_str_tuple(key_doc["world_ids"], f"{where}.key.world_ids"),
            zone_id=as_opt_str(key_doc["zone_id"], f"{where}.key.zone_id"),
        )
        site_alarm = doc["site_alarm_sequence"]
        try:
            state = cls(
                event_id=as_str(doc["event_id"], f"{where}.event_id"),
                gateway_id=as_str(doc["gateway_id"], f"{where}.gateway_id"),
                origin=as_enum(Origin, doc["source"], f"{where}.source"),
                event_sequence=as_int(doc["event_sequence"], f"{where}.event_sequence", minimum=1),
                lifecycle=as_enum(Lifecycle, doc["lifecycle"], f"{where}.lifecycle"),
                key=key,
                rule_version=as_int(doc["rule_version"], f"{where}.rule_version"),
                severity=as_enum(Severity, doc["severity"], f"{where}.severity"),
                needs_check=as_bool(doc["needs_check"], f"{where}.needs_check"),
                verification=as_enum(Verification, doc["verification"], f"{where}.verification"),
                world_ids=_str_tuple(doc["world_ids"], f"{where}.world_ids"),
                origin_world_ids=_str_tuple(doc["origin_world_ids"], f"{where}.origin_world_ids"),
                zone_id=as_opt_str(doc["zone_id"], f"{where}.zone_id"),
                metrics=MappingProxyType(
                    {
                        as_str(k, f"{where}.metrics"): as_number(v, f"{where}.metrics.{k}")
                        for k, v in as_mapping(doc["metrics"], f"{where}.metrics").items()
                    }
                ),
                reasons=_str_tuple(doc["reasons"], f"{where}.reasons"),
                raised_at=as_timestamp_ms(doc["raised_at"], f"{where}.raised_at"),
                evaluated_at=as_timestamp_ms(doc["evaluated_at"], f"{where}.evaluated_at"),
                observation_time=as_timestamp_ms(doc["observation_time"], f"{where}.observation_time"),
                config_version=as_str(doc["config_version"], f"{where}.config_version"),
                calibration_version=as_str(doc["calibration_version"], f"{where}.calibration_version"),
                scene_version=as_str(doc["scene_version"], f"{where}.scene_version"),
                identity_revisions=MappingProxyType(
                    {
                        as_str(k, f"{where}.identity_revisions"): as_int(
                            v, f"{where}.identity_revisions.{k}", minimum=0
                        )
                        for k, v in as_mapping(doc["identity_revisions"], f"{where}.identity_revisions").items()
                    }
                ),
                acks=tuple(
                    AckRecord.from_payload(a, f"{where}.acks[{i}]")
                    for i, a in enumerate(_sequence(doc["acks"], f"{where}.acks"))
                ),
                related_event_id=as_opt_str(doc["related_event_id"], f"{where}.related_event_id"),
                local_alerted=as_bool(doc["local_alerted"], f"{where}.local_alerted"),
                confidence_reasons=tuple(
                    as_enum(ConfidenceReason, r, f"{where}.confidence_reasons[{i}]")
                    for i, r in enumerate(_sequence(doc["confidence_reasons"], f"{where}.confidence_reasons"))
                ),
                resume_count=as_int(doc["resume_count"], f"{where}.resume_count", minimum=0),
                site_alarm_sequence=None
                if site_alarm is None
                else as_int(site_alarm, f"{where}.site_alarm_sequence", minimum=1),
                escalation_floor=None
                if doc.get("escalation_floor") is None
                else as_enum(Severity, doc["escalation_floor"], f"{where}.escalation_floor"),
            )
        except ValueError as exc:  # normalize_confidence 등
            if isinstance(exc, RiskInputError):
                raise
            raise RiskInputError(f"{where}: {exc}") from exc
        if state.to_record() != _plain(doc):
            raise RiskInputError(f"{where}: 파생 필드가 상태와 맞지 않는다(손상된 기록)")
        return state


_STATE_KEYS = (
    "event_id",
    "gateway_id",
    "source",
    "event_sequence",
    "lifecycle",
    "rule_id",
    "rule_version",
    "severity",
    "confidence",
    "needs_check",
    "confidence_reasons",
    "verification",
    "world_ids",
    "origin_world_ids",
    "zone_id",
    "group_key",
    "metrics",
    "reasons",
    "resume_count",
    "raised_at",
    "evaluated_at",
    "observation_time",
    "config_version",
    "calibration_version",
    "scene_version",
    "identity_revisions",
    "acknowledged",
    "acks",
    "related_event_id",
    "local_alerted",
    "site_alarm_sequence",
    "key",
)


def _sequence(value: Any, where: str) -> tuple[Any, ...]:
    """JSON 배열(list) 또는 읽기 전용으로 얼린 배열(tuple, 저널 meta)."""
    if not isinstance(value, list | tuple):
        raise RiskInputError(f"{where}: 배열이어야 한다 (받은 값: {type(value).__name__})")
    return tuple(value)


def _str_tuple(value: Any, where: str) -> tuple[str, ...]:
    return tuple(as_str(v, f"{where}[{i}]") for i, v in enumerate(_sequence(value, where)))


def _plain(value: Any) -> Any:
    """Mapping·tuple 을 dict·list 로 바꾼 복사본(기록 비교용)."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


@dataclass(frozen=True)
class EventTransition:
    """확정된 전이 하나(I1 risk_event_transition 의 재료, 저널 기록 단위).

    occurred_at 은 전이를 확정한 주입 시계 시각이다. 판정 근거 시각은 observation_time·evaluated_at 이다.
    evidence 는 전이 시점의 대상별 근거(카메라·좌표)다. 전송 여부(resent)와 journal_offset 은 저널이 붙인다.
    """

    state: EventState
    transition: Lifecycle
    changes: tuple[Change, ...]
    occurred_at: int
    evidence: tuple[TrackEvidence, ...] = ()
    ack: AckRecord | None = None
    evidence_ref: str | None = None
    close: CloseRecord | None = None  # 운영자 종료 전이에만
    escalation: Mapping[str, Any] | None = field(default=None, hash=False)  # 운영자 상향 전이에만(계약 operator_escalation)

    @property
    def event_id(self) -> str:
        return self.state.event_id

    @property
    def event_sequence(self) -> int:
        return self.state.event_sequence

    def to_payload(self) -> dict[str, Any]:
        doc = self._payload()
        if self.close is not None:  # 종료 전이에만 싣는다(다른 전이의 기록 형식은 그대로)
            doc["close"] = self.close.to_payload()
        if self.escalation is not None:  # 상향 전이에만. 받은 값 그대로(outbox 가 계약 escalation 으로 그대로 보낸다)
            doc["escalation"] = dict(self.escalation)
        return doc

    def _payload(self) -> dict[str, Any]:
        s = self.state
        return {
            "event_id": s.event_id,
            "event_sequence": s.event_sequence,
            "transition": str(self.transition),
            "changes": [str(c) for c in self.changes],
            "occurred_at": self.occurred_at,
            "gateway_id": s.gateway_id,
            "observation_time": s.observation_time,
            "evaluated_at": s.evaluated_at,
            "rule_id": s.rule_id,
            "rule_version": s.rule_version,
            "config_version": s.config_version,
            "calibration_version": s.calibration_version,
            "scene_version": s.scene_version,
            "source": str(s.origin),
            "severity": str(s.severity),
            "confidence": str(s.confidence),
            "needs_check": s.needs_check,
            "confidence_reasons": [str(r) for r in s.confidence_reasons],
            "verification": str(s.verification),
            "world_ids": list(s.world_ids),
            "origin_world_ids": list(s.origin_world_ids),
            "zone_id": s.zone_id,
            "group_key": s.group_key,
            "metrics": dict(s.metrics),
            "reasons": list(s.reasons),
            "acknowledged": s.acknowledged,
            "ack": None if self.ack is None else self.ack.to_payload(),
            "related_event_id": s.related_event_id,
            "evidence": [e.to_payload() for e in self.evidence],
            "evidence_ref": self.evidence_ref,
            "resumed": self.resumed,
        }

    @property
    def resumed(self) -> bool:
        """해소된 이벤트를 다시 활성으로 돌린 전이인지(I1 resumed)."""
        return Change.RESUMED in self.changes


@dataclass(frozen=True)
class LifecycleStep:
    """한 번의 입력(판정·확인·교정·복원)이 낳은 전이와 현장 경보 신호. 순서는 저널 기록 순서다."""

    transitions: tuple[EventTransition, ...] = ()
    local_signals: tuple[LocalSignal, ...] = ()


@dataclass(frozen=True)
class Persistence:
    """발생 지속 조건(6.4): 판정 프레임 최근 window_frames 개 중 min_frames 개 이상에서 발생 판정이 있어야 발생한다.

    (1, 1) 은 지속 조건 없이 즉시 발생이다.
    """

    min_frames: int = 2
    window_frames: int = 3

    def __post_init__(self) -> None:
        as_int(self.min_frames, "Persistence.min_frames", minimum=1)
        as_int(self.window_frames, "Persistence.window_frames", minimum=1)
        if self.min_frames > self.window_frames:
            raise RiskInputError(
                f"Persistence: min_frames {self.min_frames} 가 window_frames {self.window_frames} 보다 크다"
            )

    @property
    def immediate(self) -> bool:
        return self.min_frames == 1


IMMEDIATE = Persistence(1, 1)


def _default_persistence_by_rule() -> dict[str, Persistence]:
    # R7(구역 감시 능력 저하)은 즉시 발생한다. 카메라 장애는 순간 오검출이 아니라 상태 보고와 camera_stall_ms(500ms)로
    # 이미 걸러진 판정이고, G3 '카메라 분리 → 구역 저하 표시 1초 이내' 예산을 지속 조건 지연으로 줄이지 않는다.
    return {R7_RULE_ID: IMMEDIATE}


@dataclass(frozen=True)
class LifecyclePolicy:
    """생명주기 설정(초기 예시값, 7장 머리말: 실제 안전 기준이 아니며 시나리오 데이터셋으로 조정한다).

    - clear_hold_ms: 해소 유지 시간. 판정 없이 유효 관측이 이만큼 이어져야 해소한다(7.2 예시 3초).
    - clear_hold_ms_by_rule: 규칙별 해소 유지 시간.
    - metrics_update_interval_ms: 수치(metrics)만 바뀐 UPDATED 의 최소 간격. 등급·신뢰도·검증 상태 변화는 바로 낸다.
    - persistence: 발생 지속 조건(6.4, 초기값 3프레임 중 2프레임). persistence_by_rule 로 규칙별로 바꾼다
      (기본 R7 즉시). 비우면 모든 규칙이 persistence 를 쓴다.
    - resume_window_ms: 해소 후 이 시간(관측 시각 기준, 이하) 안의 재발생은 기존 이벤트를 재개한다(6.4, 초기값 10초).
      None 이면 재개하지 않는다(항상 새 이벤트).
    """

    clear_hold_ms: int = 3000
    clear_hold_ms_by_rule: Mapping[str, int] = field(default_factory=dict, hash=False)
    metrics_update_interval_ms: int = 1000
    persistence: Persistence = Persistence()
    persistence_by_rule: Mapping[str, Persistence] = field(default_factory=_default_persistence_by_rule, hash=False)
    resume_window_ms: int | None = 10_000

    def __post_init__(self) -> None:
        as_int(self.clear_hold_ms, "LifecyclePolicy.clear_hold_ms", minimum=0)
        as_int(self.metrics_update_interval_ms, "LifecyclePolicy.metrics_update_interval_ms", minimum=0)
        if self.resume_window_ms is not None:
            as_int(self.resume_window_ms, "LifecyclePolicy.resume_window_ms", minimum=0)
        if not isinstance(self.persistence, Persistence):
            raise RiskInputError("LifecyclePolicy.persistence 는 Persistence 여야 한다")
        by_rule = {}
        for rule_id, hold in sorted(self.clear_hold_ms_by_rule.items()):
            by_rule[rule_id] = as_int(hold, f"LifecyclePolicy.clear_hold_ms_by_rule.{rule_id}", minimum=0)
        object.__setattr__(self, "clear_hold_ms_by_rule", MappingProxyType(by_rule))
        persistence = {}
        for rule_id, value in sorted(self.persistence_by_rule.items()):
            if not isinstance(value, Persistence):
                raise RiskInputError(f"LifecyclePolicy.persistence_by_rule.{rule_id} 는 Persistence 여야 한다")
            persistence[rule_id] = value
        object.__setattr__(self, "persistence_by_rule", MappingProxyType(persistence))

    def hold_ms(self, rule_id: str) -> int:
        return self.clear_hold_ms_by_rule.get(rule_id, self.clear_hold_ms)

    def persistence_for(self, rule_id: str) -> Persistence:
        return self.persistence_by_rule.get(rule_id, self.persistence)


# 지속 조건·재개 없이 발생 판정 한 번에 새 이벤트를 만드는 설정(S15P31A101-156 이전 동작). 생명주기 기본 동작만 보는
# 시험과 format_version 1.3 미만 벡터가 쓴다. 제품 기본값이 아니다.
LEGACY_IMMEDIATE_POLICY = LifecyclePolicy(persistence=IMMEDIATE, persistence_by_rule={}, resume_window_ms=None)


@runtime_checkable
class VerificationPolicy(Protocol):
    def verifiable(self, key: EventKey, world_ids: tuple[str, ...], world: WorldState, now_ms: int) -> bool:
        """이 이벤트의 해소 여부를 지금 관측으로 확인할 수 있는지."""
        ...


@dataclass(frozen=True)
class ObservedTracksPolicy:
    """기본 검증 정책.

    공통: 입력이 최신이어야 한다. T_lag = now - t_latest_global 이 max_lag_ms 를 넘으면(영상 전체 중단·지연)
    멈춘 관측으로 해소를 확인하지 않는다.
    사람 대상: 대상 World ID 가 모두 World 상태에 있고 OBSERVED 이며 신원이 IDENTITY_UNCERTAIN 이 아니고,
    대상 자신의 관측이 최신이어야 한다(T_gap = t_latest_global - t_last_valid ≤ max_gap_ms). OBSERVED 도 최신이라는
    뜻이 아니므로(6.1) 상류 상태 문자열만 믿지 않는다. PREDICTED·UNKNOWN 위치나 불확실한 신원으로는 위험이
    사라졌다고 확인하지 않는다.
    구역 대상: 규칙(R7)이 카메라 커버리지를 직접 판정하므로 lag 만 본다. 단, 판정이 사라진 판정(복원 이벤트는 항상)에서는
    추적기가 판정의 감시 확보 근거(Evaluation.zone_clear_evidence)를 더 요구한다(S15P31A101-174·180).

    초기값은 6.1 의 OBSERVED 경계(gap 200ms)와 System Health DEGRADED lag 기준(600ms)이다(실험용 초기값).
    """

    max_gap_ms: int = 200
    max_lag_ms: int = 600

    def __post_init__(self) -> None:
        as_int(self.max_gap_ms, "ObservedTracksPolicy.max_gap_ms", minimum=0)
        as_int(self.max_lag_ms, "ObservedTracksPolicy.max_lag_ms", minimum=0)

    def verifiable(self, key: EventKey, world_ids: tuple[str, ...], world: WorldState, now_ms: int) -> bool:
        if now_ms - world.t_latest_global > self.max_lag_ms:
            return False
        tracks = {t.world_id: t for t in world.tracks}
        for world_id in world_ids:
            track = tracks.get(world_id)
            if track is None or track.observation_state is not ObservationState.OBSERVED:
                return False
            if track.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN:
                return False
            if world.t_latest_global - track.t_last_valid > self.max_gap_ms:
                return False
        return True


@dataclass(frozen=True)
class IdentityCorrection:
    """World Track Manager 의 ID 교정 기록(명세 5.2): previous_world_id 가 world_id 로 바뀌었다."""

    previous_world_id: str
    world_id: str
    identity_revision: int

    def __post_init__(self) -> None:
        if not self.previous_world_id or not self.world_id:
            raise RiskInputError("IdentityCorrection: World ID 가 비었다")
        if self.previous_world_id == self.world_id:
            raise RiskInputError(f"IdentityCorrection: 같은 World ID 로 교정할 수 없다 ({self.world_id})")
        as_int(self.identity_revision, "IdentityCorrection.identity_revision", minimum=0)


@dataclass(frozen=True)
class IdentitySplit:
    """World Track Manager 의 ID 분할 기록(명세 5.2, S15P31A101-201): new_world_id 가 world_id 에서 갈라져 나왔다.

    world_id 는 원래 ID 를 지킨 쪽이고 identity_revision 은 분할 뒤 두 ID 의 값이다(둘 다 같은 값으로 오른다).
    """

    world_id: str
    new_world_id: str
    identity_revision: int

    def __post_init__(self) -> None:
        if not self.world_id or not self.new_world_id:
            raise RiskInputError("IdentitySplit: World ID 가 비었다")
        if self.world_id == self.new_world_id:
            raise RiskInputError(f"IdentitySplit: 같은 World ID 로 분할할 수 없다 ({self.world_id})")
        as_int(self.identity_revision, "IdentitySplit.identity_revision", minimum=0)


RelatedEventFn = Callable[[EventKey, Finding], str | None]
"""새 이벤트를 열 때 이전 이벤트와의 연결(related_event_id)을 돌려주는 함수 (명세 3.2 재시작 복구: 재관측된 사람에게
같은 위험이 다시 걸려 새 이벤트가 생기면 이전 이벤트와의 연결을 남긴다). 연결할 것이 없으면 None. Tier 0 조립부가 복구
쪽(recovery.relink)에서 만들어 apply 에 넘긴다. 연결은 정보일 뿐 이벤트의 신원·해소를 바꾸지 않는다.

추적기는 이 함수를 판정을 반영하기 전에(상태를 바꾸기 전에) 활성 이벤트가 없는 키의 발생 판정마다 부른다. 함수가 예외를
내거나 비지 않은 문자열이 아닌 값을 주면 연결 없음으로 보고 로그만 남긴다(연결 때문에 판정·현장 경보를 잃지 않는다).
발생 지속 조건을 세는 동안 처음 얻은 연결을 키별로 기억했다가 발생(RAISED)할 때 싣는다(그사이 연결 시간이 끝나도)."""


class LifecycleError(RuntimeError):
    """추적기 사용 순서·출처가 계약과 다르다(다른 출처 판정, 시각 역행 등)."""


class _Active:
    """추적기 안의 활성 이벤트(가변). 밖으로는 EventState 로만 낸다."""

    def __init__(self, state: EventState) -> None:
        self.state = state
        self.clear_since: int | None = None  # 해소 확인을 시작한 관측 시각
        # 복원 보호(S15P31A101-174): restore 로 넣은 이벤트. 구역 대상이면 판정이 오는 동안에도 감시 확보 근거가 있는
        # 판정에서만 VERIFIED(복원하지 않은 구역 대상은 판정이 사라진 판정에서만 근거를 요구한다, -180).
        # 해소될 때까지 남는다(재개한 이벤트는 새 _Active 라 보호가 없다).
        self.restored = False
        # 마지막 판정에 이 이벤트의 판정이 있었는지(운영자 종료 거부 조건). 복원 직후는 아직 판정이 없어 False 다.
        self.condition_present = False
        # ID 분할 상대(S15P31A101-201): 분할 전 이 이벤트의 사람일 수도 있는 갈라져 나간 World ID. 해소하려면 이들도 유효하게
        # 관측되고 같은 규칙 판정이 없어야 한다. 해소될 때까지 남는다(재개한 이벤트는 새 _Active 라 없다)
        self.split_partners: set[str] = set()
        self.metrics_emitted_at = state.evaluated_at
        self.mark_emitted()

    def update(self, **changes: Any) -> None:
        self.state = replace(self.state, **changes)

    def mark_emitted(self) -> None:
        """전이로 내보낸 수치·근거를 기억한다. 제한 간격 때문에 미룬 변경을 나중에 내기 위해서다."""
        self.emitted_metrics = dict(self.state.metrics)
        self.emitted_reasons = self.state.reasons


class LifecycleTracker:
    """한 출처의 RiskEvent 생명주기. LIVE 는 Tier 0 인스턴스(gateway_id)마다 하나를 쓴다."""

    def __init__(
        self,
        *,
        origin: Origin,
        gateway_id: str,
        clock: Clock,
        policy: LifecyclePolicy | None = None,
        id_factory: EventIdFactory | None = None,
        verification: VerificationPolicy | None = None,
    ) -> None:
        if not gateway_id:
            raise RiskInputError("LifecycleTracker: gateway_id 가 비었다")
        self._origin = Origin(origin)
        self._gateway_id = gateway_id
        self._clock = clock
        self._policy = LifecyclePolicy() if policy is None else policy
        self._ids = EventIdFactory(clock) if id_factory is None else id_factory
        self._verification = ObservedTracksPolicy() if verification is None else verification
        self._by_key: dict[EventKey, _Active] = {}
        self._by_id: dict[str, _Active] = {}
        self._pending: dict[EventKey, _Pending] = {}  # 발생 지속 조건을 세는 중인 키
        # 활성 이벤트가 없는 키에 대해 처음 얻은 이전 이벤트 연결(재시작 복구). 발생하면 싣고, 대기가 끝나면 버린다.
        self._related_hint: dict[EventKey, str] = {}
        self._resolved: dict[EventKey, _Resolved] = {}  # 재개 시간 안의 해소 이벤트(6.4)
        self._last_evaluated_at = -1
        self._last_observation_time: int | None = None

    @property
    def origin(self) -> Origin:
        return self._origin

    @property
    def gateway_id(self) -> str:
        return self._gateway_id

    def active(self) -> tuple[EventState, ...]:
        """활성 이벤트(event_state_snapshot 재료). 키 순서로 정렬."""
        return tuple(a.state for a in self._sorted_active())

    def get(self, event_id: str) -> EventState | None:
        active = self._by_id.get(event_id)
        return None if active is None else active.state

    def restore_protected(self) -> tuple[str, ...]:
        """복원 보호가 걸린 활성 이벤트의 event_id(키 순서, 진단·시험용). 해소되면 빠진다."""
        return tuple(a.state.event_id for a in self._sorted_active() if a.restored)

    def groups(self) -> Mapping[str, tuple[EventState, ...]]:
        """group_key → 활성 이벤트. 한 사람에게 걸린 여러 규칙을 화면에서 한 묶음으로 보일 때 쓴다(7.4)."""
        out: dict[str, list[EventState]] = {}
        for state in self.active():
            out.setdefault(state.group_key, []).append(state)
        return MappingProxyType({k: tuple(v) for k, v in sorted(out.items())})

    # --- 판정 반영 -----------------------------------------------------------------

    def apply(
        self,
        evaluation: Evaluation,
        world: WorldState,
        *,
        config_version: str,
        evidence_ref: str | None = None,
        related: RelatedEventFn | None = None,
    ) -> LifecycleStep:
        """한 번의 판정을 반영한다. world 는 그 판정의 입력 World 상태다.

        related: 새 이벤트(RAISED)의 related_event_id 를 정하는 함수(재시작 복구, RelatedEventFn). 상태를 바꾸기 전에
        활성 이벤트가 없는 키의 발생 판정마다 미리 부르고, 오류·잘못된 값은 연결 없음으로 낮춘다(판정은 그대로 반영).
        지속 조건을 세는 동안 처음 얻은 연결을 기억해 발생할 때 싣는다. 재개(같은 event_id)와 기존 이벤트 갱신에는
        쓰지 않는다. 없으면(그리고 기억한 연결도 없으면) related_event_id 를 비운다(지금 동작).
        """
        if evaluation.origin is not self._origin:
            raise LifecycleError(f"{self._origin} 추적기에 {evaluation.origin} 판정이 들어왔다")
        if world.t_latest_global != evaluation.observation_time:
            raise LifecycleError(
                f"판정의 관측 시각 {evaluation.observation_time} 과 World 상태 {world.t_latest_global} 가 다르다"
            )
        if evaluation.evaluated_at < self._last_evaluated_at:
            raise LifecycleError(f"판정 시각이 뒤로 갔다 ({self._last_evaluated_at} → {evaluation.evaluated_at})")
        if not config_version:
            raise RiskInputError("apply: config_version 이 비었다")
        self._last_evaluated_at = evaluation.evaluated_at

        grouped: dict[EventKey, list[Finding]] = {}
        for finding in evaluation.findings:
            grouped.setdefault(EventKey.of(finding), []).append(finding)
        tracks = {t.world_id: t for t in world.tracks}
        frame = _Frame(evaluation, config_version, evidence_ref, self._clock.now_ms())
        # 연결 함수는 상태를 바꾸기 전에 모두 부른다(도중에 실패해 일부만 반영되는 일이 없게). 실패는 연결 없음이다.
        if related is not None:
            for key, found in sorted(grouped.items(), key=lambda kv: kv[0].sort_key):
                raising = [f for f in found if not f.holding]
                if not raising or key in self._by_key or key in self._related_hint:
                    continue
                value = _call_related(related, key, _representative(raising))
                if value is not None:
                    self._related_hint[key] = value

        # 관측 시각이 앞으로 간 판정만 지속 조건의 프레임으로 센다(같은 관측을 다시 판정해도 한 프레임이다).
        advancing = self._last_observation_time is None or evaluation.observation_time > self._last_observation_time
        if advancing:
            self._last_observation_time = evaluation.observation_time
        self._prune_resolved(evaluation.observation_time)

        transitions: list[EventTransition] = []
        signals: list[LocalSignal] = []
        # 대기 중인 키는 관측 시각이 멈춘 판정에서도 안전 검사한다(_persist).
        keys = set(grouped) | set(self._by_key) | set(self._pending)
        for key in sorted(keys, key=lambda k: k.sort_key):
            findings = grouped.get(key, [])
            active = self._by_key.get(key)
            if active is None:
                # holding 은 기존 이벤트 유지용이다. 새 이벤트의 등급·구역은 발생 판정에서만 고른다.
                raising = [f for f in findings if not f.holding]
                opening = self._persist(key, raising, advancing, world, evaluation.evaluated_at)
                if opening is not None:
                    self._open(key, opening, world, tracks, frame, transitions, signals)
                    self._related_hint.pop(key, None)
                elif key not in self._pending:
                    self._related_hint.pop(key, None)  # 대기가 이벤트 없이 끝났다(순간 오검출)
            else:
                finding = _maintaining(findings, active.state) if findings else None
                self._advance(active, finding, world, tracks, frame, transitions, signals)
        return LifecycleStep(tuple(transitions), tuple(signals))

    def pending(self) -> Mapping[EventKey, int]:
        """발생 지속 조건을 세는 중인 키 → 창 안의 발생 판정 프레임 수(시험·진단용)."""
        return MappingProxyType({k: p.count for k, p in sorted(self._pending.items(), key=lambda kv: kv[0].sort_key)})

    def _persist(
        self, key: EventKey, raising: list[Finding], advancing: bool, world: WorldState, now_ms: int
    ) -> Finding | None:
        """발생 지속 조건(6.4)을 센다. 이번 판정으로 이벤트를 열어야 하면 그 대표 판정을, 아니면 None 을 돌려준다.

        두 단계로 나눈다.
        1. 프레임 집계: 관측 시각이 앞으로 간 판정만 센다(같은 관측을 다시 판정해도 한 프레임).
        2. 안전 검사: 이번 판정 전부터 대기 중이던 키는 판정 주기마다(관측 시각이 멈춰도) 검사한다. 이번 판정에
           새 관측의 발생 판정이 없거나(사라짐) 관측 시각이 멈췄는데(영상 정지·지연) 대상을 유효하게 관측할 수 없으면
           (UNKNOWN·PREDICTED·트랙 없음·gap 초과·lag 초과, VerificationPolicy) 마지막 발생 판정으로 바로 발생시킨다.
           판정 주기 시계(주입 시계)는 영상이 멈춰도 간다(6.1). 그래서 전체 정지 중에도 늦어도 lag 기준
           (ObservedTracksPolicy.max_lag_ms, 초기 600ms)을 넘는 첫 판정 주기에 발생한다.
        """
        persistence = self._policy.persistence_for(key.rule_id)
        if raising and persistence.immediate:
            self._pending.pop(key, None)
            return _representative(raising)
        pending = self._pending.get(key)
        was_pending = pending is not None
        if advancing:
            if pending is None:
                if not raising:
                    return None
                pending = self._pending[key] = _Pending(persistence.window_frames)
            pending.push(_representative(raising) if raising else None)
            if raising and pending.count >= persistence.min_frames:
                del self._pending[key]
                return _representative(raising)
            if pending.count == 0:
                # 창 안에 발생 판정이 남지 않았다(순간 오검출). 이벤트 없이 끝낸다.
                del self._pending[key]
                return None
        if pending is None or not was_pending or pending.last is None:
            return None
        if (raising and advancing) or self._verification.verifiable(key, key.world_ids, world, now_ms):
            return None
        # 지속 대기 중 관측 상실·정지: 발생 판정이 사라진(또는 새로 나지 않는) 까닭이 위험이 사라져서인지 보이지 않아서인지
        # 모른다. 안전 쪽으로 마지막 발생 판정으로 발생시키고(검증 상태는 UNVERIFIABLE) 확인 필요 이유 PREDICTED 를 더한다
        # (위치가 현재 관측이 아니라 마지막 추정값이다). 재개 시간 안이면 _open 이 재개로 처리한다.
        del self._pending[key]
        last = pending.last
        return replace(
            last,
            confidence_reasons=last.confidence_reasons + (ConfidenceReason.PREDICTED,),
            reasons=last.reasons + (REASON_PENDING_OBSERVATION_LOST,),
        )

    def _open(
        self,
        key: EventKey,
        finding: Finding,
        world: WorldState,
        tracks: Mapping[str, WorldTrack],
        frame: "_Frame",
        transitions: list[EventTransition],
        signals: list[LocalSignal],
    ) -> None:
        """발생 조건을 채운 키의 이벤트를 연다. 재개 시간 안에 해소된 같은 키 이벤트가 있으면 재개한다(6.4)."""
        resolved = self._resolved.pop(key, None)
        window = self._policy.resume_window_ms
        if (
            resolved is not None
            and window is not None
            and frame.evaluation.observation_time - resolved.observation_time <= window
        ):
            self._resume(resolved.state, finding, world, tracks, frame, transitions, signals)
        else:
            self._raise(key, finding, world, tracks, frame, transitions, signals)

    def _resume(
        self,
        previous: EventState,
        finding: Finding,
        world: WorldState,
        tracks: Mapping[str, WorldTrack],
        frame: "_Frame",
        transitions: list[EventTransition],
        signals: list[LocalSignal],
    ) -> None:
        """해소된 이벤트를 같은 event_id 로 다시 활성으로 돌린다. 새 위험 발생처럼 미확인으로 되돌리고 다시 울린다."""
        key = previous.key
        evaluation = frame.evaluation
        verified = self._verification.verifiable(key, key.world_ids, world, evaluation.evaluated_at)
        revisions = dict(previous.identity_revisions)
        revisions |= {w: tracks[w].identity_revision for w in key.world_ids if w in tracks}
        state = replace(
            previous,
            event_sequence=previous.event_sequence + 1,
            lifecycle=Lifecycle.UPDATED,
            rule_version=finding.rule_version,
            severity=finding.severity,
            needs_check=finding.needs_check,
            confidence_reasons=finding.confidence_reasons,
            verification=Verification.VERIFIED if verified else Verification.UNVERIFIABLE,
            zone_id=finding.zone_id,
            metrics=finding.metrics,
            reasons=finding.reasons,
            evaluated_at=evaluation.evaluated_at,
            observation_time=evaluation.observation_time,
            config_version=frame.config_version,
            calibration_version=evaluation.calibration_version,
            scene_version=evaluation.scene_version,
            identity_revisions=MappingProxyType(revisions),
            acks=(),
            local_alerted=False,
            site_alarm_sequence=None,
            resume_count=previous.resume_count + 1,
            escalation_floor=None,  # 운영자 상향은 해소로 끝난다. 재개는 새 위험 발생처럼 규칙 등급으로 다시 시작한다
        )
        active = _Active(state)
        active.condition_present = True
        self._by_key[key] = active
        self._by_id[state.event_id] = active
        self._alert_if_needed(active, frame.occurred_at, signals)
        transitions.append(self._emit(active, Lifecycle.UPDATED, (Change.RESUMED,), frame, tracks))

    def _prune_resolved(self, observation_time: int) -> None:
        """재개 시간이 지난 해소 이벤트를 잊는다. 그 뒤의 재발생은 새 이벤트다."""
        window = self._policy.resume_window_ms
        if window is None:
            self._resolved.clear()
            return
        for key in [k for k, r in self._resolved.items() if observation_time - r.observation_time > window]:
            del self._resolved[key]

    def _raise(
        self,
        key: EventKey,
        finding: Finding,
        world: WorldState,
        tracks: Mapping[str, WorldTrack],
        frame: "_Frame",
        transitions: list[EventTransition],
        signals: list[LocalSignal],
    ) -> None:
        verified = self._verification.verifiable(key, key.world_ids, world, frame.evaluation.evaluated_at)
        related_event_id = self._related_hint.get(key)
        state = EventState(
            event_id=self._ids.new(),
            gateway_id=self._gateway_id,
            origin=self._origin,
            event_sequence=1,
            lifecycle=Lifecycle.RAISED,
            key=key,
            rule_version=finding.rule_version,
            severity=finding.severity,
            needs_check=finding.needs_check,
            confidence_reasons=finding.confidence_reasons,
            verification=Verification.VERIFIED if verified else Verification.UNVERIFIABLE,
            world_ids=key.world_ids,
            origin_world_ids=key.world_ids,
            zone_id=finding.zone_id,
            metrics=finding.metrics,
            reasons=finding.reasons,
            raised_at=frame.evaluation.evaluated_at,
            evaluated_at=frame.evaluation.evaluated_at,
            observation_time=frame.evaluation.observation_time,
            config_version=frame.config_version,
            calibration_version=frame.evaluation.calibration_version,
            scene_version=frame.evaluation.scene_version,
            identity_revisions=MappingProxyType({w: tracks[w].identity_revision for w in key.world_ids if w in tracks}),
            related_event_id=related_event_id,
        )
        active = _Active(state)
        active.condition_present = True
        self._by_key[key] = active
        self._by_id[state.event_id] = active
        # 경보 발행 여부(local_alerted)까지 확정한 상태를 전이에 싣는다. 저널 전이로 복원해도 ACK·해소 신호가 맞다.
        self._alert_if_needed(active, frame.occurred_at, signals)
        transitions.append(self._emit(active, Lifecycle.RAISED, (Change.RAISED,), frame, tracks))

    def _advance(
        self,
        active: _Active,
        finding: Finding | None,
        world: WorldState,
        tracks: Mapping[str, WorldTrack],
        frame: "_Frame",
        transitions: list[EventTransition],
        signals: list[LocalSignal],
    ) -> None:
        state = active.state
        evaluation = frame.evaluation
        changes: set[Change] = set()
        active.condition_present = finding is not None

        # ID 교정(identity_revision 증가)은 해소 확인을 처음부터 다시 세게 한다. 해소하지 않는다(5.2, G3).
        revisions = dict(state.identity_revisions)
        for world_id in state.world_ids:
            track = tracks.get(world_id)
            if track is None:
                continue
            seen = revisions.get(world_id)
            if seen is not None and track.identity_revision > seen:
                changes.add(Change.IDENTITY_REVISED)
            if seen is None or track.identity_revision > seen:
                revisions[world_id] = track.identity_revision
        identity_revised = Change.IDENTITY_REVISED in changes

        partners = tuple(sorted(active.split_partners))
        verified = self._verification.verifiable(state.key, state.world_ids + partners, world, evaluation.evaluated_at)
        # 분할 상대에게 같은 규칙 판정이 있으면 분할 전 이 이벤트의 사람이 그쪽일 수 있다: 해소를 세지 않는다(S15P31A101-201)
        partner_condition = any(
            f.rule_id == state.rule_id and active.split_partners.intersection(f.world_ids) for f in evaluation.findings
        )
        if verified and state.key.target is FindingTarget.ZONE and (finding is None or active.restored):
            # 감시 확보 근거(S15P31A101-174·180): 사람 없는 구역 대상 이벤트는 판정이 없고 입력이 최신이라는 것만으로
            # 조건이 사라졌다고 확인하지 않는다. 그 규칙이 이 판정의 유효 입력으로 그 구역에 조건이 없다고 확인한 근거가
            # 있어야 한다(명세 6.4 유효 관측으로 해소 확인). 구성 전환으로 규칙·구역이 빠져 판정이 사라진 것은 근거가
            # 아니다. 판정이 계속 오는 동안의 검증 상태는 기존 정책 그대로이고(정상 경로의 전이 불변), 복원 이벤트만
            # 판정 중에도 근거를 요구한다(명세 3.2 복원 이벤트 자동 해소 금지, -174 동작 유지).
            verified = (state.rule_id, state.key.zone_id) in evaluation.zone_clear_evidence
        verification = Verification.VERIFIED if verified else Verification.UNVERIFIABLE
        if verification is not state.verification:
            changes.add(Change.VERIFICATION)

        updates: dict[str, Any] = {
            "verification": verification,
            "identity_revisions": MappingProxyType(revisions),
            "evaluated_at": evaluation.evaluated_at,
            "observation_time": evaluation.observation_time,
            "config_version": frame.config_version,
            "calibration_version": evaluation.calibration_version,
            "scene_version": evaluation.scene_version,
        }
        if (frame.config_version, evaluation.calibration_version, evaluation.scene_version) != (
            state.config_version,
            state.calibration_version,
            state.scene_version,
        ):
            changes.add(Change.VERSION)
        previous_severity = state.severity
        resolved = False
        if finding is not None and finding.holding and finding.zone_id != state.zone_id:
            # 다른 구역의 holding: 유지(해소 확인 재시작)와 확인 필요·근거만 반영한다. holding 은 발생 판정이 아니므로
            # 이벤트의 구역과 그 구역에서 나온 등급·수치를 바꾸지 않는다(관측을 잃은 사람이 들어간 적 없는 구역으로
            # 옮겨 가지 않게 한다).
            finding = replace(
                finding,
                severity=state.severity,
                zone_id=state.zone_id,
                metrics=state.metrics,
            )
        floor = state.escalation_floor
        if finding is not None and floor is not None and _rank(finding.severity) < _rank(floor):
            # 운영자 상향 바닥값(S15P31A101-272): 해소될 때까지 규칙 판정이 운영자가 올린 등급을 낮추지 않는다(5.4 하향 불가).
            # 규칙이 더 높게 판정하면 그 등급을 따른다.
            finding = replace(finding, severity=floor)
        if finding is not None:
            active.clear_since = None
            if finding.severity is not state.severity:
                changes.add(Change.SEVERITY)
            if finding.confidence_reasons != state.confidence_reasons:
                changes.add(Change.CONFIDENCE)
            if finding.zone_id != state.zone_id:
                changes.add(Change.ZONE)
            if finding.rule_version != state.rule_version:
                changes.add(Change.VERSION)
            # 마지막으로 내보낸 값과 비교한다. 간격 안에서 바뀌고 고정된 값도 간격이 지나면 낸다.
            metrics_changed = (
                dict(finding.metrics) != active.emitted_metrics or finding.reasons != active.emitted_reasons
            )
            interval = self._policy.metrics_update_interval_ms
            if metrics_changed and evaluation.evaluated_at - active.metrics_emitted_at >= interval:
                changes.add(Change.METRICS)
            updates |= {
                "rule_version": finding.rule_version,
                "severity": finding.severity,
                "needs_check": finding.needs_check,
                "confidence_reasons": finding.confidence_reasons,
                "zone_id": finding.zone_id,
                "metrics": finding.metrics,
                "reasons": finding.reasons,
            }
        elif verified and not identity_revised and not partner_condition:
            if active.clear_since is None:
                active.clear_since = evaluation.observation_time
            resolved = evaluation.observation_time - active.clear_since >= self._policy.hold_ms(state.rule_id)
        else:
            # 관측으로 확인할 수 없거나 신원이 바뀌었다. 해소 확인을 처음부터 다시 센다(자동 해소 없음).
            active.clear_since = None

        if resolved:
            changes.add(Change.CLEARED)
        if not changes:
            # 전이 없이 최신 판정 값만 반영한다(저널에 남지 않는 값은 다음 전이·스냅샷에 실린다).
            active.update(**updates)
            return
        if changes & {Change.METRICS, Change.SEVERITY, Change.CONFIDENCE, Change.ZONE}:
            active.metrics_emitted_at = evaluation.evaluated_at
        lifecycle = Lifecycle.RESOLVED if resolved else Lifecycle.UPDATED
        active.update(**updates, event_sequence=state.event_sequence + 1, lifecycle=lifecycle)
        if not resolved and Change.SEVERITY in changes and _rank(active.state.severity) > _rank(previous_severity):
            # 등급이 올라가면 그 등급으로 다시 울린다(이미 확인했어도 새 위험 등급이다).
            # 전이보다 먼저 정해 local_alerted 를 전이 상태에 싣는다(_raise 와 같다).
            self._alert_if_needed(active, frame.occurred_at, signals, force=True)
        transitions.append(self._emit(active, lifecycle, _ordered(changes), frame, tracks))
        if resolved:
            self._drop(active)
            if self._policy.resume_window_ms is not None:
                self._resolved[active.state.key] = _Resolved(active.state, evaluation.observation_time)
            if active.state.local_alerted:
                signals.append(AlertClear(**self._signal_fields(active.state, frame.occurred_at)))

    # --- 확인(ACK) -------------------------------------------------------------------

    def acknowledge(
        self,
        event_id: str,
        actor: AckActor,
        actor_id: str,
        acked_at: int,
        target_sequence: int | None = None,
        channel: str | None = None,
    ) -> LifecycleStep | None:
        """확인을 기록한다. 활성 이벤트가 아니면(이미 해소됐거나 모르는 ID) None 이고 아무것도 기록하지 않는다.

        확인은 해소가 아니다. UPDATED 전이(ACKNOWLEDGED)로 누가 확인했는지 저널에 남기고, 현장 경보를 낸
        LIVE 이벤트면 반복 소리 정지(AlarmStop)를 낸다. 같은 이벤트를 여러 번 확인해도 모두 기록한다.
        target_sequence 는 확인 대상 상태(확인한 사람이 본 event_sequence, site_ack.event_sequence)다. 없으면 지금
        마지막 전이를 대상으로 본다. 지금보다 큰 값은 지금 값으로 줄인다(본 적 없는 상태를 확인할 수 없다).
        대상이 지금 경보 단계보다 앞이면(상향 전 확인) 기록하고 AlarmStop 도 내지만(대상 sequence 로 노트북이 거른다)
        site_acknowledged 는 바뀌지 않는다.
        """
        active = self._by_id.get(event_id)
        if active is None:
            return None
        actor = AckActor(actor)
        if not actor_id:
            raise RiskInputError("acknowledge: actor_id 가 비었다")
        occurred_at = self._clock.now_ms()
        state = active.state
        target = state.event_sequence
        if target_sequence is not None:
            target = min(as_int(target_sequence, "acknowledge.target_sequence", minimum=1), state.event_sequence)
        ack = AckRecord(
            actor=actor,
            actor_id=actor_id,
            acked_at=as_int(acked_at, "acknowledge.acked_at", minimum=0),
            recorded_at=occurred_at,
            target_sequence=target,
            channel=channel,
        )
        active.update(acks=state.acks + (ack,), event_sequence=state.event_sequence + 1, lifecycle=Lifecycle.UPDATED)
        active.mark_emitted()
        transition = EventTransition(
            state=active.state,
            transition=Lifecycle.UPDATED,
            changes=(Change.ACKNOWLEDGED,),
            occurred_at=occurred_at,
            ack=ack,
        )
        signals: tuple[LocalSignal, ...] = ()
        if active.state.local_alerted:
            signals = (
                AlarmStop(
                    **self._signal_fields(active.state, occurred_at),
                    acked_by=actor,
                    actor_id=actor_id,
                    target_sequence=target,
                    acked_at=ack.acked_at,
                    channel=channel,
                ),
            )
        return LifecycleStep((transition,), signals)

    # --- 운영자 종료 -------------------------------------------------------------------

    def close(
        self, request: CloseRequest, *, commit: Callable[[LifecycleStep], Any] | None = None
    ) -> LifecycleStep | CloseReject:
        """관리자의 구역 대상 이벤트 종료(event_close, S15P31A101-200). 받으면 RESOLVED 전이(변경 사유 OPERATOR_CLOSED)
        하나와, 현장 경보를 낸 이벤트면 AlertClear 를 돌려준다. 받지 않으면 거부 사유만 돌려주고 아무것도 바꾸지 않는다.

        감시 확보 근거가 오지 않는 구역 대상 이벤트(카메라를 치움, 구역을 구성에서 뺌, 재시작 복원)를 사람이 닫는 길이다.
        관측으로 해소를 확인한 것이 아니므로 검증 상태는 그대로 두고(UNVERIFIABLE 이면 UNVERIFIABLE) 판정 근거
        (evidence_ref)를 싣지 않는다. 누가·왜 닫았는지는 close 기록이 근거다. 거부: 활성이 아님, 사람 대상, HIGH·CRITICAL,
        마지막 판정에 그 이벤트의 판정이 있음(조건이 지금 성립). 종료한 이벤트는 재개 대상에 넣지 않는다: 같은 (규칙, 구역)이
        다시 발생 조건을 채우면 재개 시간 안이어도 새 이벤트다(재개는 관측 해소 뒤의 재발생, 6.4).

        commit: 종료 단계를 추적기 상태를 바꾸기 전에 넘겨받는 함수(Tier 0 는 저널 제출). 예외를 내면 아무것도 바꾸지 않고
        그 예외를 그대로 올린다(제출하지 못한 종료를 활성 제거·수락으로 확정하지 않는다, S15P31A101-200 리뷰 F1).
        """
        if self._origin is not Origin.LIVE:
            raise LifecycleError(f"운영자 종료는 LIVE 이벤트만 받는다({self._origin})")
        active = self._by_id.get(request.event_id)
        if active is None:
            return CloseReject.NOT_ACTIVE
        state = active.state
        if state.key.target is not FindingTarget.ZONE:
            return CloseReject.TARGET_NOT_ZONE
        if _rank(state.severity) >= _rank(Severity.HIGH):
            return CloseReject.SEVERITY_NOT_ALLOWED
        if active.condition_present:
            return CloseReject.CONDITION_PRESENT
        occurred_at = self._clock.now_ms()
        record = CloseRecord(
            close_id=as_str(request.close_id, "close.close_id"),
            actor_id=as_str(request.actor_id, "close.actor_id"),
            requested_at=as_timestamp_ms(request.requested_at, "close.requested_at"),
            recorded_at=occurred_at,
            reason=as_str(request.reason, "close.reason"),
        )
        closed = replace(state, event_sequence=state.event_sequence + 1, lifecycle=Lifecycle.RESOLVED)
        transition = EventTransition(
            state=closed,
            transition=Lifecycle.RESOLVED,
            changes=(Change.OPERATOR_CLOSED,),
            occurred_at=occurred_at,
            close=record,
        )
        signals: tuple[LocalSignal, ...] = ()
        if closed.local_alerted:
            signals = (AlertClear(**self._signal_fields(closed, occurred_at)),)
        step = LifecycleStep((transition,), signals)
        if commit is not None:
            commit(step)  # 실패하면 상태를 바꾸기 전이다
        active.state = closed
        active.mark_emitted()
        self._drop(active)
        return step

    # --- 운영자 상향 -------------------------------------------------------------------

    def escalate(
        self, request: EscalationRequest, *, commit: Callable[[LifecycleStep], Any] | None = None
    ) -> LifecycleStep | EscalateReject:
        """관리자가 승인한 VLM 상향 의견(event_escalate, S15P31A101-272, 명세 5.4·12.3). 받으면 같은 event_id 의 UPDATED 전이
        (변경 사유 SEVERITY·ESCALATED, escalation 은 받은 값 그대로, resumed false) 하나와, 3.10 상향 재경보(등급이 오르면 그 등급으로
        다시 울림, 규칙 상향과 같은 경로)의 LocalAlert 를 돌려준다. 받지 않으면 거부 사유만 돌려주고 아무것도 바꾸지 않는다.

        판정이 아니므로 판정 근거(evidence)·검증 상태·수치는 그대로이고 evidence_ref 는 비운다(저널이 그 이벤트의 직전 판정 근거를
        싣고, 모르면 null). 받은 등급은 해소될 때까지 바닥값(escalation_floor)이다: 이후 규칙 판정이 낮추지 않는다(5.4 하향 불가).
        거부: 활성이 아님(NOT_ACTIVE), target_severity 가 지금 등급보다 높지 않음(NOT_UPWARD).

        commit: 상태를 바꾸기 전에 단계를 넘겨받는 함수(Tier 0 는 저널 제출). 예외를 내면 아무것도 바꾸지 않고 그 예외를 올린다(close 와 같다).
        """
        if self._origin is not Origin.LIVE:
            raise LifecycleError(f"운영자 상향은 LIVE 이벤트만 받는다({self._origin})")
        active = self._by_id.get(request.event_id)
        if active is None:
            return EscalateReject.NOT_ACTIVE
        state = active.state
        target = request.target_severity
        if _rank(target) <= _rank(state.severity):
            return EscalateReject.NOT_UPWARD
        occurred_at = self._clock.now_ms()
        floor = state.escalation_floor
        escalated = replace(
            state,
            severity=target,
            escalation_floor=target if floor is None or _rank(target) > _rank(floor) else floor,
            event_sequence=state.event_sequence + 1,
            lifecycle=Lifecycle.UPDATED,
        )
        escalated, alert = self._local_alert(escalated, occurred_at, force=True)  # 등급 상향 재경보(3.10, _advance 와 같다)
        transition = EventTransition(
            state=escalated,
            transition=Lifecycle.UPDATED,
            changes=(Change.SEVERITY, Change.ESCALATED),
            occurred_at=occurred_at,
            escalation=request.escalation,
        )
        step = LifecycleStep((transition,), () if alert is None else (alert,))
        if commit is not None:
            commit(step)  # 실패하면 상태를 바꾸기 전이다
        active.state = escalated
        active.mark_emitted()
        return step

    # --- ID 교정 ---------------------------------------------------------------------

    def correct_identity(self, correction: IdentityCorrection) -> LifecycleStep:
        """명시적 ID 교정을 반영한다(명세 5.2). 진행 중인 위험은 해소하지 않는다(G3).

        previous_world_id 를 대상으로 하는 활성 이벤트의 World ID 를 world_id 로 바꾼다. 원본 World ID 는
        origin_world_ids 에 남는다. 바뀐 키에 이미 활성 이벤트가 있으면 키를 바꾸지 않고 related_event_id 로
        연결만 남긴다(그 이벤트는 옛 ID 가 더는 관측되지 않으므로 UNVERIFIABLE 로 남는다).
        """
        occurred_at = self._clock.now_ms()
        transitions: list[EventTransition] = []
        for active in self._by_id.values():  # 분할 상대가 합쳐지면 남는 ID 로 잇는다(S15P31A101-201)
            if correction.previous_world_id in active.split_partners:
                active.split_partners.discard(correction.previous_world_id)
                active.split_partners.add(correction.world_id)
        targets = [a for a in self._sorted_active() if correction.previous_world_id in a.state.world_ids]
        for active in targets:
            state = active.state
            world_ids = tuple(
                sorted({correction.world_id if w == correction.previous_world_id else w for w in state.world_ids})
            )
            new_key = EventKey(state.rule_id, world_ids, state.key.zone_id)
            other = self._by_key.get(new_key)
            active.clear_since = None
            if other is not None and other is not active:
                active.update(
                    related_event_id=other.state.event_id,
                    event_sequence=state.event_sequence + 1,
                    lifecycle=Lifecycle.UPDATED,
                )
                change = Change.IDENTITY_CONFLICT
            else:
                revisions = {w: r for w, r in state.identity_revisions.items() if w != correction.previous_world_id}
                revisions[correction.world_id] = correction.identity_revision
                del self._by_key[state.key]
                active.update(
                    key=new_key,
                    world_ids=world_ids,
                    identity_revisions=MappingProxyType(revisions),
                    event_sequence=state.event_sequence + 1,
                    lifecycle=Lifecycle.UPDATED,
                )
                self._by_key[new_key] = active
                change = Change.IDENTITY_CORRECTED
            active.mark_emitted()
            transitions.append(
                EventTransition(
                    state=active.state, transition=Lifecycle.UPDATED, changes=(change,), occurred_at=occurred_at
                )
            )
        for active in self._by_id.values():  # 상대가 이벤트 자신의 ID 로 합쳐졌으면 상대가 아니다
            active.split_partners.difference_update(active.state.world_ids)
        return LifecycleStep(tuple(transitions))

    def split_identity(self, split: IdentitySplit) -> LifecycleStep:
        """ID 분할을 반영한다(명세 5.2, S15P31A101-201). 이벤트를 옮기거나 해소하지 않는다(G3).

        world_id 를 대상으로 하거나 world_id 를 분할 상대로 둔(분할 계보 안) 사람 대상 활성 이벤트는 그대로 두고(원본 World ID
        보존) new_world_id 를 분할 상대로 더해 해소 확인을 처음부터 다시 센다(W→N→M 이면 W 의 이벤트 상대는 N·M, 리뷰 R1). 전이는 내지 않는다: 같은 tick 판정에서 world_id 의 identity_revision 이 올라
        IDENTITY_REVISED 갱신이 나간다. new_world_id 에 같은 위험이 판정되면 그쪽은 새 이벤트다.
        """
        for active in self._sorted_active():
            world_ids = active.state.world_ids
            lineage = split.world_id in world_ids or split.world_id in active.split_partners
            if active.state.key.target is FindingTarget.PERSON and lineage and split.new_world_id not in world_ids:
                active.split_partners.add(split.new_world_id)
                active.clear_since = None
        return LifecycleStep(())

    # --- 체크포인트 복원 -------------------------------------------------------------

    def restore(self, states: Iterable[EventState]) -> LifecycleStep:
        """체크포인트·저널에서 되살린 활성 이벤트를 넣는다(명세 3.2 재시작 복구).

        복원한 이벤트는 UNVERIFIABLE 로 두고 자동 해소하지 않는다. event_sequence 는 이어서 올린다.
        모든 복원 이벤트에 복원 보호를 건다(해소될 때까지). 사람 대상은 기존 정책대로 대상의 재관측으로, 구역 대상은 판정의
        감시 확보 근거(Evaluation.zone_clear_evidence)가 있을 때만 VERIFIED 가 된다(S15P31A101-174).
        지속 조건 대기 중인 키와 재개 시간 안의 해소 이벤트는 체크포인트에 없어 복원하지 않는다(재시작 직후의
        재발생은 지속 조건을 다시 세고 새 이벤트가 된다).
        이미 활성 이벤트가 있는 추적기에는 복원하지 않는다(재시작 직후 한 번만 쓴다).
        """
        if self._by_id:
            raise LifecycleError("restore 는 활성 이벤트가 없는 추적기에서만 한다")
        occurred_at = self._clock.now_ms()
        restored: list[_Active] = []
        for state in states:
            if state.origin is not self._origin:
                raise LifecycleError(f"{self._origin} 추적기에 {state.origin} 이벤트를 복원하려 했다")
            if state.lifecycle is Lifecycle.RESOLVED:
                raise LifecycleError(f"해소된 이벤트는 복원하지 않는다: {state.event_id}")
            if state.key in self._by_key or state.event_id in self._by_id:
                raise LifecycleError(f"복원할 이벤트가 겹친다: {state.event_id}")
            active = _Active(state)
            active.restored = True
            self._by_key[state.key] = active
            self._by_id[state.event_id] = active
            restored.append(active)
        transitions = []
        for active in sorted(restored, key=lambda a: a.state.key.sort_key):
            state = active.state
            changes = (
                (Change.RESTORED, Change.VERIFICATION)
                if state.verification is Verification.VERIFIED
                else (Change.RESTORED,)
            )
            active.update(
                verification=Verification.UNVERIFIABLE,
                event_sequence=state.event_sequence + 1,
                lifecycle=Lifecycle.UPDATED,
            )
            transitions.append(
                EventTransition(
                    state=active.state, transition=Lifecycle.UPDATED, changes=changes, occurred_at=occurred_at
                )
            )
        return LifecycleStep(tuple(transitions))

    # --- 내부 -------------------------------------------------------------------------

    def _sorted_active(self) -> list[_Active]:
        return sorted(self._by_key.values(), key=lambda a: a.state.key.sort_key)

    def _drop(self, active: _Active) -> None:
        del self._by_key[active.state.key]
        del self._by_id[active.state.event_id]

    def _emit(
        self,
        active: _Active,
        lifecycle: Lifecycle,
        changes: tuple[Change, ...],
        frame: "_Frame",
        tracks: Mapping[str, WorldTrack],
    ) -> EventTransition:
        active.mark_emitted()
        return EventTransition(
            state=active.state,
            transition=lifecycle,
            changes=changes,
            occurred_at=frame.occurred_at,
            evidence=tuple(TrackEvidence.of(w, tracks.get(w)) for w in active.state.world_ids),
            evidence_ref=frame.evidence_ref,
        )

    def _alert_if_needed(
        self, active: _Active, occurred_at: int, signals: list[LocalSignal], force: bool = False
    ) -> None:
        state, alert = self._local_alert(active.state, occurred_at, force=force)
        if alert is not None:
            active.state = state
            signals.append(alert)

    def _local_alert(self, state: EventState, occurred_at: int, *, force: bool) -> tuple[EventState, LocalAlert | None]:
        """현장 경보를 (다시) 내야 하면 경보 단계를 연 상태와 LocalAlert, 아니면 (state, None). 상태를 바꾸지 않는다."""
        plan = channels_for(state.severity, state.confidence, state.origin)
        if Channel.LOCAL_ALERT not in plan.channels or (state.local_alerted and not force):
            return state, None
        state = replace(state, local_alerted=True, site_alarm_sequence=state.event_sequence)
        return state, LocalAlert(
            **self._signal_fields(state, occurred_at),
            rule_id=state.rule_id,
            zone_id=state.zone_id,
            world_ids=state.world_ids,
            observation_time=state.observation_time,
        )

    @staticmethod
    def _signal_fields(state: EventState, occurred_at: int) -> dict[str, Any]:
        return {
            "origin": state.origin,
            "event_id": state.event_id,
            "gateway_id": state.gateway_id,
            "event_sequence": state.event_sequence,
            "issued_at": occurred_at,
            "severity": state.severity,
            "needs_check": state.needs_check,
        }


class _Pending:
    """발생 지속 조건을 세는 중인 키. 최근 프레임들의 발생 판정 여부와 마지막 발생 판정을 기억한다."""

    def __init__(self, window_frames: int) -> None:
        self.frames: deque[bool] = deque(maxlen=window_frames)
        self.last: Finding | None = None

    def push(self, finding: Finding | None) -> None:
        self.frames.append(finding is not None)
        if finding is not None:
            self.last = finding

    @property
    def count(self) -> int:
        return sum(self.frames)


@dataclass(frozen=True)
class _Resolved:
    """재개 시간 안의 해소 이벤트. observation_time 은 해소를 확정한 판정의 관측 시각이다."""

    state: EventState
    observation_time: int


@dataclass(frozen=True)
class _Frame:
    evaluation: Evaluation
    config_version: str
    evidence_ref: str | None
    occurred_at: int


def _call_related(related: RelatedEventFn, key: EventKey, finding: Finding) -> str | None:
    """연결 함수를 부른다. 예외·잘못된 값은 연결 없음으로 낮추고 로그를 남긴다(판정·현장 경보를 잃지 않는다)."""
    try:
        value = related(key, finding)
    except Exception:  # noqa: BLE001 - 어떤 오류든 연결 없음으로 낮춰 판정·경보를 지킨다
        _log.exception("related_event_id 연결 함수가 실패했다(연결 없이 판정을 반영한다): %s", key)
        return None
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        _log.error("related_event_id 연결 함수가 잘못된 값을 돌려줬다(연결 없이 판정을 반영한다): %s → %r", key, value)
        return None
    return value


def _rank(severity: Severity) -> int:
    return _SEVERITY_RANK[Severity(severity)]


def _representative(findings: list[Finding]) -> Finding:
    """같은 키의 판정이 여럿이면(예: 한 사람이 같은 규칙의 두 구역에 걸침) 가장 높은 등급을 대표로 쓴다.

    등급이 같으면 발생 판정(holding=False)을, 그다음 정렬 순서가 앞선 것을 고른다.
    """
    return min(findings, key=lambda f: (-_rank(f.severity), f.holding, f.sort_key))


def _maintaining(findings: list[Finding], state: EventState) -> Finding:
    """활성 이벤트의 대표 판정. holding 은 유지만 하므로 등급을 바꾸지 않는다(바꾸는 것은 발생 판정뿐이다).

    holding 의 등급을 판정 값과 관계없이 현재 등급으로 맞춘 뒤 _representative 와 같은 순서로 고른다. 그래서
    현재 등급보다 높은 발생 판정만 등급을 올리고, 현재 등급보다 낮은 발생 판정은 holding 이 있으면 등급을
    내리지 못한다. 등급과 발생 여부가 같으면 이벤트의 현재 구역과 같은 holding 을 먼저 고른다(holding 은 구역을
    바꾸지 않는다). 발생 판정끼리의 순서는 _representative 와 같다.
    """
    current = state.severity
    held = [replace(f, severity=current) if f.holding else f for f in findings]
    return min(
        held,
        key=lambda f: (-_rank(f.severity), f.holding, f.holding and f.zone_id != state.zone_id, f.sort_key),
    )


def _ordered(changes: set[Change]) -> tuple[Change, ...]:
    return tuple(sorted(changes, key=_CHANGE_ORDER.__getitem__))
