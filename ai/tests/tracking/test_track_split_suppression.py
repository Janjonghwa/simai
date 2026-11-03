"""-458: 작은 합성 장면으로 운영 S3·짧은 재연결 창과 관측/추정 경계를 검사한다."""

import importlib.util
from pathlib import Path

import pytest

from simai_ai.tracking.world_state import world_track_document
from simai_ai.tracking.world_track import IdentityConfidence, IdentityFlag, TrackLifecycle

_spec = importlib.util.spec_from_file_location("_wt458", Path(__file__).with_name("test_world_track.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)
T, DT = wt.T, wt.DT


@pytest.fixture(scope="module")
def rig():
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("ownerless", [False, True])
@pytest.mark.parametrize("owned", [True, False])
def test_disjoint_remainder_suppresses_new_id_and_flags_owner(rig, enabled, ownerless, owned):
    m = wt._manager(split_suppression_enabled=enabled, split_suppress_ownerless=ownerless)
    first = wt._walk(m, rig, 3, x0=3.0, speed=0.0)[-1].tracks[0]
    t = T + 3 * DT
    clusters = [wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")),
                wt._cluster(rig, "b", t, 3.0, 3.3, cams=("cam3", "cam4"), prefix="p" if owned else "new")]
    frame = m.update(wt._frame(rig, t, clusters), rig)
    suppressed = enabled and (owned or ownerless)
    assert len(m.issuer.issued) == (1 if suppressed else 2)
    if suppressed:
        (tr,) = frame.tracks
        assert tr.world_id == first.world_id and tr.t_last_valid == t
        assert tr.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
        assert IdentityFlag.CROSS_VIEW_UNCERTAIN in tr.identity_flags
        assert set(tr.cameras) in ({"cam1", "cam2"}, {"cam3", "cam4"})
        assert len(tr.local_keys) == 2  # 억제한 클러스터의 측정·소유권을 합치지 않는다
        assert frame.revisions == () and tr.identity_revision == 0
        assert m.diag["new_track.split_remainder_suppressed"] == 1


@pytest.mark.parametrize("cams,dy", [(wt.ALL, 0.45), (("cam3", "cam4"), 0.8)])
def test_overlapping_camera_or_far_person_is_registered_immediately(rig, cams, dy):
    m = wt._manager()
    t = T
    frame = m.update(wt._frame(rig, t, [
        wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")),
        wt._cluster(rig, "b", t, 3.0, 3.0 + dy, cams=cams, prefix="new"),
    ]), rig)
    assert len(frame.tracks) == 2 and all(tr.updated for tr in frame.tracks)
    assert m.config.confirm_hits == 3  # 전역 등록 확정 횟수는 늘리지 않는다


def test_distinct_people_045m_apart_keep_two_ids_with_disjoint_views(rig):
    """처음 같은 카메라에서 따로 본 두 사람은 이후 서로 다른 두 카메라씩 보여도 두 ID 를 유지한다."""
    m = wt._manager()
    assert m.config.split_suppression_enabled
    for k in range(6):
        t = T + k * DT
        frame = m.update(wt._frame(rig, t, [
            wt._cluster(rig, "a", t, 3.0, 3.0, cams=wt.ALL if k == 0 else ("cam1", "cam2"), prefix="a"),
            wt._cluster(rig, "b", t, 3.0, 3.45, cams=wt.ALL if k == 0 else ("cam3", "cam4"), prefix="b"),
        ]), rig)
        assert len(frame.tracks) == 2 and all(tr.updated for tr in frame.tracks)
        assert {tr.world_id for tr in frame.tracks} == set(m.issuer.issued[:2])
        assert frame.revisions == ()
    assert len(m.issuer.issued) == 2
    assert m.diag["new_track.split_remainder_suppressed"] == 0


@pytest.mark.parametrize("ownerless", [False, True])
def test_first_seen_disjoint_people_045m_apart_keep_two_ids_by_default(rig, ownerless):
    """처음 보는 두 사람은 두 ID. 주인 없음 포함 비교 모드에서는 1차의 누락 위험을 재현한다."""
    m = wt._manager(split_suppress_ownerless=ownerless)
    assert m.config.split_suppression_enabled
    for k in range(5):
        t = T + k * DT
        frame = m.update(wt._frame(rig, t, [
            wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2"), prefix="a"),
            wt._cluster(rig, "b", t, 3.0, 3.45, cams=("cam3", "cam4"), prefix="b"),
        ]), rig)
        assert len(frame.tracks) == (1 if ownerless else 2)
        assert all(tr.updated for tr in frame.tracks)
        if ownerless:
            (tr,) = frame.tracks
            assert tr.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
            assert IdentityFlag.CROSS_VIEW_UNCERTAIN in tr.identity_flags
            assert tr.cameras == ("cam1", "cam2")
        assert frame.revisions == ()
    assert len(m.issuer.issued) == (1 if ownerless else 2)
    assert m.diag["new_track.split_remainder_suppressed"] == (5 if ownerless else 0)


def test_recently_owned_045m_split_remainder_is_suppressed(rig):
    """한 사람의 4대 묶음이 2+2로 갈라진 나머지는 첫 등장 두 사람과 구별한다."""
    m = wt._manager()
    assert not m.config.split_suppress_ownerless
    original = wt._walk(m, rig, 3, x0=3.0, speed=0.0)[-1].tracks[0].world_id
    for k in range(3, 8):
        t = T + k * DT
        frame = m.update(wt._frame(rig, t, [
            wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")),
            wt._cluster(rig, "b", t, 3.0, 3.45, cams=("cam3", "cam4")),
        ]), rig)
        (tr,) = frame.tracks
        assert tr.world_id == original and tr.updated
        assert IdentityFlag.CROSS_VIEW_UNCERTAIN in tr.identity_flags
        assert len(tr.local_keys) == 2 and tr.cameras == ("cam1", "cam2")
    assert len(m.issuer.issued) == 1
    assert m.diag["new_track.split_remainder_suppressed"] == 5


@pytest.mark.parametrize("window", [500, 1000, 12000])
@pytest.mark.parametrize("extra_ms,suppressed", [(0, True), (1, False)])
def test_recent_owner_window_boundary_and_one_owned_key(rig, window, extra_ms, suppressed):
    """로컬 키 하나만 최근 주인의 것이어도 충분하다. 일반 key TTL·재연결 창과 별도로 만료한다."""
    m = wt._manager(split_suppression_owner_ttl_ms=window)
    original = wt._walk(m, rig, 3, x0=3.0, speed=0.0)[-1].tracks[0].world_id
    last_owned = T + 2 * DT
    # cam3·4 키는 갱신하지 않고 cam1·2로만 주인을 계속 관측한다.
    t = last_owned + window + extra_ms
    for keep_t in range(last_owned + DT, t, DT):
        m.update(wt._frame(rig, keep_t, [
            wt._cluster(rig, "a", keep_t, 3.0, 3.0, cams=("cam1", "cam2")),
        ]), rig)
    views = (wt._view(rig, "cam3", "cam3-p", t, 3.0, 3.45),
             wt._view(rig, "cam4", "cam4-new", t, 3.0, 3.45))
    remainder = wt.ViewCluster("b", views, wt.triangulate_skeleton([v.view for v in views], rig))
    frame = m.update(wt._frame(rig, t, [
        wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")), remainder,
    ]), rig)
    assert len(m.issuer.issued) == (1 if suppressed else 2)
    assert frame.track(original).updated
    assert m.diag["new_track.split_remainder_suppressed"] == int(suppressed)


@pytest.mark.parametrize("ownerless", [False, True])
def test_recent_key_of_another_track_does_not_qualify_as_remainder(rig, ownerless):
    """근처 a 가 아닌 먼 b 의 최근 키는 a 의 분할 나머지 근거가 아니다."""
    m = wt._manager(split_suppress_ownerless=ownerless)
    for k in range(3):
        t = T + k * DT
        m.update(wt._frame(rig, t, [
            wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")),
            wt._cluster(rig, "b", t, 3.0, 5.0, cams=("cam3", "cam4"), prefix="b"),
        ]), rig)
    t = T + 3 * DT
    views = (wt._view(rig, "cam3", "cam3-b", t, 3.0, 3.45),
             wt._view(rig, "cam4", "cam4-new", t, 3.0, 3.45))
    candidate = wt.ViewCluster("new", views, wt.triangulate_skeleton([v.view for v in views], rig))
    frame = m.update(wt._frame(rig, t, [
        wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")), candidate,
    ]), rig)
    assert len(m.issuer.issued) == (2 if ownerless else 3)
    assert sum(tr.updated for tr in frame.tracks) == (1 if ownerless else 2)
    assert m.diag["new_track.split_remainder_suppressed"] == int(ownerless)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("ownerless", [False, True])
def test_current_single_view_detection_of_owner_vetoes_suppression(rig, reverse, ownerless):
    """주인 갱신 묶음(cam1·2) 밖의 cam3 1대 검출도 이번 tick 의 반증이다. 후보 cam4 는 주인 이력이 있다."""
    m = wt._manager(split_suppress_ownerless=ownerless)
    original = wt._walk(m, rig, 3, x0=3.0, speed=0.0)[-1].tracks[0].world_id
    t = T + 3 * DT
    views = (wt._view(rig, "cam3", "cam3-new", t, 3.0, 3.45),
             wt._view(rig, "cam4", "cam4-p", t, 3.0, 3.45))
    candidate = wt.ViewCluster("b", views, wt.triangulate_skeleton([v.view for v in views], rig))
    clusters = [
        wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")),
        candidate,
        wt._cluster(rig, "a-single", t, 3.0, 3.0, cams=("cam3",)),
    ]
    frame = m.update(wt._frame(rig, t, list(reversed(clusters)) if reverse else clusters), rig)
    assert len(frame.tracks) == 2 and all(tr.updated for tr in frame.tracks)
    assert frame.track(original).association_id == "a"
    assert frozenset(m.issuer.issued) in m._distinct
    assert m.diag["new_track.split_remainder_suppressed"] == 0


@pytest.mark.parametrize("inherited", [False, True])
@pytest.mark.parametrize("ownerless", [False, True])
def test_existing_distinct_pair_vetoes_suppression_of_new_id(rig, inherited, ownerless):
    """서로 다른 주인의 키가 섞여 같은 ID 복귀는 금지여도 반증은 새 ID 억제에 쓴다. 병합 상속도 같은 규칙이다."""
    m = wt._manager(lost_after_ms=1000, split_suppress_ownerless=ownerless)
    for k in range(5):
        t = T + k * DT
        if inherited:
            # a 와 duplicate 는 0.8 m 에서 따로 등록한 뒤 가까워진다. b 의 반증은 duplicate 에만 있다.
            clusters = [
                wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")),
                wt._cluster(rig, "duplicate", t, 3.0, 3.8 - 0.1 * k, cams=("cam3", "cam4")),
                wt._cluster(rig, "b", t, 6.5, 5.5, cams=("cam3", "cam4"), prefix="b"),
            ]
        else:
            clusters = [
                wt._cluster(rig, "a", t, 3.0, 3.0),
                wt._cluster(rig, "b", t, 3.0, 3.45, prefix="b"),
            ]
        m.update(wt._frame(rig, t, clusters), rig)
    a, b = m.issuer.issued[0], m.issuer.issued[-1]
    if inherited:
        duplicate = m.issuer.issued[1]
        assert frozenset((duplicate, b)) in m._distinct
        assert frozenset((a, b)) not in m._distinct
    for k in range(5, 25):
        t = T + k * DT
        m.update(wt._frame(rig, t, [wt._cluster(rig, "a", t, 3.0, 3.0)]), rig)
    if inherited:
        assert [(r.previous_world_id, r.world_id) for r in m.revisions] == [(duplicate, a)]
    assert frozenset((a, b)) in m._distinct
    assert b in m.lost_candidates
    # b 는 소실 후보다. cam3 은 b, cam4 는 a 의 옛 키라 늦은 재연결은 불가하고 S3 에는 a 의 최근 이력이 있다.
    t = T + 25 * DT
    views = (wt._view(rig, "cam3", "cam3-b", t, 3.0, 3.45),
             wt._view(rig, "cam4", "cam4-p", t, 3.0, 3.45))
    back = wt.ViewCluster("back", views, wt.triangulate_skeleton([v.view for v in views], rig))
    before = len(m.issuer.issued)
    frame = m.update(wt._frame(rig, t, [
        wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")), back,
    ]), rig)
    assert len(frame.tracks) == 2 and all(tr.updated for tr in frame.tracks)
    assert len(m.issuer.issued) == before + 1
    assert frame.track(a).association_id == "a"
    assert frame.track(m.issuer.issued[-1]).association_id == "back"
    assert frame.reassociations == ()
    assert m.diag["reconnect_skip.holder_updated_now"] >= 1
    assert m.diag["new_track.split_remainder_suppressed"] == 0


def test_prediction_alone_does_not_suppress_nearby_new_person(rig):
    m = wt._manager(gate_base_m=0.05, gate_max_m=0.1)
    original = wt._walk(m, rig, 3, x0=3.0, speed=0.0)[-1].tracks[0]
    t = T + 3 * DT
    frame = m.update(wt._frame(rig, t, [
        wt._cluster(rig, "new", t, 3.0, 3.4, cams=("cam3", "cam4"), prefix="new"),
    ]), rig)
    assert len(m.issuer.issued) == 2
    assert not frame.track(original.world_id).updated
    assert m.diag["new_track.split_remainder_suppressed"] == 0


@pytest.mark.parametrize("window", [1000, 2000])
@pytest.mark.parametrize("extra_ms,same_id", [(0, True), (1, False)])
def test_short_reconnection_window_boundary_even_without_empty_ticks(rig, window, extra_ms, same_id):
    m = wt._manager(lost_after_ms=window)
    original = wt._walk(m, rig, 3, x0=3.0, speed=0.0)[-1].tracks[0]
    t = original.t_last_valid + window + extra_ms
    frame = m.update(wt._frame(rig, t, [
        wt._cluster(rig, "back", t, 3.0, 3.0, prefix="new"),
    ]), rig)
    updated = [tr for tr in frame.tracks if tr.updated]
    assert len(updated) == 1
    assert (updated[0].world_id == original.world_id) is same_id
    assert len(m.issuer.issued) == (1 if same_id else 2)


@pytest.mark.parametrize("window", [1000, 2000])
def test_id_retention_does_not_extend_observation_or_create_joints(rig, window):
    m = wt._manager(lost_after_ms=window)
    original = wt._walk(m, rig, 3, x0=3.0, speed=0.0)[-1].tracks[0]
    for gap, state in ((300, "PREDICTED"), (600, "UNKNOWN"), (window, "UNKNOWN")):
        t = original.t_last_valid + gap
        tr = m.update(wt._frame(rig, t, []), rig).track(original.world_id)
        assert tr.lifecycle is TrackLifecycle.OCCLUDED
        assert tr.t_last_valid == original.t_last_valid and not tr.updated
        assert tr.pelvis_xyz is None and tr.skeleton is None and tr.cameras == ()
        assert tr.position_xy == original.position_xy
        assert world_track_document(tr, t)["observation_state"] == state
    t += 1
    assert m.update(wt._frame(rig, t, []), rig).track(original.world_id).lifecycle is TrackLifecycle.LOST


def test_single_view_does_not_register_a_world_track(rig):
    m = wt._manager()
    frame = m.update(wt._frame(rig, T, [wt._cluster(rig, "one", T, 3.0, 3.0, cams=("cam1",))]), rig)
    assert frame.tracks == () and m.issuer.issued == ()
