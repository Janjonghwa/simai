"""Tier 0 체크포인트 파일 (명세 3.2 재시작 복구·저널 계약).

체크포인트는 그 시점까지 저널을 적용한 요약일 뿐이고 정본은 이벤트 저널이다. 담는 것:
활성 RiskEvent 상태(EventState 전체), 해소 기록(tombstone: 해소된 이벤트의 마지막 sequence, 리뷰 M1),
last_applied_journal_offset, config_version, 마지막으로 처리한 촬영 시각,
World Track 상태(TrackCheckpoint, 복원 매칭은 S15P31A101-54).

**형식(내부, 계약 아님).** 파일 하나 = JSON {"format", "format_version", "sha256", "body"}. sha256 은 body 의 정규 JSON
(키 정렬, 공백 없음, NaN 금지, UTF-8)의 해시다. 형식 버전이 다르면(앞으로 바뀐 형식 포함) 읽지 않는다.

**세대.** `checkpoint-{세대 20자리}.json` 으로 매번 새 파일을 원자적으로 쓰고(_atomic.durable_write), 새 파일이 디스크에
남은 뒤에 오래된 세대와 깨진 파일을 지운다(온전한 최근 keep 개, 기본 2). 읽을 때는 최신 세대부터 검사해 처음으로 온전한 것을 쓴다.
깨짐·부분 쓰기(임시 파일만 남음)·해시 불일치·형식 버전 불일치·다른 Tier 0 의 것·내용 형식 위반은 이유와 함께 버리고
직전 세대로 물러선다(지어내지 않는다). 저널과 맞는지(저널보다 앞섬, 정리로 빈 구간)는 restore 가 저널을 보고 판단한다.

**정리 하한.** 직전 세대로 물러설 수 있어야 하므로 저널 정리에 넘기는 last_applied_journal_offset 은 남아 있는 세대의
가장 작은 값이다(compaction_floor). 명세 경계 min(acked_offset, last_applied_journal_offset) 보다 늘 보수적이다.
"""

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simai_ai.recovery._atomic import TMP_SUFFIX, Fsync, durable_write, fsync_dir
from simai_ai.recovery.projection import Tombstone
from simai_ai.risk.common import Origin
from simai_ai.risk.lifecycle import EventState, Lifecycle
from simai_ai.tracking.world_track import TrackCheckpoint

FORMAT = "simai-tier0-checkpoint"
# 1: 첫 형식. 2: 해소 기록(resolved, resolved_watermark) 추가(리뷰 M1). 1 은 해소 기록이 없어 읽지 않는다.
FORMAT_VERSION = 2
_NAME_RE = re.compile(r"^checkpoint-(\d{20})\.json$")
_TOP_KEYS = {"format", "format_version", "sha256", "body"}
_BODY_KEYS = {
    "gateway_id",
    "checkpoint_seq",
    "written_at",
    "last_applied_journal_offset",
    "last_applied_msg_id",
    "config_version",
    "last_capture_time",
    "events",
    "tracks_t",
    "tracks",
    "resolved",
    "resolved_watermark",
}


class CheckpointError(RuntimeError):
    """체크포인트 저장소 사용 오류."""


class CheckpointInvalid(ValueError):
    """체크포인트 파일을 믿을 수 없다(이유는 reason)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def checkpoint_name(seq: int) -> str:
    return f"checkpoint-{seq:020d}.json"


def _canonical(doc: Any) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _opt_int(value: Any, name: str, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CheckpointInvalid(f"{name} 형식 위반: {value!r}")
    return value


@dataclass(frozen=True)
class Checkpoint:
    """체크포인트 한 세대.

    last_applied_journal_offset: 이 체크포인트의 events 가 반영한 마지막 저널 offset(포함). 저널이 비어 있었으면 None.
    last_applied_msg_id: 그 기록의 msg_id. 복구 때 같은 저널인지(다른 저널로 바뀌어 offset 만 맞는 경우) 확인한다.
    events: 그 offset 까지 저널을 적용했을 때 활성인(RESOLVED 가 아닌) 이벤트. 키 순서.
    config_version: 쓸 때 활성이던 구성 묶음 버전(모르면 None). last_capture_time: 마지막으로 처리한 촬영 시각(UTC ms).
    tracks_t: tracks 를 뽑은 공통 시각. tracks 가 없으면 None 일 수 있다.
    tombstones: 그 offset 까지 해소된 이벤트의 마지막 sequence·판정 시각(오래된 것부터, 최근 일부).
    resolved_watermark: tombstones 에서 밀려난 해소 기록들의 마지막 판정 시각 최댓값(projection 의 보장 경계).
    """

    gateway_id: str
    checkpoint_seq: int
    written_at: int
    last_applied_journal_offset: int | None
    last_applied_msg_id: str | None
    config_version: str | None
    last_capture_time: int | None
    events: tuple[EventState, ...]
    tracks_t: int | None
    tracks: tuple[TrackCheckpoint, ...]
    tombstones: tuple[Tombstone, ...] = ()
    resolved_watermark: int | None = None

    def body(self) -> dict[str, Any]:
        return {
            "gateway_id": self.gateway_id,
            "checkpoint_seq": self.checkpoint_seq,
            "written_at": self.written_at,
            "last_applied_journal_offset": self.last_applied_journal_offset,
            "last_applied_msg_id": self.last_applied_msg_id,
            "config_version": self.config_version,
            "last_capture_time": self.last_capture_time,
            "events": [e.to_record() for e in self.events],
            "tracks_t": self.tracks_t,
            "tracks": [t.to_record() for t in self.tracks],
            "resolved": [[t.event_id, t.event_sequence, t.evaluated_at] for t in self.tombstones],
            "resolved_watermark": self.resolved_watermark,
        }

    def encode(self) -> bytes:
        body = self.body()
        doc = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "sha256": hashlib.sha256(_canonical(body)).hexdigest(),
            "body": body,
        }
        return _canonical(doc)

    @classmethod
    def decode(cls, data: bytes, *, gateway_id: str) -> "Checkpoint":
        """파일 내용 → Checkpoint. 믿을 수 없으면 CheckpointInvalid."""
        try:
            doc = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CheckpointInvalid(f"JSON 이 아니다(깨짐·부분 쓰기): {exc}") from None
        if not isinstance(doc, dict) or set(doc) != _TOP_KEYS:
            raise CheckpointInvalid("최상위 필드가 형식과 다르다")
        if doc["format"] != FORMAT:
            raise CheckpointInvalid(f"형식 이름이 다르다: {doc['format']!r}")
        if doc["format_version"] != FORMAT_VERSION or isinstance(doc["format_version"], bool):
            raise CheckpointInvalid(
                f"형식 버전 불일치: {doc['format_version']!r} (읽을 수 있는 버전 {FORMAT_VERSION}. 1 은 해소 기록이 없는 이전 형식)"
            )
        body = doc["body"]
        if not isinstance(body, dict):
            raise CheckpointInvalid("body 가 객체가 아니다")
        if hashlib.sha256(_canonical(body)).hexdigest() != doc["sha256"]:
            raise CheckpointInvalid("sha256 불일치(손상)")
        return cls.from_body(body, gateway_id=gateway_id)

    @classmethod
    def from_body(cls, body: Mapping[str, Any], *, gateway_id: str) -> "Checkpoint":
        if set(body) != _BODY_KEYS:
            raise CheckpointInvalid(f"body 필드가 형식과 다르다: {sorted(set(body) ^ _BODY_KEYS)}")
        if body["gateway_id"] != gateway_id:
            raise CheckpointInvalid(f"다른 Tier 0 의 체크포인트다: {body['gateway_id']!r} (기대 {gateway_id})")
        seq = _opt_int(body["checkpoint_seq"], "checkpoint_seq", minimum=1)
        written_at = _opt_int(body["written_at"], "written_at")
        if seq is None or written_at is None:
            raise CheckpointInvalid("checkpoint_seq·written_at 은 비울 수 없다")
        last_offset = _opt_int(body["last_applied_journal_offset"], "last_applied_journal_offset")
        last_msg = body["last_applied_msg_id"]
        if (last_offset is None) != (last_msg is None) or (
            last_msg is not None and (not isinstance(last_msg, str) or not last_msg)
        ):
            raise CheckpointInvalid(f"last_applied_msg_id 형식 위반: {last_msg!r}")
        config_version = body["config_version"]
        if config_version is not None and (not isinstance(config_version, str) or not config_version):
            raise CheckpointInvalid(f"config_version 형식 위반: {config_version!r}")
        if not isinstance(body["events"], list) or not isinstance(body["tracks"], list):
            raise CheckpointInvalid("events·tracks 는 배열이어야 한다")
        try:
            events = tuple(EventState.from_record(e, f"events[{i}]") for i, e in enumerate(body["events"]))
            tracks = tuple(TrackCheckpoint.from_record(t) for t in body["tracks"])
        except (ValueError, TypeError) as exc:
            raise CheckpointInvalid(f"내용 형식 위반: {exc}") from None
        _check_events(events, gateway_id)
        tombstones = _tombstones(body["resolved"], {e.event_id for e in events})
        if len({t.world_id for t in tracks}) != len(tracks):
            raise CheckpointInvalid("같은 World ID 의 Track 이 둘 이상이다")
        return cls(
            gateway_id=gateway_id,
            checkpoint_seq=seq,
            written_at=written_at,
            last_applied_journal_offset=last_offset,
            last_applied_msg_id=last_msg,
            config_version=config_version,
            last_capture_time=_opt_int(body["last_capture_time"], "last_capture_time"),
            events=events,
            tracks_t=_opt_int(body["tracks_t"], "tracks_t"),
            tracks=tracks,
            tombstones=tombstones,
            resolved_watermark=_opt_int(body["resolved_watermark"], "resolved_watermark"),
        )


def _reject_constant(name: str) -> Any:
    raise ValueError(f"표준 JSON 이 아닌 수: {name}")


def _tombstones(value: Any, active_ids: set[str]) -> tuple[Tombstone, ...]:
    if not isinstance(value, list):
        raise CheckpointInvalid("resolved 는 배열이어야 한다")
    out: list[Tombstone] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        if not isinstance(item, list) or len(item) != 3 or not isinstance(item[0], str) or not item[0]:
            raise CheckpointInvalid(f"resolved[{i}] 형식 위반: {item!r}")
        seq = _opt_int(item[1], f"resolved[{i}].event_sequence", minimum=1)
        evaluated_at = _opt_int(item[2], f"resolved[{i}].evaluated_at")
        if seq is None or evaluated_at is None:
            raise CheckpointInvalid(f"resolved[{i}] 를 비울 수 없다")
        if item[0] in seen or item[0] in active_ids:
            raise CheckpointInvalid(f"resolved[{i}] 가 다른 해소 기록·활성 이벤트와 겹친다: {item[0]}")
        seen.add(item[0])
        out.append(Tombstone(item[0], seq, evaluated_at))
    return tuple(out)


def _check_events(events: Iterable[EventState], gateway_id: str) -> None:
    ids: set[str] = set()
    keys = set()
    for e in events:
        if e.origin is not Origin.LIVE:
            raise CheckpointInvalid(f"LIVE 가 아닌 이벤트: {e.event_id} ({e.origin})")
        if e.gateway_id != gateway_id:
            raise CheckpointInvalid(f"다른 Tier 0 의 이벤트: {e.event_id} ({e.gateway_id})")
        if e.lifecycle is Lifecycle.RESOLVED:
            raise CheckpointInvalid(f"해소된 이벤트가 활성 목록에 있다: {e.event_id}")
        if e.event_id in ids or e.key in keys:
            raise CheckpointInvalid(f"이벤트가 겹친다: {e.event_id}")
        ids.add(e.event_id)
        keys.add(e.key)


@dataclass(frozen=True)
class Rejected:
    """읽지 않고 버린 체크포인트와 그 이유."""

    name: str
    checkpoint_seq: int | None
    reason: str


@dataclass(frozen=True)
class LoadReport:
    """load 결과. checkpoints 는 온전한 세대(최신 먼저), rejected 는 버린 파일."""

    checkpoints: tuple[Checkpoint, ...]
    rejected: tuple[Rejected, ...]

    @property
    def latest(self) -> Checkpoint | None:
        return self.checkpoints[0] if self.checkpoints else None


class CheckpointStore:
    """체크포인트 디렉터리 하나. 쓰기는 한 스레드에서 한다(Checkpointer)."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        gateway_id: str,
        keep: int = 2,
        fsync: Fsync = os.fsync,
    ) -> None:
        if not gateway_id:
            raise ValueError("gateway_id 가 비었다")
        if keep < 2:
            raise ValueError("keep 은 2 이상이어야 한다(직전 세대로 물러설 수 있게)")
        self._root = Path(root)
        self._gateway_id = gateway_id
        self._keep = keep
        self._fsync = fsync
        self._next_seq: int | None = None
        self._retained: dict[int, int | None] = {}  # 세대 → last_applied_journal_offset (온전한 세대만)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def gateway_id(self) -> str:
        return self._gateway_id

    def _names(self) -> list[tuple[int, Path]]:
        if not self._root.is_dir():
            return []
        out = []
        for p in self._root.iterdir():
            m = _NAME_RE.match(p.name)
            if m is not None:
                out.append((int(m.group(1)), p))
        return sorted(out)

    def load(self) -> LoadReport:
        """모든 세대를 검사한다. 남은 임시 파일(쓰다 죽은 흔적)은 지운다. save 전에 한 번 불러야 한다."""
        self._root.mkdir(parents=True, exist_ok=True)
        for p in self._root.iterdir():
            if p.name.startswith("checkpoint-") and p.name.endswith(TMP_SUFFIX):
                p.unlink()
        good: list[Checkpoint] = []
        rejected: list[Rejected] = []
        names = self._names()
        for seq, path in reversed(names):
            try:
                cp = Checkpoint.decode(path.read_bytes(), gateway_id=self._gateway_id)
                if cp.checkpoint_seq != seq:
                    raise CheckpointInvalid(f"파일 이름의 세대 {seq} 와 내용의 세대 {cp.checkpoint_seq} 가 다르다")
            except CheckpointInvalid as exc:
                rejected.append(Rejected(path.name, seq, exc.reason))
                continue
            except OSError as exc:
                rejected.append(Rejected(path.name, seq, f"읽기 실패: {exc}"))
                continue
            good.append(cp)
        self._next_seq = (names[-1][0] + 1) if names else 1
        self._retained = {cp.checkpoint_seq: cp.last_applied_journal_offset for cp in good}
        return LoadReport(tuple(good), tuple(rejected))

    def save(
        self,
        *,
        written_at: int,
        last_applied_journal_offset: int | None,
        last_applied_msg_id: str | None,
        config_version: str | None,
        last_capture_time: int | None,
        events: Iterable[EventState],
        tracks_t: int | None = None,
        tracks: Iterable[TrackCheckpoint] = (),
        tombstones: Iterable[Tombstone] = (),
        resolved_watermark: int | None = None,
    ) -> Checkpoint:
        """새 세대를 원자적으로 쓰고 오래된 세대를 지운다. 쓰기가 실패하면 예외(이전 세대는 그대로)."""
        if self._next_seq is None:
            raise CheckpointError("load 를 먼저 불러야 한다(세대 번호를 이어 가기 위해)")
        events = tuple(sorted(events, key=lambda e: e.key.sort_key))
        _check_events(events, self._gateway_id)
        cp = Checkpoint(
            gateway_id=self._gateway_id,
            checkpoint_seq=self._next_seq,
            written_at=written_at,
            last_applied_journal_offset=last_applied_journal_offset,
            last_applied_msg_id=last_applied_msg_id,
            config_version=config_version,
            last_capture_time=last_capture_time,
            events=events,
            tracks_t=tracks_t,
            tracks=tuple(tracks),
            tombstones=tuple(tombstones),
            resolved_watermark=resolved_watermark,
        )
        data = cp.encode()
        # 쓰기 전에 되읽기 검사: 읽을 수 없는 체크포인트는 남기지 않는다.
        Checkpoint.decode(data, gateway_id=self._gateway_id)
        self._next_seq += 1
        durable_write(self._root / checkpoint_name(cp.checkpoint_seq), data, self._fsync)
        self._retained[cp.checkpoint_seq] = cp.last_applied_journal_offset
        self._prune()
        return cp

    def _prune(self) -> None:
        """온전한 최신 keep 세대만 남긴다. 그보다 오래된 세대와 load 가 버린(깨진) 파일은 지운다.

        깨진 파일을 남겨 온전한 직전 세대를 대신 지우면, 새 세대가 나중에 깨졌을 때 물러설 곳이 없다.
        """
        keep = set(sorted(self._retained)[-self._keep :])
        doomed = [(seq, path) for seq, path in self._names() if seq not in keep]
        for seq, path in doomed:
            path.unlink()
            self._retained.pop(seq, None)
        if doomed:
            fsync_dir(self._root)

    def compaction_floor(self) -> int | None:
        """저널 정리에 넘길 last_applied_journal_offset: 남아 있는 온전한 세대 중 가장 작은 값.

        세대가 없거나 어느 세대든 None(저널이 비었을 때 쓴 것)이면 None(정리하지 않음).
        """
        if not self._retained:
            return None
        values = list(self._retained.values())
        if any(v is None for v in values):
            return None
        return min(v for v in values if v is not None)
