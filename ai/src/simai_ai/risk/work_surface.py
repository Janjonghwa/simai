"""등록 작업면 (명세 7.5 작업면 등록과 가장자리, 3.2 구성 묶음 Operational Geometry, 5.3).

R2(가장자리 근접)·R3(고소 위치)가 쓰는 높이·가장자리는 관리자가 등록한 작업면(OBB 바닥 투영 + 상판 높이)에서 온다.
정적 3D 결과는 쓰지 않는다(7.5, AGENTS 규칙 1).

**형식.** 구성 묶음 Operational Geometry 의 `work_surfaces` 항목이고, ContactEstimator 가 지지 표면 후보로 읽는 것과 같은
임시 형식이다(`contact.surfaces` 머리말, 구성 요소 내용 형식은 계약 범위 밖). risk 는 다른 기능 패키지를 import 하지 않으므로
(ai/CONVENTION.md) 같은 형식을 여기서 따로 읽는다. 계약이 생기면 둘 다 그 형식으로 바꾼다.

    {"surface_id": "podium-1", "top_height_m": 0.4, "center_xy": [4.5, 6.75], "size_xy": [4.0, 1.1], "yaw_rad": 0.0,
     "top_height_sigma_m": 0.02}

- `center_xy`·`size_xy`·`yaw_rad` 는 OBB 의 바닥 투영(가운데, 가로·세로 전체 길이, +Z 축 회전 라디안), `top_height_m` 는 상판
  높이(바닥 Z=0 기준 미터). `yaw_rad`(기본 0)·`top_height_sigma_m`(기본 0.02)은 생략할 수 있다. 다른 키는 무시한다.
- 검사도 ContactEstimator 와 같다: 상판은 바닥보다 높고(> 0), 크기는 양수, 값은 유한, `surface_id` 는 비지 않고 바닥 ID
  (`floor`)와 겹치지 않으며 서로 겹치지 않는다. 구성 묶음에서 읽을 때(`work_surfaces_from_operational_geometry`) 형식이 틀린
  항목은 ContactEstimator 와 똑같이 빼고 `rejected` 에 사유를 남긴다(두 쪽이 같은 작업면 집합을 보게 한다).
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from simai_ai.risk._parse import RiskInputError, as_mapping, as_number, as_str, as_vec, require

FLOOR_ID = "floor"
"""바닥 지지 표면 ID (world_state 계약 support_surface_id: "floor 또는 등록 작업면 ID")"""
FLOOR_Z_M = 0.0
"""바닥 높이 (좌표 규약 4장: 바닥 Z=0)"""
DEFAULT_TOP_HEIGHT_SIGMA_M = 0.02
"""상판 높이 표준편차 기본값 (contact.surfaces.DEFAULT_SURFACE_SIGMA_M 와 같은 값)"""

Corner = tuple[float, float]


@dataclass(frozen=True)
class WorkSurface:
    """등록 작업면 하나 (OBB 바닥 투영 + 상판 높이)."""

    surface_id: str
    top_height_m: float
    center_xy: tuple[float, ...]
    size_xy: tuple[float, ...]
    yaw_rad: float = 0.0
    top_height_sigma_m: float = DEFAULT_TOP_HEIGHT_SIGMA_M

    def __post_init__(self) -> None:
        where = f"work_surface {self.surface_id!r}"
        if not isinstance(self.surface_id, str) or not self.surface_id:
            raise RiskInputError("work_surface: surface_id 가 비었다")
        if self.surface_id == FLOOR_ID:
            raise RiskInputError(f"{where}: 바닥 ID 와 겹친다")
        for name in ("top_height_m", "yaw_rad", "top_height_sigma_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise RiskInputError(f"{where}: {name} 가 유한한 수가 아니다")
        if not self.top_height_m > FLOOR_Z_M:
            raise RiskInputError(f"{where}: 상판 높이는 바닥보다 높아야 한다")
        if self.top_height_sigma_m < 0:
            raise RiskInputError(f"{where}: top_height_sigma_m 는 0 이상이어야 한다")
        for name, positive in (("center_xy", False), ("size_xy", True)):
            vec = tuple(getattr(self, name))
            if len(vec) != 2 or any(
                isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) for v in vec
            ):
                raise RiskInputError(f"{where}: {name} 는 유한한 수 2개여야 한다")
            if positive and not all(v > 0 for v in vec):
                raise RiskInputError(f"{where}: size_xy 는 양수 2개여야 한다")
            object.__setattr__(self, name, (float(vec[0]), float(vec[1])))
        object.__setattr__(self, "top_height_m", float(self.top_height_m))
        object.__setattr__(self, "yaw_rad", float(self.yaw_rad))
        object.__setattr__(self, "top_height_sigma_m", float(self.top_height_sigma_m))

    @property
    def footprint(self) -> tuple[Corner, Corner, Corner, Corner]:
        """바닥 투영 사각형의 꼭짓점 4개(반시계 방향). 변 i 는 꼭짓점 i → i+1 이고 바깥쪽은 진행 방향 오른쪽이다."""
        hx, hy = self.size_xy[0] / 2.0, self.size_xy[1] / 2.0
        c, s = math.cos(self.yaw_rad), math.sin(self.yaw_rad)
        cx, cy = self.center_xy
        local = ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))
        a, b, c2, d = ((cx + u * c - v * s, cy + u * s + v * c) for u, v in local)
        return (a, b, c2, d)

    def local_xy(self, xy: Iterable[float]) -> tuple[float, float]:
        """World XY → 작업면 축 좌표(가운데 원점, yaw 만큼 되돌림)."""
        x, y = tuple(xy)[:2]
        dx, dy = x - self.center_xy[0], y - self.center_xy[1]
        c, s = math.cos(self.yaw_rad), math.sin(self.yaw_rad)
        return (dx * c + dy * s, -dx * s + dy * c)

    def contains_xy(self, xy: Iterable[float], margin_m: float = 0.0) -> bool:
        """수평 위치가 바닥 투영을 margin_m 만큼 넓힌 사각형 안(경계 포함)인지."""
        u, v = self.local_xy(xy)
        return abs(u) <= self.size_xy[0] / 2.0 + margin_m and abs(v) <= self.size_xy[1] / 2.0 + margin_m

    @classmethod
    def from_json(cls, obj: Any, where: str = "work_surface") -> "WorkSurface":
        obj = as_mapping(obj, where)
        yaw = obj.get("yaw_rad", 0.0)
        sigma = obj.get("top_height_sigma_m", DEFAULT_TOP_HEIGHT_SIGMA_M)
        return cls(
            surface_id=as_str(require(obj, "surface_id", where), f"{where}.surface_id"),
            top_height_m=as_number(require(obj, "top_height_m", where), f"{where}.top_height_m"),
            center_xy=as_vec(require(obj, "center_xy", where), 2, f"{where}.center_xy"),
            size_xy=as_vec(require(obj, "size_xy", where), 2, f"{where}.size_xy"),
            yaw_rad=as_number(yaw, f"{where}.yaw_rad"),
            top_height_sigma_m=as_number(sigma, f"{where}.top_height_sigma_m"),
        )


@dataclass(frozen=True)
class WorkSurfaceSet:
    """구성 묶음에서 읽은 작업면과 출처 버전, 형식이 틀려 뺀 항목((surface_id 또는 순번, 사유))."""

    surfaces: tuple[WorkSurface, ...] = ()
    geometry_version: str | None = None
    rejected: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        check_unique_ids(self.surfaces, "WorkSurfaceSet")


def check_unique_ids(surfaces: Iterable[WorkSurface], where: str) -> None:
    ids = [s.surface_id for s in surfaces]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise RiskInputError(f"{where}: 작업면 ID 가 겹친다 {duplicated}")


def work_surfaces_from_operational_geometry(
    geometry: Mapping[str, Any], geometry_version: str | None = None
) -> WorkSurfaceSet:
    """구성 묶음 operational_geometry 문서에서 작업면을 읽는다. 형식이 틀린 항목은 빼고 rejected 에 남긴다.

    contact.surfaces.SurfaceSet.from_operational_geometry 와 같은 항목을 받고 같은 항목을 뺀다.
    """
    version = geometry_version if geometry_version is not None else geometry.get("version")
    items = geometry.get("work_surfaces") or ()
    surfaces: list[WorkSurface] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        label = f"#{i}"
        if isinstance(item, Mapping) and isinstance(item.get("surface_id"), str) and item["surface_id"]:
            label = item["surface_id"]
        try:
            surface = WorkSurface.from_json(_thaw(item), f"work_surfaces[{i}]")
            if surface.surface_id in seen:
                raise RiskInputError("surface_id 가 겹친다")
        except RiskInputError as e:
            rejected.append((label, str(e)))
            continue
        seen.add(surface.surface_id)
        surfaces.append(surface)
    return WorkSurfaceSet(tuple(surfaces), version if isinstance(version, str) else None, tuple(rejected))


def _thaw(value: Any) -> Any:
    """구성 묶음의 읽기 전용 JSON(Mapping·tuple, config.bundle.freeze_json)을 dict·list 로 되돌린다. ContactEstimator 는
    tuple 좌표도 받으므로 같은 항목을 받으려면 되돌려서 읽어야 한다."""
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw(v) for v in value]
    return value
