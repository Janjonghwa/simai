"""다시점 집계 (S15P31A101-87, simai_ai.hmr.multiview). 합성 장면(SIM 상한, 제품 성능 아님).

반례: 한 카메라에 같은 사람 후보가 둘(카메라당 하나만), 재등장으로 World ID 가 바뀜(낡은 프롬프트 ID 를 새 ID 에 붙이지 않음),
ID 교정 뒤 차수 변경, pelvis null(Tier 0 모름)이면 World 관절·root 오차를 지어내지 않음, Tier 0 가 그 카메라에서 보지 않은 사람에
HMR 후보를 붙이지 않음, 박스는 겹치지만 엉덩이가 다른 사람(pelvis 게이트), 몸 방향이 뒤집힌 카메라 하나(대표에서 빠짐),
보정 판 불일치. 음성 대조: HMR 자기 translation 을 root 로 쓰면 깊이 오차만큼 틀린다(Tier 0 고정이 그것을 막음).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from simai_ai.hmr.multiview import (
    COCO17_INDEX,
    AggregationConfig,
    Reject,
    Selection,
    aggregate_frame,
    aggregate_person,
    associate,
    pose_frame_record,
    single_view_world_keypoints,
)
from simai_ai.hmr.rotation import matrix_to_rotvec, rotvec_to_matrix
from simai_ai.providers.contract import schema_errors
from tests.hmr.multiview_scene import PROVENANCE, T0, Person, hmr_candidate, make_rig, track_doc


@pytest.fixture(scope="module")
def rig():
    return make_rig()


def two_people():
    return [Person("w-1", (3.0, 2.5), 0.3), Person("w-2", (5.2, 3.6), -2.0)]


def candidates(people, rig, **kw):
    return [hmr_candidate(p, rig.cameras[cid], seed=i, **kw) for i, (p, cid) in enumerate((p, c) for p in people for c in rig.cameras)]


def by_id(agg):
    return {p.world_id: p for p in agg.people}


def test_exact_scene_recovers_relative_pose_root_rotation_and_anchor(rig):
    people = two_people()
    doc = track_doc(people, rig)
    assert schema_errors(doc, "world_tracks") == []
    agg = aggregate_frame(candidates(people, rig), doc, rig)
    assert agg.rejected == ()
    got = by_id(agg)
    for p in people:
        a = got[p.world_id]
        assert sorted(a.candidates) == sorted(rig.cameras)
        assert a.selection is Selection.CROSS_VIEW
        np.testing.assert_allclose(a.world_keypoints, p.world(), atol=1e-9)
        np.testing.assert_allclose(rotvec_to_matrix(np.asarray(a.root_rotation)), p.R_true, atol=2e-4)
        assert a.hmr_root_offset_m == pytest.approx(0.0, abs=1e-3)
        assert a.ground_anchor_xyz == pytest.approx((p.pelvis[0], p.pelvis[1], 0.0))
        assert a.joint_source == "inferred"


def test_root_is_tier0_pelvis_not_hmr_translation(rig):
    """단안 깊이 오차(+30%)가 있어도 위치는 Tier 0 pelvis 다. 음성 대조: HMR 자기 translation 이면 1 m 넘게 틀린다."""
    p = Person("w-1", (4.0, 3.0), 1.0)
    doc = track_doc([p], rig)
    cands = [hmr_candidate(p, rig.cameras[cid], depth_err=0.3) for cid in rig.cameras]
    a = aggregate_frame(cands, doc, rig).people[0]
    np.testing.assert_allclose(a.world_keypoints, p.world(), atol=1e-9)
    assert a.hmr_root_offset_m == pytest.approx(0.0, abs=1e-3)
    raw = single_view_world_keypoints(cands[0], rig.cameras["cam_A"])
    assert np.linalg.norm(raw[9] - p.world()[9]) > 1.0


def test_one_camera_two_candidates_for_same_person_keeps_one(rig):
    """반례: cam_B 에 같은 사람 후보가 둘(중복 검출). 카메라당 하나만 붙고 나머지는 CAMERA_SLOT_TAKEN."""
    p = Person("w-1", (4.0, 3.0), 0.0)
    doc = track_doc([p], rig)
    cands = [hmr_candidate(p, rig.cameras[cid]) for cid in rig.cameras]
    good = cands[1]
    x0, y0, x1, y1 = good.bbox
    dup = hmr_candidate(p, rig.cameras["cam_B"], candidate_id="cam_B:dup", depth_err=0.1,
                        bbox=(x0 + 0.1 * (x1 - x0), y0, x1 + 0.1 * (x1 - x0), y1))
    agg = aggregate_frame([*cands, dup], doc, rig)
    a = agg.people[0]
    assert a.candidates["cam_B"] is good
    assert [(r.candidate_id, r.reason) for r in agg.rejected] == [("cam_B:dup", Reject.CAMERA_SLOT_TAKEN)]
    assert len(a.candidates) == 4


def test_reentry_with_new_world_id_does_not_reuse_stale_prompt(rig):
    """반례: 나갔다 들어와 Tier 0 가 새 ID(w-3)를 줬는데 HMR 은 예전 ID(w-1) 프롬프트로 돌았다. 박스가 같아도 w-3 에 붙이지 않는다."""
    p = Person("w-3", (4.0, 3.0), 0.0)
    doc = track_doc([p], rig)
    stale = [hmr_candidate(p, rig.cameras[cid], prompt_world_id="w-1") for cid in rig.cameras]
    agg = aggregate_frame(stale, doc, rig)
    assert agg.people == ()
    assert {r.reason for r in agg.rejected} == {Reject.PROMPT_WORLD_ID_NOT_IN_FRAME}


def test_identity_revision_change_rejects_candidates_made_before_correction(rig):
    p = Person("w-1", (4.0, 3.0), 0.0, identity_revision=2)
    doc = track_doc([p], rig)
    old = [hmr_candidate(p, rig.cameras[cid], prompt_world_id="w-1", prompt_identity_revision=1) for cid in rig.cameras]
    new = hmr_candidate(p, rig.cameras["cam_A"], candidate_id="new", prompt_world_id="w-1", prompt_identity_revision=2)
    agg = aggregate_frame([*old, new], doc, rig)
    assert list(agg.people[0].candidates) == ["cam_A"]
    assert agg.people[0].candidates["cam_A"] is new
    assert sum(r.reason is Reject.IDENTITY_REVISION_CHANGED for r in agg.rejected) == 4


def test_unknown_pelvis_leaves_world_joints_and_root_offset_empty(rig):
    """반례: Tier 0 pelvis null. World 관절·root 오차·접지를 비우고 pose_frame 위치도 null 이다(HMR translation 으로 채우지 않음)."""
    p = Person("w-1", (4.0, 3.0), 0.5)
    doc = track_doc([p], rig, pelvis_known=False)
    cands = [hmr_candidate(p, rig.cameras[cid], depth_err=0.2) for cid in rig.cameras]
    a = aggregate_frame(cands, doc, rig).people[0]
    assert a.pelvis_xyz is None and a.world_keypoints is None and a.hmr_root_offset_m is None and a.ground_anchor_xyz is None
    assert a.selection is Selection.MEDOID
    assert a.root_rotation is not None  # 몸 방향은 위치가 아니다
    rec = pose_frame_record(a, doc, run_id="r1", model_version="m", input_hash="sha256:" + "0" * 64,
                            input_provenance=PROVENANCE, hmr_latency_ms=10, lag_vs_tier0_ms=20, fallback=False)
    assert schema_errors(rec, "human_mesh_output") == []
    assert rec["root"]["tier0_pelvis_xyz"] is None and rec["root"]["tier0_ground_anchor_xyz"] is None
    assert rec["canonical_motion"]["joints"] == [None] * 17
    assert set(rec["canonical_motion"]["joint_source"]) == {"inferred"}


def test_person_not_seen_by_tier0_in_a_camera_is_not_attached_there(rig):
    """반례: Tier 0 per_camera 에 cam_D 가 없다(가림·연결 실패). HMR 이 cam_D 에서 그 사람을 찾아도 붙이지 않는다."""
    p = Person("w-1", (4.0, 3.0), 0.0)
    doc = track_doc([p], rig, cameras={"w-1": ["cam_A", "cam_B", "cam_C"]})
    agg = aggregate_frame([hmr_candidate(p, rig.cameras[cid]) for cid in rig.cameras], doc, rig)
    assert sorted(agg.people[0].candidates) == ["cam_A", "cam_B", "cam_C"]
    assert [(r.camera_id, r.reason) for r in agg.rejected] == [("cam_D", Reject.NO_TRACK_MATCH)]


@pytest.mark.parametrize("b_xy", [(3.0, 4.5), (6.0, 4.5)], ids=["hip_far_in_image", "same_ray_behind"])
def test_box_of_one_person_but_other_persons_body_is_not_attached(rig, b_xy):
    """반례: 박스는 w-1 것인데 HMR 이 다른 사람(w-2)을 복원했다. 화면에서 엉덩이가 멀면 2D 게이트가, 같은 시선 위 뒤 사람이면
    깊이 비 게이트가 막는다. 음성 대조: 게이트를 끄면 붙는다."""
    a, b = Person("w-1", (3.0, 2.0), 0.0), Person("w-2", b_xy, 0.0)
    doc = track_doc([a, b], rig, cameras={"w-1": ["cam_A"], "w-2": ["cam_C"]})
    wrong = hmr_candidate(b, rig.cameras["cam_A"], bbox=tuple(doc["tracks"][0]["per_camera"][0]["bbox"]))
    clusters, rejected = associate([wrong], doc, rig)
    assert clusters == {}
    assert rejected[0].reason is Reject.NO_TRACK_MATCH
    off = AggregationConfig(pelvis_gate_bbox_ratio=1e9, max_depth_ratio=1e9)
    clusters, _ = associate([wrong], doc, rig, off)
    assert "cam_A" in clusters["w-1"]


def test_depth_error_within_gate_still_attaches(rig):
    """경계: 단안 깊이 오차 +40%(비 1.4 < 1.5)는 붙는다. +60% 는 붙지 않는다."""
    p = Person("w-1", (4.0, 3.0), 0.0)
    doc = track_doc([p], rig)
    ok = hmr_candidate(p, rig.cameras["cam_A"], depth_err=0.4)
    far = hmr_candidate(p, rig.cameras["cam_B"], depth_err=0.6)
    clusters, rejected = associate([ok, far], doc, rig)
    assert list(clusters["w-1"]) == ["cam_A"]
    assert [(r.camera_id, r.reason) for r in rejected] == [("cam_B", Reject.NO_TRACK_MATCH)]


@pytest.mark.parametrize("pelvis_known", [True, False])
def test_flipped_camera_is_not_representative(rig, pelvis_known):
    """반례: cam_C 의 몸 방향이 180° 뒤집힘. 다시점 선택(pelvis 있음)·medoid(없음) 모두 그 카메라를 대표로 고르지 않는다."""
    p = Person("w-1", (4.0, 3.0), 0.7)
    doc = track_doc([p], rig, pelvis_known=pelvis_known, px_noise=1.0)
    cands = [hmr_candidate(p, rig.cameras[cid], yaw_err=math.pi if cid == "cam_C" else 0.0, joint_noise=0.01, seed=i)
             for i, cid in enumerate(rig.cameras)]
    a = aggregate_frame(cands, doc, rig).people[0]
    assert a.representative_camera != "cam_C"
    R = rotvec_to_matrix(np.asarray(a.root_rotation))
    assert np.degrees(np.linalg.norm(matrix_to_rotvec(R @ p.R_true.T))) < 1.0
    if pelvis_known:
        assert a.cross_view_error_px["cam_C"] > 10 * max(v for k, v in a.cross_view_error_px.items() if k != "cam_C")


def test_two_candidates_without_cross_view_use_score_and_single(rig):
    p = Person("w-1", (4.0, 3.0), 0.0)
    doc = track_doc([p], rig, pelvis_known=False)
    track = doc["tracks"][0]
    c1 = hmr_candidate(p, rig.cameras["cam_A"], score=0.6)
    c2 = hmr_candidate(p, rig.cameras["cam_B"], score=0.9)
    a = aggregate_person(track, T0, {"cam_A": c1, "cam_B": c2}, rig)
    assert (a.representative_camera, a.selection) == ("cam_B", Selection.SCORE)
    a = aggregate_person(track, T0, {"cam_A": c1}, rig)
    assert (a.representative_camera, a.selection) == ("cam_A", Selection.SINGLE)


def test_calibration_version_mismatch_is_refused(rig):
    p = Person("w-1", (4.0, 3.0), 0.0)
    doc = track_doc([p], make_rig(calibration_version="sim-cal-other"))
    with pytest.raises(ValueError, match="보정"):
        aggregate_frame([hmr_candidate(p, rig.cameras["cam_A"])], doc, rig)


def test_non_finite_hmr_output_is_rejected_not_used(rig):
    p = Person("w-1", (4.0, 3.0), 0.0)
    doc = track_doc([p], rig)
    bad = hmr_candidate(p, rig.cameras["cam_A"])
    kp = bad.keypoints_cam.copy()
    kp[COCO17_INDEX[13]] = np.nan
    bad = type(bad)(**{**bad.__dict__, "keypoints_cam": kp})
    agg = aggregate_frame([bad, hmr_candidate(p, rig.cameras["cam_B"])], doc, rig)
    assert list(agg.people[0].candidates) == ["cam_B"]
    assert agg.rejected[0].reason is Reject.NON_FINITE_KEYPOINTS


def test_pose_frame_record_matches_contract_and_keeps_tier0_root(rig):
    people = two_people()
    doc = track_doc(people, rig)
    agg = aggregate_frame(candidates(people, rig, depth_err=0.15), doc, rig)
    for a in agg.people:
        rec = pose_frame_record(a, doc, run_id="r1", model_version="Fast-SAM-3D-Body@d72aa36", input_hash="sha256:" + "1" * 64,
                                input_provenance=PROVENANCE, hmr_latency_ms=180, lag_vs_tier0_ms=320, fallback=False,
                                quality={"leave_one_camera_out_reproj_px": 3.2, "silhouette_iou": None, "foot_to_anchor_error_m": 0.01})
        assert schema_errors(rec, "human_mesh_output") == []
        assert rec["root"]["source"] == "TIER0"
        assert rec["root"]["tier0_pelvis_xyz"] == list(a.pelvis_xyz)
        assert len(rec["native_pose"]["params"]["joint_rotations"]) == 127
        assert [s["camera_id"] for s in rec["per_camera_sources"]] == sorted(rig.cameras)
    a = agg.people[0]
    no_rot = type(a)(**{**a.__dict__, "root_rotation": None})
    assert pose_frame_record(no_rot, doc, run_id="r1", model_version="m", input_hash="sha256:" + "1" * 64,
                             input_provenance=PROVENANCE, hmr_latency_ms=1, lag_vs_tier0_ms=1, fallback=True) is None
