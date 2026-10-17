"""현장 경보 코어: 받은 메시지 → 활성 경보 집합 → 소리·화면 상태. 전송·화면 구현과 분리돼 있다.

규칙 (명세 3.10, 7.3, 계약 local_alert·alarm_stop·site_ack):
- local_alert RAISED·UPDATED 에 표시를 켜고, 확인 전이면 소리를 반복한다. RESOLVED 에 표시를 내린다.
- 소리는 확인(alarm_stop, 현장 확인 버튼, acknowledged=true)까지 반복하고, 표시는 해소까지 유지한다. 확인은 해소가 아니다.
- 확인은 이벤트에 붙는다. 한 번 확인된 이벤트는 해소 전까지 다시 울리지 않는다(뒤이은 UPDATED 의 acknowledged 가
  아직 false 여도: 현장 확인을 Tier 0 가 저널에 쓰기 전에 보낸 갱신일 수 있다). 예외로 등급이 올라가면(HIGH → CRITICAL)
  확인을 풀고 다시 울린다. 같은 등급 재전송·하향은 확인을 유지한다. 해소 뒤 재개된 이벤트는 새로 판단한다.
- (event_id, event_sequence) 로 중복·역순을 거른다. 같은 event_id 에서 이미 반영한 것 이하의 sequence 는 무시해
  화면을 되돌리지 않는다. 해소된 이벤트의 sequence 도 기억해 늦게 온 RAISED 가 경보를 되살리지 않게 한다.
- LIVE·CRITICAL·HIGH 가 아닌 local_alert 는 거부한다(messages.py).
- 소리 장치가 실패하면 sound_fault 로 화면에 크게 알리고, 다음 판단 때(tick 포함) 다시 시도한다.
- 시험 경보는 RiskEvent·site_ack 를 만들지 않는 별도 경로다. 실제 경보가 있으면 실제 경보 소리가 먼저다.

상태 모델은 '활성 경보 집합'(event_id → ActiveAlert)이다. 활성 현장 경보 목록(health.active_site_alerts)으로 이 집합을
맞추는 재동기화는 S15P31A101-161 에서 붙인다(여기서는 health 를 받아 두기만 한다).
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter, OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from simai_site_alert.clock import Clock, SystemClock
from simai_site_alert.messages import (
    AlarmStop,
    Health,
    LocalAlert,
    Other,
    Rejected,
    build_site_ack,
    parse_message,
)
from simai_site_alert.sound import TEST, SoundBackend, SoundPattern, pattern_for

log = logging.getLogger(__name__)

_RESOLVED_MEMORY = 4096  # 해소된 이벤트의 마지막 sequence 를 기억하는 개수
_EARLY_ACK_MEMORY = 256
_MSG_ID_MEMORY = 4096


@dataclass(frozen=True)
class AckInfo:
    channel: str | None  # CONTROL_ROOM / FOREMAN_PWA / SITE_BUTTON
    actor_role: str | None
    actor_id: str | None
    acked_at: int | None
    local: bool  # 이 노트북의 현장 확인 버튼으로 확인했고 아직 Tier 0 기록(alarm_stop)을 받지 못함


@dataclass(frozen=True)
class ActiveAlert:
    event_id: str
    event_sequence: int
    severity: str
    summary: str
    rule_id: str | None
    zone_id: str | None
    confidence: str | None
    needs_check: bool
    world_ids: tuple[str, ...]
    acknowledged: bool
    ack: AckInfo | None
    observation_time: int | None
    first_received_ms: int
    last_received_ms: int

    @property
    def sounding(self) -> bool:
        return not self.acknowledged


@dataclass(frozen=True)
class TestAlertState:
    active: bool
    started_ms: int
    sound_started: bool  # 시험음 재생을 걸었는지(들렸는지는 사람이 확인)


@dataclass(frozen=True)
class ScreenState:
    """화면이 그리는 전부. 코어가 바뀔 때마다 새로 만들어 화면에 넘긴다."""

    alerts: tuple[ActiveAlert, ...]  # 표시 순서: CRITICAL 먼저, 확인 전 먼저, 먼저 받은 것 먼저
    playing: str | None  # 지금 반복 중인 소리 패턴 이름
    sound_fault: str | None  # 소리 장치 오류(있으면 화면에 크게)
    test: TestAlertState | None  # 진행 중이거나 마지막 시험 경보
    control_connected: bool | None  # I0 제어 채널 연결 상태(None 은 아직 모름)
    pending_site_acks: int  # 보냈지만 Tier 0 기록을 아직 받지 못한 현장 확인 수


class Outcome(Enum):
    APPLIED = "APPLIED"
    STALE = "STALE"  # 중복 또는 늦게 온 낮은 sequence
    REJECTED = "REJECTED"
    IGNORED = "IGNORED"  # 단말이 쓰지 않는 메시지(health, clock_sync 등)


@dataclass(frozen=True)
class HandleResult:
    outcome: Outcome
    reason: str | None = None
    sound_start_latency_s: float | None = None  # 이 메시지로 소리 시작을 걸었으면 수신 → start() 반환까지


_SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1}


class SiteAlertCore:
    def __init__(self, sound: SoundBackend, *, laptop_id: str, clock: Clock | None = None) -> None:
        self._sound = sound
        self._clock = clock or SystemClock()
        self.laptop_id = laptop_id
        self._lock = threading.RLock()
        self._active: dict[str, ActiveAlert] = {}
        self._resolved: OrderedDict[str, int] = OrderedDict()
        self._early_acks: OrderedDict[str, AckInfo] = OrderedDict()
        self._seen_msg_ids: OrderedDict[str, None] = OrderedDict()
        self._pending_site_acks: dict[str, dict[str, Any]] = {}
        self._playing: SoundPattern | None = None
        self._sound_fault: str | None = None
        self._check_failed = False  # 장치 확인(check)이 실패했다. 다시 확인될 때까지 오류 표시를 내리지 않는다
        self._test: TestAlertState | None = None
        self._control_connected: bool | None = None
        self._listeners: list[Callable[[ScreenState], None]] = []
        self.last_health: Health | None = None  # S15P31A101-161 재동기화가 쓴다
        self.rejected: Counter[str] = Counter()
        self.latency_samples_s: deque[float] = deque(maxlen=100_000)

    # ---- 구독 -------------------------------------------------------------------------------------------------

    def add_listener(self, fn: Callable[[ScreenState], None]) -> None:
        with self._lock:
            self._listeners.append(fn)
            self._notify()

    def _notify(self) -> None:
        state = self._screen_state()
        for fn in list(self._listeners):
            try:
                fn(state)
            except Exception:  # 화면 오류가 경보 처리를 멈추지 않게 한다
                log.exception("화면 갱신 실패")

    # ---- 수신 -------------------------------------------------------------------------------------------------

    def handle_text(self, text: str | bytes, received_mono: float | None = None) -> HandleResult:
        """I0 제어 채널에서 받은 메시지 하나. received_mono 는 수신 시각(clock.monotonic 기준)."""
        if received_mono is None:
            received_mono = self._clock.monotonic()
        try:
            obj = json.loads(text)
        except (ValueError, UnicodeDecodeError):
            return self._reject(Rejected("MALFORMED", "JSON 이 아니다"))
        return self.handle(obj, received_mono)

    def handle(self, obj: Any, received_mono: float | None = None) -> HandleResult:
        if received_mono is None:
            received_mono = self._clock.monotonic()
        try:
            msg = parse_message(obj)
        except Rejected as e:
            return self._reject(e)
        with self._lock:
            msg_id = obj.get("msg_id") if isinstance(obj, dict) else None
            if isinstance(msg_id, str) and not isinstance(msg, (Health, Other)):
                if msg_id in self._seen_msg_ids:
                    return HandleResult(Outcome.STALE, "같은 msg_id 를 이미 받았다")
                self._seen_msg_ids[msg_id] = None
                while len(self._seen_msg_ids) > _MSG_ID_MEMORY:
                    self._seen_msg_ids.popitem(last=False)
            if isinstance(msg, LocalAlert):
                return self._on_local_alert(msg, received_mono)
            if isinstance(msg, AlarmStop):
                return self._on_alarm_stop(msg)
            if isinstance(msg, Health):
                self.last_health = msg
                return HandleResult(Outcome.IGNORED, "health (재동기화는 S15P31A101-161)")
            return HandleResult(Outcome.IGNORED, f"{msg.msg_type} 는 단말이 쓰지 않는다")

    def _reject(self, e: Rejected) -> HandleResult:
        with self._lock:
            self.rejected[e.reason] += 1
        log.warning("메시지 거부 %s", e)
        return HandleResult(Outcome.REJECTED, e.reason)

    def _last_sequence(self, event_id: str) -> int | None:
        active = self._active.get(event_id)
        if active is not None:
            return active.event_sequence
        return self._resolved.get(event_id)

    def _on_local_alert(self, msg: LocalAlert, received_mono: float) -> HandleResult:
        if msg.contract_warnings:
            log.warning("local_alert %s 계약 위반(경보는 유지): %s", msg.event_id, ", ".join(msg.contract_warnings))
        last = self._last_sequence(msg.event_id)
        if last is not None and msg.event_sequence <= last:
            return HandleResult(Outcome.STALE, f"event_sequence {msg.event_sequence} <= 반영한 {last}")
        now_ms = self._clock.now_ms()
        if msg.lifecycle == "RESOLVED":
            self._active.pop(msg.event_id, None)
            self._pending_site_acks.pop(msg.event_id, None)
            self._early_acks.pop(msg.event_id, None)
            self._remember_resolved(msg.event_id, msg.event_sequence)
            latency = self._reconcile(None)
        else:
            reopened = msg.event_id in self._resolved
            prev = self._active.get(msg.event_id)
            ack: AckInfo | None = prev.ack if prev else None
            acknowledged = bool(prev and prev.acknowledged)
            if not reopened:
                early = self._early_acks.pop(msg.event_id, None)
                if early is not None:
                    ack, acknowledged = early, True
            else:
                self._resolved.pop(msg.event_id)
            escalated = prev is not None and _SEVERITY_RANK[msg.severity] < _SEVERITY_RANK[prev.severity]
            if escalated and acknowledged:
                # 확인 뒤 등급이 올라가면(HIGH → CRITICAL) 확인을 풀고 다시 울린다. Tier 0 도 실제 상향이면 경보를 다시
                # 보낸다(S15P31A101-73). 같은 등급 재전송·하향은 확인을 유지한다. 상향 전 확인으로 보낸 site_ack 는 다시
                # 보내지 않는다(Tier 0 가 기록하면 alarm_stop 으로 상향된 경보까지 멈추게 되므로).
                acknowledged, ack = False, None
                self._pending_site_acks.pop(msg.event_id, None)
            elif msg.acknowledged and not acknowledged:
                acknowledged = True  # 누가 확인했는지는 alarm_stop 으로 온다
            self._active[msg.event_id] = ActiveAlert(
                event_id=msg.event_id,
                event_sequence=msg.event_sequence,
                severity=msg.severity,
                summary=msg.summary,
                rule_id=msg.rule_id,
                zone_id=msg.zone_id,
                confidence=msg.confidence,
                needs_check=msg.needs_check,
                world_ids=msg.world_ids,
                acknowledged=acknowledged,
                ack=ack,
                observation_time=msg.observation_time,
                first_received_ms=prev.first_received_ms if prev else now_ms,
                last_received_ms=now_ms,
            )
            latency = self._reconcile(received_mono)
        self._notify()
        return HandleResult(Outcome.APPLIED, msg.lifecycle, latency)

    def _remember_resolved(self, event_id: str, seq: int) -> None:
        self._resolved[event_id] = seq
        self._resolved.move_to_end(event_id)
        while len(self._resolved) > _RESOLVED_MEMORY:
            self._resolved.popitem(last=False)

    def _on_alarm_stop(self, msg: AlarmStop) -> HandleResult:
        ack = AckInfo(msg.ack.channel, msg.ack.actor_role, msg.ack.actor_id, msg.ack.acked_at, local=False)
        self._pending_site_acks.pop(msg.event_id, None)  # 저널에 기록됐다(누가 확인했든)
        alert = self._active.get(msg.event_id)
        if alert is None:
            if msg.event_id in self._resolved:
                return HandleResult(Outcome.STALE, "이미 해소된 이벤트의 alarm_stop")
            # local_alert 보다 먼저 왔다. 그 이벤트가 오면 확인된 상태로 시작한다.
            self._early_acks[msg.event_id] = ack
            while len(self._early_acks) > _EARLY_ACK_MEMORY:
                self._early_acks.popitem(last=False)
            self._notify()
            return HandleResult(Outcome.APPLIED, "alarm_stop (이벤트 도착 전)")
        self._active[msg.event_id] = replace(alert, acknowledged=True, ack=ack)
        self._reconcile(None)
        self._notify()
        return HandleResult(Outcome.APPLIED, "alarm_stop")

    # ---- 현장 확인 버튼 ------------------------------------------------------------------------------------------

    def site_ack(self, event_id: str) -> dict[str, Any] | None:
        """현장 확인 버튼. 즉시 소리를 멈추고(표시는 해소까지 유지) 보낼 site_ack 봉투를 돌려준다.

        활성 경보가 아니거나 이미 확인된 경보면 None(보낼 것 없음). 봉투는 Tier 0 기록(alarm_stop)이나 해소를 받을 때까지
        pending_site_acks() 에 남아 재접속 때 같은 봉투로 다시 보낸다(같은 ack_id 는 Tier 0 가 한 번만 기록).
        """
        with self._lock:
            alert = self._active.get(event_id)
            if alert is None or alert.acknowledged:
                return None
            now_ms = self._clock.now_ms()
            envelope = build_site_ack(event_id=event_id, laptop_id=self.laptop_id, now_ms=now_ms)
            ack = AckInfo("SITE_BUTTON", "SITE", self.laptop_id, now_ms, local=True)
            self._active[event_id] = replace(alert, acknowledged=True, ack=ack)
            self._pending_site_acks[event_id] = envelope
            self._reconcile(None)
            self._notify()
            return envelope

    def pending_site_acks(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._pending_site_acks.values())

    # ---- 시험 경보 ---------------------------------------------------------------------------------------------

    def start_test_alert(self) -> bool:
        """시작 시 시험 경보(소리·화면 확인). RiskEvent·site_ack 를 만들지 않는다. 소리 재생을 걸었으면 True."""
        with self._lock:
            try:
                self._sound.check()
                self._check_failed = False
            except Exception as e:
                self._check_failed = True
                self._set_fault(e)
            self._test = TestAlertState(active=True, started_ms=self._clock.now_ms(), sound_started=False)
            self._reconcile(None)
            started = self._sound_fault is None and self._playing is not None
            self._test = replace(self._test, sound_started=started)
            self._notify()
            return started

    def end_test_alert(self) -> None:
        with self._lock:
            if self._test is None or not self._test.active:
                return
            self._test = replace(self._test, active=False)
            self._reconcile(None)
            self._notify()

    # ---- 연결 상태·주기 ----------------------------------------------------------------------------------------

    def set_control_connected(self, connected: bool) -> None:
        with self._lock:
            if self._control_connected != connected:
                self._control_connected = connected
                self._notify()

    def tick(self) -> None:
        """주기 호출(약 1초). 소리 장치가 실패해 있으면 다시 시도한다."""
        with self._lock:
            if self._sound_fault is not None and self._target() is not None:
                self._reconcile(None)
                self._notify()

    # ---- 소리 판단 ---------------------------------------------------------------------------------------------

    def _target(self) -> SoundPattern | None:
        sounding = [a for a in self._active.values() if a.sounding]
        if sounding:
            top = min(sounding, key=lambda a: _SEVERITY_RANK[a.severity])
            return pattern_for(top.severity)
        if self._test is not None and self._test.active:
            return TEST
        return None

    def _set_fault(self, e: BaseException) -> None:
        reason = str(e) or type(e).__name__
        if self._sound_fault != reason:
            log.error("소리 장치 오류: %s", reason)
        self._sound_fault = reason

    def _reconcile(self, received_mono: float | None) -> float | None:
        """목표 소리와 지금 소리를 맞춘다. 이 호출로 소리 시작을 걸었고 received_mono 가 있으면 지연(초)을 돌려준다."""
        target = self._target()
        if target is self._playing and (target is None or self._sound_fault is None):
            return None
        if target is None:
            try:
                self._sound.stop()
            except Exception as e:
                self._set_fault(e)
            self._playing = None
            return None
        try:
            self._sound.start(target)
        except Exception as e:  # 백엔드 오류가 수신 루프를 멈추지 않게 한다
            self._playing = None
            self._set_fault(e)
            return None
        self._playing = target
        if self._check_failed:
            # 재생 호출이 성공해도 장치 확인이 실패한 상태면 소리가 실제로 나는지 알 수 없다. 다시 확인해 본다.
            try:
                self._sound.check()
                self._check_failed = False
                self._sound_fault = None
            except Exception as e:
                self._set_fault(e)
        else:
            self._sound_fault = None
        if received_mono is None:
            return None
        latency = self._clock.monotonic() - received_mono
        self.latency_samples_s.append(latency)
        return latency

    # ---- 조회 -------------------------------------------------------------------------------------------------

    def active_alerts(self) -> dict[str, ActiveAlert]:
        with self._lock:
            return dict(self._active)

    def alarm_status(self) -> dict[str, list[str]]:
        """laptop_heartbeat.alarm 형식 (반복 소리 중 / 표시 중 event_id)."""
        with self._lock:
            return {
                "sounding_event_ids": [a.event_id for a in self._active.values() if a.sounding],
                "displayed_event_ids": list(self._active),
            }

    def screen_state(self) -> ScreenState:
        with self._lock:
            return self._screen_state()

    def _screen_state(self) -> ScreenState:
        alerts = sorted(
            self._active.values(),
            key=lambda a: (_SEVERITY_RANK[a.severity], a.acknowledged, a.first_received_ms, a.event_id),
        )
        return ScreenState(
            alerts=tuple(alerts),
            playing=self._playing.name if self._playing else None,
            sound_fault=self._sound_fault,
            test=self._test,
            control_connected=self._control_connected,
            pending_site_acks=len(self._pending_site_acks),
        )
