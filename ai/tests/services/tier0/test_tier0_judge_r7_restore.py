"""Tier 0 판정 루프에서 복원한 R7 이벤트의 해소 근거가 lifecycle 까지 닿는지 (S15P31A101-174, 명세 3.2·6.4, D-G3-09).

JudgmentLoop 는 카메라 연결·마지막 촬영 시각과 구성 묶음 구역의 관측 카메라(geometry.zone_coverage)로 감시 상태를 만들어
ActiveConfig.evaluate → RiskEngine → Evaluation.zone_clear_evidence → LifecycleTracker.apply 로 넘긴다. 인지만 대역이고
(카메라 4대 SIM 보정) 구성 묶음 검증·엔진·R7·추적기·저널은 실제 모듈이다.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402

from services.tier0.judge import JudgmentLoop  # noqa: E402
from services.tier0.perception import Perceived  # noqa: E402
from sim.scenario import parse  # noqa: E402
from sim.simulate import simulate  # noqa: E402
from simai_ai.config import BundleVerifier, ConfigSync  # noqa: E402
from simai_ai.eventlog import EventJournal, JournalWriter  # noqa: E402
from simai_ai.geometry.rig import CameraRig  # noqa: E402
from simai_ai.providers.files import FileCalibrationProvider  # noqa: E402
from simai_ai.risk import (  # noqa: E402
    FixedClock,
    Lifecycle,
    LifecyclePolicy,
    LifecycleTracker,
    Origin,
    default_registry,
)
from simai_ai.risk.lifecycle import Verification  # noqa: E402
from simai_ai.tracking.time_model import CameraClock  # noqa: E402
from simai_ai.tracking.world_track import WorldTrackFrame  # noqa: E402

T0 = 1_760_342_400_000
POLICY = LifecyclePolicy(clear_hold_ms=300)
CAMERAS = ("cam1", "cam2", "cam3", "cam4")


@pytest.fixture(scope="module")
def calibration(tmp_path_factory):
    files = simulate(parse(ts.scenario(300)))
    path = tmp_path_factory.mktemp("cal") / "calibration.json"
    path.write_bytes(files["calibration.json"])
    return path


class _Feed:
    def __init__(self) -> None:
        self.down: set[str] = set()

    def camera_connected(self, camera_id: str) -> bool:
        return camera_id not in self.down


class _Manager:
    def export_tracks(self):
        return ()


@dataclass
class _Perception:
    rig: CameraRig
    calibration_version: str
    feed: _Feed = field(default_factory=_Feed)
    manager: _Manager = field(default_factory=_Manager)
    _t: int | None = None

    def camera_clocks(self):
        return tuple(CameraClock(c, self._t, True) for c in CAMERAS)

    def t_latest_global(self):
        return self._t

    def step(self, now_ms: int, surfaces, sync_invalid=()):
        t = now_ms - 40
        self._t = t
        payload = {"t_latest_global": t, "calibration_version": self.calibration_version, "scene_version": "scene-3",
                   "tracks": []}
        frame = WorldTrackFrame(t=t, calibration_version=self.calibration_version, tracks=(), revisions=())
        return Perceived(t=t, frame=frame, payload=payload, t_latest_global=t)


class Site:
    def __init__(self, tmp: Path, calibration: Path, clock: FixedClock, *, rules=(("R7", 1),), restore=()) -> None:
        doc = json.loads(calibration.read_text(encoding="utf-8"))
        self.clock = clock
        message, fetcher = ts.bundle_message(doc, "cfg-1", T0 - 10, rules=rules)
        verifier = BundleVerifier(fetcher, default_registry(), lambda v: v == doc["calibration_version"], clock)
        config_sync = ConfigSync(verifier, clock, source_id=ts.GATEWAY, restored=verifier.verify(message),
                                 restored_applied_at=T0)
        self.journal = EventJournal.open(tmp, clock=clock)
        self.writer = JournalWriter(self.journal)
        self.writer.start()
        self.tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id=ts.GATEWAY, clock=clock, policy=POLICY)
        if restore:
            self.journal.append_step(self.tracker.restore(restore))
        rig = CameraRig.from_provider(FileCalibrationProvider(calibration))
        self.perception = _Perception(rig, doc["calibration_version"])
        self.loop = JudgmentLoop(lock=threading.RLock(), now_ms=clock.now_ms, config_sync=config_sync,
                                 perception=self.perception, tracker=self.tracker, journal=self.journal,
                                 writer=self.writer, calibration_state=lambda: "PROVISIONAL")
        self.transitions = []

    def run(self, n: int, *, down=()) -> None:
        self.perception.feed.down = set(down)
        for _ in range(n):
            self.clock.advance(100)
            for step in self.loop.tick().steps:
                self.transitions += step.transitions

    def resolved(self):
        return [t for t in self.transitions if t.transition is Lifecycle.RESOLVED]

    def close(self) -> None:
        self.writer.stop(5)
        self.journal.close()


def _restored_site(tmp_path, calibration, *, rules=(("R7", 1),)):
    clock = FixedClock(T0)
    before = Site(tmp_path / "j1", calibration, clock)
    covering = before.loop._monitoring(before.loop.config_sync.begin_tick()).zone_cameras[ts.ZONE["zone_id"]]
    assert len(covering) >= 2, covering  # 한 대만 남기면 저하, 다 붙이면 정상이 되는 구역
    before.run(3, down=covering[1:])
    states = before.tracker.active()
    assert [s.rule_id for s in states] == ["R7"]
    before.close()
    clock.advance(5000)
    return Site(tmp_path / "j2", calibration, clock, rules=rules, restore=states), covering


def test_restored_r7_stays_while_degraded_and_resolves_when_cameras_return(tmp_path, calibration):
    site, covering = _restored_site(tmp_path, calibration)
    try:
        site.run(300, down=covering[1:])  # 30초 저하
        assert site.resolved() == []
        [state] = site.tracker.active()
        assert state.verification is Verification.UNVERIFIABLE
        site.run(5)  # 카메라 복구: 첫 근거부터 300ms 뒤 해소
        assert len(site.resolved()) == 1 and site.tracker.active() == ()
    finally:
        site.close()


def test_restored_r7_is_not_resolved_when_bundle_has_no_r7(tmp_path, calibration):
    """재시작 뒤 구성 묶음에서 R7 이 빠졌다(카메라는 모두 정상): 감시 확보 근거가 없어 해소하지 않는다."""
    site, _ = _restored_site(tmp_path, calibration, rules=(("R1", 1),))
    try:
        site.run(300)
        assert site.resolved() == []
        [state] = site.tracker.active()
        assert state.rule_id == "R7" and state.verification is Verification.UNVERIFIABLE
    finally:
        site.close()


# ---------------------------------------------------------------- 운영자 종료 (S15P31A101-200)


def _drain(site) -> bool:
    """쓰기 스레드가 앞서 넣은 기록을 모두 쓸 때까지 기다린다(빈 단계는 순서 표지일 뿐 기록하지 않는다)."""
    from simai_ai.risk import LifecycleStep

    site.writer.submit_step(LifecycleStep()).result(timeout=5)
    return True


def _close(event_id: str, close_id: str = "7a2b3c4d-5e6f-4a1b-9c2d-3e4f5a6b7c8d"):
    from simai_ai.risk.lifecycle import CloseRequest

    return CloseRequest(event_id=event_id, close_id=close_id, actor_id="user-0003", requested_at=T0, reason="R7 제거")


def test_operator_close_of_stuck_restored_r7_is_accepted_once_and_journaled(tmp_path, calibration):
    """구성에서 R7 이 빠져 근거가 오지 않는 복원 R7: 종료 ACCEPTED → 저널 RESOLVED·활성 목록에서 빠짐. 같은 close_id 재전송은
    DUPLICATE(저널 기록 대기 중이든 기록 뒤든 다시 기록하지 않음)."""
    site, _ = _restored_site(tmp_path, calibration, rules=(("R1", 1),))
    try:
        site.run(10)
        [state] = site.tracker.active()
        result = site.loop.close(_close(state.event_id))
        assert result["result"] == "ACCEPTED" and result["event_sequence"] == state.event_sequence + 1
        assert site.loop.close(_close(state.event_id))["result"] == "DUPLICATE"  # 쓰기 스레드 fsync 전일 수 있다
        assert _drain(site)
        assert site.loop.close(_close(state.event_id))["result"] == "DUPLICATE"
        assert site.loop.active_states() == () and site.loop.current_state(state.event_id) is None
        closes = [e for e in site.journal.iter_from(0) if e.payload.get("close")]
        assert len(closes) == 1 and closes[0].payload["transition"] == "RESOLVED"
        assert (site.loop.stats.closes, site.loop.stats.duplicate_closes) == (1, 2)
        site.run(10)
        assert site.tracker.active() == ()  # R7 이 구성에 없어 다시 나지 않는다
    finally:
        site.close()


def test_operator_close_is_rejected_while_r7_still_judges_the_zone_degraded(tmp_path, calibration):
    """반례: 카메라가 아직 끊긴 채 R7 이 구성에 있으면 판정이 계속 나므로 거부(CONDITION_PRESENT), 저널·상태 그대로.
    카메라가 돌아오면 근거로 자동 해소되고, 그 뒤 종료는 NOT_ACTIVE 다."""
    site, covering = _restored_site(tmp_path, calibration)
    try:
        site.run(5, down=covering[1:])
        [state] = site.tracker.active()
        offset = site.journal.next_offset
        result = site.loop.close(_close(state.event_id))
        assert (result["result"], result["reject_reason"]) == ("REJECTED", "CONDITION_PRESENT")
        assert "event_sequence" not in result
        assert _drain(site) and site.journal.next_offset == offset
        assert site.tracker.active() == (state,)
        site.run(5)
        assert len(site.resolved()) == 1
        assert site.loop.close(_close(state.event_id, "8b3c4d5e-6f7a-4b2c-8d3e-4f5a6b7c8d9e"))["reject_reason"] == "NOT_ACTIVE"
    finally:
        site.close()
