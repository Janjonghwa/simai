"""377 측정 JSON/py-spy 원시 스택을 영구 조사 자료와 스키마 검증된 Run 기록으로 묶는다.

원시 프로파일은 tmp/377에 두고, 작은 수치·해시·Run 기록만 docs/design에 보관한다.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import statistics
import subprocess

import numpy as np
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
TMP = Path("C:/Users/SSAFY/simai-local/codex-tmp/377")
SCRIPT = "ai/experiments/profile_perception.py"


def sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def spy_summary(path):
    total, step = 0, 0
    inclusive, leaf, threads = Counter(), Counter(), Counter()
    functions = Counter()
    native = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stack, count = line.rsplit(" ", 1)
        count = int(count)
        frames = stack.split(";")
        total += count
        if any(".dll)" in f or ".pyd)" in f for f in frames):
            native += count
        python_frames = [f for f in frames if ".py:" in f]
        for kind in ("decode", "jpeg", "benchmark"):
            if any(f.startswith(kind + " (") for f in python_frames):
                threads[kind] += count
        start = next((i for i, f in enumerate(python_frames) if f.startswith("step (") and "perception.py:" in f), None)
        if start is not None:
            step += count
            for frame in set(python_frames[start:]):
                inclusive[frame] += count
            for function in {re.sub(r":\d+\)$", ")", f) for f in python_frames[start:]}:
                functions[function] += count
            leaf[python_frames[-1]] += count
    return {"path": str(path), "sha256": sha256(path), "total_gil_samples": total,
            "step_gil_samples": step, "native_stack_samples": native, "thread_samples": dict(threads),
            "step_inclusive_top": inclusive.most_common(25), "step_python_leaf_top": leaf.most_common(25),
            "step_function_inclusive_top": functions.most_common(25)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=TMP)
    parser.add_argument("--output", type=Path, default=ROOT / "ai/experiments/runs/perception-profile-377/data.json")
    args = parser.parse_args()
    if not args.source.resolve().is_relative_to(TMP.resolve()):
        parser.error("source는 지정된 tmp/377 내부여야 한다")
    allowed = ROOT / "ai/experiments/runs/perception-profile-377/data.json"
    if args.output.resolve() != allowed.resolve():
        parser.error(f"영구 결과 경로는 {allowed}만 허용한다")
    registry = Registry()
    run_schema = None
    for path in (ROOT / "contracts").rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        if path.name == "run_record.schema.json":
            run_schema = schema
    validator = Draft202012Validator(run_schema, registry=registry)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip()
    runs = []
    # 예비 smoke/부착 탐색도 Run 기록은 보존하되 최종 시간 집계에서 제외한다.
    names = re.compile(r"(?:r[12]-(?:base|media|decode|jpeg)-[1-6]|cp-[1-6]|spy-(?:base|media)-[1-6]|spy-initial-base-[1-4]|switch-[12]|smoke5|smoke-media5|spy-probe5)\.json$")
    for path in sorted(args.source.glob("*.json")):
        if not names.fullmatch(path.name):
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        elapsed = result["media_duration_s"]
        record = {
            "schema_version": "2.2.0", "run_id": "profile377-" + path.stem,
            "experiment_id": "VAL-01", "jira_key": "S15P31A101-377", "owner": "S15P31A101-377 Owner",
            "git_commit": commit, "branch": branch, "dirty_worktree": True,
            "env_hash": "unavailable: local Windows venv; no Docker image",
            "model_version": "none: synthetic COCO17 observations; no detector/GPU",
            "config": {"path": SCRIPT, "sha256": "sha256:" + result["script_sha256"],
                       "overrides": {**{k: result[k] for k in ("people", "mode", "ticks", "warmup", "seed", "noise_px", "missing", "pace", "jpeg_target_fps")},
                                     "switch_interval_ms": result.get("switch_interval_ms", 5.),
                                     "spy_rate": result.get("spy_rate", 200 if path.stem.startswith("spy-initial") else None)}},
            "input_provenance": result["input_provenance"],
            "gpu": {"provider": "LOCAL", "model": "NOT_USED", "driver": "NOT_QUERIED", "cuda": "NOT_USED", "shared_with_tier0": False},
            "started_at": result["started_at"], "finished_at": result["started_at"] + round(elapsed * 1000),
            "status": "SUCCEEDED", "experiment_state": "BENCHMARK",
            "metrics": {"median_ms": result["median_ms"], "p95_ms": result["p95_ms"], "people": result["people"],
                        "tracks_min": result["tracks_min"], "tracks_max": result["tracks_max"],
                        "decoded_fps_per_camera_min": min(result["decoded"]) / elapsed,
                        "jpeg_fps": result["jpeg_encoded"] / elapsed},
            "performance": {"stage_latency_ms": {"perception_step": {"p50": result["median_ms"], "p95": result["p95_ms"]}}},
            "results": [{"kind": "REPORT", "uri": "repo:docs/design/tier0-perception-profile.md"},
                        {"kind": "LOG", "uri": path.as_uri(), "sha256": sha256(path)}],
            "cost": {"gpu_hours": 0, "notes": "로컬 CPU 조사. SIM 입력, 제품 성능/채택 게이트/독립 리뷰 근거로 사용 불가."},
            "review": None, "notes": "리뷰 생략(사용자 지시). thread CPU 시계는 Windows 양자화에 유의. 원시 프로파일은 지정 임시 경로에만 보관.",
        }
        spy_path = path.with_suffix(".raw")
        if path.stem == "spy-probe5":
            spy_path = args.source / "native-attached.txt"
        spy = spy_summary(spy_path) if spy_path.exists() else None
        if spy:
            record["results"].append({"kind": "LOG", "uri": spy_path.as_uri(), "sha256": spy["sha256"]})
        profile_path = path.with_suffix(".pstats")
        if profile_path.exists():
            record["results"].append({"kind": "LOG", "uri": profile_path.as_uri(), "sha256": sha256(profile_path)})
        validator.validate(record)
        runs.append({"name": path.stem, "record": record, "measurement": result, "py_spy": spy})
    pooled = []
    for people in range(1, 7):
        for mode in ("base", "media", "decode", "jpeg"):
            selected = [r["measurement"] for r in runs if re.fullmatch(rf"r[12]-{mode}-{people}", r["name"])]
            if not selected:
                continue
            values = [t for r in selected for t in r["step_ms"]]
            pooled.append({"people": people, "mode": mode, "samples": len(values),
                           "median_ms": statistics.median(values), "p95_ms": float(np.percentile(values, 95)),
                           "run_medians_ms": [r["median_ms"] for r in selected],
                           "over_100ms": sum(t > 100 for t in values), "over_200ms": sum(t > 200 for t in values)})
    manifest = Path("C:/Users/SSAFY/Desktop/simai/S15P31A101/ai/experiments/recordings/rec-20261029-015512z-lobby-railing-pose/manifest.json")
    doc = {"purpose": "S15P31A101-377 CPU 합성 입력 조사; 제품 성능 아님", "run_record_schema": "contracts/experiments/run_record.schema.json",
           "current_harness_sha256": sha256(ROOT / SCRIPT),
           "excluded_from_latency_pool": "smoke/probe, cProfile, 모든 py-spy 실행, switch 대조군. 200Hz initial 스택은 비용 순위에서도 제외.",
           "recording_manifest": {"path": str(manifest), "sha256": sha256(manifest)},
           "pooled": pooled, "runs": runs}
    args.output.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"validated_runs": len(runs), "pooled": pooled}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
