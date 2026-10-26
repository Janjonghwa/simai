"""시계 차이 교환 응답 (명세 3.9, 계약 clock_sync_request·clock_sync_response, S15P31A101-289).

Tier 0 가 보내는 clock_sync_request 에 단말이 받은 즉시 clock_sync_response 로 답하는지 본다.
- 응답 필드: exchange_id·t1_ms 는 그대로, t2 = 수신 시각, t3 = 보내기 직전 시각(t3 ≥ t2), 계약 스키마 통과.
- 여러 요청은 받은 순서대로 하나씩 답한다. 경보 잠금을 잡고 있어도 기다리지 않는다.
- 형식이 틀린 요청은 거부로 기록만 하고 답하지 않으며 연결을 끊지 않는다.
- 실제 WebSocket 제어 채널에서 끊겼다 다시 붙어도 답한다.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from conftest import LAPTOP_ID, load_example
from test_transport_app import Tier0Stub, wait_until

from simai_site_alert.app import SiteAlertApp
from simai_site_alert.clock import FakeClock
from simai_site_alert.core import Outcome, SiteAlertCore
from simai_site_alert.messages import CLOCK_SYNC_SCHEMA_VERSION
from simai_site_alert.sound import FakeSoundBackend
from simai_site_alert.transport import InMemoryControlChannel, WebSocketControlChannel


class SteppingClock(FakeClock):
    """now_ms 를 읽을 때마다 step_ms 씩 흐른다(t2 와 t3 가 다른 시각에 읽혔는지 드러낸다)."""

    def __init__(self, step_ms: int = 3) -> None:
        super().__init__()
        self.step_ms = step_ms
        self.reads: list[int] = []

    def now_ms(self) -> int:
        value = super().now_ms()
        self.reads.append(value)
        self.advance(self.step_ms / 1000)
        return value


class BackwardsClock(FakeClock):
    """두 번째 읽기부터 시계가 뒤로 뛴다(t3 < t2)."""

    def __init__(self) -> None:
        super().__init__()
        self._first = True

    def now_ms(self) -> int:
        value = super().now_ms()
        if self._first:
            self._first = False
            self.advance(-5.0)
        return value


def request(t1_ms: int = 1_760_342_399_827, exchange_id: str | None = None) -> dict:
    msg = load_example("clock_sync_request")
    msg["msg_id"] = str(uuid.uuid4())
    msg["payload"] = {"exchange_id": exchange_id or str(uuid.uuid4()), "t1_ms": t1_ms}
    return msg


def make_app(clock: FakeClock) -> tuple[SiteAlertApp, SiteAlertCore, InMemoryControlChannel]:
    core = SiteAlertCore(FakeSoundBackend(), laptop_id=LAPTOP_ID, clock=clock)
    channel = InMemoryControlChannel()
    app = SiteAlertApp(core, channel, tick_s=60, clock=clock)
    app.start(test_alert_s=None)
    channel.set_connected(True)
    return app, core, channel


def test_response_fields_and_contract(contract_validator) -> None:
    clock = SteppingClock()
    app, _core, channel = make_app(clock)
    try:
        req = load_example("clock_sync_request")
        contract_validator("clock_sync_request", req)
        start = len(clock.reads)
        channel.deliver(req)
        [resp] = channel.sent
        contract_validator("clock_sync_response", resp)
        p = resp["payload"]
        assert resp["msg_type"] == "clock_sync_response" and resp["source_id"] == LAPTOP_ID
        assert resp["schema_version"] == CLOCK_SYNC_SCHEMA_VERSION == load_example("clock_sync_response")["schema_version"]
        assert resp["msg_id"] != req["msg_id"]
        assert (p["exchange_id"], p["t1_ms"]) == (req["payload"]["exchange_id"], req["payload"]["t1_ms"])
        # t2 는 수신 콜백에 들어온 첫 읽기, t3 는 그 뒤(보내기 직전) 읽기. 봉투 sent_at 은 t3
        assert p["t2_ms"] == clock.reads[start] and p["t3_ms"] > p["t2_ms"]
        assert resp["sent_at"] == p["t3_ms"]
    finally:
        app.close()


def test_multiple_requests_answered_in_order(contract_validator) -> None:
    app, _core, channel = make_app(SteppingClock())
    try:
        reqs = [request(t1_ms=1_760_342_400_000 + i * 1000) for i in range(5)]
        for r in reqs:
            channel.deliver(r)
        assert [m["msg_type"] for m in channel.sent] == ["clock_sync_response"] * 5
        assert [(m["payload"]["exchange_id"], m["payload"]["t1_ms"]) for m in channel.sent] == [
            (r["payload"]["exchange_id"], r["payload"]["t1_ms"]) for r in reqs]
        t2s = [m["payload"]["t2_ms"] for m in channel.sent]
        assert t2s == sorted(t2s) and len({m["msg_id"] for m in channel.sent}) == 5
        for m in channel.sent:
            contract_validator("clock_sync_response", m)
            assert m["payload"]["t3_ms"] >= m["payload"]["t2_ms"]
    finally:
        app.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"t1_ms": 1_760_342_399_827},  # exchange_id 없음
        {"exchange_id": "7d3f0c2e-5b1a-4c8e-9f6d-2a1b3c4d5e6f"},  # t1_ms 없음
        {"exchange_id": "not-a-uuid", "t1_ms": 1},
        {"exchange_id": "7d3f0c2e-5b1a-4c8e-9f6d-2a1b3c4d5e6f", "t1_ms": "1"},
        {"exchange_id": "7d3f0c2e-5b1a-4c8e-9f6d-2a1b3c4d5e6f", "t1_ms": -1},
        {"exchange_id": "7d3f0c2e-5b1a-4c8e-9f6d-2a1b3c4d5e6f", "t1_ms": True},
    ],
)
def test_malformed_request_not_answered_but_recorded(payload: dict) -> None:
    app, core, channel = make_app(SteppingClock())
    try:
        bad = request()
        bad["payload"] = payload
        assert core.handle(bad).outcome is Outcome.REJECTED
        channel.deliver(bad)
        assert channel.sent == [] and core.rejected["MALFORMED"] == 2
        assert channel.connected and channel.resets == 0  # 연결은 끊지 않는다
        good = request()
        channel.deliver(good)  # 다음 정상 요청에는 답한다
        assert [m["payload"]["exchange_id"] for m in channel.sent] == [good["payload"]["exchange_id"]]
    finally:
        app.close()


def test_clock_jumping_back_sends_no_response() -> None:
    """t2 와 t3 사이 노트북 시계가 뒤로 뛰면(t3 < t2) 계약을 어기는 응답을 보내지 않는다."""
    app, _core, channel = make_app(BackwardsClock())
    try:
        channel.deliver(request())
        assert channel.sent == []
    finally:
        app.close()


def test_disconnected_request_dropped_then_answered_after_reconnect() -> None:
    app, _core, channel = make_app(SteppingClock())
    try:
        channel.set_connected(False)
        channel.deliver(request())  # 끊긴 동안 받은 요청의 응답은 쌓아 두지 않는다(Tier 0 가 새 교환을 시작한다)
        channel.set_connected(True)
        assert channel.sent == []
        fresh = request()
        channel.deliver(fresh)
        assert [m["payload"]["exchange_id"] for m in channel.sent] == [fresh["payload"]["exchange_id"]]
    finally:
        app.close()


def test_answer_does_not_wait_for_alarm_lock() -> None:
    """경보 잠금을 다른 스레드가 잡고 있어도 응답은 기다리지 않고 나간다."""
    app, core, channel = make_app(SteppingClock())
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with core._lock:  # noqa: SLF001 - 시험: 경보 처리 중 흉내
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert held.wait(5)
        done = threading.Event()
        threading.Thread(target=lambda: (channel.deliver(request()), done.set()), daemon=True).start()
        assert done.wait(2), "경보 잠금을 기다렸다"
        assert [m["msg_type"] for m in channel.sent] == ["clock_sync_response"]
    finally:
        release.set()
        holder.join(5)
        app.close()


def test_websocket_answers_and_keeps_answering_after_reconnect(contract_validator) -> None:
    """실제 제어 채널: 접속마다 요청 둘(하나는 형식 오류)을 받는다. 형식 오류로 끊지 않고, 다시 붙은 뒤에도 답한다."""
    good, bad = request(), request()
    del bad["payload"]["exchange_id"]
    with Tier0Stub([bad, good]) as stub:
        core = SiteAlertCore(FakeSoundBackend(), laptop_id=LAPTOP_ID)
        channel = WebSocketControlChannel(f"ws://127.0.0.1:{stub.port}/control", device_token="tok-289",
                                          reconnect_min_s=0.05, reconnect_max_s=0.1)
        app = SiteAlertApp(core, channel)
        app.start(test_alert_s=None)
        try:
            wait_until(lambda: len(stub.received) == 1)
            assert len(stub._conns) == 1  # noqa: SLF001 - 형식 오류 요청으로 끊지 않았다
            stub.drop_connections()
            wait_until(lambda: len(stub.received) == 2)
            for resp in stub.received:
                contract_validator("clock_sync_response", resp)
                p = resp["payload"]
                assert (p["exchange_id"], p["t1_ms"]) == (good["payload"]["exchange_id"], good["payload"]["t1_ms"])
                assert p["t3_ms"] >= p["t2_ms"]
            assert stub.received[0]["msg_id"] != stub.received[1]["msg_id"]
            assert core.rejected["MALFORMED"] == 2
        finally:
            app.close()
