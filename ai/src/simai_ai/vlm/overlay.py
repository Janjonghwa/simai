"""얼굴 처리된 VLM 프레임에만 이벤트 기하를 메모리에서 표시한다."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from simai_ai.vlm.face_blur import VlmFrame


def _cv2():
    """OpenCV 는 선택 의존성(extra io)이라 쓸 때만 불러온다. 워커 이미지에는 있고, 기본 설치(nightly CI)에는 없다."""
    import cv2

    return cv2


NEAR = 0.01  # 카메라 평면의 특이점과 무한 확대를 피하는 1 cm 근접 평면


def clip_polygon(points: np.ndarray, axis: int, bound: float, greater: bool) -> np.ndarray:
    """한 평면으로 다각형을 자른다(Sutherland–Hodgman)."""
    out = []
    if len(points) == 0:
        return points
    prev = points[-1]
    inside_prev = prev[axis] >= bound if greater else prev[axis] <= bound
    for cur in points:
        inside = cur[axis] >= bound if greater else cur[axis] <= bound
        if inside != inside_prev:
            out.append(prev + (cur - prev) * ((bound - prev[axis]) / (cur[axis] - prev[axis])))
        if inside:
            out.append(cur)
        prev, inside_prev = cur, inside
    return np.asarray(out, dtype=float).reshape(-1, points.shape[1])


def project(camera: Mapping[str, Any], camera_points: np.ndarray) -> np.ndarray:
    """카메라 좌표를 왜곡 포함 픽셀로 투영한다."""
    cv2 = _cv2()
    k = np.asarray(camera['K'], dtype=float)
    distortion = camera['distortion']
    coeffs = np.asarray(distortion['coeffs'], dtype=float)
    pts = np.asarray(camera_points, dtype=float).reshape(-1, 1, 3)
    if distortion['model'] == 'OPENCV_FISHEYE':
        result, _ = cv2.fisheye.projectPoints(pts, np.zeros(3), np.zeros(3), k, coeffs)
    elif distortion['model'] in ('NONE', 'OPENCV_PINHOLE'):
        result, _ = cv2.projectPoints(pts, np.zeros(3), np.zeros(3), k, coeffs if len(coeffs) else None)
    else:
        raise ValueError('지원하지 않는 왜곡 모델')
    return result.reshape(-1, 2)


def overlay(frame: VlmFrame, meta: Mapping[str, Any], state: Mapping[str, Any],
            world_ids: Sequence[str], camera: Mapping[str, Any] | None) -> tuple[VlmFrame, bool]:
    """정확한 보정 및 높이를 모르는 표시는 생략한다. OpenCV 가 없으면 덧그리지 않고 원본을 돌려준다."""
    try:
        cv2 = _cv2()
    except ImportError:
        return frame, False
    image = frame.pixels.copy()
    h, w = image.shape[:2]
    drawn = False
    try:
        if camera is not None and list(camera['image_size']) == [w, h]:
            r = np.asarray(camera['R'], dtype=float).reshape(3, 3)
            t = np.asarray(camera['t'], dtype=float).reshape(3)
            zone = state.get('zone') or {}
            if zone.get('zone_id') == state.get('event_zone_id') and zone.get('height_m') is not None:
                poly = np.asarray(zone.get('polygon_xy'), dtype=float)
                if poly.ndim == 2 and poly.shape[0] >= 3 and poly.shape[1] == 2 and np.isfinite(poly).all():
                    xyz = np.column_stack((poly, np.full(len(poly), float(zone['height_m']))))
                    cam = clip_polygon(xyz @ r.T + t, 2, NEAR, True)
                    if len(cam) >= 3:
                        uv = project(camera, cam)
                        if np.isfinite(uv).all():
                            for axis, bound, greater in ((0, 0, True), (0, w-1, False), (1, 0, True), (1, h-1, False)):
                                uv = clip_polygon(uv, axis, bound, greater)
                            if len(uv) >= 3:
                                p = np.rint(uv).astype(np.int32)
                                fill = image.copy()
                                cv2.fillPoly(fill, [p], (0, 0, 255))
                                image = cv2.addWeighted(fill, 0.3, image, 0.7, 0)
                                cv2.polylines(image, [p], True, (0, 0, 255), 2)
                                drawn = True
            for track in state.get('tracks', []):
                if not isinstance(track, Mapping) or track.get('world_id') not in world_ids:
                    continue
                point = track.get('ground_anchor_xyz')
                if point is None:
                    continue
                cam = r @ np.asarray(point, dtype=float).reshape(3) + t
                if np.isfinite(cam).all() and cam[2] >= NEAR:
                    uv = project(camera, cam.reshape(1, 3))[0]
                    if np.isfinite(uv).all() and 0 <= uv[0] < w and 0 <= uv[1] < h:
                        x, y = np.rint(uv).astype(int)
                        cv2.circle(image, (x, y), 6, (0, 255, 255), 2)
                        cv2.putText(image, 'target', (x, max(12, y-8)), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 255, 255), 1)
                        drawn = True
        # 프레임에 대상자와 직접 결속된 상자가 있을 때만 사용한다.
        box = meta.get('target_bbox')
        if box is not None:
            x1, y1, x2, y2 = np.asarray(box, dtype=float).reshape(4)
            if np.isfinite([x1, y1, x2, y2]).all() and x1 < x2 and y1 < y2 and x2 >= 0 and y2 >= 0 and x1 < w and y1 < h:
                a = (int(max(0, x1)), int(max(0, y1)))
                b = (int(min(w-1, x2)), int(min(h-1, y2)))
                cv2.rectangle(image, a, b, (0, 255, 255), 2)
                cv2.putText(image, 'target', (a[0], max(12, a[1]-4)), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 255, 255), 1)
                drawn = True
    except (ValueError, TypeError, KeyError, cv2.error, OverflowError):
        return frame, False
    if drawn:
        cv2.putText(image, 'red: danger zone  yellow: target', (5, max(12, h-6)),
                    cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1)
        return frame.with_overlay(image), True
    return frame, False
