"""outbox: 저널의 acked_offset 이후 구간을 EC2 로 보낸다 (명세 3.2 저널 계약·재동기화, 12.3 I1, 부록 D-G3-07·14).

outbox 는 별도 저장소가 아니다. 저널에서 acked_offset 이후 구간을 읽어 보내고, EC2 의 journal_ack 마다
acked_offset 을 영속 저장(AckedOffsetStore)한 뒤 앞으로 옮긴다. 그래서 "저널에는 있는데 전송 대기열에는 없는"
상태가 생기지 않는다.

- 전송은 주입한다(Transport). 보낼 메시지 목록을 받아 보내고, 연결 문제면 TransportError(또는 OSError)를 낸다.
  실패하면 커서를 옮기지 않고 다음 pump 에서 같은 기록부터 다시 보낸다.
- I1 형식 변환은 보낼 때 여기서만 한다(to_i1_transition_payload). 저널에는 lifecycle payload 를 그대로 둔다.
- 메시지 ID 는 저널에 기록할 때 정한 msg_id 를 그대로 쓴다. 재전송·재시작 뒤에도 같으므로 EC2 가 중복을 걸러 낸다.
- resent: 이번 프로세스에서 이미 보낸 기록이거나 시작할 때 이미 저널에 있던 기록(재시작 전 보냈을 수 있음)이면 true.
- 시작할 때는 acked_offset 이후 전체를 다시 보낸다(새 Pod 는 acked_offset 이후 전이부터 재전송, 3.2 식별자 규칙).
- journal_ack.resend_from_offset 이 오면 그 offset 부터 다시 보낸다(EC2 가 빈 구간을 찾음).
- 정리(compact)는 min(acked_offset, last_applied_journal_offset) 이하만 저널에 맡긴다. last_applied_journal_offset 은
  체크포인트(S15P31A101-51)가 준다.
- 계약 필수 값을 모르는 기록(판정 전이의 evidence_ref 를 끝내 모름, 확인의 ack_id·채널 없음)은 지어내지 않는다.
  판정이 아닌 전이(확인·복원·ID 교정)의 근거를 모르면 보류하지 않고 evidence_ref=null 로 보낸다(계약 1.4.0). 보내기 전에
  잡아 그 기록에서 전송을 보류하고(blocked) 경고를 남긴다. 순서를 지키려고 그 뒤 기록도 보내지 않는다.
- 재전송을 마친 뒤 event_state_snapshot 을 보내는 일은 추적기 상태를 가진 쪽(서비스 조립)이 drained 를 보고 한다.
- 운영자 종료 기록(lifecycle payload.close, S15P31A101-200)은 계약 resolution(risk_event_transition 2.0.0)으로 바꿔 보낸다.
- 운영자 상향 기록(lifecycle payload.escalation, S15P31A101-272)은 받은 계약 operator_escalation 그대로라 바꾸지 않고 보낸다(2.1.0).
"""

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from simai_ai.eventlog.acked import AckedOffsetStore
from simai_ai.eventlog.journal import (
    ACK_ACTOR_ROLE,
    ACK_CHANNELS,
    EventJournal,
    ack_id_of,
    evidence_ref_of,
    is_judgment_transition,
)
from simai_ai.eventlog.record import JournalEntry
from simai_ai.risk.clock import Clock

# risk_event_transition 2.0.0(1.3.0 confidence_reasons·resumed 추가, 1.4.0 판정이 아닌 전이의 evidence_ref null 허용,
# 2.0.0 운영자 종료 해소 resolution: 있으면 RESOLVED 의 VERIFIED·evidence_ref 필수가 풀린다, S15P31A101-200,
# 2.1.0 운영자 상향 escalation: 재개가 아닌 UPDATED 에만, S15P31A101-272)
SCHEMA_VERSION = "2.1.0"
# 계약에서 선택(없어도 됨)이고 null 을 허용하지 않는 필드. lifecycle payload 는 비었을 때 null 로 싣는다.
_OPTIONAL_NOT_NULL = ("ack", "related_event_id")
_OPERATOR_ACK_KEYS = ("ack_id", "channel", "actor_role", "actor_id", "acked_at")
_RESOLUTION_KEYS = ("close_id", "channel", "actor_role", "actor_id", "requested_at", "reason")


class TransportError(RuntimeError):
    """전송 실패(연결 끊김 등). outbox 는 커서를 옮기지 않고 다음에 다시 보낸다."""


_log = logging.getLogger(__name__)


class OutboxError(RuntimeError):
    """저널과 acked_offset 이 맞지 않는다(지어내지 않고 멈춘다)."""


class UnsendableEntry(OutboxError):
    """계약 필수 값을 몰라 I1 메시지로 만들 수 없는 기록. outbox 는 이 기록에서 전송을 보류한다."""

    def __init__(self, entry: JournalEntry, reason: str) -> None:
        super().__init__(f"offset {entry.journal_offset}: {reason}")
        self.journal_offset = entry.journal_offset
        self.reason = reason


@dataclass(frozen=True)
class Blocked:
    """전송 보류 상태. 이 offset 부터 보내지 못한다(서비스가 health·로그로 알린다)."""

    journal_offset: int
    reason: str


class Transport(Protocol):
    def __call__(self, messages: Sequence[dict[str, Any]]) -> None: ...


class MessageBuilder(Protocol):
    def __call__(self, entry: JournalEntry, *, resent: bool, sent_at: int, source_id: str) -> dict[str, Any]: ...


def operator_ack(entry: JournalEntry) -> dict[str, Any]:
    """저널 기록의 확인(payload.ack)을 계약 operator_ack 으로 바꾼다. 모르는 값은 지어내지 않는다(UnsendableEntry).

    lifecycle AckRecord 는 {actor, actor_id, acked_at, recorded_at, target_sequence, channel} 이고 계약은 {ack_id,
    channel, actor_role, actor_id, acked_at} 이다(추가 필드 금지). ack_id 는 저널이 기록할 때 받은 계약 값(meta.ack_id),
    channel 은 받은 값 그대로(AckRecord.channel), actor_role 은 AckActor 와 1:1 이다(SITE_BUTTON ↔ SITE).
    """
    ack = entry.payload.get("ack")
    if not isinstance(ack, Mapping):
        raise UnsendableEntry(entry, "확인 기록이 아니다")
    ack_id = ack_id_of(entry)
    if ack_id is None:
        raise UnsendableEntry(entry, "확인 기록에 ack_id 가 없다(계약 operator_ack 필수)")
    channel = ack.get("channel")
    if channel not in ACK_CHANNELS:
        raise UnsendableEntry(entry, f"확인 채널을 모른다: {channel!r}")
    role = ack.get("actor_role") or ACK_ACTOR_ROLE.get(str(ack.get("actor")))
    if role is None:
        raise UnsendableEntry(entry, f"모르는 확인 주체: {ack.get('actor')!r}")
    if (channel == "SITE_BUTTON") != (role == "SITE"):
        raise UnsendableEntry(entry, f"채널 {channel} 과 확인 주체 {role} 가 맞지 않는다")
    out = {
        "ack_id": ack_id,
        "channel": channel,
        "actor_role": role,
        "actor_id": ack.get("actor_id"),
        "acked_at": ack.get("acked_at"),
    }
    return {k: out[k] for k in _OPERATOR_ACK_KEYS}


def operator_resolution(entry: JournalEntry) -> dict[str, Any]:
    """저널 기록의 운영자 종료(payload.close, lifecycle CloseRecord) → 계약 operator_resolution. 모르는 값은 지어내지 않는다.

    lifecycle 기록은 {close_id, channel, actor_role, actor_id, requested_at, recorded_at, reason} 이고 계약은 kind 를 더하고
    recorded_at 을 뺀다(추가 필드 금지, Tier 0 기록 시각은 전이의 occurred_at). RESOLVED 가 아니거나 값이 비면 UnsendableEntry.
    """
    close = entry.payload.get("close")
    if not isinstance(close, Mapping) or entry.payload.get("transition") != "RESOLVED":
        raise UnsendableEntry(entry, "운영자 종료 기록은 RESOLVED 전이에만 있다")
    out: dict[str, Any] = {"kind": "OPERATOR_CLOSED"}
    for key in _RESOLUTION_KEYS:
        value = close.get(key)
        if value is None or value == "" or isinstance(value, bool):
            raise UnsendableEntry(entry, f"운영자 종료 기록에 {key} 가 없다(계약 operator_resolution 필수)")
        out[key] = value
    if (out["channel"], out["actor_role"]) != ("CONTROL_ROOM", "ADMIN"):
        raise UnsendableEntry(entry, f"운영자 종료 채널·역할 ({out['channel']}, {out['actor_role']}) 은 계약이 받지 않는다")
    return out


def to_i1_transition_payload(entry: JournalEntry, *, resent: bool) -> dict[str, Any]:
    """저널 기록 하나 → I1 risk_event_transition payload. 저널의 기록 자체는 바꾸지 않는다(복사본을 바꾼다).

    - 계약에 없는 lifecycle 필드(changes, evidence 등)는 그대로 둔다(수신 측은 모르는 필드를 무시한다, 12.1).
    - 값이 null 인 선택 필드(ack, related_event_id)는 뺀다.
    - evidence_ref 가 비었으면(확인·복원 전이) 저널이 남긴 그 이벤트의 직전 판정 근거(meta.evidence_ref)를 쓴다.
      그것도 없으면 판정이 아닌 전이(확인·복원·ID 교정)는 null 로 보내고(계약 1.4.0), 판정 전이는 UnsendableEntry
      (지어내지 않음).
    - 확인 기록(lifecycle 은 UPDATED + 변경 사유 ACKNOWLEDGED)은 계약대로 transition=ACKNOWLEDGED, ack=operator_ack.
    - 운영자 종료 기록(close)은 계약 resolution(operator_resolution)으로 바꾸고 close 는 뺀다(2.0.0).
    - journal_offset·resent 를 붙인다.
    """
    payload = entry.payload_dict()
    for key in _OPTIONAL_NOT_NULL:
        if key in payload and payload[key] is None:
            del payload[key]
    evidence = evidence_ref_of(entry)
    if evidence is None and is_judgment_transition(entry.payload):
        # 판정 전이는 계약 필수. 정리 뒤에도 근거 색인이 지키지만, 그래도 모르면 지어내지 않는다.
        raise UnsendableEntry(entry, "evidence_ref 를 모른다(이벤트의 판정 근거 기록이 저널에 없다)")
    payload["evidence_ref"] = evidence
    if "ack" in payload:
        payload["transition"] = "ACKNOWLEDGED"
        payload["ack"] = operator_ack(entry)
    if "close" in payload:
        payload["resolution"] = operator_resolution(entry)
        del payload["close"]
    payload["journal_offset"] = entry.journal_offset
    payload["resent"] = resent
    return payload


def build_transition_message(
    entry: JournalEntry,
    *,
    resent: bool,
    sent_at: int,
    source_id: str,
    schema_version: str = SCHEMA_VERSION,
) -> dict[str, Any]:
    """저널 기록 하나를 I1 봉투 메시지로 만든다(payload 는 to_i1_transition_payload). msg_id 는 기록 때 정한 값."""
    return {
        "schema_version": schema_version,
        "msg_type": entry.kind,
        "msg_id": entry.msg_id,
        "sent_at": sent_at,
        "source_id": source_id,
        "payload": to_i1_transition_payload(entry, resent=resent),
    }


@dataclass(frozen=True)
class JournalAckResult:
    """journal_ack 처리 결과. accepted=False 면 아무것도 바꾸지 않았다(reason)."""

    accepted: bool
    advanced: bool
    acked_offset: int | None
    resend_from: int | None = None
    reason: str | None = None


def _as_offset(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


class Outbox:
    """저널의 acked_offset 이후 구간 전송기. 보내는 스레드 하나(pump)와 받는 스레드(on_journal_ack)가 함께 써도 된다."""

    def __init__(
        self,
        journal: EventJournal,
        store: AckedOffsetStore,
        *,
        gateway_id: str,
        transport: Transport,
        clock: Clock,
        batch_size: int = 64,
        build_message: MessageBuilder = build_transition_message,
    ) -> None:
        if not gateway_id:
            raise ValueError("gateway_id 가 비었다")
        if batch_size <= 0:
            raise ValueError("batch_size 는 양수")
        self._journal = journal
        self._store = store
        self._gateway_id = gateway_id
        self._transport = transport
        self._clock = clock
        self._batch_size = batch_size
        self._build = build_message
        self._lock = threading.Lock()
        self._acked = store.load()
        last, first = journal.last_offset, journal.first_offset
        if self._acked is not None and (last is None or self._acked > last):
            raise OutboxError(f"acked_offset {self._acked} 이 저널 끝({last})보다 뒤다. 저널이 사라졌거나 바뀌었다")
        if first is not None and self._acked is not None and self._acked + 1 < first:
            raise OutboxError(f"acked_offset {self._acked} 뒤 구간이 정리돼 없다(첫 offset {first})")
        # 시작할 때 이미 있던 기록은 재시작 전에 보냈을 수 있으므로 resent 로 보낸다.
        self._resend_upto = -1 if last is None else last
        self._sent_high = -1
        self._cursor = self._start()
        self._generation = 0
        self.ec2_sync_mode: bool | None = None
        self._blocked: Blocked | None = None
        self.last_error: BaseException | None = None

    def _start(self) -> int:
        if self._acked is not None:
            return self._acked + 1
        first = self._journal.first_offset
        return first if first is not None else self._journal.next_offset

    # --- 상태 ---------------------------------------------------------------------------

    @property
    def acked_offset(self) -> int | None:
        with self._lock:
            return self._acked

    @property
    def cursor(self) -> int:
        """다음에 보낼 journal_offset."""
        with self._lock:
            return self._cursor

    @property
    def drained(self) -> bool:
        """지금 저널 끝까지 보냈는지(확인까지 받았다는 뜻은 아니다)."""
        with self._lock:
            return self._cursor >= self._journal.next_offset

    @property
    def blocked(self) -> Blocked | None:
        """보내지 못하고 멈춘 기록(계약 필수 값을 모름). None 이면 막힘 없음."""
        with self._lock:
            return self._blocked

    def pending(self, limit: int | None = None) -> list[JournalEntry]:
        """outbox = acked_offset 이후 저널 구간(EC2 확인을 아직 받지 못한 기록)."""
        with self._lock:
            start = self._start()
        return self._journal.read(start, limit)

    def pending_count(self) -> int:
        with self._lock:
            return self._journal.next_offset - self._start()

    # --- 보내기 -------------------------------------------------------------------------

    def pump(self, max_batches: int | None = None) -> int:
        """커서부터 저널 끝까지(최대 max_batches 묶음) 보낸다. 보낸 기록 수. 전송 실패면 거기서 멈춘다."""
        sent = 0
        batches = 0
        while max_batches is None or batches < max_batches:
            with self._lock:
                generation = self._generation
                entries = self._journal.read(self._cursor, self._batch_size)
                if not entries:
                    break
                sent_at = self._clock.now_ms()
                messages = []
                stop: UnsendableEntry | None = None
                for e in entries:
                    try:
                        messages.append(
                            self._build(
                                e, resent=self._is_resend(e.journal_offset), sent_at=sent_at, source_id=self._gateway_id
                            )
                        )
                    except UnsendableEntry as exc:
                        stop = exc
                        break
                if stop is not None:
                    if self._blocked is None or self._blocked.journal_offset != stop.journal_offset:
                        _log.warning("outbox 전송 보류: %s", stop)
                    self._blocked = Blocked(stop.journal_offset, stop.reason)
                    entries = entries[: len(messages)]
                else:
                    self._blocked = None
            if not messages:
                break
            try:
                self._transport(messages)
            except (TransportError, OSError) as exc:
                self.last_error = exc
                break
            last = entries[-1].journal_offset
            with self._lock:
                self._sent_high = max(self._sent_high, last)
                if generation == self._generation:
                    self._cursor = max(self._cursor, last + 1)
            sent += len(entries)
            batches += 1
            if stop is not None:
                break
        return sent

    def _is_resend(self, offset: int) -> bool:
        return offset <= max(self._resend_upto, self._sent_high)

    def reset(self) -> None:
        """연결이 끊겼다 다시 이어졌을 때. acked_offset 이후부터 다시 보낸다(resent=true)."""
        with self._lock:
            self._cursor = self._start()
            self._generation += 1

    # --- EC2 확인 -----------------------------------------------------------------------

    def on_journal_ack(self, payload: Mapping[str, Any]) -> JournalAckResult:
        """journal_ack payload 를 반영한다. acked_offset 을 먼저 영속 저장하고 나서 메모리 값을 옮긴다.

        - gateway_id 가 다르거나 acked_offset 이 저널 끝보다 뒤면 거절한다(아무것도 바꾸지 않음).
        - 지금보다 작은 acked_offset 은 되돌리지 않는다(늦게 온 확인).
        - resend_from_offset 이 있으면 그 offset(acked_offset 이후로 맞춤)부터 다시 보낸다.
        """
        if payload.get("gateway_id") != self._gateway_id:
            return JournalAckResult(False, False, self.acked_offset, reason="gateway_mismatch")
        acked = _as_offset(payload.get("acked_offset"))
        if acked is None:
            return JournalAckResult(False, False, self.acked_offset, reason="invalid_acked_offset")
        resend = payload.get("resend_from_offset")
        if resend is not None and _as_offset(resend) is None:
            return JournalAckResult(False, False, self.acked_offset, reason="invalid_resend_from_offset")
        last = self._journal.last_offset
        if last is None or acked > last:
            return JournalAckResult(False, False, self.acked_offset, reason="beyond_journal")
        with self._lock:
            advanced = False
            if self._acked is None or acked > self._acked:
                self._store.save(acked)
                self._acked = acked
                advanced = True
            assert self._acked is not None
            floor = self._acked + 1
            resend_from = None
            if resend is not None:
                resend_from = max(floor, resend)
                if resend_from < self._cursor:
                    self._cursor = resend_from
                    self._generation += 1
            self._cursor = max(self._cursor, floor)
            sync_mode = payload.get("sync_mode")
            if isinstance(sync_mode, bool):
                self.ec2_sync_mode = sync_mode
            return JournalAckResult(True, advanced, self._acked, resend_from=resend_from)

    # --- 정리 ---------------------------------------------------------------------------

    def compact(self, last_applied_journal_offset: int | None) -> int:
        """min(acked_offset, last_applied_journal_offset) 이하 구간 중 보관 기간이 지난 세그먼트를 지운다."""
        return self._journal.compact(self.acked_offset, last_applied_journal_offset)
