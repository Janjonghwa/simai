"""OpenAI 호환 LLM 클라이언트: 요청 형식, 오류 → 코드·재시도 여부, 설정·키 숨김 (S15P31A101-225). 네트워크는 쓰지 않는다."""

from __future__ import annotations

import io
import json
import socket
import urllib.error

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
)

KEY = "sk-test-not-a-real-key"
ENV = {"SIMAI_LLM_BASE_URL": "https://llm.example/v1", "SIMAI_LLM_MODEL": "gpt-6.1-sol", "SIMAI_LLM_API_KEY": KEY}
SETTINGS = LlmSettings(base_url="https://llm.example/v1", model="gpt-6.1-sol", api_key=KEY, timeout_s=30)
MESSAGES = [{"role": "system", "content": "규칙"}, {"role": "user", "content": "{}"}]


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(body, seen):
    def open_(request, timeout):
        seen.append((request, timeout))
        if isinstance(body, BaseException):
            raise body
        return Response(json.dumps(body).encode() if not isinstance(body, bytes) else body)

    return open_


def http_error(code):
    return urllib.error.HTTPError("https://llm.example/v1/chat/completions", code, "x", {}, None)


def test_request_shape_and_parsed_result():
    seen = []
    body = {"model": "gpt-6.1-sol-2026-10", "choices": [{"message": {"content": "{\"sections\": []}"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    result = OpenAICompatClient(SETTINGS, opener=opener_returning(body, seen)).complete(MESSAGES)
    assert (result.content, result.model, result.input_tokens, result.output_tokens) == (
        "{\"sections\": []}", "gpt-6.1-sol-2026-10", 12, 7)
    [(request, timeout)] = seen
    assert request.full_url == "https://llm.example/v1/chat/completions" and request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {KEY}" and timeout == 30
    sent = json.loads(request.data)
    assert set(sent) == {"model", "messages", "response_format"}  # temperature 는 설정이 없으면 싣지 않는다(제공사 기본값)
    assert sent["messages"] == MESSAGES and sent["response_format"] == {"type": "json_object"}


def test_temperature_is_sent_only_when_configured():
    seen = []
    body = {"choices": [{"message": {"content": "x"}}]}
    settings = LlmSettings.from_env(ENV | {"SIMAI_LLM_TEMPERATURE": "1"})
    OpenAICompatClient(settings, opener=opener_returning(body, seen)).complete(MESSAGES)
    assert json.loads(seen[0][0].data)["temperature"] == 1.0
    assert LlmSettings.from_env(ENV | {"SIMAI_LLM_TEMPERATURE": " "}).temperature is None


def test_missing_usage_and_model_fall_back():
    body = {"choices": [{"message": {"content": "x"}}]}
    result = OpenAICompatClient(SETTINGS, opener=opener_returning(body, [])).complete(MESSAGES)
    assert result.model == "gpt-6.1-sol" and result.input_tokens is None


@pytest.mark.parametrize(
    "failure,error,retryable",
    [
        (http_error(401), LlmAuthError, False),
        (http_error(403), LlmAuthError, False),
        (http_error(400), LlmRejected, False),
        (http_error(429), LlmUnavailable, True),
        (http_error(500), LlmUnavailable, True),
        (http_error(503), LlmUnavailable, True),
        (TimeoutError("read"), LlmTimeout, True),
        (socket.timeout("read"), LlmTimeout, True),
        (urllib.error.URLError(TimeoutError("connect")), LlmTimeout, True),
        (urllib.error.URLError("refused"), LlmUnavailable, True),
        (b"not json", LlmBadResponse, True),
        ({"choices": []}, LlmBadResponse, True),
        ({"choices": [{"message": {"content": "  "}}]}, LlmBadResponse, True),
    ],
)
def test_failures_map_to_codes_and_retryability(failure, error, retryable):
    with pytest.raises(error) as e:
        OpenAICompatClient(SETTINGS, opener=opener_returning(failure, [])).complete(MESSAGES)
    assert e.value.retryable is retryable and e.value.code.startswith("LLM_")
    assert KEY not in str(e.value)


def test_settings_from_env_and_key_never_in_repr():
    env = {"SIMAI_LLM_BASE_URL": "https://llm.example/v1/", "SIMAI_LLM_MODEL": "gpt-6.1-sol", "SIMAI_LLM_API_KEY": KEY}
    s = LlmSettings.from_env(env)
    assert (s.base_url, s.timeout_s) == ("https://llm.example/v1", 60.0) and KEY not in repr(s)
    for broken in ({}, env | {"SIMAI_LLM_API_KEY": " "}, env | {"SIMAI_LLM_BASE_URL": "http://llm.example/v1"},
                   env | {"SIMAI_LLM_TIMEOUT_S": "abc"}, env | {"SIMAI_LLM_TIMEOUT_S": "0"},
                   env | {"SIMAI_LLM_TEMPERATURE": "hot"}, env | {"SIMAI_LLM_TEMPERATURE": "3"}):
        with pytest.raises(LlmConfigError) as e:
            LlmSettings.from_env(broken)
        assert e.value.code == "LLM_CONFIG" and not e.value.retryable and KEY not in str(e.value)
