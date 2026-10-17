"""Tier 0 활성 현장 경보 목록 송신 (명세 3.10, 12.2 I0, FR-137, S15P31A101-161).

(재)접속 직후 즉시, 그 뒤 1Hz 마다 보낸다. 보낸 필드를 health 예시에 합쳐 계약 스키마로 검증한다.
services 는 설치 대상이 아니라(ai/ 작업 디렉터리에서 실행) 시험에서 ai/ 를 import 경로에 넣는다.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
CONTRACTS = AI_DIR.parent / "contracts"
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.tier0.site_alert_sync import SiteAlertResync  # noqa: E402
from simai_ai.risk import (  # noqa: E402
    AckActor,
    Evaluation,
    EventIdFactory,
    Finding,
    FixedClock,
    LifecyclePolicy,
    LifecycleTracker,
    Origin,
    Persistence,
    Severity,
    WorldState,
    ack_from_contract,
    alarm_stop_contract_payload,
)

T0 = 1760342400100
TRACK = {
    "world_id": "w-0012",
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


@pytest.fixture(scope="module")
def schema_errors():
    """errors(스키마 이름, payload): 그 메시지 예시의 봉투에 payload 를 넣어 계약으로 검증한 오류 목록."""
    import jsonschema
    import referencing

    registry = referencing.Registry()
    by_name = {}
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], referencing.Resource.from_contents(schema))
        by_name[path.name.removesuffix(".schema.json")] = schema

    def errors(name: str, payload: dict) -> list[str]:
        example = json.loads((CONTRACTS / "examples" / f"{name}.json").read_text(encoding="utf-8"))
        doc = {**example, "payload": payload}
        validator = jsonschema.Draft202012Validator(by_name[name], registry=registry)
        return [e.message for e in validator.iter_errors(doc)]

    return errors


@pytest.fixture(scope="module")
def health_errors(schema_errors):
    example = json.loads((CONTRACTS / "examples" / "health.json").read_text(encoding="utf-8"))
    return lambda fields: schema_errors("health", {**example["payload"], **fields})


class Tier0:
    """추적기 하나 + 주입 시계. 판정을 넣어 활성 이벤트를 만든다."""

    def __init__(self, origin=Origin.LIVE, clock=None):
        self.clock = clock or FixedClock(T0)
        self.origin = origin
        self.tracker = LifecycleTracker(
            origin=origin,
            gateway_id="tier0-01",
            clock=self.clock,
            policy=LifecyclePolicy(persistence=Persistence(1, 1), persistence_by_rule={}),
            id_factory=EventIdFactory(self.clock, randbits=lambda n: 0),
        )

    def judge(self, severity=Severity.HIGH, world_id="w-0012"):
        t_obs = self.clock.now_ms() - 100
        world = WorldState.from_payload(
            {
                "t_latest_global": t_obs,
                "calibration_version": "cal-7",
                "scene_version": "scene-3",
                "tracks": [TRACK | {"world_id": world_id, "t_last_valid": t_obs}],
            }
        )
        finding = Finding(
            rule_id="R1",
            rule_version=1,
            severity=severity,
            world_ids=(world_id,),
            zone_id="z-04",
            reasons=("xy_inside_polygon",),
            metrics={"dwell_s": 1.4},
        )
        evaluation = Evaluation(
            origin=self.origin,
            evaluated_at=self.clock.now_ms(),
            observation_time=t_obs,
            calibration_version="cal-7",
            scene_version="scene-3",
            rules=(),
            zones=(),
            findings=(finding,),
        )
        return self.tracker.apply(evaluation, world, config_version="cfg-12")


@pytest.fixture
def tier0():
    return Tier0()


def make_sync(tier0: Tier0, sent: list, **kwargs) -> SiteAlertResync:
    return SiteAlertResync(active_states=tier0.tracker.active, send=sent.append, clock=tier0.clock, **kwargs)


def test_connect_sends_immediately_and_matches_contract(tier0, health_errors):
    [t] = tier0.judge(Severity.CRITICAL).transitions
    sent: list = []
    sync = make_sync(tier0, sent)
    assert sync.tick() is None and sent == []  # 접속 전에는 보내지 않는다
    fields = sync.control_connected()
    assert sent == [fields]
    assert fields["active_site_alerts_as_of"] == tier0.clock.now_ms()
    [item] = fields["active_site_alerts"]
    assert (item["event_id"], item["event_sequence"], item["acknowledged"]) == (t.event_id, 1, False)
    assert health_errors(fields) == []


def test_empty_list_is_sent_and_valid(tier0, health_errors):
    sent: list = []
    fields = make_sync(tier0, sent).control_connected()
    assert fields["active_site_alerts"] == [] and health_errors(fields) == []


def test_one_hz_after_connect(tier0):
    sent: list = []
    sync = make_sync(tier0, sent)
    sync.control_connected()
    tier0.clock.advance(999)
    assert sync.tick() is None and len(sent) == 1
    tier0.clock.advance(1)
    assert sync.tick() is not None and len(sent) == 2
    tier0.clock.advance(500)
    assert sync.tick() is None and len(sent) == 2


def test_reconnect_sends_immediately_without_waiting_for_period(tier0):
    sent: list = []
    sync = make_sync(tier0, sent)
    sync.control_connected()
    sync.control_disconnected()
    tier0.clock.advance(5000)
    assert sync.tick() is None and len(sent) == 1  # 끊긴 동안은 보내지 않는다
    tier0.clock.advance(100)
    sync.control_connected()  # 1Hz 주기를 기다리지 않는다
    assert len(sent) == 2


def test_list_built_at_send_time_reflects_latest_state(tier0, health_errors):
    sent: list = []
    sync = make_sync(tier0, sent)
    sync.control_connected()
    [t] = tier0.judge().transitions
    tier0.tracker.acknowledge(t.event_id, AckActor.SITE_BUTTON, "laptop-01", T0, target_sequence=1)
    tier0.clock.advance(1000)
    fields = sync.tick()
    assert fields["active_site_alerts"][0]["acknowledged"] is True and health_errors(fields) == []


def test_non_live_tracker_never_listed(health_errors):
    clock = FixedClock(T0)
    live, what_if, replay = Tier0(clock=clock), Tier0(Origin.WHAT_IF, clock), Tier0(Origin.REPLAY, clock)
    for t in (live, what_if, replay):
        t.judge(Severity.CRITICAL)
    sent: list = []
    sync = SiteAlertResync(
        active_states=lambda: [*live.tracker.active(), *what_if.tracker.active(), *replay.tracker.active()],
        send=sent.append,
        clock=clock,
    )
    fields = sync.control_connected()
    assert [a["source"] for a in fields["active_site_alerts"]] == ["LIVE"] and health_errors(fields) == []


def test_send_failure_retries_next_tick(tier0):
    calls = []

    def flaky(fields):
        calls.append(fields)
        if len(calls) == 1:
            raise ConnectionError("대기열 닫힘")

    sync = SiteAlertResync(active_states=tier0.tracker.active, send=flaky, clock=tier0.clock)
    assert sync.control_connected() is None
    tier0.clock.advance(10)
    assert sync.tick() is not None and len(calls) == 2


def test_build_and_send_hold_injected_lock(tier0):
    lock = threading.Lock()
    seen = []
    sync = SiteAlertResync(
        active_states=lambda: seen.append(lock.locked()) or tier0.tracker.active(),
        send=lambda fields: seen.append(lock.locked()),
        clock=tier0.clock,
        lock=lock,
    )
    sync.control_connected()
    assert seen == [True, True] and not lock.locked()


def test_interval_must_be_positive(tier0):
    with pytest.raises(ValueError):
        make_sync(tier0, [], interval_ms=0)


def test_not_ready_until_tracker_restored(tier0):
    """재시작 직후 추적기 복원 전에는 빈 목록을 보내지 않는다(노트북의 실제 경보를 내리지 않게, 리뷰 R3)."""
    restored = False
    sent: list = []
    sync = make_sync(tier0, sent, ready=lambda: restored)
    assert sync.control_connected() is None and sent == []
    tier0.clock.advance(5000)
    assert sync.tick() is None and sent == []
    tier0.judge()
    restored = True
    fields = sync.tick()  # 준비되면 주기를 기다리지 않고 바로
    assert sent == [fields] and len(fields["active_site_alerts"]) == 1


def test_reentrant_lock_in_send_is_allowed(tier0):
    lock = threading.RLock()

    def send(fields):
        with lock:  # 대기열이 같은 lock 을 다시 잡아도(RLock) 교착되지 않는다
            pass

    sync = SiteAlertResync(active_states=tier0.tracker.active, send=send, clock=tier0.clock, lock=lock)
    assert sync.control_connected() is not None


@pytest.mark.parametrize("actor", list(AckActor))
@pytest.mark.parametrize("target", [1, None])
def test_alarm_stop_contract_payload_matches_schema(tier0, schema_errors, actor, target):
    [t] = tier0.judge().transitions
    tier0.judge(Severity.CRITICAL)
    [stop] = tier0.tracker.acknowledge(t.event_id, actor, "id-1", T0, target_sequence=target).local_signals
    payload = alarm_stop_contract_payload(stop, ack_id="6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f")
    assert schema_errors("alarm_stop", payload) == []
    assert payload["event_sequence"] == (1 if target == 1 else 2) != stop.event_sequence


@pytest.mark.parametrize(("channel", "role"), [("CONTROL_ROOM", "FOREMAN"), ("FOREMAN_PWA", "ADMIN")])
def test_cross_channel_event_ack_is_schema_valid_and_flows(tier0, schema_errors, channel, role):
    """스키마가 허용하는 교차 조합(반장이 관제 화면, 관리자가 PWA)도 받아 같은 채널·역할로 alarm_stop 을 만든다."""
    example = json.loads((CONTRACTS / "examples" / "event_ack.json").read_text(encoding="utf-8"))
    [t] = tier0.judge().transitions
    ack_body = {**example["payload"]["ack"], "channel": channel, "actor_role": role}
    payload = {"event_id": t.event_id, "event_sequence": 1, "ack": ack_body}
    assert schema_errors("event_ack", payload) == []
    ack = ack_from_contract("event_ack", payload)
    [stop] = tier0.tracker.acknowledge(**ack.acknowledge_args()).local_signals
    out = alarm_stop_contract_payload(stop, ack_id=ack.ack_id)
    assert schema_errors("alarm_stop", out) == []
    assert (out["ack"]["channel"], out["ack"]["actor_role"]) == (channel, role)


@pytest.mark.parametrize("name", ["event_ack", "site_ack"])
def test_contract_ack_examples_flow_to_alarm_stop(tier0, schema_errors, name):
    """계약 예시 event_ack·site_ack → acknowledge(target) → 계약 alarm_stop(대상 그대로). 둘 다 스키마에 맞는다."""
    example = json.loads((CONTRACTS / "examples" / f"{name}.json").read_text(encoding="utf-8"))
    [t] = tier0.judge().transitions
    tier0.judge(Severity.CRITICAL)
    payload = {**example["payload"], "event_id": t.event_id, "event_sequence": 1}
    assert schema_errors(name, payload) == []
    ack = ack_from_contract(name, payload)
    [stop] = tier0.tracker.acknowledge(**ack.acknowledge_args()).local_signals
    out = alarm_stop_contract_payload(stop, ack_id=ack.ack_id)
    assert schema_errors("alarm_stop", out) == []
    assert out["event_sequence"] == 1 and out["ack"]["ack_id"] == example["payload"]["ack"]["ack_id"]
    assert out["ack"]["channel"] == example["payload"]["ack"]["channel"]
