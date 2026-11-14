"""세팅 「카메라 확인」 입력 내보내기(S15P31A101-646): 관제 경로의 공통 시각 view 를 같은 Pod 의 분석 워커에 파일로 넘긴다.

세팅 워커(`services.worker.camera_check`)가 새 보정 후보로 카메라 확인을 시작하면 `<state>/camera-check/request.json` 을 쓴다.
요청이 있는 동안만 판정 스레드가 이미 만든 view(`Perception` 의 `views_at` 결과, ROI 거르기 전)를 최대 `max_rate_hz` 로
`<state>/camera-check/sessions/<setup_session_id>/views.jsonl` 에 덧붙인다. 계산(새 보정으로 연결·재투영)은 워커가 한다:
Tier 0 판정 스레드의 지연(13·14차 혼잡 때 GIL 붕괴·지연 폭증)을 늘리지 않으려고 여기서는 복사·직렬화만 하고, 파일 쓰기는
보조 스레드가 한다(대기열이 차면 버린다). 요청이 없거나 만료되면 아무것도 하지 않는다. 판정·위치에는 관여하지 않는다.

시도(S15P31A101-687): 요청에 `attempt_id` 가 있으면 `sessions/<setup_session_id>/<attempt_id>/views.jsonl` 에 적는다. 「확인만 다시」로
시도가 바뀌면 대기열에 남은 이전 시도 줄은 이전 시도 폴더로 가서 새 시도에 섞이지 않는다. 줄 수 상한도 시도마다 센다.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from simai_ai.calibration.camera_check import views_to_json
from simai_ai.tracking.cross_view import LocalView

log = logging.getLogger(__name__)

REQUEST_FILE = "request.json"
_SESSION_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_ATTEMPT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def session_views_path(root: Path, setup_session_id: str, attempt_id: str | None = None) -> Path:
    base = Path(root) / "sessions" / setup_session_id
    return (base / attempt_id if attempt_id else base) / "views.jsonl"


class CameraCheckTap:
    """offer 는 판정 스레드, 쓰기는 보조 스레드 하나. 요청 파일은 check_interval_ms 마다 한 번만 본다."""

    def __init__(self, root: Path, now_ms: Callable[[], int], *, max_rate_hz: float = 5.0,
                 check_interval_ms: int = 1_000, max_lines: int = 6_000, queue_size: int = 64) -> None:
        if max_rate_hz <= 0 or check_interval_ms <= 0 or max_lines <= 0:
            raise ValueError("max_rate_hz·check_interval_ms·max_lines 는 양수다")
        self.root = Path(root)
        self._now_ms = now_ms
        self._period_ms = int(1000 / max_rate_hz)
        self._check_ms = check_interval_ms
        self._max_lines = max_lines
        self._queue: queue.Queue[tuple[tuple[str, str | None], dict[str, Any]] | None] = queue.Queue(maxsize=queue_size)
        self._session: str | None = None
        self._attempt: str | None = None
        self._expires_at: int | None = None
        self._next_check = 0
        self._last_t: int | None = None
        self._lines: dict[tuple[str, str | None], int] = {}
        self.dropped = 0
        self.written = 0
        self.errors = 0
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 판정 스레드

    def _refresh(self, now: int) -> None:
        if now < self._next_check:
            return
        self._next_check = now + self._check_ms
        try:
            doc = json.loads((self.root / REQUEST_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._session = None
            return
        session = doc.get("setup_session_id") if isinstance(doc, dict) else None
        expires = doc.get("expires_at_ms") if isinstance(doc, dict) else None
        attempt = doc.get("attempt_id") if isinstance(doc, dict) else None
        if not isinstance(session, str) or not _SESSION_RE.match(session):
            self._session = None
            return
        if attempt is not None and (not isinstance(attempt, str) or not _ATTEMPT_RE.match(attempt)):
            self._session = None  # 형식이 틀린 시도 ID 는 경로에 쓰지 않는다
            return
        if session != self._session or attempt != self._attempt:
            log.info("카메라 확인 입력 내보내기 시작: 세팅 세션 %s, 시도 %s", session, attempt or "-")
            self._last_t = None
        self._session = session
        self._attempt = attempt
        self._expires_at = expires if isinstance(expires, int) and not isinstance(expires, bool) else None

    def offer(self, t: int, views: Sequence[LocalView]) -> None:
        """공통 시각 t 의 view. 요청이 있을 때만, 직전 줄과 period 이상 떨어진 시각만 넘긴다."""
        now = self._now_ms()
        self._refresh(now)
        session = self._session
        if session is None or (self._expires_at is not None and now > self._expires_at):
            return
        if self._last_t is not None and t - self._last_t < self._period_ms:
            return
        key = (session, self._attempt)
        if self._lines.get(key, 0) >= self._max_lines:
            return
        self._last_t = t
        line = views_to_json(t, views)
        try:
            self._queue.put_nowait((key, line))
            self._lines[key] = self._lines.get(key, 0) + 1
        except queue.Full:
            self.dropped += 1

    # ------------------------------------------------------------------ 보조 스레드

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            (session, attempt), line = item
            try:
                path = session_views_path(self.root, session, attempt)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
                self.written += 1
            except OSError:
                self.errors += 1
                if self.errors in (1, 10, 100):
                    log.warning("카메라 확인 입력을 쓰지 못했다(판정은 계속한다, %d회)", self.errors)

    def status(self) -> dict[str, Any]:
        return {"session": self._session, "attempt": self._attempt, "written": self.written, "dropped": self.dropped,
                "errors": self.errors}
