"""-657 R4 중복 발생 방지·2대 관측 기립 회복 시험 (명세 7.2 R4, 5.2 head_z, 14차: 낙상 1회에 R4 두 건·4분 미해소).

장면 도구는 test_r4_fall_suspect.py 의 것을 쓰고, 위치·골반·카메라·별칭만 더한다.
"""

import importlib.util
from pathlib import Path

from simai_ai.risk import (
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    Origin,
    RiskEngine,
    default_registry,
)
from simai_ai.risk.rules.r4_fall_suspect import (
    REASON_HEAD_UNCERTAIN_RECOVERY,
    REASON_TWIN_SUSPECT,
)

_spec = importlib.util.spec_from_file_location("_r4", Path(__file__).with_name("test_r4_fall_suspect.py"))
r4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r4)
T0, STEP = r4.T0, r4.STEP


def doc(t, head, *, wid="w-0001", xy=(1.0, 1.0), cams=("cam1", "cam2"), pelvis=None, validity=None, aliases=None,
        support=0.0, revision=0):
    d = r4.track(head, t=t, world_id=wid, cameras=cams, validity=validity, support=support, revision=revision)
    d["position_xy"] = list(xy)
    if pelvis is not None:
        d["pelvis_xyz"] = [xy[0], xy[1], pelvis]
    if aliases:
        d["aliases"] = list(aliases)
    return d


def run(scene):
    """scene: [(오프셋, [트랙 문서 만드는 함수(t) …])]."""
    clock = FixedClock(T0)
    engine = RiskEngine([default_registry().create("R4", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    out = []
    for dt, makers in scene:
        t = T0 + dt
        state = r4.world([m(t) for m in makers], t)
        clock.set(t + 100)
        evaluation = engine.evaluate(state, [], Origin.LIVE)
        step = tracker.apply(evaluation, state, config_version="cfg-1")
        out.append((dt, evaluation.findings, step.transitions))
    return out, tracker


def raised(result):
    return [(dt, tr) for dt, _, trs in result for tr in trs if tr.transition is Lifecycle.RAISED]


def resolved(result):
    return [(dt, tr) for dt, _, trs in result for tr in trs if tr.transition is Lifecycle.RESOLVED]


def head_of(dt, start=1000, end=1800, top=1.75, bottom=0.25):
    if dt <= start:
        return top
    if dt >= end:
        return bottom
    return top + (bottom - top) * (dt - start) / (end - start)


def _twin_scene(twin_cams, twin_xy=(1.6, 1.4)):
    """A(w-0059)가 서 있다가 1.0~1.8 s 에 넘어진다. 넘어지는 도중(1.4 s, 머리 약 1.0 m) 분신 w-0061 이 태어나 같은 높이를 따라간다."""
    scene = []
    for dt in range(0, 6000, STEP):
        h = head_of(dt)
        makers = [lambda t, h=h: doc(t, h, wid="w-0059", cams=("camA", "camD"))]
        if dt >= 1400:
            makers.append(lambda t, h=h: doc(t, h, wid="w-0061", xy=twin_xy, cams=twin_cams))
        scene.append((dt, makers))
    return scene


def test_view_split_twin_born_mid_fall_does_not_raise_a_second_r4():
    """14차 장면: 낙상 도중 카메라가 겹치지 않는 분신이 태어나 같은 낙상을 보여도 R4 는 원래 사람 하나만 발생한다."""
    result, _ = run(_twin_scene(("camB", "camC")))
    assert [tr.state.world_ids for _, tr in raised(result)] == [("w-0059",)]
    held = [f for _, fs, _ in result for f in fs if f.world_ids == ("w-0061",) and REASON_TWIN_SUSPECT in f.reasons]
    assert held and all(f.holding for f in held)


def test_two_people_falling_together_seen_by_shared_cameras_both_raise():
    """안전장치: 같은 카메라에 함께 보인(카메라가 겹치는) 두 사람의 동시 낙상은 둘 다 발생한다."""
    result, _ = run(_twin_scene(("camA", "camB")))
    assert sorted(tr.state.world_ids for _, tr in raised(result)) == [("w-0059",), ("w-0061",)]


def test_far_twin_candidate_is_not_suppressed():
    """반경(1.35 m) 밖에서 넘어진 사람은 분신이 아니다."""
    result, _ = run(_twin_scene(("camB", "camC"), twin_xy=(3.0, 1.0)))
    assert len(raised(result)) == 2


def _fall_then_two_view_stand(pelvis_when_up):
    """넘어져 누웠다가(6 s) 2대 관측(UNCERTAIN 머리)으로 일어서 머리 1.72 m 를 유지한다."""
    scene = []
    for dt in range(0, 6000, STEP):
        h = head_of(dt)
        scene.append((dt, [lambda t, h=h: doc(t, h, validity="VALID")]))
    for dt in range(6000, 20000, STEP):
        scene.append((dt, [lambda t: doc(t, 1.72, validity="UNCERTAIN", pelvis=pelvis_when_up)]))
    return scene


def test_two_view_uncertain_standing_with_observed_pelvis_resolves_within_ten_seconds():
    """2대 관측으로 머리가 UNCERTAIN 이어도 관측 골반이 함께 기립(0.95 m)을 보이면 회복을 세고, 일어선 뒤 10 s 안에 해소한다."""
    result, _ = run(_fall_then_two_view_stand(0.95))
    assert len(raised(result)) == 1
    ((dt, _),) = resolved(result)
    assert 6000 < dt <= 6000 + 10_000


def test_uncertain_head_alone_does_not_resolve():
    """머리만 높고 골반을 모르면(또는 앉아 골반이 낮으면) UNCERTAIN 머리만으로는 해소하지 않는다(명세 5.2)."""
    for pelvis in (None, 0.5):
        result, _ = run(_fall_then_two_view_stand(pelvis))
        assert len(raised(result)) == 1 and resolved(result) == []
        assert any(REASON_HEAD_UNCERTAIN_RECOVERY in f.reasons for _, fs, _ in result for f in fs)


def test_merged_away_id_does_not_raise_observation_lost():
    """병합으로 사라진 ID(남는 ID 의 aliases)는 '급하강 직후 관측 상실'로 새 R4 를 내지 않는다."""
    scene = []
    for dt in range(0, 3000, STEP):
        h = head_of(dt)
        makers = [lambda t, h=h: doc(t, h, wid="w-0059", cams=("camA", "camD"))]
        if dt < 1600:
            makers.append(lambda t, h=h: doc(t, h, wid="w-0061", xy=(1.3, 1.0), cams=("camB", "camC")))
        else:
            makers = [lambda t, h=h: doc(t, h, wid="w-0059", cams=("camA", "camD"), aliases=["w-0061"], revision=1)]
        scene.append((dt, makers))
    result, _ = run(scene)
    assert all(f.world_ids != ("w-0061",) or f.holding for dt, fs, _ in result if dt >= 1600 for f in fs)
    assert all(tr.state.world_ids != ("w-0061",) for _, tr in raised(result))
