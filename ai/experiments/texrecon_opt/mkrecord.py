"""fill_real.json·runs/*/result.json 에서 run_record.json 을 만들고 스키마로 검증한다."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

here = Path(__file__).parent
root = here.parents[2]
W = Path("C:/Users/SSAFY/simai-local/texrecon-opt")
r = json.loads((W / "fill/fill_real.json").read_text(encoding="utf-8"))
d, f = r["default"], r["default"]["fill"]
sha = "sha256:" + hashlib.sha256((here / "fill_real.py").read_bytes()).hexdigest()
m = {"fill_seconds": r["default_seconds"], "check_mae_before": d["check"]["mae_mean_before"], "check_mae_after": d["check"]["mae_mean_after"],
     "stretched_removed_faces": f["stretched"]["removed_faces"], "stretched_removed_area_ratio": f["stretched"]["removed_area_ratio"],
     "wall_planes_accepted": len(f["wall_planes"]["accepted"]), "floor_fill_m2": f["floor"]["fill_m2"],
     "fill_projected_ratio": f["surface_source"]["fill_ratio"]["ESTIMATED_PLANE"], "fill_gray_ratio": f["surface_source"]["fill_ratio"]["UNOBSERVED"],
     "fill_share_of_surface": f["surface_source"]["fill_share_of_surface"], "triangles_delta": d["triangles_delta"]}
for k, v in r["stretched_sweep"].items():
    m[f"stretched_cos{k}_removed_faces"] = v["removed_faces"]
    m[f"stretched_cos{k}_loo_wrong_rate"] = v["loo_wrong_rate"]
for name in ("base", "term_gmi", "faces_75k", "scale_075", "scale_050", "outlier_clamping"):
    x = json.loads((W / f"runs/{name}/result.json").read_text(encoding="utf-8"))
    m[f"texrecon_{name}_loo_mae_covered"] = x["loo_mean"]["mae_covered"]
    m[f"texrecon_{name}_texrecon_total_s"] = round(x["times"]["data_costs_s"] + x["times"]["bake_total_s"], 1)
rec = {"schema_version": "2.2.0", "run_id": "exp01-texrecon-opt-fill-20261105-0001", "experiment_id": "EXP-01", "jira_key": "S15P31A101-511",
       "owner": "S15P31A101-511 Owner", "git_commit": "8fe75c27", "branch": "S15P31A101-511-texrecon-optimize", "dirty_worktree": True,
       "env_hash": "sha256:" + "0" * 64, "model_version": "glb_fill@1.1.0(stretched-removal+floor-wall-fill), texrecon mvs-texturing f3374298 (CPU)",
       "config": {"path": "ai/experiments/texrecon_opt/fill_real.py", "sha256": sha},
       "input_provenance": {"frames": {"source": "FILE", "dataset_id": "session12-lobby-clean-bg-cal-lobby-init-f0ab1c6b-jba2-2f6cc097"},
                            "calibration": {"source": "G1_AUTO", "calibration_version": "cal-lobby-init-f0ab1c6b-jba2-2f6cc097"}, "is_upper_bound": True},
       "gpu": {"model": "none (CPU only)", "driver": "n/a", "cuda": "n/a", "provider": "LOCAL", "shared_with_tier0": False},
       "started_at": 1793865000000, "finished_at": 1793868600000, "status": "SUCCEEDED", "experiment_state": "BENCHMARK", "metrics": m,
       "performance": {"stage_latency_ms": {"glb_fill_enhance_4cam": {"p50": int(r["default_seconds"] * 1000), "p95": int(r["default_seconds"] * 1000)}}},
       "results": [{"kind": "REPORT", "uri": "file://ai/experiments/texrecon_opt/REPORT.md"}],
       "notes": "1프레임, 정답 아님. texrecon 설정 비교는 범위 변경으로 중단(면 수·해상도·데이터 항·이상치 일부만). 기하는 4대 기반이라 한 대 빼기 덮임은 낙관적. env_hash 는 자리표시값."}
(here / "run_record.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
spec = importlib.util.spec_from_file_location("ve", root / "contracts/scripts/validate_examples.py")
ve = importlib.util.module_from_spec(spec)
sys.modules["ve"] = ve
spec.loader.exec_module(ve)
registry, by_name = ve.load_schemas()
errs = [e.message for e in Draft202012Validator(by_name["run_record"], registry=registry).iter_errors(rec)]
print("schema errors:", errs or "none")
