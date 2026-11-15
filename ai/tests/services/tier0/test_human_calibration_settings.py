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


def test_config_overrides_name_file_values_the_environment_disables(tmp_path):
    """설정 파일이 명시한 값을 환경 변수가 무효로 만들면 말한다(S15P31A101-747, 18차 감사: live_calibration.enabled=true 인데 효과 없음)."""
    config = tmp_path / "tier0.json"
    doc = {"input": {"mode": "sim", "run_dir": "."}, "live_calibration": {"enabled": True}, "drift_watch_enabled": True,
           "process_split": False}
    config.write_text(json.dumps(doc), encoding="utf-8")
    off = load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "0", "SIMAI_TIER0_PROCESS_SPLIT": "1"}).config_overrides
    assert any("live_calibration.enabled=true" in t and "HUMAN_CALIBRATION=0" in t for t in off)
    assert any("drift_watch_enabled=true" in t for t in off)
    assert any("process_split=false" in t and "PROCESS_SPLIT=1" in t for t in off)
    assert load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "1"}).config_overrides == ()
    # 설정 파일이 말하지 않은 기본값은 덮은 것이 아니다
    config.write_text(json.dumps({"input": {"mode": "sim", "run_dir": "."}}), encoding="utf-8")
    assert load_settings(config, env={}).config_overrides == ()


def test_intended_default_off_calibration_overrides_are_info_not_warnings(tmp_path):
    """사람 관절 보정 기본 꺼짐은 의도된 설정이다(S15P31A101-756): 설정 무효 WARNING 대상(config_overrides)에는 남지만
    status 에서는 경고(disabled_features)가 아니라 정보(info_notes)로 나간다. 다른 덮어쓰기는 경고로 남는다."""
    from types import SimpleNamespace

    from services.tier0.service import Tier0Service

    config = tmp_path / "tier0.json"
    doc = {"input": {"mode": "sim", "run_dir": "."}, "live_calibration": {"enabled": True}, "drift_watch_enabled": True,
           "process_split": False}
    config.write_text(json.dumps(doc), encoding="utf-8")
    s = load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "0", "SIMAI_TIER0_PROCESS_SPLIT": "1"})
    assert len(s.config_info) == 2 and all(t in s.config_overrides for t in s.config_info)
    assert any("process_split=false" in t for t in s.config_overrides) and not any("process_split" in t for t in s.config_info)
    fake = SimpleNamespace(settings=s, reid_collector=SimpleNamespace(state="disabled", reason="FileNotFoundError: cope"))
    warnings = Tier0Service._disabled_features(fake)
    assert not any("live_calibration" in t or "drift_watch" in t for t in warnings)
    assert any("process_split=false" in t for t in warnings) and any(t.startswith("Re-ID 꺼짐") for t in warnings)
