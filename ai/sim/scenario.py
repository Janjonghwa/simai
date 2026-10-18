"""시나리오 정의(JSON) 읽기와 검사.

시나리오 파일의 모든 길이는 미터, 각도는 라디안, 시각은 ms 정수다.
밑줄로 시작하는 키(_note 등)는 사람용 메모로 보고 무시한다. 그 밖의 모르는 키는 오류로 막는다.
필드 설명은 sim/README.md 에 있다.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

# 카메라 기본값: Tapo C110 stream1 (명세 3.3). 화각은 판매처 정보(가로 약 105°, 미확인)라 시나리오에서 명시한다.
DEFAULT_CAMERA: dict[str, Any] = {
    "image_size": [2304, 1296],
    "fps": 25,
    "hfov_rad": 1.832596,
    "distortion": {"model": "NONE", "coeffs": []},
    "roll_rad": 0.0,
    "time_offset_ms": 0,
    "capture_jitter_ms": 0,
    "timestamp_error_ms": 0,
}

DEFAULT_KEYPOINTS: dict[str, Any] = {
    "pixel_noise_sigma_px": 1.0,
    "confidence_range": [0.6, 0.98],
    "min_visible_joints": 3,
    "occluded": "conf_zero",
    "out_of_frame": "null",
}

HIDDEN_POLICIES = ("conf_zero", "null")
DISTORTION_MODELS = {"NONE": 0, "OPENCV_PINHOLE": 5, "OPENCV_FISHEYE": 4}


class ScenarioError(ValueError):
    """시나리오 정의가 잘못됐다."""


@dataclass(frozen=True)
class Surface:
    """높이가 있는 지지 표면 겸 가림 물체(단상 등). 축 정렬 상자."""

    surface_id: str
    box_min: tuple[float, float, float]
    box_max: tuple[float, float, float]


@dataclass(frozen=True)
class CameraSpec:
    camera_id: str
    position: tuple[float, float, float]
    look_at: tuple[float, float, float]
    roll_rad: float
    image_size: tuple[int, int]
    fps: float
    hfov_rad: float
    distortion_model: str
    distortion_coeffs: tuple[float, ...]
    time_offset_ms: int
    capture_jitter_ms: int
    timestamp_error_ms: int


@dataclass(frozen=True)
class Waypoint:
    t_ms: int
    xy: tuple[float, float]


POSTURES = ("stand", "sit", "squat", "stoop", "sit_floor", "lie")
"""자세 이름. stand 는 걷기 골격, 나머지는 sim.body 의 고정 자세(키 1.70 m 기준 몸 좌표)다"""
EASES = ("linear", "in")
"""자세 전환 보간: linear(등속), in(가속, 진행률². 넘어짐처럼 점점 빨라지는 전환)"""


@dataclass(frozen=True)
class PostureKey:
    """t_ms 에 posture 자세다. 앞 키에서 이 키까지 ease 로 보간한다."""

    t_ms: int
    posture: str
    ease: str = "linear"


@dataclass(frozen=True)
class PersonSpec:
    person_id: str
    height_m: float
    radius_m: float
    initial_heading_rad: float
    waypoints: tuple[Waypoint, ...]
    postures: tuple[PostureKey, ...] = ()
    """비어 있으면 늘 서서 걷는다(자세 키가 생기기 전 출력과 바이트 단위로 같다)"""


@dataclass(frozen=True)
class KeypointSpec:
    pixel_noise_sigma_px: float
    confidence_range: tuple[float, float]
    min_visible_joints: int
    occluded: str
    out_of_frame: str


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    seed: int
    start_time_ms: int
    duration_ms: int
    world_rate_hz: float
    site_id: str
    room_size_m: tuple[float, float]
    ceiling_height_m: float
    surfaces: tuple[Surface, ...]
    cameras: tuple[CameraSpec, ...]
    people: tuple[PersonSpec, ...]
    keypoints: KeypointSpec
    canonical: dict[str, Any]
    """시드 덮어쓰기까지 반영한 원본 정의. 해시와 manifest 에 쓴다."""


def resolve_path(name_or_path: str) -> Path:
    """시나리오 이름(basic)이면 sim/scenarios/<이름>.json, 아니면 파일 경로로 본다."""
    path = Path(name_or_path)
    if path.suffix == ".json" or path.exists():
        return path
    return SCENARIO_DIR / f"{name_or_path}.json"


def list_builtin() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.json"))


def load(name_or_path: str, seed: int | None = None) -> Scenario:
    path = resolve_path(name_or_path)
    if not path.is_file():
        raise ScenarioError(f"시나리오 파일이 없다: {path} (내장 시나리오: {', '.join(list_builtin())})")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ScenarioError(f"{path}: JSON 형식 오류: {e}") from e
    if seed is not None:
        raw = dict(raw, seed=seed)
    return parse(raw)


def parse(raw: Any) -> Scenario:
    doc = _obj(raw, "scenario")
    _keys(
        doc,
        "scenario",
        required={"name", "seed", "start_time_ms", "duration_ms", "room", "cameras", "people"},
        optional={"description", "world_rate_hz", "site_id", "surfaces", "camera_defaults", "keypoints"},
    )

    room = _obj(doc["room"], "room")
    _keys(room, "room", required={"size_m"}, optional={"ceiling_height_m"})
    size = _vec(room["size_m"], 2, "room.size_m")
    if min(size) <= 0:
        raise ScenarioError("room.size_m 은 양수여야 한다")
    ceiling = _num(room.get("ceiling_height_m", 3.0), "room.ceiling_height_m", positive=True)

    surfaces = tuple(_surface(s, i, size) for i, s in enumerate(_list(doc.get("surfaces", []), "surfaces")))
    _unique([s.surface_id for s in surfaces] + ["floor"], "surface id")

    cam_defaults = dict(DEFAULT_CAMERA)
    if "camera_defaults" in doc:
        overrides = _obj(doc["camera_defaults"], "camera_defaults")
        _keys(overrides, "camera_defaults", required=set(), optional=set(DEFAULT_CAMERA))
        cam_defaults.update(_strip(overrides))
    cameras = tuple(_camera(c, i, cam_defaults) for i, c in enumerate(_list(doc["cameras"], "cameras")))
    for c in cameras:
        x, y, z = c.position
        if not (0 <= x <= size[0] and 0 <= y <= size[1] and 0 < z <= ceiling):
            raise ScenarioError(f"카메라 {c.camera_id}: position 이 교실 안·천장 아래가 아니다")
    if not cameras:
        raise ScenarioError("cameras 가 비어 있다")
    _unique([c.camera_id for c in cameras], "camera id")

    people = tuple(_person(p, i, size) for i, p in enumerate(_list(doc["people"], "people")))
    _unique([p.person_id for p in people], "person id")

    kp = dict(DEFAULT_KEYPOINTS)
    if "keypoints" in doc:
        kp_raw = _obj(doc["keypoints"], "keypoints")
        _keys(kp_raw, "keypoints", required=set(), optional=set(DEFAULT_KEYPOINTS))
        kp.update(_strip(kp_raw))
    conf = _vec(kp["confidence_range"], 2, "keypoints.confidence_range")
    if not 0 < conf[0] <= conf[1] <= 1:
        raise ScenarioError("keypoints.confidence_range 는 0 < 하한 <= 상한 <= 1")
    for key in ("occluded", "out_of_frame"):
        if kp[key] not in HIDDEN_POLICIES:
            raise ScenarioError(f"keypoints.{key} 는 {HIDDEN_POLICIES} 중 하나")
    keypoints = KeypointSpec(
        pixel_noise_sigma_px=_num(kp["pixel_noise_sigma_px"], "keypoints.pixel_noise_sigma_px", nonneg=True),
        confidence_range=(conf[0], conf[1]),
        min_visible_joints=_int(kp["min_visible_joints"], "keypoints.min_visible_joints", minimum=1),
        occluded=kp["occluded"],
        out_of_frame=kp["out_of_frame"],
    )

    name = doc["name"]
    if not isinstance(name, str) or not name:
        raise ScenarioError("name 은 비어 있지 않은 문자열")
    return Scenario(
        name=name,
        description=str(doc.get("description", "")),
        seed=_int(doc["seed"], "seed", minimum=0),
        start_time_ms=_int(doc["start_time_ms"], "start_time_ms", minimum=0),
        duration_ms=_int(doc["duration_ms"], "duration_ms", minimum=1),
        world_rate_hz=_num(doc.get("world_rate_hz", 10), "world_rate_hz", positive=True),
        site_id=str(doc.get("site_id", "sim-classroom")),
        room_size_m=(size[0], size[1]),
        ceiling_height_m=ceiling,
        surfaces=surfaces,
        cameras=cameras,
        people=people,
        keypoints=keypoints,
        canonical=doc,
    )


def _surface(raw: Any, i: int, room: tuple[float, ...]) -> Surface:
    where = f"surfaces[{i}]"
    doc = _obj(raw, where)
    _keys(doc, where, required={"id", "box_min", "box_max"}, optional=set())
    lo = _vec(doc["box_min"], 3, f"{where}.box_min")
    hi = _vec(doc["box_max"], 3, f"{where}.box_max")
    if any(a >= b for a, b in zip(lo, hi)):
        raise ScenarioError(f"{where}: box_min < box_max 여야 한다")
    if lo[2] != 0:
        raise ScenarioError(f"{where}: 바닥에 놓인 상자만 지원한다(box_min z = 0)")
    if lo[0] < 0 or lo[1] < 0 or hi[0] > room[0] or hi[1] > room[1]:
        raise ScenarioError(f"{where}: 교실 바닥 밖으로 나간다")
    return Surface(
        surface_id=_id(doc["id"], f"{where}.id"), box_min=(lo[0], lo[1], lo[2]), box_max=(hi[0], hi[1], hi[2])
    )


def _camera(raw: Any, i: int, defaults: dict[str, Any]) -> CameraSpec:
    where = f"cameras[{i}]"
    doc = _obj(raw, where)
    _keys(doc, where, required={"id", "position", "look_at"}, optional=set(DEFAULT_CAMERA))
    merged = dict(defaults)
    merged.update(_strip(doc))
    size = _vec(merged["image_size"], 2, f"{where}.image_size")
    if any(int(s) != s or s < 1 for s in size):
        raise ScenarioError(f"{where}.image_size 는 양의 정수 [width, height]")
    hfov = _num(merged["hfov_rad"], f"{where}.hfov_rad", positive=True)
    if hfov >= math.pi:
        raise ScenarioError(f"{where}.hfov_rad 는 핀홀 모델에서 π 미만이어야 한다 (라디안)")
    dist = _obj(merged["distortion"], f"{where}.distortion")
    _keys(dist, f"{where}.distortion", required={"model", "coeffs"}, optional=set())
    model = dist["model"]
    if model not in DISTORTION_MODELS:
        raise ScenarioError(f"{where}.distortion.model 은 {sorted(DISTORTION_MODELS)} 중 하나")
    coeffs = tuple(_num(c, f"{where}.distortion.coeffs") for c in _list(dist["coeffs"], f"{where}.distortion.coeffs"))
    if len(coeffs) != DISTORTION_MODELS[model]:
        raise ScenarioError(
            f"{where}.distortion.coeffs 는 {model} 에서 {DISTORTION_MODELS[model]}개다 "
            "(OPENCV_PINHOLE: k1 k2 p1 p2 k3, OPENCV_FISHEYE: k1 k2 k3 k4)"
        )
    fps = _num(merged["fps"], f"{where}.fps", positive=True)
    jitter = _int(merged["capture_jitter_ms"], f"{where}.capture_jitter_ms", minimum=0)
    ts_error = _int(merged["timestamp_error_ms"], f"{where}.timestamp_error_ms", minimum=0)
    if 2 * (jitter + ts_error) >= 1000.0 / fps:
        raise ScenarioError(
            f"{where}: capture_jitter_ms + timestamp_error_ms 는 프레임 간격의 절반보다 작아야 한다 (시각 순서 유지)"
        )
    pos = _vec(doc["position"], 3, f"{where}.position")
    look = _vec(doc["look_at"], 3, f"{where}.look_at")
    return CameraSpec(
        camera_id=_id(doc["id"], f"{where}.id"),
        position=(pos[0], pos[1], pos[2]),
        look_at=(look[0], look[1], look[2]),
        roll_rad=_num(merged["roll_rad"], f"{where}.roll_rad"),
        image_size=(int(size[0]), int(size[1])),
        fps=fps,
        hfov_rad=hfov,
        distortion_model=model,
        distortion_coeffs=coeffs,
        time_offset_ms=_int(merged["time_offset_ms"], f"{where}.time_offset_ms", minimum=0),
        capture_jitter_ms=jitter,
        timestamp_error_ms=ts_error,
    )


def _person(raw: Any, i: int, room: tuple[float, ...]) -> PersonSpec:
    where = f"people[{i}]"
    doc = _obj(raw, where)
    _keys(
        doc, where, required={"id", "waypoints"}, optional={"height_m", "radius_m", "initial_heading_rad", "postures"}
    )
    waypoints = []
    for j, w in enumerate(_list(doc["waypoints"], f"{where}.waypoints")):
        wdoc = _obj(w, f"{where}.waypoints[{j}]")
        _keys(wdoc, f"{where}.waypoints[{j}]", required={"t_ms", "xy"}, optional=set())
        xy = _vec(wdoc["xy"], 2, f"{where}.waypoints[{j}].xy")
        if not (0 <= xy[0] <= room[0] and 0 <= xy[1] <= room[1]):
            raise ScenarioError(f"{where}.waypoints[{j}].xy 가 교실 밖이다")
        waypoints.append(
            Waypoint(t_ms=_int(wdoc["t_ms"], f"{where}.waypoints[{j}].t_ms", minimum=0), xy=(xy[0], xy[1]))
        )
    if not waypoints:
        raise ScenarioError(f"{where}.waypoints 가 비어 있다")
    if any(a.t_ms >= b.t_ms for a, b in zip(waypoints, waypoints[1:])):
        raise ScenarioError(f"{where}.waypoints 의 t_ms 는 증가해야 한다")
    postures = []
    for j, k in enumerate(_list(doc.get("postures", []), f"{where}.postures")):
        kwhere = f"{where}.postures[{j}]"
        kdoc = _obj(k, kwhere)
        _keys(kdoc, kwhere, required={"t_ms", "posture"}, optional={"ease"})
        if kdoc["posture"] not in POSTURES:
            raise ScenarioError(f"{kwhere}.posture 는 {POSTURES} 중 하나")
        ease = kdoc.get("ease", "linear")
        if ease not in EASES:
            raise ScenarioError(f"{kwhere}.ease 는 {EASES} 중 하나")
        t_ms = _int(kdoc["t_ms"], f"{kwhere}.t_ms", minimum=0)
        postures.append(PostureKey(t_ms=t_ms, posture=kdoc["posture"], ease=ease))
    if any(a.t_ms >= b.t_ms for a, b in zip(postures, postures[1:])):
        raise ScenarioError(f"{where}.postures 의 t_ms 는 증가해야 한다")
    return PersonSpec(
        person_id=_id(doc["id"], f"{where}.id"),
        height_m=_num(doc.get("height_m", 1.70), f"{where}.height_m", positive=True),
        radius_m=_num(doc.get("radius_m", 0.2), f"{where}.radius_m", positive=True),
        initial_heading_rad=_num(doc.get("initial_heading_rad", 0.0), f"{where}.initial_heading_rad"),
        waypoints=tuple(waypoints),
        postures=tuple(postures),
    )


def _strip(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _obj(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where} 는 객체여야 한다")
    return raw


def _list(raw: Any, where: str) -> list[Any]:
    if not isinstance(raw, list):
        raise ScenarioError(f"{where} 는 배열이어야 한다")
    return raw


def _keys(doc: dict[str, Any], where: str, required: set[str], optional: set[str]) -> None:
    keys = {k for k in doc if not k.startswith("_")}
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ScenarioError(f"{where}: 필수 키 없음 {sorted(missing)}")
    if unknown:
        raise ScenarioError(f"{where}: 모르는 키 {sorted(unknown)}")


def _num(raw: Any, where: str, positive: bool = False, nonneg: bool = False) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ScenarioError(f"{where} 는 유한한 수여야 한다")
    if positive and raw <= 0:
        raise ScenarioError(f"{where} 는 양수여야 한다")
    if nonneg and raw < 0:
        raise ScenarioError(f"{where} 는 0 이상이어야 한다")
    return float(raw)


def _int(raw: Any, where: str, minimum: int | None = None) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ScenarioError(f"{where} 는 정수여야 한다")
    if minimum is not None and raw < minimum:
        raise ScenarioError(f"{where} 는 {minimum} 이상이어야 한다")
    return raw


def _vec(raw: Any, n: int, where: str) -> tuple[float, ...]:
    items = _list(raw, where)
    if len(items) != n:
        raise ScenarioError(f"{where} 는 길이 {n} 배열이어야 한다")
    return tuple(_num(x, where) for x in items)


def _id(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw or not all(c.isalnum() or c in "_-" for c in raw) or not raw.isascii():
        raise ScenarioError(f"{where} 는 영문·숫자·_- 로 된 문자열이어야 한다")
    return raw


def _unique(ids: list[str], what: str) -> None:
    seen = set()
    for x in ids:
        if x in seen:
            raise ScenarioError(f"{what} 중복: {x}")
        seen.add(x)
