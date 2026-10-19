#!/usr/bin/env python3
"""Tier 0 GPU 우선 보호 Pod 측정 도구 (S15P31A101-60, 명세 3.2, 6.6, 15.1 G0 'VLM·분석 워커 동시 실행', 부록 D D-G0-08).

두 하위 명령:

  load   더미 분석 부하. torch 로 GPU 메모리를 잡고 행렬곱을 돌린다(분석 워커 흉내). 분석 워커와 같은 상한
         (--fraction, torch.cuda.set_per_process_memory_fraction)을 둔다. 강제 종료(kill -9) 시험의 대상이다.
  watch  Tier 0 표시 파일(state/tier0.live)의 gpu_priority 칸을 --seconds 동안 읽어 지연 표본을 모으고 판정한다.
         합격: 지연 표본 p95 ≤ 예산(기본 600ms), 모르는 표본 0, 표시 파일 오래됨·없음 0, 선확보 OK 유지, 판정 오류 증가 0
         (모든 창에 값이 있어야 함), 판정 tick 이 모든 창 사이에서 증가(멈춤 없음, 빠진 창 없음). 결과 JSON 을 --out(과 $SIMAI_RUN_DIR/metrics.json)에 쓴다. 종료 코드 0 합격, 1 불합격.

GPU 큐(gpu_queue.py)의 Job 으로 돌릴 때는 live_guard 를 끈 측정 전용 큐로 돌린다(Tier 0 를 일부러 띄운 측정 세션이다).
표준 라이브러리만 쓰고, load 만 torch 가 필요하다.

지연 표본은 Tier 0 보정 T_lag(now − t_latest_global)이다. capture_to_risk 와 같은 구간(촬영 → Tier 0 판정)이지만 판정 시각이
아니라 표본 시각 기준이라 프레임 간격만큼 크게 나온다(보수적). 끝에서 끝 capture_to_risk 측정(-183 큐의 지연 항목)과 함께 본다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[max(0, min(len(s) - 1, math.ceil(q / 100 * len(s)) - 1))]


def cmd_load(args: argparse.Namespace) -> int:
    import torch  # noqa: PLC0415 - load 만 torch 가 필요하다

    if not torch.cuda.is_available():
        print("tier0_priority_probe load: CUDA 없음", file=sys.stderr)
        return 2
    if args.fraction:
        torch.cuda.set_per_process_memory_fraction(args.fraction, 0)
    hold = torch.empty(int(args.mem_mb) * 1024 * 1024, dtype=torch.uint8, device="cuda:0")
    a = torch.randn(args.n, args.n, device="cuda:0", dtype=torch.float16)
    b = torch.randn(args.n, args.n, device="cuda:0", dtype=torch.float16)
    end = time.monotonic() + args.seconds
    iters = 0
    print(f"load: 메모리 {args.mem_mb} MB, 행렬 {args.n}, {args.seconds}s, pid {os.getpid()}", flush=True)
    while time.monotonic() < end:
        for _ in range(10):
            a = a @ b
            a = a / a.norm()
        torch.cuda.synchronize()
        iters += 10
        if args.duty < 1.0:
            time.sleep(0.01 * (1 - args.duty) / max(args.duty, 1e-3))
    print(f"load: 끝 ({iters} 회, 잡은 메모리 {hold.numel() // 1024 // 1024} MB)", flush=True)
    return 0


def _read(marker: Path) -> tuple[dict | None, float | None]:
    try:
        st = marker.stat()
        return json.loads(marker.read_text(encoding="utf-8")), st.st_mtime
    except (OSError, ValueError):
        return None, None


def cmd_watch(args: argparse.Namespace) -> int:
    marker = Path(args.marker)
    lags: list[float] = []
    windows = unknown = stale = missing = 0
    reserve_states: dict[str, int] = {}
    max_age = 0.0
    seen: set[int] = set()
    ticks: list[int] = []
    errors: list[int] = []
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        doc, mtime = _read(marker)
        if doc is None:
            missing += 1
        else:
            age = time.time() - (mtime or 0)
            max_age = max(max_age, age)
            if age > args.max_age_s:
                stale += 1
            gp = doc.get("gpu_priority") or {}
            written = gp.get("written_at")
            if isinstance(written, int) and written not in seen:
                seen.add(written)
                windows += 1
                load = gp.get("load") or {}
                lags.extend(float(v) for v in load.get("lag_ms") or [])
                unknown += int(load.get("unknown") or 0)
                state = (gp.get("reserve") or {}).get("state")
                reserve_states[str(state)] = reserve_states.get(str(state), 0) + 1
                judge = (doc.get("status") or {}).get("judge") or {}
                if isinstance(judge.get("ticks"), int):
                    ticks.append(judge["ticks"])
                if isinstance(judge.get("errors"), int):
                    errors.append(judge["errors"])
        time.sleep(args.poll_s)

    p95 = _pct(lags, 95)
    result = {
        "seconds": args.seconds,
        "windows": windows,
        "lag_samples": len(lags),
        "lag_ms_p50": _pct(lags, 50),
        "lag_ms_p95": p95,
        "lag_ms_p99": _pct(lags, 99),
        "lag_ms_max": max(lags) if lags else None,
        "over_budget": sum(1 for v in lags if v > args.budget_ms),
        "budget_ms": args.budget_ms,
        "unknown_samples": unknown,
        "marker_missing_reads": missing,
        "marker_stale_reads": stale,
        "marker_max_age_s": round(max_age, 2),
        "reserve_states": reserve_states,
        "judge_ticks_delta": (ticks[-1] - ticks[0]) if len(ticks) >= 2 else None,
        # 연속한 두 창 사이에 tick 이 늘지 않은 횟수(표시 파일 갱신 5초 사이 판정이 멈춤)
        "judge_stalled_windows": sum(1 for a, b in zip(ticks, ticks[1:]) if b <= a),
        "judge_missing_windows": windows - len(ticks),
        "judge_errors_delta": (errors[-1] - errors[0]) if len(errors) >= 2 else None,
    }
    checks = {
        "p95_within_budget": p95 is not None and p95 <= args.budget_ms,
        "no_unknown": unknown == 0 and len(lags) > 0,
        "marker_fresh": stale == 0 and missing == 0,
        "reserve_ok": set(reserve_states) <= set(args.reserve_ok.split(",")) and bool(reserve_states),
        # 시작·끝 차이만이 아니라 모든 창에서 늘어야 한다. 판정 통계가 빠진 창이 있으면 불합격(모름)
        "judge_progressing": (result["judge_ticks_delta"] or 0) > 0 and result["judge_stalled_windows"] == 0
        and result["judge_missing_windows"] == 0,
        "no_judge_errors": result["judge_errors_delta"] == 0 and len(errors) == windows,
    }
    result["checks"] = checks
    result["passed"] = all(checks.values())
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    run_dir = os.environ.get("SIMAI_RUN_DIR")
    if run_dir:  # gpu_queue Run 기록의 metrics(스칼라만)
        scalars = {k: v for k, v in result.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        scalars["passed"] = 1 if result["passed"] else 0
        Path(run_dir, "metrics.json").write_text(json.dumps(scalars), encoding="utf-8")
    return 0 if result["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    root = Path(os.environ.get("SIMAI_ROOT", "/workspace/simai"))
    parser = argparse.ArgumentParser(prog="tier0_priority_probe", description="Tier 0 GPU 우선 보호 Pod 측정")
    sub = parser.add_subparsers(dest="cmd", required=True)
    load = sub.add_parser("load", help="더미 분석 부하(torch GPU)")
    load.add_argument("--mem-mb", type=int, default=20_000, help="잡을 GPU 메모리 (기본 20000)")
    load.add_argument("--n", type=int, default=8192, help="행렬 크기 (기본 8192)")
    load.add_argument("--seconds", type=float, default=600)
    load.add_argument("--duty", type=float, default=1.0, help="GPU 계산 비율 0~1 (기본 1, 쉬지 않음)")
    load.add_argument("--fraction", type=float, default=0.70, help="프로세스 메모리 상한 비율 (분석 워커 기본 0.70, 0 은 없음)")
    watch = sub.add_parser("watch", help="Tier 0 표시 파일 지연 표본 수집·판정")
    watch.add_argument("--marker", default=str(root / "state" / "tier0.live"))
    watch.add_argument("--seconds", type=float, default=600)
    watch.add_argument("--poll-s", type=float, default=1.0)
    watch.add_argument("--budget-ms", type=float, default=600)
    watch.add_argument("--max-age-s", type=float, default=15)
    watch.add_argument("--reserve-ok", default="OK", help="합격으로 보는 선확보 상태(쉼표). Pod 는 OK")
    watch.add_argument("--out", help="결과 JSON 경로")
    args = parser.parse_args(argv)
    return cmd_load(args) if args.cmd == "load" else cmd_watch(args)


if __name__ == "__main__":
    sys.exit(main())
