"""고전적 기준 백엔드(plumb-line + 소실점)를 시뮬레이터 카메라 합성 교실로 시험한다.

지표(모두 SIM 입력이므로 상한이다. 제품 성능으로 쓰지 않는다):
- 정답 대비 초점 상대 오차
- 직선성 잔차: 방의 직선을 정답 카메라로 투영한 점을 추정 모델로 편 뒤 직선 맞춤 RMS(검출기와 무관)
- 같은 광선 재투영 오차: 정답 모델로 되돌린 광선을 추정 모델로 다시 투영한 픽셀 차이(자세 고정, 엄격)
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from simai_ai.calibration import check_calibration_structure
from simai_ai.calibration.intrinsics import (
    STATUS_FAILED,
    STATUS_NO_FOCAL,
    STATUS_OK,
    PlumbLineConfig,
    PlumbLineVPEstimator,
    model_discrepancy_px,
)
from simai_ai.calibration.intrinsics.plumbline import monotonic_in_image

CAMERA_IDS = ("cam1", "cam2", "cam3", "cam4")


def _metrics(case, camera_id, intr):
    cam = case.cameras[camera_id]
    est = case.estimate(camera_id)
    gt_f = cam["K"][0][0]
    lines = intr.world_line_points(cam)
    disc = model_discrepancy_px(cam, est)
    return {
        "camera_id": camera_id,
        "status": est.status,
        "focal_px": est.focal_px,
        "focal_gt_px": gt_f,
        "focal_rel_err": abs(est.focal_px - gt_f) / gt_f,
        "focal_std_px": est.uncertainty.focal_std_px,
        "k1": est.distortion_coeffs[0],
        "k2": est.distortion_coeffs[1],
        "straightness_before_px": intr.straightness_rms_px(lines, None, None, None),
        "straightness_after_px": intr.straightness_rms_px(
            lines, est.focal_px, est.distortion_coeffs, est.principal_point
        ),
        "straightness_gt_px": intr.straightness_rms_px(
            lines, gt_f, cam["distortion"]["coeffs"][:2] or [0.0], est.principal_point
        ),
        "reprojection_median_px": disc.median_px,
        "reprojection_p95_px": disc.p95_px,
        "reprojection_max_px": disc.max_px,
        "detected_line_count": est.uncertainty.line_count,
        "is_upper_bound": True,
    }


@pytest.mark.parametrize("camera_id", CAMERA_IDS)
def test_wide_angle_distortion_recovered(wide_case, intr, camera_id):
    """강한 광각 배럴 왜곡(k1 −0.30, k2 0.09)에서 초점·왜곡을 되찾고 직선이 펴진다."""
    m = _metrics(wide_case, camera_id, intr)
    print("SIM 상한 지표(wide):", json.dumps(m, ensure_ascii=False))
    assert m["status"] == STATUS_OK
    assert m["focal_rel_err"] < 0.01
    assert abs(m["k1"] - intr.WIDE["coeffs"][0]) < 0.02
    assert abs(m["k2"] - intr.WIDE["coeffs"][1]) < 0.02
    # 왜곡을 무시하면 휘어 있던 직선(RMS 수 픽셀)이 0.1 픽셀 안쪽으로 펴진다
    assert m["straightness_before_px"] > 1.0
    assert m["straightness_after_px"] < 0.1
    assert m["reprojection_p95_px"] < 3.0
    est = wide_case.estimate(camera_id)
    assert est.uncertainty.focal_std_px is not None and est.uncertainty.focal_std_px < 0.01 * est.focal_px
    assert est.uncertainty.straightness_rms_px < est.uncertainty.straightness_rms_uncorrected_px
    assert est.principal_point_assumed


def test_barrel_with_tangential_is_close(barrel_case, intr):
    """약한 배럴 + 접선 왜곡: 접선은 모델링하지 않지만 초점·k1 은 가깝다(남는 차이는 번들 조정이 다듬는다)."""
    m = _metrics(barrel_case, "cam1", intr)
    print("SIM 상한 지표(barrel+tangential):", json.dumps(m, ensure_ascii=False))
    assert m["status"] == STATUS_OK
    assert m["focal_rel_err"] < 0.01
    assert abs(m["k1"] - intr.BARREL_TANGENTIAL["coeffs"][0]) < 0.02
    assert m["straightness_before_px"] > 0.5
    assert m["straightness_after_px"] < 0.1
    assert m["reprojection_p95_px"] < 6.0


def test_no_distortion_gives_near_zero_coefficients(pinhole_case, intr):
    m = _metrics(pinhole_case, "cam1", intr)
    print("SIM 상한 지표(no distortion):", json.dumps(m, ensure_ascii=False))
    assert m["status"] == STATUS_OK
    assert m["focal_rel_err"] < 0.01
    assert abs(m["k1"]) < 0.01 and abs(m["k2"]) < 0.01
    assert m["reprojection_p95_px"] < 2.0


def test_frontal_view_leaves_focal_empty(frontal_case):
    """정면·수평 카메라는 직교 소실점 쌍이 없어 초점을 비운다(지어내지 않는다). 왜곡은 픽셀 단위로 남는다."""
    est = frontal_case.estimate("front")
    assert est.status == STATUS_NO_FOCAL
    assert est.focal_px is None and est.distortion_coeffs is None
    assert est.radial_pixel is not None and est.radial_pixel[0] < 0  # 배럴
    with pytest.raises(ValueError):
        est.to_contract_camera()


def test_blank_image_fails_without_inventing_values():
    est = PlumbLineVPEstimator().estimate(np.full((120, 200), 0.5), camera_id="blank")
    assert est.status == STATUS_FAILED
    assert est.focal_px is None and est.radial_pixel is None


def test_deterministic(wide_case):
    img = wide_case.image("cam1")
    a = PlumbLineVPEstimator().estimate(img, camera_id="cam1")
    b = PlumbLineVPEstimator().estimate(img, camera_id="cam1")
    assert a == b


def test_estimate_fits_calibration_contract(wide_case):
    """추정한 내부 파라미터를 보정 문서의 카메라에 넣어도 계약 형식(스키마)을 지킨다."""
    doc = json.loads(json.dumps(wide_case.calibration))
    for cam in doc["cameras"]:
        part = wide_case.estimate(cam["camera_id"]).to_contract_camera()
        assert part["camera_id"] == cam["camera_id"]
        assert part["image_size"] == cam["image_size"]
        cam["K"] = part["K"]
        cam["distortion"] = part["distortion"]
    assert doc["provider_source"] == "SIM" and doc["state"] != "CALIBRATED"
    assert check_calibration_structure(doc) == []


def test_single_radial_coefficient_fails_safely_on_strong_distortion(wide_case):
    """k1 만으로는 강한 광각 왜곡을 펼 수 없다. 휜 직선을 모두 이상치로 빼는 퇴화 대신 실패로 둔다(지어내지 않음)."""
    est = PlumbLineVPEstimator(PlumbLineConfig(num_radial=1)).estimate(wide_case.image("cam1"), camera_id="cam1")
    assert est.status == STATUS_FAILED
    assert est.focal_px is None and est.radial_pixel is None
    assert any("펴지 못했다" in n for n in est.uncertainty.notes)


def test_single_radial_coefficient_on_mild_distortion(barrel_case):
    """약한 왜곡에서는 k1 만으로도 돈다(k2 = 0). 초점은 직교 세 방향이 맞을 때만 정한다."""
    est = PlumbLineVPEstimator(PlumbLineConfig(num_radial=1)).estimate(barrel_case.image("cam1"), camera_id="cam1")
    assert est.status == STATUS_NO_FOCAL  # 1계수 모델로는 소실점 세 방향이 맞지 않는다
    assert est.radial_pixel[1] == 0.0 and est.uncertainty.k2_std is None
    assert est.radial_pixel[0] < 0  # 배럴


def test_pair_only_focal_is_opt_in(barrel_case):
    """직교 세 방향이 확인되지 않으면 기본은 초점을 비우고, 설정을 켜야 쌍 하나로 정하며 메모에 남는다."""
    img = barrel_case.image("cam1")
    strict = PlumbLineVPEstimator(PlumbLineConfig(num_radial=1)).estimate(img, camera_id="cam1")
    loose = PlumbLineVPEstimator(PlumbLineConfig(num_radial=1, require_orthogonal_triple=False)).estimate(
        img, camera_id="cam1"
    )
    assert strict.status == STATUS_NO_FOCAL and strict.focal_px is None
    assert loose.status == STATUS_OK
    assert any("확인하지 못함" in n for n in loose.uncertainty.notes)


@pytest.mark.parametrize("camera_id", CAMERA_IDS)
def test_estimated_distortion_does_not_fold_inside_image(wide_case, camera_id):
    assert monotonic_in_image(wide_case.estimate(camera_id).radial_pixel)


def _segment(p0, p1, n=200):
    t = np.linspace(0.0, 1.0, n)[:, None]
    return np.asarray(p0, dtype=float) * (1 - t) + np.asarray(p1, dtype=float) * t


@pytest.mark.parametrize("seed", range(4))
def test_only_curves_leaves_distortion_empty(seed):
    """직선 없이 원호만 있으면 펴지지 않으므로 왜곡을 지어내지 않고 FAILED(왜곡도 비움)로 둔다."""
    rng = np.random.default_rng(10 + seed)
    chains = []
    for _ in range(20):
        c = rng.uniform([200, 150], [950, 500])
        r = rng.uniform(80, 200)
        a0 = rng.uniform(0, 2 * np.pi)
        a = np.linspace(a0, a0 + 1.5, 60)
        chains.append(np.column_stack([c[0] + r * np.cos(a), c[1] + r * np.sin(a)]))
    est = PlumbLineVPEstimator().estimate_from_chains(chains, image_size=(1152, 648), camera_id="arcs")
    assert est.status == STATUS_FAILED
    assert est.radial_pixel is None and est.focal_px is None
    assert est.uncertainty.straightness_rms_px > 1.0  # 편 뒤에도 휘어 있음이 기록된다


def test_non_orthogonal_vanishing_points_leave_focal_empty():
    """곧은 직선이 세 소실점으로 모이지만 세 방향이 서로 직교가 아니면(쌍별 초점이 다름) 초점을 비운다.

    직교 세 방향 요구를 끄면 쌍 하나로 초점을 내고 메모가 남는다(변별력 확인).
    """
    rng = np.random.default_rng(3)
    center = np.array([576.0, 324.0])
    vps = [center + (900.0, 100.0), center + (-400.0, 50.0), center + (0.0, -2000.0)]
    chains = []
    for v in vps:
        for _ in range(15):
            p0 = rng.uniform([100, 80], [1050, 560])
            d = (v - p0) / np.linalg.norm(v - p0)
            chains.append(_segment(p0, p0 + 200 * d))
    strict_est = PlumbLineVPEstimator()
    strict = strict_est.estimate_from_chains(chains, image_size=(1152, 648), camera_id="nonortho")
    assert strict.status == STATUS_NO_FOCAL and strict.focal_px is None
    assert strict.uncertainty.vanishing_point_count >= 3
    assert len(strict_est.last_diagnostics.pair_focals) >= 2  # 쌍별 초점은 있었지만 서로 맞지 않았다
    loose = PlumbLineVPEstimator(PlumbLineConfig(require_orthogonal_triple=False)).estimate_from_chains(
        chains, image_size=(1152, 648), camera_id="nonortho"
    )
    assert loose.status == STATUS_OK
    assert any("확인하지 못함" in n for n in loose.uncertainty.notes)
