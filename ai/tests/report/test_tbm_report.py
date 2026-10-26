"""TBM 보고서 초안: 요청 조립(텍스트만)·인용 검사·재생성·등급 고정 (명세 5.4·8.3, S15P31A101-225). 실제 LLM 은 부르지 않는다."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

from simai_ai.providers.contract import schema_errors
from simai_ai.report.llm import ChatResult, LlmUnavailable
from simai_ai.report.tbm import (
    MAX_ATTEMPTS,
    PROMPT_VERSION,
    ReportRejected,
    assert_text_only,
    build_messages,
    check_draft,
    generate_report,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/tbm_report.schema.json#/$defs/result"}
C42_0 = "산업안전보건기준규칙 제42조#0"
C43 = "산업안전보건기준규칙 제43조"
C13 = "산업안전보건기준규칙 제13조"


def example_input(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["payload"]["input"]


@pytest.fixture
def tbm():
    return example_input("job_request.tbm_report.json")


@pytest.fixture
def daily():
    return example_input("job_request.tbm_report_daily.json")


class FakeLlm:
    model = "gpt-6.1-sol"

    def __init__(self, *contents, tokens=(100, 50)) -> None:
        self.contents = list(contents)
        self.calls: list[list[dict]] = []
        self.tokens = tokens

    def complete(self, messages):
        self.calls.append([dict(m) for m in messages])
        content = self.contents.pop(0)
        if isinstance(content, Exception):
            raise content
        return ChatResult(content=content, model="gpt-6.1-sol-2026-10", input_tokens=self.tokens[0],
                          output_tokens=self.tokens[1])


def draft(*sections) -> str:
    return json.dumps({"sections": list(sections)}, ensure_ascii=False)


def section(kind, text="설명", citations=(), title="제목", **extra) -> dict:
    return {"kind": kind, "title": title, "paragraphs": [{"text": text, "citations": list(citations)}], **extra}


GOOD_TBM = draft(
    section("SUMMARY", "위험 항목 4건이 계산되었다."),
    section("RISK_ITEM", "작업 위치 p-01 은 가장자리에서 0.3 m 안쪽이다. 제42조에 따라 추락 방지 조치가 필요하다.",
            [C42_0, C13], item_index=2),
    section("RECOMMENDATION", "개구부에 덮개를 설치한다.", [C43]),
)


def run(inp, *contents):
    llm = FakeLlm(*contents)
    return generate_report(inp, llm, now_ms=lambda: 1760342430000), llm


def test_good_draft_passes_contract_with_citations_inside_input_and_severity_from_input(tbm):
    result, llm = run(tbm, GOOD_TBM)
    assert schema_errors(result, RESULT_SCHEMA) == []
    allowed = {c["clause_id"] for c in tbm["clauses"]}
    assert set(result["citations"]) <= allowed and result["citations"] == [C42_0, C13, C43]
    assert [r["severity"] for r in result["risk_items"]] == [it["severity"] for it in tbm["plan"]["items"]]
    assert result["generator"] == {"model": "gpt-6.1-sol-2026-10", "prompt_version": PROMPT_VERSION, "attempts": 1,
                                   "input_tokens": 100, "output_tokens": 50}
    assert len(llm.calls) == 1 and result["report_id"] == tbm["report_id"]


def test_daily_report_has_no_risk_items_and_uses_event_sections(daily):
    good = draft(section("SUMMARY", "이벤트 2건."), section("EVENT_REVIEW", "z-04 이벤트는 해소됐다.", [C42_0]),
                 section("RECOMMENDATION", "개구부 점검.", [C43]))
    result, _ = run(daily, good)
    assert schema_errors(result, RESULT_SCHEMA) == [] and result["risk_items"] == []
    bad, problems = check_draft(draft(section("RISK_ITEM", "x", item_index=0)), daily)
    assert bad is None and any("DAILY" in p for p in problems)


def test_citation_outside_input_is_rejected_then_regenerated_once(tbm):
    outside = draft(section("SUMMARY", "요약", ["산업안전보건기준규칙 제57조"]))
    result, llm = run(tbm, outside, GOOD_TBM)
    assert result["generator"]["attempts"] == 2 and result["generator"]["input_tokens"] == 200
    retry = llm.calls[1][-1]
    assert retry["role"] == "user" and "제57조" in retry["content"]  # 버린 이유를 알려 다시 쓰게 한다
    assert llm.calls[1][:2] == llm.calls[0]


def test_two_rejected_drafts_fail_the_report(tbm):
    outside = draft(section("SUMMARY", "요약", ["산업안전보건기준규칙 제57조"]))
    with pytest.raises(ReportRejected) as e:
        run(tbm, outside, outside)
    assert "제57조" in str(e.value) and MAX_ATTEMPTS == 2


@pytest.mark.parametrize(
    "citation,ok",
    [
        (C42_0, True),  # 입력 청크 그대로
        ("산업안전보건기준규칙 제42조", True),  # 조 단위: 그 조의 청크가 입력에 있다
        ("산업안전보건기준규칙 제42조#1", False),  # 입력에 없는 청크
        (C43, True),  # 입력 조 단위 그대로
        ("산업안전보건기준규칙 제43조#0", False),  # 입력은 조 단위: 청크 번호를 지어내지 않는다
        ("산업안전보건기준규칙 제57조", False),
        ("KOSHA-R-43", False),
    ],
)
def test_chunk_and_article_matching(tbm, citation, ok):
    d, problems = check_draft(draft(section("SUMMARY", "요약", [citation])), tbm)
    assert (d is not None) is ok, problems


@pytest.mark.parametrize("text", ["제57조에 따른다", "산업안전보건기준규칙 제 9 조", "제44조의2 를 본다"])
def test_article_written_in_text_must_be_in_input(tbm, text):
    d, problems = check_draft(draft(section("SUMMARY", text)), tbm)
    assert d is None and any("본문" in p for p in problems)


def test_text_mention_of_input_article_is_allowed(tbm):
    d, problems = check_draft(draft(section("SUMMARY", "제43조와 제 42 조를 따른다")), tbm)
    assert d is not None, problems


def test_llm_cannot_change_severity_or_add_fields(tbm):
    with_severity = draft(section("RISK_ITEM", "등급은 낮다", item_index=0, severity="INFO"))
    d, problems = check_draft(with_severity, tbm)
    assert d is None and any("severity" in p for p in problems)
    result, _ = run(tbm, with_severity, GOOD_TBM)
    assert result["risk_items"][0]["severity"] == tbm["plan"]["items"][0]["severity"] == "HIGH"


@pytest.mark.parametrize(
    "content,needle",
    [
        ("not json", "JSON"),
        (json.dumps({"sections": [], "x": 1}), "sections"),
        (draft(section("SUMMARY", "추락 확률 85%")), "퍼센트"),
        (draft(section("RISK_ITEM", "x", item_index=9)), "item_index"),
        (draft(section("SUMMARY", "x", item_index=0)), "item_index"),
        (draft(section("SUMMARY", "x", [C43, C43])), "두 번"),
        (draft(section("BOGUS", "x")), "섹션 종류"),
    ],
)
def test_malformed_drafts_are_rejected(tbm, content, needle):
    d, problems = check_draft(content, tbm)
    assert d is None and any(needle in p for p in problems), problems


def test_length_limit(tbm):
    inp = copy.deepcopy(tbm) | {"options": {"max_chars": 500}}
    d, problems = check_draft(draft(section("SUMMARY", "가" * 501)), inp)
    assert d is None and any("상한" in p for p in problems)


def test_request_is_text_only_without_addresses_or_people(daily):
    messages = build_messages(daily)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert all(set(m) == {"role", "content"} and isinstance(m["content"], str) for m in messages)
    facts = json.loads(messages[1]["content"])
    assert set(facts) == {"report_kind", "report_date", "clauses", "events"}
    assert all("event_id" not in e for e in facts["events"])  # 이벤트 ID 도 보내지 않는다(필요 없음)
    text = json.dumps(messages, ensure_ascii=False)
    assert "s3://" not in text and "http" not in text and daily["site_id"] not in text


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}],
        [{"role": "user", "content": "프레임 s3://simai-site/events/cam1.jpg 참고"}],
        [{"role": "user", "content": "https://example.com/a.png"}],
        [{"role": "user", "content": b"bytes"}],
        [{"role": "user", "content": "x", "name": "홍길동"}],
    ],
)
def test_pre_send_check_rejects_non_text(messages):
    with pytest.raises(ValueError):
        assert_text_only(messages)


def test_clause_text_with_address_is_never_sent(tbm):
    inp = copy.deepcopy(tbm)
    inp["clauses"][0]["text"] += " 자세한 내용 https://example.com"
    llm = FakeLlm(GOOD_TBM)
    with pytest.raises(ValueError):
        generate_report(inp, llm, now_ms=lambda: 0)
    assert llm.calls == []


def test_llm_error_propagates_and_cancel_stops_before_next_call(tbm):
    with pytest.raises(LlmUnavailable):
        run(tbm, LlmUnavailable("503"))
    cancelled = threading.Event()
    llm = FakeLlm(draft(section("SUMMARY", "요약", ["산업안전보건기준규칙 제57조"])), GOOD_TBM)

    def progress(_):
        cancelled.set()

    with pytest.raises(InterruptedError):
        generate_report(tbm, llm, now_ms=lambda: 0, cancelled=cancelled, progress=progress)
    assert len(llm.calls) == 1
