"""기준 시점 맞추기(S15P31A101-765): 회전 분해·부호·허용 오차 경계·매칭 실패·기준 없음·계산 공유·계약."""

import json
import math
import unittest
from pathlib import Path

import numpy as np

from simai_camera_helper.aim_check import (
    AimChecker,
    AimConfig,
    AimHysteresis,
    compose,
    decompose,
    describe_aim,
    measure,
    target_of,
)
from simai_camera_helper.judge import CAMERAS, PAIRS, CoverageJudge, PairMetrics, Params
from simai_camera_helper.message import CameraFrame, build_message
from simai_camera_helper.service import Client
from simai_camera_helper.view_check import RoundFeatures, ViewCheckConfig, ViewChecker, analysis_K, parse_lens

W, H = 1280, 720
K = [[900.0, 0.0, 639.5], [0.0, 900.0, 359.5], [0.0, 0.0, 1.0]]
DIST = {"model": "OPENCV", "coeffs": [-0.12, 0.03, 0.0, 0.0, 0.0]}
OK = {cid: "OK" for cid in CAMERAS}


def validate_coverage_guide_schema(instance: dict) -> None:
    import jsonschema
    from referencing import Registry, Resource

    schema_path = Path(__file__).resolve().parents[3] / "contracts/messages/coverage_guide.schema.json"
    defs_path = schema_path.parent.parent / "common/defs.schema.json"
    resource = Resource.from_contents(json.loads(defs_path.read_text(encoding="utf-8")))
    registry = Registry().with_resource("https://simai.dev/contracts/common/defs.schema.json", resource)
    jsonschema.Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8")), registry=registry
    ).validate(instance)


def scene(seed=7, w=W, h=H):
    import cv2

    rng = np.random.default_rng(seed)
    img = (rng.random((h, w)) * 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (0, 0), 3)
    for _ in range(260):
        x, y = int(rng.integers(0, w - 40)), int(rng.integers(0, h - 40))
        cv2.rectangle(img, (x, y), (x + int(rng.integers(8, 60)), y + int(rng.integers(8, 60))),
                      int(rng.integers(0, 256)), -1)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def rotated(image, R):
    """같은 위치에서 R(기준 광선 → 지금 광선)만큼 돈 카메라가 본 영상(왜곡 없음)."""
    import cv2

    Km = np.asarray(K)
    return cv2.warpPerspective(image, Km @ R @ np.linalg.inv(Km), (image.shape[1], image.shape[0]),
                               borderMode=cv2.BORDER_REFLECT)


def distorted(ideal, coeffs):
    """왜곡 없는 영상 → 렌즈 왜곡을 입힌 영상(출력 픽셀마다 왜곡을 지운 자리를 읽는다)."""
    import cv2

    Km = np.asarray(K)
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float64)
    pts = np.stack([xs.ravel(), ys.ravel()], axis=1)[:, None]
    und = cv2.undistortPoints(pts, Km, np.asarray(coeffs), P=Km)[:, 0]
    mx = und[:, 0].reshape(H, W).astype(np.float32)
    my = und[:, 1].reshape(H, W).astype(np.float32)
    return cv2.remap(ideal, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def reference(images, distortion=None, scene_version="scene-aim", calibration="cal-aim"):
    cams = [{"camera_id": cid, "url": f"https://bucket.example/{cid}.jpg", "image_size": [W, H], "K": K,
             "distortion": distortion or {"model": "NONE", "coeffs": []}} for cid in images]
    return {"scene_version": scene_version, "calibration_version": calibration, "expires_at_ms": 1, "cameras": cams}


class FakeFetch:
    def __init__(self, images):
        self.images = images
        self.calls = []

    def __call__(self, url):
        cid = url.rsplit("/", 1)[1].removesuffix(".jpg")
        self.calls.append(cid)
        return self.images[cid]


def lens(distortion=None):
    return parse_lens({"K": K, "image_size": [W, H], "distortion": distortion or {"model": "NONE", "coeffs": []}})


def synthetic_matches(R, n=400, noise_px=0.1, seed=3):
    """분석 해상도·왜곡 제거 좌표의 정확한 대응(기준 p → 지금 q = K R K⁻¹ p), 지금 영상 안에 들어온 것만."""
    cfg = ViewCheckConfig()
    Ka = analysis_K(lens(), cfg)
    w, h = round(W * cfg.max_image_side / W), round(H * cfg.max_image_side / W)
    rng = np.random.default_rng(seed)
    p = rng.uniform([0, 0], [w, h], size=(n * 3, 2))
    proj = np.c_[p, np.ones(len(p))] @ (Ka @ R @ np.linalg.inv(Ka)).T
    q = proj[:, :2] / proj[:, 2, None] + rng.normal(0, noise_px, size=(len(p), 2))
    inside = (q[:, 0] >= 0) & (q[:, 0] < w) & (q[:, 1] >= 0) & (q[:, 1] < h) & (proj[:, 2] > 0)
    return p[inside][:n], q[inside][:n]


class RotationDecompositionTest(unittest.TestCase):
    def test_compose_and_decompose_round_trip(self):
        rng = np.random.default_rng(5)
        for _ in range(50):
            pan, tilt, roll = rng.uniform(-0.6, 0.6, 3)
            got = decompose(compose(pan, tilt, roll))
            np.testing.assert_allclose(got, (pan, tilt, roll), atol=1e-12)

    def test_sign_convention_right_and_up_are_positive(self):
        """교정량: 기준 영상 중심이 지금 영상 오른쪽에 보이면 오른쪽으로(+pan), 위에 보이면 위로(+tilt) 돌린다."""
        right = target_of(compose(math.radians(3), 0, 0), lens())
        up = target_of(compose(0, math.radians(3), 0), lens())
        self.assertGreater(right[0], (W - 1) / 2 + 40)
        self.assertAlmostEqual(right[1], (H - 1) / 2, delta=0.5)
        self.assertLess(up[1], (H - 1) / 2 - 40)
        self.assertAlmostEqual(up[0], (W - 1) / 2, delta=0.5)
        # roll +: 카메라를 (뒤에서 볼 때) 시계 방향으로 돌리면 카메라 x 축이 아래(+y)로 기운다
        x_axis = compose(0, 0, math.radians(5)) @ np.array([1.0, 0.0, 0.0])
        self.assertGreater(x_axis[1], 0)

    def test_recovers_angles_from_exact_correspondences(self):
        cfg = ViewCheckConfig()
        for angles in ((1.2, -0.4, 0.3), (-2.5, 1.0, -1.5), (0.1, 0.05, 0.0), (6.0, -3.0, 2.0)):
            R = compose(*map(math.radians, angles))
            p, q = synthetic_matches(R)
            v = measure(p, q, lens(), cfg, AimConfig())
            self.assertNotEqual(v.status, "UNKNOWN", angles)
            np.testing.assert_allclose([math.degrees(a) for a in (v.pan_rad, v.tilt_rad, v.roll_rad)], angles,
                                       atol=0.01)

    def test_hysteresis_needs_two_rounds_to_move_up_or_down(self):
        h = AimHysteresis(2)
        seq = [(1, "ALIGNED", "MISALIGNED"),   # 처음은 가장 낮은 단계에서, 한 라운드로는 오르지 않는다
               (2, "ALIGNED", "ALIGNED"),      # 2라운드 연속 충족
               (2, "MISALIGNED", "ALIGNED"),   # 같은 키프레임은 세지 않는다
               (3, "NEAR", "ALIGNED"),         # 한 라운드 미충족으로는 내려가지 않는다
               (4, "ALIGNED", "ALIGNED"),
               (5, "NEAR", "ALIGNED"),
               (6, "MISALIGNED", "NEAR"),      # 2라운드 연속 미충족 → 둘 중 높은 단계(NEAR)로
               (7, "MISALIGNED", "MISALIGNED"),
               (8, "NEAR", "MISALIGNED"),
               (9, "ALIGNED", "NEAR")]         # NEAR·ALIGNED 연속 → 둘 다 충족한 NEAR 까지만
        for ts, raw, shown in seq:
            self.assertEqual(h.observe("cam_A", ts, raw), shown, (ts, raw))
        h.reset("cam_A")
        self.assertEqual(h.observe("cam_A", 10, "ALIGNED"), "MISALIGNED")

    def test_tolerance_boundary(self):
        cfg = ViewCheckConfig()
        # 두 단계: 재사용 가능(광축 0.25°·roll 1.0°), 새 맵 세팅엔 충분(광축 1.5°·roll 3.0°). 이번 라운드 측정만(히스테리시스 전)
        cases = [((0.20, 0.0, 0.0), "ALIGNED"), ((0.30, 0.0, 0.0), "NEAR"),
                 ((0.0, -0.22, 0.0), "ALIGNED"), ((0.0, 0.28, 0.0), "NEAR"),
                 ((0.2, 0.2, 0.0), "NEAR"),  # 광축 차 0.28° > 0.25°: 합이 아닌 크기로 본다
                 ((0.1, 0.1, 0.9), "ALIGNED"), ((0.0, 0.0, 1.1), "NEAR"),
                 ((1.4, 0.3, 0.0), "NEAR"), ((1.2, 1.0, 0.0), "MISALIGNED"),  # 광축 1.43° / 1.56°
                 ((0.5, 0.0, 2.9), "NEAR"), ((0.0, 0.0, 3.2), "MISALIGNED")]
        for angles, status in cases:
            p, q = synthetic_matches(compose(*map(math.radians, angles)), noise_px=0.05)
            v = measure(p, q, lens(), cfg, AimConfig())
            self.assertEqual(v.status, status, angles)
            # 광축이 허용 오차 안이면 화살표를 그리지 않는다(roll 만 남은 경우 포함)
            self.assertEqual(v.arrow_rad is None, math.hypot(angles[0], angles[1]) <= 0.25, angles)

    def test_target_outside_image_is_null_but_arrow_from_angles(self):
        R = compose(math.radians(40), 0, 0)
        self.assertIsNone(target_of(R, lens()))
        p, q = synthetic_matches(R, n=300)
        v = measure(p, q, lens(), ViewCheckConfig(), AimConfig())
        self.assertEqual(v.status, "MISALIGNED")
        self.assertIsNone(v.target_point)
        self.assertIsNone(v.target_px)
        self.assertAlmostEqual(v.arrow_rad, 0.0, places=3)  # 오른쪽
        up = measure(*synthetic_matches(compose(0, math.radians(2), 0)), lens(), ViewCheckConfig(), AimConfig())
        self.assertAlmostEqual(up.arrow_rad, -math.pi / 2, places=2)  # 위쪽(영상 y 아래가 +)
        self.assertLess(up.target_point[1], 0.5)


class AimCheckerImageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.refs = {cid: scene(seed=i + 11) for i, cid in enumerate(CAMERAS)}

    def frames(self, ts, images):
        return {cid: (ts, images[cid]) for cid in CAMERAS}

    def test_recovers_pan_tilt_roll_per_camera_from_images(self):
        angles = {"cam_A": (1.2, 0.0, 0.0), "cam_B": (0.0, -0.8, 0.0), "cam_C": (0.0, 0.0, 1.5),
                  "cam_D": (0.1, 0.05, 0.2)}
        current = {cid: rotated(self.refs[cid], compose(*map(math.radians, angles[cid]))) for cid in CAMERAS}
        checker = AimChecker(fetch=FakeFetch(self.refs))
        first = checker.check(reference(self.refs), self.frames(10, current), OK)
        self.assertTrue(all(c["status"] == "MISALIGNED" for c in first["cameras"]))  # 한 라운드로는 오르지 않는다
        aim = checker.check(reference(self.refs), self.frames(11, current), OK)
        by = {c["camera_id"]: c for c in aim["cameras"]}
        for cid, expected in angles.items():
            got = [math.degrees(by[cid][k]) for k in ("pan_rad", "tilt_rad", "roll_rad")]
            np.testing.assert_allclose(got, expected, atol=0.05, err_msg=cid)
        self.assertEqual([by[c]["status"] for c in CAMERAS], ["NEAR", "NEAR", "NEAR", "ALIGNED"])
        self.assertIsNone(by["cam_D"]["arrow_rad"])
        self.assertGreater(by["cam_A"]["target_point"][0], 0.5)  # 기준 중심이 오른쪽에 → 오른쪽으로
        self.assertGreater(by["cam_B"]["target_point"][1], 0.5)  # 아래로 돌릴 것(tilt −)
        validate_coverage_guide_schema(self.message(aim))

    def test_lens_distortion_is_removed_before_rotation(self):
        R = compose(math.radians(-1.5), math.radians(0.7), math.radians(-0.5))
        refs = {cid: distorted(self.refs[cid], DIST["coeffs"]) for cid in CAMERAS}
        current = {cid: distorted(rotated(self.refs[cid], R), DIST["coeffs"]) for cid in CAMERAS}
        aim = AimChecker(fetch=FakeFetch(refs)).check(reference(refs, DIST), self.frames(10, current), OK)
        for c in aim["cameras"]:
            got = [math.degrees(c[k]) for k in ("pan_rad", "tilt_rad", "roll_rad")]
            np.testing.assert_allclose(got, (-1.5, 0.7, -0.5), atol=0.06, err_msg=c["camera_id"])
            # 목표 점은 왜곡 그대로의 원본 영상 좌표(왜곡을 무시하면 가장자리에서 어긋난다)
            ideal = target_of(R, lens(DIST))
            self.assertAlmostEqual(c["target_px"][0], round(ideal[0], 1), delta=0.05)

    def test_match_failure_is_unknown(self):
        other = {cid: scene(seed=99 + i) for i, cid in enumerate(CAMERAS)}
        parallax = {cid: self._parallax(self.refs[cid]) for cid in CAMERAS}
        current = {"cam_A": other["cam_A"], "cam_B": parallax["cam_B"], "cam_C": self.refs["cam_C"],
                   "cam_D": self.refs["cam_D"]}
        refs = dict(self.refs)
        checker = AimChecker(fetch=FakeFetch(refs))
        checker.check(reference(refs), self.frames(9, current), {**OK, "cam_D": "STALE"})
        aim = checker.check(reference(refs), self.frames(10, current), {**OK, "cam_D": "STALE"})
        by = {c["camera_id"]: c for c in aim["cameras"]}
        self.assertEqual(by["cam_A"]["status"], "UNKNOWN")
        self.assertIn(by["cam_A"]["reason"], ("FEW_MATCHES", "NOT_ROTATION"))
        self.assertEqual((by["cam_B"]["status"], by["cam_B"]["reason"]), ("UNKNOWN", "NOT_ROTATION"))
        self.assertEqual(by["cam_C"]["status"], "ALIGNED")
        self.assertEqual((by["cam_D"]["status"], by["cam_D"]["reason"]), ("UNKNOWN", "STALE"))
        for c in (by["cam_A"], by["cam_B"], by["cam_D"]):
            self.assertIsNone(c["pan_rad"])
            self.assertIsNone(c["target_point"])
            self.assertIsNone(c["arrow_rad"])
        validate_coverage_guide_schema(self.message(aim))

    @staticmethod
    def _parallax(image, near_px=36, far_px=6):
        out = image.copy()
        half = image.shape[0] // 2
        out[:half] = np.roll(image[:half], far_px, axis=1)
        out[half:] = np.roll(image[half:], near_px, axis=1)
        return out

    def test_no_reference_means_no_aim_check(self):
        checker = AimChecker(fetch=FakeFetch(self.refs))
        self.assertIsNone(checker.check(None, self.frames(1, self.refs), OK))
        self.assertIsNone(checker.check({"scene_version": "s"}, self.frames(1, self.refs), OK))
        partial = reference({"cam_A": self.refs["cam_A"]})
        checker.check(partial, self.frames(1, self.refs), OK)
        aim = checker.check(partial, self.frames(2, self.refs), OK)
        by = {c["camera_id"]: c for c in aim["cameras"]}
        self.assertEqual(by["cam_A"]["status"], "ALIGNED")
        self.assertEqual((by["cam_B"]["status"], by["cam_B"]["reason"]), ("UNKNOWN", "NO_REFERENCE"))

    def test_shares_current_sift_and_reference_with_reuse_check(self):
        """같은 맵·같은 키프레임이면 재사용 시점 확인이 뽑은 SIFT·받은 기준 특징을 그대로 쓴다(중복 계산 없음)."""
        fetch = FakeFetch(self.refs)
        viewer = ViewChecker(fetch=fetch)
        aimer = AimChecker(fetch=fetch)
        features = RoundFeatures()
        ref = reference(self.refs)
        viewer.check(ref, self.frames(5, self.refs), OK, features)
        aim = aimer.check(ref, self.frames(5, self.refs), OK, features, donors=(viewer,))
        self.assertEqual(sorted(fetch.calls), sorted(CAMERAS))  # 기준 사진은 카메라마다 한 번만 받았다
        self.assertEqual(features.hits, 4)  # 지금 키프레임 SIFT 4장은 aim 이 다시 뽑지 않았다
        self.assertTrue(all(abs(c["pan_rad"]) < 1e-3 and abs(c["roll_rad"]) < 1e-3 for c in aim["cameras"]))
        self.assertIn('"A":"-0.00/', describe_aim(aim))

    def message(self, aim):
        params = Params(image_width_px=1024, max_keypoints=2048, intrinsics_source="DEFAULT", ransac_threshold_px=3.0)
        judge = CoverageJudge(params)
        pairs = [PairMetrics(a, b, 120, 70, (0.5, 0.5), (0.5, 0.5), 5, 5) for a, b in PAIRS]
        cams = [CameraFrame(cid, 1_793_690_000_000) for cid in CAMERAS]
        msg = build_message("gw-lobby", 1_793_690_000_500, cams, pairs, judge, aim_check=aim)
        self.assertEqual(msg["schema_version"], "1.4.0")
        self.assertIn("stage_a_readiness", msg)
        return msg


class AimMessageTest(unittest.TestCase):
    def test_contract_rejects_aim_check_with_old_version_and_unknown_with_angles(self):
        import jsonschema

        params = Params(image_width_px=1024, max_keypoints=2048, intrinsics_source="DEFAULT", ransac_threshold_px=3.0)
        judge = CoverageJudge(params)
        pairs = [PairMetrics(a, b) for a, b in PAIRS]
        cams = [CameraFrame(cid, None, has_video=False) for cid in CAMERAS]
        aim = {"scene_version": "s", "calibration_version": "c", "max_aim_rad": 0.004363, "max_roll_rad": 0.017453,
               "max_setup_aim_rad": 0.02618, "max_setup_roll_rad": 0.05236, "hysteresis_rounds": 2,
               "cameras": [{"camera_id": cid, "status": "UNKNOWN", "reason": "NO_VIDEO", "matches": None,
                            "inlier_ratio": None, "pan_rad": None, "tilt_rad": None, "roll_rad": None,
                            "target_point": None, "target_px": None, "arrow_rad": None} for cid in CAMERAS]}
        msg = build_message("gw-lobby", 1_793_690_000_500, cams, pairs, judge, aim_check=aim, stage_a=False)
        self.assertEqual(msg["schema_version"], "1.4.0")
        validate_coverage_guide_schema(msg)
        old = dict(msg, schema_version="1.3.0")
        with self.assertRaises(jsonschema.ValidationError):
            validate_coverage_guide_schema(old)
        bad = json.loads(json.dumps(msg))
        bad["aim_check"]["cameras"][0]["pan_rad"] = 0.01
        with self.assertRaises(jsonschema.ValidationError):
            validate_coverage_guide_schema(bad)

    def test_client_keeps_aim_reference_only_while_demand_is_active(self):
        client = Client("https://example.com", "secret-token-xyz")
        ref = {"scene_version": "s", "calibration_version": "c", "cameras": []}
        client.note_response({"active_demand": True, "aim_reference": ref})
        self.assertEqual(client.aim_reference(), ref)
        client.note_response({"active_demand": False, "aim_reference": ref})
        self.assertIsNone(client.aim_reference())
        client.note_response({"active_demand": True})  # 옛 EC2: 키 없음
        self.assertIsNone(client.aim_reference())
