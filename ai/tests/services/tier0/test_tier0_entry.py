"""Tier 0 진입점·설정·어댑터 (S15P31A101-171): 종료 코드, 환경 변수, I1 경로 설정, 봉투 msg_id, 검출기 어댑터, 기준선 저장."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services.tier0.i1_client import Envelope, I1Settings  # noqa: E402
from services.tier0.service import baselines_from_json, baselines_to_json, load_baselines, save_baselines  # noqa: E402
from services.tier0.settings import Tier0ConfigError, load_settings  # noqa: E402
from simai_ai.calibration.monitor import BaselineRecord  # noqa: E402
from simai_ai.risk import FixedClock  # noqa: E402


def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    import os

    return subprocess.run([sys.executable, "-m", "services.tier0", *args], cwd=AI_DIR, capture_output=True, text=True,
                          encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1", **(env or {})}, timeout=120)


def test_bad_config_exits_2(tmp_path) -> None:
    cfg = tmp_path / "bad.json"
    cfg.write_text(json.dumps({"input": {"mode": "warp"}}), encoding="utf-8")
    result = _run(["--config", str(cfg)])
    assert result.returncode == 2 and "설정 오류" in result.stderr


def test_live_without_engine_exits_4_without_judging(tmp_path) -> None:
    cal = AI_DIR.parent / "contracts" / "examples" / "calibration.json"
    cfg = tmp_path / "live.json"
    cfg.write_text(json.dumps({"state_dir": str(tmp_path / "state"), "calibration_file": str(cal),
                               "input": {"mode": "live", "camera_ids": ["cam1"]}}), encoding="utf-8")
    result = _run(["--config", str(cfg)], env={"SIMAI_TIER0_ENGINE": str(tmp_path / "none.engine")})
    assert result.returncode == 4, result.stderr
    assert "SIMAI_TIER0_ENGINE" in result.stderr


def test_settings_read_env_and_refuse_partial_control(tmp_path) -> None:
    run = tmp_path / "run"
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"input": {"mode": "sim", "run_dir": "run"}}), encoding="utf-8")
    s = load_settings(cfg, env={"SIMAI_ROOT": str(tmp_path / "root"), "SIMAI_TIER0_ENGINE": "/x/y.engine"})
    assert s.state_dir == tmp_path / "root" / "state" and s.sim.run_dir == run
    assert s.calibration_file == run / "calibration.json" and s.engine_path == "/x/y.engine"
    assert s.i1 is None and s.control is None  # EC2·노트북 없이(오프라인)
    with pytest.raises(Tier0ConfigError) as e:
        load_settings(cfg, env={"SIMAI_TIER0_CONTROL_PORT": "70000"})
    assert "RUNPOD_TCP_PORT_70000" in str(e.value) and "SIMAI_LAPTOP_TOKEN_SHA256" in str(e.value)


def test_i1_path_is_configurable_and_gateway_id_is_shared(tmp_path) -> None:
    env = {"SIMAI_EC2_BASE_URL": "https://ec2.example.com", "SIMAI_TIER0_TOKEN": "t0k3n-value",
           "SIMAI_EC2_I1_PATH": "/ws/tier0-v2"}
    assert I1Settings.from_env(env).url == "wss://ec2.example.com/ws/tier0-v2"
    assert I1Settings.from_env({k: v for k, v in env.items() if k != "SIMAI_EC2_I1_PATH"}).url.endswith("/ws/tier0")
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"gateway_id": "tier0-07", "input": {"mode": "sim", "run_dir": "r"}}), encoding="utf-8")
    # SIM 입력은 루프백 I1 만 연다(-599): EC2 흉내 주소로 gateway_id 공유를 본다
    s = load_settings(cfg, env={**env, "SIMAI_EC2_BASE_URL": "https://127.0.0.1:8443"})
    assert s.gateway_id == "tier0-07" and s.i1.source_id == "tier0-07"


def test_envelope_keeps_given_msg_id() -> None:
    env = Envelope("tier0-01", FixedClock(1_760_342_400_000))
    msg = env.make("world_state", {"tracks": []}, msg_id="0192a6f0-7c1e-7b3a-9f2d-000000000001")
    assert msg["msg_id"] == "0192a6f0-7c1e-7b3a-9f2d-000000000001"
    assert env.make("world_state", {"tracks": []})["msg_id"] != msg["msg_id"]


def test_calibration_baselines_round_trip_keeps_verification(tmp_path) -> None:
    """보정 감시 기준선(-117)은 저장·복원해도 검증 여부가 그대로다. 깨진 파일은 검증된 것으로 지어내지 않는다."""
    recs = {"cal-1": BaselineRecord("cal-1", {"cam1": 1.2, "cam2": 0.9}, ("cam1",), {"cam1": "observations"})}
    path = tmp_path / "b.json"
    save_baselines(path, recs)
    back = load_baselines(path)
    assert back == recs and baselines_from_json(baselines_to_json(recs)) == recs
    path.write_text("{broken", encoding="utf-8")
    assert load_baselines(path) == {}


def test_yolo_detector_adapter_on_real_weights(tmp_path) -> None:
    """실제 YOLO 경로. ultralytics(extras detection)와 가중치(SIMAI_TEST_YOLO_WEIGHTS)가 없으면 건너뛴다."""
    import os

    pytest.importorskip("ultralytics")
    weights = os.environ.get("SIMAI_TEST_YOLO_WEIGHTS")
    if not weights or not Path(weights).exists():
        pytest.skip("SIMAI_TEST_YOLO_WEIGHTS 가 없다")
    import numpy as np

    from services.tier0.yolo_detector import UltralyticsPoseDetector
    from simai_ai.providers.base import Frame

    det = UltralyticsPoseDetector(weights, imgsz=640, half=False, device="cpu")
    frame = Frame("cam1", 1_760_342_400_000, "SIM", {"stream_epoch": 0, "frame_sequence": 1},
                  image=np.zeros((480, 640, 3), np.uint8))
    [out] = det.detect([frame])
    assert out.camera_id == "cam1" and out.frame_sequence == 1 and out.detections == ()
