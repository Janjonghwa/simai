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
   하나뿐이거나 없거나 수직인 쌍이 없으면 축이 모호하다(AXES_AMBIGUOUS, 사유와 함께 실패).
6. 원점: 두 묶음에서 각각 지지 점이 가장 많은 벽과 바닥, 세 평면의 교점(방 모서리 바닥). X·Y 는 두 벽의 방 안쪽 법선이고
   X × Y = +Z(오른손)가 되도록 어느 벽이 X 인지 정한다. 두 벽의 직교 오차는 Y 가 흡수한다. 직교화한 축에서 바닥 점이
   0.10 m 넘게 −X·−Y 쪽인 비율이 5% 를 넘으면(둔각 모서리) 축이 모호하다고 보고 실패한다(방은 +X·+Y 쪽).
7. 스케일: `choose_scale`(명세 4.3 우선순위). 모르면 MODEL_ESTIMATE(MapAnything 메트릭 = 1).

X_world = s · Ra · (X_mapanything − o). 위 길이 한도는 MapAnything 단위(모델 추정 미터) 기준이다.
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
NO_POINTS = "NO_POINTS"

ORIGIN_RULE = "두 경계 벽 방향 묶음에서 각각 지지 점이 가장 많은 벽과 바닥의 교점, X·Y = 방 안쪽 법선(X × Y = +Z)"


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
    쓰지 않는다(지어내지 않고 다음 근거로). 사람 키 통계는 표본이 가장 많은 것, 나머지는 표본 수 가중 평균이다.
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


# --------------------------------------------------------------------------- 고정


def fix_world_frame(
    points: np.ndarray,
    cam2world: Mapping[str, np.ndarray],
    *,
    scale_estimates: Sequence[ScaleEstimate] = (),
    foot_points: np.ndarray | None = None,
    config: WorldFrameConfig = WorldFrameConfig(),
) -> WorldFrameResult:
    """points: MapAnything world 점(사람·저신뢰 점을 뺀 것), cam2world: camera_id → 4×4 (OpenCV)."""
    cfg = config
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

    walls = find_walls(pts, floor, cfg, rng)
    ev["walls"] = [w.record() for w in walls]
    pair, why, info = choose_axes(walls, up, cfg)
    ev["axes"] = info
    if pair is None:
        return WorldFrameResult(STATUS_FAILED, AXES_AMBIGUOUS, why, ev)
    wa, wb = pair
    if float(np.cross(up, wa.normal) @ wb.normal) > 0:
        wx, wy = wa, wb
    else:
        wx, wy = wb, wa
    X = wx.normal
    Y = np.cross(up, X)
    Ra = np.vstack([X, Y, up])
    origin = np.linalg.solve(np.vstack([wx.normal, wy.normal, up]), [wx.offset, wy.offset, floor.offset])
    ev["axes"].update({
        "rule": ORIGIN_RULE, "x_wall": _index(walls, wx), "y_wall": _index(walls, wy),
        "x_axis_ma": _r(X), "y_axis_ma": _r(Y), "z_axis_ma": _r(up),
        "y_vs_wall_deg": round(_line_angle_deg(Y, wy.normal), 3),
    })
    ev["origin"] = {"point_ma": _r(origin), "rule": ORIGIN_RULE}

    # 직교화(Y = up × X) 뒤에도 방이 +X·+Y 쪽인지 본다. 원점 모서리가 90° 보다 넓게 열리면(둔각, 안쪽 법선 사이 < 90°)
    # 직교 축으로는 방이 음의 쪽으로 넘어간다. 경계 벽과 같은 기준(0.10 m 넘게 밖인 바닥 점 5% 이하)으로 막는다
    on_floor = np.abs(floor.distance(pts)) < cfg.plane_tol_m
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

    scale = choose_scale(scale_estimates)
    ev["scale"] = scale.record
    s = scale.scale
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
    warnings = []
    if info.get("other_families"):
        warnings.append(f"경계 벽 방향이 {len(info['families'])}개다(직사각형이 아닌 방이면 축 선택을 확인)")
    return WorldFrameResult(
        STATUS_OK, None, None, ev, Ra, origin, s, scale.basis, scale.cross_checks, tuple(warnings)
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
