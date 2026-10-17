"""주입하는 시계. 녹화 도구 코드는 현재 시각을 직접 읽지 않고 이 인터페이스를 거친다."""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int:
        """UTC epoch ms (int64). 노트북은 NTP 로 UTC 에 맞춘다(명세 3.9)."""

    def monotonic(self) -> float:
        """경과 시간 측정용 초."""

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """시험용. sleep 하면 시각이 그만큼 흐른다(실제로 기다리지 않음). 스레드 하나에서만 쓴다."""

    def __init__(self, start_ms: int = 1_760_342_400_000) -> None:
        self._t = start_ms / 1000
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            return round(self._t * 1000)

    def monotonic(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds
