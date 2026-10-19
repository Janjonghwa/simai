"""같은 GPU 를 쓰는 Tier 0 와 분석 워커의 자원 보호 (명세 3.2 GPU 공유 규칙, 6.6 지연 예산, 20.5 안전 경로 격리, 15.1 G0).

- `reserve`: Tier 0 GPU 메모리 선확보(torch CUDA 가 있을 때만), 분석 워커 프로세스 메모리 상한.
- `budget`: 프로세스별 GPU 메모리 예산(초기값, G0 실측으로 고친다)과 분석 워커 설정(vLLM `gpu_memory_utilization` 등).
- `admission`: Tier 0 가 표시 파일(state/tier0.live)에 싣는 지연 창(LoadWindow)과, 분석 워커가 새 작업을 받기 전에
  그 신호를 읽어 받을지 정하는 문(AdmissionGate). 모르면 받지 않는다.

표준 라이브러리만 쓴다. torch 는 reserve 안에서 필요할 때만 불러온다(노트북·CI 는 없으면 건너뛴다).
"""

from simai_ai.gpu.admission import (
    Admission,
    AdmissionGate,
    AdmissionPolicy,
    LoadWindow,
    marker_section,
)
from simai_ai.gpu.budget import BudgetError, GpuBudget, WorkerGpuSettings
from simai_ai.gpu.reserve import CapResult, ReserveResult, cap_process_memory, reserve_tier0_memory

__all__ = [
    "Admission",
    "AdmissionGate",
    "AdmissionPolicy",
    "BudgetError",
    "CapResult",
    "GpuBudget",
    "LoadWindow",
    "ReserveResult",
    "WorkerGpuSettings",
    "cap_process_memory",
    "marker_section",
    "reserve_tier0_memory",
]
