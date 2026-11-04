"""Round diagnostics independent of the synchronous matcher; no image or response logging."""

from __future__ import annotations

import logging
import threading
import time

from .judge import CAMERAS

LOG = logging.getLogger("simai_camera_helper.service")
DEFAULT_STALL_SECONDS = 30


def bounded_setting(env: dict[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    """Reject invalid configuration without logging its potentially secret raw value."""
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = minimum - 1
    if not minimum <= value <= maximum:
        LOG.warning("invalid %s; using default=%s range=%s..%s", name, default, minimum, maximum)
        return default
    return value


class Progress:
    """Small locked state shared with a watchdog; all elapsed time uses an injected clock."""

    def __init__(self, stall_seconds: int = DEFAULT_STALL_SECONDS, *, clock=time.monotonic):
        self._clock = clock
        self._interval = stall_seconds
        self._lock = threading.Lock()
        self._active_since: float | None = None
        self._last_sent: float | None = None
        self._last_warning: float | None = None
        self._phase = "waiting"
        self._phase_since = clock()

    def phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase
            self._phase_since = self._clock()

    def finished(self, outcome: str) -> None:
        with self._lock:
            self._phase = "waiting" if outcome == "ok" else "send_failed"
            self._phase_since = self._clock()
            if outcome == "ok":
                self._last_sent = self._phase_since

    def check(self, active: bool, snapshot: dict) -> None:
        with self._lock:
            now = self._clock()
            if not active:
                self._active_since = None
                self._last_warning = None
                return
            if self._active_since is None:
                self._active_since = now
            baseline = max(self._active_since, self._last_sent or self._active_since)
            if now - baseline < self._interval:
                return
            if self._last_warning is not None and now - self._last_warning < self._interval:
                return
            self._last_warning = now
            elapsed_ms = int((now - baseline) * 1000)
            phase_ms = int((now - self._phase_since) * 1000)
            missing = [cid for cid in CAMERAS if cid not in snapshot]
            if self._phase in ("matching", "loading", "posting"):
                detail = f"reason={self._phase} elapsed_ms={phase_ms}"
            elif missing:
                detail = "reason=missing_frames missing=" + ",".join(missing)
            elif self._phase == "send_failed":
                detail = "reason=send_failed"
            else:
                detail = "reason=unchanged_timestamps"
        LOG.warning("guide stalled no_result_ms=%s %s", elapsed_ms, detail)


def watchdog_loop(progress: Progress, frames, active: threading.Event, stop: threading.Event) -> None:
    while not stop.is_set():
        progress.check(active.is_set(), frames.snapshot())
        stop.wait(0.5)


def log_round(round_ts_ms: int, match_ms: int, timestamps: dict[str, int], grades: list[str],
              outcome: str) -> None:
    ages = " ".join(f"{cid}:{round_ts_ms - timestamps[cid]}" for cid in CAMERAS)
    LOG.info("guide round round_ts_ms=%s match_ms=%s frame_age_ms=[%s] grades=[%s] send=%s",
             round_ts_ms, match_ms, ages, " ".join(g[0] for g in grades), outcome)
