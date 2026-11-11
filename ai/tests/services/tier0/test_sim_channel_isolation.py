"""SIM 입력의 실제 알림 채널 격리 (S15P31A101-599).

SIM 입력은 합성 장면인데 판정 출처가 LIVE 다(로컬 e2e·G3 시험이 노트북 시험 상대·EC2 흉내로 LIVE 경로를 쓴다).
운영 .env(EC2 I1 주소, 제어 채널 기본 0.0.0.0)가 남은 채 SIM 을 띄우면 합성 이벤트가 노트북 현장 경보·EC2·워치로
LIVE 로 나간다(AGENTS 규칙 5 위반). 그래서 SIM 은 I1 주소와 제어 채널 듣는 주소가 루프백일 때만 연다.

- SIM + 운영형 주소(EC2 도메인, 0.0.0.0·사설 IP 듣기) → 시작하지 않는다(Tier0ConfigError, 진입점 종료 코드 2)
- SIM + 루프백 → 기존처럼 I1·제어 채널을 연다(test_tier0_e2e 의 시험 상대 경로)
- file 입력은 같은 운영 env 에서도 두 채널을 열지 않고, live 입력은 그대로 연다(동작 불변)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services.tier0.settings import Tier0ConfigError, _is_loopback, load_settings

TOKEN = "t0k3n-value-for-599-test"
CONTROL = {
    "SIMAI_TIER0_CONTROL_PORT": "70000",
    "RUNPOD_TCP_PORT_70000": "40100",
    "SIMAI_TIER0_TLS_CERT_FILE": "cert.pem",
    "SIMAI_TIER0_TLS_KEY_FILE": "key.pem",
    "SIMAI_LAPTOP_TOKEN_SHA256": "sha256:" + "0" * 64,
}
# 운영 .env 모양: EC2 공개 주소 + 제어 채널(듣는 주소 기본 0.0.0.0)
PROD_ENV = {"SIMAI_EC2_BASE_URL": "https://simai.live", "SIMAI_TIER0_TOKEN": TOKEN, **CONTROL}


def _config(tmp: Path, input_doc: dict) -> Path:
    cfg = tmp / "tier0.json"
    cfg.write_text(json.dumps({"state_dir": str(tmp / "state"), "calibration_file": "cal.json", "input": input_doc}),
                   encoding="utf-8")
    return cfg


def _sim(tmp: Path) -> Path:
    return _config(tmp, {"mode": "sim", "run_dir": "run"})


@pytest.mark.parametrize("host, loop", [
    ("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("[::1]", True), ("localhost", True),
    ("LOCALHOST.", True), ("0.0.0.0", False), ("::", False), ("10.0.0.5", False), ("simai.live", False),
    ("localhost.simai.live", False), ("", False), (None, False),
])
def test_is_loopback(host, loop) -> None:
    assert _is_loopback(host) is loop


def test_sim_with_production_env_refuses_to_start(tmp_path) -> None:
    with pytest.raises(Tier0ConfigError) as e:
        load_settings(_sim(tmp_path), env=PROD_ENV)
    msg = str(e.value)
    assert "SIM" in msg and "I1 주소" in msg and "SIMAI_TIER0_CONTROL_LISTEN_HOST=0.0.0.0" in msg
    assert TOKEN not in msg  # 비밀 값은 오류에 싣지 않는다


@pytest.mark.parametrize("env", [
    {"SIMAI_EC2_I1_URL": "wss://simai.live/ws/tier0", "SIMAI_TIER0_TOKEN": TOKEN},                      # I1 만
    {"SIMAI_EC2_I1_URL": "wss://10.0.0.5:8443/ws/tier0", "SIMAI_TIER0_TOKEN": TOKEN},                   # 사설 IP 도 거부
    {**CONTROL},                                                                                         # 제어만, 기본 0.0.0.0
    {**CONTROL, "SIMAI_TIER0_CONTROL_LISTEN_HOST": "192.168.0.7"},
    {**CONTROL, "SIMAI_TIER0_CONTROL_LISTEN_HOST": "127.0.0.1",                                          # 제어는 루프백,
     "SIMAI_EC2_I1_URL": "wss://simai.live/ws/tier0", "SIMAI_TIER0_TOKEN": TOKEN},                       # I1 은 운영
    {"SIMAI_EC2_BASE_URL": "https://127.0.0.1:8443", "SIMAI_TIER0_TOKEN": TOKEN, **CONTROL},             # I1 루프백, 제어 0.0.0.0
])
def test_sim_refuses_any_non_loopback_channel(tmp_path, env) -> None:
    with pytest.raises(Tier0ConfigError, match="루프백"):
        load_settings(_sim(tmp_path), env=env)


@pytest.mark.parametrize("i1_url, listen", [
    ("wss://127.0.0.1:18443/ws/tier0", "127.0.0.1"),
    ("wss://localhost:18443/ws/tier0", "localhost"),
    ("wss://[::1]:18443/ws/tier0", "::1"),
])
def test_sim_with_loopback_channels_keeps_e2e_path(tmp_path, i1_url, listen) -> None:
    env = {"SIMAI_EC2_I1_URL": i1_url, "SIMAI_TIER0_TOKEN": TOKEN, **CONTROL, "SIMAI_TIER0_CONTROL_LISTEN_HOST": listen}
    s = load_settings(_sim(tmp_path), env=env)
    assert s.input_mode == "sim" and s.i1 is not None and s.i1.url == i1_url
    assert s.control is not None and s.control.host == listen and s.control.port == 40100


def test_sim_without_channels_still_runs_offline(tmp_path) -> None:
    s = load_settings(_sim(tmp_path), env={})
    assert s.i1 is None and s.control is None


def test_file_input_ignores_production_channels(tmp_path) -> None:
    s = load_settings(_config(tmp_path, {"mode": "file", "session_dir": "rec"}), env=PROD_ENV)
    assert s.input_mode == "file" and s.i1 is None and s.control is None


def test_live_input_keeps_production_channels(tmp_path) -> None:
    s = load_settings(_config(tmp_path, {"mode": "live"}), env=PROD_ENV)
    assert s.input_mode == "live" and s.i1 is not None and s.i1.url == "wss://simai.live/ws/tier0"
    assert s.control is not None and s.control.host == "0.0.0.0"


def test_entry_exits_2_for_sim_with_production_env(tmp_path) -> None:
    """진입점은 설정 오류(종료 코드 2)로 끝나고 I1 접속·제어 채널을 열기 전에 멈춘다."""
    cfg = _sim(tmp_path)
    drop = ("SIMAI_EC2_I1_URL", "SIMAI_EC2_BASE_URL", "SIMAI_TIER0_CONTROL_LISTEN_HOST")
    env = {k: v for k, v in os.environ.items() if k not in drop} | {"PYTHONUTF8": "1", **PROD_ENV}
    result = subprocess.run([sys.executable, "-m", "services.tier0", "--config", str(cfg)], cwd=AI_DIR,
                            capture_output=True, text=True, encoding="utf-8", env=env, timeout=120, check=False)
    assert result.returncode == 2, result.stderr
    assert "설정 오류" in result.stderr and "루프백" in result.stderr
