"""구역별 관측 카메라: R7 입력 `MonitoringState.zone_cameras` (명세 7.2 R7, 6.2, 6.5, risk.monitoring).

R7(구역 감시 능력 저하)은 구역마다 그 구역을 시야에 담는 카메라 목록이 있어야 판정할 수 있다(없으면 저하로 본다).
목록은 보정(카메라 자세·화각·렌즈 왜곡)과 구역(다각형·높이)으로 정해지므로 Tier 0 가 보정·구역 버전이 바뀔 때 계산해 넘긴다.

판정(보수적, 모르면 담지 않는다): 구역을 바닥(Z=0)과 확인 높이(head_height_m) 사이의 기둥으로 본다(VOLUME_3D 는 z_range).
카메라가 그 기둥을 '담는다'는 것은 아래가 모두 성립할 때다. 하나라도 아니면 그 카메라는 그 구역을 담지 않는다.

1. 기둥의 모든 꼭짓점이 카메라 앞(깊이 > min_depth_m)에 있다.
2. 정규화 평면에서 꼭짓점 투영의 볼록 껍질(= 핀홀 투영한 기둥 전체) P 가 렌즈 왜곡 식의 첫 단조 가지 안이다
   (distortion.radial_branch. 그 너머는 왜곡 식이 접혀 밖의 광선이 영상 안으로 접혀 들어올 수 있다).
3. P 의 경계를 촘촘히(변마다 edge_samples 개) 실제 왜곡 식(distortion.distort)으로 투영한 픽셀이 모두 영상 안(가장자리
   margin_px 안쪽)이다. 반지름 방향 왜곡은 가지 안에서 방향을 보존하는 단조 사상이라 경계가 영상(볼록) 안이면 안쪽도 안이다.

보수성을 보장할 수 없는 모델·계수는 '미확인'으로 빼고 그 카메라를 세지 않는다: 접선 왜곡 계수(p1·p2)가 0 이 아닌
OPENCV_PINHOLE(방향을 보존하지 않아 경계만으로 안쪽을 보장할 수 없다), 가지를 구할 수 없는 계수(가지 반지름 0).
가림(사람·가구)은 보지 않는다(정적 3D 가 없는 MVP 범위). 경계 표본 사이의 미세한 휨은 margin_px 로 흡수한다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from simai_ai.geometry import distortion
from simai_ai.geometry.rig import CameraRig, RigCamera

DEFAULT_HEAD_HEIGHT_M = 1.8
"""바닥 구역을 확인하는 위쪽 높이(사람 머리 높이 근처). 바닥만 보이고 사람 몸통이 잘리는 카메라를 세지 않는다"""
DEFAULT_MARGIN_PX = 2.0
DEFAULT_MIN_DEPTH_M = 0.1
DEFAULT_EDGE_SAMPLES = 64


def _heights(zone: Any, head_height_m: float) -> tuple[float, ...]:
    z_range = getattr(zone, "z_range", None)
    if z_range is not None:
        return (float(z_range[0]), float(z_range[1]))
    return (0.0, head_height_m)


def coverage_supported(camera: RigCamera) -> bool:
    """이 카메라의 왜곡 모델·계수로 보수적 판정을 할 수 있는지(아니면 '미확인'으로 담지 않는다)."""
    model, coeffs = camera.distortion_model, camera.distortion_coeffs
    if model == distortion.MODEL_PINHOLE and len(coeffs) >= 4 and (coeffs[2] != 0.0 or coeffs[3] != 0.0):
        return False  # 접선 왜곡: 방향 보존이 아니라 경계 검사만으로 안쪽을 보장할 수 없다
    return model in (distortion.MODEL_NONE, distortion.MODEL_PINHOLE, distortion.MODEL_FISHEYE)


def _hull(points: np.ndarray) -> np.ndarray:
    """2D 볼록 껍질(반시계, 단조 사슬). 점이 둘 이하면 그대로."""
    pts = sorted({(float(x), float(y)) for x, y in points})
    if len(pts) <= 2:
        return np.array(pts, dtype=float)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1], dtype=float)


def _boundary(hull: np.ndarray, samples: int) -> np.ndarray:
    if len(hull) == 1:
        return hull
    t = np.linspace(0.0, 1.0, samples, endpoint=False)[:, None]
    nxt = np.roll(hull, -1, axis=0)
    return np.concatenate([a + t * (b - a) for a, b in zip(hull, nxt, strict=True)])


def camera_sees_points(
    camera: RigCamera,
    points: np.ndarray,
    *,
    margin_px: float = DEFAULT_MARGIN_PX,
    min_depth_m: float = DEFAULT_MIN_DEPTH_M,
    edge_samples: int = DEFAULT_EDGE_SAMPLES,
) -> bool:
    """World 점 (N, 3) 의 볼록 껍질 전체가 카메라 앞, 왜곡 가지 안, 실제 왜곡 투영으로 영상 안(margin_px 안쪽)인지."""
    if not coverage_supported(camera):
        return False
    cam = camera.to_camera(points)
    if not np.all(np.isfinite(cam)) or np.any(cam[:, 2] <= min_depth_m):
        return False
    hull = _hull(cam[:, :2] / cam[:, 2:3])
    branch = distortion.radial_branch(camera.distortion_model, camera.distortion_coeffs)
    r = np.hypot(hull[:, 0], hull[:, 1])
    limit = np.arctan(r) if camera.distortion_model == distortion.MODEL_FISHEYE else r
    if branch.r_max <= 0 or not np.all(limit < branch.r_max):
        return False  # 가지 밖: 왜곡 식이 접혀 영상 안 판단을 믿을 수 없다(P 와 가지 원판이 모두 볼록이라 꼭짓점이면 충분)
    edge = _boundary(hull, max(edge_samples, 1))
    pixels = camera.ideal_pixels(distortion.distort(edge, camera.distortion_model, camera.distortion_coeffs))
    if not np.all(np.isfinite(pixels)):
        return False
    width, height = camera.image_size
    u, v = pixels[:, 0], pixels[:, 1]
    return bool(
        np.all(u >= margin_px) and np.all(u <= width - margin_px) and np.all(v >= margin_px) and np.all(v <= height - margin_px)
    )


def zone_cameras(
    rig: CameraRig,
    zones: Iterable[Any],
    *,
    head_height_m: float = DEFAULT_HEAD_HEIGHT_M,
    margin_px: float = DEFAULT_MARGIN_PX,
    min_depth_m: float = DEFAULT_MIN_DEPTH_M,
) -> Mapping[str, tuple[str, ...]]:
    """구역 ID → 그 구역을 담는 카메라(정렬). 구역은 risk.Zone(zone_id, polygon_xy, z_range) 모양이면 된다.

    같은 zone_id 의 버전이 여럿이면(유효 구간이 다른 버전) 모든 버전을 담는 카메라만 남긴다(어느 버전이 판정에 쓰여도
    목록이 맞게, 보수적).
    """
    if not (head_height_m > 0 and math.isfinite(head_height_m)):
        raise ValueError(f"head_height_m 은 양수다: {head_height_m}")
    out: dict[str, set[str]] = {}
    for zone in zones:
        polygon = [tuple(float(c) for c in p) for p in zone.polygon_xy]
        points = np.array([[x, y, z] for z in _heights(zone, head_height_m) for x, y in polygon], dtype=float)
        seen = {
            cid
            for cid, camera in rig.cameras.items()
            if camera_sees_points(camera, points, margin_px=margin_px, min_depth_m=min_depth_m)
        }
        out[zone.zone_id] = seen if zone.zone_id not in out else out[zone.zone_id] & seen
    return {zone_id: tuple(sorted(cams)) for zone_id, cams in sorted(out.items())}
