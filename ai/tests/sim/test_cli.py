"""실행 명령과 시나리오 검사."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[2]
BASIC = json.loads((AI_DIR / "sim" / "scenarios" / "basic.json").read_text(encoding="utf-8"))


def test_list_builtin():
    result = subprocess.run(
        [sys.executable, "-m", "sim.run", "--list"],
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    assert result.returncode == 0
    assert result.stdout.split() == ["basic", "crossing", "podium"]


def test_refuses_non_empty_out(run_sim, tmp_path):
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    result = run_sim("basic", tmp_path)
    assert result.returncode == 2
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "calibration.json").exists()


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        pytest.param({"unknown_key": 1}, "모르는 키", id="unknown-key"),
        pytest.param({"camera_defaults": {"capture_jitter_ms": 20}}, "프레임 간격의 절반", id="jitter-too-large"),
        pytest.param({"camera_defaults": {"hfov_rad": 105}}, "라디안", id="hfov-in-degrees"),
        pytest.param(
            {"people": [{"id": "p", "waypoints": [{"t_ms": 0, "xy": [20.0, 1.0]}]}]}, "교실 밖", id="outside-room"
        ),
    ],
)
def test_invalid_scenario_fails(run_sim, write_scenario, tmp_path, patch, message):
    doc = dict(BASIC)
    for key, value in patch.items():
        doc[key] = dict(doc[key], **value) if isinstance(value, dict) and key in doc else value
    result = run_sim(write_scenario(doc), tmp_path / "out")
    assert result.returncode == 2
    assert message in result.stderr
    assert not (tmp_path / "out").exists()


def test_scenario_files_are_small():
    """시나리오 정의만 커밋한다. 큰 출력은 sim/runs/ 등 커밋하지 않는 곳에 둔다."""
    for path in (AI_DIR / "sim" / "scenarios").glob("*.json"):
        assert path.stat().st_size < 16 * 1024, path.name
