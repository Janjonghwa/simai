"""학습 모델 백엔드(GeoCalib·AnyCalib) 연결 코드. 실제 추론은 패키지·가중치가 있을 때만 돈다(없으면 건너뜀).

실제 추론 시험을 돌리려면 torch·geocalib(또는 anycalib)를 설치하고 환경 변수로 가중치 경로·해시를 준다:
SIMAI_GEOCALIB_WEIGHTS, SIMAI_GEOCALIB_SHA256 / SIMAI_ANYCALIB_WEIGHTS, SIMAI_ANYCALIB_SHA256.
"""

from __future__ import annotations

import builtins
import json
import os

import pytest

from simai_ai.calibration.intrinsics import (
    METHOD_ANYCALIB,
    METHOD_GEOCALIB,
    STATUS_FAILED,
    AnyCalibEstimator,
    GeoCalibEstimator,
    IntrinsicsEstimator,
    LearnedBackendUnavailable,
    ModelRef,
    WeightsRef,
    model_discrepancy_px,
)
from simai_ai.calibration.intrinsics.learned import learned_estimate, parse_anycalib_params

FAKE_WEIGHTS = WeightsRef("/workspace/simai/models/geocalib/geocalib-distorted.tar", "sha256:" + "a" * 64)


def _block_import(monkeypatch, names):
    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name.split(".")[0] in names:
            raise ImportError(f"blocked {name}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_missing_dependency_raises_clear_error(monkeypatch):
    _block_import(monkeypatch, {"torch", "geocalib", "anycalib"})
    geo = GeoCalibEstimator(FAKE_WEIGHTS)  # 만들 때는 깨지지 않는다
    assert isinstance(geo, IntrinsicsEstimator)
    with pytest.raises(LearnedBackendUnavailable, match="git\\+https://github.com/cvg/GeoCalib"):
        geo.estimate(None, camera_id="cam1")  # type: ignore[arg-type]
    any_ = AnyCalibEstimator(FAKE_WEIGHTS)
    with pytest.raises(LearnedBackendUnavailable, match="AnyCalib"):
        any_.estimate(None, camera_id="cam1")  # type: ignore[arg-type]


def test_unsupported_camera_models_are_rejected():
    with pytest.raises(ValueError):
        GeoCalibEstimator(FAKE_WEIGHTS, camera_model="simple_divisional")
    with pytest.raises(ValueError):
        AnyCalibEstimator(FAKE_WEIGHTS, cam_id="kb:4")


def test_anycalib_parameter_order():
    assert parse_anycalib_params("simple_radial:2", [440.0, 576.0, 324.0, -0.3, 0.09]) == (
        440.0,
        440.0,
        576.0,
        324.0,
        [-0.3, 0.09],
    )
    assert parse_anycalib_params("radial:2", [440.0, 444.0, 570.0, 320.0, -0.3, 0.09]) == (
        440.0,
        444.0,
        570.0,
        320.0,
        [-0.3, 0.09],
    )
    assert parse_anycalib_params("pinhole", [440.0, 441.0, 576.0, 324.0])[4] == []
    with pytest.raises(ValueError):
        parse_anycalib_params("radial:2", [1.0, 2.0])


def test_learned_output_becomes_contract_intrinsics():
    model = ModelRef("GeoCalib", "1.0", "distorted", FAKE_WEIGHTS, "Apache-2.0", "CC-BY-4.0")
    est = learned_estimate(
        camera_id="cam1",
        image_size=(1152, 648),
        fx=440.0,
        fy=450.0,
        cx=576.0,
        cy=324.0,
        radial=[-0.3],
        focal_std_px=12.0,
        method=METHOD_GEOCALIB,
        model=model,
        principal_point_assumed=True,
    )
    assert est.focal_px == 445.0
    assert est.distortion_coeffs == (-0.3, 0.0, 0.0, 0.0, 0.0)
    assert any("평균" in n for n in est.uncertainty.notes)
    part = est.to_contract_camera()
    assert part["distortion"]["model"] == "OPENCV_PINHOLE"
    rec = est.model.to_record()
    assert rec["weights"]["sha256"] == FAKE_WEIGHTS.sha256 and rec["license_weights"] == "CC-BY-4.0"


@pytest.mark.parametrize(
    "overrides",
    [
        {"fx": float("nan")},
        {"radial": [float("nan"), 0.09]},  # 초점은 정상이어도 왜곡이 NaN 이면 실패
        {"fx": -1.0},
        {"success": False},
        {"fx": 50.0, "fy": 50.0},  # 너비 1152 의 0.15 배 미만
        {"fx": 9000.0, "fy": 9000.0},  # 너비의 6 배 초과
    ],
)
def test_bad_learned_output_is_failed_and_empty(overrides):
    """모델 실패·이상값은 값을 비운 FAILED 로 낸다(공유 단계에 들어가지 않는다)."""
    model = ModelRef("AnyCalib", "1.0", "anycalib_dist", FAKE_WEIGHTS, "Apache-2.0", "Apache-2.0")
    kwargs = {
        "camera_id": "cam1",
        "image_size": (1152, 648),
        "fx": 440.0,
        "fy": 440.0,
        "cx": 576.0,
        "cy": 324.0,
        "radial": [-0.3, 0.09],
        "focal_std_px": None,
        "method": METHOD_ANYCALIB,
        "model": model,
        "principal_point_assumed": False,
    }
    kwargs.update(overrides)
    est = learned_estimate(**kwargs)
    assert est.status == STATUS_FAILED
    assert est.focal_px is None and est.distortion_coeffs is None and est.radial_pixel is None
    assert est.model is model


def _weights_from_env(prefix):
    path = os.environ.get(f"{prefix}_WEIGHTS")
    digest = os.environ.get(f"{prefix}_SHA256")
    if not path or not digest:
        pytest.skip(f"{prefix}_WEIGHTS / {prefix}_SHA256 가 없다(가중치는 커밋하지 않는다)")
    return WeightsRef(path, digest)


def test_geocalib_on_synthetic_room(wide_case):
    pytest.importorskip("torch")
    pytest.importorskip("geocalib")
    weights = _weights_from_env("SIMAI_GEOCALIB")
    img = (wide_case.image("cam1").clip(0, 1) * 255).astype("uint8")
    est = GeoCalibEstimator(weights).estimate(img, camera_id="cam1")
    cam = wide_case.cameras["cam1"]
    print(
        "SIM 상한 지표(GeoCalib):",
        json.dumps(
            {
                "focal_px": est.focal_px,
                "gt": cam["K"][0][0],
                "coeffs": est.distortion_coeffs,
                "p95": model_discrepancy_px(cam, est).p95_px,
            }
        ),
    )
    assert est.method == METHOD_GEOCALIB and est.focal_px > 0


def test_anycalib_on_synthetic_room(wide_case):
    pytest.importorskip("torch")
    pytest.importorskip("anycalib")
    weights = _weights_from_env("SIMAI_ANYCALIB")
    img = (wide_case.image("cam1").clip(0, 1) * 255).astype("uint8")
    est = AnyCalibEstimator(weights).estimate(img, camera_id="cam1")
    cam = wide_case.cameras["cam1"]
    print(
        "SIM 상한 지표(AnyCalib):",
        json.dumps(
            {
                "focal_px": est.focal_px,
                "gt": cam["K"][0][0],
                "coeffs": est.distortion_coeffs,
                "p95": model_discrepancy_px(cam, est).p95_px,
            }
        ),
    )
    assert est.method == METHOD_ANYCALIB and est.focal_px > 0
