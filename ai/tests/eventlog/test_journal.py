"""이벤트 저널 계약 (명세 3.2 저널 계약, 부록 D-G3-07·11·18): offset, 추가 전용, fsync, 복구, 정리, 단일 작성자."""

import itertools
import os

import pytest

from simai_ai.eventlog import (
    TRANSITION_KIND,
    EventJournal,
    JournalConfig,
    JournalCorruptError,
    JournalError,
    JournalFormatError,
    JournalRangeError,
    JournalWriteError,
)
from simai_ai.eventlog._lock import WriterLockError
from simai_ai.eventlog.journal import is_judgment_transition, segment_name
from simai_ai.eventlog.record import HEADER_SIZE, decode_at, encode, thaw

ACK_ID = "5b0c7a1e-2f4d-4c8e-9a6b-1d2e3f405162"


def _segments(root):
    return sorted(p for p in root.iterdir() if p.suffix == ".journal")


# --- 레코드 형식 -----------------------------------------------------------------------


def test_record_round_trip_and_tamper_detection():
    rec = encode(7, TRANSITION_KIND, "m-1", 123, {"a": 1, "b": [1.5, None, "한글"]})
    d = decode_at(rec, 0)
    assert d is not None and d.size == len(rec)
    assert (d.entry.journal_offset, d.entry.kind, d.entry.msg_id, d.entry.recorded_at) == (
        7,
        TRANSITION_KIND,
        "m-1",
        123,
    )
    assert d.entry.payload_dict() == {"a": 1, "b": [1.5, None, "한글"]}
    # 꼬리가 짧거나(기록 중 종료) 한 바이트라도 바뀌면 온전한 레코드로 읽지 않는다.
    for cut in (1, HEADER_SIZE - 1, HEADER_SIZE, len(rec) - 1):
        assert decode_at(rec[:cut], 0) is None
    for i in range(len(rec)):
        bad = bytearray(rec)
        bad[i] ^= 0x01
        assert decode_at(bytes(bad), 0) is None, i


@pytest.mark.parametrize("payload", [{"x": float("nan")}, {"x": float("inf")}, {1: "a"}, {"x": object()}])
def test_record_refuses_non_json(payload):
    with pytest.raises(JournalFormatError):
        encode(0, TRANSITION_KIND, "m", 0, payload)


# --- journal_offset 과 event_sequence -------------------------------------------------


def test_journal_offset_is_global_and_independent_of_event_sequence(tmp_path, clock, live_events, append):
    """journal_offset 은 저널 전체에서 0,1,2… 빈 번호 없이 오르고, event_sequence 는 이벤트별로 1 부터다."""
    with EventJournal.open(tmp_path, clock=clock) as j:
        a = append(j, 1, event_id="0192a6f0-7c1e-7b3a-9f2d-00000000000a")
        b = append(j, 1, event_id="0192a6f0-7c1e-7b3a-9f2d-00000000000b")
        a2 = append(j, 2, "UPDATED", event_id="0192a6f0-7c1e-7b3a-9f2d-00000000000a")
        b2 = append(j, 2, "RESOLVED", event_id="0192a6f0-7c1e-7b3a-9f2d-00000000000b")
        assert [e.journal_offset for e in (a, b, a2, b2)] == [0, 1, 2, 3]
        assert [e.payload["event_sequence"] for e in (a, b, a2, b2)] == [1, 1, 2, 2]
        assert j.last_offset == 3 and j.next_offset == 4


def test_journal_records_lifecycle_transitions_and_acks_as_given(tmp_path, live_events):
    """전이와 운영자 확인(ACKNOWLEDGED)을 lifecycle payload 그대로, 기록 순서대로 남긴다."""
    raised = live_events.raise_event()
    event_id = raised.transitions[0].event_id
    acked = live_events.tracker.acknowledge(
        event_id, "FOREMAN", "acct-7", live_events.clock.now_ms(), channel="FOREMAN_PWA"
    )
    resolved = live_events.resolve_steps()
    with EventJournal.open(tmp_path, clock=live_events.clock) as j:
        entries = j.append_step(raised) + j.append_step(acked, ack_id=ACK_ID)
        for step in resolved:
            entries += j.append_step(step)
        expected = [t.to_payload() for s in (raised, acked, *resolved) for t in s.transitions]
        assert [e.payload_dict() for e in entries] == expected
        assert [e.payload["event_sequence"] for e in entries] == [1, 2, 3]
        assert entries[1].payload["changes"] == ("ACKNOWLEDGED",)
        assert entries[1].payload["ack"]["actor_id"] == "acct-7"
        # payload 밖 덧붙임: 계약 ack_id, 확인 전이에 없는 판정 근거는 그 이벤트의 직전 근거
        # meta.state 는 전이 뒤 이벤트의 완전한 상태(재시작 복구용, S15P31A101-51)이고 I1 로 보내지 않는다.
        states = [t.state.to_record() for s in (raised, acked, *resolved) for t in s.transitions]
        assert [thaw(e.meta["state"]) for e in entries] == states
        meta = [{k: v for k, v in e.meta.items() if k != "state"} for e in entries]
        assert meta[1] == {"ack_id": ACK_ID, "evidence_ref": entries[0].payload["evidence_ref"]}
        assert meta[0] == {} and j.has_ack_id(ACK_ID)
        assert [e.payload_dict() for e in j.read(0)] == expected


def test_ack_transition_needs_contract_ack_id(tmp_path, live_events):
    """확인 전이는 계약 ack_id 없이 기록하지 않는다(I1 operator_ack 필수, site_ack 중복 제거)."""
    raised = live_events.raise_event()
    event_id = raised.transitions[0].event_id
    no_channel = live_events.tracker.acknowledge(event_id, "ADMIN", "acct-1", live_events.clock.now_ms())
    acked = live_events.tracker.acknowledge(
        event_id, "ADMIN", "acct-1", live_events.clock.now_ms(), channel="CONTROL_ROOM"
    )
    with EventJournal.open(tmp_path, clock=live_events.clock) as j:
        with pytest.raises(JournalError, match="확인 전이가 아닌데"):
            j.append_transition(raised.transitions[0], ack_id=ACK_ID)
        j.append_step(raised)
        with pytest.raises(JournalError, match="ack_id"):
            j.append_step(acked)
        with pytest.raises(JournalError, match="채널"):  # 받은 채널을 모르면 지어내지 않고 거절
            j.append_step(no_channel, ack_id=ACK_ID)
        assert j.next_offset == 1
        j.append_step(acked, ack_id=ACK_ID)
    with EventJournal.open(tmp_path, clock=live_events.clock) as j:
        assert j.has_ack_id(ACK_ID)  # 재시작 뒤에도 meta 에서 되살린다
        assert j.entry(1).meta["ack_id"] == ACK_ID


def test_journal_refuses_non_live_and_offsets_in_payload(tmp_path, clock, append, tp):
    with EventJournal.open(tmp_path, clock=clock) as j:

        class Planned:
            def to_payload(self):
                return tp(source="PLANNED")

        with pytest.raises(JournalError, match="LIVE"):
            j.append_transition(Planned())
        with pytest.raises(JournalError, match="journal_offset"):
            j.append(TRANSITION_KIND, tp() | {"journal_offset": 5})
        with pytest.raises(JournalError, match="resent"):
            j.append(TRANSITION_KIND, tp() | {"resent": False})
        assert j.next_offset == 0


# --- fsync 와 추가 전용 ---------------------------------------------------------------


def test_every_append_is_fsynced_before_it_returns(tmp_path, clock, append):
    seen = []

    def fsync(fd):
        os.fsync(fd)
        seen.append(j._next_offset)  # fsync 하는 동안에는 아직 기록이 보이지 않는다(잠금 안이라 속성 대신 직접 본다)

    j = EventJournal.open(tmp_path, clock=clock, fsync=fsync)
    with j:
        for i in range(5):
            append(j, i + 1)
            assert len(seen) == i + 1
    assert seen == [0, 1, 2, 3, 4]


def test_append_only_existing_bytes_never_change(tmp_path, clock, append):
    with EventJournal.open(tmp_path, clock=clock) as j:
        append(j, 1)
        before = _segments(tmp_path)[0].read_bytes()
        for i in range(2, 6):
            append(j, i)
            now = _segments(tmp_path)[0].read_bytes()
            assert now[: len(before)] == before
            before = now


def test_fsync_failure_stops_writing_and_restart_recovers(tmp_path, clock, append):
    calls = itertools.count()

    def fsync(fd):
        if next(calls) == 2:
            raise OSError("디스크 오류")
        os.fsync(fd)

    j = EventJournal.open(tmp_path, clock=clock, fsync=fsync)
    append(j, 1)
    append(j, 2)
    with pytest.raises(JournalWriteError):
        append(j, 3)
    with pytest.raises(JournalWriteError):
        append(j, 4)
    j.close()
    # 실패한 기록은 파일에 남았을 수 있다(fsync 만 실패). 다시 열면 온전한 기록까지 이어 쓴다.
    with EventJournal.open(tmp_path, clock=clock) as j2:
        assert j2.last_offset in (1, 2)
        nxt = append(j2, 9)
        assert nxt.journal_offset == j2.report.next_offset
        assert [e.journal_offset for e in j2.read(0)] == list(range(nxt.journal_offset + 1))


# --- 복구 -----------------------------------------------------------------------------


def test_restart_restores_last_offset_and_continues(tmp_path, clock, append):
    with EventJournal.open(tmp_path, clock=clock) as j:
        ids = [append(j, i).msg_id for i in range(1, 4)]
    with EventJournal.open(tmp_path, clock=clock) as j:
        assert (j.report.last_offset, j.report.next_offset, j.report.torn_tail) == (2, 3, False)
        assert append(j, 4).journal_offset == 3
        assert [e.msg_id for e in j.read(0, 3)] == ids


@pytest.mark.parametrize("keep", [1, HEADER_SIZE - 1, HEADER_SIZE + 3, -1])
def test_torn_tail_is_cut_and_offset_reused(tmp_path, clock, keep, append, tp):
    with EventJournal.open(tmp_path, clock=clock) as j:
        append(j, 1)
        append(j, 2)
    seg = _segments(tmp_path)[0]
    good = seg.read_bytes()
    torn = encode(2, TRANSITION_KIND, "m-torn", 0, tp(3))
    seg.write_bytes(good + torn[: keep if keep > 0 else len(torn) + keep])
    with EventJournal.open(tmp_path, clock=clock) as j:
        assert j.report.torn_tail and j.report.truncated_bytes > 0
        assert j.report.last_offset == 1
        assert seg.read_bytes() == good
        e = append(j, 3)
        assert e.journal_offset == 2 and e.msg_id != "m-torn"


def test_readonly_open_reports_torn_tail_without_touching_files(tmp_path, clock, append):
    with EventJournal.open(tmp_path, clock=clock) as j:
        append(j, 1)
    seg = _segments(tmp_path)[0]
    data = seg.read_bytes() + b"SJR1\x05"
    seg.write_bytes(data)
    with EventJournal.open(tmp_path, clock=clock, readonly=True) as r:
        assert r.report.torn_tail and r.report.last_offset == 0
        assert [e.journal_offset for e in r.read(0)] == [0]
        with pytest.raises(JournalError):
            append(r, 2)
    assert seg.read_bytes() == data


def test_corruption_before_valid_records_is_not_silently_dropped(tmp_path, clock, append):
    with EventJournal.open(tmp_path, clock=clock) as j:
        for i in range(3):
            append(j, i + 1)
    seg = _segments(tmp_path)[0]
    data = bytearray(seg.read_bytes())
    data[HEADER_SIZE + 2] ^= 0xFF  # 첫 레코드 본문 손상, 뒤에 온전한 레코드가 있다
    seg.write_bytes(bytes(data))
    with pytest.raises(JournalCorruptError):
        EventJournal.open(tmp_path, clock=clock)
    assert seg.read_bytes() == bytes(data)  # 고치지 않는다


def test_offset_gap_between_segments_is_corruption(tmp_path, clock, append):
    cfg = JournalConfig(segment_max_bytes=1)
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        for i in range(3):
            append(j, i + 1)
    _segments(tmp_path)[1].unlink()
    with pytest.raises(JournalCorruptError, match="이어지지"):
        EventJournal.open(tmp_path, clock=clock)


# --- 세그먼트·읽기·정리 ---------------------------------------------------------------


def test_segments_rotate_and_read_spans_them(tmp_path, clock, append):
    cfg = JournalConfig(segment_max_bytes=2500)
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        entries = [append(j, i + 1) for i in range(10)]
    segs = _segments(tmp_path)
    assert len(segs) > 1
    assert segs[0].name == segment_name(0)
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        assert j.read(0) == entries
        assert j.read(3, 4) == entries[3:7]
        assert j.read(10) == []
        with pytest.raises(JournalRangeError):
            j.read(11)


def test_compact_only_up_to_min_of_acked_and_last_applied_and_after_retention(tmp_path, clock, append):
    cfg = JournalConfig(segment_max_bytes=1, retention_ms=60_000)  # 기록마다 새 세그먼트
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        for i in range(6):
            append(j, i + 1)
        assert len(_segments(tmp_path)) == 6
        assert j.compact(None, 5) == 0 and j.compact(5, None) == 0  # 모르면 지우지 않는다
        assert j.compact(5, 5) == 0  # 보관 기간 안
        clock.advance(60_001)
        assert j.compact(3, 1) == 2  # min(3, 1) = 1 → offset 0, 1
        assert j.first_offset == 2
        assert j.compact(4, 10) == 3  # offset 2..4
        assert j.compact(10, 10) == 0  # 활성(마지막) 세그먼트는 지우지 않는다
        assert j.first_offset == 5 and j.next_offset == 6
        with pytest.raises(JournalRangeError, match="정리"):
            j.read(4)
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        assert (j.first_offset, j.next_offset) == (5, 6)
        assert append(j, 7).journal_offset == 6


def test_ack_ids_are_indexed_across_restart(tmp_path, clock, append):
    ack = {"ack_id": "0192a6f0-7c1e-7b3a-9f2d-0000000000aa", "channel": "SITE_BUTTON"}
    with EventJournal.open(tmp_path, clock=clock) as j:
        append(j, 2, "ACKNOWLEDGED", ack=ack)
        assert j.has_ack_id(ack["ack_id"]) and not j.has_ack_id("other")
    with EventJournal.open(tmp_path, clock=clock) as j:
        assert j.has_ack_id(ack["ack_id"])


# --- 단일 작성자 ----------------------------------------------------------------------


def test_single_writer_lock(tmp_path, clock):
    j = EventJournal.open(tmp_path, clock=clock)
    with pytest.raises(WriterLockError):
        EventJournal.open(tmp_path, clock=clock)
    with EventJournal.open(tmp_path, clock=clock, readonly=True):
        pass  # 읽기 전용은 잠금을 쥐지 않는다
    j.close()
    EventJournal.open(tmp_path, clock=clock).close()


def test_raw_append_of_transition_kind_is_live_only(tmp_path, clock, append):
    with EventJournal.open(tmp_path, clock=clock) as j:
        with pytest.raises(JournalError, match="LIVE"):
            append(j, 1, source="REPLAY")
        assert j.next_offset == 0


def test_last_offset_survives_when_only_an_empty_segment_is_left(tmp_path, clock, append):
    """회전 직후 죽어 빈 세그먼트가 생기고 앞 세그먼트가 정리돼도 마지막 offset 을 잃지 않는다(offset 은 이어진다)."""
    cfg = JournalConfig(segment_max_bytes=1, retention_ms=0)
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        append(j, 1)
        append(j, 2)
    (tmp_path / segment_name(2)).write_bytes(b"")  # 회전 직후(레코드 쓰기 전) 종료 흔적
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        assert (j.last_offset, j.next_offset) == (1, 2)
        clock.advance(1)
        assert j.compact(1, 1) == 2  # 기록이 있는 세그먼트를 모두 지워도
        assert (j.first_offset, j.last_offset, j.next_offset) == (None, 1, 2)
        assert j.read(2) == []
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        assert (j.report.first_offset, j.report.last_offset, j.report.next_offset) == (None, 1, 2)
        assert append(j, 3).journal_offset == 2


def test_evidence_of_active_events_survives_compaction_and_restart(tmp_path, clock, append):
    """정리로 판정 근거 기록이 지워져도 활성 이벤트의 근거는 색인으로 남고, 해소된 이벤트는 색인에서 빠진다."""
    cfg = JournalConfig(segment_max_bytes=1, retention_ms=0)
    a, b = "0192a6f0-7c1e-7b3a-9f2d-00000000000a", "0192a6f0-7c1e-7b3a-9f2d-00000000000b"
    ref_a, ref_b = "0192a6f0-7c1e-7b3a-9f2d-0000000000e1", "0192a6f0-7c1e-7b3a-9f2d-0000000000e2"
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        append(j, 1, event_id=a, evidence_ref=ref_a)
        append(j, 1, event_id=b, evidence_ref=ref_b)
        append(j, 2, "RESOLVED", event_id=b, evidence_ref=ref_b)
        append(j, 3, "UPDATED", event_id="0192a6f0-7c1e-7b3a-9f2d-00000000000c")
        clock.advance(1)
        assert j.compact(2, 2) == 3
    with EventJournal.open(tmp_path, clock=clock, config=cfg) as j:
        assert j._evidence.get(a) == ref_a
        assert b not in j._evidence  # 해소된 이벤트의 근거는 지킬 필요가 없다


@pytest.mark.parametrize(
    ("actor", "channel"), [("ADMIN", "SITE_BUTTON"), ("FOREMAN", "SITE_BUTTON"), ("SITE_BUTTON", "CONTROL_ROOM")]
)
def test_ack_channel_actor_mismatch_is_refused_before_write(tmp_path, live_events, actor, channel):
    """계약 operator_ack 짝(SITE_BUTTON ↔ SITE)이 아닌 확인은 기록 전에 거절한다(보낼 수 없는 기록을 남기지 않음)."""
    raised = live_events.raise_event()
    acked = live_events.tracker.acknowledge(
        raised.transitions[0].event_id, actor, "x-1", live_events.clock.now_ms(), channel=channel
    )
    with EventJournal.open(tmp_path, clock=live_events.clock) as j:
        j.append_step(raised)
        with pytest.raises(JournalError, match="맞지 않는다"):
            j.prepare_step(acked, ack_id=ACK_ID)
        with pytest.raises(JournalError, match="맞지 않는다"):
            j.append_step(acked, ack_id=ACK_ID)
        assert j.next_offset == 1 and not j.has_ack_id(ACK_ID)


def test_non_judgment_transition_with_unknown_evidence_is_recorded(tmp_path, live_events):
    """판정이 아닌 전이(확인)는 판정 근거를 몰라도 거절하지 않는다(계약 1.4.0, outbox 가 null 로 보낸다)."""
    raised = live_events.raise_event()
    acked = live_events.tracker.acknowledge(
        raised.transitions[0].event_id, "SITE_BUTTON", "laptop-01", live_events.clock.now_ms(), channel="SITE_BUTTON"
    )
    with EventJournal.open(tmp_path, clock=live_events.clock) as j:
        j.release(j.prepare_step(acked, ack_id=ACK_ID))
        (entry,) = j.append_step(acked, ack_id=ACK_ID)
        assert "evidence_ref" not in entry.meta and entry.meta["ack_id"] == ACK_ID


@pytest.mark.parametrize(
    ("over", "judgment"),
    [
        ({"transition": "RAISED", "changes": ["RAISED"]}, True),
        ({"transition": "RESOLVED", "changes": ["CLEARED"]}, True),
        ({"transition": "UPDATED", "changes": ["SEVERITY", "METRICS"]}, True),
        ({"transition": "UPDATED", "changes": ["VERIFICATION"]}, True),
        ({"transition": "UPDATED", "changes": ["RESUMED", "SEVERITY"], "resumed": True}, True),
        ({"transition": "UPDATED", "changes": ["IDENTITY_CORRECTED", "SEVERITY"]}, True),
        ({"transition": "UPDATED", "changes": []}, True),
        ({"transition": "UPDATED"}, True),
        ({"transition": "UPDATED", "changes": ["ACKNOWLEDGED"], "ack": {"actor": "ADMIN"}}, False),
        ({"transition": "ACKNOWLEDGED"}, False),
        ({"transition": "UPDATED", "changes": ["RESTORED"]}, False),
        ({"transition": "UPDATED", "changes": ["RESTORED", "VERIFICATION"]}, False),
        ({"transition": "UPDATED", "changes": ["IDENTITY_CORRECTED"]}, False),
        ({"transition": "UPDATED", "changes": ["IDENTITY_CONFLICT"]}, False),
    ],
)
def test_is_judgment_transition(over, judgment):
    assert is_judgment_transition(over) is judgment


def test_transition_with_unknown_evidence_is_refused_before_write(tmp_path, live_events):
    """판정 근거를 payload·저널·대기 중 어디서도 모르면 지어내지 않고 기록 전에 거절한다."""
    raised = live_events.raise_event()

    class NoEvidence:
        def to_payload(self):
            return raised.transitions[0].to_payload() | {"evidence_ref": None}

    class Step:
        transitions = (NoEvidence(),)

    with EventJournal.open(tmp_path, clock=live_events.clock) as j:
        with pytest.raises(JournalError, match="evidence_ref"):
            j.prepare_step(Step())
        with pytest.raises(JournalError, match="evidence_ref"):
            j.append_transition(NoEvidence())
        assert j.next_offset == 0
        j.append_step(raised)  # 근거를 아는 뒤에는 같은 이벤트의 근거 없는 전이도 직전 근거로 기록한다
        entry = j.append_transition(NoEvidence())
        assert entry.meta["evidence_ref"] == raised.transitions[0].to_payload()["evidence_ref"]
