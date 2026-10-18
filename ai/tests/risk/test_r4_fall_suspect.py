"""R4 낙상 의심 단위 시험 (명세 7.2 R4, 5.2 head_z·관절 게이트, 7.1, 6.4, 부록 D-G6-02).

장면은 10 Hz 관측 열(관측 시각 오프셋 → 트랙)로 만들고 등록부 R4 와 기본 생명주기(3프레임 중 2, 해소 3초)에 넣는다.
머리 높이는 World Z(절대), 접지 Z 는 ground_anchor_xyz 의 Z 다.
"""

import pytest

from simai_ai.risk import (
    ConfidenceReason,
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    Origin,
    RiskEngine,
    Severity,
    Verification,
    WorldState,
    default_registry,
)
from simai_ai.risk.rules.r4_fall_suspect import (
    R4_HEAD_TOP_OFFSET_M,
    REASON_HEAD_DROP,
    REASON_HEAD_UNAVAILABLE,
    REASON_HEAD_UNCERTAIN,
    REASON_INPUT_UNKNOWN,
    REASON_LOST_AFTER_DROP,
    REASON_LOW_POSTURE,
    REASON_LOW_SUSTAINED,
    REASON_SUPPORT_HEAD_CHANGE,
    REASON_SUPPORT_LAST_KNOWN,
    REASON_SUPPORT_UNKNOWN,
    REASON_TRACK_MISSING,
    REASON_TRACK_NOT_OBSERVED,
    FallSuspectRule,
)

T0 = 1760342400000
STEP = 100
FLOOR = 0.0
HEIGHT = 1.70

# 표식: 트랙 대신 이 값을 넣으면 그 시각 world_state 에서 사람이 빠진다
ABSENT = object()


def track(
    head=HEIGHT,
    *,
    support=FLOOR,
    t=T0,
    state="OBSERVED",
    revision=0,
    world_id="w-0001",
    skeleton=None,
    cameras=("cam1", "cam2"),
    validity=None,
):
    """world_track JSON. head=None 이면 head_z null, support=None 이면 접지 모름(Z=0 으로 채우지 않음).

    validity 를 주면 head_z_validity(world_state 1.4.0)를 싣고, None 이면 싣지 않는다(이전 버전 문서).
    """
    anchor = None if support is None else [1.0, 1.0, support]
    doc = {
        "world_id": world_id,
        "t_last_valid": t,
        "observation_state": state,
        "track_lifecycle": "CONFIRMED",
        "identity_confidence": "HIGH",
        "identity_revision": revision,
        "position_xy": [1.0, 1.0],
        "position_xy_sigma": [0.05, 0.05],
        "pelvis_xyz": None,
        "ground_anchor_xyz": anchor,
        "support_surface_id": None if support is None else "floor",
        "support_surface_validity": "UNKNOWN" if support is None else "VALID",
        "contact_validity": "UNAVAILABLE" if support is None else "VALID",
        "z_validity": "UNAVAILABLE" if support is None else "VALID",
        "head_z": head,
        "cameras": list(cameras),
    }
    if skeleton is not None:
        doc["skeleton"] = skeleton
    if validity is not None:
        doc["head_z_validity"] = validity
    return doc


def head_skeleton(head_top, validity="UNCERTAIN", source="observed"):
    """귀·눈만 있는 COCO17 skeleton. 귀·눈 Z = head_top − 오프셋."""
    z = head_top - R4_HEAD_TOP_OFFSET_M
    joints = [None] * 17
    valid = ["UNAVAILABLE"] * 17
    for j in (1, 2, 3, 4):
        joints[j] = [1.0, 1.0, z]
        valid[j] = validity
    return {
        "joint_layout": "COCO17",
        "joints": joints,
        "visible": [j is not None for j in joints],
        "validity": valid,
        "joint_source": [source] * 17,
    }


def world(tracks, t, calibration="cal-1"):
    return WorldState.from_payload(
        {"t_latest_global": t, "calibration_version": calibration, "scene_version": "scene-1", "tracks": tracks}
    )


def ramp(start_ms, end_ms, a, b):
    """start~end(포함) 10 Hz 표본의 (오프셋, 값). 선형."""
    out = []
    n = (end_ms - start_ms) // STEP
    for i in range(n + 1):
        f = i / n if n else 1.0
        out.append((start_ms + i * STEP, a + (b - a) * f))
    return out


def hold(start_ms, end_ms, value):
    return [(t, value) for t in range(start_ms, end_ms + 1, STEP)]


def frames(profile, **kwargs):
    """(오프셋, 머리 높이) 열 → (오프셋, 트랙 kwargs) 열. 같은 오프셋은 뒤의 것이 이긴다."""
    seen = {}
    for dt, head in profile:
        seen[dt] = {"head": head, **kwargs}
    return sorted(seen.items())


def run(scene, *, rule=None, origin=Origin.LIVE, lag_ms=100):
    """scene: [(오프셋, 트랙 kwargs | ABSENT | [트랙 kwargs, …])]. 단계별 (오프셋, 판정, 전이)와 추적기를 돌려준다."""
    clock = FixedClock(T0)
    engine = RiskEngine([rule if rule is not None else default_registry().create("R4", 1)], clock)
    tracker = LifecycleTracker(origin=origin, gateway_id="tier0-01", clock=clock)
    out = []
    for dt, spec in scene:
        t = T0 + dt
        if spec is ABSENT:
            tracks = []
        elif isinstance(spec, list):
            tracks = [track(t=t, **s) for s in spec]
        else:
            tracks = [track(t=t, **spec)]
        state = world(tracks, t)
        clock.set(t + lag_ms)
        evaluation = engine.evaluate(state, [], origin)
        step = tracker.apply(evaluation, state, config_version="cfg-1")
        out.append((dt, evaluation.findings, step.transitions, step.local_signals))
    return out, tracker


def transitions(result):
    return [(dt, tr) for dt, _, trs, _ in result for tr in trs]


def raised(result):
    return [(dt, tr) for dt, tr in transitions(result) if tr.transition is Lifecycle.RAISED]


def resolved(result):
    return [(dt, tr) for dt, tr in transitions(result) if tr.transition is Lifecycle.RESOLVED]


def raising_findings(result):
    return [(dt, f) for dt, fs, _, _ in result for f in fs if not f.holding]


def fall(start=1000, duration=800, top=HEIGHT, bottom=0.2, lie_until=6000, **kwargs):
    """서 있다(start 까지) duration 동안 넘어져 lie_until 까지 누운 장면."""
    end = start + duration
    profile = hold(0, start, top) + ramp(start, end, top, bottom) + hold(end, lie_until, bottom)
    return frames(profile, **kwargs)


# --- 발생 ---------------------------------------------------------------------------


def test_fall_on_floor_raises_critical_after_low_hold_and_resolves_when_standing():
    scene = fall() + frames(hold(6100, 12000, HEIGHT))
    result, _ = run(scene)
    ((dt, tr),) = raised(result)
    # 머리 0.6 m 이하가 처음 된 표본(1600 ms: 0.2+1.5×(0.25)=… 아래 계산)에서 2초 유지 + 지속 조건 2프레임째
    first_low = next(d for d, h in fall() if d > 1000 and h["head"] <= 0.6)
    assert dt == first_low + 2000 + STEP
    assert tr.state.severity is Severity.CRITICAL and tr.state.rule_id == "R4"
    assert tr.state.confidence_reasons == () and tr.state.needs_check is False
    assert tr.state.zone_id is None and tr.state.world_ids == ("w-0001",)
    assert tr.state.reasons == (REASON_HEAD_DROP, REASON_LOW_SUSTAINED)
    assert tr.state.metrics["head_drop_m"] >= 0.8
    # 일어선 뒤 판정 없이 3초 → 해소
    ((dt_res, _),) = resolved(result)
    assert dt_res == 6100 + 3000


def test_fall_rings_local_alarm_only_in_live():
    for origin in Origin:
        result, _ = run(fall(), origin=origin)
        assert len(raised(result)) == 1  # 같은 규칙을 네 입력에 적용한다
        rang = [s for _, _, _, sigs in result for s in sigs]
        if origin is Origin.LIVE:
            assert [type(s).__name__ for s in rang] == ["LocalAlert"]  # CRITICAL: 노트북 로컬 경보(7.3)
        else:
            assert rang == []  # PLANNED·WHAT_IF·REPLAY 는 알림 채널로 보내지 않는다


def test_fall_on_podium_uses_podium_top():
    # 40 cm 단상 위에서 옆으로 넘어짐: 머리 2.10 → 0.70 (절대 높이는 0.6 m 를 넘지만 지면 기준 0.30 m)
    result, _ = run(fall(top=2.10, bottom=0.70, support=0.4))
    ((_, tr),) = raised(result)
    assert tr.state.confidence_reasons == ()
    assert tr.state.metrics["head_height_m"] == pytest.approx(0.30)


def test_fall_off_podium_with_unknown_landing_support_uses_last_support():
    # 단상(0.4)에서 바닥으로 떨어져 접지를 잃음: 마지막 접지 0.4 기준으로 −0.25 m → 낮은 자세, 확인 필요(Z_UNKNOWN)
    profile = hold(0, 1000, 2.10) + ramp(1000, 1800, 2.10, 0.15) + hold(1800, 6000, 0.15)
    scene = [(dt, {"head": h, "support": 0.4 if dt <= 1000 else None}) for dt, h in profile]
    result, _ = run(scene)
    ((_, tr),) = raised(result)
    assert tr.state.confidence_reasons == (ConfidenceReason.Z_UNKNOWN,)
    assert REASON_SUPPORT_LAST_KNOWN in tr.state.reasons
    assert tr.state.metrics["head_height_m"] == pytest.approx(-0.25)


def test_fall_with_support_never_known_uses_head_change():
    # 접지를 한 번도 모름: 바닥 Z=0 으로 채우지 않고 급하강 전 머리 높이에서 내려간 양(1.5 m)으로 본다
    result, _ = run(fall(support=None))
    ((_, tr),) = raised(result)
    assert tr.state.confidence_reasons == (ConfidenceReason.Z_UNKNOWN,)
    assert REASON_SUPPORT_HEAD_CHANGE in tr.state.reasons
    assert "head_height_m" not in tr.state.metrics


def test_uncertain_head_joints_raise_fall_with_joint_uncertain():
    # head_z 는 VALID 일 때만 실린다(null). skeleton 의 UNCERTAIN 귀·눈으로 판정하되 확인 필요(JOINT_UNCERTAIN), 등급 유지.
    profile = hold(0, 1000, HEIGHT) + ramp(1000, 1800, HEIGHT, 0.2) + hold(1800, 6000, 0.2)
    scene = [(dt, {"head": None, "skeleton": head_skeleton(h)}) for dt, h in profile]
    result, _ = run(scene)
    ((_, tr),) = raised(result)
    assert tr.state.severity is Severity.CRITICAL
    assert tr.state.confidence_reasons == (ConfidenceReason.JOINT_UNCERTAIN,)
    assert tr.state.metrics["head_height_m"] == pytest.approx(0.2)


def test_uncertain_drop_then_valid_lying_keeps_joint_uncertain():
    # 급하강을 UNCERTAIN 머리로만 봤으면 누운 뒤 VALID 가 되어도 근거가 불확실하다: JOINT_UNCERTAIN 유지
    profile = hold(0, 1000, HEIGHT) + ramp(1000, 1800, HEIGHT, 0.2) + hold(1800, 6000, 0.2)
    scene = [
        (dt, {"head": None, "skeleton": head_skeleton(h)} if dt <= 1800 else {"head": h}) for dt, h in profile
    ]
    result, _ = run(scene)
    ((_, tr),) = raised(result)
    assert tr.state.confidence_reasons == (ConfidenceReason.JOINT_UNCERTAIN,)


def test_inferred_head_joints_are_not_used():
    # HMR 추정(inferred) 귀·눈은 쓰지 않는다(규칙 1): 머리 높이를 모름 → 유지만, 발생 없음
    profile = hold(0, 1000, HEIGHT) + ramp(1000, 1800, HEIGHT, 0.2) + hold(1800, 6000, 0.2)
    scene = [(dt, {"head": None, "skeleton": head_skeleton(h, "VALID", "inferred")}) for dt, h in profile]
    result, _ = run(scene)
    assert raised(result) == []
    assert all(f.reasons == (REASON_INPUT_UNKNOWN, REASON_HEAD_UNAVAILABLE) for _, fs, _, _ in result for f in fs)


# --- 급하강 직후 관측 상실 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("lost", "code"),
    [
        ({"state": "UNKNOWN"}, REASON_TRACK_NOT_OBSERVED),
        ({"state": "PREDICTED"}, REASON_TRACK_NOT_OBSERVED),
        ({"head": None}, REASON_HEAD_UNAVAILABLE),
        (ABSENT, REASON_TRACK_MISSING),
    ],
    ids=["unknown", "predicted", "head-null", "track-missing"],
)
def test_observation_lost_right_after_drop_raises_and_never_resolves(lost, code):
    # 머리 1.70 → 0.85 (0.5초, 하강 0.85 m)까지 보이고 책상 뒤로 사라짐 → 바로 발생(PREDICTED), 상실 중 해소 없음
    profile = hold(0, 1000, HEIGHT) + ramp(1000, 1500, HEIGHT, 0.85)
    scene = frames(profile) + [(dt, lost) for dt in range(1600, 9000, STEP)]
    result, _ = run(scene)
    ((dt, tr),) = raised(result)
    assert dt == 1700  # 상실 첫 판정 + 지속 조건 2프레임째
    assert tr.state.severity is Severity.CRITICAL
    # 트랙을 잃으면 PREDICTED(위치가 마지막 추정값), 트랙은 보이는데 머리 근거만 잃으면 JOINT_UNCERTAIN
    expected = ConfidenceReason.JOINT_UNCERTAIN if code == REASON_HEAD_UNAVAILABLE else ConfidenceReason.PREDICTED
    assert expected in tr.state.confidence_reasons and tr.state.needs_check
    assert tr.state.reasons == (REASON_HEAD_DROP, REASON_LOST_AFTER_DROP, code)
    assert resolved(result) == []
    last = transitions(result)[-1][1]
    if lost is not ABSENT and lost.get("head", 1) is None:
        assert last.state.verification is Verification.VERIFIED  # 트랙은 보이지만 머리를 모른다: 발생 판정이 해소를 막는다
    else:
        assert last.state.verification is Verification.UNVERIFIABLE


def test_lost_then_seen_standing_resolves_and_seen_lying_keeps_event():
    profile = hold(0, 1000, HEIGHT) + ramp(1000, 1500, HEIGHT, 0.85)
    lost = [(dt, {"state": "UNKNOWN"}) for dt in range(1600, 3000, STEP)]
    standing = frames(hold(3000, 8000, HEIGHT))
    result, _ = run(frames(profile) + lost + standing)
    assert len(raised(result)) == 1
    ((dt, _),) = resolved(result)
    assert dt == 3000 + 3000
    lying = frames(hold(3000, 12000, 0.2))
    result, _ = run(frames(profile) + lost + lying)
    assert len(raised(result)) == 1 and resolved(result) == []
    # 다시 보이면 낮은 자세 유지 판정으로 이어진다
    assert raising_findings(result)[-1][1].reasons == (REASON_HEAD_DROP, REASON_LOW_SUSTAINED)


def test_loss_long_after_drop_without_low_posture_does_not_raise():
    # 급하강(빠르게 바닥에 앉기, 머리 0.9) 뒤 1.5초 지나 가려짐: 급하강 '직후'가 아니고 낮은 자세도 아니었다 → 발생 없음
    profile = hold(0, 1000, HEIGHT) + ramp(1000, 1800, HEIGHT, 0.9) + hold(1800, 3300, 0.9)
    scene = frames(profile) + [(dt, {"state": "UNKNOWN"}) for dt in range(3400, 6000, STEP)]
    result, _ = run(scene)
    assert raised(result) == []


def test_missing_track_memory_expires():
    rule = FallSuspectRule(lost_memory_ms=1000)
    profile = hold(0, 1000, HEIGHT) + ramp(1000, 1500, HEIGHT, 0.85)
    scene = frames(profile) + [(dt, ABSENT) for dt in range(1600, 4000, STEP)]
    result, _ = run(scene, rule=rule)
    fired = [dt for dt, f in raising_findings(result)]
    assert fired[0] == 1600 and fired[-1] == 2500  # 마지막 관측(1500) + 1초까지만 낸다
    assert resolved(result) == []  # 이벤트는 lifecycle 이 UNVERIFIABLE 로 유지한다


# --- 오경보 대조 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile",
    [
        # 빠른 앉기(의자): 0.4초에 1.70 → 1.25
        hold(0, 1000, HEIGHT) + ramp(1000, 1400, HEIGHT, 1.25) + hold(1400, 8000, 1.25),
        # 쪼그려 줍기: 0.6초에 1.10, 1.5초 뒤 일어남
        hold(0, 1000, HEIGHT) + ramp(1000, 1600, HEIGHT, 1.10) + hold(1600, 3100, 1.10) + ramp(3100, 3700, 1.10, HEIGHT)
        + hold(3700, 8000, HEIGHT),
        # 허리 굽혀 줍기: 0.5초에 1.00
        hold(0, 1000, HEIGHT) + ramp(1000, 1500, HEIGHT, 1.00) + hold(1500, 2500, 1.00) + ramp(2500, 3000, 1.0, HEIGHT)
        + hold(3000, 8000, HEIGHT),
        # 웅크림 유지
        hold(0, 1000, HEIGHT) + ramp(1000, 1700, HEIGHT, 1.10) + hold(1700, 15000, 1.10),
        # 빠르게 바닥에 앉기(0.8초에 0.9, 하강 0.8 m → 후보지만 낮은 자세 아님)
        hold(0, 1000, HEIGHT) + ramp(1000, 1800, HEIGHT, 0.90) + hold(1800, 12000, 0.90),
        # 의도적으로 눕기: 1.5초에 앉고 1초 뒤 2초에 걸쳐 누움
        hold(0, 1000, HEIGHT) + ramp(1000, 2500, HEIGHT, 0.90) + hold(2500, 3500, 0.90) + ramp(3500, 5500, 0.90, 0.20)
        + hold(5500, 15000, 0.20),
        # 휘청였다 바로 일어남(급하강 후 1초 안에 일어섬)
        hold(0, 1000, HEIGHT) + ramp(1000, 1600, HEIGHT, 0.40) + ramp(1600, 2600, 0.40, HEIGHT)
        + hold(2600, 8000, HEIGHT),
    ],
    ids=["fast-sit", "squat-pickup", "stoop-pickup", "crouch", "fast-floor-sit", "deliberate-lie", "stumble-recover"],
)
def test_normal_postures_do_not_raise(profile):
    result, _ = run(frames(profile))
    assert raised(result) == []
    assert raising_findings(result) == []


def test_deliberate_lying_only_holds():
    # 의도적으로 누운 사람은 발생 없이 낮은 자세 유지(holding)만 낸다(활성 이벤트가 있을 때만 의미가 있다)
    profile = hold(0, 1000, HEIGHT) + ramp(1000, 4000, HEIGHT, 0.2) + hold(4000, 8000, 0.2)
    result, _ = run(frames(profile))
    last = result[-1][1]
    assert [(f.holding, f.reasons) for f in last] == [(True, (REASON_LOW_POSTURE,))]


@pytest.mark.parametrize("support_from", [(0.4, 0.0), (0.75, 0.0)], ids=["podium-step-down", "desk-jump-down"])
def test_stepping_down_lowers_support_too(support_from):
    # 단상에서 내려오기(머리 0.4 m 하강)·책상에서 뛰어내려 쪼그려 착지(머리 1.25 m 하강, 후보): 접지도 내려가 지면 기준
    # 머리 높이는 낮은 자세가 아니다
    top, floor = support_from
    stand_top = top + HEIGHT
    landing = floor + (1.20 if top > 0.5 else HEIGHT)
    profile = hold(0, 1000, stand_top) + ramp(1000, 1600, stand_top, landing) + hold(1600, 8000, landing)
    scene = [(dt, {"head": h, "support": top if dt < 1300 else floor}) for dt, h in profile]
    result, _ = run(scene)
    assert raised(result) == []
    assert raising_findings(result) == []


def test_jump_down_with_support_unknown_after_landing_is_bounded():
    # 반례(알려진 한계): 책상(0.75)에서 뛰어내린 뒤 접지를 모르면 마지막 접지(상판) 기준으로 낮은 자세로 보인다.
    # 접지가 1초 안에 돌아오면 유지 시간 전이라 발생하지 않는다.
    profile = hold(0, 1000, 2.45) + ramp(1000, 1600, 2.45, 1.20) + hold(1600, 8000, 1.20)
    scene = [(dt, {"head": h, "support": 0.75 if dt < 1300 else (None if dt < 2600 else 0.0)}) for dt, h in profile]
    result, _ = run(scene)
    assert raised(result) == []
    # 접지를 2초 넘게 모르면 발생하되 확인 필요(Z_UNKNOWN)를 붙인다(대체 기준임을 드러낸다)
    scene = [(dt, {"head": h, "support": 0.75 if dt < 1300 else None}) for dt, h in profile]
    result, _ = run(scene)
    ((_, tr),) = raised(result)
    assert ConfidenceReason.Z_UNKNOWN in tr.state.confidence_reasons


# --- 모르는 상태로 해소하지 않음 --------------------------------------------------------


def test_active_fall_not_resolved_while_head_unknown():
    # 발생 뒤 트랙은 OBSERVED 인데 머리 높이를 모름(head_z null, skeleton 없음) 5초: 해소하지 않는다
    scene = fall() + [(dt, {"head": None}) for dt in range(6100, 11100, STEP)]
    result, _ = run(scene)
    assert len(raised(result)) == 1 and resolved(result) == []


def test_active_fall_not_resolved_while_support_unknown_and_no_history():
    # 발생 뒤 새 규칙 인스턴스(재시작·구성 전환)로 이력이 없고 접지도 모름: 유지 판정으로 해소를 막는다
    result, tracker = run(fall())
    assert len(raised(result)) == 1
    fresh = FallSuspectRule()
    clock = FixedClock(T0 + 7000)
    engine = RiskEngine([fresh], clock)
    for dt in range(7000, 12000, STEP):
        t = T0 + dt
        state = world([track(head=1.7, support=None, t=t)], t)
        clock.set(t + 100)
        step = tracker.apply(engine.evaluate(state, [], Origin.LIVE), state, config_version="cfg-1")
        assert all(tr.transition is not Lifecycle.RESOLVED for tr in step.transitions)
    (f,) = engine.evaluate(state, [], Origin.LIVE).findings
    assert f.holding and f.reasons == (REASON_INPUT_UNKNOWN, REASON_SUPPORT_UNKNOWN)
    assert f.confidence_reasons == (ConfidenceReason.Z_UNKNOWN,)


def test_lying_person_after_restart_is_held_without_history():
    # 이력 없는 새 인스턴스: 누워 있는 사람에게 low_posture 유지 판정(새 이벤트는 만들지 않음)
    rule = FallSuspectRule()
    result, _ = run(frames(hold(0, 5000, 0.2)), rule=rule)
    assert raised(result) == []
    assert all(f.holding and f.reasons == (REASON_LOW_POSTURE,) for _, fs, _, _ in result for f in fs)


def test_head_flicker_while_standing_does_not_raise():
    # head_z 가 VALID·null 을 오가도(관절 게이트 깜빡임) 서 있는 사람에게 발생하지 않는다. null 표본은 유지만.
    scene = [(dt, {"head": HEIGHT if (dt // STEP) % 2 == 0 else None}) for dt in range(0, 8000, STEP)]
    result, _ = run(scene)
    assert raised(result) == []
    assert all(f.holding for _, fs, _, _ in result for f in fs)


def test_head_flicker_while_falling_still_raises():
    # 넘어지는 동안 머리가 깜빡여도(표본 절반 null) 놓치지 않는다
    scene = [(dt, {"head": h if (dt // STEP) % 2 == 0 else None}) for dt, h in [(d, k["head"]) for d, k in fall()]]
    result, _ = run(scene)
    assert len(raised(result)) == 1


# --- 상태 관리 -----------------------------------------------------------------------


def test_reevaluating_same_observation_is_idempotent():
    rule = FallSuspectRule()
    clock = FixedClock(T0)
    engine = RiskEngine([rule], clock)
    outs = []
    for dt, spec in fall():
        t = T0 + dt
        state = world([track(t=t, **spec)], t)
        clock.set(t + 100)
        a = engine.evaluate(state, [], Origin.LIVE).findings
        clock.set(t + 150)
        b = engine.evaluate(state, [], Origin.LIVE).findings  # 같은 관측을 한 번 더 판정
        assert a == b
        outs.append(a)
    plain, _ = run(fall())
    assert outs == [fs for _, fs, _, _ in plain]


def test_time_going_back_resets_history():
    # 다른 재생이 같은 인스턴스에 들어와 시간이 뒤로 가면 이력을 버린다(급하강이 섞이지 않는다)
    rule = FallSuspectRule()
    clock = FixedClock(T0)
    engine = RiskEngine([rule], clock)
    for dt, h in hold(0, 1000, HEIGHT):
        engine.evaluate(world([track(head=h, t=T0 + dt)], T0 + dt), [], Origin.REPLAY)
    # 처음으로 되감아 0.2 m 로 시작: 이전 1.70 표본과 이어 급하강으로 보지 않는다
    for dt, h in hold(0, 4000, 0.2):
        findings = engine.evaluate(world([track(head=h, t=T0 + dt)], T0 + dt), [], Origin.REPLAY).findings
        assert all(f.holding for f in findings)


def test_identity_revision_resets_person_history():
    # 1.70 을 보던 ID 가 교정(revision 1)된 뒤 0.2 로 보이면 다른 사람일 수 있다: 급하강으로 보지 않는다
    scene = frames(hold(0, 1000, HEIGHT)) + frames(hold(1100, 6000, 0.2), revision=1)
    result, _ = run(scene)
    assert raised(result) == []


def test_people_are_independent():
    scene = [
        (dt, [{"head": spec["head"], "world_id": "w-0001"}, {"head": HEIGHT, "world_id": "w-0002"}])
        for dt, spec in fall()
    ]
    result, _ = run(scene)
    ((_, tr),) = raised(result)
    assert tr.state.world_ids == ("w-0001",)


def test_single_view_and_identity_reasons_are_carried():
    result, _ = run(fall(cameras=("cam1",)))
    ((_, tr),) = raised(result)
    assert tr.state.confidence_reasons == (ConfidenceReason.SINGLE_VIEW,)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"drop_window_ms": 0},
        {"drop_min_m": 0.0},
        {"low_raise_m": 0.9, "low_clear_m": 0.8},
        {"fallback_low_drop_m": 0.5, "fallback_clear_drop_m": 0.8},
        {"low_hold_ms": True},
        {"head_top_offset_m": float("nan")},
    ],
)
def test_bad_config_is_rejected(kwargs):
    from simai_ai.risk._parse import RiskInputError

    with pytest.raises(RiskInputError):
        FallSuspectRule(**kwargs)


def test_registry_builds_fresh_instances():
    registry = default_registry()
    a, b = registry.create("R4", 1), registry.create("R4", 1)
    assert a is not b and a.spec.key == ("R4", 1)


# --- 리뷰 F1: 구성 묶음 전환이 진행 중인 낙상 후보를 잃지 않는다 ---------------------------------


def _bundle_kit():
    import runpy
    from pathlib import Path

    conftest = Path(__file__).resolve().parents[1] / "config" / "conftest.py"
    return runpy.run_path(str(conftest))["BundleKit"]()


def _run_config_sync(scene, switch_at=(), calibration=lambda version: "cal-stable"):
    """실제 BundleVerifier → ConfigSync 경로(R4 v1 만 등록). switch_at 시각마다 같은 R4 v1 의 새 묶음을 발행·전환한다."""
    from simai_ai.config import ConfigSync

    kit = _bundle_kit()
    sync = ConfigSync(kit.verifier(), kit.clock, source_id="tier0-01")
    sync.on_connected()
    versions = iter(f"cfg{i}" for i in range(1, 100))
    first = next(versions)
    notice = kit.publish(first, rule_versions=(("R4", 1),), calibration_version=calibration(first))
    assert sync.receive(notice).accepted
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=kit.clock)
    out, engines = [], set()
    for dt, spec in scene:
        t = T0 + dt
        kit.clock.set(t + 100)
        if dt in switch_at:
            version = next(versions)
            notice = kit.publish(version, rule_versions=(("R4", 1),), calibration_version=calibration(version))
            assert sync.receive(notice).accepted
        active = sync.begin_tick()
        engines.add(id(active.engine))
        state = world([track(t=t, **spec)], t, calibration=active.calibration_version)  # Tier 0 는 묶음의 보정으로 낸다
        evaluation = active.evaluate(state, Origin.LIVE)
        step = tracker.apply(evaluation, state, config_version=active.config_version)
        out.append((dt, evaluation.findings, step.transitions, step.local_signals))
    return out, len(engines)


def test_review_f1_config_switch_mid_fall_keeps_candidate():
    # 리뷰 재현: 2000 ms(넘어진 직후, 낮은 자세 유지 중)에 같은 R4 v1·같은 보정의 새 묶음으로 전환해도 3700 ms 에 발생한다
    baseline, _ = _run_config_sync(fall())
    assert [dt for dt, _ in raised(baseline)] == [3700]
    switched, engines = _run_config_sync(fall(), switch_at={2000})
    assert engines == 2  # 실제로 다른 엔진(다른 규칙 인스턴스)으로 바뀌었다
    assert [dt for dt, _ in raised(switched)] == [3700]


@pytest.mark.parametrize("switch", [900, 1000, 1400, 1800, 1900, 2500, 3600, 3700, 4000])
def test_config_switch_just_before_during_after_fall(switch):
    # 반례: 넘어지기 직전·하강 중·하강 끝·유지 중·발생 직전·발생 프레임·발생 뒤 어느 시각에 전환해도 결과가 같다
    result, engines = _run_config_sync(fall(), switch_at={switch})
    assert engines == 2
    assert [dt for dt, _ in raised(result)] == [3700]


def test_config_switch_with_new_calibration_resets_history():
    # 보정이 바뀌면 World 좌표가 달라 이전 머리 높이와 비교하지 않는다. 보정 변경으로 머리가 1.2 m 내려가 보여도
    # 급하강으로 보지 않는다(같은 보정이면 발생하는 장면).
    scene = frames(hold(0, 2000, HEIGHT)) + frames(hold(2100, 7000, 0.5))
    same, _ = _run_config_sync(scene, switch_at={2100})
    assert len(raised(same)) == 1
    changed, _ = _run_config_sync(scene, switch_at={2100}, calibration=lambda version: f"cal-{version}")
    assert raised(changed) == []


# --- 리뷰 F2: 출처끼리 이력이 섞이지 않는다 -----------------------------------------------------


def _live_after(seed):
    """seed(clock) 가 준비한 엔진에 LIVE 로 처음부터 누운 사람 5초를 넣는다. (발생 시각, 현장 경보 신호 수)."""
    clock = FixedClock(T0)
    engine = seed(clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    opened, signals = [], 0
    for dt in range(100, 5000, STEP):
        t = T0 + dt
        clock.set(t + 100)
        state = world([track(head=0.2, t=t)], t)
        step = tracker.apply(engine.evaluate(state, [], Origin.LIVE), state, config_version="cfg")
        opened += [dt for tr in step.transitions if tr.transition is Lifecycle.RAISED]
        signals += len(step.local_signals)
    return opened, signals


@pytest.mark.parametrize("other", [Origin.REPLAY, Origin.WHAT_IF, Origin.PLANNED])
def test_review_f2_other_origin_history_does_not_reach_live(other):
    # 리뷰 재현: 같은 엔진에 다른 출처의 서 있는 한 프레임을 넣은 뒤 LIVE 에 누운 사람만 들어와도 급하강이 아니다
    def seeded(clock):
        engine = RiskEngine([FallSuspectRule()], clock)
        engine.evaluate(world([track(head=HEIGHT, t=T0)], T0), [], other)
        return engine

    assert _live_after(lambda clock: RiskEngine([FallSuspectRule()], clock)) == ([], 0)
    assert _live_after(seeded) == ([], 0)


def test_interleaved_replay_and_live_same_world_id_are_independent():
    # 반례: 같은 world_id 를 REPLAY(계속 서 있음)와 LIVE(넘어짐)가 번갈아 쓴다. LIVE 는 혼자일 때와 같은 시각에 발생하고
    # REPLAY 는 판정 없음이다. REPLAY 관측 시각이 늘 조금 앞서므로 LIVE 시각은 REPLAY 보다 뒤로 간다(출처마다 따로 잰다).
    alone, _ = run(fall())
    clock = FixedClock(T0)
    engine = RiskEngine([FallSuspectRule()], clock)
    live = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    replay = LifecycleTracker(origin=Origin.REPLAY, gateway_id="tier0-01", clock=clock)
    live_raised, replay_findings = [], []
    for dt, spec in fall():
        t = T0 + dt
        clock.set(t + 100)
        r_state = world([track(head=HEIGHT, t=t + 50)], t + 50)
        r_eval = engine.evaluate(r_state, [], Origin.REPLAY)
        replay.apply(r_eval, r_state, config_version="cfg")
        replay_findings += list(r_eval.findings)
        l_state = world([track(t=t, **spec)], t)
        step = live.apply(engine.evaluate(l_state, [], Origin.LIVE), l_state, config_version="cfg")
        live_raised += [dt for tr in step.transitions if tr.transition is Lifecycle.RAISED]
    assert live_raised == [dt for dt, _ in raised(alone)]
    assert replay_findings == []


def test_engines_sharing_one_rule_object_do_not_share_history():
    # 반례: 한 규칙 객체를 두 엔진이 공유해도(규칙 객체는 상태가 없다) 기억은 엔진마다 따로다
    rule = FallSuspectRule()

    def seeded(clock):
        other = RiskEngine([rule], clock)
        other.evaluate(world([track(head=HEIGHT, t=T0)], T0), [], Origin.LIVE)
        return RiskEngine([rule], clock)

    assert _live_after(seeded) == ([], 0)


# --- 리뷰 F3: 급하강 뒤 후속 창 경계 -------------------------------------------------------------


def _late_low(first_low):
    """1.70 → 0.85 를 1000~1500 ms 에 내려가 2000 ms 까지 0.85(마지막 급하강 표본 2000 ms), 띠(0.79 m) 뒤 first_low 부터 0.50."""
    profile = (
        hold(0, 1000, HEIGHT) + ramp(1000, 1500, HEIGHT, 0.85) + hold(1500, 2000, 0.85)
        + hold(2100, first_low - STEP, 0.79) + hold(first_low, 7500, 0.50)
    )  # fmt: skip
    rule = FallSuspectRule()
    clock = FixedClock(T0)
    engine = RiskEngine([rule], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    opened, last_drop = [], None
    for dt, spec in frames(profile):
        t = T0 + dt
        clock.set(t + 100)
        state = world([track(t=t, **spec)], t)
        step = tracker.apply(engine.evaluate(state, [], Origin.LIVE), state, config_version="cfg")
        opened += [dt for tr in step.transitions if tr.transition is Lifecycle.RAISED]
        session = engine.state.view(Origin.LIVE).slot(rule.spec.key, lambda: None)
        memory = session.memory.get("w-0001")
        if memory is not None and memory.phase.value != "NONE":
            last_drop = memory.t_drop - T0
    return opened, last_drop


def test_review_f3_followup_window_boundary():
    # 리뷰 재현: 마지막 급하강 2000 ms 에서 정확히 2.0초(4000 ms) 뒤 시작한 낮은 자세는 받고, 2.1초(4100 ms)는 받지 않는다
    opened, last_drop = _late_low(4000)
    assert last_drop == 2000
    assert opened == [6100]
    assert _late_low(4100)[0] == []
    assert _late_low(4200)[0] == []


def test_low_started_inside_window_may_hold_past_window():
    # 반례: 창 안(1.9초)에 시작한 낮은 자세는 유지 시간이 창 밖으로 넘어가도 발생한다(창은 낮은 자세의 시작에만 적용)
    assert _late_low(3900)[0] == [6000]


# --- 권장: 머리 꼭대기 오프셋 단일화 -------------------------------------------------------------


def test_head_top_offset_matches_tier0_joint_gate():
    # R4 가 UNCERTAIN 귀·눈으로 다시 만드는 머리 높이는 Tier 0 head_z 와 같은 오프셋이어야 한다(단차 방지).
    # head_z_validity(1.4.0) 뒤에도 UNCERTAIN 값은 계약에 없어 이 경로가 남는다(모듈 설명).
    pytest.importorskip("numpy")
    from simai_ai.geometry.joint_gate import JointGateConfig

    assert JointGateConfig().head_top_offset_m == R4_HEAD_TOP_OFFSET_M


# --- world_state 1.4.0 head_z_validity (S15P31A101-177) -------------------------------------------


def v14(head, validity=None, **kwargs):
    """1.4.0 문서의 트랙 kwargs. validity 를 안 주면 head 가 있으면 VALID, 없으면 UNAVAILABLE."""
    if validity is None:
        validity = "VALID" if head is not None else "UNAVAILABLE"
    return {"head": head, "validity": validity, **kwargs}


def uncertain(head_top, **kwargs):
    """1.4.0 UNCERTAIN 표본: head_z null, skeleton 귀·눈 UNCERTAIN(Tier 0 게이트가 낸 그대로)."""
    return {"head": None, "validity": "UNCERTAIN", "skeleton": head_skeleton(head_top), **kwargs}


def no_value(validity):
    """값 없는 1.4.0 표본(UNAVAILABLE, 또는 귀·눈 좌표가 실리지 않은 UNCERTAIN)."""
    return {"head": None, "validity": validity}


FALL_PROFILE = hold(0, 1000, HEIGHT) + ramp(1000, 1800, HEIGHT, 0.2) + hold(1800, 6000, 0.2)


def summary(result):
    """비교용: 발생·해소 전이(시각, 전이, 확인 필요 이유, 근거 코드)."""
    return [
        (dt, tr.transition, tr.state.confidence_reasons, tr.state.reasons)
        for dt, tr in transitions(result)
        if tr.transition in (Lifecycle.RAISED, Lifecycle.RESOLVED)
    ]


def test_v14_valid_fall_uses_head_z_and_does_not_reread_skeleton():
    # VALID 면 head_z 를 그대로 쓴다. 이전 버전 문서였다면 'skeleton 에 VALID 귀·눈이 없음'으로 UNCERTAIN 이 됐을 장면에서도
    # head_z_validity 가 VALID 라고 하므로 확정 근거다(skeleton 을 다시 보지 않는다).
    no_valid_ears = head_skeleton(HEIGHT, validity="UNAVAILABLE")
    result, _ = run([(dt, v14(h, skeleton=no_valid_ears)) for dt, h in FALL_PROFILE])
    ((dt, tr),) = raised(result)
    assert tr.state.confidence_reasons == () and tr.state.reasons == (REASON_HEAD_DROP, REASON_LOW_SUSTAINED)
    legacy, _ = run([(dt, {"head": h, "skeleton": no_valid_ears}) for dt, h in FALL_PROFILE])
    ((dt_legacy, tr_legacy),) = raised(legacy)
    assert dt_legacy == dt and tr_legacy.state.confidence_reasons == (ConfidenceReason.JOINT_UNCERTAIN,)


def test_v14_uncertain_head_raises_fall_with_joint_uncertain_like_legacy():
    # 단상 낙상처럼 머리 근거가 내내 UNCERTAIN(계약에 값 없음): skeleton UNCERTAIN 귀·눈으로 다시 만들어 '낙상 의심 + 확인
    # 필요'(CRITICAL 유지). 이전 버전 문서(head_z null + skeleton UNCERTAIN)와 발생·해소 전이가 같다.
    profile = hold(0, 1000, 2.10) + ramp(1000, 1800, 2.10, 0.55) + hold(1800, 6000, 0.55)
    standing = hold(6100, 10000, 2.10)
    result, _ = run(
        [(dt, uncertain(h, support=0.4)) for dt, h in profile] + frames(standing, validity="VALID", support=0.4)
    )
    ((_, tr),) = raised(result)
    assert tr.state.severity is Severity.CRITICAL
    assert tr.state.confidence_reasons == (ConfidenceReason.JOINT_UNCERTAIN,)
    assert tr.state.metrics["head_height_m"] == pytest.approx(0.15)
    legacy, _ = run(
        [(dt, {"head": None, "skeleton": head_skeleton(h), "support": 0.4}) for dt, h in profile]
        + frames(standing, support=0.4)
    )
    assert summary(result) == summary(legacy)
    assert transitions(result)[-1][1].transition is Lifecycle.RESOLVED


@pytest.mark.parametrize("ears", [("VALID", "observed"), ("UNCERTAIN", "inferred")], ids=["valid-ears", "inferred"])
def test_v14_uncertain_uses_only_observed_uncertain_ears(ears):
    # UNCERTAIN 은 게이트의 UNCERTAIN 값(관측 UNCERTAIN 귀·눈)만 다시 만든다. 어긋난 skeleton(VALID 귀·눈만)이나 추정
    # (inferred) 귀·눈으로는 만들지 않고 머리 높이를 모름(head_uncertain)으로 둔다: 넘어지는 귀·눈이 보여도 발생 없음, 유지만.
    validity, source = ears
    scene = [
        (dt, {"head": None, "validity": "UNCERTAIN", "skeleton": head_skeleton(h, validity, source)})
        for dt, h in FALL_PROFILE
    ]
    result, _ = run(scene)
    assert raised(result) == []
    assert {f.reasons for _, fs, _, _ in result for f in fs} == {(REASON_INPUT_UNKNOWN, REASON_HEAD_UNCERTAIN)}
    assert all(f.holding and ConfidenceReason.JOINT_UNCERTAIN in f.confidence_reasons for _, fs, _, _ in result for f in fs)


def test_v14_unavailable_is_not_rebuilt_from_skeleton():
    # UNAVAILABLE 이면 skeleton 에 귀·눈 좌표가 있어도 다시 만들지 않는다(송신 측이 값을 못 냈다고 했다). 이전 버전 문서였다면
    # VALID 귀·눈으로 다시 만들어 발생했을 장면이다(대조).
    profile = ramp(1100, 1800, 1.5, 0.2) + hold(1800, 6000, 0.2)
    result, _ = run(
        frames(hold(0, 1000, HEIGHT), validity="VALID")
        + [(dt, v14(None, skeleton=head_skeleton(h, "VALID"))) for dt, h in profile]
    )
    assert raised(result) == []
    assert {f.reasons for _, fs, _, _ in result for f in fs} == {(REASON_INPUT_UNKNOWN, REASON_HEAD_UNAVAILABLE)}
    legacy, _ = run(
        frames(hold(0, 1000, HEIGHT)) + [(dt, {"head": None, "skeleton": head_skeleton(h, "VALID")}) for dt, h in profile]
    )
    assert len(raised(legacy)) == 1


@pytest.mark.parametrize("validity", ["UNAVAILABLE", "UNCERTAIN"])
def test_v14_head_lost_right_after_drop_raises_joint_uncertain_and_holds(validity):
    # 반례: 급하강(VALID) 직후 머리 근거가 UNAVAILABLE(또는 귀·눈 값 없는 UNCERTAIN)이 됨 → 바로 발생(급하강 직후 관측 상실,
    # JOINT_UNCERTAIN). 근거가 없는 동안 해소하지 않고, 서 있는 VALID 머리가 보이면 판정 없이 3초 뒤 해소.
    code = REASON_HEAD_UNAVAILABLE if validity == "UNAVAILABLE" else REASON_HEAD_UNCERTAIN
    drop = frames(hold(0, 1000, HEIGHT) + ramp(1000, 1500, HEIGHT, 0.85), validity="VALID")
    lost = [(dt, no_value(validity)) for dt in range(1600, 8000, STEP)]
    standing = frames(hold(8000, 12000, HEIGHT), validity="VALID")
    result, _ = run(drop + lost + standing)
    ((dt, tr),) = raised(result)
    assert dt == 1700  # 상실 첫 판정 + 지속 조건 2프레임째
    assert tr.state.confidence_reasons == (ConfidenceReason.JOINT_UNCERTAIN,) and tr.state.needs_check
    assert tr.state.reasons == (REASON_HEAD_DROP, REASON_LOST_AFTER_DROP, code)
    assert tr.state.verification is Verification.VERIFIED  # 트랙은 보인다: 발생 판정이 해소를 막는다
    ((dt_res, _),) = resolved(result)
    assert dt_res == 8000 + 3000


@pytest.mark.parametrize("validity", ["UNAVAILABLE", "UNCERTAIN"])
def test_v14_head_unknown_after_fall_never_resolves(validity):
    # 반례: 발생 뒤 머리 근거가 UNAVAILABLE·값 없는 UNCERTAIN 5초 → 모르는 상태로 해소하지 않는다(6.4)
    scene = fall(validity="VALID") + [(dt, no_value(validity)) for dt in range(6100, 11100, STEP)]
    result, _ = run(scene)
    assert len(raised(result)) == 1 and resolved(result) == []


def _gap(kind, head_top):
    if kind == "uncertain-skeleton":
        return uncertain(head_top)
    return no_value("UNCERTAIN" if kind == "uncertain-no-value" else "UNAVAILABLE")


GAPS = ["uncertain-skeleton", "uncertain-no-value", "unavailable"]


@pytest.mark.parametrize("gap", GAPS)
def test_v14_valid_uncertain_flicker_while_falling_still_raises(gap):
    # 반례: VALID → UNCERTAIN → VALID 깜빡임(표본 절반) 중 넘어짐. 놓치지 않는다. UNCERTAIN 값이 급하강·낮은 자세 근거에
    # 섞였으면 확인 필요다. 값 없는 표본은 급하강 직후 관측 상실(JOINT_UNCERTAIN)로 먼저 발생 판정을 내고, 다음 VALID 표본이
    # 낮은 자세를 확인하면 그 근거(VALID)로 잇는다(이전 버전 문서의 head_z null 깜빡임과 같다).
    scene = [(dt, v14(h) if (dt // STEP) % 2 == 0 else _gap(gap, h)) for dt, h in FALL_PROFILE]
    result, _ = run(scene)
    ((dt, tr),) = raised(result)
    assert tr.state.severity is Severity.CRITICAL
    plain, _ = run(frames(FALL_PROFILE, validity="VALID"))
    assert dt <= raised(plain)[0][0]  # 깜빡임이 발생을 늦추지 않는다
    if gap == "uncertain-skeleton":
        assert ConfidenceReason.JOINT_UNCERTAIN in tr.state.confidence_reasons
    else:
        lost = [f for _, fs, _, _ in result for f in fs if REASON_LOST_AFTER_DROP in f.reasons]
        assert lost and all(ConfidenceReason.JOINT_UNCERTAIN in f.confidence_reasons for f in lost)


@pytest.mark.parametrize("gap", GAPS)
def test_v14_valid_uncertain_flicker_while_standing_does_not_raise(gap):
    # 반례: 서 있는 사람의 VALID·UNCERTAIN·UNAVAILABLE 깜빡임은 발생하지 않는다. 다시 만든 UNCERTAIN 값이 Tier 0 와 같은
    # 오프셋이라 VALID head_z 와 단차(가짜 급하강)가 없다.
    scene = [(dt, v14(HEIGHT) if (dt // STEP) % 2 == 0 else _gap(gap, HEIGHT)) for dt in range(0, 8000, STEP)]
    result, _ = run(scene)
    assert raised(result) == []
    assert raising_findings(result) == []


def test_v14_uncertain_gap_during_low_hold_keeps_fall():
    # 반례: 급하강·낮은 자세(VALID) 뒤 유지 시간 중 1초 동안 UNCERTAIN(귀·눈 값 있음)이 끼어도 발생 시각이 같고 확인 필요가 붙는다
    plain, _ = run(fall(validity="VALID"))
    ((dt_plain, _),) = raised(plain)
    scene = [(dt, uncertain(spec["head"]) if 2500 <= dt < 3500 else spec) for dt, spec in fall(validity="VALID")]
    result, _ = run(scene)
    ((dt, tr),) = raised(result)
    assert dt == dt_plain
    assert tr.state.confidence_reasons == (ConfidenceReason.JOINT_UNCERTAIN,)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"head_z": None, "head_z_validity": "VALID"}, "VALID"),
        ({"head_z": 1.7, "head_z_validity": "UNCERTAIN"}, "UNCERTAIN"),
        ({"head_z": 1.7, "head_z_validity": "UNAVAILABLE"}, "UNAVAILABLE"),
        ({"head_z": None, "head_z_sigma": 0.02, "head_z_validity": "UNCERTAIN"}, "head_z_sigma"),
        ({"head_z": 1.7, "head_z_validity": None}, "head_z_validity"),
        ({"head_z": 1.7, "head_z_validity": "valid"}, "head_z_validity"),
    ],
    ids=["valid-null", "uncertain-value", "unavailable-value", "uncertain-sigma", "explicit-null", "bad-enum"],
)
def test_v14_contract_violations_are_rejected(fields, message):
    # 반례(계약 위반 입력): head_z_validity 와 head_z 가 어긋나면 지어내지 않고 입력 거부(스키마 조건부 규칙과 같다)
    from simai_ai.risk._parse import RiskInputError

    doc = track(t=T0)
    doc.update(fields)
    with pytest.raises(RiskInputError, match=message):
        world([doc], T0)


def test_v14_valid_without_value_bypassing_parser_is_unknown_not_invented():
    # 반례: 파서를 우회해 VALID 인데 head_z 가 없는 트랙이 들어와도 값을 지어내지 않고 모름(head_unavailable)으로 둔다
    import dataclasses

    engine = RiskEngine([FallSuspectRule()], FixedClock(T0 + 100))
    state = world([track(t=T0, validity="VALID")], T0)
    broken = dataclasses.replace(state.tracks[0])
    object.__setattr__(broken, "head_z", None)
    findings = engine.evaluate(dataclasses.replace(state, tracks=(broken,)), [], Origin.LIVE).findings
    assert [(f.holding, f.reasons) for f in findings] == [(True, (REASON_INPUT_UNKNOWN, REASON_HEAD_UNAVAILABLE))]


def test_legacy_documents_keep_skeleton_reconstruction():
    # 하위 호환: head_z_validity 가 없는 문서는 지금처럼 읽는다. head_z null + 관측 VALID 귀·눈이면 다시 만들어 확정 근거로 쓴다.
    result, _ = run([(dt, {"head": None, "skeleton": head_skeleton(h, "VALID")}) for dt, h in FALL_PROFILE])
    ((_, tr),) = raised(result)
    assert tr.state.confidence_reasons == ()
    plain, _ = run(frames(FALL_PROFILE))
    assert summary(result) == summary(plain)
