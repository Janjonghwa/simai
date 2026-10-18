"""재시작 뒤 복원 Track 매칭 시험 (명세 3.2 재시작 복구·부록 C, 5.2, 6.1, S15P31A101-54).

복원 Track(체크포인트의 Track)은 재관측과 맞출 후보일 뿐이다. 위치 정본이 아니고(OBSERVED 로 새지 않음) 같은 World ID
로 잇지도 않는다. 재시작 뒤에는 로컬 트래커가 새로 시작해 늦은 재연결의 근거(카메라 2대의 옛 로컬 트랙)가 없고, 위치만으로는
잇지 않는다(-40 과 같은 근거 기준). 합성 카메라·골격 도구는 test_world_track.py 의 것을 쓴다. 모든 입력은 손으로 만든 SIM 이다.
"""

import importlib.util
from pathlib import Path

import pytest

from simai_ai.geometry.triangulation import triangulate_skeleton
from simai_ai.tracking.cross_view import ViewCluster
from simai_ai.tracking.world_state import world_state_message, world_state_payload
from simai_ai.tracking.world_track import (
    IdentityConfidence,
    IdentityFlag,
    RestoreState,
    TrackCheckpoint,
    TrackLifecycle,
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
)

_spec = importlib.util.spec_from_file_location("_wt_helpers_restore", Path(__file__).with_name("test_world_track.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

T, DT, ALL = wt.T, wt.DT, wt.ALL
UNCERTAIN = IdentityConfidence.IDENTITY_UNCERTAIN
CANDIDATE = IdentityFlag.RESTORED_TRACK_CANDIDATE
MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
R1, R2 = "w-pre-0001", "w-pre-0002"


@pytest.fixture(scope="module")
def rig():
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def _restored(world_id=R1, x=3.0, y=3.0, t_last_valid=T - 2000):
    return TrackCheckpoint(
        world_id=world_id,
        created_t=t_last_valid - 5000,
        t_last_valid=t_last_valid,
        lifecycle=TrackLifecycle.CONFIRMED,
        identity_revision=1,
        aliases=("w-pre-0009",),
        position_xyz=(x, y, 0.95),
        filter_sigma_xyz=(0.03, 0.03, 0.03),
        velocity=(0.0, 0.0, 0.0),
    )


def _manager(tracks=(), tracks_t=None, **cfg):
    m = WorldTrackManager(WorldIdIssuer("post"), WorldTrackConfig(**cfg))
    m.restore(tracks, tracks_t)
    return m


def _at(m, rig, t, x, y, cams=ALL, prefix="p"):
    return m.update(wt._frame(rig, t, [wt._cluster(rig, "xv-1", t, x, y, cams, prefix)]), rig)


def _mixed(rig, name, t, x, y, ids):
    views = tuple(wt._view(rig, cam, lid, t, x, y) for cam, lid in sorted(ids.items()))
    skeleton = triangulate_skeleton([v.view for v in views], rig) if len(views) >= 2 else None
    return ViewCluster(name, views, skeleton, ())


# ---------------------------------------------------------------- 후보일 뿐 신원·위치가 아니다


def test_restored_track_is_a_candidate_not_an_identity_or_position(rig, contract_errors):
    """복원 Track 자리 근처에 나타난 사람은 새 ID 다(복원 ID·revision·별칭을 물려받지 않음). 위치는 자기 측정이다.
    복원 Track 이 후보로 남아 있는 동안 IDENTITY_UNCERTAIN(RESTORED_TRACK_CANDIDATE)이다."""
    restored = _restored()
    m = _manager([restored], restored.t_last_valid)
    assert m.restore_status.state is RestoreState.PENDING
    f = _at(m, rig, T, 3.2, 3.0)
    (tr,) = f.tracks
    assert tr.world_id.startswith("w-post-") and tr.world_id != R1
    assert (tr.identity_revision, tr.aliases, tr.lifecycle) == (0, (), TrackLifecycle.TENTATIVE)
    assert tr.position_xy == pytest.approx((3.2, 3.0), abs=0.02) and tr.t_last_valid == T
    assert tr.restored_candidates == (R1,) and tr.restored_excluded == ()
    assert tr.identity_confidence is UNCERTAIN and CANDIDATE in tr.identity_flags
    status = m.restore_status
    assert (status.state, status.restored, status.decided_t, status.link_until) == (
        RestoreState.ACCEPTED,
        (R1,),
        T,
        T + 30_000,
    )
    # world_state·export_tracks 에 복원 Track 은 없다 (위치 정본이 아니다)
    payload = world_state_payload(f, f.t, "scene-1")
    assert [d["world_id"] for d in payload["tracks"]] == [tr.world_id]
    assert contract_errors("world_state", world_state_message(payload, msg_id=MSG_ID, sent_at=T + 30)) == []
    assert [c.world_id for c in m.export_tracks()] == [tr.world_id]
    assert m.restored_alive == (R1,) and m.lost_candidates == ()


def test_single_camera_view_at_restored_spot_does_not_revive_position(rig):
    """복원 위치가 OBSERVED 로 새는 경로: 복원 Track 자리에 카메라 한 대로만 보인 사람으로는 아무 Track 도 만들지 않는다
    (2대 미만은 지어내지 않음, 복원 위치로 채우지 않음). 이어서 새 ID 의 관측 상태는 자기 t_last_valid 로만 정한다."""
    restored = _restored(t_last_valid=T - 500)
    m = _manager([restored], restored.t_last_valid)
    for k in range(5):
        f = _at(m, rig, T + k * DT, 3.0, 3.0, cams=("cam1",))
        assert f.tracks == ()
    seen = [_at(m, rig, T + k * DT, 3.0, 3.0) for k in range(5, 9)]
    (tr,) = seen[-1].tracks
    assert tr.world_id != R1 and tr.t_last_valid == T + 8 * DT and tr.lifecycle is TrackLifecycle.CONFIRMED
    hidden = [m.update(wt._frame(rig, T + k * DT, []), rig) for k in range(9, 16)]
    last = hidden[-1].track(tr.world_id)
    assert last.t_last_valid == T + 8 * DT  # 복원 Track 이 위치를 대신 채우지 않는다
    doc = world_state_payload(hidden[-1], hidden[-1].t, "scene-1")["tracks"][0]
    assert doc["observation_state"] == "UNKNOWN" and doc["world_id"] == tr.world_id


# ---------------------------------------------------------------- 30초 규칙


@pytest.mark.parametrize("age_ms, accepted", [(29_900, True), (30_000, True), (30_100, False)])
def test_checkpoint_age_boundary(rig, age_ms, accepted):
    """첫 프레임 시각 − 체크포인트 Track 시각이 30 s 를 넘으면 복원 Track 을 받지 않는다(같은 자리에 나타나도 후보 없음)."""
    restored = _restored(t_last_valid=T - age_ms)
    m = _manager([restored], T - age_ms)
    f = _at(m, rig, T, 3.0, 3.0)
    (tr,) = f.tracks
    status = m.restore_status
    assert (status.state is RestoreState.ACCEPTED) is accepted
    assert (tr.restored_candidates == (R1,)) is accepted
    assert (CANDIDATE in tr.identity_flags) is accepted
    if not accepted:
        assert "30100 ms 전" in status.reason and status.restored == () and m.restored_alive == ()
    assert status.link_until == T + 30_000  # 이벤트 연결 시간은 받든 버리든 첫 프레임부터 잰다


def test_restored_track_expires_30s_after_its_last_observation(rig):
    """체크포인트는 30 s 안이어도 그 Track 의 마지막 관측(가림 중이던 Track)이 30 s 넘으면 후보에서 뺀다."""
    occluded = _restored(R1, t_last_valid=T - 29_000)
    fresh = _restored(R2, x=6.0, t_last_valid=T - 1000)
    m = _manager([occluded, fresh], T - 1000)
    m.update(wt._frame(rig, T, []), rig)
    assert m.restored_alive == (R1, R2)
    m.update(wt._frame(rig, T + 1000, []), rig)
    assert m.restored_alive == (R1, R2)  # 30 000 ms 째: 아직 남는다
    m.update(wt._frame(rig, T + 1001, []), rig)
    assert m.restored_alive == (R2,)


@pytest.mark.parametrize(
    "tracks, tracks_t, reason",
    [
        ([_restored(t_last_valid=T - 1000)], T + 1, "시각 역행"),
        ([_restored(t_last_valid=T - 1000)], None, "모른다"),
    ],
)
def test_untrusted_checkpoint_time_discards_tracks(rig, tracks, tracks_t, reason):
    """체크포인트 Track 시각을 믿을 수 없으면(첫 프레임보다 뒤, 모름) 복원하지 않는다(모르면 비운다)."""
    m = _manager(tracks, tracks_t)
    (tr,) = _at(m, rig, T, 3.0, 3.0).tracks
    assert m.restore_status.state is RestoreState.DISCARDED and reason in m.restore_status.reason
    assert tr.restored_candidates == () and CANDIDATE not in tr.identity_flags


def test_restore_without_tracks_is_accepted_and_opens_link_window(rig):
    """체크포인트에 Track 이 없어도(아무도 없었음, 저널만으로 복구) 재시작 복구다: 연결 시간은 연다."""
    m = _manager((), None)
    m.update(wt._frame(rig, T, []), rig)
    status = m.restore_status
    assert (status.state, status.restored, status.link_until) == (RestoreState.ACCEPTED, (), T + 30_000)
    assert status.link_open(T + 30_000) and not status.link_open(T + 30_001)


def test_no_restore_means_no_link_window(rig):
    m = WorldTrackManager(WorldIdIssuer("post"))
    _at(m, rig, T, 3.0, 3.0)
    assert m.restore_status.state is RestoreState.NONE and not m.restore_status.link_open(T)


# ---------------------------------------------------------------- 후보 판정 (도달 거리, 첫 관측부터)


def test_candidate_flag_lasts_while_restored_track_alive_then_clears(rig):
    """복원 Track 이 후보로 남는 동안(마지막 관측 + 30 s) 매 시각 IDENTITY_UNCERTAIN 이다. 끝나면 유지 시간 뒤 풀린다."""
    restored = _restored(t_last_valid=T - 2000)
    m = _manager([restored], restored.t_last_valid)
    until = restored.t_last_valid + 30_000
    frames = [_at(m, rig, T + k * 200, 3.1, 3.0) for k in range(160)]  # 32 s
    wid = frames[0].tracks[0].world_id
    for f in frames:
        tr = f.track(wid)
        if f.t <= until:
            assert tr.identity_confidence is UNCERTAIN and CANDIDATE in tr.identity_flags, f.t - T
    assert frames[-1].track(wid).identity_confidence is IdentityConfidence.HIGH
    assert frames[-1].track(wid).restored_candidates == (R1,)  # 관계 기록은 남는다(연결 판단용)
    assert m.issuer.issued == (wid,)


def test_unreachable_first_observation_excludes_and_waiting_does_not_help(rig):
    """재시작이 짧아(0.2 s) 복원 Track 이 갈 수 있는 거리(0.9 m) 밖에 처음 나타난 사람은 그 사람이 아니다. 그 뒤 복원
    Track 자리로 걸어가도 후보가 되지 않는다(도달 거리는 첫 관측부터 잰다, -40 리뷰 F4 와 같은 원칙)."""
    restored = _restored(t_last_valid=T - 200)
    m = _manager([restored], restored.t_last_valid)
    frames = [_at(m, rig, T + k * DT, 6.0 - 0.1 * k, 3.0) for k in range(30)]  # 1 m/s 로 (3.1, 3.0) 까지
    wid = frames[0].tracks[0].world_id
    assert all(f.track(wid).restored_candidates == () for f in frames)
    assert all(f.track(wid).restored_excluded == (R1,) for f in frames)
    assert all(CANDIDATE not in f.track(wid).identity_flags for f in frames)


@pytest.mark.parametrize("dx, candidate", [(0.89, True), (0.91, False)])
def test_reach_boundary(rig, dx, candidate):
    """도달 거리 = 0.5 m + 2 m/s × (첫 관측 − 복원 Track 의 마지막 관측) (늦은 재연결과 같은 식). 공백 0.2 s → 0.9 m."""
    restored = _restored(x=3.0, t_last_valid=T - 200)
    m = _manager([restored], restored.t_last_valid)
    (tr,) = _at(m, rig, T, 3.0 + dx, 3.0).tracks
    assert (tr.restored_candidates == (R1,)) is candidate
    assert (tr.restored_excluded == (R1,)) is (not candidate)


def test_two_restored_tracks_contest_one_person(rig):
    """두 복원 Track 이 한 사람을 다투면 둘 다 후보로 남기고(가까운 순) 어느 쪽으로도 잇지 않는다."""
    a = _restored(R1, x=3.0, y=3.0, t_last_valid=T - 500)
    b = _restored(R2, x=3.0, y=3.8, t_last_valid=T - 500)
    m = _manager([a, b], T - 500)
    (tr,) = _at(m, rig, T, 3.1, 3.2).tracks
    assert tr.restored_candidates == (R1, R2)
    assert tr.world_id not in (R1, R2) and tr.identity_confidence is UNCERTAIN


def test_restored_track_never_reobserved_expires_without_side_effects(rig):
    """복원 Track 이 끝내 재관측되지 않으면 마지막 관측 + 30 s 에 후보에서 빠진다. 그 뒤 같은 자리에 나타난 사람은
    아무 후보도 없는 새 ID 다. world_state 에는 그동안 아무 Track 도 없다."""
    restored = _restored(t_last_valid=T - 1000)
    m = _manager([restored], restored.t_last_valid)
    frames = [m.update(wt._frame(rig, T + k * 100, []), rig) for k in range(291)]
    assert all(f.tracks == () for f in frames)
    assert m.restored_alive == (R1,)  # T + 29 000: 마지막 관측 + 30 000 ms 까지는 남는다
    f = _at(m, rig, T + 29_100, 3.0, 3.0)
    assert m.restored_alive == ()
    (tr,) = f.tracks
    assert tr.restored_candidates == () and tr.restored_excluded == () and CANDIDATE not in tr.identity_flags


# ---------------------------------------------------------------- -40 병합과 함께


NEW_IDS = {"cam3": "cam3-q", "cam4": "cam4-q"}
REVIVED = dict(NEW_IDS, cam1="cam1-p", cam2="cam2-p")


def test_merge_after_restart_unions_candidates(rig):
    """재시작 뒤 생긴 두 ID 가 -40 근거(카메라 2대의 옛 로컬 트랙)로 병합되면 복원 Track 후보를 합친다. 한쪽 첫 관측에서
    갈 수 없었던 복원 Track 도 다른 쪽에서 후보였으면 후보로 남긴다(같은 사람이라는 근거가 있어 보수적으로 둔다)."""
    a = _restored(R1, x=3.0, t_last_valid=T - 200)
    b = _restored(R2, x=6.5, t_last_valid=T - 200)
    m = _manager([a, b], T - 200)
    first = [_at(m, rig, T + k * DT, 3.0, 3.0) for k in range(10)]
    x = first[0].tracks[0].world_id
    assert first[-1].track(x).restored_candidates == (R1,) and first[-1].track(x).restored_excluded == (R2,)
    for k in range(10, 45):  # 3.5 s 가림: X 는 LOST(소실 후보)
        m.update(wt._frame(rig, T + k * DT, []), rig)
    frames = []
    for k in range(45, 80):
        ids = NEW_IDS if k < 50 else REVIVED
        frames.append(m.update(wt._frame(rig, T + k * DT, [_mixed(rig, "xv-5", T + k * DT, 4.0, 3.0, ids)]), rig))
    y = frames[0].tracks[0].world_id
    assert y != x and frames[0].track(y).restored_candidates == (R1, R2)
    (rev,) = m.revisions
    assert (rev.world_id, rev.previous_world_id) == (x, y)
    survivor = frames[-1].track(x)
    assert survivor.restored_candidates == (R1, R2) and survivor.restored_excluded == ()


# ---------------------------------------------------------------- 입력 검사


def test_restore_input_checks(rig):
    m = WorldTrackManager(WorldIdIssuer("post"))
    _at(m, rig, T, 3.0, 3.0)
    with pytest.raises(ValueError, match="첫 update 전"):
        m.restore([_restored()], T - 100)
    m = WorldTrackManager(WorldIdIssuer("post"))
    m.restore([], None)
    with pytest.raises(ValueError, match="한 번만"):
        m.restore([], None)
    with pytest.raises(ValueError, match="이번 세션"):
        WorldTrackManager(WorldIdIssuer("post")).restore([_restored("w-post-0001")], T - 100)
    with pytest.raises(ValueError, match="둘 이상"):
        WorldTrackManager(WorldIdIssuer("post")).restore([_restored(), _restored()], T - 100)
    with pytest.raises(TypeError):
        WorldTrackManager(WorldIdIssuer("post")).restore([_restored().to_record()], T - 100)
    with pytest.raises(ValueError):
        WorldTrackConfig(restore_max_age_ms=-1)
