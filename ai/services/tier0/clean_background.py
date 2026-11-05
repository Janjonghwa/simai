"""기존 누적기를 Tier 0에 조립한다. tick에서는 참조만 넘기고 CPU/디스크는 전용 스레드가 맡는다."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import shutil
import threading
import uuid
import weakref
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from services.tier0.settings import CleanBackgroundSettings
from simai_ai.calibration.world_frame.clean_background import CleanBackgroundAccumulator, CleanBackgroundConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Context:
    document: Mapping[str, Any]
    provenance: Mapping[str, Any]
    generations: Mapping[str, int]

    @property
    def version(self) -> str:
        return self.document["calibration_version"]


def _json(path: Path, doc: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


class BackgroundCleanBackground:
    def __init__(self, document, provenance, *, settings: CleanBackgroundSettings,
                 state_dir: Path, stop: threading.Event) -> None:
        self.settings = settings
        self.root = settings.output_dir or state_dir / "clean-background"
        self.control = state_dir / "clean_background"
        self.stop = stop
        self._context = _Context(copy.deepcopy(document), copy.deepcopy(provenance),
                                 {c["camera_id"]: 0 for c in document["cameras"]})
        self._active: _Context | None = None
        self.accumulator = CleanBackgroundAccumulator(
            config=CleanBackgroundConfig(
                sample_interval_ms=settings.sample_interval_ms,
                min_samples_per_camera=settings.min_samples_per_camera,
                max_samples_per_camera=settings.max_samples_per_camera, max_bytes=settings.max_bytes))
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

    def configure_rotation_samples(self, max_side: int) -> None:
        with self._rotation_lock:
            if self._rotation_side != max_side:
                self._rotation_side = max_side
                self._rotation_samples.clear()
                self._rotation_views = {}

    def _small_samples(self, cid: str, samples: list) -> list:
        import cv2

        with self._rotation_lock:
            side = self._rotation_side
            assert side is not None
            cache = self._rotation_samples.setdefault(cid, {})
            retained = {}
            result = []
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
            self._rotation_samples[cid] = retained
            return result

    def offer(self, frame, detections) -> bool:
        """디코더가 소유권을 넘긴 불변 프레임을 참조한다. lock/큐가 바쁘면 기다리지 않는다."""
        ctx = self._context
        cid, at = frame.camera_id, frame.estimated_capture_time
        generation = ctx.generations.get(cid)
        if generation is None or self.stop.is_set():
            return False
        previous = self._last_offer.get(cid)
        if previous is not None and previous[0] == generation and at - previous[1] < self.settings.sample_interval_ms:
            return False
        size = int(getattr(frame.image, "nbytes", 0))
        if size <= 0 or not self._queue_lock.acquire(blocking=False):
            self.dropped += 1
            return False
        try:
            if (len(self._queue) >= self.settings.queue_depth
                    or self._pending_bytes + size > self.settings.max_pending_bytes):
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
            self._context = _Context(ctx.document, ctx.provenance,
                                     {**ctx.generations, cid: ctx.generations[cid] + 1})

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
        for cid, generation in ctx.generations.items():
            if previous is not None and previous.generations.get(cid) != generation:
                self.accumulator.reset_camera(cid)
        if previous is not None:
            for cid in previous.generations.keys() - ctx.generations.keys():
                self.accumulator.reset_camera(cid)
        # 새 버전은 남은 표본을 다시 메타와 묶는다. 과거 산출물은 그대로 둔다.
        self._exported = {cid: value for cid, value in self._exported.items()
                          if previous is not None and previous.version == ctx.version
                          and previous.generations.get(cid) == ctx.generations.get(cid) and cid in ctx.generations}
        self._complete = None
        self._active = ctx
        self._rotation_views = {}
        log.info("clean_background context version=%s generations=%s", ctx.version, ctx.generations)
        return True

    def process_one(self) -> bool:
        """전용 스레드 한 단계. 합성 시험도 이 경로를 그대로 쓴다."""
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
                changed |= self.accumulator.add_frame(cid, frame.estimated_capture_time, frame.image, detections)
                self._expire_samples(cid, frame.estimated_capture_time)
                if self._rotation_side is not None:
                    self._publish_rotation(cid)
        self._regenerate(ctx)
        if changed:
            self._export_ready(ctx)
            self.last_error = None
            self._write_status(ctx)
        return item is not None

    def _regenerate(self, ctx: _Context) -> None:
        request = self.control / "regenerate.request.json"
        if not request.is_file():
            return
        try:
            doc = json.loads(request.read_text(encoding="utf-8"))
            request_id = doc["request_id"]
            if not isinstance(request_id, int) or isinstance(request_id, bool) or request_id < 1:
                raise ValueError("invalid request_id")
            requested_at = doc.get("requested_at_ms")
            if isinstance(requested_at, int) and not isinstance(requested_at, bool):
                for cid in ctx.generations:
                    self._expire_samples(cid, requested_at)
            ready = self._regenerate_bundle(ctx)
            if not ready:
                self._complete = None
            response = {"request_id": request_id, "bundle_path": str(self._complete) if ready else None,
                        "camera_ids": sorted(ctx.generations), "calibration_version": ctx.version,
                        "status": "READY" if ready else "NOT_READY", "reason": None if ready else "BACKGROUND_NOT_READY"}
            self.control.mkdir(parents=True, exist_ok=True)
            tmp = self.control / f".response-{uuid.uuid4().hex}.tmp"
            try:
                _json(tmp, response)
                tmp.replace(self.control / "regenerate.response.json")
            finally:
                tmp.unlink(missing_ok=True)
            # 새 요청이 도착했다면 지우지 않는다.
            if json.loads(request.read_text(encoding="utf-8")) == doc:
                request.unlink(missing_ok=True)
            self._write_status(ctx)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            log.exception("잘못된 배경 재생성 요청")
            request.unlink(missing_ok=True)

    def _expire_samples(self, cid: str, now_ms: int) -> None:
        # 기존 설정으로 창 길이를 조절한다. 공백이 있으면 오래된 표본을 보관하지 않는다.
        samples = self.accumulator._samples.get(cid)
        if samples is not None:
            horizon = self.settings.sample_interval_ms * self.settings.max_samples_per_camera
            while samples and samples[0].capture_time_ms < now_ms - horizon:
                samples.popleft()

    def _publish_rotation(self, cid: str) -> None:
        """배경 스레드에서 새 표본만 축소한다. 회전 작업자는 불변 스냅샷을 참조한다."""
        ctx = self._active
        q = self.accumulator._samples.get(cid)
        if ctx is None or self._context is not ctx or not q or len(q) < self.settings.min_samples_per_camera:
            self._rotation_views = {k: v for k, v in self._rotation_views.items() if k != cid}
            return
        # 준비 게이트(기본 20장)는 유지하고 회전 판단은 최근 최대 10장으로 한다.
        # 오래된 자세 혼합을 줄이며, 관측 과반·분산 게이트는 이 창에 그대로 적용한다.
        samples = list(q)[-min(10, self.settings.min_samples_per_camera):]
        if len({s.frame_bgr.shape for s in samples}) != 1:
            self._rotation_views = {k: v for k, v in self._rotation_views.items() if k != cid}
            return
        small = self._small_samples(cid, samples)
        revealed = np.logical_or.reduce([s[2] for s in small])
        if 1.0 - float(revealed.mean()) >= self.accumulator.config.max_unrevealed_ratio:
            self._rotation_views = {k: v for k, v in self._rotation_views.items() if k != cid}
            return
        view = {"signature": (ctx.generations[cid], samples[0].capture_time_ms, samples[-1].capture_time_ms),
                "size": samples[-1].frame_bgr.shape[1::-1],
                "sample_times": [s.capture_time_ms for s in samples],
                "samples": [(s[1], s[2]) for s in small]}
        self._rotation_views = {**self._rotation_views, cid: view}

    def rotation_snapshot(self) -> dict | None:
        ctx = self._active
        views = self._rotation_views
        if ctx is None or self._context is not ctx or set(views) != set(ctx.generations):
            return None
        return views

    def recent_signature(self, cid: str) -> tuple[int, int, int] | None:
        views = self.rotation_snapshot()
        return views[cid]["signature"] if views is not None and cid in views else None

    def synthesize_recent(self, cid: str) -> tuple[np.ndarray, np.ndarray, int] | None:
        from simai_ai.calibration.rotation_worker import synthesize_samples

        views = self.rotation_snapshot()
        if views is None or cid not in views:
            return None
        view = views[cid]
        bg, valid = synthesize_samples(view["samples"])
        return bg, valid, view["signature"][2]

    def _mask_unknown(self, folder: Path, cid: str, doc: dict[str, Any]) -> None:
        """관측 횟수가 부족하거나 시간에 따라 변하는 픽셀은 유효 마스크에서 제외한다."""
        import cv2

        samples = list(self.accumulator._samples[cid])
        h, w = samples[0].reveal_mask.shape
        valid = np.empty((h, w), dtype=np.uint8)
        minimum = max(2, math.ceil(len(samples) * 0.5))
        # 행 단위로 임시 메모리를 제한한다. 검출로 가린 픽셀은 분산에서 제외한다.
        for start in range(0, h, self.accumulator.config.rows_per_chunk):
            end = min(h, start + self.accumulator.config.rows_per_chunk)
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
        if not cv2.imwrite(str(image_path), image, [cv2.IMWRITE_JPEG_QUALITY, self.accumulator.config.jpeg_quality]):
            raise OSError(f"배경 이미지를 쓸 수 없다: {image_path}")
        mask_name = f"{cid}.valid.png"
        if not cv2.imwrite(str(folder / mask_name), valid):
            raise OSError(f"배경 마스크를 쓸 수 없다: {cid}")
        record.update(valid_mask=mask_name, invalid_ratio=round(float((valid == 0).mean()), 6),
                      image_sha256="sha256:" + hashlib.sha256(image_path.read_bytes()).hexdigest())

    def _regenerate_bundle(self, ctx: _Context) -> bool:
        """가득 찬 최근 창을 쓰고, 부족한 카메라는 세팅 기준을 명시적으로 대체한다."""
        if not all(cid in self._reference for cid in ctx.generations):
            return False
        # 다른 자세/해상도의 기준 이미지를 현재 보정에 맞는 것처럼 내보내지 않는다.
        for cid in ctx.generations:
            reference_doc = self._reference[cid][1]
            reference_generation = reference_doc["camera_generations"].get(cid)
            status = self.accumulator.camera_status(cid)
            full = status.ready and status.sample_count >= self.settings.max_samples_per_camera
            if not full and reference_generation != ctx.generations[cid]:
                return False
        version_dir = self.root / ctx.version
        staging = version_dir / f".pending-{uuid.uuid4().hex}"
        target = version_dir / f"bundle-{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=True)
            cameras = {}
            for cid in sorted(ctx.generations):
                status = self.accumulator.camera_status(cid)
                full = status.ready and status.sample_count >= self.settings.max_samples_per_camera
                if full:
                    temp = staging / f"camera-{uuid.uuid4().hex}"
                    result = self.accumulator.export(temp, calibration_version=ctx.version, camera_ids=[cid])
                    self._mask_unknown(temp, cid, result.metadata)
                    record = result.metadata["cameras"][cid]
                    record["background_source"] = "RECENT"
                    folder = temp
                else:
                    folder, ref_doc = self._reference[cid]
                    record = copy.deepcopy(ref_doc["cameras"][cid])
                    record["background_source"] = "REFERENCE_FALLBACK"
                    record["reference_calibration_version"] = record["calibration_version"]
                    record["calibration_version"] = ctx.version
                for name in (record["image"], record["valid_mask"]):
                    shutil.copyfile(folder / name, staging / name)
                if full:
                    shutil.rmtree(folder)
                cameras[cid] = record
            meta = {**copy.deepcopy(next(iter(self._reference.values()))[1]), "cameras": cameras,
                    "calibration_version": ctx.version, "input_provenance": ctx.provenance,
                    "camera_generations": dict(ctx.generations), "complete": True, "ready": True}
            _json(staging / "clean_backgrounds.json", meta)
            _json(staging / "COMPLETE", {"calibration_version": ctx.version, "metadata": "clean_backgrounds.json",
                                        "camera_ids": sorted(cameras)})
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
            meta = {**next(iter(self._exported.values()))[1], "cameras": cameras, "complete": True,
                    "input_provenance": ctx.provenance, "camera_generations": dict(ctx.generations)}
            _json(staging / "clean_backgrounds.json", meta)
            _json(staging / "COMPLETE", {"calibration_version": ctx.version, "metadata": "clean_backgrounds.json",
                                        "camera_ids": sorted(cameras)})
            if self._publish(staging, target, ctx):
                self._complete = target
                log.info("clean_background complete version=%s path=%s", ctx.version, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _write_status(self, ctx: _Context) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        cameras = {cid: {**self.accumulator.camera_status(cid).to_record(),
                         "reference_path": str(self._reference[cid][0]) if cid in self._reference else None,
                         "exported": cid in self._exported,
                         "export_path": str(self._exported[cid][0]) if cid in self._exported else None}
                   for cid in ctx.generations}
        doc = {"calibration_version": ctx.version, "input_provenance": ctx.provenance,
               "camera_generations": dict(ctx.generations), "cameras": cameras,
               "complete": self._complete is not None,
               "bundle": None if self._complete is None else str(self._complete),
               "queue_dropped": self.dropped, "last_error": self.last_error}
        staging = self.root / f".status-{uuid.uuid4().hex}.tmp"
        try:
            _json(staging, doc)
            if self._context is ctx:
                staging.replace(self.root / "status.json")
                log.info("clean_background status %s", json.dumps(doc, ensure_ascii=False))
        finally:
            staging.unlink(missing_ok=True)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                if not self.process_one():
                    self.stop.wait(.1)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("clean_background failed(판정은 계속한다)")
                # 저장 실패도 표본을 버리지 않고 재시도한다. 같은 완성 경로를 덮어쓰지 않는다.
                if self._active is not None:
                    try:
                        self._write_status(self._active)
                    except OSError:
                        log.exception("clean_background status_write_failed")
                self._retry = True
                self.stop.wait(1.)
        with self._queue_lock:
            self._queue.clear()
            self._pending_bytes = 0
