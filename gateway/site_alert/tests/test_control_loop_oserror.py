"""제어 루프 생성이 계속 OSError 로 실패하는 동안의 단말 종합 시험 (S15P31A101-202).

transport.start 의 이벤트 루프 생성이 WinError 10014(예산 소진)나 10048 같은 다른 OSError 로 계속 실패해도, 단말 전체(관제 중단 표시,
EC2 하트비트, tick, 재접속 간격, 스레드, close)가 정상이고 오류가 그치면 다시 붙는지 실제 시간으로 본다. 약 4초 걸린다.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from test_heartbeat_e2e import HealthTier0
from test_winsock_retry import _flaky_loops, wsa
from tls_helpers import wait_until

from simai_site_alert import transport, winsock
from simai_site_alert.app import SiteAlertApp
from simai_site_alert.clock import SystemClock
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.link_status import CONTROL_HALTED, SiteLinkMonitor
from simai_site_alert.sound import FakeSoundBackend
from simai_site_alert.transport import WebSocketControlChannel

RECONNECT_S = 0.4
FAIL_WINDOW_S = 2.2
OWN_THREADS = ("i0-control", "site-alert-tick", "ec2-heartbeat")


class FakeEc2:
    """응답 200 만 하는 EC2 흉내(하트비트 시각만 쌓는다)."""

    def __init__(self) -> None:
        self.at: list[float] = []

    def post(self, envelope: dict) -> int:
        self.at.append(time.monotonic())
        return 200


def _own_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name in OWN_THREADS and t.is_alive()]


@pytest.mark.parametrize("error", [wsa(10014), wsa(10048)], ids=["10014-budget-exhausted", "10048"])
def test_persistent_loop_creation_error_keeps_terminal_alive(monkeypatch, error) -> None:
    monkeypatch.setattr(winsock, "sys", SimpleNamespace(platform="win32"))  # 10014 은 Windows 에서만 다시 시도한다
    stamps: list[float] = []
    ticks: list[float] = []
    clock = SystemClock()
    core = SiteAlertCore(FakeSoundBackend(), laptop_id="laptop-01", clock=clock)
    real_tick = core.tick
    core.tick = lambda: (ticks.append(time.monotonic()), real_tick())[1]  # type: ignore[method-assign]
    ec2 = FakeEc2()
    before = {t.ident for t in _own_threads()}
    with HealthTier0() as tier0:
        state = _flaky_loops(monkeypatch, [error] * 100_000)
        inner = transport.asyncio.new_event_loop
        transport.asyncio.new_event_loop = lambda: (stamps.append(time.monotonic()), inner())[1]
        channel = WebSocketControlChannel(f"ws://127.0.0.1:{tier0.port}/control", device_token="tok",
                                          reconnect_min_s=RECONNECT_S, reconnect_max_s=RECONNECT_S)
        app = SiteAlertApp(core, channel, monitor=SiteLinkMonitor(), ec2=ec2, clock=clock)  # type: ignore[arg-type]
        closed = False
        try:
            began = time.monotonic()
            starter = threading.Thread(target=lambda: app.start(test_alert_s=None), daemon=True)
            starter.start()
            starter.join(2)
            assert not starter.is_alive()  # start() 가 영원히 기다리지 않는다(고치기 전: 루프 스레드가 죽어 멈췄다)
            displays = []
            while time.monotonic() - began < FAIL_WINDOW_S:
                displays.append(core.screen_state().site.site_display)
                time.sleep(0.1)
            assert set(displays) == {CONTROL_HALTED}  # 모르는 상태를 정상으로 표시하지 않는다
            assert channel.send({"msg_type": "x"}) is False and tier0.connections == 0

            # 하트비트(EC2): 약 1초 주기를 유지한다(제어 루프 실패가 하트비트 스레드를 막지 않는다)
            gaps = [b - a for a, b in zip(ec2.at, ec2.at[1:])]
            assert len(ec2.at) >= 2 and all(0.5 <= g <= 1.6 for g in gaps), gaps
            # tick: 0.5초 주기로 멈춤 없이 돈다
            assert len(ticks) >= 3 and max(b - a for a, b in zip(ticks, ticks[1:])) < 1.0
            # 재접속 간격: 루프 생성 시도 묶음(10014 재시도 포함) 사이가 재접속 간격 이상이다. 빠른 무한 재시도가 아니다
            starts = [s for i, s in enumerate(stamps) if i == 0 or s - stamps[i - 1] > 0.1]
            assert 2 <= len(starts) <= FAIL_WINDOW_S / RECONNECT_S + 2, len(starts)
            assert all(b - a >= RECONNECT_S * 0.9 for a, b in zip(starts, starts[1:])), starts
            assert sum(t.name == "i0-control" for t in _own_threads()) == 1  # 루프 스레드 하나(재시도마다 늘지 않는다)

            # 오류가 그치면 다시 붙고 Tier 0 로 하트비트가 간다
            state.errors.clear()
            wait_until(lambda: tier0.connections == 1, 3)
            wait_until(lambda: any(m.get("msg_type") == "laptop_heartbeat" for m in tier0.received), 3)

            started = time.monotonic()
            app.close()
            closed = True
            assert time.monotonic() - started < 6
        finally:
            if not closed:
                app.close()
        wait_until(lambda: {t.ident for t in _own_threads()} <= before, 3)  # 스레드 누수 없음


def test_close_while_creation_keeps_failing_ends_within_limit(monkeypatch) -> None:
    """반례(종료 순서): 루프 생성이 계속 실패하는 단말의 close() 는 제한 시간 안에 끝나고 모든 스레드가 사라진다."""
    before = {t.ident for t in _own_threads()}
    _flaky_loops(monkeypatch, [wsa(10048)] * 100_000)
    clock = SystemClock()
    core = SiteAlertCore(FakeSoundBackend(), laptop_id="laptop-01", clock=clock)
    channel = WebSocketControlChannel("ws://127.0.0.1:9/control", device_token="tok", reconnect_min_s=5.0)
    app = SiteAlertApp(core, channel, monitor=SiteLinkMonitor(), ec2=FakeEc2(), clock=clock)  # type: ignore[arg-type]
    starter = threading.Thread(target=lambda: app.start(test_alert_s=None), daemon=True)
    starter.start()
    starter.join(2)
    assert not starter.is_alive()
    time.sleep(0.3)  # 재접속 간격(5초)을 기다리는 중
    started = time.monotonic()
    app.close()
    assert time.monotonic() - started < 2  # 재접속 간격을 다 기다리지 않는다
    wait_until(lambda: {t.ident for t in _own_threads()} <= before, 3)
