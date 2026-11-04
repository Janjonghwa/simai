"""한 변수씩 바꾼 추적 재생 실험을 병렬로 돌리고 지표 표를 만든다 (S15P31A101-413).

    python experiments/track_replay/run_experiments.py --cache <캐시> --calibration <보정> --plan <실험 JSON> --out <결과 폴더>

실험 JSON: {"base": {...replay_tracking 설정...}, "experiments": {"이름": {"cross_view": {...}}, ...}}.
실험 설정의 "calibration_file" 은 그 실험만 쓸 보정 문서 경로다(보정 교란 실험).
각 실험 설정은 base 위에 한 단계 깊이로 덮는다. 결과: <out>/<이름>/metrics.json, <out>/summary.md·summary.json.
검출은 하지 않는다(CPU). 프로세스마다 캐시를 한 번 읽는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from replay_tracking import load_cache, run  # 같은 폴더(스크립트 실행 시 sys.path[0])

_CACHE: dict[str, Any] = {}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _job(args: tuple[str, str, str, dict[str, Any], str]) -> tuple[str, dict[str, Any]]:
    name, cache_dir, calibration, cfg, out = args
    if cache_dir not in _CACHE:
        _CACHE[cache_dir] = load_cache(Path(cache_dir))
    calibration = cfg.pop("calibration_file", calibration)  # 보정 교란 실험(예: 카메라 회전)만 다른 보정 문서를 쓴다
    m = run(_CACHE[cache_dir], Path(calibration), cfg, Path(out) / name)
    return name, m


def row(name: str, m: dict[str, Any], segments: list[str]) -> dict[str, Any]:
    d = m["diagnostics"]
    w = d["world"]
    cams = m["updated_cameras_ratio"]
    re_m = m.get("reentry") or {}
    r = {
        "name": name,
        "ids": m["ids_total"],
        "ids/min": m["ids_per_min"],
        "confirmed": m["ids_confirmed"],
        "new_track": w["manager"].get("new_track.created", 0),
        "lost T/C": f"{w['lost'].get('TENTATIVE', 0)}/{w['lost'].get('CONFIRMED', 0)}",
        "merge": w["revisions"].get("MERGE", 0),
        "life p50 s": m["lifespan_s"]["p50"],
        "<6s": m["lifespan_s"]["under_6s_ratio"],
        "cam 2/3/4": "/".join(str(cams.get(k, 0)) for k in ("2", "3", "4")),
        "cam mean": m["updated_cameras_mean"],
        "bad bone": m["bones"]["tracks_with_bad_bone_ratio"],
        "split suppressed": w["manager"].get("new_track.split_remainder_suppressed", 0),
        "zone dup": f"{m['zone_proxy']['duplicates']} ({m['zone_proxy']['id_entries']}/{m['zone_proxy']['occupancy_episodes']})",
        "reentry": (
            None
            if re_m.get("continuation_rate") is None
            else f"{re_m['continuation_rate']} ({re_m.get('resumed_after_lost', 0)}/{re_m.get('resumed_after_lost', 0) + re_m.get('unresumed_confirmed_losses', 0)})"
        ),
        "ms/tick": (m.get("run") or {}).get("ms_per_tick"),
    }
    for kind, review in m.get("association_review", {}).items():
        r[f"{kind} false/reviewed/candidates"] = (
            f"{review['false_connections']}/{review['reviewed']}/{review['candidates']}"
        )
    for s in segments:
        seg = m["segments"].get(s) or {}
        r[f"{s} new/p·min"] = seg.get("new_ids_per_person_min")
        r[f"{s} =n/>n/<n"] = "/".join(str(seg.get(k)) for k in ("count_exact", "count_over", "count_under"))
        r[f"{s} swap"] = seg.get("id_changes")
    return r


def markdown(rows: list[dict[str, Any]]) -> str:
    keys = list(rows[0])
    lines = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
    for r in rows:
        lines.append("| " + " | ".join("" if r[k] is None else str(r[k]) for k in keys) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", required=True)
    ap.add_argument("--calibration", required=True)
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--dataset", help="계획 datasets 의 입력·구간 설정 선택. 없으면 기존 base 사용")
    args = ap.parse_args(argv)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    base = plan.get("base") or {}
    if args.dataset:
        base = _merge(base, plan["datasets"][args.dataset]["config"])
    exps = plan["experiments"]
    names = [n for n in exps if not args.only or n in args.only]
    jobs = [(n, args.cache, args.calibration, _merge(base, exps[n]), str(args.out)) for n in names]
    args.out.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for name, m in pool.map(_job, jobs):
            results[name] = m
            print(f"끝: {name} ids={m['ids_total']} ({m['run']['elapsed_s']} s)", flush=True)
    segments = list((base.get("segments_s") or {}).keys())
    rows = [row(n, results[n], segments) for n in names]
    (args.out / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out / "summary.md").write_text(markdown(rows), encoding="utf-8")
    print(markdown(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
