"""GPU 메모리 예산과 분석 워커 설정 (명세 3.2 GPU 메모리 예산표, 6.6 지연 예산, 15.1 G0).

예산표의 값은 모두 G0 실측 전 초기값이다(H100 80GB 기준). G0 에서 Tier 0 사용량(엔진 + 디코더 + 선확보)과 VLM·마스크 사용량을
재서 고친다. 비율은 GPU 전체 메모리에 대한 값이다.

| 프로세스 | 설정 | 초기값 |
|---|---|---|
| Tier 0 | 예산 비율(tier0_fraction), torch 선확보(SIMAI_TIER0_GPU_RESERVE_MB) | 0.20, 8192 MB |
| 분석 워커 전체 | torch 할당 상한(worker_fraction, SIMAI_WORKER_GPU_MEMORY_FRACTION) | 0.70 |
| VLM(vLLM) | gpu_memory_utilization(SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION), 동시 요청 수(…_MAX_NUM_SEQS) | 0.30, 4 |

불변식: tier0_fraction + worker_fraction ≤ 0.95(드라이버·컨텍스트 여유), vLLM 비율 ≤ worker_fraction.
vLLM 의 gpu_memory_utilization 은 '이 vLLM 인스턴스가 쓸 GPU 전체 메모리 비율'이라 Tier 0 몫을 빼고 정한다.
11-06 GPU PoC(H200, vLLM 0.21 + vllm-cosmos3, `--quantization fp8`, S15P31A101-525): 가중치 16.79 → 10.05 GiB, 첫 응답 1.73 → 1.57 s,
실제 입력 약 2,900 토큰/요청. H100 80GB(81,559 MiB)의 0.30(24,468 MiB)은 FP8 가중치 10.05 GiB + CUDA graph(0.03 GiB)·활성화·torch 밖 약 2.9 GiB
+ KV 캐시(약 10.9 GiB = 79,536 토큰, 32k 상한 기준 동시 2.43x. H200 에서 같은 절대 크기로 띄운 시험값, S15P31A101-580)를 담고(KV 는 BF16,
FP8 은 가중치만), 아바타(0.10)를 켠 기본 구성에서도 분석 워커에 0.30(24,468 MiB)을
남겨 12차 GPU 세션의 MapAnything OOM(워커 몫 0.05 ≈ 4 GB < 피크 6.4 GB)을 막는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from simai_ai.gpu.admission import (
    DEFAULT_LIVE_BLOCKED,
    DEFAULT_MAX_QUEUE_WAIT_EXCEEDED,
    DEFAULT_RESUME_AFTER_S,
    AdmissionPolicy,
)

HEADROOM_FRACTION = 0.05
MIN_WORKER_TORCH_FRACTION = 0.10  # H100 80GB 기준 ~8GB: MapAnything 피크 할당(6.4GB) OOM 방지를 위한 분석 워커 최소 보장 비율


class BudgetError(ValueError):
    """예산·설정 값이 틀렸다."""


@dataclass(frozen=True)
class GpuBudget:
    tier0_fraction: float = 0.20
    worker_fraction: float = 0.70
    vllm_gpu_memory_utilization: float = 0.30
    vllm_max_num_seqs: int = 4

    def errors(self) -> list[str]:
        errs = []
        for name in ("tier0_fraction", "worker_fraction", "vllm_gpu_memory_utilization"):
            v = getattr(self, name)
            if not 0 < v <= 1:
                errs.append(f"{name} 는 0 초과 1 이하다: {v}")
        if self.tier0_fraction + self.worker_fraction > 1 - HEADROOM_FRACTION + 1e-9:
            errs.append(f"tier0_fraction + worker_fraction 이 {1 - HEADROOM_FRACTION} 을 넘는다: "
                        f"{self.tier0_fraction} + {self.worker_fraction}")
        if self.vllm_gpu_memory_utilization > self.worker_fraction + 1e-9:
            errs.append(f"vllm_gpu_memory_utilization({self.vllm_gpu_memory_utilization}) 이 "
                        f"worker_fraction({self.worker_fraction}) 보다 크다")
        if self.vllm_max_num_seqs < 1:
            errs.append("vllm_max_num_seqs 는 1 이상이다")
        return errs


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise BudgetError(f"{key} 는 숫자다") from None


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise BudgetError(f"{key} 는 정수다") from None


def _optional_int(env: Mapping[str, str], key: str) -> int | None:
    raw = (env.get(key) or "").strip()
    if not raw or raw == "-":
        return None
    try:
        return int(raw)
    except ValueError:
        raise BudgetError(f"{key} 는 정수다") from None


def _flag(env: Mapping[str, str], key: str) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if raw in ("", "0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    raise BudgetError(f"{key} 는 1 또는 0 이다")


@dataclass(frozen=True)
class WorkerGpuSettings:
    """분석 워커의 GPU 공유 설정(환경 변수). 처리기(VLM 등)는 budget 에서 vLLM 값을 읽는다."""

    marker: Path
    policy: AdmissionPolicy
    budget: GpuBudget
    live_wait_ms: int = 5_000
    hold_max_s: float = 60.0
    pause_s: float = 2.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WorkerGpuSettings:
        """환경 변수:

        SIMAI_TIER0_MARKER                      Tier 0 표시 파일 (기본 $SIMAI_ROOT/state/tier0.live)
        SIMAI_WORKER_LAG_BUDGET_MS              capture_to_risk 예산 (기본 600, 명세 6.6 p95)
        SIMAI_WORKER_SETUP_LAG_BUDGET_MS        site_setup 전용 지연 예산 (기본 빈 값/"-" = 무제한, 양의 정수면 상한 ms)
        SIMAI_WORKER_MARKER_MAX_AGE_S           표시 파일이 이보다 오래되면 모름 → 받지 않음 (기본 15, 갱신 5초의 3배)
        SIMAI_WORKER_RESUME_AFTER_S             멈춘 뒤 통과 상태가 이만큼 이어져야 다시 받음 (기본 10, 10차 실측 근거)
        SIMAI_WORKER_MAX_QUEUE_WAIT_EXCEEDED    한 창(5초)에서 넘기는 큐 대기 초과 프레임 수, 넘으면 멈춤 (기본 2)
        SIMAI_WORKER_LIVE_BLOCKED_TYPES         LIVE 중 받지 않는 작업 유형(쉼표) (기본 static_reconstruct,hmr_refine,
                                                hmr_appearance_update. 빈 값 "-" 는 막지 않음)
        SIMAI_WORKER_LIVE_WAIT_MS               LIVE 중 대여 long-poll 상한 (기본 5000)
        SIMAI_WORKER_HOLD_MAX_S                 대여 뒤 지연 초과면 시작하지 않고 기다리는 상한 (기본 60)
        SIMAI_WORKER_DEV_NO_LIVE                1 이면 표시 파일이 없을 때 관제 없음으로 보고 모든 유형을 받는다(개발 시간
                                                명시 모드). 기본(빈 값)은 표시 파일이 없으면 모름 → 받지 않는다
        SIMAI_WORKER_GPU_MEMORY_FRACTION        분석 워커 torch 할당 상한 (기본 0.70)
        SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION, SIMAI_WORKER_VLLM_MAX_NUM_SEQS   (기본 0.30, 4)
        SIMAI_GPU_TIER0_FRACTION                예산 검사용 Tier 0 몫 (기본 0.20)
        """
        root = Path(env.get("SIMAI_ROOT") or "/workspace/simai")
        marker = Path((env.get("SIMAI_TIER0_MARKER") or "").strip() or root / "state" / "tier0.live")
        raw_types = env.get("SIMAI_WORKER_LIVE_BLOCKED_TYPES")
        if raw_types is None or not raw_types.strip():
            blocked = DEFAULT_LIVE_BLOCKED
        elif raw_types.strip() == "-":
            blocked = frozenset()
        else:
            blocked = frozenset(t.strip() for t in raw_types.split(",") if t.strip())
        try:
            policy = AdmissionPolicy(
                lag_budget_ms=_int(env, "SIMAI_WORKER_LAG_BUDGET_MS", 600),
                max_marker_age_s=_float(env, "SIMAI_WORKER_MARKER_MAX_AGE_S", 15.0),
                resume_after_s=_float(env, "SIMAI_WORKER_RESUME_AFTER_S", DEFAULT_RESUME_AFTER_S),
                max_queue_wait_exceeded=_int(env, "SIMAI_WORKER_MAX_QUEUE_WAIT_EXCEEDED", DEFAULT_MAX_QUEUE_WAIT_EXCEEDED),
                live_blocked_types=blocked,
                allow_without_marker=_flag(env, "SIMAI_WORKER_DEV_NO_LIVE"),
                setup_lag_budget_ms=_optional_int(env, "SIMAI_WORKER_SETUP_LAG_BUDGET_MS"),
            )
        except ValueError as e:
            raise BudgetError(str(e)) from None
        budget = GpuBudget(
            tier0_fraction=_float(env, "SIMAI_GPU_TIER0_FRACTION", 0.20),
            worker_fraction=_float(env, "SIMAI_WORKER_GPU_MEMORY_FRACTION", 0.70),
            vllm_gpu_memory_utilization=_float(env, "SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION", 0.30),
            vllm_max_num_seqs=_int(env, "SIMAI_WORKER_VLLM_MAX_NUM_SEQS", 4),
        )
        errs = budget.errors()
        if errs:
            raise BudgetError("; ".join(errs))
        live_wait_ms = _int(env, "SIMAI_WORKER_LIVE_WAIT_MS", 5_000)
        hold_max_s = _float(env, "SIMAI_WORKER_HOLD_MAX_S", 60.0)
        if not 0 <= live_wait_ms <= 30_000:
            raise BudgetError("SIMAI_WORKER_LIVE_WAIT_MS 는 0~30000 이다")
        if hold_max_s < 0:
            raise BudgetError("SIMAI_WORKER_HOLD_MAX_S 는 0 이상이다")
        return cls(marker=marker, policy=policy, budget=budget, live_wait_ms=live_wait_ms, hold_max_s=hold_max_s)
