"""-467: 구성 묶음으로 받은 현장 세팅 보정의 Tier 0 채택(계보·content_hash·계약·출처 확인, 새 World 기준, 어긋나면 거부).

-745: 묶음이 세팅 「카메라 확인」 통과 근거(config_bundle 1.4.0 verification)를 실으면 활성 PROVISIONAL 세팅 보정을 CALIBRATED 로 올린다."""

import copy
import sys
from pathlib import Path

import pytest

pytest.importorskip("cv2")
pytest.importorskip("scipy")

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2]):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_calibration_swap as swap
import tier0_site as ts

from simai_ai.calibration.document import content_hash
from simai_ai.calibration.runtime import RuntimeCalibrationStore
from simai_ai.config import BundleVerifier
from simai_ai.risk import default_registry

# -344 시험의 실제 Tier 0 조립(시뮬레이션 녹화·tick 전진)을 그대로 쓴다
recording = swap.recording
site = swap.site
candidate = swap.candidate

SESSION = "0192a6f0-7c1e-7b3a-9f2d-000000000452"
T0_MS = 1_760_342_400_000


def setup_doc(service, version="cal-setup-1", *, previous=None, session=SESSION, source=None):
    old = service.calibration.current()
    doc = copy.deepcopy(old)
    doc["schema_version"] = "2.2.0"
    doc["calibration_version"] = version
    doc["state"] = "PROVISIONAL"
    if source is not None:
        doc["provider_source"] = source  # 현장 세팅 워커 출처(G1_AUTO). 시험 녹화의 시작 문서는 SIM 이다
    doc["cameras"][0]["t"][0] += 0.05  # 새 세팅의 자세(운영 중 후보 검증 문턱과 무관)
    if session is not None:
        doc["setup_session_id"] = session
    doc["previous_calibration_version"] = old["calibration_version"] if previous is None else previous
    doc["content_hash"] = content_hash(doc)
    return doc


def receive(service, doc, config_version, *, drop=None, verification=None):
    notice, fetcher = ts.bundle_message(doc, config_version, service.config_sync.active.bundle.issued_at + 1,
                                        calibration_verification=verification)
    if drop is not None:
        del fetcher.blobs[f"https://ec2.test/config/{config_version}/{drop}.json"]
    service.config_sync._verifier = BundleVerifier(fetcher, default_registry(), service.calibration.known, service.clock,
                                                   setup_calibration=service.calibration.setup_candidate_problem)
    return service.config_sync.receive(notice)


def test_setup_bundle_adopts_calibration_as_new_world_at_bundle_boundary(site):
    service = site.service
    store = service.calibration
    old = service.rig.calibration_version
    doc = setup_doc(service)
    assert receive(service, doc, "cfg-setup").accepted
    # 검증만으로는 저장·전환하지 않는다(판정 경계에서만)
    assert not store.known("cal-setup-1") and service.rig.calibration_version == old
    site.tick()
    assert service.config_sync.active.config_version == "cfg-setup"
    assert service.rig.calibration_version == "cal-setup-1" and store.current()["calibration_version"] == "cal-setup-1"
    payload = store.transitions[-1]["payload"]
    assert (payload["reason"], payload["previous_calibration_version"], payload["world_frame_id"]) == (
        "RECALIBRATED", old, "cal-setup-1")
    # 2회 연속·교체 뒤 감시 대상이 아니라 바로 기준. 전환 사실은 상태에 남긴다
    assert service._swap_probation is None
    adoption = service.calibration_status["setup_adoption"]
    assert (adoption["version"], adoption["previous_version"], adoption["setup_session_id"], adoption["config_version"]) == (
        "cal-setup-1", old, SESSION, "cfg-setup")
    acks = [a["payload"] for a in service.config_sync.take_acks()]
    assert any(a["config_version"] == "cfg-setup" and a["result"] == "APPLIED" for a in acks)
    # 재시작해도 새 World 계보가 기준이다(이전 World 문서는 safety 기준이 아니다)
    restored = RuntimeCalibrationStore(store.root, lambda: pytest.fail("저장소가 있어 시작 파일을 읽지 않는다"))
    assert restored.current()["calibration_version"] == "cal-setup-1"
    assert restored.scene_metadata()["world_frame_id"] == "cal-setup-1"
    assert restored.safety_reference()[0]["calibration_version"] == "cal-setup-1"
    assert restored.document(old)["calibration_version"] == old  # 이전 문서는 덮어쓰지 않는다


@pytest.mark.parametrize("case", ["hash", "lineage", "not_setup", "missing_document"])
def test_setup_bundle_rejected_keeps_previous_calibration_and_bundle(site, case):
    service = site.service
    old = service.rig.calibration_version
    active = service.config_sync.active.config_version
    doc = setup_doc(service, previous="cal-other" if case == "lineage" else None,
                    session=None if case == "not_setup" else SESSION)
    if case == "hash":
        doc["cameras"][0]["t"][0] += 0.01  # 내용을 바꾸고 content_hash 는 그대로
    result = receive(service, doc, "cfg-bad", drop="calibration" if case == "missing_document" else None)
    assert not result.accepted
    assert result.reason == ("DOWNLOAD_FAILED" if case == "missing_document" else "REFERENCE_INVALID")
    site.tick()
    assert service.rig.calibration_version == old and not service.calibration.known("cal-setup-1")
    assert service.config_sync.active.config_version == active
    assert "setup_adoption" not in service.calibration_status


def test_setup_bundle_rejected_at_boundary_when_active_calibration_changed_after_verify(site):
    service = site.service
    old = service.rig.calibration_version
    active = service.config_sync.active.config_version
    assert receive(service, setup_doc(service), "cfg-setup").accepted
    # 검증 뒤 같은 경계 전에 운영 중 후보가 활성 보정을 바꾸면 계보(previous)가 어긋나 적용하지 않는다
    rotation, report = candidate(service)
    assert service.propose_calibration(rotation, report)
    site.tick()
    assert service.rig.calibration_version == rotation["calibration_version"] != old
    assert not service.calibration.known("cal-setup-1")
    assert service.config_sync.active.config_version == active
    rejected = [a["payload"] for a in service.config_sync.take_acks() if a["payload"]["config_version"] == "cfg-setup"]
    assert rejected and rejected[-1]["result"] == "REJECTED" and rejected[-1]["reject_reason"] == "REFERENCE_INVALID"


def passed(version="cal-setup-1", session=SESSION):
    """EC2 가 싣는 세팅 「카메라 확인」 통과 근거(config_bundle 1.4.0)."""
    return {"camera_check": "PASSED", "calibration_version": version, "setup_session_id": session}


def assert_calibrated(service):
    from simai_ai.calibration.state import operation_for
    from simai_ai.tracking.system_health import SystemHealth

    assert service.calibration.state == "CALIBRATED" and service._calibration_state() == "CALIBRATED"
    assert service.judge._active_calibration_state() == "CALIBRATED"  # 판정 문맥: R2·R3·R4 에 PROVISIONAL_CALIB 를 붙이지 않는다
    op = operation_for(service._calibration_state())
    assert op.system_health is SystemHealth.NOMINAL and not op.needs_confirmation_rules and not op.r1_wide_uncertainty
    assert service.health.payload()["calibration_state"] == "CALIBRATED"


def test_verified_setup_bundle_adopts_and_marks_calibrated(site):
    """카메라 확인 통과 세팅 → 세팅 묶음(근거 포함) → 채택(RECALIBRATED, 문서 PROVISIONAL) 뒤 STATE_CHANGED CALIBRATED."""
    service = site.service
    store = service.calibration
    old = service.rig.calibration_version
    doc = setup_doc(service, source="G1_AUTO")
    assert receive(service, doc, "cfg-setup", verification=passed()).accepted
    assert store.state != "CALIBRATED"  # 검증만으로는 바꾸지 않는다(판정 경계에서만)
    site.tick()
    assert service.rig.calibration_version == "cal-setup-1"
    assert store.current()["state"] == "PROVISIONAL"  # 문서는 동결(덮어쓰지 않는다)
    adopted, verified = (r["payload"] for r in store.transitions[-2:])
    assert (adopted["reason"], adopted["calibration_state"]) == ("RECALIBRATED", "PROVISIONAL")
    assert (verified["reason"], verified["calibration_state"], verified["calibration_version"]) == (
        "STATE_CHANGED", "CALIBRATED", "cal-setup-1")
    assert verified["previous_calibration_version"] == old and verified["world_frame_id"] == "cal-setup-1"
    assert verified["calibration_hash"] == doc["content_hash"]
    assert store.transitions[-1]["verification"] == passed()
    assert_calibrated(service)
    # calibration_update 로 EC2 에 간다(계약 검사)
    from simai_ai.providers.contract import schema_violations

    message = {"schema_version": "2.1.0", "msg_type": "calibration_update", "msg_id": store.transitions[-1]["msg_id"],
               "sent_at": T0_MS, "source_id": "tier0-test", "payload": verified}
    assert schema_violations(message, "calibration_update") == []
    # 재시작해도 이력에서 CALIBRATED 를 읽는다
    restored = RuntimeCalibrationStore(store.root, lambda: pytest.fail("저장소가 있어 시작 파일을 읽지 않는다"))
    assert restored.state == "CALIBRATED"
    # 같은 근거를 다시 받아도(EC2 재전송·다음 묶음) 전환을 되풀이하지 않는다
    count = len(store.transitions)
    assert receive(service, doc, "cfg-zones", verification=passed()).accepted
    site.tick()
    assert service.config_sync.active.config_version == "cfg-zones" and len(store.transitions) == count


@pytest.mark.parametrize("case", ["none", "other_version", "sim_source"])
def test_setup_bundle_without_valid_verification_stays_provisional(site, case):
    """재생 시험 걷기 생략(SKIPPED_REPLAY)·기준선 없는 PASSED 는 EC2 가 근거를 싣지 않는다. 근거가 어긋나도 올리지 않는다."""
    service = site.service
    doc = setup_doc(service, source=None if case == "sim_source" else "G1_AUTO")
    verification = {"none": None, "other_version": passed("cal-other"), "sim_source": passed()}[case]
    assert receive(service, doc, "cfg-setup", verification=verification).accepted
    site.tick()
    assert service.rig.calibration_version == "cal-setup-1"
    assert service.calibration.state == "PROVISIONAL" and service._calibration_state() == "PROVISIONAL"
    assert service.calibration.transitions[-1]["payload"]["reason"] == "RECALIBRATED"


def test_later_bundle_with_verification_marks_known_calibration(site):
    """GPU 재기동 경로: 보정은 이미 저장소에 있고(시작 묶음·이전 채택) EC2 가 다시 보낸 최근 묶음에 근거가 있으면 그때 올린다."""
    service = site.service
    doc = setup_doc(service, source="G1_AUTO")
    assert receive(service, doc, "cfg-setup").accepted
    site.tick()
    assert service.calibration.state == "PROVISIONAL"
    assert receive(service, doc, "cfg-resent", verification=passed()).accepted
    site.tick()
    assert service.config_sync.active.config_version == "cfg-resent"
    assert service.calibration.transitions[-1]["payload"]["reason"] == "STATE_CHANGED"
    assert_calibrated(service)


def test_drifted_calibration_is_not_promoted(site):
    """틀어짐이 확인된(DRIFTED) 보정은 재세팅 전까지 근거가 와도 올리지 않는다(명세 4.4)."""
    service = site.service
    doc = setup_doc(service, source="G1_AUTO")
    assert receive(service, doc, "cfg-setup").accepted
    site.tick()
    service.calibration._state = "DRIFTED"  # report_drift 가 남기는 운영 상태(전환 이력 저장은 test_drift_watch 가 본다)
    assert service.calibration.verification_problem(passed()) == "운영 상태 DRIFTED 는 올리지 않는다"
    count = len(service.calibration.transitions)
    assert receive(service, doc, "cfg-resent", verification=passed()).accepted
    site.tick()
    assert service.calibration.state == "DRIFTED" and len(service.calibration.transitions) == count


def test_restart_with_restored_bundle_promotes_once_at_startup(site, monkeypatch):
    """복원 묶음은 적용 경계를 거치지 않고 같은 config_version 재전송도 단락된다. 기록 실패 뒤 재기동하면 기동 때 한 번 올린다."""
    from services.tier0.service import Tier0Service

    service = site.service
    doc = setup_doc(service, source="G1_AUTO")
    assert receive(service, doc, "cfg-setup", verification=passed()).accepted
    with monkeypatch.context() as patch:
        patch.setattr(service.calibration, "mark_verified", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        site.tick()
    # 기록 실패는 묶음 적용을 막지 않고 PROVISIONAL(확인 필요)로 남는다
    assert service.config_sync.active.config_version == "cfg-setup"
    assert service.calibration.state == "PROVISIONAL"
    assert service.stop()
    restarted = Tier0Service(site.settings, mono_ns=lambda: site.elapsed[0] * 1_000_000,
                             wall_ms=lambda: T0_MS + site.elapsed[0])
    restarted.start()
    try:
        assert restarted.config_sync.active.config_version == "cfg-setup"  # 복원 묶음
        assert restarted.rig.calibration_version == "cal-setup-1"
        assert restarted.calibration.transitions[-1]["payload"]["reason"] == "STATE_CHANGED"
        assert restarted._calibration_state() == "CALIBRATED"
    finally:
        assert restarted.stop()
