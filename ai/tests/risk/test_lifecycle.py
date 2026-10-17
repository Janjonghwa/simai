"""RiskEvent 생명주기 (명세 3.2, 6.4, 7.3, 7.4, 5.2 ID 교정, 수정 6 ACK, G3)."""

import json

import pytest

from simai_ai.risk import (
    AckActor,
    AlarmStop,
    AlertClear,
    Change,
    Evaluation,
    EventIdFactory,
    Finding,
    FixedClock,
    IdentityCorrection,
    Lifecycle,
    LifecycleError,
    LifecyclePolicy,
    LifecycleTracker,
    LocalAlert,
    NonLiveAlertError,
    Origin,
    Severity,
    Verification,
    WorldState,
)
from simai_ai.risk.event_id import is_uuid7

T0 = 1760342400100  # conftest 의 T0 와 같다

GATEWAY = "tier0-01"
NON_LIVE = [Origin.PLANNED, Origin.WHAT_IF, Origin.REPLAY]


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
    """주입 시계 하나로 판정을 만들어 추적기에 넣는다. 관측 시각은 판정 시각보다 100ms 앞이다."""

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

    def world(self, tracks, fresh=True, observed_at=None):
        """fresh 면 모든 트랙을 이 관측 시각에 관측한 것으로 둔다(t_last_valid = t_latest_global)."""
        t_obs = self.clock.now_ms() - 100 if observed_at is None else observed_at
        tracks = [self.track_json()] if tracks is None else tracks
        if fresh:
            tracks = [t | {"t_last_valid": t_obs} for t in tracks]
        return WorldState.from_payload(
            {"t_latest_global": t_obs, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": tracks}
        )

    def step(self, findings=(), tracks=None, dt=100, fresh=True, observed_at=None):
        self.clock.advance(dt)
        world = self.world(tracks, fresh, observed_at)
        evaluation = Evaluation(
            origin=self.origin,
            evaluated_at=self.clock.now_ms(),
            observation_time=world.t_latest_global,
            calibration_version=world.calibration_version,
            scene_version=world.scene_version,
            rules=(),
            zones=(),
            findings=tuple(sorted(findings, key=lambda f: f.sort_key)),
        )
        return self.tracker.apply(evaluation, world, config_version="cfg-12")

    def hold(self, ms, findings=(), tracks=None, dt=100, fresh=True):
        """ms 동안 dt 간격으로 같은 입력을 넣고 모든 결과를 돌려준다."""
        return [self.step(findings, tracks, dt, fresh) for _ in range(ms // dt)]


@pytest.fixture
def harness(track_json):
    def make(**kwargs):
        return Harness(track_json, **kwargs)

    return make


def transitions(steps):
    return [t for s in steps for t in s.transitions]


def signals(steps):
    return [s for st in steps for s in st.local_signals]


# --- 발생 ---------------------------------------------------------------------------


def test_raise_creates_uuid7_event_with_sequence_1_and_local_alert(harness):
    h = harness()
    step = h.step([finding(severity=Severity.HIGH)])
    (t,) = step.transitions
    assert t.transition is Lifecycle.RAISED and t.changes == (Change.RAISED,)
    assert is_uuid7(t.event_id) and t.state.gateway_id == GATEWAY and t.event_sequence == 1
    assert t.state.world_ids == t.state.origin_world_ids == ("w-0012",)
    assert t.state.verification is Verification.VERIFIED and not t.state.acknowledged
    assert t.state.config_version == "cfg-12" and t.state.calibration_version == "cal-7"
    (alert,) = step.local_signals
    assert isinstance(alert, LocalAlert)
    assert (alert.event_id, alert.severity, alert.gateway_id, alert.event_sequence) == (
        t.event_id,
        Severity.HIGH,
        GATEWAY,
        1,
    )


@pytest.mark.parametrize("severity", [Severity.CAUTION, Severity.INFO])
def test_low_severity_has_no_local_alert(harness, severity):
    step = harness().step([finding(severity=severity)])
    assert len(step.transitions) == 1 and step.local_signals == ()


def test_low_confidence_keeps_severity_and_alerts_with_needs_check(harness):
    """7.3: 신뢰도가 낮아도 등급 유지 + 같은 경보 + '확인 필요'."""
    step = harness().step([finding(severity=Severity.CRITICAL, needs_check=True)])
    (t,) = step.transitions
    assert t.state.severity is Severity.CRITICAL and str(t.state.confidence) == "LOW"
    (alert,) = step.local_signals
    assert alert.severity is Severity.CRITICAL and alert.needs_check is True


def test_holding_alone_does_not_raise(harness):
    step = harness().step([finding(holding=True)])
    assert step.transitions == () and step.local_signals == ()


def test_evidence_and_payload_are_json_ready(harness):
    (t,) = harness().step([finding()]).transitions
    (evidence,) = t.evidence
    assert evidence.present and evidence.cameras == ("cam1", "cam3") and evidence.ground_anchor_xyz is not None
    payload = json.loads(json.dumps(t.to_payload()))
    assert payload["transition"] == "RAISED" and payload["source"] == "LIVE" and payload["event_sequence"] == 1
    assert payload["evidence"][0]["world_id"] == "w-0012"


# --- 병합(7.4) ----------------------------------------------------------------------


def test_same_person_same_rule_is_one_event(harness):
    h = harness()
    first = h.step([finding()])
    again = h.hold(1000, [finding()])
    assert len(first.transitions) == 1 and transitions(again) == [] and signals(again) == []
    assert len(h.tracker.active()) == 1


def test_same_rule_two_zones_merge_with_highest_severity(harness):
    h = harness()
    (t,) = h.step(
        [finding(zone_id="z-01", severity=Severity.HIGH), finding(zone_id="z-02", severity=Severity.CRITICAL)]
    ).transitions
    assert t.state.severity is Severity.CRITICAL and t.state.zone_id == "z-02"


def test_new_event_severity_comes_from_raising_findings_only(harness):
    """holding 판정은 기존 이벤트 유지용이다. 더 높은 등급의 holding 이 새 이벤트 등급·현장 경보를 정하지 않는다."""
    h = harness()
    mixed = [
        finding(zone_id="z-caution", severity=Severity.CAUTION),
        finding(zone_id="z-critical", severity=Severity.CRITICAL, holding=True),
    ]
    step = h.step(mixed)
    (t,) = step.transitions
    assert t.state.severity is Severity.CAUTION and t.state.zone_id == "z-caution"
    assert step.local_signals == ()
    # 다음 판정에서도 holding 이 활성 이벤트를 발생 등급 위로 올리지 않는다.
    again = h.hold(1000, mixed)
    assert transitions(again) == [] and signals(again) == []
    assert h.tracker.active()[0].severity is Severity.CAUTION


def test_holding_keeps_current_severity_within_band(harness):
    """활성 HIGH 이벤트: 발생 판정이 CAUTION 이어도 HIGH 띠 안쪽(holding)이면 HIGH 를 유지한다."""
    h = harness()
    h.step([finding(severity=Severity.HIGH)])
    steps = h.hold(
        1000, [finding(zone_id="z-05", severity=Severity.CAUTION), finding(severity=Severity.CRITICAL, holding=True)]
    )
    assert transitions(steps) == [] and signals(steps) == []
    assert h.tracker.active()[0].severity is Severity.HIGH


def test_zone_change_updates_same_event(harness):
    h = harness()
    (raised,) = h.step([finding(zone_id="z-01")]).transitions
    (updated,) = h.step([finding(zone_id="z-02")]).transitions
    assert updated.event_id == raised.event_id and updated.changes == (Change.ZONE,) and updated.event_sequence == 2


def test_holding_in_other_zone_keeps_zone_severity_and_metrics(harness):
    """holding 은 발생 판정이 아니므로 구역을 바꾸지 않는다. 확인 필요·근거만 반영하고 유지한다."""
    h = harness()
    (raised,) = h.step([finding(zone_id="z-01", metrics={"zone_distance_m": 0.0})]).transitions
    held = [
        finding(
            zone_id="z-02",
            severity=Severity.CAUTION,
            needs_check=True,
            holding=True,
            reasons=("observation_lost",),
            metrics={"zone_distance_m": 0.3},
        )
    ]
    (updated,) = h.step(held).transitions
    assert updated.event_id == raised.event_id and updated.changes == (Change.CONFIDENCE,)
    assert updated.state.zone_id == "z-01" and updated.state.severity is Severity.HIGH
    assert dict(updated.state.metrics) == {"zone_distance_m": 0.0} and updated.state.needs_check
    later = transitions(h.hold(10_000, held))
    assert all(t.transition is Lifecycle.UPDATED and Change.ZONE not in t.changes for t in later)
    [state] = h.tracker.active()  # 해소되지 않고 원래 구역으로 유지
    assert state.event_id == raised.event_id and state.zone_id == "z-01" and state.severity is Severity.HIGH


def test_lower_holding_in_same_zone_does_not_lower_severity(harness):
    """holding 은 등급을 올리지도 내리지도 않는다(7.2 R6 '원래 규칙 유지', 7.3 등급 유지)."""
    h = harness()
    h.step([finding(severity=Severity.CRITICAL)])
    steps = h.hold(1000, [finding(severity=Severity.HIGH, holding=True)])
    assert all(Change.SEVERITY not in t.changes for t in transitions(steps)) and signals(steps) == []
    assert h.tracker.active()[0].severity is Severity.CRITICAL


def test_holding_prefers_event_zone_over_other_zone(harness):
    h = harness()
    h.step([finding(zone_id="z-02")])
    (t,) = h.step(
        [
            finding(zone_id="z-01", needs_check=True, holding=True),  # 정렬 순서는 z-01 이 앞선다
            finding(zone_id="z-02", needs_check=True, holding=True, metrics={"zone_distance_m": 0.0}),
        ]
    ).transitions
    assert t.changes == (Change.CONFIDENCE,) and t.state.zone_id == "z-02"
    assert dict(t.state.metrics) == {"zone_distance_m": 0.0}


def test_raising_finding_in_other_zone_still_changes_zone(harness):
    """발생 판정의 구역 변경(7.4)은 그대로다. 같은 등급이면 발생 판정이 holding 보다 앞선다."""
    h = harness()
    h.step([finding(zone_id="z-02")])
    (t,) = h.step([finding(zone_id="z-03"), finding(zone_id="z-02", holding=True)]).transitions
    assert t.changes == (Change.ZONE,) and t.state.zone_id == "z-03"


def test_same_person_multiple_rules_share_group_key(harness, track_json):
    h = harness()
    tracks = [track_json(world_id="w-0012"), track_json(world_id="w-0013")]
    h.step([finding("R1"), finding("R2", severity=Severity.CRITICAL), finding("R1", world_ids=("w-0013",))], tracks)
    groups = h.tracker.groups()
    assert sorted(groups) == ["person:w-0012", "person:w-0013"]
    assert [s.rule_id for s in groups["person:w-0012"]] == ["R1", "R2"]
    assert len({s.event_id for s in h.tracker.active()}) == 3


def test_zone_target_event_is_keyed_by_zone(harness):
    """R7 형태(사람 없음): (규칙, 구역)이 키, 기본 정책에서 VERIFIED."""
    h = harness()
    (t,) = h.step([finding("R7", world_ids=(), zone_id="z-04")], tracks=[]).transitions
    assert t.state.group_key == "zone:z-04" and t.state.verification is Verification.VERIFIED
    assert h.hold(1000, [finding("R7", world_ids=(), zone_id="z-04")], tracks=[]) and len(h.tracker.active()) == 1


# --- 해소 -----------------------------------------------------------------------------


def test_resolves_only_after_clear_hold_with_valid_observation(harness):
    h = harness(policy=LifecyclePolicy(clear_hold_ms=3000))
    (raised,) = h.step([finding()]).transitions
    before = h.hold(3000)  # 해소 확인 시작부터 2900ms 까지
    assert transitions(before) == []
    resolved = h.step()
    (t,) = resolved.transitions
    assert t.transition is Lifecycle.RESOLVED and t.changes == (Change.CLEARED,)
    assert t.event_id == raised.event_id and t.event_sequence == 2
    (clear,) = resolved.local_signals
    assert isinstance(clear, AlertClear) and clear.event_id == raised.event_id
    assert h.tracker.active() == ()


def test_rule_specific_hold(harness):
    h = harness(policy=LifecyclePolicy(clear_hold_ms=3000, clear_hold_ms_by_rule={"R1": 500}))
    h.step([finding()])
    steps = h.hold(700)
    assert [t.transition for t in transitions(steps)] == [Lifecycle.RESOLVED]


def test_finding_reappearing_resets_clear_hold(harness):
    h = harness()
    h.step([finding()])
    h.hold(2000)
    h.step([finding()])
    assert transitions(h.hold(3000)) == []  # 다시 처음부터 센다
    assert transitions([h.step()])[0].transition is Lifecycle.RESOLVED


def test_holding_keeps_event_alive(harness):
    h = harness()
    h.step([finding()])
    assert transitions(h.hold(10_000, [finding(holding=True)])) == []
    assert len(h.tracker.active()) == 1


def test_new_raise_after_resolution_is_new_event(harness):
    h = harness(policy=LifecyclePolicy(clear_hold_ms=0))
    (first,) = h.step([finding()]).transitions
    assert h.step().transitions[0].transition is Lifecycle.RESOLVED
    (second,) = h.step([finding()]).transitions
    assert second.transition is Lifecycle.RAISED and second.event_id != first.event_id and second.event_sequence == 1


# --- 관측 상실: 자동 해소 없음(6.4, G3) --------------------------------------------------


@pytest.mark.parametrize(
    "lost",
    [
        pytest.param(lambda tj: [], id="track-gone"),
        pytest.param(lambda tj: [tj(observation_state="UNKNOWN")], id="unknown"),
        pytest.param(lambda tj: [tj(observation_state="PREDICTED")], id="predicted"),
        pytest.param(lambda tj: [tj(identity_confidence="IDENTITY_UNCERTAIN")], id="identity-uncertain"),
    ],
)
def test_observation_loss_is_unverifiable_and_never_auto_resolves(harness, track_json, lost):
    h = harness(policy=LifecyclePolicy(clear_hold_ms=3000))
    (raised,) = h.step([finding()]).transitions
    (t,) = h.step(tracks=lost(track_json)).transitions
    assert t.transition is Lifecycle.UPDATED and t.changes == (Change.VERIFICATION,)
    assert t.state.verification is Verification.UNVERIFIABLE
    assert transitions(h.hold(60_000, tracks=lost(track_json))) == []  # 1분이 지나도 해소하지 않는다
    assert [s.event_id for s in h.tracker.active()] == [raised.event_id]

    # 다시 관측되면 VERIFIED 로 돌아오고 그때부터 해소 확인을 센다.
    (back,) = h.step().transitions
    assert back.changes == (Change.VERIFICATION,) and back.state.verification is Verification.VERIFIED
    assert transitions(h.hold(2900)) == []
    assert h.step().transitions[0].transition is Lifecycle.RESOLVED


def test_stale_observed_track_is_unverifiable_and_never_auto_resolves(harness, track_json):
    """6.1: OBSERVED 도 최신이라는 뜻이 아니다. 다른 카메라 영상만 진행하고 대상의 새 관측이 없으면 해소하지 않는다."""
    h = harness(policy=LifecyclePolicy(clear_hold_ms=3000))
    (raised,) = h.step([finding()]).transitions
    last_seen = h.clock.now_ms() - 100
    stale = [track_json(t_last_valid=last_seen)]  # observation_state 는 OBSERVED 그대로
    (t,) = h.step(tracks=stale, dt=300, fresh=False).transitions  # gap 300ms
    assert t.changes == (Change.VERIFICATION,) and t.state.verification is Verification.UNVERIFIABLE
    assert transitions(h.hold(60_000, tracks=stale, fresh=False)) == []
    assert [s.event_id for s in h.tracker.active()] == [raised.event_id]

    # 대상이 다시 관측되면 그때부터 해소 확인을 센다.
    (back,) = h.step().transitions
    assert back.changes == (Change.VERIFICATION,) and back.state.verification is Verification.VERIFIED
    assert transitions(h.hold(2900)) == []
    assert h.step().transitions[0].transition is Lifecycle.RESOLVED


def test_whole_video_stall_is_unverifiable_and_never_auto_resolves(harness):
    """6.1: 영상이 모두 멈추면 lag 가 커진다. 멈춘 관측으로는 해소를 확인하지 않는다."""
    h = harness(policy=LifecyclePolicy(clear_hold_ms=3000))
    (raised,) = h.step([finding()]).transitions
    frozen = h.clock.now_ms() - 100
    stalled = [h.step(observed_at=frozen) for _ in range(600)]  # 판정 시계만 60초 진행
    (t,) = transitions(stalled)
    assert t.changes == (Change.VERIFICATION,) and t.state.verification is Verification.UNVERIFIABLE
    assert [s.event_id for s in h.tracker.active()] == [raised.event_id]
    (back,) = h.step().transitions  # 영상 재개
    assert back.state.verification is Verification.VERIFIED
    assert transitions(h.hold(2900)) == []
    assert h.step().transitions[0].transition is Lifecycle.RESOLVED


def test_unverifiable_while_finding_present(harness, track_json):
    """규칙이 예측 위치로 판정을 유지해도 관측이 아니면 UNVERIFIABLE 이다(R6 확장 지점)."""
    h = harness()
    h.step([finding()])
    (t,) = h.step([finding(holding=True)], tracks=[track_json(observation_state="PREDICTED")]).transitions
    assert t.state.verification is Verification.UNVERIFIABLE


# --- ID 교정(5.2, G3) ---------------------------------------------------------------------


def test_identity_revision_resets_clear_hold_and_does_not_resolve(harness, track_json):
    h = harness(policy=LifecyclePolicy(clear_hold_ms=3000))
    h.step([finding()], tracks=[track_json(identity_revision=2)])
    assert transitions(h.hold(2900, tracks=[track_json(identity_revision=2)])) == []
    (revised,) = h.step(tracks=[track_json(identity_revision=3)]).transitions  # 해소 직전에 ID 교정
    assert revised.transition is Lifecycle.UPDATED and revised.changes == (Change.IDENTITY_REVISED,)
    assert revised.state.origin_world_ids == ("w-0012",)
    assert transitions(h.hold(3000, tracks=[track_json(identity_revision=3)])) == []
    assert h.step(tracks=[track_json(identity_revision=3)]).transitions[0].transition is Lifecycle.RESOLVED


def test_identity_correction_rekeys_event_and_keeps_origin_ids(harness, track_json):
    h = harness()
    (raised,) = h.step([finding()]).transitions
    (t,) = h.tracker.correct_identity(IdentityCorrection("w-0012", "w-0020", 4)).transitions
    assert t.changes == (Change.IDENTITY_CORRECTED,) and t.transition is Lifecycle.UPDATED
    assert t.state.world_ids == ("w-0020",) and t.state.origin_world_ids == ("w-0012",)
    assert t.state.group_key == "person:w-0020" and t.event_sequence == 2
    # 새 ID 로 들어오는 같은 규칙 판정은 같은 이벤트를 갱신한다(중복 이벤트 없음).
    step = h.step([finding(world_ids=("w-0020",))], tracks=[track_json(world_id="w-0020", identity_revision=4)])
    assert all(t.transition is not Lifecycle.RAISED for t in step.transitions)
    assert [s.event_id for s in h.tracker.active()] == [raised.event_id]


def test_identity_correction_conflict_links_without_resolving(harness, track_json):
    h = harness()
    tracks = [track_json(world_id="w-0012"), track_json(world_id="w-0020")]
    h.step([finding(), finding(world_ids=("w-0020",))], tracks)
    old, other = sorted(h.tracker.active(), key=lambda s: s.world_ids)
    (t,) = h.tracker.correct_identity(IdentityCorrection("w-0012", "w-0020", 5)).transitions
    assert t.changes == (Change.IDENTITY_CONFLICT,) and t.state.related_event_id == other.event_id
    # 옛 ID 는 더 관측되지 않으므로 UNVERIFIABLE 로 남고 해소되지 않는다.
    later = h.hold(10_000, [finding(world_ids=("w-0020",))], [track_json(world_id="w-0020")])
    assert all(t.transition is not Lifecycle.RESOLVED for t in transitions(later))
    assert {s.event_id for s in h.tracker.active()} == {old.event_id, other.event_id}
    assert h.tracker.get(old.event_id).verification is Verification.UNVERIFIABLE


# --- 확인(ACK, 수정 6) -----------------------------------------------------------------


@pytest.mark.parametrize("actor", list(AckActor))
def test_ack_by_anyone_stops_sound_and_records_actor_but_does_not_resolve(harness, actor):
    h = harness()
    (raised,) = h.step([finding()]).transitions
    h.clock.advance(50)
    step = h.tracker.acknowledge(raised.event_id, actor, "op-1", T0 + 140)
    (t,) = step.transitions
    assert t.transition is Lifecycle.UPDATED and t.changes == (Change.ACKNOWLEDGED,)
    assert t.state.acknowledged and t.ack.actor is actor and t.ack.actor_id == "op-1"
    assert t.ack.acked_at == T0 + 140 and t.ack.recorded_at == h.clock.now_ms() and t.event_sequence == 2
    (stop,) = step.local_signals
    assert isinstance(stop, AlarmStop) and stop.acked_by is actor and stop.event_id == raised.event_id
    # 확인은 해소가 아니다. 판정이 남아 있으면 이벤트도 표시도 그대로다(AlertClear 없음).
    later = h.hold(10_000, [finding()])
    assert transitions(later) == [] and signals(later) == []
    assert h.tracker.get(raised.event_id).lifecycle is Lifecycle.UPDATED


def test_every_ack_is_recorded(harness):
    h = harness()
    (raised,) = h.step([finding()]).transitions
    h.tracker.acknowledge(raised.event_id, AckActor.SITE_BUTTON, "laptop-01", T0 + 150)
    h.tracker.acknowledge(raised.event_id, AckActor.ADMIN, "admin-1", T0 + 160)
    state = h.tracker.get(raised.event_id)
    assert [a.actor for a in state.acks] == [AckActor.SITE_BUTTON, AckActor.ADMIN] and state.event_sequence == 3


def test_ack_of_inactive_event_is_ignored(harness):
    h = harness(policy=LifecyclePolicy(clear_hold_ms=0))
    (raised,) = h.step([finding()]).transitions
    h.step()
    assert h.tracker.acknowledge(raised.event_id, AckActor.ADMIN, "admin-1", T0) is None
    assert h.tracker.acknowledge("unknown", AckActor.ADMIN, "admin-1", T0) is None


def test_ack_without_local_alert_has_no_alarm_stop(harness):
    h = harness()
    (raised,) = h.step([finding(severity=Severity.CAUTION)]).transitions
    step = h.tracker.acknowledge(raised.event_id, AckActor.FOREMAN, "foreman-1", T0)
    assert len(step.transitions) == 1 and step.local_signals == ()


def test_resolution_after_ack_clears_display(harness):
    h = harness(policy=LifecyclePolicy(clear_hold_ms=0))
    (raised,) = h.step([finding()]).transitions
    h.tracker.acknowledge(raised.event_id, AckActor.SITE_BUTTON, "laptop-01", T0)
    (clear,) = h.step().local_signals
    assert isinstance(clear, AlertClear)


# --- 등급 변화 ----------------------------------------------------------------------------


def test_escalation_into_alert_range_alerts(harness):
    h = harness()
    h.step([finding(severity=Severity.CAUTION)])
    step = h.step([finding(severity=Severity.HIGH)])
    assert step.transitions[0].changes == (Change.SEVERITY,)
    assert [type(s) for s in step.local_signals] == [LocalAlert]


def test_escalation_after_ack_alerts_again_and_downgrade_is_silent(harness):
    h = harness()
    (raised,) = h.step([finding(severity=Severity.HIGH)]).transitions
    h.tracker.acknowledge(raised.event_id, AckActor.ADMIN, "admin-1", T0)
    up = h.step([finding(severity=Severity.CRITICAL)])
    assert [s.severity for s in up.local_signals] == [Severity.CRITICAL]
    down = h.step([finding(severity=Severity.HIGH)])
    assert down.transitions[0].changes == (Change.SEVERITY,) and down.local_signals == ()


def test_confidence_change_is_an_update(harness):
    h = harness()
    h.step([finding()])
    (t,) = h.step([finding(needs_check=True)]).transitions
    assert t.changes == (Change.CONFIDENCE,) and t.state.needs_check


def test_metrics_only_updates_are_throttled(harness):
    h = harness(policy=LifecyclePolicy(metrics_update_interval_ms=1000))
    h.step([finding(metrics={"d_m": 1.0})])
    steps = [h.step([finding(metrics={"d_m": 1.0 - i / 100})]) for i in range(1, 21)]  # 2초, 100ms 간격
    updates = transitions(steps)
    assert [t.changes for t in updates] == [(Change.METRICS,), (Change.METRICS,)]
    assert h.tracker.active()[0].metrics["d_m"] == pytest.approx(0.8)  # 전이가 없어도 최신 값은 반영


def test_metrics_change_settling_within_interval_is_emitted_later(harness):
    """수치가 제한 간격 안에 한 번 바뀌고 고정돼도, 간격이 지나면 최신 수치로 전이를 낸다."""
    h = harness(policy=LifecyclePolicy(metrics_update_interval_ms=1000))
    h.step([finding(metrics={"d_m": 1.0})])
    steps = [h.step([finding(metrics={"d_m": 0.5})]) for _ in range(21)]
    (t,) = transitions(steps)
    assert t.changes == (Change.METRICS,) and t.state.metrics["d_m"] == 0.5
    assert t.state.evaluated_at - T0 == 1100  # 발생(T0+100)에서 1000ms 뒤
    assert transitions(h.hold(2000, [finding(metrics={"d_m": 0.5})])) == []  # 발행한 값과 같으면 더 내지 않는다


def test_rule_version_change_is_recorded(harness):
    """규칙 버전만 바뀌어도 감사용 전이를 남긴다(6.4 근거: 규칙 버전)."""
    h = harness()
    h.step([finding()])
    (t,) = h.step([Finding("R1", 2, Severity.HIGH, ("w-0012",), "z-04", reasons=("xy_inside_polygon",))]).transitions
    assert t.changes == (Change.VERSION,) and t.state.rule_version == 2


# --- 출처 분리(7.4, AGENTS.md 규칙 5) ---------------------------------------------------------


@pytest.mark.parametrize("origin", NON_LIVE)
def test_non_live_never_emits_local_signals(harness, origin, track_json):
    """PLANNED·WHAT_IF·REPLAY 는 전이는 만들되 현장 경보 신호는 절대 내지 않는다."""
    h = harness(origin=origin, policy=LifecyclePolicy(clear_hold_ms=0))
    steps = [h.step([finding(severity=Severity.CAUTION)]), h.step([finding(severity=Severity.CRITICAL)])]
    event_id = h.tracker.active()[0].event_id
    steps.append(h.tracker.acknowledge(event_id, AckActor.SITE_BUTTON, "laptop-01", T0))
    steps.append(h.step([finding(severity=Severity.CRITICAL, needs_check=True)]))
    steps.append(h.step(tracks=[]))
    steps.append(h.step())
    assert [t.transition for t in transitions(steps)] == [
        Lifecycle.RAISED,
        Lifecycle.UPDATED,
        Lifecycle.UPDATED,
        Lifecycle.UPDATED,
        Lifecycle.UPDATED,
        Lifecycle.RESOLVED,
    ]
    assert all(t.state.origin is origin for t in transitions(steps))
    assert signals(steps) == []


@pytest.mark.parametrize("origin", NON_LIVE)
@pytest.mark.parametrize("cls", [LocalAlert, AlarmStop, AlertClear])
def test_local_signal_cannot_be_built_for_non_live(origin, cls):
    common = {
        "event_id": "e",
        "gateway_id": GATEWAY,
        "event_sequence": 1,
        "issued_at": T0,
        "severity": Severity.HIGH,
        "needs_check": False,
    }
    extra = {
        LocalAlert: {"rule_id": "R1", "zone_id": None, "world_ids": ("w-1",), "observation_time": T0},
        AlarmStop: {"acked_by": AckActor.ADMIN, "actor_id": "admin-1"},
        AlertClear: {},
    }[cls]
    assert cls(origin=Origin.LIVE, **common, **extra).to_payload()["event_id"] == "e"
    with pytest.raises(NonLiveAlertError):
        cls(origin=origin, **common, **extra)


# --- 입력 계약 -----------------------------------------------------------------------------


def test_tracker_rejects_other_origin_and_time_reversal(harness):
    h = harness()
    h.step([finding()])
    world = h.world(None)
    evaluation = Evaluation(Origin.REPLAY, h.clock.now_ms(), world.t_latest_global, "cal-7", "scene-3", (), (), ())
    with pytest.raises(LifecycleError):
        h.tracker.apply(evaluation, world, config_version="cfg-12")
    stale = Evaluation(Origin.LIVE, T0, world.t_latest_global, "cal-7", "scene-3", (), (), ())
    with pytest.raises(LifecycleError):
        h.tracker.apply(stale, world, config_version="cfg-12")
    mismatched = Evaluation(Origin.LIVE, h.clock.now_ms(), world.t_latest_global - 1, "cal-7", "scene-3", (), (), ())
    with pytest.raises(LifecycleError):
        h.tracker.apply(mismatched, world, config_version="cfg-12")


# --- 체크포인트 복원(3.2) ---------------------------------------------------------------------


def test_restore_marks_unverifiable_continues_sequence_and_does_not_resolve(harness, track_json):
    h = harness()
    (raised,) = h.step([finding()]).transitions
    snapshot = h.tracker.active()

    restarted = harness()
    restarted.clock.set(h.clock.now_ms() + 5000)
    (t,) = restarted.tracker.restore(snapshot).transitions
    assert t.event_id == raised.event_id and t.event_sequence == 2
    assert t.changes == (Change.RESTORED, Change.VERIFICATION) and t.state.verification is Verification.UNVERIFIABLE
    # 재관측 전(트랙 없음)에는 해소하지 않는다.
    assert transitions(restarted.hold(10_000, tracks=[])) == []
    assert [s.event_id for s in restarted.tracker.active()] == [raised.event_id]
    with pytest.raises(LifecycleError):
        restarted.tracker.restore(snapshot)


def _journal_restore_then_ack_and_resolve(harness, journal):
    """저널의 마지막 전이 상태로 복원한 뒤 ACK·해소가 현장 신호를 내는지 본다."""
    restarted = harness(policy=LifecyclePolicy(clear_hold_ms=0))
    restarted.tracker.restore([journal.state])
    ack = restarted.tracker.acknowledge(journal.event_id, AckActor.ADMIN, "admin-1", T0)
    resolved = restarted.step()  # 판정 없이 재관측(해소 유지 시간 0)
    return ack, resolved


def test_restore_from_raised_transition_keeps_alarm_state(harness):
    """3.2: 저널이 복구 정본이다. 경보를 시작한 RAISED 전이 상태로 복원해도 ACK 는 소리를 멈추고 해소는 표시를 끈다."""
    h = harness()
    step = h.step([finding()])
    (raised,) = step.transitions
    assert [type(s) for s in step.local_signals] == [LocalAlert]
    assert raised.state.local_alerted is True and raised.state == h.tracker.active()[0]
    ack, resolved = _journal_restore_then_ack_and_resolve(harness, raised)
    assert [type(s) for s in ack.local_signals] == [AlarmStop]
    assert resolved.transitions[0].transition is Lifecycle.RESOLVED
    assert [type(s) for s in resolved.local_signals] == [AlertClear]


def test_restore_from_escalation_transition_keeps_alarm_state(harness):
    h = harness()
    h.step([finding(severity=Severity.CAUTION)])
    step = h.step([finding(severity=Severity.HIGH)])
    (up,) = step.transitions
    assert [type(s) for s in step.local_signals] == [LocalAlert]
    assert up.state.local_alerted is True and step.local_signals[0].event_sequence == up.event_sequence
    ack, resolved = _journal_restore_then_ack_and_resolve(harness, up)
    assert [type(s) for s in ack.local_signals] == [AlarmStop]
    assert [type(s) for s in resolved.local_signals] == [AlertClear]


def test_snapshot_payload_is_json_ready(harness):
    h = harness()
    (raised,) = h.step([finding()]).transitions
    h.tracker.acknowledge(raised.event_id, AckActor.SITE_BUTTON, "laptop-01", T0)
    (state,) = h.tracker.active()
    payload = json.loads(json.dumps(state.to_payload()))
    assert payload["acknowledged"] is True and payload["lifecycle"] == "UPDATED" and payload["local_alerted"] is True
