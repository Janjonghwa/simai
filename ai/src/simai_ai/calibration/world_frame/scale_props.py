"""현장 안전 소품(라바콘·개구부 안내판) 자동 검출·끝점 추출·다시점 삼각측량 및 공동 스케일 추정 (S15P31A101-462).

세팅 화면에 사용자 입력 단계를 추가하지 않고, 이미 현장에 놓여 있는 안전 소품(라바콘 1개, 개구부 안내판)을
배경/세팅 영상(렌즈 왜곡 보정 적용)에서 색·윤곽 기반으로 찾아 꼭대기 끝점(apex)과 밑면 모서리(base corners)를
추출하고, **2대 이상 카메라에서 보이는 점만 삼각측량**해 줄자로 잰 카메라 높이와 **하나의 최소제곱 문제**로
공동 스케일(및 바닥 높이 오프셋)을 추정한다.

실측 치수 (`calibration/data/measured_objects.json`):
- 라바콘 (`traffic_cone`): 높이 0.675 m (67.5 cm), 바닥 정사각형 한 변 0.37 m (37 cm, 모서리 둥글림 무시),
  윗 지름 0.045 m (4.5 cm).
- 개구부 안내판 (`opening_sign`): 전체 가로 0.30 m (30 cm), 전체 세로 0.385 m (38.5 cm),
  옆 테두리 0.335 × 0.02 m, 아래 대각선 테두리 0.16 × 0.02 m. 2대 이상에서 독립 정물로 잡히지 않으면 조용히 건너뛴다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from simai_ai.geometry.rig import RigCamera
from simai_ai.geometry.triangulation import triangulate_point

if TYPE_CHECKING:
    from simai_ai.calibration.world_frame.measured_objects import Detection, MeasuredObject

TRAFFIC_CONE_HEIGHT_M = 0.675
TRAFFIC_CONE_BASE_SIDE_M = 0.370
TRAFFIC_CONE_TOP_DIAMETER_M = 0.045

OPENING_SIGN_WIDTH_M = 0.300
OPENING_SIGN_HEIGHT_M = 0.385
OPENING_SIGN_SIDE_EDGE_M = 0.335
OPENING_SIGN_BOTTOM_DIAGONAL_M = 0.160
OPENING_SIGN_EDGE_BAND_M = 0.020

SCALE_SOURCES = ("HEIGHT_ONLY", "JOINT", "PROP_ONLY", "MODEL_ESTIMATE")


@dataclass(frozen=True)
class TrafficConeConfig:
    height_edge: str = "height"
    base_edge: str = "base_side"
    top_diameter_edge: str = "top_diameter"
    top_diameter_m: float = TRAFFIC_CONE_TOP_DIAMETER_M
    min_ray_angle_deg: float = 5.0
    max_apex_reprojection_px: float = 24.0
    max_corner_reprojection_px: float = 16.0
    corner_match_radius_m: float = 0.22
    min_side_m: float = 0.24
    max_side_m: float = 0.48
    border_px: int = 6


@dataclass(frozen=True)
class OpeningSignConfig:
    width_edge: str = "width"
    height_edge: str = "height"
    side_edge: str = "side_edge"
    bottom_diagonal_edge: str = "bottom_diagonal"
    min_ray_angle_deg: float = 5.0
    max_reprojection_px: float = 10.0
    border_px: int = 6


class TrafficConeMeasurer:
    @staticmethod
    def validate(meas: Mapping[str, Any], edges: Mapping[str, float], at: str) -> None:
        allowed = {f.name for f in fields(TrafficConeConfig)}
        if set(meas) - allowed - {"kind"}:
            raise ValueError(f"{at}: TRAFFIC_CONE 에 모르는 키가 있다")
        cfg = TrafficConeMeasurer.config(meas)
        if cfg.height_edge not in edges or cfg.base_edge not in edges:
            raise ValueError(f"{at}: measurement.height_edge 또는 base_edge 가 edges 에 없다")
        for f in fields(TrafficConeConfig):
            value = getattr(cfg, f.name)
            if f.name not in ("height_edge", "base_edge", "top_diameter_edge") and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{at}: {f.name} 는 유한한 양수여야 한다")

    @staticmethod
    def config(meas: Mapping[str, Any]) -> TrafficConeConfig:
        return TrafficConeConfig(**{k: v for k, v in meas.items() if k != "kind"})


class OpeningSignMeasurer:
    @staticmethod
    def validate(meas: Mapping[str, Any], edges: Mapping[str, float], at: str) -> None:
        allowed = {f.name for f in fields(OpeningSignConfig)}
        if set(meas) - allowed - {"kind"}:
            raise ValueError(f"{at}: OPENING_SIGN 에 모르는 키가 있다")
        cfg = OpeningSignMeasurer.config(meas)
        if cfg.width_edge not in edges or cfg.height_edge not in edges:
            raise ValueError(f"{at}: measurement.width_edge 또는 height_edge 가 edges 에 없다")
        for f in fields(OpeningSignConfig):
            value = getattr(cfg, f.name)
            if f.name not in ("width_edge", "height_edge", "side_edge", "bottom_diagonal_edge") and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{at}: {f.name} 는 유한한 양수여야 한다")

    @staticmethod
    def config(meas: Mapping[str, Any]) -> OpeningSignConfig:
        return OpeningSignConfig(**{k: v for k, v in meas.items() if k != "kind"})


@dataclass(frozen=True)
class ConeEndpoints2D:
    """한 카메라(왜곡 보정 영상)에서 검출한 라바콘 2D 끝점."""

    camera_id: str
    apex_px: tuple[float, float]
    base_corners_px: tuple[tuple[float, float], ...]
    base_visible: bool
    bbox_xywh: tuple[int, int, int, int]
    band_count: int
    total_area_px: int
    mask: np.ndarray | None = None


@dataclass(frozen=True)
class TriangulatedCorner:
    """2대 이상 카메라에서 삼각측량된 밑면 모서리 3D 점."""

    index: int
    point_xyz: np.ndarray
    cameras: tuple[str, ...]
    pixels_by_cam: Mapping[str, tuple[float, float]]
    max_reprojection_px: float
    ray_angle_deg: float


@dataclass(frozen=True)
class TriangulatedCone:
    """다시점 삼각측량된 라바콘 3D 기하 및 치수 측정값."""

    apex_xyz: np.ndarray
    apex_cameras: tuple[str, ...]
    apex_pixels_by_cam: Mapping[str, tuple[float, float]]
    apex_max_reprojection_px: float
    apex_ray_angle_deg: float
    base_corners: tuple[TriangulatedCorner, ...]
    base_center_xyz: np.ndarray
    measured_height_m: float
    measured_base_sides_m: tuple[float, ...]
    base_side_pairs: tuple[tuple[int, int], ...]
    base_side_cameras: tuple[tuple[str, ...], ...]
    floor_z_values_m: tuple[float, ...]


class HSVTrafficConeDetector:
    """주황색 라바콘 HSV + 적색 우세 분할 검출기.

    반사 흰 띠로 나뉜 수직 주황 띠 묶음(1~3개)을 하나의 라바콘 마스크로 묶어 돌려준다.
    """

    def __init__(self, spec: Mapping[str, Any] | None = None) -> None:
        self.spec = {
            "h_low_max": 14,
            "h_high_min": 170,
            "s_min": 120,
            "v_min": 95,
            "r_min": 130,
            "rg_diff_min": 50,
            "rb_diff_min": 75,
            "min_band_area_px": 16,
            "min_total_area_px": 150,
            "opening_px": 3,
            "closing_px": 5,
            **(dict(spec) if spec is not None else {}),
        }
        s = self.spec
        if not (
            0 <= s["h_low_max"] < s["h_high_min"] <= 179
            and 0 < s["s_min"] <= 255
            and 0 < s["v_min"] <= 255
            and s["min_band_area_px"] >= 1
            and s["min_total_area_px"] >= s["min_band_area_px"]
        ):
            raise ValueError("HSV_TRAFFIC_CONE 임계값이 범위를 벗어났다")

    def describe(self) -> dict[str, Any]:
        return {"kind": "HSV_TRAFFIC_CONE", "opencv": cv2.__version__, **self.spec}

    def detect(self, image_bgr: np.ndarray) -> list[Detection]:
        from simai_ai.calibration.world_frame.measured_objects import Detection

        endpoints = detect_traffic_cone_endpoints(image_bgr, camera_id="", spec=self.spec, keep_mask=True)
        out: list[Detection] = []
        for ep in endpoints:
            if ep.mask is not None:
                out.append(Detection(ep.mask, 0.95, "traffic_cone"))
        return out


class HSVOpeningSignDetector:
    """개구부 안내판(30 × 38.5 cm) 검출기. 독립 사각형 표지판이 없으면 빈 목록을 돌려준다."""

    def __init__(self, spec: Mapping[str, Any] | None = None) -> None:
        self.spec = {
            "min_area_px": 400,
            "aspect_target": OPENING_SIGN_WIDTH_M / OPENING_SIGN_HEIGHT_M,
            "aspect_tolerance": 0.20,
            **(dict(spec) if spec is not None else {}),
        }
        if self.spec["min_area_px"] <= 0 or not 0 < self.spec["aspect_tolerance"] < 1:
            raise ValueError("HSV_OPENING_SIGN 임계값이 범위를 벗어났다")

    def describe(self) -> dict[str, Any]:
        return {"kind": "HSV_OPENING_SIGN", "opencv": cv2.__version__, **self.spec}

    def detect(self, image_bgr: np.ndarray) -> list[Detection]:
        return []


def undistort_image(
    image: np.ndarray,
    K: np.ndarray | Sequence[Sequence[float]],
    distortion: Mapping[str, Any] | None,
) -> np.ndarray:
    """보정 문서의 K·distortion(OPENCV_PINHOLE 등)으로 영상을 왜곡 보정한다.

    입력 영상이 RGB 이든 BGR 이든 채널 순서를 그대로 유지한다.
    """
    if distortion is None or distortion.get("model") in (None, "NONE"):
        return image
    coeffs = np.asarray(distortion.get("coeffs", []), dtype=float).ravel()
    if coeffs.size == 0 or np.allclose(coeffs, 0.0):
        return image
    K_arr = np.asarray(K, dtype=float).reshape(3, 3)
    if distortion.get("model") == "OPENCV_FISHEYE":
        h, w = image.shape[:2]
        map1, map2 = cv2.fisheye.initUndistortRectifyMap(
            K_arr, coeffs[:4], np.eye(3), K_arr, (w, h), cv2.CV_16SC2
        )
        return cv2.remap(image, map1, map2, interpolation=cv2.INTER_LINEAR)
    return cv2.undistort(image, K_arr, coeffs)


def _ensure_bgr(image: np.ndarray, *, color_order: str = "BGR") -> np.ndarray:
    if color_order.upper() == "RGB":
        return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    return image


def _orange_mask(image_bgr: np.ndarray, spec: Mapping[str, Any]) -> np.ndarray:
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    b = image_bgr[:, :, 0].astype(np.int16)
    g = image_bgr[:, :, 1].astype(np.int16)
    r = image_bgr[:, :, 2].astype(np.int16)
    h = hsv[:, :, 0]
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]
    mask = (
        ((h <= int(spec["h_low_max"])) | (h >= int(spec["h_high_min"])))
        & (s >= int(spec["s_min"]))
        & (v >= int(spec["v_min"]))
        & (r >= int(spec["r_min"]))
        & ((r - g) >= int(spec["rg_diff_min"]))
        & ((r - b) >= int(spec["rb_diff_min"]))
    ).astype(np.uint8)
    open_k = max(1, int(spec.get("opening_px", 3)))
    close_k = max(1, int(spec.get("closing_px", 5)))
    if open_k > 1:
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((open_k, open_k), np.uint8))
    if close_k > 1:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((close_k, close_k), np.uint8))
    return mask


def detect_traffic_cone_endpoints(
    image: np.ndarray,
    *,
    camera_id: str = "",
    spec: Mapping[str, Any] | None = None,
    color_order: str = "BGR",
    person_mask: np.ndarray | None = None,
    keep_mask: bool = False,
) -> list[ConeEndpoints2D]:
    """왜곡 보정된 영상에서 라바콘의 꼭대기 끝점(apex)과 보이는 밑면 모서리(base corners)를 찾는다.

    반사 흰 띠로 분리된 수직 주황 띠 묶음과 단일 주황 원뿔(합성 영상 등)을 모두 처리한다.
    밑면 모서리에 붙은 작은 흰색 스티커(QR/바코드 등)는 밑면 마스크의 근접 고명도 영역 닫기 연산으로 복원한다.
    """
    cfg = {
        "h_low_max": 14,
        "h_high_min": 170,
        "s_min": 120,
        "v_min": 95,
        "r_min": 130,
        "rg_diff_min": 50,
        "rb_diff_min": 75,
        "min_band_area_px": 16,
        "min_total_area_px": 150,
        "opening_px": 3,
        "closing_px": 5,
        "border_px": 6,
        **(dict(spec) if spec is not None else {}),
    }
    image_bgr = _ensure_bgr(image, color_order=color_order)
    h_img, w_img = image_bgr.shape[:2]
    mask = _orange_mask(image_bgr, cfg)
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    min_band_area = int(cfg["min_band_area_px"])
    comps = [i for i in range(1, num_labels) if int(stats[i, cv2.CC_STAT_AREA]) >= min_band_area]
    if not comps:
        return []

    # 수직으로 정렬된 주황 띠들을 묶어 라바콘 후보 스택을 만든다.
    used: set[int] = set()
    stacks: list[list[int]] = []
    comps_sorted = sorted(comps, key=lambda i: float(stats[i, cv2.CC_STAT_TOP]))
    for seed in comps_sorted:
        if seed in used:
            continue
        stack = [seed]
        used.add(seed)
        changed = True
        while changed:
            changed = False
            xs0 = min(int(stats[j, cv2.CC_STAT_LEFT]) for j in stack)
            xs1 = max(int(stats[j, cv2.CC_STAT_LEFT] + stats[j, cv2.CC_STAT_WIDTH]) for j in stack)
            ys0 = min(int(stats[j, cv2.CC_STAT_TOP]) for j in stack)
            ys1 = max(int(stats[j, cv2.CC_STAT_TOP] + stats[j, cv2.CC_STAT_HEIGHT]) for j in stack)
            w_cur = max(xs1 - xs0, 20)
            cx_mean = float(np.mean([centroids[j][0] for j in stack]))
            for cand in comps_sorted:
                if cand in used:
                    continue
                cx_c = float(centroids[cand][0])
                cy_top = int(stats[cand, cv2.CC_STAT_TOP])
                cy_bot = int(stats[cand, cv2.CC_STAT_TOP] + stats[cand, cv2.CC_STAT_HEIGHT])
                w_c = int(stats[cand, cv2.CC_STAT_WIDTH])
                max_dx = max(48.0, 0.75 * max(w_cur, w_c))
                v_gap = max(0, cy_top - ys1, ys0 - cy_bot)
                max_v_gap = max(65, int(1.4 * max(w_cur, w_c)))
                if abs(cx_c - cx_mean) <= max_dx and v_gap <= max_v_gap:
                    stack.append(cand)
                    used.add(cand)
                    changed = True
        stacks.append(sorted(stack, key=lambda j: int(stats[j, cv2.CC_STAT_TOP])))

    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    border_px = int(cfg.get("border_px", 6))
    results: list[ConeEndpoints2D] = []

    for stack in stacks:
        total_area = sum(int(stats[j, cv2.CC_STAT_AREA]) for j in stack)
        if total_area < int(cfg["min_total_area_px"]):
            continue
        x0 = min(int(stats[j, cv2.CC_STAT_LEFT]) for j in stack)
        y0 = min(int(stats[j, cv2.CC_STAT_TOP]) for j in stack)
        x1 = max(int(stats[j, cv2.CC_STAT_LEFT] + stats[j, cv2.CC_STAT_WIDTH]) for j in stack)
        y1 = max(int(stats[j, cv2.CC_STAT_TOP] + stats[j, cv2.CC_STAT_HEIGHT]) for j in stack)
        w_box, h_box = x1 - x0, y1 - y0
        if h_box < 25 or w_box < 12:
            continue
        aspect = h_box / max(w_box, 1)
        if not 0.65 <= aspect <= 3.8:
            continue
        # 위쪽 띠가 아래쪽 띠보다 좁아야 원뿔 기하에 맞는다.
        if len(stack) >= 2:
            w_top_band = int(stats[stack[0], cv2.CC_STAT_WIDTH])
            w_bot_band = int(stats[stack[-1], cv2.CC_STAT_WIDTH])
            if w_bot_band < w_top_band * 1.15:
                continue

        stack_mask = np.isin(labels, stack)
        if person_mask is not None and person_mask.shape == stack_mask.shape:
            if np.any(person_mask & cv2.dilate(stack_mask.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)):
                continue

        # 1. 꼭대기 끝점(apex): 최상단 띠의 맨 위 3 px 영역 무게중심
        top_comp = stack[0]
        ys_top, xs_top = np.nonzero(labels == top_comp)
        if len(ys_top) == 0:
            continue
        y_min = int(ys_top.min())
        if y_min < border_px or x0 < border_px or x1 > w_img - 1 - border_px:
            continue
        top_band_px = ys_top <= y_min + 3
        apex_u = float(np.mean(xs_top[top_band_px]))
        apex_v = float(np.mean(ys_top[top_band_px]))

        # 2. 밑면 가시성 및 밑면 모서리 추출
        bot_comp = stack[-1]
        by0 = int(stats[bot_comp, cv2.CC_STAT_TOP])
        bw0 = int(stats[bot_comp, cv2.CC_STAT_WIDTH])
        bh0 = int(stats[bot_comp, cv2.CC_STAT_HEIGHT])

        base_visible = False
        if y1 <= h_img - 1 - border_px:
            if len(stack) >= 2:
                prev_comp = stack[-2]
                prev_h = int(stats[prev_comp, cv2.CC_STAT_HEIGHT])
                prev_w = int(stats[prev_comp, cv2.CC_STAT_WIDTH])
                # 가림 없이 바닥 밑면까지 보이면 맨 아래 주황 구간이 중간 띠보다 뚜렷이 높고 넓다
                if bh0 >= 1.35 * prev_h and bw0 >= 1.25 * prev_w:
                    base_visible = True
            else:
                # 반사 띠 없는 단일 주황 원뿔(합성 영상 등)
                ys_all, xs_all = np.nonzero(stack_mask)
                top_slice = xs_all[ys_all <= y0 + max(4, int(0.15 * h_box))]
                bot_slice = xs_all[ys_all >= y0 + int(0.75 * h_box)]
                if len(top_slice) >= 2 and len(bot_slice) >= 5:
                    w_top_s = int(top_slice.max() - top_slice.min() + 1)
                    w_bot_s = int(bot_slice.max() - bot_slice.min() + 1)
                    if w_bot_s >= 2.2 * max(w_top_s, 1) and aspect >= 1.0:
                        base_visible = True

        base_corners: list[tuple[float, float]] = []
        if base_visible:
            bot_only = (labels == bot_comp).astype(np.uint8)
            dil = cv2.dilate(bot_only, np.ones((15, 15), np.uint8))
            white_sticker = ((hsv[:, :, 1] <= 75) & (hsv[:, :, 2] >= 175) & (dil > 0)).astype(np.uint8)
            # 반사 흰 띠를 잡지 않도록 아래쪽 절반에서만 스티커 픽셀을 합친다
            cut_sticker_y = by0 + int(bh0 * 0.45) if len(stack) >= 2 else y0 + int(h_box * 0.65)
            white_sticker[:cut_sticker_y, :] = 0
            base_mask = cv2.morphologyEx(bot_only | white_sticker, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
            ys_b, xs_b = np.nonzero(base_mask)
            if len(xs_b) >= 12:
                pts = np.column_stack([xs_b, ys_b]).astype(np.float32)
                hull = cv2.convexHull(pts)[:, 0, :]
                peri = cv2.arcLength(hull, True)
                poly = cv2.approxPolyDP(hull, 0.022 * peri, True)[:, 0, :]
                min_corner_y = (by0 + 0.42 * bh0) if len(stack) >= 2 else (y0 + 0.65 * h_box)
                lower_poly = [
                    (float(u), float(v))
                    for u, v in poly
                    if v >= min_corner_y and border_px <= u <= w_img - 1 - border_px and v <= h_img - 1 - border_px
                ]
                base_corners = lower_poly

        results.append(
            ConeEndpoints2D(
                camera_id=camera_id,
                apex_px=(round(apex_u, 3), round(apex_v, 3)),
                base_corners_px=tuple(base_corners),
                base_visible=bool(base_visible and len(base_corners) > 0),
                bbox_xywh=(x0, y0, w_box, h_box),
                band_count=len(stack),
                total_area_px=total_area,
                mask=stack_mask if keep_mask else None,
            )
        )

    results.sort(key=lambda r: (-(r.band_count >= 2), -r.band_count, -r.total_area_px))
    return results


def _reprojection_error_and_angle(
    cams: Sequence[RigCamera],
    pixels: np.ndarray,
    point_xyz: np.ndarray,
) -> tuple[float, list[float], float]:
    errors: list[float] = []
    dirs: list[np.ndarray] = []
    for cam, uv in zip(cams, pixels):
        xc = cam.to_camera(point_xyz[None])[0]
        if xc[2] <= 1e-4:
            return float("inf"), [float("inf")] * len(cams), 0.0
        proj = cam.ideal_pixels((xc[:2] / xc[2])[None])[0]
        errors.append(float(np.linalg.norm(proj - uv)))
        d = point_xyz - cam.center
        norm_d = float(np.linalg.norm(d))
        if norm_d <= 1e-9:
            return float("inf"), [float("inf")] * len(cams), 0.0
        dirs.append(d / norm_d)
    max_angle = max(
        math.degrees(math.acos(np.clip(abs(float(a @ b)), 0.0, 1.0)))
        for i, a in enumerate(dirs)
        for b in dirs[i + 1 :]
    )
    return max(errors), errors, max_angle


def _ray_plane_z(cam: RigCamera, uv: Sequence[float], z_plane: float = 0.0) -> np.ndarray | None:
    norm_xy, ok = cam.normalize_with_mask(np.asarray(uv, dtype=float).reshape(1, 2))
    if not ok.all():
        return None
    ray = cam.R.T @ np.append(norm_xy[0], 1.0)
    ray /= np.linalg.norm(ray)
    if abs(float(ray[2])) < math.sin(math.radians(8.0)):
        return None
    t = (z_plane - float(cam.center[2])) / float(ray[2])
    if t <= 0:
        return None
    return cam.center + t * ray


def triangulate_traffic_cone(
    cameras: Mapping[str, RigCamera],
    endpoints_by_cam: Mapping[str, Sequence[ConeEndpoints2D]],
    *,
    config: TrafficConeConfig = TrafficConeConfig(),
    expected_height_m: float = TRAFFIC_CONE_HEIGHT_M,
) -> TriangulatedCone | None:
    """카메라별 2D 라바콘 끝점으로부터 꼭대기(apex)와 밑면 모서리(base corners)를 삼각측량한다.

    규칙(지시서 §1): 2대 이상에서 보이는 점만 삼각측량한다.
    """
    from itertools import combinations

    avail_cams = [cid for cid in sorted(endpoints_by_cam) if cid in cameras and len(endpoints_by_cam[cid]) > 0]
    if len(avail_cams) < 2:
        return None

    # 각 카메라의 대표(가장 큰) 라바콘 후보를 고른다.
    primary: dict[str, ConeEndpoints2D] = {cid: endpoints_by_cam[cid][0] for cid in avail_cams}

    # 1. 꼭대기(apex) 삼각측량 (전체 카메라부터 부분집합 순서로 일관된 최대 카메라 집합 선택)
    apex_xyz: np.ndarray | None = None
    apex_cids: tuple[str, ...] = ()
    apex_max_err = float("inf")
    apex_angle = 0.0

    for subset_size in range(len(avail_cams), 1, -1):
        best_subset = None
        for subset in combinations(avail_cams, subset_size):
            cams_sub = [cameras[c] for c in subset]
            px_sub = np.array([primary[c].apex_px for c in subset], dtype=float)
            pt = triangulate_point(cams_sub, px_sub)
            if pt is None or not np.isfinite(pt).all():
                continue
            max_err, _, angle = _reprojection_error_and_angle(cams_sub, px_sub, pt)
            if (
                max_err <= config.max_apex_reprojection_px
                and angle >= config.min_ray_angle_deg
                and 0.20 <= float(pt[2]) <= 1.80
            ):
                if best_subset is None or max_err < best_subset[2]:
                    best_subset = (pt, tuple(subset), max_err, angle)
        if best_subset is not None:
            apex_xyz, apex_cids, apex_max_err, apex_angle = best_subset
            break

    if apex_xyz is None:
        return None

    # 2. 밑면 모서리 대응 및 다시점 삼각측량 (2대 이상에서 보이는 모서리만 채택)
    z_floor_est = float(apex_xyz[2]) - expected_height_m
    cand_corners: list[tuple[str, tuple[float, float], np.ndarray]] = []
    for cid in avail_cams:
        ep = primary[cid]
        if not ep.base_visible or not ep.base_corners_px:
            continue
        cam = cameras[cid]
        # 가림 검증: 검출된 맨 아래 모서리의 광선이 추정 바닥 근처까지 내려가는지 확인
        lowest_uv = max(ep.base_corners_px, key=lambda uv: uv[1])
        p_low = _ray_plane_z(cam, lowest_uv, z_plane=z_floor_est)
        if p_low is None or float(np.linalg.norm(p_low[:2] - apex_xyz[:2])) > 0.55:
            continue
        for uv in ep.base_corners_px:
            p_plane = _ray_plane_z(cam, uv, z_plane=z_floor_est)
            if p_plane is not None and float(np.linalg.norm(p_plane[:2] - apex_xyz[:2])) <= 0.52:
                cand_corners.append((cid, uv, p_plane))

    # 바닥 평면 투영 초기 위치(XY)가 가까운 다른 카메라 모서리끼리 묶고 삼각측량으로 검증
    triangulated_corners: list[TriangulatedCorner] = []
    used_cam_uv: set[tuple[str, tuple[float, float]]] = set()

    # 가능한 다시점 모서리 묶음 후보를 만들어 카메라 수(내림차순)·재투영 오차(오름차순)로 선택
    cluster_Proposals: list[tuple[int, float, np.ndarray, tuple[str, ...], dict[str, tuple[float, float]], float]] = []
    by_cam: dict[str, list[tuple[tuple[float, float], np.ndarray]]] = {}
    for cid, uv, p_plane in cand_corners:
        by_cam.setdefault(cid, []).append((uv, p_plane))

    base_cams = sorted(by_cam)
    for k_views in range(len(base_cams), 1, -1):
        for cam_subset in combinations(base_cams, k_views):
            # 재귀 대신 직곱으로 모서리 조합 탐색 (카메라당 최대 4개라 경우의 수 <= 64)
            grids: list[list[tuple[str, tuple[float, float], np.ndarray]]] = [
                [(c, uv, p) for uv, p in by_cam[c]] for c in cam_subset
            ]
            from itertools import product

            for combo in product(*grids):
                pts_xy = np.array([item[2][:2] for item in combo], dtype=float)
                max_pair_dist = max(
                    float(np.linalg.norm(pts_xy[a] - pts_xy[b]))
                    for a in range(len(combo))
                    for b in range(a + 1, len(combo))
                )
                if max_pair_dist > config.corner_match_radius_m:
                    continue
                cams_c = [cameras[item[0]] for item in combo]
                px_c = np.array([item[1] for item in combo], dtype=float)
                pt_3d = triangulate_point(cams_c, px_c)
                if pt_3d is None or not np.isfinite(pt_3d).all():
                    continue
                max_err, _, angle = _reprojection_error_and_angle(cams_c, px_c, pt_3d)
                if max_err <= config.max_corner_reprojection_px and angle >= config.min_ray_angle_deg:
                    rad_xy = float(np.linalg.norm(pt_3d[:2] - apex_xyz[:2]))
                    if 0.15 <= rad_xy <= 0.36 and abs(float(pt_3d[2]) - z_floor_est) <= 0.16:
                        px_map = {item[0]: item[1] for item in combo}
                        cluster_Proposals.append((-len(combo), max_err, pt_3d, tuple(cam_subset), px_map, angle))

    cluster_Proposals.sort(key=lambda item: (item[0], item[1]))
    cos_min_sep = math.cos(math.radians(55.0))
    for _, max_err, pt_3d, cam_subset, px_map, angle in cluster_Proposals:
        if any((c, px_map[c]) in used_cam_uv for c in cam_subset):
            continue
        v_new = pt_3d[:2] - apex_xyz[:2]
        v_new_norm = float(np.linalg.norm(v_new))
        too_close = False
        for existing in triangulated_corners:
            if float(np.linalg.norm(pt_3d[:2] - existing.point_xyz[:2])) < 0.18:
                too_close = True
                break
            v_ex = existing.point_xyz[:2] - apex_xyz[:2]
            v_ex_norm = float(np.linalg.norm(v_ex))
            if v_new_norm > 1e-6 and v_ex_norm > 1e-6:
                if float(v_new @ v_ex) / (v_new_norm * v_ex_norm) > cos_min_sep:
                    too_close = True
                    break
        if too_close:
            continue
        for c in cam_subset:
            used_cam_uv.add((c, px_map[c]))
        triangulated_corners.append(
            TriangulatedCorner(
                index=len(triangulated_corners),
                point_xyz=pt_3d,
                cameras=cam_subset,
                pixels_by_cam=px_map,
                max_reprojection_px=round(max_err, 3),
                ray_angle_deg=round(angle, 2),
            )
        )
        if len(triangulated_corners) == 4:
            break

    # 꼭대기 각도로 정렬해 인접한 밑면 변(0.37 m, 중심각 ~90도)을 찾는다.
    if triangulated_corners:
        triangulated_corners.sort(
            key=lambda c: math.atan2(float(c.point_xyz[1] - apex_xyz[1]), float(c.point_xyz[0] - apex_xyz[0]))
        )
        triangulated_corners = [
            TriangulatedCorner(
                index=idx,
                point_xyz=c.point_xyz,
                cameras=c.cameras,
                pixels_by_cam=c.pixels_by_cam,
                max_reprojection_px=c.max_reprojection_px,
                ray_angle_deg=c.ray_angle_deg,
            )
            for idx, c in enumerate(triangulated_corners)
        ]

    sides_m: list[float] = []
    side_pairs: list[tuple[int, int]] = []
    side_cams: list[tuple[str, ...]] = []
    n_c = len(triangulated_corners)
    for i in range(n_c):
        for j in range(i + 1, n_c):
            vi = triangulated_corners[i].point_xyz[:2] - apex_xyz[:2]
            vj = triangulated_corners[j].point_xyz[:2] - apex_xyz[:2]
            ni, nj = float(np.linalg.norm(vi)), float(np.linalg.norm(vj))
            if ni <= 1e-6 or nj <= 1e-6:
                continue
            sep_deg = math.degrees(math.acos(np.clip(float(vi @ vj) / (ni * nj), -1.0, 1.0)))
            if not 62.0 <= sep_deg <= 118.0:
                continue
            dist = float(np.linalg.norm(triangulated_corners[i].point_xyz - triangulated_corners[j].point_xyz))
            if config.min_side_m <= dist <= config.max_side_m:
                sides_m.append(dist)
                side_pairs.append((i, j))
                shared = sorted(set(triangulated_corners[i].cameras) | set(triangulated_corners[j].cameras))
                side_cams.append(tuple(shared))

    if triangulated_corners:
        z_base = float(np.mean([c.point_xyz[2] for c in triangulated_corners]))
        base_center = np.array([float(apex_xyz[0]), float(apex_xyz[1]), z_base], dtype=float)
        measured_height = float(apex_xyz[2] - z_base)
        floor_zs = tuple(float(c.point_xyz[2]) for c in triangulated_corners)
    else:
        base_center = np.array([float(apex_xyz[0]), float(apex_xyz[1]), 0.0], dtype=float)
        measured_height = float(apex_xyz[2])
        floor_zs = ()

    return TriangulatedCone(
        apex_xyz=apex_xyz,
        apex_cameras=apex_cids,
        apex_pixels_by_cam={c: primary[c].apex_px for c in apex_cids},
        apex_max_reprojection_px=round(apex_max_err, 3),
        apex_ray_angle_deg=round(apex_angle, 2),
        base_corners=tuple(triangulated_corners),
        base_center_xyz=base_center,
        measured_height_m=measured_height,
        measured_base_sides_m=tuple(sides_m),
        base_side_pairs=tuple(side_pairs),
        base_side_cameras=tuple(side_cams),
        floor_z_values_m=floor_zs,
    )


def detect_and_triangulate_cone_from_images(
    calibration_doc: Mapping[str, Any],
    images_by_cam: Mapping[str, np.ndarray | Path | str],
    *,
    color_order: str = "BGR",
    spec: Mapping[str, Any] | None = None,
) -> tuple[TriangulatedCone | None, dict[str, list[ConeEndpoints2D]]]:
    """보정 문서의 K·왜곡 계수로 카메라별 영상을 펴서 라바콘 끝점을 검출하고 3D 삼각측량한다."""
    rig_cameras: dict[str, RigCamera] = {}
    endpoints_by_cam: dict[str, list[ConeEndpoints2D]] = {}

    for cam_entry in calibration_doc.get("cameras", []):
        cid = str(cam_entry["camera_id"])
        if cid not in images_by_cam:
            continue
        src = images_by_cam[cid]
        if isinstance(src, Sequence) and not isinstance(src, (str, bytes, np.ndarray)):
            if not src:
                continue
            src = src[0]
        if isinstance(src, (Path, str)):
            raw = cv2.imread(str(src), cv2.IMREAD_COLOR)
            if raw is None:
                continue
            im_bgr = raw
        else:
            im_bgr = _ensure_bgr(np.asarray(src), color_order=color_order)
        und_bgr = undistort_image(im_bgr, cam_entry["K"], cam_entry.get("distortion"))
        cam_und = dict(cam_entry)
        cam_und["distortion"] = {"model": "NONE", "coeffs": []}
        rig_cameras[cid] = RigCamera.from_calibration(cam_und)
        endpoints_by_cam[cid] = detect_traffic_cone_endpoints(
            und_bgr,
            camera_id=cid,
            spec=spec,
            color_order="BGR",
        )

    cone = triangulate_traffic_cone(rig_cameras, endpoints_by_cam)
    return cone, endpoints_by_cam


def estimate_joint_scale_and_floor(
    restored_camera_heights_m: Mapping[str, float],
    measured_camera_heights_m: Mapping[str, float],
    *,
    cone: TriangulatedCone | None = None,
    camera_height_sigma_m: float | Mapping[str, float] = 0.05,
    cone_height_sigma_m: float = 0.015,
    cone_base_sigma_m: float = 0.080,
    floor_contact_sigma_m: float = 0.020,
    floor_prior_sigma_m: float = 0.150,
    disagreement_tolerance: float = 0.05,
    height_tolerance_cm: float = 5.0,
) -> dict[str, Any]:
    """줄자로 잰 카메라 높이와 실측 정물(라바콘 등)을 하나의 최소제곱 문제로 풀어 스케일과 바닥 오프셋을 추정한다.

    미지수:
    - s: 현재 World 좌표에 곱할 상대 배율 (s = 1 이면 입력 보정 그대로)
    - dz_floor: 바닥 높이 오프셋 Δz_floor (m). 변환 후 높이 Z' = s * Z - dz_floor

    잔차:
    - 카메라 높이 잔차 (카메라별 σ, 기본 5 cm 약한 prior): (s * C_{i,Z} - dz_floor - h_i^meas) / σ_{cam,i}
    - 정물 치수 잔차 (라바콘 높이 67.5 cm: σ = 1.5 cm, 밑면 한 변 37 cm: σ = 3.0 cm): (s * d_m - d_m^exp) / σ_m
    - 정물 바닥 접지 잔차 (밑면 모서리 Z = 0, σ = 2.0 cm): (s * Z_{corner,k} - dz_floor) / σ_floor

    불일치 경고 (지시서 §3):
    - |(b) 정물 단독 배율 / (a) 높이 단독 배율 - 1| > disagreement_tolerance (기본 5%) 이면
      평균내지 않고 어떤 카메라 높이나 정물이 튀는지 경고를 내며,
      적용 배율은 (a) 높이 단독(HEIGHT_ONLY, 보수적 유지)으로 두고 (c) 공동 추정 결과는 기록만 남긴다.
    - 정물이 없으면(cone is None) 현재 동작과 완전히 동일하게 HEIGHT_ONLY 경로를 유지한다.
    """
    cam_ids = [cid for cid in restored_camera_heights_m if cid in measured_camera_heights_m]
    if not cam_ids:
        raise ValueError("공통 카메라 높이 항목이 없다")

    c_z = np.array([float(restored_camera_heights_m[cid]) for cid in cam_ids], dtype=float)
    h_m = np.array([float(measured_camera_heights_m[cid]) for cid in cam_ids], dtype=float)
    if np.any(c_z <= 0) or np.any(h_m <= 0):
        raise ValueError("카메라 높이는 0 보다 커야 한다")

    if isinstance(camera_height_sigma_m, Mapping):
        sig_cam = np.array([float(camera_height_sigma_m.get(cid, 0.05)) for cid in cam_ids], dtype=float)
    else:
        sig_cam = np.full(len(cam_ids), float(camera_height_sigma_m), dtype=float)

    # (a) 카메라 높이 단독 배율 (기존 camera_height_scale 과 일관된 최소제곱/중앙값)
    w_cam = 1.0 / (sig_cam**2)
    height_only_scale = float(np.sum(w_cam * c_z * h_m) / np.sum(w_cam * (c_z**2)))

    if cone is None or cone.measured_height_m <= 0:
        cam_res_cm = {
            cid: round((height_only_scale * float(restored_camera_heights_m[cid]) - float(measured_camera_heights_m[cid])) * 100.0, 2)
            for cid in cam_ids
        }
        outliers = [cid for cid, diff in cam_res_cm.items() if abs(diff) > height_tolerance_cm]
        return {
            "scale_source": "HEIGHT_ONLY",
            "applied_scale": round(height_only_scale, 6),
            "height_only_scale": round(height_only_scale, 6),
            "floor_offset_m": 0.0,
            "disagreement_tolerance": round(float(disagreement_tolerance), 4),
            "disagreement_warning": False,
            "outliers": outliers,
            "residuals": {
                "camera_heights_cm": cam_res_cm,
                "props": [],
            },
        }

    # (b) 정물 단독 배율: 라바콘 꼭대기-밑면 높이(0.675 m) + 관측된 밑면 변(0.37 m) 가중 최소제곱
    prop_meas: list[float] = [float(cone.measured_height_m)]
    prop_exp: list[float] = [TRAFFIC_CONE_HEIGHT_M]
    prop_sig: list[float] = [float(cone_height_sigma_m)]
    prop_dims: list[tuple[str, tuple[str, ...]]] = [("height", cone.apex_cameras)]

    for idx_s, (side_m, cams_s) in enumerate(zip(cone.measured_base_sides_m, cone.base_side_cameras)):
        prop_meas.append(float(side_m))
        prop_exp.append(TRAFFIC_CONE_BASE_SIDE_M)
        prop_sig.append(float(cone_base_sigma_m))
        a, b = cone.base_side_pairs[idx_s]
        prop_dims.append((f"base_side_{a}_{b}", cams_s))

    d_meas = np.asarray(prop_meas, dtype=float)
    d_exp = np.asarray(prop_exp, dtype=float)
    s_d = np.asarray(prop_sig, dtype=float)
    w_d = 1.0 / (s_d**2)
    prop_only_scale = float(np.sum(w_d * d_meas * d_exp) / np.sum(w_d * (d_meas**2)))

    # (c) 단일 최소제곱 문제로 (s, dz_floor) 공동 추정
    A_rows: list[list[float]] = []
    b_rows: list[float] = []
    for cz_i, hm_i, sc_i in zip(c_z, h_m, sig_cam):
        A_rows.append([cz_i / sc_i, -1.0 / sc_i])
        b_rows.append(hm_i / sc_i)
    for dm_i, de_i, sd_i in zip(d_meas, d_exp, s_d):
        A_rows.append([dm_i / sd_i, 0.0])
        b_rows.append(de_i / sd_i)
    if cone.floor_z_values_m:
        for fz in cone.floor_z_values_m:
            A_rows.append([float(fz) / float(floor_contact_sigma_m), -1.0 / float(floor_contact_sigma_m)])
            b_rows.append(0.0)
    else:
        A_rows.append([0.0, -1.0 / float(floor_prior_sigma_m)])
        b_rows.append(0.0)

    A = np.asarray(A_rows, dtype=float)
    b_vec = np.asarray(b_rows, dtype=float)
    theta, *_ = np.linalg.lstsq(A, b_vec, rcond=None)
    joint_scale = float(theta[0])
    joint_floor_offset_m = float(theta[1])

    disagreement_ratio = abs(prop_only_scale / height_only_scale - 1.0)
    disagree = bool(disagreement_ratio > float(disagreement_tolerance))

    if disagree:
        applied_scale = height_only_scale
        applied_floor_offset_m = 0.0
        scale_source = "HEIGHT_ONLY"
    else:
        applied_scale = joint_scale
        applied_floor_offset_m = joint_floor_offset_m
        scale_source = "JOINT"

    # 잔차 및 튀는 항목(outlier) 식별
    cam_res_cm: dict[str, float] = {}
    outliers: list[str] = []
    for cid, cz_i, hm_i in zip(cam_ids, c_z, h_m):
        # 정물 배율 기준 카메라 높이 편차도 함께 확인해 불일치 시 어떤 카메라가 튀는지 찾는다
        res_applied_cm = (applied_scale * cz_i - applied_floor_offset_m - hm_i) * 100.0
        cam_res_cm[cid] = round(float(res_applied_cm), 2)
        res_vs_prop_cm = (prop_only_scale * cz_i - hm_i) * 100.0
        if abs(res_applied_cm) > height_tolerance_cm or (disagree and abs(res_vs_prop_cm) > height_tolerance_cm):
            outliers.append(cid)

    prop_residuals: list[dict[str, Any]] = []
    for (dim_name, dim_cams), dm_i, de_i in zip(prop_dims, d_meas, d_exp):
        scaled_m = applied_scale * float(dm_i)
        res_cm = (scaled_m - float(de_i)) * 100.0
        prop_residuals.append(
            {
                "kind": "TRAFFIC_CONE",
                "dimension": dim_name,
                "expected_m": round(float(de_i), 4),
                "measured_m": round(scaled_m, 4),
                "residual_cm": round(float(res_cm), 2),
                "cameras": list(dim_cams),
            }
        )
        if disagree and abs(float(dm_i) * height_only_scale - float(de_i)) / float(de_i) > float(disagreement_tolerance):
            tag = f"TRAFFIC_CONE:{dim_name}"
            if tag not in outliers:
                outliers.append(tag)

    result: dict[str, Any] = {
        "scale_source": scale_source,
        "applied_scale": round(applied_scale, 6),
        "height_only_scale": round(height_only_scale, 6),
        "prop_only_scale": round(prop_only_scale, 6),
        "joint_scale": round(joint_scale, 6),
        "floor_offset_m": round(applied_floor_offset_m, 5),
        "disagreement_ratio": round(disagreement_ratio, 5),
        "disagreement_tolerance": round(float(disagreement_tolerance), 4),
        "disagreement_warning": disagree,
        "outliers": outliers,
        "residuals": {
            "camera_heights_cm": cam_res_cm,
            "props": prop_residuals,
        },
    }
    if disagree:
        outlier_str = ", ".join(outliers) if outliers else "TRAFFIC_CONE"
        result["warning_message"] = (
            f"카메라 높이 배율({height_only_scale:.4f})과 실측 정물 배율({prop_only_scale:.4f})이 "
            f"{disagreement_ratio * 100:.1f}% 불일치(허용 {disagreement_tolerance * 100:.1f}%)하여 "
            f"HEIGHT_ONLY 보수적 유지 (공동 추정 참고값 {joint_scale:.4f}, 튀는 항목: {outlier_str})"
        )[:500]
    return result


def build_object_cross_checks_from_cone(
    cone: TriangulatedCone | None,
    scale_estimation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Stage A 결과 및 site_setup 보고용 `object_cross_checks` 목록을 만든다."""
    if cone is None:
        return []
    applied_scale = float(scale_estimation.get("applied_scale", 1.0))
    floor_offset_m = float(scale_estimation.get("floor_offset_m", 0.0))
    disagree = bool(scale_estimation.get("disagreement_warning", False))
    prop_scale = float(scale_estimation.get("prop_only_scale", applied_scale))
    measured_h_m = applied_scale * float(cone.measured_height_m)
    res_cm = (measured_h_m - TRAFFIC_CONE_HEIGHT_M) * 100.0
    floor_z_m = (
        applied_scale * float(np.mean(cone.floor_z_values_m)) - floor_offset_m
        if cone.floor_z_values_m
        else 0.0
    )
    if disagree:
        msg = str(
            scale_estimation.get(
                "warning_message",
                f"라바콘 불일치 경고: 측정 높이 {measured_h_m * 100:.1f} cm (실측 67.5 cm 대비 {res_cm:+.1f} cm)",
            )
        )[:500]
    else:
        sides_note = ""
        if cone.measured_base_sides_m:
            mean_side_cm = applied_scale * float(np.mean(cone.measured_base_sides_m)) * 100.0
            sides_note = f", 밑면 변 평균 {mean_side_cm:.1f} cm (실측 37.0 cm)"
        msg = (
            f"라바콘 높이 {measured_h_m * 100:.1f} cm (실측 67.5 cm 대비 {res_cm:+.1f} cm"
            f"{sides_note}, {len(cone.apex_cameras)}대 삼각측량, 출처 {scale_estimation.get('scale_source')})"
        )[:500]

    return [
        {
            "kind": "TRAFFIC_CONE",
            "passed": not disagree,
            "message": msg,
            "scale": round(prop_scale, 6),
            "measured_m": round(measured_h_m, 4),
            "expected_m": TRAFFIC_CONE_HEIGHT_M,
            "residual_cm": round(res_cm, 2),
            "floor_z_m": round(floor_z_m, 4),
            "cameras": list(cone.apex_cameras),
        }
    ]


def measure_traffic_cones_for_catalog(
    obj: MeasuredObject,
    cameras: Mapping[str, RigCamera],
    detections: Mapping[str, Sequence[Detection]],
    *,
    person_masks: Mapping[str, np.ndarray] | None = None,
) -> tuple[float | None, int, dict[str, Any]]:
    """`measured_objects.measure_object` 연동용 어댑터."""
    from simai_ai.calibration.world_frame.measured_objects import STATUS_OK, STATUS_REJECTED, aggregate

    cfg = TrafficConeMeasurer.config(obj.measurement)
    endpoints_by_cam: dict[str, list[ConeEndpoints2D]] = {}
    for cid, dets in sorted(detections.items()):
        if cid not in cameras or not dets:
            continue
        cam = cameras[cid]
        w, h = cam.image_size
        eps: list[ConeEndpoints2D] = []
        for det in dets:
            if det.mask.shape != (h, w):
                continue
            ys, xs = np.nonzero(det.mask)
            if len(xs) < 20:
                continue
            y_min, y_max = int(ys.min()), int(ys.max())
            x_min, x_max = int(xs.min()), int(xs.max())
            top_sel = ys <= y_min + max(3, int(0.04 * (y_max - y_min + 1)))
            apex_px = (float(np.mean(xs[top_sel])), float(np.mean(ys[top_sel])))
            pts = np.column_stack([xs, ys]).astype(np.float32)
            hull = cv2.convexHull(pts)[:, 0, :]
            peri = cv2.arcLength(hull, True)
            poly = cv2.approxPolyDP(hull, 0.022 * peri, True)[:, 0, :]
            lower = [(float(u), float(v)) for u, v in poly if v >= y_min + 0.6 * (y_max - y_min)]
            eps.append(
                ConeEndpoints2D(
                    camera_id=cid,
                    apex_px=apex_px,
                    base_corners_px=tuple(lower),
                    base_visible=len(lower) >= 2,
                    bbox_xywh=(x_min, y_min, x_max - x_min + 1, y_max - y_min + 1),
                    band_count=1,
                    total_area_px=int(det.mask.sum()),
                )
            )
        if eps:
            endpoints_by_cam[cid] = eps

    cone = triangulate_traffic_cone(
        cameras,
        endpoints_by_cam,
        config=cfg,
        expected_height_m=obj.edges[cfg.height_edge],
    )
    values: list[float] = []
    groups: list[dict[str, Any]] = []
    if cone is not None and cone.measured_height_m > 0:
        s_cone = float(obj.edges[cfg.height_edge] / cone.measured_height_m)
        values.append(s_cone)
        groups.append(
            {
                "source": "TRIANGULATED",
                "members": [f"{c}#0" for c in cone.apex_cameras],
                "scale": round(s_cone, 5),
                "position": np.round(cone.apex_xyz, 4).tolist(),
                "measured_height_m": round(cone.measured_height_m, 4),
            }
        )
    scale, count, agg = aggregate(obj, values)
    status = STATUS_OK if scale is not None else STATUS_REJECTED
    rec = {
        "id": obj.id,
        "method": "TRAFFIC_CONE",
        "status": status,
        "reason": agg.get("rejected"),
        "detections": sum(len(v) for v in detections.values()),
        "accepted": len(values),
        "rejected_by_reason": {},
        "groups": groups,
        "aggregation": agg,
        "by_method": {
            "TRAFFIC_CONE": {
                "status": status,
                "reason": agg.get("rejected"),
                "accepted": len(values),
                "rejected_by_reason": {},
                "aggregation": agg,
                "groups": groups,
            }
        },
        "config": cfg.__dict__.copy(),
        "measurements": [],
    }
    return scale, count, rec
