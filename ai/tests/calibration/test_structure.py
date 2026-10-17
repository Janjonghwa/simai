"""보정 문서 형식 검사(structure) = 계약 스키마 직접 검증 (명세 21.2 계약 우선, S15P31A101-152).

형식 규칙은 `contracts/providers/calibration.schema.json` 이 정본이고 코드로 옮기지 않는다. 여기서는
- 계약 예시와 문서 변형을 스키마대로 거절·허용하는지(이전 손 검사 대조 시험의 변형 목록을 회귀 시험으로 유지),
- `$ref`(common/defs) 가 해석되는지, 스키마 사본만 바꿔도 저장소의 저장 전 검사가 따라가는지,
- jsonschema 가 없을 때 import 가 설치 방법을 알려 주는지 본다.
"""

import copy
import json
import os
import shutil
import subprocess
import sys

import pytest
import referencing.exceptions

from simai_ai.calibration import CalibrationVersionStore, RejectReason
from simai_ai.calibration.structure import INSTALL_HINT, check_calibration_structure
from simai_ai.providers import contract


def _schema(name):
    return json.loads((contract.contracts_dir() / name).read_text(encoding="utf-8"))


CAL = _schema("providers/calibration.schema.json")
CAM = CAL["$defs"]["camera_calibration"]


def _propose(store, cameras, evidence, at_ms):
    return store.propose(
        provider_source="G1_AUTO", provider_version="g1", cameras=cameras, evidence=evidence, at_ms=at_ms
    )


@pytest.fixture
def base(cameras, evidence, t0):
    result = _propose(CalibrationVersionStore(site_id="classroom-1"), cameras, evidence, t0)
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
    assert check_calibration_structure(doc) == []


@pytest.mark.parametrize("doc", _examples(invalid=True))
def test_invalid_examples_rejected(doc):
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
    (("scale_cross_check_basis",), ["MEASURED_OBJECT", "MEASURED_OBJECT"]),
    (("scale_cross_check_basis",), ["TAPE"]),
    (("scale_cross_check_basis",), "MEASURED_OBJECT"),
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
def test_rejects_schema_violation(base, path, value):
    doc = copy.deepcopy(base)
    _set(doc, path, value)
    assert check_calibration_structure(doc)


@pytest.mark.parametrize(("path", "value"), VALID, ids=_ids(VALID))
def test_accepts_what_schema_allows(base, path, value):
    doc = copy.deepcopy(base)
    _set(doc, path, value)
    assert check_calibration_structure(doc) == []


@pytest.mark.parametrize("source", sorted(contract.NEVER_CALIBRATED_SOURCES))
def test_never_calibrated_sources(base, source):
    """allOf 1: 정답·시뮬레이터·모델 추정 출처는 CALIBRATED·DRIFTED 불가 (명세 20.4 규칙 4)."""
    for state in ("CALIBRATED", "DRIFTED"):
        assert check_calibration_structure({**base, "provider_source": source, "state": state})
    assert check_calibration_structure({**base, "provider_source": source, "state": "PROVISIONAL"}) == []


def test_calibrated_without_position_check_requires_one(base):
    doc = copy.deepcopy(base)
    del doc["validation"]["max_pairwise_position_disagreement_m"]
    doc["validation"].pop("max_position_error_m", None)
    assert check_calibration_structure(doc)
    doc["validation"]["max_position_error_m"] = 0.1
    assert check_calibration_structure(doc) == []


@pytest.mark.parametrize("doc", [[], "doc", None, 3])
def test_non_object_root(doc):
    assert check_calibration_structure(doc)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("cameras", 0, "camera_id"), "cam 1"),  # defs camera_id
        (("schema_version",), "1.2"),  # defs semver
        (("provider_source",), "BOGUS"),  # defs calibration_source
        (("coordinate_frame", "unit"), "cm"),  # defs coordinate_frame
    ],
)
def test_defs_refs_are_resolved(base, path, value):
    """calibration.schema.json 의 상대 $ref(../common/defs.schema.json) 가 defs 의 $id 로 해석된다."""
    doc = copy.deepcopy(base)
    _set(doc, path, value)
    errors = check_calibration_structure(doc)
    assert errors and all("/".join(map(str, path)) in e for e in errors), errors


# ------------------------------------------------------------------ 스키마가 정본
# 스키마는 contracts/ 위치별로 한 번 읽어 캐시한다(providers.contract._schemas). 그래서 사본을 먼저 고친 뒤
# SIMAI_CONTRACTS_DIR 로 가리키고, 같은 문서를 원본 위치와 사본 위치에서 각각 검사해 결과를 비교한다.


@pytest.fixture
def edited_contracts(tmp_path, monkeypatch):
    """contracts/ 스키마 사본을 고치고 SIMAI_CONTRACTS_DIR 로 가리키는 함수를 준다."""

    def make(rel, change):
        root = tmp_path / "contracts"
        shutil.copytree(contract.contracts_dir(), root, ignore=shutil.ignore_patterns("examples", "scripts"))
        path = root / rel
        if change is None:
            path.unlink()
        else:
            schema = json.loads(path.read_text(encoding="utf-8"))
            change(schema)
            path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv(contract.CONTRACTS_DIR_ENV, str(root))
        return root

    return make


def test_store_follows_defs_change(edited_contracts, cameras, evidence, t0):
    """defs 의 camera_id 패턴만 바꾸면 코드 수정 없이 저장 전 검사가 따라간다($ref 경유)."""
    assert _propose(CalibrationVersionStore(site_id="classroom-1"), cameras, evidence, t0).adopted

    edited_contracts("common/defs.schema.json", lambda s: s["$defs"]["camera_id"].update(pattern="^CAM[0-9]+$"))
    store = CalibrationVersionStore(site_id="classroom-1")
    result = _propose(store, cameras, evidence, t0)
    assert not result.adopted and result.reject_reason is RejectReason.CONTRACT_VIOLATION
    assert any("cameras/0/camera_id" in v for v in result.violations), result.violations
    assert store.versions == () and store.snapshot() is None


def test_store_follows_calibration_schema_change(edited_contracts, base):
    """calibration.schema.json 의 규칙(필수 항목·허용 범위)을 바꾸면 결과가 바뀐다."""
    doc = copy.deepcopy(base)
    del doc["provider_version"]
    doc["cameras"][0]["image_size"] = [640, 480]
    assert check_calibration_structure(doc) == []

    def change(schema):
        schema["required"].append("provider_version")
        schema["$defs"]["camera_calibration"]["properties"]["image_size"]["items"]["minimum"] = 1280

    edited_contracts("providers/calibration.schema.json", change)
    errors = check_calibration_structure(doc)
    assert any("provider_version" in e for e in errors), errors
    assert any("cameras/0/image_size/0" in e for e in errors), errors


def test_unresolvable_ref_fails_loudly(edited_contracts, cameras, evidence, t0):
    """defs 를 찾지 못하면 형식 검사를 건너뛰어 통과시키지 않고 예외를 낸다. 저장소는 바뀌지 않는다."""
    edited_contracts("common/defs.schema.json", None)
    store = CalibrationVersionStore(site_id="classroom-1")
    with pytest.raises(referencing.exceptions.Unresolvable):
        _propose(store, cameras, evidence, t0)
    assert store.versions == () and store.transitions == () and store.snapshot() is None


# ------------------------------------------------------------------ extras 없는 설치


def test_import_without_jsonschema_explains_install():
    """jsonschema 가 없는 설치(extras calibration 없음)에서 import 하면 설치 방법을 적은 ImportError 가 난다."""
    code = "\n".join(
        [
            "import sys",
            "sys.modules['jsonschema'] = None",
            "try:",
            "    import simai_ai.calibration",
            "except ImportError as e:",
            "    print(e)",
            "    sys.exit(3)",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.strip() == INSTALL_HINT
    assert "--extra calibration" in result.stdout
