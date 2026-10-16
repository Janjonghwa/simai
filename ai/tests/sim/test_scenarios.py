"""시나리오 의미 시험: 보정값으로 정답을 다시 투영하면 관측과 맞는지, 단상·교차 시나리오가 의도대로인지."""

import copy
import json
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[2]
BASIC = json.loads((AI_DIR / "sim" / "scenarios" / "basic.json").read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _project(cam: dict, p: list[float]) -> tuple[float, float]:
    """calibration.json 만으로 투영한다 (x_cam = R · x_world + t, 왜곡 NONE)."""
    R, t, K = cam["R"], cam["t"], cam["K"]
    xc = [sum(R[i][k] * p[k] for k in range(3)) + t[i] for i in range(3)]
    return K[0][0] * xc[0] / xc[2] + K[0][2], K[1][1] * xc[1] / xc[2] + K[1][2]


def _noiseless(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["camera_defaults"] = dict(doc["camera_defaults"], capture_jitter_ms=0, timestamp_error_ms=0)
    for cam in doc["cameras"]:
        cam["time_offset_ms"] = 0
    doc["keypoints"] = {"pixel_noise_sigma_px": 0.0}
    return doc


def test_reprojected_truth_matches_observations(run_sim, write_scenario, tmp_path):
    """지터·잡음을 끄면 촬영 시각과 tick 이 겹치는 프레임에서 관측 keypoint = 정답 관절의 투영."""
    out = tmp_path / "out"
    assert run_sim(write_scenario(_noiseless(BASIC)), out).returncode == 0
    cams = {c["camera_id"]: c for c in json.loads((out / "calibration.json").read_text(encoding="utf-8"))["cameras"]}
    truth = {row["t"]: row for row in _jsonl(out / "truth" / "world_truth.jsonl")}
    track_ids = {p["id"]: i + 1 for i, p in enumerate(BASIC["people"])}
    checked = 0
    for cam_id, cam in cams.items():
        frames = {
            r["frame_sequence"]: r for r in _jsonl(out / "truth" / "camera_frames.jsonl") if r["camera_id"] == cam_id
        }
        for row in _jsonl(out / "observations" / f"{cam_id}.jsonl"):
            t = frames[row["frame_sequence"]]["true_capture_time_ms"]
            if t not in truth:
                continue
            dets = {d["local_track_id"]: d for d in row["detections"]}
            for person in truth[t]["people"]:
                det = dets.get(f"{cam_id}-{track_ids[person['person_id']]}")
                if det is None:
                    continue
                for joint, kp in zip(person["joints"] + person["foot_tips"], det["keypoints_2d"] + det["foot_tips_2d"]):
                    if kp is None or kp[2] == 0:
                        continue
                    u, v = _project(cam, joint)
                    # 정답 좌표는 0.01 mm 로 반올림돼 있어 픽셀로는 0.01 px 안팎까지 차이가 난다.
                    assert kp[0] == pytest.approx(u, abs=0.02) and kp[1] == pytest.approx(v, abs=0.02)
                    checked += 1
    assert checked > 1000


def test_podium_support_height(builtin_outputs):
    out = builtin_outputs["podium"]
    rows = _jsonl(out / "truth" / "world_truth.jsonl")
    student_surfaces = []
    for row in rows:
        for p in row["people"]:
            ankles = [p["joints"][15], p["joints"][16]]
            if p["person_id"] == "teacher":
                assert p["support_surface_id"] == "podium-1" and p["support_z"] == pytest.approx(0.2)
                assert p["ground_anchor_xyz"][2] == pytest.approx(0.2)
            assert all(a[2] > p["support_z"] for a in ankles)
            assert all(tip[2] > p["support_z"] for tip in p["foot_tips"])
            assert p["head_top_xyz"][2] > p["support_z"] + 1.4
            if p["person_id"] == "student":
                if not student_surfaces or student_surfaces[-1] != p["support_surface_id"]:
                    student_surfaces.append(p["support_surface_id"])
    # 학생은 바닥 → 단상 → 바닥
    assert student_surfaces == ["floor", "podium-1", "floor"]
    # world_tracks 에서도 단상 위 접지점은 Z=0 이 아니라 단상 높이다.
    anchors = [
        tr["ground_anchor_xyz"][2]
        for row in _jsonl(out / "world_tracks.jsonl")
        for tr in row["tracks"]
        if tr["support_surface_id"] == "podium-1"
    ]
    assert anchors and all(z == pytest.approx(0.2) for z in anchors)


def test_crossing_occludes_one_person(builtin_outputs):
    """교차 시나리오에서 엇갈리는 동안 한 사람이 다른 사람을 가린다 (conf 0)."""
    out = builtin_outputs["crossing"]
    start = json.loads((out / "manifest.json").read_text(encoding="utf-8"))["start_time_ms"]
    occluded_at = []
    for cam in ("cam1", "cam2", "cam3", "cam4"):
        for row in _jsonl(out / "observations" / f"{cam}.jsonl"):
            for det in row["detections"]:
                # 코·눈(0~2)은 등을 보일 때 자기 가림으로도 conf 0 이 되므로 몸 관절만 본다.
                if any(kp is not None and kp[2] == 0 for kp in det["keypoints_2d"][3:]):
                    occluded_at.append(row["estimated_capture_time_ms"] - start)
    assert occluded_at, "가림이 한 번도 없다"
    # 몸 관절은 다른 사람 원기둥으로만 가리므로 두 사람이 가까운 엇갈림 구간에만 가림이 생긴다.
    assert all(1000 <= t <= 5000 for t in occluded_at), (min(occluded_at), max(occluded_at))


def test_out_of_view_person_is_not_detected(run_sim, write_scenario, tmp_path):
    """카메라 뒤에 있는 사람은 검출되지 않는다(누락). 관절을 지어내지 않는다."""
    doc = copy.deepcopy(BASIC)
    doc["cameras"] = [{"id": "cam1", "position": [0.3, 0.3, 2.7], "look_at": [4.5, 3.75, 0.9]}]
    doc["people"] = [{"id": "behind", "waypoints": [{"t_ms": 0, "xy": [0.1, 0.1]}]}]
    doc["duration_ms"] = 400
    out = tmp_path / "out"
    assert run_sim(write_scenario(doc), out).returncode == 0
    rows = _jsonl(out / "observations" / "cam1.jsonl")
    assert rows and all(r["detections"] == [] for r in rows)
    assert all(r["tracks"] == [] for r in _jsonl(out / "world_tracks.jsonl"))


def test_hidden_keypoint_policy_null(run_sim, write_scenario, tmp_path):
    """occluded = null 이면 가린 관절은 conf 0 대신 null 로 나온다."""
    doc = json.loads((AI_DIR / "sim" / "scenarios" / "crossing.json").read_text(encoding="utf-8"))
    doc["keypoints"] = {"occluded": "null"}
    out = tmp_path / "out"
    assert run_sim(write_scenario(doc), out).returncode == 0
    kps = [
        kp
        for cam in ("cam1", "cam2", "cam3", "cam4")
        for row in _jsonl(out / "observations" / f"{cam}.jsonl")
        for det in row["detections"]
        for kp in det["keypoints_2d"]
    ]
    assert None in kps
    assert all(kp is None or kp[2] > 0 for kp in kps)


def test_face_hidden_from_behind(builtin_outputs):
    """서 있는 사람(+Y 를 봄)의 얼굴은 뒤쪽 카메라(cam1, cam2: y=0.3)에서 보이지 않고 앞쪽 카메라에서는 보인다."""
    out = builtin_outputs["basic"]
    for cam, expect_visible in (("cam1", False), ("cam2", False), ("cam3", True), ("cam4", True)):
        seen = []
        for row in _jsonl(out / "observations" / f"{cam}.jsonl"):
            det = next((d for d in row["detections"] if d["local_track_id"] == f"{cam}-2"), None)
            if det is not None:
                seen.append(all(kp is not None and kp[2] > 0 for kp in det["keypoints_2d"][:3]))
        assert seen
        if expect_visible:
            # 걷는 사람이 앞을 지날 때만 가린다.
            assert sum(seen) > 0.8 * len(seen)
        else:
            assert not any(seen)
