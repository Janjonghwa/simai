"""아바타 처리 부하(p95 지속 초과·히스테리시스)와 분석 워커 끝단 지연 접수 기준을 구분한다."""

import json
import os
import threading

import pytest

from simai_ai.gpu.admission import (
    AVATAR_EXTREME_LAG_MS,
    AVATAR_OVER_BUDGET_HOLD_S,
    AdmissionGate,
    AdmissionPolicy,
    AvatarAdmissionGate,
)
from simai_ai.gpu.processing import ProcessingWindow


class Clock:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def advance(self, dt):
        self.t += float(dt)


def sample(p95=10.0, *, max_ms=None, samples=20, unknown=0):
    max_val = p95 if max_ms is None else max_ms
    return {"samples": samples, "unknown": unknown, "lag_ms_p95": p95, "lag_ms_max": max_val}


def legacy_sample(max_ms=10.0, *, samples=20, unknown=0):
    return {"samples": samples, "unknown": unknown, "lag_ms_max": max_ms}


def write_marker(path, *, processing, reserve="OK", phase="running", mtime=None):
    path.write_text(json.dumps({"gpu_priority": {
        "phase": phase, "reserve": {"state": reserve},
        "load": legacy_sample(5000), "processing": processing}}), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def gate(path, cls=AvatarAdmissionGate, *, clock=None, resume_after_s=10.0, **kw):
    if clock is None:
        return cls(path, AdmissionPolicy(resume_after_s=resume_after_s),
                   wall_s=lambda: path.stat().st_mtime, monotonic=lambda: 0.0, **kw)
    return cls(path, AdmissionPolicy(resume_after_s=resume_after_s),
               wall_s=lambda: clock.t, monotonic=lambda: clock.t, **kw)


def test_high_capture_latency_does_not_block_avatar_but_still_blocks_vlm(tmp_path):
    path = tmp_path / "tier0.live"
    write_marker(path, processing={"judge": sample(100, max_ms=260), "detect": sample(150, max_ms=330),
                                   "detect_required": True})
    assert gate(path, resume_after_s=0).check().admit
    assert not gate(path, AdmissionGate, resume_after_s=0).check().admit


def test_single_spike_in_max_or_short_p95_spike_passes(tmp_path):
    """단발 튐 통과: (1) 창 최댓값이 예산을 넘어도 p95 ≤ 예산이면 통과, (2) p95 > 예산도 T_block 미만이면 통과."""
    path = tmp_path / "tier0.live"
    clock = Clock()
    g = gate(path, clock=clock)
    # 11-08 3명 수준: judge p95 97.4ms ≤ 100ms, 창 최댓값 262.3ms > 100ms
    for _ in range(10):
        write_marker(path, processing={"judge": sample(97.4, max_ms=262.3),
                                       "detect": sample(140.0, max_ms=310.0),
                                       "detect_required": True}, mtime=clock.t)
        adm = g.check()
        assert adm.admit and adm.code == "ok"
        clock.advance(5.0)
    # 한 창(5초 < T_block 10초) 동안 judge p95 가 180ms 로 튀었다가 다음 창에 80ms 로 복귀
    write_marker(path, processing={"judge": sample(180.0, max_ms=300.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    clock.advance(5.0)
    write_marker(path, processing={"judge": sample(80.0, max_ms=220.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    # 연속이 아닌 단발 튐이 다시 와도 누적되지 않고 통과
    clock.advance(5.0)
    write_marker(path, processing={"judge": sample(160.0, max_ms=280.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    clock.advance(4.9)
    write_marker(path, processing={"judge": sample(160.0, max_ms=280.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    assert g.stats()["resets"] == {}


@pytest.mark.parametrize("stage,proc", [
    ("judge", {"judge": sample(233.4, max_ms=555.4), "detect_required": False}),
    ("detect", {"judge": sample(80.0, max_ms=190.0), "detect": sample(203.0, max_ms=317.2), "detect_required": True}),
])
def test_sustained_p95_over_budget_blocks_after_t_block(tmp_path, stage, proc):
    """지속 초과 차단: p95 > 예산이 연속 T_block(10초) 이어지면 차단한다."""
    path = tmp_path / "tier0.live"
    clock = Clock()
    g = gate(path, clock=clock)
    write_marker(path, processing=proc, mtime=clock.t)
    assert g.check().admit  # t=0s: 초과 시작
    clock.advance(5.0)
    write_marker(path, processing=proc, mtime=clock.t)
    assert g.check().admit  # t=5s < 10s
    clock.advance(4.9)
    write_marker(path, processing=proc, mtime=clock.t)
    assert g.check().admit  # t=9.9s < 10s
    clock.advance(0.1)
    write_marker(path, processing=proc, mtime=clock.t)
    adm = g.check()  # t=10.0s >= T_block
    assert not adm.admit and adm.code == "over_budget" and stage in adm.reason
    assert g.stats()["resets"] == {"over_budget": 1}
    # 과부하가 계속되는 동안 계속 차단
    clock.advance(5.0)
    write_marker(path, processing=proc, mtime=clock.t)
    assert not g.check().admit


def test_resume_hysteresis_requires_continuous_under_budget(tmp_path):
    """재개 히스테리시스: 차단 뒤에는 p95 ≤ 예산이 연속 T_resume(10초) 이어져야 다시 연다."""
    path = tmp_path / "tier0.live"
    clock = Clock()
    g = gate(path, clock=clock)
    over = {"judge": sample(233.4, max_ms=400.0), "detect_required": False}
    ok = {"judge": sample(85.0, max_ms=210.0), "detect_required": False}
    write_marker(path, processing=over, mtime=clock.t)
    assert g.check().admit
    clock.advance(AVATAR_OVER_BUDGET_HOLD_S)
    write_marker(path, processing=over, mtime=clock.t)
    assert not g.check().admit  # 차단됨

    # 회복 시작(t=10s)
    clock.advance(5.0)
    write_marker(path, processing=ok, mtime=clock.t)
    adm = g.check()
    assert not adm.admit and adm.code == "stabilizing"
    clock.advance(5.0)
    write_marker(path, processing=ok, mtime=clock.t)
    assert not g.check().admit  # 안정 대기 5/10s

    # 재개 전에 다시 p95 초과가 오면 안정 대기 시계가 초기화된다
    clock.advance(2.0)
    write_marker(path, processing=over, mtime=clock.t)
    adm = g.check()
    assert not adm.admit and adm.code == "over_budget"

    # 다시 정상 표본이 연속 10초 이어져야 재개
    clock.advance(3.0)
    write_marker(path, processing=ok, mtime=clock.t)
    assert g.check().code == "stabilizing"
    clock.advance(9.9)
    write_marker(path, processing=ok, mtime=clock.t)
    assert not g.check().admit
    clock.advance(0.1)
    write_marker(path, processing=ok, mtime=clock.t)
    adm = g.check()
    assert adm.admit and adm.code == "ok"
    assert g.stats()["opened"] == 2


@pytest.mark.parametrize("processing,expected_code", [
    (None, "lag_unknown"),
    ({}, "lag_unknown"),
    ({"judge": sample(), "detect_required": "false"}, "lag_unknown"),
    ({"judge": sample(), "detect_required": True}, "lag_unknown"),
    ({"judge": {"samples": 0, "unknown": 0, "lag_ms_p95": 10, "lag_ms_max": 10}, "detect_required": False}, "lag_unknown"),
    ({"judge": {**sample(), "unknown": 1}, "detect_required": False}, "lag_unknown"),
    ({"judge": sample(float("nan")), "detect_required": False}, "lag_unknown"),
    ({"judge": sample(-1), "detect_required": False}, "lag_unknown"),
    ({"judge": {**sample(10), "lag_ms_p95": None}, "detect_required": False}, "lag_unknown"),
    ({"judge": {**sample(10), "lag_ms_p95": "10"}, "detect_required": False}, "lag_unknown"),
    ({"judge": sample(20.0, max_ms=AVATAR_EXTREME_LAG_MS), "detect_required": False}, "over_budget"),
    ({"judge": sample(120.0, max_ms=200.0), "detect": sample(50.0, max_ms=AVATAR_EXTREME_LAG_MS),
      "detect_required": True}, "over_budget"),
    ({"judge": sample(120.0, max_ms=200.0), "detect": {**sample(), "unknown": 1},
      "detect_required": True}, "lag_unknown"),
])
def test_unknown_or_extreme_processing_stops_avatar_immediately(tmp_path, processing, expected_code):
    """모름·형식 오류·극단적 멈춤(창 최대 ≥ 1000ms)은 T_block 기다리지 않고 즉시 차단한다."""
    path = tmp_path / "tier0.live"
    clock = Clock()
    write_marker(path, processing=processing, mtime=clock.t)
    adm = gate(path, clock=clock).check()
    assert not adm.admit and adm.code == expected_code


def test_processing_does_not_override_gpu_reservation_or_stale_marker(tmp_path):
    """GPU 예약 실패·기동 중·표시 파일 만료는 즉시 차단하고, 해소 뒤 T_resume 대기를 거친다."""
    path = tmp_path / "tier0.live"
    clock = Clock()
    write_marker(path, processing={"judge": sample(), "detect_required": False}, reserve="LOST", mtime=clock.t)
    assert not gate(path, clock=clock).check().admit
    write_marker(path, processing={"judge": sample(), "detect_required": False}, phase="starting", mtime=clock.t)
    assert not gate(path, clock=clock).check().admit

    g = gate(path, clock=clock)
    write_marker(path, processing={"judge": sample(), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    clock.advance(16.0)  # 갱신 없음 → 만료
    adm = g.check()
    assert not adm.admit and adm.code == "marker_stale"
    # 새 표시 파일이 다시 써져도 T_resume(10초) 동안은 안정 대기
    write_marker(path, processing={"judge": sample(), "detect_required": False}, mtime=clock.t)
    assert g.check().code == "stabilizing"
    clock.advance(10.0)
    write_marker(path, processing={"judge": sample(), "detect_required": False}, mtime=clock.t)
    assert g.check().admit


def test_legacy_marker_without_p95_field_falls_back_to_max(tmp_path):
    """p95 필드 없는 옛 표시 파일 처리: lag_ms_max 를 보수적 상한으로 사용해 히스테리시스·극단값 규칙을 적용한다."""
    path = tmp_path / "tier0.live"
    clock = Clock()
    g = gate(path, clock=clock)
    write_marker(path, processing={"judge": legacy_sample(100.0), "detect": legacy_sample(150.0),
                                   "detect_required": True}, mtime=clock.t)
    assert g.check().admit
    # 옛 형식에서 100.1ms 단발 1창(5초)은 통과, 10초 연속이면 차단
    write_marker(path, processing={"judge": legacy_sample(100.1), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    clock.advance(5.0)
    write_marker(path, processing={"judge": legacy_sample(100.1), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    clock.advance(5.0)
    write_marker(path, processing={"judge": legacy_sample(100.1), "detect_required": False}, mtime=clock.t)
    adm = g.check()
    assert not adm.admit and adm.code == "over_budget" and "p95 없음" in adm.reason
    # 옛 형식에서도 극단값(>= 1000ms)은 즉시 차단
    g2 = gate(path, clock=clock)
    write_marker(path, processing={"judge": legacy_sample(1000.0), "detect_required": False}, mtime=clock.t)
    assert not g2.check().admit


def test_processing_window_measures_p95_samples_running_work_and_failed_work():
    """처리 창은 표본 수·p95·최댓값을 함께 산출하고, 진행 중 경과와 실패(unknown) 규칙을 유지한다."""
    clock = [0.]
    window = ProcessingWindow(100, monotonic=lambda: clock[0])
    # 100개 표본(64개 초과): 앞 5개는 200ms, 뒤 95개는 30ms → p95(95번째)=30.0ms, max=200.0ms
    for ms in ([200.0] * 5 + [30.0] * 95):
        def step(dt=ms / 1000.0):
            clock[0] += dt
        window.run(step)
    out = window.take()
    assert out["samples"] == 100 and out["unknown"] == 0
    assert out["lag_ms_p95"] == 30.0 and out["lag_ms_max"] == 200.0 and out["over_budget"] == 5

    clock[0] = 0.
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        release.wait(2)
    thread = threading.Thread(target=lambda: window.run(work))
    thread.start()
    try:
        assert entered.wait(1)
        clock[0] = .7
        snap = window.take()
        assert snap["samples"] == 1 and snap["lag_ms_max"] == 700 and snap["lag_ms_p95"] == 700
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
    done = window.take()
    assert done["samples"] == 1 and done["lag_ms_max"] == 700 and done["lag_ms_p95"] == 700
    assert window.take()["samples"] == 0
    with pytest.raises(ValueError):
        window.run(lambda: (_ for _ in ()).throw(ValueError()))
    failed = window.take()
    assert failed["unknown"] == 1 and failed["lag_ms_p95"] is None and failed["lag_ms_max"] is None

