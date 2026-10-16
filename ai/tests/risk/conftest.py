"""Risk Engine 시험 공용 도구.

구체 규칙(R1~R7)이 아직 없으므로 엔진·실행기를 확인할 시험용 규칙(PROBE)을 둔다. 배포 등록부에는 넣지 않는다.
"""

import copy

import pytest

from simai_ai.risk import Finding, RuleContext, RuleRegistry, RuleSpec, Severity, WorldState

T0 = 1760342400100

_TRACK = {
    "world_id": "w-0012",
    "t_last_valid": T0 - 20,
    "observation_state": "OBSERVED",
    "track_lifecycle": "CONFIRMED",
    "identity_confidence": "HIGH",
    "identity_revision": 2,
    "position_xy": [3.40, 5.01],
    "position_xy_sigma": [0.08, 0.08],
    "pelvis_xyz": [3.40, 5.01, 0.92],
    "ground_anchor_xyz": [3.41, 5.02, 0.0],
    "support_surface_id": "floor",
    "support_surface_validity": "VALID",
    "contact_validity": "VALID",
    "z_validity": "VALID",
    "head_z": 1.68,
    "cameras": ["cam1", "cam3"],
}

_ZONE = {
    "zone_id": "z-04",
    "zone_version": "zone-1",
    "judge_type": "GROUND_FOOTPRINT",
    "polygon_xy": [[3.0, 4.5], [4.0, 4.5], [4.0, 5.5], [3.0, 5.5]],
    "z_range": None,
    "severity": "HIGH",
}


class ProbeRule:
    """사람 × 유효 구역마다 판정 하나를 낸다. 구역이 없으면 사람마다 zone_id 없는 판정 하나.

    needs_check 는 접지점을 모를 때 True, metrics 에 staleness 를 싣는다(시간 모델·주입 시계 확인용).
    """

    spec = RuleSpec(rule_id="PROBE", rule_version=1, reads=("tracks", "zones"))

    def evaluate(self, ctx: RuleContext):
        zones = [z.zone_id for z in ctx.zones] or [None]
        for track in ctx.world.tracks:
            for zone_id in zones:
                yield Finding(
                    rule_id="PROBE",
                    rule_version=1,
                    severity=Severity.HIGH,
                    world_ids=(track.world_id,),
                    zone_id=zone_id,
                    needs_check=track.ground_anchor_xyz is None,
                    reasons=("probe",),
                    metrics={"staleness_ms": ctx.staleness_ms(track)},
                )


class ZoneProbeRule:
    """사람과 관계없이 유효 구역마다 구역 대상 판정 하나를 낸다(world_ids 없음).

    R7(구역 감시 능력 저하)처럼 사람이 없어도 나는 시스템 판정의 형식만 흉내 낸다. 판정 로직은 없다(R7 은 -73).
    """

    spec = RuleSpec(rule_id="ZONE_PROBE", rule_version=1, reads=("zones",))

    def evaluate(self, ctx: RuleContext):
        for zone in ctx.zones:
            yield Finding(
                rule_id="ZONE_PROBE",
                rule_version=1,
                severity=Severity.HIGH,
                world_ids=(),
                zone_id=zone.zone_id,
                reasons=("coverage_lost",),
                metrics={"observing_cameras": 1},
            )


@pytest.fixture
def track_json():
    """world_track JSON 한 개. 인자로 필드를 덮어쓴다."""

    def make(**overrides):
        return copy.deepcopy(_TRACK) | overrides

    return make


@pytest.fixture
def zone_json():
    def make(**overrides):
        return copy.deepcopy(_ZONE) | overrides

    return make


@pytest.fixture
def world_payload(track_json):
    def make(tracks=None, t_latest_global=T0):
        return {
            "t_latest_global": t_latest_global,
            "calibration_version": "cal-7",
            "scene_version": "scene-3",
            "tracks": [track_json()] if tracks is None else tracks,
        }

    return make


@pytest.fixture
def world(world_payload):
    def make(**kwargs):
        return WorldState.from_payload(world_payload(**kwargs))

    return make


@pytest.fixture
def probe_registry():
    registry = RuleRegistry()
    registry.register("PROBE", 1, ProbeRule)
    registry.register("ZONE_PROBE", 1, ZoneProbeRule)
    return registry


@pytest.fixture
def probe_rule():
    return ProbeRule()


@pytest.fixture
def zone_probe_rule():
    return ZoneProbeRule()
