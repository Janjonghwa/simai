"""-784 확정 Track 근처 새 World ID 출생 보류 시험 (2026-11-16 라이브: 실제 2명, 27분에 화면 World ID 27개).

합성 장면만 쓴다. 재현 장면(가장 많은 유형): 가만히 선 확정 한 사람이 몇 tick 동안 예각 북쪽 쌍(camB·camC)에만 보이고 그 쌍의
엉덩이 점이 에피폴라 방향으로 밀려(±16 px) 골반이 0.8 m 뛴다. 갈 수 있는 거리(0.5 + 2 × 0.1 = 0.7 m) 밖이라 원래 Track 은
갱신되지 않고(아바타 캡슐) 새 ID 가 생겨, 원래 ID 가 돌아온 뒤에도 3 s 넘게 둘 다 화면에 남았다.

안전: 같은 시각 같은 카메라에 따로 보인 진짜 새 사람은 태어난 시각에 바로 내보낸다. 끝내 따로 보이지 않아도 보류 상한(1.5 s)
안에 내보낸다. 사라진 사람 옆에 나타난 다른 사람은 ID 가 넘어갈 수는 있어도 화면에서 빠지지 않는다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from simai_ai.geometry.rig import CameraRig
from simai_ai.tracking.diagnostics import window_log_line
from simai_ai.tracking.world_state import world_state_payload

_spec = importlib.util.spec_from_file_location("_dg784", Path(__file__).with_name("test_track_duplicate_guard.py"))
dg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dg)
wt = dg.wt
T, DT = dg.T, dg.DT


@pytest.fixture(scope="module")
def acute():
    return CameraRig.from_provider(wt._DictCalibrationProvider(dg._acute_doc()))


@pytest.fixture(scope="module")
def rig():
    return CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def _jump_scene(acute, prefix, jump_ticks, total=70, **cfg):
    """k=0..9 네 대로 확정, jump_ticks 동안 북쪽 쌍에만 보이며 골반이 0.8 m 뜀, 그 뒤 다시 네 대."""
    m = dg._mgr(**cfg)
    frames = []
    for k in range(total):
        t = T + k * DT
        if k in jump_ticks:
            clusters = [dg._cl(acute, "n", t, dg.X0, dg.Y0, dg._ids(dg.NORTH, prefix), 16.0)]
        else:
            clusters = [dg._cl(acute, "all", t, dg.X0, dg.Y0, dg._ids(dg.ACUTE))]
        frames.append(dg._step(m, acute, k, clusters))
    return m, frames


@pytest.mark.parametrize("prefix", ["p", "new"])
def test_reproduction_without_hold_shows_duplicate_for_seconds(acute, prefix):
    """재현(보류 끔 = 이전 동작): 0.5 s 깊이 뜀 동안 원래 ID 는 갱신되지 않고, 새 ID 가 3 s 넘게 화면에 남는다."""
    m, frames = _jump_scene(acute, prefix, range(10, 15), birth_hold_enabled=False)
    first = m.issuer.issued[0]
    dup = [f for f in frames if any(tr.world_id != first for tr in f.tracks)]
    assert len(dup) >= 30
    assert all(not f.track(first).updated for f in frames[10:15])


@pytest.mark.parametrize("prefix", ["p", "new"])
def test_stationary_depth_jump_is_absorbed_without_new_id(acute, prefix):
    """같은 장면(보류 켬): 화면에는 원래 ID 하나뿐이고, 뛴 관측은 원래 ID 로 이어진다. 공백은 최대 2 tick(0.2 s)이라 OCCLUDED
    로 가지 않는다(첫 뜀 시각은 0.7 m 갈 수 있는 거리 밖이라 흡수 근거로 세지 않고, 0.2 s 공백의 0.9 m 안에서 2번 센다).
    교정 기록·별칭·revision 을 남기지 않는다(내보낸 적 없는 ID). 로컬 트랙이 이어지든(p) 새로 시작하든(new) 같다."""
    m, frames = _jump_scene(acute, prefix, range(10, 15))
    first = m.issuer.issued[0]
    assert all([tr.world_id for tr in f.tracks] == [first] for f in frames)
    updated = [f.track(first).updated for f in frames]
    assert not any(not (a or b or c) for a, b, c in zip(updated, updated[1:], updated[2:], strict=False))  # 3 tick 연속 없음
    assert all(f.track(first).lifecycle is wt.TrackLifecycle.CONFIRMED for f in frames[5:])
    assert all(f.revisions == () for f in frames) and m.revisions == ()
    last = frames[-1].track(first)
    assert last.aliases == () and last.identity_revision == 0
    assert m.diag["birth_hold.absorbed"] == 2  # 뜀 시작과 네 대 복귀 때 각각
    assert m.diag["lifecycle.confirmed"] == 1


def test_single_tick_jump_never_reaches_world_state(acute):
    """한 tick 만 뛴 관측의 새 Track 은 보류된 채 TENTATIVE 공백으로 소멸한다(화면·world_state 에 한 번도 나가지 않는다)."""
    m, frames = _jump_scene(acute, "new", range(10, 11), total=30)
    first = m.issuer.issued[0]
    assert len(m.issuer.issued) == 2
    assert all([tr.world_id for tr in f.tracks] == [first] for f in frames)
    payload = world_state_payload(frames[10], frames[10].t, "scene-784")
    assert [d["world_id"] for d in payload["tracks"]] == [first]
    assert m.diag["birth_hold.expired"] == 1 and m.diag["lifecycle.lost_tentative"] == 0


def _confirmed(m, rig, x, y, ticks=5, cams=wt.ALL, prefix="p"):
    for k in range(ticks):
        t = T + k * DT
        m.update(wt._frame(rig, t, [wt._cluster(rig, "P1", t, x, y, cams=cams, prefix=prefix)]), rig)
    return m.issuer.issued[0]


@pytest.mark.parametrize("dx, held", [(0.4, True), (0.6, False)])
def test_real_newcomer_seen_by_same_cameras_is_shown_at_birth(rig, dx, held):
    """진짜 새 사람이 확정된 사람 옆에 나타나 같은 카메라들에 따로 보이면 태어난 시각에 바로 내보낸다(지연 0). 0.4 m 는 원래
    사람의 관측 오차로 설명돼 보류했다가 같은 시각 안에 풀리고, 0.6 m 는 넓은 기선 카메라로는 설명되지 않아 보류하지도 않는다."""
    m = dg._mgr()
    p1 = _confirmed(m, rig, 3.0, 3.0)
    frames = []
    for k in range(5, 15):
        t = T + k * DT
        frames.append(m.update(wt._frame(rig, t, [
            wt._cluster(rig, "P1", t, 3.0, 3.0),
            wt._cluster(rig, "P2", t, 3.0 + dx, 3.0, prefix="b"),
        ]), rig))
    p2 = m.issuer.issued[1]
    assert {tr.world_id for tr in frames[0].tracks} == {p1, p2} and frames[0].track(p2).updated
    assert all(len(f.tracks) == 2 and all(tr.updated for tr in f.tracks) for f in frames)
    assert m.diag["birth_hold.started"] == m.diag["birth_hold.released_distinct"] == int(held)
    assert m.revisions == ()


def test_close_newcomer_never_seen_by_same_camera_is_shown_within_cap(rig):
    """확정된 사람(cam1·cam2) 0.45 m 옆에 다른 사람이 cam3·cam4 로만 보여 끝내 따로 보인 근거가 없어도, 보류 상한(1.5 s) 안에
    내보내고 합치지 않는다(근접 두 사람을 하나로 만들지 않는다). 보류 중에는 체크포인트에도 넣지 않는다."""
    m = dg._mgr()
    p1 = _confirmed(m, rig, 3.0, 3.0, cams=("cam1", "cam2"))
    frames = []
    held_checked = False
    for k in range(5, 45):
        t = T + k * DT
        frames.append(m.update(wt._frame(rig, t, [
            wt._cluster(rig, "P1", t, 3.0, 3.0, cams=("cam1", "cam2")),
            wt._cluster(rig, "P2", t, 3.0, 3.45, cams=("cam3", "cam4"), prefix="b"),
        ]), rig))
        if len(m.issuer.issued) == 2 and not held_checked:
            assert [c.world_id for c in m.export_tracks()] == [p1]
            held_checked = True
    p2 = m.issuer.issued[1]
    shown = next(i for i, f in enumerate(frames) if f.track(p2) is not None)
    assert shown * DT <= 1500
    assert all({tr.world_id for tr in f.tracks} == {p1, p2} for f in frames[shown:])
    assert all(f.track(p1).updated for f in frames)
    assert m.revisions == () and m.diag["birth_hold.absorbed"] == 0
    assert m.diag["birth_hold.released_timeout"] == 1


def test_newcomer_beside_vanished_person_is_never_hidden(acute):
    """사라진 사람(P1, 1 s 동안 어느 카메라에도 없음) 자리에 다른 사람 P2 가 예각 북쪽 쌍으로만 보여 골반이 0.8 m 밀려 있으면
    (P1 의 관측 오차로 설명됨) P1 의 ID 가 P2 로 넘어갈 수는 있지만 화면에서 사람이 빠지는 시각은 태어난 뒤 2 tick 뿐이다.
    P1 이 네 대로 돌아오면 그 위치는 P2 쪽 ID 의 관측 오차로 설명되지 않아 태어난 시각에 바로 내보낸다(ID 맞바꿈은 남는 대가,
    설계 문서 잔여 위험)."""
    m = dg._mgr()
    for k in range(10):
        dg._step(m, acute, k, [dg._cl(acute, "all", T + k * DT, dg.X0, dg.Y0, dg._ids(dg.ACUTE))])
    for k in range(10, 40):
        t = T + k * DT
        clusters = [dg._cl(acute, "P2", t, dg.X0, dg.Y0, dg._ids(dg.NORTH, "b"), 16.0)]
        if k >= 20:
            clusters.append(dg._cl(acute, "P1", t, dg.X0, dg.Y0, dg._ids(dg.ACUTE)))
        f = dg._step(m, acute, k, clusters)
        if k not in (10, 11):
            assert sum(tr.updated for tr in f.tracks) == len(clusters), k
    assert m.diag["birth_hold.absorbed"] == 1


def test_window_line_reports_birth_hold():
    line = window_log_line({"world": {"manager": {
        "birth_hold.started": 5, "birth_hold.absorbed": 2, "birth_hold.merged_silent": 1, "birth_hold.expired": 1,
        "birth_hold.released_distinct": 1, "birth_hold.released_timeout": 0,
    }}})
    assert "출생보류 5/흡수 2+병합 1/소멸 1/풀림 다른사람 1·멀어짐 0·시간초과 0" in line


@pytest.mark.parametrize("shift", [22.0, 26.0])
def test_person_approaching_hidden_track_is_not_absorbed(acute, shift):
    """리뷰 1: 가려진 A(어느 카메라에도 없음) 옆에 다른 사람 B 가 북쪽 쌍으로만 보여 골반이 A 에서 1 m 넘게 떨어져 있으면, 출생
    보류는 걸려도(관측 오차로 설명) 흡수는 하지 않는다(σ 확장 없는 갈 수 있는 거리·1.0 m 상한·공백 0.5 s 이하). B 는 A 의 ID 를
    받지 않고 새 ID 로 1.5 s 안에 나간다."""
    m = dg._mgr()
    for k in range(10):
        dg._step(m, acute, k, [dg._cl(acute, "all", T + k * DT, dg.X0, dg.Y0, dg._ids(dg.ACUTE))])
    a = m.issuer.issued[0]
    frames = []
    for k in range(10, 40):
        t = T + k * DT
        frames.append(dg._step(m, acute, k, [dg._cl(acute, "B", t, dg.X0, dg.Y0, dg._ids(dg.NORTH, "b"), shift)]))
    assert m.diag["birth_hold.started"] >= 1
    assert m.diag["birth_hold.absorbed"] == 0
    assert all(f.track(a) is None or not f.track(a).updated for f in frames)  # A 의 ID 는 B 의 위치를 받지 않는다
    shown = next(i for i, f in enumerate(frames) if any(tr.world_id != a for tr in f.tracks))
    assert shown * DT <= 1500
    assert all(any(tr.world_id != a and tr.updated for tr in f.tracks) for f in frames[shown:])


def test_expired_held_track_returns_borrowed_keys(acute):
    """리뷰 2: 보류 Track 이 원래 Track 의 로컬 트랙(북쪽 쌍)을 받아 간 뒤 보류인 채 소멸하면 원래 주인에게 되돌린다."""
    m = dg._mgr()
    for k in range(10):
        dg._step(m, acute, k, [dg._cl(acute, "all", T + k * DT, dg.X0, dg.Y0, dg._ids(dg.ACUTE))])
    a = m.issuer.issued[0]
    north = {("camB", "camB-p"), ("camC", "camC-p")}
    dg._step(m, acute, 10, [dg._cl(acute, "n", T + 10 * DT, dg.X0, dg.Y0, dg._ids(dg.NORTH), 16.0)])
    assert not north & set(m._tracks[a].keys)  # 보류 Track 이 잠시 가져갔다
    for k in range(11, 20):
        f = dg._step(m, acute, k, [dg._cl(acute, "s", T + k * DT, dg.X0, dg.Y0, dg._ids(dg.SOUTH))])
        assert [tr.world_id for tr in f.tracks] == [a]
    assert m.diag["birth_hold.expired"] == 1
    assert north <= set(m._tracks[a].keys)
    assert m.diag["birth_hold.keys_returned"] == 2


def test_held_track_does_not_flag_exported_track(rig):
    """리뷰 3: 숨긴 보류 Track 이 0.3 m 옆에 있어도 내보내는 Track 에 POSSIBLE_DUPLICATE·AMBIGUOUS_TRACK_MATCH 를 붙이지 않는다."""
    m = dg._mgr()
    p1 = _confirmed(m, rig, 3.0, 3.0, cams=("cam1", "cam2"))
    for k in range(5, 12):
        t = T + k * DT
        f = m.update(wt._frame(rig, t, [
            wt._cluster(rig, "P1", t, 3.0, 3.0, cams=("cam1", "cam2")),
            wt._cluster(rig, "P2", t, 3.0, 3.3, cams=("cam3", "cam4"), prefix="b"),
        ]), rig)
        assert [tr.world_id for tr in f.tracks] == [p1]
        flags = set(map(str, f.track(p1).identity_flags))
        assert not flags & {"POSSIBLE_DUPLICATE", "AMBIGUOUS_TRACK_MATCH"}, (k, flags)
