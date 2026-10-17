"""보정 문서의 계약 형식 검사 (`contracts/providers/calibration.schema.json` 과 같은 범위, jsonschema 없이).

버전 저장소는 기본 설치(jsonschema 없음)에서도 저장 전에 계약 전체를 검증해야 한다(명세 4.5 검증 후 원자적 전환,
리뷰 r2 F1). 그래서 스키마가 보는 형식·enum·패턴·개수·범위·조건부 필수 항목을 여기서 그대로 본다.

- 기하 검사(`providers.contract.check_calibration_geometry`)는 스키마 형식이 맞다는 전제로 회전·내부 파라미터·카메라
  중심을 계산한다. 이 검사가 실패한 문서에는 기하 검사를 돌리지 않는다.
- NaN·무한대는 스키마로 막을 수 없어 기하 검사(`_check_calibration_values`)가 본다. 여기서는 타입만 본다.
- 스키마와 이 검사가 어긋나지 않는지는 시험(`tests/calibration/test_structure.py`)이 스키마 파일과 jsonschema 로
  대조한다. 스키마를 고치면 이 파일도 함께 고친다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from simai_ai.calibration.criteria import ScaleBasis
from simai_ai.providers.contract import NEVER_CALIBRATED_SOURCES
from simai_ai.tracking.system_health import CalibrationState

# common/defs.schema.json calibration_source
CALIBRATION_SOURCES = frozenset({"G1_AUTO", "MANUAL_PNP", "CHARUCO", "DATASET_GT", "SIM", "MODEL_ESTIMATE"})
CALIBRATION_STATES = frozenset(str(s) for s in CalibrationState)
SCALE_BASES = frozenset(str(b) for b in ScaleBasis)
DISTORTION_MODELS = frozenset({"OPENCV_PINHOLE", "OPENCV_FISHEYE", "NONE"})
EXTRINSIC_CONVENTION = "OPENCV_WORLD_TO_CAMERA"

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

_TOP_REQUIRED = (
    "schema_version",
    "calibration_version",
    "provider_source",
    "state",
    "created_at",
    "coordinate_frame",
    "scale_basis",
    "extrinsic_convention",
    "cameras",
)
_TOP_KEYS = frozenset(_TOP_REQUIRED) | {"provider_version", "scale_cross_check_basis", "validation", "content_hash"}
_FRAME_CONSTS = {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"}
_CAMERA_REQUIRED = ("camera_id", "image_size", "K", "distortion", "R", "t")
_CAMERA_KEYS = frozenset(_CAMERA_REQUIRED) | {"camera_height_m", "reprojection_error_px"}
# validation 키 → 상한(없으면 None). checkpoint_count 는 정수라 따로 본다.
_VALIDATION_NUMBERS: dict[str, float | None] = {
    "max_position_error_m": None,
    "max_pairwise_position_disagreement_m": None,
    "max_head_height_error_m": None,
    "floor_plane_disagreement_m": None,
    "floor_coverage_ratio": 1.0,
}
_VALIDATION_KEYS = frozenset(_VALIDATION_NUMBERS) | {"checkpoint_count"}


def is_number(value: Any) -> bool:
    """JSON Schema number (bool 제외). 유한성은 보지 않는다."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_vec3(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(is_number(v) for v in value)


def is_mat3(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(is_vec3(row) for row in value)


def check_calibration_structure(doc: Any) -> list[str]:
    """보정 문서가 계약 스키마의 형식을 지키는지 본다. 위반 메시지 목록(비면 통과)."""
    out: list[str] = []
    if not isinstance(doc, Mapping):
        return ["<root>: 객체가 아니다"]
    _keys(doc, _TOP_REQUIRED, _TOP_KEYS, "<root>", out)

    _field(doc, "schema_version", lambda v: isinstance(v, str) and bool(_SEMVER.search(v)), "semver 문자열", out)
    _field(doc, "calibration_version", lambda v: isinstance(v, str) and len(v) >= 1, "비지 않은 문자열", out)
    _enum_field(doc, "provider_source", CALIBRATION_SOURCES, out)
    _field(doc, "provider_version", lambda v: isinstance(v, str), "문자열", out)
    _enum_field(doc, "state", CALIBRATION_STATES, out)
    _field(doc, "created_at", lambda v: is_integer(v) and v >= 0, "0 이상의 정수(UTC ms)", out)
    _enum_field(doc, "scale_basis", SCALE_BASES, out)
    _field(doc, "extrinsic_convention", lambda v: v == EXTRINSIC_CONVENTION, EXTRINSIC_CONVENTION, out)
    _field(doc, "content_hash", lambda v: isinstance(v, str) and bool(_SHA256.search(v)), "sha256:<64 hex>", out)

    if "coordinate_frame" in doc:
        _check_frame(doc["coordinate_frame"], out)
    if "scale_cross_check_basis" in doc:
        _check_cross_checks(doc["scale_cross_check_basis"], out)
    if "cameras" in doc:
        cams = doc["cameras"]
        if not isinstance(cams, list) or not cams:
            out.append("cameras: 카메라가 하나 이상인 배열이어야 한다")
        else:
            for i, cam in enumerate(cams):
                _check_camera(cam, f"cameras/{i}", out)
    if "validation" in doc:
        _check_validation(doc["validation"], out)

    # allOf 1: 정답·시뮬레이터·모델 추정 카메라는 CALIBRATED·DRIFTED 불가 (명세 20.4 규칙 4)
    if doc.get("provider_source") in NEVER_CALIBRATED_SOURCES and "state" in doc:
        if doc["state"] not in ("UNCALIBRATED", "PROVISIONAL"):
            out.append(f"state: {doc['provider_source']} 출처는 UNCALIBRATED·PROVISIONAL 만 된다 ({doc['state']!r})")
    # allOf 2: CALIBRATED 는 독립 검증점 결과가 있어야 한다 (명세 4.4)
    if doc.get("state") == "CALIBRATED":
        v = doc.get("validation")
        if not isinstance(v, Mapping):
            out.append("validation: CALIBRATED 는 validation 이 있어야 한다")
        else:
            for key in ("checkpoint_count", "max_head_height_error_m"):
                if key not in v:
                    out.append(f"validation.{key}: CALIBRATED 는 필수")
            if "max_pairwise_position_disagreement_m" not in v and "max_position_error_m" not in v:
                out.append(
                    "validation: CALIBRATED 는 max_pairwise_position_disagreement_m 또는 max_position_error_m 이 필수"
                )
    return out


def _enum(values: frozenset[str]) -> str:
    return "다음 중 하나 " + ", ".join(sorted(values))


def _keys(
    obj: Mapping[str, Any], required: tuple[str, ...], allowed: frozenset[str], where: str, out: list[str]
) -> None:
    out += [f"{where}: 필수 항목 {k} 없음" for k in required if k not in obj]
    out += [f"{where}: 허용하지 않는 항목 {k!r}" for k in obj if k not in allowed]


def _field(
    obj: Mapping[str, Any], key: str, ok: Callable[[Any], bool], expected: str, out: list[str], where: str = ""
) -> None:
    if key in obj and not ok(obj[key]):
        out.append(f"{where}{key} = {obj[key]!r}: {expected} 이어야 한다")


def _enum_field(obj: Mapping[str, Any], key: str, values: frozenset[str], out: list[str], where: str = "") -> None:
    _field(obj, key, lambda v: isinstance(v, str) and v in values, _enum(values), out, where)


def _check_frame(frame: Any, out: list[str]) -> None:
    if not isinstance(frame, Mapping):
        out.append("coordinate_frame: 객체가 아니다")
        return
    _keys(frame, tuple(_FRAME_CONSTS), frozenset(_FRAME_CONSTS) | {"site_id"}, "coordinate_frame", out)
    for key, const in _FRAME_CONSTS.items():
        _field(frame, key, lambda v, c=const: v == c, repr(const), out, "coordinate_frame.")
    _field(frame, "site_id", lambda v: isinstance(v, str), "문자열", out, "coordinate_frame.")


def _check_cross_checks(value: Any, out: list[str]) -> None:
    if not isinstance(value, list) or not value:
        out.append("scale_cross_check_basis: 비지 않은 배열이어야 한다")
        return
    if any(not isinstance(b, str) or b not in SCALE_BASES for b in value):
        out.append(f"scale_cross_check_basis = {value!r}: 원소는 {_enum(SCALE_BASES)}")
    elif len(set(value)) != len(value):
        out.append(f"scale_cross_check_basis = {value!r}: 원소가 중복된다")


def _check_camera(cam: Any, where: str, out: list[str]) -> None:
    if not isinstance(cam, Mapping):
        out.append(f"{where}: 객체가 아니다")
        return
    _keys(cam, _CAMERA_REQUIRED, _CAMERA_KEYS, where, out)
    w = f"{where}/"
    _field(cam, "camera_id", lambda v: isinstance(v, str) and bool(_CAMERA_ID.search(v)), "[A-Za-z0-9_-]+", out, w)
    _field(
        cam,
        "image_size",
        lambda v: isinstance(v, list) and len(v) == 2 and all(is_integer(x) and x >= 1 for x in v),
        "[width, height] 1 이상의 정수 두 개",
        out,
        w,
    )
    _field(cam, "K", is_mat3, "3x3 수치 행렬", out, w)
    _field(cam, "R", is_mat3, "3x3 수치 행렬", out, w)
    _field(cam, "t", is_vec3, "수치 3개", out, w)
    _field(cam, "camera_height_m", is_number, "수치", out, w)
    if "distortion" in cam:
        dist = cam["distortion"]
        if not isinstance(dist, Mapping):
            out.append(f"{w}distortion: 객체가 아니다")
        else:
            _keys(dist, ("model", "coeffs"), frozenset({"model", "coeffs"}), f"{w}distortion", out)
            dw = f"{w}distortion/"
            _enum_field(dist, "model", DISTORTION_MODELS, out, dw)
            _field(dist, "coeffs", lambda v: isinstance(v, list) and all(is_number(x) for x in v), "수치 배열", out, dw)
    if "reprojection_error_px" in cam:
        rep = cam["reprojection_error_px"]
        if not isinstance(rep, Mapping):
            out.append(f"{w}reprojection_error_px: 객체가 아니다")
        else:
            _keys(rep, (), frozenset({"median", "p95"}), f"{w}reprojection_error_px", out)
            for key in ("median", "p95"):
                _field(rep, key, lambda v: is_number(v) and not v < 0, "0 이상의 수치", out, f"{w}reprojection_error_px/")


def _check_validation(v: Any, out: list[str]) -> None:
    if not isinstance(v, Mapping):
        out.append("validation: 객체가 아니다")
        return
    _keys(v, (), _VALIDATION_KEYS, "validation", out)
    _field(v, "checkpoint_count", lambda x: is_integer(x) and x >= 0, "0 이상의 정수", out, "validation.")
    for key, upper in _VALIDATION_NUMBERS.items():
        _field(
            v,
            key,
            lambda x, u=upper: is_number(x) and not x < 0 and (u is None or not x > u),
            "0 이상" + ("" if upper is None else f" {upper} 이하") + "의 수치",
            out,
            "validation.",
        )
