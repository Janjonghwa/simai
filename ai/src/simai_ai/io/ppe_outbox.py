"""PPE 프로세스 → Tier 0 로컬 IPC: 다 만든 I1 `ppe_state` 봉투를 Tier 0 가 자기 I1 연결로 대신 보낸다 (명세 5.5·12.3, S15P31A101-507).

PPE 는 I1 연결을 따로 열지 않는다. EC2 는 gateway(source_id)마다 열린 연결 하나로 운영자 명령(event_ack·event_close·
event_escalate)을 보내므로, 같은 source_id 의 연결이 둘이면 명령이 PPE 쪽으로 갈 수 있다. 그래서 PPE 는 메시지 한 건을
파일 한 개로 이 폴더에 쓰고(임시 파일 → os.replace), Tier 0 는 `I1Publisher.add_source` 로 꺼내 보낸 뒤 지운다.

- 파일 이름은 `<stream_epoch 13자리>-<state_sequence 12자리>.json` 이라 이름순이 보낸 순서다(새 PPE 프로세스는 epoch 가 더 크다).
- 쓰는 쪽은 대기 파일이 상한(max_pending)을 넘으면 오래된 것부터 지운다. 빈 순번은 계약상 잃은 메시지다(EC2 가 다시 요청하지 않음).
- 읽는 쪽은 msg_type 이 `ppe_state` 인 봉투만 넘기고(source_id 는 자기 연결 값으로 맞춘다), 오래된 파일(max_age_s)·깨진 파일은 세고 버린다.
  계약 전체 검사는 EC2 가 한다. 실패·느린 디스크·PPE 부재는 Tier 0 를 막지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
import re
import time
from pathlib import Path
from typing import Any

DEFAULT_SHM_DIR = Path("/dev/shm")
DEFAULT_SHM_DIRNAME = "simai-ppe-outbox"
DEFAULT_STATE_RELPATH = Path("state/ppe-outbox")
MSG_TYPE = "ppe_state"
DEFAULT_MAX_PENDING = 32
DEFAULT_MAX_BATCH = 8
DEFAULT_MAX_AGE_S = 10.0  # 근거 만료(5.5, 마지막 근거 + 10초)보다 오래 쌓인 상태는 보내지 않는다
_NAME_RE = re.compile(r"^\d{13}-\d{12}\.json$")


def ppe_outbox_path(
    env: Mapping[str, str] | None = None,
    root: Path | str | None = None,
    *,
    shm_dir: Path | None = None,
) -> Path:
    """outbox 폴더: SIMAI_PPE_OUTBOX → /dev/shm(tmpfs) → $SIMAI_ROOT/state/ppe-outbox. PPE·Tier 0 가 같은 규칙으로 찾는다."""
    env_map = os.environ if env is None else env
    explicit = env_map.get("SIMAI_PPE_OUTBOX")
    if explicit:
        return Path(explicit)
    shm = (DEFAULT_SHM_DIR if os.name == "posix" else None) if shm_dir is None else shm_dir
    if shm is not None and shm.is_dir() and os.access(shm, os.W_OK):
        return shm / DEFAULT_SHM_DIRNAME
    base = Path(root) if root is not None else Path(env_map.get("SIMAI_ROOT") or "/workspace/simai")
    return base / DEFAULT_STATE_RELPATH


def _pending(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if _NAME_RE.match(p.name))
    except FileNotFoundError:
        return []


class PpeOutboxWriter:
    """PPE 서비스 쪽. 스레드 하나에서 쓴다."""

    def __init__(self, path: Path, *, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        if max_pending < 1:
            raise ValueError("max_pending 은 1 이상이다")
        self.path = Path(path)
        self.max_pending = max_pending
        self.written = self.dropped = self.errors = 0

    def put(self, message: Mapping[str, Any]) -> bool:
        """봉투 하나를 쓴다. 쓰지 못하면 세고 False(PPE 는 다음 주기에 새 상태를 보낸다)."""
        try:
            payload = message["payload"]
            name = f"{int(payload['stream_epoch']):013d}-{int(payload['state_sequence']):012d}.json"
            data = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self.path.mkdir(parents=True, exist_ok=True)
            temp = self.path / f".{name}.{os.getpid()}.tmp"
            try:
                temp.write_bytes(data)
                os.replace(temp, self.path / name)
            finally:
                temp.unlink(missing_ok=True)
        except (OSError, KeyError, TypeError, ValueError):
            self.errors += 1
            return False
        self.written += 1
        self._prune()
        return True

    def _prune(self) -> None:
        pending = _pending(self.path)
        for old in pending[: max(0, len(pending) - self.max_pending)]:
            try:
                old.unlink()
                self.dropped += 1
            except FileNotFoundError:
                pass  # Tier 0 가 방금 가져갔다
            except OSError:
                self.errors += 1


class PpeOutboxReader:
    """Tier 0 쪽. `take` 를 `I1Publisher.add_source` 에 넘긴다(publisher 스레드 하나가 부른다)."""

    def __init__(
        self,
        path: Path,
        *,
        source_id: str,
        max_batch: int = DEFAULT_MAX_BATCH,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        wall_s: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.source_id = source_id
        self.max_batch = max_batch
        self.max_age_s = max_age_s
        self._wall_s = wall_s
        self.taken = self.stale = self.invalid = self.errors = self.relabeled = 0

    def take(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for file in _pending(self.path):
            if len(out) >= self.max_batch:
                break
            try:
                age = self._wall_s() - file.stat().st_mtime
                raw = file.read_bytes()
                file.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                self.errors += 1
                continue
            if age > self.max_age_s:
                self.stale += 1
                continue
            try:
                doc = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self.invalid += 1
                continue
            if not isinstance(doc, dict) or doc.get("msg_type") != MSG_TYPE or not isinstance(doc.get("payload"), dict):
                self.invalid += 1
                continue
            if doc.get("source_id") != self.source_id:
                # PPE 는 SIMAI_TIER0_ID(없으면 tier0-01)로 쓴다. Tier 0 설정 파일의 gateway_id 가 다르면 이 연결의 값으로 맞춘다
                doc["source_id"] = self.source_id
                self.relabeled += 1
            out.append(doc)
        self.taken += len(out)
        return out

    def stats(self) -> dict[str, int]:
        return {"taken": self.taken, "stale": self.stale, "invalid": self.invalid, "errors": self.errors,
                "relabeled": self.relabeled}
