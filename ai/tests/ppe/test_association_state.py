"""PPE 사람 연결·상태 누적 합성 장면 시험 (명세 5.5, S15P31A101-491). 순수 Python, GPU 없음.

머리 폭 40px 사람을 원본 2304×1296 픽셀에 그리고, 착용·미착용·함정(손에 든 안전모, 의자 조끼)·가림·겹침·
ID 개정·카메라 보완·깜빡임·만료를 확인한다. 수치는 config 초기값 기준이다.
"""

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


def test_missing_detection_is_not_not_worn_evidence():
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1")], [NO_HELMET()] if b != 3 else [])
    assert state(j, closed_after(5)).state == "UNKNOWN"
    for b in range(6, 20):
        feed(j, "cam1", t_of(b), [person("w1")], [])
    v = state(j, closed_after(19))
    assert (v.state, v.reason) == ("UNKNOWN", pa.R_NO_DETECTION)


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


def test_identity_uncertain_resets_and_blocks_evidence():
    j = PpeJudge()
    _worn(j)
    feed(j, "cam1", t_of(6), [person("w1", ident="IDENTITY_UNCERTAIN")], [HELMET()])
    v = state(j, closed_after(6))
    assert (v.state, v.reason) == ("UNKNOWN", pa.R_IDENTITY_UNCERTAIN)


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
    j = PpeJudge()
    for b in range(8):
        feed(j, "cam1", t_of(b), [person("w1", local="a")], [HELMET()])
        feed(j, "cam2", t_of(b, 40), [person("w1", 1200, local="b")], [NO_HELMET(1200)])
    v = state(j, closed_after(7))
    assert (v.state, v.reason) == ("UNKNOWN", ps.R_CROSS_CAMERA_CONFLICT)


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
