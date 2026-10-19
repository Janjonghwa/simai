"""운영자 종료(event_close, 명세 6.4, S15P31A101-200): 근거가 오지 않아 남은 구역 대상 이벤트를 관제 화면 관리자가 닫는다.

종료는 RESOLVED 전이(변경 사유 OPERATOR_CLOSED, close 기록)로 남고 검증 상태·판정 근거를 지어내지 않는다. 사람 대상·
HIGH·CRITICAL·지금 판정이 있는 이벤트는 거부하고 아무것도 바꾸지 않는다. 종료한 이벤트는 재개하지 않는다.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from simai_ai.risk import (
    AlertClear,
    Evaluation,
    Finding,
    FixedClock,
    LifecycleTracker,
    Origin,
    Severity,
    WorldState,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.lifecycle import (
    Change,
    CloseReject,
    CloseRequest,
    Lifecycle,
    LifecycleError,
    LifecycleStep,
    Verification,
)

T0 = 1760342400100
MSG = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"


def r7(severity=Severity.CAUTION, zone="z-04") -> Finding:
    return Finding(rule_id="R7", rule_version=1, severity=severity, world_ids=(), zone_id=zone,
                   reasons=("monitoring_degraded",))


def r1(world_id="w-a-0001") -> Finding:
    return Finding(rule_id="R1", rule_version=1, severity=Severity.CAUTION, world_ids=(world_id,), zone_id="z-01")


class Site:
    def __init__(self, origin=Origin.LIVE) -> None:
        self.clock = FixedClock(T0)
        self.t = T0
        self.tracker = LifecycleTracker(origin=origin, gateway_id="tier0-01", clock=self.clock)
        self.origin = origin

    def tick(self, findings=(), evidence=()):
        self.t += 100
        self.clock.set(self.t + 10)
        world = WorldState(t_latest_global=self.t, calibration_version="cal-7", scene_version="scene-3", tracks=())
        evaluation = Evaluation(origin=self.origin, evaluated_at=self.t + 10, observation_time=self.t,
                                calibration_version="cal-7", scene_version="scene-3", rules=(), zones=(),
                                findings=tuple(findings), zone_clear_evidence=frozenset(evidence))
        return self.tracker.apply(evaluation, world, config_version="cfg-12", evidence_ref=MSG)


def request(event_id, close_id="7a2b3c4d-5e6f-4a1b-9c2d-3e4f5a6b7c8d", reason="3번 카메라 철거") -> CloseRequest:
    return CloseRequest(event_id=event_id, close_id=close_id, actor_id="user-0003", requested_at=T0, reason=reason)


def restored_r7(**changes):
    """R7 이 나고 재시작으로 복원된 추적기(근거 없는 판정이 계속 와도 남는 이벤트)."""
    before = Site()
    before.tick([r7()])
    [state] = before.tracker.active()
    after = Site()
    after.t = after.clock.now_ms() + 5000
    after.tracker.restore([replace(state, **changes)])
    return after, state.event_id


def test_close_restored_zone_event_records_operator_resolution_without_inventing_evidence():
    site, event_id = restored_r7()
    for _ in range(50):  # 근거 없는 판정 5초: 자동 해소 없음
        assert not [t for t in site.tick().transitions if t.transition is Lifecycle.RESOLVED]
    before = site.tracker.get(event_id)
    step = site.tracker.close(request(event_id))
    [t] = step.transitions
    assert t.transition is Lifecycle.RESOLVED and t.changes == (Change.OPERATOR_CLOSED,)
    assert t.event_sequence == before.event_sequence + 1
    assert t.state.verification is Verification.UNVERIFIABLE  # 관측으로 확인한 것이 아니다(VERIFIED 로 바꾸지 않음)
    payload = t.to_payload()
    assert payload["evidence_ref"] is None and payload["transition"] == "RESOLVED"
    assert payload["close"] == {"close_id": "7a2b3c4d-5e6f-4a1b-9c2d-3e4f5a6b7c8d", "channel": "CONTROL_ROOM",
                                "actor_role": "ADMIN", "actor_id": "user-0003", "requested_at": T0,
                                "recorded_at": site.clock.now_ms(), "reason": "3번 카메라 철거"}
    assert site.tracker.active() == () and site.tracker.get(event_id) is None
    assert step.local_signals == ()  # CAUTION: 현장 경보를 낸 적 없다


def test_other_transitions_do_not_carry_a_close_record():
    site = Site()
    [raised] = site.tick([r7()]).transitions
    assert "close" not in raised.to_payload()  # 기존 기록 형식은 그대로


def test_close_of_alerted_event_clears_site_alert():
    """현장 경보를 낸 뒤 CAUTION 으로 내려간 구역 이벤트를 닫으면 해소 신호(AlertClear)를 낸다(health 목록에서도 빠진다)."""
    site, event_id = restored_r7(local_alerted=True, site_alarm_sequence=1)
    step = site.tracker.close(request(event_id))
    [signal] = step.local_signals
    assert isinstance(signal, AlertClear) and signal.event_id == event_id


@pytest.mark.parametrize("severity", [Severity.HIGH, Severity.CRITICAL])
def test_high_and_critical_zone_events_are_rejected(severity):
    site, event_id = restored_r7(severity=severity)
    before = site.tracker.get(event_id)
    assert site.tracker.close(request(event_id)) is CloseReject.SEVERITY_NOT_ALLOWED
    assert site.tracker.get(event_id) == before  # 아무것도 바꾸지 않는다


def test_person_event_is_rejected():
    site = Site()
    site.tick([r1()])
    site.tick([r1()])  # 지속 조건 3프레임 중 2
    site.tick()  # 판정이 사라짐(조건 없음 검사를 통과하는 상태로 만든다)
    [state] = site.tracker.active()
    assert site.tracker.close(request(state.event_id)) is CloseReject.TARGET_NOT_ZONE
    assert site.tracker.get(state.event_id) == state


def test_unknown_and_already_closed_events_are_not_active():
    site, event_id = restored_r7()
    assert site.tracker.close(request("0192a6f0-7c1e-7b3a-9f2d-000000000999")) is CloseReject.NOT_ACTIVE
    assert isinstance(site.tracker.close(request(event_id)), LifecycleStep)
    # 같은 명령을 다시 받아도(멱등 키는 저널이 본다) 추적기는 두 번 닫지 않는다
    assert site.tracker.close(request(event_id)) is CloseReject.NOT_ACTIVE
    assert site.tracker.close(request(event_id, close_id="other")) is CloseReject.NOT_ACTIVE


def test_auto_resolved_event_is_not_active():
    site = Site()
    [raised] = site.tick([r7()]).transitions
    for _ in range(35):
        site.tick(evidence={("R7", "z-04")})
    assert site.tracker.active() == ()
    assert site.tracker.close(request(raised.event_id)) is CloseReject.NOT_ACTIVE


def test_event_whose_condition_is_judged_now_is_rejected_until_the_finding_stops():
    """반례(경계): 마지막 판정에 R7 판정이 있으면 거부. 판정이 사라진 바로 다음 판정부터 받는다."""
    site = Site()
    [raised] = site.tick([r7()]).transitions
    site.tick([r7()])
    assert site.tracker.close(request(raised.event_id)) is CloseReject.CONDITION_PRESENT
    site.tick()  # 규칙이 구성에서 빠짐: 판정도 근거도 없다
    assert site.tracker.get(raised.event_id).verification is Verification.UNVERIFIABLE
    assert site.tracker.close(request(raised.event_id)).transitions[0].changes == (Change.OPERATOR_CLOSED,)


def test_recurrence_after_close_is_a_new_event_even_within_resume_window():
    """반례(재개 경계): 종료 100ms 뒤 같은 (규칙, 구역)이 다시 나면 새 event_id 의 RAISED 다(재개하지 않음)."""
    site = Site()
    [raised] = site.tick([r7()]).transitions
    site.tick()
    site.tracker.close(request(raised.event_id))
    [again] = site.tick([r7()]).transitions
    assert again.transition is Lifecycle.RAISED and again.event_id != raised.event_id
    assert not again.resumed and again.state.event_sequence == 1


def test_negative_control_auto_resolved_recurrence_still_resumes():
    """음성 대조: 관측 해소(근거 유지) 뒤 재개 시간 안의 재발생은 기존대로 같은 event_id 를 재개한다."""
    site = Site()
    [raised] = site.tick([r7()]).transitions
    for _ in range(35):
        site.tick(evidence={("R7", "z-04")})
    [again] = site.tick([r7()]).transitions
    assert again.event_id == raised.event_id and again.resumed


def test_non_live_tracker_does_not_take_operator_close():
    site = Site(origin=Origin.REPLAY)
    [raised] = site.tick([r7()]).transitions
    site.tick()
    with pytest.raises(LifecycleError):
        site.tracker.close(request(raised.event_id))
    assert site.tracker.get(raised.event_id) is not None


def test_close_request_from_contract_example_and_rejections():
    doc = json.loads((EXAMPLES / "event_close.json").read_text(encoding="utf-8"))
    req = CloseRequest.from_contract(doc["payload"])
    assert req.close_id == doc["payload"]["close"]["close_id"] and req.actor_id == "user-0003"
    bad = json.loads((EXAMPLES / "invalid" / "event_close.foreman_pwa.json").read_text(encoding="utf-8"))
    with pytest.raises(RiskInputError):
        CloseRequest.from_contract(bad["payload"])
    for close in ({"reason": ""}, {"reason": "x" * 501}, {"actor_id": ""}, {"close_id": None}):
        payload = {**doc["payload"], "close": {**doc["payload"]["close"], **close}}
        with pytest.raises(RiskInputError):
            CloseRequest.from_contract(payload)
