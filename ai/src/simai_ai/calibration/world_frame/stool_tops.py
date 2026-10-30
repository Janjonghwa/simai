"""하늘색 스툴 윗면: HSV 분할 → 네 모서리 → 보정 대응 → 공통 삼각측량(-357).

길이는 입력 보정 단위다. 축척은 실측 / 입력 길이인 보정 제안이며 보정·World 상태를 고치지 않는다.
높이 평면 교차는 대응의 초기값과 단안 보조 기록에만 쓴다. 근거 집계에는 다시점 독립 스툴만 넣는다.
"""

from __future__ import annotations

import math
from itertools import combinations
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, TYPE_CHECKING

import numpy as np

from simai_ai.geometry.rig import RigCamera
from simai_ai.geometry.triangulation import triangulate_point

if TYPE_CHECKING:
    from simai_ai.calibration.world_frame.measured_objects import Detection, MeasuredObject


@dataclass(frozen=True)
class StoolConfig:
    top_edge: str = "top_width"
    height_m: float = 0.425
    match_radius: float = 0.20
    min_ray_angle_deg: float = 5.0
    max_reprojection_px: float = 8.0
    max_ray_residual: float = 0.03
    max_height_spread: float = 0.05
    max_tilt_deg: float = 10.0
    square_tolerance: float = 0.20
    plane_square_tolerance: float = 0.35
    min_edge_px: float = 25.0
    border_px: int = 8
    max_size_ratio: float = 1.8


class StoolTopMeasurer:
    @staticmethod
    def validate(meas: Mapping[str, Any], edges: Mapping[str, float], at: str) -> None:
        allowed = {f.name for f in fields(StoolConfig)}
        if set(meas) - allowed - {"kind"}:
            raise ValueError(f"{at}: STOOL_TOP 에 모르는 키가 있다")
        cfg = StoolTopMeasurer.config(meas)
        if cfg.top_edge not in edges:
            raise ValueError(f"{at}: measurement.top_edge 가 edges 에 없다")
        for f in fields(StoolConfig):
            value = getattr(cfg, f.name)
            if f.name != "top_edge" and (isinstance(value, bool) or not isinstance(value, (int, float))
                                        or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{at}: {f.name} 는 유한한 양수여야 한다")

    @staticmethod
    def config(meas: Mapping[str, Any]) -> StoolConfig:
        return StoolConfig(**{k: v for k, v in meas.items() if k != "kind"})


class HSVStoolDetector:
    """OpenCV H(0~179)·S·V. 인접 윗면의 짙은 틈과 청록/청색 색차로 나눈다.

    회색·흰 반사광을 채우기 위해 윤곽 내부를 채우지만, 실제 색 픽셀 덮임 70% 미만은 버린다.
    완전히 같은 색·이음매 없는 붙은 면은 임의로 반분하지 않는다(측정의 정사각형 검사에서 거절).
    """

    def __init__(self, spec: Mapping[str, Any]) -> None:
        self.spec = {"h_min": 85, "h_max": 115, "s_min": 45, "v_min": 185,
                     "h_splits": [94, 103], "v_levels": [185, 210, 235],
                     "min_area_px": 600, "opening_px": 3, **dict(spec)}
        s = self.spec
        boundaries = [s["h_min"], *s["h_splits"], s["h_max"] + 1]
        if not (0 <= s["h_min"] < s["h_max"] <= 179 and all(a < b for a, b in zip(boundaries, boundaries[1:]))
                and 0 < s["s_min"] < 255 and 0 < s["v_min"] < 255
                and s["min_area_px"] > 0 and s["opening_px"] >= 1):
            raise ValueError("HSV_STOOL 임계값이 범위를 벗어났다")

    def describe(self) -> dict[str, Any]:
        import cv2

        return {"kind": "HSV_STOOL", "opencv": cv2.__version__, **self.spec}

    def detect(self, image_bgr: np.ndarray) -> list[Detection]:
        import cv2

        from simai_ai.calibration.world_frame.measured_objects import Detection

        hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
        s = self.spec
        kernel = np.ones((int(s["opening_px"]),) * 2, np.uint8)
        out = []
        boundaries = [s["h_min"], *s["h_splits"], s["h_max"] + 1]
        # 전체 색역은 한 윗면 안의 색 번짐을 보존하고, 나눈 색역은 붙은 면의 가설을 추가한다.
        ranges = [(lo, upper, v) for v in s["v_levels"]
                  for lo, upper in [(s["h_min"], s["h_max"] + 1), *zip(boundaries, boundaries[1:])]]
        for lo, upper, value in ranges:
            hi = upper - 1
            mask = cv2.inRange(hsv, (lo, s["s_min"], max(value, s["v_min"])), (hi, 255, 255))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            for contour in contours:
                if cv2.contourArea(contour) < s["min_area_px"]:
                    continue
                quad = fit_quad(contour)
                if quad is None:
                    continue
                area = cv2.contourArea(quad.astype(np.float32))
                coverage = cv2.contourArea(contour) / max(area, 1)
                if not 0.70 <= coverage <= 1.15:
                    continue
                filled = np.zeros(mask.shape, np.uint8)
                cv2.drawContours(filled, [contour], -1, 1, cv2.FILLED)
                filled = filled.astype(bool)
                if any(np.count_nonzero(filled & d.mask) / np.count_nonzero(filled | d.mask) > .75 for d in out):
                    continue
                out.append(Detection(filled, min(coverage, 1.0), "stool_top"))
        return sorted(out, key=lambda d: tuple(np.argwhere(d.mask).min(axis=0)[::-1]))


def fit_quad(contour: np.ndarray) -> np.ndarray | None:
    """둥근 모서리를 제외한 네 직선을 맞춰 교차시킨다. 모서리 픽셀을 그대로 변 끝으로 쓰지 않는다."""
    import cv2

    hull = cv2.convexHull(contour)
    perimeter = cv2.arcLength(hull, True)
    q = None
    for eps in (0.02, 0.03, 0.04, 0.05):
        poly = cv2.approxPolyDP(hull, eps * perimeter, True)
        if len(poly) == 4:
            q = poly[:, 0].astype(float)
            break
    if q is None or not cv2.isContourConvex(q.astype(np.float32)):
        return None
    pts = contour.reshape(-1, 2).astype(float)
    lines = []
    for a, b in zip(q, np.roll(q, -1, axis=0)):
        delta = b - a
        length = np.linalg.norm(delta)
        if length < 10:
            return None
        along = (pts - a) @ delta / length**2
        distance = np.abs(delta[0] * (pts[:, 1] - a[1]) - delta[1] * (pts[:, 0] - a[0])) / length
        chosen = pts[(along > 0.15) & (along < 0.85) & (distance < max(3, length * 0.04))]
        if len(chosen) < 5:
            return None
        vx, vy, x, y = cv2.fitLine(chosen.astype(np.float32), cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        n = np.array([-vy, vx], float)
        lines.append((n, float(n @ [x, y])))
    corners = []
    for i in range(4):
        A = np.array([lines[i - 1][0], lines[i][0]])
        if abs(np.linalg.det(A)) < 0.1:
            return None
        corners.append(np.linalg.solve(A, [lines[i - 1][1], lines[i][1]]))
    corners = np.array(corners)
    if not cv2.isContourConvex(corners.astype(np.float32)) or np.max(np.linalg.norm(corners - q, axis=1)) > 0.12 * perimeter:
        return None
    return corners


def _plane_points(cam: RigCamera, pixels: np.ndarray, normal: np.ndarray, offset: float,
                  height: float) -> tuple[np.ndarray, np.ndarray] | None:
    uv, ok = cam.normalize_with_mask(pixels)
    if not ok.all():
        return None
    dirs = np.column_stack([uv, np.ones(len(uv))]) @ cam.R
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    denom = dirs @ normal
    if np.any(np.abs(denom) < math.sin(math.radians(10))):
        return None
    t = (height - offset - cam.center @ normal) / denom
    if np.any(t <= 0):
        return None
    return cam.center + t[:, None] * dirs, dirs


def _square(points: np.ndarray, tolerance: float) -> bool:
    edges = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(edges, axis=1)
    if lengths.min() <= 0 or lengths.max() / lengths.min() - 1 > tolerance:
        return False
    cos = np.sum(edges * np.roll(edges, 1, axis=0), axis=1) / (lengths * np.roll(lengths, 1))
    return bool(np.max(np.abs(cos)) <= tolerance)


def _orders() -> list[np.ndarray]:
    return [np.roll(np.arange(4)[::sign], shift) for sign in (1, -1) for shift in range(4)]


def _triangulate_all(members: Sequence[dict], cameras: Mapping[str, RigCamera], cfg: StoolConfig,
                 normal: np.ndarray) -> tuple[np.ndarray | None, dict[str, Any]]:
    cams = [cameras[m["camera_id"]] for m in members]
    if len(cams) < 2:
        return None, {"status": "NOT_ENOUGH_VIEWS"}
    ref = np.asarray(members[0]["plane_points"])
    pixels = []
    for m in members:
        p = np.asarray(m["plane_points"])
        order = min(_orders(), key=lambda order: np.linalg.norm(p[order] - ref, axis=1).sum())
        pixels.append(np.asarray(m["corners_px"])[order])
    points, residuals, angles, errors = [], [], [], []
    for j in range(4):
        px = np.array([uv[j] for uv in pixels])
        # Tier 0와 같은 기하 모듈을 쓴다. 오프라인 전용 삼각측량을 만들지 않는다.
        point = triangulate_point(cams, px)
        if point is None:
            return None, {"status": "DEGENERATE"}
        dirs = []
        for cam, uv in zip(cams, px):
            d = point - cam.center
            d /= np.linalg.norm(d)
            dirs.append(d)
            observed = cam.normalize(uv[None])[0]
            ray = cam.R.T @ np.append(observed, 1)
            ray /= np.linalg.norm(ray)
            residuals.append(float(np.linalg.norm(np.cross(point - cam.center, ray))))
            xc = cam.to_camera(point[None])[0]
            projected = cam.ideal_pixels((xc[:2] / xc[2])[None])[0]
            errors.append(float(np.linalg.norm(projected - cam.ideal_pixels(observed[None])[0])))
        angles.append(max(math.degrees(math.acos(np.clip(abs(a @ b), 0, 1)))
                          for i, a in enumerate(dirs) for b in dirs[i + 1:]))
        points.append(point)
    points = np.array(points)
    rec = {"max_ray_residual": round(max(residuals), 5), "max_reprojection_px": round(max(errors), 3),
           "min_ray_angle_deg": round(min(angles), 3)}
    reason = None
    if max(errors) > cfg.max_reprojection_px or max(residuals) > cfg.max_ray_residual:
        reason = "RAYS_DISAGREE"
    elif min(angles) < cfg.min_ray_angle_deg:
        reason = "NARROW_ANGLE"
    elif np.ptp(points @ normal) > cfg.max_height_spread:
        reason = "NOT_HORIZONTAL"
    else:
        _, _, vh = np.linalg.svd(points - points.mean(axis=0))
        tilt = math.degrees(math.acos(np.clip(abs(vh[-1] @ normal), 0, 1)))
        rec["tilt_deg"] = round(tilt, 3)
        if tilt > cfg.max_tilt_deg:
            reason = "NOT_HORIZONTAL"
        elif not _square(points, cfg.square_tolerance):
            reason = "NOT_SQUARE"
    rec["status"] = reason or "OK"
    return (None if reason else points), rec


def _triangulate(members: Sequence[dict], cameras: Mapping[str, RigCamera], cfg: StoolConfig,
                 normal: np.ndarray) -> tuple[np.ndarray | None, dict[str, Any]]:
    points, rec = _triangulate_all(members, cameras, cfg, normal)
    if points is not None or len(members) < 3:
        return points, rec
    # 가린/잘못 맞춘 카메라 한 대가 나머지의 일치하는 관측까지 버리게 하지 않는다.
    for size in range(len(members) - 1, 1, -1):
        candidates = []
        for subset in combinations(members, size):
            p, report = _triangulate_all(subset, cameras, cfg, normal)
            if p is not None:
                candidates.append((p, report, subset))
        if not candidates:
            continue
        lengths = [float(np.median(np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1))) for p, _, _ in candidates]
        if max(lengths) / min(lengths) - 1 > .07:
            return None, {"status": "AMBIGUOUS_VIEW_SUBSETS", "full_set": rec}
        p, report, subset = min(candidates, key=lambda v: v[1]["max_reprojection_px"])
        ids = {id(m) for m in subset}
        report |= {"excluded_members": [f"{m['camera_id']}#{m['index']}" for m in members if id(m) not in ids],
                   "full_set": rec}
        return p, report
    return None, rec


def measure_stools(obj: MeasuredObject, cameras: Mapping[str, RigCamera], detections: Mapping[str, Sequence[Detection]],
                   *, normal: np.ndarray | None = None, offset: float = 0.0,
                   person_masks: Mapping[str, np.ndarray] | None = None) -> tuple[float | None, int, dict[str, Any]]:
    """한 스툴당 네 변 중앙값 하나. 삼각측량 성공한 스툴만 aggregate 에 넣는다. 바닥은 n·x + offset = 0."""
    import cv2

    from simai_ai.calibration.world_frame.measured_objects import aggregate

    cfg = StoolTopMeasurer.config(obj.measurement)
    normal = np.array([0., 0., 1.]) if normal is None else np.asarray(normal, float)
    measurements = []
    for cid, dets in sorted(detections.items()):
        cam = cameras[cid]
        w, h = cam.image_size
        for i, det in enumerate(dets):
            if det.mask.shape != (h, w):
                raise ValueError(f"{cid}: 검출 마스크와 보정 해상도가 다르다")
            contours, _ = cv2.findContours(det.mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            q = fit_quad(max(contours, key=cv2.contourArea)) if contours else None
            rec = {"camera_id": cid, "index": i, "status": "REJECTED", "reason": None,
                   "mask_area_px": int(det.mask.sum())}
            measurements.append(rec)
            if q is None:
                rec["reason"] = "NO_QUAD"
                continue
            rec["corners_px"] = np.round(q, 4).tolist()
            reason = None
            if np.any(q < cfg.border_px) or np.any(q[:, 0] > w - 1 - cfg.border_px) or np.any(q[:, 1] > h - 1 - cfg.border_px):
                reason = "TRUNCATED"
            elif np.min(np.linalg.norm(np.roll(q, -1, axis=0) - q, axis=1)) < cfg.min_edge_px:
                reason = "EDGE_TOO_SHORT_PX"
            person = (person_masks or {}).get(cid)
            if person is not None:
                if person.shape != det.mask.shape:
                    raise ValueError(f"{cid}: 사람 마스크 크기가 다르다")
                if np.any(person & cv2.dilate(det.mask.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)):
                    reason = "ON_PERSON"
            if reason:
                rec["reason"] = reason
                continue
            plane = _plane_points(cam, q, normal, offset, cfg.height_m)
            if plane is None:
                rec["reason"] = "PLANE_UNSTABLE"
                continue
            points, _ = plane
            lengths = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
            rec |= {"plane_points": np.round(points, 6).tolist(), "position": points.mean(axis=0).tolist(),
                    "auxiliary_edge_lengths": lengths.tolist(), "auxiliary_scale": obj.edges[cfg.top_edge] / float(np.median(lengths))}
            known = obj.edges[cfg.top_edge]
            if lengths.max() > known * cfg.max_size_ratio or lengths.min() < known / cfg.max_size_ratio:
                rec["reason"] = "IMPLAUSIBLE_SIZE_ON_HEIGHT_PLANE"
                continue
            if not _square(points, cfg.plane_square_tolerance):
                rec["reason"] = "NOT_SQUARE_ON_HEIGHT_PLANE"
                continue
            rec["status"] = "ACCEPTED"
    # 겹치는 색역 가설을 독립 스툴로 세지 않는다. 부분 색 조각보다 넓게 관측된 윗면을 남긴다.
    selected = []
    for m in sorted([m for m in measurements if m["status"] == "ACCEPTED"], key=lambda m: -m["mask_area_px"]):
        if any(a["camera_id"] == m["camera_id"] and np.linalg.norm(np.asarray(a["position"]) - m["position"]) < .25
               for a in selected):
            m |= {"status": "REJECTED", "reason": "DUPLICATE_COLOR_HYPOTHESIS"}
        else:
            selected.append(m)
    # 완전 연결 + 한 카메라 한 검출. 단일 연결 사슬로 이웃 스툴을 합치지 않는다.
    groups: list[list[dict]] = []
    for m in measurements:
        if m["status"] != "ACCEPTED":
            continue
        candidates = [g for g in groups if all(a["camera_id"] != m["camera_id"]
                      and np.linalg.norm(np.asarray(a["position"]) - m["position"]) <= cfg.match_radius for a in g)]
        if len(candidates) == 1:
            candidates[0].append(m)
        elif len(candidates) > 1:
            m |= {"status": "REJECTED", "reason": "AMBIGUOUS_MATCH"}
        else:
            groups.append([m])
    values, group_recs = [], []
    for group_index, g in enumerate(groups):
        points, tri = _triangulate(g, cameras, cfg, normal)
        excluded = set(tri.get("excluded_members", []))
        for m in g:
            m["group_index"] = group_index
            m["triangulation_status"] = tri["status"]
            m["used_in_triangulation"] = points is not None and f"{m['camera_id']}#{m['index']}" not in excluded
        gr = {"members": [f"{m['camera_id']}#{m['index']}" for m in g], "triangulation": tri,
              "source": "TRIANGULATED" if points is not None else "HEIGHT_PLANE_AUXILIARY",
              "auxiliary_scale": float(np.median([m["auxiliary_scale"] for m in g]))}
        if points is not None:
            lengths = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
            scale = obj.edges[cfg.top_edge] / float(np.median(lengths))
            gr |= {"corners": np.round(points, 6).tolist(), "edge_lengths": lengths.tolist(), "scale": scale}
            values.append(scale)
        group_recs.append(gr)
    scale, count, agg = aggregate(obj, values)
    descriptive = None
    if values:
        med = float(np.median(values))
        sigma = 1.4826 * float(np.median(np.abs(np.asarray(values) - med)))
        descriptive = {"sample_count": len(values), "median_scale": med,
                       "relative_standard_error": 1.2533 * sigma / med / math.sqrt(len(values)),
                       "note": "이상치 제거 전 기술 통계. 최소 표본/일치도 검사 전 값이며 채택 근거가 아님"}
    reasons: dict[str, int] = {}
    for m in measurements:
        if m["reason"]:
            reasons[m["reason"]] = reasons.get(m["reason"], 0) + 1
    rec = {"id": obj.id, "method": "STOOL_TOP", "status": "OK" if scale is not None else "REJECTED",
           "reason": agg.get("rejected"), "detections": len(measurements),
           "accepted": sum(m["status"] == "ACCEPTED" for m in measurements), "rejected_by_reason": reasons,
           "stool_candidates": len(groups), "multiview_samples": len(values), "groups": group_recs,
           "aggregation": agg, "descriptive": descriptive, "measurements": measurements, "config": cfg.__dict__, "by_method": {},
           "note": "높이 평면 값은 보조만. 삼각측량 성공 스툴당 네 변 중앙값 하나를 독립 표본으로 집계"}
    return scale, count, rec


def draw_stools(image: np.ndarray, measurements: Sequence[Mapping[str, Any]]) -> np.ndarray:
    import cv2

    out = image.copy()
    for m in measurements:
        if "corners_px" not in m:
            continue
        q = np.rint(m["corners_px"]).astype(np.int32)
        used = m.get("used_in_triangulation", False)
        color = (0, 210, 0) if used else ((0, 200, 255) if m["status"] == "ACCEPTED" else (0, 0, 255))
        cv2.polylines(out, [q], True, color, 2)
        label = f"#{m['index']} " + (f"g{m['group_index']} {'TRI' if used else 'AUX'}" if "group_index" in m else str(m['reason']))
        cv2.putText(out, label, tuple(q.min(axis=0) - [0, 5]), cv2.FONT_HERSHEY_SIMPLEX, .55, color, 2)
    return out
