"""한 번에 한 스레드만 TLS 상태를 만지는 소켓 (I1 클라이언트·I0 제어 채널 서버, S15P31A101-175).

왜: websockets 동기 연결은 수신 스레드가 socket.recv 에 머무는 동안 다른 스레드가 socket.sendall 한다. ssl.SSLSocket 이면
한 SSL 객체를 두 스레드가 동시에 만진다(SSL_read·SSL_write 가 GIL 을 놓고 겹친다). OpenSSL 은 SSL 객체 하나를 여러 스레드가
동시에 쓰는 것을 허용하지 않는다. 서버가 TLS 1.3 NewSessionTicket(운영 nginx·JSSE 기본값)을 보내는 순간과 업그레이드 요청
쓰기가 겹치면 요청 레코드가 깨지거나 사라지고(서버: 읽기 시간 초과·record layer failure, 클라이언트: InvalidMessage), 원시
재현에서는 프로세스가 죽었다(Windows access violation, Linux segmentation fault).

어떻게: TLS 는 메모리 BIO 두 개와 ssl.SSLObject 로 한다. OS 소켓 입출력은 평문 소켓(raw)이 맡고, SSLObject 는 OS 핸들을 모른다.
- _tls 잠금: SSLObject·BIO 를 만지는 짧은 구간(암복호화, 메모리만)만 잡는다. 소켓 입출력은 이 잠금 밖에서 한다. 그래서 수신
  스레드가 recv 에 막혀 있어도 송신 스레드는 바로 암호화하고 보낸다(잠금이 recv 를 기다리지 않는다).
- _send 잠금: 나가는 암호문의 순서. 나가는 BIO 에서 꺼낸 바이트는 _send 를 쥔 스레드만 꺼내고, 놓기 전에 다 보낸다. BIO 는
  만든 순서대로 나오므로 TLS 레코드 순서가 지켜진다(수신 중에 생긴 응답 레코드 — TLS 1.3 KeyUpdate 응답, 경고 등 — 도 같다).
  _send 는 _tls 를 쥔 채 '나가는 BIO 가 비었음'을 본 그 자리에서 놓는다. 수신 스레드는 응답 레코드를 만든 뒤 _send 를
  기다리지 않고(acquire(blocking=False)) 시도만 한다. 실패하면 지금 쥔 스레드가 놓기 전에 그것까지 보낸다(빈 BIO 확인과
  놓기가 _tls 안에서 한 번에 일어나므로 빠뜨리는 순간이 없다). 수신이 송신에 막혀 서로 기다리는 일이 없다.
- _recv 잠금: 받은 암호문을 들어오는 BIO 에 받은 순서대로 넣는다(수신 스레드가 하나여도 지킨다).
- 핸드셰이크는 만들 때(아직 스레드 하나일 때) 끝낸다. 시작할 때 OS 소켓에 걸린 시간 제한이 핸드셰이크 전체의 마감이다
  (ssl.SSLSocket.do_handshake 와 같다). 레코드를 조금씩 흘리는 상대가 읽기마다 제한을 새로 받아 마감을 넘기지 못하게,
  읽기·쓰기마다 남은 시간만 건다. 넘으면 TimeoutError(그 전에 평문은 한 바이트도 나가지 않는다). 그 뒤 SSLObject.write 가 읽기를 기다려야 하는 상태(TLS 1.2 재협상)는
  지원하지 않고 ssl.SSLError 로 끊는다(재연결). 재협상을 받아들여 진행하는 대신 끊는 쪽이 안전하다(보안 하향 아님).

websockets 호환: 동기 클라이언트가 TLS 를 켤 때 부르는 `ssl.wrap_socket(sock, server_hostname=...)`(SerialTLSContext),
연결이 쓰는 recv·sendall·settimeout·shutdown·close·getsockname·getpeername, 동기 서버 ServerConnection(sock, protocol,
server, **kwargs) 생성자에 기댄다(덕 타이핑). 시험한 판은 잠금 파일의 websockets 17.2 뿐이다(pyproject 는 >=14). 판을
올리면 test_serial_tls·test_i1_tls 가 이 기대를 확인한다.

검증은 넘겨받은 SSLContext 그대로다(체인·호스트 이름·최소 버전·고정값 확인은 부르는 쪽 책임, wrap_bio 가 같은 검증을 한다).
shutdown 은 OS 소켓만 닫고(close_notify 를 보내지 않는다, ssl.SSLSocket.shutdown 과 같다) close 는 OS 소켓을 shutdown 한 뒤
닫는다(막힌 다른 스레드를 깨운다).
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any

_CHUNK = 256 * 1024
"""한 번에 암호화해 보내는 평문 크기(큰 메시지에서 _tls 를 오래 쥐지 않고 메모리 BIO 가 크게 자라지 않게)"""


class SerialTLSSocket:
    """websockets 동기 연결이 쓰는 소켓 메서드만 갖춘 TLS 소켓. recv·send 를 서로 다른 스레드가 동시에 불러도 SSL 상태는
    한 번에 한 스레드만 만진다. 여기 없는 소켓 메서드(recv_into·makefile 등)는 일부러 없다(평문으로 새지 않게)."""

    recv_bufsize = 65536

    def __init__(
        self,
        sock: socket.socket,
        context: ssl.SSLContext,
        *,
        server_side: bool = False,
        server_hostname: str | None = None,
        do_handshake: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(sock, ssl.SSLSocket):
            raise TypeError("이미 TLS 인 소켓은 감싸지 않는다(평문 소켓을 넘긴다)")
        self._sock = sock
        self._in = ssl.MemoryBIO()
        self._out = ssl.MemoryBIO()
        self._obj = context.wrap_bio(self._in, self._out, server_side=server_side, server_hostname=server_hostname)
        self._tls = threading.Lock()
        self._send = threading.Lock()
        self._recv = threading.Lock()
        self._mono = monotonic
        if do_handshake:
            self.do_handshake()

    # --- 핸드셰이크 -------------------------------------------------------------------

    def do_handshake(self) -> None:
        """핸드셰이크를 끝낸다. 지금 OS 소켓의 시간 제한(None 이면 무제한)이 전체 마감이다. 넘으면 TimeoutError.
        실패하면 경고 레코드를 보내 보고(실패해도 무시) 원래 예외를 올린다. 끝나면 소켓 시간 제한을 처음 값으로 되돌린다."""
        original = self._sock.gettimeout()
        deadline = None if original is None else self._mono() + original
        with self._recv, self._send:
            try:
                while True:
                    failure: BaseException | None = None
                    with self._tls:
                        try:
                            self._obj.do_handshake()
                            done = True
                        except ssl.SSLWantReadError:
                            done = False
                        except ssl.SSLError as e:
                            failure, done = e, True
                        out = self._out.read()
                    if out:
                        try:
                            self._remaining(deadline)
                            self._sock.sendall(out)
                        except OSError:
                            if failure is None:
                                raise
                    if failure is not None:
                        raise failure
                    if done:
                        return
                    self._remaining(deadline)
                    data = self._sock.recv(self.recv_bufsize)
                    with self._tls:
                        if data:
                            self._in.write(data)
                        else:
                            self._in.write_eof()
            finally:
                try:
                    self._sock.settimeout(original)
                except OSError:  # 그사이 닫혔다
                    pass

    def _remaining(self, deadline: float | None) -> None:
        """핸드셰이크 마감까지 남은 시간을 OS 소켓에 건다. 이미 지났으면 TimeoutError."""
        if deadline is None:
            return
        left = deadline - self._mono()
        if left <= 0:
            raise TimeoutError("TLS 핸드셰이크 제한 시간을 넘겼다")
        self._sock.settimeout(left)

    # --- 받기 -------------------------------------------------------------------------

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        """복호화한 바이트(최대 bufsize). 끝이면 b"" (close_notify 와 ssl.SSLSocket 기본처럼 잘린 EOF 도 b"")."""
        if flags:
            raise ValueError("flags 는 지원하지 않는다")
        with self._recv:
            while True:
                failure: BaseException | None = None
                data: bytes | None
                with self._tls:
                    try:
                        data = self._obj.read(bufsize)
                    except ssl.SSLWantReadError:
                        data = None
                    except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
                        data = b""
                    except ssl.SSLError as e:  # 깨진 레코드 등: 경고 레코드가 나가는 BIO 에 있을 수 있다
                        failure, data = e, b""
                    reply = self._out.pending > 0  # 수신이 만든 응답(KeyUpdate·경고 등)
                if reply:
                    self._flush_if_free()
                if failure is not None:
                    raise failure
                if data is not None:
                    return data
                raw = self._sock.recv(self.recv_bufsize)  # 잠금 밖: 송신 스레드는 그동안 암호화·송신한다
                with self._tls:
                    if raw:
                        self._in.write(raw)
                    else:
                        self._in.write_eof()

    # --- 보내기 -----------------------------------------------------------------------

    def send(self, data: Any) -> int:
        """data 의 앞부분(최대 _CHUNK)을 보내고 보낸 평문 길이를 돌려준다."""
        with memoryview(data) as view, view.cast("B") as octets:
            chunk = octets[:_CHUNK]
            self._send.acquire()
            try:
                self._encrypt(chunk)
            except BaseException:
                self._send.release()
                raise
            self._flush_and_release()
            return len(chunk)

    def sendall(self, data: Any) -> None:
        with memoryview(data) as view, view.cast("B") as octets:
            self._send.acquire()
            try:
                for start in range(0, len(octets), _CHUNK):
                    self._encrypt(octets[start:start + _CHUNK])
                    self._flush_locked()
            except BaseException:
                self._send.release()
                raise
            self._flush_and_release()

    def _encrypt(self, chunk: memoryview) -> None:
        """_send 를 쥔 채로 부른다."""
        with self._tls:
            try:
                written = self._obj.write(chunk)
            except ssl.SSLWantReadError:
                raise ssl.SSLError("TLS 쓰기 중 읽기가 필요한 상태(재협상 등)는 지원하지 않는다. 연결을 끊는다") from None
        if written != len(chunk):  # 메모리 BIO 는 크기 제한이 없어 다 쓴다
            raise ssl.SSLError("TLS 평문을 다 쓰지 못했다")

    def _flush_locked(self) -> None:
        """_send 를 쥔 채로: 나가는 BIO 가 빌 때까지 보낸다(잠금은 쥔 그대로)."""
        while True:
            with self._tls:
                out = self._out.read()
            if not out:
                return
            self._sock.sendall(out)

    def _flush_and_release(self) -> None:
        """_send 를 쥔 채로: 나가는 BIO 가 빌 때까지 보내고 _send 를 놓는다. 빈 것을 본 _tls 구간 안에서 놓는다."""
        held = True
        try:
            while True:
                with self._tls:
                    out = self._out.read()
                    if not out:
                        self._send.release()
                        held = False
                        return
                self._sock.sendall(out)
        finally:
            if held:
                self._send.release()

    def _flush_if_free(self) -> None:
        """수신 스레드: 응답 레코드를 보낸다. _send 를 다른 스레드가 쥐었으면 그 스레드가 놓기 전에 보낸다(기다리지 않음)."""
        if self._send.acquire(blocking=False):
            self._flush_and_release()

    # --- 소켓 -------------------------------------------------------------------------

    @property
    def raw(self) -> socket.socket:
        """OS 소켓(끊기 경로의 shutdown·대기 입출력 취소용). 여기로 직접 읽고 쓰지 않는다."""
        return self._sock

    @property
    def ssl_object(self) -> ssl.SSLObject:
        """서버 확인(인증서·공개키 지문)용. 읽기 전용으로만 쓴다."""
        return self._obj

    def getpeercert(self, binary_form: bool = False) -> Any:
        with self._tls:
            return self._obj.getpeercert(binary_form)

    def version(self) -> str | None:
        with self._tls:
            return self._obj.version()

    def cipher(self) -> tuple[str, str, int] | None:
        with self._tls:
            return self._obj.cipher()

    def settimeout(self, value: float | None) -> None:
        self._sock.settimeout(value)

    def gettimeout(self) -> float | None:
        return self._sock.gettimeout()

    def setsockopt(self, *args: Any) -> None:
        self._sock.setsockopt(*args)

    def getsockopt(self, *args: Any) -> Any:
        return self._sock.getsockopt(*args)

    def getsockname(self) -> Any:
        return self._sock.getsockname()

    def getpeername(self) -> Any:
        return self._sock.getpeername()

    def fileno(self) -> int:
        return self._sock.fileno()

    @property
    def family(self) -> int:
        return self._sock.family

    @property
    def type(self) -> int:
        return self._sock.type

    def shutdown(self, how: int) -> None:
        """OS 소켓만 shutdown 한다(TLS 상태는 그대로, 막힌 recv·send 를 푼다)."""
        self._sock.shutdown(how)

    def close(self) -> None:
        """OS 소켓을 shutdown 한 뒤 닫는다. Linux 는 close 만으로는 다른 스레드의 막힌 recv·send 가 깨어나지 않는다(그 스레드가
        닫힌 소켓에 계속 막힌다). shutdown 이 먼저 깨운다."""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:  # 이미 끊겼거나 닫혔다
            pass
        self._sock.close()

    def __enter__(self) -> SerialTLSSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"SerialTLSSocket(fileno={self._sock.fileno()})"


class SerialTLSContext:
    """websockets 의 ssl 인자 자리에 넣는 감싸개. wrap_socket 이 ssl.SSLSocket 대신 SerialTLSSocket 을 돌려준다.

    websockets 동기 클라이언트는 TLS 를 켤 때 ssl.wrap_socket(sock, server_hostname=...) 만 부른다(14~17 판). 검증 설정은
    감싼 SSLContext 그대로다.
    """

    def __init__(self, context: ssl.SSLContext) -> None:
        self.context = context

    @property
    def verify_mode(self) -> ssl.VerifyMode:
        return self.context.verify_mode

    @property
    def check_hostname(self) -> bool:
        return self.context.check_hostname

    def wrap_socket(
        self,
        sock: socket.socket,
        server_side: bool = False,
        do_handshake_on_connect: bool = True,
        server_hostname: str | None = None,
        **kwargs: Any,
    ) -> SerialTLSSocket:
        if kwargs:
            raise TypeError(f"지원하지 않는 wrap_socket 인자: {sorted(kwargs)}")
        return SerialTLSSocket(sock, self.context, server_side=server_side, server_hostname=server_hostname,
                               do_handshake=do_handshake_on_connect)
