"""사람 관절 보정의 환경 변수 기본값과 입력 검증."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.tier0.settings import Tier0ConfigError, load_settings


def test_human_calibration_settings(tmp_path):
    config = tmp_path / "tier0.json"
    config.write_text(json.dumps({"input": {"mode": "sim", "run_dir": "."}}), encoding="utf-8")
    assert not load_settings(config, env={}).human_calibration_enabled
    assert not load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "0"}).human_calibration_enabled
    assert load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "1"}).human_calibration_enabled
    with pytest.raises(Tier0ConfigError, match="SIMAI_TIER0_HUMAN_CALIBRATION"):
        load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "yes"})
