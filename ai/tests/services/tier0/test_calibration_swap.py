"""-344: 실제 인지·판정·영속 저장을 묶어 tick 경계와 재시작을 검증한다. SIM 지표는 상한이다."""

import copy
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2]):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import tier0_site as ts  # noqa: E402
from test_tier0_service import _settings  # noqa: E402

from services.tier0.i1_client import Envelope, I1Client  # noqa: E402
from services.tier0.i1_publisher import I1Publisher  # noqa: E402
from services.tier0.service import Tier0Service, Tier0StartError  # noqa: E402
from simai_ai.calibration.document import content_hash  # noqa: E402
from simai_ai.calibration.runtime import RuntimeCalibrationStore  # noqa: E402
from simai_ai.config import BundleVerifier  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import default_registry  # noqa: E402

T0 = 1_760_342_400_000


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    run = ts.run_sim(tmp_path_factory.mktemp("swap-sim"), ts.scenario(4000))
    path = run / "calibration.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["coordinate_frame"]["site_id"] = "swap-test-site"
    doc["content_hash"] = content_hash(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return run


@pytest.fixture
def site(tmp_path, recording, monkeypatch):
    # 실제 판정 스레드 대신 시험이 tick을 전진시킨다. 나머지 조립과 체크포인트/저널은 그대로다.
    monkeypatch.setattr(Tier0Service, "_run_judge", lambda self: None)
    monkeypatch.setattr(Tier0Service, "_run_housekeeping", lambda self: None)
    elapsed = [0]
    settings = _settings(tmp_path, recording, anchor_ms=T0)
    service = Tier0Service(settings, mono_ns=lambda: elapsed[0] * 1_000_000, wall_ms=lambda: T0 + elapsed[0])
    service.start()
    output = []
    service.judge._offer_world_state = lambda payload, msg_id: output.append(copy.deepcopy(payload))

    def tick():
        elapsed[0] += 100
        return service.judge.tick()

    value = SimpleNamespace(service=service, tick=tick, output=output, settings=settings, elapsed=elapsed)
    yield value
    service.link = None  # 시험 전용 링크는 네트워크 스레드가 없다
    if not service._stop.is_set():
        assert service.stop()


def candidate(service, version="cal-swap-2"):
    old = service.calibration.current()
    doc = copy.deepcopy(old)
    doc["calibration_version"] = version
    doc["state"] = "PROVISIONAL"
    # 실제 투영 행렬도 달라져야 캐시 무효화를 검증할 수 있다.
    doc["cameras"][0]["t"][0] += 0.001
    doc["content_hash"] = content_hash(doc)
    ids = [c["camera_id"] for c in doc["cameras"]]
    report = {
        "previous_version": old["calibration_version"],
        "calibration": {"version": version, "content_hash": doc["content_hash"], "state": "PROVISIONAL", "schema_errors": []},
        "split": {"overlap_ticks": 0},
        "data_sufficiency": {"usable": True, "counts": {
            "fit_ticks": 30, "fit_active_ticks": 25, "fit_observations": 40,
            "fit_3plus_observations": 25, "camera_3plus_observations": dict.fromkeys(ids, 15),
            "holdout_ticks": 15, "holdout_active_ticks": 10,
        }},
        "holdout": {
            "before": {"reprojection_error_px": {cid: {"median": 3., "p95": 7.} for cid in ids}},
            "after": {"reprojection_error_px": {cid: {"median": 1., "p95": 3.} for cid in ids}},
        },
        "world_alignment": {"usable": True, "anchor_cameras": ids,
                            "anchor_center_residuals_cm": dict.fromkeys(ids, 1.),
                            "anchor_rotation_residuals_deg": dict.fromkeys(ids, 0.1)},
    }
    return doc, report


def test_switch_boundary_versions_provenance_geometry_avatar_and_track_continuity(site):
    service = site.service
    for _ in range(12):
        site.tick()
    assert site.output and site.output[-1]["tracks"]
    old = service.rig
    manager, contact = service.perception.manager, service.perception.contact
    ids = {t["world_id"] for t in site.output[-1]["tracks"]}
    assert service.perception.associator._essential
    doc, report = candidate(service)
    assert service.propose_calibration(doc, report)
    assert service.rig is old and service.perception.rig is old
    assert service.calibration.current()["calibration_version"] == old.calibration_version
    # 호출자가 수락 뒤 원본 객체를 바꿔도 저장 후보는 불변이다.
    doc["cameras"][0]["t"][0] += 100
    report["world_alignment"]["usable"] = False
    shared = []
    service.perception.avatar_writer = SimpleNamespace(offer=lambda payload, images: shared.append(payload), close=lambda: None)
    result = site.tick()
    assert result.perceived is not None
    version = "cal-swap-2"
    assert service.rig is service.perception.rig is service.perception.associator.rig
    assert service.rig.calibration_version == version
    assert service.perception.manager is manager and service.perception.contact is contact
    assert ids <= {t["world_id"] for t in site.output[-1]["tracks"]}
    assert site.output[-2]["calibration_version"] == old.calibration_version
    payload = site.output[-1]
    assert payload["calibration_version"] == result.perceived.frame.calibration_version == version
    assert payload["input_provenance"]["calibration"]["calibration_version"] == version
    assert shared[-1]["world"]["calibration_version"] == shared[-1]["calibration"]["calibration_version"] == version
    assert service._calibration_state() == "PROVISIONAL"
    assert service.calibration_status["effective_at"] == service.clock.now_ms()
    # 새 기하로 만든 캐시만 남는다.
    assoc = service.perception.associator
    cameras = list(service.rig.cameras.values())
    expected = assoc._essential_matrix(cameras[0], cameras[1])
    fresh = type(assoc)(service.rig)._essential_matrix(cameras[0], cameras[1])
    assert (expected == fresh).all()


def test_proposal_from_other_thread_during_tick_does_not_mix(site):
    service = site.service
    for _ in range(5):
        site.tick()
    old = service.rig.calibration_version
    doc, report = candidate(service)
    entered, finished = threading.Event(), threading.Event()
    result = []

    def propose():
        entered.set()
        result.append(service.propose_calibration(doc, report))
        finished.set()

    original = service.perception.feed.poll
    worker = threading.Thread(target=propose)

    def poll(now):
        worker.start()
        assert entered.wait(2)
        assert not finished.is_set()
        assert service.rig.calibration_version == old
        return original(now)

    service.perception.feed.poll = poll
    site.tick()
    worker.join(5)
    assert result == [True]
    assert site.output[-1]["calibration_version"] == old
    service.perception.feed.poll = original
    site.tick()
    assert site.output[-1]["calibration_version"] == doc["calibration_version"]


@pytest.mark.parametrize("change, reason", [
    ("K", "내부 파라미터"), ("image_size", "내부 파라미터"), ("distortion", "내부 파라미터"),
    ("site", "site_id"), ("cameras", "카메라 집합"), ("hash", "content_hash"),
    ("holdout", "holdout"), ("improvement", "개선율"), ("alignment", "World 정렬"),
    ("rotation", "World 정렬"), ("missing", "검증 지표"), ("data", "최소 데이터"),
    ("stale", "기준 버전"), ("report_hash", "보고서"), ("state", "PROVISIONAL"),
    ("schema", "계약"), ("nan", "range"),
])
def test_rejected_candidates_keep_active_and_record_reason(site, change, reason, caplog):
    service = site.service
    old = service.rig
    doc, report = candidate(service)
    cid = doc["cameras"][0]["camera_id"]
    if change == "K":
        doc["cameras"][0]["K"][0][0] += 1
    elif change == "image_size":
        doc["cameras"][0]["image_size"][0] += 1
    elif change == "distortion":
        doc["cameras"][0]["distortion"]["coeffs"] = [0., 0., 0., 0., 0.]
        doc["cameras"][0]["distortion"]["model"] = "OPENCV_PINHOLE"
    elif change == "site":
        doc["coordinate_frame"]["site_id"] = "other-site"
    elif change == "cameras":
        doc["cameras"].pop()
    elif change == "holdout":
        report["holdout"]["after"]["reprojection_error_px"][cid]["p95"] = 6.
    elif change == "improvement":
        for v in report["holdout"]["before"]["reprojection_error_px"].values():
            v["median"] = 1.
    elif change == "alignment":
        report["world_alignment"]["anchor_center_residuals_cm"][cid] = 16.
    elif change == "rotation":
        report["world_alignment"]["anchor_rotation_residuals_deg"][cid] = 1.6
    elif change == "missing":
        report["holdout"]["after"]["reprojection_error_px"][cid]["median"] = None
    elif change == "data":
        report["data_sufficiency"]["counts"]["holdout_ticks"] = 14
    elif change == "stale":
        report["previous_version"] = "old-version"
    elif change == "report_hash":
        report["calibration"]["content_hash"] = "0" * 64
    elif change == "state":
        doc["state"] = "UNCALIBRATED"
    elif change == "schema":
        del doc["cameras"][0]["R"]
    elif change == "nan":
        report["holdout"]["after"]["reprojection_error_px"][cid]["median"] = float("nan")
    if change != "schema":
        doc["content_hash"] = content_hash(doc)
    if change == "hash":
        doc["content_hash"] = "0" * 64
    elif change not in ("report_hash", "schema"):
        report["calibration"]["content_hash"] = doc["content_hash"]
    assert not service.propose_calibration(doc, report)
    assert reason in service.calibration_status["last_rejection"]["reason"]
    assert "보정 후보 거부" in caplog.text
    site.tick()
    assert service.rig is old and service.calibration.current()["calibration_version"] == old.calibration_version
    assert service.health.payload()["system_health"] != "NOMINAL"


def test_notification_reliable_queue_full_and_replay_after_restart(site):
    service = site.service
    old = service.rig.calibration_version
    doc, report = candidate(service)
    assert service.propose_calibration(doc, report)
    site.tick()
    # 실제 I1 신뢰 대기열을 1칸으로 제한해 backpressure를 만든다(소켓은 열지 않는다).
    client = I1Client(SimpleNamespace(), on_message=lambda message: None, reliable_limit=1)
    publisher = I1Publisher(client, Envelope("tier0-test", service.clock), health_payload=lambda: None)
    service.link = SimpleNamespace(publisher=publisher)
    publisher.add_source(service._calibration_messages)
    client.enqueue(publisher.envelope.make("calibration_update", {}))
    publisher._drain_sources()
    assert len(publisher._carry) == 1
    client._reliable.clear()
    publisher._drain_sources()
    assert not publisher._carry
    _, message = client._reliable[0]
    assert schema_errors(message, "calibration_update") == []
    payload = message["payload"]
    assert payload["previous_calibration_version"] == old
    assert payload["effective_at"] == service.calibration_status["effective_at"]
    assert payload["calibration_state"] == "PROVISIONAL" and payload["reason"] == "RECALIBRATED"
    assert payload["calibration_hash"] == doc["content_hash"]
    assert payload["calibration_ref"].startswith("file:")
    assert json.loads(service.calibration.path(doc["calibration_version"]).read_text(encoding="utf-8")) == doc
    restored = RuntimeCalibrationStore(service.calibration.root, lambda: pytest.fail("저장된 head 우선"))
    assert restored.transitions[0]["msg_id"] == message["msg_id"]


def test_known_old_bundle_accepted_with_active_calibration_maintained(site):
    service = site.service
    doc, report = candidate(service)
    assert service.propose_calibration(doc, report)
    site.tick()
    old = service.calibration.document(service.calibration.transitions[0]["payload"]["previous_calibration_version"])
    notice, fetcher = ts.bundle_message(old, "cfg-new", service.config_sync.active.bundle.issued_at + 1)
    service.config_sync._verifier = BundleVerifier(fetcher, default_registry(), service.calibration.known, service.clock)
    result = service.config_sync.receive(notice)
    assert result.accepted
    site.tick()
    assert service.config_sync.active.config_version == "cfg-new"
    assert service.rig.calibration_version == doc["calibration_version"]
    assert service.health.payload()["config_sync"] == "STALE"
    # 활성 보정의 새 묶음이 오면 정상 CURRENT로 돌아가며 새 버전도 known이다.
    notice, fetcher = ts.bundle_message(doc, "cfg-current", service.config_sync.active.bundle.issued_at + 1)
    service.config_sync._verifier = BundleVerifier(fetcher, default_registry(), service.calibration.known, service.clock)
    service.config_sync.on_connected()
    assert service.config_sync.receive(notice).accepted
    site.tick()
    assert service.health.payload()["config_sync"] == "CURRENT"


def test_restart_does_not_activate_a_candidate_waiting_for_tick(site):
    service = site.service
    old = service.rig.calibration_version
    doc, report = candidate(service)
    assert service.propose_calibration(doc, report)
    restored = RuntimeCalibrationStore(service.calibration.root, lambda: pytest.fail("시작 파일을 읽지 않는다"))
    assert restored.current()["calibration_version"] == old
    assert not restored.known(doc["calibration_version"])
    assert restored.transitions == []


def test_health_rejection_degrades_without_contract_change():
    from test_tier0_health import _Clock, _msg, _reporter
    reporter = _reporter(_Clock())
    assert reporter.payload()["system_health"] == "NOMINAL"
    reporter._calibration_issue = lambda: True
    payload = reporter.payload()
    assert payload["system_health"] == "DEGRADED"
    assert schema_errors(_msg(payload), "health") == []


def test_proposal_before_start_is_rejected(site):
    service = Tier0Service(site.settings)
    assert not service.propose_calibration({}, {})
    assert service.calibration_status["last_rejection"] == {"at_ms": None, "reason": "실행 중인 서비스가 아니다"}


def test_restart_uses_last_active_even_if_bootstrap_missing(site):
    service = site.service
    doc, report = candidate(service)
    assert service.propose_calibration(doc, report)
    site.tick()
    assert service.stop()
    from dataclasses import replace
    settings = replace(site.settings, calibration_file=site.settings.state_dir / "missing.json")
    restarted = Tier0Service(settings, mono_ns=lambda: site.elapsed[0] * 1_000_000,
                             wall_ms=lambda: T0 + site.elapsed[0])
    restarted.start()
    try:
        assert restarted.rig.calibration_version == doc["calibration_version"]
        assert restarted.perception.associator.rig is restarted.rig
        assert restarted._calibration_state() == "PROVISIONAL"
        assert restarted.config_sync.active is not None  # 직전 보정의 구성 묶음도 복구한다
    finally:
        assert restarted.stop()


def test_persist_failure_keeps_all_references_old_and_retries(site, monkeypatch):
    service = site.service
    doc, report = candidate(service)
    assert service.propose_calibration(doc, report)
    old = service.rig
    with monkeypatch.context() as patch:
        patch.setattr(service.calibration, "_save", lambda head: (_ for _ in ()).throw(OSError("disk full")))
        site.tick()
    assert service.rig is service.perception.rig is service.perception.associator.rig is old
    assert service.calibration.current()["calibration_version"] == old.calibration_version
    assert service.calibration_status["last_rejection"] is not None
    site.tick()
    assert service.rig.calibration_version == doc["calibration_version"]


def test_corrupt_persistent_head_fails_closed(site):
    service = site.service
    assert service.stop()
    (service.calibration.root / "active.json").write_text("{", encoding="utf-8")
    restarted = Tier0Service(site.settings)
    with pytest.raises(Tier0StartError, match="보정 문서"):
        restarted.start()
