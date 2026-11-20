"""PPE 사람 연결·상태 누적 합성 장면 시험 (명세 5.5, S15P31A101-491). 순수 Python, GPU 없음.

머리 폭 40px 사람을 원본 2304×1296 픽셀에 그리고, 착용·미착용·함정(손에 든 안전모, 의자 조끼)·가림·겹침·
ID 개정·카메라 보완·깜빡임·만료를 확인한다. 수치는 config 초기값 기준이다.
"""

import dataclasses

import pytest

from simai_ai.ppe import association as pa
from simai_ai.ppe import state as ps
from simai_ai.ppe.association import FrameDetections, FrameObservations, PersonObservation, associate_frame
from simai_ai.ppe.classes import Candidate
from simai_ai.ppe.state import PpeJudge

IMAGE = (2304, 1296)
CLASS_ID = {("helmet", "WORN"): 0, ("helmet", "NOT_WORN"): 1, ("vest", "WORN"): 2, ("vest", "NOT_WORN"): 3}


def keypoints(cx, hy, conf=0.9, face_conf=None, torso_conf=None):
    face_conf = conf if face_conf is None else face_conf
    torso_conf = conf if torso_conf is None else torso_conf
    pts = [
        (cx, hy + 8, face_conf), (cx + 8, hy, face_conf), (cx - 8, hy, face_conf),
        (cx + 20, hy + 2, face_conf), (cx - 20, hy + 2, face_conf),
        (cx + 36, hy + 60, torso_conf), (cx - 36, hy + 60, torso_conf),
        (cx + 45, hy + 110, conf), (cx - 45, hy + 110, conf),
        (cx + 48, hy + 150, conf), (cx - 48, hy + 150, conf),
        (cx + 24, hy + 160, torso_conf), (cx - 24, hy + 160, torso_conf),
        (cx + 24, hy + 230, conf), (cx - 24, hy + 230, conf),
        (cx + 24, hy + 300, conf), (cx - 24, hy + 300, conf),
    ]
    return [list(p) for p in pts]


def person(wid, cx=500, hy=300, *, rev=0, local=None, ident="HIGH", source="observed", drop=(), **kp):
    kps = keypoints(cx, hy, **kp)
    for i in drop:
        kps[i] = None
    return PersonObservation(world_id=wid, identity_revision=rev, local_track_id=local or f"l-{wid}",
                             keypoints_2d=kps, identity_confidence=ident, joint_source=source)


def cand(item, state, box, conf=0.8):
    cid = CLASS_ID[(item, state)]
    return Candidate(cid, ("helmet_head", "no_helmet_head", "vest_torso", "no_vest_torso")[cid], item, state, conf,
                     tuple(float(v) for v in box))


def head_box(cx=500, hy=300):
    return [cx - 24, hy - 40, cx + 24, hy + 20]


def vest_box(cx=500, hy=300):
    return [cx - 40, hy + 55, cx + 40, hy + 165]


def HELMET(cx=500, hy=300):
    return cand("helmet", "WORN", head_box(cx, hy))


def NO_HELMET(cx=500, hy=300):
    return cand("helmet", "NOT_WORN", head_box(cx, hy))


def VEST(cx=500, hy=300):
    return cand("vest", "WORN", vest_box(cx, hy))


def NO_VEST(cx=500, hy=300):
    return cand("vest", "NOT_WORN", vest_box(cx, hy))


_seq = {}


def feed(judge, cam, t, persons, cands, *, epoch=0, det_time=None):
    seq = _seq[(cam, epoch)] = _seq.get((cam, epoch), 0) + 1
    frame = FrameObservations(cam, epoch, seq, t, IMAGE, tuple(persons))
    det = FrameDetections(cam, epoch, seq, t if det_time is None else det_time, IMAGE, tuple(cands))
    assoc = associate_frame(frame, det, judge.cfg)
    judge.ingest(assoc)
    return assoc


def t_of(b, off=10):
    return b * 500 + off


def closed_after(b):
    """칸 b 까지 닫히는 최소 평가 시각."""
    return (b + 1) * 500 + 100


def state(judge, now, wid="w1", item="helmet"):
    return judge.evaluate(now)[wid][item]


# ---- 영역 ------------------------------------------------------------------------------------
def test_head_and_torso_regions_contain_synthetic_equipment():
    kps = keypoints(500, 300)
    head, torso = pa.head_region(kps, IMAGE), pa.torso_region(kps, IMAGE)
    assert head.clear and torso.clear and 0 < head.quality <= 1
    assert pa.overlap_scores("helmet", tuple(head_box()), head)[1]
    assert pa.overlap_scores("vest", tuple(vest_box()), torso)[1]


def test_low_confidence_face_points_give_no_head_region():
    assert pa.head_region(keypoints(500, 300, face_conf=0.1), IMAGE) is None


def test_single_ear_and_eye_side_view_gives_unclear_region():
    head = pa.head_region(person("w1", drop=(pa.NOSE, pa.L_EYE, pa.L_EAR)).keypoints_2d, IMAGE)
    assert head is not None and not head.clear   # 2점뿐이라 미착용 근거로는 쓰지 않는다


# ---- 정상 ------------------------------------------------------------------------------------
def test_worn_after_full_window_not_before():
    j = PpeJudge()
    for b in range(5):
        feed(j, "cam1", t_of(b), [person("w1")], [HELMET(), VEST()])
    v = state(j, closed_after(4))
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_ACCUMULATING)
    feed(j, "cam1", t_of(5), [person("w1")], [HELMET(), VEST()])
    views = j.evaluate(closed_after(5))
    assert views["w1"]["helmet"].state == "WORN" and views["w1"]["vest"].state == "WORN"
    assert views["w1"]["helmet"].expires_at == t_of(5) + 10_000
    assert views["w1"]["helmet"].evidence[0].camera_id == "cam1"
    assert views["w1"]["helmet"].support_ratio == 1.0 and views["w1"]["helmet"].valid_cells == 6


def test_not_worn_needs_all_six_clear_bins():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1")], [NO_HELMET(), NO_VEST()])
    views = j.evaluate(closed_after(5))
    assert views["w1"]["helmet"].state == "NOT_WORN" and views["w1"]["vest"].state == "NOT_WORN"


def _not_worn_bins(j, bins):
    """bins 의 칸에만 미착용 프레임을 넣는다(빠진 칸은 표본 없는 빈 칸 = 프레임 드롭·추론 건너뜀)."""
    for b in bins:
        feed(j, "cam1", t_of(b), [person("w1")], [NO_HELMET(), NO_VEST()])


def test_not_worn_tolerates_one_empty_bin():
    """S15P31A101-654: 6칸 중 빈 칸 1칸은 허용한다(14차 로비: 빈 칸 하나에 창 전체가 리셋돼 미착용이 확정되지 않았다)."""
    j = PpeJudge()
    _not_worn_bins(j, [0, 1, 3, 4, 5])
    views = j.evaluate(closed_after(5))
    assert views["w1"]["helmet"].state == "NOT_WORN" and views["w1"]["vest"].state == "NOT_WORN"
    assert views["w1"]["helmet"].valid_cells == 5 and views["w1"]["helmet"].evidence


def test_not_worn_rejects_two_empty_bins():
    j = PpeJudge()
    _not_worn_bins(j, [0, 2, 4, 5])
    assert state(j, closed_after(5)).state == "UNKNOWN"


def test_not_worn_needs_last_bin_not_worn():
    """빈 칸이 마지막이면 아직 확정하지 않고, 다음 칸이 미착용이면 그때 확정한다(근거 시각이 마지막 미착용 칸)."""
    j = PpeJudge()
    _not_worn_bins(j, [0, 1, 2, 3, 4])
    assert state(j, closed_after(5)).state == "UNKNOWN"
    _not_worn_bins(j, [6])
    v = state(j, closed_after(6))
    assert v.state == "NOT_WORN" and v.last_evidence_at == t_of(6)


def test_empty_bin_plus_neutral_bin_is_allowed_since_0_9():
    """빈 칸 1칸과 판정 없는(검출 없음) 칸 1칸이 함께여도 미착용이다(0.9.0, S15P31A101-818). 0.8.0 규칙에서는 막혔다."""
    for cfg, want in ((pa.DEFAULT_CONFIG, "NOT_WORN"),
                      (dataclasses.replace(pa.DEFAULT_CONFIG, not_worn_max_neutral_bins=0), "UNKNOWN")):
        j = PpeJudge(cfg)
        _not_worn_bins(j, [0, 3, 4, 5])
        feed(j, "cam1", t_of(2), [person("w1")], [])
        assert state(j, closed_after(5)).state == want


def test_missing_detection_is_not_not_worn_evidence():
    """검출 없음만으로는 미착용이 되지 않고, 미착용 근거 시각도 늘리지 않는다(창에 끼는 것만 허용, S15P31A101-818)."""
    j = PpeJudge()
    for b in range(20):
        feed(j, "cam1", t_of(b), [person("w1")], [])
    v = state(j, closed_after(19))
    assert (v.state, v.reason) == ("UNKNOWN", pa.R_NO_DETECTION)
    k = PpeJudge()
    for b in range(6):
        feed(k, "cam1", t_of(b), [person("w1")], [NO_HELMET()] if b != 3 else [])
    v = state(k, closed_after(5))
    assert v.state == "NOT_WORN" and v.last_evidence_at == t_of(5)
    for b in range(6, 30):
        feed(k, "cam1", t_of(b), [person("w1")], [])
    assert state(k, closed_after(29)).state == "UNKNOWN"   # 마지막 미착용 근거 + 10초가 지나 만료


def test_not_worn_requires_clear_head():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1", drop=(pa.NOSE, pa.L_EAR, pa.R_EAR))], [NO_HELMET()])
    v = state(j, closed_after(5))
    assert (v.state, v.reason) == ("UNKNOWN", pa.R_REGION_NOT_CLEAR)


# ---- 모호함 ----------------------------------------------------------------------------------
def test_one_helmet_over_two_overlapping_people_is_unknown():
    j = PpeJudge()
    people = [person("w1", 500), person("w2", 540)]
    for b in range(8):
        a = feed(j, "cam1", t_of(b), people, [cand("helmet", "WORN", [496, 260, 544, 320])])
    assert {s.reason for s in a.samples if s.item == "helmet"} == {pa.R_SHARED_DETECTION}
    views = j.evaluate(closed_after(7))
    for wid in ("w1", "w2"):
        assert (views[wid]["helmet"].state, views[wid]["helmet"].reason) == ("UNKNOWN", pa.R_SHARED_DETECTION)


def test_worn_and_not_worn_on_same_head_is_unknown():
    j = PpeJudge()
    for b in range(8):
        feed(j, "cam1", t_of(b), [person("w1")], [HELMET(), cand("helmet", "NOT_WORN", head_box(), 0.6)])
    v = state(j, closed_after(7))
    assert (v.state, v.reason) == ("UNKNOWN", pa.R_CONFLICTING_CLASSES)


def test_well_separated_people_each_get_own_equipment():
    j = PpeJudge()
    people = [person("w1", 500), person("w2", 900)]
    for b in range(6):
        feed(j, "cam1", t_of(b), people, [HELMET(500), NO_HELMET(900)])
    views = j.evaluate(closed_after(5))
    assert views["w1"]["helmet"].state == "WORN" and views["w2"]["helmet"].state == "NOT_WORN"


# ---- 함정(G9: 손에 든 안전모·의자 조끼 WORN 오판 0건, 머리 가림은 UNKNOWN) -----------------------
def test_helmet_held_in_hand_is_not_worn():
    j = PpeJudge()
    hand = cand("helmet", "WORN", [530, 430, 578, 480])
    for b in range(6):
        a = feed(j, "cam1", t_of(b), [person("w1")], [NO_HELMET(), hand])
    assert len(a.orphans) == 1
    assert state(j, closed_after(5)).state == "NOT_WORN"


def test_helmet_in_hand_without_head_detection_never_worn():
    j = PpeJudge()
    for b in range(8):
        feed(j, "cam1", t_of(b), [person("w1")], [cand("helmet", "WORN", [530, 430, 578, 480])])
    assert state(j, closed_after(7)).state == "UNKNOWN"


def test_helmet_beside_head_partially_overlapping_blocks_not_worn():
    j = PpeJudge()
    for b in range(8):
        feed(j, "cam1", t_of(b), [person("w1")], [NO_HELMET(), cand("helmet", "WORN", [520, 250, 580, 310])])
    v = state(j, closed_after(7))
    assert (v.state, v.reason) == ("UNKNOWN", pa.R_PARTIAL_OVERLAP)


def test_vest_on_chair_is_not_counted():
    j = PpeJudge()
    for b in range(6):
        a = feed(j, "cam1", t_of(b), [person("w1")], [cand("vest", "WORN", [900, 600, 980, 700]), NO_VEST()])
    assert len(a.orphans) == 1
    assert state(j, closed_after(5), item="vest").state == "NOT_WORN"


def test_occluded_head_is_unknown_even_with_no_helmet_box():
    j = PpeJudge()
    for b in range(8):
        feed(j, "cam1", t_of(b), [person("w1", face_conf=0.1)], [NO_HELMET()])
    v = state(j, closed_after(7))
    assert (v.state, v.reason) == ("UNKNOWN", pa.R_REGION_NOT_OBSERVABLE)


def test_inferred_keypoints_are_not_used():
    j = PpeJudge()
    for b in range(8):
        feed(j, "cam1", t_of(b), [person("w1", source="inferred")], [HELMET()])
    v = state(j, closed_after(7))
    assert (v.state, v.reason) == ("UNKNOWN", pa.R_KEYPOINTS_NOT_OBSERVED)


# ---- 시간·ID ----------------------------------------------------------------------------------
def _worn(j, bins=range(6), **kw):
    for b in bins:
        feed(j, "cam1", t_of(b), [person("w1", **kw)], [HELMET()])


def test_expires_ten_seconds_after_last_valid_evidence():
    j = PpeJudge()
    _worn(j)
    last = t_of(5)
    assert state(j, last + 10_000).state == "WORN"
    v = state(j, last + 10_001)
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_EXPIRED)
    assert state(j, last + 10_600).state == "UNKNOWN"


def test_identity_revision_change_resets_without_carry_over():
    j = PpeJudge()
    _worn(j)
    assert state(j, closed_after(5)).state == "WORN"
    feed(j, "cam1", t_of(6), [person("w1", rev=1)], [HELMET()])
    v = state(j, closed_after(6))
    assert (v.state, v.reason, v.evidence) == ("UNKNOWN", ps.R_REVISION_CHANGED, ())
    _worn(j, range(7, 11), rev=1)
    assert state(j, closed_after(10)).state == "UNKNOWN"
    feed(j, "cam1", t_of(11), [person("w1", rev=0)], [HELMET()])
    assert j.dropped.get("STALE_IDENTITY_REVISION") == 1
    feed(j, "cam2", t_of(11, 40), [person("w1", rev=1, local="c2")], [HELMET()])
    assert (state(j, closed_after(11)).state, j.tracks["w1"].identity_revision) == ("WORN", 1)


def test_local_id_handover_between_world_ids_resets_both():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1", local="7"), person("w2", 900, local="8")], [HELMET(), HELMET(900)])
    views = j.evaluate(closed_after(5))
    assert (views["w1"]["helmet"].state, views["w2"]["helmet"].state) == ("WORN", "WORN")
    feed(j, "cam1", t_of(6), [person("w2", 500, local="7")], [HELMET()])
    views = j.evaluate(closed_after(6))
    for wid in ("w1", "w2"):
        assert (views[wid]["helmet"].state, views[wid]["helmet"].reason) == ("UNKNOWN", ps.R_MAPPING_CHANGED)


# ---- 순수 병합 승계(S15P31A101-661: aliases 에 새 ID 가 붙고 분할 아님이면 차수가 올라도 잇는다) --------------------
def merged(wid="w1", *, rev=1, aliases=("w2",), split_from=None, split_source=False, **kw):
    return dataclasses.replace(person(wid, rev=rev, **kw), aliases=tuple(aliases), split_from=split_from,
                               split_source=split_source)


def test_pure_merge_keeps_survivor_state_and_window():
    j = PpeJudge()
    _worn(j)
    assert state(j, closed_after(5)).state == "WORN"
    feed(j, "cam1", t_of(6), [merged()], [HELMET()])
    v = state(j, closed_after(6))
    assert (v.state, v.reason, j.tracks["w1"].identity_revision) == ("WORN", ps.R_WINDOW_WORN, 1)
    assert v.last_evidence_at == t_of(6)   # 병합 뒤 착용 칸이 그대로 근거로 이어진다
    assert j.identity_events == {"merge_carried": 1}


def test_absorbed_track_window_moves_to_empty_survivor_and_absorbed_id_is_retired():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w2", local="8")], [HELMET()])
    assert state(j, closed_after(5), "w2").state == "WORN"
    # 소실됐던 옛 ID w1 이 w2 를 흡수(남는 ID 를 PPE 가 처음 본다)
    feed(j, "cam1", t_of(6), [merged("w1", rev=3, local="8")], [HELMET()])
    views = j.evaluate(closed_after(6))
    assert "w2" not in views and (views["w1"]["helmet"].state, views["w1"]["helmet"].reason) == ("WORN",
                                                                                                ps.R_WINDOW_WORN)
    assert views["w1"]["helmet"].reason != ps.R_MAPPING_CHANGED
    feed(j, "cam2", t_of(7), [person("w2", local="c2")], [HELMET()])   # 흡수된 ID 의 늦은 관측은 버린다
    assert j.dropped.get("RETIRED_WORLD_ID") == 1 and "w2" not in j.tracks


def test_absorbed_window_moves_into_known_but_empty_survivor():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1", local="7", drop=(1, 2, 3, 4)), person("w2", 900, local="8")],
             [HELMET(900)])
    views = j.evaluate(closed_after(5))
    assert (views["w1"]["helmet"].state, views["w2"]["helmet"].state) == ("UNKNOWN", "WORN")
    feed(j, "cam1", t_of(6), [merged("w1", cx=900, local="8")], [HELMET(900)])
    v = state(j, closed_after(6))
    assert (v.state, v.reason) == ("WORN", ps.R_WINDOW_WORN)


def test_local_id_taken_over_from_absorbed_id_is_not_a_mapping_change():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1", local="7"), person("w2", 900, local="8")], [HELMET(), HELMET(900)])
    feed(j, "cam1", t_of(6), [merged("w1", cx=900, local="8")], [HELMET(900)])
    v = state(j, closed_after(6))
    assert (v.state, v.reason) == ("WORN", ps.R_WINDOW_WORN)


def test_opposite_confirmed_states_on_merge_become_unknown():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1", local="7"), person("w2", 900, local="8")], [HELMET(), NO_HELMET(900)])
    views = j.evaluate(closed_after(5))
    assert (views["w1"]["helmet"].state, views["w2"]["helmet"].state) == ("WORN", "NOT_WORN")
    feed(j, "cam1", t_of(6), [merged("w1")], [])
    v = state(j, closed_after(6))
    assert (v.state, v.reason, v.evidence) == ("UNKNOWN", ps.R_CROSS_CAMERA_CONFLICT, ())


def test_overlapping_bins_on_merge_are_conservative():
    item_a, item_b = ps.ItemState(window_start_bin=0), ps.ItemState(window_start_bin=0)
    item_a.results = {0: ps.B_WORN, 1: ps.B_WORN, 2: ps.B_EMPTY}
    item_b.results = {0: ps.B_WORN, 1: ps.B_NOT_WORN, 2: ps.B_WORN, 3: ps.B_CONFLICT}
    out = ps._carry_item(item_a, item_b, 4)
    assert out.results == {0: ps.B_WORN, 1: ps.B_UNKNOWN, 2: ps.B_WORN, 3: ps.B_CONFLICT}
    assert item_a.results[1] == ps.B_WORN   # 원본은 바꾸지 않는다


def test_split_revision_change_resets():
    j = PpeJudge()
    _worn(j)
    # 분할: 원래 ID 는 별칭 변화 없이 차수만 오른다
    feed(j, "cam1", t_of(6), [merged(aliases=(), split_source=True)], [HELMET()])
    v = state(j, closed_after(6))
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_REVISION_CHANGED)
    assert j.identity_events == {"revision_reset": 1}


def test_merge_overlapping_a_split_at_the_same_revision_resets():
    j = PpeJudge()
    _worn(j)
    feed(j, "cam1", t_of(6), [merged(split_source=True)], [HELMET()])
    assert state(j, closed_after(6)).state == "UNKNOWN"


def test_new_split_from_resets_even_with_new_alias():
    j = PpeJudge()
    _worn(j)
    feed(j, "cam1", t_of(6), [merged(split_from="w9")], [HELMET()])
    assert state(j, closed_after(6)).state == "UNKNOWN"


def test_short_uncertainty_after_pure_merge_keeps_state_but_long_one_resets():
    j = PpeJudge()
    _worn(j)
    for b in (6, 7, 8):   # 병합 직후 LATE_REASSOCIATION 불확실 1.5초: 상태 유지
        feed(j, "cam1", t_of(b), [merged(ident=UNCERTAIN)], [HELMET()])
    assert state(j, closed_after(8)).state == "WORN"
    j2 = PpeJudge()
    _worn(j2)
    for b in range(6, 14):   # 3초를 넘게 이어지면 지금처럼 초기화
        feed(j2, "cam1", t_of(b), [merged(ident=UNCERTAIN)], [HELMET()])
    v = state(j2, closed_after(13))
    assert (v.state, v.reason) == ("UNKNOWN", pa.R_IDENTITY_UNCERTAIN)


# ---- ID 불확실(S15P31A101-653: 표본은 버리고 확정 상태는 유지, 3초 초과면 초기화) ---------------------------
UNCERTAIN = "IDENTITY_UNCERTAIN"


def test_short_identity_uncertainty_keeps_state_without_extending_expiry():
    j = PpeJudge()
    _worn(j)
    for b in (6, 7):   # 1초 불확실(착용 검출이 있어도 근거로 쓰지 않는다)
        feed(j, "cam1", t_of(b), [person("w1", ident=UNCERTAIN)], [HELMET()])
    v = state(j, closed_after(7))
    assert (v.state, v.reason) == ("WORN", ps.R_WINDOW_WORN)
    assert v.last_evidence_at == t_of(5) and v.expires_at == t_of(5) + 10_000   # 불확실 칸이 만료를 늘리지 않는다
    assert state(j, t_of(5) + 10_001).state == "UNKNOWN"


def test_identity_uncertainty_longer_than_reset_threshold_resets():
    j = PpeJudge()
    _worn(j)
    for b in range(6, 14):   # 6칸 시작 ~ 13칸: 첫 불확실 프레임 뒤 3.5초 > 3초
        feed(j, "cam1", t_of(b), [person("w1", ident=UNCERTAIN)], [HELMET()])
    v = state(j, closed_after(13))
    assert (v.state, v.reason, v.evidence) == ("UNKNOWN", pa.R_IDENTITY_UNCERTAIN, ())


def test_uncertainty_exactly_at_threshold_does_not_reset():
    j = PpeJudge()
    _worn(j)
    for b in range(6, 13):   # 첫 불확실 뒤 3.0초 — 넘지 않았다
        feed(j, "cam1", t_of(b), [person("w1", ident=UNCERTAIN)], [HELMET()])
    assert state(j, closed_after(12)).state == "WORN"


def test_reset_threshold_restarts_after_a_certain_frame():
    j = PpeJudge()
    _worn(j)
    for b in range(6, 11):   # 2초 불확실
        feed(j, "cam1", t_of(b), [person("w1", ident=UNCERTAIN)], [HELMET()])
    feed(j, "cam1", t_of(11), [person("w1")], [HELMET()])   # 확실한 프레임이 끊는다
    for b in range(12, 17):   # 다시 2초 불확실 — 합치면 3초를 넘지만 이어지지 않았다
        feed(j, "cam1", t_of(b), [person("w1", ident=UNCERTAIN)], [HELMET()])
    v = state(j, closed_after(16))
    assert v.state == "WORN" and v.last_evidence_at == t_of(11)


def test_identity_revision_change_still_resets_immediately_even_inside_uncertainty():
    j = PpeJudge()
    _worn(j)
    feed(j, "cam1", t_of(6), [person("w1", ident=UNCERTAIN)], [HELMET()])
    feed(j, "cam1", t_of(7), [person("w1", rev=1, ident=UNCERTAIN)], [HELMET()])
    v = state(j, closed_after(7))
    assert (v.state, v.evidence) == ("UNKNOWN", ())


def test_uncertain_bins_are_skipped_so_six_valid_bins_within_span_still_decide():
    j = PpeJudge()
    # 유효 칸 0·1·2, 불확실 칸 3·4, 유효 칸 5·6·7 → 최근 10칸 안 유효 6칸이 모두 미착용
    for b in range(8):
        ident = UNCERTAIN if b in (3, 4) else "HIGH"
        feed(j, "cam1", t_of(b), [person("w1", ident=ident)], [NO_HELMET()])
    v = state(j, closed_after(7))
    assert (v.state, v.reason, v.valid_cells) == ("NOT_WORN", ps.R_WINDOW_NOT_WORN, 6)


def test_valid_bins_scattered_beyond_span_do_not_decide():
    j = PpeJudge()
    # 유효 칸이 0·1·2 와 9·10·11 로 흩어지면 최근 10칸(2~11) 안에는 4개뿐이다
    for b in range(12):
        ident = "HIGH" if b in (0, 1, 2, 9, 10, 11) else UNCERTAIN   # 불확실 3~8칸: 첫·끝 프레임 간격 2.5초
        feed(j, "cam1", t_of(b), [person("w1", ident=ident)], [NO_HELMET()])
    assert state(j, closed_after(11)).state == "UNKNOWN"


def test_detections_in_uncertain_bins_are_never_evidence():
    j = PpeJudge()
    for b in range(6):   # 착용 검출이 6칸 내내 있어도 모두 불확실이면 확정하지 않는다
        feed(j, "cam1", t_of(b), [person("w1", ident=UNCERTAIN)], [HELMET()])
    v = state(j, closed_after(5))
    assert (v.state, v.reason, v.valid_cells) == ("UNKNOWN", pa.R_IDENTITY_UNCERTAIN, 0)
    # 다른 카메라의 확실한 프레임이 같은 칸에 있어도 그 칸은 건너뛴다
    j2 = PpeJudge()
    for b in range(6):
        feed(j2, "cam1", t_of(b), [person("w1", ident=UNCERTAIN if b == 2 else "HIGH")], [HELMET()])
        feed(j2, "cam2", t_of(b, off=60), [person("w1", local="c2")], [HELMET()])
    assert state(j2, closed_after(5)).state == "UNKNOWN"   # 칸 2 를 빼면 유효 5칸뿐
    feed(j2, "cam1", t_of(6), [person("w1")], [HELMET()])
    assert state(j2, closed_after(6)).state == "WORN"


def test_new_stream_epoch_clears_local_mapping_and_old_epoch_is_dropped():
    j = PpeJudge()
    feed(j, "cam1", t_of(0), [person("w1", local="1")], [HELMET()], epoch=0)
    feed(j, "cam1", t_of(1), [person("w2", 900, local="1")], [HELMET(900)], epoch=1)
    assert j.tracks["w1"].items["helmet"].reason != ps.R_MAPPING_CHANGED
    feed(j, "cam1", t_of(2), [person("w1", local="1")], [HELMET()], epoch=0)
    assert j.dropped.get("STALE_STREAM_EPOCH") == 1


def test_frame_mismatch_is_rejected():
    j = PpeJudge()
    a = feed(j, "cam1", t_of(0), [person("w1")], [HELMET()], det_time=t_of(0) + 33)
    assert a.rejected == "CAPTURE_TIME_MISMATCH" and a.samples == []
    assert j.dropped.get("CAPTURE_TIME_MISMATCH") == 1


def test_late_frame_after_bin_closed_is_dropped():
    j = PpeJudge()
    _worn(j, range(3))
    j.evaluate(closed_after(2))
    feed(j, "cam2", t_of(2, 400), [person("w1", local="c2")], [HELMET()])
    assert j.dropped.get("LATE_FRAME") == 1


# ---- 여러 카메라 ------------------------------------------------------------------------------
def test_other_camera_fills_occluded_view():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1", face_conf=0.1, local="a")], [NO_HELMET()])
        feed(j, "cam2", t_of(b, 60), [person("w1", 1200, local="b")], [HELMET(1200)])
    v = state(j, closed_after(5))
    assert v.state == "WORN" and {e.camera_id for e in v.evidence} == {"cam2"}


def test_cameras_alternating_still_make_one_sample_per_bin():
    j = PpeJudge()
    for b in range(6):
        cam = "cam1" if b % 2 == 0 else "cam2"
        feed(j, cam, t_of(b, 50), [person("w1", local=cam)], [HELMET()])
    assert state(j, closed_after(5)).state == "WORN"


def test_cross_camera_contradiction_is_unknown():
    """미착용 카메라가 2대 이상이면 착용 카메라가 있어도 충돌 칸이다(S15P31A101-766)."""
    j = PpeJudge()
    for b in range(8):
        feed(j, "cam1", t_of(b), [person("w1", local="a")], [HELMET()])
        feed(j, "cam2", t_of(b, 40), [person("w1", 1200, local="b")], [NO_HELMET(1200)])
        feed(j, "cam3", t_of(b, 60), [person("w1", 300, local="c")], [NO_HELMET(300)])
    v = state(j, closed_after(7))
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_CROSS_CAMERA_CONFLICT)


# ---- 칸 결합: 미착용 카메라 1대는 충돌이 아니다(S15P31A101-766, ppe-policy-0.7.0) ---------------------------------
def _s(cam, kind, t=1000):
    return pa.Sample(world_id="w1", identity_revision=0, item="vest", kind=kind, reason="R", camera_id=cam,
                     stream_epoch=0, frame_sequence=1, capture_time_ms=t, local_track_id=f"l-{cam}",
                     confidence=0.8, region_quality=0.9)


def test_one_not_worn_camera_among_worn_is_worn_bin():
    j = PpeJudge()
    result, support = j.combine_bin([_s("cam1", pa.S_WORN), _s("cam2", pa.S_NOT_WORN, 1020)])
    assert result == ps.B_WORN and {s.camera_id for s in support} == {"cam1"}   # 근거에는 착용 표본만
    result, _ = j.combine_bin([_s("cam1", pa.S_WORN), _s("cam2", pa.S_WORN), _s("cam3", pa.S_NOT_WORN)])
    assert result == ps.B_WORN


def test_two_not_worn_cameras_with_worn_is_conflict():
    j = PpeJudge()
    result, support = j.combine_bin([_s("cam1", pa.S_WORN), _s("cam2", pa.S_NOT_WORN), _s("cam3", pa.S_NOT_WORN)])
    assert result == ps.B_CONFLICT and len(support) == 3
    result, _ = j.combine_bin([_s("cam1", pa.S_WORN), _s("cam2", pa.S_WORN),
                               _s("cam3", pa.S_NOT_WORN), _s("cam4", pa.S_NOT_WORN)])
    assert result == ps.B_CONFLICT


def test_same_camera_both_states_stays_conflict():
    """병합으로 합친 칸에서 같은 카메라가 두 상태를 함께 냈으면 그대로 충돌이다."""
    j = PpeJudge()
    assert j.combine_bin([_s("cam1", pa.S_WORN), _s("cam1", pa.S_NOT_WORN)])[0] == ps.B_CONFLICT


def test_threshold_one_restores_previous_rule():
    j = PpeJudge(dataclasses.replace(pa.DEFAULT_CONFIG, conflict_min_not_worn_cameras=1))
    assert j.combine_bin([_s("cam1", pa.S_WORN), _s("cam2", pa.S_NOT_WORN)])[0] == ps.B_CONFLICT


def test_occasional_single_camera_not_worn_keeps_worn():
    """2026-11-16 로비 검은 조끼: 한 카메라가 가끔 미착용을 내도 착용 확정이 끊기지 않는다."""
    def run(cfg):
        j = PpeJudge(cfg)
        for b in range(12):
            feed(j, "cam1", t_of(b), [person("w1", local="a")], [VEST()])
            feed(j, "cam2", t_of(b, 30), [person("w1", 1200, local="b")], [VEST(1200)])
            odd = [NO_VEST(300)] if b % 3 == 2 else [VEST(300)]   # cam3 가 세 칸에 한 번 미착용
            feed(j, "cam3", t_of(b, 60), [person("w1", 300, local="c")], odd)
        return j.evaluate(closed_after(11))["w1"]["vest"]

    v = run(pa.DEFAULT_CONFIG)
    assert (v.state, v.reason) == ("WORN", ps.R_WINDOW_WORN)
    old = run(dataclasses.replace(pa.DEFAULT_CONFIG, conflict_min_not_worn_cameras=1))
    assert (old.state, old.reason) == ("UNKNOWN", ps.R_CROSS_CAMERA_CONFLICT)   # 0.6.0 규칙이면 끊긴다


def test_second_frame_same_camera_same_bin_is_not_double_counted():
    j = PpeJudge()
    feed(j, "cam1", t_of(0, 10), [person("w1")], [HELMET()])
    feed(j, "cam1", t_of(0, 260), [person("w1")], [HELMET()])
    assert j.dropped.get("SECOND_SAMPLE_SAME_CAMERA_BIN") == 2   # 안전모·조끼 표본


# ---- 깜빡임 ----------------------------------------------------------------------------------
def test_single_contrary_bin_goes_unknown_not_not_worn_then_rebuilds():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1")], [HELMET()])
    for b in range(6, 10):
        feed(j, "cam1", t_of(b), [person("w1")], [])
    assert state(j, closed_after(9)).state == "WORN"
    feed(j, "cam1", t_of(10), [person("w1")], [NO_HELMET()])
    v = state(j, closed_after(10))
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_CONTRARY)
    for b in range(11, 15):
        feed(j, "cam1", t_of(b), [person("w1")], [HELMET()])
    assert state(j, closed_after(14)).state == "UNKNOWN"
    feed(j, "cam1", t_of(15), [person("w1")], [HELMET()])
    assert state(j, closed_after(15)).state == "WORN"


def test_alternating_evidence_never_settles():
    j = PpeJudge()
    seen = set()
    for b in range(30):
        feed(j, "cam1", t_of(b), [person("w1")], [HELMET() if b % 2 else NO_HELMET()])
        seen.add(state(j, closed_after(b)).state)
    assert seen == {"UNKNOWN"}


def test_one_missed_worn_bin_in_six_still_worn():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1")], [] if b == 2 else [HELMET()])
    v = state(j, closed_after(5))
    assert v.state == "WORN" and (v.valid_cells, v.support_ratio) == (5, 1.0)


def test_never_jumps_directly_between_worn_and_not_worn():
    j = PpeJudge()
    prev, transitions = "UNKNOWN", []
    for b, worn in enumerate([1] * 8 + [0] * 8 + [1] * 8):
        feed(j, "cam1", t_of(b), [person("w1")], [HELMET() if worn else NO_HELMET()])
        cur = state(j, closed_after(b)).state
        if cur != prev:
            transitions.append((prev, cur))
        prev = cur
    assert ("WORN", "NOT_WORN") not in transitions and ("NOT_WORN", "WORN") not in transitions
    assert transitions[:2] == [("UNKNOWN", "WORN"), ("WORN", "UNKNOWN")]
    assert ("UNKNOWN", "NOT_WORN") in transitions


@pytest.fixture(autouse=True)
def _fresh_sequences():
    _seq.clear()


# ---- ID 초기화 사유는 초기화 칸까지만(S15P31A101-729, ppe-policy-0.5.0) ---------------------------------------------
def _mapping_reset():
    """w1·w2 가 WORN 인 상태에서 cam1 local 7 이 w1 → w2 로 넘어가 둘 다 칸 6 에서 초기화된다."""
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1", local="7"), person("w2", 900, local="8")], [HELMET(), HELMET(900)])
    feed(j, "cam1", t_of(6), [person("w2", 500, local="7")], [HELMET()])
    views = j.evaluate(closed_after(6))
    assert (views["w2"]["helmet"].state, views["w2"]["helmet"].reason) == ("UNKNOWN", ps.R_MAPPING_CHANGED)
    return j


def test_mapping_reset_reason_gives_way_to_empty_bin_reason_and_is_counted():
    j = _mapping_reset()
    assert j.identity_events.get("mapping_reset") == 2   # 두 Track 모두 초기화
    v = state(j, closed_after(7), "w2")   # 칸 7: w2 표본 없음
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_NO_OBSERVATION)


def test_mapping_reset_reason_gives_way_to_accumulating_then_confirms():
    j = _mapping_reset()
    feed(j, "cam1", t_of(7), [person("w2", 500, local="7")], [HELMET()])
    v = state(j, closed_after(7), "w2")
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_ACCUMULATING)
    for b in range(8, 12):
        feed(j, "cam1", t_of(b), [person("w2", 500, local="7")], [HELMET()])
    assert state(j, closed_after(11), "w2").state == "WORN"   # 확정 시점은 이전과 같다(칸 6~11)


def test_revision_reset_reason_gives_way_to_identity_uncertain_bin():
    j = PpeJudge()
    _worn(j)
    feed(j, "cam1", t_of(6), [person("w1", rev=1)], [HELMET()])
    assert state(j, closed_after(6)).reason == ps.R_REVISION_CHANGED
    feed(j, "cam1", t_of(7), [person("w1", rev=1, ident="IDENTITY_UNCERTAIN")], [HELMET()])
    v = state(j, closed_after(7))
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_IDENTITY_UNCERTAIN)


def test_contrary_reason_still_stays_until_confirmed():
    j = PpeJudge()
    _worn(j)
    feed(j, "cam1", t_of(6), [person("w1")], [NO_HELMET()])
    for b in range(7, 9):
        feed(j, "cam1", t_of(b), [person("w1")], [])
    v = state(j, closed_after(8))
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_CONTRARY)


def test_policy_version_marks_current_rules():
    from simai_ai.ppe.config import POLICY_VERSION
    assert POLICY_VERSION == "ppe-policy-0.9.0"


# ---- 머리 폭 하한 = 어깨 폭 × 0.6 (S15P31A101-737, ppe-policy-0.6.0) ----------------------------------------------
def _narrow_face(cx=500, hy=300, eye_half=3, shoulder_conf=0.9):
    """옆·뒷모습처럼 귀가 안 보이고 눈 간격이 좁은 사람(눈·귀 기준 머리 폭 = 눈 간격 × 2.5)."""
    kps = keypoints(cx, hy, torso_conf=shoulder_conf)
    kps[pa.L_EYE] = [cx + eye_half, hy, 0.9]
    kps[pa.R_EYE] = [cx - eye_half, hy, 0.9]
    kps[pa.L_EAR][2] = kps[pa.R_EAR][2] = 0.1
    return PersonObservation(world_id="w1", identity_revision=0, local_track_id="l-w1", keypoints_2d=kps)


def _helmet_reason(p, cands, cfg=pa.DEFAULT_CONFIG):
    frame = FrameObservations("cam1", 0, 1, 1000, IMAGE, (p,))
    a = associate_frame(frame, FrameDetections("cam1", 0, 1, 1000, IMAGE, tuple(cands)), cfg)
    [s] = [s for s in a.samples if s.item == "helmet"]
    return s.kind, s.reason


def test_narrow_face_head_detection_connects_with_shoulder_floor():
    p = _narrow_face()   # 눈·귀 기준 폭 15px, 어깨 72px → 하한 43.2px
    old = dataclasses.replace(pa.DEFAULT_CONFIG, head_min_scale_shoulder_ratio=0.0)
    assert _helmet_reason(p, [HELMET()], old) == (pa.S_NONE, pa.R_NO_DETECTION)   # 머리 상자가 영역에 덜 들어 연결 안 됨
    assert _helmet_reason(p, [HELMET()]) == (pa.S_WORN, pa.R_WORN_MATCHED)
    assert _helmet_reason(p, [NO_HELMET()])[0] == pa.S_NOT_WORN
    assert pa.head_region(p.keypoints_2d, IMAGE).scale == pytest.approx(0.6 * 72)


def test_shoulder_floor_does_not_make_small_face_clear():
    p = _narrow_face(eye_half=2)   # 눈·귀 기준 폭 10px < 선명 기준 12px
    region = pa.head_region(p.keypoints_2d, IMAGE)
    assert region.scale == pytest.approx(43.2) and not region.clear
    assert _helmet_reason(p, [NO_HELMET()]) == (pa.S_UNKNOWN, pa.R_REGION_NOT_CLEAR)   # 미착용 근거로 쓰지 않는다
    assert _helmet_reason(p, [HELMET()]) == (pa.S_WORN, pa.R_WORN_MATCHED)


def test_without_both_shoulders_head_scale_stays_face_based():
    p = _narrow_face(shoulder_conf=0.1)
    assert pa.head_region(p.keypoints_2d, IMAGE).scale == pytest.approx(15.0)
    assert _helmet_reason(p, [HELMET()]) == (pa.S_NONE, pa.R_NO_DETECTION)


# ---- 불확실한 ID 의 local ID 차지는 다툼(S15P31A101-769, ppe-policy-0.8.0) ----------------------------------------------
def _w1_worn_on_two_cams():
    """w1 이 cam1 local 7·cam2 local 9 로 WORN 확정."""
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1", local="7")], [HELMET()])
        feed(j, "cam2", t_of(b, 40), [person("w1", local="9")], [HELMET()])
    assert state(j, closed_after(5)).state == "WORN"
    return j


def _temp_takes_cam1(j, bins, wid="t1"):
    """불확실한 임시 ID 가 cam1 local 7 을 가져가고, w1 은 cam2 로 계속 보인다."""
    for b in bins:
        feed(j, "cam1", t_of(b), [person(wid, local="7", ident="IDENTITY_UNCERTAIN")], [HELMET()])
        feed(j, "cam2", t_of(b, 40), [person("w1", local="9")], [HELMET()])


def test_uncertain_temp_id_taking_local_id_does_not_reset_owner():
    j = _w1_worn_on_two_cams()
    _temp_takes_cam1(j, range(6, 9))
    v = state(j, closed_after(8))
    assert (v.state, v.reason) == ("WORN", ps.R_WINDOW_WORN)
    assert j.identity_events.get("mapping_reset") is None and j.identity_events.get("mapping_contested") == 1
    assert j.local_owner[("cam1", 0, "7")][0] == "w1"   # 주인 기록은 그대로


def test_owner_reclaiming_contested_local_id_clears_contest_without_reset():
    j = _w1_worn_on_two_cams()
    _temp_takes_cam1(j, range(6, 8))
    feed(j, "cam1", t_of(8), [person("w1", local="7")], [HELMET()])
    v = state(j, closed_after(8))
    assert (v.state, j.contested) == ("WORN", {})
    assert j.identity_events.get("mapping_contest_cleared") == 1 and j.identity_events.get("mapping_reset") is None


def test_temp_id_merged_back_into_owner_clears_contest():
    j = _w1_worn_on_two_cams()
    _temp_takes_cam1(j, range(6, 8))
    feed(j, "cam1", t_of(8), [merged("w1", aliases=("t1",), local="7")], [HELMET()])
    v = state(j, closed_after(8))
    assert (v.state, v.reason) == ("WORN", ps.R_WINDOW_WORN)
    assert j.contested == {} and j.identity_events.get("mapping_reset") is None


def test_temp_id_becoming_certain_while_holding_local_id_resets_both():
    j = _w1_worn_on_two_cams()
    _temp_takes_cam1(j, range(6, 8))
    feed(j, "cam1", t_of(8), [person("t1", local="7")], [HELMET()])   # 쥔 채로 확실해졌다
    views = j.evaluate(closed_after(8))
    assert views["w1"]["helmet"].state == "UNKNOWN" and views["w1"]["helmet"].reason == ps.R_MAPPING_CHANGED
    assert j.identity_events.get("mapping_reset") == 2 and j.local_owner[("cam1", 0, "7")][0] == "t1"


def test_contested_temp_id_merged_into_other_person_resets_both():
    """임시 ID 가 w1 의 local ID 를 가져갔다가 다른 사람 w2 에 병합되면 진짜 주인 변경이다."""
    j = _w1_worn_on_two_cams()
    for b in range(6, 8):
        feed(j, "cam1", t_of(b), [person("t1", local="7", ident="IDENTITY_UNCERTAIN"),
                                  person("w2", 900, local="8")], [HELMET(), HELMET(900)])
    feed(j, "cam1", t_of(8), [merged("w2", cx=500, rev=1, aliases=("t1",), local="7")], [HELMET()])
    views = j.evaluate(closed_after(8))
    for wid in ("w1", "w2"):
        assert (views[wid]["helmet"].state, views[wid]["helmet"].reason) == ("UNKNOWN", ps.R_MAPPING_CHANGED)
    assert j.contested == {}


def test_contest_disappears_with_vanished_temp_id():
    j = _w1_worn_on_two_cams()
    _temp_takes_cam1(j, range(6, 7))
    j.evaluate(t_of(7) + j.cfg.track_retention_ms + 1)
    assert j.contested == {}


def test_contest_flag_off_restores_immediate_reset():
    j = PpeJudge(dataclasses.replace(pa.DEFAULT_CONFIG, uncertain_claim_contests=False))
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1", local="7")], [HELMET()])
    feed(j, "cam1", t_of(6), [person("t1", local="7", ident="IDENTITY_UNCERTAIN")], [HELMET()])
    assert state(j, closed_after(6)).reason == ps.R_MAPPING_CHANGED


# ---- 고립된 ID 불확실 사람은 판정에 쓴다(S15P31A101-818, ppe-policy-0.9.0) ----------------------------------------------
def _uncertain(wid="w1", *, isolated, **kw):
    return dataclasses.replace(person(wid, ident="IDENTITY_UNCERTAIN", **kw), isolated=isolated)


def test_isolated_uncertain_person_is_not_blocked_in_association():
    a = feed(PpeJudge(), "cam1", t_of(0), [_uncertain(isolated=True)], [NO_HELMET()])
    [s] = [s for s in a.samples if s.item == "helmet"]
    assert (s.kind, s.reason) == (pa.S_NOT_WORN, pa.R_NOT_WORN_MATCHED)
    b = feed(PpeJudge(), "cam1", t_of(0), [_uncertain(isolated=False)], [NO_HELMET()])
    [s] = [s for s in b.samples if s.item == "helmet"]
    assert (s.kind, s.reason) == (pa.S_UNKNOWN, pa.R_IDENTITY_UNCERTAIN)


def test_isolated_uncertain_person_confirms_not_worn_like_certain():
    """2026-11-19 연습: 혼자 선 작업자가 Tier 0 불확실인 채로 안전모를 벗었다. 고립이면 6칸으로 미착용이 확정된다."""
    j = PpeJudge()
    _worn(j)
    for b in range(6, 14):
        feed(j, "cam1", t_of(b), [_uncertain(isolated=True)], [NO_HELMET()])
    v = state(j, closed_after(13))
    assert (v.state, v.reason) == ("NOT_WORN", ps.R_WINDOW_NOT_WORN)
    assert j.tracks["w1"].skipped_bins == set()   # 건너뛴 칸이 없다


def test_non_isolated_uncertain_person_is_still_skipped_and_reset():
    j = PpeJudge()
    _worn(j)
    for b in range(6, 14):
        feed(j, "cam1", t_of(b), [_uncertain(isolated=False)], [NO_HELMET()])
    v = state(j, closed_after(13))
    assert v.state == "UNKNOWN" and v.reason == ps.R_IDENTITY_UNCERTAIN   # 3초 초과로 초기화, 미착용 아님


def test_isolation_radius_zero_turns_exception_off():
    j = PpeJudge(dataclasses.replace(pa.DEFAULT_CONFIG, isolated_uncertain_radius_m=0.0))
    for b in range(8):
        feed(j, "cam1", t_of(b), [_uncertain(isolated=True)], [NO_HELMET()])
    assert state(j, closed_after(7)).state == "UNKNOWN"


# ---- 판정 없는 칸(중립 칸) 허용(S15P31A101-818, ppe-policy-0.9.0) -------------------------------------------------------
def _not_worn_with(j, pattern, start=0):
    """pattern: 칸마다 'N'(미착용)·'0'(검출 없음 = 중립)·'C'(머리 불선명 = 중립)·'-'(표본 없음 = 빈 칸)·'W'(착용)·'S'(두 사람 겹침)."""
    for i, c in enumerate(pattern):
        b = start + i
        if c == "-":
            continue
        if c == "S":   # 두 사람에 걸친 미착용 상자 → 모호(SHARED_DETECTION)
            feed(j, "cam1", t_of(b), [person("w1"), person("w9", 530, local="l-w9")], [NO_HELMET(515)])
            continue
        p = person("w1", face_conf=0.1) if c == "C" else person("w1")
        cands = {"N": [NO_HELMET()], "0": [], "C": [NO_HELMET()], "W": [HELMET()]}[c]
        feed(j, "cam1", t_of(b), [p], cands)
    return state(j, closed_after(start + len(pattern) - 1))


def test_neutral_bins_up_to_two_do_not_block_not_worn():
    assert _not_worn_with(PpeJudge(), "NN0NN0N").state == "NOT_WORN"      # 중립 2칸(검출 없음)
    assert _not_worn_with(PpeJudge(), "N-NN0NN").state == "NOT_WORN"      # 빈 칸 1 + 중립 1


def test_too_many_gaps_or_two_empty_bins_still_block():
    assert _not_worn_with(PpeJudge(), "N00N0N").state != "NOT_WORN"       # 중립 3칸
    assert _not_worn_with(PpeJudge(), "NN-NN-N").state != "NOT_WORN"      # 빈 칸 2칸(빈 칸은 1칸까지)


def test_worn_and_ambiguous_bins_still_block_not_worn():
    assert _not_worn_with(PpeJudge(), "NNWNNN").state != "NOT_WORN"
    assert _not_worn_with(PpeJudge(), "NNSNNN").state != "NOT_WORN"


def test_neutral_bins_zero_restores_previous_rule():
    j = PpeJudge(dataclasses.replace(pa.DEFAULT_CONFIG, not_worn_max_neutral_bins=0))
    assert _not_worn_with(j, "NN0NNN").state != "NOT_WORN"
