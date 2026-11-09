"""구성 묶음 선택 구성 요소 ppe_policy (명세 7.2 R9·R11, 12.3, 계약 config_bundle 1.3.0·data/ppe_policy 1.1.0, S15P31A101-519·-548).

- 없으면 통과하고 요구 없음(ConfigBundle.ppe_policy None).
- 실려 있으면 다른 구성 요소처럼 내려받아 해시·계약 스키마·version 참조를 검사한다. 틀리면 묶음 전체를 거부하고 이전 묶음을 쓴다.
- 보관한 묶음을 재시작 때 다시 검증해도 ppe_policy 가 그대로 돌아온다.
"""

import pytest

from simai_ai.config import BundleArchive, BundleRejected, ConfigSync, RejectReason
from simai_ai.risk import HelmetRequirement, default_registry

WORK = {"format_version": "1.0.0", "version": "ppe-3", "work_id": 7, "effective_from_ms": 1760342000000, "helmet": "REQUIRED"}
NO_WORK = {"format_version": "1.0.0", "version": "ppe-4", "work_id": None, "effective_from_ms": 1760342400350,
           "helmet": "NOT_REQUIRED"}


def rejected(kit, message):
    with pytest.raises(BundleRejected) as info:
        kit.verifier().verify(message)
    return info.value


def test_bundle_without_ppe_policy_means_no_requirement(kit):
    bundle = kit.verifier().verify(kit.publish("cfg-13"))
    assert bundle.ppe_policy is None


@pytest.mark.parametrize("doc", [WORK, NO_WORK])
def test_ppe_policy_is_verified_and_kept(kit, doc):
    message = kit.publish("cfg-14", ppe_policy=doc)
    bundle = kit.verifier().verify(message)
    assert bundle.ppe_policy is not None
    assert bundle.ppe_policy.version == doc["version"]
    assert bundle.ppe_policy.work_id == doc["work_id"]
    assert bundle.ppe_policy.helmet is HelmetRequirement(doc["helmet"])
    assert bundle.ppe_policy.helmet_required is (doc["helmet"] == "REQUIRED")
    assert message["payload"]["components"]["ppe_policy"]["sha256"] in bundle.blobs  # 보관할 원본 바이트


WORK_VEST = WORK | {"format_version": "1.1.0", "vest": "REQUIRED"}
NO_WORK_VEST = NO_WORK | {"format_version": "1.1.0", "vest": "NOT_REQUIRED"}


@pytest.mark.parametrize("doc", [WORK_VEST, NO_WORK_VEST])
def test_ppe_policy_1_1_vest_is_verified_and_kept(kit, doc):
    """1.1.0 조끼 요구(-548)도 같은 검증을 거쳐 판정 값으로 온다."""
    bundle = kit.verifier().verify(kit.publish("cfg-14", ppe_policy=doc))
    assert bundle.ppe_policy is not None
    assert bundle.ppe_policy.vest_required is (doc["vest"] == "REQUIRED")


@pytest.mark.parametrize(
    "doc",
    [
        WORK | {"helmet": "NOT_REQUIRED"},  # 작업 중인데 요구 없음(스키마 allOf)
        NO_WORK | {"helmet": "REQUIRED"},  # 작업이 없는데 요구
        WORK | {"vest": "REQUIRED"},  # vest 는 1.1.0 부터(1.0.0 문서에 싣지 않는다)
        WORK_VEST | {"vest": "NOT_REQUIRED"},  # 작업 중인데 조끼 요구 없음(안전모와 같은 규칙)
        NO_WORK_VEST | {"vest": "REQUIRED"},  # 작업이 없는데 조끼 요구
        WORK | {"zone_overrides": []},  # 구역별 요구·예외는 두지 않는다
        {k: v for k, v in WORK.items() if k != "work_id"},
        WORK | {"helmet": True},
    ],
)
def test_malformed_ppe_policy_rejects_whole_bundle(kit, doc):
    assert rejected(kit, kit.publish("cfg-14", ppe_policy=doc)).reason is RejectReason.SCHEMA_INVALID


def test_ppe_policy_version_must_match_reference(kit):
    """내용의 version(ppe-3)이 알림·매니페스트가 참조한 version(ppe-9)과 다르면 참조 무결성 위반이다."""

    def patch(manifest):
        manifest["components"]["ppe_policy"]["version"] = "ppe-9"
        return manifest

    message = kit.publish("cfg-15", ppe_policy=WORK, manifest_patch=patch)
    message["payload"]["components"]["ppe_policy"]["version"] = "ppe-9"
    assert rejected(kit, message).reason is RejectReason.REFERENCE_INVALID


def test_ppe_policy_hash_mismatch(kit):
    message = kit.publish("cfg-14", ppe_policy=WORK, tamper="ppe_policy")
    assert rejected(kit, message).reason is RejectReason.HASH_MISMATCH


def test_ppe_policy_download_failure(kit):
    message = kit.publish("cfg-14", ppe_policy=WORK)
    kit.fetcher.fail.add(message["payload"]["components"]["ppe_policy"]["uri"])
    assert rejected(kit, message).reason is RejectReason.DOWNLOAD_FAILED


def test_rejected_ppe_policy_keeps_previous_bundle(kit):
    """형식이 틀린 ppe_policy 는 묶음 전체를 거부하고 이전 묶음(그 요구)을 계속 쓴다(명세 7.2 R9 67행)."""
    sync = ConfigSync(kit.verifier(), kit.clock, source_id="tier0-01")
    sync.on_connected()
    assert sync.receive(kit.publish("cfg-13", ppe_policy=WORK)).accepted
    kit.clock.advance(10)
    assert sync.begin_tick().bundle.ppe_policy.version == "ppe-3"
    result = sync.receive(kit.publish("cfg-14", ppe_policy=NO_WORK | {"helmet": "REQUIRED"}))
    assert result.reason is RejectReason.SCHEMA_INVALID
    kit.clock.advance(10)
    active = sync.begin_tick()
    assert active.config_version == "cfg-13"
    assert active.bundle.ppe_policy.helmet_required


def test_archive_restores_ppe_policy(kit, tmp_path):
    archive = BundleArchive(tmp_path)
    bundle = kit.verifier().verify(kit.publish("cfg-14", ppe_policy=WORK))
    archive.save(bundle)
    kit.fetcher.blobs.clear()
    restored = archive.restore("cfg-14", registry=default_registry(), calibration_known=kit.known, clock=kit.clock)
    assert restored.ppe_policy == bundle.ppe_policy
