"""GPU 1차 세션 큐 (S15P31A101-183) 반례 시험: 노트북에서 가짜 항목(CPU 명령)으로 세션 실행기를 돌린다.

반례: 항목 제한 시간 초과(다음 항목은 돈다), 실패 항목이 다음 항목을 막지 않음, 전체 시간 상한, 중간 Ctrl-C 와 이어 돌기,
사전 점검(용량 부족·결과 폴더에 쓸 수 없음), 실행 중 디스크 가득 참(큐 예외 → 묶음은 남김), 묶음 폴더 쓰기 실패(임시 폴더로),
비밀 파일·비밀 값·심볼릭 링크가 결과 묶음에 섞이지 않음, 자리만 있는 항목은 큐에 넣지 않음(SUCCEEDED 로 꾸미지 않음),
EXP 큐(g)는 LIVE 표시 파일이 있으면 돌지 않음. Pod 모드 Job 파일이 gpu_queue 형식·Run 기록 스키마를 통과하는지도 본다.
"""

from __future__ import annotations

import _thread
import errno
import json
import os
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest

from simai_ai.providers.contract import schema_errors

PY = sys.executable
SECRET_TOKEN = "hf_" + "Q" * 34
PRIVATE_KEY = "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEEIFakeFakeFake\n-----END EC PRIVATE KEY-----\n"


def cpu_item(key: str, name: str, code: str, *, timeout_s: float = 30, expected_s: float = 1) -> dict:
    return {"key": key, "name": name, "jira": "S15P31A101-183", "title": name, "command": [PY, "-c", code],
            "expected_s": expected_s, "timeout_s": timeout_s, "model_version": "cpu",
            "provenance": {"frames": {"source": "SIM", "dataset_id": "none"}, "is_upper_bound": True}}


def touch(name: str) -> str:
    """Job 이 $SIMAI_RUN_DIR 의 부모(runs/) 옆 out/ 에 자기 이름을 남긴다(어느 항목이 돌았는지)."""
    return (f"import os, pathlib; d = pathlib.Path(os.environ['SIMAI_RUN_DIR']).parents[3] / 'out'; d.mkdir(exist_ok=True);"
            f" (d / '{name}').write_text('x')")


def ran(root: Path) -> set[str]:
    out = root / "simai" / "out"
    return {p.name for p in out.iterdir()} if out.is_dir() else set()


def run(gs, tmp_path: Path, monkeypatch, fake_items: list[dict], *extra: str, session: str = "t") -> int:
    monkeypatch.setattr(gs, "items", lambda lay: fake_items)
    return gs.main(["run", "--mode", "dryrun", "--root", str(tmp_path), "--session", session, "--min-free-mb", "1",
                    *extra])


def state(tmp_path: Path, session: str = "t") -> dict:
    return json.loads((tmp_path / "simai" / "queue" / f"session1-{session}" / "state.json").read_text(encoding="utf-8"))["jobs"]


def bundles(tmp_path: Path, session: str = "t") -> list[Path]:
    return sorted((tmp_path / "simai" / "runs" / "gpu-session1" / session).glob("*.tar.gz"))


def members(bundle: Path) -> dict[str, bytes]:
    with tarfile.open(bundle) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


# ---------------------------------------------------------------- 제한 시간·실패·자리만


def test_timeout_and_failure_do_not_block_next_items_and_placeholders_are_not_queued(gs, tmp_path, monkeypatch, capsys):
    items = [
        cpu_item("a", "a_slow", "import time; time.sleep(60)", timeout_s=1),
        cpu_item("b", "b_fail", touch("b_fail") + "; raise SystemExit(3)"),
        {"key": "c", "name": "c_placeholder", "jira": "S15P31A101-182", "title": "자리", "command": None,
         "placeholder": "-182 머지 후 채움", "expected_s": 1, "timeout_s": 1},
        cpu_item("e", "e_ok", touch("e_ok")),
    ]
    t0 = time.monotonic()
    code = run(gs, tmp_path, monkeypatch, items)
    assert code == gs.EXIT_FAILED
    assert time.monotonic() - t0 < 45  # 60 s 잠자는 항목을 제한 시간에 끊었다
    st = state(tmp_path)
    assert st["a_slow"]["status"] == "failed" and "타임아웃" in st["a_slow"]["reason"]
    assert st["b_fail"]["status"] == "failed" and st["b_fail"]["exit_code"] == 3
    assert st["e_ok"]["status"] == "succeeded"
    assert "c_placeholder" not in st  # 자리만 있는 항목은 큐에 없다(성공 기록으로 꾸미지 않는다)
    assert ran(tmp_path) == {"b_fail", "e_ok"}
    [bundle] = bundles(tmp_path)
    files = members(bundle)
    summary = json.loads(files["session.json"])
    assert summary["placeholders"][0]["why"] == "자리만: -182 머지 후 채움"  # 제외·자리만을 구분해 적는다(-205)
    assert summary["jobs"]["a_slow"]["status"] == "failed"
    records = [json.loads(v) for k, v in files.items() if k.endswith("run_record.json")]
    assert {r["status"] for r in records} == {"FAILED", "SUCCEEDED"} and len(records) == 3
    # 검증 큐 기록은 운영 경로 검증 Run(VAL-01, run_record 2.1.0)이고 계약을 통과한다(-197, 이전에는 INT-01 로 적었다)
    assert {(r["experiment_id"], r["schema_version"]) for r in records} == {("VAL-01", "2.2.0")}
    assert all("e_gate" not in r and schema_errors(r, "run_record") == [] for r in records)
    assert "scp -P" in capsys.readouterr().out


def test_total_time_cap_aborts_running_item_and_skips_the_rest(gs, tmp_path, monkeypatch):
    items = [cpu_item("a", "a_long", "import time; time.sleep(60)", timeout_s=120), cpu_item("b", "b_next", touch("b_next"))]
    t0 = time.monotonic()
    code = run(gs, tmp_path, monkeypatch, items, "--max-total-s", "2")
    assert code == gs.EXIT_DEADLINE
    assert time.monotonic() - t0 < 40
    st = state(tmp_path)
    assert st["a_long"]["status"] == "aborted" and st["b_next"]["status"] == "pending"
    assert ran(tmp_path) == set()
    summary = json.loads(members(bundles(tmp_path)[0])["session.json"])
    assert summary["deadline_hit"] is True and summary["exit"] == gs.EXIT_DEADLINE


def test_ctrl_c_mid_item_aborts_bundles_and_rerun_resumes(gs, tmp_path, monkeypatch):
    items = [cpu_item("a", "a_first", touch("a_first")),
             cpu_item("b", "b_long", "import time; time.sleep(60)", timeout_s=120),
             cpu_item("c", "c_after", touch("c_after"))]
    timer = threading.Timer(3.0, _thread.interrupt_main, args=(2,))  # SIGINT 를 주 스레드에(터미널 Ctrl-C 와 같은 처리기)
    timer.start()
    try:
        code = run(gs, tmp_path, monkeypatch, items)
    finally:
        timer.cancel()
    assert code == gs.EXIT_SIGNAL
    st = state(tmp_path)
    assert st["a_first"]["status"] == "succeeded" and st["b_long"]["status"] == "aborted"
    assert st["c_after"]["status"] == "pending"
    assert len(bundles(tmp_path)) == 1
    # 같은 세션으로 다시: 끝난 항목은 건너뛰고 중단된 항목부터 이어 돈다
    items[1] = cpu_item("b", "b_long", touch("b_long"))
    assert run(gs, tmp_path, monkeypatch, items) == gs.EXIT_OK
    st = state(tmp_path)
    assert st["a_first"]["attempts"] == 1 and st["b_long"]["attempts"] == 2
    assert ran(tmp_path) == {"a_first", "b_long", "c_after"}


# ---------------------------------------------------------------- 디스크·쓰기 실패


def test_preflight_refuses_when_disk_is_nearly_full(gs, tmp_path, monkeypatch):
    items = [cpu_item("a", "a_never", touch("a_never"))]
    monkeypatch.setattr(gs, "items", lambda lay: items)
    code = gs.main(["run", "--mode", "dryrun", "--root", str(tmp_path), "--session", "t", "--min-free-mb", "1e12"])
    assert code == gs.EXIT_PREFLIGHT
    assert ran(tmp_path) == set()
    summary = json.loads(members(bundles(tmp_path)[0])["session.json"])
    assert any("남은 용량" in e for e in summary["preflight_errors"])


def test_unwritable_result_folder_falls_back_and_never_runs_items(gs, tmp_path, monkeypatch):
    # 결과 폴더 자리에 파일이 있어 폴더를 만들 수 없다 → 사전 점검 실패, 묶음은 컨테이너 임시 폴더로
    (tmp_path / "simai" / "runs").mkdir(parents=True)
    (tmp_path / "simai" / "runs" / "gpu-session1").write_text("not a dir")
    fallback = tmp_path / "fallback-tmp"
    fallback.mkdir()
    monkeypatch.setattr(gs.tempfile, "gettempdir", lambda: str(fallback))
    code = run(gs, tmp_path, monkeypatch, [cpu_item("a", "a_never", touch("a_never"))])
    assert code == gs.EXIT_PREFLIGHT
    assert ran(tmp_path) == set()
    found = list((fallback / "simai-session1").glob("*.tar.gz"))
    assert len(found) == 1
    assert json.loads(members(found[0])["session.json"])["preflight_errors"]


def test_disk_full_during_run_crashes_queue_but_bundle_and_lock_are_cleaned(gs, tmp_path, monkeypatch):
    """실행 중 ENOSPC(상태 파일 쓰기)로 gpu_queue 가 예외로 끝나도 세션은 묶음을 남기고 잠금을 풀고 끝난다."""
    gq = gs.load_gpu_queue()
    real = gq.write_json_atomic
    started = tmp_path / "simai" / "out" / "b_started"

    def full_disk(path, obj):
        if started.exists() and Path(path).name == "state.json":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, obj)

    monkeypatch.setattr(gq, "write_json_atomic", full_disk)
    items = [cpu_item("a", "a_ok", touch("a_ok")),
             cpu_item("b", "b_fills", touch("b_started") + "; import time; time.sleep(2)"),
             cpu_item("c", "c_never", touch("c_never"))]
    code = run(gs, tmp_path, monkeypatch, items)
    assert code == gs.EXIT_CRASH
    assert "c_never" not in ran(tmp_path)
    assert not (tmp_path / "simai" / "queue" / "queue.lock").exists()  # 다음 세션이 잠금에 막히지 않는다
    summary = json.loads(members(bundles(tmp_path)[0])["session.json"])
    assert "No space left" in summary["crash"]


def test_bundle_folder_write_failure_falls_back_to_temp(gs, tmp_path, monkeypatch):
    lay = gs.Layout("dryrun", tmp_path, "t", None)
    lay.session_dir.parent.mkdir(parents=True)
    lay.session_dir.write_text("볼륨의 묶음 자리가 파일이다")  # 묶음 폴더를 만들 수 없다
    monkeypatch.setattr(gs.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    bundle, warnings = gs.make_bundle(lay, {"session": "t"}, 50)
    assert bundle is not None and bundle.parent == tmp_path / "tmp" / "simai-session1"
    assert any("임시 폴더" in w for w in warnings)


# ---------------------------------------------------------------- 비밀


def test_secrets_never_enter_the_bundle(gs, tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("SIMAI_WORKER_TOKEN", "worker-secret-value-123456")
    private = tmp_path / ".simai-private"
    private.mkdir()
    (private / "hf.env").write_text(f"HF_TOKEN={SECRET_TOKEN}\n")
    code = "\n".join([
        "import os, pathlib",
        "d = pathlib.Path(os.environ['SIMAI_RUN_DIR'])",
        "print('HF_TOKEN env:', os.environ.get('HF_TOKEN'), os.environ.get('SIMAI_WORKER_TOKEN'))",
        f"(d / 'hf.env').write_text('HF_TOKEN={SECRET_TOKEN}')",
        f"(d / 'tls.key').write_text({PRIVATE_KEY!r})",
        f"(d / 'cert-copy.txt').write_text({PRIVATE_KEY!r})",
        f"(d / 'notes.txt').write_text('debug HF_TOKEN={SECRET_TOKEN}')",
        "(d / 'device_token.json').write_text('{}')",
        "(d / 'ok.json').write_text('{\"fine\": 1}')",
        "try:",
        f"    os.symlink(r'{private / 'hf.env'}', d / 'link-to-private.txt')",
        "except OSError:",
        "    pass",
    ])
    assert run(gs, tmp_path, monkeypatch, [cpu_item("a", "a_leaky", code)]) == gs.EXIT_OK
    [bundle] = bundles(tmp_path)
    files = members(bundle)
    names = {Path(n).name for n in files}
    assert "ok.json" in names and "job.log" in names
    assert not names & {"hf.env", "tls.key", "cert-copy.txt", "notes.txt", "device_token.json", "link-to-private.txt"}
    for name, data in files.items():
        assert SECRET_TOKEN.encode() not in data, name
        assert b"PRIVATE KEY" not in data or name == "bundle-manifest.json", name
        assert b"worker-secret-value" not in data, name
    manifest = json.loads(files["bundle-manifest.json"])
    excluded = {Path(e["path"]).name: e["reason"] for e in manifest["excluded"]}
    assert {"hf.env", "tls.key", "cert-copy.txt", "notes.txt"} <= set(excluded)
    summary = json.loads(files["session.json"])
    assert "HF_TOKEN" in summary["scrubbed_env"] and "SIMAI_WORKER_TOKEN" in summary["scrubbed_env"]
    log = next(v for k, v in files.items() if k.endswith("job.log")).decode("utf-8", "replace")
    assert "HF_TOKEN env: None None" in log  # Job 에 비밀 환경 변수가 넘어가지 않았다


# ---------------------------------------------------------------- EXP 큐(g)의 LIVE 보호


def _exp_jobs(path: Path, root: Path) -> Path:
    doc = {"queue": {"name": "ignored"},
           "record": {"experiment_id": "EXP-02", "jira_key": "S15P31A101-87", "owner": "t", "experiment_state": "PLAN",
                      "model_version": "cpu", "input_provenance": {"frames": {"source": "SIM"}, "is_upper_bound": True}},
           "jobs": [{"name": "g_exp", "command": [PY, "-c", touch("g_exp")], "expected_s": 1, "timeout_s": 30, "vram_mb": 0}]}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_session_refuses_to_start_during_live(gs, tmp_path, monkeypatch):
    marker = tmp_path / "simai" / "state" / "tier0.live"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    code = run(gs, tmp_path, monkeypatch, [cpu_item("a", "a_never", touch("a_never"))])
    assert code == gs.EXIT_LIVE and ran(tmp_path) == set()
    assert "tier0.live" in json.loads(members(bundles(tmp_path)[0])["session.json"])["live"]


def test_exp_queue_does_not_run_while_live_marker_is_fresh(gs, tmp_path, monkeypatch):
    marker = tmp_path / "simai" / "state" / "tier0.live"
    marker.parent.mkdir(parents=True)
    jobs = _exp_jobs(tmp_path / "exp.json", tmp_path)
    # 검증 큐 항목이 도는 중에 LIVE 가 시작된다(첫 항목이 표시 파일을 만든다) → 마지막 EXP 큐는 돌지 않는다
    start_live = f"import pathlib; pathlib.Path(r'{marker}').write_text('{{}}')"
    code = run(gs, tmp_path, monkeypatch, [cpu_item("a", "a_ok", touch("a_ok") + "; " + start_live)], "--exp-jobs", str(jobs))
    assert code == gs.EXIT_LIVE
    assert ran(tmp_path) == {"a_ok"}
    # 오래된 표시 파일(강제 종료된 Tier 0 가 남긴 것, 30 s 넘게 갱신 없음)은 무시하고 돈다
    old = time.time() - 120
    os.utime(marker, (old, old))
    assert run(gs, tmp_path, monkeypatch, [cpu_item("a", "a_ok", touch("a_ok"))], "--exp-jobs", str(jobs)) == gs.EXIT_OK
    assert "g_exp" in ran(tmp_path)


# ---------------------------------------------------------------- Pod 모드 Job 파일


def test_pod_job_file_passes_gpu_queue_check_and_run_record_schema(gs, monkeypatch):
    monkeypatch.setenv("SIMAI_IMAGE_DIGEST", "sha256:" + "a" * 64)
    lay = gs.Layout("pod", None, "20261020", "/opt/simai/ai/.venv/bin/python")
    doc, held = gs.build_jobs(lay, {"commit": "0" * 40, "branch": "dev", "dirty_worktree": False})
    names = [j["name"] for j in doc["jobs"]]
    # 시험 도구(t)가 그것을 쓰는 c·d 보다 먼저. e(엘리스로 이동)·f(GPU 불필요)는 제외, 실험 이미지 항목(g·m)은 다른 이미지 Pod (S15P31A101-205·-207)
    assert names == ["a_pod_check", "b_tier0_engine", "t_test_tools", "c_tier0_live", "d_tier0_gpu_priority"]
    assert {h["key"] for h in held} == {"e", "f", "g0", "g1", "g2", "g3", "g4", "m0", "m1", "m2",
                                        "u1", "u2", "u3", "u4", "v4", "e25"}  # 통합 이미지 항목(S15P31A101-246·-250·-291·-25)
    assert all(j["timeout_s"] >= j["expected_s"] for j in doc["jobs"])
    assert doc["queue"]["continue_on_failure"] is True
    tools = next(j for j in doc["jobs"] if j["name"] == "t_test_tools")["command"]
    assert any(c.startswith("pytest==") for c in tools) and any(c.startswith("cryptography==") for c in tools)
    gq = gs.load_gpu_queue()
    path = Path(gs.tempfile.mkdtemp()) / "jobs.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    gq.load_job_file(path)
    queue = gq.Queue(doc, path, path.parent / "state", None, gs.REPO / "contracts", persist=False)
    assert queue.dry_run_errors() == []
    assert doc["record"]["experiment_id"] == "VAL-01"
