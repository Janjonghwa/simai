"""지지 표면 후보: 바닥과 등록 작업면 (명세 5.2 접지 추정, 7.5 작업면 등록과 가장자리, 3.2 구성 묶음).

ContactEstimator 는 바닥(Z=0, 좌표 규약 4장)과 관리자가 등록한 작업면의 상판만 지지 표면 후보로 쓴다. 등록 작업면은
활성 구성 묶음(`config.ActiveConfig`)의 Operational Geometry(`operational_geometry.work_surfaces`)에서 읽는다. 정적 3D·HMR
결과는 후보로 쓰지 않는다(AGENTS 규칙 1).

**작업면 항목 임시 형식.** 구성 묶음 구성 요소의 내용 형식은 계약 범위 밖이라(`config.bundle` 머리말) 아래는 이 모듈의
임시 형식이다. 계약이 생기면 그 형식으로 바꾼다.

    {"surface_id": "podium-1", "top_height_m": 0.4, "center_xy": [4.5, 6.75], "size_xy": [4.0, 1.1], "yaw_rad": 0.0,
     "top_height_sigma_m": 0.02}

- `center_xy`·`size_xy`·`yaw_rad` 는 OBB 의 바닥 투영(가운데, 가로·세로 전체 길이, +Z 축 회전 라디안), `top_height_m` 는
  상판 높이(바닥 기준 미터). `yaw_rad`(기본 0)와 `top_height_sigma_m`(기본 `DEFAULT_SURFACE_SIGMA_M`)은 생략할 수 있다.
- 형식이 틀린 항목은 후보에서 빼고 `rejected` 에 사유를 남긴다. 등록 작업면을 모르면 그 위에 선 사람은 지지 표면을
  찾지 못해 UNKNOWN 이 될 뿐이고 바닥으로 바뀌지 않는다(규칙 2). 그래서 빼는 쪽이 안전하다.
- `surface_id` 는 바닥 ID(`FLOOR_ID`)와 겹칠 수 없다.

현재 시각을 읽지 않는다. 길이는 미터, 각도는 라디안이다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from simai_ai.config import ActiveConfig

FLOOR_ID = "floor"
"""바닥 지지 표면 ID (world_state 계약 support_surface_id: "floor 또는 등록 작업면 ID")"""
DEFAULT_SURFACE_SIGMA_M = 0.02
"""작업면 상판 높이 표준편차 기본값 (관리자가 입력한 상판 높이). 초기값, G2 에서 조정"""
FLOOR_SIGMA_Z_M = 0.03
"""바닥 접지점 높이 표준편차. 발목만으로는 약 8 cm 이하 미등록 상판을 바닥과 구별하지 못하므로(contact.estimator
바닥 확정의 분해능) 그 대부분을 2σ 로 덮는 값이다. 초기값, G2 에서 조정"""


@dataclass(frozen=True)
class SupportSurface:
    """지지 표면 후보 하나. 바닥은 footprint 가 없다(교실 어디든 후보)."""

    surface_id: str
    top_z: float
    sigma_z: float = DEFAULT_SURFACE_SIGMA_M
    center_xy: tuple[float, float] | None = None
    size_xy: tuple[float, float] | None = None
    yaw_rad: float = 0.0

    def __post_init__(self) -> None:
        if not self.surface_id:
            raise ValueError("surface_id 가 비었다")
        if not (_finite(self.top_z) and _finite(self.sigma_z) and self.sigma_z >= 0):
            raise ValueError(f"{self.surface_id}: 높이·표준편차가 유한하지 않다")
        if self.is_floor:
            if self.center_xy is not None or self.size_xy is not None or self.top_z != 0.0:
                raise ValueError("바닥은 Z=0 이고 footprint 가 없다")
            return
        if self.center_xy is None or self.size_xy is None:
            raise ValueError(f"{self.surface_id}: 작업면은 OBB footprint(center_xy, size_xy)가 있어야 한다")
        if not (len(self.center_xy) == 2 and all(_finite(v) for v in self.center_xy)):
            raise ValueError(f"{self.surface_id}: center_xy 는 유한한 수 2개여야 한다")
        if not (len(self.size_xy) == 2 and all(_finite(v) and v > 0 for v in self.size_xy)):
            raise ValueError(f"{self.surface_id}: size_xy 는 양수 2개여야 한다")
        if not _finite(self.yaw_rad):
            raise ValueError(f"{self.surface_id}: yaw_rad 가 유한하지 않다")
        if not self.top_z > 0:
            raise ValueError(f"{self.surface_id}: 작업면 상판 높이는 바닥보다 높아야 한다")

    @property
    def is_floor(self) -> bool:
        return self.surface_id == FLOOR_ID

    def contains_xy(self, xy: tuple[float, float] | list[float], margin_m: float = 0.0) -> bool:
        """수평 위치가 footprint 를 margin 만큼 넓힌 사각형 안인지. 바닥은 늘 참이다."""
        if self.is_floor:
            return True
        assert self.center_xy is not None and self.size_xy is not None
        dx, dy = xy[0] - self.center_xy[0], xy[1] - self.center_xy[1]
        c, s = math.cos(self.yaw_rad), math.sin(self.yaw_rad)
        u, v = dx * c + dy * s, -dx * s + dy * c
        return abs(u) <= self.size_xy[0] / 2 + margin_m and abs(v) <= self.size_xy[1] / 2 + margin_m



@dataclass(frozen=True)
class SurfaceSet:
    """한 판정 시각의 지지 표면 후보(바닥 + 등록 작업면)와 그 출처 버전.

    geometry_version 은 Operational Geometry 버전(구성 묶음이 없으면 None). rejected 는 형식이 틀려 뺀 작업면
    (surface_id 또는 순번, 사유)이다.
    """

    work_surfaces: tuple[SupportSurface, ...] = ()
    geometry_version: str | None = None
    rejected: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        ids = [s.surface_id for s in self.work_surfaces]
        if FLOOR_ID in ids:
            raise ValueError("작업면 ID 가 바닥 ID 와 겹친다")
        if len(ids) != len(set(ids)):
            raise ValueError(f"작업면 ID 가 겹친다: {ids}")

    @property
    def surfaces(self) -> tuple[SupportSurface, ...]:
        """바닥을 포함한 모든 후보."""
        return (FLOOR, *self.work_surfaces)

    def get(self, surface_id: str) -> SupportSurface | None:
        for s in self.surfaces:
            if s.surface_id == surface_id:
                return s
        return None

    def candidates(self, xy: tuple[float, float] | list[float], margin_m: float) -> tuple[SupportSurface, ...]:
        """그 수평 위치에 있을 수 있는 후보 (바닥은 늘 들어간다)."""
        return tuple(s for s in self.surfaces if s.contains_xy(xy, margin_m))

    @classmethod
    def floor_only(cls) -> SurfaceSet:
        """구성 묶음이 아직 없을 때: 바닥만 안다. 높은 곳에 선 사람은 UNKNOWN 이 된다."""
        return cls()

    @classmethod
    def from_operational_geometry(cls, geometry: Mapping[str, Any], geometry_version: str | None = None) -> SurfaceSet:
        """구성 묶음 operational_geometry 문서(읽기 전용 JSON 포함)에서 작업면을 읽는다. 형식이 틀린 항목은 뺀다."""
        version = geometry_version if geometry_version is not None else geometry.get("version")
        items = geometry.get("work_surfaces") or ()
        surfaces: list[SupportSurface] = []
        rejected: list[tuple[str, str]] = []
        seen: set[str] = set()
        for i, item in enumerate(items):
            label = f"#{i}"
            try:
                if not isinstance(item, Mapping):
                    raise ValueError("항목이 객체가 아니다")
                sid = item.get("surface_id")
                if not isinstance(sid, str) or not sid:
                    raise ValueError("surface_id 가 비었다")
                label = sid
                if sid == FLOOR_ID:
                    raise ValueError("바닥 ID 와 겹친다")
                if sid in seen:
                    raise ValueError("surface_id 가 겹친다")
                surface = SupportSurface(
                    surface_id=sid,
                    top_z=_number(item, "top_height_m"),
                    sigma_z=_number(item, "top_height_sigma_m", DEFAULT_SURFACE_SIGMA_M),
                    center_xy=_pair(item, "center_xy"),
                    size_xy=_pair(item, "size_xy"),
                    yaw_rad=_number(item, "yaw_rad", 0.0),
                )
            except ValueError as e:
                rejected.append((label, str(e)))
                continue
            seen.add(sid)
            surfaces.append(surface)
        return cls(tuple(surfaces), version if isinstance(version, str) else None, tuple(rejected))

    @classmethod
    def from_active_config(cls, active: ActiveConfig | None) -> SurfaceSet:
        """활성 구성 묶음의 Operational Geometry. 묶음이 없으면 바닥만이다."""
        if active is None:
            return cls.floor_only()
        bundle = active.bundle
        return cls.from_operational_geometry(bundle.operational_geometry, bundle.geometry_version)


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _number(item: Mapping[str, Any], key: str, default: float | None = None) -> float:
    if key not in item:
        if default is None:
            raise ValueError(f"{key} 가 없다")
        return default
    v = item[key]
    if not _finite(v):
        raise ValueError(f"{key} 가 유한한 수가 아니다: {v!r}")
    return float(v)


def _pair(item: Mapping[str, Any], key: str) -> tuple[float, float]:
    v = item.get(key)
    if not isinstance(v, (list, tuple)) or len(v) != 2 or not all(_finite(x) for x in v):
        raise ValueError(f"{key} 는 유한한 수 2개여야 한다: {v!r}")
    return (float(v[0]), float(v[1]))


FLOOR = SupportSurface(FLOOR_ID, 0.0, sigma_z=FLOOR_SIGMA_Z_M)
"""바닥 후보 (모듈 끝: 검사 도우미를 쓰므로 그 정의 뒤에 둔다)"""
