"""Tier 0 접속 주소 조회(명세 3.12 엔드포인트 등록, 12.6 GET /api/endpoints/tier0, 계약 tier0_endpoint).

RunPod 는 Pod 가 재설정될 때마다 외부 포트 매핑이 바뀌므로 노트북에 주소를 고정하지 않는다. 노트북은 EC2 REST 로
현재 엔드포인트(Tier 0 가 등록한 `tier0_endpoint` 봉투 그대로)를 받아 I0 제어 채널에 접속한다.

공개키 지문의 관계(계약 설명, 명세 3.12·12.7·14):
- 노트북은 Tier 0 공개키 지문을 미리 고정해 둔다(.env SIMAI_TIER0_PINNED_SPKI_SHA256, 키 교체 기간에는 쉼표로 여러 개).
  고정값이 없으면 접속하지 않는다(처음 본 키를 믿는 방식 없음).
- EC2 응답의 `tls_public_key_sha256` 이 고정 목록에 없으면 접속하지 않는다. 조회 값만으로 신뢰할 키를 바꾸지 않는다.
- 목록에 있으면 그 값을 이번 접속에서 기대하는 지문으로 쓰고, 실제 서버 인증서의 공개키 지문이 그것과 같아야 한다
  (`pinning.PinnedPeerVerifier`, 기기 토큰을 보내기 전).

전송은 표준 라이브러리 urllib(HTTPS, 인증서 검증 켬, 리다이렉트 안 따름). 평문 http 는 같은 컴퓨터 시험 서버만 허용한다.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from simai_site_alert.pinning import PinMismatchError, is_fingerprint

LOOKUP_PATH = "/api/endpoints/tier0"
DEFAULT_CONTROL_PATH = "/control"  # Tier 0 I0 제어 WebSocket 경로(기본안. 계약에 경로 필드가 없다)
_INSTANCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class EndpointUnavailable(Exception):
    """현재 엔드포인트를 받지 못했다. 잠시 뒤 다시 조회한다(문장에 토큰이 없다)."""


class EndpointNotRegistered(EndpointUnavailable):
    """EC2 에 등록된 Tier 0 엔드포인트가 없다(404). Tier 0 가 기동해 등록할 때까지 다시 조회한다."""


class EndpointAuthError(EndpointUnavailable):
    """401·403. 기기 토큰이 틀렸거나 폐기됐다."""


class InvalidEndpoint(EndpointUnavailable):
    """응답이 tier0_endpoint 형식이 아니다. 그 값으로는 접속하지 않는다."""


@dataclass(frozen=True)
class Tier0Endpoint:
    gateway_id: str
    public_ip: str
    control_port: int
    video_ports: Mapping[str, int]  # camera_id → public_port
    tls_public_key_sha256: str
    registered_at: int
    pod_id: str | None = None

    def control_url(self, path: str = DEFAULT_CONTROL_PATH) -> str:
        return f"wss://{self.public_ip}:{self.control_port}{path}"


def _port(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise InvalidEndpoint("public_port 가 1~65535 정수가 아니다")
    return value


def parse_endpoint(doc: Any) -> Tier0Endpoint:
    """조회 응답 본문(tier0_endpoint 봉투)을 해석한다. 계약과 스키마 밖 규칙(VIDEO camera_id 유일, source_id 일치)을 본다."""
    if not isinstance(doc, Mapping) or doc.get("msg_type") != "tier0_endpoint":
        raise InvalidEndpoint("tier0_endpoint 메시지가 아니다")
    p = doc.get("payload")
    if not isinstance(p, Mapping):
        raise InvalidEndpoint("payload 가 없다")
    gateway_id = p.get("gateway_id")
    if not isinstance(gateway_id, str) or not _INSTANCE_ID.match(gateway_id):
        raise InvalidEndpoint("gateway_id 형식이 틀렸다")
    if doc.get("source_id") != gateway_id:
        raise InvalidEndpoint("봉투 source_id 와 gateway_id 가 다르다")
    ip = p.get("public_ip")
    try:
        if not isinstance(ip, str):
            raise ValueError
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        raise InvalidEndpoint("public_ip 가 IPv4 주소가 아니다") from None
    if addr.is_unspecified or addr.is_multicast:
        raise InvalidEndpoint("public_ip 가 접속할 수 있는 주소가 아니다")
    fingerprint = p.get("tls_public_key_sha256")
    if not is_fingerprint(fingerprint):
        raise InvalidEndpoint("tls_public_key_sha256 형식이 틀렸다")
    registered_at = p.get("registered_at")
    if isinstance(registered_at, bool) or not isinstance(registered_at, int) or registered_at < 0:
        raise InvalidEndpoint("registered_at 이 없다")
    ports = p.get("ports")
    if not isinstance(ports, list):
        raise InvalidEndpoint("ports 가 없다")
    control: set[int] = set()
    video: dict[str, int] = {}
    for item in ports:
        if not isinstance(item, Mapping):
            raise InvalidEndpoint("ports 항목 형식이 틀렸다")
        role, public_port = item.get("role"), _port(item.get("public_port"))
        if role == "CONTROL":
            control.add(public_port)
        elif role == "VIDEO":
            cam = item.get("camera_id")
            if not isinstance(cam, str) or not _CAMERA_ID.match(cam):
                raise InvalidEndpoint("VIDEO camera_id 형식이 틀렸다")
            if cam in video:
                raise InvalidEndpoint("VIDEO camera_id 가 두 번 있다")
            video[cam] = public_port
        else:
            raise InvalidEndpoint("ports.role 은 CONTROL 이나 VIDEO 다")
    if len(control) != 1:
        raise InvalidEndpoint("CONTROL 포트가 하나여야 한다")
    if not video:
        raise InvalidEndpoint("VIDEO 포트가 없다")
    pod_id = p.get("pod_id")
    return Tier0Endpoint(
        gateway_id=gateway_id,
        public_ip=str(addr),
        control_port=next(iter(control)),
        video_ports=dict(video),
        tls_public_key_sha256=fingerprint,
        registered_at=registered_at,
        pod_id=pod_id if isinstance(pod_id, str) else None,
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가지 않는다. 따라가면 기기 토큰이 다른 호스트·평문 http 로 실린다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class Ec2EndpointLookup:
    """EC2 `GET /api/endpoints/tier0`(기기 토큰). fetch() 는 Tier0Endpoint 나 EndpointUnavailable."""

    def __init__(
        self,
        base_url: str,
        *,
        device_token: str,
        ssl_context: ssl.SSLContext | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("SIMAI_EC2_BASE_URL 이 올바른 주소가 아니다")
        if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
            raise ValueError("엔드포인트 조회는 HTTPS 만 쓴다: 루프백 시험 서버가 아니면 https 주소여야 한다")
        if not device_token:
            raise ValueError("기기 토큰이 없다(.env 의 SIMAI_DEVICE_TOKEN)")
        self._url = base_url.rstrip("/") + LOOKUP_PATH
        self._token = device_token
        self._timeout_s = timeout_s
        # 인증서 검증을 끄지 않는다(EC2 는 공인 HTTPS 인증서, 명세 14)
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def __repr__(self) -> str:  # 토큰을 찍지 않는다
        return f"Ec2EndpointLookup(url={self._url!r}, device_token=***)"

    def fetch(self) -> Tier0Endpoint:
        request = urllib.request.Request(
            self._url,
            method="GET",
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._token}"},
        )
        try:
            with self._opener.open(request, timeout=self._timeout_s) as resp:
                status, raw = int(resp.status), resp.read()
        except urllib.error.HTTPError as e:
            with e:
                status = int(e.code)
            if status == 404:
                raise EndpointNotRegistered("EC2 에 등록된 Tier 0 엔드포인트가 없다 (404)") from None
            if status in (401, 403):
                raise EndpointAuthError(f"엔드포인트 조회 인증 실패 ({status}). 기기 토큰을 확인한다") from None
            raise EndpointUnavailable(f"엔드포인트 조회 실패 (EC2 응답 {status})") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise EndpointUnavailable(f"엔드포인트 조회 실패 ({type(e).__name__}: {e})") from None
        if status != 200:
            raise EndpointUnavailable(f"엔드포인트 조회: 예상하지 못한 응답 {status}")
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InvalidEndpoint("엔드포인트 조회 응답이 JSON 이 아니다") from None
        return parse_endpoint(doc)


@dataclass(frozen=True)
class ControlTarget:
    """이번에 접속할 I0 제어 채널: 주소와, 서버 인증서 공개키가 같아야 하는 지문(고정 목록 안의 값)."""

    url: str
    spki_sha256: str


class EndpointResolver:
    """조회 결과 + 노트북 고정 지문 → ControlTarget. 지문이 고정 목록에 없으면 PinMismatchError(접속하지 않음)."""

    def __init__(
        self,
        fetch: Callable[[], Tier0Endpoint],
        pins: frozenset[str],
        *,
        control_path: str = DEFAULT_CONTROL_PATH,
    ) -> None:
        if not pins:
            raise ValueError("고정 지문이 없다(.env 의 SIMAI_TIER0_PINNED_SPKI_SHA256). 고정값 없이는 접속하지 않는다")
        if not control_path.startswith("/"):
            raise ValueError("제어 채널 경로는 '/' 로 시작한다")
        self._fetch = fetch
        self.pins = pins
        self._path = control_path

    def resolve(self) -> ControlTarget:
        endpoint = self._fetch()
        if endpoint.tls_public_key_sha256 not in self.pins:
            raise PinMismatchError("EC2 가 알려 준 Tier 0 공개키 지문이 노트북 고정값과 다르다. 접속하지 않는다")
        return ControlTarget(endpoint.control_url(self._path), endpoint.tls_public_key_sha256)
