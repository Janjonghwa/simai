"""운영자 상향이 저널·재시작·재동기화를 거쳐도 유지되는지 (명세 3.2 재시작 복구, 5.4, S15P31A101-272).

상향 전이는 저널 meta 에 바닥값을 실은 상태로 남으므로 체크포인트(상향 전·후) 어느 쪽에서 복구해도 등급·바닥값이 돌아오고,
escalation_id 는 재시작 뒤에도 저널 색인으로 남아 같은 명령을 다시 기록하지 않는다.
"""

import pytest

from simai_ai.recovery import RecoverySource
from simai_ai.risk import Evaluation, Finding, Origin, Severity, WorldState
from simai_ai.risk.lifecycle import EscalationRequest

ZONE_ID = "z-04"
ESC_ID = "3c9d1e2f-4a5b-4c6d-9e8f-1a2b3c4d5e6f"


def r7(severity=Severity.CAUTION) -> Finding:
    return Finding(rule_id="R7", rule_version=1, severity=severity, world_ids=(), zone_id=ZONE_ID,
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


def escalation(event_id: str) -> EscalationRequest:
    return EscalationRequest.from_contract({"event_id": event_id, "escalation": {
        "escalation_id": ESC_ID, "basis": "VLM_PROPOSAL", "assessment_job_id": "job-1", "target_severity": "HIGH",
        "channel": "CONTROL_ROOM", "actor_role": "ADMIN", "actor_id": "user-0003", "requested_at": 1}})


@pytest.mark.parametrize("checkpoint_after_escalation", [False, True])
def test_escalated_severity_floor_and_id_survive_restart(tmp_path, clock, sd, checkpoint_after_escalation):
    site = sd.Site(tmp_path, clock)
    (raised,) = zone_step(site, sd, findings=[r7()]).transitions
    site.checkpoint()
    step = site.tracker.escalate(escalation(raised.event_id))
    (entry,) = site.journal.append_step(step)
    assert entry.payload["escalation"]["escalation_id"] == ESC_ID and entry.payload["severity"] == "HIGH"
    assert entry.meta["evidence_ref"] == sd.WORLD_STATE_MSG_ID  # 직전 판정 근거
    if checkpoint_after_escalation:
        site.checkpoint()
    site.close()

    clock.advance(5000)
    site = sd.Site(tmp_path, clock)
    assert site.result.source is RecoverySource.CHECKPOINT
    [state] = site.tracker.active()
    assert state.event_id == raised.event_id
    assert state.severity is Severity.HIGH and state.escalation_floor is Severity.HIGH
    assert site.journal.has_escalation_id(ESC_ID)  # 같은 명령 재전송은 다시 기록하지 않는다(재시작 뒤에도)
    # 재시작 뒤에도 규칙 판정이 운영자 등급을 낮추지 않는다
    zone_step(site, sd, findings=[r7(Severity.CAUTION)])
    assert site.tracker.get(raised.event_id).severity is Severity.HIGH
    # I1 재동기화: 재전송 구간에 상향 전이가 그대로 있다
    site.outbox.pump()
    sent = [m["payload"] for m in site.sent if m["payload"]["event_id"] == raised.event_id]
    escalated = [p for p in sent if "escalation" in p]
    assert len(escalated) == 1 and escalated[0]["escalation"]["escalation_id"] == ESC_ID
    site.close()
