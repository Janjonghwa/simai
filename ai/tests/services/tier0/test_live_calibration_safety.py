"""-356 안전장치 합성 시나리오. 감지/복구 시간은 제품 성능이 아니다."""

import copy
import sys
from dataclasses import replace
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("scipy")

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2], HERE.parents[1] / "calibration"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_calibration_swap import candidate, recording, site as calibration_site  # noqa: E402, F401
import test_calibration_swap  # noqa: E402
from test_drift_watch import FAST, field_calibration  # noqa: E402
from test_live_candidate import synthetic_rig_and_frames  # noqa: E402, F401
from simai_ai.calibration import live_candidate as lc  # noqa: E402
from simai_ai.calibration.document import content_hash  # noqa: E402
from simai_ai.calibration.live_safety import CandidateConfirmation, SwapProbation  # noqa: E402
from simai_ai.calibration.monitor import BaselineRecord, MonitorReport, MonitorStatus, ReprojectionMonitor  # noqa: E402
from simai_ai.calibration.runtime import RuntimeCalibrationStore  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from services.tier0.i1_client import Envelope  # noqa: E402


@pytest.fixture
def site(tmp_path, recording, monkeypatch):
    original = test_calibration_swap._settings
    monkeypatch.setattr(test_calibration_swap, "_settings",
                        lambda *args, **kwargs: replace(original(*args, **kwargs), human_calibration_enabled=True))
    yield from calibration_site.__wrapped__(tmp_path, recording, monkeypatch)


def changed_report(doc, moved, *, start=0, end=65_000):
    ids = [c["camera_id"] for c in doc["cameras"]]
    monitor = ReprojectionMonitor(doc, baseline=BaselineRecord(doc["calibration_version"], dict.fromkeys(ids, 1.), tuple(ids)))
    findings = []
    for t in range(start, start + end + 1, 1000):
        report = monitor.ingest(t, [(cid, 20. if cid in moved and t >= start + 30_000 else 1.) for cid in ids])
        findings.extend(report.findings)
    return monitor, replace(report, findings=tuple(findings))


def test_sudden_turn_change_point_precedes_delayed_drift(synthetic_rig_and_frames):
    truth, _, _, _ = synthetic_rig_and_frames
    _, report = changed_report(truth, {"cam_D"})
    camera = next(c for c in report.cameras if c.camera_id == "cam_D")
    assert camera.status is MonitorStatus.DRIFTED
    assert camera.change_point_ms == 30_000
    assert report.t_ms > camera.change_point_ms + 30_000
    assert all(c.status is MonitorStatus.MONITORING for c in report.cameras if c.camera_id != "cam_D")


def test_cutoff_keeps_post_move_nulls_and_rejects_late_old_frames(synthetic_rig_and_frames):
    _, _, _, frames = synthetic_rig_and_frames
    buffer = lc.LiveJointBuffer()
    for frame in frames:
        if frame.camera_id == "cam_D":
            frame = replace(frame, detections=tuple(replace(d, detection=replace(d.detection,
                keypoints_2d=(None, *d.detection.keypoints_2d[1:]))) for d in frame.detections))
        buffer.append_frame(frame)
    cutoff = frames[len(frames) // 2].estimated_capture_time_ms
    expected = sum(f.camera_id == "cam_D" and f.estimated_capture_time_ms < cutoff for f in frames)
    assert buffer.reset_camera("cam_D", since_ms=cutoff) == expected
    assert not buffer.append_frame(replace(frames[3], frame_sequence=10000))
    tracks = buffer.to_session_tracks()
    assert all(t >= cutoff for t, views in tracks.ticks for view in views if view.camera_id == "cam_D")
    assert all(view.view.keypoints[0] is None for _, views in tracks.ticks for view in views if view.camera_id == "cam_D")
    assert any(t < cutoff for t, views in tracks.ticks for view in views if view.camera_id == "cam_A")
    assert buffer.summary()["total_frames"] == len(frames) - expected


@pytest.mark.parametrize("moved_count", [1, 2])
def test_move_then_no_people_defers_and_locks_recovery(site, moved_count):
    service, doc = field_calibration(site)
    live = service.live_calibration
    ids = list(service.rig.cameras)[:moved_count]
    _, report = changed_report(doc, set(ids), start=service.clock.now_ms())
    service._apply_drift_report(report, ())
    assert set(live.recovering) == set(ids)
    assert service.calibration.state == "DRIFTED"
    # 사람이 없으므로 복구 자료가 없고, 일반 후보를 병렬로 실행할 수 없다.
    live.runner._config = replace(live.runner.config, use_subprocess=False)
    assert live.run_once().status == "INSUFFICIENT_DATA"
    assert service._pending_calibration is None
    assert service.health.payload()["calibration_state"] == "DRIFTED"
    with live._run_lock:
        assert live.run_once() is None


def test_second_move_invalidates_inflight_generation_and_confirmation(site):
    service, doc = field_calibration(site)
    live = service.live_calibration
    cid = list(service.rig.cameras)[1]
    monitor, report = changed_report(doc, {cid}, start=service.clock.now_ms())
    service._apply_drift_report(report, ())
    generation = live.generation
    live.confirmation.observe({"previous_version": doc["calibration_version"], "validation_end_ms": report.t_ms,
                               "improvement_decision": {"accepted": True}})
    for t in range(report.t_ms + 1000, report.t_ms + 4000, 1000):
        report2 = monitor.ingest(t, [(cid, 60.)])
    service._apply_drift_report(report2, ())
    assert live.generation > generation
    assert live.confirmation.count == 0
    assert live.recovering[cid] == report.t_ms + 1000
    proposal, proposal_report = candidate(service, "cal-obsolete")
    assert not service._propose_live_calibration(proposal, proposal_report, generation)


def test_confirmation_needs_new_data_and_resets_on_failure():
    confirm = CandidateConfirmation()
    report = {"previous_version": "cal-1", "validation_end_ms": 1000, "improvement_decision": {"accepted": True}}
    assert not confirm.observe(report)
    assert not confirm.observe(report)
    assert confirm.count == 1
    assert confirm.observe({**report, "validation_end_ms": 16_000})
    assert not confirm.observe({**report, "improvement_decision": {"accepted": False}})
    assert confirm.count == 0
    assert not confirm.observe({**report, "previous_version": "cal-2", "validation_end_ms": 20_000})


def accept_automatic(service, version):
    proposal, report = candidate(service, version)
    report.update(improvement_decision={"accepted": True}, validation_end_ms=service.clock.now_ms())
    generation = service.live_calibration.generation
    assert not service._propose_live_calibration(proposal, report, generation)
    report["validation_end_ms"] += 15_000
    assert service._propose_live_calibration(proposal, report, generation)
    return proposal


def test_swap_worsens_rolls_back_as_new_version_and_survives_restart(site):
    service, old = field_calibration(site)
    old_bytes = service.calibration.path(old["calibration_version"]).read_bytes()
    proposed = accept_automatic(service, "cal-bad-swap")
    site.tick()
    assert service._swap_probation is not None
    start = service.clock.now_ms()
    # 교체 직후의 창/표본/지속 조건을 갖춘 독립 운영 근거를 공급한다.
    ids = list(service.rig.cameras)
    monitor = ReprojectionMonitor(proposed, FAST,
        baseline=BaselineRecord(proposed["calibration_version"], dict.fromkeys(ids, 1.), tuple(ids)))
    for delta in range(1000, 15_001, 1000):
        site.elapsed[0] += 1000
        report = monitor.ingest(start + delta, [(cid, 20. if cid == ids[1] else 1.) for cid in ids])
        service._apply_drift_report(report, ())
    assert service.calibration_status["probation"] == "악화 지속: 되돌림 대기"
    pending_version = service.calibration_status["pending_version"]
    assert pending_version not in (old["calibration_version"], proposed["calibration_version"])
    site.tick()
    restored = service.calibration.current()
    assert restored["calibration_version"] == pending_version
    assert restored["cameras"] == old["cameras"]
    assert restored["state"] == "PROVISIONAL"
    assert service.calibration.path(old["calibration_version"]).read_bytes() == old_bytes
    assert service.calibration.current_report()["restored_from"] == old["calibration_version"]
    payload = service.calibration.transitions[-1]["payload"]
    assert payload["previous_calibration_version"] == proposed["calibration_version"]
    envelope = Envelope(service.settings.gateway_id, service.clock)
    assert schema_errors(envelope.make("calibration_update", payload), "calibration_update") == []
    assert "되돌림 뒤" in service.live_calibration.blocked_reason
    store = RuntimeCalibrationStore(service.calibration.root, lambda: pytest.fail("bootstrap 불필요"))
    assert store.current() == restored
    assert store.transitions == service.calibration.transitions


def test_probation_no_people_gap_and_fusion_deterioration(synthetic_rig_and_frames):
    truth, _, _, _ = synthetic_rig_and_frames
    version = truth["calibration_version"]
    probation = SwapProbation("old", version, 0, fusion_before=.9)
    empty = MonitorReport(1000, version, (), ())
    assert not probation.observe(empty)
    assert not probation.observe(replace(empty, t_ms=2000), .5)
    assert not probation.observe(replace(empty, t_ms=20_000), .5)  # 공백 뒤 지속 재시작
    for t in range(21_000, 30_000, 1000):
        assert not probation.observe(replace(empty, t_ms=t), .5)
    assert probation.observe(replace(empty, t_ms=30_000), .5)
    assert not probation.observe(replace(empty, t_ms=61_000), .5)


@pytest.mark.parametrize("moved_count", [1, 2, 3])
def test_baseline_alignment_stops_previous_to_previous_accumulation(monkeypatch, synthetic_rig_and_frames, moved_count):
    truth, _, _, frames = synthetic_rig_and_frames
    previous = copy.deepcopy(truth)
    for camera in previous["cameras"]:
        camera["t"] = (np.asarray(camera["t"]) - np.asarray(camera["R"]) @ np.array([.04, 0., 0.])).tolist()
    previous["calibration_version"] = "cal-shifted"
    previous["content_hash"] = content_hash(previous)
    buffer = lc.LiveJointBuffer()
    for frame in frames:
        buffer.append_frame(frame)
    tracks = buffer.to_session_tracks()
    ids = [c["camera_id"] for c in truth["cameras"]]
    report = {"world_alignment": {"anchor_cameras": ids, "usable": True},
              "holdout": {"before": {}}, "calibration": {}}
    result = lc.CandidateRunResult("PROPOSED", True, "full_session", previous, report)
    monkeypatch.setattr(lc, "_execute_candidate_cycle", lambda *args, **kwargs: result)
    monkeypatch.setattr(lc, "evaluate_calibration", lambda *args, **kwargs: {})
    monkeypatch.setattr(lc, "common_reprojection_metrics", lambda *args, **kwargs: {})
    monkeypatch.setattr(lc, "evaluate_improvement", lambda *args, **kwargs: ({}, {"accepted": True}))
    actual = lc.execute_candidate_cycle(tracks, {}, previous, world_reference=truth, moved_camera_ids=ids[-moved_count:])
    if moved_count == 3:
        assert not actual.proposed
        assert "2대 미만" in actual.report["failure_reason"]
        return
    assert actual.proposed
    for camera, baseline in zip(actual.calibration["cameras"], truth["cameras"], strict=True):
        assert np.allclose(camera["R"], baseline["R"], atol=1e-8)
        assert np.allclose(camera["t"], baseline["t"], atol=1e-8)
    assert actual.report["reference_version"] == truth["calibration_version"]
    assert ids[-1] not in actual.report["world_alignment"]["anchor_cameras"]
    assert actual.report["cumulative_deviation"]["cameras"][ids[0]]["position_cm"] < .001


def test_reference_and_moved_history_survive_restart(site):
    service, doc = field_calibration(site)
    cid = list(service.rig.cameras)[1]
    _, report = changed_report(doc, {cid}, start=service.clock.now_ms())
    service._apply_drift_report(report, ())
    expected = service.calibration.safety_reference()
    store = RuntimeCalibrationStore(service.calibration.root, lambda: pytest.fail("bootstrap 불필요"))
    assert store.safety_reference() == expected
    assert cid in expected[1]


def test_safety_can_be_disabled(site):
    live = site.service.live_calibration
    live.safety_enabled = False
    proposal, report = candidate(site.service, "cal-legacy-behavior")
    assert site.service._propose_live_calibration(proposal, report, live.generation)
