"""I1 클라이언트·스케줄러·디스패처 단위 시험 (S15P31A101-45). 실제 소켓 없이 가짜 연결로 순서·주기·버림·재연결을 본다."""

from __future__ import annotations

import copy
import json
import logging
import queue
import ssl
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.tier0.endpoint_registration import Backoff, EndpointConfigError  # noqa: E402
from services.tier0.i1_client import (  # noqa: E402
    Envelope,
    I1Client,
    I1ConfigError,
    I1Settings,
    QueueFull,
    SendFailed,
    WebSocketConnector,
    require_verifying,
)
from services.tier0.i1_dispatch import I1Dispatcher, wire_tier0_handlers  # noqa: E402
from services.tier0.i1_publisher import (  # noqa: E402
    EndpointI1Registration,
    I1Publisher,
    OutboxI1Transport,
    attach_outbox,
    build_i1_link,
)
from simai_ai.eventlog import TRANSITION_KIND, AckedOffsetStore, EventJournal, Outbox  # noqa: E402
from simai_ai.eventlog import TransportError as OutboxTransportError  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import FixedClock  # noqa: E402
from simai_ai.risk.alerting import ContractAck  # noqa: E402

REPO = AI_DIR.parent
EXAMPLES = REPO / "contracts" / "examples"
TOKEN = "i1-secret-token-9c2e/+="  # 인코딩하면 꼴이 바뀌는 문자를 넣는다
T0 = 1760342400000

# 가림 대상으로 등록한다(실제로는 WebSocketConnector 를 만들 때 등록된다)
WebSocketConnector("wss://ec2.example/ws", TOKEN)


def example(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def world_payload(t: int) -> dict:
    payload = copy.deepcopy(example("world_state")["payload"])
    payload["t_latest_global"] = t
    return payload


def health_payload() -> dict:
    payload = copy.deepcopy(example("health")["payload"])
    for key in ("active_site_alerts", "active_site_alerts_as_of", "config_sync", "config_version", "ec2_world_state_drops"):
        payload.pop(key, None)
    return payload


def transition_payload(seq: int) -> dict:
    payload = copy.deepcopy(example("risk_event_transition")["payload"])
    del payload["journal_offset"], payload["resent"]
    payload["event_sequence"] = seq
    payload["transition"] = "RAISED" if seq == 1 else "UPDATED"
    return payload


class FakeMono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def wait_until(cond, timeout: float = 3.0, step: float = 0.005) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class FakeConn:
    """가짜 연결. send_delay 로 느린 서버를 흉내 낸다. close 하면 수신 반복이 끝난다."""

    def __init__(self, send_delay: float = 0.0) -> None:
        self.sent: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()
        self.closed = threading.Event()
        self.send_delay = send_delay
        self.fail_sends = False
        self.close_error: BaseException | None = None

    def send(self, text: str) -> None:
        if self.closed.is_set() or self.fail_sends:
            raise ConnectionError(f"끊김 (close reason {TOKEN})")
        if self.send_delay:
            time.sleep(self.send_delay)
        self.sent.append(json.loads(text))

    def __iter__(self):
        while True:
            item = self.inbox.get()
            if item is None:
                if self.close_error is not None:
                    raise self.close_error
                return
            yield item

    def close(self) -> None:
        if not self.closed.is_set():
            self.closed.set()
            self.inbox.put(None)


class FakeConnector:
    """plan 의 항목을 차례로 쓴다: 예외면 연결 실패, FakeConn 이면 그 연결. 다 쓰면 실패만 낸다."""

    def __init__(self, plan) -> None:
        self.plan = list(plan)
        self.conns: list[FakeConn] = []
        self.attempts = 0

    def connect(self):
        self.attempts += 1
        if not self.plan:
            raise ConnectionError("더 없음")
        item = self.plan.pop(0)
        if isinstance(item, BaseException):
            raise item
        self.conns.append(item)
        return item


class RecordingBackoff(Backoff):
    """delay(failures) 를 기록하고 짧게 기다린다."""

    def __init__(self) -> None:
        super().__init__(initial_s=0.01, factor=2.0, max_s=0.02)
        object.__setattr__(self, "calls", [])

    def delay(self, failures: int) -> float:
        self.calls.append(failures)
        return 0.01


FAST = {"backoff": Backoff(initial_s=0.01, factor=2.0, max_s=0.02)}


def envelope(clock=None) -> Envelope:
    return Envelope("tier0-01", clock or FixedClock(T0))


def kinds(messages) -> list[str]:
    return [m["msg_type"] for m in messages]


# ---------------------------------------------------------------------------- 설정·봉투·TLS·토큰


def test_settings_from_env_requires_wss_and_token_and_hides_token() -> None:
    s = I1Settings.from_env({"SIMAI_EC2_I1_URL": "wss://ec2.example/ws/tier0", "SIMAI_TIER0_TOKEN": TOKEN})
    assert s.url == "wss://ec2.example/ws/tier0" and s.source_id == "tier0-01" and s.ca_file is None
    assert TOKEN not in repr(s)
    derived = I1Settings.from_env({"SIMAI_EC2_BASE_URL": "https://ec2.example:8443/", "SIMAI_TIER0_TOKEN": TOKEN,
                                   "SIMAI_TIER0_ID": "tier0-02"})
    assert derived.url == "wss://ec2.example:8443/ws/tier0" and derived.source_id == "tier0-02"
    with pytest.raises(I1ConfigError, match="wss"):
        I1Settings.from_env({"SIMAI_EC2_I1_URL": "ws://ec2.example/ws", "SIMAI_TIER0_TOKEN": TOKEN})
    with pytest.raises(I1ConfigError, match="SIMAI_TIER0_TOKEN"):
        I1Settings.from_env({"SIMAI_EC2_I1_URL": "wss://ec2.example/ws"})
    with pytest.raises(I1ConfigError):
        I1Settings.from_env({"SIMAI_EC2_BASE_URL": "http://ec2.example", "SIMAI_TIER0_TOKEN": TOKEN})
    with pytest.raises(I1ConfigError, match="계정"):
        I1Settings.from_env({"SIMAI_EC2_I1_URL": "wss://u:p@ec2.example/ws", "SIMAI_TIER0_TOKEN": TOKEN})


@pytest.mark.parametrize(
    "url",
    [
        f"wss://ec2.example/ws/tier0?token={TOKEN}",
        f"wss://ec2.example/ws/tier0?token={urllib.parse.quote(TOKEN, safe='')}",
        "wss://ec2.example/ws/tier0?mode=live",  # query 자체를 받지 않는다
        "wss://ec2.example/ws/tier0#frag",
        f"wss://ec2.example/ws/{urllib.parse.quote(TOKEN, safe='')}",  # 경로에 인코딩한 토큰
        f"wss://ec2.example/ws/{urllib.parse.quote_plus(TOKEN)}",
    ],
)
def test_url_carrying_token_or_query_is_rejected_without_echoing_token(url) -> None:
    for build in (
        lambda: I1Settings.from_env({"SIMAI_EC2_I1_URL": url, "SIMAI_TIER0_TOKEN": TOKEN}),
        lambda: I1Settings(url=url, token=TOKEN),
        lambda: WebSocketConnector(url, TOKEN),
    ):
        with pytest.raises(I1ConfigError) as e:
            build()
        assert TOKEN not in str(e.value) and urllib.parse.quote(TOKEN, safe="") not in str(e.value)


def test_connector_refuses_unverified_tls_and_hides_token() -> None:
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(I1ConfigError, match="검증"):
        WebSocketConnector("wss://ec2.example/ws", TOKEN, ssl_context=insecure)
    no_hostname = ssl.create_default_context()
    no_hostname.check_hostname = False
    with pytest.raises(I1ConfigError):
        require_verifying(no_hostname)
    with pytest.raises(I1ConfigError):
        WebSocketConnector("ws://127.0.0.1:1/ws", TOKEN)
    with pytest.raises(I1ConfigError):
        WebSocketConnector("wss://ec2.example/ws", "  ")
    connector = WebSocketConnector("wss://ec2.example/ws", TOKEN)
    assert TOKEN not in repr(connector)


def test_errors_after_connect_do_not_keep_token(caplog) -> None:
    """서버 close reason·송신 오류에 토큰이 실려도 last_error·SendFailed·로그에 남기지 않는다."""
    conn = FakeConn()
    conn.close_error = ConnectionError(f"received 1008 (policy violation) {TOKEN}; "
                                       f"{urllib.parse.quote(TOKEN, safe='')}")
    client = I1Client(FakeConnector([conn]), on_message=lambda raw: None, **FAST)
    caplog.set_level(logging.DEBUG)
    client.start()
    try:
        assert wait_until(lambda: client.connected)
        link = client.current_link()
        conn.close()
        assert wait_until(lambda: client.stats.disconnects >= 1)
        assert client.last_error and TOKEN not in client.last_error and "***" in client.last_error
        with pytest.raises(SendFailed) as e:
            client.send_on(link, example("calibration_update"))
        assert TOKEN not in str(e.value)
    finally:
        client.close()
    direct = I1Client(FakeConnector([]), on_message=lambda raw: None)
    with pytest.raises(SendFailed) as e2:
        broken = FakeConn()
        broken.fail_sends = True
        direct._send(broken, example("calibration_update"))
    assert TOKEN not in str(e2.value) and TOKEN not in (direct.last_error or "")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in text and urllib.parse.quote(TOKEN, safe="") not in text


def test_envelope_fields_and_versions() -> None:
    env = Envelope("tier0-01", FixedClock(T0), new_msg_id=lambda: "0192a6f0-7c1e-7b3a-9f2d-000000000001")
    msg = env.make("world_state", world_payload(T0 - 100))
    assert msg["schema_version"] == "2.0.0" and msg["sent_at"] == T0 and msg["source_id"] == "tier0-01"
    assert schema_errors(msg, "world_state") == []
    assert env.make("health", health_payload())["schema_version"] == "2.1.0"
    assert schema_errors(env.make("health", health_payload()), "health") == []
    for name in ("observation_summary", "calibration_update", "event_state_snapshot", "event_frame_ready"):
        doc = env.make(name, example(name)["payload"])
        assert schema_errors(doc, name) == [], name
    with pytest.raises(ValueError, match="위로"):
        env.make("journal_ack", {})


def test_backoff_saturates_instead_of_overflowing() -> None:
    b = Backoff()
    assert [b.delay(n) for n in (1, 2, 6, 100, 1025, 10**9)] == [1.0, 2.0, 30.0, 30.0, 30.0, 30.0]


# ---------------------------------------------------------------------------- 디스패처


def linked(d: I1Dispatcher) -> I1Dispatcher:
    """연결된 상태의 디스패처(build_i1_link 가 연결 훅으로 부르는 on_connected)."""
    d.on_connected()
    return d


class FakeOutbox:
    def __init__(self) -> None:
        self.acks: list[dict] = []
        self.resets = 0
        self.pumps = 0
        self.drained = True
        self.blocked = None

    def on_journal_ack(self, payload):
        from simai_ai.eventlog import JournalAckResult

        self.acks.append(payload)
        return JournalAckResult(True, True, payload["acked_offset"])

    def reset(self):
        self.resets += 1

    def pump(self, max_batches=None):
        self.pumps += 1
        return 0


class FakeConfigSync:
    def __init__(self) -> None:
        self.received: list[dict] = []
        self.events: list[str] = []
        self.acks: list[dict] = []

    def receive(self, message):
        self.received.append(message)

    def on_connected(self):
        self.events.append("connected")

    def on_disconnected(self):
        self.events.append("disconnected")

    def take_acks(self):
        out, self.acks = self.acks, []
        return out

    def health_fields(self):
        return {"config_sync": "UNVERIFIED"}


def test_dispatcher_routes_to_existing_handlers_and_dedups() -> None:
    outbox, sync, acks = FakeOutbox(), FakeConfigSync(), []
    d = linked(wire_tier0_handlers(I1Dispatcher(), outbox=outbox, config_sync=sync, on_event_ack=acks.append,
                                   ack_seen=lambda ack_id: False))
    try:
        for name in ("journal_ack", "config_bundle", "event_ack"):
            d.handle_raw(json.dumps(example(name)))
        assert wait_until(lambda: len(sync.received) == 1)  # 구성 작업 스레드
        assert outbox.acks == [example("journal_ack")["payload"]]
        assert sync.received == [example("config_bundle")]
        [ack] = acks
        assert isinstance(ack, ContractAck) and ack.event_id == example("event_ack")["payload"]["event_id"]
        assert ack.target_sequence == 2 and ack.channel == "CONTROL_ROOM"
        # 같은 연결에서 같은 msg_id 는 다시 넘기지 않는다
        d.handle_raw(json.dumps(example("journal_ack")))
        assert len(outbox.acks) == 1 and d.stats.duplicates == 1
        assert wait_until(lambda: d.stats.handled == {"journal_ack": 1, "config_bundle": 1, "event_ack": 1})
        # 새 연결에서는 같은 msg_id 의 재알림을 다시 처리한다(ConfigSync 가 최신 일치를 다시 확인)
        d.on_connected()
        d.handle(example("config_bundle"))
        assert wait_until(lambda: len(sync.received) == 2)
    finally:
        assert d.close()


def test_dispatcher_retries_after_handler_failure_and_flags_conflicts(caplog) -> None:
    attempts = []

    def flaky(message):
        attempts.append(message["payload"]["acked_offset"])
        if len(attempts) == 1:
            raise OSError("일시 저장 실패")

    d = linked(I1Dispatcher())
    d.register("journal_ack", flaky)
    msg = example("journal_ack")
    with caplog.at_level(logging.ERROR):
        d.handle(msg)
    d.handle(msg)  # 실패한 메시지는 기억하지 않았다: 다시 처리
    d.handle(msg)  # 성공한 뒤에는 중복
    changed = copy.deepcopy(msg)
    changed["payload"]["acked_offset"] += 1
    d.handle(changed)  # 같은 msg_id, 다른 내용: 충돌로 세고 넘기지 않는다
    assert attempts == [1042, 1042]
    assert (d.stats.handler_errors, d.stats.duplicates, d.stats.conflicts) == (1, 1, 1)


def test_dispatcher_drops_malformed_unknown_and_survives_handler_errors(caplog) -> None:
    d = linked(I1Dispatcher())
    d.register("journal_ack", lambda m: (_ for _ in ()).throw(RuntimeError("처리 실패")))
    assert d.handle_raw(b"\xff not json") is None
    assert d.handle_raw("[1, 2]") is None
    assert d.handle({"msg_type": "journal_ack", "msg_id": "x"}) is None  # payload 없음
    assert d.handle({"msg_type": "world_state", "msg_id": "y", "payload": {}}) is None  # 위로 가는 종류
    assert d.handle({"msg_type": "event_ack", "msg_id": "z", "payload": {}}) is None  # 처리기 없음
    with caplog.at_level(logging.ERROR):
        assert d.handle_raw(json.dumps(example("journal_ack"))) is None
    assert (d.stats.malformed, d.stats.unknown, d.stats.unhandled, d.stats.handler_errors) == (3, 1, 1, 1)
    with pytest.raises(ValueError):
        d.register("health", lambda m: None)


def test_event_ack_forbidden_channel_and_ack_id_idempotency() -> None:
    acks, recorded = [], set()

    def on_ack(ack):
        acks.append(ack)
        recorded.add(ack.ack_id)

    d = linked(wire_tier0_handlers(I1Dispatcher(), on_event_ack=on_ack, ack_seen=recorded.__contains__))
    bad = example("event_ack")
    bad["payload"]["ack"]["channel"] = "SITE_BUTTON"  # 현장 버튼은 site_ack 로만
    d.handle(bad)
    assert acks == [] and d.stats.handler_errors == 1
    good = example("event_ack")
    d.handle(good)
    d.on_connected()  # 재연결 뒤 EC2 가 같은 확인을 다시 보내도
    again = copy.deepcopy(good)
    again["msg_id"] = "0192a6f0-7c1e-7b3a-9f2d-0000000001ff"  # msg_id 가 달라도 ack_id 로 한 번만
    d.handle(good)
    d.handle(again)
    assert len(acks) == 1 and d.stats.skipped_acks == 2


def test_slow_config_download_does_not_block_journal_ack_and_close_keeps_live_thread() -> None:
    entered, release = threading.Event(), threading.Event()

    class SlowSync(FakeConfigSync):
        def receive(self, message):
            entered.set()
            assert release.wait(10)
            super().receive(message)

    outbox, sync = FakeOutbox(), SlowSync()
    conn = FakeConn()
    link = build_i1_link(FakeConnector([conn]), envelope(), health_payload=health_payload,
                         make_outbox=lambda transport: outbox, config_sync=sync, client_kwargs=FAST)
    link.start()
    try:
        assert wait_until(lambda: link.client.connected)
        conn.inbox.put(json.dumps(example("config_bundle")))
        assert entered.wait(3)
        ack = example("journal_ack")
        ack["payload"] = {"gateway_id": "tier0-01", "acked_offset": 0}
        conn.inbox.put(json.dumps(ack))
        assert wait_until(lambda: outbox.acks == [ack["payload"]])  # 내려받기 중에도 확인은 처리된다
        assert link.close(timeout_s=0.3) is False
        assert link.alive_threads() == ["i1-config-worker"]  # 끝나지 않은 스레드를 잊지 않는다
    finally:
        release.set()
        assert wait_until(lambda: link.alive_threads() == [])
        assert link.close()
    assert len(sync.received) == 1


# ---------------------------------------------------------------------------- 스케줄러 (가짜 클라이언트)


class FakeClient:
    def __init__(self, reliable_limit: int = 100) -> None:
        self.latest: list[dict] = []
        self.fifo: list[tuple[dict, str | None]] = []
        self.connected = True
        self.reliable_limit = reliable_limit
        self.drops = {"world_state": (0, 0), "health": (0, 0)}

    def drop_counts(self, kind):
        return self.drops[kind]

    def put_latest(self, message):
        self.latest.append(message)
        return False

    def enqueue(self, message, *, replace_key=None):
        if replace_key is not None:
            self.fifo = [(m, k) for m, k in self.fifo if k != replace_key]
        if len(self.fifo) >= self.reliable_limit:
            raise QueueFull("가득")
        self.fifo.append((message, replace_key))


def test_publisher_world_state_10hz_latest_only_and_health_1hz() -> None:
    mono, client = FakeMono(), FakeClient()
    pub = I1Publisher(client, envelope(), health_payload=health_payload, monotonic=mono)
    pub.on_connected()  # 연결 직후 health 즉시
    assert kinds(client.latest) == ["health"]
    t = T0
    for step in range(1, 251):  # 2.5 초를 10 ms 씩, world_state 는 25 Hz 로 들어온다
        mono.t = step * 0.01
        if step % 4 == 0:
            t += 40
            pub.offer_world_state(world_payload(t))
        pub.tick()
    worlds = [m for m in client.latest if m["msg_type"] == "world_state"]
    healths = [m for m in client.latest if m["msg_type"] == "health"]
    assert 24 <= len(worlds) <= 26  # 10 Hz
    assert len(healths) == 3  # 즉시 1 + 1 Hz 두 번(1.0 s, 2.0 s)
    ts = [m["payload"]["t_latest_global"] for m in worlds]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)  # 같은 상태를 두 번 보내지 않는다
    assert pub.world_coalesced > 0  # 25 Hz → 10 Hz 로 내려 뽑음
    # 새 상태가 없으면 보내지 않는다(마지막으로 넣은 것은 다음 주기에 한 번 나간다)
    for step in range(251, 261):
        mono.t = step * 0.01
        pub.tick()
    before = len(client.latest)
    for step in range(261, 321):
        mono.t = step * 0.01
        pub.tick()
    assert all(m["msg_type"] == "health" for m in client.latest[before:])


def test_publisher_health_merges_fields_retries_first_and_stops_when_disconnected() -> None:
    mono, client = FakeMono(), FakeClient()
    state = {"base": None}
    pub = I1Publisher(client, envelope(), health_payload=lambda: state["base"],
                      health_fields=[lambda: {"config_sync": "UNVERIFIED"},
                                     lambda: {"active_site_alerts": [], "active_site_alerts_as_of": T0}],
                      monotonic=mono)
    pub.on_connected()
    assert client.latest == []  # 모르는 동안은 보내지 않는다(지어내지 않음)
    mono.t = 0.05
    pub.tick()
    assert client.latest == []
    state["base"] = health_payload()
    mono.t = 0.06
    pub.tick()  # 첫 health 는 주기를 기다리지 않고 만들 수 있게 되자마자
    [h] = client.latest
    assert h["payload"]["config_sync"] == "UNVERIFIED" and h["payload"]["active_site_alerts"] == []
    assert schema_errors(h, "health") == []
    pub.on_disconnected()
    client.connected = False
    mono.t = 5.0
    pub.tick()
    assert len(client.latest) == 1


def test_publisher_events_and_config_ack_are_held_when_reliable_queue_is_full() -> None:
    client, sync = FakeClient(reliable_limit=2), FakeConfigSync()
    pub = I1Publisher(client, envelope(), health_payload=health_payload, monotonic=FakeMono())
    pub.add_source(sync.take_acks)
    pub.send_event("calibration_update", example("calibration_update")["payload"])
    sync.acks.extend([example("config_ack"), example("config_ack.rejected")])
    pub.tick()
    assert kinds(m for m, _ in client.fifo) == ["calibration_update", "config_ack"]
    with pytest.raises(QueueFull):  # 이벤트는 호출자에게 알린다(버리지 않음)
        pub.send_event("calibration_update", example("calibration_update")["payload"])
    client.reliable_limit = 10
    pub.tick()  # 붙들었던 config_ack 를 다음 tick 에 넣는다
    assert kinds(m for m, _ in client.fifo) == ["calibration_update", "config_ack", "config_ack"]
    for m, _ in client.fifo:
        assert schema_errors(m, m["msg_type"]) == [], m["msg_type"]
    for bad in ("world_state", "event_state_snapshot", "risk_event_transition"):
        with pytest.raises(ValueError):
            pub.send_event(bad, {})


def test_endpoint_adapter_replaces_previous_and_does_not_invent_values(caplog) -> None:
    client = FakeClient()
    built = iter([example("tier0_endpoint"), example("tier0_endpoint")])
    reg = EndpointI1Registration(client, lambda: next(built), source_id="tier0-01")
    reg.register()
    reg.register()
    assert kinds(m for m, _ in client.fifo) == ["tier0_endpoint"]  # 재연결마다 새것으로 바꾼다

    def broken():
        raise EndpointConfigError(["RUNPOD_PUBLIC_IP 가 없다"])

    reg2 = EndpointI1Registration(client, broken, source_id="tier0-01")
    with caplog.at_level(logging.ERROR):
        assert reg2.register() is None
    assert reg2.failures == 1 and len(client.fifo) == 1
    other = EndpointI1Registration(client, lambda: example("tier0_endpoint"), source_id="tier0-09")
    assert other.register() is None and other.failures == 1


# ---------------------------------------------------------------------------- 클라이언트 (가짜 연결)


def make_client(connector, **kw) -> I1Client:
    kw.setdefault("backoff", RecordingBackoff())
    return I1Client(connector, on_message=I1Dispatcher().handle_raw, **kw)


def test_first_message_on_each_connection_is_health_and_world_state_is_not_replayed() -> None:
    conns = [FakeConn(), FakeConn()]
    client = make_client(FakeConnector(conns), first_message_type="health")
    env = envelope()
    client.add_on_connected(lambda: client.put_latest(env.make("health", health_payload())))
    client.enqueue(env.make("calibration_update", example("calibration_update")["payload"]))  # 연결 전 이벤트는 보관
    assert client.put_latest(env.make("world_state", world_payload(T0))) is False
    assert client.stats.discarded_offline["world_state"] == 1  # 끊긴 동안의 world_state 는 버린다
    client.start()
    try:
        assert wait_until(lambda: len(conns[0].sent) == 2)
        assert kinds(conns[0].sent) == ["health", "calibration_update"]
        client.drop_connection()
        assert wait_until(lambda: len(conns[1].sent) >= 1)
        assert kinds(conns[1].sent)[0] == "health"
        assert client.stats.connects == 2 and client.generation == 2
    finally:
        client.close()


def test_unknown_health_keeps_other_sends_closed_until_health_can_be_made() -> None:
    conn = FakeConn()
    state = {"base": None}
    outbox = FakeOutbox()
    link = build_i1_link(FakeConnector([conn]), envelope(), health_payload=lambda: state["base"],
                         make_outbox=lambda transport: outbox, client_kwargs=FAST)
    link.publisher.send_event("calibration_update", example("calibration_update")["payload"])
    link.start()
    try:
        assert wait_until(lambda: link.client.connected)
        link.publisher.offer_world_state(world_payload(T0))
        time.sleep(0.3)
        assert conn.sent == [] and outbox.pumps == 0  # health 를 모르는 동안 아무것도 보내지 않는다
        state["base"] = health_payload()
        assert wait_until(lambda: len(conn.sent) >= 2)
        assert kinds(conn.sent)[:2] == ["health", "calibration_update"]
        assert wait_until(lambda: outbox.pumps > 0)
    finally:
        link.close()


@pytest.mark.parametrize("first", ["health", None])
def test_old_outbox_batch_never_continues_on_new_connection(tmp_path, first) -> None:
    """리뷰 재현(R2): 이전 연결에서 배치 send 가 멈춘 사이 새 연결이 열려도, 남은 배치는 새 연결로 가지 않는다.

    first=None(첫 메시지 관문 없음)에서도 연결 고정(LinkRef)만으로 막히는지 따로 본다.
    """
    entered, release = threading.Event(), threading.Event()

    class PausedConn(FakeConn):
        def send(self, text):
            super().send(text)
            if json.loads(text)["msg_type"] == "risk_event_transition" and not entered.is_set():
                entered.set()
                assert release.wait(4)

    c1, c2 = PausedConn(), FakeConn()
    clock = FixedClock(T0)
    with EventJournal.open(tmp_path / "journal", clock=clock) as journal:
        for seq in (1, 2, 3):
            journal.append(TRANSITION_KIND, transition_payload(seq))
        link = build_i1_link(
            FakeConnector([c1, c2]), envelope(), health_payload=health_payload,
            make_outbox=lambda t: Outbox(journal, AckedOffsetStore(tmp_path / "ack.json", gateway_id="tier0-01"),
                                         gateway_id="tier0-01", transport=t, clock=clock),
            client_kwargs={**FAST, "first_message_type": first},
        )
        link.start()
        try:
            assert entered.wait(3)
            c1.close()
            assert wait_until(lambda: link.client.generation >= 2)
            release.set()
            assert wait_until(lambda: len([m for m in c2.sent if m["msg_type"] == "risk_event_transition"]) >= 3)
            time.sleep(0.2)
        finally:
            release.set()
            link.close()
        seen = [(m["msg_type"], m["payload"].get("journal_offset")) for m in c2.sent]
        if first == "health":
            assert seen[0] == ("health", None)  # 새 연결의 첫 메시지
        offsets = [o for k, o in seen if k == "risk_event_transition"]
        assert offsets == [0, 1, 2]  # 오름차순 재전송, 이전 배치의 나머지가 끼어들지 않는다
        assert all(m["payload"]["resent"] for m in c2.sent if m["msg_type"] == "risk_event_transition")
        assert link.outbox.acked_offset is None and link.outbox.pending_count() == 3


def test_snapshot_goes_once_per_connection_after_outbox_replay(tmp_path) -> None:
    c1, c2 = FakeConn(), FakeConn()
    clock = FixedClock(T0)
    snapshot = example("event_state_snapshot")["payload"]
    with EventJournal.open(tmp_path / "journal", clock=clock) as journal:
        for seq in (1, 2):
            journal.append(TRANSITION_KIND, transition_payload(seq))
        link = build_i1_link(
            FakeConnector([c1, c2]), envelope(), health_payload=health_payload,
            make_outbox=lambda t: Outbox(journal, AckedOffsetStore(tmp_path / "ack.json", gateway_id="tier0-01"),
                                         gateway_id="tier0-01", transport=t, clock=clock),
            snapshot_payload=lambda: snapshot, client_kwargs=FAST,
        )
        link.start()
        try:
            assert wait_until(lambda: "event_state_snapshot" in kinds(c1.sent))
            time.sleep(0.2)
            link.client.drop_connection()
            assert wait_until(lambda: "event_state_snapshot" in kinds(c2.sent))
            time.sleep(0.2)
        finally:
            link.close()
    for conn in (c1, c2):
        k = kinds(conn.sent)
        assert k[0] == "health" and k.count("event_state_snapshot") == 1
        snap = k.index("event_state_snapshot")
        transitions = [i for i, x in enumerate(k) if x == "risk_event_transition"]
        assert len(transitions) == 2 and max(transitions) < snap  # 재전송을 마친 뒤 스냅샷
        assert schema_errors(conn.sent[snap], "event_state_snapshot") == []


def test_slow_server_drops_oldest_world_state_and_counts() -> None:
    conn = FakeConn(send_delay=0.25)
    client = make_client(FakeConnector([conn]))
    env = envelope()
    client.start()
    try:
        assert wait_until(lambda: client.connected)
        for i in range(15):  # 10 Hz 로 1.5 초
            client.put_latest(env.make("world_state", world_payload(T0 + i * 100)))
            time.sleep(0.1)
        assert wait_until(lambda: client.stats.sent.get("world_state", 0) >= 4, timeout=2)
        time.sleep(0.3)
    finally:
        client.close()
    sent = [m["payload"]["t_latest_global"] for m in conn.sent if m["msg_type"] == "world_state"]
    dropped = client.stats.dropped["world_state"]
    assert dropped >= 5 and len(sent) + dropped <= 15
    assert sent == sorted(sent)  # 늦게라도 오래된 것을 나중에 보내지 않는다
    assert sent[-1] >= T0 + 12 * 100  # 마지막은 최신에 가깝다


def test_failed_event_send_is_requeued_with_same_msg_id() -> None:
    first, second = FakeConn(), FakeConn()
    first.fail_sends = True
    client = make_client(FakeConnector([first, second]))
    env = envelope()
    msg = env.make("calibration_update", example("calibration_update")["payload"])
    client.enqueue(msg)
    client.start()
    try:
        assert wait_until(lambda: len(second.sent) == 1)
    finally:
        client.close()
    assert second.sent[0]["msg_id"] == msg["msg_id"] and client.stats.send_failures >= 1


def test_reliable_queue_never_drops_and_telemetry_overflow_drops_oldest() -> None:
    client = make_client(FakeConnector([]), reliable_limit=2, telemetry_limit=2)
    env = envelope()
    acks = [example("config_ack"), example("config_ack.rejected")]
    for a in acks:
        client.enqueue(a)
    with pytest.raises(QueueFull):
        client.enqueue(env.make("calibration_update", example("calibration_update")["payload"]))
    assert client.queue_sizes()["reliable"] == 2 and client.stats.reliable_full == 1
    summaries = [env.make("observation_summary", example("observation_summary")["payload"]) for _ in range(4)]
    for m in summaries:
        client.enqueue(m)
    assert client.queue_sizes()["telemetry"] == 2 and client.stats.telemetry_overflow == 2
    for bad in (example("event_state_snapshot"), example("risk_event_transition"), summaries[0] | {"msg_type": "health"}):
        with pytest.raises(ValueError):
            client.enqueue(bad)
    with pytest.raises(ValueError):
        client.put_latest(summaries[0])


def test_backoff_grows_on_failures_and_resets_after_stable_connection() -> None:
    backoff = RecordingBackoff()
    stable = FakeConn()
    connector = FakeConnector([ConnectionError("1"), ConnectionError("2"), stable, ConnectionError("3")])
    client = make_client(connector, backoff=backoff, stable_after_s=0.05)
    client.start()
    try:
        assert wait_until(lambda: client.connected)
        time.sleep(0.1)  # 안정 연결
        client.drop_connection()
        assert wait_until(lambda: len(backoff.calls) >= 4)
    finally:
        client.close()
    assert backoff.calls[:4] == [1, 2, 1, 2]  # 실패 1·2 → 안정 연결 뒤 끊김은 1 부터 → 다음 실패 2


def test_backoff_keeps_growing_when_connection_drops_immediately() -> None:
    backoff = RecordingBackoff()
    quick = [FakeConn() for _ in range(3)]
    for c in quick:
        c.close()  # 붙자마자 끊김
    client = make_client(FakeConnector([ConnectionError("x"), *quick]), backoff=backoff, stable_after_s=10)
    client.start()
    try:
        assert wait_until(lambda: len(backoff.calls) >= 4)
    finally:
        client.close()
    assert backoff.calls[:4] == [1, 2, 3, 4]


def test_outbox_transport_raises_outbox_error_when_offline_and_attach_wires_reset_and_pump() -> None:
    client = make_client(FakeConnector([]))
    transport = OutboxI1Transport(client)
    with pytest.raises(OutboxTransportError):
        transport([example("risk_event_transition")])
    with pytest.raises(SendFailed):
        client.current_link()
    conn = FakeConn()
    client2 = make_client(FakeConnector([conn]), pump_interval_s=0.01)
    outbox = FakeOutbox()
    attach_outbox(client2, outbox)
    client2.start()
    try:
        assert wait_until(lambda: outbox.pumps >= 3)
    finally:
        client2.close()
    assert outbox.resets == 1


def test_unserializable_message_is_dropped_not_fatal() -> None:
    conn = FakeConn()
    client = make_client(FakeConnector([conn]))
    env = envelope()
    bad = env.make("calibration_update", {"effective_at": float("nan")})
    client.enqueue(bad)
    client.enqueue(env.make("calibration_update", example("calibration_update")["payload"]))
    client.start()
    try:
        assert wait_until(lambda: len(conn.sent) == 1)
    finally:
        client.close()
    assert kinds(conn.sent) == ["calibration_update"] and client.stats.unserializable == 1



# ---------------------------------------------------------------------------- 2차 리뷰: 재연결 재알림·옛 연결 항목·정지 표식


def bundle_kit():
    """tests/config 의 BundleKit(실제 ConfigSync 검증 경로)을 쓴다. Event 로 막을 수 있는 내려받기를 단다."""
    import runpy

    kit = runpy.run_path(str(AI_DIR / "tests" / "config" / "conftest.py"))["BundleKit"]()
    gate = {"entered": threading.Event(), "release": threading.Event(), "block": set()}
    original = kit.fetcher.fetch

    def fetch(uri):
        if any(uri.startswith(prefix) for prefix in gate["block"]):
            gate["entered"].set()
            assert gate["release"].wait(10)
        return original(uri)

    kit.fetcher.fetch = fetch
    return kit, gate


def config_link(kit, conns):
    from simai_ai.config import ConfigSync

    sync = ConfigSync(kit.verifier(), kit.clock, source_id="tier0-01")
    link = build_i1_link(FakeConnector(conns), envelope(), health_payload=health_payload,
                         health_fields=[sync.health_fields], config_sync=sync, client_kwargs=FAST)
    return sync, link


def test_reannounce_on_new_connection_while_bundle_is_downloading_reaches_config_sync() -> None:
    """N1: 내려받기 중 끊기고 새 연결에서 같은 msg_id 로 다시 알리면 ConfigSync 로 넘겨 CURRENT 가 된다."""
    kit, gate = bundle_kit()
    c1, c2 = FakeConn(), FakeConn()
    sync, link = config_link(kit, [c1, c2])
    notice = kit.publish("cfg-a")
    gate["block"].add("s3://simai-site/config/cfg-a/")
    link.start()
    try:
        assert wait_until(lambda: link.client.connected)
        c1.inbox.put(json.dumps(notice))
        assert gate["entered"].wait(3)
        assert sync.state.value == "PENDING"
        c1.close()
        assert wait_until(lambda: link.client.generation == 2 and link.client.connected)
        c2.inbox.put(json.dumps(notice))  # 같은 msg_id
        assert wait_until(lambda: link.dispatcher.pending_background() >= 1)
        assert link.dispatcher.stats.duplicates == 0
        gate["release"].set()
        assert wait_until(lambda: link.dispatcher.stats.handled.get("config_bundle", 0) == 2)
        active = sync.begin_tick()
        assert active is not None and active.config_version == "cfg-a"
        assert sync.state.value == "CURRENT"  # 재접속 확인이 끝났다(UNVERIFIED 에 머물지 않는다)
    finally:
        gate["release"].set()
        link.close()


def test_bundle_from_dropped_connection_does_not_mark_config_sync_connected() -> None:
    """권장 1: 끊긴 연결에서 받아 아직 시작하지 않은 묶음은 버리고, 처리 중에 끊긴 묶음도 ConfigSync 를 연결됨으로 두지 않는다."""
    kit, gate = bundle_kit()
    c1 = FakeConn()
    sync, link = config_link(kit, [c1])  # 두 번째 연결 없음: 끊긴 채로 남는다
    first, second = kit.publish("cfg-b1"), kit.publish("cfg-b2", issued_at=kit.clock.now_ms() + 10)
    gate["block"].add("s3://simai-site/config/cfg-b1/")
    link.start()
    try:
        assert wait_until(lambda: link.client.connected)
        c1.inbox.put(json.dumps(first))
        assert gate["entered"].wait(3)
        c1.inbox.put(json.dumps(second))  # 첫 묶음 뒤에 줄 선다
        assert wait_until(lambda: link.dispatcher.pending_background() == 1)
        c1.close()
        assert wait_until(lambda: not link.client.connected)
        gate["release"].set()
        assert wait_until(lambda: link.dispatcher.stats.stale_background == 1)
        assert wait_until(lambda: link.dispatcher.stats.handled.get("config_bundle", 0) == 1)
        time.sleep(0.05)
        assert sync.connected is False and sync.state.value == "UNVERIFIED"
        assert sync.health_fields()["config_sync"] == "UNVERIFIED"
        assert all(t.after.value != "CURRENT" for t in sync.transitions())
        assert [b.config_version for b in sync.versions()] == ["cfg-b1"]  # cfg-b2 는 시작하지 않았다
    finally:
        gate["release"].set()
        link.close()


def test_dispatcher_close_twice_and_restart_leaves_no_stop_marker() -> None:
    """권장 3: close 를 두 번 부르거나 닫은 뒤 다시 써도 다음 구성 묶음이 처리된다."""
    received = []
    d = linked(I1Dispatcher())
    d.register("config_bundle", received.append, background=True)
    d.handle(example("config_bundle"))
    assert wait_until(lambda: len(received) == 1)
    assert d.close() and d.close()
    d.on_connected()
    d.handle(example("config_bundle"))  # 새 연결: 같은 msg_id 도 다시
    assert wait_until(lambda: len(received) == 2)
    assert d.close() and d.alive_threads() == []


def test_event_ack_requires_ack_seen() -> None:
    with pytest.raises(ValueError, match="ack_seen"):
        wire_tier0_handlers(I1Dispatcher(), on_event_ack=lambda ack: None)
    with pytest.raises(ValueError, match="ack_seen"):
        build_i1_link(FakeConnector([]), envelope(), health_payload=health_payload, on_event_ack=lambda ack: None)


# ---------------------------------------------------------------------------- 반례: 막힌 송신(교착·종료 지연·재연결 폭주)


class BlockingConn(FakeConn):
    """읽지 않는 상대: calibration_update(또는 모든 메시지)에서 send 가 연결이 끊길 때까지 막힌다."""

    def __init__(self, *, block_all: bool = False) -> None:
        super().__init__()
        self.block_all = block_all
        self.blocked = threading.Event()

    def send(self, text: str) -> None:
        kind = json.loads(text)["msg_type"]
        if self.block_all or kind == "calibration_update":
            self.blocked.set()
            self.closed.wait()  # 연결이 끊겨야 풀린다
            raise ConnectionError("소켓 닫힘")
        super().send(text)


def test_counterexample_deadlock_blocked_send_does_not_stop_receiving_or_close() -> None:
    """반례(교착): 송신 스레드가 막혀 있어도 수신(journal_ack)은 처리되고, 두 스레드에서 동시에 close 해도 막히지 않는다."""
    conn = BlockingConn()
    outbox = FakeOutbox()
    link = build_i1_link(FakeConnector([conn]), envelope(), health_payload=health_payload,
                         make_outbox=lambda t: outbox, client_kwargs={**FAST, "send_timeout_s": 60})
    link.start()
    try:
        assert wait_until(lambda: link.client.connected)
        link.publisher.send_event("calibration_update", example("calibration_update")["payload"])
        assert conn.blocked.wait(3)
        ack = example("journal_ack")
        ack["payload"] = {"gateway_id": "tier0-01", "acked_offset": 3}
        conn.inbox.put(json.dumps(ack))
        assert wait_until(lambda: outbox.acks == [ack["payload"]], timeout=2)
        results = []
        closers = [threading.Thread(target=lambda: results.append(link.close(timeout_s=2))) for _ in range(2)]
        t0 = time.monotonic()
        for t in closers:
            t.start()
        for t in closers:
            t.join(5)
        assert all(not t.is_alive() for t in closers) and time.monotonic() - t0 < 3
        assert results == [True, True]
    finally:
        link.close()


def test_counterexample_shutdown_delay_close_returns_within_timeout_even_with_blocked_send_and_download() -> None:
    """반례(종료 지연): 송신이 막히고 구성 내려받기도 막혀도 close(timeout) 는 제한 시간 안에 돌아오고 남은 스레드를 알린다."""
    kit, gate = bundle_kit()
    conn = BlockingConn()
    sync, link = config_link(kit, [conn])
    gate["block"].add("s3://simai-site/config/cfg-c/")
    link.start()
    try:
        assert wait_until(lambda: link.client.connected)
        conn.inbox.put(json.dumps(kit.publish("cfg-c")))
        assert gate["entered"].wait(3)
        link.publisher.send_event("calibration_update", example("calibration_update")["payload"])
        assert conn.blocked.wait(3)
        t0 = time.monotonic()
        ok = link.close(timeout_s=0.5)
        took = time.monotonic() - t0
        assert took < 2.0, took
        assert ok is False and link.alive_threads() == ["i1-config-worker"]  # 송신·연결 스레드는 끝났다
        assert link.client.alive_threads() == []
    finally:
        gate["release"].set()
        assert wait_until(lambda: link.alive_threads() == [])
        link.close()


def test_counterexample_reconnect_storm_send_timeouts_keep_backoff_growing() -> None:
    """반례(재연결 폭주): 상대가 읽지 않아 send 시간 초과로 끊긴 연결은 오래 붙어 있었어도 실패 횟수를 되돌리지 않는다."""
    backoff = RecordingBackoff()
    conns = [BlockingConn(block_all=True) for _ in range(5)]
    client = I1Client(FakeConnector(conns), on_message=lambda raw: None, backoff=backoff, stable_after_s=0.0,
                      send_timeout_s=0.1)
    env = envelope()
    client.add_on_connected(lambda: client.put_latest(env.make("health", health_payload())))
    client.start()
    try:
        assert wait_until(lambda: len(backoff.calls) >= 4, timeout=5)
    finally:
        assert client.close(timeout_s=2)
    assert backoff.calls[:4] == [1, 2, 3, 4]  # stable_after_s=0 이어도 시간 초과 연결은 안정으로 보지 않는다
    assert client.stats.send_timeouts >= 4 and client.alive_threads() == []
