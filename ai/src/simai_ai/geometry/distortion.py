"""렌즈 왜곡 모델의 정방향과 역방향 (명세 4.3, 5.2). 식은 OpenCV 와 같다.

- OPENCV_PINHOLE: 계수 [k1 k2 p1 p2], [k1 k2 p1 p2 k3], [k1 k2 p1 p2 k3 k4 k5 k6] (유리 모델). `cv2.projectPoints` 식.
- OPENCV_FISHEYE: 계수 [k1 k2 k3 k4]. θ_d = θ(1 + k1θ² + k2θ⁴ + k3θ⁶ + k4θ⁸), θ = atan(r). `cv2.fisheye` 식.

좌표는 모두 카메라 정규화 좌표 (x/z, y/z) 다. 정방향은 왜곡 없는 좌표 → 왜곡 좌표, 역방향(왜곡 제거)은 그 반대다.

왜곡 제거는 모르면 비운다(AGENTS.md 규칙 2):

1. 반지름 방향 식 g(r)(핀홀은 r·방사 배율, 어안은 θ_d(θ))가 0 에서 처음으로 단조 증가를 잃는 곳(도함수 ≤ 0
   또는 유리 모델 분모 ≤ 0)까지를 쓸 수 있는 가지로 본다. 그 너머는 왜곡 식이 접혀 한 픽셀이 여러 광선에 대응하거나
   대응하는 광선이 없다. 가지 끝의 왜곡 반지름보다 바깥 픽셀은 풀지 않는다(NaN).
2. 가지 안에서는 해가 하나뿐이다. 반지름 방정식을 구간을 지키는 뉴턴법(구간 밖으로 나가면 이분법)으로 풀어
   영상 모서리처럼 왜곡이 강한 곳에서도 수렴한다. cv2.undistortPoints 의 고정점 반복은 강한 광각 배럴 왜곡
   (k1 −0.30, k2 0.09 등)의 모서리에서 발산해 수백 픽셀 어긋난 광선을 낸다.
3. 접선 계수(p1·p2)가 있으면 반지름 해를 초기값으로 2차원 감쇠 뉴턴법으로 다듬는다.
4. 다시 왜곡해 원래 좌표와의 차이(왕복 오차)가 허용치 이하이고, 가지 안이고, (핀홀) 야코비안 행렬식이 양수일 때만
   해로 인정한다. 아니면 그 점은 NaN 이고 실패 마스크가 False 다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.polynomial import Polynomial

MODEL_NONE = "NONE"
MODEL_PINHOLE = "OPENCV_PINHOLE"
MODEL_FISHEYE = "OPENCV_FISHEYE"
PINHOLE_COEFF_COUNTS = (0, 4, 5, 8)
FISHEYE_COEFF_COUNT = 4

# 핀홀 모델에서 가지를 찾는 왜곡 없는 반지름 상한(정규화 좌표). 10 ≈ 광축에서 84°. 그 너머는 핀홀 투영 자체가 무의미하다
PINHOLE_MAX_RADIUS = 10.0
# 어안 모델의 θ 상한. 90° 이상은 핀홀 정규화 좌표(tan θ)로 나타낼 수 없다
FISHEYE_MAX_THETA = math.pi / 2
# 가지 경계: 도함수(또는 유리 모델 분모)가 이 값 이하이면 접힌다고 본다
MIN_SLOPE = 1e-6
# 가지 경계: 다항식 근·극값 후보에 더하는 보조 표본 수(보장은 근·극값이 한다)와 경계 이분법 횟수
BRANCH_SAMPLES = 257
BRANCH_BISECTIONS = 60
# 반지름 방정식(구간 보호 뉴턴법)과 접선 다듬기(2차원 감쇠 뉴턴법) 최대 반복 수
RADIAL_ITERATIONS = 100
REFINE_ITERATIONS = 30
REFINE_HALVINGS = 10
# 반복을 멈추는 상대 변화(반지름) 또는 상대 잔차(접선 다듬기)
CONVERGED = 1e-15
# 왕복 오차 허용치 기본값(정규화 좌표). 호출자는 픽셀 허용치를 초점으로 나눠 넘긴다(`RigCamera`)
DEFAULT_MAX_RESIDUAL = 1e-9


@dataclass(frozen=True)
class RadialBranch:
    """왜곡 식이 단조 증가하는 첫 가지 [0, r_max). rd_max 는 그 끝의 왜곡 반지름(핀홀 r_d, 어안 θ_d)이다."""

    r_max: float
    rd_max: float


def check_model(model: str, coeffs: Sequence[float]) -> None:
    """지원하는 왜곡 모델과 계수 개수인지 확인한다. 아니면 ValueError."""
    if model == MODEL_NONE:
        if any(c != 0.0 for c in coeffs):
            raise ValueError(f"왜곡 모델 NONE 인데 계수가 있다: {tuple(coeffs)}")
    elif model == MODEL_PINHOLE:
        if len(coeffs) not in PINHOLE_COEFF_COUNTS:
            raise ValueError(f"OPENCV_PINHOLE 계수는 4·5·8개만 지원한다: {len(coeffs)}개")
    elif model == MODEL_FISHEYE:
        if len(coeffs) != FISHEYE_COEFF_COUNT:
            raise ValueError(f"OPENCV_FISHEYE 계수는 [k1, k2, k3, k4] 4개여야 한다: {len(coeffs)}개")
    else:
        raise ValueError(f"모르는 왜곡 모델 {model}")


def distort(xn: np.ndarray, model: str, coeffs: Sequence[float]) -> np.ndarray:
    """왜곡 없는 정규화 좌표 (N, 2) → 왜곡 정규화 좌표 (N, 2)."""
    xn = np.asarray(xn, dtype=float).reshape(-1, 2)
    if model == MODEL_NONE or (model == MODEL_PINHOLE and not coeffs):
        return xn.copy()
    if model == MODEL_PINHOLE:
        k1, k2, p1, p2, k3, k4, k5, k6 = _pinhole8(coeffs)
        x, y = xn[:, 0], xn[:, 1]
        s = x * x + y * y
        radial = (1 + s * (k1 + s * (k2 + s * k3))) / (1 + s * (k4 + s * (k5 + s * k6)))
        return np.column_stack(
            [
                x * radial + 2 * p1 * x * y + p2 * (s + 2 * x * x),
                y * radial + p1 * (s + 2 * y * y) + 2 * p2 * x * y,
            ]
        )
    if model == MODEL_FISHEYE:
        r = np.hypot(xn[:, 0], xn[:, 1])
        theta_d, _ = _fisheye_g(np.arctan(r), coeffs)
        scale = np.ones_like(r)
        nz = r > 1e-12
        scale[nz] = theta_d[nz] / r[nz]
        return xn * scale[:, None]
    raise ValueError(f"모르는 왜곡 모델 {model}")


def radial_branch(model: str, coeffs: Sequence[float]) -> RadialBranch:
    """왜곡 식의 첫 단조 가지 (카메라마다 한 번 계산한다).

    표본 격자에 기대지 않는다. s = r² 에 대한 다항식 Q₁(s) = (g' 의 분자) − MIN_SLOPE·den²(어안은 g' − MIN_SLOPE)과
    Q₂(s) = den − MIN_SLOPE 의 근과 극값(도함수의 근)을 모두 후보점으로 잡는다. 연속 함수의 최솟값은 끝점이나 극값에
    있으므로, 후보점에서 모두 조건을 만족하면 [0, 상한] 전체에서 만족한다. 처음 조건이 깨지는 후보점과 그 앞 후보점
    사이에는 극값이 없어 Q 가 단조이므로, 그 사이를 이분법으로 좁히면 첫 경계다. 표본 사이의 좁은 접힘도 놓치지 않는다.
    """
    if model == MODEL_NONE or (model == MODEL_PINHOLE and not coeffs):
        return RadialBranch(math.inf, math.inf)
    cap = PINHOLE_MAX_RADIUS if model == MODEL_PINHOLE else FISHEYE_MAX_THETA
    s_cap = cap * cap
    candidates = [np.array([0.0, s_cap]), np.linspace(0.0, s_cap, BRANCH_SAMPLES)]
    for poly in _branch_polynomials(model, coeffs):
        for q in (poly, poly.deriv()):
            q = q.trim()
            if q.degree() >= 1:
                re = np.real(q.roots())
                candidates.append(re[(re >= 0.0) & (re <= s_cap)])
    s = np.unique(np.concatenate(candidates))
    r = np.sqrt(s)
    bad = ~_monotone(model, coeffs, r)
    if bad[0]:
        return RadialBranch(0.0, 0.0)
    if not bad.any():
        r_max = cap
    else:
        i = int(np.argmax(bad))
        lo, hi = float(r[i - 1]), float(r[i])
        for _ in range(BRANCH_BISECTIONS):
            mid = 0.5 * (lo + hi)
            if _monotone(model, coeffs, np.array([mid]))[0]:
                lo = mid
            else:
                hi = mid
        r_max = lo
    g, _ = _radial(model, coeffs, np.array([r_max]))
    return RadialBranch(float(r_max), float(g[0]))


def _branch_polynomials(model: str, coeffs: Sequence[float]) -> list[Polynomial]:
    """가지 경계 조건을 s = r²(어안은 θ²)의 다항식으로: 양수여야 하는 식들."""
    if model == MODEL_PINHOLE:
        k1, k2, _, _, k3, k4, k5, k6 = _pinhole8(coeffs)
        s = Polynomial([0.0, 1.0])
        num = Polynomial([1.0, k1, k2, k3])
        den = Polynomial([1.0, k4, k5, k6])
        slope_num = num * den + 2 * s * (num.deriv() * den - num * den.deriv())  # g' = slope_num / den²
        return [slope_num - MIN_SLOPE * den * den, den - MIN_SLOPE]
    k1, k2, k3, k4 = (float(c) for c in coeffs)
    return [Polynomial([1.0 - MIN_SLOPE, 3 * k1, 5 * k2, 7 * k3, 9 * k4])]


def undistort_radius(
    rd: np.ndarray, model: str, coeffs: Sequence[float], branch: RadialBranch | None = None
) -> np.ndarray:
    """왜곡 반지름(핀홀 r_d, 어안 θ_d) → 왜곡 없는 반지름(핀홀 r, 어안 θ). 가지 밖·유한하지 않은 값은 NaN.

    g(r) = rd 를 [0, r_max) 에서 푼다. 뉴턴 걸음이 현재 구간(부호로 좁힌 [lo, hi]) 밖으로 나가면 이분법으로 바꾼다.
    가지 안에서 g 는 단조이므로 해가 하나이고 반드시 수렴한다.
    """
    rd = np.asarray(rd, dtype=float)
    if branch is None:
        branch = radial_branch(model, coeffs)
    out = np.full(rd.shape, np.nan)
    ok = np.isfinite(rd) & (rd >= 0.0) & (rd < branch.rd_max)
    if model == MODEL_NONE or (model == MODEL_PINHOLE and not coeffs):
        out[ok] = rd[ok]
        return out
    if not ok.any():
        return out
    target = rd[ok]
    lo = np.zeros_like(target)
    hi = np.full_like(target, branch.r_max)
    r = np.clip(target, lo, hi)
    active = np.arange(len(target))  # 아직 수렴하지 않은 점만 계속 계산한다
    for _ in range(RADIAL_ITERATIONS):
        ri, ti = r[active], target[active]
        g, dg = _radial(model, coeffs, ri)
        f = g - ti
        lo_i = np.where(f <= 0, ri, lo[active])
        hi_i = np.where(f > 0, ri, hi[active])
        with np.errstate(divide="ignore", invalid="ignore"):
            nxt = ri - f / dg
        inside = np.isfinite(nxt) & (nxt >= lo_i) & (nxt <= hi_i)
        nxt = np.where(inside, nxt, 0.5 * (lo_i + hi_i))
        nxt = np.where(f == 0.0, ri, nxt)
        r[active], lo[active], hi[active] = nxt, lo_i, hi_i
        done = np.abs(nxt - ri) <= CONVERGED * np.maximum(1.0, ri)
        active = active[~done]
        if len(active) == 0:
            break
    out[ok] = r
    return out


def undistort(
    xd: np.ndarray,
    model: str,
    coeffs: Sequence[float],
    max_residual: float = DEFAULT_MAX_RESIDUAL,
    branch: RadialBranch | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """왜곡 정규화 좌표 (N, 2) → (왜곡 없는 정규화 좌표 (N, 2), 성공 마스크 (N,)).

    실패한 점(입력이 유한하지 않음, 왜곡 식이 접히는 영역, 미수렴, 왕복 오차 > max_residual)은 NaN 이다.
    max_residual 은 정규화 좌표 단위의 왕복 오차 허용치다(다시 왜곡한 좌표와 xd 사이 거리).
    """
    xd = np.asarray(xd, dtype=float).reshape(-1, 2)
    if not max_residual > 0:
        raise ValueError(f"max_residual 은 양수여야 한다: {max_residual}")
    finite = np.isfinite(xd).all(axis=1)
    if model == MODEL_NONE or (model == MODEL_PINHOLE and not coeffs):
        out = xd.copy()
        out[~finite] = np.nan
        return out, finite
    if branch is None:
        branch = radial_branch(model, coeffs)
    rd = np.hypot(xd[:, 0], xd[:, 1])
    r = undistort_radius(rd, model, coeffs, branch)
    scale = np.ones_like(rd)
    nz = rd > 1e-15
    if model == MODEL_PINHOLE:
        scale[nz] = r[nz] / rd[nz]
    elif model == MODEL_FISHEYE:
        with np.errstate(invalid="ignore"):
            scale[nz] = np.tan(r[nz]) / rd[nz]
    else:
        raise ValueError(f"모르는 왜곡 모델 {model}")
    scale[~np.isfinite(r)] = np.nan
    x = xd * scale[:, None]

    ok = np.isfinite(x).all(axis=1)
    if model == MODEL_PINHOLE:
        _, _, p1, p2, *_ = _pinhole8(coeffs)
        if (p1 != 0.0 or p2 != 0.0) and ok.any():
            x[ok] = _refine_pinhole(x[ok], xd[ok], coeffs)
            ok &= np.isfinite(x).all(axis=1)
        det = np.full(len(x), np.nan)
        det[ok] = _pinhole_jacobian_det(x[ok], coeffs)
        with np.errstate(invalid="ignore"):
            ok &= (np.hypot(x[:, 0], x[:, 1]) < branch.r_max) & (det > 0)
    else:
        with np.errstate(invalid="ignore"):
            ok &= np.arctan(np.hypot(x[:, 0], x[:, 1])) < branch.r_max
    residual = np.full(len(x), np.inf)
    if ok.any():
        residual[ok] = np.hypot(*(distort(x[ok], model, coeffs) - xd[ok]).T)
    ok &= residual <= max_residual
    x[~ok] = np.nan
    return x, ok


def _pinhole8(coeffs: Sequence[float]) -> tuple[float, ...]:
    k = [float(c) for c in coeffs] + [0.0] * (8 - len(coeffs))
    return tuple(k[:8])


def _fisheye_g(theta: np.ndarray, coeffs: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    k1, k2, k3, k4 = (float(c) for c in coeffs)
    t2 = theta * theta
    g = theta * (1 + t2 * (k1 + t2 * (k2 + t2 * (k3 + t2 * k4))))
    dg = 1 + t2 * (3 * k1 + t2 * (5 * k2 + t2 * (7 * k3 + 9 * k4 * t2)))
    return g, dg


def _pinhole_parts(r: np.ndarray, coeffs: Sequence[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """반지름 r 에서 (g, g', 분모). g = r · num(s)/den(s), s = r²."""
    k1, k2, _, _, k3, k4, k5, k6 = _pinhole8(coeffs)
    s = r * r
    num = 1 + s * (k1 + s * (k2 + s * k3))
    den = 1 + s * (k4 + s * (k5 + s * k6))
    dnum = k1 + s * (2 * k2 + 3 * k3 * s)
    dden = k4 + s * (2 * k5 + 3 * k6 * s)
    with np.errstate(divide="ignore", invalid="ignore"):
        g = r * num / den
        dg = (num * den + 2 * s * (dnum * den - num * dden)) / (den * den)
    return g, dg, den


def _radial(model: str, coeffs: Sequence[float], r: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if model == MODEL_PINHOLE:
        g, dg, _ = _pinhole_parts(r, coeffs)
        return g, dg
    return _fisheye_g(r, coeffs)


def _monotone(model: str, coeffs: Sequence[float], r: np.ndarray) -> np.ndarray:
    if model == MODEL_PINHOLE:
        g, dg, den = _pinhole_parts(r, coeffs)
        return np.isfinite(g) & np.isfinite(dg) & (den > MIN_SLOPE) & (dg > MIN_SLOPE)
    g, dg = _fisheye_g(r, coeffs)
    return np.isfinite(g) & np.isfinite(dg) & (dg > MIN_SLOPE)


def _pinhole_jacobian(x: np.ndarray, coeffs: Sequence[float]) -> tuple[np.ndarray, ...]:
    """핀홀 왜곡 정방향의 야코비안 (j00, j01, j10, j11) = ∂(x_d, y_d)/∂(x, y)."""
    k1, k2, p1, p2, k3, k4, k5, k6 = _pinhole8(coeffs)
    xx, yy = x[:, 0], x[:, 1]
    s = xx * xx + yy * yy
    num = 1 + s * (k1 + s * (k2 + s * k3))
    den = 1 + s * (k4 + s * (k5 + s * k6))
    dnum = k1 + s * (2 * k2 + 3 * k3 * s)
    dden = k4 + s * (2 * k5 + 3 * k6 * s)
    radial = num / den
    drad = (dnum * den - num * dden) / (den * den)  # d radial / d s
    j00 = radial + 2 * xx * xx * drad + 2 * p1 * yy + 6 * p2 * xx
    j01 = 2 * xx * yy * drad + 2 * p1 * xx + 2 * p2 * yy
    j10 = 2 * xx * yy * drad + 2 * p1 * xx + 2 * p2 * yy
    j11 = radial + 2 * yy * yy * drad + 6 * p1 * yy + 2 * p2 * xx
    return j00, j01, j10, j11


def _pinhole_jacobian_det(x: np.ndarray, coeffs: Sequence[float]) -> np.ndarray:
    j00, j01, j10, j11 = _pinhole_jacobian(x, coeffs)
    return j00 * j11 - j01 * j10


def _refine_pinhole(x: np.ndarray, xd: np.ndarray, coeffs: Sequence[float]) -> np.ndarray:
    """접선 계수까지 포함한 F(x) = distort(x) − xd = 0 을 2차원 뉴턴법으로 푼다. 잔차가 늘면 걸음을 반으로 줄인다."""
    x = x.copy()
    res = distort(x, MODEL_PINHOLE, coeffs) - xd
    norm = np.hypot(res[:, 0], res[:, 1])
    scale = 1.0 + np.hypot(xd[:, 0], xd[:, 1])
    active = np.flatnonzero(norm > CONVERGED * scale)  # 아직 수렴하지 않은 점만 계속 계산한다
    for _ in range(REFINE_ITERATIONS):
        if len(active) == 0:
            break
        xa, ra = x[active], res[active]
        j00, j01, j10, j11 = _pinhole_jacobian(xa, coeffs)
        det = j00 * j11 - j01 * j10
        with np.errstate(divide="ignore", invalid="ignore"):
            step = np.column_stack([(j11 * ra[:, 0] - j01 * ra[:, 1]) / det, (-j10 * ra[:, 0] + j00 * ra[:, 1]) / det])
        stuck = ~np.isfinite(step).all(axis=1)
        step[stuck] = 0.0
        alpha = np.ones(len(active))
        pending = ~stuck
        for _ in range(REFINE_HALVINGS):
            sub = np.flatnonzero(pending)
            if len(sub) == 0:
                break
            cand = xa[sub] - alpha[sub, None] * step[sub]
            cres = distort(cand, MODEL_PINHOLE, coeffs) - xd[active[sub]]
            cnorm = np.hypot(cres[:, 0], cres[:, 1])
            better = np.isfinite(cnorm) & (cnorm <= norm[active[sub]])
            take = active[sub[better]]
            x[take], res[take], norm[take] = cand[better], cres[better], cnorm[better]
            pending[sub[better]] = False
            alpha[sub[~better]] *= 0.5
        # 잔차가 기준 이하가 됐거나, 걸음을 줄여도 잔차가 줄지 않으면(부동소수 한계 또는 실패) 멈춘다.
        # 실패한 점은 호출자의 왕복 오차 검사에서 걸러진다
        moved = ~pending & ~stuck
        keep = moved & (norm[active] > CONVERGED * scale[active])
        active = active[keep]
    return x
