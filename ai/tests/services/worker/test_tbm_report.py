"""tbm_report 처리기 (S15P31A101-225 완료 조건, 명세 5.4·8.3·12.5).

외부 LLM 은 가짜다(실제 호출 없음). 종단 시험은 실제 HTTP 가짜 EC2(conftest.run_main)로 대여 → 실행 → 결과 제출까지 돌리고,
가짜 EC2 가 모든 메시지를 계약(job_request 3.3.0 의 tbm_report 입력, job_callback 2.3.0 의 tbm_report 결과)으로 검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import services.worker.__main__ as entry
from services.worker.handlers import JobContext, JobFailed
from services.worker.tbm_report import TbmReportHandler, client_from_env
from simai_ai.providers.contract import schema_errors
from simai_ai.report.llm import (
    ChatResult,
    LlmAuthError,
    LlmConfigError,
    LlmSettings,
    LlmTimeout,
    LlmUnavailable,
    OpenAICompatClient,
    OpenAIResponsesClient,
)
from tests.report.test_llm_client import ENV, opener_returning

EXAMPLES = Path(__file__).resolve().parents[4] / "contracts" / "examples"
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/tbm_report.schema.json#/$defs/result"}


def example_input(name: str = "job_request.tbm_report.json") -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["payload"]["input"]


def good_draft(kind="TBM") -> str:
    middle = ({"kind": "RISK_ITEM", "title": "가장자리", "item_index": 2,
               "paragraphs": [{"text": "추락 방지 조치가 필요하다.", "citations": ["산업안전보건기준규칙 제42조#0"]}]}
              if kind == "TBM" else
              {"kind": "EVENT_REVIEW", "title": "이벤트", "paragraphs": [{"text": "해소됐다.", "citations": []}]})
    return json.dumps({"sections": [{"kind": "SUMMARY", "title": "요약", "paragraphs": [{"text": "요약", "citations": []}]},
                                    middle]}, ensure_ascii=False)


class FakeLlm:
    model = "gpt-6.1-sol"

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return ChatResult(content=out, model=None)


def handler(llm) -> TbmReportHandler:
    return TbmReportHandler(clock_ms=lambda: 1760342430000, client_factory=lambda: llm)


def ctx_for(inp: dict) -> JobContext:
    return JobContext("job-0001", "0192a6f0-7c1e-7b3a-9f2d-000000000311", "tbm_report", 1, inp)


def test_e2e_lease_generate_submit_and_result_passes_contract(ec2, run_main):
    job_id = ec2.add_job("tbm_report", example_input(), priority="REPORT", timeout_ms=60_000)
    llm = FakeLlm(good_draft())
    assert run_main({"tbm_report": handler(llm)}) == 0  # FakeEC2 가 job_callback(2.3.0 tbm 결과 포함)을 계약으로 검증했다
    job = ec2.jobs[job_id]
    assert job.state == "SUCCEEDED", job.error
    r = job.result
    assert not schema_errors(r, RESULT_SCHEMA)
    assert r["generator"]["model"] == "gpt-6.1-sol" and r["citations"] == ["산업안전보건기준규칙 제42조#0"]
    assert [i["severity"] for i in r["risk_items"]] == [i["severity"] for i in example_input()["plan"]["items"]]


def test_e2e_with_fake_responses_api_passes_contract_and_citation_check(ec2, run_main):
    job_id = ec2.add_job("tbm_report", example_input(), priority="REPORT", timeout_ms=60_000)
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}
    body = {"status": "completed", "model": "gpt-6.1-sol", "usage": {"input_tokens": 900, "output_tokens": 300},
            "output": [reasoning, {"type": "message", "role": "assistant", "status": "completed",
                                   "content": [{"type": "output_text", "text": good_draft(), "annotations": []}]}]}
    seen = []
    client = OpenAIResponsesClient(LlmSettings.from_env(ENV), opener=opener_returning(body, seen))
    assert run_main({"tbm_report": TbmReportHandler(clock_ms=lambda: 1760342430000, client_factory=lambda: client)}) == 0
    job = ec2.jobs[job_id]
    assert job.state == "SUCCEEDED", job.error
    assert not schema_errors(job.result, RESULT_SCHEMA) and job.result["citations"] == ["산업안전보건기준규칙 제42조#0"]
    assert "api_style" not in json.dumps(job.result) and seen[0][0].full_url.endswith("/v1/responses")


def test_client_from_env_picks_style():
    assert isinstance(client_from_env(ENV), OpenAIResponsesClient)
    assert isinstance(client_from_env(ENV | {"SIMAI_LLM_API_STYLE": "chat"}), OpenAICompatClient)


def test_daily_report(ec2, run_main):
    job_id = ec2.add_job("tbm_report", example_input("job_request.tbm_report_daily.json"), priority="REPORT",
                         timeout_ms=60_000)
    assert run_main({"tbm_report": handler(FakeLlm(good_draft("DAILY")))}) == 0
    assert ec2.jobs[job_id].state == "SUCCEEDED" and ec2.jobs[job_id].result["risk_items"] == []


def test_rejected_drafts_fail_without_retry_and_out_of_input_citation_never_reaches_result():
    bad = json.dumps({"sections": [{"kind": "SUMMARY", "title": "요약",
                                    "paragraphs": [{"text": "요약", "citations": ["산업안전보건기준규칙 제57조"]}]}]},
                     ensure_ascii=False)
    llm = FakeLlm(bad, bad)
    with pytest.raises(JobFailed) as e:
        handler(llm)(ctx_for(example_input()))
    assert (e.value.code, e.value.retryable) == ("REPORT_DRAFT_REJECTED", False) and llm.calls == 2
    assert "제57조" in e.value.message


@pytest.mark.parametrize(
    "error,code,retryable",
    [
        (LlmTimeout("60초"), "LLM_TIMEOUT", True),
        (LlmUnavailable("HTTP 503"), "LLM_UNAVAILABLE", True),
        (LlmAuthError("HTTP 401"), "LLM_AUTH", False),
    ],
)
def test_llm_errors_keep_code_and_retryability(error, code, retryable):
    with pytest.raises(JobFailed) as e:
        handler(FakeLlm(error))(ctx_for(example_input()))
    assert (e.value.code, e.value.retryable) == (code, retryable)


def test_missing_key_fails_before_any_call_and_is_not_retryable(monkeypatch):
    for name in ("SIMAI_LLM_BASE_URL", "SIMAI_LLM_MODEL", "SIMAI_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(JobFailed) as e:
        TbmReportHandler(clock_ms=lambda: 0)(ctx_for(example_input()))
    assert (e.value.code, e.value.retryable) == ("LLM_CONFIG", False)
    with pytest.raises(LlmConfigError):
        client_from_env({})


@pytest.mark.parametrize("why", ["image_uri", "person_name", "old_3_1"])
def test_invalid_input_fails_before_any_llm_call(why):
    inp = example_input("job_request.tbm_report_daily.json")
    if why == "image_uri":
        inp["events"][0]["frame_uri"] = "s3://simai-site/events/cam1.jpg"
    elif why == "person_name":
        inp["events"][0]["worker_name"] = "홍길동"
    else:
        inp = {"report_kind": "DAILY", "rule_results": [], "clauses": [{"clause_id": "KOSHA-R-43", "text": "x"}]}
    llm = FakeLlm(good_draft("DAILY"))
    with pytest.raises(JobFailed) as e:
        handler(llm)(ctx_for(inp))
    assert (e.value.code, e.value.retryable) == ("TBM_INPUT_INVALID", False) and llm.calls == 0


def test_cancelled_job_does_not_call_llm():
    ctx = ctx_for(example_input())
    ctx.cancelled.set()
    llm = FakeLlm(good_draft())
    with pytest.raises(JobFailed) as e:
        handler(llm)(ctx)
    assert e.value.code == "CANCELLED" and llm.calls == 0


def test_build_handlers_always_registers_tbm_report():
    assert isinstance(entry.build_handlers(None)["tbm_report"], TbmReportHandler)
