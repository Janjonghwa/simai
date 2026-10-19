"""Tier 0 GPU 우선 보호의 조립 시험 (S15P31A101-60, 명세 3.2, 6.6, 15.1 G0, 부록 D D-G0-08). GPU 없이 SIM 입력으로 돈다.

- 기동 중 표시 파일: GPU 선확보 시점에 표시 파일이 phase=starting 으로 이미 있다(GPU 실험 큐·분석 워커가 기동 중에도 LIVE 로 본다).
  선확보 결과(가짜 torch OK)와 지연 표본이 표시 파일에 실리고, 실제 접수 문이 그것을 읽어 받는다. 정상 종료하면 지운다.
- 시계 차이 모름: 노트북이 시계 교환에 답하지 않으면 lag 를 모른다 → 지연 표본이 모두 unknown → 접수 문은 받지 않는다.
- 기동 실패: 이번에 만든 표시 파일은 지운다. 이전 실행이 남긴 표시 파일이 있었으면 start_failed 로 남긴다.
- 분석 워커 강제 종료(D-G0-08): 더미 분석 워커 프로세스(실제 Worker + 접수 문 + CPU·메모리 부하)가 Tier 0 표시 파일을 보고
  작업을 받다가 강제 종료돼도 Tier 0 판정 tick 이 끊기지 않고, 표시 파일·지연 표본도 계속 갱신된다.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402
from test_i1_tls import make_cert  # noqa: E402

import services.tier0.service as service_mod  # noqa: E402
from services.tier0.service import Tier0Service, Tier0StartError  # noqa: E402
from services.tier0.settings import load_settings  # noqa: E402
from simai_ai.gpu.admission import AdmissionGate, AdmissionPolicy  # noqa: E402
from simai_ai.gpu.reserve import MB  # noqa: E402

DUMMY = HERE / "dummy_worker.py"


@pytest.fixture(scope="module")
def sim_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sim")
    return ts.run_sim(tmp, ts.scenario(60_000))


@pytest.fixture(autouse=True)
def fast_marker(monkeypatch):
    # 표시 파일 갱신 5초 → 1.5초(시험 시간). 지연 표본(1초)보다 길어야 창마다 표본이 하나 이상 있다
    monkeypatch.setattr(service_mod, "MARKER_EVERY_S", 1.5)


def _now() -> int:
    return time.time_ns() // 1_000_000


def _settings(tmp: Path, run: Path, *, env: dict | None = None, reserve_mb: int = 0, calibration: Path | None = None):
    cal = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    ts.archive_bundle(tmp / "boot", cal, "cfg-1", _now() - 1000)
    cfg = ts.write_config(tmp / "tier0.json", run_dir=run, state_dir=tmp / "state", anchor_wall_ms=_now() + 300,
                          archive=tmp / "boot", version="cfg-1")
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    doc["gpu"] = {"reserve_mb": reserve_mb}
    if calibration is not None:
        doc["calibration_file"] = str(calibration)
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    return load_settings(cfg, env=env or {})


def _marker(settings) -> dict | None:
    try:
        return json.loads(settings.live_marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _gp(settings) -> dict:
    doc = _marker(settings) or {}
    return doc.get("gpu_priority") or {}


def _gate(settings, **kw) -> AdmissionGate:
    policy = AdmissionPolicy(resume_after_s=0.0, live_linger_s=0.0, **kw)
    return AdmissionGate(settings.live_marker, policy, wall_s=time.time, monotonic=time.monotonic)


class _Cuda:
    def __init__(self, t) -> None:
        self._t = t

    def is_available(self) -> bool:
        return True

    def memory_reserved(self, device: int = 0) -> int:
        return self._t.cached


class MarkerCheckingTorch:
    """선확보 순간에 표시 파일이 phase=starting 으로 이미 있는지 기록하는 가짜 torch."""

    uint8 = "uint8"

    def __init__(self, marker: Path) -> None:
        self.marker = marker
        self.cached = 0
        self.phases_seen: list[str | None] = []
        self.cuda = _Cuda(self)

    def empty(self, n: int, *, dtype: str, device: str) -> object:
        try:
            self.phases_seen.append(json.loads(self.marker.read_text(encoding="utf-8"))["gpu_priority"]["phase"])
        except (OSError, ValueError, KeyError):
            self.phases_seen.append(None)
        self.cached += n
        return object()


def test_marker_is_written_before_reservation_and_carries_load_for_gate(tmp_path, sim_run) -> None:
    settings = _settings(tmp_path, sim_run, reserve_mb=1024)
    torch = MarkerCheckingTorch(settings.live_marker)
    service = Tier0Service(settings, gpu_torch=torch)
    service.start()
    try:
        assert torch.phases_seen and set(torch.phases_seen) == {"starting"}  # 선확보 전에 기동 중 표시 파일
        assert ts.wait_until(lambda: (_gp(settings).get("load") or {}).get("samples", 0) > 0, timeout=10)
        gp = _gp(settings)
        assert gp["phase"] == "running"
        assert gp["reserve"]["state"] == "OK" and gp["reserve"]["reserved_mb"] == pytest.approx(1024)
        assert torch.cached == 1024 * MB
        gate = _gate(settings, lag_budget_ms=5000)
        assert ts.wait_until(lambda: gate.check().admit, timeout=10), gate.check()
        adm = gate.check()
        assert adm.live and adm.allowed_types(["static_reconstruct", "vlm_rationale"]) == ["vlm_rationale"]
    finally:
        assert service.stop(timeout_s=30)
    assert not settings.live_marker.exists()
    adm = gate.check()
    assert adm.live and not adm.admit  # 표시 파일이 없으면 기본은 모름(리뷰 R1)
    dev = AdmissionGate(settings.live_marker, AdmissionPolicy(resume_after_s=0.0, live_linger_s=0.0,
                                                              allow_without_marker=True),
                        wall_s=time.time, monotonic=time.monotonic)
    adm = dev.check()
    assert adm.admit and not adm.live  # 관제 없는 개발 시간 명시 모드에서만 LIVE 아님


def test_unknown_clock_offset_means_unknown_lag_and_worker_refuses(tmp_path, sim_run) -> None:
    cert, key = make_cert(tmp_path)
    port = ts.free_port()
    env = ts.tier0_env(cert=cert, key=key, control_port=port, ec2_url=None)
    settings = _settings(tmp_path, sim_run, env=env)
    service = Tier0Service(settings)
    laptop = ts.Laptop(port, cert, answer_clock=False).start()
    service.start()
    try:
        assert ts.wait_until(lambda: (_gp(settings).get("load") or {}).get("samples", 0) >= 1, timeout=15)
        load = _gp(settings)["load"]
        assert load["unknown"] == load["samples"] and load["lag_ms_max"] is None
        assert _gp(settings)["reserve"]["state"] == "DISABLED"
        gate = _gate(settings, lag_budget_ms=600_000)  # 예산을 아무리 넉넉히 줘도 모르면 받지 않는다
        for _ in range(5):
            adm = gate.check()
            assert adm.live and not adm.admit, adm
            time.sleep(0.3)
        assert "모르" in adm.reason or "모름" in adm.reason  # unknown 표본 또는 표본 없음(부하로 창이 비면)
    finally:
        service.stop()
        laptop.close()


def test_start_failure_removes_new_marker_but_keeps_previous_unclean(tmp_path, sim_run) -> None:
    settings = _settings(tmp_path, sim_run, calibration=tmp_path / "missing-calibration.json")
    with pytest.raises(Tier0StartError):
        Tier0Service(settings).start()
    assert not settings.live_marker.exists()

    settings.live_marker.write_text('{"started": true}', encoding="utf-8")  # 이전 실행이 비정상 종료로 남김
    with pytest.raises(Tier0StartError):
        Tier0Service(settings).start()
    gp = _gp(settings)
    assert gp["phase"] == "start_failed"
    assert not _gate(settings).check().admit


def test_marker_write_failure_is_unknown_not_idle(tmp_path, sim_run) -> None:
    """리뷰 R1 재현: 표시 파일 쓰기만 실패하는 Tier 0 가 돌 때 새 접수 문은 LIVE(모름)로 보고 아무 유형도 받지 않는다.
    Tier 0 는 판정을 계속하고, 쓰기 실패를 status·로그와 health tier0_warnings LIVE_MARKER_WRITE_FAILED(2.1.0, -197)로 알린다.
    쓰기가 다시 되면 경고가 빠진다."""
    settings = _settings(tmp_path, sim_run)
    service = service_mod.Tier0Service(settings)
    original = service_mod._atomic_write
    failing = {"on": True}

    def failed_marker(path, data):
        if failing["on"] and path == settings.live_marker:
            raise PermissionError("injected marker write failure")
        return original(path, data)

    def codes() -> list[str] | None:
        warnings = service.health.payload().get("tier0_warnings")
        return None if warnings is None else [w["code"] for w in warnings]

    with patch.object(service_mod, "_atomic_write", failed_marker):
        service.start()
        try:
            gate = AdmissionGate(settings.live_marker, AdmissionPolicy(), wall_s=time.time, monotonic=time.monotonic)
            adm = gate.check()
            assert service.started and not settings.live_marker.exists()
            assert adm.live and not adm.admit
            assert adm.allowed_types(["static_reconstruct", "vlm_rationale"]) == []
            ticks = service.judge.stats.ticks
            assert ts.wait_until(lambda: service.judge.stats.ticks > ticks + 10, timeout=10)  # 판정은 계속
            assert service.marker_error is not None and "PermissionError" in service.marker_error
            assert service.status()["marker_error"] == service.marker_error
            assert ts.wait_until(lambda: codes() == ["LIVE_MARKER_WRITE_FAILED"], timeout=10)
            failing["on"] = False  # 볼륨이 돌아왔다
            assert ts.wait_until(lambda: settings.live_marker.exists() and codes() == [], timeout=10)
            assert service.marker_error is None
            ticks = service.judge.stats.ticks
            assert ts.wait_until(lambda: service.judge.stats.ticks > ticks + 5, timeout=10)
        finally:
            assert service.stop(timeout_s=30)
    assert service_mod._atomic_write is original


def _ticks_monitor(service: Tier0Service, stop: threading.Event, out: list[tuple[float, int]]) -> None:
    while not stop.is_set():
        out.append((time.monotonic(), service.judge.stats.ticks))
        time.sleep(0.05)


def test_killing_analysis_worker_does_not_interrupt_tier0(tmp_path, sim_run) -> None:
    settings = _settings(tmp_path, sim_run)
    service = Tier0Service(settings)
    service.start()
    samples: list[tuple[float, int]] = []
    stop = threading.Event()
    mon = threading.Thread(target=_ticks_monitor, args=(service, stop, samples), daemon=True)
    proc = None
    try:
        assert ts.wait_until(lambda: (_gp(settings).get("load") or {}).get("samples", 0) > 0, timeout=10)
        mon.start()
        # 지연 예산은 넉넉히(노트북 CPU). 이 시험의 대상은 강제 종료 뒤 판정 지속이다(지연 실측은 Pod 큐)
        proc = subprocess.Popen([sys.executable, str(DUMMY), str(settings.live_marker), "5000", "150", "64"],
                                cwd=AI_DIR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        assert proc.stdout.readline().strip() == "READY"
        done = 0
        end = time.monotonic() + 20
        while done < 3 and time.monotonic() < end:
            if proc.stdout.readline().strip() == "DONE":
                done += 1
        assert done >= 3, "더미 분석 워커가 접수 문을 통과해 작업을 받아야 한다"
        errors_before = service.judge.stats.errors
        marker_before = settings.live_marker.stat().st_mtime
        t_kill = time.monotonic()
        proc.kill()  # 강제 종료(SIGKILL / TerminateProcess): 정리 기회 없음
        proc.wait(timeout=10)
        time.sleep(3.0)
        stop.set()
        mon.join(timeout=2)
        after = [(t, n) for t, n in samples if t >= t_kill - 1.0]
        assert after[-1][1] - after[0][1] >= 25  # 10 Hz 판정이 4초 동안 계속(여유를 둔 하한)
        stalls = []
        last_t, last_n = after[0]
        for t, n in after[1:]:
            if n > last_n:
                stalls.append(t - last_t)
                last_t, last_n = t, n
        stalls.append(after[-1][0] - last_t)
        assert max(stalls) < 1.0, f"판정 tick 이 {max(stalls):.2f}s 멈췄다"
        assert service.judge.stats.errors == errors_before
        assert ts.wait_until(lambda: settings.live_marker.stat().st_mtime > marker_before, timeout=5)
        load = _gp(settings)["load"]
        assert load["samples"] > 0 and load["unknown"] == 0
        alive = {t.name for t in service._threads if t.is_alive()}
        assert {"tier0-judge", "tier0-housekeeping"} <= alive
    finally:
        stop.set()
        if proc is not None and proc.poll() is None:
            proc.kill()
        assert service.stop(timeout_s=30)


def test_pod_probe_watch_reads_live_marker(tmp_path, sim_run) -> None:
    """-183 큐에 넘기는 측정 도구(tier0_priority_probe watch)가 실제 Tier 0 표시 파일을 읽어 판정한다(노트북, SIM)."""
    probe = AI_DIR / "experiments" / "gpu_queue" / "tier0_priority_probe.py"
    settings = _settings(tmp_path, sim_run)
    service = Tier0Service(settings)
    service.start()
    try:
        assert ts.wait_until(lambda: (_gp(settings).get("load") or {}).get("samples", 0) > 0, timeout=10)
        out = tmp_path / "probe.json"
        r = subprocess.run([sys.executable, str(probe), "watch", "--marker", str(settings.live_marker), "--seconds", "5",
                            "--poll-s", "0.3", "--budget-ms", "5000", "--reserve-ok", "DISABLED", "--out", str(out)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        result = json.loads(out.read_text(encoding="utf-8"))
        assert result["passed"] and result["windows"] >= 2 and result["lag_samples"] >= 2
        assert result["judge_ticks_delta"] > 0 and result["judge_errors_delta"] == 0
        # 예산을 지연보다 작게 주면 불합격(판정이 실제로 값을 본다)
        r = subprocess.run([sys.executable, str(probe), "watch", "--marker", str(settings.live_marker), "--seconds", "3",
                            "--poll-s", "0.3", "--budget-ms", "0.001", "--reserve-ok", "DISABLED"],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert r.returncode == 1
    finally:
        assert service.stop(timeout_s=30)


class _Counts:
    def __init__(self) -> None:
        self.qwe = {"cam1": 0, "cam2": 0}
        self.broken = False

    def counts(self, cam):
        if self.broken:
            raise RuntimeError("집계 오류(가짜)")
        return {"skipped_by_reason": {"SUPERSEDED": 9, "QUEUE_WAIT_EXCEEDED": self.qwe[cam]}}


class _FakeLive:
    camera_ids = ("cam1", "cam2")

    def __init__(self) -> None:
        self.ingest = type("I", (), {})()
        self.ingest.log = _Counts()


class _Report:
    lag_ms = 120


class _Health:
    def evaluate(self):
        return type("S", (), {"report": _Report()})()


def test_live_queue_wait_exceeded_feeds_load_window_and_gate(tmp_path, sim_run) -> None:
    """-182 LIVE 입력의 QUEUE_WAIT_EXCEEDED 누계(카메라 합) 증가분이 지연 표본에 실리고 접수 문이 받지 않는다.
    SUPERSEDED(최신 프레임에 밀림)는 넣지 않는다. 누계를 읽지 못하면 지연을 모름으로 센다."""
    settings = _settings(tmp_path, sim_run)
    service = Tier0Service(settings)
    service.health = _Health()
    live = _FakeLive()
    service.live_input = live
    service._sample_load()  # 증가 없음
    assert service.load.take()["queue_wait_exceeded"] == 0
    live.ingest.log.qwe = {"cam1": 2, "cam2": 1}
    service._sample_load()
    out = service.load.take()
    assert out["queue_wait_exceeded"] == 3 and out["unknown"] == 0
    service._sample_load()  # 누계 그대로 → 증가분 0
    assert service.load.take()["queue_wait_exceeded"] == 0
    live.ingest.log.qwe = {"cam1": 4, "cam2": 1}
    service._sample_load()
    load = service.load.take()
    assert load["queue_wait_exceeded"] == 2
    marker = settings.live_marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"gpu_priority": {"phase": "running", "reserve": {"state": "OK"}, "load": load}}),
                      encoding="utf-8")
    adm = _gate(settings).check()
    assert not adm.admit and "버린 프레임 2" in adm.reason
    live.ingest.log.broken = True
    service._sample_load()
    assert service.load.take()["unknown"] == 1
