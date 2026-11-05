"""분석 워커를 Tier 0 뒤에 띄우는 스크립트(deploy/scripts/wait_tier0_ready.py) 시험 (S15P31A101-183, -60 deploy 순서).

반례: 표시 파일 없음(엔진 준비 중)·기동 중(phase=starting)·선확보 PARTIAL·오래됨·깨짐·gpu_priority 없음은 모두 '준비 안 됨'.
시간 안에 준비되지 않으면 명령을 띄우지 않는다(코드 3). --no-tier0 인데 표시 파일이 있으면 거부(코드 4).
준비되면 명령을 띄우고, --env-file 의 비밀 값은 명령 환경에만 들어가고 스크립트 출력에는 나오지 않는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "scripts" / "wait_tier0_ready.py"


def marker(path: Path, phase: str = "running", state: str = "OK", *, section: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"status": {}}
    if section:
        doc["gpu_priority"] = {"version": 1, "phase": phase, "reserve": {"state": state, "reserved_mb": 8192.0}}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_unknown_or_unready_states_are_not_ready(wt, tmp_path):
    m = tmp_path / "state" / "tier0.live"
    assert wt.readiness(m, {"OK"})[0] is False  # 엔진 준비 중(표시 파일 없음)
    for phase, state in (("starting", "PENDING"), ("running", "PARTIAL"), ("running", "LOST"), ("start_failed", "OK")):
        marker(m, phase, state)
        assert wt.readiness(m, {"OK"})[0] is False, (phase, state)
    marker(m, section=False)
    assert wt.readiness(m, {"OK"})[0] is False
    m.write_text("{깨짐", encoding="utf-8")
    assert wt.readiness(m, {"OK"})[0] is False
    marker(m)
    assert wt.readiness(m, {"OK"})[0] is True
    old = time.time() - 60
    os.utime(m, (old, old))
    assert wt.readiness(m, {"OK"})[0] is False  # 죽은 Tier 0 의 표시 파일


def run(*args: str, env: dict | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env={**os.environ, "PYTHONUTF8": "1", **(env or {})})


def test_timeout_never_starts_the_worker(tmp_path):
    m = marker(tmp_path / "tier0.live", "starting", "PENDING")
    flag = tmp_path / "started"
    r = run("--marker", str(m), "--timeout-s", "2", "--poll-s", "0.2", "--",
            sys.executable, "-c", f"open(r'{flag}', 'w').write('x')")
    assert r.returncode == 3 and not flag.exists()
    assert "phase='starting'" in r.stderr


def test_starts_after_tier0_becomes_ready_and_keeps_secret_out_of_output(tmp_path):
    m = tmp_path / "tier0.live"
    env_file = tmp_path / "worker.env"
    env_file.write_text("SIMAI_WORKER_TOKEN=worker-secret-xyz-123\n# 주석\n", encoding="utf-8")
    later = threading.Timer(1.5, lambda: marker(m))  # 엔진 준비가 끝나 Tier 0 가 running·OK 를 쓴다
    later.start()
    try:
        r = run("--marker", str(m), "--timeout-s", "30", "--poll-s", "0.2", "--env-file", str(env_file), "--",
                sys.executable, "-c", "import os; print('worker', len(os.environ['SIMAI_WORKER_TOKEN']))")
    finally:
        later.cancel()
    assert r.returncode == 0, r.stderr
    assert "worker 21" in r.stdout  # 명령 환경에는 들어갔다
    assert "worker-secret-xyz-123" not in r.stderr and "worker-secret-xyz-123" not in r.stdout
    assert "표시 파일 없음" in r.stderr


def test_no_tier0_mode_refuses_when_tier0_marker_exists(tmp_path):
    m = marker(tmp_path / "tier0.live")
    r = run("--no-tier0", "--marker", str(m), "--", sys.executable, "-c", "print('should not run')")
    assert r.returncode == 4 and "should not run" not in r.stdout
    m.unlink()
    r = run("--no-tier0", "--marker", str(m), "--", sys.executable, "-c", "print('dev run')")
    assert r.returncode == 0 and "dev run" in r.stdout


def test_changed_worker_fraction_is_rejected_before_start(tmp_path):
    settings = tmp_path / "settings"
    settings.write_text("SIMAI_WORKER_TORCH_MEMORY_FRACTION=0.20\n", encoding="utf-8")
    r = run("--no-tier0", "--marker", str(tmp_path / "missing"), "--env-file", str(settings), "--",
            sys.executable, "-c", "print('should not run')",
            env={"SIMAI_VALIDATED_WORKER_FRACTION": "0.10", "SIMAI_WORKER_TORCH_MEMORY_FRACTION": "0.10"})
    assert r.returncode == 2 and "should not run" not in r.stdout
    assert "검증한 값과 다르다" in r.stderr
