"""GPU 큐 시험 공용 fixture.

큐는 Pod 에서 `python gpu_queue.py run jobs.json` 으로 도는 스크립트라 시험도 같은 명령을 그대로 돌리고
상태 파일·Run 기록을 읽어 검사한다. GPU·RunPod 는 쓰지 않는다(Job 은 CPU 명령, nvidia-smi 는 없거나 대역).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

AI_DIR = Path(__file__).resolve().parents[3]
REPO = AI_DIR.parent
CONTRACTS = REPO / "contracts"
QUEUE = AI_DIR / "experiments" / "gpu_queue" / "gpu_queue.py"
HERE = Path(__file__).resolve().parent
CPU_JOB = HERE / "cpu_job.py"
FAKE_SMI = HERE / "fake_nvidia_smi.py"

PROVENANCE = {"frames": {"source": "SIM", "dataset_id": "sim/basic"}, "is_upper_bound": True}


def py(*args: object) -> list[str]:
    return [sys.executable, str(CPU_JOB), *map(str, args)]


def job(name: str, command: list[str] | str, **kw) -> dict:
    return {"name": name, "command": command, "expected_s": 1, "timeout_s": 30, "vram_mb": 0, **kw}


class QueueEnv:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state = tmp / "state"
        self.out = tmp / "out.txt"
        self.live = tmp / "tier0.live"

    def write(self, jobs: list[dict], name: str = "t", record: dict | None = None, **queue) -> Path:
        q = {
            "name": name,
            "workdir": str(self.tmp),
            "poll_s": 0.1,
            "kill_grace_s": 1,
            "lock": {"heartbeat_s": 0.3, "ttl_s": 5},
            "live_guard": {"marker_files": [str(self.live)], "process_patterns": [], "poll_s": 0.2, "max_wait_s": 30},
            "gpu_query_cmd": [str(self.tmp / "no-nvidia-smi")],
            "vram_wait_s": 0.3,
        }
        q.update(queue)
        rec = {
            "experiment_id": "INT-01",
            "jira_key": "S15P31A101-36",
            "owner": "queue-test",
            "experiment_state": "PLAN",
            "model_version": "cpu-sample",
            "input_provenance": PROVENANCE,
            "gpu": {"provider": "LOCAL"},
        }
        if record is not None:
            rec = record
        path = self.tmp / f"jobs_{name}.json"
        path.write_text(json.dumps({"queue": q, "record": rec, "jobs": jobs}, ensure_ascii=False), encoding="utf-8")
        return path

    def args(self, *args: object) -> list[str]:
        return [sys.executable, str(QUEUE), "--state-dir", str(self.state), *map(str, args)]

    def run(self, *args: object, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self.args(*args), capture_output=True, text=True, encoding="utf-8", env=_env(), timeout=timeout)

    def start(self, *args: object) -> subprocess.Popen:
        return subprocess.Popen(self.args(*args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=_env())

    def state_of(self, name: str = "t") -> dict:
        path = self.state / name / "state.json"
        for _ in range(20):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.05)
        raise AssertionError(f"상태 파일을 읽을 수 없음: {path}")

    def wait_running(self, job_name: str, name: str = "t", sleeping: Path | None = None, timeout: float = 20) -> dict:
        """Job 이 실행 중 상태가 될 때까지 기다린다. sleeping 을 주면 cpu_job slow-if 가 잠들 때까지 기다린다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sleeping is not None and not Path(f"{sleeping}.sleeping").is_file():
                pass
            elif (self.state / name / "state.json").is_file():
                st = self.state_of(name)["jobs"][job_name]
                if st["status"] == "running" and st.get("pid"):
                    return st
            time.sleep(0.1)
        raise AssertionError(f"{job_name} 가 시작되지 않음")

    def records(self, name: str = "t") -> list[dict]:
        paths = sorted((self.state / name / "runs").glob("*/run_record.json"))
        return sorted((json.loads(p.read_text(encoding="utf-8")) for p in paths), key=lambda r: r["started_at"])

    def lines(self) -> list[str]:
        return self.out.read_text(encoding="utf-8").splitlines() if self.out.is_file() else []


def _env() -> dict[str, str]:
    env = dict(os.environ, PYTHONUTF8="1")
    env.pop("RUNPOD_POD_ID", None)
    return env


@pytest.fixture
def qenv(tmp_path) -> QueueEnv:
    return QueueEnv(tmp_path)


@pytest.fixture(scope="session")
def run_record_errors():
    registry = Registry()
    for p in sorted(CONTRACTS.rglob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        registry = registry.with_resource(s["$id"], Resource.from_contents(s))
    schema = json.loads((CONTRACTS / "experiments" / "run_record.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, registry=registry)
    return lambda doc: [e.message for e in validator.iter_errors(doc)]


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # 좀비(부모가 아직 거두지 않음)는 끝난 것으로 본다
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def wait_dead(pid: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


# 시험 파일은 conftest 를 import 하지 않는다(--import-mode=importlib). 도우미는 qenv 로 꺼내 쓴다.
QueueEnv.py = staticmethod(py)
QueueEnv.job = staticmethod(job)
QueueEnv.pid_alive = staticmethod(pid_alive)
QueueEnv.wait_dead = staticmethod(wait_dead)
QueueEnv.FAKE_SMI = FAKE_SMI
QueueEnv.REPO = REPO
