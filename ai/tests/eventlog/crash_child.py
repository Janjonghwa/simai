"""강제 종료 시험(test_crash.py)의 자식 프로세스. 정해진 지점까지 진행한 뒤 "HANG" 을 알리고 멈춘다.

부모가 이 프로세스를 강제로 끝낸다(proc.kill: POSIX SIGKILL, Windows TerminateProcess). 정리 코드는 돌지 않는다.
사용법: python crash_child.py <scenario> <journal_dir>
"""

import copy
import json
import os
import sys
import time
from pathlib import Path

from simai_ai.eventlog import (
    ACKED_NAME,
    TRANSITION_KIND,
    AckedOffsetStore,
    EventJournal,
    JournalConfig,
    Outbox,
)
from simai_ai.risk import FixedClock

GATEWAY = "tier0-01"
T0 = 1760342400100
EXAMPLE = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "risk_event_transition.json"
EC2_LOG = "ec2_received.jsonl"


def say(*parts: object) -> None:
    print(*parts, flush=True)


def hang() -> None:
    say("HANG")
    time.sleep(600)
    os._exit(3)  # 부모가 끝내지 않으면 실패로 끝난다


def payload(i: int) -> dict:
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    p = copy.deepcopy(doc["payload"])
    del p["journal_offset"], p["resent"]
    p.update(event_id=f"0192a6f0-7c1e-7b3a-9f2d-{i:012x}", event_sequence=1, transition="RAISED")
    return p


class FileEc2:
    """EC2 흉내: 받은 메시지를 파일에 추가하고 fsync 한다(EC2 가 영속 저장한 것)."""

    def __init__(self, root: Path) -> None:
        self._path = root / EC2_LOG

    def __call__(self, messages) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.writelines(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
            f.flush()
            os.fsync(f.fileno())


def append_n(journal: EventJournal, start: int, n: int) -> None:
    for i in range(start, start + n):
        e = journal.append(TRANSITION_KIND, payload(i))
        say("APPENDED", e.journal_offset, e.msg_id)


def main() -> None:
    scenario, root = sys.argv[1], Path(sys.argv[2])
    clock = FixedClock(T0)
    small = scenario in ("during_compact", "during_rotate")
    config = JournalConfig(segment_max_bytes=1, retention_ms=0) if small else None
    journal = EventJournal.open(root, clock=clock, config=config)
    store = AckedOffsetStore(root / ACKED_NAME, gateway_id=GATEWAY)

    if scenario == "after_append":
        # 전이 기록(fsync) 직후 종료: 아직 한 번도 보내지 않았다.
        append_n(journal, 0, 5)
        hang()

    elif scenario == "before_send":
        # 전송 직전(전송 함수 안, EC2 에 닿기 전) 종료.
        append_n(journal, 0, 5)

        def transport(messages) -> None:
            say("SENDING", len(messages))
            hang()

        Outbox(journal, store, gateway_id=GATEWAY, transport=transport, clock=clock).pump()

    elif scenario == "acked_update":
        # EC2 가 받은 뒤 acked_offset 갱신 중(임시 파일 fsync 뒤, 바꾸기 전) 종료.
        append_n(journal, 0, 5)
        outbox = Outbox(journal, store, gateway_id=GATEWAY, transport=FileEc2(root), clock=clock)
        outbox.pump()
        outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": 1})
        say("ACKED", 1)

        def fsync(fd: int) -> None:
            os.fsync(fd)
            say("ACKED_TMP_SYNCED")
            hang()

        outbox._store = AckedOffsetStore(root / ACKED_NAME, gateway_id=GATEWAY, fsync=fsync)
        outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": 4})

    elif scenario == "torn_write":
        # 레코드를 반쯤 쓴 상태에서 종료.
        append_n(journal, 0, 3)
        import simai_ai.eventlog.journal as jmod

        real = jmod.write_all

        def half(fd: int, data: bytes) -> None:
            real(fd, data[: len(data) // 2])
            os.fsync(fd)
            say("HALF_WRITTEN")
            hang()

        jmod.write_all = half
        journal.append(TRANSITION_KIND, payload(3))

    elif scenario == "during_rotate":
        # 세그먼트 회전 중(새 세그먼트 파일을 만든 직후, 레코드를 쓰기 전) 종료. 기록마다 새 세그먼트다.
        append_n(journal, 0, 2)
        outbox = Outbox(journal, store, gateway_id=GATEWAY, transport=FileEc2(root), clock=clock)
        outbox.pump()
        import simai_ai.eventlog.journal as jmod

        def stuck(fd: int, data: bytes) -> None:
            say("ROTATED_BEFORE_WRITE")
            hang()

        jmod.write_all = stuck
        journal.append(TRANSITION_KIND, payload(2))

    elif scenario == "busy":
        # 쉬지 않고 기록하는 중 임의 시점 종료. 돌아온 append 는 모두 남아야 한다. 레코드 하나는 os.write 한 번이라
        # 프로세스 종료가 레코드 중간을 끊는 일은 드물다(레코드 사이 종료를 덮는다). 쓰기 중간 종료는 torn_write 가 덮는다.
        i = 0
        while True:
            append_n(journal, i, 1)
            i += 1

    elif scenario == "during_compact":
        # 확인 후 정리 중(세그먼트 일부만 지움) 종료.
        append_n(journal, 0, 6)
        outbox = Outbox(journal, store, gateway_id=GATEWAY, transport=FileEc2(root), clock=clock)
        outbox.pump()
        outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": 4})
        clock.advance(1)
        real_unlink = Path.unlink
        count = {"n": 0}

        def unlink(self: Path, missing_ok: bool = False) -> None:
            count["n"] += 1
            if count["n"] == 3:
                say("COMPACT_PARTIAL")
                hang()
            real_unlink(self, missing_ok=missing_ok)

        Path.unlink = unlink  # type: ignore[method-assign]
        outbox.compact(last_applied_journal_offset=4)

    else:
        raise SystemExit(f"모르는 시나리오: {scenario}")


if __name__ == "__main__":
    main()
