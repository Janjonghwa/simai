"""보정 버전 관리 (명세 4.5, 4.3 동결, AGENTS 규칙 6 덮어쓰지 않는다).

- 후보 보정은 백그라운드에서 계산해 `propose` 로 넣는다. 판정(criteria)과 계약 검사를 거쳐 아래 조건을 만족할 때만
  새 버전으로 원자적으로 전환한다. 버전은 추가만 하고 이전 버전 문서는 바꾸지 않는다.
- 위치 결과에는 `snapshot()` 으로 얻은 `ActiveCalibration.tag` 로 calibration_version 을 붙인다. 계산 하나는 한
  스냅숏만 쓰므로, 계산 도중 전환이 일어나도 결과와 버전이 어긋나지 않는다.

전환 조건 (현재 상태 → 후보 판정):

| 현재          | 후보 CALIBRATED | 후보 PROVISIONAL                          |
| 없음          | 전환            | 전환 (AI 초기 추정)                        |
| PROVISIONAL   | 전환            | 재투영 오차가 의미 있게 줄면 전환            |
| CALIBRATED    | 거절 (동결)     | 거절 (동결)                                |
| DRIFTED       | 전환            | 전환 (틀어진 보정보다 나쁘다고 볼 근거 없음) |

"의미 있게 줄었다"는 카메라 중 가장 나쁜 재투영 오차 중앙값이 `min_relative_improvement` 이상 줄었다는 뜻이다.
어느 쪽이든 값을 모르면 줄지 않은 것으로 본다.

후보는 위 표보다 먼저 계약 검사를 통과해야 한다. 실패하면 버전 목록·전이 기록·활성 버전을 그대로 둔다.
계약 검사는 모두 jsonschema 없이 돈다: 스키마와 같은 범위의 형식 검사(`structure`: 필수·허용 항목, 행렬·벡터 크기,
enum·패턴, 수치 범위, 조건부 필수), 형식이 맞을 때만 하는 기하·상태 검사(NaN·무한대, 회전, 내부 파라미터, 카메라
중심, CALIBRATED 조건), 판정이 찾은 유효하지 않은 입력(NaN·음수 측정값, camera_id 중복)이다. 형식 검사가 스키마와
같은 범위인지는 시험이 스키마 파일·jsonschema 와 대조한다. 저장은 NaN·무한대를 허용하지 않는 엄격한 JSON 으로 한다.

DRIFTED 는 운영 상태 표시이며 보정 값이 바뀌지 않으므로 새 버전을 만들지 않는다. 상태 기록(transitions, 추가만)에
남기고 `current()` 문서의 state 에만 반영한다. DATASET_GT·SIM·MODEL_ESTIMATE 보정은 계약상 DRIFTED 가 될 수 없어
틀어짐을 표시하지 않는다(현장 보정이 아니므로 감시 대상이 아니다).
틀어짐 근거는 그 근거를 낸 보정 버전에만 적용한다. 감지하는 동안 다른 버전으로 전환됐으면 이전 버전 기준의 근거를
버리고 새 활성 버전으로 다시 감지한다.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from simai_ai.calibration.criteria import (
    CalibrationEvidence,
    CalibrationJudgement,
    ScaleBasis,
    judge_calibration,
)
from simai_ai.calibration.document import CameraCalibration, build_calibration_doc
from simai_ai.calibration.drift import CameraObservation, DriftConfig, DriftFinding, detect_drift
from simai_ai.calibration.state import check_transition, operation_for
from simai_ai.calibration.structure import check_calibration_structure
from simai_ai.providers.base import CalibrationDoc, CalibrationProvenance, CalibrationProvider
from simai_ai.providers.contract import (
    DEFAULT_CALIBRATED_CRITERIA,
    DEFAULT_RANGES,
    NEVER_CALIBRATED_SOURCES,
    CalibratedCriteria,
    PlausibleRanges,
    check_calibration_geometry,
    check_calibration_state,
    is_finite_number,
)
from simai_ai.tracking.system_health import CalibrationState
from simai_ai.tracking.time_model import require_ms


class RejectReason(StrEnum):
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
    FROZEN = "FROZEN"
    NOT_IMPROVED = "NOT_IMPROVED"


@dataclass(frozen=True)
class VersionSwitchConfig:
    min_relative_improvement: float = 0.10
    version_prefix: str = "cal"

    def __post_init__(self) -> None:
        if not is_finite_number(self.min_relative_improvement) or not 0 <= self.min_relative_improvement < 1:
            raise ValueError(f"min_relative_improvement 는 0 이상 1 미만이어야 한다: {self.min_relative_improvement}")


@dataclass(frozen=True)
class ProposalResult:
    """후보 하나의 처리 결과. adopted 가 False 면 doc 은 저장되지 않은 후보 문서다."""

    adopted: bool
    doc: CalibrationDoc
    judgement: CalibrationJudgement
    reject_reason: RejectReason | None = None
    violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class StateTransition:
    """보정 상태 전이 한 건 (추가만 하는 기록). from_version 은 전환 전 버전(없으면 None)."""

    at_ms: int
    from_state: CalibrationState
    to_state: CalibrationState
    from_version: str | None
    to_version: str
    reasons: tuple[str, ...]


class ActiveCalibration:
    """한 시점의 활성 보정 스냅숏. 이후 전환·DRIFTED 표시가 있어도 바뀌지 않는다."""

    __slots__ = ("_doc", "_state")

    def __init__(self, doc: Mapping[str, Any], state: CalibrationState) -> None:
        self._doc = _strict_json(doc)
        self._state = CalibrationState(state)

    @property
    def state(self) -> CalibrationState:
        return self._state

    @property
    def calibration_version(self) -> str:
        return json.loads(self._doc)["calibration_version"]

    def doc(self) -> CalibrationDoc:
        """계약 문서 사본. state 는 스냅숏 시점의 운영 상태(DRIFTED 포함)다."""
        doc = json.loads(self._doc)
        doc["state"] = str(self._state)
        return doc

    def provenance(self) -> CalibrationProvenance:
        doc = json.loads(self._doc)
        return {"source": doc["provider_source"], "calibration_version": doc["calibration_version"]}

    def tag(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """위치 결과에 calibration_version 을 붙인 사본을 준다.

        다른 버전이 이미 붙어 있으면 덮어쓰지 않고 ValueError.
        """
        version = self.calibration_version
        existing = result.get("calibration_version")
        if existing is not None and existing != version:
            raise ValueError(f"결과에 이미 다른 calibration_version {existing!r} 이 있다 (활성 {version!r})")
        return {**result, "calibration_version": version}


@dataclass(frozen=True)
class _Head:
    index: int
    state: CalibrationState


class CalibrationVersionStore:
    """보정 버전 목록(추가만)과 활성 버전·운영 상태. 쓰기는 잠금 안에서 한 번에 바꾼다.

    camera_ids 는 현장에 설치된 카메라 목록이다. 주면 후보가 정확히 이 카메라들을 보정해야 CALIBRATED 가 될 수 있고,
    주지 않으면 서로 다른 카메라 criteria.min_cameras 대(기본 4대)를 요구한다.
    """

    def __init__(
        self,
        site_id: str | None = None,
        criteria: CalibratedCriteria = DEFAULT_CALIBRATED_CRITERIA,
        config: VersionSwitchConfig = VersionSwitchConfig(),
        ranges: PlausibleRanges = DEFAULT_RANGES,
        camera_ids: Collection[str] | None = None,
    ) -> None:
        if camera_ids is not None:
            camera_ids = tuple(camera_ids)
            if not camera_ids or len(set(camera_ids)) != len(camera_ids):
                raise ValueError(f"설치 카메라 목록은 비어 있지 않고 중복이 없어야 한다: {camera_ids}")
        self.camera_ids = camera_ids
        self.site_id = site_id
        self.criteria = criteria
        self.config = config
        self.ranges = ranges
        self._lock = threading.Lock()
        self._versions: tuple[str, ...] = ()  # JSON 문자열로 보관해 밖에서 고칠 수 없게 한다
        self._evidence: tuple[str, ...] = ()  # _versions 와 같은 순서의 판정 근거 감사 기록
        self._transitions: tuple[StateTransition, ...] = ()
        self._head: _Head | None = None

    # ------------------------------------------------------------------ 읽기

    @property
    def state(self) -> CalibrationState:
        head = self._head
        return CalibrationState.UNCALIBRATED if head is None else head.state

    @property
    def versions(self) -> tuple[CalibrationDoc, ...]:
        """저장된 모든 버전 문서(저장 당시 그대로, 추가 순서)."""
        return tuple(json.loads(v) for v in self._versions)

    @property
    def transitions(self) -> tuple[StateTransition, ...]:
        return self._transitions

    def evidence_record(self, calibration_version: str) -> dict[str, Any]:
        """그 버전을 채택할 때 쓴 판정 근거 원자료(감사 기록, 명세 4.3 사용한 근거 기록).

        계약 문서에는 근거 종류(scale_basis·scale_cross_check_basis)와 검산 요약만 들어가므로, 정물 이름·배율·표본 수와
        검산점별 관측, 바닥 근거를 여기에 남긴다. 버전 문서와 함께 추가만 한다. 없는 버전이면 LookupError.
        """
        for i in range(len(self._versions)):
            if self._version_id(i) == calibration_version:
                return json.loads(self._evidence[i])
        raise LookupError(f"보정 버전 {calibration_version!r} 이 없다")

    def snapshot(self) -> ActiveCalibration | None:
        """활성 보정 스냅숏. UNCALIBRATED(버전 없음)면 None."""
        head = self._head
        if head is None:
            return None
        return ActiveCalibration(json.loads(self._versions[head.index]), head.state)

    # ------------------------------------------------------------------ 쓰기

    def propose(
        self,
        *,
        provider_source: str,
        provider_version: str,
        cameras: Sequence[CameraCalibration],
        evidence: CalibrationEvidence,
        at_ms: int,
        fallback_scale_basis: ScaleBasis | str = ScaleBasis.MODEL_ESTIMATE,
    ) -> ProposalResult:
        """후보 보정을 판정하고 조건을 만족하면 새 버전으로 전환한다. at_ms 는 후보의 created_at 이다."""
        require_ms("at_ms", at_ms)
        camera_docs = [c.to_contract() for c in cameras]
        judgement = judge_calibration(provider_source, camera_docs, evidence, self.criteria, self.camera_ids)
        with self._lock:
            self._require_monotonic(at_ms)
            doc = build_calibration_doc(
                calibration_version=self._next_version_id(),
                provider_source=provider_source,
                provider_version=provider_version,
                created_at=at_ms,
                cameras=camera_docs,
                judgement=judgement,
                fallback_scale_basis=fallback_scale_basis,
                site_id=self.site_id,
            )
            violations = tuple([f"입력: {m}" for m in judgement.invalid_inputs] + self._contract_violations(doc))
            if violations:
                return ProposalResult(False, doc, judgement, RejectReason.CONTRACT_VIOLATION, violations)
            reject = self._reject_reason(judgement.state, doc)
            if reject is not None:
                return ProposalResult(False, doc, judgement, reject)

            head = self._head
            check_transition(self.state, judgement.state)
            stored, audit = _strict_json(doc), _strict_json(_evidence_record(evidence, judgement))
            transition = StateTransition(
                at_ms=at_ms,
                from_state=self.state,
                to_state=judgement.state,
                from_version=None if head is None else self._version_id(head.index),
                to_version=doc["calibration_version"],
                reasons=tuple(f.message for f in judgement.failures),
            )
            self._versions = self._versions + (stored,)
            self._evidence = self._evidence + (audit,)
            self._transitions = self._transitions + (transition,)
            self._head = _Head(len(self._versions) - 1, judgement.state)
            return ProposalResult(True, json.loads(json.dumps(doc)), judgement)

    def report_drift(
        self, findings: Iterable[DriftFinding], at_ms: int, calibration_version: str | None = None
    ) -> bool:
        """틀어짐 근거가 있으면 활성 보정을 DRIFTED 로 표시한다. 표시했으면 True.

        calibration_version 은 근거를 낸 보정 버전이다. 주면 활성 버전이 그 버전일 때만 표시한다(다른 버전의 근거를
        새 버전에 적용하지 않는다).
        """
        return self._report_drift(tuple(findings), at_ms, calibration_version) is True

    def _report_drift(
        self, findings: tuple[DriftFinding, ...], at_ms: int, calibration_version: str | None
    ) -> bool | None:
        """표시했으면 True, 표시할 것이 없으면 False, 근거를 낸 버전이 이미 활성 버전이 아니면 None."""
        require_ms("at_ms", at_ms)
        with self._lock:
            head = self._head
            if head is None:
                return False
            doc = json.loads(self._versions[head.index])
            version = doc["calibration_version"]
            if calibration_version is not None and calibration_version != version:
                return None
            if not findings or head.state not in (CalibrationState.CALIBRATED, CalibrationState.PROVISIONAL):
                return False
            if doc["provider_source"] in NEVER_CALIBRATED_SOURCES:
                return False
            self._require_monotonic(at_ms)
            check_transition(head.state, CalibrationState.DRIFTED)
            self._transitions = self._transitions + (
                StateTransition(
                    at_ms=at_ms,
                    from_state=head.state,
                    to_state=CalibrationState.DRIFTED,
                    from_version=version,
                    to_version=version,
                    reasons=tuple(f"{f.camera_id}: {f.reason} {f.message}" for f in findings),
                ),
            )
            self._head = _Head(head.index, CalibrationState.DRIFTED)
            return True

    def observe(
        self, observations: Iterable[CameraObservation], at_ms: int, config: DriftConfig = DriftConfig()
    ) -> tuple[DriftFinding, ...]:
        """운영 관측으로 틀어짐을 감지하고, 근거가 있으면 DRIFTED 로 표시한다. 감지한 근거를 돌려준다.

        감지는 잠금 밖에서 스냅숏으로 한다. 그동안 다른 버전으로 전환됐으면 이전 버전 기준의 근거는 버리고 새 활성
        버전으로 다시 감지한다(전환은 검증된 새 보정이 있을 때만 일어나므로 반복은 짧다).
        """
        observations = tuple(observations)
        while True:
            snap = self.snapshot()
            if snap is None:
                return ()
            findings = detect_drift(snap.doc(), observations, config)
            if self._report_drift(findings, at_ms, snap.calibration_version) is not None:
                return findings

    # ------------------------------------------------------------------ 내부

    def _contract_violations(self, doc: Mapping[str, Any]) -> list[str]:
        """계약 전체 검사. 형식(스키마 범위)이 틀리면 형식이 맞다고 전제하는 기하·상태 검사는 돌리지 않는다."""
        structure = check_calibration_structure(doc)
        if structure:
            return [f"형식: {m}" for m in structure]
        return check_calibration_geometry(doc, self.ranges) + check_calibration_state(doc, self.criteria)

    def _reject_reason(self, candidate: CalibrationState, doc: Mapping[str, Any]) -> RejectReason | None:
        head = self._head
        if head is None:
            return None
        if operation_for(head.state).frozen:
            return RejectReason.FROZEN
        if candidate is CalibrationState.CALIBRATED or head.state is CalibrationState.DRIFTED:
            return None
        active = json.loads(self._versions[head.index])
        before, after = _worst_median(active), _worst_median(doc)
        if before is None or after is None or after > before * (1.0 - self.config.min_relative_improvement):
            return RejectReason.NOT_IMPROVED
        return None

    def _next_version_id(self) -> str:
        used = {self._version_id(i) for i in range(len(self._versions))}
        n = len(self._versions) + 1
        while f"{self.config.version_prefix}-{n}" in used:
            n += 1
        return f"{self.config.version_prefix}-{n}"

    def _version_id(self, index: int) -> str:
        return json.loads(self._versions[index])["calibration_version"]

    def _require_monotonic(self, at_ms: int) -> None:
        if self._transitions and at_ms < self._transitions[-1].at_ms:
            raise ValueError(f"at_ms {at_ms} 가 마지막 전이 시각 {self._transitions[-1].at_ms} 보다 이르다")


def _strict_json(doc: Mapping[str, Any]) -> str:
    """NaN·무한대를 허용하지 않는 JSON. 계약 검사가 먼저 걸러내지만 저장 경계에서 한 번 더 막는다."""
    return json.dumps(doc, allow_nan=False)


def _evidence_record(evidence: CalibrationEvidence, judgement: CalibrationJudgement) -> dict[str, Any]:
    return {"evidence": asdict(evidence), "scale_decision": asdict(judgement.scale)}


def _worst_median(doc: Mapping[str, Any]) -> float | None:
    values = [(cam.get("reprojection_error_px") or {}).get("median") for cam in doc.get("cameras", [])]
    if not values or any(v is None for v in values):
        return None
    return max(values)


class VersionedCalibrationProvider(CalibrationProvider):
    """버전 저장소의 활성 보정을 CalibrationProvider 로 낸다. UNCALIBRATED(버전 없음)면 current() 가 LookupError."""

    def __init__(self, store: CalibrationVersionStore) -> None:
        self.store = store

    def current(self) -> CalibrationDoc:
        return self._snapshot().doc()

    def provenance(self) -> CalibrationProvenance:
        return self._snapshot().provenance()

    def _snapshot(self) -> ActiveCalibration:
        snap = self.store.snapshot()
        if snap is None:
            raise LookupError("활성 보정이 없다 (UNCALIBRATED)")
        return snap
