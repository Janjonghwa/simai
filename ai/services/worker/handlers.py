"""작업 처리기 인터페이스. 작업 유형별 실제 처리(VLM·정적 3D·HMR 등)는 기능 패키지에 두고 후속 티켓에서 조립한다.

처리기는 `JobContext` 를 받아 결과(job_callback.result 로 갈 JSON 객체)를 돌려주거나 `JobFailed` 를 던진다.
처리기는 위치·경보를 바꾸지 못한다. VLM 결과는 상향 의견만 담는다(계약 vlm_rationale).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

SITE_SETUP_STAGES: tuple[str, ...] = (
    "COLLECTING_BACKGROUND",
    "INITIAL_POSE_AND_WORLD",
    "JOINT_BA_AND_VERIFY",
    "PREVIEW_AND_VLM_DRAFT",
    "READY_FOR_ZONES",
    "BAKING_TEXTURE",
    "COMPLETED",
)


def safe_monotonic(clock: Callable[[], float]) -> Callable[[], float]:
    """시험용 시계(반복자 등)도 병렬 스레드에서 안전하게 읽고, 원소가 다하면 마지막 시각을 유지한다."""
    if getattr(clock, "_is_safe_monotonic", False):
        return clock
    lock = threading.Lock()
    last = 0.0

    def _now() -> float:
        nonlocal last
        with lock:
            try:
                last = float(clock())
            except StopIteration:
                pass
            return last

    _now._is_safe_monotonic = True  # type: ignore[attr-defined]
    return _now


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
    """처리기에 넘기는 작업 하나. cancelled 가 켜지면(대여 상실·시간 초과·취소) 처리기는 되도록 빨리 멈춘다.

    deadline(monotonic 초)이 있으면 처리기·객체 저장소·얼굴 처리·VLM 호출은 remaining_s() 안에서만 동작한다.
    """

    job_id: str
    lease_id: str
    job_type: str
    attempt: int
    input: Mapping[str, Any]
    cancelled: threading.Event = field(default_factory=threading.Event)
    _progress: float | None = None
    _site_setup: dict[str, Any] | None = None
    _site_setup_history: list[dict[str, Any]] = field(default_factory=list)
    _on_site_setup: Callable[[dict[str, Any]], None] | None = None
    timeout_ms: int | None = None
    deadline: float | None = None
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self.monotonic = safe_monotonic(self.monotonic)
        if self.deadline is None and self.timeout_ms is not None:
            self.deadline = self.monotonic() + self.timeout_ms / 1000.0

    def bind_budget(
        self,
        default_timeout_ms: int,
        *,
        monotonic: Callable[[], float] | None = None,
    ) -> float:
        """처리기 시작 시점에 시계·실행 상한(deadline)을 확정하고 시작 시각(monotonic 초)을 돌려준다."""
        if monotonic is not None:
            self.monotonic = safe_monotonic(monotonic)
        now = self.monotonic()
        if self.timeout_ms is None:
            self.timeout_ms = default_timeout_ms
        if self.deadline is None:
            self.deadline = now + self.timeout_ms / 1000.0
        return now

    def remaining_s(self, now: float | None = None) -> float | None:
        """실행 상한(deadline)까지 남은 시간(초). deadline 이 없으면 None."""
        if self.deadline is None:
            return None
        current = self.monotonic() if now is None else now
        return max(0.0, self.deadline - current)

    def is_expired(self, now: float | None = None) -> bool:
        """실행 상한(deadline)이 지났는지 여부. deadline 이 없으면 False."""
        rem = self.remaining_s(now)
        return rem is not None and rem <= 0.0

    def report_progress(self, fraction: float | None) -> None:
        """진행률 0~1 (하트비트에 실려 표시에만 쓰인다)."""
        self._progress = None if fraction is None else min(1.0, max(0.0, float(fraction)))

    @property
    def progress(self) -> float | None:
        return self._progress

    def report_site_setup(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """site_setup 단계 스냅샷을 갱신하고(단조 증가 검사) 하트비트 콜백이 있으면 즉시 알린다."""
        snap = dict(snapshot)
        status = str(snap.get("status", ""))
        if status not in SITE_SETUP_STAGES:
            raise ValueError(f"알 수 없는 site_setup 단계: {status!r}")
        pct = float(snap.get("progress_pct", -1.0))
        if not 0.0 <= pct <= 100.0:
            raise ValueError(f"site_setup progress_pct 는 0~100 범위여야 한다: {pct}")
        if self._site_setup is not None:
            prev_status = str(self._site_setup["status"])
            prev_pct = float(self._site_setup["progress_pct"])
            if SITE_SETUP_STAGES.index(status) < SITE_SETUP_STAGES.index(prev_status):
                raise ValueError(
                    f"site_setup 단계는 되돌아갈 수 없다: {prev_status} -> {status}"
                )
            if pct < prev_pct - 1e-9:
                raise ValueError(
                    f"site_setup progress_pct 는 감소할 수 없다: {prev_pct} -> {pct}"
                )
        self._site_setup = snap
        self._site_setup_history.append(dict(snap))
        self.report_progress(pct / 100.0)
        if self._on_site_setup is not None:
            self._on_site_setup(dict(snap))
        return snap

    @property
    def site_setup(self) -> dict[str, Any] | None:
        return None if self._site_setup is None else dict(self._site_setup)

    @property
    def site_setup_history(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._site_setup_history]


class JobHandler(Protocol):
    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        """작업을 실행하고 결과 객체를 돌려준다. 실패는 JobFailed 로 알린다."""
        ...
