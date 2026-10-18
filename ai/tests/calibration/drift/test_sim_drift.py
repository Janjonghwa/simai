"""틀어짐 감시 SIM 시험 (명세 4.4 DRIFTED, 6.5, 15.1 틀어짐 재현, 부록 D-G1-03 "사람 존재 시 2분 이내").

SIM 입력(input_provenance.is_upper_bound)이므로 여기서 잰 감지 시간·오경보 수는 입력이 완벽할 때의 상한이다.
D-G1-03 합격은 개발 교실 현장 측정(FM, 레이저 포인터)으로 판정한다.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.drift import CameraObservation, DriftReason
from simai_ai.calibration.monitor import MonitorEvidence, MonitorStatus
from simai_ai.calibration.recalibration import TriggerPhase
from simai_ai.calibration.watch import UpdateReason, calibration_update_payload
from simai_ai.providers.contract import schema_errors
from simai_ai.tracking.system_health import CalibrationState

TWO_MINUTES_MS = 120_000


def _angle_deg(R1, R2) -> float:
    """두 회전 사이 각(도). 소수 4자리로 반올림한 보정 R 은 직교에서 조금 벗어나므로 먼저 직교화한다."""

    def ortho(R):
        U, _, Vt = np.linalg.svd(np.asarray(R, dtype=float))
        return U @ Vt

    return float(np.degrees(Rotation.from_matrix(ortho(R1) @ ortho(R2).T).magnitude()))


def test_inputs_are_upper_bound(drift_sim, nominal_run):
    assert drift_sim.input_provenance["is_upper_bound"] is True
    assert all(r.is_upper_bound for r in nominal_run.reports)


def test_one_degree_rotation_detected_within_two_minutes(drift_sim, rotation_run, dsim):
    t_rot = drift_sim.start + dsim.ROTATION_AT_S * 1000
    marked = rotation_run.first_marked()
    assert marked is not None, "1° 회전을 감지하지 못했다"
    latency = marked.t_ms - t_rot
    assert 0 <= latency <= TWO_MINUTES_MS, f"감지 {latency / 1000:.1f}s (SIM 상한)"
    assert marked.drift_reason is UpdateReason.DRIFT_DETECTED
    findings = marked.monitor.findings
    assert [f.camera_id for f in findings] == ["cam2"]
    assert findings[0].reason is DriftReason.REPROJECTION_INCREASED
    # 회전 전에는 표시하지 않았다
    assert not any(r.drift_marked for r in rotation_run.reports if r.t_ms < t_rot)
    # 저장소: 새 버전 없이 DRIFTED 전이만 추가
    drift = [tr for tr in rotation_run.store.transitions if tr.to_state is CalibrationState.DRIFTED]
    assert len(drift) == 1 and drift[0].from_version == drift[0].to_version == marked.calibration_version
    assert drift[0].at_ms == marked.t_ms


def test_drift_starts_recalibration_and_new_version_is_provisional(drift_sim, rotation_run):
    marked = rotation_run.first_marked()
    assert rotation_run.outcomes, "재보정 요청이 나오지 않았다"
    t_req, outcome = rotation_run.outcomes[0]
    assert t_req > marked.t_ms
    request = outcome.request
    assert request.calibration_version == marked.calibration_version
    assert all(obs.t >= marked.t_ms for obs in request.observations), "DRIFTED 표시 전 관측이 섞였다"
    assert outcome.adopted, outcome.skipped_reason
    # 판정은 저장소 criteria: SIM 입력 결과는 출처 SIM 이고 CALIBRATED 가 될 수 없다
    doc = outcome.proposal.doc
    assert doc["provider_source"] == "SIM" and doc["state"] == "PROVISIONAL"
    store = rotation_run.store
    assert store.state is CalibrationState.PROVISIONAL
    assert [v["calibration_version"] for v in store.versions] == ["cal-1", "cal-2"]  # 추가만
    assert store.versions[0]["state"] == "CALIBRATED"  # 저장된 이전 버전 문서는 DRIFTED 표시로 바뀌지 않는다
    # 재보정 결과 cam2 는 돌아간 자세를 따르고, 회전 초기값을 먼저 맞춰 World 축이 돌지 않는다(SIM 상한)
    assert outcome.request.realigned_camera_ids == ("cam2",)
    cams = {c["camera_id"]: c for c in doc["cameras"]}
    rotated_truth = drift_sim.truth_cameras["cam2r"]["R"]
    assert _angle_deg(cams["cam2"]["R"], rotated_truth) < 0.1
    for cid in ("cam1", "cam3", "cam4"):
        assert _angle_deg(cams[cid]["R"], drift_sim.truth_cameras[cid]["R"]) < 0.1
    # 새 버전은 새 기준선으로 감시하고, 돌아간 자세 그대로면 다시 틀어짐으로 보지 않는다
    later = [r for r in rotation_run.reports if r.calibration_version == "cal-2"]
    assert later and not any(r.drift_marked for r in later)
    assert later[-1].monitor.camera("cam2").status is MonitorStatus.MONITORING
    assert later[-1].recalibration_phase is TriggerPhase.IDLE
    # 새 버전의 검증된 기준선은 재보정 관측으로 잰다(시간·재시작만으로 검증되지 않는다)
    assert outcome.baseline is not None and outcome.baseline.calibration_version == "cal-2"
    assert set(outcome.baseline.verified_camera_ids) == {"cam1", "cam2", "cam3", "cam4"}
    assert later[-1].evidence is MonitorEvidence.FULL


def test_no_rotation_no_false_alarm_over_ten_minutes(nominal_run, dsim):
    assert len(nominal_run.reports) * dsim.SAMPLE_PERIOD_MS >= dsim.DURATION_MS - 1000
    assert not any(r.drift_marked for r in nominal_run.reports)
    assert not any(r.monitor.findings for r in nominal_run.reports)
    assert nominal_run.store.state is CalibrationState.CALIBRATED
    assert len(nominal_run.store.transitions) == 1
    statuses = [c.status for r in nominal_run.reports for c in r.monitor.cameras]
    assert MonitorStatus.SUSPECT not in statuses
    # 기준선을 잡은 뒤에는 네 대 모두 감시 근거가 있다
    tail = nominal_run.reports[len(nominal_run.reports) // 10 :]
    assert all(r.evidence is MonitorEvidence.FULL for r in tail)


def test_resolution_change_is_immediate(drift_sim, dsim, contract_errors):
    t_change = drift_sim.start + 60_000

    def streams(t):
        return [CameraObservation("cam3", image_size=(1280, 720))] if t >= t_change else []

    run = dsim.run_watch(drift_sim, drift_sim.frames, t_change + 5_000, streams)
    marked = run.first_marked()
    assert marked is not None and marked.t_ms == min(t for t in drift_sim.times if t >= t_change)
    assert marked.drift_reason is UpdateReason.STREAM_CHANGED
    assert [(f.camera_id, f.reason) for f in marked.monitor.findings] == [("cam3", DriftReason.RESOLUTION_CHANGED)]
    assert run.store.state is CalibrationState.DRIFTED
    assert marked.monitor.camera("cam3").status is MonitorStatus.STREAM_CHANGED
    payload = calibration_update_payload(
        run.store,
        reason=marked.drift_reason,
        effective_at=marked.t_ms,
        calibration_ref="s3://simai-site/calibration/cal-1.json",
        monitor=marked.monitor,
    )
    assert payload["calibration_state"] == "DRIFTED" and payload["reason"] == "STREAM_CHANGED"
    assert payload["previous_calibration_version"] is None
    assert {c["camera_id"] for c in payload["cameras"]} <= {"cam1", "cam2", "cam4"}
    assert contract_errors(payload) == []


def test_no_people_is_no_evidence_and_keeps_state(drift_sim, dsim):
    start = drift_sim.start
    empty_from, rotate_at, back_at = start + 60_000, start + 120_000, start + 300_000

    def frames(t):
        if empty_from <= t < back_at:
            return []
        return drift_sim.frames(t, rotated=t >= rotate_at)

    run = dsim.run_watch(drift_sim, frames, back_at + 150_000)
    empty = [r for r in run.reports if empty_from + 31_000 <= r.t_ms < back_at]
    assert empty and all(r.evidence is MonitorEvidence.NONE for r in empty)
    assert all(set(r.monitor.unmonitored_camera_ids) == {"cam1", "cam2", "cam3", "cam4"} for r in empty)
    assert all(c.status is MonitorStatus.NO_EVIDENCE for r in empty for c in r.monitor.cameras)
    # 근거 없이 바꾸지 않는다: CALIBRATED 그대로, 전이 기록도 그대로
    assert all(r.state is CalibrationState.CALIBRATED and not r.drift_marked for r in empty)
    assert not any(r.drift_marked for r in run.reports if r.t_ms < back_at)
    # 사람이 돌아오면 2분 안에 감지한다
    marked = run.first_marked()
    assert marked is not None and marked.t_ms - back_at <= TWO_MINUTES_MS


@pytest.fixture(scope="session")
def contract_errors():
    def _errors(payload):
        message = {
            "schema_version": "2.0.0",
            "msg_type": "calibration_update",
            "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000117",
            "sent_at": payload["effective_at"],
            "source_id": "tier0-01",
            "payload": payload,
        }
        return schema_errors(message, "calibration_update")

    return _errors
