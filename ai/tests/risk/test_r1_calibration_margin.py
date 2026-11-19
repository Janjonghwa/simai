"""R1 보정 상태별 넓은 불확실성 (명세 4.4 'R1은 넓은 불확실성으로 동작', 7.1 불확실성 범위, S15P31A101-233).

보정이 CALIBRATED 가 아니면(PROVISIONAL·DRIFTED·UNCALIBRATED) R1 불확실성 범위를 보정 여유 0.3 m 만큼 넓힌다. 공통 벡터
r1_boundary_provisional_calib(1.5)가 PROVISIONAL 생명주기 정답을 고정하고, 여기서는 경계값·음성 대조(CALIBRATED·None 은 전과
같음)·반례(상태 전환, 모르는 보정, UNKNOWN 트랙, 높이)를 본다. 손 계산: σ 0.05 → 반축 0.1, + 점유 0.3 = 범위 반경 0.4 m
(CALIBRATED), + 보정 여유 0.3 = 0.7 m(넓힘). 해소 간격 0.3 m 라 유지 띠는 0.4~0.7 m / 0.7~1.0 m.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from simai_ai.risk import (
    FixedClock,
    LifecycleTracker,
    RiskEngine,
    RuleContext,
    ZoneEntryRule,
    default_registry,
)
from simai_ai.risk.common import (
    CALIBRATION_STATES,
    PROVISIONAL_CALIB_STATES,
    ConfidenceReason,
)
from simai_ai.risk.rules.r1_zone_entry import (
    R1_CALIBRATION_MARGIN_M,
    R1_WIDENED_STATES,
    REASON_XY_CLEAR_MARGIN,
    REASON_XY_INSIDE,
    REASON_XY_UNCERTAINTY_OVERLAP,
    REASON_Z_CLEAR_MARGIN,
    REASON_Z_OVERLAP,
)
from simai_ai.risk.vectors import parse_vector, run_vector
from tests.risk.test_r1_zone_entry import ABOVE, T0, skeleton, track, world, zone

VECTOR = Path(__file__).resolve().parent / "vectors" / "r1_boundary_provisional_calib.json"
WIDENED = sorted(PROVISIONAL_CALIB_STATES)
NOT_WIDENED = ["CALIBRATED", None]


def judge(*tracks, state, zones=None):
    zones = (zone(),) if zones is None else tuple(zones)
    ctx = RuleContext(now_ms=T0 + 100, world=world(*tracks), zones=zones, calibration_state=state)
    return list(ZoneEntryRule().evaluate(ctx))


def only(findings):
    assert len(findings) == 1, findings
    return findings[0]


def test_margin_value_and_states():
    """보정 여유 0.3 m = CALIBRATED 기준 한도 15 cm × k=2(설계 문서). 넓히는 상태 = CALIBRATED 가 아닌 상태 전부."""
    assert R1_CALIBRATION_MARGIN_M == pytest.approx(2 * 0.15)
    assert R1_WIDENED_STATES == CALIBRATION_STATES - {"CALIBRATED"}


@pytest.mark.parametrize("state", WIDENED)
@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (4.35, "overlap"),  # CALIBRATED 에서도 범위가 걸려 유지
        (4.55, "overlap"),  # CALIBRATED 는 유지 띠(0.4~0.7)
        (4.69, "overlap"),
        (4.70, "overlap"),  # 넓힌 범위가 정확히 닿음(허용오차로 걸림)
        (4.701, "hold"),  # CALIBRATED 는 판정 없음
        (4.99, "hold"),
        (5.01, None),
    ],
)
def test_widened_boundary_hysteresis(state, x, expected):
    """범위와 유지 띠가 0.3 m 더 바깥까지 넓어진다(해소가 더 엄격). 근거 코드·수치로 보정 상태가 드러난다."""
    findings = judge(track((x, 5.0)), state=state)
    if expected is None:
        assert findings == []
        return
    f = only(findings)
    assert f.holding
    base = REASON_XY_UNCERTAINTY_OVERLAP if expected == "overlap" else REASON_XY_CLEAR_MARGIN
    assert f.reasons == (base, f"calibration_{state.lower()}")
    assert dict(f.metrics) == {"zone_distance_m": pytest.approx(x - 4.0), "calibration_margin_m": 0.3}
    # 확인 필요는 붙이지 않는다(PROVISIONAL_CALIB 는 R2·R3·R4 만, 7.1). 등급은 구역 등급 그대로
    assert f.confidence_reasons == () and not f.needs_check


@pytest.mark.parametrize("state", NOT_WIDENED)
@pytest.mark.parametrize("x", [4.35, 4.39, 4.40, 4.401, 4.55, 4.69, 4.71, 4.9, 5.01])
def test_calibrated_and_not_applicable_unchanged(state, x):
    """음성 대조: CALIBRATED 와 보정 상태 없음(PLANNED)은 보정 상태를 넘기지 않은 판정과 값이 정확히 같다(넓히지 않음)."""
    t = track((x, 5.0))
    baseline = list(ZoneEntryRule().evaluate(RuleContext(now_ms=T0 + 100, world=world(t), zones=(zone(),))))
    assert judge(t, state=state) == baseline
    assert all("calibration_margin_m" not in f.metrics for f in baseline)


@pytest.mark.parametrize("state", NOT_WIDENED)
def test_calibrated_unchanged_across_judge_types(state):
    """음성 대조(높이·Z 모름·접지점·트랙 이유 포함): 여러 판정 경로에서 CALIBRATED·None 결과가 기본 판정과 같다."""
    tracks = [
        track(skel=skeleton(0.0, 1.75), anchor=(None, None, 0.0)),
        track(skel=skeleton(4.1, 5.8), anchor=(None, None, 4.1)),
        track(anchor=None, head=None),
        track((4.6, 5.0), state="PREDICTED", cameras=("cam1",)),
        track(anchor=(4.45, 5.0, 0.0)),
    ]
    zones = (zone(), ABOVE)
    for t in tracks:
        baseline = list(ZoneEntryRule().evaluate(RuleContext(now_ms=T0 + 100, world=world(t), zones=zones)))
        assert judge(t, state=state, zones=zones) == baseline


@pytest.mark.parametrize(
    ("top", "calibrated", "widened"),
    [
        # 구역 [2.0, 4.0]. 위 끝 = VALID 관절 중 가장 높은 Z(관절 σ 없음). 간격 = 2.0 - top, 넓히면 0.3 더 작다
        (1.60, None, "hold"),  # 간격 0.40 → 0.10
        (1.75, "hold", "raise"),  # 간격 0.25 → 겹침
        (1.69, None, "hold"),  # 간격 0.31 → 0.01
    ],
)
def test_volume_height_widened(top, calibrated, widened):
    """VOLUME_3D 높이도 위·아래 끝을 0.3 m 씩 넓힌다(CALIBRATED 기준 머리 높이 오차 한도 15 cm × 2)."""
    t = track(skel=skeleton(0.0, top))
    for state, want in (("CALIBRATED", calibrated), ("PROVISIONAL", widened)):
        findings = judge(t, state=state, zones=[ABOVE])
        if want is None:
            assert findings == [], state
            continue
        f = only(findings)
        assert f.holding is (want == "hold"), state
        assert (REASON_Z_OVERLAP if want == "raise" else REASON_Z_CLEAR_MARGIN) in f.reasons
        assert f.metrics["occupancy_z_max_m"] == pytest.approx(top)  # 수치는 넓힘 전 값


def test_volume_bottom_side_widened():
    """아래 끝: 4.1 m 위에 선 사람(바닥 = 접지점 Z)은 구역 [2.0, 4.0] 과 간격 0.1 → CALIBRATED 유지, 넓히면 겹침(발생)."""
    t = track(skel=skeleton(4.1, 5.8), anchor=(None, None, 4.1))
    assert only(judge(t, state="CALIBRATED", zones=[ABOVE])).holding
    f = only(judge(t, state="PROVISIONAL", zones=[ABOVE]))
    assert not f.holding and REASON_Z_OVERLAP in f.reasons


def test_z_unknown_keeps_confirmation_and_adds_calibration_reason():
    """반례(모르는 높이): Z 를 모르면 넓힌 XY 로 유지 + Z_UNKNOWN 은 그대로(보정 여유가 확인 필요를 지우지 않는다)."""
    f = only(judge(track((4.6, 5.0), anchor=None, head=None), state="PROVISIONAL", zones=[ABOVE]))
    assert f.holding
    assert f.confidence_reasons == (ConfidenceReason.Z_UNKNOWN,)
    assert f.reasons == (REASON_XY_UNCERTAINTY_OVERLAP, "calibration_provisional")


def test_inside_reason_unchanged_besides_calibration_code():
    f = only(judge(track(), state="DRIFTED"))
    assert f.reasons == (REASON_XY_INSIDE, "calibration_drifted")


@pytest.mark.parametrize("state", WIDENED)
def test_unknown_track_not_judged_even_when_widened(state):
    """반례(모르는 위치가 판정으로 바뀌는 경로): UNKNOWN 트랙은 넓혀도 R1 이 판정하지 않는다(R6 이 원래 이벤트를 유지)."""
    assert judge(track(state="UNKNOWN"), state=state) == []


def test_predicted_track_keeps_reasons_and_widens():
    """PREDICTED·SINGLE_VIEW 이유는 그대로이고 넓힘은 더해진다(σ 가 커진 예측 위치에도 여유를 더한다)."""
    f = only(judge(track((4.9, 5.0), state="PREDICTED", sigma=(0.1, 0.1), cameras=("cam1",)), state="PROVISIONAL"))
    # 범위 0.2 + 0.3 + 0.3 = 0.8 → 간격 0.1(유지). CALIBRATED 는 범위 0.5 → 간격 0.4(판정 없음)
    assert f.holding
    assert set(f.confidence_reasons) == {ConfidenceReason.PREDICTED, ConfidenceReason.SINGLE_VIEW}
    assert judge(track((4.9, 5.0), state="PREDICTED", sigma=(0.1, 0.1), cameras=("cam1",)), state="CALIBRATED") == []


# --- 생명주기(공통 벡터) ------------------------------------------------------------------------


def _load() -> dict:
    return json.loads(VECTOR.read_text(encoding="utf-8"))


def _milestones(doc: dict, state_per_step) -> list[tuple[int, str]]:
    """벡터 입력을 단계별 보정 상태로 엔진 + 생명주기에 돌려 (장면 시작 기준 ms, 전이) 목록을 낸다(기대값은 보지 않는다)."""
    v = parse_vector(doc)
    clock = FixedClock(v.steps[0].clock_ms)
    engine = RiskEngine([default_registry().create("R1", 1)], clock)
    tracker = LifecycleTracker(origin=v.origin, gateway_id="tier0-01", clock=clock, policy=v.lifecycle.policy)
    start = v.steps[0].clock_ms
    out = []
    for i, step in enumerate(v.steps):
        clock.set(step.clock_ms)
        ev = engine.evaluate(step.world_state, v.zones, v.origin, calibration_state=state_per_step(i))
        res = tracker.apply(ev, step.world_state, config_version="cfg-12")
        out += [(step.clock_ms - start, str(t.transition)) for t in res.transitions if str(t.transition) != "UPDATED"]
    return out


def test_vector_passes_and_marks_every_finding():
    doc = _load()
    assert doc["calibration_state"] == "PROVISIONAL"
    assert run_vector(parse_vector(doc)).passed
    findings = [f for s in doc["steps"] for f in s["expect"]["findings"]]
    assert findings and all(f["reasons"][-1] == "calibration_provisional" for f in findings)
    assert all("PROVISIONAL_CALIB" not in f["confidence_reasons"] for f in findings)


def test_provisional_same_entry_and_resolves_later():
    """경계 근처 진입: 발생은 모두 +1600 ms, PROVISIONAL 은 해소가 +4900 ms 로 늦다(CALIBRATED 는 +4800 ms)."""
    doc = _load()
    assert _milestones(doc, lambda i: "PROVISIONAL") == [(1600, "RAISED"), (4900, "RESOLVED")]
    for state in NOT_WIDENED:
        assert _milestones(doc, lambda i, s=state: s) == [(1600, "RAISED"), (4800, "RESOLVED")]


@pytest.mark.parametrize("state", ["DRIFTED", "UNCALIBRATED"])
def test_vector_same_for_untrusted_calibration(state):
    """반례(모르는 보정이 정상으로 처리되는 경로): DRIFTED·UNCALIBRATED(방어적)도 PROVISIONAL 과 같이 넓힌다(근거 코드만 다름)."""
    doc = copy.deepcopy(_load())
    doc["calibration_state"] = state
    for s in doc["steps"]:
        for f in s["expect"]["findings"]:
            f["reasons"][-1] = f"calibration_{state.lower()}"
    assert run_vector(parse_vector(doc)).passed


def test_vector_fails_when_calibrated():
    """음성 대조: 정답을 그대로 두고 CALIBRATED 로 돌리면 어긋난다(벡터가 보정 상태를 실제로 본다)."""
    doc = copy.deepcopy(_load())
    doc["calibration_state"] = "CALIBRATED"
    assert not run_vector(parse_vector(doc)).passed


def test_switch_to_calibrated_while_in_widened_band_does_not_resolve_at_once():
    """반례(순서·상태 전환): 나가는 중 0.6 m(넓힌 범위에서 유지, CALIBRATED 로도 유지 띠)에서 CALIBRATED 로 바뀌어도 바로 해소하지
    않는다. 0.85 m(CALIBRATED 판정 없음)부터 3초를 세어 +4800 에 해소한다(해소는 언제나 관측된 3초 유지)."""
    doc = _load()
    switch = next(i for i, s in enumerate(doc["steps"]) if s["clock_ms"] - doc["steps"][0]["clock_ms"] == 1700)
    got = _milestones(doc, lambda i: "PROVISIONAL" if i < switch else "CALIBRATED")
    assert got == [(1600, "RAISED"), (4800, "RESOLVED")]


def test_switch_to_provisional_while_clearing_restarts_clear_wait():
    """반례: CALIBRATED 로 해소를 세는 중(0.85 m 띠 밖)에 PROVISIONAL 로 바뀌면 그 위치가 넓힌 유지 띠라 해소 확인이 다시 시작된다
    (해소가 넓힘 전 기준으로 끝나지 않는다). 1800 단계만 PROVISIONAL → 해소는 1900 부터 3초 = +4900."""
    doc = _load()
    start = doc["steps"][0]["clock_ms"]
    idx = next(i for i, s in enumerate(doc["steps"]) if s["clock_ms"] - start == 1800)
    got = _milestones(doc, lambda i: "PROVISIONAL" if i == idx else "CALIBRATED")
    assert got == [(1600, "RAISED"), (4900, "RESOLVED")]


# --- 고정 기대(공통 경로가 함께 바뀌는 회귀) · 규칙 값 카탈로그 ---------------------------------------

# (입력, 기대) 를 손으로 적었다. 기대 = (holding, reasons, metrics, confidence_reasons). None 은 판정 없음.
_FIXED = [
    (track((3.5, 5.0)), None, (False, (REASON_XY_INSIDE,), {"zone_distance_m": 0.0}, ())),
    (track((4.35, 5.0)), None, (True, (REASON_XY_UNCERTAINTY_OVERLAP,), {"zone_distance_m": 0.35}, ())),
    (track((4.40, 5.0)), None, (True, (REASON_XY_UNCERTAINTY_OVERLAP,), {"zone_distance_m": 0.40}, ())),
    (track((4.401, 5.0)), None, (True, (REASON_XY_CLEAR_MARGIN,), {"zone_distance_m": 0.401}, ())),
    (track((4.69, 5.0)), None, (True, (REASON_XY_CLEAR_MARGIN,), {"zone_distance_m": 0.69}, ())),
    (track((4.71, 5.0)), None, None),
    (track((4.6, 5.0), state="PREDICTED", cameras=("cam1",)), None,
     (True, (REASON_XY_CLEAR_MARGIN,), {"zone_distance_m": 0.6},
      (ConfidenceReason.PREDICTED, ConfidenceReason.SINGLE_VIEW))),
    (track(skel=skeleton(0.0, 1.75)), ABOVE,
     (True, (REASON_XY_INSIDE, REASON_Z_CLEAR_MARGIN),
      {"zone_distance_m": 0.0, "occupancy_z_min_m": 0.0, "occupancy_z_max_m": 1.75}, ())),
    (track(skel=skeleton(0.0, 1.60)), ABOVE, None),
    (track(anchor=None, head=None), ABOVE,
     (False, (REASON_XY_INSIDE,), {"zone_distance_m": 0.0}, (ConfidenceReason.Z_UNKNOWN,))),
]


@pytest.mark.parametrize("state", NOT_WIDENED)
@pytest.mark.parametrize(("t", "z", "want"), _FIXED)
def test_calibrated_and_not_applicable_match_fixed_expectation(state, t, z, want):
    """음성 대조(고정 값): CALIBRATED·None 은 넓힘 전 정의대로 손으로 적은 값과 같다. 보정 상태 없는 기본 경로와만 비교하면
    두 경로가 함께 바뀌는 회귀를 놓치므로 값으로 고정한다."""
    findings = judge(t, state=state, zones=[zone() if z is None else z])
    if want is None:
        assert findings == []
        return
    f = only(findings)
    holding, reasons, metrics, confidence = want
    assert (f.rule_id, f.rule_version, f.world_ids, f.severity.value) == ("R1", 1, ("w-0012",), "HIGH")
    assert f.holding is holding and f.reasons == reasons and f.confidence_reasons == confidence
    assert f.needs_check is bool(confidence)
    assert f.metrics.keys() == metrics.keys()
    assert all(f.metrics[k] == pytest.approx(v, abs=1e-12) for k, v in metrics.items())


def test_catalog_records_calibration_margin_and_states():
    """보정 여유·넓힘 상태는 R1 규칙 값이라 카탈로그(rule_version 시드)에 적힌다(11.4-1, 첫 운영 배포 전 같은 버전 보완 예외)."""
    from simai_ai.risk.rule_catalog import catalog

    r1 = next(e for e in catalog() if (e.rule_id, e.rule_version) == ("R1", 1))
    assert r1.thresholds["calibration_margin"] == 0.3
    assert r1.thresholds["calibration_widened_states"] == ["DRIFTED", "PROVISIONAL", "UNCALIBRATED"]


def test_constructor_settings_drive_widening():
    """설정이 실제 판정을 정한다: 여유 0 이나 넓힘 상태 없음이면 PROVISIONAL 도 넓히지 않는다. 모르는 상태·음수 여유는 거부."""
    t = track((4.55, 5.0))
    ctx = RuleContext(now_ms=T0 + 100, world=world(t), zones=(zone(),), calibration_state="PROVISIONAL")
    base = list(ZoneEntryRule().evaluate(RuleContext(now_ms=T0 + 100, world=world(t), zones=(zone(),))))
    assert list(ZoneEntryRule(calibration_margin_m=0.0).evaluate(ctx)) == base
    assert list(ZoneEntryRule(calibration_widened_states=frozenset()).evaluate(ctx)) == base
    assert only(list(ZoneEntryRule().evaluate(ctx))).holding
    from simai_ai.risk._parse import RiskInputError

    with pytest.raises(RiskInputError):
        ZoneEntryRule(calibration_widened_states=frozenset({"provisional"}))
    with pytest.raises(RiskInputError):
        ZoneEntryRule(calibration_margin_m=-0.1)


def test_provisional_point_inside_raises():
    f = only(judge(track((3.9, 5.0)), state="PROVISIONAL"))
    assert not f.holding and f.reasons == (REASON_XY_INSIDE, "calibration_provisional")
