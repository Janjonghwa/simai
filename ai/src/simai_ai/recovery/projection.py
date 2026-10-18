"""저널 → 활성 이벤트 상태 (명세 3.2 복구 순서 2단계, 저널 계약).

전이 기록마다 meta.state(전이 뒤 EventState 전체, eventlog.STATE_META_KEY)가 있어 기록 하나로 그 이벤트의 상태를
온전히 되살린다. 투영(EventProjection)은 journal_offset 순서대로 기록을 적용해 활성 이벤트 집합을 유지한다.

- 체크포인트 상태에서 시작하면 last_applied_journal_offset 다음 기록부터, 체크포인트가 없으면 저널 처음부터 적용한다.
- 멱등: 이벤트별로 이미 적용한 event_sequence 이하의 기록은 건너뛴다(체크포인트와 재적용 저널에 같은 이벤트가 둘 다
  있어도 두 번 적용하지 않고, 낮은 sequence 로 되돌리지 않는다). 해소(RESOLVED)가 최종이면 활성에서 뺀다.
- 해소 기록(tombstone): 해소된 이벤트의 마지막 sequence 와 판정 시각(evaluated_at)을 최근 max_tombstones 개(기본
  1024) 기억하고 체크포인트에도 남긴다. 그래서 해소 뒤(체크포인트 경계를 넘어서도) 같은 이벤트의 과거 전이가 다시
  기록돼도 되살리지 않는다(리뷰 M1).
- 보장 경계: 최근 max_tombstones 개 해소 이벤트는 sequence 로 정확히 거른다. 그보다 오래돼 밀려난 이벤트는 밀려난
  것들의 마지막 판정 시각 최댓값(resolved_watermark)을 남겨, 활성도 tombstone 도 아닌 이벤트의 기록 중 판정 시각이 그
  이하인 것을 오래된 기록으로 보고 건너뛴다(stale). 과거 전이의 판정 시각은 그 이벤트가 해소된 판정 시각 이하이기
  때문이다. 이 경계는 Tier 0 판정 시계가 watermark 시각보다 더 뒤로 가지 않는다는 전제에 기댄다(1024 개 해소가
  쌓이는 시간보다 큰 시계 역행이면 새 이벤트를 오래된 기록으로 잘못 거를 수 있다). 해소 후 재개(RESUMED)는 재개
  시간(초기 10초) 안에만 나므로 그 사이에 1024 개가 밀려나지 않는 한 tombstone 으로 정확히 처리된다.
- 기록을 믿을 수 없으면(meta.state 없음·형식 위반·payload 와 불일치·다른 Tier 0·LIVE 아님) ReplayError 로 멈춘다.
  건너뛰면 활성 이벤트를 잃거나 해소된 이벤트를 되살릴 수 있다(지어내지 않는다).
- offset 은 빈 번호 없이 이어져야 한다. 체크포인트가 저널보다 앞서거나(저널이 기록을 잃음), 같은 offset 의 msg_id 가
  다르거나(다른 저널), 이어 읽을 구간이 정리로 지워졌으면 JournalMismatch 로 알린다(kind). restore 가 kind 를 보고
  멈추거나(앞섬·다른 저널) 직전 세대·저널 전체로 물러선다(정리).

Checkpointer 가 같은 투영을 이어 쓴다. 그래서 체크포인트 내용은 늘 fsync 가 끝난 저널 기록까지만 반영하고(저널보다
앞서지 않음) 그 offset 을 last_applied_journal_offset 으로 남긴다.
"""

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field

from simai_ai.eventlog import STATE_META_KEY, TRANSITION_KIND, EventJournal, JournalEntry, JournalRangeError
from simai_ai.eventlog.record import thaw
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import Origin
from simai_ai.risk.lifecycle import EventState, Lifecycle

# 해소된 이벤트의 마지막 sequence 를 기억하는 수. 체크포인트(1초마다)에 함께 쓰므로 크기를 제한한다(약 100B/개).
DEFAULT_MAX_TOMBSTONES = 1024


class ReplayError(RuntimeError):
    """저널 기록을 상태로 적용할 수 없다(손상·형식 위반·51 이전 형식). 건너뛰지 않는다."""


class LegacyJournalFormat(ReplayError):
    """meta.state 가 없는 이전 형식(S15P31A101-51 이전) 전이 기록이다."""


@dataclass(frozen=True)
class Tombstone:
    """해소된 이벤트의 마지막 sequence 와 그 해소 판정 시각."""

    event_id: str
    event_sequence: int
    evaluated_at: int


class JournalMismatch(RuntimeError):
    """투영의 위치와 저널이 맞지 않는다(앞섬·정리로 빈 구간)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # "ahead" | "different" | "compacted"


@dataclass
class ReplayStats:
    """적용 결과. 이상(gaps·unknown_updates·duplicates)은 복구 보고·로그용이고 상태는 기록대로 둔다."""

    applied: int = 0
    duplicates: int = 0  # 이미 적용한 sequence 이하라 건너뛴 기록
    stale: int = 0  # 밀려난 해소 이벤트의 기록으로 보고(판정 시각 ≤ resolved_watermark) 건너뛴 기록
    other_kinds: int = 0  # 전이가 아닌 기록
    gaps: list[tuple[str, int, int]] = field(default_factory=list)  # (event_id, 기대 sequence, 받은 sequence)
    unknown_updates: list[str] = field(default_factory=list)  # 모르는 이벤트의 RAISED·재개가 아닌 전이


class EventProjection:
    """저널을 적용한 활성 이벤트 집합(LIVE, Tier 0 하나)."""

    def __init__(
        self,
        gateway_id: str,
        *,
        events: Iterable[EventState] = (),
        last_applied_journal_offset: int | None = None,
        last_applied_msg_id: str | None = None,
        tombstones: Iterable[Tombstone] = (),
        resolved_watermark: int | None = None,
        max_tombstones: int = DEFAULT_MAX_TOMBSTONES,
    ) -> None:
        if not gateway_id:
            raise ValueError("gateway_id 가 비었다")
        if max_tombstones < 1:
            raise ValueError("max_tombstones 는 1 이상")
        self._gateway_id = gateway_id
        self._max_tombstones = max_tombstones
        self._active: dict[str, EventState] = {}
        self._tombstones: OrderedDict[str, Tombstone] = OrderedDict()
        self._watermark = resolved_watermark
        for e in events:
            if e.event_id in self._active:
                raise ReplayError(f"시작 상태에 같은 이벤트가 둘이다: {e.event_id}")
            self._check_state(e)
            if e.lifecycle is Lifecycle.RESOLVED:
                raise ReplayError(f"시작 상태에 해소된 이벤트가 있다: {e.event_id}")
            self._active[e.event_id] = e
        for t in tombstones:  # 오래된 것부터
            if t.event_id in self._active or t.event_id in self._tombstones:
                raise ReplayError(f"시작 상태의 해소 기록이 겹친다: {t.event_id}")
            self._tombstones[t.event_id] = t
        self._evict()
        self._last_applied = last_applied_journal_offset
        self._last_msg_id = last_applied_msg_id
        self.stats = ReplayStats()

    @property
    def last_applied_journal_offset(self) -> int | None:
        return self._last_applied

    @property
    def last_applied_msg_id(self) -> str | None:
        """마지막으로 적용한 기록의 msg_id(같은 저널인지 확인용)."""
        return self._last_msg_id

    @property
    def resolved_watermark(self) -> int | None:
        """밀려난 해소 기록들의 마지막 판정 시각 최댓값. 밀려난 것이 없으면 None."""
        return self._watermark

    def tombstones(self) -> tuple[Tombstone, ...]:
        """기억하는 해소 기록(오래된 것부터, 최대 max_tombstones 개)."""
        return tuple(self._tombstones.values())

    def _evict(self) -> None:
        while len(self._tombstones) > self._max_tombstones:
            _, old = self._tombstones.popitem(last=False)
            self._watermark = old.evaluated_at if self._watermark is None else max(self._watermark, old.evaluated_at)

    def active(self) -> tuple[EventState, ...]:
        """활성 이벤트(RESOLVED 가 아닌 마지막 상태). 키 순서."""
        return tuple(sorted(self._active.values(), key=lambda e: e.key.sort_key))

    def _check_state(self, state: EventState) -> None:
        if state.origin is not Origin.LIVE:
            raise ReplayError(f"LIVE 가 아닌 이벤트: {state.event_id} ({state.origin})")
        if state.gateway_id != self._gateway_id:
            raise ReplayError(f"다른 Tier 0 의 이벤트: {state.event_id} ({state.gateway_id})")

    def apply(self, entry: JournalEntry) -> None:
        """기록 하나를 적용한다. offset 은 직전 적용 다음이어야 한다."""
        offset = entry.journal_offset
        if self._last_applied is not None and offset != self._last_applied + 1:
            raise ReplayError(f"offset 이 이어지지 않는다: 직전 {self._last_applied}, 받은 값 {offset}")
        if entry.kind != TRANSITION_KIND:
            self.stats.other_kinds += 1
            self._last_applied, self._last_msg_id = offset, entry.msg_id
            return
        raw = entry.meta.get(STATE_META_KEY)
        if raw is None:
            raise LegacyJournalFormat(f"offset {offset}: 상태(meta.state)가 없는 전이 기록이라 되살릴 수 없다")
        try:
            state = EventState.from_record(thaw(raw), f"offset {offset} meta.state")
        except RiskInputError as exc:
            raise ReplayError(str(exc)) from exc
        self._check_state(state)
        payload = entry.payload
        if (
            payload.get("event_id") != state.event_id
            or payload.get("event_sequence") != state.event_sequence
            or payload.get("transition") != str(state.lifecycle)
        ):
            raise ReplayError(f"offset {offset}: payload 와 meta.state 가 다르다(손상)")
        known = self._active[state.event_id].event_sequence if state.event_id in self._active else None
        if known is None:
            tomb = self._tombstones.get(state.event_id)
            known = None if tomb is None else tomb.event_sequence
            if known is None and self._watermark is not None and state.evaluated_at <= self._watermark:
                # 활성도 tombstone 도 아닌데 밀려난 해소 기록보다 오래된 판정: 이미 해소된 이벤트의 과거 전이로 본다.
                self.stats.stale += 1
                self._last_applied, self._last_msg_id = offset, entry.msg_id
                return
            if known is None and state.lifecycle is not Lifecycle.RAISED and state.resume_count == 0:
                self.stats.unknown_updates.append(state.event_id)
        if known is not None and state.event_sequence <= known:
            self.stats.duplicates += 1
            self._last_applied, self._last_msg_id = offset, entry.msg_id
            return
        if known is not None and state.event_sequence != known + 1:
            self.stats.gaps.append((state.event_id, known + 1, state.event_sequence))
        if state.lifecycle is Lifecycle.RESOLVED:
            self._active.pop(state.event_id, None)
            self._tombstones.pop(state.event_id, None)
            self._tombstones[state.event_id] = Tombstone(state.event_id, state.event_sequence, state.evaluated_at)
            self._evict()
        else:
            self._tombstones.pop(state.event_id, None)
            self._active[state.event_id] = state
        self.stats.applied += 1
        self._last_applied, self._last_msg_id = offset, entry.msg_id

    def check_against(self, journal: EventJournal) -> int:
        """이어 적용할 첫 offset 을 돌려준다. 저널과 맞지 않으면 JournalMismatch."""
        last, first, nxt = journal.last_offset, journal.first_offset, journal.next_offset
        if self._last_applied is not None:
            if last is None or self._last_applied > last:
                raise JournalMismatch(
                    "ahead", f"체크포인트가 저널보다 앞선다: last_applied {self._last_applied}, 저널 끝 {last}"
                )
            start = self._last_applied + 1
            if self._last_msg_id is not None and first is not None and first <= self._last_applied:
                got = journal.entry(self._last_applied).msg_id
                if got != self._last_msg_id:
                    raise JournalMismatch(
                        "different",
                        f"offset {self._last_applied} 의 msg_id 가 다르다(다른 저널): {got} (기대 {self._last_msg_id})",
                    )
        else:
            start = 0
        if start < nxt and (first is None or start < first):
            raise JournalMismatch("compacted", f"이어 읽을 저널 구간이 정리돼 없다: {start} (첫 offset {first})")
        return start

    def catch_up(self, journal: EventJournal) -> int:
        """저널 끝(부르는 시점)까지 적용한다. 적용한(건너뛴 것 포함) 기록 수."""
        start = self.check_against(journal)
        n = 0
        try:
            for entry in journal.iter_from(start):
                self.apply(entry)
                n += 1
        except JournalRangeError as exc:
            raise JournalMismatch("compacted", str(exc)) from exc
        return n
