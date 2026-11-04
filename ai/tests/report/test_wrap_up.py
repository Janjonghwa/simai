from __future__ import annotations

import copy
import json

import pytest

from simai_ai.report.llm import ChatResult, LlmTimeout
from simai_ai.report.wrap_up import (
    deterministic,
    generate,
    numbers_match,
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
    lambda s: s.replace("규칙 R1 2건", "규칙 R1 3건"),
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
        "outside_work_entry_count": 0,
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
])
def test_invalid_or_personal_input_is_rejected(edit):
    request = copy.deepcopy(REQUEST)
    edit(request)
    with pytest.raises(ValueError):
        validate_request(request)
