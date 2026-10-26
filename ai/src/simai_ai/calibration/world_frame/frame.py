"""MapAnything world 점 → World 좌표계 고정 (명세 4.1, 4.3 2단계, S15P31A101-102).

**규칙(설계 문서 "World 고정 규칙").**
1. 상방 사전값: 카메라들의 영상 위쪽(−y) 방향 평균. 바로 선 설치를 전제한다(`check_cameras_upright` 와 같은 전제).
2. 바닥: 상방 사전값과 30° 안인 평면을 RANSAC 으로 찾고, 법선을 상방 쪽으로 돌린 뒤 (a) 모든 카메라가 0.5 m 넘게 위에
   있고 (b) 평면 아래 0.10 m 넘게 떨어진 점이 3% 이하인 평면 중 가장 낮은 것(카메라 중심 평균에서 잰 높이로 비교). 없으면 바닥을 비운다(FLOOR_NOT_FOUND,
   Z = 0 을 지어내지 않는다). 상방 = 바닥 법선(중력 방향은 그 반대).
3. 발 접지점이 있으면 바닥 확인: 접지점과 바닥의 절대 거리 중앙값 median(|d|) ≤ 0.05 m, 90 백분위 ≤ 0.15 m 가 아니면 실패
   (FLOOR_FOOT_MISMATCH. 명세 4.3 은 발 접지점이 바닥의 근거라 서로 어긋나면 고정하지 않는다).
4. 벽: 바닥 위 0.3 m 보다 높은 점에서 상방과 수직(±10°)인 평면을 찾고, 바닥 점이 거의 모두(밖 5% 이하) 한쪽에 있고 벽 방향
   길이가 1 m 이상인 것만 경계 벽으로 쓴다(방 가운데 수직면·사람은 바닥 점이 양쪽에 있다). 법선은 방 안쪽으로 돌린다.
5. 축: 경계 벽을 방향 묶음(평행 ±10°)으로 묶고, 지지 점이 많은 순서로 서로 수직(90 ± 10°)인 두 묶음을 고른다. 묶음이
   하나뿐이면 6a, 없거나 수직인 쌍이 없으면 축이 모호하다(AXES_AMBIGUOUS, 사유와 함께 실패).
6. 원점: 두 묶음에서 각각 지지 점이 가장 많은 벽과 바닥, 세 평면의 교점(방 모서리 바닥). X·Y 는 두 벽의 방 안쪽 법선이고
   X × Y = +Z(오른손)가 되도록 어느 벽이 X 인지 정한다. 두 벽의 직교 오차는 Y 가 흡수한다. 직교화한 축에서 바닥 점이
   0.10 m 넘게 −X·−Y 쪽인 비율이 5% 를 넘으면(둔각 모서리) 축이 모호하다고 보고 실패한다(방은 +X·+Y 쪽).
   5·6 의 축 출처는 WALLS 다.
6a. 경계 벽 한 방향(축 출처 WALL_AND_FLOOR, S15P31A101-277): 묶음이 하나뿐(ONE_WALL_DIRECTION)이고 바닥 두 점이 없으면,
   지지 점이 가장 많은 경계 벽의 방 안쪽 수평 법선 = X, Y = Z × X, 원점 = 그 벽과 바닥의 교선 위에서 카메라 중심 평균을
   교선에 수직으로 내린 점(원점 출처 CAMERA_LAYOUT_ON_WALL). 사분면 판정은 X 쪽만(바닥 점 5% 넘게 −X 로 0.10 m 넘게 벗어나면
   실패), Y 쪽은 기록만. 경계 벽이 없거나 두 묶음이 수직이 아니면 지금처럼 실패한다.
6b. 바닥 두 점(축 출처 MANUAL_FLOOR_POINTS, 선택 입력, S15P31A101-277): 설치자가 카메라 하나의 그림에서 찍은 두 점(원점,
   +X 방향 점)을 주면 벽 결과와 관계없이 이 경로가 이긴다(사람 지정의 재현성. 벽 축이 있으면 각도 차만 기록). 두 광선을
   바닥 평면과 교차해 원점 = 첫 점, X = 둘째 점 − 첫 점을 바닥에 투영해 정규화, Z = 바닥 법선, Y = Z × X. 광선이 바닥과 만나지 않거나(카메라 뒤),
   바닥과 이루는 각이 10° 미만이거나, 그 픽셀 근처 MapAnything 점이 바닥에서 0.10 m 넘게 떨어졌거나(바닥이 아닌 곳을 찍음),
   두 점이 스케일 반영 뒤 1.0 m 미만이면 실패(FLOOR_POINTS_INVALID, 비움). 사분면 판정은 하지 않고(사람이 정한 기준) 바닥 점의
   사분면별 비율만 기록한다. 우선순위: 바닥 두 점 > 벽 두 방향(WALLS) > 벽 한 방향(WALL_AND_FLOOR) > 실패.
7. 스케일: `choose_scale`(명세 4.3 우선순위). 모르면 MODEL_ESTIMATE(MapAnything 메트릭 = 1).
   카메라 높이 실측(MEASURED_CAMERA_HEIGHT, 선택 입력, -277): 줄자로 잰 바닥 → 렌즈 중심 높이 / 바닥 평면에서 잰 카메라
   중심 높이를 카메라마다 내고 중앙값을 근거 값으로 쓴다. 2대 미만이거나 중앙값에서 3% 넘게 벗어난 카메라가 있으면 쓰지 않는다.

X_world = s · Ra · (X_mapanything − o). 위 길이 한도는 MapAnything 단위(모델 추정 미터) 기준이다(두 점 최소 거리만 World m).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from simai_ai.calibration.criteria import SCALE_PRIORITY, ScaleBasis, ScaleEstimate
from simai_ai.providers.contract import DEFAULT_CALIBRATED_CRITERIA

STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
FLOOR_NOT_FOUND = "FLOOR_NOT_FOUND"
FLOOR_FOOT_MISMATCH = "FLOOR_FOOT_MISMATCH"
AXES_AMBIGUOUS = "AXES_AMBIGUOUS"
FLOOR_POINTS_INVALID = "FLOOR_POINTS_INVALID"
NO_POINTS = "NO_POINTS"

# 축 출처(계약 calibration world_axes_source). CAMERA_LAYOUT 은 예약 값이라 여기서는 내지 않는다
AXES_WALLS = "WALLS"
AXES_WALL_AND_FLOOR = "WALL_AND_FLOOR"
AXES_MANUAL_FLOOR_POINTS = "MANUAL_FLOOR_POINTS"
# 원점 출처(결과 근거 evidence.origin.source)
ORIGIN_WALL_CORNER = "WALL_CORNER"
ORIGIN_CAMERA_LAYOUT_ON_WALL = "CAMERA_LAYOUT_ON_WALL"
ORIGIN_FLOOR_POINT = "FLOOR_POINT"
ONE_WALL_DIRECTION = "ONE_WALL_DIRECTION"

ORIGIN_RULE = "두 경계 벽 방향 묶음에서 각각 지지 점이 가장 많은 벽과 바닥의 교점, X·Y = 방 안쪽 법선(X × Y = +Z)"
MANUAL_ORIGIN_RULE = ("설치자가 카메라 그림에서 찍은 바닥 두 점: 원점 = 첫 점 광선과 바닥의 교점, X = 둘째 점 − 첫 점을 바닥에 "
                      "투영해 정규화, Z = 바닥 법선, Y = Z × X")
WALL_AND_FLOOR_RULE = ("경계 벽 한 방향: 지지 점이 가장 많은 경계 벽의 방 안쪽 수평 법선 = X, Y = Z × X, 원점 = 그 벽과 바닥의 "
                       "교선 위에서 카메라 중심 평균을 교선에 수직으로 내린 점")
AMBIGUOUS_HINT = "바닥 두 점(--floor-points)을 주면 축을 정할 수 있다"


@dataclass(frozen=True)
class WorldFrameConfig:
    max_points: int = 200_000
    conf_percentile: float = 10.0  # 카메라마다 신뢰도 하위 이 백분위를 버린다(run_mapanything 점구름과 같은 값)
    ransac_iterations: int = 600
    ransac_eval_points: int = 40_000
    seed: int = 0
    plane_tol_m: float = 0.03
    max_planes: int = 8
    min_plane_fraction: float = 0.02
    floor_max_tilt_deg: float = 30.0
    floor_below_margin_m: float = 0.10
    floor_max_below_fraction: float = 0.03
    min_camera_height_m: float = 0.5
    wall_max_tilt_deg: float = 10.0
    wall_min_height_m: float = 0.3
    wall_min_length_m: float = 1.0
    wall_outside_margin_m: float = 0.10
    wall_max_outside_fraction: float = 0.05
    family_tol_deg: float = 10.0
    orthogonal_tol_deg: float = 10.0
    foot_median_tol_m: float = 0.05
    foot_p90_tol_m: float = 0.15
    min_foot_points: int = 10
    # 바닥 두 점(-277). 근거는 설계 문서 "바닥 두 점·카메라 높이 축척" 표
    floor_point_min_incidence_deg: float = 10.0  # 광선과 바닥이 이루는 각 하한
    floor_point_max_height_m: float = 0.10  # 찍은 픽셀 근처 MapAnything 점의 바닥 위 높이 한도(바닥 아래 점 기준과 같음)
    floor_point_min_separation_m: float = 1.0  # 두 점 사이 바닥 위 거리 하한(스케일 반영 뒤 World m)
    # 카메라 높이 축척(-277)
    camera_height_min_cameras: int = 2
    camera_height_max_spread: float = 0.03  # 카메라별 배율이 중앙값에서 벗어나는 최대 비율


@dataclass(frozen=True)
class FloorPointRays:
    """바닥 두 점(원점, +X 방향 점)의 MapAnything world 광선(`inputs.floor_point_rays` 가 픽셀에서 만든다)."""

    camera_id: str
    center: np.ndarray  # 카메라 중심(MapAnything world)
    directions: tuple[np.ndarray, np.ndarray]  # 광선 방향(원점, +X 방향 점)
    observed: tuple[np.ndarray | None, np.ndarray | None]  # 그 픽셀 근처 MapAnything 점(없으면 None = 확인 못 함)
    record: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Plane:
    """n · x = d (n 단위 벡터)."""

    normal: np.ndarray
    offset: float
    inliers: int
    rms: float

    def distance(self, pts: np.ndarray) -> np.ndarray:
        return pts @ self.normal - self.offset


@dataclass(frozen=True)
class WorldFrameResult:
    """status OK 면 rotation·origin·scale 이 있다. FAILED 면 비어 있고 reason 이 있다."""

    status: str
    reason: str | None
    detail: str | None
    evidence: dict[str, Any]
    rotation: np.ndarray | None = None  # MapAnything world → World 회전(행 = X, Y, Z 축)
    origin: np.ndarray | None = None  # MapAnything world 의 World 원점
    scale: float | None = None
    scale_basis: str | None = None
    scale_cross_checks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    axes_source: str | None = None  # WALLS·WALL_AND_FLOOR·MANUAL_FLOOR_POINTS (OK 일 때만)

    def matrix(self) -> np.ndarray:
        """MapAnything world → World 4×4 [sR | T] (-214 `load_world_alignment` 형식)."""
        if self.status != STATUS_OK:
            raise ValueError(f"World 고정 실패({self.reason})라 변환이 없다")
        M = np.eye(4)
        M[:3, :3] = self.scale * self.rotation
        M[:3, 3] = -self.scale * self.rotation @ self.origin
        return M


# --------------------------------------------------------------------------- 스케일


@dataclass(frozen=True)
class ScaleChoice:
    basis: str
    scale: float
    cross_checks: tuple[str, ...]
    record: dict[str, Any]


def choose_scale(estimates: Iterable[ScaleEstimate], tolerance: float | None = None) -> ScaleChoice:
    """명세 4.3 우선순위로 스케일 하나를 고르고 다른 근거로 교차 확인한다. scale = 실제 길이 / MapAnything 길이.

    근거별 값: 실측 정물은 정물(label)마다 평균한 뒤 정물끼리 평균하고, 정물끼리 허용치(정물 일치 기준)보다 어긋나면 그 근거를
    쓰지 않는다(지어내지 않고 다음 근거로). 사람 키 통계는 표본이 가장 많은 것, 나머지는 표본 수 가중 평균이다(카메라 높이
    실측은 `camera_height_scale` 이 카메라끼리 일치를 본 뒤 하나만 넣는다).
    MODEL_ESTIMATE(MapAnything 메트릭, 1.0)는 받지 않았어도 마지막 근거로 둔다. 교차 확인은 고른 값과 허용치
    (CalibratedCriteria.cross_check_tolerance, 5%) 안인 다른 근거다.
    """
    tol = DEFAULT_CALIBRATED_CRITERIA.cross_check_tolerance if tolerance is None else tolerance
    estimates = tuple(estimates)
    values: dict[ScaleBasis, float] = {}
    notes: dict[str, Any] = {}
    for basis in SCALE_PRIORITY:
        es = [e for e in estimates if e.basis is basis]
        if not es:
            continue
        if basis is ScaleBasis.MEASURED_OBJECT:
            by_label: dict[str, list[float]] = {}
            for e in es:
                by_label.setdefault(str(e.label), []).append(e.scale)
            objs = {k: sum(v) / len(v) for k, v in by_label.items()}
            mean = sum(objs.values()) / len(objs)
            spread = max(abs(v / mean - 1) for v in objs.values())
            notes[str(basis)] = {"objects": len(objs), "spread": round(spread, 4)}
            if spread > DEFAULT_CALIBRATED_CRITERIA.measured_object_agreement:
                notes[str(basis)]["rejected"] = "정물끼리 어긋남"
                continue
            values[basis] = mean
        elif basis is ScaleBasis.HUMAN_HEIGHT_STATS:
            best = max(es, key=lambda e: e.sample_count)
            values[basis] = best.scale
            notes[str(basis)] = {"people": best.sample_count}
        else:
            n = sum(e.sample_count for e in es)
            values[basis] = sum(e.scale * e.sample_count for e in es) / n
            notes[str(basis)] = {"samples": n}
    values.setdefault(ScaleBasis.MODEL_ESTIMATE, 1.0)
    chosen = next(b for b in SCALE_PRIORITY if b in values)
    s = values[chosen]
    agree = tuple(str(b) for b in SCALE_PRIORITY if b is not chosen and b in values and abs(values[b] / s - 1) <= tol)
    disagree = {str(b): round(values[b], 5) for b in SCALE_PRIORITY if b is not chosen and b in values and str(b) not in agree}
    record = {
        "basis": str(chosen),
        "scale": round(s, 6),
        "candidates": {str(b): round(v, 6) for b, v in values.items()},
        "cross_checks": list(agree),
        "disagreeing": disagree,
        "tolerance": tol,
        "notes": notes,
        "priority": [str(b) for b in SCALE_PRIORITY],
    }
    if chosen is ScaleBasis.MODEL_ESTIMATE:
        record["accuracy_note"] = "실측 정물이 없어 스케일 정확도는 모델 추정 수준이다(명세 4.3: 정물 없으면 2~5% 이상)"
    return ScaleChoice(str(chosen), s, agree, record)


def camera_height_scale(
    floor: Plane, centers: np.ndarray, ids: Sequence[str], heights: Mapping[str, float], cfg: WorldFrameConfig
) -> tuple[ScaleEstimate | None, dict[str, Any]]:
    """실측 카메라 높이(바닥 → 렌즈 중심, m) / 바닥 평면에서 잰 카메라 중심 높이(MapAnything 단위)의 카메라별 비 → 중앙값.

    결과에 없는 카메라는 쓰지 않고 기록한다. 쓸 수 있는 카메라가 cfg.camera_height_min_cameras 미만이거나, 카메라별 비가
    중앙값에서 cfg.camera_height_max_spread 넘게 벗어나면 근거를 쓰지 않는다(None, 이유 기록. 다음 근거로).
    """
    for cid, v in heights.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise ValueError(f"카메라 높이 {cid} 는 양의 유한한 수(m)다: {v!r}")
    model_h = {c: float(floor.distance(centers[i:i + 1])[0]) for i, c in enumerate(ids)}
    used = sorted(c for c in heights if c in model_h)
    rec: dict[str, Any] = {
        "measured_m": {c: float(heights[c]) for c in sorted(heights)},
        "model_height_ma": {c: round(model_h[c], 4) for c in used},
        "ratio": {c: round(heights[c] / model_h[c], 5) for c in used},
        "cameras": len(used),
        "unknown_cameras": sorted(set(heights) - set(model_h)),
        "missing_cameras": sorted(set(model_h) - set(heights)),
        "max_spread": cfg.camera_height_max_spread,
        "floor_rms_ma": round(floor.rms, 4),
    }
    if len(used) < cfg.camera_height_min_cameras:
        rec["rejected"] = f"쓸 수 있는 카메라 {len(used)}대 < {cfg.camera_height_min_cameras}대"
        return None, rec
    ratios = np.array([heights[c] / model_h[c] for c in used])
    med = float(np.median(ratios))
    spread = float(np.max(np.abs(ratios / med - 1)))
    rec |= {"scale": round(med, 6), "spread": round(spread, 5)}
    if spread > cfg.camera_height_max_spread:
        rec["rejected"] = f"카메라끼리 어긋남(중앙값에서 {spread:.1%} > {cfg.camera_height_max_spread:.0%})"
        return None, rec
    return ScaleEstimate(ScaleBasis.MEASURED_CAMERA_HEIGHT, med, len(used), "camera_heights"), rec


# --------------------------------------------------------------------------- 평면


def _ransac_plane(pts: np.ndarray, rng: np.random.Generator, cfg: WorldFrameConfig, accept) -> tuple[Plane, np.ndarray] | None:
    n_pts = len(pts)
    if n_pts < 3:
        return None
    ev = pts if n_pts <= cfg.ransac_eval_points else pts[rng.choice(n_pts, cfg.ransac_eval_points, replace=False)]
    best_count, best = 0, None
    for _ in range(cfg.ransac_iterations):
        a, b, c = pts[rng.choice(n_pts, 3, replace=False)]
        n = np.cross(b - a, c - a)
        norm = np.linalg.norm(n)
        if norm < 1e-9:
            continue
        n = n / norm
        if not accept(n):
            continue
        count = int(np.count_nonzero(np.abs(ev @ n - n @ a) < cfg.plane_tol_m))
        if count > best_count:
            best_count, best = count, (n, float(n @ a))
    if best is None:
        return None
    n, d = best
    for _ in range(2):  # 전체 점으로 최소제곱 다듬기(받아들일 수 없는 방향이거나 인라이어가 줄면 이전 평면 유지)
        inl = np.abs(pts @ n - d) < cfg.plane_tol_m
        if inl.sum() < 3:
            return None
        q = pts[inl]
        centroid = q.mean(axis=0)
        n2 = np.linalg.svd(q - centroid, full_matrices=False)[2][2]
        n2 = n2 if n2 @ n >= 0 else -n2
        d2 = float(n2 @ centroid)
        if not accept(n2) or np.count_nonzero(np.abs(pts @ n2 - d2) < cfg.plane_tol_m) < inl.sum():
            break
        n, d = n2, d2
    inl = np.abs(pts @ n - d) < cfg.plane_tol_m
    rms = float(np.sqrt(np.mean((pts[inl] @ n - d) ** 2))) if inl.any() else float("nan")
    return Plane(n, d, int(inl.sum()), rms), inl


def _up_prior(cam2world: Sequence[np.ndarray]) -> np.ndarray:
    ups = np.array([-np.asarray(T)[:3, 1] for T in cam2world])  # 영상 위쪽(−y)의 world 방향
    up = ups.mean(axis=0)
    return up / np.linalg.norm(up)


def find_floor(pts: np.ndarray, centers: np.ndarray, up0: np.ndarray, cfg: WorldFrameConfig, rng) -> tuple[Plane | None, list]:
    cos_tilt = math.cos(math.radians(cfg.floor_max_tilt_deg))
    min_pts = max(50, int(cfg.min_plane_fraction * len(pts)))
    remaining = np.arange(len(pts))
    tried, floors = [], []
    for _ in range(cfg.max_planes):
        res = _ransac_plane(pts[remaining], rng, cfg, lambda n: abs(n @ up0) >= cos_tilt)
        if res is None:
            break
        plane, inl = res
        if plane.inliers < min_pts:
            break
        if plane.normal @ up0 < 0:
            plane = Plane(-plane.normal, -plane.offset, plane.inliers, plane.rms)
        cam_h = plane.distance(centers)
        below = float(np.mean(plane.distance(pts) < -cfg.floor_below_margin_m))
        ok = bool(np.all(cam_h > cfg.min_camera_height_m)) and below <= cfg.floor_max_below_fraction
        tried.append({
            "normal_ma": _r(plane.normal), "offset_ma": round(plane.offset, 4), "inliers": plane.inliers,
            "below_fraction": round(below, 4), "min_camera_height": round(float(cam_h.min()), 3),
            "max_camera_height": round(float(cam_h.max()), 3), "mean_camera_height": float(cam_h.mean()),
            "floor_candidate": ok,
        })
        if ok:
            floors.append(plane)
        remaining = remaining[~inl]
    if not floors:
        return None, tried
    # 가장 낮은 = 카메라 중심 평균에서 잰 카메라 높이가 가장 큰 평면. offset(MapAnything 원점 기준)으로 비교하면 법선이
    # 조금 다른 평면끼리 원점까지의 수평 팔 길이만큼 높낮이가 뒤바뀐다(원점은 첫 카메라라 방 가장자리다)
    return max(floors, key=lambda p: float(np.mean(p.distance(centers)))), tried


@dataclass(frozen=True)
class Wall:
    normal: np.ndarray  # 수평, 방 안쪽
    offset: float
    inliers: int
    length: float
    height: float
    tilt_deg: float
    outside_fraction: float
    boundary: bool

    def record(self) -> dict[str, Any]:
        return {
            "normal_ma": _r(self.normal), "offset_ma": round(self.offset, 4), "inliers": self.inliers,
            "length": round(self.length, 3), "height": round(self.height, 3), "tilt_deg": round(self.tilt_deg, 2),
            "outside_fraction": round(self.outside_fraction, 4), "boundary": self.boundary,
        }


def find_walls(pts: np.ndarray, floor: Plane, cfg: WorldFrameConfig, rng) -> list[Wall]:
    up = floor.normal
    h = floor.distance(pts)
    floor_pts = pts[np.abs(h) < cfg.plane_tol_m]
    cand = pts[h > cfg.wall_min_height_m]
    sin_tilt = math.sin(math.radians(cfg.wall_max_tilt_deg))
    min_pts = max(50, int(cfg.min_plane_fraction * len(cand)))
    remaining = np.arange(len(cand))
    walls: list[Wall] = []
    for _ in range(cfg.max_planes):
        res = _ransac_plane(cand[remaining], rng, cfg, lambda n: abs(n @ up) <= sin_tilt)
        if res is None:
            break
        plane, inl = res
        if plane.inliers < min_pts:
            break
        q = cand[remaining][inl]
        tilt = math.degrees(math.asin(min(1.0, abs(plane.normal @ up))))
        n = plane.normal - (plane.normal @ up) * up
        n /= np.linalg.norm(n)
        d = float(np.median(q @ n))
        side = floor_pts @ n - d
        if len(side) and np.median(side) < 0:
            n, d, side = -n, -d, -side
        outside = float(np.mean(side < -cfg.wall_outside_margin_m)) if len(side) else 1.0
        along = q @ np.cross(up, n)
        length = float(np.percentile(along, 98) - np.percentile(along, 2))
        height = float(np.percentile(q @ up, 98) - np.percentile(q @ up, 2))
        boundary = outside <= cfg.wall_max_outside_fraction and length >= cfg.wall_min_length_m
        walls.append(Wall(n, d, plane.inliers, length, height, tilt, outside, boundary))
        remaining = remaining[~inl]
    return walls


def _line_angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    """두 벽 법선이 이루는 선 각도(0 = 평행, 90 = 수직)."""
    return math.degrees(math.acos(min(1.0, abs(float(a @ b)))))


def choose_axes(walls: Sequence[Wall], up: np.ndarray, cfg: WorldFrameConfig) -> tuple[tuple[Wall, Wall] | None, str, dict]:
    boundary = sorted((w for w in walls if w.boundary), key=lambda w: -w.inliers)
    families: list[list[Wall]] = []
    for w in boundary:
        for fam in families:
            if _line_angle_deg(fam[0].normal, w.normal) <= cfg.family_tol_deg:
                fam.append(w)
                break
        else:
            families.append([w])
    families.sort(key=lambda f: -sum(w.inliers for w in f))
    info = {"boundary_walls": len(boundary), "families": [
        {"walls": len(f), "inliers": sum(w.inliers for w in f), "normal_ma": _r(f[0].normal)} for f in families
    ]}
    if not families:
        return None, "경계 벽을 찾지 못함(NO_WALL)", info
    if len(families) == 1:
        return None, "경계 벽 방향이 하나뿐이다(ONE_WALL_DIRECTION)", info
    for i in range(len(families)):
        for j in range(i + 1, len(families)):
            ang = _line_angle_deg(families[i][0].normal, families[j][0].normal)
            if abs(90.0 - ang) <= cfg.orthogonal_tol_deg:
                info["orthogonality_error_deg"] = round(abs(90.0 - ang), 3)
                info["chosen_families"] = [i, j]
                if len(families) > 2:
                    info["other_families"] = len(families) - 2
                return (families[i][0], families[j][0]), "", info
    return None, f"서로 수직(90±{cfg.orthogonal_tol_deg}°)인 경계 벽 방향 쌍이 없다(WALLS_NOT_ORTHOGONAL)", info


def _wall_axes(pair: tuple[Wall, Wall], up: np.ndarray) -> tuple[Wall, Wall]:
    """(X 벽, Y 벽): 다른 벽의 안쪽 법선이 up × X 쪽(+Y)에 오게 하는 벽이 X 벽이다(오른손)."""
    wa, wb = pair
    return (wa, wb) if float(np.cross(up, wa.normal) @ wb.normal) > 0 else (wb, wa)


def _single_wall(walls: Sequence[Wall]) -> Wall | None:
    """경계 벽 묶음이 한 방향뿐일 때 쓸 벽: 지지 점이 가장 많은 경계 벽."""
    boundary = [w for w in walls if w.boundary]
    return max(boundary, key=lambda w: w.inliers) if boundary else None


def _quadrants(fl_ma: np.ndarray) -> dict[str, float]:
    """바닥 점의 사분면별 비율(기록용)."""
    if not len(fl_ma):
        return {}
    px, py = fl_ma[:, 0] >= 0, fl_ma[:, 1] >= 0
    return {"+X+Y": round(float(np.mean(px & py)), 4), "-X+Y": round(float(np.mean(~px & py)), 4),
            "-X-Y": round(float(np.mean(~px & ~py)), 4), "+X-Y": round(float(np.mean(px & ~py)), 4)}


def manual_floor_axes(
    floor: Plane, rays: FloorPointRays, scale: float, cfg: WorldFrameConfig
) -> tuple[np.ndarray | None, np.ndarray | None, str, dict[str, Any]]:
    """바닥 두 점 → (Ra, 원점, 실패 사유, 기록). 실패하면 Ra·원점이 None 이고 사유가 있다(지어내지 않는다).

    광선 d 와 카메라 중심 c 에서 바닥 교점 = c + t·d, t = h / (−n·d)(h = 바닥 위 카메라 높이). n·d ≥ 0 이면 광선이 바닥과
    만나지 않는다(BEHIND_CAMERA: 수평 위를 보거나 교점이 카메라 뒤). 바닥과 이루는 각 asin(−n·d) 이 하한보다 작으면 깊이 방향
    오차가 1/tan 으로 커진다(RAY_GRAZING). 그 픽셀 근처 MapAnything 점이 바닥에서 한도보다 떨어졌으면 바닥이 아닌 곳(책상·
    사람)을 찍은 것이다(POINT_NOT_ON_FLOOR, 근처 점이 없으면 확인 못 함으로 기록). 두 점의 바닥 위 거리(스케일 반영)가
    하한보다 작으면 X 방향이 픽셀 오차에 크게 흔들린다(POINTS_TOO_CLOSE).
    """
    up = floor.normal
    center = np.asarray(rays.center, dtype=float)
    h_cam = float(floor.distance(center[None])[0])
    info: dict[str, Any] = {"camera_id": rays.camera_id, "camera_height_ma": round(h_cam, 4), "points": {}}
    pts = []
    for name, d, obs in zip(("origin", "x_axis"), rays.directions, rays.observed, strict=True):
        d = np.asarray(d, dtype=float)
        d = d / np.linalg.norm(d)
        down = -float(d @ up)
        rec: dict[str, Any] = {"incidence_deg": round(math.degrees(math.asin(max(-1.0, min(1.0, down)))), 3)}
        info["points"][name] = rec
        if h_cam <= 0 or down <= 1e-9:
            return None, None, f"{name} 광선이 바닥과 만나지 않는다(BEHIND_CAMERA: 수평 위 또는 카메라 뒤)", info
        if rec["incidence_deg"] < cfg.floor_point_min_incidence_deg:
            return None, None, (f"{name} 광선이 바닥과 {rec['incidence_deg']}° 로 거의 평행하다(RAY_GRAZING: 하한 "
                                f"{cfg.floor_point_min_incidence_deg}°, 카메라에서 더 가까운 바닥 점을 고른다)"), info
        t = h_cam / down
        p = center + t * d
        rec |= {"range_ma": round(t, 4), "point_ma": _r(p)}
        if obs is None:
            rec["floor_check"] = "NOT_CHECKED"  # 그 픽셀 근처에 MapAnything 점이 없다(사람 마스크·저신뢰)
        else:
            obs = np.asarray(obs, dtype=float)
            hob = float(floor.distance(obs[None])[0])
            rec |= {"observed_height_ma": round(hob, 4), "observed_offset_ma": round(float(np.linalg.norm(obs - p)), 4)}
            if abs(hob) > cfg.floor_point_max_height_m:
                rec["floor_check"] = "OFF_FLOOR"
                return None, None, (f"{name} 픽셀의 MapAnything 점이 바닥에서 {hob:+.3f} 떨어졌다(POINT_NOT_ON_FLOOR: 한도 "
                                    f"{cfg.floor_point_max_height_m}, 책상·사람이 아닌 바닥 점을 고른다)"), info
            rec["floor_check"] = "ON_FLOOR"
        pts.append(p)
    v = pts[1] - pts[0]
    v = v - (v @ up) * up
    sep = float(np.linalg.norm(v)) * scale
    info["separation_m"] = round(sep, 4)
    if sep < cfg.floor_point_min_separation_m:
        return None, None, (f"두 점 사이 바닥 거리 {sep:.3f} m < {cfg.floor_point_min_separation_m} m "
                            "(POINTS_TOO_CLOSE: 멀리 떨어진 두 점을 고른다)"), info
    X = v / np.linalg.norm(v)
    Y = np.cross(up, X)
    return np.vstack([X, Y, up]), pts[0], "", info


# --------------------------------------------------------------------------- 고정


def fix_world_frame(
    points: np.ndarray,
    cam2world: Mapping[str, np.ndarray],
    *,
    scale_estimates: Sequence[ScaleEstimate] = (),
    foot_points: np.ndarray | None = None,
    floor_points: FloorPointRays | None = None,
    camera_heights: Mapping[str, float] | None = None,
    config: WorldFrameConfig = WorldFrameConfig(),
) -> WorldFrameResult:
    """points: MapAnything world 점(사람·저신뢰 점을 뺀 것), cam2world: camera_id → 4×4 (OpenCV).

    floor_points: 바닥 두 점 광선(있으면 축 출처 MANUAL_FLOOR_POINTS, 벽보다 우선). camera_heights: camera_id → 실측 높이(m,
    바닥 → 렌즈 중심, 스케일 근거 MEASURED_CAMERA_HEIGHT). 카메라 높이 근거는 바닥에서 재므로 scale_estimates 로는 받지 않는다.
    """
    cfg = config
    if any(ScaleBasis(e.basis) is ScaleBasis.MEASURED_CAMERA_HEIGHT for e in scale_estimates):
        raise ValueError("카메라 높이 실측은 camera_heights 로 준다(찾은 바닥에서 잰 높이와 비교해야 한다)")
    rng = np.random.default_rng(cfg.seed)
    pts = np.asarray(points, dtype=float)
    pts = pts[np.isfinite(pts).all(axis=1)]
    ev: dict[str, Any] = {"points_used": int(min(len(pts), cfg.max_points)), "config": cfg.__dict__.copy()}
    if len(pts) < 100:
        return WorldFrameResult(STATUS_FAILED, NO_POINTS, f"쓸 수 있는 점이 {len(pts)} 개다", ev)
    if len(pts) > cfg.max_points:
        pts = pts[rng.choice(len(pts), cfg.max_points, replace=False)]
    ids = sorted(cam2world)
    centers = np.array([np.asarray(cam2world[c])[:3, 3] for c in ids])
    up0 = _up_prior([cam2world[c] for c in ids])
    ev["up_prior_ma"] = _r(up0)

    floor, tried = find_floor(pts, centers, up0, cfg, rng)
    ev["floor_search"] = tried
    if floor is None:
        return WorldFrameResult(
            STATUS_FAILED, FLOOR_NOT_FOUND,
            "카메라 아래에 있고 그 아래 점이 거의 없는 수평면을 찾지 못했다(바닥을 지어내지 않는다)", ev,
        )
    up = floor.normal
    ev["floor"] = {
        "normal_ma": _r(up), "offset_ma": round(floor.offset, 4), "inliers": floor.inliers, "rms_ma": round(floor.rms, 4),
        "tilt_from_prior_deg": round(math.degrees(math.acos(min(1.0, float(up @ up0)))), 3),
        "gravity_ma": _r(-up),
    }
    if foot_points is not None:
        check = check_floor_with_feet(floor, np.asarray(foot_points, dtype=float), cfg)
        ev["floor"]["foot_check"] = check
        if check["status"] == "INCONSISTENT":
            return WorldFrameResult(STATUS_FAILED, FLOOR_FOOT_MISMATCH, check["detail"], ev)
    else:
        ev["floor"]["foot_check"] = {"status": "NOT_CHECKED", "detail": "발 접지점 입력 없음"}

    # 스케일은 바닥만 있으면 정해진다(카메라 높이 실측은 바닥에서 잰다). 축을 못 정해도 근거는 남긴다
    estimates = list(scale_estimates)
    height_rec = None
    if camera_heights is not None:
        height_est, height_rec = camera_height_scale(floor, centers, ids, camera_heights, cfg)
        if height_est is not None:
            estimates.append(height_est)
    scale = choose_scale(estimates)
    if height_rec is not None:
        scale.record["camera_heights"] = height_rec
    ev["scale"] = scale.record
    s = scale.scale

    walls = find_walls(pts, floor, cfg, rng)
    ev["walls"] = [w.record() for w in walls]
    pair, why, info = choose_axes(walls, up, cfg)
    ev["axes"] = info
    on_floor = np.abs(floor.distance(pts)) < cfg.plane_tol_m
    warnings = []
    # 축 우선순위: 사람이 준 바닥 두 점 > 경계 벽 두 방향(WALLS) > 경계 벽 한 방향(WALL_AND_FLOOR) > 실패(비움)
    one_wall = _single_wall(walls) if pair is None and ONE_WALL_DIRECTION in why else None
    ref_wall = _wall_axes(pair, up)[0] if pair is not None else one_wall  # 두 점과 비교할 벽 축(있으면)
    if floor_points is not None:
        # 사람 지정이 이긴다(재현성). 벽 축이 있으면 그 축과의 각도 차만 기록한다
        Ra, origin, fail, manual = manual_floor_axes(floor, floor_points, s, cfg)
        info.update({"source": AXES_MANUAL_FLOOR_POINTS, "floor_points": manual | {"input": floor_points.record}})
        if Ra is None:
            return WorldFrameResult(STATUS_FAILED, FLOOR_POINTS_INVALID, fail, ev)
        X, Y = Ra[0], Ra[1]
        info.update({"rule": MANUAL_ORIGIN_RULE, "x_axis_ma": _r(X), "y_axis_ma": _r(Y), "z_axis_ma": _r(up)})
        ev["origin"] = {"point_ma": _r(origin), "rule": MANUAL_ORIGIN_RULE, "source": ORIGIN_FLOOR_POINT}
        local_ma = (pts - origin) @ Ra.T
        info["floor_quadrant_fractions"] = _quadrants(local_ma[on_floor])  # 판정하지 않는다(사람이 정한 기준)
        if ref_wall is not None:
            Yw = np.cross(up, ref_wall.normal)
            ang = math.degrees(math.atan2(float(X @ Yw), float(X @ ref_wall.normal)))
            ang = (ang + 45.0) % 90.0 - 45.0  # 벽 축 X·Y 중 가까운 것과의 각도
            info["walls_comparison"] = {"x_axis_ma": _r(ref_wall.normal), "angle_to_wall_axes_deg": round(ang, 3),
                                        "walls": "WALLS" if pair is not None else "ONE_WALL",
                                        "note": "비교용 기록(축은 사람 지정)"}
            if abs(ang) > cfg.orthogonal_tol_deg:
                warnings.append(f"바닥 두 점 축이 경계 벽 축과 {ang:+.1f}° 다르다(찍은 점을 확인)")
        if pair is None:
            info["walls_status"] = why
        axes_source = AXES_MANUAL_FLOOR_POINTS
    elif pair is None and one_wall is not None:
        # 경계 벽 한 방향(열린 공간): 벽 법선이 X, 원점은 벽·바닥 교선 위 카메라 중심 평균의 수선 발
        X = one_wall.normal
        Y = np.cross(up, X)
        Ra = np.vstack([X, Y, up])
        cbar = centers.mean(axis=0)
        origin = np.linalg.solve(np.vstack([X, up, Y]), [one_wall.offset, floor.offset, float(Y @ cbar)])
        ev["axes"].update({
            "source": AXES_WALL_AND_FLOOR, "rule": WALL_AND_FLOOR_RULE, "x_wall": _index(walls, one_wall),
            "x_wall_inliers": one_wall.inliers, "walls_status": why,
            "x_axis_ma": _r(X), "y_axis_ma": _r(Y), "z_axis_ma": _r(up),
        })
        ev["origin"] = {"point_ma": _r(origin), "rule": WALL_AND_FLOOR_RULE, "source": ORIGIN_CAMERA_LAYOUT_ON_WALL,
                        "camera_center_mean_ma": _r(cbar)}
        local_ma = (pts - origin) @ Ra.T
        fl_ma = local_ma[on_floor]
        # 사분면 판정은 X 쪽(벽 안쪽)만: 경계 벽과 같은 기준. Y 쪽은 원점이 카메라 배치로 정해져 양쪽에 방이 있다(기록만)
        outside = float(np.mean(fl_ma[:, 0] < -cfg.wall_outside_margin_m))
        ev["axes"]["floor_outside_x_fraction"] = round(outside, 4)
        ev["axes"]["floor_quadrant_fractions"] = _quadrants(fl_ma)
        if outside > cfg.wall_max_outside_fraction:
            return WorldFrameResult(
                STATUS_FAILED, AXES_AMBIGUOUS,
                f"벽 한 방향 축에서 방이 +X(벽 안쪽)에 놓이지 않는다(QUADRANT_X: 바닥 점 {outside:.1%} 가 "
                f"{cfg.wall_outside_margin_m} 넘게 밖, 한도 {cfg.wall_max_outside_fraction:.0%})", ev,
            )
        warnings.append("축은 경계 벽 하나와 바닥(WALL_AND_FLOOR), 원점은 카메라 배치로 정했다"
                        "(카메라를 벽을 따라 옮기면 원점이 Y 로 움직인다)")
        axes_source = AXES_WALL_AND_FLOOR
    else:
        if pair is None:
            return WorldFrameResult(STATUS_FAILED, AXES_AMBIGUOUS, f"{why}. {AMBIGUOUS_HINT}", ev)
        wx, wy = _wall_axes(pair, up)
        X = wx.normal
        Y = np.cross(up, X)
        Ra = np.vstack([X, Y, up])
        origin = np.linalg.solve(np.vstack([wx.normal, wy.normal, up]), [wx.offset, wy.offset, floor.offset])
        ev["axes"].update({
            "source": AXES_WALLS, "rule": ORIGIN_RULE, "x_wall": _index(walls, wx), "y_wall": _index(walls, wy),
            "x_axis_ma": _r(X), "y_axis_ma": _r(Y), "z_axis_ma": _r(up),
            "y_vs_wall_deg": round(_line_angle_deg(Y, wy.normal), 3),
        })
        ev["origin"] = {"point_ma": _r(origin), "rule": ORIGIN_RULE, "source": ORIGIN_WALL_CORNER}

        # 직교화(Y = up × X) 뒤에도 방이 +X·+Y 쪽인지 본다. 원점 모서리가 90° 보다 넓게 열리면(둔각, 안쪽 법선 사이 < 90°)
        # 직교 축으로는 방이 음의 쪽으로 넘어간다. 경계 벽과 같은 기준(0.10 m 넘게 밖인 바닥 점 5% 이하)으로 막는다
        local_ma = (pts - origin) @ Ra.T
        fl_ma = local_ma[on_floor]
        outside = float(np.mean((fl_ma[:, 0] < -cfg.wall_outside_margin_m) | (fl_ma[:, 1] < -cfg.wall_outside_margin_m)))
        ev["axes"]["floor_outside_quadrant_fraction"] = round(outside, 4)
        if outside > cfg.wall_max_outside_fraction:
            return WorldFrameResult(
                STATUS_FAILED, AXES_AMBIGUOUS,
                f"직교화한 축으로 방이 +X·+Y 쪽에 놓이지 않는다(QUADRANT: 바닥 점 {outside:.1%} 가 "
                f"{cfg.wall_outside_margin_m} 넘게 밖, 한도 {cfg.wall_max_outside_fraction:.0%}. 두 벽 직교 오차 "
                f"{info.get('orthogonality_error_deg')}°)", ev,
            )
        if info.get("other_families"):
            warnings.append(f"경계 벽 방향이 {len(info['families'])}개다(직사각형이 아닌 방이면 축 선택을 확인)")
        axes_source = AXES_WALLS

    local = local_ma * s
    fl = local[on_floor]
    ev["room"] = {
        "floor_points": int(on_floor.sum()),
        "floor_xy_p2_m": _r(np.percentile(fl[:, :2], 2, axis=0)) if len(fl) else None,
        "floor_xy_p98_m": _r(np.percentile(fl[:, :2], 98, axis=0)) if len(fl) else None,
        "camera_heights_m": {c: round(float(s * floor.distance(centers[i:i + 1])[0]), 4) for i, c in enumerate(ids)},
    }
    ceiling = [t for t in tried if t["max_camera_height"] < 0]  # 모든 카메라보다 위인 수평면
    if ceiling:
        # 천장 높이 = 카메라 중심 평균 아래 바닥까지 + 위 천장까지(바닥 고르기와 같은 기준점). 검산용 기록일 뿐 쓰지 않는다
        floor_h = float(np.mean(floor.distance(centers)))
        ev["room"]["ceiling_height_m"] = round(s * min(floor_h - t["mean_camera_height"] for t in ceiling), 3)
    return WorldFrameResult(
        STATUS_OK, None, None, ev, Ra, origin, s, scale.basis, scale.cross_checks, tuple(warnings), axes_source
    )


def check_floor_with_feet(floor: Plane, feet: np.ndarray, cfg: WorldFrameConfig) -> dict[str, Any]:
    feet = feet[np.isfinite(feet).all(axis=1)] if len(feet) else feet
    if len(feet) < cfg.min_foot_points:
        return {"status": "NOT_CHECKED", "detail": f"발 접지점 {len(feet)} 개 < {cfg.min_foot_points}", "points": len(feet)}
    d = floor.distance(feet)
    # 거리 기준은 절대 거리다(위·아래 오차가 섞여 부호 중앙값이 0 으로 상쇄되는 경우를 막는다). 부호 중앙값은 편향 진단용
    med_abs, p90 = float(np.median(np.abs(d))), float(np.percentile(np.abs(d), 90))
    ok = med_abs <= cfg.foot_median_tol_m and p90 <= cfg.foot_p90_tol_m
    return {
        "status": "CONSISTENT" if ok else "INCONSISTENT", "points": int(len(feet)),
        "median_abs_distance_ma": round(med_abs, 4), "p90_abs_distance_ma": round(p90, 4),
        "signed_median_distance_ma": round(float(np.median(d)), 4),
        "detail": None if ok else (
            f"발 접지점과 MapAnything 바닥이 어긋난다(|d| 중앙값 {med_abs:.3f}, |d| 90% {p90:.3f}; "
            f"한도 {cfg.foot_median_tol_m}·{cfg.foot_p90_tol_m})"
        ),
    }


def _index(items: Sequence[Any], item: Any) -> int:
    return next(i for i, x in enumerate(items) if x is item)


def _r(v: Any, nd: int = 5) -> list[float]:
    return [round(float(x), nd) for x in np.asarray(v).reshape(-1)]
