"""Tier 0 → 아바타 로컬 IPC. 최신 한 건의 참조만 넘기고 파일 I/O는 daemon에서 한다.

NPZ(JSON UTF-8 metadata + camera별 uint8 BGR 배열), 임시 파일 → os.replace.
읽기는 완성된 이전/새 파일 중 하나다. 실패·느린 디스크·수신자 부재는 Tier 0를 막지 않는다.
Frame.image는 넘긴 뒤 수정하지 않는 소유권 규약이다. 영상은 이 최신 파일 한 건만 유지한다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
import uuid

import numpy as np

from .channel_timing import ChannelTiming

DEFAULT_SHM_DIR = Path("/dev/shm")
DEFAULT_SHM_FILENAME = "simai-avatar-input.npz"
DEFAULT_STATE_RELPATH = Path("state/avatar-input.npz")
DEFAULT_PERIOD_S = 0.2
PUBLICATION_FORMAT = "simai.avatar-publication@1"


def publication_path(path: Path) -> Path:
    return path.with_name(path.name + ".published.json")


def avatar_channel_path(
    env: Mapping[str, str] | None = None,
    root: Path | str | None = None,
    *,
    shm_dir: Path | None = None,
) -> Path:
    """아바타 입력 공유 파일 경로: SIMAI_AVATAR_CHANNEL → /dev/shm(tmpfs) → $SIMAI_ROOT/state/avatar-input.npz."""
    env_map = os.environ if env is None else env
    explicit = env_map.get("SIMAI_AVATAR_CHANNEL")
    if explicit:
        return Path(explicit)
    shm = (DEFAULT_SHM_DIR if os.name == "posix" else None) if shm_dir is None else shm_dir
    if shm is not None and shm.is_dir() and os.access(shm, os.W_OK):
        return shm / DEFAULT_SHM_FILENAME
    base = Path(root) if root is not None else Path(env_map.get("SIMAI_ROOT") or "/workspace/simai")
    return base / DEFAULT_STATE_RELPATH


class LatestChannelWriter:
    def __init__(self, path: Path, *, period_s: float = DEFAULT_PERIOD_S, clock_ms=None,
                 timing: ChannelTiming | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending = None
        self.period_s = period_s
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.timing = timing or ChannelTiming()
        self.dropped = self.errors = 0
        self._thread = threading.Thread(target=self._run, name="avatar-ipc", daemon=True)
        self._thread.start()

    def offer(self, metadata, frames) -> bool:
        if not self._lock.acquire(blocking=False):
            self.dropped += 1
            return False
        try:
            self.dropped += int(self._pending is not None)
            self._pending = ({**metadata, "produced_at_ms": self._clock_ms()}, frames, self.timing.clock())
        finally:
            self._lock.release()
        self._wake.set()
        return True

    def _write(self, metadata, frames) -> None:
        started = self.timing.clock()
        image_arrays = {}
        refs = {}
        for i, (cid, frame) in enumerate(frames.items()):
            if frame.image is None:
                continue
            key = f"image_{i}"
            image = np.asarray(frame.image)
            if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                continue
            image_arrays[key] = image
            refs[cid] = {"array": key, "capture_time_ms": frame.estimated_capture_time,
                         "frame_ref": dict(frame.frame_ref)}
        publication_id = uuid.uuid4().hex
        doc = {"format": "simai.avatar-input@1", **metadata, "frames": refs,
               "publication_id": publication_id}
        doc.pop("published_at_ms", None)  # 게시 완료 뒤의 시각은 아래 완료 표식에만 기록한다.
        arrays = {
            "metadata": np.frombuffer(json.dumps(doc, allow_nan=False).encode("utf-8"), dtype=np.uint8),
            **image_arrays,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(f".{os.getpid()}.tmp")
        published_path = publication_path(self.path)
        published_temp = published_path.with_suffix(f".{os.getpid()}.tmp")
        self.timing.record("write_prepare", (self.timing.clock() - started) * 1000)
        try:
            with self.timing.measure("write_npz"):
                with temp.open("wb") as f:
                    np.savez(f, **arrays)
            with self.timing.measure("write_replace"):
                os.replace(temp, self.path)
                published_at_ms = self._clock_ms()  # NPZ 교체 성공 직후의 완료 관측 시각.
            with self.timing.measure("write_publication"):
                publication = {"format": PUBLICATION_FORMAT, "publication_id": publication_id,
                               "published_at_ms": published_at_ms}
                published_temp.write_text(json.dumps(publication), encoding="utf-8")
                os.replace(published_temp, published_path)
        finally:
            temp.unlink(missing_ok=True)
            published_temp.unlink(missing_ok=True)

    def _run(self) -> None:
        report_at = time.monotonic() + 60
        while not self._stop.is_set():
            self._wake.wait(0.2)
            self._wake.clear()
            with self._lock:
                item, self._pending = self._pending, None
            if item is None:
                continue
            started = time.monotonic()
            metadata, frames, offered_at = item
            self.timing.record("offer_to_write_start", (self.timing.clock() - offered_at) * 1000)
            try:
                with self.timing.measure("write_total"):
                    self._write(metadata, frames)
                self.timing.record("offer_to_write_complete", (self.timing.clock() - offered_at) * 1000)
            except Exception:  # 파일 실패는 이 보조 경로에서만 센다
                self.errors += 1
            if time.monotonic() >= report_at:
                logging.getLogger(__name__).info("공유 채널 계측: %s", self.timing.summary())
                report_at = time.monotonic() + 60
            remaining = self.period_s - (time.monotonic() - started)
            if remaining > 0:
                self._stop.wait(remaining)  # 최대 5Hz, 중간 참조는 최신 한 건으로 합친다

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=0.2)


class LatestChannelReader:
    supports_predicate = True

    def __init__(self, path: Path, *, timing: ChannelTiming | None = None,
                 require_publication: bool = False) -> None:
        self.path = path
        self.timing = timing or ChannelTiming()
        self.require_publication = require_publication

    def latest(self, *, predicate: Callable[[dict[str, Any]], bool] | None = None):
        try:
            # replace 직후에도 하나의 열린 파일에서 metadata를 먼저 확인하고 필요한 그림만 읽는다.
            with self.timing.measure("read_total"), self.path.open("rb") as f:
                with self.timing.measure("read_metadata"):
                    z = np.load(f, allow_pickle=False)
                    try:
                        doc = json.loads(z["metadata"].tobytes())
                        if not isinstance(doc, dict):
                            raise ValueError("공유 metadata는 JSON object여야 한다")
                        doc.pop("published_at_ms", None)  # NPZ 내장 값은 완료 표식으로 검증된 시각이 아니다.
                        if self.require_publication and "publication_id" in doc:
                            publication = json.loads(publication_path(self.path).read_text(encoding="utf-8"))
                            stamp = publication.get("published_at_ms") if isinstance(publication, dict) else None
                            if (not isinstance(publication, dict)
                                    or publication.get("format") != PUBLICATION_FORMAT
                                    or not isinstance(doc["publication_id"], str) or not doc["publication_id"]
                                    or publication.get("publication_id") != doc["publication_id"]
                                    or not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0):
                                self.timing.record("publication_unavailable")
                                z.close()
                                return None
                            doc["published_at_ms"] = stamp
                    except Exception:
                        z.close()
                        raise
                with z:
                    return self._selected(z, doc, predicate)
        except (OSError, ValueError, KeyError, EOFError):
            self.timing.record("read_error")
            return None

    def _selected(self, z, doc, predicate):
        if doc.get("format") != "simai.avatar-input@1":
            return None
        with self.timing.measure("read_select"):
            selected = predicate is None or predicate(doc)
        if not selected:
            self.timing.record("images_skipped")
            return doc, {}
        with self.timing.measure("read_images"):
            images = {cid: z[ref["array"]] for cid, ref in doc["frames"].items()}
        self.timing.record("images_loaded")
        return doc, images
