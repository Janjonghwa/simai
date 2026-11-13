"""녹화 폴더마다 작업 구간 조각을 올리고 이어 올리기 상태를 남긴다 (S15P31A101-668).

- 대상: 녹화 루트의 `live-*` 폴더(session.json 이 있는 것) 중 상태가 done 이 아닌 것. 첫 프레임이 14일보다 오래됐으면 올리지 않고 done(expired).
- 관제 중에도 종료 작업을 올린다. 진행 중 녹화는 닫힘 표시가 있는 파일만 읽고 다음 조회를 기다린다.
- 작업이 아직 안 끝났으면(recording_windows.open_work) 그 녹화는 pending 으로 두고 다음 실행 때 다시 묻는다.
- 조각마다: 임시 mp4(<녹화>/.upload/<segment_id>.mp4) → sha256 → 서명 → PUT(시도마다 새 서명) → 등록 → 상태 기록 → 임시 파일 삭제.
  같은 조각은 같은 segment_id 라 다시 올려도 EC2 가 DUPLICATE 로 받는다. 4xx 로 거절된 조각은 rejected 로 적고 다시 하지 않는다.
- 원본 녹화는 지우지 않는다(-554 보관 규칙 그대로).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simai_recorder.upload.client import DEFAULT_ATTEMPTS, Ec2Client, RequestRejected, TransientError
from simai_recorder.upload.mux import mux_chunk, sha256_hex
from simai_recorder.upload.plan import Chunk, Window, plan_chunks, read_tracks, recording_span

log = logging.getLogger("simai_recorder.upload")

STATE_FILE = "upload-state.json"
TEMP_DIR = ".upload"
RETENTION_MS = 14 * 24 * 3600 * 1000
#: EC2 구간 조회 한 번의 최대 범위(24시간)
QUERY_SPAN_MS = 24 * 3600 * 1000
BUSY_PREFIX = "관제"


def monitoring_active(mode_file: Path | None) -> bool:
    """관제(run) 중이면 True. 미리보기(preview)는 업로드를 막지 않는다(CCTV 원격 시청은 보는 사람이 있을 때만 올린다)."""
    if mode_file is None:
        return False
    try:
        return mode_file.read_text(encoding="utf-8-sig").strip().startswith(BUSY_PREFIX)
    except OSError:
        return False


@dataclass
class RunSummary:
    uploaded: int = 0
    duplicate: int = 0
    rejected: int = 0
    failed: int = 0
    deferred: bool = False
    pending_recordings: list[str] = field(default_factory=list)
    done_recordings: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.failed:
            return 1
        return 3 if self.deferred or self.pending_recordings else 0


class _State:
    """upload-state.json. 여러 스레드가 조각 결과를 적으므로 잠금을 쥐고 통째로 다시 쓴다."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("status", "pending")
        self.data.setdefault("segments", {})

    @property
    def done(self) -> bool:
        return self.data.get("status") == "done"

    def segment(self, segment_id: str) -> dict[str, Any] | None:
        return self.data["segments"].get(segment_id)

    def set_segment(self, chunk: Chunk, status: str, now_ms: int, code: str | None = None) -> None:
        with self.lock:
            entry: dict[str, Any] = {"camera_id": chunk.camera_id, "work_id": chunk.work_id, "start_ms": chunk.start_ms,
                                     "status": status, "at_ms": now_ms}
            if code:
                entry["code"] = code
            self.data["segments"][chunk.segment_id] = entry
            self._write()

    def finish(self, status: str, now_ms: int, note: str | None = None) -> None:
        with self.lock:
            self.data["status"] = status
            self.data["updated_at_ms"] = now_ms
            if note:
                self.data["note"] = note
            else:
                self.data.pop("note", None)
            self._write()

    def _write(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.path)


def find_recordings(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("live-") and (p / "session.json").exists())


def fetch_windows(client: Ec2Client, span: tuple[int, int]) -> tuple[list[Window], bool]:
    """녹화 범위를 24시간씩 나눠 묻고 합친다(작업 하나가 두 번 나오면 하나로)."""
    windows: dict[int, Window] = {}
    open_work = False
    start, end = span
    while start <= end:
        stop = min(end, start + QUERY_SPAN_MS)
        body = client.windows(start, stop)
        open_work = open_work or bool(body.get("open_work"))
        for w in body.get("windows") or []:
            windows[int(w["work_id"])] = Window(int(w["work_id"]), int(w["start_ms"]), int(w["end_ms"]))
        start = stop + 1
    return sorted(windows.values(), key=lambda w: (w.start_ms, w.work_id)), open_work


def segment_record(chunk: Chunk, size: int, sha_hex: str, object_key: str, created_ms: int, uploaded_ms: int,
                   attempts: int) -> dict[str, Any]:
    """계약 recording_segment 1.0.0."""
    return {
        "schema_version": "1.0.0",
        "segment_id": chunk.segment_id,
        "recording_id": chunk.recording_id,
        "work_id": chunk.work_id,
        "camera_id": chunk.camera_id,
        "stream_epochs": [chunk.stream_epoch],
        "segment_start_capture_ms": chunk.start_ms,
        "segment_end_capture_ms": chunk.end_ms,
        "frame_count": chunk.frame_count,
        "starts_with_keyframe": True,
        "has_gaps": chunk.has_gaps,
        "content_type": "video/mp4",
        "container": "mp4",
        "timestamp_mapping": "PTS_FROM_CAPTURE_MS",
        "size_bytes": size,
        "sha256": f"sha256:{sha_hex}",
        "face_redaction": "NONE",
        "created_at_ms": created_ms,
        "object_key": object_key,
        "uploaded_at_ms": uploaded_ms,
        "upload_attempts": attempts,
    }


class Uploader:
    def __init__(self, client: Ec2Client, now_ms: Callable[[], int], *, mode_file: Path | None = None, parallel: int = 2,
                 sleep: Callable[[float], None], attempts: int = DEFAULT_ATTEMPTS) -> None:
        self.client = client
        self.now_ms = now_ms
        self.mode_file = mode_file
        self.parallel = max(1, parallel)
        self.sleep = sleep
        self.attempts = attempts

    def busy(self) -> bool:
        return monitoring_active(self.mode_file)

    def run(self, root: Path, only: Path | None = None) -> RunSummary:
        summary = RunSummary()
        recordings = [only] if only else find_recordings(root)
        # 모든 녹화의 미확정 계획을 먼저 보고한다. 첫 녹화 완료만 보고 화면이 폴링을 멈추지 않게 한다.
        for recording in recordings:
            self.upload_recording(recording, summary, plan_only=True)
        if summary.failed:
            return summary
        for recording in recordings:
            self.upload_recording(recording, summary)
            if summary.deferred:
                break
        return summary

    def upload_recording(self, recording: Path, summary: RunSummary, *, plan_only: bool = False) -> None:
        state = _State(recording / STATE_FILE)
        if state.done:
            return
        name = recording.name
        try:
            session = json.loads((recording / "session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary.failed += 1
            return
        active = "ended_at_ms" in session and session["ended_at_ms"] is None
        tracks = []
        span = None
        # 열린 파일은 읽지 않는다. 빈 녹화도 세션 범위로 물어 확정된 0개와 미보고를 구분한다.
        if session.get("started_at_ms") is not None:
            span = (int(session["started_at_ms"]), int(session.get("ended_at_ms") or self.now_ms()))
        else:
            tracks = read_tracks(recording, closed_only=active or self.busy())
            span = recording_span(tracks)
        if span is None:
            if not active:
                state.finish("done", self.now_ms(), "프레임 없음")
                summary.done_recordings.append(name)
            return
        if span[0] <= self.now_ms() - RETENTION_MS:
            state.finish("done", self.now_ms(), "보관 기간(14일) 지남")
            summary.done_recordings.append(name)
            return
        try:
            windows, open_work = fetch_windows(self.client, span)
        except (TransientError, RequestRejected) as e:
            log.warning("%s: 작업 구간을 묻지 못했다(%s). 다음에 다시", name, e)
            summary.failed += 1
            summary.pending_recordings.append(name)
            return
        # 이미 확정한 작업은 조각 메타데이터를 다시 수 GB씩 읽지 않는다. 구간 시각도 키에 넣어 재개 작업과 구분한다.
        cached = state.data.setdefault("windows", {})

        def window_key(window: Window) -> str:
            return f"{window.work_id}:{window.start_ms}:{window.end_ms}"

        todo = [w for w in windows if window_key(w) not in cached]
        if todo and not tracks:
            tracks = read_tracks(recording, closed_only=active)
        planned = plan_chunks(name, tracks, todo)
        chunks = [c for c in planned
                  if (state.segment(c.segment_id) or {}).get("status") not in ("uploaded", "rejected")]
        failed_before = summary.failed

        cameras = {c["camera_id"] for c in session.get("cameras", [])}
        latest: dict[str, int] = {}
        for track in tracks:
            latest[track.camera_id] = max(latest.get(track.camera_id, -1), track.frames[-1].capture_ms)

        def report() -> None:
            for window in windows:
                saved = cached.get(window_key(window))
                if saved:
                    self.client.progress(window.work_id, name, saved["expected"], saved["failed"], True)
                    continue
                work_chunks = [c for c in planned if c.work_id == window.work_id]
                failed = sum((state.segment(c.segment_id) or {}).get("status") in ("failed", "rejected")
                             for c in work_chunks)
                sealed = not active or bool(cameras and all(latest.get(c, -1) >= window.end_ms for c in cameras))
                self.client.progress(window.work_id, name, len(work_chunks), failed, sealed and not plan_only)
                terminal = all((state.segment(c.segment_id) or {}).get("status") in ("uploaded", "rejected")
                               for c in work_chunks)
                if sealed and terminal and not plan_only:
                    cached[window_key(window)] = {"expected": len(work_chunks), "failed": failed}

        try:
            report()
            if plan_only:
                return
            last_report = self.now_ms()
            with ThreadPoolExecutor(max_workers=self.parallel) as pool:
                for _ in pool.map(lambda c: self._upload_chunk(recording, c, state, summary), chunks):
                    if self.now_ms() - last_report >= 10_000:
                        report()
                        last_report = self.now_ms()
            report()
        except (TransientError, RequestRejected) as e:
            log.warning("%s: 진행 보고 실패(%s)", name, e)
            summary.failed += 1
        if summary.failed > failed_before or open_work or active:
            state.finish("pending", self.now_ms(), "녹화 진행 중 또는 재시도 대기")
            summary.pending_recordings.append(name)
        else:
            state.finish("done", self.now_ms())
            summary.done_recordings.append(name)

    def _upload_chunk(self, recording: Path, chunk: Chunk, state: _State, summary: RunSummary) -> None:
        temp = recording / TEMP_DIR / f"{chunk.segment_id}.mp4"
        try:
            created = self.now_ms()
            mux_chunk(chunk, temp)
            size, sha_hex = temp.stat().st_size, sha256_hex(temp)
            object_key, attempts = self._put(chunk, temp, size, sha_hex)
            record = segment_record(chunk, size, sha_hex, object_key, created, self.now_ms(), attempts)
            result = self.client.register(record)
            status = result.get("status")
            state.set_segment(chunk, "uploaded", self.now_ms())
            if status == "DUPLICATE":
                summary.duplicate += 1
            else:
                summary.uploaded += 1
            log.info("올림 %s %s %d프레임 %.1f MB (%s)", chunk.camera_id, chunk.segment_id, chunk.frame_count, size / 1e6, status)
        except RequestRejected as e:
            state.set_segment(chunk, "rejected", self.now_ms(), e.code)
            summary.rejected += 1
            log.warning("거절 %s %s: %s", chunk.camera_id, chunk.segment_id, e)
        except (TransientError, OSError, ValueError, av_error()) as e:
            state.set_segment(chunk, "failed", self.now_ms())
            summary.failed += 1
            log.warning("실패 %s %s: %s (다음에 다시)", chunk.camera_id, chunk.segment_id, type(e).__name__)
        finally:
            temp.unlink(missing_ok=True)

    def _put(self, chunk: Chunk, temp: Path, size: int, sha_hex: str) -> tuple[str, int]:
        """시도마다 새 서명(5분, PUT 시작 때만 검사). 네트워크·5xx 는 1·2·4·8·16 s 뒤 다시."""
        for attempt in range(1, self.attempts + 1):
            signed = self.client.sign(chunk.segment_id, size, sha_hex)
            try:
                self.client.put(signed, temp)
                return str(signed["object_key"]), attempt
            except TransientError as e:
                if attempt == self.attempts:
                    raise
                delay = float(2 ** (attempt - 1))
                log.warning("S3 업로드 실패(%s), %.0f초 뒤 다시(%d/%d)", e, delay, attempt, self.attempts)
                self.sleep(delay)
        raise AssertionError("도달하지 않음")


def av_error() -> type[Exception]:
    """PyAV 포장 오류(깨진 세그먼트 등). 조각 하나만 실패로 두고 다음으로 간다."""
    import av

    return av.FFmpegError
