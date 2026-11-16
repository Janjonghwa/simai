"""재사용 시점 확인(S15P31A101-712): 합성 장면으로 같음·회전·이동·다른 장면·근거 없음을 본다."""

import json
import math
import unittest
from pathlib import Path

import numpy as np

from simai_camera_helper.judge import CAMERAS, PAIRS, CoverageJudge, PairMetrics, Params
from simai_camera_helper.message import CameraFrame, build_message
from simai_camera_helper.view_check import (
    MultiViewChecker,
    Verdict,
    ViewChecker,
    ViewConfirm,
    describe,
    parse_reference,
)

W, H = 1280, 720


def validate_coverage_guide_schema(instance: dict) -> None:
    """coverage_guide 계약 검증(다른 시험 모듈을 import 하지 않아 어디서 돌려도 된다)."""
    import jsonschema
    from referencing import Registry, Resource

    schema_path = Path(__file__).resolve().parents[3] / "contracts/messages/coverage_guide.schema.json"
    defs_path = schema_path.parent.parent / "common/defs.schema.json"
    resource = Resource.from_contents(json.loads(defs_path.read_text(encoding="utf-8")))
    registry = Registry().with_resource("https://simai.dev/contracts/common/defs.schema.json", resource)
    jsonschema.Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8")), registry=registry
    ).validate(instance)
K = [[900.0, 0.0, 639.5], [0.0, 900.0, 359.5], [0.0, 0.0, 1.0]]


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


def rotate(image, yaw_rad, pitch_rad=0.0):
    import cv2

    c, s = math.cos(yaw_rad), math.sin(yaw_rad)
    Ry = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    c, s = math.cos(pitch_rad), math.sin(pitch_rad)
    Rx = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    Km = np.asarray(K)
    Hm = Km @ Ry @ Rx @ np.linalg.inv(Km)
    return cv2.warpPerspective(image, Hm, (image.shape[1], image.shape[0]), borderMode=cv2.BORDER_REFLECT)


def parallax(image, near_px=36, far_px=6):
    """위쪽(먼 벽)과 아래쪽(가까운 바닥)이 다르게 밀린 장면: 카메라가 옆으로 옮겨진 시차."""
    out = image.copy()
    half = image.shape[0] // 2
    out[:half] = np.roll(image[:half], far_px, axis=1)
    out[half:] = np.roll(image[half:], near_px, axis=1)
    return out


def noisy(image, seed=1):
    rng = np.random.default_rng(seed)
    return np.clip(image.astype(np.int16) + rng.integers(-6, 7, image.shape), 0, 255).astype(np.uint8)


def reference(images, distortion=None):
    cams = []
    for cid in images:
        cams.append({
            "camera_id": cid, "url": f"https://bucket.example/{cid}.jpg", "image_size": [W, H], "K": K,
            "distortion": distortion or {"model": "NONE", "coeffs": []},
        })
    return {"scene_version": "scene-1", "calibration_version": "cal-1", "expires_at_ms": 1, "cameras": cams}


class FakeFetch:
    def __init__(self, images):
        self.images = images
        self.calls = []
        self.fail = set()

    def __call__(self, url):
        cid = url.rsplit("/", 1)[1].removesuffix(".jpg")
        self.calls.append(cid)
        if cid in self.fail:
            raise OSError("network")
        return self.images[cid]


OK = {cid: "OK" for cid in CAMERAS}


class ViewCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.refs = {cid: scene(seed=i + 1) for i, cid in enumerate(CAMERAS)}

    def checker(self, refs=None):
        fetch = FakeFetch(refs or self.refs)
        return ViewChecker(fetch=fetch), fetch

    def frames(self, ts, images):
        return {cid: (ts, images[cid]) for cid in CAMERAS}

    def test_same_view_with_noise_is_same(self):
        checker, fetch = self.checker()
        current = {cid: noisy(img) for cid, img in self.refs.items()}
        view = checker.check(reference(self.refs), self.frames(10, current), OK)
        self.assertEqual([c["status"] for c in view["cameras"]], ["SAME"] * 4)
        self.assertEqual(view["scene_version"], "scene-1")
        # 기준 사진은 한 번만 받는다
        checker.check(reference(self.refs), self.frames(20, current), OK)
        self.assertEqual(sorted(fetch.calls), sorted(CAMERAS))

    def test_small_rotation_within_limit_is_same_with_angle(self):
        checker, _ = self.checker()
        current = dict(self.refs)
        current["cam_B"] = rotate(self.refs["cam_B"], math.radians(4))
        view = checker.check(reference(self.refs), self.frames(10, current), OK)
        cam_b = view["cameras"][1]
        self.assertEqual(cam_b["status"], "SAME")
        self.assertAlmostEqual(cam_b["rotation_rad"], math.radians(4), delta=math.radians(0.6))

    def test_large_rotation_needs_two_keyframes_then_changed(self):
        checker, _ = self.checker()
        current = dict(self.refs)
        current["cam_C"] = rotate(self.refs["cam_C"], math.radians(14), math.radians(3))
        first = checker.check(reference(self.refs), self.frames(10, current), OK)
        self.assertEqual((first["cameras"][2]["status"], first["cameras"][2]["reason"]), ("UNKNOWN", "CONFIRMING"))
        # 같은 키프레임이면 다시 세지 않는다
        again = checker.check(reference(self.refs), self.frames(10, current), OK)
        self.assertEqual(again["cameras"][2]["reason"], "CONFIRMING")
        second = checker.check(reference(self.refs), self.frames(20, current), OK)
        cam_c = second["cameras"][2]
        self.assertEqual((cam_c["status"], cam_c["reason"]), ("CHANGED", "ROTATED"))
        self.assertGreater(cam_c["rotation_rad"], math.radians(10))
        self.assertEqual([c["status"] for i, c in enumerate(second["cameras"]) if i != 2], ["SAME"] * 3)

    def test_parallax_is_moved(self):
        checker, _ = self.checker()
        current = dict(self.refs)
        current["cam_A"] = parallax(self.refs["cam_A"])
        checker.check(reference(self.refs), self.frames(10, current), OK)
        view = checker.check(reference(self.refs), self.frames(20, current), OK)
        self.assertEqual((view["cameras"][0]["status"], view["cameras"][0]["reason"]), ("CHANGED", "MOVED"))

    def test_different_scene_is_view_different(self):
        checker, _ = self.checker()
        current = dict(self.refs)
        current["cam_D"] = scene(seed=99)
        checker.check(reference(self.refs), self.frames(10, current), OK)
        view = checker.check(reference(self.refs), self.frames(20, current), OK)
        self.assertEqual((view["cameras"][3]["status"], view["cameras"][3]["reason"]), ("CHANGED", "VIEW_DIFFERENT"))

    def test_evidence_gap_resets_confirmation(self):
        checker, _ = self.checker()
        current = dict(self.refs)
        current["cam_D"] = scene(seed=99)
        checker.check(reference(self.refs), self.frames(10, current), OK)
        stale = dict(OK, cam_D="STALE")
        gap = checker.check(reference(self.refs), self.frames(20, current), stale)
        self.assertEqual(gap["cameras"][3]["reason"], "STALE")
        after = checker.check(reference(self.refs), self.frames(30, current), OK)
        self.assertEqual(after["cameras"][3]["reason"], "CONFIRMING")

    def test_missing_reference_fetch_failure_and_retry(self):
        clock = [0.0]
        fetch = FakeFetch(self.refs)
        fetch.fail.add("cam_B")
        checker = ViewChecker(fetch=fetch, clock=lambda: clock[0])
        ref = reference({cid: self.refs[cid] for cid in ("cam_A", "cam_B", "cam_C")})  # cam_D 사진 없음
        view = checker.check(ref, self.frames(10, self.refs), OK)
        reasons = {c["camera_id"]: c["reason"] for c in view["cameras"]}
        self.assertEqual((reasons["cam_B"], reasons["cam_D"]), ("NO_REFERENCE", "NO_REFERENCE"))
        fetch.fail.clear()
        checker.check(ref, self.frames(20, self.refs), OK)
        self.assertEqual(fetch.calls.count("cam_B"), 1)  # 30 s 안에는 다시 받지 않는다
        clock[0] = 31.0
        view = checker.check(ref, self.frames(30, self.refs), OK)
        self.assertEqual(view["cameras"][1]["status"], "SAME")

    def test_aspect_mismatch_and_no_reference_block(self):
        import cv2

        checker, _ = self.checker()
        current = dict(self.refs)
        current["cam_A"] = cv2.resize(self.refs["cam_A"], (640, 480))
        view = checker.check(reference(self.refs), self.frames(10, current), dict(OK, cam_B="NO_VIDEO"))
        self.assertEqual(view["cameras"][0]["reason"], "STREAM_MISMATCH")
        self.assertEqual(view["cameras"][1]["reason"], "NO_VIDEO")
        self.assertIsNone(checker.check(None, self.frames(10, current), OK))
        self.assertIsNone(parse_reference({"scene_version": "s", "cameras": []}))

    def test_lower_resolution_stream_and_distortion_still_same(self):
        import cv2

        dist = {"model": "OPENCV_PINHOLE", "coeffs": [-0.12, 0.03, 0.0, 0.0, 0.0]}
        checker, _ = self.checker()
        current = {cid: cv2.resize(img, (960, 540), interpolation=cv2.INTER_AREA) for cid, img in self.refs.items()}
        view = checker.check(reference(self.refs, dist), self.frames(10, current), OK)
        self.assertEqual([c["status"] for c in view["cameras"]], ["SAME"] * 4)

    def test_confirm_only_counts_distinct_keyframes(self):
        confirm = ViewConfirm()
        changed = Verdict("CHANGED", "MOVED", 100, 0.3)
        self.assertEqual(confirm.observe("cam_A", 1, changed).reason, "CONFIRMING")
        self.assertEqual(confirm.observe("cam_A", 1, changed).reason, "CONFIRMING")
        self.assertEqual(confirm.observe("cam_A", 2, changed).status, "CHANGED")
        self.assertEqual(confirm.observe("cam_A", 3, Verdict("SAME", None, 100, 0.9, 0.0)).status, "SAME")
        self.assertEqual(confirm.observe("cam_A", 4, changed).reason, "CONFIRMING")

    def test_message_with_view_check_matches_contract(self):
        checker, _ = self.checker()
        current = dict(self.refs)
        current["cam_C"] = rotate(self.refs["cam_C"], math.radians(14))
        checker.check(reference(self.refs), self.frames(10, current), OK)
        view = checker.check(reference(self.refs), self.frames(20, current), OK)
        msg = build_message(
            "gw-lobby", 1_793_690_000_000, [CameraFrame(cid, 1_793_689_999_000) for cid in CAMERAS],
            [PairMetrics(a, b, 120, 45, (0.5, 0.5), (0.5, 0.5)) for a, b in PAIRS],
            CoverageJudge(Params()), reuse_view_check=view, stage_a=False,
        )
        self.assertEqual(msg["schema_version"], "1.1.0")
        validate_coverage_guide_schema(msg)
        self.assertIn("C\":\"C:ROTATED", describe(view))
        plain = build_message(
            "gw-lobby", 1_793_690_000_000, [CameraFrame(cid, 1_793_689_999_000) for cid in CAMERAS],
            [PairMetrics(a, b) for a, b in PAIRS], CoverageJudge(Params()), stage_a=False,
        )
        self.assertEqual(plain["schema_version"], "1.0.0")
        self.assertNotIn("reuse_view_check", plain)


class MultiViewCheckTest(unittest.TestCase):
    """S15P31A101-714: 후보 맵마다 따로 비교하고, 목록에서 빠진 맵의 확인 상태는 버린다."""

    @classmethod
    def setUpClass(cls):
        cls.refs = {cid: scene(seed=i + 1) for i, cid in enumerate(CAMERAS)}
        cls.other = {cid: scene(seed=i + 40) for i, cid in enumerate(CAMERAS)}

    def multi(self):
        images = {**{f"a-{cid}": img for cid, img in self.refs.items()},
                  **{f"b-{cid}": img for cid, img in self.other.items()}}

        def fetch(url):
            return images[url.rsplit("/", 1)[1].removesuffix(".jpg")]

        return MultiViewChecker(factory=lambda: ViewChecker(fetch=fetch))

    @staticmethod
    def ref(prefix, scene_version):
        r = reference(CAMERAS)
        r["scene_version"] = scene_version
        r["calibration_version"] = "cal-" + scene_version
        for cam in r["cameras"]:
            cam["url"] = f"https://bucket.example/{prefix}-{cam['camera_id']}.jpg"
        return r

    def test_each_candidate_map_is_compared_separately(self):
        multi = self.multi()
        refs = [self.ref("a", "scene-a"), self.ref("b", "scene-b"), self.ref("a", "scene-a")]
        frames = {cid: (10, noisy(self.refs[cid])) for cid in CAMERAS}
        views = multi.check_all(refs, frames, OK)
        self.assertEqual([v["scene_version"] for v in views], ["scene-a", "scene-b"])  # 같은 맵은 한 번만
        self.assertEqual({c["status"] for c in views[0]["cameras"]}, {"SAME"})
        # 다른 맵은 첫 회 CHANGED 가 확인 중(UNKNOWN·CONFIRMING)이다
        self.assertEqual({c["reason"] for c in views[1]["cameras"]}, {"CONFIRMING"})
        frames = {cid: (20, noisy(self.refs[cid], seed=2)) for cid in CAMERAS}
        views = multi.check_all(refs, frames, OK)
        self.assertEqual({c["reason"] for c in views[1]["cameras"]}, {"VIEW_DIFFERENT"})
        # 목록에서 빠진 맵은 버리고, 다시 들어오면 확인을 처음부터 센다
        multi.check_all([self.ref("a", "scene-a")], {cid: (30, self.refs[cid]) for cid in CAMERAS}, OK)
        self.assertEqual(set(multi.checkers), {("scene-a", "cal-scene-a")})
        views = multi.check_all(refs, {cid: (40, self.refs[cid]) for cid in CAMERAS}, OK)
        self.assertEqual({c["reason"] for c in views[1]["cameras"]}, {"CONFIRMING"})

    def test_message_with_view_checks_matches_contract(self):
        multi = self.multi()
        views = multi.check_all([self.ref("a", "scene-a"), self.ref("b", "scene-b")],
                                {cid: (10, self.refs[cid]) for cid in CAMERAS}, OK)
        msg = build_message(
            "gw-lobby", 1_793_690_000_000, [CameraFrame(cid, 1_793_689_999_000) for cid in CAMERAS],
            [PairMetrics(a, b, 120, 45, (0.5, 0.5), (0.5, 0.5)) for a, b in PAIRS],
            CoverageJudge(Params()), reuse_view_checks=views, stage_a=False,
        )
        self.assertEqual(msg["schema_version"], "1.2.0")
        self.assertNotIn("reuse_view_check", msg)
        validate_coverage_guide_schema(msg)
        # 1.3.0(S15P31A101-761): Stage A 기준을 함께 실어도 후보 맵 목록은 그대로 계약에 맞는다
        both = build_message(
            "gw-lobby", 1_793_690_000_000, [CameraFrame(cid, 1_793_689_999_000) for cid in CAMERAS],
            [PairMetrics(a, b, 120, 45, (0.5, 0.5), (0.5, 0.5)) for a, b in PAIRS],
            CoverageJudge(Params()), reuse_view_checks=views,
        )
        self.assertEqual(both["schema_version"], "1.3.0")
        self.assertIn("stage_a_readiness", both)
        validate_coverage_guide_schema(both)


class ServiceWiringTest(unittest.TestCase):
    def test_client_keeps_candidate_list_only_from_new_ec2(self):
        from simai_camera_helper.service import Client

        client = Client("https://example.com", "tok-secret", "/api/devices/coverage-guides")
        ref = {"scene_version": "s", "calibration_version": "c", "cameras": []}
        client.note_response({"active_demand": True, "reuse_reference": ref})
        self.assertIsNone(client.reuse_references())  # 옛 EC2: 목록 키가 없으면 하나만 비교한다
        client.note_response({"active_demand": True, "reuse_reference": ref, "reuse_references": [ref, "x"]})
        self.assertEqual(client.reuse_references(), [ref])
        client.note_response({"active_demand": False, "reuse_references": [ref]})
        self.assertIsNone(client.reuse_references())

    def test_view_checks_failure_never_blocks_the_guide(self):
        from simai_camera_helper.service import reuse_view_checks

        frames = [CameraFrame(cid, 1_000) for cid in CAMERAS]

        class Broken:
            def check_all(self, *args):
                raise RuntimeError("boom")

        self.assertIsNone(reuse_view_checks(Broken(), [{"scene_version": "s"}], {}, frames, 1_000, 5_000))
        self.assertIsNone(reuse_view_checks(Broken(), None, {}, frames, 1_000, 5_000))

    def test_client_keeps_reference_only_while_demand_is_active(self):
        from simai_camera_helper.service import Client

        client = Client("https://example.com", "tok-secret", "/api/devices/coverage-guides")
        ref = {"scene_version": "s", "calibration_version": "c", "cameras": []}
        client.note_response({"active_demand": True, "reuse_reference": ref})
        self.assertEqual(client.reuse_reference(), ref)
        client.note_response({"accepted": True})  # 수요 필드가 없는 응답은 건드리지 않는다
        self.assertEqual(client.reuse_reference(), ref)
        client.note_response({"active_demand": True, "reuse_reference": None})
        self.assertIsNone(client.reuse_reference())
        client.note_response({"active_demand": False, "reuse_reference": ref})
        self.assertIsNone(client.reuse_reference())

    def test_view_check_failure_never_blocks_the_guide(self):
        from simai_camera_helper.service import reuse_view_check

        frames = [CameraFrame(cid, 1_000) for cid in CAMERAS]

        class Broken:
            def check(self, *args):
                raise RuntimeError("boom")

        self.assertIsNone(reuse_view_check(Broken(), {"scene_version": "s"}, {}, frames, 1_000, 5_000))
        self.assertIsNone(reuse_view_check(Broken(), None, {}, frames, 1_000, 5_000))


if __name__ == "__main__":
    unittest.main()
