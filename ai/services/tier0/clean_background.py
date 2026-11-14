"""기존 누적기를 Tier 0에 조립한다.

- `settings.offload=True`(기본)일 때는 `multiprocessing`(spawn 컨텍스트) 자식 프로세스 1개가
  카메라별 표본 링버퍼·사람 마스크·회전 복구용 축소 표본·`nanmedian` 합성·유효 마스크 계산을 맡는다.
- 판정/검출 스레드의 `offer`는 참조만 유계 큐에 넘기고(블로킹·프레임 복사 금지),
  전용 코디네이터가 `multiprocessing.shared_memory` 고정 슬롯과 유계 메타 큐(`put_nowait`)로 자식에 전달한다.
- `settings.offload=False`이면 기존 인프로세스 경로로 동작한다.
- 세팅 배경 촬영(S15P31A101-686): 워커가 `regenerate.request.json` 에 `fresh_since_ms` 를 실어 보내면, 그 요청이 열려 있는 동안
  표본 간격을 `setup_sample_interval_ms` 로 줄이고 그 시각 이후 표본만으로 카메라별 상태·묶음을 만든다(옛 묶음·기준 대체 없음).
"""

from __future__ import annotations

import atexit
import copy
import hashlib
import json
import logging
import math
import multiprocessing
import os
import queue
import shutil
import threading
import time
import uuid
import weakref
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Any

import numpy as np

from services.tier0.settings import CleanBackgroundSettings
from simai_ai.calibration.world_frame.clean_background import (
    CameraBackgroundStatus,
    CleanBackgroundAccumulator,
    CleanBackgroundConfig,
    CleanBackgroundExport,
    extract_person_bboxes,
)
from simai_ai.tracking.local_tracker import require_camera_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Context:
    document: Mapping[str, Any]
    provenance: Mapping[str, Any]
    generations: Mapping[str, int]

    @property
    def version(self) -> str:
        return self.document["calibration_version"]


class _WorkerCrashedError(RuntimeError):
    """자식 프로세스가 비정상 종료되었을 때 발생한다."""


class _WorkerStoppedError(RuntimeError):
    """종료 신호로 자식 대기를 중단할 때 발생한다."""


@dataclass
class _ShmSlot:
    slot_id: int
    shm: SharedMemory | None = None
    in_use: bool = False


def _json(path: Path, doc: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _empty_status(cid: str) -> CameraBackgroundStatus:
    return CameraBackgroundStatus(
        camera_id=cid,
        sample_count=0,
        time_range_ms=None,
        never_revealed_ratio=None,
        mean_person_cover_ratio=None,
        image_size=None,
        bytes_used=0,
        ready=False,
    )


# 세팅 배경 촬영의 카메라별 상태(계약 site_setup 1.16.0 background_capture.cameras.*.state, S15P31A101-686)
CAPTURE_READY = "READY"
CAPTURE_INSUFFICIENT = "INSUFFICIENT_SAMPLES"
CAPTURE_PERSON = "PERSON_OCCLUDED"
CAPTURE_NO_VIDEO = "NO_VIDEO"
# 다른 카메라 관측 시각이 하한을 이만큼 지났는데 이 카메라 프레임이 하한 뒤로 하나도 없으면 영상 없음(세팅 간격 1 s 의 5배)
NO_VIDEO_GRACE_MS = 5_000


def capture_state(
    status: CameraBackgroundStatus,
    *,
    required: int,
    max_unrevealed_ratio: float,
    last_frame_ms: int | None,
    since_ms: int,
    observed_now_ms: int,
) -> str:
    """세팅 촬영 하한 뒤 표본으로 본 카메라 한 대의 상태. observed_now_ms 는 카메라들의 마지막 관측 시각(데이터 시계)."""
    if status.ready:
        return CAPTURE_READY
    if status.sample_count == 0:
        no_frame = last_frame_ms is None or last_frame_ms < since_ms
        return CAPTURE_NO_VIDEO if no_frame and observed_now_ms - since_ms >= NO_VIDEO_GRACE_MS else CAPTURE_INSUFFICIENT
    person_now = (status.last_person_cover_ratio or 0.0) > 0.0
    hidden = (
        status.sample_count >= required
        and status.never_revealed_ratio is not None
        and status.never_revealed_ratio >= max_unrevealed_ratio
    )
    return CAPTURE_PERSON if person_now or hidden else CAPTURE_INSUFFICIENT


def _small_samples_for_camera(
    cache: dict[int, tuple[Any, np.ndarray, np.ndarray]],
    side: int,
    samples: list[Any],
) -> list[tuple[Any, np.ndarray, np.ndarray]]:
    import cv2

    retained: dict[int, tuple[Any, np.ndarray, np.ndarray]] = {}
    result: list[tuple[Any, np.ndarray, np.ndarray]] = []
    for sample in samples:
        key = id(sample)
        entry = cache.get(key)
        if entry is None or entry[0]() is not sample:
            h, w = sample.reveal_mask.shape
            scale = min(1.0, side / max(h, w))
            size = (round(w * scale), round(h * scale))
            gray = cv2.resize(sample.frame_bgr, size, interpolation=cv2.INTER_LINEAR)
            # 축소 영역에 가린 픽셀이 하나라도 있으면 보간된 값도 사용하지 않는다.
            mask = cv2.resize(sample.reveal_mask.astype(np.float32), size, interpolation=cv2.INTER_AREA) >= 0.999999
            entry = (weakref.ref(sample), gray, mask)
        retained[key] = entry
        result.append(entry)
    cache.clear()
    cache.update(retained)
    return result


def _build_rotation_view_payload(
    accumulator: CleanBackgroundAccumulator,
    rotation_samples: dict[str, dict[int, tuple[Any, np.ndarray, np.ndarray]]],
    rotation_side: int | None,
    min_samples: int,
    cid: str,
) -> dict[str, Any] | None:
    if rotation_side is None:
        return None
    q = accumulator._samples.get(cid)
    if not q or len(q) < min_samples:
        return None
    samples = list(q)[-min(10, min_samples) :]
    if len({s.frame_bgr.shape for s in samples}) != 1:
        return None
    cache = rotation_samples.setdefault(cid, {})
    small = _small_samples_for_camera(cache, rotation_side, samples)
    revealed = np.logical_or.reduce([s[2] for s in small])
    if not bool(revealed.any()):
        return None
    return {
        "first_ms": samples[0].capture_time_ms,
        "last_ms": samples[-1].capture_time_ms,
        "size": samples[-1].frame_bgr.shape[1::-1],
        "sample_times": [s.capture_time_ms for s in samples],
        "samples": [(s[1], s[2]) for s in small],
        # 혼잡 판정 보류 근거(-564): 회전 창의 평균 사람 가림·끝내 못 본 비율
        "mean_person_cover_ratio": round(float(np.mean([s.person_cover_ratio for s in samples])), 6),
        "never_revealed_ratio": round(float(1.0 - revealed.mean()), 6),
    }


def _mask_unknown_with_samples(
    samples: list[Any],
    rows_per_chunk: int,
    jpeg_quality: int,
    folder: Path,
    cid: str,
    doc: dict[str, Any],
) -> dict[str, Any]:
    """관측 횟수가 부족하거나 시간에 따라 변하는 픽셀은 유효 마스크에서 제외한다."""
    import cv2

    h, w = samples[0].reveal_mask.shape
    valid = np.empty((h, w), dtype=np.uint8)
    minimum = max(2, math.ceil(len(samples) * 0.5))
    # 행 단위로 임시 메모리를 제한한다. 검출로 가린 픽셀은 분산에서 제외한다.
    for start in range(0, h, rows_per_chunk):
        end = min(h, start + rows_per_chunk)
        masks = np.stack([sample.reveal_mask[start:end] for sample in samples])
        pixels = np.stack([sample.frame_bgr[start:end] for sample in samples]).astype(np.float32)
        count = masks.sum(axis=0)
        weights = masks[..., None]
        total = (pixels * weights).sum(axis=0)
        mean = total / np.maximum(count[..., None], 1)
        variance = (((pixels - mean) ** 2) * weights).sum(axis=0) / np.maximum(count[..., None], 1)
        valid[start:end] = ((count >= minimum) & (variance.max(axis=2) <= 225)).astype(np.uint8) * 255
    record = doc["cameras"][cid]
    image_path = folder / record["image"]
    image = cv2.imread(str(image_path))
    if image is None:
        raise OSError(f"배경 이미지를 읽을 수 없다: {image_path}")
    image[valid == 0] = 0
    if not cv2.imwrite(str(image_path), image, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]):
        raise OSError(f"배경 이미지를 쓸 수 없다: {image_path}")
    mask_name = f"{cid}.valid.png"
    if not cv2.imwrite(str(folder / mask_name), valid):
        raise OSError(f"배경 마스크를 쓸 수 없다: {cid}")
    updates = {
        "valid_mask": mask_name,
        "invalid_ratio": round(float((valid == 0).mean()), 6),
        "image_sha256": "sha256:" + hashlib.sha256(image_path.read_bytes()).hexdigest(),
    }
    record.update(updates)
    return updates


def _clean_background_worker_main(
    config: CleanBackgroundConfig,
    cmd_queue: Any,
    res_queue: Any,
    horizon_ms: int | None = None,
) -> None:
    """spawn 자식 프로세스 진입점: 카메라별 표본 링버퍼·마스크·nanmedian 합성·유효 마스크를 전담한다.

    horizon_ms: 표본 보관 창(관측 시각 기준). 누적기 간격은 세팅 촬영의 짧은 간격이라 평소 창을 따로 받는다(-686).
    """
    for env_key in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(env_key, "1")
    try:
        import cv2

        cv2.setNumThreads(1)
    except Exception:  # noqa: BLE001
        pass
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass

    accumulator = CleanBackgroundAccumulator(config=config)
    rotation_side: int | None = None
    rotation_samples: dict[str, dict[int, tuple[Any, np.ndarray, np.ndarray]]] = {}
    shm_cache: dict[str, SharedMemory] = {}
    horizon = horizon_ms if horizon_ms is not None else config.sample_interval_ms * config.max_samples_per_camera

    def get_shm(name: str) -> SharedMemory:
        shm = shm_cache.get(name)
        if shm is None:
            shm = SharedMemory(name=name, create=False)
            if os.name != "nt":
                try:
                    from multiprocessing import resource_tracker

                    resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            shm_cache[name] = shm
        return shm

    def close_shm(name: str) -> None:
        shm = shm_cache.pop(name, None)
        if shm is not None:
            try:
                shm.close()
            except Exception:  # noqa: BLE001
                pass

    def expire_camera(cid: str, now_ms: int) -> None:
        samples = accumulator._samples.get(cid)
        if samples is not None:
            while samples and samples[0].capture_time_ms < now_ms - horizon:
                samples.popleft()
            if not samples:
                accumulator._image_sizes.pop(cid, None)

    try:
        while True:
            msg = cmd_queue.get()
            if msg is None:
                break
            cmd = msg[0]
            req_id = msg[1]
            if cmd == "crash_for_test":
                os._exit(int(msg[2]) if len(msg) > 2 else 1)
            try:
                if cmd == "add_frame":
                    (
                        _,
                        _,
                        cid,
                        capture_time_ms,
                        slot_id,
                        shm_name,
                        shape,
                        dtype_str,
                        boxes,
                        rot_side,
                        stale_shms,
                    ) = msg
                    for old_name in stale_shms:
                        close_shm(old_name)
                    if rot_side != rotation_side:
                        rotation_side = rot_side
                        rotation_samples.clear()
                    shm = get_shm(shm_name)
                    arr = np.ndarray(shape, dtype=np.dtype(dtype_str), buffer=shm.buf)
                    try:
                        added = accumulator.add_frame(cid, capture_time_ms, arr, boxes)
                    finally:
                        del arr
                    expire_camera(cid, capture_time_ms)
                    rot_view = _build_rotation_view_payload(
                        accumulator,
                        rotation_samples,
                        rotation_side,
                        config.min_samples_per_camera,
                        cid,
                    )
                    res_queue.put(
                        (
                            "ok",
                            req_id,
                            {
                                "added": added,
                                "slot_id": slot_id,
                                "statuses": accumulator.status(),
                                "rotation_view": rot_view,
                                "worker_pid": os.getpid(),
                            },
                        )
                    )
                elif cmd == "reset_cameras":
                    _, _, cids = msg
                    for cid in cids:
                        accumulator.reset_camera(cid)
                        rotation_samples.pop(cid, None)
                    res_queue.put(
                        (
                            "ok",
                            req_id,
                            {
                                "statuses": accumulator.status(),
                                "worker_pid": os.getpid(),
                            },
                        )
                    )
                elif cmd == "expire_samples":
                    _, _, cids, now_ms = msg
                    for cid in cids:
                        expire_camera(cid, now_ms)
                    res_queue.put(
                        (
                            "ok",
                            req_id,
                            {
                                "statuses": accumulator.status(),
                                "worker_pid": os.getpid(),
                            },
                        )
                    )
                elif cmd == "configure_rotation":
                    _, _, max_side = msg
                    if rotation_side != max_side:
                        rotation_side = max_side
                        rotation_samples.clear()
                    views = {
                        cid: _build_rotation_view_payload(
                            accumulator,
                            rotation_samples,
                            rotation_side,
                            config.min_samples_per_camera,
                            cid,
                        )
                        for cid in accumulator.camera_ids
                    }
                    res_queue.put(
                        (
                            "ok",
                            req_id,
                            {
                                "rotation_views": views,
                                "worker_pid": os.getpid(),
                            },
                        )
                    )
                elif cmd == "status_since":
                    _, _, since_ms, min_samples = msg
                    res_queue.put(
                        (
                            "ok",
                            req_id,
                            {
                                "statuses": {
                                    cid: accumulator.camera_status(cid, since_ms=since_ms, min_samples=min_samples)
                                    for cid in accumulator.camera_ids
                                },
                                "worker_pid": os.getpid(),
                            },
                        )
                    )
                elif cmd == "export":
                    _, _, out_dir_str, calibration_version, camera_ids, require_ready, since_ms, min_samples = msg
                    exported = accumulator.export(
                        Path(out_dir_str),
                        calibration_version=calibration_version,
                        camera_ids=camera_ids,
                        require_ready=require_ready,
                        since_ms=since_ms,
                        min_samples=min_samples,
                    )
                    res_queue.put(
                        (
                            "ok",
                            req_id,
                            {
                                "exported": exported,
                                "statuses": accumulator.status(),
                                "worker_pid": os.getpid(),
                            },
                        )
                    )
                elif cmd == "mask_unknown":
                    _, _, folder_str, cid, doc, since_ms = msg
                    samples = [
                        s for s in accumulator._samples.get(cid, ())
                        if since_ms is None or s.capture_time_ms >= since_ms
                    ]
                    if not samples:
                        raise ValueError(f"{cid}: 누적된 표본이 없다")
                    updates = _mask_unknown_with_samples(
                        samples,
                        accumulator.config.rows_per_chunk,
                        accumulator.config.jpeg_quality,
                        Path(folder_str),
                        cid,
                        doc,
                    )
                    res_queue.put(
                        (
                            "ok",
                            req_id,
                            {
                                "updates": updates,
                                "worker_pid": os.getpid(),
                            },
                        )
                    )
                elif cmd == "synthesize_camera":
                    _, _, cid = msg
                    bg, st = accumulator.synthesize_camera(cid)
                    res_queue.put(
                        (
                            "ok",
                            req_id,
                            {
                                "bg": bg,
                                "status": st,
                                "worker_pid": os.getpid(),
                            },
                        )
                    )
                elif cmd == "ping":
                    res_queue.put(("ok", req_id, {"worker_pid": os.getpid()}))
                else:
                    raise ValueError(f"알 수 없는 명령: {cmd!r}")
            except Exception as exc:  # noqa: BLE001
                res_queue.put(("error", req_id, type(exc), str(exc)))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        for shm in list(shm_cache.values()):
            try:
                shm.close()
            except Exception:  # noqa: BLE001
                pass
        shm_cache.clear()


class _OffloadAccumulatorProxy:
    """`offload=True`일 때 부모 프로세스에서 `CleanBackgroundAccumulator`와 동일한 공개 인터페이스를 제공한다.

    실제 원본 표본 배열과 마스크·nanmedian 합성은 자식 프로세스가 소유하고,
    부모는 최신 카메라별 상태 요약(`CameraBackgroundStatus`)만 메모리에 캐시한다.
    """

    def __init__(self, owner: BackgroundCleanBackground, config: CleanBackgroundConfig) -> None:
        self._owner = owner
        self.config = config
        self._statuses: dict[str, CameraBackgroundStatus] = {}

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(self._statuses)

    @property
    def total_bytes(self) -> int:
        return sum(st.bytes_used for st in self._statuses.values())

    def _clear_cached(self) -> None:
        self._statuses.clear()

    def _update_statuses(self, statuses: Mapping[str, CameraBackgroundStatus]) -> None:
        self._statuses = dict(statuses)

    def add_frame(
        self,
        camera_id: str,
        capture_time_ms: int,
        frame_bgr: np.ndarray,
        person_boxes: Any = (),
    ) -> bool:
        return self._owner._worker_add_frame(camera_id, capture_time_ms, frame_bgr, person_boxes)

    def reset_camera(self, camera_id: str) -> None:
        cid = require_camera_id(camera_id)
        self._statuses.pop(cid, None)
        self._owner._worker_reset_cameras((cid,))

    def camera_status(self, camera_id: str) -> CameraBackgroundStatus:
        cid = require_camera_id(camera_id)
        st = self._statuses.get(cid)
        if st is not None:
            return st
        return _empty_status(cid)

    def status(self) -> dict[str, CameraBackgroundStatus]:
        return {cid: self.camera_status(cid) for cid in sorted(self._statuses)}

    def is_camera_ready(self, camera_id: str) -> bool:
        return self.camera_status(camera_id).ready

    def is_ready(self, camera_ids: Sequence[str] | None = None) -> bool:
        targets = tuple(camera_ids) if camera_ids is not None else tuple(self._statuses)
        if not targets:
            return False
        return all(self.is_camera_ready(cid) for cid in targets)

    def synthesize_camera(self, camera_id: str) -> tuple[np.ndarray, CameraBackgroundStatus]:
        cid = require_camera_id(camera_id)
        return self._owner._worker_synthesize_camera(cid)

    def export(
        self,
        out_dir: Path,
        *,
        calibration_version: str,
        camera_ids: Sequence[str] | None = None,
        require_ready: bool = True,
        since_ms: int | None = None,
        min_samples: int | None = None,
    ) -> CleanBackgroundExport:
        return self._owner._worker_export(
            out_dir,
            calibration_version=calibration_version,
            camera_ids=camera_ids,
            require_ready=require_ready,
            since_ms=since_ms,
            min_samples=min_samples,
        )


_LIVE_INSTANCES: weakref.WeakSet[BackgroundCleanBackground] = weakref.WeakSet()


def _atexit_cleanup() -> None:
    for inst in list(_LIVE_INSTANCES):
        try:
            inst.close()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_atexit_cleanup)


# 공유 메모리 한 칸이 받을 프레임(2304×1296 BGR 은 약 9 MB). tmpfs 가 모자라면 쓰는 순간 SIGBUS 로
# Tier 0 프로세스 전체가 죽으므로, 시작·칸 할당 때 남은 공간을 먼저 본다(Lead 검토, S15P31A101-563).
# 시작 기준은 실제 칸 크기 그대로(queue_depth 4 × 10 MB = 40 MB): 도커 기본 /dev/shm 64 MB 에서도 분리가 켜지게(-574).
# 더 큰 프레임은 칸 할당 때 다시 보고 모자라면 그 표본만 버린다
_SHM_SLOT_BUDGET_BYTES = 10 * 1024 * 1024
_SHM_HEADROOM_BYTES = 4 * 1024 * 1024
_SHM_DIR = Path("/dev/shm")


def _shm_free_bytes() -> int | None:
    """Linux tmpfs 남은 바이트. /dev/shm 이 없는 OS(Windows·macOS)는 None(검사 생략)."""
    if not _SHM_DIR.is_dir():
        return None
    try:
        return int(shutil.disk_usage(_SHM_DIR).free)
    except OSError:
        return None


class BackgroundCleanBackground:
    def __init__(
        self,
        document: Mapping[str, Any],
        provenance: Mapping[str, Any],
        *,
        settings: CleanBackgroundSettings,
        state_dir: Path,
        stop: threading.Event,
    ) -> None:
        if settings.offload:
            free = _shm_free_bytes()
            need = max(1, settings.queue_depth) * _SHM_SLOT_BUDGET_BYTES + _SHM_HEADROOM_BYTES
            if free is not None and free < need:
                log.warning("[tier0] clean_background: /dev/shm 남은 공간 %d B < 필요 %d B, 프로세스 분리를 끄고 "
                            "인프로세스로 누적한다", free, need)
                settings = replace(settings, offload=False)
        self.settings = settings
        self.root = settings.output_dir or state_dir / "clean-background"
        self.control = state_dir / "clean_background"
        self.stop = stop
        self._context = _Context(
            copy.deepcopy(document),
            copy.deepcopy(provenance),
            {c["camera_id"]: 0 for c in document["cameras"]},
        )
        self._active: _Context | None = None
        # 누적기는 세팅 촬영의 짧은 간격까지 받고, 평소 간격은 offer 가 지킨다(-686). 보관 창은 평소 간격 기준 그대로다
        self._horizon_ms = settings.sample_interval_ms * settings.max_samples_per_camera
        config = CleanBackgroundConfig(
            sample_interval_ms=min(settings.sample_interval_ms, settings.setup_sample_interval_ms),
            min_samples_per_camera=settings.min_samples_per_camera,
            max_samples_per_camera=settings.max_samples_per_camera,
            max_bytes=settings.max_bytes,
        )
        self._mp_ctx = multiprocessing.get_context("spawn")
        self._process: multiprocessing.Process | None = None
        self._cmd_queue: Any = None
        self._res_queue: Any = None
        self._ipc_lock = threading.Lock()
        self._req_seq = 0
        self._shm_slots: list[_ShmSlot] = [
            _ShmSlot(slot_id=i) for i in range(max(1, settings.queue_depth))
        ]
        self._retired_shm_names: list[str] = []
        self.allocated_shm_names: set[str] = set()
        self.active_shm_names: set[str] = set()
        self.worker_pid: int | None = None
        self.worker_restarts = 0
        self.disabled = False
        self._last_child_expire: tuple[str, int] | None = None
        self._last_child_rotation: dict[str, dict[str, Any] | None] = {}

        if settings.offload:
            self.accumulator: Any = _OffloadAccumulatorProxy(self, config)
        else:
            self.accumulator = CleanBackgroundAccumulator(config=config)

        self._queue: deque = deque()
        self._queue_lock = threading.Lock()  # 프레임 복사/상태 계산/파일 I/O 중에는 잡지 않는다.
        self._pending_bytes = 0
        self._last_offer: dict[str, tuple[int, int]] = {}
        self._exported: dict[str, tuple[Path, dict[str, Any]]] = {}
        self._reference: dict[str, tuple[Path, dict[str, Any]]] = {}
        self._complete: Path | None = None
        self.dropped = 0
        self.last_error: str | None = None
        self._retry = False
        self._rotation_side: int | None = None
        self._rotation_samples: dict[str, dict[int, tuple[Any, np.ndarray, np.ndarray]]] = {}
        self._rotation_lock = threading.Lock()
        self._rotation_views: dict[str, dict] = {}
        # 세팅 배경 촬영 요청(-686). 열려 있는 동안 offer 가 짧은 간격으로 표본을 받는다
        self._fresh: dict[str, Any] | None = None
        # 카메라별 마지막으로 받은 프레임 관측 시각(간격 때문에 버린 프레임 포함). 영상 없음 판단용
        self._last_seen: dict[str, int] = {}
        _LIVE_INSTANCES.add(self)

    @property
    def worker_process(self) -> multiprocessing.Process | None:
        return self._process

    def is_camera_ready(self, cid: str) -> bool:
        return self.accumulator.is_camera_ready(cid)

    def is_ready(self, camera_ids: Sequence[str] | None = None) -> bool:
        targets = tuple(camera_ids) if camera_ids is not None else tuple(self._context.generations)
        if not targets:
            return False
        return all(self.accumulator.is_camera_ready(cid) for cid in targets)

    def _start_worker(self) -> None:
        if not self.settings.offload or self.disabled:
            return
        if self._process is not None and self._process.is_alive():
            return
        self._cleanup_worker()
        cmd_capacity = max(8, self.settings.queue_depth * 2)
        self._cmd_queue = self._mp_ctx.Queue(maxsize=cmd_capacity)
        self._res_queue = self._mp_ctx.Queue()
        self._process = self._mp_ctx.Process(
            target=_clean_background_worker_main,
            args=(self.accumulator.config, self._cmd_queue, self._res_queue, self._horizon_ms),
            name="tier0-clean-bg-worker",
            daemon=True,
        )
        self._process.start()
        self.worker_pid = self._process.pid

    def _ensure_worker(self) -> bool:
        if not self.settings.offload:
            return True
        if self.disabled or self.stop.is_set():
            return False
        if self._process is not None and not self._process.is_alive():
            self._handle_worker_crash(f"exitcode={self._process.exitcode}")
            return not self.disabled
        if self._process is None:
            self._start_worker()
        return self._process is not None and not self.disabled

    def _unlink_shm(self, shm: SharedMemory) -> None:
        name = shm.name
        try:
            shm.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            shm.unlink()
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass
        self.active_shm_names.discard(name)

    def _cleanup_worker(self) -> None:
        proc = self._process
        self._process = None
        self.worker_pid = None
        cmd_q = self._cmd_queue
        res_q = self._res_queue
        self._cmd_queue = None
        self._res_queue = None

        if proc is not None:
            if proc.is_alive():
                if cmd_q is not None:
                    try:
                        cmd_q.put_nowait(None)
                    except Exception:  # noqa: BLE001
                        pass
                proc.join(timeout=0.5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=0.5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=0.5)

        for q in (cmd_q, res_q):
            if q is not None:
                try:
                    q.cancel_join_thread()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    q.close()
                except Exception:  # noqa: BLE001
                    pass

        for slot in self._shm_slots:
            if slot.shm is not None:
                self._unlink_shm(slot.shm)
                slot.shm = None
            slot.in_use = False
        self._retired_shm_names.clear()

    def close(self) -> None:
        """서비스 종료·시험 정리 시 큐와 자식 프로세스, 공유 메모리를 모두 정리한다."""
        with self._queue_lock:
            self._queue.clear()
            self._pending_bytes = 0
        with self._ipc_lock:
            self._cleanup_worker()

    def _handle_worker_crash(self, reason: str) -> None:
        with self._ipc_lock:
            self._cleanup_worker()
        if isinstance(self.accumulator, _OffloadAccumulatorProxy):
            self.accumulator._clear_cached()
        self._rotation_views = {}
        self._last_child_rotation.clear()
        self._last_child_expire = None
        with self._queue_lock:
            queued_cids = {item[0].camera_id for item in self._queue}
        for cid in list(self._last_offer):
            if cid not in queued_cids:
                self._last_offer.pop(cid, None)
        self._active = None

        if self.worker_restarts < 1:
            self.worker_restarts += 1
            self.last_error = f"clean_background worker restarted ({reason})"
            log.warning("clean_background 자식 프로세스 비정상 종료(%s): 1회 재시작한다", reason)
            try:
                with self._ipc_lock:
                    self._start_worker()
            except Exception as exc:  # noqa: BLE001
                self.disabled = True
                self.last_error = f"clean_background worker restart failed ({exc})"
                log.exception("clean_background 자식 프로세스 재시작 실패: 배경 누적을 끈다(판정은 계속한다)")
        else:
            self.disabled = True
            self.last_error = f"clean_background worker disabled ({reason})"
            log.error("clean_background 자식 프로세스 재실패(%s): 배경 누적을 끈다(판정은 계속한다)", reason)
            with self._queue_lock:
                self._queue.clear()
                self._pending_bytes = 0

        try:
            self._write_status(self._context)
        except OSError:
            log.exception("clean_background status_write_failed")

    def _call_worker(
        self,
        cmd: str,
        *args: Any,
        release_slot: _ShmSlot | None = None,
    ) -> dict[str, Any]:
        with self._ipc_lock:
            if self._process is None or not self._process.is_alive():
                if release_slot is not None:
                    release_slot.in_use = False
                raise _WorkerCrashedError(
                    f"exitcode={None if self._process is None else self._process.exitcode}"
                )
            self._req_seq += 1
            req_id = self._req_seq
            proc = self._process
            cmd_q = self._cmd_queue
            res_q = self._res_queue
            try:
                cmd_q.put_nowait((cmd, req_id, *args))
            except queue.Full:
                if release_slot is not None:
                    release_slot.in_use = False
                raise
            except (EOFError, BrokenPipeError, OSError) as exc:
                if release_slot is not None:
                    release_slot.in_use = False
                raise _WorkerCrashedError(str(exc)) from exc

            while True:
                if self.stop.is_set():
                    if release_slot is not None:
                        release_slot.in_use = False
                    raise _WorkerStoppedError("중지 신호 수신")
                try:
                    reply = res_q.get(timeout=0.05)
                except queue.Empty:
                    if not proc.is_alive():
                        if release_slot is not None:
                            release_slot.in_use = False
                        raise _WorkerCrashedError(f"exitcode={proc.exitcode}") from None
                    continue
                except (EOFError, BrokenPipeError, OSError) as exc:
                    if release_slot is not None:
                        release_slot.in_use = False
                    raise _WorkerCrashedError(str(exc)) from exc
                if reply[1] == req_id:
                    break

            if release_slot is not None:
                release_slot.in_use = False
            kind = reply[0]
            if kind == "error":
                _, _, exc_cls, msg = reply
                try:
                    raise exc_cls(msg)
                except TypeError:
                    raise RuntimeError(f"{exc_cls.__name__}: {msg}") from None
            payload: dict[str, Any] = reply[2]
            if "worker_pid" in payload:
                self.worker_pid = payload["worker_pid"]
            return payload

    def _acquire_shm_slot(self, nbytes: int) -> _ShmSlot | None:
        slot = next((s for s in self._shm_slots if not s.in_use), None)
        if slot is None:
            return None
        if slot.shm is not None and slot.shm.size < nbytes:
            self._retired_shm_names.append(slot.shm.name)
            self._unlink_shm(slot.shm)
            slot.shm = None
        if slot.shm is None:
            free = _shm_free_bytes()
            if free is not None and free < nbytes + _SHM_HEADROOM_BYTES:
                return None  # 쓰다가 SIGBUS 로 죽느니 이 표본을 버린다(dropped 증가)
            shm = SharedMemory(create=True, size=nbytes)
            slot.shm = shm
            self.allocated_shm_names.add(shm.name)
            self.active_shm_names.add(shm.name)
        slot.in_use = True
        return slot

    def _worker_add_frame(
        self,
        camera_id: str,
        capture_time_ms: int,
        frame_bgr: np.ndarray,
        person_boxes: Any = (),
    ) -> bool:
        cid = require_camera_id(camera_id)
        img = np.asarray(frame_bgr)
        if img.ndim != 3 or img.shape[2] != 3 or img.dtype != np.uint8:
            raise ValueError(
                f"{cid}: 프레임은 (H, W, 3) uint8 BGR 배열이어야 한다 (받은 shape={img.shape}, dtype={img.dtype})"
            )
        h, w = int(img.shape[0]), int(img.shape[1])
        if h < 1 or w < 1:
            raise ValueError(f"{cid}: 프레임 크기가 비어 있다: {(w, h)}")
        boxes = extract_person_bboxes(person_boxes)
        if not self._ensure_worker():
            return False

        slot = self._acquire_shm_slot(int(img.nbytes))
        if slot is None or slot.shm is None:
            self.dropped += 1
            return False
        try:
            dst = np.ndarray(img.shape, dtype=np.uint8, buffer=slot.shm.buf)
            np.copyto(dst, img)
            del dst
            stale = tuple(self._retired_shm_names)
            self._retired_shm_names.clear()
            payload = self._call_worker(
                "add_frame",
                cid,
                int(capture_time_ms),
                slot.slot_id,
                slot.shm.name,
                img.shape,
                img.dtype.str,
                boxes,
                self._rotation_side,
                stale,
                release_slot=slot,
            )
        except queue.Full:
            self.dropped += 1
            return False
        self.accumulator._update_statuses(payload["statuses"])
        self._last_child_expire = (cid, int(capture_time_ms))
        self._last_child_rotation[cid] = payload.get("rotation_view")
        return bool(payload["added"])

    def _worker_reset_cameras(self, cids: Sequence[str]) -> None:
        if not cids or self._process is None or not self._process.is_alive():
            return
        try:
            payload = self._call_worker("reset_cameras", tuple(cids))
            self.accumulator._update_statuses(payload["statuses"])
            for cid in cids:
                self._last_child_rotation.pop(cid, None)
        except (_WorkerCrashedError, _WorkerStoppedError):
            pass

    def _worker_expire_samples(self, cids: Sequence[str], now_ms: int) -> None:
        if not cids or not self._ensure_worker():
            return
        payload = self._call_worker("expire_samples", tuple(cids), int(now_ms))
        self.accumulator._update_statuses(payload["statuses"])

    def _worker_export(
        self,
        out_dir: Path,
        *,
        calibration_version: str,
        camera_ids: Sequence[str] | None = None,
        require_ready: bool = True,
        since_ms: int | None = None,
        min_samples: int | None = None,
    ) -> CleanBackgroundExport:
        if not self._ensure_worker():
            raise RuntimeError("배경 누적 작업자가 비활성화되었다")
        payload = self._call_worker(
            "export",
            str(Path(out_dir)),
            calibration_version,
            tuple(camera_ids) if camera_ids is not None else None,
            bool(require_ready),
            since_ms,
            min_samples,
        )
        self.accumulator._update_statuses(payload["statuses"])
        return payload["exported"]

    def _worker_mask_unknown(self, folder: Path, cid: str, doc: dict[str, Any], since_ms: int | None) -> None:
        if not self._ensure_worker():
            raise RuntimeError("배경 누적 작업자가 비활성화되었다")
        payload = self._call_worker("mask_unknown", str(Path(folder)), cid, doc, since_ms)
        doc["cameras"][cid].update(payload["updates"])

    def _statuses_since(self, ctx: _Context, since_ms: int, min_samples: int) -> dict[str, CameraBackgroundStatus]:
        """세팅 촬영 하한(since_ms) 뒤 표본만 센 카메라별 상태. 표본이 한 장도 없는 카메라도 빈 상태로 채운다."""
        if self.settings.offload:
            if not self._ensure_worker():
                raise RuntimeError("배경 누적 작업자가 비활성화되었다")
            statuses = dict(self._call_worker("status_since", int(since_ms), int(min_samples))["statuses"])
        else:
            statuses = {
                cid: self.accumulator.camera_status(cid, since_ms=since_ms, min_samples=min_samples)
                for cid in self.accumulator.camera_ids
            }
        return {cid: statuses.get(cid) or _empty_status(cid) for cid in ctx.generations}

    def _worker_synthesize_camera(self, cid: str) -> tuple[np.ndarray, CameraBackgroundStatus]:
        if not self._ensure_worker():
            raise RuntimeError("배경 누적 작업자가 비활성화되었다")
        payload = self._call_worker("synthesize_camera", cid)
        return payload["bg"], payload["status"]

    def configure_rotation_samples(self, max_side: int) -> None:
        with self._rotation_lock:
            if self._rotation_side != max_side:
                self._rotation_side = max_side
                self._rotation_samples.clear()
                self._rotation_views = {}
                self._last_child_rotation.clear()

    def _small_samples(self, cid: str, samples: list) -> list:
        with self._rotation_lock:
            side = self._rotation_side
            assert side is not None
            cache = self._rotation_samples.setdefault(cid, {})
            return _small_samples_for_camera(cache, side, samples)

    def offer(self, frame, detections) -> bool:
        """디코더가 소유권을 넘긴 불변 프레임을 참조한다. lock/큐가 바쁘면 기다리지 않는다."""
        if self.disabled or self.stop.is_set():
            return False
        ctx = self._context
        cid, at = frame.camera_id, frame.estimated_capture_time
        generation = ctx.generations.get(cid)
        if generation is None:
            return False
        self._last_seen[cid] = at
        # 세팅 배경 촬영 중에는 짧은 간격으로 받는다(-686)
        interval = self.settings.setup_sample_interval_ms if self._fresh is not None else self.settings.sample_interval_ms
        previous = self._last_offer.get(cid)
        if previous is not None and previous[0] == generation and at - previous[1] < interval:
            return False
        size = int(getattr(frame.image, "nbytes", 0))
        if size <= 0 or not self._queue_lock.acquire(blocking=False):
            self.dropped += 1
            return False
        try:
            if (
                len(self._queue) >= self.settings.queue_depth
                or self._pending_bytes + size > self.settings.max_pending_bytes
            ):
                self.dropped += 1
                return False
            self._queue.append((frame, detections, generation, size))
            self._pending_bytes += size
            self._last_offer[cid] = generation, at
            return True
        finally:
            self._queue_lock.release()

    def reset_camera(self, cid: str) -> None:
        ctx = self._context
        if cid in ctx.generations:
            self._context = _Context(
                ctx.document,
                ctx.provenance,
                {**ctx.generations, cid: ctx.generations[cid] + 1},
            )

    def activate(self, document, provenance) -> None:
        """작은 문서만 비교한다. 자세·렌즈·해상도가 같은 카메라의 표본은 유지한다."""
        ctx = self._context
        old = {c["camera_id"]: c for c in ctx.document["cameras"]}
        fields = ("R", "t", "K", "distortion", "image_size")
        generations = {}
        for camera in document["cameras"]:
            cid = camera["camera_id"]
            changed = cid not in old or any(camera.get(k) != old[cid].get(k) for k in fields)
            generations[cid] = ctx.generations.get(cid, 0) + int(changed)
        self._context = _Context(copy.deepcopy(document), copy.deepcopy(provenance), generations)

    def _sync(self) -> bool:
        ctx = self._context
        if self._active is ctx:
            return False
        previous = self._active
        reset_cids: list[str] = []
        for cid, generation in ctx.generations.items():
            if previous is not None and previous.generations.get(cid) != generation:
                reset_cids.append(cid)
        if previous is not None:
            for cid in previous.generations.keys() - ctx.generations.keys():
                reset_cids.append(cid)
        if reset_cids:
            if self.settings.offload:
                for cid in reset_cids:
                    self.accumulator._statuses.pop(cid, None)
                    self._last_child_rotation.pop(cid, None)
                self._worker_reset_cameras(reset_cids)
            else:
                for cid in reset_cids:
                    self.accumulator.reset_camera(cid)
        # 새 버전은 남은 표본을 다시 메타와 묶는다. 과거 산출물은 그대로 둔다.
        self._exported = {
            cid: value
            for cid, value in self._exported.items()
            if previous is not None
            and previous.version == ctx.version
            and previous.generations.get(cid) == ctx.generations.get(cid)
            and cid in ctx.generations
        }
        self._complete = None
        self._active = ctx
        self._rotation_views = {}
        log.info("clean_background context version=%s generations=%s", ctx.version, ctx.generations)
        return True

    def process_one(self) -> bool:
        """전용 스레드 한 단계. 합성 시험도 이 경로를 그대로 쓴다."""
        if self.disabled or self.stop.is_set():
            return False
        if self.settings.offload and not self._ensure_worker():
            return False
        try:
            changed = self._sync() or self._retry
            self._retry = False
            with self._queue_lock:
                item = self._queue.popleft() if self._queue else None
                if item is not None:
                    self._pending_bytes -= item[3]
            ctx = self._active
            assert ctx is not None
            if item is not None:
                frame, detections, generation, _ = item
                cid = frame.camera_id
                if ctx.generations.get(cid) == generation:
                    changed |= self.accumulator.add_frame(
                        cid,
                        frame.estimated_capture_time,
                        frame.image,
                        detections,
                    )
                    self._expire_samples(cid, frame.estimated_capture_time)
                    if self._rotation_side is not None:
                        self._publish_rotation(cid)
            self._regenerate(ctx, changed)
            if changed:
                self._export_ready(ctx)
                self.last_error = None
                self._write_status(ctx)
            return item is not None
        except _WorkerCrashedError as exc:
            self._handle_worker_crash(str(exc))
            return False
        except _WorkerStoppedError:
            return False

    def _regenerate(self, ctx: _Context, changed: bool = True) -> None:
        """배경 다시 만들기 요청(`regenerate.request.json`)을 처리한다.

        - `fresh_since_ms` 가 있는 요청(세팅 배경 촬영, -686): 그 시각 이후 표본만 센다. 카메라마다 세팅 최소 표본 수가 모이고 못 채운
          비율이 상한 아래가 될 때까지 요청을 열어 두고, 새 표본이 들어올 때마다 카메라별 상태(COLLECTING)를 응답한다. 다 되면 그
          표본만으로 묶음을 만들어 READY 로 닫는다. 옛 묶음·세팅 기준으로 대신하지 않는다.
        - 그 밖의 요청(관제 중 다시 만들기): 최근 창이 가득 찬(메모리 상한 안에서 도달 가능한 수) 카메라만 쓴다. 한 대라도 모자라면
          NOT_READY 와 카메라별 사유로 닫는다(세팅 기준 대체 없음).
        """
        request = self.control / "regenerate.request.json"
        if not request.is_file():
            if self._fresh is not None:
                log.info("clean_background 세팅 촬영 요청이 사라져 촬영 모드를 끝낸다 request_id=%s", self._fresh["request_id"])
                self._fresh = None
                self._write_status(ctx)
            return
        try:
            doc = json.loads(request.read_text(encoding="utf-8"))
            request_id = doc["request_id"]
            if not isinstance(request_id, int) or isinstance(request_id, bool) or request_id < 1:
                raise ValueError("invalid request_id")
            if "fresh_since_ms" in doc:
                self._regenerate_fresh(ctx, request, doc, request_id, changed)
                return
            self._fresh = None
            requested_at = doc.get("requested_at_ms")
            if isinstance(requested_at, int) and not isinstance(requested_at, bool):
                if self.settings.offload:
                    self._worker_expire_samples(tuple(ctx.generations), requested_at)
                else:
                    for cid in ctx.generations:
                        self._expire_samples(cid, requested_at)
            full = self._full_samples(ctx)
            cameras = self._recent_cameras(ctx, full)
            ready = all(c["state"] == CAPTURE_READY for c in cameras.values()) and self._regenerate_bundle(ctx, full)
            if not ready:
                self._complete = None
            self._write_status(ctx)
            self._respond({
                "request_id": request_id,
                "bundle_path": str(self._complete) if ready else None,
                "camera_ids": sorted(ctx.generations),
                "calibration_version": ctx.version,
                "status": "READY" if ready else "NOT_READY",
                "reason": None if ready else "BACKGROUND_NOT_READY",
                "required_samples": full,
                "cameras": cameras,
            })
            self._close_request(request, doc)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            log.exception("잘못된 배경 재생성 요청")
            request.unlink(missing_ok=True)
            self._fresh = None

    def _regenerate_fresh(
        self, ctx: _Context, request: Path, doc: Mapping[str, Any], request_id: int, changed: bool,
    ) -> None:
        since = doc["fresh_since_ms"]
        if not isinstance(since, int) or isinstance(since, bool) or since < 0:
            raise ValueError("invalid fresh_since_ms")
        started = self._fresh is None or self._fresh["doc"] != doc
        if started:
            self._fresh = {"request_id": request_id, "since_ms": since, "doc": dict(doc)}
            log.info("clean_background 세팅 촬영 시작 request_id=%s since_ms=%s", request_id, since)
        elif not changed:
            return  # 새 표본이 없으면 상태도 같다
        required = self.settings.setup_min_samples_per_camera
        observed_now = max([since, *(self._last_seen.get(cid, since) for cid in ctx.generations)])
        statuses = self._statuses_since(ctx, since, required)
        cameras = {
            cid: {
                "state": capture_state(
                    st, required=required, max_unrevealed_ratio=self.accumulator.config.max_unrevealed_ratio,
                    last_frame_ms=self._last_seen.get(cid), since_ms=since, observed_now_ms=observed_now,
                ),
                "sample_count": st.sample_count,
                "person_visible": None if st.last_person_cover_ratio is None else st.last_person_cover_ratio > 0.0,
                "never_revealed_ratio": None if st.never_revealed_ratio is None else round(st.never_revealed_ratio, 6),
                "last_frame_ms": self._last_seen.get(cid),
            }
            for cid, st in statuses.items()
        }
        response: dict[str, Any] = {
            "request_id": request_id,
            "purpose": doc.get("purpose"),
            "bundle_path": None,
            "camera_ids": sorted(ctx.generations),
            "calibration_version": ctx.version,
            "status": "COLLECTING",
            "reason": None,
            "sample_since_ms": since,
            "required_samples": required,
            "sample_interval_ms": self.settings.setup_sample_interval_ms,
            "cameras": cameras,
        }
        if all(c["state"] == CAPTURE_READY for c in cameras.values()):
            if self._assemble_bundle(ctx, since_ms=since, min_samples=required, extra={"sample_since_ms": since}):
                self._write_status(ctx)  # 응답보다 먼저: 워커가 READY 를 보고 status.json 의 묶음을 곧바로 확인한다
                self._respond({**response, "status": "READY", "bundle_path": str(self._complete)})
                self._close_request(request, doc)
                log.info("clean_background 세팅 촬영 완료 request_id=%s bundle=%s", request_id, self._complete)
                return
        elif observed_now - since > self.settings.setup_capture_max_ms:
            self._respond({**response, "status": "NOT_READY", "reason": "CAPTURE_EXPIRED"})
            self._close_request(request, doc)
            log.warning("clean_background 세팅 촬영 요청이 %d ms 안에 끝나지 않아 닫는다 request_id=%s",
                        self.settings.setup_capture_max_ms, request_id)
            self._write_status(ctx)
            return
        self._respond(response)
        if started:
            self._write_status(ctx)

    def _respond(self, response: Mapping[str, Any]) -> None:
        self.control.mkdir(parents=True, exist_ok=True)
        tmp = self.control / f".response-{uuid.uuid4().hex}.tmp"
        try:
            _json(tmp, response)
            tmp.replace(self.control / "regenerate.response.json")
        finally:
            tmp.unlink(missing_ok=True)

    def _close_request(self, request: Path, doc: Mapping[str, Any]) -> None:
        # 새 요청이 도착했다면 지우지 않는다.
        if json.loads(request.read_text(encoding="utf-8")) == doc:
            request.unlink(missing_ok=True)
        self._fresh = None

    def _full_samples(self, ctx: _Context) -> int:
        """관제 중 다시 만들기의 '가득 찬 창' 표본 수: 최대 표본 수와 메모리 상한이 카메라마다 허용하는 수 중 작은 값.

        2304×1296 표본 1장(BGR+마스크)은 약 12 MB 라 2.2 GiB 상한에서 4대면 카메라당 약 48장에서 축출이 시작된다.
        최대 표본 수(60)를 그대로 요구하면 영원히 못 채워 늘 다른 경로로 빠졌다(-686). 최소 표본 수보다 작게는 두지 않는다.
        """
        statuses = [self.accumulator.camera_status(cid) for cid in ctx.generations]
        per_sample = max((st.bytes_used / st.sample_count for st in statuses if st.sample_count), default=0.0)
        cap = self.settings.max_samples_per_camera
        if per_sample > 0 and ctx.generations:
            cap = min(cap, int(self.settings.max_bytes // (per_sample * len(ctx.generations))))
        return max(self.settings.min_samples_per_camera, cap)

    def _recent_cameras(self, ctx: _Context, full: int) -> dict[str, dict[str, Any]]:
        out = {}
        for cid in ctx.generations:
            st = self.accumulator.camera_status(cid)
            if st.ready and st.sample_count >= full:
                state = CAPTURE_READY
            elif st.sample_count == 0:
                state = CAPTURE_NO_VIDEO
            elif st.sample_count >= self.settings.min_samples_per_camera and not st.ready:
                state = CAPTURE_PERSON
            else:
                state = CAPTURE_INSUFFICIENT
            out[cid] = {"state": state, "sample_count": st.sample_count}
        return out

    def _expire_samples(self, cid: str, now_ms: int) -> None:
        # 기존 설정으로 창 길이를 조절한다. 공백이 있으면 오래된 표본을 보관하지 않는다.
        if self.settings.offload:
            if self._last_child_expire == (cid, int(now_ms)):
                return
            self._worker_expire_samples((cid,), now_ms)
            return
        samples = self.accumulator._samples.get(cid)
        if samples is not None:
            horizon = self._horizon_ms
            while samples and samples[0].capture_time_ms < now_ms - horizon:
                samples.popleft()
            if not samples:
                self.accumulator._image_sizes.pop(cid, None)

    def _publish_rotation(self, cid: str) -> None:
        """배경 작업자에서 새 표본만 축소한다. 회전 작업자는 불변 스냅샷을 참조한다."""
        ctx = self._active
        if ctx is None or self._context is not ctx:
            self._rotation_views = {k: v for k, v in self._rotation_views.items() if k != cid}
            return
        if self.settings.offload:
            rot_view = self._last_child_rotation.get(cid)
            if rot_view is None:
                self._rotation_views = {k: v for k, v in self._rotation_views.items() if k != cid}
                return
            view = {
                "signature": (ctx.generations[cid], rot_view["first_ms"], rot_view["last_ms"]),
                "size": rot_view["size"],
                "sample_times": rot_view["sample_times"],
                "samples": rot_view["samples"],
                "mean_person_cover_ratio": rot_view.get("mean_person_cover_ratio"),
                "never_revealed_ratio": rot_view.get("never_revealed_ratio"),
            }
            self._rotation_views = {**self._rotation_views, cid: view}
            return
        q = self.accumulator._samples.get(cid)
        if not q or len(q) < self.settings.min_samples_per_camera:
            self._rotation_views = {k: v for k, v in self._rotation_views.items() if k != cid}
            return
        # 준비 게이트(기본 20장)는 유지하고 회전 판단은 최근 최대 10장으로 한다.
        # 오래된 자세 혼합을 줄이며, 관측 과반·분산 게이트는 이 창에 그대로 적용한다.
        samples = list(q)[-min(10, self.settings.min_samples_per_camera) :]
        if len({s.frame_bgr.shape for s in samples}) != 1:
            self._rotation_views = {k: v for k, v in self._rotation_views.items() if k != cid}
            return
        small = self._small_samples(cid, samples)
        revealed = np.logical_or.reduce([s[2] for s in small])
        if not bool(revealed.any()):
            self._rotation_views = {k: v for k, v in self._rotation_views.items() if k != cid}
            return
        view = {
            "signature": (ctx.generations[cid], samples[0].capture_time_ms, samples[-1].capture_time_ms),
            "size": samples[-1].frame_bgr.shape[1::-1],
            "sample_times": [s.capture_time_ms for s in samples],
            "samples": [(s[1], s[2]) for s in small],
            "mean_person_cover_ratio": round(float(np.mean([s.person_cover_ratio for s in samples])), 6),
            "never_revealed_ratio": round(float(1.0 - revealed.mean()), 6),
        }
        self._rotation_views = {**self._rotation_views, cid: view}

    def rotation_snapshot(self, exclude_cameras=()) -> dict | None:
        ctx = self._active
        views = self._rotation_views
        if ctx is None or self._context is not ctx:
            return None
        required = set(ctx.generations) - set(exclude_cameras or ())
        if not required or not required <= set(views):
            return None
        return {cid: views[cid] for cid in required}

    def recent_signature(self, cid: str) -> tuple[int, int, int] | None:
        ctx = self._active
        views = self._rotation_views
        if ctx is None or self._context is not ctx or cid not in views:
            return None
        return views[cid]["signature"]

    def synthesize_recent(self, cid: str) -> tuple[np.ndarray, np.ndarray, int] | None:
        from simai_ai.calibration.rotation_worker import synthesize_samples

        views = self.rotation_snapshot()
        if views is None or cid not in views:
            return None
        view = views[cid]
        bg, valid = synthesize_samples(view["samples"])
        return bg, valid, view["signature"][2]

    def _mask_unknown(self, folder: Path, cid: str, doc: dict[str, Any], since_ms: int | None = None) -> None:
        """관측 횟수가 부족하거나 시간에 따라 변하는 픽셀은 유효 마스크에서 제외한다(since_ms 면 그 뒤 표본만 본다)."""
        if self.settings.offload:
            self._worker_mask_unknown(folder, cid, doc, since_ms)
            return
        samples = [s for s in self.accumulator._samples[cid] if since_ms is None or s.capture_time_ms >= since_ms]
        _mask_unknown_with_samples(
            samples,
            self.accumulator.config.rows_per_chunk,
            self.accumulator.config.jpeg_quality,
            folder,
            cid,
            doc,
        )

    def _regenerate_bundle(self, ctx: _Context, full: int | None = None) -> bool:
        """가득 찬 최근 창(메모리 상한 안에서 도달 가능한 표본 수)만 쓴다. 한 대라도 모자라면 만들지 않는다.

        예전에는 모자란 카메라를 세팅 기준(REFERENCE_FALLBACK)으로 대신했는데, 회전 등으로 시점이 바뀐 뒤에도 옛 화면을 써서
        15차 세팅이 FLOOR_NOT_FOUND 로 실패했다(-686). 이제 대신하지 않는다(모르면 비운다).
        """
        full = self._full_samples(ctx) if full is None else full
        for cid in ctx.generations:
            status = self.accumulator.camera_status(cid)
            if not (status.ready and status.sample_count >= full):
                return False
        return self._assemble_bundle(ctx, since_ms=None, min_samples=None, extra={})

    def _assemble_bundle(
        self, ctx: _Context, *, since_ms: int | None, min_samples: int | None, extra: Mapping[str, Any],
    ) -> bool:
        """카메라마다 (since_ms 뒤) 표본으로 배경·유효 마스크를 만들어 새 UUID 묶음으로 공개하고 `_complete` 로 둔다."""
        version_dir = self.root / ctx.version
        staging = version_dir / f".pending-{uuid.uuid4().hex}"
        target = version_dir / f"bundle-{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=True)
            cameras: dict[str, Any] = {}
            base: dict[str, Any] | None = None
            for cid in sorted(ctx.generations):
                temp = staging / f"camera-{uuid.uuid4().hex}"
                try:
                    result = self.accumulator.export(
                        temp, calibration_version=ctx.version, camera_ids=[cid], since_ms=since_ms, min_samples=min_samples,
                    )
                except ValueError as exc:
                    log.warning("clean_background 묶음 조립 중 %s 가 준비되지 않았다: %s", cid, exc)
                    return False
                self._mask_unknown(temp, cid, result.metadata, since_ms)
                record = result.metadata["cameras"][cid]
                record["background_source"] = "RECENT"
                for name in (record["image"], record["valid_mask"]):
                    shutil.copyfile(temp / name, staging / name)
                shutil.rmtree(temp)
                cameras[cid] = record
                base = base or result.metadata
            assert base is not None
            meta = {
                **{k: v for k, v in base.items() if k != "cameras"},
                "cameras": cameras,
                "calibration_version": ctx.version,
                "input_provenance": ctx.provenance,
                "camera_generations": dict(ctx.generations),
                "complete": True,
                "ready": True,
                **extra,
            }
            _json(staging / "clean_backgrounds.json", meta)
            _json(
                staging / "COMPLETE",
                {
                    "calibration_version": ctx.version,
                    "metadata": "clean_backgrounds.json",
                    "camera_ids": sorted(cameras),
                },
            )
            if not self._publish(staging, target, ctx):
                return False
            self._complete = target
            return True
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _publish(self, staging: Path, target: Path, ctx: _Context) -> bool:
        if self.stop.is_set() or self._context is not ctx:
            return False
        staging.rename(target)  # 같은 파일시스템의 새 UUID 경로로 공개한다.
        return True

    def _export_ready(self, ctx: _Context) -> None:
        version_dir = self.root / ctx.version
        # 계약의 ID는 파일명 구성요소다. 외부 경로로 빠져나가는 값은 거부한다.
        for name in (ctx.version, *ctx.generations):
            if name in (".", "..") or "/" in name or "\\" in name or ":" in name:
                raise ValueError("배경 경로의 버전/카메라 ID가 안전하지 않다")
        for cid in ctx.generations:
            if cid in self._exported or not self.accumulator.is_camera_ready(cid):
                continue
            staging = version_dir / f".pending-{uuid.uuid4().hex}"
            target = version_dir / f"camera-{uuid.uuid4().hex}"
            try:
                exported = self.accumulator.export(staging, calibration_version=ctx.version, camera_ids=[cid])
                doc = exported.metadata
                self._mask_unknown(staging, cid, doc)
                doc["cameras"][cid]["background_source"] = "REFERENCE"
                doc.update(input_provenance=ctx.provenance, camera_generations=dict(ctx.generations), complete=False)
                _json(exported.meta_path, doc)
                if not self._publish(staging, target, ctx):
                    return
                self._exported[cid] = target, doc
                self._reference.setdefault(cid, (target, copy.deepcopy(doc)))
                log.info("clean_background camera_ready camera=%s version=%s path=%s", cid, ctx.version, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        if len(self._exported) != len(ctx.generations) or self._complete is not None:
            return
        staging = version_dir / f".pending-{uuid.uuid4().hex}"
        target = version_dir / f"bundle-{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=True)
            cameras = {}
            for cid, (folder, doc) in self._exported.items():
                record = doc["cameras"][cid]
                shutil.copyfile(folder / record["image"], staging / record["image"])
                shutil.copyfile(folder / record["valid_mask"], staging / record["valid_mask"])
                cameras[cid] = record
            meta = {
                **next(iter(self._exported.values()))[1],
                "cameras": cameras,
                "complete": True,
                "input_provenance": ctx.provenance,
                "camera_generations": dict(ctx.generations),
            }
            _json(staging / "clean_backgrounds.json", meta)
            _json(
                staging / "COMPLETE",
                {
                    "calibration_version": ctx.version,
                    "metadata": "clean_backgrounds.json",
                    "camera_ids": sorted(cameras),
                },
            )
            if self._publish(staging, target, ctx):
                self._complete = target
                log.info("clean_background complete version=%s path=%s", ctx.version, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _write_status(self, ctx: _Context) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        cameras = {
            cid: {
                **self.accumulator.camera_status(cid).to_record(),
                "reference_path": str(self._reference[cid][0]) if cid in self._reference else None,
                "exported": cid in self._exported,
                "export_path": str(self._exported[cid][0]) if cid in self._exported else None,
            }
            for cid in ctx.generations
        }
        doc = {
            "calibration_version": ctx.version,
            "input_provenance": ctx.provenance,
            "camera_generations": dict(ctx.generations),
            "cameras": cameras,
            "complete": self._complete is not None,
            "bundle": None if self._complete is None else str(self._complete),
            "queue_dropped": self.dropped,
            "last_error": self.last_error,
            # 세팅 배경 촬영 중이면 그 요청과 표본 하한(-686). 진단용
            "setup_capture": None if self._fresh is None else {
                "request_id": self._fresh["request_id"], "since_ms": self._fresh["since_ms"],
            },
        }
        staging = self.root / f".status-{uuid.uuid4().hex}.tmp"
        try:
            _json(staging, doc)
            if self._context is ctx:
                target = self.root / "status.json"
                for attempt in range(5):
                    try:
                        staging.replace(target)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.005)
                log.info("clean_background status %s", json.dumps(doc, ensure_ascii=False))
        finally:
            staging.unlink(missing_ok=True)

    def run(self) -> None:
        try:
            if self.settings.offload and not self.disabled:
                self._ensure_worker()
            while not self.stop.is_set() and not self.disabled:
                try:
                    if not self.process_one():
                        if self.disabled:
                            break
                        self.stop.wait(0.1)
                    elif not self.disabled:
                        self.last_error = None
                except Exception as exc:  # noqa: BLE001
                    self.last_error = str(exc)
                    log.exception("clean_background failed(판정은 계속한다)")
                    # 저장 실패도 표본을 버리지 않고 재시도한다. 같은 완성 경로를 덮어쓰지 않는다.
                    if self._active is not None:
                        try:
                            self._write_status(self._active)
                        except OSError:
                            log.exception("clean_background status_write_failed")
                    self._retry = True
                    self.stop.wait(1.0)
        finally:
            self.close()
