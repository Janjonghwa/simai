"""Windows WinError 10014 일시 오류 대응 (S15P31A101-199): 공용 함수(winsock.retry_wsaefault)와 단말의 관제 경로.

가짜 오류 주입으로 본다. 실제 Winsock 의 일시 오류는 시각을 고를 수 없어 기다리지 않는다. Windows 여부는 winsock 모듈의 sys 만
바꿔 정한다(전역 sys.platform 은 건드리지 않는다).
"""

from __future__ import annotations

import socket
import threading
import time
import types
from types import SimpleNamespace

import pytest
from tls_helpers import wait_until

from simai_site_alert import relay_status, transport, winsock
from simai_site_alert.relay_status import RelayStatusReceiver, build_report
from simai_site_alert.transport import WebSocketControlChannel


def wsa(code: int) -> OSError:
    e = OSError(f"[WinError {code}] 주입")
    e.winerror = code
    return e


OTHER_ERRORS = [wsa(10048), wsa(10061), ConnectionRefusedError("refused"), TimeoutError("timed out")]


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(winsock, "sys", SimpleNamespace(platform="win32"))


@pytest.fixture
def fake_time(monkeypatch, windows):
    """가짜 시계: sleep 은 기다리지 않고 시각만 민다. advance(s) 로 fn 이 걸린 시간을 흉내 낸다."""
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(s: float) -> None:
        clock.sleeps.append(s)
        clock.now += s

    clock.advance = lambda s: setattr(clock, "now", clock.now + s)
    monkeypatch.setattr(winsock, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: clock.now))
    return clock


def flaky(errors, result="ok", cost_s: float = 0.0, clock=None):
    calls = []

    def fn():
        calls.append(1)
        if clock is not None:
            clock.advance(cost_s)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return result

    return fn, calls


# ---------------------------------------------------------------------------- 공용 함수


def test_transient_10014_is_retried_until_it_succeeds(fake_time):
    fn, calls = flaky([wsa(10014)] * 3)
    assert winsock.retry_wsaefault(fn, what="t") == "ok"
    assert len(calls) == 4 and fake_time.sleeps == pytest.approx([0.01, 0.02, 0.03])


def test_success_without_error_does_not_wait(fake_time):
    fn, calls = flaky([])
    assert winsock.retry_wsaefault(fn, what="t") == "ok" and len(calls) == 1 and fake_time.sleeps == []


def test_persistent_10014_is_raised_within_the_budget(fake_time, caplog):
    """반례(경계): 끝까지 10014 면 예산 안에서 멈추고 마지막 10014 를 그대로 올린다(성공으로 바꾸거나 계속 기다리지 않음)."""
    fn, calls = flaky([wsa(10014)] * 1000)
    with pytest.raises(OSError) as info:
        winsock.retry_wsaefault(fn, what="경로X")
    assert info.value.winerror == 10014
    assert sum(fake_time.sleeps) <= winsock.BUDGET_S and max(fake_time.sleeps) <= winsock.MAX_WAIT_S
    assert len(calls) == len(fake_time.sleeps) + 1 < 1000
    assert "경로X" in caplog.text and "멈춘다" in caplog.text  # 다 쓴 것은 로그로 드러난다


def test_slow_failing_call_counts_against_the_budget(fake_time):
    """반례(경계): 한 번에 0.25 s 걸리고 10014 로 끝나는 호출은 두 번째에서 멈춘다(예산 + 호출 한 번 안으로 묶인다)."""
    fn, calls = flaky([wsa(10014)] * 10, cost_s=0.25, clock=fake_time)
    with pytest.raises(OSError):
        winsock.retry_wsaefault(fn, what="t")
    assert len(calls) == 2 and fake_time.now <= winsock.BUDGET_S + 0.25


@pytest.mark.parametrize("error", OTHER_ERRORS, ids=["10048", "10061", "refused", "timeout"])
def test_other_errors_are_raised_at_once(fake_time, error):
    fn, calls = flaky([error])
    with pytest.raises(type(error)):
        winsock.retry_wsaefault(fn, what="t")
    assert len(calls) == 1 and fake_time.sleeps == []


def test_off_windows_is_a_no_op(monkeypatch):
    monkeypatch.setattr(winsock, "sys", SimpleNamespace(platform="linux"))
    fn, calls = flaky([wsa(10014)])
    with pytest.raises(OSError):
        winsock.retry_wsaefault(fn, what="t")
    assert len(calls) == 1


# ---------------------------------------------------------------------------- 제어 채널 이벤트 루프 (socketpair)


class Tier0Stub:
    """접속만 받는 loopback 제어 서버."""

    def __init__(self) -> None:
        import asyncio

        self.accepted = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        import asyncio

        from websockets.asyncio.server import serve

        async def handler(ws) -> None:
            self.accepted += 1
            await ws.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await asyncio.get_running_loop().run_in_executor(None, self._stop.wait)

    def __enter__(self) -> Tier0Stub:
        self._thread.start()
        assert self._ready.wait(5)
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(5)


def _flaky_loops(monkeypatch, errors):
    """transport 가 부르는 asyncio.new_event_loop 를 errors 를 차례로 올리는 것으로 바꾼다(다 쓰면 진짜 루프)."""
    import asyncio

    real = asyncio.new_event_loop
    state = SimpleNamespace(calls=0, errors=list(errors))

    def new_event_loop():
        state.calls += 1
        if state.errors:
            raise state.errors.pop(0)
        return real()

    proxy = types.ModuleType("asyncio_proxy")
    proxy.__dict__.update(asyncio.__dict__)
    proxy.new_event_loop = new_event_loop
    monkeypatch.setattr(transport, "asyncio", proxy)
    return state


def _start_in_thread(channel, connected: list) -> threading.Thread:
    t = threading.Thread(target=lambda: channel.start(lambda *_: None, connected.append), daemon=True)
    t.start()
    t.join(2)
    return t


def test_control_loop_10014_is_retried_and_channel_connects(windows, monkeypatch):
    with Tier0Stub() as stub:
        state = _flaky_loops(monkeypatch, [wsa(10014)] * 2)
        channel = WebSocketControlChannel(f"ws://127.0.0.1:{stub.port}/control", device_token="tok",
                                          reconnect_min_s=5.0)  # 재접속 간격을 길게: 다시 시도만으로 붙어야 한다
        connected: list = []
        try:
            assert not _start_in_thread(channel, connected).is_alive()
            wait_until(lambda: connected == [True] and stub.accepted == 1, 3)  # 서버 처리기는 핸드셰이크 뒤에 돈다
            assert state.calls == 3
        finally:
            channel.close()


def test_persistent_loop_failure_does_not_hang_start_and_shows_disconnected(windows, monkeypatch, caplog):
    """반례(모름을 정상으로 두지 않음·멈춤): 예산을 넘는 10014 에도 start() 는 돌아오고, 연결 안 됨(on_connected 없음,
    send 는 False)으로 남다가, 오류가 그치면 재접속 간격 뒤에 루프를 다시 만들어 붙는다."""
    with Tier0Stub() as stub:
        state = _flaky_loops(monkeypatch, [wsa(10014)] * 1000)
        channel = WebSocketControlChannel(f"ws://127.0.0.1:{stub.port}/control", device_token="tok",
                                          reconnect_min_s=0.2)
        connected: list = []
        try:
            assert not _start_in_thread(channel, connected).is_alive()  # 고치기 전: 루프 스레드가 죽어 영원히 기다렸다
            time.sleep(0.5)
            assert connected == [] and channel.send({"msg_type": "x"}) is False and stub.accepted == 0
            assert "이벤트 루프를 만들지 못했다" in caplog.text
            state.errors.clear()
            wait_until(lambda: connected == [True] and stub.accepted == 1, 3)
        finally:
            channel.close()


def test_close_while_loop_creation_keeps_failing_returns(windows, monkeypatch):
    """반례(종료 순서): 루프를 못 만드는 중에 close() 해도 스레드가 끝난다."""
    _flaky_loops(monkeypatch, [wsa(10014)] * 1000)
    channel = WebSocketControlChannel("ws://127.0.0.1:9/control", device_token="tok", reconnect_min_s=0.2)
    assert not _start_in_thread(channel, []).is_alive()
    started = time.monotonic()
    channel.close()
    assert time.monotonic() - started < 2 and not channel._thread.is_alive()  # noqa: SLF001 - 시험


def test_other_loop_error_is_not_retried_inside_the_budget(windows, monkeypatch):
    """음성 대조: 10014 가 아닌 오류는 공용 함수가 다시 시도하지 않는다(한 번 부르고 재접속 간격을 기다린다)."""
    state = _flaky_loops(monkeypatch, [wsa(10048)] * 1000)
    channel = WebSocketControlChannel("ws://127.0.0.1:9/control", device_token="tok", reconnect_min_s=5.0)
    try:
        assert not _start_in_thread(channel, []).is_alive()
        time.sleep(0.3)
        assert state.calls == 1
    finally:
        channel.close()


# ---------------------------------------------------------------------------- 중계 상태 포트 (bind)


def _flaky_bind(monkeypatch, errors):
    state = SimpleNamespace(binds=0, closed=0, errors=list(errors))

    class FlakySocket(socket.socket):
        def bind(self, addr):
            state.binds += 1
            if state.errors:
                raise state.errors.pop(0)
            return super().bind(addr)

        def close(self):
            state.closed += 1
            return super().close()

    proxy = types.ModuleType("socket_proxy")
    proxy.__dict__.update(socket.__dict__)
    proxy.socket = FlakySocket
    monkeypatch.setattr(relay_status, "socket", proxy)
    return state


def test_relay_status_bind_10014_is_retried_and_receives(windows, monkeypatch):
    state = _flaky_bind(monkeypatch, [wsa(10014)] * 2)
    got: list = []
    rx = RelayStatusReceiver(lambda report, now: got.append(report), port=0)
    rx.start()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        assert state.binds == 3 and state.closed == 2  # 실패한 소켓은 닫았다
        tx.sendto(build_report(boot="b", seq=1, camera_count=1, video_cameras=["cam1"], cameras=[]),
                  ("127.0.0.1", rx.port))
        wait_until(lambda: len(got) == 1, 3)
    finally:
        tx.close()
        rx.close()


def test_relay_status_bind_persistent_10014_raises_and_leaks_nothing(windows, monkeypatch):
    """반례: 예산을 다 쓰면 OSError 를 올린다(cli 가 영상 상태를 모름 = 관제 중단으로 표시하는 기존 경로). 소켓은 모두 닫는다."""
    state = _flaky_bind(monkeypatch, [wsa(10014)] * 1000)
    with pytest.raises(OSError) as info:
        RelayStatusReceiver(lambda *a: None, port=0)
    assert info.value.winerror == 10014 and 1 < state.binds == state.closed < 1000


@pytest.mark.parametrize("error", OTHER_ERRORS, ids=["10048", "10061", "refused", "timeout"])
def test_relay_status_other_bind_errors_raise_at_once(windows, monkeypatch, error):
    state = _flaky_bind(monkeypatch, [error])
    with pytest.raises(type(error)):
        RelayStatusReceiver(lambda *a: None, port=0)
    assert state.binds == 1 == state.closed
