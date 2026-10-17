"""event_id UUIDv7 생성기 (명세 3.2 식별자 규칙, RFC 9562)."""

import random
import uuid

import pytest

from simai_ai.risk import EventIdFactory, FixedClock
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.event_id import is_uuid7, uuid7_ms

T0 = 1760342400100


class StepBackClock:
    """시계가 뒤로 가는 경우(SystemClock 은 NTP 보정으로 뒤로 갈 수 있다)."""

    def __init__(self, values):
        self._values = iter(values)

    def now_ms(self):
        return next(self._values)


def test_layout_is_rfc9562_v7_with_clock_ms():
    value = EventIdFactory(FixedClock(T0)).new()
    parsed = uuid.UUID(value)
    assert is_uuid7(value) and parsed.version == 7 and parsed.variant == uuid.RFC_4122
    assert uuid7_ms(value) == T0


def test_ids_are_unique_and_sorted_within_and_across_ms():
    clock = FixedClock(T0)
    factory = EventIdFactory(clock, randbits=random.Random(1).getrandbits)
    ids = []
    for _ in range(5):
        ids += [factory.new() for _ in range(300)]  # 같은 ms 안에서 여러 개
        clock.advance(1)
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)


def test_clock_going_back_keeps_monotonic():
    factory = EventIdFactory(StepBackClock([T0, T0 - 500, T0 + 1]))
    a, b, c = factory.new(), factory.new(), factory.new()
    assert a < b < c
    assert uuid7_ms(b) == T0  # 뒤로 간 시각 대신 직전 ms 를 이어 쓴다


def test_counter_overflow_moves_to_next_ms():
    factory = EventIdFactory(FixedClock(T0), randbits=lambda n: (1 << n) - 1)  # 카운터를 최대 근처에서 시작
    ids = [factory.new() for _ in range(3000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert uuid7_ms(ids[-1]) > T0


def test_injected_randomness_reproduces_ids():
    def make():
        return EventIdFactory(FixedClock(T0), randbits=random.Random("v").getrandbits).new()

    assert make() == make()


@pytest.mark.parametrize(
    "value",
    [
        str(uuid.uuid4()),
        "0192A6F0-7C1E-7B3A-9F2D-3C4B5A6D7E8F",  # 대문자는 정규형이 아니다
        "not-a-uuid",
        "",
    ],
)
def test_is_uuid7_rejects_other_strings(value):
    assert not is_uuid7(value)
    with pytest.raises(RiskInputError):
        uuid7_ms(value)


def test_spec_example_is_uuid7():
    assert is_uuid7("0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f")  # 명세 12.3 예시
