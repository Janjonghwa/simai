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

부하 크기는 감지한 GPU 메모리 비율로 정한다(S15P31A101-205, plan_loads): H100 80GB(nvidia-smi 81,559 MiB)에서 위 40·20·70 GB 와 같고
다른 GPU 는 같은 비율(1,000 MB 단위 반올림)이다. 상한 0.70 은 비율 그대로다. 48GB급 미만(Tier 0 전용 최소 사양 24GB급)은 분석 부하를
Tier 0 와 같은 GPU 에 올리지 않으므로 P3·P5·P6 을 SKIP + 이유로 남긴다. GPU 메모리를 읽지 못하면 SKIP 이 아니라 FAIL 이다(모르면 통과로 보지 않음).
  P7 분석 워커 접수 문(지연 초과 → 받지 않음 → 30 s 뒤 받음): 처리기·EC2 가 있어야 해서 SKIP(선택, 수동)

결과는 $SIMAI_RUN_DIR 와 $SIMAI_ROOT/runs/g0-tier0-priority/<UTC>/ 에 남긴다. 종료 코드: 0 필수 단계 통과, 1 실패 있음.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
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
REF_TOTAL_MB = 81_559  # H100 80GB 의 nvidia-smi memory.total. 이 값에서 부하가 -60 MR 값(40·20·70 GB)과 같다
BASE_LOAD_MB = {"P3": 40_000, "P5": 20_000, "P6": 70_000}
LOAD_FRACTION = 0.70  # 분석 워커 프로세스 메모리 상한 비율(SIMAI_WORKER_GPU_MEMORY_FRACTION 기본값)
MIN_SHARED_TOTAL_MB = 45_000  # Tier 0 + 분석(실험) 최소 사양 48GB급(L40S·A6000 은 약 46,000~49,000 MiB)
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
    total_mb = gpu_total_mb(shlex.split(os.environ.get("SIMAI_NVIDIA_SMI") or "nvidia-smi"))
    loads = plan_loads(total_mb, int(os.environ.get("SIMAI_TIER0_GPU_RESERVE_MB") or 8192))
    steps.metrics["gpu_total_mb"] = total_mb
    steps.metrics["loads"] = json.dumps(loads, ensure_ascii=False)

    total = dur["baseline"] + dur["load"] + dur["kill_watch"] + dur["oom"] + 300  # 측정 사이 여유
    harness = None
    ready = False
    try:
        if PROBE.is_file():
            harness = sl.LiveHarness(out / "live", seconds=total)
            harness.start(sl.encode_stream(250))
            ready = harness.wait_ready(args.ready_timeout_s)
        marker = harness.marker if harness else None

        def need(fn, load_key=None):  # SKIP 인 부하 단계는 선택 단계로 남는다(필수 실패로 세지 않음)
            def wrapped():
                if load_key and loads[load_key]["status"] != "RUN":
                    plan = loads[load_key]
                    return plan["status"], plan["why"], {"gpu_total_mb": total_mb}
                if not PROBE.is_file():
                    return "FAIL", f"{PROBE} 없음 (-60 머지 필요)", {}
                if not ready:
                    return "FAIL", "LIVE 하네스가 준비되지 않음 (c4 와 tier0-live.log 를 본다)", {}
                return fn()
            return wrapped

        steps.run("P1_reserve", True, need(lambda: _p1(marker, out)))
        steps.run("P2_baseline", True, need(lambda: _watch(py, marker, dur["baseline"], out / "baseline.json")))
        steps.run("P3_with_load", loads["P3"]["status"] != "SKIP",
                  need(lambda: _p3(py, marker, dur, out, harness, loads["P3"]["mem_mb"]), "P3"))
        steps.run("P4_vlm_load", False, lambda: ("SKIP", "VLM 처리기(vLLM)가 아직 없다(선택)", {}))
        steps.run("P5_kill_load", loads["P5"]["status"] != "SKIP",
                  need(lambda: _p5(py, marker, dur, out, loads["P5"]["mem_mb"]), "P5"))
        steps.run("P6_over_limit", loads["P6"]["status"] != "SKIP",
                  need(lambda: _p6(py, marker, dur, out, loads["P6"]["mem_mb"]), "P6"))
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


def gpu_total_mb(smi: list[str]) -> float | None:
    """첫 GPU 의 memory.total(MiB). 읽지 못하면 None(부하 크기를 모른다)."""
    try:
        r = subprocess.run([*smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip().splitlines()[0]) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def plan_loads(total_mb: float | None, reserve_mb: int) -> dict:
    """P3·P5·P6 부하. status RUN(mem_mb 로 돈다)·SKIP(의미 없음)·FAIL(GPU 메모리를 모름)."""
    if total_mb is None:
        return {k: {"status": "FAIL", "mem_mb": None, "why": "GPU 메모리를 읽지 못해 부하 크기를 정할 수 없다"} for k in BASE_LOAD_MB}
    cap = LOAD_FRACTION * total_mb
    out = {}
    for key, base in BASE_LOAD_MB.items():
        mem = int(round(base * total_mb / REF_TOTAL_MB / 1000) * 1000)
        if total_mb < MIN_SHARED_TOTAL_MB:
            out[key] = {"status": "SKIP", "mem_mb": mem, "why": f"GPU {total_mb:.0f} MiB 는 Tier 0 전용 사양(48GB급 미만): "
                                                               "분석 부하를 같은 GPU 에 올리지 않아 이 측정은 의미가 없다"}
        elif key != "P6" and (mem > cap or mem + reserve_mb > total_mb):
            out[key] = {"status": "SKIP", "mem_mb": mem, "why": f"부하 {mem} MB 가 상한 {cap:.0f} MB 또는 남은 메모리"
                                                               f"(전체 {total_mb:.0f} - 선확보 {reserve_mb})를 넘는다"}
        elif key == "P6" and mem <= cap:
            out[key] = {"status": "SKIP", "mem_mb": mem, "why": f"부하 {mem} MB 가 상한 {cap:.0f} MB 를 넘지 않아 상한 시험이 아니다"}
        else:
            out[key] = {"status": "RUN", "mem_mb": mem, "why": ""}
    return out


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


def _watch_env(run_dir: Path) -> dict[str, str]:
    """probe 의 metrics.json 이 이 단계 것을 덮지 않게 watch 마다 따로 둔 SIMAI_RUN_DIR. probe 는 폴더를 만들지 않으므로 여기서 만든다."""
    run_dir.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "SIMAI_RUN_DIR": str(run_dir)}


def _watch(py: str, marker: Path, seconds: float, path: Path):
    env = _watch_env(path.parent / f".watch-{path.stem}")
    r = subprocess.run(_watch_cmd(py, marker, seconds, path), env=env, timeout=seconds + 120)
    return _watch_result(r.returncode, path)


def _load(py: str, mem_mb: int, seconds: float, out: Path, name: str) -> subprocess.Popen:
    logf = open(out / f"load-{name}.log", "ab")  # noqa: SIM115
    return subprocess.Popen([py, str(PROBE), "load", "--mem-mb", str(mem_mb), "--seconds", str(seconds),
                             "--fraction", str(LOAD_FRACTION)], stdout=logf, stderr=subprocess.STDOUT)


def _stop(proc: subprocess.Popen) -> int | None:
    if proc.poll() is None:
        proc.kill()
    try:
        return proc.wait(30)
    except subprocess.TimeoutExpired:
        return None


def _p3(py, marker, dur, out, harness, mem_mb):
    log = harness.log_path
    before = log.read_bytes().count(b"out of memory") if log.exists() else 0
    load = _load(py, mem_mb, dur["load"] + 60, out, "p3")
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


def _p5(py, marker, dur, out, mem_mb):
    load = _load(py, mem_mb, dur["kill_watch"] + 120, out, "p5")
    path = out / "kill.json"
    env = _watch_env(out / ".watch-kill")
    watch = subprocess.Popen(_watch_cmd(py, marker, dur["kill_watch"], path), env=env)
    time.sleep(dur["kill_after"])
    load.kill()  # kill -9 (POSIX SIGKILL)
    load.wait(30)
    code = watch.wait(dur["kill_watch"] + 120)
    status, detail, values = _watch_result(code, path)
    return status, f"부하 kill -9 뒤 {detail}", values


def _p6(py, marker, dur, out, mem_mb):
    load = _load(py, mem_mb, dur["oom"], out, "p6")
    try:
        status, detail, values = _watch(py, marker, dur["oom"], out / "over_limit.json")
    finally:
        code = load.wait(dur["oom"] + 120) if load.poll() is None else load.returncode
    text = (out / "load-p6.log").read_text(encoding="utf-8", errors="replace")
    oom = code not in (0, None) and "OutOfMemory" in text
    values["load_exit"] = code
    if not oom:
        status = "FAIL"
    return status, f"{mem_mb} MB 부하 종료 {code} (메모리 부족 {oom}), {detail}", values


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
