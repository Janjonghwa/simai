"""신원 불확실 정책 (S15P31A101-698 ID-5 사유별 유지 시간, ID-6 2대 경쟁 승격 조건).

- ID-5: 관측 순간 사유(2대 경쟁·애매 배정·Cross-view 불확실)는 300 ms 만 유지하고, 상태 사유(분할·재연결·중복·복원 후보)는
  1 s 를 유지한다. 스위치를 끄면 모든 사유가 1 s(이전 동작)다.
- ID-6: 2대 클러스터도 몸통 관절(어깨·엉덩이) 경쟁일 때만 신원 불확실. 손목 같은 끝 관절만의 경쟁은 신원을 올리지 않는다.
  스위치를 끄면 2대 클러스터는 어느 관절 경쟁이든 신원 불확실(이전 동작)이다. 3대 이상 규칙(엉덩이만)은 그대로다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from simai_ai.geometry.joint_gate import GateIssue
from simai_ai.geometry.rig import CameraRig
from simai_ai.tracking.world_track import IdentityFlag, WorldTrackConfig
from simai_ai.tracking.world_track import _competitor_flag, _hold_ms

_spec = importlib.util.spec_from_file_location("_wt698", Path(__file__).with_name("test_world_track.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

L_WRIST, L_SHOULDER, L_HIP = 9, 5, 11


# ------------------------------------------------------------------ ID-5


def _pulse_uncertain_ticks(**cfg) -> int:
    """확정 뒤 50 tick 동안 10 tick 마다 한 번 Cross-view 불확실 클러스터. World 불확실 tick 수."""
    rig = CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))
    m = wt._manager(**cfg)
    for k in range(10):
        t = wt.T + k * wt.DT
        m.update(wt._frame(rig, t, [wt._cluster(rig, "p", t, 3, 3)]), rig)
    n = 0
    for k in range(10, 60):
        t = wt.T + k * wt.DT
        frame = m.update(wt._frame(rig, t, [wt._cluster(rig, "p", t, 3, 3, uncertain=k % 10 == 0)]), rig)
        n += str(frame.tracks[0].identity_confidence) == "IDENTITY_UNCERTAIN"
    return n


def test_transient_reason_clears_after_300ms() -> None:
    # 사유가 붙은 tick + 300 ms 안의 3 tick = 4 tick, 5번
    assert _pulse_uncertain_ticks() == 20


def test_switch_off_keeps_one_second_hold() -> None:
    assert _pulse_uncertain_ticks(uncertain_hold_per_reason=False) == 50


def _pulse_with_neighbour(gap_m: float, **cfg) -> int:
    """두 사람(3 m, 3 m)·(3 m + gap, 3 m) 중 첫 사람에게만 10 tick 마다 Cross-view 불확실. 첫 사람의 불확실 tick 수."""
    rig = CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))
    m = wt._manager(**{"uncertain_hold_near_m": 1.0, **cfg})
    n = 0
    for k in range(60):
        t = wt.T + k * wt.DT
        pulse = k >= 10 and k % 10 == 0
        frame = m.update(wt._frame(rig, t, [wt._cluster(rig, "p", t, 3, 3, prefix="p", uncertain=pulse),
                                            wt._cluster(rig, "q", t, 3 + gap_m, 3, prefix="q")]), rig)
        if k >= 10:
            first = min(frame.tracks, key=lambda s: abs(s.position_xy[0] - 3.0))
            n += str(first.identity_confidence) == "IDENTITY_UNCERTAIN"
    return n


def test_transient_reason_near_other_person_keeps_one_second() -> None:
    # 가까이(1 m 안) 다른 사람이 있을 때 붙은 관측 순간 사유는 1 s 유지
    assert _pulse_with_neighbour(0.8) == 50
    assert WorldTrackConfig().uncertain_hold_near_m == 1.0


def test_transient_reason_far_from_others_uses_short_hold() -> None:
    assert _pulse_with_neighbour(2.5) == 20


def test_near_hold_switch_off() -> None:
    assert _pulse_with_neighbour(0.8, uncertain_hold_near_m=0.0) == 20
    assert _pulse_with_neighbour(0.8, uncertain_hold_per_reason=False) == 50
    with pytest.raises(ValueError):
        WorldTrackConfig(uncertain_hold_near_m=-1.0)


def _jump_uncertain_ticks(jump_m: float, **cfg) -> tuple[int, int]:
    """한 사람이 서 있다가 한 tick 에 jump_m 만큼 옮겨 간 뒤 그 자리에 머문다. (뛴 뒤 첫 15 tick 중 불확실 tick 수, 뛰기 전 불확실 tick 수)"""
    rig = CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))
    m = wt._manager(**cfg)
    after = before = 0
    for k in range(40):
        t = wt.T + k * wt.DT
        x = 3.0 if k < 20 else 3.0 + jump_m
        frame = m.update(wt._frame(rig, t, [wt._cluster(rig, "p", t, x, 3, prefix="p")]), rig)
        unc = any(str(s.identity_confidence) == "IDENTITY_UNCERTAIN" for s in frame.tracks)
        if 10 <= k < 20:
            before += unc
        elif 20 <= k < 35:
            after += unc
    return after, before


def test_position_jump_holds_identity_uncertain_one_second() -> None:
    # 같은 Track 이 그대로 받는 0.6 m 이동(게이트 안, 기존 로직은 표시하지 않음)도 2 m/s × 0.1 s + 0.3 m 를 넘으면
    # 위치 급변으로 보고 1 s(사유 tick + 10 tick) 동안 신원 불확실
    assert _jump_uncertain_ticks(0.6, uncertain_jump_margin_m=0.0) == (0, 0)
    after, before = _jump_uncertain_ticks(0.6, uncertain_jump_margin_m=0.3)
    assert before == 0
    assert after == 11


def test_small_move_is_not_a_jump() -> None:
    assert _jump_uncertain_ticks(0.4, uncertain_jump_margin_m=0.3) == (0, 0)


def test_jump_switch_off() -> None:
    assert _jump_uncertain_ticks(0.6, uncertain_jump_margin_m=0.3, uncertain_hold_per_reason=False) == (0, 0)


def _hidden_next_to(gap_m: float, **cfg) -> int:
    """p·q 가 gap_m 떨어져 서 있다가 p 에 사유가 한 번 붙고, 그 뒤 p 만 8 tick 동안 안 보인다. 가려진 동안 p 의 불확실 tick 수."""
    rig = CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))
    m = wt._manager(**{"uncertain_hold_near_m": 0.0, **cfg})
    n = 0
    p_id = None
    for k in range(30):
        t = wt.T + k * wt.DT
        clusters = [wt._cluster(rig, "q", t, 3 + gap_m, 3, prefix="q")]
        if not 21 <= k <= 28:
            clusters.append(wt._cluster(rig, "p", t, 3, 3, prefix="p", uncertain=k == 20))
        frame = m.update(wt._frame(rig, t, clusters), rig)
        if k == 20:
            p_id = min(frame.tracks, key=lambda s: abs(s.position_xy[0] - 3.0)).world_id
        if 21 <= k <= 28:
            p = next(s for s in frame.tracks if s.world_id == p_id)
            n += str(p.identity_confidence) == "IDENTITY_UNCERTAIN"
    return n


def test_hidden_next_to_other_keeps_existing_reason() -> None:
    # B: 가려진 채 0.8 m 옆에 다른 사람이 있으면 이미 붙은 사유를 끄지 않는다(8 tick 모두)
    assert _hidden_next_to(0.8) == 8
    # 멀리 있거나 스위치를 끄면 300 ms(3 tick) 뒤 꺼진다
    assert _hidden_next_to(2.5) == 3
    assert _hidden_next_to(0.8, uncertain_hold_hidden_m=0.0) == 3


def test_swap_suspicion_keeps_ambiguity_for_full_hold() -> None:
    # C: swap 의심 중에 붙은 관측 순간 사유는 의심 기간(uncertain_hold_ms)만큼 유지한다
    rig = CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))
    m = wt._manager()
    n = 0
    for k in range(40):
        t = wt.T + k * wt.DT
        if k == 20:
            tr = next(iter(m._tracks.values()))
            tr.swap_suspected_until = t + 1000
        frame = m.update(wt._frame(rig, t, [wt._cluster(rig, "p", t, 3, 3, prefix="p", uncertain=k == 20)]), rig)
        if k >= 20:
            n += str(frame.tracks[0].identity_confidence) == "IDENTITY_UNCERTAIN"
    assert n == 11  # 사유가 붙은 tick + 1 s(10 tick)


@pytest.mark.parametrize(
    "flag",
    [IdentityFlag.IDENTITY_SPLIT, IdentityFlag.LATE_REASSOCIATION, IdentityFlag.REASSOCIATION_AMBIGUOUS,
     IdentityFlag.POSSIBLE_DUPLICATE, IdentityFlag.RESTORED_TRACK_CANDIDATE],
)
def test_state_reasons_keep_full_hold(flag) -> None:
    assert _hold_ms(WorldTrackConfig(), flag) == 1000


@pytest.mark.parametrize(
    "flag", [IdentityFlag.TWO_VIEW_COMPETITOR_JOINT, IdentityFlag.AMBIGUOUS_TRACK_MATCH, IdentityFlag.CROSS_VIEW_UNCERTAIN]
)
def test_transient_reasons_hold(flag) -> None:
    assert _hold_ms(WorldTrackConfig(), flag) == 300
    assert _hold_ms(WorldTrackConfig(uncertain_hold_per_reason=False), flag) == 1000


# ------------------------------------------------------------------ ID-6


def _gated(*competitor_joints: int) -> SimpleNamespace:
    joints = [SimpleNamespace(reasons=[GateIssue.TWO_VIEW_COMPETITOR] if j in competitor_joints else []) for j in range(17)]
    return SimpleNamespace(identity_uncertain_candidate=bool(competitor_joints), joints=joints)


def _cluster(n_views: int) -> SimpleNamespace:
    return SimpleNamespace(views=tuple(range(n_views)))


def test_two_view_wrist_only_does_not_raise_identity() -> None:
    assert not _competitor_flag(_gated(L_WRIST), _cluster(2), WorldTrackConfig())


@pytest.mark.parametrize("joint", [L_SHOULDER, L_HIP])
def test_two_view_torso_competitor_still_raises_identity(joint) -> None:
    assert _competitor_flag(_gated(joint), _cluster(2), WorldTrackConfig())
    assert _competitor_flag(_gated(L_WRIST, joint), _cluster(2), WorldTrackConfig())


def test_two_view_switch_off_raises_on_any_joint() -> None:
    cfg = WorldTrackConfig(two_view_identity_torso_only=False)
    assert _competitor_flag(_gated(L_WRIST), _cluster(2), cfg)
    assert _competitor_flag(_gated(L_WRIST), _cluster(2), None)  # 설정 없이 부르면 이전 동작


def test_three_view_rule_unchanged() -> None:
    cfg = WorldTrackConfig()
    assert not _competitor_flag(_gated(L_WRIST), _cluster(3), cfg)
    assert not _competitor_flag(_gated(L_SHOULDER), _cluster(3), cfg)
    assert _competitor_flag(_gated(L_HIP), _cluster(3), cfg)
    assert not _competitor_flag(_gated(), _cluster(2), cfg)
