"""주기 체크포인트 (명세 3.2 재시작 복구: 초기값 1초, 저널 계약: 정리 경계).

한 번 쓸 때:
1. 투영(EventProjection)을 저널 끝(그 순간 fsync 가 끝난 기록)까지 따라잡는다. 체크포인트의 이벤트 상태는 판정
   스레드의 추적기 메모리가 아니라 저널에서 만든다. 그래서 아직 저널에 들어가지 않은(쓰기 대기 중인) 전이는 담기지 않고
   체크포인트가 저널보다 앞서는 일이 없다. last_applied_journal_offset = 투영이 마지막으로 적용한 offset 이다.
2. 판정 스레드가 update_inputs 로 넘긴 마지막 값(config_version, 마지막으로 처리한 촬영 시각, World Track 상태)을 함께
   싣는다.
3. CheckpointStore.save 로 새 세대를 원자적으로 쓴다.
4. 정리(compact)를 맡았으면 compact(store.compaction_floor()) 를 부른다. outbox.compact 를 넘기면 저널은
   min(acked_offset, last_applied_journal_offset) 이하(이전)만 정리한다. floor 는 남은 세대의 가장 작은 offset 이다.

run_once 는 부르는 스레드에서 쓴다. start 는 별도 스레드에서 interval_s 마다 run_once 를 부른다(판정 스레드에서 파일
쓰기·fsync 를 하지 않게). 쓰기가 실패하면 last_error 에 남기고 다음 주기에 다시 한다(이전 세대는 그대로). 마지막 성공에서
stall_after_s 가 지나면 stalled() 가 참이다(서비스가 health·로그로 알린다).
"""

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from simai_ai.eventlog import EventJournal
from simai_ai.recovery.checkpoint import Checkpoint, CheckpointStore
from simai_ai.recovery.projection import EventProjection
from simai_ai.risk.clock import Clock
from simai_ai.tracking.world_track import TrackCheckpoint

DEFAULT_INTERVAL_S = 1.0
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckpointInputs:
    """판정 스레드가 넘기는 체크포인트 재료(불변)."""

    config_version: str | None = None
    last_capture_time: int | None = None
    tracks_t: int | None = None
    tracks: tuple[TrackCheckpoint, ...] = ()


class Checkpointer:
    def __init__(
        self,
        journal: EventJournal,
        store: CheckpointStore,
        projection: EventProjection,
        *,
        clock: Clock,
        compact: Callable[[int | None], int] | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        stall_after_s: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_s <= 0 or stall_after_s <= 0:
            raise ValueError("interval_s·stall_after_s 는 양수")
        self._journal = journal
        self._store = store
        self._projection = projection
        self._clock = clock
        self._compact = compact
        self._interval_s = interval_s
        self._stall_after_s = stall_after_s
        self._monotonic = monotonic
        self._inputs = CheckpointInputs()
        self._lock = threading.Lock()  # 입력·상태
        self._run_lock = threading.Lock()  # 쓰기는 한 번에 하나
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_ok: float | None = None
        self._started_at: float | None = None
        self.last_checkpoint: Checkpoint | None = None
        self.last_error: BaseException | None = None
        self.compacted = 0

    def update_inputs(
        self,
        *,
        config_version: str | None,
        last_capture_time: int | None,
        tracks_t: int | None = None,
        tracks: Iterable[TrackCheckpoint] = (),
    ) -> None:
        """판정 스레드용: 바로 돌아온다. 다음 체크포인트에 이 값이 실린다."""
        inputs = CheckpointInputs(config_version, last_capture_time, tracks_t, tuple(tracks))
        with self._lock:
            self._inputs = inputs

    def run_once(self) -> Checkpoint:
        """지금 체크포인트 하나를 쓴다(투영 따라잡기 → 저장 → 정리). 실패하면 예외(last_error 에도 남김)."""
        with self._run_lock:
            try:
                self._projection.catch_up(self._journal)
                with self._lock:
                    inputs = self._inputs
                cp = self._store.save(
                    written_at=self._clock.now_ms(),
                    last_applied_journal_offset=self._projection.last_applied_journal_offset,
                    last_applied_msg_id=self._projection.last_applied_msg_id,
                    config_version=inputs.config_version,
                    last_capture_time=inputs.last_capture_time,
                    events=self._projection.active(),
                    tracks_t=inputs.tracks_t,
                    tracks=inputs.tracks,
                    tombstones=self._projection.tombstones(),
                    resolved_watermark=self._projection.resolved_watermark,
                )
                if self._compact is not None:
                    self.compacted += self._compact(self._store.compaction_floor())
            except BaseException as exc:
                with self._lock:
                    self.last_error = exc
                raise
            with self._lock:
                self.last_checkpoint = cp
                self.last_error = None
                self._last_ok = self._monotonic()
            return cp

    def stalled(self) -> bool:
        """마지막 성공(없으면 시작) 뒤 stall_after_s 가 지났는지."""
        with self._lock:
            since = self._last_ok if self._last_ok is not None else self._started_at
            return since is not None and self._monotonic() - since > self._stall_after_s

    def start(self) -> "Checkpointer":
        if self._thread is not None:
            raise RuntimeError("이미 시작했다")
        with self._lock:
            self._started_at = self._monotonic()
        self._thread = threading.Thread(target=self._run, name="tier0-checkpointer", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout_s: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - last_error 에 남기고 다음 주기에 다시 한다
                _log.warning("체크포인트를 쓰지 못했다(다음 주기에 다시 한다)", exc_info=True)
