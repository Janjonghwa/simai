"""simai-bootstrap after-live 와 Tier 0 정상 종료 기록(S15P31A101-403). GPU·네트워크를 쓰지 않는다(장면 모드 실행은 가로챈다).

- after-live: 로비 작업 폴더에 정상 종료 기록이 있어야 띄운다(없음·표시 파일 남음·깨짐 → 75). 장면 준비 모드로 워커만(--once 없이),
  같은 state/ 와 표시 파일, 자동 제출 켬·끝나면 종료, 개발 플래그·좁은 torch 상한은 넘기지 않는다.
- Tier 0 시작 경로(감시 프로세스 start_tier0)는 지난 정상 종료 기록을 지운다(엔진 준비 중을 LIVE 아님으로 보지 않게).
- 기록 경로 규칙이 분석 워커 접수 문(simai_ai.gpu.admission.clean_stop_path)과 같다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from simai_ai.gpu.admission import clean_stop_path, clean_stop_record
from tests.deploy.test_simai_supervisor import World, _sup


@pytest.fixture
def lobby(bs, tmp_path, monkeypatch):
    root = tmp_path / "simai"
    work = root / "runs" / "lobby-live"
    (work / "state").mkdir(parents=True)
    monkeypatch.setenv("SIMAI_ROOT", str(root))
    monkeypatch.setenv("SIMAI_EC2_BASE_URL", "https://ec2.test")
    monkeypatch.setenv("SIMAI_WORKER_DEV_NO_LIVE", "1")
    monkeypatch.setenv("SIMAI_WORKER_TORCH_MEMORY_FRACTION", "0.05")
    for key in ("SIMAI_TIER0_MARKER", "SIMAI_STATE_DIR", "SIMAI_AUTO_STATIC_EXIT_WHEN_DONE", "SIMAI_AUTO_STATIC_RECONSTRUCT"):
        monkeypatch.delenv(key, raising=False)
    calls: list[dict] = []

    def fake_scene(rest):
        calls.append({"rest": list(rest), "env": dict(os.environ)})
        return 0

    monkeypatch.setattr(bs, "scene", fake_scene)
    return {"root": root, "work": work, "marker": work / "state" / "tier0.live", "calls": calls}


def _stopped(marker: Path) -> Path:
    path = clean_stop_path(marker)
    path.write_text(json.dumps(clean_stop_record(stopped_at_ms=1_762_050_000_000, pid=42)), encoding="utf-8")
    return path


def test_clean_stop_path_rule_matches_worker_gate(bs, tmp_path):
    marker = tmp_path / "state" / "tier0.live"
    assert bs.clean_stop_path(marker) == clean_stop_path(marker)
    assert bs.CLEAN_STOP_PHASE == clean_stop_record(stopped_at_ms=1, pid=1)["phase"]


@pytest.mark.parametrize("case", ["no_record", "marker_left", "broken"])
def test_after_live_refuses_without_clean_stop(bs, lobby, case):
    if case == "marker_left":  # 관제 중이거나 비정상 종료(표시 파일이 남음)
        _stopped(lobby["marker"])
        lobby["marker"].write_text("{}", encoding="utf-8")
    elif case == "broken":
        clean_stop_path(lobby["marker"]).write_text("{not json", encoding="utf-8")
    assert bs.main(["after-live", "--worker-token-file", "t"]) == bs.EXIT_BUSY
    assert lobby["calls"] == []


def test_after_live_runs_worker_only_in_scene_mode_with_lobby_state(bs, lobby):
    _stopped(lobby["marker"])
    assert bs.main(["after-live", "--worker-token-file", "/root/.simai_worker_token", "--", "--wait-ms", "5000"]) == 0
    [call] = lobby["calls"]
    assert call["rest"] == ["--worker-token-file", "/root/.simai_worker_token", "--",
                            sys.executable, "-m", "services.worker", "--wait-ms", "5000"]
    env = call["env"]
    assert env["SIMAI_TIER0_MARKER"] == str(lobby["marker"])
    assert env["SIMAI_STATE_DIR"] == str(lobby["work"] / "state")
    assert env["SIMAI_AUTO_STATIC_RECONSTRUCT"] == "1" and env["SIMAI_AUTO_STATIC_EXIT_WHEN_DONE"] == "1"
    assert "SIMAI_WORKER_DEV_NO_LIVE" not in env  # 개발 플래그가 아니라 정상 종료 기록으로 판단한다
    assert "SIMAI_WORKER_TORCH_MEMORY_FRACTION" not in env  # vLLM 이 없으니 분석 워커 예산 전체


def test_after_live_usage_errors(bs, lobby, monkeypatch):
    _stopped(lobby["marker"])
    assert bs.main(["after-live", "--", "--once"]) == bs.EXIT_USAGE  # 한 번만 처리하면 관제 뒤 대기 시간을 못 채운다
    monkeypatch.delenv("SIMAI_EC2_BASE_URL")
    assert bs.main(["after-live"]) == bs.EXIT_USAGE
    assert lobby["calls"] == []


def test_supervisor_start_tier0_clears_previous_clean_stop(bs, tmp_path, monkeypatch):
    world = World()
    sup = _sup(bs, tmp_path, monkeypatch, world)
    sup.marker.parent.mkdir(parents=True, exist_ok=True)
    record = _stopped(sup.marker)
    sup.start_tier0()
    assert not record.exists() and world.spawned == ["tier0"]
