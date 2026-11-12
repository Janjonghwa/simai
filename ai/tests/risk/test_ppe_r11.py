"""R11 입력(조끼 요구·상태)과 R11 규칙 경계 (S15P31A101-548, 명세 5.5·7.2·8.10, 계약 data/ppe_policy 1.1.0).

판정 흐름 전체(발생·해소·요구 해제·UNKNOWN·REPLAY·1.0.0 묶음)는 공통 벡터 tests/risk/vectors/r11_*.json 이 본다.
여기서는 1.1.0 요구 문서 읽기, 조끼 상태 저장, R9 와 서로 섞이지 않는 것, 생명주기 기본값을 본다.
"""

import json
from pathlib import Path

import pytest

from simai_ai.risk import (
    FixedClock,
    LifecyclePolicy,
    Origin,
    PpeItem,
    PpePolicy,
    PpeSnapshot,
    PpeStateStore,
    RiskEngine,
    VestRequiredRule,
    WearObservation,
    WearState,
    WorldState,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.rules.r9_helmet import HelmetRequiredRule

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
EXAMPLES = CONTRACTS / "examples"
STATE = json.loads((EXAMPLES / "ppe_state.json").read_text(encoding="utf-8"))
T0 = 1760342400400  # 예시 ppe_state 의 capture_time_ms


def track(world_id, t, revision=0):
    return {
        "world_id": world_id, "t_last_valid": t, "observation_state": "OBSERVED", "track_lifecycle": "CONFIRMED",
        "identity_confidence": "HIGH", "identity_revision": revision, "position_xy": [3.4, 5.0],
        "position_xy_sigma": [0.08, 0.08], "pelvis_xyz": [3.4, 5.0, 0.92], "ground_anchor_xyz": [3.4, 5.0, 0.0],
        "support_surface_id": "floor", "support_surface_validity": "VALID", "contact_validity": "VALID",
        "z_validity": "VALID", "head_z": 1.68, "cameras": ["cam1", "cam3"],
    }


def world(t, *tracks):
    return WorldState.from_payload(
        {"t_latest_global": t, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": list(tracks)}
    )


def vest(state, *, t=T0, world_id="w-0012"):
    return WearObservation(world_id, 0, t, state, None if state == "UNKNOWN" else t + 10_000, PpeItem.VEST)


def helmet(state, *, t=T0, world_id="w-0012"):
    return WearObservation(world_id, 0, t, state, None if state == "UNKNOWN" else t + 10_000)


WORK = PpePolicy(version="ppe-4", work_id=7, effective_from_ms=T0 - 60_000, helmet="REQUIRED", vest="REQUIRED")


# --- 요구(ppe_policy 1.1.0) ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "vest_value", "required"),
    [("ppe_policy.json", None, False), ("ppe_policy.vest.json", "REQUIRED", True),
     ("ppe_policy.vest_no_work.json", "NOT_REQUIRED", False)],
)
def test_policy_reads_optional_vest(name, vest_value, required):
    """1.0.0 문서(vest 없음)는 조끼 요구 없음, 1.1.0 은 실린 값대로(계약 예시)."""
    doc = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    policy = PpePolicy.from_json(doc)
    assert (None if policy.vest is None else str(policy.vest)) == vest_value
    assert policy.vest_required is required
    assert PpePolicy.from_json(policy.to_json() | {"format_version": doc["format_version"]}) == policy


def test_vest_requirement_follows_work():
    """안전모와 같은 작업 기준(8.10): 작업이 없으면 조끼도 NOT_REQUIRED. 스키마와 같은 규칙을 한 번 더 막는다."""
    with pytest.raises(RiskInputError):
        PpePolicy(version="ppe-x", work_id=None, effective_from_ms=T0, helmet="NOT_REQUIRED", vest="REQUIRED")
    # 1.2.0(S15P31A101-654): 작업 중에도 조끼를 끈 작업은 NOT_REQUIRED 다
    assert not PpePolicy(version="ppe-x", work_id=7, effective_from_ms=T0, helmet="REQUIRED", vest="NOT_REQUIRED").vest_required


# --- 조끼 상태 -------------------------------------------------------------------------------------------


def test_snapshot_keeps_helmet_and_vest_apart():
    snap = PpeSnapshot.from_payloads([STATE["payload"]])
    t = world(T0, track("w-0012", T0, revision=2)).tracks[0]
    assert snap.helmet_state(t, T0) is WearState.WORN
    assert snap.vest_state(t, T0) is WearState.NOT_WORN  # 예시: 안전모 착용·조끼 미착용
    assert snap.vests["w-0013"].state is WearState.UNKNOWN


def test_store_keeps_vests():
    store = PpeStateStore()
    assert store.offer(STATE)
    snap = store.snapshot()
    assert snap.helmets["w-0012"].state is WearState.WORN
    assert snap.vests["w-0012"].state is WearState.NOT_WORN
    assert snap.vests["w-0012"].item is PpeItem.VEST


# --- 규칙 ------------------------------------------------------------------------------------------------


def evaluate(rule, policy, observations):
    engine = RiskEngine([rule], FixedClock(T0 + 100))
    return engine.evaluate(
        world(T0, track("w-0012", T0)), [], Origin.LIVE, ppe_policy=policy,
        ppe=PpeSnapshot.from_observations(observations),
    )


def test_r11_reads_vest_only_and_r9_reads_helmet_only():
    """안전모 착용·조끼 미착용이면 R11 만 나고, 반대면 R9 만 난다(서로 섞이지 않는다)."""
    observations = [helmet("WORN"), vest("NOT_WORN")]
    assert [f.rule_id for f in evaluate(VestRequiredRule(), WORK, observations).findings] == ["R11"]
    assert evaluate(HelmetRequiredRule(), WORK, observations).findings == ()
    observations = [helmet("NOT_WORN"), vest("WORN")]
    assert evaluate(VestRequiredRule(), WORK, observations).findings == ()
    assert [f.rule_id for f in evaluate(HelmetRequiredRule(), WORK, observations).findings] == ["R9"]


def test_r11_needs_vest_requirement():
    """vest 가 없는 1.0.0 요구(안전모만 REQUIRED)면 조끼 미착용이어도 R11 판정이 없고, 모두 해소 근거다."""
    policy = PpePolicy(version="ppe-3", work_id=7, effective_from_ms=T0, helmet="REQUIRED")
    evaluation = evaluate(VestRequiredRule(), policy, [vest("NOT_WORN")])
    assert evaluation.findings == ()
    assert "R11" in evaluation.rules_cleared_for_all


def test_lifecycle_defaults_treat_r11_like_r9():
    policy = LifecyclePolicy()
    assert policy.clear_hold_ms_by_rule["R11"] == 0
    assert "R11" in policy.pending_loss_exempt_rules
