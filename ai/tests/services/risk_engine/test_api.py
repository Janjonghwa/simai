"""Risk Engine 컨테이너 내부 API (services.risk_engine, S15P31A101-99, 계약 risk_plan_request·risk_plan_result 1.0.0).

계약 예시 왕복, 오류 판정 순서(계약 시험의 참고 분류기와 같은 결과), HTTP 상태·retryable, 응답 계약 검사, 동시 요청, 알림 경로 없음.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.risk_engine import handler as handler_mod
from services.risk_engine.app import create_app, health
from services.risk_engine.handler import HTTP_STATUS, MAX_BODY_BYTES, PLAN_PATH, handle
from simai_ai.providers.contract import schema_errors
from simai_ai.providers.testing import example_path, load_example
from simai_ai.risk.registry import default_registry

AI_DIR = Path(__file__).resolve().parents[3]
VOLATILE = ("msg_id", "sent_at")
VOLATILE_PAYLOAD = ("computed_at",)

pytestmark = pytest.mark.skipif(not example_path("risk_plan_request.json").is_file(), reason="contracts/ 예시가 없다")


def _contract_test_module():
    path = AI_DIR / "tests" / "risk" / "test_plan_contract.py"
    spec = importlib.util.spec_from_file_location("risk_plan_contract_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REF = _contract_test_module()


@pytest.fixture(scope="module")
def client():
    clock = iter(range(1760342399000, 1760342399000 + 10**9))
    with TestClient(create_app(now_ms=lambda: next(clock))) as c:
        yield c


def _strip(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    for k in VOLATILE:
        doc.pop(k)
    for k in VOLATILE_PAYLOAD:
        doc["payload"].pop(k)
    return doc


def _post(client, body) -> tuple[int, dict]:
    content = body if isinstance(body, (bytes, str)) else json.dumps(body)
    r = client.post(PLAN_PATH, content=content, headers={"content-type": "application/json"})
    doc = r.json()
    assert schema_errors(doc, "risk_plan_result") == [], doc  # 어떤 응답이든 계약 문서다
    return r.status_code, doc


def _unknown_rule_request():
    doc = load_example("risk_plan_request.json")
    doc["payload"]["rules"]["rule_versions"] = [{"rule_id": "R8", "rule_version": 1}]
    return doc


def _unreadable_request():
    doc = load_example("risk_plan_request.json")
    doc["payload"].pop("request_id")
    return doc


EXAMPLE_PAIRS = [
    (lambda: load_example("risk_plan_request.json"), "risk_plan_result.json", 200),
    (lambda: load_example("risk_plan_request.generated_positions.json"), "risk_plan_result.generated_positions.json", 200),
    (_unknown_rule_request, "risk_plan_result.unknown_rule.json", 422),
    (_unreadable_request, "risk_plan_result.unreadable_request.json", 400),
]


@pytest.mark.parametrize(("make", "result_name", "status"), EXAMPLE_PAIRS, ids=lambda x: str(x)[:40])
def test_contract_examples_round_trip(client, make, result_name, status):
    """계약 예시 요청은 계약 예시 결과와 같은 응답이다(봉투 msg_id·sent_at, computed_at 만 다르다)."""
    code, doc = _post(client, make())
    assert code == status
    assert _strip(doc) == _strip(load_example(result_name))


@pytest.mark.parametrize("case", [c[0] for c in REF._cases()])
def test_error_precedence_matches_reference_classifier(client, case):
    """계약 시험의 오류 판정 순서 19사례: API 의 code·request_id·HTTP 상태가 참고 분류기와 같다."""
    body, expected = next((b, e) for name, b, e in REF._cases() if name == case)
    ref_code, ref_request_id = REF._classify(body)
    assert ref_code == expected
    code, doc = _post(client, body)
    payload = doc["payload"]
    if expected == "OK":
        assert code == 200 and payload["status"] == "OK"
        assert payload["request_id"] == ref_request_id
        return
    assert payload["status"] == "ERROR"
    assert payload["error"]["code"] == expected
    assert payload["request_id"] == ref_request_id
    assert code == HTTP_STATUS[expected] == REF.HTTP_STATUS[expected]
    assert payload["error"]["retryable"] is False


@pytest.mark.parametrize(
    "body",
    [
        b'{"payload": {"request_id": NaN}}',
        json.dumps(load_example("risk_plan_request.json")).replace("6.0, 4.3", "NaN, 4.3").encode(),
        json.dumps(load_example("risk_plan_request.json")).replace("6.0, 4.3", "Infinity, 4.3").encode(),
        json.dumps(load_example("risk_plan_request.json")).replace("6.0, 4.3", "1e400, 4.3").encode(),
        b"[]",
        b'"x"',
        b'{"payload": []}',
        b'{"payload": {"request_id": 7}}',
        "{\"payload\": {\"request_id\": \"\ud800\"}}".encode("utf-8", "surrogatepass"),
        b"",
    ],
    ids=["nan-id", "nan-xy", "inf-xy", "overflow-xy", "list", "string", "payload-list", "int-id", "bad-utf8", "empty"],
)
def test_unreadable_bodies_are_invalid_request_without_id(client, body):
    """반례: NaN·Infinity·범위를 넘는 수는 JSON 이 아니다. 엔진까지 가서 ENGINE_ERROR(재시도)나 OK 가 되지 않는다."""
    code, doc = _post(client, body)
    assert code == 400
    assert doc["payload"]["request_id"] is None
    assert doc["payload"]["error"]["code"] == "INVALID_REQUEST"


def test_body_over_limit_is_rejected(client):
    doc = load_example("risk_plan_request.json")
    doc["pad"] = "x" * MAX_BODY_BYTES
    code, out = _post(client, doc)
    assert code == 400 and out["payload"]["error"]["code"] == "INVALID_REQUEST"


def test_engine_error_is_retryable_and_hides_details(client, monkeypatch):
    def boom(payload):
        raise RuntimeError("C:/secret/path token=abc")

    monkeypatch.setattr(handler_mod, "evaluate_plan", boom)
    code, doc = _post(client, load_example("risk_plan_request.json"))
    assert code == 500
    err = doc["payload"]["error"]
    assert err == {"code": "ENGINE_ERROR", "message": "계산 중 내부 오류(RuntimeError)", "retryable": True}
    assert doc["payload"]["request_id"] == load_example("risk_plan_request.json")["payload"]["request_id"]


def test_result_outside_contract_becomes_engine_error(client, monkeypatch):
    """반례: 규칙이 NaN·확률 이름 metrics 를 내면 계약 밖 문서(비JSON NaN)를 보내지 않고 ENGINE_ERROR 다."""
    real = handler_mod.evaluate_plan

    def bad(payload, name):
        out = real(payload)
        item = dict(out.items[0], metrics={name: float("nan") if name == "zone_distance_m" else 0.5})
        return type(out)(out.positions, (item,) + out.items[1:], out.evaluation)

    for name in ("zone_distance_m", "fall_probability"):
        monkeypatch.setattr(handler_mod, "evaluate_plan", lambda p, n=name: bad(p, n))
        code, doc = _post(client, load_example("risk_plan_request.json"))
        assert code == 500 and doc["payload"]["error"]["code"] == "ENGINE_ERROR", name


def test_empty_positions_array_generates(client):
    doc = load_example("risk_plan_request.generated_positions.json")
    doc["payload"]["layout"]["work_positions"] = []
    code, out = _post(client, doc)
    assert code == 200
    assert _strip(out) == _strip(load_example("risk_plan_result.generated_positions.json"))


def test_same_request_twice_same_result(client):
    """같은 request_id 를 다시 보내도 결과가 같다(계산만 하고 저장하지 않음, Spring 재시도)."""
    body = load_example("risk_plan_request.generated_positions.json")
    first, second = _post(client, body)[1], _post(client, body)[1]
    assert _strip(first) == _strip(second)
    assert first["msg_id"] != second["msg_id"]


def test_concurrent_requests_do_not_mix(client):
    """동시 요청 24개(정상·생성·오류 섞음): 각 응답은 자기 request_id 와 자기 결과만 싣는다."""
    jobs = []
    for i in range(24):
        make, result_name, status = EXAMPLE_PAIRS[i % 3]  # unreadable 은 request_id 가 없어 뺀다
        req = make()
        rid = f"0192a6f0-7c1e-7b3a-9f2d-{i:012d}"
        req["payload"]["request_id"] = rid
        expected = _strip(load_example(result_name))
        expected["payload"]["request_id"] = rid
        jobs.append((req, expected, status))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: _post(client, j[0]), jobs))
    for (req, expected, status), (code, doc) in zip(jobs, results, strict=True):
        assert code == status
        assert _strip(doc) == expected


def test_healthz_reports_engine_and_registry(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["engine_version"] == "0.1.0"
    assert [(x["rule_id"], x["rule_version"]) for x in body["rules"]] == list(default_registry())
    assert body["registry_digest"].startswith("sha256:") and body == health()
    assert body["position_rule"] == "plan-positions-1"


def test_docs_and_other_methods_are_closed(client):
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(path).status_code == 404
    assert client.get(PLAN_PATH).status_code == 405


def test_handle_without_fastapi_matches_api(client):
    """handler.handle 은 FastAPI 없이 같은 결과를 낸다(Tier 0 이미지에서 라이브러리로 대조할 때 쓴다)."""
    body = json.dumps(load_example("risk_plan_request.json")).encode()
    status, content = handle(body)
    assert status == 200
    assert _strip(json.loads(content)) == _strip(_post(client, body)[1])


def test_no_alert_journal_or_network_modules_loaded():
    """앱을 만들고 한 번 판정해도 알림 전송·저널·Tier 0 전송·네트워크 클라이언트 모듈이 올라오지 않는다(규칙 5, 12.2)."""
    code = (
        "import json, sys\n"
        "from services.risk_engine.app import create_app\n"
        "from services.risk_engine.handler import handle\n"
        "from simai_ai.providers.testing import load_example\n"
        "create_app()\n"
        "assert handle(json.dumps(load_example('risk_plan_request.json')).encode())[0] == 200\n"
        "bad = ('websockets', 'httpx', 'requests', 'urllib3', 'services.tier0', 'services.worker', 'simai_ai.eventlog',\n"
        "       'simai_ai.recovery', 'simai_ai.io')\n"
        "print(json.dumps(sorted(m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in bad))))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=AI_DIR, capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def test_missing_contract_schemas_fail_startup_and_are_not_input_errors(monkeypatch, tmp_path):
    """반례: 계약 스키마가 없는 이미지(예: 이전 Tier 0 이미지의 contracts/)는 기동에서 실패하고, 요청 중이면 입력 오류(400)가 아니다.
    계약 문서를 검증할 수 없으므로 계약 밖 본문을 보내지 않고 본문 없는 500(계약 '본문 없는 실패', Spring 재시도)이다."""
    from simai_ai.providers import contract

    body = json.dumps(load_example("risk_plan_request.json")).encode()
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "defs.schema.json").write_text(
        (contract.contracts_dir() / "common" / "defs.schema.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv(contract.CONTRACTS_DIR_ENV, str(tmp_path))
    with pytest.raises(RuntimeError, match="risk_plan_request"):
        create_app()
    assert handle(body) == (500, b"")


DEEP = 1500


@pytest.mark.parametrize(
    "body",
    [
        b"[" * DEEP + b"0" + b"]" * DEEP,
        b'{"a":' * DEEP + b"0" + b"}" * DEEP,
        # 읽을 수 있는 request_id 뒤에 깊은 중첩(스키마 검증 단계에서 넘침)
        b'{"payload": {"request_id": "0192a6f0-7c1e-7b3a-9f2d-000000000411", "x": ' + b"[" * 900 + b"]" * 900 + b"}}",
    ],
    ids=["deep-array", "deep-object", "deep-after-request-id"],
)
def test_deeply_nested_json_is_invalid_request_not_engine_error(client, body):
    """반례(리뷰 F1): 깊게 중첩된 JSON 의 RecursionError 는 읽기 단계 INVALID_REQUEST(400, 재시도 안 함)다.
    request_id 를 모르면 null 이고, 응답은 늘 계약을 통과한다(_post 가 검사)."""
    code, doc = _post(client, body)
    assert code == 400
    err = doc["payload"]["error"]
    assert err["code"] == "INVALID_REQUEST" and err["retryable"] is False


def test_unbuildable_contract_document_becomes_bodyless_500(client, monkeypatch):
    """반례(리뷰 F1 fallback): request_id 를 모르는 채 내부 오류가 나면 계약상 ENGINE_ERROR 문서를 만들 수 없다(null 은
    INVALID_REQUEST 만). 계약 밖 문서를 보내지 않고 본문 없는 500(계약 '본문 없는 실패', Spring 재시도)이다."""

    def boom(body):
        raise RuntimeError("x")

    monkeypatch.setattr(handler_mod, "_read", boom)
    assert handle(b"{}") == (500, b"")
    r = client.post(PLAN_PATH, content=b"{}", headers={"content-type": "application/json"})
    assert r.status_code == 500 and r.content == b"" and not r.headers["content-type"].startswith("application/json")


def _asgi_post(headers, chunk_size, max_chunks=10_000):
    """ASGI 로 직접 본문을 청크로 보낸다(네트워크 없이). (HTTP 상태, 본문, 앱이 받아 간 청크 수)."""
    import asyncio

    app = create_app()
    state = {"chunks": 0}
    messages = []

    async def receive():
        state["chunks"] += 1
        return {"type": "http.request", "body": b" " * chunk_size, "more_body": state["chunks"] < max_chunks}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": PLAN_PATH, "raw_path": PLAN_PATH.encode(), "query_string": b"", "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in headers], "server": ("test", 8080), "client": ("test", 1),
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(body), state["chunks"]


@pytest.mark.parametrize(
    ("headers", "max_reads"),
    [
        ([], MAX_BODY_BYTES // (1 << 20) + 1),  # Content-Length 없음: 상한을 넘는 청크에서 멈춘다
        ([("content-length", "10")], MAX_BODY_BYTES // (1 << 20) + 1),  # 거짓(작은) Content-Length 를 믿지 않는다
        ([("content-length", str(MAX_BODY_BYTES + 1))], 0),  # 상한을 넘는다고 밝히면 읽기 전에 거절
    ],
    ids=["no-length", "false-length", "declared-too-large"],
)
def test_body_limit_applies_while_receiving(headers, max_reads):
    """반례(리뷰 F2): 4 MiB 상한은 다 받은 뒤가 아니라 받는 중에 적용한다. 넘으면 나머지 청크를 읽지 않는다."""
    status, doc, chunks = _asgi_post(headers, chunk_size=1 << 20)
    assert status == 400
    assert doc["payload"]["error"]["code"] == "INVALID_REQUEST" and doc["payload"]["request_id"] is None
    assert schema_errors(doc, "risk_plan_result") == []
    assert chunks <= max_reads  # 1 MiB 씩 10,000 개(약 10 GB)를 보내려 해도 상한 + 1 청크까지만 받는다


def test_body_at_limit_is_read(client):
    """경계: 정확히 상한인 본문은 읽는다(JSON 이 아니라 INVALID_REQUEST 지만 '너무 크다'가 아니다)."""
    code, doc = _post(client, b" " * MAX_BODY_BYTES)
    assert code == 400 and "너무 크다" not in doc["payload"]["error"]["message"]


def test_generated_positions_replayed_through_api_as_requested(client):
    """리뷰 C4: Spring 이 생성 위치를 다시 계산하려면 gen- ID 를 쓸 수 없다(요청 스키마가 막음). 계획 ID 로 바꿔 API 로 보내면
    같은 좌표·같은 판정이 나온다(ID 만 바뀜)."""
    request = load_example("risk_plan_request.generated_positions.json")
    _, first = _post(client, request)
    rename = {p["position_id"]: "p-" + p["position_id"].removeprefix("gen-") for p in first["payload"]["positions"]}
    request["payload"]["layout"]["work_positions"] = [
        {k: (rename[v] if k == "position_id" else v) for k, v in p.items() if k != "position_source"}
        for p in first["payload"]["positions"]
    ]
    assert schema_errors(request, "risk_plan_request") == []
    code, again = _post(client, request)
    assert code == 200
    assert {p["position_source"] for p in again["payload"]["positions"]} == {"REQUESTED"}
    expected = [i | {"position_ids": [rename[x] for x in i["position_ids"]]} for i in first["payload"]["items"]]
    assert again["payload"]["items"] == expected
