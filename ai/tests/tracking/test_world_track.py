"""World Track Manager 단위 시험 (명세 5.2 추적 3계층·ID 교정 기록, 6.1 관측 상태, 6.2 World Track 생명주기).

합성 카메라 4대(교실 모서리, 높이 2.7 m)와 합성 골격(COCO17)을 투영해 view 를 만들고, Cross-view 결과(AssociationFrame)를
실제 CrossViewAssociator 또는 손으로 만든 클러스터로 넣는다.
"""

import json
import math

import numpy as np
import pytest

from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.joint_gate import HeadZ
from simai_ai.geometry.rig import CameraRig
from simai_ai.geometry.triangulation import JointValidity, triangulate_skeleton
from simai_ai.providers.base import CalibrationProvider
from simai_ai.tracking.cross_view import (
    AssociationFrame,
    CrossViewAssociator,
    LocalView,
    UncertainReason,
    ViewCluster,
)
from simai_ai.tracking.time_model import TimeModelConfig, evaluate_track
from simai_ai.tracking.world_state import (
    SupportContact,
    world_state_message,
    world_state_payload,
    world_track_document,
)
from simai_ai.tracking.world_track import (
    IdentityConfidence,
    IdentityFlag,
    TrackLifecycle,
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
    WorldTrackSnapshot,
    session_prefix_from_ms,
)

T = 1_760_342_400_000
DT = 100
SIZE = (2304, 1296)
CORNERS = {"cam1": (0.3, 0.3, 2.7), "cam2": (8.7, 0.3, 2.7), "cam3": (8.7, 7.2, 2.7), "cam4": (0.3, 7.2, 2.7)}
ALL = tuple(CORNERS)
# COCO17 서 있는 자세 오프셋 (dx 오른쪽, dz 위, 미터)
_OFFSETS = [
    (0.0, 1.60), (-0.03, 1.63), (0.03, 1.63), (-0.08, 1.60), (0.08, 1.60),
    (-0.20, 1.40), (0.20, 1.40), (-0.25, 1.10), (0.25, 1.10), (-0.25, 0.85), (0.25, 0.85),
    (-0.12, 0.95), (0.12, 0.95), (-0.12, 0.50), (0.12, 0.50), (-0.12, 0.08), (0.12, 0.08),
]  # fmt: skip


def _look_at(position, target):
    z = np.subtract(target, position)
    z = z / np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x = x / np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.vstack([x, y, z])
    return R, -R @ np.asarray(position, dtype=float)


def _calibration_doc():
    f = (SIZE[0] / 2) / math.tan(math.radians(105) / 2)
    cameras = []
    for cid, pos in CORNERS.items():
        R, t = _look_at(pos, (4.5, 3.75, 0.9))
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
        "schema_version": "2.0.0",
        "calibration_version": "test-rig-1",
        "provider_source": "SIM",
        "state": "PROVISIONAL",
        "created_at": T,
        "coordinate_frame": {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"},
        "scale_basis": "DATASET_GT",
        "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
        "cameras": cameras,
    }


class _DictCalibrationProvider(CalibrationProvider):
    def __init__(self, doc):
        self._doc = doc

    def current(self):
        return json.loads(json.dumps(self._doc))


@pytest.fixture(scope="module")
def rig():
    return CameraRig.from_provider(_DictCalibrationProvider(_calibration_doc()))


def _joints(x, y):
    return [np.array([x, y - dx, dz]) for dx, dz in _OFFSETS]


def _view(rig, cam, local_id, t, x, y):
    camera = rig.cameras[cam]
    kps = []
    for X in _joints(x, y):
        xc = camera.to_camera(X)[0]
        uv = camera.K[:2, :2] @ (xc[:2] / xc[2]) + camera.K[:2, 2]
        kps.append((float(uv[0]), float(uv[1]), 0.9))
    return LocalView(cam, local_id, InterpolatedView(cam, t, tuple(kps), "SIM", 0.0, (t,)))


def _cluster(rig, name, t, x, y, cams=ALL, prefix="p", uncertain=False):
    views = tuple(_view(rig, c, f"{c}-{prefix}", t, x, y) for c in cams)
    skeleton = triangulate_skeleton([v.view for v in views], rig) if len(views) >= 2 else None
    reasons = (UncertainReason.AMBIGUOUS_ASSIGNMENT,) if uncertain else ()
    return ViewCluster(name, views, skeleton, reasons)


def _frame(rig, t, clusters):
    return AssociationFrame(t, rig.calibration_version, tuple(clusters), ())


def _manager(**cfg):
    return WorldTrackManager(WorldIdIssuer("test"), WorldTrackConfig(**cfg))


def _walk(manager, rig, ticks, x0=2.0, y=3.0, speed=1.0, start=0, cams=ALL, prefix="p"):
    out = []
    for k in range(start, start + ticks):
        t = T + k * DT
        x = x0 + speed * k * DT / 1000.0
        out.append(manager.update(_frame(rig, t, [_cluster(rig, "xv-1", t, x, y, cams, prefix)]), rig))
    return out


# ---------------------------------------------------------------- World ID


def test_issuer_format_and_no_reuse():
    issuer = WorldIdIssuer("abc1")
    ids = [issuer.issue() for _ in range(3)]
    assert ids == ["w-abc1-0001", "w-abc1-0002", "w-abc1-0003"]
    assert issuer.issued == tuple(ids)
    for bad in ("", "ABC", "a-b", "x" * 17, 5):
        with pytest.raises(ValueError):
            WorldIdIssuer(bad)


def test_session_prefix_differs_per_start_time():
    a, b = session_prefix_from_ms(T), session_prefix_from_ms(T + 1)
    assert a != b and a == session_prefix_from_ms(T)
    assert session_prefix_from_ms(0) == "0"
    # 재시작한 세션의 ID 는 이전 세션 ID 와 겹치지 않는다
    assert WorldIdIssuer(a).issue() != WorldIdIssuer(b).issue()
    with pytest.raises(TypeError):
        session_prefix_from_ms(1.5)


def test_config_rejects_history_bonus_not_below_margin():
    with pytest.raises(ValueError):
        WorldTrackConfig(key_bonus=0.3, ambiguity_margin=0.3)
    cfg = WorldTrackConfig.from_time_model(TimeModelConfig(max_speed_mps=1.5, predicted_max_gap_ms=400))
    assert cfg.max_speed_mps == 1.5 and cfg.max_extrapolation_ms == 400


# ---------------------------------------------------------------- 생명주기·칼만 필터


def test_new_track_tentative_then_confirmed(rig):
    frames = _walk(_manager(), rig, 4)
    first = frames[0].tracks[0]
    assert first.lifecycle is TrackLifecycle.TENTATIVE
    assert first.identity_confidence is IdentityConfidence.LOW
    assert frames[1].tracks[0].lifecycle is TrackLifecycle.TENTATIVE
    assert frames[2].tracks[0].lifecycle is TrackLifecycle.CONFIRMED
    last = frames[3].tracks[0]
    assert last.identity_confidence is IdentityConfidence.HIGH  # 4대로 갱신된 CONFIRMED
    assert {f.tracks[0].world_id for f in frames} == {"w-test-0001"}
    assert last.cameras == ALL and last.updated and last.pelvis_xyz is not None
    assert last.skeleton is not None and all(j.joint_source == "observed" for j in last.skeleton.joints)


def test_kalman_tracks_position_and_velocity(rig):
    frames = _walk(_manager(), rig, 15)
    last = frames[-1].tracks[0]
    x_true = 2.0 + 1.0 * 14 * DT / 1000.0
    assert last.position_xy == pytest.approx((x_true, 3.0), abs=0.03)
    assert last.velocity[0] == pytest.approx(1.0, abs=0.15)
    assert abs(last.velocity[1]) < 0.1
    assert max(last.position_xy_sigma) < 0.05
    assert last.position_xy_sigma == last.filter_sigma_xy  # 이번 시각 측정이 있으면 공백 확대가 없다


def test_occlusion_holds_position_grows_sigma_then_lost(rig):
    cfg = WorldTrackConfig()
    m = WorldTrackManager(WorldIdIssuer("test"), cfg)
    frames = _walk(m, rig, 10)
    seen = frames[-1].tracks[0]
    t_last = seen.t_last_valid
    states = []
    for k in range(10, 10 + 32):
        t = T + k * DT
        f = m.update(_frame(rig, t, []), rig)
        states.append((t, f))
        tr = f.track(seen.world_id)
        if t - t_last <= cfg.lost_after_ms:
            assert tr is not None
            # 속도로 옮기지 않는다(예측 위치를 관측처럼 쓰지 않음). σ 는 최대 속도 × 공백만큼 커진다
            assert tr.position_xy == seen.position_xy
            grow = cfg.max_speed_mps * (t - t_last) / 1000.0
            assert tr.position_xy_sigma[0] == pytest.approx(seen.filter_sigma_xy[0] + grow)
            assert tr.pelvis_xyz is None and tr.skeleton is None and tr.cameras == ()
            expected = TrackLifecycle.OCCLUDED if t - t_last > cfg.occluded_after_ms else TrackLifecycle.CONFIRMED
            assert tr.lifecycle is expected
            assert tr.identity_confidence is not IdentityConfidence.HIGH
            doc = world_track_document(tr, t)
            gap = t - t_last
            assert doc["observation_state"] == ("OBSERVED" if gap <= 200 else "PREDICTED" if gap <= 500 else "UNKNOWN")
    lost = [(t, f.track(seen.world_id)) for t, f in states if f.track(seen.world_id) is not None]
    assert lost[-1][1].lifecycle is TrackLifecycle.LOST
    assert lost[-1][0] - t_last > cfg.lost_after_ms
    assert states[-1][1].track(seen.world_id) is None  # LOST 는 한 번만 낸다
    assert m.lost_candidates == (seen.world_id,)  # 늦은 재연결 후보로만 남는다 (world_state 에는 없음)
    # 로컬 트랙 연결 이력 없이(새 로컬 트랙 ID) 다시 나타나면 새 ID 이고 이전 ID 를 다시 쓰지 않는다
    again = _walk(m, rig, 1, start=45, prefix="q")[0].tracks[0]
    assert again.world_id != seen.world_id
    assert again.lifecycle is TrackLifecycle.TENTATIVE
    assert m.issuer.issued == (seen.world_id, again.world_id)


def test_tentative_without_measurement_is_lost(rig):
    m = _manager()
    first = _walk(m, rig, 1)[0].tracks[0]
    f1 = m.update(_frame(rig, T + 2 * DT, []), rig)
    assert f1.track(first.world_id).lifecycle is TrackLifecycle.TENTATIVE
    f2 = m.update(_frame(rig, T + 5 * DT, []), rig)
    assert f2.track(first.world_id).lifecycle is TrackLifecycle.LOST


def test_reacquire_within_gate_keeps_id(rig):
    m = _manager()
    frames = _walk(m, rig, 10)
    wid = frames[-1].tracks[0].world_id
    for k in range(10, 18):  # 0.8 s 동안 아무 카메라에도 없음
        m.update(_frame(rig, T + k * DT, []), rig)
    # 새 로컬 트랙 ID 로 다시 나타난다 (걸어서 0.8 m 이동)
    back = _walk(m, rig, 3, start=18, prefix="q")
    assert [f.tracks[0].world_id for f in back] == [wid] * 3
    assert back[0].tracks[0].lifecycle is TrackLifecycle.CONFIRMED
    # 연결 이력 없이 게이트만으로 다시 잡았으므로 잠시 MEDIUM
    assert back[0].tracks[0].identity_confidence is IdentityConfidence.MEDIUM


def test_far_reappearance_is_new_id(rig):
    m = _manager()
    frames = _walk(m, rig, 5)
    wid = frames[-1].tracks[0].world_id
    t = T + 7 * DT
    f = m.update(_frame(rig, t, [_cluster(rig, "xv-9", t, 6.5, 3.0, prefix="q")]), rig)
    new = [tr for tr in f.tracks if tr.world_id != wid]
    assert len(new) == 1 and new[0].lifecycle is TrackLifecycle.TENTATIVE
    assert f.track(wid).association_id is None


def test_local_track_history_beyond_gate_is_not_attached(rig):
    """같은 로컬 트랙 ID 라도 3D 거리 게이트 밖이면 붙이지 않는다 (로컬 트랙 ID 가 다른 사람으로 넘어간 경우)."""
    m = _manager()
    frames = _walk(m, rig, 5)
    wid = frames[-1].tracks[0].world_id
    t = T + 5 * DT
    f = m.update(_frame(rig, t, [_cluster(rig, "xv-1", t, 6.0, 5.0)]), rig)  # prefix 같은 로컬 트랙, 4 m 떨어짐
    assert f.track(wid).association_id is None
    assert len(f.tracks) == 2


def test_single_view_cluster_never_creates_track_but_keeps_history(rig):
    m = _manager()
    t = T
    f = m.update(_frame(rig, t, [_cluster(rig, "xv-1", t, 3.0, 3.0, cams=("cam1",))]), rig)
    assert f.tracks == ()  # 2대 미만은 지어내지 않는다
    frames = _walk(m, rig, 6, start=1)
    wid = frames[-1].tracks[0].world_id
    # cam1 에만 보이는 동안(가림) 위치는 갱신하지 않고 로컬 트랙 연결 이력만 잇는다
    for k in range(7, 22):
        t = T + k * DT
        f = m.update(_frame(rig, t, [_cluster(rig, "xv-1", t, 2.0 + k * 0.1, 3.0, cams=("cam1",))]), rig)
        assert f.track(wid).association_id is None and f.track(wid).t_last_valid == T + 6 * DT
    # 2.5 s 가림 뒤(소실 3 s 전) 같은 로컬 트랙과 함께 다시 보이면 이어지고, 연결 이력이 있으므로 MEDIUM 으로 내리지 않는다
    t = T + 22 * DT
    f = m.update(_frame(rig, t, [_cluster(rig, "xv-1", t, 3.2, 3.0)]), rig)
    tr = f.track(wid)
    assert tr.updated and tr.lifecycle is TrackLifecycle.CONFIRMED
    assert tr.identity_confidence is IdentityConfidence.HIGH


# ---------------------------------------------------------------- 애매함·IDENTITY_UNCERTAIN


def _two_tracks(m, rig, y1=3.0, y2=3.6, ticks=5):
    for k in range(ticks):
        t = T + k * DT
        m.update(
            _frame(rig, t, [_cluster(rig, "xv-1", t, 3.0, y1, prefix="a"), _cluster(rig, "xv-2", t, 3.0, y2, prefix="b")]),
            rig,
        )
    return "w-test-0001", "w-test-0002"


def test_uncertain_cluster_between_two_tracks_is_not_forced(rig):
    m = _manager()
    a, b = _two_tracks(m, rig)
    t = T + 5 * DT
    # 두 Track 의 한가운데, 다른 로컬 트랙 ID 로 온 IDENTITY_UNCERTAIN 클러스터
    f = m.update(_frame(rig, t, [_cluster(rig, "xv-7", t, 3.0, 3.3, prefix="z", uncertain=True)]), rig)
    assert len(f.tracks) == 2  # 새 ID 도 만들지 않는다 (같은 사람 두 번째 ID 가 된다)
    for wid in (a, b):
        tr = f.track(wid)
        assert tr.association_id is None
        assert tr.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
        assert IdentityFlag.CROSS_VIEW_UNCERTAIN in tr.identity_flags


def test_uncertain_cluster_with_single_candidate_is_attached_and_flagged(rig):
    m = _manager()
    frames = _walk(m, rig, 5)
    wid = frames[-1].tracks[0].world_id
    t = T + 5 * DT
    f = m.update(_frame(rig, t, [_cluster(rig, "xv-1", t, 2.5, 3.0, uncertain=True)]), rig)
    tr = f.track(wid)
    assert tr.updated
    assert tr.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
    # 사유는 uncertain_hold_ms 동안 유지된다
    later = _walk(m, rig, 11, start=6)
    assert later[9].tracks[0].identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN  # 1 s 째(포함)
    assert later[10].tracks[0].identity_confidence is IdentityConfidence.HIGH  # 1 s 가 지나면 풀린다


def test_uncertain_cluster_far_from_tracks_becomes_new_uncertain_id(rig):
    m = _manager()
    t = T
    f = m.update(_frame(rig, t, [_cluster(rig, "xv-1", t, 3.0, 3.0, uncertain=True)]), rig)
    assert len(f.tracks) == 1
    assert f.tracks[0].identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN


def test_close_tracks_with_equal_cost_are_flagged_but_both_updated(rig):
    m = _manager()
    # 3 cm: 거리 비용 차 0.06 + 연결 이력 보너스 0.2 가 여유 0.3 보다 작다
    a, b = _two_tracks(m, rig, 3.0, 3.03)
    t = T + 5 * DT
    f = m.update(
        _frame(rig, t, [_cluster(rig, "xv-1", t, 3.0, 3.0, prefix="a"), _cluster(rig, "xv-2", t, 3.0, 3.03, prefix="b")]),
        rig,
    )
    for wid in (a, b):
        tr = f.track(wid)
        assert tr.updated  # 위치는 갱신한다 (붙어 선 두 사람을 둘 다 놓치지 않는다)
        assert IdentityFlag.AMBIGUOUS_TRACK_MATCH in tr.identity_flags


def test_two_people_side_by_side_keep_ids(rig):
    m = _manager()
    a, b = _two_tracks(m, rig, 3.0, 3.5, ticks=20)
    f = m.update(_frame(rig, T + 20 * DT, []), rig)
    assert {tr.world_id for tr in f.tracks} == {a, b}
    assert m.issuer.issued == (a, b)


# ---------------------------------------------------------------- ID 교정 기록


def _split_ticks(m, rig, start, stop, y=3.0):
    """Cross-view 가 한 사람을 (cam1, cam2) 와 (cam3, cam4) 두 클러스터로 계속 가른다."""
    out = []
    for k in range(start, stop):
        t = T + k * DT
        clusters = [
            _cluster(rig, "xv-1", t, 3.0, y, cams=("cam1", "cam2")),
            _cluster(rig, "xv-2", t, 3.0, y, cams=("cam3", "cam4")),
        ]
        out.append(m.update(_frame(rig, t, clusters), rig))
    return out


def test_split_person_two_ids_are_flagged_while_close_not_merged(rig):
    m = _manager()
    frames = _split_ticks(m, rig, 0, 25)  # 2.5 s: 만든 시각의 표시(1 s 유지)가 풀린 뒤에도 계속 붙어야 한다
    a, b = (tr.world_id for tr in frames[-1].tracks)
    for f in frames[12:]:
        for tr in f.tracks:
            assert IdentityFlag.POSSIBLE_DUPLICATE in tr.identity_flags
            assert tr.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
    # 다시 한 클러스터로 묶여도 합치지 않는다 (병합·교정은 S15P31A101-40)
    t = T + 25 * DT
    f = m.update(_frame(rig, t, [_cluster(rig, "xv-3", t, 3.0, 3.0)]), rig)
    assert f.revisions == () and m.revisions == ()
    assert {tr.world_id for tr in f.tracks} == {a, b}
    assert all(tr.identity_revision == 0 and tr.aliases == () for tr in f.tracks)
    assert all(tr.lifecycle is not TrackLifecycle.LOST for tr in f.tracks)
    assert all(IdentityFlag.POSSIBLE_DUPLICATE in tr.identity_flags for tr in f.tracks)


def test_local_id_switch_does_not_merge_two_people(rig):
    """리뷰 장면: CONFIRMED A·B 중 B 가 한 시각 cam3 에만 보이고, cam1 로컬 트래커가 B 의 ID 를 A 에게 넘겼다."""
    m = _manager()
    a, b = _two_tracks(m, rig, 3.0, 3.45, ticks=6)
    t = T + 6 * DT
    a_views = [_view(rig, "cam1", "cam1-b", t, 3.0, 3.0)] + [_view(rig, c, f"{c}-a", t, 3.0, 3.0) for c in ALL[1:]]
    a_cluster = ViewCluster("xv-1", tuple(a_views), triangulate_skeleton([v.view for v in a_views], rig))
    b_cluster = _cluster(rig, "xv-2", t, 3.0, 3.45, cams=("cam3",), prefix="b")
    f = m.update(_frame(rig, t, [a_cluster, b_cluster]), rig)
    assert f.track(a).updated
    tb = f.track(b)
    assert tb is not None and tb.lifecycle is not TrackLifecycle.LOST
    assert f.revisions == () and f.track(a).identity_revision == 0 and f.track(a).aliases == ()
    # 다음 시각 B 가 다시 여러 카메라에 보이면 같은 ID 로 이어진다 (새 ID 없음)
    t += DT
    f = m.update(
        _frame(rig, t, [_cluster(rig, "xv-1", t, 3.0, 3.0, prefix="a"), _cluster(rig, "xv-2", t, 3.0, 3.45, prefix="b")]),
        rig,
    )
    assert f.track(b).updated and m.issuer.issued == (a, b)


def test_uncertain_new_person_next_to_updated_track_is_not_dropped(rig):
    """이미 갱신된 Track 옆에 새로 나타난 사람의 클러스터가 IDENTITY_UNCERTAIN 이어도 새 ID 로 world_state 에 들어간다."""
    m = _manager()
    frames = _walk(m, rig, 5, speed=0.0, x0=3.0)
    a = frames[-1].tracks[0].world_id
    for k in range(5, 15):
        t = T + k * DT
        f = m.update(
            _frame(
                rig,
                t,
                [
                    _cluster(rig, "xv-1", t, 3.0, 3.0),
                    _cluster(rig, "xv-2", t, 3.0, 3.4, cams=("cam1", "cam3"), prefix="n", uncertain=True),
                ],
            ),
            rig,
        )
        new = [tr for tr in f.tracks if tr.world_id != a]
        assert len(new) == 1, k
        assert new[0].identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
        assert f.track(a).updated
    assert new[0].lifecycle is TrackLifecycle.CONFIRMED
    payload = world_state_payload(f, f.t, "scene-1")
    assert {doc["world_id"] for doc in payload["tracks"]} == {a, new[0].world_id}


# ---------------------------------------------------------------- 입력 검사


@pytest.mark.parametrize("gap_m", [0.45, 1.2])
def test_new_person_with_switched_local_id_is_not_dropped(rig, gap_m):
    """재리뷰 장면: cam1 로컬 트래커가 A 의 옛 ID 를 새 사람 B 에게 넘기고 A 에게 새 ID 를 줬다. B 의 클러스터는
    IDENTITY_UNCERTAIN. 주인 Track(A)이 멀리 있으므로 갈라진 나머지 view 가 아니다: B 는 바로 새 ID 로 들어간다."""
    m = _manager()
    frames = _walk(m, rig, 5, speed=0.0, x0=3.0, prefix="a")
    a = frames[-1].tracks[0].world_id
    for k in range(5, 10):
        t = T + k * DT
        a_views = [_view(rig, "cam1", "cam1-new", t, 3.0, 3.0)] + [
            _view(rig, c, f"{c}-a", t, 3.0, 3.0) for c in ALL[1:]
        ]
        b_views = [_view(rig, "cam1", "cam1-a", t, 3.0, 3.0 + gap_m)] + [
            _view(rig, c, f"{c}-b", t, 3.0, 3.0 + gap_m) for c in ("cam2", "cam3")
        ]
        a_cluster = ViewCluster("xv-1", tuple(a_views), triangulate_skeleton([v.view for v in a_views], rig))
        b_cluster = ViewCluster(
            "xv-2",
            tuple(b_views),
            triangulate_skeleton([v.view for v in b_views], rig),
            (UncertainReason.TWO_VIEW_COMPETITOR,),
        )
        f = m.update(_frame(rig, t, [a_cluster, b_cluster]), rig)
        assert f.track(a).updated
        new = [tr for tr in f.tracks if tr.world_id != a]
        assert len(new) == 1, k  # 첫 tick 부터 빠지지 않는다
        assert new[0].identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN


def test_blocked_split_view_near_owner_flags_owner(rig):
    """3차 리뷰 장면(0.30 m): 거리 조건 안쪽이라 새 ID 를 만들지 않을 때도 주인 Track A 에 IDENTITY_UNCERTAIN 을 붙인다."""
    m = _manager()
    frames = _walk(m, rig, 5, speed=0.0, x0=3.0, prefix="a")
    a = frames[-1].tracks[0].world_id
    assert frames[-1].tracks[0].identity_confidence is IdentityConfidence.HIGH
    t = T + 5 * DT
    a_views = [_view(rig, "cam1", "cam1-new", t, 3.0, 3.0)] + [_view(rig, c, f"{c}-a", t, 3.0, 3.0) for c in ALL[1:]]
    b_views = [_view(rig, "cam1", "cam1-a", t, 3.0, 3.3)] + [_view(rig, c, f"{c}-b", t, 3.0, 3.3) for c in ("cam2", "cam3")]
    a_cluster = ViewCluster("xv-1", tuple(a_views), triangulate_skeleton([v.view for v in a_views], rig))
    b_cluster = ViewCluster(
        "xv-2", tuple(b_views), triangulate_skeleton([v.view for v in b_views], rig), (UncertainReason.TWO_VIEW_COMPETITOR,)
    )
    f = m.update(_frame(rig, t, [a_cluster, b_cluster]), rig)
    assert [tr.world_id for tr in f.tracks] == [a]  # 갈라진 나머지 view 로 보고 새 ID 는 만들지 않는다
    tr = f.track(a)
    assert tr.updated
    assert tr.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
    assert IdentityFlag.CROSS_VIEW_UNCERTAIN in tr.identity_flags


def test_cluster_with_two_views_of_one_camera_is_rejected(rig):
    t = T
    v1, v2 = _view(rig, "cam1", "cam1-a", t, 3.0, 3.0), _view(rig, "cam1", "cam1-b", t, 3.5, 3.0)
    bad = ViewCluster("xv-1", (v1, v2), None)
    with pytest.raises(ValueError):
        _manager().update(_frame(rig, t, [bad]), rig)
    dup = [ViewCluster("xv-1", (v1,), None), ViewCluster("xv-2", (v1,), None)]
    with pytest.raises(ValueError):
        _manager().update(_frame(rig, t, dup), rig)


def test_input_checks(rig):
    m = _manager()
    _walk(m, rig, 2)
    with pytest.raises(ValueError):
        m.update(_frame(rig, T + DT, []), rig)  # 시각이 앞으로 가지 않는다
    with pytest.raises(ValueError):
        m.update(AssociationFrame(T + 5 * DT, "other-cal", (), ()), rig)
    with pytest.raises(TypeError):
        m.update(_frame(rig, T + 6 * DT, []), rig, sync_invalid_cameras="cam1")


def test_real_associator_pipeline(rig):
    """실제 CrossViewAssociator 결과를 그대로 받는다. association_id 는 World ID 가 아니다."""
    assoc = CrossViewAssociator(rig)
    m = _manager()
    for k in range(5):
        t = T + k * DT
        views = [_view(rig, c, f"{c}-1", t, 2.0 + 0.1 * k, 3.0) for c in ALL]
        views += [_view(rig, c, f"{c}-2", t, 6.0, 5.0 - 0.1 * k) for c in ALL]
        f = m.update(assoc.associate(t, views), rig)
    assert len(f.tracks) == 2
    assert all(tr.lifecycle is TrackLifecycle.CONFIRMED for tr in f.tracks)
    assert all(tr.world_id.startswith("w-test-") and not tr.world_id.startswith("xv-") for tr in f.tracks)


# ---------------------------------------------------------------- world_state 조립


def _snapshot(**kw):
    base = dict(
        world_id="w-test-0001",
        t=T,
        t_last_valid=T,
        lifecycle=TrackLifecycle.CONFIRMED,
        identity_confidence=IdentityConfidence.HIGH,
        identity_revision=0,
        position_xy=(1.0, 2.0),
        position_xy_sigma=(0.05, 0.05),
        velocity=(0.0, 0.0, 0.0),
    )
    base.update(kw)
    return WorldTrackSnapshot(**base)


def test_world_state_message_matches_contract(rig, contract_errors):
    m = WorldTrackManager(
        WorldIdIssuer("test"),
        input_provenance={"frames": {"source": "SIM"}, "is_upper_bound": True},
    )
    frames = _walk(m, rig, 5)
    for k in range(5, 12):
        frames.append(m.update(_frame(rig, T + k * DT, []), rig))
    for f in frames:
        t_latest = f.t + 40
        payload = world_state_payload(f, t_latest, "scene-3")
        msg = world_state_message(payload, msg_id="0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f", sent_at=t_latest + 5)
        assert contract_errors("world_state", msg) == []
        # 계약 1.1.0: 입력 출처를 payload 에 싣는다 (관측과 추정 구분). 봉투 버전은 마지막 변경(2.0.0, -179)
        assert msg["schema_version"] == "2.0.0"
        assert payload["input_provenance"] == {"frames": {"source": "SIM"}, "is_upper_bound": True}
        assert payload["input_provenance"] is not f.input_provenance  # 복사본
        for doc in payload["tracks"]:
            # 접지를 모르면 비운다 (Z=0 대체 금지)
            assert doc["ground_anchor_xyz"] is None and "surface_source" not in doc
            assert "aliases" not in doc  # 병합이 없었다
            assert doc["support_surface_validity"] == "UNKNOWN"
            # 계약 2.0.0: head_z 유효성을 늘 싣고, 값은 VALID·UNCERTAIN 일 때(관측 귀·눈에서만)
            assert doc["head_z_validity"] in {"VALID", "UNCERTAIN", "UNAVAILABLE"}
            assert (doc["head_z"] is not None) == (doc["head_z_validity"] != "UNAVAILABLE")
            assert doc["z_validity"] == "UNAVAILABLE" and doc["contact_validity"] == "UNAVAILABLE"
            if "skeleton" in doc:
                assert set(doc["skeleton"]["joint_source"]) == {"observed"}
    observed = world_state_payload(frames[4], frames[4].t, "scene-3")["tracks"][0]
    assert observed["observation_state"] == "OBSERVED" and observed["pelvis_xyz"] is not None
    assert observed["head_z"] == pytest.approx(1.63 + 0.12 - 0.015, abs=0.05)
    hidden = world_state_payload(frames[-1], frames[-1].t, "scene-3")["tracks"][0]
    assert hidden["observation_state"] == "UNKNOWN" and hidden["track_lifecycle"] == "OCCLUDED"
    assert hidden["pelvis_xyz"] is None and hidden["head_z"] is None and hidden["cameras"] == []


def test_payload_without_provenance_omits_field(rig, contract_errors):
    f = _walk(_manager(), rig, 1)[0]  # input_provenance 를 넘기지 않은 Manager
    payload = world_state_payload(f, f.t, "scene-1")
    assert "input_provenance" not in payload
    msg = world_state_message(payload, msg_id="0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f", sent_at=f.t)
    assert contract_errors("world_state", msg) == []
    bad = dict(payload, input_provenance={"frames": {"source": "SIM"}})  # is_upper_bound 없음
    assert contract_errors("world_state", dict(msg, payload=bad))


def test_head_z_needs_observed_skeleton_evidence():
    """계약 2.0.0: VALID·UNCERTAIN 값은 같은 Track skeleton 의 관측 귀·눈 근거가 있을 때만 싣는다(test_world_state_head_z).
    skeleton 없이 값만 오면 근거를 확인할 수 없어 UNAVAILABLE·null 이다."""
    for validity in (JointValidity.VALID, JointValidity.UNCERTAIN):
        doc = world_track_document(_snapshot(head_z=HeadZ(1.7, validity, 0.02, (1, 2))), T)
        assert (doc["head_z"], doc["head_z_sigma"], doc["head_z_validity"]) == (None, None, "UNAVAILABLE")
    assert world_track_document(_snapshot(head_z=HeadZ(None, JointValidity.UNAVAILABLE)), T)["head_z"] is None


def test_observation_state_is_separate_from_lifecycle():
    doc = world_track_document(_snapshot(lifecycle=TrackLifecycle.TENTATIVE), T + 300)
    assert (doc["track_lifecycle"], doc["observation_state"]) == ("TENTATIVE", "PREDICTED")
    doc = world_track_document(_snapshot(lifecycle=TrackLifecycle.OCCLUDED), T + 100)
    assert (doc["track_lifecycle"], doc["observation_state"]) == ("OCCLUDED", "OBSERVED")
    with pytest.raises(ValueError):
        world_track_document(_snapshot(), T - 1)  # t_latest_global 이 t_last_valid 보다 앞


def test_support_contact_rules(contract_errors):
    contact = SupportContact(
        (1.0, 2.0, 0.4), (0.05, 0.05, 0.02), "podium", "VALID", "VALID", "VALID", surface_source="inferred"
    )
    payload = {
        "t_latest_global": T,
        "calibration_version": "cal-1",
        "scene_version": "scene-1",
        "tracks": [world_track_document(_snapshot(), T, contact)],
    }
    msg = world_state_message(payload, msg_id="0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f", sent_at=T)
    assert contract_errors("world_state", msg) == []
    assert payload["tracks"][0]["ground_anchor_xyz"] == [1.0, 2.0, 0.4]
    with pytest.raises(ValueError):
        SupportContact((1.0, 2.0, 0.0), None, "floor", "UNKNOWN")  # 지지 표면을 모르면 접지점을 싣지 않는다
    with pytest.raises(ValueError):
        SupportContact((1.0, 2.0, 0.0), None, "floor", "VALID", "VALID", "UNAVAILABLE")
    with pytest.raises(ValueError):
        SupportContact(support_surface_validity="MAYBE")


def test_payload_and_message_checks(rig):
    m = _manager()
    f = _walk(m, rig, 1)[0]
    with pytest.raises(ValueError):
        world_state_payload(f, f.t, "")
    with pytest.raises(ValueError):
        world_state_payload(f, f.t, "scene-1", contacts={"w-other": SupportContact()})
    with pytest.raises(ValueError):
        world_state_message({}, msg_id="not-a-uuid", sent_at=T)


def test_time_input_uses_filter_sigma_at_t_last_valid(rig):
    m = _manager()
    frames = _walk(m, rig, 5)
    for k in range(5, 8):
        frames.append(m.update(_frame(rig, T + k * DT, []), rig))
    tr = frames[-1].tracks[0]
    ti = tr.time_input()
    assert ti.t_last_valid == tr.t_last_valid and ti.position_xy_sigma == tr.filter_sigma_xy
    state = evaluate_track(ti, tr.t, tr.t + 50)
    assert state.observation_state.value == "PREDICTED"


# ---------------------------------------------------------------- 체크포인트 (S15P31A101-51)


def test_export_tracks_round_trip_and_restored_state_is_never_observed(rig):
    """3.2: Track 상태를 체크포인트로 내보내고 되살린다. 되살린 Track 은 재관측 전이므로 OBSERVED 가 아니다."""
    from simai_ai.tracking.time_model import ObservationState
    from simai_ai.tracking.world_track import TrackCheckpoint

    m = _manager()
    frames = _walk(m, rig, 6)
    last = frames[-1].tracks[0]
    assert world_track_document(last, last.t)["observation_state"] == "OBSERVED"  # 내보내기 직전에는 관측 중
    before = m.export_tracks()
    assert m.export_tracks() == before  # 상태를 바꾸지 않는다
    (cp,) = before
    assert (cp.world_id, cp.t_last_valid, cp.lifecycle) == (last.world_id, last.t_last_valid, last.lifecycle)
    assert cp.position_xyz[:2] == pytest.approx(last.position_xy)
    assert cp.filter_sigma_xyz[:2] == pytest.approx(last.filter_sigma_xy)
    assert m.last_t == last.t
    doc = json.loads(json.dumps(cp.to_record()))
    restored = TrackCheckpoint.from_record(doc)
    assert restored == cp and restored.observation_state is ObservationState.UNKNOWN
    # 관측 상태를 기록에 끼워 넣어도(손상·위조) 받지 않는다
    with pytest.raises(ValueError, match="모름"):
        TrackCheckpoint.from_record(doc | {"observation_state": "OBSERVED"})
    with pytest.raises(ValueError):
        TrackCheckpoint.from_record(doc | {"position_xyz": [1.0, float("nan"), 0.0]})
    with pytest.raises(ValueError, match="빠짐"):
        TrackCheckpoint.from_record({k: v for k, v in doc.items() if k != "t_last_valid"})
