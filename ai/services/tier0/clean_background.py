"""기존 누적기를 Tier 0에 조립한다. tick에서는 참조만 넘기고 CPU/디스크는 전용 스레드가 맡는다."""

from __future__ import annotations

import copy
import json
import logging
import shutil
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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
        self._published: dict[str, tuple[str, int]] = {}
        self._complete: Path | None = None
        self.dropped = 0
        self.last_error: str | None = None
        self._retry = False

    def offer(self, frame, detections) -> bool:
        """디코더가 소유권을 넘긴 불변 프레임을 참조한다. lock/큐가 바쁘면 기다리지 않는다."""
        ctx = self._context
        cid, at = frame.camera_id, frame.estimated_capture_time
        generation = ctx.generations.get(cid)
        if generation is None or self.stop.is_set() or self._published.get(cid) == (ctx.version, generation):
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
        self._published = {cid: (ctx.version, ctx.generations[cid]) for cid in self._exported}
        self._complete = None
        self._active = ctx
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
        if changed:
            self._export_ready(ctx)
            self.last_error = None
            self._write_status(ctx)
        return item is not None

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
                doc.update(input_provenance=ctx.provenance, camera_generations=dict(ctx.generations), complete=False)
                _json(exported.meta_path, doc)
                if not self._publish(staging, target, ctx):
                    return
                self._exported[cid] = target, doc
                self._published = {**self._published, cid: (ctx.version, ctx.generations[cid])}
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
