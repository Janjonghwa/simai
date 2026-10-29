"""외부 파라미터 실행 도구(-214)를 시뮬레이터 4대 + 걷는 사람 한 명으로 끝까지 돌린다(keypoint 파일 → cameras.json → 번들 조정 → 제안).

- keypoint 파일은 시뮬레이터 관측(픽셀 잡음 σ 1px)을 YOLO 출력처럼 COCO17 만 담아 만든다(발 끝점 없음 → 발목 접지).
- cameras.json 은 정답을 교란한 초기값(conftest `SimCase.initial`)을 MapAnything world(cam1 OpenCV 축, 스케일 1.03)로 옮겨
  만든다. World 정렬 파일은 그 닮음 변환의 역이다(-102 결과 자리).
- SIM 입력이므로 지표는 상한이다(is_upper_bound). 허용 오차는 회귀용이고 G1 합격 기준이 아니다.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.extrinsics import cli
from simai_ai.calibration.extrinsics.initial import extrinsics_to_cam2world
from simai_ai.calibration.extrinsics.keypoints import (
    KEYPOINT_FILE_FORMAT,
    write_keypoint_file,
)
from simai_ai.calibration.intrinsics import (
    IntrinsicsEstimate,
    IntrinsicsUncertainty,
    build_intrinsics_record,
    combine_shared_lens,
)
from simai_ai.calibration.intrinsics.estimate import radial_pixel_from_opencv

ONE_WALKER = [
    {
        "id": "walker",
        "height_m": 1.74,
        "waypoints": [
            {"t_ms": 0, "xy": [1.5, 1.5]},
            {"t_ms": 5000, "xy": [7.5, 1.5]},
            {"t_ms": 9000, "xy": [7.5, 6.0]},
            {"t_ms": 14000, "xy": [1.5, 6.0]},
            {"t_ms": 18000, "xy": [1.5, 1.5]},
            {"t_ms": 24000, "xy": [7.5, 6.0]},
        ],
    }
]
TWO_WALKERS = ONE_WALKER + [
    {"id": "walker2", "height_m": 1.62, "waypoints": [{"t_ms": 0, "xy": [3.0, 5.0]}, {"t_ms": 12000, "xy": [6.0, 2.5]}]}
]
SCALE_M = 1.03
WALKABLE = "0.5,0.5;8.5,0.5;8.5,7.0;0.5,7.0"


def _jsonl(path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _keypoint_file(case, path, drop_joint_except=None):
    rows = []
    for obs_path in sorted((case.out / "observations").glob("*.jsonl")):
        for row in _jsonl(obs_path):
            dets = []
            for det in row["detections"]:
                kps = [list(k) if k is not None else None for k in det["keypoints_2d"]]
                if drop_joint_except is not None and row["camera_id"] != drop_joint_except[1]:
                    kps[drop_joint_except[0]] = None
                dets.append({"bbox": det["bbox"], "score": 1.0, "keypoints_2d": kps})
            rows.append({k: row[k] for k in ("camera_id", "stream_epoch", "frame_sequence", "estimated_capture_time_ms",
                                              "timestamp_source", "timestamp_uncertainty_ms")} | {"detections": dets})
    header = {
        "format": KEYPOINT_FILE_FORMAT,
        "format_version": "0.1.0",
        "created_at": case.manifest["start_time_ms"],
        "session_id": "sim",
        "input_provenance": case.input_provenance,
        "cameras": [{"camera_id": cid, "image_size": cam["image_size"]} for cid, cam in sorted(case.truth_cameras.items())],
    }
    write_keypoint_file(path, header, rows)
    return path


def _intrinsics_file(intr, path):
    estimates = []
    for cam in intr.cameras:
        c = cam.camera
        f, size, coeffs = c["K"][0][0], tuple(c["image_size"]), tuple(c["distortion"]["coeffs"])
        estimates.append(
            IntrinsicsEstimate(
                camera_id=cam.camera_id, image_size=size, focal_px=f, distortion_coeffs=coeffs,
                radial_pixel=radial_pixel_from_opencv(coeffs, f, size),
                uncertainty=IntrinsicsUncertainty(focal_std_px=0.05 * f, k1_std=0.05, k2_std=0.03),
                method="SIM_PERTURBED", method_version="t", status="OK",
                principal_point=(c["K"][0][2], c["K"][1][2]), principal_point_assumed=False,
            )
        )
    shared = combine_shared_lens(estimates)
    record = build_intrinsics_record(created_at=1_760_342_400_000, frames={"source": "SIM"}, estimates=estimates, shared=shared)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _mapanything_files(extr, ref, tmp_path):
    """World 기준 (R, t) → MapAnything world(ref 카메라 OpenCV 축 × SCALE_M) cameras.json 과 되돌리는 정렬 파일."""
    RA, tA = np.array(ref[0]), np.array(ref[1])
    views = []
    for cid, (R, t) in extr.items():
        Rm = np.array(R) @ RA.T
        tm = SCALE_M * (np.array(t) - Rm @ tA)
        views.append({"name": cid, "cam2world": extrinsics_to_cam2world(Rm, tm).tolist(), "metric_scaling_factor": 2.9})
    cams = tmp_path / "cameras.json"
    cams.write_text(json.dumps({"run_id": "sim", "convention": "cam2world 4x4, OpenCV, MapAnything world, m (NOT_VERIFIED)",
                                "views": views}), encoding="utf-8")
    M = np.eye(4)
    M[:3, :3] = RA.T / SCALE_M
    M[:3, 3] = -RA.T @ tA
    align = tmp_path / "align.json"
    align.write_text(json.dumps({"format": "simai.world_alignment", "mapanything_to_world": M.tolist()}), encoding="utf-8")
    return cams, align


@pytest.fixture(scope="module")
def one_walker(tmp_path_factory, ba):
    return ba.SimCase(ba.run_sim(tmp_path_factory.mktemp("x-one"), ONE_WALKER, 24000))


@pytest.fixture(scope="module")
def inputs(one_walker, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("x-in")
    intr, extr = one_walker.initial(seed=7)
    ref = one_walker.truth_cameras["cam1"]
    cams, align = _mapanything_files(extr, (ref["R"], ref["t"]), tmp)
    return {
        "dir": tmp,
        "keypoints": _keypoint_file(one_walker, tmp / "kp.jsonl"),
        "intrinsics": _intrinsics_file(intr, tmp / "intr.json"),
        "cameras": cams,
        "align": align,
        "initial": extr,
    }


def _run(inputs, out, *extra, keypoints=None, intrinsics=None, now_ms=1_760_342_500_000):
    code = cli.main(
        ["adjust", "--keypoints", str(keypoints or inputs["keypoints"]),
         "--intrinsics", str(intrinsics or inputs["intrinsics"]),
         "--cameras-json", str(inputs["cameras"]), "--out", str(out), *extra],
        now_ms=now_ms,
    )
    return code, json.loads(out.read_text(encoding="utf-8"))


def _rot_deg(Ra, Rb):
    return float(np.degrees(np.linalg.norm(Rotation.from_matrix(np.asarray(Ra).T @ np.asarray(Rb)).as_rotvec())))


def _pitch(R):
    return math.degrees(math.asin(float(np.asarray(R)[2, 2])))


@pytest.fixture(scope="module")
def provided(inputs):
    return _run(inputs, inputs["dir"] / "out-provided.json", "--world-alignment", str(inputs["align"]),
                "--walkable-area", WALKABLE)


def test_end_to_end_converges_near_truth_with_provided_world(provided, one_walker, inputs):
    code, out = provided
    assert code == cli.EXIT_OK, (out.get("proposal_skipped"), out["bundle"].get("flags"))
    assert out["bundle"]["status"] == "CONVERGED"
    assert out["world_frame"]["source"] == "PROVIDED"
    errors = {}
    for cam in out["cameras"]:
        cid = cam["camera_id"]
        gt = one_walker.truth_cameras[cid]
        R0, t0 = inputs["initial"][cid]
        C = -np.array(cam["R"]).T @ np.array(cam["t"])
        C0 = -np.array(R0).T @ np.array(t0)
        Cgt = one_walker.truth_center(cid)
        errors[cid] = {
            "rot_deg": (_rot_deg(R0, gt["R"]), _rot_deg(cam["R"], gt["R"])),
            "center_m": (float(np.linalg.norm(C0 - Cgt)), float(np.linalg.norm(C - Cgt))),
            "height_m": float(C[2] - Cgt[2]),
            "pitch_deg": _pitch(cam["R"]) - _pitch(gt["R"]),
        }
        assert cam["reprojection_error_px"]["median"] <= 2.0, cid
    # 카메라 쌍의 상대 회전(벽 방향 게이지와 무관): 2° 교란에서 정답 가까이로 수렴한다
    final = {c["camera_id"]: c["R"] for c in out["cameras"]}
    for a, b in (("cam1", "cam2"), ("cam2", "cam3"), ("cam3", "cam4"), ("cam4", "cam1")):
        gt = np.array(one_walker.truth_cameras[a]["R"]) @ np.array(one_walker.truth_cameras[b]["R"]).T
        rel0 = np.array(inputs["initial"][a][0]) @ np.array(inputs["initial"][b][0]).T
        rel = np.array(final[a]) @ np.array(final[b]).T
        errors[f"{a}-{b}"] = (_rot_deg(rel0, gt), _rot_deg(rel, gt))
        assert errors[f"{a}-{b}"][1] < 0.2 < errors[f"{a}-{b}"][0], (a, b)
    print("SIM 상한 외부 파라미터 오차(초기, 결과):", json.dumps(errors, indent=1))
    for cid, e in errors.items():
        if "-" in cid:
            continue
        # 남은 회전 오차는 게이지(벽 방향 = 초기값 평균)로 고정한 공통 yaw 몫이다
        assert e["rot_deg"][1] < e["rot_deg"][0], cid
        assert e["center_m"][1] < e["center_m"][0] or e["center_m"][1] < 0.1, cid
        assert abs(e["height_m"]) < 0.06, cid
        assert abs(e["pitch_deg"]) < 0.3, cid


def test_sim_result_is_provisional_with_model_scale(provided):
    _, out = provided
    proposal = out["proposal"]
    assert proposal["adopted"] is True and proposal["state"] == "PROVISIONAL"
    assert {"SOURCE", "SCALE_PRIMARY"} <= set(proposal["failed_criteria"])  # SIM 출처·스케일 근거 없음
    doc = proposal["doc"]
    assert doc["provider_source"] == "SIM" and doc["scale_basis"] == "MODEL_ESTIMATE" and doc["state"] == "PROVISIONAL"
    assert out["bundle"]["scale"]["determined"] is False
    assert out["input_provenance"]["is_upper_bound"] is True


def test_interim_world_runs_but_is_not_proposed(inputs, one_walker):
    code, out = _run(inputs, inputs["dir"] / "out-interim.json")
    assert code == cli.EXIT_NOT_PROPOSED
    assert out["proposal"] is None and "INTERIM" in out["proposal_skipped"]
    assert out["world_frame"]["source"] == "INTERIM_PERSON_VERTICAL"
    assert out["bundle"]["status"] == "CONVERGED"
    # 벽 방향·원점과 무관한 값(카메라 높이·내려다보는 각)은 사람 세로축 임시 정렬로도 정답 가까이다.
    # 정물이 없으므로 스케일은 cameras.json(MapAnything 메트릭, 여기서는 3% 큼)을 그대로 이어받는다
    for cam in out["cameras"]:
        gt = one_walker.truth_cameras[cam["camera_id"]]
        C = -np.array(cam["R"]).T @ np.array(cam["t"])
        assert abs(C[2] - SCALE_M * one_walker.truth_center(cam["camera_id"])[2]) < 0.04, cam["camera_id"]
        assert abs(_pitch(cam["R"]) - _pitch(gt["R"])) < 0.3, cam["camera_id"]


def test_joint_seen_by_one_camera_is_not_a_variable(inputs, one_walker, provided):
    # 왼 손목(9)을 cam1 에서만 남긴다: 2대 미만에서 보인 관절은 3D 점이 되지 않는다
    kp = _keypoint_file(one_walker, inputs["dir"] / "kp-wrist.jsonl", drop_joint_except=(9, "cam1"))
    code, out = _run(inputs, inputs["dir"] / "out-wrist.json", "--world-alignment", str(inputs["align"]), keypoints=kp)
    assert code == cli.EXIT_OK
    full = provided[1]["bundle"]["rounds"][0]["points"]
    dropped = out["bundle"]["rounds"][0]["points"]
    samples = provided[1]["correspondence"]["observations"]
    assert 0 < full - dropped <= samples
    assert "forearm" in out["bundle"]["bone_lengths_m"]["walker"]  # 오른쪽 아래팔로 뼈 변수는 남는다


def test_two_walkers_are_not_matched_by_guess(tmp_path, ba, inputs):
    case = ba.SimCase(ba.run_sim(tmp_path, TWO_WALKERS, 12000))
    kp = _keypoint_file(case, tmp_path / "kp2.jsonl")
    code, out = _run(inputs, tmp_path / "out2.json", "--world-alignment", str(inputs["align"]), keypoints=kp)
    # 두 사람이 내내 함께 보이면 누가 누구인지 모르므로 관측을 만들지 않고, 조정·제안도 하지 않는다
    assert code == cli.EXIT_NOT_USABLE
    assert "2대 미만" in out["correspondence_error"] and out["proposal"] is None and "bundle" not in out


def test_config_errors_exit_2(inputs, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"run_id": "x", "convention": "world2cam", "views": []}), encoding="utf-8")
    code = cli.main(["adjust", "--keypoints", str(inputs["keypoints"]), "--intrinsics", str(inputs["intrinsics"]),
                     "--cameras-json", str(bad), "--out", str(tmp_path / "o.json")], now_ms=1_760_342_500_000)
    assert code == cli.EXIT_CONFIG and "convention" in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()


# --------------------------------------------------------------------------- 출처 합치기 (리뷰 R1 반례)

FILE_PROVENANCE = {"frames": {"source": "FILE", "dataset_id": "recording/rec-x"}, "is_upper_bound": False}
LIVE_PROVENANCE = {"frames": {"source": "LIVE"}, "is_upper_bound": False}
SIM_PROVENANCE = {"frames": {"source": "SIM"}, "is_upper_bound": True}


def _with_provenance(src, dst, provenance, key="input_provenance"):
    """JSON(첫 줄이 머리인 JSONL 포함) 파일의 input_provenance 만 바꾼 사본."""
    text = src.read_text(encoding="utf-8")
    if src.suffix == ".jsonl":
        first, rest = text.split("\n", 1)
        doc = json.loads(first)
        doc[key] = provenance
        dst.write_text(json.dumps(doc) + "\n" + rest, encoding="utf-8")
    else:
        doc = json.loads(text)
        doc[key] = provenance
        dst.write_text(json.dumps(doc), encoding="utf-8")
    return dst


@pytest.fixture(scope="module")
def file_keypoints(inputs):
    """실제 녹화처럼 frames FILE 만 적힌 keypoint 파일(값은 SIM 관측 그대로)."""
    return _with_provenance(inputs["keypoints"], inputs["dir"] / "kp-file.jsonl", FILE_PROVENANCE)


@pytest.fixture(scope="module")
def live_intrinsics(inputs):
    return _with_provenance(inputs["intrinsics"], inputs["dir"] / "intr-live.json", LIVE_PROVENANCE)


def test_sim_intrinsics_make_file_keypoint_run_an_upper_bound(inputs, file_keypoints):
    # keypoint 는 FILE 인데 내부 파라미터 기록이 SIM 이면 결과 전체가 상한이고 SIM 출처다(G1_AUTO 로 기록하지 않는다)
    code, out = _run(inputs, inputs["dir"] / "out-mixed.json", "--world-alignment", str(inputs["align"]),
                     keypoints=file_keypoints)
    assert code == cli.EXIT_OK
    assert out["input_provenance"]["is_upper_bound"] is True
    assert out["input_provenance"]["calibration"]["source"] == "SIM"
    assert out["input_sources"]["upper_bound_inputs"] == {"intrinsics": "SIM"}
    assert out["bundle"]["provider_source"] == "SIM"
    proposal = out["proposal"]
    assert proposal["doc"]["provider_source"] == "SIM" and proposal["state"] == "PROVISIONAL"
    assert "SOURCE" in proposal["failed_criteria"]


def test_file_keypoints_and_live_intrinsics_record_mapanything_as_model_estimate(inputs, file_keypoints, live_intrinsics):
    code, out = _run(inputs, inputs["dir"] / "out-live.json", "--world-alignment", str(inputs["align"]),
                     keypoints=file_keypoints, intrinsics=live_intrinsics)
    assert code == cli.EXIT_OK
    prov = out["input_provenance"]
    assert prov["is_upper_bound"] is False
    assert prov["calibration"] == {"source": "MODEL_ESTIMATE", "calibration_version": "mapanything-sim"}
    assert out["bundle"]["provider_source"] == "G1_AUTO"  # 상한 입력이 없으면 무마커 자동 보정 출처다
    assert out["inputs"]["cameras_json"]["sha256"].startswith("sha256:")
    assert out["world_frame"]["sha256"].startswith("sha256:") and out["world_frame"]["mapanything_to_world"]


def test_sim_world_alignment_also_makes_an_upper_bound(inputs, file_keypoints, live_intrinsics, tmp_path):
    align = _with_provenance(inputs["align"], tmp_path / "align-sim.json", SIM_PROVENANCE)
    code, out = _run(inputs, tmp_path / "out.json", "--world-alignment", str(align), keypoints=file_keypoints,
                     intrinsics=live_intrinsics)
    assert code == cli.EXIT_OK
    assert out["input_provenance"]["is_upper_bound"] is True and out["bundle"]["provider_source"] == "SIM"
    assert out["input_sources"]["upper_bound_inputs"] == {"world_alignment": "SIM"}


def test_existing_result_file_is_not_overwritten(inputs, tmp_path, capsys):
    out = tmp_path / "exists.json"
    out.write_text("old", encoding="utf-8")
    code = cli.main(["adjust", "--keypoints", str(inputs["keypoints"]), "--intrinsics", str(inputs["intrinsics"]),
                     "--cameras-json", str(inputs["cameras"]), "--out", str(out)], now_ms=1_760_342_500_000)
    assert code == cli.EXIT_CONFIG and "덮어쓰지" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "old"


def test_world_alignment_from_another_mapanything_run_is_refused(inputs, tmp_path, capsys):
    """-102 결과는 만든 cameras.json 의 sha256 을 싣는다. 다른 실행의 cameras.json 과 섞으면 거절(종료 2)."""
    doc = json.loads(inputs["align"].read_text(encoding="utf-8"))
    doc["cameras_json_sha256"] = "sha256:" + "0" * 64
    align = tmp_path / "align-other.json"
    align.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "out-other.json"
    code = cli.main(["adjust", "--keypoints", str(inputs["keypoints"]), "--intrinsics", str(inputs["intrinsics"]),
                     "--cameras-json", str(inputs["cameras"]), "--world-alignment", str(align), "--out", str(out)],
                    now_ms=1_760_342_500_000)
    assert code == cli.EXIT_CONFIG and "다른 MapAnything 실행" in capsys.readouterr().err
    assert not out.exists()


def test_failed_world_frame_result_is_refused(inputs, tmp_path, capsys):
    """-102 가 바닥·축을 못 정한 결과(status FAILED, 변환 없음)는 World 정렬로 받지 않는다(임시 정렬로 바꾸지도 않는다)."""
    align = tmp_path / "align-failed.json"
    align.write_text(json.dumps({"format": "simai.world_alignment", "status": "FAILED", "reason": "FLOOR_NOT_FOUND",
                                 "detail": "바닥 없음"}), encoding="utf-8")
    out = tmp_path / "out-failed.json"
    code = cli.main(["adjust", "--keypoints", str(inputs["keypoints"]), "--intrinsics", str(inputs["intrinsics"]),
                     "--cameras-json", str(inputs["cameras"]), "--world-alignment", str(align), "--out", str(out)],
                    now_ms=1_760_342_500_000)
    assert code == cli.EXIT_CONFIG and "FLOOR_NOT_FOUND" in capsys.readouterr().err
    assert not out.exists()


def test_world_alignment_made_with_other_intrinsics_is_refused(inputs, tmp_path, capsys):
    """-102 결과는 쓴 내부 파라미터 기록 sha256(inputs.intrinsics.sha256)을 싣는다. --intrinsics 와 다르면 거절(종료 2)."""
    doc = json.loads(inputs["align"].read_text(encoding="utf-8"))
    doc["inputs"] = {"intrinsics": {"path": "other.json", "sha256": "sha256:" + "2" * 64}}
    align = tmp_path / "align-intr.json"
    align.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "out-intr.json"
    code = cli.main(["adjust", "--keypoints", str(inputs["keypoints"]), "--intrinsics", str(inputs["intrinsics"]),
                     "--cameras-json", str(inputs["cameras"]), "--world-alignment", str(align), "--out", str(out)],
                    now_ms=1_760_342_500_000)
    assert code == cli.EXIT_CONFIG and "다른 내부 파라미터" in capsys.readouterr().err
    assert not out.exists()


# --------------------------------------------------------------------------- 촬영 시각 바로잡기 (-313)


def _with_broken_pts(src, dst, cameras=("cam1", "cam2", "cam3")):
    """로비 녹화 같은 시각 오류를 넣은 사본: cameras 의 PTS 가 13장 묶음마다 번갈아 420 ms 이르다(로비 실측 370~440 ms, 묶음 안 간격은 맞다)."""
    lines = src.read_text(encoding="utf-8").splitlines()
    out = [lines[0]]
    for ln in lines[1:]:
        row = json.loads(ln)
        if row["camera_id"] in cameras and (row["frame_sequence"] // 13) % 2:
            row["estimated_capture_time_ms"] -= 420
        out.append(json.dumps(row))
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    return dst


@pytest.fixture(scope="module")
def broken_pts_keypoints(inputs):
    return _with_broken_pts(inputs["keypoints"], inputs["dir"] / "kp-time.jsonl")


def test_broken_pts_make_too_many_outliers_without_sync(inputs, broken_pts_keypoints):
    code, out = _run(inputs, inputs["dir"] / "out-time-off.json", "--world-alignment", str(inputs["align"]),
                     "--no-time-sync", keypoints=broken_pts_keypoints)
    print("시각 바로잡기 없음:", json.dumps(out["bundle"]["outliers"]))
    assert code == cli.EXIT_NOT_USABLE
    assert out["bundle"]["status"] == "UNCERTAIN" and "TOO_MANY_OUTLIERS" in out["bundle"]["flags"]
    assert out["timing"] is None and out["correspondence"]["time_offsets_ms"] is None


def test_time_sync_retimes_broken_pts_and_converges(inputs, broken_pts_keypoints, one_walker):
    code, out = _run(inputs, inputs["dir"] / "out-time-on.json", "--world-alignment", str(inputs["align"]),
                     keypoints=broken_pts_keypoints)
    print("시각 바로잡기:", json.dumps(out["bundle"]["outliers"]), json.dumps(out["timing"]["applied_offsets_ms"]))
    assert code == cli.EXIT_OK, (out.get("proposal_skipped"), out["bundle"].get("flags"))
    timeline = out["timing"]["timeline"]
    assert timeline["cam1"]["retimed"] and timeline["cam2"]["retimed"] and timeline["cam3"]["retimed"]
    assert not timeline["cam4"]["retimed"]
    assert any("frame_sequence 시계로 다시 매겼다" in w for w in out["warnings"])
    assert out["bundle"]["status"] == "CONVERGED" and out["bundle"]["outliers"]["removed_ratio"] <= 0.10
    for cam in out["cameras"]:
        C = -np.array(cam["R"]).T @ np.array(cam["t"])
        assert abs(C[2] - one_walker.truth_center(cam["camera_id"])[2]) < 0.06, cam["camera_id"]
        assert cam["reprojection_error_px"]["median"] <= 2.0, cam["camera_id"]


# --------------------------------------------------------------------------- 렌즈 모드·실측 카메라 높이 (-321)


def _heights_file(path, heights):
    path.write_text(
        json.dumps({"format": "simai.camera_heights", "heights_m": heights, "method": "SIM 정답", "measured_at": "t"}),
        encoding="utf-8",
    )
    return path


def test_shared_lens_and_camera_heights_end_to_end(inputs, one_walker, tmp_path):
    heights = {cid: float(one_walker.truth_center(cid)[2]) for cid in ("cam1", "cam2", "cam3", "cam4")}
    hfile = _heights_file(tmp_path / "heights.json", heights)
    code, out = _run(inputs, tmp_path / "o.json", "--world-alignment", str(inputs["align"]), "--lens", "shared",
                     "--camera-heights", str(hfile))
    assert code == cli.EXIT_OK, (out.get("proposal_skipped"), out["bundle"].get("flags"))
    bundle = out["bundle"]
    assert bundle["lens"]["mode"] == "shared" and len({c["K"][0][0] for c in out["cameras"]}) == 1
    assert bundle["scale"]["source"] == "MEASURED_CAMERA_HEIGHT" and bundle["scale"]["camera_heights"]["applied"]
    assert out["inputs"]["camera_heights"]["heights_m"] == heights
    assert out["inputs"]["camera_heights"]["sha256"].startswith("sha256:")
    assert out["proposal"]["doc"]["scale_basis"] == "MEASURED_CAMERA_HEIGHT"
    for cam in out["cameras"]:
        C = -np.array(cam["R"]).T @ np.array(cam["t"])
        assert abs(C[2] - heights[cam["camera_id"]]) < 0.03, cam["camera_id"]


@pytest.mark.parametrize(
    "extra,message",
    [
        (["--fixed-lens", "1000,-0.1,0.0"], "--lens fixed"),
        (["--lens", "fixed", "--fixed-lens", "1000,-0.1"], "F,K1,K2"),
        (["--camera-heights", "HEIGHTS"], "cam9"),
    ],
)
def test_lens_and_height_config_errors_exit_2(inputs, tmp_path, capsys, extra, message):
    hfile = _heights_file(tmp_path / "h.json", {"cam1": 2.7, "cam9": 2.6})
    extra = [str(hfile) if v == "HEIGHTS" else v for v in extra]
    code = cli.main(["adjust", "--keypoints", str(inputs["keypoints"]), "--intrinsics", str(inputs["intrinsics"]),
                     "--cameras-json", str(inputs["cameras"]), "--out", str(tmp_path / "o.json"), *extra],
                    now_ms=1_760_342_500_000)
    assert code == cli.EXIT_CONFIG and message in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()
