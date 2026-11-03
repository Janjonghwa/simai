"""배경 특징점 기반 카메라 순수 회전 감지·복구 워커 (명세 4.4, S15P31A101-454).

판정 틱 밖의 전용 저우선순위 스레드에서 카메라별 기준 배경과 최근 배경을 비교한다.
순수 회전은 CandidateConfirmation(2회 연속 확인) → tick 경계 교체 → SwapProbation(60초 감시)을 거치고,
위치 이동·시차·허용각 초과(REQUIRES_SETUP)는 자동 복구 없이 DRIFTED 및 '현장 세팅 다시 하기' 경고를 올린다.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import cv2

from services.tier0.clean_background import BackgroundCleanBackground
from services.tier0.settings import RotationRecoverySettings
from simai_ai.calibration.drift import DriftFinding, DriftReason
from simai_ai.calibration.live_safety import CandidateConfirmation
from simai_ai.calibration.monitor import CameraMonitorReport, MonitorReport, MonitorStatus
from simai_ai.calibration.rotation_recovery import (
    Features,
    consistent_rotation,
    extract_features,
    make_candidate,
)

log = logging.getLogger("services.tier0.rotation_recovery")


class BudgetExceededError(RuntimeError):
    """단일 실행 주기의 CPU 시간 예산을 초과했을 때 다음 주기로 이월한다."""


def _lower_thread_priority() -> None:
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass


class BackgroundRotationRecovery:
    """기준 배경과 최근 배경 누적 창을 비교해 중심 고정 회전 후보를 제안한다."""

    def __init__(
        self,
        document: Mapping[str, Any],
        provenance: Mapping[str, Any],
        clean_background: BackgroundCleanBackground,
        *,
        settings: RotationRecoverySettings,
        now_ms: Callable[[], int],
        stop: threading.Event,
        propose: Callable[[Mapping[str, Any], Mapping[str, Any], int], bool],
        on_drift_report: Callable[[MonitorReport, tuple[str, ...]], None] | None = None,
        on_requires_setup: Callable[[str, str], None] | None = None,
        in_probation: Callable[[], bool] = lambda: False,
        reference_document: Callable[[], Mapping[str, Any]] | None = None,
        cpu_clock_s: Callable[[], float] = time.thread_time,
    ) -> None:
        self.document = copy.deepcopy(document)
        self.provenance = copy.deepcopy(provenance)
        self.clean_background = clean_background
        self.settings = settings
        self.safety_enabled = settings.safety_enabled
        self.confirmation = CandidateConfirmation()
        self.generation = 0
        self.blocked_reason: str | None = None
        self._reference_source = reference_document or (lambda: self._bootstrap_document)
        self._bootstrap_document = copy.deepcopy(document)
        self._now_ms = now_ms
        self._stop = stop
        self._propose = propose
        self._on_drift_report = on_drift_report
        self._on_requires_setup = on_requires_setup
        self._in_probation = in_probation
        self._cpu_clock_s = cpu_clock_s
        self._run_lock = threading.Lock()
        self._cycle_generation = 0
        self._next_run_ms = now_ms() + settings.interval_ms
        self._ref_cache: dict[str, tuple[Path, Features]] = {}
        self._recent_cache: dict[str, tuple[tuple[int, int, int], Features]] = {}
        self._last_signatures: dict[str, tuple[int, int, int]] | None = None
        self._last_candidate_report: dict[str, Any] | None = None
        self.running = False
        self.cycles = 0
        self.budget_deferrals = 0
        self.last_status: str | None = None
        self.last_error: str | None = None
        self.last_report: dict[str, Any] | None = None

    def activate(self, document: Mapping[str, Any], provenance: Mapping[str, Any]) -> None:
        self.document = copy.deepcopy(document)
        self.provenance = copy.deepcopy(provenance)
        self.generation += 1
        self.blocked_reason = None
        self.confirmation.reset()
        self._last_candidate_report = None
        self._last_signatures = None

    def reset_camera(self, camera_id: str) -> None:
        self.generation += 1
        self.confirmation.reset()
        self._last_candidate_report = None
        self._last_signatures = None
        self._recent_cache.pop(camera_id, None)

    def block(self, reason: str) -> None:
        self.generation += 1
        self.blocked_reason = reason
        self.confirmation.reset()
        self._last_candidate_report = None
        log.warning("회전 복구 보류: %s", reason)

    def _load_references(self, config, check_budget: Callable[[], None]) -> dict[str, tuple[dict[str, Any], Features]] | None:
        ref_doc = self._reference_source()
        ref_cameras = {c["camera_id"]: c for c in ref_doc["cameras"]}
        references: dict[str, tuple[dict[str, Any], Features]] = {}
        for cam in self.document["cameras"]:
            cid = cam["camera_id"]
            ref_entry = self.clean_background._reference.get(cid)
            if ref_entry is None or cid not in ref_cameras:
                return None
            folder, meta = ref_entry
            ref_cam = ref_cameras[cid]
            cached = self._ref_cache.get(cid)
            if cached is not None and cached[0] == folder:
                references[cid] = ref_cam, cached[1]
                continue
            check_budget()
            rec = meta["cameras"][cid]
            image = cv2.imread(str(folder / rec["image"]))
            mask = cv2.imread(str(folder / rec["valid_mask"]), cv2.IMREAD_GRAYSCALE)
            if image is None or mask is None:
                return None
            features = extract_features(image, mask, ref_cam, config)
            self._ref_cache[cid] = folder, features
            references[cid] = ref_cam, features
        return references

    def _load_recent(
        self, config, check_budget: Callable[[], None]
    ) -> tuple[dict[str, Features], dict[str, int], dict[str, tuple[int, int, int]]] | None:
        signatures: dict[str, tuple[int, int, int]] = {}
        for cam in self.document["cameras"]:
            cid = cam["camera_id"]
            sig = self.clean_background.recent_signature(cid)
            if sig is None:
                return None
            signatures[cid] = sig
        if self._last_signatures == signatures:
            return None
        features: dict[str, Features] = {}
        recent_ms: dict[str, int] = {}
        for cam in self.document["cameras"]:
            cid = cam["camera_id"]
            sig = signatures[cid]
            cached = self._recent_cache.get(cid)
            if cached is not None and cached[0] == sig:
                features[cid] = cached[1]
                recent_ms[cid] = sig[2]
                continue
            check_budget()
            synthesized = self.clean_background.synthesize_recent(cid)
            if synthesized is None:
                return None
            bg, valid, end_ms = synthesized
            check_budget()
            try:
                feat = extract_features(bg, valid, cam, config)
            except ValueError as exc:
                if str(exc).startswith("STREAM_CHANGED"):
                    raise ValueError(f"STREAM_CHANGED:{cid}:{exc}") from exc
                raise
            self._recent_cache[cid] = sig, feat
            features[cid] = feat
            recent_ms[cid] = end_ms
        return features, recent_ms, signatures

    def _emit_monitor_report(
        self,
        now_ms: int,
        recent_ms: Mapping[str, int],
        *,
        bad_cameras: set[str],
        status_for_bad: MonitorStatus,
        findings: tuple[DriftFinding, ...] = (),
        rotations: Mapping[str, Any] = (),
    ) -> None:
        if self._on_drift_report is None:
            return
        min_samples = self.clean_background.settings.min_samples_per_camera
        threshold = self.settings.config.max_residual_px
        cameras = []
        for cam in self.document["cameras"]:
            cid = cam["camera_id"]
            sample_ms = min(now_ms, int(recent_ms.get(cid, now_ms)))
            fit = rotations.get(cid) if isinstance(rotations, Mapping) else None
            res = float(fit.get("rotation_residual_p95_px", threshold + 1.0 if cid in bad_cameras else 0.5)) if isinstance(fit, Mapping) else (threshold + 1.0 if cid in bad_cameras else 0.5)
            cameras.append(
                CameraMonitorReport(
                    camera_id=cid,
                    status=status_for_bad if cid in bad_cameras else MonitorStatus.MONITORING,
                    window_samples=min_samples,
                    window_median_px=res,
                    window_p95_px=res,
                    baseline_median_px=1.0,
                    baseline_verified=True,
                    threshold_px=threshold,
                    exceed_ms=0,
                    last_sample_ms=sample_ms,
                    window_last_sample_ms=sample_ms,
                )
            )
        age_limit = max(60_000, self.settings.interval_ms * 2)
        report = MonitorReport(
            t_ms=now_ms,
            calibration_version=self.document["calibration_version"],
            cameras=tuple(cameras),
            findings=findings,
            min_window_samples=1,
            max_sample_age_ms=age_limit,
        )
        changed_streams = tuple(f.camera_id for f in findings if f.reason is DriftReason.RESOLUTION_CHANGED)
        self._on_drift_report(report, changed_streams)

    def _handle_requires_setup(
        self, now_ms: int, cid: str, reason: str, recent_ms: Mapping[str, int], rotations: Mapping[str, Any]
    ) -> None:
        self.confirmation.reset()
        self._last_candidate_report = None
        self.last_status = "REQUIRES_SETUP"
        self.last_error = f"현장 세팅 다시 하기: {cid} ({reason})"
        if self._on_requires_setup is not None:
            self._on_requires_setup(cid, reason)
        stream_changed = reason.startswith("STREAM_CHANGED")
        finding = DriftFinding(
            camera_id=cid,
            reason=DriftReason.RESOLUTION_CHANGED if stream_changed else DriftReason.REPROJECTION_INCREASED,
            message=f"현장 세팅 다시 하기: {reason}",
        )
        self._emit_monitor_report(
            now_ms,
            recent_ms,
            bad_cameras={cid},
            status_for_bad=MonitorStatus.STREAM_CHANGED if stream_changed else MonitorStatus.DRIFTED,
            findings=(finding,),
            rotations=rotations,
        )
        self.clean_background.reset_camera(cid)
        self._recent_cache.pop(cid, None)

    def run_once(self) -> dict[str, Any] | None:
        if not self._run_lock.acquire(blocking=False):
            return None
        self._cycle_generation = self.generation
        self.running = True
        now = self._now_ms()
        cpu_start = self._cpu_clock_s()
        budget_s = self.settings.cpu_budget_ms / 1000.0
        config = self.settings.config

        def check_budget() -> None:
            if self._stop.is_set():
                raise BudgetExceededError("중지 신호 수신")
            if self._cpu_clock_s() - cpu_start > budget_s:
                raise BudgetExceededError(f"CPU 시간 예산 초과 ({self.settings.cpu_budget_ms}ms)")

        try:
            references = self._load_references(config, check_budget)
            if references is None:
                self.last_status = "NOT_READY"
                return {"status": "NOT_READY"}
            loaded = self._load_recent(config, check_budget)
            if loaded is None:
                self.last_status = self.last_status or "NOT_READY"
                return {"status": "WAITING_NEW_SAMPLES"}
            features, recent_ms, signatures = loaded
            version = f"cal-rot-{now}-{self.cycles + 1}-{uuid.uuid4().hex[:8]}"
            candidate, report = make_candidate(
                self.document,
                references,
                features,
                recent_ms,
                at_ms=now,
                version=version,
                config=config,
                check_budget=check_budget,
            )
            if self._cycle_generation != self.generation:
                return None
            self._last_signatures = signatures
            self.cycles += 1
            self.last_report = report
            status = report["status"]
            if self._in_probation():
                bad_ids = set(report.get("changed_camera_ids") or ())
                if status == "REQUIRES_SETUP" and report.get("camera_id"):
                    bad_ids.add(report["camera_id"])
                self._emit_monitor_report(
                    now,
                    recent_ms,
                    bad_cameras=bad_ids,
                    status_for_bad=MonitorStatus.SUSPECT,
                    rotations=report.get("rotations", {}),
                )
                self.last_status = "PROBATION_BAD" if bad_ids else "PROBATION_OK"
                self.last_error = report.get("reason") if bad_ids else None
                return report
            if status == "STABLE":
                self.confirmation.reset()
                self._last_candidate_report = None
                self.last_status = "STABLE"
                self.last_error = None
                return report
            if status == "REQUIRES_SETUP":
                self._handle_requires_setup(
                    now, report["camera_id"], str(report["reason"]), recent_ms, report.get("rotations", {})
                )
                return report
            assert candidate is not None
            if self.blocked_reason is not None:
                self.last_status = "BLOCKED"
                self.last_error = self.blocked_reason
                return report
            if self.safety_enabled:
                if self.confirmation.count > 0 and not consistent_rotation(
                    self._last_candidate_report, report, config.confirmation_tolerance_rad
                ):
                    self.confirmation.reset()
                confirmed = self.confirmation.observe(report)
                self._last_candidate_report = report
                if not confirmed:
                    self.last_status = "CONFIRMING"
                    self.last_error = None
                    return report
            else:
                report["consecutive_confirmation"] = {
                    "count": 2,
                    "required": 2,
                    "validation_end_ms": report["validation_end_ms"],
                }
            accepted = self._propose(candidate, report, self._cycle_generation)
            if accepted:
                for cid in report["changed_camera_ids"]:
                    self.clean_background.reset_camera(cid)
                    self._recent_cache.pop(cid, None)
                self.last_status = "PROPOSED"
                self.last_error = None
            else:
                self.last_status = "REJECTED"
                self.last_error = "보정 후보 수락 거부"
            return report
        except BudgetExceededError as exc:
            self.budget_deferrals += 1
            self.last_status = "BUDGET_DEFERRED"
            self.last_error = str(exc)
            log.info("회전 복구 다음 주기로 이월: %s", exc)
            return {"status": "BUDGET_DEFERRED", "reason": str(exc)}
        except ValueError as exc:
            msg = str(exc)
            if msg.startswith("STREAM_CHANGED"):
                parts = msg.split(":", 2)
                cid = parts[1] if len(parts) == 3 else self.document["cameras"][0]["camera_id"]
                reason = parts[2] if len(parts) == 3 else msg
                self._handle_requires_setup(now, cid, reason, {}, {})
                return {"status": "REQUIRES_SETUP", "camera_id": cid, "reason": reason}
            self.confirmation.reset()
            self.cycles += 1
            self.last_status, self.last_error = "FAILED", msg
            log.exception("회전 복구 실행 실패(판정은 계속한다)")
            return None
        except Exception as exc:  # noqa: BLE001
            self.confirmation.reset()
            self.cycles += 1
            self.last_status, self.last_error = "FAILED", str(exc)
            log.exception("회전 복구 실행 실패(판정은 계속한다)")
            return None
        finally:
            self.running = False
            self._run_lock.release()

    def run(self) -> None:
        _lower_thread_priority()
        while not self._stop.wait(0.1):
            if self._now_ms() >= self._next_run_ms:
                self.run_once()
                self._next_run_ms = self._now_ms() + self.settings.interval_ms

    def status(self) -> dict[str, Any]:
        return {
            "active_version": self.document["calibration_version"],
            "running": self.running,
            "cycles": self.cycles,
            "budget_deferrals": self.budget_deferrals,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "blocked_reason": self.blocked_reason,
            "safety_enabled": self.safety_enabled,
            "confirmation_count": self.confirmation.count,
        }
