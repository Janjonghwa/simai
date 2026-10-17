"""작업 처리기 인터페이스. 작업 유형별 실제 처리(VLM·정적 3D·HMR 등)는 기능 패키지에 두고 후속 티켓에서 조립한다.

처리기는 `JobContext` 를 받아 결과(job_callback.result 로 갈 JSON 객체)를 돌려주거나 `JobFailed` 를 던진다.
처리기는 위치·경보를 바꾸지 못한다. VLM 결과는 상향 의견만 담는다(계약 vlm_rationale).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class JobFailed(Exception):
    """처리기가 알리는 실패. retryable 이면 EC2 가 새 대여로 다시 내줄 수 있다."""

    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        if not code:
            raise ValueError("실패 코드가 비었다")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class JobContext:
    """처리기에 넘기는 작업 하나. cancelled 가 켜지면(대여 상실·시간 초과·취소) 처리기는 되도록 빨리 멈춘다."""

    job_id: str
    lease_id: str
    job_type: str
    attempt: int
    input: Mapping[str, Any]
    cancelled: threading.Event = field(default_factory=threading.Event)
    _progress: float | None = None

    def report_progress(self, fraction: float | None) -> None:
        """진행률 0~1 (하트비트에 실려 표시에만 쓰인다)."""
        self._progress = None if fraction is None else min(1.0, max(0.0, float(fraction)))

    @property
    def progress(self) -> float | None:
        return self._progress


class JobHandler(Protocol):
    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        """작업을 실행하고 결과 객체를 돌려준다. 실패는 JobFailed 로 알린다."""
        ...
