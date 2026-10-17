"""분석 워커 루프: 대여 → (처리기 실행 + 하트비트) → 결과·실패 제출.

처리기는 작업마다 별도 데몬 스레드에서 돌고, 이 루프는 하트비트 주기마다 대여를 연장한다.
- 하트비트가 REJECTED 면 처리기에 취소를 알리고 결과를 내지 않는다.
- 하트비트가 네트워크 오류로 계속 실패해 마지막 ACTIVE 로 계산한 만료 시각이 지나면 대여를 잃은 것으로 보고 버린다.
  (EC2 는 그 사이 만료·재할당하므로 같은 작업은 새 대여로 다시 온다.)
- 실행 상한(leased_at + timeout_ms)을 넘기면 취소를 알리고 TIMED_OUT 을 제출한다.
Python 스레드는 강제로 끝낼 수 없으므로 처리기는 ctx.cancelled 를 보고 스스로 멈춰야 한다.
버린 처리기가 유예 시간 안에 멈추지 않으면 다음 대여를 받지 않고 HandlerStuck 을 던진다
(GPU 작업이 겹치지 않게. 진입점은 종료하고 프로세스 관리자가 다시 띄운다, 명세 3.2 Tier 0 자원 우선).
"""

from __future__ import annotations

import enum
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from .client import GaveUp, Lease, LeaseClient, RequestRejected, RetryableError
from .handlers import JobContext, JobFailed, JobHandler

log = logging.getLogger("services.worker")
_MESSAGE_LIMIT = 500


class HandlerStuck(Exception):
    """취소를 알린 처리기 스레드가 유예 시간 안에 끝나지 않았다. 새 대여를 받지 않는다."""


class Outcome(enum.Enum):
    NO_JOB = "NO_JOB"  # long-poll 동안 작업 없음(204) 또는 멈춤 요청
    ACCEPTED = "ACCEPTED"  # 제출 반영
    DUPLICATE = "DUPLICATE"  # 다시 보낸 제출을 EC2 가 이미 반영했다(성공으로 본다)
    REJECTED = "REJECTED"  # 하트비트·제출 거부(만료·재할당·취소·시간 초과). 결과는 버렸다
    LEASE_LOST = "LEASE_LOST"  # 하트비트 실패로 대여 만료 시각이 지나 작업을 버렸다
    GAVE_UP = "GAVE_UP"  # 대여가 끝나기 전에 제출하지 못했다
    INVALID = "INVALID"  # EC2 가 제출 형식을 거부했다(400 등). 다시 보내지 않는다


class Worker:
    def __init__(
        self,
        client: LeaseClient,
        handlers: Mapping[str, JobHandler],
        *,
        clock_ms: Callable[[], int],
        monotonic: Callable[[], float],
        wait_ms: int = 25_000,
        abandon_grace_s: float = 30.0,
    ) -> None:
        if not handlers:
            raise ValueError("작업 처리기가 하나도 없다")
        self.client = client
        self.handlers = dict(handlers)
        self._clock_ms = clock_ms
        self._monotonic = monotonic
        self.wait_ms = wait_ms
        self.abandon_grace_s = abandon_grace_s
        self._abandoned: threading.Thread | None = None

    def run(self, stop: threading.Event) -> None:
        """stop 이 켜질 때까지 작업을 가져와 처리한다. AuthError·RequestRejected(대여 요청)·HandlerStuck 은 위로 던진다."""
        while not stop.is_set():
            self.run_once(should_stop=stop.is_set)

    def run_once(self, *, should_stop: Callable[[], bool] = lambda: False) -> Outcome:
        self._wait_abandoned()
        lease = self.client.lease(sorted(self.handlers), self.wait_ms, should_stop=should_stop)
        if lease is None:
            return Outcome.NO_JOB
        return self.process(lease)

    def _wait_abandoned(self) -> None:
        thread, self._abandoned = self._abandoned, None
        if thread is None:
            return
        thread.join(self.abandon_grace_s)
        if thread.is_alive():
            self._abandoned = thread
            raise HandlerStuck(f"{thread.name} 처리기가 취소 뒤 {self.abandon_grace_s}초 안에 멈추지 않았다")

    # ------------------------------------------------------------------ 작업 하나

    def process(self, lease: Lease) -> Outcome:
        received = self._monotonic()  # 남은 시간은 받은 순간부터 센다
        handler = self.handlers.get(lease.job_type)
        ctx = JobContext(lease.job_id, lease.lease_id, lease.job_type, lease.attempt, lease.input)
        started_at = self._clock_ms()
        if handler is None:  # EC2 가 요청 밖의 유형을 내준 경우
            deadline = received + lease.ttl_ms / 1000
            return self._submit_failure(lease, started_at, "NO_HANDLER", f"처리기 없음: {lease.job_type}", deadline)

        box: dict[str, Any] = {}
        done = threading.Event()
        thread = threading.Thread(
            target=self._run_handler, args=(handler, ctx, box, done), name=f"job-{lease.job_id}", daemon=True
        )
        thread.start()
        log.info("작업 시작 %s (%s, 시도 %d)", lease.job_id, lease.job_type, lease.attempt)
        try:
            return self._supervise(lease, ctx, box, done, started_at, received)
        finally:
            if not done.is_set():  # 거부·대여 상실·시간 초과·예외(AuthError 등)로 처리기를 버린다
                ctx.cancelled.set()
                self._abandoned = thread

    def _supervise(
        self, lease: Lease, ctx: JobContext, box: dict[str, Any], done: threading.Event, started_at: int, received: float
    ) -> Outcome:
        lease_deadline = received + lease.ttl_ms / 1000
        timeout_deadline = received + lease.time_left_ms / 1000
        interval = lease.heartbeat_interval_ms / 1000
        next_heartbeat = received + interval
        hb_failures = 0
        while True:
            wait = min(next_heartbeat, lease_deadline, timeout_deadline) - self._monotonic()
            if done.wait(max(0.0, wait)):
                break
            now = self._monotonic()
            if now >= timeout_deadline:
                ctx.cancelled.set()
                log.warning("작업 %s 실행 상한 %dms 초과", lease.job_id, lease.timeout_ms)
                error = {"code": "TIMEOUT", "message": f"{lease.timeout_ms}ms 초과"}
                return self._submit(lease, "TIMED_OUT", started_at, error=error, deadline=lease_deadline)
            if now >= lease_deadline:
                ctx.cancelled.set()
                log.warning("작업 %s 대여를 잃었다(하트비트 %d회 실패). 작업을 버린다", lease.job_id, hb_failures)
                return Outcome.LEASE_LOST
            if now < next_heartbeat:
                continue
            try:
                status = self.client.heartbeat(lease, ctx.progress)
            except RetryableError as e:
                hb_failures += 1
                next_heartbeat = now + min(interval, self.client.backoff.delay(hb_failures))
                log.warning("하트비트 실패 %d회: %s", hb_failures, e)
                continue
            except RequestRejected as e:
                ctx.cancelled.set()
                log.error("하트비트 거부(형식·경로): %s. 작업을 버린다", e)
                return Outcome.REJECTED
            hb_failures = 0
            if status.status != "ACTIVE":
                ctx.cancelled.set()
                log.warning("작업 %s 하트비트 거부: %s %s. 작업을 멈춘다", lease.job_id, status.status, status.reason)
                return Outcome.REJECTED
            lease_deadline = now + (status.ttl_ms or 0) / 1000
            if status.heartbeat_interval_ms:
                interval = status.heartbeat_interval_ms / 1000
            next_heartbeat = now + interval

        finished_at = self._clock_ms()
        if "result" in box:
            return self._submit(lease, "SUCCEEDED", started_at, finished_at, result=box["result"], deadline=lease_deadline)
        failure: JobFailed = box["failure"]
        error = {"code": failure.code, "message": failure.message[:_MESSAGE_LIMIT], "retryable": failure.retryable}
        return self._submit(lease, "FAILED", started_at, finished_at, error=error, deadline=lease_deadline)

    @staticmethod
    def _run_handler(handler: JobHandler, ctx: JobContext, box: dict[str, Any], done: threading.Event) -> None:
        try:
            result = handler(ctx)
            if not isinstance(result, Mapping):
                raise JobFailed("INVALID_RESULT", f"처리기 결과가 객체가 아니다: {type(result).__name__}")
            box["result"] = dict(result)
        except JobFailed as e:
            box["failure"] = e
        except Exception as e:  # 처리기 오류는 실패 보고로 바꾼다
            log.exception("처리기 오류 %s", ctx.job_id)
            box["failure"] = JobFailed("HANDLER_ERROR", f"{type(e).__name__}: {e}")
        finally:
            done.set()

    def _submit_failure(self, lease: Lease, started_at: int, code: str, message: str, deadline: float) -> Outcome:
        return self._submit(lease, "FAILED", started_at, error={"code": code, "message": message}, deadline=deadline)

    def _submit(
        self,
        lease: Lease,
        status: str,
        started_at: int,
        finished_at: int | None = None,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        deadline: float,
    ) -> Outcome:
        message = self.client.callback_message(
            lease,
            status,
            started_at=started_at,
            finished_at=self._clock_ms() if finished_at is None else finished_at,
            result=result,
            error=error,
        )
        try:
            reply = self.client.submit(lease, message, deadline=deadline)
        except GaveUp as e:
            log.error("작업 %s 결과를 버린다: %s", lease.job_id, e)
            return Outcome.GAVE_UP
        except RequestRejected as e:
            log.error("작업 %s 제출 형식 거부: %s", lease.job_id, e)
            return Outcome.INVALID
        if reply.status == "ACCEPTED":
            log.info("작업 %s %s 제출 반영", lease.job_id, status)
            return Outcome.ACCEPTED
        if reply.status == "DUPLICATE":
            log.info("작업 %s 제출은 이미 반영돼 있다", lease.job_id)
            return Outcome.DUPLICATE
        log.warning("작업 %s 제출 거부: %s %s. 결과를 버린다", lease.job_id, reply.status, reply.reason)
        return Outcome.REJECTED
