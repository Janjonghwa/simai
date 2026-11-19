"""사각지대를 들락날락하는 한 사람의 ID 유지 시험 (명세 5.2 늦은 재연결·DORMANT 재입장, S15P31A101-802).

증상(2026-11-19 로비 실시간 세션): 사실상 혼자인 한 사람이 2대(cam_B·cam_C) 화각 끝 모서리에서 사라졌다가 11.2~14.9 s 뒤 같은
자리에 돌아올 때마다 새 World ID 가 생겼다(18분 12개). 고친 것:
1. 재연결 창(reconnect_window_ms) 10 s → 20 s.
2. Re-ID 재입장 마진의 경쟁자는 지금 활성 Track 과, 최선 후보와 다른 사람으로 확인된(같은 시각 함께 위치 측정 = 동시 활성,
   또는 병합 금지 쌍) 소실·휴면 후보만. 동시에 있은 적 없는 후보는 같은 사람의 옛 ID 일 수 있어 경쟁자로 세지 않는다.
모든 입력은 손으로 만든 SIM 이다(합성 카메라·골격은 test_world_track.py, 특징은 직교 단위 벡터).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import numpy as np
import pytest

from simai_ai.geometry.triangulation import triangulate_skeleton
from simai_ai.reid.collector import LocalTrackFeature, LocalTrackFeatureCache
from simai_ai.reid.visibility import PartVisibility
from simai_ai.tracking.cross_view import ViewCluster
from simai_ai.tracking.world_track import (
    IdentityFlag,
    RevisionKind,
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
)

_spec = importlib.util.spec_from_file_location("_wt_helpers", Path(__file__).with_name("test_world_track.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

T, DT, ALL = wt.T, wt.DT, wt.ALL
GAP_MS = 15_000
"""사라졌다 돌아오기까지(관측 실측 11.2~14.9 s 의 위쪽). 예전 창 10 s 는 넘고 새 창 20 s 안이다"""
CORNER = (2.0, 2.0)
"""들락날락하는 자리 (합성 교실의 한 모서리 쪽)"""
FAR = (6.5, 5.0)
"""다른 사람 B 의 자리 (갤러리 동결 거리 1.0 m 보다 멀다)"""


@pytest.fixture(scope="module")
def rig():
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def _emb(weights: dict[int, float], dim: int = 1280) -> np.ndarray:
    """직교 기저의 가중합(정규화). 기저 i 와의 코사인 유사도는 weights[i] / ‖weights‖."""
    v = np.zeros(dim, dtype=np.float32)
    for i, w in weights.items():
        v[i] = w
    return (v / float(np.linalg.norm(v))).astype(np.float32)


P = _emb({0: 1.0})
"""사람 P(들락날락하는 사람)의 외형"""
Q = _emb({1: 1.0})
"""P 가 겉옷을 걸쳤을 때의 외형(P 와 유사도 0)"""
A = _emb({2: 1.0})
B = _emb({3: 1.0})


def _put(cache: LocalTrackFeatureCache, ids: dict[str, str], t: int, emb: np.ndarray) -> None:
    for idx, (cam, lid) in enumerate(sorted(ids.items())):
        cache.put(
            LocalTrackFeature(
                camera_id=cam,
                local_track_id=lid,
                frame_sequence=max(1, (t - T) // DT + 1),
                capture_time_ms=t,
                embedding=emb,
                visibility=PartVisibility.all_visible(),
                quality_score=0.9,
                orientation_bin=(1 + idx) % 8,
            ),
            keep_timeline=True,
        )


def _cluster(rig, name: str, t: int, xy: tuple[float, float], ids: dict[str, str]) -> ViewCluster:
    views = tuple(wt._view(rig, cam, lid, t, xy[0], xy[1]) for cam, lid in sorted(ids.items()))
    return ViewCluster(name, views, triangulate_skeleton([v.view for v in views], rig), ())


def _ids(tag: str) -> dict[str, str]:
    return {c: f"{c}-{tag}" for c in ALL}


def _run(mgr, rig, cache, t0: int, ticks: int, people=()):
    """t0 부터 ticks 시각 동안 people[(이름, 자리, 로컬 트랙, 외형 또는 None)]을 본다(빈 people 은 아무도 없음)."""
    frames = []
    for k in range(ticks):
        t = t0 + k * DT
        clusters = []
        for name, xy, ids, emb in people:
            if emb is not None and cache is not None:
                _put(cache, ids, t, emb)
            clusters.append(_cluster(rig, name, t, xy, ids))
        frames.append(mgr.update(wt._frame(rig, t, clusters), rig))
    return frames


def _reid_manager(session: str, **cfg) -> tuple[WorldTrackManager, LocalTrackFeatureCache]:
    cache = LocalTrackFeatureCache()
    return WorldTrackManager(WorldIdIssuer(session), WorldTrackConfig(reid_enabled=True, **cfg),
                             feature_lookup=cache.lookup), cache


def _same_person_two_ids(mgr, rig, cache) -> tuple[str, str, int]:
    """같은 사람 P 의 ID 를 둘(w0, w1) 만든다. 둘은 한 번도 동시 활성이 아니다.

    w0: P 로 1 s. 15 s 비움. w1: 겉옷(Q, P 와 유사도 0)으로 돌아와 새 ID(rejected_low_sim)를 받고, 지연 병합 창(6 s)이 지난 뒤
    겉옷을 벗어(P) 갤러리가 P 로 바뀐다. 실제 세션의 연쇄 ID 들처럼 두 ID 의 외형이 같다. (w0, w1, 마지막 시각)을 돌려준다.
    """
    f = _run(mgr, rig, cache, T, 10, [("P", CORNER, _ids("p0"), P)])
    w0 = f[-1].tracks[0].world_id
    t = T + 9 * DT
    _run(mgr, rig, cache, t + DT, GAP_MS // DT - 1)
    t += GAP_MS
    f = _run(mgr, rig, cache, t, 65, [("P", CORNER, _ids("p1"), Q)])
    (w1,) = (s.world_id for s in f[-1].tracks if s.updated)
    assert w1 != w0
    t += 65 * DT
    f = _run(mgr, rig, cache, t, 10, [("P", CORNER, _ids("p1"), P)])
    assert f[-1].track(w1) is not None and f[-1].track(w1).updated
    return w0, w1, t + 9 * DT


# ---------------------------------------------------------------- (a) 한 사람이 15 s 씩 네 번 들락날락


@pytest.mark.parametrize("window, expected_ids", [(20_000, 1), (10_000, 5)])
def test_one_person_four_returns_keeps_id_with_20s_window(rig, window, expected_ids):
    """(a) 재연결 창: 2대(cam1·cam2) 로컬 트래커가 옛 로컬 트랙을 이어 온 채 15 s 뒤 같은 자리로 돌아오면(화각 끝에서 골반이 가려
    World 위치만 없던 경우와 같은 근거) 20 s 창에서는 네 번 모두 같은 ID 다. 예전 10 s 창이면 돌아올 때마다 새 ID 였다(Re-ID 없음)."""
    mgr = WorldTrackManager(WorldIdIssuer("win"), WorldTrackConfig(reconnect_window_ms=window))
    f = _run(mgr, rig, None, T, 10, [("P", CORNER, _ids("p"), None)])
    wid = f[-1].tracks[0].world_id
    t = T + 9 * DT
    seen = []
    for n in range(4):
        _run(mgr, rig, None, t + DT, GAP_MS // DT - 1)  # 아무도 안 보임
        t += GAP_MS
        back = {"cam1": "cam1-p", "cam2": "cam2-p", "cam3": f"cam3-q{n}", "cam4": f"cam4-q{n}"}
        f = _run(mgr, rig, None, t, 10, [("P", CORNER, back, None)])
        (snap,) = f[-1].tracks
        seen.append(snap.world_id)
        if window == 20_000:
            assert wid in f[0].reassociations
        t += 9 * DT
    assert len(mgr.issuer.issued) == expected_ids
    assert (set(seen) == {wid}) is (window == 20_000)


@pytest.mark.parametrize("proven_only", [True, False])
def test_one_person_four_returns_reid_rival_rule_with_10s_window(rig, proven_only, caplog):
    """(a)(d) 창을 10 s 로 되돌려도(15 s 공백이면 DORMANT) Re-ID 재입장이 이어 준다. 같은 사람의 옛 ID 두 개(w0·w1, 동시 활성
    아님)가 같은 외형이라 예전 규칙은 둘이 서로의 경쟁자가 되어 마진 0 으로 거절하고 돌아올 때마다 새 ID 를 냈다(연쇄).
    새 규칙은 동시에 있은 적 없는 후보를 경쟁자로 세지 않아 네 번 모두 한 ID 로 잇고, 외형이 같은 옛 ID 들 중에서는 마지막으로
    쓰던 ID(w1)를 골라 돌아올 때마다 w0·w1 을 오가지 않는다."""
    mgr, cache = _reid_manager(f"rv{int(proven_only)}", reconnect_window_ms=10_000,
                               reid_reentry_rival_proven_only=proven_only)
    w0, w1, t = _same_person_two_ids(mgr, rig, cache)
    issued = len(mgr.issuer.issued)
    before = dict(mgr.diag)
    seen = []
    caplog.set_level(logging.INFO, logger="simai_ai.tracking.world_track")
    for n in range(4):
        _run(mgr, rig, cache, t + DT, GAP_MS // DT - 1)
        t += GAP_MS
        returning = _emb({0: 1.0, 900 + n: 0.05})  # 같은 사람 P(조금씩 다른 잡음), 매번 새 로컬 트랙
        f = _run(mgr, rig, cache, t, 15, [("P", CORNER, _ids(f"r{n}"), returning)])
        (snap,) = (s for s in f[-1].tracks if s.updated)
        seen.append(snap.world_id)
        t += 14 * DT

    def delta(key: str) -> int:
        return mgr.diag.get(key, 0) - before.get(key, 0)

    if proven_only:
        assert seen == [w1] * 4
        assert delta("reid.reentry_pick_recent") >= 1  # 첫 귀환은 유사도 동률이라 먼저 만든 w0 가 최선이었다
        assert len(mgr.issuer.issued) == issued  # 새 ID 없음
        assert delta("reid.reentry_linked") == 4 and delta("reid.reentry_reconnected") == 4
        assert delta("reid.reentry_linked_coactive_rule") == 4  # 예전 규칙이면 넷 다 마진으로 거절됐다
        assert delta("reid.reentry_rival_never_coactive") >= 4
        assert delta("reid.reentry_rejected_margin") == 0
        linked = [r.getMessage() for r in caplog.records if "decision=linked" in r.getMessage()]
        assert len(linked) == 4
        assert all("rival_kind=none" in m and "coactive_rule=True" in m and "never_coactive=w-" in m for m in linked)
    else:
        assert len(set(seen)) == 4 and not set(seen) & {w0, w1}  # 돌아올 때마다 새 ID (연쇄)
        assert len(mgr.issuer.issued) == issued + 4
        assert delta("reid.reentry_linked") == 0 and delta("reid.reentry_rejected_margin") >= 4
        assert delta("reid.reentry_rival_never_coactive") == 0


def test_late_feature_merge_uses_same_rival_rule(rig):
    """지연 도착 병합(_reid_late_reentry_merge)도 같은 경쟁자 규칙이다: 돌아온 첫 시각에 특징이 없어 새 ID 가 생겨도, 다음 시각
    특징이 오면 동시 활성이 아닌 옛 ID 두 개가 서로 막지 않아 마지막으로 쓰던 ID(w1)로 병합한다(MERGE 교정 기록)."""
    mgr, cache = _reid_manager("late")
    w0, w1, t = _same_person_two_ids(mgr, rig, cache)
    _run(mgr, rig, cache, t + DT, GAP_MS // DT - 1)
    t += GAP_MS
    ids = _ids("late")
    first = _run(mgr, rig, cache, t, 1, [("P", CORNER, ids, None)])[-1]  # 특징 아직 없음
    (fresh,) = (s.world_id for s in first.tracks if s.updated)
    assert fresh not in (w0, w1)
    f = _run(mgr, rig, cache, t + DT, 1, [("P", CORNER, ids, P)])[-1]
    (rec,) = f.revisions
    assert rec.kind is RevisionKind.MERGE and rec.previous_world_id == fresh and rec.world_id == w1
    assert mgr.diag["reid.reentry_merged"] == 1 and mgr.diag["reid.reentry_linked_coactive_rule"] == 1


# ---------------------------------------------------------------- (b) 함께 있던 다른 사람이 남아 있을 때


def test_coactive_other_stays_returning_person_gets_own_id(rig):
    """(b) A·B 가 함께 보인 뒤(동시 활성) A 만 15 s 나갔다 돌아오면 A 는 A 의 ID 다. 남아 있는 B 는 활성 경쟁자로 그대로 센다."""
    mgr, cache = _reid_manager("both")
    f = _run(mgr, rig, cache, T, 10, [("A", CORNER, _ids("a"), A), ("B", FAR, _ids("b"), B)])
    by_place = {round(s.position_xy[0]): s.world_id for s in f[-1].tracks}
    wa, wb = by_place[round(CORNER[0])], by_place[round(FAR[0])]
    t = T + 10 * DT
    _run(mgr, rig, cache, t, GAP_MS // DT - 1, [("B", FAR, _ids("b"), B)])  # A 없음, B 는 계속
    t += GAP_MS - DT
    f = _run(mgr, rig, cache, t, 5, [("A", CORNER, _ids("a2"), _emb({2: 1.0, 950: 0.05})), ("B", FAR, _ids("b"), B)])
    assert {s.world_id for s in f[-1].tracks if s.updated} == {wa, wb}
    assert f[0].track(wa).updated and wa in f[0].reassociations
    assert len(mgr.issuer.issued) == 2
    assert mgr.diag["reid.reentry_linked"] == 1 and mgr.diag["reid.reentry_linked_coactive_rule"] == 0


# ---------------------------------------------------------------- (c) 함께 있던 두 사람이 모두 나간 뒤


def _two_leave_then_ambiguous_return(rig, co_active: bool):
    """A·B 가 (함께 또는 따로) 보이고 모두 사라진 뒤, A 쪽에 조금 가깝지만 B 와도 마진 0.10 안인 외형이 돌아온다.

    돌아온 외형: A 와 0.70, B 와 0.66 (마진 0.04). (manager, A ID, B ID, 돌아온 시각의 프레임)을 돌려준다.
    """
    mgr, cache = _reid_manager(f"amb{int(co_active)}")
    if co_active:
        f = _run(mgr, rig, cache, T, 10, [("A", CORNER, _ids("a"), A), ("B", FAR, _ids("b"), B)])
        by_place = {round(s.position_xy[0]): s.world_id for s in f[-1].tracks}
        wa, wb = by_place[round(CORNER[0])], by_place[round(FAR[0])]
        t = T + 10 * DT
    else:
        f = _run(mgr, rig, cache, T, 10, [("A", CORNER, _ids("a"), A)])
        wa = f[-1].tracks[0].world_id
        t = T + 10 * DT
        _run(mgr, rig, cache, t, 50)  # A 가 사라지고 5 s 뒤에야 B 가 나타난다(한 번도 함께 측정되지 않음)
        t += 50 * DT
        f = _run(mgr, rig, cache, t, 10, [("B", FAR, _ids("b"), B)])
        (wb,) = (s.world_id for s in f[-1].tracks if s.updated)
        t += 10 * DT
    assert wa != wb
    _run(mgr, rig, cache, t, GAP_MS // DT - 1)
    t += GAP_MS - DT
    ambiguous = _emb({2: 0.70, 3: 0.66, 999: (1 - 0.70**2 - 0.66**2) ** 0.5})
    back = _run(mgr, rig, cache, t, 1, [("X", CORNER, _ids("x"), ambiguous)])[-1]
    return mgr, wa, wb, back


def test_coactive_pair_both_left_ambiguous_return_stays_conservative(rig):
    """(c) A·B 가 동시 활성이었으면 다른 사람으로 확인된 것이라 B 는 A 의 경쟁자다: 마진 0.04 < 0.10 이면 잇지 않고 새 ID(애매)."""
    mgr, wa, wb, back = _two_leave_then_ambiguous_return(rig, co_active=True)
    (snap,) = (s for s in back.tracks if s.updated)
    assert snap.world_id not in (wa, wb)
    assert IdentityFlag.REASSOCIATION_AMBIGUOUS in snap.identity_flags
    assert mgr.diag["reid.reentry_rejected_margin"] >= 1 and mgr.diag["reid.reentry_ambiguous"] >= 1
    assert mgr.diag.get("reid.reentry_linked", 0) == 0


def test_residual_never_coactive_pair_links_best_candidate(rig):
    """잔여 위험 고정(-802): 한 번도 함께 측정된 적 없는 A·B 는 다른 사람이라는 근거가 없어 서로의 경쟁자가 아니고, 마진(0.10) 안·
    하한(0.65) 이상이면 같은 사람의 옛 ID 로 보고 마지막으로 관측된 쪽(B)으로 잇는다. 돌아온 외형은 A 쪽에 더 가깝다(A 0.70,
    B 0.66). 돌아온 사람이 실제로 A 였다면 잘못 이은 것이다(외형이 0.65 넘게 닮은 두 사람이 한 번도 동시에 보이지 않고 차례로
    드나든 경우만. 예전 규칙은 새 ID 를 냈다)."""
    mgr, _wa, wb, back = _two_leave_then_ambiguous_return(rig, co_active=False)
    (snap,) = (s for s in back.tracks if s.updated)
    assert snap.world_id == wb
    assert mgr.diag["reid.reentry_pick_recent"] == 1
    assert mgr.diag["reid.reentry_linked"] == 1 and mgr.diag["reid.reentry_linked_coactive_rule"] == 1


# ---------------------------------------------------------------- 동시 활성 기록의 수명


def test_co_active_survives_merge_and_is_pruned_with_ids(rig):
    """동시 활성 기록은 병합하면 남는 ID 로 옮겨 가고, ID 가 장기 보류까지 끝나 사라지면 함께 지워진다(크기 상한)."""
    mgr, cache = _reid_manager("prune", reid_dormant_ttl_ms=60_000)
    f = _run(mgr, rig, cache, T, 10, [("A", CORNER, _ids("a"), A), ("B", FAR, _ids("b"), B)])
    wa, wb = sorted(s.world_id for s in f[-1].tracks)
    assert mgr._co_active == {wa: {wb}, wb: {wa}}
    # 병합: B 가 사라진 뒤 B 의 외형으로 늦게 특징이 온 새 Track 은 B 로 합쳐지고, A 와의 동시 활성 기록은 B 에 남는다
    t = T + 10 * DT
    _run(mgr, rig, cache, t, 40, [("A", CORNER, _ids("a"), A)])
    t += 40 * DT
    _run(mgr, rig, cache, t, 1, [("A", CORNER, _ids("a"), A), ("B", FAR, _ids("b2"), None)])
    f = _run(mgr, rig, cache, t + DT, 1, [("A", CORNER, _ids("a"), A), ("B", FAR, _ids("b2"), B)])[-1]
    (rec,) = f.revisions
    assert rec.world_id == wb
    assert mgr._co_active == {wa: {wb}, wb: {wa}}
    # 둘 다 사라지고 장기 보류 TTL(60 s)이 지나면 기록도 없다
    t += 2 * DT
    _run(mgr, rig, cache, t, 1)
    mgr.update(wt._frame(rig, t + 90_000, []), rig)
    assert mgr._co_active == {}
