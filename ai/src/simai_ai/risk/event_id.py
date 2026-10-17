"""RiskEvent 식별자 (명세 3.2 식별자 규칙).

event_id 는 UUIDv7(RFC 9562)이다. 앞 48비트가 UTC epoch ms 라 시간 순서로 정렬되고, 나머지가 난수라
Pod 재시작·여러 현장에서도 충돌하지 않는다. Tier 0 인스턴스 식별자 gateway_id 는 event_id 에 섞지 않고
이벤트에 함께 싣는다. Python 3.11 표준 라이브러리에 uuid7 이 없어 여기서 직접 만든다(risk 는 서드파티 금지).

- 시각은 주입 시계(Clock)에서만 읽는다.
- 같은 ms 안에서는 rand_a 12비트를 카운터로 써서 만든 순서대로 정렬되게 한다(RFC 9562 6.2 방법 1).
  카운터가 넘치거나 시계가 뒤로 가면 직전 ms 를 이어 써서 단조 증가를 지킨다(RFC 9562 6.2 허용).
- 난수는 주입할 수 있다(기본 secrets.randbits). 공통 벡터는 고정 난수로 같은 ID 를 재현한다.
"""

import secrets
import uuid
from collections.abc import Callable

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.clock import Clock

RandBits = Callable[[int], int]

_TS_MAX = (1 << 48) - 1
_COUNTER_MAX = (1 << 12) - 1


class EventIdFactory:
    """event_id(UUIDv7 문자열) 생성기. Tier 0 인스턴스마다 하나를 쓴다."""

    def __init__(self, clock: Clock, randbits: RandBits = secrets.randbits) -> None:
        self._clock = clock
        self._randbits = randbits
        self._last_ms = -1
        self._counter = 0

    def new(self) -> str:
        ms = self._clock.now_ms()
        if ms <= self._last_ms:
            # 같은 ms 이거나 시계가 뒤로 갔다. 직전 ms 를 이어 쓰고 카운터를 올린다.
            ms = self._last_ms
            self._counter += 1
            if self._counter > _COUNTER_MAX:
                ms += 1
                self._counter = self._randbits(11)
        else:
            # 새 ms. 카운터는 난수로 시작하되 최상위 비트를 비워 같은 ms 에 2048개 이상 여유를 둔다.
            self._counter = self._randbits(11)
        if not 0 <= ms <= _TS_MAX:
            raise RiskInputError(f"UUIDv7 시각 범위(48비트 ms)를 벗어났다: {ms}")
        self._last_ms = ms
        value = (ms << 80) | (0x7 << 76) | (self._counter << 64) | (0b10 << 62) | self._randbits(62)
        return str(uuid.UUID(int=value))


def is_uuid7(value: str) -> bool:
    """RFC 9562 UUIDv7 문자열(소문자 8-4-4-4-12)인지."""
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return str(parsed) == value and parsed.version == 7 and parsed.variant == uuid.RFC_4122


def uuid7_ms(value: str) -> int:
    """UUIDv7 의 앞 48비트 UTC epoch ms."""
    if not is_uuid7(value):
        raise RiskInputError(f"UUIDv7 이 아니다: {value!r}")
    return uuid.UUID(value).int >> 80
