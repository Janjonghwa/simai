"""3D 관절 유효성 게이트 시험 (명세 5.2, 부록 D D-G2-06).

합성 카메라 4대(교실 모서리, 높이 2.7 m)로 알려진 골격을 투영해 게이트 판정을 본다. 판정 시험마다 음성 대조
(임계값을 느슨하게 하면 같은 단언이 실패하는지)를 일부 둔다.
"""

import math
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.joint_gate import (
    COCO17_BONES,
    GateIssue,
    JointGate,
    JointGateConfig,
    epipolar_distance_px,
    triangulation_covariance,
)
from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.geometry.triangulation import JointIssue, JointValidity
from simai_ai.providers.contract import check_world_track_semantics, schema_errors

T = 1_760_342_400_000
SIZE = (2304, 1296)
CORNERS = {"cam1": (0.3, 0.3, 2.7), "cam2": (8.7, 0.3, 2.7), "cam3": (8.7, 7.2, 2.7), "cam4": (0.3, 7.2, 2.7)}
SKELETON_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/world_state.schema.json#/$defs/skeleton"}
L_WRIST, L_ELBOW, L_EAR, NOSE = 9, 7, 3, 0
NO_OTHERS: dict = {}  # 같은 시각 다른 사람 검출이 없음을 명시 (None 은 '모름')


def look_at(position, target):
    """sim/camera.py 와 같은 규칙: z 앞, x = z × up, y = z × x (OpenCV)."""
    z = np.subtract(target, position)
    z = z / np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x = x / np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.vstack([x, y, z])
    return R, -R @ np.asarray(position, dtype=float)


def make_rig(placements, state="PROVISIONAL", f=None, size=SIZE):
    """{camera_id: (중심, 바라보는 점)} 으로 만든 왜곡 없는 보정 묶음."""
    f = f or (size[0] / 2) / math.tan(math.radians(105) / 2)
    K = ((f, 0.0, size[0] / 2), (0.0, f, size[1] / 2), (0.0, 0.0, 1.0))
    cams = {}
    for cid, (center, target) in placements.items():
        R, t = look_at(center, target)
        cams[cid] = RigCamera(cid, PinholeCamera(K, R, t), size)
    return CameraRig("gate-rig-1", "SIM", state, cams, {"source": "SIM", "calibration_version": "gate-rig-1"})


def corner_rig(state="PROVISIONAL"):
    return make_rig({cid: (pos, (4.5, 3.75, 0.9)) for cid, pos in CORNERS.items()}, state)


def project(cam: RigCamera, X):
    xc = cam.to_camera(X)[0]
    return cam.K[:2, :2] @ (xc[:2] / xc[2]) + cam.K[:2, 2]


def standing_joints(x=4.5, y=3.75):
    heights = [1.6, 1.62, 1.62, 1.6, 1.6, 1.42, 1.42, 1.15, 1.15, 0.9, 0.9, 0.92, 0.92, 0.5, 0.5, 0.08, 0.08]
    side = [0, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1]
    return np.array([(x + 0.03 * s, y + 0.18 * s, z) for s, z in zip(side, heights)])


def views_of(rig, X_joints, t=T, cameras=None, drop=None, noise=None, conf=0.9):
    """골격 X_joints(17, 3)의 카메라별 투영. drop={camera_id: [관절...]} 는 None, noise={(camera_id, 관절): (du, dv)}."""
    out = []
    for cid, cam in rig.cameras.items():
        if cameras is not None and cid not in cameras:
            continue
        kps = []
        for j, X in enumerate(X_joints):
            if drop and j in drop.get(cid, ()):
                kps.append(None)
                continue
            uv = project(cam, X) + np.asarray(noise.get((cid, j), (0.0, 0.0)) if noise else (0.0, 0.0))
            kps.append((float(uv[0]), float(uv[1]), conf))
        out.append(InterpolatedView(cid, t, tuple(kps), "SIM", 0.0, (t,)))
    return out


def warm_up(gate, rig, frames=8, x0=3.0, step=0.05, t0=T, dt=100):
    """천천히 걷는(0.5 m/s) 골격으로 이력(직전 VALID 위치, 뼈 길이 기준)을 쌓는다. 마지막 (t, x) 를 돌려준다."""
    t, x = t0, x0
    for _ in range(frames):
        sk = gate.update(views_of(rig, standing_joints(x=x), t=t), rig, NO_OTHERS)
        assert all(j.validity == JointValidity.VALID for j in sk.joints)
        t, x = t + dt, x + step
    return t - dt, x - step


def is_flagged(gj, issue):
    """판정 단언: 이 관절이 VALID 가 아니고 사유에 issue 가 있다."""
    return gj.validity != JointValidity.VALID and issue in gj.reasons


# --------------------------------------------------------------------------- 정상 3~4대


@pytest.mark.parametrize("cameras", [None, ["cam1", "cam2", "cam3"]], ids=["4-views", "3-views"])
def test_normal_multi_view_all_valid(cameras):
    rig = corner_rig()
    X = standing_joints()
    sk = JointGate().update(views_of(rig, X, cameras=cameras), rig, NO_OTHERS)
    for gj, truth in zip(sk.joints, X):
        assert gj.validity == JointValidity.VALID
        assert gj.reasons == ()
        assert gj.joint_source == "observed"
        np.testing.assert_allclose(gj.xyz, truth, atol=1e-6)
        assert gj.sigma_max_m < 0.02
        assert gj.speed_mps is None  # 첫 관측: 비교할 이력이 없다
    assert not sk.identity_uncertain_candidate
    doc = sk.to_skeleton()
    assert schema_errors(doc, SKELETON_SCHEMA) == []
    assert doc["joint_source"] == ["observed"] * 17
    np.testing.assert_allclose(sk.pelvis_xyz(), (X[11] + X[12]) / 2, atol=1e-6)
    assert sk.pelvis_validity() == JointValidity.VALID


# --------------------------------------------------------------------------- 1대만 관측


def test_one_view_joint_is_null_not_invented():
    rig = corner_rig()
    X = standing_joints()
    # 왼손목은 cam1 에서만, 오른 엉덩이(12)는 어디에서도 보이지 않는다
    drop = {"cam2": [L_WRIST, 12], "cam3": [L_WRIST, 12], "cam4": [L_WRIST, 12], "cam1": [12]}
    sk = JointGate().update(views_of(rig, X, drop=drop), rig, NO_OTHERS)
    for j in (L_WRIST, 12):
        gj = sk.joints[j]
        assert gj.xyz is None and not gj.visible
        assert gj.validity == JointValidity.UNAVAILABLE
        assert gj.reasons == (JointIssue.TOO_FEW_VIEWS,)
        assert gj.sigma_max_m is None
    assert sk.pelvis_xyz() is None
    assert sk.pelvis_validity() == JointValidity.UNAVAILABLE
    doc = sk.to_skeleton()
    assert doc["joints"][L_WRIST] is None and doc["validity"][L_WRIST] == "UNAVAILABLE"
    assert schema_errors(doc, SKELETON_SCHEMA) == []
    track = {"world_id": "w-1", "cameras": list(sk.camera_ids), "pelvis_xyz": None, "skeleton": doc}
    assert check_world_track_semantics({"t": T, "tracks": [track]}) == []


def test_single_camera_person_has_no_joints():
    rig = corner_rig()
    sk = JointGate().update(views_of(rig, standing_joints(), cameras=["cam1"]), rig, NO_OTHERS)
    assert all(gj.xyz is None and gj.validity == JointValidity.UNAVAILABLE for gj in sk.joints)
    assert sk.head_z(0.12).value is None


def test_sync_invalid_cameras_are_excluded():
    """명세 3.9: 동기화 무효 카메라는 삼각측량에서 뺀다. 남은 카메라가 2대 미만인 관절은 비운다."""
    rig = corner_rig()
    X = standing_joints()
    drop = {"cam3": [L_WRIST], "cam4": [L_WRIST]}  # 왼손목은 cam1·cam2 에서만 보인다
    sk = JointGate().update(views_of(rig, X, drop=drop), rig, NO_OTHERS, sync_invalid_cameras=["cam2"])
    assert "cam2" not in sk.camera_ids
    assert sk.joints[L_WRIST].xyz is None and sk.joints[L_WRIST].validity == JointValidity.UNAVAILABLE
    assert sk.joints[NOSE].validity == JointValidity.VALID and "cam2" not in sk.joints[NOSE].camera_ids
    with pytest.raises(ValueError):
        JointGate().update(views_of(rig, X, cameras=["cam1"]), rig, NO_OTHERS, sync_invalid_cameras=["cam1"])


# --------------------------------------------------------------------------- 2대만 관측


def test_two_views_without_competitor_follow_spec():
    """명세 5.2: 2대 관측은 경쟁 후보가 있을 때만 막는다. 경쟁 후보가 없고 기하가 좋으면 VALID 다."""
    rig = corner_rig()
    X = standing_joints()
    sk = JointGate().update(views_of(rig, X, cameras=["cam1", "cam2"]), rig, others={"cam1": [], "cam2": []})
    assert all(gj.validity == JointValidity.VALID for gj in sk.joints)
    # 보수 설정: VALID 에 3대를 요구하면 2대 관절은 UNCERTAIN
    strict = JointGate(JointGateConfig(min_views_for_valid=3))
    sk = strict.update(views_of(rig, X, cameras=["cam1", "cam2"]), rig, NO_OTHERS)
    assert all(is_flagged(gj, GateIssue.FEW_VIEWS_FOR_VALID) for gj in sk.joints)


def competitor_on_epipolar_line(rig, X, own="cam1", other="cam2", depth=1.4, offset_px=(0.0, 0.0)):
    """own 카메라에서 X 와 같은 픽셀에 보이는 다른 깊이의 점(다른 사람의 같은 관절)을 other 에 투영한 keypoint."""
    C = rig.cameras[own].center
    X_alt = C + depth * (np.asarray(X) - C)
    uv = project(rig.cameras[other], X_alt) + np.asarray(offset_px)
    return (float(uv[0]), float(uv[1]), 0.9)


def two_view_with_competitor(config=None, cameras=("cam1", "cam2"), offset_px=(0.0, 3.0)):
    rig = corner_rig()
    X = standing_joints()
    other_kps = [None] * 17
    other_kps[L_WRIST] = competitor_on_epipolar_line(rig, X[L_WRIST], offset_px=offset_px)
    gate = JointGate(config)
    return gate.update(views_of(rig, X, cameras=list(cameras)), rig, others={"cam2": [tuple(other_kps)]})


def test_two_view_competitor_is_uncertain_and_marks_identity():
    """D-G2-06: 경쟁 후보가 있는 2대 관측 관절은 VALID 0건, 연결은 IDENTITY_UNCERTAIN 후보."""
    sk = two_view_with_competitor()
    assert is_flagged(sk.joints[L_WRIST], GateIssue.TWO_VIEW_COMPETITOR)
    assert sk.joints[L_WRIST].validity == JointValidity.UNCERTAIN
    assert sk.joints[L_WRIST].xyz is not None  # 좌표는 두고 등급만 낮춘다
    assert sk.identity_uncertain_candidate
    others = [gj for j, gj in enumerate(sk.joints) if j != L_WRIST]
    assert all(gj.validity == JointValidity.VALID for gj in others)


def test_negative_control_tight_epipolar_threshold_misses_competitor():
    """음성 대조: 에피폴라 기준을 1 px 로 좁히면 3 px 떨어진 경쟁 후보를 놓쳐 위 단언이 실패한다."""
    sk = two_view_with_competitor(JointGateConfig(competitor_epipolar_px=1.0))
    with pytest.raises(AssertionError):
        assert is_flagged(sk.joints[L_WRIST], GateIssue.TWO_VIEW_COMPETITOR)


def test_unknown_others_never_leaves_two_view_joint_valid():
    """others=None 은 '다른 사람 없음'이 아니라 '모름'이다. 2대 관측 관절은 VALID 로 두지 않는다 (D-G2-06)."""
    rig = corner_rig()
    X = standing_joints()
    sk = JointGate().update(views_of(rig, X, cameras=["cam1", "cam2"]), rig, None)
    for gj in sk.joints:
        assert is_flagged(gj, GateIssue.COMPETITOR_CHECK_UNAVAILABLE)
        assert gj.validity == JointValidity.UNCERTAIN and gj.xyz is not None
    assert not sk.identity_uncertain_candidate  # 모르는 것과 애매한 연결은 다르다
    # 3대 이상 관측 관절은 경쟁 후보 검사 대상이 아니다
    sk = JointGate().update(views_of(rig, X, cameras=["cam1", "cam2", "cam3"]), rig, None)
    assert all(gj.validity == JointValidity.VALID for gj in sk.joints)
    # 빠뜨릴 수 없다: others 는 필수 인자
    with pytest.raises(TypeError):
        JointGate().update(views_of(rig, X), rig)  # type: ignore[call-arg]


def test_others_with_wrong_joint_count_is_rejected():
    rig = corner_rig()
    views = views_of(rig, standing_joints(), cameras=["cam1", "cam2"])
    with pytest.raises(ValueError):
        JointGate().update(views, rig, {"cam2": [((100.0, 100.0, 0.9),)]})


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_competitor_makes_check_unknown(bad):
    """다른 사람 keypoint 가 유한하지 않으면 그 관절의 경쟁 후보 검사는 '알 수 없음': 2대 관절 UNCERTAIN, 예외 없음."""
    rig = corner_rig()
    X = standing_joints()
    for value in [(bad, 500.0, 0.9), (500.0, 500.0, bad)]:
        other = [None] * 17
        other[L_WRIST] = value
        sk = JointGate().update(views_of(rig, X, cameras=["cam1", "cam2"]), rig, {"cam2": [tuple(other)]})
        assert is_flagged(sk.joints[L_WRIST], GateIssue.COMPETITOR_CHECK_UNAVAILABLE)
        assert all(gj.validity == JointValidity.VALID for j, gj in enumerate(sk.joints) if j != L_WRIST)
    # 같은 관절에 진짜 경쟁 후보도 있으면 경쟁 후보로 판정한다
    other_bad = [None] * 17
    other_bad[L_WRIST] = (bad, 500.0, 0.9)
    other_real = [None] * 17
    other_real[L_WRIST] = competitor_on_epipolar_line(rig, X[L_WRIST], offset_px=(0.0, 3.0))
    others = {"cam2": [tuple(other_bad), tuple(other_real)]}
    sk = JointGate().update(views_of(rig, X, cameras=["cam1", "cam2"]), rig, others)
    assert is_flagged(sk.joints[L_WRIST], GateIssue.TWO_VIEW_COMPETITOR)
    # 3대 관측 관절에는 영향이 없다
    sk = JointGate().update(views_of(rig, X, cameras=["cam1", "cam2", "cam3"]), rig, {"cam2": [tuple(other_bad)]})
    assert sk.joints[L_WRIST].validity == JointValidity.VALID


def test_competitor_does_not_matter_with_three_views_or_off_line():
    sk = two_view_with_competitor(cameras=("cam1", "cam2", "cam3"))
    assert sk.joints[L_WRIST].validity == JointValidity.VALID
    assert not sk.identity_uncertain_candidate
    sk = two_view_with_competitor(offset_px=(0.0, 60.0))  # 에피폴라선에서 먼 다른 사람
    assert sk.joints[L_WRIST].validity == JointValidity.VALID


def test_competitor_behind_camera_is_not_counted():
    """에피폴라선은 카메라 앞뒤를 가리지 않는다. 짝지은 두 시선이 카메라 뒤에서 만나면 후보가 아니다."""
    rig = corner_rig()
    X = standing_joints()
    other_kps = [None] * 17
    other_kps[L_WRIST] = competitor_on_epipolar_line(rig, X[L_WRIST], depth=-0.3)  # cam1 뒤쪽 점의 투영
    sk = JointGate().update(views_of(rig, X, cameras=["cam1", "cam2"]), rig, others={"cam2": [tuple(other_kps)]})
    assert sk.joints[L_WRIST].validity == JointValidity.VALID


def test_epipolar_distance_is_zero_for_true_match():
    rig = corner_rig()
    a, b = rig.cameras["cam1"], rig.cameras["cam3"]
    X = np.array([5.0, 2.0, 1.3])
    assert epipolar_distance_px(a, project(a, X), b, project(b, X)) < 1e-6
    assert epipolar_distance_px(b, project(b, X), a, project(a, X)) < 1e-6
    assert epipolar_distance_px(a, project(a, X), b, project(b, X) + np.array([0.0, 50.0])) > 10


# --------------------------------------------------------------------------- 좁은 시선각·불확실성


def test_narrow_ray_angle_is_uncertain():
    rig = corner_rig()
    X = np.tile([4.5, 150.0, 1.0], (17, 1))  # cam1·cam2 앞, 기선(8.4 m)에 비해 아주 먼 점
    sk = JointGate().update(views_of(rig, X, cameras=["cam1", "cam2"]), rig, NO_OTHERS)
    gj = sk.joints[NOSE]
    assert gj.xyz is not None
    assert is_flagged(gj, JointIssue.NARROW_RAY_ANGLE)
    assert GateIssue.HIGH_TRIANGULATION_UNCERTAINTY in gj.reasons
    assert gj.sigma_xyz_m[1] > 1.0  # 깊이(Y) 방향 불확실성이 크다


def short_baseline():
    """0.7 m 떨어진 두 카메라가 4 m 앞을 본다: 시선각 약 10° (5° 기준 통과), 깊이 표준편차는 5 cm 를 넘는다."""
    target = (0.0, 4.0, 1.0)
    rig = make_rig({"a": ((-0.35, 0.0, 1.0), target), "b": ((0.35, 0.0, 1.0), target)}, f=1000.0, size=(1920, 1080))
    return rig, np.tile(target, (17, 1))


def test_wide_enough_angle_but_high_uncertainty_is_uncertain():
    rig, X = short_baseline()
    gj = JointGate().update(views_of(rig, X), rig, NO_OTHERS).joints[NOSE]
    assert gj.ray_angle_rad > math.radians(5)
    assert JointIssue.NARROW_RAY_ANGLE not in gj.reasons
    assert is_flagged(gj, GateIssue.HIGH_TRIANGULATION_UNCERTAINTY)
    assert gj.sigma_max_m > 0.05


def test_negative_control_loose_sigma_lets_short_baseline_pass():
    """음성 대조: 불확실성 상한을 1 m 로 풀면 위 단언이 실패한다(그 관절이 VALID 가 된다)."""
    rig, X = short_baseline()
    gj = JointGate(JointGateConfig(max_position_sigma_m=1.0)).update(views_of(rig, X), rig, NO_OTHERS).joints[NOSE]
    assert gj.validity == JointValidity.VALID
    with pytest.raises(AssertionError):
        assert is_flagged(gj, GateIssue.HIGH_TRIANGULATION_UNCERTAINTY)


def test_covariance_matches_monte_carlo():
    """해석 공분산이 같은 픽셀 잡음의 삼각측량 산포와 맞는다."""
    from simai_ai.geometry.triangulation import triangulate_point

    rig, _ = short_baseline()
    cams = list(rig.cameras.values())
    X = np.array([0.0, 4.0, 1.0])
    cov = triangulation_covariance(cams, X, 2.0)
    rng = np.random.default_rng(3)
    px = np.array([project(c, X) for c in cams])
    samples = np.array([triangulate_point(cams, px + rng.normal(0, 2.0, px.shape)) for _ in range(400)])
    np.testing.assert_allclose(np.sqrt(np.diag(np.cov(samples.T))), np.sqrt(np.diag(cov)), rtol=0.2)


# --------------------------------------------------------------------------- 재투영 잔차


def test_high_reprojection_residual_is_uncertain_and_never_upgraded():
    rig = corner_rig()
    X = standing_joints()
    # cam1 관측의 에피폴라선에 수직으로 30 px 밀면 2대 재투영 잔차가 약 15 px (8 px 초과, 이상치 20 px 이하)
    a, b = rig.cameras["cam1"], rig.cameras["cam2"]
    uv_a, uv_b = project(a, X[L_WRIST]), project(b, X[L_WRIST])
    nu, nv = (epipolar_distance_px(a, uv_a, b, uv_b + d) for d in (np.array([1.0, 0.0]), np.array([0.0, 1.0])))
    same_sign = epipolar_distance_px(a, uv_a, b, uv_b + np.array([1.0, 1.0])) > max(nu, nv)
    normal = np.array([nu, nv if same_sign else -nv])  # 거리는 절댓값이라 두 성분의 부호 관계를 따로 정한다
    views = views_of(rig, X, cameras=["cam1", "cam2"], noise={("cam2", L_WRIST): 30.0 * normal / np.linalg.norm(normal)})
    gj = JointGate(JointGateConfig(max_position_sigma_m=10.0)).update(views, rig, NO_OTHERS).joints[L_WRIST]
    assert gj.xyz is not None
    assert is_flagged(gj, JointIssue.HIGH_REPROJECTION_ERROR)
    assert 8.0 < gj.reprojection_error_px < 20.0


@pytest.mark.parametrize("state", ["UNCALIBRATED", "DRIFTED"])
def test_untrusted_calibration_caps_at_uncertain(state):
    rig = corner_rig(state=state)
    sk = JointGate().update(views_of(rig, standing_joints()), rig, NO_OTHERS)
    assert all(is_flagged(gj, JointIssue.UNTRUSTED_CALIBRATION) for gj in sk.joints)


# --------------------------------------------------------------------------- 뼈 길이 급변


def wrist_jump(config=None):
    """이력을 쌓은 뒤 왼손목만 0.3 m 들린 골격(3 m/s, 속도 상한 안): 아래팔 길이가 0.25 → 0.05 m."""
    rig = corner_rig()
    gate = JointGate(config)
    t, x = warm_up(gate, rig)
    X = standing_joints(x=x + 0.05)
    X[L_WRIST, 2] += 0.3
    return gate, gate.update(views_of(rig, X, t=t + 100), rig, NO_OTHERS)


def test_bone_length_jump_flags_both_endpoints():
    gate, sk = wrist_jump()
    assert gate.bone_reference((L_ELBOW, L_WRIST)) == pytest.approx(0.25, abs=1e-6)
    for j in (L_WRIST, L_ELBOW):
        assert is_flagged(sk.joints[j], GateIssue.BONE_LENGTH_CHANGE)
        assert GateIssue.SPEED_LIMIT not in sk.joints[j].reasons
    untouched = {j for bone in COCO17_BONES if L_WRIST not in bone and L_ELBOW not in bone for j in bone} - {
        L_WRIST,
        L_ELBOW,
    }
    assert all(sk.joints[j].validity == JointValidity.VALID for j in untouched)


def test_negative_control_loose_bone_tolerance_lets_jump_pass():
    """음성 대조: 뼈 길이 상대 허용을 10배로 풀면 위 단언이 실패한다."""
    _, sk = wrist_jump(JointGateConfig(bone_rel_tolerance=10.0))
    with pytest.raises(AssertionError):
        assert is_flagged(sk.joints[L_WRIST], GateIssue.BONE_LENGTH_CHANGE)


def test_bone_check_needs_enough_samples():
    rig = corner_rig()
    gate = JointGate()
    warm_up(gate, rig, frames=3)  # bone_min_samples(5) 미만
    X = standing_joints(x=3.15)
    X[L_WRIST, 2] += 0.3
    sk = gate.update(views_of(rig, X, t=T + 300), rig, NO_OTHERS)
    assert sk.joints[L_WRIST].validity == JointValidity.VALID


# --------------------------------------------------------------------------- 속도·시간 점프


def body_jump(dt_ms, config=None, distance=1.0):
    """이력을 쌓은 뒤 dt_ms 뒤에 몸 전체가 distance 만큼 옮겨진 골격 (뼈 길이는 같다)."""
    rig = corner_rig()
    gate = JointGate(config)
    t, x = warm_up(gate, rig)
    return gate, rig, gate.update(views_of(rig, standing_joints(x=x + distance), t=t + dt_ms), rig, NO_OTHERS), t + dt_ms


def test_speed_limit_flags_teleport():
    _, _, sk, _ = body_jump(100)  # 1 m / 0.1 s = 10 m/s
    for gj in sk.joints:
        assert is_flagged(gj, GateIssue.SPEED_LIMIT)
        assert gj.speed_mps == pytest.approx(10.0, rel=1e-6)
        assert GateIssue.BONE_LENGTH_CHANGE not in gj.reasons


def test_negative_control_loose_speed_limit_lets_teleport_pass():
    """음성 대조: 속도 상한을 100 m/s 로 풀면 위 단언이 실패한다."""
    _, _, sk, _ = body_jump(100, JointGateConfig(max_joint_speed_mps=100.0))
    with pytest.raises(AssertionError):
        assert is_flagged(sk.joints[NOSE], GateIssue.SPEED_LIMIT)


def test_time_jump_beyond_history_gap_skips_speed_check():
    """가림 뒤 재관측(직전 VALID 가 max_history_gap_ms 보다 오래됨)은 속도를 비교하지 않는다."""
    _, _, sk, _ = body_jump(2000)
    assert all(gj.validity == JointValidity.VALID and gj.speed_mps is None for gj in sk.joints)


def test_speed_flag_recovers_after_teleport():
    """순간이동으로 막힌 뒤 새 위치에 머물면, 직전 VALID 대비 속도가 상한 아래로 내려가거나 이력이 오래되면
    다시 VALID 가 된다 (영구히 막히지 않는다)."""
    gate, rig, sk, t = body_jump(100)
    t_last_valid = t - 100
    x_new = 3.35 + 1.0
    while any(gj.validity != JointValidity.VALID for gj in sk.joints):
        assert all(gj.speed_mps > gate.config.max_joint_speed_mps for gj in sk.joints)
        t += 100
        assert t - t_last_valid <= gate.config.max_history_gap_ms + 100
        sk = gate.update(views_of(rig, standing_joints(x=x_new), t=t), rig, NO_OTHERS)


def test_time_must_move_forward_and_reset_clears_history():
    rig = corner_rig()
    gate = JointGate()
    gate.update(views_of(rig, standing_joints(), t=T), rig, NO_OTHERS)
    with pytest.raises(ValueError):
        gate.update(views_of(rig, standing_joints(), t=T), rig, NO_OTHERS)
    with pytest.raises(ValueError):
        gate.update(views_of(rig, standing_joints(), t=T - 100), rig, NO_OTHERS)
    gate.reset()
    sk = gate.update(views_of(rig, standing_joints(x=1.0), t=T - 100), rig, NO_OTHERS)
    assert all(gj.validity == JointValidity.VALID for gj in sk.joints)


def test_speed_compares_against_last_valid_not_last_flagged():
    """한 프레임 튄 관절(UNCERTAIN)은 속도 기준점이 되지 않는다: 다음 프레임이 원래 자리면 VALID."""
    rig = corner_rig()
    gate = JointGate()
    t, x = warm_up(gate, rig)
    X = standing_joints(x=x)
    X_bad = X.copy()
    X_bad[NOSE, 1] += 1.0
    sk = gate.update(views_of(rig, X_bad, t=t + 100), rig, NO_OTHERS)
    assert is_flagged(sk.joints[NOSE], GateIssue.SPEED_LIMIT)
    sk = gate.update(views_of(rig, X, t=t + 200), rig, NO_OTHERS)
    assert sk.joints[NOSE].validity == JointValidity.VALID


# --------------------------------------------------------------------------- head_z


def test_head_z_uses_valid_ears_and_eyes():
    rig = corner_rig()
    X = standing_joints()
    sk = JointGate().update(views_of(rig, X), rig, NO_OTHERS)
    hz = sk.head_z(0.12)
    assert hz.validity == JointValidity.VALID
    assert hz.joints == (1, 2, 3, 4)
    assert hz.value == pytest.approx(float(np.mean(X[1:5, 2])) + 0.12, abs=1e-6)
    assert 0 < hz.sigma_m < 0.01


def test_head_z_falls_back_to_uncertain_then_empty():
    rig = corner_rig()
    X = standing_joints()
    # 눈·귀가 cam1·cam2 에서만 보이고 왼귀만 경쟁 후보가 있다 → 왼귀 UNCERTAIN, 나머지 VALID → VALID 3개만 쓴다
    face_drop = {c: [1, 2, 3, 4] for c in ("cam3", "cam4")}
    other = [None] * 17
    other[L_EAR] = competitor_on_epipolar_line(rig, X[L_EAR], offset_px=(0.0, 2.0))
    sk = JointGate().update(views_of(rig, X, drop=face_drop), rig, others={"cam2": [tuple(other)]})
    hz = sk.head_z(0.12)
    assert hz.validity == JointValidity.VALID and L_EAR not in hz.joints
    # 눈·귀가 모두 UNCERTAIN 이면 값은 내되 UNCERTAIN (낙상 판단은 "확인 필요")
    sk = JointGate().update(views_of(rig, X, cameras=["cam1", "cam2"]), rig, NO_OTHERS)
    unc = JointGate(JointGateConfig(min_views_for_valid=3)).update(views_of(rig, X, cameras=["cam1", "cam2"]), rig, NO_OTHERS)
    assert unc.head_z(0.12).validity == JointValidity.UNCERTAIN
    assert unc.head_z(0.12).value == pytest.approx(sk.head_z(0.12).value)
    # 눈·귀가 2대 미만이면 비운다 (Z=0 등으로 채우지 않는다)
    sk = JointGate().update(views_of(rig, X, drop={c: [1, 2, 3, 4] for c in ("cam2", "cam3", "cam4")}), rig, NO_OTHERS)
    hz = sk.head_z(0.12)
    assert hz.value is None and hz.validity == JointValidity.UNAVAILABLE


# --------------------------------------------------------------------------- 설정


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_views_for_valid": 1},
        {"keypoint_sigma_px": 0.0},
        {"max_joint_speed_mps": -1.0},
        {"bone_min_samples": 40},
        {"bone_abs_tolerance_m": -0.1},
        {"competitor_epipolar_px": 0.0},
    ],
)
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        JointGateConfig(**kwargs)


def test_config_is_explicit_and_frozen():
    cfg = JointGateConfig()
    with pytest.raises(FrozenInstanceError):
        cfg.max_joint_speed_mps = 1.0  # type: ignore[misc]
    assert replace(cfg, max_joint_speed_mps=1.0).max_joint_speed_mps == 1.0
