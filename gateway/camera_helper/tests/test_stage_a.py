"""S15P31A101-761: 옆 4쌍 「Stage A 를 통과할 만큼 충분」 판정·흔들림·대각 제외. 표준 라이브러리만으로 돈다(스키마 검사는 jsonschema 있을 때)."""

import json
import logging
import unittest
from pathlib import Path

from simai_camera_helper.judge import (
    PAIRS,
    SIDE_PAIRS,
    CoverageJudge,
    PairMetrics,
    ReadinessParams,
    StageAReadiness,
    guides,
    occupied_cells,
    shortfall,
)
from simai_camera_helper.message import CameraFrame, build_message
from simai_camera_helper.progress import log_round

NOW = 1_793_690_000_000
DIAGONALS = (("cam_A", "cam_C"), ("cam_B", "cam_D"))


def good(a, b, inliers=80):
    return PairMetrics(a, b, 200, inliers, (0.7, 0.5), (0.3, 0.5), 5, 6)


def bad(a, b):
    return PairMetrics(a, b, 200, 40, (0.9, 0.85), (0.1, 0.8), 2, 2)


def round_pairs(side=good, diagonal=bad, overrides=None):
    overrides = overrides or {}
    result = []
    for a, b in PAIRS:
        maker = overrides.get((a, b), diagonal if (a, b) in DIAGONALS else side)
        result.append(maker(a, b))
    return result


def statuses(rows):
    return {(r["a"], r["b"]): r["status"] for r in rows}


class OccupiedCellsTests(unittest.TestCase):
    def test_corner_cluster_and_edge_band(self):
        corner = [(0.9, 0.9), (0.95, 0.92), (0.8, 0.8), (0.85, 0.99)]
        self.assertEqual(occupied_cells(corner), 1)
        band = [(0.85, y / 10 + 0.01) for y in range(10)] * 2
        self.assertEqual(occupied_cells(band), 4)

    def test_single_stray_point_does_not_count_and_borders_clip(self):
        self.assertEqual(occupied_cells([(0.1, 0.1)]), 0)
        self.assertEqual(occupied_cells([(1.0, 1.0), (0.99, 0.99)]), 1)
        self.assertEqual(occupied_cells([(0.0, 0.0), (0.24, 0.24), (0.25, 0.25)]), 1)

    def test_cells_validation(self):
        with self.assertRaises(ValueError):
            PairMetrics("cam_A", "cam_B", cells_a=3)
        with self.assertRaises(ValueError):
            PairMetrics("cam_A", "cam_B", 10, 5, cells_a=17)


class ShortfallTests(unittest.TestCase):
    def test_inlier_boundary(self):
        self.assertEqual(shortfall(PairMetrics("cam_A", "cam_B", 200, 59, cells_a=6, cells_b=6)), ["FEW_INLIERS"])
        self.assertEqual(shortfall(PairMetrics("cam_A", "cam_B", 200, 60, cells_a=6, cells_b=6)), [])

    def test_ratio_boundary(self):
        self.assertEqual(shortfall(PairMetrics("cam_A", "cam_B", 600, 60, cells_a=6, cells_b=6)), [])
        self.assertEqual(shortfall(PairMetrics("cam_A", "cam_B", 601, 60, cells_a=6, cells_b=6)), ["LOW_RATIO"])

    def test_cells_boundary_uses_worse_image(self):
        self.assertEqual(shortfall(PairMetrics("cam_A", "cam_B", 200, 90, cells_a=4, cells_b=4)), [])
        self.assertEqual(shortfall(PairMetrics("cam_A", "cam_B", 200, 90, cells_a=9, cells_b=3)), ["CLUSTERED"])
        # 칸 수를 모르면 충분이라 하지 않는다
        self.assertEqual(shortfall(PairMetrics("cam_A", "cam_B", 200, 90)), ["CLUSTERED"])

    def test_unknown_is_not_a_shortfall(self):
        self.assertIsNone(shortfall(PairMetrics("cam_A", "cam_B")))

    def test_params_validate(self):
        for kwargs in ({"min_inliers": 0}, {"min_cells": 0}, {"min_cells": 17}, {"window_rounds": 1},
                       {"min_inlier_ratio": 0}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                ReadinessParams(**kwargs)


class StabilityTests(unittest.TestCase):
    def test_ready_only_after_full_window(self):
        tracker = StageAReadiness()
        seen = [statuses(tracker.update(round_pairs()))[("cam_A", "cam_B")] for _ in range(4)]
        self.assertEqual(seen, ["CONFIRMING", "CONFIRMING", "CONFIRMING", "READY"])

    def test_one_bad_round_is_unstable_until_window_refills(self):
        tracker = StageAReadiness()
        key = ("cam_B", "cam_C")
        for _ in range(4):
            tracker.update(round_pairs())
        rows = tracker.update(round_pairs(overrides={key: bad}))
        row = next(r for r in rows if (r["a"], r["b"]) == key)
        self.assertEqual(row["status"], "UNSTABLE")
        self.assertEqual(row["shortfall"], ["FEW_INLIERS", "CLUSTERED"])
        self.assertEqual((row["sufficient_rounds"], row["observed_rounds"]), (3, 4))
        after = [statuses(tracker.update(round_pairs()))[key] for _ in range(4)]
        self.assertEqual(after, ["UNSTABLE", "UNSTABLE", "UNSTABLE", "READY"])

    def test_alternating_never_ready_and_all_bad_is_short(self):
        tracker = StageAReadiness()
        key = ("cam_C", "cam_D")
        seen = [statuses(tracker.update(round_pairs(overrides={key: good if k % 2 else bad})))[key]
                for k in range(8)]
        self.assertEqual(seen[0], "SHORT")
        self.assertTrue(all(s == "UNSTABLE" for s in seen[1:]))
        short = StageAReadiness()
        for _ in range(5):
            last = statuses(short.update(round_pairs(side=bad)))
        self.assertEqual(set(last.values()), {"SHORT"})

    def test_missing_measurement_resets_window(self):
        tracker = StageAReadiness()
        key = ("cam_A", "cam_D")
        for _ in range(4):
            tracker.update(round_pairs())
        rows = tracker.update(round_pairs(overrides={key: lambda a, b: PairMetrics(a, b)}))
        row = next(r for r in rows if (r["a"], r["b"]) == key)
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertEqual((row["inliers"], row["cells_a"], row["observed_rounds"], row["shortfall"]),
                         (None, None, 0, []))
        self.assertEqual(statuses(tracker.update(round_pairs()))[key], "CONFIRMING")


class DiagonalTests(unittest.TestCase):
    def test_rows_are_side_pairs_only_and_ignore_diagonals(self):
        tracker = StageAReadiness()
        for diagonal in (bad, good, bad, good):
            rows = tracker.update(round_pairs(diagonal=diagonal))
        self.assertEqual([(r["a"], r["b"]) for r in rows], list(SIDE_PAIRS))
        self.assertEqual(set(statuses(rows).values()), {"READY"})

    def test_flagged_side_pair_gets_direction_even_with_many_inliers_but_diagonal_does_not(self):
        judge = CoverageJudge()
        side_short = {("cam_C", "cam_D"): bad}
        for _ in range(2):
            _, _, state, _, instructions = judge.evaluate(round_pairs(overrides=side_short))
        cd = [g for g in instructions if g["pair"] == ["cam_C", "cam_D"]]
        self.assertEqual({(g["camera_id"], g["direction"], g["magnitude"]) for g in cd},
                         {("cam_C", "RIGHT", "SMALL"), ("cam_D", "LEFT", "SMALL")})
        self.assertEqual(state, "CONNECTED")
        # 대각 쌍은 인라이어 40·구석 몰림이어도 Stage A 기준이 아니라 안내하지 않는다(기존 ≥30 규칙)
        self.assertFalse([g for g in instructions if tuple(g["pair"]) in DIAGONALS])
        # 충분한 옆 쌍(ENOUGH)도 안내하지 않는다
        self.assertFalse([g for g in instructions if g["pair"] == ["cam_A", "cam_B"]])

    def test_guides_without_flags_keep_previous_rule(self):
        pairs = round_pairs(overrides={("cam_C", "cam_D"): bad})
        grades = {(p.a, p.b): "ENOUGH" for p in pairs}
        self.assertEqual(guides(pairs, grades, "CONNECTED"), [])


class MessageTests(unittest.TestCase):
    def message(self, judge, pairs):
        frames = [CameraFrame(cid, NOW) for cid in ("cam_A", "cam_B", "cam_C", "cam_D")]
        return build_message("gw-lobby", NOW, frames, pairs, judge)

    def test_message_carries_side_readiness_and_matches_contract(self):
        judge = CoverageJudge()
        for _ in range(4):
            message = self.message(judge, round_pairs(overrides={("cam_B", "cam_C"): bad}))
        self.assertEqual(message["schema_version"], "1.3.0")
        readiness = message["stage_a_readiness"]
        self.assertEqual((readiness["min_inliers"], readiness["min_cells"], readiness["window_rounds"],
                          readiness["grid"]), (60, 4, 4, [4, 4]))
        self.assertEqual(statuses(readiness["pairs"]), {("cam_A", "cam_B"): "READY", ("cam_B", "cam_C"): "SHORT",
                                                        ("cam_C", "cam_D"): "READY", ("cam_A", "cam_D"): "READY"})
        for pair in message["pairs"]:
            self.assertNotIn("cells_a", pair)
        schema = Path(__file__).resolve().parents[3] / "contracts/messages/coverage_guide.schema.json"
        try:
            import jsonschema
            from referencing import Registry, Resource
        except ImportError:
            self.skipTest("jsonschema not installed")
        defs = schema.parent.parent / "common/defs.schema.json"
        registry = Registry().with_resource("https://simai.dev/contracts/common/defs.schema.json",
                                            Resource.from_contents(json.loads(defs.read_text(encoding="utf-8"))))
        jsonschema.Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")),
                                        registry=registry).validate(message)

    def test_unavailable_camera_makes_its_side_pairs_unknown(self):
        judge = CoverageJudge()
        frames = [CameraFrame(cid, NOW if cid != "cam_D" else None) for cid in ("cam_A", "cam_B", "cam_C", "cam_D")]
        message = build_message("gw-lobby", NOW, frames, round_pairs(), judge)
        got = statuses(message["stage_a_readiness"]["pairs"])
        self.assertEqual(got[("cam_C", "cam_D")], "UNKNOWN")
        self.assertEqual(got[("cam_A", "cam_D")], "UNKNOWN")
        self.assertEqual(got[("cam_A", "cam_B")], "CONFIRMING")

    def test_round_log_appends_side_readiness(self):
        judge = CoverageJudge()
        message = self.message(judge, round_pairs())
        with self.assertLogs("simai_camera_helper.service", level=logging.INFO) as logs:
            log_round(NOW, 10, {cid: NOW for cid in ("cam_A", "cam_B", "cam_C", "cam_D")},
                      [p["grade"] for p in message["pairs"]], "ok", message["stage_a_readiness"]["pairs"])
        self.assertTrue(logs.output[0].endswith(
            "send=ok stage_a=[AB:CONFIRMING(80,5,6) BC:CONFIRMING(80,5,6) CD:CONFIRMING(80,5,6) "
            "AD:CONFIRMING(80,5,6)]"))


if __name__ == "__main__":
    unittest.main()
