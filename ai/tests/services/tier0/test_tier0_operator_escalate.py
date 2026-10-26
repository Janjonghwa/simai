"""Tier 0 운영자 상향 처리 (I1 event_escalate → event_escalate_result, 명세 5.4·12.3·3.10, S15P31A101-272).

실제 LifecycleTracker·EventJournal·JournalWriter·JudgmentLoop 로 판단 순서(DUPLICATE → NOT_ACTIVE → NOT_UPWARD → JOURNAL_UNAVAILABLE →
ACCEPTED)와 상향 재경보(local_alert)·클립 전달, 응답 계약을 본다. 운영자 종료 시험(test_tier0_judge 의 close)과 같은 구성이다.
"""

from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402

from services.tier0.clips.trigger import clip_trigger_for  # noqa: E402
from services.tier0.judge import JudgmentLoop  # noqa: E402
from simai_ai.eventlog import EventJournal, JournalWriter  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import (  # noqa: E402
    Evaluation,
    Finding,
    FixedClock,
    LifecyclePolicy,
    LifecycleStep,
    LifecycleTracker,
    Origin,
    Persistence,
    Severity,
    WorldState,
)
from simai_ai.risk.lifecycle import CloseRequest, EscalationRequest  # noqa: E402

T0 = 1_760_342_400_000
MSG = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
POLICY = LifecyclePolicy(persistence=Persistence(1, 1), persistence_by_rule={}, clear_hold_ms=300)


class Clips:
    def __init__(self) -> None:
        self.transitions = []

    def on_transitions(self, transitions):
        self.transitions.extend(transitions)


class Site:
    def __init__(self, tmp: Path, *, max_queue: int | None = None) -> None:
        self.clock = FixedClock(T0)
        self.tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id=ts.GATEWAY, clock=self.clock, policy=POLICY)
        self.journal = EventJournal.open(tmp / "j", clock=self.clock)
        self.writer = JournalWriter(self.journal, **({} if max_queue is None else {"max_queue": max_queue})).start()
        self.i0: list[tuple[str, dict]] = []
        self.clips = Clips()
        self.loop = JudgmentLoop(lock=threading.RLock(), now_ms=self.clock.now_ms, config_sync=None, perception=None,
                                 tracker=self.tracker, journal=self.journal, writer=self.writer,
                                 calibration_state=lambda: "CALIBRATED",
                                 send_i0=lambda kind, payload: self.i0.append((kind, payload)) or True, clips=self.clips)

    def tick(self, findings=()):
        self.clock.advance(100)
        t = self.clock.now_ms() - 10
        world = WorldState(t_latest_global=t, calibration_version="cal-7", scene_version="scene-3", tracks=())
        evaluation = Evaluation(origin=Origin.LIVE, evaluated_at=self.clock.now_ms(), observation_time=t,
                                calibration_version="cal-7", scene_version="scene-3", rules=(), zones=(),
                                findings=tuple(findings))
        step = self.tracker.apply(evaluation, world, config_version="cfg-1", evidence_ref=MSG)
        self.journal.append_step(step)
        return step

    def close(self):
        self.writer.stop(3)
        self.journal.close()


@pytest.fixture
def site(tmp_path):
    s = Site(tmp_path)
    yield s
    s.close()


def r1(severity=Severity.HIGH) -> Finding:
    return Finding(rule_id="R1", rule_version=1, severity=severity, world_ids=("w-0012",), zone_id="z-01")


def r7(zone="z-04") -> Finding:
    return Finding(rule_id="R7", rule_version=1, severity=Severity.CAUTION, world_ids=(), zone_id=zone)


def request(event_id: str, target="CRITICAL", escalation_id=None) -> EscalationRequest:
    return EscalationRequest.from_contract({"event_id": event_id, "escalation": {
        "escalation_id": escalation_id or str(uuid.uuid4()), "basis": "VLM_PROPOSAL", "assessment_job_id": "job-1",
        "target_severity": target, "channel": "CONTROL_ROOM", "actor_role": "ADMIN", "actor_id": "user-0003",
        "requested_at": T0}})


def assert_result_contract(payload: dict) -> None:
    msg = {"schema_version": "1.0.0", "msg_type": "event_escalate_result", "msg_id": str(uuid.uuid4()), "sent_at": T0,
           "source_id": ts.GATEWAY, "payload": payload}
    assert schema_errors(msg, "event_escalate_result") == [], schema_errors(msg, "event_escalate_result")


def test_accepted_escalation_is_journaled_re_alerted_and_handed_to_clips(site) -> None:
    [raised] = site.tick([r1(Severity.HIGH)]).transitions
    site.i0.clear()
    site.clips.transitions.clear()
    req = request(raised.event_id)
    result = site.loop.escalate(req)
    assert_result_contract(result)
    assert result == {"escalation_id": req.escalation_id, "event_id": raised.event_id, "result": "ACCEPTED",
                      "event_sequence": raised.event_sequence + 1, "decided_at": site.clock.now_ms()}
    # 3.10 상향 재경보: 규칙 상향과 같은 local_alert(CRITICAL)가 계약대로 나간다
    [(kind, alert)] = site.i0
    assert kind == "local_alert" and alert["severity"] == "CRITICAL" and alert["event_sequence"] == result["event_sequence"]
    msg = {"schema_version": "2.1.0", "msg_type": kind, "msg_id": str(uuid.uuid4()), "sent_at": T0,
           "source_id": ts.GATEWAY, "payload": alert}
    assert schema_errors(msg, kind) == []
    # 클립: 같은 위험 구간의 등급 변경으로 넘어가 CRITICAL 보존 표시 경로(clip_trigger_for → _escalate·_hold_if_critical)를 탄다
    [t] = site.clips.transitions
    trigger = clip_trigger_for(t)
    assert trigger is not None and trigger.severity is Severity.CRITICAL
    assert trigger.key == clip_trigger_for(raised).key  # 새 클립이 아니라 같은 열쇠의 상향
    site.writer.stop(3)
    [entry] = [e for e in site.journal.iter_from(0) if e.payload.get("escalation")]
    assert entry.payload["event_sequence"] == result["event_sequence"] and entry.meta["evidence_ref"] == MSG
    assert site.loop.stats.escalations == 1


def test_duplicate_escalation_id_is_not_recorded_twice(site) -> None:
    [raised] = site.tick([r1(Severity.CAUTION)]).transitions
    req = request(raised.event_id, "HIGH")
    assert site.loop.escalate(req)["result"] == "ACCEPTED"
    again = site.loop.escalate(req)  # 기록 대기 중이어도
    assert_result_contract(again)
    assert again["result"] == "DUPLICATE" and "event_sequence" not in again
    assert ts.wait_until(lambda: site.writer.backlog() == 0, timeout=3)
    assert site.loop.escalate(req)["result"] == "DUPLICATE"
    # 다른 escalation_id 로 같은 등급을 다시 승인하면 NOT_UPWARD(기록 없음)
    other = site.loop.escalate(request(raised.event_id, "HIGH"))
    assert (other["result"], other["reject_reason"]) == ("REJECTED", "NOT_UPWARD")
    site.writer.stop(3)
    assert len([e for e in site.journal.iter_from(0) if e.payload.get("escalation")]) == 1
    assert site.loop.stats.duplicate_escalations == 2


def test_not_active_for_unknown_resolved_and_operator_closed(site) -> None:
    unknown = site.loop.escalate(request("0192a6f1-0a00-7c00-8a00-00000000ffff"))
    assert_result_contract(unknown)
    assert (unknown["result"], unknown["reject_reason"]) == ("REJECTED", "NOT_ACTIVE")
    [zone] = site.tick([r7()]).transitions
    site.tick()
    closed = site.loop.close(CloseRequest(event_id=zone.event_id, close_id=str(uuid.uuid4()), actor_id="user-0003",
                                          requested_at=T0, reason="카메라 철거"))
    assert closed["result"] == "ACCEPTED"
    after_close = site.loop.escalate(request(zone.event_id))
    assert (after_close["result"], after_close["reject_reason"]) == ("REJECTED", "NOT_ACTIVE")


@pytest.mark.parametrize("target", ["HIGH", "CAUTION"])
def test_not_upward_is_rejected_and_nothing_is_recorded(site, target) -> None:
    [raised] = site.tick([r1(Severity.HIGH)]).transitions
    site.i0.clear()
    result = site.loop.escalate(request(raised.event_id, target))
    assert_result_contract(result)
    assert (result["result"], result["reject_reason"]) == ("REJECTED", "NOT_UPWARD")
    assert site.i0 == [] and site.tracker.get(raised.event_id).event_sequence == raised.event_sequence
    assert site.loop.stats.rejected_escalations == 1


def test_escalation_not_submitted_to_journal_keeps_event_and_can_be_retried(tmp_path) -> None:
    """max_queue=1 로 쓰기 대기열 포화: 제출하지 못한 상향은 REJECTED JOURNAL_UNAVAILABLE 이고 이벤트(등급·바닥값)는 그대로,
    escalation_id 도 남지 않는다. 포화가 풀린 뒤 같은 escalation_id 는 ACCEPTED, 저널의 상향 전이는 정확히 1건."""
    s = Site(tmp_path, max_queue=1)
    started, release = threading.Event(), threading.Event()
    try:
        [raised] = s.tick([r1(Severity.HIGH)]).transitions
        before = s.tracker.active()
        blocker = s.writer._submit(lambda: (started.set(), release.wait(10)))  # 쓰기 스레드를 멈춘다
        assert started.wait(3)
        filler = s.writer.submit_step(LifecycleStep())  # 대기열 한 칸을 채운다
        s.i0.clear()
        req = request(raised.event_id)
        first = s.loop.escalate(req)
        assert_result_contract(first)
        assert (first["result"], first.get("reject_reason")) == ("REJECTED", "JOURNAL_UNAVAILABLE")
        assert s.tracker.active() == before and s.i0 == [] and not s.journal.has_escalation_id(req.escalation_id)
        release.set()
        blocker.result(3)
        filler.result(3)
        second = s.loop.escalate(req)
        assert second["result"] == "ACCEPTED" and s.tracker.get(raised.event_id).severity is Severity.CRITICAL
        assert s.loop.escalate(req)["result"] == "DUPLICATE"
        s.writer.stop(3)
        assert len([e for e in s.journal.iter_from(0) if e.payload.get("escalation")]) == 1
    finally:
        release.set()
        s.close()
