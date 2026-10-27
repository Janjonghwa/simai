"""VLM 클라이언트 (S15P31A101-22): 가짜 vLLM 서버(127.0.0.1)로 요청 모양·입력 관문·응답 파싱·오류 코드·설정을 본다. GPU 없음."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from simai_ai.vlm.client import (
    VlmBadResponse,
    VlmClient,
    VlmConfigError,
    VlmInputError,
    VlmRejected,
    VlmSettings,
    VlmTimeout,
    VlmUnavailable,
)
from simai_ai.vlm.face_blur import Person, prepare_vlm_frames

REPO = Path(__file__).resolve().parents[3]
FACE_KPS = ((110.0, 110.0, 0.9), (104.0, 100.0, 0.9), (116.0, 100.0, 0.9), (95.0, 102.0, 0.8), (125.0, 102.0, 0.8))
PERSON = Person(bbox=(65.0, 60.0, 155.0, 300.0), keypoints=FACE_KPS + (None,) * 12)
OK = {"model": "cosmos3-nano", "choices": [{"message": {"role": "assistant", "content": "사람 1명, 위험 없음."}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 900, "completion_tokens": 12}}


class Provider:
    def people(self, image):
        return [PERSON]


def raw_encode(pixels):
    """시험용 인코더: 픽셀 바이트 그대로(흐림이 서버까지 가는지 비교한다)."""
    return "image/png", np.ascontiguousarray(pixels).tobytes()


def frames(n=1):
    rng = np.random.default_rng(0)
    items = [(f"s3://f{i}", rng.integers(0, 256, (320, 240, 3), dtype=np.uint8)) for i in range(n)]
    return items, prepare_vlm_frames(items, "BLURRED", Provider()).frames


class Fake:
    """가짜 vLLM. reply = (status, body bytes) 또는 delay_s 만큼 늦게."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.status, self.body, self.delay_s = 200, json.dumps(OK).encode(), 0.0
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                fake.requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                time.sleep(fake.delay_s)
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(fake.body)))
                self.end_headers()
                try:
                    self.wfile.write(fake.body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.server.shutdown()
    f.server.server_close()


def client(fake, **kw):
    return VlmClient(VlmSettings(port=fake.port, **kw), encode=raw_encode)


# ------------------------------------------------------------------ 요청 모양


def test_request_shape_defaults(fake):
    _, fs = frames(2)
    r = client(fake).complete("몇 명?", fs)
    path, body = fake.requests[0]
    assert path == "/v1/chat/completions"
    assert body["model"] == "cosmos3-nano" and body["max_tokens"] == 256 and body["repetition_penalty"] == 1.05
    assert "temperature" not in body and "frequency_penalty" not in body and "response_format" not in body
    assert "stream" not in body
    (msg,) = body["messages"]
    assert msg["role"] == "user"
    assert [p["type"] for p in msg["content"]] == ["text", "image_url", "image_url"]
    assert msg["content"][0]["text"] == "몇 명?"
    assert all(p["image_url"]["url"].startswith("data:image/png;base64,") for p in msg["content"][1:])
    assert (r.content, r.model, r.input_tokens, r.output_tokens, r.finish_reason) == ("사람 1명, 위험 없음.", "cosmos3-nano", 900, 12, "stop")
    assert r.latency_ms >= 0


def test_optional_args_sent_only_when_set(fake):
    _, fs = frames()
    schema = {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}}
    client(fake, temperature=0.0, frequency_penalty=0.3, repetition_penalty=1.0, max_tokens=64).complete(
        "q", fs, system="너는 관제 보조다", response_format=schema)
    body = fake.requests[0][1]
    assert body["temperature"] == 0.0 and body["frequency_penalty"] == 0.3 and body["repetition_penalty"] == 1.0
    assert body["max_tokens"] == 64 and body["response_format"] == schema
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_same_shape_as_vlm_int(fake, monkeypatch):
    """4차 세션 측정 도구(deploy/scripts/vlm_int.py)와 같은 경로·메시지 모양. 다른 것은 측정용 stream·temperature 0 과 반복 억제뿐."""
    spec = importlib.util.spec_from_file_location("deploy_scripts_vlm_int_c", REPO / "deploy" / "scripts" / "vlm_int.py")
    vi = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = vi
    spec.loader.exec_module(vi)
    seen = {}

    def capture(req, timeout):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        raise OSError("캡처만")

    monkeypatch.setattr(vi.urllib.request, "urlopen", capture)
    vi.chat_once("http://127.0.0.1:8000/v1/chat/completions", "cosmos3-nano", "data:image/jpeg;base64,AA==", 256)
    _, fs = frames()
    client(fake).complete("q", fs)
    ours = fake.requests[0][1]
    theirs = seen["body"]
    assert seen["url"].endswith(fake.requests[0][0])
    assert set(theirs) - {"stream", "temperature"} == set(ours) - {"repetition_penalty"}
    assert (ours["model"], ours["max_tokens"]) == (theirs["model"], theirs["max_tokens"])
    shape = lambda b: [(m["role"], [p["type"] for p in m["content"]]) for m in b["messages"]]
    assert shape(ours) == shape(theirs)


# ------------------------------------------------------------------ 입력 관문


@pytest.mark.parametrize("bad", [
    [np.zeros((4, 4, 3), np.uint8)], np.zeros((4, 4, 3), np.uint8), [b"\xff\xd8"], b"\xff\xd8",
    ["https://example.com/a.jpg"], ["s3://bucket/frame.jpg"], "s3://bucket/frame.jpg", [Path("a.jpg")],
])
def test_rejects_non_vlmframe_images(fake, bad):
    with pytest.raises(VlmInputError) as e:
        client(fake).complete("q", bad)
    assert e.value.code == "VLM_INPUT" and not e.value.retryable
    assert fake.requests == []


@pytest.mark.parametrize("url", ["https://example.com/x.jpg", "s3://bucket/x.jpg", "data:image/png;base64,eA=="])
def test_send_check_rejects_foreign_image_parts(fake, url):
    """보내기 전 검사: 이 호출의 VlmFrame data URL 이 아닌 이미지 파트(외부·저장소 주소, 다른 data URL)가 섞이면 보내지 않는다."""
    from simai_ai.vlm.client import _check_images

    payload = client(fake).build_payload("q", frames()[1])
    allowed = {payload["messages"][0]["content"][1]["image_url"]["url"]}
    _check_images(payload, allowed)
    payload["messages"][0]["content"].append({"type": "image_url", "image_url": {"url": url}})
    with pytest.raises(VlmInputError):
        _check_images(payload, allowed)


def test_rejects_unknown_response_format(fake):
    with pytest.raises(VlmInputError):
        client(fake).complete("q", frames()[1], response_format={"type": "regex", "regex": ".*"})
    assert fake.requests == []


def test_empty_prompt_rejected(fake):
    _, fs = frames()
    with pytest.raises(VlmInputError):
        client(fake).complete("  ", fs)


# ------------------------------------------------------------------ 270 관문 → 클라이언트 종단


def test_end_to_end_sends_only_blurred_pixels(fake):
    items, fs = frames(2)
    client(fake).complete("q", fs)
    urls = [p["image_url"]["url"] for p in fake.requests[0][1]["messages"][0]["content"][1:]]
    for (_, original), f, url in zip(items, fs, urls, strict=True):
        sent = np.frombuffer(base64.b64decode(url.split(",", 1)[1]), np.uint8).reshape(original.shape)
        assert np.array_equal(sent, f.pixels)
        assert not np.array_equal(sent, original)  # 얼굴 영역이 바뀌었다
        assert np.array_equal(sent[200:], original[200:])  # 얼굴 밖은 그대로


def test_jpeg_encoder_resizes_to_1280():
    pytest.importorskip("cv2")
    from simai_ai.vlm.client import encode_jpeg

    mime, data = encode_jpeg(np.zeros((1080, 1920, 3), np.uint8))
    import cv2

    assert mime == "image/jpeg" and cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR).shape == (720, 1280, 3)


# ------------------------------------------------------------------ 응답 파싱


def test_missing_usage_is_none(fake):
    fake.body = json.dumps({"choices": [{"message": {"content": "답"}}]}).encode()
    r = client(fake).complete("q", frames()[1])
    assert (r.content, r.model, r.input_tokens, r.output_tokens, r.finish_reason) == ("답", "cosmos3-nano", None, None, None)


@pytest.mark.parametrize("body", [b"not json", json.dumps({"choices": []}).encode(),
                                  json.dumps({"choices": [{"message": {"content": "  "}}]}).encode(),
                                  json.dumps({"choices": [{"message": {"content": None}}]}).encode()])
def test_bad_responses(fake, body):
    fake.body = body
    with pytest.raises(VlmBadResponse) as e:
        client(fake).complete("q", frames()[1])
    assert e.value.code == "VLM_BAD_RESPONSE" and e.value.retryable


# ------------------------------------------------------------------ 오류 코드


@pytest.mark.parametrize(("status", "cls", "code", "retryable"), [
    (400, VlmRejected, "VLM_REQUEST_REJECTED", False),
    (404, VlmRejected, "VLM_REQUEST_REJECTED", False),
    (429, VlmUnavailable, "VLM_UNAVAILABLE", True),
    (500, VlmUnavailable, "VLM_UNAVAILABLE", True),
    (503, VlmUnavailable, "VLM_UNAVAILABLE", True),
])
def test_http_errors(fake, status, cls, code, retryable):
    fake.status, fake.body = status, json.dumps({"message": "max_model_len 초과", "type": "BadRequestError"}).encode()
    with pytest.raises(cls) as e:
        client(fake).complete("q", frames()[1])
    assert (e.value.code, e.value.retryable) == (code, retryable)
    if status == 400:
        assert "max_model_len 초과" in str(e.value)


def test_connection_refused():
    """vLLM 이 아직 안 떴거나 재기동 중(Windows 는 닫힌 포트 연결이 재시도로 늦어 opener 로 흉내 낸다)."""
    import urllib.error

    def refuse(req, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    with pytest.raises(VlmUnavailable) as e:
        VlmClient(VlmSettings(), encode=raw_encode, opener=refuse).complete("q", frames()[1])
    assert (e.value.code, e.value.retryable) == ("VLM_UNAVAILABLE", True)


def test_timeout(fake):
    fake.delay_s = 1.0
    with pytest.raises(VlmTimeout) as e:
        client(fake, timeout_s=0.2).complete("q", frames()[1])
    assert (e.value.code, e.value.retryable) == ("VLM_TIMEOUT", True)


# ------------------------------------------------------------------ 설정


def test_settings_defaults():
    s = VlmSettings.from_env({})
    assert s.url == "http://127.0.0.1:8000/v1/chat/completions"
    assert (s.model, s.timeout_s, s.max_tokens, s.repetition_penalty, s.frequency_penalty, s.temperature) == (
        "cosmos3-nano", 30.0, 256, 1.05, None, None)


def test_settings_from_env():
    s = VlmSettings.from_env({"SIMAI_VLLM_PORT": "8100", "SIMAI_VLLM_MODEL": "m", "SIMAI_VLM_TIMEOUT_S": "12.5",
                              "SIMAI_VLM_MAX_TOKENS": "512", "SIMAI_VLM_REPETITION_PENALTY": "1.1",
                              "SIMAI_VLM_FREQUENCY_PENALTY": "0.2", "SIMAI_VLM_TEMPERATURE": "0"})
    assert s.url == "http://127.0.0.1:8100/v1/chat/completions"
    assert (s.model, s.timeout_s, s.max_tokens, s.repetition_penalty, s.frequency_penalty, s.temperature) == (
        "m", 12.5, 512, 1.1, 0.2, 0.0)


@pytest.mark.parametrize("env", [
    {"SIMAI_VLLM_PORT": "abc"}, {"SIMAI_VLLM_PORT": "0"}, {"SIMAI_VLLM_PORT": "70000"},
    {"SIMAI_VLM_TIMEOUT_S": "0"}, {"SIMAI_VLM_TIMEOUT_S": "601"}, {"SIMAI_VLM_MAX_TOKENS": "0"},
    {"SIMAI_VLM_MAX_TOKENS": "1.5"}, {"SIMAI_VLM_REPETITION_PENALTY": "0"}, {"SIMAI_VLM_FREQUENCY_PENALTY": "3"},
    {"SIMAI_VLM_TEMPERATURE": "-1"},
])
def test_settings_invalid(env):
    with pytest.raises(VlmConfigError) as e:
        VlmSettings.from_env(env)
    assert (e.value.code, e.value.retryable) == ("VLM_CONFIG", False)
