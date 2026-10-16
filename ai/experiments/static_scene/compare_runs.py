"""EXP-01 Run 비교: 기준 Run 대비 카메라 거리 비, 회전 차이, 깊이, 지연·메모리, known pose 사용 여부.

  python compare_runs.py --base C1_room4_bf16 C2_room4_apache_bf16 C3_room4_fp32 ...
결과는 표준 출력과 <runs>/compare_<base>.json 에 남긴다.
"""
import argparse
import json
import os
from pathlib import Path

import numpy as np

RUNS = Path(os.environ.get("SIMAI_ROOT", "/workspace/simai")) / "runs" / "exp01"


def load(run):
    return (json.loads((RUNS / run / "metrics.json").read_text(encoding="utf-8")),
            json.loads((RUNS / run / "cameras.json").read_text(encoding="utf-8")))


def poses(cams):
    return np.array([v["cam2world"] for v in cams["views"]], float)


def pair_dist(T):
    c = T[:, :3, 3]
    d = np.linalg.norm(c[:, None] - c[None], axis=-1)
    return d[np.triu_indices(len(c), 1)]


def rot_deg(A, B):
    return float(np.degrees(np.arccos(np.clip((np.trace(A.T @ B) - 1) / 2, -1, 1))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("runs", nargs="+")
    a = ap.parse_args()
    _, bc = load(a.base)
    bT = poses(bc)
    bd = pair_dist(bT)
    rows = []
    for r in a.runs:
        m, c = load(r)
        T = poses(c)
        ratio = pair_dist(T) / np.maximum(bd, 1e-9)
        row = {
            "run": r, "result": m["result"], "infer_ms_warm": m.get("infer_ms_warm"), "gpu_peak_mb": m.get("gpu_used_peak_mb"),
            "cam_dist_ratio_vs_base": [round(float(ratio.min()), 3), round(float(ratio.max()), 3)],
            "rot_diff_vs_base_max_deg": round(max(rot_deg(x, y) for x, y in zip(bT[:, :3, :3], T[:, :3, :3])), 2),
            "depth_p50_m": [v["depth_m"].get("p50") for v in m["per_view"]],
            "metric_scaling_factor": m["per_view"][0].get("metric_scaling_factor"),
            "fov_x_deg": [v["fov_x_deg"] for v in c["views"]],
        }
        if "pose_input_vs_output" in m:
            row["pose_in_vs_out"] = m["pose_input_vs_output"]
            row["out_over_in_cam_dist"] = m["camera_distance_ratio_out_over_in"]
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    (RUNS / f"compare_{a.base}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
