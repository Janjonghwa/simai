"""-467: 구성 묶음으로 받은 현장 세팅 보정의 Tier 0 채택(계보·content_hash·계약·출처 확인, 새 World 기준, 어긋나면 거부)."""

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


def setup_doc(service, version="cal-setup-1", *, previous=None, session=SESSION):
    old = service.calibration.current()
    doc = copy.deepcopy(old)
    doc["schema_version"] = "2.2.0"
    doc["calibration_version"] = version
    doc["state"] = "PROVISIONAL"
    doc["cameras"][0]["t"][0] += 0.05  # 새 세팅의 자세(운영 중 후보 검증 문턱과 무관)
    if session is not None:
        doc["setup_session_id"] = session
    doc["previous_calibration_version"] = old["calibration_version"] if previous is None else previous
    doc["content_hash"] = content_hash(doc)
    return doc


def receive(service, doc, config_version, *, drop=None):
    notice, fetcher = ts.bundle_message(doc, config_version, service.config_sync.active.bundle.issued_at + 1)
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
