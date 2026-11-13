"""I5 `vlm_zone_draft` 처리기: 세팅 미리보기 3D 맵 기하 후보 추출 + Cosmos3-Nano 구역 초안 제안 (명세 5.3·8.2, S15P31A101-455).

- 입력은 `contracts/messages/vlm_zone_draft.schema.json#/$defs/input`, 결과는 `#/$defs/result` 로 검증한다.
- 초안은 관리자가 수락해 구성 묶음으로 발행하기 전까지 Tier 0 판정에 절대 들어가지 않는다(`safety_use: False`).
- VLM 프레임은 `gate_input_frames`(`prepare_vlm_frames`)를 거쳐 얼굴을 비식별 처리한 `VlmFrame` 만 VLM 에 넘긴다.
- 프레임(S15P31A101-479): 세팅 워커가 올린 카메라별 대표 프레임(긴 변 1280 px 이하로 줄인 JPEG)이 `frames` 로 들어온다. 보정 문서의 `K` 는
  원본 해상도(`image_size`) 기준이라, 후보를 투영해 크롭하기 전에 프레임 크기에 맞춰 `K` 를 비례 축소한다(`_fit_cameras_to_frames`).
- VLM 서버 비가용·시간 초과·응답 형식 오류 시에도 전체 초안 생성을 실패시키지 않고 결정적 기하 후보를 `"AI 의견 없음"`(`status="NO_OPINION"`)으로 돌려준다.
- 변경 의심 구역 의견(1.1.0, S15P31A101-631): 입력에 `suspected_zones` 가 있으면 초안 뒤에 구역마다 이전·이번 세팅의 같은 카메라 크롭을 VLM 에
  비교시켜 `zone_opinions` 를 낸다(`simai_ai.vlm.zone_opinion`). 이전 사진·보정을 못 읽거나 실패하면 그 구역만 `NO_OPINION`(사유)이고 작업은
  실패시키지 않는다. 이전 프레임도 같은 얼굴 처리 관문을 거치며, 고른 카메라의 장만 처리한다.
- 시연 개구부 스위치(S15P31A101-671): `SIMAI_ZONE_DRAFT_OPENING_ONLY`(기본 켬)이면 후보를 `OPENING` 으로 제한하고 단상 사이 낮은·빈 자리
  후보·가려진 바닥 빈 칸 거르기·겹침 합치기를 켠 뒤, 얼굴 처리된 프레임의 노랑·검정 줄무늬 비율로 순위를 매겨 VLM 에 '표시된 개구부인가'를
  묻는다(아니면 버림, `SIMAI_ZONE_DRAFT_MAX_OPENINGS` 개까지). 끄면 이전 동작 그대로다. 계약(입력·결과 스키마)은 바뀌지 않는다.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np

from simai_ai.calibration.document import content_hash
from simai_ai.providers.contract import schema_errors
from simai_ai.vlm.client import VlmClient, VlmError, VlmSettings
from simai_ai.vlm.face_blur import Decoder, KeypointProvider
from simai_ai.vlm.zone_draft import (
    CameraPose,
    GeometryCandidate,
    ZoneDraftOptions,
    ZoneGeometryConfig,
    assess_geometry_candidates,
    assess_opening_candidates,
    cameras_from_calibration,
    extract_geometry_candidates,
    filter_and_dedup_candidates,
    load_points_from_bytes,
)
from simai_ai.vlm.zone_opinion import (
    NoOpinionReason,
    SuspectedZone,
    ZoneOpinionConfig,
    assess_zone_opinions,
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
        opinion_config: ZoneOpinionConfig | None = None,
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
        self._opinion_config = opinion_config or ZoneOpinionConfig()
        self._env = env
        # 스위치는 처리기를 만들 때 한 번 읽는다(S15P31A101-671). 켜져 있으면 기하 설정에 개구부 전용 설정을 덧씌운다
        self._options = ZoneDraftOptions.from_env(env)
        if self._options.opening_only:
            self._geometry_config = self._options.geometry_config(geometry_config)

    def __call__(self, ctx: JobContext) -> dict[str, Any]:
        started_s = self._monotonic()
        inp = json.loads(json.dumps(ctx.input))
        if errors := schema_errors(inp, INPUT_SCHEMA):
            raise JobFailed("VLM_ZONE_DRAFT_INPUT_INVALID", "; ".join(errors))

        _check_cancelled(ctx)
        # 보정을 먼저 읽는다: 가려진 바닥 빈 칸 거르기(S15P31A101-671)가 원본 해상도 카메라로 가시성을 본다
        calibration_cameras = self._resolve_cameras(ctx, inp)
        candidates = self._resolve_candidates(ctx, inp, calibration_cameras)
        ctx.report_progress(0.4)

        _check_cancelled(ctx)
        frames_by_camera = self._resolve_frames(ctx, inp)
        cameras_by_id = _fit_cameras_to_frames(calibration_cameras, frames_by_camera)
        ctx.report_progress(0.6)

        _check_cancelled(ctx)
        vlm_client = self._open_client(ctx.remaining_s())
        if self._options.opening_only:
            draft_items, status, model_version = assess_opening_candidates(
                candidates,
                vlm_client=vlm_client,
                frames_by_camera=frames_by_camera,
                cameras_by_id=cameras_by_id,
                options=self._options,
            )
        else:
            draft_items, status, model_version = assess_geometry_candidates(
                candidates,
                vlm_client=vlm_client,
                frames_by_camera=frames_by_camera,
                cameras_by_id=cameras_by_id,
            )
        log.info(
            "job=%s opening_only=%s candidates=%s drafts=%s",
            ctx.job_id,
            self._options.opening_only,
            [(c.candidate_id, c.source) for c in candidates],
            [(d.candidate_id, d.recommended_name, d.vlm_status, d.rationale) for d in draft_items],
        )
        ctx.report_progress(0.8)

        zone_opinions: list[dict[str, Any]] | None = None
        if inp.get("suspected_zones"):
            _check_cancelled(ctx)
            zone_opinions = self._assess_opinions(ctx, inp, vlm_client, frames_by_camera, cameras_by_id)
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
            "prompt_version": self._options.prompt_version,
            "latency_ms": elapsed_ms,
            "drafts": [item.to_dict() for item in draft_items],
        }
        if zone_opinions is not None:
            result["zone_opinions"] = zone_opinions
        if errors := schema_errors(result, RESULT_SCHEMA):
            raise JobFailed("VLM_ZONE_DRAFT_OUTPUT_INVALID", "; ".join(errors))
        return result

    def _resolve_candidates(
        self, ctx: JobContext, inp: Mapping[str, Any], cameras: Mapping[str, CameraPose] | None = None
    ) -> list[GeometryCandidate]:
        raw_cands = inp.get("geometry_candidates")
        if raw_cands is not None:
            given = [GeometryCandidate.from_dict(c) for c in raw_cands]
            if self._options.opening_only and self._geometry_config is not None:
                return filter_and_dedup_candidates(given, self._geometry_config)
            return given

        uri = inp.get("pointcloud_uri") or inp.get("preview_scene_uri")
        if not uri or self._store is None:
            return []

        raw_bytes = self._get(ctx, str(uri))
        try:
            points_xyz = load_points_from_bytes(raw_bytes, str(uri))
        except ValueError as e:
            raise JobFailed("VLM_ZONE_DRAFT_SCENE_INVALID", f"{uri}: {e}") from e
        return extract_geometry_candidates(points_xyz, config=self._geometry_config, cameras=cameras)

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

    def _assess_opinions(
        self,
        ctx: JobContext,
        inp: Mapping[str, Any],
        vlm_client: Any | None,
        after_frames: Mapping[str, Any],
        after_cameras: Mapping[str, CameraPose],
    ) -> list[dict[str, Any]]:
        """변경 의심 구역 의견(S15P31A101-631). 이전 사진·보정을 못 쓰면 모든 구역이 그 사유의 NO_OPINION 이다."""
        zones = [SuspectedZone.from_dict(z) for z in inp["suspected_zones"]]
        previous_frames = inp.get("previous_frames") or []
        blocked: NoOpinionReason | None = None
        before_cameras: dict[str, CameraPose] = {}
        if not previous_frames or not inp.get("previous_calibration_ref"):
            blocked = "NO_PREVIOUS_FRAMES"
        elif not after_cameras:
            blocked = "CALIBRATION_UNAVAILABLE"
        else:
            before_cameras = self._resolve_previous_cameras(ctx, inp)
            if not before_cameras:
                blocked = "CALIBRATION_UNAVAILABLE"
        # 화면이 전·후 사진을 둘 다 보일 수 있는 카메라만 고른다(입력에 프레임이 있는 카메라)
        allowed = {str(f["camera_id"]) for f in previous_frames} & {str(f["camera_id"]) for f in inp.get("frames") or []}

        def before_frames(cameras: set[str]) -> Mapping[str, Any]:
            # 크롭 박스가 정규화 좌표라 이전 프레임은 K 를 맞출 필요가 없다
            return self._gate_frames(ctx, [f for f in previous_frames if str(f["camera_id"]) in cameras], "이전 세팅")

        opinions = assess_zone_opinions(
            zones,
            vlm_client=vlm_client,
            before_cameras=before_cameras,
            after_cameras=after_cameras,
            before_frames=before_frames,
            after_frames=after_frames,
            allowed_cameras=allowed,
            blocked_reason=blocked,
            remaining_s=ctx.remaining_s,
            cfg=self._opinion_config,
        )
        log.info(
            "job=%s zone_opinions=%s",
            ctx.job_id,
            [(o.zone_id, o.status, o.reason or o.recommended_action) for o in opinions],
        )
        return [o.to_dict() for o in opinions]

    def _resolve_previous_cameras(self, ctx: JobContext, inp: Mapping[str, Any]) -> dict[str, CameraPose]:
        """이전 세팅 보정. 읽지 못하면 빈 사전(의견만 NO_OPINION, 초안 작업은 계속)."""
        ref = str(inp["previous_calibration_ref"])
        try:
            raw = self._get(ctx, ref)
            doc = json.loads(raw)
        except (JobFailed, ValueError) as e:
            log.warning("job=%s 이전 보정 %s 을 읽지 못해 구역 의견을 건너뛴다: %s", ctx.job_id, ref, e)
            return {}
        if not isinstance(doc, Mapping):
            return {}
        expected_hash = inp.get("previous_calibration_hash")
        if expected_hash and content_hash(doc) != expected_hash:
            log.warning("job=%s 이전 보정 %s 의 content_hash 가 다르다", ctx.job_id, ref)
            return {}
        return cameras_from_calibration(doc)

    def _resolve_frames(self, ctx: JobContext, inp: Mapping[str, Any]) -> dict[str, Any]:
        return self._gate_frames(ctx, inp.get("frames") or [], "이번 세팅")

    def _gate_frames(self, ctx: JobContext, frames_in: Sequence[Mapping[str, Any]], label: str) -> dict[str, Any]:
        """프레임을 받아 얼굴 처리 관문을 거친 `VlmFrame` 을 카메라별로 돌려준다. 실패하면 빈 사전(VLM 만 건너뛴다)."""
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
            log.warning("job=%s %s 프레임 준비 실패로 VLM 없이 진행한다: %s", ctx.job_id, label, exc)
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
