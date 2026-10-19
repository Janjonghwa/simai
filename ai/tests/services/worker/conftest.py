"""분석 워커 시험 공용: 가짜 EC2 작업 큐.

services 는 설치 대상이 아니라(ai/ 작업 디렉터리에서 실행) 시험에서 ai/ 를 import 경로에 넣는다.

FakeEC2 는 `Transport` 를 구현하고, 계약(contracts/messages/job_*.schema.json 설명)에 적은 서버 규칙을
그대로 흉내 낸다: 우선순위 대여, 하트비트 연장, 만료 시 재할당(attempt + 1), 같은 대여 재제출은 DUPLICATE,
만료·재할당된 대여 제출은 REJECTED, 실행 상한 뒤 하트비트는 JOB_TIMED_OUT, 반납(consume_attempt false)은 시도 횟수 그대로 재대기. 주고받는 모든 메시지를
스키마로 검증한다. 시각은 실제 monotonic 시계(작은 값)를 쓰고, 네트워크 고장은 경로 종류별로 넣는다.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.worker.client import Backoff, LeaseClient  # noqa: E402
from services.worker.runner import Worker  # noqa: E402
from services.worker.transport import Response, TransportError  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402

T0 = 1760342400000
PRIORITY_ORDER = {"VLM_FAST": 0, "REPORT": 1, "BACKGROUND": 2}


def assert_valid(msg: dict[str, Any]) -> None:
    errors = schema_errors(msg, msg["msg_type"])
    assert not errors, f"{msg['msg_type']} 계약 위반: {errors}"


@dataclass
class Job:
    job_id: str
    job_type: str
    priority: str
    input: dict[str, Any]
    timeout_ms: int
    seq: int
    attempt: int = 0
    state: str = "QUEUED"  # QUEUED, LEASED, SUCCEEDED, FAILED, TIMED_OUT, CANCELLED
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    final_lease: str | None = None


@dataclass
class LeaseRec:
    lease_id: str
    job_id: str
    worker_id: str
    attempt: int
    leased_at: int
    expires_at: int
    state: str = "ACTIVE"  # ACTIVE, EXPIRED, SUBMITTED, CANCELLED, TIMED_OUT
    submissions: list[dict[str, Any]] = field(default_factory=list)


class FakeEC2:
    def __init__(self, *, ttl_ms=5000, heartbeat_interval_ms=20, max_attempts=2, worker_id="worker-01") -> None:
        self.ttl_ms = ttl_ms
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.max_attempts = max_attempts
        self.worker_id = worker_id
        self.token_ok = True
        self.jobs: dict[str, Job] = {}
        self.leases: dict[str, LeaseRec] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.fail_before: dict[str, int] = {}  # 종류 → 남은 횟수: 처리 전에 연결 끊김
        self.fail_after: dict[str, int] = {}  # 종류 → 남은 횟수: 처리한 뒤 응답 잃음
        self.down: set[str] = set()  # 계속 끊긴 종류
        self._seq = itertools.count()
        self._cond = threading.Condition()

    # ------------------------------------------------------------------ 시험 조작

    def now_ms(self) -> int:
        return T0 + int(time.monotonic() * 1000)

    def add_job(self, job_type: str, input: dict[str, Any] | None = None, *, priority="BACKGROUND", timeout_ms=5000) -> str:
        job_id = f"job-{len(self.jobs) + 1:04d}"
        self.jobs[job_id] = Job(job_id, job_type, priority, dict(input or {}), timeout_ms, next(self._seq))
        return job_id

    def force_expire(self, job_id: str) -> None:
        """현재 대여를 지금 만료시키고 재할당 규칙을 적용한다(대여 시간이 지난 것처럼)."""
        with self._cond:
            for rec in self.leases.values():
                if rec.job_id == job_id and rec.state == "ACTIVE":
                    rec.expires_at = self.now_ms()
            self._expire_due()

    def cancel(self, job_id: str) -> None:
        with self._cond:
            self.jobs[job_id].state = "CANCELLED"
            for rec in self.leases.values():
                if rec.job_id == job_id and rec.state == "ACTIVE":
                    rec.state = "CANCELLED"

    def bodies(self, kind: str) -> list[dict[str, Any]]:
        with self._cond:
            return [body for path, body in self.requests if _kind(path) == kind]

    def wait_for(self, kind: str, n: int, timeout_s: float = 5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: sum(1 for p, _ in self.requests if _kind(p) == kind) >= n, timeout_s)

    def leases_of(self, job_id: str) -> list[LeaseRec]:
        return sorted((r for r in self.leases.values() if r.job_id == job_id), key=lambda r: r.attempt)

    # ------------------------------------------------------------------ Transport

    def post(self, path: str, body: dict[str, Any], timeout_s: float) -> Response:
        kind = _kind(path)
        if kind in self.down or self._take(self.fail_before, kind):
            raise TransportError("연결 끊김(가짜)")
        if not self.token_ok:
            return Response(401, None)
        assert_valid(body)
        assert body["source_id"] == self.worker_id
        with self._cond:
            self.requests.append((path, body))
            self._cond.notify_all()
            self._expire_due()
            if kind == "lease":
                resp = self._lease(body)
            elif kind == "heartbeat":
                resp = self._heartbeat(path, body)
            else:
                resp = self._result(path, body)
        if resp.body is not None:
            assert_valid(resp.body)
        if self._take(self.fail_after, kind):
            raise TransportError("응답 잃음(가짜)")
        return resp

    # ------------------------------------------------------------------ 서버 규칙

    @staticmethod
    def _take(faults: dict[str, int], kind: str) -> bool:
        if faults.get(kind, 0) > 0:
            faults[kind] -= 1
            return True
        return False

    def _envelope(self, msg_type: str, payload: dict[str, Any], sent_at: int | None = None) -> dict[str, Any]:
        return {
            "schema_version": "3.0.0" if msg_type == "job_request" else "2.0.0",  # job_request 3.0.0: 시도 번호(-197)
            "msg_type": msg_type,
            "msg_id": str(uuid.uuid4()),
            "sent_at": self.now_ms() if sent_at is None else sent_at,
            "source_id": "ec2",
            "payload": payload,
        }

    def _status(self, ids: tuple[str, str], status: str, http: int = 200, sent_at: int | None = None, **extra) -> Response:
        payload = {"job_id": ids[0], "lease_id": ids[1], "status": status, **extra}
        return Response(http, self._envelope("job_lease_status", payload, sent_at))

    def _expire_due(self) -> None:
        now = self.now_ms()
        for rec in self.leases.values():
            if rec.state == "ACTIVE" and now >= rec.expires_at:
                rec.state = "EXPIRED"
                job = self.jobs[rec.job_id]
                if job.state == "LEASED":
                    job.state = "QUEUED" if job.attempt < self.max_attempts else "TIMED_OUT"

    def _lease(self, body: dict[str, Any]) -> Response:
        types = set(body["payload"]["job_types"])
        queued = [j for j in self.jobs.values() if j.state == "QUEUED" and j.job_type in types]
        if not queued:
            return Response(204, None)  # 실제 EC2 는 wait_ms 동안 붙잡는다
        job = min(queued, key=lambda j: (PRIORITY_ORDER[j.priority], j.seq))
        job.attempt += 1
        job.state = "LEASED"
        now = self.now_ms()
        rec = LeaseRec(str(uuid.uuid4()), job.job_id, body["source_id"], job.attempt, now, now + self.ttl_ms)
        self.leases[rec.lease_id] = rec
        payload = {
            "job_id": job.job_id,
            "lease_id": rec.lease_id,
            "attempt": job.attempt,
            "max_attempts": self.max_attempts,
            "job_type": job.job_type,
            "priority": job.priority,
            "timeout_ms": job.timeout_ms,
            "leased_at": now,
            "lease_expires_at": rec.expires_at,
            "heartbeat_interval_ms": self.heartbeat_interval_ms,
            "input": job.input,
        }
        return Response(200, self._envelope("job_request", payload, sent_at=now))

    def _find(self, path: str, body: dict[str, Any]) -> LeaseRec | None:
        p = body["payload"]
        assert f"/{p['job_id']}/" in path, "경로의 job_id 와 본문이 다르다"
        rec = self.leases.get(p["lease_id"])
        if rec is None or rec.job_id != p["job_id"] or rec.worker_id != body["source_id"]:
            return None
        return rec

    def _heartbeat(self, path: str, body: dict[str, Any]) -> Response:
        p = body["payload"]
        ids = (p["job_id"], p["lease_id"])
        rec = self._find(path, body)
        if rec is None:
            return self._status(ids, "REJECTED", 409, reason="UNKNOWN_LEASE")
        job = self.jobs[rec.job_id]
        if rec.state == "CANCELLED":
            return self._status(ids, "REJECTED", 409, reason="JOB_CANCELLED")
        if rec.state != "ACTIVE":
            return self._status(ids, "REJECTED", 409, reason="LEASE_EXPIRED")
        now = self.now_ms()
        if now >= rec.leased_at + job.timeout_ms:
            rec.state = "TIMED_OUT"
            job.state = "TIMED_OUT"
            return self._status(ids, "REJECTED", 409, reason="JOB_TIMED_OUT")
        rec.expires_at = now + self.ttl_ms
        return self._status(ids, "ACTIVE", sent_at=now, lease_expires_at=rec.expires_at)

    def _result(self, path: str, body: dict[str, Any]) -> Response:
        p = body["payload"]
        ids = (p["job_id"], p["lease_id"])
        rec = self._find(path, body)
        if rec is None:
            return self._status(ids, "REJECTED", 409, reason="UNKNOWN_LEASE")
        if rec.submissions:  # 만료 검사보다 먼저: 같은 대여 재제출은 DUPLICATE
            rec.submissions.append(body)
            return self._status(ids, "DUPLICATE")
        if rec.state != "ACTIVE":
            return self._status(ids, "REJECTED", 409, reason="LEASE_EXPIRED")
        rec.submissions.append(body)
        rec.state = "SUBMITTED"
        job = self.jobs[rec.job_id]
        job.final_lease = rec.lease_id
        status = p["status"]
        if status == "SUCCEEDED":
            job.state, job.result, job.error = "SUCCEEDED", p["result"], None
        elif status == "FAILED" and p["error"].get("consume_attempt") is False:
            job.error = p["error"]  # 반납(2.1.0): 시도 횟수를 쓰지 않고 다시 대기열로(마지막 시도여도)
            job.attempt -= 1
            job.state = "QUEUED"
        else:
            job.error = p["error"]
            retry = status == "FAILED" and p["error"].get("retryable", False) and job.attempt < self.max_attempts
            job.state = "QUEUED" if retry else status
        return self._status(ids, "ACCEPTED")


def _kind(path: str) -> str:
    if path.endswith("/lease"):
        return "lease"
    if path.endswith("/heartbeat"):
        return "heartbeat"
    if path.endswith("/result"):
        return "result"
    raise AssertionError(f"모르는 경로: {path}")


class RecordingSleep:
    """백오프 대기를 기록만 하고 실제로 기다리지 않는다."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(round(seconds, 6))


@pytest.fixture
def ec2() -> FakeEC2:
    return FakeEC2()


@pytest.fixture
def sleep() -> RecordingSleep:
    return RecordingSleep()


def make_client(server, sleep, *, backoff: Backoff | None = None) -> LeaseClient:
    return LeaseClient(
        server,
        "worker-01",
        clock_ms=lambda: T0 + int(time.monotonic() * 1000),
        monotonic=time.monotonic,
        sleep=sleep,
        backoff=backoff,
    )


@pytest.fixture
def client(ec2, sleep) -> LeaseClient:
    return make_client(ec2, sleep)


@pytest.fixture
def make_worker(client):
    def _make(handlers, wait_ms: int = 0) -> Worker:
        return Worker(client, handlers, clock_ms=lambda: T0 + int(time.monotonic() * 1000), monotonic=time.monotonic, wait_ms=wait_ms)

    return _make
