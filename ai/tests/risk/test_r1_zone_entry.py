"""R1 위험 구역 진입 (명세 7.2 R1, 8.2, 7.1, 6.4, 부록 D-G6-04·05)."""

import pytest

from simai_ai.risk import (
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    LocalAlert,
    ObservationLossHold,
    Origin,
    RiskEngine,
    RuleContext,
    Severity,
    WorldState,
    Zone,
    ZoneEntryRule,
    default_registry,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import ConfidenceReason
from simai_ai.risk.rules.r1_zone_entry import (
    REASON_XY_CLEAR_MARGIN,
    REASON_XY_INSIDE,
    REASON_XY_UNCERTAINTY_OVERLAP,
    REASON_Z_CLEAR_MARGIN,
    REASON_Z_OVERLAP,
    occupancy_height,
)
from simai_ai.risk.world import WorldTrack

T0 = 1760342400100
Z_UNKNOWN = ConfidenceReason.Z_UNKNOWN
PREDICTED = ConfidenceReason.PREDICTED
SINGLE_VIEW = ConfidenceReason.SINGLE_VIEW
JOINT_UNCERTAIN = ConfidenceReason.JOINT_UNCERTAIN
IDENTITY_UNCERTAIN = ConfidenceReason.IDENTITY_UNCERTAIN
SQUARE = [[3.0, 4.5], [4.0, 4.5], [4.0, 5.5], [3.0, 5.5]]


def zone(judge="GROUND_FOOTPRINT", z_range=None, severity="HIGH", zone_id="z-04", polygon=SQUARE):
    return Zone.from_json(
        {
            "zone_id": zone_id,
            "zone_version": "zone-1",
            "judge_type": judge,
            "polygon_xy": polygon,
            "z_range": z_range,
            "severity": severity,
        }
    )


ABOVE = zone("VOLUME_3D", [2.0, 4.0], "HIGH", zone_id="z-above")
ABOVE_LOW_FLOOR = zone("VOLUME_3D", [0.0, 2.5], "HIGH", zone_id="z-floor")


def skeleton(base_z, top, *, validity="VALID", inferred_top=None):
    joints = [None] * 17
    valid = ["UNAVAILABLE"] * 17
    source = ["observed"] * 17
    for i, z in {15: base_z + 0.08, 16: base_z + 0.08, 11: (base_z + top) / 2, 12: (base_z + top) / 2, 1: top}.items():
        joints[i] = [3.5, 5.0, z]
        valid[i] = validity
    if inferred_top is not None:
        joints[10], valid[10], source[10] = [3.5, 5.0, inferred_top], "VALID", "inferred"
    return {
        "joint_layout": "COCO17",
        "joints": joints,
        "visible": [j is not None for j in joints],
        "validity": valid,
        "joint_source": source,
    }


def track(
    xy=(3.5, 5.0),
    *,
    world_id="w-0012",
    state="OBSERVED",
    sigma=(0.05, 0.05),
    anchor=(None, None, 0.0),
    anchor_sigma=None,
    head=1.7,
    head_sigma=None,
    cameras=("cam1", "cam3"),
    identity="HIGH",
    skel=None,
    t=T0,
):
    """anchor=None 이면 접지점을 모른다. anchor 의 XY 가 None 이면 position_xy 와 같은 XY."""
    if anchor is not None:
        anchor = [xy[0] if anchor[0] is None else anchor[0], xy[1] if anchor[1] is None else anchor[1], anchor[2]]
    obj = {
        "world_id": world_id,
        "t_last_valid": t,
        "observation_state": state,
        "track_lifecycle": "CONFIRMED",
        "identity_confidence": identity,
        "identity_revision": 0,
        "position_xy": list(xy),
        "position_xy_sigma": list(sigma),
        "pelvis_xyz": None,
        "ground_anchor_xyz": anchor,
        "support_surface_id": None if anchor is None else "floor",
        "support_surface_validity": "UNKNOWN" if anchor is None else "VALID",
        "contact_validity": "UNAVAILABLE" if anchor is None else "VALID",
        "z_validity": "UNAVAILABLE" if anchor is None else "VALID",
        "head_z": head,
        "cameras": list(cameras),
    }
    if anchor_sigma is not None:
        obj["ground_anchor_sigma"] = list(anchor_sigma)
    if head_sigma is not None:
        obj["head_z_sigma"] = head_sigma
    if skel is not None:
        obj["skeleton"] = skel
    return obj


def world(*tracks, t=T0):
    return WorldState.from_payload(
        {"t_latest_global": t, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": list(tracks)}
    )


def judge(*tracks, zones=None, rule=None):
    rule = ZoneEntryRule() if rule is None else rule
    zones = (zone(),) if zones is None else tuple(zones)
    return list(rule.evaluate(RuleContext(now_ms=T0 + 100, world=world(*tracks), zones=zones)))


def only(findings):
    assert len(findings) == 1, findings
    return findings[0]


# --- GROUND_FOOTPRINT ---------------------------------------------------------------------


def test_inside_raises_with_zone_severity():
    f = only(judge(track(), zones=[zone(severity="CRITICAL")]))
    assert (f.rule_id, f.rule_version, f.world_ids, f.zone_id) == ("R1", 1, ("w-0012",), "z-04")
    assert f.severity is Severity.CRITICAL
    assert not f.holding and not f.needs_check and f.confidence_reasons == ()
    assert f.reasons == (REASON_XY_INSIDE,)
    assert dict(f.metrics) == {"zone_distance_m": 0.0}


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        # σ 0.05 → 반축 0.1, + 점유 0.3 → 범위 반경 0.4. 해소 간격 0.3 → 유지 띠는 경계 0.4~0.7 m
        (4.39, "overlap"),
        (4.399, "overlap"),
        (4.40, "overlap"),  # 정확히 닿음(부동소수점 간격 ~4e-16 도 허용오차로 걸림)
        (4.401, "hold"),
        (4.69, "hold"),
        (4.71, None),
        (5.50, None),
    ],
)
def test_boundary_hysteresis(x, expected):
    findings = judge(track((x, 5.0)))
    if expected is None:
        assert findings == []
        return
    f = only(findings)
    assert f.holding
    assert f.reasons == ((REASON_XY_UNCERTAINTY_OVERLAP,) if expected == "overlap" else (REASON_XY_CLEAR_MARGIN,))
    assert f.metrics["zone_distance_m"] == pytest.approx(x - 4.0)


def test_zero_clear_margin_still_raises():
    rule = ZoneEntryRule(clear_margin_m=0.0)  # 히스테리시스 띠 없음: 범위가 걸리면 유지, 아니면 판정 없음
    assert only(judge(track((4.39, 5.0)), rule=rule)).holding
    assert judge(track((4.41, 5.0)), rule=rule) == []
    assert not only(judge(track(skel=skeleton(0.0, 2.0)), zones=[ABOVE], rule=rule)).holding


@pytest.mark.parametrize(
    ("xy", "sigma"),
    [
        ((4.4, 5.0), (0.05, 0.05)),  # 오른쪽 변에 수직으로 닿음(반축 0.1 + 0.3)
        ((3.5, 5.9), (0.05, 0.05)),  # 위 변에 수직으로 닿음
        ((4.0 + 0.4 / 2**0.5, 5.5 + 0.4 / 2**0.5), (0.05, 0.05)),  # 꼭짓점(4.0, 5.5)에 닿음
        ((4.0 + 0.7 / 2**0.5, 4.5 - 0.7 / 2**0.5), (0.2, 0.2)),  # 꼭짓점(4.0, 4.5)에 닿음, 반축 0.4
        ((4.43, 5.0), (0.065, 0.065)),  # 0.13 + 0.3 = 0.43
    ],
)
def test_uncertainty_contact_holds_and_neighbours(xy, sigma):
    """불확실성 범위의 경계 접촉과 그 주변은 새 이벤트 없이 유지한다."""
    assert only(judge(track(xy, sigma=sigma))).holding
    cx, cy = 3.5, 5.0  # 구역 중심 쪽으로 / 반대쪽으로 1 µm
    dx, dy = xy[0] - cx, xy[1] - cy
    norm = (dx * dx + dy * dy) ** 0.5
    inward = (xy[0] - 1e-6 * dx / norm, xy[1] - 1e-6 * dy / norm)
    outward = (xy[0] + 1e-6 * dx / norm, xy[1] + 1e-6 * dy / norm)
    assert only(judge(track(inward, sigma=sigma))).holding
    assert only(judge(track(outward, sigma=sigma))).holding


def test_exact_height_contact_raises():
    # 위 끝 1.7 + 2·0.15 = 2.0(부동소수점 1.9999999999999998) → 구역 바닥 2.0 에 닿음 → 발생
    f = only(judge(track(head=1.7, head_sigma=0.15), zones=[ABOVE]))
    assert not f.holding and f.reasons == (REASON_XY_INSIDE, REASON_Z_OVERLAP)
    assert only(judge(track(head=1.7 - 1e-6, head_sigma=0.15), zones=[ABOVE])).holding


def test_uncertainty_contact_never_raises():
    transitions, _ = _run([4.4] * 10)
    assert not [t for t in transitions if t.transition is Lifecycle.RAISED]


def test_large_uncertainty_only_holds():
    # 경계 1.0 m 밖: σ 0.05 는 판정 없음, σ 0.4(반축 0.8 + 0.3 = 1.1) 는 범위가 걸려 유지
    assert judge(track((5.0, 5.0))) == []
    f = only(judge(track((5.0, 5.0), sigma=(0.4, 0.4))))
    assert f.holding and f.reasons == (REASON_XY_UNCERTAINTY_OVERLAP,)


def test_anisotropic_uncertainty():
    # x 방향만 불확실: 오른쪽 경계(x)로는 걸리고, 위쪽 경계(y)로는 걸리지 않는다
    assert only(judge(track((4.8, 5.0), sigma=(0.3, 0.02)))).holding
    assert judge(track((3.5, 6.3), sigma=(0.3, 0.02))) == []


def test_foot_position_preferred_over_pelvis_xy():
    # 몸(position_xy)은 구역 밖, 발(접지점)은 구역 안 → 발 기준으로 발생
    f = only(judge(track((5.5, 5.0), anchor=(3.9, 5.0, 0.0))))
    assert f.reasons == (REASON_XY_INSIDE,)
    # 접지점 수평 표준편차가 있으면 그것을 쓴다
    assert judge(track((5.5, 5.0), anchor=(4.8, 5.0, 0.0), anchor_sigma=(0.02, 0.02, 0.02))) == []
    assert only(judge(track((5.5, 5.0), anchor=(4.8, 5.0, 0.0), anchor_sigma=(0.3, 0.3, 0.02)))).holding


def test_ground_anchor_takes_precedence_for_entry():
    """접지점이 밖이면 position_xy 가 안이어도 기존 이벤트만 유지한다."""
    f = only(judge(track((3.9, 5.0), anchor=(4.5, 5.0, 0.0))))
    assert f.holding and f.reasons == (REASON_XY_INSIDE,)
    f = only(judge(track((3.9, 5.0), anchor=(4.5, 5.0, 0.0)), zones=[ABOVE_LOW_FLOOR]))
    assert f.holding and f.reasons == (REASON_XY_INSIDE, REASON_Z_OVERLAP)
    # 둘 다 밖이면 작은 간격(position_xy 경계 0.5 m → 간격 0.1)으로 유지 띠
    f = only(judge(track((4.5, 5.0), anchor=(5.5, 5.0, 0.0))))
    assert f.holding and f.metrics["zone_distance_m"] == pytest.approx(0.5)


def test_ground_footprint_without_anchor_uses_position_and_ignores_height():
    # GROUND_FOOTPRINT 는 높이를 보지 않는다(8.2 표). 접지점이 없으면 position_xy 로 판정한다.
    f = only(judge(track(anchor=None, head=None)))
    assert f.reasons == (REASON_XY_INSIDE,)
    assert not f.needs_check


def test_unknown_track_is_not_judged_by_r1_but_held_by_r6():
    lost = track(state="UNKNOWN", anchor=None, head=None, cameras=())
    assert judge(lost) == []
    f = only(judge(lost, rule=ObservationLossHold(ZoneEntryRule())))
    assert f.holding and f.needs_check and f.reasons == ("observation_lost", "last_seen_in_zone")
    assert f.confidence_reasons == (PREDICTED,)  # 마지막 추정 위치(S15P31A101-156)


def test_each_zone_judged_with_its_severity():
    other = zone(zone_id="z-05", severity="CAUTION", polygon=[[4.0, 4.5], [5.0, 4.5], [5.0, 5.5], [4.0, 5.5]])
    findings = judge(track((4.0, 5.0)), zones=[zone(severity="HIGH"), other])
    assert {(f.zone_id, f.severity) for f in findings} == {("z-04", Severity.HIGH), ("z-05", Severity.CAUTION)}


# --- 확인 필요 근거 ------------------------------------------------------------------------


def test_predicted_single_view_identity_uncertain():
    f = only(judge(track(state="PREDICTED", cameras=(), identity="IDENTITY_UNCERTAIN", anchor=None)))
    assert f.holding
    assert f.needs_check
    assert f.reasons == (REASON_XY_INSIDE,)  # 판정 근거에는 이유를 겹쳐 적지 않는다
    assert f.confidence_reasons == (PREDICTED, SINGLE_VIEW, IDENTITY_UNCERTAIN)  # 명세 7.1 선언 순서
    # 같은 카메라가 두 번 적혀도 1대다
    assert only(judge(track(cameras=("cam1", "cam1")))).confidence_reasons == (SINGLE_VIEW,)


def test_confidence_reasons_also_on_holding():
    f = only(judge(track((4.5, 5.0), cameras=("cam2",))))
    assert f.holding and f.needs_check and f.reasons == (REASON_XY_CLEAR_MARGIN,)
    assert f.confidence_reasons == (SINGLE_VIEW,)


def test_floor_and_podium_same_xy_are_distinguished():
    """D-G6-04: 같은 XY 의 바닥 위 사람과 단상 위 사람을 구분한다."""
    floor = track(anchor=(None, None, 0.0), skel=skeleton(0.0, 1.65))
    podium = track(anchor=(None, None, 0.4), skel=skeleton(0.4, 2.05))
    assert judge(floor, zones=[ABOVE]) == []  # 0~1.65 m, 간격 0.35 ≥ 0.3
    f = only(judge(podium, zones=[ABOVE]))
    assert not f.holding and not f.needs_check
    assert f.reasons == (REASON_XY_INSIDE, REASON_Z_OVERLAP)
    assert f.metrics["occupancy_z_min_m"] == 0.4 and f.metrics["occupancy_z_max_m"] == 2.05


def test_height_hysteresis_band():
    f = only(judge(track(skel=skeleton(0.0, 1.8)), zones=[ABOVE]))  # 간격 0.2 → 유지
    assert f.holding and f.reasons == (REASON_XY_INSIDE, REASON_Z_CLEAR_MARGIN)
    # 수평이 유지 띠면 높이가 겹쳐도 유지(발생 아님)
    f = only(judge(track((4.5, 5.0), anchor=(None, None, 0.4), skel=skeleton(0.4, 2.1)), zones=[ABOVE]))
    assert f.holding and f.reasons == (REASON_XY_CLEAR_MARGIN, REASON_Z_OVERLAP)


def test_raised_arm_counts_only_valid_observed_joints():
    """D-G6-05: 팔을 든 자세. VALID 관측 관절만 위 끝에 쓰고 추정(inferred) 관절은 쓰지 않는다."""
    assert judge(track(skel=skeleton(0.0, 1.6, inferred_top=2.6)), zones=[ABOVE]) == []
    assert not only(judge(track(skel=skeleton(0.0, 2.1)), zones=[ABOVE])).holding  # 든 손목이 VALID 면 겹침


def test_head_z_used_only_without_valid_joints():
    # VALID 관절이 있으면 head_z 가 높아도 쓰지 않는다
    assert judge(track(head=2.5, skel=skeleton(0.0, 1.6)), zones=[ABOVE]) == []
    # skeleton 이 없으면 head_z
    f = only(judge(track(head=2.1), zones=[ABOVE]))
    assert f.reasons == (REASON_XY_INSIDE, REASON_Z_OVERLAP) and not f.needs_check
    # skeleton 에 VALID 관절이 없으면 head_z 는 UNCERTAIN 관절에서 왔다 → JOINT_UNCERTAIN
    f = only(judge(track(head=2.1, skel=skeleton(0.0, 2.0, validity="UNCERTAIN")), zones=[ABOVE]))
    assert f.needs_check and f.reasons == (REASON_XY_INSIDE, REASON_Z_OVERLAP)
    assert f.confidence_reasons == (JOINT_UNCERTAIN,)


def test_height_uncertainty_widens_range():
    # 위 끝 1.6 + 2·0.04 = 1.68 → 간격 0.32 → 판정 없음 / 2·0.1 → 1.8 → 간격 0.2 → 유지
    assert judge(track(head=1.6), zones=[ABOVE]) == []
    assert judge(track(head=1.6, head_sigma=0.04), zones=[ABOVE]) == []
    assert only(judge(track(head=1.6, head_sigma=0.1), zones=[ABOVE])).holding
    # 아래 끝도 넓힌다: 구역 [0.0, 0.5] 위 단상 0.75 m 에 선 사람, σz 0.1 → 0.55 → 간격 0.05 → 유지
    low = zone("VOLUME_3D", [0.0, 0.5], zone_id="z-low")
    assert judge(track(anchor=(None, None, 0.85), head=2.5), zones=[low]) == []
    assert only(judge(track(anchor=(None, None, 0.75), anchor_sigma=(0.02, 0.02, 0.1), head=2.5), zones=[low])).holding


@pytest.mark.parametrize(
    ("anchor_z", "head"),
    [
        (None, None),  # 둘 다 모름
        (None, 2.5),  # 서 있는 면 모름, 위 끝이 구역 높이 안
        (None, 4.5),  # 서 있는 면 모름, 위 끝이 구역보다 높음(발은 구역 높이에 있을 수 있다)
        (2.5, None),  # 위 끝 모름, 서 있는 면이 구역 높이 안
        (0.0, None),  # 위 끝 모름, 서 있는 면이 구역 아래(머리는 구역 높이에 닿을 수 있다)
    ],
)
def test_partial_height_unknown_still_raises_with_z_unknown(anchor_z, head):
    """높이 양 끝 중 하나라도 모르면 XY 가 안일 때 발생하고 Z_UNKNOWN(확인 필요)을 붙인다. 높이 겹침 근거는 붙이지 않는다."""
    anchor = None if anchor_z is None else (None, None, anchor_z)
    f = only(judge(track(anchor=anchor, head=head), zones=[ABOVE]))
    assert not f.holding
    assert f.needs_check and f.reasons == (REASON_XY_INSIDE,) and f.confidence_reasons == (Z_UNKNOWN,)
    # XY 가 유지 띠면 유지 + Z_UNKNOWN(새 이벤트는 만들지 않는다)
    f = only(judge(track((4.5, 5.0), anchor=anchor, head=head), zones=[ABOVE]))
    assert f.holding and f.needs_check and f.reasons == (REASON_XY_CLEAR_MARGIN,)
    assert f.confidence_reasons == (Z_UNKNOWN,)


@pytest.mark.parametrize(
    ("anchor_z", "head"),
    [
        (None, 1.6),  # 위 끝이 구역 최저 높이(2.0)보다 0.4 m 낮음: 아는 끝만으로 겹칠 수 없다
        (4.5, None),  # 서 있는 면이 구역 최고 높이(4.0)보다 0.5 m 높음
    ],
)
def test_partial_height_unknown_not_judged_when_known_end_excludes_overlap(anchor_z, head):
    anchor = None if anchor_z is None else (None, None, anchor_z)
    assert judge(track(anchor=anchor, head=head), zones=[ABOVE]) == []


def test_partial_height_unknown_within_clear_margin_only_holds():
    # 위 끝 1.8 → 구역 최저 2.0 과 간격 0.2(해소 간격 0.3 안): 유지만 한다
    f = only(judge(track(anchor=None, head=1.8), zones=[ABOVE]))
    assert f.holding and f.reasons == (REASON_XY_INSIDE, REASON_Z_CLEAR_MARGIN)
    assert f.confidence_reasons == (Z_UNKNOWN,)


def test_z_unknown_outside_xy_is_not_judged():
    assert judge(track((6.0, 5.0), anchor=None, head=None), zones=[ABOVE]) == []


def test_occupancy_height_never_fills_zero():
    h = occupancy_height(WorldTrack.from_json(track(anchor=None, head=None)))
    assert (h.bottom, h.top) == (None, None)


# --- 생명주기와 함께 ------------------------------------------------------------------------


def _run(xs, *, origin=Origin.LIVE, dt=100):
    """10 Hz 로 x 위치를 따라 걷는 사람을 엔진·생명주기에 넣는다. (전이, 신호) 목록."""
    clock = FixedClock(T0)
    engine = RiskEngine([default_registry().create("R1", 1)], clock)
    tracker = LifecycleTracker(origin=origin, gateway_id="tier0-01", clock=clock)
    transitions, signals = [], []
    for i, x in enumerate(xs):
        t = T0 + i * dt
        clock.set(t + 50)
        w = world(track((x, 5.0), t=t), t=t)
        step = tracker.apply(engine.evaluate(w, [zone()], origin), w, config_version="cfg-1")
        transitions += step.transitions
        signals += step.local_signals
    return transitions, signals


def test_height_unknown_inside_zone_opens_event_with_needs_check():
    """회귀(11-18 시연): 접지점·머리 높이가 없는 프레임만 이어져도 구역 안이면 이벤트가 열리고 확인 필요가 붙는다."""
    clock = FixedClock(T0)
    engine = RiskEngine([default_registry().create("R1", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    transitions = []
    for i in range(5):
        t = T0 + i * 100
        clock.set(t + 50)
        w = world(track(anchor=None, head=None, t=t), t=t)
        transitions += tracker.apply(engine.evaluate(w, [ABOVE], Origin.LIVE), w, config_version="cfg-1").transitions
    raised = [x for x in transitions if x.state.rule_id == "R1"]
    assert raised and raised[0].state.confidence_reasons == (Z_UNKNOWN,) and raised[0].state.needs_check


def test_flicker_at_boundary_makes_one_event():
    """경계에서 0.3 m 안쪽으로 흔들리는 사람(깜빡임)은 이벤트 하나로 유지되고, 띠 밖 3초 뒤에만 해소된다."""
    wobble = [3.9, 3.9, 4.38, 4.62, 4.41, 4.66, 4.35, 4.55] * 5  # 진입 뒤 경계 밖 유지 띠에서 흔들림
    leave = [5.0] * 31  # 띠 밖 3.0초
    transitions, signals = _run(wobble + leave)
    raised = [t for t in transitions if t.transition is Lifecycle.RAISED]
    resolved = [t for t in transitions if t.transition is Lifecycle.RESOLVED]
    assert len(raised) == 1 and len(resolved) == 1
    assert resolved[0].state.observation_time - (T0 + (len(wobble)) * 100) == 3000
    assert [type(s).__name__ for s in signals] == ["LocalAlert", "AlertClear"]


@pytest.mark.parametrize("origin", [Origin.WHAT_IF, Origin.REPLAY, Origin.PLANNED])
def test_non_live_same_transitions_no_site_alert(origin):
    path = [5.2, 4.6, 3.6, 3.6] + [5.0] * 31
    live, live_signals = _run(path)
    other, other_signals = _run(path, origin=origin)
    assert [(t.transition, t.changes) for t in live] == [(t.transition, t.changes) for t in other]
    assert any(isinstance(s, LocalAlert) for s in live_signals)
    assert other_signals == []


def test_registry_wraps_r1_with_r6():
    rule = default_registry().create("R1", 1)
    assert isinstance(rule, ObservationLossHold)
    assert isinstance(rule.inner, ZoneEntryRule)


@pytest.mark.parametrize("name", ["k", "occupancy_radius_m", "clear_margin_m", "tolerance_m"])
@pytest.mark.parametrize("value", [-0.1, float("nan"), True])
def test_invalid_parameters(name, value):
    with pytest.raises(RiskInputError):
        ZoneEntryRule(**{name: value})


# --- world_state 2.0.0: UNCERTAIN head_z 값이 계약으로 와도 판정은 1.4.0 때와 같다 (S15P31A101-179) ---------------


@pytest.mark.parametrize("head", [1.5, 1.7])
def test_uncertain_head_z_value_from_v2_keeps_z_unknown_like_v14(head):
    """회귀: VOLUME_3D 구역(z 2.0~4.0), skeleton 전부 UNCERTAIN, XY 안. 2.0.0 은 UNCERTAIN head_z 값(1.5·1.7)을 싣지만
    risk.world 가 확정값으로 넘기지 않으므로, 1.4.0(UNCERTAIN + null)·dev 와 같이 위 끝을 모르는 Z_UNKNOWN 발생이다.
    (값을 확정값으로 쓰면 1.7 은 유지만, 1.5 는 판정 없음이 되어 판정이 바뀐다.)"""
    skel = skeleton(0.0, head - 0.12, validity="UNCERTAIN")
    v2 = dict(track(head=head, head_sigma=0.04, skel=skel), head_z_validity="UNCERTAIN")
    v14 = dict(track(head=None, skel=skel), head_z_validity="UNCERTAIN")
    (f,) = judge(v2, zones=[ABOVE])
    assert not f.holding and f.reasons == (REASON_XY_INSIDE,) and f.confidence_reasons == (Z_UNKNOWN,)
    assert judge(v2, zones=[ABOVE]) == judge(v14, zones=[ABOVE])


def test_valid_head_z_still_used_as_confirmed_top():
    # VALID 값은 그대로 확정 위 끝이다(skeleton 이 없을 때): 2.0.0 에서도 바뀌지 않는다
    v2 = dict(track(head=2.1), head_z_validity="VALID")
    f = only(judge(v2, zones=[ABOVE]))
    assert f.reasons == (REASON_XY_INSIDE, REASON_Z_OVERLAP) and not f.needs_check


@pytest.mark.parametrize(("state", "cameras", "anchor", "raises"), [
    ("PREDICTED", ("cam1", "cam2"), (None, None, 0.0), False),
    ("OBSERVED", ("cam1",), None, False),
    ("OBSERVED", ("cam1",), (None, None, 0.0), True),
    ("OBSERVED", ("cam1", "cam2"), None, True),
])
def test_entry_requires_observed_confident_position(state, cameras, anchor, raises):
    f = only(judge(track(state=state, cameras=cameras, anchor=anchor)))
    assert f.holding is (not raises)


def test_open_event_survives_uncertain_frame():
    clock = FixedClock(T0)
    engine = RiskEngine([default_registry().create("R1", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    for i, state in enumerate(("OBSERVED", "OBSERVED", "PREDICTED")):
        t = T0 + i * 100
        clock.set(t)
        w = world(track(state=state, t=t), t=t)
        step = tracker.apply(engine.evaluate(w, [zone()], Origin.LIVE), w, config_version="cfg-1")
        if i == 1:
            assert any(x.transition is Lifecycle.RAISED for x in step.transitions)
        if i == 2:
            assert not any(x.transition is Lifecycle.RESOLVED for x in step.transitions)
            assert len(tracker.active()) == 1


def test_entry_point_boundary_tolerance():
    assert not only(judge(track((4.0, 5.0)))).holding
    boundary = only(judge(track((4.0 + 0.5e-9, 5.0))))
    assert not boundary.holding and boundary.reasons == (REASON_XY_INSIDE,)
    assert only(judge(track((4.0 + 2e-9, 5.0)))).holding
