"""판정 루프 한 tick 의 배선과 순서 불변식 (S15P31A101-171, docs/design/tier0-service.md).

인지 단계는 대역(FakePerception: world_state payload 와 ID 교정 기록을 그대로 낸다)이고 나머지는 실제 모듈이다:
ConfigSync(실제 검증기·구성 묶음), LifecycleTracker, EventJournal·JournalWriter, R1. 반례와 음성 대조:
- 같은 tick 의 ID 교정 → 판정 순서: 지키면 이벤트 하나가 새 ID 로 이어지고, 뒤집으면(음성 대조) 같은 사람에게 이벤트가 둘이 된다.
- 구성 묶음 전환 tick 경계: tick 도중 새 묶음이 들어와도 그 tick 의 인지(작업면)·판정·world_state 는 한 묶음이고 다음 tick 에
  전환한다. begin_tick 을 인지 뒤로 옮기면(음성 대조) 작업면과 판정 묶음이 섞인다.
- 판정 전이의 evidence_ref = 그 tick world_state 봉투 msg_id.
- 확인 멱등(ack_id), 저널 쓰기 실패 중에도 현장 경보는 나간다.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402

from services.tier0.judge import JudgmentLoop  # noqa: E402
from simai_ai.config import BundleVerifier, ConfigSync  # noqa: E402
from simai_ai.eventlog import EventJournal, JournalWriter  # noqa: E402
from simai_ai.geometry.rig import CameraRig  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.providers.files import FileCalibrationProvider  # noqa: E402
from simai_ai.risk import (  # noqa: E402
    AckActor,
    ContractAck,
    FixedClock,
    Lifecycle,
    LifecyclePolicy,
    LifecycleTracker,
    Origin,
    Persistence,
    default_registry,
)
from simai_ai.tracking.time_model import CameraClock  # noqa: E402
from simai_ai.tracking.world_track import IdentityRevision, RevisionKind, WorldTrackFrame  # noqa: E402

CAL_FILE = AI_DIR.parent / "contracts" / "examples" / "calibration.json"
CALIBRATION = json.loads(CAL_FILE.read_text(encoding="utf-8"))
T0 = 1_760_342_400_000
INSIDE = (4.6, 3.8)
OUTSIDE = (1.0, 1.0)
POLICY = LifecyclePolicy(persistence=Persistence(1, 1), persistence_by_rule={}, clear_hold_ms=300)


def track(world_id: str, xy, t: int, revision: int = 0) -> dict:
    return {
        "world_id": world_id, "t_last_valid": t, "observation_state": "OBSERVED", "track_lifecycle": "CONFIRMED",
        "identity_confidence": "HIGH", "identity_revision": revision, "position_xy": list(xy),
        "position_xy_sigma": [0.05, 0.05], "pelvis_xyz": None, "ground_anchor_xyz": [xy[0], xy[1], 0.0],
        "support_surface_id": "floor", "support_surface_validity": "VALID", "contact_validity": "VALID",
        "z_validity": "VALID", "surface_source": "observed", "head_z": 1.7, "head_z_sigma": 0.03, "cameras": ["cam1"],
    }


class _Feed:
    camera_ids = ("cam1",)

    def camera_connected(self, camera_id: str) -> bool:
        return True


class _Manager:
    def export_tracks(self):
        return ()


@dataclass
class FakePerception:
    """인지 대역. plan 의 (tracks, revisions) 를 tick 마다 하나씩 낸다. on_step 은 인지 도중에 부르는 함수(경합 흉내)."""

    clock: FixedClock
    rig: CameraRig
    plan: list[tuple[dict, tuple]] = field(default_factory=list)
    on_step: Any = None
    surfaces_seen: list[Any] = field(default_factory=list)
    feed: _Feed = field(default_factory=_Feed)
    manager: _Manager = field(default_factory=_Manager)
    _t: int | None = None
    lagging: tuple[str, ...] = ()
    excluded_cameras: tuple[str, ...] = ()

    def camera_clocks(self):
        return (CameraClock("cam1", self._t, True),)

    def t_latest_global(self):
        return self._t

    def step(self, now_ms: int, surfaces, sync_invalid=()):
        from services.tier0.perception import Perceived

        self.surfaces_seen.append(surfaces)
        if self.on_step is not None:
            self.on_step()
        if not self.plan:
            return None
        people, revisions = self.plan.pop(0)
        t = now_ms - 40
        self._t = t
        payload = {"t_latest_global": t, "calibration_version": CALIBRATION["calibration_version"],
                   "scene_version": "scene-3",
                   "tracks": [track(w, xy, t, rev) for w, (xy, rev) in people.items()]}
        frame = WorldTrackFrame(t=t, calibration_version=CALIBRATION["calibration_version"], tracks=(),
                                revisions=tuple(revisions))
        return Perceived(t=t, frame=frame, payload=payload, t_latest_global=t)


def _bundle(version: str, issued_at: int, *, rules=(("R1", 1),), ppe_policy=None):
    message, fetcher = ts.bundle_message(CALIBRATION, version, issued_at, rules=rules, ppe_policy=ppe_policy)
    geometry = json.loads(fetcher.blobs[message["payload"]["components"]["operational_geometry"]["uri"]])
    assert geometry["version"] == f"geom-{version}"
    return message, fetcher


class Site:
    def __init__(self, tmp: Path, *, start_writer: bool = True, loop_cls=JudgmentLoop, policy=POLICY,
                 verification=None, fsync=None, calibration_state=None, rules=(("R1", 1),), ppe_policy=None,
                 ppe_snapshot=None) -> None:
        import threading

        self.clock = FixedClock(T0)
        self.rig = CameraRig.from_provider(FileCalibrationProvider(CAL_FILE))
        message, self.fetcher = _bundle("cfg-1", T0 - 10, rules=rules, ppe_policy=ppe_policy)
        verifier = BundleVerifier(self.fetcher, default_registry(), lambda v: v == CALIBRATION["calibration_version"],
                                  self.clock)
        self.config_sync = ConfigSync(verifier, self.clock, source_id=ts.GATEWAY, restored=verifier.verify(message),
                                      restored_applied_at=T0)
        self.journal = EventJournal.open(tmp / "journal", clock=self.clock,
                                         **({} if fsync is None else {"fsync": fsync}))
        self.writer = JournalWriter(self.journal)
        if start_writer:
            self.writer.start()
        self.tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id=ts.GATEWAY, clock=self.clock, policy=policy,
                                        verification=verification)
        self.perception = FakePerception(self.clock, self.rig)
        self.i0: list[tuple[str, dict]] = []
        self.offered: list[tuple[dict, str]] = []
        self.loop = loop_cls(
            lock=threading.RLock(), now_ms=self.clock.now_ms, config_sync=self.config_sync, perception=self.perception,
            tracker=self.tracker, journal=self.journal, writer=self.writer,
            calibration_state=calibration_state or (lambda: str(self.rig.state)),
            send_i0=lambda kind, payload: self.i0.append((kind, payload)) or True,
            offer_world_state=lambda payload, msg_id: self.offered.append((payload, msg_id)),
            **({} if ppe_snapshot is None else {"ppe_snapshot": ppe_snapshot}),
        )

    def tick(self, people: dict, revisions=()):
        self.clock.advance(100)
        self.perception.plan.append(({w: (xy, rev) for w, (xy, rev) in people.items()}, revisions))
        return self.loop.tick()

    def flush(self):
        self.writer.stop(5)
        return self.journal.read(0)

    def close(self):
        self.writer.stop(5)
        self.journal.close()


@pytest.fixture
def site(tmp_path):
    s = Site(tmp_path)
    yield s
    s.close()


def merge(previous: str, survivor: str, revision: int = 1) -> IdentityRevision:
    return IdentityRevision(t=T0, kind=RevisionKind.MERGE, world_id=survivor, previous_world_id=previous,
                            identity_revision=revision, reason="LATE_REASSOCIATION")


class ApplyBeforeCorrect(JudgmentLoop):
    """음성 대조: 같은 tick 에서 판정을 먼저 반영하고 교정을 뒤에 넘긴다(잘못된 순서)."""

    def _judge(self, perceived, active):
        revisions = perceived.frame.revisions
        frame = WorldTrackFrame(perceived.frame.t, perceived.frame.calibration_version, (), ())
        from services.tier0.perception import Perceived

        result = super()._judge(Perceived(perceived.t, frame, perceived.payload, perceived.t_latest_global), active)
        for revision in revisions:
            self._record(self.tracker.correct_identity(revision.to_correction()))
        return result


def _r1_events(tracker):
    return [s for s in tracker.active() if s.rule_id == "R1"]


def test_identity_correction_is_applied_before_judgment_in_the_same_tick(site) -> None:
    site.tick({"w-x": (INSIDE, 0)})
    [event] = _r1_events(site.tracker)
    assert event.world_ids == ("w-x",)
    # 같은 tick: X 가 Y 로 합쳐졌고 Y 가 구역 안에서 관측된다
    result = site.tick({"w-y": (INSIDE, 1)}, revisions=(merge("w-x", "w-y"),))
    [after] = _r1_events(site.tracker)
    assert after.event_id == event.event_id and after.world_ids == ("w-y",) and after.origin_world_ids == ("w-x",)
    transitions = [t for s in result.steps for t in s.transitions]
    assert [str(c) for c in transitions[0].changes] == ["IDENTITY_CORRECTED"]  # 교정이 먼저
    assert all(t.transition is not Lifecycle.RAISED for t in transitions)  # 새 ID 로 새 이벤트를 만들지 않는다
    entries = site.flush()
    assert [e.payload["event_id"] for e in entries] == [event.event_id] * len(entries)


def test_negative_control_reversed_order_duplicates_event(tmp_path) -> None:
    """음성 대조: 순서를 뒤집으면 위 시험의 '이벤트 하나' 가 깨진다(시험이 순서를 실제로 잡는다)."""
    s = Site(tmp_path, loop_cls=ApplyBeforeCorrect)
    try:
        s.tick({"w-x": (INSIDE, 0)})
        result = s.tick({"w-y": (INSIDE, 1)}, revisions=(merge("w-x", "w-y"),))
        raised = [t for st in result.steps for t in st.transitions if t.transition is Lifecycle.RAISED]
        assert len(raised) == 1  # 새 ID 로 RAISED(중복 경보)가 먼저 생긴다
        # -657: 다음 교정에서 흡수 마감이 유령 카드를 지워도, 이미 나간 중복 RAISED 는 되돌릴 수 없다(순서가 실제로 중요하다)
    finally:
        s.close()


def split(source: str, new: str, revision: int = 1) -> IdentityRevision:
    return IdentityRevision(t=T0, kind=RevisionKind.SPLIT, world_id=new, previous_world_id=source,
                            identity_revision=revision, reason="SPLIT_OBSERVED", since=T0 - 300)


class IgnoreSplit(JudgmentLoop):
    """음성 대조: 분할 기록을 Risk 에 넘기지 않는다(이전 동작)."""

    def _judge(self, perceived, active):
        from services.tier0.perception import Perceived

        frame = perceived.frame
        kept = tuple(r for r in frame.revisions if r.kind is not RevisionKind.SPLIT)
        frame = WorldTrackFrame(frame.t, frame.calibration_version, frame.tracks, kept)
        return super()._judge(Perceived(perceived.t, frame, perceived.payload, perceived.t_latest_global), active)


def _split_ticks(s):
    s.tick({"w-x": (INSIDE, 0)})
    [event] = _r1_events(s.tracker)
    # 같은 tick: w-n 이 w-x 에서 갈라져 나왔다. w-x 는 구역 밖, w-n 은 구역 안
    first = s.tick({"w-x": (OUTSIDE, 1), "w-n": (INSIDE, 1)}, revisions=(split("w-x", "w-n"),))
    later = [s.tick({"w-x": (OUTSIDE, 1), "w-n": (INSIDE, 1)}) for _ in range(10)]  # 1 s (해소 유지 0.3 s)
    return event, first, later


def test_identity_split_keeps_pre_split_event_until_both_sides_clear(site) -> None:
    """S15P31A101-201: 분할은 이벤트를 옮기거나 해소하지 않는다. 분할 전 이벤트는 w-x 에 남아 IDENTITY_REVISED 로 표시되고,
    갈라져 나간 w-n 이 구역 안인 동안 w-x 의 관측만으로 해소되지 않는다. w-n 의 같은 위험은 새 이벤트다."""
    event, first, later = _split_ticks(site)
    transitions = [t for s in first.steps for t in s.transitions]
    revised = [t for t in transitions if t.event_id == event.event_id]
    assert [str(c) for t in revised for c in t.changes] == ["IDENTITY_REVISED"]
    assert [t.state.world_ids for t in transitions if t.transition is Lifecycle.RAISED] == [("w-n",)]
    assert site.loop.stats.corrections == 1
    assert all(t.transition is not Lifecycle.RESOLVED for r in later for s in r.steps for t in s.transitions)
    assert site.tracker.get(event.event_id).world_ids == ("w-x",)
    for _ in range(5):  # w-n 도 구역을 벗어나면 둘 다 해소된다
        site.tick({"w-x": (OUTSIDE, 1), "w-n": (OUTSIDE, 1)})
    assert site.tracker.get(event.event_id) is None


def test_negative_control_ignored_split_resolves_pre_split_event(tmp_path) -> None:
    """음성 대조: 분할을 넘기지 않으면 w-n 이 구역 안인데 w-x 의 관측만으로 분할 전 이벤트가 해소된다."""
    s = Site(tmp_path, loop_cls=IgnoreSplit)
    try:
        event, _, later = _split_ticks(s)
        assert s.tracker.get(event.event_id) is None
        assert any(t.transition is Lifecycle.RESOLVED and t.event_id == event.event_id
                   for r in later for st in r.steps for t in st.transitions)
    finally:
        s.close()


def test_config_bundle_switch_only_at_tick_boundary(site) -> None:
    new, fetcher = _bundle("cfg-2", T0 + 5)
    site.fetcher.blobs.update(fetcher.blobs)
    # 인지 도중(수신 스레드가 끼어든 것처럼) 새 묶음이 검증·대기에 들어간다
    site.perception.on_step = lambda: (site.config_sync.receive(new), setattr(site.perception, "on_step", None))
    first = site.tick({"w-x": (INSIDE, 0)})
    assert first.config_version == "cfg-1"
    assert site.perception.surfaces_seen[-1].geometry_version == "geom-cfg-1"
    assert site.offered[-1][0]["config_version"] == "cfg-1"
    assert {t.state.config_version for s in first.steps for t in s.transitions} == {"cfg-1"}
    second = site.tick({"w-x": (INSIDE, 0)})
    assert second.config_version == "cfg-2" and site.perception.surfaces_seen[-1].geometry_version == "geom-cfg-2"
    assert site.offered[-1][0]["config_version"] == "cfg-2"
    acks = [a["payload"] for a in site.config_sync.take_acks()]
    assert any(a["result"] == "APPLIED" and a["config_version"] == "cfg-2" for a in acks)


class SwitchAfterPerception(JudgmentLoop):
    """음성 대조: 묶음 전환(begin_tick)을 인지 뒤에 한다(잘못된 경계)."""

    def tick(self):
        with self.lock:
            from simai_ai.contact import SurfaceSet

            surfaces = SurfaceSet.from_active_config(self.config_sync.active)
            perceived = self.perception.step(self._now_ms(), surfaces)
            active = self.config_sync.begin_tick()
            return self._judge(perceived, active)


def test_negative_control_switch_after_perception_mixes_bundles(tmp_path) -> None:
    s = Site(tmp_path, loop_cls=SwitchAfterPerception)
    try:
        new, fetcher = _bundle("cfg-2", T0 + 5)
        s.fetcher.blobs.update(fetcher.blobs)
        s.perception.on_step = lambda: (s.config_sync.receive(new), setattr(s.perception, "on_step", None))
        first = s.tick({"w-x": (INSIDE, 0)})
        assert first.config_version == "cfg-2" and s.perception.surfaces_seen[-1].geometry_version == "geom-cfg-1"  # 섞임
    finally:
        s.close()


def test_evidence_ref_is_the_world_state_msg_id_of_the_same_tick(site) -> None:
    result = site.tick({"w-x": (INSIDE, 0)})
    payload, msg_id = site.offered[-1]
    assert result.evidence_ref == msg_id and uuid.UUID(msg_id)
    judged = [t for s in result.steps for t in s.transitions]
    assert judged and all(t.to_payload()["evidence_ref"] == msg_id for t in judged)
    message = {"schema_version": "1.3.0", "msg_type": "world_state", "msg_id": msg_id, "sent_at": T0,
               "source_id": ts.GATEWAY, "payload": payload}
    assert schema_errors(message, "world_state") == []
    # 교정 전이는 판정이 아니다: evidence_ref 는 비고 저널이 직전 판정 근거를 meta 로 잇는다
    result2 = site.tick({"w-y": (INSIDE, 1)}, revisions=(merge("w-x", "w-y"),))
    entries = site.flush()
    corrected = [e for e in entries if "IDENTITY_CORRECTED" in (e.payload.get("changes") or [])]
    assert corrected and corrected[0].meta.get("evidence_ref") == msg_id
    assert result2.evidence_ref != msg_id


def _ack(event_id: str, ack_id: str, seq: int | None = None) -> ContractAck:
    return ContractAck(event_id=event_id, actor=AckActor.SITE_BUTTON, actor_id="laptop-01", acked_at=T0,
                       target_sequence=seq, ack_id=ack_id, channel="SITE_BUTTON")


ACK1, ACK2 = str(uuid.uuid4()), str(uuid.uuid4())


def test_ack_is_recorded_once_per_ack_id_and_alarm_stop_carries_it(site) -> None:
    site.tick({"w-x": (INSIDE, 0)})
    [event] = _r1_events(site.tracker)
    assert [k for k, _ in site.i0] == ["local_alert"]
    assert site.loop.acknowledge(_ack(event.event_id, ACK1, event.event_sequence)) is not None
    assert site.loop.acknowledge(_ack(event.event_id, ACK1, event.event_sequence)) is None  # 같은 ack_id
    assert site.loop.acknowledge(_ack(str(uuid.uuid4()), ACK2)) is None  # 모르는 이벤트: 기록하지 않음
    stops = [p for k, p in site.i0 if k == "alarm_stop"]
    assert len(stops) == 1 and stops[0]["ack"]["ack_id"] == ACK1 and stops[0]["event_sequence"] == event.event_sequence
    entries = site.flush()
    assert sum(1 for e in entries if e.meta.get("ack_id") == ACK1) == 1
    for kind, payload in site.i0:
        msg = {"schema_version": "2.1.0", "msg_type": kind, "msg_id": str(uuid.uuid4()), "sent_at": T0,
               "source_id": ts.GATEWAY, "payload": payload}
        assert schema_errors(msg, kind) == [], schema_errors(msg, kind)


def test_site_alert_still_sent_when_journal_rejects(tmp_path) -> None:
    """반례: 저널 쓰기 스레드가 없거나 실패해도(JournalError) 현장 경보는 fsync 를 기다리지 않고 나간다."""
    s = Site(tmp_path, start_writer=False)
    try:
        s.tick({"w-x": (INSIDE, 0)})
        assert [k for k, _ in s.i0] == ["local_alert"]
        assert s.loop.stats.journal_errors == 1
    finally:
        s.journal.close()


def test_event_frame_failure_cannot_change_journal_or_site_alert(site) -> None:
    class FailingFrames:
        def on_transitions(self, transitions, tracks):
            # JPEG·HTTP 로 넘어가기 전에 현장 경보를 이미 보냈어야 한다.
            assert [kind for kind, _ in site.i0] == ["local_alert"]
            raise OSError("프레임 경로 실패")

    site.loop._event_frames = FailingFrames()
    result = site.tick({"w-x": (INSIDE, 0)})
    assert [kind for kind, _ in site.i0] == ["local_alert"]
    assert site.loop.stats.errors == 0 and site.loop.stats.journal_errors == 0
    transitions = [t for step in result.steps for t in step.transitions]
    assert transitions and len(site.flush()) == len(transitions)


def test_resolved_alert_sends_local_alert_resolved(site) -> None:
    site.tick({"w-x": (INSIDE, 0)})
    for _ in range(6):
        site.tick({"w-x": (OUTSIDE, 0)})
    kinds = [(k, p["lifecycle"]) for k, p in site.i0 if k == "local_alert"]
    assert kinds == [("local_alert", "RAISED"), ("local_alert", "RESOLVED")]


# --- 리뷰 F1: 스냅샷 콜백과 판정 tick 의 lock 순서 -----------------------------------------------


def _snapshot_for(site: Site):
    from types import SimpleNamespace

    from services.tier0.event_snapshot import EventStateSnapshot

    snap = EventStateSnapshot(site.journal, gateway_id=ts.GATEWAY, clock=site.clock, config_version=lambda: "cfg-1",
                              start_offset=site.journal.next_offset, seed=site.tracker.active(),
                              current_state=site.loop.current_state, risk_observed_at=site.loop.risk_observed_at)
    outbox = SimpleNamespace(cursor=site.journal.next_offset, blocked=None, drained=True)
    snap.bind(outbox)
    site.loop._snapshot = snap
    return snap, outbox


def _drain(site: Site) -> None:
    assert ts.wait_until(lambda: site.writer.backlog() == 0, timeout=5)


def test_snapshot_callback_paused_inside_snapshot_lock_does_not_block_tick(site) -> None:
    """리뷰 부록 A: 스냅샷이 자기 lock 안에서 risk_observed_at 콜백에 머무는 동안 판정 tick 이 끝나야 한다(교착 없음)."""
    import threading

    site.tick({"w-x": (INSIDE, 0)})
    _drain(site)
    snap, outbox = _snapshot_for(site)
    inside, resume = threading.Event(), threading.Event()

    def risk(event_id):
        inside.set()
        resume.wait(5)
        return site.loop.risk_observed_at(event_id)

    snap._risk_observed_at = risk
    t1 = threading.Thread(target=snap.payload, daemon=True)
    t1.start()
    assert inside.wait(2)
    site.clock.advance(100)
    site.perception.plan.append(({"w-x": (INSIDE, 0)}, ()))
    t2 = threading.Thread(target=site.loop.tick, daemon=True)
    t2.start()
    t2.join(3)
    assert not t2.is_alive()  # 스냅샷이 자기 lock 을 쥐고 있어도 판정은 진행한다
    resume.set()
    t1.join(3)
    assert not t1.is_alive()


def test_snapshot_payload_and_ticks_repeat_concurrently(site) -> None:
    """스냅샷 만들기와 판정 tick 을 동시에 반복해도 둘 다 끝까지 간다(정상 경로, 콜백·broken 조회 포함)."""
    import threading

    site.tick({"w-x": (INSIDE, 0)})
    _drain(site)
    snap, outbox = _snapshot_for(site)
    stop = threading.Event()
    made: list[int] = []

    def snapshots():
        while not stop.is_set():
            outbox.cursor = site.journal.next_offset
            if snap.payload() is not None:
                made.append(1)

    t1 = threading.Thread(target=snapshots, daemon=True)
    t1.start()
    for _ in range(200):
        site.tick({"w-x": (INSIDE, 0)})
    stop.set()
    t1.join(3)
    assert not t1.is_alive() and made
    assert site.loop.stats.errors == 0


def test_reseed_waits_for_empty_journal_queue_then_restarts_snapshot(tmp_path) -> None:
    """-81 reseed: broken 이면 저널 쓰기 대기열이 빌 때까지 기다렸다가(새 단계가 남아 있으면 하지 않음) 다시 시작한다."""
    import threading

    gate = threading.Event()
    gate.set()
    s = Site(tmp_path, fsync=lambda fd: gate.wait(10))
    try:
        s.tick({"w-x": (INSIDE, 0)})
        _drain(s)
        snap, _outbox = _snapshot_for(s)
        gate.clear()  # 디스크 정체: 다음 단계가 대기열에 남는다
        [event] = _r1_events(s.tracker)
        s.loop.acknowledge(_ack(event.event_id, str(uuid.uuid4()), event.event_sequence))
        assert s.writer.backlog() >= 1
        with snap._lock:
            snap._broken = "정리로 접지 않은 기록이 지워짐(시험)"
        s.tick({"w-x": (INSIDE, 0)})
        assert s.loop.stats.reseeds == 0 and snap.broken is not None  # 저널이 아직 추적기를 따라오지 않았다
        gate.set()
        _drain(s)
        s.tick({"w-x": (INSIDE, 0)})
        assert s.loop.stats.reseeds == 1 and snap.broken is None
        assert set(snap._rows) == {e.event_id for e in s.tracker.active()}
    finally:
        gate.set()
        s.close()


# --- 리뷰 F2: 입력 정지 중에도 마지막 실제 World 로 판정을 계속한다 ----------------------------------------


def test_full_input_stop_raises_pending_alert_as_unverifiable(tmp_path) -> None:
    """위험을 한 프레임 본 뒤 영상이 멈췄다: 새 관측을 지어내지 않고 같은 World 로 판정을 이어 가 지속 대기 경보를 낸다."""
    s = Site(tmp_path, policy=LifecyclePolicy(persistence=Persistence(2, 3), persistence_by_rule={}))
    try:
        s.tick({"w-x": (INSIDE, 0)})
        assert not s.tracker.active() and list(s.tracker.pending().values()) == [1]
        for _ in range(30):  # 3 s 동안 새 프레임 없음(시계만 간다)
            s.clock.advance(100)
            s.loop.tick()
        [event] = _r1_events(s.tracker)
        assert str(event.verification) == "UNVERIFIABLE"
        assert [k for k, _ in s.i0] == ["local_alert"]
        assert len(s.offered) == 1  # 새 관측이 없으면 world_state 를 다시 보내지 않는다
        assert event.observation_time == s.offered[0][0]["t_latest_global"]  # 원래 촬영 시각 그대로
        assert s.loop.stats.frozen_evaluations >= 1
    finally:
        s.close()


def test_short_stop_does_not_count_same_observation_twice_and_resumes(tmp_path) -> None:
    """입력 정지 중 재개: 짧게 멈춘 동안 같은 관측을 지속 조건의 새 프레임으로 세지 않고, 새 프레임이 오면 정상 발생한다."""
    s = Site(tmp_path, policy=LifecyclePolicy(persistence=Persistence(2, 3), persistence_by_rule={}))
    try:
        s.tick({"w-x": (INSIDE, 0)})
        for _ in range(3):  # 300 ms: lag 기준(600 ms) 안이라 아직 유효 관측. 같은 관측을 다시 세지 않는다
            s.clock.advance(100)
            s.loop.tick()
        assert not s.tracker.active() and list(s.tracker.pending().values()) == [1]
        assert s.loop.stats.frozen_evaluations == 3
        s.tick({"w-x": (INSIDE, 0)})  # 새 프레임(관측 시각이 앞으로 간다)
        [event] = _r1_events(s.tracker)
        assert str(event.verification) == "VERIFIED" and len(s.offered) == 2
    finally:
        s.close()


def test_clock_estimate_expiry_during_input_stop_makes_event_unverifiable(tmp_path) -> None:
    """입력이 멈춘 동안 노트북 시계 추정이 만료(None)되면 활성 이벤트의 해소 확인을 멈춘다(UNVERIFIABLE)."""
    from simai_ai.tracking.decision_time import ClockAwareVerification
    from simai_ai.tracking.time_model import ClockOffset

    offset = [ClockOffset(0.0, 5.0)]
    s = Site(tmp_path, verification=ClockAwareVerification(offset_source=lambda: offset[0]))
    try:
        s.tick({"w-x": (INSIDE, 0)})
        s.tick({"w-x": (INSIDE, 0)})
        [event] = _r1_events(s.tracker)
        assert str(event.verification) == "VERIFIED"
        offset[0] = None  # 교환이 끊겨 추정이 만료됐다
        s.clock.advance(100)
        result = s.loop.tick()  # 새 프레임 없음
        assert str(s.tracker.get(event.event_id).verification) == "UNVERIFIABLE"
        assert any("VERIFICATION" in [str(c) for c in t.changes] for st in result.steps for t in st.transitions)
    finally:
        s.close()


# ---------------------------------------------------------------- 운영자 종료: 저널 제출 실패 (S15P31A101-200 리뷰 F1)


def test_operator_close_not_submitted_to_journal_keeps_event_and_can_be_retried(tmp_path) -> None:
    """리뷰 재현(max_queue=1 로 쓰기 대기열 포화): 제출하지 못한 종료는 REJECTED JOURNAL_UNAVAILABLE 이고 이벤트·다른 이벤트는
    그대로 활성, close_id 도 남지 않는다. 포화가 풀린 뒤 같은 close_id 재전송은 ACCEPTED, 저널의 종료 전이는 정확히 1건,
    한 번 더 보내면 DUPLICATE."""
    import threading

    from simai_ai.risk import Evaluation, Finding, FixedClock, LifecycleStep, LifecycleTracker, Origin, Severity, WorldState
    from simai_ai.risk.lifecycle import CloseRequest

    clock = FixedClock(T0)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id=ts.GATEWAY, clock=clock)
    journal = EventJournal.open(tmp_path / "j", clock=clock)

    def tick(findings=()):
        clock.advance(100)
        t = clock.now_ms() - 10
        world = WorldState(t_latest_global=t, calibration_version="cal-7", scene_version="scene-3", tracks=())
        evaluation = Evaluation(origin=Origin.LIVE, evaluated_at=clock.now_ms(), observation_time=t,
                                calibration_version="cal-7", scene_version="scene-3", rules=(), zones=(),
                                findings=tuple(findings))
        journal.append_step(tracker.apply(evaluation, world, config_version="cfg-1",
                                          evidence_ref="0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"))

    def r7(zone):
        return Finding(rule_id="R7", rule_version=1, severity=Severity.CAUTION, world_ids=(), zone_id=zone)

    tick([r7("z-04"), r7("z-05")])
    tick([r7("z-05")])  # z-04 만 판정이 사라짐(종료 가능), z-05 는 판정 중
    [closable] = [s for s in tracker.active() if s.zone_id == "z-04"]
    before = tracker.active()
    writer = JournalWriter(journal, max_queue=1).start()
    started, release = threading.Event(), threading.Event()
    blocker = writer._submit(lambda: (started.set(), release.wait(10)))  # 쓰기 스레드를 멈춘다
    try:
        assert started.wait(3)
        filler = writer.submit_step(LifecycleStep())  # 대기열 한 칸을 채운다
        loop = JudgmentLoop(lock=threading.RLock(), now_ms=clock.now_ms, config_sync=None, perception=None,
                            tracker=tracker, journal=journal, writer=writer, calibration_state=lambda: "PROVISIONAL")
        request = CloseRequest(event_id=closable.event_id, close_id="7a2b3c4d-5e6f-4a1b-9c2d-3e4f5a6b7c8d",
                               actor_id="user-0003", requested_at=T0, reason="카메라 철거")
        first = loop.close(request)
        assert (first["result"], first.get("reject_reason")) == ("REJECTED", "JOURNAL_UNAVAILABLE")
        assert schema_errors({"schema_version": "1.1.0", "msg_type": "event_close_result", "msg_id": str(uuid.uuid4()),
                              "sent_at": T0, "source_id": ts.GATEWAY, "payload": first}, "event_close_result") == []
        assert tracker.active() == before and loop.active_states() == before  # 아무것도 바꾸지 않았다
        assert not journal.has_close_id(request.close_id) and loop.stats.journal_errors == 1
        release.set()
        blocker.result(3)
        filler.result(3)
        second = loop.close(request)
        assert second["result"] == "ACCEPTED" and second["event_sequence"] == closable.event_sequence + 1
        assert loop.close(request)["result"] == "DUPLICATE"  # 기록 대기 중이어도
        assert ts.wait_until(lambda: writer.backlog() == 0, timeout=3)  # 대기열 한 칸: 넣지 않고 비기를 기다린다
        assert loop.close(request)["result"] == "DUPLICATE"
        closes = [e for e in journal.iter_from(0) if e.payload.get("close")]
        assert len(closes) == 1 and closes[0].payload["event_sequence"] == second["event_sequence"]
        assert [s.zone_id for s in tracker.active()] == ["z-05"]
    finally:
        release.set()
        writer.stop(3)
        journal.close()


# --- 보정 상태 전달 (명세 4.4, D-G3-03, S15P31A101-218) -------------------------------------------------------


def test_judgment_passes_active_calibration_state_every_tick(tmp_path, monkeypatch) -> None:
    """판정마다(입력 정지 tick 포함) 그 시점의 활성 보정 상태를 읽어 엔진에 넘긴다. 시작 때 한 번 읽고 굳히지 않는다
    (운영 중 DRIFTED 표시가 다음 판정부터 R2·R3·R4 확인 필요가 되도록)."""
    from simai_ai.config import ActiveConfig

    seen: list[str | None] = []
    original = ActiveConfig.evaluate

    def spy(self, world, origin, **kwargs):
        seen.append(kwargs.get("calibration_state"))
        return original(self, world, origin, **kwargs)

    monkeypatch.setattr(ActiveConfig, "evaluate", spy)
    state = ["PROVISIONAL"]
    s = Site(tmp_path, calibration_state=lambda: state[0])
    try:
        s.tick({"w-1": (INSIDE, 0)})
        state[0] = "DRIFTED"
        s.tick({"w-1": (INSIDE, 0)})
        state[0] = "CALIBRATED"
        s.clock.advance(100)
        result = s.loop.tick()  # 새 프레임 없음: 마지막 World 로 판정만 다시 한다
        assert result.perceived is None and s.loop.stats.frozen_evaluations == 1
    finally:
        s.close()
    assert seen == ["PROVISIONAL", "DRIFTED", "CALIBRATED"]


def test_default_site_uses_rig_calibration_state(tmp_path, monkeypatch) -> None:
    """Tier 0 와 같은 출처(보정 문서 state)가 그대로 넘어간다. 예시 보정 문서는 PROVISIONAL 이다."""
    from simai_ai.config import ActiveConfig

    seen: list[str | None] = []
    original = ActiveConfig.evaluate
    monkeypatch.setattr(ActiveConfig, "evaluate",
                        lambda self, w, o, **kw: seen.append(kw.get("calibration_state")) or original(self, w, o, **kw))
    s = Site(tmp_path)
    try:
        s.tick({"w-1": (OUTSIDE, 0)})
    finally:
        s.close()
    assert CALIBRATION["state"] == "PROVISIONAL" and seen == ["PROVISIONAL"]


def test_judgment_loop_requires_calibration_state() -> None:
    """보정 상태 입력에 기본값이 없다(빠뜨리면 '해당 없음'으로 조용히 PROVISIONAL_CALIB 가 사라지지 않게)."""
    import inspect

    param = inspect.signature(JudgmentLoop).parameters["calibration_state"]
    assert param.default is inspect.Parameter.empty


class _Boom(RuntimeError):
    pass


def _raise():
    raise _Boom("보정 저장소 읽기 실패")


@pytest.mark.parametrize("getter", [_raise, lambda: None, lambda: "calibrated", lambda: ""],
                         ids=["exception", "none", "typo", "empty"])
def test_unknown_calibration_state_is_judged_as_uncalibrated(tmp_path, monkeypatch, getter) -> None:
    """반례(모르는 보정이 정상·해당 없음으로 처리되는 경로): LIVE 진입점에서 보정 상태 읽기가 예외·None·모르는 값이면 판정을
    빠뜨리지 않고 UNCALIBRATED 로 넘긴다(R2·R3·R4 확인 필요). None(PLANNED 전용 '해당 없음')은 엔진까지 가지 않는다."""
    from simai_ai.config import ActiveConfig

    seen: list[str | None] = []
    original = ActiveConfig.evaluate
    monkeypatch.setattr(ActiveConfig, "evaluate",
                        lambda self, w, o, **kw: seen.append(kw.get("calibration_state")) or original(self, w, o, **kw))
    s = Site(tmp_path, calibration_state=getter)
    try:
        s.tick({"w-1": (INSIDE, 0)})
        s.tick({"w-1": (INSIDE, 0)})
        s.clock.advance(100)
        s.loop.tick()  # 입력 정지 tick 도 같다
    finally:
        s.close()
    assert seen == ["UNCALIBRATED"] * 3
    assert s.loop.stats.calibration_state_unknown == 3 and s.loop.stats.evaluated == 2



# --- R9: PPE 중계 저장소 → 판정 → 저널 (명세 7.2 R9, 5.5, S15P31A101-519) ---------------------------

PPE_WORK = {"format_version": "1.0.0", "version": "ppe-3", "work_id": 7, "effective_from_ms": T0 - 60_000,
            "helmet": "REQUIRED"}
PPE_EXAMPLE = json.loads((AI_DIR.parent / "contracts" / "examples" / "ppe_state.json").read_text(encoding="utf-8"))


def _ppe_message(seq: int, world_id: str, state: str, t: int) -> dict:
    person = dict(PPE_EXAMPLE["payload"]["people"][0])
    helmet = dict(person["helmet"]) | {"state": state, "last_evidence_at_ms": t, "expires_at_ms": t + 10_000,
                                       "evidence": [dict(person["helmet"]["evidence"][0]) | {"candidate_state": state}]}
    person |= {"world_id": world_id, "identity_revision": 0, "capture_time_ms": t, "helmet": helmet}
    payload = dict(PPE_EXAMPLE["payload"]) | {"state_sequence": seq, "people": [person]}
    return dict(PPE_EXAMPLE) | {"payload": payload}


def test_r9_runs_from_relayed_ppe_state_without_site_alert(tmp_path) -> None:
    """중계한 ppe_state 로 R9 가 나고(CAUTION: 현장 경보 없음, 7.3), WORN 이 오면 그 판정에 해소된다."""
    from simai_ai.risk import PpeStateStore

    store = PpeStateStore()
    s = Site(tmp_path, rules=(("R1", 1), ("R9", 1)), ppe_policy=PPE_WORK, ppe_snapshot=store.snapshot)
    try:
        assert store.offer(_ppe_message(1, "w-1", "NOT_WORN", T0))
        s.tick({"w-1": (OUTSIDE, 0)})
        [event] = [e for e in s.tracker.active() if e.rule_id == "R9"]
        assert event.world_ids == ("w-1",) and event.zone_id is None and str(event.severity) == "CAUTION"
        assert store.offer(_ppe_message(2, "w-1", "WORN", T0 + 100))
        s.tick({"w-1": (OUTSIDE, 0)})
        assert [e for e in s.tracker.active() if e.rule_id == "R9"] == []
        entries = [e.payload for e in s.flush()]
    finally:
        s.close()
    assert [(p["rule_id"], p["transition"], p["verification"]) for p in entries] == [
        ("R9", "RAISED", "VERIFIED"), ("R9", "RESOLVED", "VERIFIED")]
    assert [k for k, _ in s.i0 if k == "local_alert"] == []  # CAUTION 은 노트북 로컬 경보로 내지 않는다


def test_r9_off_when_bundle_has_no_r9(tmp_path) -> None:
    """기본 꺼짐(8.10·15.1): 묶음 rules 에 R9 가 없으면 작업 중 NOT_WORN 이어도 이벤트가 없다."""
    from simai_ai.risk import PpeStateStore

    store = PpeStateStore()
    s = Site(tmp_path, ppe_policy=PPE_WORK, ppe_snapshot=store.snapshot)
    try:
        store.offer(_ppe_message(1, "w-1", "NOT_WORN", T0))
        for _ in range(3):
            s.tick({"w-1": (OUTSIDE, 0)})
        assert s.tracker.active() == ()
    finally:
        s.close()


def test_ppe_snapshot_failure_does_not_lose_the_tick(tmp_path) -> None:
    """안전모 상태를 읽지 못하면 그 tick 은 모름(빈 상태)으로 판정하고 tick 을 잃지 않는다(R9 새로 안 냄)."""

    def broken():
        raise RuntimeError("boom")

    s = Site(tmp_path, rules=(("R1", 1), ("R9", 1)), ppe_policy=PPE_WORK, ppe_snapshot=broken)
    try:
        s.tick({"w-1": (INSIDE, 0)})
        assert s.loop.stats.evaluated == 1
        assert [e.rule_id for e in s.tracker.active()] == ["R1"]
    finally:
        s.close()


# --- 작업 시작 = 관제 시작 (S15P31A101-679, 사용자 결정 2026-11-13) ------------------------------------

def _ppe(version: str, work_id: int | None) -> dict:
    """EC2 가 작업 상태로 만드는 ppe_policy(작업이 없으면 work_id null·요구 없음)."""
    return {"format_version": "1.2.0", "version": version, "work_id": work_id, "effective_from_ms": T0 - 60_000,
            "helmet": "REQUIRED" if work_id is not None else "NOT_REQUIRED",
            "vest": "REQUIRED" if work_id is not None else "NOT_REQUIRED"}


def _switch(s: Site, version: str, issued_at: int, work_id: int | None) -> None:
    """작업 시작·종료처럼 새 ppe_policy 를 실은 묶음을 받는다(다음 tick 경계에서 전환)."""
    message, fetcher = _bundle(version, issued_at, ppe_policy=_ppe(f"ppe-{version}", work_id))
    s.fetcher.blobs.update(fetcher.blobs)
    assert s.config_sync.receive(message).accepted


def _messages(s: Site) -> list[dict]:
    from simai_ai.eventlog.outbox import build_transition_message

    return [build_transition_message(e, resent=False, sent_at=T0, source_id=ts.GATEWAY) for e in s.flush()]


def test_before_work_start_no_event_or_site_alert_even_inside_zone_but_world_state_continues(tmp_path) -> None:
    s = Site(tmp_path, ppe_policy=_ppe("ppe-idle", None))
    try:
        for _ in range(5):
            result = s.tick({"w-x": (INSIDE, 0)})
            assert result.steps == ()
        assert s.tracker.active() == () and s.tracker.pending() == {}
        assert [k for k, _ in s.i0] == []  # 현장 경보기·워치 없음
        assert len(s.offered) == 5  # 추적·world_state(화면)는 계속
        assert s.loop.stats.idle_ticks == 5 and s.loop.stats.evaluated == 0
        assert s.flush() == []  # 저널(→ EC2 이벤트·관제 알림) 없음
    finally:
        s.close()


def test_work_start_turns_judgment_on_and_work_end_closes_open_event_and_site_alert(tmp_path) -> None:
    s = Site(tmp_path, ppe_policy=_ppe("ppe-idle", None))
    try:
        s.tick({"w-x": (INSIDE, 0)})
        _switch(s, "cfg-2", T0 + 5, 48)  # 작업 시작
        s.tick({"w-x": (INSIDE, 0)})
        [event] = s.tracker.active()
        assert event.rule_id == "R1" and event.local_alerted
        _switch(s, "cfg-3", T0 + 300, None)  # 작업 종료: 사람은 아직 구역 안
        closing = s.tick({"w-x": (INSIDE, 0)})
        assert s.tracker.active() == ()
        [transition] = [t for step in closing.steps for t in step.transitions]
        assert transition.transition.value == "RESOLVED"
        assert [c.value for c in transition.changes] == ["VERSION", "CLEARED", "WORK_ENDED"]
        assert transition.evidence_ref == closing.evidence_ref == s.offered[-1][1]
        for _ in range(3):
            assert s.tick({"w-x": (INSIDE, 0)}).steps == ()  # 종료 뒤에는 다시 나지 않는다
        alerts = [p["lifecycle"] for k, p in s.i0 if k == "local_alert"]
        assert alerts == ["RAISED", "RESOLVED"]  # 현장 경보기·워치가 꺼진다
        messages = _messages(s)
    finally:
        s.close()
    assert [(m["payload"]["transition"], m["payload"]["verification"]) for m in messages] == [
        ("RAISED", "VERIFIED"), ("RESOLVED", "VERIFIED")]
    for message in messages:  # 관측 해소 규칙(VERIFIED·근거)을 지키는 계약 문서다(resolution 없음)
        assert schema_errors(message, "risk_event_transition") == [], schema_errors(message, "risk_event_transition")
        assert "resolution" not in message["payload"]


def test_unverifiable_event_is_still_closed_at_work_end_and_pending_is_dropped(tmp_path) -> None:
    """관측을 잃어 UNVERIFIABLE 인 이벤트도 작업 종료로 닫히고, 지속 대기 중인 키는 관측 상실로 발생하지 않는다."""
    s = Site(tmp_path, ppe_policy=_ppe("ppe-work", 7),
             policy=LifecyclePolicy(persistence=Persistence(3, 3), persistence_by_rule={}, clear_hold_ms=300))
    try:
        for _ in range(3):
            s.tick({"w-x": (INSIDE, 0)})
        s.tick({"w-y": (INSIDE, 0)})  # w-y 는 지속 조건(3/3)을 세는 중
        [event] = s.tracker.active()
        assert event.world_ids == ("w-x",) and event.verification.value == "UNVERIFIABLE"  # w-x 가 사라졌다
        assert s.tracker.pending()
        _switch(s, "cfg-2", T0 + 900, None)
        s.tick({})  # 아무도 보이지 않는다: 대기 중 관측 상실이어도 발생하지 않는다
        assert s.tracker.active() == () and s.tracker.pending() == {}
        entries = [e.payload for e in s.flush()]
    finally:
        s.close()
    assert [(list(p["world_ids"]), p["transition"], p["verification"]) for p in entries][-1] == (
        ["w-x"], "RESOLVED", "VERIFIED")
    assert not any(list(p["world_ids"]) == ["w-y"] for p in entries)


def test_work_end_during_input_stop_closes_with_last_world(tmp_path) -> None:
    s = Site(tmp_path, ppe_policy=_ppe("ppe-work", 7))
    try:
        s.tick({"w-x": (INSIDE, 0)})
        assert len(s.tracker.active()) == 1
        _switch(s, "cfg-2", T0 + 300, None)
        s.clock.advance(100)
        frozen = s.loop.tick()  # 새 프레임 없음: 마지막 World 로 감시를 내린다
        assert frozen.perceived is None and s.tracker.active() == ()
        [transition] = [t for step in frozen.steps for t in step.transitions]
        assert transition.evidence_ref == s.offered[-1][1]
    finally:
        s.close()


def test_switch_to_another_work_closes_previous_events_and_resets_rule_memory(tmp_path) -> None:
    """종료 묶음을 건너뛰고 작업 A → B 로 바로 바뀌면 같은 tick 에 A 의 이벤트를 닫고 B 로 판정한다."""
    s = Site(tmp_path, ppe_policy=_ppe("ppe-a", 7))
    try:
        s.tick({"w-x": (INSIDE, 0)})
        [first] = s.tracker.active()
        memory = s.config_sync.active.engine.state
        memory.view(Origin.LIVE).slot(("R4", 1), lambda: "이전 작업 이력")
        _switch(s, "cfg-2", T0 + 300, 8)
        result = s.tick({"w-x": (INSIDE, 0)})
        transitions = [t for step in result.steps for t in step.transitions]
        assert [(t.event_id == first.event_id, t.transition.value) for t in transitions] == [
            (True, "RESOLVED"), (False, "RAISED")]
        assert memory.view(Origin.LIVE).slot(("R4", 1), lambda: "새 작업") == "새 작업"
    finally:
        s.close()


def test_restart_outside_work_closes_restored_events_on_first_world(tmp_path) -> None:
    """재시작 뒤 보관 묶음이 작업 밖이면 복원한 활성 이벤트를 첫 실제 World tick 에 닫는다(따로 저장한 상태 없이 묶음으로 복원)."""
    s = Site(tmp_path, ppe_policy=_ppe("ppe-work", 7))
    try:
        s.tick({"w-x": (INSIDE, 0)})
        [event] = s.tracker.active()
    finally:
        s.close()
    restarted = Site(tmp_path / "after", ppe_policy=_ppe("ppe-idle", None))
    try:
        restored = restarted.tracker.restore([event])
        restarted.loop._record(restored)
        restarted.tick({"w-x": (INSIDE, 0)})
        assert restarted.tracker.active() == ()
        kinds = [(p["transition"], list(p["changes"])) for p in (e.payload for e in restarted.flush())]
    finally:
        restarted.close()
    assert kinds[0] == ("UPDATED", ["RESTORED", "VERIFICATION"])
    assert kinds[-1][0] == "RESOLVED" and "WORK_ENDED" in kinds[-1][1]


def test_restart_during_work_keeps_restored_events(tmp_path) -> None:
    """반례: 재시작 뒤 보관 묶음이 같은 작업 중이면 복원 이벤트를 닫지 않는다(처음 판정을 켜는 tick 은 감시 내림이 아니다)."""
    s = Site(tmp_path, ppe_policy=_ppe("ppe-work", 7))
    try:
        s.tick({"w-x": (INSIDE, 0)})
        [event] = s.tracker.active()
    finally:
        s.close()
    restarted = Site(tmp_path / "after", ppe_policy=_ppe("ppe-work", 7))
    try:
        restarted.tracker.restore([event])
        restarted.tick({"w-x": (INSIDE, 0)})
        assert [e.event_id for e in restarted.tracker.active()] == [event.event_id]
        assert restarted.loop.stats.work_end_closes == 0
    finally:
        restarted.close()


def test_bundle_without_ppe_policy_keeps_judging(site) -> None:
    """ppe_policy 가 없는 묶음(-518 이전 EC2·시험)은 작업 정보가 없다는 뜻이라 이전처럼 판정한다(모르는 채로 경보를 끄지 않는다)."""
    site.tick({"w-x": (INSIDE, 0)})
    assert [e.rule_id for e in site.tracker.active()] == ["R1"]
    assert site.loop.stats.idle_ticks == 0
