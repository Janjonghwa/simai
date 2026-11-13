"""감시 내림(LifecycleTracker.stand_down, S15P31A101-679): 작업 진행 중이 아닌 tick 에 활성 이벤트를 모두 닫고 대기·재개 후보를 버린다.

작업 시작 = 관제 시작(사용자 결정 2026-11-13). 작업 종료는 구성으로 확정된 사실이라 R9 요구 해제처럼 VERIFIED 로 그 tick 에 닫고
근거는 그 tick 의 world_state 다. 구역 대상(R7) 이벤트도, 관측을 잃은 사람 대상 이벤트도 같다.
"""

from dataclasses import replace

import pytest

from simai_ai.risk import AlertClear, Evaluation, Finding, FixedClock, LifecycleTracker, Origin, Severity, WorldState
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.lifecycle import Change, Lifecycle, LifecyclePolicy, Persistence, Verification

T0 = 1760342400100
MSG = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"


def r7(zone="z-04") -> Finding:
    return Finding(rule_id="R7", rule_version=1, severity=Severity.CAUTION, world_ids=(), zone_id=zone,
                   reasons=("monitoring_degraded",))


def r1(world_id="w-a-0001", severity=Severity.CAUTION) -> Finding:
    return Finding(rule_id="R1", rule_version=1, severity=severity, world_ids=(world_id,), zone_id="z-01")


class Site:
    def __init__(self, policy=None) -> None:
        self.clock = FixedClock(T0)
        self.t = T0
        policy = policy or LifecyclePolicy(persistence=Persistence(1, 1))  # 발생 판정 한 번에 연다(이 시험은 감시 내림만 본다)
        self.tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=self.clock, policy=policy)

    def _world(self) -> WorldState:
        self.t += 100
        self.clock.set(self.t + 10)
        return WorldState(t_latest_global=self.t, calibration_version="cal-7", scene_version="scene-3", tracks=())

    def tick(self, findings=()):
        world = self._world()
        evaluation = Evaluation(origin=Origin.LIVE, evaluated_at=self.t + 10, observation_time=self.t,
                                calibration_version="cal-7", scene_version="scene-3", rules=(), zones=(),
                                findings=tuple(findings))
        return self.tracker.apply(evaluation, world, config_version="cfg-12", evidence_ref=MSG)

    def stand_down(self, config_version="cfg-13"):
        return self.tracker.stand_down(self._world(), config_version=config_version, evidence_ref=MSG)


def test_zone_and_person_events_close_in_the_same_tick_as_verified_with_world_state_evidence():
    site = Site()
    site.tick([r7(), r1()])
    site.tick()  # 판정이 사라져 사람 대상은 관측 없음(UNVERIFIABLE), 구역 대상은 근거 없음
    assert {s.verification for s in site.tracker.active()} == {Verification.UNVERIFIABLE}
    sequences = {s.event_id: s.event_sequence for s in site.tracker.active()}
    step = site.stand_down()
    assert site.tracker.active() == ()
    assert [t.state.rule_id for t in step.transitions] == ["R1", "R7"]
    for t in step.transitions:
        assert t.transition is Lifecycle.RESOLVED and t.state.lifecycle is Lifecycle.RESOLVED
        assert t.changes == (Change.VERIFICATION, Change.VERSION, Change.CLEARED, Change.WORK_ENDED)
        assert t.state.verification is Verification.VERIFIED and t.evidence_ref == MSG
        assert t.state.config_version == "cfg-13" and t.state.event_sequence == sequences[t.event_id] + 1
        assert t.to_payload()["changes"][-1] == "WORK_ENDED"


def test_alerted_event_clears_site_alert():
    site = Site()
    [raised] = site.tick([r1(severity=Severity.HIGH)]).transitions
    assert raised.state.local_alerted
    step = site.stand_down()
    [signal] = step.local_signals
    assert isinstance(signal, AlertClear) and signal.event_id == raised.event_id


def test_no_resume_across_work_and_pending_is_dropped_without_loss_raise():
    site = Site(policy=LifecyclePolicy(persistence=Persistence(3, 3), persistence_by_rule={}))
    site.tick([r1("w-p")])  # 대기(1/3)
    assert site.tracker.pending()
    assert site.stand_down().transitions == ()
    assert site.tracker.pending() == {}
    # 대기 중이던 사람이 보이지 않아도(관측 상실) 다음 판정에서 발생하지 않는다
    assert site.tick().transitions == ()


def test_recurrence_after_stand_down_is_a_new_event():
    site = Site()
    [raised] = site.tick([r7()]).transitions
    site.stand_down()
    [again] = site.tick([r7()]).transitions
    assert again.transition is Lifecycle.RAISED and again.event_id != raised.event_id and not again.resumed


def test_empty_tracker_gives_empty_step_and_inputs_are_required():
    site = Site()
    assert site.stand_down().transitions == ()
    with pytest.raises(RiskInputError):
        site.tracker.stand_down(site._world(), config_version="cfg-1", evidence_ref="")
    with pytest.raises(RiskInputError):
        site.tracker.stand_down(site._world(), config_version="", evidence_ref=MSG)


def test_restored_event_closes_too():
    before = Site()
    before.tick([r7()])
    [state] = before.tracker.active()
    after = Site()
    after.t = after.clock.now_ms() + 5000
    after.tracker.restore([replace(state)])
    [t] = after.stand_down().transitions
    assert t.state.event_id == state.event_id and t.transition is Lifecycle.RESOLVED
