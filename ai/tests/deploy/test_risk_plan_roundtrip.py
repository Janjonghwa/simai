"""deploy/scripts/risk_plan_roundtrip.py (S15P31A101-99): 라이브러리 직접·실제 HTTP 서버 두 방식이 계약 예시와 같고, 다르면 잡는다."""

from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
import time

import pytest

from .conftest import REPO, load_script

AI_DIR = REPO / "ai"
EXAMPLES = REPO / "contracts" / "examples"

if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))


@pytest.fixture(scope="module")
def rt():
    return load_script("risk_plan_roundtrip")


def test_direct_matches_contract_examples(rt, tmp_path):
    dump = tmp_path / "direct.json"
    assert rt.main(["--direct", "--examples", str(EXAMPLES), "--dump", str(dump)]) == 0
    doc = json.loads(dump.read_text(encoding="utf-8"))
    assert set(doc) == {"requested", "generated", "generated_empty_array", "unknown_rule", "unreadable_request"}


def test_http_server_matches_direct(rt, tmp_path):
    """실제 uvicorn 서버(127.0.0.1, 임의 포트)로 왕복한 응답이 라이브러리 직접 호출과 같다(D-G3-04 의 비교 방식)."""
    import uvicorn

    from services.risk_engine.app import create_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert time.monotonic() < deadline, "서버가 뜨지 않았다"
            time.sleep(0.05)
        http, direct = tmp_path / "http.json", tmp_path / "direct.json"
        assert rt.main(["--url", f"http://127.0.0.1:{port}", "--examples", str(EXAMPLES), "--dump", str(http)]) == 0
        assert rt.main(["--direct", "--examples", str(EXAMPLES), "--dump", str(direct)]) == 0
        assert http.read_text(encoding="utf-8") == direct.read_text(encoding="utf-8")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_mismatch_is_reported(rt, tmp_path, capsys):
    """음성 대조: 기대 예시의 등급 하나를 바꾸면 점검이 실패한다(비교가 실제로 일한다)."""
    examples = tmp_path / "examples"
    shutil.copytree(EXAMPLES, examples)
    path = examples / "risk_plan_result.generated_positions.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["payload"]["items"][0]["severity"] = "LOW"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert rt.main(["--direct", "--examples", str(examples)]) == 1
    out = capsys.readouterr().out
    assert "FAIL  generated " in out and "FAIL  generated_empty_array" in out and "PASS  requested" in out
