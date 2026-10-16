"""출력이 계약과 규칙을 지키는지 본다 (명세 20.4 Provider 계약 시험, 21.3 계약 계층).

스키마로 표현할 수 없는 규칙(R 의 직교성, 관절 배열 길이, is_upper_bound 와 출처의 일치,
2대 미만에서 보인 관절은 null, Z=0 대체 금지)을 여기서 검사한다.
"""

import hashlib
import json
import math
from pathlib import Path

import pytest

SCENARIOS = ["basic", "podium", "crossing"]
TAPO_C110 = {"image_size": [2304, 1296], "fps": 25}


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _det3(m) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_calibration_is_sim_and_not_calibrated(builtin_outputs, scenario):
    cal = _json(builtin_outputs[scenario] / "calibration.json")
    assert cal["provider_source"] == "SIM"
    assert cal["state"] in {"UNCALIBRATED", "PROVISIONAL"}
    assert cal["extrinsic_convention"] == "OPENCV_WORLD_TO_CAMERA"
    assert cal["coordinate_frame"] | {"site_id": None} == {
        "frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z", "site_id": None,
    }  # fmt: skip
    assert len(cal["cameras"]) == 4
    for cam in cal["cameras"]:
        assert cam["image_size"] == TAPO_C110["image_size"]
        R, t = cam["R"], cam["t"]
        for i in range(3):
            for j in range(3):
                dot = sum(R[i][k] * R[j][k] for k in range(3))
                assert dot == pytest.approx(1.0 if i == j else 0.0, abs=1e-6)
        assert _det3(R) == pytest.approx(1.0, abs=1e-6)
        # 카메라 중심 C = -Rᵀ t 의 높이가 camera_height_m 과 같아야 한다.
        center_z = -sum(R[k][2] * t[k] for k in range(3))
        assert center_z == pytest.approx(cam["camera_height_m"], abs=1e-5)
        K = cam["K"]
        assert K[0][2] == cam["image_size"][0] / 2 and K[1][2] == cam["image_size"][1] / 2
        assert K[2] == [0.0, 0.0, 1.0]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_manifest_provenance_and_hashes(builtin_outputs, scenario):
    out = builtin_outputs[scenario]
    man = _json(out / "manifest.json")
    prov = man["input_provenance"]
    assert prov["is_upper_bound"] is True
    assert prov["frames"]["source"] == prov["calibration"]["source"] == prov["tracks"]["source"] == "SIM"
    cal = _json(out / "calibration.json")
    assert prov["calibration"]["calibration_version"] == cal["calibration_version"] == man["calibration_version"]
    listed = {f["path"]: f for f in man["files"]}
    actual = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} - {"manifest.json"}
    assert set(listed) == actual
    for rel, entry in listed.items():
        data = (out / rel).read_bytes()
        assert entry["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()
        assert entry["bytes"] == len(data)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_observations_timing_and_keypoints(builtin_outputs, scenario):
    out = builtin_outputs[scenario]
    man = _json(out / "manifest.json")
    truth = {(r["camera_id"], r["frame_sequence"]): r for r in _jsonl(out / "truth" / "camera_frames.jsonl")}
    w, h = TAPO_C110["image_size"]
    for cam_id in man["input_provenance"]["frames"]["camera_ids"]:
        rows = _jsonl(out / "observations" / f"{cam_id}.jsonl")
        assert len(rows) == man["duration_ms"] * TAPO_C110["fps"] // 1000
        times = [r["estimated_capture_time_ms"] for r in rows]
        assert all(isinstance(t, int) for t in times)
        assert times == sorted(times) and len(set(times)) == len(times)
        for row in rows:
            assert row["timestamp_source"] == "SIM"
            gt = truth[(cam_id, row["frame_sequence"])]
            assert gt["estimated_capture_time_ms"] == row["estimated_capture_time_ms"]
            assert abs(gt["true_capture_time_ms"] - row["estimated_capture_time_ms"]) <= row["timestamp_uncertainty_ms"]
            for det in row["detections"]:
                assert det["joint_layout"] == "COCO17"
                assert len(det["keypoints_2d"]) == 17 and len(det["foot_tips_2d"]) == 2
                x1, y1, x2, y2 = det["bbox"]
                assert 0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h
                for kp in det["keypoints_2d"] + det["foot_tips_2d"]:
                    if kp is None:
                        continue
                    u, v, conf = kp
                    assert 0.0 <= conf <= 1.0
                    if conf > 0:
                        assert 0 <= u < w and 0 <= v < h


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_world_tracks_follow_tier0_semantics(builtin_outputs, scenario):
    rows = _jsonl(builtin_outputs[scenario] / "world_tracks.jsonl")
    assert rows
    for row in rows:
        assert row["provider_source"] == "SIM"
        for track in row["tracks"]:
            sk = track["skeleton"]
            n = len(sk["joints"])
            assert n == len(sk["visible"]) == len(sk["validity"]) == len(sk["joint_source"]) == 17
            counts = [0] * n
            for pc in track["per_camera"]:
                for j, kp in enumerate(pc["keypoints_2d"]):
                    counts[j] += kp is not None and kp[2] > 0
            for j in range(n):
                # 2대 미만에서 보인 관절은 지어내지 않는다.
                assert sk["visible"][j] == (counts[j] >= 2)
                assert (sk["joints"][j] is None) == (not sk["visible"][j])
            # 접지를 모르면 비운다 (Z=0 대체 금지).
            if track["support_surface_validity"] != "VALID" or track["z_validity"] == "UNAVAILABLE":
                assert track["ground_anchor_xyz"] is None
            if track["pelvis_xyz"] is None:
                assert track["ground_anchor_xyz"] is not None
            assert len({pc["camera_id"] for pc in track["per_camera"]}) == len(track["per_camera"])


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_outputs_match_contract_schemas(builtin_outputs, contract_errors, scenario):
    out = builtin_outputs[scenario]
    assert contract_errors("calibration", _json(out / "calibration.json")) == []
    for i, row in enumerate(_jsonl(out / "world_tracks.jsonl")):
        assert contract_errors("world_tracks", row) == [], f"line {i}"
    provenance_schema = {"$ref": "https://simai.dev/contracts/common/defs.schema.json#/$defs/input_provenance"}
    assert contract_errors(provenance_schema, _json(out / "manifest.json")["input_provenance"]) == []


def test_floats_are_finite(builtin_outputs):
    def walk(x):
        if isinstance(x, float):
            assert math.isfinite(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    out = builtin_outputs["basic"]
    walk(_json(out / "calibration.json"))
    for row in _jsonl(out / "truth" / "world_truth.jsonl"):
        walk(row)
