"""공개키 고정·엔드포인트 조회 시험 도구: 자체 서명 인증서(시험 중 생성, 커밋 금지), Tier 0 TLS 제어 서버, 가짜 EC2.

인증서와 비밀 키는 pytest tmp_path 에만 만든다(저장소에 남지 않는다).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import http.server
import json
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CertFiles:
    cert_file: Path
    key_file: Path
    der: bytes
    fingerprint: str  # sha256:<hex>, cryptography 가 계산한 SubjectPublicKeyInfo DER 의 SHA-256


def make_cert(directory: Path, name: str = "tier0", *, rsa: bool = False, loopback_san: bool = False) -> CertFiles:
    """loopback_san: 127.0.0.1·localhost 이름을 넣는다(호스트 이름까지 검증하는 HTTPS 시험 서버, S15P31A101-91)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod
    from cryptography.x509.oid import NameOID

    key = rsa_mod.generate_private_key(public_exponent=65537, key_size=2048) if rsa else ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name}-test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
    )
    if loopback_san:
        import ipaddress

        cert = cert.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]),
            critical=False,
        )
    cert = cert.sign(key, hashes.SHA256())
    directory.mkdir(parents=True, exist_ok=True)
    cert_file, key_file = directory / f"{name}.crt", directory / f"{name}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return CertFiles(cert_file, key_file, cert.public_bytes(serialization.Encoding.DER),
                    "sha256:" + hashlib.sha256(spki).hexdigest())


def server_context(cert: CertFiles) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert.cert_file, cert.key_file)
    return ctx


def wait_until(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("시간 안에 조건이 맞지 않았다")
        time.sleep(0.01)


def endpoint_doc(ip: str, control_port: int, fingerprint: str, *, registered_at: int = 1760340000100,
                 gateway_id: str = "tier0-01") -> dict:
    """EC2 조회 응답 본문(Tier 0 가 등록한 tier0_endpoint 봉투 그대로)."""
    return {
        "schema_version": "1.3.0",
        "msg_type": "tier0_endpoint",
        "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000304",
        "sent_at": registered_at + 20,
        "source_id": gateway_id,
        "payload": {
            "gateway_id": gateway_id,
            "public_ip": ip,
            "ports": [
                {"role": "CONTROL", "requested_port": 70000, "public_port": control_port},
                {"role": "VIDEO", "camera_id": "cam1", "requested_port": 70001, "public_port": 44465},
                {"role": "VIDEO", "camera_id": "cam2", "requested_port": 70002, "public_port": 44466},
            ],
            "tls_public_key_sha256": fingerprint,
            "registered_at": registered_at,
            "pod_id": "abc123xyz",
            "datacenter": "AP-JP-1",
        },
    }


class TlsTier0:
    """Tier 0 I0 제어 채널 흉내(wss, 시험용 자체 서명 인증서). 접속마다 인증 헤더와 받은 메시지를 기록한다."""

    def __init__(self, cert: CertFiles, host: str = "127.0.0.1", hello: list[dict] | None = None,
                 *, close_immediately: bool = False) -> None:
        self.cert = cert
        self.close_immediately = close_immediately
        self.connected_at: list[float] = []
        self.host = host
        self.hello = hello or []
        self.auth: list[str | None] = []
        self.paths: list[str] = []
        self.received: list[dict] = []
        self.port: int | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._conns: list = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _handler(self, ws) -> None:
        from websockets.exceptions import ConnectionClosed

        self._conns.append(ws)
        self.auth.append(ws.request.headers.get("Authorization"))
        self.paths.append(ws.request.path)
        self.connected_at.append(time.monotonic())
        if self.close_immediately:  # 받자마자 끊는 서버
            await ws.close()
            return
        try:
            for msg in self.hello:
                await ws.send(json.dumps(msg))
            async for raw in ws:
                self.received.append(json.loads(raw))
        except ConnectionClosed:
            pass

    async def _main(self) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        async with serve(self._handler, self.host, 0, ssl=server_context(self.cert)) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._loop.run_in_executor(None, self._stop.wait)
            for ws in list(self._conns):
                await ws.close()

    def __enter__(self) -> TlsTier0:
        self._thread.start()
        assert self._ready.wait(5)
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(5)


class RawTlsServer:
    """TLS 핸드셰이크만 받고, 그 뒤 클라이언트가 보낸 바이트를 모두 기록하는 서버(사칭 서버 흉내)."""

    def __init__(self, cert: CertFiles, host: str = "127.0.0.1") -> None:
        self._ctx = server_context(cert)
        self._sock = socket.socket()
        self._sock.bind((host, 0))
        self._sock.listen()
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
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


class FakeEc2:
    """EC2 `GET /api/endpoints/tier0` 흉내. current 를 바꿔 Pod 재설정을 흉내 내고, fail_next 로 실패 응답을 먼저 낸다."""

    def __init__(self, current: dict | None = None) -> None:
        self.current = current
        self.fail_next: list[int] = []
        self.requests: list[tuple[str, str | None]] = []
        ec2 = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                ec2.requests.append((self.path, self.headers.get("Authorization")))
                if ec2.fail_next:
                    self._reply(ec2.fail_next.pop(0), b"")
                elif self.path != "/api/endpoints/tier0":
                    self._reply(404, b"")
                elif ec2.current is None:
                    self._reply(404, b"")
                else:
                    self._reply(200, json.dumps(ec2.current).encode())

            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}"

    def __enter__(self) -> FakeEc2:
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()


class _TlsThreadingServer(http.server.ThreadingHTTPServer):
    """TLS 수립을 받는 스레드가 아니라 요청 스레드에서, 제한 시간을 두고 한다(S15P31A101-370).

    듣는 소켓을 통째로 감싸면 accept 안에서 제한 없이 수립해, 느린 CI 에서 수립 하나가 끝나지 않을 때
    serve_forever 가 멈추고 shutdown() 이 끝없이 기다렸다.
    """

    daemon_threads = True
    handshake_timeout_s = 5.0

    def __init__(self, address, handler, context: ssl.SSLContext) -> None:
        self._tls_context = context
        super().__init__(address, handler)

    def finish_request(self, request, client_address) -> None:
        request.settimeout(self.handshake_timeout_s)
        try:
            tls = self._tls_context.wrap_socket(request, server_side=True)
        except (OSError, ssl.SSLError):
            return  # 수립 실패·시간 초과는 그 연결만 버린다
        try:
            super().finish_request(tls, client_address)
        finally:
            tls.close()


class HttpsEc2:
    """하트비트 시험용 로컬 HTTPS EC2 (S15P31A101-91). 인증서는 시험 중에만 만든 자체 서명(loopback_san).

    mode: "ok"(200), "status:<코드>", "delay:<초>"(그만큼 늦게 200), "redirect:<주소>"(302). 받은 요청을 requests 에 쌓는다.
    """

    def __init__(self, cert: CertFiles, mode: str = "ok") -> None:
        self.mode = mode
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body,
                                       "at": time.monotonic()})
                mode = outer.mode
                if mode.startswith("delay:"):
                    time.sleep(float(mode.split(":", 1)[1]))
                    mode = "ok"
                if mode.startswith("redirect:"):
                    self.send_response(302)
                    self.send_header("Location", mode.split(":", 1)[1])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = int(mode.split(":", 1)[1]) if mode.startswith("status:") else 200
                payload = b"{}"
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

            def log_message(self, *args) -> None:
                pass

        self.server = _TlsThreadingServer(("127.0.0.1", 0), Handler, server_context(cert))
        self.port = self.server.server_address[1]
        self.base_url = f"https://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self) -> HttpsEc2:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
