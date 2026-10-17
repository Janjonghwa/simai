"""제어 채널 클라이언트(loopback WebSocket)와 조립(app): 수신, 연결 상태, site_ack 송신·재전송, 주소 규칙."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from simai_site_alert.app import SiteAlertApp
from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.sound import FakeSoundBackend
from simai_site_alert.transport import InMemoryControlChannel, WebSocketControlChannel


def wait_until(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("시간 안에 조건이 맞지 않았다")
        time.sleep(0.01)


class Tier0Stub:
    """시험용 Tier 0 제어 서버. 접속마다 to_send 를 보내고, 받은 메시지와 인증 헤더를 기록한다."""

    def __init__(self, to_send: list[dict]) -> None:
        self.to_send = to_send
        self.received: list[dict] = []
        self.auth: list[str | None] = []
        self.port: int | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._conns: list = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _handler(self, ws) -> None:
        from websockets.exceptions import ConnectionClosed

        self._conns.append(ws)
        self.auth.append(ws.request.headers.get("Authorization"))
        try:
            for msg in self.to_send:
                await ws.send(json.dumps(msg))
            async for raw in ws:
                self.received.append(json.loads(raw))
        except ConnectionClosed:
            pass

    async def _main(self) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        async with serve(self._handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._loop.run_in_executor(None, self._stop.wait)

    def __enter__(self) -> Tier0Stub:
        self._thread.start()
        self._ready.wait(5)
        return self

    def drop_connections(self) -> None:
        for ws in list(self._conns):
            asyncio.run_coroutine_threadsafe(ws.close(), self._loop).result(5)
        self._conns.clear()

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(5)


def test_websocket_delivers_alert_and_sends_site_ack() -> None:
    eid = uuid7()
    with Tier0Stub([local_alert(eid, 1, "RAISED")]) as stub:
        sound = FakeSoundBackend()
        core = SiteAlertCore(sound, laptop_id="laptop-01")
        channel = WebSocketControlChannel(f"ws://127.0.0.1:{stub.port}/control", device_token="tok-123",
                                          reconnect_min_s=0.05, reconnect_max_s=0.1)
        app = SiteAlertApp(core, channel)
        app.start(test_alert_s=None)
        try:
            wait_until(lambda: eid in core.active_alerts())
            assert core.screen_state().control_connected is True
            assert sound.playing.name == "CRITICAL"
            assert stub.auth == ["Bearer tok-123"]
            app.site_ack(eid)
            assert sound.playing is None  # 누르는 즉시 소리 정지
            wait_until(lambda: len(stub.received) == 1)
            assert stub.received[0]["msg_type"] == "site_ack"
            assert stub.received[0]["payload"]["event_id"] == eid
            # 끊겼다 다시 붙으면 Tier 0 기록(alarm_stop)을 받지 못한 site_ack 를 같은 봉투로 다시 보낸다
            stub.drop_connections()
            wait_until(lambda: len(stub.received) == 2)
            assert stub.received[1] == stub.received[0]
        finally:
            app.close()
        assert core.screen_state().control_connected is False


def test_site_ack_while_disconnected_is_sent_on_reconnect() -> None:
    sound = FakeSoundBackend()
    core = SiteAlertCore(sound, laptop_id="laptop-01")
    channel = InMemoryControlChannel()
    app = SiteAlertApp(core, channel, tick_s=60)
    app.start(test_alert_s=None)
    try:
        channel.set_connected(True)
        eid = uuid7()
        channel.deliver(local_alert(eid, 1, "RAISED"))
        channel.set_connected(False)
        app.site_ack(eid)
        assert sound.playing is None and channel.sent == []
        channel.set_connected(True)
        assert [m["msg_type"] for m in channel.sent] == ["site_ack"]
    finally:
        app.close()


def test_app_startup_runs_test_alert_then_ends() -> None:
    sound = FakeSoundBackend()
    core = SiteAlertCore(sound, laptop_id="laptop-01")
    app = SiteAlertApp(core, None, tick_s=60)
    app.start(test_alert_s=0.05)
    try:
        assert core.screen_state().test.active
        assert sound.calls == [("start", "TEST")]
        wait_until(lambda: not core.screen_state().test.active)
        assert sound.calls[-1] == ("stop", None)
    finally:
        app.close()


@pytest.mark.parametrize(
    "url",
    ["ws://10.0.0.5:7000/control", "http://127.0.0.1:7000/control", "tcp://tier0:7000"],
)
def test_channel_requires_wss_except_loopback(url: str) -> None:
    with pytest.raises(ValueError):
        WebSocketControlChannel(url, device_token="tok")


def test_channel_requires_token() -> None:
    with pytest.raises(ValueError):
        WebSocketControlChannel("wss://tier0.example:7000/control", device_token="")


def test_wss_verifies_certificates() -> None:
    import ssl

    channel = WebSocketControlChannel("wss://tier0.example:7000/control", device_token="tok")
    ctx = channel._ssl  # noqa: SLF001 - 시험
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
