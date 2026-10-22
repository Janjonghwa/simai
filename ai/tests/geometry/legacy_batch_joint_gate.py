# S15P31A101-237 이전(dev abe9529) `ai/src/simai_ai/geometry/joint_gate.py` 의 고정 사본. 회귀 비교(test_geometry_speedup_regression.py) 전용이며 고치지 않는다.
# 원본과 다른 곳은 import 경로뿐이다(같은 묶음의 legacy_batch_* 사본을 쓴다. rig 는 이번에 바꾸지 않아 지금 모듈을 쓴다).
# ruff: noqa
"""3D 관절 유효성 게이트 (명세 5.2 3D 관절 유효성 게이트·World 위치의 정의, 3.9 촬영 시각 계약, 7.1 JOINT_UNCERTAIN).

두 대에 보인다는 것과 그 관절의 Z 가 안전 판단에 충분히 정확하다는 것은 다르다. 사람 한 명(World Track 하나)의
공통 시각 관절 3D 를 `triangulation.triangulate_skeleton` 으로 만든 뒤, 관절마다 아래 근거로 VALID / UNCERTAIN /
UNAVAILABLE 을 정하고 사유 코드를 남긴다.

- 관측 카메라 수(시간 보간 후 유효한 카메라만): 2대 미만은 좌표를 비우고 UNAVAILABLE(지어내지 않는다).
- 재투영 잔차, 두 시선 사이 각도, 보정 상태: `triangulation` 의 최소 기준 사유를 그대로 잇는다.
- 삼각측량 불확실성: 쓴 카메라의 투영 야코비안과 가정한 keypoint 픽셀 잡음으로 3D 공분산을 구해 가장 큰 축의
  표준편차를 본다. 시선각이 커도 멀리 있거나 카메라 배치가 나쁘면 커진다.
- 시간 일관성: 직전 VALID 위치 대비 속도 상한, 사람별 뼈 길이 기준(최근 표본의 중앙값) 대비 변화.
- 2대 관측 경쟁 후보: 관측 카메라가 2대뿐이고 그 관절의 에피폴라선 위에 다른 사람의 같은 관절이 있으면 VALID 로
  두지 않는다. UNCERTAIN 으로 두고 그 연결을 IDENTITY_UNCERTAIN 후보로 표시한다(D-G2-06).

왜곡 제거는 view 마다 한 번 하고(`RigCamera.undistort_keypoints`) 삼각측량과 경쟁 후보 검사가 그 결과를 같이 쓴다.
호출자가 Cross-view 등에서 이미 푼 결과를 `undistorted` 로 넘기면 다시 풀지 않는다(S15P31A101-168).

이 모듈의 관절은 모두 Tier 0 관측(joint_source = observed)이다. HMR 등 Tier 1 결과를 받지 않는다. 게이트는 등급을
낮추기만 한다(삼각측량이 UNCERTAIN·UNAVAILABLE 로 둔 관절을 VALID 로 올리지 않는다). 현재 시각을 읽지 않고
skeleton 의 공통 시각 t 만 쓴다. 임계값은 `JointGateConfig` 에 있고 모두 초기값이다(G2·A0 에서 조정).
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from simai_ai.geometry.interpolation import InterpolatedView, Keypoint
from simai_ai.geometry.rig import CameraRig, RigCamera, UndistortedKeypoints
from legacy_batch_triangulation import (
    COCO17_L_HIP,
    COCO17_R_HIP,
    MIN_VIEWS,
    JointIssue,
    JointValidity,
    SkeletonEstimate,
    TriangulationConfig,
    triangulate_normalized_many,
    triangulate_skeleton,
)

# COCO17 관절 번호 (contracts common joint_layout 순서)
COCO17_L_EYE, COCO17_R_EYE, COCO17_L_EAR, COCO17_R_EAR = 1, 2, 3, 4
# head_z 근거 관절 (명세 5.2: 양쪽 귀·눈 중점)
COCO17_HEAD_JOINTS = (COCO17_L_EYE, COCO17_R_EYE, COCO17_L_EAR, COCO17_R_EAR)
# 뼈 길이 검사에 쓰는 COCO17 뼈. 얼굴 관절 사이는 길이가 짧아 keypoint 잡음과 구분되지 않으므로 뺀다.
COCO17_BONES: tuple[tuple[int, int], ...] = (
    (5, 7), (7, 9),      # 왼팔: 어깨-팔꿈치, 팔꿈치-손목
    (6, 8), (8, 10),     # 오른팔
    (11, 13), (13, 15),  # 왼다리: 엉덩이-무릎, 무릎-발목
    (12, 14), (14, 16),  # 오른다리
    (5, 6), (11, 12),    # 어깨 폭, 엉덩이 폭
    (5, 11), (6, 12),    # 몸통 옆선
)  # fmt: skip
_ORDER = (JointValidity.UNAVAILABLE, JointValidity.UNCERTAIN, JointValidity.VALID)


class GateIssue(StrEnum):
    """게이트가 더하는 VALID 가 아닌 사유. 삼각측량 사유(`JointIssue`)와 함께 붙는다."""

    FEW_VIEWS_FOR_VALID = "FEW_VIEWS_FOR_VALID"
    """좌표는 있지만 관측 카메라 수가 VALID 하한(`min_views_for_valid`)보다 적다"""
    HIGH_TRIANGULATION_UNCERTAINTY = "HIGH_TRIANGULATION_UNCERTAINTY"
    """삼각측량 3D 표준편차(가장 큰 축)가 상한을 넘는다"""
    SPEED_LIMIT = "SPEED_LIMIT"
    """직전 VALID 위치 대비 속도가 상한을 넘는다"""
    BONE_LENGTH_CHANGE = "BONE_LENGTH_CHANGE"
    """이 관절이 끝점인 뼈의 길이가 사람별 기준에서 허용 범위 넘게 바뀌었다"""
    TWO_VIEW_COMPETITOR = "TWO_VIEW_COMPETITOR"
    """관측 2대뿐이고 에피폴라선 위에 다른 사람의 같은 관절이 있다 (연결을 IDENTITY_UNCERTAIN 후보로 표시)"""
    COMPETITOR_CHECK_UNAVAILABLE = "COMPETITOR_CHECK_UNAVAILABLE"
    """관측 2대뿐인데 경쟁 후보를 확인할 수 없다: 다른 사람 검출을 모르거나(others=None) 그 관절 값이 유한하지 않다"""


GateReason = JointIssue | GateIssue


@dataclass(frozen=True)
class JointGateConfig:
    """관절 유효성 게이트 임계값. 명세에 수치가 없어 모두 초기값이며 G2·A0 실측으로 다시 정한다.

    재투영 잔차·시선각·신뢰도·이상치 기준은 `triangulation` 에 있다(`triangulation` 필드로 함께 넘긴다).
    근거와 SIM 측정은 docs/design/tier0-joint-gate.md.
    """

    triangulation: TriangulationConfig = field(default_factory=TriangulationConfig)
    min_views_for_valid: int = MIN_VIEWS
    """VALID 에 필요한 관측 카메라 수. 명세 5.2 는 2대 관측을 경쟁 후보가 있을 때만 막으므로 기본은 2다"""
    keypoint_sigma_px: float = 2.0
    """삼각측량 불확실성 계산에 쓰는 2D keypoint 픽셀 표준편차(가정). YOLO26x-pose 실측 오차로 다시 정한다"""
    max_position_sigma_m: float = 0.05
    """VALID 상한: 3D 공분산의 가장 큰 축 표준편차. G2 머리 Z p95 10 cm 의 절반"""
    max_joint_speed_mps: float = 6.0
    """VALID 상한: 직전 VALID 위치 대비 속도. 교실 동작(걷기·팔 동작·낙상 머리 속도 3~4 m/s)보다 넉넉히"""
    max_history_gap_ms: int = 500
    """직전 VALID 위치가 이보다 오래됐으면 속도 검사를 하지 않는다(가림 뒤 재관측, 시간 점프)"""
    bone_window: int = 30
    """뼈 길이 기준 = 최근 이 개수 표본의 중앙값 (10 Hz 에서 3초)"""
    bone_min_samples: int = 5
    """뼈 길이 표본이 이보다 적으면 뼈 길이 검사를 하지 않는다"""
    bone_abs_tolerance_m: float = 0.06
    bone_rel_tolerance: float = 0.25
    """뼈 길이 허용 변화 = max(절대 허용, 상대 허용 × 기준 길이)"""
    competitor_epipolar_px: float = 15.0
    """2대 관측 경쟁 후보: 다른 사람의 같은 관절이 에피폴라선에서 이 거리(왜곡 제거 픽셀) 안이면 후보로 본다.
    정답 기준 경쟁 후보 띠(10 px)에 관측 잡음 여유 5 px 를 더한 값이다. 10 px 로 두면 SIM noisy 에서 띠 경계의
    경쟁 후보 관절이 VALID 로 새어 나왔다(docs/design/tier0-joint-gate.md)"""
    head_top_offset_m: float = 0.12
    """head_z = 귀·눈 Z 평균 + 이 오프셋. 성인 귀·눈 높이에서 머리 꼭대기까지(약 12 cm) 초기값, G2 에서 조정"""

    def __post_init__(self) -> None:
        if self.min_views_for_valid < MIN_VIEWS:
            raise ValueError(f"min_views_for_valid 는 {MIN_VIEWS} 이상이어야 한다: {self.min_views_for_valid}")
        positive = {
            "keypoint_sigma_px": self.keypoint_sigma_px,
            "max_position_sigma_m": self.max_position_sigma_m,
            "max_joint_speed_mps": self.max_joint_speed_mps,
            "max_history_gap_ms": self.max_history_gap_ms,
            "bone_window": self.bone_window,
            "bone_min_samples": self.bone_min_samples,
            "competitor_epipolar_px": self.competitor_epipolar_px,
        }
        for name, value in positive.items():
            if not value > 0:
                raise ValueError(f"{name} 는 양수여야 한다: {value}")
        if self.bone_min_samples > self.bone_window:
            raise ValueError("bone_min_samples 는 bone_window 이하여야 한다")
        if self.bone_abs_tolerance_m < 0 or self.bone_rel_tolerance < 0:
            raise ValueError("뼈 길이 허용 범위는 음수일 수 없다")


@dataclass(frozen=True)
class GatedJoint:
    """관절 하나의 게이트 결과. xyz 가 None 이면 visible=false, validity=UNAVAILABLE 이다."""

    xyz: tuple[float, float, float] | None
    validity: JointValidity
    reasons: tuple[GateReason, ...] = ()
    """VALID 가 아닌 사유 (판정 근거). VALID 이면 비어 있다"""
    camera_ids: tuple[str, ...] = ()
    reprojection_error_px: float | None = None
    ray_angle_rad: float | None = None
    sigma_xyz_m: tuple[float, float, float] | None = None
    """삼각측량 공분산 대각의 표준편차 (World X, Y, Z)"""
    sigma_max_m: float | None = None
    """삼각측량 공분산의 가장 큰 축 표준편차"""
    speed_mps: float | None = None
    """직전 VALID 위치 대비 속도. 비교할 이력이 없으면 None"""
    joint_source: str = "observed"
    """Tier 0 삼각측량 관절은 늘 observed (명세 5.6)"""

    @property
    def visible(self) -> bool:
        return self.xyz is not None


@dataclass(frozen=True)
class HeadZ:
    """머리 꼭대기 높이 추정 (명세 5.2 head_z). value 가 None 이면 비운다."""

    value: float | None
    validity: JointValidity
    sigma_m: float | None = None
    """근거 관절 Z 표준편차로 만든 값. 오프셋 자체의 불확실성은 넣지 않는다"""
    joints: tuple[int, ...] = ()


@dataclass(frozen=True)
class GatedSkeleton:
    """사람 한 명의 공통 시각 t 게이트 결과. World 미터, 오른손, +Z 상방."""

    t: int
    joint_layout: str
    joints: tuple[GatedJoint, ...]
    calibration_version: str
    camera_ids: tuple[str, ...] = ()
    identity_uncertain_candidate: bool = False
    """2대 관측 경쟁 후보가 있는 관절이 하나라도 있으면 True. 카메라 간 연결이 IDENTITY_UNCERTAIN 후보다"""

    def to_skeleton(self) -> dict[str, Any]:
        """`world_state.schema.json#/$defs/skeleton` 문서."""
        return {
            "joint_layout": self.joint_layout,
            "joints": [list(j.xyz) if j.xyz is not None else None for j in self.joints],
            "visible": [j.visible for j in self.joints],
            "validity": [str(j.validity) for j in self.joints],
            "joint_source": [j.joint_source for j in self.joints],
        }

    def pelvis_xyz(self) -> tuple[float, float, float] | None:
        """골반 = 양쪽 엉덩이 중점 (명세 5.2 pelvis_xyz). 하나라도 없으면 None(비움)."""
        _require_coco17(self.joint_layout)
        left, right = self.joints[COCO17_L_HIP].xyz, self.joints[COCO17_R_HIP].xyz
        if left is None or right is None:
            return None
        return tuple((a + b) / 2.0 for a, b in zip(left, right))  # type: ignore[return-value]

    def pelvis_validity(self) -> JointValidity:
        """골반 상태: 두 엉덩이 상태 중 약한 쪽."""
        _require_coco17(self.joint_layout)
        return _weaker(self.joints[COCO17_L_HIP].validity, self.joints[COCO17_R_HIP].validity)

    def head_z(self, offset_m: float) -> HeadZ:
        """귀·눈 Z 평균 + 오프셋. VALID 귀·눈만 쓰고, VALID 가 없으면 UNCERTAIN 귀·눈으로 UNCERTAIN 값을 낸다.

        좌표가 있는 귀·눈이 없으면 비운다(UNAVAILABLE). UNCERTAIN 값은 낙상(R4) 판단에서 "확인 필요"가 된다(7.1).
        """
        _require_coco17(self.joint_layout)
        for level in (JointValidity.VALID, JointValidity.UNCERTAIN):
            used = [j for j in COCO17_HEAD_JOINTS if self.joints[j].validity == level]
            if used:
                zs = [self.joints[j].xyz[2] for j in used]  # type: ignore[index]
                sigmas = [self.joints[j].sigma_xyz_m for j in used]
                sigma = (
                    math.sqrt(sum(s[2] ** 2 for s in sigmas)) / len(used)  # type: ignore[index]
                    if all(s is not None for s in sigmas)
                    else None
                )
                return HeadZ(sum(zs) / len(zs) + offset_m, level, sigma, tuple(used))
        return HeadZ(None, JointValidity.UNAVAILABLE)


class JointGate:
    """World Track 하나의 관절 유효성 게이트. 시간 일관성 이력(직전 VALID 위치, 뼈 길이 표본)을 가진다.

    World ID 가 병합·분할·교정되면(`identity_revision` 증가) 이력이 다른 사람 것일 수 있으므로 `reset()` 한다.
    """

    def __init__(self, config: JointGateConfig | None = None) -> None:
        self.config = config or JointGateConfig()
        self._last_t: int | None = None
        self._last_valid: dict[int, tuple[int, np.ndarray]] = {}
        self._bones: dict[tuple[int, int], deque[float]] = {}

    def reset(self) -> None:
        self._last_t = None
        self._last_valid.clear()
        self._bones.clear()

    def bone_reference(self, bone: tuple[int, int]) -> float | None:
        """뼈 길이 기준(최근 표본의 중앙값). 표본이 `bone_min_samples` 보다 적으면 None."""
        samples = self._bones.get(bone)
        if not samples or len(samples) < self.config.bone_min_samples:
            return None
        return statistics.median(samples)

    def update(
        self,
        views: Mapping[str, InterpolatedView] | Sequence[InterpolatedView],
        rig: CameraRig,
        others: Mapping[str, Sequence[Sequence[Keypoint]]] | None,
        joint_layout: str = "COCO17",
        sync_invalid_cameras: Collection[str] = (),
        *,
        undistorted: Mapping[str, UndistortedKeypoints] | None = None,
        others_undistorted: Mapping[str, Sequence[UndistortedKeypoints]] | None = None,
        skeleton: SkeletonEstimate | None = None,
    ) -> GatedSkeleton:
        """같은 사람의 카메라별 보간 keypoint 를 삼각측량하고 게이트를 적용한다.

        others: 카메라별로 같은 공통 시각의 **다른 사람** 검출 keypoint (joint_layout 순서). 2대 관측 경쟁 후보 검사에
        쓴다. 이 사람에게 묶인 검출은 넣지 않는다. 필수 인자다: 다른 사람이 없으면 빈 컨테이너(`{}`)로 명시한다.
        None(다른 사람 검출을 모름)이면 경쟁 후보가 없다고 볼 수 없으므로 2대 관측 관절을 VALID 로 두지 않는다
        (`COMPETITOR_CHECK_UNAVAILABLE`). keypoint 목록 길이가 관절 배치와 다르면 ValueError.
        sync_invalid_cameras: 시간 동기화가 무효인 카메라(`SystemHealthReport.sync_invalid_cameras`). 그 카메라의
        관측은 삼각측량과 경쟁 후보 검사에서 뺀다(명세 3.9: 동기화가 무효이면 다시점 3D 를 안전용으로 확정하지 않는다).
        undistorted: 카메라 ID → 이 사람 view 를 이미 왜곡 제거한 결과(`RigCamera.undistort_keypoints`, 예: Cross-view
        클러스터의 `ViewCluster.undistorted`). 그 view 의 keypoint 로 만든 것만 쓰고, 없거나 맞지 않으면 여기서 푼다.
        others_undistorted: others 와 같은 카메라·같은 순서의 왜곡 제거 결과(다른 사람 클러스터의 `ViewCluster.undistorted`).
        경쟁 후보 검사에서 다른 사람 keypoint 를 다시 풀지 않는다. 그 검출의 keypoint 로 만든 것만 쓴다.
        skeleton: 이 view 들로 이미 만든 삼각측량 결과(예: Cross-view 클러스터의 `ViewCluster.skeleton`). 동기화 무효 카메라를
        뺀 뒤의 view·보정·관절 배치·`config.triangulation` 이 모두 같을 때만 쓰고(`SkeletonEstimate.reusable_for`),
        아니면 여기서 다시 삼각측량한다.
        """
        excluded = set(sync_invalid_cameras)
        view_list = [
            v for v in (views.values() if isinstance(views, Mapping) else views) if v.camera_id not in excluded
        ]
        if not view_list:
            raise ValueError("동기화가 유효한 카메라의 view 가 없다")
        if skeleton is not None and skeleton.reusable_for(view_list, rig, joint_layout, self.config.triangulation):
            assert skeleton.inputs is not None
            und = {u.camera_id: u for u in skeleton.inputs.undistorted}
        else:
            # view 마다 한 번 왜곡 제거해 삼각측량과 경쟁 후보 검사가 같이 쓴다. 보정에 없는 카메라는 삼각측량이 KeyError 로 막는다
            und = {}
            for v in view_list:
                camera = rig.cameras.get(v.camera_id)
                if camera is None:
                    continue
                cached = undistorted.get(v.camera_id) if undistorted is not None else None
                if cached is None or not cached.matches(camera, v.keypoints):
                    cached = camera.undistort_keypoints(v.keypoints)
                und[v.camera_id] = cached
            skeleton = triangulate_skeleton(view_list, rig, joint_layout, self.config.triangulation, undistorted=und)
        if others is not None:
            others = {cid: kps for cid, kps in others.items() if cid not in excluded}
            for cid, detections in others.items():
                for kps in detections:
                    if len(kps) != len(skeleton.joints):
                        raise ValueError(
                            f"others[{cid}]: {joint_layout} 는 keypoint {len(skeleton.joints)}개인데 {len(kps)}개다"
                        )
        if self._last_t is not None and skeleton.t <= self._last_t:
            raise ValueError(f"공통 시각이 앞으로 가지 않는다: {skeleton.t} <= {self._last_t} (이력이 섞이면 reset)")
        pixels = {v.camera_id: v.keypoints for v in view_list}  # 경쟁 후보 검사용 이 사람의 관측
        gated = self._gate(skeleton, rig, pixels, others, und, others_undistorted)
        self._last_t = skeleton.t
        return gated

    def _gate(
        self,
        skeleton: SkeletonEstimate,
        rig: CameraRig,
        pixels: Mapping[str, Sequence[Keypoint]],
        others: Mapping[str, Sequence[Sequence[Keypoint]]] | None,
        undistorted: Mapping[str, UndistortedKeypoints],
        others_undistorted: Mapping[str, Sequence[UndistortedKeypoints]] | None = None,
    ) -> GatedSkeleton:
        cfg = self.config
        t = skeleton.t
        n = len(skeleton.joints)
        reasons: list[list[GateReason]] = [list(j.issues) for j in skeleton.joints]
        speeds: list[float | None] = [None] * n
        # 삼각측량 불확실성: 좌표가 있는 관절을 한 번에 계산한다
        sigma_xyz, sigma_max = _position_sigmas(skeleton, rig, cfg.keypoint_sigma_px)

        for j, est in enumerate(skeleton.joints):
            if est.xyz is None:
                continue
            X = np.array(est.xyz)
            cams = rig.subset(est.camera_ids)
            if len(est.camera_ids) < cfg.min_views_for_valid:
                reasons[j].append(GateIssue.FEW_VIEWS_FOR_VALID)
            if not sigma_max[j] <= cfg.max_position_sigma_m:  # type: ignore[operator]
                reasons[j].append(GateIssue.HIGH_TRIANGULATION_UNCERTAINTY)
            # 속도 상한 (직전 VALID 위치 대비)
            last = self._last_valid.get(j)
            if last is not None and 0 < t - last[0] <= cfg.max_history_gap_ms:
                speeds[j] = float(np.linalg.norm(X - last[1])) / ((t - last[0]) / 1000.0)
                if speeds[j] > cfg.max_joint_speed_mps:  # type: ignore[operator]
                    reasons[j].append(GateIssue.SPEED_LIMIT)
            # 2대 관측 경쟁 후보. 확인할 수 없으면(None) 경쟁 후보가 없다고 보지 않는다
            if len(est.camera_ids) == MIN_VIEWS:
                competitor = (
                    None
                    if others is None
                    else _has_epipolar_competitor(j, cams, pixels, others, cfg, undistorted, others_undistorted)
                )
                if competitor is None:
                    reasons[j].append(GateIssue.COMPETITOR_CHECK_UNAVAILABLE)
                elif competitor:
                    reasons[j].append(GateIssue.TWO_VIEW_COMPETITOR)

        # 뼈 길이 변화: 기준이 있는 뼈가 허용 범위를 넘으면 두 끝점 모두 (어느 쪽이 틀렸는지 모른다)
        bones = COCO17_BONES if skeleton.joint_layout == "COCO17" else ()
        lengths: dict[tuple[int, int], float] = {}
        for a, b in bones:
            xa, xb = skeleton.joints[a].xyz, skeleton.joints[b].xyz
            if xa is None or xb is None:
                continue
            length = float(np.linalg.norm(np.subtract(xa, xb)))
            lengths[(a, b)] = length
            ref = self.bone_reference((a, b))
            if ref is not None and abs(length - ref) > max(cfg.bone_abs_tolerance_m, cfg.bone_rel_tolerance * ref):
                for k in (a, b):
                    if GateIssue.BONE_LENGTH_CHANGE not in reasons[k]:
                        reasons[k].append(GateIssue.BONE_LENGTH_CHANGE)

        joints = []
        for j, est in enumerate(skeleton.joints):
            if est.xyz is None:
                validity = JointValidity.UNAVAILABLE
            else:
                validity = JointValidity.UNCERTAIN if reasons[j] else JointValidity.VALID
            # 게이트는 등급을 낮추기만 한다
            validity = _weaker(validity, est.validity)
            joints.append(
                GatedJoint(
                    xyz=est.xyz,
                    validity=validity,
                    reasons=tuple(reasons[j]),
                    camera_ids=est.camera_ids,
                    reprojection_error_px=est.reprojection_error_px,
                    ray_angle_rad=est.ray_angle_rad,
                    sigma_xyz_m=sigma_xyz[j],
                    sigma_max_m=sigma_max[j],
                    speed_mps=speeds[j],
                )
            )

        # 이력 갱신: 속도는 최종 VALID 위치만, 뼈 길이 표본은 시간 검사 전 기하 기준을 통과한 끝점끼리만 쓴다.
        # (시간 검사 결과로 표본을 고르면 처음 잡힌 잘못된 기준이 고정될 수 있다. 중앙값이 가끔 섞이는 이상치를 견딘다)
        temporal = {GateIssue.SPEED_LIMIT, GateIssue.BONE_LENGTH_CHANGE}
        for j, gj in enumerate(joints):
            if gj.validity == JointValidity.VALID:
                self._last_valid[j] = (t, np.array(gj.xyz))
        geometry_ok = [
            gj.xyz is not None and skeleton.joints[j].validity == JointValidity.VALID and not (set(gj.reasons) - temporal)
            for j, gj in enumerate(joints)
        ]
        for (a, b), length in lengths.items():
            if geometry_ok[a] and geometry_ok[b]:
                self._bones.setdefault((a, b), deque(maxlen=cfg.bone_window)).append(length)

        return GatedSkeleton(
            t=t,
            joint_layout=skeleton.joint_layout,
            joints=tuple(joints),
            calibration_version=skeleton.calibration_version,
            camera_ids=skeleton.camera_ids,
            identity_uncertain_candidate=any(GateIssue.TWO_VIEW_COMPETITOR in gj.reasons for gj in joints),
        )


def triangulation_covariance(cameras: Sequence[RigCamera], X: np.ndarray, sigma_px: float) -> np.ndarray | None:
    """픽셀 잡음 σ(등방, 카메라 독립) 가정의 삼각측량 3D 공분산 σ²(JᵀJ)⁻¹. J 는 이상적 핀홀 픽셀 투영 야코비안.

    해가 퇴화했거나(정보 행렬 특이) 점이 카메라 뒤면 None.
    """
    X = np.asarray(X, dtype=float).reshape(1, 3)
    cov, ok = triangulation_covariances(cameras, np.ones((1, len(cameras)), dtype=bool), X, sigma_px)
    return cov[0] if ok[0] else None


def triangulation_covariances(
    cameras: Sequence[RigCamera], mask: np.ndarray, X: np.ndarray, sigma_px: float
) -> tuple[np.ndarray, np.ndarray]:
    """점 M 개의 `triangulation_covariance` 를 한 번에. mask (M, C) 는 점마다 쓴 카메라, X 는 (M, 3).

    (공분산 (M, 3, 3), 구했는지 (M,)). 구하지 못한 점(퇴화, 쓴 카메라 뒤)의 공분산 행은 NaN 이다.
    """
    X = np.asarray(X, dtype=float).reshape(-1, 3)
    mask = np.asarray(mask, dtype=bool)
    M = len(X)
    R = np.stack([cam.R for cam in cameras])
    t = np.stack([cam.t for cam in cameras])
    K = np.stack([cam.K for cam in cameras])
    xc = np.einsum("cij,mj->mci", R, X) + t
    x, y, z = xc[..., 0], xc[..., 1], xc[..., 2]
    front = ((z > 0) | ~mask).all(axis=1)
    z = np.where(mask & (z > 0), z, 1.0)
    fx, s, fy = K[:, 0, 0], K[:, 0, 1], K[:, 1, 1]
    zero = np.zeros_like(z)
    du = np.stack([fx / z, s / z, -(fx * x + s * y) / z**2], axis=2)
    dv = np.stack([zero, fy / z, -fy * y / z**2], axis=2)
    J = np.stack([np.einsum("mck,ckj->mcj", du, R), np.einsum("mck,ckj->mcj", dv, R)], axis=2)
    J = np.where(mask[:, :, None, None], J, 0.0).reshape(M, -1, 3)
    info = np.einsum("mri,mrj->mij", J, J)
    cov = np.full((M, 3, 3), np.nan)
    ok = front.copy()
    if ok.any():
        with np.errstate(divide="ignore", invalid="ignore"):
            cond = np.linalg.cond(info[ok])
        idx = np.flatnonzero(ok)
        ok[idx[~(cond <= 1e12)]] = False
    if ok.any():
        cov[ok] = sigma_px**2 * np.linalg.inv(info[ok])
    return cov, ok


def _position_sigmas(
    skeleton: SkeletonEstimate, rig: CameraRig, sigma_px: float
) -> tuple[list[tuple[float, float, float] | None], list[float | None]]:
    """관절마다 (공분산 대각 표준편차 (X, Y, Z), 가장 큰 축 표준편차). 좌표가 없으면 None, 공분산을 못 구하면 inf."""
    n = len(skeleton.joints)
    sigma_xyz: list[tuple[float, float, float] | None] = [None] * n
    sigma_max: list[float | None] = [None] * n
    solved = [j for j, est in enumerate(skeleton.joints) if est.xyz is not None]
    if not solved:
        return sigma_xyz, sigma_max
    cam_ids = sorted({cid for j in solved for cid in skeleton.joints[j].camera_ids})
    column = {cid: k for k, cid in enumerate(cam_ids)}
    mask = np.zeros((len(solved), len(cam_ids)), dtype=bool)
    for row, j in enumerate(solved):
        mask[row, [column[cid] for cid in skeleton.joints[j].camera_ids]] = True
    X = np.array([skeleton.joints[j].xyz for j in solved])
    cov, ok = _triangulation_covariances(rig.subset(cam_ids), mask, X, sigma_px)
    diag = np.sqrt(np.maximum(np.diagonal(cov, axis1=1, axis2=2), 0.0))
    biggest = np.full(len(solved), math.inf)
    if ok.any():
        biggest[ok] = np.sqrt(np.maximum(np.linalg.eigvalsh(cov[ok]).max(axis=1), 0.0))
    for row, j in enumerate(solved):
        if ok[row]:
            sigma_xyz[j] = (float(diag[row, 0]), float(diag[row, 1]), float(diag[row, 2]))
            sigma_max[j] = float(biggest[row])
        else:
            sigma_xyz[j], sigma_max[j] = (math.inf, math.inf, math.inf), math.inf
    return sigma_xyz, sigma_max


# 시험이 이전 구현과 바꿔 끼울 수 있게 모듈 안에서는 이 이름으로 부른다
_triangulation_covariances = triangulation_covariances


def epipolar_distance_px(cam_a: RigCamera, uv_a: Sequence[float], cam_b: RigCamera, uv_b: Sequence[float]) -> float:
    """cam_a 픽셀 uv_a 의 에피폴라선(cam_b 이상적 핀홀 픽셀 공간)과 cam_b 픽셀 uv_b 사이 거리.

    두 픽셀 중 하나라도 왜곡을 제거할 수 없으면(`RigCamera.normalize` 가 NaN) NaN 이다(거리를 모른다).
    """
    xa = cam_a.normalize(np.asarray(uv_a[:2], dtype=float))[0]
    xb = cam_b.normalize(np.asarray(uv_b[:2], dtype=float))
    return float(epipolar_distances_normalized(cam_a, xa, cam_b, xb)[0])


def epipolar_distances_normalized(
    cam_a: RigCamera, xa: np.ndarray, cam_b: RigCamera, xb: np.ndarray
) -> np.ndarray:
    """cam_a 정규화 좌표 xa (2,) 의 에피폴라선과 cam_b 정규화 좌표 xb (M, 2) 각각의 거리(cam_b 이상적 핀홀 픽셀).

    `epipolar_distance_px` 의 왜곡 제거 뒤 단계를 여러 점에 한 번에 한다. xa 나 그 행의 xb 가 유한하지 않으면 NaN.
    """
    xb = np.asarray(xb, dtype=float).reshape(-1, 2)
    xa_h = np.append(np.asarray(xa, dtype=float).reshape(2), 1.0)
    if not np.all(np.isfinite(xa_h)):
        return np.full(len(xb), math.nan)
    R = cam_b.R @ cam_a.R.T
    t = cam_b.t - R @ cam_a.t
    tx = np.array([[0.0, -t[2], t[1]], [t[2], 0.0, -t[0]], [-t[1], t[0], 0.0]])
    E = tx @ R
    line_n = E @ xa_h  # cam_b 정규화 좌표의 직선
    line_px = np.linalg.inv(cam_b.K).T @ line_n  # 이상적 핀홀 픽셀 좌표의 직선
    norm = math.hypot(line_px[0], line_px[1])
    out = np.full(len(xb), math.nan)
    finite = np.isfinite(xb).all(axis=1)
    if finite.any():
        px = cam_b.ideal_pixels(xb[finite])
        if norm == 0:
            out[finite] = math.inf
        else:
            out[finite] = np.abs(px[:, 0] * line_px[0] + px[:, 1] * line_px[1] + line_px[2]) / norm
    return out


def _has_epipolar_competitor(
    j: int,
    cams: Sequence[RigCamera],
    pixels: Mapping[str, Sequence[Keypoint]],
    others: Mapping[str, Sequence[Sequence[Keypoint]]],
    cfg: JointGateConfig,
    undistorted: Mapping[str, UndistortedKeypoints],
    others_undistorted: Mapping[str, Sequence[UndistortedKeypoints]] | None = None,
) -> bool | None:
    """두 카메라 중 한쪽의 이 사람 관측과 다른 쪽의 다른 사람 같은 관절이 에피폴라 조건을 만족하는지.

    에피폴라선은 카메라 앞뒤를 가리지 않으므로, 그렇게 짝지은 두 시선이 두 카메라 앞에서 만날 때만 후보로 센다.
    다른 사람의 그 관절 값(u, v, 신뢰도)에 유한하지 않은 값이 있거나 그 픽셀의 왜곡을 제거할 수 없으면 후보인지
    알 수 없으므로 None 이다(안전 쪽). 이 사람 관측은 `undistorted`(view 마다 한 번 푼 값)를 쓰고, 다른 사람의 그
    관절은 `others_undistorted` 에 그 검출의 결과가 있으면 쓰고 없으면 카메라마다 한 번에 왜곡 제거한다.
    """
    min_conf = cfg.triangulation.min_keypoint_confidence
    unknown = False
    for own, other in ((cams[0], cams[1]), (cams[1], cams[0])):
        if pixels[own.camera_id][j] is None:
            continue
        known = others_undistorted.get(other.camera_id, ()) if others_undistorted is not None else ()
        rows: list[np.ndarray | None] = []
        pending: list[tuple[float, float]] = []
        for n, kps in enumerate(others.get(other.camera_id, ())):
            c = kps[j]
            if c is None:
                continue
            if not all(math.isfinite(float(x)) for x in c[:3]):
                unknown = True
                continue
            if c[2] < min_conf:
                continue
            if n < len(known) and known[n].matches(other, kps):
                rows.append(known[n].normalized[j])
            else:
                rows.append(None)
                pending.append((float(c[0]), float(c[1])))
        if not rows:
            continue
        solved = iter(other.normalize(np.array(pending)) if pending else ())
        xb = np.array([next(solved) if row is None else row for row in rows])
        xa = undistorted[own.camera_id].normalized[j]
        distance = epipolar_distances_normalized(own, xa, other, xb)
        unknown = unknown or bool(np.isnan(distance).any())
        near = np.flatnonzero(distance <= cfg.competitor_epipolar_px)
        if len(near):
            pairs = np.stack([np.broadcast_to(xa, (len(near), 2)), xb[near]], axis=1)
            if np.isfinite(triangulate_normalized_many([own, other], pairs)).all(axis=1).any():
                return True
    return None if unknown else False


def _weaker(a: JointValidity, b: JointValidity) -> JointValidity:
    return _ORDER[min(_ORDER.index(a), _ORDER.index(b))]


def _require_coco17(layout: str) -> None:
    if layout != "COCO17":
        raise ValueError(f"이 계산은 COCO17 관절 배치만 지원한다: {layout}")
