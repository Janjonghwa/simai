"""-346 연결 시험. 합성 투영/사람 대응은 상한이며 제품 성능으로 쓰지 않는다."""

import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2]):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_calibration_swap import candidate, recording, site as calibration_site  # noqa: E402, F401
import test_calibration_swap  # noqa: E402
from test_tier0_service import _settings  # noqa: E402

from services.tier0.drift_watch import BackgroundDriftWatch, person_observations  # noqa: E402
from services.tier0.i1_client import Envelope  # noqa: E402
from services.tier0.service import Tier0Service, load_baselines, save_baselines  # noqa: E402
from services.tier0.settings import Tier0ConfigError, load_settings  # noqa: E402
from simai_ai.calibration.document import content_hash  # noqa: E402
from simai_ai.calibration.drift import CameraObservation  # noqa: E402
from simai_ai.calibration.monitor import BaselineRecord, MonitorConfig, MonitorEvidence  # noqa: E402
from simai_ai.calibration.runtime import RuntimeCalibrationStore  # noqa: E402
from simai_ai.geometry import distortion  # noqa: E402
from simai_ai.geometry.interpolation import InterpolatedView  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.tracking.cross_view import AssociationFrame, LocalView, UncertainReason, ViewCluster  # noqa: E402

FAST = MonitorConfig(window_ms=5000, min_window_samples=3, baseline_min_samples=3,
                     baseline_min_span_ms=1000, sustain_ms=2000)


@pytest.fixture
def site(tmp_path, recording, monkeypatch):
    original = test_calibration_swap._settings
    monkeypatch.setattr(test_calibration_swap, "_settings",
                        lambda *args, **kwargs: replace(original(*args, **kwargs), human_calibration_enabled=True))
    yield from calibration_site.__wrapped__(tmp_path, recording, monkeypatch)


def field_calibration(site):
    service = site.service
    service.drift_watch._config = FAST
    service.perception.observe_association = None
    doc, report = candidate(service)
    doc["provider_source"] = "G1_AUTO"
    doc["content_hash"] = content_hash(doc)
    report["calibration"]["content_hash"] = doc["content_hash"]
    assert service.propose_calibration(doc, report)
    site.tick()
    # 기본 워커는 실제 서비스가 만들며, 합성 시험은 같은 경로에 빠른 설정을 쓴다.
    return service, doc


def association(service, t, *, turned=False, cameras=4, empty=False):
    if empty:
        return AssociationFrame(t, service.rig.calibration_version, (), ())
    rng = np.random.default_rng(t % 10000)
    points = np.array([4., 3., 0.]) + np.column_stack(
        [rng.normal(0, .15, 17), rng.normal(0, .15, 17), np.linspace(.1, 1.7, 17)])
    views = []
    for i, (cid, camera) in enumerate(list(service.rig.cameras.items())[:cameras]):
        xc = camera.to_camera(points)
        if turned and i == 1:
            angle = np.deg2rad(2.)
            rotation = np.array([[np.cos(angle), 0, np.sin(angle)], [0, 1, 0],
                                 [-np.sin(angle), 0, np.cos(angle)]])
            xc = xc @ rotation.T
        xy = distortion.distort(xc[:, :2] / xc[:, 2:3], camera.distortion_model, camera.distortion_coeffs)
        uv = (np.column_stack([xy, np.ones(17)]) @ camera.K.T)[:, :2]
        keypoints = tuple((float(u), float(v), .95) for u, v in uv)
        view = InterpolatedView(cid, t, keypoints, "SIM", 0., (t,))
        views.append(LocalView(cid, "local", view))
    return AssociationFrame(t, service.rig.calibration_version,
                            (ViewCluster("person", tuple(views), None),), ())


def drive(site, service, doc, *, cameras=4, empty=False, count=12):
    for _ in range(count):
        site.elapsed[0] += 1000
        t = service.clock.now_ms()
        assoc = association(service, t, turned=True, cameras=cameras, empty=empty)
        service.drift_watch.offer(doc, assoc)
        deadline = time.monotonic() + 5
        while service._drift_report is None or service._drift_report.t_ms < t:
            assert time.monotonic() < deadline, f"감시 정체: errors={service.drift_watch.errors}"
            time.sleep(.002)


def test_synthetic_drift_persists_notifies_health_and_i1(site):
    service, doc = field_calibration(site)
    original = service.calibration.path(doc["calibration_version"]).read_bytes()
    notified = []
    service._on_camera_drifted = notified.append
    drive(site, service, doc)
    assert service._calibration_state() == "DRIFTED"
    assert notified == [list(service.rig.cameras)[1]]
    assert service.health.payload()["calibration_state"] == "DRIFTED"
    assert service.health.payload()["system_health"] in ("DEGRADED", "UNSAFE_UNKNOWN")
    assert service.calibration.path(doc["calibration_version"]).read_bytes() == original
    assert len(service.calibration._head["versions"]) == 2
    transition = service.calibration.transitions[-1]
    payload = transition["payload"]
    assert payload["reason"] == "DRIFT_DETECTED" and payload["calibration_version"] == doc["calibration_version"]
    assert payload["calibration_hash"] == doc["content_hash"]
    envelope = Envelope(service.settings.gateway_id, service.clock)
    assert schema_errors(envelope.make("calibration_update", payload), "calibration_update") == []
    service.link = SimpleNamespace(publisher=SimpleNamespace(envelope=envelope))
    messages = service._calibration_messages()
    assert messages[-1]["msg_id"] == transition["msg_id"] and messages[-1]["payload"] == payload
    assert service._calibration_messages() == []
    service.link = None
    restored = RuntimeCalibrationStore(service.calibration.root, lambda: pytest.fail("복원 대신 bootstrap"))
    assert restored.state == "DRIFTED" and restored.transitions == service.calibration.transitions
    drive(site, service, doc, empty=True, count=7)
    assert service._calibration_state() == "DRIFTED" and notified == [list(service.rig.cameras)[1]]
    assert len(service.calibration.transitions) == 2
    assert service._drift_report.evidence is MonitorEvidence.NONE


def test_ambiguous_drift_only_reports_without_recollection(site):
    service, doc = field_calibration(site)
    notified = []
    service._on_camera_drifted = notified.append
    drive(site, service, doc, cameras=3)
    assert service._calibration_state() == "DRIFTED"
    assert service.calibration.transitions[-1]["payload"]["reason"] == "DRIFT_DETECTED"
    assert notified == []
    assert any(c.ambiguous_reported for c in service._drift_report.cameras)


def test_no_people_keeps_state_and_reports_no_evidence(site):
    service, doc = field_calibration(site)
    drive(site, service, doc, empty=True, count=3)
    assert service._calibration_state() == "PROVISIONAL"
    assert service._drift_report.evidence is MonitorEvidence.NONE
    assert len(service.calibration.transitions) == 1


def test_rig_switch_restarts_baseline_and_rejects_old_report(site):
    service, doc = field_calibration(site)
    drive(site, service, doc)
    old_report = service._drift_report
    new_doc, report = candidate(service, "cal-new")
    assert service.propose_calibration(new_doc, report)
    site.tick()
    assert service._drift_report is None and service._calibration_state() == "PROVISIONAL"
    service._apply_drift_report(old_report, ())
    assert service._drift_report is None
    drive(site, service, new_doc, empty=True, count=1)
    assert all(c.baseline_median_px is None and not c.baseline_verified for c in service._drift_report.cameras)
    assert service.drift_watch._monitor.calibration_version == "cal-new"
    assert doc["calibration_version"] in service.drift_watch.baselines()


def test_baseline_verification_survives_restore_and_is_version_bound(site):
    service, doc = field_calibration(site)
    cid = next(iter(service.rig.cameras))
    version = doc["calibration_version"]
    baseline = BaselineRecord(version, {cid: 1.}, (cid,), {cid: "calibration observations"})
    save_baselines(service.settings.baselines_file, {version: baseline})
    worker = BackgroundDriftWatch(lambda *_: None, baselines=load_baselines(service.settings.baselines_file), config=FAST)
    worker._measure(doc, association(service, service.clock.now_ms()), ())
    assert worker.baselines()[version].verified_camera_ids == (cid,)
    assert worker.baselines()[version].values_px[cid] == 1.


def test_stream_change_blocks_recollection_and_notifies_even_after_drift(site):
    service, doc = field_calibration(site)
    notified = []
    service._on_camera_drifted = notified.append
    cid = next(iter(service.rig.cameras))
    site.elapsed[0] += 1000
    assoc = association(service, service.clock.now_ms(), empty=True)
    changed = (CameraObservation(cid, image_size=(1, 1)),)
    service.drift_watch._measure(doc, assoc, changed)
    assert service._calibration_state() == "DRIFTED" and notified == []
    assert service.calibration.transitions[-1]["payload"]["reason"] == "STREAM_CHANGED"
    drive(site, service, doc)
    assert notified == []


def test_save_failure_retries_latched_drift_evidence(site, monkeypatch):
    service, doc = field_calibration(site)
    original = service.calibration._save
    failed = []

    def save(head):
        if not failed:
            failed.append(True)
            raise OSError("disk unavailable once")
        original(head)

    monkeypatch.setattr(service.calibration, "_save", save)
    drive(site, service, doc)
    assert failed and service._calibration_state() == "DRIFTED"
    assert len(service.calibration.transitions) == 2


def test_monitor_disabled(tmp_path, recording, monkeypatch):
    monkeypatch.setattr(Tier0Service, "_run_judge", lambda self: None)
    monkeypatch.setattr(Tier0Service, "_run_housekeeping", lambda self: None)
    settings = _settings(tmp_path, recording, anchor_ms=1760342400000)
    config = tmp_path / "tier0.json"
    value = json.loads(config.read_text(encoding="utf-8"))
    value["drift_watch_enabled"] = False
    config.write_text(json.dumps(value), encoding="utf-8")
    settings = load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "1"})
    service = Tier0Service(settings)
    service.start()
    try:
        assert service.drift_watch is None and service.perception.observe_association is None
        assert not any(t.name == "tier0-drift-watch" for t in service._threads)
    finally:
        assert service.stop()
    value["drift_watch_enabled"] = "false"
    config.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(Tier0ConfigError, match="bool"):
        load_settings(config, env={})


def test_default_perception_supplies_real_clusters(site):
    service = site.service
    calls = []
    service.drift_watch.offer = lambda doc, assoc, streams: calls.append((doc, assoc))
    for _ in range(12):
        site.tick()
    assert calls and any(a.clusters for _, a in calls)
    for doc, assoc in calls:
        assert assoc.calibration_version == doc["calibration_version"]
        people = person_observations(assoc)
        assert all(p.t == assoc.t and len(p.views) >= 3 for p in people)
    assoc = next(a for _, a in calls if a.clusters)
    cluster = assoc.clusters[0]
    uncertain = replace(cluster, uncertain_reasons=(UncertainReason.AMBIGUOUS_ASSIGNMENT,))
    assert person_observations(replace(assoc, clusters=(uncertain,))) == ()


def test_slow_monitor_does_not_block_ticks_and_queue_is_bounded(site, monkeypatch):
    service = site.service
    worker = service.drift_watch
    entered, release = threading.Event(), threading.Event()
    original = worker._measure

    def slow(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(worker, "_measure", slow)
    try:
        site.tick()
        assert entered.wait(3)
        # 기하 계산이 무기한 기다리는 동안에도 판정 lock과 tick은 계속 진행한다.
        start = time.perf_counter()
        for _ in range(12):
            site.tick()
        elapsed = time.perf_counter() - start
        assert elapsed < 3 and service.judge.stats.perceived >= 10
        doc = service.calibration.current()
        offer_times = []
        for i in range(1, 1001):
            assoc = association(service, service.clock.now_ms() + i * 1000, empty=True)
            offered_at = time.perf_counter_ns()
            worker.offer(doc, assoc)
            offer_times.append((time.perf_counter_ns() - offered_at) / 1e6)
        assert worker.dropped >= 8
        assert worker._pending[1].t == service.clock.now_ms() + 1000000
        p95 = float(np.percentile(offer_times, 95))
        assert p95 < 5
        print(f"blocked_monitor_ticks=12 elapsed_ms={elapsed * 1000:.3f} offer_n=1000 offer_p95_ms={p95:.6f}")
    finally:
        release.set()


def test_human_calibration_master_switch(tmp_path, recording, caplog):
    settings = _settings(tmp_path, recording, anchor_ms=1760342400000)
    assert not settings.human_calibration_enabled
    service = Tier0Service(settings)
    with caplog.at_level("INFO", logger="services.tier0"):
        service.start()
    try:
        assert service.drift_watch is None
        assert service.live_calibration is None
        assert service.perception.observe_association is None
        assert service.perception.observe_frame is None
        assert not any(t.name in {"tier0-drift-watch", "tier0-live-calibration"} for t in service._threads)
        assert not service.status()["calibration"]["live_candidate"]["enabled"]
        assert "사람 관절 보정 꺼짐" in caplog.text
    finally:
        assert service.stop()
    config = tmp_path / "tier0.json"
    assert load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "1"}).human_calibration_enabled
    with pytest.raises(Tier0ConfigError, match="SIMAI_TIER0_HUMAN_CALIBRATION"):
        load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "yes"})
