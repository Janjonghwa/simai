"""Tier 0 쪽 노트북(게이트웨이) 연결 판정: health.gateway_link (명세 3.2 이중 감시, 6.5, 12.2 I0 laptop_heartbeat).

노트북은 I0 제어 채널이 연결돼 있으면 1초마다 `laptop_heartbeat` 를 보낸다(gateway/site_alert). Tier 0 는 그 하트비트로
노트북 연결을 판정한다. 노트북 쪽 장애 판단(gateway/site_alert link_status)과 같은 기준을 쓴다: 1초 주기 3회 연속 누락
(3초)이면 끊김이다.

- 연결됨(CONNECTED): 제어 채널이 연결돼 있고, 그 연결에서 받은 마지막 하트비트가 missed_limit × period_ms 안이다.
- 모르면 끊김이다: 연결 직후 아직 하트비트를 받지 못했으면 끊김으로 본다(노트북이 살아 있다는 근거가 없다). 연결이
  끊기면 그 연결에서 받은 하트비트를 버린다(재접속 뒤 새 하트비트가 와야 연결됨).
- 경계: 마지막 하트비트에서 정확히 missed_limit × period_ms 가 지난 순간은 아직 연결됨이고, 그보다 1 ms 라도 지나면
  끊김이다(세 번째 하트비트가 올 차례인 순간까지는 기다린다).

시각은 Tier 0 단조 시계 ms 로 주입받는다(벽시계·노트북 시계와 무관). 현재 시각을 직접 읽지 않는다.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

DEFAULT_PERIOD_MS = 1000
"""노트북 하트비트 주기 (laptop_heartbeat 1초, 명세 12.6)"""
DEFAULT_MISSED_LIMIT = 3
"""연속 누락 허용 횟수. 이만큼 놓치면 끊김 (3초, gateway/site_alert 와 같다)"""


@dataclass(frozen=True)
class GatewayLinkStatus:
    """판정 한 번의 결과. last_heartbeat_age_ms 는 받은 적이 없으면 None."""

    connected: bool
    control_connected: bool
    heartbeats: int
    last_heartbeat_age_ms: float | None

    @property
    def link_state(self) -> str:
        """계약 common/defs link_state 값."""
        return "CONNECTED" if self.connected else "DISCONNECTED"


class GatewayLinkMonitor:
    """노트북 제어 채널 연결과 하트비트로 gateway_link 를 판정한다. 여러 스레드에서 불러도 된다."""

    def __init__(self, *, period_ms: int = DEFAULT_PERIOD_MS, missed_limit: int = DEFAULT_MISSED_LIMIT) -> None:
        if isinstance(period_ms, bool) or not isinstance(period_ms, int) or period_ms <= 0:
            raise ValueError(f"period_ms 는 양의 정수다: {period_ms!r}")
        if isinstance(missed_limit, bool) or not isinstance(missed_limit, int) or missed_limit <= 0:
            raise ValueError(f"missed_limit 는 양의 정수다: {missed_limit!r}")
        self._timeout_ms = period_ms * missed_limit
        self._lock = threading.Lock()
        self._connected = False
        self._generation = 0
        self._last_heartbeat: float | None = None
        self._heartbeats = 0
        self._last_now: float | None = None

    @property
    def timeout_ms(self) -> int:
        return self._timeout_ms

    def _check(self, now_mono_ms: float) -> None:
        if not math.isfinite(now_mono_ms):
            raise ValueError(f"단조 시각이 유한하지 않다: {now_mono_ms!r}")
        if self._last_now is not None and now_mono_ms < self._last_now:
            raise ValueError(f"단조 시계가 뒤로 갔다: {self._last_now} -> {now_mono_ms}")
        self._last_now = now_mono_ms

    def control_connected(self, now_mono_ms: float) -> int:
        """제어 채널이 (재)연결됐다. 이전 연결의 하트비트는 버린다. 이 연결의 세대 번호를 돌려준다."""
        with self._lock:
            self._check(now_mono_ms)
            self._connected = True
            self._generation += 1
            self._last_heartbeat = None
            return self._generation

    def control_disconnected(self, generation: int | None = None) -> None:
        """제어 채널이 끊겼다. generation 을 주면 그 연결일 때만 끊김으로 바꾼다(늦게 온 이전 연결의 끊김 통지 무시)."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._connected = False
            self._last_heartbeat = None

    def heartbeat(self, now_mono_ms: float, generation: int | None = None) -> bool:
        """하트비트 하나를 받았다. 연결이 없거나 다른(이전) 연결의 것이면 세지 않고 False."""
        with self._lock:
            self._check(now_mono_ms)
            if not self._connected or (generation is not None and generation != self._generation):
                return False
            self._last_heartbeat = now_mono_ms
            self._heartbeats += 1
            return True

    def status(self, now_mono_ms: float) -> GatewayLinkStatus:
        with self._lock:
            self._check(now_mono_ms)
            age = None if self._last_heartbeat is None else now_mono_ms - self._last_heartbeat
            ok = self._connected and age is not None and age <= self._timeout_ms
            return GatewayLinkStatus(ok, self._connected, self._heartbeats, age)

    def ok(self, now_mono_ms: float) -> bool:
        return self.status(now_mono_ms).connected
