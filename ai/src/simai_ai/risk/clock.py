"""주입 시계 (ai/CONVENTION.md 코드 절, 명세 6.1·21.3).

Risk Engine 은 현재 시각을 직접 읽지 않고 Clock 을 주입받는다. risk 안에서 실제 시각을 읽는 곳은
SystemClock.now_ms 하나뿐이다. tests/risk/test_isolation.py 가 그 밖의 시각 읽기(이 파일의 FixedClock·모듈
수준 포함)와 재노출·동적 import 우회를 막는다.

- LIVE(Tier 0): SystemClock. 영상이 모두 멈춰도 시계는 진행하므로 lag 가 커진다(명세 6.1).
- PLANNED·REPLAY·WHAT_IF·공통 벡터·시나리오 재생: FixedClock 으로 판정 시각을 고정한다.

시각은 UTC epoch 밀리초 정수다.
"""

import time
from typing import Protocol, runtime_checkable

from simai_ai.risk._parse import as_int


@runtime_checkable
class Clock(Protocol):
    def now_ms(self) -> int:
        """판정 시각(UTC epoch ms)."""
        ...


class SystemClock:
    """실제 시계. LIVE 판정용."""

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class FixedClock:
    """고정 시계. set·advance 로만 움직인다. 뒤로 가지 않는다."""

    def __init__(self, t_ms: int) -> None:
        self._t_ms = as_int(t_ms, "FixedClock.t_ms", minimum=0)

    def now_ms(self) -> int:
        return self._t_ms

    def set(self, t_ms: int) -> None:
        t_ms = as_int(t_ms, "FixedClock.set", minimum=0)
        if t_ms < self._t_ms:
            raise ValueError(f"FixedClock 은 뒤로 가지 않는다: {self._t_ms} → {t_ms}")
        self._t_ms = t_ms

    def advance(self, delta_ms: int) -> None:
        self.set(self._t_ms + as_int(delta_ms, "FixedClock.advance", minimum=0))
