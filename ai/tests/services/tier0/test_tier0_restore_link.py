"""재시작 경로의 복원 Track 후보·이전 이벤트 연결 배선 (S15P31A101-178, docs/design/tier0-service.md·tier0-recovery.md).

한 프로세스 안의 실제 서비스(Tier0Service, 실제 스레드·저널·체크포인트·SIM 입력)와 판정 루프 단위(FakePerception)로 본다.
반례(보고 전 자기 반례 시험):
- 30 s 넘은 체크포인트로 재시작: 복원 Track 을 버리고(DISCARDED, 이유 로그) 위치 없는 규칙(같은 규칙·같은 구역)으로만 잇는다.
  이전 경보는 해소되지 않는다.
- 연결 함수 예외(함수 자체·for_frame 둘 다): 연결 없음으로 낮추고 새 이벤트·현장 경보는 그대로 나간다.
- 재시작 두 번 연속(두 번째 프로세스가 인지 전에 끝남): 체크포인트가 복구 결과(Track·시각)를 잃지 않아 세 번째가 같은 후보를 받는다.
- 체크포인트 Track 거부(중복 ID): 후보 없이 시작하고 연결 시간은 연다.
- 판정 루프 순서: 같은 tick 의 ID 교정 뒤, 판정 lock 안에서 연결 함수를 만들고, 입력 정지 tick 은 마지막 실제 World 의 frame 으로.

장면 시각(S15P31A101-181): 재시작한 프로세스도 첫 프로세스와 같은 anchor 로 장면을 이어 간다(실제 재시작처럼 사람은 내려가 있던
시간만큼만 움직인다). 첫 프로세스는 사람이 구역에 선 뒤의 위치가 체크포인트에 실린 다음에 끝낸다. 재시작 때 장면을 앞으로 건너뛰면
사람이 최대 속도보다 빨리 '순간 이동'해 복원 Track 이 갈 수 없는 거리(restored_excluded)가 되고, -54 규칙대로 연결이 빠진다
(첫 프로세스를 언제 끝냈는지에 따라 붙었다 빠졌다 하던 원인).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402
from test_tier0_judge import INSIDE, Site, merge  # noqa: E402
from test_tier0_service import _now, _settings  # noqa: E402

from services.tier0.restore_link import RestoreLink  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
from simai_ai.recovery.relink import RestoredEventLinker  # noqa: E402
from simai_ai.tracking.world_track import (  # noqa: E402
    RestoreState,
    TrackCheckpoint,
    TrackLifecycle,
    WorldIdIssuer,
    WorldTrackFrame,
    WorldTrackManager,
)

# 걷는 사람(tier0_site.scenario)은 2.5 s 에 (4.6, 3.8)에 선다. 첫 프로세스는 그 뒤의 관측을 체크포인트에 싣고 끝낸다
STANDING_MS = 3000
STANDING_XY = (4.6, 3.8)


@pytest.fixture(scope="module")
def sim_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sim")
    return ts.run_sim(tmp, ts.scenario(120_000))


def _r1(service: Tier0Service):
    return [s for s in service.judge.active_states() if s.rule_id == "R1"]


def _first_run(tmp_path, sim_run):
    """첫 프로세스: R1 발생 → 사람이 선 뒤의 관측까지 처리 → 정상 종료(마지막 체크포인트에 Track).

    그 이벤트, 체크포인트 Track ID, 장면 anchor 를 돌려준다. 재시작한 프로세스는 같은 anchor 로 장면을 이어 간다.
    """
    anchor = _now() + 300
    first = Tier0Service(_settings(tmp_path, sim_run, anchor_ms=anchor))
    first.start()
    try:
        assert ts.wait_until(lambda: bool(_r1(first)), timeout=20)
        [event] = _r1(first)

        def standing_seen() -> bool:  # 선 뒤의 관측을 처리했고 그 사람의 Track 이 체크포인트 재료에 있다
            with first.judge_lock:
                last_t = first.perception.manager.last_t
                ids = {t.world_id for t in first.perception.manager.export_tracks()}
            return last_t is not None and last_t >= anchor + STANDING_MS and set(event.world_ids) <= ids

        assert ts.wait_until(standing_seen, timeout=20)
    finally:
        assert first.stop() is True
    tracks = first.checkpointer.last_checkpoint.tracks
    [subject] = [t for t in tracks if t.world_id in event.world_ids]
    # 전제 확인: 복원 Track 은 선 자리다(재시작 뒤 같은 자리 관측이 갈 수 있는 거리 안)
    assert abs(subject.position_xyz[0] - STANDING_XY[0]) < 0.3 and abs(subject.position_xyz[1] - STANDING_XY[1]) < 0.3, subject
    return event, tuple(t.world_id for t in tracks), anchor


def _new_r1(service: Tier0Service, old_id: str):
    return [s for s in _r1(service) if s.event_id != old_id and str(s.lifecycle) != "RESOLVED"]


def _assert_previous_kept(service: Tier0Service, event) -> None:
    kept = service.tracker.get(event.event_id)
    assert kept is not None and str(kept.lifecycle) != "RESOLVED", "이전 경보가 해소됐다"
    assert kept.world_ids == event.world_ids  # 새 ID 로 옮기지 않는다(연결만)
    assert str(kept.verification) == "UNVERIFIABLE"


def test_restart_links_new_event_to_restored_event_without_moving_or_resolving_it(tmp_path, sim_run) -> None:
    event, track_ids, anchor = _first_run(tmp_path, sim_run)
    second = Tier0Service(_settings(tmp_path, sim_run, anchor_ms=anchor))  # 장면을 이어 간다
    second.start()
    seen_world_ids: set[str] = set()
    try:
        assert ts.wait_until(lambda: bool(_new_r1(second, event.event_id)), timeout=20)
        [new] = _new_r1(second, event.event_id)
        assert new.related_event_id == event.event_id
        assert not set(new.world_ids) & set(track_ids)  # 새 ID(재시작 뒤 세션)
        status = second.restore_link.status()
        assert status["state"] == "ACCEPTED" and set(event.world_ids) <= set(status["restored"])
        assert event.event_id in status["linkable_events"]
        # 복원 Track 은 위치 정본이 아니다: 판정 입력 World(world_state payload)·체크포인트 Track 에 나가지 않는다
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            with second.judge_lock:
                last = second.judge._last
                exported = {t.world_id for t in second.perception.manager.export_tracks()}
            if last is not None:
                seen_world_ids |= {t["world_id"] for t in last[0]["tracks"]}
            assert not exported & set(track_ids)
            time.sleep(0.05)
        assert seen_world_ids and not seen_world_ids & set(track_ids)
        _assert_previous_kept(second, event)
        assert second.status()["restore_link"]["state"] == "ACCEPTED"  # 표시 파일에도 싣는다
    finally:
        assert second.stop() is True


def test_checkpoint_older_than_30s_discards_tracks_logs_reason_and_links_by_zone_only(tmp_path, sim_run, caplog) -> None:
    """반례: 30 s 넘은 체크포인트. 복원 Track 을 후보로 쓰지 않고(이유 로그) 같은 규칙·같은 구역 규칙으로만 잇는다."""
    event, track_ids, anchor = _first_run(tmp_path, sim_run)
    skew = 45_000  # 재시작이 45 s 뒤라고 본다(벽시계를 앞으로 민다. 같은 anchor 라 장면도 45 s 뒤, 사람은 그대로 서 있다)
    wall = lambda: time.time_ns() // 1_000_000 + skew  # noqa: E731
    second = Tier0Service(_settings(tmp_path, sim_run, anchor_ms=anchor), wall_ms=wall)
    caplog.set_level(logging.WARNING, logger="services.tier0.restore_link")
    second.start()
    try:
        assert ts.wait_until(lambda: bool(_new_r1(second, event.event_id)), timeout=20)
        [new] = _new_r1(second, event.event_id)
        status = second.restore_link.status()
        assert status["state"] == "DISCARDED" and status["restored"] == [] and status["alive"] == []
        assert "상한 30000 ms" in status["reason"]
        assert any("복원 Track 을 버렸다" in r.getMessage() and "상한 30000 ms" in r.getMessage() for r in caplog.records)
        assert second.perception.manager.restored_alive == ()
        # 후보가 없으니 새 Track 은 복원 후보 표시가 없다(위치를 쓰지 않았다)
        with second.judge_lock:
            snaps = second.judge._last_frame.tracks
        assert snaps and all(s.restored_candidates == () and s.restored_excluded == () for s in snaps)
        assert new.related_event_id == event.event_id  # 위치를 모를 때 규칙: 같은 R1·같은 구역
        _assert_previous_kept(second, event)
    finally:
        assert second.stop() is True


def _raising_fn(*_args, **_kwargs):
    raise RuntimeError("연결 함수 고장(시험)")


@pytest.mark.parametrize("where", ["related_fn", "for_frame"])
def test_related_failure_degrades_to_no_link_and_keeps_alert(tmp_path, sim_run, monkeypatch, where) -> None:
    """반례: 연결 함수가 예외를 내도(함수 자체·for_frame) 새 이벤트는 연결 없이 발생하고 현장 경보 경로가 막히지 않는다."""
    event, _, anchor = _first_run(tmp_path, sim_run)
    if where == "related_fn":
        monkeypatch.setattr(RestoredEventLinker, "for_frame", lambda self, frame, status: _raising_fn)
    else:
        monkeypatch.setattr(RestoredEventLinker, "for_frame", _raising_fn)
    second = Tier0Service(_settings(tmp_path, sim_run, anchor_ms=anchor))
    second.start()
    try:
        assert ts.wait_until(lambda: bool(_new_r1(second, event.event_id)), timeout=20)
        [new] = _new_r1(second, event.event_id)
        assert new.related_event_id is None
        assert second.judge.stats.errors == 0  # 판정 tick 이 실패하지 않았다
        if where == "for_frame":
            assert second.restore_link.related_errors > 0
        _assert_previous_kept(second, event)
    finally:
        assert second.stop() is True


def test_two_restarts_in_a_row_keep_checkpoint_tracks_for_the_third(tmp_path, sim_run) -> None:
    """반례: 두 번째 프로세스가 인지 tick 없이 끝나도 그 체크포인트가 복구 결과(Track·Track 시각)를 잃지 않는다."""
    event, track_ids, anchor = _first_run(tmp_path, sim_run)
    second = Tier0Service(_settings(tmp_path, sim_run, anchor_ms=_now() + 120_000))  # 프레임이 오지 않는다
    second.start()
    try:
        assert ts.wait_until(lambda: second.checkpointer.last_checkpoint is not None, timeout=5)
        assert second.judge.stats.perceived == 0
    finally:
        assert second.stop() is True
    cp = second.checkpointer.last_checkpoint
    assert tuple(t.world_id for t in cp.tracks) == track_ids and cp.config_version == "cfg-1"
    third = Tier0Service(_settings(tmp_path, sim_run, anchor_ms=anchor))  # 첫 프로세스의 장면을 이어 간다
    third.start()
    try:
        assert ts.wait_until(lambda: bool(_new_r1(third, event.event_id)), timeout=20)
        [new] = _new_r1(third, event.event_id)
        status = third.restore_link.status()
        assert status["state"] == "ACCEPTED" and set(event.world_ids) <= set(status["restored"])
        assert new.related_event_id == event.event_id
        _assert_previous_kept(third, event)
    finally:
        assert third.stop() is True


# --- 단위: RestoreLink 와 판정 루프 배선 ---------------------------------------------------------------------


def _checkpoint_track(world_id: str, t: int) -> TrackCheckpoint:
    return TrackCheckpoint(world_id=world_id, created_t=t - 1000, t_last_valid=t, lifecycle=TrackLifecycle.CONFIRMED,
                           identity_revision=0, aliases=(), position_xyz=(4.6, 3.8, 0.9),
                           filter_sigma_xyz=(0.05, 0.05, 0.05), velocity=(0.0, 0.0, 0.0))


def test_rejected_checkpoint_tracks_start_without_candidates_but_open_link_window(caplog) -> None:
    manager = WorldTrackManager(WorldIdIssuer("s2"))
    t0 = 1_760_342_400_000
    dup = [_checkpoint_track("w-s1-1", t0), _checkpoint_track("w-s1-1", t0)]
    caplog.set_level(logging.ERROR, logger="services.tier0.restore_link")
    link = RestoreLink(manager, SimpleNamespace(tracks=dup, tracks_t=t0, restored=()))
    assert link.rejected_tracks is not None and "ValueError" in link.rejected_tracks
    assert manager.restore_status.state is RestoreState.PENDING  # 빈 후보로 다시 불렀다
    assert any("복원 후보로 받지 못했다" in r.getMessage() for r in caplog.records)
    assert link.status()["rejected_tracks"] == link.rejected_tracks


class _RecordingLink:
    """판정 루프가 related 를 부른 시점의 상태를 기록하는 대역."""

    def __init__(self, site: Site) -> None:
        self.site = site
        self.calls: list[tuple[int, bool, tuple]] = []

    def related(self, frame: WorldTrackFrame):
        active = tuple(sorted(w for s in self.site.tracker.active() for w in s.world_ids))
        self.calls.append((frame.t, self.site.loop.lock._is_owned(), active))
        return lambda key, finding: "restored-event-1" if key.rule_id == "R1" else None


def test_judge_builds_related_after_identity_correction_inside_lock_and_on_frozen_ticks(tmp_path) -> None:
    import threading

    from services.tier0.judge import JudgmentLoop

    site = Site(tmp_path)
    try:
        link = _RecordingLink(site)
        site.loop = JudgmentLoop(
            lock=threading.RLock(), now_ms=site.clock.now_ms, config_sync=site.config_sync, perception=site.perception,
            tracker=site.tracker, journal=site.journal, writer=site.writer, calibration_state=lambda: "PROVISIONAL",
            send_i0=lambda kind, payload: site.i0.append((kind, payload)) or True, restore_link=link,
        )
        site.tick({"w-x": (INSIDE, 0)})
        [event] = [s for s in site.tracker.active() if s.rule_id == "R1"]
        assert event.related_event_id == "restored-event-1"  # 연결 함수가 apply 에 넘어갔다
        # 같은 tick 에 병합(X→Y): 연결 함수는 교정 뒤(이벤트가 이미 Y)·lock 안에서 만든다
        site.tick({"w-y": (INSIDE, 1)}, revisions=(merge("w-x", "w-y"),))
        t_last, owned, active = link.calls[-1]
        assert owned and active == ("w-y",)
        assert all(c[1] for c in link.calls)
        # 입력 정지 tick: 마지막 실제 World 의 frame 으로 만든다
        site.clock.advance(100)
        site.loop.tick()
        assert link.calls[-1][0] == t_last and len(link.calls) == 3
    finally:
        site.close()
