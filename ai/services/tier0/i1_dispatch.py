"""I1 수신 디스패처: EC2 → Tier 0 메시지를 처리할 모듈로 넘긴다 (명세 12.1·12.2 I1 아래로, 3.2).

받는 메시지(12.2): `journal_ack`(저널 전송 확인·빈 구간 재요청), `config_bundle`(구성 묶음 알림), `event_ack`(운영자 확인),
`event_close`(관리자의 구역 대상 이벤트 종료, S15P31A101-200), `event_escalate`(관리자가 승인한 상향, S15P31A101-272).
처리 로직은 각 모듈의 기존 함수를 부른다. 여기서는 봉투를 확인하고, 중복을 거르고, 종류별로 넘기기만 한다.

| msg_type | 넘기는 곳 | 스레드 |
|---|---|---|
| journal_ack | simai_ai.eventlog.Outbox.on_journal_ack(payload) | 수신 스레드 |
| config_bundle | simai_ai.config.ConfigSync.receive(message) (봉투째. 내려받기·검증은 ConfigSync) | 구성 작업 스레드(받은 순서대로) |
| event_ack | simai_ai.risk.alerting.ack_from_contract("event_ack", payload) → 주입한 on_ack(ContractAck) | 수신 스레드 |
| event_close | simai_ai.risk.lifecycle.CloseRequest.from_contract(payload) → 주입한 on_event_close(CloseRequest) | 수신 스레드
| event_escalate | simai_ai.risk.lifecycle.EscalationRequest.from_contract(payload) → 주입한 on_event_escalate(EscalationRequest) | 수신 스레드 |

연결 세대: on_connected 마다 세대가 하나 는다. on_disconnected 뒤에는 연결된 세대가 없다.

- 중복 제거(12.1): 처리에 성공한 msg_id 만, 그 연결 안에서만 기억한다. 처리 중 예외가 나면 기억하지 않아 같은 메시지를
  다시 받으면 다시 처리한다. 구성 작업 스레드에서 아직 처리 중인 msg_id 도 같은 연결에서 다시 오면 중복이지만, **새 연결**에서
  같은 msg_id 로 다시 오면(재연결 재알림) 다시 넘긴다 — ConfigSync 는 검증 중 묶음의 재알림으로 EC2 최신을 기록한다.
- 같은 연결에서 같은 msg_id 에 내용(payload)이 다르면 충돌로 세고 넘기지 않는다(먼저 것을 유지).
- event_ack 의 이중 기록은 계약 ack_id 로 막는다: on_event_ack 를 꽂으려면 ack_seen(예: EventJournal.has_ack_id)이
  필수다. msg_id 기억은 연결마다 비우므로 연결을 넘는 멱등은 ack_id 뿐이다.
- config_bundle 은 내려받기가 오래 걸릴 수 있어 구성 작업 스레드 하나에서 받은 순서대로 처리한다(journal_ack·event_ack 는
  계속 처리된다). 끊긴(또는 바뀐) 연결에서 받은 항목은 시작하기 전에 버린다(stale_background). 처리하는 동안 연결이
  끊기거나 바뀌었으면 끝난 뒤 지금 연결 상태를 ConfigSync 에 다시 알린다(link_sync). 옛 연결의 묶음이 ConfigSync 를
  '연결됨'으로 남기지 않게 한다. Fetcher 에는 강제 취소가 없어 close 는 제한 시간까지 기다리고, 끝나지 않은 스레드는
  alive_threads 로 알린다. 작업 스레드마다 자기 대기열을 써서 close 를 두 번 부르거나 다시 시작해도 정지 표식이 남지 않는다.
- 모르는 종류는 버리고 센다. 위로 가는 종류가 아래로 오면 역시 버린다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from simai_ai.risk.alerting import ContractAck, ack_from_contract
from simai_ai.risk.lifecycle import CloseRequest, EscalationRequest

log = logging.getLogger("services.tier0.i1.dispatch")

DOWNSTREAM_TYPES = frozenset({"journal_ack", "config_bundle", "event_ack", "event_close", "event_escalate"})

Handler = Callable[[Mapping[str, Any]], Any]
_STOP = object()


@dataclass
class DispatchStats:
    handled: dict[str, int] = field(default_factory=dict)
    duplicates: int = 0
    conflicts: int = 0
    malformed: int = 0
    unknown: int = 0
    unhandled: int = 0
    handler_errors: int = 0
    skipped_acks: int = 0
    stale_background: int = 0


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass
class _Worker:
    thread: threading.Thread
    queue: queue.Queue[Any]
    stopping: threading.Event


class I1Dispatcher:
    """EC2 → Tier 0 메시지 디스패처. handle_raw 를 I1Client 의 on_message 로, on_connected·on_disconnected 를 연결 훅으로 준다."""

    def __init__(self, *, dedup_window: int = 4096) -> None:
        if dedup_window < 1:
            raise ValueError("dedup_window 는 1 이상")
        self._handlers: dict[str, tuple[Handler, bool]] = {}
        self._seen: OrderedDict[str, str] = OrderedDict()  # 이 연결에서 처리에 성공한 msg_id → payload 요약
        self._inflight: dict[str, tuple[str, int]] = {}  # 구성 작업 스레드에 넣은 msg_id → (요약, 연결 세대)
        self._window = dedup_window
        self._lock = threading.Lock()
        self._generation = 0
        self._connected = False
        self._worker: _Worker | None = None
        self._lingering: list[_Worker] = []  # close 제한 시간 안에 끝나지 않은 작업 스레드(잊지 않는다)
        self._link_listeners: list[tuple[Callable[[], Any], Callable[[], Any]]] = []
        self.stats = DispatchStats()

    def register(self, msg_type: str, handler: Handler, *, background: bool = False) -> None:
        """msg_type 메시지(봉투 전체)를 handler 로 넘긴다. background 면 구성 작업 스레드에서 받은 순서대로 부른다."""
        if msg_type not in DOWNSTREAM_TYPES:
            raise ValueError(f"I1 아래로 오는 메시지가 아니다: {msg_type}")
        self._handlers[msg_type] = (handler, background)

    def add_link_listener(self, on_up: Callable[[], Any], on_down: Callable[[], Any]) -> None:
        """구성 작업 항목이 처리 중 연결 변화를 지나갔을 때 지금 연결 상태를 다시 알릴 곳(ConfigSync 의 연결 표시)."""
        self._link_listeners.append((on_up, on_down))

    # --- 연결 -----------------------------------------------------------------------

    def on_connected(self) -> None:
        """새 연결: 세대를 올리고 중복 기억을 비운다(재연결 때 같은 msg_id 의 재알림은 다시 처리한다)."""
        with self._lock:
            self._generation += 1
            self._connected = True
            self._seen.clear()

    def on_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._seen.clear()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    # --- 받기 -----------------------------------------------------------------------

    def handle_raw(self, raw: str | bytes) -> Any:
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self._count("malformed")
            log.warning("I1 수신: JSON 이 아니라 버렸다")
            return None
        return self.handle(message)

    def handle(self, message: Any) -> Any:
        if not isinstance(message, dict):
            self._count("malformed")
            log.warning("I1 수신: 봉투가 객체가 아니라 버렸다")
            return None
        msg_type, msg_id, payload = message.get("msg_type"), message.get("msg_id"), message.get("payload")
        if not isinstance(msg_type, str) or not isinstance(msg_id, str) or not isinstance(payload, dict):
            self._count("malformed")
            log.warning("I1 수신: 봉투 필드(msg_type·msg_id·payload)가 틀려 버렸다")
            return None
        if msg_type not in DOWNSTREAM_TYPES:
            self._count("unknown")
            log.warning("I1 수신: 받지 않는 종류 %r 를 버렸다", msg_type[:64])
            return None
        entry = self._handlers.get(msg_type)
        if entry is None:
            self._count("unhandled")
            log.warning("I1 수신: %s 처리기가 없어 버렸다", msg_type)
            return None
        digest = _digest(payload)
        handler, background = entry
        with self._lock:
            generation = self._generation
            known = self._seen.get(msg_id)
            pending = self._inflight.get(msg_id)
            if known is None and pending is not None and pending[1] == generation:
                known = pending[0]  # 같은 연결에서 아직 처리 중
            if known is not None:
                if known == digest:
                    self.stats.duplicates += 1
                    if msg_id in self._seen:
                        self._seen.move_to_end(msg_id)
                else:
                    self.stats.conflicts += 1
                    log.warning("I1 수신: 같은 msg_id 에 다른 내용의 %s 가 와서 넘기지 않았다(먼저 것을 유지)", msg_type)
                return None
            if background:
                self._inflight[msg_id] = (digest, generation)
        if background:
            self._submit((msg_type, msg_id, digest, generation, handler, message))
            return None
        return self._run(msg_type, msg_id, digest, generation, handler, message)

    def _run(
        self, msg_type: str, msg_id: str, digest: str, generation: int, handler: Handler, message: Mapping[str, Any]
    ) -> Any:
        try:
            result = handler(message)
        except Exception:  # noqa: BLE001 - 한 메시지 처리 실패가 수신을 멈추지 않는다. 기억하지 않아 재처리할 수 있다
            with self._lock:
                self._forget_inflight(msg_id, generation)
                self.stats.handler_errors += 1
            log.exception("I1 수신: %s 처리 실패", msg_type)
            return None
        with self._lock:
            self._forget_inflight(msg_id, generation)
            if generation == self._generation and self._connected:  # 지금 연결의 기억에만 넣는다
                self._seen[msg_id] = digest
                while len(self._seen) > self._window:
                    self._seen.popitem(last=False)
            self.stats.handled[msg_type] = self.stats.handled.get(msg_type, 0) + 1
        return result

    def _forget_inflight(self, msg_id: str, generation: int) -> None:
        pending = self._inflight.get(msg_id)
        if pending is not None and pending[1] == generation:  # 새 연결에서 다시 넣은 항목은 지우지 않는다
            del self._inflight[msg_id]

    def _count(self, name: str) -> None:
        with self._lock:
            setattr(self.stats, name, getattr(self.stats, name) + 1)

    # --- 구성 작업 스레드 -------------------------------------------------------------

    def _submit(self, item: tuple[Any, ...]) -> None:
        with self._lock:
            worker = self._worker
            if worker is None or not worker.thread.is_alive() or worker.stopping.is_set():
                q: queue.Queue[Any] = queue.Queue()  # 작업 스레드마다 새 대기열: 이전 정지 표식이 남지 않는다
                stopping = threading.Event()
                thread = threading.Thread(target=self._work, args=(q, stopping), name="i1-config-worker", daemon=True)
                worker = self._worker = _Worker(thread, q, stopping)
                thread.start()
            worker.queue.put(item)

    def _work(self, q: queue.Queue[Any], stopping: threading.Event) -> None:
        while True:
            item = q.get()
            if item is _STOP or stopping.is_set():
                return
            msg_type, msg_id, digest, generation, handler, message = item
            with self._lock:
                current = generation == self._generation and self._connected
                if not current:
                    self._forget_inflight(msg_id, generation)
                    self.stats.stale_background += 1
            if not current:  # 끊긴(바뀐) 연결에서 받은 항목: 시작하지 않는다(새 연결의 재알림을 기다린다)
                log.info("I1 수신: 끊긴 연결에서 받은 %s 를 처리하지 않았다", msg_type)
                continue
            self._run(msg_type, msg_id, digest, generation, handler, message)
            with self._lock:
                still = generation == self._generation and self._connected
                connected = self._connected
            if not still:  # 처리하는 동안 연결이 끊기거나 바뀌었다: 지금 연결 상태를 다시 알린다
                for on_up, on_down in self._link_listeners:
                    try:
                        (on_up if connected else on_down)()
                    except Exception:  # noqa: BLE001
                        log.exception("I1 수신: 연결 상태 다시 알리기 실패")

    def pending_background(self) -> int:
        worker = self._worker
        return worker.queue.qsize() if worker is not None else 0

    def close(self, timeout_s: float = 5.0) -> bool:
        """구성 작업 스레드를 멈춘다(처리 중인 것은 끝날 때까지 timeout_s 기다린다). 끝났으면 True.

        아직 시작하지 않은 항목은 버린다(재연결 때 EC2 가 다시 알린다). 몇 번 불러도, 다시 시작해도 정지 표식이 남지 않는다.
        """
        with self._lock:
            worker, self._worker = self._worker, None
            self._inflight.clear()
        if worker is None:
            return True
        worker.stopping.set()
        worker.queue.put(_STOP)
        worker.thread.join(timeout_s)
        if worker.thread.is_alive():
            log.warning("I1 구성 작업 스레드가 제한 시간 안에 끝나지 않았다(내려받기 중)")
            with self._lock:
                self._lingering.append(worker)
            return False
        return True

    def alive_threads(self) -> list[str]:
        with self._lock:
            workers = [self._worker, *self._lingering]
            self._lingering = [w for w in self._lingering if w.thread.is_alive()]
        return [w.thread.name for w in workers if w is not None and w.thread.is_alive()]


def wire_tier0_handlers(
    dispatcher: I1Dispatcher,
    *,
    outbox: Any | None = None,
    config_sync: Any | None = None,
    on_event_ack: Callable[[ContractAck], Any] | None = None,
    ack_seen: Callable[[str], bool] | None = None,
    on_event_close: Callable[[CloseRequest], Any] | None = None,
    on_event_escalate: Callable[[EscalationRequest], Any] | None = None,
) -> I1Dispatcher:
    """기존 모듈의 처리 함수를 디스패처에 꽂는다. 주지 않은 것은 꽂지 않는다(받으면 unhandled 로 센다).

    outbox: simai_ai.eventlog.Outbox (on_journal_ack). config_sync: simai_ai.config.ConfigSync (receive, 구성 작업 스레드).
    on_event_ack: 계약 확인(ContractAck)을 받아 LifecycleTracker.acknowledge·저널 기록·alarm_stop 을 하는 서비스 함수.
    ack_seen: 이 ack_id 를 이미 기록했는지(EventJournal.has_ack_id). on_event_ack 를 꽂으면 필수다.
    on_event_close: CloseRequest 를 받아 event_close_result payload 를 돌려주는 서비스 함수(JudgmentLoop.close, close_id 멱등은
    그 함수가 저널로 한다). 결과를 EC2 로 보내는 것은 부르는 쪽(build_i1_link)이다.
    on_event_escalate: EscalationRequest 를 받아 event_escalate_result payload 를 돌려주는 서비스 함수(JudgmentLoop.escalate,
    escalation_id 멱등은 그 함수가 저널로 한다). event_close 와 같다.
    """
    if on_event_ack is not None and ack_seen is None:
        raise ValueError("on_event_ack 에는 ack_seen(예: EventJournal.has_ack_id)이 필요하다(재연결 뒤 이중 기록 방지)")
    if outbox is not None:

        def journal_ack(message: Mapping[str, Any]) -> Any:
            result = outbox.on_journal_ack(message["payload"])
            if not result.accepted:
                log.warning("journal_ack 를 반영하지 않았다: %s", result.reason)
            return result

        dispatcher.register("journal_ack", journal_ack)
    if config_sync is not None:
        dispatcher.register("config_bundle", lambda message: config_sync.receive(message), background=True)
        dispatcher.add_link_listener(config_sync.on_connected, config_sync.on_disconnected)
    if on_event_ack is not None:
        assert ack_seen is not None

        def event_ack(message: Mapping[str, Any]) -> Any:
            ack = ack_from_contract("event_ack", message["payload"])
            if ack_seen(ack.ack_id):
                dispatcher._count("skipped_acks")  # 같은 확인(ack_id)은 한 번만 기록한다
                return None
            return on_event_ack(ack)

        dispatcher.register("event_ack", event_ack)
    if on_event_close is not None:
        dispatcher.register("event_close", lambda message: on_event_close(CloseRequest.from_contract(message["payload"])))
    if on_event_escalate is not None:
        dispatcher.register(
            "event_escalate", lambda message: on_event_escalate(EscalationRequest.from_contract(message["payload"]))
        )
    return dispatcher
