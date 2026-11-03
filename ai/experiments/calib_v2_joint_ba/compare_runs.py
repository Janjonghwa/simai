"""Compare fixed-track joint/projected runs using only the standard library.

Usage: python compare_runs.py <reference-run-directory> <candidate-run-directory>
Exit 0 means median agreement only; convergence/time/adoption are separate fields.
"""

import argparse
import json
import math
from pathlib import Path, PureWindowsPath


def compare(reference, candidate, tolerance=0.05):
    rows = []
    if reference["calibration_version"] != candidate["calibration_version"] or reference["anchor"] != candidate["anchor"]:
        raise ValueError("Different calibration or anchor")
    for name, stage in reference["stages"].items():
        if name == "initial":
            continue
        other = candidate["stages"][name]
        for pair, a in stage["holdout"]["pairs"].items():
            b = other["holdout"]["pairs"][pair]
            valid = a["n"] == b["n"] and all(
                isinstance(s["median"], (float, int)) and math.isfinite(s["median"])
                and s.get("invalid", 0) == 0 for s in (a, b))
            difference = abs(a["median"] - b["median"]) if valid else None
            rows.append({"stage": name, "pair": pair, "observations": b["n"],
                         "holdout_insufficient": candidate["input"]["pairs"][pair]["holdout_insufficient"],
                         "reference_median_px": a["median"], "candidate_median_px": b["median"],
                         "absolute_difference_px": difference,
                         "within_tolerance": valid and difference <= tolerance})
    return {"tolerance_px": tolerance, "rows": rows,
            "all_medians_within_tolerance": bool(rows) and all(r["within_tolerance"] for r in rows),
            "candidate_all_solves_converged": candidate.get("all_solves_converged", False),
            "candidate_all_stages_within_10s": all(
                s.get("stage_seconds", s["seconds"]) <= 10.
                for name, s in candidate["stages"].items() if name != "initial"),
            "reference_all_main_stages_converged": all(s["success"] for name, s in reference["stages"].items()
                                                       if name != "initial"),
            "adoption_forbidden": True,
            "note": "Agreement with a nonconverged reference is diagnostic only; independent metric validation is required"}


def read(folder, name):
    return json.loads((folder / name).read_text(encoding="utf-8"))


def calibration_hash(manifest, calibration):
    # v2 config uses '/' while Path writes '\\' into Windows input manifests.
    hashes = [e["sha256"] for e in manifest if PureWindowsPath(e["path"]) == PureWindowsPath(calibration)]
    if len(hashes) != 1:
        raise ValueError("Expected exactly one calibration entry in input manifest")
    return hashes[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    if read(args.reference, "tracks.json")["tracks"] != read(args.candidate, "tracks.json")["tracks"]:
        raise ValueError("Observations/splits/order differ; replay the same saved tracks")
    ignored = {"solver", "projected_point_max_iterations", "projected_point_gradient_tol",
               "projected_point_step_tol", "projected_point_fallback_nfev", "stage_target_seconds"}
    configs = [{k: v for k, v in read(p, "config.json").items() if k not in ignored}
               for p in (args.reference, args.candidate)]
    if configs[0] != configs[1]:
        raise ValueError("Non-solver configuration differs")
    calibration_hashes = []
    for folder, config in zip((args.reference, args.candidate), configs):
        entries = read(folder, "input_manifest.json")
        calibration_hashes.append(calibration_hash(entries, config["calibration"]))
    if calibration_hashes[0] != calibration_hashes[1]:
        raise ValueError("Calibration input hashes differ")
    result = compare(read(args.reference, "measurements.json"), read(args.candidate, "measurements.json"))
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if result["all_medians_within_tolerance"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
