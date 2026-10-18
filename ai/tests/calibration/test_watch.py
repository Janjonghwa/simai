"""보정 감시 묶음·자동 재보정 트리거 시험 (명세 4.4 DRIFTED 자동 재보정 시작, 4.5, 12.2 calibration_update)."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration import CalibrationEvidence, CalibrationState, CalibrationVersionStore
from simai_ai.calibration.bundle import PersonObservation
from simai_ai.calibration.drift import CameraObservation, DriftFinding, DriftReason
from simai_ai.calibration.monitor import MonitorConfig, MonitorEvidence, MonitorStatus, baseline_from_observations
from simai_ai.calibration.recalibration import (
    RecalibrationConfig,
    RecalibrationOutcome,
    RecalibrationRequest,
    RecalibrationTrigger,
    TriggerPhase,
    intrinsics_init,
    realign_rotation,
    run_recalibration,
)
from simai_ai.calibration.watch import (
    CalibrationDriftWatch,
    RecalibrationBlock,
    UpdateReason,
    calibration_update_payload,
    previous_calibration_version,
)
from simai_ai.geometry import distortion
from simai_ai.geometry.rig import RigCamera
from simai_ai.providers.contract import schema_errors
from simai_ai.providers.provenance import build_input_provenance

T0 = 1_760_342_400_000
IDS = ("cam1", "cam2", "cam3", "cam4")
LIVE = build_input_provenance({"source": "LIVE", "camera_ids": list(IDS)})
FAST = MonitorConfig(
    window_ms=5_000, min_window_samples=4, baseline_min_samples=8, baseline_min_span_ms=1_000, sustain_ms=3_000
)
RECAL = RecalibrationConfig(min_collect_ms=2_000, min_observations=4, sample_period_ms=0, retry_after_ms=5_000)


@pytest.fixture
def store(cameras, evidence):
    store = CalibrationVersionStore(site_id="classroom-1", camera_ids=IDS)
    store.propose(provider_source="G1_AUTO", provider_version="t", cameras=cameras, evidence=evidence, at_ms=T0)
    assert store.state is CalibrationState.CALIBRATED
    return store


def _rigs(doc, rotate=None, deg=1.0):
    rigs = {}
    for cam in doc["cameras"]:
        cam = dict(cam)
        if cam["camera_id"] == rotate:
            R = np.array(cam["R"])
            C = -R.T @ np.array(cam["t"])
            R2 = Rotation.from_rotvec([0.0, np.radians(deg), 0.0]).as_matrix() @ R
            cam["R"], cam["t"] = R2.tolist(), (-R2 @ C).tolist()
        rigs[cam["camera_id"]] = RigCamera.from_calibration(cam)
    return rigs


def _people(rigs, t, n=2, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n):
        base = np.array([rng.uniform(2.5, 5.5), rng.uniform(2.0, 4.0), 0.0])
        X = base + np.column_stack([rng.normal(0, 0.15, 17), rng.normal(0, 0.15, 17), np.linspace(0.1, 1.7, 17)])
        views = {}
        for cid, cam in rigs.items():
            Xc = cam.to_camera(X)
            xd = distortion.distort(Xc[:, :2] / Xc[:, 2:3], cam.distortion_model, cam.distortion_coeffs)
            uv = (np.column_stack([xd, np.ones(len(xd))]) @ cam.K.T)[:, :2] + rng.normal(0, 0.8, (17, 2))
            views[cid] = tuple((float(u), float(v), 0.9) for u, v in uv)
        out.append(PersonObservation(f"p{k}", t, views))
    return out


def _angle_deg(R1, R2) -> float:
    """두 회전 사이 각(도). 소수 4자리로 반올림한 보정 R 은 직교에서 조금 벗어나므로 먼저 직교화한다."""

    def ortho(R):
        U, _, Vt = np.linalg.svd(np.asarray(R, dtype=float))
        return U @ Vt

    return float(np.degrees(Rotation.from_matrix(ortho(R1) @ ortho(R2).T).magnitude()))


def _drive(watch, rigs_for, start_ms, end_ms, step_ms=250):
    reports = []
    for i, t in enumerate(range(start_ms, end_ms, step_ms)):
        reports.append(watch.update(t, _people(rigs_for(t), t, seed=i)))
    return reports


def test_uncalibrated_store_reports_no_evidence():
    store = CalibrationVersionStore()
    report = CalibrationDriftWatch(store, LIVE).update(T0, [])
    assert report.state is CalibrationState.UNCALIBRATED and report.monitor is None
    assert report.evidence is MonitorEvidence.NONE and report.calibration_version is None
    assert report.is_upper_bound is False


def test_drift_marks_store_and_requests_recalibration(store):
    doc = store.snapshot().doc()
    requests = []
    watch = CalibrationDriftWatch(
        store,
        LIVE,
        monitor_config=FAST,
        recalibration_config=RECAL,
        start_recalibration=requests.append,
        min_update_interval_ms=0,
    )
    nominal, turned = _rigs(doc), _rigs(doc, rotate="cam2")
    t_rot = T0 + 5_000
    reports = _drive(watch, lambda t: turned if t >= t_rot else nominal, T0 + 1, T0 + 20_000)
    marked = [r for r in reports if r.drift_marked]
    assert len(marked) == 1 and marked[0].drift_reason is UpdateReason.DRIFT_DETECTED
    assert [f.camera_id for f in marked[0].monitor.findings] == ["cam2"]
    assert store.state is CalibrationState.DRIFTED and len(store.versions) == 1  # 새 버전 없음
    requested = [r for r in reports if r.recalibration_request is not None]
    assert len(requested) == 1 and requests == [requested[0].recalibration_request]
    req = requests[0]
    assert req.calibration_version == "cal-1" and len(req.observations) >= RECAL.min_observations
    assert all(o.t >= marked[0].t_ms for o in req.observations)
    # 틀어진 카메라만 회전 초기값을 먼저 맞춘다(카메라 중심은 그대로)
    assert req.realigned_camera_ids == ("cam2",)
    assert req.initial_extrinsics["cam1"] == (doc["cameras"][0]["R"], doc["cameras"][0]["t"])
    R2, t2 = (np.array(v) for v in req.initial_extrinsics["cam2"])
    assert _angle_deg(R2, turned["cam2"].R) < 0.05
    np.testing.assert_allclose(-R2.T @ t2, turned["cam2"].center, atol=2e-3)  # 반올림한 R 의 비직교 몫
    assert requested[0].t_ms - marked[0].t_ms >= RECAL.min_collect_ms
    assert reports[-1].recalibration_phase is TriggerPhase.REQUESTED
    # calibration_update payload (I1 계약)
    payload = calibration_update_payload(
        store,
        reason=UpdateReason.DRIFT_DETECTED,
        effective_at=marked[0].t_ms,
        calibration_ref="s3://simai-site/calibration/cal-1.json",
        monitor=marked[0].monitor,
    )
    cams = {c["camera_id"]: c["reprojection_error_px"] for c in payload["cameras"]}
    assert cams["cam2"]["median"] > 10 and payload["calibration_state"] == "DRIFTED"
    assert payload["calibration_hash"] == doc["content_hash"]
    message = {
        "schema_version": "2.0.0",
        "msg_type": "calibration_update",
        "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000117",
        "sent_at": marked[0].t_ms,
        "source_id": "tier0-01",
        "payload": payload,
    }
    assert schema_errors(message, "calibration_update") == []


def test_new_version_resets_monitor(store, cameras, evidence):
    watch = CalibrationDriftWatch(store, LIVE, monitor_config=FAST, min_update_interval_ms=0)
    rigs = _rigs(store.snapshot().doc())
    reports = _drive(watch, lambda t: rigs, T0 + 1, T0 + 4_000)
    assert reports[-1].monitor.camera("cam1").status is MonitorStatus.MONITORING
    store.report_drift([DriftFinding("cam1", DriftReason.REPROJECTION_INCREASED, "시험")], T0 + 4_000, "cal-1")
    store.propose(
        provider_source="G1_AUTO",
        provider_version="t",
        cameras=cameras,
        evidence=CalibrationEvidence(),
        at_ms=T0 + 4_100,
    )
    assert store.snapshot().calibration_version == "cal-2"
    report = watch.update(T0 + 4_200, _people(rigs, T0 + 4_200))
    assert report.calibration_version == "cal-2" and report.monitor.calibration_version == "cal-2"
    assert report.monitor.camera("cam1").baseline_median_px is None
    assert previous_calibration_version(store, "cal-2") == "cal-1"
    assert previous_calibration_version(store, "cal-1") is None


def test_never_calibrated_source_is_not_marked(cameras):
    store = CalibrationVersionStore()
    store.propose(
        provider_source="SIM", provider_version="t", cameras=cameras, evidence=CalibrationEvidence(), at_ms=T0
    )
    watch = CalibrationDriftWatch(store, LIVE, monitor_config=FAST)
    report = watch.update(T0 + 10, [], [CameraObservation("cam1", image_size=(640, 480))])
    assert report.monitor.findings and not report.drift_marked
    assert store.state is CalibrationState.PROVISIONAL


def test_update_interval_limits_measurement_but_not_stream_changes(store):
    watch = CalibrationDriftWatch(store, LIVE, monitor_config=FAST, min_update_interval_ms=1_000)
    rigs = _rigs(store.snapshot().doc())
    counts = []
    for i, t in enumerate(range(T0 + 1, T0 + 3_000, 100)):
        report = watch.update(t, _people(rigs, t, seed=i))
        counts.append(report.monitor.camera("cam1").window_samples)
    assert max(counts) == 3 * 2  # 1초마다 사람 2명
    t = T0 + 3_050
    report = watch.update(t, [], [CameraObservation("cam4", image_size=(1280, 720))])
    assert report.drift_marked and report.drift_reason is UpdateReason.STREAM_CHANGED
    # 해상도가 바뀐 카메라가 있으면 이전 내부 파라미터로 자동 재보정하지 않는다
    for i, t in enumerate(range(T0 + 3_100, T0 + 60_000, 1_000)):
        report = watch.update(t, _people(rigs, t, seed=i))
    assert report.stream_changed_camera_ids == ("cam4",)
    assert report.recalibration_phase is TriggerPhase.CANCELLED and report.recalibration_request is None
    assert report.recalibration_blocked is RecalibrationBlock.STREAM_CHANGED


def test_trigger_collects_after_drift_only(store):
    doc = store.snapshot().doc()
    config = RecalibrationConfig(min_collect_ms=1_000, min_observations=2, sample_period_ms=500, retry_after_ms=2_000)
    trig = RecalibrationTrigger(doc, LIVE, config)
    rigs = _rigs(doc)
    trig.add(_people(rigs, T0))  # 시작 전에는 모으지 않는다
    assert trig.phase is TriggerPhase.IDLE and trig.collected == 0
    trig.start(T0 + 1_000, ["cam2"])
    trig.add(_people(rigs, T0 + 900))  # 표시 전 관측
    trig.add(_people(rigs, T0 + 1_000, n=1))
    trig.add(_people(rigs, T0 + 1_200, n=1))  # 같은 사람 500ms 안
    assert trig.collected == 1
    assert trig.poll(T0 + 1_500) is None
    trig.add(_people(rigs, T0 + 1_600, n=1))
    assert trig.poll(T0 + 1_900) is None  # 최소 수집 시간 전
    req = trig.poll(T0 + 2_000)
    assert req is not None and trig.phase is TriggerPhase.REQUESTED and len(req.observations) == 2
    assert trig.poll(T0 + 2_100) is None  # 한 번만
    trig.finished(RecalibrationOutcome(req, None, None, "번들 조정 FAILED"), T0 + 2_200)
    assert trig.phase is TriggerPhase.WAITING_RETRY and trig.collected == 0
    trig.add(_people(rigs, T0 + 3_000, n=1))
    assert trig.collected == 0
    assert trig.poll(T0 + 4_200) is None and trig.phase is TriggerPhase.COLLECTING


def test_intrinsics_init_from_active_calibration(store):
    doc = store.snapshot().doc()
    init = intrinsics_init(doc)
    cam = init.camera("cam1")
    assert cam.camera["K"] == doc["cameras"][0]["K"] and cam.principal_point_fixed
    assert cam.focal_prior_std_px == pytest.approx(0.01 * 1000.0)


def test_realign_rotation(store):
    doc = store.snapshot().doc()
    turned = _rigs(doc, rotate="cam3", deg=1.5)
    obs = [p for k in range(10) for p in _people(turned, T0 + k, seed=k)]
    R, _ = realign_rotation(doc, "cam3", obs)
    assert _angle_deg(R, turned["cam3"].R) < 0.05
    # 다른 틀어진 카메라는 기준점에서 뺀다. 기준 카메라가 모자라거나 대응이 적으면 맞추지 않는다
    assert realign_rotation(doc, "cam3", obs, exclude={"cam1", "cam2"}) is None
    assert realign_rotation(doc, "cam3", obs[:1], min_correspondences=100) is None
    assert realign_rotation(doc, "cam9", obs) is None


def test_run_recalibration_skips_stale_request(store):
    doc = store.snapshot().doc()
    trig = RecalibrationTrigger(doc, LIVE, RECAL)
    trig.start(T0 + 1, ["cam2"])
    trig.add(_people(_rigs(doc), T0 + 10, n=4))
    req = trig.poll(T0 + 5_000)
    assert req is not None
    outcome = run_recalibration(req, store, at_ms=T0 + 5_000, is_valid=lambda r: True)  # 저장소는 아직 CALIBRATED
    assert not outcome.adopted and outcome.result is None and "DRIFTED" in outcome.skipped_reason
    with pytest.raises(TypeError):
        run_recalibration(req, store, at_ms=T0 + 5_000)  # 요청 무효 확인 콜백은 반드시 준다
    with pytest.raises(TypeError):
        run_recalibration(req, store, at_ms=T0 + 5_000, is_valid=None)


@pytest.mark.parametrize("kwargs", [{"min_observations": 0}, {"min_collect_ms": -1}, {"focal_prior_rel": 0}])
def test_recalibration_config_validation(kwargs):
    with pytest.raises(ValueError):
        RecalibrationConfig(**kwargs)


def test_invalid_provenance_rejected(store):
    with pytest.raises(ValueError):
        CalibrationDriftWatch(store, {"frames": {"source": "NOPE"}})


def _watch(store, **kwargs):
    kwargs.setdefault("monitor_config", FAST)
    kwargs.setdefault("recalibration_config", RECAL)
    kwargs.setdefault("min_update_interval_ms", 0)
    return CalibrationDriftWatch(store, LIVE, **kwargs)


def test_resolution_change_after_drift_cancels_recalibration(store):
    """F1: 재투영으로 DRIFTED 가 된 뒤 같은 카메라 해상도가 바뀌면 알리고, 모으던 관측·낸 요청을 무효로 한다."""
    doc = store.snapshot().doc()
    watch = _watch(store)
    nominal, turned = _rigs(doc), _rigs(doc, rotate="cam2")
    reports = _drive(watch, lambda t: turned if t >= T0 + 5_000 else nominal, T0 + 1, T0 + 20_000)
    request = next(r.recalibration_request for r in reports if r.recalibration_request is not None)
    assert watch.request_still_valid(request)
    t = T0 + 20_001
    report = watch.update(t, [], [CameraObservation("cam2", image_size=(1280, 720))])
    assert not report.drift_marked  # 이미 DRIFTED
    assert report.drift_reason is UpdateReason.STREAM_CHANGED
    assert report.new_stream_changed_camera_ids == ("cam2",) and report.stream_changed_camera_ids == ("cam2",)
    assert report.recalibration_blocked is RecalibrationBlock.STREAM_CHANGED
    assert not watch.request_still_valid(request)
    outcome = run_recalibration(request, store, at_ms=t, is_valid=watch.request_still_valid)
    assert not outcome.adopted
    later = _drive(watch, lambda t: turned, T0 + 20_250, T0 + 60_000)
    assert all(r.recalibration_request is None for r in later)
    assert later[-1].recalibration_phase is TriggerPhase.CANCELLED


def test_start_already_rotated_is_detected(store):
    """F3: 감시를 시작하기 전에 이미 1° 돌아간 카메라도 절대 상한으로 잡는다(학습 기준선을 정상으로 삼지 않는다)."""
    doc = store.snapshot().doc()
    watch = _watch(store)
    turned = _rigs(doc, rotate="cam2")
    reports = _drive(watch, lambda t: turned, T0 + 1, T0 + 20_000)
    marked = [r for r in reports if r.drift_marked]
    assert marked and [f.camera_id for f in marked[0].monitor.findings] == ["cam2"]
    assert not any(r.monitor.camera("cam2").status is MonitorStatus.MONITORING for r in reports)


def test_learned_baseline_stays_unverified_and_calibration_input_baseline_is_verified(store):
    """R2: 운영 중에 배운 기준선은 저장·재시작해도 UNVERIFIED, 보정 입력 관측으로 잰 기준선만 FULL."""
    doc = store.snapshot().doc()
    rigs = _rigs(doc)
    late = _watch(store)
    last = _drive(late, lambda t: rigs, T0 + 1, T0 + 6_000)[-1]
    assert last.evidence is MonitorEvidence.UNVERIFIED
    saved = late.baselines()
    assert set(saved["cal-1"].values_px) == set(IDS) and saved["cal-1"].verified_camera_ids == ()
    again = _watch(store, baselines=saved)
    reports = _drive(again, lambda t: rigs, T0 + 1_000_001, T0 + 1_030_000)
    assert all(r.evidence is not MonitorEvidence.FULL for r in reports)
    assert again.baselines()["cal-1"].verified_camera_ids == ()
    calibration_walk = [p.views for k in range(10) for p in _people(rigs, T0 + k, n=3, seed=k)]
    verified = baseline_from_observations(doc, calibration_walk, FAST)
    good = _watch(store, baselines={"cal-1": verified})
    assert _drive(good, lambda t: rigs, T0 + 1, T0 + 3_000)[-1].evidence is MonitorEvidence.FULL
    assert good.baselines()["cal-1"].verified_camera_ids == IDS


def test_collection_buffer_saturation_recovers(store):
    """R3: 틀어진 카메라가 가려진 채 버퍼가 차도, 다시 보이면 오래된 관측을 내보내고 요청한다."""
    doc = store.snapshot().doc()
    config = RecalibrationConfig(
        min_collect_ms=1_000, min_observations=4, max_observations=20, sample_period_ms=0, realign_retry_ms=2_000
    )
    trig = RecalibrationTrigger(doc, LIVE, config)
    trig.start(T0 + 1, ["cam2"])
    turned = _rigs(doc, rotate="cam2")
    t = T0 + 1
    for k in range(40):  # cam2 가 안 보이는 관측으로 버퍼를 채운다
        t = T0 + 1 + k * 300
        obs = _people(turned, t, seed=k)
        trig.add([PersonObservation(o.person_id, o.t, {c: v for c, v in o.views.items() if c != "cam2"}) for o in obs])
        assert trig.poll(t) is None
    assert trig.collected == config.max_observations and trig.realign_failed
    request = None
    for k in range(40, 120):
        t = T0 + 1 + k * 300
        trig.add(_people(turned, t, seed=k))
        request = trig.poll(t)
        if request is not None:
            break
    assert request is not None and request.realigned_camera_ids == ("cam2",)
    assert len(request.observations) == config.max_observations
    R2 = np.array(request.initial_extrinsics["cam2"][0])
    assert _angle_deg(R2, turned["cam2"].R) < 0.05


def test_three_cameras_ambiguous_blocks_recalibration(cameras, evidence):
    """F4: 3대만 보이면 원인 카메라를 정하지 않고, 보정은 DRIFTED 로 하되 자동 재보정은 시작하지 않는다."""
    store = CalibrationVersionStore(site_id="classroom-1")
    store.propose(provider_source="G1_AUTO", provider_version="t", cameras=cameras[:3], evidence=evidence, at_ms=T0)
    doc = store.snapshot().doc()
    watch = _watch(store)
    nominal, turned = _rigs(doc), _rigs(doc, rotate="cam2")
    reports = _drive(watch, lambda t: turned if t >= T0 + 5_000 else nominal, T0 + 1, T0 + 40_000)
    assert store.state is CalibrationState.DRIFTED
    assert not any(c.status is MonitorStatus.DRIFTED for r in reports for c in r.monitor.cameras)
    assert reports[-1].recalibration_blocked is RecalibrationBlock.ATTRIBUTION_UNCERTAIN
    assert all(r.recalibration_request is None for r in reports)


def test_realign_failure_holds_request(store):
    """틀어진 카메라 회전 초기값을 맞출 대응이 없으면 요청하지 않는다(World 축 게이지를 보장하지 못한다)."""
    doc = store.snapshot().doc()
    trig = RecalibrationTrigger(doc, LIVE, RECAL)
    trig.start(T0 + 1, ["cam2"])
    rigs = _rigs(doc)
    for k in range(6):  # cam2 가 안 보이는 관측만
        obs = _people(rigs, T0 + 10 + k)
        trig.add([PersonObservation(o.person_id, o.t, {c: v for c, v in o.views.items() if c != "cam2"}) for o in obs])
    assert trig.poll(T0 + 5_000) is None and trig.realign_failed
    assert trig.phase is TriggerPhase.COLLECTING and trig.issued is None


def test_request_validity_after_version_switch(store, cameras):
    doc = store.snapshot().doc()
    watch = _watch(store)
    nominal, turned = _rigs(doc), _rigs(doc, rotate="cam2")
    reports = _drive(watch, lambda t: turned if t >= T0 + 5_000 else nominal, T0 + 1, T0 + 20_000)
    request = next(r.recalibration_request for r in reports if r.recalibration_request is not None)
    assert isinstance(request, RecalibrationRequest)
    store.propose(
        provider_source="G1_AUTO",
        provider_version="t",
        cameras=cameras,
        evidence=CalibrationEvidence(),
        at_ms=T0 + 20_001,
    )
    assert not watch.request_still_valid(request)


def test_people_leaving_during_suspect_does_not_complete_drift(store):
    """F2: 기본 설정. 16초 동안만 높은 오차를 본 뒤 사람이 사라지면 DRIFTED 로 바꾸지 않고 감시 근거 없음이 된다."""
    doc = store.snapshot().doc()
    watch = CalibrationDriftWatch(store, LIVE)
    nominal, turned = _rigs(doc), _rigs(doc, rotate="cam2")
    reports = []
    for i, s in enumerate(range(120)):
        t = T0 + 1 + s * 1_000
        people = [] if s > 45 else _people(turned if s >= 30 else nominal, t, seed=i)
        reports.append(watch.update(t, people))
    assert not any(r.drift_marked for r in reports)
    assert store.state is CalibrationState.CALIBRATED
    assert any(r.monitor.camera("cam2").status is MonitorStatus.SUSPECT for r in reports)
    assert reports[-1].evidence is MonitorEvidence.NONE


def test_return_after_observation_gap_does_not_reuse_sustain(store):
    """N1: 기본 Watch. 초과 18초에서 사람이 31초 사라졌다 돌아오면 복귀 뒤 새로 20초를 쌓아야 DRIFTED 다."""
    doc = store.snapshot().doc()
    watch = CalibrationDriftWatch(
        store,
        LIVE,
        baselines={
            "cal-1": baseline_from_observations(
                doc, [p.views for k in range(10) for p in _people(_rigs(doc), T0 + k, n=3, seed=k)]
            )
        },
    )
    nominal, turned = _rigs(doc), _rigs(doc, rotate="cam2")
    last_person, back, marks = None, None, []
    for sec in range(200):
        t = T0 + 1 + sec * 1_000
        if last_person is None:
            report = watch.update(t, _people(turned if sec >= 30 else nominal, t, seed=sec))
            if report.monitor.camera("cam2").exceed_ms >= 18_000:
                last_person = sec
                back = sec + 31
        elif sec < back:
            report = watch.update(t, [])
        else:
            report = watch.update(t, _people(turned, t, seed=sec))
        if report.drift_marked:
            marks.append(sec)
    assert last_person is not None and marks
    assert marks[0] - back >= 20
