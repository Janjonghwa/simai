"""Tier 0 TLS 공개키 고정(명세 12.7 서버 확인, 14 TLS 서버 확인, 계약 tier0_endpoint.tls_public_key_sha256).

Tier 0 TLS 키는 한 번 만들어 Network Volume 비밀 폴더에 두고(자체 서명), Pod 가 바뀌면 공개 IP 가 바뀐다. 그래서 공인 CA
체인·호스트 이름으로는 서버를 확인할 수 없고, 노트북은 미리 고정한 공개키 지문(SubjectPublicKeyInfo DER 의 SHA-256)만
신뢰한다. TLS 핸드셰이크가 서버의 비밀 키 소유를 증명하므로, 핸드셰이크가 끝난 연결에서 서버 인증서의 공개키 지문이
고정값과 같으면 그 서버다.

- `pinned_client_context()`: 공개키 고정 전용 TLS 문맥. CA 파일이 없으면 체인 검증 대신 고정 지문 검사로 서버를 확인한다.
  이 문맥은 `PinnedPeerVerifier` 없이 쓰이지 않는다(transport 가 검사 없이 연결하지 않게 막는다).
- `PinnedPeerVerifier`: TLS 핸드셰이크 직후, 기기 토큰을 싣는 HTTP 요청을 보내기 전에 부른다. 지문이 다르면 예외.
- 인증서 검증을 끄고 아무 서버에나 붙는 경로는 없다.
"""

from __future__ import annotations

import hashlib
import re
import ssl
from collections.abc import Iterable

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


class PinMismatchError(ssl.SSLError):
    """서버 공개키 지문이 고정값과 다르다. 접속하지 않는다(토큰을 보내기 전에 끊는다)."""


def _der_tlv(buf: bytes, i: int) -> tuple[int, int, int]:
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
    """X.509 인증서(DER)의 SubjectPublicKeyInfo(DER, 태그 포함). ai/services/tier0/endpoint_registration.py 와 같은 계산."""
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
    """서버 인증서(DER)의 공개키 지문 `sha256:<64 hex>`."""
    return "sha256:" + hashlib.sha256(spki_der_from_cert_der(cert_der)).hexdigest()


def is_fingerprint(value: object) -> bool:
    return isinstance(value, str) and bool(_FINGERPRINT.match(value))


def parse_pins(text: str | Iterable[str] | None) -> frozenset[str]:
    """고정 지문 목록. 쉼표로 여러 개(키 교체 기간). 형식이 틀리면 ValueError(값은 문장에 넣지 않는다)."""
    items = text.split(",") if isinstance(text, str) else list(text or [])
    pins = set()
    for raw in items:
        value = raw.strip().lower()
        if not value:
            continue
        if not _FINGERPRINT.match(value):
            raise ValueError("고정 지문은 'sha256:<소문자 hex 64자>' 형식이어야 한다 (SIMAI_TIER0_PINNED_SPKI_SHA256)")
        pins.add(value)
    return frozenset(pins)


def pinned_client_context(ca_file: str | None = None) -> ssl.SSLContext:
    """공개키 고정 연결용 TLS 문맥(TLS 1.2 이상).

    - ca_file 이 없으면: Tier 0 인증서는 자체 서명이고 IP 가 Pod 마다 바뀌므로 체인·호스트 이름 검사를 하지 않고,
      핸드셰이크 직후 `PinnedPeerVerifier` 가 서버 공개키를 고정값과 비교해 확인한다(그 전에는 아무것도 보내지 않는다).
    - ca_file(팀 사설 CA)이 있으면: 체인 검증을 켜고(CERT_REQUIRED), 지문 검사도 함께 한다. 호스트 이름은 Pod 마다
      바뀌는 IP 라 고정 지문이 대신한다.
    """
    if ca_file:
        ctx = ssl.create_default_context(cafile=ca_file)
        ctx.check_hostname = False
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class PinnedPeerVerifier:
    """TLS 연결의 서버 공개키 지문이 expected 와 같은지 본다. expected 는 고정 목록 안의 값이어야 한다."""

    def __init__(self, expected: str, pins: frozenset[str]) -> None:
        if not pins:
            raise ValueError("고정 지문이 없다. 미리 고정한 Tier 0 공개키 지문 없이는 접속하지 않는다")
        if expected not in pins:
            raise PinMismatchError("EC2 가 알려 준 Tier 0 공개키 지문이 노트북 고정값과 다르다")
        self.expected = expected

    def __call__(self, ssl_object: ssl.SSLObject | ssl.SSLSocket) -> None:
        der = ssl_object.getpeercert(binary_form=True)
        if not der:
            raise PinMismatchError("서버 인증서가 없다")
        try:
            actual = spki_sha256(der)
        except ValueError as e:
            raise PinMismatchError(f"서버 인증서를 해석하지 못했다: {e}") from e
        if actual != self.expected:
            raise PinMismatchError("서버 공개키 지문이 고정값과 다르다. 접속하지 않는다")
