"""Cross-view Association 단위 시험 (명세 5.2 추적 3계층·계산 순서, 부록 D-G2-04).

합성 카메라 4대(교실 모서리, 높이 2.7 m)와 합성 골격(COCO17)을 투영해 view 를 만든다.
"""

import dataclasses
import json
import math

import numpy as np
import pytest

from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.base import CalibrationProvider
from simai_ai.tracking.cross_view import (
    CrossViewAssociator,
    CrossViewConfig,
    LocalTrackletBuffer,
    LocalView,
    PairEvidence,
    RejectReason,
    UncertainReason,
    ViewCluster,
)
from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection

T = 1_760_342_400_000
SIZE = (2304, 1296)
CORNERS = {"cam1": (0.3, 0.3, 2.7), "cam2": (8.7, 0.3, 2.7), "cam3": (8.7, 7.2, 2.7), "cam4": (0.3, 7.2, 2.7)}

# COCO17 관절의 서 있는 자세 오프셋 (dx 오른쪽, dz 위, 미터). 발목은 지지 표면 위 0.08 m
_OFFSETS = [
    (0.0, 1.60),
    (-0.03, 1.63),
    (0.03, 1.63),
    (-0.08, 1.60),
    (0.08, 1.60),
    (-0.20, 1.40),
    (0.20, 1.40),
    (-0.25, 1.10),
    (0.25, 1.10),
    (-0.25, 0.85),
    (0.25, 0.85),
    (-0.12, 0.95),
    (0.12, 0.95),
    (-0.12, 0.50),
    (0.12, 0.50),
    (-0.12, 0.08),
    (0.12, 0.08),
]


def look_at(position, target):
    z = np.subtract(target, position)
    z = z / np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x = x / np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.vstack([x, y, z])
    return R, -R @ np.asarray(position, dtype=float)


def calibration_doc():
    f = (SIZE[0] / 2) / math.tan(math.radians(105) / 2)
    cameras = []
    for cid, pos in CORNERS.items():
        R, t = look_at(pos, (4.5, 3.75, 0.9))
        cameras.append(
            {
                "camera_id": cid,
                "image_size": list(SIZE),
                "K": [[f, 0.0, SIZE[0] / 2], [0.0, f, SIZE[1] / 2], [0.0, 0.0, 1.0]],
                "distortion": {"model": "NONE", "coeffs": []},
                "R": R.tolist(),
                "t": t.tolist(),
            }
        )
    return {
        "schema_version": "1.1.0",
        "calibration_version": "test-rig-1",
        "provider_source": "SIM",
        "state": "PROVISIONAL",
        "created_at": T,
        "coordinate_frame": {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"},
        "scale_basis": "DATASET_GT",
        "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
        "cameras": cameras,
    }


class DictCalibrationProvider(CalibrationProvider):
    def __init__(self, doc):
        self._doc = doc

    def current(self):
        return json.loads(json.dumps(self._doc))


@pytest.fixture(scope="module")
def rig():
    return CameraRig.from_provider(DictCalibrationProvider(calibration_doc()))


def skeleton(x, y, surface_z=0.0):
    """(x, y) 에 +x 방향을 보고 선 사람의 관절 3D. 오른쪽은 -y."""
    return [np.array([x, y - dx, surface_z + dz]) for dx, dz in _OFFSETS]


def project(rig, cam_id, joints, shift=(0.0, 0.0), noise=0.0, rng=None):
    cam = rig.cameras[cam_id]
    kps = []
    for X in joints:
        xc = cam.to_camera(X)[0]
        uv = cam.K[:2, :2] @ (xc[:2] / xc[2]) + cam.K[:2, 2] + np.asarray(shift)
        if noise:
            uv = uv + rng.normal(0.0, noise, 2)
        kps.append((float(uv[0]), float(uv[1]), 0.9))
    return tuple(kps)


def lv(cam_id, track, keypoints, t=T):
    return LocalView(cam_id, track, InterpolatedView(cam_id, t, keypoints, "SIM", 0.0, (t,)))


def views_of(rig, people, t=T, cameras=tuple(CORNERS), **kwargs):
    """people: {이름: 관절 3D}. 로컬 트랙 ID 는 '{카메라}-{이름}'."""
    return [
        lv(c, f"{c}-{name}", project(rig, c, joints, **kwargs), t) for name, joints in people.items() for c in cameras
    ]


def owner(key):
    return key[1].split("-", 1)[1]


def assert_pure_and_exclusive(frame):
    for c in frame.clusters:
        assert len(set(c.camera_ids)) == len(c.camera_ids), f"같은 카메라 두 view: {c.keys}"
        assert len({owner(k) for k in c.keys}) == 1, f"사람이 섞였다: {c.keys}"


def test_two_people_apart(rig):
    people = {"a": skeleton(3.0, 3.0), "b": skeleton(6.0, 5.0)}
    frame = CrossViewAssociator(rig).associate(T, views_of(rig, people))
    assert frame.calibration_version == "test-rig-1"
    assert len(frame.clusters) == 2
    assert_pure_and_exclusive(frame)
    for c in frame.clusters:
        assert set(c.camera_ids) == set(CORNERS)
        assert not c.identity_uncertain
        assert c.skeleton is not None and set(c.skeleton.camera_ids) == set(CORNERS)
        truth = people[owner(c.keys[0])]
        pelvis = c.skeleton.pelvis_xyz()
        np.testing.assert_allclose(pelvis, (truth[11] + truth[12]) / 2, atol=1e-3)
        assert c.skeleton.to_skeleton()["joint_source"] == ["observed"] * 17


def test_epipolar_30_default_keeps_downstream_geometry_gates(rig):
    """두 합성 카메라의 투영을 옮겨 20~30 px 쌍과 30 px 초과 쌍을 만든다."""
    default = CrossViewAssociator(rig)
    old = CrossViewAssociator(rig, CrossViewConfig(epipolar_gate_px=20.0))
    assert default.config.epipolar_gate_px == 30.0
    person = skeleton(4.0, 3.0)
    a = lv("cam1", "a", project(rig, "cam1", person))
    pa = default._prepare(a)
    selected = {}
    for pixels in range(1, 201):
        b = lv("cam2", "b", project(rig, "cam2", person, shift=(0.0, float(pixels))))
        pb = default._prepare(b)
        distance = default._epipolar_px(pa, pb, pa.mask & pb.mask)
        if 24 <= distance <= 26:
            selected.setdefault("inside", (b, pb))
        if 34 <= distance <= 36:
            selected.setdefault("outside", (b, pb))
    assert set(selected) == {"inside", "outside"}
    for name, (b, pb) in selected.items():
        single = default._pair(pa, pb)
        batched = default._pairs({a.key: pa, b.key: pb})[(a.key, b.key)]
        assert single.reject == batched.reject
        assert single.epipolar_px == pytest.approx(batched.epipolar_px)
        assert (single.reject is RejectReason.EPIPOLAR) is (name == "outside")
        assert old._pair(old._prepare(a), old._prepare(b)).reject is RejectReason.EPIPOLAR
    assert default.config.residual_gate_px == old.config.residual_gate_px
    assert default.config.residual_marginal_px == old.config.residual_marginal_px


def test_close_people_never_share_camera(rig):
    """0.35 m 간격으로 붙어 선 두 사람도 같은 카메라 view 를 한 클러스터에 넣지 않는다."""
    rng = np.random.default_rng(0)
    for dy in (0.35, 0.5):
        people = {"a": skeleton(4.5, 3.5), "b": skeleton(4.5, 3.5 + dy)}
        frame = CrossViewAssociator(rig).associate(T, views_of(rig, people, noise=1.0, rng=rng))
        assert_pure_and_exclusive(frame)


def test_duplicate_detection_is_ambiguous_not_merged(rig):
    """cam1 이 같은 사람을 두 번 검출하면(점수 차 0) 어느 쪽도 합치지 않고 IDENTITY_UNCERTAIN 으로 둔다."""
    person = skeleton(4.0, 3.0)
    views = views_of(rig, {"a": person})
    views.append(lv("cam1", "cam1-a2", project(rig, "cam1", person)))
    frame = CrossViewAssociator(rig).associate(T, views)
    assert all(len(set(c.camera_ids)) == len(c.camera_ids) for c in frame.clusters)
    for key in (("cam1", "cam1-a"), ("cam1", "cam1-a2")):
        c = frame.cluster_of(key)
        assert c.keys == (key,)
        assert UncertainReason.AMBIGUOUS_ASSIGNMENT in c.uncertain_reasons
    rest = frame.cluster_of(("cam2", "cam2-a"))
    assert set(rest.camera_ids) == {"cam2", "cam3", "cam4"}
    assert rest.identity_uncertain
    assert ("cam1", "cam1-a2") in rest.competitors


def _shifted_duplicate_views(rig):
    """cam1 이 같은 사람을 6 px 어긋나게 한 번 더 검출한 입력. 비용 차가 커서 애매함 검사에는 걸리지 않는다."""
    person = skeleton(4.0, 3.0)
    views = views_of(rig, {"a": person})
    views.append(lv("cam1", "cam1-a2", project(rig, "cam1", person, shift=(6.5, 0.0))))
    return views


def test_same_camera_constraint_alone_blocks_shifted_duplicate(rig):
    """같은 카메라 금지 제약만으로 지켜지는 경우: 애매함 표시 없이도 cam1 의 두 번째 검출은 따로 남는다."""
    frame = CrossViewAssociator(rig).associate(T, _shifted_duplicate_views(rig))
    assert all(len(set(c.camera_ids)) == len(c.camera_ids) for c in frame.clusters)
    main = frame.cluster_of(("cam1", "cam1-a"))
    assert set(main.camera_ids) == set(CORNERS) and not main.identity_uncertain
    assert frame.cluster_of(("cam1", "cam1-a2")).keys == (("cam1", "cam1-a2"),)


def test_negative_control_without_same_camera_constraint(rig):
    """음성 대조: 같은 카메라 금지 제약을 끄면 같은 입력에서 같은 카메라 view 두 개가 한 클러스터에 들어간다."""
    frame = CrossViewAssociator(rig, CrossViewConfig(same_camera_exclusive=False)).associate(
        T, _shifted_duplicate_views(rig)
    )
    assert any(len(set(c.camera_ids)) < len(c.camera_ids) for c in frame.clusters)


def test_marginal_residual_is_not_merged(rig):
    """잔차가 경계(residual_marginal_px 초과)인 쌍으로는 새로 합치지 않고 표시한다."""
    rng = np.random.default_rng(1)
    config = CrossViewConfig(residual_marginal_px=1e-6)
    frame = CrossViewAssociator(rig, config).associate(T, views_of(rig, {"a": skeleton(4.0, 3.0)}, noise=1.0, rng=rng))
    assert len(frame.clusters) == 4
    assert all(UncertainReason.MARGINAL_RESIDUAL in c.uncertain_reasons for c in frame.clusters)
    assert all(c.skeleton is None for c in frame.clusters)


def test_history_keeps_link_and_id(rig):
    """연결 이력: 같은 클러스터였던 로컬 트랙 쌍은 시각마다 이력이 늘고 association_id 가 유지된다.
    이력이 쌓이면 비슷한 경쟁 검출이 나타나도 기존 연결을 지킨다(이력이 없으면 애매해서 합치지 않는다)."""
    assoc = CrossViewAssociator(rig)
    ids = []
    for k in range(10):
        t = T + 100 * k
        frame = assoc.associate(t, views_of(rig, {"a": skeleton(3.0 + 0.1 * k, 3.0)}, t=t))
        assert len(frame.clusters) == 1
        ids.append(frame.clusters[0].association_id)
    assert len(set(ids)) == 1
    assert assoc.link_age(("cam1", "cam1-a"), ("cam2", "cam2-a")) == 10

    t = T + 1000
    person = skeleton(4.0, 3.0)
    ghost = lv("cam1", "cam1-ghost", project(rig, "cam1", person, shift=(3.5, 0.0)), t)
    with_history = assoc.associate(t, views_of(rig, {"a": person}, t=t) + [ghost])
    kept = with_history.cluster_of(("cam1", "cam1-a"))
    assert set(kept.camera_ids) == set(CORNERS) and not kept.identity_uncertain
    assert kept.association_id == ids[0]

    fresh = CrossViewAssociator(rig).associate(t, views_of(rig, {"a": person}, t=t) + [ghost])
    assert fresh.cluster_of(("cam1", "cam1-a")).keys == (("cam1", "cam1-a"),)
    assert fresh.cluster_of(("cam1", "cam1-a")).identity_uncertain


def test_history_cannot_beat_geometrically_equal_rival(rig):
    """history_bonus < ambiguity_margin: 이력이 꽉 차도 기하가 똑같은 경쟁 검출(로컬 ID 가 옆 사람으로 넘어간 경우 등)은
    애매함으로 남는다. 이력만으로 연결을 지키지 않는다."""
    assoc = CrossViewAssociator(rig)
    for k in range(10):
        t = T + 100 * k
        assoc.associate(t, views_of(rig, {"a": skeleton(3.0 + 0.1 * k, 3.0)}, t=t))
    t = T + 1000
    person = skeleton(4.0, 3.0)
    twin = lv("cam1", "cam1-twin", project(rig, "cam1", person), t)
    frame = assoc.associate(t, views_of(rig, {"a": person}, t=t) + [twin])
    c = frame.cluster_of(("cam1", "cam1-a"))
    assert c.keys == (("cam1", "cam1-a"),) and UncertainReason.AMBIGUOUS_ASSIGNMENT in c.uncertain_reasons


def test_history_is_dropped_when_split(rig):
    assoc = CrossViewAssociator(rig)
    assoc.associate(T, views_of(rig, {"a": skeleton(3.0, 3.0)}))
    assert assoc.link_age(("cam1", "cam1-a"), ("cam2", "cam2-a")) == 1
    # cam2 트랙이 이제 다른 사람 위치를 보인다: 기하가 반대하므로 이력을 지운다
    views = views_of(rig, {"a": skeleton(3.0, 3.0)}, t=T + 100, cameras=("cam1", "cam3", "cam4"))
    views.append(lv("cam2", "cam2-a", project(rig, "cam2", skeleton(7.0, 6.0)), T + 100))
    frame = assoc.associate(T + 100, views)
    assert frame.cluster_of(("cam2", "cam2-a")).keys == (("cam2", "cam2-a"),)
    assert assoc.link_age(("cam1", "cam1-a"), ("cam2", "cam2-a")) == 0
    assert assoc.link_age(("cam1", "cam1-a"), ("cam3", "cam3-a")) == 2


def test_elevated_person_floor_hypothesis_is_not_position(rig):
    """임시 바닥 가설은 후보 거르기에만 쓴다: 0.4 m 단상 위 사람도 연결되고, 관절 Z 는 삼각측량 값(바닥으로 내리지 않음)."""
    people = {"a": skeleton(2.5, 2.0, surface_z=0.4)}
    frame = CrossViewAssociator(rig).associate(T, views_of(rig, people))
    (c,) = frame.clusters
    assert set(c.camera_ids) == set(CORNERS)
    ankles = [c.skeleton.joints[j].xyz for j in (15, 16)]
    assert all(z == pytest.approx(0.48, abs=1e-3) for _, _, z in ankles)
    # 결과 객체에는 바닥 가설 위치가 없다 (거리만 근거로 남는다)
    for cls in (PairEvidence, ViewCluster):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not names & {"floor_xy", "ground_anchor_xyz", "position_xy"}


@pytest.mark.parametrize("surface_z", [1.2, 1.5, 2.0])
@pytest.mark.parametrize("xy", [(4.5, 3.75), (3.5, 3.0), (5.5, 4.5)])
def test_person_on_high_surface_is_connected(rig, surface_z, xy):
    """바닥 가설 높이(0~1 m)보다 높은 면(책상 위 의자 1.2 m, R3 고소 2 m) 위 사람도 4대 한 클러스터로 연결된다.
    바닥 가설은 판단할 수 없음으로 두고 삼각측량 검증에 맡긴다. 관절 Z 는 삼각측량 값 그대로다."""
    rng = np.random.default_rng(int(surface_z * 10))
    people = {"a": skeleton(*xy, surface_z=surface_z)}
    frame = CrossViewAssociator(rig).associate(T, views_of(rig, people, noise=1.0, rng=rng))
    (c,) = frame.clusters
    assert set(c.camera_ids) == set(CORNERS) and not c.identity_uncertain
    assert all(p.feasible for p in frame.pairs)
    ankle_z = [c.skeleton.joints[j].xyz[2] for j in (15, 16)]
    assert all(z == pytest.approx(surface_z + 0.08, abs=0.03) for z in ankle_z)


def test_far_apart_people_still_rejected_without_floor_gate(rig):
    """바닥 가설로 탈락시키지 않아도 몇 m 떨어진 엉뚱한 짝은 에피폴라·삼각측량이 거른다."""
    people = {"a": skeleton(1.5, 1.5), "b": skeleton(7.5, 6.0), "c": skeleton(2.0, 6.0, surface_z=2.0)}
    frame = CrossViewAssociator(rig).associate(T, views_of(rig, people))
    assert len(frame.clusters) == 3
    assert_pure_and_exclusive(frame)


def test_excluded_cameras_are_not_fused(rig):
    """동기화 무효 카메라(호출자가 System Health 에서 넘김)의 view 는 이 시각 연결에 쓰지 않는다."""
    frame = CrossViewAssociator(rig).associate(T, views_of(rig, {"a": skeleton(3.0, 3.0)}), exclude_cameras={"cam2"})
    (c,) = frame.clusters
    assert set(c.camera_ids) == {"cam1", "cam3", "cam4"}
    assert frame.excluded == (("cam2", "cam2-a"),)
    assert all("cam2" not in (p.a[0], p.b[0]) for p in frame.pairs)


def test_exclude_cameras_input_checks(rig):
    assoc = CrossViewAssociator(rig)
    views = views_of(rig, {"a": skeleton(3.0, 3.0)})
    with pytest.raises(TypeError):
        assoc.associate(T, views, exclude_cameras="cam2")  # 문자열 하나는 글자별로 풀려 조용히 무시된다
    with pytest.raises(ValueError, match="cam9"):
        assoc.associate(T, views, exclude_cameras=["cam9"])


def test_floor_cost_is_monotonic_in_gap(rig):
    """바닥 비용은 거리에 단조 비감소이고 판단 불가 비용에서 멈춘다: 먼 짝이 가까운 짝보다 싸지 않다."""
    config = CrossViewConfig()
    assoc = CrossViewAssociator(rig, config)
    a = skeleton(3.0, 3.0)
    base = assoc._prepare(lv("cam1", "cam1-a", project(rig, "cam1", a)))
    costs = []
    for dx in (0.0, 0.3, 0.45, 0.9, 2.0, 5.0):
        other = skeleton(3.0 + dx, 3.0)
        # cam2 view 의 바닥 가설만 dx 만큼 옮기고 나머지 관절은 같은 사람으로 둔다
        kps = list(project(rig, "cam2", a))
        moved = project(rig, "cam2", other)
        kps[15], kps[16] = moved[15], moved[16]
        ev = assoc._pair(base, assoc._prepare(lv("cam2", "cam2-a", tuple(kps))))
        floor_cost = round(ev.cost - ev.epipolar_px / config.epipolar_scale_px - ev.residual_px / config.residual_scale_px, 12)
        costs.append(floor_cost)
    assert costs == sorted(costs)
    assert costs[0] < 0.05 and costs[2] < config.floor_missing_cost
    assert costs[-1] == pytest.approx(config.floor_missing_cost)


def test_pair_evidence_rejects_wrong_person(rig):
    # 두 사람이 어느 카메라 쌍의 기준선 위에도 나란히 놓이지 않는 배치 (기준선 위면 2대로는 가릴 수 없다)
    people = {"a": skeleton(2.0, 5.0), "b": skeleton(7.0, 2.5)}
    frame = CrossViewAssociator(rig).associate(T, views_of(rig, people))
    for p in frame.pairs:
        assert p.a[0] != p.b[0]
        if owner(p.a) == owner(p.b):
            assert p.feasible and p.residual_px < 0.5
        else:
            assert not p.feasible and math.isinf(p.cost)


def test_single_camera_view_is_left_alone(rig):
    frame = CrossViewAssociator(rig).associate(T, views_of(rig, {"a": skeleton(3.0, 3.0)}, cameras=("cam2",)))
    (c,) = frame.clusters
    assert c.keys == (("cam2", "cam2-a"),) and c.skeleton is None and not c.identity_uncertain


def test_input_checks(rig):
    assoc = CrossViewAssociator(rig)
    good = views_of(rig, {"a": skeleton(3.0, 3.0)})
    with pytest.raises(ValueError, match="공통 시각"):
        assoc.associate(T + 1, good)
    with pytest.raises(ValueError, match="두 번"):
        assoc.associate(T, good + [good[0]])
    with pytest.raises(KeyError):
        assoc.associate(T, [lv("cam9", "x", good[0].view.keypoints)])
    with pytest.raises(ValueError, match="keypoint"):
        assoc.associate(T, [lv("cam1", "x", good[0].view.keypoints[:5])])
    assoc.associate(T, good)
    with pytest.raises(ValueError, match="늘어나는"):
        assoc.associate(T, good)
    with pytest.raises(ValueError):
        LocalView("cam1", "x", good[1].view)  # view 는 cam2 의 것
    with pytest.raises(ValueError):
        CrossViewConfig(residual_marginal_px=20.0, residual_gate_px=10.0)
    with pytest.raises(ValueError):
        CrossViewAssociator(rig, joint_layout="MHR70")


def test_tracklet_buffer_interpolates_to_common_time(rig):
    person = skeleton(3.0, 3.0)
    kps = project(rig, "cam1", person)
    moved = tuple((u + 10.0, v, c) for u, v, c in kps)

    def frame(cam, seq, t, keypoints, unc=4.0):
        det = TrackedDetection(f"{cam}-t1", PoseDetection((0.0, 0.0, 10.0, 10.0), 0.9, keypoints))
        return Observation2DFrame(cam, 0, seq, t, "CAMERA_STREAM", unc, (det,))

    buf = LocalTrackletBuffer()
    buf.add(frame("cam1", 0, T - 20, kps))
    buf.add(frame("cam1", 1, T + 20, moved))
    buf.add(frame("cam2", 0, T - 5, kps, unc=None))  # 촬영 시각 불확실성을 모르면 쓰지 않는다
    buf.add(frame("cam2", 1, T + 35, kps, unc=None))
    views, drops = buf.views_at(T)
    assert [v.key for v in views] == [("cam1", "cam1-t1")]
    assert views[0].view.keypoints[0][0] == pytest.approx(kps[0][0] + 5.0)
    assert {d.camera_id for d in drops} == {"cam2"}
    assert buf.add(frame("cam1", 2, T - 500, kps))  # 보간 창을 넘겨 늦게 온 프레임은 기록만
    buf.prune(T + 1000)
    assert buf.keys == ()


@pytest.mark.parametrize("bad", [(math.nan, 500.0, 0.9), (500.0, math.inf, 0.9), (500.0, 500.0, math.nan)])
def test_non_finite_keypoint_is_not_an_observation(rig, bad):
    """풀 수 없는 keypoint(NaN·inf)는 관측으로 세지 않는다. 연결과 삼각측량이 예외 없이 그 관절만 뺀다 (-166)."""
    views = views_of(rig, {"a": skeleton(3.0, 3.0), "b": skeleton(6.0, 4.5)})
    views = [
        lv(v.camera_id, v.local_track_id, (bad, *v.view.keypoints[1:])) if v.local_track_id == "cam1-a" else v
        for v in views
    ]
    frame = CrossViewAssociator(rig).associate(T, views)
    assert_pure_and_exclusive(frame)
    a = next(c for c in frame.clusters if ("cam1", "cam1-a") in c.keys)
    assert len(a.keys) == 4 and not a.identity_uncertain
    assert "cam1" not in a.skeleton.joints[0].camera_ids
    assert a.skeleton.joints[0].xyz is not None


def _noisy_lower_body_views(rig):
    """cam2 에서 머리·상체(0..7)는 정확하고 하체·말단 9개 관절(8..16: 팔꿈치·손목·엉덩이·무릎·발목)이 흔들린 입력."""
    person = skeleton(3.5, 3.0)
    views = views_of(rig, {"a": person}, cameras=("cam1", "cam3", "cam4"))
    kps = list(project(rig, "cam2", person))
    for j in range(8, 17):
        u, v, c = kps[j]
        kps[j] = (u, v + 26.0, c)
    views.append(lv("cam2", "cam2-a", tuple(kps)))
    return views


def _epipolar_noisy_lower_body_pair(rig):
    """cam2 에서 상체(0..10)는 약간(10 px), 하체 6개 관절(11..16: 엉덩이·무릎·발목)은 크게(48 px) 흔들린 2대 입력."""
    person = skeleton(3.5, 3.0)
    v1 = lv("cam1", "cam1-a", project(rig, "cam1", person))
    kps = list(project(rig, "cam2", person))
    for j in range(11):
        u, v, c = kps[j]
        kps[j] = (u, v + 10.0, c)
    for j in range(11, 17):
        u, v, c = kps[j]
        kps[j] = (u, v + 48.0, c)
    v2 = lv("cam2", "cam2-a", tuple(kps))
    return [v1, v2]


def test_noisy_lower_body_pair_passes_with_joint_noise_weighting(rig):
    """흔들리는 하체·말단 관절 때문에 비가중 중앙값·평균에서는 기각되던 쌍이 관절 잡음 가중(V1a) 뒤 통과한다 (-354)."""
    assoc = CrossViewAssociator(rig)
    frame = assoc.associate(T, _noisy_lower_body_views(rig))
    assert len(frame.clusters) == 1
    (c,) = frame.clusters
    assert set(c.camera_ids) == set(CORNERS) and not c.identity_uncertain
    cam2_pairs = [p for p in frame.pairs if "cam2" in (p.a[0], p.b[0])]
    assert len(cam2_pairs) == 3
    assert all(p.feasible and not p.marginal and p.residual_px <= assoc.config.residual_marginal_px for p in cam2_pairs)

    epi_frame = CrossViewAssociator(rig).associate(T, _epipolar_noisy_lower_body_pair(rig))
    (ep,) = epi_frame.pairs
    assert ep.epipolar_px <= assoc.config.epipolar_gate_px
    assert ep.reject is not RejectReason.EPIPOLAR


def test_wrong_person_pair_still_rejected_with_joint_noise_weighting(rig):
    """관절 잡음 가중(V1a)을 켜도 전신이 어긋난 다른 사람 쌍은 여전히 기각된다 (-354)."""
    people = {"a": skeleton(2.0, 5.0), "b": skeleton(7.0, 2.5)}
    frame = CrossViewAssociator(rig).associate(T, views_of(rig, people))
    assert len(frame.clusters) == 2
    assert_pure_and_exclusive(frame)
    for p in frame.pairs:
        if owner(p.a) == owner(p.b):
            assert p.feasible
        else:
            assert not p.feasible and math.isinf(p.cost)

    # 전신이 크게 어긋난 다른 트랙은 30 px 문턱에서도 정규화 가중 합이 1 이라 기각된다
    person = skeleton(3.5, 3.0)
    shifted_all = [
        lv("cam1", "cam1-a", project(rig, "cam1", person)),
        lv("cam2", "cam2-b", project(rig, "cam2", person, shift=(0.0, 48.0))),
    ]
    (bad_pair,) = CrossViewAssociator(rig).associate(T, shifted_all).pairs
    assert not bad_pair.feasible and math.isinf(bad_pair.cost)


def test_joint_noise_weighted_false_matches_unweighted_behavior(rig):
    """설정(`joint_noise_weighted=False`)으로 끄면 예전 비가중 평균·중앙값과 같고 하체 흔들림 쌍을 기각한다 (-354)."""
    unweighted_cfg = CrossViewConfig(joint_noise_weighted=False, epipolar_gate_px=20.0)
    assoc_off = CrossViewAssociator(rig, unweighted_cfg)
    frame_off = assoc_off.associate(T, _noisy_lower_body_views(rig))
    cam2_pairs = [p for p in frame_off.pairs if "cam2" in (p.a[0], p.b[0])]
    assert any(p.reject is RejectReason.RESIDUAL and p.residual_px > unweighted_cfg.residual_gate_px for p in cam2_pairs)
    assert frame_off.cluster_of(("cam2", "cam2-a")).keys == (("cam2", "cam2-a"),)

    (ep_off,) = CrossViewAssociator(rig, unweighted_cfg).associate(T, _epipolar_noisy_lower_body_pair(rig)).pairs
    assert ep_off.reject is RejectReason.EPIPOLAR and ep_off.epipolar_px > unweighted_cfg.epipolar_gate_px

    # 묶음 경로(`_pairs`)와 단일 쌍 경로(`_pair`)가 켬·끔 모두에서 일치한다
    for weighted in (True, False):
        assoc = CrossViewAssociator(rig, CrossViewConfig(joint_noise_weighted=weighted))
        prepared = {v.key: assoc._prepare(v) for v in _noisy_lower_body_views(rig)}
        for (ka, kb), ev in assoc._pairs(prepared).items():
            single = assoc._pair(prepared[ka], prepared[kb])
            assert ev.reject == single.reject and ev.marginal == single.marginal
            assert ev.epipolar_px == pytest.approx(single.epipolar_px, abs=1e-9)
            assert ev.residual_px == pytest.approx(single.residual_px, abs=1e-9)
            assert ev.cost == pytest.approx(single.cost, abs=1e-9)
