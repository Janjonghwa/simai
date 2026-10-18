"""TLS 동시 읽기·쓰기 경합 재현 자식 프로세스 (S15P31A101-175 시험용, test_serial_tls 가 띄운다).

websockets 동기 클라이언트처럼 읽기 스레드를 먼저 띄운 뒤 업그레이드 요청을 쓴다. 서버는 TLS 1.3 세션 티켓을 보낸다(운영
기본값). ssl.SSLSocket 이면 티켓 처리(읽기)와 요청 쓰기가 한 SSL 객체에서 겹쳐 요청이 깨지거나 프로세스가 죽는다(Windows
access violation, Linux segmentation fault). 죽으면 부모가 종료 코드로 안다.

사용: python tls_race_child.py <cert> <key> <회수> <serial|sslsocket>
출력 마지막 줄: RESULT fails=<실패 수> n=<회수>
"""

from __future__ import annotations

import base64
import hashlib
import socket
import ssl
import sys
import threading
from pathlib import Path

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.tier0.serial_tls import SerialTLSSocket  # noqa: E402

GET = (b"GET /ws/tier0 HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
       b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
TIMEOUT_S = 3.0


class TicketServer:
    """업그레이드 요청을 받으면 101 을 돌려주는 TLS 1.3 서버(세션 티켓 기본 2 장)."""

    def __init__(self, cert: str, key: str) -> None:
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.keep: list = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(raw,), daemon=True).start()

    def handle(self, raw: socket.socket) -> None:
        try:
            raw.settimeout(TIMEOUT_S)
            conn = self.ctx.wrap_socket(raw, server_side=True)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            key = [line.split(": ", 1)[1] for line in data.decode().split("\r\n") if line.lower().startswith("sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            self.keep.append(conn)
        except OSError:
            raw.close()


def once(port: int, ctx: ssl.SSLContext, mode: str) -> bool:
    raw = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT_S)
    if mode == "serial":
        tls = SerialTLSSocket(raw, ctx, server_hostname="127.0.0.1")
        os_sock = tls.raw
    else:
        tls = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
        os_sock = tls
    tls.settimeout(None)
    got: list[bytes] = []
    done = threading.Event()

    def reader() -> None:
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        got.append(data)
        done.set()

    threading.Thread(target=reader, daemon=True).start()  # websockets 처럼 요청을 쓰기 전에 읽기 스레드부터
    try:
        tls.sendall(GET)
    except OSError:
        pass
    ok = done.wait(TIMEOUT_S + 1)
    try:
        socket.socket.shutdown(os_sock, socket.SHUT_RDWR)
    except OSError:
        pass
    done.wait(2)
    tls.close()
    return ok and bool(got) and got[0].startswith(b"HTTP/1.1 101")


def main() -> int:
    cert, key, n, mode = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    server = TicketServer(cert, key)
    ctx = ssl.create_default_context(cafile=cert)
    fails = sum(0 if once(server.port, ctx, mode) else 1 for _ in range(n))
    print(f"RESULT fails={fails} n={n}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
