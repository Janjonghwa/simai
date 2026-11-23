"""현장 경보 코어: 받은 메시지 → 활성 경보 집합 → 소리·화면 상태. 전송·화면 구현과 분리돼 있다.

규칙 (명세 3.10, 7.3, 계약 local_alert·alarm_stop·site_ack·health.active_site_alerts):
- local_alert RAISED·UPDATED 에 표시를 켜고, 확인 전이면 소리를 반복한다. RESOLVED 에 표시를 내린다.
- 소리는 확인(alarm_stop, 현장 확인 버튼, acknowledged=true)까지 반복하고, 표시는 해소까지 유지한다. 확인은 해소가 아니다.
- 확인은 이벤트에 붙는다. 한 번 확인된 이벤트는 해소 전까지 다시 울리지 않는다(뒤이은 UPDATED 의 acknowledged 가
  아직 false 여도: 현장 확인을 Tier 0 가 저널에 쓰기 전에 보낸 갱신일 수 있다). 예외로 등급이 올라가면(HIGH → CRITICAL)
  확인을 풀고 다시 울린다. 같은 등급 재전송·하향은 확인을 유지한다. 해소 뒤 재개된 이벤트는 새로 판단한다.
- alarm_stop 에 확인 대상 event_sequence 가 있으면, 그보다 뒤에 다시 울리기 시작한 경보(등급 상향·재개, alarm_sequence)는
  멈추지 않는다. 등급 상향 전 상태에 대한 늦은 정지가 상향된 경보 소리까지 멈추지 않게 한다(S15P31A101-161).
- (event_id, event_sequence) 로 중복·역순을 거른다. 같은 event_id 에서 이미 반영한 것 이하의 sequence 는 무시해
  화면을 되돌리지 않는다. 해소된 이벤트의 sequence 도 기억해 늦게 온 RAISED 가 경보를 되살리지 않게 한다.
- LIVE·CRITICAL·HIGH 가 아닌 local_alert 는 거부한다(messages.py).
- 소리 장치가 실패하면 sound_fault 로 화면에 크게 알리고, 다음 판단 때(tick 포함) 다시 시도한다.
- 시험 경보는 RiskEvent·site_ack 를 만들지 않는 별도 경로다. 실제 경보가 있으면 실제 경보 소리가 먼저다.
- 시험 입력(S15P31A101-724, AGENTS 규칙 5): 영상 중계가 녹화 파일(FILE)·합성 영상(SYNTHETIC)을 보내는 동안(set_test_input)은
  받은 경보를 표시·확인·재동기화는 그대로 하되 소리를 내지 않고, 하트비트 alarm.sounding_event_ids 도 비운다. Tier 0 는 재생 영상을
  LIVE 로 판정하므로 이 단말이 실제 알림을 끄는 마지막 자리다. 시작 시 시험 경보(소리 확인)는 그대로 울린다. 시험 입력 중에 표시된
  경보는 실제 카메라(RTSP)로 돌아온 뒤에도 해소될 때까지 울리지 않는다(재생 영상의 경보가 실제 관제 중에 울리지 않게).
- 재생 시험 알림 스위치(S15P31A101-736, replay_test_alerts, .env SIMAI_REPLAY_TEST_ALERTS, 기본 꺼짐): 켜면 위 시험 입력 차단을 하지 않는다
  (녹화 재생 리허설에서 소리를 라이브처럼 낸다). 켜진 채 시험 입력이면 화면에 「재생 시험 · 실제 알림 켜짐」 띠를 띄운다.
- 낙상 새 알림(S15P31A101-863): 소리는 목표 패턴(최상위 등급)이 지금과 같으면 이어 간다(-723 경보 피로). 예외로 처음 들어온
  event_id 의 낙상(RESTART_RULE) 경보가 소리를 내는 대상이면 같은 패턴이어도 처음부터 다시 울린다(진입 소리 중 낙상을 귀로 알게).
  같은 event_id 의 재전송·재판단·재동기화는 다시 울리지 않는다. 해소돼 활성에서 빠지면 잊는다(재개는 새로 판단).

상태 모델은 '활성 경보 집합'(event_id → ActiveAlert)이다.

재동기화 (명세 3.10, S15P31A101-161): Tier 0 가 (재)접속 직후와 1Hz health 마다 보내는 활성 현장 경보 목록
(active_site_alerts)으로 이 집합을 맞춘다. local_alert·alarm_stop·해소를 놓치거나 끊겼다 다시 붙어도 다음 목록에서
Tier 0 상태와 같아진다.
- 목록에 있고 처음 보는 이벤트(놓친 local_alert)는 표시하고, acknowledged 가 false 면 소리를 반복한다.
- 목록의 event_sequence 가 반영한 것보다 낮으면 무시한다(화면을 되돌리지 않음). 높으면 등급·문구를 맞추고, 등급이
  올라갔으면(놓친 상향) 확인을 풀고 다시 울린다. 해소된 이벤트는 기억한 sequence 이하의 항목으로 되살리지 않는다.
- 확인 상태: 목록이 true 면 소리를 멈춘다. false 면 확인을 풀고 다시 울린다(Tier 0 기준으로 지금 경보 단계의 확인이
  없다: 놓친 상향·재개). 단 아직 Tier 0 기록을 받지 못한 현장 확인과, as_of 보다 뒤에 보낸 alarm_stop·local_alert 로
  받은 확인은 유지한다(목록에 반영되지 않았을 수 있다). 같은 ms 는 유지하지 않는다(소리를 다시 켜는 쪽).
- 한계: 해소(RESOLVED)를 놓친 뒤 재개는 UPDATED 로 와서 노트북이 재개인지 알 수 없다. 확인된 경보가 재개돼도 다음 목록
  (최대 약 1초)까지 조용하고, 그 사이 옛 단계를 대상으로 한 늦은 alarm_stop 도 적용된다. 다음 목록이 바로잡는다.
- 목록에서 빠진 이벤트는 해소로 보고 표시를 내린다. 단 as_of 와 같거나 뒤에 보낸 local_alert 로 켜거나 갱신한 경보는
  끄지 않는다(다음 목록에서 다시 판단). 형식이 틀려 판단할 수 없는 항목의 이벤트, event_id 도 읽을 수 없는 항목이 있는 목록,
  active_site_alerts 가 없는 health(이전 송신 측)로는 끄지 않는다.
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
from simai_site_alert.link_status import SiteStatus
from simai_site_alert.messages import (
    AlarmStop,
    ClockSyncRequest,
    Health,
    LocalAlert,
    Other,
    Rejected,
    SiteAlertItem,
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
    # 지금 경보 단계를 시작한 event_sequence: 처음 받은 경보·등급 상향·재개(목록으로 확인이 풀렸으면 그 목록의 값).
    # 확인 대상 sequence 가 이보다 낮은 alarm_stop 은 이 경보를 멈추지 않는다.
    sound_mode: str = "REPEAT"
    alarm_sequence: int = 0
    # 이 경보를 마지막으로 켜거나 갱신한 local_alert 의 봉투 sent_at(Tier 0 시계). 목록으로만 알면 None.
    last_sent_at: int | None = None
    # 확인을 알려 준 alarm_stop·local_alert 의 봉투 sent_at(Tier 0 시계). 목록·현장 버튼으로 확인했으면 None.
    ack_sent_at: int | None = None

    @property
    def sounding(self) -> bool:
        return not self.acknowledged and self.sound_mode != "SILENT"


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
    site: SiteStatus | None = None  # 장애별 현장 표시(link_status). 감시를 켜지 않았으면(--offline) None
    test_input: bool = False  # 시험 입력(녹화 파일·합성 영상) 중: 실제 경보 소리를 끈다(S15P31A101-724)
    test_muted: frozenset[str] = frozenset()  # 시험 입력 중에 표시돼 해소까지 소리를 내지 않는 경보 event_id
    replay_test_alerts: bool = False  # 재생 시험 알림 스위치가 켜져 있다(시험 입력이어도 소리를 낸다, S15P31A101-736)


@dataclass(frozen=True)
class _EarlyAck:
    """local_alert 보다 먼저 온 alarm_stop."""

    ack: AckInfo
    target: int | None
    sent_at: int | None


class Outcome(Enum):
    APPLIED = "APPLIED"
    STALE = "STALE"  # 중복 또는 늦게 온 낮은 sequence
    REJECTED = "REJECTED"
    IGNORED = "IGNORED"  # 경보에 쓰지 않는 메시지(clock_sync_request 는 app 이 답한다), 목록 없는 health


@dataclass(frozen=True)
class HandleResult:
    outcome: Outcome
    reason: str | None = None
    sound_start_latency_s: float | None = None  # 이 메시지로 소리 시작을 걸었으면 수신 → start() 반환까지
    health: Health | None = None  # health 였으면 해석한 값(장애 상태 기계 link_status 가 쓴다)
    clock_sync: ClockSyncRequest | None = None  # clock_sync_request 였으면 해석한 값(app 이 받은 즉시 답한다)


_SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1}

# 목표 패턴이 지금과 같아도 새 이벤트면 경보음을 처음부터 다시 시작하는 규칙. 사용자 결정 2026-11-23(낙상만):
# 관제 화면(-819)·워치(-860)·Tier 0 소리 사다리(-861)가 낙상을 새로 알리는 것과 같은 규칙을 현장 스피커에도 쓴다.
RESTART_RULE = "R4"


class SiteAlertCore:
    def __init__(self, sound: SoundBackend, *, laptop_id: str, clock: Clock | None = None,
                 replay_test_alerts: bool = False) -> None:
        self._sound = sound
        self._once_played: dict[tuple[str, int], int] = {}
        self._clock = clock or SystemClock()
        self.laptop_id = laptop_id
        self._lock = threading.RLock()
        self._active: dict[str, ActiveAlert] = {}
        self._resolved: OrderedDict[str, int] = OrderedDict()
        self._early_acks: OrderedDict[str, _EarlyAck] = OrderedDict()
        self._seen_msg_ids: OrderedDict[str, None] = OrderedDict()
        self._pending_site_acks: dict[str, dict[str, Any]] = {}
        self._playing: SoundPattern | None = None
        self._sound_fault: str | None = None
        self._check_failed = False  # 장치 확인(check)이 실패했다. 다시 확인될 때까지 오류 표시를 내리지 않는다
        self._test: TestAlertState | None = None
        self._control_connected: bool | None = None
        self._site: SiteStatus | None = None
        self._test_input = False  # 영상 중계 입력이 녹화 파일·합성 영상이다(set_test_input)
        self._test_muted: set[str] = set()  # 시험 입력 중에 활성이었던 경보. 해소될 때까지 울리지 않는다
        self._restart_seen: set[str] = set()  # 이미 처리한 RESTART_RULE 경보 event_id. 활성에서 빠지면 지운다
        # 재생 시험 알림 스위치(S15P31A101-736): 켜면 시험 입력이어도 소리를 끄지 않는다. 기동 때 정하고 바꾸지 않는다
        self._replay_test_alerts = bool(replay_test_alerts)
        if self._replay_test_alerts:
            log.warning("재생 시험 알림 스위치가 켜져 있다(SIMAI_REPLAY_TEST_ALERTS): 녹화 재생 중에도 경보 소리를 낸다. 본 시연 전에 끈다")
        self._listeners: list[Callable[[ScreenState], None]] = []
        self.last_health: Health | None = None  # 마지막으로 받은 health(재동기화 부분)
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
        if isinstance(msg, ClockSyncRequest):  # 경보 상태와 무관하다. 잠금을 기다리지 않고 돌려준다(응답 지연을 늘리지 않게)
            return HandleResult(Outcome.IGNORED, "clock_sync_request 는 app 이 답한다", clock_sync=msg)
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
                return self._on_health(msg, received_mono)
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
            ack_sent_at = prev.ack_sent_at if prev else None
            alarm_sequence = prev.alarm_sequence if prev else msg.event_sequence
            if not reopened:
                early = self._early_acks.pop(msg.event_id, None)
                if early is not None and prev is None and (early.target is None or early.target >= alarm_sequence):
                    ack, acknowledged, ack_sent_at = early.ack, True, early.sent_at
            else:
                self._resolved.pop(msg.event_id)
            escalated = prev is not None and _SEVERITY_RANK[msg.severity] < _SEVERITY_RANK[prev.severity]
            if escalated:
                alarm_sequence = msg.event_sequence
            if escalated and acknowledged:
                # 확인 뒤 등급이 올라가면(HIGH → CRITICAL) 확인을 풀고 다시 울린다. Tier 0 도 실제 상향이면 경보를 다시
                # 보낸다(S15P31A101-73). 같은 등급 재전송·하향은 확인을 유지한다. 상향 전 확인으로 보낸 site_ack 는 다시
                # 보내지 않는다(그 확인은 상향 전 상태가 대상이라 상향된 경보를 멈추지 못한다, alarm_stop.event_sequence).
                acknowledged, ack, ack_sent_at = False, None, None
                self._pending_site_acks.pop(msg.event_id, None)
            elif msg.acknowledged and not acknowledged:
                acknowledged = True  # 누가 확인했는지는 alarm_stop 으로 온다
                ack_sent_at = msg.sent_at
            self._active[msg.event_id] = ActiveAlert(
                event_id=msg.event_id,
                event_sequence=msg.event_sequence,
                severity=msg.severity,
                sound_mode=msg.sound_mode,
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
                alarm_sequence=alarm_sequence,
                last_sent_at=msg.sent_at,
                ack_sent_at=ack_sent_at,
            )
            latency = self._reconcile(received_mono)
        self._notify()
        return HandleResult(Outcome.APPLIED, msg.lifecycle, latency)

    def _remember_resolved(self, event_id: str, seq: int) -> None:
        self._resolved[event_id] = seq
        self._resolved.move_to_end(event_id)
        while len(self._resolved) > _RESOLVED_MEMORY:
            self._resolved.popitem(last=False)

    def _drop_pending_site_ack(self, event_id: str, ack_id: str | None = None) -> None:
        """보낸 현장 확인을 Tier 0 가 기록했다. ack_id 를 주면 그 확인일 때만 지운다."""
        envelope = self._pending_site_acks.get(event_id)
        if envelope is None:
            return
        if ack_id is None or envelope["payload"]["ack"]["ack_id"] == ack_id:
            del self._pending_site_acks[event_id]

    def _on_alarm_stop(self, msg: AlarmStop) -> HandleResult:
        ack = AckInfo(msg.ack.channel, msg.ack.actor_role, msg.ack.actor_id, msg.ack.acked_at, local=False)
        alert = self._active.get(msg.event_id)
        if alert is None:
            if msg.event_id in self._resolved:
                return HandleResult(Outcome.STALE, "이미 해소된 이벤트의 alarm_stop")
            # local_alert 보다 먼저 왔다. 그 이벤트가 오면(대상 sequence 가 맞으면) 확인된 상태로 시작한다.
            self._early_acks[msg.event_id] = _EarlyAck(ack, msg.event_sequence, msg.sent_at)
            while len(self._early_acks) > _EARLY_ACK_MEMORY:
                self._early_acks.popitem(last=False)
            self._notify()
            return HandleResult(Outcome.APPLIED, "alarm_stop (이벤트 도착 전)")
        if msg.event_sequence is not None and msg.event_sequence < alert.alarm_sequence:
            # 등급 상향·재개 전 상태에 대한 늦은 정지. 다시 울리기 시작한 경보는 멈추지 않는다. 그 확인이 이 노트북의
            # 현장 확인이었다면 Tier 0 가 기록한 것이므로 다시 보내지 않는다.
            self._drop_pending_site_ack(msg.event_id, msg.ack.ack_id)
            return HandleResult(
                Outcome.STALE,
                f"alarm_stop 대상 event_sequence {msg.event_sequence} < 경보 단계 시작 {alert.alarm_sequence} (상향 전 확인)",
            )
        self._drop_pending_site_ack(msg.event_id)  # 지금 경보 단계의 확인이 저널에 기록됐다(누가 확인했든)
        self._active[msg.event_id] = replace(alert, acknowledged=True, ack=ack, ack_sent_at=msg.sent_at)
        self._reconcile(None)
        self._notify()
        return HandleResult(Outcome.APPLIED, "alarm_stop")

    # ---- 재동기화 (health.active_site_alerts) ---------------------------------------------------------------------

    def _on_health(self, msg: Health, received_mono: float) -> HandleResult:
        self.last_health = msg
        if msg.contract_warnings:
            log.warning("health.active_site_alerts 계약 위반: %s", ", ".join(msg.contract_warnings))
        if msg.active_site_alerts is None:
            return HandleResult(Outcome.IGNORED, "active_site_alerts 없는 health 로는 경보를 바꾸지 않는다", health=msg)
        as_of = msg.active_site_alerts_as_of
        now_ms = self._clock.now_ms()
        changed = 0
        listed = set()
        for item in msg.active_site_alerts:
            listed.add(item.event_id)
            changed += self._apply_list_item(item, as_of, now_ms)
        if not msg.partial:
            for event_id, alert in list(self._active.items()):
                if event_id in listed or event_id in msg.undecidable_event_ids:
                    continue
                if as_of is not None and alert.last_sent_at is not None and alert.last_sent_at >= as_of:
                    # 목록을 만든 뒤(같은 ms 포함, 끄지 않는 쪽)에 보낸 local_alert 로 켠 경보. 다음 목록에서 다시 판단한다
                    continue
                # 목록에서 빠졌다: 해소로 본다(RESOLVED 를 놓쳤다).
                del self._active[event_id]
                self._drop_pending_site_ack(event_id)
                self._early_acks.pop(event_id, None)
                self._remember_resolved(event_id, alert.event_sequence)
                changed += 1
        latency = None
        if changed:
            latency = self._reconcile(received_mono)
            self._notify()
        return HandleResult(Outcome.APPLIED, f"active_site_alerts 재동기화 (바뀐 경보 {changed})", latency, health=msg)

    def _apply_list_item(self, item: SiteAlertItem, as_of: int | None, now_ms: int) -> bool:
        """목록 항목 하나를 활성 경보 집합에 맞춘다. 바뀌었으면 True."""
        event_id = item.event_id
        alert = self._active.get(event_id)
        if alert is None:
            resolved_seq = self._resolved.get(event_id)
            if resolved_seq is not None and item.event_sequence <= resolved_seq:
                return False  # 이미 해소를 반영했다. 늦은 목록으로 되살리지 않는다
            early = None
            if resolved_seq is not None:
                self._resolved.pop(event_id)  # 해소 뒤 재개(놓친 local_alert)
            else:
                early = self._early_acks.pop(event_id, None)
            acknowledged, ack, ack_sent_at = item.acknowledged, None, None
            if (
                early is not None
                and (early.target is None or early.target >= item.event_sequence)
                and (item.acknowledged or (as_of is not None and early.sent_at is not None and early.sent_at > as_of))
            ):
                acknowledged, ack, ack_sent_at = True, early.ack, early.sent_at
            self._active[event_id] = ActiveAlert(
                event_id=event_id,
                event_sequence=item.event_sequence,
                severity=item.severity,
                sound_mode=item.sound_mode,
                summary=item.summary,
                rule_id=item.rule_id,
                zone_id=item.zone_id,
                confidence=None,
                needs_check=False,
                world_ids=(),
                acknowledged=acknowledged,
                ack=ack,
                observation_time=None,
                first_received_ms=now_ms,
                last_received_ms=now_ms,
                alarm_sequence=item.event_sequence,
                last_sent_at=None,
                ack_sent_at=ack_sent_at,
            )
            return True
        if item.event_sequence < alert.event_sequence:
            return False  # 노트북이 더 새 상태를 받았다(목록 뒤에 보낸 local_alert). 되돌리지 않는다
        new = replace(alert, sound_mode=item.sound_mode)
        escalated = False
        if item.event_sequence > alert.event_sequence:
            escalated = _SEVERITY_RANK[item.severity] < _SEVERITY_RANK[alert.severity]
            new = replace(
                new,
                event_sequence=item.event_sequence,
                severity=item.severity,
                sound_mode=item.sound_mode,
                summary=item.summary,
                rule_id=item.rule_id or alert.rule_id,
                zone_id=item.zone_id if item.zone_id is not None else alert.zone_id,
                last_received_ms=now_ms,
                alarm_sequence=item.event_sequence if escalated else alert.alarm_sequence,
            )
        if escalated and new.acknowledged:
            # 놓친 상향. 상향 전 확인은 상향된 경보를 멈추지 못한다(local_alert 상향과 같다).
            new = replace(new, acknowledged=False, ack=None, ack_sent_at=None)
            self._pending_site_acks.pop(event_id, None)
        if item.acknowledged:
            if not new.acknowledged:
                new = replace(new, acknowledged=True, ack=None, ack_sent_at=None)
            elif new.ack is not None and new.ack.local:
                new = replace(new, ack=None)  # Tier 0 가 지금 경보 단계의 확인을 기록했다
            self._drop_pending_site_ack(event_id)
        elif new.acknowledged:
            keep = (new.ack is not None and new.ack.local) or (
                as_of is not None and new.ack_sent_at is not None and new.ack_sent_at > as_of
            )  # 같은 ms 는 유지하지 않는다(목록이 미확인이라면 소리를 다시 켜는 쪽). 목록에서 빼는 쪽만 >= 다
            if not keep:
                # Tier 0 기준으로 지금 경보 단계의 확인이 없다(놓친 재개·상향 뒤 하향 등). 다시 울린다. 이 목록 이전
                # 상태를 대상으로 한 늦은 alarm_stop 이 다시 멈추지 않게 경보 단계 시작을 이 목록 sequence 로 올린다.
                new = replace(
                    new,
                    acknowledged=False,
                    ack=None,
                    ack_sent_at=None,
                    alarm_sequence=max(new.alarm_sequence, item.event_sequence),
                )
        if new == alert:
            return False
        self._active[event_id] = new
        return True

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
            envelope = build_site_ack(
                event_id=event_id, laptop_id=self.laptop_id, now_ms=now_ms, event_sequence=alert.event_sequence
            )
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

    def set_site_status(self, status: SiteStatus) -> None:
        """장애 상태 기계(link_status)가 계산한 현장 표시. 경보 판단에는 쓰지 않는다(표시만)."""
        with self._lock:
            if self._site != status:
                self._site = status
                self._notify()

    def set_test_input(self, active: bool) -> None:
        """영상 중계가 알린 입력 종류(relay_status → RelayReport.test_input). 시험 입력이면 실제 경보 소리를 끈다(S15P31A101-724)."""
        with self._lock:
            if self._test_input != active:
                self._test_input = active
                if self._replay_test_alerts:
                    log.warning("시험 입력 %s: 재생 시험 알림 스위치가 켜져 있어 경보 소리를 그대로 낸다", "시작" if active else "끝")
                else:
                    log.warning("시험 입력 %s: 실제 경보 소리를 %s", "시작" if active else "끝", "끈다" if active else "다시 켠다")
                self._reconcile(None)
                self._notify()

    @property
    def test_input(self) -> bool:
        with self._lock:
            return self._test_input

    def tick(self) -> None:
        """주기 호출(약 1초). 소리 장치가 실패해 있으면 다시 시도한다."""
        with self._lock:
            if self._sound_fault is not None and self._target() is not None:
                self._reconcile(None)
                self._notify()

    # ---- 소리 판단 ---------------------------------------------------------------------------------------------

    def _target(self) -> SoundPattern | None:
        now = self._clock.now_ms()
        active_ids = set(self._active)
        self._once_played = {k: t for k, t in self._once_played.items() if k[0] in active_ids}
        self._update_test_muted()
        sounding = [a for a in self._active.values() if a.sounding and a.event_id not in self._test_muted and (
            a.sound_mode != "ONCE" or (a.event_id, a.alarm_sequence) not in self._once_played
            or now - self._once_played[(a.event_id, a.alarm_sequence)] < pattern_for(a.severity).period_ms
        )]
        if sounding:
            top = min(sounding, key=lambda a: _SEVERITY_RANK[a.severity])
            once = all(a.sound_mode == "ONCE" for a in sounding if a.severity == top.severity)
            return pattern_for(top.severity, once=once)
        if self._test is not None and self._test.active:
            return TEST
        return None

    def _update_test_muted(self) -> None:
        """시험 입력 중이면 지금 활성 경보를 모두 소리 끔 목록에 넣고, 해소된(활성에서 빠진) 경보는 목록에서 뺀다.
        재생 시험 알림 스위치가 켜져 있으면 넣지 않는다(S15P31A101-736)."""
        if self._test_input and not self._replay_test_alerts:
            self._test_muted.update(self._active)
        self._test_muted.intersection_update(self._active)

    def _take_new_restart(self) -> bool:
        """처음 보는 RESTART_RULE 경보를 기억하고, 그중 소리를 내는 대상(시험 소리 끔 아님)이 있으면 True.
        _target() 뒤에 부른다(_test_muted 를 그때 맞춘다). 소리를 내지 않던 경보도 기억해 나중에 다시 시작하지 않는다."""
        ids = {a.event_id for a in self._active.values() if a.rule_id == RESTART_RULE}
        self._restart_seen.intersection_update(ids)
        new = ids - self._restart_seen
        self._restart_seen |= new
        return any(self._active[i].sounding and i not in self._test_muted for i in new)

    def _set_fault(self, e: BaseException) -> None:
        reason = str(e) or type(e).__name__
        if self._sound_fault != reason:
            log.error("소리 장치 오류: %s", reason)
        self._sound_fault = reason

    def _reconcile(self, received_mono: float | None) -> float | None:
        """목표 소리와 지금 소리를 맞춘다. 이 호출로 소리 시작을 걸었고 received_mono 가 있으면 지연(초)을 돌려준다."""
        target = self._target()
        restart = self._take_new_restart()
        new_once = target is not None and not target.repeat and any(
            a.sounding and a.event_id not in self._test_muted and a.sound_mode == "ONCE"
            and (a.event_id, a.alarm_sequence) not in self._once_played and pattern_for(a.severity, once=True) is target for a in self._active.values()
        )
        if target is self._playing and not new_once and not restart and (target is None or self._sound_fault is None):
            return None
        if target is None:
            try:
                self._sound.stop()
            except Exception as e:
                self._set_fault(e)
            self._playing = None
            return None
        if restart and target is self._playing:
            # 같은 패턴 재시작: 백엔드 start 는 '재생 중이면 바꾼다'만 약속하므로 멈춘 뒤 다시 걸어 처음부터 울린다
            # (winsound 는 PlaySound(None) 뒤 새 PlaySound). 정지가 실패해도 시작은 건다.
            try:
                self._sound.stop()
            except Exception as e:
                self._set_fault(e)
        try:
            self._sound.start(target)
        except Exception as e:  # 백엔드 오류가 수신 루프를 멈추지 않게 한다
            self._playing = None
            self._set_fault(e)
            return None
        self._playing = target
        if not target.repeat:
            for alert in self._active.values():
                if alert.sounding and alert.sound_mode == "ONCE" and pattern_for(alert.severity, once=True) is target:
                    self._once_played.setdefault((alert.event_id, alert.alarm_sequence), self._clock.now_ms())
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
        """laptop_heartbeat.alarm 형식 (반복 소리 중 / 표시 중 event_id). 시험 입력으로 소리를 끈 경보는 반복 소리 중이 아니다."""
        with self._lock:
            self._update_test_muted()
            return {
                "sounding_event_ids": [a.event_id for a in self._active.values() if a.sounding and a.event_id not in self._test_muted and (
                    a.sound_mode != "ONCE" or (a.event_id, a.alarm_sequence) not in self._once_played
                    or self._clock.now_ms() - self._once_played[(a.event_id, a.alarm_sequence)] < pattern_for(a.severity).period_ms
                )],
                "displayed_event_ids": list(self._active),
            }

    def screen_state(self) -> ScreenState:
        with self._lock:
            return self._screen_state()

    def _screen_state(self) -> ScreenState:
        self._update_test_muted()
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
            site=self._site,
            test_input=self._test_input,
            test_muted=frozenset(self._test_muted),
            replay_test_alerts=self._replay_test_alerts,
        )
