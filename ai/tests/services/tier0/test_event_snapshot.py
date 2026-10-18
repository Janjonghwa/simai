"""event_state_snapshot 만들기·보내기 (S15P31A101-81, 명세 3.2 재동기화 알림 규칙, 부록 D-G3-05).

스냅샷은 저널 접기로 만든다: journal_offset 은 이 연결에서 outbox 가 보낸 마지막 offset 이고, 활성 목록은 그 offset 까지
보낸 전이의 결과와 같다. 반례: 만드는 도중 새 전이, 재요청으로 커서가 물러남(만들기 전·만드는 중·보낸 뒤, 반복), 재전송
도중 다시 끊김, acked_offset 이 한참 뒤, 활성 0개·빈 저널, 확인·재개 표시, 재개 기간 경계, 해소 대기 중 스냅샷, 재시작
(정리된 RAISED), 정리와 접기 동시, 정리 우회 뒤 회복. 리뷰 재현 R1~R4 는 test_review_r* 로 남긴다.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
for p in (AI_DIR, Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from resync_site import CONFIG, GATEWAY, INSIDE, OUTSIDE, Site  # noqa: E402
from test_i1_units import FAST, FakeConn, FakeConnector, health_payload, wait_until  # noqa: E402

from services.tier0.event_snapshot import EventStateSnapshot  # noqa: E402
from services.tier0.i1_client import Envelope  # noqa: E402
from services.tier0.i1_publisher import build_i1_link  # noqa: E402
from simai_ai.eventlog import AckedOffsetStore, EventJournal, JournalConfig, Outbox, TransportError  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import FixedClock  # noqa: E402

T0 = 1760342400000


class Wire:
    """outbox 전송 함수 흉내. fail 이면 끊김."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail = False

    def __call__(self, messages) -> None:
        if self.fail:
            raise TransportError("끊김")
        self.sent.extend(messages)

    def offsets(self) -> list[int]:
        return [m["payload"]["journal_offset"] for m in self.sent]


class Kit:
    def __init__(self, tmp_path: Path, *, config: JournalConfig | None = None, acked: int | None = None,
                 batch_size: int = 64, t0: int = T0) -> None:
        self.clock = FixedClock(t0)
        self.journal = EventJournal.open(tmp_path / "journal", clock=self.clock, config=config)
        self.site = Site(self.journal, self.clock)
        self.store = AckedOffsetStore(tmp_path / "acked.json", gateway_id=GATEWAY)
        if acked is not None:
            self.store.save(acked)
        self.batch_size = batch_size
        self.wire = Wire()
        self.config = {"version": CONFIG}

    def snapshot(self, *, start_offset: int = 0, seed=(), current=True, outbox=True, risk=False,
                 **kw) -> EventStateSnapshot:
        snap = EventStateSnapshot(
            self.journal, gateway_id=GATEWAY, clock=self.clock, config_version=lambda: self.config["version"],
            start_offset=start_offset, seed=seed, current_state=self.site.tracker.get if current else None,
            risk_observed_at=self.site.risk_observed_at if risk else None, **kw,
        )
        if outbox:
            self.box = snap.bind(self.outbox())
        return snap

    def outbox(self) -> Outbox:
        if not hasattr(self, "_outbox"):
            self._outbox = Outbox(self.journal, self.store, gateway_id=GATEWAY, transport=self.wire,
                                  clock=self.clock, batch_size=self.batch_size)
        return self._outbox

    def close(self) -> None:
        self.journal.close()


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path)
    yield k
    k.close()


def contract_ok(payload: dict) -> None:
    message = Envelope(GATEWAY, FixedClock(T0)).make("event_state_snapshot", payload)
    assert schema_errors(message, "event_state_snapshot") == []


def by_id(payload: dict) -> dict[str, dict]:
    return {e["event_id"]: e for e in payload["active_events"]}


def last_sent_sequence(messages: list[dict]) -> dict[str, tuple[int, str]]:
    """보낸 전이로 다시 접은 이벤트별 (마지막 sequence, 마지막 전이). EC2 가 받은 것과 같다."""
    out: dict[str, tuple[int, str]] = {}
    for m in messages:
        p = m["payload"]
        out[p["event_id"]] = (p["event_sequence"], p["transition"])
    return out


def wire_link(k: Kit, snap: EventStateSnapshot, conns, *, batch_size: int = 64, **client):
    """-171 조립 순서: bind 한 SnapshotOutbox 를 outbox 로, snapshot_payload 는 주지 않고 attach 로 보낸다."""
    link = build_i1_link(
        FakeConnector(conns), Envelope(GATEWAY, k.clock), health_payload=health_payload,
        make_outbox=lambda t: snap.bind(Outbox(k.journal, k.store, gateway_id=GATEWAY, transport=t, clock=k.clock,
                                               batch_size=batch_size)),
        client_kwargs={**FAST, **client},
    )
    snap.attach(link.client, link.publisher.envelope)
    return link


def wire(conn) -> list[tuple[str, int | None]]:
    return [(m["msg_type"], m["payload"].get("journal_offset")) for m in conn.sent]


def raised_observation(messages: list[dict], event_id: str) -> int:
    [p] = [m["payload"] for m in messages
           if m["payload"]["event_id"] == event_id and m["payload"]["transition"] == "RAISED"]
    return p["observation_time"]


# ---------------------------------------------------------------------------- 기본


def test_snapshot_is_fold_of_sent_transitions_and_matches_contract(kit) -> None:
    site = kit.site
    site.step({"w-a": INSIDE, "w-b": INSIDE, "w-c": INSIDE})
    a, b, c = (site.event_of(w) for w in ("w-a", "w-b", "w-c"))
    site.site_ack(c)
    for _ in range(5):  # w-b 는 나가서 해소, w-a·w-c 는 남는다
        site.step({"w-a": INSIDE, "w-b": OUTSIDE, "w-c": INSIDE})
    assert {s.event_id for s in site.tracker.active()} == {a, c}
    snap = kit.snapshot()
    kit.outbox().pump()
    payload = snap.payload()
    assert payload is not None, snap.not_ready
    contract_ok(payload)
    assert payload["journal_offset"] == kit.journal.last_offset == kit.wire.offsets()[-1]
    assert payload["config_version"] == CONFIG and payload["gateway_id"] == GATEWAY
    events = by_id(payload)
    assert set(events) == {a, c}  # 해소된 w-b 는 없다
    sent = last_sent_sequence(kit.wire.sent)
    for event_id, e in events.items():
        state = site.tracker.get(event_id)
        assert e["event_sequence"] == state.event_sequence == sent[event_id][0]
        assert e["raised_at"] == state.raised_at
        assert e["lifecycle"] == str(state.lifecycle) and e["verification"] == "VERIFIED"
        # 주입이 없으면 저널의 마지막 위험 근거(RAISED 판정) 시각. 추적기의 최신 관측 시각은 섞지 않는다
        assert e["last_valid_observation_at"] == raised_observation(kit.wire.sent, event_id)
    assert events[c]["acknowledged"] is True and events[a]["acknowledged"] is False
    assert sent[b][1] == "RESOLVED"
    # 위험 관측 시각을 주입하면(-171) 전이 없이 이어진 위험 관측까지 올린다
    injected = kit.snapshot(risk=True)
    events = by_id(injected.payload())
    assert events[a]["last_valid_observation_at"] == site.risk_seen[a][1] > raised_observation(kit.wire.sent, a)


def test_zero_active_events_and_empty_journal(kit) -> None:
    snap = kit.snapshot()
    assert snap.payload() is None and snap.not_ready == "empty_journal"  # 반영한 offset 이 없다(지어내지 않음)
    kit.site.step({"w-a": INSIDE})
    for _ in range(5):
        kit.site.step({"w-a": OUTSIDE})
    assert kit.site.tracker.active() == ()
    kit.outbox().pump()
    payload = snap.payload()
    contract_ok(payload)
    assert payload["active_events"] == [] and payload["journal_offset"] == kit.journal.last_offset


def test_unknown_config_version_is_not_invented(kit) -> None:
    kit.site.step({"w-a": INSIDE})
    snap = kit.snapshot()
    kit.outbox().pump()
    kit.config["version"] = None
    assert snap.payload() is None and snap.not_ready == "config_version_unknown"
    kit.config["version"] = CONFIG
    assert snap.payload() is not None and snap.not_ready is None


# ---------------------------------------------------------------------------- 반례: 경합


def test_counterexample_new_transitions_while_building_are_not_in_snapshot(kit) -> None:
    """만드는 도중(접기 전·후 모두) 새 전이가 저널에 붙어도 스냅샷은 보낸 offset 까지의 결과다."""
    site = kit.site
    site.step({"w-a": INSIDE})
    a = site.event_of("w-a")
    kit.outbox().pump()
    sent_upto = kit.wire.offsets()[-1]
    # 보낸 뒤·스냅샷 전에 w-a 가 시야에서 사라지고(UPDATED VERIFICATION) w-n 이 새로 들어온다(RAISED)
    site.step({"w-n": INSIDE})
    snap = kit.snapshot()

    def config_during_build():  # 스냅샷을 만드는 중에도 전이가 붙는다(w-a 다시 관측 → UPDATED)
        site.step({"w-a": INSIDE, "w-n": INSIDE})
        return CONFIG

    snap._config_version = config_during_build
    payload = snap.payload()
    contract_ok(payload)
    assert payload["journal_offset"] == sent_upto < kit.journal.last_offset
    events = by_id(payload)
    assert set(events) == {a}  # 아직 보내지 않은 RAISED(w-n)는 없다
    assert events[a]["event_sequence"] == 1 and events[a]["verification"] == "VERIFIED"
    assert site.tracker.get(a).event_sequence == 3  # 추적기는 앞서 있다
    # 저널의 위험 근거(RAISED) 시각 그대로. 추적기의 최신 관측은 섞지 않는다
    raised = kit.wire.sent[0]["payload"]
    assert events[a]["last_valid_observation_at"] == raised["observation_time"]
    # 주입된 위험 관측도 아직 보내지 않은 sequence(3) 의 것이면 섞지 않는다
    assert kit.site.risk_seen[a][0] == 3
    injected = kit.snapshot(risk=True, outbox=False)
    injected.bind(kit.outbox())
    assert by_id(injected.payload())[a]["last_valid_observation_at"] == raised["observation_time"]
    # 스냅샷 뒤의 전이는 같은 outbox 로 이어서 나간다(누락 없음)
    kit.outbox().pump()
    assert kit.wire.offsets() == list(range(kit.journal.last_offset + 1))


def test_counterexample_cursor_rewound_by_resend_request_waits(kit) -> None:
    """journal_ack.resend_from_offset 으로 커서가 물러나면 만들지 않고, 다시 보낸 뒤에 만든다."""
    kit.site.step({"w-a": INSIDE})
    kit.site.step({"w-b": INSIDE})
    snap = kit.snapshot()
    outbox = kit.outbox()
    outbox.pump()
    snap.follow()
    last = kit.journal.last_offset
    result = outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": 0, "resend_from_offset": 1})
    assert result.accepted and outbox.cursor == 1
    assert snap.payload() is None and snap.not_ready == "cursor_behind"
    outbox.pump()
    payload = snap.payload()
    assert payload["journal_offset"] == last


def test_counterexample_resend_dropped_midway_snapshot_once_after_full_replay(tmp_path) -> None:
    """재전송 도중 다시 끊기면 그 연결에는 스냅샷이 없고, 새 연결에서 acked_offset 이후를 오름차순으로 다 보낸 뒤 한 번."""
    pause, release = threading.Event(), threading.Event()

    class PausedConn(FakeConn):
        def send(self, text):
            super().send(text)
            m = json.loads(text)
            if m["msg_type"] == "risk_event_transition" and m["payload"]["journal_offset"] == 4:
                pause.set()
                assert release.wait(4)

    k = Kit(tmp_path, acked=1, batch_size=2)
    try:
        for i in range(4):
            k.site.step({f"w-{j}": INSIDE for j in range(i + 1)})  # 사람이 하나씩 늘며 RAISED
        k.site.step({"w-1": INSIDE, "w-2": INSIDE, "w-3": INSIDE})  # w-0 관측 상실(UPDATED)
        last = k.journal.last_offset
        snap = k.snapshot(outbox=False)
        c1, c2 = PausedConn(), FakeConn()
        link = wire_link(k, snap, [c1, c2], batch_size=2)
        link.start()
        try:
            assert pause.wait(3)
            c1.close()
            assert wait_until(lambda: link.client.generation >= 2)
            release.set()
            assert wait_until(lambda: "event_state_snapshot" in [m["msg_type"] for m in c2.sent])
            time.sleep(0.2)
        finally:
            release.set()
            link.close()
        assert "event_state_snapshot" not in [m["msg_type"] for m in c1.sent]
        kinds = [m["msg_type"] for m in c2.sent]
        assert kinds[0] == "health" and kinds.count("event_state_snapshot") == 1
        snap_at = kinds.index("event_state_snapshot")
        transitions = [m for m in c2.sent[:snap_at] if m["msg_type"] == "risk_event_transition"]
        assert [m["payload"]["journal_offset"] for m in transitions] == list(range(2, last + 1))
        assert all(m["payload"]["resent"] for m in transitions)
        assert not [m for m in c2.sent[snap_at:] if m["msg_type"] == "risk_event_transition"]
        payload = c2.sent[snap_at]["payload"]
        assert schema_errors(c2.sent[snap_at], "event_state_snapshot") == []
        assert payload["journal_offset"] == last
        assert {e["event_id"] for e in payload["active_events"]} == {s.event_id for s in k.site.tracker.active()}
    finally:
        k.close()


def test_counterexample_acked_offset_far_behind(tmp_path) -> None:
    """acked_offset 이 한참 뒤(수백 건 미확인): 여러 pump 에 걸쳐 오름차순으로 한 번씩 보내고 끝난 뒤에 스냅샷."""
    k = Kit(tmp_path)
    try:
        for i in range(60):  # 사람 60명이 차례로 들어갔다 나간다: RAISED·RESOLVED 가 섞인 300여 건
            k.site.step({f"w-{i}": INSIDE})
            for _ in range(4):
                k.site.step({f"w-{i}": OUTSIDE})
        k.site.step({"w-x": INSIDE, "w-y": INSIDE})
        k.site.site_ack(k.site.event_of("w-x"))
        last = k.journal.last_offset
        assert last >= 120
        k.store.save(3)
        snap = k.snapshot(outbox=False)
        conn = FakeConn()
        link = wire_link(k, snap, [conn], batch_size=16)
        link.start()
        try:
            assert wait_until(lambda: "event_state_snapshot" in [m["msg_type"] for m in conn.sent], timeout=10)
        finally:
            link.close()
        kinds = [m["msg_type"] for m in conn.sent]
        snap_at = kinds.index("event_state_snapshot")
        offsets = [m["payload"]["journal_offset"] for m in conn.sent if m["msg_type"] == "risk_event_transition"]
        assert offsets == list(range(4, last + 1))  # 누락·중복 없음, 오름차순
        assert max(i for i, x in enumerate(kinds) if x == "risk_event_transition") < snap_at
        payload = conn.sent[snap_at]["payload"]
        assert payload["journal_offset"] == last
        events = by_id(payload)
        assert set(events) == {s.event_id for s in k.site.tracker.active()}
        assert events[k.site.event_of("w-x")]["acknowledged"] is True
        assert events[k.site.event_of("w-y")]["acknowledged"] is False
    finally:
        k.close()


# ---------------------------------------------------------------------------- 반례: 확인·재개 표시


def test_counterexample_ack_then_resume_shows_unacknowledged(kit) -> None:
    """확인한 이벤트가 해소 후 재개되면 새 위험처럼 미확인으로 돌아간다(6.4). 확인 기록은 저널에 남는다."""
    site = kit.site
    site.step({"w-a": INSIDE})
    a = site.event_of("w-a")
    site.site_ack(a)
    snap = kit.snapshot()
    kit.outbox().pump()
    first = by_id(snap.payload())[a]
    assert first["acknowledged"] is True and first["lifecycle"] == "UPDATED" and first["event_sequence"] == 2
    for _ in range(5):
        site.step({"w-a": OUTSIDE})
    kit.outbox().pump()
    assert snap.payload()["active_events"] == []
    site.step({"w-a": INSIDE})  # 재개 시간(10s) 안 재발생: 같은 event_id
    kit.outbox().pump()
    resumed = by_id(snap.payload())[a]
    assert resumed["acknowledged"] is False and resumed["event_sequence"] == site.tracker.get(a).event_sequence
    assert resumed["raised_at"] == first["raised_at"]
    assert any(m["payload"].get("ack") for m in kit.wire.sent)  # 이전 확인은 보낸 전이에 남았다


# ---------------------------------------------------------------------------- 반례: 재시작·정리


def compacting_kit(tmp_path) -> Kit:
    return Kit(tmp_path, config=JournalConfig(segment_max_bytes=4096, retention_ms=0))


def test_counterexample_restart_seed_with_compacted_raise(tmp_path) -> None:
    """재시작: RAISED 가 정리돼 없어도 시작 상태(seed)로 raised_at 을 채우고, 복원 직후 유효 관측 근거는 null 이다."""
    k = compacting_kit(tmp_path)
    site = k.site
    site.step({"w-a": INSIDE})
    a = site.event_of("w-a")
    original = site.tracker.get(a)
    for i in range(40):  # 다른 사람들로 세그먼트를 넘긴다
        site.step({"w-a": INSIDE, f"w-{i}": INSIDE})
        site.step({"w-a": INSIDE, f"w-{i}": OUTSIDE})
        site.step({"w-a": INSIDE, f"w-{i}": OUTSIDE})
        site.step({"w-a": INSIDE, f"w-{i}": OUTSIDE})
        site.step({"w-a": INSIDE, f"w-{i}": OUTSIDE})
    states = site.tracker.active()
    last = k.journal.last_offset
    k.store.save(last)
    assert k.journal.compact(last, last) > 0 and k.journal.first_offset > 0  # RAISED(offset 0)는 지워졌다
    k.close()

    # 새 프로세스: 저널을 다시 열고 추적기를 복원한다(복원 전이는 저널에 남는다)
    k2 = Kit(tmp_path, config=JournalConfig(segment_max_bytes=4096, retention_ms=0), t0=k.clock.now_ms() + 5000)
    try:
        start = k2.journal.next_offset
        k2.journal.append_step(k2.site.tracker.restore(states))
        snap = k2.snapshot(start_offset=start, seed=states)
        k2.outbox().pump()
        payload = snap.payload()
        contract_ok(payload)
        e = by_id(payload)[a]
        assert e["raised_at"] == original.raised_at
        assert e["verification"] == "UNVERIFIABLE" and e["last_valid_observation_at"] is None
        assert e["event_sequence"] == k2.site.tracker.get(a).event_sequence
        # 다시 관측돼도 전이는 VERIFICATION 뿐이라(위험이 사라진 재관측과 구분 불가) 저널만으로는 null 을 둔다.
        # 위험 관측 시각을 주입하면(-171) 그 시각이 실린다.
        k2.site.step({"w-a": INSIDE})
        k2.outbox().pump()
        e = by_id(snap.payload())[a]
        assert e["verification"] == "VERIFIED" and e["last_valid_observation_at"] is None
        injected = k2.snapshot(start_offset=start, seed=states, risk=True)
        assert by_id(injected.payload())[a]["last_valid_observation_at"] == k2.site.risk_seen[a][1]

        # seed 없이 정리된 저널부터 접으면 raised_at 을 모른다: 지어내지 않고, 추적기가 알면 그 값을 쓴다
        blind = EventStateSnapshot(k2.journal, gateway_id=GATEWAY, clock=k2.clock, config_version=lambda: CONFIG,
                                   start_offset=k2.journal.first_offset)
        blind.bind(k2.outbox())
        assert blind.payload() is None and blind.not_ready == f"raised_at_unknown:{a}"
        helped = EventStateSnapshot(k2.journal, gateway_id=GATEWAY, clock=k2.clock, config_version=lambda: CONFIG,
                                    start_offset=k2.journal.first_offset, current_state=k2.site.tracker.get)
        helped.bind(k2.outbox())
        assert by_id(helped.payload())[a]["raised_at"] == original.raised_at
    finally:
        k2.close()


def test_counterexample_restart_folding_old_records_drops_previous_process_observation(tmp_path) -> None:
    """정리 전 저널을 처음부터 접으면 이전 프로세스의 VERIFIED 시각이 보이지만, 복원(RESTORED) 뒤에는 근거가 없다(null).

    이전 실행의 관측 시각을 남기면 EC2 가 재관측도 안 된 복원 이벤트를 '최근 유효 관측 근거 있음'으로 알린다.
    """
    k = Kit(tmp_path)
    k.site.step({"w-a": INSIDE})
    a = k.site.event_of("w-a")
    states = k.site.tracker.active()
    k.close()
    k2 = Kit(tmp_path, t0=k.clock.now_ms() + 5000)
    try:
        before = EventStateSnapshot(k2.journal, gateway_id=GATEWAY, clock=k2.clock, config_version=lambda: CONFIG,
                                    start_offset=0)
        before.bind(k2.outbox())
        k2.outbox().pump()
        assert by_id(before.payload())[a]["last_valid_observation_at"] is not None  # 이전 실행의 VERIFIED 판정
        k2.journal.append_step(k2.site.tracker.restore(states))
        k2.outbox().pump()
        e = by_id(before.payload())[a]
        assert e["verification"] == "UNVERIFIABLE" and e["last_valid_observation_at"] is None
        assert e["event_sequence"] == 2 and e["raised_at"] == states[0].raised_at
    finally:
        k2.close()


def test_counterexample_guarded_compaction_never_outruns_folding(tmp_path) -> None:
    """접기를 한 번도 하지 않은 채 ACK·정리가 앞서도 SnapshotOutbox.compact 는 접은 위치 너머를 지우지 않는다.
    같은 순서로 저널을 직접 정리하면(우회) 지어내지 않고 멈추고, status 로 보이며, reseed 로 회복한다."""
    k = compacting_kit(tmp_path)
    try:
        guarded = k.snapshot()
        box = k.box
        bypassed = EventStateSnapshot(k.journal, gateway_id=GATEWAY, clock=k.clock, config_version=lambda: CONFIG,
                                      start_offset=0)
        bypassed.bind(k.outbox())
        assert guarded.payload() is None  # 빈 저널
        for i in range(40):
            k.site.step({"w-a": INSIDE, f"w-{i}": INSIDE})
            for _ in range(4):
                k.site.step({"w-a": INSIDE, f"w-{i}": OUTSIDE})
        box.pump()
        box.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": k.journal.last_offset})
        assert box.compact(k.journal.last_offset) == 0  # 아직 아무것도 접지 않았다: 지우지 않는다
        assert k.journal.first_offset == 0
        payload = guarded.payload()
        assert payload is not None and set(by_id(payload)) == {k.site.event_of("w-a")}
        assert box.compact(k.journal.last_offset) > 0  # 접은 뒤에는 정리된다
        # 우회: guarded 의 상한으로 지워진 구간은 bypassed 가 아직 접지 않은 구간이다(공급자마다 자기 SnapshotOutbox 로)
        assert bypassed.payload() is None and bypassed.not_ready == "broken"
        assert "JournalRangeError" in bypassed.status()["broken"]
        bypassed.reseed(k.site.tracker.active(), k.journal.next_offset)  # 저널 쓰기가 끝난(동기) 지점
        assert bypassed.status()["broken"] is None
        k.site.step({"w-a": INSIDE, "w-new": INSIDE})
        box.pump()
        p1, p2 = guarded.payload(), bypassed.payload()
        assert p2 is not None and set(by_id(p2)) == set(by_id(p1)) == {s.event_id for s in k.site.tracker.active()}
        for event_id, e in by_id(p2).items():
            assert e["event_sequence"] == by_id(p1)[event_id]["event_sequence"]
            assert e["raised_at"] == by_id(p1)[event_id]["raised_at"]
    finally:
        k.close()


def test_seed_and_start_offset_are_checked(kit) -> None:
    kit.site.step({"w-a": INSIDE})
    with pytest.raises(ValueError):
        EventStateSnapshot(kit.journal, gateway_id=GATEWAY, clock=kit.clock, config_version=lambda: CONFIG,
                           start_offset=5)
    state = kit.site.tracker.active()[0]
    with pytest.raises(ValueError):  # 같은 event_id 를 두 번 넣지 않는다
        EventStateSnapshot(kit.journal, gateway_id=GATEWAY, clock=kit.clock, config_version=lambda: CONFIG,
                           start_offset=1, seed=[state, state])


# ---------------------------------------------------------------------------- 리뷰 재현 R1~R4 (회귀)


def test_review_r1_ack_and_compaction_before_folding_do_not_lose_snapshot(tmp_path) -> None:
    """R1: 마지막 전이 송신이 끝나기 전에 ACK·정리가 오면(접기 전), 정리는 접은 위치 너머를 지우지 않는다.

    기본 보관 기간(24h)을 지난 세그먼트에서 재현한다. 스냅샷은 그 연결에서 나가고, 정리는 접은 뒤에만 진행된다.
    """
    k = Kit(tmp_path, config=JournalConfig(segment_max_bytes=4096))
    entered, release = threading.Event(), threading.Event()
    try:
        for i in range(10):
            k.site.step({"w-a": INSIDE, f"w-{i}": INSIDE})
        k.clock.advance(24 * 60 * 60 * 1000 + 1)
        last = k.journal.last_offset

        class Paused(FakeConn):
            def send(self, text):
                super().send(text)
                m = json.loads(text)
                if m["msg_type"] == "risk_event_transition" and m["payload"]["journal_offset"] == last:
                    entered.set()
                    assert release.wait(3)

        conn, conn2 = Paused(), FakeConn()
        snap = k.snapshot(outbox=False)
        link = wire_link(k, snap, [conn, conn2])
        link.start()
        try:
            assert entered.wait(3)
            ack = link.outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": last})
            removed = link.outbox.compact(last)  # -171 의 정리 주기(SnapshotOutbox)
            release.set()
            assert wait_until(lambda: "event_state_snapshot" in [m["msg_type"] for m in conn.sent])
            assert ack.accepted and removed == 0 and snap.broken is None
            assert wait_until(lambda: snap.applied_offset == last)
            assert link.outbox.compact(last) > 0  # 접은 뒤에는 정리된다
            k.site.step({"w-a": INSIDE})
            link.client.drop_connection()  # 다음 연결에서도 스냅샷을 만든다
            assert wait_until(lambda: link.client.generation >= 2 and snap.sent >= 2)
        finally:
            release.set()
            link.close()
        assert snap.status()["broken"] is None
        [first] = [m["payload"] for m in conn.sent if m["msg_type"] == "event_state_snapshot"]
        [second] = [m["payload"] for m in conn2.sent if m["msg_type"] == "event_state_snapshot"]
        assert first["journal_offset"] == last and second["journal_offset"] == k.journal.last_offset
        assert set(by_id(second)) == {s.event_id for s in k.site.tracker.active()}
    finally:
        k.close()


def test_review_r2_resend_request_while_building_discards_and_resends_after_replay(kit) -> None:
    """R2: 스냅샷을 만드는 사이 resend_from_offset 이 오면 그 스냅샷을 버리고, 재전송 뒤 다시 만들어 보낸다."""
    kit.site.step({"w-a": INSIDE, "w-b": INSIDE})
    entered, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def config():
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            assert release.wait(3)
        return CONFIG

    snap = kit.snapshot(outbox=False)
    snap._config_version = config
    conn = FakeConn()
    link = wire_link(kit, snap, [conn])
    link.start()
    try:
        assert entered.wait(3)
        result = link.outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": 0, "resend_from_offset": 1})
        assert result.accepted and result.resend_from == 1
        release.set()
        assert wait_until(lambda: "event_state_snapshot" in [m["msg_type"] for m in conn.sent])
        time.sleep(0.2)
    finally:
        release.set()
        link.close()
    order = wire(conn)
    assert order == [("health", None), ("risk_event_transition", 0), ("risk_event_transition", 1),
                     ("risk_event_transition", 1), ("event_state_snapshot", 1)]
    assert snap.discarded == 1 and snap.sent == 1


def test_review_r3_resume_keeps_raised_at_without_current_state(kit) -> None:
    """R3: current_state 없이도 해소 → 재개 기간 안 재개에서 처음 RAISED 의 raised_at 을 잃지 않는다."""
    snap = kit.snapshot(current=False)
    kit.site.step({"w-a": INSIDE})
    a = kit.site.event_of("w-a")
    kit.box.pump()
    raised_at = by_id(snap.payload())[a]["raised_at"]
    for _ in range(5):
        kit.site.step({"w-a": OUTSIDE})
    kit.box.pump()
    assert snap.payload()["active_events"] == []
    kit.site.step({"w-a": INSIDE})
    kit.box.pump()
    payload = snap.payload()
    assert payload is not None, snap.not_ready
    assert by_id(payload)[a]["raised_at"] == raised_at == kit.site.tracker.get(a).raised_at


def test_review_r4_observation_after_risk_disappears_is_not_risk_evidence(kit) -> None:
    """R4: 해소 대기(구역 밖, VERIFIED, 전이 없음) 중 스냅샷은 위험이 사라진 관측 시각을 싣지 않는다."""
    kit.site.step({"w-a": INSIDE})
    a = kit.site.event_of("w-a")
    plain, injected = kit.snapshot(), kit.snapshot(risk=True, outbox=False)
    injected.bind(kit.outbox())
    kit.box.pump()
    before = by_id(plain.payload())[a]["last_valid_observation_at"]
    step = kit.site.step({"w-a": OUTSIDE})
    assert step.transitions == ()
    assert kit.site.tracker.get(a).observation_time > before
    assert by_id(plain.payload())[a]["last_valid_observation_at"] == before
    assert by_id(injected.payload())[a]["last_valid_observation_at"] == before  # 주입값도 위험 관측만


# ---------------------------------------------------------------------------- 수정의 반례


def test_counterexample_repeated_resend_requests_and_request_after_send(kit) -> None:
    """재요청 반복: 만드는 중 두 번 → 둘 다 버림. 스냅샷을 보내는 중 한 번 → 완료로 치지 않고 재전송 뒤 다시 보낸다.
    EC2 가 받는 마지막 스냅샷은 마지막 재전송 뒤에 있다."""
    kit.site.step({"w-a": INSIDE, "w-b": INSIDE})
    kit.site.step({"w-c": INSIDE})
    holder = {}
    builds = {"n": 0}

    def config():
        builds["n"] += 1
        if builds["n"] <= 2:  # 만드는 중 재요청(수신 스레드 흉내)
            holder["link"].outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": 0, "resend_from_offset": 1})
        return CONFIG

    class AckDuringSnapshot(FakeConn):
        fired = False

        def send(self, text):
            super().send(text)
            if json.loads(text)["msg_type"] == "event_state_snapshot" and not AckDuringSnapshot.fired:
                AckDuringSnapshot.fired = True  # 보낸 직후·완료 기록 전 재요청
                holder["link"].outbox.on_journal_ack(
                    {"gateway_id": GATEWAY, "acked_offset": 0, "resend_from_offset": 2})

    snap = kit.snapshot(outbox=False)
    snap._config_version = config
    conn = AckDuringSnapshot()
    link = wire_link(kit, snap, [conn])
    holder["link"] = link
    link.start()
    try:
        assert wait_until(lambda: snap.sent >= 2)
        time.sleep(0.2)
    finally:
        link.close()
    order = wire(conn)
    last = kit.journal.last_offset
    snaps = [i for i, (t, _) in enumerate(order) if t == "event_state_snapshot"]
    assert len(snaps) == 2 and snap.discarded == 3  # 만드는 중 2번 버림 + 보낸 뒤 1번 미완료
    transitions_between = [o for t, o in order[snaps[0]:snaps[1]] if t == "risk_event_transition"]
    assert transitions_between == list(range(2, last + 1))  # 보낸 뒤 재요청의 재전송이 두 스냅샷 사이에
    assert order[-1] == ("event_state_snapshot", last)


def test_counterexample_compaction_and_folding_run_concurrently(tmp_path) -> None:
    """정리와 접기 동시: 판정이 이어지는 동안 다른 스레드가 EC2 ACK(받은 만큼)와 정리를 계속 돌려도 접기가 끊기지 않고,
    재연결 뒤 스냅샷이 추적기와 맞는다."""
    k = compacting_kit(tmp_path)
    c1, c2 = FakeConn(), FakeConn()
    stop = threading.Event()
    try:
        snap = k.snapshot(outbox=False)
        link = wire_link(k, snap, [c1, c2], batch_size=4)
        removed = {"n": 0}

        def ec2_and_compactor():
            while not stop.is_set():
                got = [m["payload"]["journal_offset"] for m in list(c1.sent)
                       if m["msg_type"] == "risk_event_transition"]
                if got:
                    link.outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": max(got)})
                    removed["n"] += link.outbox.compact(max(got))
                time.sleep(0.002)

        side = threading.Thread(target=ec2_and_compactor, daemon=True)
        link.start()
        side.start()
        try:
            for i in range(30):
                k.site.step({"w-a": INSIDE, f"w-{i}": INSIDE})
                for _ in range(4):
                    k.site.step({"w-a": INSIDE, f"w-{i}": OUTSIDE})
                time.sleep(0.01)
            assert wait_until(lambda: link.outbox.drained, timeout=5)
            stop.set()
            side.join(2)
            link.client.drop_connection()
            assert wait_until(lambda: "event_state_snapshot" in [m["msg_type"] for m in c2.sent], timeout=5)
        finally:
            stop.set()
            link.close()
        assert removed["n"] > 0 and k.journal.first_offset > 0  # 정리가 실제로 돌았다
        assert snap.status()["broken"] is None
        [final] = [m["payload"] for m in c2.sent if m["msg_type"] == "event_state_snapshot"]
        assert final["journal_offset"] == k.journal.last_offset
        assert set(by_id(final)) == {s.event_id for s in k.site.tracker.active()}
    finally:
        k.close()


def resolved_at(messages: list[dict], event_id: str) -> int:
    [p] = [m["payload"] for m in messages
           if m["payload"]["event_id"] == event_id and m["payload"]["transition"] == "RESOLVED"]
    return p["observation_time"]


@pytest.mark.parametrize("gap", [10_000, 10_001])
def test_counterexample_resume_window_boundary(kit, gap) -> None:
    """재개 기간 경계(관측 시각 차 10000ms 는 재개, 10001ms 는 새 이벤트, lifecycle 과 같은 경계). 같은 판정에서 먼저
    접히는 다른 사람(w-0)의 전이가 기간 정리를 먼저 돌려도 재개 이벤트의 raised_at 을 잃지 않는다."""
    snap = kit.snapshot(current=False)
    kit.site.step({"w-a": INSIDE})
    a = kit.site.event_of("w-a")
    for _ in range(5):
        kit.site.step({"w-a": OUTSIDE})
    kit.box.pump()
    assert snap.payload()["active_events"] == []
    t_next = resolved_at(kit.wire.sent, a) + gap
    kit.site.step({"w-0": INSIDE, "w-a": INSIDE}, dt=t_next + 40 - kit.clock.now_ms())
    kit.box.pump()
    payload = snap.payload()
    assert payload is not None, snap.not_ready
    events = by_id(payload)
    new_a = kit.site.event_of("w-a")
    assert (new_a == a) is (gap == 10_000)
    assert events[new_a]["raised_at"] == kit.site.tracker.get(new_a).raised_at
    order = [m["payload"]["event_id"] for m in kit.wire.sent if m["payload"]["observation_time"] == t_next]
    assert order[0] == kit.site.event_of("w-0")  # w-0 전이가 먼저 접힌다


def test_counterexample_snapshot_during_clear_hold_and_reappearance_outside(kit) -> None:
    """해소 대기 중 스냅샷: 이벤트는 활성으로 싣지만 위험 근거 시각은 마지막 위험 관측이다. 관측 상실 뒤 구역 밖에서
    다시 보인 VERIFICATION 전이(위험 아님)도 위험 근거로 쓰지 않는다."""
    kit.site.step({"w-a": INSIDE})
    a = kit.site.event_of("w-a")
    snap = kit.snapshot(risk=True)
    kit.site.step({"w-a": INSIDE})
    kit.site.step({"w-a": INSIDE})
    last_risk = kit.site.risk_seen[a][1]
    kit.site.step({})  # 관측 상실: UNVERIFIABLE
    kit.site.step({"w-a": OUTSIDE})  # 구역 밖에서 다시 보임: VERIFICATION 만, 해소 대기 시작
    kit.box.pump()
    changes = [m["payload"]["changes"] for m in kit.wire.sent]
    assert changes[-1] == ["VERIFICATION"] and kit.site.tracker.get(a).verification == "VERIFIED"
    e = by_id(snap.payload())[a]
    assert e["verification"] == "VERIFIED" and e["last_valid_observation_at"] == last_risk
    plain = kit.snapshot(outbox=False)
    plain.bind(kit.outbox())
    assert by_id(plain.payload())[a]["last_valid_observation_at"] == raised_observation(kit.wire.sent, a)


def test_counterexample_partial_fold_limits_compaction(tmp_path) -> None:
    """접기가 일부만 따라온 상태(보낸 커서 중간)에서 ACK 가 끝까지 오고 정리를 돌려도, 정리는 접은 위치까지만 지운다."""
    k = compacting_kit(tmp_path)
    try:
        snap = k.snapshot()
        for i in range(40):
            k.site.step({"w-a": INSIDE, f"w-{i}": INSIDE})
            for _ in range(4):
                k.site.step({"w-a": INSIDE, f"w-{i}": OUTSIDE})
        last = k.journal.last_offset
        assert last > 64
        k.box.pump(1)  # 첫 묶음(64건)만 보냄
        snap.follow()
        assert snap.applied_offset == 63
        k.box.pump()
        k.box.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": last})
        assert k.box.compact(last) > 0
        assert k.journal.first_offset <= 64  # 접지 않은 64 이후는 남았다
        payload = snap.payload()
        assert payload is not None and payload["journal_offset"] == last and snap.broken is None
    finally:
        k.close()


def test_counterexample_resend_request_after_completed_snapshot_sends_new_one(kit) -> None:
    """이미 스냅샷을 마친 연결에서 EC2 가 빈 구간을 다시 요청하면, 재전송을 마친 뒤 새 스냅샷을 보낸다."""
    kit.site.step({"w-a": INSIDE, "w-b": INSIDE})
    snap = kit.snapshot(outbox=False)
    conn = FakeConn()
    link = wire_link(kit, snap, [conn])
    link.start()
    try:
        assert wait_until(lambda: snap.sent == 1)
        time.sleep(0.1)
        link.outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": 0, "resend_from_offset": 1})
        assert wait_until(lambda: snap.sent == 2)
        time.sleep(0.1)
    finally:
        link.close()
    assert wire(conn) == [("health", None), ("risk_event_transition", 0), ("risk_event_transition", 1),
                          ("event_state_snapshot", 1), ("risk_event_transition", 1), ("event_state_snapshot", 1)]
