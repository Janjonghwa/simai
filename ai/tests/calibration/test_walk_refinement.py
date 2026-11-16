"""세팅 카메라 확인 걷기 다듬기 시험(S15P31A101-760).

합성 카메라 4대(test_camera_check 의 리그·투영 헬퍼)에 한 사람이 방을 두 바퀴 걷는 view 를 투영한다. 보정 후보를 틀어 두고
다듬은 뒤 Holdout 으로만 카메라 확인과 같은 판정을 해서 채택·거절을 본다.
"""

from __future__ import annotations

import copy
import functools
import math

import numpy as np
import pytest

from simai_ai.calibration.camera_check import DEFAULT_CONFIG, PASS, PASSED, evaluate_views, pair_key
from simai_ai.calibration.document import content_hash
from simai_ai.calibration.walk_refinement import (
    CENTER_SHIFT_XY,
    FEW_FIT_POINTS,
    WalkRefinementConfig,
    refine_extrinsics_from_walk,
    split_walk_records,
)
from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.providers.contract import schema_errors
from simai_ai.tracking.cross_view import LocalView
from tests.calibration.test_camera_check import CAMERA_CHECK_SCHEMA, CORNERS, OFFSETS, T0, _doc, _project

HEIGHTS = {cid: pos[2] for cid, pos in CORNERS.items()}
DIAGONALS = ("cam1-cam3", "cam2-cam4")


def _walk(truth, *, seconds=36.0, hz=5.0, center=(4.5, 3.75), radii=(3.0, 2.4), laps=2, shift_ms=None, seed=7,
          noise_px=2.0):
    """한 사람이 타원을 laps 바퀴 걷는다(약 0.9 m/s). shift_ms 의 카메라는 그만큼 늦은 자세를 같은 시각에 보낸다(시각 밀림)."""
    rng = np.random.default_rng(seed)
    shift_ms = shift_ms or {}
    ticks = int(seconds * hz)
    out = []
    for k in range(ticks):
        t = T0 + int(k * 1000 / hz)
        views = []
        for cid in CORNERS:
            a = 2 * math.pi * laps * (k - shift_ms.get(cid, 0) * hz / 1000) / ticks
            x, y = center[0] + radii[0] * math.cos(a), center[1] + radii[1] * math.sin(a)
            joints = [np.array([x, y - dx, dz]) for dx, dz in OFFSETS]
            kps = _project(truth, cid, joints, rng, noise_px)
            views.append(LocalView(cid, f"{cid}-p0", InterpolatedView(cid, t, kps, "SIM", 0.0, (t,))))
        out.append((t, views))
    return out


@functools.cache
def _room_walk():
    return tuple(_walk(_doc()))


def _shifted_center(doc, cid, shift_m, wall_m=8.0):
    """cid 의 중심을 카메라 오른쪽(수평)으로 shift_m 옮기고, wall_m 앞 벽 점이 같은 픽셀에 오도록 회전을 맞춘 후보.

    배경 BA(Stage A)가 중심을 고정한 채 먼 벽 특징에 회전을 맞춘 상황(설계 문서 원인 B): 벽에서는 잔차가 없고 방 가운데 사람에서
    시차가 남는다."""
    out = copy.deepcopy(doc)
    cam = next(c for c in out["cameras"] if c["camera_id"] == cid)
    R, K = np.asarray(cam["R"]), np.asarray(cam["K"])
    C = -R.T @ np.asarray(cam["t"])
    right = np.array([R[0, 0], R[0, 1], 0.0])
    C2 = C + shift_m * right / np.linalg.norm(right)
    rays = [R.T @ (np.linalg.inv(K) @ [u, v, 1.0]) for u in np.linspace(200, 2100, 7) for v in np.linspace(150, 1150, 5)]
    W = np.array([C + wall_m * r / np.linalg.norm(r) for r in rays])
    A = (W - C2) / np.linalg.norm(W - C2, axis=1, keepdims=True)
    B = (W - C) @ R.T
    B /= np.linalg.norm(B, axis=1, keepdims=True)
    U, _, Vt = np.linalg.svd(B.T @ A)
    R2 = U @ np.diag([1.0, 1.0, np.sign(np.linalg.det(U @ Vt))]) @ Vt
    cam["R"], cam["t"] = R2.tolist(), (-R2 @ C2).tolist()
    return out


def _rotation_error_deg(doc, truth):
    out = {}
    for cam in doc["cameras"]:
        ref = next(c for c in truth["cameras"] if c["camera_id"] == cam["camera_id"])
        Rd = np.asarray(cam["R"]) @ np.asarray(ref["R"]).T
        out[cam["camera_id"]] = math.degrees(math.acos(min(1.0, (np.trace(Rd) - 1) / 2)))
    return out


def _assert_adopted(result, base_version):
    assert result.adopted and result.reasons == ()
    doc, summary = result.calibration_doc, result.summary
    version = doc["calibration_version"]
    assert version != base_version and version.startswith(base_version + "-walk-")
    assert doc["content_hash"] == content_hash(doc)
    assert summary["status"] == PASSED and summary["calibration_version"] == version
    assert schema_errors(summary, CAMERA_CHECK_SCHEMA) == []
    assert all(p["verdict"] == PASS for p in summary["pairs"].values()) and len(summary["pairs"]) == 6
    assert all(c["verdict"] == PASS for c in summary["cameras"].values())
    assert summary["baseline"]["calibration_version"] == version == result.baseline.calibration_version
    assert set(result.baseline.values_px) == set(CORNERS)
    # Holdout 표본 하한(설정)과 쌍 기준(통과율·잔차)은 카메라 확인 기준 그대로다
    assert summary["thresholds"]["min_pair_samples"] == 15 and summary["thresholds"]["max_pair_residual_median_px"] == 8.0
    for cam in doc["cameras"]:
        assert cam["reprojection_error_px"]["median"] == summary["cameras"][cam["camera_id"]]["loo_median_px"]
    return doc, summary


def test_split_alternates_fit_and_holdout_blocks_with_discarded_gaps():
    walk = list(_room_walk())
    empty = (T0 - 1_000, [])  # 사람이 보이기 전 시각은 기준(0)이 아니다
    fit, holdout = split_walk_records([*walk[::-1], empty], WalkRefinementConfig())
    assert fit and holdout and {t for t, _ in fit}.isdisjoint({t for t, _ in holdout})
    phases_fit = {(t - T0) % 5_000 for t, _ in fit}
    phases_hold = {(t - T0) % 5_000 for t, _ in holdout}
    assert max(phases_fit) < 2_400 and min(phases_hold) >= 3_000 and max(phases_hold) < 4_400
    gap = min(abs(a - b) for a, _ in fit for b, _ in holdout)
    assert gap >= 600  # 완충 0.6 s 보다 가까운 Fit·Holdout 시각이 없다
    assert [t for t, _ in fit] == sorted(t for t, _ in fit)
    assert 0.60 < len(fit) / (len(fit) + len(holdout)) < 0.66  # Fit 63% : Holdout 37%


def test_three_degree_rotation_is_recovered_and_six_pairs_pass_on_holdout():
    truth = _doc()
    candidate = _doc(rotate={"cam2": 3.0})
    walk = list(_room_walk())
    before = evaluate_views(walk, candidate).summary
    assert before["status"] == "FAILED"
    assert before["pairs"]["cam2-cam4"]["pass_rate"] < 0.75  # 대각이 실패한다
    original = copy.deepcopy(candidate)
    result = refine_extrinsics_from_walk(candidate, walk, camera_heights_m=HEIGHTS)
    assert candidate == original  # 입력 문서를 덮어쓰지 않는다
    doc, summary = _assert_adopted(result, "cal-check-1")
    errors = _rotation_error_deg(doc, truth)
    assert max(errors.values()) < 0.2, errors
    assert result.record["cameras"]["cam2"]["rotation_change_deg"] == pytest.approx(3.0, abs=0.2)
    assert max(v["center_shift_xy_m"] for v in result.record["cameras"].values()) < 0.02
    assert max(p["residual_median_px"] for p in summary["pairs"].values()) < 3.0
    # 게이지: Stage A 카메라 중심의 평균 높이를 지킨다(바닥 Z=0)
    heights = [c["camera_height_m"] for c in doc["cameras"]]
    assert np.mean(heights) == pytest.approx(np.mean(list(HEIGHTS.values())), abs=1e-3)


def test_center_shift_compensated_at_the_wall_needs_six_dof_to_fix_the_diagonal():
    """cam3 중심이 수평 20 cm 틀리고 8 m 벽에 맞춘 역회전으로 보상된 후보: 옆 쌍은 대체로 통과하고 대각 cam1–cam3 과 cam3 재투영이
    실패한다. 6 자유도 다듬기로 6쌍이 통과하고, 같은 걷기를 중심을 묶은(회전만) 다듬기보다 잔차가 작다."""
    truth = _doc()
    candidate = _shifted_center(truth, "cam3", 0.20)
    walk = _walk(truth, center=(5.8, 4.8), radii=(2.2, 1.8))  # 방 가운데·cam3 가까이(3~6 m)를 걷는다
    before = evaluate_views(walk, candidate).summary
    assert before["status"] == "FAILED"
    assert before["pairs"]["cam1-cam3"]["verdict"] == "FAIL"
    assert sum(before["pairs"][pair_key(a, b)]["verdict"] == PASS
               for a, b in (("cam1", "cam2"), ("cam2", "cam3"), ("cam3", "cam4"), ("cam1", "cam4"))) >= 3
    result = refine_extrinsics_from_walk(candidate, walk, camera_heights_m=HEIGHTS)
    _, summary = _assert_adopted(result, "cal-check-1")
    assert max(v["center_shift_xy_m"] for v in result.record["cameras"].values()) <= 0.20
    rotation_only = refine_extrinsics_from_walk(
        candidate, walk, camera_heights_m=HEIGHTS,
        config=WalkRefinementConfig(center_sigma_xy_m=1e-4, center_sigma_z_m=1e-4))
    after_3dof = rotation_only.record["after"]["pairs"]
    assert max(p["residual_median_px"] for p in summary["pairs"].values()) < max(
        p["residual_median_px"] for p in after_3dof.values())
    assert summary["cameras"]["cam3"]["loo_median_px"] < rotation_only.record["after"]["cameras"]["cam3"]["loo_median_px"]


def test_one_far_off_camera_is_bootstrapped_from_the_other_three():
    """한 대가 10° 틀어져 그 카메라가 낀 쌍이 모두 0~30% 이면 나머지 세 대 삼각측량으로 회전을 먼저 맞춘다(8° 상한 대신 15°)."""
    truth = _doc()
    result = refine_extrinsics_from_walk(_doc(rotate={"cam2": 10.0}), list(_room_walk()), camera_heights_m=HEIGHTS)
    assert result.record["bootstrap"]["camera_id"] == "cam2" and result.record["bootstrap"]["applied"] is True
    doc, _ = _assert_adopted(result, "cal-check-1")
    assert result.record["cameras"]["cam2"]["rotation_cap_deg"] == 15.0
    assert max(_rotation_error_deg(doc, truth).values()) < 0.2


@pytest.mark.parametrize("case", ["focal_plus_25pct", "time_shift_500ms"])
def test_errors_that_extrinsics_cannot_explain_are_not_adopted(case):
    """한 대 초점 +25%, 한 대 시각 500 ms 밀림: Fit 잔차는 줄여도 카메라를 물리 상한 넘게 옮겨야 해서 채택하지 않는다."""
    truth = _doc()
    if case == "focal_plus_25pct":
        candidate = copy.deepcopy(truth)
        cam = next(c for c in candidate["cameras"] if c["camera_id"] == "cam2")
        cam["K"][0][0] *= 1.25
        cam["K"][1][1] *= 1.25
        walk = list(_room_walk())
    else:
        candidate = truth
        walk = _walk(truth, shift_ms={"cam2": 500})
    assert evaluate_views(walk, candidate).summary["status"] == "FAILED"
    result = refine_extrinsics_from_walk(candidate, walk, camera_heights_m=HEIGHTS)
    assert not result.adopted and result.calibration_doc is None and result.summary is None and result.baseline is None
    assert CENTER_SHIFT_XY in result.reasons
    assert result.record["adopted"] is False and result.record["reasons"] == list(result.reasons)


def test_too_few_samples_are_not_refined():
    result = refine_extrinsics_from_walk(_doc(rotate={"cam2": 3.0}), list(_room_walk())[:8], camera_heights_m=HEIGHTS)
    assert not result.adopted and result.reasons == (FEW_FIT_POINTS,) and result.calibration_doc is None
    assert "solver" not in result.record


def test_height_far_from_the_input_height_is_rejected():
    """다듬은 카메라 높이가 입력(관리자 확인) 높이에서 10 cm 넘게 벗어나면 채택하지 않는다."""
    wrong = {**HEIGHTS, "cam1": HEIGHTS["cam1"] + 0.3}
    result = refine_extrinsics_from_walk(_doc(rotate={"cam2": 3.0}), list(_room_walk()), camera_heights_m=wrong)
    assert not result.adopted and "HEIGHT_DIFF" in result.reasons


def test_config_rejects_bad_values():
    with pytest.raises(ValueError):
        WalkRefinementConfig(fit_block_ms=0)
    with pytest.raises(ValueError):
        WalkRefinementConfig(stage1_loss="l2")
    with pytest.raises(ValueError):
        WalkRefinementConfig(center_sigma_xy_m=-1.0)
    cfg = WalkRefinementConfig()
    assert cfg.period_ms == 5_000
    hold = cfg.holdout_check_config(DEFAULT_CONFIG)
    assert hold.min_pair_samples == 15 and hold.min_loo_samples == 20 and hold.min_duration_ms == 20_000
    assert hold.min_four_view_ticks == 0  # Holdout 평가는 걷기 대기를 하지 않는다
    assert hold.min_pair_pass_rate == DEFAULT_CONFIG.min_pair_pass_rate
    assert hold.max_loo_median_px == DEFAULT_CONFIG.max_loo_median_px
