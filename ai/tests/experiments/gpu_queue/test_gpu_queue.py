"""GPU 큐 시험 (노트북, GPU 없음): 순차 실행·Run 기록 스키마, 타임아웃, 잠금, 재시작 복구, LIVE 보호."""

import json
import subprocess
import sys
import time
import uuid

import pytest

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="/proc·프로세스 그룹은 Linux(Pod)에서만 쓴다")


def test_two_jobs_run_in_order_and_records_pass_schema(qenv, run_record_errors):
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out)), qenv.job("b", qenv.py("ok", "b", qenv.out))])
    r = qenv.run("run", jobs)
    assert r.returncode == 0, r.stdout + r.stderr
    assert qenv.lines() == ["a", "b"]

    recs = qenv.records()
    assert [r["status"] for r in recs] == ["SUCCEEDED", "SUCCEEDED"]
    assert recs[0]["finished_at"] <= recs[1]["started_at"]  # 하나씩 순서대로
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=qenv.REPO, capture_output=True, text=True).stdout.strip()
    for rec in recs:
        assert run_record_errors(rec) == []
        assert rec["git_commit"] == head
        assert rec["input_provenance"] == {"frames": {"source": "SIM", "dataset_id": "sim/basic"}, "is_upper_bound": True}
        assert rec["gpu"] == {"model": "", "driver": "", "cuda": "", "provider": "LOCAL"}  # nvidia-smi 없음 → 비움
        assert isinstance(rec["started_at"], int) and rec["finished_at"] >= rec["started_at"]
        assert rec["metrics"] == {"sample_value": 1.5, "label": rec["run_id"].split("-")[1]}  # 스칼라만
        assert [x["kind"] for x in rec["results"]] == ["LOG"]
        assert rec["results"][0]["sha256"].startswith("sha256:")
        assert "exit_code=0" in rec["notes"]
        assert rec["config"]["path"].endswith("jobs_t.json")

    # 다시 돌려도 끝난 Job 은 돌지 않는다
    r = qenv.run("run", jobs)
    assert r.returncode == 0, r.stdout + r.stderr
    assert qenv.lines() == ["a", "b"]
    assert "이미 끝난 Job 건너뜀: a, b" in r.stdout
    # --rerun 으로 지정한 Job 만 다시 돈다
    assert qenv.run("run", jobs, "--rerun", "b").returncode == 0
    assert qenv.lines() == ["a", "b", "b"]


def test_gpu_info_and_vram_check_with_fake_nvidia_smi(qenv, run_record_errors):
    smi = [qenv.py()[0], str(qenv.FAKE_SMI)]  # 남은 VRAM 81559 - 1024 MB
    jobs = qenv.write(
        [qenv.job("big", qenv.py("ok", "big", qenv.out), vram_mb=90000), qenv.job("small", qenv.py("ok", "small", qenv.out), vram_mb=4000)],
        gpu_query_cmd=smi,
    )
    r = qenv.run("run", jobs)
    assert r.returncode == 0, r.stdout + r.stderr
    assert qenv.lines() == ["small"]
    big, small = qenv.records()
    assert big["status"] == "ABORTED" and "VRAM 부족" in big["failure_reason"]
    assert small["gpu"] == {"model": "NVIDIA H100 80GB HBM3", "driver": "550.54.15", "cuda": "12.4", "provider": "LOCAL"}
    assert run_record_errors(big) == [] and run_record_errors(small) == []
    assert qenv.state_of()["jobs"]["big"]["status"] == "aborted"  # 끝난 것이 아니므로 다음 실행 때 다시 시도


def test_timeout_kills_child_tree_and_queue_continues(qenv, run_record_errors):
    pidfile = qenv.tmp / "grandchild.pid"
    jobs = qenv.write([qenv.job("hang", qenv.py("hang", pidfile), timeout_s=1.5), qenv.job("next", qenv.py("ok", "next", qenv.out))])
    t0 = time.monotonic()
    r = qenv.run("run", jobs)
    assert time.monotonic() - t0 < 30
    assert r.returncode == 1, r.stdout + r.stderr  # 실패한 Job 이 있으면 1
    assert qenv.lines() == ["next"]
    hang, nxt = qenv.records()
    assert hang["status"] == "FAILED" and "타임아웃" in hang["failure_reason"]
    assert nxt["status"] == "SUCCEEDED"
    assert run_record_errors(hang) == []
    assert qenv.wait_dead(int(pidfile.read_text())), "손자 프로세스가 남음"
    assert qenv.state_of()["jobs"]["hang"]["status"] == "failed"


def test_failure_stops_queue_when_continue_on_failure_false(qenv):
    jobs = qenv.write([qenv.job("bad", qenv.py("fail", 3)), qenv.job("after", qenv.py("ok", "after", qenv.out))], continue_on_failure=False)
    r = qenv.run("run", jobs)
    assert r.returncode == 1
    assert qenv.lines() == []
    st = qenv.state_of()["jobs"]
    assert st["bad"]["status"] == "failed" and st["bad"]["exit_code"] == 3
    assert st["after"]["status"] == "pending"
    (bad,) = qenv.records()
    assert bad["failure_reason"] == "종료 코드 3"


def test_lock_prevents_second_queue_on_same_volume(qenv):
    flag = qenv.tmp / "hold.flag"
    flag.write_text("")
    first = qenv.write([qenv.job("hold", qenv.py("slow-if", flag, "hold", qenv.out))], name="first")
    second = qenv.write([qenv.job("other", qenv.py("ok", "other", qenv.out))], name="second")
    proc = qenv.start("run", first)
    try:
        qenv.wait_running("hold", name="first", sleeping=flag)
        r = qenv.run("run", second)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "다른 큐가 실행 중" in r.stdout
        assert "other" not in qenv.lines()
        lock = json.loads((qenv.state / "queue.lock").read_text(encoding="utf-8"))
        assert lock["queue"] == "first" and lock["current"]["job"] == "hold"
    finally:
        flag.unlink()
        proc.kill()
        proc.wait(timeout=10)


def test_stale_lock_is_taken_over(qenv):
    qenv.state.mkdir(parents=True)
    lock = {"token": "x", "queue": "old", "host": "other-pod", "pid": 1, "heartbeat_at": int(time.time() * 1000) - 60_000, "ttl_s": 5}
    (qenv.state / "queue.lock").write_text(json.dumps(lock), encoding="utf-8")
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))])
    r = qenv.run("run", jobs)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "오래된 잠금 회수" in r.stdout
    assert not (qenv.state / "queue.lock").exists()  # 끝나면 잠금을 푼다

    # TTL 안의 다른 호스트 잠금은 회수하지 않는다
    lock["heartbeat_at"] = int(time.time() * 1000)
    (qenv.state / "queue.lock").write_text(json.dumps(lock), encoding="utf-8")
    assert qenv.run("run", jobs, "--rerun", "a").returncode == 2


def test_restart_recovers_after_queue_crash(qenv, run_record_errors):
    flag = qenv.tmp / "slow.flag"
    flag.write_text("")
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out)), qenv.job("b", qenv.py("slow-if", flag, "b", qenv.out))])
    proc = qenv.start("run", jobs)
    st = qenv.wait_running("b", sleeping=flag)
    orphan = st["pid"]
    proc.kill()  # 큐 프로세스가 갑자기 죽음 (Job 프로세스는 남는다)
    proc.wait(timeout=10)
    assert qenv.pid_alive(orphan)

    flag.unlink()
    r = qenv.run("run", jobs)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "복구: b" in r.stdout
    assert qenv.wait_dead(orphan), "죽은 큐가 남긴 Job 프로세스를 정리하지 않음"
    assert qenv.lines() == ["a", "b"]  # a 는 다시 돌지 않음
    recs = [x for x in qenv.records() if x["run_id"].startswith("t-b-")]
    assert [x["status"] for x in recs] == ["ABORTED", "SUCCEEDED"]
    assert "finished_at" not in recs[0]  # 언제 끝났는지 모르면 비운다
    assert all(run_record_errors(x) == [] for x in recs)
    assert qenv.state_of()["jobs"]["b"]["attempts"] == 2


def test_live_marker_stop_policy_does_not_start_exp_jobs(qenv):
    qenv.live.write_text("")
    guard = {"marker_files": [str(qenv.live)], "process_patterns": [], "on_live": "stop", "poll_s": 0.2}
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))], live_guard=guard)
    r = qenv.run("run", jobs)
    assert r.returncode == 3, r.stdout + r.stderr
    assert qenv.lines() == [] and qenv.records() == []
    assert qenv.state_of()["jobs"]["a"]["status"] == "pending"

    qenv.live.unlink()
    assert qenv.run("run", jobs).returncode == 0
    assert qenv.lines() == ["a"]


def test_live_wait_policy_waits_then_resumes(qenv):
    qenv.live.write_text("")
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))])
    proc = qenv.start("run", jobs)
    try:
        time.sleep(1.5)
        assert proc.poll() is None and qenv.lines() == []  # LIVE 동안은 시작하지 않음
        qenv.live.unlink()
        out, _ = proc.communicate(timeout=30)
    finally:
        proc.kill()
    assert proc.returncode == 0, out
    assert "LIVE 관제 중 → EXP Job 대기" in out
    assert qenv.lines() == ["a"]


def test_live_start_preempts_running_job_and_retries(qenv, run_record_errors):
    flag = qenv.tmp / "slow.flag"
    flag.write_text("")
    jobs = qenv.write([qenv.job("exp", qenv.py("slow-if", flag, "exp", qenv.out))])
    proc = qenv.start("run", jobs)
    try:
        st = qenv.wait_running("exp", sleeping=flag)
        qenv.live.write_text("")  # Tier 0 LIVE 시작
        assert qenv.wait_dead(st["pid"]), "LIVE 시작 뒤에도 EXP Job 이 돎"
        flag.unlink()
        time.sleep(0.5)
        assert qenv.lines() == []  # LIVE 동안 재시작하지 않음
        qenv.live.unlink()
        out, _ = proc.communicate(timeout=30)
    finally:
        proc.kill()
    assert proc.returncode == 0, out
    assert qenv.lines() == ["exp"]
    recs = qenv.records()
    assert [x["status"] for x in recs] == ["ABORTED", "SUCCEEDED"]
    assert "LIVE 관제 시작으로 중단" in recs[0]["failure_reason"]
    assert all(run_record_errors(x) == [] for x in recs)


def test_check_rejects_job_without_input_provenance(qenv):
    record = {"experiment_id": "INT-01", "jira_key": "S15P31A101-36", "owner": "t", "experiment_state": "PLAN", "model_version": "m"}
    bad = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))], record=record)
    r = qenv.run("check", bad)
    assert r.returncode == 4
    assert "input_provenance" in r.stderr

    good = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))], name="good")
    r = qenv.run("check", good)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Run 기록 형식 통과" in r.stdout
    assert not qenv.state.exists()


def test_check_catches_schema_violation_before_gpu(qenv):
    """스키마 위반(예: 없는 experiment_id)은 Job 을 돌리기 전에 막는다."""
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))])
    doc = json.loads(jobs.read_text(encoding="utf-8"))
    doc["record"]["experiment_id"] = "EXP-99"
    jobs.write_text(json.dumps(doc), encoding="utf-8")
    r = qenv.run("run", jobs)
    assert r.returncode == 4
    assert "experiment_id" in r.stdout
    assert qenv.lines() == []


@linux_only
def test_live_process_pattern_blocks_exp_jobs_but_ignores_own_jobs(qenv):
    """Tier 0 명령줄을 찾으면 LIVE. 큐가 띄운 Job 은 같은 문자열을 써도 Tier 0 로 보지 않는다."""
    marker = f"fake-tier0-{uuid.uuid4().hex}"
    guard = {"marker_files": [], "process_patterns": [marker], "on_live": "stop", "poll_s": 0.2}
    own = qenv.job("own", ["sh", "-c", f"sleep 1; echo own >> {qenv.out}  # {marker}"])
    jobs = qenv.write([own], live_guard=guard)
    assert qenv.run("run", jobs).returncode == 0  # 자기 Job 의 명령줄은 무시
    assert qenv.lines() == ["own"]

    tier0 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
    try:
        r = qenv.run("run", jobs, "--rerun", "own")
    finally:
        tier0.kill()
        tier0.wait()
    assert r.returncode == 3, r.stdout + r.stderr
    assert f"Tier 0 프로세스 pid={tier0.pid}" in r.stdout
    assert qenv.lines() == ["own"]


@linux_only
def test_descendant_that_left_process_group_is_cleaned_up(qenv):
    """Job 이 정상 종료해도 setsid 로 그룹을 벗어난 자손이 GPU 를 잡고 남지 않게 표식으로 찾아 끝낸다."""
    pidfile = qenv.tmp / "escaped.pid"
    jobs = qenv.write([qenv.job("escape", qenv.py("escape", pidfile))])
    r = qenv.run("run", jobs)
    assert r.returncode == 0, r.stdout + r.stderr
    assert qenv.wait_dead(int(pidfile.read_text())), "그룹을 벗어난 자손이 남음"


def test_stamp_writes_git_info_for_pod_copy(qenv):
    """Pod 에 저장소 없이 파일만 복사할 때를 위해 노트북 git 정보를 Job 파일에 찍는다."""
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))])
    stamped = qenv.tmp / "jobs.stamped.json"
    r = qenv.run("stamp", jobs, "-o", stamped)
    assert r.returncode == 0, r.stdout + r.stderr
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=qenv.REPO, capture_output=True, text=True).stdout.strip()
    assert json.loads(stamped.read_text(encoding="utf-8"))["git"]["commit"] == head
    assert not qenv.state.exists()  # stamp·check 는 상태 폴더를 만들지 않는다


@linux_only
def test_sigterm_aborts_job_and_keeps_it_for_next_run(qenv):
    import signal

    flag = qenv.tmp / "slow.flag"
    flag.write_text("")
    jobs = qenv.write([qenv.job("exp", qenv.py("slow-if", flag, "exp", qenv.out))])
    proc = qenv.start("run", jobs)
    st = qenv.wait_running("exp", sleeping=flag)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 130, out
    assert qenv.wait_dead(st["pid"])
    (rec,) = qenv.records()
    assert rec["status"] == "ABORTED" and rec["failure_reason"] == "큐 중지 신호"
    assert qenv.state_of()["jobs"]["exp"]["status"] == "aborted"
    assert not (qenv.state / "queue.lock").exists()


# ---------------------------------------------------------------- Tier 0 우선 보호 빈틈 (S15P31A101-60)


def test_live_starting_during_vram_wait_does_not_start_exp_job(qenv):
    """VRAM 이 비기를 기다리는 동안 LIVE 가 시작되고 그다음 VRAM 이 비어도 Job 을 띄우지 않는다(띄우기 직전 LIVE 재확인)."""
    used = qenv.tmp / "used_mb.txt"
    used.write_text("81000", encoding="utf-8")  # 남은 559 MB < 필요 4000 MB
    smi = [qenv.py()[0], str(qenv.FAKE_SMI), str(used)]
    guard = {"marker_files": [str(qenv.live)], "process_patterns": [], "on_live": "stop", "poll_s": 5}
    jobs = qenv.write([qenv.job("exp", qenv.py("ok", "exp", qenv.out), vram_mb=4000)], gpu_query_cmd=smi,
                      live_guard=guard, vram_wait_s=30, poll_s=0.1)
    proc = qenv.start("run", jobs)
    try:
        time.sleep(1.5)  # 큐가 VRAM 대기에 들어감
        qenv.live.write_text("")  # Tier 0 LIVE 시작(기동 중 표시 파일)
        time.sleep(0.3)
        used.write_text("1024", encoding="utf-8")  # 이어서 VRAM 이 빈다
        out, _ = proc.communicate(timeout=30)
    finally:
        proc.kill()
    assert proc.returncode == 3, out
    assert qenv.lines() == [], "LIVE 중에 EXP Job 이 시작됨"
    assert qenv.records() == []
    assert qenv.state_of()["jobs"]["exp"]["status"] == "pending"


def test_live_check_command_that_cannot_run_is_treated_as_live(qenv):
    """LIVE 확인 명령을 실행하지 못하면 모름 → LIVE 로 본다(EXP 를 시작하지 않는다)."""
    guard = {"marker_files": [], "process_patterns": [], "check_command": [str(qenv.tmp / "no-such-health-check")],
             "on_live": "stop", "poll_s": 0.2}
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))], live_guard=guard)
    r = qenv.run("run", jobs)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "모름" in r.stdout
    assert qenv.lines() == []


def test_default_process_patterns_cover_tier0_entrypoint_engine_phase(qenv):
    """Tier 0 이미지 진입점(엔진 준비 → Tier 0)도 LIVE 로 본다. 셸로 바꿔 탄 경우·다른 이름은 아니다."""
    import re

    sys.path.insert(0, str(qenv.QUEUE_DIR))
    try:
        import gpu_queue
    finally:
        sys.path.pop(0)
    patterns = [re.compile(p) for p in gpu_queue.DEFAULT_GUARD["process_patterns"]]
    hit = lambda cmd: any(p.search(cmd) for p in patterns)  # noqa: E731
    assert hit("bash /usr/local/bin/simai-tier0")
    assert hit("bash /usr/local/bin/simai-tier0 --config /workspace/simai/tier0.json")
    assert hit("python -m services.tier0 --config x")
    assert not hit("bash /usr/local/bin/simai-tier0-check")
    assert not hit("python -m simai_ai.detection.engine_cache plan")


def _guard_class(qenv):
    sys.path.insert(0, str(qenv.QUEUE_DIR))
    try:
        import gpu_queue
    finally:
        sys.path.pop(0)
    return gpu_queue.LiveGuard


def test_string_check_command_failure_is_live_not_idle(qenv):
    """리뷰 R4 재현: 문자열 확인 명령은 셸이 '명령 없음'을 일반 종료 코드로 바꾼다. 실행 실패를 구별할 수 없으니 LIVE(모름)."""
    LiveGuard = _guard_class(qenv)
    cmd = "simai-no-such-healthcheck-review-60"
    g = LiveGuard({"marker_files": [], "process_patterns": [], "check_command": cmd}, qenv.tmp, lambda *_: None)
    assert subprocess.run(cmd, shell=True, capture_output=True).returncode != 0
    assert g.reason() is not None and "모름" in g.reason()
    # Job 파일에서는 아예 받지 않는다(GPU 작업 전에 종료 코드 4)
    guard = {"marker_files": [], "process_patterns": [], "check_command": cmd, "on_live": "stop"}
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))], live_guard=guard)
    r = qenv.run("check", jobs)
    assert r.returncode == 4 and "인자 목록" in r.stderr
    assert qenv.run("run", jobs).returncode == 4 and qenv.lines() == []


@pytest.mark.parametrize("code, live", [(0, True), (1, False), (2, True), (127, True)])
def test_check_command_exit_codes(qenv, code, live):
    """0 = LIVE, not_live_exit_codes(기본 1) = LIVE 아님, 그 밖(점검 도구 오류 등) = 모름 → LIVE."""
    LiveGuard = _guard_class(qenv)
    cmd = [sys.executable, "-c", f"raise SystemExit({code})"]
    g = LiveGuard({"marker_files": [], "process_patterns": [], "check_command": cmd}, qenv.tmp, lambda *_: None)
    assert (g.reason() is not None) is live


# ---------------------------------------------------------------- GPU 제공자 (S15P31A101-208)


def _site_provider():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("rr208", Path(__file__).resolve().parents[3] / "experiments" / "gpu_queue" / "run_record.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.site_provider


@pytest.mark.parametrize("env, want", [
    ({"RUNPOD_POD_ID": "abc"}, "RUNPOD"),
    ({"VAST_TCP_PORT_22": "41022"}, "VAST"),
    ({"RUNPOD_POD_ID": "abc", "VAST_TCP_PORT_22": "41022"}, "RUNPOD"),  # RunPod 신호가 먼저
    ({}, "LOCAL"),  # 신호가 없으면 클라우드로 지어내지 않는다
    ({"RUNPOD_POD_ID": "", "VAST_TCP_PORT_22": ""}, "LOCAL"),  # 빈 값은 신호가 아니다
])
def test_site_provider_uses_only_known_site_signals(env, want):
    assert _site_provider()(env) == want


def test_other_provider_job_record_passes_schema_and_unknown_provider_does_not(qenv, run_record_errors):
    rec = {"experiment_id": "INT-01", "jira_key": "S15P31A101-36", "owner": "queue-test", "experiment_state": "PLAN",
           "model_version": "cpu-sample", "input_provenance": {"frames": {"source": "SIM", "dataset_id": "sim/basic"}, "is_upper_bound": True},
           "gpu": {"provider": "OTHER"}}
    jobs = qenv.write([qenv.job("a", qenv.py("ok", "a", qenv.out))], record=rec)
    assert qenv.run("run", jobs).returncode == 0
    (r,) = qenv.records()
    assert r["gpu"]["provider"] == "OTHER" and run_record_errors(r) == []
    r["gpu"]["provider"] = "GCP"  # 음성 대조: 닫힌 목록 밖은 계약이 거른다
    assert run_record_errors(r) != []
