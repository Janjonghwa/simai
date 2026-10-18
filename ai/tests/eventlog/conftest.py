"""이벤트 저널·outbox 시험 공용 도구."""

import copy
import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from simai_ai.risk import EventIdFactory, Finding, FixedClock, LifecycleTracker, Origin, Severity, WorldState
from simai_ai.risk.lifecycle import LEGACY_IMMEDIATE_POLICY

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "contracts"
GATEWAY = "tier0-01"
T0 = 1760342400100
WORLD_STATE_MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"

_TRACK = {
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


def example_transition_payload(event_sequence: int = 1, transition: str = "RAISED", **over) -> dict:
    """계약 예시 risk_event_transition 의 payload 에서 저널·전송이 붙이는 필드를 뺀 것."""
    doc = json.loads((CONTRACTS / "examples" / "risk_event_transition.json").read_text(encoding="utf-8"))
    payload = copy.deepcopy(doc["payload"])
    del payload["journal_offset"], payload["resent"]
    payload.update(event_sequence=event_sequence, transition=transition, **over)
    return payload


class CountingFsync:
    """fsync 호출을 센다(실제 fsync 도 한다)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, fd: int) -> None:
        os.fsync(fd)
        self.calls += 1


class LiveEvents:
    """실제 LifecycleTracker(LIVE, 지속 조건 없음)로 전이를 만든다."""

    def __init__(self) -> None:
        self.clock = FixedClock(T0)
        self.tracker = LifecycleTracker(
            origin=Origin.LIVE,
            gateway_id=GATEWAY,
            clock=self.clock,
            policy=LEGACY_IMMEDIATE_POLICY,
            id_factory=EventIdFactory(self.clock, randbits=lambda n: 0),
        )

    def step(self, findings=()):
        from simai_ai.risk import Evaluation

        self.clock.advance(100)
        t_obs = self.clock.now_ms() - 100
        world = WorldState.from_payload(
            {
                "t_latest_global": t_obs,
                "calibration_version": "cal-7",
                "scene_version": "scene-3",
                "tracks": [_TRACK | {"t_last_valid": t_obs}],
            }
        )
        evaluation = Evaluation(
            origin=Origin.LIVE,
            evaluated_at=self.clock.now_ms(),
            observation_time=t_obs,
            calibration_version="cal-7",
            scene_version="scene-3",
            rules=(),
            zones=(),
            findings=tuple(findings),
        )
        return self.tracker.apply(evaluation, world, config_version="cfg-12", evidence_ref=WORLD_STATE_MSG_ID)

    @staticmethod
    def finding(severity=Severity.HIGH):
        return Finding(
            rule_id="R1",
            rule_version=1,
            severity=severity,
            world_ids=("w-0012",),
            zone_id="z-04",
            reasons=("xy_inside_polygon",),
        )

    def raise_event(self):
        return self.step([self.finding()])

    def resolve_steps(self, limit: int = 300):
        """판정 없이 유효 관측을 이어 해소될 때까지 돌린다."""
        steps = []
        for _ in range(limit):
            step = self.step()
            steps.append(step)
            if any(str(t.transition) == "RESOLVED" for t in step.transitions):
                return steps
        raise AssertionError("해소되지 않았다")


@pytest.fixture
def tp():
    """계약 예시 기반 전이 payload 생성 함수."""
    return example_transition_payload


@pytest.fixture
def append():
    """journal.append(TRANSITION_KIND, 예시 payload) 를 줄여 쓰는 함수."""
    from simai_ai.eventlog import TRANSITION_KIND

    def do(journal, seq=1, transition="RAISED", **over):
        return journal.append(TRANSITION_KIND, example_transition_payload(seq, transition, **over))

    return do


@pytest.fixture
def live_events():
    return LiveEvents()


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture(scope="session")
def message_errors():
    """I1 메시지(봉투 포함)의 계약 위반 목록을 돌려주는 함수."""
    registry = Registry()
    for p in sorted(CONTRACTS.rglob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        registry = registry.with_resource(s["$id"], Resource.from_contents(s))

    def errors(doc: dict) -> list[str]:
        schema = json.loads((CONTRACTS / "messages" / f"{doc['msg_type']}.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, registry=registry)
        return [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(doc)]

    return errors
