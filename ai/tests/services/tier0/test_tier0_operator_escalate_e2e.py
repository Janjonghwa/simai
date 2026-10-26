"""운영자 상향 E2E (S15P31A101-272, 명세 5.4·12.3, I1 event_escalate·event_escalate_result·risk_event_transition 2.1.0).

1. I1 링크 수준: 로컬 wss EC2 흉내가 event_escalate 를 보내면 실제 I1 클라이언트·디스패처·outbox 와 실제 판정 루프(복원 R7)를 거쳐
   event_escalate_result(ACCEPTED)와 risk_event_transition(UPDATED + escalation)이 EC2 에 온다. 같은 escalation_id 재전송은 DUPLICATE,
   전이는 하나뿐이다. 모두 계약 통과. (운영자 종료 E2E test_tier0_operator_close_e2e 와 같은 구성)
2. 프로세스 수준: 실제 Tier 0 프로세스가 I1 수신에 event_escalate 를 꽂았는지 본다. 사람 대상(R1, 구역 HIGH)을 CRITICAL 로 올리면
   ACCEPTED 와 escalation 전이, 모르는 이벤트는 NOT_ACTIVE.
"""

from __future__ import annotations

import copy
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402
from test_i1_tls import GATEWAY, TOKEN, Ec2Server, example, health_payload, make_cert, wait_until  # noqa: E402
from test_tier0_e2e import _start, site  # noqa: E402, F401 - site 는 픽스처
from test_tier0_judge_r7_restore import _restored_site, calibration  # noqa: E402, F401 - calibration 은 픽스처

from services.tier0.i1_client import Backoff, Envelope, WebSocketConnector  # noqa: E402
from services.tier0.i1_publisher import build_i1_link  # noqa: E402
from simai_ai.eventlog import ACKED_NAME, AckedOffsetStore, Outbox  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402

ESC_ID = "3c9d1e2f-4a5b-4c6d-9e8f-1a2b3c4d5e6f"


def escalate_message(event_id: str, escalation_id: str = ESC_ID) -> dict:
    msg = copy.deepcopy(example("event_escalate"))
    msg["msg_id"] = str(uuid.uuid4())
    msg["payload"]["event_id"] = event_id
    msg["payload"]["escalation"]["escalation_id"] = escalation_id
    return msg


def of(messages, kind):
    return [m["payload"] for m in messages if m["msg_type"] == kind]


def test_event_escalate_over_wss_returns_result_and_escalation_transition(tmp_path, calibration):  # noqa: F811
    site_, _ = _restored_site(tmp_path, calibration, rules=(("R1", 1),))
    cert, key = make_cert(tmp_path)
    server = Ec2Server(cert, key)
    link = build_i1_link(
        WebSocketConnector(server.url, TOKEN, ca_file=str(cert)), Envelope(GATEWAY, site_.clock),
        health_payload=health_payload,
        make_outbox=lambda transport: Outbox(site_.journal, AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY),
                                             gateway_id=GATEWAY, transport=transport, clock=site_.clock),
        on_event_escalate=site_.loop.escalate,
        client_kwargs={"backoff": Backoff(initial_s=0.05, factor=2.0, max_s=0.2)},
    )
    try:
        site_.run(10)
        [state] = site_.tracker.active()
        link.start()
        assert wait_until(lambda: len(server.connections) == 1, timeout=10)
        server.send(escalate_message(state.event_id))

        def escalated():
            return [p for p in of(server.all_messages(), "risk_event_transition")
                    if p["event_id"] == state.event_id and "escalation" in p]

        assert wait_until(lambda: of(server.all_messages(), "event_escalate_result") and escalated(), timeout=10)
        [result] = of(server.all_messages(), "event_escalate_result")
        assert result["result"] == "ACCEPTED" and result["event_sequence"] == state.event_sequence + 1
        [transition] = escalated()
        assert transition["event_sequence"] == result["event_sequence"] and transition["severity"] == "CRITICAL"
        assert transition["transition"] == "UPDATED" and transition["resumed"] is False
        assert transition["escalation"] == example("event_escalate")["payload"]["escalation"]

        server.send(escalate_message(state.event_id))  # 재연결 재전송 흉내: msg_id 가 달라도 escalation_id 로 한 번만
        server.send(escalate_message("0192a6f0-7c1e-7b3a-9f2d-000000000999", "8b3c4d5e-6f7a-4b2c-8d3e-4f5a6b7c8d9e"))
        assert wait_until(lambda: len(of(server.all_messages(), "event_escalate_result")) == 3, timeout=10)
        answers = [(r["result"], r.get("reject_reason")) for r in of(server.all_messages(), "event_escalate_result")]
        assert answers == [("ACCEPTED", None), ("DUPLICATE", None), ("REJECTED", "NOT_ACTIVE")]
        site_.run(5)
        assert len(escalated()) == 1 and site_.tracker.get(state.event_id).severity.value == "CRITICAL"
        for m in server.all_messages():
            assert schema_errors(m, m["msg_type"]) == [], m
    finally:
        link.close()
        server.stop()
        site_.close()


def test_tier0_process_answers_event_escalate(site) -> None:  # noqa: F811
    ec2, laptop, tmp = site["ec2"], site["laptop"], site["tmp"]
    log_path = tmp / "tier0.log"
    proc = _start(site["cfg"], site["env"], log_path)
    site["procs"].append(proc)
    laptop.start()

    def r1():
        return [p for p in of(ec2.all_messages(), "risk_event_transition")
                if p["rule_id"] == "R1" and p["transition"] == "RAISED"]

    assert ts.wait_until(lambda: bool(r1()), timeout=40), log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
    event_id = r1()[0]["event_id"]
    ec2.send(escalate_message(event_id))
    ec2.send(escalate_message("0192a6f0-7c1e-7b3a-9f2d-000000000999", "8b3c4d5e-6f7a-4b2c-8d3e-4f5a6b7c8d9e"))
    assert ts.wait_until(lambda: len(of(ec2.all_messages(), "event_escalate_result")) == 2, timeout=15), \
        log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
    results = {r["event_id"]: r for r in of(ec2.all_messages(), "event_escalate_result")}
    assert results[event_id]["result"] == "ACCEPTED"
    assert results["0192a6f0-7c1e-7b3a-9f2d-000000000999"]["reject_reason"] == "NOT_ACTIVE"
    assert ts.wait_until(lambda: [p for p in of(ec2.all_messages(), "risk_event_transition")
                                  if p["event_id"] == event_id and "escalation" in p], timeout=15)
    for m in ec2.all_messages():
        if m["msg_type"] in ("event_escalate_result", "risk_event_transition"):
            assert schema_errors(m, m["msg_type"]) == [], m
