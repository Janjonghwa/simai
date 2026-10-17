"""계약 검사 함수가 위반을 실제로 잡는지 본다. 통과 예시를 하나씩 망가뜨려 위반이 나오는지 확인한다."""

import copy
import math

import pytest

from simai_ai.providers import WorldTrackProvider, contract
from simai_ai.providers.testing import assert_no_violations, load_example


def _has(violations, *needles):
    assert violations, "위반이 나와야 한다"
    text = "\n".join(violations)
    for needle in needles:
        assert needle in text, text


def _rot_z(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


# --------------------------------------------------------------------------- 회전


def test_rotation_accepts_rotations():
    assert contract.check_rotation(_rot_z(37)) == []
    assert contract.check_rotation(load_example("calibration.json")["cameras"][0]["R"]) == []


@pytest.mark.parametrize(
    ("R", "needle"),
    [
        pytest.param([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]], "det(R)", id="reflection"),
        pytest.param([[2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 2.0]], "직교", id="scaled"),
        pytest.param([[1.0, 0.1, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], "직교", id="sheared"),
        pytest.param([[1.0, 0.0], [0.0, 1.0]], "3x3", id="not-3x3"),
    ],
)
def test_rotation_rejects(R, needle):
    _has(contract.check_rotation(R), needle)


# --------------------------------------------------------------------------- 보정


def test_calibration_example_passes(calibration_doc):
    assert_no_violations(contract.check_calibration(calibration_doc))


def test_camera_center_of_example(calibration_doc):
    cam = calibration_doc["cameras"][0]
    center = contract.camera_center(cam["R"], cam["t"])
    assert center[2] == pytest.approx(cam["camera_height_m"], abs=contract.CAMERA_HEIGHT_TOL_M)


def test_calibration_rejects_millimetre_translation(calibration_doc):
    cam = calibration_doc["cameras"][0]
    cam["t"] = [v * 1000 for v in cam["t"]]
    cam.pop("camera_height_m")
    _has(contract.check_calibration_geometry(calibration_doc), "카메라 중심 Z")


def test_calibration_rejects_camera_height_mismatch(calibration_doc):
    calibration_doc["cameras"][0]["camera_height_m"] = 2.7
    _has(contract.check_calibration_geometry(calibration_doc), "camera_height_m")


def test_calibration_rejects_z_down_world(calibration_doc):
    """World 의 Z 를 뒤집으면(Z-down) 카메라가 바닥 아래로 가고 이미지 아래 방향이 World 위쪽이 된다."""
    cam = calibration_doc["cameras"][0]
    # x_world' = F x_world, F = diag(1, -1, -1) (det=+1 이 되도록 Y 도 뒤집음) → R' = R F
    cam["R"] = [[row[0], -row[1], -row[2]] for row in cam["R"]]
    cam.pop("camera_height_m")
    _has(contract.check_calibration_geometry(calibration_doc), "카메라 중심 Z")
    _has(contract.check_cameras_upright(calibration_doc), "이미지 아래 방향")


def test_calibration_rejects_non_rotation(calibration_doc):
    calibration_doc["cameras"][0]["R"][2] = [0.0, 0.0, 1.0]
    _has(contract.check_calibration_geometry(calibration_doc), "직교")


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        pytest.param(lambda c: c["K"][0].__setitem__(2, 3000.0), "주점", id="principal-point-outside"),
        pytest.param(lambda c: c["K"][0].__setitem__(0, -1000.0), "초점 거리", id="negative-focal"),
        pytest.param(lambda c: c["K"].__setitem__(2, [0.0, 0.0, 2.0]), "형식", id="bad-last-row"),
        pytest.param(lambda c: c["distortion"].update(coeffs=[0.1, 0.2, 0.3]), "PINHOLE", id="pinhole-3-coeffs"),
        pytest.param(lambda c: c["distortion"].update(model="NONE"), "NONE", id="none-with-coeffs"),
        pytest.param(lambda c: c["distortion"].update(model="OPENCV_FISHEYE"), "FISHEYE", id="fisheye-5-coeffs"),
    ],
)
def test_calibration_rejects_intrinsics(calibration_doc, mutate, needle):
    mutate(calibration_doc["cameras"][0])
    _has(contract.check_calibration_geometry(calibration_doc), needle)


def test_calibration_rejects_duplicate_camera(calibration_doc):
    calibration_doc["cameras"].append(dict(calibration_doc["cameras"][0]))
    _has(contract.check_calibration_geometry(calibration_doc), "중복")


@pytest.mark.parametrize("source", ["DATASET_GT", "SIM", "MODEL_ESTIMATE"])
def test_never_calibrated_sources(calibration_doc, source):
    calibration_doc.update(provider_source=source, state="CALIBRATED")
    _has(contract.check_calibration_state(calibration_doc), "CALIBRATED 가 될 수 없다")
    assert contract.check_calibration_schema(calibration_doc), "스키마도 막는다"


@pytest.mark.parametrize("source", ["DATASET_GT", "SIM", "MODEL_ESTIMATE"])
def test_never_calibrated_sources_may_be_provisional(calibration_doc, source):
    calibration_doc.update(provider_source=source, state="PROVISIONAL")
    assert contract.check_calibration_state(calibration_doc) == []


@pytest.mark.parametrize("source", ["G1_AUTO", "CHARUCO"])
def test_calibrated_sources_with_passing_validation(calibration_doc, source):
    calibration_doc.update(provider_source=source, state="CALIBRATED")
    assert_no_violations(contract.check_calibration(calibration_doc))


def test_manual_pnp_not_calibrated_after_rev6(calibration_doc):
    """평면도 점 PnP 는 수정 6 에서 대체 경로에서 빠졌다. 계약 값은 남지만 CALIBRATED 는 안 된다."""
    calibration_doc["state"] = "CALIBRATED"
    _has(contract.check_calibration_state(calibration_doc), "MANUAL_PNP", "CALIBRATED 가 될 수 없다")
    assert contract.check_calibration_schema(calibration_doc) == [], "스키마는 막지 않는다(계약 시험이 막는다)"


def test_manual_pnp_may_be_provisional(calibration_doc):
    assert calibration_doc["provider_source"] == "MANUAL_PNP"
    assert_no_violations(contract.check_calibration(calibration_doc))


_GOOD_VALIDATION = {
    "checkpoint_count": 4,
    "max_pairwise_position_disagreement_m": 0.1,
    "max_head_height_error_m": 0.1,
    "floor_plane_disagreement_m": 0.02,
    "floor_coverage_ratio": 0.85,
}


def _without(key):
    return {k: v for k, v in _GOOD_VALIDATION.items() if k != key}


@pytest.mark.parametrize(
    ("validation", "needle"),
    [
        pytest.param(None, "validation", id="missing"),
        pytest.param({**_GOOD_VALIDATION, "checkpoint_count": 2}, "검증점", id="too-few-checkpoints"),
        pytest.param(
            {**_GOOD_VALIDATION, "max_pairwise_position_disagreement_m": 0.16},
            "max_pairwise_position_disagreement_m",
            id="pairwise-position",
        ),
        pytest.param(
            _without("max_pairwise_position_disagreement_m"),
            "max_pairwise_position_disagreement_m 이 없다",
            id="pairwise-position-missing",
        ),
        pytest.param({**_GOOD_VALIDATION, "max_head_height_error_m": 0.16}, "max_head_height_error_m", id="head-error"),
        pytest.param(_without("max_head_height_error_m"), "max_head_height_error_m 이 없다", id="head-error-missing"),
        pytest.param({**_GOOD_VALIDATION, "floor_coverage_ratio": 0.5}, "floor_coverage_ratio", id="coverage"),
        pytest.param(_without("floor_coverage_ratio"), "floor_coverage_ratio 이 없다", id="coverage-missing"),
        pytest.param({**_GOOD_VALIDATION, "floor_plane_disagreement_m": 0.05}, "floor_plane", id="floor-plane"),
        pytest.param(
            _without("floor_plane_disagreement_m"), "floor_plane_disagreement_m 이 없다", id="floor-plane-missing"
        ),
    ],
)
def test_calibrated_requires_independent_validation(calibration_doc, validation, needle):
    calibration_doc.update(provider_source="G1_AUTO", state="CALIBRATED")
    calibration_doc.pop("validation")
    if validation is not None:
        calibration_doc["validation"] = validation
    _has(contract.check_calibration_state(calibration_doc), needle)


def test_absolute_position_error_not_a_calibrated_criterion(calibration_doc):
    """절대 위치 오차는 개발 교실 G1·G2 실측 지표다. 수정 6 이후 CALIBRATED 조건이 아니다."""
    calibration_doc.update(provider_source="G1_AUTO", state="CALIBRATED")
    calibration_doc["validation"]["max_position_error_m"] = 0.5
    assert_no_violations(contract.check_calibration(calibration_doc))


def test_calibrated_requires_reprojection_within_criteria(calibration_doc):
    calibration_doc.update(provider_source="G1_AUTO", state="CALIBRATED")
    calibration_doc["cameras"][0]["reprojection_error_px"] = {"median": 2.5, "p95": 4.0}
    _has(contract.check_calibration_state(calibration_doc), "재투영 오차 median")
    calibration_doc["cameras"][0].pop("reprojection_error_px")
    _has(contract.check_calibration_state(calibration_doc), "재투영 오차 median 이 없다", "재투영 오차 p95 이 없다")


@pytest.mark.parametrize(
    ("basis", "cross", "needle"),
    [
        pytest.param("STANDARD_OBJECT", ["KNOWN_PERSON_HEIGHT"], "주 근거가 될 수 없다", id="weak-primary"),
        pytest.param("FLOOR_PLAN", ["KNOWN_PERSON_HEIGHT"], "주 근거가 될 수 없다", id="floor-plan"),
        pytest.param("MEASURED_OBJECT", None, "교차 확인", id="no-cross-check"),
        pytest.param("MEASURED_OBJECT", ["MEASURED_OBJECT"], "교차 확인", id="cross-check-same-basis"),
        pytest.param("MEASURED_OBJECT", ["DATASET_GT"], "교차 확인", id="cross-check-dataset-gt"),
    ],
)
def test_calibrated_requires_scale_bases(calibration_doc, basis, cross, needle):
    calibration_doc.update(provider_source="G1_AUTO", state="CALIBRATED", scale_basis=basis)
    calibration_doc.pop("scale_cross_check_basis")
    if cross is not None:
        calibration_doc["scale_cross_check_basis"] = cross
    _has(contract.check_calibration_state(calibration_doc), needle)


def test_height_stats_primary_accepted(calibration_doc):
    calibration_doc.update(
        provider_source="CHARUCO",
        state="CALIBRATED",
        scale_basis="HUMAN_HEIGHT_STATS",
        scale_cross_check_basis=["MEASURED_OBJECT", "MARKER"],
    )
    assert_no_violations(contract.check_calibration(calibration_doc))


def test_calibrated_criteria_override(calibration_doc):
    calibration_doc.update(provider_source="G1_AUTO", state="CALIBRATED")
    strict = contract.CalibratedCriteria(max_pairwise_position_disagreement_m=0.1)
    _has(contract.check_calibration_state(calibration_doc, strict), "max_pairwise_position_disagreement_m")


@pytest.mark.parametrize(
    "name",
    [
        "calibration.dataset_calibrated.json",
        "calibration.model_estimate_calibrated.json",
        "calibration.sim_calibrated.json",
    ],
)
def test_invalid_calibration_examples_fail(name):
    doc = load_example(f"invalid/{name}")
    _has(contract.check_calibration(doc), "CALIBRATED 가 될 수 없다")


@pytest.mark.parametrize(
    "key", ["max_head_height_error_m", "max_pairwise_position_disagreement_m", "floor_plane_disagreement_m"]
)
def test_calibrated_rejects_nan_validation(calibration_doc, key):
    """NaN 은 대소 비교가 모두 거짓이라 상한 검사를 통과하던 결함. jsonschema 도 NaN 을 막지 않는다."""
    calibration_doc.update(provider_source="G1_AUTO", state="CALIBRATED")
    calibration_doc["validation"][key] = float("nan")
    _has(contract.check_calibration_state(calibration_doc), key)
    _has(contract.check_calibration(calibration_doc), "유한")


def test_calibrated_rejects_nan_coverage_and_reprojection(calibration_doc):
    calibration_doc.update(provider_source="G1_AUTO", state="CALIBRATED")
    calibration_doc["validation"]["floor_coverage_ratio"] = float("nan")
    calibration_doc["cameras"][0]["reprojection_error_px"] = {"median": float("nan"), "p95": 3.0}
    _has(contract.check_calibration_state(calibration_doc), "floor_coverage_ratio", "재투영 오차 median")


@pytest.mark.parametrize(
    ("path", "value", "needle"),
    [
        pytest.param(("cameras", 0, "t", 0), float("inf"), "유한", id="t-inf"),
        pytest.param(("cameras", 0, "K", 0, 0), float("nan"), "유한", id="K-nan"),
        pytest.param(("validation", "floor_plane_disagreement_m"), -0.01, "floor_plane_disagreement_m", id="neg"),
        pytest.param(("validation", "floor_coverage_ratio"), 1.2, "floor_coverage_ratio", id="coverage-over-1"),
        pytest.param(("cameras", 0, "reprojection_error_px", "p95"), -1.0, "p95", id="p95-negative"),
    ],
)
def test_calibration_values_must_be_finite_and_in_range(calibration_doc, path, value, needle):
    """스키마 없이 도는 기하 검사(버전 저장소가 쓴다)도 비유한 수치·범위 밖 값을 잡는다."""
    target = calibration_doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    _has(contract.check_calibration_geometry(calibration_doc), needle)


def test_calibration_camera_ids_unique(calibration_doc):
    calibration_doc["cameras"].append(copy.deepcopy(calibration_doc["cameras"][0]))
    _has(contract.check_calibration_geometry(calibration_doc), "camera_id", "중복")


# --------------------------------------------------------------------------- World 트랙


def _track(doc):
    return doc["tracks"][0]


def test_world_tracks_example_passes(tracks_doc):
    assert_no_violations(contract.check_world_tracks(tracks_doc))


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"support_surface_validity": "UNKNOWN"}, id="surface-unknown"),
        pytest.param({"support_surface_validity": "UNCERTAIN"}, id="surface-uncertain"),
        pytest.param({"z_validity": "UNAVAILABLE"}, id="z-unavailable"),
        pytest.param({"contact_validity": "UNAVAILABLE"}, id="contact-unavailable"),
        pytest.param({"support_surface_id": None}, id="no-surface-id"),
    ],
)
def test_ground_anchor_requires_verified_contact(tracks_doc, update):
    _track(tracks_doc).update(update)
    _has(contract.check_ground_anchor(tracks_doc), "Z=0 대체 금지")


def test_unknown_contact_with_empty_anchor_passes(tracks_doc):
    _track(tracks_doc).update(
        ground_anchor_xyz=None, support_surface_validity="UNKNOWN", support_surface_id=None, z_validity="UNAVAILABLE"
    )
    assert_no_violations(contract.check_world_tracks(tracks_doc))


def test_z0_fill_example_is_caught():
    """contracts 의 Z=0 대체 위반 예시(world_state) 트랙도 같은 검사로 잡힌다."""
    payload = load_example("invalid/world_state.z0_fill.json")["payload"]
    _has(contract.check_ground_anchor(payload), "Z=0 대체 금지")


def test_world_rejects_millimetres(tracks_doc):
    tr = _track(tracks_doc)
    tr["position_xy"] = [1200.0, 400.0]
    tr["pelvis_xyz"] = [1200.0, 400.0, 950.0]
    tr["ground_anchor_xyz"] = [1210.0, 410.0, 0.0]
    tr["head_z"] = 1710.0
    _has(contract.check_world_units_axes(tracks_doc), "미터 단위", "pelvis_xyz Z")


def test_world_rejects_z_down(tracks_doc):
    tr = _track(tracks_doc)
    tr["pelvis_xyz"][2] = -0.95
    tr["head_z"] = -1.71
    _has(contract.check_world_units_axes(tracks_doc), "head_z", "골반이 접지점보다")


def test_world_allows_head_below_pelvis(tracks_doc):
    """허리를 숙이거나 넘어진 자세는 머리가 골반보다 낮다. 정상 출력이므로 막지 않는다."""
    _track(tracks_doc)["head_z"] = 0.5
    assert_no_violations(contract.check_world_tracks(tracks_doc))


def test_world_rejects_skeleton_joint_out_of_range(tracks_doc):
    _track(tracks_doc)["skeleton"] = _skeleton(joints={0: [1.2, 0.4, 1600.0]})
    _has(contract.check_world_units_axes(tracks_doc), "skeleton.joints[0]")


def _skeleton(joints=None, visible=None, validity=None, source=None, n=17):
    sk = {
        "joint_layout": "COCO17",
        "joints": [None] * n,
        "visible": [False] * n,
        "validity": ["UNAVAILABLE"] * n,
        "joint_source": ["observed"] * n,
    }
    for key, values in (("joints", joints), ("visible", visible), ("validity", validity), ("joint_source", source)):
        for j, v in (values or {}).items():
            sk[key][j] = v
    return sk


def test_valid_skeleton_passes(tracks_doc):
    _track(tracks_doc)["skeleton"] = _skeleton(joints={0: [1.2, 0.4, 1.65]}, visible={0: True}, validity={0: "VALID"})
    assert_no_violations(contract.check_world_tracks(tracks_doc))


@pytest.mark.parametrize(
    ("skeleton", "needle"),
    [
        pytest.param(_skeleton(n=16), "관절 17개", id="wrong-count"),
        pytest.param({**_skeleton(), "visible": [False] * 16}, "길이가 다르다", id="length-mismatch"),
        pytest.param(
            _skeleton(joints={3: [1.0, 0.0, 1.5]}, validity={3: "UNCERTAIN"}), "visible=false", id="invented-joint"
        ),
        pytest.param(_skeleton(validity={2: "VALID"}), "null 관절", id="null-joint-valid"),
    ],
)
def test_skeleton_rules(tracks_doc, skeleton, needle):
    _track(tracks_doc)["skeleton"] = skeleton
    _has(contract.check_world_track_semantics(tracks_doc), needle)


def test_predicted_position_is_not_observed(tracks_doc):
    _track(tracks_doc).update(
        pelvis_xyz=None,
        ground_anchor_xyz=None,
        support_surface_validity="UNKNOWN",
        support_surface_id=None,
        z_validity="UNAVAILABLE",
        head_z=None,
    )
    _has(contract.check_world_track_semantics(tracks_doc), "예측값")
    _track(tracks_doc)["observation_state"] = "PREDICTED"
    assert_no_violations(contract.check_world_tracks(tracks_doc))


def test_same_camera_twice_in_one_track(tracks_doc):
    per_camera = _track(tracks_doc)["per_camera"]
    per_camera[1]["camera_id"] = per_camera[0]["camera_id"]
    _has(contract.check_world_track_semantics(tracks_doc), "같은 카메라")


def test_keypoint_count_follows_layout(tracks_doc):
    _track(tracks_doc)["per_camera"][0]["keypoints_2d"] = [[915.0, 240.0, 1.0], None]
    _has(contract.check_world_track_semantics(tracks_doc), "keypoint 17개")


def test_keypoints_require_layout(tracks_doc):
    _track(tracks_doc)["per_camera"][0].pop("joint_layout")
    _has(contract.check_world_track_semantics(tracks_doc), "joint_layout 이 있어야")


def test_bbox_order(tracks_doc):
    _track(tracks_doc)["per_camera"][0]["bbox"] = [1020.0, 210.0, 812.0, 760.0]
    _has(contract.check_world_track_semantics(tracks_doc), "bbox")


def test_duplicate_world_id(tracks_doc):
    tracks_doc["tracks"].append(dict(_track(tracks_doc)))
    _has(contract.check_world_track_semantics(tracks_doc), "같은 world_id")


def test_t_last_valid_after_t(tracks_doc):
    _track(tracks_doc)["t_last_valid"] = tracks_doc["t"] + 1
    _has(contract.check_world_track_semantics(tracks_doc), "t_last_valid")


def _shifted(doc, dt, **update):
    out = copy.deepcopy(doc)
    out["t"] += dt
    for tr in out["tracks"]:
        tr["t_last_valid"] = out["t"]
    out.update(update)
    return out


def test_stream_must_increase(tracks_doc):
    docs = [tracks_doc, _shifted(tracks_doc, 100), _shifted(tracks_doc, 100)]
    _has(contract.check_world_track_stream(docs), "시각 순서")


def test_stream_source_must_not_change(tracks_doc):
    docs = [tracks_doc, _shifted(tracks_doc, 100, provider_version="other")]
    _has(contract.check_world_track_stream(docs), "바뀌었다")


def test_stream_must_not_be_empty():
    _has(contract.check_world_track_stream([]), "없다")


def test_tracks_match_calibration(tracks_doc, calibration_doc):
    calibration_doc["calibration_version"] = tracks_doc["calibration_version"]
    cam = calibration_doc["cameras"][0]
    calibration_doc["cameras"] = [dict(cam, camera_id="hd_00_00"), dict(cam, camera_id="hd_00_03")]
    assert contract.check_tracks_match_calibration(tracks_doc, calibration_doc) == []

    calibration_doc["cameras"][0]["image_size"] = [960, 540]
    _has(contract.check_tracks_match_calibration(tracks_doc, calibration_doc), "영상 크기")


def test_tracks_against_other_calibration(tracks_doc, calibration_doc):
    _has(
        contract.check_tracks_match_calibration(tracks_doc, calibration_doc),
        "calibration_version 불일치",
        "보정에 없다",
    )


def test_schema_errors_reports_unknown_schema():
    _has(contract.schema_errors({}, "no_such"), "대응 스키마 없음")


def test_contracts_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv(contract.CONTRACTS_DIR_ENV, str(tmp_path))
    assert contract.contracts_dir() == tmp_path


def test_malformed_calibration_reports_schema_only(calibration_doc):
    calibration_doc["cameras"][0]["R"] = "identity"
    _has(contract.check_calibration(calibration_doc), "스키마")


def test_malformed_tracks_reports_schema_only(tracks_doc):
    _track(tracks_doc)["pelvis_xyz"] = [1.0]
    _has(contract.check_world_tracks(tracks_doc), "스키마")


# --------------------------------------------------------------------------- 독립 리뷰 음성 대조 (R1~R4)


class _OneDocProvider(WorldTrackProvider):
    def __init__(self, doc):
        self._doc = doc

    source = property(lambda self: self._doc["provider_source"])
    provider_version = property(lambda self: self._doc["provider_version"])

    def frames(self):
        return iter([copy.deepcopy(self._doc)])


def _one_view(doc):
    """기여·관측 카메라를 한 대로 줄인다."""
    tr = _track(doc)
    tr["cameras"] = tr["cameras"][:1]
    tr["per_camera"] = tr["per_camera"][:1]
    return tr


def test_r1_single_camera_observed_joint_rejected(tracks_doc):
    """리뷰 R1 재현: 한 대에서만 보였는데 observed 관절을 채우면 거부한다."""
    tracks_doc.update(provider_source="TIER0_LOG", provider_version="review-one-view")
    tr = _one_view(tracks_doc)
    tr["skeleton"] = _skeleton(joints={0: [1.2, 0.4, 1.65]}, visible={0: True}, validity={0: "VALID"})
    tr["pelvis_xyz"] = None
    _has(contract.check_world_tracks(tracks_doc), "observed 관절")
    _has(contract.check_world_track_provider(_OneDocProvider(tracks_doc)), "observed 관절")


def test_r1_single_camera_pelvis_rejected(tracks_doc):
    _one_view(tracks_doc)
    _has(contract.check_world_tracks(tracks_doc), "pelvis_xyz")


def test_r1_no_camera_pelvis_rejected(tracks_doc):
    """가려져 기여 카메라가 없는 예측 상태에서도 골반을 채우지 않는다."""
    tr = _track(tracks_doc)
    tr.update(cameras=[], per_camera=[], observation_state="PREDICTED")
    _has(contract.check_world_tracks(tracks_doc), "pelvis_xyz")


def test_r1_single_camera_ground_projection_allowed(tracks_doc):
    """검증된 표면 위 접지 투영(한 대)은 합법 경로다. 골반·관절이 비어 있으면 통과한다."""
    tr = _one_view(tracks_doc)
    tr.update(pelvis_xyz=None, head_z=None, position_xy=tr["ground_anchor_xyz"][:2])
    assert_no_violations(contract.check_world_tracks(tracks_doc))


def test_r1_single_camera_inferred_joint_allowed(tracks_doc):
    """한 대 영상으로 HMR 이 추정한 inferred 관절은 삼각측량 주장이 아니므로 카메라 수 검사 대상이 아니다."""
    tr = _one_view(tracks_doc)
    tr["pelvis_xyz"] = None
    tr["skeleton"] = _skeleton(
        joints={0: [1.2, 0.4, 1.65]}, visible={0: True}, validity={0: "UNCERTAIN"}, source={0: "inferred"}
    )
    assert_no_violations(contract.check_world_tracks(tracks_doc))


def test_r1_two_cameras_observed_joint_allowed(tracks_doc):
    _track(tracks_doc)["skeleton"] = _skeleton(joints={0: [1.2, 0.4, 1.65]}, visible={0: True}, validity={0: "VALID"})
    assert_no_violations(contract.check_world_tracks(tracks_doc))


def test_r2_inferred_valid_joint_allowed(tracks_doc):
    """리뷰 R2: 출처(inferred)와 유효성(VALID)은 따로다. 위험 판정에서 observed+VALID 만 쓰는 규칙은 risk 경계 몫이다."""
    _track(tracks_doc)["skeleton"] = _skeleton(
        joints={5: [1.0, 0.3, 1.4]}, visible={5: True}, validity={5: "VALID"}, source={5: "inferred"}
    )
    assert_no_violations(contract.check_world_tracks(tracks_doc))


def test_r4_camera_roll_is_not_an_axis_error(calibration_doc):
    """리뷰 R4 재현: 광축 주위 roll 180도(D=diag(-1,-1,1), R'=DR, t'=Dt)는 같은 카메라 중심의 정상 보정이다."""
    cam = calibration_doc["cameras"][0]
    cam["R"] = [[-v for v in cam["R"][0]], [-v for v in cam["R"][1]], list(cam["R"][2])]
    cam["t"] = [-cam["t"][0], -cam["t"][1], cam["t"][2]]
    center = contract.camera_center(cam["R"], cam["t"])
    assert center == pytest.approx([0.744066, 0.744066, 2.190402], abs=1e-6)
    assert_no_violations(contract.check_calibration(calibration_doc))
    # 바로 선 설치를 전제하는 현장은 선택 검사로 확인할 수 있다.
    _has(contract.check_cameras_upright(calibration_doc), "이미지 아래 방향")


def test_plausible_ranges_override(calibration_doc, tracks_doc):
    """경험적 범위는 구현·현장에 맞게 바꿀 수 있다."""
    low_ceiling = contract.PlausibleRanges(max_camera_height_m=2.0)
    _has(contract.check_calibration(calibration_doc, ranges=low_ceiling), "카메라 중심 Z")
    short_room = contract.PlausibleRanges(world_z_m=(-0.5, 1.0))
    _has(contract.check_world_tracks(tracks_doc, ranges=short_room), "head_z")
