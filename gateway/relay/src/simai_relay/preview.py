"""현장 노트북 CCTV 로컬 미리보기: 중계가 이미 받은 압축 프레임을 루프백 WebSocket 으로 관제 앱에 넘긴다 (S15P31A101-310).

명세 3.3 "RTSP 접속 규칙"·16장 결정: 관제 화면 실시간 영상은 중계가 이미 받은 스트림만 쓴다. 이 모듈은 같은 노트북의
로컬 미리보기이고, 노트북 밖 원격 시청은 같은 허브 구조로 EC2 에 올리는 remote.py(S15P31A101-400, 허브를 따로 둔다)다. 그래서
- 카메라 RTSP 세션을 더 열지 않는다. 수신 루프가 Tier 0 로 넘기는 I0 기록(I0CodecInfo, I0Frame)을 그대로 나눠 받는다
  (PreviewSink 가 원래 CameraSink 를 감싼다). 디코딩·재인코딩하지 않는다(H.264 Annex B 그대로).
- 로컬 미리보기는 EC2·다른 기기를 거치지 않는다. 127.0.0.1 에서만 듣는다(PREVIEW_HOST 고정, 바꾸는 인자 없음, 시험으로 고정).
- 형식은 contracts/messages/cctv_preview.schema.json (simai-cctv-preview/1):
  텍스트 = hello(카메라 목록) 한 번 → 카메라별 마지막 codec(I0 codec_info + WebCodecs codec 문자열),
  이진 = 헤더 길이(u32 BE) | I0 frame_header JSON | 접근 단위. 소비자마다 카메라별 다음 키프레임부터 보낸다.

수신 스레드를 막지 않는다:
- 소비자(브라우저 연결)가 없으면 아무것도 하지 않는다. 있으면 이진 메시지를 한 번만 만들어 소비자 큐에 넣고 바로 돌아온다
  (잠금은 소비자마다 짧게, 보내기는 소비자마다 따로 도는 서버 스레드가 한다).
- 소비자마다 큐 상한(프레임 수·바이트). 넘치면 그 소비자의 그 카메라만 다음 키프레임까지 건너뛰고 버린 수를 센다.
  느린 소비자는 다른 소비자·Tier 0 전송에 영향을 주지 않는다.
- 미리보기 쪽 예외는 수신·Tier 0 전송으로 올리지 않는다(로그만).

다른 웹 페이지가 루프백 포트로 영상을 가져가지 못하게, 브라우저가 붙이는 Origin 을 허용 목록으로만 받는다.
RTSP 계정은 입력(RtspSource) 안에만 있고 I0 기록에 없으므로 미리보기 메시지·로그에도 없다.
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from simai_relay.sink import CameraSink, I0CodecInfo, I0Frame, SinkFactory

log = logging.getLogger("simai_relay.preview")

PROTOCOL = "simai-cctv-preview/1"
PREVIEW_HOST = "127.0.0.1"  # 루프백만. 0.0.0.0·다른 주소로 바꾸는 인자를 두지 않는다(명세 3.3: 영상은 노트북 밖으로 나가지 않음)
DEFAULT_PREVIEW_PORT = 47032  # 상태 보고 UDP 47031 옆. CLI 는 --preview-port 를 줄 때만 켠다
# 관제 앱이 열리는 출처. 운영(simai.live), 개발 서버(vite 5173), 빌드 미리보기(vite preview 4173)
DEFAULT_ORIGINS = (
    "https://simai.live",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)
DEFAULT_MAX_CONSUMERS = 4
DEFAULT_MAX_QUEUE_FRAMES = 50  # 소비자 하나, 4대 합쳐 25fps×4 의 0.5초
DEFAULT_MAX_QUEUE_BYTES = 16 * 1024 * 1024  # 2304x1296 키프레임(수백 KiB) 여러 장
GET_POLL_S = 0.5


def codec_string(sps: bytes) -> str:
    """WebCodecs VideoDecoder codec 문자열 avc1.PPCCLL (SPS 의 profile_idc·constraint 플래그·level_idc)."""
    if len(sps) < 4:
        raise ValueError("SPS 가 짧다")
    return f"avc1.{sps[1]:02X}{sps[2]:02X}{sps[3]:02X}"


def hello_message(cameras: Sequence[str]) -> str:
    return json.dumps({"kind": "hello", "protocol": PROTOCOL, "cameras": list(cameras)}, ensure_ascii=False)


def codec_message(codec_info: dict[str, Any]) -> str:
    sps = base64.b64decode(codec_info["sps"])
    return json.dumps({"kind": "codec", "protocol": PROTOCOL, "codec_info": codec_info,
                       "codec_string": codec_string(sps)}, ensure_ascii=False)


def frame_message(record: I0Frame) -> bytes:
    """이진 메시지: 헤더 길이(u32 BE) | I0 frame_header JSON(UTF-8) | 접근 단위(Annex B 그대로)."""
    header = json.dumps(record.header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(header)) + header + record.data


def parse_frame_message(message: bytes) -> tuple[dict[str, Any], bytes]:
    """frame_message 의 역(시험·점검용)."""
    (n,) = struct.unpack(">I", message[:4])
    return json.loads(message[4 : 4 + n].decode("utf-8")), message[4 + n :]


@dataclass
class CameraCounters:
    sent: int = 0  # 소비자 큐에 넣은 프레임(소비자 합)
    dropped_overflow: int = 0  # 소비자 큐가 넘쳐 다음 키프레임까지 건너뛴 프레임(소비자 합)
    waiting_keyframe: int = 0  # 접속 직후·새 epoch·건너뛰기 뒤 키프레임 전이라 넣지 않은 프레임(소비자 합)


class PreviewConsumer:
    """브라우저 연결 하나의 송신 큐. 수신 스레드가 offer_*, 그 연결의 서버 스레드가 get 을 부른다."""

    def __init__(self, cameras: Sequence[str], max_frames: int, max_bytes: int) -> None:
        self._cond = threading.Condition()
        self._items: deque[tuple[str | bytes, int]] = deque()  # (메시지, 큐 상한에 세는 바이트, codec 은 0)
        self._frames = 0
        self._bytes = 0
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._waiting = {c: True for c in cameras}  # 카메라별: 다음 키프레임부터 넣는다
        self.closed = False
        self.max_depth = 0

    def put_text(self, text: str, camera_id: str | None = None) -> None:
        """hello·codec. 상한에 세지 않는다(작고, 빠지면 디코더를 설정하지 못한다). codec 뒤에는 그 카메라를 키프레임부터."""
        with self._cond:
            if camera_id is not None:
                self._waiting[camera_id] = True
            self._items.append((text, 0))
            self._cond.notify()

    def offer_frame(self, camera_id: str, keyframe: bool, message: bytes, counters: CameraCounters) -> None:
        with self._cond:
            if self.closed:
                return
            if self._waiting.get(camera_id, True) and not keyframe:
                counters.waiting_keyframe += 1
                return
            if self._frames >= self._max_frames or self._bytes + len(message) > self._max_bytes:
                self._waiting[camera_id] = True
                counters.dropped_overflow += 1
                return
            self._waiting[camera_id] = False
            self._items.append((message, len(message)))
            self._frames += 1
            self._bytes += len(message)
            self.max_depth = max(self.max_depth, self._frames)
            counters.sent += 1
            self._cond.notify()

    def get(self, timeout: float) -> str | bytes | None:
        with self._cond:
            if not self._items and not self.closed:
                self._cond.wait(timeout)
            if not self._items:
                return None
            message, size = self._items.popleft()
            if size:
                self._frames -= 1
                self._bytes -= size
            return message

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._items.clear()
            self._frames = self._bytes = 0
            self._cond.notify_all()

    @property
    def depth(self) -> int:
        with self._cond:
            return self._frames


@dataclass
class HubStats:
    consumers_total: int = 0
    consumers_rejected: int = 0  # 소비자 상한을 넘어 받지 않은 연결
    errors: int = 0  # 미리보기 쪽 예외(수신·Tier 0 전송으로 올리지 않음)
    max_consumer_depth: int = 0
    counters: dict[str, CameraCounters] = field(default_factory=dict)


class PreviewHub:
    """카메라 여러 대의 마지막 codec 과 소비자 목록. 수신 스레드(카메라마다)와 서버 스레드(소비자마다)가 같이 쓴다."""

    def __init__(self, cameras: Sequence[str], *, max_consumers: int = DEFAULT_MAX_CONSUMERS,
                 max_queue_frames: int = DEFAULT_MAX_QUEUE_FRAMES,
                 max_queue_bytes: int = DEFAULT_MAX_QUEUE_BYTES) -> None:
        if not cameras or max_consumers < 1 or max_queue_frames < 1 or max_queue_bytes < 1:
            raise ValueError("카메라가 있어야 하고 상한은 1 이상이다")
        self.cameras = list(cameras)
        self._max_consumers = max_consumers
        self._max_frames = max_queue_frames
        self._max_bytes = max_queue_bytes
        self._lock = threading.Lock()
        self._codecs: dict[str, str] = {}
        self._consumers: tuple[PreviewConsumer, ...] = ()  # 바꿀 때 통째로 갈아 끼운다(읽기는 잠그지 않음)
        self._closed = False
        self.stats = HubStats(counters={c: CameraCounters() for c in self.cameras})

    # ------------------------------------------------------------ 수신 스레드 쪽

    def publish_codec(self, record: I0CodecInfo) -> None:
        camera_id = record.codec_info["camera_id"]
        text = codec_message(record.codec_info)
        with self._lock:
            self._codecs[camera_id] = text
            consumers = self._consumers
        for c in consumers:
            c.put_text(text, camera_id)

    def publish_frame(self, record: I0Frame) -> None:
        consumers = self._consumers
        if not consumers:
            return
        camera_id = record.header["camera_id"]
        counters = self.stats.counters.setdefault(camera_id, CameraCounters())
        message = frame_message(record)
        keyframe = bool(record.header["keyframe"])
        for c in consumers:
            c.offer_frame(camera_id, keyframe, message, counters)

    # ------------------------------------------------------------ 서버 스레드 쪽

    def register(self) -> PreviewConsumer | None:
        """새 소비자. hello 와 카메라별 마지막 codec 을 먼저 넣는다. 상한이면 None."""
        with self._lock:
            if self._closed:
                return None
            if len(self._consumers) >= self._max_consumers:
                self.stats.consumers_rejected += 1
                return None
            consumer = PreviewConsumer(self.cameras, self._max_frames, self._max_bytes)
            consumer.put_text(hello_message(self.cameras))
            for camera_id in self.cameras:
                if camera_id in self._codecs:
                    consumer.put_text(self._codecs[camera_id], camera_id)
            self._consumers = (*self._consumers, consumer)
            self.stats.consumers_total += 1
        return consumer

    def unregister(self, consumer: PreviewConsumer) -> None:
        consumer.close()
        with self._lock:
            self.stats.max_consumer_depth = max(self.stats.max_consumer_depth, consumer.max_depth)
            self._consumers = tuple(c for c in self._consumers if c is not consumer)

    def close_all(self, final: bool = False) -> None:
        """소비자를 모두 닫는다. final 이면 그 뒤 새 소비자도 받지 않는다(서버 멈춤)."""
        with self._lock:
            self._closed = self._closed or final
            consumers, self._consumers = self._consumers, ()
        for c in consumers:
            c.close()

    # ------------------------------------------------------------ 상태

    @property
    def consumers(self) -> int:
        return len(self._consumers)

    def snapshot(self) -> dict[str, Any]:
        """상태 로그용 묶음 값."""
        consumers = self._consumers
        s = self.stats
        return {"consumers": len(consumers), "consumers_total": s.consumers_total,
                "consumers_rejected": s.consumers_rejected, "errors": s.errors,
                "queue_limit_frames": self._max_frames, "queue_limit_bytes": self._max_bytes,
                "queue_max_depth": max([s.max_consumer_depth, *(c.max_depth for c in consumers)])}

    def camera_snapshot(self, camera_id: str) -> dict[str, Any]:
        c = self.stats.counters.get(camera_id, CameraCounters())
        return {"sent": c.sent, "dropped_overflow": c.dropped_overflow, "waiting_keyframe": c.waiting_keyframe}


class PreviewSink:
    """원래 CameraSink(Tier 0 전송·개수 세기)를 감싸 같은 기록을 미리보기에도 넘긴다. 원래 전송을 먼저 한다."""

    def __init__(self, inner: CameraSink, hub: PreviewHub) -> None:
        self.inner = inner
        self._hub = hub

    def _guard(self, fn: Any, record: Any) -> None:
        try:
            fn(record)
        except Exception as e:  # 미리보기 고장이 수신·Tier 0 전송을 멈추지 않게
            self._hub.stats.errors += 1
            log.warning("미리보기 넘기기 실패 kind=%s", type(e).__name__)

    def send_codec_info(self, record: I0CodecInfo) -> None:
        self.inner.send_codec_info(record)
        self._guard(self._hub.publish_codec, record)

    def send_frame(self, record: I0Frame) -> None:
        self.inner.send_frame(record)
        self._guard(self._hub.publish_frame, record)

    def close(self) -> None:
        self.inner.close()


def preview_factory(inner: SinkFactory, hub: PreviewHub) -> SinkFactory:
    return lambda camera_id: PreviewSink(inner(camera_id), hub)


class PreviewServer:
    """127.0.0.1:<port> WebSocket 서버(websockets 동기 서버, 연결마다 스레드). 보내기만 하고 받은 메시지는 쓰지 않는다."""

    def __init__(self, hub: PreviewHub, port: int, origins: Sequence[str] = DEFAULT_ORIGINS) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("--preview-port 는 0~65535 다")
        if not origins:
            raise ValueError("허용할 출처가 하나는 있어야 한다")
        self.hub = hub
        self._port = port
        self._origins = list(origins)
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from websockets.sync.server import serve

        self._server = serve(
            self._handle, PREVIEW_HOST, self._port,
            origins=self._origins,  # 브라우저가 붙이는 Origin 이 목록에 없으면 403(다른 웹 페이지가 영상을 가져가지 못하게)
            compression=None,  # 압축된 영상을 다시 deflate 하지 않는다(CPU)
            max_size=64 * 1024,  # 받는 쪽 상한(관제 앱은 보내지 않는다)
            logger=logging.getLogger("simai_relay.preview.websockets"),
        )
        self._thread = threading.Thread(target=self._server.serve_forever, name="relay-preview", daemon=True)
        self._thread.start()
        log.info("미리보기 시작 ws://%s:%d (출처 %s)", PREVIEW_HOST, self.port, ", ".join(self._origins))

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.socket.getsockname()[:2]
        return host, port

    @property
    def port(self) -> int:
        return self.address[1]

    def stop(self, timeout: float = 3.0) -> None:
        """새 연결을 막고 소비자를 닫는다. 읽지 않는 브라우저에 보내다 막힌 연결이 있어도 timeout 안에 돌아온다."""
        self.hub.close_all(final=True)  # 서버 스레드가 다음 get 에서 끝난다(shutdown 은 그 스레드들을 기다린다)
        if self._server is not None:
            closer = threading.Thread(target=self._server.shutdown, name="relay-preview-stop", daemon=True)
            closer.start()
            closer.join(timeout)
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _handle(self, ws: Any) -> None:
        from websockets.exceptions import ConnectionClosed

        from websockets.protocol import State

        consumer = self.hub.register()
        if consumer is None:
            log.warning("미리보기 소비자를 받지 않았다(상한 또는 멈추는 중)")
            ws.close(1013, "too many viewers")
            return
        log.info("미리보기 접속 (소비자 %d)", self.hub.consumers)
        try:
            while True:
                message = consumer.get(GET_POLL_S)
                if message is None:
                    if consumer.closed or ws.state is not State.OPEN:  # 보낼 것이 없어도 브라우저가 떠났으면 끝낸다
                        break
                    continue
                ws.send(message)
        except ConnectionClosed:
            pass
        finally:
            self.hub.unregister(consumer)
            log.info("미리보기 끊김 (소비자 %d)", self.hub.consumers)
