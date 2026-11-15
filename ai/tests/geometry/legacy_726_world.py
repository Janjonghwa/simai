# S15P31A101-726 이전(dev bd706a9f) World Track 갱신의 바꾼 함수 고정 사본. 동등성 비교(test_tier0_hotpath_equivalence.py)·벤치 전용이며 고치지 않는다.
# - `world_track._others`: 클러스터마다 frame 전체 view 를 다시 모은 경쟁 후보 수집 (지금은 `world_track._OthersIndex`)
# - `joint_gate._has_epipolar_competitor`·`epipolar_distances_normalized`: 카메라 쌍 행렬을 관절·방향마다 다시 만든 경쟁 후보 검사
# `legacy_world()` 안에서는 지금 모듈이 이 사본을 부른다(나머지 World Track·관절 게이트 코드는 그대로).
# ruff: noqa
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import numpy as np

from simai_ai.geometry import joint_gate as joint_gate_module
from simai_ai.geometry.rig import RigCamera, UndistortedKeypoints
from simai_ai.geometry.triangulation import solvable_normalized_many
from simai_ai.tracking import world_track as world_track_module


def _others(frame, cluster) -> tuple[dict[str, list[Sequence[Any]]], dict[str, list[UndistortedKeypoints]]]:
    """같은 시각 다른 클러스터의 카메라별 keypoint 와 그 왜곡 제거 결과 (관절 유효성 게이트의 2대 관측 경쟁 후보 검사용).

    왜곡 제거 결과는 keypoint 와 같은 카메라·같은 순서다. 한 카메라에서 하나라도 없으면 그 카메라는 넣지 않는다(게이트가
    순서가 어긋난 결과를 쓰지 않도록. 넣지 않은 카메라는 게이트가 다시 푼다).
    """
    others: dict[str, list[Sequence[Any]]] = {}
    undistorted: dict[str, list[UndistortedKeypoints]] = {}
    missing: set[str] = set()
    for c in frame.clusters:
        if c is cluster:
            continue
        for v in c.views:
            others.setdefault(v.camera_id, []).append(v.view.keypoints)
            u = c.undistorted.get(v.camera_id)
            if u is None:
                missing.add(v.camera_id)
            else:
                undistorted.setdefault(v.camera_id, []).append(u)
    return others, {cid: us for cid, us in undistorted.items() if cid not in missing}


def epipolar_distances_normalized(cam_a: RigCamera, xa: np.ndarray, cam_b: RigCamera, xb: np.ndarray) -> np.ndarray:
    xb = np.asarray(xb, dtype=float).reshape(-1, 2)
    xa_h = np.append(np.asarray(xa, dtype=float).reshape(2), 1.0)
    if not np.all(np.isfinite(xa_h)):
        return np.full(len(xb), math.nan)
    R = cam_b.R @ cam_a.R.T
    t = cam_b.t - R @ cam_a.t
    tx = np.array([[0.0, -t[2], t[1]], [t[2], 0.0, -t[0]], [-t[1], t[0], 0.0]])
    E = tx @ R
    line_n = E @ xa_h  # cam_b 정규화 좌표의 직선
    line_px = np.linalg.inv(cam_b.K).T @ line_n  # 이상적 핀홀 픽셀 좌표의 직선
    norm = math.hypot(line_px[0], line_px[1])
    out = np.full(len(xb), math.nan)
    finite = np.isfinite(xb).all(axis=1)
    if finite.any():
        px = cam_b.ideal_pixels(xb[finite])
        if norm == 0:
            out[finite] = math.inf
        else:
            out[finite] = np.abs(px[:, 0] * line_px[0] + px[:, 1] * line_px[1] + line_px[2]) / norm
    return out


def _has_epipolar_competitor(
    j: int,
    cams: Sequence[RigCamera],
    pixels: Mapping[str, Sequence[Any]],
    others: Mapping[str, Sequence[Sequence[Any]]],
    cfg: Any,
    undistorted: Mapping[str, UndistortedKeypoints],
    others_undistorted: Mapping[str, Sequence[UndistortedKeypoints]] | None = None,
    lines: Any = None,  # 지금 호출부가 넘기는 저장소. 이전 구현은 쓰지 않는다
) -> bool | None:
    min_conf = cfg.triangulation.min_keypoint_confidence
    unknown = False
    for own, other in ((cams[0], cams[1]), (cams[1], cams[0])):
        if pixels[own.camera_id][j] is None:
            continue
        known = others_undistorted.get(other.camera_id, ()) if others_undistorted is not None else ()
        rows: list[np.ndarray | None] = []
        pending: list[tuple[float, float]] = []
        for n, kps in enumerate(others.get(other.camera_id, ())):
            c = kps[j]
            if c is None:
                continue
            if not all(math.isfinite(float(x)) for x in c[:3]):
                unknown = True
                continue
            if c[2] < min_conf:
                continue
            if n < len(known) and known[n].matches(other, kps):
                rows.append(known[n].normalized[j])
            else:
                rows.append(None)
                pending.append((float(c[0]), float(c[1])))
        if not rows:
            continue
        solved = iter(other.normalize(np.array(pending)) if pending else ())
        xb = np.array([next(solved) if row is None else row for row in rows])
        xa = undistorted[own.camera_id].normalized[j]
        distance = epipolar_distances_normalized(own, xa, other, xb)
        unknown = unknown or bool(np.isnan(distance).any())
        near = np.flatnonzero(distance <= cfg.competitor_epipolar_px)
        if len(near):
            pairs = np.stack([np.broadcast_to(xa, (len(near), 2)), xb[near]], axis=1)
            if solvable_normalized_many([own, other], pairs).any():
                return True
    return None if unknown else False


class _LegacyOthersIndex:
    """`world_track._OthersIndex` 자리에 끼워 이전 `_others` 를 부른다."""

    def __init__(self, frame) -> None:
        self.frame = frame

    def others(self, cluster):
        return _others(self.frame, cluster)


@contextmanager
def legacy_world():
    """이 안에서는 World Track·관절 게이트가 이전 구현(`_others`, 경쟁 후보 검사)을 쓴다."""
    saved = (world_track_module._OthersIndex, joint_gate_module._has_epipolar_competitor)
    world_track_module._OthersIndex = _LegacyOthersIndex
    joint_gate_module._has_epipolar_competitor = _has_epipolar_competitor
    try:
        yield
    finally:
        world_track_module._OthersIndex, joint_gate_module._has_epipolar_competitor = saved
