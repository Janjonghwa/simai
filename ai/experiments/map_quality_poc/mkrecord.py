"""report.json / report_loo.json 에서 run_record.json 을 만들고 스키마로 검증한다."""
import hashlib
import json
from pathlib import Path

import jsonschema

here = Path(__file__).parent
root = here.parents[2]
W = Path("C:/Users/SSAFY/simai-local/map-quality-poc")
r = json.loads((W / "report.json").read_text(encoding="cp949"))
s = json.loads((W / "report_loo.json").read_text(encoding="cp949"))["summary"]
sha = "sha256:" + hashlib.sha256((here / "run.py").read_bytes()).hexdigest()
m = {}
for k, v in s.items():
    kk = k.split("(")[0].replace("+", "_").replace(" ", "")
    for a in ("mae_covered", "mae_valid", "coverage"):
        m[f"{kk}_{a}"] = v[a]
m.update({"stretched_faces_removed": r["stretched"]["slant_faces"] + r["stretched"]["long_edge_faces"],
          "wall_planes_accepted": len(r["planes"]), "floor_fill_m2": r["floor"]["fill_m2"], "atlas_charts": r["atlas"]["charts"]})
rec = {"schema_version": "2.2.0", "run_id": "exp01-map-quality-poc-20261105-0001", "experiment_id": "EXP-01", "jira_key": "S15P31A101-505",
       "owner": "S15P31A101-505 Owner", "git_commit": "e1e137e7", "branch": "S15P31A101-505-map-quality-poc", "dirty_worktree": True,
       "env_hash": "sha256:" + "0" * 64, "model_version": "openreco-style-bake(xatlas 0.0.11)+floor-wall-fill@poc",
       "config": {"path": "ai/experiments/map_quality_poc/run.py", "sha256": sha},
       "input_provenance": {"frames": {"source": "FILE", "dataset_id": "session12-lobby-clean-bg-cal-lobby-init-f0ab1c6b-jba2-2f6cc097"},
                            "calibration": {"source": "G1_AUTO", "calibration_version": "cal-lobby-init-f0ab1c6b-jba2-2f6cc097"},
                            "is_upper_bound": True},
       "gpu": {"model": "none (CPU only)", "driver": "n/a", "cuda": "n/a", "provider": "LOCAL", "shared_with_tier0": False},
       "started_at": 1793862000000, "finished_at": 1793865000000, "status": "SUCCEEDED", "experiment_state": "BENCHMARK", "metrics": m,
       "performance": {"stage_latency_ms": {"visibility_4cam": {"p50": 27126, "p95": 35000}, "xatlas_4chunks": {"p50": 14830, "p95": 14830},
                                            "bake_4cam": {"p50": 15514, "p95": 15514}}},
       "results": [{"kind": "REPORT", "uri": "file://ai/experiments/map_quality_poc/REPORT.md"}],
       "notes": "texrecon 한 대 빼기는 미실행(바이너리 없음). 기하는 4대 기반이라 한 대 빼기 덮임은 낙관적. 1프레임, 정답 아님. env_hash 는 자리표시값."}
(here / "run_record.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
sch = json.loads((root / "contracts/experiments/run_record.schema.json").read_text(encoding="utf-8"))
print(root)

# 스키마 검증(contracts/scripts/validate_examples.py 와 같은 로더)
import importlib.util
import sys

from jsonschema import Draft202012Validator

spec = importlib.util.spec_from_file_location("ve", root / "contracts/scripts/validate_examples.py")
ve = importlib.util.module_from_spec(spec)
sys.modules["ve"] = ve
spec.loader.exec_module(ve)
registry, by_name = ve.load_schemas()
errs = [e.message for e in Draft202012Validator(by_name["run_record"], registry=registry).iter_errors(rec)]
print("schema errors:", errs or "none")
