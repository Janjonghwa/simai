"""실제 시간 종단 시험: 로컬 Tier 0 제어 서버(WebSocket, 1 Hz health) + 영상 중계 상태(UDP) + 로컬 HTTPS EC2 를 띄우고
단절 시나리오마다 현장 표시가 5초 안에 바뀌는지(G3), 반쯤 열린 제어 연결을 끊고 다시 붙는지, 두 하트비트가 계약을
통과하는지 본다. 약 30초 걸린다."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import threading
import time

import pytest
from tls_helpers import HttpsEc2, make_cert, wait_until

from simai_site_alert.app import SiteAlertApp
from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.clock import SystemClock
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.heartbeat import Ec2HeartbeatClient
from simai_site_alert.link_status import CONTROL_HALTED, NORMAL, REMOTE_FAULT, SiteLinkMonitor
from simai_site_alert.relay_status import RelayStatusReceiver, build_report, new_boot_id
from simai_site_alert.sound import FakeSoundBackend
from simai_site_alert.transport import WebSocketControlChannel

TOKEN = "e2e-device-token"
G3_SITE_S = 5.0


class HealthTier0:
    """1 Hz 로 health 를 보내는 Tier 0 제어 서버. silence_current() 는 지금 연결만 조용하게 만든다(반쯤 열린 연결 흉내)."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.connections = 0
        self.port: int | None = None
        self.last_health_at: float | None = None
        self._silent: set = set()
        self._conns: list = []
        self._t = 1_760_342_000_000
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def _health(self) -> str:
        self._t += 1000
        return json.dumps({"schema_version": "1.4.0", "msg_type": "health", "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000001",
                           "sent_at": int(time.time() * 1000), "source_id": "tier0-01",
                           "payload": {"system_health": "NOMINAL", "t_latest_global": self._t, "cameras": [],
                                       "pipeline_lag_ms": 100, "calibration_state": "CALIBRATED",
                                       "active_site_alerts": [], "active_site_alerts_as_of": int(time.time() * 1000) - 5000}})

    async def _sender(self, ws) -> None:
        while True:
            if ws not in self._silent:
                await ws.send(self._health())
                self.last_health_at = time.monotonic()
            await asyncio.sleep(1.0)

    async def _handler(self, ws) -> None:
        from websockets.exceptions import ConnectionClosed

        self.connections += 1
        self._conns.append(ws)
        task = asyncio.ensure_future(self._sender(ws))
        try:
            async for raw in ws:
                self.received.append(json.loads(raw))
        except ConnectionClosed:
            pass
        finally:
            task.cancel()

    async def _main(self) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        async with serve(self._handler, "127.0.0.1", 0, ping_interval=None) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._loop.run_in_executor(None, self._stop.wait)

    def silence_current(self) -> None:
        self._silent.update(self._conns)

    def send(self, message: dict) -> None:
        ws = self._conns[-1]
        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(message)), self._loop).result(5)

    def drop_all(self) -> None:
        for ws in list(self._conns):
            asyncio.run_coroutine_threadsafe(ws.close(), self._loop).result(5)

    def __enter__(self) -> HealthTier0:
        self._thread.start()
        self._ready.wait(5)
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(5)


class RelayReporter:
    """영상 중계 흉내: 1초마다 상태를 UDP 로 보낸다."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.on = True
        self.last_at: float | None = None
        self._boot = new_boot_id()
        self._seq = 0
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(1.0 if self._seq else 0):
            if self.on:
                cams = [{"camera_id": c, "rtsp": "CONNECTED"} for c in ("cam1", "cam2")]
                self._sock.sendto(build_report(boot=self._boot, seq=self._seq, camera_count=2,
                                               video_cameras=["cam1", "cam2"], cameras=cams), ("127.0.0.1", self.port))
                self.last_at = time.monotonic()
            self._seq += 1

    def __enter__(self) -> RelayReporter:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(3)
        self._sock.close()


def wait_display(core: SiteAlertCore, display: str, timeout: float) -> float:
    start = time.monotonic()
    wait_until(lambda: core.screen_state().site.site_display == display, timeout)
    return time.monotonic()


def test_end_to_end_fault_displays_within_5s(tmp_path, contract_validator) -> None:
    cert = make_cert(tmp_path, "ec2", loopback_san=True)
    clock = SystemClock()
    sound = FakeSoundBackend()
    core = SiteAlertCore(sound, laptop_id="laptop-01", clock=clock)
    monitor = SiteLinkMonitor()
    with HealthTier0() as tier0, HttpsEc2(cert) as ec2:
        channel = WebSocketControlChannel(f"ws://127.0.0.1:{tier0.port}/control", device_token=TOKEN,
                                          reconnect_min_s=0.1, reconnect_max_s=0.5)
        client = Ec2HeartbeatClient(ec2.base_url, device_token=TOKEN, timeout_s=1.0,
                                    ssl_context=ssl.create_default_context(cafile=str(cert.cert_file)))
        app = SiteAlertApp(core, channel, monitor=monitor, ec2=client, clock=clock)
        rx = RelayStatusReceiver(app.on_relay_report, port=0, monotonic=clock.monotonic)
        rx.start()
        try:
            with RelayReporter(rx.port) as relay:
                app.start(test_alert_s=None)
                assert core.screen_state().site.site_display == CONTROL_HALTED  # 시작 직후는 모른다
                wait_display(core, NORMAL, 8)

                # 1) EC2 만 막힘(응답 없음) → 원격 관제 장애, 그동안 현장 경보는 바로 울린다
                ec2.mode = "delay:30"
                last_ok = time.monotonic()
                t = wait_display(core, REMOTE_FAULT, 6)
                assert t - last_ok <= G3_SITE_S
                eid = uuid7()
                tier0.send(local_alert(eid, 1, "RAISED"))
                wait_until(lambda: eid in core.active_alerts(), 1.0)
                assert sound.playing.name == "CRITICAL"
                assert core.screen_state().site.site_display == REMOTE_FAULT  # 관제 중단으로 잘못 표시하지 않는다
                ec2.mode = "ok"
                wait_display(core, NORMAL, 8)

                # 2) 반쯤 열린 제어 연결: 소켓은 열린 채 health 가 끊김 → 관제 중단(5초 안), 끊고 다시 붙어 복구
                conns = tier0.connections
                tier0.silence_current()
                last_health = tier0.last_health_at
                t = wait_display(core, CONTROL_HALTED, 6)
                assert t - last_health <= G3_SITE_S
                wait_until(lambda: tier0.connections > conns, 3)  # 노트북이 끊고 다시 붙었다
                wait_display(core, NORMAL, 8)

                # 3) 영상 중계 보고가 끊김 → 관제 중단(5초 안)
                relay.on = False
                last_relay = relay.last_at
                t = wait_display(core, CONTROL_HALTED, 6)
                assert t - last_relay <= G3_SITE_S
                relay.on = True
                wait_display(core, NORMAL, 8)

                # 4) Tier 0 가 연결을 닫음 → 바로 관제 중단
                closed_at = time.monotonic()
                tier0.drop_all()
                t = wait_display(core, CONTROL_HALTED, 3)
                assert t - closed_at <= 1.0
        finally:
            app.close()
            rx.close()

        # 두 하트비트 모두 계약을 통과하고, 토큰은 인증 헤더에만 있다
        to_tier0 = [m for m in tier0.received if m.get("msg_type") == "laptop_heartbeat"]
        assert len(to_tier0) >= 10
        for m in to_tier0:
            contract_validator("laptop_heartbeat", m)
            assert "ec2" in m["payload"]
        to_ec2 = [json.loads(r["body"]) for r in ec2.requests]
        assert len(to_ec2) >= 10
        for m in to_ec2:
            contract_validator("laptop_heartbeat", m)
            assert "ec2" not in m["payload"]
        assert {r["headers"]["Authorization"] for r in ec2.requests} == {f"Bearer {TOKEN}"}
        assert all(TOKEN not in r["body"].decode() for r in ec2.requests)
        assert {m["payload"]["site_display"] for m in to_ec2} >= {NORMAL, CONTROL_HALTED}
        seqs = [m["payload"]["heartbeat_seq"] for m in to_ec2]
        assert seqs == sorted(seqs)


@pytest.fixture(autouse=True)
def _quiet_websockets_logs(caplog):
    caplog.set_level("WARNING")
