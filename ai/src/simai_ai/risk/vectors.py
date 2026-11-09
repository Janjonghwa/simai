"""Risk Engine 공통 테스트 벡터 형식과 실행기 (명세 3.2, 15.1 G3, 21.3).

같은 벡터를 Tier 0 배포 이미지와 EC2 컨테이너 이미지 양쪽에서 돌려 같은 결과가 나오는지 본다.
실행기는 배포 이미지에도 들어가도록 src 에 두고, 벡터 JSON 은 ai/tests/risk/vectors/ 에 둔다.

    python -m simai_ai.risk.vectors ai/tests/risk/vectors     # 모두 통과하면 종료 코드 0

벡터 형식(JSON, format_version 1.x). 형식은 ai/tests/risk/vectors/README.md 에도 적는다.
1.1 은 생명주기(lifecycle) 시나리오를, 1.2 는 단계별 감시 상태(monitoring, R7 입력)를, 1.3 은 발생 지속 조건·재개
설정과 확인 필요 이유(confidence_reasons)를, 1.4 는 등록 작업면(work_surfaces, R2·R3·R5 입력)을, 1.5 는 보정 상태
(calibration_state, R2·R3·R4 의 PROVISIONAL_CALIB 입력)를, 1.6 은 안전모 요구·상태(ppe_policy·ppe_state, R9 입력)를 더했다.
이전 벡터는 그대로 읽힌다. monitoring 키는 format_version 1.2 이상에서만, 1.3 키는 1.3 이상에서만, work_surfaces 는 1.4 이상에서만,
calibration_state 는 1.5 이상에서만, ppe_policy·ppe_state 는 1.6 이상에서만 받는다(낮은 버전에 있으면 오류).

    {
      "format_version": "1.0.0",
      "vector_id": "empty-scene",
      "description": "무엇을 확인하는지",
      "origin": "LIVE",                                  # LIVE / PLANNED / REPLAY / WHAT_IF
      "rules": [{"rule_id": "R1", "rule_version": 1}],   # 등록부에서 만든다. 빈 배열 가능
      "zones": [ Zone ],                                 # simai_ai.risk.zone.Zone.from_json 형식
      "work_surfaces": [ WorkSurface ],                  # 선택(1.4). simai_ai.risk.work_surface.WorkSurface.from_json 형식
      "calibration_state": "PROVISIONAL",                # 선택(1.5). UNCALIBRATED / PROVISIONAL / CALIBRATED / DRIFTED / null
      "steps": [
        {
          "clock_ms": 1760342400200,                     # 고정 시계의 판정 시각. 단계마다 줄지 않는다
          "world_state": { I1 world_state payload },     # contracts/messages/world_state.schema.json
          "monitoring": { MonitoringState },             # 선택(1.2). simai_ai.risk.monitoring.MonitoringState.from_json
          "expect": {"findings": [ Finding ]}            # 순서 무관. 정렬해 비교한다
        }
      ]
    }

Finding 은 rule_id, rule_version, severity, world_ids, zone_id, needs_check, reasons, metrics 를 모두 적는다.
holding(히스테리시스 띠, 기본 false)은 선택이다(1.1).
사람 없는 구역·시스템 판정(R7 형태)은 world_ids 를 [] 로, zone_id 를 채운다. 둘 다 비울 수는 없다.
metrics 는 절대 오차 METRIC_ABS_TOL 안에서 같으면 통과다(플랫폼별 libm 끝자리 차이). 나머지는 정확히 같아야 한다.
모든 단계에서 "LIVE 가 아니면 알림 후보 0건" 불변 규칙도 함께 검사한다.

생명주기 시나리오(1.1, 선택). 최상위에 lifecycle 이 있으면 단계마다 LifecycleTracker 에 판정을 넣고 전이·현장
경보 신호를 비교한다.

    "lifecycle": {"gateway_id": "tier0-01", "config_version": "cfg-12",
                  "policy": {"clear_hold_ms": 3000, "clear_hold_ms_by_rule": {}, "metrics_update_interval_ms": 1000}},
    steps[] 에 더하는 필드:
      "inject_findings": [ Finding ],    # 규칙 출력 대신 넣는 판정(구체 규칙 등록 전 생명주기 시험용). 선택
      "acks": [{"event": "e1", "actor": "SITE_BUTTON", "actor_id": "laptop-01", "acked_at": 1760342400500}],  # 선택
      "expect": {"findings": [...],      # 규칙이 낸 판정만(inject_findings 제외)
                 "transitions": [{"event": "e1", "event_sequence": 1, "transition": "RAISED", "changes": ["RAISED"],
                                  "rule_id": "R1", "severity": "HIGH", "needs_check": false,
                                  "verification": "VERIFIED", "world_ids": ["w-0012"], "zone_id": "z-04",
                                  "acknowledged": false}],
                 "local_signals": [{"kind": "LOCAL_ALERT", "event": "e1", "severity": "HIGH", "needs_check": false}]}

- event 는 이벤트 별칭이다. 실행기가 RAISED 순서대로 e1, e2, … 를 붙인다(event_id 는 UUIDv7 이라 적지 않는다).
- 확인(acks)은 그 단계의 판정을 반영한 뒤 같은 시계로 적용한다. 전이·신호는 판정 → 확인 순서로 비교한다.
- lifecycle 이 있으면 expect.transitions·local_signals 를 반드시 적는다(빈 배열 가능).
- 불변 규칙도 함께 검사한다: LIVE 가 아니면 현장 경보 신호 0건, event_id 는 UUIDv7, gateway_id 일치,
  event_sequence 는 이벤트별로 1 부터 1 씩 증가.

발생 지속 조건·재개(1.3, 선택). format_version 1.3 미만 생명주기 벡터는 지속 조건·재개 없는 설정
(lifecycle.LEGACY_IMMEDIATE_POLICY: 발생 판정 한 번에 새 이벤트)으로 돌린다. 1.3 이상은 제품 기본값
(LifecyclePolicy(): 3프레임 중 2프레임, R7 즉시, 재개 10초)에서 policy 로 적은 값만 바꾼다.

    "policy": {..., "persistence": {"min_frames": 2, "window_frames": 3},
               "persistence_by_rule": {"R7": {"min_frames": 1, "window_frames": 1}},   # 적으면 기본값(R7 즉시)을 대신한다
               "resume_window_ms": 10000}                                              # null 이면 재개하지 않는다
    Finding 에 "confidence_reasons": ["PREDICTED", ...] (선택, 기본 []) 를 적을 수 있다.
    expect.transitions[] 에 "confidence_reasons" 가 필수다. 재개 전이는 changes ["RESUMED"] 로 보인다.

등록 작업면(1.4, 선택). 최상위 work_surfaces 는 모든 단계에 같이 쓰는 등록 작업면(명세 7.5, 활성 구성 묶음의 Operational
Geometry)이다. 형식은 simai_ai.risk.work_surface.WorkSurface.from_json(ContactEstimator 와 같은 임시 형식)이고 ID 가 겹치면 오류다.
없으면 등록 작업면이 없는 것으로 판정한다(R2·R3 는 판정 근거가 없다).

보정 상태(1.5, 선택). 최상위 calibration_state 는 모든 단계의 world 위치를 만든 보정의 상태(명세 4.4, RuleContext.calibration_state)다.
없거나 null 이면 보정 상태가 적용되지 않는 입력(PLANNED 계획 위치와 같다)으로 판정해 PROVISIONAL_CALIB 가 붙지 않는다.
steps[].calibration_state(선택, 1.5)는 그 단계부터가 아니라 그 단계만의 값으로 최상위 값을 대신한다(운영 중 보정 상태 전환 시험).

안전모 요구·상태(1.6, 선택, R9 입력, 명세 7.2·12.3). 최상위 ppe_policy 는 모든 단계의 구성 묶음 구성 요소 ppe_policy 내용
(contracts/data/ppe_policy 문서, null 이면 묶음에 ppe_policy 가 없음 = 요구 없음)이고, 없으면 null 과 같다.
steps[].ppe_policy(선택)는 그 단계만의 값으로 최상위 값을 대신한다(작업 시작·종료 묶음 전환 시험).
steps[].ppe_state(선택)는 그 단계 판정 전에 받은 I1 ppe_state payload 목록이다. 받은 상태는 뒤 단계에도 남고(Tier 0 의
PpeStateStore 처럼 사람별 최신 상태, 만료는 expires_at_ms), 한 번도 받지 않은 사람은 UNKNOWN 이다.
"""

import argparse
import json
import math
import random
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from simai_ai.risk._parse import (
    RiskInputError,
    as_bool,
    as_enum,
    as_int,
    as_list,
    as_mapping,
    as_number,
    as_opt_str,
    as_str,
    as_timestamp_ms,
    reject_unknown_keys,
    require,
)
from simai_ai.risk.alerting import AckActor, LocalSignal
from simai_ai.risk.clock import FixedClock
from simai_ai.risk.common import CALIBRATION_STATES, ConfidenceReason, Origin, Severity
from simai_ai.risk.engine import Evaluation, RiskEngine, RuleContractError
from simai_ai.risk.event_id import EventIdFactory, is_uuid7
from simai_ai.risk.lifecycle import (
    LEGACY_IMMEDIATE_POLICY,
    EventTransition,
    LifecyclePolicy,
    LifecycleTracker,
    Persistence,
)
from simai_ai.risk.monitoring import MonitoringState
from simai_ai.risk.ppe import PpePolicy, PpeSnapshot, WearObservation
from simai_ai.risk.registry import RuleRegistry, default_registry
from simai_ai.risk.rule import Finding
from simai_ai.risk.work_surface import WorkSurface, check_unique_ids
from simai_ai.risk.world import WorldState
from simai_ai.risk.zone import Zone

FORMAT_MAJOR = 1
MONITORING_MIN_MINOR = 2  # steps[].monitoring 을 더한 마이너 버전
PERSISTENCE_MIN_MINOR = 3  # 발생 지속 조건·재개 설정, confidence_reasons 를 더한 마이너 버전
WORK_SURFACES_MIN_MINOR = 4  # 최상위 work_surfaces(등록 작업면, R2·R3·R5 입력)를 더한 마이너 버전
CALIBRATION_STATE_MIN_MINOR = 5  # 최상위 calibration_state(보정 상태, R2·R3·R4 의 PROVISIONAL_CALIB 입력)를 더한 마이너 버전
PPE_MIN_MINOR = 6  # ppe_policy·ppe_state(안전모 요구·상태, R9 입력)를 더한 마이너 버전
METRIC_ABS_TOL = 1e-9


@dataclass(frozen=True)
class VectorAck:
    event: str  # 이벤트 별칭(e1, e2, …)
    actor: AckActor
    actor_id: str
    acked_at: int


@dataclass(frozen=True)
class VectorStep:
    clock_ms: int
    world_state: WorldState
    expected: tuple[Finding, ...]
    monitoring: MonitoringState | None = None
    injected: tuple[Finding, ...] = ()
    acks: tuple[VectorAck, ...] = ()
    # 생명주기 기대값. 비교용 dict(키는 _TRANSITION_KEYS·_SIGNAL_KEYS). lifecycle 이 없으면 None.
    expected_transitions: tuple[Mapping[str, Any], ...] | None = None
    expected_signals: tuple[Mapping[str, Any], ...] | None = None
    calibration_state: str | None = None  # 이 단계의 보정 상태(steps[].calibration_state, 없으면 최상위 값)
    ppe_policy: PpePolicy | None = None  # 이 단계의 안전모 요구(steps[].ppe_policy, 없으면 최상위 값)
    ppe_received: tuple[WearObservation, ...] = ()  # 이 단계 판정 전에 받은 안전모·조끼 상태(steps[].ppe_state)


@dataclass(frozen=True)
class LifecycleSpec:
    gateway_id: str
    config_version: str
    policy: LifecyclePolicy
    with_reasons: bool = False  # 기대 전이에 confidence_reasons 를 적는지(1.3 이상)


@dataclass(frozen=True)
class Vector:
    vector_id: str
    description: str
    origin: Origin
    rules: tuple[tuple[str, int], ...]
    zones: tuple[Zone, ...]
    steps: tuple[VectorStep, ...]
    path: Path | None = None
    lifecycle: LifecycleSpec | None = None
    work_surfaces: tuple[WorkSurface, ...] = ()
    calibration_state: str | None = None


@dataclass(frozen=True)
class VectorResult:
    vector_id: str
    path: Path | None
    mismatches: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.mismatches


def parse_vector(obj: Any, path: Path | None = None) -> Vector:
    where = str(path) if path else "vector"
    obj = as_mapping(obj, where)
    reject_unknown_keys(
        obj,
        {"format_version", "vector_id", "description", "origin", "rules", "zones", "work_surfaces", "steps"}
        | {"lifecycle", "calibration_state", "ppe_policy"},
        where,
    )

    version = as_str(require(obj, "format_version", where), f"{where}.format_version")
    parts = version.split(".")
    if parts[0] != str(FORMAT_MAJOR):
        raise RiskInputError(f"{where}: format_version {version} 은 지원하지 않는다(지원: {FORMAT_MAJOR}.x)")
    if len(parts) < 2 or not parts[1].isdigit():
        raise RiskInputError(f"{where}: format_version {version} 에 마이너 버전이 없다(예: {FORMAT_MAJOR}.2.0)")
    minor = int(parts[1])

    rules = []
    for i, r in enumerate(as_list(require(obj, "rules", where), f"{where}.rules")):
        at = f"{where}.rules[{i}]"
        r = as_mapping(r, at)
        reject_unknown_keys(r, {"rule_id", "rule_version"}, at)
        rules.append((as_str(require(r, "rule_id", at), at), as_int(require(r, "rule_version", at), at, minimum=1)))

    lifecycle = (
        None if obj.get("lifecycle") is None else _parse_lifecycle(obj["lifecycle"], f"{where}.lifecycle", minor)
    )
    if "work_surfaces" in obj and minor < WORK_SURFACES_MIN_MINOR:
        raise RiskInputError(
            f"{where}.work_surfaces: format_version {FORMAT_MAJOR}.{WORK_SURFACES_MIN_MINOR} 이상에서만 쓴다(이 벡터 {version})"
        )
    work_surfaces = tuple(
        WorkSurface.from_json(w, f"{where}.work_surfaces[{i}]")
        for i, w in enumerate(as_list(obj.get("work_surfaces", []), f"{where}.work_surfaces"))
    )
    check_unique_ids(work_surfaces, f"{where}.work_surfaces")
    if "calibration_state" in obj and minor < CALIBRATION_STATE_MIN_MINOR:
        raise RiskInputError(
            f"{where}.calibration_state: format_version {FORMAT_MAJOR}.{CALIBRATION_STATE_MIN_MINOR} 이상에서만 쓴다(이 벡터 {version})"
        )
    calibration_state = _parse_calibration_state(obj.get("calibration_state"), f"{where}.calibration_state")
    if "ppe_policy" in obj and minor < PPE_MIN_MINOR:
        raise RiskInputError(f"{where}.ppe_policy: format_version {FORMAT_MAJOR}.{PPE_MIN_MINOR} 이상에서만 쓴다(이 벡터 {version})")
    ppe_policy = _parse_ppe_policy(obj.get("ppe_policy"), f"{where}.ppe_policy")
    transition_keys = _TRANSITION_KEYS + (("confidence_reasons",) if minor >= PERSISTENCE_MIN_MINOR else ())

    steps = []
    last_clock = -1
    for i, s in enumerate(as_list(require(obj, "steps", where), f"{where}.steps")):
        at = f"{where}.steps[{i}]"
        s = as_mapping(s, at)
        step_keys = {"clock_ms", "world_state", "monitoring", "expect", "calibration_state", "ppe_policy", "ppe_state"}
        if lifecycle is not None:
            step_keys |= {"inject_findings", "acks"}
        reject_unknown_keys(s, step_keys, at)
        if "calibration_state" in s and minor < CALIBRATION_STATE_MIN_MINOR:
            raise RiskInputError(
                f"{at}.calibration_state: format_version {FORMAT_MAJOR}.{CALIBRATION_STATE_MIN_MINOR} 이상에서만 쓴다(이 벡터 {version})"
            )
        for key in ("ppe_policy", "ppe_state"):
            if key in s and minor < PPE_MIN_MINOR:
                raise RiskInputError(
                    f"{at}.{key}: format_version {FORMAT_MAJOR}.{PPE_MIN_MINOR} 이상에서만 쓴다(이 벡터 {version})"
                )
        if "monitoring" in s and minor < MONITORING_MIN_MINOR:
            raise RiskInputError(
                f"{at}.monitoring: format_version {FORMAT_MAJOR}.{MONITORING_MIN_MINOR} 이상에서만 쓴다(이 벡터 {version})"
            )
        clock_ms = as_timestamp_ms(require(s, "clock_ms", at), f"{at}.clock_ms")
        if clock_ms < last_clock:
            raise RiskInputError(f"{at}.clock_ms: 단계의 시계가 뒤로 갔다 ({last_clock} → {clock_ms})")
        last_clock = clock_ms
        expect = as_mapping(require(s, "expect", at), f"{at}.expect")
        reject_unknown_keys(
            expect, {"findings"} if lifecycle is None else {"findings", "transitions", "local_signals"}, f"{at}.expect"
        )
        step = VectorStep(
            clock_ms=clock_ms,
            world_state=WorldState.from_payload(require(s, "world_state", at), f"{at}.world_state"),
            monitoring=_parse_monitoring(s.get("monitoring"), f"{at}.monitoring"),
            calibration_state=(
                _parse_calibration_state(s["calibration_state"], f"{at}.calibration_state")
                if "calibration_state" in s
                else calibration_state
            ),
            ppe_policy=(
                _parse_ppe_policy(s["ppe_policy"], f"{at}.ppe_policy") if "ppe_policy" in s else ppe_policy
            ),
            ppe_received=_parse_ppe_state(s.get("ppe_state", []), f"{at}.ppe_state"),
            expected=_parse_findings(require(expect, "findings", f"{at}.expect"), f"{at}.expect.findings", minor),
        )
        if lifecycle is not None:
            transitions = as_list(require(expect, "transitions", f"{at}.expect"), f"{at}.expect.transitions")
            signals = as_list(require(expect, "local_signals", f"{at}.expect"), f"{at}.expect.local_signals")
            step = replace(
                step,
                injected=_parse_findings(s.get("inject_findings", []), f"{at}.inject_findings", minor),
                acks=tuple(
                    _parse_ack(a, f"{at}.acks[{j}]") for j, a in enumerate(as_list(s.get("acks", []), f"{at}.acks"))
                ),
                expected_transitions=tuple(
                    _parse_expected(t, transition_keys, f"{at}.expect.transitions[{j}]")
                    for j, t in enumerate(transitions)
                ),
                expected_signals=tuple(
                    _parse_expected(t, _SIGNAL_KEYS, f"{at}.expect.local_signals[{j}]") for j, t in enumerate(signals)
                ),
            )
        steps.append(step)
    if not steps:
        raise RiskInputError(f"{where}.steps: 단계가 하나 이상 있어야 한다")

    return Vector(
        vector_id=as_str(require(obj, "vector_id", where), f"{where}.vector_id"),
        description=as_str(require(obj, "description", where), f"{where}.description"),
        origin=as_enum(Origin, require(obj, "origin", where), f"{where}.origin"),
        rules=tuple(rules),
        zones=tuple(
            Zone.from_json(z, f"{where}.zones[{i}]")
            for i, z in enumerate(as_list(require(obj, "zones", where), f"{where}.zones"))
        ),
        steps=tuple(steps),
        path=path,
        lifecycle=lifecycle,
        work_surfaces=work_surfaces,
        calibration_state=calibration_state,
    )


def _parse_calibration_state(value: Any, where: str) -> str | None:
    state = as_opt_str(value, where)
    if state is not None and state not in CALIBRATION_STATES:
        raise RiskInputError(f"{where}: 모르는 보정 상태 {state!r}")
    return state


def _parse_ppe_policy(value: Any, where: str) -> PpePolicy | None:
    return None if value is None else PpePolicy.from_json(value, where)


def _parse_ppe_state(value: Any, where: str) -> tuple[WearObservation, ...]:
    out: list[WearObservation] = []
    for i, payload in enumerate(as_list(value, where)):
        people = as_list(require(as_mapping(payload, f"{where}[{i}]"), "people", f"{where}[{i}]"), f"{where}[{i}].people")
        for j, p in enumerate(people):
            out.extend(WearObservation.all_from_person(p, f"{where}[{i}].people[{j}]"))
    return tuple(out)


def load_vector(path: Path) -> Vector:
    with open(path, encoding="utf-8") as f:
        return parse_vector(json.load(f), path)


def run_vector(vector: Vector, registry: RuleRegistry | None = None) -> VectorResult:
    registry = default_registry() if registry is None else registry
    clock = FixedClock(vector.steps[0].clock_ms)
    engine = RiskEngine((registry.create(rule_id, version) for rule_id, version in vector.rules), clock)
    lifecycle = None if vector.lifecycle is None else _LifecycleRun(vector, vector.lifecycle, clock)

    mismatches: list[str] = []
    received: list[WearObservation] = []  # 받은 안전모·조끼 상태(Tier 0 PpeStateStore 처럼 쌓는다)
    for i, step in enumerate(vector.steps):
        clock.set(step.clock_ms)
        received.extend(step.ppe_received)
        evaluation = engine.evaluate(
            step.world_state,
            vector.zones,
            vector.origin,
            monitoring=step.monitoring,
            work_surfaces=vector.work_surfaces,
            calibration_state=step.calibration_state,
            ppe_policy=step.ppe_policy,
            ppe=PpeSnapshot.from_observations(received),
        )
        mismatches += [f"steps[{i}]: {m}" for m in _compare(evaluation, step)]
        if lifecycle is not None:
            mismatches += [f"steps[{i}]: {m}" for m in lifecycle.step(evaluation, step)]
    return VectorResult(vector.vector_id, vector.path, tuple(mismatches))


class _LifecycleRun:
    """벡터 하나의 생명주기 실행. 이벤트 별칭(e1, e2, …)과 이벤트별 마지막 event_sequence 를 기억한다."""

    def __init__(self, vector: Vector, spec: LifecycleSpec, clock: FixedClock) -> None:
        self._spec = spec
        # 같은 벡터면 같은 event_id 가 나오도록 난수를 고정한다. 비교는 별칭으로 한다.
        ids = EventIdFactory(clock, randbits=random.Random(vector.vector_id).getrandbits)
        self._tracker = LifecycleTracker(
            origin=vector.origin, gateway_id=spec.gateway_id, clock=clock, policy=spec.policy, id_factory=ids
        )
        self._labels: dict[str, str] = {}
        self._sequences: dict[str, int] = {}
        self._with_reasons = spec.with_reasons

    def step(self, evaluation: Evaluation, step: VectorStep) -> list[str]:
        out: list[str] = []
        if step.injected:
            findings = tuple(sorted(evaluation.findings + step.injected, key=lambda f: f.sort_key))
            evaluation = replace(evaluation, findings=findings)
        result = self._tracker.apply(evaluation, step.world_state, config_version=self._spec.config_version)
        transitions = list(result.transitions)
        signals = list(result.local_signals)
        for t in transitions:  # 같은 단계의 확인이 방금 생긴 이벤트를 별칭으로 가리킬 수 있게 먼저 붙인다
            self._labels.setdefault(t.event_id, f"e{len(self._labels) + 1}")
        for ack in step.acks:
            event_id = next((e for e, label in self._labels.items() if label == ack.event), None)
            acked = None
            if event_id is not None:
                acked = self._tracker.acknowledge(event_id, ack.actor, ack.actor_id, ack.acked_at)
            if acked is None:
                out.append(f"확인 대상 {ack.event} 가 활성 이벤트가 아니다")
                continue
            transitions += acked.transitions
            signals += acked.local_signals

        actual_transitions = [self._show_transition(t, out) for t in transitions]
        actual_signals = [self._show_signal(s) for s in signals]
        if not evaluation.origin.alertable and signals:
            out.append(f"{evaluation.origin} 생명주기에서 현장 경보 신호 {len(signals)}건이 나왔다(LIVE 만 알린다)")
        if actual_transitions != list(step.expected_transitions or ()):
            out.append(f"전이 실제 {actual_transitions} != 기대 {list(step.expected_transitions or ())}")
        if actual_signals != list(step.expected_signals or ()):
            out.append(f"현장 경보 신호 실제 {actual_signals} != 기대 {list(step.expected_signals or ())}")
        return out

    def _show_transition(self, t: EventTransition, out: list[str]) -> dict[str, Any]:
        s = t.state
        label = self._labels.setdefault(s.event_id, f"e{len(self._labels) + 1}")
        if not is_uuid7(s.event_id):
            out.append(f"{label}: event_id {s.event_id} 가 UUIDv7 이 아니다")
        if s.gateway_id != self._spec.gateway_id:
            out.append(f"{label}: gateway_id {s.gateway_id} != {self._spec.gateway_id}")
        expected_sequence = self._sequences.get(s.event_id, 0) + 1
        if s.event_sequence != expected_sequence:
            out.append(f"{label}: event_sequence {s.event_sequence} != {expected_sequence}(1 씩 증가해야 한다)")
        self._sequences[s.event_id] = s.event_sequence
        shown = {
            "event": label,
            "event_sequence": s.event_sequence,
            "transition": str(t.transition),
            "changes": [str(c) for c in t.changes],
            "rule_id": s.rule_id,
            "severity": str(s.severity),
            "needs_check": s.needs_check,
            "verification": str(s.verification),
            "world_ids": list(s.world_ids),
            "zone_id": s.zone_id,
            "acknowledged": s.acknowledged,
        }
        if self._with_reasons:
            shown["confidence_reasons"] = [str(r) for r in s.confidence_reasons]
        return shown

    def _show_signal(self, signal: LocalSignal) -> dict[str, Any]:
        return {
            "kind": str(signal.kind),
            "event": self._labels.get(signal.event_id, signal.event_id),
            "severity": str(signal.severity),
            "needs_check": signal.needs_check,
        }


def discover(paths: Iterable[Path]) -> list[Path]:
    """파일은 그대로, 디렉터리는 아래의 *.json 을 이름순으로 모은다."""
    found: list[Path] = []
    for p in paths:
        found += sorted(p.rglob("*.json")) if p.is_dir() else [p]
    return found


def run_paths(paths: Iterable[Path], registry: RuleRegistry | None = None) -> list[VectorResult]:
    results = []
    for path in discover(paths):
        try:
            results.append(run_vector(load_vector(path), registry))
        except (OSError, ValueError, LookupError, RuleContractError) as e:
            # 읽기·형식 오류(RiskInputError, JSON 오류), 미등록 규칙, 규칙 형식 위반은 그 벡터의 실패로 보고한다.
            results.append(VectorResult(path.stem, path, (f"벡터를 실행하지 못했다: {e}",)))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m simai_ai.risk.vectors", description="Risk Engine 공통 테스트 벡터 실행"
    )
    parser.add_argument("paths", nargs="+", type=Path, help="벡터 JSON 파일 또는 디렉터리")
    args = parser.parse_args(argv)

    results = run_paths(args.paths)
    if not results:
        print("벡터가 없다", file=sys.stderr)
        return 1
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'} {r.vector_id} ({r.path})")
        for m in r.mismatches:
            print(f"    {m}")
    failed = sum(not r.passed for r in results)
    print(f"{len(results) - failed}/{len(results)} 통과")
    return 0 if failed == 0 else 1


def _parse_finding(obj: Any, where: str, minor: int) -> Finding:
    obj = as_mapping(obj, where)
    keys = {
        "rule_id",
        "rule_version",
        "severity",
        "world_ids",
        "zone_id",
        "needs_check",
        "reasons",
        "metrics",
        "holding",
    }
    if minor >= PERSISTENCE_MIN_MINOR:
        keys.add("confidence_reasons")
    reject_unknown_keys(obj, keys, where)
    metrics = as_mapping(require(obj, "metrics", where), f"{where}.metrics")
    return Finding(
        rule_id=as_str(require(obj, "rule_id", where), f"{where}.rule_id"),
        rule_version=as_int(require(obj, "rule_version", where), f"{where}.rule_version", minimum=1),
        severity=as_enum(Severity, require(obj, "severity", where), f"{where}.severity"),
        world_ids=tuple(
            as_str(w, f"{where}.world_ids[{i}]")
            for i, w in enumerate(as_list(require(obj, "world_ids", where), f"{where}.world_ids"))
        ),
        zone_id=as_opt_str(require(obj, "zone_id", where), f"{where}.zone_id"),
        needs_check=as_bool(require(obj, "needs_check", where), f"{where}.needs_check"),
        reasons=tuple(
            as_str(r, f"{where}.reasons[{i}]")
            for i, r in enumerate(as_list(require(obj, "reasons", where), f"{where}.reasons"))
        ),
        metrics={k: as_number(v, f"{where}.metrics.{k}") for k, v in metrics.items()},
        holding=as_bool(obj.get("holding", False), f"{where}.holding"),
        confidence_reasons=tuple(
            as_enum(ConfidenceReason, r, f"{where}.confidence_reasons[{i}]")
            for i, r in enumerate(as_list(obj.get("confidence_reasons", []), f"{where}.confidence_reasons"))
        ),
    )


def _parse_findings(items: Any, where: str, minor: int) -> tuple[Finding, ...]:
    """판정 목록을 읽어 정렬한다(순서 무관 비교)."""
    parsed = (_parse_finding(f, f"{where}[{j}]", minor) for j, f in enumerate(as_list(items, where)))
    return tuple(sorted(parsed, key=lambda f: f.sort_key))


_TRANSITION_KEYS = (
    "event",
    "event_sequence",
    "transition",
    "changes",
    "rule_id",
    "severity",
    "needs_check",
    "verification",
    "world_ids",
    "zone_id",
    "acknowledged",
)
_SIGNAL_KEYS = ("kind", "event", "severity", "needs_check")


def _parse_lifecycle(obj: Any, where: str, minor: int) -> LifecycleSpec:
    obj = as_mapping(obj, where)
    reject_unknown_keys(obj, {"gateway_id", "config_version", "policy"}, where)
    at = f"{where}.policy"
    policy = as_mapping(obj.get("policy", {}), at)
    keys = {"clear_hold_ms", "clear_hold_ms_by_rule", "metrics_update_interval_ms"}
    with_persistence = minor >= PERSISTENCE_MIN_MINOR
    if with_persistence:
        keys |= {"persistence", "persistence_by_rule", "resume_window_ms"}
    reject_unknown_keys(policy, keys, at)
    # 1.3 미만은 지속 조건·재개 없는 설정, 1.3 이상은 제품 기본값에서 적은 값만 바꾼다.
    default = LifecyclePolicy() if with_persistence else LEGACY_IMMEDIATE_POLICY
    # 적지 않으면 기본값(R9 0, LifecyclePolicy)을 쓴다. 적으면 그 값이 규칙별 해소 유지 시간 전체다.
    by_rule = as_mapping(
        policy.get("clear_hold_ms_by_rule", default.clear_hold_ms_by_rule), f"{at}.clear_hold_ms_by_rule"
    )
    persistence = default.persistence
    if "persistence" in policy:
        persistence = _parse_persistence(policy["persistence"], f"{at}.persistence")
    persistence_by_rule = dict(default.persistence_by_rule)
    if "persistence_by_rule" in policy:
        items = as_mapping(policy["persistence_by_rule"], f"{at}.persistence_by_rule")
        persistence_by_rule = {
            k: _parse_persistence(v, f"{at}.persistence_by_rule.{k}") for k, v in items.items()
        }
    resume_window_ms = default.resume_window_ms
    if "resume_window_ms" in policy:
        raw = policy["resume_window_ms"]
        resume_window_ms = None if raw is None else as_int(raw, f"{at}.resume_window_ms", minimum=0)
    return LifecycleSpec(
        gateway_id=as_str(require(obj, "gateway_id", where), f"{where}.gateway_id"),
        config_version=as_str(require(obj, "config_version", where), f"{where}.config_version"),
        policy=LifecyclePolicy(
            clear_hold_ms=as_int(policy.get("clear_hold_ms", default.clear_hold_ms), f"{at}.clear_hold_ms", minimum=0),
            clear_hold_ms_by_rule={
                k: as_int(v, f"{at}.clear_hold_ms_by_rule.{k}", minimum=0) for k, v in by_rule.items()
            },
            metrics_update_interval_ms=as_int(
                policy.get("metrics_update_interval_ms", default.metrics_update_interval_ms),
                f"{at}.metrics_update_interval_ms",
                minimum=0,
            ),
            persistence=persistence,
            persistence_by_rule=persistence_by_rule,
            resume_window_ms=resume_window_ms,
        ),
        with_reasons=with_persistence,
    )


def _parse_persistence(obj: Any, where: str) -> Persistence:
    obj = as_mapping(obj, where)
    reject_unknown_keys(obj, {"min_frames", "window_frames"}, where)
    return Persistence(
        min_frames=as_int(require(obj, "min_frames", where), f"{where}.min_frames", minimum=1),
        window_frames=as_int(require(obj, "window_frames", where), f"{where}.window_frames", minimum=1),
    )


def _parse_monitoring(obj: Any, where: str) -> MonitoringState | None:
    """단계별 감시 상태(1.2, 선택). 없으면 None 으로 규칙에 넘긴다.

    R7 은 LIVE·REPLAY 면 '모름'으로 저하 판정하고, PLANNED·WHAT_IF 면 판정하지 않는다.
    """
    return None if obj is None else MonitoringState.from_json(obj, where)


def _parse_ack(obj: Any, where: str) -> VectorAck:
    obj = as_mapping(obj, where)
    reject_unknown_keys(obj, {"event", "actor", "actor_id", "acked_at"}, where)
    return VectorAck(
        event=as_str(require(obj, "event", where), f"{where}.event"),
        actor=as_enum(AckActor, require(obj, "actor", where), f"{where}.actor"),
        actor_id=as_str(require(obj, "actor_id", where), f"{where}.actor_id"),
        acked_at=as_timestamp_ms(require(obj, "acked_at", where), f"{where}.acked_at"),
    )


def _parse_expected(obj: Any, keys: tuple[str, ...], where: str) -> dict[str, Any]:
    """기대 전이·신호. 모든 키를 적어야 하고, 실행 결과를 같은 키의 JSON 값으로 바꿔 그대로 비교한다."""
    obj = as_mapping(obj, where)
    reject_unknown_keys(obj, set(keys), where)
    return {k: require(obj, k, where) for k in keys}


def _compare(evaluation: Evaluation, step: VectorStep) -> list[str]:
    out: list[str] = []
    if evaluation.evaluated_at != step.clock_ms:
        out.append(f"판정 시각 {evaluation.evaluated_at} != 고정 시계 {step.clock_ms}")
    alerts = evaluation.alert_candidates()
    if not evaluation.origin.alertable and alerts:
        out.append(f"{evaluation.origin} 판정에서 알림 후보 {len(alerts)}건이 나왔다(LIVE 만 알린다)")

    actual, expected = evaluation.findings, step.expected
    if len(actual) != len(expected):
        out.append(f"판정 {len(actual)}건, 기대 {len(expected)}건: 실제 {[_show(f) for f in actual]}")
        return out
    for j, (a, e) in enumerate(zip(actual, expected, strict=True)):
        if not _same(a, e):
            out.append(f"findings[{j}] 실제 {_show(a)} != 기대 {_show(e)}")
    return out


def _same(a: Finding, e: Finding) -> bool:
    plain = (
        "rule_id",
        "rule_version",
        "severity",
        "world_ids",
        "zone_id",
        "needs_check",
        "confidence_reasons",
        "reasons",
        "holding",
    )
    if any(getattr(a, k) != getattr(e, k) for k in plain):
        return False
    if a.metrics.keys() != e.metrics.keys():
        return False
    return all(math.isclose(a.metrics[k], e.metrics[k], rel_tol=0.0, abs_tol=METRIC_ABS_TOL) for k in a.metrics)


def _show(f: Finding) -> dict[str, Any]:
    return {
        "rule_id": f.rule_id,
        "rule_version": f.rule_version,
        "severity": str(f.severity),
        "world_ids": list(f.world_ids),
        "zone_id": f.zone_id,
        "needs_check": f.needs_check,
        "confidence_reasons": [str(r) for r in f.confidence_reasons],
        "reasons": list(f.reasons),
        "metrics": dict(f.metrics),
        "holding": f.holding,
    }


if __name__ == "__main__":
    raise SystemExit(main())
