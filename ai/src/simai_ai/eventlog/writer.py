"""저널 쓰기 스레드 (명세 3.2 저널 계약: 판정 스레드와 분리한 별도 스레드, 시간 제한, 쓰기 정지 감시, D-G3-18).

판정 스레드는 LifecycleStep 을 submit_step 으로 넘기고 바로 돌아가 현장 경보(local_alert)를 보낸다. 현장 경보는
fsync 를 기다리지 않는다. EC2 로 가는 전이는 outbox 가 저널에서 읽으므로 fsync 가 끝난 기록만 나간다.

- 기록은 넘긴 순서대로 한 스레드가 한다(단일 작성자).
- write_step 은 시간 제한을 두고 기다린다(넘으면 JournalTimeout, 기록은 계속 진행된다).
- stalled(): 가장 오래 기다리는 기록이 stall_after_s 보다 오래됐으면 참. 서비스가 health·로그로 알린다.
- 디스크 기록이 실패하면(JournalWriteError) 저널이 그 뒤 기록을 모두 거부한다. failed 로 알 수 있다. 재시작이 복구한다.
"""

import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from simai_ai.eventlog.journal import EventJournal, JournalError, JournalWriteError, StepLike
from simai_ai.eventlog.record import JournalEntry

_STOP = object()


class JournalTimeout(JournalError):
    """시간 제한 안에 기록이 끝나지 않았다."""


class JournalWriter:
    def __init__(
        self,
        journal: EventJournal,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        stall_after_s: float = 1.0,
        max_queue: int = 10_000,
    ) -> None:
        if stall_after_s <= 0:
            raise ValueError("stall_after_s 는 양수")
        self._journal = journal
        self._monotonic = monotonic
        self._stall_after_s = stall_after_s
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue)
        self._waiting: deque[float] = deque()  # 아직 끝나지 않은 기록의 넣은 시각(순서대로)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.failed: BaseException | None = None

    def start(self) -> "JournalWriter":
        if self._thread is not None:
            raise JournalError("이미 시작했다")
        self._thread = threading.Thread(target=self._run, name="event-journal-writer", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout_s: float | None = None) -> None:
        """넣은 기록을 모두 쓰고 멈춘다."""
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout_s)

    def _submit(self, fn: Callable[[], Any]) -> Future[Any]:
        if self._thread is None:
            raise JournalError("시작하지 않은 쓰기 스레드")
        future: Future[Any] = Future()
        with self._lock:
            try:
                self._queue.put_nowait((fn, future))
            except queue.Full as exc:
                raise JournalError("저널 쓰기 대기열이 가득 찼다(쓰기 정지 의심)") from exc
            self._waiting.append(self._monotonic())
        return future

    def submit(self, kind: str, payload: Mapping[str, Any]) -> "Future[JournalEntry]":
        return self._submit(lambda: self._journal.append(kind, payload))

    def submit_step(self, step: StepLike, *, ack_id: str | None = None) -> "Future[tuple[JournalEntry, ...]]":
        """판정 스레드용: 바로 돌아온다. 결과(future)는 fsync 까지 끝난 기록이다. ack_id 는 확인 단계일 때.

        단계 검사(LIVE·확인 ack_id·채널과 주체·판정 근거)는 넣기 전에 이 스레드에서 해서 잘못되면 바로 오류를 낸다
        (prepare_step). 넣기가 실패하면 대기 중으로 올린 값을 되돌린다.
        """
        reservation = self._journal.prepare_step(step, ack_id=ack_id)
        try:
            return self._submit(lambda: self._journal.append_step(step, ack_id=ack_id))
        except BaseException:
            # 넣지 못했다(대기열 가득 참 등). 대기 중으로 올린 ack_id 를 되돌려야 같은 확인을 다시 받을 때 기록한다.
            self._journal.release(reservation)
            raise

    def write_step(self, step: StepLike, timeout_s: float, *, ack_id: str | None = None) -> tuple[JournalEntry, ...]:
        """확인(ACK) 처리처럼 기록을 확인하고 나서 다음으로 가야 할 때. 시간 제한을 넘으면 JournalTimeout."""
        future = self.submit_step(step, ack_id=ack_id)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout as exc:
            raise JournalTimeout(f"저널 기록이 {timeout_s}s 안에 끝나지 않았다") from exc

    def stalled(self) -> bool:
        with self._lock:
            if not self._waiting:
                return False
            return self._monotonic() - self._waiting[0] > self._stall_after_s

    def backlog(self) -> int:
        with self._lock:
            return len(self._waiting)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            fn, future = item
            result: Any = None
            error: BaseException | None = None
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001 - 삼키지 않고 future 로 넘긴다
                error = exc
                if isinstance(exc, JournalWriteError) and self.failed is None:
                    self.failed = exc
            with self._lock:
                self._waiting.popleft()
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)
