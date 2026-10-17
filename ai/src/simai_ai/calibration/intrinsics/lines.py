"""교실 직선 후보(에지 사슬) 검출. numpy 만 쓴다(OpenCV 없이 시험할 수 있게).

렌즈 왜곡이 있으면 실제 직선이 영상에서 휘어 있으므로 "직선 선분"이 아니라 "매끄럽게 이어진 에지 사슬"을 찾는다.
왜곡 추정(plumb-line)이 사슬마다 직선성을 보고, 펴진 사슬로 소실점을 찾는다.

1. 가우시안 흐림 → Sobel 기울기.
2. 기울기 방향 비최대 억제(쌍선형 표본) + 포물선 보간으로 서브픽셀 에지점.
3. 이웃 에지점 연결: 8-이웃 중 에지 방향이 비슷하고(`link_angle_tol_rad`), 이웃 방향이 에지 접선 쪽인 것만 잇는다
   (가는 선의 양쪽 에지처럼 나란한 에지가 서로 붙지 않게).
4. 연결 성분을 3차 다항식으로 맞춰 잔차가 큰 곳(모서리·교차)에서 재귀로 자른다. 짧은 사슬은 버린다.

좌표는 OpenCV 픽셀 규약(정수 (u, v) 가 픽셀 중심, 배열 인덱스 [v, u])이다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from simai_ai.calibration.intrinsics.estimate import to_gray


@dataclass(frozen=True)
class EdgeChainConfig:
    blur_sigma_px: float = 1.0
    # 기울기 크기 임계값 = 이 비율 × 기울기 크기 99 백분위수
    threshold_ratio: float = 0.2
    link_angle_tol_rad: float = math.radians(20.0)
    # 이웃 방향과 에지 접선의 각이 이보다 크면 잇지 않는다
    link_offset_tol_rad: float = math.radians(60.0)
    min_length_px: float = 40.0
    # 3차 다항식 잔차가 이보다 크면 자른다
    split_tol_px: float = 1.5
    border_px: int = 3
    # 정수 배 축소(블록 평균) 후 검출하고 좌표를 원본으로 되돌린다. 1 이면 원본 그대로
    downsample: int = 1


def detect_edge_chains(image: np.ndarray, config: EdgeChainConfig | None = None) -> list[np.ndarray]:
    """영상 → 에지 사슬 목록. 각 사슬은 원본 픽셀 좌표 (N, 2) [u, v] 이고 사슬 방향으로 정렬돼 있다."""
    cfg = config or EdgeChainConfig()
    gray = to_gray(image)
    s = int(cfg.downsample)
    if s < 1:
        raise ValueError(f"downsample 은 1 이상이어야 한다: {s}")
    if s > 1:
        h, w = (gray.shape[0] // s) * s, (gray.shape[1] // s) * s
        gray = gray[:h, :w].reshape(h // s, s, w // s, s).mean(axis=(1, 3))
    points, tangent_angle, index_map = _subpixel_edges(gray, cfg)
    if len(points) == 0:
        return []
    labels = _link(points, tangent_angle, index_map, cfg)
    chains = _split_components(points, labels, cfg.min_length_px / s, cfg.split_tol_px / s)
    if s > 1:
        chains = [(c + 0.5) * s - 0.5 for c in chains]
    return chains


def _gaussian_blur(img: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return img
    radius = max(1, math.ceil(3 * sigma))
    x = np.arange(-radius, radius + 1, dtype=float)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    k /= k.sum()
    pad = np.pad(img, radius, mode="edge")
    tmp = sum(k[i] * pad[:, i : i + img.shape[1]] for i in range(len(k)))
    return sum(k[i] * tmp[i : i + img.shape[0], :] for i in range(len(k)))


def _bilinear(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    h, w = img.shape
    x = np.clip(x, 0, w - 1.000001)
    y = np.clip(y, 0, h - 1.000001)
    x0 = np.floor(x).astype(int)
    y0 = np.floor(y).astype(int)
    fx = x - x0
    fy = y - y0
    return (
        img[y0, x0] * (1 - fx) * (1 - fy)
        + img[y0, x0 + 1] * fx * (1 - fy)
        + img[y0 + 1, x0] * (1 - fx) * fy
        + img[y0 + 1, x0 + 1] * fx * fy
    )


def _subpixel_edges(gray: np.ndarray, cfg: EdgeChainConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    g = _gaussian_blur(gray, cfg.blur_sigma_px)
    p = np.pad(g, 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:] - p[:-2, :-2] - 2 * p[1:-1, :-2] - p[2:, :-2]) / 8.0
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:] - p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:]) / 8.0
    mag = np.hypot(gx, gy)
    h, w = mag.shape
    positive = mag[mag > 0]
    if positive.size == 0:
        return np.zeros((0, 2)), np.zeros(0), np.full((h, w), -1, dtype=np.int64)
    thr = cfg.threshold_ratio * float(np.percentile(positive, 99))
    b = cfg.border_px
    cand = np.zeros_like(mag, dtype=bool)
    cand[b : h - b, b : w - b] = mag[b : h - b, b : w - b] > thr
    vy, vx = np.nonzero(cand)
    m0 = mag[vy, vx]
    ux = gx[vy, vx] / m0
    uy = gy[vy, vx] / m0
    xf = vx.astype(float)
    yf = vy.astype(float)
    m_plus = _bilinear(mag, xf + ux, yf + uy)
    m_minus = _bilinear(mag, xf - ux, yf - uy)
    keep = (m0 >= m_plus) & (m0 > m_minus)
    vx, vy, xf, yf, ux, uy = vx[keep], vy[keep], xf[keep], yf[keep], ux[keep], uy[keep]
    m0, m_plus, m_minus = m0[keep], m_plus[keep], m_minus[keep]
    denom = m_minus - 2 * m0 + m_plus
    with np.errstate(divide="ignore", invalid="ignore"):
        delta = np.where(np.abs(denom) > 1e-12, 0.5 * (m_minus - m_plus) / denom, 0.0)
    delta = np.clip(delta, -0.5, 0.5)
    points = np.column_stack([xf + delta * ux, yf + delta * uy])
    # 에지 접선 방향 = 기울기에 수직. [0, π) 로 접는다
    tangent = np.mod(np.arctan2(uy, ux) + math.pi / 2, math.pi)
    index_map = np.full((h, w), -1, dtype=np.int64)
    index_map[vy, vx] = np.arange(len(vx))
    return points, tangent, index_map


def _link(points: np.ndarray, tangent: np.ndarray, index_map: np.ndarray, cfg: EdgeChainConfig) -> np.ndarray:
    h, w = index_map.shape
    vy, vx = np.nonzero(index_map >= 0)
    src_all = index_map[vy, vx]
    pairs_i: list[np.ndarray] = []
    pairs_j: list[np.ndarray] = []
    cos_offset = math.cos(cfg.link_offset_tol_rad)
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        ny, nx = vy + dy, vx + dx
        inside = (ny >= 0) & (ny < h) & (nx >= 0) & (nx < w)
        src = src_all[inside]
        dst = index_map[ny[inside], nx[inside]]
        ok = dst >= 0
        src, dst = src[ok], dst[ok]
        diff = np.abs(tangent[src] - tangent[dst])
        diff = np.minimum(diff, math.pi - diff)
        mean_t = tangent[src]
        off = np.array([dx, dy], dtype=float) / math.hypot(dx, dy)
        align = np.abs(np.cos(mean_t) * off[0] + np.sin(mean_t) * off[1])
        good = (diff < cfg.link_angle_tol_rad) & (align > cos_offset)
        pairs_i.append(src[good])
        pairs_j.append(dst[good])
    i = np.concatenate(pairs_i)
    j = np.concatenate(pairs_j)
    labels = np.arange(len(points))
    # 최소 라벨 전파 + 포인터 점프 (연결 성분)
    for _ in range(10_000):
        m = np.minimum(labels[i], labels[j])
        new = labels.copy()
        np.minimum.at(new, i, m)
        np.minimum.at(new, j, m)
        new = new[new]
        if np.array_equal(new, labels):
            break
        labels = new
    return labels


def _split_components(points: np.ndarray, labels: np.ndarray, min_length: float, tol: float) -> list[np.ndarray]:
    order = np.argsort(labels, kind="stable")
    sorted_labels = labels[order]
    starts = np.flatnonzero(np.r_[True, sorted_labels[1:] != sorted_labels[:-1]])
    ends = np.r_[starts[1:], len(order)]
    chains: list[np.ndarray] = []
    min_points = max(5, int(min_length * 0.7))
    for s, e in zip(starts, ends, strict=True):
        if e - s < min_points:
            continue
        _split(points[order[s:e]], min_length, tol, chains, depth=0)
    return chains


def _principal_frame(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = pts.mean(axis=0)
    c = pts - mean
    cov = c.T @ c
    _evals, evecs = np.linalg.eigh(cov)
    d = evecs[:, 1]
    n = evecs[:, 0]
    return mean, d, n


def _split(pts: np.ndarray, min_length: float, tol: float, out: list[np.ndarray], depth: int) -> None:
    if len(pts) < 5:
        return
    mean, d, n = _principal_frame(pts)
    u = (pts - mean) @ d
    order = np.argsort(u)
    pts, u = pts[order], u[order]
    if u[-1] - u[0] < min_length:
        return
    wv = (pts - mean) @ n
    scale = max(1.0, float(np.abs(u).max()))
    coef = np.polyfit(u / scale, wv, 3)
    resid = np.abs(wv - np.polyval(coef, u / scale))
    k = int(np.argmax(resid))
    # 같은 위치에 겹친 점(가는 선 양쪽이 아니라 같은 에지의 중복)이나 끊긴 틈도 자른다
    gaps = np.hypot(*np.diff(pts, axis=0).T)
    gap_k = int(np.argmax(gaps)) if len(gaps) else 0
    if gaps.size and gaps[gap_k] > 3.0 and depth < 64:
        _split(pts[: gap_k + 1], min_length, tol, out, depth + 1)
        _split(pts[gap_k + 1 :], min_length, tol, out, depth + 1)
        return
    if resid[k] <= tol:
        out.append(pts)
        return
    if depth >= 64:
        return
    k = min(max(k, 2), len(pts) - 3)
    _split(pts[:k], min_length, tol, out, depth + 1)
    _split(pts[k + 1 :], min_length, tol, out, depth + 1)
