"""이벤트 저널·outbox (명세 3.2 저널 계약, 6.4, 15.1 G3, 부록 D-G3).

RunPod Tier 0 전용이다(EC2 컨테이너는 Risk Engine 만 쓰고 저널을 쓰지 않는다). 표준 라이브러리만 쓴다.
패키지 이름이 journal 이 아닌 것은 저장소 .gitignore 가 런타임 저널 데이터 디렉터리(journal/)를 막기 때문이다.

- EventJournal: 단일 작성자 추가 전용 파일, 기록마다 fsync, 전역 journal_offset, 깨진 꼬리 복구, 읽기 API.
- JournalWriter: 판정 스레드와 분리한 쓰기 스레드(시간 제한·정지 감시).
- AckedOffsetStore: acked_offset 원자적 영속 저장.
- Outbox: acked_offset 이후 구간 EC2 전송(전송 주입), journal_ack 반영, 정리. I1 변환(to_i1_transition_payload,
  확인 → 계약 operator_ack)은 보낼 때만 하고 저널 기록은 바꾸지 않는다.
"""

from simai_ai.eventlog.acked import ACKED_NAME, AckedOffsetError, AckedOffsetStore
from simai_ai.eventlog.journal import (
    STATE_META_KEY,
    TRANSITION_KIND,
    EventJournal,
    JournalConfig,
    JournalCorruptError,
    JournalError,
    JournalRangeError,
    JournalWriteError,
    RecoveryReport,
    Reservation,
    ack_id_of,
    evidence_ref_of,
    is_judgment_transition,
)
from simai_ai.eventlog.outbox import (
    SCHEMA_VERSION,
    Blocked,
    JournalAckResult,
    Outbox,
    OutboxError,
    TransportError,
    UnsendableEntry,
    build_transition_message,
    operator_ack,
    to_i1_transition_payload,
)
from simai_ai.eventlog.record import JournalEntry, JournalFormatError
from simai_ai.eventlog.writer import JournalTimeout, JournalWriter

__all__ = [
    "ACKED_NAME",
    "SCHEMA_VERSION",
    "STATE_META_KEY",
    "TRANSITION_KIND",
    "AckedOffsetError",
    "AckedOffsetStore",
    "Blocked",
    "EventJournal",
    "JournalAckResult",
    "JournalConfig",
    "JournalCorruptError",
    "JournalEntry",
    "JournalError",
    "JournalFormatError",
    "JournalRangeError",
    "JournalTimeout",
    "JournalWriteError",
    "JournalWriter",
    "Outbox",
    "OutboxError",
    "RecoveryReport",
    "Reservation",
    "TransportError",
    "UnsendableEntry",
    "ack_id_of",
    "build_transition_message",
    "evidence_ref_of",
    "is_judgment_transition",
    "operator_ack",
    "to_i1_transition_payload",
]
