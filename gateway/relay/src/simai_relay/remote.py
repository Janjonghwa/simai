"""CCTV 원격 시청 업로드: 중계가 이미 받은 압축 프레임을 보는 사람이 있을 때만 EC2 로 올린다 (S15P31A101-400).

경로: 카메라 ─RTSP 1세션─ 중계 ─┬─ 127.0.0.1 로컬 미리보기(preview.PreviewServer, 그대로)
                                 └─ 이 모듈: EC2 wss://<EC2>/ws/devices/cctv ─ EC2 가 저장 없이 관리자 브라우저에 나눠 줌

- 카메라 RTSP 세션을 더 열지 않고 디코딩·재인코딩하지 않는다. 로컬 미리보기와 같은 PreviewHub 구조를 쓰되
  허브를 따로 둔다(대기열·소비자 상한이 로컬 미리보기·Tier 0 전송과 따로).
- 늘 붙어 있다. 끊기면 1 s → 최대 30 s 지수 백오프로 다시 붙는다. 인증은 기기 토큰(Authorization: Bearer, 헤더에만).
- EC2 가 텍스트 cctv_remote_control {"kind":"demand","active":…} 을 접속 직후·바뀔 때 보낸다.
  active 면 허브에 소비자 하나를 등록한다(hello → 카메라별 마지막 codec → 카메라별 다음 키프레임부터 이진 프레임,
  형식은 cctv_preview 와 같다). inactive 면 소비자를 빼고(프레임을 멈춤) 연결은 둔다.
- 소비자가 없으면 수신 스레드는 허브에서 바로 돌아온다(보는 사람이 없을 때 비용 없음). 대기열이 넘치면
  그 카메라만 다음 키프레임까지 건너뛴다(PreviewConsumer). Tier 0 전송을 기다리게 하지 않는다(PreviewSink 가 원래 전송을 먼저 한다).
- ping 15 s, pong 없이 30 s 면 끊는다(websockets 가 한다). EC2 의 ping 에는 websockets 가 답한다.

비밀: 토큰은 헤더에만 싣고 주소·로그·repr·예외 문장에 넣지 않는다. 주소에 계정·query·fragment·토큰이 있으면 만들지 않는다.
"""

from __future__ import annotations

import contextlib
import json
import logging
import ssl
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from simai_relay.preview import DEFAULT_MAX_QUEUE_BYTES as PREVIEW_MAX_QUEUE_BYTES
from simai_relay.preview import DEFAULT_MAX_QUEUE_FRAMES as PREVIEW_MAX_QUEUE_FRAMES
from simai_relay.preview import PreviewConsumer, PreviewHub

log = logging.getLogger("simai_relay.remote")

REMOTE_PATH = "/ws/devices/cctv"
CONTROL_KIND = "demand"
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 30.0
PING_INTERVAL_S = 15.0
PING_TIMEOUT_S = 30.0
OPEN_TIMEOUT_S = 10.0
RECV_POLL_S = 0.5
CONTROL_MAX_BYTES = 64 * 1024  # 받는 쪽 상한(EC2 는 작은 텍스트만 보낸다)
# 원격 업로드 대기열 상한은 로컬 미리보기와 같은 기준(50프레임·16 MiB, 설계 cctv-remote-view.md), 대기열 자체는 따로 둔다
DEFAULT_MAX_QUEUE_FRAMES = PREVIEW_MAX_QUEUE_FRAMES
DEFAULT_MAX_QUEUE_BYTES = PREVIEW_MAX_QUEUE_BYTES
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def remote_url(base_url: str, token: str) -> str:
    """SIMAI_EC2_BASE_URL(https) → wss://<호스트>/ws/devices/cctv. http 는 루프백 시험 서버만(ws)."""
    parts = urllib.parse.urlsplit(base_url.strip())
    if not parts.hostname or parts.scheme not in ("https", "http"):
        raise ValueError("SIMAI_EC2_BASE_URL 이 올바른 https 주소가 아니다")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise ValueError("원격 CCTV 는 https(wss) 만 쓴다: 루프백 시험 서버가 아니면 https 주소여야 한다")
    if parts.username or parts.password:
        raise ValueError("SIMAI_EC2_BASE_URL 에 계정을 넣지 않는다 (기기 토큰은 SIMAI_DEVICE_TOKEN)")
    if parts.query or parts.fragment or "?" in base_url or "#" in base_url:
        raise ValueError("SIMAI_EC2_BASE_URL 에 query·fragment 를 넣지 않는다")
    decoded = {base_url, urllib.parse.unquote(base_url), urllib.parse.unquote_plus(base_url)}
    forms = {f for f in (token, urllib.parse.quote(token, safe=""), urllib.parse.quote_plus(token)) if f}
    if any(f in text for f in forms for text in decoded):
        raise ValueError("SIMAI_EC2_BASE_URL 에 기기 토큰이 들어 있다 (토큰은 Authorization 헤더로만)")
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + REMOTE_PATH, "", ""))


def parse_control(text: str) -> bool | None:
    """cctv_remote_control 의 active. 모르는 메시지면 None(그 메시지만 버린다)."""
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(doc, dict) or doc.get("kind") != CONTROL_KIND or not isinstance(doc.get("active"), bool):
        return None
    return doc["active"]


@dataclass
class RemoteStats:
    connected: bool = False
    demand: bool = False
    connects: int = 0  # 붙은 횟수
    failures: int = 0  # 붙지 못하거나 끊긴 횟수
    demand_changes: int = 0
    sent_messages: int = 0
    sent_bytes: int = 0
    last_error: str | None = None  # 예외 종류만(문장에 주소·토큰이 섞이지 않게)


class RemoteUploader:
    """EC2 CCTV 수신 WebSocket 클라이언트. start() 하면 스레드 하나가 접속·재접속·수요 처리를 맡는다."""

    def __init__(self, hub: PreviewHub, base_url: str, device_token: str, *,
                 ssl_context: ssl.SSLContext | None = None,
                 sleep: Callable[[float], bool] | None = None) -> None:
        if not device_token or not device_token.strip():
            raise ValueError("기기 토큰이 없다(.env 의 SIMAI_DEVICE_TOKEN)")
        self.url = remote_url(base_url, device_token)
        self.hub = hub
        self._token = device_token.strip()
        self._ssl = None
        if self.url.startswith("wss://"):
            context = ssl_context if ssl_context is not None else ssl.create_default_context()
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise ValueError("인증서 검증(체인·호스트 이름)을 끈 문맥으로는 붙지 않는다")
            self._ssl = context
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait  # 시험이 바꾼다. True 면 멈춤
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._lock = threading.Lock()
        self.stats = RemoteStats()

    def __repr__(self) -> str:  # 토큰을 찍지 않는다
        return f"RemoteUploader(url={self.url!r}, device_token=***)"

    # ------------------------------------------------------------ 수명

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="relay-remote-cctv", daemon=True)
        self._thread.start()
        log.info("원격 CCTV 업로드 대기 시작 %s (보는 사람이 있을 때만 올린다)", self.url)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self.hub.close_all(final=True)
        with self._lock:
            ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):  # 멈추는 중
                ws.close()
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------ 접속 루프

    def _run(self) -> None:
        backoff = BACKOFF_START_S
        while not self._stop.is_set():
            opened = False
            try:
                opened = self._session()
            except Exception as e:  # noqa: BLE001 - 어떤 실패든 다시 붙는다(문장은 남기지 않는다)
                self.stats.failures += 1
                self.stats.last_error = type(e).__name__
                log.warning("원격 CCTV 연결 실패·끊김 (%s), %.0f초 뒤 다시", type(e).__name__, backoff)
            finally:
                self.stats.connected = False
                self._set_demand_off()
            if self._stop.is_set():
                break
            if opened:
                backoff = BACKOFF_START_S
            if self._sleep(backoff):
                break
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _session(self) -> bool:
        """한 번 붙어 끊길 때까지. 붙었으면 True(다음 백오프를 처음부터)."""
        from websockets.sync.client import connect

        with connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self._token}"},
            ssl=self._ssl,
            open_timeout=OPEN_TIMEOUT_S,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            compression=None,  # 압축된 영상을 다시 deflate 하지 않는다(CPU)
            max_size=CONTROL_MAX_BYTES,
            logger=logging.getLogger("simai_relay.remote.websockets"),
        ) as ws:
            with self._lock:
                self._ws = ws
            self.stats.connected = True
            self.stats.connects += 1
            log.info("원격 CCTV 연결됨 (%d번째)", self.stats.connects)
            try:
                self._serve(ws)
            finally:
                with self._lock:
                    self._ws = None
        return True

    def _serve(self, ws: Any) -> None:
        """수요를 받아 소비자를 붙이고 뗀다. 보내기는 따로 도는 스레드가 한다(받기와 함께 돌 수 있다)."""
        from websockets.exceptions import ConnectionClosed

        consumer: PreviewConsumer | None = None
        sender: threading.Thread | None = None
        try:
            while not self._stop.is_set():
                try:
                    message = ws.recv(timeout=RECV_POLL_S)
                except TimeoutError:
                    if sender is not None and not sender.is_alive():  # 보내다 끊김
                        raise ConnectionError("송신 실패") from None
                    continue
                except ConnectionClosed:
                    return
                if not isinstance(message, str):
                    continue
                active = parse_control(message)
                if active is None:
                    log.debug("모르는 원격 제어 메시지를 버렸다")
                    continue
                if active and consumer is None:
                    consumer = self.hub.register()
                    if consumer is None:  # 멈추는 중
                        return
                    sender = threading.Thread(target=self._send_loop, args=(ws, consumer),
                                              name="relay-remote-cctv-send", daemon=True)
                    sender.start()
                elif not active and consumer is not None:
                    self.hub.unregister(consumer)
                    consumer = None
                    if sender is not None:
                        sender.join(2.0)
                    sender = None
                if active != self.stats.demand:
                    self.stats.demand = active
                    self.stats.demand_changes += 1
                    log.info("원격 CCTV 수요 %s", "있음: 올린다" if active else "없음: 멈춤")
        finally:
            if consumer is not None:
                self.hub.unregister(consumer)
            if sender is not None:
                sender.join(2.0)

    def _send_loop(self, ws: Any, consumer: PreviewConsumer) -> None:
        try:
            while True:
                message = consumer.get(RECV_POLL_S)
                if message is None:
                    if consumer.closed:
                        return
                    continue
                ws.send(message)
                self.stats.sent_messages += 1
                self.stats.sent_bytes += len(message)
        except Exception as e:  # noqa: BLE001 - 끊김은 수신 루프가 다시 붙는다
            self.stats.last_error = type(e).__name__
            with contextlib.suppress(Exception):
                ws.close()

    def _set_demand_off(self) -> None:
        if self.stats.demand:
            self.stats.demand = False
            self.stats.demand_changes += 1

    # ------------------------------------------------------------ 상태

    def snapshot(self) -> dict[str, Any]:
        s = self.stats
        return {"connected": s.connected, "demand": s.demand, "connects": s.connects, "failures": s.failures,
                "demand_changes": s.demand_changes, "sent_messages": s.sent_messages, "sent_bytes": s.sent_bytes,
                "last_error": s.last_error, "hub": self.hub.snapshot()}


def make_remote_hub(cameras: list[str]) -> PreviewHub:
    """원격 업로드 전용 허브(소비자 하나, 로컬 미리보기와 따로)."""
    return PreviewHub(cameras, max_consumers=1, max_queue_frames=DEFAULT_MAX_QUEUE_FRAMES,
                      max_queue_bytes=DEFAULT_MAX_QUEUE_BYTES)

