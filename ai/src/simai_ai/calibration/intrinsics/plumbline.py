"""고전적 기준 백엔드: 교실 직선의 plumb-line 왜곡 추정 + 직교 소실점 초점 추정 (명세 4.3 1단계, FR-23).

학습 모델 없이 시험할 수 있는 기준선이다. 학습 모델(GeoCalib·AnyCalib)과 같은 `IntrinsicsEstimator` 형식을 낸다.

1. **직선 후보.** `lines.detect_edge_chains` 로 매끄러운 에지 사슬을 찾는다(왜곡 때문에 휘어 있다).
2. **왜곡(plumb-line).** 주점을 영상 중심으로 두고, 픽셀 단위 방사 모델 ρ_d = ρ_u (1 + b1 ρ_u² + b2 ρ_u⁴)
   (ρ 는 반대각선으로 나눈 반지름)의 (b1, b2) 를 사슬들이 가장 곧게 펴지도록 Levenberg–Marquardt 로 맞춘다.
   잔차는 펴진 사슬의 직선 맞춤 수직 거리이고, 축소로 잔차를 줄이는 해를 막기 위해 사슬 길이 비(원본/펴진)를
   곱해 원본 픽셀 단위로 둔다. 곧게 펴지지 않는 사슬(실제 곡선)은 반복해서 이상치로 뺀다.
   직선성은 초점과 무관하게 픽셀 공간에서 정해지므로 (b1, b2) 는 초점을 몰라도 구해진다.
3. **초점(소실점).** 펴진 사슬을 직선으로 보고 RANSAC 으로 소실점을 차례로 찾는다(교실은 서로 직교한 세 방향이
   많다). 유한한 두 소실점 v1, v2 가 직교 방향이면 f² = −(v1 − c)·(v2 − c) 다. 세 쌍의 초점이 서로 맞는(직교 세
   방향이 확인된) 소실점 셋이 있을 때만 그 중앙값을 쓴다. 정면을 보는 카메라처럼 소실점이 무한대이거나 세 쌍이
   맞지 않으면 초점을 정하지 않는다(status NO_FOCAL, 지어내지 않는다. 쌍 하나로 정하는 것은 설정으로만 켠다).
4. **OpenCV 계수.** k1 = b1 (f/D)², k2 = b2 (f/D)⁴ (정확히 같은 모델).

불확실성(1σ 근사):
- 왜곡: LM 의 (JᵀJ)⁻¹ σ². 한 사슬 안의 점은 서로 상관돼 있으므로 점 수 대신 사슬 수를 자유도로 보고 키운다.
- 초점: 소실점 지지 직선을 다시 뽑아(bootstrap, 시드 고정) 구한 초점의 표준편차와 쌍 사이 차이 중 큰 값.

결정적이다: 같은 영상·설정이면 같은 결과(RANSAC·bootstrap 시드 고정).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

from simai_ai.calibration.intrinsics.estimate import (
    METHOD_PLUMB_LINE_VP,
    STATUS_FAILED,
    STATUS_NO_FOCAL,
    STATUS_OK,
    IntrinsicsEstimate,
    IntrinsicsEstimationError,
    IntrinsicsUncertainty,
    half_diagonal,
    opencv_from_radial_pixel,
    radial_monotonic,
    undistort_radii,
)
from simai_ai.calibration.intrinsics.lines import EdgeChainConfig, detect_edge_chains

PLUMB_LINE_VERSION = "0.1.0"
_BAD_RESIDUAL = 1e3


@dataclass(frozen=True)
class PlumbLineConfig:
    edges: EdgeChainConfig = field(default_factory=EdgeChainConfig)
    # 방사 계수 개수: 1 = k1 만, 2 = k1·k2
    num_radial: int = 2
    min_chains: int = 8
    # 사슬 RMS 가 max(이 배수 × 중앙값, outlier_floor_px) 를 넘으면 이상치
    outlier_factor: float = 3.0
    outlier_floor_px: float = 0.5
    outlier_rounds: int = 3
    # 이상치를 뺀 뒤 남은 직선 후보의 길이 비율이 이보다 작으면 왜곡 모델이 직선 대부분을 펴지 못한 것으로 보고
    # 실패로 둔다(맞지 않는 모델이 휜 직선을 모두 이상치로 빼는 퇴화를 막는다). 빈 교실 프레임 기준 초기값
    min_kept_length_fraction: float = 0.7
    # 편 뒤 직선성 RMS(원본 픽셀)가 이보다 크면 실패(곡선을 맞춘 것). 서브픽셀 에지 기준 초기값
    max_straightness_px: float = 1.0
    # 펴기 전 RMS 가 improvement_check_min_px 보다 컸는데 편 뒤 RMS 가 그 비율보다 크면 실패
    improvement_check_min_px: float = 1.0
    max_improvement_ratio: float = 0.5
    # 픽셀 단위 방사 계수(반대각선 기준)의 허용 범위. C110 급 광각(시험 b ≈ (−0.67, 0.45))을 넉넉히 덮는다
    b1_range: tuple[float, float] = (-1.5, 1.0)
    b2_range: tuple[float, float] = (-1.0, 2.0)
    lm_iterations: int = 60
    # 소실점
    vp_min_line_px: float = 60.0
    vp_inlier_angle_rad: float = math.radians(1.0)
    vp_max_count: int = 4
    vp_min_lines: int = 3
    ransac_iterations: int = 1500
    # 세 쌍의 초점이 이 상대 차이 안이면 서로 맞는 직교 세 방향으로 본다
    triple_consistency: float = 0.05
    # True 면 서로 맞는 직교 세 방향(소실점 셋)이 있을 때만 초점을 정한다. False 면 지지 직선이 가장 많은 쌍 하나로도
    # 정한다(수평 카메라처럼 세로 소실점이 무한대인 배치. 직교 여부를 확인하지 못하므로 결과 메모에 남는다)
    require_orthogonal_triple: bool = True
    # 초점 허용 범위: 영상 너비의 배수
    focal_range_width: tuple[float, float] = (0.15, 6.0)
    # 소실점이 중심에서 이 배수(× 반대각선)보다 멀면 초점 계산에 쓰지 않는다(무한대 근처는 조건이 나쁘다)
    vp_max_distance_diag: float = 50.0
    # bootstrap 상대 표준편차가 이보다 크면 초점을 정하지 않는다(퇴화 배치: 정면 카메라 등)
    max_focal_rel_std: float = 0.05
    bootstrap: int = 40
    seed: int = 0


@dataclass(frozen=True)
class _Chains:
    """사슬들을 이어 붙인 배열(반대각선 단위, 중심 기준)과 사슬 경계."""

    q: np.ndarray  # (N, 2)
    rho: np.ndarray  # (N,)
    starts: np.ndarray  # (C,)
    counts: np.ndarray  # (C,)
    extent_d: np.ndarray  # (C,) 원본(왜곡) 공간 사슬 길이

    @property
    def chain_index(self) -> np.ndarray:
        return np.repeat(np.arange(len(self.counts)), self.counts)

    def subset(self, keep: np.ndarray) -> _Chains:
        idx = np.flatnonzero(keep)
        parts = [self.q[self.starts[c] : self.starts[c] + self.counts[c]] for c in idx]
        q = np.concatenate(parts) if parts else np.zeros((0, 2))
        counts = self.counts[idx]
        starts = np.r_[0, np.cumsum(counts)[:-1]] if len(counts) else np.zeros(0, dtype=int)
        return _Chains(q, np.hypot(q[:, 0], q[:, 1]), starts.astype(int), counts, self.extent_d[idx])


@dataclass(frozen=True)
class _Lines:
    """펴진 직선(중심 기준 원본 픽셀 단위): 중점, 방향, 길이, 정규화한 동차 직선."""

    mid: np.ndarray
    direction: np.ndarray
    length: np.ndarray
    homog: np.ndarray
    D: float


@dataclass(frozen=True)
class PlumbLineDiagnostics:
    """시험·진단용 중간 결과."""

    chains: list[np.ndarray]
    kept_chain_mask: np.ndarray
    radial_pixel: tuple[float, float]
    vanishing_points: list[np.ndarray]
    pair_focals: list[float]


class PlumbLineVPEstimator:
    """plumb-line + 소실점 기준 추정기 (`IntrinsicsEstimator`)."""

    method = METHOD_PLUMB_LINE_VP

    def __init__(self, config: PlumbLineConfig | None = None) -> None:
        self.config = config or PlumbLineConfig()
        if self.config.num_radial not in (1, 2):
            raise ValueError(f"num_radial 은 1 또는 2: {self.config.num_radial}")
        self.last_diagnostics: PlumbLineDiagnostics | None = None

    def estimate(self, image: np.ndarray, *, camera_id: str) -> IntrinsicsEstimate:
        arr = np.asarray(image)
        if arr.ndim not in (2, 3):
            raise IntrinsicsEstimationError(f"영상은 (H, W) 또는 (H, W, C) 배열이어야 한다: {arr.shape}")
        h, w = arr.shape[:2]
        chains = detect_edge_chains(arr, self.config.edges)
        return self.estimate_from_chains(chains, image_size=(w, h), camera_id=camera_id)

    def estimate_from_chains(
        self, chains: Sequence[np.ndarray], *, image_size: tuple[int, int], camera_id: str
    ) -> IntrinsicsEstimate:
        """에지 사슬(원본 픽셀 좌표 목록)로 추정한다. 사슬을 다른 검출기에서 받을 때도 쓴다."""
        cfg = self.config
        w, h = image_size
        D = half_diagonal(image_size)
        center = np.array([w / 2.0, h / 2.0])
        chains = [np.asarray(c, dtype=float).reshape(-1, 2) for c in chains if len(c) >= 5]
        notes: list[str] = []
        if len(chains) < cfg.min_chains:
            self.last_diagnostics = PlumbLineDiagnostics(list(chains), np.zeros(len(chains), bool), (0.0, 0.0), [], [])
            return _failed(
                camera_id, image_size, len(chains), f"직선 후보가 {len(chains)}개로 {cfg.min_chains}개보다 적다"
            )
        data = _pack(chains, center, D)
        keep = np.ones(len(chains), dtype=bool)
        params = np.zeros(cfg.num_radial)
        cov = None
        sub = data
        for _ in range(cfg.outlier_rounds):
            sub = data.subset(keep)
            if len(sub.counts) < cfg.min_chains:
                break
            params, cov = _fit_radial(sub, cfg, params)
            rms = _chain_rms(data, params) * D
            limit = max(cfg.outlier_factor * float(np.median(rms[keep])), cfg.outlier_floor_px)
            new_keep = rms <= limit
            if np.array_equal(new_keep, keep):
                break
            keep = new_keep
        sub = data.subset(keep)
        b = (float(params[0]), float(params[1]) if cfg.num_radial == 2 else 0.0)
        if len(sub.counts) < cfg.min_chains:
            self.last_diagnostics = PlumbLineDiagnostics(list(chains), keep, b, [], [])
            return _failed(camera_id, image_size, int(keep.sum()), "이상치를 뺀 직선이 너무 적다")
        kept_fraction = float(data.extent_d[keep].sum() / data.extent_d.sum())
        if kept_fraction < cfg.min_kept_length_fraction:
            self.last_diagnostics = PlumbLineDiagnostics(list(chains), keep, b, [], [])
            return _failed(
                camera_id,
                image_size,
                int(keep.sum()),
                f"왜곡 모델이 직선 후보 대부분을 펴지 못했다(남은 길이 비율 {kept_fraction:.2f} < "
                f"{cfg.min_kept_length_fraction})",
            )
        res_after = _residuals(sub, np.array(b[: cfg.num_radial]))
        res_before = _residuals(sub, np.zeros(cfg.num_radial))
        straight_after = float(np.sqrt(np.mean(res_after**2)) * D)
        straight_before = float(np.sqrt(np.mean(res_before**2)) * D)
        why = _implausible(b, straight_before, straight_after, cfg)
        if why is not None:
            # 펴지지 않은 직선으로 맞춘 왜곡은 지어낸 값이다. 왜곡도 비우고 공유 단계에 넣지 않는다(FAILED)
            self.last_diagnostics = PlumbLineDiagnostics(list(chains), keep, b, [], [])
            return _failed(camera_id, image_size, int(keep.sum()), why, straight_before, straight_after)
        b_std: list[float | None] = [None, None]
        if cov is not None:
            for i in range(cfg.num_radial):
                b_std[i] = float(math.sqrt(max(cov[i, i], 0.0)))

        lines = _undistorted_lines(sub, b, D, cfg.vp_min_line_px)
        vps, vp_inliers = _find_vanishing_points(lines, cfg)
        focal, focal_std, pair_focals, used = _focal_from_vps(vps, vp_inliers, lines, image_size, cfg)
        if focal is not None and (focal_std is None or focal_std / focal > cfg.max_focal_rel_std):
            notes.append(
                f"소실점 배치가 나빠 초점 {focal:.1f}px 의 불확실성"
                f"({'모름' if focal_std is None else f'{focal_std:.1f}px'})이 커서 쓰지 않았다"
            )
            focal = None
        self.last_diagnostics = PlumbLineDiagnostics(list(chains), keep, b, vps, pair_focals)
        uncertainty_common = {
            "straightness_rms_px": straight_after,
            "straightness_rms_uncorrected_px": straight_before,
            "line_count": int(keep.sum()),
            "vanishing_point_count": len(vps),
        }
        if focal is None:
            notes.append("서로 맞는 직교 소실점이 없어 초점을 정하지 못했다(왜곡만 픽셀 단위로 남긴다)")
            return IntrinsicsEstimate(
                camera_id=camera_id,
                image_size=(int(w), int(h)),
                focal_px=None,
                distortion_coeffs=None,
                radial_pixel=b,
                uncertainty=IntrinsicsUncertainty(notes=tuple(notes), **uncertainty_common),
                method=self.method,
                method_version=PLUMB_LINE_VERSION,
                status=STATUS_NO_FOCAL,
            )
        coeffs = opencv_from_radial_pixel(b, focal, image_size)
        s = focal / D
        if used == 1:
            notes.append(
                "초점은 소실점 한 쌍에서 정했다(직교 세 방향으로 확인하지 못함). 주점은 영상 중심으로 가정했다"
            )
        else:
            notes.append(f"초점은 서로 맞는 직교 소실점 {used}쌍에서 정했다. 주점은 영상 중심으로 가정했다")
        return IntrinsicsEstimate(
            camera_id=camera_id,
            image_size=(int(w), int(h)),
            focal_px=float(focal),
            distortion_coeffs=coeffs,
            radial_pixel=b,
            uncertainty=IntrinsicsUncertainty(
                focal_std_px=focal_std,
                k1_std=None if b_std[0] is None else b_std[0] * s**2,
                k2_std=None if b_std[1] is None else b_std[1] * s**4,
                notes=tuple(notes),
                **uncertainty_common,
            ),
            method=self.method,
            method_version=PLUMB_LINE_VERSION,
            status=STATUS_OK,
        )


def _implausible(
    b: tuple[float, float], straight_before: float, straight_after: float, cfg: PlumbLineConfig
) -> str | None:
    """맞춘 왜곡을 믿을 수 없는 이유(믿을 수 있으면 None).

    - 편 뒤에도 직선성 RMS 가 절대 기준을 넘음(직선이 아니라 곡선을 맞춤)
    - 펴기 전에 뚜렷이 휘어 있었는데 개선 비율이 작음
    - 계수가 렌즈로 그럴듯한 범위 밖이거나 영상 안에서 접힘
    """
    if straight_after > cfg.max_straightness_px:
        return f"편 뒤에도 직선성 RMS {straight_after:.2f}px 가 기준 {cfg.max_straightness_px}px 를 넘는다(직선 부족)"
    if straight_before > cfg.improvement_check_min_px and straight_after > cfg.max_improvement_ratio * straight_before:
        return (
            f"직선성 개선이 작다({straight_before:.2f} → {straight_after:.2f}px, 비율 "
            f"{straight_after / straight_before:.2f} > {cfg.max_improvement_ratio})"
        )
    lo1, hi1 = cfg.b1_range
    lo2, hi2 = cfg.b2_range
    if not (lo1 <= b[0] <= hi1 and lo2 <= b[1] <= hi2):
        return f"왜곡 계수 b=({b[0]:.3f}, {b[1]:.3f}) 가 허용 범위 b1 {cfg.b1_range}, b2 {cfg.b2_range} 밖이다"
    if not monotonic_in_image(b):
        return f"왜곡 계수 b=({b[0]:.3f}, {b[1]:.3f}) 가 영상 안에서 접힌다"
    return None


def _failed(
    camera_id: str,
    image_size: tuple[int, int],
    count: int,
    why: str,
    straight_before: float | None = None,
    straight_after: float | None = None,
) -> IntrinsicsEstimate:
    return IntrinsicsEstimate(
        camera_id=camera_id,
        image_size=(int(image_size[0]), int(image_size[1])),
        focal_px=None,
        distortion_coeffs=None,
        radial_pixel=None,
        uncertainty=IntrinsicsUncertainty(
            line_count=count,
            straightness_rms_px=straight_after,
            straightness_rms_uncorrected_px=straight_before,
            notes=(why,),
        ),
        method=METHOD_PLUMB_LINE_VP,
        method_version=PLUMB_LINE_VERSION,
        status=STATUS_FAILED,
    )


# ---------------------------------------------------------------------------
# plumb-line
# ---------------------------------------------------------------------------


def _pack(chains: Sequence[np.ndarray], center: np.ndarray, D: float) -> _Chains:
    qs = [(c - center) / D for c in chains]
    counts = np.array([len(q) for q in qs], dtype=int)
    starts = np.r_[0, np.cumsum(counts)[:-1]].astype(int)
    q = np.concatenate(qs)
    extent = np.array([_extent(x) for x in qs])
    return _Chains(q, np.hypot(q[:, 0], q[:, 1]), starts, counts, extent)


def _extent(q: np.ndarray) -> float:
    c = q - q.mean(axis=0)
    _, vecs = np.linalg.eigh(c.T @ c)
    u = c @ vecs[:, 1]
    return float(u.max() - u.min())


def _undistort(data: _Chains, params: np.ndarray) -> tuple[np.ndarray, bool]:
    b1 = float(params[0])
    b2 = float(params[1]) if len(params) > 1 else 0.0
    if b1 == 0.0 and b2 == 0.0:
        return data.q, True
    ru, ok = undistort_radii(data.rho, b1, b2)
    if not ok:
        return data.q, False
    _, ok_corner = undistort_radii(np.array([1.0]), b1, b2)
    if not ok_corner:
        return data.q, False
    scale = np.ones_like(data.rho)
    nz = data.rho > 1e-12
    scale[nz] = ru[nz] / data.rho[nz]
    return data.q * scale[:, None], True


def _chain_stats(q: np.ndarray, data: _Chains) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """사슬별 중심, 단위 방향, 단위 법선 (벡터화)."""
    n = data.counts.astype(float)
    sx = np.add.reduceat(q[:, 0], data.starts) / n
    sy = np.add.reduceat(q[:, 1], data.starts) / n
    idx = data.chain_index
    dx = q[:, 0] - sx[idx]
    dy = q[:, 1] - sy[idx]
    sxx = np.add.reduceat(dx * dx, data.starts)
    syy = np.add.reduceat(dy * dy, data.starts)
    sxy = np.add.reduceat(dx * dy, data.starts)
    theta = 0.5 * np.arctan2(2 * sxy, sxx - syy)
    direction = np.column_stack([np.cos(theta), np.sin(theta)])
    normal = np.column_stack([-np.sin(theta), np.cos(theta)])
    return np.column_stack([sx, sy]), direction, normal


def _residuals(data: _Chains, params: np.ndarray) -> np.ndarray:
    """펴진 사슬의 직선 수직 거리 × (원본 길이 / 펴진 길이). 반대각선 단위."""
    q, ok = _undistort(data, params)
    if not ok:
        return np.full(len(data.q), _BAD_RESIDUAL)
    mean, direction, normal = _chain_stats(q, data)
    idx = data.chain_index
    rel = q - mean[idx]
    dist = rel[:, 0] * normal[idx, 0] + rel[:, 1] * normal[idx, 1]
    along = rel[:, 0] * direction[idx, 0] + rel[:, 1] * direction[idx, 1]
    ext_u = np.maximum.reduceat(along, data.starts) - np.minimum.reduceat(along, data.starts)
    ratio = data.extent_d / np.maximum(ext_u, 1e-12)
    return dist * ratio[idx]


def _chain_rms(data: _Chains, params: np.ndarray) -> np.ndarray:
    r = _residuals(data, params)
    return np.sqrt(np.add.reduceat(r * r, data.starts) / data.counts)


def _cost(data: _Chains, params: np.ndarray) -> float:
    r = _residuals(data, params)
    return float(r @ r)


def _fit_radial(data: _Chains, cfg: PlumbLineConfig, init: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    # 1차원 격자로 시작점을 고른 뒤(국소 최소 회피) LM
    grid = np.linspace(-1.2, 0.6, 37)
    starts = [init.copy()]
    for g in grid:
        p = np.zeros(cfg.num_radial)
        p[0] = g
        starts.append(p)
    best = min(starts, key=lambda p: _cost(data, p))
    params = best.astype(float)
    lam = 1e-3
    cost = _cost(data, params)
    J = None
    for _ in range(cfg.lm_iterations):
        r = _residuals(data, params)
        J = _jacobian(data, params)
        JTJ = J.T @ J
        g = J.T @ r
        improved = False
        for _ in range(12):
            A = JTJ + lam * np.diag(np.maximum(np.diag(JTJ), 1e-12))
            try:
                step = -np.linalg.solve(A, g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            cand = params + step
            c = _cost(data, cand)
            if c < cost:
                params, cost = cand, c
                lam = max(lam / 3, 1e-9)
                improved = True
                break
            lam *= 10
        if not improved or np.linalg.norm(step) < 1e-10:
            break
    cov = None
    if J is not None:
        r = _residuals(data, params)
        n_eff = max(len(data.counts) - len(params), 1)
        sigma2 = float(r @ r) / max(len(r) - len(params), 1)
        try:
            # 한 사슬의 점은 상관돼 있으므로 (점 수 / 사슬 수) 배로 키운다
            cov = np.linalg.inv(J.T @ J) * sigma2 * (len(r) / n_eff)
        except np.linalg.LinAlgError:
            cov = None
    return params, cov


def _jacobian(data: _Chains, params: np.ndarray, h: float = 1e-6) -> np.ndarray:
    cols = []
    for i in range(len(params)):
        dp = np.zeros_like(params)
        dp[i] = h
        cols.append((_residuals(data, params + dp) - _residuals(data, params - dp)) / (2 * h))
    return np.column_stack(cols)


# ---------------------------------------------------------------------------
# 소실점·초점
# ---------------------------------------------------------------------------


def _undistorted_lines(data: _Chains, b: tuple[float, float], D: float, min_len_px: float) -> _Lines:
    q, _ = _undistort(data, np.array(b))
    mean, direction, normal = _chain_stats(q, data)
    idx = data.chain_index
    rel = q - mean[idx]
    along = rel[:, 0] * direction[idx, 0] + rel[:, 1] * direction[idx, 1]
    length = (np.maximum.reduceat(along, data.starts) - np.minimum.reduceat(along, data.starts)) * D
    mid = mean * D
    keep = length >= min_len_px
    mid, direction, normal, length = mid[keep], direction[keep], normal[keep], length[keep]
    # 동차 직선 l = (n, −n·m), 조건수를 위해 D 로 나눈 좌표에서 정의한다
    homog = np.column_stack([normal, -(normal * (mid / D)).sum(axis=1)])
    return _Lines(mid, direction, length, homog, D)


def _vp_angle(lines: _Lines, vp: np.ndarray) -> np.ndarray:
    """각 직선의 방향과 (중점 → 소실점) 방향의 각(라디안). vp 는 D 단위 동차 좌표."""
    e = vp[:2][None, :] - (lines.mid / lines.D) * vp[2]
    norm = np.linalg.norm(e, axis=1)
    cosang = np.abs((e * lines.direction).sum(axis=1)) / np.maximum(norm, 1e-12)
    ang = np.arccos(np.clip(cosang, 0.0, 1.0))
    return np.where(norm < 1e-9, math.pi / 2, ang)


def _refine_vp(lines: _Lines, inliers: np.ndarray) -> np.ndarray:
    A = lines.homog[inliers] * np.sqrt(lines.length[inliers])[:, None]
    _, _, vt = np.linalg.svd(A)
    v = vt[-1]
    return v / np.linalg.norm(v)


def _find_vanishing_points(lines: _Lines, cfg: PlumbLineConfig) -> tuple[list[np.ndarray], list[np.ndarray]]:
    n = len(lines.length)
    if n < 2:
        return [], []
    rng = np.random.default_rng(cfg.seed)
    remaining = np.ones(n, dtype=bool)
    vps: list[np.ndarray] = []
    inlier_sets: list[np.ndarray] = []
    weights = lines.length / lines.length.sum()
    for _ in range(cfg.vp_max_count):
        idx = np.flatnonzero(remaining)
        if len(idx) < cfg.vp_min_lines:
            break
        p = weights[idx] / weights[idx].sum()
        best_score, best_v = -1.0, None
        for _ in range(cfg.ransac_iterations):
            i, j = rng.choice(idx, size=2, replace=False, p=p)
            v = np.cross(lines.homog[i], lines.homog[j])
            nv = np.linalg.norm(v)
            if nv < 1e-12:
                continue
            v = v / nv
            ang = _vp_angle(lines, v)
            inl = remaining & (ang < cfg.vp_inlier_angle_rad)
            score = float(lines.length[inl].sum())
            if score > best_score:
                best_score, best_v = score, v
        if best_v is None:
            break
        v = best_v
        inl = remaining & (_vp_angle(lines, v) < cfg.vp_inlier_angle_rad)
        for _ in range(3):
            if inl.sum() < 2:
                break
            v = _refine_vp(lines, inl)
            inl = remaining & (_vp_angle(lines, v) < cfg.vp_inlier_angle_rad)
        if inl.sum() < cfg.vp_min_lines:
            break
        vps.append(v)
        inlier_sets.append(inl)
        remaining &= ~inl
    return vps, inlier_sets


def _vp_point(v: np.ndarray, D: float, eps: float = 1e-6) -> np.ndarray | None:
    """동차 소실점(D 단위) → 중심 기준 픽셀 좌표. 무한대면 None."""
    if abs(v[2]) < eps:
        return None
    return v[:2] / v[2] * D


def _pair_focal(
    v1: np.ndarray, v2: np.ndarray, D: float, frange: tuple[float, float], max_dist: float = math.inf
) -> float | None:
    p1, p2 = _vp_point(v1, D), _vp_point(v2, D)
    if p1 is None or p2 is None:
        return None
    if np.linalg.norm(p1) > max_dist or np.linalg.norm(p2) > max_dist:
        return None
    f2 = -float(p1 @ p2)
    if f2 <= 0:
        return None
    f = math.sqrt(f2)
    if not (frange[0] <= f <= frange[1]):
        return None
    return f


def _focal_from_vps(
    vps: list[np.ndarray],
    inliers: list[np.ndarray],
    lines: _Lines,
    image_size: tuple[int, int],
    cfg: PlumbLineConfig,
) -> tuple[float | None, float | None, list[float], int]:
    if len(vps) < 2:
        return None, None, [], 0
    D = lines.D
    w = image_size[0]
    frange = (cfg.focal_range_width[0] * w, cfg.focal_range_width[1] * w)
    support = [float(lines.length[s].sum()) for s in inliers]
    pairs: dict[tuple[int, int], float] = {}
    for i, j in combinations(range(len(vps)), 2):
        f = _pair_focal(vps[i], vps[j], D, frange, cfg.vp_max_distance_diag * D)
        if f is not None:
            pairs[(i, j)] = f
    if not pairs:
        return None, None, [], 0
    chosen: list[tuple[int, int]] | None = None
    best_support = -1.0
    for a, b, c in combinations(range(len(vps)), 3):
        trio = [(a, b), (a, c), (b, c)]
        if all(t in pairs for t in trio):
            fs = [pairs[t] for t in trio]
            if (max(fs) - min(fs)) / float(np.median(fs)) <= cfg.triple_consistency:
                s = support[a] + support[b] + support[c]
                if s > best_support:
                    best_support, chosen = s, trio
    if chosen is None:
        # 서로 맞는 직교 세 방향이 없다. 쌍 하나는 직교인지 확인할 길이 없으므로 기본은 초점을 정하지 않는다
        if cfg.require_orthogonal_triple:
            return None, None, [float(f) for f in pairs.values()], 0
        chosen = [max(pairs, key=lambda t: min(support[t[0]], support[t[1]]))]
    focals = [pairs[t] for t in chosen]
    focal = float(np.median(focals))
    # bootstrap: 지지 직선을 다시 뽑아 소실점·초점을 다시 구한다
    rng = np.random.default_rng(cfg.seed + 1)
    used_vps = sorted({i for t in chosen for i in t})
    boot: list[float] = []
    for _ in range(cfg.bootstrap):
        new_vps = {}
        for i in used_vps:
            idx = np.flatnonzero(inliers[i])
            pick = rng.choice(idx, size=len(idx), replace=True)
            A = lines.homog[pick] * np.sqrt(lines.length[pick])[:, None]
            _, _, vt = np.linalg.svd(A)
            new_vps[i] = vt[-1] / np.linalg.norm(vt[-1])
        fs = [_pair_focal(new_vps[a], new_vps[b], D, frange, cfg.vp_max_distance_diag * D) for a, b in chosen]
        fs = [f for f in fs if f is not None]
        if fs:
            boot.append(float(np.median(fs)))
    # 다시 뽑은 표본의 80% 이상에서 초점이 나와야 불확실성을 믿는다(아니면 모름 → 초점을 쓰지 않음)
    boot_std = float(np.std(boot)) if len(boot) >= max(3, 0.8 * cfg.bootstrap) else None
    spread = float(np.std(focals)) if len(focals) > 1 else 0.0
    focal_std = None if boot_std is None else max(boot_std, spread)
    return focal, focal_std, [float(f) for f in pairs.values()], len(chosen)


def monotonic_in_image(radial_pixel: Sequence[float]) -> bool:
    """픽셀 단위 방사 계수가 영상 모서리(ρ_d = 1)까지 단조인가."""
    b1, b2 = radial_pixel
    ru, ok = undistort_radii(np.array([1.0]), b1, b2)
    return ok and radial_monotonic(b1, b2, float(ru[0]))
