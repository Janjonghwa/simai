"""acked_offset 영속 저장 (명세 3.2 저널 계약: EC2 ACK(journal_ack)마다 acked_offset 을 영속 저장).

파일 하나(JSON)를 원자적으로 바꾼다: 임시 파일에 쓰고 fsync → os.replace → 디렉터리 fsync(POSIX). 도중에 죽으면
이전 값이나 새 값 둘 중 하나만 남는다. 이전 값이 남으면 그 뒤 구간을 다시 보내게 되고, EC2 는 msg_id·
(event_id, event_sequence)로 중복을 걸러 낸다(유실 대신 중복 쪽으로 실패한다).

파일이 없으면 아직 아무것도 확인받지 않은 것(None)이다. 파일이 깨졌거나 다른 Tier 0 의 것이면 지어내지 않고 오류다.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path

from simai_ai.eventlog._fs import atomic_write

ACKED_NAME = "acked_offset.json"


class AckedOffsetError(RuntimeError):
    """acked_offset 파일을 믿을 수 없다(깨짐, 다른 gateway_id, 형식 위반)."""


class AckedOffsetStore:
    def __init__(self, path: str | os.PathLike[str], *, gateway_id: str, fsync: Callable[[int], None] = os.fsync):
        if not gateway_id:
            raise ValueError("gateway_id 가 비었다")
        self._path = Path(path)
        self._tmp = self._path.with_name(self._path.name + ".tmp")
        self._gateway_id = gateway_id
        self._fsync = fsync

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> int | None:
        """저장된 acked_offset. 없으면 None. 남은 임시 파일(바꾸기 전에 죽은 흔적)은 버린다."""
        if self._tmp.exists():
            self._tmp.unlink()
        if not self._path.exists():
            return None
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AckedOffsetError(f"acked_offset 파일이 깨졌다: {self._path}") from exc
        if not isinstance(doc, dict) or doc.get("gateway_id") != self._gateway_id:
            raise AckedOffsetError(f"다른 Tier 0 의 acked_offset 이다: {doc!r} (기대 {self._gateway_id})")
        value = doc.get("acked_offset")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AckedOffsetError(f"acked_offset 형식 위반: {value!r}")
        return value

    def save(self, acked_offset: int) -> None:
        if not isinstance(acked_offset, int) or isinstance(acked_offset, bool) or acked_offset < 0:
            raise ValueError(f"acked_offset 은 0 이상 정수: {acked_offset!r}")
        data = json.dumps({"gateway_id": self._gateway_id, "acked_offset": acked_offset}).encode("utf-8")
        atomic_write(self._path, data, self._fsync)
