"""4차 GPU 세션 큐 (S15P31A101-291) 반례 시험. GPU·네트워크·Pod 를 쓰지 않는다(가짜 nvidia-smi, 가짜 vLLM HTTP).

반례: MoGe(h8)가 어떤 경로로도 큐에 끼지 않음, h7 해상도는 h2~h5 값만, 가중치 sha256 불일치면 그 Job 이 predict 전에 실패
(음성 대조: 해시가 맞으면 predict 까지 간다), 생성한 EXP Job 에도 LIVE 보호가 걸림(다른 저장 위치로 만든 파일이어도 그 세션의 표시 파일을 본다,
음성 대조: 오래된 표시 파일이면 돈다), --only 오타·다른 이미지 키는 빈 큐로 '성공'하지 않음, exp_job --metrics(끝난 depth.json 을
덮어쓰지 않음, OOM 기록도 지표로), v4 가 환경이 덮어쓴 vLLM 기본값으로는 시작하지 않음·모르는 단계(표본 없음)를 통과로 보지 않음.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from .conftest import load_script

REPO = Path(__file__).resolve().parents[3]
PY = Path(sys.executable).as_posix()
FAKE_SMI = (REPO / "ai" / "tests" / "experiments" / "gpu_queue" / "fake_nvidia_smi.py").as_posix()


def _s4():
    spec = importlib.util.spec_from_file_location("static_scene_session4_jobs", REPO / "ai" / "experiments" / "static_scene" / "session4_jobs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(s4, root: str = "/srv/simai", **kw):
    return s4.build(root, REPO.as_posix(), PY, "/root/da3", f"{root}/datasets/classroom4-prepared-20261026",
                    f"{root}/runs/exp01/{s4.DEFAULT_MA_RUN}", "classroom-stills-20261026", "20261027-s4", **kw)


def _text(job) -> str:
    return job["command"] if isinstance(job["command"], str) else " ".join(map(str, job["command"]))


# ---------------------------------------------------------------- 생성기


def test_session4_queue_order_records_and_gpu_queue_check(tmp_path):
    s4 = _s4()
    doc = _doc(s4)
    names = [j["name"] for j in doc["jobs"]]
    assert names == [*(f"w1_mapanything_known_k_default_{r}" for r in ("r518", "r1036", "r1554", "rfull")),
                     "h0_da3_venv", "h1_da3_weights", "h2_da3_pose_504", "h3_da3_pose_1008", "h4_da3_pose_1512",
                     "h5_da3_pose_2304", "h6_da3_nopose_504"]  # h7 은 사람이 해상도를 고른 뒤에만
    by = {j["name"]: j for j in doc["jobs"]}
    for r in ("r518", "rfull"):  # -282: 장면 준비 모드로 감싼다(static 가중치·Tier 0 우선), 기록은 -282
        cmd = by[f"w1_mapanything_known_k_default_{r}"]["command"]
        assert cmd[cmd.index("--") + 1:cmd.index("--") + 4] == [PY, "deploy/scripts/simai_bootstrap.py", "scene"]
        assert by[f"w1_mapanything_known_k_default_{r}"]["record"]["jira_key"] == "S15P31A101-282"
    for n in names[4:]:
        assert doc["jobs"][names.index(n)].get("record", {}).get("jira_key", doc["record"]["jira_key"]) == "S15P31A101-283"
    h5 = _text(by["h5_da3_pose_2304"])
    assert "--process-res 2304" in h5 and "--metrics depth.json" in h5 and "/root/da3/bin/python" in h5
    assert "--no-pose" in _text(by["h6_da3_nopose_504"]) and "--ray-pose" not in " ".join(map(_text, doc["jobs"]))
    outs = [_text(j).split("--out ")[-1].split()[0] for j in doc["jobs"] if j["name"].startswith(("h2", "h3", "h4", "h5", "h6"))]
    assert len(set(outs)) == 5 and all(o.endswith("_20261027-s4") for o in outs)  # 세션마다·항목마다 다른 결과 폴더
    assert doc["queue"]["live_guard"]["enabled"] is True and "marker_files" not in doc["queue"]["live_guard"]
    path = tmp_path / "exp.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "ai" / "experiments" / "gpu_queue" / "gpu_queue.py"), "check", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=REPO / "ai")
    assert r.returncode == 0, r.stdout + r.stderr


def test_moge_never_enters_and_h7_takes_only_h2_to_h5_resolutions():
    s4 = _s4()
    for kw in ({}, {"h7_res": 1008}, {"only": ("h2", "h6")}):
        assert all("moge" not in _text(j).lower() and not j["name"].startswith("h8") for j in _doc(s4, **kw)["jobs"])
    with pytest.raises(SystemExit, match="S15P31A101-287"):
        _doc(s4, only=("h8",))
    with pytest.raises(SystemExit, match="h2~h5"):
        _doc(s4, h7_res=999)  # 고른 값이 아닌 해상도
    with pytest.raises(SystemExit, match="--h7-res"):
        _doc(s4, only=("h7",))  # 해상도 없이 h7
    with pytest.raises(SystemExit, match="모르는 항목"):
        _doc(s4, only=("h9",))
    h7 = _doc(s4, h7_res=1512, only=("h7",))["jobs"]
    assert [j["name"] for j in h7] == ["h7_da3_raypose_1512"]
    assert "--ray-pose" in h7[0]["command"] and "--process-res 1512" in h7[0]["command"]


def _run_job(job, root: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Job 명령을 gpu_queue 와 같이(작업 폴더 = 저장소 사본, 문자열이면 bash -c) 돌린다. 가짜 nvidia-smi."""
    gs = load_script("gpu_session1")
    env = {**os.environ, **job.get("env", {}), "SIMAI_ROOT": str(root), "SIMAI_NVIDIA_SMI": f"{PY} {FAKE_SMI}",
           "SIMAI_MIN_FREE_GB": "0", "PYTHONUTF8": "1", **(extra_env or {})}
    for k in ("HF_TOKEN", "FAKE_SMI_FAIL", "FAKE_SMI_CC", "FAKE_SMI_TOTAL_MB"):
        env.pop(k, None)
    cmd = [gs.find_bash(), "-c", job["command"]] if isinstance(job["command"], str) else job["command"]
    return subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def test_weight_hash_mismatch_fails_the_job_before_predict_and_matching_hash_reaches_predict(tmp_path):
    s4 = _s4()
    root = tmp_path / "simai"
    doc = _doc(s4, root.as_posix())
    by = {j["name"]: j for j in doc["jobs"]}
    wdir = root / s4.DA3_WEIGHTS
    wdir.mkdir(parents=True)
    for f in s4.DA3_FILES:
        (wdir / f).write_bytes(b"tampered " + f.encode())  # 이미 있는데 목록 sha256 과 다르다(받다 만 파일·다른 리비전)
    out = Path(_text(by["h2_da3_pose_504"]).split("--out ")[-1].split()[0])
    for name in ("h1_da3_weights", "h2_da3_pose_504"):
        r = _run_job(by[name], root)
        assert r.returncode == 2, r.stdout + r.stderr  # 가중치 오류(simai-bootstrap 코드 2)로 Job 실패
        assert "해시 불일치" in r.stderr and "run_hybrid_depth" not in r.stderr  # predict 는 돌지 않았다
    assert not out.exists() and all((wdir / f).read_bytes().startswith(b"tampered") for f in s4.DA3_FILES)  # 지우지 않는다
    # 음성 대조: 같은 파일을 sha256 이 맞는 목록으로 보면 확인을 지나 exp_job → predict 까지 간다(여기서는 입력 폴더가 없어 predict 가 실패)
    manifest = json.loads((REPO / s4.MANIFEST).read_text(encoding="utf-8"))
    for w in manifest["weights"]:
        w["sha256"] = hashlib.sha256((root / w["path"]).read_bytes()).hexdigest()
    ok = tmp_path / "da3-ok.json"
    ok.write_text(json.dumps(manifest), encoding="utf-8")
    job = {**by["h2_da3_pose_504"], "command": by["h2_da3_pose_504"]["command"].replace("/root/da3/bin/python", PY)}
    r = _run_job(job, root, {"SIMAI_WEIGHTS_MANIFEST": ok.as_posix()})
    assert r.returncode != 0 and "해시 불일치" not in r.stderr and "run_hybrid_depth" in r.stderr, r.stdout + r.stderr


def test_live_guard_stops_generated_exp_jobs_even_if_file_was_made_for_another_root(gs, tmp_path, monkeypatch):
    s4 = _s4()
    doc = _doc(s4)  # Pod 저장 위치(/srv/simai)로 만든 파일을 다른 저장 위치(드라이런 tmp)의 세션이 돈다
    for j in doc["jobs"]:
        name = j["name"]
        j["command"] = [PY, "-c", "import os, pathlib; d = pathlib.Path(os.environ['SIMAI_RUN_DIR']).parents[3] / 'out';"
                                  f" d.mkdir(exist_ok=True); (d / '{name}').write_text('x')"]
    path = tmp_path / "exp.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    marker = tmp_path / "simai" / "state" / "tier0.live"
    marker.parent.mkdir(parents=True)
    start_live = f"import pathlib; pathlib.Path(r'{marker}').write_text('{{}}')"  # 검증 큐 도중 LIVE 시작
    item = {"key": "a", "name": "a_ok", "jira": "S15P31A101-31", "title": "a", "command": [PY, "-c", start_live],
            "expected_s": 1, "timeout_s": 30, "model_version": "cpu",
            "provenance": {"frames": {"source": "SIM", "dataset_id": "none"}, "is_upper_bound": True}}
    monkeypatch.setattr(gs, "items", lambda lay: [item])
    argv = ["run", "--mode", "dryrun", "--root", str(tmp_path), "--min-free-mb", "1", "--only", "a", "--exp-jobs", str(path)]
    assert gs.main([*argv, "--session", "live"]) == gs.EXIT_LIVE
    out = tmp_path / "simai" / "out"
    assert not out.exists()  # 생성한 EXP Job 은 하나도 돌지 않았다
    # 음성 대조: 30 s 넘게 갱신되지 않은 표시 파일(죽은 Tier 0)이면 모두 돈다
    item["command"] = [PY, "-c", "pass"]
    old = time.time() - 120
    os.utime(marker, (old, old))
    assert gs.main([*argv, "--session", "stale"]) == gs.EXIT_OK
    assert {p.name for p in out.iterdir()} == {j["name"] for j in doc["jobs"]}


# ---------------------------------------------------------------- 세션 실행기 --only


def test_only_refuses_typo_or_other_image_keys_instead_of_an_empty_success(gs, capsys):
    assert gs.main(["plan", "--mode", "pod", "--root", "/srv", "--image", "unified", "--only", "a,v4"]) == gs.EXIT_OK
    table = capsys.readouterr().out
    assert "v4_vlm_int" in table and "--only 밖" in table
    for image, only in (("unified", "v1"), ("tier0", "v4"), ("unified", "e"), ("unified", " , ")):
        assert gs.main(["plan", "--mode", "pod", "--root", "/srv", "--image", image, "--only", only]) == gs.EXIT_BAD_JOBS
    lay = gs.Layout("pod", Path("/srv"), "s4", PY, "unified")
    lay.only = {"a", "t", "v4"}
    doc, held = gs.build_jobs(lay, {"commit": "0" * 40, "branch": "dev", "dirty_worktree": False})
    assert [j["name"] for j in doc["jobs"]] == ["a_pod_check", "t_test_tools", "v4_vlm_int"]
    assert doc["queue"]["live_guard"] == {"enabled": False}  # v4 는 자기 Tier 0 하네스를 띄우는 검증 큐 항목


# ---------------------------------------------------------------- exp_job --metrics


def test_exp_job_metrics_name_guards_finished_depth_and_keeps_oom_record(ej, tmp_path, monkeypatch):
    out = tmp_path / "HYB283_da3_pose_2304_s4"
    run_dir = tmp_path / "run"
    monkeypatch.setenv("SIMAI_RUN_DIR", str(run_dir))
    oom = {"result": "OOM", "error": "OutOfMemoryError: CUDA", "adapter": {"adapter": "da3", "process_res": 2304}}
    child = (f"import json, pathlib; p = pathlib.Path(r'{out}'); p.mkdir(parents=True, exist_ok=True); "
             f"(p / 'depth.json').write_text(json.dumps({oom!r})); raise SystemExit(3)")
    assert ej.main(["--out", str(out), "--metrics", "depth.json", "--", PY, "-c", child]) == 3
    m = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert m["result"] == "OOM" and m["adapter.process_res"] == 2304  # 실패도 지표로 남는다
    marker = tmp_path / "ran"
    assert ej.main(["--out", str(out), "--metrics", "depth.json", "--", PY, "-c", f"open(r'{marker}', 'w')"]) == ej.EXIT_EXISTS
    assert not marker.exists()  # 끝난(실패 포함) 깊이 폴더를 덮어쓰지 않는다
    assert ej.main(["--out", str(out), "--metrics", "../x.json", "--", PY, "-c", "pass"]) == 2


# ---------------------------------------------------------------- v4 (vlm_int)


@pytest.fixture
def vi(monkeypatch):
    module = load_script("vlm_int")
    for k in ("SIMAI_VLLM_EXTRA_ARGS", "SIMAI_WORKER_VLLM_MAX_MODEL_LEN", "VLLM_USE_FLASHINFER_SAMPLER", "SIMAI_SIDECAR_CPUS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(module.bs, "usable_cpus", lambda: list(range(224)))  # 3차 세션 Pod: affinity 224, cgroup 23.8
    monkeypatch.setattr(module.bs, "cgroup_vcpu", lambda read=None: (23.8, "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us"))
    return module


def test_v4_plan_uses_supervisor_defaults_and_quota_cpus(vi, tmp_path):
    plan = vi.vllm_plan(tmp_path, dict(os.environ))
    argv = plan["argv"]
    assert argv[argv.index("--max-model-len") + 1] == "32768" and plan["env"]["VLLM_USE_FLASHINFER_SAMPLER"] == "0"
    assert plan["cpus"] == list(range(212, 224)) and vi.expected_cpu_count(plan["affinity"], plan["quota"]) == 12
    assert vi.defaults_problems(plan, dict(os.environ)) == []
    # 경계: 할당량 7.99 → 8 → 4개, 7.0 → 묶지 않음, 할당량 없음·6개 → 묶지 않음
    assert vi.expected_cpu_count(224, 7.99) == 4 and vi.expected_cpu_count(224, 7.0) is None and vi.expected_cpu_count(6, None) is None


@pytest.mark.parametrize("env, want", [
    ({"SIMAI_VLLM_EXTRA_ARGS": "--max-model-len=65536"}, "--max-model-len"),
    ({"SIMAI_VLLM_EXTRA_ARGS": "--max_model_len 32768 --max-model-len 8192"}, "--max-model-len"),
    ({"SIMAI_WORKER_VLLM_MAX_MODEL_LEN": "16384"}, "--max-model-len"),
    ({"VLLM_USE_FLASHINFER_SAMPLER": "1"}, "VLLM_USE_FLASHINFER_SAMPLER"),
    ({"SIMAI_SIDECAR_CPUS": "none"}, "SIMAI_SIDECAR_CPUS"),
])
def test_v4_refuses_to_measure_overridden_defaults_before_using_gpu(vi, tmp_path, monkeypatch, env, want):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    plan = vi.vllm_plan(tmp_path, dict(os.environ))
    assert any(want in p for p in vi.defaults_problems(plan, dict(os.environ)))
    weights = tmp_path / "simai" / "weights" / "cosmos3-nano"
    weights.mkdir(parents=True)
    (weights / "config.json").write_text("{}")
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "simai"))
    monkeypatch.setenv("SIMAI_RUN_DIR", str(tmp_path / "run"))
    assert vi.main(["--images", str(tmp_path / "no-images")]) == vi.EXIT_CONFIG
    s = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))
    assert any(want in e for e in s["config_errors"]) and not (tmp_path / "run" / "live").exists()  # 하네스를 띄우지 않았다


def test_v4_without_vlm_weights_exits_2_with_the_fetch_hint(vi, tmp_path, monkeypatch):
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "simai"))
    monkeypatch.setenv("SIMAI_RUN_DIR", str(tmp_path / "run"))
    assert vi.main(["--images", str(tmp_path)]) == vi.EXIT_CONFIG
    errs = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))["config_errors"]
    assert any("SIMAI_WEIGHTS_SET=vlm" in e for e in errs) and any("질문 그림 없음" in e for e in errs)


def _good_summary() -> dict:
    phase = {"samples": 60, "cameras_ready_min": 4, "reserve_states": {"OK": 60}}
    req = {"n": 20, "errors": 0, "total_p95": 2.1, "g7_total_p95_le_5s": True}
    return {"defaults_problems": [], "vllm_ready_s": 70.1, "cameras": 4, "requests": {k: dict(req) for k in ("concurrency1", "concurrency4", "sustained4")},
            "phases": {p: dict(phase) for p in ("A", "B", "C")}, "plan": {"cpus": list(range(212, 224)), "cpus_expected": 12},
            "vllm_affinity": list(range(212, 224)), "kill9": {"group_after_killpg": []}}


@pytest.mark.parametrize("break_it, want", [
    (lambda s: s["requests"]["sustained4"].update(total_p95=5.2, g7_total_p95_le_5s=False), "sustained4"),
    (lambda s: s["requests"]["concurrency4"].update(errors=1), "concurrency4"),
    (lambda s: s["requests"].pop("concurrency1"), "concurrency1"),
    (lambda s: s["phases"]["C"].update(samples=0), "표본 없음"),  # 모르는 단계는 통과가 아니다
    (lambda s: s["phases"]["B"].update(cameras_ready_min=None), "카메라 ready"),  # 표시 파일을 못 읽은 표본이 있었다
    (lambda s: s["phases"]["B"].update(cameras_ready_min=3), "카메라 ready"),
    (lambda s: s["phases"]["A"].update(reserve_states={"OK": 59, "None": 1}), "reserve"),
    (lambda s: s.update(vllm_ready_s=None), "/health"),
    (lambda s: s.update(vllm_affinity=list(range(112, 224))), "CPU 묶기"),  # 3차 세션처럼 112개에 묶임
    (lambda s: s["plan"].update(cpus_expected=11), "할당량 기준"),
    (lambda s: s.pop("kill9"), "D(kill -9"),
    (lambda s: s["kill9"].update(group_after_killpg=[4242]), "남은 프로세스"),
    (lambda s: s.update(defaults_problems=["--max-model-len ['65536']"]), "기본값 아님"),
])
def test_v4_judge_fails_each_broken_condition(vi, break_it, want):
    assert vi.judge(_good_summary()) == []
    s = _good_summary()
    break_it(s)
    assert any(want in w for w in vi.judge(s)), vi.judge(s)


def test_v4_phase_summary_marks_unknown_marker_samples_and_slices_lag_by_phase(vi):
    rows = [{"phase": "A", "lag_count": 0, "cameras_ready": 4, "reserve_state": "OK", "frame_age_p95_max": 60},
            {"phase": "A", "lag_count": 3, "cameras_ready": 4, "reserve_state": "OK", "frame_age_p95_max": 62},
            {"phase": "B", "lag_count": 3, "cameras_ready": None, "reserve_state": None, "frame_age_p95_max": None},
            {"phase": "B", "lag_count": 5, "cameras_ready": 4, "reserve_state": "OK", "frame_age_p95_max": 75}]
    lags = [100, 110, 120, 300, 310]
    a, b = vi.phase_summary(rows, lags, "A"), vi.phase_summary(rows, lags, "B")
    assert a["cameras_ready_min"] == 4 and a["pipeline_lag_ms_p95"] == 120 and a["frame_age_p95_max_max"] == 62
    assert b["cameras_ready_min"] is None and b["reserve_states"] == {"None": 1, "OK": 1} and b["lag_samples"] == 2
    assert vi.phase_summary(rows, lags, "C")["samples"] == 0


class _FakeVllm(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(self.status)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.status == 200:
            for part in ("교실에 ", "세 명이 있다."):
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': part}}]})}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


def test_v4_requests_measure_stream_and_count_server_errors(vi):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeVllm)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
    try:
        stats, rows = vi.burst(url, "cosmos3-nano", ["data:image/jpeg;base64,AA=="], 6, 2, 16)
        assert stats["n"] == 6 and stats["errors"] == 0 and stats["g7_total_p95_le_5s"] is True
        assert all(r["ttft"] is not None and r["ttft"] <= r["total"] and r["chars"] > 0 for r in rows)
        _FakeVllm.status = 500
        stats, _ = vi.burst(url, "cosmos3-nano", ["x"], 3, 1, 16)
        assert stats["errors"] == 3 and stats["g7_total_p95_le_5s"] is False  # 모두 오류면 G7 통과가 아니다
    finally:
        _FakeVllm.status = 200
        srv.shutdown()


def _fake_proc(root: Path, rows: dict[int, tuple[str, int]]) -> Path:
    """/proc/<pid>/stat 흉내: {pid: (상태, pgrp)}. comm 에 공백·괄호가 있어도 마지막 ')' 뒤를 읽는다."""
    for pid, (state, pgrp) in rows.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} (VLLM::Engine (x)) {state} 1 {pgrp} {pgrp} 0 -1 0 0 0 0 0 7 3 0 0" + chr(10))
    (root / "self").mkdir()
    return root


def test_v4_group_ignores_zombie_leader_but_not_a_live_child(vi, tmp_path):
    # 리더(4200)를 SIGKILL 하고 아직 거두지 않았거나 거둔 직후: 좀비만 남은 그룹은 빈 그룹이다 → D 판정 PASS
    only_zombie = _fake_proc(tmp_path / "a", {4200: ("Z", 4200), 4300: ("S", 77)})
    assert vi.group_pids(4200, only_zombie) == []
    s = _good_summary()
    s["kill9"]["group_after_killpg"] = vi.group_pids(4200, only_zombie)
    assert vi.judge(s) == []
    # 살아 있는 EngineCore(같은 그룹, S·D 상태)가 남으면 FAIL
    live_child = _fake_proc(tmp_path / "b", {4200: ("Z", 4200), 4201: ("S", 4200), 4202: ("D", 4200)})
    assert vi.group_pids(4200, live_child) == [4201, 4202]
    s["kill9"]["group_after_killpg"] = vi.group_pids(4200, live_child)
    assert any("남은 프로세스" in w for w in vi.judge(s))
    assert vi.proc_stat(4201, live_child) == ("S", 4200, 10) and vi.proc_stat(9999, live_child) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SIGKILL·프로세스 그룹·/proc 는 Linux(Pod·Jenkins)")
def test_v4_kill9_check_reaps_the_leader_and_reports_an_empty_group(vi, tmp_path):
    # 리더가 같은 그룹의 손자를 남기고 죽는 vLLM 모양: 고아 1개 → 그룹 정리 뒤 빈 그룹(리더 좀비가 남아 늘 FAIL 하던 결함, 리뷰 -291 필수)
    code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    proc = vi.bs.Proc("vllm", [sys.executable, "-c", code], dict(os.environ), tmp_path / "logs")
    time.sleep(1.0)
    k = vi.kill9_check(proc, [sys.executable, "-c", "raise SystemExit(1)"], settle_s=0.5, wait_s=10)
    assert k["group_before"] == 2 and k["orphans_after_10s"] == 1 and k["group_after_killpg"] == []
    assert proc.poll() is not None  # 리더를 거뒀다(좀비로 남기지 않음)


def test_generator_refuses_git_bash_converted_pod_paths(tmp_path, monkeypatch):
    s4 = _s4()
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    monkeypatch.delenv("MSYS_NO_PATHCONV", raising=False)
    monkeypatch.delenv("MSYS2_ARG_CONV_EXCL", raising=False)
    out = tmp_path / "exp.json"
    with pytest.raises(SystemExit, match="MSYS_NO_PATHCONV"):  # /srv/simai/... → C:/Program Files/Git/srv/simai/...
        s4.main(["--repo", "C:/Program Files/Git/srv/simai/setup/session1/repo", "--session", "s4", "-o", str(out)])
    assert not out.exists()
    monkeypatch.setenv("MSYS_NO_PATHCONV", "1")  # 음성 대조: 변환을 끈 Pod 경로는 그대로 받는다
    assert s4.main(["--repo", "/srv/simai/setup/session1/repo", "--session", "s4", "-o", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["queue"]["workdir"] == "/srv/simai/setup/session1/repo"
