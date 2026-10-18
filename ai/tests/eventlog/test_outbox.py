"""outbox·acked_offset (명세 3.2 저널 계약·재동기화, 12.3 I1, 부록 D-G3-07·14·18)."""

import json
import os

import pytest

from simai_ai.eventlog import (
    ACKED_NAME,
    SCHEMA_VERSION,
    AckedOffsetError,
    AckedOffsetStore,
    EventJournal,
    JournalConfig,
    Outbox,
    OutboxError,
    TransportError,
)

GATEWAY = "tier0-01"
ACK_ID = "5b0c7a1e-2f4d-4c8e-9a6b-1d2e3f405162"


class Ec2:
    """EC2 흉내: 받은 메시지를 모으고, 실패를 주입할 수 있다. msg_id 로 중복을 걸러 낸다."""

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []
        self.fail = False

    def __call__(self, messages):
        if self.fail:
            raise TransportError("연결 끊김")
        self.batches.append([json.loads(json.dumps(m)) for m in messages])

    @property
    def messages(self):
        return [m for b in self.batches for m in b]

    def unique(self):
        seen = {}
        for m in self.messages:
            seen.setdefault(m["msg_id"], m)
        return list(seen.values())


@pytest.fixture
def make():
    """저널·acked 저장소·outbox 를 연다. 시험이 끝나면 저널을 닫는다(작성자 잠금 해제)."""
    opened = []

    def do(tmp_path, clock, ec2, **kw):
        journal = EventJournal.open(tmp_path, clock=clock, config=kw.pop("config", None))
        opened.append(journal)
        store = AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY)
        return journal, store, Outbox(journal, store, gateway_id=GATEWAY, transport=ec2, clock=clock, **kw)

    yield do
    for journal in opened:
        journal.close()


def ack(acked, **kw):
    return {"gateway_id": GATEWAY, "acked_offset": acked, **kw}


# --- acked_offset 저장 ----------------------------------------------------------------


def test_acked_store_round_trip_atomic_and_strict(tmp_path):
    store = AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY)
    assert store.load() is None
    store.save(4)
    assert store.load() == 4
    assert not (tmp_path / (ACKED_NAME + ".tmp")).exists()
    # 바꾸기 전에 죽은 임시 파일은 버리고 이전 값을 쓴다.
    (tmp_path / (ACKED_NAME + ".tmp")).write_text('{"gateway_id": "tier0-01", "acked_offset": 9', encoding="utf-8")
    assert store.load() == 4
    assert AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY).load() == 4  # 다시 열어도 같다
    with pytest.raises(AckedOffsetError, match="다른 Tier 0"):
        AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id="tier0-02").load()
    (tmp_path / ACKED_NAME).write_text("{broken", encoding="utf-8")
    with pytest.raises(AckedOffsetError, match="깨졌다"):
        store.load()


def test_acked_store_fsyncs_before_replace(tmp_path, monkeypatch):
    order = []
    store = AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY, fsync=lambda fd: order.append("fsync"))
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b)))
    store.save(1)
    assert order == ["fsync", "replace"]


# --- 전송 -----------------------------------------------------------------------------


def test_outbox_is_journal_after_acked_offset_and_ack_is_persisted(make, tmp_path, clock, append):
    ec2 = Ec2()
    journal, store, outbox = make(tmp_path, clock, ec2)
    entries = [append(journal, i + 1) for i in range(5)]
    assert outbox.pending() == entries and outbox.pending_count() == 5
    assert outbox.pump() == 5
    assert [m["payload"]["journal_offset"] for m in ec2.messages] == [0, 1, 2, 3, 4]
    assert [m["msg_id"] for m in ec2.messages] == [e.msg_id for e in entries]
    assert outbox.drained and outbox.pending_count() == 5  # 보냈지만 확인 전이면 outbox 에 남는다

    res = outbox.on_journal_ack(ack(2))
    assert res.accepted and res.advanced and res.acked_offset == 2
    assert store.load() == 2  # 메모리보다 먼저 디스크에
    assert outbox.pending() == entries[3:]
    assert outbox.pump() == 0  # 이미 보낸 뒤 구간은 다시 보내지 않는다(같은 연결)


def test_messages_match_i1_contract_for_lifecycle_transitions(make, tmp_path, live_events, message_errors):
    ec2 = Ec2()
    journal, _, outbox = make(tmp_path, live_events.clock, ec2)
    journal.append_step(live_events.raise_event())
    for step in live_events.resolve_steps():
        journal.append_step(step)
    outbox.pump()
    assert [m["payload"]["transition"] for m in ec2.messages] == ["RAISED", "RESOLVED"]
    for m in ec2.messages:
        assert m["schema_version"] == SCHEMA_VERSION and m["source_id"] == GATEWAY
        assert m["payload"]["resent"] is False
        assert message_errors(m) == [], m


@pytest.mark.parametrize(
    ("actor", "channel", "role"),
    [
        ("ADMIN", "CONTROL_ROOM", "ADMIN"),
        ("ADMIN", "FOREMAN_PWA", "ADMIN"),  # 관리자가 PWA 로 확인해도 받은 채널 그대로
        ("FOREMAN", "FOREMAN_PWA", "FOREMAN"),
        ("SITE_BUTTON", "SITE_BUTTON", "SITE"),
    ],
)
def test_ack_is_sent_as_contract_acknowledged_with_operator_ack(
    make, tmp_path, live_events, message_errors, actor, channel, role
):
    """lifecycle 확인 기록(UPDATED + ACKNOWLEDGED, AckRecord)은 저널에 그대로 두고, I1 로는 계약 형식으로 보낸다."""
    ec2 = Ec2()
    journal, _, outbox = make(tmp_path, live_events.clock, ec2)
    raised = live_events.raise_event()
    journal.append_step(raised)
    event_id = raised.transitions[0].event_id
    acked = live_events.tracker.acknowledge(
        event_id, actor, "dev-or-acct-1", live_events.clock.now_ms() - 5, channel=channel
    )
    (entry,) = journal.append_step(acked, ack_id=ACK_ID)
    assert entry.payload["transition"] == "UPDATED" and entry.payload["ack"]["actor"] == actor  # 기록은 그대로
    outbox.pump()
    msg = ec2.messages[1]
    assert message_errors(msg) == [], msg
    p = msg["payload"]
    assert p["transition"] == "ACKNOWLEDGED" and p["event_sequence"] == 2 and p["event_id"] == event_id
    assert p["ack"] == {
        "ack_id": ACK_ID,
        "channel": channel,
        "actor_role": role,
        "actor_id": "dev-or-acct-1",
        "acked_at": live_events.clock.now_ms() - 5,
    }
    assert p["evidence_ref"] == ec2.messages[0]["payload"]["evidence_ref"]
    assert journal.entry(entry.journal_offset).payload == entry.payload  # 보내도 기록은 바뀌지 않는다


def test_unsendable_entry_is_held_with_warning_not_sent(make, tmp_path, clock, append, caplog):
    """계약 필수 값(확인의 ack_id)을 모르면 지어내지 않고 그 기록에서 전송을 보류한다. 앞 기록은 보낸다."""
    ec2 = Ec2()
    journal, _, outbox = make(tmp_path, clock, ec2)
    append(journal, 1)
    bad = {"actor": "ADMIN", "actor_id": "a", "acked_at": 1, "recorded_at": 2, "channel": "CONTROL_ROOM"}
    append(journal, 2, "UPDATED", ack=bad)  # 저수준 append 는 ack_id 를 검사하지 않는다
    append(journal, 3)
    with caplog.at_level("WARNING"):
        assert outbox.pump() == 1
    assert [m["payload"]["journal_offset"] for m in ec2.messages] == [0]
    assert outbox.blocked is not None and outbox.blocked.journal_offset == 1 and "ack_id" in outbox.blocked.reason
    assert "전송 보류" in caplog.text
    assert outbox.pump() == 0 and outbox.cursor == 1  # 순서를 지켜 뒤 기록도 보내지 않는다


def test_ack_after_compaction_and_restart_still_carries_evidence(make, tmp_path, live_events, message_errors):
    """오래 활성인 이벤트의 판정 근거 기록이 정리된 뒤 재시작해도 확인 전이는 근거를 싣고 계약을 지킨다."""
    cfg = JournalConfig(segment_max_bytes=1, retention_ms=0)
    ec2 = Ec2()
    journal, _, outbox = make(tmp_path, live_events.clock, ec2, config=cfg)
    raised = live_events.raise_event()
    journal.append_step(raised)
    event_id = raised.transitions[0].event_id
    for i in range(3):  # 다른 이벤트 기록
        journal.append(
            "risk_event_transition", raised.transitions[0].to_payload() | {"event_id": f"{event_id[:-1]}{i}"}
        )
    outbox.pump()
    outbox.on_journal_ack(ack(2))
    live_events.clock.advance(1)
    assert outbox.compact(last_applied_journal_offset=2) == 3  # RAISED 기록이 지워졌다
    journal.close()

    ec2b = Ec2()
    journal, _, outbox = make(tmp_path, live_events.clock, ec2b, config=cfg)
    acked = live_events.tracker.acknowledge(
        event_id, "ADMIN", "acct-1", live_events.clock.now_ms(), channel="CONTROL_ROOM"
    )
    journal.append_step(acked, ack_id=ACK_ID)
    outbox.pump()
    assert [m["payload"]["journal_offset"] for m in ec2b.messages] == [3, 4]
    msg = ec2b.messages[-1]
    assert msg["payload"]["transition"] == "ACKNOWLEDGED"
    assert msg["payload"]["evidence_ref"] == raised.transitions[0].to_payload()["evidence_ref"]
    assert message_errors(msg) == [], msg
    assert outbox.blocked is None


def test_unknown_evidence_is_held_not_sent_as_null(make, tmp_path, live_events, message_errors):
    """근거를 끝내 모르면(저널에 그 이벤트의 판정 기록이 없음) null 로 보내지 않고 보류한다."""
    ec2 = Ec2()
    journal, _, outbox = make(tmp_path, live_events.clock, ec2)
    raised = live_events.raise_event()
    payload = raised.transitions[0].to_payload() | {"evidence_ref": None}
    journal.append("risk_event_transition", payload)
    assert outbox.pump() == 0 and ec2.messages == []
    assert outbox.blocked is not None and "evidence_ref" in outbox.blocked.reason


def test_transport_failure_keeps_cursor_and_retries_same_message_ids(make, tmp_path, clock, append):
    ec2 = Ec2()
    journal, _, outbox = make(tmp_path, clock, ec2, batch_size=2)
    for i in range(5):
        append(journal, i + 1)
    ec2.fail = True
    assert outbox.pump() == 0 and isinstance(outbox.last_error, TransportError)
    assert outbox.cursor == 0
    ec2.fail = False
    assert outbox.pump(max_batches=1) == 2
    assert outbox.pump() == 3
    assert [m["payload"]["journal_offset"] for m in ec2.messages] == [0, 1, 2, 3, 4]


def test_reconnect_resends_from_acked_offset_with_same_ids_and_resent_flag(make, tmp_path, clock, append):
    ec2 = Ec2()
    journal, _, outbox = make(tmp_path, clock, ec2)
    entries = [append(journal, i + 1) for i in range(4)]
    outbox.pump()
    outbox.on_journal_ack(ack(1))
    outbox.reset()
    ec2.batches.clear()
    assert outbox.pump() == 2
    assert [(m["payload"]["journal_offset"], m["payload"]["resent"]) for m in ec2.messages] == [(2, True), (3, True)]
    assert [m["msg_id"] for m in ec2.messages] == [e.msg_id for e in entries[2:]]
    # 새 기록은 처음 보내는 것이다.
    append(journal, 5)
    outbox.pump()
    assert ec2.messages[-1]["payload"]["resent"] is False


def test_resend_from_offset_rewinds_but_never_before_acked(make, tmp_path, clock, append):
    ec2 = Ec2()
    journal, _, outbox = make(tmp_path, clock, ec2)
    for i in range(6):
        append(journal, i + 1)
    outbox.pump()
    ec2.batches.clear()
    res = outbox.on_journal_ack(ack(1, resend_from_offset=3, sync_mode=True))
    assert res.accepted and res.resend_from == 3 and outbox.ec2_sync_mode is True
    outbox.pump()
    assert [m["payload"]["journal_offset"] for m in ec2.messages] == [3, 4, 5]
    assert all(m["payload"]["resent"] for m in ec2.messages)
    ec2.batches.clear()
    outbox.on_journal_ack(ack(4, resend_from_offset=0))  # acked 이전은 다시 요청할 수 없다 → acked+1
    outbox.pump()
    assert [m["payload"]["journal_offset"] for m in ec2.messages] == [5]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"gateway_id": "tier0-02", "acked_offset": 1}, "gateway_mismatch"),
        ({"gateway_id": GATEWAY, "acked_offset": -1}, "invalid_acked_offset"),
        ({"gateway_id": GATEWAY, "acked_offset": True}, "invalid_acked_offset"),
        ({"gateway_id": GATEWAY, "acked_offset": 9}, "beyond_journal"),
        ({"gateway_id": GATEWAY, "acked_offset": 1, "resend_from_offset": "2"}, "invalid_resend_from_offset"),
    ],
)
def test_bad_journal_ack_changes_nothing(make, tmp_path, clock, append, payload, reason):
    journal, store, outbox = make(tmp_path, clock, Ec2())
    for i in range(3):
        append(journal, i + 1)
    res = outbox.on_journal_ack(payload)
    assert not res.accepted and res.reason == reason
    assert outbox.acked_offset is None and store.load() is None


def test_late_lower_ack_does_not_move_acked_back(make, tmp_path, clock, append):
    journal, store, outbox = make(tmp_path, clock, Ec2())
    for i in range(4):
        append(journal, i + 1)
    outbox.on_journal_ack(ack(3))
    res = outbox.on_journal_ack(ack(1))
    assert res.accepted and not res.advanced and res.acked_offset == 3 and store.load() == 3


def test_restart_resends_everything_after_persisted_acked_offset(make, tmp_path, clock, append):
    ec2 = Ec2()
    journal, _, outbox = make(tmp_path, clock, ec2)
    entries = [append(journal, i + 1) for i in range(5)]
    outbox.pump()
    outbox.on_journal_ack(ack(1))
    journal.close()

    ec2b = Ec2()
    journal, _, outbox = make(tmp_path, clock, ec2b)
    assert outbox.acked_offset == 1
    assert outbox.pump() == 3
    got = ec2b.messages
    assert [m["payload"]["journal_offset"] for m in got] == [2, 3, 4]
    assert [m["msg_id"] for m in got] == [e.msg_id for e in entries[2:]]
    assert all(m["payload"]["resent"] for m in got)  # 재시작 전에 보냈을 수 있다
    journal.close()


def test_outbox_refuses_acked_offset_beyond_journal(make, tmp_path, clock, append):
    journal, store, _ = make(tmp_path, clock, Ec2())
    append(journal, 1)
    store.save(5)
    with pytest.raises(OutboxError, match="저널 끝"):
        Outbox(journal, store, gateway_id=GATEWAY, transport=Ec2(), clock=clock)
    journal.close()


def test_compact_through_outbox_keeps_unacked(make, tmp_path, clock, append):
    cfg = JournalConfig(segment_max_bytes=1, retention_ms=0)
    journal, _, outbox = make(tmp_path, clock, Ec2(), config=cfg)
    for i in range(6):
        append(journal, i + 1)
    clock.advance(1)
    assert outbox.compact(last_applied_journal_offset=5) == 0  # 확인받은 것이 없다
    outbox.pump()
    outbox.on_journal_ack(ack(3))
    assert outbox.compact(last_applied_journal_offset=2) == 3  # min(3, 2) = 2 → offset 0..2
    assert journal.first_offset == 3
    assert [e.journal_offset for e in outbox.pending()] == [4, 5]
    journal.close()


def test_ec2_send_happens_only_after_fsync(tmp_path, clock, append):
    """D-G3-18: outbox 는 fsync 가 끝난(append 가 돌아온) 기록만 읽는다."""
    ec2 = Ec2()
    state = {}

    def fsync(fd):
        # fsync 도중에 다른 스레드가 pump 해도 이 기록은 아직 보이지 않는다.
        state["visible_during_fsync"] = journal._next_offset
        os.fsync(fd)

    journal = EventJournal.open(tmp_path, clock=clock, fsync=fsync)
    store = AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY)
    outbox = Outbox(journal, store, gateway_id=GATEWAY, transport=ec2, clock=clock)
    append(journal, 1)
    assert state["visible_during_fsync"] == 0
    outbox.pump()
    assert len(ec2.messages) == 1
    journal.close()
