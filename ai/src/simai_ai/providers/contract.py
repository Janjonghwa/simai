"""Provider 공통 계약 시험의 검사 함수 (명세 20.4 규칙 6, 21.3).

모든 Provider 구현(DATASET_GT, SIM, MANUAL_PNP, CHARUCO, G1_AUTO, TIER0_LOG 등)이 같은 검사를 통과해야 한다.
검사 함수는 위반 설명 목록을 돌려준다(빈 목록이면 통과). pytest 에서 쓰는 묶음은 `simai_ai.providers.testing`.

검사 범주:
- 스키마: `contracts/` JSON Schema 검증 (jsonschema 필요, dev 그룹)
- 회전: R 직교·det=+1
- 단위·축(경험적 범위): `PlausibleRanges` 의 교실·인체 범위로 mm·cm 단위와 Z-down World 같은 명백한 오류를 잡는다.
  범위를 통과했다고 단위·축이 맞다는 증명은 아니다(Y-up World 등은 알려진 World 기준점과 비교해야 한다).
  구현·현장에 맞게 범위를 바꿀 수 있다.
- Z=0 대체 금지: 접지를 모르면 ground_anchor_xyz 는 비운다
- 2대 미만 관측: 기여·관측 카메라가 2대 미만이면 삼각측량 결과(pelvis_xyz, observed 관절)는 비운다
- 보정 상태: calibration_source 별 CALIBRATED 규칙과 명세 4.4 기준 중 문서로 표현된 항목
- 출처: provider_source·버전이 input_provenance 로 그대로 전달되는지

스키마로 막는 규칙도 검사 함수에서 다시 본다. 스키마 검증과 별개로 이유가 드러나는 위반 메시지를 주기 위함이다.
계약에서 바로 나오는 허용치는 모듈 상수, 경험적 범위는 `PlausibleRanges`, CALIBRATED 기준은 `CalibratedCriteria` 다.
위험 판정에서 observed+VALID 관절만 확정 근거로 쓰는 규칙(명세 5.6)은 Provider 계약이 아니라 risk 소비 경계의 규칙이다.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from simai_ai.providers.base import (
    FRAME_SOURCES,
    TIMESTAMP_SOURCES,
    CalibrationProvider,
    Frame,
    FrameSource,
    WorldTrackProvider,
)
from simai_ai.providers.provenance import is_upper_bound

CONTRACTS_DIR_ENV = "SIMAI_CONTRACTS_DIR"
DEFS_ID = "https://simai.dev/contracts/common/defs.schema.json"

# 관절 배치별 관절 수. MHR_NATIVE 는 길이가 정해져 있지 않아 길이 검사를 하지 않는다.
JOINT_COUNTS = {"COCO17": 17, "HALPE26": 26, "MHR70": 70}

# 수치 허용치
ROTATION_TOL = 1e-3  # R·Rᵀ = I, det(R) = 1 의 원소별 허용 오차 (예시는 소수 4자리로 적는다)
CAMERA_HEIGHT_TOL_M = 0.05  # camera_height_m 과 카메라 중심 Z 의 차이
# 삼각측량에 필요한 최소 카메라 수 (명세 5.2: 2대 이상에서 보인 관절만 삼각측량)
MIN_TRIANGULATION_CAMERAS = 2

# 선택 검사(check_cameras_upright): 이미지 아래 방향(카메라 +y)의 World Z 성분 상한.
# 바로 선 카메라는 0 이하다(천장에서 수직으로 내려다보면 0 근처라 여유를 둔다). 카메라 roll 은 계약이 제한하지 않는다.
CAMERA_DOWN_MAX_Z = 0.1

CALIBRATED_SOURCES = frozenset({"G1_AUTO", "MANUAL_PNP", "CHARUCO"})
NEVER_CALIBRATED_SOURCES = frozenset({"DATASET_GT", "SIM", "MODEL_ESTIMATE"})


@dataclass(frozen=True)
class CalibratedCriteria:
    """명세 4.4 CALIBRATED 기준(초기값). 독립 검증점 결과는 필수, 나머지는 값이 있을 때만 본다."""

    min_checkpoints: int = 3
    max_position_error_m: float = 0.30
    max_head_height_error_m: float = 0.15
    max_reprojection_median_px: float = 2.0
    max_reprojection_p95_px: float = 5.0
    max_floor_plane_disagreement_m: float = 0.03
    min_floor_coverage_ratio: float = 0.8


DEFAULT_CALIBRATED_CRITERIA = CalibratedCriteria()


@dataclass(frozen=True)
class PlausibleRanges:
    """단위·축 검사에 쓰는 경험적 범위 (교실 기준). 계약이 정한 값이 아니라 명백한 오류를 잡는 휴리스틱이다.

    mm·cm 단위를 쓰면 값이 수백~수천이 되어 걸리고, Z-down World 는 높이가 음수가 되어 걸린다.
    """

    max_camera_height_m: float = 20.0
    world_z_m: tuple[float, float] = (-0.5, 10.0)
    max_horizontal_m: float = 100.0
    # 접지점 기준 골반·머리 높이 (앉기·쪼그리기·누움 포함). 머리가 골반보다 낮은 자세가 있어 둘의 순서는 보지 않는다.
    pelvis_above_anchor_m: tuple[float, float] = (0.0, 1.6)
    head_above_anchor_m: tuple[float, float] = (0.0, 2.5)


DEFAULT_RANGES = PlausibleRanges()


# --------------------------------------------------------------------------- 스키마


def contracts_dir() -> Path:
    """저장소의 contracts/ 위치. 환경 변수 SIMAI_CONTRACTS_DIR 이 있으면 그것을 쓴다."""
    override = os.environ.get(CONTRACTS_DIR_ENV)
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts"
        if (candidate / "common" / "defs.schema.json").is_file():
            return candidate
    raise FileNotFoundError(f"contracts/ 를 찾지 못했다. {CONTRACTS_DIR_ENV} 로 경로를 지정한다")


@cache
def _schemas(root: Path) -> tuple[Any, dict[str, dict[str, Any]]]:
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError as e:  # pragma: no cover - dev 그룹이 없는 설치
        raise ImportError("스키마 검증에는 jsonschema 가 필요하다 (ai/ 에서 uv sync, dev 그룹)") from e

    registry = Registry()
    by_name: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        by_name[path.name.removesuffix(".schema.json")] = schema
    return registry, by_name


def schema_errors(doc: Any, schema: str | Mapping[str, Any]) -> list[str]:
    """doc 을 스키마로 검증한다. schema 는 이름(예: "calibration") 또는 스키마 객체({"$ref": ...})."""
    from jsonschema import Draft202012Validator

    registry, by_name = _schemas(contracts_dir().resolve())
    if isinstance(schema, str):
        if schema not in by_name:
            return [f"대응 스키마 없음: {schema}.schema.json"]
        schema = by_name[schema]
    validator = Draft202012Validator(dict(schema), registry=registry)
    return [
        f"스키마 {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    ]


def defs_ref(name: str) -> dict[str, str]:
    """common/defs.schema.json 의 정의 하나를 가리키는 스키마."""
    return {"$ref": f"{DEFS_ID}#/$defs/{name}"}


# --------------------------------------------------------------------------- 기하


def _matmul_t(a: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * a[j][k] for k in range(3)) for j in range(3)] for i in range(3)]


def _det3(m: Sequence[Sequence[float]]) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def camera_center(R: Sequence[Sequence[float]], t: Sequence[float]) -> list[float]:
    """OPENCV_WORLD_TO_CAMERA(x_cam = R·x_world + t) 의 카메라 중심 C = -Rᵀ·t (World, 미터)."""
    return [-sum(R[i][j] * t[i] for i in range(3)) for j in range(3)]


def check_rotation(R: Sequence[Sequence[float]], tol: float = ROTATION_TOL, where: str = "R") -> list[str]:
    """R 이 회전 행렬인지(직교, det=+1) 본다."""
    if len(R) != 3 or any(len(row) != 3 for row in R):
        return [f"{where}: 3x3 행렬이 아니다"]
    out = []
    rrt = _matmul_t(R)
    worst = max(abs(rrt[i][j] - (1.0 if i == j else 0.0)) for i in range(3) for j in range(3))
    if worst > tol:
        out.append(f"{where}: 직교가 아니다 (|R·Rᵀ - I| 최대 {worst:.2e} > {tol:.0e})")
    det = _det3(R)
    if abs(det - 1.0) > tol:
        out.append(f"{where}: det(R) = {det:.6f}, +1 이어야 한다 (반사 행렬 또는 축척 포함)")
    return out


# --------------------------------------------------------------------------- 보정


def check_calibration_schema(doc: Mapping[str, Any]) -> list[str]:
    return schema_errors(doc, "calibration")


def check_calibration_geometry(doc: Mapping[str, Any], ranges: PlausibleRanges = DEFAULT_RANGES) -> list[str]:
    """회전과 단위·축, 카메라 내부 파라미터를 본다. 카메라 설치 자세(roll)는 보지 않는다(check_cameras_upright)."""
    out: list[str] = []
    seen: set[str] = set()
    for i, cam in enumerate(doc.get("cameras", [])):
        where = f"cameras[{i}]({cam.get('camera_id')})"
        if cam.get("camera_id") in seen:
            out.append(f"{where}: camera_id 중복")
        seen.add(cam.get("camera_id"))

        R, t = cam.get("R"), cam.get("t")
        rot = check_rotation(R, where=f"{where}.R") if R is not None else [f"{where}: R 없음"]
        out += rot
        if not rot and t is not None and len(t) == 3:
            center = camera_center(R, t)
            if not 0.0 < center[2] <= ranges.max_camera_height_m:
                out.append(
                    f"{where}: 카메라 중심 Z = {center[2]:.3f} m. "
                    f"바닥(Z=0) 위 {ranges.max_camera_height_m} m 이내여야 한다 "
                    "(미터 단위, +Z 상방, World → 카메라 규약 확인)"
                )
            height = cam.get("camera_height_m")
            if height is not None and abs(height - center[2]) > CAMERA_HEIGHT_TOL_M:
                out.append(
                    f"{where}: camera_height_m = {height} 이 카메라 중심 Z = {center[2]:.3f} 와 "
                    f"{CAMERA_HEIGHT_TOL_M} m 넘게 다르다"
                )

        out += _check_intrinsics(cam, where)
    return out


def check_cameras_upright(doc: Mapping[str, Any], max_down_z: float = CAMERA_DOWN_MAX_Z) -> list[str]:
    """선택 검사: 카메라가 바로 서 있는지(이미지 아래 방향이 World 위쪽을 향하지 않는지) 본다.

    공통 계약이 아니다. 광축 주위로 돌려 단 카메라도 정상 보정이므로, 바로 선 설치를 전제하는 현장·구현만 쓴다.
    """
    out = []
    for i, cam in enumerate(doc.get("cameras", [])):
        R = cam.get("R")
        if R is not None and R[1][2] > max_down_z:
            out.append(
                f"cameras[{i}]({cam.get('camera_id')}): 이미지 아래 방향이 World 위쪽을 향한다 "
                f"(R[1][2] = {R[1][2]:.3f}). 설치 자세가 바로 선 카메라인지 확인"
            )
    return out


def _check_intrinsics(cam: Mapping[str, Any], where: str) -> list[str]:
    out: list[str] = []
    K, size = cam.get("K"), cam.get("image_size")
    if K is not None and len(K) == 3 and all(len(r) == 3 for r in K):
        if K[1][0] != 0 or K[2] != [0, 0, 1]:
            out.append(f"{where}.K: [[fx, s, cx], [0, fy, cy], [0, 0, 1]] 형식이 아니다")
        if not (K[0][0] > 0 and K[1][1] > 0):
            out.append(f"{where}.K: 초점 거리 fx, fy 는 양수여야 한다")
        if size is not None and len(size) == 2 and not (0 < K[0][2] < size[0] and 0 < K[1][2] < size[1]):
            out.append(f"{where}.K: 주점 ({K[0][2]}, {K[1][2]}) 이 영상 {size} 밖이다 (해상도 불일치)")
    dist = cam.get("distortion") or {}
    n, model = len(dist.get("coeffs", [])), dist.get("model")
    if model == "NONE" and n != 0:
        out.append(f"{where}.distortion: NONE 이면 coeffs 는 비어야 한다")
    elif model == "OPENCV_FISHEYE" and n != 4:
        out.append(f"{where}.distortion: OPENCV_FISHEYE 는 계수 4개 [k1, k2, k3, k4]")
    elif model == "OPENCV_PINHOLE" and n not in (4, 5, 8, 12, 14):
        out.append(f"{where}.distortion: OPENCV_PINHOLE 계수 개수 {n} 은 OpenCV 가 받지 않는다 (4, 5, 8, 12, 14)")
    return out


def check_calibration_state(
    doc: Mapping[str, Any], criteria: CalibratedCriteria = DEFAULT_CALIBRATED_CRITERIA
) -> list[str]:
    """calibration_source 별 CALIBRATED 규칙 (명세 20.4 규칙 4, 4.4).

    검사 범위: DATASET_GT·SIM·MODEL_ESTIMATE 는 CALIBRATED 불가, 그 밖의 출처는 독립 검증점 결과(개수·위치 오차·
    머리 높이 오차)가 필수다. 재투영 오차·바닥 평면 차이·커버리지는 문서에 값이 있을 때만 본다.
    스케일 근거의 세부 조건(사람 20명 이상의 키 통계 등)처럼 스키마에 표현되지 않은 4.4 조건은 보지 않는다.
    이 검사를 통과했다고 운영 CALIBRATED 조건 전부를 검증한 것은 아니다.
    """
    source, state = doc.get("provider_source"), doc.get("state")
    if state != "CALIBRATED":
        return []
    if source in NEVER_CALIBRATED_SOURCES:
        return [f"{source} 보정은 우리 현장의 검증된 보정이 아니므로 CALIBRATED 가 될 수 없다"]
    if source not in CALIBRATED_SOURCES:
        return [f"알 수 없는 출처 {source} 는 CALIBRATED 가 될 수 없다"]

    v = doc.get("validation")
    if not v:
        return ["CALIBRATED 는 독립 검증점 결과(validation)가 있어야 한다"]
    out: list[str] = []
    c = criteria
    if v.get("checkpoint_count", 0) < c.min_checkpoints:
        out.append(f"CALIBRATED: 독립 검증점 {v.get('checkpoint_count', 0)}곳 < {c.min_checkpoints}곳")
    _at_most(out, v, "max_position_error_m", c.max_position_error_m, required=True)
    _at_most(out, v, "max_head_height_error_m", c.max_head_height_error_m, required=True)
    _at_most(out, v, "floor_plane_disagreement_m", c.max_floor_plane_disagreement_m)
    if v.get("floor_coverage_ratio") is not None and v["floor_coverage_ratio"] < c.min_floor_coverage_ratio:
        out.append(f"CALIBRATED: floor_coverage_ratio {v['floor_coverage_ratio']} < {c.min_floor_coverage_ratio}")
    for cam in doc.get("cameras", []):
        rep = cam.get("reprojection_error_px") or {}
        _at_most(out, rep, "median", c.max_reprojection_median_px, label=f"{cam.get('camera_id')} 재투영 오차")
        _at_most(out, rep, "p95", c.max_reprojection_p95_px, label=f"{cam.get('camera_id')} 재투영 오차")
    return out


def _at_most(
    out: list[str], d: Mapping[str, Any], key: str, limit: float, required: bool = False, label: str = ""
) -> None:
    value = d.get(key)
    name = f"{label} {key}".strip()
    if value is None:
        if required:
            out.append(f"CALIBRATED: {name} 이 없다")
    elif value > limit:
        out.append(f"CALIBRATED: {name} = {value} > {limit}")


def check_calibration(
    doc: Mapping[str, Any],
    criteria: CalibratedCriteria = DEFAULT_CALIBRATED_CRITERIA,
    ranges: PlausibleRanges = DEFAULT_RANGES,
) -> list[str]:
    """보정 문서 하나에 대한 모든 검사. 스키마를 통과하지 못하면 형식을 전제하는 기하 검사는 건너뛴다."""
    errors = check_calibration_schema(doc)
    if not errors:
        errors += check_calibration_geometry(doc, ranges)
    return errors + check_calibration_state(doc, criteria)


def check_calibration_provenance(provider: CalibrationProvider) -> list[str]:
    """provenance() 가 문서의 provider_source·calibration_version 을 그대로 전달하는지 본다."""
    doc = provider.current()
    prov = provider.provenance()
    out = schema_errors(prov, {"$ref": f"{DEFS_ID}#/$defs/input_provenance/properties/calibration"})
    expected = {"source": doc.get("provider_source"), "calibration_version": doc.get("calibration_version")}
    if dict(prov) != expected:
        out.append(f"provenance() = {dict(prov)} 이 문서의 출처 {expected} 와 다르다")
    return out


def check_calibration_provider(
    provider: CalibrationProvider,
    criteria: CalibratedCriteria = DEFAULT_CALIBRATED_CRITERIA,
    ranges: PlausibleRanges = DEFAULT_RANGES,
) -> list[str]:
    return check_calibration(provider.current(), criteria, ranges) + check_calibration_provenance(provider)


# --------------------------------------------------------------------------- World 트랙


def check_world_tracks_schema(doc: Mapping[str, Any]) -> list[str]:
    return schema_errors(doc, "world_tracks")


def _tracks(doc: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for i, tr in enumerate(doc.get("tracks", [])):
        yield f"t={doc.get('t')} tracks[{i}]({tr.get('world_id')})", tr


def check_ground_anchor(doc: Mapping[str, Any]) -> list[str]:
    """Z=0 대체 금지 (명세 5.2, 20.4 규칙 1). 접지를 모르면 ground_anchor_xyz 는 null 이다."""
    out = []
    for where, tr in _tracks(doc):
        if tr.get("ground_anchor_xyz") is None:
            continue
        reasons = []
        if tr.get("support_surface_validity") != "VALID":
            reasons.append(f"support_surface_validity={tr.get('support_surface_validity')}")
        if tr.get("z_validity") == "UNAVAILABLE":
            reasons.append("z_validity=UNAVAILABLE")
        if tr.get("contact_validity") == "UNAVAILABLE":
            reasons.append("contact_validity=UNAVAILABLE")
        if tr.get("support_surface_id") is None:
            reasons.append("support_surface_id=null")
        if reasons:
            out.append(f"{where}: 접지 미확인({', '.join(reasons)})인데 ground_anchor_xyz 를 채웠다 (Z=0 대체 금지)")
    return out


def check_world_units_axes(doc: Mapping[str, Any], ranges: PlausibleRanges = DEFAULT_RANGES) -> list[str]:
    """미터·+Z 상방을 경험적 값 범위로 본다. 골반·머리는 접지점 위에 있어야 한다.

    머리가 골반보다 낮은 자세(허리 숙임, 낙상)가 있으므로 머리와 골반의 높이 순서는 보지 않는다.
    """
    z_range = ranges.world_z_m

    def _z_ok(z: float) -> bool:
        return z_range[0] <= z <= z_range[1]

    out = []
    for where, tr in _tracks(doc):
        pelvis, anchor, head_z = tr.get("pelvis_xyz"), tr.get("ground_anchor_xyz"), tr.get("head_z")
        xy = tr.get("position_xy") or []
        if any(abs(v) > ranges.max_horizontal_m for v in xy):
            out.append(f"{where}: position_xy {xy} 가 {ranges.max_horizontal_m} m 를 넘는다 (미터 단위 확인)")
        named = [("pelvis_xyz", pelvis), ("ground_anchor_xyz", anchor)]
        for name, p in named:
            if p is not None and not _z_ok(p[2]):
                out.append(f"{where}: {name} Z = {p[2]} 가 {z_range} m 밖이다 (미터, +Z 상방 확인)")
        if head_z is not None and not _z_ok(head_z):
            out.append(f"{where}: head_z = {head_z} 가 {z_range} m 밖이다")
        if anchor is not None:
            if pelvis is not None and not _within(pelvis[2] - anchor[2], ranges.pelvis_above_anchor_m):
                out.append(
                    f"{where}: 골반이 접지점보다 {pelvis[2] - anchor[2]:.3f} m 위, {ranges.pelvis_above_anchor_m} 밖이다"
                )
            if head_z is not None and not _within(head_z - anchor[2], ranges.head_above_anchor_m):
                out.append(
                    f"{where}: 머리가 접지점보다 {head_z - anchor[2]:.3f} m 위, {ranges.head_above_anchor_m} 밖이다"
                )
        for j, p in enumerate((tr.get("skeleton") or {}).get("joints", [])):
            if p is not None and not _z_ok(p[2]):
                out.append(f"{where}: skeleton.joints[{j}] Z = {p[2]} 가 {z_range} m 밖이다")
    return out


def _within(v: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] < v <= bounds[1]


def _view_cameras(track: Mapping[str, Any]) -> set[str]:
    """이 트랙의 근거가 된 카메라: 기여 카메라(cameras)와 관측 카메라(per_camera)의 합집합."""
    return set(track.get("cameras", [])) | {obs.get("camera_id") for obs in track.get("per_camera", [])}


def check_world_track_semantics(doc: Mapping[str, Any]) -> list[str]:
    """관측과 추정 구분, 관절 배열, 카메라별 관측 (명세 5.2).

    2대 미만 관측: 기여·관측 카메라의 합집합이 2대 미만이면 삼각측량 결과인 pelvis_xyz 와 observed 관절은
    null 이어야 한다. 검증된 지지 표면 위 접지 투영(ground_anchor_xyz, position_xy)은 한 대로도 되는 합법 경로라
    막지 않고, 한 대 영상에서 추정한 inferred 관절도 삼각측량 주장이 아니라 막지 않는다.
    한계: 현재 표현에는 관절별 관측 카메라 목록이 없다. 트랙 전체로 2대 이상이 보였을 때 각 관절이 실제로
    2대 이상에서 보였는지는 검증하지 못한다(visible·validity 는 Provider 의 주장을 그대로 받는다).
    """
    out = []
    t = doc.get("t")
    ids = [tr.get("world_id") for tr in doc.get("tracks", [])]
    if len(ids) != len(set(ids)):
        out.append(f"t={t}: 같은 world_id 가 한 시각에 두 번 나온다")
    for where, tr in _tracks(doc):
        if t is not None and tr.get("t_last_valid", 0) > t:
            out.append(f"{where}: t_last_valid {tr.get('t_last_valid')} 가 문서 시각 t 보다 늦다")
        if (
            tr.get("pelvis_xyz") is None
            and tr.get("ground_anchor_xyz") is None
            and tr.get("observation_state") == "OBSERVED"
        ):
            out.append(
                f"{where}: 골반·접지점이 모두 없으면 position_xy 는 예측값이다 (observation_state=OBSERVED 불가)"
            )
        out += _check_min_views(tr, where)
        out += _check_skeleton(tr.get("skeleton"), where)
        cams = [obs.get("camera_id") for obs in tr.get("per_camera", [])]
        if len(cams) != len(set(cams)):
            out.append(
                f"{where}: per_camera 에 같은 카메라가 두 번 있다 (같은 카메라의 두 검출을 한 World ID 에 묶지 않음)"
            )
        for k, obs in enumerate(tr.get("per_camera", [])):
            out += _check_2d(
                obs.get("bbox"), obs.get("joint_layout"), obs.get("keypoints_2d"), f"{where}.per_camera[{k}]"
            )
    return out


def _check_min_views(tr: Mapping[str, Any], where: str) -> list[str]:
    n_views = len(_view_cameras(tr))
    if n_views >= MIN_TRIANGULATION_CAMERAS:
        return []
    out = []
    if tr.get("pelvis_xyz") is not None:
        out.append(
            f"{where}: 기여·관측 카메라 {n_views}대인데 pelvis_xyz 를 채웠다 "
            f"({MIN_TRIANGULATION_CAMERAS}대 미만이면 null, 지어내지 않음)"
        )
    sk = tr.get("skeleton") or {}
    filled = [
        j
        for j, (p, src) in enumerate(zip(sk.get("joints", []), sk.get("joint_source", [])))
        if p is not None and src == "observed"
    ]
    if filled:
        out.append(
            f"{where}: 기여·관측 카메라 {n_views}대인데 observed 관절 {filled} 을 채웠다 "
            f"({MIN_TRIANGULATION_CAMERAS}대 미만 관절은 null)"
        )
    return out


def _check_skeleton(sk: Mapping[str, Any] | None, where: str) -> list[str]:
    if not sk:
        return []
    where = f"{where}.skeleton"
    arrays = {k: sk.get(k, []) for k in ("joints", "visible", "validity", "joint_source")}
    lengths = {k: len(v) for k, v in arrays.items()}
    out = []
    if len(set(lengths.values())) != 1:
        return [f"{where}: 관절 배열 길이가 다르다 {lengths}"]
    expected = JOINT_COUNTS.get(sk.get("joint_layout"))
    if expected is not None and lengths["joints"] != expected:
        out.append(f"{where}: {sk.get('joint_layout')} 는 관절 {expected}개인데 {lengths['joints']}개다")
    for j, (p, vis, val, _src) in enumerate(zip(*arrays.values())):
        if not vis and p is not None:
            out.append(f"{where}.joints[{j}]: visible=false 인 관절을 채웠다 (2대 미만 관절은 null)")
        if p is None and val != "UNAVAILABLE":
            out.append(f"{where}.joints[{j}]: null 관절의 validity 는 UNAVAILABLE 이어야 한다 ({val})")
    return out


def _check_2d(bbox: Sequence[float] | None, layout: str | None, kps: Sequence[Any] | None, where: str) -> list[str]:
    out = []
    if bbox is not None and len(bbox) == 4 and not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
        out.append(f"{where}: bbox {list(bbox)} 는 [x1, y1, x2, y2] (x1<x2, y1<y2) 가 아니다")
    if kps is not None:
        if layout is None:
            out.append(f"{where}: keypoints_2d 가 있으면 joint_layout 이 있어야 한다")
        elif JOINT_COUNTS.get(layout) is not None and len(kps) != JOINT_COUNTS[layout]:
            out.append(f"{where}: {layout} 는 keypoint {JOINT_COUNTS[layout]}개인데 {len(kps)}개다")
    return out


def check_world_tracks(doc: Mapping[str, Any], ranges: PlausibleRanges = DEFAULT_RANGES) -> list[str]:
    """World 트랙 문서(JSONL 한 줄) 하나에 대한 모든 검사. 스키마를 통과하지 못하면 형식을 전제하는 검사는 건너뛴다."""
    errors = check_world_tracks_schema(doc) + check_ground_anchor(doc)
    if errors:
        return errors
    return check_world_units_axes(doc, ranges) + check_world_track_semantics(doc)


def check_world_track_stream(docs: Iterable[Mapping[str, Any]]) -> list[str]:
    """시각 순서와 출처 일관성. t 는 엄격히 증가하고 provider_source·provider_version 은 바뀌지 않는다."""
    out: list[str] = []
    prev_t, first = None, None
    for doc in docs:
        if first is None:
            first = doc
        elif (doc.get("provider_source"), doc.get("provider_version")) != (
            first.get("provider_source"),
            first.get("provider_version"),
        ):
            out.append(f"t={doc.get('t')}: provider_source·provider_version 이 스트림 중간에 바뀌었다")
        if prev_t is not None and doc.get("t") is not None and doc["t"] <= prev_t:
            out.append(f"t={doc.get('t')}: 시각이 앞 문서 t={prev_t} 보다 늦지 않다 (시각 순서)")
        prev_t = doc.get("t")
    if first is None:
        out.append("World 트랙 문서가 하나도 없다")
    return out


def check_tracks_match_calibration(tracks_doc: Mapping[str, Any], calibration_doc: Mapping[str, Any]) -> list[str]:
    """같이 쓰는 World 트랙과 보정이 같은 보정 버전·카메라·영상 크기를 쓰는지 본다."""
    out = []
    if tracks_doc.get("calibration_version") != calibration_doc.get("calibration_version"):
        out.append(
            f"calibration_version 불일치: tracks {tracks_doc.get('calibration_version')} "
            f"!= calibration {calibration_doc.get('calibration_version')}"
        )
    sizes = {c["camera_id"]: c.get("image_size") for c in calibration_doc.get("cameras", [])}
    for where, tr in _tracks(tracks_doc):
        for cam in tr.get("cameras", []):
            if cam not in sizes:
                out.append(f"{where}: 기여 카메라 {cam} 가 보정에 없다")
        for obs in tr.get("per_camera", []):
            cam, bbox = obs.get("camera_id"), obs.get("bbox")
            if cam not in sizes:
                out.append(f"{where}: per_camera 카메라 {cam} 가 보정에 없다")
            elif bbox is not None and sizes[cam] is not None:
                w, h = sizes[cam]
                if not (0 <= bbox[0] and 0 <= bbox[1] and bbox[2] <= w and bbox[3] <= h):
                    out.append(f"{where}: {cam} bbox {bbox} 가 영상 크기 {sizes[cam]} 밖이다")
    return out


def check_world_track_provider(
    provider: WorldTrackProvider, max_frames: int | None = None, ranges: PlausibleRanges = DEFAULT_RANGES
) -> list[str]:
    """WorldTrackProvider 구현의 모든 검사. max_frames 로 앞부분만 볼 수 있다."""
    docs = []
    for i, doc in enumerate(provider.frames()):
        if max_frames is not None and i >= max_frames:
            break
        docs.append(doc)
    out = check_world_track_stream(docs)
    for doc in docs:
        out += check_world_tracks(doc, ranges)
        if doc.get("provider_source") != provider.source or doc.get("provider_version") != provider.provider_version:
            out.append(
                f"t={doc.get('t')}: 문서 출처 ({doc.get('provider_source')}, {doc.get('provider_version')}) 가 "
                f"Provider ({provider.source}, {provider.provider_version}) 와 다르다"
            )
    out += check_tracks_provenance(provider)
    return out


def check_tracks_provenance(provider: WorldTrackProvider) -> list[str]:
    prov = provider.provenance()
    out = schema_errors(prov, {"$ref": f"{DEFS_ID}#/$defs/input_provenance/properties/tracks"})
    expected = {"source": provider.source, "provider_version": provider.provider_version}
    if dict(prov) != expected:
        out.append(f"provenance() = {dict(prov)} 이 Provider 출처 {expected} 와 다르다")
    return out


# --------------------------------------------------------------------------- FrameSource

# 의미가 모순되는 (frame_source, timestamp_source) 조합만 막는다: 실제 영상(LIVE·FILE·DATASET)에 시뮬레이터 시각,
# LIVE 스트림에 파일 시각. 그 밖의 조합(예: 공개 데이터에 기록된 수신 추정 시각)은 허용한다.
FORBIDDEN_TIMESTAMP_SOURCES = {
    "LIVE": frozenset({"FILE", "SIM"}),
    "FILE": frozenset({"SIM"}),
    "DATASET": frozenset({"SIM"}),
    "SIM": frozenset(),
}


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_frame(frame: Frame, source: str | None = None) -> list[str]:
    """프레임 하나 (명세 3.9 촬영 시각 계약). source 를 주면 출처와 모순되는 timestamp_source 도 본다."""
    where = f"frame({frame.camera_id}, {frame.estimated_capture_time})"
    out = schema_errors(frame.camera_id, defs_ref("camera_id"))
    if not _is_int(frame.estimated_capture_time) or frame.estimated_capture_time < 0:
        out.append(f"{where}: estimated_capture_time 은 UTC epoch ms 정수여야 한다")
    if frame.timestamp_source not in TIMESTAMP_SOURCES:
        out.append(f"{where}: timestamp_source {frame.timestamp_source} 는 {TIMESTAMP_SOURCES} 중 하나여야 한다")
    elif frame.timestamp_source in FORBIDDEN_TIMESTAMP_SOURCES.get(source, frozenset()):
        out.append(f"{where}: {source} 출처 프레임에 timestamp_source {frame.timestamp_source} 를 쓸 수 없다")
    u = frame.timestamp_uncertainty_ms
    if u is not None and (not isinstance(u, (int, float)) or isinstance(u, bool) or not u >= 0 or math.isinf(u)):
        out.append(f"{where}: timestamp_uncertainty_ms 는 0 이상의 유한한 값이어야 한다")
    out += _check_frame_ref(frame.frame_ref, where)
    if (frame.image is None) == (frame.detections is None):
        out.append(f"{where}: image 와 detections 중 정확히 하나를 채운다")
    for k, det in enumerate(frame.detections or ()):
        dw = f"{where}.detections[{k}]"
        out += _check_2d(det.bbox, det.joint_layout, det.keypoints_2d, dw)
        if det.joint_layout not in (*JOINT_COUNTS, "MHR_NATIVE"):
            out.append(f"{dw}: joint_layout {det.joint_layout} 를 모른다")
        for j, kp in enumerate(det.keypoints_2d):
            if kp is not None and (len(kp) != 3 or not 0.0 <= kp[2] <= 1.0):
                out.append(f"{dw}.keypoints_2d[{j}]: [u, v, confidence(0~1)] 또는 None 이어야 한다")
    return out


def _check_frame_ref(ref: Mapping[str, Any], where: str) -> list[str]:
    keys = set(ref)
    if keys == {"stream_epoch", "frame_sequence"} or keys == {"file_uri", "frame_index"}:
        ints = [ref[k] for k in keys & {"stream_epoch", "frame_sequence", "frame_index"}]
        if all(_is_int(v) and v >= 0 for v in ints) and (
            "file_uri" not in ref or (isinstance(ref["file_uri"], str) and ref["file_uri"])
        ):
            return []
    return [
        f"{where}: frame_ref 는 {{stream_epoch, frame_sequence}} 또는 {{file_uri, frame_index}} (0 이상 정수) 여야 한다"
    ]


def check_frame_source(source: FrameSource, max_frames: int | None = None) -> list[str]:
    """FrameSource 구현의 모든 검사. 카메라별 촬영 시각은 줄어들지 않는다."""
    out: list[str] = []
    if source.source not in FRAME_SOURCES:
        out.append(f"source {source.source} 는 {FRAME_SOURCES} 중 하나여야 한다")
    cams = set(source.camera_ids)
    last: dict[str, int] = {}
    count = 0
    for i, frame in enumerate(source.frames()):
        if max_frames is not None and i >= max_frames:
            break
        count += 1
        out += check_frame(frame, source.source)
        if frame.camera_id not in cams:
            out.append(f"frame({frame.camera_id}): camera_ids {sorted(cams)} 에 없는 카메라")
        prev = last.get(frame.camera_id)
        if prev is not None and frame.estimated_capture_time < prev:
            out.append(
                f"frame({frame.camera_id}, {frame.estimated_capture_time}): 카메라별 촬영 시각이 줄었다 ({prev} 다음)"
            )
        last[frame.camera_id] = frame.estimated_capture_time
    if count == 0:
        out.append("프레임이 하나도 없다")
    prov = source.provenance()
    out += schema_errors(prov, {"$ref": f"{DEFS_ID}#/$defs/input_provenance/properties/frames"})
    if prov.get("source") != source.source:
        out.append(f"provenance() source {prov.get('source')} 가 {source.source} 와 다르다")
    if prov.get("camera_ids") is not None and set(prov["camera_ids"]) != cams:
        out.append(f"provenance() camera_ids {prov['camera_ids']} 가 {sorted(cams)} 와 다르다")
    return out


# --------------------------------------------------------------------------- input_provenance


def check_input_provenance(provenance: Mapping[str, Any]) -> list[str]:
    """input_provenance 스키마와 is_upper_bound 가 출처와 맞는지 본다 (명세 20.4 규칙 3)."""
    out = schema_errors(provenance, defs_ref("input_provenance"))
    if out:
        return out
    expected = is_upper_bound(provenance["frames"], provenance.get("calibration"), provenance.get("tracks"))
    if provenance["is_upper_bound"] != expected:
        out.append(f"is_upper_bound={provenance['is_upper_bound']} 이 입력 출처와 맞지 않다 (기대 {expected})")
    return out


def check_provenance_carries(
    provenance: Mapping[str, Any],
    frame_source: FrameSource | None = None,
    calibration_provider: CalibrationProvider | None = None,
    track_provider: WorldTrackProvider | None = None,
) -> list[str]:
    """소비 결과물의 input_provenance 가 쓴 Provider 들의 출처를 그대로 담는지 본다 (명세 20.4 규칙 2).

    결과물을 만드는 모듈(scene, hmr, 실험 Run 기록)의 시험에서 쓴다.
    """
    out = check_input_provenance(provenance)
    pairs = [("frames", frame_source), ("calibration", calibration_provider), ("tracks", track_provider)]
    for key, provider in pairs:
        if provider is None:
            continue
        expected = dict(provider.provenance())
        got = provenance.get(key)
        if got is None:
            out.append(f"input_provenance.{key} 가 없다 (쓴 Provider 출처 {expected})")
        elif any(got.get(k) != v for k, v in expected.items()):
            out.append(f"input_provenance.{key} = {got} 가 Provider 출처 {expected} 와 다르다")
    return out
