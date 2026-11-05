"""I5 `vlm_zone_draft` 처리기: 세팅 미리보기 3D 맵 기하 후보 추출 + Cosmos3-Nano 구역 초안 제안 (명세 5.3·8.2, S15P31A101-455).

- 입력은 `contracts/messages/vlm_zone_draft.schema.json#/$defs/input`, 결과는 `#/$defs/result` 로 검증한다.
- 초안은 관리자가 수락해 구성 묶음으로 발행하기 전까지 Tier 0 판정에 절대 들어가지 않는다(`safety_use: False`).
- VLM 프레임은 `gate_input_frames`(`prepare_vlm_frames`)를 거쳐 얼굴을 비식별 처리한 `VlmFrame` 만 VLM 에 넘긴다.
- 프레임(S15P31A101-479): 세팅 워커가 올린 카메라별 대표 프레임(긴 변 1280 px 이하로 줄인 JPEG)이 `frames` 로 들어온다. 보정 문서의 `K` 는
  원본 해상도(`image_size`) 기준이라, 후보를 투영해 크롭하기 전에 프레임 크기에 맞춰 `K` 를 비례 축소한다(`_fit_cameras_to_frames`).
- VLM 서버 비가용·시간 초과·응답 형식 오류 시에도 전체 초안 생성을 실패시키지 않고 결정적 기하 후보를 `"AI 의견 없음"`(`status="NO_OPINION"`)으로 돌려준다.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import numpy as np

from simai_ai.calibration.document import content_hash
from simai_ai.providers.contract import schema_errors
from simai_ai.vlm.client import VlmClient, VlmError, VlmSettings
from simai_ai.vlm.face_blur import Decoder, KeypointProvider
from simai_ai.vlm.zone_draft import (
    PROMPT_VERSION,
    CameraPose,
    GeometryCandidate,
    ZoneGeometryConfig,
    assess_geometry_candidates,
    cameras_from_calibration,
    extract_geometry_candidates,
    load_points_from_bytes,
)

from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError
from .vlm_frames import LocalFrames, decode_image, gate_input_frames

log = logging.getLogger("services.worker.vlm_zone_draft")

INPUT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/vlm_zone_draft.schema.json#/$defs/input"}
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/vlm_zone_draft.schema.json#/$defs/result"}


class VlmZoneDraftHandler:
    def __init__(
        self,
        store: ObjectStore | None = None,
        provider: KeypointProvider | None = None,
        *,
        client_factory: Callable[..., Any] | None = VlmClient,
        decode: Decoder | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        local_frames: LocalFrames | None = None,
        geometry_config: ZoneGeometryConfig | None = None,
        env: Mapping[str, str] = os.environ,
    ) -> None:
        self._store = store
        self._provider = provider
        self._client_factory = client_factory
        # 운영 조립(__main__)은 decode 를 넘기지 않는다. 없으면 모든 프레임이 decode 실패로 빠져 VLM 크롭이 영영 없었다(S15P31A101-479)
        self._decode: Decoder = decode if decode is not None else decode_image
        self._monotonic = monotonic
        self._local_frames = local_frames
        self._geometry_config = geometry_config
        self._env = env

    def __call__(self, ctx: JobContext) -> dict[str, Any]:
        started_s = self._monotonic()
        inp = json.loads(json.dumps(ctx.input))
        if errors := schema_errors(inp, INPUT_SCHEMA):
            raise JobFailed("VLM_ZONE_DRAFT_INPUT_INVALID", "; ".join(errors))

        _check_cancelled(ctx)
        candidates = self._resolve_candidates(ctx, inp)
        ctx.report_progress(0.4)

        _check_cancelled(ctx)
        cameras_by_id = self._resolve_cameras(ctx, inp)
        frames_by_camera = self._resolve_frames(ctx, inp)
        cameras_by_id = _fit_cameras_to_frames(cameras_by_id, frames_by_camera)
        ctx.report_progress(0.6)

        _check_cancelled(ctx)
        vlm_client = self._open_client(ctx.remaining_s())
        draft_items, status, model_version = assess_geometry_candidates(
            candidates,
            vlm_client=vlm_client,
            frames_by_camera=frames_by_camera,
            cameras_by_id=cameras_by_id,
        )
        ctx.report_progress(0.95)

        elapsed_ms = max(0, round((self._monotonic() - started_s) * 1000))
        result: dict[str, Any] = {
            "setup_session_id": inp["setup_session_id"],
            "site_id": inp["site_id"],
            "calibration_version": inp["calibration_version"],
            "preview_scene_version": inp["preview_scene_version"],
            "safety_use": False,
            "status": status,
            "model_version": model_version,
            "prompt_version": PROMPT_VERSION,
            "latency_ms": elapsed_ms,
            "drafts": [item.to_dict() for item in draft_items],
        }
        if errors := schema_errors(result, RESULT_SCHEMA):
            raise JobFailed("VLM_ZONE_DRAFT_OUTPUT_INVALID", "; ".join(errors))
        return result

    def _resolve_candidates(self, ctx: JobContext, inp: Mapping[str, Any]) -> list[GeometryCandidate]:
        raw_cands = inp.get("geometry_candidates")
        if raw_cands is not None:
            return [GeometryCandidate.from_dict(c) for c in raw_cands]

        uri = inp.get("pointcloud_uri") or inp.get("preview_scene_uri")
        if not uri or self._store is None:
            return []

        raw_bytes = self._get(ctx, str(uri))
        try:
            points_xyz = load_points_from_bytes(raw_bytes, str(uri))
        except ValueError as e:
            raise JobFailed("VLM_ZONE_DRAFT_SCENE_INVALID", f"{uri}: {e}") from e
        return extract_geometry_candidates(points_xyz, config=self._geometry_config)

    def _resolve_cameras(self, ctx: JobContext, inp: Mapping[str, Any]) -> dict[str, CameraPose]:
        cal_ref = inp.get("calibration_ref")
        if not cal_ref or self._store is None:
            return {}
        raw = self._get(ctx, str(cal_ref))
        try:
            doc = json.loads(raw)
        except ValueError as e:
            raise JobFailed("CALIBRATION_INVALID", f"JSON 이 아니다: {e}") from e
        if not isinstance(doc, Mapping):
            raise JobFailed("CALIBRATION_INVALID", "보정 문서가 객체가 아니다")
        expected_hash = inp.get("calibration_hash")
        # 서버가 싣는 calibration_hash 는 보정 content_hash(원본 바이트 해시가 아님)다
        if expected_hash and content_hash(doc) != expected_hash:
            raise JobFailed(
                "CALIBRATION_HASH_MISMATCH",
                f"{cal_ref} 의 content_hash 가 calibration_hash 와 다르다",
            )
        return cameras_from_calibration(doc)

    def _resolve_frames(self, ctx: JobContext, inp: Mapping[str, Any]) -> dict[str, Any]:
        frames_in = inp.get("frames") or []
        if not frames_in or self._store is None or self._provider is None:
            return {}
        uri_to_cam = {str(f["frame_uri"]): str(f["camera_id"]) for f in frames_in if "frame_uri" in f and "camera_id" in f}
        try:
            gate = gate_input_frames(
                ctx,
                self._store,
                frames_in,
                self._provider,
                decode=self._decode,
                local=self._local_frames,
            )
        except JobFailed as exc:
            # 프레임 다운로드/얼굴 검출 실패 시 VLM 만 건너뛰고 기하 후보는 살린다
            log.warning("job=%s 프레임 준비 실패로 VLM 없이 기하 후보만 반환한다: %s", ctx.job_id, exc)
            return {}
        if gate.dropped:
            log.info(
                "job=%s dropped=%s",
                ctx.job_id,
                [(d.source, d.reason, d.detail) for d in gate.dropped],
            )
        return {uri_to_cam.get(vlm_frame.source, vlm_frame.source): vlm_frame for vlm_frame in gate.frames}

    def _open_client(self, remaining_s: float | None) -> Any | None:
        if self._client_factory is None:
            return None
        try:
            sig = inspect.signature(self._client_factory)
            if len(sig.parameters) == 0:
                return self._client_factory()
            settings = VlmSettings.from_env(self._env)
            if remaining_s is not None and remaining_s > 0:
                settings = replace(settings, timeout_s=min(settings.timeout_s, remaining_s))
            return self._client_factory(settings)
        except (VlmError, ValueError, TypeError) as e:
            log.warning("VLM 클라이언트를 열지 못해 기하 후보만 반환한다: %s", e)
            return None

    def _get(self, ctx: JobContext, uri: str) -> bytes:
        assert self._store is not None
        try:
            return self._store.get(ctx, uri)
        except ObjectStoreError as e:
            raise JobFailed("OBJECT_GET_FAILED", str(e), retryable=e.retryable) from e


def _fit_cameras_to_frames(cameras: Mapping[str, CameraPose], frames: Mapping[str, Any]) -> dict[str, CameraPose]:
    """프레임이 보정 해상도(`image_size`)와 다르면 그 카메라의 `K` 를 프레임 크기에 맞춰 비례 축소한다(크롭 좌표 정합).

    프레임은 업로드 때 긴 변 1280 px 이하로 줄어 있을 수 있는데, 투영은 원본 해상도 기준 `K` 라서 그대로 쓰면 후보가 엉뚱한 곳(또는 화면 밖)으로
    투영된다. 크기를 모르거나 같으면 그대로 둔다. 왜곡 계수는 쓰지 않는다(핀홀 투영, 기존 동작).
    """
    out = dict(cameras)
    for cam_id, frame in frames.items():
        pose = cameras.get(cam_id)
        pixels = getattr(frame, "pixels", None)
        if pose is None or pixels is None or getattr(pixels, "ndim", 0) < 2:
            continue
        frame_h, frame_w = pixels.shape[:2]
        src_w, src_h = pose.image_size
        if src_w <= 0 or src_h <= 0 or (frame_w, frame_h) == (src_w, src_h):
            continue
        k = np.array(pose.K, dtype=np.float64).reshape(3, 3)
        k[0, :] *= frame_w / src_w
        k[1, :] *= frame_h / src_h
        out[cam_id] = replace(pose, K=k, image_size=(int(frame_w), int(frame_h)))
    return out


def _check_cancelled(ctx: JobContext) -> None:
    if ctx.cancelled.is_set():
        raise JobFailed("CANCELLED", "작업이 취소됐다")
