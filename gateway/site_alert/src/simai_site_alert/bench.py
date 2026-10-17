"""수신 → 소리 시작 지연 측정 (부록 D-G0-04 '이벤트 → 소리 p95 300ms' 중 단말 몫).

같은 컴퓨터에 시험용 WebSocket 서버(Tier 0 흉내)를 띄우고, 단말의 실제 제어 채널 클라이언트로 붙는다.
서버가 local_alert RAISED 와 RESOLVED 를 번갈아 보내고, 단말은 RAISED 마다 소리를 시작·RESOLVED 마다 멈춘다.
재는 구간은 '제어 채널이 메시지를 받은 시각 → 소리 백엔드 start() 가 돌아온 시각'이다(JSON 해석·중복 판단·
코어 처리·백엔드 호출 포함). 네트워크(Tier 0 → 노트북)와 스피커 출력 지연은 들어가지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import statistics
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from simai_site_alert.app import SiteAlertApp
from simai_site_alert.clock import SystemClock
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.sound import SoundBackend
from simai_site_alert.transport import WebSocketControlChannel


def uuid7(ms: int | None = None) -> str:
    """UUIDv7 문자열(시험·벤치용 event_id). Python 3.11 표준 라이브러리에 uuid7 이 없어 직접 만든다."""
    ms = time.time_ns() // 1_000_000 if ms is None else ms
    rand = secrets.randbits(74)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | (rand >> 62) << 64 | 0b10 << 62 | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))


def local_alert(event_id: str, seq: int, lifecycle: str, *, severity: str = "CRITICAL", source: str = "LIVE",
                acknowledged: bool = False, now_ms: int | None = None, **overrides: Any) -> dict[str, Any]:
    now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    payload: dict[str, Any] = {
        "event_id": event_id,
        "event_sequence": seq,
        "lifecycle": lifecycle,
        "source": source,
        "severity": severity,
        "confidence": "HIGH",
        "needs_check": False,
        "rule_id": "R2",
        "zone_id": "z-04",
        "world_ids": ["w-0012"],
        "summary": "R2 가장자리 근접 0.82m (높이차 2.1m)",
        "observation_time": now_ms - 240,
        "evaluated_at": now_ms - 5,
        "acknowledged": acknowledged,
    }
    payload.update(overrides)
    return {
        "schema_version": "1.2.0",
        "msg_type": "local_alert",
        "msg_id": str(uuid.uuid4()),
        "sent_at": now_ms,
        "source_id": "tier0-01",
        "payload": payload,
    }


@dataclass(frozen=True)
class BenchResult:
    count: int
    samples_ms: list[float]

    @property
    def p50_ms(self) -> float:
        return statistics.median(self.samples_ms)

    @property
    def p95_ms(self) -> float:
        return percentile(self.samples_ms, 95)

    @property
    def max_ms(self) -> float:
        return max(self.samples_ms)


def percentile(values: list[float], q: float) -> float:
    """최근접 순위 백분위수."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))  # ceil
    return ordered[int(rank) - 1]


def run_bench(sound: SoundBackend, *, count: int = 300, interval_s: float = 0.02, timeout_s: float = 60.0) -> BenchResult:
    """count 번 RAISED → RESOLVED 를 보내고 RAISED 마다 수신 → 소리 start() 반환 지연을 모은다."""
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed

    loop_ready = threading.Event()
    done = threading.Event()
    state: dict[str, Any] = {}

    async def handler(ws) -> None:
        try:
            for _ in range(count):
                event_id = uuid7()
                await ws.send(json.dumps(local_alert(event_id, 1, "RAISED")))
                await asyncio.sleep(interval_s)
                await ws.send(json.dumps(local_alert(event_id, 2, "RESOLVED")))
                await asyncio.sleep(interval_s)
            await ws.wait_closed()
        except ConnectionClosed:
            pass

    async def server_main() -> None:
        async with serve(handler, "127.0.0.1", 0) as server:
            state["port"] = server.sockets[0].getsockname()[1]
            loop_ready.set()
            await asyncio.get_running_loop().run_in_executor(None, done.wait)

    server_thread = threading.Thread(target=lambda: asyncio.run(server_main()), daemon=True)
    server_thread.start()
    loop_ready.wait(10)

    clock = SystemClock()
    core = SiteAlertCore(sound, laptop_id="laptop-bench", clock=clock)
    channel = WebSocketControlChannel(f"ws://127.0.0.1:{state['port']}/control", device_token="bench-token",
                                      monotonic=clock.monotonic)
    app = SiteAlertApp(core, channel)
    app.start(test_alert_s=None)
    deadline = time.monotonic() + timeout_s
    try:
        # 마지막 RESOLVED 까지 받아 소리가 멈춘 뒤 닫는다
        while (len(core.latency_samples_s) < count or core.active_alerts()) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        app.close()
        done.set()
        server_thread.join(timeout=5)
    samples = [s * 1000 for s in core.latency_samples_s]
    if len(samples) < count:
        raise RuntimeError(f"{count} 건 중 {len(samples)} 건만 소리 시작을 기록했다 (rejected={dict(core.rejected)})")
    return BenchResult(count=count, samples_ms=samples)
