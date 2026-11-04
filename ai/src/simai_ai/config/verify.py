"""구성 묶음 검증: 스키마 → 내려받기 → 내용 해시 → 내용 형식 → 참조 무결성 (명세 3.2, FR-83).

검증은 판정 스레드 밖(I1 수신 쪽)에서 돈다. 실패하면 묶음을 만들지 않고 거부 사유(계약 config_ack.reject_reason)를
돌려준다. 거부된 묶음은 판정에 들어가지 않으므로 Tier 0 는 마지막 검증 묶음으로 계속 판정한다.

| 단계 | 실패 사유 |
|---|---|
| 알림 메시지를 `contracts/messages/config_bundle.schema.json` 으로 검증 | SCHEMA_INVALID |
| 매니페스트·구성 요소 내려받기(Fetcher) | DOWNLOAD_FAILED |
| 매니페스트 해시 = bundle_hash, 구성 요소 해시 = components.*.sha256 | HASH_MISMATCH |
| JSON(엄격: NaN·무한대 금지) 과 내용 형식(bundle 모듈 표), 보정 문서는 calibration 스키마 | SCHEMA_INVALID |
| 참조 무결성(아래) | REFERENCE_INVALID |

참조 무결성:
- 매니페스트의 config_version·components 가 알림과 같다(알림이 가리키는 묶음이 실제로 그 묶음이다).
- 각 구성 요소 내용의 version(보정은 calibration_version)이 참조한 version 과 같다.
- 규칙: (rule_id, rule_version) 이 규칙 등록부에 있고, 규칙 ID 당 한 버전이며, rules 내용과 알림의 rule_versions 가 같다.
- 보정: calibration_version 이 Tier 0 보정 저장소에 있다(`calibration_known`). 없으면 세팅 보정 채택 조건(`setup_calibration`,
  -467: 세팅 출처·계보가 지금 활성 보정·content_hash·계약)을 통과한 경우에만 받고 `ConfigBundle.setup_calibration` 을 켠다.
  채택(저장소 추가·전환)은 검증이 아니라 적용 경계에서 한다(ConfigSync before_apply).
- 구역: 같은 구역 ID 의 유효 구간이 겹치지 않는다(겹치면 판정 중 엔진이 멈춘다. 반쪽 실패를 검증에서 막는다).
- 작업면·정적 객체: ID 가 비지 않고 겹치지 않는다.

jsonschema 는 extras calibration(tier0 에 포함)과 dev 그룹에 있다. 없으면 import 때 실패한다(형식 검사 없이 묶음을
적용하는 경로를 남기지 않는다. calibration.structure 와 같은 방식).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

INSTALL_HINT = (
    "simai_ai.config 는 구성 묶음 알림을 계약 스키마로 검증하려고 jsonschema 를 쓴다. "
    "extras calibration(또는 tier0)으로 설치한다: ai/ 에서 `uv sync --extra calibration`"
)

try:
    import jsonschema  # noqa: F401
    import referencing  # noqa: F401
except ImportError as e:  # pragma: no cover - extras 없는 설치
    raise ImportError(INSTALL_HINT) from e

from simai_ai.config.bundle import (
    COMPONENT_NAMES,
    BundleNotice,
    ComponentRef,
    ConfigBundle,
    freeze_json,
    sha256_of,
    thaw_json,
)
from simai_ai.providers.contract import schema_violations
from simai_ai.risk import Clock, RiskEngine, RuleContractError, RuleRegistry, UnknownRuleError, Zone
from simai_ai.risk._parse import RiskInputError


class RejectReason(StrEnum):
    """계약 config_ack.reject_reason."""

    SCHEMA_INVALID = "SCHEMA_INVALID"
    HASH_MISMATCH = "HASH_MISMATCH"
    REFERENCE_INVALID = "REFERENCE_INVALID"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"


class FetchError(Exception):
    """내려받기 실패(연결·권한·없음). 문장에 비밀 값(서명 URL 토큰 등)을 넣지 않는다.
    Fetcher 가 FetchError 대신 OSError(TimeoutError·ConnectionError 등)를 내도 DOWNLOAD_FAILED 로 거부한다."""


class Fetcher(Protocol):
    def fetch(self, uri: str) -> bytes:
        """uri 의 내용 바이트.

        실패하면 FetchError 를 낸다. 전송 계층의 오류(HTTP 클라이언트·SDK 예외, 시간 초과, 인증 실패 등)도 FetchError 로
        감싸서 낸다(문장에 비밀 값을 넣지 않는다). 감싸지 않은 OSError 도 DOWNLOAD_FAILED 로 다루지만, 그 밖의 예외는
        ConfigSync 가 '예상 밖 오류'로 로그를 남긴다.
        """
        ...


CalibrationKnown = Callable[[str], bool]
"""calibration_version 이 Tier 0 보정 저장소에 있는지"""

SetupCalibrationGate = Callable[[Mapping[str, Any]], "str | None"]
"""저장소에 없는 보정 문서를 세팅 보정으로 받아들일 수 있는지. None 이면 통과, 아니면 거부 사유(-467).
Tier 0 는 `RuntimeCalibrationStore.setup_candidate_problem` 을 넘긴다."""


def calibration_known_from(store: Any) -> CalibrationKnown:
    """`CalibrationVersionStore`(versions() 가 보정 문서 목록) 를 참조 검사 함수로 바꾼다.

    config 는 calibration 패키지를 import 하지 않는다(무거운 extras). 저장소 객체를 받아 이름으로만 쓴다.
    """

    def known(version: str) -> bool:
        return any(doc.get("calibration_version") == version for doc in store.versions())

    return known


class BundleRejected(Exception):
    """검증 실패. reason 은 계약 거부 사유, detail 은 사람이 읽는 설명, bundle_hash 는 계산했으면 그 값."""

    def __init__(self, reason: RejectReason, detail: str, bundle_hash: str | None = None) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = RejectReason(reason)
        self.detail = detail
        self.bundle_hash = bundle_hash


@dataclass(frozen=True)
class NoticeHeader:
    """스키마가 틀린 알림에서도 ACK 에 쓸 수 있으면 꺼낸 값(둘 다 형식이 맞을 때만)."""

    config_version: str | None
    bundle_hash: str | None


def notice_header(message: Any) -> NoticeHeader:
    """ACK 를 만들 수 있는지 보려고 config_version·bundle_hash 만 꺼낸다. 형식이 틀리면 None."""
    payload = message.get("payload") if isinstance(message, Mapping) else None
    if not isinstance(payload, Mapping):
        return NoticeHeader(None, None)
    version = payload.get("config_version")
    digest = payload.get("bundle_hash")
    version_ok = isinstance(version, str) and len(version) > 0
    digest_ok = (
        isinstance(digest, str)
        and len(digest) == 71
        and digest.startswith("sha256:")
        and all(c in "0123456789abcdef" for c in digest[7:])
    )
    return NoticeHeader(version if version_ok else None, digest if digest_ok else None)


def _strict_json(data: bytes, where: str, bundle_hash: str | None) -> Any:
    def reject_constant(name: str) -> Any:
        raise ValueError(f"표준 JSON 이 아닌 값 {name}")

    try:
        return json.loads(data.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError) as e:
        raise BundleRejected(RejectReason.SCHEMA_INVALID, f"{where}: JSON 이 아니다 ({e})", bundle_hash) from None


def _object(value: Any, where: str, bundle_hash: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BundleRejected(RejectReason.SCHEMA_INVALID, f"{where}: 객체여야 한다", bundle_hash)
    return value


def _version_of(doc: Mapping[str, Any], key: str, ref: ComponentRef, bundle_hash: str) -> str:
    version = doc.get(key)
    if not isinstance(version, str) or not version:
        raise BundleRejected(RejectReason.SCHEMA_INVALID, f"{ref.name}: {key} 가 없다", bundle_hash)
    if version != ref.version:
        raise BundleRejected(
            RejectReason.REFERENCE_INVALID,
            f"{ref.name}: 내용의 버전 {version} 이 참조한 버전 {ref.version} 과 다르다",
            bundle_hash,
        )
    return version


class BundleVerifier:
    """구성 묶음 하나를 검증해 ConfigBundle 을 만든다. 상태가 없어 여러 스레드에서 불러도 된다.

    - fetcher: uri → 바이트. Tier 0 는 EC2 객체 저장소, 재시작 복구는 보관소(archive)의 로컬 사본을 쓴다.
    - registry: 규칙 등록부(두 배포가 같은 등록부를 쓴다).
    - calibration_known: 보정 버전 참조 검사(`calibration_known_from(store)`).
    - clock: verified_at 기록용 주입 시계.
    - setup_calibration: 저장소에 없는 보정 버전을 세팅 보정으로 받을지 묻는 검사(-467). 없으면 지금처럼 거부한다.
    """

    def __init__(
        self,
        fetcher: Fetcher,
        registry: RuleRegistry,
        calibration_known: CalibrationKnown,
        clock: Clock,
        *,
        setup_calibration: SetupCalibrationGate | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._registry = registry
        self._calibration_known = calibration_known
        self._clock = clock
        self._setup_calibration = setup_calibration

    def check_message(self, message: Any) -> BundleNotice:
        """알림 메시지 스키마 검사. 통과하면 BundleNotice."""
        try:
            problems = schema_violations(message, "config_bundle", check_formats=False)
        except FileNotFoundError as e:  # contracts/ 를 못 찾음: 검증할 수 없으면 적용하지 않는다
            raise BundleRejected(RejectReason.SCHEMA_INVALID, f"계약 스키마를 찾지 못했다 ({e})") from None
        if problems:
            raise BundleRejected(RejectReason.SCHEMA_INVALID, "; ".join(problems))
        return BundleNotice.from_message(message)

    def verify(self, message: Any, notice: BundleNotice | None = None) -> ConfigBundle:
        """검증을 모두 통과하면 ConfigBundle, 아니면 BundleRejected.

        notice 는 같은 message 로 check_message 를 이미 통과한 결과다(스키마 검사를 두 번 하지 않는다).
        """
        if notice is None or notice.message != freeze_json(message):
            notice = self.check_message(message)
        manifest_bytes = self._fetch(notice.bundle_uri, "매니페스트", None)
        computed = sha256_of(manifest_bytes)
        if computed != notice.bundle_hash:
            raise BundleRejected(RejectReason.HASH_MISMATCH, "매니페스트 해시가 bundle_hash 와 다르다", computed)
        manifest = _object(_strict_json(manifest_bytes, "매니페스트", computed), "매니페스트", computed)
        self._check_manifest(notice, manifest, computed)

        blobs: dict[str, bytes] = {computed: manifest_bytes}
        docs: dict[str, Mapping[str, Any]] = {}
        for name in COMPONENT_NAMES:
            ref = notice.components[name]
            data = self._fetch(ref.uri, name, computed)
            if sha256_of(data) != ref.sha256:
                raise BundleRejected(RejectReason.HASH_MISMATCH, f"{name} 해시가 참조와 다르다", computed)
            blobs[ref.sha256] = data
            docs[name] = _object(_strict_json(data, name, computed), name, computed)

        zones_version, zones = self._zones(notice.components["zones"], docs["zones"], computed)
        rules_version, rules = self._rules(notice, docs["rules"], computed)
        calibration_version, setup_calibration = self._calibration(
            notice.components["calibration"], docs["calibration"], computed
        )
        geometry_version = self._geometry(
            notice.components["operational_geometry"], docs["operational_geometry"], computed
        )
        return ConfigBundle(
            notice=notice,
            bundle_hash=computed,
            zones_version=zones_version,
            zones=zones,
            rules_version=rules_version,
            rules=rules,
            calibration_version=calibration_version,
            geometry_version=geometry_version,
            operational_geometry=freeze_json(docs["operational_geometry"]),
            verified_at=self._clock.now_ms(),
            blobs=freeze_json(blobs),
            setup_calibration=setup_calibration,
        )

    # --- 단계 -----------------------------------------------------------------------

    def _fetch(self, uri: str, what: str, bundle_hash: str | None) -> bytes:
        try:
            data = self._fetcher.fetch(uri)
        except FetchError as e:
            raise BundleRejected(RejectReason.DOWNLOAD_FAILED, f"{what} 내려받기 실패 ({e})", bundle_hash) from None
        except OSError as e:  # 시간 초과·연결 끊김 등(TimeoutError, ConnectionError). 문장에는 예외 종류만 적는다
            raise BundleRejected(
                RejectReason.DOWNLOAD_FAILED, f"{what} 내려받기 실패 ({type(e).__name__})", bundle_hash
            ) from None
        if not isinstance(data, bytes | bytearray):
            raise BundleRejected(RejectReason.DOWNLOAD_FAILED, f"{what}: 바이트를 받지 못했다", bundle_hash)
        return bytes(data)

    def _check_manifest(self, notice: BundleNotice, manifest: Mapping[str, Any], digest: str) -> None:
        payload = notice.message["payload"]
        if manifest.get("config_version") != notice.config_version:
            raise BundleRejected(RejectReason.REFERENCE_INVALID, "매니페스트의 config_version 이 알림과 다르다", digest)
        if "issued_at" in manifest and manifest["issued_at"] != notice.issued_at:
            raise BundleRejected(RejectReason.REFERENCE_INVALID, "매니페스트의 issued_at 이 알림과 다르다", digest)
        if manifest.get("components") != thaw_json(payload["components"]):
            raise BundleRejected(RejectReason.REFERENCE_INVALID, "매니페스트의 components 가 알림과 다르다", digest)

    def _zones(self, ref: ComponentRef, doc: Mapping[str, Any], digest: str) -> tuple[str, tuple[Zone, ...]]:
        version = _version_of(doc, "version", ref, digest)
        items = doc.get("zones")
        if not isinstance(items, list):
            raise BundleRejected(RejectReason.SCHEMA_INVALID, "zones: zones 배열이 없다", digest)
        zones: list[Zone] = []
        for i, item in enumerate(items):
            try:
                zones.append(Zone.from_json(item, f"zones[{i}]"))
            except RiskInputError as e:
                raise BundleRejected(RejectReason.SCHEMA_INVALID, str(e), digest) from None
        _check_zone_overlaps(zones, digest)
        return version, tuple(sorted(zones, key=lambda z: (z.zone_id, z.zone_version)))

    def _rules(self, notice: BundleNotice, doc: Mapping[str, Any], digest: str) -> tuple[str, tuple[Any, ...]]:
        ref = notice.components["rules"]
        version = _version_of(doc, "version", ref, digest)
        items = doc.get("rule_versions")
        if not isinstance(items, list) or not all(
            isinstance(r, Mapping)
            and isinstance(r.get("rule_id"), str)
            and isinstance(r.get("rule_version"), int)
            and not isinstance(r.get("rule_version"), bool)
            for r in items
        ):
            raise BundleRejected(RejectReason.SCHEMA_INVALID, "rules: rule_versions 형식이 틀렸다", digest)
        listed = sorted((r["rule_id"], r["rule_version"]) for r in items)
        if listed != sorted(notice.rule_versions):
            raise BundleRejected(RejectReason.REFERENCE_INVALID, "rules 내용과 알림의 rule_versions 가 다르다", digest)
        rules = []
        for rule_id, rule_version in listed:
            try:
                rules.append(self._registry.create(rule_id, rule_version))
            except UnknownRuleError:
                raise BundleRejected(
                    RejectReason.REFERENCE_INVALID, f"등록되지 않은 규칙 {rule_id} v{rule_version}", digest
                ) from None
        try:
            # 엔진이 받는 조건(규칙 ID 당 한 버전)을 검증 때 확인한다. 적용 시점에 엔진을 만들다 실패하지 않게 한다.
            RiskEngine(rules, self._clock)
        except RuleContractError as e:
            raise BundleRejected(RejectReason.REFERENCE_INVALID, f"rules: {e}", digest) from None
        return version, tuple(sorted(rules, key=lambda r: r.spec.key))

    def _calibration(self, ref: ComponentRef, doc: Mapping[str, Any], digest: str) -> tuple[str, bool]:
        problems = schema_violations(dict(doc), "calibration", check_formats=False)
        if problems:
            raise BundleRejected(RejectReason.SCHEMA_INVALID, "calibration: " + "; ".join(problems), digest)
        version = _version_of(doc, "calibration_version", ref, digest)
        if self._calibration_known(version):
            return version, False
        problem = "세팅 보정 채택 경로 없음" if self._setup_calibration is None else self._setup_calibration(thaw_json(doc))
        if problem is not None:
            raise BundleRejected(
                RejectReason.REFERENCE_INVALID,
                f"Tier 0 보정 저장소에 없는 보정 버전 {version} (세팅 보정으로도 받지 않음: {problem})",
                digest,
            )
        return version, True

    def _geometry(self, ref: ComponentRef, doc: Mapping[str, Any], digest: str) -> str:
        version = _version_of(doc, "version", ref, digest)
        for key, id_key in (("work_surfaces", "surface_id"), ("static_objects", "object_id")):
            if key not in doc:
                continue
            items = doc[key]
            if not isinstance(items, list) or not all(isinstance(i, Mapping) for i in items):
                raise BundleRejected(RejectReason.SCHEMA_INVALID, f"operational_geometry: {key} 형식이 틀렸다", digest)
            ids = [i.get(id_key) for i in items]
            if not all(isinstance(i, str) and i for i in ids):
                raise BundleRejected(
                    RejectReason.SCHEMA_INVALID, f"operational_geometry: {key}.{id_key} 가 비었다", digest
                )
            if len(set(ids)) != len(ids):
                raise BundleRejected(
                    RejectReason.REFERENCE_INVALID, f"operational_geometry: {key}.{id_key} 가 겹친다", digest
                )
        return version


def _check_zone_overlaps(zones: Iterable[Zone], digest: str) -> None:
    by_id: dict[str, list[Zone]] = {}
    for zone in zones:
        by_id.setdefault(zone.zone_id, []).append(zone)
    for zone_id, versions in sorted(by_id.items()):
        lo = -(2**63)
        hi = 2**63
        spans = sorted(
            ((lo if z.valid_from is None else z.valid_from), (hi if z.valid_to is None else z.valid_to), z.zone_version)
            for z in versions
        )
        for (_, end, v1), (start, _, v2) in itertools.pairwise(spans):
            if start < end:
                raise BundleRejected(
                    RejectReason.REFERENCE_INVALID,
                    f"구역 {zone_id} 의 버전 {v1}, {v2} 유효 구간이 겹친다",
                    digest,
                )
