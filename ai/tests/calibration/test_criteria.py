"""CALIBRATED 판정 조건별 시험표 (명세 4.4 CALIBRATED 기준, 4.3 스케일 근거, 20.4 규칙 4. 수정 6).

통과 사례 하나와, 항목마다 그 항목 하나만 미달인 사례를 둔다. 미달 사례는 PROVISIONAL 이고 미달 항목이 정확히
그 항목 하나여야 한다(다른 항목이 함께 걸리면 시험이 그 항목을 따로 보지 못한다).
"""

import dataclasses
import json

import pytest

from simai_ai.calibration import (
    CalibrationState,
    Criterion,
    HeightCheckpoint,
    ScaleBasis,
    ScaleEstimate,
    judge_calibration,
)
from simai_ai.calibration.criteria import CROSS_CHECK_BASES, SCALE_PRIORITY
from simai_ai.providers import contract
from simai_ai.providers.contract import CalibratedCriteria

C = Criterion
MO, KP, HS = ScaleBasis.MEASURED_OBJECT, ScaleBasis.KNOWN_PERSON_HEIGHT, ScaleBasis.HUMAN_HEIGHT_STATS


def _judge(cameras, evidence, source="G1_AUTO", criteria=CalibratedCriteria()):
    return judge_calibration(source, [c.to_contract() for c in cameras], evidence, criteria)


def _with_camera(cameras, index, **changes):
    out = list(cameras)
    out[index] = dataclasses.replace(out[index], **changes)
    return out


def _points(*rows):
    """(머리 높이 오차, 쌍별 불일치[, 보정에 씀]) 행으로 검산점을 만든다. 실제 키 1.75m."""
    out = []
    for i, row in enumerate(rows):
        head, pair, *used = row
        measured = None if head is None else 1.75 + head
        out.append(HeightCheckpoint(f"p{i}", 1.75, measured, pair, used_in_calibration=bool(used and used[0])))
    return tuple(out)


def _scales(*rows):
    """(근거, 배율, 표본 수) 행으로 스케일 근거를 만든다. 실측 정물은 행마다 서로 다른 정물 이름을 붙인다."""
    return tuple(
        ScaleEstimate(basis, scale, n, label=f"정물{i}" if basis is MO else None)
        for i, (basis, scale, n) in enumerate(rows)
    )


GOOD_POINTS = ((0.06, 0.08), (-0.06, 0.11), (0.03, 0.06), (-0.03, 0.09))

# (id, 카메라 변경(index, 필드), 근거 변경, 기대 미달 항목)
CASES = [
    ("reprojection-median", (1, {"reprojection_median_px": 2.1}), {}, C.REPROJECTION_MEDIAN),
    ("reprojection-p95", (2, {"reprojection_p95_px": 5.1}), {}, C.REPROJECTION_P95),
    ("reprojection-median-unknown", (0, {"reprojection_median_px": None}), {}, C.REPROJECTION_MEDIAN),
    ("floor-coverage", None, {"floor_coverage_ratio": 0.79}, C.FLOOR_COVERAGE),
    ("floor-coverage-unknown", None, {"floor_coverage_ratio": None}, C.FLOOR_COVERAGE),
    ("floor-plane", None, {"floor_plane_disagreement_m": 0.031}, C.FLOOR_PLANE),
    ("floor-plane-unknown", None, {"floor_plane_disagreement_m": None}, C.FLOOR_PLANE),
    (
        "scale-one-object",
        None,
        {"scale_estimates": _scales((MO, 1.0, 1), (KP, 1.0, 1))},
        C.SCALE_PRIMARY,
    ),
    (
        "scale-objects-disagree",
        None,
        {"scale_estimates": _scales((MO, 0.97, 1), (MO, 1.03, 1), (KP, 1.0, 1))},
        C.SCALE_PRIMARY,
    ),
    (
        "scale-height-stats-19-people",
        None,
        {"scale_estimates": _scales((HS, 1.0, 19), (KP, 1.0, 1))},
        C.SCALE_PRIMARY,
    ),
    (
        "scale-known-person-only",
        None,
        {"scale_estimates": _scales((KP, 1.0, 1), (ScaleBasis.STANDARD_OBJECT, 1.0, 1))},
        C.SCALE_PRIMARY,
    ),
    ("scale-no-estimates", None, {"scale_estimates": ()}, C.SCALE_PRIMARY),
    (
        "scale-no-cross-check",
        None,
        {"scale_estimates": _scales((MO, 1.0, 1), (MO, 1.0, 1))},
        C.SCALE_CROSS_CHECK,
    ),
    (
        "scale-cross-check-disagrees",
        None,
        {"scale_estimates": _scales((MO, 1.0, 1), (MO, 1.0, 1), (KP, 1.06, 1))},
        C.SCALE_CROSS_CHECK,
    ),
    (
        "scale-cross-check-dataset-gt",
        None,
        {"scale_estimates": _scales((MO, 1.0, 1), (MO, 1.0, 1), (ScaleBasis.DATASET_GT, 1.0, 1))},
        C.SCALE_CROSS_CHECK,
    ),
    (
        "scale-residual",
        None,
        {"scale_estimates": _scales((MO, 1.03, 1), (MO, 1.03, 1), (KP, 1.03, 1))},
        C.SCALE_RESIDUAL,
    ),
    ("checkpoints-2", None, {"checkpoints": _points(*GOOD_POINTS[:2])}, C.CHECKPOINT_COUNT),
    (
        "checkpoint-used-in-calibration",
        None,
        {"checkpoints": _points(*GOOD_POINTS[:2], (0.02, 0.05, True))},
        C.CHECKPOINT_COUNT,
    ),
    ("head-height", None, {"checkpoints": _points(*GOOD_POINTS[:3], (0.16, 0.05))}, C.HEAD_HEIGHT),
    ("head-height-unknown", None, {"checkpoints": _points(*GOOD_POINTS[:3], (None, 0.05))}, C.HEAD_HEIGHT),
    ("pairwise-position", None, {"checkpoints": _points(*GOOD_POINTS[:3], (0.02, 0.16))}, C.PAIRWISE_POSITION),
    (
        "pairwise-position-unknown",
        None,
        {"checkpoints": _points(*GOOD_POINTS[:3], (0.02, None))},
        C.PAIRWISE_POSITION,
    ),
]


def test_all_criteria_pass(cameras, evidence):
    result = _judge(cameras, evidence)
    assert result.state is CalibrationState.CALIBRATED
    assert result.failures == ()
    assert result.scale.primary is MO
    assert result.scale.cross_checks == (KP,)
    assert result.scale.recorded_basis is MO


@pytest.mark.parametrize(
    ("camera_change", "evidence_change", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_single_criterion_fails(cameras, evidence, camera_change, evidence_change, expected):
    if camera_change is not None:
        cameras = _with_camera(cameras, camera_change[0], **camera_change[1])
    result = _judge(cameras, dataclasses.replace(evidence, **evidence_change))
    assert result.state is CalibrationState.PROVISIONAL
    assert result.failed_criteria == {expected}, [f.message for f in result.failures]


def test_every_criterion_has_a_failing_case():
    """출처는 출처 시험, 카메라 집합은 카메라 집합 시험이 따로 본다."""
    assert {c[-1] for c in CASES} | {C.SOURCE, C.CAMERA_SET} == set(Criterion)


# --- 스케일 근거 ---


def test_height_stats_20_people_is_primary(cameras, evidence):
    result = _judge(cameras, dataclasses.replace(evidence, scale_estimates=_scales((HS, 1.0, 20), (KP, 1.01, 1))))
    assert result.state is CalibrationState.CALIBRATED
    assert result.scale.primary is HS


def test_objects_disagree_but_height_stats_primary(cameras, evidence):
    estimates = _scales((MO, 0.97, 1), (MO, 1.03, 1), (HS, 1.0, 25))
    result = _judge(cameras, dataclasses.replace(evidence, scale_estimates=estimates))
    assert result.state is CalibrationState.CALIBRATED
    assert result.scale.primary is HS
    assert result.scale.cross_checks == (MO,)  # 서로 어긋난 정물도 하나씩은 다른 근거로 교차 확인에 쓸 수 있다


def test_measured_object_preferred_over_height_stats(cameras, evidence):
    estimates = _scales((MO, 1.0, 1), (MO, 1.0, 1), (HS, 1.01, 30))
    result = _judge(cameras, dataclasses.replace(evidence, scale_estimates=estimates))
    assert result.scale.primary is MO
    assert result.scale.cross_checks == (HS,)


def test_cross_checks_in_priority_order(cameras, evidence):
    estimates = _scales(
        (MO, 1.0, 1), (MO, 1.0, 1), (ScaleBasis.MODEL_ESTIMATE, 1.0, 1), (KP, 1.0, 1), (KP, 1.01, 1), (HS, 1.2, 5)
    )
    result = _judge(cameras, dataclasses.replace(evidence, scale_estimates=estimates))
    assert result.scale.cross_checks == (KP, ScaleBasis.MODEL_ESTIMATE), "일치한 근거만, 우선순위 순, 중복 없이"


def test_recorded_basis_without_primary_is_highest_priority_present(cameras, evidence):
    estimates = _scales((ScaleBasis.MODEL_ESTIMATE, 1.0, 1), (KP, 1.0, 1))
    result = _judge(cameras, dataclasses.replace(evidence, scale_estimates=estimates))
    assert result.scale.primary is None
    assert result.scale.recorded_basis is KP


def test_recorded_basis_none_without_estimates(cameras, evidence):
    result = _judge(cameras, dataclasses.replace(evidence, scale_estimates=()))
    assert result.scale.recorded_basis is None


def test_scale_estimate_rejects_non_positive():
    with pytest.raises(ValueError):
        ScaleEstimate(MO, 0.0)
    with pytest.raises(ValueError):
        ScaleEstimate(HS, 1.0, sample_count=0)


# --- 출처 (20.4 규칙 4) ---


@pytest.mark.parametrize("source", sorted(contract.NEVER_CALIBRATED_SOURCES))
def test_never_calibrated_sources_with_perfect_numbers(cameras, evidence, source):
    """정답·시뮬레이터·모델 추정 보정은 수치가 모두 좋아도 CALIBRATED 불가."""
    perfect = [dataclasses.replace(c, reprojection_median_px=0.0, reprojection_p95_px=0.0) for c in cameras]
    result = _judge(perfect, evidence, source=source)
    assert result.state is CalibrationState.PROVISIONAL
    assert result.failed_criteria == {C.SOURCE}


def test_unlisted_source_not_calibrated(cameras, evidence):
    """계약 출처 값에 없는 출처(v1.3 에서 뺀 대체 경로 등)는 수치가 좋아도 CALIBRATED 불가."""
    result = _judge(cameras, evidence, source="UNLISTED_SOURCE")
    assert result.state is CalibrationState.PROVISIONAL
    assert result.failed_criteria == {C.SOURCE}


def test_calibrated_sources_v13():
    """v1.3 대체 경로는 걷기 재수행·실측 정물 추가 뒤 G1 재보정뿐이라 G1_AUTO 만 CALIBRATED 출처다 (4.3)."""
    assert contract.CALIBRATED_SOURCES == {"G1_AUTO"}
    assert contract.CALIBRATED_SOURCES.isdisjoint(contract.NEVER_CALIBRATED_SOURCES)


def test_scale_bases_consistent_with_contract():
    assert {MO, HS} == contract.PRIMARY_SCALE_BASES
    assert CROSS_CHECK_BASES == set(SCALE_PRIORITY)
    assert ScaleBasis.DATASET_GT not in CROSS_CHECK_BASES


def test_all_failures_collected(cameras, evidence):
    """출처가 불가여도 나머지 항목을 끝까지 본다."""
    bad = dataclasses.replace(evidence, floor_coverage_ratio=0.5, floor_plane_disagreement_m=0.1)
    result = _judge(cameras, bad, source="SIM")
    assert result.failed_criteria == {C.SOURCE, C.FLOOR_COVERAGE, C.FLOOR_PLANE}


def test_no_cameras(evidence):
    assert C.REPROJECTION_MEDIAN in _judge([], evidence).failed_criteria


# --- 임계값은 설정값 ---


def test_thresholds_inclusive(cameras, evidence):
    """기준값과 같으면 통과한다. 이진 소수로 정확히 표현되는 값으로 본다."""
    criteria = CalibratedCriteria(
        max_reprojection_median_px=1.25,
        max_reprojection_p95_px=3.5,
        min_floor_coverage_ratio=0.75,
        max_floor_plane_disagreement_m=0.03125,
        max_head_height_error_m=0.125,
        max_pairwise_position_disagreement_m=0.125,
    )
    edge_cameras = [dataclasses.replace(c, reprojection_median_px=1.25, reprojection_p95_px=3.5) for c in cameras]
    edge = dataclasses.replace(
        evidence,
        floor_coverage_ratio=0.75,
        floor_plane_disagreement_m=0.03125,
        checkpoints=(
            HeightCheckpoint("p1", 1.75, 1.875, 0.125),
            HeightCheckpoint("p2", 1.75, 1.625, 0.0625),
            HeightCheckpoint("p3", 1.5, 1.5, 0.0),
        ),
    )
    assert _judge(edge_cameras, edge, criteria=criteria).state is CalibrationState.CALIBRATED


def test_criteria_configurable(cameras, evidence):
    strict = CalibratedCriteria(max_reprojection_median_px=1.0)
    assert _judge(cameras, evidence, criteria=strict).failed_criteria == {C.REPROJECTION_MEDIAN}


def test_default_criteria_match_rev6():
    c = CalibratedCriteria()
    assert (c.max_reprojection_median_px, c.max_reprojection_p95_px) == (2.0, 5.0)
    assert c.min_floor_coverage_ratio == 0.8
    assert c.max_floor_plane_disagreement_m == 0.03
    assert (c.min_measured_objects, c.min_height_stats_people) == (2, 20)
    assert c.min_checkpoints == 3
    assert (c.max_head_height_error_m, c.max_pairwise_position_disagreement_m) == (0.15, 0.15)


# --- 판정 결과의 validation ---


def test_validation_for_contract(cameras, evidence):
    v = _judge(cameras, evidence).validation
    assert v == {
        "checkpoint_count": 4,
        "max_pairwise_position_disagreement_m": 0.11,
        "max_head_height_error_m": 0.06,
        "floor_plane_disagreement_m": 0.02,
        "floor_coverage_ratio": 0.85,
    }
    ref = "https://simai.dev/contracts/providers/calibration.schema.json#/properties/validation"
    assert contract.schema_errors(v, {"$ref": ref}) == []


def test_validation_omits_unknown(cameras, evidence):
    unknown = dataclasses.replace(evidence, floor_coverage_ratio=None, checkpoints=_points((None, None), (0.1, 0.1)))
    v = _judge(cameras, unknown).validation
    assert v == {"checkpoint_count": 2, "floor_plane_disagreement_m": 0.02}


def test_scale_basis_enum_matches_contract():
    path = contract.contracts_dir() / "providers" / "calibration.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert {b.value for b in ScaleBasis} == set(schema["$defs"]["scale_basis"]["enum"])


# --- 리뷰 회귀: 유효하지 않은 수치(M1) ---

NAN, INF = float("nan"), float("inf")
VALIDATION_REF = "https://simai.dev/contracts/providers/calibration.schema.json#/properties/validation"


@pytest.mark.parametrize(
    ("evidence_change", "expected"),
    [
        pytest.param({"floor_coverage_ratio": NAN}, C.FLOOR_COVERAGE, id="coverage-nan"),
        pytest.param({"floor_coverage_ratio": INF}, C.FLOOR_COVERAGE, id="coverage-inf"),
        pytest.param({"floor_coverage_ratio": 1.5}, C.FLOOR_COVERAGE, id="coverage-over-1"),
        pytest.param({"floor_plane_disagreement_m": NAN}, C.FLOOR_PLANE, id="floor-plane-nan"),
        pytest.param({"floor_plane_disagreement_m": -0.01}, C.FLOOR_PLANE, id="floor-plane-negative"),
        pytest.param({"checkpoints": _points(*GOOD_POINTS[:3], (NAN, 0.05))}, C.HEAD_HEIGHT, id="head-nan"),
        pytest.param({"checkpoints": _points(*GOOD_POINTS[:3], (0.02, NAN))}, C.PAIRWISE_POSITION, id="pair-nan"),
        pytest.param({"checkpoints": _points(*GOOD_POINTS[:3], (0.02, -0.01))}, C.PAIRWISE_POSITION, id="pair-neg"),
    ],
)
def test_invalid_evidence_values_never_approve(cameras, evidence, evidence_change, expected):
    """NaN·무한대·범위 밖 값은 대소 비교가 거짓이 되어 통과하던 결함. 미달이고, validation 에 남기지 않는다."""
    result = _judge(cameras, dataclasses.replace(evidence, **evidence_change))
    assert result.state is CalibrationState.PROVISIONAL
    assert result.failed_criteria == {expected}, [f.message for f in result.failures]
    assert result.invalid_inputs, "유효하지 않은 입력은 모르는 값과 구분해 알린다"
    json.dumps(result.validation, allow_nan=False)
    assert contract.schema_errors(result.validation, {"$ref": VALIDATION_REF}) == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("reprojection_median_px", NAN, C.REPROJECTION_MEDIAN),
        ("reprojection_median_px", -1.0, C.REPROJECTION_MEDIAN),
        ("reprojection_p95_px", NAN, C.REPROJECTION_P95),
        ("reprojection_p95_px", INF, C.REPROJECTION_P95),
    ],
)
def test_invalid_reprojection_never_approves(cameras, evidence, field, value, expected):
    result = _judge(_with_camera(cameras, 2, **{field: value}), evidence)
    assert result.state is CalibrationState.PROVISIONAL
    assert result.failed_criteria == {expected}
    assert result.invalid_inputs


@pytest.mark.parametrize(
    "change",
    [
        {"max_reprojection_median_px": NAN},
        {"min_floor_coverage_ratio": INF},
        {"min_floor_coverage_ratio": 1.5},
        {"max_head_height_error_m": -0.1},
        {"min_checkpoints": 0},
        {"min_cameras": 0},
    ],
)
def test_criteria_reject_invalid_thresholds(change):
    with pytest.raises(ValueError):
        CalibratedCriteria(**change)


def test_scale_estimate_rejects_non_finite():
    with pytest.raises(ValueError):
        ScaleEstimate(HS, INF, 20)


# --- 리뷰 회귀: 카메라 집합(M2) ---


@pytest.mark.parametrize("keep", [1, 2, 3])
def test_missing_cameras_never_calibrated(cameras, evidence, keep):
    """설치 4대 중 일부만 낸 보정은 4대의 바닥 평면 차이를 검증하지 않았으므로 CALIBRATED 가 아니다."""
    kept = cameras[:keep]
    ids = tuple(c.camera_id for c in kept)
    result = _judge(kept, dataclasses.replace(evidence, floor_plane_camera_ids=ids))
    assert result.state is CalibrationState.PROVISIONAL
    assert result.failed_criteria == {C.CAMERA_SET}, [f.message for f in result.failures]


def test_duplicate_camera_ids_never_calibrated(cameras, evidence):
    dup = [*cameras[:3], dataclasses.replace(cameras[3], camera_id="cam1")]
    result = _judge(dup, dataclasses.replace(evidence, floor_plane_camera_ids=("cam1", "cam2", "cam3")))
    assert result.state is CalibrationState.PROVISIONAL
    assert C.CAMERA_SET in result.failed_criteria


def test_expected_camera_set_must_match(cameras, evidence):
    ids = [c.camera_id for c in cameras]
    docs = [c.to_contract() for c in cameras]
    assert judge_calibration("G1_AUTO", docs, evidence, expected_camera_ids=ids).state is CalibrationState.CALIBRATED
    missing = judge_calibration("G1_AUTO", docs, evidence, expected_camera_ids=[*ids, "cam5"])
    assert missing.failed_criteria == {C.CAMERA_SET}
    other = judge_calibration("G1_AUTO", docs, evidence, expected_camera_ids=[*ids[:3], "cam9"])
    assert other.failed_criteria == {C.CAMERA_SET}


@pytest.mark.parametrize(
    "floor_ids",
    [
        pytest.param((), id="unknown"),
        pytest.param(("cam1", "cam2", "cam3"), id="one-camera-missing"),
        pytest.param(("cam1", "cam2", "cam3", "cam9"), id="foreign-camera"),
    ],
)
def test_floor_plane_must_cover_every_camera(cameras, evidence, floor_ids):
    result = _judge(cameras, dataclasses.replace(evidence, floor_plane_camera_ids=floor_ids))
    assert result.failed_criteria == {C.FLOOR_PLANE}, [f.message for f in result.failures]


# --- 리뷰 회귀: 중복 근거(M3) ---


def test_repeated_checkpoint_counts_once(cameras, evidence):
    result = _judge(cameras, dataclasses.replace(evidence, checkpoints=(evidence.checkpoints[0],) * 3))
    assert result.state is CalibrationState.PROVISIONAL
    assert result.failed_criteria == {C.CHECKPOINT_COUNT}
    assert result.validation["checkpoint_count"] == 1


def test_repeated_observations_aggregate_per_point(cameras, evidence):
    """같은 지점을 여러 번 관측해도 한 곳이고, 오차는 그 지점 관측 중 가장 나쁜 값으로 본다."""
    points = (*_points(*GOOD_POINTS[:3]), HeightCheckpoint("p0", 1.75, 1.89, 0.14))
    result = _judge(cameras, dataclasses.replace(evidence, checkpoints=points))
    assert result.state is CalibrationState.CALIBRATED
    assert result.validation["checkpoint_count"] == 3
    assert result.validation["max_head_height_error_m"] == 0.14
    assert result.validation["max_pairwise_position_disagreement_m"] == 0.14


def test_point_used_in_calibration_in_any_record_is_not_independent(cameras, evidence):
    points = (*_points(*GOOD_POINTS[:3]), HeightCheckpoint("p0", 1.75, 1.75, 0.0, used_in_calibration=True))
    result = _judge(cameras, dataclasses.replace(evidence, checkpoints=points))
    assert result.failed_criteria == {C.CHECKPOINT_COUNT}
    assert result.validation["checkpoint_count"] == 2


def test_checkpoint_requires_point_id_and_known_height():
    with pytest.raises(ValueError):
        HeightCheckpoint("", 1.75, 1.75, 0.0)
    with pytest.raises(ValueError):
        HeightCheckpoint("p1", NAN, 1.75, 0.0)


def test_same_measured_object_counts_once(cameras, evidence):
    same = ScaleEstimate(MO, 1.0, label="same-cone")
    scales = (same, same, ScaleEstimate(KP, 1.0))
    result = _judge(cameras, dataclasses.replace(evidence, scale_estimates=scales))
    assert result.state is CalibrationState.PROVISIONAL
    assert result.failed_criteria == {C.SCALE_PRIMARY}


def test_repeated_object_observations_still_two_objects(cameras, evidence):
    scales = (
        ScaleEstimate(MO, 1.0, label="안전봉"),
        ScaleEstimate(MO, 1.002, label="안전봉"),
        ScaleEstimate(MO, 0.999, label="라바콘"),
        ScaleEstimate(KP, 1.0),
    )
    result = _judge(cameras, dataclasses.replace(evidence, scale_estimates=scales))
    assert result.state is CalibrationState.CALIBRATED


def test_measured_object_requires_label():
    with pytest.raises(ValueError):
        ScaleEstimate(MO, 1.0)


# --- 기본 기준값 경계 (리뷰 권장) ---


def test_default_thresholds_boundaries(cameras, evidence):
    """기본 기준값(2px·5px·80%·3cm·15cm)과 같으면 통과, 아주 조금 넘으면 미달."""
    edge_cams = [dataclasses.replace(c, reprojection_median_px=2.0, reprojection_p95_px=5.0) for c in cameras]
    edge = dataclasses.replace(
        evidence,
        floor_coverage_ratio=0.8,
        floor_plane_disagreement_m=0.03,
        checkpoints=_points((0.15, 0.15), (-0.15, 0.15), (0.0, 0.0)),
    )
    assert _judge(edge_cams, edge).state is CalibrationState.CALIBRATED
    eps = 1e-5
    over = [
        (_with_camera(edge_cams, 0, reprojection_median_px=2.0 + eps), edge, C.REPROJECTION_MEDIAN),
        (_with_camera(edge_cams, 0, reprojection_p95_px=5.0 + eps), edge, C.REPROJECTION_P95),
        (edge_cams, dataclasses.replace(edge, floor_coverage_ratio=0.8 - eps), C.FLOOR_COVERAGE),
        (edge_cams, dataclasses.replace(edge, floor_plane_disagreement_m=0.03 + eps), C.FLOOR_PLANE),
        (edge_cams, dataclasses.replace(edge, checkpoints=_points((0.15 + eps, 0), (0, 0), (0, 0))), C.HEAD_HEIGHT),
        (
            edge_cams,
            dataclasses.replace(edge, checkpoints=_points((0, 0.15 + eps), (0, 0), (0, 0))),
            C.PAIRWISE_POSITION,
        ),
    ]
    for cams, ev, expected in over:
        assert _judge(cams, ev).failed_criteria == {expected}
