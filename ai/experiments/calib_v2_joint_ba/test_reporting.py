"""Dependency-free checks for numerical comparison and nonconvergence reporting."""

import copy
import unittest

from compare_runs import calibration_hash, compare
from v2 import report


def measurement():
    stats = {"n": 2, "invalid": 0, "median": 1., "p90": 1.5, "p95": 1.6}
    hold = {"pairs": {"cam_C-cam_D": stats}, "cameras": {}}
    stage = {"success": False, "nfev": 4000, "njev": 3999, "seconds": 168.,
             "variables": 9, "residual_calls": 40000, "observability_seconds": 0.1,
             "stage_seconds": 168.2, "within_time_target": False,
             "adoption_forbidden": True, "adoption_blockers": ["solver_not_converged"],
             "holdout": hold, "pose_change": {}}
    return {"solver": "projected", "calibration_version": "test", "anchor": "cam_D", "anchor_reason": "test",
            "all_solves_converged": False, "adoption_blockers": ["solver_not_converged_or_invalid"],
            "stages": {"initial": {"holdout": hold, "pose_change": {}}, "A_rotation": stage},
            "input": {"pairs": {"cam_C-cam_D": {"train": 15, "holdout": 1, "holdout_insufficient": True}}},
            "sensitivity": [], "translation_unstable": True, "data_jacobian_weak": True, "total_seconds": 169.}


class ComparisonTests(unittest.TestCase):
    def test_manifest_windows_paths_accept_both_slash_forms(self):
        manifest = [{"path": r"C:\inputs\calibration.json", "sha256": "sha256:example"}]
        self.assertEqual(calibration_hash(manifest, "C:/inputs/calibration.json"), "sha256:example")
        with self.assertRaises(ValueError):
            calibration_hash(manifest, "C:/other/calibration.json")

    def test_agreement_does_not_erase_nonconvergence_or_weak_pair(self):
        reference, candidate = measurement(), measurement()
        candidate["stages"]["A_rotation"]["holdout"]["pairs"]["cam_C-cam_D"]["median"] += 0.049
        result = compare(reference, candidate)
        self.assertTrue(result["all_medians_within_tolerance"])
        self.assertTrue(result["rows"][0]["holdout_insufficient"])
        self.assertFalse(result["candidate_all_solves_converged"])
        self.assertFalse(result["candidate_all_stages_within_10s"])
        self.assertTrue(result["adoption_forbidden"])

    def test_disagreement_and_invalid_metrics_fail(self):
        reference = measurement()
        for value in (1.051, None, float("nan")):
            candidate = copy.deepcopy(reference)
            candidate["stages"]["A_rotation"]["holdout"]["pairs"]["cam_C-cam_D"]["median"] = value
            self.assertFalse(compare(reference, candidate)["all_medians_within_tolerance"])

    def test_report_explicitly_blocks_nonconverged_stage(self):
        output = report(measurement())
        self.assertIn("채택 금지: solver_not_converged", output)
        self.assertIn("4000/3999", output)
        self.assertIn("168.200", output)


if __name__ == "__main__":
    unittest.main()
