"""Tier 0 기존 단계 계측에 더한 것(S15P31A101-662): 판정에 쓰지 않는 p50, 루프 깨어남 지연, 스레드 CPU, I1 직렬화.

- StageTiming.summary 에 p50 만 더한다(mean·p95·max 의미 그대로).
- ThreadCpuSampler 는 /proc/self/task 꼴 디렉터리에서 스레드별 CPU% 를 잰다(Linux). 없으면 threads 는 모름(None).
- 새 계측 지점: LIVE 수신 처리(frame_receive), I1 직렬화·송신(world_state·나머지), 판정·검출·정리 루프 깨어남 지연.
- 서비스: 기존 30초 `[tier0] stage_timing` 줄과 표시 파일 status 에 위 값을 싣는다. I1 health 는 그대로다.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
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

from services.tier0 import live_input as live_input_module  # noqa: E402
from services.tier0 import service as service_module  # noqa: E402
from services.tier0.i1_client import I1Client  # noqa: E402
from services.tier0.live_input import I0Bridge  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
from services.tier0.settings import load_settings  # noqa: E402
from services.tier0.thread_cpu import ThreadCpuSampler  # noqa: E402
from simai_ai.tracking.stage_timing import StageTiming  # noqa: E402


def test_summary_adds_p50_and_keeps_existing_keys() -> None:
    timing = StageTiming(capacity=256)
    for i in range(100):
        timing.record("judge_tick", 2, float(i + 1))
    wall = timing.summary()["judge_tick"]["2"]["wall_ms"]
    assert wall == {"mean": 50.5, "p50": 50.0, "p95": 95.0, "max": 100.0}


def _write_stat(task: Path, tid: int, comm: str, utime: int, stime: int) -> None:
    d = task / str(tid)
    d.mkdir(parents=True, exist_ok=True)
    # 필드 1 pid, 2 (comm), 3 state, 4..13, 14 utime, 15 stime
    rest = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 5
    (d / "stat").write_text(f"{tid} ({comm}) " + " ".join(rest), encoding="utf-8")


def test_thread_cpu_sampler_reads_proc_task_and_groups_names(tmp_path) -> None:
    task = tmp_path / "task"
    now, cpu = [100.0], [10.0]
    threads = [SimpleNamespace(native_id=11, name="tier0-judge"), SimpleNamespace(native_id=12, name="tier0-event-s3_0"),
               SimpleNamespace(native_id=13, name="tier0-event-s3_1")]
    _write_stat(task, 11, "python", 100, 0)
    _write_stat(task, 12, "python", 0, 0)
    _write_stat(task, 13, "python", 0, 0)
    _write_stat(task, 14, "av:h264:df (x)", 0, 0)
    sampler = ThreadCpuSampler(task_dir=task, clock=lambda: now[0], process_cpu_s=lambda: cpu[0],
                               threads=lambda: threads, ticks_per_s=100.0)
    assert sampler.sample() is None  # 첫 표본은 기준
    now[0], cpu[0] = 110.0, 25.0  # 10초 동안 프로세스 CPU 15초 = 150%
    _write_stat(task, 11, "python", 600, 100)  # +600 tick = 6 s → 60%
    _write_stat(task, 12, "python", 50, 0)  # 5%
    _write_stat(task, 13, "python", 30, 20)  # 5% → 같은 묶음 10%
    _write_stat(task, 14, "av:h264:df (x)", 200, 0)  # 이름 모르는 C 스레드 20%
    _write_stat(task, 15, "python", 10, 0)  # 창 안에 생긴 스레드: 1%
    out = sampler.sample()
    assert out["window_s"] == 10.0 and out["process_pct"] == 150.0
    assert out["threads"] == {"tier0-judge": 60.0, "native:av:h264:df (x)": 20.0, "tier0-event-s3": 10.0,
                              "native:python": 1.0}


def test_thread_cpu_sampler_without_proc_reports_process_only(tmp_path) -> None:
    now, cpu = [0.0], [0.0]
    sampler = ThreadCpuSampler(task_dir=tmp_path / "none", clock=lambda: now[0], process_cpu_s=lambda: cpu[0])
    sampler.sample()
    now[0], cpu[0] = 2.0, 1.0
    assert sampler.sample() == {"window_s": 2.0, "process_pct": 50.0, "threads": None}


def test_live_bridge_records_receive_separately_from_decode(monkeypatch) -> None:
    received = []
    ingest = SimpleNamespace(camera_ids=("cam1",), receive=lambda m: received.append(m))
    monkeypatch.setattr(live_input_module, "frame_from_contract", lambda header, data: ("msg", header["camera_id"]))
    bridge = I0Bridge(ingest)
    bridge.attach_clips(lambda m: time.sleep(0.002))
    for _ in range(3):
        bridge.on_frame({"camera_id": "cam1"}, b"")
    out = bridge.timing.summary()
    assert out["frame_receive"]["unknown"]["count"] == 3
    assert out["frame_receive"]["unknown"]["wall_ms"]["p50"] >= 1.5  # 클립 링 버퍼 포함
    assert out["frame_receive_decode"]["unknown"]["count"] == 3  # 기존 디코딩 구간 이름·의미 그대로
    assert len(received) == 3


def test_i1_send_records_serialize_and_send_per_kind() -> None:
    sent = []

    class Conn:
        def send(self, text: str) -> None:
            sent.append(text)

    client = I1Client(SimpleNamespace(connect=lambda: None), on_message=lambda raw: None)
    client._send(Conn(), {"msg_type": "world_state", "payload": {"tracks": []}})
    client._send(Conn(), {"msg_type": "health", "payload": {}})
    out = client.timing.summary()
    assert {"i1_serialize.world_state", "i1_send.world_state", "i1_serialize.other", "i1_send.other"} == set(out)
    assert all(v["unknown"]["count"] == 1 for v in out.values()) and len(sent) == 2


# --------------------------------------------------------------------------- 서비스 조립(SIM, 실제 스레드)


@pytest.fixture(scope="module")
def sim_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sim")
    return ts.run_sim(tmp, ts.scenario(20_000))


def _settings(tmp: Path, run: Path):
    calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    ts.archive_bundle(tmp / "boot", calibration, "cfg-1", time.time_ns() // 1_000_000 - 1000)
    cfg = ts.write_config(tmp / "tier0.json", run_dir=run, state_dir=tmp / "state",
                          anchor_wall_ms=time.time_ns() // 1_000_000 + 300, archive=tmp / "boot", version="cfg-1")
    return load_settings(cfg, env={})


def test_service_stage_timing_line_and_status_carry_new_fields(tmp_path, sim_run, monkeypatch, caplog) -> None:
    monkeypatch.setattr(service_module, "STAGE_TIMING_EVERY_S", 0.3)
    caplog.set_level(logging.INFO, logger="services.tier0")
    service = Tier0Service(_settings(tmp_path, sim_run))
    service.start()
    try:
        assert ts.wait_until(lambda: service.judge.stats.perceived >= 5, timeout=20)
        assert ts.wait_until(lambda: service.thread_cpu is not None, timeout=10)
        status = service.status()
    finally:
        assert service.stop(timeout_s=30) is True
    assert "judge_wake_lag" in status["loop_timing"] and "housekeeping_wake_lag" in status["loop_timing"]
    assert status["i1_timing"] is None  # SIM 조립: I1 없음(모름은 None)
    assert status["thread_cpu"]["process_pct"] is not None
    # 기존 키는 그대로이고 p50 이 더해졌다
    tick = next(iter(status["stage_timing"]["judge_tick"].values()))["wall_ms"]
    assert tick["p95"] >= tick["p50"] >= 0
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[tier0] stage_timing ")]
    doc = json.loads(lines[-1].split(" ", 2)[2])
    assert {"judge", "roi_filtered", "detector", "input", "reid"} <= set(doc)  # 기존 묶음
    assert {"frame_age", "i1", "loops", "thread_cpu"} <= set(doc)  # -662 추가
    assert "judge_wake_lag" in doc["loops"] and doc["frame_age"] is None and doc["i1"] is None


def test_wait_measured_records_wake_lag_and_stops_without_recording(tmp_path, sim_run) -> None:
    service = Tier0Service(_settings(tmp_path, sim_run))  # 시작하지 않는다: 쉬기 도우미만 본다
    assert service._wait_measured("judge_wake_lag", 0.001) is False
    assert service.loop_timing.summary()["judge_wake_lag"]["unknown"]["count"] == 1
    service._stop.set()
    assert service._wait_measured("judge_wake_lag", 0.5) is True
    assert service.loop_timing.summary()["judge_wake_lag"]["unknown"]["count"] == 1
    assert threading.current_thread().is_alive()
