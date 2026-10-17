"""구성 묶음 형식 (명세 3.2 구성 묶음, 11.1 config_bundle, 7.5 작업면).

구성 묶음 = 구역, 규칙 버전, 보정 버전, Operational Geometry(바닥 평면·등록 정적 객체·작업면 OBB), 각 버전 ID 와 해시.
EC2 가 보내는 알림(I1 `config_bundle`)의 정본은 `contracts/messages/config_bundle.schema.json` 이다. 알림은 매니페스트 주소
(bundle_uri)와 해시만 싣고, 내려받는 구성 요소의 내용 형식은 계약 범위 밖이다. 그래서 아래 내용 형식은 이 패키지의 임시
형식이고, 계약이 생기면 그 형식으로 바꾼다(MR 초안 '남은 일').

| 문서 | 임시 형식 |
|---|---|
| 매니페스트(bundle_uri) | `{"config_version", "previous_config_version"?, "issued_at"?, "components"}`. components 는 알림의 components 와 같다 |
| zones | `{"version", "zones": [zone, ...]}`. zone 은 `simai_ai.risk.zone.Zone.from_json` 형식 |
| rules | `{"version", "rule_versions": [{"rule_id", "rule_version"}, ...]}`. 알림의 rule_versions 와 같아야 한다 |
| calibration | `contracts/providers/calibration.schema.json` 보정 문서. calibration_version 이 구성 요소 version 과 같다 |
| operational_geometry | `{"version", "work_surfaces"?: [{"surface_id", ...}], "static_objects"?: [{"object_id", ...}]}`. 나머지 필드는 그대로 보관 |

검증을 통과한 묶음(ConfigBundle)은 읽기 전용이다. 묶음은 덮어쓰지 않고 새 config_version 으로만 바뀐다(AGENTS 규칙 6).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from simai_ai.risk import Rule, Zone

COMPONENT_NAMES = ("zones", "rules", "calibration", "operational_geometry")
"""구성 요소 이름(계약 config_bundle.payload.components 의 필수 키)"""


def sha256_of(data: bytes) -> str:
    """계약 sha256 형식(`sha256:<hex 64>`)의 내용 해시."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def freeze_json(value: Any) -> Any:
    """JSON 값을 읽기 전용으로 바꾼다(dict → MappingProxyType, list → tuple). 보관한 묶음이 바뀌지 않게 한다."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze_json(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_json(v) for v in value)
    return value


def thaw_json(value: Any) -> Any:
    """freeze_json 의 반대(JSON 직렬화·비교용 사본)."""
    if isinstance(value, Mapping):
        return {k: thaw_json(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(v) for v in value]
    return value


@dataclass(frozen=True)
class ComponentRef:
    """알림·매니페스트의 구성 요소 참조(version, sha256, uri)."""

    name: str
    version: str
    sha256: str
    uri: str


@dataclass(frozen=True)
class BundleNotice:
    """I1 config_bundle 알림의 payload 중 검증·ACK 에 쓰는 값. 스키마 검사를 통과한 메시지에서만 만든다."""

    msg_id: str
    config_version: str
    previous_config_version: str | None
    issued_at: int
    bundle_uri: str
    bundle_hash: str
    components: Mapping[str, ComponentRef]
    rule_versions: tuple[tuple[str, int], ...]
    message: Mapping[str, Any] = field(hash=False, repr=False)  # 받은 메시지 원본(읽기 전용, 보관용)

    @classmethod
    def from_message(cls, message: Mapping[str, Any]) -> BundleNotice:
        payload = message["payload"]
        comps = payload["components"]
        refs = {
            name: ComponentRef(name, comps[name]["version"], comps[name]["sha256"], comps[name]["uri"])
            for name in COMPONENT_NAMES
        }
        rule_versions = tuple((r["rule_id"], r["rule_version"]) for r in comps["rules"]["rule_versions"])
        return cls(
            msg_id=message["msg_id"],
            config_version=payload["config_version"],
            previous_config_version=payload.get("previous_config_version"),
            issued_at=payload["issued_at"],
            bundle_uri=payload["bundle_uri"],
            bundle_hash=payload["bundle_hash"],
            components=MappingProxyType(refs),
            rule_versions=rule_versions,
            message=freeze_json(message),
        )


@dataclass(frozen=True)
class ConfigBundle:
    """검증을 통과한 구성 묶음. 판정 한 번(tick)은 이 묶음 하나만 쓴다.

    - zones: 구역 버전들(판정 시각에 유효한 버전만 엔진이 고른다). 같은 구역 ID 의 유효 구간은 겹치지 않는다(검증).
    - rules: 등록부에서 만든 규칙(규칙 ID 당 한 버전). 규칙은 상태가 없어 묶음끼리 공유해도 된다.
    - calibration_version: 이 묶음이 기준으로 삼는 보정 버전(Tier 0 보정 저장소에 있는 것만 통과).
    - operational_geometry: 작업면·정적 객체(읽기 전용 JSON). R2·R3·ContactEstimator 가 쓸 형식은 후속 이슈에서 정한다.
    - blobs: 내려받은 원본 바이트(해시 → 바이트). 보관소(archive)가 그대로 저장해 재시작 때 다시 검증한다.
    """

    notice: BundleNotice
    bundle_hash: str  # Tier 0 가 내려받아 계산한 매니페스트 해시
    zones_version: str
    zones: tuple[Zone, ...]
    rules_version: str
    rules: tuple[Rule, ...]
    calibration_version: str
    geometry_version: str
    operational_geometry: Mapping[str, Any] = field(hash=False)
    verified_at: int = 0
    blobs: Mapping[str, bytes] = field(default_factory=lambda: MappingProxyType({}), hash=False, repr=False)

    @property
    def config_version(self) -> str:
        return self.notice.config_version

    @property
    def issued_at(self) -> int:
        return self.notice.issued_at

    @property
    def rule_versions(self) -> tuple[tuple[str, int], ...]:
        return tuple(r.spec.key for r in self.rules)
