"""world_state 조립의 위치·접지 필드 (명세 5.2 World 위치의 정의, 6.1, 계약 world_state 1.3.0).

- position_xy: 골반과 발목으로 확인한 접지점 중 더 최근 관측을 고르고, 위치·σ·t_last_valid·observation_state·속도가 모두
  그 관측 시각을 가리킨다(리뷰 F1: 현재 접지점 XY 와 과거 골반 시각을 섞지 않는다).
- ground_anchor_xyz·지지 표면은 ContactEstimator 결과(SupportContact)대로 싣고, surface_source 는 접지점이 있을 때만 싣는다(1.3.0).
"""

import pytest

from simai_ai.risk.common import Severity
from simai_ai.risk.rule import RuleContext
from simai_ai.risk.rules.r1_zone_entry import ZoneEntryRule
from simai_ai.risk.world import WorldState, WorldTrack
from simai_ai.risk.zone import Zone, ZoneJudgeType
from simai_ai.tracking.time_model import TrackTimeInput, evaluate_track
from simai_ai.tracking.world_state import SupportContact, world_state_message, world_state_payload, world_track_document
from simai_ai.tracking.world_track import IdentityConfidence, TrackLifecycle, WorldTrackFrame, WorldTrackSnapshot

T = 1_760_342_400_000
MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"


def _snapshot(**kw):
    """마지막 골반 측정이 T(X=0)이고 지금은 골반이 없는 Track. σ 는 world_track 처럼 2 m/s × 공백만큼 커져 있다."""
    t = kw.pop("t", T + 600)
    base = {
        "world_id": "w-test-0001",
        "t": t,
        "t_last_valid": T,
        "lifecycle": TrackLifecycle.OCCLUDED,
        "identity_confidence": IdentityConfidence.HIGH,
        "identity_revision": 0,
        "position_xy": (0.0, 4.0),
        "position_xy_sigma": (0.03 + 2.0 * (t - T) / 1000, 0.03 + 2.0 * (t - T) / 1000),
        "velocity": (1.5, 0.0, 0.0),
    }
    base.update(kw)
    return WorldTrackSnapshot(**base)


def _observed(t, x=0.9):
    """이번 시각 t 에 발목 두 개로 확인한 바닥 접지점."""
    return SupportContact(
        (x, 4.0, 0.0),
        (0.007, 0.007, 0.03),
        "floor",
        "VALID",
        "VALID",
        "VALID",
        surface_source="observed",
        observed_xy=(x, 4.0),
        observed_xy_sigma=(0.007, 0.007),
        observed_at=t,
    )


def test_fresh_anchor_without_pelvis_sets_all_fields_to_anchor_time(contract_errors):
    """골반이 600 ms 없지만 발목은 이번 시각 VALID: 문서 전체가 접지점 관측 시각(지금) 기준이다."""
    t = T + 600
    doc = world_track_document(_snapshot(t=t), t, _observed(t))
    assert doc["position_xy"] == [0.9, 4.0] and doc["position_xy_sigma"] == [0.007, 0.007]
    assert doc["t_last_valid"] == t and doc["observation_state"] == "OBSERVED"
    assert "velocity" not in doc  # 그 시각의 속도를 모른다 (과거 골반 필터 속도를 싣지 않는다)
    assert doc["pelvis_xyz"] is None
    assert doc["surface_source"] == "observed" and "observed_at" not in doc  # 내부 관측 시각은 계약 필드가 아니다
    payload = {"t_latest_global": t, "calibration_version": "cal-1", "scene_version": "scene-1", "tracks": [doc]}
    assert contract_errors("world_state", world_state_message(payload, msg_id=MSG_ID, sent_at=t)) == []
    # 시간 모델에 계약 정의대로 넣어도 같은 시각의 위치다 (과거 속도로 다시 옮기지 않는다)
    state = evaluate_track(
        TrackTimeInput(doc["world_id"], doc["t_last_valid"], tuple(doc["position_xy"]), tuple(doc["position_xy_sigma"]), (0.0, 0.0, 0.0)),
        t,
        t,
    )
    assert state.position_xy == pytest.approx((0.9, 4.0))


def test_fresh_anchor_keeps_r1_new_entry():
    """리뷰 F1 반례: 골반 공백 600 ms 동안 발목으로 확인한 접지점이 구역 안이면 R1 이 새 진입을 낸다(UNKNOWN 으로 건너뛰지 않음)."""
    t = T + 600
    doc = world_track_document(_snapshot(t=t), t, _observed(t))
    zone = Zone("danger", "z1", ZoneJudgeType.GROUND_FOOTPRINT, ((0.8, 3.8), (1.2, 3.8), (1.2, 4.2), (0.8, 4.2)), None, Severity.HIGH)
    ctx = RuleContext(t, WorldState(t, "cal-1", "scene-1", (WorldTrack.from_json(doc),)), (zone,))
    findings = list(ZoneEntryRule().evaluate(ctx))
    assert findings and findings[0].holding is False


def test_older_anchor_observation_ages_with_its_own_time():
    """마지막 접지점 관측(T+300)이 골반(T)보다 최근이고 지금(T+600)은 근거가 없다: 그 관측 시각 기준으로 σ 를 키운다."""
    t = T + 600
    held = SupportContact(
        (0.45, 4.0, 0.0),
        (0.607, 0.607, 0.06),
        "floor",
        "VALID",
        "UNCERTAIN",
        "UNCERTAIN",
        surface_source="inferred",
        observed_xy=(0.45, 4.0),
        observed_xy_sigma=(0.007, 0.007),
        observed_at=T + 300,
    )
    doc = world_track_document(_snapshot(t=t), t, held)
    assert doc["t_last_valid"] == T + 300 and doc["observation_state"] == "PREDICTED"
    assert doc["position_xy"] == [0.45, 4.0]
    assert doc["position_xy_sigma"] == pytest.approx([0.007 + 0.6, 0.007 + 0.6])
    assert "velocity" not in doc


def test_pelvis_wins_when_as_recent_or_newer():
    t = T + 600
    with_pelvis = _snapshot(t=t, t_last_valid=t, pelvis_xyz=(1.0, 4.0, 0.95), position_xy=(1.0, 4.0), position_xy_sigma=(0.03, 0.03))
    doc = world_track_document(with_pelvis, t, _observed(t))
    assert doc["position_xy"] == [1.0, 4.0] and doc["t_last_valid"] == t and doc["velocity"] == [1.5, 0.0, 0.0]
    old_obs = _snapshot(t=t, t_last_valid=T + 500)
    contact = SupportContact(observed_xy=(0.9, 4.0), observed_xy_sigma=(0.007, 0.007), observed_at=T + 400)
    doc = world_track_document(old_obs, t, contact)  # 접지가 지금 UNKNOWN 이고 접지점 관측이 골반보다 오래됐다
    assert doc["position_xy"] == [0.0, 4.0] and doc["t_last_valid"] == T + 500
    assert doc["ground_anchor_xyz"] is None


def test_payload_with_contacts(contract_errors):
    t = T + 600
    frame = WorldTrackFrame(t, "cal-1", (_snapshot(t=t),))
    payload = world_state_payload(frame, t, "scene-1", contacts={"w-test-0001": _observed(t)})
    assert contract_errors("world_state", world_state_message(payload, msg_id=MSG_ID, sent_at=t)) == []
    with pytest.raises(ValueError):
        world_track_document(_snapshot(t=t), t, _observed(t + 100))  # 접지점 관측이 Track 시각보다 뒤


def test_support_contact_invariants():
    anchor = ((1.0, 2.0, 0.0), (0.01, 0.01, 0.03), "floor", "VALID", "VALID", "VALID")
    with pytest.raises(ValueError):
        SupportContact(*anchor)  # 접지점이 있는데 surface_source 가 없다 (내부 불변식)
    with pytest.raises(ValueError):
        SupportContact(surface_source="inferred")  # 접지점 없이 출처만
    with pytest.raises(ValueError):
        SupportContact(*anchor, surface_source="guessed")
    with pytest.raises(ValueError):
        SupportContact(*anchor, surface_source="observed")  # observed 인데 관측 시각이 없다
    with pytest.raises(ValueError):
        SupportContact(observed_xy=(1.0, 2.0), observed_at=T)  # observed_* 는 함께
    SupportContact(*anchor, surface_source="inferred")  # 유지는 관측 기록 없이도 된다
