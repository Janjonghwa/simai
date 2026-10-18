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
- health.active_site_alerts 항목도 같은 기준이다. 실제 알림 여부를 가르는 필드(event_id, event_sequence, source,
  severity, acknowledged)가 틀린 항목은 쓰지 않는다. event_id 를 읽을 수 있으면 그 이벤트는 '이번 목록으로 판단 못 함'
  (undecidable)으로, event_id 도 못 읽으면 목록 전체를 '빠진 이벤트를 해소로 볼 수 없음'(partial)으로 표시한다.
  틀린 항목 하나 때문에 진짜 경보를 끄지 않기 위해서다.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

# site_ack 를 정의한 계약 버전(contracts/examples/site_ack.json 과 같다). 2.1.0: 확인 대상 event_sequence.
SITE_ACK_SCHEMA_VERSION = "2.1.0"
# laptop_heartbeat 계약 버전(contracts/examples/laptop_heartbeat.json 과 같다)
LAPTOP_HEARTBEAT_SCHEMA_VERSION = "1.2.0"

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
    # 확인 대상 상태의 event_sequence(계약 2.1.0). 이보다 뒤에 다시 울리기 시작한 경보(상향·재개)는 멈추지 않는다.
    # None 은 이전 송신 측(대상 모름): 지금 경보를 멈춘다.
    event_sequence: int | None = None
    sent_at: int | None = None
    contract_warnings: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class SiteAlertItem:
    """활성 현장 경보 하나 (health.$defs.active_site_alert). source 는 LIVE, severity 는 CRITICAL·HIGH 로 확인했다."""

    event_id: str
    event_sequence: int
    acknowledged: bool
    severity: Literal["CRITICAL", "HIGH"]
    summary: str
    rule_id: str | None
    zone_id: str | None


@dataclass(frozen=True)
class Health:
    """health 의 현장 경보 재동기화 부분 (명세 3.10, 계약 health.active_site_alerts).

    active_site_alerts 가 None 이면 필드가 없거나 배열이 아니다(이전 송신 측). 빈 목록과 다르다: None 으로는 경보를
    끄지 않는다.
    """

    sent_at: int | None
    active_site_alerts: tuple[SiteAlertItem, ...] | None
    active_site_alerts_as_of: int | None
    undecidable_event_ids: frozenset[str] = frozenset()  # 항목이 틀려 이번 목록으로 판단하지 않는 이벤트
    partial: bool = False  # event_id 도 읽을 수 없는 항목이 있다: 목록에서 빠진 이벤트를 해소로 보지 않는다
    contract_warnings: tuple[str, ...] = field(default=())
    # 장애 표시용(link_status, S15P31A101-91). 없거나 형식이 틀리면 None(모름): 정상으로 보지 않는다.
    system_health: str | None = None  # NOMINAL / DEGRADED / UNSAFE_UNKNOWN
    t_latest_global: int | None = None  # 처리 진행 판단(늘어나는지)에만 쓴다. 다른 시계와 비교하지 않는다


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
    seq = p.get("event_sequence")
    if seq is not None and (not _is_int(seq) or seq < 1):
        # 대상 상태를 잘못 실은 정지로 상향된 경보를 멈추면 위험하다. 쓰지 않는다(다음 health 목록이 확인 상태를 맞춘다).
        raise Rejected("MALFORMED", "alarm_stop: event_sequence 가 1 이상의 정수가 아니다")
    return AlarmStop(
        event_id=event_id,
        event_sequence=seq,
        sent_at=env.sent_at,
        ack=OperatorAck(
            ack_id=s("ack_id"),
            channel=s("channel"),
            actor_role=s("actor_role"),
            actor_id=s("actor_id"),
            acked_at=acked_at if _is_int(acked_at) else None,
        ),
        contract_warnings=tuple(warnings),
    )


def _parse_site_alert_item(raw: Any) -> SiteAlertItem | str | None:
    """항목 하나. 쓸 수 있으면 SiteAlertItem, event_id 만 읽히면 그 event_id(판단 못 함), 아무것도 못 읽으면 None."""
    if not isinstance(raw, dict):
        return None
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not _UUID_V7.match(event_id):
        return None
    seq = raw.get("event_sequence")
    acknowledged = raw.get("acknowledged")
    severity = raw.get("severity")
    if (
        raw.get("source") != "LIVE"  # PLANNED·WHAT_IF·REPLAY 는 현장 경보로 울리지 않는다(AGENTS.md 규칙 5)
        or severity not in SITE_SEVERITIES
        or not _is_int(seq)
        or seq < 1
        or not isinstance(acknowledged, bool)
    ):
        return event_id
    rule_id = raw.get("rule_id")
    rule_id = rule_id if isinstance(rule_id, str) and rule_id else None
    zone_id = raw.get("zone_id")
    zone_id = zone_id if isinstance(zone_id, str) and zone_id else None
    summary = raw.get("summary")
    summary = summary if isinstance(summary, str) and summary else (rule_id or "현장 경보")
    return SiteAlertItem(event_id, seq, acknowledged, severity, summary, rule_id, zone_id)


SYSTEM_HEALTHS = ("NOMINAL", "DEGRADED", "UNSAFE_UNKNOWN")


def parse_health(env: Envelope) -> Health:
    p = env.payload
    raw_alerts = p.get("active_site_alerts")
    as_of = p.get("active_site_alerts_as_of")
    as_of = as_of if _is_int(as_of) else None
    system_health = p.get("system_health")
    system_health = system_health if system_health in SYSTEM_HEALTHS else None
    t_latest = p.get("t_latest_global")
    t_latest = t_latest if _is_int(t_latest) and t_latest >= 0 else None
    link = {"system_health": system_health, "t_latest_global": t_latest}
    if not isinstance(raw_alerts, list):
        return Health(sent_at=env.sent_at, active_site_alerts=None, active_site_alerts_as_of=as_of, **link)
    items: dict[str, SiteAlertItem] = {}
    undecidable: set[str] = set()
    partial = False
    warnings: list[str] = []
    for raw in raw_alerts:
        parsed = _parse_site_alert_item(raw)
        if parsed is None:
            partial = True
            warnings.append("event_id 를 읽을 수 없는 항목")
        elif isinstance(parsed, str):
            undecidable.add(parsed)
            warnings.append(f"{parsed} 항목 형식 오류(LIVE·CRITICAL·HIGH·event_sequence·acknowledged)")
        else:
            prev = items.get(parsed.event_id)
            if prev is not None:
                warnings.append(f"{parsed.event_id} 가 두 번 나왔다")
            if prev is None or parsed.event_sequence > prev.event_sequence:
                items[parsed.event_id] = parsed
    for event_id in undecidable:
        items.pop(event_id, None)
    return Health(
        sent_at=env.sent_at,
        active_site_alerts=tuple(items.values()),
        active_site_alerts_as_of=as_of,
        undecidable_event_ids=frozenset(undecidable),
        partial=partial,
        contract_warnings=tuple(warnings),
        **link,
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


def build_site_ack(*, event_id: str, laptop_id: str, now_ms: int, event_sequence: int | None = None) -> dict[str, Any]:
    """현장 확인 버튼 → site_ack 봉투 (contracts/messages/site_ack.schema.json).

    ack_id 는 노트북이 발급한다. 다시 보낼 때는 같은 봉투(같은 ack_id·msg_id)를 보내므로 Tier 0 는 한 번만 기록한다.
    event_sequence 는 누를 때 표시하던 경보의 sequence 다(확인 대상 상태, 계약 2.1.0). Tier 0 는 alarm_stop 에 돌려준다.
    """
    if not _UUID_V7.match(event_id):
        raise ValueError("event_id 가 UUIDv7 이 아니다")
    if event_sequence is not None and (not _is_int(event_sequence) or event_sequence < 1):
        raise ValueError("event_sequence 는 1 이상의 정수다")
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
            **({} if event_sequence is None else {"event_sequence": event_sequence}),
            "ack": {
                "ack_id": ack_id,
                "channel": "SITE_BUTTON",
                "actor_role": "SITE",
                "actor_id": laptop_id,  # 노트북 기기 ID (봉투 source_id 와 같다). 사람 이름을 넣지 않는다
                "acked_at": now_ms,
            },
        },
    }


def build_laptop_heartbeat(*, laptop_id: str, now_ms: int, payload: dict[str, Any]) -> dict[str, Any]:
    """laptop_heartbeat 봉투 (contracts/messages/laptop_heartbeat.schema.json). payload 는 link_status 가 만든다.

    노트북 ID 는 봉투 source_id, 시각은 봉투 sent_at(노트북 벽시계)이다. 받는 쪽은 자기 단조 시계로 누락을 잰다.
    """
    return {
        "schema_version": LAPTOP_HEARTBEAT_SCHEMA_VERSION,
        "msg_type": "laptop_heartbeat",
        "msg_id": str(uuid.uuid4()),
        "sent_at": now_ms,
        "source_id": laptop_id,
        "payload": payload,
    }
