"""번들 조정 결과의 검산과 퇴화 판단 (명세 4.3 3~4단계, 4.4 판정 입력).

- `floor_planes`: 카메라별 바닥 평면과 그 차이(명세 4.4 "4대의 바닥 평면 차이"). 카메라 c 의 평면은 c 가 본 접지점을
  c 와 다른 카메라 한 대씩 짝지어 삼각측량한 점(`geometry.triangulation.triangulate_point`)에 맞춘 z = a·x + b·y + d 다.
  차이는 접지점 XY 전체에서 잰 카메라 쌍별 평면 높이 차이의 최댓값이다. 평면을 맞출 접지점이 부족한 카메라가 있으면
  차이를 비운다(모르면 비움 → 판정 미달).
- `floor_coverage`: 걸을 수 있는 바닥 다각형을 주면 칸(기본 0.5 m) 중 접지점이 있는 칸의 비율. 다각형이 없으면 비운다.
- `camera_height_check`: CCVS 방식 단일 카메라 높이 검산. 사람 몸이 수직이라는 가정만으로, 카메라 한 대의 방향·내부
  파라미터와 두 어깨 중점·두 발목 중점의 시선으로 그 카메라의 바닥 위 높이를 따로 구해 조정 결과 높이와 비교한다.
  어깨–발목 높이 차이와 발목 높이는 조정 결과의 사람별 중앙값을 쓰므로 스케일은 조정 결과를 따른다(스케일 검산이 아니라
  카메라별 방향·높이의 일관성 검산이다). 명세 16장은 CCVS 를 미뤘다(4.4 독립 검증이 대신). criteria(CALIBRATED 판정)
  항목은 아니지만, 차이가 한도를 넘거나 검산할 수 없으면 조정 결과를 불확실(UNCERTAIN)로 표시해 보정 후보로 넣지 않는다
  (`HumanBundleResult.propose_to`). 판정을 바꾸지 않고 후보를 더 엄격하게 거르는 쪽이다.
- `camera_height_scale`: 실측 카메라 높이 축척(명세 4.3 스케일 근거 MEASURED_CAMERA_HEIGHT, -321). 줄자로 잰 바닥 →
  렌즈 중심 높이 / 조정 결과 카메라 높이(World Z, 바닥 Z=0 은 발 접지점)의 카메라별 비 → 중앙값. 2대 미만이거나 비가
  중앙값에서 한도(3%)보다 벗어나는 카메라가 있으면 쓰지 않는다(-277 `world_frame.frame.camera_height_scale` 과 같은 규칙).
- 퇴화: 카메라 수, 접지점 수·수평 분포(한 사람 직선 보행), 카메라별 영상 덮음 비율.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from simai_ai.geometry.rig import RigCamera
from simai_ai.geometry.triangulation import triangulate_point

IMAGE_GRID = (4, 3)
MIN_CELL_OBSERVATIONS = 3


@dataclass(frozen=True)
class FloorPlanes:
    """카메라별 바닥 평면 (a, b, d): z = a·x + b·y + d (접지 높이를 뺀 바닥 높이). 모르면 None."""

    planes: Mapping[str, tuple[float, float, float] | None]
    disagreement_m: float | None
    max_offset_from_floor_m: float | None
    point_counts: Mapping[str, int]


@dataclass(frozen=True)
class CameraHeightCheck:
    camera_id: str
    adjusted_height_m: float
    single_view_height_m: float | None
    sample_count: int

    @property
    def difference_m(self) -> float | None:
        if self.single_view_height_m is None:
            return None
        return abs(self.single_view_height_m - self.adjusted_height_m)

    def to_record(self) -> dict:
        diff = self.difference_m
        return {
            "camera_id": self.camera_id,
            "adjusted_height_m": round(self.adjusted_height_m, 4),
            "single_view_height_m": None if self.single_view_height_m is None else round(self.single_view_height_m, 4),
            "difference_m": None if diff is None else round(diff, 4),
            "sample_count": self.sample_count,
        }


def floor_planes(
    rigs: Mapping[str, RigCamera],
    contacts: Sequence[tuple[Mapping[str, tuple[float, float]], float, tuple[float, float]]],
    min_points: int,
) -> FloorPlanes:
    """contacts: (camera_id → 접지점 원본 픽셀, 접지 높이, 조정 결과 XY) 목록."""
    samples: dict[str, list[tuple[float, float, float]]] = {cid: [] for cid in rigs}
    for pixels, height, _ in contacts:
        for cid in pixels:
            if cid not in rigs:
                continue
            for other in pixels:
                if other == cid or other not in rigs:
                    continue
                X = triangulate_point([rigs[cid], rigs[other]], np.array([pixels[cid], pixels[other]]))
                if X is not None:
                    samples[cid].append((float(X[0]), float(X[1]), float(X[2]) - height))
    planes: dict[str, tuple[float, float, float] | None] = {}
    for cid, pts in samples.items():
        planes[cid] = _fit_plane(np.array(pts)) if len(pts) >= min_points else None
    eval_xy = np.array([xy for _, _, xy in contacts], dtype=float).reshape(-1, 2)
    known = {cid: p for cid, p in planes.items() if p is not None}
    disagreement = offset = None
    if len(known) == len(planes) and len(known) >= 2 and len(eval_xy):
        heights = {cid: eval_xy @ np.array(p[:2]) + p[2] for cid, p in known.items()}
        disagreement = max(
            float(np.max(np.abs(heights[a] - heights[b]))) for a, b in itertools.combinations(heights, 2)
        )
        offset = max(float(np.max(np.abs(h))) for h in heights.values())
    return FloorPlanes(planes, disagreement, offset, {cid: len(p) for cid, p in samples.items()})


def _fit_plane(pts: np.ndarray) -> tuple[float, float, float]:
    """z = a x + b y + d 최소제곱. 잔차가 중앙값 절대 편차의 4배를 넘는 점은 한 번 빼고 다시 맞춘다."""
    A = np.column_stack([pts[:, 0], pts[:, 1], np.ones(len(pts))])
    coef = np.linalg.lstsq(A, pts[:, 2], rcond=None)[0]
    r = pts[:, 2] - A @ coef
    mad = float(np.median(np.abs(r - np.median(r)))) or 1e-9
    keep = np.abs(r) <= 4 * 1.4826 * mad
    if keep.sum() >= 3:
        coef = np.linalg.lstsq(A[keep], pts[keep, 2], rcond=None)[0]
    return float(coef[0]), float(coef[1]), float(coef[2])


def floor_coverage(polygon: Sequence[tuple[float, float]], contact_xy: np.ndarray, cell_m: float) -> float | None:
    """다각형 안 칸 중 접지점이 하나 이상 있는 칸의 비율. 다각형이 없거나 칸이 없으면 None."""
    if len(polygon) < 3:
        return None
    poly = np.asarray(polygon, dtype=float)
    lo, hi = poly.min(axis=0), poly.max(axis=0)
    xs = np.arange(lo[0] + cell_m / 2, hi[0], cell_m)
    ys = np.arange(lo[1] + cell_m / 2, hi[1], cell_m)
    cells = [(x, y) for x in xs for y in ys if _inside(poly, x, y)]
    if not cells:
        return None
    covered = set()
    for x, y in np.asarray(contact_xy, dtype=float).reshape(-1, 2):
        covered.add((math.floor((x - lo[0]) / cell_m), math.floor((y - lo[1]) / cell_m)))
    hit = sum(1 for x, y in cells if (math.floor((x - lo[0]) / cell_m), math.floor((y - lo[1]) / cell_m)) in covered)
    return hit / len(cells)


def _inside(poly: np.ndarray, x: float, y: float) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def contact_minor_spread(contact_xy: np.ndarray) -> float | None:
    """접지점 수평 분포의 짧은 축 표준편차(m). 점이 3개 미만이면 None."""
    xy = np.asarray(contact_xy, dtype=float).reshape(-1, 2)
    if len(xy) < 3:
        return None
    eig = np.linalg.eigvalsh(np.cov(xy.T))
    return float(math.sqrt(max(eig[0], 0.0)))


def image_coverage(uv: np.ndarray, image_size: tuple[int, int]) -> float:
    """영상을 4x3 칸으로 나눠 관측이 MIN_CELL_OBSERVATIONS 개 이상인 칸의 비율."""
    uv = np.asarray(uv, dtype=float).reshape(-1, 2)
    if not len(uv):
        return 0.0
    gx, gy = IMAGE_GRID
    w, h = image_size
    ix = np.clip((uv[:, 0] / w * gx).astype(int), 0, gx - 1)
    iy = np.clip((uv[:, 1] / h * gy).astype(int), 0, gy - 1)
    counts = np.bincount(iy * gx + ix, minlength=gx * gy)
    return float(np.mean(counts >= MIN_CELL_OBSERVATIONS))


def camera_height_check(
    rig: RigCamera,
    pairs: Sequence[tuple[tuple[float, float], tuple[float, float], float, float]],
) -> CameraHeightCheck:
    """pairs: (어깨 중점 픽셀, 발목 중점 픽셀, 어깨–발목 높이 차 H, 발목 중점 높이 z_b) 목록.

    카메라 중심을 (0, 0, h) 로 두고 두 시선이 같은 XY 의 z_b, z_b + H 높이 점을 지난다는 조건에서
    u = h − z_b = H · b·(b − a) / |b − a|² (a, b 는 발목·어깨 시선의 수평 성분 / 수직 성분)를 표본마다 풀고 중앙값을 쓴다.
    """
    estimates = []
    Rt = rig.R.T
    for top_uv, bottom_uv, H, z_b in pairs:
        n = rig.normalize(np.array([top_uv, bottom_uv]))
        if not np.all(np.isfinite(n)):
            continue
        d_top = Rt @ np.array([n[0, 0], n[0, 1], 1.0])
        d_bot = Rt @ np.array([n[1, 0], n[1, 1], 1.0])
        if abs(d_top[2]) < 1e-9 or abs(d_bot[2]) < 1e-9:
            continue
        a = d_bot[:2] / d_bot[2]
        b = d_top[:2] / d_top[2]
        diff = b - a
        denom = float(diff @ diff)
        if denom < 1e-6:
            continue
        estimates.append(H * float(b @ diff) / denom + z_b)
    return CameraHeightCheck(
        camera_id=rig.camera_id,
        adjusted_height_m=float(rig.center[2]),
        single_view_height_m=float(np.median(estimates)) if estimates else None,
        sample_count=len(estimates),
    )


def camera_height_scale(
    adjusted_m: Mapping[str, float],
    measured_m: Mapping[str, float],
    *,
    min_cameras: int,
    max_spread: float,
    floor_known: bool = True,
) -> tuple[float | None, dict]:
    """(실측 / 조정 높이 비의 중앙값 또는 None, 기록). adjusted_m: camera_id → 조정 결과 카메라 높이(World Z).

    쓰지 않는 경우(None, 기록의 rejected 에 이유): 바닥을 접지점으로 정하지 못함(높이의 기준이 없음), 조정 높이가 바닥
    위가 아닌 카메라, 쓸 수 있는 카메라 min_cameras 대 미만, 중앙값에서 max_spread 넘게 벗어난 카메라(카메라별 이유는
    exceeding). 조정 결과에 없는 실측 카메라·실측이 없는 조정 카메라는 쓰지 않고 기록만 한다. 잔차는 (중앙값 × 조정 높이
    − 실측) m 이다. 실측 값이 양의 유한한 수가 아니면 입력 오류다.
    """
    for cid, v in measured_m.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise ValueError(f"카메라 높이 {cid} 는 양의 유한한 수(m)다: {v!r}")
    used = sorted(c for c in measured_m if c in adjusted_m)
    rec: dict = {
        "measured_m": {c: float(measured_m[c]) for c in sorted(measured_m)},
        "adjusted_height_m": {c: round(float(adjusted_m[c]), 4) for c in used},
        "unknown_cameras": sorted(set(measured_m) - set(adjusted_m)),
        "missing_cameras": sorted(set(adjusted_m) - set(measured_m)),
        "min_cameras": min_cameras,
        "max_spread": max_spread,
    }
    if not floor_known:
        rec["rejected"] = "바닥(Z=0)을 발 접지점으로 정하지 못해 카메라 높이의 기준이 없다"
        return None, rec
    below = [c for c in used if not adjusted_m[c] > 0]
    if below:
        rec["rejected"] = f"조정 결과 바닥 위가 아닌 카메라: {below}"
        return None, rec
    rec["cameras"] = len(used)
    if len(used) < min_cameras:
        rec["rejected"] = f"쓸 수 있는 카메라 {len(used)}대 < {min_cameras}대"
        return None, rec
    ratios = {c: float(measured_m[c]) / float(adjusted_m[c]) for c in used}
    med = float(np.median(list(ratios.values())))
    deviation = {c: r / med - 1.0 for c, r in ratios.items()}
    rec |= {
        "ratio": {c: round(r, 5) for c, r in ratios.items()},
        "scale": round(med, 6),
        "deviation": {c: round(d, 5) for c, d in deviation.items()},
        "spread": round(max(abs(d) for d in deviation.values()), 5),
        "residual_m": {c: round(med * float(adjusted_m[c]) - float(measured_m[c]), 4) for c in used},
    }
    exceeding = {
        c: f"배율 {ratios[c]:.4f} 이 중앙값 {med:.4f} 에서 {d:+.1%} 벗어남(한도 {max_spread:.0%})"
        for c, d in deviation.items()
        if abs(d) > max_spread
    }
    if exceeding:
        rec["exceeding"] = exceeding
        rec["rejected"] = f"카메라끼리 어긋남: {sorted(exceeding)}"
        return None, rec
    return med, rec
