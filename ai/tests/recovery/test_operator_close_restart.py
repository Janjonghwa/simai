"""운영자 종료가 저널·재시작·재동기화를 거쳐도 다시 열리지 않는지 (명세 3.2 재시작 복구, 6.4, S15P31A101-200).

종료 전이는 저널에 RESOLVED(해소 기록)로 남으므로 체크포인트를 거치든 저널 전체로 복구하든 활성으로 복원되지 않고,
close_id 는 재시작 뒤에도 저널 색인으로 남아 같은 명령을 다시 기록하지 않는다. 판정 근거를 모르는 이벤트(저널이 처음 보는 이벤트)도
종료는 기록한다(근거는 누가·왜이고 world_state 근거를 지어내지 않는다).
"""

import pytest

from simai_ai.eventlog import EventJournal
from simai_ai.eventlog.journal import JournalError, is_judgment_transition
from simai_ai.recovery import RecoverySource
from simai_ai.risk import Evaluation, Finding, FixedClock, LifecycleTracker, Origin, Severity, WorldState
from simai_ai.risk.lifecycle import Change, CloseRequest, Lifecycle

ZONE_ID = "z-04"
CLOSE_ID = "7a2b3c4d-5e6f-4a1b-9c2d-3e4f5a6b7c8d"


def r7() -> Finding:
    return Finding(rule_id="R7", rule_version=1, severity=Severity.CAUTION, world_ids=(), zone_id=ZONE_ID,
                   reasons=("monitoring_degraded",))


def zone_step(site, sd, *, findings=()):
    site.clock.advance(100)
    t_obs = site.clock.now_ms() - 100
    world = WorldState(t_latest_global=t_obs, calibration_version="cal-7", scene_version="scene-3", tracks=())
    evaluation = Evaluation(origin=Origin.LIVE, evaluated_at=site.clock.now_ms(), observation_time=t_obs,
                            calibration_version="cal-7", scene_version="scene-3", rules=(), zones=(),
                            findings=tuple(findings))
    step = site.tracker.apply(evaluation, world, config_version="cfg-12", evidence_ref=sd.WORLD_STATE_MSG_ID)
    site.journal.append_step(step)
    site.checkpointer.update_inputs(config_version="cfg-12", last_capture_time=t_obs)
    return step


def close_request(event_id: str) -> CloseRequest:
    return CloseRequest(event_id=event_id, close_id=CLOSE_ID, actor_id="user-0003", requested_at=1, reason="카메라 철거")


@pytest.mark.parametrize("checkpoint_after_close", [False, True])
def test_closed_zone_event_stays_closed_across_restarts(tmp_path, clock, sd, checkpoint_after_close):
    site = sd.Site(tmp_path, clock)
    (raised,) = zone_step(site, sd, findings=[r7()]).transitions
    site.checkpoint()
    site.close()

    # 재시작 1: 복원(근거 없이 남음) → 관리자 종료
    clock.advance(5000)
    site = sd.Site(tmp_path, clock)
    assert [s.event_id for s in site.tracker.active()] == [raised.event_id]
    zone_step(site, sd)  # 규칙이 빠진 판정: 근거 없음
    step = site.tracker.close(close_request(raised.event_id))
    (closed,) = site.journal.append_step(step)
    assert closed.payload["transition"] == "RESOLVED" and list(closed.payload["changes"]) == ["OPERATOR_CLOSED"]
    assert site.journal.has_close_id(CLOSE_ID)
    if checkpoint_after_close:
        site.checkpoint()
    site.close()

    # 재시작 2: 체크포인트(종료 전 또는 후)와 저널 재적용 어느 쪽이든 다시 열리지 않는다
    clock.advance(5000)
    site = sd.Site(tmp_path, clock)
    assert site.result.source is RecoverySource.CHECKPOINT
    assert site.tracker.active() == () and site.result.restored == ()
    assert site.journal.has_close_id(CLOSE_ID)  # 같은 명령 재전송은 다시 기록하지 않는다(재시작 뒤에도)
    # I1 재동기화: 재전송 구간(acked_offset 이후)에 종료가 있고, 저널을 접은 활성 목록에 그 이벤트가 없다
    site.outbox.pump()
    sent = [m["payload"] for m in site.sent if m["payload"]["event_id"] == raised.event_id]
    assert sent[-1]["transition"] == "RESOLVED" and "OPERATOR_CLOSED" in sent[-1]["changes"]
    assert raised.event_id not in {s.event_id for s in site.result.projection.active()}
    # 같은 구역 위험이 다시 나면 새 이벤트(종료된 이벤트를 재개하지 않음)
    (again,) = zone_step(site, sd, findings=[r7()]).transitions
    assert again.transition is Lifecycle.RAISED and again.event_id != raised.event_id
    site.close()


def test_close_of_event_without_known_evidence_is_journaled(tmp_path):
    """반례(모르는 근거): 저널이 판정 근거를 모르는 이벤트(체크포인트로만 복원)도 종료는 기록한다. 같은 근거 없는 관측 RESOLVED 는
    기존대로 거부한다(음성 대조)."""
    clock = FixedClock(1_760_342_400_000)
    before = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    world = WorldState(t_latest_global=clock.now_ms() - 100, calibration_version="cal-7", scene_version="scene-3",
                       tracks=())
    before.apply(Evaluation(origin=Origin.LIVE, evaluated_at=clock.now_ms(), observation_time=world.t_latest_global,
                            calibration_version="cal-7", scene_version="scene-3", rules=(), zones=(),
                            findings=(r7(),)), world, config_version="cfg-12", evidence_ref="0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f")
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    with EventJournal.open(tmp_path, clock=clock) as journal:
        journal.append_step(tracker.restore(before.active()))
        step = tracker.close(close_request(before.active()[0].event_id))
        payload = step.transitions[0].to_payload()
        assert not is_judgment_transition(payload)
        (entry,) = journal.append_step(step)
        assert entry.payload["evidence_ref"] is None and "evidence_ref" not in entry.meta
        observed = {k: v for k, v in payload.items() if k != "close"} | {"changes": [str(Change.CLEARED)]}
        assert is_judgment_transition(observed)
        with pytest.raises(JournalError):
            journal.append_transition(type("T", (), {"to_payload": lambda self: observed})())
