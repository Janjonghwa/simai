"""Tier 0 GPU 메모리 선확보와 분석 워커 메모리 상한 (명세 3.2: Tier 0 가 GPU 메모리를 먼저 확보하고, 분석 워커는 남은 메모리
안에서 상한을 두고 실행한다).

선확보(GpuReservation):
- torch CUDA 캐싱 할당기에 target_mb 만큼 블록을 잡았다가 텐서만 놓는다. 블록은 Tier 0 프로세스의 캐시에 남아(nvidia-smi 에는
  Tier 0 사용량으로 보인다) 다른 프로세스가 가져가지 못하고, Tier 0 의 torch 할당(전처리·후처리 텐서, ultralytics TensorRT
  입출력 버퍼)이 그 블록을 다시 쓴다. 이미 Tier 0 가 잡아 둔 캐시(memory_reserved)는 target 에 센다.
- 블록은 chunk_mb 단위로 잡는다. 도중에 메모리가 모자라면 잡은 만큼은 그대로 두고 PARTIAL 이다(남은 메모리를 다른 프로세스가
  이미 쥐고 있다는 뜻이다. 분석 워커는 PARTIAL 이면 새 작업을 받지 않는다, admission).
- torch.cuda.empty_cache() 를 부르면 블록이 드라이버로 돌아간다. status() 는 지금 캐시 크기를 다시 재고 target 아래로 떨어졌으면
  LOST 로 알린다(Tier 0 코드에서 empty_cache 를 부르지 않는다).
- torch 가 없거나 CUDA 를 쓸 수 없으면 SKIPPED(노트북·CI). TensorRT 만 있고 torch 가 없는 조립은 엔진 로드가 곧 메모리 확보라
  따로 잡지 않는다(SKIPPED, 이유에 적는다). target_mb 가 0 이면 DISABLED.

분석 워커 상한(cap_process_memory): torch.cuda.set_per_process_memory_fraction 으로 이 프로세스의 torch 할당 상한을 둔다.
vLLM 은 자기 설정(gpu_memory_utilization, budget.WorkerGpuSettings)으로 따로 묶는다.

torch 는 함수 안에서만 불러온다(기본 설치에 없다). 시험은 torch_module 로 가짜를 넣는다.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import threading
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("simai_ai.gpu")

MB = 1024 * 1024

OK = "OK"
PARTIAL = "PARTIAL"
LOST = "LOST"
SKIPPED = "SKIPPED"
DISABLED = "DISABLED"
FAILED = "FAILED"
PENDING = "PENDING"
# 분석 워커가 새 작업을 받아도 되는 선확보 상태. 그 밖(PENDING·PARTIAL·LOST·FAILED, 모르는 값)은 받지 않는다
ADMIT_STATES = frozenset({OK, SKIPPED, DISABLED})


@dataclass(frozen=True)
class ReserveResult:
    state: str
    target_mb: int
    reserved_mb: float | None  # Tier 0 프로세스의 torch 캐시 크기(잴 수 없으면 None)
    device: int
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "target_mb": self.target_mb, "reserved_mb": self.reserved_mb,
                "device": self.device, "reason": self.reason}


@dataclass(frozen=True)
class CapResult:
    state: str  # OK, SKIPPED, DISABLED, FAILED
    fraction: float | None
    reason: str | None = None


def _load_torch(torch_module: Any | None) -> tuple[Any | None, str | None]:
    if torch_module is not None:
        return torch_module, None
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        if importlib.util.find_spec("tensorrt") is not None:
            return None, "torch 없음(TensorRT 단독: 엔진 로드가 곧 메모리 확보라 따로 잡지 않는다)"
        return None, "torch 없음"
    except Exception as e:  # noqa: BLE001 - CUDA 라이브러리 문제 등으로 import 가 깨져도 Tier 0 기동을 막지 않는다
        return None, f"torch 를 불러오지 못했다: {type(e).__name__}: {e}"
    return torch, None


def _cuda_ready(torch: Any) -> str | None:
    try:
        if not torch.cuda.is_available():
            return "CUDA 를 쓸 수 없다"
    except Exception as e:  # noqa: BLE001 - 드라이버 문제도 '쓸 수 없음'
        return f"CUDA 확인 실패: {type(e).__name__}: {e}"
    return None


class GpuReservation:
    """Tier 0 의 선확보 상태. acquire 는 기동 때 한 번, status 는 표시 파일을 쓸 때마다 부른다(스레드 안전)."""

    def __init__(self, target_mb: int, *, device: int = 0, chunk_mb: int = 256, torch_module: Any | None = None) -> None:
        if target_mb < 0 or chunk_mb <= 0 or device < 0:
            raise ValueError("target_mb·device 는 0 이상, chunk_mb 는 양수다")
        self.target_mb = int(target_mb)
        self.device = int(device)
        self.chunk_mb = int(chunk_mb)
        self._torch_module = torch_module
        self._torch: Any | None = None
        self._lock = threading.Lock()
        self._result = ReserveResult(PENDING, self.target_mb, None, self.device, "아직 확보하지 않았다")

    def acquire(self) -> ReserveResult:
        with self._lock:
            self._result = self._acquire()
            r = self._result
        if r.state in (OK, SKIPPED, DISABLED):
            log.info("Tier 0 GPU 메모리 선확보 %s: 목표 %d MB, 캐시 %s MB (%s)", r.state, r.target_mb, r.reserved_mb, r.reason)
        else:
            log.warning("Tier 0 GPU 메모리 선확보 %s: 목표 %d MB, 캐시 %s MB (%s)", r.state, r.target_mb, r.reserved_mb, r.reason)
        return r

    def _acquire(self) -> ReserveResult:
        if self.target_mb == 0:
            return ReserveResult(DISABLED, 0, None, self.device, "target_mb=0")
        torch, why = _load_torch(self._torch_module)
        if torch is None:
            return ReserveResult(SKIPPED, self.target_mb, None, self.device, why)
        why = _cuda_ready(torch)
        if why is not None:
            return ReserveResult(SKIPPED, self.target_mb, None, self.device, why)
        self._torch = torch
        dev = f"cuda:{self.device}"
        try:
            have = torch.cuda.memory_reserved(self.device) / MB
            need = self.target_mb - have
            blocks = []
            failure = None
            try:
                while need > 0:
                    size = min(self.chunk_mb, need)
                    try:
                        blocks.append(torch.empty(int(size * MB), dtype=torch.uint8, device=dev))
                    except RuntimeError as e:  # torch.cuda.OutOfMemoryError 는 RuntimeError 의 하위
                        failure = f"{type(e).__name__}: {str(e)[:200]}"
                        break
                    need -= size
            finally:
                del blocks  # 텐서만 놓는다. 블록은 캐싱 할당기에 남는다(empty_cache 를 부르지 않는다)
            reserved = round(torch.cuda.memory_reserved(self.device) / MB, 1)
        except Exception as e:  # noqa: BLE001 - 선확보 실패로 Tier 0 를 멈추지 않는다(판정이 먼저다)
            return ReserveResult(FAILED, self.target_mb, None, self.device, f"{type(e).__name__}: {e}")
        if failure is not None or reserved < self.target_mb:
            return ReserveResult(PARTIAL, self.target_mb, reserved, self.device,
                                 failure or "캐시가 목표보다 작다")
        return ReserveResult(OK, self.target_mb, reserved, self.device, None)

    def status(self) -> dict[str, Any]:
        """표시 파일에 싣는 상태. OK 였는데 지금 캐시가 목표 아래면(empty_cache 등) LOST 로 바꾼다."""
        with self._lock:
            r = self._result
            if r.state == OK and self._torch is not None:
                try:
                    now_mb = round(self._torch.cuda.memory_reserved(self.device) / MB, 1)
                except Exception as e:  # noqa: BLE001
                    self._result = r = ReserveResult(FAILED, r.target_mb, None, r.device, f"캐시 크기 확인 실패: {e!r}")
                else:
                    if now_mb < r.target_mb:
                        self._result = r = ReserveResult(LOST, r.target_mb, now_mb, r.device,
                                                         "torch 캐시가 목표 아래로 줄었다(empty_cache 등)")
                        log.error("Tier 0 GPU 선확보를 잃었다: 캐시 %.1f MB < 목표 %d MB", now_mb, r.target_mb)
                    else:
                        r = ReserveResult(OK, r.target_mb, now_mb, r.device, None)
            return r.as_dict()


def reserve_tier0_memory(target_mb: int, *, device: int = 0, chunk_mb: int = 256,
                         torch_module: Any | None = None) -> GpuReservation:
    """선확보를 만들고 바로 잡는다. 결과는 돌려준 객체의 status() 로 본다."""
    reservation = GpuReservation(target_mb, device=device, chunk_mb=chunk_mb, torch_module=torch_module)
    reservation.acquire()
    return reservation


def cap_process_memory(fraction: float | None, *, device: int = 0, torch_module: Any | None = None) -> CapResult:
    """분석 워커 프로세스의 torch 할당 상한(전체 GPU 메모리에 대한 비율). None 이면 두지 않는다(DISABLED)."""
    if fraction is None:
        return CapResult(DISABLED, None, "상한 없음")
    if not 0 < fraction <= 1:
        raise ValueError("fraction 은 0 초과 1 이하다")
    torch, why = _load_torch(torch_module)
    if torch is None:
        return CapResult(SKIPPED, fraction, why)
    why = _cuda_ready(torch)
    if why is not None:
        return CapResult(SKIPPED, fraction, why)
    try:
        torch.cuda.set_per_process_memory_fraction(fraction, device)
    except Exception as e:  # noqa: BLE001
        return CapResult(FAILED, fraction, f"{type(e).__name__}: {e}")
    return CapResult(OK, fraction, None)
