"""아바타 처리 부하와 분석 워커 끝단 지연 접수 기준을 구분한다."""

import json
import threading

import pytest

from simai_ai.gpu.admission import AdmissionGate, AdmissionPolicy, AvatarAdmissionGate
from simai_ai.gpu.processing import ProcessingWindow


def sample(ms=10):
    return {"samples": 1, "unknown": 0, "lag_ms_max": ms}


def write_marker(path, *, processing, reserve="OK"):
    path.write_text(json.dumps({"gpu_priority": {
        "phase": "running", "reserve": {"state": reserve},
        "load": sample(5000), "processing": processing}}), encoding="utf-8")


def gate(path, cls=AvatarAdmissionGate):
    return cls(path, AdmissionPolicy(resume_after_s=0),
               wall_s=lambda: path.stat().st_mtime, monotonic=lambda: 0)


def test_high_capture_latency_does_not_block_avatar_but_still_blocks_vlm(tmp_path):
    path = tmp_path / "tier0.live"
    write_marker(path, processing={"judge": sample(100), "detect": sample(150), "detect_required": True})
    assert gate(path).check().admit
    assert not gate(path, AdmissionGate).check().admit


@pytest.mark.parametrize("processing", [
    None, {}, {"judge": sample(), "detect_required": "false"},
    {"judge": sample(100.1), "detect_required": False},
    {"judge": sample(), "detect": sample(150.1), "detect_required": True},
    {"judge": sample(), "detect_required": True},
    {"judge": {"samples": 0}, "detect_required": False},
    {"judge": {**sample(), "unknown": 1}, "detect_required": False},
    {"judge": sample(float("nan")), "detect_required": False},
    {"judge": sample(-1), "detect_required": False},
])
def test_busy_or_unknown_processing_stops_avatar(tmp_path, processing):
    path = tmp_path / "tier0.live"
    write_marker(path, processing=processing)
    assert not gate(path).check().admit


def test_processing_does_not_override_gpu_reservation_or_stale_marker(tmp_path):
    path = tmp_path / "tier0.live"
    write_marker(path, processing={"judge": sample(), "detect_required": False}, reserve="LOST")
    assert not gate(path).check().admit
    write_marker(path, processing={"judge": sample(), "detect_required": False})
    admission = gate(path)
    admission._wall_s = lambda: path.stat().st_mtime + 16
    assert not admission.check().admit


def test_processing_window_measures_running_work_and_failed_work():
    clock = [0.]
    window = ProcessingWindow(100, monotonic=lambda: clock[0])
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        release.wait(2)
    thread = threading.Thread(target=lambda: window.run(work))
    thread.start()
    try:
        assert entered.wait(1)
        clock[0] = .7
        assert window.take()["lag_ms_max"] == 700
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
    assert window.take()["lag_ms_max"] == 700
    assert window.take()["samples"] == 0
    with pytest.raises(ValueError):
        window.run(lambda: (_ for _ in ()).throw(ValueError()))
    assert window.take()["unknown"] == 1
