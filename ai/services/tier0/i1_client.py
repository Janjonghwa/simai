"""I1 WebSocket 클라이언트 (RunPod Tier 0 ↔ EC2): wss 연결, 재연결, 봉투, 송신 대기열 (명세 12.1·12.2·12.3, 14, 3.12).

계약: contracts/messages/envelope.schema.json 과 I1 메시지 스키마. 이 모듈은 연결과 송신 순서만 맡는다. 무엇을 언제
보낼지는 i1_publisher, 받은 메시지를 어디로 넘길지는 i1_dispatch 가 정한다(조립만, ai/CONVENTION.md).

연결과 보안(14장)
- wss 만 쓴다. 평문 ws 는 받지 않는다. 인증서 검증(체인·호스트 이름)을 끄는 경로가 없다. 사설 CA 는 CA 파일로만 더한다.
- Tier 0 토큰은 업그레이드 요청의 Authorization 헤더에만 싣는다. I1 주소에는 query·fragment·계정을 받지 않고, 주소
  어디에든 토큰이(퍼센트 인코딩한 꼴 포함) 들어 있으면 설정 단계에서 거부한다.
- 토큰은 로그·예외 문장·repr·last_error 에 넣지 않는다. 연결 실패뿐 아니라 연결 뒤 수신·송신 오류(서버가 보낸 close
  reason 등)도 저장하거나 다시 던지기 전에 등록한 비밀 값을 가린다. websockets 는 DEBUG 로그에 요청 헤더를 그대로
  찍으므로 연결 로거에 가림 필터를 단다(DEBUG 를 켜도 토큰이 나가지 않는다).
- 환경 프록시를 따르지 않는다(proxy=None). Pod 에서 EC2 로 바로 붙는다.
- TLS 상태는 한 번에 한 스레드만 만진다(serial_tls.SerialTLSSocket, S15P31A101-175). websockets 동기 연결은 수신 스레드가
  recv 에 머무는 동안 다른 스레드가 send 하므로, ssl.SSLSocket 이면 서버의 TLS 1.3 세션 티켓 처리와 업그레이드 요청 쓰기가
  한 SSL 객체에서 겹쳐 요청이 깨지거나 프로세스가 죽는다. 메모리 BIO + SSLObject 를 잠금으로 감싸고 소켓 입출력은 잠금 밖에서 한다.

재연결
- 연결 실패·끊김마다 지수 백오프(endpoint_registration.Backoff: 1s·2s·4s… 상한 30s, 실패 횟수가 아무리 커도 상한으로
  계속)로 다시 붙는다. 연결이 stable_after_s 이상 유지된 뒤 끊기면 실패 횟수를 0 으로 되돌린다. 바로 끊기는 연결은
  간격이 계속 는다.
- 송신 진행 감시: send 하나가 send_timeout_s 를 넘기면(EC2 가 읽지 않아 TCP 창이 참) 감시 스레드가 소켓을 shutdown 해
  막힌 send 를 풀고 다시 붙는다. 그렇게 끊긴 연결은 오래 붙어 있었어도 '안정 연결'로 보지 않는다(실패 횟수를 되돌리지
  않아 간격이 계속 는다 — 읽지 않는 상대에게 재연결 폭주를 막는다). websockets 의 close·ping 은 send 와 같은 잠금을 잡으므로
  끊을 때는 소켓을 먼저 shutdown 하고 close 는 별도 스레드에서 한다(close(timeout_s) 가 막힌 send 에 붙잡히지 않는다).
  Windows 는 shutdown 에 더해 그 핸들의 대기 입출력을 취소(CancelIoEx)하고, 핸들은 원래 닫기 경로만 닫는다(abort_connection).
- 연결 직후 on_connected 훅(연결 스레드)을 모두 부른 뒤 연결을 연다. 송신 스레드는 새 연결(세대)을 처음 볼 때 어떤
  송신보다 먼저 on_writer_open 훅(outbox.reset 등)을 부른다. 송신 스레드가 pump 를 부르는 유일한 스레드라 reset 이
  이전 pump 와 겹치지 않는다.
- 첫 메시지 관문(first_message_type="health"): 새 연결에서는 그 종류가 나갈 때까지 다른 송신(이벤트 대기열·world_state·
  pump)을 열지 않는다. health 를 아직 만들 수 없으면(payload 를 모름) 만들 수 있을 때까지 기다린다(지어내지 않음).

송신(한 연결에 보내는 스레드는 송신 스레드 하나. websockets 동기 연결은 동시 send 를 허용하지 않는다)
- 연결 고정(LinkRef): pump 의 배치는 시작할 때 잡은 연결·세대에만 보낸다(send_on). 그사이 연결이 바뀌면 남은 메시지는
  보내지 않고 SendFailed → outbox 는 TransportError 로 멈춘다. 이전 연결의 배치가 새 연결로 이어지지 않는다.
- 최신 칸(latest): world_state·health. 아직 못 보낸 것을 새것이 밀어내면 버리고 센다(dropped). 끊긴 동안 들어온 것은
  센 뒤 버린다(discarded_offline). 끊기면 칸을 비우고 칸에 남아 있던 것과 보내다 연결이 끊긴 것을 센다
  (lost_on_disconnect, 6.5 "그 구간의 world_state 는 데이터 없음"). 집계는 연결을 넘어 누적한다(재연결로 0 이 되지 않음).
  새 연결은 이전 연결에서 보내던 최신 칸 메시지의 성패가 정해질 때까지(send_timeout_s + 여유 상한) 기다린 뒤 on_connected
  훅(첫 health)을 부른다. 늦게 돌아온 send 실패가 첫 health 의 버림 수에서 빠지지 않는다.
  world_state 버림 수는 drop_counts 로 꺼내 health 1.5.0 선택 필드 `ec2_world_state_drops` 로 싣는다
  (i1_publisher.I1Publisher.world_state_drops·with_world_state_drops, S15P31A101-170).
- 신뢰 대기열(reliable): config_ack·calibration_update·event_frame_ready·tier0_endpoint·event_close_result·
  event_escalate_result. 끊겨도 보관하고 버리지 않는다.
  상한(reliable_limit)에 차면 enqueue 가 QueueFull 을 내 호출자가 붙들고 다시 넣는다(backpressure). 보내다 실패하면
  맨 앞에 되돌린다(같은 msg_id, EC2 가 중복을 거른다, 12.1). replace_key 가 같은 대기 메시지는 새것으로 바꾼다.
- 텔레메트리 대기열(telemetry): observation_summary. 상한을 넘으면 가장 오래된 것을 버리고 센다(telemetry_overflow).
- risk_event_transition 은 outbox 가, event_state_snapshot 은 outbox 재전송을 마친 pump 가 연결 고정으로 직접 보낸다.
  대기열로는 받지 않는다(순서 보장. i1_publisher.attach_outbox).
- 우선순위: health → 신뢰 → 텔레메트리 → world_state. pump 는 pump_interval_s 마다 송신 스레드에서 부른다.

시계: sent_at 은 주입한 Clock(UTC ms), 간격·백오프는 주입한 단조 시계로 잰다. 현재 시각을 직접 읽지 않는다.
"""

from __future__ import annotations

import ctypes
import functools
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from services.tier0.endpoint_registration import DEFAULT_TIER0_ID, Backoff
from services.tier0.health import HEALTH_SCHEMA_VERSION
from services.tier0.serial_tls import SerialTLSContext, SerialTLSSocket
from simai_ai.risk import Clock
from simai_ai.tracking.world_state import WORLD_STATE_SCHEMA_VERSION

log = logging.getLogger("services.tier0.i1")

CONTRACTS_VERSION = "2.0.0"
"""메시지별 버전이 없을 때 쓰는 봉투 schema_version (contracts/README.md 현재 계약 버전)"""

# 봉투 schema_version: 그 메시지 계약이 마지막으로 바뀐 버전 (contracts/README.md, contracts/examples).
# config_ack(ConfigSync)·tier0_endpoint(EndpointRegistrar)·risk_event_transition(outbox)은 만드는 모듈이 정한다.
MESSAGE_SCHEMA_VERSIONS: Mapping[str, str] = {
    "event_frame_ready": "1.1.0",  # 1.1.0: 프레임 선택 local_copy (-408)
    "world_state": WORLD_STATE_SCHEMA_VERSION,  # 2.1.0: 분할 출처 split_from (-201). 만드는 모듈(tracking.world_state)이 정한다
    "health": HEALTH_SCHEMA_VERSION,  # 2.1.0: 2.0.0(-179) + tier0_warnings LIVE_MARKER_WRITE_FAILED(-197). health.py 가 정한다
    "observation_summary": "1.3.0",  # -155
    "calibration_update": "2.1.0",  # World 계보·카메라 자세 추가 (-381, Lead 승인 필요)
    "event_close_result": "1.1.0",  # 운영자 종료 결과, 1.1.0 JOURNAL_UNAVAILABLE (-200)
    "event_escalate_result": "1.0.0",  # 운영자 상향 결과 (계약 -206, Tier 0 처리 -272)
}

# I1 위로(Tier 0 → EC2) 메시지 (명세 12.2). 다른 종류는 보내지 않는다.
UPSTREAM_TYPES = frozenset(
    {
        "world_state",
        "health",
        "observation_summary",
        "calibration_update",
        "risk_event_transition",
        "event_state_snapshot",
        "config_ack",
        "event_frame_ready",
        "tier0_endpoint",
        "event_close_result",
        "event_escalate_result",
    }
)
LATEST_KINDS = ("health", "world_state")  # 최신 칸
INFLIGHT_WAIT_GRACE_S = 0.5
"""이전 연결의 최신 칸 송신 성패를 기다리는 여유(송신 감시 주기 0.25 s 이상)"""
RELIABLE_TYPES = frozenset(
    {"config_ack", "calibration_update", "event_frame_ready", "tier0_endpoint", "event_close_result", "event_escalate_result"}
)
TELEMETRY_TYPES = frozenset({"observation_summary"})

DEFAULT_I1_PATH = "/ws/tier0"
"""SIMAI_EC2_I1_URL 이 없을 때 SIMAI_EC2_BASE_URL 에 붙이는 경로의 기본값(SIMAI_EC2_I1_PATH 로 바꾼다). 서버 확정 전 기본안이다"""


# ---------------------------------------------------------------------------- 비밀 값 가림


class _RedactAuthorization(logging.Filter):
    """websockets DEBUG 로그의 요청 헤더 줄(`> Authorization: Bearer <토큰>`)과 등록한 비밀 값을 가린다."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add_secret(self, secret: str) -> None:
        if secret:
            with self._lock:
                self._secrets.update(_secret_forms(secret))

    @property
    def secrets(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._secrets, key=len, reverse=True))

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - 형식이 깨진 기록은 통째로 가린다
            record.msg, record.args = "(로그 형식 오류, 가림)", ()
            return True
        if "authorization" in text.lower():
            record.msg, record.args = "> Authorization: ***", ()
        else:
            safe = redact(text, self.secrets)
            if safe != text:
                record.msg, record.args = safe, ()
        return True


def _secret_forms(secret: str) -> set[str]:
    """비밀 값과 그 퍼센트 인코딩 꼴(주소·오류 문장에 실릴 수 있는 모양)."""
    return {s for s in (secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)) if s}


def redact(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


_redactor = _RedactAuthorization()
# websockets 연결 로그는 이 로거로 받는다(DEBUG 를 켜도 토큰이 찍히지 않게)
_ws_log = logging.getLogger("services.tier0.i1.websockets")
_ws_log.addFilter(_redactor)
log.addFilter(_redactor)


def safe_error(e: BaseException) -> str:
    """예외 하나를 '종류: 문장' 으로, 등록한 비밀 값을 가려서."""
    return redact(f"{type(e).__name__}: {e}", _redactor.secrets)


# ---------------------------------------------------------------------------- 설정


class I1ConfigError(ValueError):
    """I1 설정을 모르거나 틀렸다. 문장에 비밀 값이 없다."""


def validate_url(url: str, token: str | None = None) -> None:
    """wss·호스트 필수, 계정·query·fragment 불가, 토큰(인코딩 꼴 포함)이 주소 어디에도 없어야 한다."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "wss":
        raise I1ConfigError("I1 은 wss 만 쓴다 (평문 ws 불가, 명세 14)")
    if not parts.hostname:
        raise I1ConfigError("I1 주소에 호스트가 없다")
    if parts.username or parts.password:
        raise I1ConfigError("I1 주소에 계정을 넣지 않는다 (토큰은 SIMAI_TIER0_TOKEN)")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise I1ConfigError("I1 주소에 query·fragment 를 넣지 않는다 (토큰은 Authorization 헤더로만)")
    if token:
        decoded = {url, urllib.parse.unquote(url), urllib.parse.unquote_plus(url)}
        if any(form in text for form in _secret_forms(token) for text in decoded):
            raise I1ConfigError("I1 주소에 토큰이 들어 있다 (토큰은 Authorization 헤더로만)")


@dataclass(frozen=True)
class I1Settings:
    """.env 에서 읽은 I1 접속 설정. 만들 때 주소를 검사하고, token 은 repr 에 나오지 않는다."""

    url: str
    token: str = field(repr=False)
    source_id: str = DEFAULT_TIER0_ID
    ca_file: str | None = None

    def __post_init__(self) -> None:
        if not self.token or not self.token.strip():
            raise I1ConfigError("SIMAI_TIER0_TOKEN 이 비었다 (.env)")
        validate_url(self.url, self.token)

    def __repr__(self) -> str:
        return f"I1Settings(url={self.url!r}, token=***, source_id={self.source_id!r}, ca_file={self.ca_file!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> I1Settings:
        """환경 변수(.env, 비밀 값은 커밋하지 않는다):

        SIMAI_EC2_I1_URL   I1 WebSocket 주소 (wss, query 없이). 없으면 SIMAI_EC2_BASE_URL(https) + SIMAI_EC2_I1_PATH
        SIMAI_EC2_I1_PATH  선택. SIMAI_EC2_BASE_URL 에 붙이는 경로 (기본 DEFAULT_I1_PATH, 서버 확정 전 임시 기본값)
        SIMAI_TIER0_TOKEN  Tier 0 토큰 (비밀, Authorization: Bearer)
        SIMAI_TIER0_ID     봉투 source_id (기본 tier0-01, 이벤트 gateway_id 와 같다)
        SIMAI_EC2_CA_FILE  선택. EC2 인증서를 사설 CA 로 발급했을 때 그 CA PEM (시스템 신뢰 저장소에 더한다)
        """
        env = os.environ if env is None else env
        url = (env.get("SIMAI_EC2_I1_URL") or "").strip()
        if not url:
            base = (env.get("SIMAI_EC2_BASE_URL") or "").strip()
            parts = urllib.parse.urlsplit(base)
            if not base or parts.scheme != "https" or not parts.hostname:
                raise I1ConfigError("SIMAI_EC2_I1_URL(wss) 또는 SIMAI_EC2_BASE_URL(https) 이 필요하다 (.env)")
            if parts.query or parts.fragment:
                raise I1ConfigError("SIMAI_EC2_BASE_URL 에 query·fragment 를 넣지 않는다")
            path = (env.get("SIMAI_EC2_I1_PATH") or "").strip() or DEFAULT_I1_PATH
            if not path.startswith("/"):
                raise I1ConfigError("SIMAI_EC2_I1_PATH 는 / 로 시작한다")
            url = urllib.parse.urlunsplit(("wss", parts.netloc, parts.path.rstrip("/") + path, "", ""))
        token = env.get("SIMAI_TIER0_TOKEN") or ""
        source_id = (env.get("SIMAI_TIER0_ID") or DEFAULT_TIER0_ID).strip()
        ca_file = (env.get("SIMAI_EC2_CA_FILE") or "").strip() or None
        return cls(url=url, token=token, source_id=source_id, ca_file=ca_file)


# ---------------------------------------------------------------------------- 봉투


class Envelope:
    """공통 봉투(12.1)를 만든다. msg_id 는 UUID, sent_at 은 주입 시계의 UTC ms, source_id 는 Tier 0 인스턴스 ID."""

    def __init__(
        self,
        source_id: str,
        clock: Clock,
        *,
        new_msg_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        versions: Mapping[str, str] = MESSAGE_SCHEMA_VERSIONS,
    ) -> None:
        if not source_id:
            raise ValueError("source_id 가 비었다")
        self.source_id = source_id
        self._clock = clock
        self._new_msg_id = new_msg_id
        self._versions = dict(versions)

    @property
    def clock(self) -> Clock:
        """sent_at 을 만드는 주입 시계 (i1_publisher 가 world_state 버림 누적 시작 시각에도 쓴다)."""
        return self._clock

    def make(
        self,
        msg_type: str,
        payload: Mapping[str, Any],
        *,
        schema_version: str | None = None,
        msg_id: str | None = None,
    ) -> dict[str, Any]:
        """봉투를 씌운다. msg_id 를 주면 그 값을 쓴다(판정 근거 evidence_ref 로 미리 정한 world_state msg_id, -171)."""
        if msg_type not in UPSTREAM_TYPES:
            raise ValueError(f"I1 위로 보내는 메시지가 아니다: {msg_type}")
        if not isinstance(payload, Mapping):
            raise TypeError("payload 는 객체다")
        return {
            "schema_version": schema_version or self._versions.get(msg_type, CONTRACTS_VERSION),
            "msg_type": msg_type,
            "msg_id": msg_id if msg_id is not None else self._new_msg_id(),
            "sent_at": int(self._clock.now_ms()),
            "source_id": self.source_id,
            "payload": dict(payload),
        }


# ---------------------------------------------------------------------------- 연결


class Connection(Protocol):
    """연결 하나. websockets.sync.client.ClientConnection 이 이 모양이다."""

    def send(self, message: str) -> None: ...

    def __iter__(self) -> Iterator[str | bytes]: ...

    def close(self) -> None: ...


class Connector(Protocol):
    def connect(self) -> Connection:
        """새 연결. 실패하면 예외(클라이언트가 문장을 가려서 저장한다)."""
        ...


class I1ConnectError(ConnectionError):
    """연결 실패. 문장에 토큰이 없다."""


def client_ssl_context(ca_file: str | None = None) -> ssl.SSLContext:
    """시스템 신뢰 저장소 + (선택) 사설 CA. 체인·호스트 이름 검증을 켠 컨텍스트만 만든다.

    TLS 1.2 재협상은 받지 않는다(OP_NO_RENEGOTIATION). 받아들이면 쓰기 중에 읽기가 필요해지는데, SerialTLSSocket 은 그 상태를
    지원하지 않고 끊는다. 재협상 요청은 경고로 거절하고 연결은 그대로 쓴다.
    """
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_RENEGOTIATION
    return context


def require_verifying(context: ssl.SSLContext) -> None:
    """인증서 검증을 끈 컨텍스트는 쓰지 않는다(명세 14, 12.7 "인증서 검증을 끄는 구현 금지")."""
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise I1ConfigError("I1 TLS 는 인증서 검증(CERT_REQUIRED + 호스트 이름 확인)을 끌 수 없다")


class WebSocketConnector:
    """websockets 동기 클라이언트로 wss 에 붙는다. 토큰은 Authorization 헤더에만 싣는다. TLS 는 SerialTLSSocket 으로 한다
    (연결의 socket 이 SerialTLSSocket 이다. 검증은 넘겨받은 SSLContext 그대로).

    ping_interval_s·ping_timeout_s: websockets keepalive(Pong 이 없으면 끊는다). I1 아래로는 상시 하트비트가 계약에 없어
    진행 판정(watchdog)은 서버와 맞출 일이다(MR 남은 일).
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        ssl_context: ssl.SSLContext | None = None,
        ca_file: str | None = None,
        open_timeout_s: float = 10.0,
        ping_interval_s: float = 10.0,
        ping_timeout_s: float = 10.0,
        max_size: int = 4 * 1024 * 1024,
    ) -> None:
        if not token or not token.strip():
            raise I1ConfigError("Tier 0 토큰이 비었다 (.env 의 SIMAI_TIER0_TOKEN)")
        validate_url(url, token)
        context = ssl_context if ssl_context is not None else client_ssl_context(ca_file)
        require_verifying(context)
        self._url = url
        self._token = token
        self._ssl = context
        self._open_timeout_s = open_timeout_s
        self._ping_interval_s = ping_interval_s
        self._ping_timeout_s = ping_timeout_s
        self._max_size = max_size
        _redactor.add_secret(token)

    def __repr__(self) -> str:  # 주소는 검사를 통과해 토큰이 없다
        return f"WebSocketConnector(url={self._url!r}, token=***)"

    @classmethod
    def from_settings(cls, settings: I1Settings, **kwargs: Any) -> WebSocketConnector:
        return cls(settings.url, settings.token, ca_file=settings.ca_file, **kwargs)

    def connect(self) -> Connection:
        from websockets.sync.client import connect

        require_verifying(self._ssl)
        try:
            # 17.1 부터 connect() 는 with 로 쓰라고 경고한다. __enter__ 는 연결을 그대로 돌려준다(이전 판도 같다)
            return connect(
                self._url,
                ssl=SerialTLSContext(self._ssl),  # type: ignore[arg-type]  # wrap_socket 만 쓴다(serial_tls)
                additional_headers={"Authorization": f"Bearer {self._token}"},
                open_timeout=self._open_timeout_s,
                ping_interval=self._ping_interval_s,
                ping_timeout=self._ping_timeout_s,
                max_size=self._max_size,
                compression=None,
                proxy=None,
                logger=_ws_log,
            ).__enter__()
        except Exception as e:  # 문장에서 토큰을 지우고 원인 종류만 남긴다
            raise I1ConnectError(safe_error(e)) from None


# ---------------------------------------------------------------------------- 클라이언트


class SendFailed(ConnectionError):
    """연결이 없거나 바뀌었거나 보내다 끊겼다. 보낸 쪽(outbox 등)은 다시 보낼 책임이 있다. 문장에 토큰이 없다."""


class Unserializable(ValueError):
    """JSON 으로 만들 수 없는 메시지(NaN 등). 보내지 않고 버린다."""


class QueueFull(RuntimeError):
    """신뢰 대기열이 찼다. 메시지를 버리지 않았다. 호출자가 붙들고 다시 넣는다."""


@dataclass
class _Inflight:
    """최신 칸에서 꺼내 보내는 중인 메시지 하나. counted: 실패가 확정돼 이미 버림으로 셌다(send 결과가 와도 다시 세지 않음)."""

    conn: Connection
    kind: str
    counted: bool = False


@dataclass(frozen=True)
class LinkRef:
    """송신이 열린 연결 하나와 그 세대. send_on 은 이 연결이 아직 현재일 때만 보낸다."""

    conn: Connection
    generation: int


@dataclass
class I1Stats:
    """송신 집계(연결을 넘어 누적). dropped: 최신 칸에서 못 보내고 새것에 밀려 버린 수. discarded_offline: 끊긴 동안
    들어와 버린 수. lost_on_disconnect: 끊길 때 최신 칸에 남아 있던 것과 최신 칸에서 꺼내 보내다 연결이 끊긴 것.

    world_state 의 셋은 I1Client.drop_counts 로 health 1.5.0 `ec2_world_state_drops` 에 실린다(superseded = dropped,
    disconnected = discarded_offline + lost_on_disconnect).
    """

    connects: int = 0
    disconnects: int = 0
    connect_failures: int = 0
    sent: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=lambda: {k: 0 for k in LATEST_KINDS})
    discarded_offline: dict[str, int] = field(default_factory=lambda: {k: 0 for k in LATEST_KINDS})
    lost_on_disconnect: dict[str, int] = field(default_factory=lambda: {k: 0 for k in LATEST_KINDS})
    inflight_wait_timeouts: int = 0
    """새 연결을 열기 전 이전 연결의 최신 칸 송신 성패를 send_timeout_s + 여유 안에 확정하지 못한 횟수"""
    telemetry_overflow: int = 0
    reliable_full: int = 0
    unserializable: int = 0
    send_failures: int = 0
    send_timeouts: int = 0
    stale_link_sends: int = 0
    received: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "connects": self.connects,
            "disconnects": self.disconnects,
            "connect_failures": self.connect_failures,
            "sent": dict(self.sent),
            "dropped": dict(self.dropped),
            "discarded_offline": dict(self.discarded_offline),
            "lost_on_disconnect": dict(self.lost_on_disconnect),
            "inflight_wait_timeouts": self.inflight_wait_timeouts,
            "telemetry_overflow": self.telemetry_overflow,
            "reliable_full": self.reliable_full,
            "unserializable": self.unserializable,
            "send_failures": self.send_failures,
            "send_timeouts": self.send_timeouts,
            "stale_link_sends": self.stale_link_sends,
            "received": self.received,
        }


OnMessage = Callable[[str | bytes], None]
Hook = Callable[[], None]
Pump = Callable[[], Any]
_Item = tuple[str | None, dict[str, Any]]


class I1Client:
    """I1 연결 관리자. start() 뒤 연결 스레드(수신 포함)와 송신 스레드가 돈다.

    on_message: 받은 원문(텍스트 프레임)마다 연결 스레드에서 부른다(i1_dispatch.I1Dispatcher.handle_raw).
    first_message_type: 새 연결에서 이 종류가 나가기 전에는 다른 송신을 열지 않는다(None 이면 바로 연다).
    """

    def __init__(
        self,
        connector: Connector,
        *,
        on_message: OnMessage,
        backoff: Backoff | None = None,
        stable_after_s: float = 30.0,
        reliable_limit: int = 1000,
        telemetry_limit: int = 100,
        pump_interval_s: float = 0.05,
        send_timeout_s: float = 10.0,
        first_message_type: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if reliable_limit < 1 or telemetry_limit < 1 or pump_interval_s <= 0 or stable_after_s < 0 or send_timeout_s <= 0:
            raise ValueError("reliable_limit·telemetry_limit·pump_interval_s·stable_after_s·send_timeout_s 값이 올바르지 않다")
        if first_message_type is not None and first_message_type not in LATEST_KINDS:
            raise ValueError("first_message_type 은 최신 칸 종류(health)다")
        self._connector = connector
        self._on_message = on_message
        self.backoff = backoff or Backoff()
        self._stable_after_s = stable_after_s
        self._send_timeout_s = send_timeout_s
        self._sending: tuple[float, Connection] | None = None  # 진행 중인 send 의 시작 시각과 연결
        self._timed_out: Connection | None = None  # 송신 시간 초과로 끊은 연결
        self._inflight: _Inflight | None = None  # 최신 칸에서 꺼내 보내는 중인 메시지(성패가 아직 정해지지 않음)
        self._reliable_limit = reliable_limit
        self._telemetry_limit = telemetry_limit
        self._pump_interval_s = pump_interval_s
        self._first = first_message_type
        self._mono = monotonic
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()
        self._closing = threading.Event()
        self._conn: Connection | None = None
        self._accepting = False  # 연결 훅이 도는 동안부터 최신 칸을 받는다(송신은 훅 뒤에 연다)
        self._generation = 0
        self._writer_generation = 0  # 송신 스레드가 열기 훅을 부른 세대
        self._gate_open = False  # 이 세대의 첫 메시지가 나갔다(또는 관문 없음)
        self._latest: dict[str, dict[str, Any] | None] = {k: None for k in LATEST_KINDS}
        self._reliable: deque[_Item] = deque()
        self._telemetry: deque[_Item] = deque()
        self._pumps: list[Pump] = []
        self._on_connected: list[Hook] = []
        self._on_disconnected: list[Hook] = []
        self._on_writer_open: list[Hook] = []
        self._threads: list[threading.Thread] = []
        self._failures = 0
        self.stats = I1Stats()
        self.last_error: str | None = None

    # --- 구성 -----------------------------------------------------------------------

    def add_on_connected(self, hook: Hook) -> None:
        """연결 직후, 연결을 열기 전에 연결 스레드에서 부른다(등록 순서대로)."""
        self._on_connected.append(hook)

    def add_on_disconnected(self, hook: Hook) -> None:
        self._on_disconnected.append(hook)

    def add_on_writer_open(self, hook: Hook) -> None:
        """송신 스레드가 새 연결을 처음 볼 때, 그 연결로 무엇이든 보내기 전에 부른다(outbox.reset 등)."""
        self._on_writer_open.append(hook)

    def add_pump(self, pump: Pump) -> None:
        """첫 메시지 관문이 열린 연결에서 송신 스레드가 pump_interval_s 마다 부른다(outbox.pump 등). 예외는 기록만."""
        self._pumps.append(pump)

    # --- 상태 -----------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """EC2 와 연결돼 있다(system_health 의 ec2_link_ok). 연결 훅이 도는 동안에도 True 다(첫 health 가 끊김으로
        보고되지 않게)."""
        with self._cond:
            return self._accepting

    @property
    def generation(self) -> int:
        """지금까지 연 연결 수(연결마다 1 씩 는다)."""
        with self._cond:
            return self._generation

    def drop_counts(self, kind: str) -> tuple[int, int]:
        """최신 칸 종류 하나의 누적 버림 수 (superseded, disconnected). 연결을 넘어 누적하고 재연결로 0 이 되지 않는다.

        superseded: 연결된 동안 보내기 전에 새것에 밀려 버린 수. disconnected: 끊긴 동안 들어와 버린 것 + 끊길 때 칸에
        남아 있던 것 + 보내다 연결이 끊긴 것. 직렬화 실패(코드 결함)는 넣지 않는다(stats.unserializable).
        """
        if kind not in LATEST_KINDS:
            raise ValueError(f"최신 칸 메시지가 아니다: {kind}")
        with self._cond:
            st = self.stats
            return st.dropped[kind], st.discarded_offline[kind] + st.lost_on_disconnect[kind]

    def queue_sizes(self) -> dict[str, int]:
        with self._cond:
            return {"reliable": len(self._reliable), "telemetry": len(self._telemetry)}

    def alive_threads(self) -> list[str]:
        """close() 뒤에도 끝나지 않은 스레드 이름(막힌 연결 등)."""
        return [t.name for t in self._threads if t.is_alive()]

    # --- 넣기 -----------------------------------------------------------------------

    def put_latest(self, message: dict[str, Any]) -> bool:
        """world_state·health 를 최신 칸에 넣는다. 못 보낸 이전 것을 밀어냈으면 True(버림 집계)."""
        kind = message.get("msg_type")
        if kind not in self._latest:
            raise ValueError(f"최신 칸 메시지가 아니다: {kind}")
        with self._cond:
            if not self._accepting:
                self.stats.discarded_offline[kind] += 1
                return False
            dropped = self._latest[kind] is not None
            if dropped:
                self.stats.dropped[kind] += 1
            self._latest[kind] = message
            self._cond.notify_all()
            return dropped

    def enqueue(self, message: dict[str, Any], *, replace_key: str | None = None) -> None:
        """신뢰·텔레메트리 대기열에 넣는다(끊겨도 보관). 신뢰 대기열이 차 있으면 QueueFull(버리지 않음).

        replace_key 가 같은 대기 메시지는 새것으로 바꾼다. risk_event_transition·event_state_snapshot·world_state·health 는
        받지 않는다(outbox·최신 칸 경로가 따로 있다).
        """
        kind = message.get("msg_type")
        if kind in RELIABLE_TYPES:
            queue = self._reliable
        elif kind in TELEMETRY_TYPES:
            queue = self._telemetry
        else:
            raise ValueError(f"대기열로 보내지 않는 종류다: {kind}")
        with self._cond:
            if replace_key is not None:
                kept = [item for item in queue if item[0] != replace_key]
                queue.clear()
                queue.extend(kept)
            if queue is self._reliable:
                if len(queue) >= self._reliable_limit:
                    self.stats.reliable_full += 1
                    raise QueueFull(f"I1 신뢰 대기열이 찼다({self._reliable_limit}). {kind} 를 버리지 않았다")
            else:
                while len(queue) >= self._telemetry_limit:
                    old = queue.popleft()
                    self.stats.telemetry_overflow += 1
                    log.warning("I1 텔레메트리 대기열이 상한(%d)을 넘어 가장 오래된 %s 를 버렸다",
                                self._telemetry_limit, old[1].get("msg_type"))
            queue.append((replace_key, message))
            self._cond.notify_all()

    def current_link(self) -> LinkRef:
        """송신이 열린 지금 연결(첫 메시지 관문 통과). 없으면 SendFailed."""
        with self._cond:
            if self._conn is None or not self._gate_open:
                raise SendFailed("I1 송신이 열린 연결이 없다")
            return LinkRef(self._conn, self._generation)

    def send_on(self, link: LinkRef, message: Mapping[str, Any]) -> None:
        """link 가 아직 현재 연결일 때만 그 연결로 보낸다. 바뀌었거나 끊기면 SendFailed(다른 연결로 보내지 않는다)."""
        with self._cond:
            current = self._conn is link.conn and self._generation == link.generation and self._gate_open
        if not current:
            self.stats.stale_link_sends += 1
            raise SendFailed("I1 연결이 바뀌었다")
        self._send(link.conn, message)

    # --- 시작·정지 -------------------------------------------------------------------

    def start(self) -> None:
        if self.alive_threads():
            raise RuntimeError("이전 스레드가 아직 살아 있다")
        if self._threads and not self._closing.is_set():
            raise RuntimeError("이미 시작했다")
        self._threads.clear()
        self._closing.clear()
        for name, target in (("i1-connection", self._run_connection), ("i1-writer", self._run_writer),
                             ("i1-send-watchdog", self._run_watchdog)):
            t = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(t)
            t.start()

    def close(self, timeout_s: float = 5.0) -> bool:
        """닫는다. 모든 스레드가 끝났으면 True. 끝나지 않은 스레드는 목록에 남긴다(alive_threads)."""
        self._closing.set()
        with self._cond:
            conn = self._conn
            self._cond.notify_all()
        if conn is not None:
            abort_connection(conn)  # 막힌 send 를 풀고, close 는 별도 스레드(여기서 기다리지 않는다)
        deadline = self._mono() + timeout_s
        for t in self._threads:
            t.join(max(0.0, deadline - self._mono()))
        alive = self.alive_threads()
        if alive:
            log.warning("I1 클라이언트 스레드가 제한 시간 안에 끝나지 않았다: %s", alive)
        return not alive

    def drop_connection(self) -> None:
        """지금 연결을 끊는다(시험·엔드포인트 교체용). 연결 스레드가 백오프 뒤 다시 붙는다."""
        with self._cond:
            conn = self._conn
        if conn is not None:
            abort_connection(conn)

    # --- 연결 스레드 -----------------------------------------------------------------

    def _run_connection(self) -> None:
        while not self._closing.is_set():
            try:
                conn = self._connector.connect()
            except Exception as e:  # noqa: BLE001 - 연결 실패는 백오프 뒤 다시
                self._failures += 1
                self.stats.connect_failures += 1
                self.last_error = safe_error(e)
                delay = self.backoff.delay(self._failures)
                log.warning("I1 연결 실패 (%s). %.1f 초 뒤 다시 붙는다", self.last_error, delay)
                self._closing.wait(delay)
                continue
            if self._closing.is_set():
                close_async(conn)
                break
            connected_at = self._mono()
            self._serve(conn)
            with self._cond:
                timed_out = self._timed_out is conn
            if self._mono() - connected_at >= self._stable_after_s and not timed_out:
                self._failures = 0  # 안정 연결 뒤 끊김: 첫 간격부터 다시(송신 시간 초과로 끊은 연결은 제외)
            if self._closing.is_set():
                break
            self._failures += 1
            delay = self.backoff.delay(self._failures)
            log.warning("I1 연결 끊김 (%s). %.1f 초 뒤 다시 붙는다", self.last_error, delay)
            self._closing.wait(delay)

    def _await_previous_latest_send(self) -> None:
        """이전 연결에서 최신 칸 메시지를 보내던 중이면 그 성패(실패면 버림 집계)가 정해질 때까지 기다린다.

        새 연결의 on_connected 훅이 만드는 첫 health 의 ec2_world_state_drops 에 이전 연결의 버림이 빠지지 않게 한다
        (health 1.5.0, S15P31A101-170).
        - 송신 감시가 그 연결을 시간 초과로 끊었으면(_timed_out) 실패가 확정된 것이라 기다리지 않고 바로 버림으로 센다.
          send 결과가 뒤에 와도 다시 세지 않는다(_Inflight.counted).
        - 그 밖에는(상대가 먼저 끊어 send 가 곧 돌아올 때) 결과를 기다리되 send_timeout_s + 여유(INFLIGHT_WAIT_GRACE_S)를
          넘기지 않는다. 그 사이 송신 감시가 끊으면 위처럼 바로 센다. 상한을 넘으면 기다리지 않고 연다(그 버림은 성패가
          정해진 뒤 다음 health 부터 실린다. 미리 버림으로 세지 않는다 — 나중에 성공하면 없는 버림을 지어낸 것이 된다).
        - close() 가 오면 바로 멈춘다.
        """
        deadline = self._mono() + self._send_timeout_s + INFLIGHT_WAIT_GRACE_S
        with self._cond:
            while self._inflight is not None and not self._closing.is_set():
                inflight = self._inflight
                if inflight.conn is self._timed_out:
                    if not inflight.counted:
                        inflight.counted = True
                        self.stats.lost_on_disconnect[inflight.kind] += 1
                    return
                remaining = deadline - self._mono()
                if remaining <= 0:
                    self.stats.inflight_wait_timeouts += 1
                    log.warning("I1 이전 연결의 송신 성패가 정해지지 않은 채 새 연결을 연다(첫 health 버림 수에 빠질 수 있다)")
                    return
                self._cond.wait(min(0.05, remaining))

    def _serve(self, conn: Connection) -> None:
        self._await_previous_latest_send()
        self.stats.connects += 1
        with self._cond:
            self._accepting = True
        for hook in self._on_connected:  # 연결을 열기 전에: 훅이 넣은 health 가 새 연결의 첫 메시지가 된다
            _call_quietly(hook, "on_connected")
        with self._cond:
            self._conn = conn
            self._generation += 1
            self._gate_open = self._first is None
            self._cond.notify_all()
        if self._closing.is_set():  # 훅이 도는 동안 close() 가 왔으면 close 는 이 연결을 못 봤다
            abort_connection(conn)
        log.info("I1 연결됨 (%d 번째)", self.stats.connects)
        try:
            for raw in conn:
                self.stats.received += 1
                try:
                    self._on_message(raw)
                except Exception:  # noqa: BLE001 - 처리 실패가 수신을 멈추지 않는다
                    log.exception("I1 수신 처리 실패")
        except Exception as e:  # noqa: BLE001 - 끊김(ConnectionClosed 등). close reason 의 비밀 값도 가린다
            self.last_error = safe_error(e)
        finally:
            with self._cond:
                if self._conn is conn:
                    self._conn = None
                self._accepting = False
                self._gate_open = False
                for k in LATEST_KINDS:  # 지난 상태는 다시 보내지 않는다(버린 수는 센다)
                    if self._latest[k] is not None:
                        self.stats.lost_on_disconnect[k] += 1
                    self._latest[k] = None
                self._cond.notify_all()
            close_async(conn)
            self.stats.disconnects += 1
            for hook in self._on_disconnected:
                _call_quietly(hook, "on_disconnected")

    # --- 송신 스레드 -----------------------------------------------------------------

    def _next(self) -> tuple[LinkRef, str, _Item] | None:
        """보낼 것 하나. 관문이 닫혀 있으면 첫 메시지 종류만. 그 밖에는 health → 신뢰 → 텔레메트리 → world_state."""
        conn = self._conn
        if conn is None or self._writer_generation != self._generation:
            return None
        link = LinkRef(conn, self._generation)
        if not self._gate_open:
            assert self._first is not None
            msg = self._latest[self._first]
            if msg is None:
                return None
            self._latest[self._first] = None
            self._inflight = _Inflight(link.conn, str(self._first))
            return link, "first", (None, msg)
        if self._latest["health"] is not None:
            msg, self._latest["health"] = self._latest["health"], None
            self._inflight = _Inflight(link.conn, "health")
            return link, "latest", (None, msg)
        if self._reliable:
            return link, "reliable", self._reliable.popleft()
        if self._telemetry:
            return link, "telemetry", self._telemetry.popleft()
        if self._latest["world_state"] is not None:
            msg, self._latest["world_state"] = self._latest["world_state"], None
            self._inflight = _Inflight(link.conn, "world_state")
            return link, "latest", (None, msg)
        return None

    def _run_writer(self) -> None:
        next_pump = self._mono()
        while not self._closing.is_set():
            with self._cond:
                opening = self._conn is not None and self._writer_generation != self._generation
                generation = self._generation
            if opening:
                # 새 연결: 이 스레드가 pump 를 부르는 유일한 스레드라 reset 이 이전 pump 와 겹치지 않는다
                for hook in self._on_writer_open:
                    _call_quietly(hook, "on_writer_open")
                with self._cond:
                    self._writer_generation = generation
            with self._cond:
                item = self._next()
                if item is None:
                    if self._conn is not None and self._gate_open:
                        wait = max(0.0, next_pump - self._mono())
                    else:
                        wait = 0.5
                    if wait > 0:
                        self._cond.wait(wait)
                        item = self._next()
                pumping = self._conn is not None and self._gate_open and self._writer_generation == self._generation
            if item is not None:
                self._send_item(*item)
            if pumping and self._mono() >= next_pump:
                next_pump = self._mono() + self._pump_interval_s
                for pump in self._pumps:
                    _call_quietly(pump, "pump")
            elif not pumping:
                next_pump = self._mono()

    def _send_item(self, link: LinkRef, source: str, item: _Item) -> None:
        try:
            self._deliver(link, source, item)
        finally:
            if source in ("latest", "first"):  # 성패(버림 집계 포함)가 정해졌다: 다음 연결의 첫 health 가 기다린다
                with self._cond:
                    self._inflight = None
                    self._cond.notify_all()

    def _deliver(self, link: LinkRef, source: str, item: _Item) -> None:
        key, msg = item
        try:
            self._send(link.conn, msg)
        except Unserializable:
            return  # 기록은 _send 가 남겼다. 고칠 수 없으니 다시 넣지 않는다
        except SendFailed:
            if source in ("latest", "first"):  # 최신 칸은 다시 넣지 않는다(지난 상태). 버린 수는 센다
                kind = msg.get("msg_type")
                with self._cond:
                    counted = self._inflight is not None and self._inflight.counted  # 새 연결이 이미 셌다
                    if kind in self.stats.lost_on_disconnect and not counted:
                        self.stats.lost_on_disconnect[kind] += 1
            elif source == "reliable":  # 다시 연결되면 같은 msg_id 로 다시 보낸다(버리지 않음)
                with self._cond:
                    self._reliable.appendleft((key, msg))
            elif source == "telemetry":
                with self._cond:
                    self._telemetry.appendleft((key, msg))
            return
        if source == "first":
            with self._cond:
                if self._conn is link.conn and self._generation == link.generation:
                    self._gate_open = True
                    self._cond.notify_all()

    def _send(self, conn: Connection, message: Mapping[str, Any]) -> None:
        try:  # JSON 에 NaN·Infinity 를 싣지 않는다(계약 수는 유한)
            text = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as e:
            with self._cond:
                self.stats.unserializable += 1
            log.error("I1 %s 를 JSON 으로 만들 수 없어 버렸다 (%s)", message.get("msg_type"), type(e).__name__)
            raise Unserializable(str(message.get("msg_type"))) from None
        try:
            with self._send_lock:
                with self._cond:
                    self._sending = (self._mono(), conn)
                try:
                    conn.send(text)
                finally:
                    with self._cond:
                        self._sending = None
        except Exception as e:  # noqa: BLE001 - 보내다 끊김: 연결을 끊어 연결 스레드가 다시 붙게 한다
            error = safe_error(e)
            with self._cond:
                self.stats.send_failures += 1
            self.last_error = error
            abort_connection(conn)  # 닫기 프레임을 보내려다 다시 막히지 않게 소켓부터 끊는다
            raise SendFailed(error) from None
        kind = str(message.get("msg_type"))
        with self._cond:
            self.stats.sent[kind] = self.stats.sent.get(kind, 0) + 1


    # --- 송신 감시 스레드 -------------------------------------------------------------

    def _run_watchdog(self) -> None:
        period = min(0.25, self._send_timeout_s / 4)
        while not self._closing.wait(period):
            with self._cond:
                sending = self._sending
                if sending is None or self._mono() - sending[0] < self._send_timeout_s:
                    continue
                started, conn = sending
                if self._timed_out is conn:
                    continue  # 이미 끊었다(풀리기를 기다린다)
                self._timed_out = conn
                self.stats.send_timeouts += 1
            log.warning("I1 send 가 %.1f 초를 넘겨(EC2 가 읽지 않음) 연결을 끊고 다시 붙는다", self._send_timeout_s)
            abort_connection(conn)


_WINDOWS = sys.platform == "win32"
_ERROR_NOT_FOUND = 1168  # CancelIoEx: 취소할 대기 입출력이 없다


def abort_connection(conn: Connection) -> None:
    """하위 소켓을 shutdown 해 막힌 send·recv 를 바로 풀고, close 는 별도 스레드에서 한다(호출자는 기다리지 않는다).

    websockets 동기 연결의 close 는 send 와 같은 잠금을 잡고 닫기 프레임을 보내므로 상대가 읽지 않으면 막힌다.
    TLS 객체는 건드리지 않고 OS 소켓의 shutdown 만 부른다(SerialTLSSocket 이면 그 OS 소켓, ssl.SSLSocket 이면 SSLSocket.shutdown
    이 TLS 객체를 떼어 내므로 socket.socket.shutdown). socket 이 없는 연결(시험용)은 close 만.

    Windows 는 다른 스레드(websockets 수신)가 recv 에 막혀 있으면 shutdown 으로 막힌 send 가 풀리지 않는다. 그 핸들의 대기
    입출력을 CancelIoEx 로 취소해 풀고, 핸들은 닫지 않는다. 핸들은 연결의 원래 닫기 경로(close_async → websockets
    close_socket → 소켓 close)만 닫는다. C 수준 close 로 핸들을 먼저 닫으면 그 번호를 이어받은 다른 소켓에 늦은 쓰기가 닿을
    수 있다(S15P31A101-173). SerialTLSSocket 은 OpenSSL 이 OS 핸들을 들고 있지 않아 닫힌 뒤의 늦은 쓰기는 파이썬 소켓에서
    오류로 끝난다.
    """
    sock = getattr(conn, "socket", None)
    if isinstance(sock, SerialTLSSocket):
        sock = sock.raw
    if isinstance(sock, socket.socket):
        interrupt_socket(sock)
    close_async(conn)


def interrupt_socket(sock: socket.socket) -> None:
    """OS 소켓을 shutdown 해 막힌 send·recv 를 풀고(Linux), Windows 는 그 핸들의 대기 입출력도 취소한다. 핸들은 닫지 않는다
    (닫기는 소켓 주인의 원래 경로가 한다). abort_connection 과 I0 제어 채널의 핸드셰이크 중단이 쓴다."""
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)  # Linux: 막힌 send·recv 가 바로 풀린다
    except OSError:
        pass
    if _WINDOWS:
        _cancel_pending_io(sock)


def _cancel_pending_io(sock: socket.socket) -> None:
    """Windows: 이 프로세스가 그 소켓 핸들에 걸어 둔 대기 입출력(막힌 send·recv)을 모두 취소한다. 핸들은 닫지 않는다.

    이미 닫힌 소켓(fileno -1)은 건너뛴다. fileno 를 읽은 뒤 CancelIoEx 를 부르기 전 그 사이에 원래 닫기 경로가 핸들을 닫고
    같은 번호를 새 소켓이 받으면 새 소켓의 대기 입출력이 오류로 끝날 수 있다(마이크로초 구간, 데이터를 다른 소켓에 쓰지는 않는다).
    """
    handle = sock.fileno()
    if handle < 0:
        return
    cancel = _cancel_io_ex()
    if not cancel(handle, None):
        error = ctypes.get_last_error()
        if error != _ERROR_NOT_FOUND:
            log.debug("I1 CancelIoEx 실패 (Windows 오류 %d)", error)


@functools.cache
def _cancel_io_ex() -> Any:
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    cancel = kernel32.CancelIoEx
    cancel.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cancel.restype = ctypes.c_int
    return cancel


def close_async(conn: Connection) -> None:
    threading.Thread(target=_close_quietly, args=(conn,), name="i1-close", daemon=True).start()


def _close_quietly(conn: Connection) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _call_quietly(fn: Callable[[], Any], what: str) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - 훅·pump 실패가 연결·송신을 멈추지 않는다
        log.exception("I1 %s 실패", what)
