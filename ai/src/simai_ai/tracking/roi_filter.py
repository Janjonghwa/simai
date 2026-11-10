"""교차 시점 연결 전 임시 바닥 가설로 관제 영역 밖 관측을 제외한다."""

from __future__ import annotations

import math

from simai_ai.risk.zone_geometry import distance_outside_polygon


def zone_envelope(polygons):
    """활성 구역 전체를 감싸는 축 정렬 사각형 하나. 구역 하나(예: 개구부)만 켜져 있어도
    구역 사이·주변으로 다가오는 사람을 지우지 않게, 구역별이 아니라 묶음 전체로 거른다."""
    points = [p for polygon in polygons for p in polygon]
    if not points:
        return ()
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return (((x0, y0), (x1, y0), (x1, y1), (x0, y1)),)


def filter_views(views, rig, polygons, margin_m):
    """영역이나 투영을 모르면 통과한다. 반환 위치는 World Track에 쓰지 않는다."""
    if not polygons:
        return list(views), {}
    import numpy as np

    kept, counts = [], {}
    for view in views:
        camera = rig.cameras.get(view.camera_id)
        keypoints = view.view.keypoints
        ankles = [keypoints[i] for i in (15, 16) if i < len(keypoints)
                  and keypoints[i] is not None and keypoints[i][2] > 0]
        if ankles:
            uv = (sum(p[0] for p in ankles) / len(ankles), sum(p[1] for p in ankles) / len(ankles))
        elif view.view.bbox is not None:
            x1, _, x2, y2 = view.view.bbox
            uv = ((x1 + x2) / 2, y2)
        else:
            kept.append(view)
            continue
        if camera is None:
            kept.append(view)
            continue
        normalized, valid = camera.normalize_with_mask(np.asarray([uv], dtype=float))
        if not valid[0]:
            kept.append(view)
            continue
        ray = camera.R.T @ np.array([*normalized[0], 1.0])
        if not np.all(np.isfinite(ray)) or ray[2] >= -1e-9 or camera.center[2] <= 0:
            kept.append(view)
            continue
        xy = camera.center[:2] - camera.center[2] / ray[2] * ray[:2]
        if not all(math.isfinite(float(x)) for x in xy):
            kept.append(view)
        elif any(distance_outside_polygon(tuple(xy), polygon) <= margin_m for polygon in polygons):
            kept.append(view)
        else:
            counts[view.camera_id] = counts.get(view.camera_id, 0) + 1
    return kept, counts
