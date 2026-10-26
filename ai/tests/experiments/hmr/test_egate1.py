"""E-GATE-1 평가 경로 (S15P31A101-87): 결과 묶음 형식, 실루엣 래스터, 한 카메라 제외·발-ground_anchor·연속성 지표, Run 기록.

합성 장면(tests/hmr/multiview_scene.py, SIM 상한)으로 경로만 확인한다. 제품 성능이 아니다(AGENTS 규칙 7).
반례: 묶음 그림이 기준보다 늦어 잇지 않음(관측을 다른 시각 그림으로 채우지 않음), 다른 카메라 2대 미만이면 한 카메라 제외 표본을
만들지 않음(지어내지 않음), pelvis·접지 null 이면 발 오차를 비움(Z=0 가정 금지), 재등장·ID 교정 뒤 흔들림을 차수 사이로 재지 않음,
결과 폴더가 있으면 덮어쓰지 않음.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from simai_ai.providers.contract import schema_errors
from tests.hmr.multiview_scene import (
    BOX_FACES,
    BOX_RIG,
    CAL,
    FRAME,
    K,
    SIZE,
    T0,
    Person,
    hmr_candidate,
    hmr_vertices,
    make_rig,
    track_doc,
)

REPO = Path(__file__).resolve().parents[4]
HMR = REPO / "ai" / "experiments" / "hmr"


@pytest.fixture
def mods(monkeypatch):
    monkeypatch.syspath_prepend(str(HMR))
    for name in ("egate1_bundle", "egate1_eval"):
        sys.modules.pop(name, None)
    return importlib.import_module("egate1_bundle"), importlib.import_module("egate1_eval")


@pytest.fixture(scope="module")
def rig():
    return make_rig()


def calibration_doc(rig) -> dict:
    cams = []
    for cid, cam in rig.cameras.items():
        cams.append({"camera_id": cid, "image_size": list(SIZE), "K": [list(r) for r in K],
                     "distortion": {"model": cam.distortion_model, "coeffs": list(cam.distortion_coeffs)},
                     "R": cam.R.tolist(), "t": cam.t.tolist()})
    return {"schema_version": "2.1.0", "calibration_version": CAL, "provider_source": "SIM", "provider_version": "sim-87",
            "state": "PROVISIONAL", "created_at": T0, "coordinate_frame": {**FRAME, "site_id": "sim/classroom"},
            "scale_basis": "SIM", "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA", "cameras": cams}


def write_scene(tmp_path, B, E, rig, frames, *, depth_err=0.25, scale_err=0.03, time_offset_ms=None, pelvis_known=True):
    """frames = [(t, [Person...], {world_id: [camera...]} 또는 None)]. 묶음·world_tracks·보정 파일을 쓴다."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    np.save(bundle / B.FACES_FILE, BOX_FACES.astype(np.int32))
    docs = []
    for instant, (t, people, cams) in enumerate(frames):
        doc = track_doc(people, rig, t=t, cameras=cams, pelvis_known=pelvis_known, anchor_known=pelvis_known)
        docs.append(doc)
        for cid, cam in rig.cameras.items():
            row = {"instant": instant, "camera_id": cid, "capture_time_ms": t + (time_offset_ms or {}).get(cid, 0),
                   "frame_ref": None, "image_size": list(SIZE), "K_used": [list(r) for r in K], "undistorted": True,
                   "result": "SUCCESS", "hmr_ms": 120.0, "people": []}
            arrays = []
            for i, p in enumerate(people):
                if cams is not None and cid not in cams.get(p.world_id, []):
                    continue
                err = depth_err if cid != "cam_A" else -depth_err
                c = hmr_candidate(p, cam, depth_err=err, scale_err=scale_err, joint_noise=0.005, seed=instant * 10 + i)
                truth = E.pinhole_project(np.asarray(K), cam, p.world(BOX_RIG))
                mask = E.rasterize(truth, BOX_FACES, SIZE, 10**9)
                arrays.append({"keypoints_cam": c.keypoints_cam.astype(np.float32), "camera_from_rig": c.camera_from_rig,
                               "joint_rotations": B.joint_rotations_array(c.joint_rotations),
                               "vertices_cam": hmr_vertices(p, cam, depth_err=err, scale_err=scale_err).astype(np.float16),
                               **B.pack_mask(mask)})
                row["people"].append({"candidate_id": f"{instant}:{cid}:{i}", "bbox": list(c.bbox), "score": 0.9,
                                      "prompt": {"mask_iou": 0.9, "keypoints": []}})
            B.write_image(bundle, row, arrays)
    (bundle / "metrics.json").write_text(json.dumps({"model_version": "sim-hmr", "input_provenance": {
        "frames": {"source": "SIM", "dataset_id": "sim/hmr-multiview-87"}}}), encoding="utf-8")
    wt = tmp_path / "world_tracks.jsonl"
    wt.write_text("".join(json.dumps(d) + "\n" for d in docs), encoding="utf-8")
    cal = tmp_path / "calibration.json"
    cal.write_text(json.dumps(calibration_doc(rig)), encoding="utf-8")
    return bundle, wt, cal, docs


def run_eval(E, bundle, wt, cal, out, *extra):
    return E.main(["--bundle", str(bundle), "--world-tracks", str(wt), "--calibration", str(cal), "--run-id", "egate1-sim-1",
                   "--out", str(out), *extra])


def test_rasterize_fills_square_and_drops_bad_faces(mods):
    _B, E = mods
    px = np.array([[10.0, 10.0], [20.0, 10.0], [20.0, 20.0], [10.0, 20.0], [np.nan, 0.0]])
    faces = np.array([[0, 1, 2], [0, 2, 3], [0, 1, 4]])
    m = E.rasterize(px, faces, (40, 30), 10**6)
    assert m.sum() == 100 and m[10:20, 10:20].all()
    assert E.rasterize(px, faces, (40, 30), 50).sum() == 0  # 삼각형 bbox 넓이 상한


def test_mask_pack_roundtrip_and_empty(mods):
    B, _E = mods
    m = np.zeros((30, 40), bool)
    m[5:9, 7:30] = True
    m[12, 3] = True
    packed = B.pack_mask(m)
    assert (B.unpack_mask(packed, (40, 30)) == m).all()
    assert B.pack_mask(np.zeros((4, 4), bool)) is None


def test_end_to_end_metrics_baselines_and_run_record(mods, rig, tmp_path):
    B, E = mods
    people = [Person("w-1", (3.0, 2.5), 0.3), Person("w-2", (5.2, 3.6), -2.0)]
    frames = [(T0 + 100 * i, people, None) for i in range(3)]
    bundle, wt, cal, _docs = write_scene(tmp_path, B, E, rig, frames)
    out = tmp_path / "out"
    assert run_eval(E, bundle, wt, cal, out) == 0
    m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    rec = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    assert schema_errors(rec, "run_record") == []
    assert (rec["e_gate"], rec["experiment_id"], rec["gpu"]["provider"], rec["input_provenance"]["is_upper_bound"]) == ("E-GATE-1", "EXP-02", "LOCAL", True)
    assert "상한" in rec["notes"]
    a = m["all"]
    assert m["counts"]["loo_samples"] == 3 * 2 * 4
    # 다시점(Tier 0 root) < 원본 단안(자기 translation, 깊이 ±25%)
    assert a["loo_common_px.hmr_multiview"]["p50"] < 0.25 * a["loo_common_px.hmr_single_raw"]["p50"]
    assert a["loo_common_px.tier0_triangulated"]["p50"] < 0.5
    assert a["silhouette_iou.hmr_multiview"]["p50"] > 0.85 > a["silhouette_iou.hmr_single_raw"]["p50"]
    assert a["foot_anchor_within_g5.hmr_multiview"] == 1.0
    assert a["root_offset_m.multiview"]["p50"] < 0.02 < a["root_offset_m.single_raw"]["p50"]
    assert m["counts"]["selection.CROSS_VIEW"] == 6
    lines = (out / "pose_frames.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6 and all(schema_errors(json.loads(x), "human_mesh_output") == [] for x in lines)
    assert all(isinstance(v, (int, float, str, bool, type(None))) for v in rec["metrics"].values())


def test_body_scale_error_shows_in_foot_error(mods, rig, tmp_path):
    """반례(지표가 늘 통과하지 않음): 단안 체형 크기 +10% 는 root 를 Tier 0 pelvis 에 고정해도 다리가 길어 발이 바닥 아래로
    약 9 cm 내려간다. G5(5 cm) 밖으로 잡힌다(설계 문서 잔여 위험 '체형 크기')."""
    B, E = mods
    bundle, wt, cal, _ = write_scene(tmp_path, B, E, rig, [(T0, [Person("w-1", (4.0, 3.0), 0.0)], None)], depth_err=0.0, scale_err=0.10)
    out = tmp_path / "out"
    assert run_eval(E, bundle, wt, cal, out) == 0
    a = json.loads((out / "metrics.json").read_text(encoding="utf-8"))["all"]
    assert a["foot_anchor_within_g5.hmr_multiview"] == 0.0
    assert 0.07 < a["foot_anchor_abs_m.hmr_multiview"]["p50"] < 0.11
    assert a["silhouette_iou.hmr_self"]["p50"] > 0.99  # 2D 는 맞는다(크기·깊이가 함께 틀림)


def test_unknown_pelvis_and_anchor_leave_foot_error_empty(mods, rig, tmp_path):
    B, E = mods
    frames = [(T0, [Person("w-1", (4.0, 3.0), 0.0)], None)]
    bundle, wt, cal, _ = write_scene(tmp_path, B, E, rig, frames, pelvis_known=False)
    out = tmp_path / "out"
    assert run_eval(E, bundle, wt, cal, out) == 0
    m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert m["all"]["foot_anchor_abs_m.hmr_multiview"]["n"] == 0
    assert m["all"]["foot_anchor_within_g5.hmr_multiview"] is None
    assert m["all"]["root_offset_m.multiview"]["n"] == 0
    rec = json.loads((out / "pose_frames.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["root"]["tier0_pelvis_xyz"] is None and rec["canonical_motion"]["joints"] == [None] * 17


def test_late_image_is_not_joined(mods, rig, tmp_path):
    """반례: cam_D 그림이 Tier 0 관측보다 30 ms 늦다(기준 20 ms). 다른 시각 그림으로 채우지 않는다."""
    B, E = mods
    frames = [(T0, [Person("w-1", (4.0, 3.0), 0.0)], None)]
    bundle, wt, cal, docs = write_scene(tmp_path, B, E, rig, frames, time_offset_ms={"cam_D": 30})
    images, faces = B.load_bundle(bundle)
    res = E.evaluate(images, faces, docs, make_rig())
    assert res["stats"]["join_missing"] == 1 and res["stats"]["join_time"] == 3
    assert {r["camera_id"] for r in res["rows"] if r.get("iou.hmr_self") is not None} == {"cam_A", "cam_B", "cam_C"}
    res = E.evaluate(images, faces, docs, make_rig(), E.EvalConfig(join_tolerance_ms=40))
    assert res["stats"]["join_time"] == 4


def test_two_camera_person_gives_no_loo_sample(mods, rig, tmp_path):
    """반례: 두 대에서만 보인 사람. 한 대를 빼면 1대라 기준점을 삼각측량할 수 없다 → 표본 없음(이유 기록)."""
    B, E = mods
    frames = [(T0, [Person("w-1", (4.0, 3.0), 0.0)], {"w-1": ["cam_A", "cam_C"]})]
    bundle, wt, cal, docs = write_scene(tmp_path, B, E, rig, frames)
    images, faces = B.load_bundle(bundle)
    res = E.evaluate(images, faces, docs, make_rig())
    assert [r.get("skipped") for r in res["rows"]] == ["NO_ANCHOR_FROM_OTHER_CAMERAS"] * 2
    assert res["metrics"]["counts"]["loo_samples"] == 0
    out = tmp_path / "out"
    assert run_eval(E, bundle, wt, cal, out) == 3  # 표본이 없으면 FAILED Run 기록
    rec = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    assert rec["status"] == "FAILED" and schema_errors(rec, "run_record") == []


def test_reappearance_and_revision_change_in_continuity(mods, rig, tmp_path):
    """재등장(빠졌다 다시)과 ID 교정(차수 1 → 2): 흔들림을 끊긴 구간·차수 사이로 재지 않는다."""
    B, E = mods
    a0, a2 = Person("w-1", (4.0, 3.0), 0.0), Person("w-1", (4.05, 3.0), 0.0)
    a3 = Person("w-1", (4.10, 3.0), 0.0, identity_revision=1)
    other = Person("w-2", (2.0, 1.5), 1.0)
    frames = [(T0, [a0, other], None), (T0 + 100, [other], None), (T0 + 200, [a2, other], None), (T0 + 300, [a3, other], None)]
    bundle, wt, cal, docs = write_scene(tmp_path, B, E, rig, frames)
    images, faces = B.load_bundle(bundle)
    res = E.evaluate(images, faces, docs, make_rig())
    c = res["metrics"]["continuity"]
    assert (c["reappearances"], c["identity_revision_changes"]) == (1, 1)
    w1 = [p for p in res["people"] if p["world_id"] == "w-1"]
    assert [p["jitter_m"] is None for p in w1] == [True, True, True]  # 첫 등장, 재등장, 차수 변경
    w2 = [p["jitter_m"] for p in res["people"] if p["world_id"] == "w-2"]
    assert w2[0] is None and all(j is not None and j < 0.05 for j in w2[1:])
    assert res["metrics"]["reappeared"]["loo_common_px.hmr_multiview"]["n"] == 4


def test_existing_output_is_not_overwritten(mods, rig, tmp_path):
    B, E = mods
    bundle, wt, cal, _ = write_scene(tmp_path, B, E, rig, [(T0, [Person("w-1", (4.0, 3.0), 0.0)], None)])
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    assert run_eval(E, bundle, wt, cal, out) == 2
    assert [p.name for p in out.iterdir()] == ["keep.txt"]


def test_bundle_rows_append_and_refuse_overwrite(mods, tmp_path):
    B, _E = mods
    row = {"instant": 0, "camera_id": "cam_A", "capture_time_ms": T0, "image_size": [4, 4], "people": [{"candidate_id": "x", "bbox": [0, 0, 1, 1]}]}
    B.write_image(tmp_path, row, [{"keypoints_cam": np.zeros((70, 3), np.float32)}])
    with pytest.raises(FileExistsError):
        B.write_image(tmp_path, row, [{"keypoints_cam": np.zeros((70, 3), np.float32)}])
    images, faces = B.load_bundle(tmp_path)
    assert faces is None and len(images) == 1 and images[0].people[0].arrays["keypoints_cam"].shape == (70, 3)


def test_loo_representative_selection_does_not_see_held_out_camera(mods, rig, tmp_path, monkeypatch):
    """리뷰 권장 1(누수): 한 카메라 제외 집계는 c 를 뺀 삼각측량 기준점을 pelvis 로 받는다. Tier 0 pelvis(c 를 썼을 수 있음)를
    20 cm 틀어 두어도 대표 선택에 그 값이 들어가지 않는다. 음성 대조: 원래 track 을 넘기면 이 시험이 실패한다."""
    B, E = mods
    bundle, wt, cal, docs = write_scene(tmp_path, B, E, rig, [(T0, [Person("w-1", (4.0, 3.0), 0.0)], None)])
    for t in docs[0]["tracks"]:
        t["pelvis_xyz"] = [t["pelvis_xyz"][0] + 0.2, t["pelvis_xyz"][1], t["pelvis_xyz"][2]]
    seen = []
    real = E.aggregate_person

    def spy(track, *a, **kw):
        seen.append(track["pelvis_xyz"])
        return real(track, *a, **kw)

    monkeypatch.setattr(E, "aggregate_person", spy)
    images, faces = B.load_bundle(bundle)
    res = E.evaluate(images, faces, docs, make_rig())
    tier0 = docs[0]["tracks"][0]["pelvis_xyz"]
    truth = Person("w-1", (4.0, 3.0), 0.0).pelvis
    assert len(seen) == 4 and res["metrics"]["counts"]["loo_samples"] == 4
    for p in seen:
        assert np.linalg.norm(np.asarray(p) - truth) < 0.01 < np.linalg.norm(np.asarray(p) - np.asarray(tier0))
