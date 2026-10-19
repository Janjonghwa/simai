"""카메라별 TLS 연결로 Tier 0 에 영상을 보내는 CameraSink (명세 12.7 전송·송신 큐·재접속, 14 TLS·기기 토큰·공개키 고정).

카메라마다 TlsCameraSink 하나가 TLS over TCP 연결 하나와 송신 스레드 하나, 송신 큐 하나를 가진다. 카메라 사이에 잠금·큐·
소켓을 공유하지 않으므로 한 카메라의 연결이 느리거나 끊겨도 다른 카메라는 그대로 보낸다.

- 접속 대상: EC2 조회(`simai_site_alert.endpoint.Ec2EndpointLookup`, 계약 tier0_endpoint)로 받은 공개 IP 와 그 카메라의
  VIDEO 포트. `EndpointWatcher` 가 주기적으로 다시 조회하고, 대상(주소·포트·지문)이 바뀌면 송신 스레드가 끊고 새로 붙는다.
- 서버 확인: 공개키 고정은 현장 경보 단말과 같은 코드(`simai_site_alert.pinning`)를 쓴다. 조회 값의 지문이 노트북 고정
  목록에 없으면 접속하지 않고, TLS 핸드셰이크 직후 서버 인증서의 공개키 지문이 그 값과 다르면 아무것도 보내지 않고 끊는다.
  기기 토큰(HELLO)은 그 검사를 통과한 뒤에만 보낸다. 인증서 검증을 끄고 아무 서버에나 붙는 경로는 없다.
- 레코드: `wire` 의 길이 접두 형식. 연결마다 HELLO → HELLO_ACK → 현재 epoch 의 codec_info → 키프레임부터.
- 송신 큐(`SendQueue`): 프레임 수 상한. 넘치면 다음 키프레임까지 버리고 노트북 skipped 로 센다(명세 12.7, 부록 D-G0-11).
  버린 범위는 그 범위를 끝내는 키프레임 앞에 SKIP_NOTICE 로 알려, Tier 0 가 전송 손실(gateway_to_runpod_loss)과 섞지
  않게 한다. 연결이 없을 때 온 프레임과 끊길 때 큐에 남은 프레임은 dropped_link 로 따로 센다(Tier 0 에서는 손실로 보인다).
- 소켓 송신 버퍼는 작게(기본 64 KiB) 잡아 오래된 프레임이 커널 버퍼에 쌓이지 않게 한다. 한 레코드를 send_timeout_s
  안에 보내지 못하면 연결이 죽은 것으로 보고 다시 붙는다.
- 비밀 값: 기기 토큰은 HELLO 레코드에만 싣는다. 로그·예외 문장·repr·상태에는 넣지 않는다.

send_codec_info·send_frame 은 수신 스레드에서 불리고 기다리지 않는다(큐에 넣거나 버린다).
"""

from __future__ import annotations

import logging
import select
import socket
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from simai_site_alert.endpoint import EndpointUnavailable, Tier0Endpoint
from simai_site_alert.pinning import PinMismatchError, PinnedPeerVerifier, pinned_client_context
from simai_site_alert.winsock import retry_wsaefault

from simai_relay import wire
from simai_relay.sink import I0CodecInfo, I0Frame

log = logging.getLogger("simai_relay.tls")

DEFAULT_RECONNECT_DELAYS_S = (0.5, 1.0, 2.0, 5.0)
DEFAULT_MAX_QUEUE_FRAMES = 25  # 25fps 에서 1초
DEFAULT_SNDBUF_BYTES = 64 * 1024
POLL_S = 0.2  # 송신 시간 제한 조각: 이만큼마다 멈춤 요청을 본다
MAX_SKIP_SPANS = 1024  # 지금 epoch 에서 기억하는 서로 떨어진 버린 구간 수(넘으면 오래된 구간부터 잘라 내고 표시)
# Tier 0 가 HELLO 를 거절할 때 쓰는 이유. 그 밖의 문장은 로그에 그대로 쓰지 않는다(받은 값)
KNOWN_REJECT_REASONS = frozenset({"unauthorized", "unknown_camera", "bad_protocol", "bad_hello", "superseded"})


def add_span(spans: list[list[int]], first: int, last: int) -> None:
    """정렬된 서로 떨어진 구간 목록에 [first, last] 를 더한다(겹치거나 맞닿은 구간은 합친다). 제자리에서 바꾼다."""
    merged: list[list[int]] = []
    placed = False
    for a, b in spans:
        if b < first - 1:
            merged.append([a, b])
        elif a > last + 1:
            if not placed:
                merged.append([first, last])
                placed = True
            merged.append([a, b])
        else:
            first, last = min(a, first), max(b, last)
    if not placed:
        merged.append([first, last])
    spans[:] = merged


@dataclass(frozen=True)
class VideoTarget:
    """카메라 한 대의 영상 접속 대상: Tier 0 공개 IP, 그 카메라의 VIDEO 공개 포트, 기대하는 서버 공개키 지문."""

    host: str
    port: int
    spki_sha256: str


TargetSource = Callable[[], VideoTarget | None]


class StreamRejected(Exception):
    """Tier 0 가 그 stream_epoch 를 받지 않는다고 알렸다(STREAM_REJECT). 더 큰 epoch 가 올 때까지 다시 붙지 않는다."""

    def __init__(self, reason: str, stream_epoch: int | None, current_epoch: int | None) -> None:
        self.reason = reason if reason in {"stale_epoch"} else "other"
        self.stream_epoch = stream_epoch
        self.current_epoch = current_epoch
        super().__init__(f"Tier 0 가 stream_epoch {stream_epoch} 를 거절했다 ({self.reason}, Tier 0 기준 {current_epoch})")


class HelloRejected(Exception):
    """Tier 0 가 HELLO 를 거절했다(기기 토큰·camera_id). reason 은 알려진 값이거나 'other'."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in KNOWN_REJECT_REASONS else "other"
        super().__init__(f"Tier 0 가 영상 연결을 거절했다 ({self.reason})")


# ---------------------------------------------------------------------------- 엔드포인트


class EndpointWatcher:
    """EC2 엔드포인트 조회를 주기적으로 돌려 카메라별 VideoTarget 을 알려 준다.

    고정 목록에 없는 지문을 알려 주는 조회 결과는 쓰지 않는다(그 전 대상을 유지하고, 처음부터 없으면 대상 없음).
    조회 실패도 그 전 대상을 유지한다. 대상 읽기(target_for)는 잠그지 않는다(조회 중에도 카메라 송신을 막지 않음).
    """

    def __init__(self, fetch: Callable[[], Tier0Endpoint], pins: frozenset[str], *, refresh_s: float = 30.0,
                 retry_s: float = 2.0) -> None:
        if not pins:
            raise ValueError("고정 지문이 없다(.env 의 SIMAI_TIER0_PINNED_SPKI_SHA256). 고정값 없이는 접속하지 않는다")
        self._fetch = fetch
        self.pins = pins
        self._refresh_s = refresh_s
        self._retry_s = retry_s
        self._endpoint: Tier0Endpoint | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> Tier0Endpoint | None:
        return self._endpoint

    def refresh(self) -> bool:
        """한 번 조회한다. 쓸 수 있는 엔드포인트를 받았으면 True."""
        try:
            endpoint = self._fetch()
        except EndpointUnavailable as e:  # 우리 코드가 만든 문장(토큰 없음)
            log.warning("Tier 0 엔드포인트 조회 실패: %s", e)
            return False
        except Exception as e:  # noqa: BLE001 - 조회 실패는 다시 조회한다. 문장에 무엇이 섞였을지 몰라 종류만 쓴다
            log.warning("Tier 0 엔드포인트 조회 실패 (%s)", type(e).__name__)
            return False
        if endpoint.tls_public_key_sha256 not in self.pins:
            log.warning("EC2 가 알려 준 Tier 0 공개키 지문이 노트북 고정값과 다르다. 그 엔드포인트로는 붙지 않는다")
            return False
        if endpoint != self._endpoint:
            log.info("Tier 0 엔드포인트 %s (영상 카메라 %d대)", endpoint.public_ip, len(endpoint.video_ports))
        self._endpoint = endpoint
        return True

    def target_for(self, camera_id: str) -> VideoTarget | None:
        endpoint = self._endpoint
        if endpoint is None or camera_id not in endpoint.video_ports:
            return None
        return VideoTarget(endpoint.public_ip, endpoint.video_ports[camera_id], endpoint.tls_public_key_sha256)

    def source(self, camera_id: str) -> TargetSource:
        return lambda: self.target_for(camera_id)

    def start(self) -> None:
        def run() -> None:
            while not self._stop.is_set():
                ok = self.refresh()
                self._stop.wait(self._refresh_s if ok or self._endpoint is not None else self._retry_s)

        self._thread = threading.Thread(target=run, name="tier0-endpoint", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


# ---------------------------------------------------------------------------- 송신 큐


@dataclass
class QueueStats:
    enqueued_frames: int = 0
    skipped: int = 0  # 큐가 넘쳐 다음 키프레임까지 버린 프레임 (노트북 skipped, 명세 12.7)
    skip_runs: int = 0
    dropped_link: int = 0  # 연결이 없거나 재동기화(키프레임 대기) 중이라 버린 프레임, 끊길 때 큐에 남은 프레임
    max_depth: int = 0
    spans_truncated: int = 0  # 구간 상한을 넘어 잘라 낸 버린 구간 수


class SendQueue:
    """카메라 한 대의 송신 큐와 버림 규칙. 수신 스레드(put_*)와 송신 스레드(link_*, get)가 같이 쓴다.

    - 연결이 없으면 프레임을 넣지 않는다(dropped_link). 최신 codec_info 만 기억해 다음 연결 첫 레코드로 보낸다.
    - (재)연결 직후와 새 codec_info 뒤에는 키프레임부터 넣는다(그 전 프레임은 dropped_link).
    - 프레임 수가 max_frames 에 이르면 그 프레임부터 다음 키프레임까지 버린다(skipped). 키프레임이 왔는데 여전히 가득이면
      그 키프레임도 버리고 다음 키프레임을 기다린다. 버린 범위는 끝내는 키프레임 앞에 SKIP_NOTICE 로 넣는다.
    - codec_info 와 SKIP_NOTICE 는 상한에 세지 않는다(작고, 빠지면 Tier 0 가 스트림을 잘못 읽는다).
    - SKIP_NOTICE 재전송: 소켓에 넘긴 것이 Tier 0 에 닿았는지는 알 수 없으므로(확인 응답 없음), 지금 epoch 에서 버린 범위를
      겹치거나 맞닿은 것끼리 합친 정렬된 구간 집합으로 들고(버리던 중에 끊긴 범위 포함), 새 연결마다 codec_info 바로 뒤에
      구간마다 하나씩 다시 보낸다. 크기는 서로 떨어진 구간 수뿐이다. 그 수가 max_spans 를 넘으면 가장 오래된 구간부터 잘라
      내고, 다시 보내는 첫 알림에 unknown_before(그 앞은 노트북 기록이 없다)를 실어 Tier 0 가 그 앞 빈칸을 손실로 지어내지
      않게 한다(구분 불가로 센다). Tier 0 는 이미 지난 범위는 무시하고 겹침은 합친다. 새 epoch 의 codec_info 가 오면 비운다.
    """

    def __init__(self, max_frames: int = DEFAULT_MAX_QUEUE_FRAMES, max_spans: int = MAX_SKIP_SPANS) -> None:
        if max_frames < 1 or max_spans < 1:
            raise ValueError("송신 큐 상한과 구간 상한은 1 이상이다")
        self.max_frames = max_frames
        self.max_spans = max_spans
        self._cond = threading.Condition()
        self._items: deque[tuple[str, Any]] = deque()
        self._frames = 0
        self._link_up = False
        self._need_keyframe = True
        self._skipping: list[Any] | None = None  # [camera_id, epoch, first_seq, last_seq]
        self._codec: I0CodecInfo | None = None
        self._spans: list[list[int]] = []  # 지금 epoch 에서 버린 frame_sequence 구간(정렬, 서로 떨어짐)
        self._unknown_before: int | None = None  # 이 번호 앞의 버린 구간은 잘라 내서 모른다
        self._closed = False
        self.stats = QueueStats()

    @property
    def depth(self) -> int:
        with self._cond:
            return self._frames

    def put_codec(self, record: I0CodecInfo) -> None:
        with self._cond:
            self._codec = record
            self._skipping = None  # 이전 epoch 의 버린 범위는 알리지 않는다(Tier 0 는 새 epoch 에서 번호를 새로 센다)
            self._spans = []
            self._unknown_before = None
            self._need_keyframe = True
            if self._link_up:
                self._items.append(("codec", record))
                self._cond.notify()

    def put_frame(self, record: I0Frame) -> None:
        h = record.header
        keyframe = bool(h["keyframe"])
        with self._cond:
            if not self._link_up:
                self.stats.dropped_link += 1
                return
            if self._need_keyframe:
                if not keyframe:
                    self.stats.dropped_link += 1
                    return
                self._need_keyframe = False
            if self._skipping is not None:
                if not keyframe or self._frames >= self.max_frames:
                    self._skipping[3] = h["frame_sequence"]
                    self.stats.skipped += 1
                    return
                notice = self._notice(self._skipping)
                self._remember(notice)
                self._items.append(("skip", notice))
                self._skipping = None
            elif self._frames >= self.max_frames:
                self._skipping = [h["camera_id"], h["stream_epoch"], h["frame_sequence"], h["frame_sequence"]]
                self.stats.skipped += 1
                self.stats.skip_runs += 1
                return
            self._items.append(("frame", record))
            self._frames += 1
            self.stats.enqueued_frames += 1
            self.stats.max_depth = max(self.stats.max_depth, self._frames)
            self._cond.notify()

    @staticmethod
    def _notice(skipping: list[Any]) -> dict[str, Any]:
        cam, epoch, first, last = skipping
        return {"camera_id": cam, "stream_epoch": epoch, "first_sequence": first, "last_sequence": last}

    def _remember(self, notice: dict[str, Any]) -> None:
        add_span(self._spans, notice["first_sequence"], notice["last_sequence"])
        while len(self._spans) > self.max_spans:
            _, last = self._spans.pop(0)
            self._unknown_before = max(self._unknown_before or 0, last + 1)
            self.stats.spans_truncated += 1

    def resend_notices(self) -> list[dict[str, Any]]:
        """새 연결에서 다시 보낼 SKIP_NOTICE(지금 epoch 의 구간마다 하나). 잘라 낸 구간이 있으면 첫 알림에 unknown_before."""
        with self._cond:
            if self._codec is None or not self._spans:
                return []
            cam = self._codec.codec_info["camera_id"]
            epoch = self._codec.codec_info["stream_epoch"]
            out = [{"camera_id": cam, "stream_epoch": epoch, "first_sequence": a, "last_sequence": b}
                   for a, b in self._spans]
            if self._unknown_before is not None:
                out[0]["unknown_before"] = self._unknown_before
            return out

    @property
    def spans(self) -> list[tuple[int, int]]:
        with self._cond:
            return [(a, b) for a, b in self._spans]

    def link_up(self) -> tuple[I0CodecInfo | None, list[dict[str, Any]]]:
        """연결됨. 새 연결의 첫 레코드로 보낼 최신 codec_info(없으면 None)와 그 뒤에 다시 보낼 미정산 SKIP_NOTICE."""
        with self._cond:
            self._items.clear()
            self._frames = 0
            self._skipping = None
            self._need_keyframe = True
            self._link_up = True
            codec = self._codec
        return codec, self.resend_notices()

    def link_down(self) -> None:
        with self._cond:
            self._link_up = False
            self.stats.dropped_link += self._frames
            self._items.clear()
            self._frames = 0
            if self._skipping is not None:  # 버리던 범위는 끝내는 키프레임 없이 끊겼다: 다음 연결에서 알린다
                self._remember(self._notice(self._skipping))
                self._skipping = None

    @property
    def codec_epoch(self) -> int | None:
        with self._cond:
            return self._codec.codec_info["stream_epoch"] if self._codec is not None else None

    def get(self, timeout: float) -> tuple[str, Any] | None:
        with self._cond:
            if not self._items and not self._closed:
                self._cond.wait(timeout)
            if not self._items:
                return None
            item = self._items.popleft()
            if item[0] == "frame":
                self._frames -= 1
            return item

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


# ---------------------------------------------------------------------------- 카메라별 TLS 송신


class TlsCameraSink:
    """카메라 한 대의 Tier 0 영상 연결 (CameraSink 구현). 만들면 송신 스레드가 바로 돈다."""

    def __init__(
        self,
        camera_id: str,
        target: TargetSource,
        *,
        device_token: str,
        pins: frozenset[str],
        laptop_id: str = "laptop-01",
        ca_file: str | None = None,
        max_queue_frames: int = DEFAULT_MAX_QUEUE_FRAMES,
        sndbuf_bytes: int = DEFAULT_SNDBUF_BYTES,
        connect_timeout_s: float = 5.0,
        send_timeout_s: float = 5.0,
        reconnect_delays_s: Sequence[float] = DEFAULT_RECONNECT_DELAYS_S,
        stable_after_s: float = 10.0,
    ) -> None:
        if not device_token:
            raise ValueError("기기 토큰이 없다(.env 의 SIMAI_DEVICE_TOKEN)")
        if not pins:
            raise ValueError("고정 지문이 없다(.env 의 SIMAI_TIER0_PINNED_SPKI_SHA256). 고정값 없이는 접속하지 않는다")
        self.camera_id = camera_id
        self._target = target
        self._token = device_token
        self._pins = pins
        self._laptop_id = laptop_id
        # 공개키 고정 문맥: 이 문맥으로는 PinnedPeerVerifier 검사 없이 레코드를 보내지 않는다(_open)
        self._ctx = pinned_client_context(ca_file)
        self._sndbuf = sndbuf_bytes
        self._connect_timeout = connect_timeout_s
        self._send_timeout = send_timeout_s
        self._delays = tuple(reconnect_delays_s) or (1.0,)
        self._stable_after_s = stable_after_s
        self.queue = SendQueue(max_queue_frames)
        self._closing = threading.Event()
        self._lock = threading.Lock()  # 상태 숫자만 (이 카메라 안에서)
        self.connected = False
        self.current_target: VideoTarget | None = None
        self.rejection: dict[str, Any] | None = None  # 마지막 STREAM_REJECT (reason, stream_epoch, current_epoch)
        self._counts = {"connects": 0, "connect_failures": 0, "pin_rejections": 0, "auth_rejections": 0,
                        "disconnects": 0, "sent_frames": 0, "sent_bytes": 0, "sent_codec_infos": 0,
                        "sent_skip_notices": 0, "stream_rejections": 0, "held_by_rejection": 0}
        self._thread = threading.Thread(target=self._run, name=f"tls-{camera_id}", daemon=True)
        self._thread.start()

    def __repr__(self) -> str:  # 토큰을 찍지 않는다
        return f"TlsCameraSink(camera_id={self.camera_id!r}, device_token=***, connected={self.connected})"

    # ------------------------------------------------------------------ CameraSink

    def send_codec_info(self, record: I0CodecInfo) -> None:
        self.queue.put_codec(record)

    def send_frame(self, record: I0Frame) -> None:
        self.queue.put_frame(record)

    def close(self) -> None:
        """송신 스레드를 멈춘다. 송신은 POLL_S 마다 멈춤 요청을 보므로 곧 끝난다(소켓은 송신 스레드가 닫는다)."""
        self._closing.set()
        self.queue.close()
        self._thread.join(timeout=self._connect_timeout + 2)

    # ------------------------------------------------------------------ 상태

    def snapshot(self) -> dict[str, Any]:
        q = self.queue.stats
        with self._lock:
            counts = dict(self._counts)
        return {"camera_id": self.camera_id, "tier0_video": "CONNECTED" if self.connected else "DISCONNECTED",
                **counts, "queue_depth": self.queue.depth, "queue_max_depth": q.max_depth,
                "queue_limit_frames": self.queue.max_frames, "skipped": q.skipped, "skip_runs": q.skip_runs,
                "dropped_link": q.dropped_link, "skip_spans": len(self.queue.spans),
                "skip_spans_truncated": q.spans_truncated, "tier0_rejection": self.rejection}

    def _count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] += n

    # ------------------------------------------------------------------ 송신 스레드

    def _wait(self, seconds: float) -> None:
        self._closing.wait(seconds)

    def _run(self) -> None:
        failures = 0
        while not self._closing.is_set():
            target = self._target()
            if target is None:
                self._wait(self._delays[0])
                continue
            rejected = self.rejection
            if rejected is not None:
                epoch = self.queue.codec_epoch
                if epoch is None or rejected["stream_epoch"] is None or epoch <= rejected["stream_epoch"]:
                    self._count("held_by_rejection")  # 거절된 epoch 로는 다시 붙지 않는다(새 스트림을 기다린다)
                    self._wait(self._delays[0])
                    continue
                self.rejection = None
            try:
                tls = self._open(target)
            except PinMismatchError as e:
                self._count("pin_rejections")
                log.warning("%s Tier 0 서버 확인 실패, 접속하지 않는다: %s", self.camera_id, e)
            except HelloRejected as e:
                self._count("auth_rejections")
                log.warning("%s %s", self.camera_id, e)
            except (OSError, ssl.SSLError, wire.WireError) as e:
                self._count("connect_failures")
                log.warning("%s Tier 0 영상 연결 실패 (%s)", self.camera_id, type(e).__name__)
            else:
                started = time.monotonic()
                self._serve(tls, target)
                if time.monotonic() - started >= self._stable_after_s:
                    failures = 0
            if self._closing.is_set():
                break
            if self._target() != target and target is not None:
                failures = 0  # 대상이 바뀌었으면 바로 새 대상으로
                continue
            self._wait(self._delays[min(failures, len(self._delays) - 1)])
            failures += 1

    def _open(self, target: VideoTarget) -> ssl.SSLSocket:
        # 지문이 고정 목록에 없으면 여기서 PinMismatchError (TCP 연결도 열지 않는다)
        verifier = PinnedPeerVerifier(target.spki_sha256, self._pins)

        def connect() -> socket.socket:  # 다시 시도할 때마다 새 소켓(실패한 소켓의 connect 를 되풀이하지 않는다)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self._sndbuf)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(self._connect_timeout)
                sock.connect((target.host, target.port))
            except BaseException:
                sock.close()
                raise
            return sock

        raw = retry_wsaefault(connect, what=f"{self.camera_id} Tier 0 영상 연결")
        tls: ssl.SSLSocket | None = None
        try:
            tls = self._ctx.wrap_socket(raw, server_hostname=None)
            verifier(tls)  # 핸드셰이크 직후, 아무것도 보내기 전에
            hello = {"protocol": wire.PROTOCOL, "camera_id": self.camera_id, "laptop_id": self._laptop_id,
                     "device_token": self._token}
            tls.sendall(wire.encode(wire.HELLO, hello))
            reply = wire.read_record(tls, max_meta=wire.MAX_HELLO_META, max_data=0)
            if reply is None or reply[0] != wire.HELLO_ACK:
                raise wire.WireError("HELLO_ACK 가 오지 않았다")
            if reply[1].get("ok") is not True:
                reason = reply[1].get("reason")
                raise HelloRejected(reason if isinstance(reason, str) else "other")
            tls.settimeout(POLL_S)
            return tls
        except BaseException:
            (tls or raw).close()
            raise

    def _send_all(self, tls: ssl.SSLSocket, payload: bytes) -> None:
        """레코드 하나를 다 보낸다. 소켓 시간 제한(POLL_S)마다 멈춤 요청을 보고, send_timeout_s 안에 못 보내면 TimeoutError.

        Windows 에서는 다른 스레드의 shutdown 이 막힌 send 를 깨우지 않으므로 짧은 시간 제한으로 나눠 보낸다. 시간 제한
        뒤에는 남은 바이트를 같은 순서로 다시 넘긴다(OpenSSL 의 재시도 규칙, Python 은 ACCEPT_MOVING_WRITE_BUFFER).
        """
        view = memoryview(payload)
        deadline = time.monotonic() + self._send_timeout
        while view:
            try:
                n = tls.send(view)
            except TimeoutError:
                if self._closing.is_set() or time.monotonic() >= deadline:
                    raise
                continue
            view = view[n:]

    def _check_incoming(self, tls: ssl.SSLSocket) -> None:
        """Tier 0 가 보낸 것이 있으면 읽는다(기다리지 않음). STREAM_REJECT 면 StreamRejected, 닫혔으면 ConnectionError."""
        if not tls.pending() and not select.select([tls], [], [], 0)[0]:
            return
        rec = wire.read_record(tls, max_meta=wire.MAX_HELLO_META, max_data=0)
        if rec is None:
            raise ConnectionError("Tier 0 가 연결을 닫았다")
        kind, meta, _ = rec
        if kind != wire.STREAM_REJECT:
            raise wire.WireError("연결 중에 올 수 없는 레코드")

        def as_int(v: Any) -> int | None:
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        reason = meta.get("reason")
        raise StreamRejected(reason if isinstance(reason, str) else "other", as_int(meta.get("stream_epoch")),
                             as_int(meta.get("current_epoch")))

    def _serve(self, tls: ssl.SSLSocket, target: VideoTarget) -> None:
        codec, notices = self.queue.link_up()
        self.connected = True
        self.current_target = target
        self._count("connects")
        log.info("%s Tier 0 영상 연결 %s:%d", self.camera_id, target.host, target.port)
        try:
            if codec is not None:
                self._send_all(tls, wire.encode(wire.CODEC_INFO, codec.codec_info))
                self._count("sent_codec_infos")
                for notice in notices:  # 지난 연결에서 정산하지 못한 버린 범위
                    self._send_all(tls, wire.encode(wire.SKIP_NOTICE, notice))
                    self._count("sent_skip_notices")
            while not self._closing.is_set():
                if self._target() != target:
                    log.warning("%s Tier 0 엔드포인트가 바뀌었다. 새 대상으로 다시 붙는다", self.camera_id)
                    break
                item = self.queue.get(timeout=0.2)
                if item is None:
                    self._check_incoming(tls)
                    continue
                kind, record = item
                self._check_incoming(tls)
                if kind == "frame":
                    self._send_all(tls, wire.encode(wire.FRAME, record.header, record.data))
                    self._count("sent_frames")
                    self._count("sent_bytes", len(record.data))
                elif kind == "codec":
                    self._send_all(tls, wire.encode(wire.CODEC_INFO, record.codec_info))
                    self._count("sent_codec_infos")
                else:
                    self._send_all(tls, wire.encode(wire.SKIP_NOTICE, record))
                    self._count("sent_skip_notices")
        except StreamRejected as e:
            self._count("stream_rejections")
            log.error("%s %s. 새 stream_epoch 가 올 때까지 보내지 않는다", self.camera_id, e)
            # 집계·로그를 마친 뒤에 상태를 내보낸다(상태를 본 쪽이 집계가 아직 0 인 것을 보지 않게)
            self.rejection = {"reason": e.reason, "stream_epoch": e.stream_epoch, "current_epoch": e.current_epoch}
        except (OSError, ssl.SSLError, wire.WireError) as e:
            if not self._closing.is_set():
                log.warning("%s Tier 0 영상 연결 끊김 (%s)", self.camera_id, type(e).__name__)
        finally:
            self.queue.link_down()
            self.connected = False
            self._count("disconnects")
            try:
                tls.close()
            except OSError:
                pass


class TlsSinkFactory:
    """RelaySession 의 SinkFactory. 카메라마다 독립 TlsCameraSink 를 만들고, 하트비트·상태용으로 모아 둔다."""

    def __init__(self, target_for: Callable[[str], TargetSource], **sink_kwargs: Any) -> None:
        self._target_for = target_for
        self._kwargs = sink_kwargs
        self.sinks: dict[str, TlsCameraSink] = {}

    def __call__(self, camera_id: str) -> TlsCameraSink:
        sink = TlsCameraSink(camera_id, self._target_for(camera_id), **self._kwargs)
        self.sinks[camera_id] = sink
        return sink

    def video_cameras(self) -> list[str]:
        """laptop_heartbeat payload.tier0.video_cameras: 영상 연결이 살아 있는 카메라."""
        return [c for c, s in self.sinks.items() if s.connected]

    def snapshots(self) -> list[dict[str, Any]]:
        return [s.snapshot() for s in self.sinks.values()]
