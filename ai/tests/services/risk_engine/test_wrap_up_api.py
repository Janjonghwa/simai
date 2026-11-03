import copy

from fastapi.testclient import TestClient

from services.risk_engine.app import create_app
from services.risk_engine.handler import MAX_BODY_BYTES
from simai_ai.report import llm
from simai_ai.report.wrap_up import deterministic
from tests.report.test_wrap_up import REQUEST, Client


def test_wrap_up_endpoint(monkeypatch):
    fake = Client([deterministic(REQUEST["facts"])])
    monkeypatch.setattr(llm.LlmSettings, "from_env", lambda env: None)
    monkeypatch.setattr(llm, "make_client", lambda settings: fake)
    with TestClient(create_app()) as client:
        response = client.post("/v1/report/wrap-up", json=REQUEST)
    assert response.status_code == 200
    assert response.json()["fallback"] is False
    assert response.json()["model"] == "fake-llm"


def test_wrap_up_rejects_bad_json_and_personal_fields_before_llm(monkeypatch):
    def forbidden(env):
        raise AssertionError("invalid input must not call LLM")

    monkeypatch.setattr(llm.LlmSettings, "from_env", forbidden)
    request = copy.deepcopy(REQUEST)
    request["signer_name"] = "김철수"
    with TestClient(create_app()) as client:
        assert client.post("/v1/report/wrap-up", json=request).status_code == 400
        assert client.post("/v1/report/wrap-up", content=b"{").status_code == 400
        assert client.post("/v1/report/wrap-up", json=[]).status_code == 400
        assert client.post("/v1/report/wrap-up", content=b"x" * (MAX_BODY_BYTES + 1)).status_code == 413


def test_wrap_up_endpoint_falls_back_on_timeout(monkeypatch):
    fake = Client([llm.LlmTimeout("timeout")])
    monkeypatch.setattr(llm.LlmSettings, "from_env", lambda env: None)
    monkeypatch.setattr(llm, "make_client", lambda settings: fake)
    with TestClient(create_app()) as client:
        response = client.post("/v1/report/wrap-up", json=REQUEST)
    assert response.status_code == 200
    assert response.json() == {"text": deterministic(REQUEST["facts"]), "fallback": True, "model": "deterministic"}


def test_wrap_up_streamed_body_is_bounded_without_content_length():
    def chunks():
        yield b"x" * (MAX_BODY_BYTES // 2)
        yield b"x" * (MAX_BODY_BYTES // 2 + 1)

    with TestClient(create_app()) as client:
        assert client.post("/v1/report/wrap-up", content=chunks()).status_code == 413
