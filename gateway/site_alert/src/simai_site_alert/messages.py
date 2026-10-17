"""I0 제어 채널 메시지 해석과 site_ack 생성 (contracts/messages: local_alert, alarm_stop, health, site_ack).

형식의 정본은 저장소의 contracts/ 스키마다. 단말은 실행 중에 스키마를 읽지 않고(경보 경로에 파일·검증기 의존을 두지 않음)
여기서 필요한 필드만 직접 확인한다. 시험(tests/test_contract.py)이 계약 예시를 이 해석기에 넣고, 만든 site_ack 를
스키마로 검증해 둘이 어긋나지 않게 한다.

판단 기준:
- 실제 알림 여부를 가르는 필드(event_id, event_sequence, lifecycle, source, severity)는 엄격히 본다. source 가 LIVE 가
  아니거나 없으면, severity 가 CRITICAL·HIGH 가 아니면 거부한다(AGENTS.md 규칙 5, 명세 7.3, 계약 const).
- 화면 표시용 필드(summary, rule_id 등)가 빠지거나 형식이 틀려도 경보 자체는 버리지 않는다. 진짜 CRITICAL 을 표시용
  필드 하나 때문에 조용히 버리는 쪽이 더 위험하다. 대신 contract_warnings 에 남겨 로그로 알린다.
- 수신 측은 모르는 필드를 무시한다(envelope 계약).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

# site_ack 를 정의한 계약 버전(contracts/examples/site_ack.json 과 같다).
SITE_ACK_SCHEMA_VERSION = "1.2.0"

SITE_SEVERITIES = ("CRITICAL", "HIGH")  # 노트북 로컬 경보 대상 (명세 7.3)
LIFECYCLES = ("RAISED", "UPDATED", "RESOLVED")
_UUID_V7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Rejected(ValueError):
    """받았지만 경보로 쓰지 않는 메시지. reason 은 집계용 짧은 코드."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Envelope:
    msg_type: str
    msg_id: str | None
    sent_at: int | None
    source_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class LocalAlert:
    event_id: str
    event_sequence: int
    lifecycle: Literal["RAISED", "UPDATED", "RESOLVED"]
    severity: Literal["CRITICAL", "HIGH"]
    acknowledged: bool
    confidence: str | None
    needs_check: bool
    rule_id: str | None
    zone_id: str | None
    world_ids: tuple[str, ...]
    summary: str
    observation_time: int | None
    evaluated_at: int | None
    sent_at: int | None
    contract_warnings: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class OperatorAck:
    ack_id: str | None
    channel: str | None
    actor_role: str | None
    actor_id: str | None
    acked_at: int | None


@dataclass(frozen=True)
class AlarmStop:
    event_id: str
    ack: OperatorAck
    contract_warnings: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class Health:
    """health 는 지금 받기만 한다. active_site_alerts 재동기화는 S15P31A101-161 몫이라 원본 목록만 들고 있다."""

    sent_at: int | None
    active_site_alerts: list[dict[str, Any]] | None  # 필드가 없으면 None(빈 목록과 다르다: 없으면 경보를 끄지 않는다)
    active_site_alerts_as_of: int | None


@dataclass(frozen=True)
class Other:
    """단말이 쓰지 않는 메시지(clock_sync_request 등). 무시한다."""

    msg_type: str


Message = LocalAlert | AlarmStop | Health | Other


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_envelope(obj: Any) -> Envelope:
    if not isinstance(obj, dict):
        raise Rejected("MALFORMED", "봉투가 JSON 객체가 아니다")
    msg_type = obj.get("msg_type")
    payload = obj.get("payload")
    if not isinstance(msg_type, str) or not msg_type:
        raise Rejected("MALFORMED", "msg_type 이 없다")
    if not isinstance(payload, dict):
        raise Rejected("MALFORMED", f"{msg_type}: payload 가 객체가 아니다")
    msg_id = obj.get("msg_id")
    sent_at = obj.get("sent_at")
    source_id = obj.get("source_id")
    return Envelope(
        msg_type=msg_type,
        msg_id=msg_id if isinstance(msg_id, str) else None,
        sent_at=sent_at if _is_int(sent_at) else None,
        source_id=source_id if isinstance(source_id, str) else None,
        payload=payload,
    )


def _event_id(p: dict[str, Any], msg_type: str) -> str:
    event_id = p.get("event_id")
    if not isinstance(event_id, str) or not _UUID_V7.match(event_id):
        raise Rejected("MALFORMED", f"{msg_type}: event_id 가 UUIDv7 이 아니다")
    return event_id


def parse_local_alert(env: Envelope) -> LocalAlert:
    p = env.payload
    event_id = _event_id(p, "local_alert")
    source = p.get("source")
    if source != "LIVE":
        # PLANNED·WHAT_IF·REPLAY 와 출처 없는 메시지는 실제 알림으로 내지 않는다.
        raise Rejected("NOT_LIVE", f"local_alert: source={source!r} 는 현장 경보로 울리지 않는다 (LIVE 만)")
    severity = p.get("severity")
    if severity not in SITE_SEVERITIES:
        raise Rejected("NOT_SITE_SEVERITY", f"local_alert: severity={severity!r} 는 현장 경보 대상이 아니다 (CRITICAL·HIGH 만)")
    seq = p.get("event_sequence")
    if not _is_int(seq) or seq < 1:
        raise Rejected("MALFORMED", "local_alert: event_sequence 가 1 이상의 정수가 아니다")
    lifecycle = p.get("lifecycle")
    if lifecycle not in LIFECYCLES:
        raise Rejected("MALFORMED", f"local_alert: lifecycle={lifecycle!r} 를 모른다")

    warnings: list[str] = []

    def opt(name: str, check, default):
        if name not in p:
            warnings.append(f"{name} 없음")
            return default
        value = p[name]
        if not check(value):
            warnings.append(f"{name} 형식 오류")
            return default
        return value

    acknowledged = opt("acknowledged", lambda v: isinstance(v, bool), False)
    confidence = opt("confidence", lambda v: v in ("HIGH", "LOW"), None)
    needs_check = opt("needs_check", lambda v: isinstance(v, bool), False)
    summary = opt("summary", lambda v: isinstance(v, str) and bool(v), "")
    rule_id = opt("rule_id", lambda v: isinstance(v, str) and bool(v), None)
    zone_id = opt("zone_id", lambda v: v is None or (isinstance(v, str) and bool(v)), None)
    world_ids = opt("world_ids", lambda v: isinstance(v, list) and all(isinstance(w, str) for w in v), [])
    observation_time = opt("observation_time", _is_int, None)
    evaluated_at = opt("evaluated_at", _is_int, None)
    return LocalAlert(
        event_id=event_id,
        event_sequence=seq,
        lifecycle=lifecycle,
        severity=severity,
        acknowledged=acknowledged,
        confidence=confidence,
        # 신뢰도가 낮으면 같은 경보에 '확인 필요'를 붙인다(등급은 그대로, 명세 7.3).
        needs_check=needs_check or confidence == "LOW",
        rule_id=rule_id,
        zone_id=zone_id,
        world_ids=tuple(world_ids),
        summary=summary or (rule_id or "현장 경보"),
        observation_time=observation_time,
        evaluated_at=evaluated_at,
        sent_at=env.sent_at,
        contract_warnings=tuple(warnings),
    )


def parse_alarm_stop(env: Envelope) -> AlarmStop:
    p = env.payload
    event_id = _event_id(p, "alarm_stop")
    ack = p.get("ack")
    warnings: list[str] = []
    if not isinstance(ack, dict):
        warnings.append("ack 없음")
        ack = {}

    def s(name: str) -> str | None:
        v = ack.get(name)
        return v if isinstance(v, str) and v else None

    acked_at = ack.get("acked_at")
    return AlarmStop(
        event_id=event_id,
        ack=OperatorAck(
            ack_id=s("ack_id"),
            channel=s("channel"),
            actor_role=s("actor_role"),
            actor_id=s("actor_id"),
            acked_at=acked_at if _is_int(acked_at) else None,
        ),
        contract_warnings=tuple(warnings),
    )


def parse_health(env: Envelope) -> Health:
    p = env.payload
    alerts = p.get("active_site_alerts")
    as_of = p.get("active_site_alerts_as_of")
    return Health(
        sent_at=env.sent_at,
        active_site_alerts=alerts if isinstance(alerts, list) else None,
        active_site_alerts_as_of=as_of if _is_int(as_of) else None,
    )


def parse_message(obj: Any) -> Message:
    env = parse_envelope(obj)
    if env.msg_type == "local_alert":
        return parse_local_alert(env)
    if env.msg_type == "alarm_stop":
        return parse_alarm_stop(env)
    if env.msg_type == "health":
        return parse_health(env)
    return Other(env.msg_type)


def build_site_ack(*, event_id: str, laptop_id: str, now_ms: int) -> dict[str, Any]:
    """현장 확인 버튼 → site_ack 봉투 (contracts/messages/site_ack.schema.json).

    ack_id 는 노트북이 발급한다. 다시 보낼 때는 같은 봉투(같은 ack_id·msg_id)를 보내므로 Tier 0 는 한 번만 기록한다.
    """
    if not _UUID_V7.match(event_id):
        raise ValueError("event_id 가 UUIDv7 이 아니다")
    msg_id = str(uuid.uuid4())
    ack_id = str(uuid.uuid4())
    assert _UUID.match(msg_id) and _UUID.match(ack_id)
    return {
        "schema_version": SITE_ACK_SCHEMA_VERSION,
        "msg_type": "site_ack",
        "msg_id": msg_id,
        "sent_at": now_ms,
        "source_id": laptop_id,
        "payload": {
            "event_id": event_id,
            "ack": {
                "ack_id": ack_id,
                "channel": "SITE_BUTTON",
                "actor_role": "SITE",
                "actor_id": laptop_id,  # 노트북 기기 ID (봉투 source_id 와 같다). 사람 이름을 넣지 않는다
                "acked_at": now_ms,
            },
        },
    }
