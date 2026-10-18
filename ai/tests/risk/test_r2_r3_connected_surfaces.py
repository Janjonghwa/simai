"""R2·R3·R5 리뷰 수정 회귀 (명세 7.2 R2·R3·R5, 7.5, 5.2, FR-67·68·70).

- F1: 한 상판을 OBB 하나로 등록하든 여러 개로 나눠 등록하든 R2 발생·유지·해소와 R5 가장자리 예측이 같다(이어진 같은 층 작업면의
  외곽을 본다). L·U 자, 3·10 cm 틈, 계단식 여러 층, 회전 OBB 이음새 반례를 둔다.
- F2: 운영 R3 는 인접한 낮은 면과의 높이차로 판정하고 바닥 기준 단순화는 교실 시험 프로파일에만 쓴다.
- F3: R5 가장자리 예측이 가려진 동안 유지한 지지 표면(HELD)을 쓰면 확인 필요 PREDICTED 를 붙인다.
"""

import math

import pytest

from simai_ai.risk import (
    CLASSROOM_TEST_PROFILE,
    OPERATIONAL_HEIGHT_PROFILE,
    EdgeProximityRule,
    ElevatedPositionRule,
    RiskEngine,
    WorkSurface,
    Zone,
    default_registry,
)
from simai_ai.risk.common import ConfidenceReason
from simai_ai.risk.rules.height_common import HeightProfile
from simai_ai.risk.rules.r3_elevated_position import REASON_STANDING_UNREGISTERED
from tests.risk.test_r2_r3_height import _Run, ctx, track, with_hips


def box(sid, x0, x1, y0, y1, top=2.0, yaw=0.0):
    return WorkSurface(sid, top, ((x0 + x1) / 2, (y0 + y1) / 2), (x1 - x0, y1 - y0), yaw)


def person(x, y, sid, z=2.0, velocity=None, **kw):
    t = with_hips(track(xy=(x, y), anchor=[x, y, z], support_id=sid, **kw), z + 0.9)
    if velocity is not None:
        t["velocity"] = list(velocity)
    return t


def shown(findings):
    """비교용: (규칙, 등급, 유지, 확인 필요 이유, 수치 반올림)."""
    return [
        (
            f.rule_id,
            str(f.severity),
            f.holding,
            tuple(map(str, f.confidence_reasons)),
            {k: round(v, 6) for k, v in f.metrics.items()},
        )
        for f in findings
    ]


def r2_op():
    return EdgeProximityRule(profile=OPERATIONAL_HEIGHT_PROFILE)


def support_at(surfaces, x, y):
    """그 XY 를 덮는 작업면 ID(ContactEstimator 가 낼 지지 표면 ID 에 해당)."""
    return next(s.surface_id for s in surfaces if s.contains_xy((x, y)))


# --- F1: 리뷰어 재현 수치 ----------------------------------------------------------

SPLIT = [box("a", 0, 6, 0, 6), box("b", 6, 6.5, 0, 6)]
WHOLE = [box("whole", 0, 6.5, 0, 6)]


@pytest.mark.parametrize(
    ("x", "expected"),
    [(5.2, "raise"), (5.0, "hold"), (4.8 - 1e-6, None), (6.1, "raise")],  # 바깥 변 x = 6.5, 범위 반경 0.4
)
def test_split_obb_same_as_whole_for_r2(x, expected):
    split = shown(r2_op().evaluate(ctx(person(x, 3, support_at(SPLIT, x, 3)), surfaces=SPLIT)))
    whole = shown(r2_op().evaluate(ctx(person(x, 3, "whole"), surfaces=WHOLE)))
    assert split == whole
    if expected is None:
        assert split == []
    else:
        ((rule, severity, holding, reasons, metrics),) = split
        assert (rule, severity, holding, reasons) == ("R2", "CRITICAL", expected == "hold", ())
        assert metrics["edge_distance_m"] == pytest.approx(min(6.5 - x, x, 3.0))
        assert metrics["height_diff_m"] == pytest.approx(2.0)


def test_reviewer_case_raise_values():
    ((_, _, holding, _, metrics),) = shown(r2_op().evaluate(ctx(person(5.2, 3, "a"), surfaces=SPLIT)))
    assert holding is False and metrics == {"edge_distance_m": 1.3, "height_diff_m": 2.0}


def test_split_obb_same_as_whole_for_r5():
    rule = default_registry().create("R5", 1)
    split = shown(rule.evaluate(ctx(person(3.8, 3, "a", velocity=(1, 0, 0)), surfaces=SPLIT)))
    whole = shown(rule.evaluate(ctx(person(3.8, 3, "whole", velocity=(1, 0, 0)), surfaces=WHOLE)))
    assert split == whole
    ((rule_id, severity, holding, _, metrics),) = split
    assert (rule_id, severity, holding) == ("R5", "CAUTION", False)
    assert metrics["time_to_entry_s"] == pytest.approx(1.3)


def test_active_r2_not_resolved_when_walking_to_other_obb_of_same_top():
    """리뷰 재현: B 의 (6.1, 3)에서 활성 R2 → A 의 (5.2, 3)에 4초 정지(실제 간격 0.9 m) → 해소되면 안 된다."""
    run = _Run(surfaces=SPLIT)
    run.engine = RiskEngine([default_registry().create("R2", 1)], run.clock)
    for _ in range(3):
        run.step(person(6.1, 3, "b"))
    assert set(run.active()) == {"R2"}
    for _ in range(40):
        run.step(person(5.2, 3, "a"))
    assert set(run.active()) == {"R2"}
    for _ in range(40):  # 바깥 변에서 1.3 m 넘게 떨어지면(범위 간격 1.3 m 이상) 3초 뒤 해소
        run.step(person(4.7, 3, "a"))
    assert run.active() == {}


# --- F1 반례: 모양·틈·층·회전 --------------------------------------------------------


def grid(surfaces, rule, points, z=2.0):
    out = {}
    for x, y in points:
        sid = support_at(surfaces, x, y)
        out[(x, y)] = shown(rule.evaluate(ctx(person(x, y, sid, z=z), surfaces=surfaces)))
    return out


def inside(surfaces, step=0.25):
    xs = [s.footprint for s in surfaces]
    lo_x = min(c[0] for f in xs for c in f)
    hi_x = max(c[0] for f in xs for c in f)
    lo_y = min(c[1] for f in xs for c in f)
    hi_y = max(c[1] for f in xs for c in f)
    pts = []
    x = lo_x + step / 2
    while x < hi_x:
        y = lo_y + step / 2
        while y < hi_y:
            if any(s.contains_xy((x, y)) for s in surfaces):
                pts.append((round(x, 6), round(y, 6)))
            y += step
        x += step
    return pts


def test_l_shape_two_splits_agree():
    """L 자 상판([0,4]×[0,1] ∪ [0,1]×[0,4])을 서로 다르게 나눈 두 등록이 모든 위치에서 같은 R2 를 낸다."""
    split1 = [box("l1", 0, 4, 0, 1), box("l2", 0, 1, 1, 4)]
    split2 = [box("m1", 1, 4, 0, 1), box("m2", 0, 1, 0, 4)]
    pts = inside(split1)
    assert pts and pts == inside(split2)
    assert grid(split1, r2_op(), pts) == grid(split2, r2_op(), pts)


def test_u_shape_two_splits_agree_and_inner_edge_counts():
    """U 자 상판(가운데가 빈 곳)의 두 분할이 같은 R2 를 내고, 아래 막대 위 사람에게 U 안쪽 변(바닥으로 떨어짐)이 가장자리다."""
    split1 = [box("u1", 0, 1, 0, 4), box("u2", 1, 3, 0, 1), box("u3", 3, 4, 0, 4)]
    split2 = [box("v1", 0, 1, 1, 4), box("v2", 0, 4, 0, 1), box("v3", 3, 4, 1, 4)]
    pts = inside(split1)
    assert grid(split1, r2_op(), pts) == grid(split2, r2_op(), pts)
    # 아래 막대 가운데(2.0, 0.5): 앞변 y=0, 안쪽 변 y=1 모두 0.5 m
    ((_, _, holding, _, metrics),) = shown(r2_op().evaluate(ctx(person(2.0, 0.5, "u2"), surfaces=split1)))
    assert not holding and metrics["edge_distance_m"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("gap", "x", "expected_distance", "holding"),
    [
        (0.03, 5.2, 6.5 + 0.03 - 5.2, False),  # 3 cm 틈: 이어진 층, 이음새는 가장자리 아님, B 바깥 변까지
        (0.10, 5.2, 0.8, False),  # 10 cm 틈: 따로 떨어진 면, A 의 오른쪽 변(x = 6)이 위험 가장자리
        (0.03, 4.5, None, None),  # 3 cm: B 바깥 변까지 2.03 m → 간격 1.63 m, 판정 없음
        (0.10, 4.5, 1.5, True),  # 10 cm: A 오른쪽 변 1.5 m → 간격 1.1 m, 유지
    ],
)
def test_small_gaps(gap, x, expected_distance, holding):
    surfaces = [box("a", 0, 6, 0, 6), box("b", 6 + gap, 6.5 + gap, 0, 6)]
    findings = shown(r2_op().evaluate(ctx(person(x, 3, "a"), surfaces=surfaces)))
    if expected_distance is None:
        assert findings == []
        return
    ((_, _, h, _, metrics),) = findings
    assert h is holding and metrics["edge_distance_m"] == pytest.approx(expected_distance)


def test_far_separate_same_height_surface_not_merged():
    """멀리 떨어진 같은 높이 작업면은 서 있는 층에 넣지 않는다."""
    from simai_ai.risk.rules.height_common import standing
    from simai_ai.risk.rules.r1_zone_entry import horizontal_ranges
    from tests.risk.test_r2_r3_height import world

    surfaces = [box("a", 0, 6, 0, 6), box("far", 10, 12, 0, 6), box("gap10", 6.1, 7, 0, 6)]
    t = world(person(3, 3, "a")).tracks[0]
    st = standing(t, surfaces, horizontal_ranges(t))
    assert [s.surface_id for s in st.surfaces] == ["a"]


def test_stepped_levels_use_adjacent_drop():
    """계단식 세 층(0.2 / 0.4 / 0.6 m, 맞붙음)을 운영 방식(인접 낮은 면 기준) 기준 0.3 m 로 본다.
    가운데 층의 앞뒤 변은 바닥으로 0.4 m, 왼쪽 변은 아래 층으로 0.2 m, 오른쪽 변은 위층에 막힌다."""
    profile = HeightProfile("adjacent-0.3", 0.3, test=False)
    steps = [box("s1", 0, 1, 0, 3, top=0.2), box("s2", 1, 2, 0, 3, top=0.4), box("s3", 2, 3, 0, 3, top=0.6)]
    r3 = ElevatedPositionRule(profile=profile)
    r2 = EdgeProximityRule(profile=profile)
    (f,) = r3.evaluate(ctx(person(1.5, 1.5, "s2", z=0.4), surfaces=steps))
    assert f.metrics["height_diff_m"] == pytest.approx(0.4)  # 0.6 이 아니라 인접 낮은 면(바닥)과의 차
    assert list(r3.evaluate(ctx(person(0.5, 1.5, "s1", z=0.2), surfaces=steps))) == []
    (f,) = r3.evaluate(ctx(person(2.5, 1.5, "s3", z=0.6), surfaces=steps))
    assert f.metrics["height_diff_m"] == pytest.approx(0.6)
    # 가운데 층 앞변(y = 0) 가까이: 앞변(바닥 0.4 m 낙차)이 위험 가장자리, 왼쪽(0.2 m)은 아님
    (f,) = r2.evaluate(ctx(person(1.1, 0.5, "s2", z=0.4), surfaces=steps))
    assert f.metrics["edge_distance_m"] == pytest.approx(0.5)


def _rotate(points, yaw, origin=(5.0, 5.0)):
    c, s = math.cos(yaw), math.sin(yaw)
    return [(round(origin[0] + x * c - y * s, 9), round(origin[1] + x * s + y * c, 9)) for x, y in points]


def test_rotated_split_seam_same_as_whole():
    """30° 회전한 6.5 × 6 상판을 OBB 하나로, 또는 이음새로 나눈 둘로 등록해도 같은 R2 를 낸다."""
    yaw = math.pi / 6
    (cw,) = _rotate([(0.0, 0.0)], yaw)
    whole = [WorkSurface("whole", 2.0, cw, (6.5, 6.0), yaw)]
    ca, cb = _rotate([(-0.25, 0.0), (3.0, 0.0)], yaw)
    split = [WorkSurface("a", 2.0, ca, (6.0, 6.0), yaw), WorkSurface("b", 2.0, cb, (0.5, 6.0), yaw)]
    local = [(1.95, 0.0), (2.15, 0.5), (2.9, -1.0), (1.6, 2.4), (0.0, 0.0)]
    for x, y in _rotate(local, yaw):
        s = shown(r2_op().evaluate(ctx(person(x, y, support_at(split, x, y)), surfaces=split)))
        w = shown(r2_op().evaluate(ctx(person(x, y, "whole"), surfaces=whole)))
        assert len(s) == len(w)
        for a, b in zip(s, w, strict=True):
            assert a[:4] == b[:4]
            assert a[4]["edge_distance_m"] == pytest.approx(b[4]["edge_distance_m"], abs=1e-6)


# --- F2: 운영 R3 는 인접 낮은 면 기준 -------------------------------------------------

INNER = WorkSurface("inner", 3.0, (0.0, 0.0), (4.0, 4.0))
OUTER = WorkSurface("outer", 2.5, (0.0, 0.0), (12.0, 12.0))


def test_operational_r3_uses_adjacent_lower_surface():
    on_inner = person(0, 0, "inner", z=3.0)
    assert (
        list(ElevatedPositionRule(profile=OPERATIONAL_HEIGHT_PROFILE).evaluate(ctx(on_inner, surfaces=[INNER, OUTER])))
        == []
    )
    # 교실 시험 프로파일은 바닥 기준 단순화(서 있는 면 3.0 − 0)
    (f,) = ElevatedPositionRule(profile=CLASSROOM_TEST_PROFILE).evaluate(ctx(on_inner, surfaces=[INNER, OUTER]))
    assert f.metrics["height_diff_m"] == pytest.approx(3.0)
    # 바깥 2.5 m 상판의 바깥 변은 바닥으로 2.5 m → 운영 고소 위치, 근거 수치도 2.5
    (f,) = ElevatedPositionRule(profile=OPERATIONAL_HEIGHT_PROFILE).evaluate(
        ctx(person(5.0, 0, "outer", z=2.5), surfaces=[INNER, OUTER])
    )
    assert f.metrics["height_diff_m"] == pytest.approx(2.5)


def test_operational_r3_inner_case_through_lifecycle_has_no_event():
    run = _Run(surfaces=[INNER, OUTER])
    run.engine = RiskEngine([default_registry().create("R3", 1)], run.clock)
    for _ in range(10):
        run.step(person(0, 0, "inner", z=3.0))
    assert run.transitions == [] and run.signals == []


def test_operational_r3_unregistered_height_is_conservative():
    """접지점 높이에 등록 작업면이 없으면 인접 면을 몰라 바닥 기준으로 보수적으로 판정한다."""
    t = person(1.0, 1.0, "scaffold-x", z=2.5)
    (f,) = ElevatedPositionRule(profile=OPERATIONAL_HEIGHT_PROFILE).evaluate(ctx(t, surfaces=[]))
    assert f.reasons == (REASON_STANDING_UNREGISTERED,) and f.metrics["height_diff_m"] == pytest.approx(2.5)


def test_operational_r3_unknown_z_uses_adjacent_drop():
    t = track(xy=(0.0, 0.0), anchor=None)
    assert (
        list(ElevatedPositionRule(profile=OPERATIONAL_HEIGHT_PROFILE).evaluate(ctx(t, surfaces=[INNER, OUTER]))) != []
    )
    # inner 위만 걸치는 경우에도 inner 와 이어진 층은 없고 outer 는 범위가 걸쳐(inner 를 감쌈) 후보다 → 2.5 m 낙차
    (f,) = ElevatedPositionRule(profile=OPERATIONAL_HEIGHT_PROFILE).evaluate(ctx(t, surfaces=[INNER, OUTER]))
    assert f.metrics["height_diff_m"] == pytest.approx(2.5) and f.confidence_reasons == (ConfidenceReason.Z_UNKNOWN,)


# --- F3: R5 가장자리 + HELD → PREDICTED ----------------------------------------------

STAGE = WorkSurface("stage-1", 0.4, (3.0, 3.0), (6.0, 6.0))


def stage_person(**kw):
    t = track(xy=(3.0, 3.0), anchor=[3.0, 3.0, 0.4], support_id="stage-1", **kw)
    t["velocity"] = [-1.0, 0.0, 0.0]
    return t


def test_r5_edge_with_held_support_is_predicted():
    rule = default_registry().create("R5", 1001)
    (f,) = rule.evaluate(ctx(stage_person(z_validity="UNCERTAIN", contact_validity="UNCERTAIN"), surfaces=[STAGE]))
    assert f.confidence_reasons == (ConfidenceReason.PREDICTED,) and f.needs_check
    assert f.metrics["time_to_entry_s"] == pytest.approx(1.6)
    # 걷기(contact 만 UNCERTAIN, z VALID)는 높이가 관측값이라 붙이지 않는다
    (f,) = rule.evaluate(ctx(stage_person(contact_validity="UNCERTAIN"), surfaces=[STAGE]))
    assert f.confidence_reasons == ()


def test_r5_edge_held_velocity_unknown_hold_is_predicted():
    t = stage_person(z_validity="UNCERTAIN", contact_validity="UNCERTAIN")
    t["velocity"] = None
    (f,) = default_registry().create("R5", 1001).evaluate(ctx(t, surfaces=[STAGE]))
    assert f.holding and f.confidence_reasons == (ConfidenceReason.PREDICTED,)


def test_r5_zone_target_meaning_unchanged_with_held_anchor():
    """R1 구역 대상 R5 는 HELD 접지점이어도 PREDICTED 를 붙이지 않는다(기존 의미 유지)."""
    zone = Zone.from_json(
        {
            "zone_id": "z-1",
            "zone_version": "zone-1",
            "judge_type": "GROUND_FOOTPRINT",
            "polygon_xy": [[-3.0, 2.0], [-1.0, 2.0], [-1.0, 4.0], [-3.0, 4.0]],
            "z_range": None,
            "severity": "HIGH",
        }
    )
    t = stage_person(z_validity="UNCERTAIN", contact_validity="UNCERTAIN")
    t["position_xy"], t["ground_anchor_xyz"] = [1.0, 3.0], [1.0, 3.0, 0.4]
    from simai_ai.risk import RuleContext
    from tests.risk.test_r2_r3_height import world

    rule = default_registry().create("R5", 1001)
    findings = list(rule.evaluate(RuleContext(now_ms=world(t).t_latest_global, world=world(t), zones=(zone,))))
    (f,) = findings
    assert f.zone_id == "z-1" and f.confidence_reasons == ()
