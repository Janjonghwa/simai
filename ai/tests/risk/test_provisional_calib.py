"""보정 상태 PROVISIONAL 의 R2·R3·R4 확인 필요(PROVISIONAL_CALIB) (명세 4.4, 7.1, 부록 D-G3-03 100%, S15P31A101-218).

공통 벡터 r2_r3_podium_provisional_calib·r4_fall_floor_provisional_calib(1.5)가 PROVISIONAL 정답을 고정한다. 여기서는 같은 벡터를
보정 상태만 바꿔 돌려 음성 대조(CALIBRATED·상태 없음이면 없음)와 반례(DRIFTED·UNCALIBRATED, 판정 중 상태 전환, R5·R7 무관,
PLANNED 해당 없음, 모르는 값 거부)를 본다.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from simai_ai.calibration.state import operation_for
from simai_ai.providers.testing import load_example
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.clock import FixedClock
from simai_ai.risk.common import (
    CALIBRATION_STATES,
    PROVISIONAL_CALIB_STATES,
    ConfidenceReason,
)
from simai_ai.risk.engine import RiskEngine
from simai_ai.risk.plan import evaluate_plan
from simai_ai.risk.registry import default_registry
from simai_ai.risk.rule import RuleContext
from simai_ai.risk.vectors import parse_vector, run_vector
from simai_ai.risk.world import WorldState
from simai_ai.tracking.system_health import CalibrationState

VECTOR_DIR = Path(__file__).resolve().parent / "vectors"
PROVISIONAL_VECTORS = ("r2_r3_podium_provisional_calib.json", "r4_fall_floor_provisional_calib.json")
CALIB = "PROVISIONAL_CALIB"


def _load(name: str) -> dict:
    return json.loads((VECTOR_DIR / name).read_text(encoding="utf-8"))


def _without_calib(doc: dict, state: str | None) -> dict:
    """같은 벡터를 보정 상태 state 로 돌릴 때의 정답: PROVISIONAL_CALIB 만 빠지고 나머지는 같다."""
    doc = copy.deepcopy(doc)
    if state is None:
        del doc["calibration_state"]
    else:
        doc["calibration_state"] = state
    for step in doc["steps"]:
        for f in step["expect"]["findings"]:
            f["confidence_reasons"] = [r for r in f["confidence_reasons"] if r != CALIB]
            f["needs_check"] = bool(f["confidence_reasons"])
    return doc


@pytest.mark.parametrize("name", PROVISIONAL_VECTORS)
def test_provisional_marks_every_r2_r3_r4_finding(name):
    """D-G3-03 100%: PROVISIONAL 이면 R2·R3·R4 판정 전부(발생·유지)에 PROVISIONAL_CALIB. 판정이 하나 이상 있어야 의미가 있다."""
    doc = _load(name)
    assert doc["calibration_state"] == "PROVISIONAL"
    findings = [f for s in doc["steps"] for f in s["expect"]["findings"]]
    assert findings and all(CALIB in f["confidence_reasons"] and f["needs_check"] for f in findings)
    assert any(not f.get("holding") for f in findings)
    assert run_vector(parse_vector(doc)).passed


def test_provisional_vectors_cover_holding_findings():
    """유지(holding) 판정에도 붙는다(활성 이벤트의 유지 줄도 100%). R4 벡터에 유지 판정이 있다."""
    findings = [f for name in PROVISIONAL_VECTORS for s in _load(name)["steps"] for f in s["expect"]["findings"]]
    assert any(f.get("holding") and CALIB in f["confidence_reasons"] for f in findings)


@pytest.mark.parametrize("name", PROVISIONAL_VECTORS)
@pytest.mark.parametrize("state", ["CALIBRATED", None])
def test_calibrated_or_not_applicable_has_no_provisional_calib(name, state):
    """음성 대조: 같은 입력을 CALIBRATED 나 보정 상태 없음(PLANNED 와 같은 '해당 없음')으로 돌리면 PROVISIONAL_CALIB 만 빠진다."""
    doc = _load(name)
    assert run_vector(parse_vector(_without_calib(doc, state))).passed
    # 대조: 정답을 고치지 않고 상태만 바꾸면 실패한다(벡터가 보정 상태를 실제로 본다)
    wrong = copy.deepcopy(doc)
    wrong["calibration_state"] = "CALIBRATED"
    assert not run_vector(parse_vector(wrong)).passed


@pytest.mark.parametrize("name", PROVISIONAL_VECTORS)
@pytest.mark.parametrize("state", ["DRIFTED", "UNCALIBRATED"])
def test_untrusted_calibration_is_not_treated_as_calibrated(name, state):
    """반례(모르는 보정이 정상으로 처리되는 경로): DRIFTED·UNCALIBRATED 는 PROVISIONAL 과 같게 확인 필요를 붙인다."""
    doc = copy.deepcopy(_load(name))
    doc["calibration_state"] = state
    assert run_vector(parse_vector(doc)).passed


def test_state_set_matches_calibration_operations():
    """risk 의 값 목록은 보정 상태 enum 과 같고, calibration.state 가 R2·R3·R4 확인 필요로 정한 상태를 모두 덮는다."""
    assert CALIBRATION_STATES == {s.value for s in CalibrationState}
    for state in CalibrationState:
        if {"R2", "R3", "R4"} <= operation_for(state).needs_confirmation_rules:
            assert state.value in PROVISIONAL_CALIB_STATES
    assert "CALIBRATED" not in PROVISIONAL_CALIB_STATES


def test_state_switch_mid_sequence_keeps_r4_history():
    """반례(순서·재연결): 같은 엔진에서 판정 도중 PROVISIONAL → CALIBRATED 로 바뀌면 그 뒤 판정부터 이유가 빠지고, R4 의 사람별
    이력(급하강 후보)은 보정 상태와 무관하게 이어져 낙상 발생이 그대로 나온다(보정 버전이 같으면 이력을 버리지 않는다)."""
    provisional = parse_vector(_load("r4_fall_floor_provisional_calib.json"))
    calibrated = parse_vector(_without_calib(_load("r4_fall_floor_provisional_calib.json"), "CALIBRATED"))
    clock = FixedClock(provisional.steps[0].clock_ms)
    engine = RiskEngine([default_registry().create("R4", 1)], clock)
    half = len(provisional.steps) // 2
    raised_after_switch = False
    for i, (p, c) in enumerate(zip(provisional.steps, calibrated.steps, strict=True)):
        clock.set(p.clock_ms)
        state = "PROVISIONAL" if i < half else "CALIBRATED"
        got = engine.evaluate(p.world_state, (), provisional.origin, calibration_state=state).findings
        assert got == tuple(sorted((p if i < half else c).expected, key=lambda f: f.sort_key))
        raised_after_switch |= i >= half and any(not f.holding for f in got)
    assert raised_after_switch


def test_r5_r7_ignore_calibration_state():
    """R5·R7 은 PROVISIONAL 이어도 판정·전이가 그대로다(PROVISIONAL_CALIB 없음). R1 은 넓은 불확실성으로 보정 상태를 본다
    (S15P31A101-233, tests/risk/test_r1_calibration_margin.py). 생명주기 벡터는 1.3 이상이라 1.5 로 올려도 정책이 같다."""
    doc = copy.deepcopy(_load("r7_live_without_monitoring.json"))
    doc["format_version"] = "1.5.0"
    doc["calibration_state"] = "PROVISIONAL"
    assert run_vector(parse_vector(doc)).passed
    # r5_approach_entry 는 R1 도 함께 돌려(R1 은 넓힘으로 바뀐다) R5 판정만 상태별로 대조한다
    vector = parse_vector(_load("r5_approach_entry.json"))
    r5 = []
    for state in ("PROVISIONAL", "CALIBRATED"):
        clock = FixedClock(vector.steps[0].clock_ms)
        engine = RiskEngine([default_registry().create("R5", 1)], clock)
        got = []
        for step in vector.steps:
            clock.set(step.clock_ms)
            got.append(engine.evaluate(step.world_state, vector.zones, vector.origin, calibration_state=state).findings)
        r5.append(got)
    assert r5[0] == r5[1] and any(r5[0])


def test_planned_results_have_no_provisional_calib():
    """PLANNED 계획 위치는 측정이 아니라 보정 상태가 적용되지 않는다(R2·R3 항목이 있어도 PROVISIONAL_CALIB 없음)."""
    outcome = evaluate_plan(load_example("risk_plan_request.generated_positions.json")["payload"])
    rules = {i["rule_id"] for i in outcome.items}
    assert {"R2", "R3"} <= rules
    assert all(CALIB not in i["confidence_reasons"] for i in outcome.items)


@pytest.mark.parametrize("state", ["calibrated", "", "UNKNOWN"])
def test_unknown_calibration_state_is_rejected(state):
    """모르는 값을 CALIBRATED·해당 없음으로 읽지 않는다(오타가 확인 필요를 조용히 지우지 않게)."""
    world = WorldState(t_latest_global=1, calibration_version="c", scene_version="s", tracks=())
    with pytest.raises(RiskInputError):
        RuleContext(now_ms=1, world=world, zones=(), calibration_state=state)
    doc = copy.deepcopy(_load(PROVISIONAL_VECTORS[0]))
    doc["calibration_state"] = state
    with pytest.raises(RiskInputError):
        parse_vector(doc)


def test_calibration_state_key_needs_format_1_5():
    doc = copy.deepcopy(_load(PROVISIONAL_VECTORS[0]))
    doc["format_version"] = "1.4.0"
    with pytest.raises(RiskInputError, match="1.5"):
        parse_vector(doc)


def test_reason_order_is_declaration_order():
    """PROVISIONAL_CALIB 는 다른 이유와 함께 선언 순서로 정렬된다(같은 입력이면 같은 출력, 계약 enum 순서)."""
    doc = _load("r2_r3_podium_provisional_calib.json")
    order = list(ConfidenceReason)
    for s in doc["steps"]:
        for f in s["expect"]["findings"]:
            idx = [order.index(ConfidenceReason(r)) for r in f["confidence_reasons"]]
            assert idx == sorted(idx)


LIFECYCLE_VECTOR = "r4_fall_floor_provisional_lifecycle.json"


def test_lifecycle_vector_keeps_reason_on_every_transition_and_signal():
    """D-G3-03 100%(생명주기): PROVISIONAL 단계의 전이·현장 경보 신호는 모두 PROVISIONAL_CALIB·needs_check, CALIBRATED 단계에서만
    이유가 빠진다(UPDATED CONFIDENCE). 발생·해소·재개가 모두 있다."""
    doc = _load(LIFECYCLE_VECTOR)
    kinds = set()
    for s in doc["steps"]:
        calibrated = s.get("calibration_state", doc["calibration_state"]) == "CALIBRATED"
        for t in s["expect"]["transitions"]:
            kinds |= {t["transition"], *t["changes"]}
            assert (CALIB in t["confidence_reasons"]) is not calibrated and t["needs_check"] is not calibrated
        for sig in s["expect"]["local_signals"]:
            assert sig["needs_check"]
    assert {"RAISED", "RESOLVED", "RESUMED", "CONFIDENCE"} <= kinds


def test_lifecycle_vector_fails_when_calibrated_throughout():
    """음성 대조: 같은 생명주기 벡터를 CALIBRATED 로만 돌리면 기대와 어긋난다(벡터가 단계별 보정 상태를 실제로 쓴다)."""
    doc = copy.deepcopy(_load(LIFECYCLE_VECTOR))
    assert run_vector(parse_vector(doc)).passed
    doc["calibration_state"] = "CALIBRATED"
    assert not run_vector(parse_vector(doc)).passed
    doc = copy.deepcopy(_load(LIFECYCLE_VECTOR))
    for s in doc["steps"]:
        s.pop("calibration_state", None)  # 단계 전환을 지우면 CONFIDENCE 전이가 사라져 어긋난다
    assert not run_vector(parse_vector(doc)).passed


@pytest.mark.parametrize("value", ["calibrated", "UNKNOWN"])
def test_step_calibration_state_is_validated(value):
    doc = copy.deepcopy(_load(LIFECYCLE_VECTOR))
    doc["steps"][0]["calibration_state"] = value
    with pytest.raises(RiskInputError):
        parse_vector(doc)
    doc = copy.deepcopy(_load(LIFECYCLE_VECTOR))
    del doc["calibration_state"]
    doc["format_version"] = "1.4.0"
    with pytest.raises(RiskInputError, match="1.5"):
        parse_vector(doc)

