"""Windows WinError 10014 일시 오류 대응 (S15P31A101-199): 카메라별 Tier 0 영상 연결(connect).

공용 함수(simai_site_alert.winsock.retry_wsaefault) 자체는 site_alert 시험이 본다. 여기서는 tls_sink 의 호출 지점: 10014 는
새 소켓으로 다시 붙어 재접속 없이 이어 가고, 예산을 넘거나 다른 오류면 기존 connect_failures·재접속 경로로 가서 상태에
DISCONNECTED 로 드러나는지 본다. 오류는 tls_sink 가 쓰는 socket 모듈 자리에 가짜 소켓 클래스를 넣어 주입한다.
"""

from __future__ import annotations

import socket
import time
import types
from types import SimpleNamespace

import pytest
from simai_site_alert import winsock
from tls_helpers import FakeTier0Video, wait_until

from simai_relay import tls_sink
from simai_relay.bench import SelfSigned, codec_info, frame, make_self_signed
from simai_relay.tls_sink import TlsCameraSink, VideoTarget

TOKEN = "device-token-S3CRET-199"


def wsa(code: int) -> OSError:
    e = OSError(f"[WinError {code}] 주입")
    e.winerror = code
    return e


@pytest.fixture
def cert(tmp_path) -> SelfSigned:
    return make_self_signed(tmp_path / "a", "tier0")


def _platform(monkeypatch, name: str) -> None:
    monkeypatch.setattr(winsock, "sys", SimpleNamespace(platform=name))


def _flaky_connect(monkeypatch, errors):
    state = SimpleNamespace(connects=0, created=0, closed=0, errors=list(errors))

    class FlakySocket(socket.socket):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            state.created += 1

        def connect(self, addr):
            state.connects += 1
            if state.errors:
                raise state.errors.pop(0)
            return super().connect(addr)

        def close(self):
            state.closed += 1
            return super().close()

    proxy = types.ModuleType("socket_proxy")
    proxy.__dict__.update(socket.__dict__)
    proxy.socket = FlakySocket
    monkeypatch.setattr(tls_sink, "socket", proxy)
    return state


def _sink(server: FakeTier0Video, cert: SelfSigned, delays=(0.05, 0.1)) -> TlsCameraSink:
    return TlsCameraSink("cam_A", lambda: VideoTarget("127.0.0.1", server.port, cert.fingerprint), device_token=TOKEN,
                         pins=frozenset({cert.fingerprint}), reconnect_delays_s=delays)


def test_transient_10014_on_connect_continues_without_reconnect(cert, monkeypatch):
    _platform(monkeypatch, "win32")
    state = _flaky_connect(monkeypatch, [wsa(10014)] * 3)
    with FakeTier0Video(cert, {TOKEN}) as server:
        sink = _sink(server, cert, delays=(5.0,))  # 재접속 간격을 길게: 다시 시도만으로 붙어야 한다
        try:
            wait_until(lambda: sink.connected, 3)
            sink.send_codec_info(codec_info("cam_A"))
            sink.send_frame(frame("cam_A", 0, True, b"x"))
            wait_until(lambda: len(server.frames("cam_A")) == 1, 3)
            snap = sink.snapshot()
            assert snap["connect_failures"] == 0 and snap["connects"] == 1 and snap["tier0_video"] == "CONNECTED"
            assert state.connects == 4 and state.created == 4 and len(server.by_camera("cam_A")) == 1
        finally:
            sink.close()
    assert state.closed >= 3  # 실패한 소켓 셋은 닫았다


def test_persistent_10014_goes_to_existing_reconnect_and_shows_disconnected(cert, monkeypatch):
    """반례(모름을 정상으로 두지 않음): 예산을 넘는 10014 는 connect_failures 로 세고 DISCONNECTED 로 보인다(하트비트의
    video_cameras 에서 빠진다). 오류가 그치면 기존 재접속으로 다시 붙는다. 실패한 소켓은 모두 닫는다."""
    _platform(monkeypatch, "win32")
    state = _flaky_connect(monkeypatch, [wsa(10014)] * 1000)
    with FakeTier0Video(cert, {TOKEN}) as server:
        sink = _sink(server, cert)
        try:
            wait_until(lambda: sink.snapshot()["connect_failures"] >= 2, 5)
            assert not sink.connected and sink.snapshot()["tier0_video"] == "DISCONNECTED"
            assert server.handshakes == 0
            failed = state.connects
            state.errors.clear()
            wait_until(lambda: sink.connected, 5)
            assert state.closed >= failed  # 다시 시도한 소켓까지 하나도 남기지 않았다
        finally:
            sink.close()


@pytest.mark.parametrize("error", [wsa(10048), wsa(10061), ConnectionRefusedError("refused"), TimeoutError("t")],
                         ids=["10048", "10061", "refused", "timeout"])
def test_other_connect_errors_behave_as_before(cert, monkeypatch, error):
    """음성 대조: 10014 가 아닌 오류는 다시 시도하지 않고 바로 기존 실패·재접속으로 간다(연결 시도 1번 = 실패 1번)."""
    _platform(monkeypatch, "win32")
    state = _flaky_connect(monkeypatch, [error])
    with FakeTier0Video(cert, {TOKEN}) as server:
        sink = _sink(server, cert)
        try:
            wait_until(lambda: sink.connected, 5)
            assert sink.snapshot()["connect_failures"] == 1 and state.connects == 2
        finally:
            sink.close()


def test_off_windows_10014_is_not_retried(cert, monkeypatch):
    """Linux 등에서는 공용 함수가 아무것도 하지 않는다: 10014 꼴의 오류도 기존 실패·재접속으로 간다."""
    _platform(monkeypatch, "linux")
    state = _flaky_connect(monkeypatch, [wsa(10014)])
    with FakeTier0Video(cert, {TOKEN}) as server:
        sink = _sink(server, cert)
        try:
            wait_until(lambda: sink.connected, 5)
            assert sink.snapshot()["connect_failures"] == 1 and state.connects == 2
        finally:
            sink.close()


def test_retry_delay_stays_inside_the_send_queue_window(cert, monkeypatch):
    """반례(경계): 예산을 끝까지 쓰는 10014 묶음에서도 한 번의 _open 지연이 송신 큐 창 1 s(25 프레임)보다 짧다(실제 시계)."""
    _platform(monkeypatch, "win32")
    _flaky_connect(monkeypatch, [wsa(10014)] * 1000)
    sink = TlsCameraSink("cam_A", lambda: None, device_token=TOKEN, pins=frozenset({cert.fingerprint}))
    try:
        started = time.monotonic()
        with pytest.raises(OSError):
            sink._open(VideoTarget("127.0.0.1", 9, cert.fingerprint))  # noqa: SLF001 - 시험
        assert time.monotonic() - started < 1.0
    finally:
        sink.close()
