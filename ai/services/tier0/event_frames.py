"""CRITICAL 발생 전이 → 제한 큐 → 링 버퍼 디코딩·JPEG·서명 업로드 → I1 (명세 5.4).

판정 tick 은 작은 불변 작업만 put_nowait 한다. 영상·네트워크는 단일 CPU 스레드에서만 처리한다.
사후 프레임·클립 완료를 기다리지 않고, 실패는 경보·저널로 전파하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from services.tier0.clips.ring_buffer import BufferedPacket, CameraCut, PacketRingBuffer
from services.tier0.clips.upload import ClipTransport, SignedUpload, UploadSigner, UploadSignRequest
from services.tier0.i1_client import QueueFull
from simai_ai.io.decoder import Decoder
from simai_ai.providers.contract import schema_errors
from simai_ai.risk import Origin, Severity
from simai_ai.risk.clock import Clock
from simai_ai.risk.lifecycle import Change, EventTransition, Lifecycle

log = logging.getLogger("services.tier0.event_frames")
PRE_MS = 3_000
WINDOW_MS = 5_000
CURRENT_MAX_AGE_MS = 500
MAX_CAMERAS = 2
QUEUE_DEPTH = 8
JPEG_QUALITY = 85


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EventFrameSigner:
    """기존 /api/uploads/sign EVENT_FRAME API. 토큰·서명 URL 은 로그에 쓰지 않는다."""

    def __init__(self, base_url: str, token: str, *, ca_file: str | None = None, timeout_s: float = 2.0) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("이벤트 프레임 서명 API 는 계정·쿼리 없는 HTTPS 주소여야 한다")
        self._url = base_url.rstrip("/") + "/api/uploads/sign"
        self._token = token
        self._timeout_s = timeout_s
        context = ssl.create_default_context()
        if ca_file is not None:
            context.load_verify_locations(cafile=ca_file)
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def sign(self, request: UploadSignRequest) -> SignedUpload:
        body = {"purpose": "EVENT_FRAME", "event_id": request.event_id, "content_type": request.content_type,
                "size_bytes": request.size_bytes, "sha256": request.sha256.removeprefix("sha256:")}
        req = urllib.request.Request(self._url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"})
        with self._opener.open(req, timeout=self._timeout_s) as response:
            if response.status != 200:
                raise ValueError("프레임 서명 응답 실패")
            signed = json.loads(response.read())
        uri = signed["object_uri"]
        parts = urllib.parse.urlsplit(uri)
        if (signed["method"] != "PUT" or parts.scheme != "s3" or not parts.netloc or parts.query or parts.fragment
                or parts.path != "/" + signed["object_key"]
                or not signed["object_key"].startswith(f"event-frames/{request.event_id}/")):
            raise ValueError("프레임 서명 응답 객체 주소가 올바르지 않다")
        return SignedUpload(signed["upload_url"], signed["object_key"], signed["expires_at_ms"], signed["headers"], uri)


def encode_jpeg(image: Any) -> bytes:
    import cv2

    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ValueError("JPEG 인코딩 실패")
    return encoded.tobytes()


def selected_packets(cut: CameraCut, anchor_ms: int) -> tuple[BufferedPacket, ...]:
    """현재(최대 0.5초 전) + 3~5초 전의 가장 최근 프레임. 사후·너무 오래된 영상은 쓰지 않는다."""
    packets = cut.packets
    chosen = []
    for start, end in ((anchor_ms - CURRENT_MAX_AGE_MS, anchor_ms), (anchor_ms - WINDOW_MS, anchor_ms - PRE_MS)):
        candidates = [p for p in packets if start <= p.capture_time_ms <= end and p.timestamp_source is not None]
        if candidates:
            chosen.append(max(candidates, key=lambda p: p.capture_time_ms))
    return tuple(chosen)


def decode_selected(cut: CameraCut, selected: tuple[BufferedPacket, ...],
                    decoder_factory: Callable[[], Decoder]) -> list[tuple[BufferedPacket, Any]]:
    """기존 디코더의 FrameKey 로만 원본 헤더와 그림을 결합한다(B 프레임·flush 포함)."""
    wanted = {(p.stream_epoch, p.frame_sequence): p for p in selected}
    decoded: dict[tuple[int, int], Any] = {}
    decoder = decoder_factory()

    def collect(pictures):
        for picture in pictures:
            if picture.key in wanted:
                decoded[picture.key] = picture.image

    try:
        for segment in cut.segments:
            # 빈칸 뒤 키프레임은 새 디코더에서 시작(깨진 참조를 재사용하지 않음).
            previous = None
            decoder.configure(segment.codec)
            for packet in segment.packets:
                if previous is not None and packet.frame_sequence != previous + 1:
                    collect(decoder.flush())
                    decoder.configure(segment.codec)
                collect(decoder.decode(packet.payload, (packet.stream_epoch, packet.frame_sequence), packet.keyframe))
                previous = packet.frame_sequence
            collect(decoder.flush())
    finally:
        decoder.reset()
    return [(p, decoded[key]) for key, p in wanted.items() if key in decoded]


@dataclass(frozen=True)
class _Request:
    event_id: str
    anchor_ms: int
    occurred_at: int
    cameras: tuple[str, ...]


class EventFrameService:
    """병합 이벤트 ID 당 최초 RAISED 1회. 큐 상한 초과·실패도 재판정이나 재경보를 만들지 않는다."""

    def __init__(self, ring: PacketRingBuffer, clock: Clock, *, signer: UploadSigner, transport: ClipTransport,
                 send_event: Callable[..., Any], decoder_factory: Callable[[], Decoder],
                 jpeg: Callable[[Any], bytes] = encode_jpeg, enabled: bool = True,
                 queue_depth: int = QUEUE_DEPTH) -> None:
        self._ring, self._clock = ring, clock
        self._signer, self._transport = signer, transport
        self._send_event, self._decoder_factory, self._jpeg = send_event, decoder_factory, jpeg
        self._enabled = enabled
        if queue_depth < 1:
            raise ValueError("event_frames queue_depth 는 양수다")
        self._queue: queue.Queue[_Request] = queue.Queue(maxsize=queue_depth)
        self._seen: set[str] = set()  # 판정 lock 안에서만 접근. 같은 event_id 는 재개 때도 다시 보내지 않는다.
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._enabled:
            self._thread = threading.Thread(target=self._run, name="tier0-event-frames", daemon=True)
            self._thread.start()

    def stop(self, timeout_s: float) -> bool:
        self._closing.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        return self._thread is None or not self._thread.is_alive()

    def on_transitions(self, transitions: Iterable[EventTransition], tracks: Iterable[Any] = ()) -> None:
        if not self._enabled or self._closing.is_set():
            return
        tracks = tuple(tracks)
        for transition in transitions:
            state = transition.state
            if (Origin(state.origin) is not Origin.LIVE or Severity(state.severity) is not Severity.CRITICAL
                    or state.rule_id not in {"R1", "R2", "R3", "R4"}
                    or transition.transition is not Lifecycle.RAISED or Change.RAISED not in transition.changes
                    or state.event_id in self._seen):
                continue
            self._seen.add(state.event_id)
            # 대상 World Track 을 관측한 카메라 우선. 여러 대상이 보이면 더 많은 대상을 본 쪽부터.
            visibility = Counter(camera for track in tracks if track.world_id in state.world_ids for camera in track.cameras)
            cameras = tuple(sorted(self._ring.camera_ids, key=lambda c: (-visibility[c], c)))
            try:
                self._queue.put_nowait(_Request(state.event_id, state.observation_time, transition.occurred_at, cameras))
            except queue.Full:
                log.warning("event_frames_failed event_id=%s reason=queue_full", state.event_id)

    def _run(self) -> None:
        while not self._closing.is_set():
            try:
                request = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._process(request)
            except Exception as error:  # noqa: BLE001 - 영상·네트워크 오류가 경보 경로로 새지 않는다
                log.warning("event_frames_failed event_id=%s reason=%s", request.event_id, type(error).__name__)

    def _process(self, request: _Request) -> None:
        frames = []
        used_cameras = 0
        for camera in request.cameras:
            if self._closing.is_set():
                return
            cut = self._ring.camera(camera).cut(request.anchor_ms - WINDOW_MS, request.anchor_ms)
            selected = selected_packets(cut, request.anchor_ms)
            if not selected:
                continue
            try:
                pictures = decode_selected(cut, selected, self._decoder_factory)
            except Exception as error:  # noqa: BLE001 - 한 카메라 오류에도 다른 카메라를 확인한다
                log.warning("event_frames_camera_failed event_id=%s camera_id=%s reason=%s",
                            request.event_id, camera, type(error).__name__)
                continue
            before = len(frames)
            for packet, image in pictures:
                try:
                    jpeg = self._jpeg(image)
                    digest = hashlib.sha256(jpeg).hexdigest()
                    signed = self._signer.sign(UploadSignRequest("EVENT_FRAME", "", request.event_id, camera,
                                                                "image/jpeg", len(jpeg), digest))
                    if not signed.object_uri:
                        raise ValueError("영구 객체 주소 없음")
                    self._transport.put(signed, jpeg, "image/jpeg")
                    frames.append({"camera_id": camera, "frame_uri": signed.object_uri,
                                   "estimated_capture_time_ms": packet.capture_time_ms,
                                   "timestamp_source": packet.timestamp_source,
                                   "timestamp_uncertainty_ms": packet.timestamp_uncertainty_ms})
                except Exception as error:  # noqa: BLE001 - 업로드 실패는 로그만
                    log.warning("event_frames_upload_failed event_id=%s camera_id=%s reason=%s",
                                request.event_id, camera, type(error).__name__)
            if len(frames) > before:
                used_cameras += 1
                if used_cameras == MAX_CAMERAS:
                    break
        if not frames:
            log.warning("event_frames_failed event_id=%s reason=no_frames", request.event_id)
            return
        payload = {"event_id": request.event_id, "frames": frames}
        # I1 LIVE 계약은 바꾸지 않는다. 검사는 비동기 스레드에서만 한다.
        probe = {"schema_version": "1.0.0", "msg_type": "event_frame_ready",
                 "msg_id": request.event_id, "source_id": "tier0", "sent_at": self._clock.now_ms(), "payload": payload}
        if schema_errors(probe, "event_frame_ready"):
            raise ValueError("event_frame_ready 계약 위반")
        uploaded_at = self._clock.now_ms()
        log.info("event_frames_uploaded event_id=%s frames=%d event_to_upload_ms=%d",
                 request.event_id, len(frames), uploaded_at - request.occurred_at)
        while not self._closing.is_set():
            try:
                self._send_event("event_frame_ready", payload)
                log.info("event_frame_ready_queued event_id=%s frames=%d", request.event_id, len(frames))
                return
            except QueueFull:
                # 같은 객체 주소를 붙든 채 신뢰 큐 자리만 기다린다. 다시 업로드하지 않는다.
                self._closing.wait(0.05)
        log.warning("event_frames_failed event_id=%s reason=stopped_before_enqueue", request.event_id)
