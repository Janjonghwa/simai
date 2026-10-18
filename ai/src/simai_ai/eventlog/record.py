"""저널 레코드 형식 (명세 3.2 저널 계약).

레코드 하나 = 머리 20바이트 + 본문.

    magic    4바이트  b"SJR1"
    length   u32 LE  본문 바이트 수
    offset   u64 LE  journal_offset (전역 단조 증가, event_sequence 와 다름)
    crc32    u32 LE  zlib.crc32(length·offset 8+4바이트 + 본문)
    body     length  UTF-8 JSON {"kind", "msg_id", "recorded_at", "payload", "meta"}

payload 는 기록을 넘긴 쪽(lifecycle)의 것을 그대로 두고, 저널·전송이 쓰는 덧붙임(확인의 ack_id, 근거 world_state
msg_id 등)은 meta 에 따로 둔다. 길이·체크섬으로 깨진 꼬리(기록 중 강제 종료로 일부만 쓰인 마지막 레코드)를 알아본다. 본문 JSON 은 키 정렬·
NaN 금지로 쓴다. 체크섬은 우발적 손상 감지용이고 위변조 방지가 아니다.
"""

import json
import math
import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

MAGIC = b"SJR1"
_HEADER = struct.Struct("<4sIQI")
_CRC_PREFIX = struct.Struct("<IQ")
HEADER_SIZE = _HEADER.size
# 본문 상한. 이보다 큰 길이는 깨진 머리로 본다(쓰레기 길이로 거대한 읽기를 하지 않게).
MAX_BODY_BYTES = 16 * 1024 * 1024


class JournalFormatError(ValueError):
    """레코드 형식 위반(쓰기 전 검사)."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """읽기 전용 payload 를 JSON 으로 보낼 수 있는 dict·list 로 되돌린다(복사본)."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class JournalEntry:
    """저널 기록 하나. payload 는 기록한 그대로(읽기 전용)이며 journal_offset 은 저널이 붙인다.

    msg_id 는 기록할 때 한 번 정해 저널에 남긴다. 재전송해도 같은 msg_id 로 보내 EC2 가 중복을 걸러 낸다.
    meta 는 payload 밖의 덧붙임이다(journal.EventJournal.append_transition 참고).
    """

    journal_offset: int
    kind: str
    msg_id: str
    recorded_at: int
    payload: Mapping[str, Any]
    meta: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def payload_dict(self) -> dict[str, Any]:
        return thaw(self.payload)


def _check_json(value: Any, where: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise JournalFormatError(f"{where}: 유한하지 않은 수는 기록하지 않는다")
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise JournalFormatError(f"{where}: 키는 문자열이어야 한다: {k!r}")
            _check_json(v, f"{where}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _check_json(v, f"{where}[{i}]")
    elif not (value is None or isinstance(value, bool | int | float | str)):
        raise JournalFormatError(f"{where}: JSON 으로 쓸 수 없는 값: {type(value).__name__}")


def encode(
    offset: int,
    kind: str,
    msg_id: str,
    recorded_at: int,
    payload: Mapping[str, Any],
    meta: Mapping[str, Any] | None = None,
) -> bytes:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise JournalFormatError(f"journal_offset 은 0 이상 정수: {offset!r}")
    if not kind:
        raise JournalFormatError("kind 가 비었다")
    if not msg_id:
        raise JournalFormatError("msg_id 가 비었다")
    if not isinstance(payload, Mapping):
        raise JournalFormatError("payload 는 객체여야 한다")
    _check_json(payload, "payload")
    meta = {} if meta is None else meta
    if not isinstance(meta, Mapping):
        raise JournalFormatError("meta 는 객체여야 한다")
    _check_json(meta, "meta")
    body = json.dumps(
        {"kind": kind, "msg_id": msg_id, "recorded_at": recorded_at, "payload": thaw(payload), "meta": thaw(meta)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise JournalFormatError(f"본문이 상한({MAX_BODY_BYTES}B)을 넘는다: {len(body)}B")
    crc = zlib.crc32(_CRC_PREFIX.pack(len(body), offset) + body)
    return _HEADER.pack(MAGIC, len(body), offset, crc) + body


@dataclass(frozen=True)
class Decoded:
    entry: JournalEntry
    size: int  # 머리 + 본문 바이트


def decode_at(buf: bytes | memoryview, pos: int) -> Decoded | None:
    """buf[pos:] 의 레코드 하나를 읽는다. 온전하지 않으면(짧음·magic·길이·체크섬·본문 위반) None."""
    if len(buf) - pos < HEADER_SIZE:
        return None
    magic, length, offset, crc = _HEADER.unpack_from(buf, pos)
    if magic != MAGIC or length > MAX_BODY_BYTES:
        return None
    end = pos + HEADER_SIZE + length
    if end > len(buf):
        return None
    body = bytes(buf[pos + HEADER_SIZE : end])
    if zlib.crc32(_CRC_PREFIX.pack(length, offset) + body) != crc:
        return None
    try:
        doc = json.loads(body.decode("utf-8"))
        entry = JournalEntry(
            journal_offset=offset,
            kind=str(doc["kind"]),
            msg_id=str(doc["msg_id"]),
            recorded_at=int(doc["recorded_at"]),
            payload=_freeze(doc["payload"]),
            meta=_freeze(doc.get("meta", {})),
        )
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(entry.payload, Mapping) or not isinstance(entry.meta, Mapping):
        return None
    return Decoded(entry=entry, size=HEADER_SIZE + length)
