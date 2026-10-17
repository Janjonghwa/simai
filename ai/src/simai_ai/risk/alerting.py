"""심각도 × 신뢰도 → 알림 채널, 현장 경보 신호 (명세 7.3, 3.10, 7.4 출처 분리, 수정 6 ACK).

7.3 표가 알림 채널의 정본이다. 보내는 채널은 channels_for 로만 정하고, 다른 곳에서 등급을 보고 채널을
따로 고르지 않는다. 신뢰도가 낮아도 등급은 내려가지 않고 같은 채널에 “확인 필요”만 붙는다.

현장 경보(노트북)는 RunPod Tier 0 → 노트북(I0 아래 방향)으로 바로 간다. 신호는 세 가지다.
- LocalAlert: 경보 시작(소리 반복 + 점멸 표시). CRITICAL·HIGH 이벤트가 생기거나 그 등급으로 올라갈 때.
- AlarmStop: 확인(ACK). 누가 확인하든(관리자·반장·현장 버튼) 반복 소리만 멈추고 표시는 유지한다.
- AlertClear: 해소(RESOLVED). 표시를 끝낸다. 확인은 해소가 아니므로 ACK 로는 나가지 않는다.

LIVE 만 실제 알림 대상이다(AGENTS.md 규칙 5). 세 신호는 출처가 LIVE 가 아니면 만들 수 없다
(NonLiveAlertError, engine.AlertCandidate 와 같은 이중 장치). channels_for 도 LIVE 가 아니면 빈 집합이다.

메시지 스키마(local_alert, alarm_stop 등)는 S15P31A101-20 이 contracts/ 에 만든다. 그 전까지 이 dataclass 가
내부 형식이고, 스키마가 머지되면 to_payload 결과를 스키마로 검증하는 시험을 붙인다.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from simai_ai.risk._parse import RiskInputError, as_int, as_mapping, as_str
from simai_ai.risk.common import Origin, Severity
from simai_ai.risk.engine import NonLiveAlertError

_log = logging.getLogger(__name__)


class Confidence(StrEnum):
    """증거 신뢰도(명세 7.3). severity 와 독립이다. LOW 면 “확인 필요”(needs_check)를 붙인다."""

    HIGH = "HIGH"
    LOW = "LOW"

    @classmethod
    def from_needs_check(cls, needs_check: bool) -> "Confidence":
        return cls.LOW if needs_check else cls.HIGH


class Channel(StrEnum):
    """알림 채널(명세 7.3, 3.10, 10장). WATCH_* 는 확장 단계다."""

    LOCAL_ALERT = "LOCAL_ALERT"  # 노트북 현장 경보(소리·점멸), Tier 0 → 노트북 직접
    CONTROL_SCREEN_ALERT = "CONTROL_SCREEN_ALERT"  # 관제 화면 경보(소리·점멸), EC2 경유
    CONTROL_SCREEN_NOTICE = "CONTROL_SCREEN_NOTICE"  # 관제 화면 안내(CAUTION)
    FOREMAN_PWA = "FOREMAN_PWA"  # 반장 PWA 푸시
    WATCH = "WATCH"  # 워치(확장)
    WATCH_WEAK_VIBRATION = "WATCH_WEAK_VIBRATION"  # 워치 약한 진동(확장, CAUTION·신뢰도 높음)


# 현장 경보를 울리는 등급. 7.3 표에서 LOCAL_ALERT 가 들어가는 행과 같아야 한다(시험이 확인한다).
LOCAL_ALERT_SEVERITIES = frozenset({Severity.CRITICAL, Severity.HIGH})

_ALERT = frozenset({Channel.LOCAL_ALERT, Channel.CONTROL_SCREEN_ALERT, Channel.FOREMAN_PWA, Channel.WATCH})

# 7.3 표 그대로. (심각도, 신뢰도) → 채널. INFO 는 기록만 한다(채널 없음).
_TABLE: Mapping[tuple[Severity, Confidence], frozenset[Channel]] = {
    (Severity.CRITICAL, Confidence.HIGH): _ALERT,
    (Severity.CRITICAL, Confidence.LOW): _ALERT,
    (Severity.HIGH, Confidence.HIGH): _ALERT,
    (Severity.HIGH, Confidence.LOW): _ALERT,
    (Severity.CAUTION, Confidence.HIGH): frozenset({Channel.CONTROL_SCREEN_NOTICE, Channel.WATCH_WEAK_VIBRATION}),
    (Severity.CAUTION, Confidence.LOW): frozenset({Channel.CONTROL_SCREEN_NOTICE}),
    (Severity.INFO, Confidence.HIGH): frozenset(),
    (Severity.INFO, Confidence.LOW): frozenset(),
}


@dataclass(frozen=True)
class ChannelPlan:
    """보낼 채널과 “확인 필요” 표시. needs_check 는 신뢰도가 낮을 때 참이고 등급·채널은 그대로다."""

    channels: frozenset[Channel]
    needs_check: bool


def channels_for(severity: Severity, confidence: Confidence, origin: Origin) -> ChannelPlan:
    """7.3 표로 채널을 정한다. LIVE 가 아니면 채널이 없다(보고서·시뮬레이션 화면에만 쓴다)."""
    severity, confidence, origin = Severity(severity), Confidence(confidence), Origin(origin)
    channels = _TABLE[(severity, confidence)] if origin.alertable else frozenset()
    return ChannelPlan(channels=channels, needs_check=confidence is Confidence.LOW)


class LocalSignalKind(StrEnum):
    LOCAL_ALERT = "LOCAL_ALERT"
    ALARM_STOP = "ALARM_STOP"
    ALERT_CLEAR = "ALERT_CLEAR"


class AckActor(StrEnum):
    """확인 주체(수정 6). 누가 확인해도 반복 소리를 멈추고, 누가 했는지 저널에 남긴다."""

    ADMIN = "ADMIN"  # 관제 화면(관리자). EC2 → Tier 0
    FOREMAN = "FOREMAN"  # 반장 PWA. EC2 → Tier 0
    SITE_BUTTON = "SITE_BUTTON"  # 노트북 현장 확인 버튼(site_ack). I0 제어 채널로 Tier 0 에 직접


@dataclass(frozen=True, kw_only=True)
class _LocalSignal:
    """현장 경보 신호 공통 필드. 출처가 LIVE 가 아니면 만들 수 없다."""

    origin: Origin
    event_id: str
    gateway_id: str
    event_sequence: int  # 이 신호를 낳은 전이의 event_sequence. 노트북은 낮은 값이 늦게 오면 무시한다
    issued_at: int  # 주입 시계의 발행 시각(UTC ms)
    severity: Severity
    needs_check: bool

    def __post_init__(self) -> None:
        if not Origin(self.origin).alertable:
            raise NonLiveAlertError(f"{self.origin} 판정은 현장 경보로 보내지 않는다(LIVE 만 알린다)")

    @property
    def kind(self) -> LocalSignalKind:
        raise NotImplementedError

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "event_id": self.event_id,
            "gateway_id": self.gateway_id,
            "event_sequence": self.event_sequence,
            "issued_at": self.issued_at,
            "severity": str(self.severity),
            "needs_check": self.needs_check,
        }


@dataclass(frozen=True, kw_only=True)
class LocalAlert(_LocalSignal):
    """경보 시작. 노트북은 확인(AlarmStop) 전까지 소리를 반복하고 해소(AlertClear) 전까지 표시한다.

    summary 는 규칙 근거 한 줄의 재료(규칙 ID·구역·사람)다. VLM 근거는 넣지 않는다(10.1).
    """

    rule_id: str
    zone_id: str | None
    world_ids: tuple[str, ...]
    observation_time: int

    @property
    def kind(self) -> LocalSignalKind:
        return LocalSignalKind.LOCAL_ALERT

    def to_payload(self) -> dict[str, Any]:
        return super().to_payload() | {
            "rule_id": self.rule_id,
            "zone_id": self.zone_id,
            "world_ids": list(self.world_ids),
            "observation_time": self.observation_time,
        }


@dataclass(frozen=True, kw_only=True)
class AlarmStop(_LocalSignal):
    """확인(ACK). 반복 소리만 멈춘다. 표시는 해소까지 유지한다.

    **이름 주의.** 내부 event_sequence 는 ACKNOWLEDGED 전이 자신의 sequence(저널 번호)이고, 계약
    alarm_stop.event_sequence 는 target_sequence(확인 대상 상태, 확인한 사람이 본 sequence)다. I0 로 보낼 때는
    to_payload() 가 아니라 alarm_stop_contract_payload() 를 쓴다. event_sequence 를 계약 필드로 옮기면 늦은 정지가 항상
    통과해 상향 전 확인이 상향된 경보를 멈춘다. 노트북은 target_sequence 뒤에 다시 울리기 시작한 경보(등급 상향·재개)는
    멈추지 않는다(명세 3.10, S15P31A101-161).
    """

    acked_by: AckActor
    actor_id: str
    target_sequence: int | None = None  # None: 대상 상태를 모름(노트북은 지금 경보를 멈춘다)
    acked_at: int | None = None  # 확인한 시각(보낸 쪽 기준). 계약 operator_ack.acked_at
    channel: str | None = None  # 확인이 들어온 계약 채널(받은 값 그대로). None 이면 확인 주체의 기본 채널

    @property
    def kind(self) -> LocalSignalKind:
        return LocalSignalKind.ALARM_STOP

    def to_payload(self) -> dict[str, Any]:
        return super().to_payload() | {
            "acked_by": str(self.acked_by),
            "actor_id": self.actor_id,
            "target_sequence": self.target_sequence,
            "acked_at": self.acked_at,
            "channel": self.channel,
        }


# 확인 주체 → 계약 operator_ack.actor_role 과, 받은 채널을 모를 때 쓰는 기본 채널
_ACK_ROLE = {AckActor.ADMIN: "ADMIN", AckActor.FOREMAN: "FOREMAN", AckActor.SITE_BUTTON: "SITE"}
_DEFAULT_CHANNEL = {AckActor.ADMIN: "CONTROL_ROOM", AckActor.FOREMAN: "FOREMAN_PWA", AckActor.SITE_BUTTON: "SITE_BUTTON"}


@dataclass(frozen=True)
class ContractAck:
    """I1 event_ack·I0 site_ack 의 확인 하나 → LifecycleTracker.acknowledge 인자(acknowledge_args)와 ack_id.

    target_sequence 는 계약 event_sequence(확인한 사람이 본 상태)다. 없으면 None(Tier 0 가 기록할 때의 상태를 대상).
    """

    event_id: str
    actor: AckActor
    actor_id: str
    acked_at: int
    target_sequence: int | None
    ack_id: str
    channel: str  # 받은 채널 그대로(저널·alarm_stop 에 보존)

    def acknowledge_args(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "actor": self.actor,
            "actor_id": self.actor_id,
            "acked_at": self.acked_at,
            "target_sequence": self.target_sequence,
            "channel": self.channel,
        }


# 계약이 메시지마다 허용하는 채널과 역할(event_ack.schema.json, site_ack.schema.json, defs.operator_ack).
# 확인 주체는 actor_role 로 정한다. event_ack 은 CONTROL_ROOM·FOREMAN_PWA 채널에 ADMIN·FOREMAN 역할 아무 조합이나
# 계약상 유효하다(관리자가 PWA 로, 반장이 관제 화면으로 확인할 수 있다). 채널은 받은 값을 그대로 남긴다.
_CONTRACT_ACK_RULES: dict[str, tuple[frozenset[str], Mapping[str, AckActor]]] = {
    "event_ack": (
        frozenset({"CONTROL_ROOM", "FOREMAN_PWA"}),
        {"ADMIN": AckActor.ADMIN, "FOREMAN": AckActor.FOREMAN},
    ),
    "site_ack": (frozenset({"SITE_BUTTON"}), {"SITE": AckActor.SITE_BUTTON}),
}


def ack_from_contract(msg_type: str, payload: Mapping[str, Any]) -> ContractAck:
    """event_ack(EC2 → Tier 0)·site_ack(노트북 → Tier 0) payload → ContractAck.

    계약이 거부하는 채널·역할(현장 버튼이 event_ack 로 오는 등)이면 RiskInputError. 확인 주체는 actor_role 로 정하고
    channel 은 받은 값을 그대로 남긴다. event_sequence 가 있으면 1 이상의 정수여야 한다. event_ack 에 event_sequence 가
    없으면(이전 EC2) 대상 미지정 확인으로 경고를 남긴다: Tier 0 가 기록할 때의 상태를 대상으로 보므로 상향 뒤 늦게
    도착하면 상향된 경보까지 멈출 수 있다(EC2 가 항상 채워야 한다). 쓰는 쪽: tracker.acknowledge(**ack.acknowledge_args()) 후 나온 AlarmStop 을
    alarm_stop_contract_payload(signal, ack_id=ack.ack_id) 로 보낸다(계약 alarm_stop.event_sequence = 이 target_sequence).
    """
    rules = _CONTRACT_ACK_RULES.get(msg_type)
    if rules is None:
        raise RiskInputError(f"확인 메시지가 아니다: {msg_type}")
    channels, roles = rules
    payload = as_mapping(payload, msg_type)
    ack = as_mapping(payload.get("ack"), f"{msg_type}.ack")
    channel, role = ack.get("channel"), ack.get("actor_role")
    if channel not in channels or role not in roles:
        raise RiskInputError(f"{msg_type}: 채널·역할 ({channel}, {role}) 은 이 메시지로 받지 않는다")
    event_id = as_str(payload.get("event_id"), f"{msg_type}.event_id")
    target = payload.get("event_sequence")
    if target is None and msg_type == "event_ack":
        _log.warning(
            "대상 미지정 확인: event_ack %s 에 event_sequence 가 없다(이전 EC2). 기록할 때의 상태를 대상으로 본다", event_id
        )
    return ContractAck(
        event_id=event_id,
        actor=roles[role],
        channel=channel,
        actor_id=as_str(ack.get("actor_id"), f"{msg_type}.ack.actor_id"),
        acked_at=as_int(ack.get("acked_at"), f"{msg_type}.ack.acked_at", minimum=0),
        target_sequence=None if target is None else as_int(target, f"{msg_type}.event_sequence", minimum=1),
        ack_id=as_str(ack.get("ack_id"), f"{msg_type}.ack.ack_id"),
    )


def alarm_stop_contract_payload(signal: AlarmStop, *, ack_id: str) -> dict[str, Any]:
    """내부 AlarmStop → I0 alarm_stop payload (contracts/messages/alarm_stop.schema.json).

    계약 event_sequence 는 signal.target_sequence(확인 대상 상태)다. signal.event_sequence(ACKNOWLEDGED 전이 번호)가
    아니다. 대상을 모르면(None) 필드를 싣지 않는다(노트북은 지금 경보를 멈춘다). ack_id 는 확인을 처음 받은 쪽(EC2
    event_ack 또는 노트북 site_ack)이 발급한 값을 그대로 넘긴다.
    """
    if signal.acked_at is None:
        raise ValueError("alarm_stop: acked_at 이 없다")
    actor = AckActor(signal.acked_by)
    channel, actor_role = signal.channel or _DEFAULT_CHANNEL[actor], _ACK_ROLE[actor]
    payload: dict[str, Any] = {"event_id": signal.event_id}
    if signal.target_sequence is not None:
        payload["event_sequence"] = signal.target_sequence
    payload["ack"] = {
        "ack_id": ack_id,
        "channel": channel,
        "actor_role": actor_role,
        "actor_id": signal.actor_id,
        "acked_at": signal.acked_at,
    }
    return payload


@dataclass(frozen=True, kw_only=True)
class AlertClear(_LocalSignal):
    """해소(RESOLVED). 표시를 끝낸다."""

    @property
    def kind(self) -> LocalSignalKind:
        return LocalSignalKind.ALERT_CLEAR


LocalSignal = LocalAlert | AlarmStop | AlertClear
