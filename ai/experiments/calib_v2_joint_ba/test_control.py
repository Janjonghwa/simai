"""Run with unittest; these tests intentionally require no NumPy/native libraries."""

import math
import unittest

from control import (
    CAMS, deduplicate_multiwindow_matches, huber_terms, quarantine_shared_keypoints,
    select_anchor, spatial_split, statistics, worsened,
)


class SplitTests(unittest.TestCase):
    def test_exact_quota_and_coverage_in_occupied_cells(self):
        pixels = [(x + k, y + k) for x in (10, 310, 610, 910)
                  for y in (10, 310, 610, 910) for k in range(10)]
        train, hold = spatial_split(pixels, (1200, 1200))
        self.assertEqual(len(hold), 32)
        self.assertFalse(set(train) & set(hold))
        self.assertEqual(sorted(train + hold), list(range(160)))
        for cell in range(16):
            self.assertEqual(sum(i // 10 == cell for i in hold), 2)

    def test_sparse_cells_still_get_global_twenty_percent(self):
        points = [(i * 100 + 5, i * 100 + 5) for i in range(10)]
        train, hold = spatial_split(points, (1000, 1000), grid=(10, 10))
        self.assertEqual(len(hold), 2)
        self.assertEqual(len(train), 8)

    def test_reproducible_and_different_seed(self):
        points = [(float(i), float(i % 6)) for i in range(100)]
        self.assertEqual(spatial_split(points, (100, 100)), spatial_split(points, (100, 100)))
        self.assertNotEqual(spatial_split(points, (100, 100), seed=1),
                            spatial_split(points, (100, 100), seed=2))

    def test_border_pixels_and_empty_input(self):
        self.assertEqual(spatial_split([], (100, 100)), ([], []))
        train, hold = spatial_split([(-1, -1), (0, 0), (100, 100), (101, 101), (50, 50)], (100, 100))
        self.assertEqual(len(hold), 1)
        self.assertEqual(len(train), 4)

    def test_invalid_split_rejected(self):
        for fraction in (0, 1, -0.1):
            with self.assertRaises(ValueError):
                spatial_split([], (100, 100), fraction=fraction)
        with self.assertRaises(ValueError):
            spatial_split([(float("nan"), 0)], (100, 100))

    def test_holdout_cannot_leak_through_other_edge(self):
        tracks = [
            {"id": "AB-held", "nodes": [[0, 7], [1, 8]], "split": "holdout"},
            {"id": "BC-leaks", "nodes": [[1, 8], [2, 3]], "split": "train"},
            {"id": "DA-leaks", "nodes": [[3, 4], [0, 7]], "split": "train"},
            {"id": "BC-safe", "nodes": [[1, 9], [2, 4]], "split": "train"},
        ]
        kept = quarantine_shared_keypoints(tracks)
        self.assertEqual([t["id"] for t in kept], ["AB-held", "BC-safe"])
        self.assertEqual(len(tracks), 4)

    def test_multiwindow_deduplication_and_spatial_quarantine(self):
        candidates = [
            {"uv": [[100.0, 200.0], [110.0, 210.0]], "nodes": [[0, 1], [1, 2]]},
            {"uv": [[100.5, 200.5], [110.5, 210.5]], "nodes": [[0, 1000001], [1, 1000002]]},
            {"uv": [[300.0, 400.0], [310.0, 410.0]], "nodes": [[0, 1000003], [1, 1000004]]},
        ]
        deduped = deduplicate_multiwindow_matches(candidates, merge_radius_px=2.0)
        self.assertEqual(len(deduped), 2)
        tracks = [
            {"id": "AB-held", "pair": [0, 1], "uv": [[100.0, 200.0], [110.0, 210.0]],
             "nodes": [[0, 1], [1, 2]], "split": "holdout"},
            {"id": "BC-near-held", "pair": [1, 2], "uv": [[110.8, 210.5], [500.0, 600.0]],
             "nodes": [[1, 1000009], [2, 1000010]], "split": "train"},
            {"id": "BC-far", "pair": [1, 2], "uv": [[400.0, 500.0], [500.0, 600.0]],
             "nodes": [[1, 1000011], [2, 1000012]], "split": "train"},
        ]
        kept = quarantine_shared_keypoints(tracks, merge_radius_px=2.0)
        self.assertEqual([t["id"] for t in kept], ["AB-held", "BC-far"])


class MetricTests(unittest.TestCase):
    def test_statistics_keep_original_observation_denominator(self):
        stats = statistics([1, 2, 3, float("inf")])
        self.assertEqual(stats["n"], 4)
        self.assertEqual(stats["invalid"], 1)
        self.assertIsNone(stats["median"])
        self.assertIsNone(stats["p90"])

    def test_empty_is_unknown(self):
        self.assertIsNone(statistics([])["median"])

    def test_percentile_interpolation(self):
        stats = statistics([10, 0, 20])
        self.assertEqual(stats["median"], 10)
        self.assertEqual(stats["p90"], 18)
        self.assertEqual(stats["p95"], 19)

    def test_tail_worsening_not_hidden_by_better_median(self):
        self.assertTrue(worsened({"median": 3, "p90": 5}, {"median": 2, "p90": 6}))
        self.assertTrue(worsened({"median": 3, "p90": 5}, {"median": None, "p90": None}))
        self.assertFalse(worsened({"median": 3, "p90": 5}, {"median": 2, "p90": 4}))

    def test_anchor_selected_by_recorded_error_not_camera_name(self):
        errors = dict(zip(CAMS, (2.19, 6.19, 6.64, 1.8)))
        v0 = {"runs": [{"feature": "aliked", "reproj_current_cal": {
            c: {"2view": {"median": value}} for c, value in errors.items()}}]}
        self.assertEqual(select_anchor(v0), "cam_D")


class RobustLossTests(unittest.TestCase):
    def test_continuity_at_huber_boundary(self):
        self.assertEqual(huber_terms(4, 2), (4, 1, 0))
        value, first, second = huber_terms(4 + 1e-8, 2)
        self.assertAlmostEqual(value, 4, places=6)
        self.assertAlmostEqual(first, 1, places=6)
        self.assertTrue(math.isfinite(second))

    def test_outlier_derivatives_agree_with_finite_difference(self):
        z, h = 25.0, 1e-3
        rho, first, second = huber_terms(z, 2)
        left, right = huber_terms(z - h, 2), huber_terms(z + h, 2)
        self.assertEqual(rho, 16)
        self.assertAlmostEqual((right[0] - left[0]) / (2 * h), first, places=7)
        self.assertAlmostEqual((right[1] - left[1]) / (2 * h), second, places=7)


if __name__ == "__main__":
    unittest.main()
