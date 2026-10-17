"""발생 지속 조건·이벤트 재개·확인 필요 이유 (명세 6.4, 7.1, 7.4, 15.1 G6, 부록 D-G6-07·08, S15P31A101-156)."""

from dataclasses import replace

import pytest

from simai_ai.risk import (
    AckActor,
    AlertClear,
    Change,
    ConfidenceReason,
    Evaluation,
    EventIdFactory,
    Finding,
    FixedClock,
    Lifecycle,
    LifecyclePolicy,
    LifecycleTracker,
    LocalAlert,
    ObservationLossHold,
    Origin,
    Persistence,
    RiskEngine,
    RuleContext,
    RuleSpec,
    Severity,
    Verification,
    WorldState,
    Zone,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import LEGACY_NEEDS_CHECK_REASON, normalize_confidence
from simai_ai.risk.lifecycle import IMMEDIATE, REASON_PENDING_OBSERVATION_LOST
from simai_ai.risk.rules.r6_observation_loss import point_in_polygon

T0 = 1760342400100
GATEWAY = "tier0-01"
NON_LIVE = [Origin.PLANNED, Origin.WHAT_IF, Origin.REPLAY]
R = ConfidenceReason


def finding(rule_id="R1", world_ids=("w-0012",), zone_id="z-04", severity=Severity.HIGH, **kwargs):
    return Finding(
        rule_id=rule_id,
        rule_version=1,
        severity=severity,
        world_ids=world_ids,
        zone_id=zone_id,
        reasons=kwargs.pop("reasons", ("xy_inside_polygon",)),
        **kwargs,
    )


class Harness:
    """관측 시각을 직접 정해 판정을 넣는다. 판정 시각은 관측 시각 + 100ms(lag 100ms)."""

    def __init__(self, track_json, origin=Origin.LIVE, policy=None):
        self.clock = FixedClock(T0)
        self.origin = origin
        self.track_json = track_json
        self.tracker = LifecycleTracker(
            origin=origin,
            gateway_id=GATEWAY,
            clock=self.clock,
            policy=policy,
            id_factory=EventIdFactory(self.clock, randbits=lambda n: 0),
        )

    def step(self, obs, findings=(), state="OBSERVED", tracks=None, lag=100):
        self.clock.set(max(self.clock.now_ms(), obs + lag))
        if tracks is None:
            t_last_valid = obs if state == "OBSERVED" else T0
            tracks = [self.track_json(observation_state=state, t_last_valid=t_last_valid)]
        world = WorldState.from_payload(
            {"t_latest_global": obs, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": tracks}
        )
        evaluation = Evaluation(
            origin=self.origin,
            evaluated_at=self.clock.now_ms(),
            observation_time=obs,
            calibration_version="cal-7",
            scene_version="scene-3",
            rules=(),
            zones=(),
            findings=tuple(sorted(findings, key=lambda f: f.sort_key)),
        )
        return self.tracker.apply(evaluation, world, config_version="cfg-12")


@pytest.fixture
def harness(track_json):
    def make(**kwargs):
        return Harness(track_json, **kwargs)

    return make


def at(k):
    return T0 + k * 100


# --- 확인 필요 이유 (7.1) -------------------------------------------------------------------


def test_normalize_confidence_defines_needs_check_by_reasons():
    assert normalize_confidence(False, ()) == (False, ())
    assert normalize_confidence(False, ("SINGLE_VIEW", R.Z_UNKNOWN, "Z_UNKNOWN")) == (True, (R.Z_UNKNOWN, R.SINGLE_VIEW))
    # 하위 호환: 이유 없는 needs_check=True 는 버리지 않고 원인 미상(UNSPECIFIED)을 붙인다(원인을 지어내지 않음)
    assert LEGACY_NEEDS_CHECK_REASON is R.UNSPECIFIED
    assert normalize_confidence(True, ()) == (True, (R.UNSPECIFIED,))
    assert normalize_confidence(True, (R.JOINT_UNCERTAIN,)) == (True, (R.JOINT_UNCERTAIN,))


def test_finding_confidence_reasons():
    assert finding().confidence_reasons == () and not finding().needs_check
    f = finding(confidence_reasons=(R.IDENTITY_UNCERTAIN, R.Z_UNKNOWN))
    assert f.needs_check and f.confidence_reasons == (R.Z_UNKNOWN, R.IDENTITY_UNCERTAIN)
    assert finding(needs_check=True).confidence_reasons == (R.UNSPECIFIED,)
    with pytest.raises(RiskInputError):
        finding(confidence_reasons=("LOW_SCORE",))


def test_reason_change_is_a_confidence_update_and_payload_carries_reasons(harness):
    h = harness(policy=LifecyclePolicy(persistence=IMMEDIATE))
    [raised] = h.step(at(0), [finding(confidence_reasons=(R.Z_UNKNOWN,))]).transitions
    assert raised.state.needs_check and raised.state.confidence_reasons == (R.Z_UNKNOWN,)
    assert raised.to_payload()["confidence_reasons"] == ["Z_UNKNOWN"] and raised.to_payload()["needs_check"] is True
    [more] = h.step(at(1), [finding(confidence_reasons=(R.Z_UNKNOWN, R.SINGLE_VIEW))]).transitions
    assert more.changes == (Change.CONFIDENCE,) and more.state.confidence_reasons == (R.Z_UNKNOWN, R.SINGLE_VIEW)
    [clear] = h.step(at(2), [finding()]).transitions
    assert clear.changes == (Change.CONFIDENCE,) and not clear.state.needs_check
    assert clear.to_payload()["confidence_reasons"] == [] and clear.state.severity is Severity.HIGH
    assert h.step(at(3), [finding()]).transitions == ()


def test_legacy_needs_check_warns_once_per_source(caplog):
    with caplog.at_level("WARNING", logger="simai_ai.risk.common"):
        for _ in range(3):
            assert normalize_confidence(True, (), "Finding R-WARN") == (True, (R.UNSPECIFIED,))
        normalize_confidence(True, (R.Z_UNKNOWN,), "Finding R-WARN-2")  # 이유가 있으면 경고 없음
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["Finding R-WARN"]


def test_restored_state_without_reasons_gets_unspecified(harness):
    h = harness(policy=LifecyclePolicy(persistence=IMMEDIATE))
    [raised] = h.step(at(0), [finding(needs_check=True)]).transitions
    assert raised.state.confidence_reasons == (R.UNSPECIFIED,)
    old = replace(raised.state, needs_check=True, confidence_reasons=())  # 이유 목록이 없던 체크포인트
    assert old.confidence_reasons == (R.UNSPECIFIED,) and old.to_payload()["confidence_reasons"] == ["UNSPECIFIED"]


# --- 발생 지속 조건 (6.4, D-G6-07) -----------------------------------------------------------


def test_default_policy_is_two_of_three_with_r7_immediate_and_10s_resume():
    policy = LifecyclePolicy()
    assert (policy.persistence.min_frames, policy.persistence.window_frames) == (2, 3)
    assert policy.persistence_for("R1") == Persistence(2, 3) and policy.persistence_for("R7").immediate
    assert policy.resume_window_ms == 10_000


@pytest.mark.parametrize(("min_frames", "window_frames"), [(0, 3), (3, 2), (2, 0), (True, 3)])
def test_persistence_is_validated(min_frames, window_frames):
    with pytest.raises(RiskInputError):
        Persistence(min_frames, window_frames)


def test_policy_rejects_bad_persistence_values():
    with pytest.raises(RiskInputError):
        LifecyclePolicy(resume_window_ms=-1)
    with pytest.raises(RiskInputError):
        LifecyclePolicy(persistence_by_rule={"R1": (2, 3)})


def test_single_frame_spike_makes_no_event(harness):
    h = harness()
    assert h.step(at(0), [finding()]).transitions == ()
    assert list(h.tracker.pending().values()) == [1]
    for k in (1, 2):
        assert h.step(at(k)).transitions == ()
    assert h.step(at(3)).transitions == ()  # 창(3프레임)을 벗어남
    assert h.tracker.pending() == {} and h.tracker.active() == ()


def test_two_consecutive_frames_raise_on_the_second(harness):
    h = harness()
    assert h.step(at(0), [finding()]).transitions == ()
    step = h.step(at(1), [finding()])
    [t] = step.transitions
    assert t.transition is Lifecycle.RAISED and t.state.observation_time == at(1)
    assert [type(s) for s in step.local_signals] == [LocalAlert]
    assert h.tracker.pending() == {}


def test_two_of_three_with_a_gap_raises_but_two_frames_apart_do_not(harness):
    h = harness()
    h.step(at(0), [finding()])
    h.step(at(1))
    [t] = h.step(at(2), [finding()]).transitions  # [T,F,T]
    assert t.transition is Lifecycle.RAISED

    h2 = harness()
    h2.step(at(0), [finding()])
    h2.step(at(1))
    h2.step(at(2))
    assert h2.step(at(3), [finding()]).transitions == ()  # [F,F,T]: 첫 검출은 창 밖
    [t] = h2.step(at(4), [finding()]).transitions
    assert t.transition is Lifecycle.RAISED


def test_reevaluating_the_same_observation_is_not_a_new_frame(harness):
    h = harness()
    h.step(at(0), [finding()])
    assert h.step(at(0), [finding()], lag=150).transitions == ()  # 같은 관측을 다시 판정
    assert h.step(at(0), [finding()], lag=200).transitions == ()
    assert list(h.tracker.pending().values()) == [1]
    [t] = h.step(at(1), [finding()]).transitions
    assert t.transition is Lifecycle.RAISED


def test_holding_does_not_count_toward_persistence(harness):
    h = harness()
    h.step(at(0), [finding()])
    assert h.step(at(1), [finding(holding=True)]).transitions == ()  # 관측되는 사람의 holding 은 음수 프레임
    assert h.step(at(2), [finding(holding=True)]).transitions == ()
    assert h.step(at(3), [finding(holding=True)]).transitions == ()
    assert h.tracker.active() == ()


def test_r7_is_immediate_by_default_and_configurable(harness):
    zone = finding(rule_id="R7", world_ids=(), severity=Severity.CAUTION, reasons=("monitoring_degraded",))
    [t] = harness().step(at(0), [zone]).transitions
    assert t.transition is Lifecycle.RAISED and t.state.rule_id == "R7"

    h = harness(policy=LifecyclePolicy(persistence_by_rule={}))  # R7 도 3프레임 중 2
    assert h.step(at(0), [zone]).transitions == ()
    [t] = h.step(at(1), [zone]).transitions
    assert t.transition is Lifecycle.RAISED


def test_immediate_rule_raises_even_on_a_repeated_observation(harness):
    """감시 상태는 영상이 멈춰도(관측 시각 정지) 바뀐다. 즉시 규칙은 프레임을 세지 않으므로 그때도 발생한다."""
    zone = finding(rule_id="R7", world_ids=(), severity=Severity.CAUTION, reasons=("monitoring_degraded",))
    h = harness()
    h.step(at(0))
    [t] = h.step(at(0), [zone], lag=300).transitions
    assert t.transition is Lifecycle.RAISED


# --- 지속 대기 중 관측 상실 (S15P31A101-73 리뷰 틈) --------------------------------------------


@pytest.mark.parametrize("lost", ["UNKNOWN", "PREDICTED", "missing"])
def test_observation_lost_while_pending_raises_unverifiable_with_predicted(harness, lost):
    h = harness()
    assert h.step(at(0), [finding(confidence_reasons=(R.SINGLE_VIEW,))]).transitions == ()
    step = h.step(at(1), tracks=[]) if lost == "missing" else h.step(at(1), state=lost)
    [t] = step.transitions
    assert t.transition is Lifecycle.RAISED and t.changes == (Change.RAISED,)
    assert t.state.verification is Verification.UNVERIFIABLE
    assert t.state.needs_check and t.state.confidence_reasons == (R.PREDICTED, R.SINGLE_VIEW)
    assert t.state.severity is Severity.HIGH and t.state.zone_id == "z-04"
    assert t.state.reasons[-1] == REASON_PENDING_OBSERVATION_LOST
    [alert] = step.local_signals
    assert isinstance(alert, LocalAlert) and alert.needs_check
    # 관측 상실이 이어져도 자동 해소하지 않는다(6.4, G3)
    for k in range(2, 600):
        later = h.step(at(k), tracks=[]) if lost == "missing" else h.step(at(k), state=lost)
        assert all(x.transition is not Lifecycle.RESOLVED for x in later.transitions)
    [state] = h.tracker.active()
    assert state.verification is Verification.UNVERIFIABLE


def test_observed_person_whose_finding_disappears_makes_no_event(harness):
    h = harness()
    h.step(at(0), [finding()])
    for k in (1, 2, 3):
        assert h.step(at(k)).transitions == ()
    # 창이 지난 뒤 관측을 잃어도 대기 중이 아니므로 발생하지 않는다
    assert h.step(at(4), state="UNKNOWN").transitions == ()
    assert h.tracker.active() == ()


def test_stale_observation_while_pending_also_raises(harness):
    """OBSERVED 라도 대상의 새 관측이 없으면(gap 초과) 검증할 수 없다(6.1)."""
    h = harness()
    h.step(at(0), [finding()])
    stale = [h.track_json(observation_state="OBSERVED", t_last_valid=at(0))]
    [t] = h.step(at(5), tracks=stale).transitions  # gap 500ms > 200ms
    assert t.state.verification is Verification.UNVERIFIABLE and R.PREDICTED in t.state.confidence_reasons


@pytest.mark.parametrize("origin", NON_LIVE)
def test_non_live_pending_loss_has_transition_but_no_signal(harness, origin):
    h = harness(origin=origin)
    h.step(at(0), [finding()])
    step = h.step(at(1), state="UNKNOWN")
    assert [t.transition for t in step.transitions] == [Lifecycle.RAISED] and step.local_signals == ()


class InsideZoneRule:
    """R1 흉내: 관측·예측 위치가 구역 안이면 구역 등급으로 판정(UNKNOWN 은 판정하지 않음, R6 이 맡는다)."""

    spec = RuleSpec(rule_id="R1", rule_version=1, reads=("tracks.position_xy", "zones"))

    def evaluate(self, ctx: RuleContext):
        for t in ctx.world.tracks:
            if t.observation_state.value == "UNKNOWN":
                continue
            for zone in ctx.zones:
                if point_in_polygon(t.position_xy, zone.polygon_xy):
                    yield Finding("R1", 1, zone.severity, (t.world_id,), zone.zone_id, reasons=("xy_inside_polygon",))


def test_entry_then_immediate_loss_alarms_with_r6_in_the_loop(track_json, zone_json):
    """진입 한 프레임 뒤 관측 상실: R6 holding 만 남아 경보 없이 끝나지 않고, 발생(UNVERIFIABLE + PREDICTED)한다."""
    clock = FixedClock(T0)
    engine = RiskEngine([ObservationLossHold(InsideZoneRule())], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id=GATEWAY, clock=clock)
    zones = [Zone.from_json(zone_json())]

    def step(obs, state):
        clock.set(obs + 100)
        t_last_valid = obs if state == "OBSERVED" else T0
        w = WorldState.from_payload(
            {
                "t_latest_global": obs,
                "calibration_version": "cal-7",
                "scene_version": "scene-3",
                "tracks": [track_json(observation_state=state, t_last_valid=t_last_valid)],
            }
        )
        return tracker.apply(engine.evaluate(w, zones, Origin.LIVE), w, config_version="cfg-12")

    assert step(T0, "OBSERVED").transitions == ()  # 진입 첫 프레임(지속 조건 대기)
    lost = step(T0 + 100, "UNKNOWN")
    [t] = lost.transitions
    assert t.transition is Lifecycle.RAISED and t.state.rule_id == "R1"
    assert t.state.verification is Verification.UNVERIFIABLE and t.state.confidence_reasons == (R.PREDICTED,)
    assert [type(s) for s in lost.local_signals] == [LocalAlert]
    for k in range(2, 100):
        assert all(x.transition is not Lifecycle.RESOLVED for x in step(T0 + k * 100, "UNKNOWN").transitions)


def test_r6_holding_carries_predicted_reason(track_json, zone_json):
    rule = ObservationLossHold(InsideZoneRule())
    w = WorldState.from_payload(
        {
            "t_latest_global": T0 + 600,
            "calibration_version": "cal-7",
            "scene_version": "scene-3",
            "tracks": [track_json(observation_state="UNKNOWN", t_last_valid=T0)],
        }
    )
    [f] = rule.evaluate(RuleContext(now_ms=T0 + 700, world=w, zones=(Zone.from_json(zone_json()),)))
    assert f.holding and f.needs_check and f.confidence_reasons == (R.PREDICTED,)


# --- 재개 (6.4, 7.4, D-G6-08, G6) -------------------------------------------------------------


def _raise_and_resolve(h, start=0):
    h.step(at(start), [finding()])
    [raised] = h.step(at(start + 1), [finding()]).transitions
    h.step(at(start + 2))
    [resolved] = h.step(at(start + 2) + 3000).transitions
    assert resolved.transition is Lifecycle.RESOLVED
    return raised, resolved


def test_recurrence_within_10s_resumes_the_same_event(harness):
    h = harness()
    raised, _ = _raise_and_resolve(h)
    assert h.tracker.acknowledge(raised.event_id, AckActor.ADMIN, "admin-1", at(1)) is None  # 해소된 이벤트
    t_resolved = at(2) + 3000
    h.step(t_resolved + 5000, [finding(confidence_reasons=(R.Z_UNKNOWN,))])
    step = h.step(t_resolved + 5100, [finding(confidence_reasons=(R.Z_UNKNOWN,))])
    [t] = step.transitions
    assert t.event_id == raised.event_id and t.transition is Lifecycle.UPDATED and t.changes == (Change.RESUMED,)
    assert t.resumed and t.event_sequence == 3  # RAISED 1, RESOLVED 2, 재개 3
    assert t.state.lifecycle is Lifecycle.UPDATED and t.state.resume_count == 1
    assert t.state.raised_at == raised.state.raised_at and t.state.confidence_reasons == (R.Z_UNKNOWN,)
    payload = t.to_payload()
    assert payload["resumed"] is True and payload["transition"] == "UPDATED" and payload["confidence_reasons"] == [
        "Z_UNKNOWN"
    ]
    assert t.state.to_payload()["resume_count"] == 1
    assert [type(s) for s in step.local_signals] == [LocalAlert]
    assert h.tracker.get(raised.event_id) is not None


def test_resume_resets_acknowledgement_and_rings_again(harness):
    h = harness()
    h.step(at(0), [finding()])
    [raised] = h.step(at(1), [finding()]).transitions
    ack = h.tracker.acknowledge(raised.event_id, AckActor.SITE_BUTTON, "laptop-01", at(1) + 50)
    assert ack.transitions[0].state.acknowledged
    h.step(at(2))
    resolved = h.step(at(2) + 3000)
    assert [type(s) for s in resolved.local_signals] == [AlertClear]
    h.step(at(2) + 4000, [finding()])
    step = h.step(at(2) + 4100, [finding()])
    [t] = step.transitions
    assert t.resumed and not t.state.acknowledged and t.state.local_alerted
    assert [type(s) for s in step.local_signals] == [LocalAlert]
    assert not t.to_payload()["acknowledged"]


def test_resume_window_boundary(harness):
    h = harness(policy=LifecyclePolicy(persistence=IMMEDIATE))
    [raised] = h.step(at(0), [finding()]).transitions
    h.step(at(1))
    h.step(at(1) + 3000)  # 해소
    t_resolved = at(1) + 3000
    [t] = h.step(t_resolved + 10_000, [finding()]).transitions
    assert t.resumed and t.event_id == raised.event_id
    h.step(t_resolved + 10_100)
    h.step(t_resolved + 13_100)  # 다시 해소
    t_resolved2 = t_resolved + 13_100
    [t] = h.step(t_resolved2 + 10_001, [finding()]).transitions
    assert t.transition is Lifecycle.RAISED and t.event_id != raised.event_id and not t.resumed


def test_resume_disabled_makes_a_new_event(harness):
    h = harness(policy=LifecyclePolicy(resume_window_ms=None))
    raised, _ = _raise_and_resolve(h)
    h.step(at(2) + 4000, [finding()])
    [t] = h.step(at(2) + 4100, [finding()]).transitions
    assert t.transition is Lifecycle.RAISED and t.event_id != raised.event_id


def test_resume_is_per_key(harness, track_json):
    """다른 사람·다른 규칙은 재개 대상이 아니다(키 = 규칙 + 사람, 구역 대상은 규칙 + 구역)."""
    h = harness()
    raised, _ = _raise_and_resolve(h)
    other = [track_json(), track_json(world_id="w-0031")]
    h.step(at(2) + 4000, [finding(world_ids=("w-0031",)), finding(rule_id="R2")], tracks=other)
    step = h.step(at(2) + 4100, [finding(world_ids=("w-0031",)), finding(rule_id="R2")], tracks=other)
    assert [t.transition for t in step.transitions] == [Lifecycle.RAISED, Lifecycle.RAISED]
    assert raised.event_id not in {t.event_id for t in step.transitions}


def test_flicker_for_a_minute_creates_one_event(harness):
    """경계 깜빡임 1분: 해소 → 10초 안 재발생이 반복돼도 RAISED 는 한 번(G6 새 이벤트 0건)."""
    h = harness()
    transitions = []
    obs = T0
    for _cycle in range(8):
        # 안: 두 프레임(발생·재개), 밖: 3초 + 한 프레임(해소), 쉬고 2초 뒤 다시
        transitions += h.step(obs, [finding()]).transitions
        transitions += h.step(obs + 100, [finding()]).transitions
        transitions += h.step(obs + 200).transitions
        transitions += h.step(obs + 3200).transitions
        obs += 5200
    raised = [t for t in transitions if t.transition is Lifecycle.RAISED]
    resumed = [t for t in transitions if t.resumed]
    assert len(raised) == 1 and len(resumed) == 7
    assert {t.event_id for t in transitions} == {raised[0].event_id}
    sequences = [t.event_sequence for t in transitions]
    assert sequences == list(range(1, len(sequences) + 1))


@pytest.mark.parametrize("origin", NON_LIVE)
def test_non_live_resume_has_no_signal(harness, origin):
    h = harness(origin=origin)
    _raise_and_resolve(h)
    h.step(at(2) + 4000, [finding()])
    step = h.step(at(2) + 4100, [finding()])
    assert [t.resumed for t in step.transitions] == [True] and step.local_signals == ()


# --- 지속 대기 중 관측 시각 정지 (리뷰 R1: 영상 정지·lag 초과에서도 발생) --------------------------


def _stall_tracks(h, case):
    """관측 시각 T0 에 멈춘 World 의 트랙. OBSERVED 는 상태 문자열만 남고 lag 만 커지는 경우다."""
    if case == "missing":
        return []
    if case == "UNKNOWN":
        return [h.track_json(observation_state="UNKNOWN", t_last_valid=T0)]
    return [h.track_json(observation_state="OBSERVED", t_last_valid=T0)]


@pytest.mark.parametrize("case", ["OBSERVED", "UNKNOWN", "missing"])
@pytest.mark.parametrize("origin", [Origin.LIVE, *NON_LIVE])
def test_stall_while_pending_raises_once_lag_exceeds(harness, case, origin):
    """영상 전체 정지: 관측 시각은 멈추고 판정 주기 시계만 간다(6.1). 프레임은 세지 않지만 안전 검사는 한다."""
    h = harness(origin=origin)
    assert h.step(T0, [finding()]).transitions == ()
    tracks = _stall_tracks(h, case)
    if case == "OBSERVED":
        # 대상 자신은 최신(gap 0)이라 lag 기준(600ms) 안에서는 대기한다. 같은 관측의 발생 판정도 세지 않는다
        assert h.step(T0, [finding()], tracks=tracks, lag=400).transitions == ()
        assert h.step(T0, tracks=tracks, lag=600).transitions == ()
        step = h.step(T0, tracks=tracks, lag=700)
    else:
        step = h.step(T0, tracks=tracks, lag=150)  # 대상이 보이지 않음: lag 와 관계없이 바로
    [t] = step.transitions
    assert t.transition is Lifecycle.RAISED and t.state.verification is Verification.UNVERIFIABLE
    assert t.state.confidence_reasons == (R.PREDICTED,) and t.state.observation_time == T0
    assert [type(s) for s in step.local_signals] == ([LocalAlert] if origin is Origin.LIVE else [])
    assert h.tracker.pending() == {}
    for lag in (2_000, 60_000):  # 정지가 이어져도 자동 해소 없음
        assert all(x.transition is not Lifecycle.RESOLVED for x in h.step(T0, tracks=tracks, lag=lag).transitions)
    [state] = h.tracker.active()
    assert state.verification is Verification.UNVERIFIABLE


def test_stall_with_r6_holding_in_the_loop(track_json, zone_json):
    """R6 결합: 진입 한 프레임 뒤 영상이 멈추고 대상이 UNKNOWN(R6 holding 만 남음) → 발생해 경보가 난다."""
    clock = FixedClock(T0)
    engine = RiskEngine([ObservationLossHold(InsideZoneRule())], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id=GATEWAY, clock=clock)
    zones = [Zone.from_json(zone_json())]

    def step(now, state):
        clock.set(now)
        w = WorldState.from_payload(
            {
                "t_latest_global": T0,
                "calibration_version": "cal-7",
                "scene_version": "scene-3",
                "tracks": [track_json(observation_state=state, t_last_valid=T0)],
            }
        )
        evaluation = engine.evaluate(w, zones, Origin.LIVE)
        return evaluation, tracker.apply(evaluation, w, config_version="cfg-12")

    _, first = step(T0 + 100, "OBSERVED")
    assert first.transitions == ()
    evaluation, stalled = step(T0 + 700, "UNKNOWN")  # 관측 시각 T0 그대로
    assert [f.holding for f in evaluation.findings] == [True]  # R6 holding 만
    [t] = stalled.transitions
    assert t.transition is Lifecycle.RAISED and t.state.verification is Verification.UNVERIFIABLE
    assert t.state.confidence_reasons == (R.PREDICTED,)
    assert [type(s) for s in stalled.local_signals] == [LocalAlert]


def test_stall_while_resume_pending_resumes(harness):
    h = harness()
    raised, _ = _raise_and_resolve(h)
    t_back = at(2) + 3000 + 2000
    assert h.step(t_back, [finding()]).transitions == ()  # 재발생 1프레임(대기)
    stalled = [h.track_json(observation_state="OBSERVED", t_last_valid=t_back)]
    step = h.step(t_back, tracks=stalled, lag=700)  # 영상 정지, lag 초과
    [t] = step.transitions
    assert t.resumed and t.event_id == raised.event_id and t.state.verification is Verification.UNVERIFIABLE
    assert R.PREDICTED in t.state.confidence_reasons and [type(s) for s in step.local_signals] == [LocalAlert]


def test_resume_window_is_judged_when_persistence_is_met(harness):
    """기본 정책(3프레임 중 2): 첫 재발생 프레임이 창 안이어도 지속 조건을 채운 판정이 창 밖이면 새 이벤트."""
    h = harness()
    raised, _ = _raise_and_resolve(h)
    t_resolved = at(2) + 3000
    h.step(t_resolved + 9_900, [finding()])
    [t] = h.step(t_resolved + 10_000, [finding()]).transitions  # 확정이 정확히 10초 → 재개
    assert t.resumed and t.event_id == raised.event_id

    h2 = harness()
    raised2, _ = _raise_and_resolve(h2)
    h2.step(t_resolved + 10_000, [finding()])  # 첫 재발생은 10초(창 안)
    [t] = h2.step(t_resolved + 10_100, [finding()]).transitions  # 확정은 10.1초(창 밖)
    assert t.transition is Lifecycle.RAISED and t.event_id != raised2.event_id
