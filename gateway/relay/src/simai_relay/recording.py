"""관제 중계 동시 녹화: Tier 0 로 보내는 I0 기록(codec_info, frame_header + 압축 프레임)을 그대로 세그먼트로 저장 (S15P31A101-554).

- 추가 RTSP 세션을 열지 않는다(명세 3.3, C110 동시 2세션 한도). CameraRelay 가 만든 I0CodecInfo·I0Frame 을
  RecordingSink 가 원래 CameraSink(Tier 0 TLS 전송 등)로 먼저 넘긴 뒤 카메라별 녹화 큐에 넣는다.
- 송신을 절대 막지 않는다: 녹화는 카메라마다 별도 스레드와 제한 큐(프레임 수·바이트 상한)로 돌며, 큐가 가득 차면
  녹화 프레임만 다음 키프레임까지 버리고 버린 수를 상태·로그에 남긴다.
- 디스크 여유 공간이 하한(기본 20 GB) 아래로 내려가면 경고를 남기고 녹화만 중단하며, 중계 송신은 계속한다.
- 재접속으로 stream_epoch 가 바뀔 때마다 새 세그먼트(epoch_<stream_epoch>.{h264,frames.jsonl,codec_info.json})를 연다.
- 종료(정상 종료·Ctrl+C) 시 session.json 을 마감하고 simai_recorder.manifest.write_manifest 로 manifest.json 을 만든 뒤
  validate_manifest 로 계약 스키마(contracts/data/recording_manifest.schema.json)를 검증한다.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
import tomllib
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from simai_recorder.annexb import has_parameter_sets
from simai_recorder.clock import Clock
from simai_recorder.config import REPO_ROOT, ConfigError, default_session_id
from simai_recorder.manifest import validate_consent, validate_manifest, validate_storage_uri, write_manifest
from simai_recorder.recorder import EVENTS_FILE, SESSION_FILE, CameraSpec
from simai_recorder.segment import SegmentWriter

from simai_relay import __version__
from simai_relay.sink import CameraSink, I0CodecInfo, I0Frame, SinkFactory

log = logging.getLogger("simai_relay.recording")

DEFAULT_MIN_FREE_GB = 20.0
DEFAULT_MIN_FREE_BYTES = int(DEFAULT_MIN_FREE_GB * (1024**3))
DEFAULT_MAX_QUEUE_FRAMES = 150  # 카메라당 25fps 약 6초
DEFAULT_MAX_QUEUE_BYTES = 16 * 1024 * 1024  # 키프레임 여러 장
DEFAULT_FLUSH_TIMEOUT_S = 5.0
DEFAULT_STORAGE_URI = "volume://AP-JP-1/simai/recordings"
DEFAULT_SCENARIO = "live_relay"
DEFAULT_DESCRIPTION = "관제 중계 동시 녹화"
DEFAULT_RETENTION_UNTIL = "2027-01-31"
# 관제 중계 녹화의 기본 동의 설정: 내부 개발·테스트 용도(purpose = "dev_test", 우연히 찍힌 사람 포함 가능, 보관 기한 필수)
DEFAULT_CONSENT: dict[str, Any] = {
    "contains_people": True,
    "purpose": "dev_test",
    "retention_until": DEFAULT_RETENTION_UNTIL,
}
_SESSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_POLL_S = 0.2

WriterFactory = Callable[[Path, dict[str, Any], bool], SegmentWriter]
FreeBytesFn = Callable[[Path], int]


def _default_free_bytes(path: Path) -> int:
    target = path if path.exists() else path.parent
    return shutil.disk_usage(target).free


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def session_id_for(record_dir: Path, clock: Clock, scenario: str = DEFAULT_SCENARIO) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", record_dir.name.lower()).strip("-")
    if slug and _SESSION_ID_RE.fullmatch(slug):
        return slug
    return default_session_id(clock, scenario)


@dataclass(frozen=True)
class RecordingMetadata:
    session_id: str
    scenario: str = DEFAULT_SCENARIO
    description: str = DEFAULT_DESCRIPTION
    storage_uri: str = DEFAULT_STORAGE_URI
    calibration_version: str | None = None
    consent: dict[str, Any] | None = None
    notes: str = ""
    gateway_id: str = "laptop-01"


def load_recording_metadata(
    config_path: Path | None,
    record_dir: Path,
    cameras: Sequence[CameraSpec],
    clock: Clock,
    *,
    gateway_id: str = "laptop-01",
) -> RecordingMetadata:
    """중계 설정 TOML 에 [session]·[consent] 가 있으면 읽고, 없으면 관제 녹화 기본값(purpose=dev_test)을 쓴다."""
    data: dict[str, Any] = {}
    if config_path is not None and config_path.exists():
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    sess = data.get("session") if isinstance(data.get("session"), Mapping) else {}
    scenario = str(sess.get("scenario") or DEFAULT_SCENARIO).strip()
    if not scenario:
        raise ConfigError("[session] scenario 가 비어 있다")
    storage_uri = str(sess.get("storage_uri") or DEFAULT_STORAGE_URI).strip()
    if validate_storage_uri(storage_uri):
        raise ConfigError("[session] storage_uri 는 volume://<datacenter>/<path> 또는 s3:// 여야 한다")
    raw_session_id = sess.get("session_id")
    session_id = str(raw_session_id).strip() if raw_session_id else session_id_for(record_dir, clock, scenario)
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ConfigError(f"session_id {session_id!r}: 영문 소문자·숫자·- 만 쓴다")

    raw_consent = data.get("consent")
    if isinstance(raw_consent, Mapping):
        if raw_consent.get("contains_people") is False and "purpose" not in raw_consent:
            consent: dict[str, Any] = {
                "contains_people": False,
                "retention_until": DEFAULT_RETENTION_UNTIL,
                **dict(raw_consent),
            }
        else:
            consent = {**DEFAULT_CONSENT, **dict(raw_consent)}
    else:
        consent = dict(DEFAULT_CONSENT)

    kinds = sorted({str(c.source.describe()["kind"]).upper() for c in cameras})
    if errors := validate_consent(consent, kinds):
        raise ConfigError("; ".join(errors))

    return RecordingMetadata(
        session_id=session_id,
        scenario=scenario,
        description=str(sess.get("description", DEFAULT_DESCRIPTION)),
        storage_uri=storage_uri,
        calibration_version=str(sess["calibration_version"]) if sess.get("calibration_version") else None,
        consent=consent,
        notes=str(sess.get("notes", "")),
        gateway_id=gateway_id,
    )


class CameraRecorderWorker:
    """카메라 한 대의 비동기 녹화 큐와 쓰기 스레드."""

    def __init__(
        self,
        spec: CameraSpec,
        session_dir: Path,
        clock: Clock,
        recorder: RelayRecorder,
        *,
        max_queue_frames: int = DEFAULT_MAX_QUEUE_FRAMES,
        max_queue_bytes: int = DEFAULT_MAX_QUEUE_BYTES,
        writer_factory: WriterFactory = SegmentWriter,
    ) -> None:
        self.spec = spec
        self.camera_id = spec.camera_id
        self.camera_dir = session_dir / spec.camera_id
        self._clock = clock
        self._recorder = recorder
        self._max_frames = max_queue_frames
        self._max_bytes = max_queue_bytes
        self._writer_factory = writer_factory

        self._cond = threading.Condition()
        self._items: deque[tuple[str, Any]] = deque()
        self._queued_frames = 0
        self._queued_bytes = 0
        self._waiting_keyframe = True
        self._closing = False
        self._abort_drain = False

        self.recorded_frames = 0
        self.recorded_bytes = 0
        self.dropped_frames = 0
        self.dropped_overflow = 0
        self.dropped_disk_low = 0
        self.max_queue_depth = 0
        self.segments_count = 0
        self.error: str | None = None

        self._open_failures = 0
        self._session_error: str | None = None
        self._writer: SegmentWriter | None = None
        self._events: TextIO | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.camera_dir.mkdir(parents=True, exist_ok=False)
        self._events = (self.camera_dir / EVENTS_FILE).open("x", encoding="utf-8", newline="\n")
        self._thread = threading.Thread(target=self._run, name=f"relay-rec-{self.camera_id}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 수신 스레드 쪽 (비차단)

    def offer_codec(self, record: I0CodecInfo) -> None:
        with self._cond:
            if self._closing or self._recorder.disk_low:
                return
            self._waiting_keyframe = True
            self._items.append(("codec", record))
            self._cond.notify()

    def offer_frame(self, record: I0Frame) -> None:
        keyframe = bool(record.header.get("keyframe"))
        size = len(record.data)
        overflow_now = False
        dropped_total = 0
        with self._cond:
            if self._closing:
                return
            if self._recorder.disk_low:
                self.dropped_frames += 1
                self.dropped_disk_low += 1
                return
            if self._waiting_keyframe and not keyframe:
                self.dropped_frames += 1
                return
            if self._queued_frames >= self._max_frames or self._queued_bytes + size > self._max_bytes:
                was_waiting = self._waiting_keyframe
                self._waiting_keyframe = True
                self.dropped_frames += 1
                self.dropped_overflow += 1
                dropped_total = self.dropped_frames
                overflow_now = (not was_waiting) or self.dropped_overflow == 1
            else:
                self._waiting_keyframe = False
                self._items.append(("frame", record))
                self._queued_frames += 1
                self._queued_bytes += size
                self.max_queue_depth = max(self.max_queue_depth, self._queued_frames)
                self._cond.notify()
        if overflow_now:
            log.warning(
                "%s 녹화 큐가 가득 차 녹화 프레임을 버린다 (다음 키프레임까지 건너뜀, 누적 버림 %d)",
                self.camera_id,
                dropped_total,
            )

    def set_session_outcome(self, open_failures: int = 0, error: str | None = None) -> None:
        with self._cond:
            self._open_failures = max(0, int(open_failures))
            self._session_error = error

    def stop(self) -> None:
        with self._cond:
            self._closing = True
            self._cond.notify_all()

    def abort_drain(self) -> None:
        with self._cond:
            if self._queued_frames > 0:
                self.dropped_frames += self._queued_frames
                self.dropped_overflow += self._queued_frames
            self._items.clear()
            self._queued_frames = 0
            self._queued_bytes = 0
            self._abort_drain = True
            self._closing = True
            self._cond.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    # ------------------------------------------------------------ 녹화 스레드 쪽

    def _event(self, event: str, **fields: Any) -> None:
        if self._events is None:
            return
        row = {"t_ms": self._clock.now_ms(), "camera_id": self.camera_id, "event": event, **fields}
        self._events.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._events.flush()

    def _close_writer(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            finally:
                self._writer = None

    def _run(self) -> None:
        try:
            self._loop()
        except BaseException as e:
            self.error = f"{type(e).__name__}: {e}"
            self._event("error", detail=self.error)
            log.error("%s 녹화 쓰기 실패 (%s)", self.camera_id, type(e).__name__)
        finally:
            self._close_writer()
            with self._cond:
                open_failures = self._open_failures
                sess_err = self._session_error
            for _ in range(open_failures):
                self._event("open_failed", detail="relay_open_failed")
            if sess_err and not self.error:
                self._event("error", detail=sess_err)
            if self.dropped_frames > 0:
                self._event(
                    "recording_dropped",
                    dropped_frames=self.dropped_frames,
                    dropped_overflow=self.dropped_overflow,
                    dropped_disk_low=self.dropped_disk_low,
                )
            self._event("closed", recorded_frames=self.recorded_frames, dropped_frames=self.dropped_frames)
            if self._events is not None:
                self._events.close()
                self._events = None

    def _loop(self) -> None:
        codec: dict[str, Any] | None = None
        dropped_before_keyframe = 0
        had_epoch = False
        while True:
            with self._cond:
                while not self._items and not self._closing:
                    self._cond.wait(timeout=_POLL_S)
                if self._abort_drain or not self._items:
                    break
                kind, item = self._items.popleft()
                if kind == "frame":
                    self._queued_frames -= 1
                    self._queued_bytes -= len(item.data)
            if kind == "codec":
                rec_codec: I0CodecInfo = item
                if self._writer is not None:
                    self._event("stopped", stream_epoch=codec["stream_epoch"] if codec else 0,
                                frames=self._writer.frame_count)
                self._close_writer()
                if had_epoch and codec is not None:
                    self._event("disconnected", stream_epoch=codec["stream_epoch"], detail="stream_epoch_changed")
                codec = rec_codec.codec_info
                dropped_before_keyframe = rec_codec.dropped_before_keyframe
                had_epoch = True
                self._event(
                    "connected",
                    stream_epoch=codec["stream_epoch"],
                    width=codec["width"],
                    height=codec["height"],
                    nominal_fps=codec.get("nominal_fps"),
                )
                continue

            record: I0Frame = item
            if not self._recorder.check_disk():
                if self._writer is not None and codec is not None:
                    self._event("stopped", stream_epoch=codec["stream_epoch"], frames=self._writer.frame_count)
                self._close_writer()
                with self._cond:
                    discarded = 1 + self._queued_frames
                    self.dropped_frames += discarded
                    self.dropped_disk_low += discarded
                    self._items.clear()
                    self._queued_frames = 0
                    self._queued_bytes = 0
                    self._waiting_keyframe = True
                self._event("disk_low_stopped")
                continue

            if codec is None or record.header.get("stream_epoch") != codec["stream_epoch"]:
                with self._cond:
                    self.dropped_frames += 1
                continue

            if self._writer is None:
                if not record.header.get("keyframe"):
                    with self._cond:
                        self.dropped_frames += 1
                    continue
                prefix = not has_parameter_sets(record.data)
                self._writer = self._writer_factory(self.camera_dir, codec, prefix)
                # 관제를 유지해도 종료된 작업을 올릴 수 있게 키프레임에서 로컬 파일만 나눈다.
                self._writer.rotate_after_ms = 30_000
                self.segments_count += 1
                self._event(
                    "segment_started",
                    stream_epoch=codec["stream_epoch"],
                    dropped_before_keyframe=dropped_before_keyframe,
                )
                dropped_before_keyframe = 0

            header = record.header
            if header["frame_sequence"] != self._writer.frame_count:
                header = {**header, "frame_sequence": self._writer.frame_count}
            self._writer.write(header, record.data)
            with self._cond:
                self.recorded_frames += 1
                self.recorded_bytes += len(record.data)

        if self._writer is not None and codec is not None:
            self._event("stopped", stream_epoch=codec["stream_epoch"], frames=self._writer.frame_count)

    # ------------------------------------------------------------ 상태

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            return {
                "camera_id": self.camera_id,
                "active": not self._closing and not self._recorder.disk_low and self.error is None,
                "recorded_frames": self.recorded_frames,
                "recorded_bytes": self.recorded_bytes,
                "dropped_frames": self.dropped_frames,
                "dropped_overflow": self.dropped_overflow,
                "dropped_disk_low": self.dropped_disk_low,
                "queue_depth": self._queued_frames,
                "queue_max_depth": self.max_queue_depth,
                "segments": self.segments_count,
                "error": self.error,
            }


class RecordingSink:
    """원래 CameraSink(Tier 0 전송·개수 세기)를 감싸 같은 I0 기록을 비동기 녹화 큐에도 넘긴다. 원래 전송을 먼저 한다."""

    def __init__(self, inner: CameraSink, worker: CameraRecorderWorker) -> None:
        self.inner = inner
        self._worker = worker

    def send_codec_info(self, record: I0CodecInfo) -> None:
        self.inner.send_codec_info(record)
        try:
            self._worker.offer_codec(record)
        except Exception as e:  # noqa: BLE001 - 녹화 쪽 예외가 중계 수신·송신을 멈추지 않게
            log.warning("%s 녹화 codec 큐 전달 실패 (%s)", self._worker.camera_id, type(e).__name__)

    def send_frame(self, record: I0Frame) -> None:
        self.inner.send_frame(record)
        try:
            self._worker.offer_frame(record)
        except Exception as e:  # noqa: BLE001 - 녹화 쪽 예외가 중계 수신·송신을 멈추지 않게
            log.warning("%s 녹화 프레임 큐 전달 실패 (%s)", self._worker.camera_id, type(e).__name__)

    def close(self) -> None:
        self.inner.close()


class RelayRecorder:
    """관제 중계 세션 하나의 동시 녹화 관리자."""

    def __init__(
        self,
        record_dir: Path,
        cameras: Sequence[CameraSpec],
        clock: Clock,
        *,
        metadata: RecordingMetadata | None = None,
        config_path: Path | None = None,
        gateway_id: str = "laptop-01",
        requested_duration_s: float | None = None,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        max_queue_frames: int = DEFAULT_MAX_QUEUE_FRAMES,
        max_queue_bytes: int = DEFAULT_MAX_QUEUE_BYTES,
        flush_timeout_s: float = DEFAULT_FLUSH_TIMEOUT_S,
        writer_factory: WriterFactory = SegmentWriter,
        free_bytes_fn: FreeBytesFn = _default_free_bytes,
    ) -> None:
        ids = [c.camera_id for c in cameras]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("cameras 는 비어 있지 않고 camera_id 가 겹치지 않아야 한다")
        if max_queue_frames < 1 or max_queue_bytes < 1:
            raise ValueError("녹화 큐 상한은 1 이상이다")
        if min_free_bytes < 0:
            raise ValueError("디스크 여유 하한은 0 이상이다")
        self.record_dir = Path(record_dir)
        if (self.record_dir / SESSION_FILE).exists() or (self.record_dir / "manifest.json").exists():
            raise FileExistsError(f"덮어쓰지 않는다: {self.record_dir}")
        for c in cameras:
            if (self.record_dir / c.camera_id).exists():
                raise FileExistsError(f"덮어쓰지 않는다: {self.record_dir / c.camera_id}")

        self._cameras = list(cameras)
        self._clock = clock
        self.metadata = metadata or load_recording_metadata(
            config_path, self.record_dir, self._cameras, clock, gateway_id=gateway_id
        )
        self.session_id = self.metadata.session_id
        self._requested_duration_s = requested_duration_s
        self.min_free_bytes = min_free_bytes
        self._max_frames = max_queue_frames
        self._max_bytes = max_queue_bytes
        self._flush_timeout_s = flush_timeout_s
        self._writer_factory = writer_factory
        self._free_bytes_fn = free_bytes_fn

        self._lock = threading.Lock()
        self.disk_low = False
        self.last_free_bytes: int | None = None
        self.started_at_ms: int | None = None
        self.ended_at_ms: int | None = None
        self._started = False
        self._closed = False
        self._session_doc: dict[str, Any] = {}
        self.manifest: dict[str, Any] | None = None
        self.manifest_errors: list[str] = []

        self._workers: dict[str, CameraRecorderWorker] = {
            c.camera_id: CameraRecorderWorker(
                c,
                self.record_dir,
                clock,
                self,
                max_queue_frames=max_queue_frames,
                max_queue_bytes=max_queue_bytes,
                writer_factory=writer_factory,
            )
            for c in self._cameras
        }

    def check_disk(self) -> bool:
        if self.disk_low:
            return False
        try:
            free = int(self._free_bytes_fn(self.record_dir))
        except OSError as e:
            log.warning("디스크 여유 공간 확인 실패 (%s)", type(e).__name__)
            return True
        self.last_free_bytes = free
        if free < self.min_free_bytes:
            with self._lock:
                first = not self.disk_low
                self.disk_low = True
            if first:
                log.warning(
                    "디스크 여유 공간이 하한 아래라 녹화를 중단한다 (여유 %.2f GB < 하한 %.2f GB, 폴더 %s). 중계 송신은 계속한다",
                    free / (1024**3),
                    self.min_free_bytes / (1024**3),
                    self.record_dir,
                )
            return False
        return True

    def start(self) -> None:
        if self._started:
            raise RuntimeError("이미 녹화를 시작했다")
        self._started = True
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.started_at_ms = self._clock.now_ms()
        self._session_doc = {
            "session_id": self.metadata.session_id,
            "scenario": self.metadata.scenario,
            "description": self.metadata.description,
            "calibration_version": self.metadata.calibration_version,
            "consent": self.metadata.consent,
            "storage_uri": self.metadata.storage_uri,
            "notes": self.metadata.notes,
            "recorded_by": {
                "tool": "gateway/relay",
                "version": __version__,
                "git_commit": _git_commit(),
                "gateway_id": self.metadata.gateway_id,
            },
            "requested_duration_s": self._requested_duration_s,
            "started_at_ms": self.started_at_ms,
            "ended_at_ms": None,
            "cameras": [
                {
                    "camera_id": c.camera_id,
                    "source": c.source.describe(),
                    "firmware": c.firmware,
                    "capture_time_model": c.capture_time.to_dict(),
                }
                for c in self._cameras
            ],
        }
        _write_json(self.record_dir / SESSION_FILE, self._session_doc)
        self.check_disk()
        for w in self._workers.values():
            w.start()
        log.info("동시 녹화 시작 → %s (최소 여유 %.1f GB, 큐 상한 %d프레임)",
                 self.record_dir, self.min_free_bytes / (1024**3), self._max_frames)

    def sink_for(self, camera_id: str, inner: CameraSink) -> RecordingSink:
        return RecordingSink(inner, self._workers[camera_id])

    @property
    def active(self) -> bool:
        return self._started and not self._closed and not self.disk_low

    def camera_snapshot(self, camera_id: str) -> dict[str, Any]:
        return self._workers[camera_id].snapshot()

    def snapshot(self) -> dict[str, Any]:
        cams = {cid: w.snapshot() for cid, w in self._workers.items()}
        return {
            "record_dir": str(self.record_dir),
            "session_id": self.session_id,
            "active": self.active,
            "disk_low": self.disk_low,
            "free_bytes": self.last_free_bytes,
            "min_free_bytes": self.min_free_bytes,
            "recorded_frames": sum(c["recorded_frames"] for c in cams.values()),
            "recorded_bytes": sum(c["recorded_bytes"] for c in cams.values()),
            "dropped_frames": sum(c["dropped_frames"] for c in cams.values()),
            "dropped_overflow": sum(c["dropped_overflow"] for c in cams.values()),
            "dropped_disk_low": sum(c["dropped_disk_low"] for c in cams.values()),
            "queue_limit_frames": self._max_frames,
            "queue_limit_bytes": self._max_bytes,
            "queue_max_depth": max((c["queue_max_depth"] for c in cams.values()), default=0),
            "cameras": cams,
        }

    def close(self, session: Any = None, *, flush_timeout_s: float | None = None) -> dict[str, Any]:
        if self._closed:
            assert self.manifest is not None
            return self.manifest
        if not self._started:
            self.start()
        if session is not None:
            snaps = {s["camera_id"]: s for s in session.snapshots()}
            errs = session.errors()
            for cid, w in self._workers.items():
                w.set_session_outcome(
                    open_failures=snaps.get(cid, {}).get("open_failures", 0),
                    error=errs.get(cid),
                )
        for w in self._workers.values():
            w.stop()
        timeout = self._flush_timeout_s if flush_timeout_s is None else flush_timeout_s
        deadline = time.monotonic() + timeout
        for w in self._workers.values():
            left = max(0.0, deadline - time.monotonic())
            if not w.join(timeout=left):
                w.abort_drain()
                w.join(timeout=2.0)

        self._closed = True
        now_ms = self._clock.now_ms()
        self.ended_at_ms = max(now_ms, self.started_at_ms or now_ms)
        self._session_doc["ended_at_ms"] = self.ended_at_ms
        self._session_doc["recording_stats"] = self.snapshot()
        _write_json(self.record_dir / SESSION_FILE, self._session_doc)

        self.manifest = write_manifest(self.record_dir)
        self.manifest_errors = validate_manifest(self.manifest)
        snap = self.snapshot()
        if self.manifest_errors:
            log.error("녹화 manifest 검증 실패: %s", "; ".join(self.manifest_errors))
        else:
            log.info(
                "동시 녹화 완료: %s (저장 프레임 %d, 바이트 %d, 버린 프레임 %d)",
                self.record_dir / "manifest.json",
                snap["recorded_frames"],
                snap["recorded_bytes"],
                snap["dropped_frames"],
            )
        return self.manifest


def recording_factory(inner: SinkFactory, recorder: RelayRecorder) -> SinkFactory:
    return lambda camera_id: recorder.sink_for(camera_id, inner(camera_id))


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
