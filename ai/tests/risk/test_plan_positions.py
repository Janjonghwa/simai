"""PLANNED 판정 변환과 계획 작업 위치 생성(simai_ai.risk.plan, S15P31A101-99, 명세 8.3·19.1).

생성 규칙: 작업면 가운데(넓으면 가장자리 안쪽 하나 더), 유효한 개구부 구역 경계 바깥 0.2 m. 결정적이고 gen- 접두, 요청 위치와 섞지 않는다.
"""

from __future__ import annotations

import copy
import math
import random

import pytest

from simai_ai.providers.testing import load_example
from simai_ai.risk import plan
from simai_ai.risk.plan import (
    PlanLayoutError,
    PlanRuleError,
    evaluate_plan,
    generate_positions,
)
from simai_ai.risk.rules.r2_edge_proximity import R2_RAISE_DISTANCE_M
from simai_ai.risk.work_surface import WorkSurface
from simai_ai.risk.zone import Zone
from simai_ai.risk.zone_geometry import OCCUPANCY_RADIUS_M, point_in_polygon

T = 1760342400000


def _payload(**layout) -> dict:
    p = copy.deepcopy(load_example("risk_plan_request.generated_positions.json")["payload"])
    p["layout"].update(layout)
    return p


def _surface(sid="s-1", size=(6.0, 1.2), center=(6.0, 4.0), yaw=0.0, top=3.2) -> dict:
    return {"surface_id": sid, "top_height_m": top, "center_xy": list(center), "size_xy": list(size), "yaw_rad": yaw}


def _opening(zid="o-1", poly=((2.0, 1.0), (3.0, 1.0), (3.0, 2.0), (2.0, 2.0)), **kw) -> dict:
    z = {
        "zone_id": zid,
        "zone_version": "v1",
        "judge_type": "GROUND_FOOTPRINT",
        "polygon_xy": [list(p) for p in poly],
        "z_range": None,
        "severity": "CRITICAL",
        "zone_type": "OPENING",
    }
    return z | kw


def _items_for(outcome, pid):
    return [i for i in outcome.items if pid in i["position_ids"]]


def _rules(outcome, pid):
    return {i["rule_id"] for i in _items_for(outcome, pid)}


# ---------------------------------------------------------------- 계약 예시


@pytest.mark.parametrize(
    ("request_name", "result_name"),
    [
        ("risk_plan_request.json", "risk_plan_result.json"),
        ("risk_plan_request.generated_positions.json", "risk_plan_result.generated_positions.json"),
    ],
)
def test_contract_examples(request_name, result_name):
    outcome = evaluate_plan(load_example(request_name)["payload"])
    expected = load_example(result_name)["payload"]
    assert [p.to_json() for p in outcome.positions] == expected["positions"]
    assert list(outcome.items) == expected["items"]
    assert outcome.evaluation.alert_candidates() == ()


def test_empty_positions_same_as_missing():
    missing = evaluate_plan(_payload())
    empty = evaluate_plan(_payload(work_positions=[]))
    assert missing.positions == empty.positions and missing.items == empty.items
    assert all(p.generated and p.position_id.startswith("gen-") for p in empty.positions)


def test_requested_positions_are_not_mixed_with_generated():
    """위치가 하나라도 있으면 그것만 쓴다(작업면·개구부가 있어도 만들지 않는다)."""
    one = {"position_id": "p-9", "position_xy": [0.0, 0.0], "support_surface_id": "floor"}
    outcome = evaluate_plan(_payload(work_positions=[one]))
    assert [p.to_json() for p in outcome.positions] == [one | {"position_source": "REQUESTED"}]
    assert outcome.items == ()


# ---------------------------------------------------------------- 결정성


def test_deterministic_and_order_independent():
    surfaces = [_surface("b", (5.0, 5.0), (20.0, 20.0)), _surface("a"), _surface("c", (1.0, 1.0), (30.0, 0.0), 0.7, 0.5)]
    zones = [_opening("o-2", ((40, 0), (41, 0), (41, 1), (40, 1))), _opening()]
    base = evaluate_plan(_payload(work_surfaces=surfaces, zones=zones))
    rng = random.Random(7)
    for _ in range(5):
        s2, z2 = surfaces[:], zones[:]
        rng.shuffle(s2)
        rng.shuffle(z2)
        again = evaluate_plan(_payload(work_surfaces=s2, zones=z2))
        assert again.positions == base.positions
        assert again.items == base.items
    ids = [p.position_id for p in base.positions]
    assert ids == ["gen-a-01", "gen-b-01", "gen-b-02", "gen-c-01", "gen-o-1-01", "gen-o-2-01"]


def test_surface_and_zone_with_same_id_get_distinct_ids():
    """반례: 작업면과 구역이 같은 ID 면 순번을 이어 겹치지 않는다(겹치면 엔진 world_id 중복)."""
    out = evaluate_plan(_payload(work_surfaces=[_surface("x", (4.0, 4.0))], zones=[_opening("x")]))
    ids = [p.position_id for p in out.positions]
    assert ids == ["gen-x-01", "gen-x-02", "gen-x-03"]
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------- 작업면 위치 (R2·R3)


def test_narrow_surface_center_triggers_r2_and_r3():
    out = evaluate_plan(_payload(zones=[], work_surfaces=[_surface()]))
    assert [p.position_id for p in out.positions] == ["gen-s-1-01"]
    assert _rules(out, "gen-s-1-01") == {"R2", "R3"}
    assert all(p.head_height_m is None for p in out.positions)  # 머리 높이는 지어내지 않는다


@pytest.mark.parametrize(
    ("half_min", "extra"),
    [
        (R2_RAISE_DISTANCE_M + OCCUPANCY_RADIUS_M, False),  # 경계: 가운데 간격 = 1.0 m → 가운데가 이미 R2 발생
        (R2_RAISE_DISTANCE_M + OCCUPANCY_RADIUS_M + 0.01, True),
    ],
)
def test_wide_surface_gets_edge_position_at_boundary(half_min, extra):
    out = evaluate_plan(_payload(zones=[], work_surfaces=[_surface(size=(8.0, 2 * half_min))]))
    ids = [p.position_id for p in out.positions]
    assert ids == (["gen-s-1-01", "gen-s-1-02"] if extra else ["gen-s-1-01"])
    assert "R3" in _rules(out, "gen-s-1-01")
    if extra:
        assert "R2" not in _rules(out, "gen-s-1-01")  # 넓은 면 가운데는 R2 밖 → 가장자리 위치가 R2 를 낸다
        assert _rules(out, "gen-s-1-02") == {"R2", "R3"}
    else:
        assert "R2" in _rules(out, "gen-s-1-01")


@pytest.mark.parametrize("yaw", [0.0, math.pi / 2, -math.pi / 3, math.pi])
def test_rotated_wide_surface_edge_position_stays_on_surface(yaw):
    s = WorkSurface(**_surface(size=(4.0, 3.0), center=(10.0, 10.0), yaw=yaw))
    positions = generate_positions([s], [])
    assert len(positions) == 2
    for p in positions:
        assert s.contains_xy(p.position_xy)
    u, v = s.local_xy(positions[1].position_xy)
    assert abs(abs(v) - (1.5 - plan.SURFACE_EDGE_INSET_M)) < 1e-3 and abs(u) < 1e-3  # 긴 변(4 m) 안쪽 0.5 m
    out = evaluate_plan(_payload(zones=[], work_surfaces=[_surface(size=(4.0, 3.0), center=(10.0, 10.0), yaw=yaw)]))
    assert "R2" in _rules(out, "gen-s-1-02")


def test_generated_coordinates_are_rounded_and_replayable():
    """결과에 실은 위치(mm 반올림)로 다시 판정하면(Spring 이 REQUESTED 로 다시 보냄) 같은 항목이 나온다."""
    surfaces = [_surface("r", (4.0, 3.0), (1.23456, 7.65432), 0.3)]
    zones = [_opening("o", ((0.1234, 0.5), (1.7777, 0.61), (1.5, 2.333)))]
    out = evaluate_plan(_payload(work_surfaces=surfaces, zones=zones))
    for p in out.positions:
        assert all(round(c, 3) == c for c in p.position_xy)
    replay = _payload(
        work_surfaces=surfaces,
        zones=zones,
        work_positions=[{k: v for k, v in p.to_json().items() if k != "position_source"} for p in out.positions],
    )
    again = evaluate_plan(replay)
    assert again.items == out.items


# ---------------------------------------------------------------- 개구부 위치 (R1)


@pytest.mark.parametrize("clockwise", [False, True])
def test_opening_position_outside_and_r1_holds(clockwise):
    poly = ((2.0, 1.0), (5.0, 1.0), (5.0, 2.0), (2.0, 2.0))
    poly = poly[::-1] if clockwise else poly
    out = evaluate_plan(_payload(work_surfaces=[], zones=[_opening(poly=poly)]))
    (p,) = out.positions
    assert p.support_surface_id == "floor" and p.generated
    assert not point_in_polygon(p.position_xy, poly)
    assert not _items_for(out, p.position_id)
    assert any(f.holding for f in out.evaluation.findings if f.rule_id == "R1")
    # 가장 긴 변(3 m) 쪽이다
    assert p.position_xy[1] in (0.8, 2.2)


def test_concave_opening_skips_edge_whose_outward_point_is_inside():
    """반례: 오목한 U 자 개구부에서 긴 변의 바깥 점이 다른 팔 안에 떨어지면 그 변을 건너뛰고 다음 변을 쓴다."""
    # 팔 두 개(폭 0.1 m)와 틈 0.15 m. 바깥 변은 5 m 둘로 나눠 안쪽 변(9.9 m 둘)이 가장 길다. 안쪽 변 바깥 0.2 m 는 맞은편 팔 안
    poly = (
        (0.0, 0.0), (0.35, 0.0), (0.35, 5.0), (0.35, 10.0), (0.25, 10.0),
        (0.25, 0.1), (0.1, 0.1), (0.1, 10.0), (0.0, 10.0), (0.0, 5.0),
    )
    zone = Zone.from_json(_opening(poly=poly))
    for skipped in ((0.05, 5.05), (0.3, 5.05)):
        assert point_in_polygon(skipped, zone.polygon_xy)  # 가장 긴 두 변의 바깥 점은 다각형 안이다
    (p,) = generate_positions([], [zone])
    assert p.position_xy == (0.55, 2.5)  # 다음으로 긴 변(오른쪽 바깥 아래 5 m)
    assert not point_in_polygon(p.position_xy, zone.polygon_xy)
    assert not evaluate_plan(_payload(work_surfaces=[], zones=[_opening(poly=poly)])).items


def test_only_active_opening_zones_get_positions():
    expired = _opening("old", valid_from=T - 10_000, valid_to=T)  # [from, to) 라 T 에는 유효하지 않다
    future = _opening("new", valid_from=T + 1)
    equip = _opening("eq", zone_type="EQUIPMENT_PARKING")
    untyped = _opening("nt", zone_type=None)
    out = evaluate_plan(_payload(work_surfaces=[], zones=[expired, future, equip, untyped, _opening("now", valid_from=T)]))
    assert [p.position_id for p in out.positions] == ["gen-now-01"]


def test_generated_position_inside_volume_zone_has_no_entry():
    """머리 높이를 모르는 VOLUME_3D 안 생성 위치는 발생 항목이 없다."""
    volume = {
        "zone_id": "z-vol",
        "zone_version": "v1",
        "judge_type": "VOLUME_3D",
        "polygon_xy": [[5.0, 3.0], [7.0, 3.0], [7.0, 5.0], [5.0, 5.0]],
        "z_range": [3.0, 6.0],
        "severity": "HIGH",
        "zone_type": "LIFTING",
    }
    out = evaluate_plan(_payload(zones=[volume], work_surfaces=[_surface()]))
    r1 = [i for i in _items_for(out, "gen-s-1-01") if i["rule_id"] == "R1"]
    assert not r1


def test_no_surfaces_no_openings_gives_no_positions():
    out = evaluate_plan(_payload(work_surfaces=[], zones=[_opening("eq", zone_type="EQUIPMENT_PARKING")]))
    assert out.positions == () and out.items == ()


# ---------------------------------------------------------------- 의미 검사 (계약 4·5단계)


def test_rule_errors():
    p = _payload()
    p["rules"]["rule_versions"] = [{"rule_id": "R2", "rule_version": 1}, {"rule_id": "R2", "rule_version": 1001}]
    with pytest.raises(PlanRuleError, match="R2"):
        evaluate_plan(p)
    p["rules"]["rule_versions"] = [{"rule_id": "R8", "rule_version": 1}]
    with pytest.raises(PlanRuleError, match="R8 v1"):
        evaluate_plan(p)


@pytest.mark.parametrize(
    "layout",
    [
        {"zones": [_opening(z_range=[4.0, 0.0])]},
        {"zones": [_opening(valid_from=T, valid_to=T - 1)]},
        {"zones": [_opening(), _opening()]},
        {"zones": [_opening(), _opening(zone_version="v2")]},
        {"work_surfaces": [_surface(), _surface()]},
        {"work_positions": [{"position_id": "p", "position_xy": [0, 0], "support_surface_id": "desk-9"}]},
        {
            "work_positions": [
                {"position_id": "p", "position_xy": [0, 0], "support_surface_id": "floor"},
                {"position_id": "p", "position_xy": [1, 0], "support_surface_id": "floor"},
            ]
        },
    ],
)
def test_layout_errors(layout):
    with pytest.raises(PlanLayoutError):
        evaluate_plan(_payload(**layout))


def test_two_versions_of_a_zone_at_different_times_is_fine():
    zones = [_opening(valid_to=T), _opening(zone_version="v2", valid_from=T)]
    out = evaluate_plan(_payload(work_surfaces=[], zones=zones))
    assert [p.position_id for p in out.positions] == ["gen-o-1-01"]


def test_integral_float_timestamps_are_accepted():
    """스키마의 integer 는 1.0 형태도 받는다. 엔진 형식으로 맞춰 INVALID_LAYOUT 이 되지 않는다."""
    p = _payload(zones=[_opening(valid_from=float(T - 1000), valid_to=float(T + 1000))])
    p["evaluation_time"] = float(T)
    assert [x.position_id for x in evaluate_plan(p).positions][-1] == "gen-o-1-01"
