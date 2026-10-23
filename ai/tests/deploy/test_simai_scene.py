"""simai-bootstrap 장면 준비 모드(scene, S15P31A101-250) 반례 시험. GPU·네트워크·Docker 를 쓰지 않는다(가짜 nvidia-smi, 가짜 받기, 실제 자식 프로세스).

반례: LIVE 표시 파일이 새것이면 받기 전에 거부(75), 받는 동안 Tier 0 가 뜨면 명령 직전에 거부, 표시 파일 경계(30초)·미래 시각·확인 실패,
GPU 사용 중 메모리 경계(1024 MiB)·못 읽음·NaN(모름 → 거부), SIMAI_SERVICES 에 scene 을 섞으면 받기 전에 사용법 오류, SIMAI_WEIGHTS_SET 과 상관없이
static 묶음, HF 토큰은 받기에만(명령 환경·출력·기록에 없음), 명령 없음, 명령의 종료 코드가 그대로 나감, 거부 기록 75.
Tier 0 와 상호 배제(Codex 리뷰 R1, Tier 0 우선): 최종 GPU 조회 도중 Tier 0 시작, 자식을 띄우는 순간 시작, 실행 중 시작 → 장면 작업이 끝나고(75)
Tier 0 시작 경로는 장면 잠금이 풀리기를 기다렸다 진행, 두 장면 작업 동시 거부, tier0 명령의 순서(시작 잠금 → 준비 → 장면 대기 → exec).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PY = Path(sys.executable).as_posix()
FAKE_SMI = (REPO / "ai" / "tests" / "experiments" / "gpu_queue" / "fake_nvidia_smi.py").as_posix()
TOKEN = "hf_" + "Sc3neToken" * 4
BLOB = {"ma": b"fake-map-anything" * 500, "apache": b"fake-map-anything-apache" * 500}
# 장면 작업 대역: 시작하면 flag 에 {pid, token} 을 쓰고 sleep 초 기다린 뒤 done 을 쓰고 code 로 끝난다
CHILD = ("import json, os, sys, time, pathlib\n"
         "flag, code, sleep = pathlib.Path(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])\n"
         "tok = bool({'HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN'} & set(os.environ))\n"
         "flag.write_text(json.dumps({'pid': os.getpid(), 'token': tok}))\n"
         "time.sleep(sleep)\n"
         "flag.with_suffix('.done').write_text('done')\n"
         "sys.exit(code)\n")


@pytest.fixture
def tmp_path(tmp_path):
    text = str(tmp_path)
    return Path(text[4:]) if text.startswith("\\\\?\\") else tmp_path


@pytest.fixture
def bs(bs, monkeypatch):
    monkeypatch.setattr(bs, "SCENE_POLL_S", 0.1)
    monkeypatch.setattr(bs, "SCENE_STOP_GRACE_S", 3.0)
    yield bs
    bs.release_tier0_claim()


def _setup(bs, tmp_path: Path, monkeypatch, *, used_mb: str = "0") -> dict:
    """static(장면)·tier0·exp02 묶음이 든 목록(HF 원본), 가짜 nvidia-smi(사용 중 메모리 used_mb), 가로챈 받기·exec."""
    entries = [{"name": f"map-{k}", "sets": ["static"], "path": f"weights/map-{k}/model.safetensors",
                "source": {"hf": {"repo": f"facebook/map-{k}", "file": "model.safetensors", "revision": "a" * 40}},
                "sha256": hashlib.sha256(v).hexdigest()} for k, v in BLOB.items()]
    entries += [{"name": "yolo", "sets": ["tier0"], "path": "weights/yolo/y.pt", "source": {"url": "https://example.invalid/y.pt"},
                 "sha256": "0" * 64},
                {"name": "hmr", "sets": ["exp02"], "path": "weights/hmr/h.ckpt", "source": {"url": "https://example.invalid/h"},
                 "sha256": "1" * 64}]
    manifest = tmp_path / "weights.json"
    manifest.write_text(json.dumps({"manifest_version": 1, "weights": entries}), encoding="utf-8")
    used = tmp_path / "used_mb.txt"
    used.write_text(used_mb, encoding="utf-8")
    for k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "SIMAI_WEIGHTS_SET", "SIMAI_TIER0_MARKER", "FAKE_SMI_FAIL", "SIMAI_SERVICES"):
        monkeypatch.delenv(k, raising=False)
    root = tmp_path / "simai"
    monkeypatch.setenv("SIMAI_ROOT", str(root))
    monkeypatch.setenv("SIMAI_NVIDIA_SMI", f"{PY} {FAKE_SMI} {used.as_posix()}")
    monkeypatch.setenv("SIMAI_WEIGHTS_MANIFEST", str(manifest))
    monkeypatch.setenv("SIMAI_MIN_FREE_GB", "0")
    events: list = []

    def fake_open(req):
        repo = req.full_url.split("/")[3:5]
        events.append(("get", "/".join(repo), req.unredirected_hdrs.get("Authorization")))
        return io.BytesIO(BLOB["apache" if repo[1].endswith("apache") else "ma"])

    monkeypatch.setattr(bs, "urlopen", fake_open)
    monkeypatch.setattr(bs, "execvp", lambda argv: events.append(("exec", list(argv))))
    return {"events": events, "root": root, "used": used, "marker": root / "state" / "tier0.live", "flag": tmp_path / "child.json"}


def _cmd(st: dict, code: int = 0, sleep: float = 0.0) -> list[str]:
    return [PY, "-c", CHILD, str(st["flag"]), str(code), str(sleep)]


def _ran(st: dict) -> bool:
    return st["flag"].exists()


def _done(st: dict) -> bool:
    return st["flag"].with_suffix(".done").exists()


def _live_marker(path: Path, age_s: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"gpu_priority": {"phase": "running", "reserve": {"state": "OK"}}}), encoding="utf-8")
    t = time.time() - age_s
    os.utime(path, (t, t))


def _records(root: Path) -> list[dict]:
    d = root / "runs" / "bootstrap"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def _gets(st: dict) -> list:
    return [e for e in st["events"] if e[0] == "get"]


def test_scene_prepares_static_weights_then_runs_command(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setenv("SIMAI_WEIGHTS_SET", "exp02")  # 실험 묶음을 정해 둔 이미지여도 장면 모드는 static
    monkeypatch.setenv("SIMAI_SERVICES", "tier0,vllm,worker")  # 통합 이미지 기본값: tier0 명령만 쓰므로 장면 모드는 보지 않는다
    assert bs.main(["scene", "--", *_cmd(st)]) == 0
    assert sorted(e[1] for e in _gets(st)) == ["facebook/map-apache", "facebook/map-ma"]  # tier0·exp02 항목은 받지 않는다
    assert _ran(st) and _done(st)
    assert (st["root"] / "weights" / "map-ma" / "model.safetensors").read_bytes() == BLOB["ma"]
    rec = _records(st["root"])[0]
    assert rec["set"] == "static" and rec["exit"] == 0
    assert (st["root"] / "logs" / "scene.log").is_file()


def test_fresh_live_marker_refuses_before_downloading(bs, tmp_path, monkeypatch, capsys):
    st = _setup(bs, tmp_path, monkeypatch)
    _live_marker(st["marker"], age_s=2)
    assert bs.main(["scene", *_cmd(st)]) == bs.EXIT_BUSY == 75
    assert st["events"] == [] and not _ran(st)  # 받지도 띄우지도 않았다
    assert [r["exit"] for r in _records(st["root"])] == [75]  # 준비 기록(exit 0) 없이 거부 기록만
    assert "LIVE" in capsys.readouterr().err


def test_tier0_starting_during_download_refuses_before_running(bs, tmp_path, monkeypatch):
    """받는 동안(수 GB, 수 분) Tier 0 가 떠 표시 파일을 쓰면 명령 직전 재확인이 막는다."""
    st = _setup(bs, tmp_path, monkeypatch)
    orig = bs.urlopen

    def open_and_start_tier0(req):
        _live_marker(st["marker"])
        return orig(req)

    monkeypatch.setattr(bs, "urlopen", open_and_start_tier0)
    assert bs.main(["scene", *_cmd(st)]) == bs.EXIT_BUSY
    assert not _ran(st) and _gets(st)


@pytest.mark.parametrize(("age_s", "busy"), [(29.0, True), (31.0, False), (-60.0, True)], ids=["29s-live", "31s-stale", "future-mtime"])
def test_marker_age_boundary(bs, tmp_path, monkeypatch, age_s, busy):
    st = _setup(bs, tmp_path, monkeypatch)
    _live_marker(st["marker"], age_s=age_s)
    rc = bs.main(["scene", *_cmd(st)])
    assert (rc == bs.EXIT_BUSY) is busy and (not _ran(st)) is busy


def test_custom_marker_path_is_honoured(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    other = tmp_path / "elsewhere" / "tier0.live"
    monkeypatch.setenv("SIMAI_TIER0_MARKER", str(other))
    _live_marker(other)
    assert bs.main(["scene", *_cmd(st)]) == bs.EXIT_BUSY and st["events"] == []


def test_marker_that_cannot_be_checked_is_treated_as_live(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    real_stat = Path.stat

    def stat(self, *a, **k):
        if self == st["marker"]:
            raise PermissionError("denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", stat)
    why = bs.scene_busy(st["root"], None)
    assert why is not None and "모름" in why


@pytest.mark.parametrize(("used", "busy"), [("1024", False), ("1025", True), ("[N/A]", True), ("", True)],
                         ids=["1024-ok", "1025-busy", "na-unknown", "empty-unknown"])
def test_gpu_memory_in_use_boundary_and_unknown(bs, tmp_path, monkeypatch, used, busy):
    """표시 파일로 안 보이는 GPU 사용(엔진 빌드·REPLAY·다른 컨테이너)은 사용 중 메모리로 본다. 못 읽으면 거부."""
    st = _setup(bs, tmp_path, monkeypatch, used_mb=used)
    rc = bs.main(["scene", *_cmd(st)])
    assert (rc == bs.EXIT_BUSY) is busy and (not _ran(st)) is busy


def test_gpu_query_failure_is_unknown(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    assert bs.gpu_memory_used_mb([(tmp_path / "no-such-smi").as_posix()]) is None
    assert bs.gpu_memory_used_mb([PY, "-c", "import sys; print('512'); sys.exit(1)"]) is None  # 종료 코드가 0 이 아니면 값이 있어도 모름
    assert bs.gpu_memory_used_mb([PY, "-c", "print('10'); print('2048')"]) == 2048  # GPU 여러 장이면 가장 많이 쓴 것
    assert bs.scene_busy(st["root"], [PY, "-c", "print('10'); print('2048')"]) is not None


@pytest.mark.parametrize("services", ["tier0,scene", "tier0,vllm,worker,scene", "scene"])
def test_scene_mixed_into_live_services_is_usage_error_before_downloading(bs, tmp_path, monkeypatch, capsys, services):
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setenv("SIMAI_SERVICES", services)
    assert bs.main(["tier0"]) == bs.EXIT_USAGE
    assert st["events"] == [] and "simai-bootstrap scene" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["scene"], ["scene", "--"]])
def test_scene_without_command_is_usage_error(bs, tmp_path, monkeypatch, args):
    st = _setup(bs, tmp_path, monkeypatch)
    assert bs.main(args) == bs.EXIT_USAGE and st["events"] == []


def test_hf_token_only_for_download(bs, tmp_path, monkeypatch, capsys):
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", TOKEN)
    assert bs.main(["scene", *_cmd(st)]) == 0
    assert {e[2] for e in _gets(st)} == {f"Bearer {TOKEN}"}
    assert json.loads(st["flag"].read_text(encoding="utf-8"))["token"] is False  # 장면 작업 환경에 토큰 없음
    out = capsys.readouterr()
    logs = (st["root"] / "logs" / "scene.log").read_text(encoding="utf-8")
    rec_text = "".join(p.read_text(encoding="utf-8") for p in (st["root"] / "runs" / "bootstrap").glob("*.json"))
    assert TOKEN not in out.out + out.err + rec_text + logs


def test_weights_failure_keeps_weights_exit_code_and_does_not_run(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "urlopen", lambda req: io.BytesIO(b"tampered"))
    assert bs.main(["scene", *_cmd(st)]) == bs.EXIT_STORAGE and not _ran(st)


@pytest.mark.parametrize("code", [0, 7, 1])
def test_command_exit_code_passes_through(bs, tmp_path, monkeypatch, code):
    st = _setup(bs, tmp_path, monkeypatch)
    assert bs.main(["scene", *_cmd(st, code=code)]) == code and _done(st)


def test_missing_command_binary_exits_127(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    assert bs.main(["scene", (tmp_path / "no-such-binary").as_posix()]) == bs.EXIT_NOT_FOUND and not _ran(st)


def test_command_exit_code_passes_through_real_process(tmp_path):
    """실제 프로세스: 가짜 nvidia-smi·file:// 가중치로 준비한 뒤 장면 작업의 종료 코드가 simai-bootstrap 의 종료 코드다(토큰은 넘기지 않는다)."""
    blob = b"x" * 1000
    src = tmp_path / "w.bin"
    src.write_bytes(blob)
    manifest = tmp_path / "w.json"
    manifest.write_text(json.dumps({"weights": [{"name": "m", "sets": ["static"], "path": "weights/m/w.bin",
                                                 "source": {"url": src.as_uri()}, "sha256": hashlib.sha256(blob).hexdigest()}]}))
    used = tmp_path / "used.txt"
    used.write_text("0")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SIMAI_", "HF_", "HUGGING"))}
    env |= {"SIMAI_ROOT": str(tmp_path / "simai"), "SIMAI_NVIDIA_SMI": f"{PY} {FAKE_SMI} {used.as_posix()}", "SIMAI_WEIGHTS_MANIFEST": str(manifest),
            "SIMAI_MIN_FREE_GB": "0", "HF_TOKEN": TOKEN}
    r = subprocess.run([PY, str(REPO / "deploy" / "scripts" / "simai_bootstrap.py"), "scene", PY, "-c",
                        "import os, sys; sys.exit(9 if 'HF_TOKEN' in os.environ else 7)"], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 7, r.stderr


# ---------------------------------------------------------------- 리뷰 반례(Argon -250): 받기 전 GPU 확인, 거부 기록, NaN 메모리


def test_gpu_in_use_refuses_before_downloading(bs, tmp_path, monkeypatch):
    """표시 파일이 없는 Tier 0 엔진 빌드·다른 컨테이너: 수 GB 를 받기 전에 GPU 사용 중 메모리로 거부한다."""
    st = _setup(bs, tmp_path, monkeypatch, used_mb="30000")
    assert bs.main(["scene", *_cmd(st)]) == bs.EXIT_BUSY
    assert st["events"] == [] and not _ran(st)  # 받지 않았다
    assert [r["exit"] for r in _records(st["root"])] == [75]


def test_refusal_after_prepare_appends_exit_75_record(bs, tmp_path, monkeypatch):
    """받는 동안 GPU 를 누가 잡으면 준비 기록(exit 0) 뒤에 거부 기록(75)이 추가로 남는다(준비 기록만 보고 성공으로 읽지 않게)."""
    st = _setup(bs, tmp_path, monkeypatch)
    orig = bs.urlopen

    def open_and_grab_gpu(req):
        st["used"].write_text("40000", encoding="utf-8")
        return orig(req)

    monkeypatch.setattr(bs, "urlopen", open_and_grab_gpu)
    assert bs.main(["scene", *_cmd(st)]) == bs.EXIT_BUSY and not _ran(st)
    recs = _records(st["root"])
    assert [r["exit"] for r in recs] == [0, 75] and recs[-1]["stage"] == "scene" and "GPU" in recs[-1]["error"]


@pytest.mark.parametrize("out", ["nan", "inf", "-5", "10\nnan"])
def test_non_finite_gpu_memory_is_unknown(bs, tmp_path, monkeypatch, out):
    """float('nan') 은 읽히고 내장 max 는 NaN 을 버린다: 비유한·음수는 모름(→ 명령 직전 거부)."""
    st = _setup(bs, tmp_path, monkeypatch)
    smi = [PY, "-c", f"print({out!r})"]
    assert bs.gpu_memory_used_mb(smi) is None
    assert bs.scene_busy(st["root"], smi) is not None


def test_no_gpu_before_prepare_still_exits_3_not_75(bs, tmp_path, monkeypatch):
    """준비 전 GPU 메모리를 못 읽는 것(GPU 없음)은 준비의 GPU 단계가 코드 3 으로 알린다(75 로 바꿔 말하지 않는다)."""
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SMI_FAIL", "1")
    assert bs.main(["scene", *_cmd(st)]) == bs.EXIT_GPU and st["events"] == []


# ---------------------------------------------------------------- Codex 리뷰 R1: Tier 0 와 상호 배제(Tier 0 우선)


def test_tier0_claim_during_final_gpu_query_refuses(bs, tmp_path, monkeypatch):
    """최종 GPU 조회(최대 60초) 도중 Tier 0 가 시작해 아직 메모리를 거의 안 잡았어도(0 MiB) 시작 잠금으로 보고 거부한다."""
    st = _setup(bs, tmp_path, monkeypatch)
    calls = []

    def gpu(smi):
        calls.append(1)
        if len(calls) == 2:  # 준비 뒤 최종 조회
            bs.claim_tier0(st["root"])
        return 0.0

    monkeypatch.setattr(bs, "gpu_memory_used_mb", gpu)
    assert bs.main(["scene", *_cmd(st)]) == bs.EXIT_BUSY
    assert len(calls) == 2 and not _ran(st)
    assert _records(st["root"])[-1]["exit"] == 75


def test_tier0_claim_right_before_spawn_stops_the_scene(bs, tmp_path, monkeypatch):
    """확인을 다 통과한 직후 자식을 띄우는 순간 Tier 0 가 시작하면 실행 감시가 첫 주기에 장면 작업을 끝낸다."""
    st = _setup(bs, tmp_path, monkeypatch)
    real = bs.Proc

    def spawn(*a, **k):
        bs.claim_tier0(st["root"])
        return real(*a, **k)

    monkeypatch.setattr(bs, "Proc", spawn)
    t0 = time.monotonic()
    assert bs.main(["scene", *_cmd(st, sleep=60)]) == bs.EXIT_BUSY
    assert time.monotonic() - t0 < 30 and not _done(st)  # 60초 작업을 끝까지 두지 않았다
    assert "선점" in _records(st["root"])[-1]["error"]


def test_tier0_starting_while_scene_runs_preempts_and_tier0_proceeds(bs, tmp_path, monkeypatch):
    """실행 중 Tier 0 시작: Tier 0 시작 경로(시작 잠금 → 장면 대기)가 장면 작업이 끝나기를 기다렸다 진행하고, 장면 모드는 75 로 끝난다."""
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "SCENE_POLL_S", 1.5)  # 감시 주기를 늘려, Tier 0 가 기다리지 않으면 아직 살아 있는 장면 작업이 보이게 한다
    result: dict = {}
    th = threading.Thread(target=lambda: result.update(rc=bs.main(["scene", *_cmd(st, sleep=60)])))
    th.start()
    deadline = time.monotonic() + 60
    while not _ran(st) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _ran(st), "장면 작업이 시작하지 않았다"
    child = json.loads(st["flag"].read_text(encoding="utf-8"))["pid"]
    assert bs._proc_alive(child)  # 생존 조회가 실제로 산 프로세스를 산 것으로 본다(조회 실패를 '없음'으로 읽지 않는다, C3)
    t0 = time.monotonic()
    bs.claim_tier0(st["root"])  # Tier 0 가 시작한다(tier0 명령의 첫 단계)
    bs.wait_scene_released(st["root"])  # Tier 0 는 장면 작업이 끝날 때까지 기다렸다 진행한다
    waited = time.monotonic() - t0
    assert not bs._proc_alive(child) and not _done(st)  # Tier 0 가 진행하는 시점에는 장면 작업이 이미 끝나 있다(VRAM 반환)
    th.join(30)
    assert not th.is_alive() and result["rc"] == bs.EXIT_BUSY
    assert waited < bs.SCENE_PREEMPT_WAIT_S


def test_second_scene_is_refused_while_one_runs(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    lock = bs._open_lock(st["root"], bs.SCENE_LOCK)
    try:
        assert bs._try_lock(lock)
        assert bs.main(["scene", *_cmd(st)]) == bs.EXIT_BUSY and st["events"] == []
    finally:
        lock.close()


def test_tier0_command_claims_before_prepare_and_waits_for_scene_before_exec(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    order = []
    monkeypatch.setattr(bs, "claim_tier0", lambda root: order.append("claim"))
    monkeypatch.setattr(bs, "wait_scene_released", lambda root: order.append("wait"))

    def prepare(want):
        order.append(f"prepare:{want}")
        os.environ["SIMAI_ROOT"] = str(st["root"])
        return {"gpu": {"memory_total_mb": 81559}}

    monkeypatch.setattr(bs, "prepare", prepare)
    monkeypatch.setattr(bs, "execvp", lambda argv: order.append(f"exec:{argv[0]}"))
    assert bs.main(["tier0"]) == 0
    assert order == ["claim", "prepare:tier0", "wait", "exec:simai-tier0"]


def test_tier0_claim_does_not_block_when_lock_unavailable(bs, tmp_path, monkeypatch):
    """시작 잠금을 열지 못해도(권한 등) Tier 0 는 막히지 않는다(Tier 0 우선)."""
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "_open_lock", lambda root, rel: (_ for _ in ()).throw(PermissionError("denied")))
    bs.claim_tier0(st["root"])
    bs.wait_scene_released(st["root"])
    assert bs.tier0_claimed(st["root"]) is None  # 장면 쪽은 모름 → Tier 0 가 있는 것으로 본다
    assert bs.tier0_present(st["root"]) is not None


# ---------------------------------------------------------------- Codex 재리뷰 R1 잔여: 감시 부모 정지·소멸 때 Tier 0 가 장면 그룹을 회수


BASE_PY = Path(getattr(sys, "_base_executable", sys.executable)).as_posix()  # Windows venv 실행기 없이(pid 가 실제 파이썬)


def _group_child(sleep: float = 60.0) -> subprocess.Popen:
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return subprocess.Popen([BASE_PY, "-c", f"import time; time.sleep({sleep})"], **kw)


def _gone(bs, pid: int, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while bs._proc_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def test_stalled_monitor_scene_group_is_reaped_by_tier0(bs, tmp_path, monkeypatch):
    """감시 부모가 멈춰 장면 잠금을 쥔 채 응답하지 않으면, 시한 뒤 Tier 0 가 기록된 그룹을 직접 회수하고 사라진 것을 본 뒤 진행한다."""
    _require_terminate(bs)
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "SCENE_PREEMPT_WAIT_S", 1.0)
    child = _group_child()
    lock = bs._open_lock(st["root"], bs.SCENE_LOCK)  # 멈춘 감시 부모 대역: 잠금을 쥐고 아무것도 안 한다
    try:
        assert bs._try_lock(lock)
        bs._write_scene_pgid(st["root"], child.pid)
        assert bs._proc_alive(child.pid)
        bs.claim_tier0(st["root"])
        bs.wait_scene_released(st["root"])
        assert child.poll() is not None or _gone(bs, child.pid)  # 돌아온 시점에 그룹이 없다
        assert not st["root"].joinpath(*bs.SCENE_PGID).exists()
        rec = [r for r in _records(st["root"]) if r.get("stage") == "tier0-reap-scene"]
        assert rec and rec[-1]["exit"] == 0
    finally:
        lock.close()
        if child.poll() is None:
            child.kill()


def test_reused_pid_with_different_start_is_not_killed(bs, tmp_path, monkeypatch):
    """기록의 pid 를 다른 프로세스가 다시 쓰고 있으면(시작 표지가 다름) 회수하지 않는다."""
    st = _setup(bs, tmp_path, monkeypatch)
    other = _group_child(30)
    try:
        bs._write_scene_pgid(st["root"], other.pid)
        path = st["root"].joinpath(*bs.SCENE_PGID)
        d = json.loads(path.read_text(encoding="utf-8"))
        d["start"] = "bogus-start"
        path.write_text(json.dumps(d), encoding="utf-8")
        bs.wait_scene_released(st["root"])
        time.sleep(0.3)
        assert other.poll() is None and bs._proc_alive(other.pid)
    finally:
        other.kill()


def _scene_monitor(st: dict, tmp_path: Path) -> subprocess.Popen:
    """실제 simai-bootstrap scene 감시 부모(가짜 GPU·file:// 가중치) + 60초 장면 자식."""
    blob = b"y" * 1000
    src = tmp_path / "w.bin"
    src.write_bytes(blob)
    manifest = tmp_path / "w.json"
    manifest.write_text(json.dumps({"weights": [{"name": "m", "sets": ["static"], "path": "weights/m/w.bin",
                                                 "source": {"url": src.as_uri()}, "sha256": hashlib.sha256(blob).hexdigest()}]}))
    used = tmp_path / "used0.txt"
    used.write_text("0")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SIMAI_", "HF_", "HUGGING"))}
    env |= {"SIMAI_ROOT": str(st["root"]), "SIMAI_NVIDIA_SMI": f"{BASE_PY} {FAKE_SMI} {used.as_posix()}",
            "SIMAI_WEIGHTS_MANIFEST": str(manifest), "SIMAI_MIN_FREE_GB": "0", "PYTHONUTF8": "1"}
    mon = subprocess.Popen([BASE_PY, str(REPO / "deploy" / "scripts" / "simai_bootstrap.py"), "scene", BASE_PY, "-c", CHILD,
                            str(st["flag"]), "0", "60"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while not st["flag"].exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert st["flag"].exists(), "장면 작업이 시작하지 않았다"
    return mon


def test_dead_monitor_scene_child_is_reaped_by_tier0(bs, tmp_path, monkeypatch):
    """감시 부모가 죽어 장면 잠금만 풀려도(잠금 해제 = VRAM 반환이 아니다) Tier 0 가 남은 장면 그룹을 회수한 뒤 진행한다."""
    _require_terminate(bs)
    st = _setup(bs, tmp_path, monkeypatch)
    mon = _scene_monitor(st, tmp_path)
    child = json.loads(st["flag"].read_text(encoding="utf-8"))["pid"]
    try:
        assert bs._proc_alive(child)
        mon.kill()  # 감시 부모 강제 종료(Windows 는 자식이 남는다, Linux 는 PDEATHSIG 로 리더가 같이 끝난다)
        mon.wait(10)
        bs.claim_tier0(st["root"])
        bs.wait_scene_released(st["root"])
        assert _gone(bs, child, 15) and not _done(st)
    finally:
        if bs._proc_alive(child):
            bs._signal_group(child, kill=True)


@pytest.mark.skipif(os.name == "nt", reason="SIGSTOP·프로세스 그룹은 POSIX(Linux 컨테이너)에서만")
def test_sigstopped_monitor_scene_group_is_reaped_on_posix(bs, tmp_path, monkeypatch):
    import signal as sg

    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "SCENE_PREEMPT_WAIT_S", 1.0)
    mon = _scene_monitor(st, tmp_path)
    child = json.loads(st["flag"].read_text(encoding="utf-8"))["pid"]
    try:
        os.kill(mon.pid, sg.SIGSTOP)
        bs.claim_tier0(st["root"])
        bs.wait_scene_released(st["root"])
        assert _gone(bs, child, 15)
    finally:
        os.kill(mon.pid, sg.SIGKILL)
        mon.wait(10)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG 는 Linux 전용")
def test_scene_child_dies_with_its_monitor_on_linux(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    mon = _scene_monitor(st, tmp_path)
    child = json.loads(st["flag"].read_text(encoding="utf-8"))["pid"]
    mon.kill()
    mon.wait(10)
    assert _gone(bs, child, 10)  # Tier 0 회수 없이도 리더가 끝난다


# ---------------------------------------------------------------- Codex 3차 재리뷰: namespace, 모르면 건드리지 않음, 시작 handshake, 권한 오류, 종료 권한


def _require_terminate(bs) -> None:
    """이 환경에서 시험이 띄운 자식을 끝낼 수 있는지(리뷰어 환경은 taskkill 이 거부됐다). 못 하면 회수 시험을 skip 한다(사유 표시)."""
    probe = _group_child(30)
    try:
        bs._signal_group(probe.pid, kill=True)
        if not _gone(bs, probe.pid, 5):
            pytest.skip("이 환경은 시험이 띄운 프로세스를 끝낼 권한이 없다(taskkill·TerminateProcess 거부): 회수 시험을 돌릴 수 없다")
    finally:
        if probe.poll() is None:
            probe.kill()


def _pgid_record(bs, st: dict, pid: int, **patch) -> Path:
    bs._write_scene_pgid(st["root"], pid)
    path = st["root"].joinpath(*bs.SCENE_PGID)
    d = json.loads(path.read_text(encoding="utf-8"))
    d |= patch
    path.write_text(json.dumps(d), encoding="utf-8")
    return path


@pytest.mark.parametrize("patch, why", [
    ({"pidns": "pid:[4026539999]"}, "namespace"),  # 다른 컨테이너의 기록
    ({"pidns": None}, "namespace"),  # 기록에 namespace 없음(모름)
    ({"start": None}, "신원"),  # 기록 때 시작 표지를 못 읽음
], ids=["other-pidns", "pidns-unknown", "start-unknown"])
def test_unverifiable_scene_record_is_not_signalled_and_kept(bs, tmp_path, monkeypatch, patch, why):
    """모르면 건드리지 않는다: namespace 가 다르거나 모름, 시작 표지 모름 → 신호 없음, 기록은 남기고 경고·기록(75) 뒤 진행."""
    st = _setup(bs, tmp_path, monkeypatch)
    other = _group_child(30)
    sent = []
    monkeypatch.setattr(bs, "_signal_group", lambda pgid, kill: sent.append((pgid, kill)) or True)
    try:
        path = _pgid_record(bs, st, other.pid, **patch)
        bs.wait_scene_released(st["root"])
        assert sent == [] and other.poll() is None and path.exists()
        rec = [r for r in _records(st["root"]) if r.get("stage") == "tier0-reap-scene"]
        assert rec and rec[-1]["exit"] == 75 and why in rec[-1]["error"]
    finally:
        other.kill()


def test_leader_identity_unreadable_now_is_not_signalled(bs, tmp_path, monkeypatch):
    """지금 리더의 시작 표지를 읽을 수 없으면(리더만 먼저 끝남·권한) 남의 그룹일 수 있어 신호를 보내지 않는다."""
    st = _setup(bs, tmp_path, monkeypatch)
    other = _group_child(30)
    sent = []
    try:
        path = _pgid_record(bs, st, other.pid)
        monkeypatch.setattr(bs, "_proc_start_token", lambda pid: None)
        monkeypatch.setattr(bs, "_signal_group", lambda pgid, kill: sent.append((pgid, kill)) or True)
        bs.wait_scene_released(st["root"])
        assert sent == [] and other.poll() is None and path.exists()
    finally:
        other.kill()


def test_kill_is_skipped_when_leader_identity_vanishes_after_term(bs, tmp_path, monkeypatch):
    """TERM 뒤 KILL 직전에 리더의 시작 표지를 읽을 수 없으면(리더만 끝남·권한) 모르므로 KILL 을 보내지 않고 기록(75)을 남긴다."""
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "SCENE_STOP_GRACE_S", 0.5)
    other = _group_child(30)
    sent = []
    try:
        path = _pgid_record(bs, st, other.pid)
        start = json.loads(path.read_text(encoding="utf-8"))["start"]
        tokens = iter([start])  # 첫 신원 확인은 같고, KILL 직전 재확인은 None
        monkeypatch.setattr(bs, "_proc_start_token", lambda pid: next(tokens, None))
        monkeypatch.setattr(bs, "_signal_group", lambda pgid, kill: sent.append(kill) or True)
        monkeypatch.setattr(bs, "_wait_gone", lambda pgid, s: False)
        bs.wait_scene_released(st["root"])
        assert sent == [False]  # TERM 만, KILL 없음
        rec = [r for r in _records(st["root"]) if r.get("stage") == "tier0-reap-scene"]
        assert rec and rec[-1]["exit"] == 75
    finally:
        other.kill()


def test_reap_failure_warns_records_75_and_tier0_proceeds(bs, tmp_path, monkeypatch, capsys):
    """회수하지 못하면(신호 거부) 경고와 기록(75)을 남기고 Tier 0 시작 경로는 그대로 돌아온다(Tier 0 우선, 의도된 정책)."""
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "SCENE_STOP_GRACE_S", 0.5)
    monkeypatch.setattr(bs, "_wait_gone", lambda pgid, s: False)
    monkeypatch.setattr(bs, "_signal_group", lambda pgid, kill: False)
    other = _group_child(30)
    try:
        _pgid_record(bs, st, other.pid)
        bs.wait_scene_released(st["root"])  # 돌아온다(막지 않는다)
        rec = [r for r in _records(st["root"]) if r.get("stage") == "tier0-reap-scene"]
        assert rec and rec[-1]["exit"] == 75 and "거부" in rec[-1]["error"]
        assert "경고" in capsys.readouterr().err
    finally:
        other.kill()


def test_pgid_write_failure_does_not_start_the_scene(bs, tmp_path, monkeypatch):
    """그룹 기록을 쓰지 못하면 장면 작업을 시작시키지 않고(POSIX 는 handshake 전이라 명령이 돌지 않는다) 75."""
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "_write_scene_pgid", lambda root, pid: False)
    assert bs.main(["scene", *_cmd(st, sleep=30)]) == bs.EXIT_BUSY
    assert "기록" in _records(st["root"])[-1]["error"] and not _done(st)
    if os.name != "nt":
        assert not _ran(st)


@pytest.mark.skipif(os.name == "nt", reason="handshake 는 POSIX exec 경로(Windows exec 는 pid 를 바꾼다)")
@pytest.mark.parametrize("feed, code, runs", [(b"g", 7, True), (b"", 75, False), (b"x", 75, False)], ids=["go", "eof", "wrong-byte"])
def test_scene_handshake_runs_command_only_after_go(bs, tmp_path, feed, code, runs):
    flag = tmp_path / "hs.json"
    r = subprocess.run([sys.executable, "-c", bs.SCENE_HANDSHAKE, sys.executable, "-c", CHILD, str(flag), "7", "0"],
                       input=feed, capture_output=True, timeout=60)
    assert r.returncode == code and flag.exists() is runs


@pytest.mark.skipif(os.name == "nt", reason="/proc 는 Linux")
def test_proc_permission_error_means_alive_not_gone(bs, monkeypatch):
    """/proc/<pid>/stat 을 읽을 권한이 없으면(EACCES·EPERM) 그 프로세스가 그 그룹일 수 있어 살아 있음(모름)으로 본다."""
    real = Path.read_text

    def read_text(self, *a, **k):
        if self.name == "stat" and self.parent.parent == Path("/proc"):
            raise PermissionError("denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert bs._group_alive(999_999_999) is True
