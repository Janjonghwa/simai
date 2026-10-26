"""강제 종료 복구 (명세 3.2 재시작 복구, 15.1 G3, 부록 D-G3-09·11·13).

자식 프로세스(crash_child.py)가 Tier 0 흉내(site_driver.Site)로 판정·확인·해소·체크포인트를 하다가 정해진 지점에서
멈추면 부모가 강제로 끝낸다(proc.kill: POSIX SIGKILL, Windows TerminateProcess). 그 뒤 같은 디렉터리로 다시 시작해
복구한 활성 이벤트가 저널(정본)과 일치하는지, 재시작 직후 System Health 가 UNSAFE_UNKNOWN 인지 본다.

로컬 디렉터리에서 프로세스를 강제 종료한 시험이다. Network Volume 강제 종료 실측은 S15P31A101-92 가 한다.
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

from simai_ai.eventlog import JournalConfig
from simai_ai.eventlog._lock import WriterLockError
from simai_ai.recovery import RecoveryHealthGate, RecoverySource
from simai_ai.tracking.system_health import CalibrationState, SystemHealth, evaluate_health
from simai_ai.tracking.time_model import SAME_CLOCK, CameraClock

CHILD = Path(__file__).with_name("crash_child.py")
AI_DIR = Path(__file__).resolve().parents[2]
TIMEOUT_S = 60


class Child:
    def __init__(self, scenario: str, root: Path, seed: int = 0) -> None:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, str(CHILD), scenario, str(root), str(seed)],
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
                raise AssertionError(f"자식이 기대한 줄을 내지 않았다: {self.lines[-5:]}")
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise AssertionError(f"자식이 먼저 끝났다: {self.lines[-5:]}\n{err}")
            self.lines.append(line)
            if predicate(line):
                return line

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(TIMEOUT_S)
        assert self.proc.returncode != 0

    def last_active(self) -> dict[str, int]:
        for line in reversed(self.lines):
            if line.startswith("ACTIVE "):
                return {eid: seq for eid, seq in json.loads(line[len("ACTIVE ") :])}
        raise AssertionError("ACTIVE 줄이 없다")


def _restart_health_is_unsafe() -> None:
    """재시작 직후(프레임·시계 차이 없음, 조립 실수로 SAME_CLOCK 을 넘겨도) System Health 는 UNSAFE_UNKNOWN 이다."""
    gate = RecoveryHealthGate()
    now = 1_760_342_400_000
    for cams, clock in (([CameraClock("cam1", None, True)], None), ([CameraClock("cam1", now - 50, True)], SAME_CLOCK)):
        report = evaluate_health(cams, CalibrationState.CALIBRATED, True, True, now, capture_clock=clock)
        assert gate.apply(report, capture_clock=clock).system_health is SystemHealth.UNSAFE_UNKNOWN


def _open_site(sd, root, clock, **kw):
    # 강제 종료 직후 운영체제가 저널 작성자 잠금을 늦게 풀 수 있어 잠시 다시 시도한다(eventlog test_crash 와 같다).
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        try:
            site = sd.Site(root, clock, **kw)
        except WriterLockError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
            continue
        _restart_health_is_unsafe()
        return site


def _recover(sd, root, clock, **kw):
    truth = sd.journal_truth(root, clock)
    return _open_site(sd, root, clock, **kw), truth


@pytest.fixture
def recovered_clock(sd):
    from simai_ai.risk import FixedClock

    return FixedClock(sd.T0 + 3_600_000)  # 재시작은 자식이 쓴 시각보다 뒤


def test_kill_right_after_transition(tmp_path, sd, recovered_clock):
    child = Child("after_transition", tmp_path)
    child.wait_for(lambda s: s == "HANG")
    expected = child.last_active()
    child.kill()
    site, truth = _recover(sd, tmp_path, recovered_clock)
    assert site.result.source is RecoverySource.CHECKPOINT and site.result.stats.applied > 0
    sd.assert_matches_journal(site.result, truth)
    assert {s.event_id: s.event_sequence - 1 for s in site.result.restored} == expected
    site.close()


def test_kill_right_after_operator_escalation(tmp_path, sd, recovered_clock):
    """운영자 상향(S15P31A101-272) 전이 직후 강제 종료(D-G3-11, S15P31A101-113): 체크포인트 뒤 저널에만 있는 상향이 등급·바닥값까지
    복원되고, escalation_id 는 재시작 뒤에도 저널 색인으로 남는다(같은 명령을 다시 기록하지 않음)."""
    from simai_ai.risk import Severity

    escalation_id = "3c9d1e2f-4a5b-4c6d-9e8f-1a2b3c4d5e6f"  # crash_child.ESCALATION_ID

    child = Child("after_escalate", tmp_path)
    child.wait_for(lambda s: s == "HANG")
    expected = child.last_active()
    child.kill()
    site, truth = _recover(sd, tmp_path, recovered_clock)
    sd.assert_matches_journal(site.result, truth)
    assert {s.event_id: s.event_sequence - 1 for s in site.result.restored} == expected
    [state] = site.result.restored
    assert state.severity is Severity.CRITICAL and state.escalation_floor is Severity.CRITICAL
    assert site.journal.has_escalation_id(escalation_id)
    site.close()


def test_kill_during_checkpoint_write(tmp_path, sd, recovered_clock):
    child = Child("during_checkpoint", tmp_path)
    child.wait_for(lambda s: s == "HANG")
    assert "CP_TMP_SYNCED" in child.lines
    expected = child.last_active()
    child.kill()
    tmps = list((tmp_path / "checkpoints").glob("*.tmp"))
    assert len(tmps) == 1  # 이름을 바꾸기 전에 죽었다
    site, truth = _recover(sd, tmp_path, recovered_clock)
    assert site.result.checkpoint is not None and site.result.checkpoint.checkpoint_seq == 1
    assert not list((tmp_path / "checkpoints").glob("*.tmp"))
    sd.assert_matches_journal(site.result, truth)
    assert {s.event_id: s.event_sequence - 1 for s in site.result.restored} == expected
    site.close()


def test_kill_right_after_compaction(tmp_path, sd, recovered_clock):
    child = Child("after_compact", tmp_path)
    child.wait_for(lambda s: s == "HANG")
    compacted = next(int(s.split()[1]) for s in child.lines if s.startswith("COMPACTED "))
    assert compacted > 0
    expected = child.last_active()
    child.kill()
    site = _open_site(sd, tmp_path, recovered_clock, journal_config=JournalConfig(segment_max_bytes=1, retention_ms=0))
    # 정리된 구간의 해소 이벤트는 되살아나지 않고, 정리 전 발생한 활성 이벤트는 남는다
    assert {s.event_id: s.event_sequence - 1 for s in site.result.restored} == expected
    assert len(expected) == 1
    site.close()


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_kill_at_random_point_while_busy(tmp_path, sd, recovered_clock, seed):
    """판정·확인·해소·10ms 주기 체크포인트가 함께 도는 중 임의 시점 강제 종료. 복구 상태 = 저널(정답)."""
    rng = random.Random(seed)
    target = rng.randint(15, 60)
    child = Child("busy", tmp_path, seed)
    count = {"n": 0}

    def enough(line: str) -> bool:
        if line.startswith("ACTIVE "):
            count["n"] += 1
        return count["n"] >= target

    child.wait_for(enough)
    time.sleep(rng.random() * 0.05)
    child.kill()
    site, truth = _recover(sd, tmp_path, recovered_clock, seed=seed + 100)
    sd.assert_matches_journal(site.result, truth)
    # 체크포인트 스레드가 10ms 마다 쓰므로 보통 체크포인트 + 그 뒤 저널로 복구한다(느린 환경이면 저널만일 수 있다)
    assert site.result.source in (RecoverySource.CHECKPOINT, RecoverySource.JOURNAL)
    site.close()


def test_kill_after_stale_transition_past_checkpoint_does_not_resurrect(tmp_path, sd, recovered_clock):
    """리뷰 M1 재현(강제 종료): 해소 → 체크포인트 → 같은 이벤트의 과거 RAISED 재기록 → kill → 재시작. 활성 0건."""
    child = Child("stale_after_checkpoint", tmp_path)
    child.wait_for(lambda s: s == "HANG")
    assert "STALE 0 1" in child.lines  # 종료 전 계속 돈 투영은 활성 0건, 체크포인트 offset 1
    assert child.last_active() == {}
    child.kill()
    site = _open_site(sd, tmp_path, recovered_clock)
    assert site.result.source is RecoverySource.CHECKPOINT
    assert site.result.restored == () and site.result.restore_step.transitions == ()
    site.close()
