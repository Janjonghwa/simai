from fastapi.testclient import TestClient
from services.risk_engine.app import create_app
from services.risk_engine import graph_store
from simai_ai.report import llm
from tests.report.test_tbm_graph import Store, Client, draft, REQUEST

def test_report_endpoint(monkeypatch):
    monkeypatch.setattr(graph_store, "PgGraphStore", lambda dsn: Store())
    monkeypatch.setattr(llm.LlmSettings, "from_env", lambda env: None)
    monkeypatch.setattr(llm, "make_client", lambda settings: Client([draft()]))
    with TestClient(create_app()) as client:
        response = client.post("/v1/report/generate", json=REQUEST)
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"


def test_report_endpoint_accepts_daily_and_rejects_unknown_kind(monkeypatch):
    from tests.report.test_tbm_graph import DAILY, daily_draft
    monkeypatch.setattr(graph_store, "PgGraphStore", lambda dsn: Store())
    monkeypatch.setattr(llm.LlmSettings, "from_env", lambda env: None)
    monkeypatch.setattr(llm, "make_client", lambda settings: Client([daily_draft()]))
    with TestClient(create_app()) as client:
        daily = client.post("/v1/report/generate", json=DAILY)
        unknown = client.post("/v1/report/generate", json=dict(DAILY, report_kind="WEEKLY"))
    assert daily.status_code == 200 and daily.json()["status"] == "SUCCEEDED"
    assert unknown.status_code == 400
