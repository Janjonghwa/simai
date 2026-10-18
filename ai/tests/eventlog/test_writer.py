"""저널 쓰기 스레드 (명세 3.2: 판정 스레드와 분리, 시간 제한, 쓰기 정지 감시, 부록 D-G3-18)."""

import os
import threading

import pytest

from simai_ai.eventlog import (
    ACKED_NAME,
    AckedOffsetStore,
    EventJournal,
    JournalError,
    JournalTimeout,
    JournalWriteError,
    JournalWriter,
    Outbox,
)

GATEWAY = "tier0-01"


class BlockingFsync:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, fd):
        self.entered.set()
        assert self.release.wait(10)
        os.fsync(fd)


class FakeMonotonic:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_judging_thread_does_not_wait_for_fsync_but_ec2_send_does(tmp_path, live_events):
    """D-G3-18: submit_step 은 fsync 를 기다리지 않고 돌아오고(현장 경보는 바로 나간다), outbox 는 fsync 뒤에만 보낸다."""
    fsync = BlockingFsync()
    sent = []
    journal = EventJournal.open(tmp_path, clock=live_events.clock, fsync=fsync)
    outbox = Outbox(
        journal,
        AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY),
        gateway_id=GATEWAY,
        transport=lambda msgs: sent.extend(msgs),
        clock=live_events.clock,
    )
    writer = JournalWriter(journal).start()
    try:
        step = live_events.raise_event()
        future = writer.submit_step(step)
        assert step.local_signals  # 현장 경보 신호는 이미 손에 있다(저널과 무관하게 보낸다)
        assert fsync.entered.wait(10)
        assert not future.done()
        assert outbox.pump() == 0 and sent == []  # fsync 전에는 EC2 로 나가지 않는다
        fsync.release.set()
        (entry,) = future.result(10)
        assert entry.journal_offset == 0
        assert outbox.pump() == 1 and sent[0]["payload"]["event_id"] == step.transitions[0].event_id
    finally:
        fsync.release.set()
        writer.stop(10)
        journal.close()


def test_write_timeout_and_stall_detection(tmp_path, live_events):
    fsync = BlockingFsync()
    mono = FakeMonotonic()
    journal = EventJournal.open(tmp_path, clock=live_events.clock, fsync=fsync)
    writer = JournalWriter(journal, monotonic=mono, stall_after_s=1.0).start()
    try:
        assert not writer.stalled()
        with pytest.raises(JournalTimeout):
            writer.write_step(live_events.raise_event(), timeout_s=0.05)
        assert writer.backlog() == 1
        mono.t += 0.5
        assert not writer.stalled()
        mono.t += 0.6
        assert writer.stalled()  # 1초 넘게 끝나지 않은 기록
        fsync.release.set()
        writer.stop(10)
        assert writer.backlog() == 0 and not writer.stalled()
        assert journal.last_offset == 0  # 시간 제한을 넘겨도 기록은 끝까지 했다
    finally:
        fsync.release.set()
        writer.stop(10)
        journal.close()


def test_writes_keep_submission_order_and_write_failure_is_reported(tmp_path, live_events, tp):
    calls = []

    def fsync(fd):
        calls.append(fd)
        if len(calls) == 3:
            raise OSError("디스크 오류")
        os.fsync(fd)

    journal = EventJournal.open(tmp_path, clock=live_events.clock, fsync=fsync)
    writer = JournalWriter(journal).start()
    try:
        futures = [writer.submit("risk_event_transition", tp(i + 1)) for i in range(4)]
        assert [f.result(10).journal_offset for f in futures[:2]] == [0, 1]
        for f in futures[2:]:
            with pytest.raises(JournalWriteError):
                f.result(10)
        assert isinstance(writer.failed, JournalWriteError)
    finally:
        writer.stop(10)
        journal.close()


def test_bad_ack_step_is_refused_on_submit_and_pending_ack_is_known(tmp_path, live_events):
    """확인 단계 검사는 쓰기 스레드가 아니라 넣을 때 하고, 넣은 ack_id 는 fsync 전에도 has_ack_id 가 안다."""
    fsync = BlockingFsync()
    journal = EventJournal.open(tmp_path, clock=live_events.clock, fsync=fsync)
    writer = JournalWriter(journal).start()
    try:
        raised = live_events.raise_event()
        writer.submit_step(raised)
        assert fsync.entered.wait(10)
        acked = live_events.tracker.acknowledge(
            raised.transitions[0].event_id,
            "SITE_BUTTON",
            "laptop-01",
            live_events.clock.now_ms(),
            channel="SITE_BUTTON",
        )
        with pytest.raises(JournalError, match="ack_id"):
            writer.submit_step(acked)  # 바로 오류(쓰기 스레드에서 조용히 사라지지 않음)
        ack_id = "5b0c7a1e-2f4d-4c8e-9a6b-1d2e3f405162"
        future = writer.submit_step(acked, ack_id=ack_id)
        assert journal.has_ack_id(ack_id) and not future.done()  # 같은 site_ack 가 다시 와도 한 번만 기록
        fsync.release.set()
        (entry,) = future.result(10)
        assert entry.meta["ack_id"] == ack_id and journal.has_ack_id(ack_id)
    finally:
        fsync.release.set()
        writer.stop(10)
        journal.close()


class _EmptyStep:
    transitions = ()


def test_queue_full_releases_pending_ack_so_the_same_ack_can_be_recorded_later(tmp_path, live_events):
    """대기열이 가득 차 넣지 못하면 대기 중으로 올린 ack_id 를 되돌린다(같은 확인을 다시 받으면 기록한다)."""
    fsync = BlockingFsync()
    journal = EventJournal.open(tmp_path, clock=live_events.clock, fsync=fsync)
    writer = JournalWriter(journal, max_queue=1).start()
    ack_id = "5b0c7a1e-2f4d-4c8e-9a6b-1d2e3f405162"
    try:
        raised = live_events.raise_event()
        first = writer.submit_step(raised)
        assert fsync.entered.wait(10)  # 쓰기 스레드가 첫 단계를 쥐고 fsync 에서 멈춤
        writer.submit_step(_EmptyStep())  # 대기열 한 칸을 채운다
        acked = live_events.tracker.acknowledge(
            raised.transitions[0].event_id,
            "SITE_BUTTON",
            "laptop-01",
            live_events.clock.now_ms(),
            channel="SITE_BUTTON",
        )
        with pytest.raises(JournalError, match="가득"):
            writer.submit_step(acked, ack_id=ack_id)
        assert not journal.has_ack_id(ack_id)  # 넣지 못한 확인은 중복으로 여기지 않는다
        fsync.release.set()
        first.result(10)
        (entry,) = writer.submit_step(acked, ack_id=ack_id).result(10)
        assert entry.meta["ack_id"] == ack_id and journal.has_ack_id(ack_id)
    finally:
        fsync.release.set()
        writer.stop(10)
        journal.close()
