"""Tier 0 → 노트북 활성 현장 경보 목록 송신 (명세 3.10 현장 경보 재동기화, 12.2 I0, FR-137).

노트북 제어 채널이 (재)접속한 직후 한 번(1Hz 주기를 기다리지 않음), 그 뒤 1Hz 마다 health 에 활성 현장 경보 목록
(`active_site_alerts`)과 목록을 만든 시각(`active_site_alerts_as_of`)을 실어 보낸다. 목록 내용은
simai_ai.risk.site_alerts.build_active_site_alerts(순수 함수)가 생명주기 추적기의 활성 이벤트 상태로 만든다.
여기서는 언제 만들고 보낼지만 정한다(조립, ai/CONVENTION.md).

경합 방지 (계약 health.active_site_alerts):
- 목록은 보내기 직전에 만든다. as_of 는 만든 시각이고 local_alert·alarm_stop 봉투 sent_at 과 같은 시계(주입 Clock)다.
- send 는 local_alert·alarm_stop 과 같은 제어 WebSocket 송신 대기열(한 줄)에 넣는 함수여야 한다. 목록 만들기와 대기열
  넣기 사이에 다른 스레드가 경보 신호를 넣지 못하게, 경보 신호를 넣는 쪽과 같은 lock 을 주입한다(주지 않으면
  같은 스레드에서 부른다고 본다). 그래도 생기는 틈은 노트북이 as_of 로 거른다(as_of 뒤에 보낸 local_alert 로 켠
  경보는 목록에 없어도 끄지 않는다).

lock 을 잡은 채 send 를 부른다. send 안에서 같은 lock 을 다시 잡으면 threading.Lock 은 교착된다. 대기열 함수가 같은
lock 을 잡아야 하면 RLock 을 주입한다.

재시작 순서: Tier 0 가 다시 켜지면 추적기를 체크포인트·저널로 복원(LifecycleTracker.restore)하기 전에는 목록을 보내지
않는다. 복원 전 빈 목록이 나가면 노트북은 실제 경보의 표시·소리를 모두 내린다. ready 를 주입해 복원이 끝나면 True 를
돌려주게 한다. 준비 전에 접속하면 연결만 기억하고, 준비된 뒤 첫 tick 에 바로 보낸다.

send 는 health payload 에 합칠 필드 두 개({active_site_alerts, active_site_alerts_as_of})를 받는다. health 의 나머지
필드와 봉투는 health 를 만드는 쪽이 채운다. 같은 필드를 EC2 로 가는 I1 health 에도 쓸 수 있다(fields()).
send 가 실패하면 기록만 하고 다음 tick 에 다시 보낸다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from simai_ai.risk import Clock, EventState, build_active_site_alerts, default_summary

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_MS = 1000  # 1 Hz health (명세 3.10, 12.3)

SendFields = Callable[[dict[str, Any]], None]
Summarize = Callable[[EventState], str]


class SiteAlertResync:
    """활성 현장 경보 목록 송신기. 접속 직후 즉시, 그 뒤 interval_ms 마다 보낸다.

    active_states: 지금 활성 이벤트 상태들(LIVE 추적기의 LifecycleTracker.active 등). 비LIVE 추적기 상태가 섞여도
    목록에는 들어가지 않는다(build_active_site_alerts 가 7.3 표로 거른다).
    """

    def __init__(
        self,
        *,
        active_states: Callable[[], Iterable[EventState]],
        send: SendFields,
        clock: Clock,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        summarize: Summarize = default_summary,
        lock: threading.Lock | threading.RLock | None = None,
        ready: Callable[[], bool] = lambda: True,
    ) -> None:
        if interval_ms <= 0:
            raise ValueError("interval_ms 는 양수다")
        self._active_states = active_states
        self._send = send
        self._clock = clock
        self._interval_ms = interval_ms
        self._summarize = summarize
        self._lock: AbstractContextManager[Any] = lock if lock is not None else nullcontext()
        self._ready = ready
        self._connected = False
        self._last_sent_ms: int | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def fields(self) -> dict[str, Any]:
        """지금 상태로 health 에 실을 필드를 만든다(보내지 않음). as_of 는 지금 시각이다."""
        as_of = self._clock.now_ms()
        alerts = build_active_site_alerts(self._active_states(), self._summarize)
        return {"active_site_alerts": alerts, "active_site_alerts_as_of": as_of}

    def control_connected(self) -> dict[str, Any] | None:
        """노트북 제어 채널이 (재)접속했다. 주기를 기다리지 않고 바로 목록을 보낸다. 보낸 필드를 돌려준다.

        추적기 복원 전(ready 가 False)이면 보내지 않고, 준비된 뒤 첫 tick 에 바로 보낸다.
        """
        self._connected = True
        self._last_sent_ms = None
        if not self._ready():
            return None
        return self._send_now()

    def control_disconnected(self) -> None:
        """끊겼다. 다시 접속할 때까지 보내지 않는다(다시 접속하면 즉시 보낸다)."""
        self._connected = False
        self._last_sent_ms = None

    def tick(self) -> dict[str, Any] | None:
        """주기 호출(1Hz 보다 자주 불러도 된다). 마지막 송신에서 interval_ms 가 지났으면 보낸다."""
        if not self._connected or not self._ready():
            return None
        now = self._clock.now_ms()
        if self._last_sent_ms is not None and now - self._last_sent_ms < self._interval_ms:
            return None
        return self._send_now()

    def _send_now(self) -> dict[str, Any] | None:
        with self._lock:
            fields = self.fields()
            try:
                self._send(fields)
            except Exception:  # 송신 실패가 Tier 0 루프를 멈추지 않게 한다. 다음 tick 에 다시 보낸다
                log.exception("활성 현장 경보 목록 송신 실패")
                self._last_sent_ms = None
                return None
        self._last_sent_ms = fields["active_site_alerts_as_of"]
        return fields
