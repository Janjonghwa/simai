"""관제 화면 바닥 감시 범위 (S15P31A101-606, 명세 4.4 걸을 수 있는 바닥, 4.5 설치 안내, 5.2 2대 이상 삼각측량, 13장 관제 뷰어).

정적 3D 맵은 휴대폰 보충 촬영(-605)으로 채워지므로, 맵이 보인다고 그 바닥을 카메라가 감시하는 것이 아니다. 관제자가 맵을
감시 범위로 오해하지 않게 바닥 칸마다 그 자리에 선 사람을 통째로 보는 카메라 수(0 / 1 / 2대 이상)를 표시 자료로 만든다.

1. 바닥 영역: 세팅 MapAnything 점구름을 World(미터, +Z 상방, 바닥 Z=0)로 옮긴 뒤 Z=0 근처 점으로 10 cm 점유 격자를 만들고,
   작은 틈을 닫은 다음 가장 큰 연결 영역 하나만 남기고 그 안의 구멍은 메운다. 구멍(책상 밑·기둥 뒤처럼 카메라에 바닥 점이 안 잡힌
   자리)은 방 안이므로 기하 커버리지로 평가한다(가림을 안 보는 상한). 바깥 경계는 늘리지 않는다: 어느 카메라에도 바닥으로 보이지
   않은 곳은 바닥인지 모르므로 비운다(AGENTS 규칙 2, 방 전체로 가정하지 않는다).
2. 커버리지: `coverage.floor_camera_counts`(설치 안내 `coverage_guidance` 와 같은 투영·발목/머리 높이 기준)로 30 cm 칸마다 센다.

이 자료는 Visual 층 표시용이다. Tier 0 위치·위험 판정·보정 상태에 쓰지 않는다. 가림(책상·사람)은 보지 않아 실제보다 넓게
나올 수 있다(`is_upper_bound`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from simai_ai.calibration.coverage import CoverageConfig, floor_camera_counts

FLOOR_CELL_M = 0.10
"""바닥 점유 격자 칸. 사람 발 크기(약 25 cm)보다 작고, 세팅 영상 해상도의 바닥 점 간격(먼 바닥에서 10 cm 남짓)과 비슷하다"""
FLOOR_Z_BAND_M = 0.05
"""바닥 점으로 볼 |Z| 상한. World 바닥 평면 허용 오차(WorldFrameConfig.plane_tol_m 3 cm)에 정렬 잔차 여유를 더했다"""
FLOOR_MIN_POINTS = 1
"""칸을 바닥으로 볼 최소 점 수. 먼 바닥은 점이 성기므로 1 점이면 본다(외톨이 점은 가장 큰 연결 영역 고르기에서 빠진다)"""
FLOOR_CLOSE_ITERATIONS = 2
"""틈 닫기 반복 수. 10 cm 칸에서 약 40 cm 까지의 점 빈틈(먼 바닥의 줄 간격·작은 가림)을 잇는다"""
FLOOR_MIN_AREA_M2 = 1.0
"""이보다 작은 바닥 영역은 바닥 추출 실패로 보고 비운다"""
FLOOR_MAX_GRID_CELLS = 4_000_000
"""점유 격자 상한(10 cm 칸 200 m × 200 m). 넘으면 먼 외톨이 점 때문으로 보고 축별 0.5~99.5 백분위 밖 점을 버린다"""

COVERAGE_BLOCK = 3
"""커버리지 칸 = 바닥 칸 3 × 3 = 30 cm. 사람 어깨 폭(약 45 cm)보다 작아 사각 경계가 보이고, 교실 10 × 8 m 가 약 900 칸이다"""
COVERAGE_MIN_FLOOR_SUBCELLS = 5
"""커버리지 칸 9 개 바닥 칸 중 이만큼(과반)이 바닥이어야 그 칸을 바닥으로 그린다"""
MAX_COVERAGE_CELLS = 40_000
"""계약 site_setup#/$defs/floor_coverage cells maxLength. 30 cm 칸이면 60 m × 60 m 다"""
FLOOR_SOURCE = "MAPANYTHING_FLOOR_POINTS"
"""바닥 영역의 출처(관측 점). 계약 floor_source enum"""

NOT_FLOOR = "."
"""cells 문자: 바닥을 모르는 칸(그리지 않는다)"""


@dataclass(frozen=True)
class FloorMask:
    """World XY 바닥 점유 격자. mask[row, col] 은 칸 (origin_x + col·cell_m, origin_y + row·cell_m) 에서 cell_m 크기다."""

    origin_xy: tuple[float, float]
    cell_m: float
    mask: np.ndarray

    @property
    def area_m2(self) -> float:
        return float(np.count_nonzero(self.mask)) * self.cell_m**2


def floor_mask_from_points(
    points_world: np.ndarray,
    *,
    cell_m: float = FLOOR_CELL_M,
    z_band_m: float = FLOOR_Z_BAND_M,
    min_points: int = FLOOR_MIN_POINTS,
    close_iterations: int = FLOOR_CLOSE_ITERATIONS,
    min_area_m2: float = FLOOR_MIN_AREA_M2,
) -> FloorMask | None:
    """World 점 (N, 3) 에서 관측된 바닥 영역. 바닥 점이 없거나 영역이 min_area_m2 보다 작으면 None(모르면 비운다)."""
    from scipy import (
        ndimage,  # calibration extra. 함수 안에서만 읽는다(선택 의존성 지연 import)
    )

    pts = np.asarray(points_world, dtype=float).reshape(-1, 3)
    pts = pts[np.isfinite(pts).all(axis=1)]
    xy = pts[np.abs(pts[:, 2]) <= z_band_m, :2]
    if len(xy) == 0:
        return None
    lo, hi = xy.min(axis=0), xy.max(axis=0)
    if np.prod(np.floor((hi - lo) / cell_m) + 1) > FLOOR_MAX_GRID_CELLS:
        keep_lo, keep_hi = np.percentile(xy, 0.5, axis=0), np.percentile(xy, 99.5, axis=0)
        xy = xy[((xy >= keep_lo) & (xy <= keep_hi)).all(axis=1)]
        lo, hi = xy.min(axis=0), xy.max(axis=0)
        if np.prod(np.floor((hi - lo) / cell_m) + 1) > FLOOR_MAX_GRID_CELLS:
            return None
    origin = np.floor(lo / cell_m) * cell_m
    ij = np.floor((xy - origin) / cell_m).astype(np.int64)
    cols, rows = int(ij[:, 0].max()) + 1, int(ij[:, 1].max()) + 1
    counts = np.zeros((rows, cols), dtype=np.int64)
    np.add.at(counts, (ij[:, 1], ij[:, 0]), 1)
    occupied = counts >= min_points
    if close_iterations > 0:
        # 가장자리에서 닫기가 바닥을 깎지 않게 둘레를 비워 두고 닫는다
        pad = close_iterations
        padded = np.pad(occupied, pad)
        padded = ndimage.binary_closing(padded, structure=np.ones((3, 3), bool), iterations=close_iterations)
        occupied = padded[pad:-pad, pad:-pad]
    labels, n = ndimage.label(occupied)  # 4-연결: 대각선 한 칸으로 이어진 외톨이는 따로 본다
    if n == 0:
        return None
    sizes = np.bincount(labels.ravel())[1:]
    region = ndimage.binary_fill_holes(labels == int(np.argmax(sizes)) + 1)
    if float(np.count_nonzero(region)) * cell_m**2 < min_area_m2:
        return None
    rr, cc = np.nonzero(region)
    r0, r1, c0, c1 = int(rr.min()), int(rr.max()) + 1, int(cc.min()), int(cc.max()) + 1
    return FloorMask(
        origin_xy=(float(origin[0] + c0 * cell_m), float(origin[1] + r0 * cell_m)),
        cell_m=cell_m,
        mask=region[r0:r1, c0:c1].copy(),
    )


def floor_coverage_record(
    calibration: Mapping[str, Any],
    floor: FloorMask,
    *,
    block: int = COVERAGE_BLOCK,
    min_floor_subcells: int = COVERAGE_MIN_FLOOR_SUBCELLS,
    config: CoverageConfig = CoverageConfig(),
) -> dict[str, Any] | None:
    """바닥 영역과 보정 문서로 계약 `site_setup#/$defs/floor_coverage` 문서를 만든다. 그릴 칸이 없거나 너무 크면 None.

    cells 는 행 우선 문자열이다: 행 0 이 가장 작은 Y, 열 0 이 가장 작은 X. "." 바닥 모름, "0" 사각, "1" 1대만, "2" 2대 이상
    (명세 5.2 삼각측량 가능, CoverageConfig.min_cameras 와 같다).
    """
    if block < 1 or not 1 <= min_floor_subcells <= block * block:
        raise ValueError("block 은 1 이상, min_floor_subcells 는 1~block² 이어야 한다")
    if config.min_cameras != 2:
        raise ValueError("cells 문자 '2' 는 2대 이상이다. min_cameras 는 2 여야 한다")
    mask = np.asarray(floor.mask, dtype=bool)
    rows, cols = math.ceil(mask.shape[0] / block), math.ceil(mask.shape[1] / block)
    if rows * cols > MAX_COVERAGE_CELLS or rows * cols == 0:
        return None
    padded = np.zeros((rows * block, cols * block), dtype=bool)
    padded[: mask.shape[0], : mask.shape[1]] = mask
    sub = padded.reshape(rows, block, cols, block).sum(axis=(1, 3))
    is_floor = sub >= min_floor_subcells
    if not is_floor.any():
        return None
    cell_m = round(floor.cell_m * block, 4)
    origin = (round(floor.origin_xy[0], 4), round(floor.origin_xy[1], 4))
    rr, cc = np.nonzero(is_floor)
    centers = np.column_stack([origin[0] + (cc + 0.5) * cell_m, origin[1] + (rr + 0.5) * cell_m])
    counts = floor_camera_counts(calibration, centers, config)
    chars = np.full((rows, cols), NOT_FLOOR, dtype="<U1")
    chars[rr, cc] = np.minimum(counts, 2).astype(str)
    return {
        "calibration_version": str(calibration["calibration_version"]),
        "cell_m": cell_m,
        "origin_xy": [origin[0], origin[1]],
        "cols": cols,
        "rows": rows,
        "cells": "".join(chars.ravel().tolist()),
        "ankle_height_m": config.ankle_height_m,
        "head_height_m": config.head_height_m,
        "floor_source": FLOOR_SOURCE,
        "is_upper_bound": True,
    }


def floor_coverage_from_points(
    calibration: Mapping[str, Any], points_world: np.ndarray, *, config: CoverageConfig = CoverageConfig()
) -> dict[str, Any] | None:
    """World 점구름과 보정 문서로 바닥 감시 범위 문서. 바닥을 모르면 None."""
    floor = floor_mask_from_points(points_world)
    return None if floor is None else floor_coverage_record(calibration, floor, config=config)
