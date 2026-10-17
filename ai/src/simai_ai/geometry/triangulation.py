"""다시점 삼각측량 (명세 5.2 삼각측량·계산 순서 3단계·World 위치의 정의).

공통 시각으로 보간한 카메라별 2D keypoint(`interpolation.InterpolatedView`)와 보정(`rig.CameraRig`)으로
사람 한 명의 관절 3D 를 만든다. 카메라 간 연결(같은 사람 묶기)은 호출자가 이미 했다고 본다.

- 유효 관측(신뢰도 기준 이상)이 2대 이상인 관절만 삼각측량한다. 1대 이하는 null, visible=false, UNAVAILABLE 이다.
  값(u, v, 신뢰도)이 유한하지 않거나 렌즈 왜곡을 제거할 수 없는(`RigCamera.normalize_with_mask`) keypoint 는
  관측으로 세지 않는다. 예외로 멈추지 않고 관측 수가 줄어든 대로 판정한다.
- 선형 DLT(왜곡 제거 정규화 좌표)로 초기값을 구하고 가우스-뉴턴으로 픽셀 재투영 오차를 줄인다.
- 재투영 오차가 이상치 기준을 넘는 카메라는 빼고 다시 푼다. 빼고 나서 기준 안의 관측이 2대 미만이면 null 이다
  (2대를 채우려고 이상치 관측을 세지 않는다).
- 시선이 같은 직선이면(공선) 해가 유일하지 않아 null 이다. 시선각은 방향이 아니라 직선 사이 각(0~90°)으로 본다.
- 관절 상태는 최소 기준만 본다: 관측 카메라 수, 재투영 오차, 시선각, 보정 상태. 시간 일관성(속도·뼈 길이)과
  삼각측량 불확실성을 더한 관절 유효성 게이트는 S15P31A101-29 가 이 인터페이스 위에 만든다.
- 이 모듈의 관절은 모두 Tier 0 관측(joint_source = observed)이다. 접지점(ground_anchor_xyz)은 ContactEstimator 가
  정한다. 여기서는 만들지 않는다(Z=0 대체 금지).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np

from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.rig import CameraRig, RigCamera

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


@dataclass(frozen=True)
class SkeletonEstimate:
    """사람 한 명의 공통 시각 t 관절 3D. World 미터, 오른손, +Z 상방."""

    t: int
    joint_layout: str
    joints: tuple[JointEstimate, ...]
    calibration_version: str
    camera_ids: tuple[str, ...] = ()
    """하나 이상의 관절 삼각측량에 쓴 카메라 (world_track.cameras)"""

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
) -> SkeletonEstimate:
    """같은 사람의 카메라별 보간 keypoint 로 관절 3D 를 만든다. 모든 view 는 같은 공통 시각이어야 한다."""
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
    trusted = rig.state not in UNTRUSTED_CALIBRATION_STATES

    usable = [_usable_keypoints(cam, v.keypoints, config) for cam, v in zip(cameras, view_list)]
    joints = []
    for j in range(n_joints):
        obs = [(cam, v.keypoints[j]) for cam, v, ok in zip(cameras, view_list, usable) if ok[j]]
        joints.append(_triangulate_joint(obs, trusted, config))
    used = sorted({cid for j in joints for cid in j.camera_ids})
    return SkeletonEstimate(
        t=view_list[0].t,
        joint_layout=joint_layout,
        joints=tuple(joints),
        calibration_version=rig.calibration_version,
        camera_ids=tuple(used),
    )


def triangulate_point(
    cameras: Sequence[RigCamera], pixels: np.ndarray, weights: np.ndarray | None = None, refine_iterations: int = 10
) -> np.ndarray | None:
    """원본 픽셀 (N, 2) 관측으로 World 점 하나를 푼다. 해가 없거나 어느 카메라 뒤에 있으면 None.

    픽셀·가중치가 유한하지 않거나 왜곡을 제거할 수 없는 관측이 하나라도 있으면 None 이다(어느 관측을 뺄지는 호출자 몫).
    """
    if len(cameras) < MIN_VIEWS:
        raise ValueError(f"삼각측량에는 카메라가 {MIN_VIEWS}대 이상 필요하다: {len(cameras)}대")
    pixels = np.asarray(pixels, dtype=float).reshape(-1, 2)
    w = np.ones(len(cameras)) if weights is None else np.asarray(weights, dtype=float)
    normalized = np.vstack([cam.normalize(px) for cam, px in zip(cameras, pixels)])
    if not (np.all(np.isfinite(normalized)) and np.all(np.isfinite(w))):
        return None
    X = _dlt(cameras, normalized, w)
    if X is None or not _in_front(cameras, X):
        return None
    targets = np.vstack([cam.ideal_pixels(n) for cam, n in zip(cameras, normalized)])
    X = _refine(cameras, targets, w, X, refine_iterations)
    if not _in_front(cameras, X):
        return None
    return X


def reprojection_errors(cameras: Sequence[RigCamera], pixels: np.ndarray, X: np.ndarray) -> np.ndarray:
    """카메라별 재투영 오차(왜곡 제거 픽셀). 카메라 뒤이거나 그 관측의 왜곡을 제거할 수 없으면 inf."""
    pixels = np.asarray(pixels, dtype=float).reshape(-1, 2)
    out = np.empty(len(cameras))
    for i, (cam, px) in enumerate(zip(cameras, pixels)):
        xc = cam.to_camera(X)[0]
        if xc[2] <= 0:
            out[i] = math.inf
            continue
        target = cam.ideal_pixels(cam.normalize(px))[0]
        proj = cam.ideal_pixels(xc[:2] / xc[2])[0]
        err = float(np.linalg.norm(proj - target))
        out[i] = err if math.isfinite(err) else math.inf
    return out


def max_ray_angle(cameras: Sequence[RigCamera], X: np.ndarray) -> float:
    """카메라 쌍마다 카메라 중심과 X 를 잇는 시선 직선 사이 각(0~π/2)의 최댓값(라디안).

    시선은 방향이 없는 직선으로 본다. 서로 마주 보는 두 카메라의 반대 방향 시선(180° 근처)도 같은 방향(0° 근처)처럼
    거의 같은 직선이라 깊이가 불안정하다.
    """
    dirs = [(X - cam.center) / np.linalg.norm(X - cam.center) for cam in cameras]
    best = 0.0
    for a, b in itertools.combinations(dirs, 2):
        best = max(best, math.acos(float(np.clip(abs(a @ b), 0.0, 1.0))))
    return best


def _usable_keypoints(cam: RigCamera, keypoints: Sequence[Any], config: TriangulationConfig) -> list[bool]:
    """관절마다 관측으로 셀 수 있는지: 값이 있고 (u, v, 신뢰도) 모두 유한하고 신뢰도 기준 이상이며 왜곡을 제거할 수 있다."""
    candidate = []
    pixels = np.zeros((len(keypoints), 2))
    for j, kp in enumerate(keypoints):
        ok = kp is not None and all(math.isfinite(float(x)) for x in kp[:3]) and kp[2] >= config.min_keypoint_confidence
        candidate.append(ok)
        if ok:
            pixels[j] = kp[:2]
    if not any(candidate):
        return candidate
    _, undistorted = cam.normalize_with_mask(pixels)
    return [bool(c and u) for c, u in zip(candidate, undistorted)]


def _triangulate_joint(
    obs: list[tuple[RigCamera, tuple[float, float, float]]], trusted_calibration: bool, config: TriangulationConfig
) -> JointEstimate:
    if len(obs) < config.min_views:
        return JointEstimate(None, JointValidity.UNAVAILABLE, issues=(JointIssue.TOO_FEW_VIEWS,))
    rejected: list[str] = []
    while True:
        cameras = [cam for cam, _ in obs]
        pixels = np.array([kp[:2] for _, kp in obs])
        weights = np.array([kp[2] for _, kp in obs])
        X = triangulate_point(cameras, pixels, weights, config.refine_iterations)
        errors = reprojection_errors(cameras, pixels, X) if X is not None else None
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
        rejected.append(obs[worst][0].camera_id)
        obs = obs[:worst] + obs[worst + 1 :]
    if X is None or errors is None:
        return JointEstimate(
            None,
            JointValidity.UNAVAILABLE,
            rejected_camera_ids=tuple(rejected),
            issues=(JointIssue.DEGENERATE,),
        )
    reproj = float(errors.max())
    angle = max_ray_angle(cameras, X)
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
        rejected_camera_ids=tuple(rejected),
        reprojection_error_px=reproj,
        ray_angle_rad=angle,
        issues=tuple(issues),
    )


def _worst_view(obs: list[tuple[RigCamera, tuple[float, float, float]]], config: TriangulationConfig) -> int:
    """해가 없을 때 뺄 카메라: 하나씩 빼고 풀어 남은 카메라 재투영 오차가 가장 작은 경우의 빠진 카메라."""
    best_i, best_err = 0, math.inf
    for i in range(len(obs)):
        rest = obs[:i] + obs[i + 1 :]
        cams = [cam for cam, _ in rest]
        px = np.array([kp[:2] for _, kp in rest])
        X = triangulate_point(cams, px, np.array([kp[2] for _, kp in rest]), config.refine_iterations)
        if X is None:
            continue
        err = float(reprojection_errors(cams, px, X).max())
        if err < best_err:
            best_i, best_err = i, err
    return best_i


def _dlt(cameras: Sequence[RigCamera], normalized: np.ndarray, weights: np.ndarray) -> np.ndarray | None:
    """정규화 좌표의 선형 삼각측량. 행마다 x·P₃ − P₁, y·P₃ − P₂ (P = [R | t]).

    두 번째로 작은 특이값도 0 에 가까우면 해 공간이 2차원 이상(시선 공선)이라 해가 유일하지 않다: None.
    """
    rows = []
    for cam, (x, y), w in zip(cameras, normalized, weights):
        P = np.hstack([cam.R, cam.t[:, None]])
        rows.append(w * (x * P[2] - P[0]))
        rows.append(w * (y * P[2] - P[1]))
    try:
        _, s, vt = np.linalg.svd(np.array(rows))
    except np.linalg.LinAlgError:
        return None
    if not s[0] > 0 or s[-2] <= DLT_RANK_TOLERANCE * s[0]:
        return None
    h = vt[-1]
    if abs(h[3]) < 1e-12 or not np.all(np.isfinite(h)):
        return None
    return h[:3] / h[3]


def _refine(
    cameras: Sequence[RigCamera], targets: np.ndarray, weights: np.ndarray, X: np.ndarray, iterations: int
) -> np.ndarray:
    """가중 픽셀 재투영 오차 제곱합을 가우스-뉴턴(레벤버그-마콰트 감쇠)으로 줄인다."""
    sw = np.sqrt(weights)

    def residuals(P: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
        r = np.empty(2 * len(cameras))
        J = np.empty((2 * len(cameras), 3))
        for i, cam in enumerate(cameras):
            xc, yc, zc = cam.R @ P + cam.t
            if zc <= 0:
                return None
            fx, s, cx = cam.K[0]
            fy, cy = cam.K[1][1], cam.K[1][2]
            u = fx * xc / zc + s * yc / zc + cx
            v = fy * yc / zc + cy
            du = np.array([fx / zc, s / zc, -(fx * xc + s * yc) / zc**2])
            dv = np.array([0.0, fy / zc, -fy * yc / zc**2])
            r[2 * i : 2 * i + 2] = sw[i] * (np.array([u, v]) - targets[i])
            J[2 * i] = sw[i] * (du @ cam.R)
            J[2 * i + 1] = sw[i] * (dv @ cam.R)
        return r, J

    current = residuals(X)
    if current is None:
        return X
    damping = 1e-3
    for _ in range(iterations):
        r, J = current
        cost = float(r @ r)
        H = J.T @ J
        g = J.T @ r
        try:
            step = np.linalg.solve(H + damping * np.diag(np.diag(H)), -g)
        except np.linalg.LinAlgError:
            break
        candidate = X + step
        trial = residuals(candidate)
        if trial is not None and float(trial[0] @ trial[0]) < cost:
            X, current = candidate, trial
            damping = max(damping / 10, 1e-9)
            if float(np.linalg.norm(step)) < 1e-9:
                break
        else:
            damping *= 10
            if damping > 1e6:
                break
    return X


def _in_front(cameras: Sequence[RigCamera], X: np.ndarray) -> bool:
    return all(float((cam.R @ X + cam.t)[2]) > 0 for cam in cameras)
