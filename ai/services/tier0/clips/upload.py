"""클립 업로드: 임시 서명 주소 → 객체 저장소 PUT → media_clip 기록 (명세 5.1, 12.6 POST /api/uploads/sign, 14, D-G3-19).

- 서명 주소 발급(UploadSigner)과 전송(ClipTransport)은 주입한다. 운영 조립(EVENT_CLIP 서명 클라이언트·업로드 스레드·I1 기록 출구)은
  async_upload.py(S15P31A101-748).
  객체 저장소는 비공개이고 임시 서명 주소로만 올린다(14). 서명 주소는 짧게 살아 있으므로 시도할 때마다 새로 받는다.
- 다시 시도: 서명·전송이 실패하면 지수 백오프로 다시 시도한다. 시간은 주입 시계로만 재고 잠들지 않는다(poll 로 돈다).
- EC2 단절(SignerUnavailable): 클립을 Network Volume 임시 저장소(ClipSpool)에 먼저 저장하고, 복구 뒤 올린다(5.1, 11.5).
  임시 저장소가 없으면 메모리 대기열에서만 다시 시도한다. 임시 저장한 클립은 내용을 메모리에서 내리고 시도할 때 다시 읽는다.
- 시도 상한: 임시 저장소가 없으면 상한(max_attempts)을 넘긴 클립을 버린다. 있으면 임시 저장소에 둔 채 긴 간격
  (parked_retry_ms)으로 계속 다시 시도한다.
- 보관 기간(11.5 클립 14일, 18.4 '삭제 예정일 지난 클립 0건'): 삭제 예정일(delete_after_ms)이 지난 클립은 보존 표시가
  없으면 올리지 않고 대기열·임시 저장소에서 지운다. 기동 때 복원(restore_spooled)과 poll 마다 확인한다. 보존 표시(CRITICAL)
  클립은 프로젝트 종료 파기(18.4) 전까지 올린다.
- 크래시 잔여물: 짝 없는 `.clip`, 쓰다 만 `.part`, 읽을 수 없는 `.json`, 해시가 기록과 다른 클립처럼 내용을 확인할 수 없는
  파일은 올리지 않고 지운다(개인정보 보관 최소화). 기동 때(해시까지 확인)와 sweep_interval_ms 마다(구조만 확인) 정리한다.
- 서명기·전송·임시 저장소·기록 출구가 어떤 예외를 내도 그 작업의 실패로 기록하고 백오프한다. Tier 0 루프로 전파하지
  않는다. 로그에는 예외 종류 이름만 남긴다(서명 주소가 섞이지 않게).
- 업로드·임시 저장은 이벤트 클립 경로(media.prepare_for_export)가 만든 ExportClip 만 받는다. 그 밖의 영상은 이 모듈로
  들어오지 않는다. 얼굴 흐림은 기본으로 하지 않는다(사용자 결정, media.py).
- 업로드가 끝나면 MediaClipRecord 를 기록 출구(ClipRecordSink, 이벤트 저널·outbox 쪽)로 내고 임시 저장본을 지운다.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from services.tier0.clips.media import ExportClip
from services.tier0.clips.record import RETENTION_REASON_CRITICAL, ClipDraft, ClipRetentionUpdate, MediaClipRecord
from simai_ai.risk.clock import Clock

log = logging.getLogger("services.tier0.clips")

UPLOAD_PURPOSE = "EVENT_CLIP"
_CLIP_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


# ---------------------------------------------------------------------------- 주입 인터페이스


@dataclass(frozen=True)
class UploadSignRequest:
    """POST /api/uploads/sign 에 보낼 내용(서버 요청 본문은 서버 OpenAPI 확정 전 기본안)."""

    purpose: str
    clip_id: str
    event_id: str
    camera_id: str
    content_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class SignedUpload:
    """서명된 업로드 주소. headers 는 서명에 묶인 헤더(Content-Type 등)다. 주소·헤더는 로그에 남기지 않는다."""

    upload_url: str = field(repr=False)
    object_key: str
    expires_at_ms: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    object_uri: str | None = None  # EVENT_FRAME 은 버킷을 포함한 영구 객체 주소를 I1 에 싣는다


class SignerUnavailable(Exception):
    """EC2 에 닿지 않아 서명 주소를 받을 수 없다(단절). 클립을 Network Volume 에 먼저 저장한다."""


class UploadError(Exception):
    """서명 거부·전송 실패. retryable 이 아니어도 서명 만료일 수 있어 새 주소로 상한까지 다시 시도한다."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class UploadSigner(Protocol):
    def sign(self, request: UploadSignRequest) -> SignedUpload: ...


@runtime_checkable
class ClipTransport(Protocol):
    def put(self, signed: SignedUpload, content: bytes, content_type: str) -> None: ...


@runtime_checkable
class ClipRecordSink(Protocol):
    def emit(self, record: MediaClipRecord) -> None: ...

    def emit_retention(self, update: ClipRetentionUpdate) -> None: ...


@runtime_checkable
class ClipSpool(Protocol):
    """Network Volume 임시 저장소(EC2 단절 중 이벤트 클립만). ExportClip 만 받는다."""

    def save(self, clip: ExportClip, draft: ClipDraft) -> None: ...

    def update_draft(self, draft: ClipDraft) -> None: ...

    def drafts(self) -> list[ClipDraft]: ...

    def load(self, clip_id: str) -> ExportClip: ...

    def delete(self, clip_id: str) -> None: ...

    def sweep(self, verify_content: bool) -> int: ...


# ---------------------------------------------------------------------------- 기본 구현


class HttpPutTransport:
    """서명 주소로 HTTPS PUT. TLS 검증을 끄지 않는다(14). 평문 http 는 루프백 개발 서버만."""

    def __init__(self, timeout_s: float = 30.0, opener: Callable[..., Any] | None = None) -> None:
        self._timeout_s = timeout_s
        self._open = opener or urllib.request.urlopen

    def put(self, signed: SignedUpload, content: bytes, content_type: str) -> None:
        url = urllib.parse.urlsplit(signed.upload_url)
        host = url.hostname or ""
        if url.scheme != "https" and not (url.scheme == "http" and _is_loopback(host)):
            raise UploadError("서명 주소가 https 가 아니다", retryable=False)
        headers = {"Content-Type": content_type, **dict(signed.headers)}
        request = urllib.request.Request(signed.upload_url, data=content, method="PUT", headers=headers)
        try:
            with self._open(request, timeout=self._timeout_s) as resp:
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as e:
            raise UploadError(f"PUT 응답 {e.code}", retryable=e.code in (408, 429) or e.code >= 500) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
            raise UploadError(f"PUT 연결 실패: {type(e).__name__}", retryable=True) from None
        if not 200 <= status < 300:
            raise UploadError(f"PUT 응답 {status}", retryable=status in (408, 429) or status >= 500)


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False




class DirectoryClipSpool:
    """Network Volume 폴더 임시 저장소. `<clip_id>.clip`(이벤트 클립) + `<clip_id>.json`(기록 초안).

    임시 파일에 쓰고 fsync 뒤 이름을 바꿔, 중간에 멈춰도 반쯤 쓴 클립을 올리지 않는다. 내용 파일이 먼저, 초안이 나중이라
    초안(.json)이 있으면 내용도 있다. 읽을 때 내용 해시가 초안과 다르면 올리지 않는다. 보관 기간 판단(삭제 예정일)은
    ClipUploader 가 한다.

    이 폴더는 정본이 아니라 아직 내보내지 않은 기록의 대기 공간(outbox)이다. 그래서 보내기 전 초안의 보존 표시는
    update_draft 로 고쳐 쓴다. 기록 출구로 한 번 나간 뒤에는 바꾸지 않고 ClipRetentionUpdate 를 덧붙인다(AGENTS.md 규칙 6).
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _paths(self, clip_id: str) -> tuple[Path, Path]:
        if not _CLIP_ID.fullmatch(clip_id):
            raise ValueError(f"clip_id 형식이 아니다: {clip_id!r}")
        return self.root / f"{clip_id}.clip", self.root / f"{clip_id}.json"

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".part")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _meta(clip: ExportClip, draft: ClipDraft) -> bytes:
        meta = {
            "draft": draft.to_payload(),
            "clip": {"content_type": clip.content_type, "container": clip.container,
                     "face_redaction": clip.face_redaction, "redactor_version": clip.redactor_version},
        }
        return json.dumps(meta, ensure_ascii=False).encode("utf-8")

    def save(self, clip: ExportClip, draft: ClipDraft) -> None:
        if not isinstance(clip, ExportClip):
            raise TypeError("임시 저장소는 이벤트 클립 경로가 만든 ExportClip 만 받는다")
        content_path, meta_path = self._paths(draft.clip_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_atomic(content_path, clip.content)
        self._write_atomic(meta_path, self._meta(clip, draft))

    def _read_meta(self, clip_id: str) -> dict[str, Any]:
        return json.loads(self._paths(clip_id)[1].read_text(encoding="utf-8"))

    def update_draft(self, draft: ClipDraft) -> None:
        """초안만 새로 쓴다(아직 보내지 않은 기록의 보존 표시 상향). 내용은 그대로다."""
        meta = self._read_meta(draft.clip_id)
        meta["draft"] = draft.to_payload()
        self._write_atomic(self._paths(draft.clip_id)[1], json.dumps(meta, ensure_ascii=False).encode("utf-8"))

    def drafts(self) -> list[ClipDraft]:
        out: list[ClipDraft] = []
        if not self.root.is_dir():
            return out
        for meta_path in sorted(self.root.glob("*.json")):
            try:
                out.append(ClipDraft.from_payload(json.loads(meta_path.read_text(encoding="utf-8"))["draft"]))
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.error("임시 저장 클립 초안 %s 를 읽지 못했다: %s", meta_path.name, type(e).__name__)
        return out

    def load(self, clip_id: str) -> ExportClip:
        meta = self._read_meta(clip_id)
        draft = ClipDraft.from_payload(meta["draft"])
        c = meta["clip"]
        clip = ExportClip(self._paths(clip_id)[0].read_bytes(), c["content_type"], c["container"],
                            c["face_redaction"], c["redactor_version"])
        if clip.sha256 != draft.sha256:
            raise ValueError(f"임시 저장 클립 {clip_id} 의 해시가 기록과 다르다")
        return clip

    def delete(self, clip_id: str) -> None:
        for p in self._paths(clip_id):
            p.unlink(missing_ok=True)

    def sweep(self, verify_content: bool) -> int:
        """내용을 확인할 수 없는 파일을 지운다. 지운 파일 수.

        - `*.part`(쓰다 만 임시 파일), 초안 없는 `.clip`, 내용 없는 `.json`, 읽을 수 없는 `.json`, 이름 형식이 틀린 파일
        - verify_content 면 해시가 초안과 다른 클립(짝 모두)도 지운다(기동 때. 클립을 모두 읽는다)
        """
        if not self.root.is_dir():
            return 0
        removed = 0

        def rm(path: Path) -> None:
            nonlocal removed
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as e:
                log.error("임시 저장 파일 %s 를 지우지 못했다: %s", path.name, type(e).__name__)

        for path in sorted(self.root.iterdir()):
            if not path.is_file():
                continue
            if path.name.endswith(".part") or path.suffix not in (".clip", ".json") or not _CLIP_ID.fullmatch(path.stem):
                rm(path)
                continue
            content_path, meta_path = self._paths(path.stem)
            if path.suffix == ".clip":
                if not meta_path.exists():
                    rm(path)
                continue
            if not content_path.exists():
                rm(path)
                continue
            try:
                if verify_content:
                    self.load(path.stem)
                else:
                    ClipDraft.from_payload(self._read_meta(path.stem)["draft"])
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.error("임시 저장 클립 %s 를 확인할 수 없어 지운다: %s", path.stem, type(e).__name__)
                rm(meta_path)
                rm(content_path)
        return removed


# ---------------------------------------------------------------------------- 업로더


@dataclass(frozen=True)
class RetryPolicy:
    initial_backoff_ms: int = 1_000
    max_backoff_ms: int = 60_000
    max_attempts: int = 8  # 서명·전송 실패 횟수 상한(EC2 단절로 임시 저장한 동안은 세지 않는다)
    parked_retry_ms: int = 600_000  # 상한을 넘겨 임시 저장소에 둔 클립을 다시 시도하는 간격
    max_queue_bytes: int = 256 * 1024 * 1024  # 메모리에 든 클립 내용 상한. 넘으면 임시 저장소로(없으면 버린다)
    max_sent_memory: int = 4096  # 보존 표시 상향용으로 기억하는 보낸 클립 수
    sweep_interval_ms: int = 3_600_000  # 임시 저장소 크래시 잔여물 정리 간격

    def __post_init__(self) -> None:
        for name in ("initial_backoff_ms", "max_backoff_ms", "max_attempts", "parked_retry_ms", "max_queue_bytes",
                     "max_sent_memory", "sweep_interval_ms"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"RetryPolicy.{name}: 양의 정수여야 한다 (받은 값 {v!r})")

    def backoff_ms(self, failures: int) -> int:
        return min(self.max_backoff_ms, self.initial_backoff_ms * 2 ** min(max(0, failures - 1), 30))


@dataclass
class _Job:
    draft: ClipDraft
    content: ExportClip | None  # None 이면 임시 저장소에만 있다(시도할 때 읽는다)
    next_attempt_ms: int
    failures: int = 0  # 시도 상한에 넣는 실패(서명 거부·전송 실패, 임시 저장 못 한 단절, 그 밖의 예외)
    outages: int = 0  # 임시 저장한 뒤 EC2 단절로 미룬 횟수(상한에 넣지 않는다)
    attempts: int = 0  # 업로드 시도 횟수 전체(기록에 남긴다)
    spooled: bool = False
    parked: bool = False  # 상한을 넘겨 임시 저장소에서 긴 간격으로 다시 시도하는 중
    record: MediaClipRecord | None = None  # 업로드는 끝났고 기록 출구만 남았다


@dataclass
class UploaderStats:
    uploaded: int = 0
    spooled: int = 0
    dropped: int = 0
    failures: int = 0
    expired: int = 0  # 삭제 예정일이 지나 올리지 않고 지운 클립
    swept_files: int = 0  # 내용을 확인할 수 없어 지운 임시 저장 파일
    retention_updates: int = 0


class ClipUploader:
    """이벤트 클립 대기열. enqueue 로 넣고 poll 로 시도한다(Tier 0 루프에서 주기적으로 부른다). poll 은 예외를 내지 않는다."""

    def __init__(
        self,
        signer: UploadSigner,
        transport: ClipTransport,
        sink: ClipRecordSink,
        clock: Clock,
        spool: ClipSpool | None = None,
        retry: RetryPolicy = RetryPolicy(),
    ) -> None:
        self._signer = signer
        self._transport = transport
        self._sink = sink
        self._clock = clock
        self._spool = spool
        self._retry = retry
        self._jobs: dict[str, _Job] = {}
        self._sent: OrderedDict[str, ClipDraft] = OrderedDict()
        self._updates: list[ClipRetentionUpdate] = []
        self._next_sweep_ms: int | None = None
        self.stats = UploaderStats()

    @property
    def pending(self) -> int:
        return len(self._jobs)

    def memory_bytes(self) -> int:
        """메모리에 들고 있는 클립 내용 바이트(임시 저장한 클립은 넣지 않는다)."""
        return sum(j.content.size_bytes for j in self._jobs.values() if j.content is not None)

    def enqueue(self, clip: ExportClip, draft: ClipDraft) -> None:
        if not isinstance(clip, ExportClip):
            raise TypeError("업로드는 이벤트 클립 경로가 만든 ExportClip 만 받는다")
        job = _Job(draft, clip, next_attempt_ms=self._clock.now_ms())
        if self.memory_bytes() + clip.size_bytes > self._retry.max_queue_bytes and not self._spool_job(job):
            self.stats.dropped += 1
            log.error("클립 대기열이 가득 차 %s(%s) 를 버렸다", draft.clip_id, draft.camera_id)
            return
        self._jobs[draft.clip_id] = job

    @staticmethod
    def _expired(draft: ClipDraft, now: int) -> bool:
        return not draft.retention_hold and draft.delete_after_ms <= now

    def restore_spooled(self) -> int:
        """기동할 때 임시 저장소에 남은 클립을 대기열로 되돌린다(내용은 읽지 않는다). 삭제 예정일이 지난 것은 지운다."""
        if self._spool is None:
            return 0
        now = self._clock.now_ms()
        self._sweep(now, verify_content=True)
        try:
            drafts = self._spool.drafts()
        except Exception as e:  # noqa: BLE001 - 임시 저장소 장애가 Tier 0 기동을 막지 않는다
            log.error("임시 저장소를 읽지 못했다: %s", type(e).__name__)
            return 0
        restored = 0
        for draft in drafts:
            if draft.clip_id in self._jobs:
                continue
            if self._expired(draft, now):
                self._discard_spooled(draft.clip_id)
                self.stats.expired += 1
                continue
            self._jobs[draft.clip_id] = _Job(draft, None, next_attempt_ms=now, spooled=True)
            restored += 1
        return restored

    def _sweep(self, now: int, verify_content: bool) -> None:
        if self._spool is None:
            return
        self._next_sweep_ms = now + self._retry.sweep_interval_ms
        try:
            self.stats.swept_files += self._spool.sweep(verify_content)
        except Exception as e:  # noqa: BLE001 - 정리 실패가 Tier 0 루프를 막지 않는다
            log.error("임시 저장소를 정리하지 못했다: %s", type(e).__name__)

    def raise_retention_for_event(self, event_id: str, resume_count: int, severity: str) -> None:
        """그 위험 구간(event_id, resume_count)의 대기·임시 저장·보낸 클립 모두에 보존 표시를 붙인다(재기동 뒤 상향 포함)."""
        ids = [cid for cid, j in self._jobs.items() if j.draft.event_id == event_id and j.draft.resume_count == resume_count]
        ids += [cid for cid, d in self._sent.items() if d.event_id == event_id and d.resume_count == resume_count]
        if ids:
            self.raise_retention(ids, severity)

    def raise_retention(self, clip_ids: list[str], severity: str) -> None:
        """이벤트가 CRITICAL 로 올랐다: 보존 표시를 붙인다. 보내기 전 기록은 그 값을 올리고, 보낸 기록은 추가 기록을 낸다."""
        now = self._clock.now_ms()
        for cid in clip_ids:
            job = self._jobs.get(cid)
            if job is not None:
                if job.draft.retention_hold:
                    continue
                job.draft = replace(job.draft, retention_hold=True)
                if job.record is not None:
                    job.record = replace(job.record, draft=job.draft)
                if job.spooled and self._spool is not None:
                    try:
                        self._spool.update_draft(job.draft)
                    except Exception as e:  # noqa: BLE001 - 메모리 값은 올렸다
                        log.error("임시 저장 클립 %s 의 보존 표시를 쓰지 못했다: %s", cid, type(e).__name__)
                continue
            draft = self._sent.get(cid)
            if draft is None or draft.retention_hold:
                continue
            self._sent[cid] = replace(draft, retention_hold=True)
            self._updates.append(ClipRetentionUpdate(cid, draft.event_id, draft.gateway_id, draft.resume_count,
                                                     severity, True, RETENTION_REASON_CRITICAL, now))

    def _discard_spooled(self, clip_id: str) -> None:
        if self._spool is None:
            return
        try:
            self._spool.delete(clip_id)
        except Exception as e:  # noqa: BLE001
            log.error("임시 저장 클립 %s 를 지우지 못했다: %s", clip_id, type(e).__name__)

    def _spool_job(self, job: _Job) -> bool:
        """임시 저장소에 두고 메모리에서 내용을 내린다. 이미 저장했으면 내리기만 한다."""
        if not job.spooled:
            if self._spool is None or job.content is None:
                return False
            try:
                self._spool.save(job.content, job.draft)
            except Exception as e:  # noqa: BLE001
                log.error("클립 %s 를 Network Volume 에 저장하지 못했다: %s", job.draft.clip_id, type(e).__name__)
                return False
            job.spooled = True
            self.stats.spooled += 1
        job.content = None
        return True

    def poll(self) -> None:
        now = self._clock.now_ms()
        if self._spool is not None and (self._next_sweep_ms is None or now >= self._next_sweep_ms):
            self._sweep(now, verify_content=False)
        for job in list(self._jobs.values()):
            if job.record is None and self._expired(job.draft, now):
                self._drop(job, expired=True)
                continue
            if job.next_attempt_ms > now:
                continue
            try:
                self._attempt(job, now)
            except Exception as e:  # noqa: BLE001 - 한 클립의 실패가 Tier 0 루프로 새지 않는다
                self._fail(job, now, type(e).__name__)
        while self._updates:
            try:
                self._sink.emit_retention(self._updates[0])
            except Exception as e:  # noqa: BLE001 - 다음 poll 에 다시 낸다
                log.error("클립 보존 표시 기록을 내지 못했다: %s", type(e).__name__)
                break
            self._updates.pop(0)
            self.stats.retention_updates += 1

    def _drop(self, job: _Job, *, expired: bool = False) -> None:
        self._jobs.pop(job.draft.clip_id, None)
        if job.spooled:
            self._discard_spooled(job.draft.clip_id)
        if expired:
            self.stats.expired += 1
        else:
            self.stats.dropped += 1

    def _attempt(self, job: _Job, now: int) -> None:
        if job.record is None:
            d = job.draft
            job.attempts += 1
            try:
                clip = job.content if job.content is not None else self._load(job)
                signed = self._signer.sign(UploadSignRequest(UPLOAD_PURPOSE, d.clip_id, d.event_id, d.camera_id,
                                                             clip.content_type, clip.size_bytes, clip.sha256))
                self._transport.put(signed, clip.content, clip.content_type)
            except SignerUnavailable:
                # EC2 단절: 먼저 Network Volume 에 저장하고 복구를 기다린다. 저장했으면 시도 상한에 넣지 않는다.
                if self._spool_job(job):
                    job.outages += 1
                    job.next_attempt_ms = now + self._retry.backoff_ms(job.outages)
                    return
                self._fail(job, now, "EC2 단절")
                return
            except UploadError as e:
                self._fail(job, now, str(e))
                return
            except Exception as e:  # noqa: BLE001 - 서명 응답 파싱 오류·HTTP 예외 등. 종류 이름만 남긴다
                self._fail(job, now, type(e).__name__)
                return
            job.record = MediaClipRecord(job.draft, signed.object_key, now, job.spooled, job.attempts)
            job.content = None
        try:
            self._sink.emit(job.record)
        except Exception as e:  # noqa: BLE001 - 기록 출구 실패는 다음 poll 에 기록만 다시 낸다(다시 올리지 않는다)
            log.error("클립 %s 기록을 내지 못했다: %s", job.draft.clip_id, type(e).__name__)
            job.next_attempt_ms = now + self._retry.initial_backoff_ms
            return
        self._jobs.pop(job.draft.clip_id, None)
        self._sent[job.draft.clip_id] = job.draft
        while len(self._sent) > self._retry.max_sent_memory:
            self._sent.popitem(last=False)
        self.stats.uploaded += 1
        if job.spooled:
            self._discard_spooled(job.draft.clip_id)

    def _load(self, job: _Job) -> ExportClip:
        if self._spool is None:
            raise RuntimeError("임시 저장소 없이 내용이 없는 작업")
        return self._spool.load(job.draft.clip_id)

    def _fail(self, job: _Job, now: int, reason: str) -> None:
        job.failures += 1
        self.stats.failures += 1
        if job.failures >= self._retry.max_attempts:
            if self._spool_job(job):
                if not job.parked:
                    log.error("클립 %s 업로드가 %d 번 실패해 Network Volume 에 두고 %d ms 마다 다시 시도한다: %s",
                              job.draft.clip_id, job.failures, self._retry.parked_retry_ms, reason)
                job.parked = True
                job.next_attempt_ms = now + self._retry.parked_retry_ms
            else:
                log.error("클립 %s 업로드가 %d 번 실패해 버렸다: %s", job.draft.clip_id, job.failures, reason)
                self._drop(job)
            return
        job.next_attempt_ms = now + self._retry.backoff_ms(job.failures)
