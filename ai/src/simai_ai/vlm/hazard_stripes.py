"""노란·검은 줄무늬(안전 테이프·난간) 픽셀 검출과 화면 다각형 비율 계산 (S15P31A101-671).

AI 구역 초안이 시연 개구부(단상 둘레 노랑·검정 테이프, 가운데 줄무늬 문틀형 난간)를 고를 때 쓰는 화면 근거다.
- 입력은 얼굴 처리 관문을 거친 `VlmFrame.pixels`(BGR uint8)만 쓴다(AGENTS 규칙 8). 픽셀을 저장·전송하지 않는다.
- 단순 색 규칙이다: 노랑(HSV 색상 42~72°, 채도 0.30 이상, 명도 0.55 이상)과 어두움(명도 0.35 이하)이 한 창 안에 함께
  10% 이상 섞인 자리의 노랑·어두운 픽셀을 줄무늬로 본다. 수치 근거는 14차 로비 녹화 네 카메라 사진(설계 메모).
- numpy 만 쓴다(OpenCV 없이 돈다). 근거가 없으면 0 을 돌려주고 지어내지 않는다(AGENTS 규칙 2).
- 위치·판정 정본이 아니다. 초안 후보 순위와 VLM 입력 수치로만 쓴다(AGENTS 규칙 1·4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class StripeConfig:
    """줄무늬 색 규칙. 색상은 도(0~360), 채도·명도는 0~1."""

    yellow_hue_min_deg: float = 42.0
    yellow_hue_max_deg: float = 72.0
    yellow_sat_min: float = 0.30
    yellow_val_min: float = 0.55
    dark_val_max: float = 0.35
    # 창 한 변 = 프레임 너비 / window_div(홀수, 최소 5 px). 1152 px 프레임에서 9 px 로 테이프 줄 2~3칸이 들어간다.
    window_div: float = 128.0
    min_mix_ratio: float = 0.10


def _hsv(bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """BGR uint8 → 색상(도)·채도·명도(0~1)."""
    img = np.asarray(bgr, dtype=np.float32) / 255.0
    b, g, r = img[..., 0], img[..., 1], img[..., 2]
    vmax = np.maximum(np.maximum(r, g), b)
    vmin = np.minimum(np.minimum(r, g), b)
    delta = vmax - vmin
    sat = np.where(vmax > 1e-6, delta / np.maximum(vmax, 1e-6), 0.0)
    safe = np.where(delta > 1e-6, delta, 1.0)
    hue = np.where(
        vmax == r,
        ((g - b) / safe) % 6.0,
        np.where(vmax == g, (b - r) / safe + 2.0, (r - g) / safe + 4.0),
    )
    hue = np.where(delta > 1e-6, hue * 60.0, 0.0)
    return hue, sat, vmax


def _box_mean(mask: np.ndarray, k: int) -> np.ndarray:
    """k×k 창 평균(경계는 창 안에 든 칸만 센다). 적분 영상으로 계산한다."""
    h, w = mask.shape
    r = k // 2
    integ = np.zeros((h + 1, w + 1), dtype=np.float64)
    integ[1:, 1:] = np.cumsum(np.cumsum(mask.astype(np.float64), axis=0), axis=1)
    ys = np.arange(h)
    xs = np.arange(w)
    y0 = np.clip(ys - r, 0, h)[:, None]
    y1 = np.clip(ys + r + 1, 0, h)[:, None]
    x0 = np.clip(xs - r, 0, w)[None, :]
    x1 = np.clip(xs + r + 1, 0, w)[None, :]
    total = integ[y1, x1] - integ[y0, x1] - integ[y1, x0] + integ[y0, x0]
    area = (y1 - y0) * (x1 - x0)
    return total / np.maximum(area, 1)


def stripe_window_px(width: int, cfg: StripeConfig) -> int:
    k = max(5, round(width / cfg.window_div))
    return k if k % 2 == 1 else k + 1


def hazard_stripe_mask(bgr: np.ndarray, cfg: StripeConfig | None = None) -> np.ndarray:
    """얼굴 처리된 BGR 프레임에서 노랑·검정 줄무늬 픽셀 마스크(bool, H×W)를 돌려준다."""
    c = cfg or StripeConfig()
    img = np.asarray(bgr)
    if img.ndim != 3 or img.shape[2] < 3 or img.shape[0] < 3 or img.shape[1] < 3:
        return np.zeros(img.shape[:2], dtype=bool)
    hue, sat, val = _hsv(img[..., :3])
    yellow = (hue >= c.yellow_hue_min_deg) & (hue <= c.yellow_hue_max_deg) & (sat >= c.yellow_sat_min) & (val >= c.yellow_val_min)
    dark = val <= c.dark_val_max
    if not yellow.any():
        return np.zeros(img.shape[:2], dtype=bool)
    k = stripe_window_px(img.shape[1], c)
    mixed = (_box_mean(yellow, k) >= c.min_mix_ratio) & (_box_mean(dark, k) >= c.min_mix_ratio)
    return mixed & (yellow | dark)


def convex_hull(points: np.ndarray) -> np.ndarray:
    """2D 점들의 볼록 껍질(반시계, 단조 사슬). 점이 3개 미만이거나 한 줄이면 빈 (0, 2)."""
    pts = np.unique(np.asarray(points, dtype=np.float64).reshape(-1, 2), axis=0)
    if len(pts) < 3:
        return np.zeros((0, 2), dtype=np.float64)
    order = np.lexsort((pts[:, 1], pts[:, 0]))
    pts = pts[order]

    def cross(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
        return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))

    lower: list[np.ndarray] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[np.ndarray] = []
    for p in pts[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = np.array(lower[:-1] + upper[:-1], dtype=np.float64)
    return hull if len(hull) >= 3 else np.zeros((0, 2), dtype=np.float64)


def convex_polygon_mask(hull: np.ndarray, shape: Sequence[int]) -> np.ndarray:
    """반시계 볼록 다각형 안의 픽셀 마스크(bool, shape[:2]). 화면 밖은 잘린다."""
    h, w = int(shape[0]), int(shape[1])
    out = np.zeros((h, w), dtype=bool)
    poly = np.asarray(hull, dtype=np.float64).reshape(-1, 2)
    if len(poly) < 3 or h == 0 or w == 0:
        return out
    x0 = max(0, int(np.floor(poly[:, 0].min())))
    x1 = min(w, int(np.ceil(poly[:, 0].max())) + 1)
    y0 = max(0, int(np.floor(poly[:, 1].min())))
    y1 = min(h, int(np.ceil(poly[:, 1].max())) + 1)
    if x0 >= x1 or y0 >= y1:
        return out
    gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
    inside = np.ones(gx.shape, dtype=bool)
    for i in range(len(poly)):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % len(poly)]
        inside &= (bx - ax) * (gy - ay) - (by - ay) * (gx - ax) >= 0
    out[y0:y1, x0:x1] = inside
    return out
