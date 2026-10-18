"""구역 판정용 수평 기하와 위치 불확실성 범위 (명세 7.1 불확실성 범위, 8.2 Point-in-Prism).

여러 규칙(R1 구역 진입, R6 관측 상실, 이후 R5 진입 예측 등)과 엔진이 같은 정의를 쓰도록 risk 아래 공용 모듈로 둔다.
표준 라이브러리만 쓴다(risk 는 numpy 를 쓰지 않는다).

**불확실성 범위(명세 7.1).** 수평 위치 공분산의 k=2(약 95%) 타원에 사람 점유 반경 0.3 m 를 더한 영역이다.
계약(world_state)의 수평 불확실성은 축별 표준편차 `position_xy_sigma = [σx, σy]` 뿐이고 공분산의 비대각 항이 없다.
그래서 타원은 World 축에 정렬한 반축 (k·σx, k·σy) 타원으로 본다. 비대각 항이 계약에 생기면 여기만 바꾼다.

- 발생: 이 범위가 구역 다각형에 조금이라도 걸리면(경계에 닿는 것 포함) 걸린 것으로 본다.
- 해소: 범위가 다각형에서 완전히 벗어난 거리(gap)를 규칙이 해소 임계값과 비교한다.

타원이 원이 아니면(σx ≠ σy) 타원 둘레를 바깥에 접하는 48각형으로 근사한다. 근사는 늘 진짜 타원을 품으므로
걸림 판정은 보수적(조금 일찍 걸림)이고, 오차는 긴 반축의 0.21 % 이하다(반축 0.6 m 에서 1.3 mm).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from simai_ai.risk._parse import RiskInputError

Point = Sequence[float]
Polygon = Sequence[Sequence[float]]

UNCERTAINTY_K = 2.0
"""수평 공분산 타원 배수 (명세 7.1: k=2, 약 95%)"""
OCCUPANCY_RADIUS_M = 0.3
"""사람 점유 반경 (명세 7.1: 0.3 m)"""
BOUNDARY_TOLERANCE_M = 1e-9
"""경계 접촉 허용오차(m). 정확히 닿는 경계(접선·꼭짓점·수직 접촉)가 부동소수점 끝자리 오차로 '안 닿음'이 되지 않게
이 거리 이하는 닿은 것(걸림·안쪽)으로 본다(안전 쪽, 명세 7.1 '조금이라도 걸리면')"""

_ELLIPSE_SIDES = 48
_CIRCUMSCRIBE = 1.0 / math.cos(math.pi / _ELLIPSE_SIDES)


def point_in_polygon(point: Point, polygon: Polygon, *, tolerance_m: float = BOUNDARY_TOLERANCE_M) -> bool:
    """수평 다각형 안(경계 포함, 경계에서 tolerance_m 이하도 안)인지. 짝수-홀수 규칙이라 오목 다각형도 된다."""
    if distance_to_polygon(point, polygon) <= tolerance_m:
        return True
    x, y = point[0], point[1]
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i][0], polygon[i][1]
        x2, y2 = polygon[(i + 1) % n][0], polygon[(i + 1) % n][1]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def distance_to_polygon(point: Point, polygon: Polygon) -> float:
    """점에서 다각형 경계까지의 수평 거리(m). 안쪽 점도 경계까지의 거리다(안팎은 point_in_polygon 으로 본다)."""
    best = math.inf
    n = len(polygon)
    for i in range(n):
        best = min(best, _point_segment_distance(point, polygon[i], polygon[(i + 1) % n]))
    return best


def distance_outside_polygon(point: Point, polygon: Polygon) -> float:
    """다각형 바깥이면 경계까지의 거리, 안(경계 포함)이면 0."""
    return 0.0 if point_in_polygon(point, polygon) else distance_to_polygon(point, polygon)


@dataclass(frozen=True)
class HorizontalRange:
    """수평 불확실성 범위: 중심 center, 축 정렬 타원 반축 semi_axes(=k·σ), 그 둘레에 더한 점유 반경 radius_m."""

    center: tuple[float, float]
    semi_axes: tuple[float, float]
    radius_m: float

    def __post_init__(self) -> None:
        values = (*self.center, *self.semi_axes, self.radius_m)
        if len(self.center) != 2 or len(self.semi_axes) != 2:
            raise RiskInputError("HorizontalRange: center·semi_axes 는 2차원이어야 한다")
        if any(isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) for v in values):
            raise RiskInputError("HorizontalRange: 값은 유한한 수여야 한다")
        if min(*self.semi_axes, self.radius_m) < 0:
            raise RiskInputError("HorizontalRange: 반축·반경은 0 이상이어야 한다")

    @classmethod
    def of(
        cls,
        center: Point,
        sigma: Point,
        *,
        k: float = UNCERTAINTY_K,
        radius_m: float = OCCUPANCY_RADIUS_M,
    ) -> "HorizontalRange":
        """중심과 축별 표준편차로 명세 7.1 범위를 만든다. 표준편차의 부호는 보지 않는다."""
        return cls(
            center=(float(center[0]), float(center[1])),
            semi_axes=(k * abs(float(sigma[0])), k * abs(float(sigma[1]))),
            radius_m=float(radius_m),
        )

    def gap_to(self, polygon: Polygon, *, exact_below: float = math.inf) -> float:
        """범위와 다각형(영역) 사이의 수평 간격(m). 걸리면(닿음 포함) 0.

        exact_below: 간격이 0 보다 크고 이 값 이상임이 확실하면 계산을 줄이고 그 하한을 돌려준다(규칙이 임계값과만
            비교할 때). 하한이 0 이면 늘 정확히 계산한다(걸림 여부는 정확하다).
        원(σx = σy)이면 늘 정확하고, 타원이면 48각형 근사(보수적)다.
        """
        center_distance = distance_outside_polygon(self.center, polygon)
        if center_distance == 0.0:
            return 0.0
        a, b = self.semi_axes
        if a == b:
            return max(0.0, center_distance - a - self.radius_m)
        lower = max(0.0, center_distance - max(a, b) * _CIRCUMSCRIBE - self.radius_m)
        if lower > 0.0 and lower >= exact_below:
            return lower
        return max(0.0, _region_distance(self._ellipse_polygon(), polygon) - self.radius_m)

    def overlaps(self, polygon: Polygon, *, tolerance_m: float = BOUNDARY_TOLERANCE_M) -> bool:
        """범위가 다각형에 조금이라도 걸리는지(명세 7.1 발생 조건). 간격이 tolerance_m 이하면 닿은 것으로 본다."""
        return self.gap_to(polygon, exact_below=tolerance_m) <= tolerance_m

    def ellipse_outline(self) -> list[tuple[float, float]]:
        """gap_to 가 타원(σx ≠ σy) 대신 쓰는, 타원을 바깥에 접하는 48각형의 꼭짓점(점유 반경 제외).

        움직이는 범위의 접촉 시각을 같은 기하로 구하려는 규칙(R5)이 쓴다.
        """
        return self._ellipse_polygon()

    def _ellipse_polygon(self) -> list[tuple[float, float]]:
        cx, cy = self.center
        a, b = self.semi_axes[0] * _CIRCUMSCRIBE, self.semi_axes[1] * _CIRCUMSCRIBE
        step = 2.0 * math.pi / _ELLIPSE_SIDES
        return [(cx + a * math.cos(i * step), cy + b * math.sin(i * step)) for i in range(_ELLIPSE_SIDES)]


def region_distance(first: Polygon, second: Polygon) -> float:
    """두 다각형 영역 사이 수평 거리(m). 겹치거나 닿으면 0. 작업면 footprint 끼리 맞붙었는지 볼 때 쓴다(R2·R3)."""
    return _region_distance(first, second)


def _region_distance(first: Polygon, second: Polygon) -> float:
    """두 다각형 영역 사이 거리. 한쪽 꼭짓점이 다른 쪽 안에 있거나 변이 만나면 0."""
    if any(point_in_polygon(p, second) for p in first) or any(point_in_polygon(p, first) for p in second):
        return 0.0
    best = math.inf
    n, m = len(first), len(second)
    for i in range(n):
        p1, p2 = first[i], first[(i + 1) % n]
        for j in range(m):
            best = min(best, _segment_distance(p1, p2, second[j], second[(j + 1) % m]))
            if best == 0.0:
                return 0.0
    return best


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    x, y = p[0], p[1]
    x1, y1 = a[0], a[1]
    dx, dy = b[0] - x1, b[1] - y1
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length2))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segment_distance(p1: Point, p2: Point, q1: Point, q2: Point) -> float:
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return 0.0
    return min(
        _point_segment_distance(p1, q1, q2),
        _point_segment_distance(p2, q1, q2),
        _point_segment_distance(q1, p1, p2),
        _point_segment_distance(q2, p1, p2),
    )
