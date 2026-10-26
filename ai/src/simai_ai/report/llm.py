"""외부 LLM 텍스트 생성 클라이언트 (명세 5.4 "LLM(텍스트 생성)", 14장 비밀 값, S15P31A101-225).

제공사에 묶지 않는다: `ChatClient` Protocol 하나로 주입하고, 기본 구현은 OpenAI 호환 chat completions(`POST {base_url}/chat/completions`)다.
제공사 SDK·새 HTTP 의존성 없이 표준 라이브러리(urllib)로 보낸다. 제공사와 호출 설정은 명세 19.1 미결이라 환경 변수로만 정한다.

환경 변수(Pod `.env`, 저장소·로그·보고서에 값을 남기지 않는다):
- SIMAI_LLM_BASE_URL: OpenAI 호환 API 주소(예: https://<제공사>/v1). https 만
- SIMAI_LLM_MODEL: 모델 이름(명세의 "GPT 6.1 sol" 을 제공사 표기로)
- SIMAI_LLM_API_KEY: API 키(Authorization: Bearer)
- SIMAI_LLM_TIMEOUT_S: 요청 하나의 응답 대기 상한(초, 기본 60). 작업 대여 시간 제한(timeout_ms) 안에 두 번 부를 수 있게 잡는다
- SIMAI_LLM_TEMPERATURE: 선택(0~2). 없으면 요청에 temperature 를 싣지 않는다(제공사 기본값). gpt-6.1-sol 은 기본값(1) 밖의 값을
  400 으로 거부한다(SSAFY GMS 프록시 확인, 2026-10-26)

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
    """응답 형식이 OpenAI 호환 chat completions 가 아니다(내용 없음 등)."""

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

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "LlmSettings":
        base_url = env.get("SIMAI_LLM_BASE_URL", "").strip().rstrip("/")
        model = env.get("SIMAI_LLM_MODEL", "").strip()
        api_key = env.get("SIMAI_LLM_API_KEY", "").strip()
        missing = [n for n, v in (("SIMAI_LLM_BASE_URL", base_url), ("SIMAI_LLM_MODEL", model),
                                  ("SIMAI_LLM_API_KEY", api_key)) if not v]
        if missing:
            raise LlmConfigError(f"외부 LLM 설정이 없다: {', '.join(missing)} (Pod .env)")
        if not base_url.startswith("https://"):
            raise LlmConfigError("SIMAI_LLM_BASE_URL 은 https 주소여야 한다")
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
        return cls(base_url=base_url, model=model, api_key=api_key, timeout_s=timeout_s, temperature=temperature)


Opener = Callable[..., Any]  # urllib.request.urlopen 꼴(request, timeout=) → 응답(read(), 컨텍스트 관리자)


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
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{s.base_url}/chat/completions",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {s.api_key}"},
        )
        try:
            with self._open(request, timeout=s.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise LlmAuthError(f"LLM 인증 실패(HTTP {e.code})") from None
            if e.code == 429 or e.code >= 500:
                raise LlmUnavailable(f"LLM 일시 오류(HTTP {e.code})") from None
            raise LlmRejected(f"LLM 이 요청을 거부했다(HTTP {e.code})") from None
        except (TimeoutError, socket.timeout) as e:
            raise LlmTimeout(f"LLM 응답이 {s.timeout_s:g}초 안에 오지 않았다") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise LlmTimeout(f"LLM 응답이 {s.timeout_s:g}초 안에 오지 않았다") from e
            raise LlmUnavailable(f"LLM 에 연결하지 못했다: {e.reason}") from e
        return _parse(raw, s.model)


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


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
