"""배경 특징점 기반 카메라 순수 회전 감지·복구 워커 (명세 4.4, S15P31A101-454).

판정 틱 밖의 전용 스레드가 상주 CPU 작업자에 카메라별 기준·최근 배경 비교를 맡긴다.
순수 회전은 CandidateConfirmation(2회 연속 확인) → tick 경계 교체 → SwapProbation(60초 감시)을 거치고,
위치 이동·시차·허용각 초과(REQUIRES_SETUP)는 자동 복구 없이 DRIFTED 및 '현장 세팅 다시 하기' 경고를 올린다.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import pickle
import queue
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from services.tier0.clean_background import BackgroundCleanBackground
from services.tier0.settings import RotationRecoverySettings
from simai_ai.calibration.drift import DriftFinding, DriftReason
from simai_ai.calibration.live_safety import CandidateConfirmation
from simai_ai.calibration.monitor import CameraMonitorReport, MonitorReport, MonitorStatus
from simai_ai.calibration.rotation_recovery import consistent_rotation

log = logging.getLogger("services.tier0.rotation_recovery")

DEFERRAL_WARN_EVERY = 4
"""예산 이월이 이만큼 연속이면 경고한다(15 s 주기로 1분). 이월 중에는 배경 기반 틀어짐·쓰러짐 감지가 멈춰 있다(-746)"""


class BudgetExceededError(RuntimeError):
    """단일 실행 주기의 CPU 시간 예산을 초과했을 때 다음 주기로 이월한다."""


def _lower_thread_priority() -> None:
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass


def _rotation_summary(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """보고서의 카메라별 적합을 로그·상태용으로 줄인다(회전량은 도, 잔차는 원본 px)."""
    out: dict[str, dict[str, Any]] = {}
    for cid, fit in (report.get("rotations") or {}).items():
        if not isinstance(fit, Mapping):
            continue
        angle_rad = fit.get("angle_rad")
        residual = fit.get("rotation_residual_p95_px")
        out[str(cid)] = {
            "status": str(fit.get("status")),
            "angle_deg": None if angle_rad is None else round(math.degrees(float(angle_rad)), 3),
            "residual_p95_px": None if residual is None else round(float(residual), 3),
            "matches": int(fit.get("matches") or 0),
            "reason": fit.get("reason"),
        }
    return out


def _camera_cpu_text(by_camera: Mapping[str, Mapping[str, float]]) -> str:
    return ", ".join(f"{cid}={sum(float(v) for v in row.values()):.0f}ms"
                     for cid, row in sorted(by_camera.items())) or "-"


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
        on_report: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        """on_report: 비교를 마친 주기마다 보고서(카메라별 rotations 포함)를 받는다. 융합에서 뺀 카메라의 배경 재검증용(-646)."""
        self.document = copy.deepcopy(document)
        self.provenance = copy.deepcopy(provenance)
        self.clean_background = clean_background
        self.clean_background.configure_rotation_samples(settings.config.max_image_side)
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
        self._on_report = on_report
        self._in_probation = in_probation
        self._process = None
        self.worker_pid = None
        self._run_lock = threading.Lock()
        self._cycle_generation = 0
        self._next_run_ms = now_ms() + settings.interval_ms
        self._last_signatures: dict[str, tuple[int, int, int]] | None = None
        self._last_candidate_report: dict[str, Any] | None = None
        self.running = False
        self.cycles = 0
        self.budget_deferrals = 0
        self.last_status: str | None = None
        self.last_error: str | None = None
        self.last_report: dict[str, Any] | None = None
        self.last_cpu_ms: dict[str, float] = {}
        self.feature_sides: dict[str, int] = {}
        self.drifted_cameras: set[str] = set()
        # -746: 카메라별 CPU·이어쓴 특징·연속 이월·작업자가 이미 받은 표본(다시 보내지 않음)
        self.consecutive_deferrals = 0
        self.last_completed_ms: int | None = None
        self.last_cpu_ms_by_camera: dict[str, dict[str, float]] = {}
        self.last_refreshed: tuple[str, ...] = ()
        self.last_carried: tuple[str, ...] = ()
        self.last_pending: tuple[str, ...] = ()
        self.feature_points: dict[str, int] = {}
        self.last_rotations: dict[str, dict[str, Any]] = {}
        self.samples_sent = 0
        self.samples_omitted = 0
        self.sample_resyncs = 0
        self._worker_samples: dict[str, set[tuple]] = {}

    def activate(self, document: Mapping[str, Any], provenance: Mapping[str, Any]) -> None:
        self.document = copy.deepcopy(document)
        self.provenance = copy.deepcopy(provenance)
        self.generation += 1
        self.blocked_reason = None
        self.drifted_cameras.clear()
        self.confirmation.reset()
        self._last_candidate_report = None
        self._last_signatures = None

    def reset_camera(self, camera_id: str) -> None:
        self.generation += 1
        self.confirmation.reset_candidate()
        self.confirmation.clear_camera_drift(camera_id)
        self._last_candidate_report = None
        self._last_signatures = None

    def block(self, reason: str) -> None:
        self.generation += 1
        self.blocked_reason = reason
        self.confirmation.reset()
        self._last_candidate_report = None
        log.warning("회전 복구 보류: %s", reason)

    def _load_references(self) -> dict | None:
        ref_doc = self._reference_source()
        cameras = {c["camera_id"]: c for c in ref_doc["cameras"]}
        references = {}
        for camera in self.document["cameras"]:
            cid = camera["camera_id"]
            if cid in self.drifted_cameras:
                continue
            entry = self.clean_background._reference.get(cid)
            if entry is None or cid not in cameras:
                return None
            folder, meta = entry
            record = meta["cameras"][cid]
            ref_camera = cameras[cid]
            key = (str(folder), meta.get("camera_generations", {}).get(cid),
                   ref_doc["calibration_version"], json.dumps(ref_camera, sort_keys=True),
                   json.dumps(record, sort_keys=True))
            references[cid] = {"key": key, "camera": copy.deepcopy(ref_camera),
                               "image": str(folder / record["image"]),
                               "mask": str(folder / record["valid_mask"])}
        return references or None

    def _compute(self, request):
        if self._process is None or self._process.poll() is not None:
            self.close()
            # 자식에만 적용한다. 부모의 OpenCV/BLAS 전역 설정이나 환경은 바꾸지 않는다.
            env = dict(os.environ)
            env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
                       VECLIB_MAXIMUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
            self._worker_samples = {}  # 새 작업자는 받은 표본이 없다
            self._process = subprocess.Popen(
                [sys.executable, "-m", "simai_ai.calibration.rotation_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        process = self._process
        pickle.dump(request, process.stdin, protocol=pickle.HIGHEST_PROTOCOL)
        process.stdin.flush()
        responses = queue.Queue(maxsize=1)
        def receive():
            try:
                responses.put(pickle.load(process.stdout))
            except Exception as exc:  # noqa: BLE001 -- IPC 오류도 부모에게 전달해 무한 대기를 막는다.
                responses.put(exc)
        reader = threading.Thread(target=receive, name="rotation-result", daemon=True)
        reader.start()
        while True:
            if self._stop.is_set():
                self.close()
                reader.join(timeout=1)
                raise BudgetExceededError("중지 신호 수신")
            try:
                result = responses.get(timeout=.05)
            except queue.Empty:
                if process.poll() is not None:
                    raise RuntimeError("회전 복구 작업자 종료") from None
                continue
            if isinstance(result, Exception):
                raise RuntimeError("회전 복구 작업자 응답 실패") from result
            return result

    def close(self) -> None:
        """판정 tick 밖에서 종료한다. 응답 없는 작업자도 서비스 종료를 붙잡지 않는다."""
        process = self._process
        self._process = None
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            process.stdin.close()
            process.stdout.close()

    def _emit_monitor_report(
        self,
        now_ms: int,
        recent_ms: Mapping[str, int],
        *,
        bad_cameras: set[str],
        status_for_bad: MonitorStatus,
        status_by_camera: Mapping[str, MonitorStatus] | None = None,
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
            if status_by_camera is not None and cid in status_by_camera:
                cam_status = status_by_camera[cid]
            elif cid in bad_cameras:
                cam_status = status_for_bad
            elif cid in self.drifted_cameras:
                cam_status = MonitorStatus.DRIFTED
            else:
                cam_status = MonitorStatus.MONITORING
            is_bad = cam_status in (MonitorStatus.SUSPECT, MonitorStatus.DRIFTED, MonitorStatus.STREAM_CHANGED)
            fit = rotations.get(cid) if isinstance(rotations, Mapping) else None
            res = (
                float(fit.get("rotation_residual_p95_px", threshold + 1.0 if is_bad else 0.5))
                if isinstance(fit, Mapping)
                else (threshold + 1.0 if is_bad else 0.5)
            )
            cameras.append(
                CameraMonitorReport(
                    camera_id=cid,
                    status=cam_status,
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
        self,
        now_ms: int,
        failed_cameras: Mapping[str, str],
        recent_ms: Mapping[str, int],
        rotations: Mapping[str, Any],
        *,
        suspect_cameras: Mapping[str, str] | None = None,
    ) -> None:
        self.confirmation.reset_candidate()
        for cid in failed_cameras:
            self.confirmation.clear_camera_drift(cid)
            self.drifted_cameras.add(cid)
        self._last_candidate_report = None
        first_cid, first_reason = next(iter(failed_cameras.items()))
        self.last_status = "REQUIRES_SETUP"
        self.last_error = f"현장 세팅 다시 하기: {first_cid} ({first_reason})"
        findings = []
        status_by_camera: dict[str, MonitorStatus] = {}
        if suspect_cameras:
            for cid in suspect_cameras:
                status_by_camera[cid] = MonitorStatus.SUSPECT
        for cid, reason in failed_cameras.items():
            if self._on_requires_setup is not None:
                self._on_requires_setup(cid, reason)
            stream_changed = reason.startswith("STREAM_CHANGED")
            status_by_camera[cid] = (
                MonitorStatus.STREAM_CHANGED if stream_changed else MonitorStatus.DRIFTED
            )
            findings.append(
                DriftFinding(
                    camera_id=cid,
                    reason=DriftReason.RESOLUTION_CHANGED if stream_changed else DriftReason.REPROJECTION_INCREASED,
                    message=f"현장 세팅 다시 하기: {reason}",
                )
            )
        self._emit_monitor_report(
            now_ms,
            recent_ms,
            bad_cameras=set(failed_cameras) | set(suspect_cameras or ()),
            status_for_bad=MonitorStatus.DRIFTED,
            status_by_camera=status_by_camera,
            findings=tuple(findings),
            rotations=rotations,
        )
        for cid in failed_cameras:
            self.clean_background.reset_camera(cid)

    def run_once(self) -> dict[str, Any] | None:
        if not self._run_lock.acquire(blocking=False):
            return None
        self._cycle_generation = self.generation
        self.running = True
        now = self._now_ms()
        completed: dict[str, Any] | None = None
        try:
            if self._stop.is_set():
                return None
            references = self._load_references()
            if references is None:
                self.last_status = "NOT_READY"
                return {"status": "NOT_READY"}
            if self._process is not None and self._process.poll() is not None:
                self._worker_samples = {}  # 죽은 작업자가 받았던 표본은 새 작업자에 없다
            for attempt in range(2):
                # -746: 작업자가 이미 받은 표본은 비워 보낸다(분리 켬이면 검출 프로세스가 쓰는 파일도 준다).
                recent = self.clean_background.rotation_snapshot(
                    exclude_cameras=self.drifted_cameras, omit_samples=self._worker_samples or None)
                if recent is None:
                    self.last_status = "NOT_READY"
                    return {"status": "NOT_READY"}
                signatures = {cid: data["signature"] for cid, data in recent.items()}
                if self._last_signatures == signatures:
                    return {"status": "WAITING_NEW_SAMPLES"}
                recent_ms = {cid: sig[2] for cid, sig in signatures.items()}
                sent = sum(sample is not None for view in recent.values() for sample in view["samples"])
                omitted = sum(sample is None for view in recent.values() for sample in view["samples"])
                result = self._compute({
                    "document": copy.deepcopy(self.document), "references": references, "recent": recent,
                    "recent_ms": recent_ms, "config": self.settings.config,
                    "budget_ms": self.settings.cpu_budget_ms, "now_ms": now,
                    "max_feature_age_ms": 2 * self.settings.interval_ms,
                    "version": f"cal-rot-{now}-{self.cycles + 1}-{uuid.uuid4().hex[:8]}",
                    "ignored_cameras": sorted(self.drifted_cameras),
                })
                self._worker_samples = {
                    str(cid): {(int(k[0]), int(k[1]), tuple(k[2])) for k in keys}
                    for cid, keys in (result.get("cached_samples") or {}).items()}
                self.samples_sent += sent
                self.samples_omitted += omitted
                if result["status"] == "ERROR" and str(result.get("reason", "")).startswith("SAMPLE_CACHE_MISS"):
                    # 작업자 캐시와 어긋났다(예: 작업자 교체). 비운 자리 없이 한 번 더 보낸다.
                    self.sample_resyncs += 1
                    self._worker_samples = {}
                    if attempt == 0:
                        continue
                break
            self.last_cpu_ms = result["timings"]
            self.last_cpu_ms_by_camera = dict(result.get("timings_by_camera") or {})
            self.last_refreshed = tuple(result.get("refreshed_cameras") or ())
            self.last_carried = tuple(result.get("carried_cameras") or ())
            self.last_pending = tuple(result.get("pending_cameras") or ())
            self.feature_points = dict(result.get("feature_points") or {})
            self.worker_pid = result.get("worker_pid")
            self.feature_sides = result.get("feature_sides", {})
            if self._cycle_generation != self.generation:
                return None
            # 계산 중 새 표본이 온 것은 결과를 버릴 이유가 아니다(그 창의 판정으로 유효). 카메라 리셋(세대 변경)만 버린다(-746).
            for cid, sig in signatures.items():
                current = self.clean_background.recent_signature(cid)
                if current is None or current[0] != sig[0]:
                    return {"status": "WAITING_NEW_SAMPLES"}
            if result["status"] == "BUDGET_DEFERRED":
                raise BudgetExceededError(result["reason"])
            if result["status"] == "ERROR":
                raise ValueError(result["reason"])
            if result["status"] != "COMPLETED":
                self.last_status = result["status"]
                return {"status": self.last_status}
            candidate, report = result["candidate"], result["report"]
            config = self.settings.config
            if self._cycle_generation != self.generation:
                return None
            recent_ms = dict(report.get("recent_samples_ms") or recent_ms)
            if not self.last_carried:
                self._last_signatures = signatures  # 이어쓴 카메라가 있으면 새 표본이 없어도 다음 주기에 마저 뽑는다
            self.cycles += 1
            self.last_report = report
            self.last_completed_ms = now
            self.last_rotations = _rotation_summary(report)
            completed = report
            if self._on_report is not None:
                try:
                    self._on_report(report)
                except Exception:  # noqa: BLE001 - 재검증 실패가 회전 복구를 멈추지 않는다
                    log.exception("회전 복구 보고서 전달 실패(복구는 계속한다)")
            status = report["status"]
            if self._in_probation():
                bad_ids = set(report.get("changed_camera_ids") or ())
                if status == "REQUIRES_SETUP":
                    bad_ids.update((report.get("failed_cameras") or {}).keys())
                    if report.get("camera_id"):
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
                if self.last_status in ("CONFIRMING", "SUSPECT", "NO_EVIDENCE"):
                    self._emit_monitor_report(
                        now,
                        recent_ms,
                        bad_cameras=set(),
                        status_for_bad=MonitorStatus.MONITORING,
                        rotations=report.get("rotations", {}),
                    )
                self.last_status = "STABLE"
                self.last_error = None
                return report
            if status == "NO_EVIDENCE":
                self.confirmation.reset_candidate()
                self._last_candidate_report = None
                self.last_status = "NO_EVIDENCE"
                self.last_error = str(report.get("reason") or "유효 배경 근거 부족")
                no_ev = set((report.get("no_evidence_cameras") or {}).keys())
                for cid in no_ev:
                    self.confirmation.clear_camera_drift(cid)
                self._emit_monitor_report(
                    now,
                    recent_ms,
                    bad_cameras=no_ev,
                    status_for_bad=MonitorStatus.NO_EVIDENCE,
                    rotations=report.get("rotations", {}),
                )
                return report
            if status == "REQUIRES_SETUP":
                raw_failed = report.get("failed_cameras")
                if isinstance(raw_failed, Mapping) and raw_failed:
                    failed_map = {str(k): str(v) for k, v in raw_failed.items()}
                else:
                    failed_map = {str(report["camera_id"]): str(report["reason"])}
                window_ms = max(self.settings.interval_ms * 2, 1_000)
                if self.safety_enabled:
                    confirmed, suspect = self.confirmation.observe_drift(
                        self.document["calibration_version"],
                        report,
                        at_ms=now,
                        multi_camera_window_ms=window_ms,
                    )
                    if report.get("multi_camera_suspect") or len(failed_map) >= 2:
                        self._last_candidate_report = None
                        self.last_status = "SUSPECT"
                        self.last_error = (
                            f"다수 카메라 동시 배경 불일치(기준·환경 의심, 융합 제외 보류): {', '.join(sorted(failed_map))}"
                        )
                        report["status"] = "SUSPECT"
                        report["suspect_cameras"] = dict(failed_map)
                        self._emit_monitor_report(
                            now,
                            recent_ms,
                            bad_cameras=set(failed_map),
                            status_for_bad=MonitorStatus.SUSPECT,
                            rotations=report.get("rotations", {}),
                        )
                        return report
                    if not confirmed:
                        self._last_candidate_report = None
                        self.last_status = "CONFIRMING"
                        self.last_error = f"재세팅 확인 대기: {', '.join(sorted(suspect))}"
                        self._emit_monitor_report(
                            now,
                            recent_ms,
                            bad_cameras=set(suspect),
                            status_for_bad=MonitorStatus.SUSPECT,
                            rotations=report.get("rotations", {}),
                        )
                        return report
                elif len(failed_map) >= 2:
                    self._last_candidate_report = None
                    self.last_status = "SUSPECT"
                    self.last_error = (
                        f"다수 카메라 동시 배경 불일치(기준·환경 의심, 융합 제외 보류): {', '.join(sorted(failed_map))}"
                    )
                    report["status"] = "SUSPECT"
                    report["suspect_cameras"] = dict(failed_map)
                    self._emit_monitor_report(
                        now,
                        recent_ms,
                        bad_cameras=set(failed_map),
                        status_for_bad=MonitorStatus.SUSPECT,
                        rotations=report.get("rotations", {}),
                    )
                    return report
                else:
                    confirmed, suspect = failed_map, {}
                    report["consecutive_confirmation"] = {
                        "count": 2,
                        "required": 2,
                        "validation_end_ms": report["validation_end_ms"],
                        "camera_counts": {cid: 2 for cid in confirmed},
                    }
                self._handle_requires_setup(
                    now, confirmed, recent_ms, report.get("rotations", {}), suspect_cameras=suspect
                )
                return report
            assert candidate is not None
            if self.blocked_reason is not None or self.drifted_cameras:
                self.last_status = "BLOCKED"
                self.last_error = self.blocked_reason or "재세팅 필요 카메라 존재"
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
                self.last_status = "PROPOSED"
                self.last_error = None
            else:
                self.last_status = "REJECTED"
                self.last_error = "보정 후보 수락 거부"
            return report
        except BudgetExceededError as exc:
            self.budget_deferrals += 1
            self.consecutive_deferrals += 1
            self.last_status = "BUDGET_DEFERRED"
            self.last_error = str(exc)
            log.info("회전 복구 다음 주기로 이월: %s (cpu %.0fms, 새로 뽑음 %s)", exc,
                     float(self.last_cpu_ms.get("total", 0.0)), ",".join(self.last_refreshed) or "-")
            if self.consecutive_deferrals % DEFERRAL_WARN_EVERY == 0:
                log.warning(
                    "회전 복구 이월 %d회 연속: 배경 기반 카메라 틀어짐·쓰러짐 감지가 멈춰 있다 (예산 %dms, 카메라별 cpu %s)",
                    self.consecutive_deferrals, self.settings.cpu_budget_ms, _camera_cpu_text(self.last_cpu_ms_by_camera))
            return {"status": "BUDGET_DEFERRED", "reason": str(exc)}
        except ValueError as exc:
            msg = str(exc)
            if msg.startswith("STREAM_CHANGED"):
                parts = msg.split(":", 2)
                cid = parts[1] if len(parts) == 3 else self.document["cameras"][0]["camera_id"]
                reason = parts[2] if len(parts) == 3 else msg
                self._handle_requires_setup(now, {cid: reason}, {}, {})
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
            if completed is not None:
                self._log_completed(completed)
            self.running = False
            self._run_lock.release()

    def _log_completed(self, report: Mapping[str, Any]) -> None:
        """완료 주기마다 한 줄: 결과·카메라별 회전량·판정·CPU(-746). 이월 뒤 첫 완료면 이월 횟수도 싣는다."""
        after = self.consecutive_deferrals
        self.consecutive_deferrals = 0
        cams = ", ".join(
            f"{cid}={row['status']} {row['angle_deg']:.2f}° p95 {row['residual_p95_px']:.2f}px m{row['matches']}"
            if row.get("angle_deg") is not None and row.get("residual_p95_px") is not None
            else f"{cid}={row['status']} m{row['matches']}"
            for cid, row in sorted(self.last_rotations.items()))
        log.info(
            "회전 복구 완료: 결과=%s 상태=%s cpu %.0fms(새로 뽑음 %s, 이어씀 %s)%s | %s",
            report.get("status"), self.last_status, float(self.last_cpu_ms.get("total", 0.0)),
            ",".join(self.last_refreshed) or "-", ",".join(self.last_carried) or "-",
            f" 이월 {after}회 뒤" if after else "", cams or "-")

    def timing_summary(self) -> dict[str, Any]:
        """30초 stage_timing 줄에 싣는 요약(-746). 판정에 쓰지 않는다."""
        return {
            "last_status": self.last_status,
            "cycles": self.cycles,
            "budget_deferrals": self.budget_deferrals,
            "consecutive_deferrals": self.consecutive_deferrals,
            "budget_ms": self.settings.cpu_budget_ms,
            "last_completed_ms": self.last_completed_ms,
            "cpu_ms": {k: round(float(v), 1) for k, v in self.last_cpu_ms.items()},
            "cpu_ms_by_camera": {cid: round(sum(float(v) for v in row.values()), 1)
                                 for cid, row in sorted(self.last_cpu_ms_by_camera.items())},
            "refreshed": list(self.last_refreshed),
            "carried": list(self.last_carried),
            "pending": list(self.last_pending),
            "feature_sides": dict(self.feature_sides),
            "rotations": {cid: {"status": row["status"], "angle_deg": row.get("angle_deg")}
                          for cid, row in sorted(self.last_rotations.items())},
            "samples_sent": self.samples_sent,
            "samples_omitted": self.samples_omitted,
        }

    def run(self) -> None:
        _lower_thread_priority()
        try:
            while not self._stop.wait(0.1):
                if self._now_ms() >= self._next_run_ms:
                    self.run_once()
                    self._next_run_ms = self._now_ms() + self.settings.interval_ms
        finally:
            self.close()

    def status(self) -> dict[str, Any]:
        return {
            "active_version": self.document["calibration_version"],
            "running": self.running,
            "cycles": self.cycles,
            "budget_deferrals": self.budget_deferrals,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_cpu_ms": dict(self.last_cpu_ms),
            "last_cpu_ms_by_camera": {cid: dict(row) for cid, row in self.last_cpu_ms_by_camera.items()},
            "consecutive_deferrals": self.consecutive_deferrals,
            "last_completed_ms": self.last_completed_ms,
            "refreshed_cameras": list(self.last_refreshed),
            "carried_cameras": list(self.last_carried),
            "pending_cameras": list(self.last_pending),
            "feature_points": dict(self.feature_points),
            "last_rotations": {cid: dict(row) for cid, row in self.last_rotations.items()},
            "samples_sent": self.samples_sent,
            "samples_omitted": self.samples_omitted,
            "sample_resyncs": self.sample_resyncs,
            "cpu_scope": "ROTATION_WORKER_PROCESS",
            "worker_pid": self.worker_pid,
            "feature_sides": dict(self.feature_sides),
            "blocked_reason": self.blocked_reason,
            "safety_enabled": self.safety_enabled,
            "confirmation_count": self.confirmation.count,
        }
