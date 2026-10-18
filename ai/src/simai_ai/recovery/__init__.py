"""Tier 0 재시작 복구: 체크포인트와 저널 재적용 (명세 3.2 재시작 복구·복구 순서·저널 계약, 6.2, 부록 D-G3-09·11·13).

RunPod Tier 0 전용이다. 이벤트 저널(eventlog)이 정본이고 체크포인트는 그 시점까지의 요약이다.

| 모듈 | 내용 |
|---|---|
| checkpoint | CheckpointStore: 체크포인트 세대 파일(형식 버전·sha256), 원자적 쓰기, 손상·버전 불일치 감지, 정리 하한 |
| projection | EventProjection: 저널 기록(meta.state)을 offset 순서로 적용한 활성 이벤트, 이벤트별 sequence 멱등, 해소 기록 |
| restore | recover: 체크포인트 복원 → 그 offset 이후 저널 재적용 → 활성 이벤트 UNVERIFIABLE 복원(저널에 기록) |
| checkpointer | Checkpointer: 1초 주기 체크포인트(저널에서 만든 상태 + 구성 버전·촬영 시각·Track), 저널 정리 |
| health_gate | RecoveryHealthGate: 재관측·시간 동기화 확보 전 System Health UNSAFE_UNKNOWN |

World Track 은 저장하고 꺼내 주기만 한다(TrackCheckpoint, 관측 상태 UNKNOWN). 재관측 매칭은 S15P31A101-54, 진입점
배선은 S15P31A101-171 이다.
"""

from simai_ai.recovery.checkpoint import (
    FORMAT,
    FORMAT_VERSION,
    Checkpoint,
    CheckpointError,
    CheckpointInvalid,
    CheckpointStore,
    LoadReport,
    Rejected,
)
from simai_ai.recovery.checkpointer import DEFAULT_INTERVAL_S, Checkpointer, CheckpointInputs
from simai_ai.recovery.health_gate import RecoveryHealthGate
from simai_ai.recovery.projection import (
    DEFAULT_MAX_TOMBSTONES,
    EventProjection,
    JournalMismatch,
    LegacyJournalFormat,
    ReplayError,
    ReplayStats,
    Tombstone,
)
from simai_ai.recovery.restore import (
    LegacyJournalError,
    RecoveryError,
    RecoveryIncomplete,
    RecoveryResult,
    RecoverySource,
    check_journal_format,
    recover,
)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_MAX_TOMBSTONES",
    "FORMAT",
    "FORMAT_VERSION",
    "Checkpoint",
    "CheckpointError",
    "CheckpointInputs",
    "CheckpointInvalid",
    "CheckpointStore",
    "Checkpointer",
    "EventProjection",
    "JournalMismatch",
    "LegacyJournalError",
    "LegacyJournalFormat",
    "LoadReport",
    "RecoveryError",
    "RecoveryHealthGate",
    "RecoveryIncomplete",
    "RecoveryResult",
    "RecoverySource",
    "Rejected",
    "ReplayError",
    "ReplayStats",
    "Tombstone",
    "check_journal_format",
    "recover",
]
