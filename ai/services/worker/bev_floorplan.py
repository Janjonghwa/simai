"""I5 `bev_floorplan` 처리기: 보정 문서·카메라 프레임 → BEV 평면도 PNG·메타데이터 → 객체 저장소 (명세 4.1, 12.5, FR-22).

BEV 생성은 `simai_ai.scene.bev.generate_bev`, 저장 형식은 `simai_ai.scene.bev_export.save_floorplan` 을 그대로 쓴다.
입력·결과 형식은 `contracts/messages/bev_floorplan.schema.json`.

- 입력은 계약으로 먼저 검사한다. 보정 문서는 calibration_ref 에서 받아 calibration_hash 와 바이트 sha256 이 같아야 쓴다.
  형식·해시·보정 상태·영상 크기 문제는 같은 입력으로 다시 해도 같으므로 재시도 불가 FAILED, 객체 저장소 일시 오류는 재시도 가능.
- 정적 3D 층은 쓰지 않는다(StaticSceneLayer 구현 없음). 그래서 결과 scene_version 은 입력에 있어도 null 이다.
- 우선순위 BACKGROUND. Tier 0 우선 반납은 러너가 한다. 처리기는 단계 사이(받기·생성·올리기)마다 ctx.cancelled 를 보고
  멈춘다. generate_bev 호출 하나는 중간에 멈출 수 없다.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.providers.base import CalibrationDoc, CalibrationProvider, Frame
from simai_ai.providers.contract import schema_errors
from simai_ai.scene.bev import BevConfig, BevInputError, generate_bev
from simai_ai.scene.bev_export import save_floorplan

from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError

INPUT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/bev_floorplan.schema.json#/$defs/input"}
RESULT_KEYS = ("purpose", "safety_use", "coordinate_frame", "cameras_used", "cameras_skipped", "coverage_ratio",
               "room_outline", "input_provenance")


def decode_image(content: bytes) -> np.ndarray:
    """JPEG·PNG 바이트 → (H, W, 3) BGR uint8 (OpenCV 규약, save_floorplan 기본 channel_order)."""
    import cv2

    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("영상으로 읽을 수 없는 바이트다")
    return image


class _DocCalibration(CalibrationProvider):
    def __init__(self, doc: CalibrationDoc) -> None:
        self._doc = doc

    def current(self) -> CalibrationDoc:
        return json.loads(json.dumps(self._doc))


class BevFloorplanHandler:
    def __init__(
        self,
        store: ObjectStore,
        *,
        clock_ms: Callable[[], int],
        decode: Callable[[bytes], np.ndarray] = decode_image,
        config: BevConfig | None = None,
    ) -> None:
        self._store = store
        self._clock_ms = clock_ms
        self._decode = decode
        self._config = config

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        inp = json.loads(json.dumps(ctx.input))
        if errors := schema_errors(inp, INPUT_SCHEMA):
            raise JobFailed("BEV_INPUT_INVALID", "; ".join(errors))
        cameras = [f["camera_id"] for f in inp["frames"]]
        if len(set(cameras)) != len(cameras):
            raise JobFailed("BEV_INPUT_INVALID", f"같은 카메라 프레임이 두 장이다: {cameras}")

        calibration = _DocCalibration(self._calibration(ctx, inp))
        frames = []
        for i, f in enumerate(inp["frames"]):
            _check_cancelled(ctx)
            try:
                image = self._decode(self._get(ctx, f["frame_uri"]))
            except ValueError as e:
                raise JobFailed("BEV_FRAME_UNREADABLE", f"{f['camera_id']}: {e}") from e
            frames.append(Frame(
                camera_id=f["camera_id"],
                estimated_capture_time=f["estimated_capture_time_ms"],
                timestamp_source=f["timestamp_source"],
                frame_ref={"file_uri": f["frame_uri"], "frame_index": 0},
                timestamp_uncertainty_ms=f["timestamp_uncertainty_ms"],
                image=image,
            ))
            ctx.report_progress(0.5 * (i + 1) / len(inp["frames"]))

        _check_cancelled(ctx)
        try:
            plan = generate_bev(calibration, frames, frame_source=inp["frame_source"],
                                created_at_ms=self._clock_ms(), config=self._config)
        except BevInputError as e:
            raise JobFailed("BEV_INPUT_REJECTED", str(e)) from e
        if not plan.coverage.any():
            raise JobFailed("BEV_NO_COVERAGE", "어느 카메라도 BEV 범위의 바닥을 보지 않는다")
        ctx.report_progress(0.8)

        with tempfile.TemporaryDirectory() as tmp:
            png_path, json_path = save_floorplan(plan, tmp)
            png, meta = Path(png_path).read_bytes(), Path(json_path).read_bytes()
        _check_cancelled(ctx)
        image_uri = self._put(ctx, png_path.name, png, "image/png")
        metadata_uri = self._put(ctx, json_path.name, meta, "application/json")

        m = plan.metadata
        return {
            "image_uri": image_uri,
            "image_sha256": _sha256(png),
            "content_type": "image/png",
            "metadata_uri": metadata_uri,
            "calibration_version": m["calibration_version"],
            "scene_version": None,  # 정적 3D 층을 쓰지 않는다
            "created_at": m["created_at"],
            "raster": plan.raster.to_metadata(),
            **{k: m[k] for k in RESULT_KEYS},
        }

    def _calibration(self, ctx: JobContext, inp: Mapping[str, Any]) -> CalibrationDoc:
        raw = self._get(ctx, inp["calibration_ref"])
        if _sha256(raw) != inp["calibration_hash"]:
            raise JobFailed("CALIBRATION_HASH_MISMATCH", f"{inp['calibration_ref']} 의 sha256 이 calibration_hash 와 다르다")
        try:
            doc = json.loads(raw)
        except ValueError as e:
            raise JobFailed("CALIBRATION_INVALID", f"JSON 이 아니다: {e}") from e
        if errors := schema_errors(doc, "calibration"):
            raise JobFailed("CALIBRATION_INVALID", "; ".join(errors))
        if doc["calibration_version"] != inp["calibration_version"]:
            raise JobFailed("CALIBRATION_INVALID",
                            f"보정 문서 버전 {doc['calibration_version']} 이 입력 {inp['calibration_version']} 과 다르다")
        return doc

    def _get(self, ctx: JobContext, uri: str) -> bytes:
        try:
            return self._store.get(ctx, uri)
        except ObjectStoreError as e:
            raise JobFailed("OBJECT_GET_FAILED", str(e), retryable=e.retryable) from e

    def _put(self, ctx: JobContext, name: str, content: bytes, content_type: str) -> str:
        try:
            return self._store.put(ctx, name, content, content_type)
        except ObjectStoreError as e:
            raise JobFailed("OBJECT_PUT_FAILED", str(e), retryable=e.retryable) from e


def _check_cancelled(ctx: JobContext) -> None:
    # 러너가 대여 상실·시간 초과·LIVE 선점으로 취소했다. 결과는 러너가 버리거나 반납한다
    if ctx.cancelled.is_set():
        raise JobFailed("CANCELLED", "작업이 취소됐다")


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()
