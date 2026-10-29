"""4대 합성 카메라/다중 궤적의 회전 복구, World 유지 정렬, 최소 데이터 게이트, fit/holdout 분리 시험. SIM 지표는 상한이다."""

import copy
from dataclasses import replace

import cv2
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.criteria import CalibrationEvidence, judge_calibration
from simai_ai.calibration.document import CameraCalibration, build_calibration_doc, content_hash
from simai_ai.calibration.extrinsics.cli import build_parser
from simai_ai.calibration.extrinsics.keypoints import CorrespondenceError, build_observations
from simai_ai.calibration.extrinsics.session import (
    FIXED_LENS,
    adjust_session,
    align_world_to_previous,
    bootstrap_rotations,
    session_rig,
)
from simai_ai.calibration.extrinsics.session_tracks import (
    SessionTracks,
    associate_session,
    association_metrics,
    split_session_tracks,
    track_session,
)
from simai_ai.providers.contract import check_calibration

T0 = 1793238900000
K = [[FIXED_LENS[0], 0., 1152.], [0., FIXED_LENS[0], 648.], [0., 0., 1.]]
DIST = [*FIXED_LENS[1:], 0., 0., 0.]


def _camera(cid, center):
    C = np.asarray(center, dtype=float)
    forward = np.array([4.5, 3.75, 0.9]) - C
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0., 0., 1.])
    right /= np.linalg.norm(right)
    R = np.stack([right, np.cross(forward, right), forward])
    return {"camera_id": cid, "image_size": [2304, 1296], "K": copy.deepcopy(K),
            "distortion": {"model": "OPENCV_PINHOLE", "coeffs": DIST},
            "R": R.tolist(), "t": (-R @ C).tolist()}


class BoxTracker:
    """합성 관측의 로컬 백엔드. 검출 순서 대신 박스 위치로 잇는다."""
    def __init__(self, config):
        self.centers = {}
        self.next_id = 0

    def update(self, boxes, scores, features=None):
        out, used = [], set()
        for idx, b in enumerate(boxes):
            center = np.asarray(b[:2])
            options = [(np.linalg.norm(c - center), k) for k, c in self.centers.items() if k not in used]
            best = min(options) if options else (float("inf"), 0)
            if best[0] < 80:
                key = best[1]
            else:
                self.next_id += 1
                key = self.next_id
            used.add(key)
            self.centers[key] = center
            out.append((key, idx))
        return out

    def live_track_ids(self):
        return frozenset(self.centers), frozenset()


@pytest.fixture(scope="module")
def case():
    cams = [_camera("cam_A", [.3, .3, 2.21]), _camera("cam_B", [8.7, .3, 2.195]),
            _camera("cam_C", [8.7, 7.2, 2.23]), _camera("cam_D", [.3, 7.2, 2.24])]
    doc = build_calibration_doc(
        calibration_version="cal-previous", provider_source="SIM", provider_version="synthetic-1",
        created_at=T0, cameras=cams, judgement=judge_calibration("SIM", cams, CalibrationEvidence()),
        fallback_scale_basis="MODEL_ESTIMATE", site_id="lobby", world_axes_source="WALLS",
    )
    truth = copy.deepcopy(doc)
    d = truth["cameras"][-1]
    R0 = np.asarray(d["R"])
    center = -R0.T @ np.asarray(d["t"])
    R = Rotation.from_euler("y", 7, degrees=True).as_matrix() @ R0
    d["R"], d["t"] = R.tolist(), (-R @ center).tolist()
    provenance = {"frames": {"source": "SIM", "dataset_id": "session-synthetic"}, "is_upper_bound": True}
    header = {"format": "simai.keypoint_sequences", "format_version": "0.1.0", "created_at": T0,
              "joint_layout": "COCO17", "input_provenance": provenance,
              "cameras": [{"camera_id": c["camera_id"], "image_size": c["image_size"]} for c in cams]}
    body = np.array([[0, 0, 1.68], [-.03, 0, 1.72], [.03, 0, 1.72], [-.08, 0, 1.7], [.08, 0, 1.7],
                     [-.2, 0, 1.4], [.2, 0, 1.4], [-.3, 0, 1.1], [.3, 0, 1.1], [-.36, 0, .95], [.36, 0, .95],
                     [-.12, 0, .9], [.12, 0, .9], [-.12, 0, .5], [.12, 0, .5], [-.12, 0, .08], [.12, 0, .08]])
    rows = []
    rng = np.random.default_rng(331)
    for i in range(55):
        for cam in truth["cameras"]:
            dets = []
            for person in range(2):
                a = i * .048 + person * np.pi
                X = body + [4.5 + 1.5 * np.cos(a), 3.75 + 1.6 * np.sin(a) + person * .6, 0.]
                uv = cv2.projectPoints(X, np.array(cam["R"]), np.array(cam["t"]), np.array(K), np.array(DIST))[0][:, 0]
                uv += rng.normal(0, .2, uv.shape)
                kps = [tuple([*p, .99]) for p in uv]
                bbox = [*uv.min(axis=0), *uv.max(axis=0)]
                dets.append({"bbox": bbox, "score": .99, "keypoints_2d": kps})
            if i % 2:
                dets.reverse()  # 검출 목록 순서가 카메라별/시각별로 바뀌어도 ID 대응을 보장한다.
            rows.append({"camera_id": cam["camera_id"], "stream_epoch": 0, "frame_sequence": i,
                         "estimated_capture_time_ms": T0 + i * 300, "timestamp_source": "SIM",
                         "timestamp_uncertainty_ms": 0, "detections": dets})
    tracks = track_session(header, rows, backend_factory=BoxTracker)
    return doc, truth, header, rows, tracks, provenance


@pytest.mark.parametrize("mixed", [False, True])
def test_seven_degree_recovery_with_multiple_people_and_mixed_candidates(case, mixed):
    previous, truth, _, _, tracks, _ = case
    if mixed:
        ticks = []
        for i, (t, views) in enumerate(tracks.ticks):
            own = [v for v in views if v.camera_id == "cam_D"]
            if i % 7 == 0 and len(own) == 2:
                views = tuple(replace(v, view=replace(v.view, keypoints=own[1 - own.index(v)].view.keypoints))
                              if v.camera_id == "cam_D" else v for v in views)
            ticks.append((t, views))
        tracks = SessionTracks(tuple(ticks), tracks.record)
    before = association_metrics(associate_session(tracks, session_rig(previous)))
    recovered, report = bootstrap_rotations(tracks, previous)
    after = association_metrics(associate_session(tracks, session_rig(recovered)))
    error = Rotation.from_matrix(np.array(recovered["cameras"][-1]["R"]) @ np.array(truth["cameras"][-1]["R"]).T)
    assert np.degrees(error.magnitude()) < .25
    assert after["fusion_4_ratio"] > .8
    assert after["fusion_4_ratio"] > before["fusion_4_ratio"] + .5
    assert any(c["accepted"] for cam in report["cameras"] for c in cam["candidates"])


def test_alternating_fixed_lens_height_scale_and_contract(case):
    previous, _, _, _, tracks, provenance = case
    heights = {c["camera_id"]: float((-np.array(c["R"]).T @ np.array(c["t"]))[2]) for c in previous["cameras"]}
    outcome = adjust_session(tracks, previous, measured_camera_heights=heights,
                             input_provenance=provenance, created_at=T0 + 20000, rounds=2)
    assert len(outcome.report["rounds"]) == 2
    assert outcome.calibration is not None, outcome.report.get("failure_reason")
    doc = outcome.calibration
    assert check_calibration(doc) == []
    assert doc["content_hash"] == content_hash(doc)
    assert doc["calibration_version"] != previous["calibration_version"]
    assert doc["coordinate_frame"]["site_id"] == "lobby"
    assert doc["state"] == "PROVISIONAL" and doc["provider_source"] == "SIM"
    assert doc["scale_basis"] == "MEASURED_CAMERA_HEIGHT"
    assert all(c["K"][0][0] == FIXED_LENS[0] for c in doc["cameras"])
    assert all(abs(v["height_residual_m"]) < .02 for v in outcome.report["camera_changes"].values())
    assert outcome.report["world_alignment"]["usable"] is True
    assert outcome.report["world_alignment"]["anchor_cameras"] == ["cam_A", "cam_B", "cam_C"]
    assert outcome.report["fit"]["after"]["fusion_4_ratio"] > .8
    assert outcome.report["holdout"]["after"]["fusion_4_ratio"] > .8
    assert outcome.report["holdout"]["after"]["reprojection_error_px"]["overall"]["median"] < 1.0


def test_world_perturbation_rotation_translation_scale_returns_to_previous_world(case, monkeypatch):
    """합성 4대에서 World를 일부러 회전·이동·축척해도 직전 World로 돌아오는지 검증한다(-343)."""
    import simai_ai.calibration.extrinsics.session as session_mod

    previous, truth, _, _, tracks, provenance = case
    heights = {c["camera_id"]: float((-np.array(c["R"]).T @ np.array(c["t"]))[2]) for c in previous["cameras"]}
    R_perturb = Rotation.from_euler("xyz", [4.5, -3.2, 11.0], degrees=True).as_matrix()
    t_perturb = np.array([0.75, -0.55, 0.28], dtype=float)
    s_perturb = 1.14

    def perturb_camera(c: CameraCalibration) -> CameraCalibration:
        R_old = np.asarray(c.R, dtype=float)
        C_old = -R_old.T @ np.asarray(c.t, dtype=float)
        R_new = R_old @ R_perturb.T
        C_new = s_perturb * (R_perturb @ C_old) + t_perturb
        t_new = -R_new @ C_new
        return replace(c, R=R_new.tolist(), t=t_new.tolist())

    real_adjust = session_mod.adjust_human_bundle

    def perturbed_adjust(*args, **kwargs):
        res = real_adjust(*args, **kwargs)
        if not res.cameras:
            return res
        return replace(res, cameras=tuple(perturb_camera(c) for c in res.cameras))

    monkeypatch.setattr(session_mod, "adjust_human_bundle", perturbed_adjust)
    outcome = adjust_session(tracks, previous, measured_camera_heights=heights,
                             input_provenance=provenance, created_at=T0 + 25000, rounds=2)
    assert outcome.calibration is not None, outcome.report.get("failure_reason")
    wa = outcome.report["world_alignment"]
    assert wa["usable"] is True
    assert wa["anchor_cameras"] == ["cam_A", "cam_B", "cam_C"]
    expected_rot_deg = float(np.degrees(Rotation.from_matrix(R_perturb).magnitude()))
    assert abs(wa["rotation_deg"] - expected_rot_deg) < 0.25
    assert abs(wa["scale"] - (1.0 / s_perturb)) < 0.01
    assert wa["anchor_center_residual_max_cm"] < 1.5
    # 정렬 뒤 4대 모두 정답(직전 World 기준 truth)으로 돌아와야 한다.
    truth_by_id = {c["camera_id"]: c for c in truth["cameras"]}
    for c in outcome.calibration["cameras"]:
        cid = c["camera_id"]
        R_est, t_est = np.asarray(c["R"]), np.asarray(c["t"])
        C_est = -R_est.T @ t_est
        R_gt = np.asarray(truth_by_id[cid]["R"])
        C_gt = -R_gt.T @ np.asarray(truth_by_id[cid]["t"])
        rot_err_deg = float(np.degrees(Rotation.from_matrix(R_est @ R_gt.T).magnitude()))
        pos_err_cm = float(np.linalg.norm(C_est - C_gt) * 100)
        assert rot_err_deg < 0.3, f"{cid}: rot_err_deg={rot_err_deg}"
        assert pos_err_cm < 2.0, f"{cid}: pos_err_cm={pos_err_cm}"


def test_insufficient_data_is_rejected(case):
    """데이터가 최소 기준에 못 미치면 보정을 내지 않고 실패 사유를 남기는지 검증한다(-343)."""
    previous, _, _, _, tracks, provenance = case
    heights = {c["camera_id"]: float((-np.array(c["R"]).T @ np.array(c["t"]))[2]) for c in previous["cameras"]}
    # 1) 공통 시각 수가 부족한 경우 (전체 12개 → fit 6개, holdout 6개)
    short_tracks = SessionTracks(tracks.ticks[:12], {**dict(tracks.record), "ticks": 12})
    outcome_short = adjust_session(short_tracks, previous, measured_camera_heights=heights,
                                   input_provenance=provenance, created_at=T0 + 30000, rounds=2)
    assert outcome_short.calibration is None
    assert "최소 데이터 기준 미달" in outcome_short.report["failure_reason"]
    assert outcome_short.report["data_sufficiency"]["usable"] is False
    assert outcome_short.report["rounds"] == []

    # 2) 시각 수는 충분하나 3대 이상 동시 관측이 부족한 경우 (카메라 2대만 관측)
    two_cam_ticks = tuple(
        (t, tuple(v for v in views if v.camera_id in ("cam_A", "cam_B")))
        for t, views in tracks.ticks
    )
    two_cam_tracks = SessionTracks(two_cam_ticks, dict(tracks.record))
    outcome_2cam = adjust_session(two_cam_tracks, previous, measured_camera_heights=heights,
                                  input_provenance=provenance, created_at=T0 + 31000, rounds=2)
    assert outcome_2cam.calibration is None
    assert "최소 데이터 기준 미달" in outcome_2cam.report["failure_reason"]
    assert outcome_2cam.report["data_sufficiency"]["counts"]["fit_3plus_observations"] == 0


def test_fit_and_holdout_splits_do_not_overlap(case):
    """fit과 holdout 구간이 서로 겹치지 않고 독립 평가되는지 검증한다(-343)."""
    previous, _, header, rows, tracks, provenance = case
    heights = {c["camera_id"]: float((-np.array(c["R"]).T @ np.array(c["t"]))[2]) for c in previous["cameras"]}
    # 1) 단일 연속 입력의 교대 블록 분리
    fit_tracks, holdout_tracks, split_rec = split_session_tracks(tracks, block_ticks=10)
    fit_times = {t for t, _ in fit_tracks.ticks}
    hold_times = {t for t, _ in holdout_tracks.ticks}
    assert fit_times.isdisjoint(hold_times)
    assert split_rec["overlap_ticks"] == 0
    assert len(fit_tracks.ticks) + len(holdout_tracks.ticks) == len(tracks.ticks)
    for f_lo, f_hi in split_rec["fit_windows_ms"]:
        for h_lo, h_hi in split_rec["holdout_windows_ms"]:
            assert f_hi < h_lo or h_hi < f_lo

    # 2) 다중 분산 창(sample_windows_ms) 입력의 교대 창 분리
    windows_ms = [[T0 + k * 3000, T0 + k * 3000 + 2100] for k in range(6)]
    win_header = {**dict(header), "sample_windows_ms": windows_ms}
    win_rows = [r for r in rows if any(lo <= r["estimated_capture_time_ms"] <= hi for lo, hi in windows_ms)]
    win_tracks = track_session(win_header, win_rows, backend_factory=BoxTracker)
    w_fit, w_hold, w_rec = split_session_tracks(win_tracks)
    assert w_rec["method"] == "sample_windows_alternating"
    assert w_rec["fit_window_count"] == 3 and w_rec["holdout_window_count"] == 3
    assert {t for t, _ in w_fit.ticks}.isdisjoint({t for t, _ in w_hold.ticks})
    for t, _ in w_fit.ticks:
        assert any(lo <= t <= hi for lo, hi in w_rec["fit_windows_ms"])
    for t, _ in w_hold.ticks:
        assert any(lo <= t <= hi for lo, hi in w_rec["holdout_windows_ms"])

    # 3) 겹치는 holdout을 직접 넘기면 거부해야 한다.
    with pytest.raises(ValueError, match="겹친다"):
        adjust_session(fit_tracks, previous, measured_camera_heights=heights,
                       input_provenance=provenance, created_at=T0 + 32000, rounds=2,
                       holdout_tracks=tracks)


def test_old_single_person_path_still_rejects_multiple_people(case):
    _, _, header, rows, tracks, _ = case
    with pytest.raises(CorrespondenceError):
        build_observations(header, rows)
    assert len(tracks.ticks[0][1]) == 8
    single = [{**r, "detections": r["detections"][:1]} for r in rows]
    assert len(build_observations(header, single).observations) > 0


def test_unknown_time_is_not_filled(case):
    _, truth, header, rows, tracks, _ = case
    unknown = [{**r, "timestamp_uncertainty_ms": None} for r in rows]
    empty_tracks = track_session(header, unknown, backend_factory=BoxTracker)
    assert all(not views for _, views in empty_tracks.ticks)
    sparse = []
    for t, views in tracks.ticks[:6]:
        sparse.append((t, views))
        sparse.append((t + 100, ()))
    frames = associate_session(SessionTracks(tuple(sparse), tracks.record), session_rig(truth))
    assert len(frames) == 6
    first_ids = {c.association_id for c in frames[0].clusters}
    last_ids = {c.association_id for c in frames[-1].clusters}
    assert first_ids == last_ids


def test_cli_session_defaults():
    args = build_parser().parse_args(["session", "--recording", "rec", "--previous", "cal.json",
                                     "--camera-heights", "heights.json", "--out", "new"])
    assert args.rounds == 3 and args.lens == "fixed"
    assert args.fixed_lens == "1284.27,-0.29198,0.05982"
    assert args.start_s == 5.0
    assert args.window_step_s == 24.0
    assert args.window_duration_s == 9.0
    assert args.every_n == 5
    assert args.anchor_cameras is None
