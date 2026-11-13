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
DEFAULT_CROP_PAD_RATIO = 0.35
DEFAULT_CROP_MIN_PAD_PX = 48
DEFAULT_RING_SLOTS = 3


def _camera_crop_boxes(
    image_shape: tuple[int, ...],
    cid: str,
    capture_time_ms: Any,
    frame_ref: Mapping[str, Any],
    tracks: list[Any],
) -> list[tuple[int, int, int, int]]:
    """트랙의 카메라별 2D bbox·관절 주변 영역만 잘라낼 크롭 상자 목록(xyxy, 픽셀 정수)을 구한다."""
    h, w = int(image_shape[0]), int(image_shape[1])
    if h <= 0 or w <= 0:
        return []
    seq = frame_ref.get("frame_sequence") if isinstance(frame_ref, Mapping) else None
    raw_boxes: list[tuple[int, int, int, int]] = []
    for tr in tracks:
        if not isinstance(tr, Mapping):
            continue
        for pc in tr.get("per_camera") or ():
            if not isinstance(pc, Mapping) or pc.get("camera_id") != cid:
                continue
            pc_time = pc.get("estimated_capture_time")
            if isinstance(capture_time_ms, int) and isinstance(pc_time, int) and pc_time != capture_time_ms:
                continue
            pc_seq = (pc.get("frame_ref") or {}).get("frame_sequence") if isinstance(pc.get("frame_ref"), Mapping) else None
            if seq is not None and pc_seq is not None and pc_seq != seq:
                continue
            xs: list[float] = []
            ys: list[float] = []
            bbox = pc.get("bbox")
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                try:
                    bx0, by0, bx1, by1 = (float(v) for v in bbox)
                    if np.all(np.isfinite([bx0, by0, bx1, by1])) and bx1 > bx0 and by1 > by0:
                        xs.extend((bx0, bx1))
                        ys.extend((by0, by1))
                except (TypeError, ValueError):
                    pass
            for kp in pc.get("keypoints_2d") or ():
                if isinstance(kp, (list, tuple)) and len(kp) == 3:
                    try:
                        u, v, conf = float(kp[0]), float(kp[1]), float(kp[2])
                        if np.all(np.isfinite([u, v, conf])) and conf > 0:
                            xs.append(u)
                            ys.append(v)
                    except (TypeError, ValueError):
                        pass
            if not xs or not ys:
                continue
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            pad_x = max(DEFAULT_CROP_MIN_PAD_PX, int((x1 - x0) * DEFAULT_CROP_PAD_RATIO))
            pad_y = max(DEFAULT_CROP_MIN_PAD_PX, int((y1 - y0) * DEFAULT_CROP_PAD_RATIO))
            cx0 = max(0, min(w, int(np.floor(x0)) - pad_x))
            cy0 = max(0, min(h, int(np.floor(y0)) - pad_y))
            cx1 = max(0, min(w, int(np.ceil(x1)) + pad_x))
            cy1 = max(0, min(h, int(np.ceil(y1)) + pad_y))
            if cx1 > cx0 and cy1 > cy0:
                raw_boxes.append((cx0, cy0, cx1, cy1))
    if len(raw_boxes) <= 1:
        return raw_boxes
    # 겹치거나 합집합 면적 손실이 작은 상자는 하나로 합쳐 배열 조각 수를 줄인다.
    merged = list(raw_boxes)
    changed = True
    while changed and len(merged) > 1:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                ax0, ay0, ax1, ay1 = merged[i]
                bx0, by0, bx1, by1 = merged[j]
                ux0, uy0, ux1, uy1 = min(ax0, bx0), min(ay0, by0), max(ax1, bx1), max(ay1, by1)
                area_a = (ax1 - ax0) * (ay1 - ay0)
                area_b = (bx1 - bx0) * (by1 - by0)
                area_u = (ux1 - ux0) * (uy1 - uy0)
                if area_u <= (area_a + area_b) * 1.25:
                    merged[i] = (ux0, uy0, ux1, uy1)
                    merged.pop(j)
                    changed = True
                    break
            if changed:
                break
    return merged


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
                 timing: ChannelTiming | None = None, start: bool = True) -> None:
        """start=False: 쓰기 스레드를 띄우지 않는다(다른 프로세스가 `_publish` 만 부를 때, S15P31A101-662)."""
        self.path = path
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending = None
        self.period_s = period_s
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.timing = timing or ChannelTiming()
        self.dropped = self.errors = 0
        self.last_written_bytes = 0
        self._thread = threading.Thread(target=self._run, name="avatar-ipc", daemon=True)
        if start:
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
        refs, image_arrays, total_crop_bytes = self._prepare(metadata, frames)
        self._publish(metadata, refs, image_arrays, total_crop_bytes, started=started)

    def _prepare(self, metadata, frames) -> tuple[dict[str, Any], dict[str, np.ndarray], int]:
        """카메라별 크롭 배열과 참조 표를 만든다(파일은 쓰지 않는다). 프로세스 분리 경로는 이것만 부모에서 부른다(-662)."""
        image_arrays = {}
        refs = {}
        world = metadata.get("world") if isinstance(metadata, Mapping) else None
        tracks = world.get("tracks") if isinstance(world, Mapping) else None
        use_crops = isinstance(tracks, list)
        total_crop_bytes = 0
        for i, (cid, frame) in enumerate(frames.items()):
            if frame.image is None:
                continue
            key = f"image_{i}"
            image = np.asarray(frame.image)
            if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                continue
            frame_ref = dict(frame.frame_ref)
            ref_entry: dict[str, Any] = {
                "array": key,
                "capture_time_ms": frame.estimated_capture_time,
                "frame_ref": frame_ref,
            }
            if use_crops:
                with self.timing.measure("write_crop", camera=cid):
                    crops = _camera_crop_boxes(image.shape, cid, frame.estimated_capture_time, frame_ref, tracks)
                    ref_entry["image_shape"] = [int(image.shape[0]), int(image.shape[1]), int(image.shape[2])]
                    ref_entry["crops_xyxy"] = [list(box) for box in crops]
                    if not crops:
                        arr = np.empty((0,), dtype=np.uint8)
                    elif len(crops) == 1:
                        x0, y0, x1, y1 = crops[0]
                        arr = np.ascontiguousarray(image[y0:y1, x0:x1])
                    else:
                        arr = np.concatenate([
                            np.ascontiguousarray(image[y0:y1, x0:x1]).reshape(-1)
                            for x0, y0, x1, y1 in crops
                        ])
                image_arrays[key] = arr
                total_crop_bytes += int(arr.nbytes)
            else:
                image_arrays[key] = image
                total_crop_bytes += int(image.nbytes)
            refs[cid] = ref_entry
        return refs, image_arrays, total_crop_bytes

    def _publish(self, metadata, refs, image_arrays, total_crop_bytes: int, *, started: float | None = None) -> None:
        """참조 표·배열을 NPZ 로 원자적으로 게시한다(임시 파일 → os.replace → 완료 표식)."""
        if started is None:
            started = self.timing.clock()
        publication_id = uuid.uuid4().hex
        doc = {"format": "simai.avatar-input@1", **metadata, "frames": refs,
               "publication_id": publication_id}
        doc.pop("published_at_ms", None)  # 게시 완료 뒤의 시각은 아래 완료 표식에만 기록한다.
        arrays = {
            "metadata": np.frombuffer(json.dumps(doc, allow_nan=False).encode("utf-8"), dtype=np.uint8),
            **image_arrays,
        }
        self.last_written_bytes = total_crop_bytes + int(arrays["metadata"].nbytes)
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
        if self._thread.is_alive():
            self._thread.join(timeout=0.2)


class LatestChannelReader:
    supports_predicate = True

    def __init__(self, path: Path, *, timing: ChannelTiming | None = None,
                 require_publication: bool = False, ring_slots: int = DEFAULT_RING_SLOTS) -> None:
        self.path = path
        self.timing = timing or ChannelTiming()
        self.require_publication = require_publication
        self._ring_slots = max(1, int(ring_slots))
        self._ring_buffers: dict[str, list[tuple[np.ndarray, list[tuple[int, int, int, int]]]]] = {}
        self._ring_index: dict[str, int] = {}

    def _reconstruct_frame(self, cid: str, ref: Mapping[str, Any], raw: np.ndarray) -> np.ndarray:
        shape = ref.get("image_shape")
        if not (isinstance(shape, (list, tuple)) and len(shape) == 3):
            return raw
        h, w, c = (int(v) for v in shape)
        crops: list[tuple[int, int, int, int]] = []
        for box in ref.get("crops_xyxy") or ():
            if isinstance(box, (list, tuple)) and len(box) == 4:
                x0, y0, x1, y1 = (int(v) for v in box)
                if 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h:
                    crops.append((x0, y0, x1, y1))
        slots = self._ring_buffers.get(cid)
        if slots is None or not slots or slots[0][0].shape != (h, w, c):
            slots = [(np.zeros((h, w, c), dtype=np.uint8), []) for _ in range(self._ring_slots)]
            self._ring_buffers[cid] = slots
            self._ring_index[cid] = 0
        idx = self._ring_index[cid] % len(slots)
        self._ring_index[cid] = (idx + 1) % len(slots)
        buf, prev_crops = slots[idx]
        for px0, py0, px1, py1 in prev_crops:
            buf[py0:py1, px0:px1] = 0
        if len(crops) == 1:
            x0, y0, x1, y1 = crops[0]
            buf[y0:y1, x0:x1] = raw.reshape(y1 - y0, x1 - x0, c)
        elif len(crops) > 1:
            offset = 0
            flat = raw.reshape(-1)
            for x0, y0, x1, y1 in crops:
                n = (y1 - y0) * (x1 - x0) * c
                buf[y0:y1, x0:x1] = flat[offset:offset + n].reshape(y1 - y0, x1 - x0, c)
                offset += n
        slots[idx] = (buf, crops)
        return buf

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
            images = {
                cid: self._reconstruct_frame(cid, ref, z[ref["array"]])
                for cid, ref in doc["frames"].items()
            }
        self.timing.record("images_loaded")
        return doc, images
