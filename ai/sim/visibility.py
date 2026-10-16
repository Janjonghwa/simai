"""가림 판정. 카메라 중심에서 관절까지의 선분이 가림 물체를 지나면 가려진 것으로 본다.

- 다른 사람: 지지 표면에서 키 높이까지의 연직 원기둥(반지름 radius_m)
- 단상 등 상자: 축 정렬 상자
자기 몸에 의한 가림은 얼굴(코·눈)만 본다: 카메라가 머리 뒤쪽에 있으면 가려진 것으로 한다.
몸통·팔다리의 자기 가림은 모델링하지 않는다.
"""

import math
from dataclasses import dataclass

from sim import vec
from sim.vec import Vec3

_EPS_M = 0.02
"""관절 바로 앞 이 거리 안의 교차는 가림으로 보지 않는다(관절이 물체 표면에 붙은 경우)."""

Interval = tuple[float, float]


@dataclass(frozen=True)
class Cylinder:
    center_xy: tuple[float, float]
    radius: float
    z_min: float
    z_max: float


@dataclass(frozen=True)
class Box:
    lo: Vec3
    hi: Vec3


def is_occluded(origin: Vec3, target: Vec3, cylinders: list[Cylinder], boxes: list[Box]) -> bool:
    d = vec.sub(target, origin)
    length = vec.norm(d)
    if length < 1e-9:
        return False
    s_limit = 1.0 - _EPS_M / length
    for cyl in cylinders:
        if _hits(_cylinder_interval(origin, d, cyl), s_limit):
            return True
    for box in boxes:
        if _hits(_box_interval(origin, d, box), s_limit):
            return True
    return False


def face_turned_away(heading_rad: float, head: Vec3, origin: Vec3) -> bool:
    """카메라가 얼굴 앞 반공간 밖(머리 뒤)에 있으면 True."""
    return math.cos(heading_rad) * (origin[0] - head[0]) + math.sin(heading_rad) * (origin[1] - head[1]) < 0


def _hits(interval: Interval | None, s_limit: float) -> bool:
    if interval is None:
        return False
    enter, leave = max(interval[0], 0.0), min(interval[1], 1.0)
    return enter < leave and enter < s_limit


def _slab(o: float, d: float, lo: float, hi: float) -> Interval | None:
    if abs(d) < 1e-12:
        return (-math.inf, math.inf) if lo <= o <= hi else None
    a, b = (lo - o) / d, (hi - o) / d
    return (min(a, b), max(a, b))


def _intersect(*intervals: Interval | None) -> Interval | None:
    if any(i is None for i in intervals):
        return None
    enter = max(i[0] for i in intervals)  # type: ignore[index]
    leave = min(i[1] for i in intervals)  # type: ignore[index]
    return (enter, leave) if enter < leave else None


def _box_interval(o: Vec3, d: Vec3, box: Box) -> Interval | None:
    return _intersect(*(_slab(o[k], d[k], box.lo[k], box.hi[k]) for k in range(3)))


def _cylinder_interval(o: Vec3, d: Vec3, cyl: Cylinder) -> Interval | None:
    fx, fy = o[0] - cyl.center_xy[0], o[1] - cyl.center_xy[1]
    a = d[0] * d[0] + d[1] * d[1]
    c = fx * fx + fy * fy - cyl.radius * cyl.radius
    if a < 1e-12:
        radial: Interval | None = (-math.inf, math.inf) if c <= 0 else None
    else:
        b = 2 * (fx * d[0] + fy * d[1])
        disc = b * b - 4 * a * c
        if disc < 0:
            return None
        sq = math.sqrt(disc)
        radial = ((-b - sq) / (2 * a), (-b + sq) / (2 * a))
    return _intersect(radial, _slab(o[2], d[2], cyl.z_min, cyl.z_max))
