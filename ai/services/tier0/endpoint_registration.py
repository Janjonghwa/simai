"""Tier 0 엔드포인트 등록: 기동할 때 공개 IP·카메라별 포트·제어 포트·TLS 공개키 지문을 모아 EC2 에 등록한다.

명세 3.12 엔드포인트 등록, 12.6, 12.7, 14, FR-138. 메시지 형식의 정본은 `contracts/messages/tier0_endpoint.schema.json`.

- RunPod 는 Pod 가 재설정될 때마다 외부 포트 매핑이 바뀐다. 포트는 70000 이상 번호로 요청하는 대칭 매핑을 쓰고,
  실제 공개 포트는 `RUNPOD_TCP_PORT_<요청 번호>` 에서, 공개 IP 는 `RUNPOD_PUBLIC_IP` 에서 읽는다.
- TLS 공개키 지문은 Tier 0 TLS 서버가 쓰는 인증서(PEM, Network Volume 비밀 폴더)의 SubjectPublicKeyInfo(DER) SHA-256 이다.
  인증서 파일만 읽는다. 비밀 키가 섞인 파일은 읽지 않는다.
- 값을 모르면 지어내지 않는다. 빠진 환경 변수·설정은 이름을 모두 모아 `EndpointConfigError` 로 등록 실패를 보고하고 보내지 않는다.
- 메시지에는 비밀 값(Tier 0·기기 토큰, TLS 비밀 키, RunPod API 키)을 싣지 않는다. 토큰은 Authorization 헤더에만 싣고
  로그·오류 문장에 넣지 않는다.
- 전송은 주입한다(`Transport`). I1 WebSocket 클라이언트가 아직 없어 기본 구현은 HTTPS POST(`HttpTransport`) 다.
  응답을 받지 못했거나(연결 실패·시간 초과) 429·5xx 면 지수 백오프로 같은 봉투(같은 msg_id)를 다시 보낸다.

환경 변수(.env, 비밀 값은 커밋하지 않는다):

    SIMAI_EC2_BASE_URL          EC2 주소 (https. 평문 http 는 루프백 개발 서버만)
    SIMAI_TIER0_TOKEN           Tier 0 토큰 (비밀, Authorization: Bearer)
    SIMAI_TIER0_ID              Tier 0 인스턴스 ID, 봉투 source_id·gateway_id (기본 tier0-01)
    SIMAI_TIER0_CONTROL_PORT    I0 제어 WebSocket(wss) 대칭 매핑 요청 번호 (예: 70000)
    SIMAI_TIER0_VIDEO_PORTS     카메라별 영상 TLS 포트 요청 번호 "cam1:70001,cam2:70002,..." (카메라가 포트를 같이 써도 된다)
    SIMAI_TIER0_TLS_CERT_FILE   Tier 0 TLS 서버 인증서 PEM 경로 (공개키 지문 계산용, 비밀 키 파일이 아니다)
    RUNPOD_PUBLIC_IP            Pod 공개 IPv4 (RunPod 가 넣는다)
    RUNPOD_TCP_PORT_<요청 번호>  실제 공개 포트 (RunPod 가 넣는다)
    RUNPOD_POD_ID, RUNPOD_DC_ID 선택. 있으면 pod_id, datacenter 로 싣는다

실행: `python -m services.tier0.endpoint_registration` (ai/ 에서). Tier 0 조립이 들어오면 기동 순서와 I1 재연결 때 부른다.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = "1.3.0"  # tier0_endpoint 가 들어온 계약 버전 (계약 예시와 같다)
MSG_TYPE = "tier0_endpoint"
# 등록 경로는 서버 OpenAPI 확정 전 기본안이다. 명세는 I1(WebSocket) 메시지로 정했고, I1 클라이언트가 들어오면 그쪽으로 보낸다.
REGISTER_PATH = "/api/endpoints/tier0"
MIN_REQUESTED_PORT = 70000  # RunPod 대칭 매핑 (계약 port_mapping.requested_port)
DEFAULT_TIER0_ID = "tier0-01"

_INSTANCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_POD_ID = re.compile(r"^[A-Za-z0-9-]+$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_UINT = re.compile(r"^[0-9]{1,6}$")
_PEM_CERT = re.compile(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)

log = logging.getLogger("services.tier0.endpoint")


class EndpointConfigError(Exception):
    """등록에 필요한 값을 모르거나 값이 틀렸다. 지어내지 않고 등록 실패로 보고한다(문장에 비밀 값이 없다)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("엔드포인트 등록 값 오류: " + "; ".join(self.problems))


class RegistrationFailed(Exception):
    """EC2 가 등록을 받지 않았다(인증 실패·거부) 또는 다시 보내기 상한까지 응답을 받지 못했다."""

    def __init__(self, message: str, *, retryable: bool, attempts: int) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.attempts = attempts


# ---------------------------------------------------------------------------- TLS 공개키 지문


def _der_tlv(buf: bytes, i: int) -> tuple[int, int, int]:
    """DER 항목 하나: (태그, 내용 시작, 내용 끝). 길이가 버퍼를 넘거나 부정 길이면 ValueError."""
    if i + 2 > len(buf):
        raise ValueError("DER 가 잘렸다")
    tag, length = buf[i], buf[i + 1]
    i += 2
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or n > 4 or i + n > len(buf):
            raise ValueError("DER 길이가 올바르지 않다")
        length = int.from_bytes(buf[i : i + n], "big")
        i += n
    end = i + length
    if end > len(buf):
        raise ValueError("DER 가 잘렸다")
    return tag, i, end


def spki_der_from_cert_der(cert_der: bytes) -> bytes:
    """X.509 인증서(DER)에서 SubjectPublicKeyInfo(DER, 태그 포함)를 꺼낸다.

    Certificate ::= SEQUENCE { tbsCertificate SEQUENCE { [0] version?, serialNumber, signature, issuer, validity,
    subject, subjectPublicKeyInfo, ... }, ... } 구조만 따라간다(서명은 확인하지 않는다. 지문 계산용).
    """
    tag, start, end = _der_tlv(cert_der, 0)
    if tag != 0x30 or end != len(cert_der):
        raise ValueError("X.509 인증서가 아니다")
    tag, i, _ = _der_tlv(cert_der, start)
    if tag != 0x30:
        raise ValueError("tbsCertificate 가 없다")
    tag, _, field_end = _der_tlv(cert_der, i)
    if tag == 0xA0:  # 명시적 version
        i = field_end
    for name in ("serialNumber", "signature", "issuer", "validity", "subject"):
        tag, _, field_end = _der_tlv(cert_der, i)
        if (name == "serialNumber" and tag != 0x02) or (name != "serialNumber" and tag != 0x30):
            raise ValueError(f"tbsCertificate.{name} 형식이 아니다")
        i = field_end
    tag, spki_start, spki_end = _der_tlv(cert_der, i)
    if tag != 0x30:
        raise ValueError("subjectPublicKeyInfo 가 없다")
    alg_tag, _, alg_end = _der_tlv(cert_der, spki_start)
    key_tag, _, key_end = _der_tlv(cert_der, alg_end)
    if alg_tag != 0x30 or key_tag != 0x03 or key_end != spki_end:
        raise ValueError("subjectPublicKeyInfo 형식이 아니다")
    return cert_der[i:spki_end]


def spki_sha256(cert_der: bytes) -> str:
    """인증서(DER)의 공개키 지문 `sha256:<64 hex>` (계약 tls_public_key_sha256)."""
    return "sha256:" + hashlib.sha256(spki_der_from_cert_der(cert_der)).hexdigest()


def cert_der_from_pem(pem: bytes) -> bytes:
    """PEM 의 첫 인증서(서버 인증서)를 DER 로. 비밀 키가 섞인 파일은 거부한다(지문 계산에 비밀 키를 읽지 않는다)."""
    if b"PRIVATE KEY" in pem:
        raise ValueError("인증서 파일에 비밀 키가 섞여 있다. 인증서(공개) 파일만 지정한다")
    m = _PEM_CERT.search(pem)
    if m is None:
        raise ValueError("PEM 인증서(BEGIN CERTIFICATE)가 없다")
    try:
        return base64.b64decode(b"".join(m.group(1).split()), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("PEM 인증서 본문이 base64 가 아니다") from e


# ---------------------------------------------------------------------------- 값 모으기


@dataclass(frozen=True)
class PortRequest:
    """대칭 매핑을 요청한 포트 하나. VIDEO 는 카메라마다 한 줄(포트를 같이 써도 줄은 따로)."""

    role: str  # CONTROL | VIDEO
    requested_port: int
    camera_id: str | None = None


def parse_video_ports(text: str) -> list[PortRequest]:
    """`cam1:70001,cam2:70002` → VIDEO 요청 목록. 형식이 틀리면 ValueError."""
    out: list[PortRequest] = []
    for item in (s.strip() for s in text.split(",")):
        if not item:
            continue
        cam, sep, port = item.partition(":")
        cam, port = cam.strip(), port.strip()
        if not sep or not cam or not _UINT.match(port):
            raise ValueError(f"SIMAI_TIER0_VIDEO_PORTS 항목 {item!r} 은 '<camera_id>:<요청 번호>' 형식이어야 한다")
        out.append(PortRequest("VIDEO", int(port), cam))
    return out


def _env_port_requests(env: Mapping[str, str], problems: list[str]) -> list[PortRequest]:
    requests: list[PortRequest] = []
    control = (env.get("SIMAI_TIER0_CONTROL_PORT") or "").strip()
    if not control:
        problems.append("SIMAI_TIER0_CONTROL_PORT 가 없다 (I0 제어 포트 요청 번호)")
    elif not _UINT.match(control):
        problems.append("SIMAI_TIER0_CONTROL_PORT 는 정수여야 한다")
    else:
        requests.append(PortRequest("CONTROL", int(control)))
    video = (env.get("SIMAI_TIER0_VIDEO_PORTS") or "").strip()
    if not video:
        problems.append("SIMAI_TIER0_VIDEO_PORTS 가 없다 (카메라별 영상 포트 요청 번호)")
    else:
        try:
            parsed = parse_video_ports(video)
        except ValueError as e:
            problems.append(str(e))
        else:
            if not parsed:
                problems.append("SIMAI_TIER0_VIDEO_PORTS 에 카메라가 없다")
            requests.extend(parsed)
    return requests


def build_payload(
    env: Mapping[str, str],
    *,
    cert_pem: bytes | None,
    registered_at_ms: int,
) -> dict[str, Any]:
    """환경 변수와 인증서로 tier0_endpoint payload 를 만든다. 모르거나 틀린 값은 모두 모아 EndpointConfigError.

    cert_pem 이 None 이면(파일을 못 읽음) 지문을 모르는 것으로 보고 실패한다.
    """
    problems: list[str] = []
    gateway_id = (env.get("SIMAI_TIER0_ID") or DEFAULT_TIER0_ID).strip()
    if not _INSTANCE_ID.match(gateway_id):
        problems.append("SIMAI_TIER0_ID 는 소문자·숫자·'-' 로 된 인스턴스 ID 여야 한다 (예: tier0-01)")

    public_ip = (env.get("RUNPOD_PUBLIC_IP") or "").strip()
    if not public_ip:
        problems.append("RUNPOD_PUBLIC_IP 가 없다 (Pod 공개 IP 를 모른다)")
    else:
        try:
            ip = ipaddress.IPv4Address(public_ip)
        except ValueError:
            problems.append("RUNPOD_PUBLIC_IP 가 IPv4 주소가 아니다")
        else:
            if ip.is_unspecified or ip.is_multicast:
                problems.append("RUNPOD_PUBLIC_IP 가 접속할 수 있는 주소가 아니다")
            public_ip = str(ip)

    ports: list[dict[str, Any]] = []
    seen_cameras: set[str] = set()
    requests = _env_port_requests(env, problems)
    control_requested = {r.requested_port for r in requests if r.role == "CONTROL"}
    for r in requests:
        if r.requested_port < MIN_REQUESTED_PORT:
            problems.append(f"요청 번호 {r.requested_port} 는 대칭 매핑 번호({MIN_REQUESTED_PORT} 이상)가 아니다")
            continue
        if r.role == "VIDEO":
            assert r.camera_id is not None
            if not _CAMERA_ID.match(r.camera_id):
                problems.append(f"camera_id {r.camera_id!r} 형식이 틀렸다 (영문·숫자·'_'·'-')")
                continue
            if r.camera_id in seen_cameras:
                problems.append(f"camera_id {r.camera_id} 가 SIMAI_TIER0_VIDEO_PORTS 에 두 번 있다")
                continue
            seen_cameras.add(r.camera_id)
            if r.requested_port in control_requested:
                problems.append(f"요청 번호 {r.requested_port} 를 제어 포트와 영상 포트가 같이 쓴다")
                continue
        name = f"RUNPOD_TCP_PORT_{r.requested_port}"
        raw = (env.get(name) or "").strip()
        if not raw:
            problems.append(f"{name} 가 없다 (공개 포트를 모른다. RunPod 템플릿에 {r.requested_port} 를 열었는지 확인)")
            continue
        if not _UINT.match(raw) or not 1 <= int(raw) <= 65535:
            problems.append(f"{name} 가 1~65535 포트 번호가 아니다")
            continue
        entry: dict[str, Any] = {"role": r.role}
        if r.camera_id is not None:
            entry["camera_id"] = r.camera_id
        entry.update(requested_port=r.requested_port, public_port=int(raw))
        ports.append(entry)

    fingerprint: str | None = None
    if cert_pem is None:
        problems.append("SIMAI_TIER0_TLS_CERT_FILE 인증서를 읽지 못했다 (TLS 공개키 지문을 모른다)")
    else:
        try:
            fingerprint = spki_sha256(cert_der_from_pem(cert_pem))
        except ValueError as e:
            problems.append(f"SIMAI_TIER0_TLS_CERT_FILE: {e}")

    payload: dict[str, Any] = {
        "gateway_id": gateway_id,
        "public_ip": public_ip,
        "ports": ports,
        "tls_public_key_sha256": fingerprint,
        "registered_at": int(registered_at_ms),
    }
    pod_id = (env.get("RUNPOD_POD_ID") or "").strip()
    if pod_id:
        if _POD_ID.match(pod_id):
            payload["pod_id"] = pod_id
        else:
            problems.append("RUNPOD_POD_ID 형식이 틀렸다 (영문·숫자·'-')")
    datacenter = (env.get("RUNPOD_DC_ID") or "").strip()
    if datacenter:
        payload["datacenter"] = datacenter
    if problems:
        raise EndpointConfigError(problems)
    return payload


def read_cert_pem(env: Mapping[str, str]) -> bytes | None:
    """SIMAI_TIER0_TLS_CERT_FILE 을 읽는다. 없거나 못 읽으면 None(지문을 모름)."""
    path = (env.get("SIMAI_TIER0_TLS_CERT_FILE") or "").strip()
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        log.warning("Tier 0 TLS 인증서 파일을 읽지 못했다")
        return None


def build_message(payload: Mapping[str, Any], *, msg_id: str, sent_at_ms: int) -> dict[str, Any]:
    """봉투. source_id 는 gateway_id 와 같다(계약)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "msg_type": MSG_TYPE,
        "msg_id": msg_id,
        "sent_at": int(sent_at_ms),
        "source_id": payload["gateway_id"],
        "payload": dict(payload),
    }


# ---------------------------------------------------------------------------- 전송


class TransportError(Exception):
    """응답을 받지 못했다(연결 실패·시간 초과·TLS 오류). 다시 보낼 수 있다."""


class Transport(Protocol):
    def post(self, path: str, body: Mapping[str, Any], timeout_s: float) -> int:
        """JSON 본문을 POST 하고 HTTP 상태 코드를 돌려준다. 응답을 못 받으면 TransportError."""
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가지 않는다. 따라가면 Authorization 헤더가 다른 호스트·평문 http 로 실린다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class HttpTransport:
    """표준 라이브러리 urllib 로 EC2 에 HTTPS POST. 인증서 검증은 끄지 않는다(명세 14). 평문 http 는 루프백만."""

    def __init__(self, base_url: str, token: str, *, ssl_context: ssl.SSLContext | None = None) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("SIMAI_EC2_BASE_URL 이 올바른 주소가 아니다")
        if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
            raise ValueError("EC2 등록은 TLS 만 쓴다: 루프백이 아니면 https 주소여야 한다")
        if not token:
            raise ValueError("Tier 0 토큰이 비었다 (.env 의 SIMAI_TIER0_TOKEN)")
        self._base = base_url.rstrip("/")
        self._token = token
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def __repr__(self) -> str:  # 토큰을 찍지 않는다
        return f"HttpTransport(base_url={self._base!r}, token=***)"

    def post(self, path: str, body: Mapping[str, Any], timeout_s: float) -> int:
        request = urllib.request.Request(
            self._base + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
        )
        try:
            with self._opener.open(request, timeout=timeout_s) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as e:  # 4xx·5xx·3xx 도 응답이다. 본문은 읽어 버리고 상태만 쓴다
            with e:
                return int(e.code)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            # 예외 문장에는 주소·원인만 있고 헤더(토큰)는 없다
            raise TransportError(f"{type(e).__name__}: {e}") from e


# ---------------------------------------------------------------------------- 등록


@dataclass(frozen=True)
class Backoff:
    """다시 보내기 간격: initial_s, initial_s·factor, … 를 max_s 에서 자른다."""

    initial_s: float = 1.0
    factor: float = 2.0
    max_s: float = 30.0

    def __post_init__(self) -> None:
        if self.initial_s <= 0 or self.factor < 1 or self.max_s < self.initial_s:
            raise ValueError("백오프 값이 올바르지 않다")

    def delay(self, failures: int) -> float:
        return min(self.max_s, self.initial_s * self.factor ** max(0, failures - 1))


@dataclass(frozen=True)
class Registered:
    message: dict[str, Any]
    status: int
    attempts: int


class EndpointRegistrar:
    """tier0_endpoint 를 만들어 EC2 에 등록한다. 시계·대기·ID 생성·전송은 주입한다."""

    def __init__(
        self,
        transport: Transport,
        *,
        clock_ms: Callable[[], int],
        sleep: Callable[[float], Any],
        backoff: Backoff | None = None,
        max_attempts: int = 8,
        request_timeout_s: float = 10.0,
        new_msg_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        path: str = REGISTER_PATH,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 는 1 이상이다")
        self._transport = transport
        self._clock_ms = clock_ms
        self._sleep = sleep
        self.backoff = backoff or Backoff()
        self._max_attempts = max_attempts
        self._timeout_s = request_timeout_s
        self._new_msg_id = new_msg_id
        self._path = path

    def build(self, env: Mapping[str, str], cert_pem: bytes | None) -> dict[str, Any]:
        """봉투까지 만든다. 값을 모르면 EndpointConfigError(보내지 않는다)."""
        now = int(self._clock_ms())
        payload = build_payload(env, cert_pem=cert_pem, registered_at_ms=now)
        return build_message(payload, msg_id=self._new_msg_id(), sent_at_ms=now)

    def send(self, message: dict[str, Any], *, should_stop: Callable[[], bool] = lambda: False) -> Registered:
        """같은 봉투를 2xx 까지 보낸다. 429·5xx·무응답은 백오프 후 다시, 3xx·4xx 는 바로 실패."""
        last = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                status = self._transport.post(self._path, message, self._timeout_s)
            except TransportError as e:
                last = f"응답 없음 ({e})"
            else:
                if 200 <= status < 300:
                    log.info("엔드포인트 등록 완료 (status=%d, 시도 %d)", status, attempt)
                    return Registered(message, status, attempt)
                if status in (401, 403):
                    raise RegistrationFailed(
                        f"EC2 가 등록을 거부했다: {status} 인증 실패 (SIMAI_TIER0_TOKEN·SIMAI_TIER0_ID 확인)",
                        retryable=False, attempts=attempt,
                    )
                if status != 429 and status < 500:
                    raise RegistrationFailed(
                        f"EC2 가 등록을 거부했다: {status} (주소·형식 확인. 리다이렉트는 따르지 않는다)",
                        retryable=False, attempts=attempt,
                    )
                last = f"EC2 응답 {status}"
            if attempt == self._max_attempts or should_stop():
                break
            delay = self.backoff.delay(attempt)
            log.warning("엔드포인트 등록 실패 (%s). %.1f 초 뒤 다시 보낸다 (시도 %d/%d)",
                        last, delay, attempt, self._max_attempts)
            self._sleep(delay)
        raise RegistrationFailed(f"엔드포인트 등록 실패: {last}", retryable=True, attempts=attempt)

    def register(self, env: Mapping[str, str], cert_pem: bytes | None, **kwargs: Any) -> Registered:
        return self.send(self.build(env, cert_pem), **kwargs)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """한 번 등록하고 끝낸다. 0 등록됨, 2 설정 오류(값을 모름), 1 EC2 등록 실패."""
    parser = argparse.ArgumentParser(
        prog="services.tier0.endpoint_registration",
        description="Tier 0 공개 IP·포트·TLS 공개키 지문을 EC2 에 등록한다 (tier0_endpoint)",
    )
    parser.add_argument("--max-attempts", type=int, default=8)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env = dict(os.environ if env is None else env)
    base_url = (env.get("SIMAI_EC2_BASE_URL") or "").strip()
    token = env.get("SIMAI_TIER0_TOKEN") or ""
    if not base_url or not token:
        print("등록 실패: SIMAI_EC2_BASE_URL·SIMAI_TIER0_TOKEN 이 필요하다 (.env)", file=sys.stderr)
        return 2
    try:
        transport = HttpTransport(base_url, token)
    except ValueError as e:
        print(f"등록 실패: {e}", file=sys.stderr)
        return 2
    registrar = EndpointRegistrar(
        transport,
        clock_ms=lambda: time.time_ns() // 1_000_000,
        sleep=time.sleep,
        max_attempts=args.max_attempts,
    )
    try:
        message = registrar.build(env, read_cert_pem(env))
    except EndpointConfigError as e:
        print(f"등록 실패: {e}", file=sys.stderr)
        return 2
    try:
        registrar.send(message)
    except RegistrationFailed as e:
        print(f"등록 실패: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
