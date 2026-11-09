from __future__ import annotations

import copy
import json

import pytest

from simai_ai.report.llm import ChatResult, LlmTimeout
from simai_ai.report.wrap_up import (
    deterministic,
    generate,
    numbers_match,
    unwrap_paragraph,
    validate_request,
)

REQUEST = {
    "work_type": "OPENING_WORK",
    "window_ms": [1000, 2000],
    "facts": {
        "event_count": 3,
        "severity_counts": {"CRITICAL": 2, "HIGH": 1, "CAUTION": 0, "INFO": 0},
        "rule_counts": {"R1": 2, "R2": 1},
        "max_severity": "CRITICAL",
        "operator_ack_count": 1,
        "operator_close_count": 0,
        "outside_work_entry_count": 4,
        "phase_counts": {"early": 1, "middle": 1, "late": 1},
        "first_event_min": 0, "last_event_min": 0,
        "unacknowledged_critical_count": 1,
        "top_zones": [{"name": "개구부 1", "count": 2}, {"name": "개구부 2", "count": 1}],
    },
}


class Client:
    model = "fake-llm"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.messages = []

    def complete(self, messages):
        self.messages.append(copy.deepcopy(messages))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(reply, self.model)


def test_valid_numbers_are_kept_and_only_aggregates_enter_prompt():
    text = deterministic(REQUEST["facts"])
    client = Client([text])
    assert generate(REQUEST, lambda: client) == {"text": text, "fallback": False, "model": "fake-llm"}
    prompt = json.loads(client.messages[0][1]["content"])
    assert set(prompt) == {"work_type", "facts", "required_clauses"}
    assert len(client.messages) == 1


@pytest.mark.parametrize("bad", [
    lambda s: s.replace("CRITICAL 2건", "CRITICAL 1건").replace("HIGH 1건", "HIGH 2건"),
    lambda s: s.replace("규칙 R1 위험 구역 진입 2건", "규칙 R1 위험 구역 진입 3건"),
    lambda s: s + " 사망자 99명.",
    lambda s: s + " 확률 0.3%.",
    lambda s: s + " 사고 한건.",
    lambda s: s.replace("전체 이벤트 3건", "전체 이벤트 -3건"),
    lambda s: s.replace("전체 이벤트 3건", "전체 이벤트 1.3건"),
    lambda s: s.replace("전체 이벤트 3건", "전체 이벤트 1,003건"),
    lambda s: s.replace("작업 외 진입 4건", "작업 외 진입 ４건"),
    lambda s: s.replace("최고 등급 CRITICAL", "최고 등급 HIGH"),
    lambda s: s + " 규칙 R7.",
    lambda s: s + " 최고 등급 HIGH.",
    lambda s: s + " 전체 이벤트 3건.",
])
def test_number_or_label_mismatch_rejected(bad):
    facts = REQUEST["facts"]
    assert not numbers_match(bad(deterministic(facts)), facts)


def test_rewrite_once_then_success():
    good = deterministic(REQUEST["facts"])
    client = Client([good.replace("이벤트 3건", "이벤트 77건"), good])
    result = generate(REQUEST, lambda: client)
    assert result["fallback"] is False
    assert result["text"] == good
    assert len(client.messages) == 2
    assert "77" not in json.dumps(client.messages[1], ensure_ascii=False)


def test_two_invalid_drafts_use_deterministic_fallback():
    client = Client(["전체 이벤트 999건.", "위험 없음."])
    result = generate(REQUEST, lambda: client)
    assert result == {"text": deterministic(REQUEST["facts"]), "fallback": True, "model": "deterministic"}
    assert len(client.messages) == 2


@pytest.mark.parametrize("failure", [LlmTimeout("timeout"), TimeoutError(), OSError()])
def test_timeout_and_unavailable_fallback_without_rewrite(failure):
    client = Client([failure])
    result = generate(REQUEST, lambda: client)
    assert result["fallback"] is True
    assert numbers_match(result["text"], REQUEST["facts"])
    assert len(client.messages) == 1


def test_missing_configuration_falls_back():
    from simai_ai.report.llm import LlmConfigError

    def missing():
        raise LlmConfigError("missing")

    assert generate(REQUEST, missing)["fallback"] is True


def test_zero_events_same_millisecond_is_valid_and_does_not_claim_safety():
    request = copy.deepcopy(REQUEST)
    request["window_ms"] = [1000, 1000]
    request["work_type"] = None
    request["facts"] = {
        "event_count": 0, "severity_counts": dict.fromkeys(("CRITICAL", "HIGH", "CAUTION", "INFO"), 0),
        "rule_counts": {}, "max_severity": None, "operator_ack_count": 0, "operator_close_count": 0,
        "outside_work_entry_count": 0, "phase_counts": {"early": 0, "middle": 0, "late": 0},
        "first_event_min": None, "last_event_min": None, "unacknowledged_critical_count": 0, "top_zones": [],
    }
    assert validate_request(request) == request
    text = deterministic(request["facts"])
    assert numbers_match(text, request["facts"])
    assert "안전" not in text


@pytest.mark.parametrize("edit", [
    lambda r: r.update(signer_name="김철수"),
    lambda r: r.update(work_type="김철수 작업"),
    lambda r: r["facts"].update(coordinates=[0, 0, 0]),
    lambda r: r["facts"]["rule_counts"].update(김철수=0),
    lambda r: r["facts"].update(event_count=True),
    lambda r: r["facts"].update(event_count=-1),
    lambda r: r["facts"].update(event_count=4),
    lambda r: r["facts"].update(max_severity="HIGH"),
    lambda r: r["facts"].update(operator_ack_count=4),
    lambda r: r.update(window_ms=[2000, 1000]),
    lambda r: r.update(work_type=[]),
    lambda r: r.update(window_ms=[True, 1000]),
    lambda r: r["facts"]["top_zones"][0].update(name="구역\n이름"),
    lambda r: r["facts"]["top_zones"][0].update(name="가" * 41),
    lambda r: r["facts"]["top_zones"][0].update(extra=1),
    lambda r: r["facts"].update(unacknowledged_critical_count=3),
    lambda r: r["facts"]["phase_counts"].update(early=2),
    lambda r: r["facts"]["phase_counts"].update(extra=0),
    lambda r: r["facts"].update(first_event_min=1),
    lambda r: r["facts"].update(last_event_min=True),
])
def test_invalid_or_personal_input_is_rejected(edit):
    request = copy.deepcopy(REQUEST)
    edit(request)
    with pytest.raises(ValueError):
        validate_request(request)


@pytest.mark.parametrize("wrap", [
    lambda s: json.dumps({"message": s}, ensure_ascii=False),
    lambda s: json.dumps({"text": s}),
    lambda s: json.dumps(s, ensure_ascii=False),
    lambda s: "```json\n" + json.dumps({"message": s}, ensure_ascii=False) + "\n```",
    lambda s: "```\n" + s + "\n```",
])
def test_wrapped_paragraph_is_rejected_and_rewritten(wrap):
    text = deterministic(REQUEST["facts"])
    client = Client([wrap(text), text])
    assert generate(REQUEST, lambda: client) == {"text": text, "fallback": False, "model": "fake-llm"}
    assert len(client.messages) == 2


@pytest.mark.parametrize("bad", [
    lambda s: json.dumps({"message": s, "note": "x"}, ensure_ascii=False),
    lambda s: json.dumps([s], ensure_ascii=False),
    lambda s: '{"message": "' + s,
    lambda s: s + " {추가}",
])
def test_other_json_is_rejected_then_rewritten(bad):
    text = deterministic(REQUEST["facts"])
    client = Client([bad(text), text])
    assert generate(REQUEST, lambda: client) == {"text": text, "fallback": False, "model": "fake-llm"}
    assert "형식 검사 실패" in client.messages[1][-1]["content"] or "숫자 검사 실패" in client.messages[1][-1]["content"]


def test_wrapped_twice_falls_back_to_deterministic():
    text = deterministic(REQUEST["facts"])
    bad = json.dumps({"message": text, "extra": 1}, ensure_ascii=False)
    client = Client([bad, bad])
    out = generate(REQUEST, lambda: client)
    assert out == {"text": text, "fallback": True, "model": "deterministic"}


def test_unwrap_paragraph_cases():
    assert unwrap_paragraph("  문단.  ") == "문단."
    assert unwrap_paragraph('{"message": "문단."}') is None
    assert unwrap_paragraph('{"a": 1}') is None
    assert unwrap_paragraph("{깨짐") is None
    assert unwrap_paragraph('{"message": ""}') is None
    assert unwrap_paragraph(None) is None


def test_real_case_narrative_and_timing_validation():
    from simai_ai.report.wrap_up import fact_clauses
    request = copy.deepcopy(REQUEST)
    request["window_ms"] = [0, 3600000]
    request["facts"].update(event_count=46, severity_counts={"CRITICAL": 7, "HIGH": 0, "CAUTION": 39, "INFO": 0},
        rule_counts={"R1": 7, "R5": 32, "R7": 7}, operator_ack_count=5,
        phase_counts={"early": 12, "middle": 20, "late": 14}, first_event_min=2, last_event_min=58,
        top_zones=[{"name": "개구부 1", "count": 30}, {"name": "개구부 2", "count": 16}],
        unacknowledged_critical_count=4)
    clauses = fact_clauses(request["facts"])
    paragraph = "작업 중 " + ", ".join(clauses[:5]) + ". 집계 내용은 다음과 같다. " + ", ".join(clauses[5:]) + ". 기록을 확인했다."
    assert generate(request, lambda: Client([paragraph]))["text"] == paragraph
    assert not numbers_match(paragraph.replace("중반 이벤트 20건", "중반 이벤트 12건"), request["facts"])
    assert not numbers_match(paragraph + " 추가 99건.", request["facts"])
    assert not numbers_match(paragraph + " 옥상에서 발생했다.", request["facts"])
    assert not numbers_match(paragraph.replace("개구부 1", "개구부 3"), request["facts"])
    assert not numbers_match(paragraph.replace("미확인 CRITICAL 4건", "미확인 CRITICAL 5건"), request["facts"])
    assert generate(request, lambda: Client([json.dumps({"text": paragraph}), "오류"]))["fallback"]


@pytest.mark.parametrize("rule", ["R8", "R9", "R10", "R11"])
def test_ppe_and_extended_rules_are_accepted_with_description(rule):
    """R9 안전모·R11 조끼(-549) 이벤트가 있어도 요청 전체가 거절되지 않고 설명 절이 붙는다."""
    req = copy.deepcopy(REQUEST)
    req["facts"]["rule_counts"] = {"R1": 2, rule: 1}
    validate_request(req)
    text = deterministic(req["facts"])
    assert f"규칙 {rule} " in text and numbers_match(text, req["facts"])


def test_unknown_rule_is_rejected():
    req = copy.deepcopy(REQUEST)
    req["facts"]["rule_counts"] = {"R1": 2, "R12": 1}
    with pytest.raises(ValueError):
        validate_request(req)
