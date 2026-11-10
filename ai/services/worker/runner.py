"""분석 워커 루프: 대여 → (처리기 실행 + 하트비트) → 결과·실패 제출.

처리기는 작업마다 별도 데몬 스레드에서 돌고, 이 루프는 하트비트 주기마다 대여를 연장한다.
- 하트비트가 REJECTED 면 처리기에 취소를 알리고 결과를 내지 않는다.
- 하트비트가 네트워크 오류로 계속 실패해 마지막 ACTIVE 로 계산한 만료 시각이 지나면 대여를 잃은 것으로 보고 버린다.
  (EC2 는 그 사이 만료·재할당하므로 같은 작업은 새 대여로 다시 온다.)
- 실행 상한(leased_at + timeout_ms)을 넘기면 취소를 알리고 TIMED_OUT 을 제출한다. vlm_rationale 은 이벤트 마감(관측 + 5 s,
  EC2 deadline_at 과 같은 기준)이 더 이르면 그때까지만 쓴다(S15P31A101-408).
Python 스레드는 강제로 끝낼 수 없으므로 처리기는 ctx.cancelled 를 보고 스스로 멈춰야 한다.
버린 처리기가 유예 시간 안에 멈추지 않으면 다음 대여를 받지 않고 HandlerStuck 을 던진다
(GPU 작업이 겹치지 않게. 진입점은 종료하고 프로세스 관리자가 다시 띄운다, 명세 3.2 Tier 0 자원 우선).

Tier 0 우선(명세 3.2, admission 이 있을 때, simai_ai.gpu.admission):
- 대여 요청마다(네트워크 오류 뒤 다시 보낼 때도) 접수 문을 본다. 받지 않으면(지연 초과·모름·기동 중) 대여를 요청하지 않고
  pause_s 쉰다(PAUSED). EC2 는 작업을 대기열에 그대로 둔다. LIVE 중에는 LIVE 에 막힌 유형(EXP-01·02)을 대여 요청 목록에서 빼고,
  long-poll 을 live_wait_ms 로 줄인다.
- long-poll 사이에 상황이 바뀔 수 있어 받은 뒤 다시 본다. LIVE 에 막힌 유형이면 시작하지 않고 바로, 지연 때문이면 hold_max_s(실행
  상한의 절반 이하) 동안 하트비트로 대여를 지키며 기다리다가 그래도 받을 수 없으면 반납한다(DEFERRED).
- LIVE 에 막힌 유형이 도는 중이면 별도 감시 스레드가 live_poll_s 마다 접수 문을 본다(하트비트 HTTP 대기와 무관). LIVE 가 시작되면
  바로 취소를 알리고 반납한다. 지연 초과는 새 작업만 멈추고 도는 작업은 끝까지 둔다(명세 3.2 '새 작업을 멈추고').
- 반납(계약 job_callback 2.1.0): retryable FAILED + error.consume_attempt false. EC2 는 시도 횟수를 쓰지 않고(attempt 그대로) 다시
  내주므로 마지막 시도도 Tier 0 우선 때문에 최종 FAILED 가 되지 않는다. 처리기 실패·시간 초과는 반납이 아니다(시도를 쓴다).
"""

from __future__ import annotations

import enum
import logging
import math
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from simai_ai.gpu.admission import Admission, AdmissionGate

from .client import (
    JOB_TYPES,
    GaveUp,
    Lease,
    LeaseClient,
    RequestRejected,
    RetryableError,
)

PAUSE_REPORT_S = 300.0  # 접수 중지가 이어지면 이 주기로 경고를 다시 남긴다(운영 감시)
from .handlers import JobContext, JobFailed, JobHandler

log = logging.getLogger("services.worker")
_MESSAGE_LIMIT = 500


def _safe_failure_text(value: object) -> str:
    """기존 예외 요약 규칙으로 인증값·URL·줄바꿈을 제거한다."""
    text = str(value)
    for name, secret in os.environ.items():
        if secret and any(key in name.upper() for key in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            text = text.replace(secret, "<REDACTED>")
    text = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+", "<URL>", text)
    text = re.sub(r"(?i)bearer\s+[^\s,;\"']+", "Bearer <REDACTED>", text)
    text = re.sub(r"(?i)((?:[\w-]*(?:token|secret|password|signature|api[_-]?key)|authorization)[\"']?\s*[:=]\s*)"
                  r"(?:[\"'][^\"']*[\"']|[^\s,;]+)", r"\1<REDACTED>", text)
    return " ".join(text.split())[:400]


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
    PAUSED = "PAUSED"  # Tier 0 우선(지연 초과·모름·LIVE)으로 대여 요청을 쉬었다
    DEFERRED = "DEFERRED"  # Tier 0 우선으로 받은 작업을 시작하지 않거나 멈추고 반납했다(EC2 가 반영, 시도 횟수 그대로)


@dataclass
class _Beat:
    """대여 하나의 하트비트 상태(monotonic 초). 대기(hold)와 실행 감시가 같이 쓴다."""

    lease_deadline: float
    timeout_deadline: float
    interval: float
    next_heartbeat: float
    failures: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    terminal_outcome: Outcome | None = None


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
        admission: AdmissionGate | None = None,
        live_wait_ms: int = 5_000,
        hold_max_s: float = 60.0,
        pause_s: float = 2.0,
        live_poll_s: float = 2.0,
        sleep: Callable[[float], Any] | None = None,
        type_gate: Callable[[list[str]], list[str]] | None = None,
    ) -> None:
        if not handlers:
            raise ValueError("작업 처리기가 하나도 없다")
        if unknown := sorted(set(handlers) - JOB_TYPES):
            raise ValueError(f"계약(job_request job_type)에 없는 작업 유형: {unknown}")
        if pause_s <= 0 or live_poll_s <= 0 or hold_max_s < 0:
            raise ValueError("pause_s·live_poll_s 는 양수, hold_max_s 는 0 이상이다")
        self.client = client
        self.handlers = dict(handlers)
        self._clock_ms = clock_ms
        self._monotonic = monotonic
        self.wait_ms = wait_ms
        self.abandon_grace_s = abandon_grace_s
        self._abandoned: threading.Thread | None = None
        self.admission = admission
        self.live_wait_ms = min(live_wait_ms, wait_ms)
        self.hold_max_s = hold_max_s
        self.pause_s = pause_s
        self.live_poll_s = live_poll_s
        self._sleep = sleep if sleep is not None else threading.Event().wait
        self.type_gate = type_gate  # 대여 요청 직전 작업 유형을 줄인다(vLLM 준비 전 VLM 유형 보류, S15P31A101-577 vlm_gate)
        self._last_filtered: tuple[str, ...] | None = None
        self._last_admit: bool | None = None
        self._closed_since: float | None = None
        self._closed_reported: float | None = None

    def run(self, stop: threading.Event) -> None:
        """stop 이 켜질 때까지 작업을 가져와 처리한다. AuthError·RequestRejected(대여 요청)·HandlerStuck 은 위로 던진다."""
        while not stop.is_set():
            self.run_once(should_stop=stop.is_set)

    def run_once(self, *, should_stop: Callable[[], bool] = lambda: False) -> Outcome:
        self._wait_abandoned()
        if self.admission is None:
            types = self._gated(sorted(self.handlers))
            if not types:
                self._sleep(self.pause_s)
                return Outcome.PAUSED
            lease = self.client.lease(types, self.wait_ms, should_stop=should_stop)
        else:
            lease = self._lease_gated(should_stop)
            if lease is Outcome.PAUSED:
                return Outcome.PAUSED
        if lease is None:
            return Outcome.NO_JOB
        return self.process(lease, should_stop=should_stop)

    def _lease_gated(self, should_stop: Callable[[], bool]) -> Lease | Outcome | None:
        """대여 요청 한 번마다 접수 문을 다시 본다. 네트워크 오류 뒤 다시 보내기 전에도 본다(닫혔으면 다시 보내지 않는다)."""
        failures = 0
        while not should_stop():
            adm = self._admission()
            types = self._gated(adm.allowed_types(sorted(self.handlers)))
            filtered = tuple(sorted(set(self.handlers) & adm.blocked_types)) if adm.live else ()
            if filtered != self._last_filtered:
                self._last_filtered = filtered
                if filtered:
                    log.warning("LIVE 관제 중 작업 유형 접수 제외: %s. 관제 종료 후 simai-bootstrap after-live 실행", ", ".join(filtered))
            if not types:
                self._sleep(self.pause_s)
                return Outcome.PAUSED
            try:
                return self.client.lease_once(types, self.live_wait_ms if adm.live else self.wait_ms)
            except RetryableError as e:
                failures += 1
                delay = self.client.backoff.delay(failures)
                log.warning("대여 요청 실패 %d회, %.1f초 뒤 접수 문부터 다시: %s", failures, delay, e)
                self._sleep(delay)
        return None

    def _gated(self, types: list[str]) -> list[str]:
        return types if self.type_gate is None else self.type_gate(types)

    def _admission(self) -> Admission:
        assert self.admission is not None
        try:
            adm = self.admission.check()
        except Exception as e:  # noqa: BLE001 - 접수 문이 깨지면 모름: 받지 않는다
            adm = Admission(True, False, f"접수 문 확인 실패({type(e).__name__}: {e}) → 모름",
                            self.admission.policy.live_blocked_types)
        now = self._monotonic()
        if adm.admit != self._last_admit:  # 받음/받지 않음이 바뀔 때만 남긴다(이유의 숫자는 매번 바뀐다)
            self._last_admit = adm.admit
            (log.info if adm.admit else log.warning)("Tier 0 우선 접수 문: %s (%s)", "받음" if adm.admit else "받지 않음",
                                                      adm.reason)
            self._closed_since = None if adm.admit else now
            self._closed_reported = now
        elif not adm.admit and self._closed_reported is not None and now - self._closed_reported >= PAUSE_REPORT_S:
            self._closed_reported = now
            log.warning("Tier 0 우선 접수 중지 %.0f분째: %s", (now - (self._closed_since or now)) / 60, adm.reason)
        return adm

    @property
    def paused_for_s(self) -> float | None:
        """접수 중지가 이어진 시간(초). 받는 중이면 None (운영 감시)."""
        return None if self._closed_since is None else self._monotonic() - self._closed_since

    def _wait_abandoned(self) -> None:
        thread, self._abandoned = self._abandoned, None
        if thread is None:
            return
        thread.join(self.abandon_grace_s)
        if thread.is_alive():
            self._abandoned = thread
            raise HandlerStuck(f"{thread.name} 처리기가 취소 뒤 {self.abandon_grace_s}초 안에 멈추지 않았다")

    # ------------------------------------------------------------------ 작업 하나

    def process(self, lease: Lease, *, should_stop: Callable[[], bool] = lambda: False) -> Outcome:
        received = self._monotonic()  # 남은 시간은 받은 순간부터 센다
        handler = self.handlers.get(lease.job_type)
        # vlm_rationale 은 실행 상한(대여 + 5 s)보다 이벤트 마감(관측 + 5 s)이 먼저면 그쪽까지만 쓴다(-408)
        timeout_deadline = received + lease.budget_ms / 1000
        ctx = JobContext(
            lease.job_id,
            lease.lease_id,
            lease.job_type,
            lease.attempt,
            lease.input,
            timeout_ms=lease.timeout_ms,
            deadline=timeout_deadline,
            monotonic=self._monotonic,
        )
        if handler is None:  # EC2 가 요청 밖의 유형을 내준 경우
            deadline = received + lease.ttl_ms / 1000
            return self._submit_failure(lease, self._clock_ms(), "NO_HANDLER", f"처리기 없음: {lease.job_type}", deadline)
        interval = lease.heartbeat_interval_ms / 1000
        beat = _Beat(received + lease.ttl_ms / 1000, timeout_deadline, interval, received + interval)
        ctx._on_site_setup = lambda snap: self._send_site_setup_heartbeat(lease, ctx, beat, snap)
        if self.admission is not None:
            held = self._hold(lease, ctx, beat, received, should_stop)
            if held is not None:
                return held
        started_at = self._clock_ms()

        box: dict[str, Any] = {}
        done = threading.Event()
        thread = threading.Thread(
            target=self._run_handler, args=(handler, ctx, box, done), name=f"job-{lease.job_id}", daemon=True
        )
        thread.start()
        log.info("작업 시작 %s (%s, 시도 %d)", lease.job_id, lease.job_type, lease.attempt)
        try:
            return self._supervise(lease, ctx, box, done, started_at, beat)
        finally:
            if not done.is_set():  # 거부·대여 상실·시간 초과·예외(AuthError 등)로 처리기를 버린다
                ctx.cancelled.set()
                self._abandoned = thread

    def _send_site_setup_heartbeat(
        self,
        lease: Lease,
        ctx: JobContext,
        beat: _Beat,
        snapshot: Mapping[str, Any],
    ) -> None:
        """site_setup 단계 전환마다 즉시 하트비트를 보내 EC2 에 중간 진행 스냅샷을 반영한다."""
        with beat.lock:
            if beat.terminal_outcome is not None or ctx.cancelled.is_set():
                return
            now = self._monotonic()
            try:
                status = self.client.heartbeat(
                    lease,
                    ctx.progress,
                    site_setup=snapshot,
                )
            except RetryableError as e:
                beat.failures += 1
                beat.next_heartbeat = now + min(beat.interval, self.client.backoff.delay(beat.failures))
                log.warning("site_setup 단계 하트비트 실패 %d회: %s", beat.failures, e)
                return
            except RequestRejected as e:
                log.error("site_setup 단계 하트비트 거부(형식·경로): %s. 작업을 버린다", e)
                beat.terminal_outcome = Outcome.REJECTED
                ctx.cancelled.set()
                return
            beat.failures = 0
            if status.status != "ACTIVE":
                log.warning("작업 %s 하트비트 거부: %s %s. 작업을 멈춘다", lease.job_id, status.status, status.reason)
                beat.terminal_outcome = Outcome.REJECTED
                ctx.cancelled.set()
                return
            beat.lease_deadline = now + (status.ttl_ms or 0) / 1000
            if status.heartbeat_interval_ms:
                beat.interval = status.heartbeat_interval_ms / 1000
            beat.next_heartbeat = now + beat.interval

    def _hold(self, lease: Lease, ctx: JobContext, beat: _Beat, received: float,
              should_stop: Callable[[], bool]) -> Outcome | None:
        """받은 작업을 시작해도 되는지 다시 본다. 시작해도 되면 None. 안 되면 대여를 지키며 기다리거나 반납한다."""
        hold_deadline = received + min(self.hold_max_s, 0.5 * lease.budget_ms / 1000)
        while True:
            adm = self._admission()
            if adm.blocks(lease.job_type):
                return self._defer(lease, beat, "LIVE_EXP_BLOCKED",
                                   f"LIVE 관제 중이라 {lease.job_type} 를 돌리지 않는다: {adm.reason}")
            if adm.admits(lease.job_type):
                return None
            now = self._monotonic()
            if now >= hold_deadline or should_stop():
                return self._defer(lease, beat, "TIER0_PRIORITY", f"Tier 0 우선으로 시작하지 않았다: {adm.reason}")
            wake = min(now + self.live_poll_s, beat.next_heartbeat, beat.lease_deadline, hold_deadline)
            self._sleep(max(0.0, wake - now))
            out = self._heartbeat_due(lease, ctx, beat)
            if out is not None:
                return out

    def _watch_live(self, lease: Lease, ctx: JobContext, preempted: dict[str, str], stop: threading.Event) -> None:
        """LIVE 에 막힌 유형이 도는 동안 live_poll_s 마다 접수 문을 본다. LIVE 면 바로 취소를 알린다(GPU 를 먼저 놓게)."""
        while not stop.wait(self.live_poll_s):
            adm = self._admission()
            if adm.blocks(lease.job_type):
                preempted["reason"] = adm.reason
                ctx.cancelled.set()
                log.warning("작업 %s: LIVE 관제 시작, 취소를 알렸다 (%s)", lease.job_id, adm.reason)
                return

    def _defer(self, lease: Lease, beat: _Beat, code: str, message: str, started_at: int | None = None) -> Outcome:
        """반납: 시도 횟수를 쓰지 않는 retryable FAILED (계약 job_callback 2.1.0, code 는 계약의 닫힌 목록)."""
        log.warning("작업 %s 반납(%s): %s", lease.job_id, code, message)
        error = {"code": code, "message": message[:_MESSAGE_LIMIT], "retryable": True, "consume_attempt": False}
        out = self._submit(lease, "FAILED", started_at, error=error, deadline=beat.lease_deadline)
        return Outcome.DEFERRED if out in (Outcome.ACCEPTED, Outcome.DUPLICATE) else out

    def _heartbeat_due(self, lease: Lease, ctx: JobContext, beat: _Beat) -> Outcome | None:
        """대여를 잃었거나 거부되면 그 결과, 아니면(하트비트 때가 아님·보냄·일시 실패) None."""
        with beat.lock:
            if beat.terminal_outcome is not None:
                return beat.terminal_outcome
            now = self._monotonic()
            if now >= beat.lease_deadline:
                log.warning("작업 %s 대여를 잃었다(하트비트 %d회 실패). 작업을 버린다", lease.job_id, beat.failures)
                return Outcome.LEASE_LOST
            if now < beat.next_heartbeat:
                return None
            try:
                status = self.client.heartbeat(
                    lease,
                    ctx.progress,
                    site_setup=ctx.site_setup,
                )
            except RetryableError as e:
                beat.failures += 1
                beat.next_heartbeat = now + min(beat.interval, self.client.backoff.delay(beat.failures))
                log.warning("하트비트 실패 %d회: %s", beat.failures, e)
                return None
            except RequestRejected as e:
                log.error("하트비트 거부(형식·경로): %s. 작업을 버린다", e)
                beat.terminal_outcome = Outcome.REJECTED
                return Outcome.REJECTED
            beat.failures = 0
            if status.status != "ACTIVE":
                log.warning("작업 %s 하트비트 거부: %s %s. 작업을 멈춘다", lease.job_id, status.status, status.reason)
                beat.terminal_outcome = Outcome.REJECTED
                return Outcome.REJECTED
            beat.lease_deadline = now + (status.ttl_ms or 0) / 1000
            if status.heartbeat_interval_ms:
                beat.interval = status.heartbeat_interval_ms / 1000
            beat.next_heartbeat = now + beat.interval
            return None

    def _supervise(
        self, lease: Lease, ctx: JobContext, box: dict[str, Any], done: threading.Event, started_at: int, beat: _Beat
    ) -> Outcome:
        watch_live = self.admission is not None and lease.job_type in self.admission.policy.live_blocked_types
        preempted: dict[str, str] = {}
        watch_stop = threading.Event()
        watcher = None
        if watch_live:
            # 하트비트 HTTP 대기(최대 요청 시간 제한)와 따로 돈다: 느린 EC2 가 LIVE 선점을 늦추지 않게
            watcher = threading.Thread(target=self._watch_live, args=(lease, ctx, preempted, watch_stop),
                                       name=f"job-{lease.job_id}-live", daemon=True)
            watcher.start()
        try:
            while True:
                if beat.terminal_outcome is not None:
                    ctx.cancelled.set()
                    return beat.terminal_outcome
                limit = self._monotonic() + self.live_poll_s if watch_live else math.inf
                wait = min(beat.next_heartbeat, beat.lease_deadline, beat.timeout_deadline, limit) - self._monotonic()
                if done.wait(max(0.0, wait)) and not preempted:
                    break
                if beat.terminal_outcome is not None:
                    ctx.cancelled.set()
                    return beat.terminal_outcome
                if preempted:
                    return self._defer(lease, beat, "LIVE_PREEMPTED", f"LIVE 관제 시작으로 멈췄다: {preempted['reason']}",
                                       started_at)
                now = self._monotonic()
                if now >= beat.timeout_deadline:
                    ctx.cancelled.set()
                    event_bound = lease.budget_ms < lease.time_left_ms
                    what = "이벤트 관측 + 5000ms 마감" if event_bound else f"실행 상한 {lease.timeout_ms}ms"
                    log.warning("작업 %s %s 초과", lease.job_id, what)
                    error = {"code": "TIMEOUT", "message": f"{what} 초과"}
                    return self._submit(lease, "TIMED_OUT", started_at, error=error, deadline=beat.lease_deadline)
                out = self._heartbeat_due(lease, ctx, beat)
                if out is not None:
                    ctx.cancelled.set()
                    return out
        finally:
            watch_stop.set()

        if beat.terminal_outcome is not None:
            return beat.terminal_outcome
        finished_at = self._clock_ms()
        if "result" in box:
            return self._submit(lease, "SUCCEEDED", started_at, finished_at, result=box["result"],
                                deadline=beat.lease_deadline)
        failure: JobFailed = box["failure"]
        error = {"code": failure.code, "message": failure.message[:_MESSAGE_LIMIT], "retryable": failure.retryable}
        return self._submit(lease, "FAILED", started_at, finished_at, error=error, deadline=beat.lease_deadline)

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
        started_at: int | None,
        finished_at: int | None = None,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        deadline: float,
    ) -> Outcome:
        if status == "FAILED" and error is not None:
            log.warning("작업 %s 실패 code=%s message=%s", lease.job_id,
                        _safe_failure_text(error.get("code", "")), _safe_failure_text(error.get("message", "")))
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
