"""Tier 0 지연 신호(LoadWindow)와 분석 워커 접수 문(AdmissionGate)의 반례 시험 (명세 3.2, 6.6, 20.5).

모르는 상태는 모두 '받지 않음'이어야 한다: 표시 파일 확인 실패·오래됨·깨짐·칸 없음, 기동 중, 선확보 미완, 지연 모름(표본 없음·
unknown), 예산 초과. 깜빡임은 resume_after_s 로 막고, 표시 파일이 사라진 직후(재시작 틈)는 live_linger_s 동안 LIVE 로 본다.
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
        ({"qwe": 1}, "버린 프레임"),
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


def test_session9_single_672ms_spike_does_not_reset_30s_stabilization_or_close_open_gate(tmp_path):
    """9차 로그 패턴: 평시 300~550ms 에서 26/30s 지점에 672ms 단발 1회가 나와도 30s 안정 대기가 리셋되지 않고 통과한다."""
    clock = Clock()
    marker = tmp_path / "tier0.live"
    gate = AdmissionGate(marker, AdmissionPolicy(), wall_s=lambda: clock.wall, monotonic=lambda: clock.mono)

    # 0~25s: 5초마다 표시 파일 갱신(평시 300~550ms), 아바타 루프처럼 1초마다 여러 번 check 호출
    write(marker, clock, lags=(310.0, 380.0, 440.0, 510.0, 545.0))
    assert not gate.check().admit
    for second in range(1, 26):
        clock.advance(1.0)
        if second % 5 == 0:
            write(marker, clock, lags=(320.0, 390.0, 460.0, 520.0, 550.0))
        adm = gate.check()
        assert not adm.admit and f"안정 대기 {second}/30s" in adm.reason

    # 26s: 5개 표본 중 단발 1회만 672ms 초과. 같은 표시 파일을 여러 번 읽어도(0.2s 폴링·카메라별 재확인) 리셋되지 않는다.
    clock.advance(1.0)
    write(marker, clock, lags=(340.0, 410.0, 480.0, 530.0, 672.0))
    for _ in range(5):
        adm = gate.check()
        assert not adm.admit and "안정 대기 26/30s" in adm.reason

    # 27~29s: 같은 표시 파일 유지 중에도 안정 대기 시계가 이어진다.
    for second in (27, 28, 29):
        clock.advance(1.0)
        adm = gate.check()
        assert not adm.admit and f"안정 대기 {second}/30s" in adm.reason

    # 30s: 다음 표시 파일이 300~550ms 로 돌아오면 리셋 없이 즉시 열린다.
    clock.advance(1.0)
    write(marker, clock, lags=(330.0, 405.0, 470.0, 515.0, 540.0))
    assert gate.check().admit

    # 이미 열린 뒤에도 단발 672ms 1회는 열림 상태를 깨지 않는다.
    clock.advance(5.0)
    write(marker, clock, lags=(350.0, 420.0, 490.0, 525.0, 672.0))
    for _ in range(4):
        assert gate.check().admit


def test_consecutive_over_budget_or_multi_sample_spike_or_unknown_resets_gate(tmp_path):
    """연속 초과(예: 9차 678ms → 666ms)·창 내 다수 표본 초과·지연 불명은 즉시 차단하고 30s 안정 대기를 처음부터 다시 센다."""
    clock = Clock()
    marker = tmp_path / "tier0.live"
    gate = AdmissionGate(marker, AdmissionPolicy(), wall_s=lambda: clock.wall, monotonic=lambda: clock.mono)

    write(marker, clock, lags=(320.0, 410.0, 490.0, 530.0, 550.0))
    assert settle(gate, clock, marker, 30, lags=(320.0, 410.0, 490.0, 530.0, 550.0)).admit

    # 1) 첫 번째 단발 초과(678ms)는 허용되지만, 다음 창에서도 연속 초과(666ms)가 나오면 즉시 닫힌다.
    clock.advance(5.0)
    write(marker, clock, lags=(360.0, 420.0, 480.0, 510.0, 678.0))
    assert gate.check().admit
    clock.advance(5.0)
    write(marker, clock, lags=(370.0, 430.0, 495.0, 520.0, 666.0))
    adm = gate.check()
    assert not adm.admit and "666ms > 예산 600ms" in adm.reason

    # 닫힌 뒤에는 다시 30초를 채워야 열린다(29초 유지 시점에는 아직 대기, 30초 유지 시점에 열림).
    clock.advance(1.0)
    write(marker, clock, lags=(320.0, 400.0, 470.0, 510.0, 540.0))
    assert not settle(gate, clock, marker, 29, lags=(320.0, 400.0, 470.0, 510.0, 540.0)).admit
    clock.advance(1.0)
    write(marker, clock, lags=(320.0, 400.0, 470.0, 510.0, 540.0))
    assert gate.check().admit

    # 2) 한 표시 파일 창 안에서 2개 이상 표본이 예산을 넘으면 단 한 번의 창이라도 즉시 닫힌다.
    clock.advance(5.0)
    write(marker, clock, lags=(350.0, 410.0, 520.0, 666.0, 678.0))
    adm = gate.check()
    assert not adm.admit and "678ms > 예산 600ms" in adm.reason

    # 다시 30초 안정화 뒤 열림 확인 후, 지연 불명(unknown=1) 발생 시 즉시 닫히는지 확인한다.
    clock.advance(1.0)
    write(marker, clock, lags=(320.0, 400.0, 470.0, 510.0, 540.0))
    assert settle(gate, clock, marker, 30, lags=(320.0, 400.0, 470.0, 510.0, 540.0)).admit
    clock.advance(5.0)
    write(marker, clock, lags=(350.0, 420.0, 480.0, 510.0), unknown=1)
    adm = gate.check()
    assert not adm.admit and "모르는 표본" in adm.reason


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
    s = WorkerGpuSettings.from_env({"SIMAI_WORKER_LIVE_BLOCKED_TYPES": "-", "SIMAI_WORKER_LAG_BUDGET_MS": "500",
                                    "SIMAI_WORKER_DEV_NO_LIVE": "1"})
    assert s.policy.live_blocked_types == frozenset() and s.policy.lag_budget_ms == 500
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
    ],
)
def test_budget_rejects_inconsistent_settings(env):
    with pytest.raises(BudgetError):
        WorkerGpuSettings.from_env(env)
