"""event_state_snapshot 만들기·보내기 (명세 3.2 재동기화 알림 규칙, 12.3 I1, 15.1 G3, 부록 D-G3-05).

EC2 연결이 복구되면 outbox 가 acked_offset 이후 전이를 오름차순으로 다시 보내고, 그 연결에서 재전송을 마친 직후 같은
연결로 event_state_snapshot 을 보낸다. EC2 는 이 메시지를 받아야 동기화를 마치고, 여기 있고 최근 유효 관측 근거가 있는
이벤트만 DELAYED_ACTIVE 로 알린다.

**조립(S15P31A101-171).**

    snap = EventStateSnapshot(journal, gateway_id=..., clock=..., config_version=..., start_offset=..., seed=...,
                              current_state=tracker.get, resume_window_ms=policy.resume_window_ms)
    link = build_i1_link(..., make_outbox=lambda t: snap.bind(Outbox(journal, store, ..., transport=t)))
    snap.attach(link.client, link.publisher.envelope)
    ...
    link.outbox.compact(last_applied_journal_offset)   # link.outbox 는 bind 가 돌려준 SnapshotOutbox 다

- build_i1_link 에 snapshot_payload 를 주지 않는다. 스냅샷 송신은 attach 가 건 pump 가 한다(아래 경합 처리 때문).
- 정리는 bind 가 돌려준 SnapshotOutbox.compact 로만 부른다. 원래 Outbox.compact·EventJournal.compact 를 직접 부르면
  아직 접지 않은 기록이 지워질 수 있다(그때는 broken, 아래 회복).

**저널 순서와 맞추기.** 스냅샷은 "이미 보낸 전이의 결과"여야 한다. 그래서 활성 목록을 생명주기 추적기의 지금 상태가
아니라 저널 전이를 offset 순서로 접은(fold) 결과로 만든다(추적기는 판정 스레드에서 앞서 나가고 저널 쓰기는 별도 스레드).
- journal_offset 은 이 연결에서 outbox 가 보낸 마지막 offset(outbox.cursor - 1)이고, 활성 목록은 그 offset 까지 접은
  결과다. 만드는 사이 저널에 새 전이가 붙어도 싣지 않는다. 그 전이는 스냅샷 뒤에 실시간 전이로 나간다.
- 재요청(journal_ack.resend_from_offset)으로 커서가 물러나는 것은 수신 스레드에서 언제든 일어난다. SnapshotOutbox 가
  받아들인 재요청 수(rewinds)를 세고, 스냅샷을 만들기 시작할 때와 보내기 직전에 그 수와 커서가 그대로인지 본다. 바뀌었으면
  만든 스냅샷을 버린다. 보낸 뒤에 바뀌었으면 이 연결을 완료로 기록하지 않아, 재전송을 마친 뒤 다시 만들어 보낸다.
  (그래서 EC2 가 받는 마지막 스냅샷은 늘 마지막 재요청의 재전송 뒤에 있다. 재요청이 아닌 연결마다 한 번만 보낸다.)

**정리(compact)와 접기.** 정리는 min(acked_offset, last_applied_journal_offset) 이하를 지운다. ACK 는 수신 스레드에서
접기보다 먼저 올 수 있으므로, SnapshotOutbox.compact 는 상한을 접은 위치(applied_offset)로 더 줄인다. 그래도 직접 정리 등으로
접지 않은 기록이 사라지면 지어내지 않고 멈춘다(broken). 회복: reseed(seed, start_offset) 로 추적기 상태를 다시 시작
상태로 넣는다(재시작 seed 와 같은 전제). status() 로 밖(health·로그)에서 본다.

**접기 규칙** (저널 payload 는 LifecycleTracker 의 EventTransition.to_payload 그대로다).
- risk_event_transition 기록만 접는다. RESOLVED 면 활성 목록에서 뺀다(해소된 이벤트는 스냅샷에 없다, 계약).
- lifecycle: 전이가 RAISED 면 RAISED, 그 밖(UPDATED, 확인·복원·ID 교정 포함)은 UPDATED.
- acknowledged: 마지막 전이의 acknowledged(확인 기록이 있는지). 재개(RESUMED)는 lifecycle 이 확인을 비우므로 false.
- raised_at: 전이 payload 의 raised_at(lifecycle 이 싣게 되면), 없으면 RAISED 전이의 evaluated_at(lifecycle 의 raised_at
  과 같은 값), 앞 행, 해소 뒤 재개 기간 동안 남긴 값, 시작 상태(seed), 추적기의 같은 event_id 상태 순으로 찾는다(이벤트
  동안 바뀌지 않는다). 해소한 이벤트의 raised_at 은 재개 기간(resume_window_ms, 관측 시각 기준, lifecycle 과 같은 경계)
  동안 남긴다. 끝내 모르면 지어내지 않고 스냅샷을 만들지 않는다(not_ready).
- last_valid_observation_at(위험 조건을 마지막으로 유효 관측한 촬영 시각): 검증 상태가 VERIFIED 이고 변경 사유에 발생
  판정이 있어야만 생기는 사유(RAISED·RESUMED·SEVERITY·CONFIDENCE·ZONE·METRICS)가 있는 전이의 observation_time.
  VERIFICATION·VERSION·IDENTITY_REVISED 만 있는 전이는 위험이 사라진 관측(해소 대기)일 수도 있어 쓰지 않는다. 관측을 잃으면
  마지막 값을 둔다. 복원(RESTORED)·시작 상태는 null(재시작 직후 근거 없음, 계약). 전이 없이 갱신된 더 최근 위험 관측
  시각은 risk_observed_at(선택 주입: event_id → (그때의 event_sequence, 위험 조건을 VERIFIED 로 관측한 촬영 시각))이
  알려 주고 그 sequence 가 접은 sequence 이하일 때만 올린다. 추적기의 observation_time 은 위험이 사라진 관측(해소 대기)일
  수 있어 쓰지 않는다.

**만들지 않는 경우(None, not_ready 에 이유).** outbox 미연결, 막힌 기록(blocked), 커서가 접은 위치보다 뒤, 빈 저널(반영한
offset 이 없다. 계약 journal_offset 은 0 이상), config_version 을 모름, raised_at 을 모르는 활성 이벤트, 접기 실패(broken).

스레드: attach 의 pump·payload·follow 는 I1 송신 스레드, SnapshotOutbox.on_journal_ack 는 수신 스레드, compact 는 조립의
정리 주기에서 부른다. current_state·risk_observed_at 은 판정 스레드가 바꾸는 추적기를 읽으므로 조립이 판정 루프와 같은
lock 으로 감싼 함수를 넘기는 것이 좋다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from services.tier0.i1_client import SendFailed, Unserializable
from simai_ai.eventlog import TRANSITION_KIND, EventJournal, JournalEntry, JournalError
from simai_ai.risk import Clock, EventState, Lifecycle, Origin

log = logging.getLogger(__name__)

VERIFIED = "VERIFIED"
DEFAULT_RESUME_WINDOW_MS = 10_000  # LifecyclePolicy.resume_window_ms 초기값(6.4). 조립은 실제 정책 값을 넘긴다
# 발생 판정(finding)이 있을 때만 생기는 변경 사유(lifecycle._raise·_resume·_advance 의 finding 분기)
RISK_CHANGES = frozenset({"RAISED", "RESUMED", "SEVERITY", "CONFIDENCE", "ZONE", "METRICS"})
_REQUIRED = (
    "event_id",
    "event_sequence",
    "transition",
    "observation_time",
    "rule_id",
    "rule_version",
    "severity",
    "confidence",
    "verification",
    "needs_check",
    "world_ids",
    "zone_id",
    "acknowledged",
)

CurrentState = Callable[[str], EventState | None]
# event_id → (event_sequence, 그 상태에서 위험 조건을 VERIFIED 로 관측한 마지막 촬영 시각). 모르면 None
RiskObservedAt = Callable[[str], tuple[int, int] | None]


@dataclass(frozen=True)
class ActiveRow:
    """접은 활성 이벤트 하나(계약 event_state_snapshot.$defs.active_event 의 재료)."""

    event_id: str
    event_sequence: int
    lifecycle: str
    rule_id: str
    rule_version: int
    severity: str
    confidence: str
    verification: str
    needs_check: bool
    world_ids: tuple[str, ...]
    zone_id: str | None
    raised_at: int | None  # None: 모름(스냅샷을 만들지 않는다)
    last_valid_observation_at: int | None
    acknowledged: bool

    @classmethod
    def of_state(cls, state: EventState, last_valid: int | None = None) -> ActiveRow:
        """시작 상태(seed). 이번 프로세스의 유효 관측 근거가 없으면 last_valid_observation_at 은 null."""
        return cls(
            event_id=state.event_id,
            event_sequence=state.event_sequence,
            lifecycle=str(Lifecycle.RAISED if state.lifecycle is Lifecycle.RAISED else Lifecycle.UPDATED),
            rule_id=state.rule_id,
            rule_version=state.rule_version,
            severity=str(state.severity),
            confidence=str(state.confidence),
            verification=str(state.verification),
            needs_check=state.needs_check,
            world_ids=tuple(state.world_ids),
            zone_id=state.zone_id,
            raised_at=state.raised_at,
            last_valid_observation_at=last_valid,
            acknowledged=state.acknowledged,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_sequence": self.event_sequence,
            "lifecycle": self.lifecycle,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "severity": self.severity,
            "confidence": self.confidence,
            "verification": self.verification,
            "needs_check": self.needs_check,
            "world_ids": list(self.world_ids),
            "zone_id": self.zone_id,
            "raised_at": self.raised_at,
            "last_valid_observation_at": self.last_valid_observation_at,
            "acknowledged": self.acknowledged,
        }


class SnapshotOutbox:
    """eventlog.Outbox 를 감싼다. 재요청을 세고(스냅샷 경합), 정리 상한을 스냅샷이 접은 위치로 줄인다. 나머지는 그대로 넘긴다."""

    def __init__(self, inner: Any, snapshot: EventStateSnapshot) -> None:
        self._inner = inner
        self._snapshot = snapshot

    @property
    def inner(self) -> Any:
        return self._inner

    def on_journal_ack(self, payload: Mapping[str, Any]) -> Any:
        result = self._inner.on_journal_ack(payload)
        if getattr(result, "accepted", False) and getattr(result, "resend_from", None) is not None:
            self._snapshot._note_rewind()
        return result

    def compact(self, last_applied_journal_offset: int | None) -> int:
        """min(acked_offset, last_applied_journal_offset, 스냅샷이 접은 위치) 이하만 정리한다. 접은 것이 없으면 지우지 않는다."""
        folded = self._snapshot.applied_offset
        if last_applied_journal_offset is None or folded is None:
            return 0
        return self._inner.compact(min(last_applied_journal_offset, folded))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class EventStateSnapshot:
    """저널 접기로 event_state_snapshot 을 만들고, 재전송을 마친 연결에 보낸다(모듈 설명의 조립 순서)."""

    def __init__(
        self,
        journal: EventJournal,
        *,
        gateway_id: str,
        clock: Clock,
        config_version: Callable[[], str | None],
        start_offset: int,
        seed: Iterable[EventState] = (),
        current_state: CurrentState | None = None,
        risk_observed_at: RiskObservedAt | None = None,
        resume_window_ms: int | None = DEFAULT_RESUME_WINDOW_MS,
    ) -> None:
        if not gateway_id:
            raise ValueError("gateway_id 가 비었다")
        if resume_window_ms is not None and (isinstance(resume_window_ms, bool) or resume_window_ms < 0):
            raise ValueError(f"resume_window_ms 는 0 이상이거나 None: {resume_window_ms!r}")
        self._journal = journal
        self._gateway_id = gateway_id
        self._clock = clock
        self._config_version = config_version
        self._current = current_state
        self._risk_observed_at = risk_observed_at
        self._resume_window = resume_window_ms
        self._lock = threading.Lock()
        self._rewind_lock = threading.Lock()  # 수신 스레드가 만드는 중인 스냅샷을 기다리지 않게 따로 둔다
        self._rewinds = 0
        self._rows: dict[str, ActiveRow] = {}
        self._resolved: dict[str, tuple[int | None, int]] = {}  # event_id → (raised_at, 해소 관측 시각)
        self._latest_observation: int | None = None
        self._next = 0
        self._outbox: SnapshotOutbox | None = None
        self._broken: str | None = None
        self._done: tuple[int, int] | None = None  # (연결 세대, rewinds): 이 연결에서 완료한 스냅샷
        self.not_ready: str | None = None  # 마지막으로 만들지 못한 이유
        self.sent = 0
        self.discarded = 0  # 만들거나 보내는 사이 재요청이 와서 버렸거나 완료로 치지 않은 수
        self._seed_locked(seed, start_offset, keep_last_valid=False)

    # --- 시작 상태 ---------------------------------------------------------------------

    def _seed_locked(self, seed: Iterable[EventState], start_offset: int, *, keep_last_valid: bool) -> None:
        journal = self._journal
        if not isinstance(start_offset, int) or isinstance(start_offset, bool) or start_offset < 0:
            raise ValueError(f"start_offset 은 0 이상 정수: {start_offset!r}")
        if start_offset > journal.next_offset:
            raise ValueError(f"start_offset {start_offset} 이 저널 끝({journal.next_offset})보다 뒤다")
        first = journal.first_offset
        if start_offset < journal.next_offset and (first is None or start_offset < first):
            raise ValueError(f"start_offset {start_offset} 앞 구간이 정리돼 없다(첫 offset {first})")
        rows: dict[str, ActiveRow] = {}
        for state in seed:
            if state.origin is not Origin.LIVE:
                raise ValueError(f"LIVE 가 아닌 시작 상태: {state.event_id} ({state.origin})")
            if state.lifecycle is Lifecycle.RESOLVED:
                raise ValueError(f"해소된 시작 상태: {state.event_id}")
            if state.event_id in rows:
                raise ValueError(f"시작 상태가 겹친다: {state.event_id}")
            previous = self._rows.get(state.event_id) if keep_last_valid else None
            # 같은 프로세스에서 이미 저널로 확인한 위험 관측 시각은 사실이므로 남긴다(재시작 seed 는 null)
            last_valid = None if previous is None else previous.last_valid_observation_at
            rows[state.event_id] = ActiveRow.of_state(state, last_valid)
        self._rows = rows
        self._resolved = {}
        self._next = start_offset
        self._broken = None

    def reseed(self, seed: Iterable[EventState], start_offset: int) -> None:
        """broken 회복: 활성 상태를 다시 시작 상태로 넣는다. seed 는 start_offset 직전까지의 저널을 반영해야 한다.

        예: 판정 루프 lock 안에서 저널 쓰기 대기열이 빈 것을 확인하고 (tracker.active(), journal.next_offset). 이 전제를
        조립이 지키지 못하면 재시작 seed 로 회복한다. 삭제된 기록을 지어내지 않는다.
        """
        with self._lock:
            self._seed_locked(seed, start_offset, keep_last_valid=True)
            self._done = None
        log.warning("event_state_snapshot 다시 시작: start_offset=%d", start_offset)

    # --- 연결 -------------------------------------------------------------------------

    def bind(self, outbox: Any) -> SnapshotOutbox:
        """eventlog.Outbox 를 잇고 감싼 SnapshotOutbox 를 돌려준다(make_outbox 안에서 쓴다. I1Link.outbox 가 된다)."""
        with self._lock:
            if self._outbox is not None and self._outbox.inner is not outbox:
                raise RuntimeError("이미 다른 outbox 를 이었다")
            if self._outbox is None:
                self._outbox = SnapshotOutbox(outbox, self)
            return self._outbox

    def attach(self, client: Any, envelope: Any) -> None:
        """I1 클라이언트 pump 에 접기·스냅샷 송신을 건다. build_i1_link 뒤에 불러 attach_outbox 의 pump 뒤에 돈다."""

        def pump() -> None:
            self.follow()
            self.send_if_due(client, envelope)

        client.add_pump(pump)

    def _note_rewind(self) -> None:
        with self._rewind_lock:
            self._rewinds += 1

    def _rewind_count(self) -> int:
        with self._rewind_lock:
            return self._rewinds

    # --- 상태 -------------------------------------------------------------------------

    @property
    def applied_offset(self) -> int | None:
        """접은 마지막 journal_offset(seed 가 반영한 offset 포함). 아무것도 반영하지 않았으면 None."""
        with self._lock:
            return self._next - 1 if self._next > 0 else None

    @property
    def broken(self) -> str | None:
        """접기를 멈춘 이유(정리로 기록이 사라짐, 형식 위반). None 이면 정상. 회복은 reseed."""
        with self._lock:
            return self._broken

    def status(self) -> dict[str, Any]:
        """밖(health·로그)에서 볼 상태. broken 이면 EC2 는 스냅샷을 못 받아 동기화 모드로 남는다."""
        with self._lock:
            return {
                "broken": self._broken,
                "not_ready": self.not_ready,
                "applied_offset": self._next - 1 if self._next > 0 else None,
                "sent": self.sent,
                "discarded": self.discarded,
                "rewinds": self._rewind_count(),
            }

    # --- 접기 -------------------------------------------------------------------------

    def follow(self) -> None:
        """outbox 가 보낸 커서(cursor - 1)까지 접는다. pump 마다 부른다. 실패는 broken 으로 남긴다."""
        with self._lock:
            if self._outbox is None:
                return
            self._fold_locked(self._outbox.cursor - 1)

    def _fold_locked(self, upto: int) -> None:
        if self._broken is not None or upto < self._next:
            return
        try:
            entries = self._journal.read(self._next, upto - self._next + 1)
        except JournalError as exc:
            # 정리로 지워졌거나 읽지 못함. 지어내지 않고 멈춘다(reseed 로 회복).
            self._broken = f"{type(exc).__name__}: {exc}"
            log.error("event_state_snapshot 접기 중단(offset %d~%d): %s", self._next, upto, self._broken)
            return
        for entry in entries:
            try:
                self._apply_locked(entry)
            except (KeyError, TypeError, ValueError) as exc:
                self._broken = f"offset {entry.journal_offset}: {type(exc).__name__}: {exc}"
                log.error("event_state_snapshot 접기 중단: %s", self._broken)
                return
            self._next = entry.journal_offset + 1

    def _apply_locked(self, entry: JournalEntry) -> None:
        if entry.kind != TRANSITION_KIND:
            return
        p = entry.payload
        missing = [k for k in _REQUIRED if k not in p]
        if missing:
            raise KeyError(f"전이 payload 에 필드가 없다: {missing}")
        event_id = str(p["event_id"])
        transition = str(p["transition"])
        observed = int(p["observation_time"])
        previous = self._rows.get(event_id)
        if transition == str(Lifecycle.RESOLVED):
            if previous is not None:
                self._rows.pop(event_id)
                if self._resume_window is not None:
                    self._resolved[event_id] = (previous.raised_at, observed)
            self._prune_resolved_locked(observed)
            return
        sequence = p["event_sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise ValueError(f"event_sequence 형식 위반: {sequence!r}")
        if previous is not None and sequence <= previous.event_sequence:
            log.warning("event_id %s 의 event_sequence 가 줄었다(%d → %d). 저널 순서를 따른다",
                        event_id, previous.event_sequence, sequence)
        resolved = self._resolved.pop(event_id, None)  # 재개(같은 event_id 가 다시 활성)
        changes = {str(c) for c in (p.get("changes") or ())}
        verification = str(p["verification"])
        if transition == str(Lifecycle.RAISED) or previous is None:
            last_valid = None
        else:
            last_valid = previous.last_valid_observation_at
        if "RESTORED" in changes:
            last_valid = None  # 재시작 직후: 이번 프로세스의 유효 관측 근거가 없다
        elif verification == VERIFIED and changes & RISK_CHANGES:
            last_valid = observed
        raised_at = _int_or_none(p.get("raised_at"))
        if raised_at is None and transition == str(Lifecycle.RAISED):
            raised_at = _int_or_none(p.get("evaluated_at"))
        if raised_at is None and previous is not None:
            raised_at = previous.raised_at
        if raised_at is None and resolved is not None:
            raised_at = resolved[0]
        self._rows[event_id] = ActiveRow(
            event_id=event_id,
            event_sequence=sequence,
            lifecycle=str(Lifecycle.RAISED if transition == str(Lifecycle.RAISED) else Lifecycle.UPDATED),
            rule_id=str(p["rule_id"]),
            rule_version=int(p["rule_version"]),
            severity=str(p["severity"]),
            confidence=str(p["confidence"]),
            verification=verification,
            needs_check=bool(p["needs_check"]),
            world_ids=tuple(str(w) for w in p["world_ids"]),
            zone_id=None if p["zone_id"] is None else str(p["zone_id"]),
            raised_at=raised_at,
            last_valid_observation_at=last_valid,
            acknowledged=bool(p["acknowledged"]),
        )
        self._prune_resolved_locked(observed)

    def _prune_resolved_locked(self, observed: int) -> None:
        """재개 기간이 지난 해소 이벤트를 잊는다. lifecycle 과 같은 경계(관측 시각 차가 기간을 넘으면)다."""
        if self._latest_observation is None or observed > self._latest_observation:
            self._latest_observation = observed
        window = self._resume_window
        if window is None:
            self._resolved.clear()
            return
        latest = self._latest_observation
        for event_id in [e for e, (_, t) in self._resolved.items() if latest - t > window]:
            del self._resolved[event_id]

    # --- 스냅샷 -----------------------------------------------------------------------

    def payload(self) -> dict[str, Any] | None:
        """event_state_snapshot payload. 만들 수 없으면 None(이유는 not_ready). 예외를 내지 않는다(보내지 않는다)."""
        try:
            with self._lock:
                result = self._payload_locked()
        except Exception:  # noqa: BLE001 - 송신 스레드를 멈추지 않는다. 다음 pump 에 다시 묻는다
            log.exception("event_state_snapshot 만들기 실패")
            result, self.not_ready = None, "error"
        if result is not None:
            self.not_ready = None
        return result

    def send_if_due(self, client: Any, envelope: Any) -> bool:
        """재전송을 마친 연결에 아직 보내지 않았으면 스냅샷을 만들어 보낸다. 보냈으면 True.

        만드는 사이·보내기 직전에 재요청이 오면 버리고, 보낸 뒤에 왔으면 완료로 치지 않는다(재전송 뒤 다시 보낸다).
        """
        outbox = self._outbox
        if outbox is None or not outbox.drained or outbox.blocked is not None:
            return False
        try:
            link = client.current_link()
        except SendFailed:
            return False
        token = self._rewind_count()
        if self._done == (link.generation, token):
            return False
        payload = self.payload()
        if payload is None:
            return False
        expected = payload["journal_offset"] + 1
        if self._rewind_count() != token or outbox.cursor != expected:
            self.discarded += 1
            log.info("event_state_snapshot 버림: 만드는 사이 재요청(커서 %d, 기대 %d)", outbox.cursor, expected)
            return False
        try:
            message = envelope.make("event_state_snapshot", payload)
            client.send_on(link, message)
        except (SendFailed, Unserializable):
            return False  # 다음 pump·다음 연결에서 다시 만든다
        self.sent += 1
        if self._rewind_count() == token and outbox.cursor == expected:
            self._done = (link.generation, token)
        else:
            self.discarded += 1
            log.info("event_state_snapshot 보낸 뒤 재요청: 재전송을 마치면 다시 보낸다")
        return True

    def _payload_locked(self) -> dict[str, Any] | None:
        outbox = self._outbox
        if outbox is None:
            return self._skip("no_outbox")
        if outbox.blocked is not None:
            return self._skip("outbox_blocked")
        upto = outbox.cursor - 1
        self._fold_locked(upto)
        if self._broken is not None:
            return self._skip("broken")
        if self._next - 1 != upto:
            # 접은 위치가 보낸 커서보다 뒤(재요청으로 커서가 물러남). 다시 보낸 뒤에 만든다.
            return self._skip("cursor_behind")
        if upto < 0:
            return self._skip("empty_journal")  # 반영한 offset 이 없다(계약 journal_offset 은 0 이상)
        config_version = self._config_version()
        if not config_version:
            return self._skip("config_version_unknown")
        events = []
        for event_id in sorted(self._rows):
            row = self._enrich(self._rows[event_id])
            if row.raised_at is None:
                return self._skip(f"raised_at_unknown:{event_id}")
            events.append(row.to_payload())
        return {
            "gateway_id": self._gateway_id,
            "snapshot_at": self._clock.now_ms(),
            "journal_offset": upto,
            "config_version": config_version,
            "active_events": events,
        }

    def _enrich(self, row: ActiveRow) -> ActiveRow:
        """모르는 raised_at 은 추적기의 같은 event_id 값으로, 더 최근 위험 관측 시각은 주입값(보낸 sequence 이하)으로 채운다."""
        changes: dict[str, Any] = {}
        if row.raised_at is None and self._current is not None:
            state = self._current(row.event_id)
            if state is not None and state.event_id == row.event_id:
                changes["raised_at"] = state.raised_at  # 이벤트 동안 바뀌지 않는 값
        if self._risk_observed_at is not None:
            seen = self._risk_observed_at(row.event_id)
            # 보낸 전이보다 앞서는(아직 보내지 않은 sequence 의) 관측은 섞지 않는다
            if seen is not None and seen[0] <= row.event_sequence:
                t = seen[1]
                if row.last_valid_observation_at is None or t > row.last_valid_observation_at:
                    changes["last_valid_observation_at"] = t
        return replace(row, **changes) if changes else row

    def _skip(self, reason: str) -> None:
        if self.not_ready != reason:
            log.info("event_state_snapshot 아직 만들지 않음: %s", reason)
        self.not_ready = reason


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
