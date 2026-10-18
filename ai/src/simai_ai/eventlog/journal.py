"""이벤트 저널 (명세 3.2 저널 계약·재시작 복구, 6.4, 15.1 G3, 부록 D-G3-07·11·13·14·18).

LIVE RiskEvent 전이의 내구성 로그이자 재시작 복구의 정본이다. Tier 0 Pod 와 같은 데이터센터의 Network Volume 에 둔다.

- 단일 작성자 추가 전용 파일. 쓰기 모드로 열면 작성자 잠금(_lock.WriterLock)을 쥔다.
- 기록마다 fsync. append 가 돌아오면 그 기록은 디스크에 있다. outbox(EC2 전송)는 append 가 돌아온 기록만 읽으므로
  EC2 로 가는 전이는 늘 fsync 뒤에 나간다(D-G3-18). 현장 경보는 이 저널을 거치지 않는다(writer.JournalWriter).
- 모든 기록에 전역 단조 증가 journal_offset 을 붙인다(0 부터, 빈 번호 없음). 이벤트별 event_sequence 와 다른 값이다.
  저널은 payload 의 event_sequence 를 해석하지 않고 그대로 둔다.
- 덮어쓰지 않는다(AGENTS.md 규칙 6). 기록은 파일 끝에만 붙인다. 고치는 경우는 두 가지뿐이다.
  1) 시작할 때 깨진 꼬리(append 가 돌아오지 못한 마지막 레코드의 일부)를 잘라 낸다. 그 기록은 저장된 적이 없다.
  2) 정리(compact): min(acked_offset, last_applied_journal_offset) 이하이고 보관 기간이 지난 세그먼트 파일을 통째로
     지운다. 활성(마지막) 세그먼트는 지우지 않는다.
- 세그먼트: 파일 이름이 첫 journal_offset(20자리)인 `*.journal`. 크기가 상한을 넘으면 다음 기록부터 새 파일.
  offset 은 빈 번호 없이 이어지므로 마지막 offset 은 늘 next_offset-1 이다. 회전 직후 죽어 빈 세그먼트만 남고 앞
  세그먼트가 정리돼도 빈 세그먼트의 이름이 다음 offset 을 지킨다.
- 근거 색인(evidence_index.json): 활성 이벤트(마지막 전이가 RESOLVED 가 아님)의 마지막 판정 근거(world_state msg_id).
  정리로 기록이 지워지기 전에 원자적으로 저장하고, 시작할 때 읽은 뒤 남은 세그먼트로 덮는다. 확인·복원·ID 교정
  전이(lifecycle 이 evidence_ref 를 싣지 않음)의 meta.evidence_ref 를 정리·재시작 뒤에도 채우기 위해서다.
- 운영자 확인(EC2 event_ack, 현장 site_ack)도 전이(LifecycleTracker.acknowledge 의 결과)로 기록한다. payload 는
  lifecycle 의 to_payload 그대로이며 저널은 필드를 고치지 않는다(lifecycle 이 필드를 더해도 그대로 남는다).

- 전이 기록의 meta.state 에 전이 뒤 이벤트의 완전한 상태(EventState.to_record)를 남긴다. 이벤트 상태 재적용(체크포인트 +
  last_applied_journal_offset 이후 전이, 3.2 복구 순서)은 simai_ai.recovery 가 read 와 이 값으로 한다(S15P31A101-51).
"""

import json
import os
import re
import threading
import uuid
from array import array
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self

from simai_ai.eventlog._fs import O_BINARY, atomic_write, fsync_dir, write_all
from simai_ai.eventlog._lock import WriterLock
from simai_ai.eventlog.record import MAGIC, JournalEntry, decode_at, encode
from simai_ai.risk.clock import Clock

SEGMENT_SUFFIX = ".journal"
LOCK_NAME = "journal.lock"
EVIDENCE_NAME = "evidence_index.json"
# 계약 ack_channel, lifecycle AckActor → operator_ack.actor_role (AckActor 는 -161 이 actor_role 에서 정한다, 1:1)
ACK_CHANNELS = frozenset({"CONTROL_ROOM", "FOREMAN_PWA", "SITE_BUTTON"})
ACK_ACTOR_ROLE = {"ADMIN": "ADMIN", "FOREMAN": "FOREMAN", "SITE_BUTTON": "SITE"}
TRANSITION_KIND = "risk_event_transition"
# 전이 기록 meta 에 남기는 전이 뒤 이벤트 상태(lifecycle EventState.to_record, S15P31A101-51). payload 만으로는
# 확인 기록 전체·현장 경보 단계·발생 시각 등이 빠져 재시작 복구(recovery)가 상태를 되살릴 수 없다. I1 로는 보내지 않는다.
STATE_META_KEY = "state"
# 판정이 아닌 전이(운영자 확인·재시작 복원·ID 교정)의 lifecycle 변경 사유. 이 전이는 world_state 판정 근거를 모를 수 있고,
# 계약(risk_event_transition 1.4.0)은 그때 evidence_ref 를 null 로 둔다. VERIFICATION 은 복원 전이에 함께 붙는다.
NON_JUDGMENT_CHANGES = frozenset({"ACKNOWLEDGED", "RESTORED", "IDENTITY_CORRECTED", "IDENTITY_CONFLICT"})
_NON_JUDGMENT_ALLOWED = NON_JUDGMENT_CHANGES | {"VERIFICATION"}
_SEGMENT_RE = re.compile(r"^(\d{20})\.journal$")

Fsync = Callable[[int], None]


class JournalError(RuntimeError):
    """저널 사용 오류."""


class JournalCorruptError(JournalError):
    """꼬리가 아닌 곳이 깨졌거나 offset 이 이어지지 않는다. 자동으로 고치지 않는다(기록을 버리게 되므로)."""


class JournalWriteError(JournalError):
    """기록·fsync 실패. 이후 쓰기를 거부한다. 다시 열면(재시작) 깨진 꼬리를 잘라 내고 이어 쓴다."""


class JournalRangeError(JournalError):
    """정리돼 없거나 아직 없는 offset 을 읽으려 했다."""


@dataclass(frozen=True)
class JournalConfig:
    """세그먼트 크기 상한과 정리 보관 기간(초기값, 운영 실측 후 조정)."""

    segment_max_bytes: int = 64 * 1024 * 1024
    # 정리 조건을 채워도 마지막 기록이 이 시간보다 최근인 세그먼트는 남긴다(재전송 요청·현장 감사 여유).
    retention_ms: int = 24 * 60 * 60 * 1000

    def __post_init__(self) -> None:
        if self.segment_max_bytes <= 0:
            raise ValueError("segment_max_bytes 는 양수")
        if self.retention_ms < 0:
            raise ValueError("retention_ms 는 0 이상")


@dataclass(frozen=True)
class RecoveryReport:
    """시작할 때 저널을 끝까지 읽은 결과."""

    first_offset: int | None  # 남아 있는 첫 offset (비었으면 None)
    last_offset: int | None  # 마지막 온전한 offset (비었으면 None)
    next_offset: int  # 다음 기록이 받을 offset
    entries: int
    segments: int
    torn_tail: bool  # 깨진 꼬리가 있었는지
    truncated_bytes: int  # 잘라 낸(읽기 전용이면 발견만 한) 꼬리 바이트


@dataclass(frozen=True)
class Reservation:
    """prepare_step 이 대기 중으로 올린 값. 넣기가 실패하면 EventJournal.release 로 되돌린다."""

    ack_ids: tuple[str, ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()


class TransitionLike(Protocol):
    def to_payload(self) -> dict[str, Any]: ...


class StepLike(Protocol):
    @property
    def transitions(self) -> Sequence[TransitionLike]: ...


class _Segment:
    def __init__(self, first_offset: int, path: Path) -> None:
        self.first_offset = first_offset
        self.path = path
        self.positions = array("q")
        self.size = 0
        self.last_recorded_at: int | None = None

    @property
    def count(self) -> int:
        return len(self.positions)

    @property
    def end_offset(self) -> int:
        """이 세그먼트 다음 offset."""
        return self.first_offset + len(self.positions)


def segment_name(first_offset: int) -> str:
    return f"{first_offset:020d}{SEGMENT_SUFFIX}"


def _nonempty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def ack_id_of(entry: JournalEntry) -> str | None:
    """기록의 확인 ack_id: meta.ack_id(저널이 받은 계약 값), 없으면 payload.ack.ack_id(lifecycle 이 싣게 되면)."""
    ack_id = _nonempty_str(entry.meta.get("ack_id"))
    if ack_id is None:
        ack = entry.payload.get("ack")
        if isinstance(ack, Mapping):
            ack_id = _nonempty_str(ack.get("ack_id"))
    return ack_id


def evidence_ref_of(entry: JournalEntry) -> str | None:
    """판정 근거 world_state msg_id: payload.evidence_ref, 없으면 meta.evidence_ref(그 이벤트의 직전 근거)."""
    return _nonempty_str(entry.payload.get("evidence_ref")) or _nonempty_str(entry.meta.get("evidence_ref"))


def is_judgment_transition(payload: Mapping[str, Any]) -> bool:
    """판정 전이인지(근거 world_state 가 있어야 하는지). 모르면 판정으로 본다(근거 없이 보내지 않는 쪽).

    판정이 아닌 전이: 확인(ack 를 싣거나 transition=ACKNOWLEDGED), 또는 재개가 아닌 UPDATED 중 변경 사유가 모두
    복원·ID 교정(과 복원에 붙는 VERIFICATION)인 것. RAISED·RESOLVED·재개·판정 UPDATED 는 판정 전이다.
    """
    if payload.get("ack") is not None or payload.get("transition") == "ACKNOWLEDGED":
        return False
    if payload.get("transition") != "UPDATED" or payload.get("resumed") is True:
        return True
    changes = payload.get("changes")
    if not isinstance(changes, (list, tuple)) or not changes:
        return True
    names = {str(c) for c in changes}
    return not (names & NON_JUDGMENT_CHANGES and names <= _NON_JUDGMENT_ALLOWED)


def _state_record_of(transition: Any) -> dict[str, Any] | None:
    """전이 뒤 이벤트의 완전한 상태(lifecycle EventState.to_record). 그런 상태가 없는 전이면 None.

    저널은 lifecycle 을 import 하지 않으므로 덕 타이핑으로 읽는다.
    """
    to_record = getattr(getattr(transition, "state", None), "to_record", None)
    return to_record() if callable(to_record) else None


def _valid_record_after(data: bytes, pos: int) -> bool:
    """pos 뒤에 온전한 레코드가 하나라도 있는지. 있으면 꼬리가 아니라 중간 손상이다."""
    i = data.find(MAGIC, pos + 1)
    while i != -1:
        if decode_at(data, i) is not None:
            return True
        i = data.find(MAGIC, i + 1)
    return False


class EventJournal:
    """이벤트 저널. 쓰기는 한 스레드(writer.JournalWriter)에서 하고, 읽기는 다른 스레드에서 해도 된다."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock,
        config: JournalConfig,
        fsync: Fsync,
        new_msg_id: Callable[[], str],
        readonly: bool,
    ) -> None:
        self._root = root
        self._clock = clock
        self._config = config
        self._fsync = fsync
        self._new_msg_id = new_msg_id
        self._readonly = readonly
        self._lock = threading.Lock()  # 색인·상태
        self._write_lock = threading.Lock()  # 작성자 하나(append·정리·닫기)
        self._writer_lock: WriterLock | None = None
        self._segments: list[_Segment] = []
        self._next_offset = 0
        self._fd: int | None = None
        self._broken: str | None = None
        self._closed = False
        self._ack_ids: set[str] = set()
        self._pending_ack_ids: set[str] = set()  # 쓰기 대기 중(fsync 전)인 확인
        self._pending_evidence: dict[str, str] = {}  # 쓰기 대기 중인 단계가 실은 판정 근거
        self._evidence: dict[str, str] = {}  # event_id → 마지막 판정 근거(world_state msg_id)
        self._resolved: set[str] = set()  # 마지막 전이가 RESOLVED 인 이벤트
        self.report = RecoveryReport(None, None, 0, 0, 0, False, 0)

    # --- 열기·복구 ----------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        root: str | os.PathLike[str],
        *,
        clock: Clock,
        config: JournalConfig | None = None,
        fsync: Fsync = os.fsync,
        new_msg_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        readonly: bool = False,
    ) -> "EventJournal":
        """저널을 연다. 끝까지 읽어 마지막 offset 을 복원하고, 쓰기 모드면 깨진 꼬리를 잘라 낸다.

        readonly=True 는 잠금을 쥐지 않고 아무것도 고치지 않는다(점검·복구 도구용). 깨진 꼬리는 report 로만 알린다.
        """
        path = Path(root)
        if readonly:
            if not path.is_dir():
                raise JournalError(f"저널 디렉터리가 없다: {path}")
        else:
            path.mkdir(parents=True, exist_ok=True)
        journal = cls(
            path,
            clock=clock,
            config=config or JournalConfig(),
            fsync=fsync,
            new_msg_id=new_msg_id,
            readonly=readonly,
        )
        if not readonly:
            journal._writer_lock = WriterLock(path / LOCK_NAME)
            journal._writer_lock.acquire()
        try:
            journal._recover()
            if not readonly and journal._segments:
                journal._open_active(journal._segments[-1])
        except BaseException:
            journal.close()
            raise
        return journal

    def _load_evidence_index(self) -> None:
        path = self._root / EVIDENCE_NAME
        tmp = path.with_name(path.name + ".tmp")
        if tmp.exists() and not self._readonly:
            tmp.unlink()  # 바꾸기 전에 죽은 흔적. 이전 색인이 유효하다
        if not path.exists():
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise JournalCorruptError(f"근거 색인이 깨졌다: {path}") from exc
        events = doc.get("evidence") if isinstance(doc, dict) else None
        if not isinstance(events, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and k and v for k, v in events.items()
        ):
            raise JournalCorruptError(f"근거 색인 형식 위반: {path}")
        self._evidence.update(events)

    def _save_evidence_index_locked(self) -> None:
        active = {e: ref for e, ref in sorted(self._evidence.items()) if e not in self._resolved}
        data = json.dumps({"evidence": active}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        atomic_write(self._root / EVIDENCE_NAME, data, self._fsync)

    def _recover(self) -> None:
        self._load_evidence_index()
        names = []
        for p in self._root.iterdir():
            if not p.name.endswith(SEGMENT_SUFFIX):
                continue
            m = _SEGMENT_RE.match(p.name)
            if m is None:
                raise JournalCorruptError(f"세그먼트 이름이 형식에 맞지 않는다: {p.name}")
            names.append((int(m.group(1)), p))
        names.sort()
        expected: int | None = None
        torn = False
        truncated = 0
        for i, (first, p) in enumerate(names):
            if expected is not None and first != expected:
                raise JournalCorruptError(f"세그먼트 사이 offset 이 이어지지 않는다: {expected} 다음이 {p.name}")
            seg = _Segment(first, p)
            data = p.read_bytes()
            pos, off = 0, first
            while pos < len(data):
                d = decode_at(data, pos)
                if d is None:
                    break
                if d.entry.journal_offset != off:
                    raise JournalCorruptError(f"{p.name}@{pos}: offset {d.entry.journal_offset}, 기대 {off}")
                seg.positions.append(pos)
                seg.last_recorded_at = d.entry.recorded_at
                self._index_locked(d.entry)
                pos += d.size
                off += 1
            if pos < len(data):
                if i != len(names) - 1:
                    raise JournalCorruptError(f"마지막이 아닌 세그먼트가 깨졌다: {p.name}@{pos}")
                if _valid_record_after(data, pos):
                    raise JournalCorruptError(f"꼬리가 아닌 곳이 깨졌다: {p.name}@{pos}")
                torn = True
                truncated = len(data) - pos
                if not self._readonly:
                    self._truncate(p, pos)
            seg.size = pos
            self._segments.append(seg)
            expected = off
        self._next_offset = expected if expected is not None else 0
        count = sum(s.count for s in self._segments)
        self.report = RecoveryReport(
            first_offset=self._first_offset_locked(),
            last_offset=self._last_offset_locked(),
            next_offset=self._next_offset,
            entries=count,
            segments=len(self._segments),
            torn_tail=torn,
            truncated_bytes=truncated,
        )

    def _truncate(self, path: Path, size: int) -> None:
        fd = os.open(path, os.O_RDWR | O_BINARY)
        try:
            os.ftruncate(fd, size)
            self._fsync(fd)
        finally:
            os.close(fd)

    def _open_active(self, seg: _Segment) -> None:
        flags = os.O_WRONLY | os.O_APPEND | O_BINARY
        self._fd = os.open(seg.path, flags)

    # --- 상태 ---------------------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def next_offset(self) -> int:
        with self._lock:
            return self._next_offset

    @property
    def last_offset(self) -> int | None:
        """마지막 기록의 journal_offset(next_offset-1). 한 번도 기록하지 않았으면 None.

        offset 은 빈 번호 없이 이어지므로 정리로 기록 파일이 모두 지워져도(빈 활성 세그먼트만 남음) 이 값은 남는다.
        """
        with self._lock:
            return self._last_offset_locked()

    @property
    def first_offset(self) -> int | None:
        """남아 있는(정리되지 않은) 첫 journal_offset. 기록이 없으면 None."""
        with self._lock:
            return self._first_offset_locked()

    def _last_offset_locked(self) -> int | None:
        return self._next_offset - 1 if self._next_offset > 0 else None

    def _first_offset_locked(self) -> int | None:
        for s in self._segments:
            if s.count:
                return s.first_offset
        return None

    def has_ack_id(self, ack_id: str) -> bool:
        """이 ack_id 의 확인을 이미 기록했거나 기록 대기 중인지(site_ack 중복 수신은 한 번만 기록, 3.10).

        prepare_step(JournalWriter.submit_step)에 넣은 순간부터 참이다. 정리된 세그먼트의 ack_id 는 재시작 뒤 모른다.
        """
        with self._lock:
            return ack_id in self._ack_ids or ack_id in self._pending_ack_ids

    # --- 쓰기 ---------------------------------------------------------------------------

    def _index_locked(self, entry: JournalEntry) -> None:
        ack_id = ack_id_of(entry)
        if ack_id is not None:
            self._ack_ids.add(ack_id)
            self._pending_ack_ids.discard(ack_id)
        event_id = _nonempty_str(entry.payload.get("event_id"))
        if event_id is None:
            return
        evidence = evidence_ref_of(entry)
        if evidence is not None:
            self._evidence[event_id] = evidence
            if self._pending_evidence.get(event_id) == evidence:
                del self._pending_evidence[event_id]
        if entry.payload.get("transition") == "RESOLVED":
            self._resolved.add(event_id)
        else:
            self._resolved.discard(event_id)

    def append(self, kind: str, payload: Mapping[str, Any], meta: Mapping[str, Any] | None = None) -> JournalEntry:
        """기록 하나를 붙이고 fsync 가 끝난 뒤 돌려준다. journal_offset·msg_id·recorded_at 은 저널이 붙인다.

        payload 는 넘긴 그대로 남긴다. meta 는 payload 밖의 덧붙임이다(append_transition).
        """
        if "journal_offset" in payload or "resent" in payload:
            raise JournalError("journal_offset·resent 는 저널·전송이 붙인다. payload 에 넣지 않는다")
        if kind == TRANSITION_KIND and payload.get("source") != "LIVE":
            raise JournalError(f"Tier 0 이벤트 저널은 LIVE 전이만 기록한다: source={payload.get('source')!r}")
        # 쓰기 잠금이 작성자를 하나로 줄인다. 상태 잠금은 색인을 바꿀 때만 잡아 fsync 동안에도 읽기(outbox)가 돈다.
        with self._write_lock:
            with self._lock:
                self._check_writable()
                offset = self._next_offset
                seg = self._segments[-1] if self._segments else None
            msg_id = self._new_msg_id()
            recorded_at = self._clock.now_ms()
            record = encode(offset, kind, msg_id, recorded_at, payload, meta)
            if seg is None or (seg.count and seg.size + len(record) > self._config.segment_max_bytes):
                seg = self._rotate(offset)
            assert self._fd is not None
            try:
                write_all(self._fd, record)
                self._fsync(self._fd)
            except BaseException as exc:
                # 디스크 상태를 모른다. 이어 쓰면 offset 이 어긋날 수 있으니 멈추고, 재시작이 꼬리를 정리한다.
                with self._lock:
                    self._broken = f"{type(exc).__name__}: {exc}"
                raise JournalWriteError(f"저널 기록 실패(offset {offset}): {exc}") from exc
            decoded = decode_at(record, 0)
            assert decoded is not None
            entry = decoded.entry
            with self._lock:
                seg.positions.append(seg.size)
                seg.size += len(record)
                seg.last_recorded_at = recorded_at
                self._next_offset = offset + 1
                self._index_locked(entry)
            return entry

    @staticmethod
    def _transition_meta(payload: Mapping[str, Any], ack_id: str | None) -> dict[str, Any]:
        """전이 payload 를 검사하고 meta(ack_id)를 만든다. 쓰기 전에 부르는 쪽 스레드에서도 부른다(prepare_step)."""
        if payload.get("source") != "LIVE":
            raise JournalError(f"Tier 0 이벤트 저널은 LIVE 전이만 기록한다: source={payload.get('source')!r}")
        meta: dict[str, Any] = {}
        ack = payload.get("ack")
        if ack is None:
            if ack_id is not None:
                raise JournalError("확인 전이가 아닌데 ack_id 를 받았다")
            return meta
        if not isinstance(ack, Mapping):
            raise JournalError("payload.ack 가 객체가 아니다")
        own = _nonempty_str(ack.get("ack_id"))
        if own is not None and ack_id is not None and own != ack_id:
            raise JournalError(f"확인 ack_id 가 다르다: payload {own!r}, 인자 {ack_id!r}")
        resolved = own or _nonempty_str(ack_id)
        if resolved is None:
            raise JournalError("확인 전이에는 계약 ack_id(event_ack·site_ack 의 ack.ack_id)가 있어야 한다")
        # I1 operator_ack 을 지어내지 않도록 받은 채널과 확인 주체를 쓰기 전에 확인한다.
        if ack.get("channel") not in ACK_CHANNELS:
            raise JournalError(f"확인 채널을 모른다(계약 값을 그대로 받아야 한다): {ack.get('channel')!r}")
        role = ACK_ACTOR_ROLE.get(str(ack.get("actor")))
        if role is None:
            raise JournalError(f"모르는 확인 주체: {ack.get('actor')!r}")
        # 계약 operator_ack: 현장 확인 버튼은 노트북 기기(SITE)만, 관제 화면·PWA 는 계정(ADMIN·FOREMAN)만.
        if (ack.get("channel") == "SITE_BUTTON") != (role == "SITE"):
            raise JournalError(f"확인 채널 {ack.get('channel')} 과 주체 {ack.get('actor')} 가 맞지 않는다")
        meta["ack_id"] = resolved
        return meta

    def prepare_step(self, step: StepLike, *, ack_id: str | None = None) -> "Reservation":
        """쓰기 전에(부르는 쪽 스레드에서) 단계를 검사하고, 확인 ack_id 와 판정 근거를 대기 중으로 올린다.

        잘못된 단계는 여기서 바로 오류를 내 쓰기 스레드에서 조용히 사라지거나 보낼 수 없는 기록(outbox 영구 보류)이
        저널에 들어가지 않게 한다: LIVE 아님, 확인인데 ack_id·채널 없음, 채널과 확인 주체가 맞지 않음, 판정 전이인데
        판정 근거(evidence_ref)를 payload·저널·대기 중 어디서도 모름(판정이 아닌 전이는 근거 없이 기록, 계약 1.4.0). 올린 ack_id 는 has_ack_id 가 바로 안다(같은 site_ack 를
        fsync 전에 다시 받아도 한 번만 기록). 넣기가 실패하면 돌려준 Reservation 으로 release 해 되돌린다.
        """
        acks: list[str] = []
        evidence: dict[str, str] = {}
        with self._lock:
            known = dict(self._evidence)
            known.update(self._pending_evidence)
        for t in step.transitions:
            payload = t.to_payload()
            meta = self._transition_meta(payload, ack_id if payload.get("ack") is not None else None)
            if "ack_id" in meta:
                acks.append(meta["ack_id"])
            event_id = str(payload.get("event_id"))
            ref = _nonempty_str(payload.get("evidence_ref"))
            if ref is not None:
                evidence[event_id] = ref
            elif event_id not in evidence and event_id not in known and is_judgment_transition(payload):
                raise JournalError(f"판정 근거(evidence_ref)를 모르는 전이: event_id={event_id} (지어내지 않는다)")
        with self._lock:
            self._check_writable()
            new_acks = tuple(a for a in dict.fromkeys(acks) if a not in self._pending_ack_ids)
            self._pending_ack_ids.update(new_acks)
            new_evidence = tuple((e, r) for e, r in evidence.items() if self._pending_evidence.get(e) != r)
            self._pending_evidence.update(new_evidence)
        return Reservation(new_acks, new_evidence)

    def release(self, reservation: "Reservation") -> None:
        """prepare_step 이 올린 대기 중 값을 되돌린다(쓰기 대기열에 넣지 못했을 때). 이미 기록된 것은 그대로 둔다."""
        with self._lock:
            for a in reservation.ack_ids:
                self._pending_ack_ids.discard(a)
            for e, r in reservation.evidence:
                if self._pending_evidence.get(e) == r:
                    del self._pending_evidence[e]

    def append_transition(self, transition: TransitionLike, *, ack_id: str | None = None) -> JournalEntry:
        """LifecycleTracker 가 낸 전이(확인 포함)를 그대로 기록한다. Tier 0 저널은 LIVE 만 받는다(규칙 5).

        확인 전이(payload.ack 가 있음)는 계약 ack_id(event_ack·site_ack 의 ack.ack_id)와 받은 채널(ack.channel)이
        있어야 하고, 채널과 확인 주체가 계약 짝(SITE_BUTTON ↔ SITE)이어야 기록한다. lifecycle 의 AckRecord 에 ack_id 가
        없으면 ack_id 인자로 받아 meta 에 둔다. I1 로 보낼 때 operator_ack 으로 바꾸고(outbox), 중복 수신 확인
        (has_ack_id)에 쓴다. 같은 ack_id 를 두 번 기록하지 않는 것은 부르는 쪽이 has_ack_id 로 먼저 확인한다.

        payload 의 evidence_ref 가 비었으면(확인·복원 전이) 그 이벤트의 직전 판정 근거를 meta.evidence_ref 로 남긴다.
        판정 전이의 근거를 모르면 지어내지 않고 기록 전에 거절한다(보낼 수 없는 기록이 저널에 남아 outbox 를 막지 않게).
        판정이 아닌 전이(확인·복원·ID 교정)는 근거를 몰라도 기록한다. outbox 가 evidence_ref=null 로 보낸다(계약 1.4.0).
        """
        payload = transition.to_payload()
        meta = self._transition_meta(payload, ack_id)
        state_record = _state_record_of(transition)
        if state_record is not None:
            meta[STATE_META_KEY] = state_record
        if _nonempty_str(payload.get("evidence_ref")) is None:
            event_id = str(payload.get("event_id"))
            with self._lock:
                previous = self._evidence.get(event_id)
            if previous is not None:
                meta["evidence_ref"] = previous
            elif is_judgment_transition(payload):
                raise JournalError(f"판정 근거(evidence_ref)를 모르는 전이: event_id={event_id} (지어내지 않는다)")
        return self.append(TRANSITION_KIND, payload, meta)

    def append_step(self, step: StepLike, *, ack_id: str | None = None) -> tuple[JournalEntry, ...]:
        """LifecycleStep 의 전이를 순서대로 기록한다. 하나씩 fsync 하므로 중간에 죽으면 앞 전이까지만 남는다.

        ack_id 는 확인 단계(LifecycleTracker.acknowledge 의 결과)일 때 그 확인 전이에 붙인다.
        """
        return tuple(
            self.append_transition(t, ack_id=ack_id if t.to_payload().get("ack") is not None else None)
            for t in step.transitions
        )

    def _check_writable(self) -> None:
        if self._closed:
            raise JournalError("닫힌 저널")
        if self._readonly:
            raise JournalError("읽기 전용으로 연 저널")
        if self._broken is not None:
            raise JournalWriteError(f"앞선 기록 실패로 쓰기를 멈췄다: {self._broken}")

    def _rotate(self, first_offset: int) -> _Segment:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        path = self._root / segment_name(first_offset)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | O_BINARY
        try:
            self._fd = os.open(path, flags, 0o644)
            fsync_dir(self._root)
        except BaseException as exc:
            with self._lock:
                self._broken = f"{type(exc).__name__}: {exc}"
            raise JournalWriteError(f"세그먼트를 만들지 못했다: {path}: {exc}") from exc
        seg = _Segment(first_offset, path)
        with self._lock:
            self._segments.append(seg)
        return seg

    # --- 읽기 ---------------------------------------------------------------------------

    def read(self, from_offset: int, limit: int | None = None) -> list[JournalEntry]:
        """from_offset 부터 순서대로 읽는다(최대 limit 개). from_offset 이 끝이면 빈 목록."""
        return list(self.iter_from(from_offset, limit))

    def iter_from(self, from_offset: int, limit: int | None = None) -> Iterator[JournalEntry]:
        """from_offset 부터 지금 기록된 끝까지(부르는 시점 기준) 차례로 낸다."""
        if from_offset < 0:
            raise JournalRangeError(f"offset 은 0 이상: {from_offset}")
        with self._lock:
            if self._closed:
                raise JournalError("닫힌 저널")
            end = self._next_offset
            first = self._first_offset_locked()
            if from_offset > end:
                raise JournalRangeError(f"아직 없는 offset: {from_offset} (다음 {end})")
            if from_offset < end and (first is None or from_offset < first):
                raise JournalRangeError(f"정리돼 없는 offset: {from_offset} (첫 {first})")
            if limit is not None:
                end = min(end, from_offset + max(limit, 0))
            plan = []
            for seg in self._segments:
                lo, hi = max(from_offset, seg.first_offset), min(end, seg.end_offset)
                if lo >= hi:
                    continue
                start = seg.positions[lo - seg.first_offset]
                stop = seg.positions[hi - seg.first_offset] if hi < seg.end_offset else seg.size
                plan.append((seg.path, start, stop, lo, hi))
        for path, start, stop, lo, hi in plan:
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    buf = f.read(stop - start)
            except FileNotFoundError as exc:
                raise JournalRangeError(f"읽는 중에 정리됐다: {path.name} (offset {lo}~)") from exc
            pos = 0
            for off in range(lo, hi):
                d = decode_at(buf, pos)
                if d is None or d.entry.journal_offset != off:
                    raise JournalCorruptError(f"기록된 레코드를 다시 읽지 못했다: {path.name} offset {off}")
                yield d.entry
                pos += d.size

    def entry(self, offset: int) -> JournalEntry:
        got = self.read(offset, 1)
        if not got:
            raise JournalRangeError(f"없는 offset: {offset}")
        return got[0]

    # --- 정리 ---------------------------------------------------------------------------

    def compact(self, acked_offset: int | None, last_applied_journal_offset: int | None) -> int:
        """min(acked_offset, last_applied_journal_offset) 이하이고 보관 기간이 지난 세그먼트를 지운다. 지운 수.

        둘 중 하나라도 모르면(None) 지우지 않는다. 활성(마지막) 세그먼트는 지우지 않으므로 다음 offset 은 늘 남는다.
        오래된 것부터 지우므로 도중에 죽어도 남은 세그먼트는 이어진다.
        """
        if acked_offset is None or last_applied_journal_offset is None:
            return 0
        cutoff = min(acked_offset, last_applied_journal_offset)
        with self._write_lock, self._lock:
            self._check_writable()
            horizon = self._clock.now_ms() - self._config.retention_ms
            doomed = 0
            for seg in self._segments[:-1]:
                if seg.end_offset - 1 > cutoff:
                    break
                if seg.last_recorded_at is not None and seg.last_recorded_at > horizon:
                    break
                doomed += 1
            if doomed:
                # 지울 기록에만 있던 활성 이벤트의 근거를 잃지 않도록 먼저 색인을 굳힌다.
                self._save_evidence_index_locked()
            deleted = 0
            while deleted < doomed:
                self._segments[0].path.unlink()
                self._segments.pop(0)
                deleted += 1
            if deleted:
                fsync_dir(self._root)
            return deleted

    # --- 닫기 ---------------------------------------------------------------------------

    def close(self) -> None:
        with self._write_lock, self._lock:
            if self._closed:
                return
            self._closed = True
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if self._writer_lock is not None:
                self._writer_lock.release()
                self._writer_lock = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
