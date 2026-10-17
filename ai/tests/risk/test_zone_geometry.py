"""구역 수평 기하와 위치 불확실성 범위 (명세 7.1 불확실성 범위: k=2 타원 + 점유 반경 0.3 m)."""

import math

import pytest

from simai_ai.risk import HorizontalRange
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.zone_geometry import (
    OCCUPANCY_RADIUS_M,
    UNCERTAINTY_K,
    distance_outside_polygon,
    distance_to_polygon,
    point_in_polygon,
)

SQUARE = ((3.0, 4.5), (4.0, 4.5), (4.0, 5.5), (3.0, 5.5))
CONCAVE = ((0.0, 0.0), (3.0, 0.0), (3.0, 3.0), (2.0, 3.0), (2.0, 1.0), (1.0, 1.0), (1.0, 3.0), (0.0, 3.0))


def test_spec_constants():
    assert UNCERTAINTY_K == 2.0
    assert OCCUPANCY_RADIUS_M == 0.3


def test_point_and_distance():
    assert point_in_polygon((4.0, 5.0), SQUARE)  # 경계 포함
    assert distance_outside_polygon((3.5, 5.0), SQUARE) == 0.0
    assert distance_outside_polygon((4.5, 5.0), SQUARE) == pytest.approx(0.5)
    assert distance_to_polygon((3.5, 5.0), SQUARE) == pytest.approx(0.5)  # 안쪽 점도 경계까지 거리
    assert not point_in_polygon((1.5, 2.0), CONCAVE)  # 오목한 홈
    assert distance_outside_polygon((1.5, 2.0), CONCAVE) == pytest.approx(0.5)


def test_circle_gap_is_exact():
    # 경계 1.0 m 밖, σ 0.1 → 반축 0.2, + 점유 0.3 → 간격 0.5
    r = HorizontalRange.of((5.0, 5.0), (0.1, 0.1))
    assert r.semi_axes == (0.2, 0.2) and r.radius_m == 0.3
    assert r.gap_to(SQUARE) == pytest.approx(0.5)
    assert not r.overlaps(SQUARE)


def test_exact_tangent_within_tolerance_overlaps():
    # 중심 (4.4, 5.0), 반축 0.1 + 0.3 = 0.4 → 오른쪽 변 x=4.0 에 정확히 닿는다. 부동소수점 간격 ~4e-16.
    r = HorizontalRange.of((4.4, 5.0), (0.05, 0.05))
    assert r.gap_to(SQUARE) < 1e-12
    assert r.overlaps(SQUARE)
    assert not HorizontalRange.of((4.4 + 1e-6, 5.0), (0.05, 0.05)).overlaps(SQUARE)
    assert not r.overlaps(SQUARE, tolerance_m=0.0)  # 허용오차를 끄면 끝자리 오차로 '안 닿음'
    assert point_in_polygon((4.0 + 1e-10, 5.0), SQUARE)  # 경계에서 허용오차 이하는 안쪽


def test_touching_counts_as_overlap():
    # 경계 0.5 m 밖, 반축 0.2 + 0.3 = 0.5 → 닿음 = 걸림(조금이라도 걸리면 발생)
    r = HorizontalRange.of((4.5, 5.0), (0.1, 0.1))
    assert r.gap_to(SQUARE) == 0.0
    assert r.overlaps(SQUARE)


def test_center_inside_overlaps_even_without_uncertainty():
    r = HorizontalRange.of((3.5, 5.0), (0.0, 0.0), radius_m=0.0)
    assert r.overlaps(SQUARE)
    assert HorizontalRange.of((4.2, 5.0), (0.0, 0.0), radius_m=0.0).gap_to(SQUARE) == pytest.approx(0.2)


def test_ellipse_uses_axis_sigma():
    # x 방향 σ 0.3(반축 0.6)·y 방향 σ 0.05(반축 0.1). 경계까지 0.8 m.
    wide = HorizontalRange.of((4.8, 5.0), (0.3, 0.05))  # x 로 0.8 → 0.6+0.3 ≥ 0.8 → 걸림
    assert wide.overlaps(SQUARE)
    tall = HorizontalRange.of((3.5, 6.3), (0.3, 0.05))  # y 로 0.8 → 0.1+0.3 = 0.4 → 간격 0.4
    gap = tall.gap_to(SQUARE)
    # 바깥에 접하는 다각형 근사라 간격은 정확한 값 이하이고 긴 반축의 0.3 % 안이다(보수적).
    assert 0.4 - 0.6 * 0.003 <= gap <= 0.4 + 1e-12


def test_ellipse_corner_gap_matches_sampled_truth():
    r = HorizontalRange.of((4.6, 6.0), (0.25, 0.1))
    # 정답: 타원 둘레를 촘촘히 돌며 다각형까지의 최소 거리
    truth = min(
        distance_outside_polygon((4.6 + 0.5 * math.cos(t), 6.0 + 0.2 * math.sin(t)), SQUARE)
        for t in (i * 2 * math.pi / 20000 for i in range(20000))
    )
    truth = max(0.0, truth - 0.3)
    assert truth - 0.5 * 0.003 <= r.gap_to(SQUARE) <= truth + 1e-6


def test_small_zone_inside_big_ellipse_overlaps():
    tiny = ((10.0, 10.0), (10.1, 10.0), (10.1, 10.1), (10.0, 10.1))
    r = HorizontalRange.of((10.6, 10.05), (0.5, 0.2), radius_m=0.0)  # 구역이 타원 안에 통째로 든다
    assert r.overlaps(tiny)


def test_overlaps_is_exact_for_ellipse_near_minor_axis():
    # 긴 반축(x 0.6)+0.3 안이지만 짧은 반축(y 0.1) 방향으로 0.8 떨어져 있다 → 걸리지 않는다
    r = HorizontalRange.of((3.5, 6.3), (0.3, 0.05))
    assert not r.overlaps(SQUARE)
    assert r.gap_to(SQUARE, exact_below=0.0) > 0.39


def test_exact_below_returns_lower_bound():
    r = HorizontalRange.of((8.0, 5.0), (0.3, 0.05))
    lower = r.gap_to(SQUARE, exact_below=0.3)
    exact = r.gap_to(SQUARE)
    assert lower >= 0.3
    assert lower <= exact


def test_concave_polygon_notch():
    # 홈 가운데(1.5, 2.5)에서 양쪽 벽까지 0.5 m. 반축 0.1 + 0.3 = 0.4 → 간격 0.1
    r = HorizontalRange.of((1.5, 2.5), (0.05, 0.05))
    assert r.gap_to(CONCAVE) == pytest.approx(0.1)
    assert HorizontalRange.of((1.5, 2.5), (0.1, 0.1)).overlaps(CONCAVE)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"center": (0.0, 0.0), "semi_axes": (-0.1, 0.1), "radius_m": 0.3},
        {"center": (0.0, math.nan), "semi_axes": (0.1, 0.1), "radius_m": 0.3},
        {"center": (0.0, 0.0), "semi_axes": (0.1, 0.1), "radius_m": -1.0},
        {"center": (0.0, 0.0), "semi_axes": (0.1, 0.1), "radius_m": True},
    ],
)
def test_invalid_range(kwargs):
    with pytest.raises(RiskInputError):
        HorizontalRange(**kwargs)
