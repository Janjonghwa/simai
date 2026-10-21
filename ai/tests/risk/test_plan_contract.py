"""EC2 내부 PLANNED 판정 계약(risk_plan_request·risk_plan_result 1.0.0)과 Risk Engine 코드의 일치 시험 (S15P31A101-216).

계약이 코드 형식(Zone.from_json, WorkSurface, Finding, 등록부)과 어긋나거나, 결과 예시가 실제 엔진 출력이 아니게 되면 실패한다.
계획 위치 → WorldTrack 변환(_planned_world)은 Risk Engine 컨테이너(-99)가 할 일을 시험에서만 흉내 낸 것이다.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math

import pytest

from simai_ai.providers.contract import contracts_dir, schema_errors
from simai_ai.providers.testing import example_path, load_example
from simai_ai.risk import WorldState, WorldTrack
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.clock import FixedClock
from simai_ai.risk.common import Origin, Severity
from simai_ai.risk.engine import RiskEngine
from simai_ai.risk.registry import default_registry
from simai_ai.risk.rule import Finding
from simai_ai.risk.work_surface import FLOOR_ID, WorkSurface
from simai_ai.risk.world import (
    IdentityConfidence,
    ObservationState,
    SupportSurfaceValidity,
    TrackLifecycle,
    Validity,
)
from simai_ai.risk.zone import Zone, ZoneJudgeType

pytestmark = pytest.mark.skipif(
    not example_path("risk_plan_request.json").is_file(), reason="저장소 밖(배포 이미지)에서는 contracts/ 예시가 없다"
)

# Finding 필드 → risk_item 필드. holding 은 싣지 않고(발생 판정만), needs_check 는 confidence_reasons 에서 나온다(7.1).
FINDING_TO_ITEM = {
    "rule_id": "rule_id",
    "rule_version": "rule_version",
    "severity": "severity",
    "world_ids": "position_ids",
    "zone_id": "zone_id",
    "reasons": "reason_codes",
    "metrics": "metrics",
    "confidence_reasons": "confidence_reasons",
}
FINDING_NOT_IN_ITEM = {"holding", "needs_check"}


def _schema(name: str) -> dict:
    return json.loads((contracts_dir() / name).read_text(encoding="utf-8"))


def _request_defs() -> dict:
    return _schema("messages/risk_plan_request.schema.json")["$defs"]


def _planned_world(layout: dict, t_ms: int) -> tuple[WorldState, tuple[WorkSurface, ...], tuple[Zone, ...]]:
    surfaces = tuple(WorkSurface(**s) for s in layout["work_surfaces"])
    heights = {FLOOR_ID: 0.0} | {s.surface_id: s.top_height_m for s in surfaces}
    tracks = []
    for p in layout.get("work_positions", ()):
        x, y = p["position_xy"]
        z = heights[p["support_surface_id"]]
        head = p.get("head_height_m")
        tracks.append(
            WorldTrack(
                world_id=p["position_id"],
                t_last_valid=t_ms,
                observation_state=ObservationState.OBSERVED,
                track_lifecycle=TrackLifecycle.CONFIRMED,
                identity_confidence=IdentityConfidence.HIGH,
                identity_revision=0,
                position_xy=(x, y),
                position_xy_sigma=(0.0, 0.0),
                pelvis_xyz=None,
                ground_anchor_xyz=(x, y, z),
                support_surface_id=p["support_surface_id"],
                support_surface_validity=SupportSurfaceValidity.VALID,
                contact_validity=Validity.VALID,
                z_validity=Validity.VALID,
                # 머리 높이를 모르면 지어내지 않는다(head_z None, VOLUME_3D 는 Z_UNKNOWN)
                head_z=None if head is None else z + head,
                head_z_validity=None if head is None else Validity.VALID,
                cameras=("plan-a", "plan-b"),
            )
        )
    world = WorldState(t_latest_global=t_ms, calibration_version="plan", scene_version="plan", tracks=tuple(tracks))
    zones = tuple(Zone.from_json(z, f"zones[{i}]") for i, z in enumerate(layout["zones"]))
    return world, surfaces, zones


def _item(f: Finding) -> dict:
    return {
        "rule_id": f.rule_id,
        "rule_version": f.rule_version,
        "severity": str(f.severity),
        "position_ids": list(f.world_ids),
        "zone_id": f.zone_id,
        "metrics": {k: round(v, 3) for k, v in f.metrics.items()},
        "reason_codes": list(f.reasons),
        "confidence_reasons": [str(r) for r in f.confidence_reasons],
    }


def _evaluate(payload: dict, extra_rules: tuple[tuple[str, int], ...] = ()):
    registry = default_registry()
    keys = [(r["rule_id"], r["rule_version"]) for r in payload["rules"]["rule_versions"]] + list(extra_rules)
    rules = [registry.create(rid, ver) for rid, ver in keys]
    world, surfaces, zones = _planned_world(payload["layout"], payload["evaluation_time"])
    engine = RiskEngine(rules, FixedClock(payload["evaluation_time"]))
    return engine.evaluate(world, zones, Origin(payload["source"]), work_surfaces=surfaces)


def test_examples_are_valid():
    for name, schema in (
        ("risk_plan_request.json", "risk_plan_request"),
        ("risk_plan_result.json", "risk_plan_result"),
        ("risk_plan_result.unknown_rule.json", "risk_plan_result"),
        ("risk_plan_result.unreadable_request.json", "risk_plan_result"),
    ):
        assert schema_errors(load_example(name), schema) == [], name


def test_zone_and_work_surface_shapes_match_code():
    defs = _request_defs()
    assert set(defs["zone"]["properties"]) == {f.name for f in dataclasses.fields(Zone)}
    assert set(defs["zone"]["properties"]["judge_type"]["enum"]) == {j.value for j in ZoneJudgeType}
    assert set(defs["work_surface"]["properties"]) == {f.name for f in dataclasses.fields(WorkSurface)}
    layout = load_example("risk_plan_request.json")["payload"]["layout"]
    for i, z in enumerate(layout["zones"]):
        Zone.from_json(z, f"zones[{i}]")  # 계약 형식 그대로 코드가 읽는다(모르는 키를 거부하는 코드)
    for s in layout["work_surfaces"]:
        WorkSurface(**s)


def test_severity_and_origin_match_code():
    defs = _schema("common/defs.schema.json")["$defs"]
    assert set(defs["severity"]["enum"]) == {s.value for s in Severity}
    assert set(defs["risk_origin"]["enum"]) == {o.value for o in Origin}
    assert not Origin.PLANNED.alertable


def test_finding_fields_all_mapped():
    """Finding 에 필드가 생기면 계약에 실을지(또는 왜 안 싣는지) 정해야 한다."""
    fields = {f.name for f in dataclasses.fields(Finding)}
    assert fields == set(FINDING_TO_ITEM) | FINDING_NOT_IN_ITEM
    item_props = load_example("risk_plan_result.json")["payload"]["items"][0].keys()
    assert set(FINDING_TO_ITEM.values()) == set(item_props)


def test_example_rules_are_registered():
    registry = default_registry()
    payload = load_example("risk_plan_request.json")["payload"]
    for r in payload["rules"]["rule_versions"]:
        registry.create(r["rule_id"], r["rule_version"])


@pytest.mark.parametrize(
    ("request_name", "result_name"),
    [
        ("risk_plan_request.json", "risk_plan_result.json"),
        ("risk_plan_request.generated_positions.json", "risk_plan_result.generated_positions.json"),
    ],
)
def test_result_example_is_engine_output(request_name, result_name):
    """결과 예시의 items 는 결과 positions 로 실제 엔진을 돌린 출력(holding 제외, metrics mm 반올림)과 같다."""
    request = load_example(request_name)["payload"]
    result = load_example(result_name)
    positions = result["payload"]["positions"]
    layout = dict(request["layout"], work_positions=[_position_input(p) for p in positions])
    evaluation = _evaluate(dict(request, layout=layout))
    items = [_item(f) for f in evaluation.findings if not f.holding]
    assert items == result["payload"]["items"]
    assert evaluation.alert_candidates() == ()  # PLANNED 는 알림 후보가 없다(규칙 5)
    built = copy.deepcopy(result)
    built["payload"]["items"] = items
    assert schema_errors(built, "risk_plan_result") == []


def _position_input(p: dict) -> dict:
    return {k: v for k, v in p.items() if k != "position_source"}


def test_requested_positions_echoed_as_requested():
    """요청이 위치를 주면 결과 positions 는 그 위치 그대로이고 모두 REQUESTED 다(만들지 않는다)."""
    requested = load_example("risk_plan_request.json")["payload"]["layout"]["work_positions"]
    positions = load_example("risk_plan_result.json")["payload"]["positions"]
    assert [_position_input(p) for p in positions] == requested
    assert {p["position_source"] for p in positions} == {"REQUESTED"}


def test_missing_positions_means_generated():
    """위치가 없는 요청은 스키마를 통과하고(컨테이너가 만든다), 그 결과의 위치는 모두 GENERATED 다."""
    request = load_example("risk_plan_request.generated_positions.json")
    assert "work_positions" not in request["payload"]["layout"]
    request["payload"]["layout"]["work_positions"] = []
    assert schema_errors(request, "risk_plan_request") == []  # 빈 배열도 '만든다'
    positions = load_example("risk_plan_result.generated_positions.json")["payload"]["positions"]
    assert positions and {p["position_source"] for p in positions} == {"GENERATED"}


@pytest.mark.parametrize("name", ["risk_plan_result.json", "risk_plan_result.generated_positions.json"])
def test_item_positions_are_listed(name):
    """스키마 밖 규칙: items 의 position_ids 는 payload.positions 의 ID 이고 위치 ID 는 겹치지 않는다."""
    payload = load_example(name)["payload"]
    ids = [p["position_id"] for p in payload["positions"]]
    assert len(ids) == len(set(ids))
    assert {i for item in payload["items"] for i in item["position_ids"]} <= set(ids)


@pytest.mark.parametrize(
    ("source", "pid", "ok"),
    [("GENERATED", "gen-1", True), ("GENERATED", "p-1", False), ("REQUESTED", "gen-1", False), ("REQUESTED", "p-1", True)],
)
def test_generated_id_namespace(source, pid, ok):
    result = load_example("risk_plan_result.json")
    result["payload"]["positions"][0] |= {"position_id": pid, "position_source": source}
    assert (schema_errors(result, "risk_plan_result") == []) is ok


# ---------------------------------------------------------------- 오류 판정 순서 (계약 risk_plan_result 설명)

HTTP_STATUS = {
    "INVALID_REQUEST": 400,
    "SOURCE_NOT_PLANNED": 400,
    "INVALID_LAYOUT": 400,
    "UNKNOWN_RULE": 422,
    "ENGINE_ERROR": 500,
}
UUID_REF = {"$ref": "https://simai.dev/contracts/common/defs.schema.json#/$defs/uuid"}


def _classify(body: str) -> tuple[str, str | None]:
    """계약의 오류 판정 순서를 옮긴 참고 분류기(컨테이너 -99 가 같은 순서를 쓴다). (code 또는 OK, request_id)."""
    try:
        doc = json.loads(body)
        payload = doc["payload"]
        request_id = payload["request_id"]
    except (ValueError, KeyError, TypeError):
        return "INVALID_REQUEST", None
    if schema_errors(request_id, UUID_REF):
        return "INVALID_REQUEST", None
    if "source" in payload and payload["source"] != "PLANNED":
        return "SOURCE_NOT_PLANNED", request_id
    if schema_errors(doc, "risk_plan_request"):
        return "INVALID_REQUEST", request_id
    keys = [(r["rule_id"], r["rule_version"]) for r in payload["rules"]["rule_versions"]]
    if len({rid for rid, _ in keys}) != len(keys):
        return "UNKNOWN_RULE", request_id
    registry = default_registry()
    try:
        rules = [registry.create(rid, ver) for rid, ver in keys]
    except LookupError:
        return "UNKNOWN_RULE", request_id
    layout = payload["layout"]
    try:
        zones = [Zone.from_json(z) for z in layout["zones"]]
        surfaces = [WorkSurface(**s) for s in layout["work_surfaces"]]
    except RiskInputError:
        return "INVALID_LAYOUT", request_id
    positions = layout.get("work_positions", [])
    surface_ids = [s.surface_id for s in surfaces]
    active = [z.zone_id for z in zones if z.is_active(payload["evaluation_time"])]
    if (
        len(set(surface_ids)) != len(surface_ids)
        or len({p["position_id"] for p in positions}) != len(positions)
        or len({(z.zone_id, z.zone_version) for z in zones}) != len(zones)
        or len(set(active)) != len(active)
        or any(p["support_surface_id"] not in {FLOOR_ID, *surface_ids} for p in positions)
    ):
        return "INVALID_LAYOUT", request_id
    try:
        world, _, _ = _planned_world(layout, payload["evaluation_time"])
        RiskEngine(rules, FixedClock(payload["evaluation_time"])).evaluate(
            world, zones, Origin.PLANNED, work_surfaces=surfaces
        )
    except Exception:  # noqa: BLE001 - 입력 검사를 통과한 뒤의 예외는 모두 ENGINE_ERROR
        return "ENGINE_ERROR", request_id
    return "OK", request_id


def _mut(fn) -> str:
    doc = load_example("risk_plan_request.json")
    fn(doc["payload"])
    return json.dumps(doc)


def _rules(*keys):
    return lambda p: p["rules"].__setitem__("rule_versions", [{"rule_id": r, "rule_version": v} for r, v in keys])


def _zone(i, **kw):
    return lambda p: p["layout"]["zones"][i].update(kw)


def _append_copy(key, **kw):
    return lambda p: p["layout"][key].append(dict(p["layout"][key][0], **kw))


def _cases():
    yield "ok", _mut(lambda p: None), "OK"
    yield "not_json", "{", "INVALID_REQUEST"
    yield "no_request_id", _mut(lambda p: p.pop("request_id")), "INVALID_REQUEST"
    yield "bad_request_id", _mut(lambda p: p.__setitem__("request_id", "x")), "INVALID_REQUEST"
    # 출처는 다른 스키마 위반·규칙 오류보다 먼저 본다
    yield "live", _mut(lambda p: p.__setitem__("source", "LIVE")), "SOURCE_NOT_PLANNED"
    yield (
        "live_and_no_rules_version",
        _mut(lambda p: (p.__setitem__("source", "LIVE"), p["rules"].pop("rules_version"))),
        "SOURCE_NOT_PLANNED",
    )
    yield (
        "replay_and_unknown_rule",
        _mut(lambda p: (p.__setitem__("source", "REPLAY"), _rules(("R9", 1))(p))),
        "SOURCE_NOT_PLANNED",
    )
    yield "no_source", _mut(lambda p: p.pop("source")), "INVALID_REQUEST"
    # 같은 (ID, 버전) 두 번은 스키마 위반(400), 같은 ID 다른 버전은 규칙 묶음 오류(422)
    yield "exact_duplicate_rule", _mut(_rules(("R1", 1), ("R1", 1))), "INVALID_REQUEST"
    yield "same_id_other_version", _mut(_rules(("R2", 1), ("R2", 1001))), "UNKNOWN_RULE"
    yield "unregistered_rule", _mut(_rules(("R9", 1))), "UNKNOWN_RULE"
    # 규칙 묶음이 레이아웃보다 먼저
    yield (
        "unknown_rule_and_bad_zone",
        _mut(lambda p: (_rules(("R9", 1))(p), _zone(1, z_range=[4.0, 0.0])(p))),
        "UNKNOWN_RULE",
    )
    yield "reversed_z_range", _mut(_zone(1, z_range=[4.0, 0.0])), "INVALID_LAYOUT"
    yield "reversed_validity", _mut(_zone(1, valid_from=1760367600000, valid_to=1760338800000)), "INVALID_LAYOUT"
    yield "duplicate_surface", _mut(_append_copy("work_surfaces")), "INVALID_LAYOUT"
    yield "duplicate_position", _mut(_append_copy("work_positions")), "INVALID_LAYOUT"
    yield (
        "unknown_support",
        _mut(lambda p: p["layout"]["work_positions"][0].__setitem__("support_surface_id", "desk-9")),
        "INVALID_LAYOUT",
    )
    yield "two_active_versions", _mut(_append_copy("zones", zone_version="lv-4")), "INVALID_LAYOUT"
    # 위치가 없으면 오류가 아니다(컨테이너가 만든다)
    yield "no_positions", _mut(lambda p: p["layout"].pop("work_positions")), "OK"


@pytest.mark.parametrize("case", [c[0] for c in _cases()])
def test_error_precedence(case):
    body, expected = next((b, e) for name, b, e in _cases() if name == case)
    code, request_id = _classify(body)
    assert code == expected
    if code == "OK":
        return
    result = {
        "schema_version": "1.0.0",
        "msg_type": "risk_plan_result",
        "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000499",
        "sent_at": 1,
        "source_id": "risk-engine-01",
        "payload": {
            "request_id": request_id,
            "source": "PLANNED",
            "status": "ERROR",
            "computed_at": 1,
            "engine_version": "0.1.0",
            "error": {"code": code, "message": case, "retryable": code == "ENGINE_ERROR"},
        },
    }
    assert schema_errors(result, "risk_plan_result") == []  # 분류 결과가 계약에 맞는 오류 문서가 된다


def test_engine_error_is_only_retryable_code():
    result = load_example("risk_plan_result.unknown_rule.json")
    for code in HTTP_STATUS:
        for retryable in (True, False):
            doc = copy.deepcopy(result)
            doc["payload"]["error"] |= {"code": code, "retryable": retryable}
            ok = retryable is (code == "ENGINE_ERROR")
            assert (schema_errors(doc, "risk_plan_result") == []) is ok, (code, retryable)


# ---------------------------------------------------------------- 반례


@pytest.mark.parametrize("origin", [o.value for o in Origin if o is not Origin.PLANNED])
def test_non_planned_origin_rejected_both_ways(origin):
    request = load_example("risk_plan_request.json")
    request["payload"]["source"] = origin
    assert schema_errors(request, "risk_plan_request")
    result = load_example("risk_plan_result.json")
    result["payload"]["source"] = origin
    assert schema_errors(result, "risk_plan_result")


@pytest.mark.parametrize(
    ("yaw", "ok"),
    [(math.pi, True), (-math.pi, True), (0.0, True), (3.15, False), (-3.15, False), (90, False), (180.0, False)],
)
def test_yaw_must_be_radians(yaw, ok):
    request = load_example("risk_plan_request.json")
    request["payload"]["layout"]["work_surfaces"][0]["yaw_rad"] = yaw
    assert (schema_errors(request, "risk_plan_request") == []) is ok


def test_unknown_head_height_is_check_needed_not_clear():
    """머리 높이를 모르는 계획 위치는 VOLUME_3D 구역에서 '진입 없음'이 아니라 확인 필요(Z_UNKNOWN) 항목이 된다."""
    request = load_example("risk_plan_request.json")["payload"]
    items = [_item(f) for f in _evaluate(request).findings if not f.holding]
    p03 = [i for i in items if i["position_ids"] == ["p-03"]]
    assert p03 and p03[0]["confidence_reasons"] == ["Z_UNKNOWN"]


def test_holding_findings_are_not_items():
    """유지(holding) 판정은 활성 이벤트 유지용이다. 한 번 계산하는 PLANNED 결과에 넣으면 스키마가 거부한다."""
    request = load_example("risk_plan_request.json")["payload"]
    evaluation = _evaluate(request, extra_rules=(("R4", 1),))  # 머리 높이 모르는 p-03 → R4 유지 판정
    held = [f for f in evaluation.findings if f.holding]
    assert held
    result = load_example("risk_plan_result.json")
    result["payload"]["items"].append(_item(held[0]) | {"holding": True})
    assert schema_errors(result, "risk_plan_result")


def test_reversed_z_range_is_code_side_rule():
    """z_range 최저 < 최고 는 스키마로 표현하지 못한다(README 스키마 밖 규칙). 코드가 거부하고 결과는 INVALID_LAYOUT 이다."""
    request = load_example("risk_plan_request.json")
    request["payload"]["layout"]["zones"][1]["z_range"] = [4.0, 0.0]
    assert schema_errors(request, "risk_plan_request") == []
    with pytest.raises(RiskInputError):
        Zone.from_json(request["payload"]["layout"]["zones"][1])


def test_probability_names_rejected_in_metrics():
    result = load_example("risk_plan_result.json")
    for name in ("fall_probability", "risk_percent", "fall_risk_pct", "likelihood_m", "risk_score"):
        doc = copy.deepcopy(result)
        doc["payload"]["items"][0]["metrics"][name] = 0.85
        assert schema_errors(doc, "risk_plan_result"), name
