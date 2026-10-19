"""Tier 0 서비스 조립(한 프로세스 안, 실제 스레드)의 반례 시험 (S15P31A101-171).

- 종료 신호 때 저널 flush·스레드 정리: fsync 가 느려 쓰기 대기열이 밀려도 stop 이 넣은 단계를 모두 기록하고 스레드를 끝낸다.
- 복구 중 health UNSAFE_UNKNOWN: 재시작 뒤 재관측 전에는 노트북 연결·시계 교환이 정상이어도 RECOVERING 이고, 복원한 활성
  현장 경보는 첫 health 부터 목록에 있다(빈 목록 금지). 재관측 뒤 풀린다.
- 시계 차이 모름: 노트북이 시계 교환에 답하지 않으면 laptop_clock_offset 은 null, system_health 는 UNSAFE_UNKNOWN 이고, 사람이
  관측돼도 해소 확인을 하지 않는다(UNVERIFIABLE). 현장 경보는 계속 나간다.
- 기동 순서: 복구가 I0·I1·쓰기 스레드·체크포인트보다 먼저다. 복원 전에는 활성 현장 경보 필드를 만들지 않는다.
- 구성 묶음 없음: 위험 판정을 하지 않고 health 는 NO_ACTIVE_CONFIG(UNSAFE_UNKNOWN)다.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402
from test_i1_tls import make_cert  # noqa: E402

from services.tier0.service import Tier0Service  # noqa: E402
from services.tier0.settings import load_settings  # noqa: E402
from simai_ai.eventlog import EventJournal  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import SystemClock  # noqa: E402
from simai_ai.tracking.system_health import HealthReason  # noqa: E402


@pytest.fixture(scope="module")
def sim_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sim")
    return ts.run_sim(tmp, ts.scenario(60_000))


def _settings(tmp: Path, run: Path, *, anchor_ms: int, env: dict | None = None, bootstrap: bool = True):
    calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    if bootstrap and not (tmp / "boot").exists():
        ts.archive_bundle(tmp / "boot", calibration, "cfg-1", time.time_ns() // 1_000_000 - 1000)
    cfg = ts.write_config(tmp / "tier0.json", run_dir=run, state_dir=tmp / "state", anchor_wall_ms=anchor_ms,
                          archive=tmp / "boot", version="cfg-1" if bootstrap else "none")
    if not bootstrap:
        doc = json.loads(cfg.read_text(encoding="utf-8"))
        doc.pop("bootstrap")
        cfg.write_text(json.dumps(doc), encoding="utf-8")
    return load_settings(cfg, env=env or {})


def _now() -> int:
    return time.time_ns() // 1_000_000


def _r1(service: Tier0Service):
    return [s for s in service.tracker.active() if s.rule_id == "R1"]


def _tier0_threads() -> list[str]:
    prefixes = ("tier0-", "event-journal-writer", "tier0-checkpointer", "i0-control", "i1-")
    return [t.name for t in threading.enumerate() if t.name.startswith(prefixes) and t.is_alive()]


def test_stop_flushes_slow_journal_and_ends_threads(tmp_path, sim_run) -> None:
    settings = _settings(tmp_path, sim_run, anchor_ms=_now() + 300)
    slow = threading.Event()

    def fsync(fd: int) -> None:
        if slow.is_set():
            time.sleep(1.5)  # Network Volume fsync 지연 흉내: 쓰기 대기열이 밀린다

    service = Tier0Service(settings, journal_fsync=fsync)
    service.start()
    try:
        assert settings.live_marker.exists()
        assert ts.wait_until(lambda: bool(_r1(service)), timeout=20)
        slow.set()
        assert ts.wait_until(lambda: service.writer.backlog() >= 2, timeout=20)
    finally:
        produced_before = service.judge.stats.transitions
        assert service.stop(timeout_s=30) is True
    produced = service.judge.stats.transitions
    assert produced >= produced_before
    with EventJournal.open(settings.journal_dir, clock=SystemClock(), readonly=True) as journal:
        entries = journal.read(0)
    assert len(entries) == len(service.recovery.restore_entries) + produced  # 넣은 단계를 하나도 잃지 않았다
    assert not settings.live_marker.exists()
    assert service.events.index("judge_stopped") < service.events.index("links_closed") < service.events.index(
        "journal_flushed:0") < service.events.index("final_checkpoint") < service.events.index("stopped")
    assert ts.wait_until(lambda: not _tier0_threads(), timeout=5), _tier0_threads()
    # 마지막 체크포인트가 저널 끝까지 반영했다(다음 시작의 재적용 구간이 없다)
    assert service.checkpointer.last_checkpoint.last_applied_journal_offset == entries[-1].journal_offset


@pytest.fixture
def control(tmp_path):
    cert, key = make_cert(tmp_path)
    port = ts.free_port()
    env = ts.tier0_env(cert=cert, key=key, control_port=port, ec2_url=None)
    return {"cert": cert, "env": env, "port": port}


def test_recovery_health_is_unsafe_until_reobserved_and_restored_alerts_listed(tmp_path, sim_run, control) -> None:
    settings = _settings(tmp_path, sim_run, anchor_ms=_now() + 300, env=control["env"])
    first = Tier0Service(settings)
    laptop = ts.Laptop(control["port"], control["cert"]).start()
    first.start()
    try:
        assert ts.wait_until(lambda: bool(_r1(first)), timeout=20)
        [event] = _r1(first)
    finally:
        first.stop()
    conns = len(laptop.connections)
    # 재시작: 장면을 2.5 s 뒤에 다시 시작한다(그동안 프레임이 없다 = 재관측 전)
    settings2 = _settings(tmp_path, sim_run, anchor_ms=_now() + 2500, env=control["env"])
    second = Tier0Service(settings2)
    second.start()
    try:
        assert second.recovery is not None and event.event_id in {s.event_id for s in second.recovery.restored}
        assert ts.wait_until(lambda: len(laptop.connections) > conns and laptop.connected.is_set(), timeout=10)
        # 노트북 하트비트·시계 교환이 정상이 될 때까지 기다린다(그래도 재관측 전이면 RECOVERING)
        assert ts.wait_until(lambda: second.health.last is not None and second.health.last.gateway_ok
                             and second.clock_sync.capture_clock(True) is not None, timeout=10)
        early = laptop.messages(-1, "health")
        assert early and all(h["payload"]["system_health"] == "UNSAFE_UNKNOWN" for h in early)
        assert HealthReason.RECOVERING in second.health.evaluate().report.reasons
        for h in early:  # 복원한 활성 현장 경보는 첫 health 부터 목록에 있다
            assert event.event_id in {a["event_id"] for a in h["payload"]["active_site_alerts"]}
            assert schema_errors(h, "health") == []
        # 프레임이 다시 오면(재관측) 풀린다
        assert ts.wait_until(lambda: HealthReason.RECOVERING not in second.health.evaluate().report.reasons, timeout=15)
        assert second.gate.released
        restored = second.tracker.get(event.event_id)
        assert restored is not None and str(restored.verification) == "UNVERIFIABLE"  # 자동 해소 없음
    finally:
        second.stop()
        laptop.close()


def test_unknown_clock_offset_keeps_health_unsafe_and_blocks_resolution(tmp_path, sim_run, control) -> None:
    settings = _settings(tmp_path, sim_run, anchor_ms=_now() + 300, env=control["env"])
    service = Tier0Service(settings)
    laptop = ts.Laptop(control["port"], control["cert"], answer_clock=False).start()
    service.start()
    try:
        assert ts.wait_until(lambda: bool(_r1(service)) and bool(laptop.all_messages("local_alert")), timeout=20)
        mark = len(laptop.all_messages())
        assert ts.wait_until(lambda: len([m for m in laptop.all_messages()[mark:] if m["msg_type"] == "health"]) >= 2,
                             timeout=10)
        [event] = _r1(service)
        snap = service.health.evaluate()
        assert snap.gateway_ok and snap.capture_clock is None
        assert HealthReason.CLOCK_OFFSET_UNKNOWN in snap.report.reasons
        for h in laptop.all_messages("health")[1:]:
            assert h["payload"]["system_health"] == "UNSAFE_UNKNOWN" and h["payload"]["laptop_clock_offset"] is None
            # 계약 health 2.0.0: T_lag 를 모르면 null 이다. 처리 정체 하한·0 을 참값처럼 싣지 않는다(-179)
            assert h["payload"]["pipeline_lag_ms"] is None
            assert h["schema_version"] == "2.1.0"  # I0 봉투도 health 2.1.0(i0_control 이 HEALTH_SCHEMA_VERSION 을 씀)
            assert all(c["sync_valid"] is None for c in h["payload"]["cameras"])  # 동기화 측정 근거 없음
            assert schema_errors(h, "health") == []
        # 운영 경고는 정리 주기가 상태를 읽은 뒤부터 싣는다(이 장면은 켜진 경고 없음)
        assert ts.wait_until(lambda: service.health.payload().get("tier0_warnings") == [], timeout=5)
        for h in [m for m in laptop.all_messages()[mark:] if m["msg_type"] == "health"]:  # 현장 경보 목록은 계속 나간다
            assert event.event_id in {a["event_id"] for a in h["payload"]["active_site_alerts"]}
        # 사람이 관측되고 있어도 시계 차이를 모르면 해소 확인을 하지 않는다
        time.sleep(1.0)
        assert str(service.tracker.get(event.event_id).verification) == "UNVERIFIABLE"
    finally:
        service.stop()
        laptop.close()


def test_startup_recovers_before_any_link_and_no_alert_fields_before_restore(tmp_path, sim_run, control) -> None:
    settings = _settings(tmp_path, sim_run, anchor_ms=_now() + 60_000, env=control["env"])
    service = Tier0Service(settings)
    service.start()
    try:
        events = service.events
        rec = next(i for i, e in enumerate(events) if e.startswith("recovered:"))
        for later in ("journal_writer", "checkpointer", "i0_control", "judge"):
            assert rec < events.index(later)
        assert next(i for i, e in enumerate(events) if e.startswith("config:")) < events.index("journal_writer")
        assert "active_site_alerts" in service._site_fields()
        service._restored = False  # 복원 전 상태라면
        assert service._site_fields() == {}
        assert service.resync.control_connected() is None and service.resync.tick() is None
    finally:
        service._restored = True
        service.stop()


def test_no_active_config_means_no_judgment_and_unsafe_health(tmp_path, sim_run) -> None:
    settings = _settings(tmp_path, sim_run, anchor_ms=_now() + 300, bootstrap=False)
    service = Tier0Service(settings)
    service.start()
    try:
        assert ts.wait_until(lambda: service.judge.stats.perceived >= 10, timeout=20)
        assert service.judge.stats.evaluated == 0 and service.judge.stats.no_config_ticks >= 10
        report = service.health.evaluate().report
        assert HealthReason.NO_ACTIVE_CONFIG in report.reasons and str(report.system_health) == "UNSAFE_UNKNOWN"
        payload = service.health.payload()
        assert "config_version" not in payload and payload["pipeline_lag_ms"] >= 0
    finally:
        service.stop()


# --- 리뷰 F3: 디스크(fsync) 정체 중 종료·기동 실패 정리는 제한 시간 안에 끝나고 실패를 남긴다 -------------------


class _StuckDisk:
    """저널 fsync 를 armed 동안 멈춘다(Network Volume 정체 흉내). release 로 푼다."""

    def __init__(self) -> None:
        self.armed = threading.Event()
        self.blocked = threading.Event()
        self.released = threading.Event()

    def __call__(self, fd: int) -> None:
        if self.armed.is_set() and not self.released.is_set():
            self.blocked.set()
            self.released.wait(30)


def _stuck_service(tmp_path, sim_run):
    import uuid

    from test_tier0_judge import _ack

    settings = _settings(tmp_path, sim_run, anchor_ms=_now() + 300)
    disk = _StuckDisk()
    service = Tier0Service(settings, journal_fsync=disk)
    service.start()
    assert ts.wait_until(lambda: bool(_r1(service)), timeout=20)
    assert ts.wait_until(lambda: service.writer.backlog() == 0, timeout=10)
    [event] = _r1(service)
    disk.armed.set()
    service.judge.acknowledge(_ack(event.event_id, str(uuid.uuid4()), event.event_sequence))
    assert disk.blocked.wait(5)  # 쓰기 스레드가 fsync 에서 멈췄다
    return settings, service, disk


def test_stop_returns_within_deadline_when_disk_is_stuck_and_keeps_marker(tmp_path, sim_run) -> None:
    """리뷰 부록 C: fsync 가 끝나지 않아도 stop(timeout) 은 제한 시간 안에 실패로 끝나고 표시 파일에 실패를 남긴다."""
    settings, service, disk = _stuck_service(tmp_path, sim_run)
    result: list[bool] = []
    started = time.monotonic()
    stopper = threading.Thread(target=lambda: result.append(service.stop(timeout_s=0.5)), daemon=True)
    stopper.start()
    try:
        stopper.join(4.0)
        assert not stopper.is_alive(), "stop 이 디스크 정체에 붙잡혔다"
        assert result == [False] and time.monotonic() - started < 4.0
        marker = json.loads(settings.live_marker.read_text(encoding="utf-8"))
        assert any(f.startswith("journal_backlog") for f in marker["shutdown_failed"])
        assert "final_checkpoint" not in service.events  # 저널이 다 기록되지 않았으면 마지막 체크포인트를 쓰지 않는다
    finally:
        disk.released.set()  # 디스크 정체 해제: 남은 기록은 늦게라도 끝난다(다음 시작이 저널로 복구)
    assert ts.wait_until(lambda: service.writer.backlog() == 0, timeout=10)


def test_disk_released_before_deadline_stops_cleanly(tmp_path, sim_run) -> None:
    """종료 중 디스크 정체가 제한 시간 안에 풀리면 넣은 단계를 모두 기록하고 정상 종료한다."""
    settings, service, disk = _stuck_service(tmp_path, sim_run)
    threading.Timer(0.5, disk.released.set).start()
    assert service.stop(timeout_s=10) is True
    assert not settings.live_marker.exists() and "final_checkpoint" in service.events
    with EventJournal.open(settings.journal_dir, clock=SystemClock(), readonly=True) as journal:
        assert any(e.meta.get("ack_id") for e in journal.read(0))


def test_abort_start_does_not_hang_on_stuck_disk(tmp_path, sim_run) -> None:
    """기동 실패 중 정체: 정리(_abort_start)도 같은 제한 시간 원칙을 따른다(drain_s 안에 끝난다)."""
    settings, service, disk = _stuck_service(tmp_path, sim_run)
    service.settings = replace(settings, drain_s=0.5)
    started = time.monotonic()
    aborter = threading.Thread(target=service._abort_start, daemon=True)
    aborter.start()
    try:
        aborter.join(4.0)
        assert not aborter.is_alive() and time.monotonic() - started < 4.0
    finally:
        disk.released.set()
