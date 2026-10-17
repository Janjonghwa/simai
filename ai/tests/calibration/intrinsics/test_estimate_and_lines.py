"""카메라 모델 계산, 가중치 참조, 결과 형식, 에지 사슬 검출."""

from __future__ import annotations

import hashlib
import math

import numpy as np
import pytest

from simai_ai.calibration.intrinsics import (
    STATUS_OK,
    IntrinsicsEstimate,
    IntrinsicsEstimator,
    IntrinsicsUncertainty,
    PlumbLineVPEstimator,
    WeightsMismatchError,
    WeightsRef,
    detect_edge_chains,
    model_discrepancy_px,
)
from simai_ai.calibration.intrinsics.estimate import (
    distort_normalized,
    opencv_from_radial_pixel,
    radial_pixel_from_opencv,
    undistort_normalized,
    undistort_pixels,
)


def test_radial_pixel_and_opencv_are_the_same_model():
    size = (2304, 1296)
    f = 884.0
    coeffs = (-0.3, 0.09, 0.0, 0.0, 0.0)
    b = radial_pixel_from_opencv(coeffs, f, size)
    assert opencv_from_radial_pixel(b, f, size) == pytest.approx(coeffs)
    # 같은 픽셀을 두 표현으로 펴면 같은 곳으로 간다
    D = math.hypot(size[0] / 2, size[1] / 2)
    uv = np.array([[2000.0, 1200.0], [1500.0, 700.0]])
    xn = undistort_pixels(uv, f, coeffs, (size[0] / 2, size[1] / 2))
    rho_d = np.hypot(*((uv - [size[0] / 2, size[1] / 2]) / D).T)
    rho_u = np.hypot(*(xn * f / D).T)
    assert rho_d == pytest.approx(rho_u * (1 + b[0] * rho_u**2 + b[1] * rho_u**4))


@pytest.mark.parametrize(
    "coeffs",
    [
        (-0.3, 0.09, 0.0, 0.0, 0.0),
        (-0.12, 0.02, 0.0005, -0.0003, 0.0),
        (0.05, 0.0, 0.0, 0.0, 0.0),
        (-0.2, 0.05, 0.0, 0.0, -0.005),
    ],
)
def test_undistort_inverts_distort_up_to_wide_corners(coeffs):
    # C110 절반 해상도 모서리까지(정규화 반지름 ~1.75)
    xn = np.array([[0.0, 0.0], [0.5, -0.3], [1.3, 0.73], [-1.5, 0.8], [1.6, 0.0]])
    back = undistort_normalized(distort_normalized(xn, coeffs), coeffs)
    assert np.abs(back - xn).max() < 1e-9


def test_model_discrepancy_zero_for_same_model_and_scales_with_focal():
    cam = {
        "image_size": [1152, 648],
        "K": [[442.0, 0.0, 576.0], [0.0, 442.0, 324.0], [0.0, 0.0, 1.0]],
        "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [-0.3, 0.09, 0.0, 0.0, 0.0]},
    }
    assert model_discrepancy_px(cam, cam).max_px < 1e-6
    other = {**cam, "K": [[446.42, 0.0, 576.0], [0.0, 446.42, 324.0], [0.0, 0.0, 1.0]]}
    d = model_discrepancy_px(cam, other)
    assert 1.0 < d.p95_px < 20.0
    with pytest.raises(ValueError):
        model_discrepancy_px(cam, {**cam, "image_size": [2304, 1296]})


def test_weights_ref_checks_hash(tmp_path):
    blob = tmp_path / "w.pt"
    blob.write_bytes(b"not real weights")
    digest = "sha256:" + hashlib.sha256(b"not real weights").hexdigest()
    assert WeightsRef(str(blob), digest).verify() == blob
    with pytest.raises(WeightsMismatchError):
        WeightsRef(str(blob), "sha256:" + "0" * 64).verify()
    with pytest.raises(FileNotFoundError):
        WeightsRef(str(tmp_path / "missing.pt"), digest).verify()
    with pytest.raises(ValueError):
        WeightsRef(str(blob), "md5:abc")


def test_estimate_validation_and_contract_part():
    unc = IntrinsicsUncertainty()
    est = IntrinsicsEstimate(
        "cam1", (1152, 648), 442.0, (-0.3, 0.09, 0.0, 0.0, 0.0), (-0.67, 0.45), unc, "X", "0", STATUS_OK
    )
    assert est.principal_point == (576.0, 324.0)
    part = est.to_contract_camera()
    assert part["K"] == [[442.0, 0.0, 576.0], [0.0, 442.0, 324.0], [0.0, 0.0, 1.0]]
    assert part["distortion"] == {"model": "OPENCV_PINHOLE", "coeffs": [-0.3, 0.09, 0.0, 0.0, 0.0]}
    with pytest.raises(ValueError):
        IntrinsicsEstimate("cam1", (1152, 648), None, None, None, unc, "X", "0", STATUS_OK)
    with pytest.raises(ValueError):
        IntrinsicsEstimate("cam1", (1152, 648), -1.0, None, None, unc, "X", "0", "NO_FOCAL")


def test_plumb_line_estimator_satisfies_protocol():
    assert isinstance(PlumbLineVPEstimator(), IntrinsicsEstimator)


def _line_image(h=200, w=320, a=0.3, c=100.0):
    """직선 v = a·(u − 160) + c 아래쪽이 어두운 반평면 (경계를 부드럽게)."""
    vv, uu = np.mgrid[0:h, 0:w].astype(float)
    signed = (vv - (a * (uu - 160) + c)) / math.hypot(1, a)
    return np.clip(0.5 - signed, 0, 1) * 0.6 + 0.2


def test_edge_chain_is_subpixel_on_a_straight_edge():
    img = _line_image()
    chains = detect_edge_chains(img)
    assert len(chains) == 1
    uv = chains[0]
    dist = (uv[:, 1] - (0.3 * (uv[:, 0] - 160) + 100.0)) / math.hypot(1, 0.3)
    assert np.sqrt(np.mean(dist**2)) < 0.1
    assert uv[:, 0].max() - uv[:, 0].min() > 250


def test_corner_is_split_into_two_chains():
    h, w = 200, 300
    img = np.full((h, w), 0.8)
    img[60:, 80:] = 0.2  # L 자 경계(가로·세로)
    chains = detect_edge_chains(img)
    assert len(chains) == 2


def test_downsample_returns_original_coordinates():
    img = _line_image(400, 640, c=200.0)
    chains = detect_edge_chains(
        img,
        config=__import__("simai_ai.calibration.intrinsics.lines", fromlist=["EdgeChainConfig"]).EdgeChainConfig(
            downsample=2
        ),
    )
    assert len(chains) >= 1
    uv = np.concatenate(chains)
    dist = (uv[:, 1] - (0.3 * (uv[:, 0] - 160) + 200.0)) / math.hypot(1, 0.3)
    assert np.abs(dist).mean() < 0.5
