"""이벤트 클립 조립: 링 버퍼 + 클립 대상 전이 → 자르기 → 재다중화 → (선택) 얼굴 흐림 → 업로드 (명세 5.1, 11.5, 14).

Tier 0 루프가 부르는 순서:
1. 수신 스레드: receive(I0 메시지) — 링 버퍼에만 넣는다(메모리).
2. 생명주기 단계 뒤: on_transitions(step.transitions) — LIVE·CRITICAL·HIGH 발생 순간만 대기 목록에 넣는다(trigger.py).
3. 주기적으로: poll() — 사후 구간이 다 찼거나(모든 카메라의 최신 촬영 시각 ≥ 기준 + post_ms) 마감(대기 시작 + post_ms
   + finalize_grace_ms)이 지나면 카메라마다 [기준 − pre_ms, 기준 + post_ms] 를 잘라 내보내고, 업로드 대기열을 돌린다.

얼굴 흐림은 기본으로 하지 않는다(사용자 결정, 명세 11.5 확인 필요 항목 확정). FaceRedactor 를 넘기면 내보내기 전에
반드시 거친다. 업로더가 없으면 잘라낸 클립을 메모리에서 버리고 개수만 센다. 어느 경우에도 링 버퍼와 클립 대상이
아닌 영상은 디스크·외부로 나가지 않는다.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from services.tier0.clips.media import (
    AnnexBMuxer,
    ClipMuxer,
    FaceRedactor,
    prepare_for_export,
)
from services.tier0.clips.record import DEFAULT_RETENTION_DAYS, ClipDraft
from services.tier0.clips.ring_buffer import PacketRingBuffer
from services.tier0.clips.trigger import ClipTrigger, clip_trigger_for
from services.tier0.clips.upload import ClipUploader
from simai_ai.io.messages import I0Message
from simai_ai.risk.clock import Clock
from simai_ai.risk.common import Origin, Severity
from simai_ai.risk.event_id import EventIdFactory
from simai_ai.risk.lifecycle import EventTransition

log = logging.getLogger("services.tier0.clips")


@dataclass(frozen=True)
class ClipConfig:
    """클립 구간 설정. 명세 5.1 '앞뒤 구간(예: 각 20초)'."""

    pre_ms: int = 20_000
    post_ms: int = 20_000
    finalize_grace_ms: int = 5_000  # 사후 구간이 안 차도(카메라 정지) 이만큼 더 기다린 뒤 있는 만큼 자른다
    retention_days: int = DEFAULT_RETENTION_DAYS
    critical_auto_hold: bool = True  # CRITICAL 연결분 자동 보존 표시(11.5)
    max_pending: int = 16  # 자르기를 기다리는 이벤트 수 상한
    max_seen_keys: int = 4096  # 이미 클립을 만든 (event_id, resume_count) 기억 수

    def __post_init__(self) -> None:
        for name in ("pre_ms", "post_ms", "finalize_grace_ms", "retention_days", "max_pending", "max_seen_keys"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0 or (v == 0 and name != "finalize_grace_ms"):
                raise ValueError(f"ClipConfig.{name}: 양의 정수여야 한다 (받은 값 {v!r})")


@dataclass
class _Pending:
    trigger: ClipTrigger
    queued_at_ms: int


@dataclass
class _KeyState:
    severity: Severity


@dataclass
class ClipServiceStats:
    triggers: int = 0  # 클립 대상 전이(중복 제외)
    ignored_transitions: int = 0  # 클립 대상이 아닌 전이(비 LIVE·CAUTION 이하·ACK 등)
    duplicate_triggers: int = 0
    retention_escalations: int = 0  # 같은 위험 구간이 CRITICAL 로 올라 보존 표시를 붙인 횟수
    dropped_pending_full: int = 0
    cut: int = 0  # 잘라낸 카메라 클립
    no_footage: int = 0  # 그 구간 영상이 버퍼에 없었던 카메라
    blocked_no_uploader: int = 0
    failed: int = 0  # 재다중화·(켰을 때) 흐림 실패
    by_camera: dict[str, int] = field(default_factory=dict)


def critical_hold_key(transition: EventTransition) -> tuple[str, int] | None:
    """보존 표시 대상(LIVE·CRITICAL)이면 위험 구간 열쇠 (event_id, resume_count)."""
    s = transition.state
    if not Origin(s.origin).alertable or Severity(s.severity) is not Severity.CRITICAL:
        return None
    return (s.event_id, s.resume_count)


class EventClipService:
    def __init__(
        self,
        ring: PacketRingBuffer,
        clock: Clock,
        *,
        config: ClipConfig = ClipConfig(),
        redactor: FaceRedactor | None = None,
        uploader: ClipUploader | None = None,
        muxer: ClipMuxer | None = None,
        clip_id_factory: Callable[[], str] | None = None,
    ) -> None:
        need = config.pre_ms + config.post_ms + config.finalize_grace_ms
        if need > ring.config.window_ms:
            raise ValueError(
                f"pre_ms + post_ms + finalize_grace_ms({need}) 가 링 버퍼 창({ring.config.window_ms}) 보다 길다. "
                "자를 때 앞 구간이 이미 밀려난다"
            )
        self.ring = ring
        self.config = config
        self._clock = clock
        self._redactor = redactor
        self._uploader = uploader
        self._muxer = muxer or AnnexBMuxer()
        self._new_clip_id = clip_id_factory or EventIdFactory(clock).new
        self._pending: list[_Pending] = []
        self._seen: OrderedDict[tuple[str, int], _KeyState] = OrderedDict()
        self._held: OrderedDict[tuple[str, int], None] = OrderedDict()  # 보존 표시를 붙인 CRITICAL 위험 구간
        self.stats = ClipServiceStats()

    @property
    def export_enabled(self) -> bool:
        return self._uploader is not None

    @property
    def pending(self) -> int:
        return len(self._pending)

    def receive(self, message: I0Message) -> None:
        self.ring.receive(message)

    def on_transitions(self, transitions: Iterable[EventTransition]) -> list[ClipTrigger]:
        """생명주기 전이를 받아 클립 대상이면 대기 목록에 넣는다. 새로 넣은 대상을 돌려준다."""
        added: list[ClipTrigger] = []
        for t in transitions:
            self._hold_if_critical(t)
            trigger = clip_trigger_for(t)
            if trigger is None:
                self.stats.ignored_transitions += 1
                continue
            if self._add(trigger):
                added.append(trigger)
        return added

    def on_triggers(self, triggers: Iterable[ClipTrigger], holds: Iterable[tuple[str, int]] = ()) -> list[ClipTrigger]:
        """on_transitions 와 같되 전이에서 이미 뽑은 값으로 받는다(프로세스 분리 -738: 판정 프로세스가 전이에서 뽑아 검출 프로세스로
        보낸다. 전이 객체는 읽기 전용 매핑을 품어 pickle 되지 않는다). holds 는 보존 표시 대상 (event_id, resume_count)."""
        for event_id, resume_count in holds:
            self._hold(event_id, resume_count)
        return [t for t in triggers if self._add(t)]

    def _add(self, trigger: ClipTrigger) -> bool:
        if trigger.key in self._seen:
            self.stats.duplicate_triggers += 1
            if trigger.severity is Severity.CRITICAL:
                self._escalate(trigger)
            return False
        if len(self._pending) >= self.config.max_pending:
            self.stats.dropped_pending_full += 1
            log.error("클립 대기 목록이 가득 차 이벤트 %s 의 클립을 만들지 못했다", trigger.event_id)
            return False
        self._seen[trigger.key] = _KeyState(trigger.severity)
        while len(self._seen) > self.config.max_seen_keys:
            self._seen.popitem(last=False)
        self._pending.append(_Pending(trigger, self._clock.now_ms()))
        self.stats.triggers += 1
        return True

    def _escalate(self, trigger: ClipTrigger) -> None:
        """HIGH 로 시작한 위험 구간이 CRITICAL 로 올랐다(11.5 CRITICAL 자동 보존). 새 클립은 만들지 않는다.

        자르기 전이면 대기 중인 대상의 등급을 올려 기록이 처음부터 CRITICAL·보존 표시로 나가게 한다. 이미 잘랐으면
        업로더가 보내기 전 기록은 값을 올리고, 보낸 기록에는 보존 표시 추가 기록(ClipRetentionUpdate)을 낸다.
        """
        state = self._seen[trigger.key]
        if state.severity is Severity.CRITICAL or not self.config.critical_auto_hold:
            return
        state.severity = Severity.CRITICAL
        self.stats.retention_escalations += 1
        for p in self._pending:
            if p.trigger.key == trigger.key:
                p.trigger = replace(p.trigger, severity=Severity.CRITICAL)

    def _hold_if_critical(self, t: EventTransition) -> None:
        """LIVE 이벤트가 CRITICAL 이면 그 위험 구간의 클립 모두에 보존 표시를 붙인다(11.5). 열쇠마다 한 번.

        업로더 대기열·임시 저장소(재기동 뒤 복원분 포함)·보낸 기록을 event_id 로 찾으므로, Tier 0 재기동으로 중복 제거
        기억이 비어도 이전에 만든 HIGH 클립이 보존 표시 없이 지워지지 않는다. 판정은 하지 않고 전이 값만 읽는다.
        """
        key = critical_hold_key(t)
        if key is not None:
            self._hold(*key)

    def _hold(self, event_id: str, resume_count: int) -> None:
        if not self.config.critical_auto_hold or self._uploader is None:
            return
        key = (event_id, resume_count)
        if key in self._held:
            return
        self._held[key] = None
        while len(self._held) > self.config.max_seen_keys:
            self._held.popitem(last=False)
        self._uploader.raise_retention_for_event(event_id, resume_count, str(Severity.CRITICAL))

    def poll(self) -> None:
        now = self._clock.now_ms()
        for p in list(self._pending):
            if self._ready(p, now):
                self._pending.remove(p)
                self._finalize(p.trigger, now)
        self.ring.expire(now)
        if self._uploader is not None:
            self._uploader.poll()

    def _ready(self, p: _Pending, now: int) -> bool:
        if now >= p.queued_at_ms + self.config.post_ms + self.config.finalize_grace_ms:
            return True
        end = p.trigger.anchor_capture_ms + self.config.post_ms
        newest = [self.ring.camera(c).newest_capture_ms for c in self.ring.camera_ids]
        return all(n is not None and n >= end for n in newest)

    def _finalize(self, trigger: ClipTrigger, now: int) -> None:
        start = trigger.anchor_capture_ms - self.config.pre_ms
        end = trigger.anchor_capture_ms + self.config.post_ms
        for camera_id in self.ring.camera_ids:
            cut = self.ring.camera(camera_id).cut(start, end)
            if cut.empty:
                self.stats.no_footage += 1
                continue
            self.stats.cut += 1
            try:
                muxed = self._muxer.mux(cut)
                exported = prepare_for_export(muxed, cut, self._redactor)
            except Exception as e:  # noqa: BLE001 - 한 카메라 실패가 다른 카메라·Tier 0 루프를 막지 않는다
                self.stats.failed += 1
                log.error("이벤트 %s 카메라 %s 클립을 만들지 못했다: %s", trigger.event_id, camera_id, type(e).__name__)
                continue
            if self._uploader is None:
                self.stats.blocked_no_uploader += 1
                continue
            draft = ClipDraft.build(
                clip_id=self._new_clip_id(), trigger=trigger, camera_id=camera_id, cut=cut, clip=exported,
                created_at_ms=now, retention_days=self.config.retention_days,
                critical_auto_hold=self.config.critical_auto_hold,
            )
            self._uploader.enqueue(exported, draft)
            self.stats.by_camera[camera_id] = self.stats.by_camera.get(camera_id, 0) + 1
