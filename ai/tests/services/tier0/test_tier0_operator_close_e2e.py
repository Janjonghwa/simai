"""운영자 종료 E2E (S15P31A101-200, 명세 6.4, I1 event_close·event_close_result·risk_event_transition 2.0.0).

1. I1 링크 수준: 로컬 wss EC2 흉내(test_i1_tls.Ec2Server)가 event_close 를 보내면, 실제 I1 클라이언트·디스패처·outbox 와 실제
   판정 루프(구성에서 R7 이 빠져 근거 없이 남은 복원 R7, test_tier0_judge_r7_restore)를 거쳐 event_close_result(ACCEPTED)와
   risk_event_transition(RESOLVED + resolution)이 EC2 에 온다. 같은 close_id 재전송은 DUPLICATE, 전이는 하나뿐이다. 모두 계약 통과.
2. 프로세스 수준: 실제 Tier 0 프로세스(`python -m services.tier0`)가 I1 수신에 event_close 를 꽂았는지 본다. 사람 대상(R1) 종료는
   REJECTED(TARGET_NOT_ZONE), 모르는 이벤트는 NOT_ACTIVE 로 답하고 R1 은 그대로 활성이다.
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


def close_message(event_id: str, close_id: str = "7a2b3c4d-5e6f-4a1b-9c2d-3e4f5a6b7c8d") -> dict:
    msg = copy.deepcopy(example("event_close"))
    msg["msg_id"] = str(uuid.uuid4())
    msg["payload"]["event_id"] = event_id
    msg["payload"]["close"]["close_id"] = close_id
    return msg


def of(messages, kind):
    return [m["payload"] for m in messages if m["msg_type"] == kind]


def test_event_close_over_wss_returns_result_and_resolution_transition(tmp_path, calibration):  # noqa: F811
    site_, _ = _restored_site(tmp_path, calibration, rules=(("R1", 1),))
    cert, key = make_cert(tmp_path)
    server = Ec2Server(cert, key)
    link = build_i1_link(
        WebSocketConnector(server.url, TOKEN, ca_file=str(cert)), Envelope(GATEWAY, site_.clock),
        health_payload=health_payload,
        make_outbox=lambda transport: Outbox(site_.journal, AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY),
                                             gateway_id=GATEWAY, transport=transport, clock=site_.clock),
        on_event_close=site_.loop.close,
        client_kwargs={"backoff": Backoff(initial_s=0.05, factor=2.0, max_s=0.2)},
    )
    try:
        site_.run(10)
        [state] = site_.tracker.active()
        link.start()
        assert wait_until(lambda: len(server.connections) == 1, timeout=10)
        server.send(close_message(state.event_id))

        def resolved():
            return [p for p in of(server.all_messages(), "risk_event_transition")
                    if p["event_id"] == state.event_id and p["transition"] == "RESOLVED"]

        assert wait_until(lambda: of(server.all_messages(), "event_close_result") and resolved(), timeout=10)
        [result] = of(server.all_messages(), "event_close_result")
        assert result["result"] == "ACCEPTED" and result["event_sequence"] == state.event_sequence + 1
        [closed] = resolved()
        assert closed["event_sequence"] == result["event_sequence"] and closed["verification"] == "UNVERIFIABLE"
        assert closed["resolution"]["kind"] == "OPERATOR_CLOSED" and closed["resolution"]["actor_id"] == "user-0003"

        server.send(close_message(state.event_id))  # 재연결 재전송 흉내: msg_id 가 달라도 close_id 로 한 번만
        server.send(close_message("0192a6f0-7c1e-7b3a-9f2d-000000000999", "8b3c4d5e-6f7a-4b2c-8d3e-4f5a6b7c8d9e"))
        assert wait_until(lambda: len(of(server.all_messages(), "event_close_result")) == 3, timeout=10)
        answers = [(r["result"], r.get("reject_reason")) for r in of(server.all_messages(), "event_close_result")]
        assert answers == [("ACCEPTED", None), ("DUPLICATE", None), ("REJECTED", "NOT_ACTIVE")]
        site_.run(5)
        assert len(resolved()) == 1 and site_.tracker.active() == ()
        for m in server.all_messages():
            assert schema_errors(m, m["msg_type"]) == [], m
    finally:
        link.close()
        server.stop()
        site_.close()


def test_tier0_process_answers_event_close_and_keeps_person_event(site) -> None:  # noqa: F811
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
    ec2.send(close_message(event_id))
    ec2.send(close_message("0192a6f0-7c1e-7b3a-9f2d-000000000999", "8b3c4d5e-6f7a-4b2c-8d3e-4f5a6b7c8d9e"))
    assert ts.wait_until(lambda: len(of(ec2.all_messages(), "event_close_result")) == 2, timeout=15), \
        log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
    results = {r["event_id"]: r for r in of(ec2.all_messages(), "event_close_result")}
    assert (results[event_id]["result"], results[event_id]["reject_reason"]) == ("REJECTED", "TARGET_NOT_ZONE")
    assert results["0192a6f0-7c1e-7b3a-9f2d-000000000999"]["reject_reason"] == "NOT_ACTIVE"
    assert not [p for p in of(ec2.all_messages(), "risk_event_transition")
                if p["event_id"] == event_id and p["transition"] == "RESOLVED"]
    for m in ec2.all_messages():
        if m["msg_type"] in ("event_close_result", "risk_event_transition"):
            assert schema_errors(m, m["msg_type"]) == [], m
