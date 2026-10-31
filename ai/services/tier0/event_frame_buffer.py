"""판정 스레드 소유의 작은 프레임 링. 이미지 복사·인코딩·디코딩은 하지 않는다."""

from collections import deque
from dataclasses import dataclass
from typing import Any

from simai_ai.providers.base import Frame

PRE_MS = 3_000
WINDOW_MS = 5_000
CURRENT_MAX_AGE_MS = 500
SAMPLE_MS = 1_000
MAX_HISTORY = WINDOW_MS // SAMPLE_MS + 1
MAX_CURRENT = 6  # 10 Hz 판정 입력의 0.5 s. 빠른 카메라의 미래 최신 프레임만 남기지 않는다.
MAX_FRAME_BYTES = 10 * 1024 * 1024  # 2304×1296 BGR(8.54 MiB)를 수용한다.


@dataclass(frozen=True)
class EventPicture:
    camera_id: str
    capture_time_ms: int
    timestamp_source: str
    timestamp_uncertainty_ms: float | None
    image: Any


class EventFrameBuffer:
    """카메라별 최대 6개 이력 + 최근 6개. 호출자는 판정 스레드 하나다.

    디코더가 넘긴 BGR은 이후 수정하지 않는 기존 배경/아바타 경로의 소유권 규칙을 따른다.
    snapshot은 최대 두 카메라의 네 이미지 참조만 작업 큐에 고정한다.
    """

    def __init__(self, camera_ids: tuple[str, ...]) -> None:
        self.camera_ids = camera_ids
        self._history: dict[str, deque[EventPicture]] = {c: deque(maxlen=MAX_HISTORY) for c in camera_ids}
        self._current: dict[str, deque[EventPicture]] = {c: deque(maxlen=MAX_CURRENT) for c in camera_ids}
        self._epochs: dict[str, int] = {}

    def offer(self, frame: Frame) -> bool:
        camera = frame.camera_id
        size = int(getattr(frame.image, "nbytes", 0))
        epoch = frame.frame_ref.get("stream_epoch")
        if camera not in self._history or not 0 < size <= MAX_FRAME_BYTES or epoch is None:
            return False
        previous_epoch = self._epochs.get(camera)
        if previous_epoch is not None and epoch < previous_epoch:
            return False
        history = self._history[camera]
        current = self._current[camera]
        if epoch != previous_epoch:
            history.clear()
            current.clear()
            self._epochs[camera] = epoch
        previous = current[-1] if current else None
        at = frame.estimated_capture_time
        if previous is not None and at <= previous.capture_time_ms:
            return False
        picture = EventPicture(camera, at, frame.timestamp_source, frame.timestamp_uncertainty_ms, frame.image)
        while history and history[0].capture_time_ms < at - WINDOW_MS:
            history.popleft()
        if not history or at - history[-1].capture_time_ms >= SAMPLE_MS:
            history.append(picture)
        while current and current[0].capture_time_ms < at - CURRENT_MAX_AGE_MS:
            current.popleft()
        current.append(picture)
        return True

    def snapshot(self, cameras: tuple[str, ...], anchor_ms: int, max_cameras: int) -> tuple[EventPicture, ...]:
        selected = []
        used = 0
        for camera in cameras:
            candidates = [*self._history[camera], *self._current[camera]]
            pictures = []
            for start, end in ((anchor_ms - CURRENT_MAX_AGE_MS, anchor_ms),
                               (anchor_ms - WINDOW_MS, anchor_ms - PRE_MS)):
                eligible = [p for p in candidates if start <= p.capture_time_ms <= end
                            and p.timestamp_source is not None]
                if eligible:
                    pictures.append(max(eligible, key=lambda p: p.capture_time_ms))
            if pictures:
                selected.extend(pictures)
                used += 1
                if used == max_cameras:
                    break
        return tuple(selected)

    def clear(self) -> None:
        for pictures in (*self._history.values(), *self._current.values()):
            pictures.clear()
        self._epochs.clear()
