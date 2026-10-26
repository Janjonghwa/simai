"""외부 LLM 텍스트 생성 클라이언트 (명세 5.4 "LLM(텍스트 생성)", 14장 비밀 값, S15P31A101-225·-285).

제공사에 묶지 않는다: `ChatClient` Protocol 하나로 주입한다. 구현은 둘이다.
- `OpenAIResponsesClient`(기본): OpenAI Responses API(`POST {base_url}/responses`). SSAFY GMS 안내문의 공식 방식이다.
- `OpenAICompatClient`: OpenAI 호환 Chat Completions(`POST {base_url}/chat/completions`). 2026-10-26 GMS 에서 먼저 동작을 확인한 방식.
제공사 SDK·새 HTTP 의존성 없이 표준 라이브러리(urllib)로 보낸다. 둘은 HTTP 오류 → 작업 실패 코드 매핑(`_post`)을 공유한다.

환경 변수(Pod `.env`, 저장소·로그·보고서에 값을 남기지 않는다):
- SIMAI_LLM_BASE_URL: API 주소의 `/v1` 까지(예: SSAFY GMS `https://gms.ssafy.io/gmsapi/api.openai.com/v1`). https 만.
  클라이언트가 `/responses`·`/chat/completions` 를 붙이므로 그 경로까지 넣으면 설정 오류다
- SIMAI_LLM_MODEL: 모델 이름(명세의 "GPT 6.1 sol" 을 제공사 표기로, GMS 는 `gpt-6.1-sol`)
- SIMAI_LLM_API_KEY: API 키(Authorization: Bearer)
- SIMAI_LLM_API_STYLE: `responses`(기본) 또는 `chat`
- SIMAI_LLM_TIMEOUT_S: 요청 하나의 응답 대기 상한(초, 기본 60). 작업 대여 시간 제한(timeout_ms) 안에 두 번 부를 수 있게 잡는다
- SIMAI_LLM_TEMPERATURE: 선택(0~2). 없으면 요청에 temperature 를 싣지 않는다(제공사 기본값). gpt-6.1-sol 은 기본값(1) 밖의 값을
  400 으로 거부한다(SSAFY GMS 프록시 확인, 2026-10-26)
- SIMAI_LLM_MAX_OUTPUT_TOKENS: 선택(Responses 만, 1~100000). 없으면 싣지 않는다(모델 기본 상한). 추론 토큰을 포함한 상한이라
  작게 두면 본문이 잘려 status incomplete 가 된다

오류는 `LlmError(code, retryable)` 로 알린다. 처리기가 그대로 JobFailed 로 옮긴다.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_TIMEOUT_S = 60.0
API_STYLES = ("responses", "chat")
_ENDPOINTS = {"responses": "/responses", "chat": "/chat/completions"}


class LlmError(Exception):
    """LLM 호출 실패. code 는 작업 실패 코드(job_callback error.code), retryable 은 새 대여로 다시 해 볼 만한지."""

    code = "LLM_ERROR"
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LlmConfigError(LlmError):
    """설정 없음·잘못됨(키·주소·모델). 다시 해도 같다."""

    code = "LLM_CONFIG"


class LlmAuthError(LlmError):
    """인증 실패(401·403). 키를 고쳐야 한다."""

    code = "LLM_AUTH"


class LlmRejected(LlmError):
    """요청 거부(그 밖의 4xx). 같은 요청은 다시 해도 같다."""

    code = "LLM_REQUEST_REJECTED"


class LlmTimeout(LlmError):
    code = "LLM_TIMEOUT"
    retryable = True


class LlmUnavailable(LlmError):
    """연결 실패·429·5xx. 일시적일 수 있다."""

    code = "LLM_UNAVAILABLE"
    retryable = True


class LlmBadResponse(LlmError):
    """응답 형식이 기대와 다르다(내용 없음, Responses status failed·incomplete·error 등)."""

    code = "LLM_BAD_RESPONSE"
    retryable = True


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str | None = None  # 제공사가 응답에 적은 모델 이름
    input_tokens: int | None = None
    output_tokens: int | None = None


class ChatClient(Protocol):
    @property
    def model(self) -> str: ...

    def complete(self, messages: Sequence[Mapping[str, str]]) -> ChatResult:
        """텍스트 메시지(role, content)만 받아 응답 텍스트를 돌려준다. 실패는 LlmError."""
        ...


@dataclass(frozen=True)
class LlmSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)  # repr·로그에 키가 나가지 않게
    timeout_s: float = DEFAULT_TIMEOUT_S
    temperature: float | None = None  # None 이면 요청에 싣지 않는다
    api_style: str = "responses"
    max_output_tokens: int | None = None  # Responses 만. None 이면 싣지 않는다

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LlmSettings:
        base_url = env.get("SIMAI_LLM_BASE_URL", "").strip().rstrip("/")
        model = env.get("SIMAI_LLM_MODEL", "").strip()
        api_key = env.get("SIMAI_LLM_API_KEY", "").strip()
        missing = [n for n, v in (("SIMAI_LLM_BASE_URL", base_url), ("SIMAI_LLM_MODEL", model),
                                  ("SIMAI_LLM_API_KEY", api_key)) if not v]
        if missing:
            raise LlmConfigError(f"외부 LLM 설정이 없다: {', '.join(missing)} (Pod .env)")
        if not base_url.startswith("https://"):
            raise LlmConfigError("SIMAI_LLM_BASE_URL 은 https 주소여야 한다")
        for path in _ENDPOINTS.values():
            if base_url.endswith(path):
                raise LlmConfigError(
                    f"SIMAI_LLM_BASE_URL 은 /v1 까지만 넣는다(끝의 {path} 는 클라이언트가 붙인다). "
                    "예: https://gms.ssafy.io/gmsapi/api.openai.com/v1"
                )
        api_style = env.get("SIMAI_LLM_API_STYLE", "").strip() or "responses"
        if api_style not in API_STYLES:
            raise LlmConfigError(f"SIMAI_LLM_API_STYLE 은 {' 또는 '.join(API_STYLES)} 다: {api_style!r}")
        try:
            timeout_s = float(env.get("SIMAI_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        except ValueError as e:
            raise LlmConfigError("SIMAI_LLM_TIMEOUT_S 는 초 단위 숫자다") from e
        if not 1 <= timeout_s <= 600:
            raise LlmConfigError("SIMAI_LLM_TIMEOUT_S 는 1~600 초다")
        temperature = None
        if env.get("SIMAI_LLM_TEMPERATURE", "").strip():
            try:
                temperature = float(env["SIMAI_LLM_TEMPERATURE"])
            except ValueError as e:
                raise LlmConfigError("SIMAI_LLM_TEMPERATURE 는 숫자다") from e
            if not 0 <= temperature <= 2:
                raise LlmConfigError("SIMAI_LLM_TEMPERATURE 는 0~2 다")
        max_output_tokens = None
        if env.get("SIMAI_LLM_MAX_OUTPUT_TOKENS", "").strip():
            try:
                max_output_tokens = int(env["SIMAI_LLM_MAX_OUTPUT_TOKENS"])
            except ValueError as e:
                raise LlmConfigError("SIMAI_LLM_MAX_OUTPUT_TOKENS 는 정수다") from e
            if not 1 <= max_output_tokens <= 100_000:
                raise LlmConfigError("SIMAI_LLM_MAX_OUTPUT_TOKENS 는 1~100000 이다")
        return cls(base_url=base_url, model=model, api_key=api_key, timeout_s=timeout_s, temperature=temperature,
                   api_style=api_style, max_output_tokens=max_output_tokens)


Opener = Callable[..., Any]  # urllib.request.urlopen 꼴(request, timeout=) → 응답(read(), 컨텍스트 관리자)


def make_client(settings: LlmSettings, *, opener: Opener = urllib.request.urlopen) -> ChatClient:
    """설정의 api_style 대로 클라이언트를 만든다."""
    if settings.api_style == "chat":
        return OpenAICompatClient(settings, opener=opener)
    return OpenAIResponsesClient(settings, opener=opener)


def _post(settings: LlmSettings, opener: Opener, path: str, payload: Mapping[str, Any]) -> bytes:
    """JSON POST 하나. HTTP·연결 오류를 작업 실패 코드로 바꾼다(두 방식 공통). 키는 오류 메시지에 넣지 않는다."""
    request = urllib.request.Request(
        f"{settings.base_url}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.api_key}"},
    )
    try:
        with opener(request, timeout=settings.timeout_s) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LlmAuthError(f"LLM 인증 실패(HTTP {e.code})") from None
        if e.code == 429 or e.code >= 500:
            raise LlmUnavailable(f"LLM 일시 오류(HTTP {e.code})") from None
        raise LlmRejected(f"LLM 이 요청을 거부했다(HTTP {e.code}){_error_hint(e)}") from None
    except TimeoutError as e:
        raise LlmTimeout(f"LLM 응답이 {settings.timeout_s:g}초 안에 오지 않았다") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise LlmTimeout(f"LLM 응답이 {settings.timeout_s:g}초 안에 오지 않았다") from e
        raise LlmUnavailable(f"LLM 에 연결하지 못했다: {e.reason}") from e


def _error_hint(e: urllib.error.HTTPError) -> str:
    """4xx 본문의 error.message(있으면, 300자까지). 어떤 필드를 거부했는지 알려 준다(예: 지원하지 않는 temperature)."""
    try:
        doc = json.loads(e.read() or b"{}")
    except (ValueError, OSError):
        return ""
    err = doc.get("error") if isinstance(doc, Mapping) else None
    while isinstance(err, Mapping) and isinstance(err.get("error"), Mapping):  # 중계(GMS)가 한 번 더 감싼 경우
        err = err["error"]
    message = err.get("message") if isinstance(err, Mapping) else None
    return f": {message[:300]}" if isinstance(message, str) and message else ""


class OpenAICompatClient:
    """OpenAI 호환 chat completions. JSON 응답(response_format json_object)을 요청한다. temperature 는 설정이 있을 때만 싣는다."""

    def __init__(self, settings: LlmSettings, *, opener: Opener = urllib.request.urlopen) -> None:
        self._settings = settings
        self._open = opener

    @property
    def model(self) -> str:
        return self._settings.model

    def complete(self, messages: Sequence[Mapping[str, str]]) -> ChatResult:
        s = self._settings
        payload: dict[str, Any] = {
            "model": s.model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "response_format": {"type": "json_object"},
        }
        if s.temperature is not None:
            payload["temperature"] = s.temperature
        return _parse(_post(s, self._open, _ENDPOINTS["chat"], payload), s.model)


def _parse(raw: bytes, requested_model: str) -> ChatResult:
    try:
        doc = json.loads(raw)
        content = doc["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LlmBadResponse("LLM 응답이 chat completions 형식이 아니다") from e
    if not isinstance(content, str) or not content.strip():
        raise LlmBadResponse("LLM 응답 내용이 비었다")
    usage = doc.get("usage") if isinstance(doc.get("usage"), Mapping) else {}
    model = doc.get("model") if isinstance(doc.get("model"), str) and doc.get("model") else requested_model
    return ChatResult(content=content, model=model, input_tokens=_count(usage.get("prompt_tokens")),
                      output_tokens=_count(usage.get("completion_tokens")))


class OpenAIResponsesClient:
    """OpenAI Responses API. 메시지를 role 그대로 input 에 넣고 JSON 출력(text.format json_object)을 요청한다.

    system 메시지를 instructions 로 빼지 않는다: json_object 는 input 메시지에 'json' 이라는 낱말이 있어야 받는데
    instructions 는 그 검사에 들지 않아 400 으로 거부된다(SSAFY GMS 실제 호출, 2026-10-26). input 의 system 메시지는 같은 뜻이다.

    store 는 false 로 보낸다(보고서 초안을 제공사 쪽에 남겨 다시 읽을 일이 없다). temperature·max_output_tokens 는 설정이 있을 때만 싣는다.
    """

    def __init__(self, settings: LlmSettings, *, opener: Opener = urllib.request.urlopen) -> None:
        self._settings = settings
        self._open = opener

    @property
    def model(self) -> str:
        return self._settings.model

    def complete(self, messages: Sequence[Mapping[str, str]]) -> ChatResult:
        s = self._settings
        payload: dict[str, Any] = {
            "model": s.model,
            "input": [{"role": m["role"], "content": m["content"]} for m in messages],
            "text": {"format": {"type": "json_object"}},
            "store": False,
        }
        if s.temperature is not None:
            payload["temperature"] = s.temperature
        if s.max_output_tokens is not None:
            payload["max_output_tokens"] = s.max_output_tokens
        return _parse_responses(_post(s, self._open, _ENDPOINTS["responses"], payload), s.model)


def _parse_responses(raw: bytes, requested_model: str) -> ChatResult:
    """Responses 응답 → 텍스트. output[] 의 message 항목 content 중 output_text 의 text 를 이어 붙인다(reasoning 등은 버린다).

    status 가 completed 가 아니거나 error 가 있으면 LlmBadResponse(형식 오류 계열, 재시도 가능): failed 는 제공사 쪽 실패,
    incomplete 는 출력 상한·내용 필터로 잘린 본문(잘린 JSON 을 쓰지 않는다). 모델이 답을 거부(refusal)하고 본문이 없어도 같다.
    """
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise LlmBadResponse("LLM 응답이 JSON 이 아니다") from e
    if not isinstance(doc, Mapping):
        raise LlmBadResponse("LLM 응답이 Responses 형식이 아니다")
    error = doc.get("error")
    if isinstance(error, Mapping) and error:
        raise LlmBadResponse(f"LLM 응답 오류: {error.get('code')} {str(error.get('message'))[:300]}")
    status = doc.get("status")
    if status is not None and status != "completed":
        reason = doc.get("incomplete_details")
        detail = reason.get("reason") if isinstance(reason, Mapping) else None
        raise LlmBadResponse(f"LLM 응답이 끝나지 않았다(status {status}{', ' + str(detail) if detail else ''})")
    texts: list[str] = []
    refusals: list[str] = []
    output = doc.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue  # reasoning·도구 호출 등
            for part in item.get("content") or ():
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif part.get("type") == "refusal":
                    refusals.append(str(part.get("refusal") or ""))
    elif isinstance(doc.get("output_text"), str):  # output 없이 합친 텍스트만 주는 중계
        texts.append(doc["output_text"])
    content = "".join(texts)
    if not content.strip():
        if refusals:
            raise LlmBadResponse(f"LLM 이 답을 거부했다: {refusals[0][:300]}")
        raise LlmBadResponse("LLM 응답 내용이 비었다")
    usage = doc.get("usage") if isinstance(doc.get("usage"), Mapping) else {}
    model = doc.get("model") if isinstance(doc.get("model"), str) and doc.get("model") else requested_model
    return ChatResult(content=content, model=model, input_tokens=_count(usage.get("input_tokens")),
                      output_tokens=_count(usage.get("output_tokens")))


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
