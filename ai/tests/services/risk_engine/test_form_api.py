"""Risk Engine 컨테이너 TBM 양식 경로(S15P31A101-616): POST /v1/report/form-draft · /v1/report/form-ending."""

import copy

from fastapi.testclient import TestClient

from services.risk_engine import graph_store
from services.risk_engine.app import create_app
from services.risk_engine.handler import MAX_BODY_BYTES
from simai_ai.providers.contract import schema_errors
from simai_ai.report import llm
from tests.report.test_tbm_form import ENDING, REQUEST, Client, Store, ending_text, output


def fake_llm(monkeypatch, replies):
    client = Client(replies)
    monkeypatch.setattr(llm.LlmSettings, "from_env", lambda env: None)
    monkeypatch.setattr(llm, "make_client", lambda settings: client)
    return client


def test_form_draft_endpoint(monkeypatch):
    monkeypatch.setenv("SIMAI_RAG_PG_DSN", "postgresql://fake")
    monkeypatch.setattr(graph_store, "PgGraphStore", lambda dsn: Store())
    fake_llm(monkeypatch, [output()])
    with TestClient(create_app()) as client:
        response = client.post("/v1/report/form-draft", json=REQUEST)
    assert response.status_code == 200
    body = response.json()
    assert schema_errors(body, "tbm_form_draft_result") == []
    assert body["fallback"] is False and body["citations"][0]["article_ref"] == "제43조"


def test_form_draft_without_llm_settings_is_fallback(monkeypatch):
    monkeypatch.delenv("SIMAI_RAG_PG_DSN", raising=False)
    for key in ("SIMAI_LLM_BASE_URL", "SIMAI_LLM_MODEL", "SIMAI_LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with TestClient(create_app()) as client:
        response = client.post("/v1/report/form-draft", json=REQUEST)
    assert response.status_code == 200
    body = response.json()
    assert body["fallback"] is True and body["fallback_reason"] == "LLM_NOT_CONFIGURED"
    assert body["steps"][0]["status"] == "SKIPPED"


def test_form_draft_rejects_identity_and_inconsistent_input_before_llm(monkeypatch):
    def forbidden(env):
        raise AssertionError("invalid input must not call LLM")

    monkeypatch.setattr(llm.LlmSettings, "from_env", forbidden)
    with TestClient(create_app()) as client:
        assert client.post("/v1/report/form-draft", json=dict(REQUEST, leader={"name": "김철수"})).status_code == 400
        assert client.post("/v1/report/form-draft", json=dict(REQUEST, rule_tags=["R1"])).status_code == 400
        assert client.post("/v1/report/form-draft", content=b"{").status_code == 400
        assert client.post("/v1/report/form-draft", content=b"x" * (MAX_BODY_BYTES + 1)).status_code == 413


def test_form_ending_endpoint(monkeypatch):
    fake_llm(monkeypatch, [ending_text()])
    with TestClient(create_app()) as client:
        response = client.post("/v1/report/form-ending", json=ENDING)
        bad = copy.deepcopy(ENDING)
        bad["facts"]["event_count"] = 99
        inconsistent = client.post("/v1/report/form-ending", json=bad)
        named = copy.deepcopy(ENDING)
        named["facts"]["signer_name"] = "김철수"
        identity = client.post("/v1/report/form-ending", json=named)
    assert response.status_code == 200
    assert schema_errors(response.json(), "tbm_form_ending_result") == []
    assert response.json()["fallback"] is False
    assert inconsistent.status_code == 400 and identity.status_code == 400
