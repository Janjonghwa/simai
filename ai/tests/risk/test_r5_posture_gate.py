import csv
from pathlib import Path

from tests.risk.test_lifecycle import Harness, finding
from simai_ai.risk.lifecycle import LifecyclePolicy


def test_low_head_blocks_only_new_r5(track_json):
    h = Harness(track_json)
    low = track_json(head_z=0.7)
    step = h.step([finding("R5")], tracks=[low])
    assert not step.transitions


def test_active_r5_is_not_changed_by_low_posture(track_json):
    h = Harness(track_json)
    assert h.step([finding("R5")]).transitions
    h.step([finding("R5")], tracks=[track_json(head_z=0.7)])
    assert len(h.tracker.active()) == 1


def test_r4_blocks_new_r5_for_same_person(track_json):
    h = Harness(track_json)
    step = h.step([finding("R4", zone_id=None), finding("R5")])
    assert [t.state.key.rule_id for t in step.transitions] == ["R4"]


def test_fast_head_change_and_unknown_head(track_json):
    h = Harness(track_json)
    h.step(tracks=[track_json(head_z=1.7)])
    assert not h.step([finding("R5")], tracks=[track_json(head_z=1.35)], dt=500).transitions
    # 모르는 높이를 0으로 채우지 않는다.
    other = Harness(track_json)
    assert other.step([finding("R5")], tracks=[track_json(head_z=None)]).transitions


def test_recent_r4_and_other_person(track_json):
    h = Harness(track_json)
    h.step([finding("R4", zone_id=None)])
    h.step(dt=100)
    h.step(dt=3100)
    assert not h.step([finding("R5")], dt=100).transitions
    other = track_json(world_id="other")
    assert h.step([finding("R5", world_ids=("other",))], tracks=[other]).transitions


def test_recent_r4_expires_after_clear(track_json):
    h = Harness(track_json)
    h.step([finding("R4", zone_id=None)])
    h.step(dt=100)
    h.step(dt=3100)
    assert not h.step([finding("R5")], dt=100).transitions
    assert h.step([finding("R5")], dt=10_001).transitions


def test_suppression_discards_pending_frames(track_json):
    h = Harness(track_json, policy=LifecyclePolicy())
    assert not h.step([finding("R5")]).transitions
    assert h.tracker.pending()
    assert not h.step([finding("R5")], tracks=[track_json(head_z=0.7)]).transitions
    assert not h.tracker.pending()
    assert not h.step([finding("R5")], tracks=[track_json(head_z=1.7)], dt=1500).transitions


def test_recorded_fall_lifecycle_projection(track_json):
    """운영 추출의 머리 높이와 이벤트 시각을 생명주기 경계에 투영한다.

    추출에 없는 공분산·접지·관절은 기본 시험 트랙을 쓰고 규칙 판정은 실제 이벤트에서 주입한다.
    """
    rows = list(csv.DictReader((Path(__file__).with_name("r5_posture_trace.csv")).open(encoding="utf-8")))
    h = Harness(track_json)
    emitted = []
    for index, row in enumerate(rows):
        if row["head_z_validity"] != "VALID":
            continue
        t = int(row["t_ms"])
        h.clock.set(t + 100)
        track = track_json(head_z=float(row["head_z"]), position_xy=[float(row["x"]), float(row["y"])])
        rules = {0: "R5", 1: "R1", 2: "R4", 4: "R5"}
        rule = rules[index]
        step = h.step([finding(rule, zone_id=None if rule == "R4" else "z-04")], tracks=[track], dt=0)
        emitted.extend(t.state.key.rule_id for t in step.transitions if t.transition.value == "RAISED")
    assert emitted == ["R5", "R1", "R4"]


def test_posture_transition_ids_for_fall_birth_hold(track_json):
    """-852: 마지막 apply 의 자세 전이 ID(World Track 낙상 출생 보류 힌트). R4 후보(이번 R4 발생 판정)·머리 낮음은 들어가고,
    서 있는 사람은 빠지며, 감시를 내리면 비운다."""
    h = Harness(track_json)
    standing = track_json(world_id="w-stand")
    h.step(tracks=[track_json(), standing])
    assert h.tracker.posture_transition_ids() == frozenset()
    h.step([finding("R4", zone_id=None)], tracks=[track_json(), standing])
    assert h.tracker.posture_transition_ids() == {"w-0012"}
    h.step(tracks=[track_json(world_id="w-low", head_z=0.7), standing], dt=20_000)
    assert h.tracker.posture_transition_ids() == {"w-low"}
    world = h.world([standing])
    h.tracker.stand_down(world, config_version="cfg-12", evidence_ref="msg-1")
    assert h.tracker.posture_transition_ids() == frozenset()
