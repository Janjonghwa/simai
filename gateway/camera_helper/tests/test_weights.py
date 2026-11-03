"""Opt-in session11 acceptance checks. Synthetic warps are not field camera-turn measurements."""

import json
import os
import unittest
from dataclasses import asdict

try:
    import pytest
except ImportError:
    raise unittest.SkipTest("pytest and local model dependencies are not installed") from None

from simai_camera_helper.judge import CoverageJudge, pair_grade
from simai_camera_helper.matcher import CameraMatcher, Intrinsics, load_intrinsics, prepare_image, warp_yaw
from simai_camera_helper.offline import DEFAULT_INTRINSICS, DEFAULT_RECORDING, DEFAULT_WEIGHTS, selected_keyframes

pytestmark = pytest.mark.weights


@pytest.fixture(scope="module")
def scene():
    if os.environ.get("SIMAI_CAMERA_HELPER_WEIGHT_TESTS") != "1":
        pytest.skip("set SIMAI_CAMERA_HELPER_WEIGHT_TESTS=1 to run local recording/weight acceptance tests")
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    np = pytest.importorskip("numpy")
    pytest.importorskip("torch")
    pytest.importorskip("lightglue")
    from simai_camera_helper.judge import CAMERAS

    lenses = load_intrinsics(DEFAULT_INTRINSICS)
    images, undistorted_lenses = {}, {}
    for cid in CAMERAS:
        frames = selected_keyframes(DEFAULT_RECORDING / cid / "epoch_0000.h264")
        try:
            original = next(frames)
        finally:
            frames.close()
        clean, K = prepare_image(original, lenses[cid], 1024)
        images[cid] = clean
        undistorted_lenses[cid] = Intrinsics(K, np.zeros(5), (clean.shape[1], clean.shape[0]))
    matcher = CameraMatcher(DEFAULT_WEIGHTS / "aliked-n16.pth",
                            DEFAULT_WEIGHTS / "aliked_lightglue_v0-1_arxiv.pth",
                            intrinsics=undistorted_lenses)
    baseline = matcher.match(images)
    return matcher, images, undistorted_lenses, baseline


def test_session11_expected_edges_and_connectivity(scene):
    _, _, _, pairs = scene
    grades = {(p.a, p.b): pair_grade(p) for p in pairs}
    for key in (("cam_A", "cam_B"), ("cam_B", "cam_C"), ("cam_A", "cam_D")):
        assert grades[key] == "ENOUGH", {str(k): v for k, v in grades.items()}
    judge = CoverageJudge()
    judge.evaluate(pairs)
    assert judge.evaluate(pairs)[2] == "CONNECTED"
    # C-D is expected weaker; keep its actual grade visible on failure rather than relaxing the threshold.
    assert grades[("cam_C", "cam_D")] in ("WEAK", "LACK")


@pytest.mark.parametrize("degrees", [10, 20])
def test_session11_cam_c_yaw_grade_drop_and_direction(scene, degrees, tmp_path):
    import numpy as np

    matcher, images, lenses, baseline = scene
    turned = dict(images)
    turned["cam_C"] = warp_yaw(images["cam_C"], lenses["cam_C"].K, np.deg2rad(degrees))
    after = matcher.match(turned)
    judge = CoverageJudge()
    judge.evaluate(baseline)
    judge.evaluate(baseline)
    judge.evaluate(after)
    _, grades, state, _, instructions = judge.evaluate(after)
    report = {"yaw_deg": degrees, "baseline": [asdict(p) | {"grade": pair_grade(p)} for p in baseline],
              "after": [asdict(p) | {"grade": pair_grade(p)} for p in after],
              "connectivity": state, "guides": instructions,
              "input_provenance": {"frames": {"source": "FILE"}, "is_upper_bound": False},
              "limitation": "synthetic image homography; no newly revealed background"}
    (tmp_path / "rotation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    order = {"UNKNOWN": -1, "LACK": 0, "WEAK": 1, "ENOUGH": 2}
    assert any("cam_C" in (p.a, p.b) and order[grades[(p.a, p.b)]] < order[pair_grade(p)]
               for p in baseline), report
    assert any(g["camera_id"] == "cam_C" and g["direction"] == "RIGHT" for g in instructions), report
