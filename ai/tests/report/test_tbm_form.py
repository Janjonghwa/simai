"""TBM 양식 칸 초안·종료 미팅(S15P31A101-616): 스키마, 재시도·대체, 텍스트만, 신원 미전송, 글자 수, 대체 문장 범위, 종료 미팅."""

from __future__ import annotations

import copy
import json

import pytest

from simai_ai.providers.contract import schema_errors
from simai_ai.report import tbm_form
from simai_ai.report.llm import ChatResult, LlmConfigError, LlmTimeout, LlmUnavailable
from simai_ai.report.tbm import assert_text_only
from simai_ai.report.tbm_form import (
    MAX_PLACE,
    MAX_REVIEW,
    MAX_SHORT,
    build_messages,
    check_output,
    check_request,
    fallback_draft,
    form_problems,
    generate_ending,
    generate_form_draft,
    place,
    task_name,
)
from simai_ai.report.tbm_form_templates import CHECK_ACTIONS, CHECK_CODES, GENERAL_RISKS, TAG_PRIORITY, TAG_THEMES

REQUEST = {
    "request_id": "rpt-41-r3",
    "report_date": "2026-11-12",
    "tasks": [
        {"code": "SLAB_OPENING_COVER", "label": "슬래브 개구부 덮개 설치", "rule_tags": ["R1", "R2", "R5"]},
        {"text": "자재 정리"},
    ],
    "rule_tags": ["R1", "R2", "R5"],
    "zones": [
        {"name": "개구부 1", "zone_kind": "OPENING", "rule_id": "R2"},
        {"name": "3층 슬래브 단부", "zone_kind": "FLOOR_EDGE", "rule_id": None},
    ],
}
NODE = {"node_id": "산업안전보건기준규칙 제43조", "node_kind": "ARTICLE", "layer": "OSH_STANDARD", "source_name": "산업안전보건기준에 관한 규칙",
        "article_ref": "제43조", "title": "개구부 등의 방호 조치", "body": "사업주는 개구부에 안전난간을 설치하여야 한다", "rule_tags": ["R2"],
        "embedded": True, "embedding_model": "m", "embedding_revision": "r"}
ENDING = {
    "request_id": "rpt-41-end",
    "window_ms": [1000, 2000],
    "facts": {
        "event_count": 3, "severity_counts": {"CRITICAL": 2, "HIGH": 1, "CAUTION": 0, "INFO": 0}, "rule_counts": {"R1": 2, "R2": 1},
        "max_severity": "CRITICAL", "operator_ack_count": 1, "operator_close_count": 0, "outside_work_entry_count": 4,
        "unacknowledged_critical_count": 1, "phase_counts": {"early": 1, "middle": 1, "late": 1}, "first_event_min": 0, "last_event_min": 0,
        "top_zones": [{"name": "개구부 1", "count": 2}, {"name": "개구부 2", "count": 1}],
    },
}


def output(**over):
    doc = {
        "risks": [
            {"risk": "개구부 1 덮개 설치 중 떨어질 위험", "measure": "안전난간을 먼저 세우고 안전대를 건다", "rule_tags": ["R2"]},
            {"risk": "위험구역에 들어갈 위험", "measure": "경계를 표시하고 출입을 통제한다", "rule_tags": ["R1", "R5"]},
            {"risk": "자재에 걸려 넘어질 위험", "measure": "통로를 정리한다", "rule_tags": []},
        ],
        "focus": {"risk": "개구부로 떨어질 위험", "measure": "안전난간과 안전대"},
        "checks": [{"code": c, "action": f"{c} 조치 제안"} for c in CHECK_CODES],
        "review_daily": "개구부 1 덮개 설치 전 안전난간을 세운다. 제43조를 따른다.",
        "citations": [NODE["node_id"]],
    }
    doc.update(over)
    return json.dumps(doc, ensure_ascii=False)


class Client:
    model = "fake-llm"

    def __init__(self, replies):
        self.replies, self.sent = iter(replies), []

    def complete(self, messages):
        self.sent.append(copy.deepcopy(messages))
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        return ChatResult(content=value, model="fake-llm-2026", input_tokens=10, output_tokens=5)


class Store:
    def __init__(self, fail=False):
        self.fail = fail

    def corpus_empty(self):
        if self.fail:
            raise OSError("db down")
        return False

    def seeds(self, *args):
        return [[NODE], [], [], []], []

    def expand(self, ids):
        return [], []


def run(replies, req=REQUEST, store=None):
    client = Client(replies)
    return generate_form_draft(req, lambda: client, store), client


def assert_contract(result):
    assert schema_errors(result, "tbm_form_draft_result") == []
    assert form_problems(result["form_draft"]) == []


def test_examples_requests_pass_schema_and_consistency():
    assert schema_errors(REQUEST, "tbm_form_draft_request") == []
    assert check_request(REQUEST) == []


def test_llm_draft_fills_only_form_fields_and_passes_schema():
    out, client = run([output()], store=Store())
    assert_contract(out)
    assert out["fallback"] is False and out["fallback_reason"] is None
    form = out["form_draft"]
    assert form["task_name"] == "슬래브 개구부 덮개 설치, 자재 정리"  # 코드가 정한다
    assert form["place"] == "개구부 1, 3층 슬래브 단부"
    assert form["risk_assessed"] is True
    assert all(c["yes"] is None for c in form["checks"])  # 사람이 확인한다
    assert [c["code"] for c in form["checks"]] == list(CHECK_CODES)
    assert "rule_tags" not in form["risks"][0]
    assert [c["node_id"] for c in out["citations"]] == [NODE["node_id"]]
    assert out["generator"] == {"model": "fake-llm-2026", "prompt_version": tbm_form.PROMPT_VERSION, "attempts": 1,
                                "input_tokens": 10, "output_tokens": 5}
    assert len(client.sent) == 1


def test_retry_once_with_reasons_then_success():
    bad = output(risks=json.loads(output())["risks"][:2])
    out, client = run([bad, output()], store=Store())
    assert_contract(out)
    assert out["fallback"] is False and out["generator"]["attempts"] == 2
    retry = client.sent[1]
    assert len(retry) == 3 and "정확히 3줄" in retry[2]["content"]
    assert bad not in json.dumps(retry, ensure_ascii=False)  # 거절한 초안은 다시 보내지 않는다
    assert [s["step"] for s in out["steps"]] == ["CANDIDATE_SEARCH", "LLM_DRAFT", "CODE_VERIFY", "LLM_RETRY", "CODE_VERIFY"]


def test_two_rejected_drafts_fall_back():
    out, client = run(["not json", output(citations=["지어낸 조문"])], store=Store())
    assert_contract(out)
    assert out["fallback"] is True and out["fallback_reason"] == "VALIDATION_FAILED"
    assert out["citations"] == [] and out["generator"]["attempts"] == 2
    assert out["form_draft"] == fallback_draft(REQUEST)
    assert out["steps"][-1]["step"] == "FALLBACK"


@pytest.mark.parametrize(("error", "reason"), [(LlmTimeout("t"), "LLM_TIMEOUT"), (LlmUnavailable("down"), "LLM_UNAVAILABLE")])
def test_llm_errors_fall_back(error, reason):
    out, _ = run([error])
    assert_contract(out)
    assert out["fallback_reason"] == reason and out["generator"]["attempts"] == 0


def test_not_configured_falls_back_without_calling():
    def factory():
        raise LlmConfigError("no key")

    out = generate_form_draft(REQUEST, factory)
    assert_contract(out)
    assert out["fallback_reason"] == "LLM_NOT_CONFIGURED"
    assert out["generator"]["model"] == "deterministic" and out["generator"]["attempts"] == 0


def test_graph_store_failure_does_not_block_draft_but_forbids_article_numbers():
    out, _ = run([output(citations=[]), output(citations=[], review_daily="안전난간을 세운다.")], store=Store(fail=True))
    assert_contract(out)
    assert out["steps"][0] == {"step": "CANDIDATE_SEARCH", "status": "FAILED", "detail": "법령 그래프를 읽지 못했다"}
    assert out["fallback"] is False and out["generator"]["attempts"] == 2  # 첫 초안의 '제43조' 는 허용 목록이 없어 거절


def test_messages_are_text_only_and_carry_no_identity():
    req = copy.deepcopy(REQUEST)
    req["existing"] = {"task_name": "", "place": "1층", "checks": [{"code": c, "yes": c == "PPE", "action": ""} for c in CHECK_CODES]}
    messages = build_messages(req, [NODE])
    assert_text_only(messages)
    assert [set(m) for m in messages] == [{"role", "content"}] * 2
    facts = json.loads(messages[1]["content"])
    assert set(facts) == {"report_date", "task_name", "place", "tasks", "rule_tags", "zones", "checks", "existing", "clauses"}
    assert facts["existing"] == {"place": "1층", "checks": [{"code": "PPE", "yes": True}] + [{"code": c, "yes": False} for c in CHECK_CODES[1:]]}
    # 계약이 신원 필드를 막는다(처리기가 LLM 전에 400)
    for extra in ({"leader": {"name": "김철수"}}, {"participants": ["이영희"]}):
        assert schema_errors(dict(REQUEST, **extra), "tbm_form_draft_request")
    bad_existing = dict(REQUEST, existing={"signer_name": "김철수"})
    assert schema_errors(bad_existing, "tbm_form_draft_request")


def test_check_request_consistency():
    assert check_request(dict(REQUEST, rule_tags=["R1", "R2"])) == ["rule_tags 는 작업내용 rule_tags 의 합집합이어야 한다"]
    dup = dict(REQUEST, tasks=REQUEST["tasks"] + [{"text": " 자재 정리 "}])
    assert "같은 작업내용이 두 번 있다" in check_request(dup)
    url = copy.deepcopy(REQUEST)
    url["zones"][0]["name"] = "https://example.com/a"
    assert check_request(url) == ["주소(URL·객체 주소·data URI)나 NUL 을 싣지 않는다"]


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"review_daily": "가" * (MAX_REVIEW + 1)}, "2000자를 넘는다"),
        ({"focus": {"risk": "가" * 501, "measure": "x"}}, "500자를 넘는다"),
        ({"checks": [{"code": c, "action": "x", "yes": True} for c in CHECK_CODES]}, "code·action 만"),
        ({"checks": [{"code": c, "action": "x"} for c in reversed(CHECK_CODES)]}, "순서 고정"),
        ({"review_daily": "재해 확률 30% 를 줄인다"}, "확률·퍼센트"),
        ({"review_daily": "제99조를 따른다"}, "없는 조문 번호"),
        ({"risks": [{"risk": "a", "measure": "b", "rule_tags": ["R9"]}] * 3}, "입력 rule_tags 밖"),
        ({"risks": [{"risk": "a", "measure": "b", "rule_tags": []}] * 3}, "하나도 다루지 않았다"),
        ({"risks": [{"risk": "", "measure": "b", "rule_tags": ["R1"]}] * 3}, "비지 않은 글자"),
        ({"extra": 1}, "최상위 키"),
    ],
)
def test_output_checks(over, problem):
    doc = json.loads(output())
    doc.update(over)
    out, problems = check_output(json.dumps(doc, ensure_ascii=False), REQUEST, [NODE])
    assert out is None and any(problem in p for p in problems), problems


def test_output_in_code_fence_is_accepted():
    out, problems = check_output("```json\n" + output() + "\n```", REQUEST, [NODE])
    assert problems == [] and out is not None


def test_task_name_and_place_caps():
    many = dict(REQUEST, tasks=[{"text": f"직접 쓴 아주 긴 작업 이름 {i:02d} " + "가" * 20} for i in range(20)], rule_tags=[])
    name = task_name(many)
    assert len(name) <= MAX_SHORT and name.endswith(" 외 19건")
    zones = dict(REQUEST, zones=[{"name": f"구역 {i:02d} " + "나" * 30, "zone_kind": "OPENING", "rule_id": None} for i in range(30)])
    where = place(zones)
    assert len(where) <= MAX_PLACE and "곳" in where
    assert place(dict(REQUEST, zones=[])) == ""  # 모르면 비운다
    out, _ = run([LlmTimeout("t")], req=dict(many, zones=zones["zones"]))
    assert_contract(out)


@pytest.mark.parametrize("tag", sorted(TAG_THEMES, key=lambda t: int(t[1:])))
def test_fallback_template_for_every_rule_tag(tag):
    req = dict(REQUEST, tasks=[{"code": "FLOOR_FINISHING", "label": "바닥 마감", "rule_tags": [tag]}], rule_tags=[tag])
    form = fallback_draft(req)
    assert form_problems(form) == []
    assert schema_errors({"request_id": "r", "form_draft": form, "fallback": True, "fallback_reason": "LLM_TIMEOUT", "citations": [],
                          "generator": {"model": "deterministic", "prompt_version": "p", "attempts": 0, "input_tokens": None,
                                        "output_tokens": None}}, "tbm_form_draft_result") == []
    assert form["risks"][0]["risk"] == TAG_THEMES[tag].risk.format(where=tbm_form._where(tag, req["zones"]))
    assert form["risk_assessed"] is True
    check = TAG_THEMES[tag].check
    row = next(c for c in form["checks"] if c["code"] == check)
    assert row["action"] == CHECK_ACTIONS[check][0].format(where=tbm_form._where(tag, req["zones"]))


def test_fallback_tables_cover_every_tag_and_check_code():
    implemented = {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R9", "R11"}
    assert set(TAG_THEMES) == implemented == set(TAG_PRIORITY)
    assert "R8" not in TAG_THEMES and "R10" not in TAG_THEMES
    assert set(CHECK_ACTIONS) == set(CHECK_CODES)
    assert {t.check for t in TAG_THEMES.values()} <= set(CHECK_CODES)
    assert TAG_THEMES["R2"].check == "OPENING_GUARD"
    assert TAG_THEMES["R3"].check == TAG_THEMES["R4"].check == "WORK_SURFACE_FALL"
    assert len(GENERAL_RISKS) >= 3
    text_only = dict(REQUEST, tasks=[{"text": "창고 정리"}], rule_tags=[], zones=[])
    form = fallback_draft(text_only)
    assert form_problems(form) == [] and form["risk_assessed"] is None and form["place"] == ""
    assert [r["risk"] for r in form["risks"]] == [r for r, _ in GENERAL_RISKS[:3]]
    system = build_messages(REQUEST, [NODE])[0]["content"]
    assert "R9→R8" in system and "R11→R9" in system and "장비 근접" not in system


def test_every_task_code_tags_reach_known_themes():
    """서버 WorkTasks 의 7개 항목 태그(계약 예시와 같은 표)가 모두 대체 문장을 가진다."""
    tasks = {"SLAB_OPENING_COVER": ["R1", "R2", "R5"], "WORK_PLATFORM": ["R2", "R3", "R4"], "FORMWORK": ["R2", "R3", "R4"],
             "REBAR": ["R1", "R2", "R4"], "MATERIAL_HANDLING": ["R1", "R5"], "EXTERIOR_SCAFFOLD_CHECK": ["R2", "R3", "R4"],
             "FLOOR_FINISHING": ["R1", "R4"]}
    for code, tags in tasks.items():
        req = dict(REQUEST, tasks=[{"code": code, "label": code, "rule_tags": tags}], rule_tags=tags)
        assert schema_errors(req, "tbm_form_draft_request") == [] and check_request(req) == []
        assert form_problems(fallback_draft(req)) == []


# 종료 미팅(B10) ---------------------------------------------------------------------------------------------------


def ending_text():
    return (
        "작업 종료 미팅. 경보: 총 3건(위험 2건, 경고 1건), 최고 등급 위험. "
        "주요 위험: 위험구역 진입 2건, 추락 위험(가장자리) 1건. 많이 발생한 구역: 개구부 1 2건, 개구부 2 1건. "
        "조치: 운영자 확인 1건, 미확인 위험 경보 1건, 작업 외 진입 4건. 시간대: 초반 1건, 중반 1건, 후반 1건. "
        "다음 작업 반영: 개구부 1 점검과 위험구역 진입 예방 대책을 작업 전 TBM에 반영한다."
    )


def test_ending_uses_llm_paragraph_and_passes_schema():
    client = Client([ending_text()])
    out = generate_ending(ENDING, lambda: client)
    assert schema_errors(out, "tbm_form_ending_result") == []
    assert out == {"request_id": "rpt-41-end", "ending": ending_text(), "fallback": False, "fallback_reason": None, "model": "fake-llm-2026"}
    assert "work_type" in client.sent[0][1]["content"] and "김" not in client.sent[0][1]["content"]


def test_ending_over_cap_retries_then_falls_back():
    from simai_ai.report.wrap_up import deterministic

    long = ending_text() + " 기록을 확인했다." * 300
    client = Client([long, long])
    out = generate_ending(ENDING, lambda: client)
    assert schema_errors(out, "tbm_form_ending_result") == []
    assert out["fallback"] is True and out["fallback_reason"] == "VALIDATION_FAILED" and out["ending"] == deterministic(ENDING["facts"])
    assert "길이 초과" in client.sent[1][-1]["content"]


def test_ending_timeout_and_not_configured():
    out = generate_ending(ENDING, lambda: Client([LlmTimeout("t")]))
    assert out["fallback_reason"] == "LLM_TIMEOUT" and out["model"] == "deterministic"

    def factory():
        raise LlmConfigError("no key")

    assert generate_ending(ENDING, factory)["fallback_reason"] == "LLM_NOT_CONFIGURED"


def test_ending_without_alerts_passes_contract_for_llm_and_fallback():
    req = copy.deepcopy(ENDING)
    req["facts"] = {
        "event_count": 0, "severity_counts": dict.fromkeys(("CRITICAL", "HIGH", "CAUTION", "INFO"), 0),
        "rule_counts": {}, "max_severity": None, "operator_ack_count": 0, "operator_close_count": 0,
        "outside_work_entry_count": 0, "unacknowledged_critical_count": 0,
        "phase_counts": {"early": 0, "middle": 0, "late": 0},
        "first_event_min": None, "last_event_min": None, "top_zones": [],
    }
    expected = "작업 종료 미팅. 경보: 없음. 특이사항 없이 작업을 마쳤다."
    for reply, fallback in ((expected, False), (LlmTimeout("t"), True)):
        out = generate_ending(req, lambda: Client([reply]))
        assert schema_errors(out, "tbm_form_ending_result") == []
        assert out["ending"] == expected and out["fallback"] is fallback


def test_ending_rejects_inconsistent_facts():
    bad = copy.deepcopy(ENDING)
    bad["facts"]["event_count"] = 9
    with pytest.raises(ValueError):
        generate_ending(bad, lambda: Client([]))


def test_ending_clip_keeps_whole_sentences():
    text = "가나다. " * 600
    clipped = tbm_form._clip_sentences(text, MAX_REVIEW)
    assert len(clipped) <= MAX_REVIEW and clipped.endswith(".")
