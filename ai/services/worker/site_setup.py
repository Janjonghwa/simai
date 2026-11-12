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
    + 바닥 감시 범위(`artifacts.floor_coverage`, S15P31A101-606: 미리보기 단계가 같은 점구름·새 보정으로 바닥 칸마다 사람이 통째로
    보이는 카메라 수를 세어 둔 표시용 자료. 서버가 보정 버전에 묶어 관제 화면 바닥 겹침으로 준다)
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

보정 품질 게이트(S15P31A101-646): (e) 뒤 Stage A 쌍별 홀드아웃 절대 상한·최소 홀드아웃 수·카메라 높이 차를 본다
(`joint_ba_v2.evaluate_quality_gate`). FAILED 면 문제 쌍·카메라를 실은 `quality_gate` 스냅샷을 보내고 `VALIDATION_REJECTED` 로
끝낸다(보정을 등록하지 않는다). NEEDS_CHECK(홀드아웃 부족) 쌍은 아래 카메라 확인의 필수 쌍이 된다.
카메라 확인(S15P31A101-646): (f) 보정 등록 뒤 `camera_check` 실행기가 같은 Pod 의 Tier 0 에 관제 view 를 요청하고, 사람이 걷는
동안 관제와 같은 계산(쌍 에피폴라·잔차 통과율, 한 카메라 제외 재투영, 4대 관측)으로 새 보정을 확인해 스냅샷 `camera_check` 로
알린다. 미리보기·굽기와 함께 돌고(배경 수집은 이미 끝났다), 결과를 내기 전에 판정(PASSED·FAILED)을 기다린다. 마감까지 못 하면
INCOMPLETE. 서버는 PASSED 전에는 구역 단계·세팅 완료를 거부한다.

보조 촬영(입력 `supplement_media`, S15P31A101-605)은 (h) 굽기 주입점이 처리하고 `BakedScene.supplement` 요약을 돌려주면 결과
`supplement` 로 싣는다. 보조 촬영 때문에 세팅을 실패시키지 않는다(주입점이 FALLBACK·SKIPPED 요약으로 바꾼다).
`mode: SUPPLEMENT_REBAKE` 는 (a)~(g) 를 건너뛰고 `rebaker` 주입점 하나로 이 세션 맵만 보조 촬영을 넣어 다시 굽는다(BAKING_TEXTURE →
COMPLETED). 새 보정·미리보기를 만들지 않고, 보조 촬영이 적용되지 않으면 새 Scene 도 올리지 않는다(결과 scene_version = 기준).
`mode: REUSE_SCENE`(이전 모델 재사용, S15P31A101-614)은 (a)~(f) 를 그대로 하고, (f) 의 World 다시 맞추기가 채택되면 (g)·(h) 를
건너뛰어 JOINT_BA_AND_VERIFY → READY_FOR_ZONES → COMPLETED 로 새 보정만 낸다(현재 관제 맵 유지, 보정 문서 artifacts·높이 확인은
그대로 싣는다). 거부되거나 기록이 없으면 같은 작업 안에서 FULL_SETUP 으로 이어 가고 그 스냅샷부터 `mode: FULL_SETUP` +
`reuse_fallback_reason` 을 싣는다. 모든 모드에서 보정 문서에 world_anchor 가 있으면 그 요약(`world_anchor`)을 (f) 스냅샷부터 싣는다.

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
    QUALITY_FAILED,
    ObjectScaleCrossChecker,
    StageAError,
    StageAResult,
    compute_height_checks,
    evaluate_quality_gate,
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
SITE_SETUP_FLOOR_COVERAGE_SCHEMA = {
    "$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/floor_coverage"
}


def floor_coverage_problems(doc: Any) -> list[str]:
    """바닥 감시 범위 문서(S15P31A101-606)가 계약에 맞지 않는 이유. 스키마 검사에 칸 수(cols × rows = cells 길이)를 더한다.

    스냅샷 전체가 계약 검사를 받으므로(어기면 INTERNAL_CONTRACT_ERROR) 표시용 자료 하나가 세팅을 멈추지 않게 먼저 거른다.
    """
    errs = schema_errors(doc, SITE_SETUP_FLOOR_COVERAGE_SCHEMA)
    if errs:
        return errs
    if len(doc["cells"]) != doc["cols"] * doc["rows"]:
        return [f"cells 길이 {len(doc['cells'])} 이 cols × rows = {doc['cols'] * doc['rows']} 와 다르다"]
    return []


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


BAKE_SKIPPED_LIVE_WORK = "LIVE_WORK_STARTED"  # site_setup 1.7.0 bake_skipped(-597)

MODE_REUSE_SCENE = "REUSE_SCENE"  # site_setup 1.11.0 입력 mode(-614): 현재 관제 맵을 그대로 쓰고 보정만 다시 한다
NO_ANCHOR_REFERENCE = "NO_REFERENCE"  # site_setup 1.11.0 world_anchor_summary.reason: 보정 문서에 world_anchor 가 없다


def world_anchor_summary(calibration_doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """보정 문서 world_anchor(-584)의 진행·결과 요약(site_setup 1.11.0 world_anchor_summary). 기록이 없으면 None.

    채택 여부는 문서 그대로 따른다. 기준 버전·최대값을 모르면 null·생략으로 둔다(지어내지 않는다)."""
    rec = calibration_doc.get("world_anchor")
    if not isinstance(rec, Mapping):
        return None
    accepted = rec.get("accepted") is True
    reference = rec.get("reference_calibration_version")
    out: dict[str, Any] = {
        "accepted": accepted,
        "reason": str(rec.get("reason") or ("ANCHORED" if accepted else NO_ANCHOR_REFERENCE)),
        "reference_calibration_version": str(reference) if reference else None,
    }
    for key in ("max_center_residual_m", "max_orientation_change_rad"):
        value = rec.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(value)
    return out


class BakeReclaimed(Exception):
    """(h) 정식 굽기를 작업 시작(LIVE)으로 회수했다(명세 3.2 5항, -597). 굽기 도구는 자식 프로세스를 끝낸 뒤 이것을 던진다.

    처리기는 실패로 보지 않고 정식 Scene 없이(scene_version null, bake_skipped) COMPLETED 를 낸다. 관제는 미리보기 Scene 으로 한다.
    """


def _reclaim_as_no_scene(bake: Callable[..., Any]) -> Callable[..., Any]:
    """굽기 도구를 감싸 BakeReclaimed 를 정식 Scene 없음(BakedScene(None))으로 바꾼다. 대여 취소·시간 초과는 그대로 올린다."""

    def run(**kw: Any) -> Any:
        try:
            return bake(**kw)
        except BakeReclaimed:
            log.warning("같은 현장 작업이 시작돼 정식 굽기를 멈췄다(미리보기 Scene 으로 관제)")
            return BakedScene(None)  # type: ignore[arg-type]  # None = 정식 Scene 없음(-597)

    return run


@dataclass(frozen=True, slots=True)
class BakedScene:
    """(h) 굽기로 만든 정식 Scene 버전과 GLB 객체 참조. supplement 는 보조 촬영 결과 요약(계약 supplement_summary, -605)."""

    scene_version: str
    scene_asset: ObjectRef | None = None
    supplement: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RebakeOutcome:
    """SUPPLEMENT_REBAKE 결과(-605). 보조 촬영이 적용되지 않았으면 scene_asset 없음·scene_version = 기준."""

    scene_version: str
    supplement: Mapping[str, Any]
    height_checks: dict[str, dict[str, Any]]
    scene_asset: ObjectRef | None = None


@dataclass(frozen=True, slots=True)
class PreviewDraftResult:
    """(g) 미리보기 맵(`preview` 모드) 및 선택 VLM 구역 초안 참조."""

    preview_scene_version: str
    zone_drafts_ref: str | None = None
    scene_asset: ObjectRef | None = None
    floor_coverage: Mapping[str, Any] | None = None
    """바닥 감시 범위(계약 site_setup#/$defs/floor_coverage, S15P31A101-606). 바닥을 모르면 None(싣지 않는다)"""


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
        progress: Callable[[float], None] | None = None,
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
        progress: Callable[[float], None] | None = None,
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


class SupplementRebaker(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        site_id: str,
        camera_ids: Sequence[str],
        camera_heights_m: Mapping[str, float],
        height_tolerance_cm: float,
        rebake_base: Mapping[str, Any],
        on_height_checks: Callable[[dict[str, dict[str, Any]]], None],
    ) -> RebakeOutcome: ...


class CameraChecker(Protocol):
    """세팅 「카메라 확인」 실행기(`services.worker.camera_check.CameraCheckRunner`)."""

    def start(self, setup_session_id: str, calibration_doc: Mapping[str, Any], *, expires_at_ms: int,
              required_pairs: Sequence[tuple[str, str]] = ()) -> dict[str, Any]: ...

    def poll(self) -> dict[str, Any] | None: ...

    def incomplete(self) -> dict[str, Any] | None: ...

    def stop(self) -> None: ...


CAMERA_CHECK_FINAL = frozenset({"PASSED", "FAILED", "INCOMPLETE"})


class TextureSceneBaker(Protocol):
    def __call__(
        self,
        *,
        setup_session_id: str,
        site_id: str,
        calibration_doc: Mapping[str, Any],
        backgrounds: CleanBackgroundBundle,
        preview_scene_version: str,
        progress: Callable[[float], None] | None = None,
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
        rebaker: SupplementRebaker | None = None,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        perf_counter: Callable[[], float] = time.perf_counter,
        stage_a_target_seconds: float = 10.0,
        background_wait_s: float = 0.0,
        background_poll_s: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[threading.Event, float], Any] | None = None,
        camera_check: CameraChecker | None = None,
        camera_check_poll_s: float = 1.0,
        camera_check_report_s: float = 3.0,
        camera_check_margin_s: float = 20.0,
        quality_gate: Mapping[str, Any] | None = None,
    ) -> None:
        """live_gate: True 면 굽기를 `LIVE_EXP_BLOCKED`(재시도 가능)로 미룬다 — "LIVE 작업 중" 신호 자리(운영 조립은 넘기지 않는다).
        live_notice: True 면 Tier 0 가 떠 있다는 로그만 남기고 굽는다(Tier 0 지연이 잠깐 늘 수 있음).
        background_wait_s: 배경 묶음이 완성되기를 기다리는 최대 초(0 이면 한 번만 본다). sleep(취소 신호, 초)의 기본은 신호 대기.
        world_anchor: Stage A 뒤 보정 문서 → 이전 채택 World 로 옮긴(또는 사유만 기록한) 문서. 없으면 새 World 그대로(-584).
        camera_check: 보정 등록 뒤 사람 걷기로 새 보정을 확인하는 실행기(-646). 없으면 확인하지 않는다(스냅샷에 싣지 않음).
        camera_check_report_s: 상태가 그대로면 카메라 확인 스냅샷을 이 간격(초)보다 자주 보내지 않는다(EC2 하트비트 부하).
        camera_check_margin_s: 결과를 내기 전 확인을 기다릴 때 실행 상한까지 남길 여유(초).
        quality_gate: 보정 품질 게이트 기준 덮어쓰기(joint_ba_v2.DEFAULT_QUALITY_GATE 키)."""
        if background_wait_s < 0 or background_poll_s <= 0:
            raise ValueError("background_wait_s 는 0 이상, background_poll_s 는 양수다")
        self._collect_bg = background_collector
        self._init_world = world_initializer
        self._publish_calibration = calibration_publisher or (
            lambda doc: str(doc["calibration_version"])
        )
        self._build_preview = preview_builder or self._default_preview_builder
        # 정식 굽기를 작업 시작(LIVE)으로 회수하면(BakeReclaimed) 정식 Scene 없음(scene_version None)으로 바꾼다(-597)
        self._bake_texture = _reclaim_as_no_scene(texture_baker or self._default_texture_baker)
        self._publish_frames = frame_publisher
        self._publish_bake_images = bake_image_publisher
        self._object_cross_checker = object_cross_checker
        self._feature_matcher = feature_matcher
        self._live_gate = live_gate or (lambda: False)
        self._live_notice = live_notice or (lambda: False)
        self._anchor_world = world_anchor
        self._rebake = rebaker
        self._bg_wait_s = float(background_wait_s)
        self._bg_poll_s = float(background_poll_s)
        self._monotonic = monotonic
        self._sleep = sleep or (lambda event, seconds: event.wait(seconds))
        self._clock_ms = clock_ms
        self._perf = perf_counter
        self._stage_a_target_s = float(stage_a_target_seconds)
        self._camera_check = camera_check
        self._cc_poll_s = float(camera_check_poll_s)
        self._cc_report_s = float(camera_check_report_s)
        self._cc_margin_s = float(camera_check_margin_s)
        self._quality_gate_config = dict(quality_gate) if quality_gate is not None else None
        self._emit_lock = threading.RLock()
        self._last_payload: dict[str, Any] | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._last_progress_at = float("-inf")
        self._quality_gate: dict[str, Any] | None = None
        self._cc_summary: dict[str, Any] | None = None
        self._cc_stop = threading.Event()
        self._cc_thread: threading.Thread | None = None

    @staticmethod
    def _default_preview_builder(
        *,
        setup_session_id: str,
        site_id: str,
        calibration_doc: Mapping[str, Any],
        backgrounds: CleanBackgroundBundle,
        progress: Callable[[float], None] | None = None,
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
        progress: Callable[[float], None] | None = None,
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

    def _with_checks(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """스냅샷·결과에 지금까지의 보정 품질 게이트와 카메라 확인 요약을 싣는다(-646). 다시 굽기(SUPPLEMENT_REBAKE)는 싣지 않는다."""
        out = dict(payload)
        if self._quality_gate is not None:
            out["quality_gate"] = self._quality_gate
        if self._cc_summary is not None:
            out["camera_check"] = self._cc_summary
        return out

    def _emit_progress(self, ctx: JobContext, payload: Mapping[str, Any]) -> None:
        with self._emit_lock:
            snap = self._with_checks(payload)
            if snap == self._last_snapshot:
                return
            errs = schema_errors(snap, SITE_SETUP_PROGRESS_SCHEMA)
            if errs:
                raise JobFailed(
                    "INTERNAL_CONTRACT_ERROR",
                    f"site_setup 진행 스냅샷이 계약을 위반했다: {'; '.join(errs)}",
                    retryable=False,
                )
            ctx.report_site_setup(snap)
            self._last_payload = dict(payload)
            self._last_snapshot = copy.deepcopy(snap)
            self._last_progress_at = self._monotonic()

    def _subprogress(self, ctx: JobContext, base: Mapping[str, Any], start: float,
                     end: float) -> Callable[[float], None]:
        """실제로 끝난 하위 작업 비율만 환산한다. 구간 끝 스냅샷은 호출자가 별도로 보낸다."""
        def report(fraction: float) -> None:
            self._check_cancelled(ctx)
            pct = round(start + (end - start) * min(1.0, max(0.0, fraction)), 1)
            with self._emit_lock:
                previous = self._last_payload or {}
                if pct <= float(previous.get("progress_pct", -1)):
                    return
                if self._monotonic() - self._last_progress_at < 1.0:
                    return
                self._emit_progress(ctx, {**base, "progress_pct": pct})
        return report

    # ------------------------------------------------------------------ 카메라 확인(-646)

    def _start_camera_check(self, ctx: JobContext, setup_session_id: str, calibration_doc: Mapping[str, Any],
                            calibration_version: str) -> None:
        """보정 등록 뒤 카메라 확인을 연다. 실행기가 없거나 열지 못하면 확인 없이 이어 간다(서버가 완료를 막는다)."""
        if self._camera_check is None:
            return
        doc = copy.deepcopy(dict(calibration_doc))
        doc["calibration_version"] = calibration_version
        remaining = ctx.remaining_s()
        expires_at = int(self._clock_ms()) + int(1000 * (remaining if remaining is not None else 900.0))
        required = [tuple(p["camera_ids"]) for p in (self._quality_gate or {}).get("pairs", {}).values()
                    if p.get("verdict") == "NEEDS_CHECK" and len(p.get("camera_ids", ())) == 2]
        try:
            summary = self._camera_check.start(setup_session_id, doc, expires_at_ms=expires_at,
                                               required_pairs=required)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - 확인을 못 열어도 세팅 산출물은 낸다(완료는 서버가 막는다)
            log.warning("작업 %s: 카메라 확인을 열지 못했다(%s: %s)", ctx.job_id, type(exc).__name__, exc)
            return
        with self._emit_lock:
            self._cc_summary = summary
        self._cc_stop.clear()
        self._cc_thread = threading.Thread(target=self._camera_check_loop, args=(ctx,), name="site-setup-camera-check",
                                           daemon=True)
        self._cc_thread.start()

    def _camera_check_loop(self, ctx: JobContext) -> None:
        """보조 스레드: 새 view 를 평가하고 요약이 바뀌면 마지막 스냅샷에 실어 다시 보낸다(단계·진행률은 그대로)."""
        assert self._camera_check is not None
        last_sent = self._monotonic()
        while not self._cc_stop.wait(self._cc_poll_s):
            if ctx.cancelled.is_set():
                return
            try:
                summary = self._camera_check.poll()
            except Exception:  # noqa: BLE001
                log.exception("작업 %s: 카메라 확인 평가 실패(다음 주기에 다시 본다)", ctx.job_id)
                continue
            if summary is None:
                continue
            with self._emit_lock:
                previous = self._cc_summary or {}
                if summary == previous:
                    continue
                if (summary.get("status") == previous.get("status")
                        and self._monotonic() - last_sent < self._cc_report_s):
                    continue  # 진행률만 바뀌었다: 다음 보고 때 싣는다
                self._cc_summary = summary
                last_sent = self._monotonic()
                base = self._last_payload
                if base is not None:
                    try:
                        self._emit_progress(ctx, base)
                    except Exception:  # noqa: BLE001 - 하트비트 실패는 처리기 흐름이 다음 스냅샷에서 다시 낸다
                        log.warning("작업 %s: 카메라 확인 스냅샷을 보내지 못했다", ctx.job_id, exc_info=True)
            if summary.get("status") in CAMERA_CHECK_FINAL:
                return

    def _finish_camera_check(self, ctx: JobContext) -> None:
        """결과를 내기 전에 카메라 확인 판정을 기다린다. 실행 상한 여유까지 못 하면 INCOMPLETE 로 닫는다."""
        if self._camera_check is None or self._cc_summary is None:
            return
        while (self._cc_summary or {}).get("status") not in CAMERA_CHECK_FINAL:
            self._check_cancelled(ctx)
            remaining = ctx.remaining_s()
            thread_alive = self._cc_thread is not None and self._cc_thread.is_alive()
            if (remaining is not None and remaining <= self._cc_margin_s) or not thread_alive:
                break
            self._sleep(ctx.cancelled, min(self._cc_poll_s, max(0.05, (remaining or 1.0) - self._cc_margin_s)))
        self._stop_camera_check_thread()
        if (self._cc_summary or {}).get("status") not in CAMERA_CHECK_FINAL:
            try:
                summary = self._camera_check.incomplete()
            except Exception:  # noqa: BLE001
                log.exception("작업 %s: 카메라 확인 마감 처리 실패", ctx.job_id)
                summary = None
            if summary is not None:
                with self._emit_lock:
                    self._cc_summary = summary
            log.warning("작업 %s: 카메라 확인을 마감 전에 끝내지 못했다(INCOMPLETE)", ctx.job_id)

    def _stop_camera_check_thread(self) -> None:
        self._cc_stop.set()
        thread = self._cc_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._cc_thread = None

    def _wait_for_backgrounds(
        self, ctx: JobContext, setup_session_id: str, site_id: str, camera_ids: tuple[str, ...],
        mode_fields: Mapping[str, Any] | None = None,
    ) -> tuple[CleanBackgroundBundle, dict[str, float]]:
        """(a) 묶음이 완성될 때까지 최대 background_wait_s 동안 다시 본다. 수집률이 바뀌면 COLLECTING_BACKGROUND 하트비트.

        진행률은 가장 느린 카메라 기준으로 0~14 구간에서 단조 증가(서버가 같은 대여의 감소를 거절)하고, 카메라별 수집률은 실제 값을 그대로 싣는다
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
            best_pct = max(best_pct, round(14.0 * min(pct.values()) / 100.0, 1))
            key = (best_pct, tuple(pct[c] for c in camera_ids))
            if key != last_sent:
                self._emit_progress(ctx, {
                    "setup_session_id": setup_session_id,
                    "status": "COLLECTING_BACKGROUND",
                    "progress_pct": best_pct,
                    "background_collection_pct": pct,
                    **(mode_fields or {}),
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

    def _run_rebake(
        self, ctx: JobContext, setup_session_id: str, site_id: str, camera_ids: tuple[str, ...],
        camera_heights_m: dict[str, float], height_tolerance_cm: float,
    ) -> Mapping[str, Any]:
        """SUPPLEMENT_REBAKE(-605): 기준 보정·사진으로 맵만 보조 촬영을 넣어 다시 굽는다. 보정·미리보기는 기준 값 그대로."""
        if self._rebake is None:
            raise JobFailed("INVALID_INPUT", "이 워커는 SUPPLEMENT_REBAKE 를 처리하지 않는다", retryable=False)
        base = dict(ctx.input["rebake_base"])
        missing = sorted(set(camera_ids) - {str(im["camera_id"]) for im in base["bake_images"]["images"]})
        if missing:
            raise JobFailed("INVALID_INPUT", f"rebake_base.bake_images 에 없는 카메라: {missing}", retryable=False)
        t_start = self._perf()
        self._check_cancelled(ctx)
        common = {"setup_session_id": setup_session_id, "mode": "SUPPLEMENT_REBAKE",
                  "calibration_version": str(base["calibration_version"]),
                  "preview_scene_version": str(base["preview_scene_version"])}

        def started(height_checks: dict[str, dict[str, Any]]) -> None:
            # 기준 보정 문서를 읽은 뒤 첫 스냅샷(BAKING_TEXTURE 는 보정·미리보기 버전·높이 확인이 필수)
            self._check_cancelled(ctx)
            self._emit_progress(ctx, {**common, "status": "BAKING_TEXTURE", "progress_pct": 90,
                                      "height_checks": height_checks})

        outcome = self._rebake(
            setup_session_id=setup_session_id, site_id=site_id, camera_ids=camera_ids,
            camera_heights_m=camera_heights_m, height_tolerance_cm=height_tolerance_cm, rebake_base=base,
            on_height_checks=started,
        )
        seconds = round(max(0.0, self._perf() - t_start), 4)
        result: dict[str, Any] = {
            **common, "status": "COMPLETED", "progress_pct": 100, "scene_version": outcome.scene_version,
            "height_checks": outcome.height_checks, "supplement": dict(outcome.supplement),
            "metrics": {"stage_seconds": {"BAKING_TEXTURE": seconds}, "total_seconds": seconds},
        }
        if outcome.scene_asset is not None:
            result["artifacts"] = {"scene": outcome.scene_asset.to_json()}
        errs = schema_errors(result, SITE_SETUP_RESULT_SCHEMA)
        if errs:
            raise JobFailed("INTERNAL_CONTRACT_ERROR", f"site_setup 다시 굽기 결과가 계약을 위반했다: {'; '.join(errs)}",
                            retryable=False)
        return result

    def _finish_reuse(
        self, ctx: JobContext, joint_ba_snapshot: Mapping[str, Any], stage_seconds: dict[str, float],
        stage_a: StageAResult, t_start: float,
    ) -> Mapping[str, Any]:
        """REUSE_SCENE 채택(-614): JOINT_BA_AND_VERIFY 스냅샷(새 보정 문서·높이 확인·world_anchor)을 그대로 READY_FOR_ZONES 로 올리고
        COMPLETED 로 끝낸다. 미리보기·정식 맵·카메라 프레임·굽기 사진을 만들지 않는다(현재 관제 맵과 그 구역을 그대로 쓴다)."""
        self._check_cancelled(ctx)
        stage_seconds["READY_FOR_ZONES"] = 0.0
        metrics = {"stage_seconds": dict(stage_seconds), "joint_ba": stage_a.summary_metrics}
        self._emit_progress(ctx, {**joint_ba_snapshot, "status": "READY_FOR_ZONES", "progress_pct": 85, "metrics": metrics})
        self._finish_camera_check(ctx)
        result: dict[str, Any] = self._with_checks({
            **joint_ba_snapshot,
            "status": "COMPLETED",
            "progress_pct": 100,
            "metrics": {**metrics, "total_seconds": round(max(0.0, self._perf() - t_start), 4)},
        })
        errs = schema_errors(result, SITE_SETUP_RESULT_SCHEMA)
        if errs:
            raise JobFailed("INTERNAL_CONTRACT_ERROR", f"site_setup 재사용 결과가 계약을 위반했다: {'; '.join(errs)}",
                            retryable=False)
        return result

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        self._last_payload = None
        self._last_snapshot = None
        self._last_progress_at = float("-inf")
        self._quality_gate = None
        self._cc_summary = None
        try:
            return self._run(ctx)
        finally:
            self._stop_camera_check_thread()
            if self._camera_check is not None:
                try:
                    self._camera_check.stop()
                except Exception:  # noqa: BLE001
                    log.warning("작업 %s: 카메라 확인 요청을 닫지 못했다", ctx.job_id, exc_info=True)

    def _run(self, ctx: JobContext) -> Mapping[str, Any]:
        t_start = self._perf()
        setup_session_id, site_id, camera_ids, camera_heights_m, height_tolerance_cm = (
            self._validate_input(ctx.input)
        )
        if ctx.input.get("mode") == "SUPPLEMENT_REBAKE":
            return self._run_rebake(ctx, setup_session_id, site_id, camera_ids, camera_heights_m, height_tolerance_cm)
        stage_seconds: dict[str, float] = {}
        # 이전 모델 재사용(-614): World 다시 맞추기 판정 전까지는 요청한 REUSE_SCENE 을 스냅샷에 싣는다. FULL_SETUP 은 지금처럼 싣지 않는다
        requested_reuse = ctx.input.get("mode") == MODE_REUSE_SCENE
        mode_fields: dict[str, Any] = {"mode": MODE_REUSE_SCENE} if requested_reuse else {}

        # ------------------------------------------------------------------
        # (a) COLLECTING_BACKGROUND
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        t0 = self._perf()
        bg_bundle, bg_pct = self._wait_for_backgrounds(ctx, setup_session_id, site_id, camera_ids, mode_fields)
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
                **mode_fields,
            },
        )

        # ------------------------------------------------------------------
        # (b)~(d) INITIAL_POSE_AND_WORLD (무마커 내부 파라미터 + MapAnything + 바닥 Z=0 + 카메라 높이 축척)
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        t0 = self._perf()
        initial_snapshot = {**(self._last_payload or {}), "status": "INITIAL_POSE_AND_WORLD"}
        self._emit_progress(ctx, initial_snapshot)
        try:
            init_world = self._init_world(
                setup_session_id=setup_session_id,
                site_id=site_id,
                camera_ids=camera_ids,
                camera_heights_m=camera_heights_m,
                height_tolerance_cm=height_tolerance_cm,
                backgrounds=bg_bundle,
                created_at_ms=created_at_ms,
                progress=self._subprogress(ctx, initial_snapshot, 15, 39),
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
                **mode_fields,
            },
        )

        # ------------------------------------------------------------------
        # (e)~(f) JOINT_BA_AND_VERIFY (V2 Stage A 회전 BA + 새 버전 보정 등록)
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        t0 = self._perf()
        refining_snapshot = {**(self._last_payload or {}), "status": "JOINT_BA_AND_VERIFY"}
        self._emit_progress(ctx, refining_snapshot)
        try:
            tracks_source = (
                bg_bundle.tracks_doc
                if bg_bundle.tracks_doc is not None
                else extract_tracks_from_backgrounds(
                    bg_bundle.frames_by_cam,
                    init_world.calibration_doc,
                    matcher=self._feature_matcher,
                    progress=self._subprogress(ctx, refining_snapshot, 40, 48),
                )
            )
            self._subprogress(ctx, refining_snapshot, 40, 48)(1)
            stage_a: StageAResult = run_stage_a_rotation_ba(
                init_world.calibration_doc,
                tracks_source,
                camera_heights_m=camera_heights_m,
                height_tolerance_cm=height_tolerance_cm,
                created_at_ms=created_at_ms,
                stage_target_seconds=self._stage_a_target_s,
                object_cross_checker=self._object_cross_checker,
                backgrounds_by_cam=bg_bundle.frames_by_cam if bg_bundle.frames_by_cam else None,
                progress=self._subprogress(ctx, refining_snapshot, 48, 59),
            )
        except StageAError as exc:
            raise JobFailed(exc.code, exc.message, retryable=exc.retryable) from exc

        # 보정 절대 품질 게이트(-646): 상대 악화만이 아니라 절대 상한·최소 홀드아웃·카메라 높이 차를 본다
        self._quality_gate = evaluate_quality_gate(stage_a.pair_metrics, stage_a.height_checks, self._quality_gate_config)
        if self._quality_gate["verdict"] == QUALITY_FAILED:
            stage_seconds["JOINT_BA_AND_VERIFY"] = round(max(0.0, self._perf() - t0), 4)
            self._emit_progress(ctx, {
                "setup_session_id": setup_session_id,
                "status": "JOINT_BA_AND_VERIFY",
                "progress_pct": 60,
                "background_collection_pct": bg_pct,
                "height_checks": stage_a.height_checks,
                "metrics": {"stage_seconds": dict(stage_seconds), "joint_ba": stage_a.summary_metrics},
                **mode_fields,
            })
            problems = "; ".join(p["message"] for p in self._quality_gate["problems"] if p["reason"] != "FEW_HOLDOUT")
            raise JobFailed("VALIDATION_REJECTED", f"보정 품질 기준 미달: {problems}"[:480], retryable=False)

        self._subprogress(ctx, refining_snapshot, 59, 61)(1)

        # 등록·미리보기·굽기가 같은 World 를 쓰도록 여기서 한 번만 이전 World 로 옮긴다(-584)
        calibration_doc = stage_a.calibration_doc
        if self._anchor_world is not None:
            calibration_doc = dict(self._anchor_world(copy.deepcopy(calibration_doc)))
        anchor = world_anchor_summary(calibration_doc)
        reuse_scene = False
        if requested_reuse:
            if anchor is not None and anchor["accepted"]:
                reuse_scene = True
            else:
                # 카메라가 움직였거나 비교할 기준이 없다: 같은 작업 안에서 전체 세팅으로 이어 간다(배경·보정은 이미 끝났다, -614)
                if anchor is None:
                    anchor = {"accepted": False, "reason": NO_ANCHOR_REFERENCE, "reference_calibration_version": None}
                mode_fields = {"mode": "FULL_SETUP", "reuse_fallback_reason": anchor["reason"]}
                log.warning("작업 %s: 이전 모델 재사용을 요청했지만 World 다시 맞추기가 거부돼(%s) 새 모델 세팅으로 이어 간다",
                            ctx.job_id, anchor["reason"])
        if anchor is not None:
            mode_fields = {**mode_fields, "world_anchor": anchor}
        self._subprogress(ctx, {**refining_snapshot, **mode_fields}, 61, 63)(1)
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
            **mode_fields,
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
        # 사람 걷기 카메라 확인(-646). 배경 수집은 끝났으므로 미리보기·굽기와 함께 돈다
        self._start_camera_check(ctx, setup_session_id, calibration_doc, calibration_version)

        if reuse_scene:
            # 같은 World 로 옮겨졌다: 미리보기·AI 구역 초안·굽기를 건너뛰고 새 보정만으로 끝낸다(현재 관제 맵 유지, -614)
            return self._finish_reuse(ctx, joint_ba_snapshot, stage_seconds, stage_a, t_start)

        # ------------------------------------------------------------------
        # (g) PREVIEW_AND_VLM_DRAFT → READY_FOR_ZONES
        # ------------------------------------------------------------------
        self._check_cancelled(ctx)
        t0 = self._perf()
        preview_progress = {**joint_ba_snapshot, "status": "PREVIEW_AND_VLM_DRAFT"}
        self._emit_progress(ctx, preview_progress)
        preview = self._build_preview(
            setup_session_id=setup_session_id,
            site_id=site_id,
            calibration_doc=calibration_doc,
            backgrounds=bg_bundle,
            progress=self._subprogress(ctx, preview_progress, 65, 75),
        )
        self._subprogress(ctx, preview_progress, 65, 75)(1)
        camera_frames = self._publish_camera_frames(ctx, setup_session_id, camera_ids, bg_bundle)
        self._subprogress(ctx, preview_progress, 75, 77)(1)
        # 미리보기·굽기가 읽는 같은 사진. 서버가 미리보기를 현재 Scene 으로 올리기(READY_FOR_ZONES) 전에 등록돼야 관제 화면이
        # 그 Scene 의 사진을 처음 물을 때 받는다. 서버는 대여로 미리보기·정식 Scene 둘 다에 묶는다
        bake_images = self._publish_bake_inputs(ctx, setup_session_id, camera_ids, bg_bundle)
        self._subprogress(ctx, preview_progress, 77, 79)(1)
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
            **mode_fields,
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
        if preview.floor_coverage is not None:
            # 표시용(Visual 층). 등록한 이 보정 버전으로 계산한 것만 싣는다(EC2 가 보정 버전에 묶어 현재 Scene 응답에 싣는다)
            if preview.floor_coverage.get("calibration_version") == calibration_version:
                artifacts["floor_coverage"] = dict(preview.floor_coverage)
            else:
                log.warning("작업 %s: 바닥 감시 범위의 보정 버전이 등록한 보정과 달라 싣지 않는다", ctx.job_id)
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
            "progress_pct": 85,
        }
        self._emit_progress(ctx, baking_snapshot)

        baked = self._bake_texture(
            setup_session_id=setup_session_id,
            site_id=site_id,
            calibration_doc=calibration_doc,
            backgrounds=bg_bundle,
            preview_scene_version=preview.preview_scene_version,
            progress=self._subprogress(ctx, baking_snapshot, 85, 94),
        )
        supplement: Mapping[str, Any] | None = None
        if isinstance(baked, BakedScene):
            scene_version = baked.scene_version
            if baked.scene_asset is not None:
                artifacts["scene"] = baked.scene_asset.to_json()
            supplement = baked.supplement
        else:
            scene_version = str(baked)
        stage_seconds["BAKING_TEXTURE"] = round(
            max(0.0, self._perf() - t0), 4
        )
        self._emit_progress(ctx, {**baking_snapshot, "progress_pct": 95})
        self._finish_camera_check(ctx)
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
            **mode_fields,
        }
        if scene_version is None:
            # 작업 시작으로 정식 굽기를 회수했다(site_setup 1.7.0, -597): 관제는 이미 현재 Scene 인 미리보기로 한다
            result["bake_skipped"] = BAKE_SKIPPED_LIVE_WORK
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
        if supplement is not None:
            result["supplement"] = dict(supplement)
        result = self._with_checks(result)

        errs = schema_errors(result, SITE_SETUP_RESULT_SCHEMA)
        if errs:
            raise JobFailed(
                "INTERNAL_CONTRACT_ERROR",
                f"site_setup 결과가 계약을 위반했다: {'; '.join(errs)}",
                retryable=False,
            )
        return result
