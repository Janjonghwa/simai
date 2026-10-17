"""판정 결과 → CalibrationProvider 계약 문서 시험 (calibration.schema.json, 명세 4.3 스케일 근거 기록)."""

import dataclasses

import pytest

from simai_ai.calibration import CalibrationEvidence, ScaleBasis, build_calibration_doc, judge_calibration
from simai_ai.calibration.document import CALIBRATION_SCHEMA_VERSION, content_hash
from simai_ai.providers import contract
from simai_ai.providers.testing import assert_no_violations, load_example


def _build(cameras, evidence, t0, source="G1_AUTO", fallback=ScaleBasis.MODEL_ESTIMATE, version="cal-1"):
    docs = [c.to_contract() for c in cameras]
    judgement = judge_calibration(source, docs, evidence)
    return build_calibration_doc(
        calibration_version=version,
        provider_source=source,
        provider_version="g1-0.1.0",
        created_at=t0,
        cameras=cameras,
        judgement=judgement,
        fallback_scale_basis=fallback,
        site_id="classroom-1",
    )


def test_g1_auto_calibrated_doc(cameras, evidence, t0):
    doc = _build(cameras, evidence, t0)
    assert_no_violations(contract.check_calibration(doc))
    assert_no_violations(contract.check_cameras_upright(doc))
    assert doc["provider_source"] == "G1_AUTO"
    assert doc["state"] == "CALIBRATED"
    assert doc["scale_basis"] == "MEASURED_OBJECT"
    assert doc["schema_version"] == CALIBRATION_SCHEMA_VERSION
    assert doc["coordinate_frame"] == {
        "frame": "WORLD",
        "unit": "m",
        "handedness": "right",
        "up_axis": "+Z",
        "site_id": "classroom-1",
    }
    assert doc["cameras"][0]["camera_height_m"] == pytest.approx(2.5, abs=1e-3)


def test_provisional_doc_without_scale_uses_fallback(cameras, t0):
    doc = _build(cameras, CalibrationEvidence(), t0)
    assert_no_violations(contract.check_calibration(doc))
    assert (doc["state"], doc["scale_basis"]) == ("PROVISIONAL", "MODEL_ESTIMATE")
    assert doc["validation"] == {"checkpoint_count": 0}


def test_fallback_scale_basis_recorded(cameras, t0):
    doc = _build(cameras, CalibrationEvidence(), t0, fallback="STANDARD_OBJECT")
    assert doc["scale_basis"] == "STANDARD_OBJECT"


@pytest.mark.parametrize("source", sorted(contract.NEVER_CALIBRATED_SOURCES))
def test_never_calibrated_doc_passes_schema(cameras, evidence, t0, source):
    doc = _build(cameras, evidence, t0, source=source)
    assert doc["state"] == "PROVISIONAL"
    assert_no_violations(contract.check_calibration(doc))


def test_unknown_reprojection_omitted(camera_factory, evidence, t0):
    cam = dataclasses.replace(camera_factory("cam1"), reprojection_median_px=None, reprojection_p95_px=None)
    doc = _build([cam], evidence, t0)
    assert "reprojection_error_px" not in doc["cameras"][0]
    assert doc["state"] == "PROVISIONAL"
    assert contract.check_calibration_schema(doc) == []


def test_content_hash_ignores_version_time_state(cameras, evidence, t0):
    a = _build(cameras, evidence, t0, version="cal-1")
    b = _build(cameras, evidence, t0 + 1000, version="cal-2")
    assert a["content_hash"] == b["content_hash"]
    assert a["content_hash"] == content_hash({**a, "state": "DRIFTED"})
    changed = _build([dataclasses.replace(cameras[0], reprojection_median_px=1.9)] + cameras[1:], evidence, t0)
    assert changed["content_hash"] != a["content_hash"]


def test_created_at_must_be_int_ms(cameras, evidence, t0):
    with pytest.raises(TypeError):
        _build(cameras, evidence, float(t0))


def test_examples_cover_new_scale_bases():
    g1 = load_example("calibration.g1_auto.json")
    assert (g1["provider_source"], g1["state"], g1["scale_basis"]) == ("G1_AUTO", "CALIBRATED", "MEASURED_OBJECT")
    assert_no_violations(contract.check_calibration(g1))
    base = load_example("calibration.json")
    assert (base["provider_source"], base["state"], base["scale_basis"]) == ("G1_AUTO", "PROVISIONAL", "MEASURED_OBJECT")
    assert_no_violations(contract.check_calibration(base))
