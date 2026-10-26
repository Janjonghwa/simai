"""OpenAI Responses API 클라이언트: 요청 형식, 응답 해석·status, 오류 코드, 호출 방식 설정 (S15P31A101-285). 네트워크는 쓰지 않는다."""

from __future__ import annotations

import json

import pytest

from simai_ai.report.llm import (
    LlmAuthError,
    LlmBadResponse,
    LlmConfigError,
    LlmRejected,
    LlmSettings,
    LlmTimeout,
    LlmUnavailable,
    OpenAICompatClient,
    OpenAIResponsesClient,
    make_client,
)
from tests.report.test_llm_client import (
    ENV,
    KEY,
    MESSAGES,
    http_error,
    opener_returning,
)

SETTINGS = LlmSettings.from_env(ENV | {"SIMAI_LLM_TIMEOUT_S": "30"})


def message(*parts, status="completed"):
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": status, "content": list(parts)}


def text(t):
    return {"type": "output_text", "text": t, "annotations": []}


def response(*output, **extra):
    return {"id": "resp_1", "object": "response", "status": "completed", "model": "gpt-6.1-sol-2026-10",
            "output": list(output), "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}} | extra


def complete(body, settings=SETTINGS, seen=None):
    return OpenAIResponsesClient(settings, opener=opener_returning(body, [] if seen is None else seen)).complete(MESSAGES)


def test_request_shape_and_parsed_result():
    seen = []
    result = complete(response(message(text("{\"sections\": []}"))), seen=seen)
    assert (result.content, result.model, result.input_tokens, result.output_tokens) == (
        "{\"sections\": []}", "gpt-6.1-sol-2026-10", 12, 7)
    [(request, timeout)] = seen
    assert request.full_url == "https://llm.example/v1/responses" and request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {KEY}" and timeout == 30
    sent = json.loads(request.data)
    assert set(sent) == {"model", "input", "text", "store"}  # temperature·max_output_tokens 는 설정이 있을 때만
    assert sent["model"] == "gpt-6.1-sol" and sent["store"] is False
    # system 도 input 에 둔다: json_object 는 input 메시지의 'json' 낱말만 본다(instructions 로 빼면 GMS 가 400)
    assert sent["input"] == MESSAGES and sent["text"] == {"format": {"type": "json_object"}}


def test_regeneration_messages_keep_order_and_optional_fields_when_configured():
    seen = []
    settings = LlmSettings.from_env(ENV | {"SIMAI_LLM_TEMPERATURE": "1", "SIMAI_LLM_MAX_OUTPUT_TOKENS": "4000"})
    msgs = MESSAGES + [{"role": "assistant", "content": "초안"}, {"role": "user", "content": "다시"}]
    OpenAIResponsesClient(settings, opener=opener_returning(response(message(text("x"))), seen)).complete(msgs)
    sent = json.loads(seen[0][0].data)
    assert [m["role"] for m in sent["input"]] == ["system", "user", "assistant", "user"]
    assert (sent["temperature"], sent["max_output_tokens"]) == (1.0, 4000)


def test_reasoning_items_are_ignored_and_message_texts_are_joined():
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "생각"}]}
    body = response(reasoning, message(text("{\"a\": "), text("1}")))
    assert complete(body).content == "{\"a\": 1}"


def test_missing_usage_and_model_fall_back_and_top_level_output_text_is_accepted():
    result = complete({"status": "completed", "output_text": "x"})
    assert (result.content, result.model, result.input_tokens) == ("x", "gpt-6.1-sol", None)


@pytest.mark.parametrize(
    "failure,error,retryable",
    [
        (http_error(401), LlmAuthError, False),
        (http_error(403), LlmAuthError, False),
        (http_error(400), LlmRejected, False),
        (http_error(404), LlmRejected, False),
        (http_error(429), LlmUnavailable, True),
        (http_error(502), LlmUnavailable, True),
        (TimeoutError("read"), LlmTimeout, True),
        (b"not json", LlmBadResponse, True),
        ([], LlmBadResponse, True),
        (response(), LlmBadResponse, True),  # output 비어 있음
        (response({"type": "reasoning", "summary": []}), LlmBadResponse, True),  # 추론만 있고 본문 없음
        (response(message(text("  "))), LlmBadResponse, True),
        (response(message({"type": "refusal", "refusal": "할 수 없다"})), LlmBadResponse, True),
        (response(message(text("{\"a\"")), status="incomplete",
                  incomplete_details={"reason": "max_output_tokens"}), LlmBadResponse, True),
        (response(status="failed", error={"code": "server_error", "message": "x"}), LlmBadResponse, True),
        (response(message(text("x")), error={"code": "server_error", "message": "x"}), LlmBadResponse, True),
    ],
)
def test_failures_map_to_same_codes_as_chat(failure, error, retryable):
    with pytest.raises(error) as e:
        complete(failure)
    assert e.value.retryable is retryable and e.value.code.startswith("LLM_")
    assert KEY not in str(e.value)


def test_incomplete_reason_is_in_message():
    with pytest.raises(LlmBadResponse, match="incomplete, max_output_tokens"):
        complete(response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}))


def test_api_style_setting_picks_client():
    assert SETTINGS.api_style == "responses" and isinstance(make_client(SETTINGS), OpenAIResponsesClient)
    assert LlmSettings.from_env(ENV | {"SIMAI_LLM_API_STYLE": " "}).api_style == "responses"
    chat = LlmSettings.from_env(ENV | {"SIMAI_LLM_API_STYLE": "chat"})
    assert isinstance(make_client(chat), OpenAICompatClient)


@pytest.mark.parametrize(
    "override,hint",
    [
        ({"SIMAI_LLM_API_STYLE": "completions"}, "SIMAI_LLM_API_STYLE"),
        ({"SIMAI_LLM_BASE_URL": "https://llm.example/v1/responses"}, "/v1 까지만"),
        ({"SIMAI_LLM_BASE_URL": "https://llm.example/v1/chat/completions/"}, "/v1 까지만"),
        ({"SIMAI_LLM_MAX_OUTPUT_TOKENS": "many"}, "정수"),
        ({"SIMAI_LLM_MAX_OUTPUT_TOKENS": "0"}, "1~100000"),
    ],
)
def test_bad_settings_are_config_errors(override, hint):
    with pytest.raises(LlmConfigError, match=hint) as e:
        LlmSettings.from_env(ENV | override)
    assert e.value.code == "LLM_CONFIG" and not e.value.retryable and KEY not in str(e.value)
