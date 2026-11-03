"""Standard-library tests also runnable before uv sync succeeds."""

import subprocess
import sys
import unittest

from simai_camera_helper.judge import (
    PAIRS,
    CoverageJudge,
    PairMetrics,
    Params,
    TwoRoundConfirmation,
    connectivity,
    direction,
    guides,
    pair_grade,
)


def measured(edges=(), count=30):
    return [PairMetrics(a, b, 100, count if (a, b) in edges else 0,
                        (0.8, 0.5) if (a, b) in edges else None,
                        (0.2, 0.5) if (a, b) in edges else None) for a, b in PAIRS]


CHAIN = (("cam_A", "cam_B"), ("cam_B", "cam_C"), ("cam_C", "cam_D"))


class GradeTests(unittest.TestCase):
    def test_inlier_boundaries(self):
        for n, expected in ((0, "LACK"), (14, "LACK"), (15, "WEAK"), (29, "WEAK"), (30, "ENOUGH")):
            with self.subTest(n=n):
                self.assertEqual(pair_grade(PairMetrics("cam_A", "cam_B", 100, n)), expected)

    def test_ratio_exactly_point_one_and_below(self):
        for n, raw, expected in ((15, 150, "WEAK"), (15, 151, "LACK"), (30, 300, "ENOUGH"),
                                 (30, 301, "LACK"), (29, 290, "WEAK"), (29, 291, "LACK")):
            with self.subTest(n=n, raw=raw):
                self.assertEqual(pair_grade(PairMetrics("cam_A", "cam_B", raw, n)), expected)

    def test_empty_and_unknown_differ(self):
        self.assertEqual(pair_grade(PairMetrics("cam_A", "cam_B", 0, 0)), "LACK")
        self.assertEqual(PairMetrics("cam_A", "cam_B", 0, 0).inlier_ratio, 0)
        unknown = PairMetrics("cam_A", "cam_B")
        self.assertEqual(pair_grade(unknown), "UNKNOWN")
        self.assertIsNone(unknown.inlier_ratio)

    def test_invalid_counts(self):
        for raw, count in ((0, 1), (10, -1), (None, 0), (10, None)):
            with self.subTest(raw=raw, count=count), self.assertRaises(ValueError):
                PairMetrics("cam_A", "cam_B", raw, count)

    def test_centroid_constraints(self):
        for count, centroid in ((4, (0.5, 0.5)), (5, (float("nan"), 0.5)), (5, (1.01, 0.5))):
            with self.subTest(count=count, centroid=centroid), self.assertRaises(ValueError):
                PairMetrics("cam_A", "cam_B", 10, count, centroid)

    def test_params_validate(self):
        for kwargs in ({"image_width_px": 0}, {"max_keypoints": 0}, {"ransac_threshold_px": float("nan")},
                       {"min_inlier_ratio": 0}, {"weak_min_inliers": 30}, {"intrinsics_source": "PAST_POSE"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Params(**kwargs)

    def test_no_torch_import(self):
        code = "import simai_camera_helper.judge, simai_camera_helper.message; import sys; " \
               "assert not any(m in sys.modules for m in ('torch', 'numpy', 'cv2', 'lightglue'))"
        subprocess.run([sys.executable, "-c", code], check=True)


class ConnectivityTests(unittest.TestCase):
    def grades(self, edges, grade="ENOUGH"):
        return {p: grade if p in edges else "LACK" for p in PAIRS}

    def test_chain(self):
        self.assertEqual(connectivity(self.grades(CHAIN)),
                         ("CONNECTED", [["cam_A", "cam_B", "cam_C", "cam_D"]]))

    def test_weak_edges_connect(self):
        self.assertEqual(connectivity(self.grades(CHAIN, "WEAK"))[0], "CONNECTED")

    def test_isolated(self):
        state, groups = connectivity(self.grades(CHAIN[:2]))
        self.assertEqual(state, "DISCONNECTED")
        self.assertEqual(groups, [["cam_A", "cam_B", "cam_C"], ["cam_D"]])

    def test_two_components(self):
        state, groups = connectivity(self.grades((CHAIN[0], CHAIN[2])))
        self.assertEqual(state, "DISCONNECTED")
        self.assertEqual(groups, [["cam_A", "cam_B"], ["cam_C", "cam_D"]])

    def test_unknown_may_bridge(self):
        grades = self.grades(CHAIN[:2])
        for p in PAIRS:
            if "cam_D" in p:
                grades[p] = "UNKNOWN"
        self.assertEqual(connectivity(grades)[0], "UNKNOWN")

    def test_unknown_unnecessary(self):
        grades = self.grades(CHAIN)
        grades[("cam_A", "cam_D")] = "UNKNOWN"
        self.assertEqual(connectivity(grades)[0], "CONNECTED")

    def test_unknown_cannot_bridge_all_components(self):
        grades = self.grades(())
        grades[CHAIN[0]] = "UNKNOWN"
        self.assertEqual(connectivity(grades)[0], "DISCONNECTED")

    def test_all_unknown(self):
        self.assertEqual(connectivity(dict.fromkeys(PAIRS, "UNKNOWN"))[0], "UNKNOWN")

    def test_missing_pair_rejected(self):
        with self.assertRaises(ValueError):
            connectivity({})


class DirectionTests(unittest.TestCase):
    def test_directions_and_horizontal_priority(self):
        for point, expected in (((0.7, 0.1), "RIGHT"), ((0.3, 0.9), "LEFT"),
                                ((0.5, 0.7), "DOWN"), ((0.5, 0.3), "UP"), ((0.5, 0.5), "NONE"),
                                ((0.65, 0.68), "NONE"), ((0.35, 0.32), "NONE"), (None, "NONE")):
            with self.subTest(point=point):
                self.assertEqual(direction(point), expected)

    def test_weak_and_lack_magnitudes(self):
        for count, expected in ((5, "MORE"), (14, "MORE"), (15, "SMALL"), (29, "SMALL")):
            pair = PairMetrics("cam_C", "cam_D", 100, count, (0.8, 0.5), (0.2, 0.5))
            result = guides([pair], {(pair.a, pair.b): pair_grade(pair)}, "DISCONNECTED")
            self.assertEqual([(g["direction"], g["magnitude"]) for g in result],
                             [("RIGHT", expected), ("LEFT", expected)])
            self.assertTrue(all(g["reason_code"] == "WEAK_PAIR_EDGE_CLUSTER" for g in result))

    def test_no_overlap(self):
        pair = PairMetrics("cam_A", "cam_C", 10, 4)
        result = guides([pair], {(pair.a, pair.b): "LACK"}, "DISCONNECTED")
        self.assertEqual(result[0]["direction"], "NONE")
        self.assertEqual(result[0]["reason_code"], "NO_OVERLAP_VISIBLE")

    def test_connected_is_optional(self):
        pair = PairMetrics("cam_C", "cam_D", 100, 15, (0.8, 0.5), (0.5, 0.5))
        self.assertEqual(guides([pair], {(pair.a, pair.b): "WEAK"}, "CONNECTED")[0]["reason_code"],
                         "OPTIONAL_IMPROVE")

    def test_enough_unknown_and_high_count_lack_have_no_advice(self):
        for pair in (PairMetrics("cam_A", "cam_B"),
                     PairMetrics("cam_A", "cam_B", 100, 30, (0.8, 0.5)),
                     PairMetrics("cam_A", "cam_B", 500, 30, (0.8, 0.5))):
            self.assertEqual(guides([pair], {(pair.a, pair.b): pair_grade(pair)}, "DISCONNECTED"), [])


class ConfirmationTests(unittest.TestCase):
    def test_confirmation_and_candidate_reset(self):
        confirm = TwoRoundConfirmation("UNKNOWN")
        observed = [confirm.update(v) for v in ("WEAK", "ENOUGH", "WEAK", "WEAK", "LACK", "WEAK",
                                                "LACK", "LACK", "UNKNOWN", "UNKNOWN")]
        self.assertEqual(observed, ["UNKNOWN", "UNKNOWN", "UNKNOWN", "WEAK", "WEAK", "WEAK",
                                    "WEAK", "LACK", "LACK", "UNKNOWN"])

    def test_startup_and_change_confirm_on_second_round(self):
        judge = CoverageJudge()
        pairs = measured(CHAIN)
        first = judge.evaluate(pairs)
        self.assertEqual(first[2], "UNKNOWN")
        self.assertTrue(all(p.inliers is None for p in first[0]))
        second = judge.evaluate(pairs)
        self.assertEqual(second[2], "CONNECTED")
        disconnected = measured(CHAIN[:2])
        pending = judge.evaluate(disconnected)
        self.assertEqual(pending[2], "CONNECTED")
        # Hold count and centroid together with grade while a new grade is pending.
        self.assertEqual(pending[0][-1].inliers, 30)
        self.assertEqual(judge.evaluate(disconnected)[2], "DISCONNECTED")

    def test_single_fluctuation_is_ignored(self):
        judge = CoverageJudge()
        pairs = measured(CHAIN)
        judge.evaluate(pairs)
        judge.evaluate(pairs)
        judge.evaluate(measured(()))
        result = judge.evaluate(pairs)
        self.assertEqual(result[2], "CONNECTED")
        self.assertEqual(result[1][CHAIN[-1]], "ENOUGH")

    def test_missing_immediately_invalidates_and_recovery_confirms(self):
        judge = CoverageJudge()
        pairs = measured(CHAIN)
        judge.evaluate(pairs)
        judge.evaluate(pairs)
        missing = [PairMetrics(p.a, p.b) if "cam_D" in (p.a, p.b) else p for p in pairs]
        result = judge.evaluate(missing)
        self.assertEqual(result[2], "UNKNOWN")
        self.assertTrue(all(result[1][p] == "UNKNOWN" for p in PAIRS if "cam_D" in p))
        self.assertEqual(judge.evaluate(pairs)[2], "UNKNOWN")
        self.assertEqual(judge.evaluate(pairs)[2], "CONNECTED")

    def test_duplicate_pairs_rejected(self):
        with self.assertRaises(ValueError):
            CoverageJudge().evaluate([PairMetrics(*PAIRS[0])] * 6)


if __name__ == "__main__":
    unittest.main()
