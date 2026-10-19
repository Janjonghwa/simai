#!/usr/bin/env python3
"""GPU 1차 세션 큐 d 항목: Tier 0 GPU 우선 보호 Pod 측정 (S15P31A101-60 'Pod 측정' P0~P7). -60 머지 뒤 경로 기준이다.

    python deploy/scripts/session1_gpu_priority.py            # 1차 세션용 줄인 시간
    python deploy/scripts/session1_gpu_priority.py --full     # G0 판정용 시간(-60 MR 값: watch 600 s 등)

Tier 0 는 c 항목과 같은 LIVE 하네스(session1_live.LiveHarness: 진입점 → 엔진 → Tier 0 LIVE + 노트북 흉내 + 영상 4대)로 띄운다.
측정 도구는 ai/experiments/gpu_queue/tier0_priority_probe.py(load: 더미 분석 부하, watch: 표시 파일 gpu_priority 판정)다.

  P0 pytest tests/experiments/gpu_queue tests/gpu tests/services/worker/test_runner_tier0_priority.py (Linux 전용 시험 포함) 통과
  P1 표시 파일 gpu_priority: phase=running, reserve.state=OK, reserved_mb ≥ 8192. nvidia-smi compute-apps 기록
  P2 부하 없는 기준선 watch: passed (lag p95 ≤ 600 ms, unknown 0, 판정 오류 증가 0)
  P3 더미 부하 40 GB(상한 0.70) 동안 watch: passed, Tier 0 로그 'out of memory' 0건 (G0 동시 실행)
  P4 VLM(vLLM) 부하: 처리기가 아직 없어 SKIP(선택)
  P5 더미 부하 20 GB 를 watch 도중 kill -9: watch passed (D-G0-08 판정 지속)
  P6 상한(0.70 = 56 GB)을 넘는 70 GB 부하는 메모리 부족으로 끝나고 watch 는 passed
  P7 분석 워커 접수 문(지연 초과 → 받지 않음 → 30 s 뒤 받음): 처리기·EC2 가 있어야 해서 SKIP(선택, 수동)

결과는 $SIMAI_RUN_DIR 와 $SIMAI_ROOT/runs/g0-tier0-priority/<UTC>/ 에 남긴다. 종료 코드: 0 필수 단계 통과, 1 실패 있음.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import session1_live as sl  # noqa: E402

AI = sl.AI
PROBE = AI / "experiments" / "gpu_queue" / "tier0_priority_probe.py"
SHORT = {"baseline": 120, "load": 120, "kill_watch": 90, "kill_after": 30, "oom": 60}
FULL = {"baseline": 600, "load": 600, "kill_watch": 180, "kill_after": 60, "oom": 120}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Tier 0 GPU 우선 보호 Pod 측정 (S15P31A101-60 → -183 d 항목)")
    ap.add_argument("--full", action="store_true", help="G0 판정용 시간(-60 MR 값)")
    ap.add_argument("--ready-timeout-s", type=float, default=600)
    args = ap.parse_args(argv)
    dur = FULL if args.full else SHORT
    out = Path(os.environ.get("SIMAI_RUN_DIR") or "session1-gpu-priority").resolve()
    out.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    steps = sl.Steps(out)
    steps.metrics["durations"] = json.dumps(dur)

    steps.run("P0_pytest", True, lambda: _pytest(py, out))

    total = dur["baseline"] + dur["load"] + dur["kill_watch"] + dur["oom"] + 300  # 측정 사이 여유
    harness = None
    ready = False
    try:
        if PROBE.is_file():
            harness = sl.LiveHarness(out / "live", seconds=total)
            harness.start(sl.encode_stream(250))
            ready = harness.wait_ready(args.ready_timeout_s)
        marker = harness.marker if harness else None

        def need(fn):
            def wrapped():
                if not PROBE.is_file():
                    return "FAIL", f"{PROBE} 없음 (-60 머지 필요)", {}
                if not ready:
                    return "FAIL", "LIVE 하네스가 준비되지 않음 (c4 와 tier0-live.log 를 본다)", {}
                return fn()
            return wrapped

        steps.run("P1_reserve", True, need(lambda: _p1(marker, out)))
        steps.run("P2_baseline", True, need(lambda: _watch(py, marker, dur["baseline"], out / "baseline.json")))
        steps.run("P3_with_load", True, need(lambda: _p3(py, marker, dur, out, harness)))
        steps.run("P4_vlm_load", False, lambda: ("SKIP", "VLM 처리기(vLLM)가 아직 없다(선택)", {}))
        steps.run("P5_kill_load", True, need(lambda: _p5(py, marker, dur, out)))
        steps.run("P6_over_limit", True, need(lambda: _p6(py, marker, dur, out)))
        steps.run("P7_worker_gate", False, lambda: ("SKIP", "분석 워커 처리기·EC2 가 있어야 한다(수동, -60 MR P7)", {}))
    finally:
        if harness is not None:
            harness.stop.set()
            if harness.proc is not None and harness.proc.poll() is None:
                harness.proc.send_signal(signal.SIGTERM if os.name != "nt" else signal.CTRL_BREAK_EVENT)
                try:
                    harness.proc.wait(30)
                except subprocess.TimeoutExpired:
                    pass
            harness.close()
    steps.write()
    _copy_to_volume(out)
    sl.log(f"session1_gpu_priority: 필수 단계 실패 {steps.failed()}")
    return 1 if steps.failed() else 0


def _pytest(py: str, out: Path):
    tests = ["tests/experiments/gpu_queue", "tests/gpu", "tests/services/worker/test_runner_tier0_priority.py"]
    missing = [t for t in tests if not (AI / t).exists()]
    if missing:
        return "FAIL", f"시험 없음: {missing} (-60 머지 필요)", {}
    r = subprocess.run([py, "-m", "pytest", "-q", *tests, "-p", "no:cacheprovider", f"--junitxml={out / 'junit-p0.xml'}"],
                       cwd=AI, timeout=1800)
    return ("PASS" if r.returncode == 0 else "FAIL"), f"pytest 종료 {r.returncode}", {"exit": r.returncode}


def _p1(marker: Path, out: Path):
    gp = json.loads(marker.read_text(encoding="utf-8")).get("gpu_priority") or {}
    reserve = gp.get("reserve") or {}
    smi = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv"],
                         capture_output=True, text=True, timeout=60)
    (out / "compute-apps.csv").write_text(smi.stdout, encoding="utf-8")
    ok = gp.get("phase") == "running" and reserve.get("state") == "OK" and (reserve.get("reserved_mb") or 0) >= 8192
    return ("PASS" if ok else "FAIL"), f"phase={gp.get('phase')}, reserve={reserve.get('state')} {reserve.get('reserved_mb')} MB", \
        {"phase": gp.get("phase"), "reserve_state": reserve.get("state"), "reserved_mb": reserve.get("reserved_mb")}


def _watch_cmd(py: str, marker: Path, seconds: float, path: Path) -> list[str]:
    return [py, str(PROBE), "watch", "--marker", str(marker), "--seconds", str(seconds), "--out", str(path)]


def _watch_result(code: int | None, path: Path) -> tuple[str, str, dict]:
    try:
        res = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "FAIL", f"watch 결과 없음 (exit {code})", {}
    values = {k: v for k, v in res.items() if isinstance(v, (int, float, str, bool)) or v is None}
    ok = code == 0 and res.get("passed") is True
    return ("PASS" if ok else "FAIL"), f"watch passed={res.get('passed')} lag p95={res.get('lag_ms_p95')}", values


def _watch(py: str, marker: Path, seconds: float, path: Path):
    env = {**os.environ, "SIMAI_RUN_DIR": str(path.parent / f".watch-{path.stem}")}  # probe 의 metrics.json 이 이 단계 것을 덮지 않게
    r = subprocess.run(_watch_cmd(py, marker, seconds, path), env=env, timeout=seconds + 120)
    return _watch_result(r.returncode, path)


def _load(py: str, mem_mb: int, seconds: float, out: Path, name: str) -> subprocess.Popen:
    logf = open(out / f"load-{name}.log", "ab")  # noqa: SIM115
    return subprocess.Popen([py, str(PROBE), "load", "--mem-mb", str(mem_mb), "--seconds", str(seconds),
                             "--fraction", "0.70"], stdout=logf, stderr=subprocess.STDOUT)


def _stop(proc: subprocess.Popen) -> int | None:
    if proc.poll() is None:
        proc.kill()
    try:
        return proc.wait(30)
    except subprocess.TimeoutExpired:
        return None


def _p3(py, marker, dur, out, harness):
    log = harness.log_path
    before = log.read_bytes().count(b"out of memory") if log.exists() else 0
    load = _load(py, 40_000, dur["load"] + 60, out, "p3")
    try:
        time.sleep(15)  # 부하가 메모리를 잡고 돌기 시작할 때까지
        status, detail, values = _watch(py, marker, dur["load"], out / "with_load.json")
    finally:
        _stop(load)
    oom = (log.read_bytes().count(b"out of memory") if log.exists() else 0) - before
    values["tier0_oom_lines"] = oom
    if oom:
        status = "FAIL"
    return status, f"{detail}, Tier 0 'out of memory' {oom}건", values


def _p5(py, marker, dur, out):
    load = _load(py, 20_000, dur["kill_watch"] + 120, out, "p5")
    path = out / "kill.json"
    env = {**os.environ, "SIMAI_RUN_DIR": str(out / ".watch-kill")}
    watch = subprocess.Popen(_watch_cmd(py, marker, dur["kill_watch"], path), env=env)
    time.sleep(dur["kill_after"])
    load.kill()  # kill -9 (POSIX SIGKILL)
    load.wait(30)
    code = watch.wait(dur["kill_watch"] + 120)
    status, detail, values = _watch_result(code, path)
    return status, f"부하 kill -9 뒤 {detail}", values


def _p6(py, marker, dur, out):
    load = _load(py, 70_000, dur["oom"], out, "p6")
    try:
        status, detail, values = _watch(py, marker, dur["oom"], out / "over_limit.json")
    finally:
        code = load.wait(dur["oom"] + 120) if load.poll() is None else load.returncode
    text = (out / "load-p6.log").read_text(encoding="utf-8", errors="replace")
    oom = code not in (0, None) and "OutOfMemory" in text
    values["load_exit"] = code
    if not oom:
        status = "FAIL"
    return status, f"70 GB 부하 종료 {code} (메모리 부족 {oom}), {detail}", values


def _copy_to_volume(out: Path) -> None:
    root = Path(os.environ.get("SIMAI_ROOT") or "/workspace/simai")
    dest = root / "runs" / "g0-tier0-priority" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for p in out.glob("*.json"):
            shutil.copy2(p, dest / p.name)
    except OSError as e:
        sl.log(f"결과를 {dest} 에 복사하지 못함: {e}")


if __name__ == "__main__":
    sys.exit(main())
