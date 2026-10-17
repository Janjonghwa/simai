"""R6 위험 구역 내 관측 상실 (명세 7.2 R6, 6.4, 15.1 G3 '구역 내 관측 상실 자동 해소 0건')."""

import pytest

from simai_ai.risk import (
    AckActor,
    AlertClear,
    Change,
    Finding,
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    LocalAlert,
    ObservationLossHold,
    Origin,
    RiskEngine,
    RuleContext,
    RuleRegistry,
    RuleSpec,
    Severity,
    Verification,
    WorldState,
    Zone,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.rules.r6_observation_loss import distance_to_polygon, point_in_polygon

T0 = 1760342400100

ZONE = {
    "zone_id": "z-04",
    "zone_version": "zone-1",
    "judge_type": "GROUND_FOOTPRINT",
    "polygon_xy": [[3.0, 4.5], [4.0, 4.5], [4.0, 5.5], [3.0, 5.5]],
    "z_range": None,
    "severity": "CRITICAL",
}
INSIDE = [3.4, 5.0]
FAR = [6.0, 5.0]  # 구역 경계에서 2.0m


class InsideZoneRule:
    """시험용 구역 규칙(R1 흉내): OBSERVED·PREDICTED 사람의 위치가 구역 안이면 구역 등급으로 판정한다."""

    spec = RuleSpec(rule_id="R1", rule_version=1, reads=("tracks.position_xy", "zones"))

    def evaluate(self, ctx: RuleContext):
        for track in ctx.world.tracks:
            if track.observation_state.value == "UNKNOWN":
                continue  # 정확한 위치 사용 중단(6.1)
            for zone in ctx.zones:
                if point_in_polygon(track.position_xy, zone.polygon_xy):
                    yield Finding(
                        "R1", 1, zone.severity, (track.world_id,), zone.zone_id, reasons=("xy_inside_polygon",)
                    )


def track(state="OBSERVED", xy=INSIDE, t_last_valid=T0, world_id="w-0012", sigma=None):
    obj = {
        "world_id": world_id,
        "t_last_valid": t_last_valid,
        "observation_state": state,
        "track_lifecycle": "CONFIRMED" if state == "OBSERVED" else "OCCLUDED",
        "identity_confidence": "HIGH",
        "identity_revision": 0,
        "position_xy": list(xy),
        "position_xy_sigma": sigma or ([0.08, 0.08] if state == "OBSERVED" else [0.6, 0.6]),
        "pelvis_xyz": None,
        "ground_anchor_xyz": None,
        "support_surface_id": None,
        "support_surface_validity": "UNKNOWN",
        "contact_validity": "UNAVAILABLE",
        "z_validity": "UNAVAILABLE",
        "head_z": None,
        "cameras": ["cam1", "cam3"] if state == "OBSERVED" else [],
    }
    return obj


def world(t, tracks):
    return WorldState.from_payload(
        {"t_latest_global": t, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": tracks}
    )


def evaluate(rule, w, zones=(ZONE,)):
    ctx = RuleContext(now_ms=w.t_latest_global + 100, world=w, zones=tuple(Zone.from_json(z) for z in zones))
    return list(rule.evaluate(ctx))


def test_unknown_person_last_seen_inside_gets_holding_with_original_rule_id():
    rule = ObservationLossHold(InsideZoneRule())
    [f] = evaluate(rule, world(T0 + 600, [track("UNKNOWN", t_last_valid=T0)]))
    assert (f.rule_id, f.rule_version, f.world_ids, f.zone_id) == ("R1", 1, ("w-0012",), "z-04")
    assert f.holding and f.needs_check
    assert f.severity is Severity.CRITICAL  # 구역 등급(원래 규칙 유지)
    assert f.reasons == ("observation_lost", "last_seen_in_zone")
    assert dict(f.metrics) == {"zone_distance_m": 0.0}


def test_observed_and_predicted_are_left_to_the_inner_rule():
    rule = ObservationLossHold(InsideZoneRule())
    for state in ("OBSERVED", "PREDICTED"):
        [f] = evaluate(rule, world(T0 + 300, [track(state, t_last_valid=T0)]))
        assert not f.holding and f.reasons == ("xy_inside_polygon",)
    assert evaluate(rule, world(T0, [track("OBSERVED", xy=FAR)])) == []


def test_inner_finding_for_same_person_and_zone_is_not_duplicated():
    class AlwaysRule(InsideZoneRule):
        def evaluate(self, ctx):
            for t in ctx.world.tracks:
                yield Finding("R1", 1, Severity.HIGH, (t.world_id,), "z-04", needs_check=True, reasons=("z_unknown",))

    findings = evaluate(ObservationLossHold(AlwaysRule()), world(T0 + 600, [track("UNKNOWN", t_last_valid=T0)]))
    assert [f.reasons for f in findings] == [("z_unknown",)]


@pytest.mark.parametrize(
    ("xy", "sigma", "expected"),
    [
        ([4.5, 5.0], [0.3, 0.3], 0.5),  # 경계에서 0.5m, 여유 2 × 0.3 = 0.6m
        ([4.5, 5.0], [0.1, 0.3], 0.5),  # 큰 쪽 sigma 를 쓴다
        ([4.7, 5.0], [0.3, 0.3], None),  # 0.7m > 0.6m
        (FAR, [0.6, 0.6], None),
    ],
)
def test_last_position_uncertainty_margin(xy, sigma, expected):
    findings = evaluate(ObservationLossHold(InsideZoneRule()), world(T0 + 600, [track("UNKNOWN", xy, T0, sigma=sigma)]))
    if expected is None:
        assert findings == []
    else:
        [f] = findings
        assert f.metrics["zone_distance_m"] == pytest.approx(expected)


def test_volume_zone_is_judged_on_xy_when_height_is_unknown():
    zone = ZONE | {"zone_id": "z-09", "judge_type": "VOLUME_3D", "z_range": [1.5, 3.0]}
    [f] = evaluate(ObservationLossHold(InsideZoneRule()), world(T0 + 600, [track("UNKNOWN", t_last_valid=T0)]), (zone,))
    assert f.zone_id == "z-09" and f.holding


def test_spec_keeps_inner_key_and_adds_reads():
    inner = InsideZoneRule()
    rule = ObservationLossHold(inner, severity=Severity.HIGH)
    assert rule.spec.key == inner.spec.key and rule.inner is inner
    assert rule.spec.reads[: len(inner.spec.reads)] == inner.spec.reads
    assert "tracks.observation_state" in rule.spec.reads
    registry = RuleRegistry()
    registry.register("R1", 1, lambda: ObservationLossHold(InsideZoneRule()))
    assert registry.create("R1", 1).spec.key == ("R1", 1)
    [f] = evaluate(rule, world(T0 + 600, [track("UNKNOWN", t_last_valid=T0)]))
    assert f.severity is Severity.HIGH


@pytest.mark.parametrize("sigma_k", [-1.0, float("nan"), float("inf"), True, "2"])
def test_sigma_k_is_validated(sigma_k):
    with pytest.raises(RiskInputError):
        ObservationLossHold(InsideZoneRule(), sigma_k=sigma_k)


def test_polygon_helpers():
    square = ((3.0, 4.5), (4.0, 4.5), (4.0, 5.5), (3.0, 5.5))
    assert point_in_polygon((3.5, 5.0), square)
    assert point_in_polygon((3.0, 5.0), square)  # 경계 포함
    assert point_in_polygon((4.0, 5.5), square)  # 꼭짓점
    assert not point_in_polygon((4.01, 5.0), square)
    assert distance_to_polygon((3.5, 5.0), square) == pytest.approx(0.5)
    assert distance_to_polygon((5.0, 6.5), square) == pytest.approx(2**0.5)
    concave = ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (2.0, 1.0), (0.0, 4.0))  # 위가 파인 다각형
    assert point_in_polygon((1.0, 0.5), concave)
    assert not point_in_polygon((2.0, 3.0), concave)


# --- 생명주기와 함께: 구역 안 관측 상실 자동 해소 0건 -------------------------------------


class Run:
    def __init__(self, origin=Origin.LIVE):
        self.clock = FixedClock(T0)
        self.engine = RiskEngine([ObservationLossHold(InsideZoneRule())], self.clock)
        self.tracker = LifecycleTracker(origin=origin, gateway_id="tier0-01", clock=self.clock)
        self.origin = origin

    def step(self, t, tracks):
        self.clock.set(t + 100)
        w = world(t, tracks)
        evaluation = self.engine.evaluate(w, [Zone.from_json(ZONE)], self.origin)
        return self.tracker.apply(evaluation, w, config_version="cfg-12")


def test_lost_inside_zone_never_auto_resolves_and_restarts_on_reobservation():
    run = Run()
    first = run.step(T0, [track("OBSERVED", t_last_valid=T0)])
    assert [t.transition for t in first.transitions] == [Lifecycle.RAISED]
    assert [type(s) for s in first.local_signals] == [LocalAlert]

    lost = run.step(T0 + 600, [track("UNKNOWN", t_last_valid=T0), track("UNKNOWN", [3.6, 4.8], T0, "w-0031")])
    [tr] = lost.transitions  # 이벤트 없던 w-0031 의 holding 은 새 이벤트를 만들지 않는다
    assert set(tr.changes) == {Change.VERIFICATION, Change.CONFIDENCE}
    assert tr.state.verification is Verification.UNVERIFIABLE and tr.state.needs_check
    assert tr.state.severity is Severity.CRITICAL and tr.state.reasons == ("observation_lost", "last_seen_in_zone")
    assert lost.local_signals == ()

    # 10Hz 로 2분 동안 관측 상실(트랙이 사라진 구간 포함) → 해소 0건, 새 경보 0건
    for k in range(1, 1201):
        t = T0 + 600 + k * 100
        tracks = [] if k > 600 else [track("UNKNOWN", t_last_valid=T0)]
        step = run.step(t, tracks)
        assert all(x.transition is not Lifecycle.RESOLVED for x in step.transitions)
        assert step.local_signals == ()
    [state] = run.tracker.active()
    assert state.verification is Verification.UNVERIFIABLE

    # 구역 밖에서 재관측 → 그때부터 3초 뒤 해소
    t_back = T0 + 200_000
    back = run.step(t_back, [track("OBSERVED", FAR, t_back)])
    assert [x.state.verification for x in back.transitions] == [Verification.VERIFIED]
    assert run.step(t_back + 2900, [track("OBSERVED", FAR, t_back + 2900)]).transitions == ()
    done = run.step(t_back + 3000, [track("OBSERVED", FAR, t_back + 3000)])
    assert [x.transition for x in done.transitions] == [Lifecycle.RESOLVED]
    assert [type(s) for s in done.local_signals] == [AlertClear]


@pytest.mark.parametrize("origin", [Origin.PLANNED, Origin.WHAT_IF, Origin.REPLAY])
def test_non_live_observation_loss_has_no_alerts(origin):
    run = Run(origin)
    signals = []
    signals += run.step(T0, [track("OBSERVED", t_last_valid=T0)]).local_signals
    for k in range(1, 100):
        signals += run.step(T0 + 600 + k * 100, [track("UNKNOWN", t_last_valid=T0)]).local_signals
    assert len(run.tracker.active()) == 1
    assert signals == []


# --- 인접 구역: holding 은 원래 이벤트의 구역을 바꾸지 않는다 ---------------------------------

ZONE_A = ZONE | {"zone_id": "z-07", "severity": "HIGH"}  # x 3.0~4.0
ZONE_B = ZONE | {"zone_id": "z-04", "polygon_xy": [[4.2, 4.5], [5.0, 4.5], [5.0, 5.5], [4.2, 5.5]], "severity": "HIGH"}


def _run_adjacent(lost_xy, lost_sigma, zone_b=ZONE_B):
    run = Run()
    zones = [Zone.from_json(ZONE_A), Zone.from_json(zone_b)]

    def step(t, tracks):
        run.clock.set(t + 100)
        w = world(t, tracks)
        return run.tracker.apply(run.engine.evaluate(w, zones, Origin.LIVE), w, config_version="cfg-12")

    [raised] = step(T0, [track("OBSERVED", [3.9, 5.0], T0)]).transitions
    assert raised.state.zone_id == "z-07"
    lost = step(T0 + 600, [track("UNKNOWN", lost_xy, T0, sigma=lost_sigma)])
    return step, run, raised, lost


@pytest.mark.parametrize(
    ("lost_xy", "lost_sigma"),
    [
        ([3.9, 5.0], [0.6, 0.6]),  # 원래 구역 안, 2σ 원이 이웃 구역(0.3m)까지 걸침
        ([4.1, 5.0], [0.3, 0.3]),  # 두 구역 사이(각 0.1m), 두 구역 모두 여유 안
        ([4.6, 5.0], [0.1, 0.1]),  # 마지막 추정이 이웃 구역 안, 원래 구역은 여유 밖
    ],
)
def test_holding_never_moves_lost_person_to_neighbor_zone(lost_xy, lost_sigma):
    step, run, raised, lost = _run_adjacent(lost_xy, lost_sigma)
    [tr] = lost.transitions
    assert tr.event_id == raised.event_id
    assert set(tr.changes) == {Change.VERIFICATION, Change.CONFIDENCE}
    assert tr.state.zone_id == "z-07" and tr.state.verification is Verification.UNVERIFIABLE and tr.state.needs_check
    for k in range(1, 50):  # 관측 상실이 이어져도 구역이 바뀌는 전이가 없다
        later = step(T0 + 600 + k * 100, [track("UNKNOWN", lost_xy, T0, sigma=lost_sigma)])
        assert all(Change.ZONE not in x.changes for x in later.transitions)
    [state] = run.tracker.active()
    assert state.event_id == raised.event_id and state.zone_id == "z-07"


def test_neighbor_holding_does_not_change_severity_or_metrics():
    zone_b = ZONE_B | {"severity": "CAUTION"}
    _, _, raised, lost = _run_adjacent([4.6, 5.0], [0.1, 0.1], zone_b)
    [tr] = lost.transitions
    assert tr.state.zone_id == "z-07" and tr.state.severity is Severity.HIGH
    assert dict(tr.state.metrics) == dict(raised.state.metrics)
    assert tr.state.reasons == ("observation_lost", "last_seen_in_zone")


# --- holding 은 같은 구역에서도 원래 이벤트 등급을 바꾸지 않는다 -------------------------------


class CriticalInZoneRule(InsideZoneRule):
    """판정 등급이 구역 등급과 다른 규칙(R2 흉내: 구역 등급과 관계없이 CRITICAL)."""

    spec = RuleSpec(rule_id="R2", rule_version=1, reads=("tracks.position_xy", "zones"))

    def evaluate(self, ctx: RuleContext):
        for f in super().evaluate(ctx):
            yield Finding("R2", 1, Severity.CRITICAL, f.world_ids, f.zone_id, reasons=("edge",))


def test_holding_keeps_original_severity_when_rule_severity_differs_from_zone():
    """구역 HIGH, 규칙 CRITICAL: 관측 상실 중 CRITICAL 유지, 확인(ACK)한 이벤트는 재관측 때 다시 울리지 않는다."""
    clock = FixedClock(T0)
    engine = RiskEngine([ObservationLossHold(CriticalInZoneRule())], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    zones = [Zone.from_json(ZONE_A)]  # 구역 등급 HIGH

    def step(t, tracks):
        clock.set(t + 100)
        w = world(t, tracks)
        return tracker.apply(engine.evaluate(w, zones, Origin.LIVE), w, config_version="cfg-12")

    raised = step(T0, [track("OBSERVED", INSIDE, T0)])
    [tr] = raised.transitions
    assert tr.state.severity is Severity.CRITICAL and [type(s) for s in raised.local_signals] == [LocalAlert]
    assert tracker.acknowledge(tr.event_id, AckActor.FOREMAN, "foreman-1", T0 + 200) is not None

    lost = step(T0 + 600, [track("UNKNOWN", INSIDE, T0)])
    [tr] = lost.transitions
    assert Change.SEVERITY not in tr.changes and tr.state.severity is Severity.CRITICAL
    for k in range(1, 50):
        later = step(T0 + 600 + k * 100, [track("UNKNOWN", INSIDE, T0)])
        assert all(Change.SEVERITY not in x.changes for x in later.transitions) and later.local_signals == ()

    t_back = T0 + 6000
    back = step(t_back, [track("OBSERVED", INSIDE, t_back)])
    assert all(Change.SEVERITY not in x.changes for x in back.transitions)
    assert back.local_signals == ()  # 등급이 그대로라 확인한 경보를 다시 울리지 않는다
    [state] = tracker.active()
    assert state.severity is Severity.CRITICAL and state.acknowledged
