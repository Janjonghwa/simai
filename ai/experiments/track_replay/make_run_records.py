"""추적 재생 실험 결과(metrics.json)마다 Run 기록(contracts/experiments/run_record.schema.json)을 남긴다 (S15P31A101-413).

    python experiments/track_replay/make_run_records.py --plan experiments/track_replay/plan_413.json \
        --cache <캐시> --weights <yolo26x-pose.pt> <결과 폴더>/<실험 이름> ...

experiment_id 는 VAL-01(운영 경로 검증 Run, 채택 게이트 근거 아님)이다. 입력은 dev_test 녹화(FILE)라 is_upper_bound=false 이지만
녹화 재생은 실시간 도착 흔들림이 없으므로 실시간 성능으로 쓰지 않는다(설계 문서 잔여 위험). 기록은 결과 폴더(저장소 밖)에 둔다.
jsonschema 가 있으면 계약으로 검사한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
SCHEMA_VERSION = "2.2.0"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False).stdout.strip()


def _gpu() -> dict[str, Any]:
    out = {"model": "", "driver": "", "cuda": "", "provider": "LOCAL", "shared_with_tier0": False}
    try:
        row = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, check=False, timeout=30).stdout.strip().splitlines()
        if row:
            out["model"], out["driver"] = [v.strip() for v in row[0].split(",")][:2]
        head = subprocess.run(["nvidia-smi"], capture_output=True, text=True, check=False, timeout=30).stdout
        if "CUDA Version:" in head:
            out["cuda"] = head.split("CUDA Version:")[1].split()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _flat(m: dict[str, Any]) -> dict[str, Any]:
    d = m["diagnostics"]["world"]
    out: dict[str, Any] = {
        "ids_total": m["ids_total"],
        "ids_per_min": m["ids_per_min"],
        "ids_confirmed": m["ids_confirmed"],
        "new_track_created": d["manager"].get("new_track.created", 0),
        "lost_tentative": d["lost"].get("TENTATIVE", 0),
        "lost_confirmed": d["lost"].get("CONFIRMED", 0),
        "merges": d["revisions"].get("MERGE", 0),
        "lifespan_p50_s": m["lifespan_s"]["p50"],
        "lifespan_under_6s_ratio": m["lifespan_s"]["under_6s_ratio"],
        "updated_cameras_mean": m["updated_cameras_mean"],
        "zone_proxy_duplicates": m["zone_proxy"]["duplicates"],
        "tracks_with_bad_bone_ratio": m["bones"]["tracks_with_bad_bone_ratio"],
    }
    for k, v in m["updated_cameras_ratio"].items():
        out[f"updated_cameras_{k}_ratio"] = v
    for name, seg in m["segments"].items():
        for key in ("new_ids", "new_ids_per_person_min", "count_exact", "count_over", "count_under", "id_changes"):
            if key in seg:
                out[f"{name}_{key}"] = seg[key]
    out["split_remainder_suppressed"] = d["manager"].get("new_track.split_remainder_suppressed", 0)
    for kind, review in m.get("association_review", {}).items():
        for key, value in review.items():
            out[f"{kind}_{key}"] = value
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--weights", required=True, type=Path)
    ap.add_argument("--owner", default="Jonghwa Park")
    ap.add_argument("--jira-key", default="S15P31A101-413")
    ap.add_argument("results", nargs="+", type=Path)
    args = ap.parse_args(argv)
    meta = json.loads((args.cache / "meta.json").read_text(encoding="utf-8"))
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    commit, branch = _git("rev-parse", "HEAD"), _git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--", "ai"))
    try:
        freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=False).stdout
    except OSError:
        freeze = ""
    if not freeze:
        freeze = subprocess.run(["uv", "pip", "freeze", "--python", sys.executable], capture_output=True, text=True,
                                check=False).stdout
    env_hash = "sha256:" + hashlib.sha256(freeze.encode()).hexdigest()
    weights_hash = _sha(args.weights)
    gpu = _gpu()
    validator = None
    try:
        import jsonschema
        from referencing import Registry, Resource

        contracts = REPO / "contracts"
        registry = Registry()
        target = None
        for p in contracts.rglob("*.schema.json"):
            schema = json.loads(p.read_text(encoding="utf-8"))
            registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
            if p.name == "run_record.schema.json":
                target = schema
        validator = jsonschema.Draft202012Validator(target, registry=registry)
    except Exception:  # noqa: BLE001 - 검사 도구가 없으면 기록만 남긴다
        validator = None
    plan_rel = args.plan.resolve().relative_to(REPO).as_posix()
    failed = False
    for res in args.results:
        m = json.loads((res / "metrics.json").read_text(encoding="utf-8"))
        name = res.name
        finished = int((res / "metrics.json").stat().st_mtime * 1000)
        cfg = m["run"]["config"]
        rec = {
            "schema_version": SCHEMA_VERSION,
            "run_id": f"track{args.jira_key.rsplit('-', 1)[-1]}-{name}-{commit[:8]}-{finished}",
            "experiment_id": "VAL-01",
            "jira_key": args.jira_key,
            "owner": args.owner,
            "git_commit": commit,
            "branch": branch,
            "dirty_worktree": dirty,
            "env_hash": env_hash,
            "model_version": f"{meta['weights']} imgsz{meta['imgsz']} {'fp16' if meta['half'] else 'fp32'} stride{meta.get('stride', 1)}"
                             " (2D 캐시) + TrackTrack + Tier 0 Perception",
            "weights_hash": weights_hash,
            "config": {"path": plan_rel, "sha256": _sha(args.plan), "overrides": {"experiment": name, **cfg}},
            "input_provenance": {
                "frames": {"source": "FILE", "dataset_id": f"recording-dev_test/{meta['session_id']}",
                           "camera_ids": list(meta["cameras"])},
                "calibration": {"source": "G1_AUTO", "calibration_version": m["run"]["calibration_version"]},
                "is_upper_bound": False,
            },
            "gpu": gpu,
            "started_at": finished - int(m["run"]["elapsed_s"] * 1000),
            "finished_at": finished,
            "status": "SUCCEEDED",
            "experiment_state": "BASELINE" if name.startswith("E0") else "ITERATE",
            "metrics": _flat(m),
            "results": [{"kind": "REPORT", "uri": f"local://laptop/{res.resolve().as_posix()}/metrics.json",
                         "sha256": _sha(res / "metrics.json")}],
            "notes": "CPU 재생(검출은 GPU 로 한 번 캐시). " + str(plan.get("_note", ""))[:400],
        }
        errors = []
        if validator is not None:
            errors = [e.message for e in validator.iter_errors(rec)]
        failed |= validator is None or bool(errors)
        (res / "run_record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{name}: run_record.json ({'검사 생략' if validator is None else ('통과' if not errors else errors[:3])})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
