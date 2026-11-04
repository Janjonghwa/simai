"""I5 `site_setup` 작업 처리기(S15P31A101-465).

대여 → 단계별 하트비트(`site_setup.schema.json#/$defs/progress`, 단조 증가) → 결과 제출(`#/$defs/result`).
단계:
(a) COLLECTING_BACKGROUND: Tier 0 사람 지운 배경 묶음 확인 (`BACKGROUND_INCOMPLETE`)
(b) INITIAL_POSE_AND_WORLD: 무마커 내부 파라미터 확인(기존 값 재사용 또는 추정)
(c) INITIAL_POSE_AND_WORLD: MapAnything 초기 복원 + World 자동 정의(바닥 Z=0, 경계 벽 +X, 모서리 원점, `FLOOR_FIT_FAILED`)
(d) INITIAL_POSE_AND_WORLD: 카메라 높이(`camera_heights_m`)로 축척 및 높이 불일치(`height_checks`) 계산
    (-462 정물 교차 확인은 `object_cross_checker` 확장 지점만 둔다)
(e) JOINT_BA_AND_VERIFY: V2 Stage A 회전 BA (`INSUFFICIENT_FEATURES`, `VALIDATION_REJECTED`)
(f) JOINT_BA_AND_VERIFY: 새 `calibration_version` 보정 문서 등록(덮어쓰지 않음)
(g) PREVIEW_AND_VLM_DRAFT → READY_FOR_ZONES: 미리보기 맵(`preview_scene_version`) 생성·등록
(h) BAKING_TEXTURE → COMPLETED: 고화질 정적 3D 굽기(`scene_version`, LIVE 중 굽기 금지 게이트 유지)
"""

from __future__ import annotations

import copy
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from simai_ai.calibration.criteria import (
    CalibrationJudgement,
    ScaleBasis,
    ScaleDecision,
)
from simai_ai.calibration.document import CameraCalibration, build_calibration_doc
from simai_ai.calibration.extrinsics.joint_ba_v2 import (
    ObjectScaleCrossChecker,
    StageAError,
    StageAResult,
    compute_height_checks,
    extract_tracks_from_backgrounds,
    run_stage_a_rotation_ba,
)
from simai_ai.calibration.world_frame.frame import (
    STATUS_OK,
    WorldFrameConfig,
    fix_world_frame,
)
from simai_ai.providers.contract import schema_errors
from simai_ai.tracking.system_health import CalibrationState

from .handlers import JobContext, JobFailed

SITE_SETUP_INPUT_SCHEMA = {
    "$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/input"
}
SITE_SETUP_PROGRESS_SCHEMA = {
    "$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/progress"
}
SITE_SETUP_RESULT_SCHEMA = {
    "$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/result"
}


@dataclass(frozen=True, slots=True)
class CameraPose:
    """카메라 외부 파라미터(World/초기 복원 좌표 → 카메라 좌표: X_cam = R @ X_w + t)."""

    camera_id: str
    R: np.ndarray
    t: np.ndarray


@dataclass(frozen=True, slots=True)
class CleanBackgroundBundle:
    """(a) 수집된 카메라별 사람 지운 배경 묶음과 선택 사전 추출 특징점 트랙."""

    bundle_id: str
    collection_pct_by_cam: Mapping[str, float]
    frames_by_cam: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    tracks_doc: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class InitialWorldReconstruction:
    """(b)~(d) 무마커 내부 파라미터 + MapAnything 초기 복원 + World 자동 정의 결과."""

    calibration_doc: dict[str, Any]
    restored_heights_m: dict[str, float]
    height_checks: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class PreviewDraftResult:
    """(g) 미리보기 맵(`preview` 모드) 및 선택 VLM 구역 초안 참조."""

    preview_scene_version: str
    zone_drafts_ref: str | None = None


class BackgroundCollector(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        site_id: str,
        camera_ids: Sequence[str],
    ) -> CleanBackgroundBundle: ...


class WorldInitializer(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        site_id: str,
        camera_ids: Sequence[str],
        camera_heights_m: Mapping[str, float],
        height_tolerance_cm: float,
        backgrounds: CleanBackgroundBundle,
        created_at_ms: int,
    ) -> InitialWorldReconstruction: ...


class CalibrationPublisher(Protocol):
    def __call__(self, calibration_doc: Mapping[str, Any]) -> str: ...


class PreviewSceneBuilder(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        site_id: str,
        calibration_doc: Mapping[str, Any],
        backgrounds: CleanBackgroundBundle,
    ) -> PreviewDraftResult: ...


class TextureSceneBaker(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        site_id: str,
        calibration_doc: Mapping[str, Any],
        backgrounds: CleanBackgroundBundle,
        preview_scene_version: str,
    ) -> str: ...


def _opt_float(value: Any) -> float | None:
    """재투영 오차는 아는 값만 싣는다(모르면 None, 지어내지 않는다; Stage A 가 홀드아웃 값으로 채운다)."""
    return None if value is None else float(value)


def build_world_from_pointcloud_and_poses(
    *,
    site_id: str,
    camera_ids: Sequence[str],
    camera_heights_m: Mapping[str, float],
    height_tolerance_cm: float,
    points_m: np.ndarray,
    unscaled_poses: Mapping[str, CameraPose],
    intrinsics_by_cam: Mapping[str, Mapping[str, Any]],
    created_at_ms: int,
    calibration_version: str | None = None,
) -> InitialWorldReconstruction:
    """Run ``estimate_world_frame`` (floor Z=0, boundary wall +X, corner origin) and scale by camera heights."""
    try:
        poses = [unscaled_poses[cid] for cid in camera_ids]
    except KeyError as exc:
        raise JobFailed(
            "FLOOR_FIT_FAILED",
            f"초기 복원 카메라 포즈 누락: {exc}",
            retryable=False,
        ) from exc

    cam2world_ma: dict[str, np.ndarray] = {}
    centers_ma: dict[str, np.ndarray] = {}
    for p in poses:
        R_ma = np.asarray(p.R, dtype=np.float64)
        t_ma = np.asarray(p.t, dtype=np.float64)
        c_ma = -R_ma.T @ t_ma
        T = np.eye(4, dtype=np.float64)
        T[:3, :3] = R_ma.T
        T[:3, 3] = c_ma
        cam2world_ma[p.camera_id] = T
        centers_ma[p.camera_id] = c_ma

    wf = fix_world_frame(
        np.asarray(points_m, dtype=np.float64),
        cam2world_ma,
        camera_heights={cid: float(camera_heights_m[cid]) for cid in camera_ids if cid in camera_heights_m},
        config=WorldFrameConfig(camera_height_max_spread=0.5),
    )
    if wf.status != STATUS_OK or wf.rotation is None or wf.origin is None or wf.scale is None:
        raise JobFailed(
            "FLOOR_FIT_FAILED",
            f"MapAnything World 바닥·축 정의 실패 ({wf.reason}): {wf.detail}",
            retryable=False,
        )

    cam_calibs: list[CameraCalibration] = []
    restored_heights: dict[str, float] = {}
    Ra = np.asarray(wf.rotation, dtype=np.float64)
    origin = np.asarray(wf.origin, dtype=np.float64)
    scale = float(wf.scale)
    for idx, cid in enumerate(camera_ids):
        cp = poses[idx]
        intr = intrinsics_by_cam[cid]
        R_ma = np.asarray(cp.R, dtype=np.float64)
        c_ma = centers_ma[cid]
        c_world = scale * (Ra @ (c_ma - origin))
        R_world = R_ma @ Ra.T
        t_world = -R_world @ c_world
        restored_heights[cid] = float(c_world[2])
        cam_calibs.append(
            CameraCalibration(
                camera_id=cid,
                image_size=tuple(intr.get("image_size", (2304, 1296))),  # type: ignore[arg-type]
                K=intr["K"],
                R=R_world.tolist(),
                t=t_world.tolist(),
                distortion_model=str(intr.get("distortion_model", "OPENCV_PINHOLE")),
                distortion_coeffs=tuple(intr.get("distortion_coeffs", (0.0, 0.0, 0.0, 0.0, 0.0))),
                reprojection_median_px=_opt_float(intr.get("reprojection_median_px")),
                reprojection_p95_px=_opt_float(intr.get("reprojection_p95_px")),
            )
        )

    height_checks = compute_height_checks(
        camera_heights_m,
        restored_heights,
        tolerance_cm=height_tolerance_cm,
    )
    digest = hashlib.sha256(f"{site_id}:{created_at_ms}".encode()).hexdigest()[:8]
    init_version = calibration_version or f"cal-{site_id}-init-{digest}"
    judgement = CalibrationJudgement(
        state=CalibrationState.PROVISIONAL,
        failures=(),
        scale=ScaleDecision(
            primary=None,
            primary_scale=None,
            cross_checks=(),
            recorded_basis=ScaleBasis.MEASURED_CAMERA_HEIGHT,
        ),
        validation={},
    )
    doc = build_calibration_doc(
        calibration_version=init_version,
        provider_source="G1_AUTO",
        provider_version="simai-site-setup-worker-0.1.0",
        created_at=created_at_ms,
        cameras=cam_calibs,
        judgement=judgement,
        fallback_scale_basis=ScaleBasis.MEASURED_CAMERA_HEIGHT,
        site_id=site_id,
        world_axes_source=wf.axes_source or "WALLS",
    )
    return InitialWorldReconstruction(
        calibration_doc=doc,
        restored_heights_m=restored_heights,
        height_checks=height_checks,
    )


class SiteSetupHandler:
    """`site_setup` I5 작업 처리기."""

    def __init__(
        self,
        *,
        background_collector: BackgroundCollector,
        world_initializer: WorldInitializer,
        calibration_publisher: CalibrationPublisher | None = None,
        preview_builder: PreviewSceneBuilder | None = None,
        texture_baker: TextureSceneBaker | None = None,
        object_cross_checker: ObjectScaleCrossChecker | None = None,
        feature_matcher: Callable[[np.ndarray, np.ndarray], Sequence[Mapping[str, Any]]] | None = None,
        live_gate: Callable[[], bool] | None = None,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        perf_counter: Callable[[], float] = time.perf_counter,
        stage_a_target_seconds: float = 10.0,
    ) -> None:
        self._collect_bg = background_collector
        self._init_world = world_initializer
        self._publish_calibration = calibration_publisher or (
            lambda doc: str(doc["calibration_version"])
        )
        self._build_preview = preview_builder or self._default_preview_builder
        self._bake_texture = texture_baker or self._default_texture_baker
        self._object_cross_checker = object_cross_checker
        self._feature_matcher = feature_matcher
        self._live_gate = live_gate or (lambda: False)
        self._clock_ms = clock_ms
        self._perf = perf_counter
        self._stage_a_target_s = float(stage_a_target_seconds)

    @staticmethod
    def _default_preview_builder(
        *,
        setup_session_id: str,
        site_id: str,
        calibration_doc: Mapping[str, Any],
        backgrounds: CleanBackgroundBundle,
    ) -> PreviewDraftResult:
        cal_ver = str(calibration_doc["calibration_version"])
        short = hashlib.sha256(
            f"{setup_session_id}:{site_id}:{cal_ver}:{backgrounds.bundle_id}:preview".encode()
        ).hexdigest()[:8]
        return PreviewDraftResult(
            preview_scene_version=f"scene-{site_id}-preview-{short}"[:160],
            zone_drafts_ref=None,
        )

    @staticmethod
    def _default_texture_baker(
        *,
        setup_session_id: str,
        site_id: str,
        calibration_doc: Mapping[str, Any],
        backgrounds: CleanBackgroundBundle,
        preview_scene_version: str,
    ) -> str:
        cal_ver = str(calibration_doc["calibration_version"])
        short = hashlib.sha256(
            f"{setup_session_id}:{site_id}:{cal_ver}:{backgrounds.bundle_id}:{preview_scene_version}:baked".encode()
        ).hexdigest()[:8]
        return f"scene-{site_id}-{short}"[:160]

    @staticmethod
    def _check_cancelled(ctx: JobContext) -> None:
        if ctx.cancelled.is_set():
            raise JobFailed("CANCELLED", "대여 취소 또는 시간 초과로 중단됨", retryable=True)
        if ctx.is_expired():
            raise JobFailed("TIMEOUT", "site_setup 실행 상한 초과", retryable=False)

    @staticmethod
    def _validate_input(raw: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...], dict[str, float], float]:
        errs = schema_errors(dict(raw), SITE_SETUP_INPUT_SCHEMA)
        if errs:
            raise JobFailed(
                "INVALID_INPUT",
                f"site_setup 입력이 계약을 위반했다: {'; '.join(errs)}",
                retryable=False,
            )
        setup_session_id = str(raw["setup_session_id"])
        site_id = str(raw["site_id"])
        camera_ids = tuple(str(cid) for cid in raw["calibration_target"]["camera_ids"])
        heights = {str(k): float(v) for k, v in raw["camera_heights_m"].items()}
        if set(heights.keys()) != set(camera_ids):
            raise JobFailed(
                "INVALID_INPUT",
                f"camera_heights_m 키({sorted(heights)})와 calibration_target.camera_ids({list(camera_ids)})가 일치하지 않는다",
                retryable=False,
            )
        tolerance_cm = float(raw["height_tolerance_cm"])
        return setup_session_id, site_id, camera_ids, heights, tolerance_cm

    @staticmethod
    def _emit_progress(ctx: JobContext, payload: Mapping[str, Any]) -> None:
        errs = schema_errors(dict(payload), SITE_SETUP_PROGRESS_SCHEMA)
        if errs:
            raise JobFailed(
                "INTERNAL_CONTRACT_ERROR",
                f"site_setup 진행 스냅샷이 계약을 위반했다: {'; '.join(errs)}",
                retryable=False,
            )
        ctx.report_site_setup(payload)

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        t_start = self._perf()
        setup_session_id, site_id, camera_ids, camera_heights_m, height_tolerance_cm = (
            self._validate_input(ctx.input)
        )
        stage_seconds: dict[str, float] = {}

        # ------------------------------------------------------------------
        # (a) COLLECTING_BACKGROUND
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        t0 = self._perf()
        bg_bundle = self._collect_bg(
            setup_session_id=setup_session_id,
            site_id=site_id,
            camera_ids=camera_ids,
        )
        bg_pct: dict[str, float] = {
            cid: round(float(bg_bundle.collection_pct_by_cam.get(cid, 0.0)), 2)
            for cid in camera_ids
        }
        incomplete = [cid for cid, pct in bg_pct.items() if pct < 100.0 - 1e-6]
        if incomplete:
            raise JobFailed(
                "BACKGROUND_INCOMPLETE",
                f"사람 지운 배경 수집 미완료 카메라: {incomplete} ({bg_pct})",
                retryable=True,
            )
        if bg_bundle.tracks_doc is None:
            missing_frames = [
                cid for cid in camera_ids if not bg_bundle.frames_by_cam.get(cid)
            ]
            if missing_frames:
                raise JobFailed(
                    "BACKGROUND_INCOMPLETE",
                    f"배경 이미지가 비어 있는 카메라: {missing_frames}",
                    retryable=True,
                )
        stage_seconds["COLLECTING_BACKGROUND"] = round(
            max(0.0, self._perf() - t0), 4
        )
        self._emit_progress(
            ctx,
            {
                "setup_session_id": setup_session_id,
                "status": "COLLECTING_BACKGROUND",
                "progress_pct": 15,
                "background_collection_pct": bg_pct,
                "metrics": {"stage_seconds": dict(stage_seconds)},
            },
        )

        # ------------------------------------------------------------------
        # (b)~(d) INITIAL_POSE_AND_WORLD (무마커 내부 파라미터 + MapAnything + 바닥 Z=0 + 카메라 높이 축척)
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        t0 = self._perf()
        try:
            init_world = self._init_world(
                setup_session_id=setup_session_id,
                site_id=site_id,
                camera_ids=camera_ids,
                camera_heights_m=camera_heights_m,
                height_tolerance_cm=height_tolerance_cm,
                backgrounds=bg_bundle,
                created_at_ms=int(self._clock_ms()),
            )
        except JobFailed:
            raise
        except ValueError as exc:
            raise JobFailed(
                "FLOOR_FIT_FAILED",
                f"초기 World 복원 및 바닥 평면 맞춤 실패: {exc}",
                retryable=False,
            ) from exc

        init_height_checks = compute_height_checks(
            camera_heights_m,
            init_world.restored_heights_m,
            tolerance_cm=height_tolerance_cm,
        )
        stage_seconds["INITIAL_POSE_AND_WORLD"] = round(
            max(0.0, self._perf() - t0), 4
        )
        self._emit_progress(
            ctx,
            {
                "setup_session_id": setup_session_id,
                "status": "INITIAL_POSE_AND_WORLD",
                "progress_pct": 40,
                "background_collection_pct": bg_pct,
                "height_checks": init_height_checks,
                "metrics": {"stage_seconds": dict(stage_seconds)},
            },
        )

        # ------------------------------------------------------------------
        # (e)~(f) JOINT_BA_AND_VERIFY (V2 Stage A 회전 BA + 새 버전 보정 등록)
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        t0 = self._perf()
        try:
            tracks_source = (
                bg_bundle.tracks_doc
                if bg_bundle.tracks_doc is not None
                else extract_tracks_from_backgrounds(
                    bg_bundle.frames_by_cam,
                    init_world.calibration_doc,
                    matcher=self._feature_matcher,
                )
            )
            stage_a: StageAResult = run_stage_a_rotation_ba(
                init_world.calibration_doc,
                tracks_source,
                camera_heights_m=camera_heights_m,
                height_tolerance_cm=height_tolerance_cm,
                created_at_ms=int(self._clock_ms()),
                stage_target_seconds=self._stage_a_target_s,
                object_cross_checker=self._object_cross_checker,
            )
        except StageAError as exc:
            raise JobFailed(exc.code, exc.message, retryable=exc.retryable) from exc

        calibration_version = str(
            self._publish_calibration(copy.deepcopy(stage_a.calibration_doc))
        )
        stage_seconds["JOINT_BA_AND_VERIFY"] = round(
            max(0.0, self._perf() - t0), 4
        )
        joint_ba_snapshot: dict[str, Any] = {
            "setup_session_id": setup_session_id,
            "status": "JOINT_BA_AND_VERIFY",
            "progress_pct": 65,
            "background_collection_pct": bg_pct,
            "calibration_version": calibration_version,
            "height_checks": stage_a.height_checks,
            "metrics": {
                "stage_seconds": dict(stage_seconds),
                "joint_ba": stage_a.summary_metrics,
            },
        }
        if stage_a.object_cross_checks:
            joint_ba_snapshot["object_cross_checks"] = stage_a.object_cross_checks
        self._emit_progress(ctx, joint_ba_snapshot)

        # ------------------------------------------------------------------
        # (g) PREVIEW_AND_VLM_DRAFT → READY_FOR_ZONES
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        t0 = self._perf()
        preview = self._build_preview(
            setup_session_id=setup_session_id,
            site_id=site_id,
            calibration_doc=stage_a.calibration_doc,
            backgrounds=bg_bundle,
        )
        stage_seconds["PREVIEW_AND_VLM_DRAFT"] = round(
            max(0.0, self._perf() - t0), 4
        )
        preview_snapshot: dict[str, Any] = {
            "setup_session_id": setup_session_id,
            "status": "PREVIEW_AND_VLM_DRAFT",
            "progress_pct": 80,
            "background_collection_pct": bg_pct,
            "calibration_version": calibration_version,
            "preview_scene_version": preview.preview_scene_version,
            "height_checks": stage_a.height_checks,
            "metrics": {
                "stage_seconds": dict(stage_seconds),
                "joint_ba": stage_a.summary_metrics,
            },
        }
        if preview.zone_drafts_ref is not None:
            preview_snapshot["zone_drafts_ref"] = preview.zone_drafts_ref
        if stage_a.object_cross_checks:
            preview_snapshot["object_cross_checks"] = stage_a.object_cross_checks
        self._emit_progress(ctx, preview_snapshot)

        t0_ready = self._perf()
        stage_seconds["READY_FOR_ZONES"] = round(
            max(0.0, self._perf() - t0_ready), 4
        )
        ready_snapshot = {
            **preview_snapshot,
            "status": "READY_FOR_ZONES",
            "progress_pct": 85,
            "metrics": {
                "stage_seconds": dict(stage_seconds),
                "joint_ba": stage_a.summary_metrics,
            },
        }
        self._emit_progress(ctx, ready_snapshot)

        # ------------------------------------------------------------------
        # (h) BAKING_TEXTURE → COMPLETED (LIVE 중 고화질 굽기 금지 게이트 유지)
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        if self._live_gate():
            raise JobFailed(
                "LIVE_EXP_BLOCKED",
                "LIVE 관제 중에는 고화질 텍스처 굽기(BAKING_TEXTURE)를 실행하지 않는다",
                retryable=True,
            )
        t0 = self._perf()
        baking_snapshot = {
            **ready_snapshot,
            "status": "BAKING_TEXTURE",
            "progress_pct": 95,
        }
        self._emit_progress(ctx, baking_snapshot)

        scene_version = str(
            self._bake_texture(
                setup_session_id=setup_session_id,
                site_id=site_id,
                calibration_doc=stage_a.calibration_doc,
                backgrounds=bg_bundle,
                preview_scene_version=preview.preview_scene_version,
            )
        )
        stage_seconds["BAKING_TEXTURE"] = round(
            max(0.0, self._perf() - t0), 4
        )
        total_seconds = round(max(0.0, self._perf() - t_start), 4)

        result: dict[str, Any] = {
            "setup_session_id": setup_session_id,
            "status": "COMPLETED",
            "progress_pct": 100,
            "background_collection_pct": bg_pct,
            "calibration_version": calibration_version,
            "preview_scene_version": preview.preview_scene_version,
            "scene_version": scene_version,
            "height_checks": stage_a.height_checks,
            "metrics": {
                "stage_seconds": dict(stage_seconds),
                "total_seconds": total_seconds,
                "joint_ba": stage_a.summary_metrics,
            },
        }
        if preview.zone_drafts_ref is not None:
            result["zone_drafts_ref"] = preview.zone_drafts_ref
        if stage_a.object_cross_checks:
            result["object_cross_checks"] = stage_a.object_cross_checks

        errs = schema_errors(result, SITE_SETUP_RESULT_SCHEMA)
        if errs:
            raise JobFailed(
                "INTERNAL_CONTRACT_ERROR",
                f"site_setup 결과가 계약을 위반했다: {'; '.join(errs)}",
                retryable=False,
            )
        return result
