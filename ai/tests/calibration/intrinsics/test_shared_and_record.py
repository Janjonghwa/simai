"""같은 렌즈 4대 공유, 추정 기록(input_provenance), 번들 조정 초기값 넘김."""

from __future__ import annotations

import dataclasses
import json

import pytest

from simai_ai.calibration.intrinsics import (
    METHOD_SHARED_LENS,
    STATUS_FAILED,
    STATUS_NO_CONSENSUS,
    STATUS_NO_FOCAL,
    STATUS_OK,
    IntrinsicsEstimate,
    IntrinsicsUncertainty,
    build_intrinsics_record,
    combine_shared_lens,
    to_bundle_adjustment_init,
)
from simai_ai.calibration.intrinsics.estimate import opencv_from_radial_pixel

CAMERA_IDS = ("cam1", "cam2", "cam3", "cam4")
T0 = 1_760_342_400_000
FRAMES_SIM = {"source": "SIM", "dataset_id": "sim/basic-wide", "camera_ids": list(CAMERA_IDS)}


def test_four_cameras_share_one_lens(wide_case, intr):
    estimates = [wide_case.estimate(cid) for cid in CAMERA_IDS]
    shared = combine_shared_lens(estimates)
    gt = wide_case.cameras["cam1"]
    common = shared.common
    print(
        "SIM 상한 지표(shared):",
        json.dumps(
            {
                "focal_px": common.focal_px,
                "focal_gt_px": gt["K"][0][0],
                "coeffs": common.distortion_coeffs,
                "deviations": [d.to_record() for d in shared.deviations],
                "is_upper_bound": True,
            },
            ensure_ascii=False,
        ),
    )
    assert common.status == STATUS_OK and common.method == METHOD_SHARED_LENS
    assert abs(common.focal_px - gt["K"][0][0]) / gt["K"][0][0] < 0.005
    assert abs(common.distortion_coeffs[0] - intr.WIDE["coeffs"][0]) < 0.01
    assert shared.outlier_camera_ids == ()
    assert all(d.used_in_common and d.discrepancy_p95_px < 2.0 for d in shared.deviations)
    # 모든 카메라가 공통값을 초기값으로 받는다
    assert set(shared.per_camera_initial) == set(CAMERA_IDS)
    for cid, init in shared.per_camera_initial.items():
        assert init.camera_id == cid and init.focal_px == common.focal_px


def test_different_lens_camera_is_flagged(wide_case, narrow_case):
    """cam4 만 좁은 화각 렌즈면 이상치로 표시하고 공통값에서 뺀다."""
    estimates = [wide_case.estimate(cid) for cid in ("cam1", "cam2", "cam3")] + [narrow_case.estimate("cam4")]
    narrow_gt = narrow_case.cameras["cam4"]["K"][0][0]
    assert abs(estimates[3].focal_px - narrow_gt) / narrow_gt < 0.01  # 그 카메라 자체 추정은 맞다
    shared = combine_shared_lens(estimates)
    print("SIM 상한 지표(outlier):", json.dumps([d.to_record() for d in shared.deviations], ensure_ascii=False))
    assert shared.outlier_camera_ids == ("cam4",)
    dev = {d.camera_id: d for d in shared.deviations}
    assert not dev["cam4"].used_in_common and dev["cam4"].focal_rel_dev > 0.1
    gt = wide_case.cameras["cam1"]["K"][0][0]
    assert abs(shared.common.focal_px - gt) / gt < 0.005
    init = to_bundle_adjustment_init(shared, {"format": "test"})
    assert "OUTLIER_CAMERA" in init.camera("cam4").flags
    assert init.camera("cam4").focal_prior_std_px > init.camera("cam1").focal_prior_std_px


def _fake(camera_id, focal, b=(-0.67, 0.45), status=STATUS_OK, size=(1152, 648)):
    coeffs = opencv_from_radial_pixel(b, focal, size) if focal is not None else None
    return IntrinsicsEstimate(
        camera_id=camera_id,
        image_size=size,
        focal_px=focal,
        distortion_coeffs=coeffs,
        radial_pixel=b,
        uncertainty=IntrinsicsUncertainty(focal_std_px=0.5 if focal else None, straightness_rms_px=0.1, line_count=100),
        method="PLUMB_LINE_VP",
        method_version="0.1.0",
        status=status,
    )


def test_camera_without_focal_contributes_distortion_and_gets_initial_value():
    estimates = [
        _fake("cam1", 442.0),
        _fake("cam2", 441.0),
        _fake("cam3", 443.0),
        _fake("cam4", None, status=STATUS_NO_FOCAL),
    ]
    shared = combine_shared_lens(estimates)
    assert shared.common.focal_px == pytest.approx(442.0)
    assert shared.outlier_camera_ids == ()
    dev = {d.camera_id: d for d in shared.deviations}
    assert dev["cam4"].focal_rel_dev is None and dev["cam4"].used_in_common
    assert shared.per_camera_initial["cam4"].focal_px == pytest.approx(442.0)
    init = to_bundle_adjustment_init(shared, {})
    assert "OWN_ESTIMATE_NO_FOCAL" in init.camera("cam4").flags
    assert "PRINCIPAL_POINT_ASSUMED_CENTER" in init.camera("cam1").flags


def test_no_focal_anywhere_leaves_common_focal_empty():
    shared = combine_shared_lens(
        [_fake("cam1", None, status=STATUS_NO_FOCAL), _fake("cam2", None, status=STATUS_NO_FOCAL)]
    )
    assert shared.common.focal_px is None and shared.common.status == STATUS_NO_FOCAL
    assert shared.per_camera_initial == {}
    with pytest.raises(ValueError):
        to_bundle_adjustment_init(shared, {})


def test_mixed_image_sizes_are_rejected():
    with pytest.raises(ValueError):
        combine_shared_lens([_fake("cam1", 442.0), _fake("cam2", 884.0, size=(2304, 1296))])


def test_duplicate_camera_ids_are_rejected():
    with pytest.raises(ValueError):
        combine_shared_lens([_fake("cam1", 442.0), _fake("cam1", 442.0)])


def test_record_keeps_provenance_and_contract_parts(wide_case):
    estimates = [wide_case.estimate(cid) for cid in CAMERA_IDS]
    shared = combine_shared_lens(estimates)
    record = build_intrinsics_record(created_at=T0, frames=FRAMES_SIM, estimates=estimates, shared=shared)
    json.dumps(record)  # 직렬화 가능
    assert record["format"] == "simai.intrinsics_estimate"
    assert record["input_provenance"]["frames"]["source"] == "SIM"
    assert record["input_provenance"]["is_upper_bound"] is True
    assert "calibration" not in record["input_provenance"]  # 이미지에서 카메라를 스스로 추정
    assert "provider_source" not in record  # 보정 버전이 아니다
    for rec in record["estimates"]:
        assert rec["method"] == "PLUMB_LINE_VP"
        assert set(rec["camera"]) == {"camera_id", "image_size", "K", "distortion"}
        assert rec["camera"]["distortion"]["model"] == "OPENCV_PINHOLE"
    assert record["shared_lens"]["outlier_camera_ids"] == []
    init = to_bundle_adjustment_init(shared, record)
    assert [c.camera_id for c in init.cameras] == list(CAMERA_IDS)
    assert init.source_record["format"] == "simai.intrinsics_estimate"


def test_record_rejects_non_ms_time(wide_case):
    with pytest.raises(TypeError):
        build_intrinsics_record(created_at=1_760_342_400.5, frames=FRAMES_SIM, estimates=[wide_case.estimate("cam1")])
    with pytest.raises(ValueError):
        build_intrinsics_record(created_at=-1, frames=FRAMES_SIM, estimates=[wide_case.estimate("cam1")])


def test_live_frames_are_not_upper_bound():
    record = build_intrinsics_record(
        created_at=T0, frames={"source": "LIVE", "camera_ids": ["cam1"]}, estimates=[_fake("cam1", 442.0)]
    )
    assert record["input_provenance"]["is_upper_bound"] is False


def test_estimate_is_immutable():
    est = _fake("cam1", 442.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        est.focal_px = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "focals",
    [(442.0, 443.0, 650.0, 660.0), (442.0, 650.0), (442.0, 550.0, 650.0)],
    ids=["2v2", "1v1", "spread3"],
)
def test_no_majority_leaves_common_empty(focals):
    """다수가 맞는 값이 없으면 공통 초점·왜곡을 비우고 합의 없음으로 표시한다(번들 조정 초기값도 없음)."""
    estimates = [_fake(f"cam{i + 1}", f) for i, f in enumerate(focals)]
    shared = combine_shared_lens(estimates)
    assert shared.consensus is False
    assert shared.common.status == STATUS_NO_CONSENSUS
    assert shared.common.focal_px is None and shared.common.radial_pixel is None
    assert shared.per_camera_initial == {}
    assert all("다수 합의 없음" in d.reasons and not d.used_in_common for d in shared.deviations)
    with pytest.raises(ValueError, match="NO_CONSENSUS"):
        to_bundle_adjustment_init(shared, {})
    record = build_intrinsics_record(created_at=T0, frames=FRAMES_SIM, estimates=estimates, shared=shared)
    assert record["shared_lens"]["consensus"] is False and "camera" not in record["shared_lens"]["common"]


def test_majority_with_one_outlier_still_agrees():
    shared = combine_shared_lens([_fake("cam1", 442.0), _fake("cam2", 443.0), _fake("cam3", 650.0)])
    assert shared.consensus is True and shared.outlier_camera_ids == ("cam3",)
    assert shared.common.focal_px == pytest.approx(442.5)


def test_failed_camera_values_never_enter_common():
    """FAILED 카메라는 (값이 남아 있더라도) 공통값에 들어가지 않는다."""
    bad = IntrinsicsEstimate(
        camera_id="cam4",
        image_size=(1152, 648),
        focal_px=None,
        distortion_coeffs=None,
        radial_pixel=(0.9, -0.5),
        uncertainty=IntrinsicsUncertainty(),
        method="PLUMB_LINE_VP",
        method_version="0.1.0",
        status=STATUS_FAILED,
    )
    shared = combine_shared_lens([_fake("cam1", 442.0), _fake("cam2", 442.0), bad])
    assert shared.common.radial_pixel == pytest.approx((-0.67, 0.45))
    dev = {d.camera_id: d for d in shared.deviations}
    assert not dev["cam4"].used_in_common
    assert shared.consensus is True


FULL_SIZE = (2304, 1296)


def _scatter(spread, size=(1152, 648), base=442.0):
    """같은 렌즈 4대의 정상 산포: 초점을 ±spread, ±spread/3 으로 대칭으로 흩는다."""
    scale = size[0] / 1152
    factors = (1 - spread, 1 - spread / 3, 1 + spread / 3, 1 + spread)
    return [_fake(f"cam{i + 1}", base * scale * k, size=size) for i, k in enumerate(factors)]


@pytest.mark.parametrize("spread", [0.01, 0.02, 0.03])
@pytest.mark.parametrize("size", [(1152, 648), FULL_SIZE], ids=["half", "full"])
def test_normal_focal_scatter_is_consensus(spread, size):
    """단일 영상 추정의 정상 산포(초점 1~3%)는 합의로 보고 이상치도 없다. 해상도와 무관하다."""
    shared = combine_shared_lens(_scatter(spread, size))
    assert shared.consensus is True and shared.common.status == STATUS_OK
    assert shared.outlier_camera_ids == ()
    assert to_bundle_adjustment_init(shared, {}).cameras


def test_distortion_scatter_is_consensus():
    estimates = [_fake(f"cam{i + 1}", 442.0, b=(-0.67 + d, 0.45)) for i, d in enumerate((-0.03, -0.01, 0.01, 0.03))]
    shared = combine_shared_lens(estimates)
    assert shared.consensus is True and shared.outlier_camera_ids == ()


@pytest.mark.parametrize(
    "focals",
    [(442.0, 443.0, 650.0, 660.0), (442.0, 650.0), (442.0, 446.0, 450.0, 520.0), (437.6, 442.0, 446.4, 470.0)],
    ids=["2v2", "1v1", "3+1far", "3+1near"],
)
def test_decisions_do_not_depend_on_resolution(focals):
    """같은 장면을 1152x648 과 2304x1296 으로 보면(초점 2배, 픽셀 단위 왜곡 같음) 판정과 정규화 편차가 같다."""
    half = combine_shared_lens([_fake(f"cam{i + 1}", f) for i, f in enumerate(focals)])
    full = combine_shared_lens([_fake(f"cam{i + 1}", 2 * f, size=FULL_SIZE) for i, f in enumerate(focals)])
    assert half.consensus == full.consensus
    assert half.common.status == full.common.status
    assert half.outlier_camera_ids == full.outlier_camera_ids
    for a, b in zip(half.deviations, full.deviations, strict=True):
        # 격자 여백(2px)만 해상도에 따라 달라 1% 안쪽 차이가 남는다
        assert a.discrepancy_rel == pytest.approx(b.discrepancy_rel, rel=2e-2, abs=1e-6)


def test_normal_three_plus_one_different_lens_flags_only_that_one():
    """정상 3대(±1%) + 다른 렌즈 1대(약 18%): 그 1대만 이상치로 표시하고 합의는 유지한다."""
    estimates = [_fake("cam1", 437.6), _fake("cam2", 442.0), _fake("cam3", 446.4), _fake("cam4", 520.0)]
    shared = combine_shared_lens(estimates)
    assert shared.consensus is True
    assert shared.outlier_camera_ids == ("cam4",)
    assert shared.common.focal_px == pytest.approx(442.0)


def test_slightly_off_fourth_camera_is_not_no_consensus():
    """정상 3대(±1%) + 조금 벗어난 1대(약 6%)는 합의를 깨지 않는다(이상치 표시 여부와 무관)."""
    estimates = [_fake("cam1", 437.6), _fake("cam2", 442.0), _fake("cam3", 446.4), _fake("cam4", 470.0)]
    shared = combine_shared_lens(estimates)
    assert shared.consensus is True and shared.common.status == STATUS_OK
    assert set(shared.outlier_camera_ids) <= {"cam4"}


def test_unmeasurable_discrepancy_is_outlier_and_record_stays_strict_json():
    """공통 모델이 영상 안에서 접혀 재투영 차이를 잴 수 없으면(inf) 이상치로 보고, 기록에서는 값을 비운다 (-166)."""
    # ρ_d = ρ(1 − 0.6ρ²) 는 ρ ≈ 0.745 에서 접힌다(ρ_d 최대 ≈ 0.50 < 모서리 1): 모서리 픽셀은 광선이 없다
    estimates = [_fake(cid, 442.0, b=(-0.6, 0.0)) for cid in CAMERA_IDS]
    shared = combine_shared_lens(estimates)
    assert not shared.consensus
    for d in shared.deviations:
        assert d.discrepancy_p95_px == float("inf") and d.outlier
        rec = d.to_record()
        assert "discrepancy_p95_px" not in rec and "discrepancy_rel" not in rec
        assert any("잴 수 없음" in r for r in rec["reasons"])
    record = build_intrinsics_record(created_at=T0, frames=FRAMES_SIM, estimates=estimates, shared=shared)
    json.dumps(record, allow_nan=False)
