"""카메라별 stream_epoch 예약 저장소 (명세 12.7 "(camera_id, stream_epoch, frame_sequence) 가 프레임의 유일 식별자", S15P31A101-83).

중계 프로세스를 다시 띄우거나 노트북 시계가 뒤로 가도 같은 카메라의 epoch 가 다시 쓰이지 않게, 카메라마다 지금까지 쓴 가장 큰
epoch 를 노트북 로컬 파일에 남긴다. Tier 0 는 줄어든 epoch 를 받지 않으므로(같은 식별자에 다른 프레임·시각이 붙지 않게)
epoch 가 다시 쓰이면 그 카메라가 Tier 0 에서 사라진다.

- reserve(camera_id, floor): max(저장값 + 1, floor) 를 정하고, 파일에 먼저 쓴 뒤 돌려준다(쓰지 못하면 OSError, 그 epoch 는
  쓰지 않는다). floor 는 보통 세션 시작 UTC ms 다(파일이 없어진 경우에도 이전 실행보다 클 가능성을 높인다).
- 쓰기는 원자적이다: 같은 폴더 임시 파일에 쓰고 flush·fsync 한 뒤 os.replace.
- 파일이 깨져 있으면 ValueError 로 시작을 멈춘다. 지워진 것처럼 0 부터 다시 세면 식별자가 다시 쓰일 수 있다.
- 한 파일을 중계 프로세스 둘이 같이 쓰는 것은 지원하지 않는다.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

DEFAULT_EPOCH_STATE = Path.home() / ".simai" / "relay-epochs.json"


class EpochStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()  # 재접속 때만 잡는다(프레임 경로에서는 쓰지 않는다)
        self._max: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(f"epoch 저장 파일이 깨졌다: {self.path}. 고치거나, 지울 때는 Tier 0 를 다시 띄운다") from None
        if not isinstance(doc, dict) or not all(
                isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0 for k, v in doc.items()):
            raise ValueError(f"epoch 저장 파일 형식이 틀렸다: {self.path}")
        return dict(doc)

    def last(self, camera_id: str) -> int | None:
        with self._lock:
            return self._max.get(camera_id)

    def reserve(self, camera_id: str, floor: int = 0) -> int:
        with self._lock:
            epoch = max(self._max.get(camera_id, -1) + 1, int(floor), 0)
            self._write({**self._max, camera_id: epoch})
            self._max[camera_id] = epoch
            return epoch

    def _write(self, doc: dict[str, int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
