"""조립: 제어 채널 → 코어 → 소리·화면. 경보 판단은 코어에만, 장애 판단은 link_status(SiteLinkMonitor)에만 있다.

장애 감시(monitor 를 주면, S15P31A101-91):
- 제어 채널 수신·연결, 영상 중계 상태 보고, EC2 하트비트 결과를 monitor 에 넣고, 주기 tick(0.5초)과 health 수신 때
  평가해 화면(core.set_site_status)에 넘긴다.
- 노트북 → Tier 0 하트비트: 연결돼 있으면 1초마다 laptop_heartbeat(ec2 포함)를 제어 채널로 보낸다(기다리지 않음).
- 노트북 → EC2 하트비트: Ec2HeartbeatSender 가 따로 도는 스레드에서 1초마다 보낸다(ec2 빼고).
- 연결은 살아 있는데 Tier 0 health 가 3초 넘게 없으면 제어 채널을 끊고 다시 붙는다(반쯤 열린 연결, 명세 3.2).
- 현장 경보 경로(제어 채널 수신 → 코어 → 소리)는 장애 감시를 기다리지 않는다. 메시지는 코어에 먼저 넣고 그 뒤에 감시에 알린다.
- 시계 차이 교환(명세 3.9, S15P31A101-289): clock_sync_request 를 받으면 수신 콜백 안에서 바로 clock_sync_response 를 보낸다
  (t2 = 콜백에 들어온 시각, t3 = 보내기 직전 시각, 노트북 벽시계). 경보 잠금·tick·하트비트를 기다리지 않는다.
  형식이 틀린 요청은 코어가 거부로 기록만 하고 답하지 않는다(연결은 유지).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from simai_site_alert.clock import Clock, SystemClock
from simai_site_alert.core import ScreenState, SiteAlertCore
from simai_site_alert.heartbeat import Ec2HeartbeatClient, Ec2HeartbeatSender
from simai_site_alert.link_status import RelayReport, SiteLinkMonitor, SiteStatus
from simai_site_alert.messages import ClockSyncRequest, build_clock_sync_response, build_laptop_heartbeat
from simai_site_alert.transport import ControlChannel

log = logging.getLogger(__name__)

DEFAULT_TEST_ALERT_S = 4.0
DEFAULT_TICK_S = 0.5  # 장애 표시가 3초 누락 판정 뒤 0.5초 안에 바뀌게 한다(G3 현장 5초)
TIER0_HEARTBEAT_INTERVAL_S = 1.0


class SiteAlertApp:
    def __init__(
        self,
        core: SiteAlertCore,
        channel: ControlChannel | None,
        tick_s: float = DEFAULT_TICK_S,
        *,
        monitor: SiteLinkMonitor | None = None,
        ec2: Ec2HeartbeatClient | None = None,
        clock: Clock | None = None,
        tier0_heartbeat_interval_s: float = TIER0_HEARTBEAT_INTERVAL_S,
        ec2_interval_s: float = 1.0,
    ) -> None:
        self.core = core
        self.channel = channel
        self.monitor = monitor
        self.clock = clock or SystemClock()
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._timers: list[threading.Timer] = []
        self._ticker: threading.Thread | None = None
        self._status_lock = threading.Lock()  # 평가 → 화면 넘기기를 한 줄로(늦은 평가가 새 상태를 덮지 않게)
        self._tier0_hb_interval_s = tier0_heartbeat_interval_s
        self._last_tier0_hb: float | None = None
        self.ec2_sender: Ec2HeartbeatSender | None = None
        if ec2 is not None:
            if monitor is None:
                raise ValueError("EC2 하트비트는 장애 감시(monitor)와 함께 쓴다")
            self.ec2_sender = Ec2HeartbeatSender(
                ec2, lambda: self.build_heartbeat(include_ec2=False), self._on_ec2_result,
                interval_s=ec2_interval_s, monotonic=self.clock.monotonic, now_ms=self.clock.now_ms,
            )
        self.publish_status()  # 화면을 붙이기 전에 첫 상태(모름 = 관제 중단)를 넣어 둔다

    def add_display(self, update: Callable[[ScreenState], None]) -> None:
        self.core.add_listener(update)

    def start(self, *, test_alert_s: float | None = DEFAULT_TEST_ALERT_S) -> None:
        """시험 경보(선택) → 제어 채널 시작 → 주기 tick. 시험 경보는 제어 채널과 상관없이 먼저 울린다(18.2 시작 절차)."""
        if test_alert_s:
            self.test_alert(test_alert_s)
        self.publish_status()
        if self.channel is not None:
            self.channel.start(self._on_message, self._on_connected)
        if self.ec2_sender is not None:
            self.ec2_sender.start()
        self._ticker = threading.Thread(target=self._tick_loop, name="site-alert-tick", daemon=True)
        self._ticker.start()

    # ---- 제어 채널 ------------------------------------------------------------------------------------------------

    def _on_message(self, raw: str | bytes, received_mono: float) -> None:
        received_ms = self.clock.now_ms()  # clock_sync_request 의 t2(수신 시각). 해석보다 먼저 잰다
        result = self.core.handle_text(raw, received_mono)  # 현장 경보 먼저
        if result.clock_sync is not None:
            self._answer_clock_sync(result.clock_sync, received_ms)
        if self.monitor is not None:
            self.monitor.on_tier0_message(self.clock.monotonic(), self.clock.now_ms(), result.health)
            if result.health is not None:
                self.publish_status()

    def _answer_clock_sync(self, request: ClockSyncRequest, t2_ms: int) -> None:
        """받은 즉시 답한다. 연결이 없으면 버린다(Tier 0 는 재접속 때 새 교환을 시작한다)."""
        if self.channel is None:
            return
        t3_ms = self.clock.now_ms()  # 보내기 직전
        try:
            envelope = build_clock_sync_response(request=request, laptop_id=self.core.laptop_id, t2_ms=t2_ms, t3_ms=t3_ms)
        except ValueError:  # 그 사이 노트북 시계가 뒤로 뛰었다(t3 < t2). 계약을 어기는 응답을 보내지 않고 다음 교환을 기다린다
            log.warning("노트북 시계가 뒤로 뛰어 clock_sync_response 를 보내지 않았다 (exchange_id=%s)", request.exchange_id)
            return
        if not self.channel.send(envelope):
            log.info("제어 채널이 없어 clock_sync_response 를 버렸다 (exchange_id=%s)", request.exchange_id)

    def _on_connected(self, connected: bool) -> None:
        self.core.set_control_connected(connected)
        if self.monitor is not None:
            self.monitor.on_control_connected(connected, self.clock.monotonic())
            self.publish_status()
        if connected and self.channel is not None:
            for envelope in self.core.pending_site_acks():  # 끊긴 동안 누른 현장 확인을 같은 봉투로 다시 보낸다
                self.channel.send(envelope)

    # ---- 장애 감시 ------------------------------------------------------------------------------------------------

    def on_relay_report(self, report: RelayReport, received_mono: float) -> None:
        """영상 중계 상태 보고(relay_status.RelayStatusReceiver 가 부른다)."""
        if self.monitor is not None:
            self.monitor.on_relay_report(report, received_mono)

    def _on_ec2_result(self, ok: bool, mono: float, now_ms: int) -> None:
        assert self.monitor is not None
        self.monitor.on_ec2_result(ok, mono, now_ms)
        self.publish_status()

    def publish_status(self) -> SiteStatus | None:
        if self.monitor is None:
            return None
        with self._status_lock:
            status = self.monitor.evaluate(self.clock.monotonic())
            self.core.set_site_status(status)
            return status

    def build_heartbeat(self, *, include_ec2: bool) -> dict[str, Any] | None:
        """지금 상태로 laptop_heartbeat 봉투를 만든다(Tier 0 로 보내면 include_ec2=True)."""
        status = self.publish_status()
        if status is None:
            return None
        assert self.monitor is not None
        payload = self.monitor.heartbeat_payload(status, self.core.alarm_status(), include_ec2=include_ec2)
        return build_laptop_heartbeat(laptop_id=self.core.laptop_id, now_ms=self.clock.now_ms(), payload=payload)

    def _monitor_tick(self) -> None:
        assert self.monitor is not None
        self.publish_status()
        now = self.clock.monotonic()
        if self.channel is None:
            return
        if self.monitor.needs_control_reset(now):
            self.channel.reset()
        # tick 이 조금 늦거나 빨라도 1초 주기를 지키게 주기의 3/4 를 기준으로 본다(0.5초 tick 이면 1초마다)
        if self._last_tier0_hb is None or now - self._last_tier0_hb >= self._tier0_hb_interval_s * 0.75:
            envelope = self.build_heartbeat(include_ec2=True)
            if envelope is not None and self.channel.send(envelope):
                self._last_tier0_hb = now

    # ---- 현장 확인·시험 ------------------------------------------------------------------------------------------

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
            try:
                self.core.tick()
            except Exception:  # noqa: BLE001 - 소리 재시도 오류로 tick 을 멈추지 않는다
                log.exception("경보 tick 오류")
            if self.monitor is not None:
                try:
                    self._monitor_tick()
                except Exception as e:  # noqa: BLE001 - 장애 감시 오류가 소리 재시도를 멈추지 않게. 문장은 쓰지 않는다
                    log.error("장애 감시 tick 오류 (%s)", type(e).__name__)

    def close(self) -> None:
        self._stop.set()
        for t in self._timers:
            t.cancel()
        if self.ec2_sender is not None:
            self.ec2_sender.close()
        if self.channel is not None:
            self.channel.close()
