"""Tier 0 GPU 선확보·분석 워커 메모리 상한 (명세 3.2). GPU 없이 가짜 torch 로 시험한다.

가짜 torch 는 캐싱 할당기를 흉내 낸다: empty 는 캐시(memory_reserved)를 늘리고 텐서를 놓아도 캐시는 남는다. 전체 여유를 넘기면
RuntimeError(OutOfMemoryError 흉내), empty_cache 는 캐시를 드라이버로 돌려준다.
"""

from __future__ import annotations

import sys

import pytest

from simai_ai.gpu.reserve import (
    ADMIT_STATES,
    DISABLED,
    FAILED,
    LOST,
    MB,
    OK,
    PARTIAL,
    PENDING,
    SKIPPED,
    GpuReservation,
    cap_process_memory,
    reserve_tier0_memory,
)


class _Cuda:
    def __init__(self, torch: FakeTorch) -> None:
        self._t = torch

    def is_available(self) -> bool:
        return self._t.available

    def memory_reserved(self, device: int = 0) -> int:
        return self._t.cached

    def empty_cache(self) -> None:
        self._t.cached = 0

    def set_per_process_memory_fraction(self, fraction: float, device: int = 0) -> None:
        if self._t.cap_error:
            raise RuntimeError("cap 실패(가짜)")
        self._t.caps.append((fraction, device))


class FakeTorch:
    uint8 = "uint8"

    def __init__(self, *, free_mb: float = 80_000, cached_mb: float = 0, available: bool = True) -> None:
        self.free = free_mb * MB
        self.cached = int(cached_mb * MB)
        self.available = available
        self.cap_error = False
        self.caps: list[tuple[float, int]] = []
        self.allocs: list[int] = []
        self.on_alloc = None
        self.cuda = _Cuda(self)

    def empty(self, n: int, *, dtype: str, device: str) -> object:
        assert dtype == "uint8" and device.startswith("cuda:")
        if self.on_alloc is not None:
            self.on_alloc()
        if n > self.free:
            raise RuntimeError("CUDA out of memory (가짜)")
        self.free -= n
        self.cached += n
        self.allocs.append(n)
        return object()


def test_reserves_target_in_chunks_and_keeps_cache_after_release():
    torch = FakeTorch()
    r = reserve_tier0_memory(1000, chunk_mb=256, torch_module=torch)
    st = r.status()
    assert st["state"] == OK and st["reserved_mb"] == pytest.approx(1000)
    assert [a // MB for a in torch.allocs] == [256, 256, 256, 232]
    assert torch.cached == 1000 * MB  # 텐서를 놓아도 캐시에 남는다(empty_cache 를 부르지 않는다)


def test_existing_cache_counts_toward_target():
    torch = FakeTorch(cached_mb=900)
    r = reserve_tier0_memory(1000, torch_module=torch)
    assert r.status()["state"] == OK
    assert sum(torch.allocs) == 100 * MB


def test_out_of_memory_midway_is_partial_and_not_admitted():
    """다른 프로세스가 이미 메모리를 쥐고 있으면 잡은 만큼만 두고 PARTIAL. 분석 워커는 받지 않는다."""
    torch = FakeTorch(free_mb=600)
    r = reserve_tier0_memory(1000, chunk_mb=256, torch_module=torch)
    st = r.status()
    assert st["state"] == PARTIAL and st["reserved_mb"] == pytest.approx(512)
    assert "out of memory" in st["reason"]
    assert PARTIAL not in ADMIT_STATES


def test_empty_cache_after_reservation_is_reported_lost():
    torch = FakeTorch()
    r = reserve_tier0_memory(512, torch_module=torch)
    assert r.status()["state"] == OK
    torch.cuda.empty_cache()
    st = r.status()
    assert st["state"] == LOST and LOST not in ADMIT_STATES
    assert r.status()["state"] == LOST  # 다시 늘어도 스스로 OK 로 돌아가지 않는다(재확보는 재시작)


def test_no_torch_no_cuda_and_disabled_are_skipped(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch → ImportError
    r = reserve_tier0_memory(1000)
    assert r.status()["state"] == SKIPPED and "torch 없음" in r.status()["reason"]
    assert reserve_tier0_memory(1000, torch_module=FakeTorch(available=False)).status()["state"] == SKIPPED
    assert reserve_tier0_memory(0, torch_module=FakeTorch()).status()["state"] == DISABLED
    assert {SKIPPED, DISABLED, OK} <= ADMIT_STATES


def test_pending_before_acquire_and_unexpected_error_is_failed():
    r = GpuReservation(100, torch_module=FakeTorch())
    assert r.status()["state"] == PENDING and PENDING not in ADMIT_STATES

    torch = FakeTorch()

    def broken(device: int = 0) -> int:
        raise ValueError("드라이버 이상(가짜)")

    torch.cuda.memory_reserved = broken  # type: ignore[method-assign]
    r = reserve_tier0_memory(100, torch_module=torch)
    assert r.status()["state"] == FAILED


def test_worker_cap_sets_fraction_or_skips():
    torch = FakeTorch()
    assert cap_process_memory(0.7, torch_module=torch).state == OK
    assert torch.caps == [(0.7, 0)]
    assert cap_process_memory(0.7, torch_module=FakeTorch(available=False)).state == SKIPPED
    assert cap_process_memory(None, torch_module=torch).state == DISABLED
    torch.cap_error = True
    assert cap_process_memory(0.7, torch_module=torch).state == FAILED
    with pytest.raises(ValueError):
        cap_process_memory(1.5, torch_module=torch)


def test_broken_torch_import_is_skipped_not_raised(monkeypatch):
    """CUDA 라이브러리 문제로 import torch 가 OSError 를 내도 Tier 0 기동을 막지 않는다(SKIPPED)."""
    import importlib

    real = importlib.import_module

    def broken(name, *a, **k):
        if name == "torch":
            raise OSError("libcudart.so: cannot open shared object file (가짜)")
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", broken)
    r = reserve_tier0_memory(1000)
    assert r.status()["state"] == SKIPPED and "OSError" in r.status()["reason"]
