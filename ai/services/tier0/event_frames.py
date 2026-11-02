"""CRITICAL 발생 전이 → 제한 큐 → 추론 프레임 재사용·JPEG·서명 업로드 → I1 (명세 5.4).

판정 tick 은 이미지 참조만 put_nowait 한다. JPEG·네트워크는 상한 있는 스레드 풀에서 처리한다.
사후 프레임·클립 완료를 기다리지 않고, 실패는 경보·저널로 전파하지 않는다.

판단용과 보관용을 나눈다(S15P31A101-408). local_root(Tier 0 상태 폴더)가 있으면 JPEG 를
`<local_root>/event-frames/<event_id>/<n>-<camera>-<촬영 ms>.jpg` 에 원자적으로 쓰고, 서명(객체 주소 확정)만 받은 뒤
S3 업로드를 기다리지 않고 event_frame_ready 에 객체 주소와 로컬 사본(local_copy: 상대 경로·sha256·크기)을 함께 싣는다.
같은 Pod 의 분석 워커는 사본을 읽는다. S3 업로드(증거·이력·클립용)는 뒤 업로드 풀에서 계속한다(재업로드 재시도는 예전처럼 없음).
사본을 쓰지 못한 장은 예전처럼 업로드를 마친 뒤에만 싣는다. local_root 가 없으면 예전 순서(올린 뒤 알림) 그대로다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
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
MESSAGE_VERSION = "1.1.0"  # event_frame_ready 1.1.0: 프레임 선택 local_copy (-408)
LOCAL_DIRNAME = "event-frames"
# 계약 pod_local_frame.path 와 같은 꼴. 어긋나면(대문자 event_id 등) 사본을 싣지 않는다(메시지 전체가 계약 위반이 되지 않게)
LOCAL_PATH = re.compile(r"^event-frames/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[A-Za-z0-9_-]{1,96}\.jpg$")
LOCAL_RETENTION_S = 3600.0  # 판단용 사본은 VLM 마감(관측 + 5 s)·재시도 뒤에는 쓰지 않는다. 디스크가 쌓이지 않게 1시간 뒤 지운다
UPLOAD_BACKLOG = 32  # 뒤에서 올릴 사진 상한(이벤트 8건 × 4장, JPEG 약 0.5 MB 기준 16 MB). 차면 다음 이벤트 준비가 자리를 기다린다


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


@dataclass
class _Prepared:
    """JPEG·로컬 사본·서명까지 마친 사진 한 장. jpeg 는 S3 업로드가 끝날 때까지만 붙든다."""

    camera_id: str
    jpeg: bytes = field(repr=False)
    signed: SignedUpload
    frame: dict[str, Any]


class _UploadBatch:
    """이벤트 하나의 뒤 업로드 집계. 마지막 장이 끝나면 event_frames_uploaded 를 남긴다."""

    def __init__(self, event_id: str, occurred_at: int, total: int, now_ms: Callable[[], int]) -> None:
        self._event_id, self._occurred_at, self._total, self._now_ms = event_id, occurred_at, total, now_ms
        self._done = self._ok = 0
        self._lock = threading.Lock()

    def finish(self, ok: bool) -> None:
        with self._lock:
            self._done += 1
            self._ok += int(ok)
            last = self._done == self._total
            uploaded = self._ok
        if last:
            log.info("event_frames_uploaded event_id=%s frames=%d event_to_upload_ms=%d background=1",
                     self._event_id, uploaded, self._now_ms() - self._occurred_at)


class EventFrameService:
    """병합 이벤트 ID 당 최초 RAISED 1회. 큐 상한 초과·실패도 재판정이나 재경보를 만들지 않는다."""

    def __init__(self, ring: EventFrameBuffer, clock: Clock, *, signer: UploadSigner, transport: ClipTransport,
                 send_event: Callable[..., Any],
                 jpeg: Callable[[Any], bytes] = encode_jpeg, enabled: bool = True,
                 queue_depth: int = QUEUE_DEPTH, local_root: Path | None = None,
                 wall_s: Callable[[], float] = time.time) -> None:
        self._ring, self._clock = ring, clock
        self._signer, self._transport = signer, transport
        self._send_event, self._jpeg = send_event, jpeg
        self._enabled = enabled
        if queue_depth < 1:
            raise ValueError("event_frames queue_depth 는 양수다")
        # 판단용 로컬 사본(-408). 없으면 예전처럼 S3 업로드를 마친 뒤 알린다
        self._local_root = Path(local_root) if local_root is not None else None
        self._wall_s = wall_s
        self._uploads: ThreadPoolExecutor | None = None
        if self._local_root is not None:
            self._uploads = ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="tier0-event-s3")
        self._backlog = threading.BoundedSemaphore(UPLOAD_BACKLOG)
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
        if self._uploads is not None:
            # 대기 중인 뒤 업로드는 버린다(판단용 사본은 이미 알렸다). 진행 중인 PUT 은 자기 시간 제한 안에 끝난다
            self._uploads.shutdown(wait=False, cancel_futures=True)
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

    def _write_local(self, event_id: str, index: int, picture: EventPicture, jpeg: bytes,
                     digest: str) -> dict[str, Any] | None:
        """판단용 사본을 원자적으로 쓴다(임시 파일 → os.replace). 실패하면 None(그 장은 업로드 뒤에만 싣는다)."""
        if self._local_root is None:
            return None
        rel = f"{LOCAL_DIRNAME}/{event_id}/{index}-{picture.camera_id}-{picture.capture_time_ms}.jpg"
        if not LOCAL_PATH.fullmatch(rel):
            log.warning("event_frames_local_failed event_id=%s camera_id=%s reason=path", event_id, picture.camera_id)
            return None
        target = self._local_root / rel
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as handle:
                handle.write(jpeg)
            os.replace(tmp, target)
        except OSError as error:
            log.warning("event_frames_local_failed event_id=%s camera_id=%s reason=%s",
                        event_id, picture.camera_id, type(error).__name__)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return {"path": rel, "sha256": "sha256:" + digest, "size_bytes": len(jpeg)}

    def _prepare(self, event_id: str, index: int, picture: EventPicture) -> _Prepared | None:
        """JPEG → (로컬 사본) → 서명. 서명을 받아야 객체 주소(frame_uri)가 정해진다."""
        if self._closing.is_set():
            return None
        try:
            jpeg = self._jpeg(picture.image)
            digest = hashlib.sha256(jpeg).hexdigest()
            local = self._write_local(event_id, index, picture, jpeg, digest)
            signed = self._signer.sign(UploadSignRequest("EVENT_FRAME", "", event_id, picture.camera_id,
                                                        "image/jpeg", len(jpeg), digest))
            if not signed.object_uri:
                raise ValueError("영구 객체 주소 없음")
            frame: dict[str, Any] = {"camera_id": picture.camera_id, "frame_uri": signed.object_uri,
                                     "estimated_capture_time_ms": picture.capture_time_ms,
                                     "timestamp_source": picture.timestamp_source,
                                     "timestamp_uncertainty_ms": picture.timestamp_uncertainty_ms}
            if local is not None:
                frame["local_copy"] = local
            return _Prepared(picture.camera_id, jpeg, signed, frame)
        except Exception as error:  # noqa: BLE001 - 한 사진의 실패가 다른 사진·경보로 전파되지 않는다
            log.warning("event_frames_upload_failed event_id=%s camera_id=%s reason=%s",
                        event_id, picture.camera_id, type(error).__name__)
            return None

    def _put(self, event_id: str, prepared: _Prepared) -> bool:
        if self._closing.is_set():
            return False
        try:
            self._transport.put(prepared.signed, prepared.jpeg, "image/jpeg")
            return True
        except Exception as error:  # noqa: BLE001 - 한 사진의 실패가 다른 사진·경보로 전파되지 않는다
            log.warning("event_frames_upload_failed event_id=%s camera_id=%s reason=%s",
                        event_id, prepared.camera_id, type(error).__name__)
            return False

    def _upload_later(self, event_id: str, prepared: _Prepared, batch: _UploadBatch) -> Future[bool] | None:
        """뒤 업로드 풀에 넣는다. 자리(UPLOAD_BACKLOG)가 없으면 기다린다. 종료 중이면 None."""
        assert self._uploads is not None
        while not self._backlog.acquire(timeout=0.05):
            if self._closing.is_set():
                batch.finish(False)
                return None

        def run() -> bool:
            ok = False
            try:
                ok = self._put(event_id, prepared)
                return ok
            finally:
                prepared.jpeg = b""  # 올린 뒤(실패 포함) 바이트를 놓는다
                self._backlog.release()
                batch.finish(ok)

        try:
            return self._uploads.submit(run)
        except RuntimeError:  # 종료로 풀이 닫혔다
            self._backlog.release()
            batch.finish(False)
            return None

    def _prune_local(self) -> None:
        """LOCAL_RETENTION_S 보다 오래된 이벤트 사본 폴더를 지운다(판단용 캐시, 보관용 정본은 S3)."""
        if self._local_root is None:
            return
        cutoff = self._wall_s() - LOCAL_RETENTION_S
        try:
            entries = list(os.scandir(self._local_root / LOCAL_DIRNAME))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    shutil.rmtree(entry.path)
            except OSError as error:
                log.warning("event_frames_local_prune_failed reason=%s", type(error).__name__)

    def _process(self, request: _Request) -> None:
        # 이벤트는 단일 소비자가 처리한다. 준비 풀의 대기 작업도 이벤트당 최대 4개다.
        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="tier0-event-upload") as pool:
            futures = [pool.submit(self._prepare, request.event_id, index, picture)
                       for index, picture in enumerate(request.pictures)]
            prepared = [p for future in futures if (p := future.result()) is not None]
        if not prepared:
            log.warning("event_frames_failed event_id=%s reason=no_frames", request.event_id)
            return
        if self._uploads is None:
            # 로컬 사본 없음: 예전처럼 올린 뒤에만 싣는다
            with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="tier0-event-upload") as pool:
                oks = list(pool.map(lambda p: self._put(request.event_id, p), prepared))
            frames = [p.frame for p, ok in zip(prepared, oks, strict=True) if ok]
            if frames:
                log.info("event_frames_uploaded event_id=%s frames=%d event_to_upload_ms=%d",
                         request.event_id, len(frames), self._clock.now_ms() - request.occurred_at)
        else:
            batch = _UploadBatch(request.event_id, request.occurred_at, len(prepared), self._clock.now_ms)
            pending = [(p, self._upload_later(request.event_id, p, batch)) for p in prepared]
            frames = []
            for p, future in pending:
                if "local_copy" in p.frame:
                    frames.append(p.frame)  # 판단용 사본이 있으면 S3 업로드를 기다리지 않는다
                elif future is not None and _ok(future):
                    frames.append(p.frame)  # 사본을 쓰지 못한 장은 업로드를 마친 뒤에만 싣는다
        try:
            if not frames:
                log.warning("event_frames_failed event_id=%s reason=no_frames", request.event_id)
                return
            self._send_ready(request, frames)
        finally:
            self._prune_local()

    def _send_ready(self, request: _Request, frames: list[dict[str, Any]]) -> None:
        payload = {"event_id": request.event_id, "frames": frames}
        # I1 LIVE 계약은 1.1.0(선택 local_copy)까지만 쓴다. 검사는 비동기 스레드에서만 한다.
        probe = {"schema_version": MESSAGE_VERSION, "msg_type": "event_frame_ready",
                 "msg_id": request.event_id, "source_id": "tier0", "sent_at": self._clock.now_ms(), "payload": payload}
        if schema_errors(probe, "event_frame_ready"):
            raise ValueError("event_frame_ready 계약 위반")
        local = sum("local_copy" in f for f in frames)
        log.info("event_frames_ready event_id=%s frames=%d local=%d event_to_ready_ms=%d",
                 request.event_id, len(frames), local, self._clock.now_ms() - request.occurred_at)
        while not self._closing.is_set():
            try:
                self._send_event("event_frame_ready", payload)
                log.info("event_frame_ready_queued event_id=%s frames=%d", request.event_id, len(frames))
                return
            except QueueFull:
                # 같은 객체 주소를 붙든 채 신뢰 큐 자리만 기다린다. 다시 업로드하지 않는다.
                self._closing.wait(0.05)
        log.warning("event_frames_failed event_id=%s reason=stopped_before_enqueue", request.event_id)


def _ok(future: Future[bool]) -> bool:
    try:
        return bool(future.result())
    except Exception:  # noqa: BLE001 - 취소(종료)·예외는 실패로 본다
        return False
