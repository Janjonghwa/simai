"""현장 배경 매칭과 5점 기하 회귀 시험 (합성 데이터)."""
import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from simai_ai.calibration.extrinsics.feature_matching import match_sift
from simai_ai.calibration.extrinsics.joint_ba_v2 import (
    _fit_essential_ransac, _normalize_pixels, _undistort_pixels, sampson, worsened,
)


def test_sift_homography_matches():
    rng = np.random.default_rng(41)
    image = rng.integers(0, 256, (480, 640), dtype=np.uint8)
    image = cv2.GaussianBlur(image, (3, 3), 0)
    H = np.array([[1., 0.01, 18.], [-0.01, 1., 12.], [0.00001, 0.00002, 1.]])
    warped = cv2.warpPerspective(image, H, (640, 480))
    matches = match_sift(image, warped)
    assert len(matches) > 100
    uv = np.array([m["uv"] for m in matches])
    predicted = cv2.perspectiveTransform(uv[:, 0, None, :], H)[:, 0, :]
    assert np.median(np.linalg.norm(predicted - uv[:, 1], axis=1)) < 0.5


def test_ransac_recovers_35_percent_inliers_and_is_deterministic():
    rng = np.random.default_rng(49)
    points = np.column_stack((rng.uniform(-2, 2, (200, 2)), rng.uniform(3, 8, 200)))
    a = points[:, :2] / points[:, 2:]
    shifted = points + [0.8, 0.05, 0.1]
    b = shifted[:, :2] / shifted[:, 2:]
    a += rng.normal(0, 0.00025, a.shape)
    b += rng.normal(0, 0.00025, b.shape)
    b[70:] = rng.uniform(-0.5, 0.5, (130, 2))
    E = _fit_essential_ransac(a, b, 0.001, seed=449)
    assert E is not None
    assert (sampson(E, a[:70], b[:70]) < 0.001).sum() >= 60
    np.testing.assert_array_equal(E, _fit_essential_ransac(a, b, 0.001, seed=449))


def test_holdout_tolerance_boundary():
    before = {"median": 2., "p90": 4.}
    assert not worsened(before, {"median": 3., "p90": 5.}, 1.)
    assert worsened(before, {"median": 3.001, "p90": 5.}, 1.)
    assert worsened(before, {"median": 2., "p90": None}, 1.)


def test_undistortion_improves_essential_consistency():
    rng = np.random.default_rng(37)
    points = np.column_stack((rng.uniform(-3, 3, (300, 2)), rng.uniform(3, 6, 300)))
    K = np.array([[500., 0, 320], [0, 500., 240], [0, 0, 1.]])
    coeffs = np.array([-0.29, 0.04, 0., 0., 0.])
    a, _ = cv2.projectPoints(points, np.zeros(3), np.zeros(3), K, coeffs)
    b, _ = cv2.projectPoints(points, np.zeros(3), np.array([0.8, 0.2, 0.1]), K, coeffs)
    a, b = a[:, 0], b[:, 0]
    distortion = {"model": "OPENCV_PINHOLE", "coeffs": coeffs.tolist()}
    raw_a, raw_b = _normalize_pixels(a, K), _normalize_pixels(b, K)
    clean_a = _normalize_pixels(_undistort_pixels(a, K, distortion), K)
    clean_b = _normalize_pixels(_undistort_pixels(b, K, distortion), K)
    # 동일한 실제 상대 이동에 대한 에피폴라 잔차로 비교한다.
    E = np.array([[0., -0.1, 0.2], [0.1, 0., -0.8], [-0.2, 0.8, 0.]])
    assert np.mean(sampson(E, clean_a, clean_b) < 0.002) > 0.95
    assert np.mean(sampson(E, clean_a, clean_b) < 0.002) > np.mean(sampson(E, raw_a, raw_b) < 0.002) + 0.2
