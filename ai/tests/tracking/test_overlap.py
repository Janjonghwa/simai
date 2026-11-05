"""-499: 무한 시야의 분리만 제외하고 경계/왜곡/보정 교체는 보수적으로 유지한다."""

import dataclasses

import numpy as np

from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.geometry.overlap import disjoint_camera_pairs
from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.tracking.cross_view import CrossViewAssociator, LocalView, RejectReason
from simai_ai.geometry.interpolation import InterpolatedView


def camera(cid, x, facing):
    # World x 방향으로 보는 카메라. 정상 오른손 회전.
    r = np.array([[0., 1., 0.], [0., 0., float(facing)], [float(facing), 0., 0.]])
    t = -r @ np.array([x, 0., 2.])
    pinhole = PinholeCamera(K=((500., 0., 320.), (0., 500., 240.), (0., 0., 1.)),
                            R=tuple(map(tuple, r)), t=tuple(t))
    return RigCamera(cid, pinhole, (640, 480))


def rig(a, b):
    return CameraRig("v1", "SIM", "PROVISIONAL", {a.camera_id: a, b.camera_id: b}, {})


def test_disjoint_outward_cameras_skip_expensive_pair_geometry(monkeypatch):
    cameras = rig(camera("a", -1, -1), camera("b", 1, 1))
    assert disjoint_camera_pairs(cameras) == frozenset({("a", "b")})
    assoc = CrossViewAssociator(cameras)
    monkeypatch.setattr(assoc, "_epipolar_matrix", lambda *a: (_ for _ in ()).throw(AssertionError("분리 쌍 계산")))
    views = [LocalView(c, "p", InterpolatedView(c, 1000, tuple((320., 240., .9) for _ in range(17)),
                                               "SIM", 0., (1000,))) for c in ("a", "b")]
    result = assoc.associate(1000, views)
    assert result.pairs[0].reject == RejectReason.NO_FOV_OVERLAP
    assert all(len(c.views) == 1 for c in result.clusters)


def test_overlap_or_touching_or_unknown_distortion_are_never_skipped():
    assert not disjoint_camera_pairs(rig(camera("a", -1, 1), camera("b", 1, -1)))
    assert not disjoint_camera_pairs(rig(camera("a", 0, 1), camera("b", 0, -1)))
    a = dataclasses.replace(camera("a", -1, -1), distortion_model="OPENCV_PINHOLE",
                            distortion_coeffs=(-.3, .09, 0., 0., 0.))
    assert not disjoint_camera_pairs(rig(a, camera("b", 1, 1)))


def test_replace_calibration_recomputes_skipped_pairs_and_retains_new_overlap():
    assoc = CrossViewAssociator(rig(camera("a", -1, -1), camera("b", 1, 1)))
    assert assoc.skipped_camera_pairs
    assoc.replace_rig(rig(camera("a", -1, 1), camera("b", 1, -1)))
    assert not assoc.skipped_camera_pairs
