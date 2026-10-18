"""노트북 → EC2 하트비트 (HTTPS POST /api/devices/heartbeat, 기기 토큰, 1초) (명세 3.2, 6.5, 12.6, FR-134, S15P31A101-91).

EC2 는 Tier 0 health 가 끊겼을 때 이 하트비트의 tier0.control 로 'Tier 0 정지'와 '원격 전달 단절(현장 경보 동작 중)'을
구분한다. 내용은 장애 상태 기계(link_status)가 만든다. 이 모듈은 보내기만 한다.

- HTTPS 만 쓴다(평문 http 불가, 루프백 시험 서버도 https). 인증서 검증을 끄지 않는다. 리다이렉트를 따라가지 않는다
  (따라가면 기기 토큰이 다른 호스트로 실린다).
- 기기 토큰은 Authorization 헤더에만 싣는다. 주소·로그·예외 문장·repr 에 넣지 않는다. 주소에 계정·query·fragment·토큰이
  있으면 만들지 않는다.
- 대기열이 없다: 보낼 때마다 그 순간 상태로 새로 만들고, 보내는 중이면 다음 것은 보내는 것이 끝난 뒤에 만든다(한 번에 하나).
  EC2 가 느리거나 막혀도 쌓이지 않고, 송신은 따로 도는 스레드라 현장 경보(I0 수신·소리·화면)를 막지 않는다.
- 2xx 응답만 성공이다. 결과는 on_result(ok, 단조 시각, 벽시계 ms)로 알리고, 끊김 판단(3회 누락)은 link_status 가 한다.
"""

from __future__ import annotations

import http.client
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from simai_site_alert.endpoint import _NoRedirect

log = logging.getLogger(__name__)

HEARTBEAT_PATH = "/api/devices/heartbeat"
DEFAULT_INTERVAL_S = 1.0
DEFAULT_TIMEOUT_S = 2.5


class Ec2HeartbeatError(Exception):
    """하트비트를 보내지 못했다. 문장에 토큰·주소 값이 없다(예외 종류만)."""


def _secret_forms(secret: str) -> set[str]:
    return {s for s in (secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)) if s}


def validate_base_url(base_url: str, token: str) -> str:
    """https·호스트 필수, 계정·query·fragment 불가, 토큰이 주소 어디에도 없어야 한다. 하트비트 주소를 돌려준다."""
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("SIMAI_EC2_BASE_URL 은 https 주소여야 한다 (하트비트는 HTTPS 만)")
    if parts.username or parts.password:
        raise ValueError("SIMAI_EC2_BASE_URL 에 계정을 넣지 않는다 (기기 토큰은 SIMAI_DEVICE_TOKEN)")
    if parts.query or parts.fragment or "?" in base_url or "#" in base_url:
        raise ValueError("SIMAI_EC2_BASE_URL 에 query·fragment 를 넣지 않는다")
    decoded = {base_url, urllib.parse.unquote(base_url), urllib.parse.unquote_plus(base_url)}
    if any(form in text for form in _secret_forms(token) for text in decoded):
        raise ValueError("SIMAI_EC2_BASE_URL 에 기기 토큰이 들어 있다 (토큰은 Authorization 헤더로만)")
    return base_url.rstrip("/") + HEARTBEAT_PATH


class Ec2HeartbeatClient:
    """POST /api/devices/heartbeat. post() 는 HTTP 상태 코드(2xx 가 아니어도)나 Ec2HeartbeatError."""

    def __init__(
        self,
        base_url: str,
        *,
        device_token: str,
        ssl_context: ssl.SSLContext | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not device_token or not device_token.strip():
            raise ValueError("기기 토큰이 없다(.env 의 SIMAI_DEVICE_TOKEN)")
        self.url = validate_base_url(base_url, device_token)
        self._token = device_token
        self._timeout_s = timeout_s
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("인증서 검증(체인·호스트 이름)을 끈 문맥으로는 보내지 않는다")
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def __repr__(self) -> str:  # 토큰을 찍지 않는다
        return f"Ec2HeartbeatClient(url={self.url!r}, device_token=***)"

    def post(self, envelope: dict[str, Any]) -> int:
        body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_s) as resp:
                resp.read(4096)
                return int(resp.status)
        except urllib.error.HTTPError as e:
            with e:
                return int(e.code)
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            # 문장을 쓰지 않는다(종류만): 받은 값이 섞일 수 있다. 원인 예외도 잇지 않는다(from None)
            raise Ec2HeartbeatError(f"EC2 하트비트 송신 실패 ({_kind(e)})") from None


def _kind(e: BaseException) -> str:
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, BaseException):
        return f"{type(e).__name__}/{type(e.reason).__name__}"
    return type(e).__name__


class Ec2HeartbeatSender:
    """1초마다 build() 로 만든 봉투를 EC2 로 보낸다. 스레드 하나, 한 번에 하나만 보낸다(대기열 없음).

    build 가 None 을 돌려주면 그 차례는 보내지 않는다. on_result(ok, 단조 시각, 벽시계 ms) 는 응답을 받은(또는 실패한) 직후 부른다.
    """

    def __init__(
        self,
        client: Ec2HeartbeatClient,
        build: Callable[[], dict[str, Any] | None],
        on_result: Callable[[bool, float, int], None],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        monotonic: Callable[[], float] = time.perf_counter,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._client = client
        self._build = build
        self._on_result = on_result
        self._interval_s = interval_s
        self._monotonic = monotonic
        self._now_ms = now_ms
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_problem: str | None = None  # 상태가 바뀔 때만 로그를 쓴다(1초마다 같은 경고를 쓰지 않음)
        self.sent = 0
        self.succeeded = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ec2-heartbeat", daemon=True)
        self._thread.start()

    def send_once(self) -> bool | None:
        """한 번 만들어 보낸다. 성공이면 True, 실패면 False, 보낼 것이 없으면 None."""
        envelope = self._build()
        if envelope is None:
            return None
        problem: str | None = None
        try:
            status = self._client.post(envelope)
        except Ec2HeartbeatError as e:
            ok, problem = False, str(e)
        else:
            ok = 200 <= status < 300
            if not ok:
                problem = (f"EC2 가 기기 토큰을 거부했다 ({status}). SIMAI_DEVICE_TOKEN 을 확인한다"
                           if status in (401, 403) else f"EC2 하트비트 응답 {status}")
        self.sent += 1
        self.succeeded += int(ok)
        self._on_result(ok, self._monotonic(), self._now_ms())
        if problem != self._last_problem:
            if problem is None:
                log.info("EC2 하트비트 응답 정상")
            else:
                log.warning("%s", problem)
            self._last_problem = problem
        return ok

    def _run(self) -> None:
        while not self._stop.is_set():
            started = self._monotonic()
            try:
                self.send_once()
            except Exception as e:  # noqa: BLE001 - 하트비트 오류로 스레드를 멈추지 않는다. 문장은 쓰지 않는다
                log.error("EC2 하트비트 처리 오류 (%s)", type(e).__name__)
            self._stop.wait(max(0.0, self._interval_s - (self._monotonic() - started)))

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=DEFAULT_TIMEOUT_S + 1)
