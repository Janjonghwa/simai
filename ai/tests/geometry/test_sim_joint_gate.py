"""시뮬레이터 고정 시나리오에서 관절 유효성 게이트가 회귀하지 않는지 본다 (docs/design/tier0-joint-gate.md).

SIM 입력의 상한 성능이다(is_upper_bound). 허용 오차는 회귀를 잡기 위한 것이고 G2 합격 기준이나 제품 성능이 아니다.
측정은 tests/geometry/sim_joint_gate.py 가 하고, 이 시험은 그 실행 명령을 그대로 돌려 JSON 을 읽는다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).with_name("sim_joint_gate.py")

# 허용 오차 (미터). 설계 문서 "허용 오차" 표와 같은 값이다.
TOLERANCES = {
    "clean": {"valid_smooth_max": 0.01, "head_z_p95": 0.02},
    "noisy": {"valid_smooth_max": 0.06, "head_z_p95": 0.03},
}
MIN_VALID_RATIO = 0.95


@pytest.fixture(scope="module")
def results() -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        check=False,
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _common(r):
    assert r["input_provenance"]["is_upper_bound"] is True
    assert r["input_provenance"]["frames"]["source"] == "SIM"
    assert r["fabricated_joints"] == 0
    # D-G2-06 을 게이트 플래그가 아니라 정답 투영으로 따로 센다: 정답 기준 경쟁 후보가 있는 2대 관절의 VALID 0건
    assert r["truth_competitor"]["valid"] == 0


@pytest.mark.parametrize("scenario", ["basic", "podium", "crossing"])
@pytest.mark.parametrize("variant", ["clean", "noisy"])
def test_gate_on_four_cameras(results, scenario, variant):
    r = results[f"{scenario}/{variant}"]
    tol = TOLERANCES[variant]
    _common(r)
    assert r["valid_ratio"] >= MIN_VALID_RATIO
    assert r["valid_ratio"] <= r["geometry_valid_ratio"]  # 게이트는 등급을 낮추기만 한다
    assert r["valid_error_m"]["max_smooth"] <= tol["valid_smooth_max"]
    assert r["head_z_error_m"]["p95"] <= tol["head_z_p95"]


def test_two_camera_crossing_marks_competitors(results):
    """cam1·cam2 두 대만: 엇갈리는 두 사람의 같은 관절이 에피폴라선 위에 오면 VALID 로 두지 않는다 (D-G2-06)."""
    r = results["crossing/noisy/2cam"]
    _common(r)
    assert r["uncertain_reasons"].get("TWO_VIEW_COMPETITOR", 0) > 0
    assert r["identity_uncertain_ticks"] > 0
    assert r["truth_competitor"]["two_view_joints"] > 0
    assert r["valid_ratio"] < r["geometry_valid_ratio"]
    assert r["valid_error_m"]["max_smooth"] <= TOLERANCES["noisy"]["valid_smooth_max"]
