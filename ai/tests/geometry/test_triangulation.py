"""보정 카메라 묶음과 다시점 삼각측량 시험 (명세 5.2).

합성 카메라 4대(교실 모서리, 높이 2.7 m)로 알려진 3D 점을 투영해 되돌아오는지 본다.
"""

import json
import math

import numpy as np
import pytest

from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.geometry.triangulation import (
    JointIssue,
    JointValidity,
    TriangulationConfig,
    max_ray_angle,
    reprojection_errors,
    triangulate_point,
    triangulate_skeleton,
)
from simai_ai.providers.base import CalibrationProvider
from simai_ai.providers.contract import check_world_track_semantics
from simai_ai.providers.testing import load_example

T = 1_760_342_400_000
SIZE = (2304, 1296)


def look_at(position, target):
    """sim/camera.py 와 같은 규칙: z 앞, x = z × up, y = z × x (OpenCV)."""
    z = np.subtract(target, position)
    z = z / np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x = x / np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.vstack([x, y, z])
    return R, -R @ np.asarray(position, dtype=float)


def camera_doc(camera_id, position, distortion=None):
    R, t = look_at(position, (4.5, 3.75, 0.9))
    f = (SIZE[0] / 2) / math.tan(math.radians(105) / 2)
    return {
        "camera_id": camera_id,
        "image_size": list(SIZE),
        "K": [[f, 0.0, SIZE[0] / 2], [0.0, f, SIZE[1] / 2], [0.0, 0.0, 1.0]],
        "distortion": distortion or {"model": "NONE", "coeffs": []},
        "R": R.tolist(),
        "t": t.tolist(),
    }


CORNERS = {"cam1": (0.3, 0.3, 2.7), "cam2": (8.7, 0.3, 2.7), "cam3": (8.7, 7.2, 2.7), "cam4": (0.3, 7.2, 2.7)}


def calibration_doc(distortion=None, state="PROVISIONAL"):
    return {
        "schema_version": "1.1.0",
        "calibration_version": "test-rig-1",
        "provider_source": "SIM",
        "state": state,
        "created_at": T,
        "coordinate_frame": {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"},
        "scale_basis": "DATASET_GT",
        "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
        "cameras": [camera_doc(cid, pos, distortion) for cid, pos in CORNERS.items()],
    }


class DictCalibrationProvider(CalibrationProvider):
    def __init__(self, doc):
        self._doc = doc

    def current(self):
        return json.loads(json.dumps(self._doc))


def rig_of(**kwargs):
    return CameraRig.from_provider(DictCalibrationProvider(calibration_doc(**kwargs)))


def distort(cam: RigCamera, xn):
    """OpenCV 왜곡 정방향 (sim/camera.py _distort 와 같은 식)."""
    x, y = xn
    k = cam.distortion_coeffs
    if cam.distortion_model == "OPENCV_PINHOLE":
        k1, k2, p1, p2, k3 = (list(k) + [0.0])[:5]
        r2 = x * x + y * y
        radial = 1 + k1 * r2 + k2 * r2**2 + k3 * r2**3
        return (x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x), y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y)
    if cam.distortion_model == "OPENCV_FISHEYE":
        r = math.hypot(x, y)
        th = math.atan(r)
        th_d = th * (1 + k[0] * th**2 + k[1] * th**4 + k[2] * th**6 + k[3] * th**8)
        return (x * th_d / r, y * th_d / r)
    return (x, y)


def project(cam: RigCamera, X):
    xc = cam.to_camera(X)[0]
    xd = distort(cam, xc[:2] / xc[2])
    return cam.K[:2, :2] @ np.array(xd) + cam.K[:2, 2]


def view(cam_id, keypoints, t=T):
    return InterpolatedView(cam_id, t, tuple(keypoints), "SIM", 0.0, (t,))


POINTS = [(4.5, 3.75, 1.0), (2.0, 2.0, 0.08), (7.0, 5.5, 1.6), (1.0, 6.5, 0.5)]


def test_rig_from_provider_carries_version_and_provenance():
    rig = rig_of()
    assert rig.calibration_version == "test-rig-1"
    assert rig.calibration_provenance == {"source": "SIM", "calibration_version": "test-rig-1"}
    assert set(rig.cameras) == set(CORNERS)
    np.testing.assert_allclose(rig.cameras["cam1"].center, CORNERS["cam1"], atol=1e-9)
    with pytest.raises(KeyError):
        rig.subset(["cam9"])


def test_rig_from_contract_example():
    rig = CameraRig.from_provider(DictCalibrationProvider(load_example("calibration.json")))
    assert rig.cameras  # 계약 예시(반올림된 R, 왜곡 계수 포함)를 그대로 받는다


@pytest.mark.parametrize(
    "frame",
    [
        {"frame": "WORLD", "unit": "cm", "handedness": "right", "up_axis": "+Z"},
        {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Y"},
    ],
)
def test_rig_rejects_non_world_frame(frame):
    doc = calibration_doc()
    doc["coordinate_frame"] = frame
    with pytest.raises(ValueError):
        CameraRig.from_provider(DictCalibrationProvider(doc))


@pytest.mark.parametrize(
    "distortion",
    [
        {"model": "OPENCV_PINHOLE", "coeffs": [-0.28, 0.09, 0.001, -0.0005, -0.012]},
        {"model": "OPENCV_PINHOLE", "coeffs": [-0.28, 0.09, 0.001, -0.0005]},
        {"model": "OPENCV_FISHEYE", "coeffs": [-0.02, 0.003, -0.001, 0.0002]},
    ],
)
def test_undistortion_inverts_opencv_model(distortion):
    rig = rig_of(distortion=distortion)
    for cam in rig.cameras.values():
        for X in POINTS:
            xc = cam.to_camera(X)[0]
            expected = xc[:2] / xc[2]
            np.testing.assert_allclose(cam.normalize(project(cam, X))[0], expected, atol=1e-7)


@pytest.mark.parametrize("bad", [{"model": "NONE", "coeffs": [0.1]}, {"model": "OPENCV_FISHEYE", "coeffs": [0.1]}])
def test_rig_rejects_inconsistent_distortion(bad):
    with pytest.raises(ValueError):
        rig_of(distortion=bad)


@pytest.mark.parametrize("distortion", [None, {"model": "OPENCV_PINHOLE", "coeffs": [-0.28, 0.09, 0.001, -0.0005, -0.01]}])
def test_triangulate_point_recovers_exact_point(distortion):
    rig = rig_of(distortion=distortion)
    cams = list(rig.cameras.values())
    for X in POINTS:
        px = np.array([project(c, X) for c in cams])
        est = triangulate_point(cams, px)
        np.testing.assert_allclose(est, X, atol=1e-6)
        assert reprojection_errors(cams, px, est).max() < 1e-4
        # 두 대만으로도 된다
        np.testing.assert_allclose(triangulate_point(cams[:2], px[:2]), X, atol=1e-6)


def test_refinement_reduces_reprojection_error_under_noise():
    rng = np.random.default_rng(7)
    rig = rig_of()
    cams = list(rig.cameras.values())
    X = np.array(POINTS[0])
    px = np.array([project(c, X) for c in cams]) + rng.normal(0, 2.0, (4, 2))
    linear = triangulate_point(cams, px, refine_iterations=0)
    refined = triangulate_point(cams, px)
    assert reprojection_errors(cams, px, refined).sum() <= reprojection_errors(cams, px, linear).sum() + 1e-9
    assert np.linalg.norm(refined - X) < 0.02


def test_triangulate_point_needs_two_cameras():
    rig = rig_of()
    with pytest.raises(ValueError):
        triangulate_point([rig.cameras["cam1"]], np.zeros((1, 2)))


def skeleton_views(rig, X_joints, drop=None, noise=None, conf=0.9):
    """관절 배열 X_joints(17, 3)를 카메라마다 투영한 view. drop={camera_id: [관절 번호...]} 는 None 으로 비운다."""
    views = []
    for cid, cam in rig.cameras.items():
        kps = []
        for j, X in enumerate(X_joints):
            if drop and j in drop.get(cid, ()):
                kps.append(None)
                continue
            uv = project(cam, X) + (noise.get((cid, j), 0.0) if noise else 0.0)
            kps.append((float(uv[0]), float(uv[1]), conf))
        views.append(view(cid, kps))
    return views


def standing_joints(x=4.5, y=3.75):
    heights = [1.6, 1.62, 1.62, 1.6, 1.6, 1.42, 1.42, 1.15, 1.15, 0.9, 0.9, 0.92, 0.92, 0.5, 0.5, 0.08, 0.08]
    side = [0, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1]
    return np.array([(x + 0.03 * s, y + 0.18 * s, z) for s, z in zip(side, heights)])


def test_skeleton_all_cameras_valid():
    rig = rig_of()
    X = standing_joints()
    sk = triangulate_skeleton(skeleton_views(rig, X), rig)
    assert sk.t == T
    assert sk.calibration_version == "test-rig-1"
    assert sk.camera_ids == tuple(sorted(CORNERS))
    for est, truth in zip(sk.joints, X):
        assert est.validity == JointValidity.VALID
        assert est.visible
        np.testing.assert_allclose(est.xyz, truth, atol=1e-6)
        assert len(est.camera_ids) == 4
    np.testing.assert_allclose(sk.pelvis_xyz(), (X[11] + X[12]) / 2, atol=1e-6)
    assert sk.pelvis_validity() == JointValidity.VALID


def test_one_view_joint_is_null_not_invented():
    rig = rig_of()
    X = standing_joints()
    # 왼발목(15)은 cam1 에서만, 오른 엉덩이(12)는 어디에서도 보이지 않는다
    drop = {"cam2": [15, 12], "cam3": [15, 12], "cam4": [15, 12], "cam1": [12]}
    sk = triangulate_skeleton(skeleton_views(rig, X, drop=drop), rig)
    for j in (12, 15):
        est = sk.joints[j]
        assert est.xyz is None
        assert not est.visible
        assert est.validity == JointValidity.UNAVAILABLE
        assert est.issues == (JointIssue.TOO_FEW_VIEWS,)
    assert sk.pelvis_xyz() is None  # 골반은 양쪽 엉덩이가 있어야 채운다
    assert sk.pelvis_validity() == JointValidity.UNAVAILABLE
    doc = sk.to_skeleton()
    assert doc["joints"][15] is None and doc["visible"][15] is False and doc["validity"][15] == "UNAVAILABLE"
    assert doc["joint_source"] == ["observed"] * 17
    track = {"world_id": "w-1", "cameras": list(sk.camera_ids), "pelvis_xyz": None, "skeleton": doc}
    assert check_world_track_semantics({"t": T, "tracks": [track]}) == []


def test_low_confidence_does_not_count_as_view():
    rig = rig_of()
    X = standing_joints()
    views = skeleton_views(rig, X)
    # cam2~4 의 코(0) 신뢰도를 0 으로 (가림)
    views = [
        v if v.camera_id == "cam1" else view(v.camera_id, [(*v.keypoints[0][:2], 0.0), *v.keypoints[1:]])
        for v in views
    ]
    sk = triangulate_skeleton(views, rig)
    assert sk.joints[0].xyz is None
    assert sk.joints[0].validity == JointValidity.UNAVAILABLE


def test_outlier_camera_is_rejected_with_three_or_more_views():
    rig = rig_of()
    X = standing_joints()
    sk = triangulate_skeleton(skeleton_views(rig, X, noise={("cam3", 9): np.array([80.0, -60.0])}), rig)
    wrist = sk.joints[9]
    assert wrist.rejected_camera_ids == ("cam3",)
    assert "cam3" not in wrist.camera_ids
    assert wrist.validity == JointValidity.VALID
    np.testing.assert_allclose(wrist.xyz, X[9], atol=1e-6)


def test_two_view_inconsistency_is_uncertain_not_dropped():
    rig = rig_of()
    X = standing_joints()
    drop = {"cam3": [9], "cam4": [9]}
    sk = triangulate_skeleton(skeleton_views(rig, X, drop=drop, noise={("cam2", 9): np.array([0.0, 40.0])}), rig)
    wrist = sk.joints[9]
    assert wrist.xyz is not None
    # VALID 상한(8 px)과 이상치 기준(20 px) 사이: 좌표는 두고 UNCERTAIN
    assert 8.0 < wrist.reprojection_error_px <= 20.0
    assert wrist.validity == JointValidity.UNCERTAIN
    assert JointIssue.HIGH_REPROJECTION_ERROR in wrist.issues


def test_outlier_removal_without_consistent_pair_is_null():
    """3대 → 이상치 1대 제거 → 남은 2대도 이상치 기준을 넘으면 좌표를 비운다 (2대를 채우려고 이상치를 세지 않는다)."""
    rig = rig_of()
    X = standing_joints()
    noise = {("cam2", 9): np.array([0.0, 100.0]), ("cam3", 9): np.array([400.0, -400.0])}
    views = [v for v in skeleton_views(rig, X, noise=noise) if v.camera_id != "cam4"]
    sk = triangulate_skeleton(views, rig)
    wrist = sk.joints[9]
    assert wrist.xyz is None
    assert not wrist.visible
    assert wrist.validity == JointValidity.UNAVAILABLE
    assert wrist.camera_ids == ()
    assert wrist.rejected_camera_ids[0] == "cam3"  # 먼저 뺀 이상치 이력이 남는다
    assert set(wrist.rejected_camera_ids) == {"cam1", "cam2", "cam3"}
    assert JointIssue.INCONSISTENT_VIEWS in wrist.issues
    doc = sk.to_skeleton()
    assert doc["joints"][9] is None and doc["visible"][9] is False and doc["validity"][9] == "UNAVAILABLE"
    assert sk.joints[10].validity == JointValidity.VALID  # 다른 관절은 그대로


def test_two_views_beyond_outlier_threshold_are_null():
    rig = rig_of()
    X = standing_joints()
    drop = {"cam3": [9], "cam4": [9]}
    sk = triangulate_skeleton(skeleton_views(rig, X, drop=drop, noise={("cam2", 9): np.array([0.0, 100.0])}), rig)
    wrist = sk.joints[9]
    assert wrist.xyz is None
    assert wrist.validity == JointValidity.UNAVAILABLE
    assert set(wrist.rejected_camera_ids) == {"cam1", "cam2"}
    assert wrist.issues == (JointIssue.INCONSISTENT_VIEWS,)


def line_rig(placements, f=800.0, size=(640, 480)):
    """(카메라 중심, 바라보는 점) 목록으로 만든 보정 묶음. 시선 배치 퇴화 시험용."""
    K = ((f, 0.0, size[0] / 2), (0.0, f, size[1] / 2), (0.0, 0.0, 1.0))
    cams = {}
    for i, (center, target) in enumerate(placements, start=1):
        R, t = look_at(center, target)
        cams[f"line{i}"] = RigCamera(f"line{i}", PinholeCamera(K, R, t), size)
    return CameraRig("line-rig", "SIM", "PROVISIONAL", cams, {"source": "SIM", "calibration_version": "line-rig"})


# 서로 마주 보는 두 대(시선이 반대 방향)와 한 줄로 같은 방향을 보는 두 대
FACING = [((-1.0, 0.0, 1.0), (1.0, 0.0, 1.0)), ((1.0, 0.0, 1.0), (-1.0, 0.0, 1.0))]
SAME_DIRECTION = [((-2.0, 0.0, 1.0), (5.0, 0.0, 1.0)), ((-1.0, 0.0, 1.0), (5.0, 0.0, 1.0))]


def single_point_views(rig, X, offsets=None):
    offsets = offsets or {}
    out = []
    for cid, cam in rig.cameras.items():
        uv = project(cam, X) + offsets.get(cid, 0.0)
        out.append(view(cid, [(float(uv[0]), float(uv[1]), 0.9)] * 17))
    return out


def test_ray_angle_is_angle_between_lines():
    """시선은 직선이다: 반대 방향(180° 근처)도 같은 방향(0° 근처)처럼 좁은 각이다."""
    rig = line_rig(FACING)
    cams = list(rig.cameras.values())
    # 반대 방향 시선(179.85°)은 직선 사이 각 0.15° 다
    near = math.atan(0.001 / 1.5) + math.atan(0.001 / 0.5)
    assert max_ray_angle(cams, np.array([0.5, 0.001, 1.0])) == pytest.approx(near)
    # 둔각으로 만나는 두 시선(146.6°)은 직선 사이 각 33.4° 다
    assert max_ray_angle(cams, np.array([0.0, 0.3, 1.0])) == pytest.approx(math.acos(0.91 / 1.09))
    assert max_ray_angle(cams, np.array([0.0, 0.3, 1.0])) <= math.pi / 2


@pytest.mark.parametrize("placements", [FACING, SAME_DIRECTION], ids=["opposite", "same"])
def test_near_collinear_rays_are_not_valid(placements):
    """준공선 시선: 1 px 차이가 수십 cm 오차가 되므로 VALID 로 확정하지 않는다 (재투영 오차가 0 이어도)."""
    rig = line_rig(placements)
    X = np.array([0.5, 0.001, 1.0])
    est = triangulate_skeleton(single_point_views(rig, X, {"line2": np.array([-1.0, 0.0])}), rig).joints[0]
    assert est.validity != JointValidity.VALID
    if est.xyz is not None:
        assert JointIssue.NARROW_RAY_ANGLE in est.issues
        assert est.ray_angle_rad < math.radians(5)


# 축과 비스듬한 직선 위의 공선 배치: (카메라 배치, 그 직선 위의 점)
COLLINEAR_CASES = {
    "opposite": (FACING, (0.5, 0.0, 1.0)),
    "same": (SAME_DIRECTION, (0.5, 0.0, 1.0)),
    "oblique-opposite": ([((-1.0, -1.0, 0.8), (1.0, 1.0, 1.2)), ((1.0, 1.0, 1.2), (-1.0, -1.0, 0.8))], (0.25, 0.25, 1.05)),
    "oblique-same": ([((-2.0, -1.5, 0.7), (4.0, 3.0, 1.6)), ((-1.0, -0.75, 0.85), (4.0, 3.0, 1.6))], (1.0, 0.75, 1.15)),
}


@pytest.mark.parametrize("case", list(COLLINEAR_CASES))
def test_exactly_collinear_rays_have_no_solution(case):
    """두 시선이 같은 직선이면 해가 유일하지 않다: 좌표를 비운다."""
    placements, X = COLLINEAR_CASES[case]
    rig = line_rig(placements)
    X = np.array(X)
    cams = list(rig.cameras.values())
    assert triangulate_point(cams, np.array([project(c, X) for c in cams])) is None
    est = triangulate_skeleton(single_point_views(rig, X), rig).joints[0]
    assert est.xyz is None
    assert not est.visible
    assert est.validity == JointValidity.UNAVAILABLE
    assert est.issues == (JointIssue.DEGENERATE,)


def test_crossing_rays_are_valid():
    rig = line_rig(FACING)
    X = np.array([0.0, 0.3, 1.0])
    est = triangulate_skeleton(single_point_views(rig, X), rig).joints[0]
    assert est.validity == JointValidity.VALID
    np.testing.assert_allclose(est.xyz, X, atol=1e-6)


def test_narrow_ray_angle_is_uncertain():
    rig = rig_of()
    cams = [rig.cameras["cam1"], rig.cameras["cam2"]]
    X = np.array([4.5, 150.0, 1.0])  # 두 카메라 앞, 기선(8.4 m)에 비해 아주 먼 점
    assert max_ray_angle(cams, X) < math.radians(5)
    sk = triangulate_skeleton([view(c.camera_id, [(*project(c, X), 0.9)] * 17) for c in cams], rig)
    est = sk.joints[0]
    assert est.xyz is not None
    assert est.validity == JointValidity.UNCERTAIN
    assert JointIssue.NARROW_RAY_ANGLE in est.issues


@pytest.mark.parametrize("state", ["UNCALIBRATED", "DRIFTED"])
def test_untrusted_calibration_caps_at_uncertain(state):
    rig = rig_of(state=state)
    sk = triangulate_skeleton(skeleton_views(rig, standing_joints()), rig)
    assert {j.validity for j in sk.joints} == {JointValidity.UNCERTAIN}
    assert all(JointIssue.UNTRUSTED_CALIBRATION in j.issues for j in sk.joints)


def test_input_validation():
    rig = rig_of()
    views = skeleton_views(rig, standing_joints())
    with pytest.raises(ValueError, match="같은 카메라"):
        triangulate_skeleton([views[0], views[0]], rig)
    with pytest.raises(ValueError, match="공통 시각"):
        triangulate_skeleton([views[0], view("cam2", views[1].keypoints, t=T + 1)], rig)
    with pytest.raises(ValueError):
        triangulate_skeleton([view("cam1", views[0].keypoints[:5])], rig)
    with pytest.raises(KeyError):
        triangulate_skeleton([view("cam9", views[0].keypoints)], rig)
    with pytest.raises(ValueError):
        TriangulationConfig(min_views=1)
