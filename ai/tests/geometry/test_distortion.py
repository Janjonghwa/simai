"""렌즈 왜곡 제거 시험 (명세 4.3, 5.2). S15P31A101-166.

시뮬레이터 기본 카메라(Tapo C110 stream1, 2304x1296, 가로 화각 1.832596 rad, `sim/camera.py` 의 K 식)에 강한 광각
배럴 왜곡을 넣고, 영상 전체 격자(모서리 포함)를 왜곡 제거한 뒤 다시 왜곡해 원래 픽셀로 돌아오는지 본다.
왜곡 식이 접히는 영역과 풀 수 없는 입력은 값을 지어내지 않고 NaN(+ 실패 마스크)이어야 한다.
"""

import math

import numpy as np
import pytest

from simai_ai.calibration.intrinsics import model_discrepancy_px
from simai_ai.calibration.intrinsics.estimate import undistort_normalized, undistort_radii
from simai_ai.geometry import distortion
from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.geometry.rig import RigCamera

C110_SIZE = (2304, 1296)
C110_HFOV_RAD = 1.832596
C110_F = (C110_SIZE[0] / 2) / math.tan(C110_HFOV_RAD / 2)  # sim/camera.py build() 와 같은 식 (≈ 884 px)

WIDE_PINHOLE = [
    [-0.30, 0.09, 0.0, 0.0, 0.0],  # -116 에서 고정점 반복이 모서리에서 수백 px 어긋난 계수
    [-0.30, 0.09, 0.001, -0.0005, -0.004],  # 접선·k3 포함
    [-0.30, 0.09, 0.001, -0.0005],  # 4계수
    [-0.25, 0.06, 0.0005, 0.0003, -0.002, 0.05, 0.0, 0.0],  # 유리 모델 8계수
]


def c110(model="OPENCV_PINHOLE", coeffs=(-0.30, 0.09, 0.0, 0.0, 0.0), tol_px=0.01):
    w, h = C110_SIZE
    doc = {
        "camera_id": "cam1",
        "image_size": [w, h],
        "K": [[C110_F, 0.0, w / 2], [0.0, C110_F, h / 2], [0.0, 0.0, 1.0]],
        "distortion": {"model": model, "coeffs": list(coeffs)},
        "R": np.eye(3).tolist(),
        "t": [0.0, 0.0, 0.0],
    }
    return RigCamera.from_calibration(doc, undistort_tolerance_px=tol_px)


def image_grid(step=16.0):
    w, h = C110_SIZE
    us = np.unique(np.append(np.arange(0.0, w, step), [w - 1.0, w - 0.5]))
    vs = np.unique(np.append(np.arange(0.0, h, step), [h - 1.0, h - 0.5]))
    gu, gv = np.meshgrid(us, vs)
    return np.column_stack([gu.ravel(), gv.ravel()])


def redistort(cam, xn):
    return cam.ideal_pixels(distortion.distort(xn, cam.distortion_model, cam.distortion_coeffs))


@pytest.mark.parametrize("coeffs", WIDE_PINHOLE)
def test_c110_wide_pinhole_round_trip_over_whole_image(coeffs):
    cam = c110(coeffs=coeffs)
    uv = image_grid()
    xn, ok = cam.normalize_with_mask(uv)
    assert ok.all()
    err = np.hypot(*(redistort(cam, xn) - uv).T)
    assert err.max() < 1e-6
    # 모서리 픽셀은 정규화 반지름이 크게 펴진다(배럴 왜곡): 왜곡 반지름 ≈ 1.49, 펴진 반지름 ≈ 1.69
    corner = cam.normalize(np.array([[0.0, 0.0]]))[0]
    assert np.hypot(*corner) > 1.6


@pytest.mark.parametrize("coeffs", WIDE_PINHOLE[:2])
def test_c110_rays_match_projection_of_world_points(coeffs):
    # 카메라 앞 점을 투영(왜곡 포함)한 픽셀을 되돌리면 같은 광선이어야 한다. 화각 끝까지 덮는 점들
    cam = c110(coeffs=coeffs)
    rng = np.random.default_rng(0)
    xn_true = rng.uniform([-1.75, -1.0], [1.75, 1.0], size=(4000, 2))
    uv = redistort(cam, xn_true)
    inside = (uv[:, 0] >= 0) & (uv[:, 0] <= C110_SIZE[0] - 1) & (uv[:, 1] >= 0) & (uv[:, 1] <= C110_SIZE[1] - 1)
    assert inside.sum() > 2000
    xn, ok = cam.normalize_with_mask(uv[inside])
    assert ok.all()
    np.testing.assert_allclose(xn, xn_true[inside], atol=1e-9)


def test_folding_region_is_nan_not_invented():
    # k1 = −0.30 만 있으면 r_u = 1/√0.9 ≈ 1.054 에서 g'(r) = 1 − 0.9 r² = 0 으로 접히고 g 최대 ≈ 0.703.
    # C110 모서리(왜곡 반지름 ≈ 1.49)는 대응하는 광선이 없다
    cam = c110(coeffs=(-0.30, 0.0, 0.0, 0.0, 0.0))
    branch = distortion.radial_branch("OPENCV_PINHOLE", cam.distortion_coeffs)
    # 경계는 도함수가 MIN_SLOPE(1e-6) 이하가 되는 곳이라 해석해보다 아주 조금 안쪽이다
    assert branch.r_max == pytest.approx(1 / math.sqrt(0.9), rel=1e-5)
    assert branch.r_max < 1 / math.sqrt(0.9)
    assert branch.rd_max == pytest.approx(2 / 3 / math.sqrt(0.9), rel=1e-9)
    uv = image_grid(step=32.0)
    xn, ok = cam.normalize_with_mask(uv)
    rd = np.hypot(*((uv - [C110_SIZE[0] / 2, C110_SIZE[1] / 2]) / C110_F).T)
    assert not ok[rd > branch.rd_max].any()
    assert np.isnan(xn[~ok]).all()
    inner = rd < 0.99 * branch.rd_max
    assert ok[inner].all()
    err = np.hypot(*(redistort(cam, xn[inner]) - uv[inner]).T)
    assert err.max() < 1e-6
    # 모서리 픽셀: 이전 고정점 반복은 여기서 값을 냈다
    assert np.isnan(cam.normalize(np.array([[0.0, 0.0], [2303.0, 1295.0]]))).all()


def test_root_beyond_the_first_fold_is_not_used():
    # g'(r) = 1 − 1.5 r² + 0.5 r⁴ → r = 1 에서 접히고(g = 0.6) r = √2 뒤에서 다시 증가한다.
    # 왜곡 반지름 0.65 는 첫 가지 밖이다. 뒤쪽 가지(r ≈ 1.68)에 해가 있어도 쓰지 않는다
    coeffs = (-0.5, 0.1, 0.0, 0.0, 0.0)
    xn, ok = distortion.undistort(np.array([[0.65, 0.0], [0.5, 0.0]]), "OPENCV_PINHOLE", coeffs)
    assert ok.tolist() == [False, True]
    assert np.isnan(xn[0]).all()
    assert distortion.distort(xn[1:], "OPENCV_PINHOLE", coeffs)[0] == pytest.approx([0.5, 0.0], abs=1e-12)
    assert xn[1, 0] < 1.0


def test_rational_denominator_pole_bounds_the_branch():
    # 분모 1 − 0.5 r² 가 r = √2 에서 0 이 된다. 그 앞에서 g 는 단조 증가해 무한대로 가므로 가지 끝은 극점이다
    coeffs = (0.0, 0.0, 0.0, 0.0, 0.0, -0.5, 0.0, 0.0)
    branch = distortion.radial_branch("OPENCV_PINHOLE", coeffs)
    assert branch.r_max < math.sqrt(2)
    xn, ok = distortion.undistort(np.array([[3.0, 0.0], [50.0, 0.0]]), "OPENCV_PINHOLE", coeffs)
    assert ok.all()
    assert np.hypot(*xn.T).max() < math.sqrt(2)


@pytest.mark.parametrize(
    "coeffs",
    [
        (-0.02, 0.003, -0.001, 0.0002),
        (0.08, -0.02, 0.004, -0.0003),
        (0.0, 0.0, 0.0, 0.0),
    ],
)
def test_fisheye_round_trip_over_whole_image(coeffs):
    cam = c110("OPENCV_FISHEYE", coeffs)
    uv = image_grid()
    xn, ok = cam.normalize_with_mask(uv)
    assert ok.all()
    err = np.hypot(*(redistort(cam, xn) - uv).T)
    assert err.max() < 1e-6


def test_fisheye_folding_and_beyond_90_degrees_are_nan():
    # k1 = −0.5: θ_d' = 1 − 1.5 θ² → θ ≈ 0.816 에서 접히고 θ_d 최대 ≈ 0.544
    xn, ok = distortion.undistort(np.array([[0.8, 0.0], [0.4, 0.0]]), "OPENCV_FISHEYE", (-0.5, 0.0, 0.0, 0.0))
    assert ok.tolist() == [False, True]
    assert np.isnan(xn[0]).all()
    # 계수 0(등거리): θ_d = θ. θ_d ≥ π/2 는 핀홀 정규화 좌표로 나타낼 수 없다(이전 구현은 π/2 로 잘라 값을 냈다)
    xn, ok = distortion.undistort(np.array([[1.6, 0.0], [1.5, 0.0]]), "OPENCV_FISHEYE", (0.0, 0.0, 0.0, 0.0))
    assert ok.tolist() == [False, True]
    assert xn[1, 0] == pytest.approx(math.tan(1.5))


@pytest.mark.parametrize("model,coeffs", [("NONE", ()), ("OPENCV_PINHOLE", (-0.3, 0.09, 0.0, 0.0, 0.0))])
def test_non_finite_pixels_are_nan_with_mask(model, coeffs):
    cam = c110(model, coeffs)
    uv = np.array([[np.nan, 100.0], [100.0, np.inf], [-np.inf, 0.0], [1152.0, 648.0]])
    xn, ok = cam.normalize_with_mask(uv)
    assert ok.tolist() == [False, False, False, True]
    assert np.isnan(xn[:3]).all()
    assert xn[3] == pytest.approx([0.0, 0.0])


def test_round_trip_tolerance_is_configurable():
    with pytest.raises(ValueError):
        c110(tol_px=0.0)
    # 아주 엄격한 허용치도 수렴한 점은 통과한다(왕복 오차는 1e-9 px 수준)
    cam = c110(tol_px=1e-6)
    assert cam.normalize_with_mask(image_grid(step=64.0))[1].all()
    pinhole = PinholeCamera(
        K=((C110_F, 0.0, 1152.0), (0.0, C110_F, 648.0), (0.0, 0.0, 1.0)),
        R=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        t=(0.0, 0.0, 0.0),
    )
    with pytest.raises(ValueError, match="cam9"):
        RigCamera("cam9", pinhole, C110_SIZE, "OPENCV_PINHOLE", (0.1, 0.2, 0.3))


def test_calibration_helpers_share_the_geometry_solver():
    # calibration.intrinsics 의 뉴턴법 함수는 geometry.distortion 을 그대로 쓴다: 같은 값, 같은 NaN
    coeffs = (-0.30, 0.09, 0.0, 0.0, 0.0)
    cam = c110(coeffs=coeffs)
    uv = image_grid(step=48.0)
    xd = (uv - [C110_SIZE[0] / 2, C110_SIZE[1] / 2]) / C110_F
    np.testing.assert_allclose(undistort_normalized(xd, coeffs), cam.normalize(uv), atol=1e-12)
    folded = undistort_normalized(np.array([[1.4, 0.4]]), (-0.30, 0.0, 0.0, 0.0, 0.0))
    assert np.isnan(folded).all()
    r, ok = undistort_radii(np.array([0.2, 0.9]), -0.30, 0.0)
    assert not ok and np.isnan(r[1]) and np.isfinite(r[0])
    # 기준 모델이 영상 안에서 접히면 같은 모델이라도 차이를 0 으로 보지 않는다(inf)
    folded_cam = {
        "image_size": list(C110_SIZE),
        "K": [[C110_F, 0.0, 1152.0], [0.0, C110_F, 648.0], [0.0, 0.0, 1.0]],
        "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [-0.30, 0.0, 0.0, 0.0, 0.0]},
    }
    assert model_discrepancy_px(folded_cam, folded_cam).p95_px == math.inf


# --------------------------------------------------------------------------- 표본 사이의 좁은 접힘 (리뷰 F1)


def test_narrow_pinhole_fold_between_samples_is_found():
    # g'(r) 이 r ≈ 1.0029~1.0049 에서만 잠깐 음수다(최소 ≈ −4e-6). 고정 간격 표본으로는 놓쳐 뒤쪽 가지의 해를 받았다
    coeffs = (-0.6614881469980468, 0.19690417438062005, 0.0, 0.0, 0.0)
    branch = distortion.radial_branch("OPENCV_PINHOLE", coeffs)
    assert branch.r_max == pytest.approx(1.0027891236310043, abs=1e-9)
    assert branch.rd_max == pytest.approx(0.53541609, abs=1e-7)
    xn, ok = distortion.undistort(np.array([[0.6, 0.0], [0.5, 0.0]]), "OPENCV_PINHOLE", coeffs)
    assert ok.tolist() == [False, True]
    assert np.isnan(xn[0]).all()
    # 공개 경로(C110 K)에서도 영상 안 그 픽셀은 비운다(왕복 오차 0 이라 잔차 검사로는 막을 수 없었다)
    cam = c110(coeffs=coeffs)
    xn, ok = cam.normalize_with_mask(np.array([[1682.37625738, 648.0]]))
    assert not ok[0] and np.isnan(xn).all()


def test_narrow_fisheye_fold_between_samples_is_found():
    coeffs = (-7.058297211704596, 22.4187804257596, 0.0, 0.0)
    branch = distortion.radial_branch("OPENCV_FISHEYE", coeffs)
    assert branch.rd_max == pytest.approx(0.163909, abs=1e-6)
    xn, ok = distortion.undistort(np.array([[0.2, 0.0], [0.15, 0.0]]), "OPENCV_FISHEYE", coeffs)
    assert ok.tolist() == [False, True]
    assert np.isnan(xn[0]).all()


def _first_bad_fine(model, coeffs, cap, n=400_001):
    """아주 촘촘한 표본에서 처음 단조 조건이 깨지는 r (없으면 inf). 독립 검산용."""
    r = np.linspace(0.0, cap, n)
    k = list(coeffs) + [0.0] * (8 - len(coeffs))
    if model == "OPENCV_PINHOLE":
        k1, k2, _, _, k3, k4, k5, k6 = k
        s = r * r
        num, den = 1 + k1 * s + k2 * s**2 + k3 * s**3, 1 + k4 * s + k5 * s**2 + k6 * s**3
        dnum, dden = k1 + 2 * k2 * s + 3 * k3 * s**2, k4 + 2 * k5 * s + 3 * k6 * s**2
        slope = (num * den + 2 * s * (dnum * den - num * dden)) / den**2
        bad = (den <= distortion.MIN_SLOPE) | (slope <= distortion.MIN_SLOPE)
    else:
        k1, k2, k3, k4 = k[:4]
        t2 = r * r
        bad = 1 + 3 * k1 * t2 + 5 * k2 * t2**2 + 7 * k3 * t2**3 + 9 * k4 * t2**4 <= distortion.MIN_SLOPE
    return float(r[np.argmax(bad)]) if bad.any() else math.inf


def test_branch_never_extends_past_a_fold_random_models():
    # 무작위 계수(접힘 있는 것·없는 것 섞임)에서 가지 끝이 촘촘한 독립 검산의 첫 접힘을 넘지 않고, 너무 이르지도 않다
    rng = np.random.default_rng(166)
    for _ in range(150):
        k1, k2, k3 = rng.uniform(-1.0, 1.0, 3) * [0.8, 0.5, 0.1]
        rational = rng.uniform(-0.3, 0.3, 3) * rng.integers(0, 2)  # 절반은 유리 모델
        coeffs = (k1, k2, 0.0, 0.0, k3, *rational)
        fine = _first_bad_fine("OPENCV_PINHOLE", coeffs, distortion.PINHOLE_MAX_RADIUS)
        r_max = distortion.radial_branch("OPENCV_PINHOLE", coeffs).r_max
        assert r_max <= fine + 1e-9
        assert r_max >= min(fine, distortion.PINHOLE_MAX_RADIUS) - 3e-5
    for _ in range(150):
        coeffs = tuple(rng.uniform(-1.0, 1.0, 4) * [2.0, 3.0, 1.0, 0.5])
        fine = _first_bad_fine("OPENCV_FISHEYE", coeffs, distortion.FISHEYE_MAX_THETA)
        r_max = distortion.radial_branch("OPENCV_FISHEYE", coeffs).r_max
        assert r_max <= fine + 1e-9
        assert r_max >= min(fine, distortion.FISHEYE_MAX_THETA) - 5e-6


# --------------------------------------------------------------------------- OpenCV 교차 검증 (설치돼 있을 때만)


def test_matches_opencv_where_opencv_converges():
    cv2 = pytest.importorskip("cv2")
    criteria = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 500, 1e-14)
    K = np.array([[C110_F, 0.0, 1152.0], [0.0, C110_F, 648.0], [0.0, 0.0, 1.0]])
    rng = np.random.default_rng(7)
    xn_true = rng.uniform([-0.8, -0.5], [0.8, 0.5], size=(500, 2))  # 고정점 반복이 수렴하는 가운데 영역
    for coeffs in ([-0.30, 0.09, 0.001, -0.0005, -0.004], [-0.12, 0.02, 0.0005, -0.0003, 0.0]):
        D = np.array(coeffs, dtype=float)
        uv_cv, _ = cv2.projectPoints(np.column_stack([xn_true, np.ones(len(xn_true))]), np.zeros(3), np.zeros(3), K, D)
        cam = c110(coeffs=coeffs)
        np.testing.assert_allclose(redistort(cam, xn_true), uv_cv.reshape(-1, 2), atol=1e-6)
        xn_cv = cv2.undistortPoints(uv_cv, K, D, R=None, P=None, criteria=criteria).reshape(-1, 2)
        np.testing.assert_allclose(cam.normalize(uv_cv.reshape(-1, 2)), xn_cv, atol=1e-8)
    fisheye = np.array([-0.02, 0.003, -0.001, 0.0002])
    uv_cv, _ = cv2.fisheye.projectPoints(
        np.column_stack([xn_true, np.ones(len(xn_true))]).reshape(-1, 1, 3), np.zeros(3), np.zeros(3), K, fisheye
    )
    cam = c110("OPENCV_FISHEYE", tuple(fisheye))
    xn_cv = cv2.fisheye.undistortPoints(uv_cv, K, fisheye, criteria=criteria).reshape(-1, 2)
    np.testing.assert_allclose(cam.normalize(uv_cv.reshape(-1, 2)), xn_cv, atol=1e-8)
