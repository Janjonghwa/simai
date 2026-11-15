"""아바타 처리 부하(p95 지속 초과·히스테리시스)와 분석 워커 끝단 지연 접수 기준을 구분한다."""

import json
import os
import threading

import pytest

from simai_ai.gpu.admission import (
    AVATAR_DETECT_BUDGET_MS,
    AVATAR_EXTREME_LAG_MS,
    AVATAR_JUDGE_BUDGET_MS,
    AVATAR_OVER_BUDGET_HOLD_S,
    AdmissionGate,
    AdmissionPolicy,
    AvatarAdmissionGate,
    avatar_budgets_from_env,
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
    # 17차 2~3명 수준: judge p95 179.3ms ≤ 200ms, 창 최댓값 262.3ms > 200ms
    for _ in range(10):
        write_marker(path, processing={"judge": sample(179.3, max_ms=262.3),
                                       "detect": sample(140.0, max_ms=310.0),
                                       "detect_required": True}, mtime=clock.t)
        adm = g.check()
        assert adm.admit and adm.code == "ok"
        clock.advance(5.0)
    # 한 창(5초 < T_block 10초) 동안 judge p95 가 360ms 로 튀었다가 다음 창에 80ms 로 복귀
    write_marker(path, processing={"judge": sample(360.0, max_ms=500.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    clock.advance(5.0)
    write_marker(path, processing={"judge": sample(80.0, max_ms=220.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    # 연속이 아닌 단발 튐이 다시 와도 누적되지 않고 통과
    clock.advance(5.0)
    write_marker(path, processing={"judge": sample(320.0, max_ms=480.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    clock.advance(4.9)
    write_marker(path, processing={"judge": sample(320.0, max_ms=480.0), "detect_required": False}, mtime=clock.t)
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
    write_marker(path, processing={"judge": legacy_sample(200.0), "detect": legacy_sample(150.0),
                                   "detect_required": True}, mtime=clock.t)
    assert g.check().admit
    # 옛 형식에서 200.1ms 단발 1창(5초)은 통과, 10초 연속이면 차단
    write_marker(path, processing={"judge": legacy_sample(200.1), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    clock.advance(5.0)
    write_marker(path, processing={"judge": legacy_sample(200.1), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    clock.advance(5.0)
    write_marker(path, processing={"judge": legacy_sample(200.1), "detect_required": False}, mtime=clock.t)
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


def test_avatar_admission_gate_recommends_progressive_self_throttle_intervals(tmp_path):
    """감속 기준선(judge 100ms = 판정 주기 초과 시작점, detect 150ms) 대비 비율로 200->400->600->800ms 권고 주기를 계산한다.

    차단 예산(judge 200ms)이 아니라 기준선에 묶는다: 판정이 70ms 를 넘으면 차단 전에 먼저 물러난다(-717).
    """
    path = tmp_path / "tier0.live"
    clock = Clock()
    g = gate(path, clock=clock)

    # 1. judge p95 65ms (<=70%) -> 200ms (5 Hz)
    write_marker(path, processing={"judge": sample(65.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    assert g.recommended_interval_ms() == 200
    assert g.stats()["recommended_interval_ms"] == 200

    # 2. judge p95 80ms (70~85%) -> 400ms (2.5 Hz)
    write_marker(path, processing={"judge": sample(80.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    assert g.recommended_interval_ms() == 400

    # 3. detect p95 135ms / 150ms = 0.90 (85~100%) -> 600ms (1.67 Hz)
    write_marker(
        path,
        processing={"judge": sample(60.0), "detect": sample(135.0), "detect_required": True},
        mtime=clock.t,
    )
    assert g.check().admit
    assert g.recommended_interval_ms() == 600

    # 4. judge p95 115ms (기준선 초과, 차단 예산 200ms 이하) -> 800ms (1.25 Hz)
    write_marker(path, processing={"judge": sample(115.0), "detect_required": False}, mtime=clock.t)
    assert g.check().admit
    assert g.recommended_interval_ms() == 800
    # 5. 17차 수준 judge p95 179ms: 열려 있지만 가장 느린 주기로 물러나 있다
    write_marker(path, processing={"judge": sample(179.3), "detect_required": False}, mtime=clock.t)
    adm = g.check()
    assert adm.admit and adm.code == "ok" and g.recommended_interval_ms() == 800




# 17차(H100 SXM, 14차 녹화 재생, 2~3명) 아바타 로그의 5초 창 judge p95. 이 동안 아바타 추론은 0건이었다(S15P31A101-717)
SESSION17_JUDGE_P95 = (108.3, 104.3, 118.4, 197.6, 179.3)


def test_session17_two_to_three_people_keeps_avatar_open_but_old_100ms_budget_blocked(tmp_path):
    """판정 예산 200ms(= 600 − 융합 150 − 검출 150 − 주기 100): 17차 2~3명 창은 통과, 옛 100ms 는 10초 뒤 막았다."""
    path = tmp_path / "tier0.live"
    clock = Clock()
    new, old = gate(path, clock=clock), gate(path, clock=clock, judge_budget_ms=100)
    old_codes = []
    for p95 in SESSION17_JUDGE_P95 * 2:
        write_marker(path, processing={"judge": sample(p95, max_ms=p95 + 60.0), "detect": sample(130.0, max_ms=270.0),
                                       "detect_required": True}, mtime=clock.t)
        adm = new.check()
        assert adm.admit and adm.code == "ok", adm
        old_codes.append(old.check().code)
        clock.advance(5.0)
    assert old_codes[:2] == ["ok", "ok"] and set(old_codes[2:]) == {"over_budget"}
    assert new.stats()["resets"] == {}
    assert AVATAR_JUDGE_BUDGET_MS == 200 and AVATAR_DETECT_BUDGET_MS == 150


def test_reason_reports_every_stage_against_its_budget(tmp_path):
    """차단 사유에 판정·검출 둘의 p95/예산을 함께 남긴다(17차는 판정 사유만 보여 검출 여유를 몰랐다)."""
    path = tmp_path / "tier0.live"
    write_marker(path, processing={"judge": sample(250.0), "detect": sample(120.0), "detect_required": True})
    adm = gate(path, resume_after_s=0, over_budget_hold_s=0).check()
    assert not adm.admit and adm.code == "over_budget"
    assert "judge 처리 p95 250.0ms > 200ms" in adm.reason and "detect p95 120.0/150ms" in adm.reason
    write_marker(path, processing={"judge": sample(90.0), "detect": sample(120.0), "detect_required": True})
    adm = gate(path, resume_after_s=0).check()
    assert adm.admit and "judge p95 90.0/200ms·detect p95 120.0/150ms" in adm.reason


def test_budgets_from_env_default_override_and_bad_value_falls_back_with_warning(caplog):
    """잘못된 값은 예외로 끝내지 않고(감시 프로세스의 무한 재시작 방지) 경고 후 기본값으로 진행한다."""
    assert avatar_budgets_from_env({}) == (200.0, 150.0)
    assert avatar_budgets_from_env({"SIMAI_AVATAR_JUDGE_BUDGET_MS": " ", "SIMAI_AVATAR_DETECT_BUDGET_MS": ""}) == (200.0, 150.0)
    assert avatar_budgets_from_env({"SIMAI_AVATAR_JUDGE_BUDGET_MS": "100", "SIMAI_AVATAR_DETECT_BUDGET_MS": "180"}) == (100.0, 180.0)
    for bad in ("0", "-5", "nan", "inf", "abc"):
        caplog.clear()
        with caplog.at_level("WARNING", logger="simai_ai.gpu.admission"):
            assert avatar_budgets_from_env({"SIMAI_AVATAR_JUDGE_BUDGET_MS": bad,
                                            "SIMAI_AVATAR_DETECT_BUDGET_MS": "170"}) == (200.0, 170.0)
        assert "SIMAI_AVATAR_JUDGE_BUDGET_MS" in caplog.text and "기본값" in caplog.text
    with pytest.raises(ValueError):
        AvatarAdmissionGate("x", wall_s=lambda: 0.0, monotonic=lambda: 0.0, detect_budget_ms=0)


def test_detect_only_over_budget_blocks_then_resumes_while_judge_is_normal(tmp_path):
    """판정은 정상이고 검출만 150ms 를 10 s 넘으면 막고, 검출이 예산 안으로 10 s 이어지면 다시 연다."""
    path = tmp_path / "tier0.live"
    clock = Clock()
    g = gate(path, clock=clock)
    over = {"judge": sample(60.0, max_ms=120.0), "detect": sample(180.0, max_ms=260.0), "detect_required": True}
    ok = {"judge": sample(60.0, max_ms=120.0), "detect": sample(120.0, max_ms=200.0), "detect_required": True}
    write_marker(path, processing=over, mtime=clock.t)
    assert g.check().admit
    clock.advance(AVATAR_OVER_BUDGET_HOLD_S)
    write_marker(path, processing=over, mtime=clock.t)
    adm = g.check()
    assert not adm.admit and adm.code == "over_budget"
    assert "detect 처리 p95 180.0ms > 150ms" in adm.reason and "judge p95 60.0/200ms" in adm.reason
    clock.advance(5.0)
    write_marker(path, processing=ok, mtime=clock.t)
    assert g.check().code == "stabilizing"
    clock.advance(10.0)
    write_marker(path, processing=ok, mtime=clock.t)
    adm = g.check()
    assert adm.admit and adm.code == "ok"
    assert g.stats()["resets"] == {"over_budget": 1}


def test_injected_judge_budget_drives_throttle_baseline(tmp_path):
    """주입한 판정 예산이 기준선(100ms)보다 낮으면 감속도 그 예산에 묶이고, 높으면 기준선 100ms 가 남는다."""
    path = tmp_path / "tier0.live"
    clock = Clock()
    write_marker(path, processing={"judge": sample(50.0), "detect_required": False}, mtime=clock.t)
    low = gate(path, clock=clock, judge_budget_ms=60)
    assert low.check().admit and low.judge_throttle_ms == 60.0
    assert low.recommended_interval_ms() == 400  # 50/60 = 0.83
    assert gate(path, clock=clock).check().admit
    write_marker(path, processing={"judge": sample(80.0), "detect_required": False}, mtime=clock.t)
    high = gate(path, clock=clock, judge_budget_ms=300)
    assert high.check().admit and high.judge_throttle_ms == 100.0
    assert high.recommended_interval_ms() == 400  # 80/100 = 0.80 (80/300 이면 200ms 였다)
    write_marker(path, processing={"judge": sample(250.0), "detect_required": False}, mtime=clock.t)
    adm = high.check()
    assert adm.admit and adm.code == "ok" and "judge p95 250.0/300ms" in adm.reason  # 차단은 주입한 예산 300ms 기준
