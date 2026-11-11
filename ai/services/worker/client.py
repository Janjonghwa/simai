"""I5 대여 클라이언트: 대여 요청·하트비트·결과 제출 메시지를 만들어 보내고 응답을 해석한다.

메시지 형식의 정본은 `contracts/messages/job_*.schema.json` 이다. 여기서는 봉투를 채우고,
응답을 받지 못한 경우(네트워크 오류·429·5xx)를 지수 백오프로 다시 보낸다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .transport import (
    LEASE_PATH,
    Response,
    Transport,
    TransportError,
    heartbeat_path,
    result_path,
)

SCHEMA_VERSION = "2.9.0"  # site_setup 보조 촬영 요약·다시 굽기 mode(-605)
CALLBACK_SCHEMA_VERSION = "2.17.0"  # site_setup 보조 촬영 요약·다시 굽기 mode(-605)
JOB_REQUEST_MAJOR = "3"  # job_request 3.0.0: attempt = 반납을 세지 않는 시도 번호 (-197). 2.x 는 대여 순번이라 받지 않는다
# job_request $defs.job_type 와 같은 목록(시험이 계약과 맞춰 본다). 계약 밖 유형으로 대여를 요청하면 EC2 가 400 으로 거부한다
JOB_TYPES = frozenset({
    "vlm_rationale", "tbm_report", "static_reconstruct", "calibration_ba",
    "bev_floorplan", "incident_summary", "scene_regenerate", "site_setup", "vlm_zone_draft",
    "hmr_refine", "hmr_appearance_update",
})
log = logging.getLogger("services.worker")


class RetryableError(Exception):
    """응답을 받지 못했거나 서버가 잠시 받을 수 없는 상태(429·5xx·깨진 본문). 다시 보낼 수 있다."""


class AuthError(Exception):
    """401·403. 워커 토큰이나 워커 ID 가 틀렸다. 다시 보내도 소용없다."""


class RequestRejected(Exception):
    """3xx(리다이렉트)·400·404 등 주소·형식·경로 오류. 같은 요청을 다시 보내지 않는다."""


class GaveUp(Exception):
    """대여가 끝나기 전까지 제출하지 못했다."""


@dataclass(frozen=True)
class Backoff:
    """다시 보내기 간격: initial_s, initial_s·factor, … 를 max_s 에서 자른다."""

    initial_s: float = 0.5
    factor: float = 2.0
    max_s: float = 30.0

    def __post_init__(self) -> None:
        if self.initial_s <= 0 or self.factor < 1 or self.max_s < self.initial_s:
            raise ValueError("백오프 값이 올바르지 않다")

    def delay(self, failures: int) -> float:
        """연속 실패 failures(1부터) 번째 뒤 기다릴 초."""
        delay = self.initial_s
        for _ in range(max(0, failures - 1)):
            delay = min(self.max_s, delay * self.factor)
            if delay >= self.max_s:
                break
        return delay


@dataclass(frozen=True)
class Lease:
    """job_request 한 개(작업 하나와 그 대여). 시간은 모두 EC2 시계 기준 차이로만 쓴다."""

    message: Mapping[str, Any]
    job_id: str
    lease_id: str
    job_type: str
    attempt: int
    input: Mapping[str, Any]
    heartbeat_interval_ms: int
    ttl_ms: int  # lease_expires_at − 응답 sent_at: 받은 순간부터 대여가 남은 시간
    time_left_ms: int  # leased_at + timeout_ms − 응답 sent_at: 받은 순간부터 실행 상한까지 남은 시간
    timeout_ms: int
    max_attempts: int = 1  # 최대 시도 수(job_request 3.0.0). 마지막 시도의 retryable 실패는 최종 FAILED, 반납은 시도를 쓰지 않는다
    # job_request 3.5.0 deadline_at − 응답 sent_at(-408): 받은 순간부터 이벤트 마감(vlm_rationale 은 관측 + 5 s, EC2 시계)까지
    # 남은 시간. EC2 가 싣지 않으면(이벤트에 묶이지 않은 작업) None
    deadline_left_ms: int | None = None

    @property
    def budget_ms(self) -> int:
        """받은 순간부터 이 시도에 쓸 수 있는 시간: 실행 상한과 (있으면) 이벤트 마감 중 이른 쪽."""
        return self.time_left_ms if self.deadline_left_ms is None else min(self.time_left_ms, self.deadline_left_ms)

    @classmethod
    def from_message(cls, msg: Mapping[str, Any]) -> Lease:
        version = msg.get("schema_version") if isinstance(msg, Mapping) else None
        if isinstance(version, str) and version.split(".")[0] != JOB_REQUEST_MAJOR:
            # 다른 의미의 attempt(2.x 대여 순번)와 반납을 섞지 않는다. 설정 오류라 다시 보내지 않는다(대여 요청 오류로 워커가 멈춤)
            raise RequestRejected(f"job_request {version} 는 받지 않는다: 워커는 {JOB_REQUEST_MAJOR}.x 만 받는다")
        try:
            if not isinstance(version, str):
                raise ValueError("schema_version 이 없다")
            if msg["msg_type"] != "job_request":
                raise ValueError(f"job_request 가 아니다: {msg['msg_type']}")
            p = msg["payload"]
            sent_at = int(msg["sent_at"])
            leased_at, expires_at = int(p["leased_at"]), int(p["lease_expires_at"])
            hold_ms, interval_ms = expires_at - leased_at, int(p["heartbeat_interval_ms"])
            attempt, max_attempts = int(p["attempt"]), int(p["max_attempts"])
            if hold_ms <= 0 or expires_at <= sent_at:
                raise ValueError(f"이미 만료된 대여다 (leased_at {leased_at}, lease_expires_at {expires_at}, sent_at {sent_at})")
            if not 0 < interval_ms < hold_ms:
                raise ValueError(f"하트비트 주기 {interval_ms}ms 가 대여 유지 시간 {hold_ms}ms 보다 짧아야 한다")
            if not 1 <= attempt <= max_attempts:
                raise ValueError(f"attempt {attempt} 가 max_attempts {max_attempts} 범위 밖이다")
            if int(p["timeout_ms"]) <= 0:
                raise ValueError("timeout_ms 가 0 이하다")
            deadline_left = int(p["deadline_at"]) - sent_at if p.get("deadline_at") is not None else None
            return cls(
                message=msg,
                job_id=str(p["job_id"]),
                lease_id=str(p["lease_id"]),
                job_type=str(p["job_type"]),
                attempt=attempt,
                input=p["input"],
                heartbeat_interval_ms=interval_ms,
                ttl_ms=expires_at - sent_at,
                time_left_ms=leased_at + int(p["timeout_ms"]) - sent_at,
                timeout_ms=int(p["timeout_ms"]),
                max_attempts=max_attempts,
                deadline_left_ms=deadline_left,
            )
        except (KeyError, TypeError, ValueError) as e:
            # 어긋난 대여로는 작업을 시작하지 않는다. EC2 는 만료 뒤 다시 내준다
            raise RetryableError(f"대여 응답을 쓸 수 없다: {e}") from e


@dataclass(frozen=True)
class LeaseStatus:
    """job_lease_status 한 개. ttl_ms 는 ACTIVE 일 때 받은 순간부터 대여가 남은 시간."""

    status: str
    reason: str | None = None
    ttl_ms: int | None = None
    heartbeat_interval_ms: int | None = None
    # job_lease_status 2.1.0(-597): site_setup 대여에서 같은 현장 작업이 시작됐다(정식 굽기 회수 신호). ACTIVE 에만 있다
    live_work_active: bool = False

    @classmethod
    def from_message(cls, msg: Mapping[str, Any]) -> LeaseStatus:
        try:
            if msg["msg_type"] != "job_lease_status":
                raise ValueError(f"job_lease_status 가 아니다: {msg['msg_type']}")
            p = msg["payload"]
            status = str(p["status"])
            if status not in ("ACTIVE", "ACCEPTED", "DUPLICATE", "REJECTED"):
                raise ValueError(f"모르는 상태: {status}")
            ttl = int(p["lease_expires_at"]) - int(msg["sent_at"]) if status == "ACTIVE" else None
            interval = p.get("heartbeat_interval_ms")
            live_work = status == "ACTIVE" and p.get("live_work_active") is True
            return cls(status, p.get("reason"), ttl, int(interval) if interval is not None else None, live_work)
        except (KeyError, TypeError, ValueError) as e:
            raise RetryableError(f"대여 상태 응답을 해석하지 못했다: {e}") from e


def _raise_for_status(resp: Response, what: str) -> None:
    if resp.status in (401, 403):
        raise AuthError(f"{what}: {resp.status} 인증 실패 (워커 토큰·워커 ID 확인)")
    if 300 <= resp.status < 400:  # 리다이렉트는 따라가지 않는다(토큰 누출 방지). 주소 설정을 고쳐야 한다
        raise RequestRejected(f"{what}: {resp.status} 리다이렉트는 따르지 않는다 (SIMAI_EC2_BASE_URL 확인)")
    if resp.status == 429 or resp.status >= 500:
        raise RetryableError(f"{what}: {resp.status}")
    if 400 <= resp.status < 500 and resp.status != 409:
        raise RequestRejected(f"{what}: {resp.status} {resp.body}")


class LeaseClient:
    """I5 요청 세 가지. 시계·대기·ID 생성은 주입한다(시험에서 바꿔 끼운다)."""

    def __init__(
        self,
        transport: Transport,
        worker_id: str,
        *,
        clock_ms: Callable[[], int],
        monotonic: Callable[[], float],
        sleep: Callable[[float], Any],
        backoff: Backoff | None = None,
        request_timeout_s: float = 10.0,
        new_msg_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        if not worker_id:
            raise ValueError("워커 ID 가 비었다")
        self._transport = transport
        self._worker_id = worker_id
        self._clock_ms = clock_ms
        self._monotonic = monotonic
        self._sleep = sleep
        self.backoff = backoff or Backoff()
        self._timeout_s = request_timeout_s
        self._new_msg_id = new_msg_id

    def envelope(self, msg_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": CALLBACK_SCHEMA_VERSION if msg_type == "job_callback" else SCHEMA_VERSION,
            "msg_type": msg_type,
            "msg_id": self._new_msg_id(),
            "sent_at": int(self._clock_ms()),
            "source_id": self._worker_id,
            "payload": dict(payload),
        }

    def _post(self, path: str, body: Mapping[str, Any], timeout_s: float, what: str) -> Response:
        try:
            resp = self._transport.post(path, body, timeout_s)
        except TransportError as e:
            raise RetryableError(f"{what}: {e}") from e
        _raise_for_status(resp, what)
        return resp

    # ------------------------------------------------------------------ 대여

    def lease_once(self, job_types: Sequence[str], wait_ms: int) -> Lease | None:
        """long-poll 한 번. 작업이 없으면(204) None."""
        body = self.envelope("job_lease_request", {"job_types": list(job_types), "wait_ms": int(wait_ms)})
        resp = self._post(LEASE_PATH, body, wait_ms / 1000 + self._timeout_s, "대여 요청")
        if resp.status == 204:
            return None
        if resp.status != 200 or resp.body is None:
            raise RetryableError(f"대여 요청: 예상하지 못한 응답 {resp.status}")
        return Lease.from_message(resp.body)

    def lease(self, job_types: Sequence[str], wait_ms: int, *, should_stop: Callable[[], bool] = lambda: False) -> Lease | None:
        """대여를 받을 때까지(또는 204까지) 지수 백오프로 다시 보낸다. should_stop 이 참이면 None."""
        failures = 0
        while not should_stop():
            try:
                return self.lease_once(job_types, wait_ms)
            except RetryableError as e:
                failures += 1
                delay = self.backoff.delay(failures)
                log.warning("대여 요청 실패 %d회, %.1f초 뒤 다시: %s", failures, delay, e)
                self._sleep(delay)
        return None

    # ------------------------------------------------------------------ 하트비트

    def heartbeat(
        self,
        lease: Lease,
        progress: float | None = None,
        *,
        site_setup: Mapping[str, Any] | None = None,
    ) -> LeaseStatus:
        """하트비트 한 번. 응답을 못 받으면 RetryableError (다시 보낼지는 호출 측이 대여 남은 시간으로 정한다)."""
        payload: dict[str, Any] = {
            "job_id": lease.job_id,
            "lease_id": lease.lease_id,
            "progress": progress,
        }
        if site_setup is not None:
            payload["site_setup"] = dict(site_setup)
        body = self.envelope("job_heartbeat", payload)
        try:
            resp = self._transport.post(heartbeat_path(lease.job_id), body, self._timeout_s)
        except TransportError as e:
            raise RetryableError(f"하트비트: {e}") from e
        if resp.status == 404:
            return LeaseStatus("REJECTED", "UNKNOWN_LEASE")
        if resp.status == 409 and (resp.body is None or resp.body.get("msg_type") != "job_lease_status"):
            return LeaseStatus("REJECTED", "LEASE_NOT_ACTIVE")
        _raise_for_status(resp, "하트비트")
        if resp.body is None:
            raise RetryableError(f"하트비트: 본문 없는 응답 {resp.status}")
        return LeaseStatus.from_message(resp.body)

    # ------------------------------------------------------------------ 제출

    def callback_message(
        self,
        lease: Lease,
        status: str,
        *,
        started_at: int | None,
        finished_at: int,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": lease.job_id,
            "lease_id": lease.lease_id,
            "job_type": lease.job_type,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
        }
        if status == "SUCCEEDED":
            payload["result"] = dict(result or {})
        else:
            payload["error"] = dict(error or {})
        return self.envelope("job_callback", payload)

    def submit(self, lease: Lease, message: Mapping[str, Any], *, deadline: float) -> LeaseStatus:
        """job_callback 을 보낸다. 같은 메시지(같은 msg_id·lease_id)를 deadline(monotonic 초)까지 다시 보낸다.

        응답을 잃어 다시 보낸 제출은 EC2 가 DUPLICATE 로 답한다(계약 job_callback).
        """
        failures = 0
        while True:
            try:
                resp = self._post(result_path(lease.job_id), message, self._timeout_s, "결과 제출")
                if resp.body is None:
                    raise RetryableError(f"결과 제출: 본문 없는 응답 {resp.status}")
                return LeaseStatus.from_message(resp.body)
            except RetryableError as e:
                failures += 1
                now = self._monotonic()
                if now >= deadline:
                    raise GaveUp(f"대여가 끝나기 전에 제출하지 못했다 ({failures}회 실패): {e}") from e
                delay = min(self.backoff.delay(failures), deadline - now)
                log.warning("결과 제출 실패 %d회, %.1f초 뒤 다시: %s", failures, delay, e)
                self._sleep(delay)
