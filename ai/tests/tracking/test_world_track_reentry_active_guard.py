"""활성 Track 이 한 시각 빠져도 옛 ID 를 되살리지 않는 Re-ID 재입장 경쟁자 시험 (명세 5.2 Re-ID 보조, S15P31A101-848).

증상(2026-11-23 리허설, 한 사람): 낙상 직후 새 Track(0010)의 지연 병합이 05:14:36.846 에 최선 후보 옛 ID 0007(0.743)을 지금
활성 Track 0008(0.822)에 막혀 거절했다(rejected_margin, rival_kind=active). 바로 다음 시각(36.944) 0008 이 위치 측정을 받지
못해 활성 경쟁자에서 빠지고(지연 병합 후보에서도 '태어난 뒤 측정된 Track' 이라 빠짐) 경쟁자가 다른 옛 ID 0005(0.559)로 바뀌자
같은 대상을 linked 로 다시 판정해 0007 이 되살아났다. 고친 것(reid_reentry_active_guard):
1. 이번 시각만 측정이 빠진 확정 Track(마지막 측정 1 s 안)도 활성 경쟁자로 센다.
2. 같은 판정 대상(로컬 트랙·World ID)이 지연 병합 창 동안 본 활성 경쟁자 유사도를 기억해 다시 센다.
모든 입력은 손으로 만든 SIM 이다(합성 카메라·골격은 test_world_track.py, 외형은 두 축 평면의 단위 벡터).
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pytest

from tests.tracking.test_world_track_reentry import (
    CORNER,
    DT,
    FAR,
    T,
    _ids,
    _reid_manager,
    _run,
    wt,
)

LOG_SIM_OLD = 0.743
"""로그의 옛 ID(0007) 유사도"""
LOG_SIM_ACTIVE = 0.822
"""로그의 활성 Track(0008) 유사도"""
GAP_MS = 5_000
"""옛 ID 가 사라졌다 판정 대상이 나타나기까지. LOST(3 s)는 넘고 재연결 창(20 s) 안이라 옛 ID 는 소실 후보로 남는다"""


@pytest.fixture(scope="module")
def rig():
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def _unit(deg: float, dim: int = 1280) -> np.ndarray:
    """기저 0·1 평면에서 기저 0 과 deg 도 벌어진 단위 벡터. 두 벡터의 코사인 유사도는 cos(각도 차)."""
    v = np.zeros(dim, dtype=np.float32)
    v[0], v[1] = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return v


SUBJ_DEG = math.degrees(math.acos(LOG_SIM_OLD))
OLD = _unit(0.0)
"""옛 ID 의 외형"""
SUBJ = _unit(SUBJ_DEG)
"""판정 대상(낙상 직후 생긴 새 Track)의 외형: 옛 ID 와 0.743"""
ACT = _unit(SUBJ_DEG + math.degrees(math.acos(LOG_SIM_ACTIVE)))
"""활성 Track 의 외형: 판정 대상과 0.822, 옛 ID 와 약 0.23"""
RETURNING = _unit(-math.degrees(math.acos(0.90)))
"""진짜 재입장한 옛 사람: 옛 ID 와 0.90, 활성 Track 과는 0 아래"""


def _scene(rig, session: str, subject_emb: np.ndarray, active_ticks: int, after_ticks: int, feature_from: int = 2,
           **cfg):
    """옛 사람 O(CORNER 1 s) → 5 s 비움(그동안 활성 사람 A 가 FAR 에) → 판정 대상이 CORNER 에 새 로컬 트랙으로 나타난다.

    판정 대상의 Re-ID 특징은 feature_from 시각부터 온다(2 면 첫 시각 새 ID 뒤 지연 병합, 0 이면 첫 시각 즉시 재입장). A 는 판정 대상의 처음 active_ticks 시각 동안만 보이고
    그 뒤 측정되지 않는다(로그의 0008). (관리자, 옛 ID, 활성 ID, 판정 대상 ID 들, 지연 병합 전 diag)를 돌려준다.
    """
    mgr, cache = _reid_manager(session, **cfg)
    f = _run(mgr, rig, cache, T, 10, [("O", CORNER, _ids("o"), OLD)])
    (w_old,) = (s.world_id for s in f[-1].tracks)
    t = T + 10 * DT
    f = _run(mgr, rig, cache, t, GAP_MS // DT, [("A", FAR, _ids("a"), ACT)])
    (w_act,) = (s.world_id for s in f[-1].tracks if s.updated)
    assert w_act != w_old
    t += GAP_MS
    before = dict(mgr.diag)
    subj_ids = _ids("s")
    seen: list[str] = []
    for k in range(active_ticks + after_ticks):
        people = [("S", CORNER, subj_ids, subject_emb if k >= feature_from else None)]
        if k < active_ticks:
            people.append(("A", FAR, _ids("a"), ACT))
        f = _run(mgr, rig, cache, t + k * DT, 1, people)
        (snap,) = (s for s in f[-1].tracks if s.updated and math.dist(s.position_xy, CORNER) < 0.5)
        seen.append(snap.world_id)
    return mgr, w_old, w_act, seen, before


def _delta(mgr, before, key: str) -> int:
    return mgr.diag.get(key, 0) - before.get(key, 0)


@pytest.mark.parametrize("guard", [True, False])
def test_log_case_active_rival_missing_one_tick_does_not_revive_old_id(rig, guard, caplog):
    """(a) 로그 재현: 판정 대상이 활성 Track(0.822)을 옛 ID(0.743)보다 닮았다. 활성 Track 이 다음 시각 측정되지 않아도 옛 ID 를
    되살리지 않는다(새 규칙). 이전 규칙(guard=False)은 로그처럼 경쟁자가 바뀌어 옛 ID 로 잇는다."""
    caplog.set_level(logging.INFO, logger="simai_ai.tracking.world_track")
    mgr, w_old, w_act, seen, before = _scene(rig, f"g{int(guard)}", SUBJ, active_ticks=3, after_ticks=7,
                                             reid_reentry_active_guard=guard)
    decisions = [r.getMessage() for r in caplog.records if "site=late_merge" in r.getMessage()]
    first = decisions[0]
    assert "decision=rejected_margin" in first and f"best={w_old}" in first
    assert f"rival={w_act}" in first and "rival_kind=active" in first and "sim=0.743" in first
    assert "rival_sim=0.822" in first
    if guard:
        assert w_old not in seen  # 옛 ID 를 되살리지 않는다
        assert _delta(mgr, before, "reid.reentry_merged") == 0
        assert not any("decision=linked" in m for m in decisions)
        assert all(f"rival={w_act}" in m for m in decisions)  # 경쟁자를 바꿔 다시 따지지 않는다
        assert _delta(mgr, before, "reid.reentry_active_recent") >= 1
    else:
        assert seen[-1] == w_old  # 로그의 0007 부활
        linked = [m for m in decisions if "decision=linked" in m]
        assert len(linked) == 1 and f"rival={w_act}" not in linked[0]
        assert _delta(mgr, before, "reid.reentry_merged") == 1


def test_remembered_active_rival_outlives_recent_window(rig):
    """(a) 활성 Track 이 1 s(출생 보류 근거 기준) 넘게 측정되지 않아 '이번 시각만 빠진 Track' 으로도 못 셀 때, 같은 대상이 앞서 본
    활성 유사도 기억(지연 병합 창 6 s)이 계속 막는다. 기립 키 표본을 모자라게 두어 매 시각 다시 묻게 한다(로그의 낙상 자세와 같음)."""
    mgr, w_old, _w_act, seen, before = _scene(rig, "mem", SUBJ, active_ticks=3, after_ticks=35, stature_min_samples=1000)
    assert w_old not in seen
    assert _delta(mgr, before, "reid.reentry_merged") == 0
    assert _delta(mgr, before, "reid.reentry_active_remembered") >= 1
    assert _delta(mgr, before, "reid.reentry_rejected_margin") >= 30


@pytest.mark.parametrize("active_ticks", [3, 40])
def test_genuine_reentry_unlike_active_track_still_links(rig, active_ticks):
    """(b) 활성 Track 과 닮지 않은 진짜 재입장(옛 ID 0.90, 활성 Track 0 아래)은 활성 Track 이 빠지든 계속 보이든 옛 ID 로 잇는다."""
    mgr, w_old, _w_act, seen, before = _scene(rig, f"ok{active_ticks}", RETURNING, active_ticks=active_ticks,
                                             after_ticks=5)
    assert seen[-1] == w_old
    assert _delta(mgr, before, "reid.reentry_merged") == 1
    assert _delta(mgr, before, "reid.reentry_rejected_margin") == 0


@pytest.mark.parametrize("active_ticks", [0, 40])
def test_genuine_reconnect_like_log_0005_still_links(rig, active_ticks, caplog):
    """(b) 로그 05:14:51 의 0005(실제로 방에 있던 다른 작업자, 사용자 확인)와 같은 즉시 재입장(site=reconnect): 첫 시각부터
    특징이 있고 옛 ID 와 0.90, 활성 Track 과는 닮지 않았다(로그는 rival 0002 coactive 0.262). 활성 Track 이 바로 직전에만
    측정됐든(active_ticks=0, 이번 시각만 빠진 Track) 계속 보이든 그대로 옛 ID 로 잇는다(새 ID 없음)."""
    caplog.set_level(logging.INFO, logger="simai_ai.tracking.world_track")
    mgr, w_old, _w_act, seen, before = _scene(rig, f"rc{active_ticks}", RETURNING, active_ticks=active_ticks,
                                             after_ticks=5, feature_from=0)
    assert seen[0] == w_old and set(seen) == {w_old}
    assert _delta(mgr, before, "reid.reentry_reconnected") == 1
    linked = [r.getMessage() for r in caplog.records if "site=reconnect decision=linked" in r.getMessage()]
    assert len(linked) == 1 and f"best={w_old}" in linked[0]
