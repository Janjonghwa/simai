"""I5 `site_setup` 작업 처리기(S15P31A101-465).

대여 → 단계별 하트비트(`site_setup.schema.json#/$defs/progress`, 단조 증가) → 결과 제출(`#/$defs/result`).
단계:
(a) COLLECTING_BACKGROUND: Tier 0 사람 지운 배경 묶음 확인 (`BACKGROUND_INCOMPLETE`)
(b) INITIAL_POSE_AND_WORLD: 무마커 내부 파라미터 확인(기존 값 재사용 또는 추정)
(c) INITIAL_POSE_AND_WORLD: MapAnything 초기 복원 + World 자동 정의(바닥 Z=0, 경계 벽 +X, 모서리 원점, `FLOOR_FIT_FAILED`)
(d) INITIAL_POSE_AND_WORLD: 카메라 높이(`camera_heights_m`)로 축척 및 높이 불일치(`height_checks`) 계산.
    카메라 높이 빠른 확인(-690, `height_review`): 입력 대비 상대 불일치가 3% 를 넘는 카메라가 있으면 AWAITING_CONFIRMATION 을
    보내고 하트비트 응답의 관리자 결정(job_lease_status 2.2.0 height_decision)을 `height_review_wait_s` 까지 기다린다. 고친 높이면
    같은 점구름·포즈로 World 를 다시 맞추고(MapAnything 다시 안 돌림) 그 높이로 이어 간다. 맞았거나 확인한 높이는 품질 게이트가 같은
    높이 비교로 다시 거절하지 않는다. 답이 없으면 입력 높이로 진행(TIMED_OUT, 게이트 높이 상한 그대로)
    (-462 정물 교차 확인은 `object_cross_checker` 확장 지점만 둔다)
(e) JOINT_BA_AND_VERIFY: V2 Stage A 회전 BA (`INSUFFICIENT_FEATURES`, `VALIDATION_REJECTED`)
(f) JOINT_BA_AND_VERIFY: (선택 `world_anchor`) 카메라가 움직이지 않았으면 새 보정을 이전 채택 보정의 World 로 옮긴 뒤
    새 `calibration_version` 보정 문서 등록(덮어쓰지 않음). 미리보기·굽기·map_cameras 는 이 문서를 기준으로 정렬한다(S15P31A101-584)
(f') JOINT_BA_AND_VERIFY(65%): 「카메라 확인」(S15P31A101-646, 맵 앞으로 옮김 S15P31A101-687). 관리자의 「걷기 시작」을 기다렸다가
    사람 걷기로 새 보정을 확인한다. 통과해야 (g)·(h)(REUSE 는 결과)로 간다. 실패면 「확인만 다시」를 기다리고, 대기 마감까지 통과하지
    못하거나 확인 채널을 열지 못하면 맵을 굽지 않고 실패한다
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
COLLECTING_BACKGROUND 하트비트로 보낸다. 수집기가 세팅 배경 촬영 상태(`CleanBackgroundBundle.capture`)를 주면 같은 스냅샷의
`background_capture`(카메라별 READY·INSUFFICIENT_SAMPLES·PERSON_OCCLUDED·NO_VIDEO, S15P31A101-686)로 싣는다. 보정 문서의 생성 시각(`created_at`)은 배경 묶음의 마지막 관측 시각
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
카메라 확인(S15P31A101-646 → -687): (f) 보정 등록 뒤 맵을 만들기 전에 한다. 처리기는 `camera_check` AWAITING_START + `user_wait`
스냅샷을 보내고 하트비트 응답의 관리자 요청(job_lease_status 2.3.0 camera_check_start, 시도 번호)을 기다린다. 요청이 오면 실행기가
같은 Pod 의 Tier 0 에 그 시도의 관제 view 를 요청하고, 사람이 걷는 동안 관제와 같은 계산(쌍 에피폴라·잔차 통과율, 한 카메라 제외
재투영, 4대 관측)으로 새 보정을 확인한다. 시도가 FAILED(기하)·INCOMPLETE(표본 부족, 시도 상한 `camera_check_attempt_s`)면 보정은
그대로 두고 `user_wait` 를 다시 실어 「확인만 다시」(번호 + 1)를 기다린다. 늦게 온 같거나 작은 번호는 무시한다. 대기 마감(관리자 대기
상한과 실행 상한 − 맵 몫 중 짧은 쪽)까지 통과하지 못하면 `CAMERA_CHECK_NOT_PASSED`, 시도를 열지 못하거나 Tier 0 view 가 한 줄도 오지
않으면 `CAMERA_CHECK_UNAVAILABLE` 로 끝낸다. 둘 다 미리보기·굽기를 한 번도 부르지 않는다. 서버는 PASSED 전에는 구역 단계·세팅 완료를
거부한다(이제 맵 전에 정해지므로 뒤에서 처음 막히지 않는다).

시간 예산(S15P31A101-688): 실행 상한 1,800 s 는 배경 촬영·초기 복원·높이 확인·Stage A·카메라 확인·맵 몫(`SETUP_BUDGET_*`)의 합이다.
사람 대기(높이 확인·카메라 확인)는 각자 상한과 "남은 시간 − 뒤 단계 몫" 중 짧은 쪽까지만 기다려 뒤의 맵 몫(600 s)을 깎지 않는다.
서버는 새 모델 세팅을 정밀 맵(scene_version)이 나온 뒤에만 완료시킨다(미리보기로 완료하지 않는다).

보조 촬영(입력 `supplement_media`, S15P31A101-605)은 (h) 굽기 주입점이 처리하고 `BakedScene.supplement` 요약을 돌려주면 결과
`supplement` 로 싣는다. 보조 촬영 때문에 세팅을 실패시키지 않는다(주입점이 FALLBACK·SKIPPED 요약으로 바꾼다).
`mode: SUPPLEMENT_REBAKE` 는 (a)~(g) 를 건너뛰고 `rebaker` 주입점 하나로 이 세션 맵만 보조 촬영을 넣어 다시 굽는다(BAKING_TEXTURE →
COMPLETED). 새 보정·미리보기를 만들지 않고, 보조 촬영이 적용되지 않으면 새 Scene 도 올리지 않는다(결과 scene_version = 기준).
`mode: REUSE_SCENE`(이전 모델 재사용, S15P31A101-614)은 (a)~(f) 를 그대로 하고, (f) 의 World 다시 맞추기가 채택되면 (g)·(h) 를
건너뛰어 JOINT_BA_AND_VERIFY → READY_FOR_ZONES → COMPLETED 로 새 보정만 낸다(현재 관제 맵 유지, 보정 문서 artifacts·높이 확인은
그대로 싣는다). 거부되거나 기록이 없으면 새 맵(FULL_SETUP)으로 저절로 이어 가지 않는다(S15P31A101-689): `mode: REUSE_SCENE` 그대로
거부 요약(`world_anchor`, 달라진 카메라 `changed_cameras`)을 실은 스냅샷을 보내고 새 보정을 등록하지 않은 채 `REUSE_WORLD_REJECTED` 로
실패한다(관리자가 새 맵을 고른다). 모든 모드에서 보정 문서에 world_anchor 가 있으면 그 요약(`world_anchor`)을 (f) 스냅샷부터 싣는다.

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
from typing import Any, NoReturn, Protocol

import numpy as np

from simai_ai.calibration.criteria import (
    CalibrationJudgement,
    ScaleBasis,
    ScaleDecision,
)
from simai_ai.calibration.document import CameraCalibration, build_calibration_doc
from simai_ai.calibration.extrinsics.joint_ba_v2 import (
    HEIGHT_REVIEW_AWAITING,
    HEIGHT_REVIEW_CONFIRMED,
    HEIGHT_REVIEW_CORRECTED,
    HEIGHT_REVIEW_MAX_RELATIVE_DIFF,
    HEIGHT_REVIEW_PASSED,
    HEIGHT_REVIEW_SETTLED,
    HEIGHT_REVIEW_TIMED_OUT,
    QUALITY_FAILED,
    ObjectScaleCrossChecker,
    StageAError,
    StageAResult,
    compute_height_checks,
    compute_height_review,
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
from simai_ai.scene.zone_height_diff import (
    REASON_NO_PREVIOUS_SCENE,
    REASON_REUSE_SCENE,
    REASON_SCENE_UNREADABLE,
    REASON_WORLD_NOT_ANCHORED,
    build_unknown_zone_height_changes,
)
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
    # 세팅 배경 촬영 진행(계약 site_setup 1.16.0 background_capture, -686). 모르면 None(싣지 않는다)
    capture: Mapping[str, Any] | None = None

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
    # 같은 점구름·카메라 포즈로 다른 높이에 맞춰 World 를 다시 만든다(MapAnything 을 다시 돌리지 않음, -690). 없으면 처리기가
    # world_initializer 를 다시 부른다(시험·옛 주입점)
    rebuild: Callable[[Mapping[str, float]], InitialWorldReconstruction] | None = field(default=None, compare=False)


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
# REUSE_SCENE 의 World 다시 맞추기가 거부되면 새 맵으로 이어 가지 않고 이 코드로 실패한다(job_callback error.code, S15P31A101-689)
REUSE_WORLD_REJECTED = "REUSE_WORLD_REJECTED"
# 카메라가 움직였을 가능성으로 거부한 사유(그 밖은 비교할 기준이 없거나 꺼짐)
MOVED_ANCHOR_REASONS = frozenset({"CENTER_RESIDUAL_EXCEEDED", "SCALE_OUT_OF_RANGE", "ORIENTATION_CHANGE_EXCEEDED"})


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
    if not accepted:
        out["changed_cameras"] = _changed_cameras(rec)
    return out


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _changed_cameras(rec: Mapping[str, Any]) -> list[str]:
    """거부된 world_anchor 에서 한도를 넘은 카메라(site_setup 1.18.0 changed_cameras, S15P31A101-689).

    카메라별 배치 잔차·방향 변화가 문서의 한도를 넘은 카메라만 고른다. 한도나 카메라별 값이 없으면(축척 차이·비교 못 함) 빈 목록이다
    (어느 카메라인지 지어내지 않는다)."""
    limits = rec.get("limits") if isinstance(rec.get("limits"), Mapping) else {}
    changed: set[str] = set()
    for values_key, limit_key in (("center_residuals_m", "max_center_residual_m"),
                                  ("orientation_changes_rad", "max_orientation_change_rad")):
        limit = _number(limits.get(limit_key))
        values = rec.get(values_key)
        if limit is None or not isinstance(values, Mapping):
            continue
        for camera_id, value in values.items():
            number = _number(value)
            if number is not None and number > limit:
                changed.add(str(camera_id))
    return sorted(changed)[:16]


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
    zone_height_changes: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RebakeOutcome:
    """SUPPLEMENT_REBAKE 결과(-605). 보조 촬영이 적용되지 않았으면 scene_asset 없음·scene_version = 기준."""

    scene_version: str
    supplement: Mapping[str, Any]
    height_checks: dict[str, dict[str, Any]]
    scene_asset: ObjectRef | None = None
    zone_height_changes: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PreviewDraftResult:
    """(g) 미리보기 맵(`preview` 모드) 및 선택 VLM 구역 초안 참조."""

    preview_scene_version: str
    zone_drafts_ref: str | None = None
    scene_asset: ObjectRef | None = None
    floor_coverage: Mapping[str, Any] | None = None
    """바닥 감시 범위(계약 site_setup#/$defs/floor_coverage, S15P31A101-606). 바닥을 모르면 None(싣지 않는다)"""
    zone_height_changes: Mapping[str, Any] | None = None


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

    def idle(self, calibration_doc: Mapping[str, Any], *, required_pairs: Sequence[tuple[str, str]] = ()) -> dict[str, Any]: ...

    def start(self, setup_session_id: str, calibration_doc: Mapping[str, Any], *, expires_at_ms: int,
              required_pairs: Sequence[tuple[str, str]] = (), attempt_id: str | None = None) -> dict[str, Any]: ...

    def poll(self) -> dict[str, Any] | None: ...

    def incomplete(self) -> dict[str, Any] | None: ...

    def stop(self) -> None: ...


CAMERA_CHECK_FINAL = frozenset({"PASSED", "FAILED", "INCOMPLETE"})
CAMERA_CHECK_AWAITING_START = "AWAITING_START"  # site_setup 1.19.0 camera_check.status(-687)
CAMERA_CHECK_NOT_PASSED = "CAMERA_CHECK_NOT_PASSED"  # 대기 마감까지 통과하지 못함(맵 굽지 않음)
CAMERA_CHECK_UNAVAILABLE = "CAMERA_CHECK_UNAVAILABLE"  # 확인 채널(같은 Pod 의 Tier 0 view)을 열지 못함(맵 굽지 않음)

# ---- 세팅 작업 시간 예산(S15P31A101-688, docs/design/S15P31A101-688-new-complete-time-budget.md) ----
# 실행 상한(EC2 SetupService.SETUP_TIMEOUT_MS 1,800 s) = 아래 몫의 합. 사람이 기다리는 몫(배경 촬영·높이 확인·카메라 확인)은 각자 상한까지만
# 쓰고 "남은 시간 − 뒤 단계 몫"으로도 줄여, 사람이 오래 기다려도 뒤의 기계 몫(특히 맵 몫)을 깎지 않는다. 그래서 앞에서 통과한 카메라 확인이
# 굽기 시간 초과로 버려지지 않는다. 기계 몫은 15차 실측(배경 뒤 초기 복원~보정 등록 약 60 s, 미리보기 약 18 s)과 굽기 추정(약 5분)에 여유를 둔 값이다
SETUP_BUDGET_BACKGROUND_S = 240.0  # (a) 배경 촬영(사람이 화면 밖으로, -686). site_setup_stages.DEFAULT_BACKGROUND_WAIT_S 와 같다
SETUP_BUDGET_INITIAL_S = 120.0  # (b~c) 내부 파라미터·MapAnything 초기 복원·World(15차 약 40 s)
SETUP_BUDGET_HEIGHT_REVIEW_S = 180.0  # (d) 높이 확인 대기(-690). 줄자로 한 대를 다시 재고 입력하는 1~2분
SETUP_BUDGET_STAGE_A_S = 60.0  # (e~f) Stage A·품질 게이트·보정 등록(15차 약 20 s)
SETUP_BUDGET_CAMERA_CHECK_S = 600.0  # (f') 카메라 확인: 걷기 시작 대기·시도(최대 150 s)·확인만 다시(-687)
# (g~h) 맵: 미리보기 약 18 s + 정밀 굽기 약 300 s(15차 세팅 2 는 208 s 째 취소, Pod 추정 100~155 s·노트북 245 s)
# + 굽기 실패 시 폴백 상한 120 s + GLB 업로드·등록·결과 제출 여유
SETUP_BUDGET_MAP_S = 600.0
SETUP_TIME_BUDGET_S = (SETUP_BUDGET_BACKGROUND_S + SETUP_BUDGET_INITIAL_S + SETUP_BUDGET_HEIGHT_REVIEW_S
                       + SETUP_BUDGET_STAGE_A_S + SETUP_BUDGET_CAMERA_CHECK_S + SETUP_BUDGET_MAP_S)  # 1,800 s


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
    def rebuild(heights: Mapping[str, float]) -> InitialWorldReconstruction:
        # 바닥·벽·모서리와 카메라 높이 축척(바닥 기울기 보정 포함)만 다시 맞춘다. CPU 로 수 초 안이다
        return build_world_from_pointcloud_and_poses(
            site_id=site_id, camera_ids=camera_ids, camera_heights_m=heights, height_tolerance_cm=height_tolerance_cm,
            points_m=points_m, unscaled_poses=unscaled_poses, intrinsics_by_cam=intrinsics_by_cam,
            created_at_ms=created_at_ms, calibration_version=calibration_version,
        )

    return InitialWorldReconstruction(
        calibration_doc=doc,
        restored_heights_m=restored_heights,
        height_checks=height_checks,
        rebuild=rebuild,
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
        camera_check_wait_s: float = SETUP_BUDGET_CAMERA_CHECK_S,
        camera_check_attempt_s: float = 150.0,
        camera_check_reserve_s: float = SETUP_BUDGET_MAP_S,
        camera_check_reuse_reserve_s: float = 30.0,
        camera_check_min_s: float = 45.0,
        camera_check_channel_s: float = 15.0,
        quality_gate: Mapping[str, Any] | None = None,
        height_review_wait_s: float = SETUP_BUDGET_HEIGHT_REVIEW_S,
        height_review_poll_s: float = 1.0,
        height_review_reserve_s: float = SETUP_BUDGET_STAGE_A_S + SETUP_BUDGET_CAMERA_CHECK_S + SETUP_BUDGET_MAP_S,
        height_review_max_relative_diff: float = HEIGHT_REVIEW_MAX_RELATIVE_DIFF,
    ) -> None:
        """live_gate: True 면 굽기를 `LIVE_EXP_BLOCKED`(재시도 가능)로 미룬다 — "LIVE 작업 중" 신호 자리(운영 조립은 넘기지 않는다).
        live_notice: True 면 Tier 0 가 떠 있다는 로그만 남기고 굽는다(Tier 0 지연이 잠깐 늘 수 있음).
        background_wait_s: 배경 묶음이 완성되기를 기다리는 최대 초(0 이면 한 번만 본다). sleep(취소 신호, 초)의 기본은 신호 대기.
        world_anchor: Stage A 뒤 보정 문서 → 이전 채택 World 로 옮긴(또는 사유만 기록한) 문서. 없으면 새 World 그대로(-584).
        camera_check: 보정 등록 뒤 맵 전에 사람 걷기로 새 보정을 확인하는 실행기(-646·-687). 없으면 확인하지 않는다(스냅샷에 싣지 않음).
        camera_check_report_s: 상태가 그대로면 카메라 확인 스냅샷을 이 간격(초)보다 자주 보내지 않는다(EC2 하트비트 부하).
        camera_check_wait_s: 카메라 확인 단계 전체(관리자 「걷기 시작」·「확인만 다시」 대기와 걷기)의 상한(초). 실행 상한까지
        camera_check_reserve_s(NEW: 맵 몫 SETUP_BUDGET_MAP_S) 또는 camera_check_reuse_reserve_s(REUSE: 결과 제출 몫)를 남기도록 줄인다(-688).
        camera_check_attempt_s: 시도 하나의 상한(초). 넘도록 판정을 못 하면 그 시도는 INCOMPLETE(표본 부족).
        camera_check_min_s: 단계에 이만큼도 못 쓰면 확인을 열지 않고 실패한다(걷기 30초 + 들어서는 시간).
        camera_check_channel_s: 시도를 열고 이 시간 안에 Tier 0 view 가 한 줄도 오지 않으면 확인 채널이 없다고 보고 실패한다.
        quality_gate: 보정 품질 게이트 기준 덮어쓰기(joint_ba_v2.DEFAULT_QUALITY_GATE 키).
        height_review_wait_s: 카메라 높이 빠른 확인(-690)에서 관리자 결정을 기다리는 최대 초. 실행 상한까지 height_review_reserve_s
        (뒤 단계 몫 = Stage A·등록 + 카메라 확인 전체 + 맵, -688: 높이 대기가 카메라 확인·맵 시간을 먹지 않게)를 남기도록 줄인다.
        넘으면 입력 높이 그대로 진행(TIMED_OUT, 품질 게이트 높이 차 상한은 그대로).
        height_review_poll_s: 기다리는 동안 결정을 다시 보는 간격(초). 결정은 하트비트 응답으로 들어온다."""
        if background_wait_s < 0 or background_poll_s <= 0:
            raise ValueError("background_wait_s 는 0 이상, background_poll_s 는 양수다")
        if height_review_wait_s < 0 or height_review_poll_s <= 0 or height_review_reserve_s < 0:
            raise ValueError("height_review_wait_s·height_review_reserve_s 는 0 이상, height_review_poll_s 는 양수다")
        if (camera_check_poll_s <= 0 or camera_check_attempt_s <= 0 or camera_check_channel_s <= 0
                or min(camera_check_wait_s, camera_check_reserve_s, camera_check_reuse_reserve_s, camera_check_min_s) < 0):
            raise ValueError("camera_check_poll_s·attempt_s·channel_s 는 양수, wait_s·reserve_s·min_s 는 0 이상이다")
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
        self._cc_wait_s = float(camera_check_wait_s)
        self._cc_attempt_s = float(camera_check_attempt_s)
        self._cc_reserve_s = float(camera_check_reserve_s)
        self._cc_reuse_reserve_s = float(camera_check_reuse_reserve_s)
        self._cc_min_s = float(camera_check_min_s)
        self._cc_channel_s = float(camera_check_channel_s)
        self._quality_gate_config = dict(quality_gate) if quality_gate is not None else None
        self._hr_wait_s = float(height_review_wait_s)
        self._hr_poll_s = float(height_review_poll_s)
        self._hr_reserve_s = float(height_review_reserve_s)
        self._hr_max = float(height_review_max_relative_diff)
        self._emit_lock = threading.RLock()
        self._last_payload: dict[str, Any] | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._last_progress_at = float("-inf")
        self._quality_gate: dict[str, Any] | None = None
        self._cc_summary: dict[str, Any] | None = None
        self._cc_sent_at = float("-inf")

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

    # ------------------------------------------------------------------ 카메라 확인(-646 → 맵 앞·시도 -687)

    def _set_camera_check(self, ctx: JobContext, base: Mapping[str, Any], summary: Mapping[str, Any], *,
                          force: bool) -> None:
        """카메라 확인 요약을 단계 스냅샷(base, 65%)에 실어 보낸다. 상태가 그대로면 report_s 보다 자주 보내지 않는다."""
        with self._emit_lock:
            previous = self._cc_summary or {}
            if not force and summary.get("status") == previous.get("status") and (
                    self._monotonic() - self._cc_sent_at < self._cc_report_s):
                return
            self._cc_summary = dict(summary)
            self._cc_sent_at = self._monotonic()
            self._emit_progress(ctx, base)

    def _user_wait(self, stage_deadline: float) -> dict[str, Any]:
        left = max(0.0, stage_deadline - self._monotonic())
        return {"until_ms": int(self._clock_ms() + 1000 * left), "wait_s": round(float(left), 1)}

    @staticmethod
    def _attempt_id(ctx: JobContext, no: int) -> str:
        """시도 ID = 대여 앞 8자리 + 번호(계약 attempt.id 패턴, Tier 0 가 이 시도 view 를 적는 폴더 이름)."""
        prefix = re.sub(r"[^A-Za-z0-9]", "", str(ctx.lease_id))[:8] or "lease"
        return f"{prefix}-{no}"

    def _run_camera_check(self, ctx: JobContext, base: Mapping[str, Any], setup_session_id: str,
                          calibration_doc: Mapping[str, Any], calibration_version: str, *, reserve_s: float) -> None:
        """보정 등록 뒤·맵 전 카메라 확인(-687). 통과하면 돌아오고, 아니면 JobFailed(맵을 굽지 않는다).

        1) AWAITING_START + user_wait 를 보내고 관리자의 「걷기 시작」(ctx.camera_check_start ≥ 1)을 기다린다.
        2) 시도를 열어(attempt_id 별 view 폴더) poll 한다. PASSED 면 끝. FAILED·INCOMPLETE(시도 상한)면 user_wait 를 다시 싣고
           「확인만 다시」(지금 번호보다 큰 번호)를 기다린다. 늦게 온 같거나 작은 번호는 무시한다.
        3) 단계 마감(min(대기 상한, 실행 상한 − reserve_s))까지 통과하지 못하면 CAMERA_CHECK_NOT_PASSED. 시도를 열지 못하거나 Tier 0 view
           가 channel_s 동안 한 줄도 오지 않으면 CAMERA_CHECK_UNAVAILABLE."""
        checker = self._camera_check
        if checker is None:
            return
        doc = copy.deepcopy(dict(calibration_doc))
        doc["calibration_version"] = calibration_version
        required = [tuple(p["camera_ids"]) for p in (self._quality_gate or {}).get("pairs", {}).values()
                    if p.get("verdict") == "NEEDS_CHECK" and len(p.get("camera_ids", ())) == 2]
        remaining = ctx.remaining_s()
        budget = self._cc_wait_s if remaining is None else min(self._cc_wait_s, remaining - reserve_s)
        if budget < self._cc_min_s:
            raise JobFailed(CAMERA_CHECK_NOT_PASSED,
                            f"카메라 확인에 쓸 시간이 남지 않아 맵을 만들지 않았습니다(남은 {remaining or 0:.0f}초, "
                            f"맵 몫 {reserve_s:.0f}초). 세팅을 다시 시작하세요", retryable=False)
        try:
            idle = dict(checker.idle(doc, required_pairs=required))  # type: ignore[arg-type]
        except Exception as exc:  # 확인을 열 수 없으면 맵을 굽지 않는다
            raise JobFailed(CAMERA_CHECK_UNAVAILABLE,
                            f"카메라 확인을 준비하지 못했습니다({type(exc).__name__}: {exc})"[:480], retryable=False) from exc
        stage_deadline = self._monotonic() + budget
        log.info("작업 %s: 카메라 확인 단계(보정 %s): 관리자의 걷기 시작을 최대 %.0fs 기다린다(맵 몫 %.0fs 남김)",
                 ctx.job_id, calibration_version, budget, reserve_s)
        self._set_camera_check(ctx, base, {**idle, "status": CAMERA_CHECK_AWAITING_START,
                                           "user_wait": self._user_wait(stage_deadline)}, force=True)
        attempt_no = 0
        attempt: dict[str, Any] | None = None
        attempt_started = attempt_deadline = 0.0
        last: dict[str, Any] = idle
        waiting_user = True
        while True:
            self._check_cancelled(ctx)
            now = self._monotonic()
            if waiting_user:
                requested = ctx.camera_check_start
                if requested > attempt_no and stage_deadline - now > 0:
                    attempt_no = requested
                    attempt_s = min(self._cc_attempt_s, stage_deadline - now)
                    started_ms = int(self._clock_ms())
                    attempt = {"no": attempt_no, "id": self._attempt_id(ctx, attempt_no), "started_at_ms": started_ms,
                               "expires_at_ms": started_ms + int(1000 * attempt_s),
                               "calibration_version": calibration_version}
                    try:
                        first = checker.start(setup_session_id, doc, expires_at_ms=attempt["expires_at_ms"],
                                              required_pairs=required, attempt_id=attempt["id"])  # type: ignore[arg-type]
                    except Exception as exc:  # 확인 채널을 열지 못하면 맵을 굽지 않는다
                        raise JobFailed(CAMERA_CHECK_UNAVAILABLE,
                                        f"카메라 확인을 열지 못했습니다({type(exc).__name__}: {exc})"[:480],
                                        retryable=False) from exc
                    log.info("작업 %s: 카메라 확인 시도 %d 시작(%s, 상한 %.0fs)", ctx.job_id, attempt_no, attempt["id"],
                             attempt_s)
                    attempt_started, attempt_deadline = now, now + attempt_s
                    waiting_user = False
                    last = {k: v for k, v in first.items() if k != "user_wait"}
                    last["attempt"] = attempt
                    self._set_camera_check(ctx, base, last, force=True)
                    continue
                if now >= stage_deadline:
                    break
                self._sleep(ctx.cancelled, min(self._cc_poll_s, stage_deadline - now))
                continue
            try:
                polled = checker.poll()
            except Exception:
                log.exception("작업 %s: 카메라 확인 평가 실패(다음 주기에 다시 본다)", ctx.job_id)
                polled = None
            if polled is not None:
                last = {k: v for k, v in polled.items() if k != "user_wait"}
                last["attempt"] = attempt
            status = last.get("status")
            if status == "PASSED":
                self._set_camera_check(ctx, base, last, force=True)
                log.info("작업 %s: 카메라 확인 시도 %d 통과. 맵 단계로 간다", ctx.job_id, attempt_no)
                return
            lines = getattr(checker, "lines_read", None)
            if isinstance(lines, int) and lines == 0 and now - attempt_started >= self._cc_channel_s:
                checker.stop()
                raise JobFailed(CAMERA_CHECK_UNAVAILABLE,
                                f"카메라 확인 입력(Tier 0 관제 view)이 {self._cc_channel_s:.0f}초 동안 오지 않았습니다. "
                                "관제 연결을 확인한 뒤 세팅을 다시 시작하세요", retryable=False)
            if status not in CAMERA_CHECK_FINAL and now >= attempt_deadline:
                closed = checker.incomplete()
                if closed is not None:
                    last = {k: v for k, v in closed.items() if k != "user_wait"}
                    last["attempt"] = attempt
                    status = last.get("status")
            if status in CAMERA_CHECK_FINAL:
                log.warning("작업 %s: 카메라 확인 시도 %d %s: %s", ctx.job_id, attempt_no, status,
                            [p.get("reason") for p in last.get("problems", [])])
                waiting_user = True
                if stage_deadline - self._monotonic() < self._cc_min_s:
                    break  # 다시 걸을 시간이 없다. 마감 처리
                last = {**last, "user_wait": self._user_wait(stage_deadline)}
                self._set_camera_check(ctx, base, last, force=True)
                continue
            self._set_camera_check(ctx, base, last, force=False)
            self._sleep(ctx.cancelled, max(0.01, min(self._cc_poll_s, attempt_deadline - now)))
        self._close_camera_check(ctx, base, last, attempt, budget)

    def _close_camera_check(self, ctx: JobContext, base: Mapping[str, Any], last: Mapping[str, Any],
                            attempt: Mapping[str, Any] | None, budget: float) -> NoReturn:
        """대기 마감: 더는 다시 확인을 받지 않는다. user_wait 를 뺀 마지막 요약을 남기고 맵 없이 실패로 끝낸다."""
        if self._camera_check is not None:
            self._camera_check.stop()
        final = {k: v for k, v in last.items() if k != "user_wait"}
        if final.get("status") not in ("FAILED", "INCOMPLETE"):
            # 걷기 시작을 받지 못했다(AWAITING_START 그대로). 시도가 없으니 표본 부족이 아니라 시작 안 함이다
            final["status"] = "INCOMPLETE"
            final["problems"] = [{"kind": "CHECK", "id": "camera_check", "camera_ids": [], "reason": "NOT_STARTED",
                                  "message": "걷기 시작을 받지 못했습니다. 세팅을 다시 시작하세요"}]
        self._set_camera_check(ctx, base, final, force=True)
        reasons = "; ".join(str(p.get("message")) for p in final.get("problems", []))
        no = int(attempt["no"]) if attempt is not None else 0
        log.warning("작업 %s: 카메라 확인을 대기 마감(%.0fs)까지 통과하지 못해 맵을 만들지 않는다(시도 %d)", ctx.job_id, budget, no)
        what = "「걷기 시작」을 받지 못해" if attempt is None else f"{no}번 걸었지만 통과하지 못해"
        raise JobFailed(CAMERA_CHECK_NOT_PASSED, f"카메라 확인을 {what} 맵을 만들지 않았습니다: {reasons}"[:480],
                        retryable=False)

    def _wait_for_backgrounds(
        self, ctx: JobContext, setup_session_id: str, site_id: str, camera_ids: tuple[str, ...],
        mode_fields: Mapping[str, Any] | None = None,
    ) -> tuple[CleanBackgroundBundle, dict[str, float]]:
        """(a) 묶음이 완성될 때까지 최대 background_wait_s 동안 다시 본다. 수집률이 바뀌면 COLLECTING_BACKGROUND 하트비트.

        진행률은 가장 느린 카메라 기준으로 0~14 구간에서 단조 증가(서버가 같은 대여의 감소를 거절)하고, 카메라별 수집률은 실제 값을 그대로 싣는다
        (활성 보정이 바뀌어 묶음을 새로 모으면 내려갈 수 있다). 시간이 다하면 재시도 가능한 BACKGROUND_INCOMPLETE."""
        deadline = self._monotonic() + self._bg_wait_s
        best_pct = 0.0
        last_sent: tuple[float, tuple[float, ...], str] | None = None
        while True:
            self._check_cancelled(ctx)
            bundle = self._collect_bg(setup_session_id=setup_session_id, site_id=site_id, camera_ids=camera_ids)
            pct = {cid: round(min(100.0, max(0.0, float(bundle.collection_pct_by_cam.get(cid, 0.0)))), 2)
                   for cid in camera_ids}
            missing = bundle.missing_cameras(camera_ids)
            if not missing:
                return bundle, pct
            # 마지막 상태도 화면에 남도록 시간 초과 판단보다 먼저 보낸다(어느 카메라가 왜 못 됐는지, -686)
            best_pct = max(best_pct, round(14.0 * min(pct.values()) / 100.0, 1))
            capture = dict(bundle.capture) if bundle.capture is not None else None
            key = (best_pct, tuple(pct[c] for c in camera_ids), repr(capture))
            if key != last_sent:
                self._emit_progress(ctx, {
                    "setup_session_id": setup_session_id,
                    "status": "COLLECTING_BACKGROUND",
                    "progress_pct": best_pct,
                    "background_collection_pct": pct,
                    **({"background_capture": capture} if capture is not None else {}),
                    **(mode_fields or {}),
                })
                last_sent = key
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                states = {cid: cam.get("state") for cid, cam in (capture or {}).get("cameras", {}).items()
                          if cid in missing}
                raise JobFailed(
                    "BACKGROUND_INCOMPLETE",
                    f"사람 지운 배경 수집 미완료 카메라: {missing} ({pct}{', ' + str(states) if states else ''}), "
                    f"{self._bg_wait_s:g}s 대기",
                    retryable=True,
                )
            self._sleep(ctx.cancelled, min(self._bg_poll_s, remaining))

    def _review_heights(
        self,
        ctx: JobContext,
        base: Mapping[str, Any],
        camera_ids: tuple[str, ...],
        heights: dict[str, float],
        init_world: InitialWorldReconstruction,
        reinitialize: Callable[[Mapping[str, float]], InitialWorldReconstruction],
    ) -> tuple[dict[str, float], InitialWorldReconstruction, dict[str, Any]]:
        """카메라 높이 빠른 확인(-690, 계약 site_setup 1.17.0 height_review). (진행할 높이, World, height_review) 를 돌려준다.

        모두 기준 안이면 PASSED(기다리지 않음). 넘는 카메라가 있으면 AWAITING_CONFIRMATION 스냅샷(height_checks 없음)을 보내고
        하트비트 응답의 관리자 결정(ctx.height_decision)을 기다린다. 고친 높이면 같은 점구름·포즈로 World 를 다시 맞추고(CORRECTED),
        같은 높이면 그대로(CONFIRMED), 시간이 다하면 입력 그대로(TIMED_OUT). 재대여처럼 결정이 이미 와 있으면 기다리지 않고 쓴다."""
        def review(status: str, hs: Mapping[str, float], cams: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
            return {"status": status, "max_relative_diff": self._hr_max,
                    "camera_heights_m": {c: round(float(hs[c]), 4) for c in camera_ids}, "cameras": dict(cams), **extra}

        cams = compute_height_review(heights, init_world.restored_heights_m, max_relative_diff=self._hr_max)
        off = sorted(c for c, v in cams.items() if v["verdict"] != "OK")
        decision = ctx.height_decision
        if decision is None and not off:
            return heights, init_world, review(HEIGHT_REVIEW_PASSED, heights, cams)
        if decision is None:
            wait_s = self._hr_wait_s
            remaining = ctx.remaining_s()
            if remaining is not None:
                wait_s = min(wait_s, max(0.0, remaining - self._hr_reserve_s))
            if wait_s > 0:
                self._emit_progress(ctx, {**base, "height_review": review(HEIGHT_REVIEW_AWAITING, heights, cams,
                                                                           wait_s=round(wait_s, 1))})
                decision = self._wait_height_decision(ctx, wait_s)
            if decision is None:
                log.warning("작업 %s: 카메라 높이 확인을 %.0fs 안에 받지 못해 입력 높이로 진행한다(어긋난 카메라 %s)",
                            ctx.job_id, wait_s, off)
                return heights, init_world, review(HEIGHT_REVIEW_TIMED_OUT, heights, cams)
        decided = {c: float(decision["camera_heights_m"].get(c, heights[c])) for c in camera_ids}
        if all(abs(decided[c] - heights[c]) <= 1e-6 for c in camera_ids):
            log.info("작업 %s: 관리자가 카메라 높이 입력이 실측과 맞다고 확인했다(어긋난 카메라 %s)", ctx.job_id, off)
            return heights, init_world, review(HEIGHT_REVIEW_CONFIRMED, heights, cams)
        self._check_cancelled(ctx)
        rebuilt = init_world.rebuild(decided) if init_world.rebuild is not None else reinitialize(decided)
        cams = compute_height_review(decided, rebuilt.restored_heights_m, max_relative_diff=self._hr_max)
        log.info("작업 %s: 고친 카메라 높이로 World 를 다시 맞췄다(%s)", ctx.job_id,
                 {c: decided[c] for c in camera_ids if abs(decided[c] - heights[c]) > 1e-6})
        return decided, rebuilt, review(HEIGHT_REVIEW_CORRECTED, decided, cams)

    def _wait_height_decision(self, ctx: JobContext, wait_s: float) -> dict[str, Any] | None:
        """관리자 결정을 최대 wait_s 초 기다린다. 결정은 하트비트 스레드가 ctx 에 넣는다. 취소·시간 초과는 그대로 낸다."""
        deadline = self._monotonic() + wait_s
        while True:
            self._check_cancelled(ctx)
            decision = ctx.height_decision
            if decision is not None:
                return decision
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            self._sleep(ctx.cancelled, min(self._hr_poll_s, remaining))

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
        if outcome.zone_height_changes is not None:
            result["zone_height_changes"] = dict(outcome.zone_height_changes)
        errs = schema_errors(result, SITE_SETUP_RESULT_SCHEMA)
        if errs:
            raise JobFailed("INTERNAL_CONTRACT_ERROR", f"site_setup 다시 굽기 결과가 계약을 위반했다: {'; '.join(errs)}",
                            retryable=False)
        return result

    @staticmethod
    def _fallback_zone_height_changes(
        ctx: JobContext,
        *,
        anchor: Mapping[str, Any] | None,
        compared_scene_version: str | None,
        reason_override: str | None = None,
    ) -> dict[str, Any] | None:
        """주입점이 `zone_height_changes` 를 직접 주지 않았을 때, 입력에 이전 맵·구역이 있으면 UNKNOWN 요약을 만든다."""
        if "previous_zones" not in ctx.input and "previous_scene" not in ctx.input:
            return None
        prev_zones = list(ctx.input.get("previous_zones") or [])
        prev_scene = ctx.input.get("previous_scene")
        prev_ver = (
            str(prev_scene["scene_version"])
            if isinstance(prev_scene, Mapping) and prev_scene.get("scene_version")
            else None
        )
        if reason_override is not None:
            reason = reason_override
        elif not isinstance(prev_scene, Mapping) or not prev_scene.get("uri"):
            reason = REASON_NO_PREVIOUS_SCENE
        elif anchor is None or anchor.get("accepted") is not True:
            reason = REASON_WORLD_NOT_ANCHORED
        else:
            reason = REASON_SCENE_UNREADABLE
        return build_unknown_zone_height_changes(
            prev_zones,
            reason=reason,
            previous_scene_version=prev_ver,
            compared_scene_version=compared_scene_version,
        )

    def _reject_reuse(self, ctx: JobContext, refining_snapshot: Mapping[str, Any], mode_fields: Mapping[str, Any],
                      anchor: Mapping[str, Any]) -> NoReturn:
        """REUSE_SCENE 의 World 다시 맞추기 거부(S15P31A101-689): 거부 요약을 실은 스냅샷을 보내고 실패로 끝낸다.

        -614 는 같은 작업 안에서 전체 세팅(새 맵)으로 이어 갔지만, 사용자 결정(11-13~14)으로 저절로 새 맵으로 넘어가지 않는다. 스냅샷은
        mode=REUSE_SCENE 그대로(판정 전과 같은 모드) world_anchor(accepted=false, changed_cameras)를 싣고, 새 보정 문서는 등록하지 않는다
        (Tier 0 는 지금 보정을 그대로 쓴다). 서버는 이 요약을 세션에 두고 화면이 "카메라 n 이 직전 세팅과 달라졌습니다 / 새 맵으로 다시 하세요"
        를 보인다. 같은 입력으로 다시 대여해도 결과가 같으므로 재시도하지 않는다(retryable=False)."""
        last_pct = float((self._last_payload or {}).get("progress_pct", 0.0))
        self._emit_progress(ctx, {**refining_snapshot, **mode_fields, "progress_pct": max(62.0, last_pct + 0.1),
                                  "world_anchor": dict(anchor)})
        changed = list(anchor.get("changed_cameras") or [])
        if changed:
            message = f"카메라 {', '.join(changed)} 이(가) 직전 세팅과 달라져 이전 맵을 쓸 수 없습니다. 새 맵으로 다시 하세요"
        elif anchor["reason"] in MOVED_ANCHOR_REASONS:
            message = "카메라 배치가 직전 세팅과 달라져 이전 맵을 쓸 수 없습니다. 새 맵으로 다시 하세요"
        else:
            message = "직전 세팅과 비교할 수 없어 이전 맵을 쓸 수 없습니다. 새 맵으로 다시 하세요"
        log.warning("작업 %s: 이전 맵 재사용을 요청했지만 World 다시 맞추기가 거부돼(%s, 카메라 %s) 실패로 멈춘다",
                    ctx.job_id, anchor["reason"], changed or "-")
        raise JobFailed(REUSE_WORLD_REJECTED, f"{message} ({anchor['reason']})"[:480], retryable=False)

    def _finish_reuse(
        self, ctx: JobContext, joint_ba_snapshot: Mapping[str, Any], stage_seconds: dict[str, float],
        stage_a: StageAResult, t_start: float,
    ) -> Mapping[str, Any]:
        """REUSE_SCENE 채택(-614): JOINT_BA_AND_VERIFY 스냅샷(새 보정 문서·높이 확인·world_anchor)을 그대로 READY_FOR_ZONES 로 올리고
        COMPLETED 로 끝낸다. 미리보기·정식 맵·카메라 프레임·굽기 사진을 만들지 않는다(현재 관제 맵과 그 구역을 그대로 쓴다)."""
        self._check_cancelled(ctx)
        stage_seconds["READY_FOR_ZONES"] = 0.0
        metrics = {"stage_seconds": dict(stage_seconds), "joint_ba": stage_a.summary_metrics}
        reuse_zhc = self._fallback_zone_height_changes(
            ctx,
            anchor={"accepted": True},
            compared_scene_version=None,
            reason_override=REASON_REUSE_SCENE,
        )
        zhc_fields = {"zone_height_changes": reuse_zhc} if reuse_zhc is not None else {}
        self._emit_progress(
            ctx,
            {
                **joint_ba_snapshot,
                "status": "READY_FOR_ZONES",
                "progress_pct": 85,
                "metrics": metrics,
                **zhc_fields,
            },
        )
        result: dict[str, Any] = self._with_checks({
            **joint_ba_snapshot,
            "status": "COMPLETED",
            "progress_pct": 100,
            "metrics": {**metrics, "total_seconds": round(max(0.0, self._perf() - t_start), 4)},
            **zhc_fields,
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
        self._cc_sent_at = float("-inf")
        try:
            return self._run(ctx)
        finally:
            if self._camera_check is not None:
                try:
                    self._camera_check.stop()
                except Exception:
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
        remaining = ctx.remaining_s()
        if remaining is not None and remaining < SETUP_TIME_BUDGET_S - 30.0:
            # 옛 EC2(실행 상한 900 s)면 사람 대기 몫이 먼저 줄어든다(높이 확인을 묻지 않거나 카메라 확인 대기가 짧아짐). 맵 몫은 지킨다
            log.warning("작업 %s: 실행 상한까지 %.0fs 로 세팅 시간 예산 합(%.0fs)보다 짧다. 사람 대기 몫을 줄여 맵 몫(%.0fs)을 지킨다",
                        ctx.job_id, remaining, SETUP_TIME_BUDGET_S, self._cc_reserve_s)

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
        def init_world_with(heights: Mapping[str, float], progress: Callable[[float], None] | None) -> InitialWorldReconstruction:
            try:
                return self._init_world(
                    setup_session_id=setup_session_id,
                    site_id=site_id,
                    camera_ids=camera_ids,
                    camera_heights_m=heights,
                    height_tolerance_cm=height_tolerance_cm,
                    backgrounds=bg_bundle,
                    created_at_ms=created_at_ms,
                    progress=progress,
                )
            except JobFailed:
                raise
            except ValueError as exc:
                raise JobFailed(
                    "FLOOR_FIT_FAILED",
                    f"초기 World 복원 및 바닥 평면 맞춤 실패: {exc}",
                    retryable=False,
                ) from exc

        init_world = init_world_with(camera_heights_m, self._subprogress(ctx, initial_snapshot, 15, 39))

        # 카메라 높이 빠른 확인(-690): 기준을 넘는 카메라가 있으면 관리자 확인을 기다리고, 고친 높이면 같은 모델 높이로 다시 계산한다
        camera_heights_m, init_world, height_review = self._review_heights(
            ctx,
            {
                "setup_session_id": setup_session_id,
                "status": "INITIAL_POSE_AND_WORLD",
                "progress_pct": 39,
                "background_collection_pct": bg_pct,
                **mode_fields,
            },
            camera_ids,
            camera_heights_m,
            init_world,
            lambda heights: init_world_with(heights, None),
        )
        heights_settled = height_review["status"] in HEIGHT_REVIEW_SETTLED

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
                "height_review": height_review,
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
        # 앞의 높이 빠른 확인에서 맞았거나 관리자가 확인한 높이는 같은 높이 비교로 다시 거절하지 않는다(-690)
        self._quality_gate = evaluate_quality_gate(stage_a.pair_metrics, stage_a.height_checks, self._quality_gate_config,
                                                   heights_settled=heights_settled)
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
                # 카메라가 움직였거나 비교할 기준이 없다. 새 맵(약 13분)으로 저절로 넘어가지 않고 여기서 멈춘다(S15P31A101-689):
                # 거부 요약(달라진 카메라)을 실은 스냅샷을 먼저 보내고, 새 보정은 등록하지 않은 채 실패로 끝낸다. 관리자가 새 맵을 고른다
                if anchor is None:
                    anchor = {"accepted": False, "reason": NO_ANCHOR_REFERENCE, "reference_calibration_version": None,
                              "changed_cameras": []}
                self._reject_reuse(ctx, refining_snapshot, mode_fields, anchor)
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
        # 사람 걷기 카메라 확인(-646 → -687): 맵(미리보기·굽기) 전에 끝낸다. 통과하지 못하면 여기서 실패하고 맵을 한 번도 굽지 않는다.
        # 관리자의 「걷기 시작」·「확인만 다시」를 기다리는 동안 NEW 는 미리보기·굽기 몫, REUSE 는 결과 제출 몫을 실행 상한에 남긴다
        self._run_camera_check(ctx, joint_ba_snapshot, setup_session_id, calibration_doc, calibration_version,
                               reserve_s=self._cc_reuse_reserve_s if reuse_scene else self._cc_reserve_s)

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
        preview_zhc = (
            dict(preview.zone_height_changes)
            if preview.zone_height_changes is not None
            else self._fallback_zone_height_changes(
                ctx,
                anchor=anchor,
                compared_scene_version=preview.preview_scene_version,
            )
        )
        if preview_zhc is not None:
            preview_snapshot["zone_height_changes"] = preview_zhc
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
        baked_zhc: Mapping[str, Any] | None = None
        if isinstance(baked, BakedScene):
            scene_version = baked.scene_version
            if baked.scene_asset is not None:
                artifacts["scene"] = baked.scene_asset.to_json()
            supplement = baked.supplement
            baked_zhc = baked.zone_height_changes
        else:
            scene_version = str(baked)
        stage_seconds["BAKING_TEXTURE"] = round(
            max(0.0, self._perf() - t0), 4
        )
        self._emit_progress(ctx, {**baking_snapshot, "progress_pct": 95})
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
        final_zhc = dict(baked_zhc) if baked_zhc is not None else preview_zhc
        if final_zhc is not None:
            result["zone_height_changes"] = final_zhc
        result = self._with_checks(result)

        errs = schema_errors(result, SITE_SETUP_RESULT_SCHEMA)
        if errs:
            raise JobFailed(
                "INTERNAL_CONTRACT_ERROR",
                f"site_setup 결과가 계약을 위반했다: {'; '.join(errs)}",
                retryable=False,
            )
        return result
