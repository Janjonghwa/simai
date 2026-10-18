"""SerialTLSSocket 시험 (S15P31A101-175): TLS 상태를 한 번에 한 스레드만 만지는지, 운영 조건(서버가 TLS 1.3 세션 티켓을
보냄)에서 I1·I0 제어 채널이 깨지지 않는지, 검증·끊기 경로가 그대로인지 본다.

반례 장면: 티켓 켬 연속 재연결, 막힌 send 와 수신 동시, close 와 수신 오류 동시, 8 MB 송신 중 티켓 수신, TLS 1.2 만 주는 서버,
호스트 이름 불일치, 동시 읽기·쓰기 경합 재현(자식 프로세스가 죽지 않음), 수신이 만든 응답 레코드가 막힌 송신을 기다리지 않음.
"""

from __future__ import annotations

import hashlib
import json
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from test_i1_tls import (  # noqa: E402
    GATEWAY,
    TICKETS,
    TOKEN,
    Ec2Server,
    SilentServer,
    health_payload,
    make_cert,
    server_tls_context,
    upgrade_ok,
    wait_until,
)

from services.tier0.endpoint_registration import Backoff  # noqa: E402
from services.tier0.i1_client import Envelope, I1ConnectError, WebSocketConnector, abort_connection  # noqa: E402
from services.tier0.i1_publisher import build_i1_link  # noqa: E402
from services.tier0.serial_tls import SerialTLSContext, SerialTLSSocket  # noqa: E402
from simai_ai.risk import SystemClock  # noqa: E402

CHILD = Path(__file__).with_name("tls_race_child.py")
BIG = 8 * 1024 * 1024


class ScriptedServer:
    """연결마다 TLS 핸드셰이크 뒤 behavior(conn) 를 따로 스레드에서 돌리는 서버. 기본은 운영 조건(티켓 켬)."""

    def __init__(self, cert: Path, key: Path, behavior, *, tickets: bool = True, rcvbuf: int | None = None,
                 delay_s: float = 0.0) -> None:
        self._ctx = server_tls_context(cert, key, tickets=tickets)
        self._behavior = behavior
        self._delay_s = delay_s  # 받은 뒤 이만큼 기다렸다가 핸드셰이크한다(느린 상대)
        self._listener = socket.socket()
        if rcvbuf:
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self.conns: list = []
        self.errors: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                raw, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(raw,), daemon=True).start()

    def _handle(self, raw) -> None:
        try:
            raw.settimeout(10)
            time.sleep(self._delay_s)
            conn = self._ctx.wrap_socket(raw, server_side=True)
            self.conns.append(conn)
            self._behavior(conn)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{type(e).__name__}: {e}")

    def stop(self) -> None:
        self._listener.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


def serial_client(port: int, cert: Path) -> SerialTLSSocket:
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    tls = SerialTLSSocket(raw, ssl.create_default_context(cafile=str(cert)), server_hostname="127.0.0.1")
    tls.settimeout(None)
    return tls


def recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise OSError("EOF")
        buf += chunk
    return buf


@pytest.fixture
def cert_key(tmp_path):
    return make_cert(tmp_path)


# ---------------------------------------------------------------------------- 경합 재현


def test_concurrent_read_write_race_does_not_crash_or_break_requests(cert_key) -> None:
    """반례(경합 재현): 읽기 스레드를 먼저 띄우고 요청을 쓰는 원시 클라이언트로 티켓을 보내는 서버에 100 번 붙는다.
    자식 프로세스에서 돌려 죽으면(access violation·segfault) 종료 코드로 안다. SerialTLSSocket 은 죽지 않고 실패 0 이다.
    (같은 자식을 sslsocket 모드로 돌리면 실패가 난다 — 음성 대조는 MR 기록)"""
    cert, key = cert_key
    done = subprocess.run([sys.executable, str(CHILD), str(cert), str(key), "100", "serial"],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    assert done.stdout.strip().splitlines()[-1] == "RESULT fails=0 n=100", done.stdout


# ---------------------------------------------------------------------------- I1 (운영 조건)


@pytest.mark.parametrize("tickets", TICKETS)
def test_i1_connection_uses_serial_tls(cert_key, tickets) -> None:
    """websockets 가 wrap_socket 으로 SerialTLSSocket 을 쓴다(판이 바뀌어 ssl.SSLSocket 으로 돌아가면 여기서 깨진다)."""
    cert, key = cert_key
    server = Ec2Server(cert, key, tickets=tickets == "tickets_on")
    try:
        conn = WebSocketConnector(server.url, TOKEN, ca_file=str(cert)).connect()
        try:
            assert isinstance(conn.socket, SerialTLSSocket)
            assert conn.socket.version() == "TLSv1.3"
            conn.send(json.dumps({"msg_type": "health", "payload": {}}))
            assert wait_until(lambda: len(server.messages()) == 1)
        finally:
            conn.close()
    finally:
        server.stop()


def test_i1_consecutive_reconnects_with_tickets_never_fail(cert_key) -> None:
    """반례(티켓 켬 연속 재연결): 서버가 연결을 잇달아 끊어도 매번 다시 붙고(connect 실패 0), 새 연결의 첫 메시지는 health 다."""
    cert, key = cert_key
    server = Ec2Server(cert, key, tickets=True)
    link = build_i1_link(
        WebSocketConnector(server.url, TOKEN, ca_file=str(cert)), Envelope(GATEWAY, SystemClock()),
        health_payload=health_payload,
        client_kwargs={"backoff": Backoff(initial_s=0.01, factor=1.0, max_s=0.01), "stable_after_s": 0.0},
    )
    link.start()
    try:
        for i in range(25):
            assert wait_until(lambda: len(server.connections) == i + 1 and len(server.messages(i)) >= 1), i
            server.drop(i)
        assert wait_until(lambda: len(server.connections) == 26 and len(server.messages(25)) >= 1)
    finally:
        link.close()
        server.stop()
    assert link.client.stats.connect_failures == 0, link.client.last_error
    assert all(server.messages(i)[0]["msg_type"] == "health" for i in range(26))
    assert server.auth == [f"Bearer {TOKEN}"] * 26


def test_i1_works_when_server_offers_only_tls12(cert_key) -> None:
    """반례(서버가 TLS 1.2 만): 클라이언트를 낮추지 않아도(최소 1.2) 붙고 첫 health 를 보낸다."""
    cert, key = cert_key
    server = Ec2Server(cert, key, max_tls12=True)
    link = build_i1_link(WebSocketConnector(server.url, TOKEN, ca_file=str(cert)), Envelope(GATEWAY, SystemClock()),
                         health_payload=health_payload)
    link.start()
    try:
        assert wait_until(lambda: len(server.connections) == 1 and len(server.messages(0)) >= 1)
        with link.client._cond:
            conn = link.client._conn
        assert conn is not None and conn.socket.version() == "TLSv1.2"
    finally:
        link.close()
        server.stop()
    assert server.messages(0)[0]["msg_type"] == "health"


def test_hostname_mismatch_fails_before_token_is_sent(cert_key) -> None:
    """반례(검증 유지): 인증서는 127.0.0.1 용인데 localhost 로 붙으면 핸드셰이크에서 끊겨 업그레이드(토큰)가 나가지 않는다."""
    cert, key = cert_key
    server = Ec2Server(cert, key)
    try:
        connector = WebSocketConnector(f"wss://localhost:{server.port}/ws/tier0", TOKEN, ca_file=str(cert))
        with pytest.raises(I1ConnectError) as e:
            connector.connect()
        assert TOKEN not in str(e.value)
        assert server.requests == 0
    finally:
        server.stop()


# ---------------------------------------------------------------------------- 소켓 반례


def test_receive_keeps_flowing_while_send_is_blocked(cert_key) -> None:
    """반례(막힌 send 와 수신 동시): 상대가 읽지 않아 sendall 이 막혀 있어도 다른 스레드의 recv 는 계속 받는다(송신 잠금을
    기다리지 않는다). 그 뒤 OS 소켓 shutdown 으로 막힌 send 가 풀린다(끊기 경로)."""
    cert, key = cert_key
    count = 30

    def talk_without_reading(conn) -> None:  # 읽지 않고 30 개를 보낸다
        for i in range(count):
            conn.sendall(struct.pack("!I", i))
            time.sleep(0.02)
        time.sleep(30)

    server = ScriptedServer(cert, key, talk_without_reading, rcvbuf=4096)
    tls = serial_client(server.port, cert)
    send_error: list[BaseException] = []

    def blocked_send() -> None:
        try:
            tls.sendall(b"x" * BIG)
        except OSError as e:
            send_error.append(e)

    writer = threading.Thread(target=blocked_send, daemon=True)
    writer.start()
    got: list[int] = []

    def receive() -> None:
        try:
            for _ in range(count):
                got.append(struct.unpack("!I", recv_exact(tls, 4))[0])
        except OSError:
            pass

    threading.Thread(target=receive, daemon=True).start()
    try:
        assert wait_until(lambda: len(got) == count, timeout=10), got
        assert got == list(range(count))
        assert writer.is_alive()  # 송신은 아직 막혀 있다(그동안 받았다)
        abort_connection(_Owner(tls))
        writer.join(3)
        assert not writer.is_alive() and send_error
    finally:
        abort_connection(_Owner(tls))
        tls.close()
        server.stop()


class _Owner:
    """abort_connection 이 다루는 연결 흉내(socket 이 SerialTLSSocket). close 는 원래 닫기 경로다."""

    def __init__(self, sock) -> None:
        self.socket = sock
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


def test_large_send_while_session_tickets_arrive(cert_key) -> None:
    """반례(8 MB 송신 중 티켓 수신): 읽기 스레드를 먼저 띄우고 핸드셰이크 직후 8 MB 를 보낸다. 티켓 처리(읽기)와 큰 쓰기가
    겹쳐도 서버가 받은 바이트의 해시가 같고 응답이 온다. 연속으로 되풀이한다."""
    cert, key = cert_key

    def hash_echo(conn) -> None:
        size = struct.unpack("!Q", recv_exact(conn, 8))[0]
        digest = hashlib.sha256()
        left = size
        while left:
            chunk = conn.recv(min(65536, left))
            if not chunk:
                raise OSError("EOF")
            digest.update(chunk)
            left -= len(chunk)
        conn.sendall(digest.digest())

    server = ScriptedServer(cert, key, hash_echo)
    try:
        for i in range(5):
            payload = bytes([i]) * 1000 + b"y" * BIG
            tls = serial_client(server.port, cert)
            reply: list[bytes] = []
            reader = threading.Thread(target=lambda: reply.append(recv_exact(tls, 32)), daemon=True)
            reader.start()
            tls.sendall(struct.pack("!Q", len(payload)) + payload)
            reader.join(20)
            tls.close()
            assert reply == [hashlib.sha256(payload).digest()], (i, server.errors)
    finally:
        server.stop()
    assert server.errors == []


def test_abort_close_and_receive_error_at_once_end_cleanly(cert_key) -> None:
    """반례(close 와 수신 오류 동시): 송신이 막히고 수신이 기다리는 연결에, 서버가 TLS 가 아닌 바이트를 보내(수신 오류)는
    순간 abort 와 close 를 함께 부른다. 두 스레드가 풀리고 TLS·잠금 오류 외의 예외가 새지 않으며, 그 직후 새 연결이 깨지지
    않는다. 연속으로 되풀이한다."""
    cert, key = cert_key
    go = threading.Event()

    def garbage_after_signal(conn) -> None:  # 읽지 않다가 신호가 오면 TLS 레코드가 아닌 바이트를 바로 쓴다
        go.wait(10)
        socket.socket.send(conn, b"\x17\x03\x03\x00\x05garbage-not-tls")

    server = ScriptedServer(cert, key, garbage_after_signal, rcvbuf=4096)
    fresh = SilentServer(cert, key)  # 새 연결 확인용(101 까지 응답)
    try:
        for _ in range(15):
            go.clear()
            tls = serial_client(server.port, cert)
            leaked: list[BaseException] = []

            def run(fn) -> None:
                try:
                    fn()
                except (OSError, ssl.SSLError):
                    pass
                except BaseException as e:  # noqa: BLE001 - 새는 예외를 모은다
                    leaked.append(e)

            writer = threading.Thread(target=run, args=(lambda: tls.sendall(b"z" * BIG),), daemon=True)
            reader = threading.Thread(target=run, args=(lambda: [tls.recv(4096) for _ in range(100)],), daemon=True)
            writer.start()
            reader.start()
            time.sleep(0.05)
            start = threading.Barrier(3)
            racers = [threading.Thread(target=lambda f=f: (start.wait(), run(f)), daemon=True)
                      for f in (go.set, lambda: abort_connection(_Owner(tls)), tls.close)]
            for t in racers:
                t.start()
            for t in racers:
                t.join(3)
            writer.join(3)
            reader.join(3)
            assert not writer.is_alive() and not reader.is_alive()
            assert leaked == []
            assert upgrade_ok(fresh.port, cert)
    finally:
        server.stop()
        fresh.stop()


def test_reply_record_from_reader_never_waits_for_a_blocked_writer_and_keeps_order() -> None:
    """반례(수신이 만든 응답 레코드): 송신 스레드가 송신 잠금을 쥐고 막혀 있을 때 수신 스레드가 만든 응답(KeyUpdate 응답·경고
    자리)은 기다리지 않고 넘기고, 잠금을 쥔 쪽이 놓기 전에 만든 순서대로 보낸다(빠뜨리거나 뒤바꾸지 않는다)."""
    a, b = socket.socketpair()
    try:
        tls = SerialTLSSocket(a, ssl.create_default_context(), server_hostname="x", do_handshake=False)
        tls._send.acquire()  # 송신 스레드가 잠금을 쥐고 있다
        tls._out.write(b"W1")  # 송신 스레드가 만든 레코드
        tls._out.write(b"R1")  # 그 뒤 수신 스레드가 만든 응답 레코드
        reader = threading.Thread(target=tls._flush_if_free, daemon=True)  # 수신 스레드: 기다리지 않는다
        reader.start()
        reader.join(0.5)
        assert not reader.is_alive()
        tls._flush_and_release()  # 송신 스레드가 마무리하며 응답까지 보낸다
        b.settimeout(2)
        assert recv_exact(b, 4) == b"W1R1"
        assert not tls._send.locked()
        tls._out.write(b"R2")  # 아무도 쥐지 않았으면 수신 스레드가 바로 보낸다
        tls._flush_if_free()
        assert recv_exact(b, 2) == b"R2" and not tls._send.locked()
    finally:
        a.close()
        b.close()


class _HookedLock:
    """with 로 쓰는 잠금. 놓은 직후 on_release 를 (한 번) 부른다. 경합 순간을 결정적으로 만든다."""

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self.on_release = None

    def __enter__(self):
        self._inner.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self._inner.release()
        hook, self.on_release = self.on_release, None
        if hook is not None:
            hook()


def test_reply_made_right_after_the_writers_last_check_is_not_stranded() -> None:
    """반례(경합 순간): 송신 스레드가 '나가는 BIO 가 비었다'를 본 직후, 송신 잠금을 놓기 전에 수신 스레드가 응답 레코드를
    만들고 송신 잠금을 시도한다. 빈 것 확인과 놓기가 TLS 잠금 안에서 한 번에 일어나므로 그 레코드는 남겨지지 않고 나간다."""
    a, b = socket.socketpair()
    try:
        tls = SerialTLSSocket(a, ssl.create_default_context(), server_hostname="x", do_handshake=False)
        tls._tls = _HookedLock()

        def reader_makes_reply() -> None:  # 송신 스레드가 TLS 잠금을 놓은 바로 그 순간
            def run() -> None:
                with tls._tls:
                    tls._out.write(b"R1")
                tls._flush_if_free()

            t = threading.Thread(target=run, daemon=True)
            t.start()
            t.join(2)

        tls._send.acquire()  # 송신 스레드: 보낼 것을 다 보냈고 마무리한다
        tls._tls.on_release = reader_makes_reply
        tls._flush_and_release()
        b.settimeout(2)
        assert recv_exact(b, 2) == b"R1"
        assert not tls._send.locked()
    finally:
        a.close()
        b.close()


def test_wrap_socket_rejects_already_tls_and_unknown_options(cert_key) -> None:
    """이미 TLS 인 소켓을 다시 감싸거나 모르는 wrap_socket 인자(세션 재사용 등)를 조용히 무시하지 않는다."""
    cert, _ = cert_key
    context = ssl.create_default_context(cafile=str(cert))
    with socket.socket() as plain:
        with pytest.raises(TypeError):
            SerialTLSContext(context).wrap_socket(plain, server_hostname="127.0.0.1", session=None)
    with context.wrap_socket(socket.socket(), server_hostname="127.0.0.1", do_handshake_on_connect=False) as wrapped:
        with pytest.raises(TypeError):
            SerialTLSSocket(wrapped, context, server_hostname="127.0.0.1")


# ---------------------------------------------------------------------------- I0 제어 채널 서버


def test_i0_control_server_uses_serial_tls_under_two_way_traffic(cert_key) -> None:
    """I0 제어 채널 서버(websockets 동기 서버)도 연결 소켓이 SerialTLSSocket 이고, 서버 송신(송신 스레드)과 노트북 송신(연결
    스레드 수신)이 동시에 오가도 모두 도착한다. 서버는 운영 컨텍스트(video_ingest.server_context, 티켓 켬)다."""
    from websockets.sync.client import connect

    from services.tier0.i0_control import ControlServer
    from services.tier0.video_ingest import TokenVerifier, server_context, token_sha256

    cert, key = cert_key
    laptop_token = "laptop-175-token"
    received: list[dict] = []
    server = ControlServer(host="127.0.0.1", port=0, ssl_context=server_context(str(cert), str(key)),
                           tokens=TokenVerifier(frozenset({token_sha256(laptop_token)})), source_id=GATEWAY,
                           clock=SystemClock(), on_message=lambda m, g: received.append(m))
    server.start()
    try:
        ws = connect(f"wss://127.0.0.1:{server.port}/control",
                     ssl=SerialTLSContext(ssl.create_default_context(cafile=str(cert))),
                     additional_headers={"Authorization": f"Bearer {laptop_token}"}, open_timeout=5,
                     compression=None, proxy=None).__enter__()
        try:
            assert wait_until(lambda: server.connected)
            with server._lock:
                assert isinstance(server._conn.socket, SerialTLSSocket)
            n = 200
            got: list = []
            reader = threading.Thread(target=lambda: got.extend(ws.recv(timeout=10) for _ in range(n)), daemon=True)
            reader.start()
            for i in range(n):
                assert server.send("health", {"i": i})
                ws.send(json.dumps({"msg_type": "laptop_heartbeat", "payload": {"i": i}}))
            reader.join(15)
            assert [json.loads(m)["payload"]["i"] for m in got] == list(range(n))
            assert wait_until(lambda: len(received) == n)
            assert [m["payload"]["i"] for m in received] == list(range(n))
        finally:
            ws.close()
    finally:
        assert server.close(timeout_s=5)


def test_i1_handshake_that_never_completes_times_out_without_sending_token(cert_key) -> None:
    """반례(핸드셰이크가 끝나지 않음): TCP 는 받지만 TLS 를 말하지 않는 상대에게 open_timeout 안에 연결 실패로 끝난다."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    accepted: list = []
    threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True).start()
    cert, _ = cert_key
    try:
        connector = WebSocketConnector(f"wss://127.0.0.1:{port}/ws/tier0", TOKEN, ca_file=str(cert), open_timeout_s=0.5)
        t0 = time.monotonic()
        with pytest.raises(I1ConnectError) as e:
            connector.connect()
        assert time.monotonic() - t0 < 3.0
        assert TOKEN not in str(e.value)
        assert wait_until(lambda: accepted)
        accepted[0].settimeout(1)
        first = accepted[0].recv(65536)
        assert first[:1] == b"\x16" and TOKEN.encode() not in first  # ClientHello 만 갔다
    finally:
        for s in accepted:
            s.close()
        listener.close()


def test_i0_control_stalled_handshake_does_not_block_the_laptop(cert_key, monkeypatch) -> None:
    """반례(서버 쪽 핸드셰이크 정지): TCP 만 열고 TLS 를 시작하지 않는 상대가 있어도 노트북은 붙고, 정지한 연결은 제한
    시간 뒤 서버가 닫는다."""
    from websockets.sync.client import connect

    from services.tier0 import i0_control
    from services.tier0.video_ingest import TokenVerifier, server_context, token_sha256

    monkeypatch.setattr(i0_control, "HANDSHAKE_TIMEOUT_S", 0.5)
    cert, key = cert_key
    laptop_token = "laptop-175-token"
    server = i0_control.ControlServer(host="127.0.0.1", port=0, ssl_context=server_context(str(cert), str(key)),
                                      tokens=TokenVerifier(frozenset({token_sha256(laptop_token)})), source_id=GATEWAY,
                                      clock=SystemClock(), on_message=lambda m, g: None)
    server.start()
    stalled = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    try:
        ws = connect(f"wss://127.0.0.1:{server.port}/control",
                     ssl=SerialTLSContext(ssl.create_default_context(cafile=str(cert))),
                     additional_headers={"Authorization": f"Bearer {laptop_token}"}, open_timeout=5,
                     compression=None, proxy=None).__enter__()
        try:
            assert wait_until(lambda: server.connected)
        finally:
            ws.close()
        stalled.settimeout(5)
        t0 = time.monotonic()
        try:
            assert stalled.recv(1024) == b""  # 서버가 닫았다
        except ConnectionResetError:
            pass
        assert time.monotonic() - t0 < 4.0
    finally:
        stalled.close()
        assert server.close(timeout_s=5)


# ---------------------------------------------------------------------------- 핸드셰이크 전체 마감 (리뷰 F1)


def client_hello() -> bytes:
    """정상 ClientHello 바이트(느린 상대가 조금씩 흘릴 재료)."""
    out = ssl.MemoryBIO()
    obj = ssl.create_default_context().wrap_bio(ssl.MemoryBIO(), out, server_hostname="x")
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return out.read()


def drip(sock: socket.socket, data: bytes, interval_s: float, stop: threading.Event) -> threading.Thread:
    """data 를 한 바이트씩 interval_s 마다 보낸다(마감보다 짧은 간격으로 계속 조금씩 오는 상대)."""

    def run() -> None:
        for byte in data:
            if stop.is_set():
                return
            try:
                sock.sendall(bytes([byte]))
            except OSError:
                return
            stop.wait(interval_s)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def closed_by_peer(sock: socket.socket, timeout_s: float) -> float | None:
    """상대가 닫을 때까지 기다린 시간(초). 제한 안에 닫지 않으면 None."""
    sock.settimeout(timeout_s)
    t0 = time.monotonic()
    try:
        while sock.recv(65536):
            pass
    except TimeoutError:
        return None
    except OSError:  # 끊김(RST)도 닫힘이다
        pass
    return time.monotonic() - t0


def test_i1_partial_record_trickle_cannot_stretch_open_timeout(cert_key) -> None:
    """반례(리뷰 A2, 바이트 하나씩 오는 상대): TLS 레코드 헤더 뒤 본문을 0.08 s 마다 1 바이트씩 흘려도 I1 연결은 open_timeout
    (0.3 s) 안팎에서 실패하고 토큰은 나가지 않는다. 읽기마다 제한이 새로 시작되면 상대가 멈출 때까지 붙잡힌다."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()
    seen: list[bytes] = []

    def trickle() -> None:
        raw, _ = listener.accept()
        with raw:
            raw.settimeout(2)
            seen.append(raw.recv(65536))
            raw.sendall(bytes.fromhex("1603034000"))  # 16 KiB 짜리 핸드셰이크 레코드라고 알리고
            for _ in range(60):  # 본문은 1 바이트씩(최대 4.8 s)
                if stop.wait(0.08):
                    return
                try:
                    raw.sendall(b"\x00")
                except OSError:
                    return

    t = threading.Thread(target=trickle, daemon=True)
    t.start()
    cert, _ = cert_key
    try:
        connector = WebSocketConnector(f"wss://127.0.0.1:{listener.getsockname()[1]}/ws/tier0", TOKEN,
                                       ca_file=str(cert), open_timeout_s=0.3)
        t0 = time.monotonic()
        with pytest.raises(I1ConnectError):
            connector.connect()
        took = time.monotonic() - t0
    finally:
        stop.set()
        listener.close()
        t.join(2)
    assert took < 1.0, took
    assert seen and seen[0][:1] == b"\x16" and TOKEN.encode() not in seen[0]


@pytest.mark.parametrize("kind", ["serial", "sslsocket"])
def test_server_handshake_deadline_with_trickled_client_hello(cert_key, kind) -> None:
    """반례(리뷰 A1): ClientHello 를 0.05 s 마다 1 바이트씩 흘리는 상대에게 소켓 제한 0.3 s 가 핸드셰이크 전체 마감으로
    걸린다(ssl.SSLSocket 과 같은 동작). 마감 뒤에는 상대가 계속 보내도 핸드셰이크가 끝난다(TimeoutError)."""
    cert, key = cert_key
    ctx = server_tls_context(cert, key)
    a, b = socket.socketpair()
    stop = threading.Event()
    result: list = []

    def handshake() -> None:
        t0 = time.monotonic()
        try:
            if kind == "serial":
                SerialTLSSocket(a, ctx, server_side=True)
            else:
                ctx.wrap_socket(a, server_side=True)
            result.append(("ok", time.monotonic() - t0))
        except Exception as e:  # noqa: BLE001
            result.append((type(e).__name__, time.monotonic() - t0))

    a.settimeout(0.3)
    t = threading.Thread(target=handshake, daemon=True)
    t.start()
    dripper = drip(b, client_hello(), 0.05, stop)
    try:
        t.join(1.5)
        assert not t.is_alive(), "핸드셰이크가 마감을 넘겨 살아 있다"
    finally:
        stop.set()
        dripper.join(2)
        a.close()
        b.close()
    [(outcome, took)] = result
    assert outcome in ("TimeoutError", "timeout"), result
    assert took < 0.8, took


@pytest.mark.parametrize(("delay_s", "timeout_s", "completes"), [(0.5, 1.0, True), (1.0, 0.3, False)])
def test_handshake_just_before_and_after_the_deadline(cert_key, delay_s, timeout_s, completes) -> None:
    """반례(마감 직전 완료·직후 끊김): 서버가 delay_s 뒤에 핸드셰이크를 시작한다. 마감 안이면 끝나고(시간 제한은 처음 값으로
    돌아온다), 마감을 넘기면 마감 안팎에서 TimeoutError 로 끊긴다(서버를 끝까지 기다리지 않는다)."""
    cert, key = cert_key
    server = ScriptedServer(cert, key, lambda conn: time.sleep(5), delay_s=delay_s)
    raw = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    raw.settimeout(timeout_s)
    t0 = time.monotonic()
    try:
        if completes:
            tls = SerialTLSSocket(raw, ssl.create_default_context(cafile=str(cert)), server_hostname="127.0.0.1")
            took = time.monotonic() - t0
            assert tls.version() == "TLSv1.3"
            assert delay_s - 0.1 <= took < timeout_s, took  # 서버의 기다림은 accept 때 시작한다(시계 해상도 여유)
            assert tls.gettimeout() == timeout_s
        else:
            with pytest.raises(TimeoutError):
                SerialTLSSocket(raw, ssl.create_default_context(cafile=str(cert)), server_hostname="127.0.0.1")
            took = time.monotonic() - t0
            assert timeout_s - 0.05 <= took < timeout_s + 0.4, took  # 시계 해상도 여유
    finally:
        raw.close()
        server.stop()


def test_handshake_completes_when_peer_sends_one_byte_per_segment(cert_key) -> None:
    """반례(바이트 하나씩, 마감 안): 서버의 핸드셰이크 바이트를 한 바이트씩 나눠 건네는 중계를 거쳐도 핸드셰이크가 끝나고
    데이터가 오간다(부분 레코드를 이어 붙이는 경로)."""
    cert, key = cert_key

    def echo(conn) -> None:
        conn.sendall(recv_exact(conn, 4))
        time.sleep(1)

    server = ScriptedServer(cert, key, echo)
    relay = socket.socket()
    relay.bind(("127.0.0.1", 0))
    relay.listen(1)

    def pump(src, dst, one_byte: bool) -> None:
        try:
            while chunk := src.recv(65536):
                if one_byte:
                    for i in range(len(chunk)):
                        dst.sendall(chunk[i:i + 1])
                else:
                    dst.sendall(chunk)
        except OSError:
            pass

    def run_relay() -> None:
        client, _ = relay.accept()
        upstream = socket.create_connection(("127.0.0.1", server.port))
        for s_ in (client, upstream):
            s_.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=pump, args=(client, upstream, False), daemon=True).start()
        pump(upstream, client, True)

    threading.Thread(target=run_relay, daemon=True).start()
    try:
        raw = socket.create_connection(("127.0.0.1", relay.getsockname()[1]), timeout=5)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tls = SerialTLSSocket(raw, ssl.create_default_context(cafile=str(cert)), server_hostname="127.0.0.1")
        tls.sendall(b"ping")
        assert recv_exact(tls, 4) == b"ping"
        tls.close()
    finally:
        relay.close()
        server.stop()


def _control_server(cert, key, token: str):
    from services.tier0.i0_control import ControlServer
    from services.tier0.video_ingest import TokenVerifier, server_context, token_sha256

    return ControlServer(host="127.0.0.1", port=0, ssl_context=server_context(str(cert), str(key)),
                         tokens=TokenVerifier(frozenset({token_sha256(token)})), source_id=GATEWAY,
                         clock=SystemClock(), on_message=lambda m, g: None)


def _laptop(port: int, cert, token: str):
    from websockets.sync.client import connect

    return connect(f"wss://127.0.0.1:{port}/control", ssl=SerialTLSContext(ssl.create_default_context(cafile=str(cert))),
                   additional_headers={"Authorization": f"Bearer {token}"}, open_timeout=3, compression=None,
                   proxy=None).__enter__()


def test_i0_many_slow_peers_are_cut_at_the_deadline_and_do_not_block_the_laptop(cert_key, monkeypatch) -> None:
    """반례(I0 에 느린 상대 여럿 동시): ClientHello 를 0.1 s 마다 1 바이트씩 흘리는 상대 6 개가 있어도 노트북은 바로 붙고,
    느린 상대는 모두 핸드셰이크 마감(0.5 s) 안팎에서 서버가 끊는다. 정지도 곧 끝난다."""
    from services.tier0 import i0_control

    monkeypatch.setattr(i0_control, "HANDSHAKE_TIMEOUT_S", 0.5)
    cert, key = cert_key
    server = _control_server(cert, key, "laptop-175-token")
    server.start()
    stop = threading.Event()
    slow = [socket.create_connection(("127.0.0.1", server.port), timeout=5) for _ in range(6)]
    drippers = [drip(s_, client_hello(), 0.1, stop) for s_ in slow]
    closed: list[float | None] = [None] * len(slow)

    def watch(i: int) -> None:
        closed[i] = closed_by_peer(slow[i], 3.0)

    watchers = [threading.Thread(target=watch, args=(i,), daemon=True) for i in range(len(slow))]
    for w in watchers:
        w.start()
    try:
        t0 = time.monotonic()
        ws = _laptop(server.port, cert, "laptop-175-token")
        try:
            assert time.monotonic() - t0 < 1.0
            assert wait_until(lambda: server.connected)
        finally:
            ws.close()
        for w in watchers:
            w.join(4)
        assert all(c is not None and c < 1.2 for c in closed), closed
        t1 = time.monotonic()
        assert server.close(timeout_s=2)
        assert time.monotonic() - t1 < 1.5
    finally:
        stop.set()
        for d in drippers:
            d.join(2)
        for s_ in slow:
            s_.close()
        server.close(timeout_s=1)


def test_i0_close_cuts_handshakes_and_upgrades_in_progress(cert_key) -> None:
    """반례(리뷰 A3, stop 중 핸드셰이크): 기본 마감(10 s) 그대로, TLS 를 흘리는 상대와 TLS 는 마쳤지만 HTTP 업그레이드를
    보내지 않는 상대가 있어도 close(timeout_s=1) 은 곧 True 로 돌아오고 두 상대 모두 끊긴다."""
    from services.tier0 import i0_control

    assert i0_control.HANDSHAKE_TIMEOUT_S == 10.0
    cert, key = cert_key
    server = _control_server(cert, key, "laptop-175-token")
    server.start()
    stop = threading.Event()
    trickler = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    dripper = drip(trickler, client_hello(), 0.1, stop)
    silent_raw = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    silent = SerialTLSSocket(silent_raw, ssl.create_default_context(cafile=str(cert)), server_hostname="127.0.0.1")
    try:
        time.sleep(0.4)  # 둘 다 서버 쪽 스레드에 들어가 있다(핸드셰이크 중·업그레이드 요청 대기 중)
        with server._server.lock:
            assert len(server._server.handler_threads) == 2
        t0 = time.monotonic()
        assert server.close(timeout_s=1)
        assert time.monotonic() - t0 < 1.5
        assert closed_by_peer(trickler, 2.0) is not None
        silent.settimeout(2)
        try:
            assert silent.recv(1024) == b""
        except (OSError, ssl.SSLError):
            pass
    finally:
        stop.set()
        dripper.join(2)
        trickler.close()
        silent.close()


def test_i0_handshake_starting_after_close_is_refused(cert_key) -> None:
    """반례(stop 과 새 핸드셰이크가 겹침): close 가 시작된 뒤 등록하려는 핸드셰이크는 거절된다(close 가 놓치지 않는다)."""
    cert, key = cert_key
    server = _control_server(cert, key, "laptop-175-token")
    server.start()
    a, b = socket.socketpair()
    try:
        assert server.close(timeout_s=2)
        with pytest.raises(ConnectionAbortedError):
            server._opening_started(a)
        assert a not in set(server._opening)
    finally:
        a.close()
        b.close()
