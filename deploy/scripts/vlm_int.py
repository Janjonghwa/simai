#!/usr/bin/env python3
"""통합 시험 v-int 재확인 (세션 큐 v4, S15P31A101-291 → -278 재측정). 통합 이미지 Pod 에서 Tier 0 LIVE 하네스와 vLLM 을 함께 띄운다.

3차 GPU 세션(2026-10-26)의 v-int 는 손으로 돌렸다(스크립트는 남지 않음, 결과 summary.json 의 단계·인자만 남음). 같은 단계를 재현한다:
  A  Tier 0 단독(--baseline-s, 기본 90 s): c4 와 같은 LIVE 하네스(session1_live.LiveHarness, 무늬 영상 4대 2304x1296 25 fps)
  B  vLLM 기동: 인자·환경·CPU 묶기·nice 를 감시 프로세스(simai_bootstrap.Supervisor)가 만드는 그대로 쓴다. 그래서 -278 의 기본값
     (--max-model-len 32768, VLLM_USE_FLASHINFER_SAMPLER=0, cgroup 할당량 기준 뒤쪽 n//2 CPU)이 실제로 들어간 채 잰다.
     /health 200 까지 시간 → 데움 1회 → 동시 1(--n1) → 동시 4(--n4)
  C  지속 동시 4(--load-s, 기본 600 s): G7(전체 지연 p95 ≤ 5 s)과 Tier 0 영향(frame_age·pipeline_lag·CPU·loadavg)
  D  vLLM 리더만 kill -9 → 10 s 뒤 같은 그룹에 남은 프로세스·GPU 메모리 → 감시 프로세스의 그룹 정리(Proc.signal(SIGKILL)) → 그룹 비었는지
요청마다 질문 앞에 다른 표식을 붙여 같은 그림이어도 prefix 캐시로 지연이 낙관적으로 나오지 않게 한다(3차 결과의 잔여 위험).

판정(PASS = 종료 0): 기본값이 그대로 들어감, vLLM 이 시한 안에 /health 200, 세 요청 단계 오류 0·전체 p95 ≤ 5 s, A~C 내내 카메라 4대
ready·reserve OK(표본 없음은 모름 → 실패), vLLM 이 계획한 CPU 에 묶임, D 의 그룹 정리 뒤 그룹이 빔. frame_age·CPU 는 기록만.
환경이 기본값을 덮어썼거나(이 시험은 기본값을 잰다) Cosmos3-Nano 가중치·질문 그림이 없으면 GPU 를 쓰기 전에 종료 2.
결과: $SIMAI_RUN_DIR/summary.json(단계·인자·판정), metrics.json(스칼라). 무늬 영상·시뮬레이터 보정이라 제품 성능 지표가 아니다(AGENTS 7).

    SIMAI_WEIGHTS_SET=vlm simai-bootstrap check        # Cosmos3-Nano 31.5GB (한 번, 검증 스탬프)
    python deploy/scripts/vlm_int.py --images /srv/simai/datasets/classroom4-prepared-20261026
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import shlex
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import simai_bootstrap as bs  # noqa: E402 - 같은 폴더(감시 프로세스와 같은 인자·환경·CPU 묶기)

G7_P95_S = 5.0  # 명세 15장 G7
QUESTION = "이 교실 그림에서 사람이 몇 명 보이고, 넘어지거나 위험해 보이는 사람이 있는지 한 문장으로 답하라."
SAMPLE_S = 1.0
EXIT_OK, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2


class _Stop(Exception):
    """측정을 더 할 수 없다(기록하고 판정으로 넘어간다)."""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}", file=sys.stderr, flush=True)


def pct(values, q: float):
    s = sorted(v for v in values if isinstance(v, (int, float)))
    if not s:
        return None
    return round(s[min(len(s) - 1, max(0, round(q / 100 * (len(s) - 1))))], 3)


# ---------------------------------------------------------------- 감시 프로세스와 같은 vLLM 계획


def vllm_plan(root: Path, env: dict[str, str]) -> dict:
    """감시 프로세스(Supervisor)가 vLLM 을 띄울 때의 인자·환경·CPU·nice. Supervisor 를 만들기만 하고 띄우지 않는다."""
    sup = bs.Supervisor(root, ["tier0", "vllm"], [], dict(env), ready=lambda: (True, ""), health=lambda: False)
    svc = sup.svc["vllm"]
    quota, src = bs.cgroup_vcpu()
    return {"argv": svc.argv, "env": svc.env, "cpus": sorted(sup.cpus) if sup.cpus else None, "nice": sup.nice,
            "affinity": len(bs.usable_cpus()), "quota": quota, "quota_source": src, "cpu_note": sup.cpu_note,
            "port": sup.port, "ready_timeout_s": sup.ready_timeout_s}


def defaults_problems(plan: dict, env: dict[str, str]) -> list[str]:
    """이 시험은 -278 기본값을 잰다. 환경이 덮어썼으면 무엇을 덮었는지(빈 목록 = 기본값 그대로)."""
    out = []
    argv = plan["argv"]
    lens = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--max-model-len"]
    lens += [a.split("=", 1)[1] for a in argv if a.split("=", 1)[0].replace("_", "-") == "--max-model-len" and "=" in a]
    if lens != [bs.VLLM_MAX_MODEL_LEN]:
        out.append(f"--max-model-len {lens or '없음'} (기본 {bs.VLLM_MAX_MODEL_LEN}, SIMAI_WORKER_VLLM_MAX_MODEL_LEN·SIMAI_VLLM_EXTRA_ARGS 확인)")
    if plan["env"].get("VLLM_USE_FLASHINFER_SAMPLER") != "0":
        out.append(f"VLLM_USE_FLASHINFER_SAMPLER={plan['env'].get('VLLM_USE_FLASHINFER_SAMPLER')!r} (기본 '0')")
    spec = (env.get("SIMAI_SIDECAR_CPUS") or "auto").strip().lower()
    if spec != "auto":
        out.append(f"SIMAI_SIDECAR_CPUS={spec!r} (기본 auto: cgroup 할당량 기준)")
    return out


def expected_cpu_count(affinity: int, quota: float | None) -> int | None:
    n = affinity if quota is None else min(affinity, math.ceil(quota))
    return n // 2 if n >= 8 else None


# ---------------------------------------------------------------- /proc·nvidia-smi (Linux Pod)


def proc_stat(pid: int, root: Path = Path("/proc")) -> tuple[str, int, int] | None:
    """(상태 글자, pgrp, utime+stime 틱). 없으면 None."""
    try:
        raw = (root / str(pid) / "stat").read_text()
        rest = raw[raw.rindex(")") + 2:].split()
        return rest[0], int(rest[2]), int(rest[11]) + int(rest[12])
    except (OSError, ValueError, IndexError):
        return None


def group_pids(pgid: int, root: Path = Path("/proc")) -> list[int]:
    """그룹에서 끝나지 않은 프로세스. 좀비(Z)는 뺀다(simai_bootstrap._group_alive 와 같은 기준: 이미 끝났고 거둘 일만 남음)."""
    out = []
    for p in root.iterdir():
        if p.name.isdigit() and (st := proc_stat(int(p.name), root)) is not None and st[1] == pgid and st[0] != "Z":
            out.append(int(p.name))
    return sorted(out)


def kill9_check(proc, smi: list[str], settle_s: float = 10.0, wait_s: float = 10.0) -> dict:
    """vLLM 리더만 SIGKILL → settle_s 뒤 그룹에 남은 프로세스(고아)·GPU → 감시 프로세스의 그룹 정리(_reap 의 Proc.signal(SIGKILL)) →
    그룹이 비는지. 리더는 이 프로세스의 직접 자식이라 거둔다(안 거두면 좀비로 남는다. 좀비는 group_pids 가 뺀다)."""
    before = group_pids(proc.pid)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(wait_s)
    time.sleep(settle_s)
    left = [p for p in group_pids(proc.pid) if p != proc.pid]
    used10, _ = gpu_now(smi)
    proc.signal(bs.KILL)  # killpg: 리더가 끝나도 남은 그룹(EngineCore 등)에 간다
    end = time.monotonic() + wait_s
    while group_pids(proc.pid) and time.monotonic() < end:
        time.sleep(0.5)
    used_after, _ = gpu_now(smi)
    return {"group_before": len(before), "orphans_after_10s": len(left), "gpu_used_mb_after_10s": used10,
            "group_after_killpg": group_pids(proc.pid), "gpu_used_mb_after_killpg": used_after}


def cpus_allowed(pid: int) -> list[int] | None:
    try:
        return sorted(os.sched_getaffinity(pid))
    except (OSError, AttributeError):
        return None


def gpu_now(smi: list[str]) -> tuple[float | None, float | None]:
    try:
        r = subprocess.run([*smi, "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        used, util = (float(x) for x in r.stdout.strip().splitlines()[0].split(",")[:2])
        return used, util
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None, None


def loadavg1() -> float | None:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


class Sampler(threading.Thread):
    """1 s 마다 표시 파일(카메라·frame_age·reserve), Tier 0·vLLM 그룹 CPU %, GPU, loadavg 를 단계 이름과 함께 남긴다."""

    def __init__(self, harness, smi: list[str]) -> None:
        super().__init__(name="sampler", daemon=True)
        self.h, self.smi = harness, smi
        self.phase = "A"
        self.vllm_pgid: int | None = None
        self.rows: list[dict] = []
        self.stop = threading.Event()
        self.tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def run(self) -> None:
        last: dict[str, tuple[float, int]] = {}
        while not self.stop.wait(SAMPLE_S):
            now = time.monotonic()
            row: dict = {"phase": self.phase, "lag_count": len(self.h.lag_ms())}
            live = self.h.sample() or {}
            cams = live.get("cameras") or {}
            row["cameras_ready"] = sum(1 for c in cams.values() if c.get("ready")) if cams else None
            ages = [c.get("frame_age_ms_p95") for c in cams.values() if isinstance(c.get("frame_age_ms_p95"), (int, float))]
            row["frame_age_p95_max"] = max(ages) if ages else None
            gp = (self.h.samples[-1].get("gpu_priority") or {}) if self.h.samples else {}
            row["reserve_state"] = (gp.get("reserve") or {}).get("state")
            for name, pids in (("tier0", [self.h.proc.pid] if self.h.proc else []),
                               ("vllm", group_pids(self.vllm_pgid) if self.vllm_pgid else [])):
                ticks = sum((proc_stat(p) or ("", 0, 0))[2] for p in pids)
                if name in last and pids:
                    t, k = last[name]
                    row[f"{name}_cpu_pct"] = round(100 * (ticks - k) / self.tick / max(now - t, 1e-3), 1)
                last[name] = (now, ticks)
            row["gpu_used_mb"], row["gpu_util"] = gpu_now(self.smi)
            row["loadavg1"] = loadavg1()
            self.rows.append(row)


def phase_summary(rows: list[dict], lags: list[float], phase: str) -> dict:
    sel = [r for r in rows if r["phase"] == phase]
    med = lambda k: round(statistics.median(v), 1) if (v := [r[k] for r in sel if isinstance(r.get(k), (int, float))]) else None  # noqa: E731
    top = lambda k: max(v) if (v := [r[k] for r in sel if isinstance(r.get(k), (int, float))]) else None  # noqa: E731
    lo = min((r["lag_count"] for r in sel), default=0)
    hi = max((r["lag_count"] for r in sel), default=0)
    states: dict[str, int] = {}
    for r in sel:
        states[str(r.get("reserve_state"))] = states.get(str(r.get("reserve_state")), 0) + 1
    ready = [r["cameras_ready"] for r in sel if isinstance(r.get("cameras_ready"), int)]
    return {"samples": len(sel), "frame_age_p95_max_median": med("frame_age_p95_max"), "frame_age_p95_max_max": top("frame_age_p95_max"),
            "tier0_cpu_pct_median": med("tier0_cpu_pct"), "vllm_cpu_pct_median": med("vllm_cpu_pct"),
            "gpu_used_mb_max": top("gpu_used_mb"), "gpu_util_median": med("gpu_util"), "loadavg1_max": top("loadavg1"),
            "reserve_states": states, "cameras_ready_min": min(ready) if len(ready) == len(sel) and sel else None,
            "pipeline_lag_ms_p50": pct(lags[lo:hi], 50), "pipeline_lag_ms_p95": pct(lags[lo:hi], 95), "lag_samples": hi - lo}


# ---------------------------------------------------------------- 요청


def chat_once(url: str, model: str, image_url: str, max_tokens: int, timeout: float = 120.0) -> dict:
    """스트리밍 /v1/chat/completions 한 번. 첫 토큰·전체 시간(초), 오류. 질문 앞 표식으로 prefix 캐시를 피한다."""
    body = {"model": model, "max_tokens": max_tokens, "stream": True, "temperature": 0,
            "messages": [{"role": "user", "content": [{"type": "text", "text": f"[요청 {uuid.uuid4().hex[:12]}] {QUESTION}"},
                                                      {"type": "image_url", "image_url": {"url": image_url}}]}]}
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.monotonic()
    ttft, text = None, []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - 127.0.0.1 vLLM
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                delta = ((json.loads(line[5:]).get("choices") or [{}])[0].get("delta") or {}).get("content")
                if delta:
                    ttft = ttft if ttft is not None else time.monotonic() - t0
                    text.append(delta)
    except (OSError, ValueError, urllib.error.URLError) as e:
        return {"ttft": ttft, "total": round(time.monotonic() - t0, 3), "error": f"{type(e).__name__}: {e}"[:200]}
    total = round(time.monotonic() - t0, 3)
    if not text:
        return {"ttft": None, "total": total, "error": "빈 답"}
    return {"ttft": round(ttft, 3), "total": total, "error": None, "chars": len("".join(text))}


def burst(url: str, model: str, images: list[str], n: int, conc: int, max_tokens: int,
          until: float | None = None) -> tuple[dict, list[dict]]:
    """n 회(until 이 있으면 그 시각까지) 동시 conc 로. 그림은 돌려 쓴다."""
    out: list[dict] = []
    lock = threading.Lock()
    count = iter(range(10**9))

    def worker() -> None:
        while True:
            i = next(count)
            if (until is None and i >= n) or (until is not None and time.monotonic() >= until):
                return
            r = chat_once(url, model, images[i % len(images)], max_tokens)
            with lock:
                out.append(r)

    with ThreadPoolExecutor(conc) as ex:
        for _ in range(conc):
            ex.submit(worker)
    return request_stats(out), out


def request_stats(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error")]
    total_p95 = pct([r["total"] for r in ok], 95)
    return {"n": len(rows), "errors": len(rows) - len(ok), "ttft_p50": pct([r["ttft"] for r in ok], 50),
            "ttft_p95": pct([r["ttft"] for r in ok], 95), "total_p50": pct([r["total"] for r in ok], 50), "total_p95": total_p95,
            "g7_total_p95_le_5s": bool(ok) and total_p95 is not None and total_p95 <= G7_P95_S}


def load_images(folder: Path, width: int) -> list[str]:
    """질문 그림(입력 폴더의 cam_*.jpg, 사람 마스크 PNG 제외)을 가로 width 로 줄여 JPEG data URL 로."""
    files = sorted(folder.glob("*.jpg"))
    if not files:
        return []
    import cv2  # 이미지 venv(opencv-python-headless)

    out = []
    for p in files:
        img = cv2.imread(str(p))
        if img is None:
            continue
        h, w = img.shape[:2]
        if w > width:
            img = cv2.resize(img, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ok:
            out.append("data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode())
    return out


# ---------------------------------------------------------------- 판정


def judge(s: dict) -> list[str]:
    """실패 이유 목록(빈 목록 = PASS). 모르는 값(표본 없음·None)은 통과로 보지 않는다."""
    why = [f"기본값 아님: {p}" for p in s.get("defaults_problems") or []]
    if s.get("vllm_ready_s") is None:
        why.append("vLLM 이 시한 안에 /health 200 이 아님")
    for k in ("concurrency1", "concurrency4", "sustained4"):
        r = s.get("requests", {}).get(k)
        if not r or not r.get("n"):
            why.append(f"{k}: 요청 없음")
        elif r["errors"] or not r["g7_total_p95_le_5s"]:
            why.append(f"{k}: 오류 {r['errors']}, 전체 p95 {r['total_p95']} s (G7 ≤ {G7_P95_S} s)")
    for ph in ("A", "B", "C"):
        p = s.get("phases", {}).get(ph) or {}
        if not p.get("samples"):
            why.append(f"단계 {ph}: 표본 없음(모름)")
            continue
        if p.get("cameras_ready_min") != s.get("cameras"):
            why.append(f"단계 {ph}: 카메라 ready 최소 {p.get('cameras_ready_min')}/{s.get('cameras')}")
        if set(p.get("reserve_states") or {}) != {"OK"}:
            why.append(f"단계 {ph}: reserve 상태 {p.get('reserve_states')}")
    plan_cpus = (s.get("plan") or {}).get("cpus")
    if plan_cpus is not None and s.get("vllm_affinity") != plan_cpus:
        why.append(f"vLLM CPU 묶기 {s.get('vllm_affinity')} ≠ 계획 {plan_cpus}")
    if (s.get("plan") or {}).get("cpus_expected") != (len(plan_cpus) if plan_cpus else None):
        why.append(f"CPU 묶기 개수 {len(plan_cpus) if plan_cpus else None} ≠ 할당량 기준 {(s.get('plan') or {}).get('cpus_expected')}")
    kill = s.get("kill9")
    if kill is None:
        why.append("D(kill -9 뒤 그룹 정리) 단계를 하지 못함")
    elif kill.get("group_after_killpg") != []:
        why.append(f"그룹 정리 뒤에도 남은 프로세스 {kill.get('group_after_killpg')}")
    return why


def flat(s: dict) -> dict:
    m = {"passed": not s["fail_reasons"], "fail_reasons": len(s["fail_reasons"]), "vllm_ready_s": s.get("vllm_ready_s"),
         "sidecar_cpus": len(s["plan"]["cpus"]) if s["plan"].get("cpus") else 0}
    for k, r in (s.get("requests") or {}).items():
        m |= {f"{k}_{f}": r.get(f) for f in ("n", "errors", "total_p95", "ttft_p95")}
    for ph, p in (s.get("phases") or {}).items():
        m |= {f"{ph}_{f}": p.get(f) for f in ("frame_age_p95_max_max", "pipeline_lag_ms_p95", "tier0_cpu_pct_median",
                                              "vllm_cpu_pct_median", "gpu_used_mb_max", "loadavg1_max")}
    m |= {f"kill9_{k}": v for k, v in (s.get("kill9") or {}).items() if isinstance(v, (int, float))}
    return m


# ---------------------------------------------------------------- 실행 (Pod)


def run(a: argparse.Namespace) -> int:
    import session1_live as sl  # LIVE 하네스(Tier 0 시험 도구 경로를 sys.path 에 더한다)

    out = Path(os.environ.get("SIMAI_RUN_DIR") or a.out or "vlm-int").resolve()
    out.mkdir(parents=True, exist_ok=True)
    root = Path(os.environ.get("SIMAI_ROOT") or "/srv/simai")
    env = dict(os.environ)
    plan = vllm_plan(root, env)
    plan["cpus_expected"] = expected_cpu_count(plan["affinity"], plan["quota"])
    s: dict = {"started_at": time.time_ns() // 1_000_000, "args": {k: str(v) for k, v in vars(a).items()},
               "plan": {k: v for k, v in plan.items() if k != "env"},
               "vllm_env": {k: plan["env"].get(k) for k in ("VLLM_USE_FLASHINFER_SAMPLER", "VLLM_USE_DEEP_GEMM", "HF_HUB_OFFLINE",
                                                            "OMP_NUM_THREADS")},
               "defaults_problems": defaults_problems(plan, env), "requests": {}, "phases": {}}
    model_dir = Path(plan["argv"][plan["argv"].index("serve") + 1])
    images = load_images(Path(a.images), a.image_width) if Path(a.images).is_dir() else []
    pre = list(s["defaults_problems"])
    if not (model_dir / "config.json").is_file():
        pre.append(f"Cosmos3-Nano 가중치 없음({model_dir}): SIMAI_WEIGHTS_SET=vlm simai-bootstrap check 로 먼저 받는다")
    if not images:
        pre.append(f"질문 그림 없음({a.images}/*.jpg)")
    log(plan["cpu_note"])
    log(f"vLLM 인자(감시 프로세스와 같음): {shlex.join(plan['argv'])}")
    if pre:
        for p in pre:
            log(f"시작하지 않음: {p}")
        s["config_errors"] = pre
        (out / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return EXIT_CONFIG
    smi = shlex.split(os.environ.get("SIMAI_NVIDIA_SMI") or "nvidia-smi")
    url = f"http://127.0.0.1:{plan['port']}/v1/chat/completions"
    model = plan["argv"][plan["argv"].index("--served-model-name") + 1]
    total = a.ready_timeout_s + a.baseline_s + plan["ready_timeout_s"] + a.load_s + 600
    h = sl.LiveHarness(out / "live", seconds=total)
    sampler = Sampler(h, smi)
    proc = None
    try:
        h.start(sl.encode_stream(250))
        if not h.wait_ready(a.ready_timeout_s):
            s["fail_reasons"] = ["Tier 0 LIVE 하네스가 준비되지 않음(카메라 4대 ready)"]
            return EXIT_FAIL
        s["cameras"] = len(h.cams)
        sampler.start()
        log(f"A: Tier 0 단독 {a.baseline_s:.0f}s")
        time.sleep(a.baseline_s)
        sampler.phase = "B"
        log("B: vLLM 기동")
        t0 = time.monotonic()
        proc = bs.Proc("vllm", plan["argv"], plan["env"], out / "logs")
        proc.renice(plan["nice"])
        proc.pin(set(plan["cpus"]) if plan["cpus"] else None)
        sampler.vllm_pgid = proc.pid
        while time.monotonic() - t0 < plan["ready_timeout_s"] and proc.poll() is None:
            if bs.http_ok(f"http://127.0.0.1:{plan['port']}/health"):
                s["vllm_ready_s"] = round(time.monotonic() - t0, 1)
                break
            time.sleep(2)
        s["vllm_affinity"] = cpus_allowed(proc.pid)
        if s.get("vllm_ready_s") is None:
            log(f"vLLM 이 /health 200 이 아님(종료 코드 {proc.poll()}) → 로그 {out / 'logs' / 'vllm.log'}")
        else:
            log(f"vLLM 준비 {s['vllm_ready_s']}s, CPU {s['vllm_affinity'] and len(s['vllm_affinity'])}개")
            s["warmup"] = chat_once(url, model, images[0], a.max_tokens)
            s["requests"]["concurrency1"], _ = burst(url, model, images, a.n1, 1, a.max_tokens)
            s["requests"]["concurrency4"], _ = burst(url, model, images, a.n4, 4, a.max_tokens)
            log(f"동시 1 {s['requests']['concurrency1']}, 동시 4 {s['requests']['concurrency4']}")
            sampler.phase = "C"
            log(f"C: 지속 동시 4 {a.load_s:.0f}s")
            s["requests"]["sustained4"], rows = burst(url, model, images, 0, 4, a.max_tokens, until=time.monotonic() + a.load_s)
            (out / "responses.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            log(f"지속 동시 4 {s['requests']['sustained4']}")
        sampler.phase = "D"
        if proc.poll() is not None:
            raise _Stop(f"vLLM 이 이미 끝났다(종료 코드 {proc.poll()}): D 단계를 하지 않는다")
        log("D: vLLM 리더 kill -9")
        s["kill9"] = kill9_check(proc, smi)
        time.sleep(5)
    except _Stop as e:
        log(str(e))
    finally:
        sampler.stop.set()
        if sampler.is_alive():
            sampler.join(5)
        if proc is not None and proc.poll() is None:
            proc.signal(bs.KILL)
        h.close()
        lags = h.lag_ms()
        s["phases"] = {ph: phase_summary(sampler.rows, lags, ph) for ph in ("A", "B", "C", "D")}
        s.setdefault("fail_reasons", judge(s))
        s["finished_at"] = time.time_ns() // 1_000_000
        (out / "samples.json").write_text(json.dumps(sampler.rows, ensure_ascii=False) + "\n", encoding="utf-8")
        (out / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (out / "metrics.json").write_text(json.dumps(flat(s), ensure_ascii=False) + "\n", encoding="utf-8")
    for r in s["fail_reasons"]:
        log(f"FAIL: {r}")
    log(f"v4 → {'PASS' if not s['fail_reasons'] else 'FAIL'} ({out / 'summary.json'})")
    return EXIT_OK if not s["fail_reasons"] else EXIT_FAIL


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--images", default="/srv/simai/datasets/classroom4-prepared-20261026", help="질문 그림 폴더(*.jpg)")
    ap.add_argument("--image-width", type=int, default=1280)
    ap.add_argument("--baseline-s", type=float, default=90)
    ap.add_argument("--load-s", type=float, default=600)
    ap.add_argument("--n1", type=int, default=12)
    ap.add_argument("--n4", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--ready-timeout-s", type=float, default=900, help="Tier 0 하네스 준비(엔진 캐시가 없으면 첫 빌드)")
    ap.add_argument("--out", default=None, help="결과 폴더($SIMAI_RUN_DIR 가 있으면 그것)")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
