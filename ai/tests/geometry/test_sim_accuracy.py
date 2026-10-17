"""시뮬레이터 고정 시나리오 관절 위치 오차가 허용 오차 안인지 본다 (docs/design/tier0-triangulation.md).

SIM 입력의 상한 성능이다(is_upper_bound). 허용 오차는 회귀를 잡기 위한 것이고 G2 합격 기준이 아니다.
측정은 tests/geometry/sim_accuracy.py 가 하고, 이 시험은 그 실행 명령을 그대로 돌려 JSON 을 읽는다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).with_name("sim_accuracy.py")

# 허용 오차 (미터). 설계 문서 "허용 오차" 표와 같은 값이다.
TOLERANCES = {
    "clean": {"joint_p95": 0.005, "valid_smooth_max": 0.01, "z_p95": 0.005, "pelvis_xy_p95": 0.005},
    "noisy": {"joint_p95": 0.03, "valid_smooth_max": 0.06, "z_p95": 0.02, "pelvis_xy_p95": 0.02},
}
MIN_ESTIMATED_RATIO = 0.95


def _run(*args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", *args],
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def results() -> dict:
    return _run()


@pytest.mark.parametrize("scenario", ["basic", "podium", "crossing"])
@pytest.mark.parametrize("variant", ["clean", "noisy"])
def test_joint_error_within_tolerance(results, scenario, variant):
    r = results[f"{scenario}/{variant}"]
    tol = TOLERANCES[variant]
    assert r["input_provenance"]["is_upper_bound"] is True
    assert r["input_provenance"]["frames"]["source"] == "SIM"
    assert r["input_provenance"]["calibration"]["source"] == "SIM"
    assert r["fabricated_joints"] == 0
    assert r["estimated_ratio"] >= MIN_ESTIMATED_RATIO
    assert r["joint_error_m"]["p95"] <= tol["joint_p95"]
    assert r["valid_joint_error_m"]["max_smooth"] <= tol["valid_smooth_max"]
    assert r["joint_z_error_m"]["p95"] <= tol["z_p95"]
    assert r["pelvis_xy_error_m"]["p95"] <= tol["pelvis_xy_p95"]


def test_interpolation_beats_nearest_frame():
    r = _run("--scenario", "basic", "--baseline")
    for variant in ("clean", "noisy"):
        assert r[f"basic/{variant}"]["joint_error_m"]["p95"] < r[f"basic/{variant}/nearest"]["joint_error_m"]["p95"]
