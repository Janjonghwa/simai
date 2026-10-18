"""강제 종료 복구 시험(test_crash_recovery.py)의 자식 프로세스. 정해진 지점까지 진행한 뒤 "HANG" 을 알리고 멈춘다.

부모가 이 프로세스를 강제로 끝낸다(proc.kill: POSIX SIGKILL, Windows TerminateProcess). 정리 코드는 돌지 않는다.
사용법: python crash_child.py <scenario> <root> [seed]
진행 줄: "ACTIVE <json>" = 직전 단계의 저널 기록(fsync)이 끝난 뒤 추적기의 활성 이벤트 [[event_id, event_sequence], ...].
"""

import json
import os
import random
import sys
import time
from pathlib import Path

from site_driver import GATEWAY, T0, Site, finding  # 같은 디렉터리(스크립트 경로가 sys.path 에 들어간다)

from simai_ai.eventlog import JournalConfig
from simai_ai.risk import FixedClock, Severity
from simai_ai.risk.lifecycle import Lifecycle

SMALL = JournalConfig(segment_max_bytes=1, retention_ms=0)


def say(*parts: object) -> None:
    print(*parts, flush=True)


def hang() -> None:
    say("HANG")
    time.sleep(600)
    os._exit(3)  # 부모가 끝내지 않으면 실패로 끝난다


def report(site: Site) -> None:
    say("ACTIVE", json.dumps(sorted([s.event_id, s.event_sequence] for s in site.tracker.active())))


def main() -> None:
    scenario, root = sys.argv[1], Path(sys.argv[2])
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    clock = FixedClock(T0)

    if scenario == "after_transition":
        # 이벤트 전이(fsync) 직후 종료: 마지막 체크포인트 뒤의 전이는 저널에만 있다.
        site = Site(root, clock)
        a = site.raise_event("w-1")
        site.checkpoint()
        site.ack(a.event_id)
        site.raise_event("w-2", Severity.CRITICAL)
        report(site)
        hang()

    elif scenario == "during_checkpoint":
        # 체크포인트 기록 중(임시 파일 fsync 뒤, 이름 바꾸기 전) 종료.
        armed = {"on": False}

        def fsync(fd: int) -> None:
            os.fsync(fd)
            if armed["on"]:
                say("CP_TMP_SYNCED")
                hang()

        site = Site(root, clock, store_fsync=fsync)
        a = site.raise_event("w-1")
        site.raise_event("w-2")
        site.checkpoint()
        site.ack(a.event_id)
        site.resolve("w-2", others=["w-1"])
        site.raise_event("w-3")
        report(site)
        armed["on"] = True
        site.checkpoint()

    elif scenario == "stale_after_checkpoint":
        # 리뷰 M1 재현: 해소 → 체크포인트 → 같은 이벤트의 과거 RAISED 를 다시 기록 → 강제 종료.
        site = Site(root, clock)
        raised = site.step([finding("w-1")]).transitions[0]
        site.resolve("w-1")
        cp = site.checkpoint()
        site.journal.append_transition(raised)
        site.result.projection.catch_up(site.journal)
        say("STALE", len(site.result.projection.active()), cp.last_applied_journal_offset)
        report(site)
        hang()

    elif scenario == "after_compact":
        # EC2 확인 → 정리 직후 종료(D-G3-13): 해소된 이벤트가 되살아나면 안 된다.
        site = Site(root, clock, journal_config=SMALL)
        site.raise_event("w-old")
        site.raise_event("w-gone")
        site.hold([finding("w-old"), finding("w-gone")], 2)
        site.resolve("w-gone", others=["w-old"])
        site.checkpoint()
        site.hold([finding("w-old")], 2)
        site.outbox.pump()
        site.outbox.on_journal_ack({"gateway_id": GATEWAY, "acked_offset": site.journal.last_offset})
        site.checkpoint()
        site.checkpoint()
        say("COMPACTED", site.journal.first_offset)
        report(site)
        hang()

    elif scenario == "busy":
        # 판정·확인·해소를 쉬지 않고 하면서 체크포인트 스레드가 10ms 마다 쓰는 중 임의 시점 종료.
        rng = random.Random(seed)
        site = Site(root, clock, seed=seed, interval_s=0.01)
        site.checkpointer.start()
        people: dict[str, Severity] = {}
        n = 0
        while True:
            op = rng.random()
            active = site.tracker.active()
            if op < 0.25 or not people:
                people[f"w-{n}"] = rng.choice([Severity.HIGH, Severity.CRITICAL, Severity.CAUTION])
            elif op < 0.4 and active:
                site.ack(rng.choice(active).event_id)
            elif op < 0.55 and people:
                people.pop(rng.choice(sorted(people)))
            elif op < 0.65 and people:
                w = rng.choice(sorted(people))
                people[w] = Severity.CRITICAL
            step = site.step([finding(w, s, metric=rng.random()) for w, s in sorted(people.items())])
            for t in step.transitions:
                if t.transition is Lifecycle.RESOLVED:
                    site.present.discard(t.state.world_ids[0])
            n += 1
            report(site)

    else:
        raise SystemExit(f"모르는 시나리오: {scenario}")


if __name__ == "__main__":
    main()
