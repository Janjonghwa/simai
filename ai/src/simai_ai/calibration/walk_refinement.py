"""세팅 「카메라 확인」 한 사람 걷기로 외부 파라미터를 다듬는다(S15P31A101-760, 명세 4.3).

16일 로비 세팅에서 배경 BA(Stage A)는 옆 4쌍만 묶고 MapAnything 카메라 중심을 고정한 채 회전만 맞췄다. 먼 벽에 맞춘 회전 보상이
방 가운데 사람에서 어긋나 옆 쌍은 통과하고 대각 쌍이 0% 로 떨어졌다(설계 `docs/design/S15P31A101-760-walk-refine-extrinsics.md`).
카메라 확인이 이미 모은 1인 걷기 view(카메라마다 한 사람만 보인 시각이라 4대 대응이 기하 없이 정해진다)로 카메라 4대의 회전·중심을
함께 다듬고, 다듬기에 쓰지 않은 시간 블록(Holdout)으로만 카메라 확인과 같은 판정을 다시 해서 통과할 때만 채택한다.

- 시간 분할: 사람이 처음 보인 시각부터 Fit 2.4 s → 완충 0.6 s(버림) → Holdout 1.4 s → 완충 0.6 s 를 되풀이한다. 앞뒤 절반 분할은
  걸은 구역이 갈려 Holdout 쌍 표본이 비고, 틱 교대는 이웃 틱 자세가 비슷해 Holdout 이 새지 않은 값이 아니다.
- 다듬기(Fit 만): 카메라마다 6 자유도(R = exp([ω]×)·R0, C = C0 + ΔC)와 시각·관절마다 3D 점. K·왜곡·영상 크기는 고정이라 관측은
  한 번 왜곡을 지운 이상적 핀홀 픽셀(`RigCamera.undistort_many`)로 쓴다. 사전항은 중심 σ(xy 0.08 m, z 0.02 m)·회전 σ 4°.
  관절 σ = 2.5 px × 관절 잡음 배율(`KEYPOINT_NOISE_SCALE`, 평가 관절 최솟값으로 나눔) / sqrt(score). 강건 손실은 2단계
  (Cauchy 6 px 로 LM 15회 → 18 px 넘는 관측 버림 → Huber 5 px 로 15회). 풀이는 점 블록(3×3)을 소거한 Schur 보수 LM 이다.
- 한 대만 크게 틀어졌으면(그 카메라가 낀 쌍 모두 통과율 < 0.25, 나머지 세 대 삼각형 ≥ 0.70) 세 대로 삼각측량한 관절로 그 카메라
  회전을 먼저 맞춘다(`recalibration.realign_rotation`). 그 카메라 회전 상한은 따로 둔다.
- 게이지: 수렴 뒤 Stage A 카메라 중심 대비 수평 강체(Δx, Δy, Δψ)와 평균 Δz 를 빼서 World 바닥 Z=0·축·world_anchor 를 지킨다.
- 채택(Holdout 만): 모든 쌍 PASS(표본 ≥ 15, 통과율 ≥ 0.75, 잔차 중앙값 ≤ 8 px), 모든 카메라 한 카메라 제외 재투영 PASS(표본 ≥ 20
  = 관제 감시 창 하한, 중앙값 ≤ 카메라 확인 상한), 모든 카메라 기준선 있음, 물리 상한(중심 수평 ≤ 0.20 m·수직 ≤ 0.06 m, 입력 높이 대비 ≤ 0.10 m, 회전 ≤ 8°,
  부트스트랩 카메라 ≤ 15°), Holdout 최저 쌍 통과율이 Stage A 보다 올랐음(Stage A 가 이미 1.0 이면 최대 쌍 잔차·최대 LOO 가 모두
  줄었음). 하나라도 어기면 채택하지 않는다(새 보정 문서를 내지 않는다).

세팅 보정 후보를 만드는 오프라인 계산이다. Tier 0 위치·관제 판정에 넣지 않고, 입력 보정 문서를 바꾸지 않으며(새 버전 문서를 만든다),
현재 시각을 읽지 않는다(생성 시각은 입력 문서와 걷기 view 시각으로 정한다).
"""

from __future__ import annotations

import copy
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from simai_ai.calibration.bundle.skeleton import KEYPOINT_NOISE_SCALE
from simai_ai.calibration.camera_check import (
    DEFAULT_CONFIG,
    FAIL,
    PASS,
    PASSED,
    CameraCheckConfig,
    CameraCheckEvaluator,
    evaluate_views,
    pair_key,
)
from simai_ai.calibration.document import content_hash
from simai_ai.calibration.monitor import BaselineRecord, MonitorConfig
from simai_ai.geometry.rig import RigCamera
from simai_ai.tracking.cross_view import LocalView

# COCO17 몸통·사지 관절(어깨·팔꿈치·손목·엉덩이·무릎·발목). 얼굴 관절은 시점마다 가려지고 몸 크기 대비 작아 뺀다
EVAL_JOINTS: tuple[int, ...] = tuple(range(5, 17))
_JOINT_SCALE = {j: KEYPOINT_NOISE_SCALE[j] / min(KEYPOINT_NOISE_SCALE[k] for k in EVAL_JOINTS) for j in EVAL_JOINTS}

# 채택 거절 사유(워커 로그·기록에 남긴다)
FEW_FIT_POINTS = "FEW_FIT_POINTS"
FEW_HOLDOUT = "FEW_HOLDOUT"
SOLVER_FAILED = "SOLVER_FAILED"
HOLDOUT_PAIR_FAILED = "HOLDOUT_PAIR_FAILED"
HOLDOUT_CAMERA_FAILED = "HOLDOUT_CAMERA_FAILED"
BASELINE_MISSING = "BASELINE_MISSING"
CENTER_SHIFT_XY = "CENTER_SHIFT_XY"
CENTER_SHIFT_Z = "CENTER_SHIFT_Z"
HEIGHT_DIFF = "HEIGHT_DIFF"
ROTATION_CHANGE = "ROTATION_CHANGE"
NO_IMPROVEMENT = "NO_IMPROVEMENT"
SCHEMA_INVALID = "SCHEMA_INVALID"

_LOSSES = ("cauchy", "soft_l1", "huber")


@dataclass(frozen=True)
class WalkRefinementConfig:
    """걷기 다듬기 상수. 근거는 docs/design/S15P31A101-760-walk-refine-extrinsics.md 「정한 수치」."""

    fit_block_ms: int = 2_400
    gap_ms: int = 600
    """Fit·Holdout 사이 완충(버림). 5 Hz 로 3틱, 걸음 0.7~0.9 m 라 이웃 틱 자세·검출 잡음 상관이 끊긴다"""
    holdout_block_ms: int = 1_400
    """주기 5.0 s 에 Fit 63% : Holdout 37%(쓰는 틱 기준). 30 s 걷기면 Holdout 약 8.4 s(약 40틱)"""
    min_keypoint_score: float = 0.5
    pixel_sigma_px: float = 2.5
    """-694 로비 잡음 바닥 보정의 쌍 잔차 1.3~3.4 px"""
    min_views_per_point: int = 3
    min_three_view_points: int = 150
    """3대 이상에서 보인 점이 이보다 적으면 2대 점도 쓴다"""
    min_fit_points: int = 240
    """Fit 점(시각·관절) 하한. 한 사람 12관절 기준 20시각(5 Hz·Fit 48% 면 약 8 s 걷기)"""
    min_camera_observations: int = 120
    """카메라마다 Fit 관측 하한(관절 수, 10시각 분). 모자라면 그 카메라 6 자유도가 사전항으로만 정해진다"""
    center_sigma_xy_m: float = 0.08
    """MapAnything 수평 중심 오차(15~25 cm)는 풀어 주되 축척·원점 표류는 막는다"""
    center_sigma_z_m: float = 0.02
    """관리자가 확인한 카메라 높이를 지킨다"""
    rotation_sigma_deg: float = 4.0
    stage1_loss: str = "cauchy"
    stage1_scale: float = 2.4
    """σ 단위(2.5 px × 2.4 = 6 px). 대각 30~60 px 초기 오차가 기울기를 망치지 않게 한다"""
    stage1_iterations: int = 15
    outlier_px: float = 18.0
    """1단계 뒤 이 픽셀 오차를 넘는 관측(반사·가림 오검출)은 버린다"""
    stage2_loss: str = "huber"
    stage2_scale: float = 2.0
    stage2_iterations: int = 15
    bootstrap_max_pair_rate: float = 0.25
    bootstrap_min_triangle_rate: float = 0.70
    holdout_min_duration_ms: int = 20_000
    """Holdout 시각의 첫~마지막 간격 하한. 카메라 확인 시도는 30 s 이상 걸어야 끝나고 첫 Holdout 블록은 3 s 뒤에 시작한다"""
    holdout_min_pair_samples: int = 15
    holdout_min_camera_views: int = 10
    holdout_min_loo_samples: int = 20
    """관제 감시 창 표본 하한(MonitorConfig.min_window_samples)과 같다. 기준선(baseline_record)이 이 하한으로 카메라를 넣으므로 낮추면
    PASS 인데 기준선이 빠진 카메라가 생긴다. 시도는 이 몫을 채우도록 4대 동시 관측을 기다린다(CameraCheckConfig.min_four_view_ticks)"""
    max_center_shift_xy_m: float = 0.20
    max_center_shift_z_m: float = 0.06
    max_height_diff_m: float = 0.10
    """입력 카메라 높이 대비(품질 게이트 높이 차 절대 상한 10 cm 와 같다)"""
    max_rotation_change_deg: float = 8.0
    max_bootstrap_rotation_change_deg: float = 15.0

    def __post_init__(self) -> None:
        for name in ("fit_block_ms", "holdout_block_ms", "min_views_per_point", "min_fit_points",
                     "holdout_min_duration_ms", "holdout_min_pair_samples", "holdout_min_camera_views",
                     "holdout_min_loo_samples"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"WalkRefinementConfig.{name} 는 1 이상의 정수여야 한다: {value!r}")
        if self.gap_ms < 0 or self.min_views_per_point < 2:
            raise ValueError("gap_ms 는 0 이상, min_views_per_point 는 2 이상이다")
        if self.stage1_loss not in _LOSSES or self.stage2_loss not in _LOSSES:
            raise ValueError(f"손실은 {_LOSSES} 중 하나다")
        for name in ("pixel_sigma_px", "center_sigma_xy_m", "center_sigma_z_m", "rotation_sigma_deg", "stage1_scale",
                     "stage2_scale", "outlier_px", "max_center_shift_xy_m", "max_center_shift_z_m",
                     "max_height_diff_m", "max_rotation_change_deg", "max_bootstrap_rotation_change_deg"):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(f"WalkRefinementConfig.{name} 는 유한한 양수다: {value!r}")

    @property
    def period_ms(self) -> int:
        return self.fit_block_ms + self.gap_ms + self.holdout_block_ms + self.gap_ms

    def holdout_check_config(self, base: CameraCheckConfig) -> CameraCheckConfig:
        """Holdout 평가 기준: 카메라 확인 기준에서 표본 하한만 Holdout 크기에 맞춘다(통과율·잔차·LOO 상한은 그대로)."""
        min_duration = min(base.min_duration_ms, self.holdout_min_duration_ms)
        return replace(base, min_duration_ms=min_duration, max_duration_ms=max(base.max_duration_ms, min_duration),
                       min_pair_samples=self.holdout_min_pair_samples, min_camera_views=self.holdout_min_camera_views,
                       min_loo_samples=self.holdout_min_loo_samples, min_four_view_ticks=0)


DEFAULT_WALK_CONFIG = WalkRefinementConfig()
_DEFAULT_MONITOR = MonitorConfig()

Records = list[tuple[int, list[LocalView]]]


@dataclass(frozen=True)
class WalkRefinementResult:
    """다듬기 한 번의 결과. 채택이면 calibration_doc(새 버전)·summary(Holdout 판정, PASSED)·baseline 이 그 새 버전이다."""

    adopted: bool
    reasons: tuple[str, ...]
    calibration_doc: dict[str, Any] | None
    summary: dict[str, Any] | None
    baseline: BaselineRecord | None
    record: dict[str, Any] = field(default_factory=dict)
    """전/후 쌍별 통과율·잔차, 중심 이동량, 회전 변화, 부트스트랩, 풀이 기록(JSON 으로 쓸 수 있다)"""


# --------------------------------------------------------------------------- 시간 분할


def ordered_records(records: Iterable[tuple[int, Sequence[LocalView]]]) -> Records:
    """시각 순서로 정렬하고 같은 시각은 처음 것만 남긴다(평가기는 같거나 이른 시각을 버린다)."""
    out: Records = []
    seen: set[int] = set()
    for t, views in sorted(((int(t), list(v)) for t, v in records), key=lambda item: item[0]):
        if t not in seen:
            seen.add(t)
            out.append((t, views))
    return out


def split_walk_records(records: Iterable[tuple[int, Sequence[LocalView]]],
                       config: WalkRefinementConfig = DEFAULT_WALK_CONFIG) -> tuple[Records, Records]:
    """교대 시간 블록 (Fit, Holdout). 사람이 처음 보인 시각을 0 으로 Fit → 완충 → Holdout → 완충. 완충 시각은 어느 쪽에도 넣지 않는다."""
    recs = ordered_records(records)
    t0 = next((t for t, views in recs if views), None)
    if t0 is None:
        return [], []
    fit: Records = []
    holdout: Records = []
    hold_start = config.fit_block_ms + config.gap_ms
    for t, views in recs:
        if t < t0:
            continue
        phase = (t - t0) % config.period_ms
        if phase < config.fit_block_ms:
            fit.append((t, views))
        elif hold_start <= phase < hold_start + config.holdout_block_ms:
            holdout.append((t, views))
    return fit, holdout


# --------------------------------------------------------------------------- Fit 관측


@dataclass(frozen=True)
class _Observations:
    uv: np.ndarray  # (P, N, 2) 이상적 핀홀 픽셀(없으면 0)
    mask: np.ndarray  # (P, N) bool
    sigma: np.ndarray  # (P, N) px
    three_view_points: int
    two_view_points: int


def _single_views(records: Records, cam_ids: Sequence[str]) -> list[tuple[int, dict[str, Sequence[Any]]]]:
    """시각마다 한 사람만 보인 카메라의 keypoint (카메라 확인과 같은 기준: 그 카메라 view 가 하나)."""
    known = set(cam_ids)
    out = []
    for t, views in records:
        by_cam: dict[str, list[LocalView]] = {}
        for v in views:
            if v.camera_id in known and v.t == t:
                by_cam.setdefault(v.camera_id, []).append(v)
        single = {cid: vs[0].view.keypoints for cid, vs in by_cam.items() if len(vs) == 1}
        if single:
            out.append((t, single))
    return out


def _fit_observations(singles: list[tuple[int, dict[str, Sequence[Any]]]], cams: Sequence[RigCamera],
                      config: WalkRefinementConfig) -> _Observations:
    n = len(cams)
    found: dict[tuple[int, int], dict[int, tuple[float, float, float]]] = {}
    for ci, cam in enumerate(cams):
        items = [(i, views[cam.camera_id]) for i, (_, views) in enumerate(singles)
                 if cam.camera_id in views and len(views[cam.camera_id]) > max(EVAL_JOINTS)]
        if not items:
            continue
        undistorted = cam.undistort_many([kps for _, kps in items])
        for (i, _), und in zip(items, undistorted, strict=True):
            usable = und.usable(config.min_keypoint_score)
            for j in EVAL_JOINTS:
                if usable[j]:
                    u, v = und.ideal_pixels[j]
                    found.setdefault((i, j), {})[ci] = (float(u), float(v), float(und.confidence[j]))
    three = [k for k, obs in found.items() if len(obs) >= config.min_views_per_point]
    two = [k for k, obs in found.items() if len(obs) == 2]
    keys = three + (two if len(three) < config.min_three_view_points else [])
    keys.sort()
    uv = np.zeros((len(keys), n, 2))
    mask = np.zeros((len(keys), n), dtype=bool)
    sigma = np.ones((len(keys), n))
    for p, key in enumerate(keys):
        for ci, (u, v, score) in found[key].items():
            uv[p, ci] = (u, v)
            mask[p, ci] = True
            sigma[p, ci] = config.pixel_sigma_px * _JOINT_SCALE[key[1]] / math.sqrt(max(score, 1e-6))
    used_two = len(keys) - len(three)
    return _Observations(uv, mask, sigma, len(three), used_two)


# --------------------------------------------------------------------------- 풀이(점 블록 Schur LM)


def _skew(v: np.ndarray) -> np.ndarray:
    out = np.zeros(v.shape[:-1] + (3, 3))
    out[..., 0, 1], out[..., 0, 2] = -v[..., 2], v[..., 1]
    out[..., 1, 0], out[..., 1, 2] = v[..., 2], -v[..., 0]
    out[..., 2, 0], out[..., 2, 1] = -v[..., 1], v[..., 0]
    return out


def _exp_so3(omega: np.ndarray) -> np.ndarray:
    theta = np.linalg.norm(omega, axis=-1)
    W = _skew(omega)
    small = theta < 1e-10
    t = np.where(small, 1.0, theta)
    a = np.where(small, 1.0, np.sin(t) / t)
    b = np.where(small, 0.5, (1 - np.cos(t)) / t**2)
    return np.eye(3) + a[..., None, None] * W + b[..., None, None] * (W @ W)


def _left_jacobian(omega: np.ndarray) -> np.ndarray:
    """exp(ω + δ) ≈ exp(J_l(ω) δ) · exp(ω)."""
    theta = np.linalg.norm(omega, axis=-1)
    W = _skew(omega)
    small = theta < 1e-8
    t = np.where(small, 1.0, theta)
    a = np.where(small, 0.5, (1 - np.cos(t)) / t**2)
    b = np.where(small, 1.0 / 6.0, (t - np.sin(t)) / t**3)
    return np.eye(3) + a[..., None, None] * W + b[..., None, None] * (W @ W)


def _rho_weight(e: np.ndarray, loss: str, c: float) -> tuple[np.ndarray, np.ndarray]:
    """정규화 잔차 크기 e 의 강건 비용 ρ(e) 와 반복 재가중 가중치 w = ρ'(e)/e. 작은 e 에서 ρ ≈ e²/2."""
    s = (e / c) ** 2
    if loss == "cauchy":
        return 0.5 * c * c * np.log1p(s), 1.0 / (1.0 + s)
    if loss == "soft_l1":
        root = np.sqrt(1.0 + s)
        return c * c * (root - 1.0), 1.0 / root
    inside = e <= c
    return np.where(inside, 0.5 * e * e, c * e - 0.5 * c * c), np.where(inside, 1.0, c / np.maximum(e, 1e-12))


@dataclass
class _State:
    omega: np.ndarray  # (N, 3)
    dC: np.ndarray  # (N, 3)
    X: np.ndarray  # (P, 3)


class _Solver:
    """카메라 N 대 × 6 자유도 + 점 P 개. 점마다 관측 카메라 칸(N)이 고정이라 Schur 보수를 밀집 칸으로 계산한다."""

    def __init__(self, K: np.ndarray, R0: np.ndarray, C0: np.ndarray, obs: _Observations,
                 config: WalkRefinementConfig) -> None:
        self.K, self.R0, self.C0 = K, R0, C0
        self.uv, self.mask, self.sigma = obs.uv, obs.mask.copy(), obs.sigma
        self.n = len(K)
        rot = math.radians(config.rotation_sigma_deg)
        self.prior_sigma = np.array([rot, rot, rot, config.center_sigma_xy_m, config.center_sigma_xy_m,
                                     config.center_sigma_z_m])

    def poses(self, s: _State) -> tuple[np.ndarray, np.ndarray]:
        return _exp_so3(s.omega) @ self.R0, self.C0 + s.dC

    def _project(self, s: _State) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        R, C = self.poses(s)
        Xc = np.einsum("nij,pnj->pni", R, s.X[:, None, :] - C[None])
        z = Xc[..., 2]
        safe = np.where(np.abs(z) < 1e-9, 1e-9, z)
        proj = np.einsum("nij,pnj->pni", self.K, Xc)
        return proj[..., :2] / safe[..., None], Xc, z, R

    def pixel_errors(self, s: _State) -> np.ndarray:
        """(P, N) 픽셀 재투영 오차. 관측 없는 칸·카메라 뒤는 inf."""
        uv, _, z, _ = self._project(s)
        err = np.linalg.norm(uv - self.uv, axis=-1)
        return np.where(self.mask & (z > 1e-3), err, np.inf)

    def cost(self, s: _State, loss: str, c: float) -> float:
        uv, _, z, _ = self._project(s)
        if np.any(self.mask & (z <= 1e-3)):
            return math.inf
        e = np.linalg.norm((uv - self.uv) / self.sigma[..., None], axis=-1)
        rho, _ = _rho_weight(e, loss, c)
        prior = np.concatenate([s.omega, s.dC], axis=1) / self.prior_sigma
        return float(np.sum(rho[self.mask]) + 0.5 * np.sum(prior**2))

    def drop_points(self, keep: np.ndarray, s: _State) -> _State:
        self.uv, self.mask, self.sigma = self.uv[keep], self.mask[keep], self.sigma[keep]
        return _State(s.omega, s.dC, s.X[keep])

    def solve(self, s: _State, loss: str, c: float, iterations: int) -> tuple[_State, dict[str, Any]]:
        n = self.n
        cost = self.cost(s, loss, c)
        start_cost = cost
        lam = 1e-3
        steps = 0
        for _ in range(iterations):
            uv, Xc, z, R = self._project(s)
            sig = self.sigma[..., None]
            r = (uv - self.uv) / sig  # (P, N, 2)
            e = np.linalg.norm(r, axis=-1)
            _, w = _rho_weight(e, loss, c)
            wt = np.where(self.mask, w, 0.0)
            K = self.K
            row0 = K[None, :, 0, :] - uv[..., 0:1] * np.array([0.0, 0.0, 1.0])
            row1 = K[None, :, 1, :] - uv[..., 1:2] * np.array([0.0, 0.0, 1.0])
            A = np.stack([row0, row1], axis=2) / np.where(np.abs(z) < 1e-9, 1e-9, z)[..., None, None]  # (P,N,2,3)
            Jp = np.einsum("pnij,njk->pnik", A, R) / sig[..., None]
            Jl = _left_jacobian(s.omega)  # (N, 3, 3)
            Jw = np.einsum("pnij,pnjk,nkl->pnil", A, -_skew(Xc), Jl) / sig[..., None]
            Jc = np.concatenate([Jw, -Jp], axis=-1)  # (P, N, 2, 6)
            U = np.einsum("pn,pnki,pnkj->nij", wt, Jc, Jc)
            V = np.einsum("pn,pnki,pnkj->pij", wt, Jp, Jp)
            Wm = np.einsum("pn,pnki,pnkj->pnij", wt, Jc, Jp)
            gc = np.einsum("pn,pnki,pnk->ni", wt, Jc, r)
            gp = np.einsum("pn,pnki,pnk->pi", wt, Jp, r)
            prior = np.concatenate([s.omega, s.dC], axis=1) / self.prior_sigma
            U = U + np.eye(6)[None] / (self.prior_sigma**2)[None, :, None]
            gc = gc + prior / self.prior_sigma
            accepted = False
            while lam <= 1e8:
                Ud = U + lam * np.einsum("nii->ni", U)[..., None] * np.eye(6)[None]
                Vdiag = np.einsum("pii->pi", V)
                Vd = V + (lam * Vdiag + 1e-9)[..., None] * np.eye(3)[None]
                try:
                    Vinv = np.linalg.inv(Vd)
                except np.linalg.LinAlgError:
                    lam *= 10
                    continue
                Y = np.einsum("pnim,pmk->pnik", Wm, Vinv)
                S = -np.einsum("paim,pbjm->aibj", Y, Wm)
                for i in range(n):
                    S[i, :, i, :] += Ud[i]
                rhs = -gc + np.einsum("pnim,pm->ni", Y, gp)
                try:
                    dc = np.linalg.solve(S.reshape(6 * n, 6 * n), rhs.reshape(-1)).reshape(n, 6)
                except np.linalg.LinAlgError:
                    lam *= 10
                    continue
                dp = np.einsum("pij,pj->pi", Vinv, -gp - np.einsum("pnim,ni->pm", Wm, dc))
                trial = _State(s.omega + dc[:, :3], s.dC + dc[:, 3:], s.X + dp)
                new_cost = self.cost(trial, loss, c)
                if math.isfinite(new_cost) and new_cost < cost:
                    accepted = True
                    break
                lam *= 10
            if not accepted:
                break
            steps += 1
            decrease = (cost - new_cost) / max(cost, 1e-12)
            s, cost = trial, new_cost
            lam = max(lam / 10, 1e-9)
            if decrease < 1e-7:
                break
        return s, {"loss": loss, "scale": c, "start_cost": round(start_cost, 4), "end_cost": round(cost, 4),
                   "steps": steps}


def _triangulate(K: np.ndarray, R: np.ndarray, C: np.ndarray, uv: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """관측 카메라 광선의 DLT (P, 3). 풀리지 않으면 NaN."""
    Kinv = np.linalg.inv(K)
    hom = np.concatenate([uv, np.ones(uv.shape[:-1] + (1,))], axis=-1)
    xn = np.einsum("nij,pnj->pni", Kinv, hom)
    xn = xn[..., :2] / xn[..., 2:3]
    Pm = np.concatenate([R, -np.einsum("nij,nj->ni", R, C)[..., None]], axis=-1)  # (N, 3, 4)
    rows0 = xn[..., 0:1] * Pm[None, :, 2, :] - Pm[None, :, 0, :]
    rows1 = xn[..., 1:2] * Pm[None, :, 2, :] - Pm[None, :, 1, :]
    A = np.concatenate([rows0, rows1], axis=1) * np.concatenate([mask, mask], axis=1)[..., None]
    _, _, vt = np.linalg.svd(A)
    h = vt[:, -1, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        X = h[:, :3] / h[:, 3:4]
    X[~np.isfinite(X).all(axis=1)] = np.nan
    return X


# --------------------------------------------------------------------------- 게이지·지표


def _remove_planar_gauge(R: np.ndarray, C: np.ndarray, C_ref: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """C 를 C_ref 에 맞추는 수평 강체(Z 축 회전 ψ + 수평 이동)와 평균 Δz 를 적용한다(축척은 그대로). 새 World 점 X' = Rz·X + d."""
    a, b = C[:, :2] - C[:, :2].mean(axis=0), C_ref[:, :2] - C_ref[:, :2].mean(axis=0)
    psi = math.atan2(float(np.sum(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])), float(np.sum(a * b)))
    Rz = np.array([[math.cos(psi), -math.sin(psi), 0.0], [math.sin(psi), math.cos(psi), 0.0], [0.0, 0.0, 1.0]])
    d = np.zeros(3)
    d[:2] = C_ref[:, :2].mean(axis=0) - (Rz[:2, :2] @ C[:, :2].mean(axis=0))
    d[2] = float(np.mean(C_ref[:, 2] - C[:, 2]))
    return R @ Rz.T, C @ Rz.T + d, {"yaw_deg": round(math.degrees(psi), 4), "dx_m": round(float(d[0]), 4),
                                    "dy_m": round(float(d[1]), 4), "dz_m": round(float(d[2]), 4)}


def _rotation_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    return math.degrees(math.acos(max(-1.0, min(1.0, (float(np.trace(Ra @ Rb.T)) - 1.0) / 2.0))))


def _pairs_view(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {k: {"samples": p["samples"], "pass_rate": p["pass_rate"], "residual_median_px": p["residual_median_px"],
                "verdict": p["verdict"]} for k, p in summary.get("pairs", {}).items()}


def _cameras_view(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {c: {"loo_samples": v["loo_samples"], "loo_median_px": v["loo_median_px"], "verdict": v["verdict"]}
            for c, v in summary.get("cameras", {}).items()}


def _min_rate(summary: Mapping[str, Any]) -> float | None:
    rates = [p["pass_rate"] for p in summary.get("pairs", {}).values() if p["pass_rate"] is not None]
    return min(rates) if rates else None


def _max_of(values: Iterable[Any]) -> float | None:
    finite = [float(v) for v in values if v is not None]
    return max(finite) if finite else None


def _improved(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Holdout 최저 쌍 통과율이 올랐는가. Stage A 가 이미 모두 1.0 이면(올라갈 수 없음) 최대 쌍 잔차와 최대 LOO 가 모두 줄었는가."""
    b, a = _min_rate(before), _min_rate(after)
    if a is None:
        return False
    if b is None or a > b + 1e-9:
        return True
    if b < 1.0 - 1e-9:
        return False
    pairs_b = _max_of(p["residual_median_px"] for p in before["pairs"].values())
    pairs_a = _max_of(p["residual_median_px"] for p in after["pairs"].values())
    loo_b = _max_of(c["loo_median_px"] for c in before["cameras"].values())
    loo_a = _max_of(c["loo_median_px"] for c in after["cameras"].values())
    if None in (pairs_b, pairs_a, loo_b, loo_a):
        return False
    return bool(pairs_a < pairs_b and loo_a < loo_b)  # type: ignore[operator]


def _fit_pair_rates(doc: Mapping[str, Any], fit: Records, check_config: CameraCheckConfig,
                    monitor_config: MonitorConfig) -> dict[str, float | None]:
    evaluator = CameraCheckEvaluator(doc, check_config, monitor_config=monitor_config)
    for t, views in fit:
        evaluator.observe(t, views)
    return {k: p["pass_rate"] for k, p in evaluator.summary()["pairs"].items()}


def _bootstrap_camera(rates: Mapping[str, float | None], cam_ids: Sequence[str],
                      config: WalkRefinementConfig) -> str | None:
    """한 대만 크게 틀어졌는가: 그 카메라가 낀 쌍 모두 < bootstrap_max_pair_rate, 나머지 대끼리 쌍 모두 ≥ bootstrap_min_triangle_rate."""
    hits = []
    for cid in cam_ids:
        own = [rates.get(pair_key(cid, o)) for o in cam_ids if o != cid]
        rest = [rates.get(pair_key(a, b)) for i, a in enumerate(cam_ids) for b in cam_ids[i + 1:] if cid not in (a, b)]
        if any(r is None for r in own + rest):
            continue
        if all(r < config.bootstrap_max_pair_rate for r in own) and all(  # type: ignore[operator]
                r >= config.bootstrap_min_triangle_rate for r in rest):  # type: ignore[operator]
            hits.append(cid)
    return hits[0] if len(hits) == 1 else None


def _bootstrap_rotation(doc: Mapping[str, Any], cid: str, singles: list[tuple[int, dict[str, Sequence[Any]]]],
                        config: WalkRefinementConfig) -> np.ndarray | None:
    from simai_ai.calibration.bundle.inputs import PersonObservation
    from simai_ai.calibration.recalibration import realign_rotation

    observations = []
    for t, views in singles:
        if cid not in views or len(views) < 3:
            continue
        try:
            observations.append(PersonObservation("walk", t, dict(views)))
        except ValueError:
            continue
    fixed = realign_rotation(doc, cid, observations, min_confidence=config.min_keypoint_score)
    return None if fixed is None else np.asarray(fixed[0], dtype=float)


# --------------------------------------------------------------------------- 진입점


def refine_extrinsics_from_walk(
    calibration: Mapping[str, Any],
    records: Iterable[tuple[int, Sequence[LocalView]]],
    *,
    camera_heights_m: Mapping[str, float],
    check_config: CameraCheckConfig = DEFAULT_CONFIG,
    required_pairs: Iterable[tuple[str, str]] = (),
    blocking_pairs: Iterable[tuple[str, str]] = (),
    monitor_config: MonitorConfig = _DEFAULT_MONITOR,
    config: WalkRefinementConfig = DEFAULT_WALK_CONFIG,
) -> WalkRefinementResult:
    """카메라 확인이 실패한 보정(Stage A)과 그 시도의 걷기 view 로 외부 파라미터를 한 번 다듬고 Holdout 으로 채택을 정한다.

    채택이면 새 calibration_version 문서(입력 문서를 복사해 cameras 의 R·t·camera_height_m·reprojection_error_px 만 바꾸고 content_hash
    를 다시 계산), 그 버전의 Holdout 카메라 확인 요약(PASSED)과 기준선을 돌려준다. 거절이면 문서·요약·기준선 없이 사유만 돌려준다.
    계보(previous_calibration_version)·세팅 출처는 넣지 않는다(발행 주입점이 세팅 시작 때 활성 보정으로 넣는다)."""
    started = time.perf_counter()
    required = [tuple(p) for p in required_pairs]
    blocking = [tuple(p) for p in blocking_pairs]
    base_version = str(calibration["calibration_version"])
    record: dict[str, Any] = {"base_calibration_version": base_version, "adopted": False}

    def reject(*reasons: str) -> WalkRefinementResult:
        record["reasons"] = list(reasons)
        record["seconds"] = round(time.perf_counter() - started, 3)
        return WalkRefinementResult(False, tuple(reasons), None, None, None, record)

    cams_doc = list(calibration["cameras"])
    cam_ids = [str(c["camera_id"]) for c in cams_doc]
    rigs = [RigCamera.from_calibration(c) for c in cams_doc]
    K = np.array([r.K for r in rigs])
    R_a = np.array([r.R for r in rigs])
    C_a = np.array([r.center for r in rigs])

    fit, holdout = split_walk_records(records, config)
    record["split"] = {"fit_ticks": len(fit), "holdout_ticks": len(holdout), "fit_block_ms": config.fit_block_ms,
                       "gap_ms": config.gap_ms, "holdout_block_ms": config.holdout_block_ms}
    hold_cfg = config.holdout_check_config(check_config)
    if hold_cfg.min_loo_samples < monitor_config.min_window_samples:
        # 카메라 PASS 와 기준선 포함이 같은 표본 하한을 쓰게 한다(기준선은 max(min_loo_samples, min_window_samples) 로 넣는다)
        hold_cfg = replace(hold_cfg, min_loo_samples=monitor_config.min_window_samples)
    before = evaluate_views(holdout, calibration, hold_cfg, required_pairs=required, blocking_pairs=blocking,
                            monitor_config=monitor_config).summary
    record["before"] = {"status": before["status"], "pairs": _pairs_view(before), "cameras": _cameras_view(before)}

    singles = _single_views(fit, cam_ids)
    obs = _fit_observations(singles, rigs, config)
    per_camera = obs.mask.sum(axis=0)
    record["fit_points"] = {"three_view": obs.three_view_points, "two_view": obs.two_view_points,
                            "per_camera_observations": {cid: int(per_camera[i]) for i, cid in enumerate(cam_ids)}}
    if len(obs.mask) < config.min_fit_points or int(per_camera.min()) < config.min_camera_observations:
        return reject(FEW_FIT_POINTS)
    if not holdout:
        return reject(FEW_HOLDOUT)

    # 한 대만 크게 틀어졌으면 세 대 삼각측량 관절로 그 카메라 회전부터 맞춘다
    R0 = R_a.copy()
    boot_cid = _bootstrap_camera(_fit_pair_rates(calibration, fit, check_config, monitor_config), cam_ids, config)
    record["bootstrap"] = {"camera_id": boot_cid, "applied": False}
    if boot_cid is not None:
        R_boot = _bootstrap_rotation(calibration, boot_cid, singles, config)
        if R_boot is not None:
            idx = cam_ids.index(boot_cid)
            record["bootstrap"].update(applied=True, rotation_deg=round(_rotation_deg(R_boot, R_a[idx]), 4))
            R0[idx] = R_boot

    solver = _Solver(K, R0, C_a, obs, config)
    X0 = _triangulate(K, R0, C_a, obs.uv, obs.mask)
    state = _State(np.zeros((len(K), 3)), np.zeros((len(K), 3)), np.nan_to_num(X0))
    errors = solver.pixel_errors(state)
    solver.mask &= np.isfinite(errors) & np.isfinite(X0).all(axis=1)[:, None]
    state = solver.drop_points(solver.mask.sum(axis=1) >= 2, state)
    stage1_state, stage1 = solver.solve(state, config.stage1_loss, config.stage1_scale, config.stage1_iterations)
    errors = solver.pixel_errors(stage1_state)
    before_mask = int(solver.mask.sum())
    solver.mask &= errors <= config.outlier_px
    stage1_state = solver.drop_points(solver.mask.sum(axis=1) >= 2, stage1_state)
    final_state, stage2 = solver.solve(stage1_state, config.stage2_loss, config.stage2_scale, config.stage2_iterations)
    record["solver"] = {"stage1": stage1, "stage2": stage2, "masked_observations": before_mask - int(solver.mask.sum()),
                        "points": len(final_state.X)}
    R_b, C_b = solver.poses(final_state)
    if not (np.isfinite(R_b).all() and np.isfinite(C_b).all()) or len(final_state.X) < config.min_fit_points:
        return reject(SOLVER_FAILED)
    R_new, C_new, gauge = _remove_planar_gauge(R_b, C_b, C_a)
    record["gauge"] = gauge

    reasons: list[str] = []
    cameras_rec: dict[str, Any] = {}
    for i, cid in enumerate(cam_ids):
        shift = C_new[i] - C_a[i]
        xy, dz = float(np.linalg.norm(shift[:2])), float(abs(shift[2]))
        rot = _rotation_deg(R_new[i], R_a[i])
        height = camera_heights_m.get(cid)
        hdiff = None if height is None else float(abs(C_new[i, 2] - float(height)))
        boosted = cid == boot_cid and record["bootstrap"]["applied"]
        rot_cap = config.max_bootstrap_rotation_change_deg if boosted else config.max_rotation_change_deg
        cameras_rec[cid] = {"center_shift_xy_m": round(xy, 4), "center_shift_z_m": round(float(shift[2]), 4),
                            "rotation_change_deg": round(rot, 4), "rotation_cap_deg": rot_cap,
                            "height_m": round(float(C_new[i, 2]), 4),
                            "height_diff_m": None if hdiff is None else round(hdiff, 4)}
        if xy > config.max_center_shift_xy_m:
            reasons.append(CENTER_SHIFT_XY)
        if dz > config.max_center_shift_z_m:
            reasons.append(CENTER_SHIFT_Z)
        if hdiff is not None and hdiff > config.max_height_diff_m:
            reasons.append(HEIGHT_DIFF)
        if rot > rot_cap:
            reasons.append(ROTATION_CHANGE)
    record["cameras"] = cameras_rec

    new_doc = copy.deepcopy(dict(calibration))
    for i, cam in enumerate(new_doc["cameras"]):
        cam["R"] = [[float(v) for v in row] for row in R_new[i]]
        cam["t"] = [float(v) for v in (-R_new[i] @ C_new[i])]
        cam["camera_height_m"] = round(float(C_new[i, 2]), 4)
        cam.pop("reprojection_error_px", None)  # Stage A 배경 홀드아웃 값은 새 자세의 검증값이 아니다(아래 Holdout LOO 로 채운다)
    probe = evaluate_views(holdout, {**new_doc, "calibration_version": f"{base_version}-walk-probe"}, hold_cfg,
                           required_pairs=required, blocking_pairs=blocking, monitor_config=monitor_config)
    after = probe.summary
    record["after"] = {"status": after["status"], "pairs": _pairs_view(after), "cameras": _cameras_view(after)}

    if after["status"] != PASSED and any(p["verdict"] not in (PASS, FAIL) for p in after["pairs"].values()):
        reasons.append(FEW_HOLDOUT)
    if any(p["verdict"] != PASS for p in after["pairs"].values()):
        reasons.append(HOLDOUT_PAIR_FAILED)
    if any(c["verdict"] != PASS for c in after["cameras"].values()):
        reasons.append(HOLDOUT_CAMERA_FAILED)
    if after["status"] == PASSED and set(probe.baseline.values_px) != set(cam_ids):
        reasons.append(BASELINE_MISSING)  # 기준선이 빠진 카메라는 관제 감시에서 검증되지 않은 채 남는다
    if not _improved(before, after):
        reasons.append(NO_IMPROVEMENT)

    for cam in new_doc["cameras"]:
        loo = after["cameras"].get(str(cam["camera_id"]), {})
        if loo.get("loo_median_px") is not None and loo.get("loo_p95_px") is not None:
            cam["reprojection_error_px"] = {"median": float(loo["loo_median_px"]), "p95": float(loo["loo_p95_px"])}
    last_t = max(t for t, _ in fit + holdout)
    new_doc["created_at"] = max(int(calibration["created_at"]), int(last_t))
    new_doc["calibration_version"] = base_version  # 자리값(content_hash 에 넣지 않는다)
    new_doc["content_hash"] = content_hash(new_doc)
    suffix = f"-walk-{new_doc['content_hash'].split(':')[-1][:8]}"
    version = base_version[: 160 - len(suffix)] + suffix
    new_doc["calibration_version"] = version
    if _schema_regressed(calibration, new_doc):
        reasons.append(SCHEMA_INVALID)

    record["new_calibration_version"] = version
    unique = tuple(dict.fromkeys(reasons))
    if unique:
        return reject(*unique)
    summary = copy.deepcopy(after)
    summary["calibration_version"] = version
    baseline = replace(probe.baseline, calibration_version=version)
    if "baseline" in summary:
        summary["baseline"]["calibration_version"] = version
    record.update(adopted=True, reasons=[], seconds=round(time.perf_counter() - started, 3))
    return WalkRefinementResult(True, (), new_doc, summary, baseline, record)


def _schema_regressed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """입력 문서가 보정 계약을 지켰는데 다듬은 문서가 어기면 True(입력이 이미 어기면 비교하지 않는다)."""
    from simai_ai.providers.contract import schema_errors

    return not schema_errors(dict(before), "calibration") and bool(schema_errors(dict(after), "calibration"))


__all__ = [
    "BASELINE_MISSING", "CENTER_SHIFT_XY", "CENTER_SHIFT_Z", "DEFAULT_WALK_CONFIG", "EVAL_JOINTS", "FEW_FIT_POINTS",
    "FEW_HOLDOUT", "HEIGHT_DIFF", "HOLDOUT_CAMERA_FAILED", "HOLDOUT_PAIR_FAILED", "NO_IMPROVEMENT", "ROTATION_CHANGE",
    "SCHEMA_INVALID", "SOLVER_FAILED", "WalkRefinementConfig", "WalkRefinementResult",
    "ordered_records", "refine_extrinsics_from_walk", "split_walk_records",
]
