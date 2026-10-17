"""I0 제어 채널(노트북 ↔ Tier 0, wss) 클라이언트. 코어와 분리돼 있어 코어는 문자열 메시지만 받는다.

- ControlChannel: 전송 인터페이스. 받은 메시지마다 on_message(원문, 수신 시각) 를, 연결이 바뀌면 on_connected(bool) 를 부른다.
- WebSocketControlChannel: websockets 로 만든 간단한 구현. TLS(wss) 기본, 기기 토큰 인증 헤더, 끊기면 다시 붙는다.
  인증서 검증을 끄는 경로는 없다. Tier 0 공개키 지문 고정은 S15P31A101-159 몫이라 peer_verifier 자리만 둔다.
  ws:// 는 같은 컴퓨터(loopback) 시험에서만 허용한다.
- InMemoryControlChannel: 시험·오프라인용.

실제 Tier 0 연결(엔드포인트 조회, 기기 토큰 발급, 시계 차이 교환, laptop_heartbeat 송신)은 이 티켓 범위 밖이다.
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

log = logging.getLogger(__name__)

OnMessage = Callable[[str | bytes, float], None]
OnConnected = Callable[[bool], None]
# Tier 0 서버 확인(공개키 지문 고정, S15P31A101-159). TLS 핸드셰이크 뒤 SSLObject 를 받아 맞지 않으면 예외를 낸다.
PeerVerifier = Callable[[ssl.SSLObject], None]

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class ControlChannel(Protocol):
    def start(self, on_message: OnMessage, on_connected: OnConnected) -> None: ...

    def send(self, message: dict[str, Any]) -> bool:
        """보낼 수 있으면 보내고(기다리지 않음) True, 연결이 없으면 False."""

    def close(self) -> None: ...


class InMemoryControlChannel:
    """시험·오프라인용. deliver() 로 받은 메시지를 흉내 내고, 보낸 메시지는 sent 에 쌓인다."""

    def __init__(self, monotonic: Callable[[], float] = time.perf_counter) -> None:
        self.sent: list[dict[str, Any]] = []
        self.connected = False
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

    def close(self) -> None:
        self.connected = False


class WebSocketControlChannel:
    def __init__(
        self,
        url: str,
        *,
        device_token: str,
        ca_file: str | None = None,
        peer_verifier: PeerVerifier | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
        reconnect_min_s: float = 0.5,
        reconnect_max_s: float = 5.0,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme == "ws":
            if parts.hostname not in _LOOPBACK:
                raise ValueError("ws:// 는 loopback 시험에서만 쓴다. Tier 0 제어 채널은 wss:// 다")
        elif parts.scheme != "wss":
            raise ValueError("제어 채널 주소는 wss:// 여야 한다")
        if not device_token:
            raise ValueError("기기 토큰이 없다(.env 의 SIMAI_DEVICE_TOKEN)")
        self._url = url
        self._token = device_token
        self._ssl: ssl.SSLContext | None = None
        if parts.scheme == "wss":
            # 기본 검증(인증서 체인·호스트 이름)을 끄지 않는다. 팀 사설 CA 면 ca_file 로 준다.
            self._ssl = ssl.create_default_context(cafile=ca_file)
        self._peer_verifier = peer_verifier
        self._monotonic = monotonic
        self._reconnect = (reconnect_min_s, reconnect_max_s)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._thread: threading.Thread | None = None
        self._closing = threading.Event()

    def start(self, on_message: OnMessage, on_connected: OnConnected) -> None:
        self._on_message, self._on_connected = on_message, on_connected
        ready = threading.Event()

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            ready.set()
            try:
                self._loop.run_until_complete(self._run())
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=run, name="i0-control", daemon=True)
        self._thread.start()
        ready.wait()

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        delay = self._reconnect[0]
        while not self._closing.is_set():
            try:
                async with connect(
                    self._url,
                    ssl=self._ssl,
                    additional_headers={"Authorization": f"Bearer {self._token}"},
                    open_timeout=5,
                    max_queue=None,
                ) as ws:
                    if self._peer_verifier is not None:
                        ssl_object = ws.transport.get_extra_info("ssl_object")
                        if ssl_object is None:
                            raise ssl.SSLError("TLS 가 아닌 연결은 서버를 확인할 수 없다")
                        self._peer_verifier(ssl_object)
                    self._ws = ws
                    delay = self._reconnect[0]
                    self._on_connected(True)
                    async for raw in ws:
                        self._on_message(raw, self._monotonic())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 연결 실패·끊김은 다시 붙는다. 토큰을 로그에 넣지 않는다
                if not self._closing.is_set():
                    log.warning("제어 채널 끊김 또는 연결 실패(%s): %s", type(e).__name__, e)
            finally:
                if self._ws is not None:
                    self._ws = None
                    self._on_connected(False)
            if self._closing.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._reconnect[1])

    def send(self, message: dict[str, Any]) -> bool:
        ws, loop = self._ws, self._loop
        if ws is None or loop is None:
            return False
        text = json.dumps(message, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(ws.send(text), loop)
        return True

    def close(self) -> None:
        self._closing.set()
        ws, loop = self._ws, self._loop
        if ws is not None and loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(ws.close(), loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
