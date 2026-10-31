"""같은 Pod 의 vLLM(Cosmos3-Nano Reasoner) 클라이언트 (명세 3.2·5.4·16장, S15P31A101-22).

이미지는 `VlmFrame`(얼굴 처리 관문 `prepare_vlm_frames` 의 산출물)만 받는다. 배열·바이트·URL·경로를 받는 공개 경로는 없다.
텍스트(질문·구조화 상태)는 문자열이다. 요청은 OpenAI 호환 `POST http://127.0.0.1:{port}/v1/chat/completions` 하나이고,
모양은 4차 세션 측정 도구 `deploy/scripts/vlm_int.py` 와 같다(user 메시지 하나에 text 파트 → image_url 파트, 이미지는 가로 1280 px
JPEG 품질 90 data URL). 표준 라이브러리(urllib)로 보낸다. 설계 `docs/design/vlm-serving.md`.

주소는 루프백 고정이다(호스트를 설정으로 받지 않는다): vLLM 은 감시 프로세스가 `--host 127.0.0.1` 로 띄우고, 얼굴을 처리한
프레임이라도 이미지는 Pod 밖으로 내보내지 않는다(외부 LLM 에는 텍스트만, AGENTS 8). 그래서 http 를 쓴다.

환경 변수(모두 선택):
- SIMAI_VLLM_PORT: 서버 포트(기본 8000, 감시 프로세스와 같은 변수)
- SIMAI_VLLM_MODEL: 모델 이름(기본 cosmos3-nano, 감시 프로세스 `--served-model-name`)
- SIMAI_VLM_TIMEOUT_S: 요청 하나의 응답 대기 상한(초, 기본 30, 0 초과 600 이하)
- SIMAI_VLM_MAX_TOKENS: 답 최대 토큰(기본 256, vlm_int·3차 세션과 같음, 1~4096)
- SIMAI_VLM_REPETITION_PENALTY: vLLM 확장 인자(기본 1.05, 0 초과 2 이하. 1 이면 끔)
- SIMAI_VLM_FREQUENCY_PENALTY: 있을 때만 싣는다(-2~2)
- SIMAI_VLM_TEMPERATURE: 있을 때만 싣는다(0~2). 없으면 모델 generation_config 기본값

오류는 `VlmError(code, retryable)` 로 알린다. 처리기(S15P31A101-25)가 그대로 JobFailed 로 옮긴다.
"""

from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .face_blur import VlmFrame

DEFAULT_PORT = 8000
DEFAULT_MODEL = "cosmos3-nano"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 256
DEFAULT_REPETITION_PENALTY = 1.05
MAX_WIDTH = 1280  # 3차 세션 G7 측정 그림 폭(vlm_int --width 기본)
JPEG_QUALITY = 90  # vlm_int 와 같음
RESPONSE_FORMAT_TYPES = ("json_schema", "json_object")


class VlmError(Exception):
    """VLM 호출 실패. code 는 작업 실패 코드(job_callback error.code), retryable 은 새 대여로 다시 해 볼 만한지."""

    code = "VLM_ERROR"
    retryable = False


class VlmConfigError(VlmError):
    code = "VLM_CONFIG"


class VlmInputError(VlmError):
    """VlmFrame 아닌 이미지, data URL 아닌 이미지 파트, 잘못된 response_format. 다시 해도 같다."""

    code = "VLM_INPUT"


class VlmRejected(VlmError):
    """4xx(429 제외). 같은 요청은 다시 해도 같다(예: 문맥 길이 초과)."""

    code = "VLM_REQUEST_REJECTED"


class VlmTimeout(VlmError):
    code = "VLM_TIMEOUT"
    retryable = True


class VlmUnavailable(VlmError):
    """연결 실패·429·5xx(기동 중·재기동 중 포함)."""

    code = "VLM_UNAVAILABLE"
    retryable = True


class VlmBadResponse(VlmError):
    """응답이 chat completions 형식이 아니거나 내용이 비었다."""

    code = "VLM_BAD_RESPONSE"
    retryable = True


@dataclass(frozen=True)
class VlmSettings:
    port: int = DEFAULT_PORT
    model: str = DEFAULT_MODEL
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    repetition_penalty: float = DEFAULT_REPETITION_PENALTY
    frequency_penalty: float | None = None  # None 이면 싣지 않는다
    temperature: float | None = None  # None 이면 싣지 않는다

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/chat/completions"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> VlmSettings:
        def num(name: str, cast: Callable[[str], Any], lo: float, hi: float, default: Any, lo_open: bool = False) -> Any:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                value = cast(raw)
            except ValueError as e:
                raise VlmConfigError(f"{name} 는 숫자다: {raw!r}") from e
            if not ((lo < value) if lo_open else (lo <= value)) or value > hi:
                raise VlmConfigError(f"{name} 는 {lo}{' 초과' if lo_open else ''}~{hi} 다: {raw!r}")
            return value

        model = env.get("SIMAI_VLLM_MODEL", "").strip() or DEFAULT_MODEL
        return cls(
            port=num("SIMAI_VLLM_PORT", int, 1, 65535, DEFAULT_PORT),
            model=model,
            timeout_s=num("SIMAI_VLM_TIMEOUT_S", float, 0, 600, DEFAULT_TIMEOUT_S, lo_open=True),
            max_tokens=num("SIMAI_VLM_MAX_TOKENS", int, 1, 4096, DEFAULT_MAX_TOKENS),
            repetition_penalty=num("SIMAI_VLM_REPETITION_PENALTY", float, 0, 2, DEFAULT_REPETITION_PENALTY, lo_open=True),
            frequency_penalty=num("SIMAI_VLM_FREQUENCY_PENALTY", float, -2, 2, None),
            temperature=num("SIMAI_VLM_TEMPERATURE", float, 0, 2, None),
        )


@dataclass(frozen=True)
class VlmResult:
    content: str
    model: str
    latency_ms: int  # 요청 보냄 → 응답 본문 다 받음
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None  # length 면 max_tokens 에서 잘렸다(JSON 이면 깨졌을 수 있다)


Encoder = Callable[[np.ndarray], tuple[str, bytes]]  # BGR uint8 → (MIME, 바이트)


def encode_jpeg(pixels: np.ndarray) -> tuple[str, bytes]:
    """가로 MAX_WIDTH 넘으면 줄이고 JPEG 품질 90(vlm_int 와 같음). 워커 이미지의 OpenCV(headless)."""
    import cv2

    h, w = pixels.shape[:2]
    if w > MAX_WIDTH:
        pixels = cv2.resize(pixels, (MAX_WIDTH, round(h * MAX_WIDTH / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise VlmInputError("프레임을 JPEG 로 인코딩하지 못했다")
    return "image/jpeg", buf.tobytes()


Opener = Callable[..., Any]  # urllib.request.urlopen 꼴


class VlmClient:
    def __init__(self, settings: VlmSettings, *, encode: Encoder = encode_jpeg,
                 opener: Opener = urllib.request.urlopen) -> None:
        self._s = settings
        self._encode = encode
        self._open = opener

    @property
    def model(self) -> str:
        return self._s.model

    def build_payload(self, prompt: str, frames: Sequence[VlmFrame], *, system: str | None = None,
                      response_format: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """요청 본문. 이미지 파트는 frames 를 인코딩한 data URL 뿐이다."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise VlmInputError("질문은 비지 않은 문자열이다")
        if system is not None and not isinstance(system, str):
            raise VlmInputError("system 은 문자열이다")
        if isinstance(frames, (str, bytes, np.ndarray)):
            raise VlmInputError("이미지는 VlmFrame(prepare_vlm_frames 산출물)만 받는다")
        frames = tuple(frames)
        if not all(isinstance(f, VlmFrame) for f in frames):
            raise VlmInputError("이미지는 VlmFrame(prepare_vlm_frames 산출물)만 받는다")
        urls = []
        for f in frames:
            mime, data = self._encode(f.pixels)
            urls.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": u}} for u in urls]
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}] if system is not None else []
        messages.append({"role": "user", "content": content})
        s = self._s
        payload: dict[str, Any] = {"model": s.model, "messages": messages, "max_tokens": s.max_tokens,
                                   "repetition_penalty": s.repetition_penalty}
        if s.frequency_penalty is not None:
            payload["frequency_penalty"] = s.frequency_penalty
        if s.temperature is not None:
            payload["temperature"] = s.temperature
        if response_format is not None:
            if not isinstance(response_format, Mapping) or response_format.get("type") not in RESPONSE_FORMAT_TYPES:
                raise VlmInputError(f"response_format.type 은 {' 또는 '.join(RESPONSE_FORMAT_TYPES)} 다")
            payload["response_format"] = dict(response_format)
        _check_images(payload, set(urls))
        return payload

    def complete(self, prompt: str, frames: Sequence[VlmFrame], *, system: str | None = None,
                 response_format: Mapping[str, Any] | None = None,
                 timeout_s: float | None = None) -> VlmResult:
        """질문 + 얼굴 처리된 프레임 → 답. response_format 은 형식 강제(json_schema 등, 스키마는 처리기가 정한다)."""
        effective_timeout = self._effective_timeout(timeout_s)
        payload = self.build_payload(prompt, frames, system=system, response_format=response_format)
        t0 = time.monotonic()
        raw = self._post(payload, timeout_s=effective_timeout)
        return _parse(raw, self._s.model, round((time.monotonic() - t0) * 1000))

    def _effective_timeout(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return self._s.timeout_s
        if timeout_s <= 0:
            raise VlmTimeout("VLM 호출에 남은 시간 예산이 없다")
        return min(self._s.timeout_s, float(timeout_s))

    def _post(self, payload: Mapping[str, Any], *, timeout_s: float | None = None) -> bytes:
        s = self._s
        limit_s = self._effective_timeout(timeout_s)
        request = urllib.request.Request(s.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         method="POST", headers={"Content-Type": "application/json"})
        try:
            with self._open(request, timeout=limit_s) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                raise VlmUnavailable(f"VLM 일시 오류(HTTP {e.code})") from None
            raise VlmRejected(f"VLM 이 요청을 거부했다(HTTP {e.code}){_error_hint(e)}") from None
        except TimeoutError as e:
            raise VlmTimeout(f"VLM 응답이 {limit_s:g}초 안에 오지 않았다") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise VlmTimeout(f"VLM 응답이 {limit_s:g}초 안에 오지 않았다") from e
            raise VlmUnavailable(f"VLM 에 연결하지 못했다: {e.reason}") from e
        except (ConnectionError, OSError) as e:  # 응답 읽는 중 끊김
            raise VlmUnavailable(f"VLM 연결이 끊겼다: {type(e).__name__}") from e


def _check_images(payload: Mapping[str, Any], allowed: set[str]) -> None:
    """보내기 전 검사: 이미지 파트는 이 호출에서 VlmFrame 으로 만든 data URL 만. 외부 URL·저장소 주소가 섞이면 보내지 않는다."""
    for m in payload["messages"]:
        parts = m["content"] if isinstance(m["content"], list) else []
        for p in parts:
            if p.get("type") == "text":
                continue
            url = (p.get("image_url") or {}).get("url") if p.get("type") == "image_url" else None
            if not (isinstance(url, str) and url.startswith("data:image/") and url in allowed):
                raise VlmInputError("이미지 파트에 VlmFrame data URL 이 아닌 것이 있다")


def _error_hint(e: urllib.error.HTTPError) -> str:
    """vLLM 4xx 본문의 message(300자까지). 예: 문맥 길이 초과."""
    try:
        doc = json.loads(e.read() or b"{}")
    except (ValueError, OSError):
        return ""
    err = doc.get("error", doc) if isinstance(doc, Mapping) else None
    message = err.get("message") if isinstance(err, Mapping) else None
    return f": {message[:300]}" if isinstance(message, str) and message else ""


def _parse(raw: bytes, requested_model: str, latency_ms: int) -> VlmResult:
    try:
        doc = json.loads(raw)
        choice = doc["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise VlmBadResponse("VLM 응답이 chat completions 형식이 아니다") from e
    if not isinstance(content, str) or not content.strip():
        raise VlmBadResponse("VLM 응답 내용이 비었다")
    usage = doc.get("usage") if isinstance(doc.get("usage"), Mapping) else {}
    model = doc.get("model") if isinstance(doc.get("model"), str) and doc.get("model") else requested_model
    finish = choice.get("finish_reason") if isinstance(choice, Mapping) else None
    return VlmResult(content=content, model=model, latency_ms=latency_ms, input_tokens=_count(usage.get("prompt_tokens")),
                     output_tokens=_count(usage.get("completion_tokens")),
                     finish_reason=finish if isinstance(finish, str) else None)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
