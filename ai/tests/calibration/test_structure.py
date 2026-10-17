"""보정 문서 형식 검사(structure)가 계약 스키마와 같은 범위인지 대조한다 (리뷰 r2 F1).

버전 저장소는 jsonschema 없이 저장 전 계약 전체를 검사하므로, 형식 검사가 스키마보다 느슨하면 잘못된 후보가 채택된다.
여기서는 스키마 파일의 enum·필수 항목과 상수를 맞추고, 같은 문서 변형에 대해 jsonschema 와 판정이 같은지 본다.
"""

import copy
import json

import pytest

from simai_ai.calibration import CalibrationVersionStore, structure
from simai_ai.calibration.structure import check_calibration_structure
from simai_ai.providers import contract


def _schema(name):
    return json.loads((contract.contracts_dir() / name).read_text(encoding="utf-8"))


CAL = _schema("providers/calibration.schema.json")
DEFS = _schema("common/defs.schema.json")["$defs"]
CAM = CAL["$defs"]["camera_calibration"]


def test_enums_match_schema():
    assert structure.CALIBRATION_SOURCES == set(DEFS["calibration_source"]["enum"])
    assert structure.CALIBRATION_STATES == set(DEFS["calibration_state"]["enum"])
    assert structure.SCALE_BASES == set(CAL["$defs"]["scale_basis"]["enum"])
    assert structure.DISTORTION_MODELS == set(CAM["properties"]["distortion"]["properties"]["model"]["enum"])
    assert structure.EXTRINSIC_CONVENTION == CAL["properties"]["extrinsic_convention"]["const"]
    never = CAL["allOf"][0]["if"]["properties"]["provider_source"]["enum"]
    assert contract.NEVER_CALIBRATED_SOURCES == set(never)


def test_keys_match_schema():
    assert structure._TOP_REQUIRED == tuple(CAL["required"])
    assert structure._TOP_KEYS == set(CAL["properties"])
    assert structure._CAMERA_REQUIRED == tuple(CAM["required"])
    assert structure._CAMERA_KEYS == set(CAM["properties"])
    assert structure._VALIDATION_KEYS == set(CAL["properties"]["validation"]["properties"])
    frame = DEFS["coordinate_frame"]
    assert structure._FRAME_CONSTS == {k: frame["properties"][k]["const"] for k in frame["required"]}
    assert structure._SEMVER.pattern == DEFS["semver"]["pattern"]
    assert structure._CAMERA_ID.pattern == DEFS["camera_id"]["pattern"]
    assert structure._SHA256.pattern == DEFS["sha256"]["pattern"]


@pytest.fixture
def base(cameras, evidence, t0):
    store = CalibrationVersionStore(site_id="classroom-1")
    result = store.propose(
        provider_source="G1_AUTO", provider_version="g1", cameras=cameras, evidence=evidence, at_ms=t0
    )
    assert result.adopted and result.doc["state"] == "CALIBRATED"
    return result.doc


def _examples(invalid):
    root = contract.contracts_dir() / "examples"
    # calibration.json 과 calibration.<변형>.json 만 보정 문서다(calibration_update.json 은 I1 메시지 예시)
    base = root / "invalid" if invalid else root
    paths = sorted(p for p in base.glob("calibration*.json") if p.name == "calibration.json" or p.name.startswith("calibration."))
    assert paths
    docs = [(p.name, json.loads(p.read_text(encoding="utf-8"))) for p in paths]
    return [pytest.param({k: v for k, v in d.items() if not k.startswith("_")}, id=name) for name, d in docs]


@pytest.mark.parametrize("doc", _examples(invalid=False))
def test_valid_examples_pass(doc):
    assert contract.check_calibration_schema(doc) == []
    assert check_calibration_structure(doc) == []


@pytest.mark.parametrize("doc", _examples(invalid=True))
def test_invalid_examples_rejected(doc):
    assert contract.check_calibration_schema(doc)
    assert check_calibration_structure(doc)


_DROP = object()


def _set(doc, path, value):
    target = doc
    for key in path[:-1]:
        target = target[key]
    if value is _DROP:
        del target[path[-1]]
    else:
        target[path[-1]] = value


# (경로, 값). 값이 _DROP 이면 항목을 지운다. 모두 스키마 위반이다.
INVALID = [
    *[((key,), _DROP) for key in CAL["required"]],
    (("extra",), 1),
    (("schema_version",), "1.2"),
    (("schema_version",), 120),
    (("calibration_version",), ""),
    (("provider_source",), "BOGUS"),
    (("provider_version",), 1),
    (("state",), "OK"),
    (("created_at",), -1),
    (("created_at",), "1760342400000"),
    (("created_at",), True),
    (("coordinate_frame",), []),
    (("coordinate_frame", "unit"), "cm"),
    (("coordinate_frame", "up_axis"), "+Y"),
    (("coordinate_frame", "frame"), _DROP),
    (("coordinate_frame", "site_id"), 3),
    (("coordinate_frame", "extra"), "x"),
    (("scale_basis",), "TAPE"),
    (("scale_cross_check_basis",), []),
    (("scale_cross_check_basis",), ["MARKER", "MARKER"]),
    (("scale_cross_check_basis",), ["TAPE"]),
    (("scale_cross_check_basis",), "MARKER"),
    (("extrinsic_convention",), "OPENCV_CAMERA_TO_WORLD"),
    (("cameras",), []),
    (("cameras",), {}),
    (("cameras", 0), "cam1"),
    *[(("cameras", 0, key), _DROP) for key in CAM["required"]],
    (("cameras", 0, "extra"), 1),
    (("cameras", 0, "camera_id"), "cam 1"),
    (("cameras", 0, "camera_id"), ""),
    (("cameras", 0, "image_size"), [1920]),
    (("cameras", 0, "image_size"), [1920, 1080, 3]),
    (("cameras", 0, "image_size"), [0, 1080]),
    (("cameras", 0, "image_size"), [1920.5, 1080]),
    (("cameras", 0, "K"), [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0]]),
    (("cameras", 0, "K"), [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0, 0.0]]),
    (("cameras", 0, "K"), [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, "1"]]),
    (("cameras", 0, "K", 2, 2), True),
    (("cameras", 0, "R"), [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
    (("cameras", 0, "t"), [0.0, 0.0, 1.0, 99.0]),
    (("cameras", 0, "t"), [0.0, 0.0]),
    (("cameras", 0, "t"), [0.0, None, 1.0]),
    (("cameras", 0, "distortion"), []),
    (("cameras", 0, "distortion", "model"), "UNSUPPORTED"),
    (("cameras", 0, "distortion", "model"), _DROP),
    (("cameras", 0, "distortion", "coeffs"), _DROP),
    (("cameras", 0, "distortion", "coeffs"), ["0.1"]),
    (("cameras", 0, "distortion", "extra"), 1),
    (("cameras", 0, "camera_height_m"), "2.5"),
    (("cameras", 0, "reprojection_error_px"), 1.2),
    (("cameras", 0, "reprojection_error_px", "median"), -0.1),
    (("cameras", 0, "reprojection_error_px", "p95"), "3"),
    (("cameras", 0, "reprojection_error_px", "mean"), 1.0),
    (("validation",), _DROP),
    (("validation",), []),
    (("validation", "extra"), 1),
    (("validation", "checkpoint_count"), -1),
    (("validation", "checkpoint_count"), 3.5),
    (("validation", "checkpoint_count"), _DROP),
    (("validation", "max_head_height_error_m"), _DROP),
    (("validation", "max_pairwise_position_disagreement_m"), _DROP),
    (("validation", "max_pairwise_position_disagreement_m"), -0.01),
    (("validation", "floor_plane_disagreement_m"), "0.02"),
    (("validation", "floor_coverage_ratio"), 1.01),
    (("validation", "floor_coverage_ratio"), -0.01),
    (("content_hash",), "sha256:xyz"),
    (("content_hash",), "md5:" + "0" * 64),
]

# 스키마가 허용하는 변형. 형식 검사도 허용해야 한다(지나치게 엄격하면 정상 보정이 거절된다).
VALID = [
    (("provider_version",), _DROP),
    (("scale_cross_check_basis",), _DROP),
    (("content_hash",), _DROP),
    (("coordinate_frame", "site_id"), _DROP),
    (("cameras", 0, "camera_height_m"), _DROP),
    (("cameras", 0, "reprojection_error_px"), _DROP),
    (("cameras", 0, "reprojection_error_px"), {}),
    (("cameras", 0, "distortion"), {"model": "NONE", "coeffs": []}),
    (("cameras", 0, "K", 0, 0), 1000),
    (("validation", "max_position_error_m"), 0.1),
    (("validation", "floor_coverage_ratio"), 1),
    (("state",), "PROVISIONAL"),
]


def _ids(cases):
    return ["/".join(map(str, path)) + ("=drop" if value is _DROP else f"={value!r}") for path, value in cases]


@pytest.mark.parametrize(("path", "value"), INVALID, ids=_ids(INVALID))
def test_structure_rejects_what_schema_rejects(base, path, value):
    doc = copy.deepcopy(base)
    _set(doc, path, value)
    assert contract.check_calibration_schema(doc), "변형이 스키마 위반이어야 시험이 의미가 있다"
    assert check_calibration_structure(doc)


@pytest.mark.parametrize(("path", "value"), VALID, ids=_ids(VALID))
def test_structure_accepts_what_schema_accepts(base, path, value):
    doc = copy.deepcopy(base)
    _set(doc, path, value)
    assert contract.check_calibration_schema(doc) == []
    assert check_calibration_structure(doc) == []


@pytest.mark.parametrize("source", sorted(contract.NEVER_CALIBRATED_SOURCES))
@pytest.mark.parametrize("state", ["CALIBRATED", "DRIFTED"])
def test_never_calibrated_sources_match_schema(base, source, state):
    doc = {**base, "provider_source": source, "state": state}
    assert bool(contract.check_calibration_schema(doc)) == bool(check_calibration_structure(doc))


def test_calibrated_without_position_check_requires_one(base):
    doc = copy.deepcopy(base)
    del doc["validation"]["max_pairwise_position_disagreement_m"]
    doc["validation"].pop("max_position_error_m", None)
    assert contract.check_calibration_schema(doc) and check_calibration_structure(doc)
    doc["validation"]["max_position_error_m"] = 0.1
    assert contract.check_calibration_schema(doc) == [] == check_calibration_structure(doc)


def test_non_object_root():
    assert check_calibration_structure([]) == ["<root>: 객체가 아니다"]
