import json
import unittest
from pathlib import Path

from simai_camera_helper.judge import CAMERAS, PAIRS, CoverageJudge, PairMetrics
from simai_camera_helper.message import CameraFrame, build_message

NOW = 1_793_690_000_000


def pairs():
    edges = (("cam_A", "cam_B"), ("cam_B", "cam_C"), ("cam_A", "cam_D"))
    return [PairMetrics(a, b, 100, 40 if (a, b) in edges else 0,
                        (0.6, 0.5) if (a, b) in edges else None,
                        (0.4, 0.5) if (a, b) in edges else None) for a, b in PAIRS]


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.cameras = [CameraFrame(cid, NOW - i * 280) for i, cid in enumerate(CAMERAS)]
        self.judge = CoverageJudge()

    def message(self, cameras=None, clock=NOW):
        return build_message("gw-lobby", clock, cameras or self.cameras, pairs(), self.judge)

    def test_wire_keys_and_values(self):
        self.message()
        message = self.message()
        self.assertEqual(set(message), {"schema_version", "source_id", "computed_at_ms", "valid_until_ms",
                                        "capture_skew_ms", "params", "cameras", "pairs", "connectivity",
                                        "components", "guides"})
        self.assertEqual(message["capture_skew_ms"], 840)
        self.assertEqual(message["valid_until_ms"] - message["computed_at_ms"], 10000)
        self.assertEqual(message["connectivity"], "CONNECTED")
        self.assertEqual(message["components"], [list(CAMERAS)])
        self.assertEqual(len(message["pairs"]), 6)
        self.assertEqual(message["pairs"][0]["centroid_a"], [0.6, 0.5])
        self.assertEqual(set(message["pairs"][0]), {"a", "b", "raw_matches", "inliers", "inlier_ratio",
                                                  "grade", "centroid_a", "centroid_b"})
        self.assertEqual(json.loads(json.dumps(message, allow_nan=False)), message)

    def test_no_video_null_metrics(self):
        cameras = [CameraFrame(cid, NOW if cid != "cam_D" else None) for cid in CAMERAS]
        self.message(cameras)
        message = self.message(cameras)
        self.assertEqual(message["cameras"][-1]["status"], "NO_VIDEO")
        self.assertEqual(message["connectivity"], "UNKNOWN")
        for p in message["pairs"]:
            if "cam_D" in (p["a"], p["b"]):
                self.assertEqual(p["grade"], "UNKNOWN")
                self.assertTrue(all(p[k] is None for k in ("raw_matches", "inliers", "inlier_ratio",
                                                          "centroid_a", "centroid_b")))

    def test_stale_boundary(self):
        self.assertEqual(CameraFrame("cam_A", NOW - 5000).status(NOW), "OK")
        self.assertEqual(CameraFrame("cam_A", NOW - 5001).status(NOW), "STALE")
        self.assertEqual(CameraFrame("cam_A", NOW, False).status(NOW), "NO_VIDEO")

    def test_lobby_keyframe_ages_remain_usable(self):
        cameras = [CameraFrame(cid, NOW - age) for cid, age in zip(CAMERAS, (400, 1400, 2800, 4200))]
        self.message(cameras)  # Existing two-round grade hysteresis.
        message = self.message(cameras)
        self.assertTrue(all(c["status"] == "OK" for c in message["cameras"]))
        self.assertTrue(all(p["grade"] != "UNKNOWN" for p in message["pairs"]))

    def test_configured_stale_boundary_masks_pairs_and_skew(self):
        cameras = [CameraFrame(cid, NOW - age) for cid, age in zip(CAMERAS, (0, 6000, 6001, 7000))]
        message = build_message("gw-lobby", NOW, cameras, pairs(), self.judge, stale_ms=6000)
        self.assertEqual([c["status"] for c in message["cameras"]], ["OK", "OK", "STALE", "STALE"])
        self.assertEqual(message["capture_skew_ms"], 6000)
        for pair in message["pairs"]:
            if "cam_C" in (pair["a"], pair["b"]) or "cam_D" in (pair["a"], pair["b"]):
                self.assertEqual(pair["grade"], "UNKNOWN")
                self.assertIsNone(pair["inliers"])

    def test_all_missing_has_unknown_skew(self):
        message = self.message([CameraFrame(cid, None) for cid in CAMERAS])
        self.assertIsNone(message["capture_skew_ms"])
        self.assertEqual(message["connectivity"], "UNKNOWN")

    def test_fewer_than_two_usable_timestamps_has_unknown_skew(self):
        message = self.message([CameraFrame(cid, NOW if cid == "cam_A" else NOW - 6000) for cid in CAMERAS])
        self.assertIsNone(message["capture_skew_ms"])

    def test_camera_coverage_validation(self):
        with self.assertRaises(ValueError):
            self.message(self.cameras[:3])
        with self.assertRaises(ValueError):
            self.message([self.cameras[0]] * 4)

    def test_matches_external_schema_when_available(self):
        schema = Path(__file__).resolve().parents[3] / "contracts/messages/coverage_guide.schema.json"
        if not schema.is_file():
            self.skipTest("coverage_guide schema is owned by -443 and not present in this worktree")
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        from referencing import Registry, Resource
        defs = schema.parent.parent / "common/defs.schema.json"
        resource = Resource.from_contents(json.loads(defs.read_text(encoding="utf-8")))
        registry = Registry().with_resource("https://simai.dev/contracts/common/defs.schema.json", resource)
        self.message()
        jsonschema.Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")),
                                        registry=registry).validate(self.message())


if __name__ == "__main__":
    unittest.main()
