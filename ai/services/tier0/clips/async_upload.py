"""이벤트 클립 업로더 연결: EVENT_CLIP 서명 클라이언트, I1 기록 출구, 판정 루프를 막지 않는 업로드 스레드 (S15P31A101-748).

설계: docs/design/S15P31A101-748-event-clip-upload.md.

- 서명(EventClipSigner): 기존 `POST /api/uploads/sign`(purpose EVENT_CLIP, Tier 0 토큰). 객체 키는 서버가 정한다
  (`event-clips/<event_id>/<clip_id>.<확장자>`, 응답 키가 다르면 쓰지 않는다). 이벤트 프레임 서명(event_frames.EventFrameSigner)과
  같은 틀이다. EC2 에 닿지 않으면(연결 실패·시간 초과·502~504) SignerUnavailable 로 알려 업로더가 임시 저장소에 둔다.
- 기록 출구(I1ClipRecordSink): 업로드가 끝난 클립의 media_clip, 보낸 뒤 CRITICAL 로 오른 클립의 media_clip_retention 을 I1 신뢰 큐에
  넣는다. 큐가 차면 QueueFull 이 업로더로 올라가 다음 poll 에 기록만 다시 낸다(다시 올리지 않는다).
- 업로드 스레드(BackgroundClipUploader): EventClipService 는 판정 lock 안(단일 경로 정리 주기)이나 검출 프로세스 명령 스레드에서
  부르므로 서명·PUT(수 초~수십 초)을 거기서 하지 않는다. enqueue·보존 표시 상향은 받은 편지함에 넣기만 하고 바로 돌아온다.
  스레드가 편지함을 비워 ClipUploader 에 넘기고 poll 한다(재시도·상한·임시 저장소는 ClipUploader 그대로).
"""

from __future__ import annotations

import http.client
import json
import logging
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.tier0.clips.media import ExportClip
from services.tier0.clips.record import ClipDraft, ClipRetentionUpdate, MediaClipRecord
from services.tier0.clips.upload import (
    UPLOAD_PURPOSE,
    ClipUploader,
    DirectoryClipSpool,
    HttpPutTransport,
    RetryPolicy,
    SignedUpload,
    SignerUnavailable,
    UploadError,
    UploadSignRequest,
)
from simai_ai.risk.clock import Clock

log = logging.getLogger("services.tier0.clips")

SIGN_TIMEOUT_S = 5.0
"""서명 요청 시간 제한. EC2 응답은 수십 ms 이고, 넘기면 단절로 보고 임시 저장소에 둔다"""
PUT_TIMEOUT_S = 60.0
"""S3 PUT 시간 제한. 카메라 클립 40초 × 약 4 Mbps ≈ 20 MB 를 3 MB/s 로도 올릴 수 있게(event_frames 의 5 s 는 JPEG 한 장 기준)"""
POLL_S = 0.5
"""업로드 스레드가 편지함·대기열을 보는 간격(백오프 최소 1 s 보다 짧게)"""
MAX_INBOX_BYTES = 256 * 1024 * 1024
"""스레드가 아직 받지 않은 클립 내용 상한(RetryPolicy.max_queue_bytes 와 같다). 넘으면 버리고 센다"""
SPOOL_DIRNAME = "clip-spool"
_UNAVAILABLE_STATUS = frozenset({502, 503, 504})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib 시그니처
        return None


class EventClipSigner:
    """기존 /api/uploads/sign EVENT_CLIP API. 토큰·서명 주소는 로그에 쓰지 않는다."""

    def __init__(self, base_url: str, token: str, *, ca_file: str | None = None, timeout_s: float = SIGN_TIMEOUT_S,
                 opener: Any = None) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("클립 서명 API 는 계정·쿼리 없는 HTTPS 주소여야 한다")
        self._url = base_url.rstrip("/") + "/api/uploads/sign"
        self._token = token
        self._timeout_s = timeout_s
        if opener is None:
            context = ssl.create_default_context()
            if ca_file is not None:
                context.load_verify_locations(cafile=ca_file)
            opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))
        self._opener = opener

    def sign(self, request: UploadSignRequest) -> SignedUpload:
        if request.purpose != UPLOAD_PURPOSE or not request.clip_id:
            raise UploadError("EVENT_CLIP 서명 요청이 아니다", retryable=False)
        body = {"purpose": UPLOAD_PURPOSE, "event_id": request.event_id, "clip_id": request.clip_id,
                "content_type": request.content_type, "size_bytes": request.size_bytes,
                "sha256": request.sha256.removeprefix("sha256:")}
        req = urllib.request.Request(self._url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"})
        try:
            with self._opener.open(req, timeout=self._timeout_s) as response:
                status = getattr(response, "status", 200)
                raw = response.read()
        except urllib.error.HTTPError as e:
            if e.code in _UNAVAILABLE_STATUS:
                raise SignerUnavailable(f"서명 응답 {e.code}") from None
            # 400(형식·크기)·401/403(토큰)·409(같은 키 다른 내용)는 다시 보내도 같다. 5xx 는 상한까지 다시 시도한다
            raise UploadError(f"서명 응답 {e.code}", retryable=e.code in (408, 429) or e.code >= 500) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
            raise SignerUnavailable(f"서명 연결 실패: {type(e).__name__}") from None
        if status != 200:
            raise UploadError(f"서명 응답 {status}")
        try:
            signed = json.loads(raw)
            key = signed["object_key"]
            ok = (signed["method"] == "PUT" and isinstance(key, str)
                  and key.startswith(f"event-clips/{request.event_id}/{request.clip_id}.")
                  and isinstance(signed["upload_url"], str) and isinstance(signed["headers"], dict))
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            raise UploadError("클립 서명 응답이 올바르지 않다", retryable=False)
        return SignedUpload(signed["upload_url"], key, int(signed["expires_at_ms"]), signed["headers"],
                            signed.get("object_uri"))


class I1ClipRecordSink:
    """media_clip·media_clip_retention 을 I1 신뢰 큐로(send_event = I1Publisher.send_event, 차면 QueueFull)."""

    def __init__(self, send_event: Callable[[str, Mapping[str, Any]], Any]) -> None:
        self._send = send_event

    def emit(self, record: MediaClipRecord) -> None:
        self._send("media_clip", record.to_payload())
        log.info("media_clip_queued clip_id=%s event_id=%s camera=%s", record.draft.clip_id, record.draft.event_id,
                 record.draft.camera_id)

    def emit_retention(self, update: ClipRetentionUpdate) -> None:
        self._send("media_clip_retention", update.to_payload())


@dataclass(frozen=True)
class ClipIo:
    """클립 서명·업로드 설정. 분리 경로(-738)는 이것을 검출 프로세스로 넘겨 거기서 업로드한다(토큰은 repr 에 없음).

    signer_factory·transport_factory 는 시험 주입이다(spawn 자식이 이름으로 다시 불러오는 모듈 최상위 이름)."""

    base_url: str
    token: str = field(repr=False)
    ca_file: str | None = None
    spool_dir: str | None = None
    """EC2 단절·재시도 상한 동안 클립을 두는 폴더(Tier 0 상태 폴더 아래). None 이면 메모리에서만 다시 시도한다"""
    transport_timeout_s: float = PUT_TIMEOUT_S
    signer_factory: Callable[[], Any] | None = None
    transport_factory: Callable[[], Any] | None = None

    def signer(self) -> Any:
        if self.signer_factory is not None:
            return self.signer_factory()
        return EventClipSigner(self.base_url, self.token, ca_file=self.ca_file)

    def transport(self) -> Any:
        if self.transport_factory is not None:
            return self.transport_factory()
        return HttpPutTransport(timeout_s=self.transport_timeout_s)

    def build(self, clock: Clock, sink: Any, *, retry: RetryPolicy = RetryPolicy()) -> BackgroundClipUploader:
        spool = None if self.spool_dir is None else DirectoryClipSpool(Path(self.spool_dir))
        return BackgroundClipUploader(ClipUploader(self.signer(), self.transport(), sink, clock, spool=spool, retry=retry))


class BackgroundClipUploader:
    """ClipUploader 를 자기 스레드에서 돌린다. EventClipService 가 쓰는 면(enqueue·raise_retention_for_event·poll)은 기다리지 않는다."""

    def __init__(self, uploader: ClipUploader, *, poll_s: float = POLL_S, max_inbox_bytes: int = MAX_INBOX_BYTES) -> None:
        self.uploader = uploader
        self._poll_s = poll_s
        self._max_inbox_bytes = max_inbox_bytes
        self._inbox: deque[tuple[str, Any]] = deque()
        self._inbox_bytes = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.inbox_dropped = 0
        self.errors = 0
        self.restored = 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="tier0-clip-upload", daemon=True)
            self._thread.start()

    def enqueue(self, clip: ExportClip, draft: ClipDraft) -> None:
        if not isinstance(clip, ExportClip):
            raise TypeError("업로드는 이벤트 클립 경로가 만든 ExportClip 만 받는다")
        with self._lock:
            if self._inbox_bytes + clip.size_bytes > self._max_inbox_bytes:
                self.inbox_dropped += 1
                log.error("클립 업로드 편지함이 가득 차 %s(%s) 를 버렸다", draft.clip_id, draft.camera_id)
                return
            self._inbox.append(("clip", (clip, draft)))
            self._inbox_bytes += clip.size_bytes
        self._wake.set()

    def raise_retention_for_event(self, event_id: str, resume_count: int, severity: str) -> None:
        with self._lock:
            self._inbox.append(("hold", (event_id, resume_count, severity)))
        self._wake.set()

    def poll(self) -> None:
        """정리 주기에서 불린다. 스레드를 깨우기만 한다(업로드는 스레드가 한다)."""
        self._wake.set()

    @property
    def pending(self) -> int:
        with self._lock:
            inbox = sum(1 for kind, _ in self._inbox if kind == "clip")
        return inbox + self.uploader.pending

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._inbox:
                    return
                kind, args = self._inbox.popleft()
                if kind == "clip":
                    self._inbox_bytes -= args[0].size_bytes
            if kind == "clip":
                self.uploader.enqueue(*args)
            else:
                self.uploader.raise_retention_for_event(*args)

    def run_once(self) -> None:
        """편지함을 비우고 대기열을 한 번 돈다(스레드 한 바퀴, 시험은 직접 부른다). 예외를 내지 않는다."""
        try:
            self._drain()
            self.uploader.poll()
        except Exception as error:  # noqa: BLE001 - 업로드 실패가 스레드를 멈추지 않는다(poll 자체는 예외를 내지 않는다)
            self.errors += 1
            log.warning("클립 업로드 스레드 오류: %s", type(error).__name__)

    def _run(self) -> None:
        try:
            self.restored = self.uploader.restore_spooled()
            if self.restored:
                log.info("임시 저장소의 클립 %d 개를 다시 올린다", self.restored)
        except Exception as error:  # noqa: BLE001
            log.warning("임시 저장 클립 복원 실패: %s", type(error).__name__)
        while not self._stop.is_set():
            self._wake.wait(self._poll_s)
            self._wake.clear()
            if self._stop.is_set():
                break
            self.run_once()

    def stop(self, timeout_s: float) -> bool:
        """멈춘다. 진행 중인 PUT 은 자기 시간 제한까지 갈 수 있어 기다리지 않고 데몬 스레드로 둔다(임시 저장한 클립은 다음 기동이 올린다)."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(max(0.0, timeout_s))
        return self._thread is None or not self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            inbox, inbox_bytes = len(self._inbox), self._inbox_bytes
        return {**vars(self.uploader.stats), "queued": self.uploader.pending, "inbox": inbox, "inbox_bytes": inbox_bytes,
                "inbox_dropped": self.inbox_dropped, "errors": self.errors, "restored": self.restored}
