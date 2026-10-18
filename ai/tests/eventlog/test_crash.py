"""강제 종료 복구 (명세 3.2 저널 계약, 15.1 G3 강제 종료 시 전이 유실 0건, 부록 D-G3-07·11·13·14).

자식 프로세스(crash_child.py)가 저널에 쓰다가 정해진 지점에서 멈추면 부모가 강제로 끝낸다(proc.kill: POSIX SIGKILL,
Windows TerminateProcess). 그 뒤 같은 디렉터리로 다시 열어 outbox 를 돌리고, EC2 가 받은 것(종료 전 + 재시작 후)을
msg_id 로 합쳤을 때 저널의 모든 전이가 빠짐없이 있는지 본다. 중복 수신은 같은 msg_id·(event_id, event_sequence)라
EC2 가 멱등 처리한다.

여기서 보는 것은 프로세스 강제 종료다. 전원 차단·Network Volume 장애 주입은 RunPod 에서 하는 FI 시험이다(21장).
"""

import json
import os
import queue
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from simai_ai.eventlog import ACKED_NAME, AckedOffsetStore, EventJournal, JournalConfig, Outbox
from simai_ai.eventlog._lock import WriterLockError
from simai_ai.risk import FixedClock

CHILD = Path(__file__).with_name("crash_child.py")
AI_DIR = Path(__file__).resolve().parents[2]
GATEWAY = "tier0-01"
EC2_LOG = "ec2_received.jsonl"
TIMEOUT_S = 30


class Child:
    def __init__(self, scenario: str, root: Path) -> None:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, str(CHILD), scenario, str(root)],
            cwd=AI_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.lines: list[str] = []
        self._q: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._q.put(line.rstrip("\n"))
        self._q.put(None)

    def wait_for(self, predicate) -> str:
        deadline = time.monotonic() + TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.kill()
                raise AssertionError(f"자식이 기대한 줄을 내지 않았다: {self.lines}")
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise AssertionError(f"자식이 먼저 끝났다: {self.lines}\n{err}")
            self.lines.append(line)
            if predicate(line):
                return line

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(TIMEOUT_S)
        assert self.proc.returncode != 0

    def appended(self) -> dict[int, str]:
        """돌아온 append(fsync 끝)의 journal_offset → msg_id."""
        out = {}
        for line in self.lines:
            parts = line.split()
            if parts and parts[0] == "APPENDED":
                out[int(parts[1])] = parts[2]
        return out


def crash(scenario: str, root: Path, until: str = "HANG") -> Child:
    child = Child(scenario, root)
    child.wait_for(lambda line: line == until)
    child.kill()
    return child


def reopen(root: Path, config: JournalConfig | None = None) -> EventJournal:
    # 강제 종료 직후 운영체제가 잠금을 늦게 풀 수 있어 잠시 다시 시도한다.
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        try:
            return EventJournal.open(root, clock=FixedClock(1760342500000), config=config)
        except WriterLockError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def ec2_log(root: Path) -> list[dict]:
    path = root / EC2_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def restart_and_deliver(root: Path, config: JournalConfig | None = None):
    """재시작: 저널을 다시 열고 outbox 를 끝까지 보낸다. (저널, 재시작 후 받은 메시지, acked_offset)."""
    journal = reopen(root, config)
    received: list[dict] = []
    store = AckedOffsetStore(root / ACKED_NAME, gateway_id=GATEWAY)
    outbox = Outbox(journal, store, gateway_id=GATEWAY, transport=received.extend, clock=FixedClock(1760342500000))
    outbox.pump()
    return journal, received, outbox.acked_offset


def assert_no_loss(journal: EventJournal, before: list[dict], after: list[dict], acked: int | None) -> None:
    """저널의 모든 기록이 EC2 에(종료 전 또는 재시작 후) msg_id 로 한 번 이상 도착했고, 중복은 같은 내용이다."""
    by_id: dict[str, dict] = {}
    for m in before + after:
        p = m["payload"]
        key = (p["event_id"], p["event_sequence"], p["journal_offset"])
        prev = by_id.setdefault(m["msg_id"], {"key": key})
        assert prev["key"] == key, f"같은 msg_id 에 다른 전이: {prev} vs {key}"
    first = journal.first_offset or 0
    entries = journal.read(first)
    for e in entries:
        if acked is not None and e.journal_offset <= acked:
            # 확인받은 구간은 종료 전에 EC2 가 받은 것이어야 한다.
            assert e.msg_id in {m["msg_id"] for m in before}, e.journal_offset
        assert e.msg_id in by_id, f"유실: offset {e.journal_offset}"
        assert by_id[e.msg_id]["key"] == (e.payload["event_id"], e.payload["event_sequence"], e.journal_offset)
    # 재시작 후 보낸 것은 모두 재전송 표시다(종료 전에 보냈을 수 있다).
    assert all(m["payload"]["resent"] for m in after)


def test_kill_right_after_journal_write_loses_nothing(tmp_path):
    """D-G3-14(기록 직후): 기록만 하고 한 번도 보내지 못한 채 죽어도 재시작 후 모두 보낸다."""
    child = crash("after_append", tmp_path)
    appended = child.appended()
    assert sorted(appended) == [0, 1, 2, 3, 4]
    journal, after, acked = restart_and_deliver(tmp_path)
    try:
        assert journal.report.last_offset == 4 and not journal.report.torn_tail
        assert [m["msg_id"] for m in after] == [appended[i] for i in range(5)]  # 기록 때 정한 msg_id 그대로
        assert_no_loss(journal, [], after, acked)
    finally:
        journal.close()


def test_kill_right_before_send_loses_nothing(tmp_path):
    """D-G3-14(전송 직전): 전송 함수 안에서 EC2 에 닿기 전에 죽어도 재시작 후 같은 msg_id 로 보낸다."""
    child = crash("before_send", tmp_path)
    assert any(line.startswith("SENDING") for line in child.lines)
    journal, after, acked = restart_and_deliver(tmp_path)
    try:
        assert acked is None
        assert [m["payload"]["journal_offset"] for m in after] == [0, 1, 2, 3, 4]
        assert [m["msg_id"] for m in after] == [child.appended()[i] for i in range(5)]
        assert_no_loss(journal, ec2_log(tmp_path), after, acked)
    finally:
        journal.close()


def test_kill_during_acked_offset_update_resends_instead_of_losing(tmp_path):
    """acked_offset 갱신 중 종료: 이전 값(1)이 남아 2 부터 다시 보낸다. 중복은 같은 msg_id 라 EC2 가 걸러 낸다."""
    child = crash("acked_update", tmp_path)
    assert "ACKED_TMP_SYNCED" in child.lines
    before = ec2_log(tmp_path)
    assert [m["payload"]["journal_offset"] for m in before] == [0, 1, 2, 3, 4]
    journal, after, acked = restart_and_deliver(tmp_path)
    try:
        assert acked == 1  # 바꾸기 전에 죽었으니 이전 값. 반쯤 쓴 값은 없다
        assert not (tmp_path / (ACKED_NAME + ".tmp")).exists()  # 남은 임시 파일은 버렸다
        assert [m["payload"]["journal_offset"] for m in after] == [2, 3, 4]
        assert [m["msg_id"] for m in after] == [m["msg_id"] for m in before[2:]]
        assert_no_loss(journal, before, after, acked)
    finally:
        journal.close()


def test_kill_mid_record_cuts_torn_tail_and_continues(tmp_path):
    """레코드를 반쯤 쓴 채 죽으면 재시작이 깨진 꼬리를 잘라 내고, 돌아온 기록은 모두 남는다."""
    child = crash("torn_write", tmp_path)
    assert "HALF_WRITTEN" in child.lines
    journal, after, acked = restart_and_deliver(tmp_path)
    try:
        assert journal.report.torn_tail and journal.report.truncated_bytes > 0
        assert journal.report.last_offset == 2
        assert [m["payload"]["journal_offset"] for m in after] == [0, 1, 2]
        assert_no_loss(journal, [], after, acked)
        nxt = journal.append("risk_event_transition", journal.entry(0).payload_dict() | {"event_sequence": 2})
        assert nxt.journal_offset == 3  # 깨진 기록의 offset 을 다시 쓴다(저장된 적이 없다)
    finally:
        journal.close()


@pytest.mark.parametrize("kill_after", [3, 17])
def test_kill_at_random_point_keeps_every_returned_append(tmp_path, kill_after):
    """쉬지 않고 기록하는 중 임의 시점에 죽여도 돌아온 append 는 모두 남고 offset 이 빈 번호 없이 이어진다.

    레코드 하나가 os.write 한 번이라 이 시험은 주로 레코드 사이 종료를 친다. 레코드 중간 종료(깨진 꼬리)는
    test_kill_mid_record_cuts_torn_tail_and_continues 가 주입으로 덮는다.
    """
    child = Child("busy", tmp_path)
    for _ in range(kill_after):
        child.wait_for(lambda line: line.startswith("APPENDED"))
    time.sleep(random.uniform(0, 0.05))
    child.kill()
    returned = child.appended()
    journal, after, acked = restart_and_deliver(tmp_path)
    try:
        offsets = [m["payload"]["journal_offset"] for m in after]
        assert offsets == list(range(len(offsets)))
        sent = {m["payload"]["journal_offset"]: m["msg_id"] for m in after}
        for off, msg_id in returned.items():
            assert sent.get(off) == msg_id, f"돌아온 append 유실: {off}"
        assert_no_loss(journal, [], after, acked)
    finally:
        journal.close()


def test_kill_during_compaction_keeps_remaining_contiguous(tmp_path):
    """D-G3-13: 확인 후 정리 도중 죽어도 남은 저널이 이어지고, 확인 안 된 구간은 남아 다시 보낸다."""
    config = JournalConfig(segment_max_bytes=1, retention_ms=0)
    child = crash("during_compact", tmp_path)
    assert "COMPACT_PARTIAL" in child.lines
    before = ec2_log(tmp_path)
    journal, after, acked = restart_and_deliver(tmp_path, config)
    try:
        assert acked == 4
        assert journal.first_offset == 2  # 0, 1 만 지웠다(세 번째 지우기 직전에 죽음)
        assert [e.journal_offset for e in journal.read(2)] == [2, 3, 4, 5]
        assert [m["payload"]["journal_offset"] for m in after] == [5]
        assert_no_loss(journal, before, after, acked)
    finally:
        journal.close()


def test_kill_during_segment_rotation_then_compact_and_restart(tmp_path):
    """회전 중(빈 새 세그먼트만 만들고) 죽은 뒤 확인·정리로 기록 세그먼트가 모두 지워져도 마지막 offset 을 잃지 않는다."""
    config = JournalConfig(segment_max_bytes=1, retention_ms=0)
    child = crash("during_rotate", tmp_path)
    assert "ROTATED_BEFORE_WRITE" in child.lines
    before = ec2_log(tmp_path)
    assert [m["payload"]["journal_offset"] for m in before] == [0, 1]

    journal, after, acked = restart_and_deliver(tmp_path, config)
    try:
        assert (journal.report.last_offset, journal.report.next_offset) == (1, 2)
        assert acked is None
        assert [m["payload"]["journal_offset"] for m in after] == [0, 1]  # 확인 전이라 다시 보낸다
        assert_no_loss(journal, before, after, acked)
        store = AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY)
        outbox = Outbox(journal, store, gateway_id=GATEWAY, transport=after.extend, clock=FixedClock(1760342500000))
        res = outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": 1})
        assert res.accepted and res.advanced
        assert outbox.compact(last_applied_journal_offset=1) == 2  # 빈 활성 세그먼트만 남는다
        assert (journal.first_offset, journal.last_offset, journal.next_offset) == (None, 1, 2)
        assert outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": 1}).accepted  # 같은 ack 를 다시 받아도
    finally:
        journal.close()

    # 다시 재시작: outbox 가 시작되고 새 기록이 offset 2 로 이어져 나간다.
    journal, after, acked = restart_and_deliver(tmp_path, config)
    try:
        assert (journal.last_offset, journal.next_offset, acked) == (1, 2, 1)
        base = {k: v for k, v in before[0]["payload"].items() if k not in ("journal_offset", "resent")}
        entry = journal.append("risk_event_transition", base | {"event_sequence": 2})
        assert entry.journal_offset == 2
        received: list[dict] = []
        store = AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY)
        Outbox(journal, store, gateway_id=GATEWAY, transport=received.extend, clock=FixedClock(1760342700000)).pump()
        assert [m["payload"]["journal_offset"] for m in received] == [2]
    finally:
        journal.close()
