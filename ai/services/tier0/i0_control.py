"""I0 제어 채널 서버(Tier 0 쪽, wss): 노트북과 local_alert·alarm_stop·health·시계 교환·site_ack·laptop_heartbeat 를 주고받는다.

명세 12.2 I0(제어는 WebSocket(wss), 노트북 기기 토큰으로 인증), 3.10(현장 경보·재동기화), 3.9(시계 차이 교환), 14(TLS 는
Tier 0 가 직접 종료), 3.12(포트는 대칭 매핑). 노트북 쪽 클라이언트는 gateway/site_alert(transport.WebSocketControlChannel).

- 인증: TLS 핸드셰이크 뒤 HTTP 업그레이드 요청의 Authorization: Bearer <기기 토큰> 을 video_ingest.TokenVerifier(SHA-256
  지문 목록, 상수 시간 비교)로 본다. 맞지 않으면 401 로 거절하고 연결 훅을 부르지 않는다. 토큰은 로그에 넣지 않는다.
- 노트북은 하나다. 새 연결이 오면 이전 연결을 끊는다(노트북 재접속). 연결마다 세대 번호가 하나 는다.
- 송신은 한 줄 대기열 하나와 송신 스레드 하나로 한다(local_alert·alarm_stop·health 의 순서가 섞이지 않게, 계약
  health.active_site_alerts 경합 방지). send 는 기다리지 않는다. 연결이 없으면 넣지 않고 False(끊긴 동안의 경보는 재접속
  직후 active_site_alerts 가 맞춘다). 대기열에 남은 이전 연결 세대의 메시지는 새 연결로 보내지 않는다.
- 받은 메시지(site_ack·laptop_heartbeat·clock_sync_response)는 연결 스레드에서 on_message(메시지, 세대)로 넘긴다. 처리 로직은
  넘겨받는 쪽(판정 루프·시계 교환·연결 감시)이 기존 모듈로 한다. JSON 이 아니거나 봉투가 틀리면 세고 버린다.
- 연결 훅: on_connected(세대)는 연결을 연 직후(그 연결의 첫 수신 전)에, on_disconnected(세대)는 그 연결이 끝난 뒤 부른다.

봉투 sent_at 은 주입 시계(health.active_site_alerts_as_of 와 같은 시계, 판정 시계)로 만든다.
"""

from __future__ import annotations

import json
import logging
import queue
import ssl
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from services.tier0.i1_client import abort_connection
from services.tier0.video_ingest import TokenVerifier
from simai_ai.risk import Clock

log = logging.getLogger("services.tier0.i0_control")

# I0 아래로(Tier 0 → 노트북) 메시지와 봉투 schema_version (그 메시지 계약이 마지막으로 바뀐 버전, contracts/examples)
DOWNSTREAM_VERSIONS: Mapping[str, str] = {
    "local_alert": "1.2.0",
    "alarm_stop": "2.1.0",
    "health": "1.5.0",
    "clock_sync_request": "1.3.0",
}
UPSTREAM_TYPES = frozenset({"site_ack", "laptop_heartbeat", "clock_sync_response"})

OnMessage = Callable[[Mapping[str, Any], int], Any]
Hook = Callable[[int], Any]


class _RedactAuthorization(logging.Filter):
    """websockets DEBUG 로그의 요청 헤더 줄(Authorization)을 가린다(기기 토큰이 로그에 나가지 않게)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if "authorization" in text.lower():
            record.msg, record.args = "> Authorization: ***", ()
        return True


_ws_log = logging.getLogger("services.tier0.i0_control.websockets")
_ws_log.addFilter(_RedactAuthorization())


@dataclass
class ControlStats:
    connections: int = 0
    auth_rejections: int = 0
    superseded: int = 0
    sent: dict[str, int] = field(default_factory=dict)
    not_connected: dict[str, int] = field(default_factory=dict)  # 연결이 없어 넣지 않은 메시지
    stale_dropped: int = 0  # 이전 연결 세대라 보내지 않은 메시지
    send_errors: int = 0
    received: dict[str, int] = field(default_factory=dict)
    malformed: int = 0
    unknown: int = 0
    handler_errors: int = 0
    queue_full: int = 0


def bearer_token(headers: Any) -> str | None:
    value = headers.get("Authorization") if headers is not None else None
    if not isinstance(value, str) or not value.startswith("Bearer "):
        return None
    return value[len("Bearer "):].strip() or None


class ControlServer:
    """노트북 I0 제어 채널 서버. start() 뒤 듣기 스레드·송신 스레드가 돈다."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        ssl_context: ssl.SSLContext | None,
        tokens: TokenVerifier,
        source_id: str,
        clock: Clock,
        on_message: OnMessage,
        on_connected: Hook | None = None,
        on_disconnected: Hook | None = None,
        max_queue: int = 1000,
        new_msg_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        if ssl_context is None and host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("I0 제어 채널은 wss 다. TLS 없이 여는 것은 같은 컴퓨터(loopback) 시험뿐이다")
        if not source_id:
            raise ValueError("source_id 가 비었다")
        self._host = host
        self._port = port
        self._ssl = ssl_context
        self._tokens = tokens
        self._source_id = source_id
        self._clock = clock
        self._on_message = on_message
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._new_msg_id = new_msg_id
        self._lock = threading.Lock()
        self._conn: Any = None
        self._generation = 0
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue)
        self._server: Any = None
        self._threads: list[threading.Thread] = []
        self._closing = threading.Event()
        self.stats = ControlStats()
        self.port: int | None = None

    # --- 상태 -----------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._conn is not None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def alive_threads(self) -> list[str]:
        return [t.name for t in self._threads if t.is_alive()]

    # --- 보내기 ---------------------------------------------------------------------

    def envelope(self, msg_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if msg_type not in DOWNSTREAM_VERSIONS:
            raise ValueError(f"I0 아래로 보내는 메시지가 아니다: {msg_type}")
        return {
            "schema_version": DOWNSTREAM_VERSIONS[msg_type],
            "msg_type": msg_type,
            "msg_id": self._new_msg_id(),
            "sent_at": int(self._clock.now_ms()),
            "source_id": self._source_id,
            "payload": dict(payload),
        }

    def send(self, msg_type: str, payload: Mapping[str, Any]) -> bool:
        """봉투를 씌워 송신 대기열에 넣는다(기다리지 않음). 연결이 없거나 대기열이 차면 False."""
        return self.send_message(self.envelope(msg_type, payload))

    def send_message(self, message: Mapping[str, Any]) -> bool:
        kind = str(message.get("msg_type"))
        with self._lock:
            if self._conn is None:
                self.stats.not_connected[kind] = self.stats.not_connected.get(kind, 0) + 1
                return False
            generation = self._generation
        try:
            text = json.dumps(message, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            log.exception("I0 %s 직렬화 실패(보내지 않았다)", kind)
            return False
        try:
            self._queue.put_nowait((generation, kind, text))
        except queue.Full:
            with self._lock:
                self.stats.queue_full += 1
            log.error("I0 송신 대기열이 찼다(%s 를 넣지 못했다). 다음 health 목록이 노트북을 맞춘다", kind)
            return False
        return True

    # --- 시작·정지 -------------------------------------------------------------------

    def start(self) -> None:
        from websockets.sync.server import serve

        if self._server is not None:
            raise RuntimeError("이미 시작했다")
        self._closing.clear()
        self._server = serve(
            self._handler,
            self._host,
            self._port,
            ssl=self._ssl,
            process_request=self._check,
            compression=None,
            ping_interval=10,
            ping_timeout=10,
            max_size=256 * 1024,
            logger=_ws_log,
        )
        self.port = self._server.socket.getsockname()[1]
        for name, target in (("i0-control-listen", self._server.serve_forever), ("i0-control-writer", self._run_writer)):
            t = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(t)
            t.start()
        log.info("I0 제어 채널 대기: %s:%d (%s)", self._host, self.port, "wss" if self._ssl else "ws loopback")

    def close(self, timeout_s: float = 5.0) -> bool:
        self._closing.set()
        if self._server is not None:
            self._server.shutdown()
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            abort_connection(conn)
        self._queue.put(None)
        for t in self._threads:
            t.join(timeout_s)
        alive = self.alive_threads()
        if alive:
            log.warning("I0 제어 채널 스레드가 끝나지 않았다: %s", alive)
        return not alive

    def drop_connection(self) -> None:
        """지금 연결을 끊는다(시험·운영 도구). 노트북이 다시 붙는다."""
        with self._lock:
            conn = self._conn
        if conn is not None:
            abort_connection(conn)

    # --- 연결 -----------------------------------------------------------------------

    def _check(self, connection: Any, request: Any) -> Any:
        if self._closing.is_set():
            return connection.respond(503, "closing\n")
        if not self._tokens.verify(bearer_token(request.headers)):
            with self._lock:
                self.stats.auth_rejections += 1
            log.warning("I0 제어 채널: 기기 토큰이 맞지 않아 거절했다")
            return connection.respond(401, "unauthorized\n")
        return None

    def _handler(self, conn: Any) -> None:
        with self._lock:
            previous = self._conn
            self._conn = conn
            self._generation += 1
            generation = self._generation
            self.stats.connections += 1
            if previous is not None:
                self.stats.superseded += 1
        if previous is not None:
            log.info("I0 제어 채널: 노트북이 다시 붙어 이전 연결을 끊는다")
            abort_connection(previous)
        log.info("I0 제어 채널 연결 (세대 %d)", generation)
        try:
            if self._on_connected is not None:
                try:
                    self._on_connected(generation)
                except Exception:  # noqa: BLE001 - 훅 실패가 연결을 막지 않는다
                    log.exception("I0 제어 채널 연결 훅 실패")
            for raw in conn:
                if isinstance(raw, bytes):
                    with self._lock:
                        self.stats.malformed += 1
                    continue
                self._dispatch(raw, generation)
        except Exception as e:  # noqa: BLE001 - 끊김·프로토콜 오류
            log.info("I0 제어 채널 연결 끝 (세대 %d): %s", generation, type(e).__name__)
        finally:
            current = False
            with self._lock:
                if self._conn is conn:
                    self._conn = None
                    current = True
            if current and self._on_disconnected is not None:
                try:
                    self._on_disconnected(generation)
                except Exception:  # noqa: BLE001
                    log.exception("I0 제어 채널 끊김 훅 실패")

    def _dispatch(self, raw: str, generation: int) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            with self._lock:
                self.stats.malformed += 1
            return
        if not isinstance(message, dict) or not isinstance(message.get("payload"), dict):
            with self._lock:
                self.stats.malformed += 1
            return
        kind = message.get("msg_type")
        if kind not in UPSTREAM_TYPES:
            with self._lock:
                self.stats.unknown += 1
            return
        with self._lock:
            self.stats.received[kind] = self.stats.received.get(kind, 0) + 1
        try:
            self._on_message(message, generation)
        except Exception:  # noqa: BLE001 - 한 메시지 처리 실패가 수신을 멈추지 않는다
            with self._lock:
                self.stats.handler_errors += 1
            log.exception("I0 제어 채널 %s 처리 실패", kind)

    # --- 송신 스레드 -----------------------------------------------------------------

    def _run_writer(self) -> None:
        while True:
            item = self._queue.get()
            if item is None or self._closing.is_set():
                return
            generation, kind, text = item
            with self._lock:
                conn = self._conn if self._generation == generation else None
                if conn is None:
                    self.stats.stale_dropped += 1
                    continue
            try:
                conn.send(text)
            except Exception as e:  # noqa: BLE001 - 연결 끊김. 연결 스레드가 정리한다
                with self._lock:
                    self.stats.send_errors += 1
                log.info("I0 제어 채널 송신 실패(%s): %s", kind, type(e).__name__)
                continue
            with self._lock:
                self.stats.sent[kind] = self.stats.sent.get(kind, 0) + 1
