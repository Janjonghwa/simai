"""늦은 재연결과 ID 교정 기록 시험 (명세 5.2 World Track, 6.4, 15.1 G3, 부록 D-G3-02·D-G3-10, S15P31A101-40).

합성 카메라·골격·클러스터 도구는 test_world_track.py 의 것을 그대로 쓴다. 근거는 로컬 트랙 연결 이력과 위치뿐이다
(외형·얼굴·재식별 없음). 로컬 트래커는 같은 ID 를 다른 사람 검출에 붙일 수 있으므로(ID 전환) 카메라 한 대의 옛 로컬
트랙은 근거로 세지 않는다(리뷰 F1). 모든 입력은 손으로 만든 SIM 이다.
"""

import importlib.util
from pathlib import Path

import pytest

from simai_ai.geometry.triangulation import triangulate_skeleton
from simai_ai.risk import (
    AlertClear,
    Change,
    Evaluation,
    Finding,
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    Origin,
    Severity,
    WorldState,
)
from simai_ai.tracking.cross_view import ViewCluster
from simai_ai.tracking.world_state import world_state_message, world_state_payload
from simai_ai.tracking.world_track import (
    IdentityConfidence,
    IdentityFlag,
    IdentityRevision,
    RevisionKind,
    RevisionReason,
    TrackLifecycle,
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
)

_spec = importlib.util.spec_from_file_location("_wt_helpers", Path(__file__).with_name("test_world_track.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

T, DT, ALL = wt.T, wt.DT, wt.ALL
MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
UNCERTAIN = IdentityConfidence.IDENTITY_UNCERTAIN


@pytest.fixture(scope="module")
def rig():
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def _manager(**cfg):
    return WorldTrackManager(WorldIdIssuer("test"), WorldTrackConfig(**cfg))


def _mixed(rig, name, t, x, y, ids, uncertain=False):
    """카메라 → 로컬 트랙 ID 를 직접 준 클러스터 (로컬 트래커가 일부 카메라만 옛 ID 를 이어 간 경우)."""
    views = tuple(wt._view(rig, cam, lid, t, x, y) for cam, lid in sorted(ids.items()))
    skeleton = triangulate_skeleton([v.view for v in views], rig) if len(views) >= 2 else None
    reasons = (wt.UncertainReason.AMBIGUOUS_ASSIGNMENT,) if uncertain else ()
    return ViewCluster(name, views, skeleton, reasons)


def _at(m, rig, t, clusters):
    return m.update(wt._frame(rig, t, [c(t) if callable(c) else c for c in clusters]), rig)


def _tick(m, rig, k, clusters):
    return _at(m, rig, T + k * DT, clusters)


def _empty(m, rig, start, stop):
    return [_tick(m, rig, k, []) for k in range(start, stop)]


def _ids(prefix, cams=ALL):
    return {c: f"{c}-{prefix}" for c in cams}


def _seen_then_lost(m, rig, x0=3.0, stop=45):
    """사람 X 를 (x0, 3.0) 에서 1 s 보고(로컬 트랙 cam1~4-p) stop 까지 모든 카메라에서 가린다 (3 s 넘으면 LOST)."""
    frames = wt._walk(m, rig, 10, speed=0.0, x0=x0)
    x = frames[-1].tracks[0].world_id
    _empty(m, rig, 10, stop)
    return x


# 옛 로컬 트랙 두 대(cam1·cam2) + 새 로컬 트랙 두 대
TWO_OLD = {"cam1": "cam1-p", "cam2": "cam2-p", "cam3": "cam3-q", "cam4": "cam4-q"}
# 옛 로컬 트랙 한 대(cam1) + 새 로컬 트랙 세 대 (리뷰 F1: 다른 사람에게 ID 가 넘어간 경우와 구별할 수 없다)
ONE_OLD = {"cam1": "cam1-p", "cam2": "cam2-q", "cam3": "cam3-q", "cam4": "cam4-q"}


# ---------------------------------------------------------------- 같은 ID 로 잇기


def test_short_occlusion_reappearance_keeps_id_without_revision(rig):
    """짧은 가림(1 s, 소실 3 s 전) 뒤 같은 로컬 트랙으로 다시 보이면 같은 ID 다. 교정·늦은 재연결 기록은 없다."""
    m = _manager()
    frames = wt._walk(m, rig, 10)
    wid = frames[-1].tracks[0].world_id
    hidden = _empty(m, rig, 10, 20)
    assert hidden[-1].track(wid).lifecycle is TrackLifecycle.OCCLUDED
    back = wt._walk(m, rig, 3, start=20)
    assert [f.tracks[0].world_id for f in back] == [wid] * 3
    assert back[0].tracks[0].lifecycle is TrackLifecycle.CONFIRMED
    assert all(f.revisions == () and f.reassociations == () for f in back)
    assert back[-1].tracks[0].identity_revision == 0
    assert m.issuer.issued == (wid,)


def test_lost_track_reconnects_same_id_with_two_camera_history(rig):
    """소실(3 s) 뒤 두 카메라의 로컬 트래커가 이어 온 로컬 트랙과 함께 다시 보이면 같은 World ID 로 잇는다.
    일관성 확인(자기 로컬 트랙만으로 3시각 갱신)이 끝날 때까지 IDENTITY_UNCERTAIN 이다."""
    m = _manager()
    wid = _seen_then_lost(m, rig)
    assert m.lost_candidates == (wid,)
    f = _tick(m, rig, 45, [lambda t: _mixed(rig, "xv-5", t, 4.2, 3.0, TWO_OLD)])  # 1.2 m 옮겨 감
    tr = f.track(wid)
    assert tr is not None and tr.updated and len(f.tracks) == 1
    assert tr.lifecycle is TrackLifecycle.CONFIRMED
    assert f.reassociations == (wid,) and f.revisions == ()
    assert tr.identity_revision == 0 and tr.aliases == ()  # ID 가 바뀌지 않았으므로 교정이 아니다
    assert tr.identity_confidence is UNCERTAIN and IdentityFlag.LATE_REASSOCIATION in tr.identity_flags
    assert m.issuer.issued == (wid,) and m.lost_candidates == ()
    later = [_tick(m, rig, k, [lambda t: _mixed(rig, "xv-5", t, 4.2, 3.0, TWO_OLD)]) for k in range(46, 70)]
    assert all(f.track(wid).identity_confidence is UNCERTAIN for f in later[:3])
    assert later[-1].track(wid).identity_confidence is IdentityConfidence.HIGH  # 확인을 마친 뒤 유지 시간이 지났다


def test_reconnected_track_stays_uncertain_until_consistency_is_observed(rig):
    """다시 이은 뒤 곧바로 다시 가려지면 시간이 지나도 LATE_REASSOCIATION 을 풀지 않는다 (1 s 자동 해제 없음)."""
    m = _manager()
    wid = _seen_then_lost(m, rig)
    _tick(m, rig, 45, [lambda t: _mixed(rig, "xv-5", t, 4.2, 3.0, TWO_OLD)])
    hidden = _empty(m, rig, 46, 70)  # 2.4 s 가림 (소실 3 s 전)
    assert all(f.track(wid).identity_confidence is UNCERTAIN for f in hidden)
    assert IdentityFlag.LATE_REASSOCIATION in hidden[-1].track(wid).identity_flags


def test_single_camera_history_does_not_restore_identity(rig):
    """2대 미만(cam1 만)으로 4 s 보이는 동안 LOST 가 됐다가 cam1 로컬 트랙과 새 로컬 트랙으로 다시 보이면, cam1 하나로는
    같은 사람이라 볼 수 없어 새 ID 다. 옛 Track 이 소실 후보로 남는 동안 새 ID 는 IDENTITY_UNCERTAIN 이다.
    두 번째 카메라(cam2)도 옛 로컬 트랙을 되살리면 같은 ID 로 잇는다."""
    for revived, same in ((ONE_OLD, False), (TWO_OLD, True)):
        m = _manager()
        frames = wt._walk(m, rig, 10, speed=0.0, x0=3.0)
        wid = frames[-1].tracks[0].world_id
        for k in range(10, 50):  # 걸어가며 cam1 에만 보인다: 위치는 갱신하지 않는다(2대 미만은 지어내지 않음)
            f = _tick(m, rig, k, [lambda t, k=k: wt._cluster(rig, "xv-1", t, 3.0 + 0.05 * (k - 10), 3.0, cams=("cam1",))])
            assert all(tr.pelvis_xyz is None for tr in f.tracks)
        assert m.lost_candidates == (wid,)
        f = _tick(m, rig, 50, [lambda t, ids=revived: _mixed(rig, "xv-7", t, 5.0, 3.0, ids)])
        (tr,) = f.tracks
        assert (tr.world_id == wid) is same and (f.reassociations == (wid,)) is same
        assert tr.identity_confidence is UNCERTAIN


def test_switched_local_id_does_not_restore_lost_identity(rig):
    """리뷰 F1 장면: A 가 3.5 s 가려진 뒤 다른 사람 B 가 (4.2, 3.0) 에 나타나고 cam1 로컬 트래커만 A 의 옛 ID 를 B 에게
    붙였다. B 는 새 ID 이고, A 가 소실 후보로 남는 동안(마지막 관측 + 10 s) IDENTITY_UNCERTAIN 이 풀리지 않는다.
    교정(병합)도 없다."""
    m = _manager()
    a = _seen_then_lost(m, rig)
    frames = [_tick(m, rig, k, [lambda t: _mixed(rig, "B", t, 4.2, 3.0, ONE_OLD)]) for k in range(45, 125)]
    b = frames[0].tracks[0].world_id
    assert b != a and all([tr.world_id for tr in f.tracks] == [b] for f in frames)
    assert all(f.revisions == () and f.reassociations == () for f in frames)
    # A 의 소실 후보는 T+900+10000 (k=109) 까지. 그동안 B 는 A 일 수도 있어 IDENTITY_UNCERTAIN 이다
    until = (900 + 10_000) // DT
    for f in frames:
        k = (f.t - T) // DT
        if k <= until:
            assert f.track(b).identity_confidence is UNCERTAIN, k
            assert IdentityFlag.REASSOCIATION_AMBIGUOUS in f.track(b).identity_flags
    assert frames[-1].track(b).identity_confidence is IdentityConfidence.HIGH  # 후보가 끝나고 유지 시간이 지났다
    assert m.revisions == ()


def test_no_local_track_history_long_loss_gets_new_id(rig):
    """소실 뒤 로컬 트랙 근거 없이(모두 새 로컬 트랙) 가까이 나타나면 위치만으로 잇지 않고 새 ID 다."""
    m = _manager()
    wid = _seen_then_lost(m, rig)
    f = _tick(m, rig, 45, [lambda t: wt._cluster(rig, "xv-9", t, 3.0, 3.0, prefix="q")])
    (tr,) = f.tracks
    assert tr.world_id != wid and tr.lifecycle is TrackLifecycle.TENTATIVE
    assert f.reassociations == () and f.revisions == ()
    assert IdentityFlag.REASSOCIATION_AMBIGUOUS not in tr.identity_flags


@pytest.mark.parametrize("gap_ms, same", [(9999, True), (10_000, True), (10_001, False)])
def test_reconnect_window_boundary(rig, gap_ms, same):
    """소실 후보는 마지막 관측부터 reconnect_window_ms(10 s) 까지다 (넘으면 같은 로컬 트랙이라도 새 ID)."""
    m = _manager()
    wid = _seen_then_lost(m, rig)
    t = T + 900 + gap_ms
    f = _at(m, rig, t, [lambda t: _mixed(rig, "back", t, 3.0, 3.0, TWO_OLD)])
    assert (f.tracks[0].world_id == wid) is same


def test_reconnect_window_config(rig):
    m = _manager(reconnect_window_ms=3000)  # lost_after_ms 와 같으면 늦은 재연결을 끈다
    _seen_then_lost(m, rig)
    assert m.lost_candidates == ()
    for bad in ({"reconnect_window_ms": 2000}, {"merge_confirm_frames": 0}, {"reconnect_min_cameras": 1}):
        with pytest.raises(ValueError):
            WorldTrackConfig(**bad)


def test_far_beyond_reach_is_not_reconnected(rig):
    """공백 동안 사람이 갈 수 있는 거리(0.5 m + 2 m/s × 공백) 밖이면 로컬 트랙 근거가 있어도 잇지 않는다."""
    m = _manager()
    frames = wt._walk(m, rig, 10, speed=0.0, x0=1.5)
    wid = frames[-1].tracks[0].world_id
    _empty(m, rig, 10, 12)  # 0.2 s 공백: 갈 수 있는 거리 0.9 m (위치 게이트 0.9 m 도 넘는다)
    f = _tick(m, rig, 12, [lambda t: _mixed(rig, "xv-2", t, 6.0, 3.0, _ids("p"))])
    new = [tr for tr in f.tracks if tr.world_id != wid]
    assert len(new) == 1 and IdentityFlag.REASSOCIATION_AMBIGUOUS in new[0].identity_flags
    assert f.track(wid).association_id is None


# ---------------------------------------------------------------- 애매함: 잇지 않는다


def _two_then_lost(m, rig, ya=3.0, yb=3.6):
    for k in range(10):
        _tick(m, rig, k, [lambda t: wt._cluster(rig, "xv-1", t, 3.0, ya, prefix="a"),
                          lambda t: wt._cluster(rig, "xv-2", t, 3.0, yb, prefix="b")])  # fmt: skip
    _empty(m, rig, 10, 45)
    assert len(m.lost_candidates) == 2
    return "w-test-0001", "w-test-0002"


def test_two_people_crossing_then_lost_are_not_reconnected(rig):
    """가까이(0.6 m) 있던 두 사람이 함께 사라진 뒤 각자 옛 로컬 트랙(두 대씩)을 맞바꿔 들고 다시 보이면 로컬 트랙이 서로
    넘어갔을 수 있다(교차 중 로컬 트래커 ID 전환). 잇지 않고 새 ID 둘에 REASSOCIATION_AMBIGUOUS 를 붙인다."""
    m = _manager()
    a, b = _two_then_lost(m, rig)
    clusters = [
        lambda t: _mixed(rig, "xv-3", t, 3.0, 3.6, {"cam1": "cam1-a", "cam2": "cam2-a", "cam3": "cam3-x"}),
        lambda t: _mixed(rig, "xv-4", t, 3.0, 3.0, {"cam1": "cam1-b", "cam2": "cam2-b", "cam3": "cam3-y"}),
    ]
    frames = [_tick(m, rig, k, clusters) for k in range(45, 52)]
    first = frames[0]
    assert {tr.world_id for tr in first.tracks}.isdisjoint({a, b}) and len(first.tracks) == 2
    assert all(IdentityFlag.REASSOCIATION_AMBIGUOUS in tr.identity_flags for tr in first.tracks)
    assert all(tr.identity_confidence is UNCERTAIN for f in frames for tr in f.tracks)
    assert all(f.revisions == () and f.reassociations == () for f in frames)
    assert m.revisions == ()


@pytest.mark.parametrize("d, same", [(1.499, False), (1.501, True)])
def test_crowd_radius_boundary(rig, d, same):
    """잇는 Track 의 마지막 위치 reconnect_crowd_radius_m(1.5 m) 안에 다른 소실 Track 이 있으면 잇지 않는다."""
    m = _manager()
    a, _ = _two_then_lost(m, rig, 3.0, 3.0 + d)
    f = _tick(m, rig, 45, [lambda t: _mixed(rig, "back", t, 3.0, 3.0, {"cam1": "cam1-a", "cam2": "cam2-a", "cam3": "cam3-q"})])
    assert (f.tracks[0].world_id == a) is same


def test_cluster_with_local_tracks_of_two_lost_tracks_is_ambiguous(rig):
    """한 클러스터에 두 소실 Track 의 로컬 트랙이 섞이면 어느 쪽에도 잇지 않는다 (멀리 떨어져 사라진 두 사람이라도)."""
    m = _manager()
    a, b = _two_then_lost(m, rig, 2.0, 5.0)
    mixed = {"cam1": "cam1-a", "cam2": "cam2-a", "cam3": "cam3-b", "cam4": "cam4-b"}
    f = _tick(m, rig, 45, [lambda t: _mixed(rig, "xv-3", t, 3.0, 3.5, mixed)])
    (tr,) = f.tracks
    assert tr.world_id not in (a, b) and IdentityFlag.REASSOCIATION_AMBIGUOUS in tr.identity_flags
    assert f.reassociations == () and f.revisions == ()


def test_uncertain_cross_view_cluster_is_not_reconnected(rig):
    m = _manager()
    wid = _seen_then_lost(m, rig)
    f = _tick(m, rig, 45, [lambda t: _mixed(rig, "xv-3", t, 3.0, 3.0, _ids("p"), uncertain=True)])
    assert [tr.world_id for tr in f.tracks] != [wid] and f.reassociations == ()


# ---------------------------------------------------------------- 병합 (ID 교정)

NEW_IDS = {"cam3": "cam3-q", "cam4": "cam4-q"}
REVIVED = dict(NEW_IDS, cam1="cam1-p", cam2="cam2-p")


def _lost_then_new_id(m, rig):
    """X 가 사라져(3.5 s) LOST 가 된 뒤 새 로컬 트랙(cam3·cam4)으로 나타나 새 ID Y 를 받는다."""
    x = _seen_then_lost(m, rig)
    frames = [_tick(m, rig, k, [lambda t: _mixed(rig, "xv-5", t, 4.0, 3.0, NEW_IDS)]) for k in range(45, 50)]
    (y,) = (tr.world_id for tr in frames[-1].tracks)
    assert y != x and frames[-1].track(y).lifecycle is TrackLifecycle.CONFIRMED
    return x, y


def _feed(m, rig, ks, ids):
    return [_tick(m, rig, k, [lambda t, ids=ids: _mixed(rig, "xv-5", t, 4.0, 3.0, ids)]) for k in ks]


def test_long_loss_new_id_then_revived_local_tracks_merge_with_revision(rig, contract_errors):
    """긴 소실 뒤 새 ID 를 받았는데 두 카메라의 로컬 트래커가 옛 로컬 트랙을 되살리면 근거 3시각 뒤 먼저 만든 ID 로
    교정한다."""
    m = _manager()
    x, y = _lost_then_new_id(m, rig)
    frames = _feed(m, rig, range(50, 56), REVIVED)
    assert frames[0].revisions == () and frames[1].revisions == ()  # 한두 시각으로는 합치지 않는다
    (rev,) = frames[2].revisions
    assert rev == IdentityRevision(
        t=T + 52 * DT,
        kind=RevisionKind.MERGE,
        world_id=x,
        previous_world_id=y,
        identity_revision=1,
        reason=str(RevisionReason.LATE_REASSOCIATION),
        evidence=(("cam1", "cam1-p"), ("cam2", "cam2-p")),
    )
    merged = frames[2].track(x)
    assert [tr.world_id for tr in frames[2].tracks] == [x]  # Y 는 별칭이 되어 사라진다
    assert merged.updated and merged.lifecycle is TrackLifecycle.CONFIRMED
    assert merged.identity_revision == 1 and merged.aliases == (y,)
    assert merged.identity_confidence is UNCERTAIN and IdentityFlag.LATE_REASSOCIATION in merged.identity_flags
    assert merged.position_xy == pytest.approx((4.0, 3.0), abs=0.05)  # 위치는 이번 시각 측정을 받은 Y 의 것
    assert m.revisions == (rev,) and m.issuer.issued == (x, y)  # 기록은 추가만, ID 는 다시 내지 않는다
    assert all(f.revisions == () for f in frames[3:]) and frames[-1].track(x).identity_revision == 1
    assert rev.to_correction().previous_world_id == y and rev.to_correction().world_id == x
    assert rev.to_payload()["evidence"] == [["cam1", "cam1-p"], ["cam2", "cam2-p"]]
    for f in frames:
        msg = world_state_message(world_state_payload(f, f.t, "scene-1"), msg_id=MSG_ID, sent_at=f.t)
        assert contract_errors("world_state", msg) == []
        assert all(doc["identity_revision"] == (1 if f.t >= rev.t else 0) for doc in msg["payload"]["tracks"])


def test_single_revived_local_track_never_merges_and_keeps_uncertainty(rig):
    """옛 로컬 트랙이 한 대(cam1)에서만 되살아나면 몇 시각이 지나도 합치지 않고, Y 는 X 일 수도 있어 IDENTITY_UNCERTAIN."""
    m = _manager()
    x, y = _lost_then_new_id(m, rig)
    frames = _feed(m, rig, range(50, 75), dict(NEW_IDS, cam1="cam1-p"))
    assert m.revisions == () and all([tr.world_id for tr in f.tracks] == [y] for f in frames)
    assert all(f.track(y).identity_confidence is UNCERTAIN for f in frames)
    # 두 번째 카메라가 되살아나면 넘겨받은 cam1 과 함께 근거가 된다(3시각 뒤 교정)
    frames = _feed(m, rig, range(75, 78), REVIVED)
    assert [len(f.revisions) for f in frames] == [0, 0, 1]


@pytest.mark.parametrize("gap_ms, merged", [(1999, True), (2000, True), (2001, False), (2100, False)])
def test_merge_evidence_ttl_boundary(rig, gap_ms, merged):
    """리뷰 F3: 근거 두 시각 뒤 근거 로컬 트랙이 빠졌다가 돌아올 때, 마지막 근거로부터 key_ttl_ms(2 s) 안이면 이어서 세고
    넘으면 처음부터 센다(돌아온 시각에 합치기 전에 TTL 을 본다)."""
    m = _manager()
    _lost_then_new_id(m, rig)
    _feed(m, rig, (50, 51), REVIVED)
    last = T + 51 * DT
    for t in range(last + DT, last + gap_ms, DT):  # 10 Hz 로 Y 만 보인다
        _at(m, rig, t, [lambda t: _mixed(rig, "xv-5", t, 4.0, 3.0, NEW_IDS)])
    f = _at(m, rig, last + gap_ms, [lambda t: _mixed(rig, "xv-5", t, 4.0, 3.0, REVIVED)])
    assert (len(f.revisions) == 1) is merged


def test_merge_evidence_tolerates_short_gap_but_resets_on_counter_evidence(rig):
    m = _manager()
    _lost_then_new_id(m, rig)
    seq = [REVIVED, REVIVED, NEW_IDS, REVIVED]  # 근거 로컬 트랙이 한 시각 빠져도(세지 않음) 3번째 근거에서 합친다
    frames = [_feed(m, rig, [50 + i], ids)[0] for i, ids in enumerate(seq)]
    assert [len(f.revisions) for f in frames] == [0, 0, 0, 1]

    m = _manager()
    _lost_then_new_id(m, rig)
    _feed(m, rig, [50], REVIVED)
    # X 의 다른 로컬 트랙(cam4-p)이 멀리 다른 사람 클러스터에 보인다: 반대 근거
    _tick(m, rig, 51, [lambda t: _mixed(rig, "xv-5", t, 4.0, 3.0, REVIVED),
                       lambda t: _mixed(rig, "xv-6", t, 6.5, 5.5, {"cam1": "cam1-zz", "cam2": "cam2-zz", "cam4": "cam4-p"})])  # fmt: skip
    frames = _feed(m, rig, range(52, 60), REVIVED)
    assert all(f.revisions == () for f in frames) and m.revisions == ()


def test_split_person_rejoined_merges_into_first_id(rig):
    """Cross-view 가 한 사람을 두 클러스터로 갈라 두 ID 가 생겼다가(POSSIBLE_DUPLICATE) 다시 한 클러스터로 묶이면,
    근거 3시각 뒤 먼저 만든 ID 로 합친다(두 클러스터가 같은 카메라를 쓴 적이 없다)."""
    # 기존 중복 ID 병합 경로의 기준선 (S3·-657 그림자 억제·동시 활성 병합은 끈다)
    m = _manager(split_suppression_enabled=False, shadow_suppression_enabled=False, active_merge_enabled=False)
    frames = wt._split_ticks(m, rig, 0, 15)
    a, b = sorted(tr.world_id for tr in frames[-1].tracks)
    rejoined = [_tick(m, rig, k, [lambda t: wt._cluster(rig, "xv-3", t, 3.0, 3.0)]) for k in range(15, 20)]
    assert [len(f.revisions) for f in rejoined] == [0, 0, 1, 0, 0]
    (rev,) = rejoined[2].revisions
    assert (rev.world_id, rev.previous_world_id, rev.identity_revision) == (a, b, 1)
    assert rev.reason == RevisionReason.DUPLICATE_MERGE
    assert set(rev.evidence) == {("cam3", "cam3-p"), ("cam4", "cam4-p")}
    assert [tr.world_id for tr in rejoined[-1].tracks] == [a]
    assert rejoined[-1].track(a).aliases == (b,)


def test_chained_merges_accumulate_revision_and_aliases(rig):
    """X←Y 교정 뒤 다시 사라졌다가 새 ID Z 를 받고 X←Z 로 교정되면 identity_revision 2, 별칭 (Y, Z), 앞 기록은 그대로다."""
    m = _manager()
    x, y = _lost_then_new_id(m, rig)
    _feed(m, rig, range(50, 61), REVIVED)
    first = m.revisions
    _empty(m, rig, 61, 96)
    ids2 = {"cam3": "cam3-r", "cam4": "cam4-r"}
    frames = _feed(m, rig, range(96, 101), ids2)
    z = frames[-1].tracks[0].world_id
    frames = _feed(m, rig, range(101, 104), dict(ids2, cam1="cam1-p", cam2="cam2-p"))
    assert [(r.previous_world_id, r.world_id, r.identity_revision) for r in m.revisions] == [(y, x, 1), (z, x, 2)]
    assert m.revisions[:1] == first
    assert frames[-1].track(x).aliases == (y, z)


def test_two_live_tracks_claiming_one_lost_track_do_not_merge(rig):
    """두 Track 이 한 소실 Track 의 서로 다른 옛 로컬 트랙을 동시에 가져가면 어느 쪽도 합치지 않는다."""
    m = _manager()
    x = _seen_then_lost(m, rig)
    for k in range(45, 50):
        _tick(m, rig, k, [lambda t: wt._cluster(rig, "B", t, 3.0, 3.0, prefix="b"),
                          lambda t: wt._cluster(rig, "C", t, 5.0, 3.0, prefix="c")])  # fmt: skip
    for k in range(50, 56):
        f = _tick(m, rig, k, [
            lambda t: _mixed(rig, "B", t, 3.0, 3.0, {"cam1": "cam1-p", "cam2": "cam2-p", "cam3": "cam3-b"}),
            lambda t: _mixed(rig, "C", t, 5.0, 3.0, {"cam3": "cam3-p", "cam4": "cam4-p", "cam1": "cam1-c"}),
        ])  # fmt: skip
    assert m.revisions == () and x not in {tr.world_id for tr in f.tracks} and len(f.tracks) == 2


# ---------------------------------------------------------------- 병합 금지: 같은 시각 같은 카메라의 두 검출


def test_two_people_seen_together_are_never_merged(rig):
    """같은 시각 같은 카메라의 다른 검출로 따로 보인 두 Track 은 서로 다른 사람이다. B 가 가려진 뒤 A 의 클러스터에 B 의
    로컬 트랙들이 계속 섞여도(로컬 트래커 ID 전환) 합치지 않는다."""
    m = _manager()
    a, b = wt._two_tracks(m, rig, 3.0, 3.3, ticks=5)
    for k in range(5, 15):
        ids = {"cam1": "cam1-b", "cam2": "cam2-b", "cam3": "cam3-a", "cam4": "cam4-a"}
        f = _tick(m, rig, k, [lambda t: _mixed(rig, "xv-1", t, 3.0, 3.0, ids)])
        assert f.revisions == ()
    assert m.revisions == () and {tr.world_id for tr in f.tracks} == {a, b}


def _apart(m, rig, ticks=5):
    """A(cam1·cam2, 로컬 a)와 B(cam3·cam4, 로컬 b)가 같은 카메라에 함께 보인 적 없이 따로 확정된다."""
    for k in range(ticks):
        _tick(m, rig, k, [lambda t: wt._cluster(rig, "A", t, 3.0, 3.0, cams=("cam1", "cam2"), prefix="a"),
                          lambda t: wt._cluster(rig, "B", t, 3.0, 3.45, cams=("cam3", "cam4"), prefix="b")])  # fmt: skip
    return "w-test-0001", "w-test-0002"


def test_single_camera_detection_records_distinct_pair(rig):
    """리뷰 F2 장면: B 가 cam3 1대로만(위치 갱신 없이) A 의 cam3 검출과 같은 시각에 보였다. 그 반증을 남겨, 뒤에 B 가
    가려지고 A 의 클러스터에 B 의 로컬 트랙(두 대)이 넘어가도 합치지 않는다."""
    m = _manager()
    a, b = _apart(m, rig)
    f = _tick(m, rig, 5, [lambda t: _mixed(rig, "A", t, 3.0, 3.0, {"cam1": "cam1-a", "cam2": "cam2-a", "cam3": "cam3-a"}),
                          lambda t: wt._cluster(rig, "B", t, 3.0, 3.45, cams=("cam3",), prefix="b")])  # fmt: skip
    assert f.track(a).updated and not f.track(b).updated
    for k in range(6, 20):
        f = _tick(m, rig, k, [lambda t: _mixed(rig, "A", t, 3.0, 3.0,
                                                {"cam1": "cam1-a", "cam2": "cam2-a", "cam3": "cam3-b", "cam4": "cam4-b"})])  # fmt: skip
        assert f.revisions == ()
    assert m.revisions == () and {tr.world_id for tr in f.tracks} == {a, b}


def test_occluded_track_detection_records_distinct_pair(rig):
    """가려진(OCCLUDED) B 의 로컬 트랙이 1대 클러스터로 A 와 같은 카메라에 보인 시각도 반증이다."""
    m = _manager()
    a, b = _apart(m, rig)
    for k in range(5, 10):  # B 가림, A 만 보임
        _tick(m, rig, k, [lambda t: wt._cluster(rig, "A", t, 3.0, 3.0, cams=("cam1", "cam2"), prefix="a")])
    f = _tick(m, rig, 10, [lambda t: wt._cluster(rig, "A", t, 3.0, 3.0, cams=("cam1", "cam2", "cam3"), prefix="a"),
                           lambda t: wt._cluster(rig, "B", t, 3.0, 3.45, cams=("cam3",), prefix="b")])  # fmt: skip
    assert f.track(b).lifecycle is TrackLifecycle.OCCLUDED
    for k in range(11, 25):
        f = _tick(m, rig, k, [lambda t: _mixed(rig, "A", t, 3.0, 3.0,
                                                {"cam1": "cam1-a", "cam2": "cam2-a", "cam3": "cam3-b", "cam4": "cam4-b"})])  # fmt: skip
    assert m.revisions == ()


def test_distinct_pair_is_inherited_through_merge(rig):
    """병합으로 사라진 ID 의 병합 금지 쌍은 남는 ID 로 이어진다(별칭·연쇄 병합). b 와 함께 보였던 W 는 b 를 합친 a 와도
    합치지 않는다."""
    m = _manager()
    for k in range(5):  # 한 사람을 a(cam1·2)·b(cam3·4) 로 가름, W 는 멀리 cam3·4 (b 와 같은 카메라에 함께 보임)
        _tick(m, rig, k, [lambda t: wt._cluster(rig, "a", t, 3.0, 3.0, cams=("cam1", "cam2")),
                          lambda t: wt._cluster(rig, "b", t, 3.0, 3.0, cams=("cam3", "cam4")),
                          lambda t: wt._cluster(rig, "W", t, 6.5, 5.5, cams=("cam3", "cam4"), prefix="w")])  # fmt: skip
    a, b, w = m.issuer.issued
    frames = [_tick(m, rig, k, [lambda t: wt._cluster(rig, "ab", t, 3.0, 3.0)]) for k in range(5, 30)]  # W 가림
    assert [(r.previous_world_id, r.world_id) for r in m.revisions] == [(b, a)]
    for k in range(30, 45):  # a 의 클러스터에 W 의 옛 로컬 트랙 두 대
        f = _tick(m, rig, k, [lambda t: _mixed(rig, "ab", t, 3.0, 3.0,
                                                {"cam1": "cam1-p", "cam2": "cam2-p", "cam3": "cam3-w", "cam4": "cam4-w"})])  # fmt: skip
        assert f.revisions == ()
    assert len(m.revisions) == 1 and len(frames) == 25
    assert frozenset((a, w)) in m._distinct  # b 의 쌍이 a 로 이어졌다


def test_local_id_switch_neighbour_still_visible_is_not_merged(rig):
    """옛 주인 B 가 다른 카메라(1대 클러스터)로 계속 보이면 A 에 넘어간 B 의 로컬 트랙은 근거가 아니다(-37 리뷰 F1)."""
    m = _manager()
    a, b = _apart(m, rig)
    for k in range(5, 12):
        f = _tick(m, rig, k, [
            lambda t: _mixed(rig, "xv-1", t, 3.0, 3.0, {"cam1": "cam1-a", "cam2": "cam2-a", "cam3": "cam3-b"}),
            lambda t: wt._cluster(rig, "xv-2", t, 3.0, 3.45, cams=("cam4",), prefix="b"),
        ])  # fmt: skip
        assert f.revisions == ()
    assert {tr.world_id for tr in f.tracks} == {a, b}


def test_revision_correction_only_for_merge():
    rev = IdentityRevision(T, RevisionKind.SPLIT, "w-test-0002", "w-test-0001", 1, "x")
    with pytest.raises(ValueError):
        rev.to_correction()


# ---------------------------------------------------------------- G3: 교정 후 진행 중 위험 자동 해소 0건


class _RiskLoop:
    """World Track 결과 → world_state → Risk 생명주기. inside(frame, track) 인 Track 에 R1 판정을 낸다."""

    def __init__(self, inside=lambda f, tr: tr.updated):
        self.clock = FixedClock(T)
        self.tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=self.clock)
        self.inside = inside
        self.transitions = []
        self.signals = []

    def feed(self, frame):
        # 교정을 판정 반영 전에 넘긴다 (world_track 모듈 설명의 Risk 전달 순서)
        for rev in frame.revisions:
            self.transitions += self.tracker.correct_identity(rev.to_correction()).transitions
        world = WorldState.from_payload(world_state_payload(frame, frame.t, "scene-1"))
        findings = [
            Finding(
                rule_id="R1",
                rule_version=1,
                severity=Severity.HIGH,
                world_ids=(tr.world_id,),
                zone_id="z-01",
                reasons=("xy_inside_polygon",),
            )
            for tr in frame.tracks
            if self.inside(frame, tr)
        ]
        self.clock.set(frame.t + 50)
        evaluation = Evaluation(
            origin=Origin.LIVE,
            evaluated_at=self.clock.now_ms(),
            observation_time=world.t_latest_global,
            calibration_version=world.calibration_version,
            scene_version=world.scene_version,
            rules=(),
            zones=(),
            findings=tuple(sorted(findings, key=lambda f: f.sort_key)),
        )
        step = self.tracker.apply(evaluation, world, config_version="cfg-1")
        self.transitions += step.transitions
        self.signals += step.local_signals

    def resolved(self):
        return [t for t in self.transitions if t.transition is Lifecycle.RESOLVED]


def test_switched_local_id_keeps_alarm_of_hidden_person(rig):
    """리뷰 F1 Risk 장면: 구역 안 A 의 R1 HIGH 가 진행 중일 때 A 는 계속 가려지고, 구역 밖 B 가 A 의 옛 로컬 ID(cam1)를
    달고 나타난다. A 의 신원이 B 로 복원되지 않으므로 A 의 경보는 해소되지 않는다(AlertClear 없음)."""
    m = _manager()
    loop = _RiskLoop(inside=lambda f, tr: tr.updated and tr.position_xy[0] < 3.5)
    for k in range(10):
        loop.feed(_tick(m, rig, k, [lambda t: wt._cluster(rig, "A-in-zone", t, 3.0, 3.0)]))
    (a_event,) = loop.tracker.active()
    for k in range(10, 45):
        loop.feed(_tick(m, rig, k, []))
    for k in range(45, 160):  # A 의 소실 후보가 끝난 뒤까지
        loop.feed(_tick(m, rig, k, [lambda t: _mixed(rig, "B-outside", t, 4.2, 3.0, ONE_OLD)]))
    assert loop.resolved() == [] and not any(isinstance(s, AlertClear) for s in loop.signals)
    assert a_event.event_id in {s.event_id for s in loop.tracker.active()}


def test_g3_merge_keeps_ongoing_risk_events(rig):
    """D-G3-10: X 의 위험 이벤트가 진행 중일 때 X 가 사라졌다가 새 ID Y 로 돌아와 Y 에도 이벤트가 생기고, Y 가 X 로
    교정돼도 관측으로 해소되는 이벤트는 없다. -657: 같은 사람의 같은 위험이 둘이 되므로 Y 의 이벤트는 X 의 이벤트에 귀속돼
    병합 흡수 마감(IDENTITY_MERGED)으로 닫히고(유령 카드 없음), 위험은 X 의 이벤트가 잇는다. 원본 World ID 는 남는다."""
    m = _manager()
    loop = _RiskLoop()
    for k in range(10):
        loop.feed(_tick(m, rig, k, [lambda t: wt._cluster(rig, "xv-1", t, 3.0, 3.0)]))
    (x_event,) = loop.tracker.active()
    x = x_event.world_ids[0]
    for k in range(10, 45):
        loop.feed(_tick(m, rig, k, []))
    for f in _feed(m, rig, range(45, 50), NEW_IDS):
        loop.feed(f)
    y_event = next(s for s in loop.tracker.active() if s.event_id != x_event.event_id)
    y = y_event.world_ids[0]
    frames = _feed(m, rig, range(50, 90), REVIVED)
    for f in frames:
        loop.feed(f)
    assert sum(len(f.revisions) for f in frames) == 1
    observed = [t for t in loop.resolved() if Change.IDENTITY_MERGED not in t.changes]
    assert observed == []  # 교정 후 진행 중 위험의 관측 자동 해소 0건
    (absorbed,) = [t for t in loop.transitions if Change.IDENTITY_MERGED in t.changes]
    assert absorbed.event_id == y_event.event_id and absorbed.transition is Lifecycle.RESOLVED
    assert absorbed.merge.survivor_event_id == x_event.event_id and absorbed.state.related_event_id == x_event.event_id
    assert absorbed.state.origin_world_ids == (y,)
    assert [s.event_id for s in loop.tracker.active()] == [x_event.event_id]
    survivor = loop.tracker.get(x_event.event_id)
    assert survivor.world_ids == (x,) and survivor.origin_world_ids == (x, y)
    assert not [t for t in loop.transitions if t.transition is Lifecycle.RAISED and t.state.world_ids == (x,)][1:]


def test_g3_merge_rekeys_event_to_surviving_id(rig):
    """X 에 이벤트가 없을 때 Y 의 이벤트는 교정으로 X 로 이어진다(새 이벤트 없음, 해소 없음, origin_world_ids = Y)."""
    m = _manager()
    loop = _RiskLoop()
    _seen_then_lost(m, rig)  # X 는 위험 판정 전 (구역 밖)
    for f in _feed(m, rig, range(45, 50), NEW_IDS):
        loop.feed(f)
    (y_event,) = loop.tracker.active()
    for f in _feed(m, rig, range(50, 80), REVIVED):
        loop.feed(f)
    (rev,) = m.revisions
    (event,) = loop.tracker.active()
    assert event.event_id == y_event.event_id
    assert event.world_ids == (rev.world_id,) and event.origin_world_ids == (rev.previous_world_id,)
    assert event.identity_revisions[rev.world_id] == 1
    assert loop.resolved() == [] and sum(t.transition is Lifecycle.RAISED for t in loop.transitions) == 1


def test_g3_same_id_reconnect_keeps_event(rig):
    """같은 ID 로 다시 이으면 소실 동안 확인 불가(UNVERIFIABLE)였던 이벤트가 그대로 이어진다. 해소·새 이벤트 없음."""
    m = _manager()
    loop = _RiskLoop()
    for k in range(10):
        loop.feed(_tick(m, rig, k, [lambda t: wt._cluster(rig, "xv-1", t, 3.0, 3.0)]))
    (event,) = loop.tracker.active()
    for k in range(10, 45):
        loop.feed(_tick(m, rig, k, []))
    for k in range(45, 70):
        loop.feed(_tick(m, rig, k, [lambda t: _mixed(rig, "xv-5", t, 3.5, 3.0, TWO_OLD)]))
    assert [s.event_id for s in loop.tracker.active()] == [event.event_id]
    assert loop.resolved() == [] and sum(t.transition is Lifecycle.RAISED for t in loop.transitions) == 1


# ---------------------------------------------------------------- 병합 도달 거리: 상대의 첫 관측부터 (리뷰 F4)


@pytest.mark.parametrize("xy", [(4.2, 3.0), (6.0, 5.0)])
def test_unreachable_newcomer_is_never_merged_and_alarm_stays(rig, xy):
    """리뷰 F4 장면: 구역 안 A 의 R1 HIGH 가 진행 중일 때 A 가 사라지고 0.1 s 뒤 1.2 m·3.6 m 떨어진 구역 밖에 B 가
    나타난다. cam1·cam2 로컬 트래커가 A 의 옛 ID 를 B 에게 붙였다. B 의 첫 관측은 A 의 마지막 관측에서 갈 수 없는
    거리라(0.5 + 2 × 0.1 = 0.7 m) B 가 제자리에 오래 있어도 A 로 병합하지 않고, A 의 경보는 해소되지 않는다."""
    m = _manager()
    loop = _RiskLoop(inside=lambda f, tr: tr.updated and tr.position_xy[0] < 3.5)
    for k in range(10):
        loop.feed(_tick(m, rig, k, [lambda t: wt._cluster(rig, "A-in-zone", t, 3.0, 3.0)]))
    (a_event,) = loop.tracker.active()
    a = a_event.world_ids[0]
    frames, distinct = [], []
    for k in range(10, 120):  # A 의 소실 후보가 끝난 뒤까지
        f = _tick(m, rig, k, [lambda t: _mixed(rig, "B-outside", t, *xy, TWO_OLD)])
        frames.append(f)
        distinct.append(set(m._distinct))
        loop.feed(f)
    (b,) = (tr.world_id for tr in frames[0].tracks if tr.world_id != a)
    assert m.revisions == () and all(f.reassociations == () for f in frames)
    # -657: 새 ID 를 만든 첫 시각에는 병합 금지 쌍으로 두지 않는다(예각 깊이 오차로 태어난 그림자를 영구히 묶지 않는다).
    # 다음 시각 병합 근거를 셀 때 B 의 관측 이력(첫 관측 포함)이 A 에서 갈 수 없는 곳이라 병합 금지 쌍이 된다(TTL 안 유지)
    assert frozenset((a, b)) not in distinct[0]
    assert frozenset((a, b)) in distinct[1]
    assert loop.resolved() == [] and not any(isinstance(s, AlertClear) for s in loop.signals)
    assert a_event.event_id in {s.event_id for s in loop.tracker.active()}


def _occluded_then_newcomer(m, rig, x1, y1=3.0, first_ids=NEW_IDS):
    """X 를 (3, 3) 에서 보다가(k=0..9) 1 s 가린 뒤(k=10..18) k=19 에 (x1, y1) 에서 새 로컬 트랙으로 Y 가 처음 보인다.
    공백 1 s: 위치 게이트 1.5 m, 갈 수 있는 거리 2.5 m."""
    x = wt._walk(m, rig, 10, speed=0.0, x0=3.0)[-1].tracks[0].world_id
    _empty(m, rig, 10, 19)
    f = _tick(m, rig, 19, [lambda t: _mixed(rig, "Y", t, x1, y1, first_ids)])
    (y,) = (tr.world_id for tr in f.tracks if tr.world_id != x)
    return x, y


@pytest.mark.parametrize("d, merged", [(2.49, True), (2.51, False)])
def test_merge_reach_boundary_from_first_observation(rig, d, merged):
    """반례 1(경계): Y 의 첫 관측이 X 의 마지막 관측에서 갈 수 있는 거리(2.5 m) 안이면 뒤에 두 카메라 근거로 병합하고,
    밖이면 기다려도 병합하지 않는다."""
    m = _manager()
    x, y = _occluded_then_newcomer(m, rig, 3.0 + d)
    frames = _feed_at(m, rig, range(20, 30), 3.0 + d, 3.0, REVIVED)
    assert (sum(len(f.revisions) for f in frames) == 1) is merged
    assert (frozenset((x, y)) in m._distinct) is (not merged)


def test_reachable_slow_walker_merges(rig):
    """반례 2: Y 가 X 의 마지막 위치에서 갈 수 있는 곳(2.0 m)에 처음 보이고 천천히(0.3 m/s) 걸어가다가 두 카메라가 X 의
    옛 로컬 트랙을 되살리면 정상 병합한다(도달 거리 검사가 정상 병합을 막지 않는다)."""
    m = _manager()
    x, y = _occluded_then_newcomer(m, rig, 5.0)
    frames = [_tick(m, rig, k, [lambda t, k=k: _mixed(rig, "Y", t, 5.0 + 0.03 * (k - 19), 3.0, NEW_IDS)]) for k in range(20, 26)]
    frames += [_tick(m, rig, k, [lambda t, k=k: _mixed(rig, "Y", t, 5.0 + 0.03 * (k - 19), 3.0, REVIVED)]) for k in range(26, 32)]
    revs = [r for f in frames for r in f.revisions]
    assert [(r.world_id, r.previous_world_id, r.reason) for r in revs] == [(x, y, str(RevisionReason.DUPLICATE_MERGE))]


def test_multiple_stale_tracks_only_reachable_one_merges(rig):
    """반례 3(여러 소실·가림 Track): Y 의 첫 관측에서 갈 수 없는 X2 의 로컬 트랙은 근거가 아니고(병합 금지 쌍), 갈 수
    있는 X1 의 로컬 트랙으로는 병합한다."""
    m = _manager()
    for k in range(10):
        _tick(m, rig, k, [lambda t: wt._cluster(rig, "X1", t, 2.0, 2.0, prefix="a"),
                          lambda t: wt._cluster(rig, "X2", t, 7.0, 5.5, prefix="b")])  # fmt: skip
    x1, x2 = m.issuer.issued
    _empty(m, rig, 10, 19)
    f = _tick(m, rig, 19, [lambda t: _mixed(rig, "Y", t, 4.0, 2.0, NEW_IDS)])
    (y,) = (tr.world_id for tr in f.tracks if tr.world_id not in (x1, x2))
    frames = _feed_at(m, rig, range(20, 24), 4.0, 2.0, dict(NEW_IDS, cam1="cam1-b", cam2="cam2-b"))
    assert all(f.revisions == () for f in frames) and frozenset((x2, y)) in m._distinct
    frames = _feed_at(m, rig, range(24, 30), 4.0, 2.0, dict(NEW_IDS, cam1="cam1-a", cam2="cam2-a"))
    revs = [r for f in frames for r in f.revisions]
    assert [(r.world_id, r.previous_world_id) for r in revs] == [(x1, y)]


def test_residual_risk_two_camera_simultaneous_switch_is_reconnected(rig):
    """잔여 위험(리뷰 R1, 지금 동작 고정): 두 카메라의 로컬 트래커가 같은 상황에서 함께 A 의 옛 ID 를 B 에게 붙이면
    2대 근거를 채워 B 가 A 로 이어진다. 두 추적기가 같은 오류를 동시에 내야 하므로 드물지만 독립 근거는 아니다.
    바뀌면 설계 문서의 잔여 위험 절을 함께 고친다."""
    m = _manager()
    a = _seen_then_lost(m, rig)
    f = _tick(m, rig, 45, [lambda t: _mixed(rig, "B", t, 4.2, 3.0, TWO_OLD)])
    assert f.reassociations == (a,)


def _feed_at(m, rig, ks, x, y, ids):
    return [_tick(m, rig, k, [lambda t, ids=ids: _mixed(rig, "Y", t, x, y, ids)]) for k in ks]
