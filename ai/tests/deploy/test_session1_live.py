"""c 항목 LIVE 하네스(deploy/scripts/session1_live.py) 시험 (S15P31A101-183). -182 LIVE 입력이 머지된 뒤에 돈다(없으면 건너뜀).

노트북: 진입점 대신 실제 Tier0Service + 검출기·추적 백엔드 대역(stub_tier0_live.py), 디코더 CPU, 작은 화면.
- 송신 4대 → 카메라 4대 ready, 디코딩 실패·검출 오류·결합 불일치 0, 종료 0 → PASS
- 반례: 영상 토큰이 틀리면(영상이 들어가지 않음) ready 가 되지 않아 FAIL(모르면 통과로 보지 않음)
- 반례: Tier 0 가 기동하지 못하면(설정 오류) FAIL, 하네스가 기다리다 멈추지 않는다
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

AI = Path(__file__).resolve().parents[2]
if str(AI) not in sys.path:
    sys.path.insert(0, str(AI))
pytest.importorskip("services.tier0.live_input", reason="-182 LIVE 입력 머지 뒤")
pytest.importorskip("av")

STUB = Path(__file__).with_name("stub_tier0_live.py")
SMALL = (256, 144)


def harness(live, tmp_path: Path, seconds: float = 25):
    h = live.LiveHarness(tmp_path / "w", seconds=seconds, decoder="cpu", size=SMALL)
    h.command = lambda: [sys.executable, str(STUB), "--config", str(h.cfg), "--run-seconds", str(seconds)]
    return h


def test_live_harness_passes_with_four_streams(live, tmp_path):
    h = harness(live, tmp_path)
    try:
        h.start(live.encode_stream(50, size=SMALL))
        ready = h.wait_ready(40)
        code = h.finish(90)
        status, detail, values = live.judge_live(h, code, ready)
    finally:
        h.close()
    assert status == "PASS", detail
    assert values["cameras_ready"] == 4 and values["inferred"] > 0 and values["frames_sent"] > 0
    assert values["pipeline_lag_ms_p95"] is not None  # 노트북 흉내가 시계 교환에 답해 지연을 안다
    assert not h.secrets.exists()  # 시험용 TLS 키는 지운다


def test_wrong_video_token_never_counts_as_ready(live, tmp_path, monkeypatch):
    h = harness(live, tmp_path, seconds=12)
    monkeypatch.setattr(h.ts, "LAPTOP_TOKEN", "wrong-token")  # 송신 쪽만 틀린 토큰
    try:
        h.start(live.encode_stream(25, size=SMALL))
        ready = h.wait_ready(15)
        code = h.finish(40)
        status, detail, values = live.judge_live(h, code, ready)
    finally:
        h.close()
    assert not ready and status == "FAIL" and values["cameras_ready"] == 0, detail


def test_tier0_that_cannot_start_fails_fast(live, tmp_path):
    h = harness(live, tmp_path, seconds=60)
    doc = json.loads(h.cfg.read_text(encoding="utf-8"))
    doc["input"]["decoder"] = "no-such-decoder"
    h.cfg.write_text(json.dumps(doc), encoding="utf-8")
    t0 = time.monotonic()
    try:
        h.start(live.encode_stream(25, size=SMALL))
        ready = h.wait_ready(60)
        code = h.finish(30)
        status, _, values = live.judge_live(h, code, ready)
    finally:
        h.close()
    assert status == "FAIL" and values["exit_code"] not in (0, None)
    assert time.monotonic() - t0 < 45
