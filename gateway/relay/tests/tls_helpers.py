"""Tier 0 영상 수신 흉내(시험용 TLS 서버). 인증서와 비밀 키는 pytest tmp_path 에만 만든다(저장소에 남지 않는다)."""

from __future__ import annotations

import hashlib
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field

from simai_relay import wire
from simai_relay.bench import SelfSigned


def server_context(cert: SelfSigned) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert.cert_file, cert.key_file)
    return ctx


def wait_until(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("시간 안에 조건이 맞지 않았다")
        time.sleep(0.01)


@dataclass
class Connection:
    camera_id: str | None
    hello: dict
    records: list[tuple[int, dict, int, float]] = field(default_factory=list)  # (kind, meta, data 길이, 수신 시각)
    digests: list[str] = field(default_factory=list)  # FRAME data 의 SHA-256 (내용까지 같은지 볼 때)

    def frames(self) -> list[dict]:
        return [m for k, m, _, _ in self.records if k == wire.FRAME]

    def kinds(self) -> list[int]:
        return [k for k, _, _, _ in self.records]


class FakeTier0Video:
    """카메라별 영상 연결을 받는 TLS 서버 흉내.

    tokens: 받아들일 기기 토큰. stall: HELLO_ACK 뒤 읽지 않는 카메라(수신 멈춤). close_after: 카메라별로 첫 연결에서
    프레임 n 개를 받은 뒤 끊는다. rcvbuf: 수신 버퍼(작게 하면 멈춘 수신이 송신 쪽으로 빨리 번진다).
    """

    def __init__(self, cert: SelfSigned, tokens: set[str], *, stall: set[str] | None = None,
                 close_after: dict[str, int] | None = None, rcvbuf: int | None = None,
                 pause_s: float = 0.0, reject_epochs_below: int | None = None) -> None:
        self._ctx = server_context(cert)
        self._sock = socket.socket()
        if rcvbuf:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.tokens = tokens
        self.stall = set(stall or ())
        self.close_after = dict(close_after or {})
        self.reject_epochs_below = reject_epochs_below  # 이보다 작은 epoch 의 codec_info 에 STREAM_REJECT
        self.pause_s = pause_s  # HELLO_ACK 뒤 이만큼 읽지 않다가 읽는다(송신 쪽 시간 제한·부분 쓰기 유도)
        self.connections: list[Connection] = []
        self.handshakes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._open: list[ssl.SSLSocket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def by_camera(self, camera_id: str) -> list[Connection]:
        with self._lock:
            return [c for c in self.connections if c.camera_id == camera_id]

    def frames(self, camera_id: str) -> list[dict]:
        return [f for c in self.by_camera(camera_id) for f in c.frames()]

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10)
            with self._ctx.wrap_socket(conn, server_side=True) as tls:
                with self._lock:
                    self.handshakes += 1
                    self._open.append(tls)
                rec = wire.read_record(tls, max_meta=wire.MAX_HELLO_META, max_data=0)
                if rec is None:
                    return
                hello = rec[1]
                cam = hello.get("camera_id")
                c = Connection(cam if isinstance(cam, str) else None, hello)
                with self._lock:
                    self.connections.append(c)
                if rec[0] != wire.HELLO or hello.get("device_token") not in self.tokens:
                    tls.sendall(wire.encode(wire.HELLO_ACK, {"ok": False, "reason": "unauthorized"}))
                    return
                tls.sendall(wire.encode(wire.HELLO_ACK, {"ok": True}))
                if cam in self.stall:
                    self._stop.wait()
                    return
                tls.settimeout(None)
                if self.pause_s:
                    time.sleep(self.pause_s)
                limit = self.close_after.pop(cam, None)
                while not self._stop.is_set():
                    r = wire.read_record(tls)
                    if r is None:
                        return
                    c.records.append((r[0], r[1], len(r[2]), time.perf_counter()))
                    if (r[0] == wire.CODEC_INFO and self.reject_epochs_below is not None
                            and r[1]["stream_epoch"] < self.reject_epochs_below):
                        tls.sendall(wire.encode(wire.STREAM_REJECT, {"reason": "stale_epoch",
                                                                     "stream_epoch": r[1]["stream_epoch"],
                                                                     "current_epoch": self.reject_epochs_below}))
                        while wire.read_record(tls) is not None:  # 노트북이 읽고 닫을 때까지 버린다
                            pass
                        return
                    if r[0] == wire.FRAME:
                        c.digests.append(hashlib.sha256(r[2]).hexdigest())
                    if limit is not None and len(c.frames()) >= limit:
                        return
        except (OSError, ssl.SSLError, wire.WireError):
            return

    def __enter__(self) -> FakeTier0Video:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(5)
        self._sock.close()
        with self._lock:
            for tls in self._open:
                try:
                    socket.socket.shutdown(tls, socket.SHUT_RDWR)
                except OSError:
                    pass


class RawTlsServer:
    """TLS 핸드셰이크만 받고, 그 뒤 클라이언트가 보낸 바이트를 모두 기록하는 서버(사칭 서버 흉내)."""

    def __init__(self, cert: SelfSigned) -> None:
        self._ctx = server_context(cert)
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen()
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.accepted = 0
        self.handshakes = 0
        self.received = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self.accepted += 1
            try:
                conn.settimeout(2)
                with self._ctx.wrap_socket(conn, server_side=True) as tls:
                    self.handshakes += 1
                    while True:
                        data = tls.recv(4096)
                        if not data:
                            break
                        self.received.extend(data)
            except (OSError, ssl.SSLError):
                pass

    def __enter__(self) -> RawTlsServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(5)
        self._sock.close()
