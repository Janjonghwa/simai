"""I0 제어 채널(노트북 ↔ Tier 0, wss) 클라이언트. 코어와 분리돼 있어 코어는 문자열 메시지만 받는다.

- ControlChannel: 전송 인터페이스. 받은 메시지마다 on_message(원문, 수신 시각) 를, 연결이 바뀌면 on_connected(bool) 를 부른다.
- WebSocketControlChannel: websockets 로 만든 구현. TLS(wss), 기기 토큰 인증 헤더, 끊기면 다시 붙는다.
  - 운영(resolver): 접속할 때마다 EC2 에서 현재 Tier 0 엔드포인트를 조회해(endpoint.EndpointResolver) 그 주소로 붙고,
    서버 공개키 지문이 노트북 고정값과 같은지 TLS 핸드셰이크 직후·기기 토큰을 보내기 전에 확인한다(pinning).
    접속에 실패하거나 끊기면 다시 조회하고, 연결 중에도 refresh_s 마다 다시 조회해 주소·포트·지문이 바뀌면 새로 붙는다.
  - 고정 주소(url): 시험·지연 측정용. wss 는 기본 인증서 검증(체인·호스트 이름), ws:// 는 같은 컴퓨터(loopback)만.
  인증서 검증을 끄고 아무 서버에나 붙는 경로는 없다. 서버 확인(peer_verifier)은 기기 토큰을 싣는 HTTP 요청보다 먼저 한다.
- InMemoryControlChannel: 시험·오프라인용.

기기 토큰 발급, 카메라 영상 연결은 이 모듈 밖이다. laptop_heartbeat·clock_sync_response 는 app 이 send 로 보낸다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from simai_site_alert.endpoint import ControlTarget
from simai_site_alert.pinning import PinMismatchError, PinnedPeerVerifier, pinned_client_context
from simai_site_alert.winsock import retry_wsaefault

log = logging.getLogger(__name__)

OnMessage = Callable[[str | bytes, float], None]
OnConnected = Callable[[bool], None]
# Tier 0 서버 확인. TLS 핸드셰이크 뒤·HTTP 요청(기기 토큰) 전에 SSLObject 를 받아 맞지 않으면 예외를 낸다.
PeerVerifier = Callable[[ssl.SSLObject], None]

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class _RedactAuthorization(logging.Filter):
    """websockets 는 DEBUG 로그에 요청 헤더를 그대로 찍는다(`> Authorization: Bearer <토큰>`). 토큰 줄을 가린다."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - 형식이 깨진 기록은 그대로 둔다
            return True
        if "authorization" in text.lower():
            record.msg, record.args = "> Authorization: ***", ()
        return True


# websockets 연결 로그는 이 로거로 받는다(-v 로 DEBUG 를 켜도 기기 토큰이 찍히지 않게)
_ws_log = logging.getLogger(f"{__name__}.websockets")
_ws_log.addFilter(_RedactAuthorization())


class ControlChannel(Protocol):
    def start(self, on_message: OnMessage, on_connected: OnConnected) -> None: ...

    def send(self, message: dict[str, Any]) -> bool:
        """보낼 수 있으면 보내고(기다리지 않음) True, 연결이 없으면 False."""

    def reset(self) -> None:
        """지금 연결을 끊고 다시 붙는다(기다리지 않음). Tier 0 health 가 끊긴 반쯤 열린 연결을 버릴 때 쓴다(명세 3.2)."""

    def close(self) -> None: ...


class TargetResolver(Protocol):
    """접속할 때마다 부르는 현재 접속 대상 조회. pins 는 노트북에 미리 고정한 공개키 지문 목록."""

    pins: frozenset[str]

    def resolve(self) -> ControlTarget:
        """현재 엔드포인트. 받지 못하거나 지문이 고정값과 다르면 예외(그 시도는 접속하지 않는다)."""


class InMemoryControlChannel:
    """시험·오프라인용. deliver() 로 받은 메시지를 흉내 내고, 보낸 메시지는 sent 에 쌓인다."""

    def __init__(self, monotonic: Callable[[], float] = time.perf_counter) -> None:
        self.sent: list[dict[str, Any]] = []
        self.connected = False
        self.resets = 0
        self._monotonic = monotonic
        self._on_message: OnMessage | None = None
        self._on_connected: OnConnected | None = None

    def start(self, on_message: OnMessage, on_connected: OnConnected) -> None:
        self._on_message, self._on_connected = on_message, on_connected

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        if self._on_connected:
            self._on_connected(connected)

    def deliver(self, message: dict[str, Any] | str) -> None:
        assert self._on_message is not None, "start() 전"
        text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        self._on_message(text, self._monotonic())

    def send(self, message: dict[str, Any]) -> bool:
        if not self.connected:
            return False
        self.sent.append(message)
        return True

    def reset(self) -> None:
        self.resets += 1
        if self.connected:
            self.set_connected(False)

    def close(self) -> None:
        self.connected = False


def _verified_connection_class(verifier: PeerVerifier | None) -> type:
    """기기 토큰을 싣는 HTTP 업그레이드 요청을 보내기 전에 서버를 확인하는 websockets 연결 클래스.

    websockets 는 TLS 핸드셰이크가 끝난 연결에서 handshake() 로 HTTP 요청(Authorization 헤더)을 보낸다. 그 앞에서
    verifier 가 예외를 내면 요청은 나가지 않고, websockets 가 연결을 끊고 예외를 그대로 올린다.
    """
    from websockets.asyncio.client import ClientConnection

    class VerifiedClientConnection(ClientConnection):
        async def handshake(self, *args: Any, **kwargs: Any) -> None:
            if verifier is not None:
                ssl_object = self.transport.get_extra_info("ssl_object")
                if ssl_object is None:
                    raise ssl.SSLError("TLS 가 아닌 연결은 서버를 확인할 수 없다")
                verifier(ssl_object)
            await super().handshake(*args, **kwargs)

    return VerifiedClientConnection


class WebSocketControlChannel:
    def __init__(
        self,
        url: str | None = None,
        *,
        device_token: str,
        resolver: TargetResolver | None = None,
        ca_file: str | None = None,
        peer_verifier: PeerVerifier | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
        reconnect_min_s: float = 0.5,
        reconnect_max_s: float = 5.0,
        refresh_s: float = 30.0,
        stable_after_s: float = 10.0,
    ) -> None:
        if (url is None) == (resolver is None):
            raise ValueError("고정 주소(url)와 엔드포인트 조회(resolver) 중 하나만 준다")
        if not device_token:
            raise ValueError("기기 토큰이 없다(.env 의 SIMAI_DEVICE_TOKEN)")
        self._token = device_token
        self._resolver = resolver
        self._static: ControlTarget | None = None
        self._ssl: ssl.SSLContext | None = None
        if url is not None:
            parts = urlsplit(url)
            if parts.scheme == "ws":
                if parts.hostname not in _LOOPBACK:
                    raise ValueError("ws:// 는 loopback 시험에서만 쓴다. Tier 0 제어 채널은 wss:// 다")
            elif parts.scheme != "wss":
                raise ValueError("제어 채널 주소는 wss:// 여야 한다")
            self._static = ControlTarget(url, "")
            if parts.scheme == "wss":
                # 기본 검증(인증서 체인·호스트 이름)을 끄지 않는다. 팀 사설 CA 면 ca_file 로 준다.
                self._ssl = ssl.create_default_context(cafile=ca_file)
        else:
            # 공개키 고정 문맥. 이 문맥으로는 PinnedPeerVerifier 없이 연결하지 않는다(_connect_args, _run).
            self._ssl = pinned_client_context(ca_file)
        self._peer_verifier = peer_verifier
        self._monotonic = monotonic
        self._reconnect = (reconnect_min_s, reconnect_max_s)
        self._refresh_s = refresh_s
        # 연결이 이만큼 유지된 뒤 끊겼을 때만 재접속 간격을 처음 값으로 되돌린다. 받자마자 끊는 서버에 초당 여러 번
        # 조회·재접속하지 않게 한다(백오프가 계속 늘어난다).
        self._stable_after_s = stable_after_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._thread: threading.Thread | None = None
        self._closing = threading.Event()
        self._switch_now = False
        self.current_target: ControlTarget | None = None

    def start(self, on_message: OnMessage, on_connected: OnConnected) -> None:
        self._on_message, self._on_connected = on_message, on_connected
        ready = threading.Event()

        def run() -> None:
            # Windows 이벤트 루프는 만들 때 socketpair 를 연다. 실패해도 start() 가 영원히 기다리지 않게 하고(ready),
            # 연결 안 됨(관제 중단 표시) 상태로 재접속 간격마다 다시 만든다.
            try:
                while not self._closing.is_set():
                    try:
                        loop = retry_wsaefault(asyncio.new_event_loop, what="제어 채널 이벤트 루프")
                    except OSError as e:
                        log.error("제어 채널 이벤트 루프를 만들지 못했다(%s). %.1f초 뒤 다시 만든다",
                                  type(e).__name__, self._reconnect[0])
                        ready.set()
                        self._closing.wait(self._reconnect[0])
                        continue
                    self._loop = loop
                    ready.set()
                    try:
                        loop.run_until_complete(self._run())
                    finally:
                        loop.close()
                    return
            finally:
                ready.set()

        self._thread = threading.Thread(target=run, name="i0-control", daemon=True)
        self._thread.start()
        ready.wait()

    async def _next_target(self) -> ControlTarget:
        if self._static is not None:
            return self._static
        assert self._resolver is not None
        return await asyncio.get_running_loop().run_in_executor(None, self._resolver.resolve)

    def _connect_args(self, target: ControlTarget) -> PeerVerifier | None:
        if self._static is not None:
            return self._peer_verifier
        assert self._resolver is not None
        # 접속마다 그 엔드포인트의 지문으로 검사기를 만든다. 지문이 고정 목록에 없으면 여기서 예외(접속 안 함).
        pinned = PinnedPeerVerifier(target.spki_sha256, self._resolver.pins)
        extra = self._peer_verifier
        if extra is None:
            return pinned

        def both(ssl_object: ssl.SSLObject) -> None:
            pinned(ssl_object)
            extra(ssl_object)

        return both

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        delay = self._reconnect[0]
        while not self._closing.is_set():
            self._switch_now = False
            try:
                target = await self._next_target()
                verifier = self._connect_args(target)
                if self._ssl is not None and self._ssl.verify_mode != ssl.CERT_REQUIRED and verifier is None:
                    raise ssl.SSLError("서버 확인 없이 접속하지 않는다")  # 방어: 검증 없는 TLS 연결 금지
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 조회 실패·지문 불일치는 접속하지 않고 다시 조회한다. 토큰을 로그에 넣지 않는다
                if not self._closing.is_set():
                    log.warning("Tier 0 접속 주소를 받지 못했다(%s): %s", type(e).__name__, e)
            else:
                connected_at: float | None = None
                try:
                    async with connect(
                        target.url,
                        ssl=self._ssl,
                        additional_headers={"Authorization": f"Bearer {self._token}"},
                        open_timeout=5,
                        max_queue=None,
                        logger=_ws_log,
                        create_connection=_verified_connection_class(verifier),
                    ) as ws:
                        self._ws = ws
                        self.current_target = target
                        connected_at = time.monotonic()
                        self._on_connected(True)
                        watcher = asyncio.ensure_future(self._watch(ws, target)) if self._resolver else None
                        try:
                            async for raw in ws:
                                self._on_message(raw, self._monotonic())
                        finally:
                            if watcher is not None:
                                watcher.cancel()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # 연결 실패·끊김은 다시 조회해 붙는다. 토큰을 로그에 넣지 않는다
                    if not self._closing.is_set():
                        log.warning("제어 채널 끊김 또는 연결 실패(%s): %s", type(e).__name__, e)
                finally:
                    if self._ws is not None:
                        self._ws = None
                        self._on_connected(False)
                    if connected_at is not None and time.monotonic() - connected_at >= self._stable_after_s:
                        delay = self._reconnect[0]
            if self._closing.is_set():
                break
            if self._switch_now:  # 엔드포인트가 바뀌어 일부러 끊었으면 바로 새 주소로
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._reconnect[1])

    async def _watch(self, ws: Any, target: ControlTarget) -> None:
        """연결 중 주기 재조회. 주소·포트·지문이 바뀌면 지금 연결을 닫고 새로 붙는다. 조회가 실패하면 연결을 유지한다."""
        assert self._resolver is not None
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._refresh_s)
            try:
                latest = await loop.run_in_executor(None, self._resolver.resolve)
            except PinMismatchError as e:  # EC2 가 고정 밖 지문을 알려 줬다. 지금(검증된) 연결은 유지하고, 새 접속은 거부된다
                log.warning("연결 중 재조회: EC2 가 알려 준 Tier 0 공개키 지문이 고정값과 다르다(%s). 지금 연결을 유지한다", e)
                continue
            except Exception as e:
                log.info("연결 중 엔드포인트 재조회 실패(%s). 지금 연결을 유지한다", type(e).__name__)
                continue
            if latest != target:
                log.warning("Tier 0 엔드포인트가 바뀌었다. 새 주소로 다시 접속한다")
                self._switch_now = True
                await ws.close()
                return

    def send(self, message: dict[str, Any]) -> bool:
        ws, loop = self._ws, self._loop
        if ws is None or loop is None:
            return False
        text = json.dumps(message, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(ws.send(text), loop)
        return True

    def reset(self) -> None:
        """지금 연결을 바로 끊는다(닫기 인사를 기다리지 않음: 반쯤 열린 연결은 답하지 않는다). 수신 루프가 끝나면 다시 붙는다."""
        ws, loop = self._ws, self._loop
        if ws is None or loop is None or not loop.is_running():
            return

        def abort() -> None:
            transport = getattr(ws, "transport", None)
            if transport is not None:
                transport.abort()

        log.warning("Tier 0 health 가 끊겨 제어 채널을 끊고 다시 붙는다")
        loop.call_soon_threadsafe(abort)

    def close(self) -> None:
        self._closing.set()
        ws, loop = self._ws, self._loop
        if ws is not None and loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(ws.close(), loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
