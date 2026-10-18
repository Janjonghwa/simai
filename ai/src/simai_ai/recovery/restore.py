"""재시작 복구 (명세 3.2 재시작 복구·복구 순서, 부록 D-G3-09·11·13).

복구 순서(3.2):
1. 마지막 체크포인트를 복원한다. 최신 세대부터 보고, 믿을 수 없거나(CheckpointStore.load 가 버림: 깨짐·부분 쓰기·해시·
   형식 버전·다른 Tier 0) 이어 읽을 저널 구간이 정리됐으면 직전 세대로 물러선다. 쓸 세대가 없으면 저널 처음부터
   적용한다. 저널 앞부분이 이미 정리돼 처음부터 적용할 수도 없으면 RecoveryIncomplete 로 멈춘다(활성 이벤트를 모르는데
   '없음'으로 시작하면 진행 중인 위험을 조용히 끄게 된다). 체크포인트가 저널보다 앞서면(저널이 fsync 된 기록을 잃었거나
   다른 저널) 정본이 없으므로 RecoveryError 로 멈춘다. 체크포인트가 저널보다 뒤처진 것은 정상이다(그만큼 더 적용한다).
2. 체크포인트의 last_applied_journal_offset 다음 기록부터 저널을 offset(= 이벤트별 sequence) 순서대로 다시 적용한다
   (EventProjection, 이벤트별 sequence 로 멱등). 체크포인트에 RAISED, 저널에 그 뒤 RESOLVED 가 있으면 RESOLVED 가 최종이다.
   해소 기록(tombstone)은 체크포인트에서 이어받아, 체크포인트 전에 해소된 이벤트의 과거 전이가 그 뒤에 다시 기록돼도
   되살리지 않는다(projection 의 보장 경계 참고).
3. 남은 활성 이벤트를 LifecycleTracker.restore 로 넣는다. 검증 상태는 UNVERIFIABLE, 자동 해소하지 않고, 확인 기록·현장
   경보 단계(local_alerted, site_alarm_sequence)를 그대로 이어 현장 경보를 끄지 않는다. 복원 전이(RESTORED)는 저널에
   기록한다(판정이 아닌 전이, 근거는 직전 근거 또는 null). 복원 전이는 새 이벤트를 만들지 않고 현장 경보 신호도 내지 않는다.

World Track 은 체크포인트의 Track 을 꺼내 주기만 한다(관측 상태 UNKNOWN). 재관측 매칭·30초 넘은 체크포인트 처리는
S15P31A101-54 다. 구성 묶음은 BundleArchive 가 따로 되살리고, 여기서는 체크포인트의 config_version 을 돌려준다.

이전 형식 저널(meta.state 없음, S15P31A101-51 이전 코드)은 LegacyJournalError 로 시작을 거부한다. 운영 배포 전이라
업그레이드 bridge 는 두지 않는다(Lead 결정). 조치는 오류 메시지와 docs/design/tier0-recovery.md 에 있다.

쓰기 스레드(JournalWriter)를 시작하기 전에, 저널을 쓰기 모드로 연 단일 작성자가 부른다.
"""

import logging
from dataclasses import dataclass, replace
from enum import StrEnum

from simai_ai.eventlog import STATE_META_KEY, TRANSITION_KIND, EventJournal, JournalEntry
from simai_ai.recovery.checkpoint import Checkpoint, CheckpointStore, Rejected
from simai_ai.recovery.projection import (
    DEFAULT_MAX_TOMBSTONES,
    EventProjection,
    JournalMismatch,
    LegacyJournalFormat,
    ReplayStats,
)
from simai_ai.risk.lifecycle import EventState, LifecycleStep, LifecycleTracker
from simai_ai.tracking.world_track import TrackCheckpoint

_log = logging.getLogger(__name__)


class RecoveryError(RuntimeError):
    """복구할 수 없다(저널 손상 등). 지어내지 않고 멈춘다."""


class RecoveryIncomplete(RecoveryError):
    """쓸 수 있는 체크포인트가 없고 저널 앞부분도 정리돼 활성 이벤트를 다 알 수 없다."""


LEGACY_JOURNAL_ACTION = (
    "meta.state 없는 이전 형식 저널이다(S15P31A101-51 이전 코드가 쓴 기록). 배포 전 시험 데이터면 저널·체크포인트·"
    "acked_offset 디렉터리를 보관 위치로 옮기고 새로 시작한다. 운영 데이터라면 지우지 말고 보존한 채 Lead 에 알린다"
    "(docs/design/tier0-recovery.md 운영 절차)."
)


class LegacyJournalError(RecoveryError):
    """이전 형식(meta.state 없음) 저널이라 복구하지 않는다. 메시지에 원인과 조치를 담는다."""


def check_journal_format(journal: EventJournal) -> None:
    """저널의 첫 전이 기록에 meta.state 가 있는지 미리 본다. 없으면 LegacyJournalError.

    저널 레코드 형식(SJR1)에는 meta 형식 버전 표시가 없어, 남아 있는 첫 전이 기록의 meta.state 유무로 판단한다
    (이전 코드가 쓴 기록은 늘 저널 앞쪽에 있다). 중간 기록에 없으면 재적용 중에 같은 오류를 낸다.
    """
    first = journal.first_offset
    if first is None:
        return
    for entry in journal.iter_from(first):
        if entry.kind != TRANSITION_KIND:
            continue
        if STATE_META_KEY not in entry.meta:
            raise LegacyJournalError(f"offset {entry.journal_offset}: {LEGACY_JOURNAL_ACTION}")
        return


class RecoverySource(StrEnum):
    CHECKPOINT = "CHECKPOINT"  # 체크포인트 + 그 뒤 저널
    JOURNAL = "JOURNAL"  # 체크포인트 없이 저널 처음부터
    EMPTY = "EMPTY"  # 체크포인트도 저널 기록도 없음(첫 시작)


@dataclass(frozen=True)
class RecoveryResult:
    """복구 결과.

    restored: 복원 전이 뒤(UNVERIFIABLE) 활성 이벤트. 추적기에 들어가 있다.
    restore_step: tracker.restore 의 결과(저널에 기록한 전이). restore_entries 는 그 저널 기록.
    rejected: 버린 체크포인트(파일 손상·형식 버전·저널과 불일치)와 이유. 비어 있지 않으면 운영 로그·health 에 알린다.
    projection: 복원 전이까지 적용한 투영. Checkpointer 가 이어 쓴다.
    """

    source: RecoverySource
    checkpoint: Checkpoint | None
    rejected: tuple[Rejected, ...]
    replay_from: int | None
    last_applied_journal_offset: int | None
    stats: ReplayStats  # 저널 재적용(복원 전이 기록 전)까지의 결과
    restored: tuple[EventState, ...]
    restore_step: LifecycleStep
    restore_entries: tuple[JournalEntry, ...]
    config_version: str | None
    last_capture_time: int | None
    tracks_t: int | None
    tracks: tuple[TrackCheckpoint, ...]
    projection: EventProjection


def recover(
    *,
    journal: EventJournal,
    store: CheckpointStore,
    tracker: LifecycleTracker,
    max_tombstones: int = DEFAULT_MAX_TOMBSTONES,
) -> RecoveryResult:
    """체크포인트 복원 → 그 offset 이후 저널 재적용 → 활성 이벤트 UNVERIFIABLE 복원(저널에 기록)."""
    if tracker.gateway_id != store.gateway_id:
        raise RecoveryError(f"추적기({tracker.gateway_id})와 체크포인트({store.gateway_id})의 gateway_id 가 다르다")
    check_journal_format(journal)
    report = store.load()
    rejected = list(report.rejected)
    chosen: Checkpoint | None = None
    projection: EventProjection | None = None
    replay_from: int | None = None
    for cp in report.checkpoints:
        candidate = EventProjection(
            store.gateway_id,
            events=cp.events,
            last_applied_journal_offset=cp.last_applied_journal_offset,
            last_applied_msg_id=cp.last_applied_msg_id,
            tombstones=cp.tombstones,
            resolved_watermark=cp.resolved_watermark,
            max_tombstones=max_tombstones,
        )
        try:
            replay_from = candidate.check_against(journal)
        except JournalMismatch as exc:
            if exc.kind in ("ahead", "different"):
                # 체크포인트는 fsync 가 끝난 저널 기록까지만 반영한다(Checkpointer). 그런데 저널이 그보다 짧거나 같은
                # offset 의 msg_id 가 다르면 저널이 기록을 잃었거나 다른 저널이다. 정본을 잃었으니 어느 쪽으로도 추측하지 않는다(물러서면 진행 중인 위험을
                # 끄거나, 저널이 다시 자란 뒤 이 체크포인트가 엉뚱한 offset 으로 쓰일 수 있다).
                raise RecoveryError(
                    f"저널이 체크포인트 {cp.checkpoint_seq} 와 맞지 않는다(저널 기록 유실 또는 다른 저널): {exc}"
                ) from exc
            rejected.append(Rejected(f"checkpoint-{cp.checkpoint_seq:020d}.json", cp.checkpoint_seq, str(exc)))
            continue
        chosen, projection = cp, candidate
        break
    if projection is None:
        projection = EventProjection(store.gateway_id, max_tombstones=max_tombstones)
        try:
            replay_from = projection.check_against(journal)
        except JournalMismatch as exc:
            raise RecoveryIncomplete(
                f"쓸 수 있는 체크포인트가 없고 저널 앞부분이 정리됐다({exc}). 버린 체크포인트: {rejected}"
            ) from exc
    try:
        projection.catch_up(journal)
    except JournalMismatch as exc:  # 확인 직후 정리될 수는 없다(단일 작성자). 그래도 지어내지 않는다.
        raise RecoveryError(str(exc)) from exc
    except LegacyJournalFormat as exc:
        raise LegacyJournalError(f"{exc}. {LEGACY_JOURNAL_ACTION}") from exc
    except Exception as exc:
        raise RecoveryError(f"저널을 다시 적용하지 못했다: {exc}") from exc
    for r in rejected:
        _log.warning("체크포인트를 쓰지 않았다: %s (%s)", r.name, r.reason)
    if projection.stats.gaps or projection.stats.unknown_updates or projection.stats.duplicates:
        _log.warning("저널 재적용 이상: %s", projection.stats)

    active = projection.active()
    applied_through = projection.last_applied_journal_offset
    stats = replace(
        projection.stats, gaps=list(projection.stats.gaps), unknown_updates=list(projection.stats.unknown_updates)
    )
    step = tracker.restore(active)
    entries = journal.append_step(step)
    projection.catch_up(journal)  # 복원 전이도 투영에 반영(체크포인트가 저널과 같은 상태를 이어 쓰게)
    if chosen is not None:
        source = RecoverySource.CHECKPOINT
    elif applied_through is None:
        source = RecoverySource.EMPTY
    else:
        source = RecoverySource.JOURNAL
    return RecoveryResult(
        source=source,
        checkpoint=chosen,
        rejected=tuple(rejected),
        replay_from=replay_from,
        last_applied_journal_offset=applied_through,
        stats=stats,
        restored=tracker.active(),
        restore_step=step,
        restore_entries=entries,
        config_version=None if chosen is None else chosen.config_version,
        last_capture_time=None if chosen is None else chosen.last_capture_time,
        tracks_t=None if chosen is None else chosen.tracks_t,
        tracks=() if chosen is None else chosen.tracks,
        projection=projection,
    )
