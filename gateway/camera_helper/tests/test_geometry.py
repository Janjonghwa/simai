import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from simai_camera_helper.judge import CAMERAS, direction
from simai_camera_helper.matcher import (
    Intrinsics,
    geometric_metrics,
    load_intrinsics,
    prepare_image,
    warp_yaw,
)

HAS_GEOMETRY = all(importlib.util.find_spec(m) is not None for m in ("numpy", "cv2"))


@unittest.skipUnless(HAS_GEOMETRY, "numpy and opencv-python-headless are not installed")
class GeometryTests(unittest.TestCase):
    def test_positive_depth_geometry_rejects_outliers(self):
        import cv2
        import numpy as np

        rng = np.random.default_rng(445)
        xyz = rng.uniform([-2, -1, 4], [2, 1, 10], (100, 3))
        K = np.array([[500, 0, 320], [0, 500, 240], [0, 0, 1]], float)
        pa = xyz @ K.T
        pa = pa[:, :2] / pa[:, 2:]
        shifted = xyz + [1, 0, 0]
        pb = shifted @ K.T
        pb = pb[:, :2] / pb[:, 2:]
        pb[-20:] = rng.uniform([0, 0], [640, 480], (20, 2))
        cv2.setRNGSeed(445)
        result = geometric_metrics("cam_A", "cam_B", pa, pb, K, K, (640, 480), (640, 480), 1.0)
        self.assertEqual(result.raw_matches, 100)
        self.assertGreaterEqual(result.inliers, 75)
        self.assertLess(result.inliers, 90)
        self.assertIsNotNone(result.centroid_a)
        # S15P31A101-761: 인라이어가 화면에 퍼져 있으면 4x4 칸 여러 개에 걸친다
        self.assertGreaterEqual(min(result.cells_a, result.cells_b), 4)

    def test_below_eight_matches_cannot_make_an_essential_edge(self):
        import numpy as np

        for n in (0, 4, 7):
            points = np.zeros((n, 2))
            result = geometric_metrics("cam_A", "cam_B", points, points, np.eye(3), np.eye(3),
                                       (640, 480), (640, 480), 2.0)
            self.assertEqual(result.inliers, 0)
            self.assertIsNone(result.centroid_a)

    def test_lens_loader_ignores_extrinsic_values(self):
        import numpy as np

        document = {"cameras": [{"camera_id": cid, "K": [[200, 0, 200], [0, 200, 100], [0, 0, 1]],
                                  "image_size": [400, 200],
                                  "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [0] * 5},
                                  "R": "deliberately invalid and unused", "t": "unused"} for cid in CAMERAS]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "intrinsics.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            lenses = load_intrinsics(path)
        image, K = prepare_image(np.zeros((200, 400, 3), np.uint8), lenses["cam_A"], 200)
        self.assertEqual(image.shape, (100, 200, 3))
        np.testing.assert_allclose(K, [[100, 0, 100], [0, 100, 50], [0, 0, 1]])
        self.assertEqual(set(vars(lenses["cam_A"])), {"K", "distortion", "image_size"})

    def test_default_intrinsics_are_explicit(self):
        import numpy as np

        image, K = prepare_image(np.zeros((100, 200, 3), np.uint8), None, 200)
        self.assertEqual(K[0, 0], 100)
        self.assertEqual(image.shape, (100, 200, 3))

    def test_positive_yaw_warp_direction_at_ten_and_twenty_degrees(self):
        import numpy as np

        K = np.array([[400, 0, 200], [0, 400, 100], [0, 0, 1]], float)
        image = np.zeros((200, 400, 3), np.uint8)
        image[98:103, 198:203] = 255
        for degrees in (10, 20):
            with self.subTest(degrees=degrees):
                warped = warp_yaw(image, K, np.deg2rad(degrees))
                y, x = np.where(warped[:, :, 0] > 100)
                centroid = float(x.mean() / 400), float(y.mean() / 200)
                self.assertEqual(direction(centroid), "RIGHT")

    def test_identity_warp(self):
        import numpy as np

        image = np.random.default_rng(0).integers(0, 255, (80, 100, 3), dtype=np.uint8)
        lens = Intrinsics(np.eye(3), np.zeros(5), (100, 80))
        np.testing.assert_array_equal(warp_yaw(image, lens.K, 0), image)


if __name__ == "__main__":
    unittest.main()
