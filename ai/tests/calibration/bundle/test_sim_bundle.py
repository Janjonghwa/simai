"""시뮬레이터 4대 카메라 + 걷는 사람에서 교란한 초기값이 수렴하는지, 퇴화는 불확실·실패로 표시하는지 본다.

SIM 입력이므로 지표는 상한이다(is_upper_bound). 허용 오차는 회귀를 잡기 위한 것이고 G1 합격 기준이 아니다.
`-s` 로 돌리면 G1 지표(재투영 오차, 바닥 평면 차이, 카메라 높이 오차, CCVS 검산)를 출력한다.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from simai_ai.calibration import CalibrationVersionStore, Criterion, ScaleBasis
from simai_ai.calibration.bundle import (
    STATUS_CONVERGED,
    STATUS_FAILED,
    STATUS_UNCERTAIN,
    BundleConfig,
    BundleNotUsable,
    MeasuredObjectObservation,
    PersonObservation,
    adjust_human_bundle,
)
from simai_ai.calibration.bundle import adjust as ba_adjust
from simai_ai.calibration.bundle import solver as ba_solver
from simai_ai.calibration.bundle.skeleton import KEYPOINT_NOISE_SCALE
from simai_ai.calibration.criteria import decide_scale
from simai_ai.tracking.system_health import CalibrationState

WALKABLE = ((0.5, 0.5), (8.5, 0.5), (8.5, 7.0), (0.5, 7.0))
# 서로 다른 곳에 세운 실측 정물 2개 (바닥 끝점, 높이)
POLES = {"안전봉": ((2.5, 4.2, 0.0), 1.20), "라바콘": ((6.6, 2.8, 0.0), 0.70)}


def _pitch_deg(R) -> float:
    """광축이 바닥 평면과 이루는 각(벽 방향 게이지와 무관)."""
    return math.degrees(math.asin(float(np.asarray(R)[2, 2])))


def _metrics(case, result) -> dict:
    cams = {}
    for cam in result.cameras:
        doc = cam.to_contract()
        gt = case.truth_cameras[cam.camera_id]
        C = -np.array(doc["R"]).T @ np.array(doc["t"])
        cams[cam.camera_id] = {
            "reprojection_median_px": doc["reprojection_error_px"]["median"],
            "reprojection_p95_px": doc["reprojection_error_px"]["p95"],
            "height_error_m": round(float(C[2] - case.truth_center(cam.camera_id)[2]), 4),
            "pitch_error_deg": round(_pitch_deg(doc["R"]) - _pitch_deg(gt["R"]), 4),
            "focal_error_rel": round(doc["K"][0][0] / gt["K"][0][0] - 1, 5),
            "k1_error": round(doc["distortion"]["coeffs"][0] - gt["distortion"]["coeffs"][0], 5),
        }
    return {
        "status": result.status,
        "flags": list(result.flags),
        "notes": list(result.notes),
        "provider_source": result.provider_source,
        "cameras": cams,
        "floor_plane_disagreement_m": result.floor_plane_disagreement_m,
        "floor_coverage_ratio": result.floor_coverage_ratio,
        "height_check": [hc.to_record() for hc in result.camera_height_checks],
        "rounds": result.record["rounds"],
    }


def _with_outliers(observations, rate, seed=11):
    """keypoint 의 rate 비율을 40~80 px 옮긴 관측(검출 실수 흉내)."""
    rng = np.random.default_rng(seed)
    out = []
    for obs in observations:
        views = {}
        for cid, kps in obs.views.items():
            moved = []
            for kp in kps:
                if kp is not None and kp[2] > 0 and rng.random() < rate:
                    d = rng.uniform(40, 80) * np.array([math.cos(a := rng.uniform(0, 2 * math.pi)), math.sin(a)])
                    kp = (kp[0] + d[0], kp[1] + d[1], kp[2])
                moved.append(kp)
            views[cid] = tuple(moved)
        out.append(PersonObservation(obs.person_id, obs.t, views))
    return out


def _subset(observations, camera_ids=None, until_ms=None):
    out = []
    t0 = min(o.t for o in observations)
    for obs in observations:
        if until_ms is not None and obs.t - t0 > until_ms:
            continue
        views = {cid: kps for cid, kps in obs.views.items() if camera_ids is None or cid in camera_ids}
        if views:
            out.append(PersonObservation(obs.person_id, obs.t, views))
    return out


@pytest.fixture(scope="module")
def loop_result(loop_case, ba):
    intr, extr = loop_case.initial(seed=7)
    observations = _with_outliers(loop_case.observations, rate=0.01)
    result = adjust_human_bundle(
        observations,
        intr,
        extr,
        input_provenance=loop_case.input_provenance,
        created_at=ba.T0,
        config=BundleConfig(walkable_area_xy=WALKABLE),
    )
    print(
        "SIM 상한 지표(번들 조정, 교란 초기값):", json.dumps(_metrics(loop_case, result), ensure_ascii=False, indent=1)
    )
    return result


def test_perturbed_initial_converges(loop_case, loop_result):
    result = loop_result
    m = _metrics(loop_case, result)
    assert result.status == STATUS_CONVERGED, result.flags
    assert result.provider_source == "SIM"  # 시뮬레이터 입력은 G1_AUTO 가 아니다(상한)
    assert result.record["is_upper_bound"] is True
    for cid, cam in m["cameras"].items():
        assert cam["reprojection_median_px"] <= 2.0, cid
        assert cam["reprojection_p95_px"] <= 5.0, cid
        assert abs(cam["focal_error_rel"]) < 0.01, cid  # 초기값 +4%
        assert abs(cam["k1_error"]) < 0.01, cid  # 초기값 +0.03
        assert abs(cam["pitch_error_deg"]) < 0.2, cid  # 초기값 2° 교란
        # 스케일은 교란한 초기값을 이어받으므로(정물 없음) 높이에 그 몫이 남는다
        assert abs(cam["height_error_m"]) < 0.05, cid
    assert result.floor_plane_disagreement_m is not None and result.floor_plane_disagreement_m < 0.03
    assert result.floor_coverage_ratio is not None
    for hc in result.camera_height_checks:
        assert hc.difference_m is not None and hc.difference_m < 0.05, hc
    # 1% 이상치 관측은 2차 라운드 전에 빠진다
    rounds = result.record["rounds"]
    assert len(rounds) == 2 and rounds[1]["observations"] < rounds[0]["observations"]
    assert result.record["outliers"]["removed_ratio"] <= 0.10


def test_scale_not_determined_without_measured_objects(loop_result):
    assert loop_result.scale_determined is False
    assert loop_result.scale_estimates == ()
    assert "SCALE_NOT_DETERMINED" in loop_result.notes
    assert loop_result.record["scale"]["source"] == "INITIAL_EXTRINSICS"


def test_propose_uses_criteria_and_sim_stays_provisional(loop_result, ba):
    store = CalibrationVersionStore(camera_ids=("cam1", "cam2", "cam3", "cam4"))
    proposal = loop_result.propose_to(store, at_ms=ba.T0)
    assert proposal.adopted, proposal.violations
    assert store.state is CalibrationState.PROVISIONAL
    failed = proposal.judgement.failed_criteria
    assert Criterion.SOURCE in failed  # SIM 출처는 값과 관계없이 CALIBRATED 불가
    assert Criterion.SCALE_PRIMARY in failed  # 스케일 근거 없음(모르면 비움)
    assert Criterion.CHECKPOINT_COUNT in failed  # 독립 검산점은 이 조정 밖에서 받는다
    # 재투영·바닥 평면 기준은 SIM 상한에서 통과한다
    assert not failed & {Criterion.REPROJECTION_MEDIAN, Criterion.REPROJECTION_P95, Criterion.FLOOR_PLANE}
    doc = proposal.doc
    assert doc["provider_source"] == "SIM" and doc["scale_basis"] == "MODEL_ESTIMATE"
    assert doc["validation"]["floor_plane_disagreement_m"] == loop_result.floor_plane_disagreement_m


def _project_gt(case, camera_id, X):
    gt = case.truth_cameras[camera_id]
    R = np.array(gt["R"])
    cam = ba_solver.CameraSolution(
        camera_id,
        tuple(gt["image_size"]),
        R,
        -R.T @ np.array(gt["t"]),
        gt["K"][0][0],
        (gt["K"][0][2], gt["K"][1][2]),
        tuple(gt["distortion"]["coeffs"][:2]),
    )
    return cam.project(np.asarray(X, dtype=float))


def _poles(case, rng, listed_lengths=None):
    """정물 끝점의 카메라별 관측. listed_lengths 로 정물 목록 치수를 실제와 다르게 줄 수 있다(치수 오기)."""
    objects = []
    for label, (bottom, height) in POLES.items():
        top = (bottom[0], bottom[1], bottom[2] + height)
        endpoints = {}
        for cid in case.truth_cameras:
            uv = _project_gt(case, cid, [top, bottom]) + rng.normal(scale=0.5, size=(2, 2))
            endpoints[cid] = (tuple(uv[0]), tuple(uv[1]))
        objects.append(MeasuredObjectObservation(label, (listed_lengths or {}).get(label, height), endpoints))
    return objects


def _scaled(extr, factor, about=(4.5, 3.75, 0.0)):
    """초기 외부 파라미터의 장면 스케일을 factor 배로 틀리게 한다(MapAnything 메트릭 오차 흉내)."""
    out = {}
    for cid, (R, t) in extr.items():
        R = np.array(R)
        C = -R.T @ np.array(t)
        C2 = np.array(about) + factor * (C - np.array(about))
        out[cid] = (R.tolist(), (-R @ C2).tolist())
    return out


def test_measured_objects_fix_scale(loop_case, ba):
    """실측 정물 제약(-100 자리): 초기 스케일이 5% 틀려도 정물 치수로 스케일을 정한다. 정물이 없으면 틀린 채 남는다."""
    intr, extr = loop_case.initial(seed=3)
    extr = _scaled(extr, 1.05)
    observations = _subset(loop_case.observations, until_ms=12000)
    kwargs = {"input_provenance": loop_case.input_provenance, "created_at": ba.T0}

    without = adjust_human_bundle(observations, intr, extr, **kwargs)
    with_objects = adjust_human_bundle(
        observations, intr, extr, measured_objects=_poles(loop_case, np.random.default_rng(1)), **kwargs
    )
    err_without = _metrics(loop_case, without)["cameras"]
    err_with = _metrics(loop_case, with_objects)["cameras"]
    print(
        "SIM 상한 카메라 높이 오차(초기 스케일 +5%):",
        json.dumps(
            {cid: [err_without[cid]["height_error_m"], err_with[cid]["height_error_m"]] for cid in err_with},
            ensure_ascii=False,
        ),
    )
    assert with_objects.status == STATUS_CONVERGED, with_objects.flags
    assert with_objects.scale_determined is True
    assert {e.label for e in with_objects.scale_estimates} == set(POLES)
    assert all(abs(e.scale - 1.0) < 0.01 for e in with_objects.scale_estimates)
    decision, failures = decide_scale(with_objects.scale_estimates)
    assert decision.primary is ScaleBasis.MEASURED_OBJECT, failures  # 치수가 맞으면 정물끼리 일치한다
    for cid in err_with:
        assert abs(err_with[cid]["height_error_m"]) < 0.03, cid
        assert abs(err_without[cid]["height_error_m"]) > 0.08, cid  # 스케일 근거가 없으면 초기 스케일 오차가 남는다


def test_straight_line_walk_is_uncertain(straight_case, ba):
    """한 사람이 직선으로만 걸으면 접지점이 한 줄이라 바닥 기울기가 한 축으로 정해지지 않는다."""
    intr, extr = straight_case.initial(seed=5)
    result = adjust_human_bundle(
        straight_case.observations, intr, extr, input_provenance=straight_case.input_provenance, created_at=ba.T0
    )
    assert result.status == STATUS_UNCERTAIN
    assert "FLOOR_CONTACTS_COLLINEAR" in result.flags
    with pytest.raises(BundleNotUsable):
        result.propose_to(CalibrationVersionStore(), at_ms=ba.T0)


def test_two_cameras_are_uncertain(loop_case, ba):
    intr, extr = loop_case.initial(seed=7, camera_ids=("cam1", "cam2"))
    observations = _subset(loop_case.observations, camera_ids={"cam1", "cam2"}, until_ms=9000)
    result = adjust_human_bundle(
        observations, intr, extr, input_provenance=loop_case.input_provenance, created_at=ba.T0
    )
    assert result.status == STATUS_UNCERTAIN
    assert "TOO_FEW_CAMERAS" in result.flags
    assert [c.camera_id for c in result.cameras] == ["cam1", "cam2"]


def test_one_camera_fails_without_calibration(loop_case, ba):
    intr, extr = loop_case.initial(seed=7)
    observations = _subset(loop_case.observations, camera_ids={"cam3"}, until_ms=3000)
    result = adjust_human_bundle(
        observations, intr, extr, input_provenance=loop_case.input_provenance, created_at=ba.T0
    )
    assert result.status == STATUS_FAILED
    assert result.cameras == ()  # 모르면 비운다: 보정 값을 만들지 않는다
    assert result.record["status"] == STATUS_FAILED
    with pytest.raises(BundleNotUsable):
        result.propose_to(CalibrationVersionStore(), at_ms=ba.T0)


def test_invalid_input_provenance_is_rejected(loop_case, ba):
    intr, extr = loop_case.initial(seed=7)
    provenance = dict(loop_case.input_provenance)
    provenance.pop("is_upper_bound")
    with pytest.raises(ValueError, match="input_provenance"):
        adjust_human_bundle(loop_case.observations[:5], intr, extr, input_provenance=provenance, created_at=ba.T0)


def test_joint_seen_by_one_camera_is_not_a_variable(loop_case):
    """2대 미만에서 보인 관절은 3D 변수로 만들지 않는다(지어내지 않는다)."""
    intr, extr = loop_case.initial(seed=7)
    obs = loop_case.observations[0]
    keep_cam = min(obs.views)
    nose_only_once = {
        cid: tuple(None if (j == 0 and cid != keep_cam) else kp for j, kp in enumerate(kps))
        for cid, kps in obs.views.items()
    }
    observations = [PersonObservation(obs.person_id, obs.t, nose_only_once)]
    cams = tuple(ba_adjust._camera_init(intr, extr, cid) for cid in sorted(extr))
    rigs = {
        c.camera_id: ba_adjust._rig(c.camera_id, c.image_size, c.R0, c.C0, c.f0, c.principal_point, c.k0) for c in cams
    }
    built = ba_adjust._build_problem(observations, (), cams, rigs, BundleConfig())
    assert ("person", 0, 0) not in built.point_keys
    assert ("person", 0, 5) in built.point_keys  # 왼쪽 어깨는 여러 대에서 보인다


def test_ankles_only_layout_uses_ankle_contacts(loop_case, ba):
    """발 끝점이 없으면(COCO17) 발목으로 바닥을 잡고, 발목 높이 가정을 썼다고 기록한다."""
    intr, extr = loop_case.initial(seed=7)
    observations = [
        PersonObservation(o.person_id, o.t, {cid: tuple(kps[:17]) for cid, kps in o.views.items()})
        for o in _subset(loop_case.observations, until_ms=8000)
    ]
    result = adjust_human_bundle(
        observations, intr, extr, input_provenance=loop_case.input_provenance, created_at=ba.T0
    )
    assert "FLOOR_FROM_ANKLES" in result.notes
    assert result.record["floor"]["reference_keypoint_counts"]["TOE"] == 0
    assert result.floor_plane_disagreement_m is not None and result.floor_plane_disagreement_m < 0.03


def test_measured_object_listing_error_is_not_absorbed(loop_case, ba):
    """정물 목록 치수를 6% 틀리게 적으면 정물끼리 일치하지 않아 주 근거가 되지 않는다.

    배율은 치수 제약이 들어간 끝점이 아니라 최종 카메라로 다시 삼각측량한 길이로 재므로, 조정이 오기를 흡수하지 못한다.
    """
    intr, extr = loop_case.initial(seed=3)
    observations = _subset(loop_case.observations, until_ms=12000)
    wrong = {"라바콘": POLES["라바콘"][1] * 1.06}
    result = adjust_human_bundle(
        observations,
        intr,
        extr,
        measured_objects=_poles(loop_case, np.random.default_rng(1), listed_lengths=wrong),
        input_provenance=loop_case.input_provenance,
        created_at=ba.T0,
    )
    scales = {e.label: e.scale for e in result.scale_estimates}
    print("SIM 상한 정물 배율(라바콘 치수 +6% 오기):", json.dumps(scales, ensure_ascii=False))
    assert set(scales) == set(POLES)
    decision, failures = decide_scale(result.scale_estimates)
    assert decision.primary is None
    assert any(f.criterion is Criterion.SCALE_PRIMARY for f in failures)


def test_too_many_outliers_is_uncertain(loop_case, ba):
    """관측 25% 가 이상치면 제거 비율이 상한(10%)을 넘어 불확실로 표시하고 후보로 넣지 않는다."""
    intr, extr = loop_case.initial(seed=7)
    observations = _with_outliers(_subset(loop_case.observations, until_ms=9000), rate=0.25)
    result = adjust_human_bundle(
        observations, intr, extr, input_provenance=loop_case.input_provenance, created_at=ba.T0
    )
    outliers = result.record["outliers"]
    print("이상치 25% 입력:", json.dumps(outliers, ensure_ascii=False))
    assert result.status == STATUS_UNCERTAIN
    assert "TOO_MANY_OUTLIERS" in result.flags
    assert outliers["removed_ratio"] > 0.10
    assert outliers["inlier_observations"] + outliers["removed_observations"] == outliers["initial_observations"]
    for cam in result.record["cameras"]:
        before = cam["reprojection_error_before_outlier_removal_px"]
        assert before["p95"] > cam["reprojection_error_px"]["p95"]  # 제거 전 기준도 함께 남는다
    with pytest.raises(BundleNotUsable):
        result.propose_to(CalibrationVersionStore(), at_ms=ba.T0)


def _with_joint_noise(observations, base_px, seed=13):
    """관절별 잡음(σ = base_px × COCO κ 배율): 실제 YOLO pose 처럼 엉덩이·무릎이 눈보다 4배 남짓 흔들린다(-313 로비)."""
    rng = np.random.default_rng(seed)
    out = []
    for obs in observations:
        views = {}
        for cid, kps in obs.views.items():
            moved = []
            for j, kp in enumerate(kps):
                if kp is not None and kp[2] > 0:
                    d = rng.normal(scale=base_px * KEYPOINT_NOISE_SCALE[j], size=2)
                    kp = (kp[0] + d[0], kp[1] + d[1], kp[2])
                moved.append(kp)
            views[cid] = moved
        out.append(PersonObservation(obs.person_id, obs.t, views))
    return out


def test_joint_noise_scaling_keeps_noisy_hips_as_inliers(loop_case, ba):
    """관절별 잡음이 다르면(엉덩이 σ 약 6 px) 같은 8 px 기준은 정상 관측을 대량으로 이상치로 버려 불확실이 된다(-313 로비 원인 2).
    COCO κ 배율로 잡음·기준을 관절마다 넓히면 수렴하고, 40~80 px 검출 실수는 여전히 이상치로 빠진다."""
    intr, extr = loop_case.initial(seed=7)
    noisy = _with_joint_noise(_subset(loop_case.observations, until_ms=12000), base_px=1.5)
    noisy = _with_outliers(noisy, rate=0.02)
    kwargs = dict(input_provenance=loop_case.input_provenance, created_at=ba.T0)
    uniform = adjust_human_bundle(noisy, intr, extr, config=BundleConfig(joint_noise_scaling=False), **kwargs)
    scaled = adjust_human_bundle(noisy, intr, extr, config=BundleConfig(), **kwargs)
    print("관절별 잡음:", json.dumps({"uniform": uniform.record["outliers"], "scaled": scaled.record["outliers"]}))
    assert uniform.status == STATUS_UNCERTAIN and "TOO_MANY_OUTLIERS" in uniform.flags
    assert scaled.status == STATUS_CONVERGED, scaled.flags
    assert 0.01 <= scaled.record["outliers"]["removed_ratio"] <= 0.06  # 주입한 검출 실수(2%)만큼은 빠진다
    assert scaled.record["outliers"]["joint_noise_scaling"] is True
    for cam in scaled.cameras:
        gt = loop_case.truth_cameras[cam.camera_id]
        C = -np.array(cam.R).T @ np.array(cam.t)
        assert abs(C[2] - loop_case.truth_center(cam.camera_id)[2]) < 0.06, cam.camera_id
        assert abs(_pitch_deg(cam.R) - _pitch_deg(gt["R"])) < 0.3, cam.camera_id
