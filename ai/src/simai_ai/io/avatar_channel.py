"""Tier 0 → 아바타 로컬 IPC. 최신 한 건의 참조만 넘기고 파일 I/O는 daemon에서 한다.

NPZ(JSON UTF-8 metadata + camera별 uint8 BGR 배열), 임시 파일 → os.replace.
읽기는 완성된 이전/새 파일 중 하나다. 실패·느린 디스크·수신자 부재는 Tier 0를 막지 않는다.
Frame.image는 넘긴 뒤 수정하지 않는 소유권 규약이다. 영상은 이 최신 파일 한 건만 유지한다.
"""

from __future__ import annotations

import json
import os
import threading
from io import BytesIO
from pathlib import Path

import numpy as np


class LatestChannelWriter:
    def __init__(self, path: Path, *, period_s: float = 0.2) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending = None
        self.period_s = period_s
        self.dropped = self.errors = 0
        self._thread = threading.Thread(target=self._run, name="avatar-ipc", daemon=True)
        self._thread.start()

    def offer(self, metadata, frames) -> bool:
        if not self._lock.acquire(blocking=False):
            self.dropped += 1
            return False
        try:
            self.dropped += int(self._pending is not None)
            self._pending = (metadata, frames)
        finally:
            self._lock.release()
        self._wake.set()
        return True

    def _write(self, metadata, frames) -> None:
        arrays = {}
        refs = {}
        for i, (cid, frame) in enumerate(frames.items()):
            if frame.image is None:
                continue
            key = f"image_{i}"
            image = np.asarray(frame.image)
            if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                continue
            arrays[key] = image
            refs[cid] = {"array": key, "capture_time_ms": frame.estimated_capture_time,
                         "frame_ref": dict(frame.frame_ref)}
        doc = {"format": "simai.avatar-input@1", **metadata, "frames": refs}
        arrays["metadata"] = np.frombuffer(json.dumps(doc, allow_nan=False).encode("utf-8"), dtype=np.uint8)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(f".{os.getpid()}.tmp")
        try:
            with temp.open("wb") as f:
                np.savez(f, **arrays)
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.2)
            self._wake.clear()
            with self._lock:
                item, self._pending = self._pending, None
            if item is None:
                continue
            try:
                self._write(*item)
            except Exception:  # 파일 실패는 이 보조 경로에서만 센다
                self.errors += 1
            self._stop.wait(self.period_s)  # 최대 5Hz, 중간 참조는 최신 한 건으로 합친다

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=0.2)


class LatestChannelReader:
    def __init__(self, path: Path) -> None:
        self.path = path

    def latest(self):
        try:
            # replace 직후에도 하나의 열린 파일에서 metadata와 그림을 읽는다.
            with np.load(BytesIO(self.path.read_bytes()), allow_pickle=False) as z:
                doc = json.loads(z["metadata"].tobytes())
                if doc.get("format") != "simai.avatar-input@1":
                    return None
                return doc, {cid: z[ref["array"]] for cid, ref in doc["frames"].items()}
        except (OSError, ValueError, KeyError, EOFError):
            return None
