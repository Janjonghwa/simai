"""Unit and real-track tests for V2 Stage A rotation-only Joint BA (S15P31A101-465)."""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.document import content_hash
from simai_ai.calibration.extrinsics.joint_ba_v2 import (
    CAMS,
    DEFAULT_STAGE_A_CONFIG,
    PAIRS,
    Problem,
    ProjectedProblem,
    StageAError,
    compute_height_checks,
    deduplicate_multiwindow_matches,
    evaluate,
    evaluate_object_cross_checks,
    extract_tracks_from_backgrounds,
    huber_terms,
    project,
    quarantine_shared_keypoints,
    run_stage_a_rotation_ba,
    select_anchor,
    spatial_split,
    statistics,
    worsened,
)
from simai_ai.providers.contract import schema_errors

CACHED_TRACKS_PATH = Path(
    "C:/Users/SSAFY/simai-local/codex-tmp/449/val01-calib-v2-20261103T153758154194Z/tracks.json"
)
CACHED_INITIAL_CALIB_PATH = Path(
    "C:/Users/SSAFY/simai-local/session11/lobby-bundle-rotfix/calibration.json"
)


def synthetic_scene() -> tuple[dict[str, Any], list[dict[str, Any]], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    config = copy.deepcopy(DEFAULT_STAGE_A_CONFIG)
    centers = np.array([[0.0, 0.0, 2.2], [6.0, 0.0, 2.2], [6.0, 6.0, 2.2], [0.0, 6.0, 2.2]])
    target = np.array([3.0, 3.0, 1.0])
    rotations = []
    for center in centers:
        forward = target - center
        forward /= np.linalg.norm(forward)
        right = np.cross(forward, [0.0, 0.0, 1.0])
        right /= np.linalg.norm(right)
        rotations.append(np.vstack((right, np.cross(forward, right), forward)))
    rotations_arr = np.array(rotations)
    intrinsics = np.tile([[1000.0, 0.0, 1152.0], [0.0, 1000.0, 648.0], [0.0, 0.0, 1.0]], (4, 1, 1))
    config["heights_m"] = dict.fromkeys(CAMS, 2.2)
    rng = np.random.default_rng(449)
    tracks: list[dict[str, Any]] = []
    for a, b in PAIRS:
        points = rng.uniform([1.0, 1.0, 0.2], [5.0, 5.0, 2.0], (32, 3))
        pixels = np.stack([project(points, c, intrinsics, rotations_arr, centers)[0] for c in (a, b)], axis=1)
        for i, uv in enumerate(pixels):
            tracks.append(
                {
                    "id": f"{a}-{b}:{i}",
                    "pair": [a, b],
                    "uv": uv.tolist(),
                    "nodes": [[a, 1000 * b + i], [b, 1000 * a + i]],
                    "split": "holdout" if i % 4 == 0 else "train",
                }
            )
    initial = rotations_arr.copy()
    initial[1] = Rotation.from_euler("x", 2.5, degrees=True).as_matrix() @ initial[1]
    return config, tracks, intrinsics, rotations_arr, initial, centers


def synthetic_calibration_and_tracks() -> tuple[dict[str, Any], dict[str, Any]]:
    _, tracks, K, truth, _, centers = synthetic_scene()
    initial = truth.copy()
    initial[0] = Rotation.from_euler("y", 1.2, degrees=True).as_matrix() @ initial[0]
    initial[1] = Rotation.from_euler("x", 2.5, degrees=True).as_matrix() @ initial[1]
    initial[2] = Rotation.from_euler("z", -1.5, degrees=True).as_matrix() @ initial[2]
    cameras = []
    for idx, cid in enumerate(CAMS):
        R_c = initial[idx]
        t_c = -R_c @ centers[idx]
        cameras.append(
            {
                "camera_id": cid,
                "image_size": [2304, 1296],
                "K": K[idx].tolist(),
                "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [0.0] * 5},
                "R": R_c.tolist(),
                "t": t_c.tolist(),
                "camera_height_m": round(float(centers[idx, 2]), 4),
                "reprojection_error_px": {"median": 4.0, "p95": 9.0},
            }
        )
    doc: dict[str, Any] = {
        "schema_version": "2.1.0",
        "calibration_version": "cal-synth-001",
        "provider_source": "G1_AUTO",
        "provider_version": "simai-session-calibration-0.2.0",
        "state": "PROVISIONAL",
        "created_at": 1760342400000,
        "coordinate_frame": {
            "frame": "WORLD",
            "unit": "m",
            "handedness": "right",
            "up_axis": "+Z",
            "site_id": "site-01",
        },
        "scale_basis": "MEASURED_CAMERA_HEIGHT",
        "world_axes_source": "WALL_AND_FLOOR",
        "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
        "cameras": cameras,
    }
    doc["content_hash"] = content_hash(doc)
    pairs_meta = {}
    for a, b in PAIRS:
        key = f"{CAMS[a]}-{CAMS[b]}"
        pairs_meta[key] = {
            "train": sum(t["pair"] == [a, b] and t["split"] == "train" for t in tracks),
            "holdout": sum(t["pair"] == [a, b] and t["split"] == "holdout" for t in tracks),
        }
    return doc, {"tracks": tracks, "pairs": pairs_meta}


# ---------------------------------------------------------------------------
# Control / split / quarantine / metric tests (ported from test_control.py)
# ---------------------------------------------------------------------------


def test_exact_quota_and_coverage_in_occupied_cells() -> None:
    pixels = [
        (x + k, y + k)
        for x in (10, 310, 610, 910)
        for y in (10, 310, 610, 910)
        for k in range(10)
    ]
    train, hold = spatial_split(pixels, (1200, 1200))
    assert len(hold) == 32
    assert not (set(train) & set(hold))
    assert sorted(train + hold) == list(range(160))
    for cell in range(16):
        assert sum(i // 10 == cell for i in hold) == 2


def test_sparse_cells_still_get_global_twenty_percent() -> None:
    points = [(i * 100 + 5, i * 100 + 5) for i in range(10)]
    train, hold = spatial_split(points, (1000, 1000), grid=(10, 10))
    assert len(hold) == 2
    assert len(train) == 8


def test_reproducible_and_different_seed() -> None:
    points = [(float(i), float(i % 6)) for i in range(100)]
    assert spatial_split(points, (100, 100)) == spatial_split(points, (100, 100))
    assert spatial_split(points, (100, 100), seed=1) != spatial_split(points, (100, 100), seed=2)


def test_border_pixels_and_empty_input() -> None:
    assert spatial_split([], (100, 100)) == ([], [])
    train, hold = spatial_split([(-1, -1), (0, 0), (100, 100), (101, 101), (50, 50)], (100, 100))
    assert len(hold) == 1
    assert len(train) == 4


def test_invalid_split_rejected() -> None:
    for fraction in (0, 1, -0.1):
        with pytest.raises(ValueError):
            spatial_split([], (100, 100), fraction=fraction)
    with pytest.raises(ValueError):
        spatial_split([(float("nan"), 0)], (100, 100))


def test_holdout_cannot_leak_through_other_edge() -> None:
    tracks = [
        {"id": "AB-held", "nodes": [[0, 7], [1, 8]], "split": "holdout"},
        {"id": "BC-leaks", "nodes": [[1, 8], [2, 3]], "split": "train"},
        {"id": "DA-leaks", "nodes": [[3, 4], [0, 7]], "split": "train"},
        {"id": "BC-safe", "nodes": [[1, 9], [2, 4]], "split": "train"},
    ]
    kept = quarantine_shared_keypoints(tracks)
    assert [t["id"] for t in kept] == ["AB-held", "BC-safe"]
    assert len(tracks) == 4


def test_multiwindow_deduplication_and_spatial_quarantine() -> None:
    candidates = [
        {"uv": [[100.0, 200.0], [110.0, 210.0]], "nodes": [[0, 1], [1, 2]]},
        {"uv": [[100.5, 200.5], [110.5, 210.5]], "nodes": [[0, 1000001], [1, 1000002]]},
        {"uv": [[300.0, 400.0], [310.0, 410.0]], "nodes": [[0, 1000003], [1, 1000004]]},
    ]
    deduped = deduplicate_multiwindow_matches(candidates, merge_radius_px=2.0)
    assert len(deduped) == 2
    tracks = [
        {
            "id": "AB-held",
            "pair": [0, 1],
            "uv": [[100.0, 200.0], [110.0, 210.0]],
            "nodes": [[0, 1], [1, 2]],
            "split": "holdout",
        },
        {
            "id": "BC-near-held",
            "pair": [1, 2],
            "uv": [[110.8, 210.5], [500.0, 600.0]],
            "nodes": [[1, 1000009], [2, 1000010]],
            "split": "train",
        },
        {
            "id": "BC-far",
            "pair": [1, 2],
            "uv": [[400.0, 500.0], [500.0, 600.0]],
            "nodes": [[1, 1000011], [2, 1000012]],
            "split": "train",
        },
    ]
    kept = quarantine_shared_keypoints(tracks, merge_radius_px=2.0)
    assert [t["id"] for t in kept] == ["AB-held", "BC-far"]


def test_statistics_and_worsened_and_anchor_and_huber() -> None:
    stats = statistics([1, 2, 3, float("inf")])
    assert stats["n"] == 4
    assert stats["invalid"] == 1
    assert stats["median"] is None
    assert stats["p90"] is None
    assert statistics([])["median"] is None

    s2 = statistics([10, 0, 20])
    assert s2["median"] == 10
    assert s2["p90"] == 18
    assert s2["p95"] == 19

    assert worsened({"median": 3, "p90": 5}, {"median": 2, "p90": 6})
    assert worsened({"median": 3, "p90": 5}, {"median": None, "p90": None})
    assert not worsened({"median": 3, "p90": 5}, {"median": 2, "p90": 4})

    errors = dict(zip(CAMS, (2.19, 6.19, 6.64, 1.8)))
    v0 = {
        "runs": [
            {
                "feature": "aliked",
                "reproj_current_cal": {c: {"2view": {"median": v}} for c, v in errors.items()},
            }
        ]
    }
    assert select_anchor(v0) == "cam_D"

    assert huber_terms(4, 2) == (4.0, 1.0, 0.0)
    value, first, second = huber_terms(4 + 1e-8, 2)
    assert abs(value - 4) < 1e-6
    assert abs(first - 1) < 1e-6
    assert math.isfinite(second)
    z, h = 25.0, 1e-3
    rho, first, second = huber_terms(z, 2)
    left, right = huber_terms(z - h, 2), huber_terms(z + h, 2)
    assert rho == 16.0
    assert abs((right[0] - left[0]) / (2 * h) - first) < 1e-7
    assert abs((right[1] - left[1]) / (2 * h) - second) < 1e-7


# ---------------------------------------------------------------------------
# Projected solver tests (ported from test_solver.py)
# ---------------------------------------------------------------------------


def test_projected_stage_a_undoes_2_5_degree_camera_rotation() -> None:
    config, tracks, K, truth, initial, centers = synthetic_scene()
    train = [t for t in tracks if t["split"] == "train"]
    problem = ProjectedProblem(train, K, initial, centers, 3, config, None)
    before = evaluate(tracks, K, initial, centers)
    R, C, result, info = problem.solve(initial, centers)
    after = evaluate(tracks, K, R, C)
    moved = np.degrees(Rotation.from_matrix(R[1] @ initial[1].T).magnitude())
    error = np.degrees(Rotation.from_matrix(R[1] @ truth[1].T).magnitude())
    assert result.success, result.message
    assert not info["adoption_forbidden"], info["adoption_blockers"]
    assert moved > 2.0
    assert error < 0.1
    assert info["final_cost"]["total"] < 0.1 * info["initial_cost"]["total"]
    assert after["pairs"]["cam_B-cam_C"]["median"] < 0.1 * before["pairs"]["cam_B-cam_C"]["median"]
    assert info["point_projection"]["converged"]
    assert info["point_projection"]["max_point_gradient"] < config["projected_point_gradient_tol"]
    assert info["residual_calls"] == result.nfev
    np.testing.assert_array_equal(C, centers)
    np.testing.assert_array_equal(R[3], initial[3])


def test_camera_only_solve_matches_joint_holdout() -> None:
    config, tracks, K, truth, initial, centers = synthetic_scene()
    rng = np.random.default_rng(101)
    for track in tracks:
        track["uv"] = (np.asarray(track["uv"]) + rng.normal(0, 0.15, (2, 2))).tolist()
    train = [t for t in tracks if t["split"] == "train"]
    joint = Problem(train, K, initial, centers, 3, config, None)
    projected = ProjectedProblem(train, K, initial, centers, 3, config, None)
    assert len(projected.pack(initial, centers)) == 9
    Rj, Cj, rj, _ = joint.solve(initial, centers)
    Rp, Cp, rp, info = projected.solve(initial, centers)
    assert rj.success
    assert rp.success
    assert not info["adoption_forbidden"]
    assert info["point_projection"]["converged"]
    for pair in evaluate(tracks, K, Rj, Cj)["pairs"]:
        a = evaluate(tracks, K, Rj, Cj)["pairs"][pair]["median"]
        b = evaluate(tracks, K, Rp, Cp)["pairs"][pair]["median"]
        assert abs(a - b) <= 0.05
    assert np.degrees(Rotation.from_matrix(Rp[1] @ truth[1].T).magnitude()) < 0.1
    np.testing.assert_array_equal(Cp, centers)


def test_profiled_jacobian_matches_residual_and_envelope_gradient() -> None:
    config, tracks, K, truth, _, centers = synthetic_scene()
    rng = np.random.default_rng(4404)
    for track in tracks:
        track["uv"] = (np.asarray(track["uv"]) + rng.normal(0, 0.15, (2, 2))).tolist()
    train = [t for t in tracks if t["split"] == "train"]
    train[0]["uv"][0][0] += 15.0
    p = ProjectedProblem(train, K, truth, centers, 3, config, None)
    x = p.pack(truth, centers)
    x[:3] = [0.0003, -0.0004, 0.0002]
    residual = p.residual(x)
    jac = p.profiled_jacobian(x)
    R, C, xyz = p.unpack(x)
    _, local_r, jp = p.point_system(xyz, R, C)
    fixed, _ = p.fixed_point_camera_system(x, xyz, R, C, local_r, jp)
    weights = p.loss(residual**2)[1]
    gradient = jac.T @ (weights * residual)
    prior_rows = len(p.free) * p.dof + 4
    camera_priors = slice(4 * len(train), 4 * len(train) + prior_rows)
    envelope = np.einsum("nrj,nr->j", fixed, p.point_weights(local_r) * local_r)
    envelope += jac[camera_priors].T @ residual[camera_priors]
    np.testing.assert_allclose(gradient, envelope, rtol=1e-5, atol=1e-3)
    for column in range(p.nc):
        plus, minus = x.copy(), x.copy()
        plus[column] += 1e-5
        minus[column] -= 1e-5
        numeric = (p.residual(plus) - p.residual(minus)) / 2e-5
        np.testing.assert_allclose(jac[:, column], numeric, rtol=3e-4, atol=3e-3)


def test_point_budget_exhaustion_is_marked_adoption_forbidden() -> None:
    config, tracks, K, _, initial, centers = synthetic_scene()
    config["projected_point_max_iterations"] = 0
    config["projected_point_fallback_nfev"] = 1
    config["max_nfev"] = 1
    train = [t for t in tracks if t["split"] == "train"]
    p = ProjectedProblem(train, K, initial, centers, 3, config, None)
    _, _, _, info = p.solve(initial, centers)
    assert info["adoption_forbidden"]
    assert "point_projection_not_converged" in info["adoption_blockers"]
    assert not info["point_projection"]["converged"]


# ---------------------------------------------------------------------------
# High-level Stage A pipeline & real cached tracks tests
# ---------------------------------------------------------------------------


def test_run_stage_a_rotation_ba_creates_new_version_and_preserves_input() -> None:
    calib_doc, tracks_doc = synthetic_calibration_and_tracks()
    snapshot = copy.deepcopy(calib_doc)

    res = run_stage_a_rotation_ba(
        calib_doc,
        tracks_doc,
        camera_heights_m={cid: 2.2 for cid in CAMS},
        height_tolerance_cm=5.0,
        anchor_camera_id="cam_D",
    )

    assert calib_doc == snapshot
    assert res.calibration_version != calib_doc["calibration_version"]
    assert res.calibration_doc["calibration_version"] == res.calibration_version
    assert res.calibration_doc["content_hash"] != calib_doc["content_hash"]
    assert schema_errors(res.calibration_doc, "calibration") == []
    assert res.converged is True
    assert res.stage_seconds < 10.0
    for cid in CAMS:
        assert res.pose_changes[cid]["center_cm"] == 0.0
        assert res.height_checks[cid]["within_tolerance"] is True


def test_compute_height_checks_flags_camera_exceeding_tolerance() -> None:
    checks = compute_height_checks(
        {"cam_A": 2.20, "cam_B": 2.20},
        {"cam_A": 2.23, "cam_B": 2.28},
        tolerance_cm=5.0,
    )
    assert checks["cam_A"]["difference_cm"] == 3.0
    assert checks["cam_A"]["within_tolerance"] is True
    assert checks["cam_B"]["difference_cm"] == 8.0
    assert checks["cam_B"]["within_tolerance"] is False


def test_evaluate_object_cross_checks_extension_hook() -> None:
    calib_doc, _ = synthetic_calibration_and_tracks()
    assert evaluate_object_cross_checks(calib_doc) == []

    def _checker(_doc: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"kind": "TRAFFIC_CONE", "passed": True, "message": "70cm cone within 1.1cm"}]

    out = evaluate_object_cross_checks(calib_doc, checker=_checker)
    assert out == [{"kind": "TRAFFIC_CONE", "passed": True, "message": "70cm cone within 1.1cm"}]


def test_run_stage_a_raises_insufficient_features_on_empty_tracks() -> None:
    calib_doc, _ = synthetic_calibration_and_tracks()
    with pytest.raises(StageAError) as exc_info:
        run_stage_a_rotation_ba(calib_doc, {"tracks": [], "pairs": {}})
    assert exc_info.value.code == "INSUFFICIENT_FEATURES"


def test_extract_tracks_from_backgrounds_with_injected_matcher() -> None:
    calib_doc, tracks_doc = synthetic_calibration_and_tracks()
    fake_imgs = {cid: [np.zeros((1296, 2304, 3), dtype=np.uint8)] for cid in CAMS}
    by_pair = {
        (a, b): [t for t in tracks_doc["tracks"] if t["pair"] == [a, b]]
        for a, b in PAIRS
    }
    call_idx = {"k": 0}
    pair_order = list(PAIRS)

    def _matcher(_img_a: np.ndarray, _img_b: np.ndarray) -> list[dict[str, Any]]:
        pair = pair_order[call_idx["k"] % len(pair_order)]
        call_idx["k"] += 1
        return [{"uv": t["uv"], "nodes": t["nodes"]} for t in by_pair[pair]]

    extracted = extract_tracks_from_backgrounds(
        fake_imgs,
        calib_doc,
        config={"background_windows": 1},
        matcher=_matcher,
    )
    assert len(extracted["tracks"]) >= 60
    res = run_stage_a_rotation_ba(calib_doc, extracted, anchor_camera_id="cam_D")
    assert res.converged is True


@pytest.mark.skipif(
    not (CACHED_TRACKS_PATH.exists() and CACHED_INITIAL_CALIB_PATH.exists()),
    reason="Real VAL-01 cached tracks or session11 calibration not present on this host",
)
def test_stage_a_on_real_val01_cached_tracks_converges_under_10s() -> None:
    heights_m = {"cam_A": 2.21, "cam_B": 2.195, "cam_C": 2.23, "cam_D": 2.24}
    res = run_stage_a_rotation_ba(
        CACHED_INITIAL_CALIB_PATH,
        CACHED_TRACKS_PATH,
        camera_heights_m=heights_m,
        height_tolerance_cm=5.0,
        anchor_camera_id="cam_D",
        stage_target_seconds=10.0,
    )
    assert res.converged is True
    assert res.stage_seconds < 10.0
    assert res.valid_points == 318
    assert res.anchor_camera_id == "cam_D"
    assert schema_errors(res.calibration_doc, "calibration") == []
    # Check that all pairs with sufficient holdout improved and none worsened
    for pair_key, pm in res.pair_metrics.items():
        if not pm["holdout_insufficient"]:
            assert pm["worsened"] is False, f"Holdout worsened on {pair_key}"
            assert pm["after_holdout"]["median"] < pm["before_holdout"]["median"]
    for cid in CAMS:
        assert res.pose_changes[cid]["center_cm"] == 0.0
        assert res.height_checks[cid]["within_tolerance"] is True
