"""운영자 상향(event_escalate, 명세 5.4·12.3, S15P31A101-272): 관리자가 승인한 VLM 상향 의견으로 LIVE 이벤트 등급을 올린다.

상향은 같은 event_id 의 UPDATED 전이(escalation 은 받은 값 그대로, resumed false)로 남고, 규칙 상향과 같이 다시 울린다(3.10).
지금 등급보다 높지 않거나 활성이 아니면 거부하고 아무것도 바꾸지 않는다. 올린 등급은 해소될 때까지 바닥값이다.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from simai_ai.risk import (
    AckActor,
    Evaluation,
    Finding,
    FixedClock,
    LifecyclePolicy,
    LifecycleTracker,
    LocalAlert,
    Origin,
    Persistence,
    Severity,
    WorldState,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.lifecycle import (
    Change,
    CloseRequest,
    EscalateReject,
    EscalationRequest,
    EventState,
    Lifecycle,
    LifecycleError,
)

T0 = 1760342400100
MSG = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"
ESC_ID = "3c9d1e2f-4a5b-4c6d-9e8f-1a2b3c4d5e6f"
POLICY = LifecyclePolicy(persistence=Persistence(1, 1), persistence_by_rule={}, clear_hold_ms=300)


def r1(severity=Severity.CAUTION) -> Finding:
    return Finding(rule_id="R1", rule_version=1, severity=severity, world_ids=("w-0012",), zone_id="z-01")


def r7(severity=Severity.CAUTION) -> Finding:
    return Finding(rule_id="R7", rule_version=1, severity=severity, world_ids=(), zone_id="z-04",
                   reasons=("monitoring_degraded",))


class Site:
    def __init__(self, origin=Origin.LIVE) -> None:
        self.clock = FixedClock(T0)
        self.t = T0
        self.origin = origin
        self.tracker = LifecycleTracker(origin=origin, gateway_id="tier0-01", clock=self.clock, policy=POLICY)

    def tick(self, findings=(), evidence=()):
        self.t += 100
        self.clock.set(self.t + 10)
        world = WorldState(t_latest_global=self.t, calibration_version="cal-7", scene_version="scene-3", tracks=())
        evaluation = Evaluation(origin=self.origin, evaluated_at=self.t + 10, observation_time=self.t,
                                calibration_version="cal-7", scene_version="scene-3", rules=(), zones=(),
                                findings=tuple(findings), zone_clear_evidence=frozenset(evidence))
        return self.tracker.apply(evaluation, world, config_version="cfg-12", evidence_ref=MSG)


def request(event_id, target=Severity.CRITICAL, escalation_id=ESC_ID, **extra) -> EscalationRequest:
    escalation = {"escalation_id": escalation_id, "basis": "VLM_PROPOSAL", "assessment_job_id": "job-20261017-0052",
                  "target_severity": str(target), "channel": "CONTROL_ROOM", "actor_role": "ADMIN",
                  "actor_id": "user-0003", "requested_at": T0, **extra}
    return EscalationRequest.from_contract({"event_id": event_id, "escalation": escalation})


def raised(site, finding=None):
    [t] = site.tick([finding or r1()]).transitions
    return t.state


def test_accepted_escalation_is_an_updated_transition_with_the_escalation_as_received():
    site = Site()
    before = raised(site, r1(Severity.HIGH))
    req = request(before.event_id, note="VLM 근거 확인, 난간 없음 맞음")
    step = site.tracker.escalate(req)
    [t] = step.transitions
    assert t.transition is Lifecycle.UPDATED and t.changes == (Change.SEVERITY, Change.ESCALATED)
    assert t.event_sequence == before.event_sequence + 1 and not t.resumed
    assert t.state.severity is Severity.CRITICAL and t.state.escalation_floor is Severity.CRITICAL
    payload = t.to_payload()
    assert payload["escalation"] == dict(req.escalation) and payload["escalation"]["note"] == "VLM 근거 확인, 난간 없음 맞음"
    assert payload["resumed"] is False and payload["evidence_ref"] is None  # 판정이 아니다(저널이 직전 근거를 싣는다)
    assert t.evidence == ()
    # 판정 값(검증 상태·수치·근거·관측 시각)은 그대로다
    for name in ("verification", "metrics", "reasons", "observation_time", "evaluated_at", "zone_id", "world_ids"):
        assert getattr(t.state, name) == getattr(before, name), name
    assert site.tracker.get(before.event_id) == t.state


def test_other_transitions_do_not_carry_an_escalation():
    site = Site()
    state = raised(site)
    assert "escalation" not in site.tracker.get(state.event_id).to_record()  # 바닥값이 없으면 기록 형식 그대로
    [t] = site.tick([r1(Severity.HIGH)]).transitions
    assert "escalation" not in t.to_payload()


def test_escalation_re_alerts_like_a_rule_upgrade_even_after_ack():
    """3.10: 등급이 오르면 그 등급으로 다시 울리고 경보 단계가 새로 열린다. 상향 전 상태에 대한 확인은 새 경보를 멈추지 않는다."""
    site = Site()
    state = raised(site, r1(Severity.HIGH))
    ack = site.tracker.acknowledge(state.event_id, AckActor.SITE_BUTTON, "laptop-01", T0, channel="SITE_BUTTON")
    acked = ack.transitions[0].state
    assert acked.site_acknowledged
    step = site.tracker.escalate(request(state.event_id))
    [signal] = step.local_signals
    [t] = step.transitions
    assert isinstance(signal, LocalAlert) and signal.severity is Severity.CRITICAL
    assert signal.event_sequence == t.event_sequence == t.state.site_alarm_sequence
    assert t.state.local_alerted and not t.state.site_acknowledged


def test_escalation_to_caution_does_not_sound_the_site_alarm():
    site = Site()
    state = raised(site, Finding(rule_id="R1", rule_version=1, severity=Severity.INFO, world_ids=("w-0012",),
                                 zone_id="z-01"))
    step = site.tracker.escalate(request(state.event_id, Severity.CAUTION))
    assert step.transitions[0].state.severity is Severity.CAUTION and step.local_signals == ()


@pytest.mark.parametrize("current,target", [(Severity.HIGH, Severity.HIGH), (Severity.CRITICAL, Severity.HIGH),
                                            (Severity.CRITICAL, Severity.CRITICAL), (Severity.HIGH, Severity.CAUTION)])
def test_not_upward_is_rejected_without_changes(current, target):
    site = Site()
    state = raised(site, r1(current))
    assert site.tracker.escalate(request(state.event_id, target)) is EscalateReject.NOT_UPWARD
    assert site.tracker.get(state.event_id) == state


def test_rule_raised_first_makes_the_approved_escalation_not_upward():
    """그사이 규칙이 먼저 같은 등급으로 올렸으면 NOT_UPWARD(계약 설명)."""
    site = Site()
    state = raised(site, r1(Severity.HIGH))
    site.tick([r1(Severity.CRITICAL)])
    assert site.tracker.escalate(request(state.event_id)) is EscalateReject.NOT_UPWARD


def test_unknown_resolved_and_operator_closed_events_are_not_active():
    site = Site()
    assert site.tracker.escalate(request("0192a6f1-0a00-7c00-8a00-00000000ffff")) is EscalateReject.NOT_ACTIVE
    state = raised(site)
    for _ in range(10):  # 판정 없음 + 관측 확인(트랙 없는 사람 대상은 UNVERIFIABLE): 구역 대상으로 관측 해소를 본다
        site.tick()
    zone = Site()
    [t] = zone.tick([r7()]).transitions
    for _ in range(5):
        zone.tick(evidence={("R7", "z-04")})
    assert zone.tracker.get(t.event_id) is None  # 관측 해소
    assert zone.tracker.escalate(request(t.event_id)) is EscalateReject.NOT_ACTIVE
    other = Site()
    [o] = other.tick([r7()]).transitions
    other.tick()
    closed = other.tracker.close(CloseRequest(event_id=o.event_id, close_id="7a2b3c4d-5e6f-4a1b-9c2d-3e4f5a6b7c8d",
                                              actor_id="user-0003", requested_at=T0, reason="카메라 철거"))
    assert closed.transitions[0].transition is Lifecycle.RESOLVED
    assert other.tracker.escalate(request(o.event_id)) is EscalateReject.NOT_ACTIVE
    assert site.tracker.get(state.event_id) is not None  # 사람 대상은 관측 없이 해소하지 않는다(대조)


def test_escalated_severity_is_a_floor_until_resolved():
    """정책(명세에 없어 정한 기본안): 이후 규칙 판정은 운영자가 올린 등급을 낮추지 않고, 더 높이는 것은 따른다."""
    site = Site()
    state = raised(site, r1(Severity.CAUTION))
    site.tracker.escalate(request(state.event_id, Severity.HIGH))
    step = site.tick([r1(Severity.CAUTION)])
    assert step.transitions == ()  # 등급을 내리는 SEVERITY 전이가 없다
    assert site.tracker.get(state.event_id).severity is Severity.HIGH
    [up] = site.tick([r1(Severity.CRITICAL)]).transitions
    assert Change.SEVERITY in up.changes and up.state.severity is Severity.CRITICAL
    assert up.state.escalation_floor is Severity.HIGH
    [down] = site.tick([r1(Severity.CAUTION)]).transitions  # 규칙이 올린 등급은 바닥값까지만 내려간다
    assert down.state.severity is Severity.HIGH


def test_negative_control_without_floor_the_rule_lowers_severity():
    site = Site()
    state = raised(site, r1(Severity.HIGH))
    [down] = site.tick([r1(Severity.CAUTION)]).transitions
    assert down.state.severity is Severity.CAUTION and site.tracker.get(state.event_id).escalation_floor is None


def test_floor_ends_with_resolution_and_resumed_event_starts_from_the_rule_severity():
    site = Site()
    [t] = site.tick([r7()]).transitions
    site.tracker.escalate(request(t.event_id, Severity.HIGH))
    for _ in range(5):
        site.tick(evidence={("R7", "z-04")})
    assert site.tracker.get(t.event_id) is None
    [resumed] = site.tick([r7()]).transitions
    assert resumed.resumed and resumed.event_id == t.event_id
    assert resumed.state.severity is Severity.CAUTION and resumed.state.escalation_floor is None


def test_commit_failure_changes_nothing():
    site = Site()
    state = raised(site)

    def fail(step):
        raise RuntimeError("저널 대기열 포화")

    with pytest.raises(RuntimeError):
        site.tracker.escalate(request(state.event_id), commit=fail)
    assert site.tracker.get(state.event_id) == state


def test_non_live_tracker_does_not_take_operator_escalation():
    site = Site(Origin.REPLAY)
    state = raised(site)
    with pytest.raises(LifecycleError):
        site.tracker.escalate(request(state.event_id))


def test_floor_survives_state_record_round_trip_and_old_records_still_load():
    site = Site()
    state = raised(site)
    site.tracker.escalate(request(state.event_id, Severity.HIGH))
    escalated = site.tracker.get(state.event_id)
    record = escalated.to_record()
    assert record["escalation_floor"] == "HIGH"
    assert EventState.from_record(json.loads(json.dumps(record))) == escalated
    old = state.to_record()
    assert "escalation_floor" not in old and EventState.from_record(old) == state
    with pytest.raises(RiskInputError):
        EventState.from_record(record | {"escalation_floor": "LOUD"})


def test_request_from_contract_example_and_rejections():
    doc = json.loads((EXAMPLES / "event_escalate.json").read_text(encoding="utf-8"))
    req = EscalationRequest.from_contract(doc["payload"])
    assert req.event_id == doc["payload"]["event_id"] and dict(req.escalation) == doc["payload"]["escalation"]
    assert req.escalation_id == ESC_ID and req.target_severity is Severity.CRITICAL
    for name in ("event_escalate.foreman_pwa.json", "event_escalate.extra_field.json", "event_escalate.target_info.json"):
        bad = json.loads((EXAMPLES / "invalid" / name).read_text(encoding="utf-8"))
        with pytest.raises(RiskInputError):
            EscalationRequest.from_contract(bad["payload"])
    base = doc["payload"]
    for broken in ({**base["escalation"], "basis": "OPERATOR"}, {**base["escalation"], "note": "x" * 501},
                   {k: v for k, v in base["escalation"].items() if k != "actor_id"}):
        with pytest.raises(RiskInputError):
            EscalationRequest.from_contract({"event_id": base["event_id"], "escalation": broken})


def test_escalated_state_replaced_copy_is_independent():
    """받은 escalation 사본은 읽기 전용이라 전이 기록이 나중에 바뀌지 않는다."""
    req = request("0192a6f1-0a00-7c00-8a00-000000000002")
    with pytest.raises(TypeError):
        req.escalation["target_severity"] = "INFO"  # type: ignore[index]
    assert replace(req, event_id="x").escalation is req.escalation
