"""S15P31A101-663: 인원수별 판정 tick 단계 시간과 cProfile 상위 함수 (docs/design/S15P31A101-663-judge-core-optimize.md).

ai/ 에서 실행한다. GPU·모델을 쓰지 않는다(검출·로컬 추적·Re-ID 모델만 결정적 합성 입력, judge_core_scenes.py).

    uv run python tests/services/tier0/bench_judge_core.py                         # 1·3·5·7·9명, 표 (Markdown)
    uv run python tests/services/tier0/bench_judge_core.py --people 7 --ticks 120 --json out.json
    uv run python tests/services/tier0/bench_judge_core.py --people 7 --cprofile 25  # 상위 25개 함수(누적·자체)

이전 코드와 비교하려면 이전 커밋의 ai/src·ai/services 를 꺼내 PYTHONPATH 앞에 두고 같은 스크립트를 돌린다.

    git archive <커밋> ai/src ai/services | tar -x -C <임시>
    PYTHONPATH=<임시>/ai/src:<임시>/ai uv run python tests/services/tier0/bench_judge_core.py

단계 이름은 Tier 0 StageTiming 과 같다: judge_tick(판정 lock 안 전체), perception(인지), cross_view_2d_triangulation
(Cross-view, 그 안에 cross_view_pairs·cross_view_clustering·cross_view_validate_triangulate), world_track, contact,
judgment_and_output(ID 교정·Risk 판정·LifecycleTracker), risk_evaluate. SIM 합성 입력의 처리 시간이다. 정확도 지표가 아니다.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import platform
import pstats
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import judge_core_scenes as jc

STAGES = ("judge_tick", "perception", "cross_view_2d_triangulation", "cross_view_pairs", "cross_view_clustering",
          "cross_view_validate_triangulate", "world_track", "contact", "judgment_and_output", "risk_evaluate")


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": float("nan"), "p95": float("nan"), "mean": float("nan")}
    arr = np.asarray(values)
    return {"p50": float(np.percentile(arr, 50)), "p95": float(np.percentile(arr, 95)), "mean": float(arr.mean())}


def run(people: int, ticks: int, warmup: int, top: int) -> dict:
    scene = jc.crowd(people, ticks + warmup)
    profile = cProfile.Profile() if top else None
    with tempfile.TemporaryDirectory() as tmp:
        out = jc.run_scene(scene, Path(tmp), timing=True, warmup=warmup, profile=profile)
    timing = out["timing"]
    stages = {"judge_tick": _stats(timing["judge_tick_ms"])}
    for stage in STAGES[1:]:
        samples = timing["stages"].get(stage, [])
        stages[stage] = _stats(samples[-ticks:])
    row = {"people": people, "ticks": ticks, "warmup": warmup, "tracks_seen": max(timing["people"], default=0),
           "stages": stages}
    if profile is not None:
        st = pstats.Stats(profile)
        rows = []
        for (filename, line, func), (_prim, calls, own, cum, _callers) in st.stats.items():
            rows.append({"function": f"{Path(filename).name}:{line}:{func}", "calls": calls,
                         "self_ms_per_tick": own * 1000 / ticks, "cum_ms_per_tick": cum * 1000 / ticks})
        row["cprofile"] = {
            "cumulative": sorted(rows, key=lambda r: r["cum_ms_per_tick"], reverse=True)[:top],
            "self": sorted(rows, key=lambda r: r["self_ms_per_tick"], reverse=True)[:top],
        }
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--people", type=int, nargs="+", default=[1, 3, 5, 7, 9])
    parser.add_argument("--ticks", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--cprofile", type=int, default=0, metavar="N", help="cProfile 상위 N 개 함수(측정 시간은 부풀려진다)")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    rows = [run(n, args.ticks, args.warmup, args.cprofile) for n in args.people]
    print("| 사람 | 단계 | p50 ms | p95 ms | 평균 ms |")
    print("|---:|---|---:|---:|---:|")
    for row in rows:
        for stage in STAGES:
            s = row["stages"][stage]
            print(f"| {row['people']} | {stage} | {s['p50']:.1f} | {s['p95']:.1f} | {s['mean']:.1f} |")
    for row in rows:
        if "cprofile" in row:
            for kind in ("cumulative", "self"):
                print(f"\n{row['people']}명 cProfile ({kind}, tick 당 ms)")
                for r in row["cprofile"][kind]:
                    print(f"  {r['cum_ms_per_tick'] if kind == 'cumulative' else r['self_ms_per_tick']:8.2f}"
                          f"  {r['calls']:8d}  {r['function']}")
    if args.json:
        env = {"python": platform.python_version(), "platform": platform.platform(), "cpu": platform.processor(),
               "logical_cpu": os.cpu_count(), "numpy": np.__version__}
        args.json.write_text(json.dumps({"env": env, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
