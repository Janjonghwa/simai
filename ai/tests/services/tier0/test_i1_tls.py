"""I1 실제 wss 시험 (S15P31A101-45): 로컬 WebSocket 서버(TLS 자체 서명, 시험 중에 만들고 커밋하지 않음).

보낸 메시지 계약 통과, world_state 10 Hz·health 1 Hz, 재연결 직후 health, 토큰 비노출, 인증서 검증 실패 시 토큰 미전송,
수신 디스패치(journal_ack → outbox, config_bundle → ConfigSync → config_ack, event_ack → 확인 처리)를 본다.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import ipaddress
import json
import logging
import statistics
import sys
import threading
import time
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.tier0.endpoint_registration import Backoff, EndpointRegistrar  # noqa: E402
from services.tier0.i1_client import Envelope, I1ConnectError, WebSocketConnector  # noqa: E402
from services.tier0.i1_publisher import build_i1_link  # noqa: E402
from simai_ai.config import BundleVerifier, ConfigSync, FetchError  # noqa: E402
from simai_ai.eventlog import TRANSITION_KIND, ACKED_NAME, AckedOffsetStore, EventJournal, Outbox  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import SystemClock, default_registry  # noqa: E402

REPO = AI_DIR.parent
EXAMPLES = REPO / "contracts" / "examples"
TOKEN = "i1-tls-secret-token-4d1b"
GATEWAY = "tier0-01"


def example(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def health_payload() -> dict:
    payload = copy.deepcopy(example("health")["payload"])
    for key in ("active_site_alerts", "active_site_alerts_as_of", "config_sync", "config_version"):
        payload.pop(key, None)
    return payload


def world_payload(t: int) -> dict:
    payload = copy.deepcopy(example("world_state")["payload"])
    payload["t_latest_global"] = t
    return payload


def transition_payload(seq: int) -> dict:
    payload = copy.deepcopy(example("risk_event_transition")["payload"])
    del payload["journal_offset"], payload["resent"]
    payload["event_sequence"] = seq
    payload["transition"] = "RAISED" if seq == 1 else "UPDATED"
    return payload


def client_logs(caplog) -> str:
    """Tier 0 쪽 로그만(시험 서버 websockets.server 로그는 서버가 받은 헤더를 찍으므로 뺀다)."""
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("websockets."))


def wait_until(cond, timeout: float = 5.0, step: float = 0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def make_cert(directory: Path) -> tuple[Path, Path]:
    """127.0.0.1 용 자체 서명 인증서(시험 디렉터리에만, 커밋하지 않는다)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ec2-test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "ec2.crt", directory / "ec2.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file


class Ec2Server:
    """EC2 흉내 wss 서버. 연결마다 받은 메시지(수신 단조 시각, 문서)와 Authorization 헤더를 남긴다."""

    def __init__(self, cert: Path, key: Path) -> None:
        import ssl

        self._ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ssl.load_cert_chain(cert, key)
        self.requests = 0
        self.auth: list[str | None] = []
        self.connections: list[list[tuple[float, dict]]] = []
        self.connected_at: list[float] = []
        self._ws: list = []
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()
        assert self._ready.wait(5)

    def _main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start())
        self._ready.set()
        self._loop.run_forever()

    async def _start(self) -> None:
        from websockets.asyncio.server import serve

        self._server = await serve(self._handler, "127.0.0.1", 0, ssl=self._ssl, process_request=self._check)
        self.port = self._server.sockets[0].getsockname()[1]

    def _check(self, connection, request):
        with self._lock:
            self.requests += 1
            self.auth.append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return connection.respond(401, "unauthorized\n")
        return None

    async def _handler(self, ws) -> None:
        from websockets.exceptions import ConnectionClosed

        with self._lock:
            messages: list[tuple[float, dict]] = []
            self.connections.append(messages)
            self.connected_at.append(time.monotonic())
            self._ws.append(ws)
        try:
            async for raw in ws:
                with self._lock:
                    messages.append((time.monotonic(), json.loads(raw)))
        except ConnectionClosed:
            pass

    @property
    def url(self) -> str:
        return f"wss://127.0.0.1:{self.port}/ws/tier0"

    def messages(self, index: int = -1) -> list[dict]:
        with self._lock:
            return [m for _, m in self.connections[index]] if self.connections else []

    def all_messages(self) -> list[dict]:
        with self._lock:
            return [m for conn in self.connections for _, m in conn]

    def timed(self, index: int = -1) -> list[tuple[float, dict]]:
        with self._lock:
            return list(self.connections[index])

    def send(self, message: dict, index: int = -1) -> None:
        ws = self._ws[index]
        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(message)), self._loop).result(5)

    def drop(self, index: int = -1, *, code: int = 1000, reason: str = "") -> None:
        ws = self._ws[index]
        asyncio.run_coroutine_threadsafe(ws.close(code=code, reason=reason), self._loop).result(5)

    def stop(self) -> None:
        async def shutdown():
            self._server.close()
            await self._server.wait_closed()

        asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)


@pytest.fixture
def tls(tmp_path):
    cert, key = make_cert(tmp_path)
    server = Ec2Server(cert, key)
    yield server, cert
    server.stop()


class NoFetch:
    def fetch(self, uri):
        raise FetchError("시험: 내려받기 없음")


def endpoint_env(cert: Path) -> dict:
    return {
        "SIMAI_TIER0_ID": GATEWAY,
        "SIMAI_TIER0_CONTROL_PORT": "70000",
        "SIMAI_TIER0_VIDEO_PORTS": "cam1:70001,cam2:70002",
        "SIMAI_TIER0_TLS_CERT_FILE": str(cert),
        "RUNPOD_PUBLIC_IP": "203.0.113.10",
        "RUNPOD_TCP_PORT_70000": "40100",
        "RUNPOD_TCP_PORT_70001": "40101",
        "RUNPOD_TCP_PORT_70002": "40102",
    }


def test_i1_over_wss_contract_rates_reconnect_dispatch_and_secret(tls, tmp_path, caplog) -> None:
    server, cert = tls
    caplog.set_level(logging.DEBUG)  # websockets 연결 로그(요청 헤더 포함)까지 켠다
    clock = SystemClock()
    journal = EventJournal.open(tmp_path / "journal", clock=clock)
    try:
        journal.append(TRANSITION_KIND, transition_payload(1))
        journal.append(TRANSITION_KIND, transition_payload(2))
        sync = ConfigSync(BundleVerifier(NoFetch(), default_registry(), lambda v: True, clock), clock,
                          source_id=GATEWAY)
        event_acks = []
        registrar = EndpointRegistrar(transport=None, clock_ms=clock.now_ms, sleep=time.sleep)  # 봉투 만들기만 쓴다
        env = endpoint_env(cert)
        link = build_i1_link(
            WebSocketConnector(server.url, TOKEN, ca_file=str(cert)),
            Envelope(GATEWAY, clock),
            health_payload=health_payload,
            health_fields=[sync.health_fields],
            make_outbox=lambda transport: Outbox(journal, AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY),
                                                 gateway_id=GATEWAY, transport=transport, clock=clock),
            config_sync=sync,
            on_event_ack=event_acks.append,
            ack_seen=lambda ack_id: any(a.ack_id == ack_id for a in event_acks),
            endpoint_build=lambda: registrar.build(env, cert.read_bytes()),
            client_kwargs={"backoff": Backoff(initial_s=0.05, factor=2.0, max_s=0.2), "stable_after_s": 0.5},
        )
        stop_feed = threading.Event()

        def feed() -> None:  # 파이프라인 흉내: 25 Hz 로 최신 World 상태를 넣는다
            while not stop_feed.is_set():
                link.publisher.offer_world_state(world_payload(clock.now_ms() - 120))
                time.sleep(0.04)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        link.start()
        try:
            assert wait_until(lambda: len(server.connections) == 1)
            time.sleep(3.2)

            # 수신 디스패치: journal_ack → outbox, config_bundle → ConfigSync(내려받기 실패 → REJECTED ack), event_ack
            server.send({**example("journal_ack"), "payload": {"gateway_id": GATEWAY, "acked_offset": 1}})
            server.send(example("config_bundle"))
            server.send(example("event_ack"))
            server.send(example("event_ack"))  # 같은 msg_id: 한 번만 넘긴다
            assert wait_until(lambda: link.outbox.acked_offset == 1 and len(event_acks) == 1)
            assert wait_until(lambda: any(m["msg_type"] == "config_ack" for m in server.messages(0)))

            # 재연결: 서버가 끊으면 백오프 뒤 다시 붙고, 새 연결의 첫 메시지는 health
            server.drop(0)
            assert wait_until(lambda: len(server.connections) == 2 and len(server.messages(1)) >= 3)
        finally:
            stop_feed.set()
            link.close()
            feeder.join(2)
    finally:
        journal.close()

    first, second = server.timed(0), server.timed(1)
    # 모든 메시지가 계약을 통과한다
    sent = server.all_messages()
    assert {m["msg_type"] for m in sent} >= {"health", "world_state", "risk_event_transition", "config_ack",
                                             "tier0_endpoint"}
    for m in sent:
        assert m["source_id"] == GATEWAY
        assert schema_errors(m, m["msg_type"]) == [], (m["msg_type"], schema_errors(m, m["msg_type"]))
    assert len({m["msg_id"] for m in sent if m["msg_type"] in ("world_state", "health")}) == len(
        [m for m in sent if m["msg_type"] in ("world_state", "health")])

    # 첫 메시지는 health(연결 직후 즉시), 그다음 엔드포인트 등록
    assert first[0][1]["msg_type"] == "health"
    assert first[0][0] - server.connected_at[0] < 0.5
    assert second[0][1]["msg_type"] == "health" and second[0][0] - server.connected_at[1] < 0.5
    assert [m["msg_type"] for _, m in second].count("tier0_endpoint") == 1  # 재연결마다 다시 등록

    # world_state 10 Hz, health 1 Hz (3.2 초 구간)
    window = [m for t, m in first if t - server.connected_at[0] <= 3.2]
    worlds = [m["sent_at"] for m in window if m["msg_type"] == "world_state"]
    healths = [m["sent_at"] for m in window if m["msg_type"] == "health"]
    assert 26 <= len(worlds) <= 34, len(worlds)
    assert 80 <= statistics.median(b - a for a, b in zip(worlds, worlds[1:])) <= 120
    assert 3 <= len(healths) <= 5, len(healths)
    assert all(900 <= b - a <= 1150 for a, b in zip(healths[1:], healths[2:])), healths

    # outbox 전이: 처음 연결에서 resent=false 로, 재연결 뒤에는 acked_offset 이후만 resent=true 로
    first_tr = [m for _, m in first if m["msg_type"] == "risk_event_transition"]
    assert [m["payload"]["journal_offset"] for m in first_tr] == [0, 1]
    second_tr = [m for _, m in second if m["msg_type"] == "risk_event_transition"]
    assert second_tr == []  # 모두 확인받았다(acked_offset 1)
    [ack] = [m for _, m in first if m["msg_type"] == "config_ack"]
    assert ack["payload"]["result"] == "REJECTED" and ack["payload"]["reject_reason"] == "DOWNLOAD_FAILED"
    health_after = [m for _, m in second if m["msg_type"] == "health"][0]
    assert health_after["payload"]["config_sync"] in ("UNVERIFIED", "STALE")

    # 토큰은 헤더로만 가고 로그에 없다
    assert server.auth == [f"Bearer {TOKEN}", f"Bearer {TOKEN}"]
    assert TOKEN not in client_logs(caplog)
    assert "Authorization: ***" in client_logs(caplog)  # websockets DEBUG 헤더 줄은 가려졌다
    assert event_acks[0].event_id == example("event_ack")["payload"]["event_id"]
    assert link.dispatcher.stats.duplicates == 1


def test_untrusted_certificate_fails_before_token_is_sent(tls, caplog) -> None:
    server, _ = tls
    caplog.set_level(logging.DEBUG)
    connector = WebSocketConnector(server.url, TOKEN)  # 시스템 신뢰 저장소만: 자체 서명 인증서는 믿지 않는다
    with pytest.raises(I1ConnectError) as e:
        connector.connect()
    assert TOKEN not in str(e.value) and TOKEN not in client_logs(caplog)
    assert server.requests == 0  # TLS 검증에서 끊겨 HTTP 업그레이드(토큰)가 나가지 않았다


def test_rejected_token_retries_with_backoff_without_logging_token(tls, caplog) -> None:
    server, cert = tls
    caplog.set_level(logging.DEBUG)
    link = build_i1_link(
        WebSocketConnector(server.url, TOKEN + "-wrong", ca_file=str(cert)),
        Envelope(GATEWAY, SystemClock()),
        health_payload=health_payload,
        client_kwargs={"backoff": Backoff(initial_s=0.05, factor=2.0, max_s=0.1)},
    )
    link.start()
    try:
        assert wait_until(lambda: link.client.stats.connect_failures >= 3)
    finally:
        link.close()
    assert not link.client.connected and server.connections == []
    assert TOKEN not in client_logs(caplog)


def test_server_close_reason_with_token_is_not_kept_or_logged(tls, caplog) -> None:
    """서버가 close reason 에 토큰을 되돌려 보내도(반사) last_error·송신 예외·로그에 남지 않는다."""
    from services.tier0.i1_client import SendFailed

    server, cert = tls
    caplog.set_level(logging.DEBUG)
    link = build_i1_link(
        WebSocketConnector(server.url, TOKEN, ca_file=str(cert)),
        Envelope(GATEWAY, SystemClock()),
        health_payload=health_payload,
        client_kwargs={"backoff": Backoff(initial_s=1.0, factor=2.0, max_s=1.0)},
    )
    link.start()
    try:
        assert wait_until(lambda: len(server.messages(0)) >= 1)
        stale = link.client.current_link()
        server.drop(0, code=1008, reason=TOKEN)
        assert wait_until(lambda: link.client.stats.disconnects >= 1)
        with pytest.raises(SendFailed) as e:
            link.client.send_on(stale, example("calibration_update"))
    finally:
        link.close()
    assert link.client.last_error and TOKEN not in link.client.last_error
    assert TOKEN not in str(e.value) and TOKEN not in client_logs(caplog)


class SilentServer:
    """HTTP 101 까지만 응답하고 다시 읽지 않는 TLS 서버(EC2 앱이 멈춘 상태). 수신 버퍼를 작게 잡아 송신이 빨리 막힌다."""

    def __init__(self, cert: Path, key: Path) -> None:
        import base64
        import hashlib
        import socket
        import ssl

        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(cert, key)
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self.accepted = 0
        self._conns: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._b64, self._sha1 = base64.b64encode, hashlib.sha1
        self._socket_timeout = socket.timeout
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        # 연결마다 따로 핸드셰이크한다: 한 연결이 핸드셰이크 도중 멈춰도 다음 연결(재연결)의 핸드셰이크를 막지 않는다
        while not self._stop.is_set():
            try:
                raw, _ = self._listener.accept()
            except (self._socket_timeout, OSError):
                continue
            threading.Thread(target=self._handshake, args=(raw,), daemon=True).start()

    def _handshake(self, raw) -> None:
        try:
            # 받은 소켓이 리스너의 0.1 s 시간 제한을 물려받지 않게 넉넉히 둔다(부하 중 TLS 핸드셰이크가 잘리지 않게).
            # 요청 전에 끊긴 연결(recv 가 b"")에서 무한히 돌지 않는다
            raw.settimeout(5.0)
            conn = self._ctx.wrap_socket(raw, server_side=True)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    raise OSError("요청 전에 끊김")
                data += chunk
            conn.settimeout(None)
            lines = data.decode().split("\r\n")[1:]
            headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines if ": " in line)}
            accept = self._b64(self._sha1((headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                          .encode()).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        except OSError:
            raw.close()
            return
        with self._lock:
            self._conns.append(conn)
            self.accepted += 1  # 이 뒤로는 읽지 않는다

    @property
    def url(self) -> str:
        return f"wss://127.0.0.1:{self.port}/ws/tier0"

    def stop(self) -> None:
        self._stop.set()
        self._listener.close()
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        self._thread.join(2)


def big_calibration_update() -> dict:
    payload = copy.deepcopy(example("calibration_update")["payload"])
    payload["padding_for_test"] = "x" * (8 * 1024 * 1024)  # TCP 창을 채운다
    return payload


@pytest.fixture
def silent(tmp_path):
    cert, key = make_cert(tmp_path)
    server = SilentServer(cert, key)
    yield server, cert
    server.stop()


def test_close_does_not_hang_when_ec2_stops_reading(silent) -> None:
    """N3: 상대가 읽지 않아 send 가 막혀도 close(timeout_s) 는 제한 시간 안에 돌아오고 스레드가 끝난다."""
    server, cert = silent
    link = build_i1_link(
        WebSocketConnector(server.url, TOKEN, ca_file=str(cert)), Envelope(GATEWAY, SystemClock()),
        health_payload=health_payload,
        client_kwargs={"backoff": Backoff(initial_s=0.2, max_s=0.2), "send_timeout_s": 60},
    )
    link.start()
    try:
        assert wait_until(lambda: link.client.connected)
        link.publisher.send_event("calibration_update", big_calibration_update())
        assert wait_until(lambda: link.client._sending is not None, timeout=5)
        time.sleep(0.5)
        assert link.client._sending is not None  # 막혀 있다
        t0 = time.monotonic()
        ok = link.close(timeout_s=2.0)
        took = time.monotonic() - t0
    finally:
        link.close(timeout_s=1)
    assert took < 3.0, took
    assert ok is True and link.alive_threads() == []


def test_send_watchdog_aborts_stuck_send_and_reconnects(silent) -> None:
    """N3: send 가 send_timeout_s 를 넘기면 끊고 다시 붙는다. 시간 초과 연결은 안정으로 보지 않아 간격이 는다."""
    server, cert = silent
    link = build_i1_link(
        WebSocketConnector(server.url, TOKEN, ca_file=str(cert)), Envelope(GATEWAY, SystemClock()),
        health_payload=health_payload,
        client_kwargs={"backoff": Backoff(initial_s=0.1, factor=2.0, max_s=0.4), "send_timeout_s": 0.5,
                       "stable_after_s": 0.0},
    )
    link.start()
    try:
        assert wait_until(lambda: link.client.connected)
        link.publisher.send_event("calibration_update", big_calibration_update())
        assert wait_until(lambda: link.client.stats.send_timeouts >= 1 and server.accepted >= 2, timeout=8)
        # 보내다 끊긴 신뢰 메시지는 버리지 않고 다음 연결에서 다시 보낸다(지금 다시 보내는 중이거나 대기열에 있다)
        assert link.client.stats.send_failures >= 1
        assert wait_until(lambda: link.client.queue_sizes()["reliable"] == 1 or link.client._sending is not None)
        # 서버가 두 번째 연결을 받은 뒤 클라이언트가 그 연결을 열기 전에 닫지 않는다(시험의 타이밍 가정 제거)
        assert wait_until(lambda: link.client.stats.connects >= 2)
    finally:
        assert link.close(timeout_s=2.0)
    assert link.client.stats.connects >= 2 and link.alive_threads() == []
