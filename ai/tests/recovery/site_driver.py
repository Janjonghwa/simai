"""재시작 복구 시험의 Tier 0 흉내(시험 전용). conftest 와 강제 종료 자식 프로세스(crash_child.py)가 함께 쓴다.

실제 LifecycleTracker(LIVE)·EventJournal·CheckpointStore·Checkpointer·Outbox 를 같은 디렉터리에 조립한다. 판정은 R1 형태의
Finding 을 손으로 넣는다. 시작할 때 늘 recover 를 부른다(첫 시작이면 EMPTY).
"""

import random
import uuid
from dataclasses import replace
from pathlib import Path

from simai_ai.eventlog import ACKED_NAME, AckedOffsetStore, EventJournal, JournalConfig, Outbox
from simai_ai.eventlog.record import thaw
from simai_ai.recovery import DEFAULT_MAX_TOMBSTONES, Checkpointer, CheckpointStore, RecoveryResult, recover
from simai_ai.risk import (
    Evaluation,
    EventIdFactory,
    Finding,
    FixedClock,
    LifecycleTracker,
    Origin,
    Severity,
    WorldState,
)
from simai_ai.risk.alerting import AckActor
from simai_ai.risk.lifecycle import LEGACY_IMMEDIATE_POLICY, EventState, Lifecycle, LifecyclePolicy, Verification

GATEWAY = "tier0-01"
T0 = 1760342400100
WORLD_STATE_MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
POLICY = replace(LEGACY_IMMEDIATE_POLICY, clear_hold_ms=200)
RESUME_POLICY = replace(POLICY, resume_window_ms=10_000)


def track(world_id: str, t_obs: int) -> dict:
    return {
        "world_id": world_id,
        "t_last_valid": t_obs,
        "observation_state": "OBSERVED",
        "track_lifecycle": "CONFIRMED",
        "identity_confidence": "HIGH",
        "identity_revision": 0,
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


def finding(world_id: str, severity: Severity = Severity.HIGH, metric: float = 0.5) -> Finding:
    return Finding(
        rule_id="R1",
        rule_version=1,
        severity=severity,
        world_ids=(world_id,),
        zone_id="z-04",
        reasons=("xy_inside_polygon",),
        metrics={"inside_m": metric},
    )


class Site:
    """Tier 0 하나. root/journal, root/checkpoints 를 쓴다."""

    def __init__(
        self,
        root: Path,
        clock: FixedClock,
        *,
        policy: LifecyclePolicy = POLICY,
        journal_config: JournalConfig | None = None,
        seed: int = 0,
        store_fsync=None,
        journal_fsync=None,
        interval_s: float = 1.0,
        max_tombstones: int = DEFAULT_MAX_TOMBSTONES,
    ) -> None:
        self.root = Path(root)
        self.clock = clock
        jkw = {} if journal_fsync is None else {"fsync": journal_fsync}
        self.journal = EventJournal.open(self.root / "journal", clock=clock, config=journal_config, **jkw)
        kwargs = {} if store_fsync is None else {"fsync": store_fsync}
        self.store = CheckpointStore(self.root / "checkpoints", gateway_id=GATEWAY, **kwargs)
        rng = random.Random(seed)
        self.tracker = LifecycleTracker(
            origin=Origin.LIVE,
            gateway_id=GATEWAY,
            clock=clock,
            policy=policy,
            id_factory=EventIdFactory(clock, randbits=rng.getrandbits),
        )
        self.sent: list[dict] = []
        self.outbox = Outbox(
            self.journal,
            AckedOffsetStore(self.root / "journal" / ACKED_NAME, gateway_id=GATEWAY),
            gateway_id=GATEWAY,
            transport=self.sent.extend,
            clock=clock,
        )
        self.result: RecoveryResult = recover(
            journal=self.journal, store=self.store, tracker=self.tracker, max_tombstones=max_tombstones
        )
        self.checkpointer = Checkpointer(
            self.journal,
            self.store,
            self.result.projection,
            clock=clock,
            compact=self.outbox.compact,
            interval_s=interval_s,
        )
        self.present: set[str] = set()
        self.last_capture: int | None = None

    # --- 판정 --------------------------------------------------------------------------

    def step(self, findings=(), present=None, journal=True):
        """판정 한 번(관측 시각 = 지금 - 100ms). present: 관측되는 World ID(기본은 지금까지 넣은 사람들).

        journal=False 면 저널에 쓰지 않고 단계만 돌려준다(쓰기 스레드에 넘기는 시험용)."""
        self.clock.advance(100)
        t_obs = self.clock.now_ms() - 100
        findings = tuple(findings)
        if present is None:
            self.present |= {w for f in findings for w in f.world_ids}
            present = self.present
        world = WorldState.from_payload(
            {
                "t_latest_global": t_obs,
                "calibration_version": "cal-7",
                "scene_version": "scene-3",
                "tracks": [track(w, t_obs) for w in sorted(present)],
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
            findings=findings,
        )
        step = self.tracker.apply(evaluation, world, config_version="cfg-12", evidence_ref=WORLD_STATE_MSG_ID)
        if journal:
            self.journal.append_step(step)
        self.last_capture = t_obs
        self.checkpointer.update_inputs(config_version="cfg-12", last_capture_time=t_obs)
        return step

    def raise_event(self, world_id: str, severity: Severity = Severity.HIGH) -> EventState:
        step = self.step([finding(world_id, severity)])
        (t,) = [t for t in step.transitions if world_id in t.state.world_ids]
        return t.state

    def hold(self, findings, n: int = 1) -> None:
        for _ in range(n):
            self.step(findings)

    def ack(self, event_id: str, actor=AckActor.ADMIN, channel: str = "CONTROL_ROOM") -> EventState:
        actor_id = "laptop-01" if actor is AckActor.SITE_BUTTON else "admin-1"
        step = self.tracker.acknowledge(event_id, actor, actor_id, self.clock.now_ms(), channel=channel)
        assert step is not None
        self.journal.append_step(step, ack_id=str(uuid.uuid4()))
        return step.transitions[0].state

    def resolve(self, world_id: str, others=(), limit: int = 20) -> None:
        """world_id 의 판정을 멈추고 관측을 이어 해소시킨다. others: 계속 판정을 내는 사람."""
        for _ in range(limit):
            step = self.step([finding(w) for w in others])
            if any(t.transition is Lifecycle.RESOLVED and world_id in t.state.world_ids for t in step.transitions):
                return
        raise AssertionError(f"{world_id} 가 해소되지 않았다")

    def checkpoint(self):
        return self.checkpointer.run_once()

    def close(self) -> None:
        self.checkpointer.stop()
        self.journal.close()


def journal_truth(root: Path, clock: FixedClock) -> dict[str, dict]:
    """정답: 저널 전체를 payload 만으로 접은 활성 이벤트(event_id → 마지막 기록의 payload·meta.state).

    복구 코드(meta.state 투영)와 독립적으로 payload 의 transition 으로 활성 여부를 정한다.
    """
    last: dict[str, dict] = {}
    with EventJournal.open(Path(root) / "journal", clock=clock, readonly=True) as j:
        first = j.first_offset
        for e in j.iter_from(first if first is not None else j.next_offset):
            p = e.payload_dict()
            last[p["event_id"]] = {"payload": p, "state": thaw(e.meta["state"])}
    return {eid: v for eid, v in last.items() if v["payload"]["transition"] != "RESOLVED"}


def assert_matches_journal(result: RecoveryResult, truth: dict[str, dict]) -> None:
    """복원한 활성 이벤트 = 저널 정답 + 복원 전이(UNVERIFIABLE, sequence +1). 다른 필드는 그대로."""
    got = {s.event_id: s for s in result.restored}
    assert set(got) == set(truth), (sorted(got), sorted(truth))
    for eid, t in truth.items():
        s = got[eid]
        p = t["payload"]
        assert s.event_sequence == p["event_sequence"] + 1
        assert (str(s.severity), s.acknowledged) == (p["severity"], p["acknowledged"])
        assert s.verification is Verification.UNVERIFIABLE and s.lifecycle is Lifecycle.UPDATED
        expected = replace(
            EventState.from_record(t["state"]),
            verification=Verification.UNVERIFIABLE,
            event_sequence=p["event_sequence"] + 1,
            lifecycle=Lifecycle.UPDATED,
        )
        assert s == expected
    # 복원 전이는 이벤트마다 하나, 현장 경보 신호(소리 시작·끄기)를 내지 않는다
    assert sorted(t.event_id for t in result.restore_step.transitions) == sorted(truth)
    assert result.restore_step.local_signals == ()
