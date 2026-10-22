"""다시점 삼각측량 (명세 5.2 삼각측량·계산 순서 3단계·World 위치의 정의).

공통 시각으로 보간한 카메라별 2D keypoint(`interpolation.InterpolatedView`)와 보정(`rig.CameraRig`)으로
사람 한 명의 관절 3D 를 만든다. 카메라 간 연결(같은 사람 묶기)은 호출자가 이미 했다고 본다.

- 유효 관측(신뢰도 기준 이상)이 2대 이상인 관절만 삼각측량한다. 1대 이하는 null, visible=false, UNAVAILABLE 이다.
  값(u, v, 신뢰도)이 유한하지 않거나 렌즈 왜곡을 제거할 수 없는(`RigCamera.normalize_with_mask`) keypoint 는
  관측으로 세지 않는다. 예외로 멈추지 않고 관측 수가 줄어든 대로 판정한다.
- 왜곡 제거는 view(카메라)마다 keypoint 전체를 한 번에 한다(`RigCamera.undistort_keypoints`). 관절·카메라마다 1점씩
  풀면 호출 고정 비용 때문에 10명 기준 10 Hz 를 넘는다(S15P31A101-168). 호출자가 이미 푼 결과를 `undistorted` 로
  넘기면 다시 풀지 않는다(Cross-view·관절 게이트가 같은 view 를 다시 쓴다). 점별 결과는 1점씩 풀 때와 같다.
- 선형 DLT(왜곡 제거 정규화 좌표)로 초기값을 구하고 가우스-뉴턴으로 픽셀 재투영 오차를 줄인다. 사람 한 명의 관절은
  먼저 한 번에(관절 묶음 행렬 연산) 풀고, 이상치·퇴화가 있는 관절만 관절별 순차 경로로 다시 푼다. 관절마다의 판단
  (해 유무, 카메라 앞, 감쇠·멈춤)은 관절 하나씩 풀 때와 같다.
- 재투영 오차가 이상치 기준을 넘는 카메라는 빼고 다시 푼다. 빼고 나서 기준 안의 관측이 2대 미만이면 null 이다
  (2대를 채우려고 이상치 관측을 세지 않는다).
- 시선이 같은 직선이면(공선) 해가 유일하지 않아 null 이다. 시선각은 방향이 아니라 직선 사이 각(0~90°)으로 본다.
- 관절 상태는 최소 기준만 본다: 관측 카메라 수, 재투영 오차, 시선각, 보정 상태. 시간 일관성(속도·뼈 길이)과
  삼각측량 불확실성을 더한 관절 유효성 게이트는 S15P31A101-29 가 이 인터페이스 위에 만든다.
- 이 모듈의 관절은 모두 Tier 0 관측(joint_source = observed)이다. 접지점(ground_anchor_xyz)은 ContactEstimator 가
  정한다. 여기서는 만들지 않는다(Z=0 대체 금지).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.rig import CameraRig, RigCamera, UndistortedKeypoints

# 명세 5.2: 2대 이상에서 보인 관절만 삼각측량한다
MIN_VIEWS = 2
# COCO17 엉덩이 관절 번호 (contracts common joint_layout 순서)
COCO17_L_HIP, COCO17_R_HIP = 11, 12
JOINT_COUNTS = {"COCO17": 17, "HALPE26": 26}
# DLT 계수 행렬의 두 번째로 작은 특이값이 최대 특이값의 이 비율 이하이면 해가 유일하지 않다고 본다
DLT_RANK_TOLERANCE = 1e-9
# 이 보정 상태에서는 관절을 VALID 로 확정하지 않는다 (명세 4.4: DRIFTED 는 재투영 오차 증가·해상도 변경)
UNTRUSTED_CALIBRATION_STATES = frozenset({"UNCALIBRATED", "DRIFTED"})


class JointValidity(StrEnum):
    """관절 상태 (contracts common validity, 명세 5.2 3D 관절 유효성 게이트)."""

    VALID = "VALID"
    UNCERTAIN = "UNCERTAIN"
    UNAVAILABLE = "UNAVAILABLE"


class JointIssue(StrEnum):
    """VALID 가 아닌 사유. 여러 개가 함께 붙을 수 있다."""

    TOO_FEW_VIEWS = "TOO_FEW_VIEWS"
    """유효 관측 2대 미만"""
    DEGENERATE = "DEGENERATE"
    """해가 없거나 유일하지 않거나(시선 공선) 카메라 뒤에 있다"""
    INCONSISTENT_VIEWS = "INCONSISTENT_VIEWS"
    """이상치 기준을 넘는 관측을 빼고 나면 서로 맞는 관측이 2대 미만이다"""
    HIGH_REPROJECTION_ERROR = "HIGH_REPROJECTION_ERROR"
    NARROW_RAY_ANGLE = "NARROW_RAY_ANGLE"
    """시선 사이 각이 작아 깊이·높이가 불안정하다"""
    UNTRUSTED_CALIBRATION = "UNTRUSTED_CALIBRATION"


@dataclass(frozen=True)
class TriangulationConfig:
    """최소 기준 초기값. 근거와 SIM 측정값은 docs/design/tier0-triangulation.md."""

    min_views: int = MIN_VIEWS
    min_keypoint_confidence: float = 0.3
    """이 값보다 신뢰도가 낮은 2D keypoint 는 관측으로 세지 않는다"""
    max_reprojection_error_px: float = 8.0
    """VALID 상한: 쓴 카메라 중 최대 재투영 오차 (G5 한 카메라 제외 재투영 오차 p95 8px 와 같은 크기)"""
    outlier_reprojection_error_px: float = 20.0
    """이 값을 넘는 카메라는 빼고 다시 푼다. 남은 관측이 2대 미만이면 null"""
    min_ray_angle_rad: float = math.radians(5.0)
    """VALID 하한: 쓴 카메라 쌍의 시선 직선 사이 각(0~90°) 최댓값"""
    refine_iterations: int = 10

    def __post_init__(self) -> None:
        if self.min_views < MIN_VIEWS:
            raise ValueError(f"min_views 는 {MIN_VIEWS} 이상이어야 한다 (1대로 삼각측량하지 않는다): {self.min_views}")
        if not self.outlier_reprojection_error_px >= self.max_reprojection_error_px:
            raise ValueError("outlier_reprojection_error_px 는 max_reprojection_error_px 이상이어야 한다")


@dataclass(frozen=True)
class JointEstimate:
    """관절 하나의 삼각측량 결과. xyz 가 None 이면 visible=false, validity=UNAVAILABLE 이다."""

    xyz: tuple[float, float, float] | None
    validity: JointValidity
    camera_ids: tuple[str, ...] = ()
    """삼각측량에 쓴 카메라"""
    rejected_camera_ids: tuple[str, ...] = ()
    """관측은 있었지만 쓰지 않은 카메라. 뺀 순서대로이고, 서로 맞는 관측이 남지 않았으면 마지막 관측들도 들어간다"""
    reprojection_error_px: float | None = None
    """쓴 카메라 중 최대 재투영 오차 (왜곡 제거 픽셀)"""
    ray_angle_rad: float | None = None
    """쓴 카메라 쌍의 시선 직선 사이 각(0~π/2) 최댓값"""
    issues: tuple[JointIssue, ...] = ()

    @property
    def visible(self) -> bool:
        """joint_visibility: 2대 이상 유효 관측으로 3D 위치를 얻었는지."""
        return self.xyz is not None


@dataclass(frozen=True, eq=False)
class SkeletonInputs:
    """삼각측량 결과를 만든 입력. 다른 단계가 같은 입력의 결과를 다시 쓸 수 있는지 확인하는 데만 쓴다."""

    config: TriangulationConfig
    calibration_version: str
    calibration_state: str
    undistorted: tuple[UndistortedKeypoints, ...]
    """view 순서의 왜곡 제거 결과 (카메라 모델·원본 keypoint 포함)"""


@dataclass(frozen=True)
class SkeletonEstimate:
    """사람 한 명의 공통 시각 t 관절 3D. World 미터, 오른손, +Z 상방."""

    t: int
    joint_layout: str
    joints: tuple[JointEstimate, ...]
    calibration_version: str
    camera_ids: tuple[str, ...] = ()
    """하나 이상의 관절 삼각측량에 쓴 카메라 (world_track.cameras)"""
    inputs: SkeletonInputs | None = field(default=None, compare=False, repr=False)
    """이 결과를 만든 입력 (`reusable_for`). 직접 만든 SkeletonEstimate 는 None 이다"""

    def reusable_for(
        self,
        views: Sequence[InterpolatedView],
        rig: CameraRig,
        joint_layout: str,
        config: TriangulationConfig,
    ) -> bool:
        """같은 view(순서·카메라·keypoint·시각)·같은 보정(버전·상태·카메라 모델)·같은 관절 배치·같은 설정으로
        `triangulate_skeleton` 을 부르면 이 결과와 같은지. 확인할 수 없으면 False(다시 계산한다)."""
        inputs = self.inputs
        if inputs is None or self.joint_layout != joint_layout or inputs.config != config:
            return False
        if (inputs.calibration_version, inputs.calibration_state) != (rig.calibration_version, rig.state):
            return False
        if len(views) != len(inputs.undistorted):
            return False
        for view, und in zip(views, inputs.undistorted):
            camera = rig.cameras.get(view.camera_id)
            if view.t != self.t or camera is None or view.camera_id != und.camera_id:
                return False
            if not und.matches(camera, view.keypoints):
                return False
        return True

    def to_skeleton(self) -> dict[str, Any]:
        """`world_state.schema.json#/$defs/skeleton` 문서. 관절은 모두 Tier 0 관측(observed)이다."""
        return {
            "joint_layout": self.joint_layout,
            "joints": [list(j.xyz) if j.xyz is not None else None for j in self.joints],
            "visible": [j.visible for j in self.joints],
            "validity": [str(j.validity) for j in self.joints],
            "joint_source": ["observed"] * len(self.joints),
        }

    def pelvis_xyz(self) -> tuple[float, float, float] | None:
        """골반 위치 = 양쪽 엉덩이 관절의 중점 (명세 5.2 pelvis_xyz). 하나라도 없으면 None(비움)."""
        if self.joint_layout != "COCO17":
            raise ValueError(f"골반 정의가 없는 관절 배치다: {self.joint_layout}")
        left, right = self.joints[COCO17_L_HIP].xyz, self.joints[COCO17_R_HIP].xyz
        if left is None or right is None:
            return None
        return tuple((a + b) / 2.0 for a, b in zip(left, right))  # type: ignore[return-value]

    def pelvis_validity(self) -> JointValidity:
        """골반 상태: 두 엉덩이 상태 중 약한 쪽."""
        order = [JointValidity.UNAVAILABLE, JointValidity.UNCERTAIN, JointValidity.VALID]
        left, right = self.joints[COCO17_L_HIP].validity, self.joints[COCO17_R_HIP].validity
        return order[min(order.index(left), order.index(right))]


def triangulate_skeleton(
    views: Mapping[str, InterpolatedView] | Sequence[InterpolatedView],
    rig: CameraRig,
    joint_layout: str = "COCO17",
    config: TriangulationConfig = TriangulationConfig(),
    *,
    undistorted: Mapping[str, UndistortedKeypoints] | None = None,
) -> SkeletonEstimate:
    """같은 사람의 카메라별 보간 keypoint 로 관절 3D 를 만든다. 모든 view 는 같은 공통 시각이어야 한다.

    undistorted: 카메라 ID → 그 view keypoint 를 이미 왜곡 제거한 결과(`RigCamera.undistort_keypoints`). 이 보정의
    카메라로 그 view 의 keypoint 를 푼 것만 쓰고(`UndistortedKeypoints.matches`), 없거나 맞지 않으면 여기서 푼다.
    """
    return triangulate_skeletons([views], rig, joint_layout, config, undistorted=[undistorted])[0]


def triangulate_skeletons(
    people: Sequence[Mapping[str, InterpolatedView] | Sequence[InterpolatedView]],
    rig: CameraRig,
    joint_layout: str = "COCO17",
    config: TriangulationConfig = TriangulationConfig(),
    *,
    undistorted: Sequence[Mapping[str, UndistortedKeypoints] | None] | None = None,
) -> list[SkeletonEstimate]:
    """여러 사람의 `triangulate_skeleton` 을 한 번에 한다. 결과는 사람 순서이고 사람마다 따로 부른 것과 같다.

    관절 1차 풀이를 사람 전체에 대해 행렬 연산 한 번으로 해 호출 고정 비용을 한 번만 낸다(같은 시각 클러스터 전체 등).
    undistorted: 사람마다 `triangulate_skeleton` 의 undistorted (없으면 None).
    """
    if undistorted is None:
        undistorted = [None] * len(people)
    if len(undistorted) != len(people):
        raise ValueError(f"undistorted 는 사람 수({len(people)})와 같아야 한다: {len(undistorted)}")
    view_lists = [list(views.values()) if isinstance(views, Mapping) else list(views) for views in people]
    # 넘겨받지 않은 view 는 카메라마다 모아 한 번에 왜곡 제거한다(보정에 없는 카메라는 아래 검증이 KeyError 로 막는다)
    merged = [dict(und) if und is not None else {} for und in undistorted]
    missing: dict[str, list[tuple[int, InterpolatedView]]] = {}
    for n, views in enumerate(view_lists):
        for v in views:
            camera = rig.cameras.get(v.camera_id)
            cached = merged[n].get(v.camera_id)
            if camera is not None and (cached is None or not cached.matches(camera, v.keypoints)):
                missing.setdefault(v.camera_id, []).append((n, v))
    for cid, items in missing.items():
        for (n, v), und in zip(items, rig.cameras[cid].undistort_many([v.keypoints for _, v in items])):
            merged[n][cid] = und
    inputs = [_PersonInput.prepare(views, rig, joint_layout, config, und) for views, und in zip(view_lists, merged)]
    first = _first_pass(inputs, rig, config)
    return [inp.assemble(first.get(n, {}), rig, joint_layout, config) for n, inp in enumerate(inputs)]


@dataclass(frozen=True)
class _PersonInput:
    """사람 한 명의 삼각측량 입력 (검증·왜곡 제거를 마친 것). 배열은 (view, 관절) 순서다."""

    views: list[InterpolatedView]
    cameras: list[RigCamera]
    undistorted: list[UndistortedKeypoints]
    usable: np.ndarray
    normalized: np.ndarray
    targets: np.ndarray
    confidence: np.ndarray

    @classmethod
    def prepare(
        cls,
        views: Mapping[str, InterpolatedView] | Sequence[InterpolatedView],
        rig: CameraRig,
        joint_layout: str,
        config: TriangulationConfig,
        undistorted: Mapping[str, UndistortedKeypoints] | None,
    ) -> _PersonInput:
        view_list = list(views.values()) if isinstance(views, Mapping) else list(views)
        if not view_list:
            raise ValueError("view 가 없다")
        n_joints = JOINT_COUNTS.get(joint_layout)
        if n_joints is None:
            raise ValueError(f"지원하지 않는 관절 배치: {joint_layout}")
        times = {v.t for v in view_list}
        if len(times) > 1:
            raise ValueError(f"공통 시각이 다른 view 가 섞여 있다: {sorted(times)}")
        ids = [v.camera_id for v in view_list]
        if len(ids) != len(set(ids)):
            raise ValueError(f"같은 카메라의 관측이 두 번 있다 (한 사람에 같은 카메라의 두 검출을 묶지 않는다): {ids}")
        for v in view_list:
            if len(v.keypoints) != n_joints:
                raise ValueError(f"{v.camera_id}: {joint_layout} 는 keypoint {n_joints}개인데 {len(v.keypoints)}개다")
        cameras = rig.subset(ids)
        und = [undistort_view(cam, v, undistorted) for cam, v in zip(cameras, view_list)]
        return cls(
            views=view_list,
            cameras=cameras,
            undistorted=und,
            usable=np.array([u.usable(config.min_keypoint_confidence) for u in und]),
            normalized=np.stack([u.normalized for u in und]),
            targets=np.stack([u.ideal_pixels for u in und]),
            confidence=np.stack([u.confidence for u in und]),
        )

    def assemble(
        self,
        first: Mapping[int, tuple[np.ndarray, float, float]],
        rig: CameraRig,
        joint_layout: str,
        config: TriangulationConfig,
    ) -> SkeletonEstimate:
        """1차 풀이로 확정한 관절은 그대로, 나머지는 관절별 순차 경로로 푼다."""
        trusted = rig.state not in UNTRUSTED_CALIBRATION_STATES
        joints = []
        for j in range(self.usable.shape[1]):
            idx = np.flatnonzero(self.usable[:, j])
            cams = [self.cameras[k] for k in idx]
            if j in first:
                X_j, reproj, angle = first[j]
                joints.append(_estimate(cams, X_j, reproj, angle, (), trusted, config))
            else:
                obs = _Observations(cams, self.normalized[idx, j], self.targets[idx, j], self.confidence[idx, j])
                joints.append(_triangulate_joint(obs, trusted, config))
        used = sorted({cid for j in joints for cid in j.camera_ids})
        return SkeletonEstimate(
            t=self.views[0].t,
            joint_layout=joint_layout,
            joints=tuple(joints),
            calibration_version=rig.calibration_version,
            camera_ids=tuple(used),
            inputs=SkeletonInputs(config, rig.calibration_version, rig.state, tuple(self.undistorted)),
        )


def _first_pass(
    inputs: Sequence[_PersonInput], rig: CameraRig, config: TriangulationConfig
) -> dict[int, dict[int, tuple[np.ndarray, float, float]]]:
    """1차: 유효 관측이 충분한 관절을 사람 전체에 걸쳐 한 번에 푼다(관절마다 쓴 카메라는 마스크).

    이상치·퇴화가 없는 관절만 (사람 → 관절 → (XYZ, 쓴 카메라 중 최대 재투영 오차, 시선각))으로 돌려준다. 나머지 관절은
    관절별 순차 경로(`_triangulate_joint`, 이상치 카메라를 하나씩 빼고 다시 푼다)로 넘긴다. 1차 풀이는 순차 경로의
    첫 반복과 같은 식이라 확정한 관절의 결과는 순차 경로와 같다.
    """
    cam_ids = sorted({cam.camera_id for inp in inputs for cam in inp.cameras})
    column = {cid: c for c, cid in enumerate(cam_ids)}
    C = len(cam_ids)
    # 사람마다 풀 관절의 행을 (관절, 카메라 열) 배열로 한 번에 만든다. 쓰지 않는 칸은 0 이고 mask 로 뺀다(S15P31A101-237)
    owners, joints, parts = [], [], []
    for n, inp in enumerate(inputs):
        js = np.flatnonzero(inp.usable.sum(axis=0) >= config.min_views)
        if not len(js):
            continue
        cols = [column[cam.camera_id] for cam in inp.cameras]
        ok = inp.usable[:, js].T  # (행, 그 사람 view)
        part = [np.zeros((len(js), C), dtype=bool), np.zeros((len(js), C, 2)), np.zeros((len(js), C, 2)), np.zeros((len(js), C))]
        part[0][:, cols] = ok
        part[1][:, cols] = np.where(ok[:, :, None], inp.normalized[:, js].transpose(1, 0, 2), 0.0)
        part[2][:, cols] = np.where(ok[:, :, None], inp.targets[:, js].transpose(1, 0, 2), 0.0)
        part[3][:, cols] = np.where(ok, inp.confidence[:, js].T, 0.0)
        owners += [n] * len(js)
        joints += js.tolist()
        parts.append(part)
    if not parts:
        return {}
    mask, xn, tg, w = (np.concatenate([p[k] for p in parts]) for k in range(4))
    M = len(mask)
    stack = _CameraStack.of(rig.subset(cam_ids))
    X = _solve_batch(stack, xn, tg, w, mask, config.refine_iterations)
    solved = np.isfinite(X).all(axis=1)
    worst = np.full(M, np.inf)
    angles = np.full(M, np.nan)
    if solved.any():
        worst[solved] = np.where(mask[solved], _errors_batch(stack, tg[solved], mask[solved], X[solved]), -np.inf).max(axis=1)
        angles[solved] = _max_ray_angles_batch(stack, mask[solved], X[solved])
    out: dict[int, dict[int, tuple[np.ndarray, float, float]]] = {}
    for r in np.flatnonzero(solved & (worst <= config.outlier_reprojection_error_px)).tolist():
        out.setdefault(owners[r], {})[joints[r]] = (X[r], float(worst[r]), float(angles[r]))
    return out


def undistort_view(
    camera: RigCamera, view: InterpolatedView, undistorted: Mapping[str, UndistortedKeypoints] | None = None
) -> UndistortedKeypoints:
    """view 의 keypoint 를 한 번에 왜곡 제거한다. undistorted 에 같은 카메라·같은 keypoint 결과가 있으면 그것을 쓴다."""
    if camera.camera_id != view.camera_id:
        raise ValueError(f"카메라 {camera.camera_id} 와 view 카메라 {view.camera_id} 가 다르다")
    cached = undistorted.get(view.camera_id) if undistorted is not None else None
    if cached is not None and cached.matches(camera, view.keypoints):
        return cached
    return camera.undistort_keypoints(view.keypoints)


def triangulate_point(
    cameras: Sequence[RigCamera], pixels: np.ndarray, weights: np.ndarray | None = None, refine_iterations: int = 10
) -> np.ndarray | None:
    """원본 픽셀 (N, 2) 관측으로 World 점 하나를 푼다. 해가 없거나 어느 카메라 뒤에 있으면 None.

    픽셀·가중치가 유한하지 않거나 왜곡을 제거할 수 없는 관측이 하나라도 있으면 None 이다(어느 관측을 뺄지는 호출자 몫).
    """
    if len(cameras) < MIN_VIEWS:
        raise ValueError(f"삼각측량에는 카메라가 {MIN_VIEWS}대 이상 필요하다: {len(cameras)}대")
    pixels = np.asarray(pixels, dtype=float).reshape(-1, 2)
    normalized = np.vstack([cam.normalize(px) for cam, px in zip(cameras, pixels)])
    return triangulate_normalized(cameras, normalized, weights, refine_iterations)


def triangulate_normalized(
    cameras: Sequence[RigCamera],
    normalized: np.ndarray,
    weights: np.ndarray | None = None,
    refine_iterations: int = 10,
    targets: np.ndarray | None = None,
) -> np.ndarray | None:
    """왜곡을 제거한 정규화 좌표 (N, 2) 관측으로 World 점 하나를 푼다 (`triangulate_point` 의 왜곡 제거 뒤 단계).

    정규화 좌표·가중치가 유한하지 않은 관측이 하나라도 있으면 None 이다. targets 는 그 관측의 이상적 핀홀 픽셀
    (N, 2, `UndistortedKeypoints.ideal_pixels`)이고 없으면 normalized 로 계산한다.
    """
    normalized = np.asarray(normalized, dtype=float).reshape(1, -1, 2)
    w = None if weights is None else np.asarray(weights, dtype=float).reshape(1, -1)
    t = None if targets is None else np.asarray(targets, dtype=float).reshape(1, -1, 2)
    X = triangulate_normalized_many(cameras, normalized, w, refine_iterations, t)[0]
    return X if np.isfinite(X).all() else None


def triangulate_normalized_many(
    cameras: Sequence[RigCamera],
    normalized: np.ndarray,
    weights: np.ndarray | None = None,
    refine_iterations: int = 10,
    targets: np.ndarray | None = None,
) -> np.ndarray:
    """같은 카메라 N 대로 본 점 M 개를 한 번에 푼다. normalized (M, N, 2), weights (M, N), targets (M, N, 2).

    점마다 `triangulate_normalized` 와 같고, 해가 없는 점의 행은 NaN 이다. 결과는 (M, 3).
    """
    if len(cameras) < MIN_VIEWS:
        raise ValueError(f"삼각측량에는 카메라가 {MIN_VIEWS}대 이상 필요하다: {len(cameras)}대")
    normalized = np.asarray(normalized, dtype=float).reshape(-1, len(cameras), 2)
    M = len(normalized)
    w = np.ones((M, len(cameras))) if weights is None else np.asarray(weights, dtype=float).reshape(M, -1)
    if targets is None:
        targets = np.stack([cam.ideal_pixels(normalized[:, c]) for c, cam in enumerate(cameras)], axis=1)
    targets = np.asarray(targets, dtype=float).reshape(M, -1, 2)
    mask = np.ones((M, len(cameras)), dtype=bool)
    return _solve_batch(_CameraStack.of(cameras), normalized, targets, w, mask, refine_iterations)


def solvable_normalized_many(cameras: Sequence[RigCamera], normalized: np.ndarray) -> np.ndarray:
    """(M,) `triangulate_normalized_many(cameras, normalized)` 의 행마다 해가 있는지(결과가 유한한지). 가중치는 1 이다.

    가우스-뉴턴은 쓴 카메라 모두의 앞이고 비용을 줄이는(유한한) 걸음만 받아들이므로, 해 유무는 정밀화 전의 선형 DLT 해와
    카메라 앞 확인으로 정해진다. 그래서 정밀화를 하지 않고 같은 판정을 낸다(S15P31A101-237, 경쟁 후보 검사용).
    """
    if len(cameras) < MIN_VIEWS:
        raise ValueError(f"삼각측량에는 카메라가 {MIN_VIEWS}대 이상 필요하다: {len(cameras)}대")
    normalized = np.asarray(normalized, dtype=float).reshape(-1, len(cameras), 2)
    stack = _CameraStack.of(cameras)
    mask = np.ones(normalized.shape[:2], dtype=bool)
    X, ok = _dlt_batch(stack, normalized, np.ones(normalized.shape[:2]))
    ok &= np.isfinite(normalized).all(axis=(1, 2))
    ok[ok] = _in_front_batch(stack, X[ok], mask[ok])
    return ok


def reprojection_errors(cameras: Sequence[RigCamera], pixels: np.ndarray, X: np.ndarray) -> np.ndarray:
    """카메라별 재투영 오차(왜곡 제거 픽셀). 카메라 뒤이거나 그 관측의 왜곡을 제거할 수 없으면 inf."""
    pixels = np.asarray(pixels, dtype=float).reshape(-1, 2)
    targets = np.vstack([cam.ideal_pixels(cam.normalize(px)) for cam, px in zip(cameras, pixels)])
    return reprojection_errors_ideal(cameras, targets, X)


def reprojection_errors_ideal(cameras: Sequence[RigCamera], targets: np.ndarray, X: np.ndarray) -> np.ndarray:
    """관측의 이상적 핀홀 픽셀 (N, 2) 기준 카메라별 재투영 오차. 카메라 뒤이거나 기준점이 유한하지 않으면 inf."""
    targets = np.asarray(targets, dtype=float).reshape(1, -1, 2)
    mask = np.ones((1, len(cameras)), dtype=bool)
    X = np.asarray(X, dtype=float).reshape(1, 3)
    return _errors_batch(_CameraStack.of(cameras), targets, mask, X)[0]


def max_ray_angle(cameras: Sequence[RigCamera], X: np.ndarray) -> float:
    """카메라 쌍마다 카메라 중심과 X 를 잇는 시선 직선 사이 각(0~π/2)의 최댓값(라디안).

    시선은 방향이 없는 직선으로 본다. 서로 마주 보는 두 카메라의 반대 방향 시선(180° 근처)도 같은 방향(0° 근처)처럼
    거의 같은 직선이라 깊이가 불안정하다.
    """
    mask = np.ones((1, len(cameras)), dtype=bool)
    X = np.asarray(X, dtype=float).reshape(1, 3)
    return float(_max_ray_angles_batch(_CameraStack.of(cameras), mask, X)[0])


@dataclass(frozen=True)
class _Observations:
    """관절 하나의 유효 관측 (카메라 순서). 왜곡 제거는 view 마다 한 번 해 둔 값이다."""

    cameras: list[RigCamera]
    normalized: np.ndarray
    """(N, 2) 정규화 좌표"""
    targets: np.ndarray
    """(N, 2) 이상적 핀홀 픽셀 (재투영 오차 기준)"""
    weights: np.ndarray
    """(N,) keypoint 신뢰도"""

    def __len__(self) -> int:
        return len(self.cameras)

    def without(self, i: int) -> _Observations:
        keep = np.arange(len(self.cameras)) != i
        return _Observations(
            self.cameras[:i] + self.cameras[i + 1 :], self.normalized[keep], self.targets[keep], self.weights[keep]
        )

    def solve(self, refine_iterations: int) -> np.ndarray | None:
        return triangulate_normalized(self.cameras, self.normalized, self.weights, refine_iterations, self.targets)

    def errors(self, X: np.ndarray) -> np.ndarray:
        return reprojection_errors_ideal(self.cameras, self.targets, X)


def _triangulate_joint(obs: _Observations, trusted_calibration: bool, config: TriangulationConfig) -> JointEstimate:
    """관절 하나의 순차 경로: 재투영 오차가 이상치 기준을 넘는 카메라를 하나씩 빼며 다시 푼다."""
    if len(obs) < config.min_views:
        return JointEstimate(None, JointValidity.UNAVAILABLE, issues=(JointIssue.TOO_FEW_VIEWS,))
    rejected: list[str] = []
    while True:
        cameras = obs.cameras
        X = obs.solve(config.refine_iterations)
        errors = obs.errors(X) if X is not None else None
        if errors is not None and float(errors.max()) > config.outlier_reprojection_error_px:
            if len(obs) == config.min_views:
                # 더 빼면 최소 관측 수 미만이다. 이상치 관측을 세어 좌표를 남기지 않는다
                return JointEstimate(
                    None,
                    JointValidity.UNAVAILABLE,
                    rejected_camera_ids=(*rejected, *(cam.camera_id for cam in cameras)),
                    issues=(JointIssue.INCONSISTENT_VIEWS,),
                )
            worst = int(np.argmax(errors))
        elif X is None and len(obs) > config.min_views:
            worst = _worst_view(obs, config)  # 해가 없거나 어느 카메라 뒤에 있다
        else:
            break
        rejected.append(cameras[worst].camera_id)
        obs = obs.without(worst)
    if X is None or errors is None:
        return JointEstimate(
            None,
            JointValidity.UNAVAILABLE,
            rejected_camera_ids=tuple(rejected),
            issues=(JointIssue.DEGENERATE,),
        )
    reproj = float(errors.max())
    return _estimate(cameras, X, reproj, max_ray_angle(cameras, X), tuple(rejected), trusted_calibration, config)


def _estimate(
    cameras: Sequence[RigCamera],
    X: np.ndarray,
    reproj: float,
    angle: float,
    rejected: tuple[str, ...],
    trusted_calibration: bool,
    config: TriangulationConfig,
) -> JointEstimate:
    """좌표를 얻은 관절의 최소 기준 판정 (쓴 카메라 중 최대 재투영 오차, 시선각, 보정 상태)."""
    issues = []
    if reproj > config.max_reprojection_error_px:
        issues.append(JointIssue.HIGH_REPROJECTION_ERROR)
    if angle < config.min_ray_angle_rad:
        issues.append(JointIssue.NARROW_RAY_ANGLE)
    if not trusted_calibration:
        issues.append(JointIssue.UNTRUSTED_CALIBRATION)
    return JointEstimate(
        xyz=(float(X[0]), float(X[1]), float(X[2])),
        validity=JointValidity.UNCERTAIN if issues else JointValidity.VALID,
        camera_ids=tuple(cam.camera_id for cam in cameras),
        rejected_camera_ids=rejected,
        reprojection_error_px=reproj,
        ray_angle_rad=angle,
        issues=tuple(issues),
    )


def _worst_view(obs: _Observations, config: TriangulationConfig) -> int:
    """해가 없을 때 뺄 카메라: 하나씩 빼고 풀어 남은 카메라 재투영 오차가 가장 작은 경우의 빠진 카메라."""
    best_i, best_err = 0, math.inf
    for i in range(len(obs)):
        rest = obs.without(i)
        X = rest.solve(config.refine_iterations)
        if X is None:
            continue
        err = float(rest.errors(X).max())
        if err < best_err:
            best_i, best_err = i, err
    return best_i


# ---------------------------------------------------------------- 묶음 계산
# 점 M 개를 카메라 C 대로 한 번에 푼다. 점마다 쓴 카메라는 mask (M, C) 로 고르고, 쓰지 않는 칸은 가중치 0 이라
# 식에 0 만 더한다. 점마다의 판단(해 유무, 카메라 앞, 감쇠 계수, 멈춤 조건)은 점 하나씩 풀 때와 같다.


@dataclass(frozen=True)
class _CameraStack:
    """카메라 C 대의 행렬을 쌓은 것."""

    R: np.ndarray
    """(C, 3, 3)"""
    t: np.ndarray
    """(C, 3)"""
    P: np.ndarray
    """(C, 3, 4) = [R | t]"""
    K: np.ndarray
    """(C, 3, 3)"""
    KP: np.ndarray
    """(C, 3, 4) = K · [R | t]. World 점 → 이상적 핀홀 픽셀 동차 좌표"""
    center: np.ndarray
    """(C, 3) 카메라 중심 (World)"""

    @classmethod
    def of(cls, cameras: Sequence[RigCamera]) -> _CameraStack:
        R = np.stack([cam.R for cam in cameras])
        t = np.stack([cam.t for cam in cameras])
        P = np.concatenate([R, t[:, :, None]], axis=2)
        K = np.stack([cam.K for cam in cameras])
        return cls(R=R, t=t, P=P, K=K, KP=K @ P, center=np.stack([cam.center for cam in cameras]))

    def project(self, X: np.ndarray) -> np.ndarray:
        """World 점 (M, 3) → 카메라마다 이상적 핀홀 픽셀 동차 좌표 (M, C, 3). 셋째 성분이 카메라 좌표 z 다."""
        return np.einsum("cij,mj->mci", self.KP[:, :, :3], X) + self.KP[:, :, 3]


def _solve_batch(
    stack: _CameraStack,
    normalized: np.ndarray,
    targets: np.ndarray,
    weights: np.ndarray,
    mask: np.ndarray,
    iterations: int,
) -> np.ndarray:
    """점 M 개의 삼각측량. normalized·targets (M, C, 2), weights·mask (M, C). 해가 없는 점의 행은 NaN.

    선형 DLT → 카메라 앞 확인 → 가우스-뉴턴(레벤버그-마콰트 감쇠) → 카메라 앞 확인. 쓴 관측의 정규화 좌표·가중치에
    유한하지 않은 값이 있으면 해가 없다.
    """
    M = mask.shape[0]
    out = np.full((M, 3), np.nan)
    with np.errstate(invalid="ignore"):
        finite = np.where(mask, np.isfinite(normalized).all(axis=2) & np.isfinite(weights), True).all(axis=1)
    xn = np.where(mask[:, :, None], normalized, 0.0)
    w = np.where(mask, weights, 0.0)
    tg = np.where(mask[:, :, None], targets, 0.0)
    X, ok = _dlt_batch(stack, xn, w)
    ok &= finite
    ok[ok] = _in_front_batch(stack, X[ok], mask[ok])
    if not ok.any():
        return out
    idx = np.flatnonzero(ok)
    X = _refine_batch(stack, tg[idx], w[idx], mask[idx], X[idx], iterations)
    front = _in_front_batch(stack, X, mask[idx])
    out[idx[front]] = X[front]
    return out


def _dlt_batch(stack: _CameraStack, xn: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """정규화 좌표의 선형 삼각측량. 카메라마다 두 행 x·P₃ − P₁, y·P₃ − P₂ (P = [R | t], 신뢰도 가중).

    두 번째로 작은 특이값도 0 에 가까우면 해 공간이 2차원 이상(시선 공선)이라 해가 유일하지 않다. 돌려주는 값은
    (점 (M, 3), 해가 있는지 (M,)). 쓰지 않는 카메라는 가중치 0 인 행(0 행)이라 특이값을 바꾸지 않는다.
    """
    P = stack.P[None]
    rows_x = w[:, :, None] * (xn[:, :, 0:1] * P[:, :, 2] - P[:, :, 0])
    rows_y = w[:, :, None] * (xn[:, :, 1:2] * P[:, :, 2] - P[:, :, 1])
    A = np.stack([rows_x, rows_y], axis=2).reshape(len(xn), -1, 4)
    try:
        _, s, vt = np.linalg.svd(A)
    except np.linalg.LinAlgError:
        return _dlt_one_by_one(A)
    return _dlt_solution(s, vt)


def _dlt_one_by_one(A: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """묶음 SVD 가 수렴하지 않으면 점마다 풀고, 수렴하지 않는 점만 해가 없다고 본다."""
    X = np.full((len(A), 3), np.nan)
    ok = np.zeros(len(A), dtype=bool)
    for m in range(len(A)):
        try:
            _, s, vt = np.linalg.svd(A[m])
        except np.linalg.LinAlgError:
            continue
        Xm, okm = _dlt_solution(s[None], vt[None])
        X[m], ok[m] = Xm[0], okm[0]
    return X, ok


def _dlt_solution(s: np.ndarray, vt: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    with np.errstate(invalid="ignore", divide="ignore"):
        ok = (s[:, 0] > 0) & ~(s[:, -2] <= DLT_RANK_TOLERANCE * s[:, 0])
        h = vt[:, -1]
        ok &= ~(np.abs(h[:, 3]) < 1e-12) & np.isfinite(h).all(axis=1)
        X = np.full((len(h), 3), np.nan)
        X[ok] = h[ok, :3] / h[ok, 3:4]
    return X, ok


def _in_front_batch(stack: _CameraStack, X: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """(M,) 점이 쓴 카메라 모두의 앞(카메라 좌표 z > 0)에 있는지."""
    z = stack.project(X)[..., 2]
    return ((z > 0) | ~mask).all(axis=1)


def _residuals_batch(
    stack: _CameraStack, targets: np.ndarray, sw: np.ndarray, mask: np.ndarray, X: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """가중 픽셀 재투영 잔차 r (M, 2C), 야코비안 J (M, 2C, 3), 쓴 카메라 모두의 앞인지 (M,).

    이상적 핀홀 픽셀 (u, v) = (U/Z, V/Z), [U V Z] = K·[R | t]·[X 1]. ∂u/∂X = (KP₀ − u·KP₂)/Z, ∂v/∂X = (KP₁ − v·KP₂)/Z.
    """
    M = len(X)
    h = stack.project(X)
    z = h[..., 2]
    front = ((z > 0) | ~mask).all(axis=1)
    z = np.where(mask & (z != 0), z, 1.0)  # 쓰지 않는 카메라·z = 0 은 나눗셈만 막는다 (앞이 아니라 받아들이지 않는다)
    uv = h[..., :2] / z[..., None]
    r = (sw[..., None] * (uv - targets)).reshape(M, -1)
    KP = stack.KP[None, :, :, :3]
    J = (KP[:, :, :2] - uv[..., None] * KP[:, :, 2:3]) * (sw / z)[..., None, None]
    return r, J.reshape(M, -1, 3), front


def _refine_batch(
    stack: _CameraStack, targets: np.ndarray, w: np.ndarray, mask: np.ndarray, X: np.ndarray, iterations: int
) -> np.ndarray:
    """가중 픽셀 재투영 오차 제곱합을 가우스-뉴턴(레벤버그-마콰트 감쇠)으로 줄인다. 점마다 감쇠·멈춤을 따로 한다.

    점마다: 걸음이 비용을 줄이고 모든 카메라 앞이면 받아들이고 감쇠를 1/10(하한 1e-9), 걸음 크기 < 1e-9 이면 멈춘다.
    아니면 감쇠 ×10, 1e6 을 넘으면 멈춘다. 정규 방정식이 특이하면 그 점은 멈춘다(현재 값 유지).
    """
    sw = np.sqrt(w)
    r, J, front = _residuals_batch(stack, targets, sw, mask, X)
    X = X.copy()
    active = front.copy()  # 시작점이 어느 카메라 뒤면 다듬지 않는다
    damping = np.full(len(X), 1e-3)
    eye = np.eye(3)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        for _ in range(iterations):
            # 멈추지 않은 점만 계산한다. 점마다 식이 따로라 멈춘 점을 함께 계산할 때와 결과가 같다(S15P31A101-237)
            idx = np.flatnonzero(active)
            if not len(idx):
                break
            r_a, J_a, d_a = r[idx], J[idx], damping[idx]
            cost = np.einsum("mr,mr->m", r_a, r_a)
            H = np.einsum("mri,mrj->mij", J_a, J_a)
            g = np.einsum("mri,mr->mi", J_a, r_a)
            step, solvable = _solve_3x3(H + d_a[:, None, None] * (H * eye), -g)
            cand = X[idx] + step
            tr, tJ, tfront = _residuals_batch(stack, targets[idx], sw[idx], mask[idx], cand)
            accept = solvable & tfront & (np.einsum("mr,mr->m", tr, tr) < cost)
            reject = solvable & ~accept
            took = idx[accept]
            X[took], r[took], J[took] = cand[accept], tr[accept], tJ[accept]
            d_a = np.where(accept, np.maximum(d_a / 10, 1e-9), np.where(reject, d_a * 10, d_a))
            damping[idx] = d_a
            converged = accept & (np.linalg.norm(step, axis=1) < 1e-9)
            active[idx] = solvable & ~converged & ~(reject & (d_a > 1e6))
    return X


def _solve_3x3(A: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(M, 3, 3)·(M, 3) 연립방정식. 특이한 행렬의 점은 (0, False)."""
    try:
        return np.linalg.solve(A, b[:, :, None])[:, :, 0], np.ones(len(A), dtype=bool)
    except np.linalg.LinAlgError:
        out = np.zeros_like(b)
        ok = np.zeros(len(A), dtype=bool)
        for m in range(len(A)):
            try:
                out[m] = np.linalg.solve(A[m], b[m])
                ok[m] = True
            except np.linalg.LinAlgError:
                pass
        return out, ok


def _errors_batch(stack: _CameraStack, targets: np.ndarray, mask: np.ndarray, X: np.ndarray) -> np.ndarray:
    """(M, C) 이상적 핀홀 픽셀 재투영 오차. 카메라 뒤이거나 유한하지 않으면 inf, 쓰지 않는 카메라는 NaN."""
    h = stack.project(X)
    z = h[..., 2]
    front = z > 0
    with np.errstate(invalid="ignore", divide="ignore"):
        d = h[..., :2] / np.where(front, z, 1.0)[..., None] - targets
        err = np.sqrt(d[..., 0] * d[..., 0] + d[..., 1] * d[..., 1])
    err = np.where(front & np.isfinite(err), err, np.inf)
    return np.where(mask, err, np.nan)


def _max_ray_angles_batch(stack: _CameraStack, mask: np.ndarray, X: np.ndarray) -> np.ndarray:
    """(M,) 쓴 카메라 쌍마다 시선 직선 사이 각(0~π/2)의 최댓값. 쌍이 없으면 0."""
    d = X[:, None, :] - stack.center[None]
    d = d / np.linalg.norm(d, axis=2, keepdims=True)
    cos = np.abs(np.einsum("mai,mbi->mab", d, d))
    angle = np.arccos(np.clip(cos, 0.0, 1.0))
    C = mask.shape[1]
    pair = mask[:, :, None] & mask[:, None, :] & ~np.eye(C, dtype=bool)
    return np.where(pair, angle, 0.0).max(axis=(1, 2))
