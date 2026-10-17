"""조립: 제어 채널 → 코어 → 소리·화면. 판단은 코어에만 있다."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from simai_site_alert.core import ScreenState, SiteAlertCore
from simai_site_alert.transport import ControlChannel

log = logging.getLogger(__name__)

DEFAULT_TEST_ALERT_S = 4.0


class SiteAlertApp:
    def __init__(self, core: SiteAlertCore, channel: ControlChannel | None, tick_s: float = 1.0) -> None:
        self.core = core
        self.channel = channel
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._timers: list[threading.Timer] = []
        self._ticker: threading.Thread | None = None

    def add_display(self, update: Callable[[ScreenState], None]) -> None:
        self.core.add_listener(update)

    def start(self, *, test_alert_s: float | None = DEFAULT_TEST_ALERT_S) -> None:
        """시험 경보(선택) → 제어 채널 시작 → 주기 tick. 시험 경보는 제어 채널과 상관없이 먼저 울린다(18.2 시작 절차)."""
        if test_alert_s:
            self.test_alert(test_alert_s)
        if self.channel is not None:
            self.channel.start(self._on_message, self._on_connected)
        self._ticker = threading.Thread(target=self._tick_loop, name="site-alert-tick", daemon=True)
        self._ticker.start()

    def _on_message(self, raw: str | bytes, received_mono: float) -> None:
        self.core.handle_text(raw, received_mono)

    def _on_connected(self, connected: bool) -> None:
        self.core.set_control_connected(connected)
        if connected and self.channel is not None:
            for envelope in self.core.pending_site_acks():  # 끊긴 동안 누른 현장 확인을 같은 봉투로 다시 보낸다
                self.channel.send(envelope)

    def site_ack(self, event_id: str) -> None:
        """현장 확인 버튼. 소리는 코어가 즉시 멈추고, 연결이 없으면 재접속 때 보낸다."""
        envelope = self.core.site_ack(event_id)
        if envelope is not None and self.channel is not None and not self.channel.send(envelope):
            log.warning("제어 채널이 없어 site_ack 를 재접속 뒤 보낸다 (event_id=%s)", event_id)

    def test_alert(self, seconds: float = DEFAULT_TEST_ALERT_S) -> bool:
        started = self.core.start_test_alert()
        timer = threading.Timer(seconds, self.core.end_test_alert)
        timer.daemon = True
        timer.start()
        self._timers.append(timer)
        return started

    def _tick_loop(self) -> None:
        while not self._stop.wait(self._tick_s):
            self.core.tick()

    def close(self) -> None:
        self._stop.set()
        for t in self._timers:
            t.cancel()
        if self.channel is not None:
            self.channel.close()
