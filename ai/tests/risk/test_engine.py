"""엔진 골격: 출처 불변 규칙(LIVE 만 알림), 주입 시계, 구역 버전, 규칙 공통 형식."""

import pytest

from simai_ai.risk import (
    AlertCandidate,
    Finding,
    FindingTarget,
    FixedClock,
    NonLiveAlertError,
    Origin,
    RiskEngine,
    RuleContractError,
    RuleSpec,
    Severity,
    Zone,
)
from simai_ai.risk._parse import RiskInputError

T0 = 1760342400100  # conftest 의 world() 기본 t_latest_global (트랙 t_last_valid 는 T0 - 20)
NON_LIVE = [Origin.PLANNED, Origin.REPLAY, Origin.WHAT_IF]


def test_only_live_is_alertable():
    assert [o for o in Origin if o.alertable] == [Origin.LIVE]


def test_live_evaluation_yields_alert_candidates(probe_rule, world):
    evaluation = RiskEngine([probe_rule], FixedClock(T0 + 100)).evaluate(world(), [], Origin.LIVE)
    assert evaluation.alertable
    alerts = evaluation.alert_candidates()
    assert [a.finding for a in alerts] == list(evaluation.findings)
    assert alerts[0].origin is Origin.LIVE
    assert (alerts[0].calibration_version, alerts[0].scene_version) == ("cal-7", "scene-3")


@pytest.mark.parametrize("origin", NON_LIVE)
def test_non_live_evaluation_has_no_alert_candidates(probe_rule, world, origin):
    evaluation = RiskEngine([probe_rule], FixedClock(T0 + 100)).evaluate(world(), [], origin)
    assert len(evaluation.findings) == 1  # 판정은 같게 계산한다(보고서·시뮬레이션 화면용)
    assert not evaluation.alertable
    assert evaluation.alert_candidates() == ()


@pytest.mark.parametrize("origin", NON_LIVE)
def test_alert_candidate_cannot_be_built_from_non_live(origin):
    finding = Finding(rule_id="PROBE", rule_version=1, severity=Severity.HIGH, world_ids=("w-1",))
    with pytest.raises(NonLiveAlertError):
        AlertCandidate(
            origin=origin,
            finding=finding,
            observation_time=T0,
            evaluated_at=T0,
            calibration_version="cal-7",
            scene_version="scene-3",
        )


def test_same_input_gives_same_findings_for_every_origin(probe_rule, world, zone_json):
    zones = [Zone.from_json(zone_json())]
    results = {
        origin: RiskEngine([probe_rule], FixedClock(T0 + 100)).evaluate(world(), zones, origin).findings
        for origin in Origin
    }
    assert len(set(results.values())) == 1


def test_evaluated_at_comes_from_injected_clock(probe_rule, world):
    clock = FixedClock(T0 + 100)
    engine = RiskEngine([probe_rule], clock)
    first = engine.evaluate(world(), [], Origin.LIVE)
    clock.advance(400)
    second = engine.evaluate(world(), [], Origin.LIVE)

    assert (first.evaluated_at, second.evaluated_at) == (T0 + 100, T0 + 500)
    assert first.observation_time == second.observation_time == T0
    # 영상이 멈춰도 시계는 진행하므로 staleness 가 커진다(명세 6.1). 트랙 gap 20ms + lag.
    assert first.findings[0].metrics["staleness_ms"] == 120
    assert second.findings[0].metrics["staleness_ms"] == 520


def test_only_zones_active_at_evaluation_time_are_used(probe_rule, world, zone_json):
    zones = [
        Zone.from_json(zone_json(zone_version="zone-1", valid_to=T0 + 1000)),
        Zone.from_json(zone_json(zone_version="zone-2", valid_from=T0 + 1000)),
        Zone.from_json(zone_json(zone_id="z-99", valid_from=T0 + 5000)),
    ]
    clock = FixedClock(T0 + 999)
    engine = RiskEngine([probe_rule], clock)
    before = engine.evaluate(world(), zones, Origin.REPLAY)
    clock.set(T0 + 1000)
    after = engine.evaluate(world(), zones, Origin.REPLAY)

    assert [(z.zone_id, z.zone_version) for z in before.zones] == [("z-04", "zone-1")]
    assert [(z.zone_id, z.zone_version) for z in after.zones] == [("z-04", "zone-2")]
    assert [f.zone_id for f in after.findings] == ["z-04"]


def test_overlapping_zone_versions_are_rejected(probe_rule, world, zone_json):
    zones = [Zone.from_json(zone_json(zone_version="zone-1")), Zone.from_json(zone_json(zone_version="zone-2"))]
    with pytest.raises(RuleContractError, match="구역 버전"):
        RiskEngine([probe_rule], FixedClock(T0)).evaluate(world(), zones, Origin.LIVE)


def test_findings_are_sorted_deterministically(probe_rule, world, track_json, zone_json):
    tracks = [track_json(world_id="w-b"), track_json(world_id="w-a")]
    zones = [Zone.from_json(zone_json(zone_id="z-2")), Zone.from_json(zone_json(zone_id="z-1"))]
    findings = RiskEngine([probe_rule], FixedClock(T0)).evaluate(world(tracks=tracks), zones, Origin.LIVE).findings
    assert [(f.zone_id, f.world_ids) for f in findings] == [
        ("z-1", ("w-a",)),
        ("z-1", ("w-b",)),
        ("z-2", ("w-a",)),
        ("z-2", ("w-b",)),
    ]


def test_needs_check_when_ground_anchor_unknown(probe_rule, world, track_json):
    track = track_json(ground_anchor_xyz=None, support_surface_validity="UNKNOWN")
    finding = RiskEngine([probe_rule], FixedClock(T0)).evaluate(world(tracks=[track]), [], Origin.LIVE).findings[0]
    assert finding.needs_check is True
    assert finding.severity is Severity.HIGH  # 모른다고 등급을 내리지 않는다(명세 7.3)


def test_zone_target_finding_without_people(zone_probe_rule, world, zone_json):
    # 사람이 없는 구역에서도 구역·시스템 판정(R7 형태)을 낼 수 있다. 가짜 World ID 를 만들지 않는다.
    zones = [Zone.from_json(zone_json())]
    evaluation = RiskEngine([zone_probe_rule], FixedClock(T0)).evaluate(world(tracks=[]), zones, Origin.LIVE)
    (finding,) = evaluation.findings
    assert finding.world_ids == ()
    assert finding.zone_id == "z-04"
    assert finding.target is FindingTarget.ZONE
    assert [a.finding for a in evaluation.alert_candidates()] == [finding]


def test_finding_target():
    person = Finding(rule_id="R1", rule_version=1, severity=Severity.HIGH, world_ids=("w-1",), zone_id="z-1")
    zone = Finding(rule_id="R7", rule_version=1, severity=Severity.HIGH, world_ids=(), zone_id="z-1")
    assert (person.target, zone.target) == (FindingTarget.PERSON, FindingTarget.ZONE)


class _ForgingRule:
    spec = RuleSpec(rule_id="FORGE", rule_version=1, reads=())

    def evaluate(self, ctx):
        yield Finding(rule_id="R1", rule_version=1, severity=Severity.INFO, world_ids=("w-1",))


def test_rule_cannot_emit_another_rules_finding(world):
    with pytest.raises(RuleContractError, match="FORGE"):
        RiskEngine([_ForgingRule()], FixedClock(T0)).evaluate(world(), [], Origin.LIVE)


def test_duplicate_rule_id_is_rejected(probe_rule):
    with pytest.raises(RuleContractError, match="PROBE"):
        RiskEngine([probe_rule, probe_rule], FixedClock(T0))


def test_engine_without_rules_returns_nothing(world):
    evaluation = RiskEngine([], FixedClock(T0)).evaluate(world(), [], Origin.LIVE)
    assert evaluation.findings == ()
    assert evaluation.rules == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"world_ids": (), "zone_id": None}, id="no-target"),
        pytest.param({"metrics": {"d": float("nan")}}, id="nan-metric"),
        pytest.param({"metrics": {"d": True}}, id="bool-metric"),
    ],
)
def test_finding_rejects_bad_values(kwargs):
    base = {"rule_id": "PROBE", "rule_version": 1, "severity": Severity.HIGH, "world_ids": ("w-1",)}
    with pytest.raises(RiskInputError):
        Finding(**(base | kwargs))


@pytest.mark.parametrize("version", [0, -1, True, "1"])
def test_rule_spec_version_must_be_positive_int(version):
    with pytest.raises(RiskInputError):
        RuleSpec(rule_id="R1", rule_version=version, reads=())
