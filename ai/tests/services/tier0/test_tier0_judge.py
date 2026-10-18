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


def _bundle(version: str, issued_at: int):
    message, fetcher = ts.bundle_message(CALIBRATION, version, issued_at, rules=(("R1", 1),))
    geometry = json.loads(fetcher.blobs[message["payload"]["components"]["operational_geometry"]["uri"]])
    assert geometry["version"] == f"geom-{version}"
    return message, fetcher


class Site:
    def __init__(self, tmp: Path, *, start_writer: bool = True, loop_cls=JudgmentLoop, policy=POLICY,
                 verification=None, fsync=None) -> None:
        import threading

        self.clock = FixedClock(T0)
        self.rig = CameraRig.from_provider(FileCalibrationProvider(CAL_FILE))
        message, self.fetcher = _bundle("cfg-1", T0 - 10)
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
            send_i0=lambda kind, payload: self.i0.append((kind, payload)) or True,
            offer_world_state=lambda payload, msg_id: self.offered.append((payload, msg_id)),
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
        s.tick({"w-y": (INSIDE, 1)}, revisions=(merge("w-x", "w-y"),))
        events = _r1_events(s.tracker)
        assert len(events) == 2  # 새 ID 로 RAISED 가 먼저 생기고 교정은 연결(IDENTITY_CONFLICT)만 남긴다
        assert {e.related_event_id for e in events} - {None}
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
