"""구성 묶음 시험 공용 도구: 메모리 객체 저장소(DictFetcher)와 묶음 발행기(BundleKit).

발행기는 EC2 처럼 구성 요소 JSON → 매니페스트 → config_bundle 알림을 만들고 바이트를 저장소에 올린다.
구역 버전·보정 버전에 config_version 을 넣어 두어, 판정 tick 이 한 묶음만 쓰는지(반쪽 적용 없음) 값으로 확인할 수 있다.
"""

import copy
import json
import uuid
from pathlib import Path

import pytest

from simai_ai.config import BundleVerifier, FetchError, sha256_of
from simai_ai.risk import FixedClock, default_registry

T0 = 1_760_342_400_000
CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
CALIBRATION_EXAMPLE = json.loads((CONTRACTS / "examples" / "calibration.json").read_text(encoding="utf-8"))

ZONE = {
    "zone_id": "z-04",
    "zone_version": "zone-1",
    "judge_type": "GROUND_FOOTPRINT",
    "polygon_xy": [[3.0, 4.5], [4.0, 4.5], [4.0, 5.5], [3.0, 5.5]],
    "z_range": None,
    "severity": "HIGH",
}


def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")


class DictFetcher:
    """uri → 바이트 메모리 저장소. fail 에 넣은 uri 는 내려받기 실패."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.fail: set[str] = set()

    def fetch(self, uri):
        if uri in self.fail or uri not in self.blobs:
            raise FetchError("없음")
        return self.blobs[uri]


class BundleKit:
    def __init__(self):
        self.fetcher = DictFetcher()
        self.clock = FixedClock(T0)
        self.calibrations: set[str] = set()

    def known(self, version):
        return version in self.calibrations

    def verifier(self):
        return BundleVerifier(self.fetcher, default_registry(), self.known, self.clock)

    def publish(
        self,
        version,
        issued_at=None,
        *,
        zones=None,
        rule_versions=(("R1", 1), ("R7", 1)),
        rules_doc=None,
        calibration_version=None,
        register_calibration=True,
        geometry=None,
        manifest_patch=None,
        tamper=None,
        ppe_policy=None,
    ):
        """묶음 하나를 발행하고 알림 메시지를 돌려준다.

        tamper: 올린 뒤 바꿀 구성 요소 이름 또는 "manifest"(해시가 어긋나게 된다).
        ppe_policy: 선택 구성 요소 ppe_policy 의 내용 문서(config_bundle 1.3.0). None 이면 싣지 않는다.
        """
        issued_at = self.clock.now_ms() if issued_at is None else issued_at
        base = f"s3://simai-site/config/{version}"
        cal_version = calibration_version or f"cal-{version}"
        if register_calibration:
            self.calibrations.add(cal_version)
        docs = {
            "zones": {
                "version": f"zones-{version}",
                "zones": [ZONE | {"zone_version": f"zone-{version}"}] if zones is None else zones,
            },
            "rules": rules_doc
            or {
                "version": f"rules-{version}",
                "rule_versions": [{"rule_id": r, "rule_version": v} for r, v in rule_versions],
            },
            "calibration": copy.deepcopy(CALIBRATION_EXAMPLE) | {"calibration_version": cal_version},
            "operational_geometry": geometry
            or {
                "version": f"geom-{version}",
                "work_surfaces": [{"surface_id": "desk-1", "top_height_m": 0.74}],
                "static_objects": [],
            },
        }
        if ppe_policy is not None:
            docs["ppe_policy"] = ppe_policy
        components = {}
        for name, doc in docs.items():
            data = dumps(doc)
            uri = f"{base}/{name}.json"
            self.fetcher.blobs[uri] = data
            component = {
                "version": doc.get("version") or doc.get("calibration_version"),
                "sha256": sha256_of(data),
                "uri": uri,
            }
            if name == "rules":
                component["rule_versions"] = [{"rule_id": r, "rule_version": v} for r, v in rule_versions]
            components[name] = component
        manifest = {"config_version": version, "issued_at": issued_at, "components": components}
        if manifest_patch:
            manifest = manifest_patch(copy.deepcopy(manifest))
        manifest_bytes = dumps(manifest)
        bundle_uri = f"{base}/manifest.json"
        self.fetcher.blobs[bundle_uri] = manifest_bytes
        if tamper == "manifest":
            self.fetcher.blobs[bundle_uri] = manifest_bytes + b" "
        elif tamper is not None:
            self.fetcher.blobs[f"{base}/{tamper}.json"] += b" "
        return {
            "schema_version": "2.0.0",
            "msg_type": "config_bundle",
            "msg_id": str(uuid.uuid4()),
            "sent_at": issued_at + 1,
            "source_id": "ec2",
            "payload": {
                "config_version": version,
                "previous_config_version": None,
                "issued_at": issued_at,
                "bundle_uri": bundle_uri,
                "bundle_hash": sha256_of(manifest_bytes),
                "components": components,
            },
        }


@pytest.fixture
def kit():
    return BundleKit()
