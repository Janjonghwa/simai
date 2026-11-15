"""외형 이어받기 판정(S15P31A101-743). 표시 전용 판정이며 Tier 0 값은 읽기만 한다."""

import pytest

from simai_ai.hmr.appearance_carry import AppearanceCarry, CarryConfig, standing_height
from simai_ai.tracking.world_track import WorldTrackConfig


def track(wid, xy, *, t_last=None, height=None, lifecycle="CONFIRMED"):
    tr = {"world_id": wid, "position_xy": list(xy), "track_lifecycle": lifecycle, "ground_anchor_xyz": [xy[0], xy[1], 0.]}
    if t_last is not None:
        tr["t_last_valid"] = t_last
    if height is not None:
        tr["head_z"], tr["head_z_validity"] = height, "VALID"
    return tr


def lose(carry, wid, xy, t, *, height=None, donor=True):
    """wid 를 t 에 보고, 다음 회차에 빠진 것으로 적는다."""
    carry.observe([track(wid, xy, t_last=t, height=height)], t)
    carry.depart(wid, f"state-{wid}", t + 200, donor=donor)


def appear(carry, wid, xy, t, others=(), *, height=None):
    tracks = [track(wid, xy, t_last=t, height=height), *others]
    carry.observe(tracks, t)
    carry.receiver(wid)
    return carry.decide(tracks, t)


def test_gate_values_follow_world_track_defaults():
    cfg, world = CarryConfig(), WorldTrackConfig()
    assert (cfg.gate_base_m, cfg.max_speed_mps, cfg.radius_max_m) == (world.gate_base_m, world.max_speed_mps, world.gate_max_m)
    assert cfg.crowd_radius_m == world.reconnect_crowd_radius_m and cfg.window_ms == world.lost_after_ms
    assert cfg.overlap_ms == world.tentative_max_gap_ms and cfg.keep_ms == world.reconnect_window_ms
    assert cfg.reach_m(0) == .5 and cfg.reach_m(250) == pytest.approx(1.0) and cfg.reach_m(3000) == 1.5


def test_single_donor_within_reach_is_carried_once():
    carry = AppearanceCarry()
    lose(carry, "w-1", (3., 2.), 1000)
    assert appear(carry, "w-7", (3.8, 2.), 1300) == [("w-7", "state-w-1")]   # 공백 0.3 s → 1.1 m 까지
    assert carry.stats == {"carried": 1} and "w-1" not in carry.departed and "w-7" not in carry.pending


def test_receiver_waits_for_donor_that_departs_later():
    """새 ID 가 옛 ID 가 아직 가림(살아 있음)일 때 생기면, 옛 ID 가 빠지는 회차에 정한다."""
    carry = AppearanceCarry()
    carry.observe([track("w-1", (3., 2.), t_last=1000)], 1000)
    occluded = track("w-1", (3., 2.), t_last=1000, lifecycle="OCCLUDED")
    assert appear(carry, "w-7", (3.2, 2.), 1100, [occluded]) == []   # 아직 빠지지 않음, 살아 있는 사람으로도 거부하지 않음
    assert "w-7" in carry.pending and not carry.stats
    carry.depart("w-1", "state-w-1", 4000, donor=True)
    tracks = [track("w-7", (3.5, 2.), t_last=4000)]
    carry.observe(tracks, 4000)
    assert carry.decide(tracks, 4000) == [("w-7", "state-w-1")]


@pytest.mark.parametrize("gap_ms, carried", [(-300, True), (-301, False), (3000, True), (3001, False)])
def test_time_window_bounds(gap_ms, carried):
    carry = AppearanceCarry()
    lose(carry, "w-1", (3., 2.), 10_000)
    assert bool(appear(carry, "w-7", (3., 2.), 10_000 + gap_ms)) is carried


def test_two_donors_in_reach_are_ambiguous():
    carry = AppearanceCarry()
    lose(carry, "w-1", (3., 2.), 1000)
    lose(carry, "w-2", (3.6, 2.), 1000)
    assert appear(carry, "w-7", (3.3, 2.), 1200) == []
    assert carry.stats == {"rejected_ambiguous_donor": 1} and "w-7" not in carry.pending


def test_two_receivers_for_one_donor_are_ambiguous():
    carry = AppearanceCarry()
    lose(carry, "w-1", (3., 2.), 1000)
    tracks = [track("w-7", (3.2, 2.), t_last=1200), track("w-8", (2.8, 2.), t_last=1200)]
    carry.observe(tracks, 1200)
    carry.receiver("w-7")
    carry.receiver("w-8")
    assert carry.decide(tracks, 1200) == []
    assert carry.stats == {"rejected_ambiguous_receiver": 2} and "w-1" in carry.departed


def test_other_live_person_or_lost_id_near_donor_rejects():
    carry = AppearanceCarry()
    carry.observe([track("w-5", (4.4, 2.), t_last=900)], 900)
    carry.decide([track("w-5", (4.4, 2.))], 900)
    lose(carry, "w-1", (3., 2.), 1000)
    assert appear(carry, "w-7", (3., 2.), 1100, [track("w-5", (4.4, 2.), t_last=1100)]) == []
    assert carry.stats["rejected_crowd"] == 1
    carry = AppearanceCarry()
    lose(carry, "w-1", (3., 2.), 1000)
    lose(carry, "w-2", (4.4, 2.), 1000, donor=False)   # 외형 없는 소실 ID 도 혼잡 근거다
    assert appear(carry, "w-7", (3., 2.), 1100) == [] and carry.stats["rejected_crowd"] == 1


@pytest.mark.parametrize("mine, theirs, carried", [(1.60, 1.74, True), (1.60, 1.76, False), (None, 1.80, True)])
def test_height_tolerance(mine, theirs, carried):
    carry = AppearanceCarry()
    lose(carry, "w-1", (3., 2.), 1000, height=theirs)
    assert bool(appear(carry, "w-7", (3., 2.), 1100, height=mine)) is carried
    if not carried:
        assert carry.stats == {"rejected_height": 1}


def test_standing_height_needs_valid_head_and_anchor():
    assert standing_height({"head_z": 1.9, "head_z_validity": "VALID", "ground_anchor_xyz": [0, 0, .2]}) == pytest.approx(1.7)
    assert standing_height({"head_z": 1.9, "head_z_validity": "UNCERTAIN", "ground_anchor_xyz": [0, 0, 0]}) is None
    assert standing_height({"head_z": 1.9, "head_z_validity": "VALID", "ground_anchor_xyz": None}) is None
    assert standing_height({"head_z": None, "ground_anchor_xyz": [0, 0, 0]}) is None


def test_donor_without_asset_is_not_offered_but_take_returns_it_for_merge_and_restore():
    carry = AppearanceCarry()
    lose(carry, "w-1", (3., 2.), 1000, donor=False)
    assert appear(carry, "w-7", (3., 2.), 1100) == []
    assert carry.take("w-1") == "state-w-1" and carry.take("w-1") is None


def test_pending_receiver_without_donor_expires_and_departed_expire_and_capacity():
    carry = AppearanceCarry(CarryConfig(max_departed=2))
    assert appear(carry, "w-7", (3., 2.), 1000) == []
    tracks = [track("w-7", (3., 2.), t_last=5400)]
    carry.decide(tracks, 1000 + 300 + 3000 + 1000)
    assert "w-7" in carry.pending
    carry.decide(tracks, 1000 + 300 + 3000 + 1001)
    assert "w-7" not in carry.pending and carry.stats["no_donor"] == 1
    for i, wid in enumerate(("a", "b", "c")):
        lose(carry, wid, (10. * i, 0.), 1000 + i)
    assert set(carry.departed) == {"b", "c"} and carry.stats["dropped_capacity"] == 1
    carry.expire(1202 + 10_000)
    assert set(carry.departed) == {"c"}


def test_split_child_never_inherits():
    carry = AppearanceCarry()
    lose(carry, "w-1", (3., 2.), 1000)
    child = {**track("w-7", (3., 2.), t_last=1100), "split_from": "w-3"}
    carry.observe([child], 1100)
    carry.receiver("w-7")
    assert carry.decide([child], 1100) == [] and carry.stats == {"rejected_split_child": 1}
