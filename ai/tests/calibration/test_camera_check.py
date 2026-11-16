"""세팅 「카메라 확인」 평가기 시험(S15P31A101-646). 합성 카메라 4대에 한 사람이 걷는 view 를 투영해 관제와 같은 계산을 본다."""

from __future__ import annotations

import copy
import dataclasses
import json
import math

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.camera_check import (
    FAIL,
    FAILED,
    INCOMPLETE,
    MEASURING,
    NOT_OVERLAPPING,
    PASS,
    PASSED,
    WAITING,
    CameraCheckConfig,
    CameraCheckEvaluator,
    _problems,
    baseline_from_json,
    load_baseline,
    save_baseline,
    views_from_json,
    views_to_json,
)
from simai_ai.calibration.monitor import (
    BASELINE_SOURCE_SETUP_CAMERA_CHECK,
    BaselineRecord,
    MonitorConfig,
    MonitorStatus,
    ReprojectionMonitor,
)
from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.providers.contract import schema_errors
from simai_ai.tracking.cross_view import LocalView

T0 = 1_760_342_400_000
SIZE = (2304, 1296)
CORNERS = {"cam1": (0.3, 0.3, 2.7), "cam2": (8.7, 0.3, 2.7), "cam3": (8.7, 7.2, 2.7), "cam4": (0.3, 7.2, 2.7)}
OFFSETS = [(0.0, 1.60), (-0.03, 1.63), (0.03, 1.63), (-0.08, 1.60), (0.08, 1.60), (-0.20, 1.40), (0.20, 1.40),
           (-0.25, 1.10), (0.25, 1.10), (-0.25, 0.85), (0.25, 0.85), (-0.12, 0.95), (0.12, 0.95), (-0.12, 0.50),
           (0.12, 0.50), (-0.12, 0.08), (0.12, 0.08)]
CFG = CameraCheckConfig(min_duration_ms=10_000, max_duration_ms=20_000, min_pair_samples=10, min_camera_views=10,
                        min_loo_samples=5, min_evaluated_pairs=3)
MON = MonitorConfig(min_window_samples=5)
# 세팅 보정(배경 BA)이 맞추는 4대 고리의 옆 쌍. cam1–cam3·cam2–cam4 가 대각이다
RING_PAIRS = [("cam1", "cam2"), ("cam2", "cam3"), ("cam3", "cam4"), ("cam4", "cam1")]
CAMERA_CHECK_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/camera_check"}


def _look_at(position, target):
    z = np.subtract(target, position)
    z = z / np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x = x / np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.vstack([x, y, z])
    return R, -R @ np.asarray(position, dtype=float)


def _doc(version="cal-check-1", rotate=None):
    f = (SIZE[0] / 2) / math.tan(math.radians(105) / 2)
    cameras = []
    for cid, pos in CORNERS.items():
        R, t = _look_at(pos, (4.5, 3.75, 0.9))
        if rotate and cid in rotate:
            C = np.asarray(pos, dtype=float)
            R = Rotation.from_euler("y", rotate[cid], degrees=True).as_matrix() @ R
            t = -R @ C
        cameras.append({"camera_id": cid, "image_size": list(SIZE),
                        "K": [[f, 0.0, SIZE[0] / 2], [0.0, f, SIZE[1] / 2], [0.0, 0.0, 1.0]],
                        "distortion": {"model": "NONE", "coeffs": []}, "R": R.tolist(), "t": t.tolist()})
    return {"schema_version": "1.1.0", "calibration_version": version, "provider_source": "SIM", "state": "PROVISIONAL",
            "created_at": T0, "coordinate_frame": {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"},
            "scale_basis": "DATASET_GT", "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA", "cameras": cameras}


def _project(doc, cam_id, joints, rng, noise_px=0.5):
    cam = next(c for c in doc["cameras"] if c["camera_id"] == cam_id)
    R, t, K = np.asarray(cam["R"]), np.asarray(cam["t"]), np.asarray(cam["K"])
    kps = []
    for X in joints:
        xc = R @ X + t
        uv = K[:2, :2] @ (xc[:2] / xc[2]) + K[:2, 2] + rng.normal(0.0, noise_px, 2)
        kps.append((float(uv[0]), float(uv[1]), 0.9))
    return tuple(kps)


def _walk(truth, seconds=24.0, hz=5.0, people=1, cameras=tuple(CORNERS), seed=7, noise_px=0.5):
    """한(또는 여러) 사람이 방 가운데를 원으로 걷는 view 묶음 목록 [(t, views)]."""
    rng = np.random.default_rng(seed)
    out = []
    for k in range(int(seconds * hz)):
        t = T0 + int(k * 1000 / hz)
        views = []
        for p in range(people):
            a = 2 * math.pi * k / (seconds * hz) + p * math.pi
            x, y = 4.5 + 1.8 * math.cos(a), 3.75 + 1.4 * math.sin(a)
            joints = [np.array([x, y - dx, dz]) for dx, dz in OFFSETS]
            for c in cameras:
                kps = _project(truth, c, joints, rng, noise_px)
                views.append(LocalView(c, f"{c}-p{p}", InterpolatedView(c, t, kps, "SIM", 0.0, (t,))))
        out.append((t, views))
    return out


def _run(candidate, frames, **kwargs):
    ev = CameraCheckEvaluator(candidate, CFG, monitor_config=MON, **kwargs)
    summary = ev.summary()
    for t, views in frames:
        ev.observe(t, views)
        summary = ev.summary()
        if summary["status"] in (PASSED, FAILED):
            break
    return ev, summary


def test_true_calibration_passes_and_gives_verified_baseline():
    truth = _doc()
    ev, summary = _run(truth, _walk(truth))
    assert schema_errors(summary, CAMERA_CHECK_SCHEMA) == []
    assert summary["status"] == PASSED and summary["problems"] == []
    assert summary["progress_pct"] == 100.0
    verdicts = {k: p["verdict"] for k, p in summary["pairs"].items()}
    assert all(v in (PASS, NOT_OVERLAPPING) for v in verdicts.values()), verdicts
    assert sum(v == PASS for v in verdicts.values()) >= 4
    assert all(c["verdict"] == PASS for c in summary["cameras"].values())
    record = ev.baseline_record()
    assert set(record.verified_camera_ids) == set(CORNERS)
    assert all(v < 3.0 for v in record.values_px.values())
    assert set(record.sources.values()) == {BASELINE_SOURCE_SETUP_CAMERA_CHECK}
    assert summary["baseline"]["values_px"] == {c: round(v, 4) for c, v in sorted(record.values_px.items())}
    # 끝난 뒤에는 입력을 더 받아도 같은 판정이다
    ev.observe(T0 + 10_000_000, _walk(truth, seconds=1)[0][1])
    assert ev.summary() == summary


def test_rotated_camera_candidate_fails_with_problem_camera():
    """보정 후보의 cam2 가 실제보다 3° 돌아가 있으면(14차 B–C 같은 쌍 실패) 그 카메라가 낀 쌍·카메라가 실패한다."""
    truth = _doc()
    candidate = _doc(rotate={"cam2": 3.0})
    _, summary = _run(candidate, _walk(truth))
    assert schema_errors(summary, CAMERA_CHECK_SCHEMA) == []
    assert summary["status"] == FAILED and "baseline" not in summary
    failed_pairs = [k for k, p in summary["pairs"].items() if p["verdict"] == FAIL]
    assert failed_pairs and all("cam2" in summary["pairs"][k]["camera_ids"] for k in failed_pairs)
    assert any("cam2" in p["camera_ids"] for p in summary["problems"])
    assert all(p["message"] for p in summary["problems"])


def test_waiting_then_measuring_and_progress_is_monotone_until_close():
    truth = _doc()
    ev = CameraCheckEvaluator(truth, CFG, monitor_config=MON)
    assert ev.summary()["status"] == WAITING
    ev.observe(T0 - 1_000, [])
    assert ev.summary()["status"] == WAITING  # 사람이 없으면 시작하지 않는다
    last = 0.0
    for t, views in _walk(truth, seconds=6):
        ev.observe(t, views)
        s = ev.summary()
        assert s["status"] == MEASURING and s["progress_pct"] >= last and s["progress_pct"] < 100
        last = s["progress_pct"]


def test_pairs_count_only_single_person_ticks():
    truth = _doc()
    ev = CameraCheckEvaluator(truth, CFG, monitor_config=MON)
    for t, views in _walk(truth, seconds=4, people=2):
        ev.observe(t, views)
    summary = ev.summary()
    assert all(p["samples"] == 0 for p in summary["pairs"].values())
    assert all(c["observed_views"] > 0 for c in summary["cameras"].values())


def test_unobserved_camera_fails_at_max_duration():
    truth = _doc()
    frames = _walk(truth, seconds=24, cameras=("cam1", "cam2", "cam3"))
    _, summary = _run(truth, frames)
    assert summary["status"] == FAILED
    assert summary["cameras"]["cam4"]["verdict"] == FAIL
    assert any(p["reason"] == "NOT_OBSERVED" and p["id"] == "cam4" for p in summary["problems"])


def test_required_pair_without_evidence_fails_instead_of_passing():
    """품질 게이트 '확인 필요' 쌍(홀드아웃 부족)은 사람으로도 확인하지 못하면 통과가 아니다."""
    truth = _doc()
    # cam3 와 cam4 가 동시에 보이지 않게 번갈아 지운다(같은 시각 두 카메라에 한 사람이 없다)
    frames = []
    for i, (t, views) in enumerate(_walk(truth, seconds=24)):
        drop = "cam3" if i % 2 else "cam4"
        frames.append((t, [v for v in views if v.camera_id != drop]))
    _, summary = _run(truth, frames, required_pairs=[("cam4", "cam3")])
    pair = summary["pairs"]["cam3-cam4"]
    assert pair["required"] is True and pair["samples"] == 0 and pair["verdict"] == FAIL
    assert summary["status"] == FAILED
    assert any(p["reason"] == "NOT_CONFIRMED" and p["id"] == "cam3-cam4" for p in summary["problems"])


def test_incomplete_freezes_summary():
    truth = _doc()
    ev = CameraCheckEvaluator(truth, CFG, monitor_config=MON)
    for t, views in _walk(truth, seconds=3):
        ev.observe(t, views)
    out = ev.incomplete()
    assert out["status"] == INCOMPLETE and out["problems"]
    assert schema_errors(out, CAMERA_CHECK_SCHEMA) == []
    assert ev.final and ev.summary()["status"] == INCOMPLETE


def test_incomplete_names_the_sample_shortage_so_the_screen_can_guide_a_retry():
    """S15P31A101-687: INCOMPLETE 는 표본 부족 사유만 싣는다. 사람이 없었으면 NO_PERSON, 덜 보인 카메라는 그 카메라 FEW_SAMPLES."""
    truth = _doc()
    nobody = CameraCheckEvaluator(truth, CFG, monitor_config=MON)
    nobody.observe(T0, [])
    out = nobody.incomplete()
    assert [p["reason"] for p in out["problems"]] == ["NO_PERSON"]
    assert schema_errors(out, CAMERA_CHECK_SCHEMA) == []

    ev = CameraCheckEvaluator(truth, CFG, monitor_config=MON)
    for t, views in _walk(truth, seconds=8, cameras=("cam1", "cam2", "cam3")):
        ev.observe(t, views)
    for t, views in _walk(truth, seconds=1, cameras=("cam4",)):
        ev.observe(t + 8_000, views)
    out = ev.incomplete()
    reasons = {(p["reason"], p["id"]) for p in out["problems"]}
    assert reasons == {("FEW_SAMPLES", "cam4")}
    assert out["problems"][0]["camera_ids"] == ["cam4"]
    assert schema_errors(out, CAMERA_CHECK_SCHEMA) == []


def test_person_visible_follows_the_latest_view_time():
    truth = _doc()
    ev = CameraCheckEvaluator(truth, CFG, monitor_config=MON)
    frames = _walk(truth, seconds=2)
    for t, views in frames:
        ev.observe(t, views)
    assert all(c["person_visible"] for c in ev.summary()["cameras"].values())
    later = frames[-1][0] + 2_000
    ev.observe(later, [dataclasses.replace(v, view=dataclasses.replace(v.view, t=later))
                       for v in frames[-1][1] if v.camera_id != "cam3"])
    cams = ev.summary()["cameras"]
    assert cams["cam3"]["person_visible"] is False and cams["cam1"]["person_visible"] is True


def test_views_json_round_trip_keeps_pixels_and_nulls():
    truth = _doc()
    t, views = _walk(truth, seconds=1)[0]
    views = [LocalView(v.camera_id, v.local_track_id,
                       InterpolatedView(v.camera_id, t, (None,) + v.view.keypoints[1:], "ESTIMATED", 12.5, (t - 33, t + 33),
                                        (1.0, 2.0, 3.0, 4.0)))
             for v in views]
    line = json.loads(json.dumps(views_to_json(t, views)))
    t2, back = views_from_json(line)
    assert t2 == t and back == views
    with pytest.raises(ValueError):
        views_from_json({"t": t, "views": [{"c": "cam1"}]})


def test_baseline_file_round_trip_and_version_guard(tmp_path):
    truth = _doc()
    ev, summary = _run(truth, _walk(truth))
    assert summary["status"] == PASSED
    save_baseline(tmp_path, ev.baseline_record())
    record = load_baseline(tmp_path, "cal-check-1")
    assert record is not None and record.verified_camera_ids == ev.baseline_record().verified_camera_ids
    assert load_baseline(tmp_path, "cal-other") is None
    assert load_baseline(None, "cal-check-1") is None
    assert baseline_from_json({"calibration_version": "x", "values_px": {"cam1": -1.0},
                               "source": BASELINE_SOURCE_SETUP_CAMERA_CHECK}) is None
    assert baseline_from_json({**summary["baseline"], "source": "LEARNED_IN_OPERATION"}) is None


def test_config_rejects_bad_values():
    with pytest.raises(ValueError):
        CameraCheckConfig(min_duration_ms=10, max_duration_ms=5)
    with pytest.raises(ValueError):
        CameraCheckConfig(min_pair_pass_rate=0.0)
    assert copy.deepcopy(CFG).thresholds()["min_pair_samples"] == 10


# --------------------------------------------------------------------------- S15P31A101-694 세팅 기준·관제 감시 정합


def test_setup_loo_cap_leaves_relative_margin_under_monitor_ceiling():
    """세팅을 통과한 검증 기준선 b(≤ 세팅 LOO 상한)의 관제 문턱이 b + 상대 여유 아래로 눌리지 않는다.

    15차 세팅 2 처럼 세팅 상한과 관제 절대 상한이 같으면(8px) 기준선 7.5px 카메라는 관제에서 문턱 8px 로 바로 깜빡인다.
    """
    setup, monitor = CameraCheckConfig(), MonitorConfig()
    assert monitor.absolute_max_median_px is not None
    assert setup.max_loo_median_px + monitor.increase_margin_px <= monitor.absolute_max_median_px
    cfg = dataclasses.replace(monitor, window_ms=10_000, min_window_samples=5, sustain_ms=8_000, max_sample_age_ms=2_000)
    doc = _doc()
    ids = tuple(CORNERS)
    for b in (1.0, 7.0, 9.5, setup.max_loo_median_px):
        mon = ReprojectionMonitor(doc, cfg, baseline=BaselineRecord(doc["calibration_version"], {c: b for c in ids}, ids))
        # 기준선 그대로와 기준선 + 여유 직전 값(정상 흔들림)은 감시 안이다
        noisy = b + 0.9 * monitor.increase_margin_px
        reports = [mon.ingest(T0 + k * 500, [(c, b if c != "cam2" else noisy, True) for c in ids]) for k in range(60)]
        last = reports[-1]
        assert not any(r.findings for r in reports), b
        for c in ids:
            cam = last.camera(c)
            assert cam.baseline_verified and cam.status is MonitorStatus.MONITORING, (b, c, cam.status)
            assert cam.threshold_px is not None and cam.threshold_px >= b + monitor.increase_margin_px - 1e-9, (b, cam)


def test_noise_floor_above_old_cap_passes_and_one_degree_rotation_fails():
    """15차 로비 실측처럼 정상 보정의 LOO 잡음 바닥이 옛 상한 8px 를 넘어도 통과하고(기준선이 된다), 1° 틀어진 후보는 실패한다.

    관절 잡음 7.5px(합성)로 정상 보정의 LOO 중앙값은 약 8.3~9.2px, 쌍 잔차는 약 4.4px 다(실측 잡음 바닥: LOO 5.9~8.7px,
    쌍 1.3~3.4px). 같은 걷기에서 cam2 를 1° 돌린 후보는 cam2 LOO 가 약 14px 로 새 상한 12px 를 넘는다.
    """
    truth = _doc()
    walk = _walk(truth, noise_px=7.5)
    _, summary = _run(truth, walk)
    assert summary["status"] == PASSED, summary["problems"]
    looks = {c: v["loo_median_px"] for c, v in summary["cameras"].items()}
    assert max(looks.values()) > 8.0  # 옛 상한이면 정상 보정이 실패했다
    assert set(summary["baseline"]["values_px"]) == set(CORNERS)
    old = CameraCheckEvaluator(truth, dataclasses.replace(CFG, max_loo_median_px=8.0), monitor_config=MON)
    for t, views in walk:
        old.observe(t, views)
        if old.final:
            break
    assert old.summary()["status"] == FAILED
    _, rotated = _run(_doc(rotate={"cam2": 1.0}), walk)
    assert rotated["status"] == FAILED
    assert rotated["cameras"]["cam2"]["verdict"] == FAIL
    assert any(p["reason"] == "REPROJECTION_HIGH" and p["camera_ids"] == ["cam2"] for p in rotated["problems"])


def test_true_calibration_passes_with_diagonals_as_advisory():
    """보정이 맞춘 쌍(옆 쌍)을 주면 대각은 필수가 아니지만 같은 기준으로 재서 싣고, 맞는 보정은 기준선과 함께 통과한다."""
    truth = _doc()
    _, summary = _run(truth, _walk(truth), blocking_pairs=RING_PAIRS)
    assert schema_errors(summary, CAMERA_CHECK_SCHEMA) == []
    assert summary["status"] == PASSED and summary["problems"] == []
    assert summary["pairs"]["cam1-cam3"]["required"] is False and summary["pairs"]["cam1-cam3"]["verdict"] == PASS
    assert all(c["verdict"] == PASS for c in summary["cameras"].values())
    assert set(summary["baseline"]["values_px"]) == set(CORNERS)


def test_rotation_seen_only_on_a_diagonal_is_caught_by_waiting_for_reprojection_samples():
    """한 대가 3° 돌면 옆 쌍 잔차는 5~6px 로 통과하고 대각만 0% 다. 대각을 막지 않는 대신 그 카메라의 한 카메라 제외 표본을
    채울 때까지 끝내지 않아 재투영 실패로 막는다. 대각 실패 자체는 문제로 올리지 않는다."""
    truth = _doc()
    candidate = _doc(rotate={"cam2": 3.0})
    _, summary = _run(candidate, _walk(truth), blocking_pairs=RING_PAIRS)
    assert schema_errors(summary, CAMERA_CHECK_SCHEMA) == []
    assert summary["status"] == FAILED and "baseline" not in summary
    assert summary["pairs"]["cam2-cam4"]["required"] is False and summary["pairs"]["cam2-cam4"]["verdict"] == FAIL
    assert all(p["id"] != "cam2-cam4" for p in summary["problems"])
    assert ("cam2", "REPROJECTION_HIGH") in {(p["id"], p["reason"]) for p in summary["problems"]}


def test_camera_short_of_reprojection_samples_at_max_duration_asks_for_more_walking():
    """대각을 막지 않을 때 끝까지 한 카메라 제외 표본을 못 채운 카메라는 통과로 두지 않고 표본 부족(FEW_SAMPLES)으로 다시 걷게 한다."""
    truth = _doc()
    cfg = dataclasses.replace(CFG, min_loo_samples=10_000)
    ev = CameraCheckEvaluator(truth, cfg, monitor_config=MON, blocking_pairs=RING_PAIRS)
    summary = ev.summary()
    for t, views in _walk(truth, seconds=30):
        ev.observe(t, views)
        summary = ev.summary()
        if summary["status"] in (PASSED, FAILED):
            break
    assert schema_errors(summary, CAMERA_CHECK_SCHEMA) == []
    assert summary["status"] == FAILED and summary["elapsed_s"] >= cfg.max_duration_ms / 1000
    assert {p["reason"] for p in summary["problems"]} == {"FEW_SAMPLES"}
    assert {p["id"] for p in summary["problems"]} == set(CORNERS)


def test_advisory_pairs_count_neither_as_evaluated_nor_as_a_passing_pair():
    """참고 쌍의 실패는 막지 않고, 판정 쌍 수와 카메라별 통과 쌍을 셀 때도 넣지 않는다(옆 쌍으로만 통과를 따진다)."""
    def pair(a, b, verdict):
        return {"camera_ids": [a, b], "verdict": verdict, "samples": 40, "residual_median_px": 3.0,
                "pass_rate": 1.0 if verdict == PASS else 0.2}

    cameras = {c: {"verdict": PASS, "observed_views": 40, "loo_samples": 40, "loo_median_px": 5.0} for c in CORNERS}
    diagonals = frozenset({"cam1-cam3", "cam2-cam4"})
    pairs = {"cam1-cam2": pair("cam1", "cam2", PASS), "cam2-cam3": pair("cam2", "cam3", PASS),
             "cam3-cam4": pair("cam3", "cam4", PASS), "cam1-cam4": pair("cam1", "cam4", PASS),
             "cam1-cam3": pair("cam1", "cam3", FAIL), "cam2-cam4": pair("cam2", "cam4", FAIL)}
    assert {p["id"] for p in _problems(pairs, cameras, CFG)} == diagonals
    assert _problems(pairs, cameras, CFG, advisory=diagonals) == []
    # 옆 쌍이 판정되지 않고 대각만 통과했으면 통과로 치지 않는다
    thin = {**pairs, "cam3-cam4": pair("cam3", "cam4", "INSUFFICIENT"), "cam1-cam4": pair("cam1", "cam4", "INSUFFICIENT"),
            "cam2-cam4": pair("cam2", "cam4", PASS)}
    reasons = {(p["id"], p["reason"]) for p in _problems(thin, cameras, CFG, advisory=diagonals)}
    assert ("cam4", "NO_PASSING_PAIR") in reasons and ("pairs", "FEW_PAIRS") in reasons


def test_failed_calibrated_pair_closes_without_waiting_for_reprojection_samples():
    """막는 쌍(옆 쌍)이 이미 실패했으면 재투영 표본을 기다리지 않고 판정한다(어긋난 보정은 표본이 쌓이지 않아 끝까지 걸게 된다)."""
    truth = _doc()
    candidate = _doc(rotate={"cam2": 10.0})
    cfg = dataclasses.replace(CFG, min_loo_samples=10_000)
    ev = CameraCheckEvaluator(candidate, cfg, monitor_config=MON, blocking_pairs=RING_PAIRS)
    summary = ev.summary()
    for t, views in _walk(truth, seconds=30):
        ev.observe(t, views)
        summary = ev.summary()
        if summary["status"] in (PASSED, FAILED):
            break
    assert summary["status"] == FAILED and summary["elapsed_s"] < cfg.max_duration_ms / 1000
    assert any(p["kind"] == "PAIR" and p["reason"] != "FEW_SAMPLES" for p in summary["problems"])
