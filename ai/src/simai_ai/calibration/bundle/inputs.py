"""번들 조정 입력과 설정.

- `PersonObservation`: 같은 사람 한 명의 공통 시각 t 카메라별 2D keypoint. 카메라 간 시각 차이는 호출자가
  `geometry.interpolation` 으로 공통 시각에 맞춘 뒤 넣는다(`InterpolatedView.keypoints` 를 그대로 넣을 수 있다).
  같은 사람 대응은 Cross-view 연결 결과 또는 시험용 정답이다. 이 모듈은 대응을 다시 하지 않는다.
- `MeasuredObjectObservation`: 실측 정물 스케일 제약의 자리(-100). 정물 꼭대기·바닥 끝점의 카메라별 2D 관측과
  정물 목록의 치수. 끝점 검출(YOLOE-26)은 -100 범위이며, 여기서는 받은 관측을 제약으로 넣기만 한다.
- 픽셀은 원본 해상도(왜곡 있는 영상) 기준 (u, v) 다. 시각은 UTC int64 ms, 길이는 미터, 각도는 라디안이다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from simai_ai.calibration.bundle.skeleton import SUPPORTED_COUNTS
from simai_ai.calibration.bundle.solver import LENS_FIXED, LENS_MODES, LENS_PER_CAMERA
from simai_ai.tracking.time_model import require_ms

# [u, v, confidence] 또는 None(검출되지 않음)
Keypoint = tuple[float, float, float] | None


@dataclass(frozen=True)
class PersonObservation:
    """사람 한 명의 공통 시각 t 관측. views 는 camera_id → keypoint 목록(COCO17, 선택으로 발 끝점 2개 더)."""

    person_id: str
    t: int
    views: Mapping[str, Sequence[Keypoint]]

    def __post_init__(self) -> None:
        require_ms("t", self.t)
        if not self.person_id:
            raise ValueError("person_id 가 비었다")
        counts = {len(kps) for kps in self.views.values()}
        if len(counts) > 1 or (counts and next(iter(counts)) not in SUPPORTED_COUNTS):
            raise ValueError(f"{self.person_id}@{self.t}: keypoint 수는 카메라마다 같고 17 또는 19 여야 한다: {counts}")


@dataclass(frozen=True)
class MeasuredObjectObservation:
    """실측 정물 하나(-100 자리). endpoints 는 camera_id → ((u_top, v_top), (u_bottom, v_bottom)).

    length_m 은 정물 목록의 꼭대기–바닥 끝점 거리다. 2대 이상에서 두 끝점이 다 보여야 제약으로 쓴다.
    """

    label: str
    length_m: float
    endpoints: Mapping[str, tuple[tuple[float, float], tuple[float, float]]]

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("실측 정물은 정물 목록의 이름(label)이 있어야 한다")
        if not (isinstance(self.length_m, int | float) and math.isfinite(self.length_m) and self.length_m > 0):
            raise ValueError(f"{self.label}: 치수는 유한한 양수여야 한다: {self.length_m}")


@dataclass(frozen=True)
class BundleConfig:
    """번들 조정 설정(초기값). 근거는 각 항목 주석과 `docs/design` 대신 MR 기록에 둔다."""

    min_keypoint_confidence: float = 0.3
    """이 값보다 신뢰도가 낮은 keypoint 는 관측으로 쓰지 않는다 (삼각측량 기본값과 같다)"""
    pixel_sigma_px: float = 1.5
    """재투영 잔차를 나누는 픽셀 표준편차. 강건 손실(soft_l1)의 경계가 이 크기다"""
    init_outlier_px: float = 150.0
    """초기 삼각측량에서 이 값보다 재투영 오차가 큰 관측은 뺀다 (초기 외부 파라미터 오차를 견딜 만큼 크게)"""
    outlier_px: float = 8.0
    """1차 조정 뒤 이 값보다 재투영 오차가 큰 관측은 빼고 다시 조정한다(관절별 잡음 배율을 쓰면 눈 기준 값이고 관절마다 배율을 곱한다)"""
    joint_noise_scaling: bool = True
    """관절별 픽셀 잡음 배율(`skeleton.KEYPOINT_NOISE_SCALE`, COCO κ / 눈 κ)을 pixel_sigma_px·outlier_px 에 곱한다.
    끄면 모든 관절이 같은 잡음이다(-313 이전 동작). 배율은 데이터에서 다시 재지 않는 고정값이라, 보정이 나쁘면 눈·코처럼
    배율 1 인 관절이 여전히 기준을 넘어 이상치 비율 검사(max_outlier_ratio)가 그대로 걸린다"""
    bone_sigma_m: float = 0.02
    """표본마다 뼈 길이가 사람별 길이 변수에서 벗어날 수 있는 표준편차"""
    floor_sigma_m: float = 0.01
    """접지한 발 keypoint 높이가 접지 높이에서 벗어날 수 있는 표준편차"""
    toe_contact_height_m: float = 0.02
    """접지한 발 끝 keypoint 의 바닥 위 높이 (신발 앞코). 발 끝점이 없으면 발목을 쓴다"""
    ankle_contact_height_m: float = 0.08
    """접지한 발목 keypoint 의 바닥 위 높이. 사람마다 다르고 실측하지 않으므로 발 끝점보다 불확실하다"""
    toe_contact_height_uncertainty_m: float = 0.01
    """발 끝 접지 높이 가정의 예상 편차(신발 앞코 두께). 바닥 Z=0 이 이만큼 치우칠 수 있다고 기록한다"""
    ankle_contact_height_uncertainty_m: float = 0.02
    """발목 접지 높이 가정의 예상 편차(사람·신발마다 수 cm). 발목 기준 접지점이 있으면 이 값을 기록한다"""
    stance_max_speed_mps: float = 0.4
    """이웃 표본과의 수평 속도가 이 값 이하인 발만 접지로 본다"""
    stance_window_ms: int = 300
    """접지 속도를 볼 이웃 표본의 최대 시각 차이"""
    gauge_sigma: float = 1e-4
    """게이지 고정(벽 방향 각 rad, 수평 원점 m, 스케일 m) 사전의 표준편차. 사실상 고정"""
    measured_object_sigma_rel: float = 0.005
    """실측 정물 치수 제약의 상대 표준편차 (정물 예상 정확도 약 1% 의 절반)"""
    max_iterations: int = 50
    """한 번의 풀이(라운드)에서 Levenberg–Marquardt 최대 반복 수. 넘기면 수렴하지 않은 것으로 표시한다"""
    max_outlier_ratio: float = 0.10
    """이상치 제거(연쇄로 빠진 점의 관측 포함)가 전체 관측의 이 비율을 넘으면 불확실(TOO_MANY_OUTLIERS)로 표시한다.
    보정이 나빠 정상 관측이 대량으로 기준을 넘는 경우, 남은 관측만으로 잰 재투영 지표가 기준을 통과하는 것을 막는다"""
    rounds: int = 2
    """조정 → 이상치 제거 → 재조정 횟수"""
    min_cameras: int = 3
    """이보다 적은 카메라로 조정하면 불확실로 표시한다 (2대는 내부·외부 파라미터가 사람 관측만으로 잘 정해지지 않는다)"""
    min_contacts: int = 30
    min_contact_minor_spread_m: float = 0.5
    """접지점 수평 분포의 짧은 축 표준편차가 이보다 작으면 바닥 기울기가 한 축으로 정해지지 않는다(직선 보행)"""
    min_image_coverage: float = 0.25
    """카메라 영상을 4x3 칸으로 나눴을 때 관측이 있는 칸 비율이 이보다 작으면 왜곡·초점이 잘 정해지지 않는다"""
    max_height_check_diff_m: float = 0.10
    """CCVS 방식 단일 카메라 높이와 조정 결과 카메라 높이의 차이 한도"""
    min_plane_points: int = 10
    """카메라별 바닥 평면을 맞추는 최소 접지점 수"""
    walkable_area_xy: tuple[tuple[float, float], ...] = field(default=())
    """걸을 수 있는 바닥 다각형(World XY). 주면 바닥 커버리지를 계산하고, 없으면 커버리지를 비운다"""
    coverage_cell_m: float = 0.5
    lens_mode: str = LENS_PER_CAMERA
    """렌즈 변수(-321). per_camera: 카메라마다 f·k1·k2(기본, 이전 동작). shared: 4대가 f(=fx=fy)·k1·k2 하나를 함께 맞추고
    카메라별로는 R·t 만. fixed: 렌즈를 바꾸지 않고 R·t 만. 주점은 어느 모드든 초기값 고정이다"""
    fixed_lens: tuple[float, float, float] | None = None
    """fixed 에서 모든 카메라에 쓸 (f px, k1, k2). 없으면 카메라별 내부 파라미터 초기값 그대로다. fixed 에서만 줄 수 있다"""
    camera_height_min_cameras: int = 2
    """실측 카메라 높이 축척에 필요한 최소 카메라 수 (명세 4.3: 2대 미만이면 쓰지 않는다)"""
    camera_height_max_spread: float = 0.03
    """카메라별 높이 비가 중앙값에서 벗어날 수 있는 최대 비율. 넘는 카메라가 있으면 근거를 쓰지 않는다
    (-277 World 고정과 같은 값과 근거: 줄자·렌즈 중심·바닥 평면 오차 1σ 약 1.1% 의 2~3σ)"""

    def __post_init__(self) -> None:
        for name in ("pixel_sigma_px", "bone_sigma_m", "floor_sigma_m", "gauge_sigma", "measured_object_sigma_rel"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"BundleConfig.{name} 는 유한한 양수여야 한다: {value}")
        if self.rounds < 1:
            raise ValueError(f"rounds 는 1 이상이어야 한다: {self.rounds}")
        if self.min_cameras < 2:
            raise ValueError(f"min_cameras 는 2 이상이어야 한다: {self.min_cameras}")
        if self.lens_mode not in LENS_MODES:
            raise ValueError(f"lens_mode 는 {LENS_MODES} 중 하나다: {self.lens_mode!r}")
        if self.fixed_lens is not None:
            if self.lens_mode != LENS_FIXED:
                raise ValueError(f"fixed_lens 는 lens_mode {LENS_FIXED} 에서만 준다: {self.lens_mode}")
            values = tuple(self.fixed_lens)
            if len(values) != 3 or not all(isinstance(v, int | float) and math.isfinite(v) for v in values):
                raise ValueError(f"fixed_lens 는 유한한 수 3개 (f, k1, k2) 다: {self.fixed_lens}")
            if not values[0] > 0:
                raise ValueError(f"fixed_lens 초점은 양수여야 한다: {values[0]}")
        if self.camera_height_min_cameras < 2:
            raise ValueError(f"camera_height_min_cameras 는 2 이상이어야 한다: {self.camera_height_min_cameras}")
        if not (math.isfinite(self.camera_height_max_spread) and self.camera_height_max_spread > 0):
            raise ValueError(f"camera_height_max_spread 는 유한한 양수여야 한다: {self.camera_height_max_spread}")
