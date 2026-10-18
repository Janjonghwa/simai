"""주기 체크포인트 (명세 3.2: 초기값 1초, 체크포인트는 저널까지의 요약)."""

import os
import threading
import time

import pytest

from simai_ai.eventlog import JournalWriter
from simai_ai.recovery import DEFAULT_INTERVAL_S, Checkpointer
from simai_ai.tracking.world_track import TrackCheckpoint, TrackLifecycle


class GatedFsync:
    """armed 동안 fsync 를 release 까지 붙잡는다(쓰기 스레드가 기록을 쓰고 fsync 를 기다리는 상태)."""

    def __init__(self) -> None:
        self.armed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, fd: int) -> None:
        os.fsync(fd)
        if self.armed:
            self.entered.set()
            assert self.release.wait(10)


def test_default_interval_is_one_second():
    assert DEFAULT_INTERVAL_S == 1.0


def test_checkpoint_never_includes_transition_not_yet_fsynced(tmp_path, clock, sd):
    """반례(체크포인트가 저널보다 앞섬): 쓰기 대기 중인 전이는 판정 스레드 메모리에는 있지만 저널에는 아직 없다.
    체크포인트는 저널에서 만들므로 그 전이를 담지 않고, last_applied 도 fsync 끝난 기록까지다."""
    gate = GatedFsync()
    site = sd.Site(tmp_path, clock, journal_fsync=gate)
    a = site.raise_event("w-1")
    writer = JournalWriter(site.journal).start()
    gate.armed = True
    step = site.step([sd.finding("w-1"), sd.finding("w-2")], journal=False)
    future = writer.submit_step(step)
    assert gate.entered.wait(10)
    in_memory = {s.event_id for s in site.tracker.active()}
    assert len(in_memory) == 2  # 판정 스레드는 새 이벤트를 이미 안다
    cp = site.checkpoint()
    assert {e.event_id for e in cp.events} == {a.event_id}
    assert cp.last_applied_journal_offset == 0 and cp.last_applied_msg_id == site.journal.entry(0).msg_id
    gate.armed = False
    gate.release.set()
    future.result(10)
    cp2 = site.checkpoint()
    assert {e.event_id for e in cp2.events} == in_memory and cp2.last_applied_journal_offset == 1
    writer.stop(10)
    site.close()


def test_inputs_are_carried_and_tracks_round_trip(tmp_path, clock, sd):
    site = sd.Site(tmp_path, clock)
    site.raise_event("w-1")
    tr = TrackCheckpoint(
        world_id="w-abc-0001",
        created_t=sd.T0,
        t_last_valid=sd.T0 + 500,
        lifecycle=TrackLifecycle.CONFIRMED,
        identity_revision=1,
        aliases=("w-abc-0002",),
        position_xyz=(3.4, 5.0, 0.92),
        filter_sigma_xyz=(0.05, 0.05, 0.08),
        velocity=(0.1, 0.0, 0.0),
    )
    site.checkpointer.update_inputs(config_version="cfg-13", last_capture_time=sd.T0 + 900, tracks_t=sd.T0 + 900,
                                    tracks=[tr])  # fmt: skip
    cp = site.checkpoint()
    site.close()
    again = sd.Site(tmp_path, clock)
    r = again.result
    assert (r.config_version, r.last_capture_time, r.tracks_t, r.tracks) == ("cfg-13", sd.T0 + 900, sd.T0 + 900, (tr,))
    assert r.checkpoint == cp and all(t.observation_state.value == "UNKNOWN" for t in r.tracks)
    again.close()


def test_periodic_thread_writes_generations_and_keeps_two(tmp_path, clock, sd):
    site = sd.Site(tmp_path, clock, interval_s=0.02)
    site.raise_event("w-1")
    site.checkpointer.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        cp = site.checkpointer.last_checkpoint
        if cp is not None and cp.checkpoint_seq >= 4:
            break
        time.sleep(0.01)
    site.checkpointer.stop(10)
    assert site.checkpointer.last_checkpoint.checkpoint_seq >= 4 and site.checkpointer.last_error is None
    assert not site.checkpointer.stalled()
    assert len(list((tmp_path / "checkpoints").glob("checkpoint-*.json"))) == 2
    site.close()


def test_write_failure_keeps_previous_generation_and_reports_stall(tmp_path, clock, sd):
    fail = {"on": False}

    def fsync(fd: int) -> None:
        if fail["on"]:
            raise OSError("디스크 오류(주입)")
        os.fsync(fd)

    now = {"t": 0.0}
    site = sd.Site(tmp_path, clock, store_fsync=fsync)
    site.raise_event("w-1")
    cp1 = site.checkpoint()
    checkpointer = Checkpointer(
        site.journal, site.store, site.result.projection, clock=clock, stall_after_s=5.0, monotonic=lambda: now["t"]
    )
    checkpointer.run_once()
    fail["on"] = True
    site.raise_event("w-2")
    with pytest.raises(OSError):
        checkpointer.run_once()
    assert isinstance(checkpointer.last_error, OSError)
    now["t"] = 6.0
    assert checkpointer.stalled()
    site.close()
    names = sorted(p.name for p in (tmp_path / "checkpoints").iterdir())
    assert all(n.endswith((".json", ".tmp")) for n in names)
    again = sd.Site(tmp_path, clock)
    assert again.result.checkpoint is not None and again.result.checkpoint.checkpoint_seq == cp1.checkpoint_seq + 1
    assert len(again.result.restored) == 2  # 실패한 세대 대신 직전 세대 + 저널
    again.close()
