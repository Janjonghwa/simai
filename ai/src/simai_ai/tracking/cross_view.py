"""Cross-view Association: 카메라별 로컬 트랙을 같은 사람끼리 묶는다 (명세 5.2 추적 3계층 2층, 계산 순서 2·3단계).

입력은 카메라별 Local Tracker 의 `observation_2d`(카메라 내 트랙 ID 포함)를 공통 시각 t 로 보간한 view
(`LocalTrackletBuffer`, `geometry.interpolation`)와 보정(`geometry.rig.CameraRig`)이다. 출력은 시각 t 의 view 클러스터
(같은 사람으로 본 카메라별 트랙 묶음)와 그 클러스터의 삼각측량 결과(`geometry.triangulation`)다.
MV3DT 원칙(공유 World 좌표, tracklet 연결, 전체 1:1 배정)을 Python 으로 구현한다. 참조: Chen et al. CVPR 2020 의
다시점 기하 친화도(에피폴라)와 연결 이력, RapidPoseTriangulation 의 학습 없는 삼각측량 거르기.

계산 순서 (명세 5.2):

1. 공통 시각 보간: 같은 t 로 보간된 view 끼리만 비교한다(시간 근거). 보간에 실패한 카메라·트랙은 그 t 에서 빠진다.
2. 연결 후보 생성 (카메라가 다른 view 쌍마다):
   - 에피폴라 거리: 두 view 모두 신뢰도 기준 이상인 관절의 대칭 에피폴라 거리 평균(근사 픽셀).
   - 임시 바닥 가설: 발목(양쪽 중점) 시선이 수평면 Z=h 와 만나는 XY 끼리의 거리. h 는 바닥(0)이 기본이고, 단상·
     작업면 위 사람을 놓치지 않도록 0 ~ `floor_hypothesis_max_height_m` 중 두 시선이 가장 가까운 높이로 본다.
     **이 값은 후보 비용에만 쓰는 근사다. World 위치·`ground_anchor_xyz`·Z 로 쓰지 않고, 결과 객체에도 내보내지
     않는다(명세 5.2 "임시 바닥 가설은 후보 생성에만 쓰고 안전용 접지나 Z로 쓰지 않는다", AGENTS.md 모르면 비운다).**
     비용은 min(거리 / `floor_scale_m`, `floor_missing_cost`) 로 거리에 단조 비감소이고, 가설이 없을 때와 같은 값에서
     멈춘다. 먼 거리는 "가설로 판단할 수 없음"(가설 높이보다 높은 면 위 사람일 수 있다, 예: 책상 위 의자 1.2 m,
     R3 의 2 m 고소)이라 더 벌하지 않는다. **바닥 가설만으로 쌍을 탈락시키지 않는다.** 탈락은 에피폴라·삼각측량(재투영 잔차, 관절 높이 범위) 근거로만 한다.
   - 궤적 일관성: 로컬 트랙 쌍이 이전 시각들에 같은 클러스터였던 횟수(연결 이력, `link_age`). 비용을 깎을 뿐
     기하 문턱을 넘은 쌍을 살리지 않는다.
3. 후보별 삼각측량 검증: 두 view 의 공통 관절을 2-view 선형 DLT(`geometry.triangulation` 과 같은 식, 관절 묶음으로
   벡터화한 `_two_view_triangulation`)로 풀어 재투영 잔차, 일관된 관절 비율, 관절 높이의 물리적 범위를 본다.
   클러스터 최종 관절(6단계·출력)은 `triangulate_skeleton` 그대로다. 관절 유효성 게이트(VALID/UNCERTAIN/UNAVAILABLE 상세)는 S15P31A101-29 몫이다.
4. 배정(제약 있는 최선 우선 병합): view 하나씩인 클러스터에서 시작해 합칠 수 있는 클러스터 쌍 중 비용이 가장 낮은
   쌍부터 합친다. 병합 비용은 두 클러스터 사이 view 쌍 비용의 평균에서 연결 이력 보너스를 뺀 값이다. 기하가 반대하는
   쌍이 하나라도 있으면 합치지 않고(완전 연결), 공통 관절이 적어 근거가 없는 쌍은 고정 비용으로 세며, 근거 있는 쌍이
   하나는 있어야 한다. **같은 카메라의 두 검출은 한 클러스터에 넣지 않는다**: 카메라가 겹치는 두 클러스터는 합치지
   않으므로 클러스터마다 카메라당 view 가 하나, view 마다 클러스터가 하나다(카메라별 1:1, 명세 5.2, 부록 D-G2-04).
   카메라를 차례로 배정하는 방식(Chen et al.)은 앞 카메라의 약한 연결이 먼저 굳어 뒤 카메라의 더 강한 반대 근거를
   가리는 경우가 SIM 교차 시나리오에서 나와 쓰지 않는다. 헝가리안 1:1 배정은 시각 사이 클러스터를 잇는 데 쓴다.
5. 애매하면 합치지 않는다: 고른 병합과 서로 배타적인 경쟁 병합(같은 카메라 자리를 두고 다투는 다른 클러스터)의 비용
   차가 `ambiguity_margin` 보다 작거나, 잔차가 경계(`residual_marginal_px` 초과)면 그 병합을 막고 관련 클러스터에
   `IDENTITY_UNCERTAIN` 사유를 붙인다. 2대뿐인 클러스터에 다른 사람 view 가 기하로 맞으면(제3의 카메라로 가릴 수
   없음) 역시 표시한다. 잘못 합친 ID 보다 갈라진 ID 가 덜 위험하다.
6. 클러스터 전체 삼각측량에서 대부분의 관절이 이상치로 빠지는 view 는 떼어 낸다.

계산 묶음(S15P31A101-168, docs/design/tier0-geometry-budget.md): 왜곡 제거는 카메라마다 view 전체를 한 번에 하고
(`RigCamera.undistort_many`), 후보 근거는 카메라 쌍마다 에피폴라 거리 행렬·2-view DLT 를 한 번에, 클러스터 삼각측량은
클러스터 전체를 `triangulate_skeletons` 한 번으로 푼다. 배정은 비용 행렬에서 합친 클러스터의 행·열만 다시 계산한다.
결과는 쌍·클러스터마다 따로 계산한 것과 같다. 클러스터의 왜곡 제거 결과(`ViewCluster.undistorted`)와 삼각측량
(`ViewCluster.skeleton`)은 관절 게이트가 다시 쓴다.

입력 계약: 동기화가 무효인 카메라(System Health 의 `HealthReport.sync_invalid_cameras`)는 융합에서 빼야 한다.
호출자(Tier 0 조립부)가 `associate(..., exclude_cameras=...)` 로 넘기면 그 카메라의 view 는 이 시각 연결에 쓰지
않는다. 프레임별 촬영 시각 불확실성은 보간 단계(`LocalTrackletBuffer`)가 거른다.

`association_id` 는 이 계층 안에서 시각 사이 클러스터를 잇는 내부 번호다. World ID 가 아니다. 최종 World ID·칼만
필터·늦은 재연결·ID 교정은 World Track Manager(S15P31A101-37) 몫이다. 이 모듈은 위험 판정이나 접지를 하지 않는다.
시각은 UTC int64 ms, 길이는 미터다. 현재 시각을 읽지 않는다.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from simai_ai.geometry.interpolation import (
    FrameDrop,
    InterpolatedView,
    InterpolationConfig,
    KeypointBuffer,
    KeypointSample,
)
from simai_ai.geometry.rig import CameraRig, RigCamera, UndistortedKeypoints
from simai_ai.geometry.triangulation import (
    SkeletonEstimate,
    TriangulationConfig,
    triangulate_skeleton,
    triangulate_skeletons,
)
from simai_ai.tracking.assignment import linear_sum_assignment
from simai_ai.tracking.local_tracker import Observation2DFrame
from simai_ai.tracking.time_model import require_ms

TrackKey = tuple[str, str]
"""(camera_id, local_track_id). 카메라 내 트랙 ID 는 카메라마다 따로 붙으므로 카메라와 함께 키로 쓴다"""

JOINT_COUNTS = {"COCO17": 17, "HALPE26": 26}
# 임시 바닥 가설에 쓰는 발목 관절 (COCO17 과 HALPE26 의 앞 17개는 같은 순서)
ANKLES = {"COCO17": (15, 16), "HALPE26": (15, 16)}


class RejectReason(StrEnum):
    """연결 후보 쌍을 탈락시킨 사유."""

    FEW_COMMON_JOINTS = "FEW_COMMON_JOINTS"
    """두 view 모두 신뢰도 기준 이상인 관절이 너무 적다"""
    EPIPOLAR = "EPIPOLAR"
    TRIANGULATION = "TRIANGULATION"
    """두 view 로 서로 맞게 삼각측량된 관절 비율이 낮다 (해 없음·카메라 뒤·이상치)"""
    RESIDUAL = "RESIDUAL"
    IMPLAUSIBLE_HEIGHT = "IMPLAUSIBLE_HEIGHT"
    """삼각측량한 관절 높이가 바닥 아래·천장 위로 많이 나간다 (엇갈린 시선의 허상 교점)"""


class UncertainReason(StrEnum):
    """`IDENTITY_UNCERTAIN` 표시 사유 (명세 5.2, 7.1)."""

    AMBIGUOUS_ASSIGNMENT = "AMBIGUOUS_ASSIGNMENT"
    """고른 짝과 경쟁 짝의 비용 차가 작아 합치지 않았다"""
    MARGINAL_RESIDUAL = "MARGINAL_RESIDUAL"
    """재투영 잔차가 경계라 합치지 않았다"""
    TWO_VIEW_COMPETITOR = "TWO_VIEW_COMPETITOR"
    """2대뿐인 클러스터에 다른 view 도 기하로 맞는다 (제3의 카메라로 가릴 수 없다)"""
    VIEW_SPLIT = "VIEW_SPLIT"
    """클러스터 전체 삼각측량에서 이상치가 된 view 를 떼어 냈다"""


@dataclass(frozen=True)
class CrossViewConfig:
    """초기값. 근거와 SIM 측정은 docs/design/tier0-cross-view.md. G2·A0 실측으로 조정한다."""

    min_keypoint_confidence: float = 0.3
    min_common_joints: int = 4
    epipolar_gate_px: float = 20.0
    """대칭 에피폴라 거리 평균의 상한 (근사 픽셀)"""
    epipolar_scale_px: float = 10.0
    floor_hypothesis_max_height_m: float = 1.0
    """임시 바닥 가설의 수평면 높이 상한. 0 이면 바닥 Z=0 만 본다. 단상(0.4 m)·책상(0.75 m) 위 사람을 놓치지 않게 둔다"""
    floor_scale_m: float = 1.0
    floor_missing_cost: float = 0.5
    """바닥 가설 비용의 상한. 가설이 없거나(발목이 안 보임) 거리가 멀어 판단할 수 없을 때 이 값이다 (유리해지지 않게)"""
    residual_gate_px: float = 12.0
    """두 view 삼각측량 재투영 잔차(관절 중앙값)의 상한"""
    residual_marginal_px: float = 8.0
    """이 값을 넘으면 잔차가 경계다: 새로 합치지 않고 IDENTITY_UNCERTAIN 으로 둔다"""
    residual_scale_px: float = 8.0
    min_consistent_joint_ratio: float = 0.6
    joint_z_range_m: tuple[float, float] = (-0.3, 4.5)
    """삼각측량 관절 높이의 물리적 범위. 상한은 R3 고소(지지 표면 2 m) 위 키 큰 사람의 손 든 높이(약 4.3 m)를 담는다.
    엇갈린 시선의 허상 교점(바닥 아래 깊이, 카메라 너머)을 거르는 용도라 천장 높이보다 넉넉히 둔다"""
    max_out_of_range_joint_ratio: float = 0.25
    no_evidence_cost: float = 1.0
    """공통 관절이 적어 근거가 없는 쌍(FEW_COMMON_JOINTS)의 클러스터 비용. 근거 있는 쌍이 하나는 있어야 넣는다"""
    new_cluster_cost: float = 3.0
    """배정 비용이 이 값 이상이면 기존 클러스터에 넣지 않고 새 클러스터를 만든다"""
    ambiguity_margin: float = 0.5
    """경쟁 병합과의 비용 차가 이보다 작으면 애매하다. 쌍 비용 척도에서 에피폴라 5 px 또는 잔차 4 px 차이에 해당"""
    history_bonus: float = 0.3
    """연결 이력이 꽉 찼을 때 깎는 비용. ambiguity_margin 보다 작게 둔다: 이력만으로는 기하가 같은 경쟁 후보를 이길 수
    없어(로컬 트랙 ID 가 옆 사람으로 넘어간 경우 등) 애매함 표시가 남고, 기하가 조금이라도 앞설 때만 흔들림을 막는다"""
    history_full_ticks: int = 10
    history_ttl_ms: int = 2000
    """한쪽 트랙이 안 보이는 동안 연결 이력을 들고 있는 시간"""
    same_camera_exclusive: bool = True
    """같은 카메라 금지 제약. **음성 대조 시험 전용으로만 끈다(운영에서 끄지 않는다).** 끄면 카메라가 겹치는 클러스터도
    합칠 수 있고, 같은 카메라 자리를 다투는 경쟁 병합이 없어지므로 그 애매함 검사도 함께 빠진다"""
    triangulation: TriangulationConfig = field(default_factory=TriangulationConfig)

    def __post_init__(self) -> None:
        if self.min_common_joints < 2:
            raise ValueError(f"min_common_joints 는 2 이상이어야 한다: {self.min_common_joints}")
        if not 0 < self.residual_marginal_px <= self.residual_gate_px:
            raise ValueError("0 < residual_marginal_px ≤ residual_gate_px 여야 한다")
        if self.floor_hypothesis_max_height_m < 0:
            raise ValueError("floor_hypothesis_max_height_m 는 0 이상이어야 한다")
        for name in ("epipolar_gate_px", "epipolar_scale_px", "floor_scale_m", "residual_scale_px"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} 는 양수여야 한다")
        if not self.new_cluster_cost > 0 or not self.ambiguity_margin >= 0:
            raise ValueError("new_cluster_cost 는 양수, ambiguity_margin 은 0 이상이어야 한다")
        if self.history_full_ticks < 1 or self.history_ttl_ms < 0:
            raise ValueError("history_full_ticks 는 1 이상, history_ttl_ms 는 0 이상이어야 한다")


@dataclass(frozen=True)
class LocalView:
    """카메라 한 대·로컬 트랙 하나를 공통 시각 t 로 보간한 2D keypoint."""

    camera_id: str
    local_track_id: str
    view: InterpolatedView

    def __post_init__(self) -> None:
        if self.view.camera_id != self.camera_id:
            raise ValueError(f"view 카메라 {self.view.camera_id} 와 camera_id {self.camera_id} 가 다르다")

    @property
    def key(self) -> TrackKey:
        return (self.camera_id, self.local_track_id)

    @property
    def t(self) -> int:
        return self.view.t


@dataclass(frozen=True)
class PairEvidence:
    """카메라가 다른 view 쌍 하나의 연결 근거 (계산 순서 2·3단계). cost 가 inf 면 탈락(reject)이다.

    floor_gap_m 은 임시 바닥 가설 XY 사이 거리로, 후보 거르기에만 쓴다(위치가 아니다).
    """

    a: TrackKey
    b: TrackKey
    common_joints: int
    epipolar_px: float | None = None
    floor_gap_m: float | None = None
    residual_px: float | None = None
    consistent_joints: int = 0
    reject: RejectReason | None = None
    marginal: bool = False
    cost: float = math.inf

    @property
    def feasible(self) -> bool:
        return self.reject is None


@dataclass(frozen=True)
class ViewCluster:
    """시각 t 에 같은 사람으로 본 view 묶음. 같은 카메라는 한 번만 들어 있다.

    skeleton: 2대 이상이면 클러스터 view 전부로 삼각측량한 결과(관절은 모두 observed), 1대면 None.
    competitors: 경쟁 후보였던 다른 클러스터의 트랙 키.
    """

    association_id: str
    views: tuple[LocalView, ...]
    skeleton: SkeletonEstimate | None
    uncertain_reasons: tuple[UncertainReason, ...] = ()
    competitors: tuple[TrackKey, ...] = ()
    undistorted: Mapping[str, UndistortedKeypoints] = field(default_factory=dict, compare=False, repr=False)
    """카메라 ID → 그 view 를 왜곡 제거한 결과. 관절 게이트(`JointGate.update(undistorted=...)`)가 다시 풀지 않고 쓴다"""

    @property
    def identity_uncertain(self) -> bool:
        """`identity_confidence = IDENTITY_UNCERTAIN` 으로 낼 클러스터인지."""
        return bool(self.uncertain_reasons)

    @property
    def keys(self) -> tuple[TrackKey, ...]:
        return tuple(v.key for v in self.views)

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(v.camera_id for v in self.views)


@dataclass(frozen=True)
class AssociationFrame:
    """공통 시각 t 의 Cross-view Association 결과."""

    t: int
    calibration_version: str
    clusters: tuple[ViewCluster, ...]
    pairs: tuple[PairEvidence, ...]
    excluded: tuple[TrackKey, ...] = ()
    """exclude_cameras 로 이 시각 연결에서 뺀 view"""

    def cluster_of(self, key: TrackKey) -> ViewCluster | None:
        for c in self.clusters:
            if key in c.keys:
                return c
        return None


@dataclass
class _Prepared:
    view: LocalView
    camera: RigCamera
    mask: np.ndarray
    """신뢰도 기준 이상인 관절"""
    undistorted: UndistortedKeypoints
    """view keypoint 를 한 번에 왜곡 제거한 결과. 후보 근거·클러스터 삼각측량·관절 게이트가 같이 쓴다"""
    normalized: np.ndarray
    """왜곡을 제거한 정규화 좌표 (관절 수, 2). mask 가 False 인 행은 의미 없음"""
    floor_ray: tuple[np.ndarray, np.ndarray] | None
    """임시 바닥 가설: 높이 h 수평면 위 XY = A + h·B 의 (A, B). 후보 거르기 전용, 밖으로 내보내지 않는다"""


@dataclass
class _Work:
    keys: list[TrackKey]


@dataclass
class _Flag:
    reason: UncertainReason
    flagged: frozenset[TrackKey]
    """이 키가 든 클러스터에 사유를 붙인다"""
    involved: frozenset[TrackKey]
    """경쟁 후보로 적을 키"""
    blocked: frozenset[TrackKey] | None = None
    """막은 병합의 두 클러스터 키 합. 뒤에 더 많은 근거로 한 클러스터가 되었으면 애매함이 풀린 것으로 보고 붙이지 않는다"""


class CrossViewAssociator:
    """시각마다 view 를 클러스터로 묶는다. 시각은 늘어나는 순서로만 넣는다(연결 이력이 시각 순서에 기댄다)."""

    def __init__(self, rig: CameraRig, config: CrossViewConfig | None = None, joint_layout: str = "COCO17") -> None:
        if joint_layout not in JOINT_COUNTS:
            raise ValueError(f"지원하지 않는 관절 배치: {joint_layout}")
        self.rig = rig
        self.config = config if config is not None else CrossViewConfig()
        self.joint_layout = joint_layout
        self._n_joints = JOINT_COUNTS[joint_layout]
        self._essential: dict[tuple[str, str], np.ndarray] = {}
        self._history: dict[tuple[TrackKey, TrackKey], tuple[int, int]] = {}
        """정렬한 트랙 키 쌍 → (같은 클러스터였던 시각 수, 마지막으로 같은 클러스터였던 시각)"""
        self._previous: dict[str, frozenset[TrackKey]] = {}
        self._issued = 0
        self._last_t: int | None = None

    # ------------------------------------------------------------------ 공개

    def link_age(self, a: TrackKey, b: TrackKey) -> int:
        """두 로컬 트랙이 같은 클러스터였던 시각 수 (궤적 일관성 근거).

        둘 다 보이는데 갈라지면(기하가 반대하거나, 애매함 없이 다른 클러스터로 가면) 0 으로 지운다. 한쪽이 안 보이는
        동안은 마지막으로 함께였던 시각부터 `history_ttl_ms` 까지 들고 있다.
        """
        return self._history.get(_pair_key(a, b), (0, 0))[0]

    def associate(self, t: int, views: Sequence[LocalView], exclude_cameras: Collection[str] = ()) -> AssociationFrame:
        """공통 시각 t 의 view 를 클러스터로 묶는다.

        exclude_cameras: 이 시각 융합에서 뺄 카메라(동기화 무효 등, 호출자가 System Health 에서 넘긴다). 그 카메라의
        view 는 클러스터에 넣지 않고 `AssociationFrame.excluded` 에 키만 남긴다.
        """
        require_ms("t", t)
        if self._last_t is not None and t <= self._last_t:
            raise ValueError(f"시각은 늘어나는 순서로 넣는다: 직전 {self._last_t}, 받은 값 {t}")
        keys = [v.key for v in views]
        if len(keys) != len(set(keys)):
            raise ValueError(f"같은 (카메라, 로컬 트랙) view 가 두 번 있다: {keys}")
        for v in views:
            if v.t != t:
                raise ValueError(f"{v.key}: 공통 시각 {t} 가 아닌 view ({v.t}) 다")
            if len(v.view.keypoints) != self._n_joints:
                raise ValueError(
                    f"{v.key}: {self.joint_layout} 는 keypoint {self._n_joints}개인데 {len(v.view.keypoints)}개"
                )
        self.rig.subset(sorted({v.camera_id for v in views}))  # 보정에 없는 카메라면 KeyError
        if isinstance(exclude_cameras, str):
            raise TypeError(f"exclude_cameras 는 카메라 ID 모음이어야 한다 (문자열 하나를 받음: {exclude_cameras!r})")
        excluded = frozenset(exclude_cameras)
        unknown = sorted(excluded - set(self.rig.cameras))
        if unknown:
            raise ValueError(f"exclude_cameras 에 보정 {self.rig.calibration_version} 에 없는 카메라가 있다: {unknown}")
        dropped = tuple(sorted(v.key for v in views if v.camera_id in excluded))

        used = sorted((v for v in views if v.camera_id not in excluded), key=lambda v: v.key)
        # 왜곡 제거는 카메라마다 그 카메라의 view 전체를 한 번에 한다
        by_camera: dict[str, list[LocalView]] = {}
        for v in used:
            by_camera.setdefault(v.camera_id, []).append(v)
        undistorted: dict[TrackKey, UndistortedKeypoints] = {}
        for cid, cam_views in by_camera.items():
            results = self.rig.cameras[cid].undistort_many([v.view.keypoints for v in cam_views])
            undistorted.update((v.key, u) for v, u in zip(cam_views, results))
        prepared = {v.key: self._prepare(v, undistorted[v.key]) for v in used}
        pairs = self._pairs(prepared)

        flags: list[_Flag] = []
        works = self._assign(prepared, pairs, flags)
        skeletons = self._validate(works, prepared, flags)
        self._two_view_competitors(works, pairs, flags)
        clusters = self._finish(works, skeletons, prepared, flags)
        self._update_history(t, clusters, pairs, set(prepared))
        self._last_t = t
        return AssociationFrame(
            t=t,
            calibration_version=self.rig.calibration_version,
            clusters=clusters,
            pairs=tuple(pairs[k] for k in sorted(pairs)),
            excluded=dropped,
        )

    # ------------------------------------------------------------------ 2단계: 후보 근거

    def _prepare(self, lv: LocalView, undistorted: UndistortedKeypoints | None = None) -> _Prepared:
        camera = self.rig.cameras[lv.camera_id]
        if undistorted is None:
            undistorted = camera.undistort_keypoints(lv.view.keypoints)
        # 값이 유한하지 않거나 왜곡을 제거할 수 없는 keypoint 는 관측으로 세지 않는다(모르면 비움)
        mask = undistorted.usable(self.config.min_keypoint_confidence)
        normalized = undistorted.normalized
        floor_ray = None
        ankles = [j for j in ANKLES[self.joint_layout] if mask[j]]
        if ankles:
            floor_ray = _floor_ray(camera, normalized[ankles].mean(axis=0))
        return _Prepared(lv, camera, mask, undistorted, normalized, floor_ray)

    def _essential_matrix(self, a: RigCamera, b: RigCamera) -> np.ndarray:
        """x_b·E·x_a = 0 인 E (정규화 좌표). x_b = R_ab x_a + t_ab."""
        key = (a.camera_id, b.camera_id)
        E = self._essential.get(key)
        if E is None:
            R_ab = b.R @ a.R.T
            t_ab = b.t - R_ab @ a.t
            tx = np.array([[0.0, -t_ab[2], t_ab[1]], [t_ab[2], 0.0, -t_ab[0]], [-t_ab[1], t_ab[0], 0.0]])
            E = tx @ R_ab
            self._essential[key] = E
        return E

    def _pairs(self, prepared: dict[TrackKey, _Prepared]) -> dict[tuple[TrackKey, TrackKey], PairEvidence]:
        """카메라가 다른 view 쌍 전체의 근거. 카메라 쌍마다 에피폴라 거리와 2-view 삼각측량을 한 번에 계산한다.

        쌍 하나씩 `_pair` 로 계산한 것과 결과가 같다(부동소수 연산 순서만 다르다). 문턱을 넘은 쌍의 판정 근거(일관된
        관절 수, 잔차 중앙값, 높이 범위 밖 관절 수, 바닥 가설 거리)도 카메라 쌍마다 행렬로 한 번에 구한다(S15P31A101-237).
        """
        cfg = self.config
        by_camera: dict[str, list[TrackKey]] = {}
        for key in sorted(prepared):
            by_camera.setdefault(key[0], []).append(key)
        cams = sorted(by_camera)
        stacks = {cid: _ViewStack.of([prepared[k] for k in by_camera[cid]]) for cid in cams}
        found: dict[tuple[str, str], tuple[list[list[int]], list[list[float]], dict[tuple[int, int], PairEvidence]]] = {}
        for cam_a, cam_b in itertools.combinations(cams, 2):
            pas = [prepared[k] for k in by_camera[cam_a]]
            pbs = [prepared[k] for k in by_camera[cam_b]]
            epi, common = self._epipolar_matrix(pas, pbs)
            counts = common.sum(axis=2)
            queued = np.argwhere((counts >= cfg.min_common_joints) & ~(epi > cfg.epipolar_gate_px))
            found[(cam_a, cam_b)] = (
                counts.tolist(),
                epi.tolist(),
                self._evidence_many(stacks[cam_a], stacks[cam_b], common, epi, queued),
            )
        # 키 순서((카메라 a, 트랙) → (카메라 b, 트랙))로 바로 낸다. 정렬한 것과 같은 순서다
        out: dict[tuple[TrackKey, TrackKey], PairEvidence] = {}
        for c, cam_a in enumerate(cams):
            for i, ka in enumerate(by_camera[cam_a]):
                for cam_b in cams[c + 1 :]:
                    counts_ab, epi_ab, evidence = found[(cam_a, cam_b)]
                    for k, kb in enumerate(by_camera[cam_b]):
                        n = counts_ab[i][k]
                        # 대부분의 쌍(다른 사람)은 앞의 두 탈락 조건에서 끝난다. `_evidence` 의 처음 두 분기와 같다
                        if n < cfg.min_common_joints:
                            out[(ka, kb)] = PairEvidence(ka, kb, n, reject=RejectReason.FEW_COMMON_JOINTS)
                        elif epi_ab[i][k] > cfg.epipolar_gate_px:
                            out[(ka, kb)] = PairEvidence(
                                ka, kb, n, epipolar_px=epi_ab[i][k], reject=RejectReason.EPIPOLAR
                            )
                        else:
                            out[(ka, kb)] = evidence[(i, k)]
        return out

    def _evidence_many(
        self, sa: _ViewStack, sb: _ViewStack, common: np.ndarray, epi: np.ndarray, queued: np.ndarray
    ) -> dict[tuple[int, int], PairEvidence]:
        """카메라 쌍 (a, b) 에서 에피폴라 문턱을 넘은 view 쌍 (i, k) 전체의 `_evidence` 를 한 번에 구한다."""
        if not len(queued):
            return {}
        qi, qk = queued[:, 0], queued[:, 1]
        sel = common[qi, qk]  # (Q, J) 쌍마다 공통 관절
        q, j = np.nonzero(sel)  # 쌍 순서, 쌍 안에서는 관절 순서 (= 쌍마다 공통 관절을 이어 붙인 순서)
        a_rows, b_rows = (qi[q], j), (qk[q], j)
        xyz, err = _two_view_arrays(
            sa.camera,
            sb.camera,
            (sa.normalized[a_rows], sa.confidence[a_rows], sa.ideal[a_rows]),
            (sb.normalized[b_rows], sb.confidence[b_rows], sb.ideal[b_rows]),
        )
        Q, J = sel.shape
        tri_cfg = self.config.triangulation
        solved = np.zeros((Q, J), dtype=bool)
        solved[q, j] = np.isfinite(err) & (err <= tri_cfg.outlier_reprojection_error_px)
        n_ok = solved.sum(axis=1)
        # 일관된 관절의 재투영 오차 중앙값 (np.median 과 같은 값: 홀수면 가운데, 짝수면 가운데 둘의 평균)
        ranked = np.full((Q, J), np.inf)
        ranked[q, j] = err
        ranked = np.sort(np.where(solved, ranked, np.inf), axis=1)
        rows = np.arange(Q)
        upper = ranked[rows, np.maximum(n_ok // 2, 0)]
        lower = ranked[rows, np.maximum(n_ok // 2 - 1, 0)]
        with np.errstate(invalid="ignore"):
            residual = np.where(n_ok % 2 == 1, upper, (lower + upper) / 2.0)
        z = np.full((Q, J), np.nan)
        z[q, j] = xyz[:, 2]
        z_lo, z_hi = self.config.joint_z_range_m
        with np.errstate(invalid="ignore"):
            outside = (solved & ((z < z_lo) | (z > z_hi))).sum(axis=1)
        gap = _floor_gaps(sa, sb, qi, qk, self.config.floor_hypothesis_max_height_m)
        n_common = sel.sum(axis=1)
        out = {}
        for r, (i, k) in enumerate(zip(qi.tolist(), qk.tolist())):
            out[(i, k)] = self._verdict(
                sa.keys[i],
                sb.keys[k],
                int(n_common[r]),
                float(epi[i, k]),
                None if math.isnan(gap[r]) else float(gap[r]),
                int(n_ok[r]),
                float(residual[r]),
                int(outside[r]),
            )
        return out

    def _pair(self, pa: _Prepared, pb: _Prepared) -> PairEvidence:
        """view 쌍 하나의 근거 (`_pairs` 의 쌍 하나)."""
        common = pa.mask & pb.mask
        if int(common.sum()) < self.config.min_common_joints:
            return self._evidence(pa, pb, common, math.nan, None)
        epi = self._epipolar_px(pa, pb, common)
        tri = _two_view_triangulation(pa, pb, common) if not epi > self.config.epipolar_gate_px else None
        return self._evidence(pa, pb, common, epi, tri)

    def _evidence(
        self,
        pa: _Prepared,
        pb: _Prepared,
        common: np.ndarray,
        epi: float,
        tri: tuple[np.ndarray, np.ndarray] | None,
    ) -> PairEvidence:
        """공통 관절·에피폴라 거리·2-view 삼각측량(공통 관절 순서의 XYZ, 재투영 오차)으로 쌍 근거를 정한다."""
        cfg = self.config
        a, b = pa.view.key, pb.view.key
        n_common = int(common.sum())
        if n_common < cfg.min_common_joints:
            return PairEvidence(a, b, n_common, reject=RejectReason.FEW_COMMON_JOINTS)

        if epi > cfg.epipolar_gate_px:
            return PairEvidence(a, b, n_common, epipolar_px=epi, reject=RejectReason.EPIPOLAR)
        assert tri is not None

        gap = None
        if pa.floor_ray is not None and pb.floor_ray is not None:
            gap = _floor_gap(pa.floor_ray, pb.floor_ray, cfg.floor_hypothesis_max_height_m)

        xyz, err = tri
        solved = np.isfinite(err) & (err <= cfg.triangulation.outlier_reprojection_error_px)
        n_ok = int(solved.sum())
        residual = float(np.median(err[solved])) if n_ok else math.nan
        z_lo, z_hi = cfg.joint_z_range_m
        z = xyz[solved, 2]
        outside = int(np.sum((z < z_lo) | (z > z_hi)))
        return self._verdict(a, b, n_common, epi, gap, n_ok, residual, outside)

    def _verdict(
        self,
        a: TrackKey,
        b: TrackKey,
        n_common: int,
        epi: float,
        gap: float | None,
        n_ok: int,
        residual: float,
        outside: int,
    ) -> PairEvidence:
        """에피폴라 문턱을 넘은 쌍의 판정. n_ok 는 서로 맞게 삼각측량된 관절 수, residual 은 그 재투영 오차 중앙값,
        outside 는 그중 높이 범위 밖 관절 수다. gap(바닥 가설 거리)은 기록·비용에만 쓴다. 가설 높이보다 높은 면 위
        사람일 수 있으므로 이 값으로는 탈락시키지 않는다."""
        cfg = self.config
        base = {"common_joints": n_common, "epipolar_px": epi, "floor_gap_m": gap, "consistent_joints": n_ok}
        if n_ok < max(cfg.min_common_joints, cfg.min_consistent_joint_ratio * n_common):
            return PairEvidence(a, b, **base, reject=RejectReason.TRIANGULATION)
        if outside > cfg.max_out_of_range_joint_ratio * n_ok:
            return PairEvidence(a, b, **base, residual_px=residual, reject=RejectReason.IMPLAUSIBLE_HEIGHT)
        if residual > cfg.residual_gate_px:
            return PairEvidence(a, b, **base, residual_px=residual, reject=RejectReason.RESIDUAL)
        floor_cost = cfg.floor_missing_cost if gap is None else min(gap / cfg.floor_scale_m, cfg.floor_missing_cost)
        cost = epi / cfg.epipolar_scale_px + residual / cfg.residual_scale_px + floor_cost
        return PairEvidence(
            a, b, **base, residual_px=residual, marginal=residual > cfg.residual_marginal_px, cost=float(cost)
        )

    def _epipolar_px(self, pa: _Prepared, pb: _Prepared, common: np.ndarray) -> float:
        """공통 관절의 대칭 에피폴라 거리 평균. 정규화 좌표 거리에 초점 거리를 곱한 근사 픽셀이다.

        쌍 하나용이다. 시각마다 전체 쌍은 `_epipolar_matrix` 가 같은 식을 카메라 쌍마다 한 번에 계산한다.
        """
        E = self._essential_matrix(pa.camera, pb.camera)
        xa = np.column_stack([pa.normalized[common], np.ones(int(common.sum()))])
        xb = np.column_stack([pb.normalized[common], np.ones(int(common.sum()))])
        lb = xa @ E.T  # b 영상의 에피폴라선
        la = xb @ E  # a 영상의 에피폴라선
        alg = np.abs(np.sum(xb * lb, axis=1))
        db = alg / np.maximum(np.hypot(lb[:, 0], lb[:, 1]), 1e-12) * _focal(pb.camera)
        da = alg / np.maximum(np.hypot(la[:, 0], la[:, 1]), 1e-12) * _focal(pa.camera)
        return float(np.mean((da + db) / 2.0))

    def _epipolar_matrix(self, pas: Sequence[_Prepared], pbs: Sequence[_Prepared]) -> tuple[np.ndarray, np.ndarray]:
        """카메라 a 의 view 들 × 카메라 b 의 view 들의 대칭 에피폴라 거리 평균 (len(pas), len(pbs))과 공통 관절
        (len(pas), len(pbs), 관절 수). 공통 관절이 없으면 NaN. 거리는 정규화 좌표 거리 × 초점 거리(근사 픽셀)다."""
        E = self._essential_matrix(pas[0].camera, pbs[0].camera)
        mask_a = np.stack([p.mask for p in pas])
        mask_b = np.stack([p.mask for p in pbs])
        common = mask_a[:, None, :] & mask_b[None, :, :]
        xa = np.concatenate([np.stack([p.normalized for p in pas]), np.ones((len(pas), self._n_joints, 1))], axis=2)
        xb = np.concatenate([np.stack([p.normalized for p in pbs]), np.ones((len(pbs), self._n_joints, 1))], axis=2)
        xa = np.where(mask_a[:, :, None], xa, 0.0)  # 관측이 아닌 관절(NaN 일 수 있다)은 계산만 막는다
        xb = np.where(mask_b[:, :, None], xb, 0.0)
        lb = xa @ E.T  # b 영상의 에피폴라선 (len(pas), J, 3)
        la = xb @ E  # a 영상의 에피폴라선 (len(pbs), J, 3)
        alg = np.abs(np.einsum("kjc,ijc->ikj", xb, lb))
        db = alg / np.maximum(np.hypot(lb[..., 0], lb[..., 1]), 1e-12)[:, None, :] * _focal(pbs[0].camera)
        da = alg / np.maximum(np.hypot(la[..., 0], la[..., 1]), 1e-12)[None, :, :] * _focal(pas[0].camera)
        n = common.sum(axis=2)
        with np.errstate(invalid="ignore", divide="ignore"):
            epi = np.where(common, (da + db) / 2.0, 0.0).sum(axis=2) / n
        return np.where(n > 0, epi, np.nan), common

    # ------------------------------------------------------------------ 4·5단계: 배정

    def _linkage(
        self,
        a: Sequence[TrackKey],
        b: Sequence[TrackKey],
        pairs: dict[tuple[TrackKey, TrackKey], PairEvidence],
    ) -> tuple[float, bool]:
        """두 클러스터를 합치는 비용과 잔차 경계 여부. 합칠 수 없으면 (inf, False).

        카메라가 다른 view 쌍 비용의 평균에서 연결 이력 보너스 평균을 뺀다. 기하가 반대하는 쌍이 하나라도 있으면
        합치지 않는다(완전 연결). 공통 관절이 적어 근거가 없는 쌍은 고정 비용으로 세고, 근거 있는 쌍이 하나는 있어야 한다.
        """
        cfg = self.config
        if cfg.same_camera_exclusive and {k[0] for k in a} & {k[0] for k in b}:
            return math.inf, False  # 같은 카메라 금지 제약
        costs, bonuses, marginal, evidence = [], [], False, 0
        for ka in a:
            for kb in b:
                if ka[0] == kb[0]:
                    continue  # (음성 대조에서만 온다) 같은 카메라 쌍은 근거가 없다
                ev = pairs[_pair_key(ka, kb)]
                if ev.reject is RejectReason.FEW_COMMON_JOINTS:
                    costs.append(cfg.no_evidence_cost)
                elif not ev.feasible:
                    return math.inf, False
                else:
                    costs.append(ev.cost)
                    marginal = marginal or ev.marginal
                    evidence += 1
                age = min(self.link_age(ka, kb), cfg.history_full_ticks)
                bonuses.append(cfg.history_bonus * age / cfg.history_full_ticks)
        if not evidence:
            return math.inf, False
        return statistics.fmean(costs) - statistics.fmean(bonuses), marginal

    def _assign(
        self,
        prepared: dict[TrackKey, _Prepared],
        pairs: dict[tuple[TrackKey, TrackKey], PairEvidence],
        flags: list[_Flag],
    ) -> list[_Work]:
        """제약 있는 최선 우선 병합. view 하나씩인 클러스터에서 시작해, 합칠 수 있는 클러스터 쌍 중 비용이 가장 낮은
        쌍부터 합친다. 카메라 순서에 기대지 않으므로 약한 연결이 먼저 굳어 더 강한 반대 근거를 가리지 않는다.

        - 같은 카메라 금지: 카메라가 겹치는 두 클러스터는 합치지 않는다. 그래서 클러스터마다 카메라당 view 가 하나이고
          view 마다 클러스터가 하나다(카메라별 1:1).
        - 애매하면 합치지 않는다: (A, B) 를 합치는 비용과, 서로 배타적인 경쟁 병합 — A 와 B 의 카메라가 겹치는 C,
          또는 B 와 A 의 카메라가 겹치는 C — 의 비용 차가 ambiguity_margin 보다 작으면 (A, B) 를 이번 시각에 막고
          관련 클러스터에 표시한다. 잔차가 경계인 쌍이 든 병합도 막고 표시한다.
        """
        cfg = self.config
        works = [_Work([k]) for k in sorted(prepared)]
        n = len(works)
        row = {w.keys[0]: r for r, w in enumerate(works)}
        cams = sorted({k[0] for k in prepared})
        # 행·열 번호는 처음 view 순서로 고정하고 합쳐 없어진 클러스터는 alive 로 뺀다(남은 클러스터의 순서는 그대로다).
        # member[클러스터, 카메라]: 클러스터에 그 카메라 view 가 있는지
        alive = np.ones(n, dtype=bool)
        member = np.zeros((n, len(cams)), dtype=bool)
        member[np.arange(n), [cams.index(w.keys[0][0]) for w in works]] = True
        # 클러스터 쌍 병합 비용 (cost, marginal). 병합하면 합친 클러스터의 행·열만 다시 계산한다(나머지 쌍은 그대로다).
        # veto: `_linkage` 가 근거와 상관없이 inf 를 내는 쌍(같은 카메라 금지, 기하가 반대하는 view 쌍이 있음). 합친
        # 클러스터의 veto 는 두 행의 OR 이라 다시 계산하지 않는다. blocked: 이번 시각에 막은 병합 (S15P31A101-237)
        cost = np.full((n, n), math.inf)
        marginal = np.zeros((n, n), dtype=bool)
        blocked = np.zeros((n, n), dtype=bool)
        veto = (member[:, None, :] & member[None, :, :]).any(axis=2) if cfg.same_camera_exclusive else np.eye(n) > 0
        for (ka, kb), ev in pairs.items():
            i, j = row[ka], row[kb]
            if ev.feasible:  # view 하나씩인 클러스터는 근거 있는(통과한) 쌍만 비용이 유한하다
                cost[i, j], marginal[i, j] = self._linkage([ka], [kb], pairs)
                cost[j, i], marginal[j, i] = cost[i, j], marginal[i, j]
            elif ev.reject is not RejectReason.FEW_COMMON_JOINTS:
                veto[i, j] = veto[j, i] = True
        while True:
            open_ = np.triu(cost < cfg.new_cluster_cost, k=1) & ~blocked
            if not open_.any():
                return [w for w, keep in zip(works, alive) if keep]
            # 비용이 가장 낮은 쌍. 같은 비용이면 (두 클러스터 키 중 가장 작은 키, i, j) 순서다
            ii, jj = np.nonzero(open_)
            values = cost[ii, jj]
            ties = np.flatnonzero(values == values.min())
            _, i, j = min((min(works[ii[t]].keys + works[jj[t]].keys), int(ii[t]), int(jj[t])) for t in ties)
            c = float(cost[i, j])
            a, b = works[i], works[j]
            if marginal[i, j]:
                keys = frozenset(a.keys + b.keys)
                flags.append(_Flag(UncertainReason.MARGINAL_RESIDUAL, keys, keys, keys))
                blocked[i, j] = True
                continue
            rivals: list[TrackKey] = []
            if cfg.same_camera_exclusive:
                shares_a = (member & member[i]).any(axis=1)
                shares_b = (member & member[j]).any(axis=1)
                near = (shares_b & (cost[i] - c < cfg.ambiguity_margin)) | (
                    shares_a & (cost[j] - c < cfg.ambiguity_margin)
                )
                near[[i, j]] = False
                for k in np.flatnonzero(near):
                    rivals.extend(works[k].keys)
            if rivals:
                keys = frozenset(a.keys + b.keys + rivals)
                flags.append(_Flag(UncertainReason.AMBIGUOUS_ASSIGNMENT, keys, keys, frozenset(a.keys + b.keys)))
                blocked[i, j] = True
                continue
            a.keys.extend(b.keys)
            alive[j] = False
            member[i] |= member[j]
            member[j] = False
            veto[i] |= veto[j]
            veto[:, i] = veto[i]
            cost[j, :] = cost[:, j] = math.inf
            # 합친 클러스터의 키 집합은 처음 생긴 것이라 그 클러스터가 든 막은 병합은 없다
            blocked[i, :] = blocked[:, i] = False
            for k in np.flatnonzero(alive).tolist():
                if k != i:
                    if veto[i, k]:
                        cost[i, k], marginal[i, k] = math.inf, False
                    else:
                        cost[i, k], marginal[i, k] = self._linkage(works[i].keys, works[k].keys, pairs)
                    cost[k, i], marginal[k, i] = cost[i, k], marginal[i, k]

    # ------------------------------------------------------------------ 3·6단계: 클러스터 검증

    def _validate(
        self, works: list[_Work], prepared: dict[TrackKey, _Prepared], flags: list[_Flag]
    ) -> dict[int, SkeletonEstimate]:
        """클러스터 전체를 삼각측량하고, 대부분의 관절에서 이상치로 빠진 view 를 떼어 낸다."""
        skeletons: dict[int, SkeletonEstimate] = {}

        def triangulable(w: _Work) -> bool:
            # 1대, 또는 (음성 대조에서) 같은 카메라가 겹친 클러스터는 삼각측량하지 않는다
            cams = [k[0] for k in w.keys]
            return len(cams) >= 2 and len(set(cams)) == len(cams)

        def inputs(members: list[TrackKey]) -> tuple[list[InterpolatedView], dict[str, UndistortedKeypoints]]:
            # view 는 키 순서(= ViewCluster.views 순서)로 넘긴다. 관절 게이트가 같은 순서의 view 로 이 결과를 다시 쓴다
            return [prepared[k].view.view for k in members], {k[0]: prepared[k].undistorted for k in members}

        # 처음 클러스터 전체를 한 번에 삼각측량해 두고, view 를 떼어 낸 클러스터만 다시 푼다(결과는 클러스터마다 같다)
        first = [sorted(w.keys) for w in works if triangulable(w)]
        batch = triangulate_skeletons(
            [inputs(m)[0] for m in first],
            self.rig,
            self.joint_layout,
            self.config.triangulation,
            undistorted=[inputs(m)[1] for m in first],
        )
        done = {tuple(m): sk for m, sk in zip(first, batch)}
        i = 0
        while i < len(works):
            w = works[i]
            cams = [k[0] for k in w.keys]
            if not triangulable(w):
                i += 1
                continue
            members = sorted(w.keys)
            sk = done.get(tuple(members))
            if sk is None:
                views, und = inputs(members)
                tri = self.config.triangulation
                sk = triangulate_skeleton(views, self.rig, self.joint_layout, tri, undistorted=und)
            used = {c: sum(1 for j in sk.joints if c in j.camera_ids) for c in cams}
            rejected = {c: sum(1 for j in sk.joints if c in j.rejected_camera_ids) for c in cams}
            worst = max(cams, key=lambda c: (rejected[c] - used[c], c))
            if rejected[worst] > used[worst]:
                key = next(k for k in w.keys if k[0] == worst)
                flags.append(_Flag(UncertainReason.VIEW_SPLIT, frozenset(w.keys), frozenset(w.keys)))
                w.keys.remove(key)
                works.append(_Work([key]))
                continue  # 같은 클러스터를 다시 검증한다
            skeletons[i] = sk
            i += 1
        return skeletons

    def _two_view_competitors(
        self, works: list[_Work], pairs: dict[tuple[TrackKey, TrackKey], PairEvidence], flags: list[_Flag]
    ) -> None:
        """2대뿐인 클러스터에 다른 트랙도 기하로 맞으면 제3의 카메라로 가릴 수 없으므로 표시한다 (명세 5.2)."""
        all_keys = [k for w in works for k in w.keys]
        for w in works:
            if len(w.keys) != 2 or w.keys[0][0] == w.keys[1][0]:
                continue
            ka, kb = w.keys
            rivals = []
            for other in all_keys:
                if other in w.keys or other[0] not in (ka[0], kb[0]):
                    continue
                partner = kb if other[0] == ka[0] else ka
                if pairs[_pair_key(other, partner)].feasible:
                    rivals.append(other)
            if rivals:
                flags.append(
                    _Flag(UncertainReason.TWO_VIEW_COMPETITOR, frozenset(w.keys), frozenset([*w.keys, *rivals]))
                )

    # ------------------------------------------------------------------ 결과와 이력

    def _finish(
        self,
        works: list[_Work],
        skeletons: dict[int, SkeletonEstimate],
        prepared: dict[TrackKey, _Prepared],
        flags: list[_Flag],
    ) -> tuple[ViewCluster, ...]:
        order = sorted(range(len(works)), key=lambda i: min(works[i].keys))
        member_sets = [frozenset(works[i].keys) for i in order]
        ids = self._carry_ids(member_sets)
        flags = [f for f in flags if f.blocked is None or not any(f.blocked <= ms for ms in member_sets)]
        clusters = []
        for pos, i in enumerate(order):
            keys = sorted(works[i].keys)
            reasons: list[UncertainReason] = []
            competitors: set[TrackKey] = set()
            for f in flags:
                if f.flagged & member_sets[pos]:
                    if f.reason not in reasons:
                        reasons.append(f.reason)
                    competitors |= f.involved - member_sets[pos]
            clusters.append(
                ViewCluster(
                    association_id=ids[pos],
                    views=tuple(prepared[k].view for k in keys),
                    skeleton=skeletons.get(i),
                    uncertain_reasons=tuple(sorted(reasons)),
                    competitors=tuple(sorted(competitors)),
                    undistorted={k[0]: prepared[k].undistorted for k in keys},
                )
            )
        self._previous = {c.association_id: frozenset(c.keys) for c in clusters}
        return tuple(clusters)

    def _carry_ids(self, member_sets: list[frozenset[TrackKey]]) -> list[str]:
        """직전 시각 클러스터와 겹치는 트랙 수가 최대가 되도록 1:1 로 이어 association_id 를 물려준다."""
        prev_ids = sorted(self._previous)
        ids: list[str | None] = [None] * len(member_sets)
        if prev_ids and member_sets:
            overlap = [[len(ms & self._previous[p]) for p in prev_ids] for ms in member_sets]
            for r, c in linear_sum_assignment([[-float(v) for v in row] for row in overlap]):
                if overlap[r][c] > 0:
                    ids[r] = prev_ids[c]
        out = []
        for i in ids:
            if i is None:
                self._issued += 1
                i = f"xv-{self._issued}"
            out.append(i)
        return out

    def _update_history(
        self,
        t: int,
        clusters: Iterable[ViewCluster],
        pairs: dict[tuple[TrackKey, TrackKey], PairEvidence],
        present: set[TrackKey],
    ) -> None:
        """같은 클러스터인 쌍은 이력을 늘린다. 둘 다 보이는데 갈라진 쌍은 기하가 탈락시켰거나 애매하지 않으면 지운다."""
        together: set[tuple[TrackKey, TrackKey]] = set()
        uncertain: set[TrackKey] = set()
        for c in clusters:
            together.update(itertools.combinations(sorted(c.keys), 2))
            if c.identity_uncertain:
                uncertain.update(c.keys)
        for pair in together:
            age, _ = self._history.get(pair, (0, t))
            self._history[pair] = (age + 1, t)
        for pair in list(self._history):
            if pair in together:
                continue
            a, b = pair
            if a in present and b in present:
                ev = pairs.get(pair)
                if ev is None or not ev.feasible or not (a in uncertain or b in uncertain):
                    del self._history[pair]
            elif t - self._history[pair][1] > self.config.history_ttl_ms:
                del self._history[pair]


class LocalTrackletBuffer:
    """카메라별 `observation_2d` 를 (카메라, 로컬 트랙)마다 모아 공통 시각 t 의 view 로 보간한다 (계산 순서 1단계).

    보간 규칙(촬영 시각 불확실성, 보간 창, 외삽 금지, 지연 도착)은 `geometry.interpolation` 그대로다.
    """

    def __init__(self, config: InterpolationConfig | None = None) -> None:
        self.config = config if config is not None else InterpolationConfig()
        self._buffers: dict[TrackKey, KeypointBuffer] = {}

    @property
    def keys(self) -> tuple[TrackKey, ...]:
        return tuple(sorted(self._buffers))

    def add(self, frame: Observation2DFrame) -> list[FrameDrop]:
        """프레임 하나의 트랙별 keypoint 를 넣는다. 창을 넘겨 늦게 온 것은 넣지 않고 기록을 돌려준다."""
        drops = []
        for det in frame.detections:
            key = (frame.camera_id, det.local_track_id)
            buf = self._buffers.get(key)
            if buf is None:
                buf = self._buffers[key] = KeypointBuffer(frame.camera_id, self.config)
            drop = buf.add(
                KeypointSample(
                    camera_id=frame.camera_id,
                    estimated_capture_time=frame.estimated_capture_time_ms,
                    timestamp_source=frame.timestamp_source,
                    timestamp_uncertainty_ms=frame.timestamp_uncertainty_ms,
                    keypoints=det.detection.keypoints_2d,
                    frame_ref={"stream_epoch": frame.stream_epoch, "frame_sequence": frame.frame_sequence},
                )
            )
            if drop is not None:
                drops.append(drop)
        return drops

    def views_at(self, t: int) -> tuple[list[LocalView], list[FrameDrop]]:
        """공통 시각 t 로 보간한 view 와, 보간하지 못한 트랙의 사유."""
        views, drops = [], []
        for key in sorted(self._buffers):
            view, d = self._buffers[key].interpolate(t)
            drops += d
            if view is not None:
                views.append(LocalView(key[0], key[1], view))
        return views, drops

    def prune(self, t: int) -> None:
        """t 보간에 필요 없는 프레임을 지우고, 마지막 프레임이 보간 창보다 오래된 트랙은 버퍼째 지운다."""
        require_ms("t", t)
        for key in list(self._buffers):
            buf = self._buffers[key]
            if not buf.samples or t - buf.samples[-1].estimated_capture_time > self.config.window_ms:
                del self._buffers[key]
            else:
                buf.prune(t)


def _block_key(a: _Work, b: _Work) -> frozenset[frozenset[TrackKey]]:
    return frozenset([frozenset(a.keys), frozenset(b.keys)])


def _pair_key(a: TrackKey, b: TrackKey) -> tuple[TrackKey, TrackKey]:
    return (a, b) if a <= b else (b, a)


def _two_view_triangulation(pa: _Prepared, pb: _Prepared, common: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """후보 쌍 검증용 2-view 삼각측량. 공통 관절 (n,) 마다 World XYZ (n, 3) 와 재투영 오차 (n,) 를 돌려준다.

    `geometry.triangulation` 과 같은 정규화 좌표 선형 DLT(x·P₃ − P₁, y·P₃ − P₂, P = [R | t], 신뢰도 가중)를 관절 묶음으로
    한 번에 푼다. 후보 쌍은 시각마다 view 수의 제곱만큼 생겨 관절별 가우스-뉴턴까지 하면 10 Hz 를 지키기 어려워
    정밀화는 하지 않는다. 오차는 두 카메라 중 큰 쪽의 왜곡 제거 픽셀 거리이고, 해가 없거나 어느 카메라 뒤면 inf 다.
    이 결과는 연결 판단에만 쓰고 내보내지 않는다(클러스터 관절은 `triangulate_skeleton` 결과).
    """
    return _two_view_triangulation_many([(pa, pb, common)])[0]


def _two_view_triangulation_many(
    jobs: Sequence[tuple[_Prepared, _Prepared, np.ndarray]],
) -> list[tuple[np.ndarray, np.ndarray]]:
    """같은 카메라 쌍(a, b)의 여러 view 쌍을 `_two_view_triangulation` 한 번으로 푼다. 관절마다 독립인 식이라
    쌍들의 공통 관절을 이어 붙여 계산하고 쌍마다 나눠 돌려준다."""
    if not jobs:
        return []
    idx = [np.flatnonzero(common) for _, _, common in jobs]
    sizes = [len(i) for i in idx]
    sides = []
    for side in (0, 1):
        normalized = np.concatenate([job[side].normalized[i] for job, i in zip(jobs, idx)])
        weight = np.concatenate([job[side].undistorted.confidence[i] for job, i in zip(jobs, idx)])
        target = np.concatenate([job[side].undistorted.ideal_pixels[i] for job, i in zip(jobs, idx)])
        sides.append((normalized, weight, target))
    xyz, err = _two_view_arrays(jobs[0][0].camera, jobs[0][1].camera, sides[0], sides[1])
    out = []
    start = 0
    for size in sizes:
        out.append((xyz[start : start + size], err[start : start + size]))
        start += size
    return out


def _two_view_arrays(
    cam_a: RigCamera,
    cam_b: RigCamera,
    obs_a: tuple[np.ndarray, np.ndarray, np.ndarray],
    obs_b: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    """`_two_view_triangulation` 의 계산. 관측 n 개마다 (정규화 좌표 (n, 2), 신뢰도 (n,), 이상적 핀홀 픽셀 (n, 2))를
    두 카메라에서 받아 World XYZ (n, 3) 와 재투영 오차 (n,) 를 돌려준다."""
    preps = [(cam_a, *obs_a), (cam_b, *obs_b)]
    rows = []
    for camera, normalized, w, _ in preps:
        P = np.hstack([camera.R, camera.t[:, None]])
        x, y = normalized[:, 0], normalized[:, 1]
        rows.append(w[:, None] * (x[:, None] * P[2] - P[0]))
        rows.append(w[:, None] * (y[:, None] * P[2] - P[1]))
    A = np.stack(rows, axis=1)  # (n, 4, 4)
    # 유한하지 않은 계수는 SVD 에 넣지 않는다(Linux OpenBLAS 가 ±inf 에서 멈춘다, S15P31A101-245). 그 관절은 해가 없다
    finite = np.isfinite(A).all(axis=(1, 2))
    _, s, vt = np.linalg.svd(np.where(finite[:, None, None], A, 0.0))
    h = vt[:, -1, :]
    ok = finite & (s[:, 0] > 0) & (s[:, -2] > 1e-9 * s[:, 0]) & (np.abs(h[:, 3]) > 1e-12)
    total = len(A)
    xyz = np.full((total, 3), np.nan)
    xyz[ok] = h[ok, :3] / h[ok, 3:4]
    err = np.full(total, np.inf)
    worst = np.zeros(total)
    for camera, _, _, target in preps:
        xc = camera.to_camera(np.nan_to_num(xyz))
        front = xc[:, 2] > 0
        ok &= front
        proj = camera.ideal_pixels(xc[:, :2] / np.where(front, xc[:, 2], 1.0)[:, None])
        worst = np.maximum(worst, np.linalg.norm(proj - target, axis=1))
    err[ok] = worst[ok]
    return xyz, err


def _focal(camera: RigCamera) -> float:
    return float((camera.K[0, 0] + camera.K[1, 1]) / 2.0)


def _floor_ray(camera: RigCamera, normalized: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """임시 바닥 가설: 정규화 좌표의 시선이 높이 h 수평면과 만나는 XY 를 A + h·B 로 나타낸 (A, B).

    시선이 아래를 향하지 않으면(지평선 위) None. 후보 생성용 근사다. World 위치·ground_anchor_xyz·Z 로 쓰지 않는다
    (명세 5.2).
    """
    d = camera.R.T @ np.array([normalized[0], normalized[1], 1.0])
    if d[2] >= -1e-9:
        return None
    c = camera.center
    return c[:2] - c[2] / d[2] * d[:2], d[:2] / d[2]


@dataclass(frozen=True)
class _ViewStack:
    """한 카메라의 view 들(키 순서)을 쌓은 배열. 그 카메라가 든 카메라 쌍의 근거 계산이 같이 쓴다."""

    camera: RigCamera
    keys: list[TrackKey]
    normalized: np.ndarray
    """(N, J, 2)"""
    confidence: np.ndarray
    """(N, J)"""
    ideal: np.ndarray
    """(N, J, 2) 이상적 핀홀 픽셀"""
    floor_a: np.ndarray
    """(N, 2) 임시 바닥 가설 A (없으면 NaN). 후보 거르기 전용"""
    floor_b: np.ndarray
    """(N, 2) 임시 바닥 가설 B (없으면 NaN)"""
    has_floor: np.ndarray
    """(N,)"""

    @classmethod
    def of(cls, views: Sequence[_Prepared]) -> _ViewStack:
        none = np.full(2, np.nan)
        rays = [p.floor_ray for p in views]
        return cls(
            camera=views[0].camera,
            keys=[p.view.key for p in views],
            normalized=np.stack([p.normalized for p in views]),
            confidence=np.stack([p.undistorted.confidence for p in views]),
            ideal=np.stack([p.undistorted.ideal_pixels for p in views]),
            floor_a=np.stack([none if r is None else r[0] for r in rays]),
            floor_b=np.stack([none if r is None else r[1] for r in rays]),
            has_floor=np.array([r is not None for r in rays]),
        )


def _floor_gaps(sa: _ViewStack, sb: _ViewStack, qi: np.ndarray, qk: np.ndarray, max_height: float) -> np.ndarray:
    """view 쌍 (sa 의 qi, sb 의 qk) 마다 `_floor_gap`. 어느 한쪽 가설이 없으면 NaN."""
    dA = sa.floor_a[qi] - sb.floor_a[qk]
    dB = sa.floor_b[qi] - sb.floor_b[qk]
    bb = dB[:, 0] * dB[:, 0] + dB[:, 1] * dB[:, 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        h = -(dA[:, 0] * dB[:, 0] + dA[:, 1] * dB[:, 1]) / bb
        h = np.where(bb < 1e-12, 0.0, np.minimum(np.maximum(h, 0.0), max_height))
        d = dA + h[:, None] * dB
        gap = np.sqrt(d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1])
    return np.where(sa.has_floor[qi] & sb.has_floor[qk], gap, np.nan)


def _floor_gap(ray_a: tuple[np.ndarray, np.ndarray], ray_b: tuple[np.ndarray, np.ndarray], max_height: float) -> float:
    """두 임시 바닥 가설의 XY 거리 중 높이 h ∈ [0, max_height] 에서의 최솟값 (거리 제곱이 h 의 2차식)."""
    dA, dB = ray_a[0] - ray_b[0], ray_a[1] - ray_b[1]
    bb = float(dB @ dB)
    h = 0.0 if bb < 1e-12 else min(max(-float(dA @ dB) / bb, 0.0), max_height)
    return float(np.linalg.norm(dA + h * dB))
