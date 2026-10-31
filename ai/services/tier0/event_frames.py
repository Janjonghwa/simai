"""CRITICAL 발생 전이 → 제한 큐 → 추론 프레임 재사용·JPEG·서명 업로드 → I1 (명세 5.4).

판정 tick 은 이미지 참조만 put_nowait 한다. JPEG·네트워크는 상한 있는 스레드 풀에서 처리한다.
사후 프레임·클립 완료를 기다리지 않고, 실패는 경보·저널로 전파하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import ssl
import threading
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from services.tier0.clips.upload import ClipTransport, SignedUpload, UploadSigner, UploadSignRequest
from services.tier0.event_frame_buffer import EventFrameBuffer, EventPicture
from services.tier0.i1_client import QueueFull
from simai_ai.providers.base import Frame
from simai_ai.providers.contract import schema_errors
from simai_ai.risk import Origin, Severity
from simai_ai.risk.clock import Clock
from simai_ai.risk.lifecycle import Change, EventTransition, Lifecycle

log = logging.getLogger("services.tier0.event_frames")
MAX_CAMERAS = 2
UPLOAD_WORKERS = 4
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


@dataclass(frozen=True)
class _Request:
    event_id: str
    anchor_ms: int
    occurred_at: int
    pictures: tuple[EventPicture, ...]


class EventFrameService:
    """병합 이벤트 ID 당 최초 RAISED 1회. 큐 상한 초과·실패도 재판정이나 재경보를 만들지 않는다."""

    def __init__(self, ring: EventFrameBuffer, clock: Clock, *, signer: UploadSigner, transport: ClipTransport,
                 send_event: Callable[..., Any],
                 jpeg: Callable[[Any], bytes] = encode_jpeg, enabled: bool = True,
                 queue_depth: int = QUEUE_DEPTH) -> None:
        self._ring, self._clock = ring, clock
        self._signer, self._transport = signer, transport
        self._send_event, self._jpeg = send_event, jpeg
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
        # 호출자는 판정 스레드를 먼저 멈춘다. 종료 후 대기 사진 참조를 해제한다.
        self._ring.clear()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
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
                pictures = self._ring.snapshot(cameras, state.observation_time, MAX_CAMERAS)
                self._queue.put_nowait(_Request(state.event_id, state.observation_time, transition.occurred_at, pictures))
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
            finally:
                del request  # 다음 이벤트를 기다리는 동안 이미지를 붙들지 않는다.

    def offer(self, frame: Frame, _detections: Any = None) -> None:
        """판정 스레드에서 참조만 보관한다. JPEG·복사·네트워크를 하지 않는다."""
        if self._enabled and not self._closing.is_set() and not self._ring.offer(frame):
            log.warning("event_frames_cache_dropped camera_id=%s", frame.camera_id)

    def attach(self, feed: Any) -> None:
        """기존 배경 수집과 함께, 검출 결과가 원본 헤더와 일치한 프레임만 받는다."""
        background_offer = feed.observe_decoded

        def offer_both(frame, detections):
            try:
                self.offer(frame, detections)
            finally:
                if background_offer is not None:
                    background_offer(frame, detections)

        feed.observe_decoded = offer_both

    def _upload(self, event_id: str, picture: EventPicture) -> dict[str, Any] | None:
        if self._closing.is_set():
            return None
        try:
            jpeg = self._jpeg(picture.image)
            digest = hashlib.sha256(jpeg).hexdigest()
            signed = self._signer.sign(UploadSignRequest("EVENT_FRAME", "", event_id, picture.camera_id,
                                                        "image/jpeg", len(jpeg), digest))
            if not signed.object_uri:
                raise ValueError("영구 객체 주소 없음")
            self._transport.put(signed, jpeg, "image/jpeg")
            return {"camera_id": picture.camera_id, "frame_uri": signed.object_uri,
                    "estimated_capture_time_ms": picture.capture_time_ms,
                    "timestamp_source": picture.timestamp_source,
                    "timestamp_uncertainty_ms": picture.timestamp_uncertainty_ms}
        except Exception as error:  # noqa: BLE001 - 한 사진의 실패가 다른 사진·경보로 전파되지 않는다
            log.warning("event_frames_upload_failed event_id=%s camera_id=%s reason=%s",
                        event_id, picture.camera_id, type(error).__name__)
            return None

    def _process(self, request: _Request) -> None:
        # 이벤트는 단일 소비자가 처리한다. 풀의 대기 작업도 이벤트당 최대 4개다.
        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="tier0-event-upload") as pool:
            futures = [pool.submit(self._upload, request.event_id, picture) for picture in request.pictures]
            frames = [frame for future in futures if (frame := future.result()) is not None]
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
