"""-347: perception 관절 버퍼와 후보 실행기의 수명 주기·콜백 조립."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from services.tier0.settings import LiveCalibrationSettings
from simai_ai.calibration.live_candidate import LiveBufferConfig, LiveCandidateCalibrationRunner, LiveCandidateConfig, LiveJointBuffer

log = logging.getLogger("services.tier0.live_calibration")


class BackgroundLiveCalibration:
    """run은 보조 스레드 하나, offer는 판정 스레드에서 실행한다.

    activate/reset_camera와 후보 수락은 서비스의 판정 lock으로 직렬화한다.
    문서·출처는 불변 참조로 교체해 tick 안에서 복사/검증을 하지 않는다.
    """

    def __init__(self, document: Mapping[str, Any], provenance: Mapping[str, Any], *,
                 settings: LiveCalibrationSettings, now_ms: Callable[[], int], stop: threading.Event,
                 propose: Callable[[Mapping[str, Any], Mapping[str, Any], int], bool],
                 can_run: Callable[[], bool]) -> None:
        self.buffer = LiveJointBuffer(LiveBufferConfig(max_duration_ms=settings.buffer_duration_ms))
        self.document = document
        self.provenance = provenance
        self.generation = 0
        self.blocked_reason: str | None = None
        self._cycle_generation = 0
        self._propose = propose
        self._now_ms = now_ms
        self._stop = stop
        self._next_run_ms = now_ms() + settings.interval_ms
        self.runner = LiveCandidateCalibrationRunner(
            self.buffer, lambda: self.document, measured_camera_heights=settings.measured_camera_heights,
            input_provenance=provenance, propose_calibration=self._submit,
            config=LiveCandidateConfig(interval_ms=settings.interval_ms, timeout_s=settings.timeout_s),
            can_run=lambda: not stop.is_set() and self.blocked_reason is None and can_run(),
            clock_ms=now_ms, adopt_proposed_as_active=False, stop_event=stop,
        )
        self.last_status: str | None = None
        self.last_error: str | None = None
        self.last_comparison: dict[str, Any] | None = None
        self.running = False
        self.cycles = 0

    def _submit(self, document, report) -> bool:
        return self._propose(document, report, self._cycle_generation)

    def activate(self, document, provenance) -> None:
        self.document, self.provenance = document, provenance
        self.generation += 1
        self.blocked_reason = None

    def reset_camera(self, camera_id: str) -> int:
        self.generation += 1
        removed = self.buffer.reset_camera(camera_id)
        log.info("후보 보정 재수집: %s, 제거 프레임=%d", camera_id, removed)
        return removed

    def block(self, reason: str) -> None:
        self.generation += 1
        self.blocked_reason = reason
        log.warning("후보 보정 보류: %s", reason)

    def run_once(self):
        """서비스 스레드와 시험/재생 도구가 같은 실행·오류 경로를 쓴다."""
        self._cycle_generation = self.generation
        self.running = True
        try:
            self.runner.set_input_provenance(self.provenance)
            result = self.runner.maybe_run(now_ms=self._now_ms())
            if result is not None:
                self.cycles += 1
                self.last_status = result.status
                self.last_error = result.report.get("failure_reason")
                self.last_comparison = {"holdout": result.report.get("holdout"),
                                        "decision": result.report.get("improvement_decision")}
                log.info("후보 보정 결과: %s, 사유=%s, 비교=%s", result.status, self.last_error, self.last_comparison)
            return result
        except Exception as exc:
            self.cycles += 1
            self.last_status, self.last_error = "FAILED", str(exc)
            log.exception("후보 보정 실행 실패(판정은 계속한다)")
            return None
        finally:
            self.running = False

    def run(self) -> None:
        # 기동 직후 직전 보정으로 판정을 시작한다. 첫 주기까지 수집하고 겹쳐 실행하지 않는다.
        while not self._stop.wait(0.1):
            if self._now_ms() >= self._next_run_ms:
                self._next_run_ms = self._now_ms() + self.runner.config.interval_ms
                self.run_once()

    def status(self) -> dict[str, Any]:
        return {"active_version": self.document["calibration_version"], "running": self.running,
                "cycles": self.cycles, "last_status": self.last_status, "last_error": self.last_error,
                "last_comparison": self.last_comparison,
                "blocked_reason": self.blocked_reason, "buffer": self.buffer.summary()}
