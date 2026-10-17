"""번들 조정 검산 도구: 바닥 커버리지, 접지점 분포, CCVS 방식 카메라 높이, 카메라별 바닥 평면."""

from __future__ import annotations

import numpy as np
import pytest

from simai_ai.calibration.bundle import checks
from simai_ai.geometry import distortion
from simai_ai.geometry.rig import RigCamera

ROOM = ((0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0))


def _rig(camera_id, center, target=(2.0, 1.5, 0.8), f=500.0, size=(1000, 600)):
    center = np.asarray(center, float)
    forward = np.asarray(target, float) - center
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    R = np.vstack([right, np.cross(forward, right), forward])
    return RigCamera.from_calibration(
        {
            "camera_id": camera_id,
            "image_size": list(size),
            "K": [[f, 0.0, size[0] / 2], [0.0, f, size[1] / 2], [0.0, 0.0, 1.0]],
            "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [-0.05, 0.01, 0.0, 0.0, 0.0]},
            "R": R.tolist(),
            "t": (-R @ center).tolist(),
        }
    )


def _pixel(rig, X):
    Xc = rig.to_camera(np.asarray(X, float))[0]
    xd = distortion.distort(Xc[:2] / Xc[2], rig.distortion_model, rig.distortion_coeffs)[0]
    return tuple(rig.K[:2, :2] @ xd + rig.K[:2, 2])


def test_floor_coverage_counts_cells_with_contacts():
    contacts = np.array([[0.25, 0.25], [0.3, 0.2], [3.75, 2.75]])
    ratio = checks.floor_coverage(ROOM, contacts, cell_m=0.5)
    assert ratio == pytest.approx(2 / 48)
    assert checks.floor_coverage((), contacts, 0.5) is None  # 걸을 수 있는 바닥을 모르면 비운다


def test_contact_minor_spread_detects_straight_line():
    line = np.column_stack([np.linspace(0, 5, 50), np.full(50, 2.0)])
    loop = np.column_stack([np.linspace(0, 5, 50), np.tile([1.0, 3.0], 25)])
    assert checks.contact_minor_spread(line) == pytest.approx(0.0, abs=1e-9)
    assert checks.contact_minor_spread(loop) > 0.9
    assert checks.contact_minor_spread(line[:2]) is None


def test_camera_height_check_recovers_height_from_single_view():
    rig = _rig("cam1", (0.2, 0.2, 2.6))
    pairs = []
    for x, y in [(1.5, 1.0), (2.5, 2.0), (3.0, 1.2), (1.2, 2.4)]:
        bottom = (x, y, 0.08)
        top = (x, y, 1.45)
        pairs.append((_pixel(rig, top), _pixel(rig, bottom), 1.37, 0.08))
    hc = checks.camera_height_check(rig, pairs)
    assert hc.sample_count == 4
    assert hc.single_view_height_m == pytest.approx(2.6, abs=1e-6)
    assert hc.difference_m == pytest.approx(0.0, abs=1e-6)


def test_floor_planes_agree_for_consistent_cameras_and_detect_tilted_camera():
    rigs = {
        "a": _rig("a", (0.2, 0.2, 2.6)),
        "b": _rig("b", (3.8, 0.2, 2.5)),
        "c": _rig("c", (3.8, 2.8, 2.7)),
    }
    rng = np.random.default_rng(0)
    pts = np.column_stack([rng.uniform(0.5, 3.5, 30), rng.uniform(0.5, 2.5, 30), np.full(30, 0.02)])
    contacts = [({cid: _pixel(r, X) for cid, r in rigs.items()}, 0.02, (X[0], X[1])) for X in pts]
    planes = checks.floor_planes(rigs, contacts, min_points=10)
    assert planes.disagreement_m == pytest.approx(0.0, abs=1e-6)
    # 카메라 c 의 관측만 아래로 3px 밀리면(기울어진 카메라) 그 카메라가 낀 평면이 어긋난다
    shifted = [
        ({cid: (uv[0], uv[1] + (3.0 if cid == "c" else 0.0)) for cid, uv in px.items()}, h, xy)
        for px, h, xy in contacts
    ]
    assert checks.floor_planes(rigs, shifted, min_points=10).disagreement_m > 0.005
    # 평면을 맞출 접지점이 부족한 카메라가 있으면 차이를 비운다
    assert checks.floor_planes(rigs, contacts[:4], min_points=10).disagreement_m is None
