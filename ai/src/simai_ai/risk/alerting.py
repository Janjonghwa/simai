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

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from simai_ai.risk.common import Origin, Severity
from simai_ai.risk.engine import NonLiveAlertError


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
    """확인(ACK). 반복 소리만 멈춘다. 표시는 해소까지 유지한다."""

    acked_by: AckActor
    actor_id: str

    @property
    def kind(self) -> LocalSignalKind:
        return LocalSignalKind.ALARM_STOP

    def to_payload(self) -> dict[str, Any]:
        return super().to_payload() | {"acked_by": str(self.acked_by), "actor_id": self.actor_id}


@dataclass(frozen=True, kw_only=True)
class AlertClear(_LocalSignal):
    """해소(RESOLVED). 표시를 끝낸다."""

    @property
    def kind(self) -> LocalSignalKind:
        return LocalSignalKind.ALERT_CLEAR


LocalSignal = LocalAlert | AlarmStop | AlertClear
