"""PLANNED 판정: 계획 레이아웃 → Risk Engine 입력, 계획 작업 위치 생성 (명세 8.3, 11.1, 12.2, 19.1, S15P31A101-99).

EC2 Risk Engine 컨테이너(services/risk_engine)가 `risk_plan_request` payload 를 이 모듈로 판정한다. Tier 0 와 같은 엔진·같은
등록부를 쓰고(3.2, Java 재구현 금지), 이 모듈은 계약의 입력을 엔진 입력으로 바꾸기만 한다. 스키마 검증(jsonschema)은 risk 가
쓰지 않는 의존성이라 컨테이너가 먼저 한다. 여기서는 스키마를 통과한 payload 의 의미 검사(규칙 묶음 → PlanRuleError,
레이아웃 → PlanLayoutError)와 변환·판정만 한다.

**변환(계약 risk_plan_request work_position).** 계획 위치는 확실한 입력이다: 위치 불확실성 0(점유 반경 0.3 m 는 규칙이 더함),
접지 Z = 서 있는 면 상판 높이(floor 0), 머리 높이 = 접지 Z + head_height_m(없으면 모름: head_z 비움, VOLUME_3D 는 Z_UNKNOWN).
변환에 쓴 가상 값(관측 상태·카메라 이름)은 결과에 싣지 않는다. 결과 항목은 holding 이 아닌 판정만, metrics 는 mm 반올림이다.
계획 위치는 카메라·보정으로 잰 값이 아니라 보정 상태가 적용되지 않는다(calibration_state=None, '보정 상태 해당 없음'). 그래서 R2·R3·R4
결과에 PROVISIONAL_CALIB(명세 4.4, 측정 위치의 보정 불확실성)가 붙지 않는다.

**계획 작업 위치 생성(요청에 위치가 없거나 빈 배열일 때만, 규칙 POSITION_RULE).** 사람 대상 규칙(R1·R2·R3)이 레이아웃만으로
판정을 내도록 대표 위치를 만든다. 요청에 위치가 하나라도 있으면 그 위치만 쓰고 만들지 않는다(REQUESTED 와 섞지 않음).
1. 작업면마다(surface_id 순) 상판 가운데 1곳(R3 고소 위치, 좁은 면은 R2 도). 가운데가 R2 발생 거리 밖일 만큼 넓은 면
   (짧은 변 절반 − 점유 반경 > R2 발생 거리 1.0 m)은 가장 긴 변의 가운데에서 SURFACE_EDGE_INSET_M 안쪽 1곳을 더한다(R2).
2. 판정 시각에 유효한 개구부 구역(zone_type OPENING, zone_id 순)마다 바닥 1곳: 가장 긴 변의 가운데에서 바깥으로
   OPENING_OFFSET_M(점유 반경 0.3 m 안이라 R1 발생). 그 점이 다각형 안이면(오목) 다음으로 긴 변을 쓰고, 맞는 변이 없으면 만들지 않는다.
   그 밖의 구역 유형(장비 주차·양중 등)에는 만들지 않는다: 사람이 어디서 일할지 레이아웃에서 알 수 없다(필요하면 요청이 위치를 준다).
머리 높이는 주지 않는다(모르면 비운다, 규칙 2). ID 는 `gen-<surface_id 또는 zone_id>-<두 자리 순번>`(같은 ID 를 쓰는 작업면·구역은
순번을 이어 겹치지 않게) 이고 World ID 가 아니다(규칙 1). 좌표는 mm 로 반올림한 값으로 판정하고 그 값을 그대로 싣는다.
같은 레이아웃·판정 시각이면 입력 순서와 상관없이 같은 위치·같은 ID 다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.clock import FixedClock
from simai_ai.risk.common import Origin
from simai_ai.risk.engine import Evaluation, NonLiveAlertError, RiskEngine
from simai_ai.risk.registry import RuleRegistry, default_registry
from simai_ai.risk.rule import Finding, Rule
from simai_ai.risk.rules.r2_edge_proximity import R2_RAISE_DISTANCE_M
from simai_ai.risk.work_surface import FLOOR_ID, FLOOR_Z_M, WorkSurface, check_unique_ids
from simai_ai.risk.world import (
    IdentityConfidence,
    ObservationState,
    SupportSurfaceValidity,
    TrackLifecycle,
    Validity,
    WorldState,
    WorldTrack,
)
from simai_ai.risk.zone import Zone
from simai_ai.risk.zone_geometry import OCCUPANCY_RADIUS_M, point_in_polygon

POSITION_RULE = "plan-positions-1"
"""생성 규칙 식별자(바꾸면 올린다. 컨테이너 /healthz 에 싣는다)"""
GENERATED_ID_PREFIX = "gen-"
OPENING_ZONE_TYPE = "OPENING"
OPENING_OFFSET_M = 0.2
"""개구부 경계 바깥 거리. 점유 반경(0.3 m)보다 0.1 m 짧아 범위가 구역에 걸린다(R1 xy_uncertainty_overlap, 발끝이 개구부 턱에 닿는 위치)"""
SURFACE_EDGE_INSET_M = 0.5
"""넓은 작업면 가장자리 안쪽 거리(한 걸음). 점유 범위와 가장자리 간격 0.2 m 로 R2 발생 거리 1.0 m 안"""
COORD_DECIMALS = 3
METRIC_DECIMALS = 3
PLAN_CAMERAS = ("plan-a", "plan-b")
"""변환용 가상 카메라 두 대(계획 위치는 관측이 아니므로 SINGLE_VIEW 를 붙이지 않는다). 결과에 싣지 않는다"""


class PlanRuleError(ValueError):
    """규칙 묶음 오류(계약 UNKNOWN_RULE): 등록부에 없는 (ID, 버전), 같은 rule_id 의 다른 버전 둘."""


class PlanLayoutError(ValueError):
    """레이아웃 오류(계약 INVALID_LAYOUT): 엔진이 거부한 구역·작업면, ID 중복, 모르는 지지 표면, 같은 시각 유효 구역 버전 둘."""


@dataclass(frozen=True)
class PlanPosition:
    position_id: str
    position_xy: tuple[float, float]
    support_surface_id: str
    head_height_m: float | None
    generated: bool

    def to_json(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "position_id": self.position_id,
            "position_xy": list(self.position_xy),
            "support_surface_id": self.support_surface_id,
        }
        if self.head_height_m is not None:
            doc["head_height_m"] = self.head_height_m
        doc["position_source"] = "GENERATED" if self.generated else "REQUESTED"
        return doc


@dataclass(frozen=True)
class PlanOutcome:
    positions: tuple[PlanPosition, ...]
    items: tuple[dict[str, Any], ...]
    evaluation: Evaluation


def build_rules(rule_versions: Iterable[Mapping[str, Any]], registry: RuleRegistry | None = None) -> list[Rule]:
    registry = default_registry() if registry is None else registry
    keys = [(r["rule_id"], int(r["rule_version"])) for r in rule_versions]
    ids = [rid for rid, _ in keys]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise PlanRuleError(f"같은 규칙 ID 가 여러 버전으로 들어왔다: {', '.join(duplicated)}")
    try:
        return [registry.create(rid, ver) for rid, ver in keys]
    except LookupError as e:
        raise PlanRuleError(str(e)) from None


def read_layout(
    layout: Mapping[str, Any], t_ms: int
) -> tuple[tuple[Zone, ...], tuple[WorkSurface, ...], tuple[PlanPosition, ...]]:
    """구역·작업면·요청 위치를 읽고 의미 검사를 한다(계약 오류 판정 순서 5단계)."""
    try:
        zones = tuple(Zone.from_json(_zone_ms(z), f"zones[{i}]") for i, z in enumerate(layout["zones"]))
        surfaces = tuple(WorkSurface.from_json(s, f"work_surfaces[{i}]") for i, s in enumerate(layout["work_surfaces"]))
        check_unique_ids(surfaces, "work_surfaces")
    except RiskInputError as e:
        raise PlanLayoutError(str(e)) from None
    versions = [(z.zone_id, z.zone_version) for z in zones]
    if len(set(versions)) != len(versions):
        raise PlanLayoutError(f"zones: 같은 (zone_id, zone_version) 이 둘 이상이다 {_dups(versions)}")
    active = [z.zone_id for z in zones if z.is_active(t_ms)]
    if len(set(active)) != len(active):
        raise PlanLayoutError(f"zones: 판정 시각에 유효한 같은 구역 버전이 둘 이상이다 {_dups(active)}")
    supports = {FLOOR_ID} | {s.surface_id for s in surfaces}
    requested = tuple(
        PlanPosition(
            position_id=p["position_id"],
            position_xy=(p["position_xy"][0], p["position_xy"][1]),
            support_surface_id=p["support_surface_id"],
            head_height_m=p.get("head_height_m"),
            generated=False,
        )
        for p in layout.get("work_positions") or ()
    )
    ids = [p.position_id for p in requested]
    if len(set(ids)) != len(ids):
        raise PlanLayoutError(f"work_positions: position_id 가 겹친다 {_dups(ids)}")
    unknown = sorted({p.support_surface_id for p in requested} - supports)
    if unknown:
        raise PlanLayoutError(f"work_positions: support_surface_id 가 floor 도 작업면도 아니다 {unknown}")
    return zones, surfaces, requested


def generate_positions(surfaces: Iterable[WorkSurface], active_zones: Iterable[Zone]) -> tuple[PlanPosition, ...]:
    """요청에 위치가 없을 때 쓰는 대표 위치(모듈 머리말 생성 규칙). 입력 순서와 상관없이 같은 결과다."""
    counters: dict[str, int] = {}

    def make(base: str, xy: tuple[float, float], support: str) -> PlanPosition:
        counters[base] = counters.get(base, 0) + 1
        return PlanPosition(f"{GENERATED_ID_PREFIX}{base}-{counters[base]:02d}", _round_xy(xy), support, None, True)

    out: list[PlanPosition] = []
    for s in sorted(surfaces, key=lambda s: s.surface_id):
        out.append(make(s.surface_id, s.center_xy, s.surface_id))
        edge = _surface_edge_point(s)
        if edge is not None:
            out.append(make(s.surface_id, edge, s.surface_id))
    for z in sorted(active_zones, key=lambda z: (z.zone_id, z.zone_version)):
        if z.zone_type != OPENING_ZONE_TYPE:
            continue
        point = _opening_point(z.polygon_xy)
        if point is not None:
            out.append(make(z.zone_id, point, FLOOR_ID))
    return tuple(out)


def planned_world(
    positions: Sequence[PlanPosition], surfaces: Iterable[WorkSurface], t_ms: int
) -> WorldState:
    heights = {FLOOR_ID: FLOOR_Z_M} | {s.surface_id: s.top_height_m for s in surfaces}
    tracks = []
    for p in positions:
        x, y = p.position_xy
        z = heights[p.support_surface_id]
        tracks.append(
            WorldTrack(
                world_id=p.position_id,
                t_last_valid=t_ms,
                observation_state=ObservationState.OBSERVED,
                track_lifecycle=TrackLifecycle.CONFIRMED,
                identity_confidence=IdentityConfidence.HIGH,
                identity_revision=0,
                position_xy=(x, y),
                position_xy_sigma=(0.0, 0.0),
                pelvis_xyz=None,
                ground_anchor_xyz=(x, y, z),
                support_surface_id=p.support_surface_id,
                support_surface_validity=SupportSurfaceValidity.VALID,
                contact_validity=Validity.VALID,
                z_validity=Validity.VALID,
                # 머리 높이를 모르면 지어내지 않는다(head_z None, VOLUME_3D 는 Z_UNKNOWN)
                head_z=None if p.head_height_m is None else z + p.head_height_m,
                head_z_validity=None if p.head_height_m is None else Validity.VALID,
                cameras=PLAN_CAMERAS,
            )
        )
    return WorldState(t_latest_global=t_ms, calibration_version="plan", scene_version="plan", tracks=tuple(tracks))


def finding_item(f: Finding) -> dict[str, Any]:
    """Finding → risk_plan_result risk_item (holding·needs_check 는 싣지 않는다)."""
    return {
        "rule_id": f.rule_id,
        "rule_version": f.rule_version,
        "severity": str(f.severity),
        "position_ids": list(f.world_ids),
        "zone_id": f.zone_id,
        "metrics": {k: round(v, METRIC_DECIMALS) for k, v in f.metrics.items()},
        "reason_codes": list(f.reasons),
        "confidence_reasons": [str(r) for r in f.confidence_reasons],
    }


def evaluate_plan(payload: Mapping[str, Any], registry: RuleRegistry | None = None) -> PlanOutcome:
    """스키마를 통과한 risk_plan_request payload 를 PLANNED 로 판정한다."""
    t_ms = int(payload["evaluation_time"])
    rules = build_rules(payload["rules"]["rule_versions"], registry)
    zones, surfaces, requested = read_layout(payload["layout"], t_ms)
    positions = requested or generate_positions(surfaces, (z for z in zones if z.is_active(t_ms)))
    world = planned_world(positions, surfaces, t_ms)
    evaluation = RiskEngine(rules, FixedClock(t_ms)).evaluate(
        world, zones, Origin.PLANNED, work_surfaces=surfaces, calibration_state=None  # 계획 위치: 보정 상태 해당 없음
    )
    if evaluation.alert_candidates():  # 엔진이 막지만 결과가 알림으로 새지 않는다는 것을 여기서도 확인한다(규칙 5)
        raise NonLiveAlertError("PLANNED 판정에서 알림 후보가 나왔다")
    items = tuple(finding_item(f) for f in evaluation.findings if not f.holding)
    return PlanOutcome(positions, items, evaluation)


def _zone_ms(obj: Any) -> Any:
    """스키마의 정수 시각은 1.0 같은 정수 값 실수도 받는다. 엔진 형식(정수 ms)으로 맞춘다."""
    if not isinstance(obj, Mapping):
        return obj
    fixed = dict(obj)
    for key in ("valid_from", "valid_to"):
        value = fixed.get(key)
        if isinstance(value, float) and value.is_integer():
            fixed[key] = int(value)
    return fixed


def _dups(values: Sequence[Any]) -> list[Any]:
    return sorted({v for v in values if values.count(v) > 1})


def _round_xy(xy: Sequence[float]) -> tuple[float, float]:
    # + 0.0: -0.0 을 0.0 으로(결과 JSON 에 -0.0 이 나가지 않게)
    return (round(xy[0], COORD_DECIMALS) + 0.0, round(xy[1], COORD_DECIMALS) + 0.0)


def _surface_edge_point(s: WorkSurface) -> tuple[float, float] | None:
    hx, hy = s.size_xy[0] / 2.0, s.size_xy[1] / 2.0
    if min(hx, hy) - OCCUPANCY_RADIUS_M <= R2_RAISE_DISTANCE_M:
        return None  # 가운데 위치가 이미 R2 발생 거리 안
    # 가장 긴 변(가운데에서 가장 가까운 변)의 가운데에서 안쪽으로. 정사각형은 footprint 첫 변(작업면 축 -v 쪽)
    u, v = (0.0, -hy + SURFACE_EDGE_INSET_M) if hx >= hy else (hx - SURFACE_EDGE_INSET_M, 0.0)
    c, sn = math.cos(s.yaw_rad), math.sin(s.yaw_rad)
    return (s.center_xy[0] + u * c - v * sn, s.center_xy[1] + u * sn + v * c)


def _opening_point(polygon: Sequence[Sequence[float]]) -> tuple[float, float] | None:
    n = len(polygon)
    area2 = sum(polygon[i][0] * polygon[(i + 1) % n][1] - polygon[(i + 1) % n][0] * polygon[i][1] for i in range(n))
    if area2 == 0:
        return None  # 넓이 없는 다각형은 바깥 방향을 모른다
    edges = []
    for i in range(n):
        (ax, ay), (bx, by) = polygon[i][:2], polygon[(i + 1) % n][:2]
        length = math.hypot(bx - ax, by - ay)
        if length > 0:
            edges.append((-length, i, ax, ay, bx, by))
    for neg_len, _, ax, ay, bx, by in sorted(edges):
        length = -neg_len
        dx, dy = (bx - ax) / length, (by - ay) / length
        # 반시계(넓이 > 0)면 진행 방향 오른쪽이 바깥, 시계 방향이면 왼쪽
        nx, ny = (dy, -dx) if area2 > 0 else (-dy, dx)
        point = _round_xy(((ax + bx) / 2.0 + nx * OPENING_OFFSET_M, (ay + by) / 2.0 + ny * OPENING_OFFSET_M))
        if not point_in_polygon(point, polygon):
            return point
    return None
