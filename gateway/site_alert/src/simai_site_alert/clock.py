"""주입하는 시계. 경보 코어는 현재 시각을 직접 읽지 않고 이 인터페이스를 거친다(gateway/recorder 와 같은 모양)."""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int:
        """UTC epoch ms (int64). site_ack 의 acked_at·sent_at 에 쓴다."""

    def monotonic(self) -> float:
        """경과 시간 측정용 초(수신 → 소리 시작 지연)."""


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.perf_counter()


class FakeClock:
    """시험용. advance 로만 시각이 흐른다."""

    def __init__(self, start_ms: int = 1_760_342_400_000) -> None:
        self._t = start_ms / 1000
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            return round(self._t * 1000)

    def monotonic(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds
