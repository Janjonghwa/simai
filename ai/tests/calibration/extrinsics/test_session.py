"""4대 합성 카메라/다중 궤적의 회전 복구와 교대 조정. SIM 지표는 상한이다."""

import copy
from dataclasses import replace

import cv2
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.criteria import CalibrationEvidence, judge_calibration
from simai_ai.calibration.document import build_calibration_doc, content_hash
from simai_ai.calibration.extrinsics.cli import build_parser
from simai_ai.calibration.extrinsics.keypoints import CorrespondenceError, build_observations
from simai_ai.calibration.extrinsics.session import FIXED_LENS, adjust_session, bootstrap_rotations, session_rig
from simai_ai.calibration.extrinsics.session_tracks import (
    SessionTracks, associate_session, association_metrics, track_session,
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
    heights = {c["camera_id"]: -np.array(c["R"]).T @ np.array(c["t"]) for c in previous["cameras"]}
    outcome = adjust_session(tracks, previous, measured_camera_heights={k: float(v[2]) for k, v in heights.items()},
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
