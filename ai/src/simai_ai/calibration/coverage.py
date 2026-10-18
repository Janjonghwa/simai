"""커버리지 기반 설치 안내와 걷기 경로 안내 (명세 4.5 "카메라 3번이 너무 아래를 봄" 같은 설치 안내와 걷기 경로 안내,
4.4 걸을 수 있는 바닥 80% 기준, 5.2 2대 이상 삼각측량, 18장 DRIFTED 운영).

두 가지 커버리지를 본다.

1. 기하 커버리지(설치 안내): 보정(K·왜곡·R·t)과 걸을 수 있는 바닥 다각형으로 바닥 칸마다 그 자리에 선 사람이 통째로
   (발목 높이 점과 머리 높이 점이 모두) 영상 안에 보이는 카메라 수를 센다. 0대인 칸은 사각, min_cameras(5.2 삼각측량
   2대) 미만인 칸은 약한 영역이다. 카메라마다 발목은 보이는데 머리가 영상 위로 잘리는 칸이 많으면 "너무 아래를 봄",
   머리는 보이는데 발목이 영상 아래로 잘리는 칸이 많으면 "너무 위를 봄"이다. 가림(책상·사람)은 보지 않는다(상한).
2. 관측 커버리지(걷기 안내): 실제로 지나간 사람의 관측. 카메라 영상을 4x3 칸으로 나눠 keypoint 가 있는 칸 비율
   (번들 조정의 왜곡·초점 조건과 같은 격자·기준)과, 바닥 칸 중 사람 위치(삼각측량한 발목·골반 XY 등)가 관측된 칸.
   기하로는 2대 이상이 보는데 아직 아무도 지나가지 않은 칸을 걷기 경로로 안내한다.

- 모르면 비운다: 걸을 수 있는 바닥 다각형이 없으면 바닥 안내를 만들지 않는다(교실 전체로 가정하지 않는다).
  관측을 주지 않은 항목은 관측 안내를 만들지 않는다. 보고의 `floor_evaluated`·`observed_evaluated` 로 구분한다.
- 안내는 제안이다. 위험 판정·보정 상태·보정 버전을 바꾸지 않는다.
- 투영은 렌즈 왜곡을 포함해 실제 영상 경계와 비교한다. 왜곡 식이 접히는 반지름 너머는 영상 밖으로 본다.
- World 는 미터·오른손·+Z 상방, 바닥 Z=0 이다. 지지 표면(단상 등) 위 칸은 따로 보지 않는다(바닥만).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from simai_ai.calibration.bundle.checks import IMAGE_GRID, image_coverage
from simai_ai.geometry import distortion
from simai_ai.geometry.rig import RigCamera
from simai_ai.providers.contract import is_finite_number


class GuidanceCode(StrEnum):
    FLOOR_BLIND_REGION = "FLOOR_BLIND_REGION"
    """걸을 수 있는 바닥 중 어느 카메라에도 사람이 통째로 보이지 않는 칸 (설치 안내)"""
    FLOOR_WEAK_REGION = "FLOOR_WEAK_REGION"
    """1대 이상 min_cameras 대 미만에서만 보이는 칸: 위치를 삼각측량할 수 없다 (설치 안내)"""
    CAMERA_AIM_TOO_LOW = "CAMERA_AIM_TOO_LOW"
    """카메라가 너무 아래를 본다: 발은 보이는데 머리가 영상 위로 잘리는 칸이 많다 (위로 기울일 것)"""
    CAMERA_AIM_TOO_HIGH = "CAMERA_AIM_TOO_HIGH"
    """카메라가 너무 위를 본다: 머리는 보이는데 발이 영상 아래로 잘리는 칸이 많다 (아래로 기울일 것)"""
    CAMERA_NOT_COVERING_FLOOR = "CAMERA_NOT_COVERING_FLOOR"
    """걸을 수 있는 바닥 어디에서도 사람이 통째로 보이지 않는다 (방향 확인)"""
    CAMERA_NO_OBSERVATIONS = "CAMERA_NO_OBSERVATIONS"
    """관측 기간에 이 카메라에 사람 keypoint 가 하나도 없다 (연결·방향 확인)"""
    CAMERA_LOW_IMAGE_COVERAGE = "CAMERA_LOW_IMAGE_COVERAGE"
    """관측이 영상 일부에만 몰려 있다: 영상 가장자리까지 걸어야 왜곡·초점이 정해진다 (걷기 안내)"""
    WALK_PATH_NEEDED = "WALK_PATH_NEEDED"
    """2대 이상이 보는 바닥인데 아직 사람이 지나가지 않은 칸 (걷기 경로 안내)"""


class GuidanceKind(StrEnum):
    INSTALLATION = "INSTALLATION"
    WALK = "WALK"


_KIND = {
    GuidanceCode.FLOOR_BLIND_REGION: GuidanceKind.INSTALLATION,
    GuidanceCode.FLOOR_WEAK_REGION: GuidanceKind.INSTALLATION,
    GuidanceCode.CAMERA_AIM_TOO_LOW: GuidanceKind.INSTALLATION,
    GuidanceCode.CAMERA_AIM_TOO_HIGH: GuidanceKind.INSTALLATION,
    GuidanceCode.CAMERA_NOT_COVERING_FLOOR: GuidanceKind.INSTALLATION,
    GuidanceCode.CAMERA_NO_OBSERVATIONS: GuidanceKind.INSTALLATION,
    GuidanceCode.CAMERA_LOW_IMAGE_COVERAGE: GuidanceKind.WALK,
    GuidanceCode.WALK_PATH_NEEDED: GuidanceKind.WALK,
}


@dataclass(frozen=True)
class CoverageConfig:
    cell_m: float = 0.5
    """바닥 칸 크기 (번들 조정 바닥 커버리지와 같다)"""
    min_cameras: int = 2
    """위치를 삼각측량하려면 2대 이상에서 보여야 한다 (명세 5.2)"""
    ankle_height_m: float = 0.1
    head_height_m: float = 1.7
    """사람이 통째로 보인다고 볼 머리 높이. 시연 대상(성인 키)의 기본값"""
    image_margin_px: float = 0.0
    aim_cut_ratio: float = 0.3
    """머리(또는 발)가 잘리는 칸이 카메라가 보는 칸의 이 비율 이상이면 방향 안내를 낸다"""
    min_image_coverage: float = 0.25
    """번들 조정 min_image_coverage 와 같다"""
    max_cells_listed: int = 50
    """안내 하나에 넣는 칸 수 상한 (나머지는 개수만)"""

    def __post_init__(self) -> None:
        for name in ("cell_m", "ankle_height_m", "head_height_m"):
            value = getattr(self, name)
            if not is_finite_number(value) or value <= 0:
                raise ValueError(f"CoverageConfig.{name} 는 유한한 양수여야 한다: {value!r}")
        if not self.head_height_m > self.ankle_height_m:
            raise ValueError("head_height_m 는 ankle_height_m 보다 커야 한다")
        for name in ("aim_cut_ratio", "min_image_coverage"):
            value = getattr(self, name)
            if not is_finite_number(value) or not 0 <= value <= 1:
                raise ValueError(f"CoverageConfig.{name} 는 0~1 이어야 한다: {value!r}")
        if not is_finite_number(self.image_margin_px) or self.image_margin_px < 0:
            raise ValueError(f"image_margin_px 는 0 이상이어야 한다: {self.image_margin_px!r}")
        if self.min_cameras < 1 or self.max_cells_listed < 0:
            raise ValueError("min_cameras 는 1 이상, max_cells_listed 는 0 이상이어야 한다")


@dataclass(frozen=True)
class Guidance:
    """구조화한 안내 하나. message 는 화면 문구(한국어), 나머지는 기계가 읽는 근거다."""

    code: GuidanceCode
    message: str
    camera_id: str | None = None
    cells_xy: tuple[tuple[float, float], ...] = ()
    """해당 바닥 칸 중심 (World XY, m). max_cells_listed 개까지"""
    cell_count: int = 0
    bbox_xy: tuple[float, float, float, float] | None = None
    """해당 칸 전체를 덮는 사각형 (x_min, y_min, x_max, y_max)"""
    metrics: Mapping[str, float] = field(default_factory=dict)

    @property
    def kind(self) -> GuidanceKind:
        return _KIND[self.code]

    def to_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {"code": str(self.code), "kind": str(self.kind), "message": self.message}
        if self.camera_id is not None:
            rec["camera_id"] = self.camera_id
        if self.cell_count:
            rec["cell_count"] = self.cell_count
            rec["cells_xy"] = [[round(x, 3), round(y, 3)] for x, y in self.cells_xy]
        if self.bbox_xy is not None:
            rec["bbox_xy"] = [round(v, 3) for v in self.bbox_xy]
        if self.metrics:
            rec["metrics"] = {k: round(float(v), 4) for k, v in self.metrics.items()}
        return rec


@dataclass(frozen=True)
class CameraCoverage:
    """카메라 한 대의 커버리지. 계산하지 않은 값은 None."""

    camera_id: str
    floor_cells_visible: int | None = None
    """사람이 통째로 보이는 걸을 수 있는 바닥 칸 수"""
    head_cut_ratio: float | None = None
    feet_cut_ratio: float | None = None
    image_coverage: float | None = None
    observed_points: int | None = None


@dataclass(frozen=True)
class CoverageReport:
    floor_evaluated: bool
    observed_evaluated: bool
    cell_m: float
    walkable_cells: int
    cells_by_camera_count: Mapping[int, int]
    """보이는 카메라 수 → 칸 수 (기하)"""
    triangulable_ratio: float | None
    """min_cameras 대 이상에서 보이는 칸 비율 (기하). 바닥을 모르면 None"""
    observed_floor_ratio: float | None
    """사람 위치가 관측된 칸 비율. 바닥이나 관측을 모르면 None"""
    cameras: tuple[CameraCoverage, ...]
    guidance: tuple[Guidance, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "floor_evaluated": self.floor_evaluated,
            "observed_evaluated": self.observed_evaluated,
            "cell_m": self.cell_m,
            "walkable_cells": self.walkable_cells,
            "cells_by_camera_count": {str(k): v for k, v in sorted(self.cells_by_camera_count.items())},
            "triangulable_ratio": None if self.triangulable_ratio is None else round(self.triangulable_ratio, 4),
            "observed_floor_ratio": None if self.observed_floor_ratio is None else round(self.observed_floor_ratio, 4),
            "guidance": [g.to_record() for g in self.guidance],
        }


def coverage_guidance(
    calibration: Mapping[str, Any],
    walkable_area_xy: Sequence[tuple[float, float]] = (),
    *,
    observed_pixels: Mapping[str, Iterable[Sequence[float]]] | None = None,
    observed_floor_xy: Iterable[Sequence[float]] | None = None,
    config: CoverageConfig = CoverageConfig(),
) -> CoverageReport:
    """보정 문서(계약 calibration)와 선택 입력으로 커버리지를 계산하고 안내를 만든다.

    observed_pixels: camera_id → 관측 keypoint 픽셀 (u, v) 목록(원본 해상도). 주면 카메라별 영상 커버리지 안내를 낸다.
      목록에 있는 카메라만 본다(빈 목록은 관측 없음).
    observed_floor_xy: 사람이 지나간 World XY 점(삼각측량한 발목·골반 등, 2대 이상에서 본 값). 주면 걷기 경로 안내를 낸다.
    """
    cameras = {cam["camera_id"]: RigCamera.from_calibration(cam) for cam in calibration["cameras"]}
    cells = _walkable_cells(walkable_area_xy, config.cell_m)
    floor_evaluated = cells is not None
    guidance: list[Guidance] = []
    per_camera: dict[str, dict[str, Any]] = {cid: {} for cid in cameras}
    counts: dict[int, int] = {}
    triangulable_ratio = observed_ratio = None
    walkable = 0

    if cells is not None:
        walkable = len(cells)
        visible = {}
        for cid, cam in cameras.items():
            foot_uv = _project(cam, np.column_stack([cells, np.full(len(cells), config.ankle_height_m)]))
            head_uv = _project(cam, np.column_stack([cells, np.full(len(cells), config.head_height_m)]))
            foot, head = _inside_image(cam, foot_uv, config), _inside_image(cam, head_uv, config)
            margin, height = config.image_margin_px, cam.image_size[1]
            with np.errstate(invalid="ignore"):
                # 실제로 어느 경계를 벗어났는지 본다: 머리는 위 경계, 발은 아래 경계(좌우로 나간 칸은 세지 않는다)
                head_above = head_uv[:, 1] < margin
                foot_below = foot_uv[:, 1] > height - margin
            both = foot & head
            visible[cid] = both
            seen_any = foot | head
            n_seen = int(seen_any.sum())
            head_cut = float((foot & head_above).sum() / n_seen) if n_seen else None
            feet_cut = float((head & foot_below).sum() / n_seen) if n_seen else None
            per_camera[cid].update(
                floor_cells_visible=int(both.sum()), head_cut_ratio=head_cut, feet_cut_ratio=feet_cut
            )
            if not both.any():
                guidance.append(
                    Guidance(
                        GuidanceCode.CAMERA_NOT_COVERING_FLOOR,
                        f"카메라 {cid} 에는 걸을 수 있는 바닥 어디에서도 사람이 통째로 보이지 않습니다. 방향을 확인하세요",
                        camera_id=cid,
                    )
                )
                continue
            if head_cut is not None and head_cut >= config.aim_cut_ratio and head_cut >= (feet_cut or 0.0):
                guidance.append(
                    Guidance(
                        GuidanceCode.CAMERA_AIM_TOO_LOW,
                        f"카메라 {cid} 가 너무 아래를 봅니다. 바닥 칸의 {head_cut:.0%} 에서 머리가 화면 위로 잘립니다. "
                        "조금 위로 기울이세요",
                        camera_id=cid,
                        metrics={"head_cut_ratio": head_cut},
                    )
                )
            elif feet_cut is not None and feet_cut >= config.aim_cut_ratio:
                guidance.append(
                    Guidance(
                        GuidanceCode.CAMERA_AIM_TOO_HIGH,
                        f"카메라 {cid} 가 너무 위를 봅니다. 바닥 칸의 {feet_cut:.0%} 에서 발이 화면 아래로 잘립니다. "
                        "조금 아래로 기울이세요",
                        camera_id=cid,
                        metrics={"feet_cut_ratio": feet_cut},
                    )
                )
        n_visible = np.sum([visible[cid] for cid in cameras], axis=0) if cameras else np.zeros(len(cells), dtype=int)
        for k in np.unique(n_visible):
            counts[int(k)] = int((n_visible == k).sum())
        triangulable = n_visible >= config.min_cameras
        triangulable_ratio = float(triangulable.mean()) if walkable else None
        blind = n_visible == 0
        weak = (n_visible > 0) & ~triangulable
        if blind.any():
            guidance.append(
                _cells_guidance(
                    GuidanceCode.FLOOR_BLIND_REGION,
                    "어느 카메라에도 사람이 통째로 보이지 않는 바닥이 {n}칸({area:.1f}m²) 있습니다. 카메라 위치·방향을 조정하세요",
                    cells[blind],
                    config,
                )
            )
        if weak.any():
            guidance.append(
                _cells_guidance(
                    GuidanceCode.FLOOR_WEAK_REGION,
                    f"카메라 {config.min_cameras}대 미만에만 보이는 바닥이 {{n}}칸({{area:.1f}}m²) 있습니다. "
                    "이곳은 위치를 계산할 수 없습니다. 카메라가 겹쳐 보도록 조정하세요",
                    cells[weak],
                    config,
                )
            )
        if observed_floor_xy is not None:
            seen = _observed_cells(cells, observed_floor_xy, config.cell_m, walkable_area_xy)
            observed_ratio = float(seen.mean()) if walkable else None
            need = triangulable & ~seen
            if need.any():
                guidance.append(
                    _cells_guidance(
                        GuidanceCode.WALK_PATH_NEEDED,
                        "아직 사람이 지나가지 않은 바닥이 {n}칸({area:.1f}m²) 있습니다. 표시한 곳을 걸어 주세요",
                        cells[need],
                        config,
                    )
                )

    observed_evaluated = observed_pixels is not None or (observed_floor_xy is not None and floor_evaluated)
    if observed_pixels is not None:
        for cid, pixels in observed_pixels.items():
            if cid not in cameras:
                continue
            uv = np.array([(float(p[0]), float(p[1])) for p in pixels], dtype=float).reshape(-1, 2)
            uv = uv[np.isfinite(uv).all(axis=1)]
            cov = image_coverage(uv, cameras[cid].image_size)
            per_camera[cid].update(image_coverage=cov, observed_points=len(uv))
            if not len(uv):
                guidance.append(
                    Guidance(
                        GuidanceCode.CAMERA_NO_OBSERVATIONS,
                        f"카메라 {cid} 에 사람이 보이지 않습니다. 연결과 방향을 확인하세요",
                        camera_id=cid,
                    )
                )
            elif cov < config.min_image_coverage:
                gx, gy = IMAGE_GRID
                guidance.append(
                    Guidance(
                        GuidanceCode.CAMERA_LOW_IMAGE_COVERAGE,
                        f"카메라 {cid} 화면의 {cov:.0%} 에만 사람이 지나갔습니다({gx}x{gy} 칸 기준). "
                        "화면 가장자리 쪽까지 걸어 주세요",
                        camera_id=cid,
                        metrics={"image_coverage": cov},
                    )
                )

    report_cameras = tuple(CameraCoverage(cid, **per_camera[cid]) for cid in cameras)
    return CoverageReport(
        floor_evaluated=floor_evaluated,
        observed_evaluated=observed_evaluated,
        cell_m=config.cell_m,
        walkable_cells=walkable,
        cells_by_camera_count=counts,
        triangulable_ratio=triangulable_ratio,
        observed_floor_ratio=observed_ratio,
        cameras=report_cameras,
        guidance=tuple(guidance),
    )


# --------------------------------------------------------------------------- 내부


def _walkable_cells(polygon: Sequence[tuple[float, float]], cell_m: float) -> np.ndarray | None:
    """다각형 안 칸 중심 (N, 2). 다각형이 없거나(3점 미만) 칸이 없으면 None."""
    if len(polygon) < 3:
        return None
    poly = np.asarray(polygon, dtype=float)
    if not np.isfinite(poly).all():
        return None
    lo, hi = poly.min(axis=0), poly.max(axis=0)
    xs = np.arange(lo[0] + cell_m / 2, hi[0], cell_m)
    ys = np.arange(lo[1] + cell_m / 2, hi[1], cell_m)
    cells = [(x, y) for x in xs for y in ys if _inside(poly, x, y)]
    return np.array(cells, dtype=float) if cells else None


def _inside(poly: np.ndarray, x: float, y: float) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _project(cam: RigCamera, points: np.ndarray) -> np.ndarray:
    """World 점 (N, 3) -> 왜곡 포함 원본 픽셀 (N, 2). 카메라 뒤이거나 왜곡 식이 접히는 반지름 너머면 NaN."""
    Xc = cam.to_camera(points)
    z = Xc[:, 2]
    front = z > 1e-6
    out = np.full((len(points), 2), np.nan)
    if not front.any():
        return out
    xn = Xc[front, :2] / z[front, None]
    r = np.hypot(xn[:, 0], xn[:, 1])
    branch = distortion.radial_branch(cam.distortion_model, cam.distortion_coeffs)
    radius = np.arctan(r) if cam.distortion_model == distortion.MODEL_FISHEYE else r
    xd = distortion.distort(xn, cam.distortion_model, cam.distortion_coeffs)
    uv = (np.column_stack([xd, np.ones(len(xd))]) @ cam.K.T)[:, :2]
    uv[radius >= branch.r_max] = np.nan
    out[np.flatnonzero(front)] = uv
    return out


def _inside_image(cam: RigCamera, uv: np.ndarray, config: CoverageConfig) -> np.ndarray:
    w, h = cam.image_size
    m = config.image_margin_px
    with np.errstate(invalid="ignore"):
        return (uv[:, 0] >= m) & (uv[:, 0] <= w - m) & (uv[:, 1] >= m) & (uv[:, 1] <= h - m)


def _observed_cells(
    cells: np.ndarray, points: Iterable[Sequence[float]], cell_m: float, polygon: Sequence[tuple[float, float]]
) -> np.ndarray:
    """칸마다 관측 점이 하나 이상 떨어지는지 (N,). 칸은 다각형 경계 상자 기준 격자다(_walkable_cells 와 같다)."""
    lo = np.asarray(polygon, dtype=float).min(axis=0)
    hit = set()
    for p in points:
        x, y = float(p[0]), float(p[1])
        if math.isfinite(x) and math.isfinite(y):
            hit.add((math.floor((x - lo[0]) / cell_m), math.floor((y - lo[1]) / cell_m)))
    keys = np.floor((cells - lo) / cell_m).astype(int)
    return np.array([(int(i), int(j)) in hit for i, j in keys], dtype=bool)


def _cells_guidance(code: GuidanceCode, template: str, cells: np.ndarray, config: CoverageConfig) -> Guidance:
    half = config.cell_m / 2
    n = len(cells)
    bbox = (
        float(cells[:, 0].min() - half),
        float(cells[:, 1].min() - half),
        float(cells[:, 0].max() + half),
        float(cells[:, 1].max() + half),
    )
    return Guidance(
        code,
        template.format(n=n, area=n * config.cell_m**2),
        cells_xy=tuple((float(x), float(y)) for x, y in cells[: config.max_cells_listed]),
        cell_count=n,
        bbox_xy=bbox,
        metrics={"area_m2": n * config.cell_m**2},
    )
