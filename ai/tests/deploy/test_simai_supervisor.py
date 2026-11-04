"""simai-bootstrap 감시 프로세스·가중치 이어받기·검증 스탬프 (S15P31A101-246). GPU·네트워크를 쓰지 않는다.

반례: Tier 0 준비 전에는 워커 가중치·vLLM 을 시작하지 않음, vLLM /health 전에는 워커를 띄우지 않음, 워커·vLLM 장애는 그룹 정리 후 백오프
재기동(Tier 0 에 신호 없음), 재기동도 Tier 0 가 준비 상태일 때만, 빨리 죽음이 이어지면 포기, vLLM 이 시한 안에 안 뜨면 그룹 SIGKILL,
워커 가중치 실패는 Tier 0 를 멈추지 않음, Tier 0 종료 → 워커·vLLM 내리고 같은 코드, 신호 → 워커 → vLLM → Tier 0 순서,
HF 토큰은 워커 가중치 첫 요청에만(자식 환경 없음), 48GB 미만 GPU 는 Tier 0 단독, SIMAI_SERVICES 오타는 받기 전에 64,
끊긴 받기는 Range 로 이어받고 Range 를 무시하는 원본·416·해시 불일치는 처음부터(깨진 파일을 잇지 않음), 다시 해도 같은 4xx 는 한 번만,
스탬프는 크기·mtime·sha256 이 맞을 때만 해시를 건너뜀(Tier 0 가중치는 늘 해시).
vLLM 기본값(S15P31A101-278): --max-model-len 32768(환경 값·EXTRA_ARGS 가 우선, 중복 없음), VLLM_USE_FLASHINFER_SAMPLER=0(환경 값 우선),
auto CPU 묶기는 min(affinity, cgroup 할당량 올림) 기준(v1·v2 파싱, 할당량 8 미만이면 안 묶음)이고 계산 결과를 시작 로그에 남김.
실제 프로세스 시험(POSIX, Linux 컨테이너에서 돈다)은 가짜 Tier 0·vLLM·워커(fake_service.py)로 기동·종료 순서, 손자 프로세스 정리, 토큰 비전달을 본다.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PY = Path(sys.executable).as_posix()
FAKE_SMI = (REPO / "ai" / "tests" / "experiments" / "gpu_queue" / "fake_nvidia_smi.py").as_posix()
FAKE_SERVICE = (Path(__file__).resolve().parent / "fake_service.py").as_posix()
TOKEN = "hf_" + "SupervisorTok" * 3
posix_only = pytest.mark.skipif(os.name == "nt", reason="프로세스 그룹·신호는 POSIX(Linux 컨테이너)에서만 돈다")


@pytest.fixture
def tmp_path(tmp_path):
    text = str(tmp_path)
    return Path(text[4:]) if text.startswith("\\\\?\\") else tmp_path


# ---------------------------------------------------------------- 가짜 프로세스 (모든 OS, 결정적)


class World:
    """가짜 시계·Tier 0 준비·vLLM /health 와 띄운 프로세스·보낸 신호 기록."""

    def __init__(self) -> None:
        self.now = 0.0
        self.ready = False
        self.healthy = False
        self.spawned: list[str] = []
        self.signals: list[tuple[str, int]] = []
        self.procs: dict[str, list[FakeProc]] = {}
        self.envs: dict[str, dict] = {}
        self.argvs: dict[str, list[str]] = {}
        self.fail_spawn: set[str] = set()

    def spawn(self, name, argv, env, log_dir):
        if name in self.fail_spawn:
            raise FileNotFoundError(argv[0])
        p = FakeProc(name, self)
        self.spawned.append(name)
        self.procs.setdefault(name, []).append(p)
        self.envs[name], self.argvs[name] = dict(env), list(argv)
        return p

    def last(self, name) -> FakeProc:
        return self.procs[name][-1]


class FakeProc:
    _pid = 1000

    def __init__(self, name: str, world: World) -> None:
        FakeProc._pid += 1
        self.name, self.world, self.pid, self.rc, self.nice = name, world, FakeProc._pid, None, 0
        self.ignore_term = False
        self.cpus = None

    def poll(self):
        return self.rc

    def signal(self, sig):
        self.world.signals.append((self.name, sig))
        if sig == signal.SIGTERM and not self.ignore_term and self.rc is None:
            self.rc = 0
        elif sig != signal.SIGTERM and self.rc is None:
            self.rc = -9

    def wait(self, timeout):
        return self.rc

    def renice(self, value):
        self.nice = value

    def pin(self, cpus):
        self.cpus = cpus


def _sup(bs, tmp_path, monkeypatch, world: World, services=("tier0", "vllm", "worker"), env: dict | None = None,
         weights_ok: bool = True, real_ready: bool = False, wall_ms=None):
    calls = []

    def fake_weights(root, entries):
        calls.append([e["name"] for e in entries])
        if not weights_ok:
            raise bs.BootstrapError(bs.EXIT_STORAGE, "vlm 받기 실패: URLError")
        return [{"name": e["name"], "status": "present"} for e in entries]

    manifest = tmp_path / "weights.json"
    manifest.write_text(json.dumps({"weights": [{"name": "m", "sets": ["vlm"], "path": "weights/m.bin", "source": {"url": "x"},
                                                 "sha256": "0" * 64}]}), encoding="utf-8")
    monkeypatch.setattr(bs, "ensure_weights", fake_weights)
    base = {"PATH": os.environ.get("PATH", ""), "SIMAI_WEIGHTS_MANIFEST": str(manifest), "SIMAI_STOP_GRACE_S": "1",
            "HF_TOKEN": TOKEN, "HUGGING_FACE_HUB_TOKEN": TOKEN, **(env or {})}
    ready = None if real_ready else (lambda: (world.ready, "준비" if world.ready else "표시 파일 없음"))
    sup = bs.Supervisor(tmp_path / "simai", list(services), ["simai-tier0", "--config", "c.json"], base,
                        spawn=world.spawn, ready=ready, health=lambda: world.healthy, clock=lambda: world.now,
                        sleep=lambda s: None, wall_ms=wall_ms)
    sup.weight_calls = calls
    return sup


def _settle(sup, world: World, steps: int = 3, dt: float = 1.0) -> None:
    for _ in range(steps):
        sup.step()
        if sup._weights is not None:
            sup._weights.join(5)
        world.now += dt


def _start(sup, world):
    sup.start_tier0()
    return sup.svc["tier0"].proc


def test_order_tier0_then_weights_then_vllm_then_worker_after_health(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w)
    _start(sup, w)
    _settle(sup, w, 5)
    assert w.spawned == ["tier0"] and sup.weight_calls == [] and sup.phase == "wait_tier0"  # 준비 전: 받지도 띄우지도 않는다
    w.ready = True
    _settle(sup, w, 3)
    assert sup.weight_calls == [["m"]] and w.spawned == ["tier0", "vllm"]  # 준비 뒤 가중치 → vLLM
    _settle(sup, w, 10)
    assert w.spawned == ["tier0", "vllm"]  # /health 전에는 워커를 띄우지 않는다
    w.healthy = True
    _settle(sup, w, 8)
    assert w.spawned == ["tier0", "vllm", "worker"]
    assert w.last("vllm").nice == 10 and w.last("worker").nice == 10 and w.last("tier0").nice == 0
    assert ("tier0", signal.SIGTERM) not in w.signals


def _marker(root: Path, written_at, pid, phase="running") -> None:
    m = root / "state" / "tier0.live"
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(json.dumps({"gpu_priority": {"phase": phase, "pid": pid, "written_at": written_at, "reserve": {"state": "OK"}}}),
                 encoding="utf-8")  # mtime 은 지금(15초 안): simai-wait-tier0 판정만으로는 준비로 보인다


def test_avatar_opt_in_weights_and_total_worker_budget(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, services=("tier0", "vllm", "worker", "avatar"),
               env={"SIMAI_WORKER_ENV_FILE": "/runtime/worker.env", "SIMAI_AVATAR_WEIGHTS_SET": "vlm"})
    _start(sup, w)
    assert w.envs["tier0"]["SIMAI_AVATAR_SHARE"] == "1"
    w.ready = w.healthy = True
    _settle(sup, w, 12)
    assert "avatar" in w.spawned and sup.weight_calls == [["m", "m"]]
    assert "services.avatar" in w.argvs["avatar"]
    assert w.argvs["avatar"][:3] == ["simai-wait-tier0", "--env-file", "/runtime/worker.env"]
    assert float(w.envs["worker"]["SIMAI_WORKER_TORCH_MEMORY_FRACTION"]) == pytest.approx(.05)
    assert float(w.envs["avatar"]["SIMAI_AVATAR_GPU_MEMORY_FRACTION"]) == pytest.approx(.10)
    w.signals.clear()
    sup._stop_sidecars()
    assert [n for n, s in w.signals if s == signal.SIGTERM] == ["avatar", "worker", "vllm"]


def test_avatar_budget_overflow_refused_before_spawn(bs, tmp_path, monkeypatch):
    w = World()
    with pytest.raises(bs.BootstrapError):
        _sup(bs, tmp_path, monkeypatch, w, services=("tier0", "vllm", "worker", "avatar"),
             env={"SIMAI_AVATAR_GPU_MEMORY_FRACTION": "0.20"})
    assert w.spawned == []


def test_avatar_weight_set_contains_hmr_model_rig_and_seg(bs):
    manifest = REPO / "deploy" / "weights.json"
    assert {w["name"] for w in bs.load_manifest(manifest, "avatar")} == {
        "sam-3d-body-dinov3", "sam-3d-body-dinov3-mhr", "yolo26x-seg"}
    assert bs.parse_services(None) == ["tier0"]
    assert bs.parse_services("tier0,avatar") == ["tier0", "avatar"]


def test_stale_running_marker_from_previous_run_is_not_ready_until_this_tier0_writes(bs, tmp_path, monkeypatch, capsys):
    """리뷰 -246 필수: 이전 실행이 비정상 종료로 남긴 running·OK 표시(mtime 새것)를 이번 Tier 0 의 준비로 보지 않는다.
    진입점이 엔진을 다시 만드는 동안(약 550초) vLLM·워커가 GPU 를 먼저 잡으면 안 된다. 표시 파일은 지우지 않는다(previous_unclean)."""
    root = tmp_path / "simai"
    _marker(root, 1_000, 4242)  # 이전 실행(pid 4242)이 남긴 표시
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, real_ready=True, wall_ms=lambda: 10_000)
    pid = _start(sup, w).pid
    w.healthy = True
    _settle(sup, w, 10)
    assert w.spawned == ["tier0"] and sup.weight_calls == [] and sup.phase == "wait_tier0"
    assert (root / "state" / "tier0.live").exists()  # 지우지 않았다
    _marker(root, 9_999, pid)  # 기동 시각 전에 쓴 것
    _settle(sup, w, 5)
    assert w.spawned == ["tier0"]
    _marker(root, 10_500, pid + 7)  # 다른 프로세스가 쓴 것
    _settle(sup, w, 5)
    assert w.spawned == ["tier0"]
    _marker(root, 10_500, pid, phase="starting")  # 이번 Tier 0 가 기동 중(엔진 준비 뒤 서비스 첫 단계)
    _settle(sup, w, 5)
    assert w.spawned == ["tier0"]
    assert "이번 Tier 0 기동 전의 표시 파일" in capsys.readouterr().err
    _marker(root, 10_500, pid)  # 이번 Tier 0 가 엔진 적재·선확보 뒤에 쓴 running·OK
    _settle(sup, w, 10)
    assert w.spawned[:2] == ["tier0", "vllm"] and sup.weight_calls == [["m"]]


@pytest.mark.parametrize("doc", [{"gpu_priority": {"phase": "running", "reserve": {"state": "OK"}}},  # written_at·pid 없음(모르면 준비 아님)
                                 {"gpu_priority": {"phase": "running", "pid": 1, "written_at": True, "reserve": {"state": "OK"}}}])
def test_marker_without_fresh_owner_fields_is_not_ready(bs, tmp_path, doc):
    m = tmp_path / "tier0.live"
    m.write_text(json.dumps(doc), encoding="utf-8")
    waiter = bs._load_waiter()
    assert bs.tier0_marker_ready(m, waiter, 0, 1)[0] is False
    assert bs.tier0_marker_ready(m, waiter, None, None)[0] is False  # 기동 시각을 모르면 준비 아님


def test_children_get_no_token_own_caches_thread_limits_and_vllm_budget_flags(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, env={"SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION": "0.5",
                                                  "SIMAI_WORKER_VLLM_MAX_NUM_SEQS": "2", "SIMAI_VLLM_PORT": "8123"})
    _start(sup, w)
    w.ready = w.healthy = True
    _settle(sup, w, 12)
    assert set(w.envs) == {"tier0", "vllm", "worker"}
    for name, env in w.envs.items():
        assert not {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"} & set(env), name
        assert TOKEN not in json.dumps(env)
        assert env["TRITON_CACHE_DIR"].endswith(os.path.join("simai-cache", name, "triton"))
        assert env["CUDA_CACHE_PATH"].endswith(os.path.join("simai-cache", name, "cuda"))
    assert len({e["TORCHINDUCTOR_CACHE_DIR"] for e in w.envs.values()}) == 3
    assert "OMP_NUM_THREADS" not in w.envs["tier0"] and w.envs["vllm"]["OMP_NUM_THREADS"] == w.envs["worker"]["MKL_NUM_THREADS"] == "1"
    assert w.envs["vllm"]["HF_HUB_OFFLINE"] == "1"
    a = w.argvs["vllm"]
    assert a[:2] == ["vllm", "serve"] and a[2].endswith(os.path.join("weights", "cosmos3-nano"))
    flags = dict(zip(a[3::2], a[4::2]))
    assert flags["--gpu-memory-utilization"] == "0.5" and flags["--max-num-seqs"] == "2" and flags["--host"] == "127.0.0.1"
    assert flags["--port"] == "8123" and "Cosmos3ReasonerForConditionalGeneration" in flags["--hf-overrides"]
    assert flags["--max-model-len"] == "32768"  # 모델 기본 262,144 는 0.55 의 KV 에 안 들어간다(3차 GPU 세션)
    assert w.envs["vllm"]["VLLM_USE_FLASHINFER_SAMPLER"] == "0" and w.envs["vllm"]["VLLM_USE_DEEP_GEMM"] == "0"
    assert "VLLM_USE_FLASHINFER_SAMPLER" not in w.envs["tier0"] and "VLLM_USE_FLASHINFER_SAMPLER" not in w.envs["worker"]
    assert w.argvs["worker"][-2:] == ["-m", "services.worker"]


def test_vllm_max_model_len_and_flashinfer_sampler_follow_env(bs, tmp_path, monkeypatch):
    sup = _sup(bs, tmp_path, monkeypatch, World(), env={"SIMAI_WORKER_VLLM_MAX_MODEL_LEN": "16384",
                                                        "VLLM_USE_FLASHINFER_SAMPLER": "1"})
    a = sup.svc["vllm"].argv
    assert a.count("--max-model-len") == 1 and a[a.index("--max-model-len") + 1] == "16384"
    assert sup.svc["vllm"].env["VLLM_USE_FLASHINFER_SAMPLER"] == "1"  # 사용자가 준 값이 우선


@pytest.mark.parametrize("extra", ["--max-model-len 8192", "--max-model-len=8192", "--max_model_len 8192 --enforce-eager"])
def test_vllm_extra_args_max_model_len_is_not_duplicated(bs, tmp_path, monkeypatch, extra):
    sup = _sup(bs, tmp_path, monkeypatch, World(), env={"SIMAI_VLLM_EXTRA_ARGS": extra, "SIMAI_WORKER_VLLM_MAX_MODEL_LEN": "16384"})
    a = sup.svc["vllm"].argv
    assert sum(x.split("=", 1)[0].replace("_", "-") == "--max-model-len" for x in a) == 1
    assert "16384" not in a and "32768" not in a and a[-len(extra.split()):] == extra.split()


@pytest.mark.parametrize("spec, avail, quota, want", [
    (None, list(range(16)), None, set(range(8, 16))),  # auto, 할당량 없음: 뒤쪽 절반(기존)
    ("auto", list(range(4)), None, None),  # 8개 미만은 나누지 않는다
    ("none", list(range(16)), None, None),
    ("4-5,9", list(range(8)), None, {4, 5}),  # 쓸 수 있는 것과 겹치는 것만
    ("auto", list(range(224)), 23.8, set(range(212, 224))),  # 3차 세션 Pod: affinity 224·할당량 23.8 → 올림 24 → 뒤쪽 12개
    ("auto", list(range(224)), 6.0, None),  # 할당량 6 → 8 미만이라 묶지 않음
    ("auto", list(range(224)), 7.2, set(range(220, 224))),  # 올림 8 → 4개
    ("auto", list(range(12)), 64.0, set(range(6, 12))),  # 할당량이 affinity 보다 크면 affinity 기준
    ("auto", [0, 2, 4, 6, 8, 10, 12, 14, 16, 18], 8.0, {12, 14, 16, 18}),  # affinity 목록의 뒤쪽(번호가 아니라 목록 순서)
    ("4-5", list(range(224)), 2.0, {4, 5}),  # 직접 준 목록은 할당량과 상관없이 그대로
])
def test_sidecar_cpu_affinity_rule(bs, spec, avail, quota, want):
    assert bs.sidecar_cpus(spec, avail, quota) == want


@pytest.mark.parametrize("files, expected", [
    # 3차 세션 Pod(cgroup v1 공동 마운트 cpu,cpuacct): 23.8 vCPU
    ({"/proc/self/cgroup": "4:cpu,cpuacct:/\n",
      "/proc/self/mountinfo": "35 25 0:30 / /sys/fs/cgroup/cpu,cpuacct rw - cgroup cgroup rw,cpu,cpuacct\n",
      "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "2380000\n", "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000\n"}, 23.8),
    ({"/proc/self/cgroup": "0::/\n", "/proc/self/mountinfo": "30 25 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n",
      "/sys/fs/cgroup/cpu.max": "2380000 100000\n"}, 23.8),  # v2
    ({"/proc/self/cgroup": "0::/\n", "/sys/fs/cgroup/cpu.max": "max 100000\n"}, None),  # v2 제한 없음
    ({"/proc/self/cgroup": "4:cpu,cpuacct:/\n", "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "-1",
      "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000"}, None),  # v1 제한 없음
    ({}, None),  # 못 읽음(Windows 등)
])
def test_cgroup_vcpu_parses_v1_and_v2(bs, files, expected):
    assert bs.cgroup_vcpu(files.get)[0] == expected


def test_sidecar_cpus_use_cgroup_quota_and_log_it(bs, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bs, "usable_cpus", lambda: list(range(224)))
    monkeypatch.setattr(bs, "cgroup_vcpu", lambda read=None: (23.8, "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us"))
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w)
    w.ready = w.healthy = True

    def ticking(_s):
        if sup._weights is not None:
            sup._weights.join(5)
        w.now += 1
        if w.now == 20:
            sup.request_stop(signal.SIGTERM)
    sup.sleep = ticking
    assert sup.run() == 0
    assert w.last("vllm").cpus == w.last("worker").cpus == set(range(212, 224)) and w.last("tier0").cpus is None
    err = capsys.readouterr().err
    assert "affinity 224개" in err and "23.8 vCPU" in err and "212-223 (12개)" in err


@pytest.mark.parametrize("spec", ["x-y", "20-30"])
def test_bad_sidecar_cpus_is_usage_error(bs, spec):
    with pytest.raises(bs.BootstrapError) as e:
        bs.sidecar_cpus(spec, list(range(8)))
    assert e.value.code == bs.EXIT_USAGE


def test_sidecars_are_pinned_tier0_is_not(bs, tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "usable_cpus", lambda: list(range(12)))
    monkeypatch.setattr(bs, "cgroup_vcpu", lambda read=None: (None, None))  # 시험 기계의 실제 cgroup 할당량에 기대지 않는다
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w)
    _start(sup, w)
    w.ready = w.healthy = True
    _settle(sup, w, 12)
    assert w.last("vllm").cpus == w.last("worker").cpus == set(range(6, 12)) and w.last("tier0").cpus is None


def test_worker_env_file_goes_through_wait_tier0_only_for_worker(bs, tmp_path, monkeypatch):
    priv = tmp_path / ".simai-private"
    priv.mkdir()
    (priv / "worker.env").write_text("SIMAI_WORKER_TOKEN=secret\n", encoding="utf-8")
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w)  # 기본 위치 $SIMAI_ROOT/../.simai-private/worker.env
    assert sup.svc["worker"].argv[:4] == ["simai-wait-tier0", "--env-file", str(priv / "worker.env"), "--"]
    assert "SIMAI_WORKER_TOKEN" not in sup.svc["worker"].env and "--env-file" not in sup.svc["vllm"].argv


def test_worker_args_slot_for_handler_config(bs, tmp_path, monkeypatch):
    """받을 작업 유형(VLM·hmr_refine)은 워커 설정 파일이 정한다: 감시자는 SIMAI_WORKER_ARGS 를 워커 명령 끝에 붙인다(조립은 -227)."""
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, env={"SIMAI_WORKER_ARGS": "--config /workspace/simai/worker.json"})
    assert sup.svc["worker"].argv[-4:] == ["-m", "services.worker", "--config", "/workspace/simai/worker.json"]
    assert "--config" not in sup.svc["vllm"].argv


def test_worker_crash_is_cleaned_and_restarted_with_backoff_tier0_untouched(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w)
    _start(sup, w)
    w.ready = w.healthy = True
    _settle(sup, w, 12)
    first = w.last("worker")
    first.rc = 3  # 워커가 죽는다
    _settle(sup, w, 1, dt=0.1)
    assert ("worker", bs.KILL) in w.signals  # 그룹 정리(손자까지)
    assert w.spawned.count("worker") == 1
    w.now += 6  # 백오프 5초 뒤
    _settle(sup, w, 1)
    assert w.spawned.count("worker") == 2
    assert not [s for s in w.signals if s[0] == "tier0"] and w.last("tier0").rc is None


def test_restart_waits_for_tier0_ready_marker(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w)
    _start(sup, w)
    w.ready = w.healthy = True
    _settle(sup, w, 12)
    w.last("vllm").rc = -11
    w.ready = False  # Tier 0 표시 파일이 오래됨(멈춤 등): 다시 띄우지 않는다
    _settle(sup, w, 30, dt=10)
    assert w.spawned.count("vllm") == 1
    w.ready = True
    _settle(sup, w, 2)
    assert w.spawned.count("vllm") == 2


def test_fast_failures_give_up_after_limit_and_tier0_keeps_running(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, env={"SIMAI_SUPERVISE_MAX_FAST_FAILURES": "3"})
    _start(sup, w)
    w.ready = w.healthy = True
    _settle(sup, w, 12)
    for _ in range(200):
        if sup.svc["worker"].proc is not None:
            sup.svc["worker"].proc.rc = 1  # 처리기 없음 같은 즉시 종료(1초 안)
        _settle(sup, w, 1, dt=1)
    assert w.spawned.count("worker") == 3 and sup.svc["worker"].given_up
    assert sup.svc["vllm"].proc is not None and w.last("tier0").rc is None
    assert not [s for s in w.signals if s[0] == "tier0"]


def test_long_running_failure_resets_backoff(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, env={"SIMAI_SUPERVISE_MAX_FAST_FAILURES": "2"})
    _start(sup, w)
    w.ready = w.healthy = True
    _settle(sup, w, 12)
    for _ in range(4):  # 매번 2분 넘게 돌다가 죽는다: 빠른 실패가 아니라 포기하지 않는다
        w.now += 130
        _settle(sup, w, 1)
        sup.svc["worker"].proc.rc = 1
        _settle(sup, w, 1)
        assert sup.svc["worker"].next_at - w.now <= 5
        w.now += 6
        _settle(sup, w, 1)
    assert not sup.svc["worker"].given_up and w.spawned.count("worker") == 5


def test_after_long_run_fast_failure_count_starts_from_zero(bs, tmp_path, monkeypatch):
    """리뷰 -246 권장 2: 오래 돌다 끝난 뒤에는 빠른 실패를 max_fast 번 다 채워야 포기한다(한 번 적게 포기하지 않는다)."""
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, env={"SIMAI_SUPERVISE_MAX_FAST_FAILURES": "3"})
    _start(sup, w)
    w.ready = w.healthy = True
    _settle(sup, w, 12)
    w.now += 130  # 오래 돌았다
    _settle(sup, w, 1)
    sup.svc["worker"].proc.rc = 1
    _settle(sup, w, 1)
    assert sup.svc["worker"].fails == 0 and not sup.svc["worker"].given_up
    spawned = w.spawned.count("worker")
    for _ in range(300):
        if sup.svc["worker"].proc is not None:
            sup.svc["worker"].proc.rc = 1
        _settle(sup, w, 1, dt=1)
    assert sup.svc["worker"].given_up and w.spawned.count("worker") - spawned == 3


def test_vllm_not_healthy_in_time_is_group_killed_and_retried(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, env={"SIMAI_VLLM_READY_TIMEOUT_S": "60"})
    _start(sup, w)
    w.ready = True
    _settle(sup, w, 3)
    first = w.last("vllm")
    _settle(sup, w, 70)  # /health 가 끝내 200 이 아니다
    assert first.rc == -9 and ("vllm", bs.KILL) in w.signals
    assert "worker" not in w.spawned  # VLM 이 뜨지 않았으니 워커도 아직
    assert w.spawned.count("vllm") >= 2


def test_worker_starts_without_vlm_when_vllm_is_given_up(bs, tmp_path, monkeypatch):
    w = World()
    w.fail_spawn.add("vllm")  # vllm 명령이 없다
    sup = _sup(bs, tmp_path, monkeypatch, w, env={"SIMAI_SUPERVISE_MAX_FAST_FAILURES": "2"})
    _start(sup, w)
    w.ready = True
    _settle(sup, w, 20, dt=100)
    assert sup.svc["vllm"].given_up and w.spawned == ["tier0", "worker"]


def test_worker_weights_failure_keeps_tier0_and_starts_nothing(bs, tmp_path, monkeypatch, capsys):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, weights_ok=False)
    _start(sup, w)
    w.ready = w.healthy = True
    _settle(sup, w, 20)
    assert sup.phase == "off" and w.spawned == ["tier0"] and not w.signals
    assert "Tier 0 는 계속" in capsys.readouterr().err
    rec = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "simai" / "runs" / "bootstrap").glob("*.json")]
    assert rec[0]["stage"] == "worker-weights" and rec[0]["exit"] == 2 and TOKEN not in json.dumps(rec)


def _setup_manifest(tmp_path: Path) -> Path:
    def entry(name, sets, path):
        return {"name": name, "sets": sets, "path": path, "source": {"url": "x"}, "sha256": "0" * 64}

    manifest = tmp_path / "weights-setup.json"
    manifest.write_text(json.dumps({"weights": [
        entry("m", ["vlm"], "weights/m.bin"),
        entry("ma-model", ["static"], "weights/map-anything/model.safetensors"),
        entry("ma-config", ["static"], "weights/map-anything/config.json"),
        entry("ma-apache-model", ["static"], "weights/map-anything-apache/model.safetensors"),
    ]}), encoding="utf-8")
    return manifest


def _join_setup(sup) -> None:
    if sup._setup_weights_thread is not None:
        sup._setup_weights_thread.join(5)


def test_worker_start_prepares_setup_mapanything_weights_after_vlm(bs, tmp_path, monkeypatch):
    """lobby 워커가 site_setup 을 처리하므로 워커 모델 폴더(weights/map-anything)의 static 항목만 VLM 다음에 받는다(-473)."""
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, env={"SIMAI_WEIGHTS_MANIFEST": str(_setup_manifest(tmp_path))})
    _start(sup, w)
    w.ready = True
    _settle(sup, w, 3)
    _join_setup(sup)
    assert sup.weight_calls == [["m"], ["ma-model", "ma-config"]]  # apache 사본은 워커가 쓰지 않는다
    assert sup.setup_weights_error is None and w.spawned == ["tier0", "vllm"]
    rec = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((tmp_path / "simai" / "runs" / "bootstrap").glob("*.json"))]
    assert sorted(r["stage"] for r in rec) == ["setup-weights", "worker-weights"]
    assert next(r for r in rec if r["stage"] == "setup-weights")["exit"] == 0


def test_setup_weights_failure_warns_but_keeps_worker(bs, tmp_path, monkeypatch, capsys):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, services=("tier0", "worker"),
               env={"SIMAI_WEIGHTS_MANIFEST": str(_setup_manifest(tmp_path)),
                    "SIMAI_MAPANYTHING_MODEL": str(tmp_path / "elsewhere")})
    _start(sup, w)
    w.ready = w.healthy = True
    _settle(sup, w, 4)
    _join_setup(sup)
    assert "worker" in w.spawned and sup.setup_weights_error and sup.weight_calls == []
    assert "STATIC_MODEL_MISSING" in capsys.readouterr().err


def test_setup_weights_can_be_turned_off_and_skipped_without_worker(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, env={"SIMAI_WEIGHTS_MANIFEST": str(_setup_manifest(tmp_path)),
                                                 "SIMAI_WORKER_SETUP_WEIGHTS": "0"})
    _start(sup, w)
    w.ready = True
    _settle(sup, w, 3)
    assert sup._setup_weights_thread is None and sup.weight_calls == [["m"]]
    w2 = World()
    (tmp_path / "b").mkdir()
    sup2 = _sup(bs, tmp_path / "b", monkeypatch, w2, services=("tier0", "vllm"),
                env={"SIMAI_WEIGHTS_MANIFEST": str(_setup_manifest(tmp_path))})
    _start(sup2, w2)
    w2.ready = True
    _settle(sup2, w2, 3)
    assert sup2._setup_weights_thread is None


@pytest.mark.parametrize("rc, want", [(0, 0), (5, 5), (-9, 137)])
def test_tier0_exit_stops_worker_then_vllm_and_returns_same_code(bs, tmp_path, monkeypatch, rc, want):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w)
    sup.spawn = w.spawn
    w.ready = w.healthy = True

    def ticking(_s):
        if sup._weights is not None:
            sup._weights.join(5)  # 가짜 시계라 실제로 기다리지 않는다: 가중치 스레드를 끝까지 둔다
        w.now += 1
        if w.now == 20:
            w.last("tier0").rc = rc
    sup.sleep = ticking
    assert sup.run() == want
    term = [n for n, s in w.signals if s == signal.SIGTERM]
    assert term == ["worker", "vllm"]  # Tier 0 는 이미 끝났다. 워커 먼저
    assert ("tier0", bs.KILL) in w.signals  # Tier 0 그룹에 남은 것 정리


def test_signal_stops_worker_vllm_tier0_in_order(bs, tmp_path, monkeypatch):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w)
    w.ready = w.healthy = True

    def ticking(_s):
        if sup._weights is not None:
            sup._weights.join(5)  # 가짜 시계라 실제로 기다리지 않는다: 가중치 스레드를 끝까지 둔다
        w.now += 1
        if w.now == 20:
            sup.request_stop(signal.SIGTERM)
    sup.sleep = ticking
    assert sup.run() == 0
    assert [n for n, s in w.signals if s == signal.SIGTERM] == ["worker", "vllm", "tier0"]


def test_stop_escalates_to_group_kill_when_term_is_ignored(bs, tmp_path, monkeypatch, capsys):
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w)
    t0 = _start(sup, w)
    t0.ignore_term = True
    assert sup._stop(sup.svc["tier0"]) == -9
    assert w.signals == [("tier0", signal.SIGTERM), ("tier0", bs.KILL)] and "SIGKILL" in capsys.readouterr().err


def test_tier0_command_missing_returns_127(bs, tmp_path, monkeypatch):
    w = World()
    w.fail_spawn.add("tier0")
    sup = _sup(bs, tmp_path, monkeypatch, w)
    assert sup.run() == 127 and w.spawned == []


# ---------------------------------------------------------------- main 분기


def _main_env(bs, tmp_path, monkeypatch, *, total_mb="81559", services=None):
    data = b"yolo" * 100
    src = tmp_path / "yolo.pt"
    src.write_bytes(data)
    manifest = tmp_path / "w.json"
    manifest.write_text(json.dumps({"weights": [{"name": "yolo", "sets": ["tier0"], "path": "weights/yolo.pt", "source": {"url": src.as_uri()},
                                                 "sha256": hashlib.sha256(data).hexdigest()}]}), encoding="utf-8")
    for k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "SIMAI_SERVICES", "SIMAI_TIER0_WEIGHTS", "SIMAI_TIER0_WEIGHTS_SHA256"):
        monkeypatch.delenv(k, raising=False)
    if services is not None:
        monkeypatch.setenv("SIMAI_SERVICES", services)
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "simai"))
    monkeypatch.setenv("SIMAI_NVIDIA_SMI", f"{PY} {FAKE_SMI}")
    monkeypatch.setenv("FAKE_SMI_TOTAL_MB", total_mb)
    monkeypatch.setenv("SIMAI_WEIGHTS_MANIFEST", str(manifest))
    monkeypatch.setenv("SIMAI_MIN_FREE_GB", "0")
    calls: list = []
    monkeypatch.setattr(bs, "execvp", lambda argv: calls.append(list(argv)))
    sup: list = []
    monkeypatch.setattr(bs, "supervise", lambda rec, services, rest: sup.append((services, rest)) or 0)
    return calls, sup


@pytest.mark.parametrize("value", [None, "tier0", " tier0 "])
def test_tier0_alone_is_plain_exec_as_before(bs, tmp_path, monkeypatch, value):
    calls, sup = _main_env(bs, tmp_path, monkeypatch, services=value)
    assert bs.main(["tier0", "--config", "x"]) == 0
    assert calls == [["simai-tier0", "--config", "x"]] and sup == []


def test_services_go_to_supervisor_in_fixed_order(bs, tmp_path, monkeypatch):
    calls, sup = _main_env(bs, tmp_path, monkeypatch, services="worker,tier0,vllm")
    assert bs.main(["--config", "x"]) == 0
    assert calls == [] and sup == [(["tier0", "vllm", "worker"], ["--config", "x"])]


@pytest.mark.parametrize("value", ["vllm,worker", "tier0,vlm", "tier0,tier0", ","])
def test_bad_services_is_usage_error_before_any_download(bs, tmp_path, monkeypatch, value):
    calls, sup = _main_env(bs, tmp_path, monkeypatch, services=value)
    monkeypatch.setattr(bs, "urlopen", lambda req: (_ for _ in ()).throw(AssertionError("받으면 안 된다")))
    assert bs.main(["tier0"]) == bs.EXIT_USAGE and calls == [] and sup == []
    assert not (tmp_path / "simai").exists()


def test_gpu_below_48gb_runs_tier0_alone_even_with_services(bs, tmp_path, monkeypatch, capsys):
    calls, sup = _main_env(bs, tmp_path, monkeypatch, total_mb="24564", services="tier0,vllm,worker")  # RTX 4090
    assert bs.main(["tier0"]) == 0
    assert calls == [["simai-tier0"]] and sup == [] and "48GB" in capsys.readouterr().err


def test_other_modes_ignore_services(bs, tmp_path, monkeypatch):
    calls, sup = _main_env(bs, tmp_path, monkeypatch, services="tier0,vllm")
    assert bs.main(["run", "python", "-V"]) == 0 and calls == [["python", "-V"]] and sup == []


# ---------------------------------------------------------------- 받기: 재시도·이어받기


class Resp(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None, cut_after: int | None = None) -> None:
        super().__init__(data)
        self.status, self.headers, self.cut_after = status, headers or {}, cut_after

    def read(self, n=-1):
        if self.cut_after is not None and self.tell() >= self.cut_after:
            raise ConnectionResetError("망 끊김")
        if self.cut_after is not None:
            n = min(n if n > 0 else 1 << 30, self.cut_after - self.tell())
        return super().read(n)


DATA = bytes(range(256)) * 4096  # 1 MiB


def _entry(sha: str | None = None, sets=("vlm",)) -> dict:
    return {"name": "shard", "sets": list(sets), "path": "weights/m/shard.safetensors", "source": {"url": "https://example.invalid/shard"},
            "sha256": sha or hashlib.sha256(DATA).hexdigest()}


def _server(bs, monkeypatch, script):
    """script(req, n) → 응답 또는 예외. 받은 요청의 Range 헤더를 기록한다."""
    seen: list = []

    def fake(req):
        seen.append(req.get_header("Range"))
        r = script(req, len(seen))
        if isinstance(r, BaseException):
            raise r
        return r
    monkeypatch.setattr(bs, "urlopen", fake)
    slept: list = []
    monkeypatch.setattr(bs, "backoff_sleep", slept.append)
    return seen, slept


def _range_ok(req, start):
    return Resp(DATA[start:], 206, {"Content-Range": f"bytes {start}-{len(DATA) - 1}/{len(DATA)}"})


def test_cut_download_resumes_with_range_and_backoff(bs, tmp_path, monkeypatch):
    def script(req, n):
        if n == 1:
            return Resp(DATA, cut_after=300_000)
        return _range_ok(req, int(req.get_header("Range").split("=")[1].rstrip("-")))
    seen, slept = _server(bs, monkeypatch, script)
    target = tmp_path / "simai" / "weights" / "m" / "shard.safetensors"
    assert bs.fetch(_entry(), target) == hashlib.sha256(DATA).hexdigest()
    assert target.read_bytes() == DATA and seen == [None, "bytes=300000-"] and slept == [2.0]
    assert not target.with_name(target.name + ".part").exists()


def test_origin_ignoring_range_restarts_instead_of_appending(bs, tmp_path, monkeypatch):
    seen, _ = _server(bs, monkeypatch, lambda req, n: Resp(DATA, cut_after=200_000) if n == 1 else Resp(DATA, 200))
    target = tmp_path / "w" / "shard.bin"
    assert bs.fetch(_entry(), target) == hashlib.sha256(DATA).hexdigest()
    assert target.read_bytes() == DATA and seen[1] == "bytes=200000-"  # 200 전체 응답이면 덧붙이지 않고 처음부터


def test_wrong_content_range_restarts(bs, tmp_path, monkeypatch):
    def script(req, n):
        if n == 1:
            return Resp(DATA, cut_after=100_000)
        return Resp(DATA, 206, {"Content-Range": f"bytes 0-{len(DATA) - 1}/{len(DATA)}"})  # 요청과 다른 범위
    _server(bs, monkeypatch, script)
    target = tmp_path / "w" / "shard.bin"
    bs.fetch(_entry(), target)
    assert target.read_bytes() == DATA


def test_416_restarts_from_zero(bs, tmp_path, monkeypatch):
    target = tmp_path / "w" / "shard.bin"
    target.parent.mkdir(parents=True)
    target.with_name("shard.bin.part").write_bytes(b"x" * (len(DATA) + 10))  # 원본보다 긴 남은 임시 파일

    def script(req, n):
        if n == 1:
            return urllib.error.HTTPError("u", 416, "Range Not Satisfiable", {}, None)
        return Resp(DATA, 200)
    seen, _ = _server(bs, monkeypatch, script)
    bs.fetch(_entry(), target)
    assert target.read_bytes() == DATA and seen == [f"bytes={len(DATA) + 10}-", None]


def test_retries_exhausted_keeps_part_and_next_start_resumes(bs, tmp_path, monkeypatch):
    monkeypatch.setenv("SIMAI_FETCH_ATTEMPTS", "3")
    _server(bs, monkeypatch, lambda req, n: Resp(DATA, cut_after=50_000) if n == 1 else urllib.error.URLError("timed out"))
    target = tmp_path / "w" / "shard.bin"
    with pytest.raises(bs.BootstrapError) as e:
        bs.fetch(_entry(), target)
    assert e.value.code == 2 and "3회" in str(e.value)
    part = target.with_name("shard.bin.part")
    assert part.stat().st_size == 50_000 and not target.exists()
    seen, _ = _server(bs, monkeypatch, lambda req, n: _range_ok(req, 50_000))  # 다음 시작
    bs.fetch(_entry(), target)
    assert seen == ["bytes=50000-"] and target.read_bytes() == DATA


def test_permanent_http_error_is_not_retried(bs, tmp_path, monkeypatch):
    seen, slept = _server(bs, monkeypatch, lambda req, n: urllib.error.HTTPError("u", 404, "Not Found", {}, None))
    with pytest.raises(bs.BootstrapError):
        bs.fetch(_entry(), tmp_path / "w" / "shard.bin")
    assert len(seen) == 1 and slept == [] and not (tmp_path / "w" / "shard.bin.part").exists()  # 빈 임시 파일은 남기지 않는다


@pytest.mark.parametrize("code", [429, 503])
def test_transient_http_error_is_retried(bs, tmp_path, monkeypatch, code):
    _server(bs, monkeypatch, lambda req, n: urllib.error.HTTPError("u", code, "x", {}, None) if n == 1 else Resp(DATA))
    bs.fetch(_entry(), tmp_path / "w" / "shard.bin")


def test_hash_mismatch_after_resume_discards_part(bs, tmp_path, monkeypatch):
    target = tmp_path / "w" / "shard.bin"
    target.parent.mkdir(parents=True)
    target.with_name("shard.bin.part").write_bytes(b"\0" * 1000)  # 깨진 앞부분
    _server(bs, monkeypatch, lambda req, n: _range_ok(req, 1000))
    with pytest.raises(bs.BootstrapError) as e:
        bs.fetch(_entry(), target)
    assert "해시 불일치" in str(e.value) and not target.exists() and not target.with_name("shard.bin.part").exists()


def test_concurrent_downloaders_publish_only_verified_file(bs, tmp_path, monkeypatch):
    """Codex 리뷰 R1 반례: A 가 받는 중(잠금을 쥔 채) B 가 같은 항목을 준비한다. B 의 원본은 같은 길이의 다른 바이트(200)다.
    잠금이 재확인·받기·해시·게시·스탬프 전체를 덮으므로 B 는 A 가 게시를 끝낼 때까지 기다렸다가 받지 않고 A 의 파일을 쓴다.
    최종 파일은 확인된 내용이고, 잘못된 파일에 유효한 스탬프가 남지 않는다."""
    import threading

    e = _entry()
    root = tmp_path / "simai"
    a_inside, a_go = threading.Event(), threading.Event()
    calls: list[str] = []

    class Slow(Resp):
        def read(self, n=-1):
            if not a_inside.is_set():
                a_inside.set()
                assert a_go.wait(10)
            return super().read(n)

    def fake(req):
        who = threading.current_thread().name
        calls.append(who)
        return Slow(DATA) if who == "A" else Resp(bytes(len(DATA)))  # B 의 원본: 같은 길이, 다른 내용
    monkeypatch.setattr(bs, "urlopen", fake)
    monkeypatch.setattr(bs, "backoff_sleep", lambda s: None)
    out: dict = {}

    def run(name):
        try:
            out[name] = bs.ensure_weights(root, [e])[0]["status"]
        except Exception as err:  # noqa: BLE001
            out[name] = err
    a = threading.Thread(target=run, args=("A",), name="A")
    a.start()
    assert a_inside.wait(10)
    b = threading.Thread(target=run, args=("B",), name="B")
    b.start()
    b.join(0.5)
    assert b.is_alive() and calls == ["A"]  # B 는 잠금을 기다린다(A 의 .part 를 건드리지 않는다)
    a_go.set()
    a.join(10)
    b.join(10)
    assert out == {"A": "downloaded", "B": "present-stamp"} and calls == ["A"]
    target = root / e["path"]
    assert target.read_bytes() == DATA and not target.with_name(target.name + ".part").exists()
    assert bs.ensure_weights(root, [e])[0]["status"] == "present-stamp"


def test_lock_file_is_outside_model_dir_and_kept(bs, tmp_path, monkeypatch):
    e = _entry()
    _server(bs, monkeypatch, lambda req, n: Resp(DATA))
    root = tmp_path / "simai"
    bs.ensure_weights(root, [e])
    locks = list((root / "weights" / ".locks").iterdir())
    assert len(locks) == 1 and locks[0].name.endswith(".lock")  # 지우지 않는다(inode 가 갈라지지 않게)
    assert sorted(x.name for x in (root / "weights" / "m").iterdir()) == ["shard.safetensors"]  # 모델 폴더에 남의 파일 없음


# ---------------------------------------------------------------- 검증 스탬프


def _present(bs, tmp_path, entry):
    root = tmp_path / "simai"
    target = root / entry["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(DATA)
    return root, target


def test_stamp_skips_rehash_only_while_size_and_mtime_match(bs, tmp_path, monkeypatch):
    e = _entry()
    root, target = _present(bs, tmp_path, e)
    hashed: list = []
    real = bs.sha256_file
    monkeypatch.setattr(bs, "sha256_file", lambda p: hashed.append(p) or real(p))
    assert bs.ensure_weights(root, [e])[0]["status"] == "present" and len(hashed) == 1
    stamp = json.loads((root / "weights" / ".verified" / "shard.json").read_text(encoding="utf-8"))
    assert stamp["sha256"] == e["sha256"] and stamp["stat"][0] == len(DATA)
    assert bs.ensure_weights(root, [e])[0]["status"] == "present-stamp" and len(hashed) == 1  # 다시 해시하지 않는다
    os.utime(target, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))  # 파일이 바뀐 흔적(mtime)
    assert bs.ensure_weights(root, [e])[0]["status"] == "present" and len(hashed) == 2


def test_stamp_does_not_cover_a_tampered_file_or_other_hash(bs, tmp_path, monkeypatch):
    e = _entry()
    root, target = _present(bs, tmp_path, e)
    bs.ensure_weights(root, [e])
    target.write_bytes(DATA[:-1] + b"X")  # 같은 크기, 내용·mtime 다름
    with pytest.raises(bs.BootstrapError) as err:
        bs.ensure_weights(root, [e])
    assert "해시 불일치" in str(err.value) and target.exists()
    other = dict(e, sha256="1" * 64)  # 목록 해시가 바뀌면 스탬프는 맞지 않는다
    target.write_bytes(DATA)
    bs.ensure_weights(root, [e])
    with pytest.raises(bs.BootstrapError):
        bs.ensure_weights(root, [other])


def test_replaced_file_with_restored_mtime_is_rehashed(bs, tmp_path, monkeypatch):
    """Codex 리뷰 권장 반례: 확인 뒤 다른 프로세스가 같은 크기의 다른 내용으로 바꾸고 mtime 을 되돌려도(복사·복원) 스탬프로 건너뛰지 않는다."""
    e = _entry()
    root, target = _present(bs, tmp_path, e)
    bs.ensure_weights(root, [e])
    st = target.stat()
    other = target.with_name("other.tmp")
    other.write_bytes(DATA[:-1] + b"Z")
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(other, target)  # 새 파일(inode·ctime 다름), 크기·mtime 은 같다
    assert target.stat().st_size == st.st_size and target.stat().st_mtime_ns == st.st_mtime_ns
    with pytest.raises(bs.BootstrapError) as err:
        bs.ensure_weights(root, [e])
    assert "해시 불일치" in str(err.value)


def test_file_changed_while_hashing_gets_no_stamp(bs, tmp_path, monkeypatch, capsys):
    e = _entry()
    root, target = _present(bs, tmp_path, e)
    real = bs.sha256_file

    def hash_then_touch(path):
        h = real(path)
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 7_000_000_000))  # 해시하는 동안 바뀜
        return h
    monkeypatch.setattr(bs, "sha256_file", hash_then_touch)
    assert bs.ensure_weights(root, [e])[0]["status"] == "present"
    assert not (root / "weights" / ".verified" / "shard.json").exists() and "스탬프를 남기지 않는다" in capsys.readouterr().err


def test_tier0_weights_are_always_hashed_without_stamp(bs, tmp_path, monkeypatch):
    e = _entry(sets=("tier0", "exp02"))
    root, _target = _present(bs, tmp_path, e)
    hashed: list = []
    real = bs.sha256_file
    monkeypatch.setattr(bs, "sha256_file", lambda p: hashed.append(p) or real(p))
    for _ in range(3):
        assert bs.ensure_weights(root, [e])[0]["status"] == "present"
    assert len(hashed) == 3 and not (root / "weights" / ".verified").exists()


def test_downloaded_worker_weight_gets_stamp(bs, tmp_path, monkeypatch):
    e = _entry()
    _server(bs, monkeypatch, lambda req, n: Resp(DATA))
    root = tmp_path / "simai"
    assert bs.ensure_weights(root, [e])[0]["status"] == "downloaded"
    assert bs.ensure_weights(root, [e])[0]["status"] == "present-stamp"


def test_repo_vlm_set_is_stamped_and_tier0_is_not(bs):
    vlm = bs.load_manifest(bs.DEFAULT_MANIFEST, "vlm")
    assert len(vlm) == 18 and all(bs._stamped(w) for w in vlm)
    assert not any(bs._stamped(w) for w in bs.load_manifest(bs.DEFAULT_MANIFEST, "tier0"))


# ---------------------------------------------------------------- 실제 프로세스 (POSIX)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _events(path: Path) -> list[list[str]]:
    return [ln.split(" ", 4) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _wait_for(path: Path, pred, timeout: float = 60) -> list[list[str]]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ev = _events(path)
        if pred(ev):
            return ev
        time.sleep(0.1)
    raise AssertionError(f"기다린 사건이 오지 않았다: {_events(path)}")


def _real(tmp_path: Path, **extra) -> tuple[subprocess.Popen, Path, Path]:
    data = b"fake-weights" * 100
    (tmp_path / "t0.pt").write_bytes(data)
    (tmp_path / "vlm.bin").write_bytes(data * 2)
    manifest = tmp_path / "weights.json"
    manifest.write_text(json.dumps({"weights": [
        {"name": "yolo", "sets": ["tier0"], "path": "weights/yolo/yolo26x-pose.pt", "source": {"url": (tmp_path / "t0.pt").as_uri()},
         "sha256": hashlib.sha256(data).hexdigest(), "env": "SIMAI_TIER0_WEIGHTS"},
        {"name": "fakevlm", "sets": ["fakevlm"], "path": "weights/cosmos3-nano/model.bin", "source": {"url": (tmp_path / "vlm.bin").as_uri()},
         "sha256": hashlib.sha256(data * 2).hexdigest()}]}), encoding="utf-8")
    events, root = tmp_path / "events.txt", tmp_path / "simai"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SIMAI_", "FAKE_"))}
    env |= {"SIMAI_ROOT": str(root), "SIMAI_NVIDIA_SMI": f"{PY} {FAKE_SMI}", "SIMAI_WEIGHTS_MANIFEST": str(manifest),
            "SIMAI_MIN_FREE_GB": "0", "SIMAI_SERVICES": "tier0,vllm,worker", "SIMAI_WORKER_WEIGHTS_SET": "fakevlm",
            "SIMAI_TIER0_CMD": f"{PY} {FAKE_SERVICE} tier0", "SIMAI_VLLM_CMD": f"{PY} {FAKE_SERVICE} vllm",
            "SIMAI_WORKER_CMD": f"{PY} {FAKE_SERVICE} worker", "SIMAI_VLLM_PORT": str(_free_port()),
            "SIMAI_SUPERVISE_TICK_S": "0.1", "SIMAI_SUPERVISE_BACKOFF_S": "0.2", "SIMAI_STOP_GRACE_S": "5",
            "FAKE_EVENTS": str(events), "HF_TOKEN": TOKEN, "HUGGING_FACE_HUB_TOKEN": TOKEN, "PYTHONUNBUFFERED": "1"}
    env |= extra
    p = subprocess.Popen([PY, (REPO / "deploy" / "scripts" / "simai_bootstrap.py").as_posix(), "tier0", "--config", "c.json"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p, events, root


def _finish(p: subprocess.Popen, timeout: float = 60) -> tuple[int, str]:
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
        raise AssertionError(out.decode("utf-8", "replace")[-3000:])
    return p.returncode, out.decode("utf-8", "replace")


@posix_only
def test_real_processes_start_in_order_stop_in_reverse_and_clean_grandchildren(tmp_path):
    p, events, root = _real(tmp_path)
    try:
        ev = _wait_for(events, lambda ev: any(e[0] == "worker" and e[1] == "start" for e in ev))
        starts = [e[0] for e in ev if e[1] == "start"]
        assert starts == ["tier0", "vllm", "worker"]
        assert all(e[2].startswith("clean") for e in ev if e[1] == "start")  # HF 토큰 없음
        assert [e[2:4] for e in ev if e[1] == "start" and e[0] != "tier0"] == [["clean", "omp=1"]] * 2
        kid = int(next(e[2] for e in ev if e[1] == "grandchild"))
        assert _alive(kid)
        p.send_signal(signal.SIGTERM)
        rc, out = _finish(p)
    finally:
        if p.poll() is None:
            p.kill()
    ev = _events(events)
    assert rc == 0, out[-2000:]
    assert [e[0] for e in ev if e[1] == "term"] == ["worker", "vllm", "tier0"]
    assert not _alive(kid)  # vLLM 그룹의 손자도 정리됐다
    for name in ("tier0", "vllm", "worker"):  # 줄마다 [이름] 접두로 표준 오류에, 원문은 로그 파일에
        assert f"[{name}] {name} says hello" in out
        assert f"{name} says hello" in (root / "logs" / f"{name}.log").read_text(encoding="utf-8")
    assert (root / "weights" / "cosmos3-nano" / "model.bin").exists() and (root / "weights" / ".verified" / "fakevlm.json").exists()
    assert TOKEN not in out


@posix_only
def test_real_stale_marker_does_not_start_vllm_before_this_tier0_is_ready(tmp_path):
    """리뷰 -246 필수 반례(실제 프로세스): 이전 실행의 running·OK 표시가 남은 채(mtime 새것) 새 Tier 0 가 엔진을 준비하는 동안(3초 지연) vLLM 이 뜨지 않는다."""
    root = tmp_path / "simai"
    _marker(root, int(time.time() * 1000) - 5_000, 1)
    p, events, _root = _real(tmp_path, FAKE_TIER0_READY_AFTER_S="3")
    try:
        _wait_for(events, lambda ev: any(e[0] == "worker" and e[1] == "start" for e in ev))
        p.send_signal(signal.SIGTERM)
        rc, out = _finish(p)
    finally:
        if p.poll() is None:
            p.kill()
    lines = events.read_text(encoding="utf-8").splitlines()
    ts = {(ln.split()[0], ln.split()[1]): float(ln.split()[-1]) for ln in lines if ln.split()[1] in ("start", "marker")}
    assert rc == 0, out[-2000:]
    assert ts[("tier0", "marker")] - ts[("tier0", "start")] >= 2.9
    assert ts[("vllm", "start")] >= ts[("tier0", "marker")]  # 이번 Tier 0 가 표시를 쓴 뒤에야
    assert "이번 Tier 0 기동 전의 표시 파일" in out


@posix_only
def test_real_vllm_crash_kills_its_grandchild_restarts_and_tier0_stays(tmp_path):
    p, events, _root = _real(tmp_path, FAKE_VLLM_EXIT_AFTER_S="1.5", SIMAI_SUPERVISE_MAX_FAST_FAILURES="2")
    try:
        ev = _wait_for(events, lambda ev: sum(e[0] == "vllm" and e[1] == "crash" for e in ev) >= 2, timeout=90)
        kids = [int(e[2]) for e in ev if e[1] == "grandchild"]
        time.sleep(1)
        assert len(kids) >= 2 and not any(_alive(k) for k in kids)  # 죽은 vLLM 의 손자를 남기지 않는다
        t0 = [e for e in ev if e[0] == "tier0"]
        # Tier 0 에는 신호(term)·재시작(두 번째 start)·종료가 없다. 표시 파일을 쓰기 시작한 'marker' 사건만 허용(fake_service, 리뷰 -246)
        # fake_service 사건 줄은 '<역할> <사건 내용…> <pid> <unix 초>' 라 pid 는 끝에서 두 번째 칸이다(start 는 내용에 공백이 있다)
        assert [e[1] for e in t0] in (["start"], ["start", "marker"]) and len({" ".join(e).split()[-2] for e in t0}) == 1
        assert p.poll() is None
        p.send_signal(signal.SIGTERM)
        rc, out = _finish(p)
    finally:
        if p.poll() is None:
            p.kill()
    assert rc == 0 and "다시 띄우지 않는다" in out


@posix_only
def test_real_tier0_exit_brings_down_others_with_same_code(tmp_path):
    p, events, _root = _real(tmp_path, FAKE_TIER0_EXIT_AFTER_S="6", FAKE_TIER0_EXIT_CODE="7", FAKE_VLLM_HEALTH_AFTER_S="0.2")
    try:
        rc, out = _finish(p, timeout=90)
    finally:
        if p.poll() is None:
            p.kill()
    ev = _events(events)
    assert rc == 7, out[-2000:]
    assert [e[0] for e in ev if e[1] == "term"] == ["worker", "vllm"]
    kids = [int(e[2]) for e in ev if e[1] == "grandchild"]
    assert kids and not any(_alive(k) for k in kids)



# ---------------------------------------------------------------- S15P31A101-382: GPU 상한·로비 LIVE 통합 기동·기동 전 점검


def _tlv(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        return bytes([tag, n]) + content
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(raw)]) + raw + content


_TEST_SPKI = _tlv(
    0x30,
    _tlv(0x30, _tlv(0x06, bytes.fromhex("2a8648ce3d0201")) + _tlv(0x06, bytes.fromhex("2a8648ce3d030107")))
    + _tlv(0x03, b"\x00\x04" + bytes(range(64))),
)


def _test_cert_pem() -> bytes:
    import base64

    name = _tlv(0x30, _tlv(0x31, _tlv(0x30, _tlv(0x06, b"\x55\x04\x03") + _tlv(0x0C, b"tier0-lobby"))))
    validity = _tlv(0x30, _tlv(0x17, b"261017000000Z") + _tlv(0x17, b"361017000000Z"))
    tbs = (
        _tlv(0xA0, _tlv(0x02, b"\x02"))
        + _tlv(0x02, b"\x01\x23")
        + _tlv(0x30, _tlv(0x06, b"\x2a"))
        + name
        + validity
        + name
        + _TEST_SPKI
        + _tlv(0xA3, _tlv(0x30, b""))
    )
    der = _tlv(0x30, _tlv(0x30, tbs) + _tlv(0x30, _tlv(0x06, b"\x2a")) + _tlv(0x03, b"\x00" + b"\x11" * 70))
    return b"-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(der) + b"-----END CERTIFICATE-----\n"


def test_worker_torch_memory_fraction_is_capped_even_without_avatar(bs, tmp_path, monkeypatch):
    """avatar 가 꺼진 기본 구성(tier0,vllm,worker)에서도 worker 상한은 0.15(0.70 - 0.55)로 묶여야 한다 (S15P31A101-382)."""
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, services=("tier0", "vllm", "worker"))
    assert float(sup.svc["worker"].env["SIMAI_WORKER_TORCH_MEMORY_FRACTION"]) == pytest.approx(0.15)
    assert sup.svc["worker"].env["SIMAI_TIER0_MARKER"] == str(tmp_path / "simai" / "state" / "tier0.live")
    assert sup.svc["worker"].env["SIMAI_TIER0_WEIGHTS"] == str(tmp_path / "simai" / "weights" / "yolo" / "yolo26x-pose.pt")
    assert sup.svc["worker"].env["SIMAI_VLLM_PORT"] == "8000"
    assert sup.svc["worker"].env["SIMAI_VLM_MODEL"] == "cosmos3-nano"

    # vLLM 이 없는(tier0,worker) 구성은 worker_fraction 전체(0.70)를 쓴다
    sup_no_vllm = _sup(bs, tmp_path, monkeypatch, World(), services=("tier0", "worker"))
    assert float(sup_no_vllm.svc["worker"].env["SIMAI_WORKER_TORCH_MEMORY_FRACTION"]) == pytest.approx(0.70)

    # avatar 없이도 vLLM + worker 상한 합이 예산을 넘으면 기동 전에 거부한다
    with pytest.raises(bs.BootstrapError) as exc:
        _sup(bs, tmp_path, monkeypatch, World(), services=("tier0", "vllm", "worker"),
             env={"SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION": "0.70"})
    assert exc.value.code == bs.EXIT_USAGE

    with pytest.raises(bs.BootstrapError) as exc2:
        _sup(bs, tmp_path, monkeypatch, World(), services=("tier0", "vllm", "worker"),
             env={"SIMAI_WORKER_TORCH_MEMORY_FRACTION": "0.25"})
    assert exc2.value.code == bs.EXIT_USAGE


def test_lobby_mode_prepares_work_runs_supervisor_and_propagates_nonzero_exit_code(bs, tmp_path, monkeypatch, capsys):
    """로비 LIVE 통합 기동(simai-bootstrap lobby): 설정·토큰 파일 격리·pod.jsonl·Supervisor·비-0 종료 코드 전파 (S15P31A101-382)."""
    root = tmp_path / "simai"
    work = root / "runs" / "lobby-live-s9"
    bundle = tmp_path / "bundle"
    (bundle / "tls").mkdir(parents=True)
    (bundle / "calibration.json").write_text('{"cameras": ["cam1", "cam2", "cam3"]}\n', encoding="utf-8")
    (bundle / "tls" / "tier0.crt").write_bytes(_test_cert_pem())
    (bundle / "tls" / "tier0.key").write_text("FAKE-PRIVATE-KEY\n", encoding="utf-8")
    (bundle / "laptop_token.sha256").write_text("a" * 64 + "\n", encoding="utf-8")

    tier0_tok = "tier0-secret-tok-999"
    worker_tok = "worker-secret-tok-888"
    t0_file = tmp_path / "tier0.token"
    wk_file = tmp_path / "worker-s10.token"  # 워커 ID 는 토큰 파일 이름에서(-414, 10차: 기본 worker-01 로 떠 401)
    t0_file.write_text(tier0_tok + "\n", encoding="utf-8")
    wk_file.write_text(worker_tok + "\n", encoding="utf-8")

    monkeypatch.setenv("SIMAI_ROOT", str(root))
    monkeypatch.setenv("SIMAI_SERVICES", "tier0,vllm,worker")  # 통합 이미지 ENV·10차 템플릿 값: lobby 는 따르지 않고 아바타를 띄운다(-414)
    monkeypatch.delenv("SIMAI_WORKER_ID", raising=False)
    monkeypatch.setattr(bs, "prepare", lambda want_set: {
        "storage": {"root": str(root)},
        "gpu": {"name": "NVIDIA H100", "arch": "sm_90", "memory_total_mb": 81559.0},
        "weights": [],
        "exit": 0,
    })
    monkeypatch.setattr(bs, "ensure_weights", lambda r, entries: [{"name": e["name"], "status": "present"} for e in entries])

    w = World()
    orig_sup = bs.Supervisor

    def make_sup(r, services, tier0_argv, env=None, **kw):
        sup = orig_sup(
            r, services, tier0_argv, env=env, spawn=w.spawn,
            ready=lambda: (w.ready, "ready" if w.ready else "waiting"),
            health=lambda: w.healthy, clock=lambda: w.now, wall_ms=lambda: int(w.now * 1000) + 10_000,
            **kw,
        )

        def step_and_exit(_s):
            if sup._weights is not None:
                sup._weights.join(5)
            w.now += 1
            if w.now == 1:
                marker = work / "state" / "tier0.live"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({
                    "gpu_priority": {"phase": "running", "pid": w.last("tier0").pid, "written_at": 15_000, "reserve": {"state": "OK"}}
                }), encoding="utf-8")
                w.ready = True
                w.healthy = True
            if w.now >= 6:
                w.last("tier0").rc = 17  # 비-0 종료 코드(lobby_live_s9.py 의 return 0 버그 회귀)

        sup.sleep = step_and_exit
        return sup

    monkeypatch.setattr(bs, "Supervisor", make_sup)
    rc = bs.main([
        "lobby",
        "--work", str(work),
        "--bundle-dir", str(bundle),
        "--public-host", "203.0.113.77",
        "--ec2-base-url", "https://ec2.example.invalid",
        "--tier0-token-file", str(t0_file),
        "--worker-token-file", str(wk_file),
        "--no-clean-background",
    ])
    assert rc == 17, "Tier 0 의 비-0 종료 코드를 삼키지 않고 그대로 돌려줘야 한다"
    assert w.spawned == ["tier0", "vllm", "avatar", "worker"]
    assert w.envs["worker"]["SIMAI_WORKER_ID"] == w.envs["avatar"]["SIMAI_WORKER_ID"] == "worker-s10"
    assert "SIMAI_WORKER_ID" not in w.envs["tier0"] and "SIMAI_WORKER_ID" not in w.envs["vllm"]

    # 생성된 설정 파일 검증
    cfg = json.loads((work / "tier0-live.json").read_text(encoding="utf-8"))
    assert cfg["clean_background"] == {"enabled": False}
    assert cfg["live_calibration"]["enabled"] is True
    assert cfg["live_calibration"]["measured_camera_heights"] == {"cam1": 2.49, "cam2": 2.76, "cam3": 2.39}
    endpoint = json.loads((work / "endpoint.json").read_text(encoding="utf-8"))
    assert endpoint["tls_public_key_sha256"] == "sha256:" + hashlib.sha256(_TEST_SPKI).hexdigest()
    assert endpoint["public_host"] == "203.0.113.77"

    # GPU 상한 및 아바타 채널 검증
    assert float(w.envs["worker"]["SIMAI_WORKER_TORCH_MEMORY_FRACTION"]) == pytest.approx(0.05)
    assert float(w.envs["avatar"]["SIMAI_AVATAR_GPU_MEMORY_FRACTION"]) == pytest.approx(0.10)
    assert w.envs["tier0"]["SIMAI_AVATAR_SHARE"] == "1"
    assert w.envs["avatar"]["SIMAI_AVATAR_CHANNEL"] == "wss://127.0.0.1:19446"
    for svc_name in ("tier0", "vllm", "worker", "avatar"):
        assert w.envs[svc_name]["SIMAI_TIER0_MARKER"] == str(work / "state" / "tier0.live")
        # 워커의 site_setup·scene_regenerate 가 Tier 0 와 같은 상태 폴더를 본다(after-live 와 같은 규칙, -473)
        assert w.envs[svc_name]["SIMAI_STATE_DIR"] == str(work / "state") == cfg["state_dir"]

    # 토큰 격리 및 로그 비노출 검증
    assert w.envs["tier0"]["SIMAI_TIER0_TOKEN"] == tier0_tok
    for sidecar in ("vllm", "worker", "avatar"):
        assert "SIMAI_TIER0_TOKEN" not in w.envs[sidecar]
        assert tier0_tok not in json.dumps(w.envs[sidecar])
    for svc_name in ("tier0", "vllm", "worker", "avatar"):
        assert "SIMAI_WORKER_TOKEN" not in w.envs[svc_name]
        assert worker_tok not in json.dumps(w.envs[svc_name])
        assert tier0_tok not in " ".join(w.argvs[svc_name])
        assert worker_tok not in " ".join(w.argvs[svc_name])
    worker_env_path = work / ".private" / "worker.env"
    assert worker_env_path.is_file()
    assert worker_env_path.read_text(encoding="utf-8").strip() == f"SIMAI_WORKER_TOKEN={worker_tok}"
    assert w.argvs["worker"][:4] == ["simai-wait-tier0", "--env-file", str(worker_env_path), "--"]
    assert w.argvs["avatar"][:4] == ["simai-wait-tier0", "--env-file", str(worker_env_path), "--"]

    # pod.jsonl 기록 검증
    pod_rows = [json.loads(line) for line in (work / "pod.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(pod_rows) == 1 and pod_rows[0]["gpu_priority"]["written_at"] == 15_000

    captured = capsys.readouterr()
    assert tier0_tok not in captured.out and tier0_tok not in captured.err
    assert worker_tok not in captured.out and worker_tok not in captured.err
    assert "SIMAI_SERVICES=tier0,vllm,worker 는 lobby 에서 쓰지 않는다" in captured.err
    assert "워커 ID worker-s10 (토큰 파일 이름 worker-s10.token)" in captured.err


def test_check_preflight_verifies_multiple_sets_budget_and_runtime_and_fails_nonzero(bs, tmp_path, monkeypatch, capsys):
    """기동 전 점검(preflight / check --sets): 다중 가중치·GPU 예산·런타임을 모두 검증하고 실패 시 비-0 으로 끝난다 (S15P31A101-382)."""
    root = tmp_path / "simai"
    manifest = tmp_path / "weights.json"
    manifest.write_text(json.dumps({
        "weights": [
            {"name": "w-t0", "sets": ["tier0"], "path": "weights/t0.bin", "source": {"url": "x"}, "sha256": "0" * 64},
            {"name": "w-av", "sets": ["avatar"], "path": "weights/av.bin", "source": {"url": "x"}, "sha256": "0" * 64},
            {"name": "w-st", "sets": ["static"], "path": "weights/st.bin", "source": {"url": "x"}, "sha256": "0" * 64},
            {"name": "w-vl", "sets": ["vlm"], "path": "weights/vl.bin", "source": {"url": "x"}, "sha256": "0" * 64},
        ]
    }), encoding="utf-8")
    monkeypatch.setenv("SIMAI_ROOT", str(root))
    monkeypatch.setenv("SIMAI_WEIGHTS_MANIFEST", str(manifest))
    monkeypatch.setenv("SIMAI_MIN_FREE_GB", "0.001")
    monkeypatch.setattr(bs, "detect_gpu", lambda smi: {"name": "NVIDIA H100", "arch": "sm_90", "memory_total_mb": 81559.0})
    monkeypatch.setattr(bs, "ensure_weights", lambda r, entries: [{"name": e["name"], "status": "present"} for e in entries])

    # 1) 모든 세트·예산·런타임(python, json/sys) 정상 → 종료 코드 0
    rc = bs.main([
        "preflight",
        "--require-bins", Path(sys.executable).name,
        "--require-modules", "json,math",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["exit"] == 0
    assert set(out["sets"]) == {"tier0", "avatar", "static", "vlm"}
    assert out["gpu_budget"]["worker_fraction"] == pytest.approx(0.05)
    assert out["runtime"]["modules"] == {"json": True, "math": True}

    # 2) 필수 바이너리 누락 → 종료 코드 2 (chain-s9.sh 처럼 0 으로 삼키지 않는다)
    rc_bin = bs.main([
        "preflight",
        "--require-bins", "nonexistent-texrecon-binary-xyz",
        "--require-modules", "json",
    ])
    out_bin = json.loads(capsys.readouterr().out)
    assert rc_bin == bs.EXIT_STORAGE and out_bin["exit"] == bs.EXIT_STORAGE
    assert "nonexistent-texrecon-binary-xyz" in out_bin["error"]

    # 3) 필수 파이썬 모듈 누락 → 종료 코드 2
    rc_mod = bs.main([
        "check",
        "--sets", "tier0,static",
        "--require-modules", "nonexistent_open3d_module_xyz",
    ])
    out_mod = json.loads(capsys.readouterr().out)
    assert rc_mod == bs.EXIT_STORAGE and out_mod["exit"] == bs.EXIT_STORAGE

    # 4) 가중치 세트 중 하나라도 실패 → 종료 코드 2
    def fail_static(r, entries):
        if any("static" in e.get("sets", []) for e in entries):
            raise bs.BootstrapError(bs.EXIT_STORAGE, "static 가중치 손상")
        return [{"name": e["name"], "status": "present"} for e in entries]

    monkeypatch.setattr(bs, "ensure_weights", fail_static)
    rc_w = bs.main(["check", "--sets", "tier0,static"])
    out_w = json.loads(capsys.readouterr().out)
    assert rc_w == bs.EXIT_STORAGE and out_w["sets"]["static"]["status"] == "error"


def test_scene_accepts_worker_token_file_without_exposing_token_in_argv_or_logs(bs, tmp_path, capsys):
    """scene 모드는 --worker-token-file 로 워커 토큰을 파일에서만 읽어 자식 환경에 넣는다 (S15P31A101-382)."""
    tok = "scene-worker-secret-token-555"
    tok_file = tmp_path / "worker.token"
    tok_file.write_text(tok + "\n", encoding="utf-8")
    extra_env, cmd = bs._parse_scene_args(["--worker-token-file", str(tok_file), "--", "python", "-m", "services.worker", "--once"])
    assert extra_env["SIMAI_WORKER_TOKEN"] == tok
    assert cmd == ["python", "-m", "services.worker", "--once"]
    bs.log(f"scene test leak check: {tok}")
    assert tok not in capsys.readouterr().err


# ---------------------------------------------------------------- S15P31A101-414: 10차 세션 기동 결함


def test_worker_id_order_explicit_env_file_value_then_token_file_name(bs, tmp_path):
    named = tmp_path / "worker-s10.token"
    named.write_text("tok-a\n", encoding="utf-8")
    keyed = tmp_path / "worker.env"
    keyed.write_text("SIMAI_WORKER_TOKEN=tok-b\nSIMAI_WORKER_ID=worker-kv\n", encoding="utf-8")
    generic = tmp_path / "worker.token"
    generic.write_text("tok-c\n", encoding="utf-8")
    assert bs.resolve_worker_id("worker-cli", {"SIMAI_WORKER_ID": "worker-env"}, named) == ("worker-cli", "--worker-id")
    assert bs.resolve_worker_id(None, {"SIMAI_WORKER_ID": "worker-env"}, named) == ("worker-env", "SIMAI_WORKER_ID")
    assert bs.resolve_worker_id(None, {}, named, keyed)[0] == "worker-kv"  # 파일 안의 값이 파일 이름보다 앞선다
    assert bs.resolve_worker_id(None, {}, named) == ("worker-s10", "토큰 파일 이름 worker-s10.token")
    assert bs.resolve_worker_id(None, {}, generic)[0] is None  # worker.token 같은 흔한 이름은 ID 로 보지 않는다
    assert bs.resolve_worker_id(None, {}, tmp_path / "Worker_S10.token")[0] is None  # EC2 규칙(소문자·숫자·-) 밖
    assert bs.resolve_worker_id(None, {})[0] is None
    with pytest.raises(bs.BootstrapError) as exc:
        bs.resolve_worker_id("Worker 01", {})
    assert exc.value.code == bs.EXIT_USAGE


def test_tier0_supervise_mode_gives_worker_id_from_token_file_name_and_image_pythonpath(bs, tmp_path, monkeypatch):
    tok = tmp_path / "worker-s10.token"
    tok.write_text("worker-secret-414\n", encoding="utf-8")
    ai_dir = tmp_path / "opt-ai"
    (ai_dir / "services").mkdir(parents=True)
    w = World()
    sup = _sup(bs, tmp_path, monkeypatch, w, services=("tier0", "vllm", "worker", "avatar"),
               env={"SIMAI_WORKER_TOKEN_FILE": str(tok), "SIMAI_CACHE_ROOT": str(tmp_path / "cache"),
                    "SIMAI_AI_DIR": str(ai_dir), "PYTHONPATH": str(tmp_path / "repo-copy")})
    for name in ("worker", "avatar"):
        assert sup.svc[name].env["SIMAI_WORKER_ID"] == "worker-s10"
    assert "SIMAI_WORKER_ID" not in sup.svc["tier0"].env
    for name in ("tier0", "vllm", "worker", "avatar"):  # 저장소 사본(앞)이 이미지 ai 폴더(끝)보다 앞선다
        assert sup.svc[name].env["PYTHONPATH"] == os.pathsep.join([str(tmp_path / "repo-copy"), str(ai_dir)])
        assert "worker-secret-414" not in json.dumps(sup.svc[name].env)
    assert "워커 ID worker-s10 (토큰 파일 이름 worker-s10.token)" in sup.worker_id_note

    # ID 를 정하지 못하면 워커 기본값(worker-01)을 쓴다는 경고를 남긴다
    plain = _sup(bs, tmp_path, monkeypatch, World(), services=("tier0", "worker"), env={"SIMAI_AI_DIR": str(tmp_path / "none")})
    assert "SIMAI_WORKER_ID" not in plain.svc["worker"].env and "PYTHONPATH" not in plain.svc["worker"].env
    assert "401" in plain.worker_id_note and "worker-01" in plain.worker_id_note


def test_preflight_budgets_lobby_services_and_reports_py_spy_without_failing(bs, tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "weights.json"
    manifest.write_text(json.dumps({"weights": [
        {"name": f"w-{s}", "sets": [s], "path": f"weights/{s}.bin", "source": {"url": "x"}, "sha256": "0" * 64}
        for s in ("tier0", "avatar", "static", "vlm")]}), encoding="utf-8")
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "simai"))
    monkeypatch.setenv("SIMAI_WEIGHTS_MANIFEST", str(manifest))
    monkeypatch.setenv("SIMAI_MIN_FREE_GB", "0.001")
    monkeypatch.setenv("SIMAI_SERVICES", "tier0,vllm,worker")  # 통합 이미지 ENV: preflight 는 lobby 기본(아바타 포함)으로 본다
    monkeypatch.setattr(bs, "detect_gpu", lambda smi: {"name": "NVIDIA H100", "arch": "sm_90", "memory_total_mb": 81559.0})
    monkeypatch.setattr(bs, "ensure_weights", lambda r, entries: [{"name": e["name"], "status": "present"} for e in entries])
    real_which = bs.shutil.which
    monkeypatch.setattr(bs.shutil, "which", lambda name: "/usr/local/bin/py-spy" if name == "py-spy" else real_which(name))
    monkeypatch.setattr(bs, "ptrace_capable", lambda: False)  # RunPod: SYS_PTRACE 없음
    rc = bs.main(["preflight", "--require-bins", Path(sys.executable).name, "--require-modules", "json"])
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert rc == 0 and out["exit"] == 0
    assert out["gpu_budget"]["worker_fraction"] == pytest.approx(0.05)  # avatar 0.10 을 뺀 값
    assert out["diagnostics"]["py_spy"]["status"] == "unavailable"
    assert "내장 계측" in cap.err and "실패 아님" in cap.err

    monkeypatch.setattr(bs, "ptrace_capable", lambda: True)
    assert bs.main(["preflight", "--require-bins", Path(sys.executable).name, "--require-modules", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["diagnostics"]["py_spy"]["status"] == "ok"


def test_ptrace_capability_reads_cap_eff_bit_19(bs, tmp_path):
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nCapEff:\t00000000a80425fb\n", encoding="ascii")  # Docker 기본 권한(SYS_PTRACE 없음)
    assert bs.ptrace_capable(status) is False
    status.write_text("CapEff:\t00000000a80c25fb\n", encoding="ascii")  # + CAP_SYS_PTRACE(1<<19)
    assert bs.ptrace_capable(status) is True
    assert bs.ptrace_capable(tmp_path / "missing") is None


def test_lobby_explicit_services_without_avatar_warns_and_bad_worker_id_stops_before_prepare(bs, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bs, "prepare", lambda want_set: (_ for _ in ()).throw(AssertionError("준비하면 안 된다")))
    monkeypatch.setattr(bs, "claim_tier0", lambda root: (_ for _ in ()).throw(AssertionError("잠그면 안 된다")))
    assert bs.main(["lobby", "--bundle-dir", str(tmp_path), "--worker-id", "Worker_01"]) == bs.EXIT_USAGE
    assert "--worker-id" in capsys.readouterr().err
    bs._warn_lobby_services("tier0,vllm,worker", None, ["tier0", "vllm", "worker"])
    assert "아바타를 띄우지 않는다" in capsys.readouterr().err
