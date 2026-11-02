"""처리기가 객체 저장소를 읽고 쓰는 주입점 (명세 14 객체 저장소, S15P31A101-239·-248).

객체 저장소는 비공개이고 임시 서명 주소로만 받고 올린다(14). 워커는 대여에 묶인 워커 전용 서명 API
(`POST /api/worker/jobs/{job_id}/objects/sign`, lease_id·op GET|PUT, S15P31A101-248)로 주소를 받는다.
- GET: 그 작업 input 에 있는 객체 주소(s3://)만.
- PUT: 키는 EC2 가 정한다(그 작업 전용). 처리기는 이름만 주고, 올린 객체의 s3:// 주소를 돌려받아 결과에 싣는다.
운영 구현은 HttpObjectStore 다(서명은 I5 전송으로, 주고받기는 서명 주소로). 다른 작업
(vlm_rationale·static_reconstruct·incident_summary)도 이 주입점을 쓴다.
"""

from __future__ import annotations

import hashlib
import http.client
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .handlers import JobContext
from .transport import NoRedirect, Transport, TransportError, is_loopback, objects_sign_path


class ObjectStoreError(Exception):
    """서명·전송 실패. retryable 이면 새 대여로 다시 해 볼 만하다(일시 단절, 서명 만료 등)."""

    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status  # 객체 저장소 HTTP 상태(전송 단계). 아직 올라오지 않은 객체(403·404)를 가리는 데 쓴다(-408)


class ObjectStore(Protocol):
    def get(self, ctx: JobContext, uri: str) -> bytes:
        """input 에 있는 s3:// 객체의 바이트."""
        ...

    def put(self, ctx: JobContext, name: str, content: bytes, content_type: str) -> str:
        """이 작업 전용 키에 올리고 그 s3:// 주소를 돌려준다. name 은 작업 안에서 객체를 가리는 이름이다."""
        ...


class HttpObjectStore:
    """워커 전용 서명 API(S15P31A101-248)로 주소를 받아 객체 저장소와 직접 주고받는다.

    - 서명: 대여의 lease_id 로 `POST /api/worker/jobs/{job_id}/objects/sign`. 409(대여가 유효하지 않음)·400·401·403 과
      503 STORAGE_NOT_CONFIGURED(EC2 설정 오류)는 다시 해도 같으므로 retryable=False. 응답을 못 받음·429·그 밖 5xx 는 retryable.
    - 전송: 서명 주소로 GET·PUT. PUT 은 응답의 headers(형식·크기·해시가 서명에 묶임)를 그대로 싣는다.
      서명 만료(403 "expired")·408·429·5xx·연결 실패는 retryable(새 대여에서 새 주소로). 그 밖 403(AccessDenied·
      SignatureDoesNotMatch)은 설정 오류라 retryable 아님. 서명 주소는 로그·예외 문구에 넣지 않는다.
    - 취소(ctx.cancelled)되면 서명·전송을 시작하지 않는다. 작업에 남은 시간 예산(ctx.remaining_s)이 있으면 단계마다 남은 시간을
      다시 확인해 0 이하면 시작하지 않고, 양수면 설정 상한(sign_timeout_s·data_timeout_s)과 남은 시간 중 짧은 값을 소켓 상한으로
      쓴다. 시간 상한은 러너가 취소된 처리기를 기다리는 시간(abandon_grace_s 30초)보다 짧게 둔다(막힌 전송이 워커 프로세스를
      HandlerStuck 으로 끝내지 않게). urllib 의 timeout 은 소켓 동작 하나의 상한이다.
    - TLS 검증을 끄지 않는다. 평문 http 서명 주소는 루프백 개발 저장소만.
    """

    def __init__(self, transport: Transport, *, sign_timeout_s: float = 10.0, data_timeout_s: float = 20.0,
                 opener: Callable[..., Any] | None = None) -> None:
        self._transport = transport
        self._sign_timeout_s = sign_timeout_s
        self._data_timeout_s = data_timeout_s
        self._open = opener or urllib.request.build_opener(NoRedirect()).open

    def get(self, ctx: JobContext, uri: str) -> bytes:
        signed = self._sign(ctx, {"op": "GET", "uri": uri})
        return self._transfer("GET", signed, None, ctx)

    def put(self, ctx: JobContext, name: str, content: bytes, content_type: str) -> str:
        signed = self._sign(ctx, {
            "op": "PUT", "name": name, "content_type": content_type,
            "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
        })
        uri = signed.get("uri")
        if not isinstance(uri, str) or not uri.startswith("s3://"):
            raise ObjectStoreError("서명 응답에 객체 주소가 없다", retryable=True)
        self._transfer("PUT", signed, content, ctx)
        return uri

    def _sign(self, ctx: JobContext, body: Mapping[str, Any]) -> Mapping[str, Any]:
        _stop_if_cancelled(ctx)
        timeout_s = _timeout_for(ctx, self._sign_timeout_s, "서명 요청")
        try:
            resp = self._transport.post(objects_sign_path(ctx.job_id), {"lease_id": ctx.lease_id, **body},
                                        timeout_s)
        except TransportError as e:
            _stop_if_cancelled(ctx)
            raise ObjectStoreError(f"서명 요청 실패: {e}", retryable=True) from None
        if resp.status == 200 and resp.body and isinstance(resp.body.get("url"), str):
            return resp.body
        code = (resp.body or {}).get("code") or ""
        if code != "STORAGE_NOT_CONFIGURED" and (resp.status == 200 or resp.status == 429 or resp.status >= 500):
            raise ObjectStoreError(f"서명 응답 {resp.status}", retryable=True)
        # 409 LEASE_NOT_ACTIVE(대여 상실·취소), 403 OBJECT_NOT_IN_INPUT, 400 형식 오류, 401 토큰, 503 저장소 미설정: 다시 해도 같다
        raise ObjectStoreError(f"서명 거부 {resp.status} {code}".rstrip(), retryable=False)

    def _transfer(self, method: str, signed: Mapping[str, Any], content: bytes | None, ctx: JobContext) -> bytes:
        url = str(signed["url"])
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback(parts.hostname or "")):
            raise ObjectStoreError("서명 주소가 https 가 아니다", retryable=False)
        raw_headers = signed.get("headers") or {}
        if not isinstance(raw_headers, Mapping):
            raise ObjectStoreError("서명 응답의 headers 가 객체가 아니다", retryable=True)
        headers = {str(k): str(v) for k, v in raw_headers.items()}
        _stop_if_cancelled(ctx)
        timeout_s = _timeout_for(ctx, self._data_timeout_s, f"{method} 전송")
        request = urllib.request.Request(url, data=content, method=method, headers=headers)
        try:
            with self._open(request, timeout=timeout_s) as resp:
                status = getattr(resp, "status", 200)
                data = resp.read()
        except urllib.error.HTTPError as e:
            with e:
                retryable = e.code in (408, 429) or e.code >= 500 or (e.code == 403 and _expired(e))
            raise ObjectStoreError(f"{method} 응답 {e.code}", retryable=retryable, status=e.code) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            _stop_if_cancelled(ctx)
            raise ObjectStoreError(f"{method} 연결 실패: {type(e).__name__}", retryable=True) from None
        _stop_if_cancelled(ctx)
        if not 200 <= status < 300:
            raise ObjectStoreError(f"{method} 응답 {status}", retryable=status in (408, 429) or status >= 500,
                                   status=status)
        return data


def _stop_if_cancelled(ctx: JobContext) -> None:
    if ctx.cancelled.is_set():
        raise ObjectStoreError("작업이 취소돼 객체 저장소를 쓰지 않는다", retryable=False)


def _timeout_for(ctx: JobContext, limit_s: float, what: str) -> float:
    rem = ctx.remaining_s()
    if rem is None:
        return limit_s
    if rem <= 0:
        raise ObjectStoreError(f"남은 시간 예산이 없어 {what}을 시작하지 않는다", retryable=True)
    return min(limit_s, rem)


def _expired(error: urllib.error.HTTPError) -> bool:
    """S3 는 서명 만료도 403 AccessDenied 로 답하고 본문에 "Request has expired" 를 싣는다. 그때만 새 주소로 다시 해 볼 만하다."""
    try:
        return b"expired" in error.read(4096).lower()
    except (OSError, http.client.HTTPException):
        return False
