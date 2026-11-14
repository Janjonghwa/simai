"""운영 중 재투영 오차 감시 시험 (명세 4.4 DRIFTED, 4.5, 6.5). 판정 규칙은 ingest 로, 표본은 합성 기하로 본다."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.drift import CameraObservation, DriftReason
from simai_ai.calibration.monitor import (
    AMBIGUOUS_PREFIX,
    BaselineRecord,
    MonitorConfig,
    MonitorEvidence,
    MonitorStatus,
    ReprojectionMonitor,
    baseline_from_observations,
    leave_one_out_samples,
)
from simai_ai.geometry import distortion
from simai_ai.geometry.rig import RigCamera

T0 = 1_760_342_400_000
IDS = ("cam1", "cam2", "cam3", "cam4")
CFG = MonitorConfig(
    window_ms=10_000,
    min_window_samples=5,
    baseline_min_samples=10,
    baseline_min_span_ms=2_000,
    sustain_ms=8_000,
    max_sample_age_ms=2_000,
)


@pytest.fixture
def calibration(camera_factory):
    return {"calibration_version": "cal-1", "cameras": [camera_factory(cid).to_contract() for cid in IDS]}


def _feed(monitor, start_s, end_s, values, step_ms=500, attributable=True):
    """start_s~end_s 동안 step 마다 네 카메라에 표본 하나씩. values: camera_id → 값(없으면 1.0)."""
    reports = []
    for t in range(T0 + int(start_s * 1000), T0 + int(end_s * 1000), step_ms):
        reports.append(monitor.ingest(t, [(cid, values.get(cid, 1.0), attributable) for cid in IDS]))
    return reports


def _idle(monitor, start_s, end_s, step_ms=500):
    return [monitor.ingest(t, []) for t in range(T0 + int(start_s * 1000), T0 + int(end_s * 1000), step_ms)]


def test_baseline_then_monitoring(calibration):
    mon = ReprojectionMonitor(calibration, CFG, baseline=BaselineRecord("cal-1", {c: 1.0 for c in IDS}, IDS))
    reports = _feed(mon, 0, 10, {})
    assert reports[0].camera("cam1").status is MonitorStatus.NO_EVIDENCE
    assert reports[0].evidence is MonitorEvidence.NONE
    last = reports[-1].camera("cam1")
    assert last.status is MonitorStatus.MONITORING and last.baseline_verified
    assert last.baseline_median_px == pytest.approx(1.0)
    assert last.threshold_px == pytest.approx(4.0)  # min(max(1.0 × 2, 1.0 + 3), 15)
    assert reports[-1].evidence is MonitorEvidence.FULL


def test_learned_baseline_without_verification_is_unverified(calibration):
    """보정 직후가 아닌 감시가 배운 기준선은 미확정이다(감시 시작 전 틀어짐을 정상으로 삼을 수 있다)."""
    mon = ReprojectionMonitor(calibration, CFG)
    reports = _feed(mon, 0, 10, {})
    assert any(r.camera("cam1").status is MonitorStatus.BASELINE_LEARNING for r in reports)
    last = reports[-1]
    assert last.camera("cam1").status is MonitorStatus.MONITORING
    assert not last.camera("cam1").baseline_verified
    assert last.evidence is MonitorEvidence.UNVERIFIED
    # R2: 저장했다 다시 줘도, 시간이 지나도 검증된 것으로 바뀌지 않는다
    record = mon.baseline_record()
    assert record.verified_camera_ids == () and set(record.values_px) == set(IDS)
    again = ReprojectionMonitor(calibration, CFG, baseline=record)
    later = [again.ingest(T0 + 1_000_000 + k * 500, [(c, 1.0) for c in IDS]) for k in range(400)]
    assert all(r.evidence is not MonitorEvidence.FULL for r in later)
    assert later[-1].evidence is MonitorEvidence.UNVERIFIED
    assert again.baseline_record().verified_camera_ids == ()


def test_given_baseline_is_verified_and_used(calibration):
    mon = ReprojectionMonitor(calibration, CFG, baseline=BaselineRecord("cal-1", {c: 1.0 for c in IDS}, IDS))
    report = _feed(mon, 0, 3, {"cam2": 6.0})[-1]
    assert report.camera("cam2").status is MonitorStatus.SUSPECT  # 학습 없이 바로 기준선 1px 로 본다
    assert report.camera("cam1").baseline_verified
    record = mon.baseline_record()
    assert record.values_px == {c: 1.0 for c in IDS} and record.verified_camera_ids == IDS
    # 다른 버전의 기록은 쓰지 않는다
    other = ReprojectionMonitor(calibration, CFG, baseline=BaselineRecord("cal-9", {c: 1.0 for c in IDS}, IDS))
    assert other.baseline_record().values_px == {}


def test_already_drifted_at_start_is_caught_by_absolute_ceiling(calibration):
    """감시 시작 전부터 크게 틀어진 카메라: 학습 기준선이 절대 상한을 넘어 쓰이지 않고 SUSPECT → DRIFTED."""
    mon = ReprojectionMonitor(calibration, CFG)
    reports = _feed(mon, 0, 30, {"cam2": 20.0})  # 절대 상한(15px)을 넘는 값
    marked = [r for r in reports if r.findings]
    assert [f.camera_id for f in marked[0].findings] == ["cam2"]
    assert any(r.camera("cam2").status is MonitorStatus.SUSPECT for r in reports)
    cam2 = reports[-1].camera("cam2")
    assert cam2.status is MonitorStatus.DRIFTED and not cam2.baseline_verified
    assert reports[-1].camera("cam1").status is MonitorStatus.MONITORING


def test_sustained_increase_is_drift_once(calibration):
    mon = ReprojectionMonitor(calibration, CFG, baseline=BaselineRecord("cal-1", {c: 1.0 for c in IDS}, IDS))
    _feed(mon, 0, 10, {})
    reports = _feed(mon, 10, 40, {"cam2": 12.0})
    marked = [r for r in reports if r.findings]
    assert len(marked) == 1
    finding = marked[0].findings[0]
    assert finding.camera_id == "cam2" and finding.reason is DriftReason.REPROJECTION_INCREASED
    suspect_from = next(r.t_ms for r in reports if r.camera("cam2").status is MonitorStatus.SUSPECT)
    assert marked[0].t_ms - suspect_from == CFG.sustain_ms
    assert reports[-1].camera("cam2").status is MonitorStatus.DRIFTED
    assert all(reports[-1].camera(c).status is MonitorStatus.MONITORING for c in ("cam1", "cam3", "cam4"))
    # 판정을 끝낸 카메라는 감시 근거 계산에서 뺀다
    assert reports[-1].evidence is MonitorEvidence.FULL


def test_transient_spike_is_not_drift(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    reports = _feed(mon, 10, 16, {"cam2": 12.0})  # 창 중앙값이 약 6초만 넘는다(sustain 8초 미만)
    reports += _feed(mon, 16, 60, {})
    assert any(r.camera("cam2").status is MonitorStatus.SUSPECT for r in reports)
    assert not any(r.findings for r in reports)
    assert reports[-1].camera("cam2").status is MonitorStatus.MONITORING
    assert reports[-1].camera("cam2").exceed_ms == 0


def test_small_increase_within_margin(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    reports = _feed(mon, 10, 60, {"cam2": 3.5})  # 기준선 1px 의 3.5배지만 +3px 여유 안
    assert not any(r.findings for r in reports)


def test_people_leaving_stops_sustain(calibration):
    """F2: 초과 중 사람이 사라지면 창에 표본이 남아 있어도 지속 시간이 쌓이지 않는다."""
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    reports = _feed(mon, 10, 18, {"cam2": 12.0})  # 창 중앙값이 15초쯤 넘기 시작해 3초쯤 쌓인다
    exceed = reports[-1].camera("cam2").exceed_ms
    assert 0 < exceed < CFG.sustain_ms
    later = _idle(mon, 18, 60)
    assert not any(r.findings for r in later)
    assert all(r.camera("cam2").exceed_ms <= exceed + CFG.max_sample_age_ms for r in later)
    assert later[-1].camera("cam2").status is MonitorStatus.NO_EVIDENCE
    assert later[-1].camera("cam2").exceed_ms == 0  # 근거 끊김 뒤에는 지운다
    assert later[-1].evidence is MonitorEvidence.NONE


def test_stale_window_is_no_evidence(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    report = mon.ingest(T0 + 10_000 + CFG.max_sample_age_ms + 500, [])  # 창에는 표본이 남아 있다
    assert report.camera("cam1").window_samples >= CFG.min_window_samples
    assert report.camera("cam1").status is MonitorStatus.NO_EVIDENCE


def test_long_call_gap_resets_before_counting(calibration):
    """F2: 호출이 오래 끊긴 뒤 이상 표본을 한꺼번에 넣어도 끊기기 전 지속 시간을 이어 붙이지 않는다."""
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    before = _feed(mon, 10, 18, {"cam2": 12.0})[-1].camera("cam2").exceed_ms
    assert before > 0
    t = T0 + 300_000
    report = mon.ingest(t, [("cam2", 12.0)] * 20)
    assert report.camera("cam2").exceed_ms == 0 and not report.findings
    assert report.camera("cam2").status is MonitorStatus.SUSPECT


def test_baseline_needs_span(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    report = mon.ingest(T0, [(cid, 1.0) for cid in IDS for _ in range(20)])  # 한 시각에 표본만 많다
    assert report.camera("cam1").status is MonitorStatus.BASELINE_LEARNING
    assert report.camera("cam1").baseline_median_px is None


def test_invalid_samples_ignored(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    report = mon.ingest(T0, [("cam1", float("nan")), ("cam1", -1.0), ("cam9", 1.0)])
    assert report.camera("cam1").window_samples == 0
    with pytest.raises(KeyError):
        report.camera("cam9")


def test_time_must_not_go_back(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    mon.ingest(T0 + 1000, [])
    with pytest.raises(ValueError):
        mon.ingest(T0, [])


def test_resolution_change_is_immediate_and_excluded(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    report = mon.observe(T0 + 10_000, [], [CameraObservation("cam3", image_size=(1280, 720))])
    assert [(f.camera_id, f.reason) for f in report.findings] == [("cam3", DriftReason.RESOLUTION_CHANGED)]
    cam3 = report.camera("cam3")
    assert cam3.status is MonitorStatus.STREAM_CHANGED and cam3.window_samples == 0
    assert "cam3" not in {c["camera_id"] for c in report.reprojection_errors()}
    again = mon.observe(T0 + 10_500, [], [CameraObservation("cam3", image_size=(1280, 720))])
    assert again.findings == ()  # 한 번만 낸다
    same = mon.observe(T0 + 11_000, [], [CameraObservation("cam1", image_size=(1920, 1080))])
    assert same.findings == ()
    fov = mon.observe(T0 + 11_500, [], [CameraObservation("cam4", field_of_view_changed=True)])
    assert [f.reason for f in fov.findings] == [DriftReason.FIELD_OF_VIEW_CHANGED]
    assert mon.stream_changed_camera_ids == ("cam3", "cam4")


def test_resolution_change_after_drift_is_reported(calibration):
    """F1: 재투영 오차로 이미 DRIFTED 인 카메라의 해상도 변경도 근거로 내고 스트림 변경으로 기록한다."""
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    reports = _feed(mon, 10, 40, {"cam2": 12.0})
    assert reports[-1].camera("cam2").status is MonitorStatus.DRIFTED
    report = mon.observe(T0 + 40_000, [], [CameraObservation("cam2", image_size=(1280, 720))])
    assert [(f.camera_id, f.reason) for f in report.findings] == [("cam2", DriftReason.RESOLUTION_CHANGED)]
    assert report.camera("cam2").status is MonitorStatus.STREAM_CHANGED
    assert mon.stream_changed_camera_ids == ("cam2",)


def test_stream_reprojection_values_are_not_used(calibration):
    """스트림 정보에 실린 재투영 값은 보지 않는다(이 감시기가 직접 잰 값만 쓴다)."""
    mon = ReprojectionMonitor(calibration, CFG)
    report = mon.observe(T0, [], [CameraObservation("cam1", reprojection_median_px=50.0)])
    assert report.findings == ()


def test_reprojection_errors_need_min_samples(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    report = mon.ingest(T0, [("cam1", 1.0), ("cam1", 3.0)])
    assert report.reprojection_errors() == []  # 표본 2개는 min_window_samples(5) 미만
    report = mon.ingest(T0 + 100, [("cam1", 2.0)] * 3)
    errors = report.reprojection_errors()
    assert [e["camera_id"] for e in errors] == ["cam1"]
    assert errors[0]["reprojection_error_px"]["median"] == pytest.approx(2.0)


def test_ambiguous_inconsistency_is_not_attributed(calibration):
    """F4: 귀속 불확실 표본만 기준을 넘으면 근거는 내되(귀속 불확실) 어느 카메라도 DRIFTED 로 확정하지 않는다."""
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {}, attributable=False)
    reports = _feed(mon, 10, 40, {"cam1": 7.0, "cam2": 12.0, "cam3": 7.0}, attributable=False)
    findings = [f for r in reports for f in r.findings]
    assert sorted(f.camera_id for f in findings) == ["cam1", "cam2", "cam3"]
    assert all(f.message.startswith(AMBIGUOUS_PREFIX) for f in findings)
    last = reports[-1]
    assert all(last.camera(c).status is MonitorStatus.AMBIGUOUS for c in ("cam1", "cam2", "cam3"))
    assert last.camera("cam4").status is MonitorStatus.MONITORING
    assert not any(c.status is MonitorStatus.DRIFTED for c in last.cameras)


def test_attributed_drift_stops_ambiguous_judgement(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    _feed(mon, 10, 40, {"cam2": 12.0})
    reports = _feed(mon, 40, 70, {"cam1": 12.0}, attributable=False)
    assert not any(r.findings for r in reports)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"increase_ratio": 0.5},
        {"window_ms": 0},
        {"min_window_samples": 0},
        {"sustain_ms": -1},
        {"increase_margin_px": float("nan")},
        {"min_joints_per_sample": 1.5},
        {"absolute_max_median_px": 0.0},
        {"max_sample_age_ms": -1},
    ],
)
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        MonitorConfig(**kwargs)


# --------------------------------------------------------------------------- 표본 (합성 기하)


def _rigs(camera_factory, rotate=None, deg=1.0):
    rigs = {}
    for cid in IDS:
        cam = camera_factory(cid).to_contract()
        if cid == rotate:
            R = np.array(cam["R"])
            C = -R.T @ np.array(cam["t"])
            R2 = Rotation.from_rotvec([0.0, np.radians(deg), 0.0]).as_matrix() @ R
            cam["R"], cam["t"] = R2.tolist(), (-R2 @ C).tolist()
        rigs[cid] = RigCamera.from_calibration(cam)
    return rigs


def _project(cam, X):
    Xc = cam.to_camera(X)
    xd = distortion.distort(Xc[:, :2] / Xc[:, 2:3], cam.distortion_model, cam.distortion_coeffs)
    return (np.column_stack([xd, np.ones(len(xd))]) @ cam.K.T)[:, :2]


def _people(truth, n=6, noise=1.0, seed=0, cameras=IDS):
    rng = np.random.default_rng(seed)
    people = []
    for _ in range(n):
        base = np.array([rng.uniform(2.5, 5.5), rng.uniform(2.0, 4.0), 0.0])
        X = base + np.column_stack([rng.normal(0, 0.15, 17), rng.normal(0, 0.15, 17), np.linspace(0.1, 1.7, 17)])
        people.append(
            {cid: [(u, v, 0.9) for u, v in _project(truth[cid], X) + rng.normal(0, noise, (17, 2))] for cid in cameras}
        )
    return people


def test_leave_one_out_attributes_rotated_camera(camera_factory):
    calibrated = _rigs(camera_factory)
    truth = _rigs(camera_factory, rotate="cam2")
    samples = leave_one_out_samples(calibrated, _people(truth))
    assert all(attributable for _, _, attributable in samples)
    by_cam = {cid: [e for c, e, _ in samples if c == cid] for cid in IDS}
    assert all(len(v) == 6 for v in by_cam.values())
    assert min(by_cam["cam2"]) > 10.0  # f=1000px 에서 1° ≈ 17px
    for cid in ("cam1", "cam3", "cam4"):
        assert max(by_cam[cid]) < 4.0


def test_three_views_are_not_attributable(camera_factory):
    """같은 관절을 3대만 보면 기준 쌍이 하나뿐이라 귀속 불확실 표본이다."""
    rigs = _rigs(camera_factory)
    assert leave_one_out_samples(rigs, _people(rigs, cameras=("cam1", "cam2"))) == []
    samples = leave_one_out_samples(rigs, _people(rigs, cameras=("cam1", "cam2", "cam3")))
    assert {c for c, _, _ in samples} == {"cam1", "cam2", "cam3"}
    assert not any(attributable for _, _, attributable in samples)


def test_leave_one_out_skips_low_confidence_and_unknown_cameras(camera_factory):
    rigs = _rigs(camera_factory)
    people = _people(rigs, n=1)
    people[0]["cam9"] = people[0]["cam1"]
    people[0]["cam4"] = [(u, v, 0.1) for u, v, _ in people[0]["cam4"]]
    people[0]["cam3"] = [None] * 17
    samples = leave_one_out_samples(rigs, people)
    assert samples == []  # 쓸 수 있는 카메라가 cam1·cam2 두 대뿐이다
    people = _people(rigs, n=1)
    people[0]["cam4"] = people[0]["cam4"][:5]  # 관절 배치가 다르면 그 사람을 건너뛴다
    assert leave_one_out_samples(rigs, people) == []


def test_observe_end_to_end(camera_factory, calibration):
    truth = _rigs(camera_factory, rotate="cam2")
    mon = ReprojectionMonitor(calibration, CFG)
    nominal = _rigs(camera_factory)
    for k in range(20):
        mon.observe(T0 + k * 500, _people(nominal, n=2, seed=k))
    found = []
    for k in range(20, 80):
        found += mon.observe(T0 + k * 500, _people(truth, n=2, seed=k)).findings
    assert [f.camera_id for f in found] == ["cam2"]


def test_observe_three_cameras_does_not_blame_healthy(camera_factory):
    """F4: 3대만 보이는 교실에서 한 대가 돌아가면 정상 카메라를 DRIFTED 로 확정하지 않는다."""
    calib = {"calibration_version": "cal-1", "cameras": [camera_factory(c).to_contract() for c in IDS[:3]]}
    nominal, truth = _rigs(camera_factory), _rigs(camera_factory, rotate="cam2")
    mon = ReprojectionMonitor(calib, CFG)
    reports = [mon.observe(T0 + k * 500, _people(nominal, n=2, seed=k, cameras=IDS[:3])) for k in range(20)]
    reports += [mon.observe(T0 + k * 500, _people(truth, n=2, seed=k, cameras=IDS[:3])) for k in range(20, 100)]
    findings = [f for r in reports for f in r.findings]
    assert findings and all(f.message.startswith(AMBIGUOUS_PREFIX) for f in findings)
    assert not any(c.status is MonitorStatus.DRIFTED for r in reports for c in r.cameras)


def test_people_leaving_just_before_sustain_does_not_complete(calibration):
    """R1: 지속이 sustain 직전까지 쌓인 뒤 사람이 사라지면, 창 통계가 신선해도 새 관측 없이 확정하지 않는다."""
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    reports = _feed(mon, 10, 30, {"cam2": 12.0})
    last_seen = max(r.t_ms for r in reports if r.camera("cam2").exceed_ms < CFG.sustain_ms)
    exceed = next(r.camera("cam2").exceed_ms for r in reports if r.t_ms == last_seen)
    assert CFG.sustain_ms - 1_000 <= exceed < CFG.sustain_ms
    for step in (100, 500, 1_000, 1_900):  # 빈 호출 빈도를 바꿔도
        mon2 = ReprojectionMonitor(calibration, CFG)
        _feed(mon2, 0, 10, {})
        _feed(mon2, 10, (last_seen - T0) / 1000 + 0.5, {"cam2": 12.0})
        later = [mon2.ingest(t, []) for t in range(last_seen + step, last_seen + 40_000, step)]
        assert not any(r.findings for r in later)
        assert all(r.camera("cam2").exceed_ms in (exceed, 0) for r in later)


def test_ambiguous_history_without_current_samples_is_no_evidence(calibration):
    """R4: 귀속 불확실 근거를 낸 뒤 사람이 사라지면 감시 근거 없음이다(이력은 ambiguous_reported)."""
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {}, attributable=False)
    _feed(mon, 10, 40, {"cam1": 7.0, "cam2": 12.0, "cam3": 7.0}, attributable=False)
    later = _idle(mon, 40, 80)
    last = later[-1]
    assert last.evidence is MonitorEvidence.NONE
    for cid in ("cam1", "cam2", "cam3"):
        assert last.camera(cid).status is MonitorStatus.NO_EVIDENCE and last.camera(cid).ambiguous_reported


def test_stale_statistics_are_not_in_payload(calibration):
    mon = ReprojectionMonitor(calibration, CFG)
    _feed(mon, 0, 10, {})
    assert {e["camera_id"] for e in mon.ingest(T0 + 10_000, []).reprojection_errors()} == set(IDS)
    stale = mon.ingest(T0 + 10_000 + CFG.max_sample_age_ms + 1, [])
    assert stale.reprojection_errors() == []


def test_baseline_from_calibration_input_is_verified(camera_factory, calibration):
    rigs = _rigs(camera_factory)
    obs = [p for k in range(10) for p in _people(rigs, n=3, seed=k)]
    record = baseline_from_observations(calibration, obs, CFG)
    assert record.verified_camera_ids == IDS and all(0 < v < 4 for v in record.values_px.values())
    assert set(record.sources.values()) == {"CALIBRATION_INPUT"}
    mon = ReprojectionMonitor(calibration, CFG, baseline=record)
    reports = [mon.observe(T0 + k * 500, _people(rigs, n=2, seed=100 + k)) for k in range(20)]
    assert reports[-1].evidence is MonitorEvidence.FULL
    few = baseline_from_observations(calibration, obs[:2], CFG)  # 표본이 모자라면 비운다
    assert few.values_px == {} and few.verified_camera_ids == ()


def test_short_focal_pre_rotation_stays_unverified(camera_factory):
    """R2: 초점이 짧아 1° 가 절대 상한 아래인 시작 전 틀어짐은 감지 못 하더라도 FULL 로 보고하지 않는다."""

    def short(cid):
        cam = camera_factory(cid).to_contract()
        cam["K"] = [[400.0, 0.0, 960.0], [0.0, 400.0, 540.0], [0.0, 0.0, 1.0]]
        return cam

    calib = {"calibration_version": "cal-1", "cameras": [short(c) for c in IDS]}
    truth = {}
    for cid in IDS:
        cam = short(cid)
        if cid == "cam2":
            R = np.array(cam["R"])
            C = -R.T @ np.array(cam["t"])
            R2 = Rotation.from_rotvec([0.0, np.radians(1.0), 0.0]).as_matrix() @ R
            cam["R"], cam["t"] = R2.tolist(), (-R2 @ C).tolist()
        truth[cid] = RigCamera.from_calibration(cam)
    mon = ReprojectionMonitor(calib, CFG)
    reports = [mon.observe(T0 + k * 500, _people(truth, n=2, seed=k)) for k in range(120)]
    assert all(r.evidence is not MonitorEvidence.FULL for r in reports)
    assert not reports[-1].camera("cam2").baseline_verified


def _gap_monitor(calibration):
    """기본 설정, cam2 검증 기준선 1px. 0~29초 정상, 30~62초 12px(초과 18초), 이후 빈 호출."""
    mon = ReprojectionMonitor(calibration, baseline=BaselineRecord("cal-1", {"cam2": 1.0}, ("cam2",)))
    for sec in range(63):
        report = mon.ingest(T0 + sec * 1000, [("cam2", 1.0 if sec < 30 else 12.0)] * 2)
    assert report.camera("cam2").exceed_ms == 18_000
    return mon


@pytest.mark.parametrize("gap_s", [29, 30, 31])
def test_observation_gap_boundary(calibration, gap_s):
    """N1: 관측이 evidence_gap_ms(30초) 넘게 끊겼다 돌아오면 이전 지속 시간을 쓰지 않는다. 한도 안이면 잇는다."""
    mon = _gap_monitor(calibration)
    back = 62 + gap_s
    for sec in range(63, back):
        mon.ingest(T0 + sec * 1000, [])  # 빈 호출을 계속한다
    marks = []
    for sec in range(back, back + 10):  # 복귀 뒤 10초(2명)
        report = mon.ingest(T0 + sec * 1000, [("cam2", 12.0)] * 2)
        if report.findings:
            marks.append(sec)
    if gap_s <= 30:
        assert marks  # 끊김 한도 안: 쌓인 18초에 이어 붙어 확정된다
    else:
        assert not marks and report.camera("cam2").exceed_ms < MonitorConfig().sustain_ms
        later = []
        for sec in range(back + 10, back + 40):
            r = mon.ingest(T0 + sec * 1000, [("cam2", 12.0)] * 2)
            later += [sec] if r.findings else []
        assert later and later[0] - back >= MonitorConfig().sustain_ms / 1000  # 복귀 뒤 새로 20초를 쌓아야 한다


@pytest.mark.parametrize("empty_calls", [True, False])
def test_observation_gap_return_with_enough_samples_at_once(calibration, empty_calls):
    """N1: 31초 공백 뒤 한 번에 충분한 표본으로 돌아와도(빈 호출이 있든 없든) 이전 지속 시간을 버린다."""
    mon = _gap_monitor(calibration)
    if empty_calls:
        for sec in range(63, 93):
            mon.ingest(T0 + sec * 1000, [])
    report = mon.ingest(T0 + 93_000, [("cam2", 12.0)] * 25)
    assert not report.findings and report.camera("cam2").exceed_ms == 0
    assert report.camera("cam2").status is MonitorStatus.SUSPECT
