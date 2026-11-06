"""R9 입력(안전모 요구·상태)과 R9 규칙·생명주기 예외 (명세 5.5, 7.2 R9, 6.4, S15P31A101-519).

판정 흐름 전체(발생·해소·요구 해제·UNKNOWN·REPLAY)는 공통 벡터 tests/risk/vectors/r9_*.json 이 본다. 여기서는 입력 처리와
벡터로 적기 어려운 경계를 본다.
"""

import json
from pathlib import Path

import pytest

from simai_ai.risk import (
    ConfidenceReason,
    FixedClock,
    HelmetObservation,
    HelmetRequiredRule,
    LifecyclePolicy,
    LifecycleTracker,
    Origin,
    PpePolicy,
    PpeSnapshot,
    PpeStateStore,
    RiskEngine,
    WearState,
    WorldState,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.rule import PersonClearEvidence

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
EXAMPLE = json.loads((CONTRACTS / "examples" / "ppe_state.json").read_text(encoding="utf-8"))
T0 = 1760342400400  # 예시 ppe_state 의 capture_time_ms
WORK = PpePolicy(version="ppe-3", work_id=7, effective_from_ms=T0 - 60_000, helmet="REQUIRED")
NO_WORK = PpePolicy(version="ppe-4", work_id=None, effective_from_ms=T0, helmet="NOT_REQUIRED")


def track(world_id, t, revision=0, state="OBSERVED"):
    return {
        "world_id": world_id, "t_last_valid": t, "observation_state": state, "track_lifecycle": "CONFIRMED",
        "identity_confidence": "HIGH", "identity_revision": revision, "position_xy": [3.4, 5.0],
        "position_xy_sigma": [0.08, 0.08], "pelvis_xyz": [3.4, 5.0, 0.92], "ground_anchor_xyz": [3.4, 5.0, 0.0],
        "support_surface_id": "floor", "support_surface_validity": "VALID", "contact_validity": "VALID",
        "z_validity": "VALID", "head_z": 1.68, "cameras": ["cam1", "cam3"],
    }


def world(t, *tracks):
    return WorldState.from_payload(
        {"t_latest_global": t, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": list(tracks)}
    )


def obs(state, *, revision=0, t=T0, expires=None, world_id="w-0012"):
    if state == "UNKNOWN":
        expires_at = None
    else:
        expires_at = t + 10_000 if expires is None else expires
    return HelmetObservation(world_id, revision, t, state, expires_at)


def message(seq, people, epoch=1760342000000):
    payload = dict(EXAMPLE["payload"]) | {"stream_epoch": epoch, "state_sequence": seq, "people": people}
    return dict(EXAMPLE) | {"payload": payload}


# --- 요구(ppe_policy) ----------------------------------------------------------------------------------


def test_policy_from_contract_example():
    doc = json.loads((CONTRACTS / "examples" / "ppe_policy.json").read_text(encoding="utf-8"))
    policy = PpePolicy.from_json(doc)
    assert policy.helmet_required is (doc["helmet"] == "REQUIRED")
    assert policy.work_id == doc["work_id"]


@pytest.mark.parametrize(("work_id", "helmet"), [(7, "NOT_REQUIRED"), (None, "REQUIRED")])
def test_policy_requirement_follows_work(work_id, helmet):
    """작업 기준(8.10): 작업이 있으면 REQUIRED, 없으면 NOT_REQUIRED. 스키마와 같은 규칙을 한 번 더 막는다."""
    with pytest.raises(RiskInputError):
        PpePolicy(version="ppe-x", work_id=work_id, effective_from_ms=T0, helmet=helmet)


# --- 안전모 상태 ---------------------------------------------------------------------------------------


def test_state_expires_at_expiry_time():
    o = obs("NOT_WORN", expires=T0 + 10_000)
    assert o.state_at(T0 + 10_000) is WearState.NOT_WORN
    assert o.state_at(T0 + 10_001) is WearState.UNKNOWN  # PPE 가 멈춰도 Tier 0 가 스스로 만료시킨다(5.5)


def test_snapshot_requires_same_identity_revision():
    snap = PpeSnapshot.from_observations([obs("NOT_WORN", revision=1)])
    t = world(T0, track("w-0012", T0, revision=2)).tracks[0]
    assert snap.helmet_state(t, T0) is WearState.UNKNOWN  # ID 교정 뒤 옛 상태로 판정하지 않는다(D-G6-10)
    t = world(T0, track("w-0012", T0, revision=1)).tracks[0]
    assert snap.helmet_state(t, T0) is WearState.NOT_WORN


def test_snapshot_keeps_latest_per_person():
    snap = PpeSnapshot.from_observations(
        [obs("NOT_WORN", t=T0 + 500), obs("WORN", t=T0), obs("UNKNOWN", revision=1, t=T0 - 1000)]
    )
    # identity_revision 이 크면 새것, 같으면 촬영 시각이 늦은 것
    assert snap.helmets["w-0012"].identity_revision == 1
    snap = PpeSnapshot.from_observations([obs("NOT_WORN", t=T0 + 500), obs("WORN", t=T0)])
    assert snap.helmets["w-0012"].state is WearState.NOT_WORN


def test_snapshot_from_contract_example():
    snap = PpeSnapshot.from_payloads([EXAMPLE["payload"]])
    assert snap.helmets["w-0012"].state is WearState.WORN
    assert snap.helmets["w-0013"].state is WearState.UNKNOWN


# --- Tier 0 저장소 -------------------------------------------------------------------------------------


def test_store_orders_by_stream_and_sequence():
    store = PpeStateStore()
    person = EXAMPLE["payload"]["people"][0]
    assert store.offer(message(10, [person]))
    stale = dict(person) | {"capture_time_ms": person["capture_time_ms"] + 1,
                            "helmet": dict(person["helmet"]) | {"state": "UNKNOWN", "expires_at_ms": None,
                                                                "last_evidence_at_ms": None, "evidence": []}}
    assert not store.offer(message(9, [stale]))  # 늦게 온 옛 메시지는 최신 상태를 덮지 않는다
    assert store.snapshot().helmets["w-0012"].state is WearState.WORN
    assert store.offer(message(1, [stale], epoch=1760342999999))  # 새 PPE 프로세스(epoch 큼)는 받는다
    assert store.snapshot().helmets["w-0012"].state is WearState.UNKNOWN
    assert store.stats() == {"accepted": 2, "stale": 1, "invalid": 0, "people": 1}


@pytest.mark.parametrize(
    "bad",
    [
        {"msg_type": "world_state", "payload": {}},
        message(3, [{"world_id": "w-1"}]),
        {"msg_type": "ppe_state"},
    ],
)
def test_store_counts_and_drops_invalid(bad):
    store = PpeStateStore()
    assert not store.offer(bad)
    assert store.stats()["invalid"] == 1
    assert store.snapshot().helmets == {}


def test_store_bounds_people():
    store = PpeStateStore(max_people=2)
    person = EXAMPLE["payload"]["people"][1]
    for i in range(3):
        store.offer(message(i + 1, [dict(person) | {"world_id": f"w-{i}", "capture_time_ms": T0 + i}]))
    assert sorted(store.snapshot().helmets) == ["w-1", "w-2"]  # 촬영 시각이 오래된 사람부터 지운다


# --- R9 규칙 -------------------------------------------------------------------------------------------


def evaluate(policy, snapshot, w, origin=Origin.LIVE, rules=None):
    engine = RiskEngine([HelmetRequiredRule()] if rules is None else rules, FixedClock(w.t_latest_global + 100))
    return engine.evaluate(w, [], origin, ppe_policy=policy, ppe=snapshot)


def test_r9_is_person_clear_evidence():
    assert isinstance(HelmetRequiredRule(), PersonClearEvidence)
    spec = HelmetRequiredRule().spec
    assert spec.key == ("R9", 1)
    assert spec.legal_basis == "산업안전보건기준규칙 제32조"


def test_r9_states_under_requirement():
    w = world(T0, track("w-0012", T0), track("w-0013", T0), track("w-0014", T0))
    snap = PpeSnapshot.from_observations(
        [obs("NOT_WORN"), obs("WORN", world_id="w-0013")]  # w-0014 는 상태 없음 → UNKNOWN
    )
    ev = evaluate(WORK, snap, w)
    by_id = {f.world_ids[0]: f for f in ev.findings}
    assert set(by_id) == {"w-0012", "w-0014"}
    assert not by_id["w-0012"].holding and by_id["w-0012"].confidence_reasons == ()
    assert by_id["w-0014"].holding and by_id["w-0014"].confidence_reasons == (ConfidenceReason.PPE_UNKNOWN,)
    assert all(f.severity == "CAUTION" and f.zone_id is None for f in ev.findings)
    assert ev.person_clear_evidence == frozenset({("R9", "w-0013")})
    assert ev.rules_cleared_for_all == frozenset()
    assert len(ev.alert_candidates()) == 2  # LIVE 만 알림 후보(채널은 7.3 표: CAUTION 관제 화면 안내)


@pytest.mark.parametrize("policy", [None, NO_WORK])
def test_r9_without_requirement_judges_nothing(policy):
    """ppe_policy 가 없거나(“PPE 요구 미수신”) 작업이 없으면 판정하지 않고, 요구 해제를 해소 근거로 낸다."""
    w = world(T0, track("w-0012", T0))
    ev = evaluate(policy, PpeSnapshot.from_observations([obs("NOT_WORN")]), w)
    assert ev.findings == ()
    assert ev.rules_cleared_for_all == frozenset({"R9"})


@pytest.mark.parametrize("origin", [Origin.REPLAY, Origin.PLANNED, Origin.WHAT_IF])
def test_r9_non_live_never_alerts(origin):
    w = world(T0, track("w-0012", T0))
    ev = evaluate(WORK, PpeSnapshot.from_observations([obs("NOT_WORN")]), w, origin=origin)
    assert len(ev.findings) == 1
    assert ev.alert_candidates() == ()


def test_vest_is_not_used():
    """조끼는 R9 에 쓰지 않는다(7.2): 안전모 WORN·조끼 NOT_WORN 이면 판정 없음."""
    person = EXAMPLE["payload"]["people"][0]  # 안전모 WORN, 조끼 NOT_WORN
    snap = PpeSnapshot.from_payloads([EXAMPLE["payload"] | {"people": [person]}])
    w = world(T0, track("w-0012", T0, revision=2))
    assert evaluate(WORK, snap, w).findings == ()


# --- 생명주기 예외 ---------------------------------------------------------------------------------------


def _pending_then_lost(policy):
    clock = FixedClock(T0)
    engine = RiskEngine([HelmetRequiredRule()], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, clock=clock, gateway_id="tier0-01", policy=policy)
    snap = PpeSnapshot.from_observations([obs("NOT_WORN")])
    transitions = []
    for i, tracks in enumerate([[track("w-0012", T0)], [], []]):
        t = T0 + 100 * i
        clock.set(t + 100)
        ev = engine.evaluate(world(t, *tracks), [], Origin.LIVE, ppe_policy=WORK, ppe=snap)
        transitions += tracker.apply(ev, world(t, *tracks), config_version="cfg-14").transitions
    return transitions


def test_pending_r9_is_not_raised_on_observation_loss():
    """지속 조건 대기 중 사람이 사라져도 R9 는 미리 발생시키지 않는다(UNKNOWN 이면 새 R9 없음, 7.2)."""
    assert _pending_then_lost(None) == []
    # 대조: 예외를 빼면 일반 규칙(관측 상실로 미리 발생)이 R9 를 낸다. 위 결과가 우연이 아님을 보인다
    raised = _pending_then_lost(LifecyclePolicy(pending_loss_exempt_rules=frozenset()))
    assert [(t.state.rule_id, t.transition) for t in raised] == [("R9", "RAISED")]
