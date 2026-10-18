"""장애 상태 기계 시험 도구: 벽시계를 따로 움직일 수 있는 가짜 시계, health 봉투, 0.5초 단위로 도는 조립 하네스."""

from __future__ import annotations

import uuid

from simai_site_alert.app import SiteAlertApp
from simai_site_alert.clock import FakeClock
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.link_status import RelayReport, SiteLinkMonitor
from simai_site_alert.sound import FakeSoundBackend
from simai_site_alert.transport import InMemoryControlChannel

TICK_S = 0.5
CAMS = ("cam1", "cam2", "cam3", "cam4")


class SplitClock(FakeClock):
    """단조 시계와 벽시계를 따로 움직인다(벽시계가 틀리거나 뛰는 경우)."""

    def __init__(self, start_ms: int = 1_760_342_400_000) -> None:
        super().__init__(start_ms)
        self.wall_offset_ms = 0

    def now_ms(self) -> int:
        return super().now_ms() + self.wall_offset_ms


def health(t_latest: int | None, *, system_health: str | None = "NOMINAL", sent_at: int = 1_760_342_400_000,
           alerts: list | None = None) -> dict:
    payload: dict = {"cameras": [], "pipeline_lag_ms": 120, "calibration_state": "CALIBRATED",
                     "t_latest_global": t_latest}
    if system_health is not None:
        payload["system_health"] = system_health
    if alerts is not None:
        payload["active_site_alerts"] = alerts
        payload["active_site_alerts_as_of"] = sent_at
    return {"schema_version": "1.4.0", "msg_type": "health", "msg_id": str(uuid.uuid4()), "sent_at": sent_at,
            "source_id": "tier0-01", "payload": payload}


def relay(video: tuple[str, ...] = CAMS, count: int = 4) -> RelayReport:
    cams = tuple({"camera_id": c, "rtsp": "CONNECTED", "stream_epoch": 5, "last_frame_sequence": 100} for c in CAMS[:count])
    return RelayReport(tuple(video), count, cams)


class Harness:
    """InMemory 제어 채널 + 가짜 시계 + 수동 tick. step() 한 번이 0.5초다.

    Tier 0 는 health 를 1초마다(t_latest_global 은 매번 1000 씩 증가), 중계는 1초마다 보고, EC2 는 1초마다 2xx 를 준다.
    각 흐름은 플래그로 끈다.
    """

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock or SplitClock()
        self.sound = FakeSoundBackend()
        self.core = SiteAlertCore(self.sound, laptop_id="laptop-01", clock=self.clock)
        self.channel = InMemoryControlChannel(monotonic=self.clock.monotonic)
        self.monitor = SiteLinkMonitor()
        self.app = SiteAlertApp(self.core, self.channel, tick_s=3600, monitor=self.monitor, clock=self.clock)
        self.app.start(test_alert_s=None)
        self.t_latest = 1_760_342_000_000
        self.health_on = True
        self.progress_on = True
        self.system_health: str | None = "NOMINAL"
        self.relay_on = True
        self.video: tuple[str, ...] = CAMS
        self.ec2_on = True
        self.auto_reconnect = True  # 실제 제어 채널처럼 끊기면(reset 포함) 다음 step 에 다시 붙는다
        self.n = 0
        self.last_health_at: float | None = None
        self.last_relay_at: float | None = None
        self.last_ec2_ok_at: float | None = None
        self.history: list[tuple[float, str]] = []

    @property
    def now(self) -> float:
        return self.clock.monotonic()

    def connect(self) -> None:
        self.channel.set_connected(True)

    def send_health(self) -> None:
        if self.progress_on:
            self.t_latest += 1000
        self.channel.deliver(health(self.t_latest, system_health=self.system_health, sent_at=self.clock.now_ms()))
        self.last_health_at = self.now

    def step(self) -> str:
        self.clock.advance(TICK_S)
        self.n += 1
        if self.auto_reconnect and not self.channel.connected:
            self.connect()
        if self.n % 2 == 0:
            if self.health_on and self.channel.connected:
                self.send_health()
            if self.relay_on:
                self.app.on_relay_report(relay(self.video), self.now)
                self.last_relay_at = self.now
            if self.ec2_on:
                self.app._on_ec2_result(True, self.now, self.clock.now_ms())  # noqa: SLF001 - 시험
                self.last_ec2_ok_at = self.now
            else:
                self.app._on_ec2_result(False, self.now, self.clock.now_ms())  # noqa: SLF001 - 시험
        self.app._monitor_tick()  # noqa: SLF001 - 시험(수동 tick)
        display = self.display
        self.history.append((self.now, display))
        return display

    def run(self, seconds: float) -> list[str]:
        return [self.step() for _ in range(round(seconds / TICK_S))]

    @property
    def display(self) -> str:
        site = self.core.screen_state().site
        assert site is not None
        return site.site_display

    @property
    def site(self):
        return self.core.screen_state().site

    def seconds_until(self, display: str, limit_s: float = 10.0) -> float:
        """display 가 될 때까지 step 하고 걸린 시간(초)."""
        start = self.now
        while self.display != display:
            if self.now - start > limit_s:
                raise AssertionError(f"{limit_s}초 안에 {display} 가 되지 않았다 (지금 {self.display})")
            self.step()
        return self.now - start

    def close(self) -> None:
        self.app.close()


def healthy_harness() -> Harness:
    h = Harness()
    h.connect()
    h.seconds_until("NORMAL")
    return h
