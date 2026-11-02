"""Tier 0 지연 신호(LoadWindow)와 분석 워커 접수 문(AdmissionGate)의 반례 시험 (명세 3.2, 6.6, 20.5).

모르는 상태는 모두 '받지 않음'이어야 한다: 표시 파일 확인 실패·오래됨·깨짐·칸 없음, 기동 중, 선확보 미완, 지연 모름(표본 없음·
unknown), 예산 초과. 큐 대기 초과는 창당 max_queue_wait_exceeded(2)건까지 넘긴다(S15P31A101-412). 깜빡임은 resume_after_s(10초)로
막고, 표시 파일이 사라진 직후(재시작 틈)는 live_linger_s 동안 LIVE 로 본다.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from simai_ai.gpu.admission import (
    DEFAULT_LAG_BUDGET_MS,
    DEFAULT_LIVE_BLOCKED,
    Admission,
    AdmissionGate,
    AdmissionPolicy,
    LoadWindow,
    marker_section,
)
from simai_ai.gpu.budget import BudgetError, GpuBudget, WorkerGpuSettings

TYPES = ["calibration_ba", "hmr_refine", "static_reconstruct", "tbm_report", "vlm_rationale"]


class Clock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = time.time()

    def advance(self, s: float) -> None:
        self.mono += s
        self.wall += s


def load(lags, *, unknown: int = 0, budget: int = DEFAULT_LAG_BUDGET_MS, qwe=None) -> dict:
    w = LoadWindow(budget)
    for v in lags:
        w.add(v)
    for _ in range(unknown):
        w.add(None)
    if qwe is not None:
        w.add(lags[-1] if lags else None, queue_wait_exceeded=qwe)
    return w.take()


def write(marker: Path, clock: Clock, *, phase="running", reserve="OK", lags=(120.0, 180.0), unknown=0, qwe=None,
          raw: dict | None = None) -> None:
    doc = raw if raw is not None else {
        "started": True,
        "gpu_priority": marker_section(phase=phase, reserve={"state": reserve}, load=load(list(lags), unknown=unknown, qwe=qwe),
                                       written_at_ms=int(clock.wall * 1000), pid=1),
    }
    marker.write_text(json.dumps(doc), encoding="utf-8")
    os.utime(marker, (clock.wall, clock.wall))


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    marker = tmp_path / "tier0.live"
    policy = AdmissionPolicy(resume_after_s=10.0, live_linger_s=30.0, max_marker_age_s=15.0)
    gate = AdmissionGate(marker, policy, wall_s=lambda: clock.wall, monotonic=lambda: clock.mono)
    return clock, marker, gate


def settle(gate: AdmissionGate, clock: Clock, marker: Path, seconds: float = 10.0, **kw) -> Admission:
    """seconds 동안 1초마다 표시 파일을 새로 쓰며 본다(Tier 0 가 계속 갱신하는 상황)."""
    adm = gate.check()
    steps = int(seconds)
    for _ in range(steps):
        clock.advance(1.0)
        write(marker, clock, **kw)
        adm = gate.check()
    return adm


# ------------------------------------------------------------------ LoadWindow


def test_load_window_take_summarizes_and_resets():
    w = LoadWindow(600)
    for v in (100, 650, 300):
        w.add(v)
    w.add(None)
    w.add(float("nan"))  # 숫자가 아니면 모름
    w.add(200, queue_wait_exceeded=2)
    out = w.take()
    assert out["samples"] == 6 and out["unknown"] == 2
    assert out["lag_ms_max"] == 650 and out["over_budget"] == 1
    assert out["lag_ms"] == [100, 650, 300, 200]
    assert out["queue_wait_exceeded"] == 2
    again = w.take()
    assert again["samples"] == 0 and again["lag_ms_max"] is None and again["queue_wait_exceeded"] is None


# ------------------------------------------------------------------ 접수 문


def dev_gate(clock: Clock, marker: Path) -> AdmissionGate:
    """관제가 없는 개발 시간 명시 모드(SIMAI_WORKER_DEV_NO_LIVE=1)."""
    policy = AdmissionPolicy(resume_after_s=10.0, live_linger_s=30.0, max_marker_age_s=15.0, allow_without_marker=True)
    return AdmissionGate(marker, policy, wall_s=lambda: clock.wall, monotonic=lambda: clock.mono)


def test_no_marker_is_unknown_by_default_and_refuses_all(env):
    """리뷰 R1: 표시 파일이 없다는 것만으로는 관제 없음과 쓰기 실패·엔진 준비 중을 구별할 수 없다 → 모름 → 받지 않는다."""
    clock, marker, gate = env
    for _ in range(3):
        adm = gate.check()
        assert adm.live and not adm.admit and "모름" in adm.reason
        assert adm.allowed_types(TYPES) == []
        clock.advance(100.0)


def test_no_marker_in_explicit_dev_mode_admits_all(env):
    clock, marker, _ = env
    adm = dev_gate(clock, marker).check()
    assert adm.admit and not adm.live
    assert adm.allowed_types(TYPES) == TYPES


def test_healthy_live_needs_stable_period_then_excludes_exp_types(env):
    clock, marker, gate = env
    write(marker, clock)
    first = gate.check()
    assert first.live and not first.admit and "안정 대기" in first.reason
    adm = settle(gate, clock, marker, 10)
    assert adm.admit and adm.live
    assert adm.allowed_types(TYPES) == ["calibration_ba", "tbm_report", "vlm_rationale"]
    assert all(adm.blocks(t) for t in DEFAULT_LIVE_BLOCKED)


@pytest.mark.parametrize(
    "kw, why",
    [
        ({"phase": "starting"}, "phase"),
        ({"phase": "shutdown_failed"}, "phase"),
        ({"reserve": "PENDING"}, "선확보"),
        ({"reserve": "PARTIAL"}, "선확보"),
        ({"reserve": "LOST"}, "선확보"),
        ({"reserve": "FAILED"}, "선확보"),
        ({"reserve": None}, "선확보"),
        ({"lags": ()}, "표본이 없다"),
        ({"unknown": 1}, "모르는 표본"),
        ({"lags": (100.0, 601.0)}, "예산"),
        ({"qwe": 3}, "버린 프레임"),
    ],
)
def test_unknown_or_overloaded_states_refuse(env, kw, why):
    clock, marker, gate = env
    adm = settle(gate, clock, marker, 20, **kw)
    assert adm.live and not adm.admit, adm
    assert why in adm.reason
    assert adm.allowed_types(TYPES) == []


def test_lag_exactly_at_budget_is_admitted_and_skipped_reserve_is_ok(env):
    clock, marker, gate = env
    adm = settle(gate, clock, marker, 12, lags=(600.0,), reserve="SKIPPED")
    assert adm.admit


@pytest.mark.parametrize(
    "raw",
    [
        {"started": True},  # 이전 Tier 0: 지연 신호 칸 없음
        {"gpu_priority": "running"},
        {"gpu_priority": {"phase": "running", "reserve": {"state": "OK"}, "load": "x"}},
        {"gpu_priority": {"phase": "running", "reserve": {"state": "OK"},
                          "load": {"samples": True, "unknown": 0, "lag_ms_max": 10}}},
        {"gpu_priority": {"phase": "running", "reserve": {"state": "OK"},
                          "load": {"samples": 3, "unknown": 0, "lag_ms_max": True}}},
        {"gpu_priority": {"phase": "running", "reserve": {"state": "OK"},
                          "load": {"samples": 3, "unknown": 0, "lag_ms_max": 10, "queue_wait_exceeded": "many"}}},
        [1, 2],
    ],
)
def test_malformed_marker_refuses(env, raw):
    clock, marker, gate = env
    adm = settle(gate, clock, marker, 12, raw=raw)
    assert adm.live and not adm.admit


def test_corrupt_json_refuses(env):
    clock, marker, gate = env
    marker.write_bytes(b'{"gpu_priority": {"phase": "runn')
    os.utime(marker, (clock.wall, clock.wall))
    adm = gate.check()
    assert adm.live and not adm.admit and "읽지 못했다" in adm.reason


def test_stale_marker_means_unknown_and_refuses(env):
    """Tier 0 가 죽거나 멈춰 표시 파일이 갱신되지 않으면 모름: LIVE 로 보고 받지 않는다(EXP 도 막힌다)."""
    clock, marker, gate = env
    adm = settle(gate, clock, marker, 12)
    assert adm.admit
    clock.advance(16.0)  # 갱신 없음
    adm = gate.check()
    assert adm.live and not adm.admit and "전 것" in adm.reason


def test_marker_from_the_future_refuses(env):
    clock, marker, gate = env
    write(marker, clock)
    os.utime(marker, (clock.wall + 60, clock.wall + 60))
    assert not gate.check().admit


def test_stat_error_refuses(env, monkeypatch):
    clock, marker, gate = env
    write(marker, clock)

    def denied(path, *a, **k):
        raise PermissionError("가짜")

    monkeypatch.setattr("simai_ai.gpu.admission.os.stat", denied)
    adm = gate.check()
    assert adm.live and not adm.admit and "확인하지 못했다" in adm.reason


def test_flicker_never_admits_until_continuously_ok(env):
    """지연이 예산을 넘었다 들어왔다 하면, 마지막 초과 뒤 resume_after_s 가 지나야 받는다."""
    clock, marker, gate = env
    admitted = []
    for i in range(30):
        clock.advance(1.0)
        write(marker, clock, lags=(700.0,) if i % 4 == 0 else (200.0,))
        admitted.append(gate.check().admit)
    assert not any(admitted)
    adm = settle(gate, clock, marker, 9)
    assert not adm.admit  # 아직 9초
    clock.advance(1.0)
    write(marker, clock)
    assert gate.check().admit


def default_gate(tmp_path):
    """기본 정책(재개 10초, 큐 대기 초과 창당 2건까지 넘김)과 5초마다 갱신되는 표시 파일."""
    clock = Clock()
    marker = tmp_path / "tier0.live"
    gate = AdmissionGate(marker, AdmissionPolicy(), wall_s=lambda: clock.wall, monotonic=lambda: clock.mono)
    return clock, marker, gate


def open_gate(gate: AdmissionGate, clock: Clock, marker: Path) -> None:
    """평시(10차 p95 약 300ms) 표시 파일로 기본 10초 안정 대기를 채워 연다."""
    write(marker, clock, lags=(220.0, 266.0, 301.0))
    assert not gate.check().admit
    for _ in range(2):
        clock.advance(5.0)
        write(marker, clock, lags=(230.0, 270.0, 290.0))
        adm = gate.check()
    assert adm.admit and adm.code == "ok"


def test_defaults_are_10s_resume_and_two_queue_wait_frames_per_window():
    policy = AdmissionPolicy()
    assert policy.resume_after_s == 10.0 and policy.max_queue_wait_exceeded == 2
    with pytest.raises(ValueError):
        AdmissionPolicy(max_queue_wait_exceeded=-1)


@pytest.mark.parametrize("qwe", [1, 2])
def test_queue_wait_exceeded_one_or_two_per_window_keeps_gate_open(tmp_path, qwe):
    """10차: 큐 대기 초과 프레임 한두 장으로 30초를 처음부터 세던 것이 아바타·VLM 을 막았다 → 창당 2건까지 넘긴다."""
    clock, marker, gate = default_gate(tmp_path)
    open_gate(gate, clock, marker)
    for _ in range(3):  # 창마다 이어져도 열린 채로 둔다(같은 파일을 여러 번 읽어도 한 창으로 센다)
        clock.advance(5.0)
        write(marker, clock, qwe=qwe)
        for _ in range(4):
            adm = gate.check()
            assert adm.admit and "넘김" in adm.reason
    stats = gate.stats()
    assert stats["queue_wait_tolerated_windows"] == 3 and stats["queue_wait_tolerated_frames"] == 3 * qwe
    assert stats["resets"] == {}


def test_queue_wait_tolerated_while_stabilizing_does_not_restart_clock(tmp_path):
    clock, marker, gate = default_gate(tmp_path)
    write(marker, clock)
    assert gate.check().code == "stabilizing"
    clock.advance(5.0)
    write(marker, clock, qwe=2)
    adm = gate.check()
    assert not adm.admit and "안정 대기 5/10s" in adm.reason
    clock.advance(5.0)
    write(marker, clock)
    assert gate.check().admit


def test_queue_wait_exceeded_three_in_one_window_closes_and_counts(tmp_path):
    clock, marker, gate = default_gate(tmp_path)
    open_gate(gate, clock, marker)
    clock.advance(5.0)
    write(marker, clock, qwe=3)
    adm = gate.check()
    assert not adm.admit and adm.code == "queue_wait_exceeded" and "버린 프레임 3개" in adm.reason
    assert gate.check().code == "queue_wait_exceeded"  # 같은 창을 다시 읽어도 닫힘
    stats = gate.stats()
    assert stats["resets"] == {"queue_wait_exceeded": 1}  # 초기화는 열림 → 닫힘 한 번만
    assert stats["blocked"]["queue_wait_exceeded"] == 2
    assert stats["last_reset_code"] == "queue_wait_exceeded" and stats["last_reset_ms"] == int(clock.wall * 1000)


def test_single_over_budget_window_closes_immediately_even_with_one_sample(tmp_path):
    """진짜 지연 초과는 단 한 표본·한 창이라도 바로 멈춘다(명세 3.2 창 최댓값, 단발 허용 없음)."""
    clock, marker, gate = default_gate(tmp_path)
    open_gate(gate, clock, marker)
    clock.advance(5.0)
    write(marker, clock, lags=(266.0, 301.0, 672.0))
    adm = gate.check()
    assert not adm.admit and adm.code == "over_budget" and "672ms > 예산 600ms" in adm.reason
    assert gate.stats()["resets"] == {"over_budget": 1}


def test_over_budget_with_tolerable_queue_wait_still_closes(tmp_path):
    clock, marker, gate = default_gate(tmp_path)
    open_gate(gate, clock, marker)
    clock.advance(5.0)
    write(marker, clock, lags=(300.0, 650.0), qwe=1)
    assert gate.check().code == "over_budget"


@pytest.mark.parametrize("kw, code", [({"unknown": 1}, "lag_unknown"), ({"unknown": 1, "qwe": 1}, "lag_unknown"),
                                      ({"lags": ()}, "no_samples"), ({"phase": "starting"}, "not_running")])
def test_unknown_closes_immediately(tmp_path, kw, code):
    clock, marker, gate = default_gate(tmp_path)
    open_gate(gate, clock, marker)
    clock.advance(5.0)
    write(marker, clock, **kw)
    adm = gate.check()
    assert not adm.admit and adm.code == code
    assert gate.stats()["resets"] == {code: 1}


def test_stale_marker_closes_with_stale_code(tmp_path):
    clock, marker, gate = default_gate(tmp_path)
    open_gate(gate, clock, marker)
    clock.advance(16.0)
    adm = gate.check()
    assert not adm.admit and adm.code == "marker_stale"


def test_reopens_after_10s_of_passing_after_close(tmp_path):
    """멈춘 뒤 통과 상태가 10초 이어지면 다시 연다(9.9초에는 아직 대기)."""
    clock, marker, gate = default_gate(tmp_path)
    open_gate(gate, clock, marker)
    clock.advance(5.0)
    write(marker, clock, lags=(700.0,))
    assert not gate.check().admit
    clock.advance(5.0)
    write(marker, clock)  # 첫 통과 창: 여기서 안정 시계 시작
    assert gate.check().code == "stabilizing"
    clock.advance(5.0)
    write(marker, clock, qwe=1)
    assert not gate.check().admit
    clock.advance(4.9)
    assert not gate.check().admit
    clock.advance(0.1)
    adm = gate.check()
    assert adm.admit
    stats = gate.stats()
    assert stats["opened"] == 2 and stats["resets"] == {"over_budget": 1}
    assert stats["blocked"]["stabilizing"] >= 3 and stats["blocked"]["over_budget"] == 1


def test_session10_pattern_mostly_open(tmp_path):
    """10차 실측 모양: 43분 동안 지연 초과 창 4개·큐 대기 초과 창 18개(대부분 1~2건). 닫힌 시간이 지연 초과 창 주변뿐이어야 한다."""
    clock, marker, gate = default_gate(tmp_path)
    open_gate(gate, clock, marker)
    windows = 43 * 60 // 5
    over = {100, 200, 300, 400}
    qwe = {i: (1 if i % 3 else 2) for i in range(50, 50 + 18 * 25, 25)}
    admitted = 0
    for i in range(windows):
        write(marker, clock, lags=(700.0,) if i in over else (250.0, 301.0), qwe=qwe.get(i))
        for _ in range(5):  # 1초마다 본다
            admitted += gate.check().admit
            clock.advance(1.0)
    ratio = admitted / (windows * 5)
    assert ratio > 0.97, ratio
    assert gate.stats()["resets"] == {"over_budget": 4}


def test_marker_removed_lingers_as_live_then_admits_all(env):
    """개발 모드에서도 Tier 0 재시작(정상 종료가 표시 파일을 지움 → 새 시작이 다시 씀) 틈에 EXP 를 받지 않는다."""
    clock, marker, _ = env
    gate = dev_gate(clock, marker)
    assert settle(gate, clock, marker, 12).admit
    marker.unlink()
    adm = gate.check()
    assert adm.live and not adm.admit and "재시작" in adm.reason
    clock.advance(29.0)
    assert not gate.check().admit
    write(marker, clock, phase="starting")  # 새 Tier 0 기동
    assert not gate.check().admit
    marker.unlink()
    clock.advance(31.0)
    adm = gate.check()
    assert adm.admit and not adm.live and adm.allowed_types(TYPES) == TYPES


# ------------------------------------------------------------------ 예산·설정


def test_budget_defaults_are_consistent_and_env_overrides():
    assert GpuBudget().errors() == []
    s = WorkerGpuSettings.from_env({"SIMAI_ROOT": "/v/simai"})
    assert s.marker == Path("/v/simai/state/tier0.live")
    assert s.policy.lag_budget_ms == 600 and s.policy.live_blocked_types == DEFAULT_LIVE_BLOCKED
    assert s.budget.vllm_gpu_memory_utilization == 0.55
    assert s.policy.allow_without_marker is False  # 기본: 표시 파일 없음 = 모름
    assert s.policy.resume_after_s == 10.0 and s.policy.max_queue_wait_exceeded == 2
    s = WorkerGpuSettings.from_env({"SIMAI_WORKER_LIVE_BLOCKED_TYPES": "-", "SIMAI_WORKER_LAG_BUDGET_MS": "500",
                                    "SIMAI_WORKER_DEV_NO_LIVE": "1", "SIMAI_WORKER_RESUME_AFTER_S": "30",
                                    "SIMAI_WORKER_MAX_QUEUE_WAIT_EXCEEDED": "0"})
    assert s.policy.live_blocked_types == frozenset() and s.policy.lag_budget_ms == 500
    assert s.policy.resume_after_s == 30.0 and s.policy.max_queue_wait_exceeded == 0
    assert s.policy.allow_without_marker is True


@pytest.mark.parametrize(
    "env",
    [
        {"SIMAI_WORKER_GPU_MEMORY_FRACTION": "0.8"},  # 0.2 + 0.8 > 0.95
        {"SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION": "0.75"},  # > worker 0.70
        {"SIMAI_WORKER_LAG_BUDGET_MS": "abc"},
        {"SIMAI_WORKER_LAG_BUDGET_MS": "0"},
        {"SIMAI_WORKER_LIVE_WAIT_MS": "40000"},
        {"SIMAI_WORKER_DEV_NO_LIVE": "maybe"},
        {"SIMAI_WORKER_MAX_QUEUE_WAIT_EXCEEDED": "-1"},
    ],
)
def test_budget_rejects_inconsistent_settings(env):
    with pytest.raises(BudgetError):
        WorkerGpuSettings.from_env(env)
