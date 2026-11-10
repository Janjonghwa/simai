"""I5 `site_setup` 작업 처리기(S15P31A101-465).

대여 → 단계별 하트비트(`site_setup.schema.json#/$defs/progress`, 단조 증가) → 결과 제출(`#/$defs/result`).
단계:
(a) COLLECTING_BACKGROUND: Tier 0 사람 지운 배경 묶음 확인 (`BACKGROUND_INCOMPLETE`)
(b) INITIAL_POSE_AND_WORLD: 무마커 내부 파라미터 확인(기존 값 재사용 또는 추정)
(c) INITIAL_POSE_AND_WORLD: MapAnything 초기 복원 + World 자동 정의(바닥 Z=0, 경계 벽 +X, 모서리 원점, `FLOOR_FIT_FAILED`)
(d) INITIAL_POSE_AND_WORLD: 카메라 높이(`camera_heights_m`)로 축척 및 높이 불일치(`height_checks`) 계산
    (-462 정물 교차 확인은 `object_cross_checker` 확장 지점만 둔다)
(e) JOINT_BA_AND_VERIFY: V2 Stage A 회전 BA (`INSUFFICIENT_FEATURES`, `VALIDATION_REJECTED`)
(f) JOINT_BA_AND_VERIFY: (선택 `world_anchor`) 카메라가 움직이지 않았으면 새 보정을 이전 채택 보정의 World 로 옮긴 뒤
    새 `calibration_version` 보정 문서 등록(덮어쓰지 않음). 미리보기·굽기·map_cameras 는 이 문서를 기준으로 정렬한다(S15P31A101-584)
(g) PREVIEW_AND_VLM_DRAFT → READY_FOR_ZONES: 미리보기 맵(`preview_scene_version`) 생성·등록 + 카메라별 대표 프레임 업로드·등록
    (`artifacts.camera_frames`, S15P31A101-479: 서버가 이 스냅샷으로 AI 구역 초안 작업을 만들 때 VLM 크롭 입력으로 싣는다)
    + 미리보기·굽기에 쓰는 카메라별 사람 지운 배경 사진 업로드(`artifacts.bake_images`, S15P31A101-593: 서버가 이 대여의
    미리보기·정식 Scene 에 묶어 관제 카메라 시점 VDT 사진으로 서명해 준다)
(h) BAKING_TEXTURE → COMPLETED: 고화질 정적 3D 굽기(`scene_version`). 세팅은 서버가 진행 중 작업(IN_PROGRESS)이 없을 때만
    시작시키므로(-467, 409) Tier 0 표시 파일이 있어도 굽는다(`live_notice` 는 로그만). `live_gate` 는 "LIVE 작업 중" 신호를
    받을 자리로 남긴다(지금 서버가 워커에 주는 그런 신호는 없다, S15P31A101-473)

(a) 는 Tier 0 묶음이 완성될 때까지 `background_wait_s` 동안 `background_poll_s` 마다 다시 보고, 그동안 카메라별 수집률을
COLLECTING_BACKGROUND 하트비트로 보낸다. 보정 문서의 생성 시각(`created_at`)은 배경 묶음의 마지막 관측 시각
(`CleanBackgroundBundle.observed_until_ms`)으로 정해 재시도해도 같은 입력이면 같은 문서(sha256)가 나온다(S15P31A101-473).

산출물을 객체 저장소에 올린 주입점은 그 객체 참조(`ObjectRef`)를 함께 돌려주고, 처리기는 그 뒤 스냅샷·결과의 선택 `artifacts`
(site_setup 1.2.0, S15P31A101-467)에 싣는다. EC2 는 이 참조로 문서·GLB 를 확인해 새 보정·Scene 버전을 등록한다(서버 등록 경로).
버전 문자열만 돌려주는 주입점(시험·기본값)은 그대로 받고 `artifacts` 를 싣지 않는다.

카메라 프레임(`frame_publisher`)은 AI 의견을 위한 보조 산출물이다. 올리지 못해도(저장소 오류 등) 세팅을 실패시키지 않고 프레임 없이
이어가며(초안은 \"AI 의견 없음\"), 취소·시간 초과만 그대로 낸다. 프레임은 사람 지운 배경이라 얼굴이 원칙상 없지만 계약의 얼굴 정책
(`face_policy`, BLURRED)을 함께 싣고, VLM 워커가 그 정책대로 얼굴 흐림 관문을 거친다.

굽기 입력 사진(`bake_image_publisher`, S15P31A101-593)은 VDT(Visual 층, 장식)용 보조 산출물이다. 카메라 프레임과 같이 올리지 못해도
세팅을 실패시키지 않고 사진 없이 이어간다(취소·시간 초과만 그대로). 같은 사진이 이미 GLB 텍스처로 구워졌으므로 얼굴 정책은 외형
텍스처 규칙(AGENTS 규칙 8, 기본 OBSERVED)을 따른다.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
import threading
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

log = logging.getLogger("services.worker")

MAX_CAMERA_FRAMES = 8  # site_setup#/$defs/artifacts/camera_frames maxItems = vlm_zone_draft frames maxItems
MAX_BAKE_IMAGES = 16  # site_setup#/$defs/bake_images/images maxItems = scene_camera_images images maxItems
BAKE_BUNDLE_ID_RE = re.compile(r"^bundle-[0-9a-f]{32}$")  # site_setup#/$defs/bake_images/bundle_id

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
    # 묶음의 마지막 관측 시각(UTC ms). 있으면 보정 문서 created_at 으로 쓴다(재시도 결정론). 모르면 None → 처리기 시계
    observed_until_ms: int | None = None

    def missing_cameras(self, camera_ids: Sequence[str]) -> list[str]:
        """아직 쓸 수 없는 카메라(수집률 100 미만, 또는 트랙 문서가 없는데 배경 이미지가 비었음)."""
        out = []
        for cid in camera_ids:
            pct = float(self.collection_pct_by_cam.get(cid, 0.0))
            if pct < 100.0 - 1e-6 or (self.tracks_doc is None and not self.frames_by_cam.get(cid)):
                out.append(cid)
        return out


@dataclass(frozen=True, slots=True)
class InitialWorldReconstruction:
    """(b)~(d) 무마커 내부 파라미터 + MapAnything 초기 복원 + World 자동 정의 결과."""

    calibration_doc: dict[str, Any]
    restored_heights_m: dict[str, float]
    height_checks: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """이 작업·이 대여의 서명 PUT 으로 올린 객체(계약 site_setup#/$defs/object_ref)."""

    uri: str
    sha256: str  # 올린 바이트의 SHA-256 16진수(서명에 묶은 값)
    size_bytes: int

    @classmethod
    def of(cls, uri: str, content: bytes) -> ObjectRef:
        return cls(uri, hashlib.sha256(content).hexdigest(), len(content))

    def to_json(self) -> dict[str, Any]:
        return {"uri": self.uri, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True, slots=True)
class CameraFrameRef:
    """(g) 카메라 하나의 대표 프레임(JPEG)과 올린 객체 참조(계약 site_setup#/$defs/camera_frame_ref)."""

    camera_id: str
    ref: ObjectRef
    face_policy: str = "BLURRED"  # VLM 입력 얼굴 정책(OBSERVED 금지)

    def to_json(self) -> dict[str, Any]:
        return {"camera_id": self.camera_id, **self.ref.to_json(), "face_policy": self.face_policy}


@dataclass(frozen=True, slots=True)
class BakeImageRef:
    """(g) 카메라 하나의 굽기 입력 사진(JPEG)과 올린 객체 참조(계약 site_setup#/$defs/bake_image_ref)."""

    camera_id: str
    ref: ObjectRef
    face_policy: str = "OBSERVED"  # GLB 텍스처와 같은 사진이라 외형 텍스처 규칙(AGENTS 규칙 8)

    def to_json(self) -> dict[str, Any]:
        return {"camera_id": self.camera_id, **self.ref.to_json(), "face_policy": self.face_policy}


@dataclass(frozen=True, slots=True)
class BakeImages:
    """(g) 이 대여의 미리보기·굽기 입력 사진 묶음(계약 site_setup#/$defs/bake_images)."""

    bundle_id: str
    images: tuple[BakeImageRef, ...]

    def to_json(self) -> dict[str, Any]:
        return {"bundle_id": self.bundle_id, "images": [image.to_json() for image in self.images]}


@dataclass(frozen=True, slots=True)
class PublishedCalibration:
    """(f) 올린 새 보정 문서의 버전과 객체 참조."""

    calibration_version: str
    document: ObjectRef | None = None


@dataclass(frozen=True, slots=True)
class BakedScene:
    """(h) 굽기로 만든 정식 Scene 버전과 GLB 객체 참조."""

    scene_version: str
    scene_asset: ObjectRef | None = None


@dataclass(frozen=True, slots=True)
class PreviewDraftResult:
    """(g) 미리보기 맵(`preview` 모드) 및 선택 VLM 구역 초안 참조."""

    preview_scene_version: str
    zone_drafts_ref: str | None = None
    scene_asset: ObjectRef | None = None


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
    def __call__(self, calibration_doc: Mapping[str, Any]) -> str | PublishedCalibration: ...


class PreviewSceneBuilder(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        site_id: str,
        calibration_doc: Mapping[str, Any],
        backgrounds: CleanBackgroundBundle,
    ) -> PreviewDraftResult: ...


class CameraFramePublisher(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        camera_ids: Sequence[str],
        backgrounds: CleanBackgroundBundle,
    ) -> Sequence[CameraFrameRef]: ...


class BakeImagePublisher(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        camera_ids: Sequence[str],
        backgrounds: CleanBackgroundBundle,
    ) -> BakeImages | None: ...


class TextureSceneBaker(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        site_id: str,
        calibration_doc: Mapping[str, Any],
        backgrounds: CleanBackgroundBundle,
        preview_scene_version: str,
    ) -> str | BakedScene: ...


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
        floor_tilt_correction=wf.floor_tilt_correction,
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
        frame_publisher: CameraFramePublisher | None = None,
        bake_image_publisher: BakeImagePublisher | None = None,
        object_cross_checker: ObjectScaleCrossChecker | None = None,
        feature_matcher: Callable[[np.ndarray, np.ndarray], Sequence[Mapping[str, Any]]] | None = None,
        live_gate: Callable[[], bool] | None = None,
        live_notice: Callable[[], bool] | None = None,
        world_anchor: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        perf_counter: Callable[[], float] = time.perf_counter,
        stage_a_target_seconds: float = 10.0,
        background_wait_s: float = 0.0,
        background_poll_s: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[threading.Event, float], Any] | None = None,
    ) -> None:
        """live_gate: True 면 굽기를 `LIVE_EXP_BLOCKED`(재시도 가능)로 미룬다 — "LIVE 작업 중" 신호 자리(운영 조립은 넘기지 않는다).
        live_notice: True 면 Tier 0 가 떠 있다는 로그만 남기고 굽는다(Tier 0 지연이 잠깐 늘 수 있음).
        background_wait_s: 배경 묶음이 완성되기를 기다리는 최대 초(0 이면 한 번만 본다). sleep(취소 신호, 초)의 기본은 신호 대기.
        world_anchor: Stage A 뒤 보정 문서 → 이전 채택 World 로 옮긴(또는 사유만 기록한) 문서. 없으면 새 World 그대로(-584)."""
        if background_wait_s < 0 or background_poll_s <= 0:
            raise ValueError("background_wait_s 는 0 이상, background_poll_s 는 양수다")
        self._collect_bg = background_collector
        self._init_world = world_initializer
        self._publish_calibration = calibration_publisher or (
            lambda doc: str(doc["calibration_version"])
        )
        self._build_preview = preview_builder or self._default_preview_builder
        self._bake_texture = texture_baker or self._default_texture_baker
        self._publish_frames = frame_publisher
        self._publish_bake_images = bake_image_publisher
        self._object_cross_checker = object_cross_checker
        self._feature_matcher = feature_matcher
        self._live_gate = live_gate or (lambda: False)
        self._live_notice = live_notice or (lambda: False)
        self._anchor_world = world_anchor
        self._bg_wait_s = float(background_wait_s)
        self._bg_poll_s = float(background_poll_s)
        self._monotonic = monotonic
        self._sleep = sleep or (lambda event, seconds: event.wait(seconds))
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

    def _wait_for_backgrounds(
        self, ctx: JobContext, setup_session_id: str, site_id: str, camera_ids: tuple[str, ...],
    ) -> tuple[CleanBackgroundBundle, dict[str, float]]:
        """(a) 묶음이 완성될 때까지 최대 background_wait_s 동안 다시 본다. 수집률이 바뀌면 COLLECTING_BACKGROUND 하트비트.

        진행률은 0~14 구간에서 단조 증가(서버가 같은 대여의 감소를 거절)하고, 카메라별 수집률은 실제 값을 그대로 싣는다
        (활성 보정이 바뀌어 묶음을 새로 모으면 내려갈 수 있다). 시간이 다하면 재시도 가능한 BACKGROUND_INCOMPLETE."""
        deadline = self._monotonic() + self._bg_wait_s
        best_pct = 0.0
        last_sent: tuple[float, tuple[float, ...]] | None = None
        while True:
            self._check_cancelled(ctx)
            bundle = self._collect_bg(setup_session_id=setup_session_id, site_id=site_id, camera_ids=camera_ids)
            pct = {cid: round(min(100.0, max(0.0, float(bundle.collection_pct_by_cam.get(cid, 0.0)))), 2)
                   for cid in camera_ids}
            missing = bundle.missing_cameras(camera_ids)
            if not missing:
                return bundle, pct
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise JobFailed(
                    "BACKGROUND_INCOMPLETE",
                    f"사람 지운 배경 수집 미완료 카메라: {missing} ({pct}), {self._bg_wait_s:g}s 대기",
                    retryable=True,
                )
            best_pct = max(best_pct, round(14.0 * sum(pct.values()) / (100.0 * len(pct)), 1))
            key = (best_pct, tuple(pct[c] for c in camera_ids))
            if key != last_sent:
                self._emit_progress(ctx, {
                    "setup_session_id": setup_session_id,
                    "status": "COLLECTING_BACKGROUND",
                    "progress_pct": best_pct,
                    "background_collection_pct": pct,
                })
                last_sent = key
            self._sleep(ctx.cancelled, min(self._bg_poll_s, remaining))

    def _publish_camera_frames(
        self, ctx: JobContext, setup_session_id: str, camera_ids: tuple[str, ...], bundle: CleanBackgroundBundle,
    ) -> list[CameraFrameRef]:
        """(g) 카메라별 대표 프레임을 올려 `artifacts.camera_frames` 로 알린다. 못 올리면 프레임 없이 이어간다(AI 의견 없음).

        취소·시간 초과는 그대로 낸다. 그 밖의 실패는 로그만 남긴다(구역 초안은 프레임 없이도 기하 후보를 낸다).
        """
        if self._publish_frames is None:
            return []
        try:
            frames = list(self._publish_frames(setup_session_id=setup_session_id, camera_ids=camera_ids, backgrounds=bundle))
        except JobFailed as exc:
            self._check_cancelled(ctx)
            log.warning("작업 %s: 카메라 프레임을 올리지 못해 AI 구역 초안은 프레임 없이 진행한다(%s)", ctx.job_id, exc)
            return []
        known = set(camera_ids)
        seen: set[str] = set()
        out: list[CameraFrameRef] = []
        for frame in frames:
            if frame.camera_id in known and frame.camera_id not in seen:
                seen.add(frame.camera_id)
                out.append(frame)
        return out[:MAX_CAMERA_FRAMES]  # 계약 camera_frames maxItems

    def _publish_bake_inputs(
        self, ctx: JobContext, setup_session_id: str, camera_ids: tuple[str, ...], bundle: CleanBackgroundBundle,
    ) -> BakeImages | None:
        """(g) 미리보기·굽기에 쓰는 카메라별 배경 사진을 올려 `artifacts.bake_images` 로 알린다. 못 올리면 사진 없이 이어간다.

        VDT(Visual 층) 장식용이라 어떤 실패도 세팅을 멈추지 않는다(로그만). 취소·시간 초과만 그대로 낸다.
        묶음 ID 가 계약 형식이 아니면 어느 묶음의 사진인지 밝힐 수 없어 싣지 않는다(지어내지 않는다).
        """
        if self._publish_bake_images is None:
            return None
        try:
            published = self._publish_bake_images(
                setup_session_id=setup_session_id, camera_ids=camera_ids, backgrounds=bundle,
            )
        except Exception as exc:  # noqa: BLE001 - 장식 층이라 어떤 실패도 세팅을 멈추지 않는다
            self._check_cancelled(ctx)
            log.warning("작업 %s: 굽기 입력 사진을 올리지 못해 카메라 시점 VDT 사진 없이 진행한다(%s: %s)",
                        ctx.job_id, type(exc).__name__, exc)
            return None
        if published is None:
            return None
        if not BAKE_BUNDLE_ID_RE.match(published.bundle_id or ""):
            log.warning("작업 %s: 배경 묶음 ID 형식을 몰라 굽기 입력 사진을 싣지 않는다", ctx.job_id)
            return None
        known = set(camera_ids)
        seen: set[str] = set()
        images: list[BakeImageRef] = []
        for image in published.images:
            if image.camera_id in known and image.camera_id not in seen:
                seen.add(image.camera_id)
                images.append(image)
        if not images:
            return None
        return BakeImages(published.bundle_id, tuple(images[:MAX_BAKE_IMAGES]))  # 계약 images maxItems

    def _created_at_ms(self, bundle: CleanBackgroundBundle) -> int:
        """보정 문서 생성 시각: 묶음의 마지막 관측 시각(같은 묶음 → 같은 값). 모르면 처리기 시계(재시도 결정론 없음, 로그)."""
        observed = bundle.observed_until_ms
        if isinstance(observed, int) and not isinstance(observed, bool) and observed > 0:
            return observed
        log.info("배경 묶음 %s 의 관측 시각을 몰라 보정 생성 시각에 지금 시각을 쓴다(재시도하면 다른 문서가 될 수 있다)",
                 bundle.bundle_id or "?")
        return int(self._clock_ms())

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
        bg_bundle, bg_pct = self._wait_for_backgrounds(ctx, setup_session_id, site_id, camera_ids)
        created_at_ms = self._created_at_ms(bg_bundle)
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
                created_at_ms=created_at_ms,
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
                created_at_ms=created_at_ms,
                stage_target_seconds=self._stage_a_target_s,
                object_cross_checker=self._object_cross_checker,
                backgrounds_by_cam=bg_bundle.frames_by_cam if bg_bundle.frames_by_cam else None,
            )
        except StageAError as exc:
            raise JobFailed(exc.code, exc.message, retryable=exc.retryable) from exc

        # 등록·미리보기·굽기가 같은 World 를 쓰도록 여기서 한 번만 이전 World 로 옮긴다(-584)
        calibration_doc = stage_a.calibration_doc
        if self._anchor_world is not None:
            calibration_doc = dict(self._anchor_world(copy.deepcopy(calibration_doc)))
        published = self._publish_calibration(copy.deepcopy(calibration_doc))
        artifacts: dict[str, Any] = {}
        if isinstance(published, PublishedCalibration):
            calibration_version = published.calibration_version
            if published.document is not None:
                artifacts["calibration_document"] = published.document.to_json()
        else:
            calibration_version = str(published)
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
        if stage_a.scale_source is not None:
            joint_ba_snapshot["scale_source"] = stage_a.scale_source
        if stage_a.scale_estimation is not None:
            joint_ba_snapshot["scale_estimation"] = stage_a.scale_estimation
        if stage_a.object_cross_checks:
            joint_ba_snapshot["object_cross_checks"] = stage_a.object_cross_checks
        if artifacts:
            joint_ba_snapshot["artifacts"] = dict(artifacts)
        self._emit_progress(ctx, joint_ba_snapshot)

        # ------------------------------------------------------------------
        # (g) PREVIEW_AND_VLM_DRAFT → READY_FOR_ZONES
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        t0 = self._perf()
        preview = self._build_preview(
            setup_session_id=setup_session_id,
            site_id=site_id,
            calibration_doc=calibration_doc,
            backgrounds=bg_bundle,
        )
        camera_frames = self._publish_camera_frames(ctx, setup_session_id, camera_ids, bg_bundle)
        # 미리보기·굽기가 읽는 같은 사진. 서버가 미리보기를 현재 Scene 으로 올리기(READY_FOR_ZONES) 전에 등록돼야 관제 화면이
        # 그 Scene 의 사진을 처음 물을 때 받는다. 서버는 대여로 미리보기·정식 Scene 둘 다에 묶는다
        bake_images = self._publish_bake_inputs(ctx, setup_session_id, camera_ids, bg_bundle)
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
        if stage_a.scale_source is not None:
            preview_snapshot["scale_source"] = stage_a.scale_source
        if stage_a.scale_estimation is not None:
            preview_snapshot["scale_estimation"] = stage_a.scale_estimation
        if preview.zone_drafts_ref is not None:
            preview_snapshot["zone_drafts_ref"] = preview.zone_drafts_ref
        if stage_a.object_cross_checks:
            preview_snapshot["object_cross_checks"] = stage_a.object_cross_checks
        if preview.scene_asset is not None:
            artifacts["preview_scene"] = preview.scene_asset.to_json()
        if camera_frames:
            artifacts["camera_frames"] = [frame.to_json() for frame in camera_frames]
        if bake_images is not None:
            artifacts["bake_images"] = bake_images.to_json()
        if artifacts:
            preview_snapshot["artifacts"] = dict(artifacts)
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
        if self._live_notice():
            # 세팅은 진행 중 작업이 없을 때만 시작한다(서버 409). Tier 0 는 대기 관제로 떠 있을 뿐이라 굽기를 미루지 않는다
            log.warning("작업 %s: Tier 0 가 떠 있지만 세팅 굽기를 진행한다(굽는 동안 Tier 0 지연이 잠깐 늘 수 있다)", ctx.job_id)
        t0 = self._perf()
        baking_snapshot = {
            **ready_snapshot,
            "status": "BAKING_TEXTURE",
            "progress_pct": 95,
        }
        self._emit_progress(ctx, baking_snapshot)

        baked = self._bake_texture(
            setup_session_id=setup_session_id,
            site_id=site_id,
            calibration_doc=calibration_doc,
            backgrounds=bg_bundle,
            preview_scene_version=preview.preview_scene_version,
        )
        if isinstance(baked, BakedScene):
            scene_version = baked.scene_version
            if baked.scene_asset is not None:
                artifacts["scene"] = baked.scene_asset.to_json()
        else:
            scene_version = str(baked)
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
        if stage_a.scale_source is not None:
            result["scale_source"] = stage_a.scale_source
        if stage_a.scale_estimation is not None:
            result["scale_estimation"] = stage_a.scale_estimation
        if preview.zone_drafts_ref is not None:
            result["zone_drafts_ref"] = preview.zone_drafts_ref
        if stage_a.object_cross_checks:
            result["object_cross_checks"] = stage_a.object_cross_checks
        if artifacts:
            result["artifacts"] = dict(artifacts)

        errs = schema_errors(result, SITE_SETUP_RESULT_SCHEMA)
        if errs:
            raise JobFailed(
                "INTERNAL_CONTRACT_ERROR",
                f"site_setup 결과가 계약을 위반했다: {'; '.join(errs)}",
                retryable=False,
            )
        return result
