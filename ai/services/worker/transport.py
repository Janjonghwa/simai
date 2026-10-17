"""I5 전송 계층: 분석 워커 → EC2 HTTPS 요청 한 번.

전송은 주입한다. 시험은 가짜 EC2(`Transport` 구현)를 넣고, 운영은 `HttpTransport` 를 쓴다.
경로·인증 규칙의 정본은 `contracts/messages/job_lease_request.schema.json` 의 설명이다.
"""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# 경로는 서버 OpenAPI 확정 전 기본안이다 (계약 설명과 같은 값)
LEASE_PATH = "/api/worker/jobs/lease"
TOKEN_HEADER = "Authorization"  # 값: "Bearer <워커 토큰>". 토큰은 .env 의 SIMAI_WORKER_TOKEN
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def heartbeat_path(job_id: str) -> str:
    return f"/api/worker/jobs/{urllib.parse.quote(job_id, safe='')}/heartbeat"


def result_path(job_id: str) -> str:
    return f"/api/worker/jobs/{urllib.parse.quote(job_id, safe='')}/result"


@dataclass(frozen=True)
class Response:
    """HTTP 응답. body 는 JSON 객체이거나, 본문이 없거나 JSON 이 아니면 None."""

    status: int
    body: dict[str, Any] | None


class TransportError(Exception):
    """연결 실패·시간 초과·TLS 오류처럼 응답을 받지 못한 경우. 다시 보낼 수 있다."""


class Transport(Protocol):
    def post(self, path: str, body: Mapping[str, Any], timeout_s: float) -> Response:
        """JSON 본문을 POST 하고 응답을 돌려준다. 응답을 못 받으면 TransportError."""
        ...


def _parse(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가지 않는다. 따라가면 Authorization 헤더가 다른 호스트·평문 http 로 실린다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None  # urllib 이 3xx 를 HTTPError 로 올리고, post() 가 Response(3xx) 로 돌려준다


class HttpTransport:
    """표준 라이브러리 urllib 로 EC2 에 HTTPS 요청을 보낸다.

    인증서 검증은 끄지 않는다(명세 14). 평문 http 는 같은 기계 안 개발 서버(루프백)만 허용한다.
    리다이렉트(3xx)는 따라가지 않고 응답 그대로 돌려준다(클라이언트가 거부로 분류한다).
    """

    def __init__(self, base_url: str, token: str, *, ssl_context: ssl.SSLContext | None = None) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
            raise ValueError("I5 는 TLS 만 쓴다: 루프백이 아니면 https 주소여야 한다")
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError(f"EC2 주소가 올바르지 않다: {base_url!r}")
        if not token:
            raise ValueError("워커 토큰이 비었다 (.env 의 SIMAI_WORKER_TOKEN)")
        self._base = base_url.rstrip("/")
        self._token = token
        self._ssl = ssl_context if ssl_context is not None else ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=self._ssl)
        )

    def post(self, path: str, body: Mapping[str, Any], timeout_s: float) -> Response:
        request = urllib.request.Request(
            self._base + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                TOKEN_HEADER: f"Bearer {self._token}",
            },
        )
        try:
            with self._opener.open(request, timeout=timeout_s) as resp:
                return Response(resp.status, _parse(resp.read()))
        except urllib.error.HTTPError as e:  # 4xx·5xx 도 응답이다. URLError 보다 먼저 잡는다
            with e:
                return Response(e.code, _parse(e.read()))
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise TransportError(f"{type(e).__name__}: {e}") from e
