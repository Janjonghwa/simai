"""-852 낙상 출생 보류 시험 (2026-11-23 리허설: 낙상 순간 0008 의 관측이 갈라져 0010 이 태어나고, 옛 ID 0007 로 이어져 되살아남).

자세 전이 중(posture_hint)인 확정 Track 의 birth_hold_radius_m(1.35 m) 안에서 태어난 Track 은 같은 카메라 별도 검출·관측 오차
설명과 관계없이 보류한다. 원래 Track 이 갱신되지 않는 동안 보류 Track 만 관측되면 원래 Track 으로 흡수한다. 자세 전이가 끝나면 일반
보류 규칙으로 돌아가고 상한(3 s) 뒤에는 내보낸다. 합성 장면만 쓴다(넓은 기선 4대, test_world_track.py).
"""

from __future__ import annotations

import pytest

from simai_ai.geometry.rig import CameraRig
from simai_ai.tracking.world_track import (
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
)
from tests.tracking.test_world_track_reentry import wt

T, DT = wt.T, wt.DT
X0, Y0 = 3.0, 3.0


@pytest.fixture(scope="module")
def rig():
    return CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def _mgr(**cfg) -> WorldTrackManager:
    return WorldTrackManager(WorldIdIssuer("f852"), WorldTrackConfig(**cfg))


def _confirmed(m, rig) -> str:
    """넘어질 사람 P1 을 (3, 3) 에서 네 대로 확정한다."""
    for k in range(5):
        t = T + k * DT
        m.update(wt._frame(rig, t, [wt._cluster(rig, "P1", t, X0, Y0)]), rig)
    return m.issuer.issued[0]


def _run(m, rig, k0, ticks, people, hint=None):
    """k0 부터 ticks 시각 동안 people[(이름, x, y, 로컬 접두사)] 를 본다. hint 가 있으면 매 시각 그 자세 전이 힌트를 넣는다."""
    frames = []
    for k in range(k0, k0 + ticks):
        t = T + k * DT
        if hint is not None:
            m.posture_hint = frozenset(hint(k))
        frames.append(m.update(wt._frame(rig, t, [wt._cluster(rig, n, t, x, y, prefix=p) for n, x, y, p in people]), rig))
    return frames


@pytest.mark.parametrize("falling", [True, False])
def test_split_observation_of_falling_person_is_held_and_absorbed(rig, falling):
    """넘어지는 P1 은 갱신되지 않고 0.85 m 옆에 새 로컬 트랙으로만 보인다(넓은 기선이라 관측 오차로 설명되지 않아 일반 보류는 하지
    않는 거리). 자세 전이 중이면 새 ID 를 내보내지 않고 P1 으로 흡수해 화면 ID 는 P1 하나다. 자세 전이가 아니면 이전처럼 새 ID 다."""
    m = _mgr()
    p1 = _confirmed(m, rig)
    hint = (lambda k: {p1}) if falling else (lambda k: ())
    frames = _run(m, rig, 5, 20, [("N", X0 + 0.85, Y0, "n")], hint)
    shown = {tr.world_id for f in frames for tr in f.tracks}
    if falling:
        assert shown == {p1}
        assert frames[-1].track(p1).updated
        assert m.diag["fall_hold.started"] == 1 and m.diag["birth_hold.absorbed"] == 1
        assert m.revisions == ()  # 내보낸 적 없는 ID 의 흡수는 교정 기록이 없다
    else:
        assert len(shown) == 2 and m.diag["fall_hold.started"] == 0


@pytest.mark.parametrize("falling", [True, False])
def test_body_seen_as_two_detections_is_held_until_cap(rig, falling):
    """넘어지는 사람이 같은 카메라들에 두 검출로 잡혀(P1 은 계속 갱신) 0.6 m 옆에 새 Track 이 생긴다. 일반 규칙은 태어난 시각에
    내보낸다(넓은 기선이라 관측 오차로 설명되지 않고, 같은 카메라 별도 검출은 다른 사람 근거). 자세 전이 중이면 상한(3 s)까지 내보내지 않고, 그 뒤에는 내보내되 합치지 않는다."""
    m = _mgr()
    p1 = _confirmed(m, rig)
    hint = (lambda k: {p1}) if falling else (lambda k: ())
    frames = _run(m, rig, 5, 40, [("P1", X0, Y0, "p"), ("N", X0 + 0.6, Y0, "n")], hint)
    dup = m.issuer.issued[1]
    first_shown = next(i for i, f in enumerate(frames) if f.track(dup) is not None)
    if falling:
        assert first_shown * DT == WorldTrackConfig().fall_birth_hold_max_ms
        assert m.diag["birth_hold.released_fall_timeout"] == 1
    else:
        assert first_shown == 0 and m.diag["birth_hold.started"] == 0
    assert all(f.track(p1).updated for f in frames)
    assert m.revisions == () and m.diag["birth_hold.absorbed"] == 0


def test_posture_end_releases_hold_by_normal_rule(rig):
    """자세 전이가 1 s 만에 끝나면(일어남) 일반 보류 규칙으로 돌아가 같은 카메라 별도 검출 근거로 바로 내보낸다."""
    m = _mgr()
    p1 = _confirmed(m, rig)
    frames = _run(m, rig, 5, 20, [("P1", X0, Y0, "p"), ("N", X0 + 0.6, Y0, "n")],
                  lambda k: {p1} if k < 15 else ())
    dup = m.issuer.issued[1]
    first_shown = next(i for i, f in enumerate(frames) if f.track(dup) is not None)
    assert first_shown == 10  # 힌트가 빠진 첫 시각
    assert m.diag["fall_hold.ended"] == 1 and m.diag["birth_hold.released_distinct"] == 1


def test_helper_walking_in_from_afar_is_shown_normally(rig):
    """늦게 다가와 돕는 사람은 멀리서 태어나므로(4 m 밖) 보류 없이 바로 나가고, 넘어진 사람 옆 0.5 m 까지 와도 그대로 보인다."""
    m = _mgr()
    p1 = _confirmed(m, rig)
    frames = []
    for i in range(36):
        x = X0 + max(4.0 - 0.1 * i, 0.5)
        frames.extend(_run(m, rig, 5 + i, 1, [("P1", X0, Y0, "p"), ("H", x, Y0, "h")], lambda k: {p1}))
    helper = m.issuer.issued[1]
    assert all(f.track(helper) is not None and f.track(helper).updated for f in frames)
    assert m.diag["fall_hold.started"] == 0 and m.diag["birth_hold.absorbed"] == 0


def test_disabled_keeps_previous_behaviour(rig):
    """fall_birth_hold_enabled=False 면 힌트가 있어도 이전 동작(같은 카메라 별도 검출이면 태어난 시각에 나감)."""
    m = _mgr(fall_birth_hold_enabled=False)
    p1 = _confirmed(m, rig)
    frames = _run(m, rig, 5, 5, [("P1", X0, Y0, "p"), ("N", X0 + 0.6, Y0, "n")], lambda k: {p1})
    assert frames[0].track(m.issuer.issued[1]) is not None
    assert m.diag["fall_hold.started"] == 0
