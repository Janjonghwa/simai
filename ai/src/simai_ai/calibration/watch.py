"""운영 중 보정 감시 묶음: 재투영 감시 → DRIFTED 표시 → 자동 재보정 시작 (명세 4.4, 4.5, 6.5, 12.2 calibration_update).

`CalibrationDriftWatch.update` 를 Tier 0 처리 주기마다 불러도 된다. 재투영 감시는 min_update_interval_ms(기본 1초)
간격으로만 잰다(사람 수에 비례하는 계산을 LIVE 주기마다 하지 않는다). 스트림 변경 통지는 간격과 상관없이 바로 본다.

1. 버전 저장소의 활성 보정 스냅숏을 본다. 버전이 바뀌었으면 감시기·트리거를 새로 만든다(기준선도 새로 잡는다).
   활성 보정이 없으면(UNCALIBRATED) 아무것도 하지 않는다. 검증된 기준선은 그 보정을 만든 입력 관측에서 잰
   `BaselineRecord`(`monitor.baseline_from_observations`) 뿐이다. 첫 보정은 보정 걷기 관측으로 만들어 baselines 로
   주고, 자동 재보정 결과는 `run_recalibration` 이 재보정 관측으로 만들어 `recalibration_finished` 가 넣는다. 저장·복원
   (`baselines()`)해도 검증 여부가 그대로 따라가고, 시간이 지나거나 재시작해도 검증된 것으로 바뀌지 않는다.
2. `monitor.ReprojectionMonitor` 로 카메라별 한 카메라 제외 재투영 오차를 갱신한다.
3. 새 틀어짐 근거가 있으면 `CalibrationVersionStore.report_drift` 로 DRIFTED 를 표시한다. 저장소는 새 버전을 만들지
   않고 상태 전이 기록만 추가한다(덮어쓰지 않음). 근거를 낸 버전이 이미 활성 버전이 아니면 표시하지 않는다.
   DATASET_GT·SIM·MODEL_ESTIMATE 보정은 저장소가 DRIFTED 로 표시하지 않는다(계약상 현장 보정이 아니다).
4. DRIFTED 이면 `recalibration.RecalibrationTrigger` 가 관측을 모아 재보정 요청을 낸다. 다음 경우에는 시작하지 않는다
   (보고의 recalibration_blocked):
   - 해상도·화각이 바뀐 카메라가 있다(DRIFTED 뒤에 바뀌어도). 번들 조정은 이전 내부 파라미터를 초기값·사전 분포로 쓰는데
     그 값이 맞지 않는다. 모으던 관측과 낸 요청도 취소한다(`request_still_valid` 가 False).
   - 원인 카메라를 가리지 못했다(귀속 불확실). 틀어진 카메라 회전 초기값을 맞출 수 없어 World 축 게이지를 보장하지 못한다.
   - 틀어진 카메라 회전 초기값을 맞출 대응이 아직 모자라다(계속 모으고, 맞추면 요청한다). 요청은 보고로 돌려주고
   실행은 호출자 몫이다(번들 조정은 수 초 걸려 LIVE 주기 안에서 돌리지 않는다). `start_recalibration` 콜백을 주면
   요청을 바로 넘긴다. 실행이 끝나면 `recalibration_finished` 로 결과를 알린다.

감시 근거가 없으면(사람 없음) 보정 상태를 바꾸지 않고 보고의 evidence 에 NONE(감시 근거 없음)을 남긴다. CALIBRATED 를
"확인했다"고 보고하지 않는다.

보고에는 입력 출처의 상한 여부(is_upper_bound)를 함께 싣는다. SIM·정답 입력으로 얻은 감지 시간·오경보 수는 상한이며
제품 성능으로 쓰지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from simai_ai.calibration.bundle import PersonObservation
from simai_ai.calibration.drift import CameraObservation, DriftFinding
from simai_ai.calibration.monitor import (
    STREAM_REASONS,
    BaselineRecord,
    MonitorConfig,
    MonitorEvidence,
    MonitorReport,
    MonitorStatus,
    ReprojectionMonitor,
)
from simai_ai.calibration.recalibration import (
    RecalibrationConfig,
    RecalibrationOutcome,
    RecalibrationRequest,
    RecalibrationTrigger,
    TriggerPhase,
)
from simai_ai.calibration.versions import ActiveCalibration, CalibrationVersionStore
from simai_ai.providers.contract import check_input_provenance
from simai_ai.tracking.system_health import CalibrationState
from simai_ai.tracking.time_model import require_ms


class UpdateReason(StrEnum):
    """calibration_update.reason 값 (contracts/messages/calibration_update.schema.json)."""

    INITIAL = "INITIAL"
    RECALIBRATED = "RECALIBRATED"
    STATE_CHANGED = "STATE_CHANGED"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    STREAM_CHANGED = "STREAM_CHANGED"


class RecalibrationBlock(StrEnum):
    """DRIFTED 인데 자동 재보정을 시작하지 않는 이유."""

    STREAM_CHANGED = "STREAM_CHANGED"
    """해상도·화각이 바뀐 카메라가 있다. 이전 내부 파라미터로 번들 조정을 하지 않는다"""
    ATTRIBUTION_UNCERTAIN = "ATTRIBUTION_UNCERTAIN"
    """불일치는 지속됐지만 원인 카메라를 가리지 못했다. 회전 초기값을 맞출 수 없어 World 축 게이지를 보장하지 못한다"""
    REALIGN_FAILED = "REALIGN_FAILED"
    """틀어진 카메라의 회전 초기값을 맞출 대응이 아직 모자라다(계속 모은다)"""


@dataclass(frozen=True)
class WatchReport:
    """감시 갱신 한 번의 결과.

    monitor 는 활성 보정이 없으면 None. drift_marked 는 이번 갱신에서 DRIFTED 로 표시했는지, drift_reason 은 이번 갱신에서
    보낼 calibration_update 의 reason(없으면 None. 이미 DRIFTED 인데 스트림이 바뀐 경우도 STREAM_CHANGED),
    recalibration_request 는 이번 갱신에서 낸 재보정 요청(없으면 None)이다. is_upper_bound 가 True 면 이 보고의 지표는 상한이다.
    """

    t_ms: int
    state: CalibrationState
    calibration_version: str | None
    monitor: MonitorReport | None
    drift_marked: bool
    drift_reason: UpdateReason | None
    recalibration_phase: TriggerPhase
    recalibration_request: RecalibrationRequest | None
    is_upper_bound: bool
    stream_changed_camera_ids: tuple[str, ...] = ()
    """해상도·화각이 바뀐 카메라(이 버전에서 알게 된 전부). 있으면 자동 재보정을 시작하지 않는다"""
    new_stream_changed_camera_ids: tuple[str, ...] = ()
    """이번 갱신에서 처음 알게 된 스트림 변경. 카메라가 이미 DRIFTED 여도 들어간다(drift_reason STREAM_CHANGED)"""
    recalibration_blocked: RecalibrationBlock | None = None
    """DRIFTED 인데 자동 재보정을 시작하지 않는 이유"""

    @property
    def evidence(self) -> MonitorEvidence:
        """감시 근거. 활성 보정이 없으면 NONE."""
        return MonitorEvidence.NONE if self.monitor is None else self.monitor.evidence


class CalibrationDriftWatch:
    """baselines: 보정 버전 -> 기준선 기록(`BaselineRecord`). 저장해 둔 `baselines()` 를 다시 시작할 때 준다."""

    def __init__(
        self,
        store: CalibrationVersionStore,
        input_provenance: Mapping[str, Any],
        *,
        monitor_config: MonitorConfig = MonitorConfig(),
        recalibration_config: RecalibrationConfig = RecalibrationConfig(),
        start_recalibration: Callable[[RecalibrationRequest], None] | None = None,
        min_update_interval_ms: int = 1_000,
        baselines: Mapping[str, BaselineRecord] | None = None,
    ) -> None:
        problems = check_input_provenance(input_provenance)
        if problems:
            raise ValueError(f"input_provenance 가 계약에 맞지 않는다: {problems}")
        if not isinstance(min_update_interval_ms, int) or min_update_interval_ms < 0:
            raise ValueError(f"min_update_interval_ms 는 0 이상의 정수여야 한다: {min_update_interval_ms!r}")
        self.store = store
        self.input_provenance = dict(input_provenance)
        self.monitor_config = monitor_config
        self.recalibration_config = recalibration_config
        self.start_recalibration = start_recalibration
        self.min_update_interval_ms = min_update_interval_ms
        self._baselines: dict[str, BaselineRecord] = dict(baselines or {})
        self._monitor: ReprojectionMonitor | None = None
        self._trigger: RecalibrationTrigger | None = None
        self._last_measured: int | None = None
        self._last_report: MonitorReport | None = None
        self._block: RecalibrationBlock | None = None

    @property
    def is_upper_bound(self) -> bool:
        return bool(self.input_provenance.get("is_upper_bound"))

    def baselines(self) -> dict[str, BaselineRecord]:
        """보정 버전별 기준선 기록(주어진 것 + 이번 감시에서 정한 것, 검증 여부 포함). 저장했다가 다시 시작할 때 준다."""
        out = dict(self._baselines)
        if self._monitor is not None:
            out[self._monitor.calibration_version] = self._monitor.baseline_record()
        return out

    def update(
        self,
        t_ms: int,
        people: Sequence[PersonObservation],
        streams: Iterable[CameraObservation] = (),
    ) -> WatchReport:
        """공통 시각 t_ms 의 사람 관측(Cross-view 연결 결과)과 스트림 정보로 감시를 갱신한다."""
        require_ms("t_ms", t_ms)
        streams = tuple(streams)
        snap = self.store.snapshot()
        if snap is None:
            self._monitor = self._trigger = None
            return self._report(t_ms, None, None, False, None, None, ())
        if self._monitor is None or self._monitor.calibration_version != snap.calibration_version:
            self._start_version(snap, t_ms)
        monitor, trigger = self._monitor, self._trigger
        assert monitor is not None and trigger is not None

        due = self._last_measured is None or t_ms - self._last_measured >= self.min_update_interval_ms
        findings: tuple[DriftFinding, ...] = ()
        if due or any(o.image_size is not None or o.field_of_view_changed for o in streams):
            report = monitor.observe(t_ms, [p.views for p in people] if due else [], streams)
            if due:
                self._last_measured = t_ms
            self._last_report = report
            findings = report.findings
        else:
            report = self._last_report
        new_streams = tuple(f.camera_id for f in findings if f.reason in STREAM_REASONS)
        drift_marked, reason = False, None
        if findings:
            drift_marked = self.store.report_drift(findings, t_ms, snap.calibration_version)
            if drift_marked:
                reason = _update_reason(findings)
        if new_streams:
            reason = UpdateReason.STREAM_CHANGED  # 이미 DRIFTED 여도 알린다(calibration_update)
            trigger.cancel()  # 모으던 관측·낸 요청은 이전 스트림 조건이다

        request = None
        self._block = None
        if self.store.state is CalibrationState.DRIFTED and self._is_active(snap.calibration_version):
            statuses = {} if report is None else {c.camera_id: c.status for c in report.cameras}
            drifted = sorted(cid for cid, st in statuses.items() if st is MonitorStatus.DRIFTED)
            if monitor.stream_changed_camera_ids:
                self._block = RecalibrationBlock.STREAM_CHANGED
            elif not drifted:
                self._block = RecalibrationBlock.ATTRIBUTION_UNCERTAIN
            else:
                trigger.start(t_ms, drifted)
                trigger.add(people)
                request = trigger.poll(t_ms)
                if trigger.realign_failed:
                    self._block = RecalibrationBlock.REALIGN_FAILED
                if request is not None and self.start_recalibration is not None:
                    self.start_recalibration(request)
        return self._report(t_ms, snap, report, drift_marked, reason, request, new_streams)

    def request_still_valid(self, request: RecalibrationRequest) -> bool:
        """낸 요청을 아직 채택해도 되는지: 같은 버전이 활성이고 DRIFTED 이며, 그 뒤 스트림이 바뀌지 않았다.

        `run_recalibration(..., is_valid=watch.request_still_valid)` 로 넘겨 제안 직전에 다시 확인한다.
        """
        trigger = self._trigger
        snap = self.store.snapshot()
        return (
            trigger is not None
            and trigger.issued is request
            and trigger.phase is TriggerPhase.REQUESTED
            and snap is not None
            and snap.calibration_version == request.calibration_version
            and snap.state is CalibrationState.DRIFTED
            and self._monitor is not None
            and not self._monitor.stream_changed_camera_ids
        )

    def recalibration_finished(self, outcome: RecalibrationOutcome, t_ms: int) -> None:
        """재보정 실행 결과를 알린다. 채택되면 다음 update 에서 새 버전 감시를 시작한다."""
        if outcome.adopted and outcome.baseline is not None:
            self._baselines[outcome.baseline.calibration_version] = outcome.baseline
        if self._trigger is not None:
            self._trigger.finished(outcome, t_ms)

    # ------------------------------------------------------------------ 내부

    def _start_version(self, snap: ActiveCalibration, t_ms: int) -> None:
        if self._monitor is not None:
            self._baselines[self._monitor.calibration_version] = self._monitor.baseline_record()
        doc = snap.doc()
        self._monitor = ReprojectionMonitor(
            doc, self.monitor_config, baseline=self._baselines.get(snap.calibration_version)
        )
        self._trigger = RecalibrationTrigger(doc, self.input_provenance, self.recalibration_config)
        self._last_measured = None
        self._last_report = None

    def _is_active(self, version: str) -> bool:
        snap = self.store.snapshot()
        return snap is not None and snap.calibration_version == version

    def _report(
        self,
        t_ms: int,
        snap: ActiveCalibration | None,
        report: MonitorReport | None,
        drift_marked: bool,
        reason: UpdateReason | None,
        request: RecalibrationRequest | None,
        new_streams: tuple[str, ...],
    ) -> WatchReport:
        return WatchReport(
            t_ms=t_ms,
            state=self.store.state,
            calibration_version=None if snap is None else snap.calibration_version,
            monitor=report,
            drift_marked=drift_marked,
            drift_reason=reason,
            recalibration_phase=TriggerPhase.IDLE if self._trigger is None else self._trigger.phase,
            recalibration_request=request,
            is_upper_bound=self.is_upper_bound,
            stream_changed_camera_ids=() if self._monitor is None else self._monitor.stream_changed_camera_ids,
            new_stream_changed_camera_ids=new_streams,
            recalibration_blocked=self._block,
        )


def _update_reason(findings: Iterable[DriftFinding]) -> UpdateReason:
    return (
        UpdateReason.STREAM_CHANGED
        if any(f.reason in STREAM_REASONS for f in findings)
        else UpdateReason.DRIFT_DETECTED
    )


def previous_calibration_version(store: CalibrationVersionStore, calibration_version: str) -> str | None:
    """그 버전이 활성화될 때 바뀌기 전 버전(첫 보정이면 None). 저장소의 전이 기록(추가만)에서 찾는다."""
    for tr in store.transitions:
        if tr.to_version == calibration_version and tr.from_version != calibration_version:
            return tr.from_version
    raise LookupError(f"보정 버전 {calibration_version!r} 의 전환 기록이 없다")


def calibration_update_payload(
    store: CalibrationVersionStore,
    *,
    reason: UpdateReason | str,
    effective_at: int,
    calibration_ref: str,
    monitor: MonitorReport | None = None,
) -> dict[str, Any]:
    """활성 보정의 calibration_update payload (I1, contracts/messages/calibration_update.schema.json 2.0.0).

    봉투(msg_id·sent_at·source_id)는 보내는 쪽이 붙인다. calibration_ref 는 보정 문서를 저장한 주소다.
    DRIFTED 표시는 새 버전을 만들지 않으므로 calibration_version 은 그대로이고, previous_calibration_version 은 이
    버전이 활성화되기 전 버전이다. cameras 에는 감시 표본이 충분한 카메라의 운영 중 재투영 오차만 넣는다(모르면 비움).
    """
    require_ms("effective_at", effective_at)
    snap = store.snapshot()
    if snap is None:
        raise LookupError("활성 보정이 없다 (UNCALIBRATED)")
    doc = snap.doc()
    payload: dict[str, Any] = {
        "calibration_version": doc["calibration_version"],
        "previous_calibration_version": previous_calibration_version(store, doc["calibration_version"]),
        "calibration_state": str(snap.state),
        "provider_source": doc["provider_source"],
        "reason": str(UpdateReason(reason)),
        "effective_at": effective_at,
        "calibration_ref": calibration_ref,
        "calibration_hash": doc["content_hash"],
    }
    if monitor is not None and monitor.calibration_version == doc["calibration_version"]:
        cameras = monitor.reprojection_errors()
        if cameras:
            payload["cameras"] = cameras
    return payload
