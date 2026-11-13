"""EC2(기기 토큰)·S3 호출과 재시도 (S15P31A101-668).

- 인증: 기기 토큰(`SIMAI_DEVICE_TOKEN`)을 `Authorization: Bearer` 헤더에만 싣는다. 로그·예외 문장에 토큰·서명 주소를 넣지 않는다.
- EC2: `GET /api/devices/recordings/windows`(recording_windows), `POST /api/uploads/sign`(RECORDING_SEGMENT),
  `POST /api/devices/recordings/segments`(recording_segment).
- S3: 서명이 돌려준 주소·헤더 그대로 PUT(형식·크기·해시가 서명에 묶여 있다. 서명 주소에는 Authorization 을 싣지 않는다).
- 재시도: 네트워크 오류·5xx·429 는 1·2·4·8·16 s 간격으로 최대 5번. 그 밖의 4xx 는 바로 RequestRejected(그 조각만 실패).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger("simai_recorder.upload")

T = TypeVar("T")
Opener = Callable[..., Any]

DEFAULT_ATTEMPTS = 5
DEFAULT_TIMEOUT_S = 30.0
#: PUT 은 조각 하나(약 40 MB)를 시드니로 보낸다. 느린 상향(5 Mbps)에서도 끝나게 넉넉히 둔다
PUT_TIMEOUT_S = 600.0


class RequestRejected(Exception):
    """다시 해도 같은 거절(4xx). status 와 EC2 오류 코드(있으면)만 담는다."""

    def __init__(self, what: str, status: int, code: str | None) -> None:
        super().__init__(f"{what}: HTTP {status}{f' {code}' if code else ''}")
        self.status = status
        self.code = code


class TransientError(Exception):
    """다시 해 볼 만한 실패(네트워크·5xx·429)."""


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_base_url(base_url: str) -> str:
    """https 주소만(시험의 루프백 http 는 허용). 계정·query·fragment·토큰을 넣지 않는다."""
    parts = urllib.parse.urlsplit(base_url.strip())
    loopback_http = parts.scheme == "http" and _is_loopback(parts.hostname or "")
    if (parts.scheme != "https" and not loopback_http) or not parts.hostname:
        raise ValueError("SIMAI_EC2_BASE_URL 이 올바른 https 주소가 아니다")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("SIMAI_EC2_BASE_URL 에 계정·query·fragment 를 넣지 않는다")
    return base_url.strip().rstrip("/")


def with_retry(what: str, call: Callable[[], T], sleep: Callable[[float], None], attempts: int = DEFAULT_ATTEMPTS) -> T:
    """TransientError 면 1·2·4·8·16 s 간격으로 다시. 마지막까지 실패하면 마지막 TransientError 를 그대로 올린다."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except TransientError as e:
            if attempt == attempts:
                raise
            delay = float(2 ** (attempt - 1))
            log.warning("%s 실패(%s), %.0f초 뒤 다시(%d/%d)", what, e, delay, attempt, attempts)
            sleep(delay)
    raise AssertionError("도달하지 않음")


def _error_code(error: urllib.error.HTTPError) -> str | None:
    try:
        body = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _send(what: str, opener: Opener, request: urllib.request.Request, timeout: float) -> bytes:
    """요청 한 번. 실패를 TransientError·RequestRejected 로 나눈다(문장에 주소를 넣지 않는다)."""
    try:
        with opener(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code >= 500 or e.code == 429:
            raise TransientError(f"HTTP {e.code}") from None
        raise RequestRejected(what, e.code, _error_code(e)) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise TransientError(type(e).__name__) from None


class Ec2Client:
    """EC2 기기 REST. opener 는 시험에서 바꿔 끼운다(기본 urllib.request.urlopen)."""

    def __init__(self, base_url: str, token: str, *, opener: Opener | None = None,
                 sleep: Callable[[float], None] = time.sleep, attempts: int = DEFAULT_ATTEMPTS,
                 timeout: float = DEFAULT_TIMEOUT_S) -> None:
        if not token:
            raise ValueError("기기 토큰이 없다(.env 의 SIMAI_DEVICE_TOKEN)")
        self._base = check_base_url(base_url)
        self._token = token
        self._opener: Opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._attempts = attempts
        self._timeout = timeout

    def _json(self, what: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")

        def call() -> dict[str, Any]:
            request = urllib.request.Request(self._base + path, data=data, method=method)
            request.add_header("Authorization", f"Bearer {self._token}")
            request.add_header("Accept", "application/json")
            if data is not None:
                request.add_header("Content-Type", "application/json")
            raw = _send(what, self._opener, request, self._timeout)
            return json.loads(raw or b"{}")

        return with_retry(what, call, self._sleep, self._attempts)

    def windows(self, from_ms: int, to_ms: int) -> dict[str, Any]:
        return self._json("작업 구간 조회", "GET", f"/api/devices/recordings/windows?from={from_ms}&to={to_ms}")

    def sign(self, segment_id: str, size_bytes: int, sha256_hex: str) -> dict[str, Any]:
        body = {"purpose": "RECORDING_SEGMENT", "content_type": "video/mp4", "size_bytes": size_bytes,
                "sha256": sha256_hex, "segment_id": segment_id}
        return self._json("업로드 서명", "POST", "/api/uploads/sign", body)

    def register(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._json("조각 등록", "POST", "/api/devices/recordings/segments", record)

    def put(self, signed: dict[str, Any], path: Path) -> None:
        """서명 주소로 한 번 PUT(재시도는 부르는 쪽이 새 서명으로). 파일을 메모리에 다 올리지 않고 흘려 보낸다."""
        size = path.stat().st_size
        with path.open("rb") as body:
            request = urllib.request.Request(signed["upload_url"], data=body, method=signed.get("method") or "PUT")
            for name, value in (signed.get("headers") or {}).items():
                request.add_header(name, value)
            request.add_header("Content-Length", str(size))
            _send("S3 업로드", self._opener, request, PUT_TIMEOUT_S)
