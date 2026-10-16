"""주입 시계: 실제 시계(SystemClock)와 고정 시계(FixedClock)."""

import pytest

from simai_ai.risk import Clock, FixedClock, SystemClock
from simai_ai.risk import clock as clock_module


def test_fixed_clock_set_and_advance():
    clock = FixedClock(1000)
    assert clock.now_ms() == 1000
    clock.advance(250)
    assert clock.now_ms() == 1250
    clock.set(1250)
    clock.set(2000)
    assert clock.now_ms() == 2000


@pytest.mark.parametrize("bad", [-1, 1.5, True, "1000"])
def test_fixed_clock_rejects_non_ms(bad):
    with pytest.raises(ValueError):
        FixedClock(bad)


def test_fixed_clock_does_not_go_back():
    clock = FixedClock(1000)
    with pytest.raises(ValueError):
        clock.set(999)
    with pytest.raises(ValueError):
        clock.advance(-1)
    assert clock.now_ms() == 1000


def test_system_clock_is_utc_epoch_ms(monkeypatch):
    # 실제 시각에 기대지 않는다(명세 21.3): 시계 원천을 고정해 단위 변환만 본다.
    monkeypatch.setattr(clock_module.time, "time_ns", lambda: 1_760_342_400_123_456_789)
    assert SystemClock().now_ms() == 1_760_342_400_123


def test_both_clocks_satisfy_protocol():
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FixedClock(0), Clock)
