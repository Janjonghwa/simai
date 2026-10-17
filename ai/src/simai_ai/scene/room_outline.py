"""BEV 평면도의 대략적 방 외곽 (명세 4.1 BEV 평면도, 6.3 방 형상 제한).

관리자 확인용 대략 추정이다. 정확도를 주장하지 않으며 결과는 늘 `source = inferred` 다.

- 외곽 = BEV 에서 카메라가 바닥을 본 픽셀(coverage) 합집합의 볼록 껍질이다. 오목한 방(ㄱ자 등)은 실제보다
  넓게 나온다(바깥쪽으로 틀린다).
- 근거(`basis`): 카메라별 바닥 마스크를 받았으면 FLOOR_MASK, 아니면 CAMERA_FOOTPRINT 다. CAMERA_FOOTPRINT 는
  벽에 찍힌 픽셀도 바닥으로 펴진 것이라 방 외곽이 아니라 카메라 시야 범위에 가깝다.
- 문·개구부는 추출하지 않는다(`openings = null`, 모름).
- UNKNOWN_OCCUPANCY 의 방 형상 제한(명세 6.3, v1.3 에서 확장 트랙)에 쓸 때는 이 외곽을 그대로 쓰지 않고
  `expand_outline` 으로 바깥으로 넉넉히 넓힌다. 방 형상 제한은 불확실 점유 볼륨을 무한 기둥이 아니게 하는
  상한일 뿐이므로, 외곽 추정이 틀려도 점유 가능 영역을 잘라내지 않는 쪽(넓히는 쪽)으로 오차를 둔다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from simai_ai.scene.bev import BevRaster

OUTLINE_BASES = ("FLOOR_MASK", "CAMERA_FOOTPRINT")


def convex_outline_from_mask(mask: np.ndarray, raster: BevRaster, *, basis: str) -> dict[str, Any]:
    """BEV 마스크 (H, W) 의 참 픽셀을 모두 덮는 볼록 다각형(World, 반시계)을 대략 외곽으로 낸다."""
    if basis not in OUTLINE_BASES:
        raise ValueError(f"모르는 외곽 근거: {basis} (허용: {OUTLINE_BASES})")
    mask = np.asarray(mask, dtype=bool)
    if mask.shape != (raster.height, raster.width):
        raise ValueError(f"마스크 크기 {mask.shape} 가 BEV ({raster.height}, {raster.width}) 와 다르다")
    rows = np.flatnonzero(mask.any(axis=1))
    if len(rows) == 0:
        raise ValueError("참 픽셀이 없는 마스크다")
    # 행마다 가장 왼쪽·오른쪽 참 픽셀의 바깥 모서리 네 점이면 볼록 껍질이 같다.
    sub = mask[rows]
    first = sub.argmax(axis=1)
    last = raster.width - 1 - sub[:, ::-1].argmax(axis=1)
    m = raster.meters_per_pixel
    corners = []
    for cols in (first, last + 1):
        for dr in (0, 1):
            x = raster.min_x + cols * m
            y = raster.max_y - (rows + dr) * m
            corners.append(np.column_stack([x, y]))
    hull = convex_hull(np.concatenate(corners))
    return {
        "source": "inferred",
        "basis": basis,
        "method": "CONVEX_HULL_OF_COVERAGE",
        "accuracy": "NOT_CLAIMED",
        "polygon_xy": [[float(x), float(y)] for x, y in hull],
        "openings": None,
    }


def expand_outline(polygon_xy: Sequence[Sequence[float]], margin_m: float, *, segments: int = 16) -> list[list[float]]:
    """볼록 다각형을 margin_m 만큼 바깥으로 넓힌다(원판과의 민코프스키 합, 원은 segments 각형으로 외접 근사).

    UNKNOWN_OCCUPANCY 방 형상 제한에 쓸 때 부르는 함수다. 원을 외접 다각형으로 근사하므로 결과는 늘
    정확한 오프셋 이상으로 넓다.
    """
    if not margin_m >= 0.0:
        raise ValueError(f"margin_m 은 0 이상이어야 한다: {margin_m}")
    if segments < 3:
        raise ValueError(f"segments 는 3 이상이어야 한다: {segments}")
    pts = np.asarray(polygon_xy, dtype=float).reshape(-1, 2)
    if len(pts) < 3:
        raise ValueError("다각형은 점이 3개 이상이어야 한다")
    if margin_m == 0.0:
        return [[float(x), float(y)] for x, y in convex_hull(pts)]
    radius = margin_m / math.cos(math.pi / segments)  # 외접 다각형: 변의 중점이 margin_m 거리
    angles = np.arange(segments) * (2 * math.pi / segments)
    ring = radius * np.column_stack([np.cos(angles), np.sin(angles)])
    grown = (pts[:, None, :] + ring[None, :, :]).reshape(-1, 2)
    return [[float(x), float(y)] for x, y in convex_hull(grown)]


def convex_hull(points: np.ndarray) -> np.ndarray:
    """2D 볼록 껍질(Andrew monotone chain). 반시계 순서, 시작점을 반복하지 않는다."""
    pts = np.unique(np.asarray(points, dtype=float).reshape(-1, 2), axis=0)
    if len(pts) < 3:
        return pts

    def half(seq: np.ndarray) -> list[np.ndarray]:
        chain: list[np.ndarray] = []
        for p in seq:
            while len(chain) >= 2 and _cross(chain[-2], chain[-1], p) <= 0.0:
                chain.pop()
            chain.append(p)
        return chain

    lower = half(pts)
    upper = half(pts[::-1])
    return np.array(lower[:-1] + upper[:-1])


def _cross(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))
