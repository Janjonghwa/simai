"""구성 묶음 검증: 스키마·내용 해시·내용 형식·참조 무결성 (명세 3.2, FR-83)."""

import copy

import pytest

from simai_ai.config import BundleRejected, BundleVerifier, RejectReason, calibration_known_from
from simai_ai.config.verify import notice_header
from simai_ai.risk import RuleSpec, default_registry


def reject_reason(kit, message):
    with pytest.raises(BundleRejected) as info:
        kit.verifier().verify(message)
    return info.value


def test_valid_bundle_builds_frozen_config(kit):
    message = kit.publish("cfg-13")
    bundle = kit.verifier().verify(message)
    assert bundle.config_version == "cfg-13"
    assert bundle.bundle_hash == message["payload"]["bundle_hash"]
    assert bundle.zones_version == "zones-cfg-13"
    assert [z.zone_version for z in bundle.zones] == ["zone-cfg-13"]
    assert bundle.rule_versions == (("R1", 1), ("R7", 1))
    assert bundle.calibration_version == "cal-cfg-13"
    assert bundle.geometry_version == "geom-cfg-13"
    assert bundle.verified_at == kit.clock.now_ms()
    with pytest.raises(TypeError):
        bundle.operational_geometry["version"] = "x"  # 읽기 전용
    with pytest.raises(TypeError):
        bundle.notice.message["payload"]["config_version"] = "x"


def test_schema_invalid_message(kit):
    message = kit.publish("cfg-13")
    del message["payload"]["components"]["zones"]
    assert reject_reason(kit, message).reason is RejectReason.SCHEMA_INVALID


def test_manifest_hash_mismatch(kit):
    error = reject_reason(kit, kit.publish("cfg-13", tamper="manifest"))
    assert error.reason is RejectReason.HASH_MISMATCH
    # ACK 의 bundle_hash 는 Tier 0 가 계산한 해시다
    assert error.bundle_hash is not None


@pytest.mark.parametrize("component", ["zones", "rules", "calibration", "operational_geometry"])
def test_component_hash_mismatch(kit, component):
    assert reject_reason(kit, kit.publish("cfg-13", tamper=component)).reason is RejectReason.HASH_MISMATCH


@pytest.mark.parametrize("which", ["manifest", "zones"])
def test_download_failure(kit, which):
    message = kit.publish("cfg-13")
    uri = message["payload"]["bundle_uri"] if which == "manifest" else message["payload"]["components"]["zones"]["uri"]
    kit.fetcher.fail.add(uri)
    assert reject_reason(kit, message).reason is RejectReason.DOWNLOAD_FAILED


def test_manifest_disagrees_with_notice(kit):
    def patch(manifest):
        manifest["components"]["zones"]["version"] = "zones-other"
        return manifest

    assert reject_reason(kit, kit.publish("cfg-13", manifest_patch=patch)).reason is RejectReason.REFERENCE_INVALID


def test_manifest_for_other_config_version(kit):
    def patch(manifest):
        manifest["config_version"] = "cfg-99"
        return manifest

    assert reject_reason(kit, kit.publish("cfg-13", manifest_patch=patch)).reason is RejectReason.REFERENCE_INVALID


def test_unknown_rule_version(kit):
    error = reject_reason(kit, kit.publish("cfg-13", rule_versions=(("R1", 99),)))
    assert error.reason is RejectReason.REFERENCE_INVALID
    assert "R1 v99" in error.detail


class _R1v2:
    spec = RuleSpec(rule_id="R1", rule_version=2, reads=())

    def evaluate(self, ctx):
        return ()


def test_duplicate_rule_id(kit):
    registry = default_registry()
    registry.register("R1", 2, _R1v2)
    verifier = BundleVerifier(kit.fetcher, registry, kit.known, kit.clock)
    with pytest.raises(BundleRejected) as info:
        verifier.verify(kit.publish("cfg-13", rule_versions=(("R1", 1), ("R1", 2))))
    assert info.value.reason is RejectReason.REFERENCE_INVALID


def test_rules_content_disagrees_with_notice(kit):
    doc = {"version": "rules-cfg-13", "rule_versions": [{"rule_id": "R7", "rule_version": 1}]}
    error = reject_reason(kit, kit.publish("cfg-13", rules_doc=doc))
    assert error.reason is RejectReason.REFERENCE_INVALID


def test_calibration_version_missing_from_store(kit):
    error = reject_reason(kit, kit.publish("cfg-13", register_calibration=False))
    assert error.reason is RejectReason.REFERENCE_INVALID
    assert "보정" in error.detail


def test_content_version_differs_from_reference(kit):
    # 알림·매니페스트는 geom-y 를 가리키지만 내려받은 내용은 geom-cfg-13 이다(해시는 맞다)
    error = reject_reason(kit, _with_component_version(kit, "cfg-13", "operational_geometry", "geom-y"))
    assert error.reason is RejectReason.REFERENCE_INVALID


def _with_component_version(kit, version, name, ref_version):
    def patch(manifest):
        manifest["components"][name]["version"] = ref_version
        return manifest

    message = kit.publish(version, manifest_patch=patch)
    message["payload"]["components"][name]["version"] = ref_version
    return message


def test_overlapping_zone_versions(kit):
    zones = [
        {
            "zone_id": "z-1",
            "zone_version": "a",
            "judge_type": "GROUND_FOOTPRINT",
            "polygon_xy": [[0, 0], [1, 0], [1, 1]],
            "z_range": None,
            "severity": "HIGH",
            "valid_from": 0,
            "valid_to": 100,
        },
        {
            "zone_id": "z-1",
            "zone_version": "b",
            "judge_type": "GROUND_FOOTPRINT",
            "polygon_xy": [[0, 0], [1, 0], [1, 1]],
            "z_range": None,
            "severity": "HIGH",
            "valid_from": 50,
        },
    ]
    assert reject_reason(kit, kit.publish("cfg-13", zones=zones)).reason is RejectReason.REFERENCE_INVALID
    zones[1]["valid_from"] = 100  # 이어지는 구간은 통과
    assert kit.verifier().verify(kit.publish("cfg-14", zones=zones)).config_version == "cfg-14"


def test_zone_format_error(kit):
    zones = [{"zone_id": "z-1"}]
    assert reject_reason(kit, kit.publish("cfg-13", zones=zones)).reason is RejectReason.SCHEMA_INVALID


def test_duplicate_work_surface_ids(kit):
    geometry = {"version": "geom-cfg-13", "work_surfaces": [{"surface_id": "d"}, {"surface_id": "d"}]}
    assert reject_reason(kit, kit.publish("cfg-13", geometry=geometry)).reason is RejectReason.REFERENCE_INVALID


def test_non_standard_json_rejected(kit):
    message = kit.publish("cfg-13")
    comp = message["payload"]["components"]["operational_geometry"]
    data = b'{"version": "geom-cfg-13", "work_surfaces": [{"surface_id": "d", "top_height_m": NaN}]}'
    _replace_blob(kit, message, "operational_geometry", data)
    assert comp["uri"] in kit.fetcher.blobs
    assert reject_reason(kit, message).reason is RejectReason.SCHEMA_INVALID


def _replace_blob(kit, message, name, data):
    """구성 요소 바이트를 바꾸고 해시(알림·매니페스트)를 맞춰 다시 올린다. 형식 검사만 실패하게 한다."""
    import json

    from simai_ai.config import sha256_of

    payload = message["payload"]
    payload["components"][name]["sha256"] = sha256_of(data)
    kit.fetcher.blobs[payload["components"][name]["uri"]] = data
    manifest = {
        "config_version": payload["config_version"],
        "issued_at": payload["issued_at"],
        "components": payload["components"],
    }
    manifest_bytes = json.dumps(manifest).encode()
    kit.fetcher.blobs[payload["bundle_uri"]] = manifest_bytes
    payload["bundle_hash"] = sha256_of(manifest_bytes)


def test_calibration_schema_invalid(kit):
    import json

    message = kit.publish("cfg-13")
    doc = copy.deepcopy(json.loads(kit.fetcher.blobs[message["payload"]["components"]["calibration"]["uri"]]))
    del doc["cameras"]
    _replace_blob(kit, message, "calibration", json.dumps(doc).encode())
    assert reject_reason(kit, message).reason is RejectReason.SCHEMA_INVALID


def test_notice_header_only_when_valid():
    assert (
        notice_header({"payload": {"config_version": "c", "bundle_hash": "sha256:" + "a" * 64}}).config_version == "c"
    )
    header = notice_header({"payload": {"config_version": "", "bundle_hash": "x"}})
    assert header.config_version is None and header.bundle_hash is None
    assert notice_header("not a message").config_version is None


def test_calibration_known_from_store():
    class Store:
        def versions(self):
            return ({"calibration_version": "cal-1"}, {"calibration_version": "cal-2"})

    known = calibration_known_from(Store())
    assert known("cal-2") and not known("cal-3")
