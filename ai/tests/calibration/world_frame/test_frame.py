"""World 고정(-102): 합성 교실(SIM, 상한)에서 바닥·축·원점·스케일 정확도와 비우는 경로, 스케일 우선순위, 사람 마스크."""

from __future__ import annotations

import numpy as np
import pytest

from simai_ai.calibration.criteria import ScaleBasis, ScaleEstimate
from simai_ai.calibration.world_frame.frame import (
    AXES_AMBIGUOUS,
    FLOOR_FOOT_MISMATCH,
    FLOOR_NOT_FOUND,
    STATUS_FAILED,
    STATUS_OK,
    Wall,
    WorldFrameConfig,
    choose_axes,
    choose_scale,
    fix_world_frame,
)
from simai_ai.calibration.world_frame.inputs import cross_view_reprojection, load_mapanything_run, mask_to_model_res

from .conftest import CAMS, PERSON, ROOM, make_run

L, W, H = ROOM
CORNERS = [(0, 0), (L, 0), (L, W), (0, W)]


def _fix(run, **kw):
    r = load_mapanything_run(run.run_dir, prepared=kw.pop("prepared", None))
    return r, fix_world_frame(r.all_points(), r.cam2world(), **kw)


def _composite(res, run):
    """정답 World → 추정 World (4×4)."""
    return res.matrix() @ run.world_to_ma


def test_sim_room_fixes_floor_axes_corner_and_model_scale(sim_run):
    _, res = _fix(sim_run)
    assert res.status == STATUS_OK, res.detail
    C = _composite(res, sim_run)
    s = 1.04  # MODEL_ESTIMATE 는 MapAnything 길이를 그대로(정답 대비 1.04 배)
    A = C[:3, :3] / s
    assert np.allclose(A @ A.T, np.eye(3), atol=2e-3)
    assert np.linalg.det(A) > 0  # 오른손
    assert abs(A[2, 2] - 1) < 1e-3  # +Z = 상방
    # 원점 = 방 모서리 하나의 바닥, 방은 +X·+Y 쪽
    corner_true = np.linalg.solve(C[:3, :3], -C[:3, 3])
    assert min(np.hypot(corner_true[0] - cx, corner_true[1] - cy) for cx, cy in CORNERS) < 0.03
    assert abs(corner_true[2]) < 0.01
    for cx, cy in CORNERS:
        x, y, _ = C[:3, :3] @ [cx, cy, 0] + C[:3, 3]
        assert x > -0.03 and y > -0.03
    # 카메라 높이 = 정답 × 1.04 (스케일 근거가 MODEL_ESTIMATE)
    heights = res.evidence["room"]["camera_heights_m"]
    for cid, c in CAMS.items():
        assert heights[cid] == pytest.approx(c[2] * s, abs=0.01)
    assert res.scale_basis == "MODEL_ESTIMATE"
    assert res.evidence["floor"]["foot_check"]["status"] == "NOT_CHECKED"


def test_measured_object_scale_restores_metric(sim_run):
    _, res = _fix(sim_run, scale_estimates=[
        ScaleEstimate(ScaleBasis.MEASURED_OBJECT, 1 / 1.04, label="cone-1"),
        ScaleEstimate(ScaleBasis.MEASURED_OBJECT, 1 / 1.04 * 1.005, label="pole-1"),
    ])
    assert res.status == STATUS_OK and res.scale_basis == "MEASURED_OBJECT"
    C = _composite(res, sim_run)
    assert np.allclose(C[:3, :3] @ C[:3, :3].T, np.eye(3), atol=1e-2)
    for cid, c in CAMS.items():
        assert res.evidence["room"]["camera_heights_m"][cid] == pytest.approx(c[2], abs=0.02)
    assert "MODEL_ESTIMATE" in res.scale_cross_checks  # 1.0 은 0.96 과 5% 안


def test_cross_view_reprojection_is_small_on_exact_scene(sim_run):
    run = load_mapanything_run(sim_run.run_dir)
    rep = cross_view_reprojection(run, original_width={c: 896 for c in CAMS})
    for cid in CAMS:
        assert rep["cameras"][cid]["median_model_px"] < 1.0
        assert rep["cameras"][cid]["median_px"] == pytest.approx(rep["cameras"][cid]["median_model_px"] * 4, rel=1e-3)


# ----- 모르면 비운다


def test_no_floor_points_leaves_world_empty(tmp_path):
    run = make_run(tmp_path, drop_labels=(0,))  # 바닥이 안 보임(책상 윗면은 보임)
    _, res = _fix(run)
    assert res.status == STATUS_FAILED and res.reason == FLOOR_NOT_FOUND
    assert res.rotation is None and res.origin is None and res.scale is None
    with pytest.raises(ValueError):
        res.matrix()
    # 반례: 책상 윗면(0.75 m)은 수평이고 그 위에 아무것도 없지만 아래로 벽 점이 이어져 바닥으로 받지 않는다
    assert any(t["below_fraction"] > 0.03 for t in res.evidence["floor_search"])


def test_floor_only_desk_and_ceiling_never_becomes_floor(tmp_path):
    # 바닥과 벽 아래쪽이 없고 책상·천장만: 천장은 카메라 위라서, 책상은 아래 점이 없어도 카메라 높이 조건(0.5 m)을 넘지만…
    run = make_run(tmp_path, drop_labels=(0, 1, 2, 3, 4))
    _, res = _fix(run)
    # 책상 윗면이 유일한 수평 후보라면 바닥으로 잡힐 수 있다 → 그 경우에도 벽이 없어 축이 모호해 World 를 만들지 않는다
    assert res.status == STATUS_FAILED
    assert res.reason in (FLOOR_NOT_FOUND, AXES_AMBIGUOUS)


def test_one_wall_direction_uses_wall_and_floor(tmp_path):
    """벽 방향 하나(-277 전에는 AXES_AMBIGUOUS): 벽 하나 + 바닥으로 축(WALL_AND_FLOOR). 자세한 시험은 test_open_space."""
    run = make_run(tmp_path, drop_labels=(3, 4))  # y=0, y=W 벽이 안 보임 → 벽 방향 하나
    _, res = _fix(run)
    assert res.status == STATUS_OK and res.axes_source == "WALL_AND_FLOOR"
    assert "ONE_WALL_DIRECTION" in res.evidence["axes"]["walls_status"]


def _wall(normal, inliers):
    n = np.asarray(normal, float)
    n /= np.linalg.norm(n)
    return Wall(n, 0.0, inliers, 3.0, 2.0, 0.0, 0.0, True)


def test_non_orthogonal_walls_are_ambiguous():
    up = np.array([0, 0, 1.0])
    a = 60.0
    walls = [_wall([1, 0, 0], 1000), _wall([np.cos(np.radians(a)), np.sin(np.radians(a)), 0], 800)]
    pair, why, _ = choose_axes(walls, up, WorldFrameConfig())
    assert pair is None and "WALLS_NOT_ORTHOGONAL" in why
    # 경계값: 직교 오차 정확히 10° 는 받고 10.5° 는 받지 않는다
    for ang, ok in ((80.0, True), (79.5, False)):
        walls = [_wall([1, 0, 0], 1000), _wall([np.cos(np.radians(ang)), np.sin(np.radians(ang)), 0], 800)]
        assert (choose_axes(walls, up, WorldFrameConfig())[0] is not None) is ok
    # 경계가 아닌 수직면(방 가운데)은 축 후보가 아니다
    inner = Wall(np.array([0, 1.0, 0]), 0.0, 5000, 3.0, 2.0, 0.0, 0.4, False)
    pair, why, _ = choose_axes([_wall([1, 0, 0], 1000), inner], up, WorldFrameConfig())
    assert pair is None and "ONE_WALL_DIRECTION" in why


# ----- 발 접지점


def _feet(run, offset):
    rng = np.random.default_rng(1)
    xy = rng.uniform([1, 1], [L - 1, W - 1], size=(30, 2))
    pts = np.column_stack([xy, np.full(30, offset)])
    return (run.world_to_ma[:3, :3] @ pts.T).T + run.world_to_ma[:3, 3]


def test_foot_points_confirm_or_reject_floor(sim_run):
    _, ok = _fix(sim_run, foot_points=_feet(sim_run, 0.01))
    assert ok.status == STATUS_OK and ok.evidence["floor"]["foot_check"]["status"] == "CONSISTENT"
    _, bad = _fix(sim_run, foot_points=_feet(sim_run, 0.20))  # 발이 바닥보다 20 cm 위 → 바닥과 어긋남
    assert bad.status == STATUS_FAILED and bad.reason == FLOOR_FOOT_MISMATCH and bad.rotation is None
    _, few = _fix(sim_run, foot_points=_feet(sim_run, 0.20)[:5])  # 표본 부족은 확인 못 함(실패도 확인도 아님)
    assert few.status == STATUS_OK and few.evidence["floor"]["foot_check"]["status"] == "NOT_CHECKED"


# ----- 스케일 우선순위


def test_scale_priority_order_and_cross_checks():
    E = ScaleEstimate
    full = [E(ScaleBasis.STANDARD_OBJECT, 0.97), E(ScaleBasis.HUMAN_HEIGHT_STATS, 0.95, sample_count=25),
            E(ScaleBasis.KNOWN_PERSON_HEIGHT, 0.955), E(ScaleBasis.MEASURED_OBJECT, 0.96, label="cone")]
    c = choose_scale(full)
    assert c.basis == "MEASURED_OBJECT" and c.scale == pytest.approx(0.96)
    assert c.cross_checks == ("KNOWN_PERSON_HEIGHT", "HUMAN_HEIGHT_STATS", "STANDARD_OBJECT", "MODEL_ESTIMATE")
    assert choose_scale(full[:3]).basis == "KNOWN_PERSON_HEIGHT"
    assert choose_scale(full[:2]).basis == "HUMAN_HEIGHT_STATS"
    assert choose_scale(full[:1]).basis == "STANDARD_OBJECT"
    m = choose_scale([])
    assert m.basis == "MODEL_ESTIMATE" and m.scale == 1.0 and "accuracy_note" in m.record
    # 어긋나는 근거는 교차 확인에 넣지 않고 기록한다
    far = choose_scale([E(ScaleBasis.KNOWN_PERSON_HEIGHT, 0.8)])
    assert far.cross_checks == () and far.record["disagreeing"] == {"MODEL_ESTIMATE": 1.0}


def test_disagreeing_measured_objects_fall_back_to_next_basis():
    E = ScaleEstimate
    c = choose_scale([E(ScaleBasis.MEASURED_OBJECT, 0.90, label="a"), E(ScaleBasis.MEASURED_OBJECT, 1.00, label="b"),
                      E(ScaleBasis.KNOWN_PERSON_HEIGHT, 0.95)])
    assert c.basis == "KNOWN_PERSON_HEIGHT"
    assert c.record["notes"]["MEASURED_OBJECT"]["rejected"]


# ----- 사람 마스크


def test_mask_to_model_res_matches_mapanything_crop():
    pytest.importorskip("cv2")
    m = np.zeros((1296, 2304), bool)
    m[600:700, 1100:1200] = True  # 가운데 100 px 사각형
    small = mask_to_model_res(m, (294, 518))
    s = max(518 / 2304, 294 / 1296)
    left = round(0.5 * (2304 * s - 518))
    ys, xs = np.nonzero(small)
    assert xs.min() == pytest.approx(1100 * s - left, abs=1.5) and xs.max() == pytest.approx(1200 * s - left, abs=1.5)
    assert ys.min() == pytest.approx(600 * s, abs=1.5)
    assert not mask_to_model_res(np.zeros_like(m), (294, 518)).any()


def _person_points(run_loaded, sim):
    pts = run_loaded.all_points()
    Minv = np.linalg.inv(sim.world_to_ma)
    w = (Minv[:3, :3] @ pts.T).T + Minv[:3, 3]
    lo, hi = np.array(PERSON[0]) - 0.02, np.array(PERSON[1]) + 0.02
    lo[2] = 0.05  # 사람 발밑 바닥 점은 사람 점이 아니다
    return int(np.all((w > lo) & (w < hi), axis=1).sum())


def test_person_mask_removes_person_points(tmp_path):
    from simai_ai.calibration.world_frame.inputs import load_prepared

    pytest.importorskip("cv2")
    sim = make_run(tmp_path, masks=True)
    plain = load_mapanything_run(sim.run_dir)
    masked = load_mapanything_run(sim.run_dir, prepared=load_prepared(sim.prepared_dir))
    assert _person_points(plain, sim) > 100  # 음성 대조: 마스크가 없으면 사람 점이 들어온다
    assert _person_points(masked, sim) == 0
    assert masked.record["person_masks_applied"] and not plain.record["person_masks_applied"]
    assert all(v["person_pixels_removed"] > 0 for v in masked.record["views"])
    res = fix_world_frame(masked.all_points(), masked.cam2world())
    assert res.status == STATUS_OK


# ----- 직교가 아닌(허용 ±10° 안) 벽: 직교화 뒤 방이 +X·+Y 쪽인지


def _skewed_room(k, support, seed=42):
    """평행사변형 방: (u, v) ∈ [0, 8]×[0, 6.5] → (u, v + k·u). support = 벽(u0, u8, v0, v65) 점 수."""
    rng = np.random.default_rng(seed)
    Lu, Lv, Hz = 8.0, 6.5, 2.9

    def sheared(u, v, z):
        return np.column_stack([u, v + k * u, z])

    parts = [sheared(rng.uniform(0, Lu, 8000), rng.uniform(0, Lv, 8000), np.zeros(8000))]
    for name, n in support.items():
        z = rng.uniform(0, Hz, n)
        if name in ("u0", "u8"):
            parts.append(sheared(np.full(n, 0.0 if name == "u0" else Lu), rng.uniform(0, Lv, n), z))
        else:
            parts.append(sheared(rng.uniform(0, Lu, n), np.full(n, 0.0 if name == "v0" else Lv), z))
    from .conftest import look

    target = (4.0, 3.25 + 4 * k, 0.6)
    cams = {}
    for i, (u, v) in enumerate(((0.3, 0.3), (7.7, 0.3), (7.7, 6.2), (0.3, 6.2))):
        c = np.array([u, v + k * u, 2.6])
        T = np.eye(4)
        T[:3, :3] = look(c, target).T
        T[:3, 3] = c
        cams[f"cam{i}"] = T
    return np.concatenate(parts), cams


@pytest.mark.parametrize("deg", [9.0, -9.0, 0.0])
@pytest.mark.parametrize("order", ["u0v0", "u8v65", "u0v65"])
def test_skewed_walls_keep_room_in_positive_quadrant_or_fail(deg, order):
    big, small = 5000, 1500
    support = {"u0": small, "u8": small, "v0": small, "v65": small}
    for w in {"u0v0": ("u0", "v0"), "u8v65": ("u8", "v65"), "u0v65": ("u0", "v65")}[order]:
        support[w] = big
    k = np.tan(np.radians(deg))
    pts, cams = _skewed_room(k, support)
    res = fix_world_frame(pts, cams)
    # 지지 점이 많은 두 벽의 모서리가 원점이다. (u0,v0)·(u8,v6.5) 는 k < 0 이면 둔각, (u0,v6.5) 는 k > 0 이면 둔각
    obtuse = (k < -1e-9) if order in ("u0v0", "u8v65") else (k > 1e-9)
    if obtuse:
        assert res.status == STATUS_FAILED and res.reason == AXES_AMBIGUOUS and "QUADRANT" in res.detail
        assert res.rotation is None
    else:
        assert res.status == STATUS_OK, res.detail
        local = (pts - res.origin) @ res.rotation.T
        floor = local[np.abs(local[:, 2]) < 0.03]
        assert np.mean((floor[:, 0] < -0.10) | (floor[:, 1] < -0.10)) <= 0.05
        assert res.evidence["axes"]["floor_outside_quadrant_fraction"] <= 0.05
        assert abs(res.evidence["axes"]["orthogonality_error_deg"] - abs(deg)) < 1.0


def test_mixed_sign_foot_offsets_are_inconsistent():
    from simai_ai.calibration.world_frame.frame import Plane, check_floor_with_feet

    floor = Plane(np.array([0, 0, 1.0]), 0.0, 1000, 0.01)
    feet = np.column_stack([np.zeros(30), np.zeros(30), np.r_[np.full(15, -0.10), np.full(15, 0.10)]])
    c = check_floor_with_feet(floor, feet, WorldFrameConfig())
    assert c["status"] == "INCONSISTENT"  # 부호 중앙값 0 으로 상쇄되지 않는다
    assert c["signed_median_distance_ma"] == pytest.approx(0.0) and c["median_abs_distance_ma"] == pytest.approx(0.10)
    # 경계값: 절대 거리 중앙값 0.05 는 받고 0.051 은 받지 않는다
    for z, ok in ((0.05, "CONSISTENT"), (0.051, "INCONSISTENT")):
        f = np.column_stack([np.zeros(30), np.zeros(30), np.r_[np.full(15, -z), np.full(15, z)]])
        assert check_floor_with_feet(floor, f, WorldFrameConfig())["status"] == ok



# ----- 법선이 조금 다른 바닥 후보 둘: MapAnything 원점이 아니라 카메라 기준 높이로 비교


def _two_floor_candidates(seed=3):
    """MapAnything 원점 = 방 모서리 위 카메라(0, 0, 0). 진짜 바닥 p1: z = −2.6(x ∈ [0, 2.5]). 두 번째 후보 p2: 방 가운데
    (x = 4)에서 0.15 m 높고 원점 쪽으로 4° 기운 면(x ∈ [4, 8]). offset 으로는 p2(−2.72)가 p1(−2.60)보다 낮다."""
    rng = np.random.default_rng(seed)
    n = 6000
    p1 = np.column_stack([rng.uniform(0, 2.5, n), rng.uniform(0, 6, n), np.full(n, -2.6)])
    x = rng.uniform(4, 8, n)
    p2 = np.column_stack([x, rng.uniform(0, 6, n), -2.45 + np.tan(np.radians(4)) * (x - 4)])
    centers = np.array([[0, 0, 0], [8, 0, 0], [8, 6, 0], [0, 6, 0]], float)
    return np.concatenate([p1, p2]), centers


def test_tilted_second_floor_candidate_is_not_taken_as_floor():
    from simai_ai.calibration.world_frame.frame import find_floor

    pts, centers = _two_floor_candidates()
    cfg = WorldFrameConfig()
    floor, tried = find_floor(pts, centers, np.array([0, 0, 1.0]), cfg, np.random.default_rng(0))
    assert sum(t["floor_candidate"] for t in tried) == 2  # 둘 다 바닥 후보 조건은 통과한다
    assert floor is not None and abs(floor.normal[2]) > 0.9999  # 기운 p2 가 아니라 수평 p1
    assert np.mean(floor.distance(centers)) == pytest.approx(2.6, abs=0.01)
    assert min(t["offset_ma"] for t in tried) < floor.offset  # 원점 기준 offset 으로 고르면 p2 가 뽑힌다(반례 확인)


def test_ceiling_height_is_measured_at_the_cameras():
    """천장이 4° 기울어도 천장 높이는 카메라 중심 평균 아래·위에서 잰 값(2.9 m)이다(원점 기준 offset 차이가 아니라)."""
    pts, cams = _skewed_room(0.0, {"u0": 5000, "u8": 1500, "v0": 5000, "v65": 1500})
    rng = np.random.default_rng(5)
    x = rng.uniform(0, 8, 6000)
    ceil = np.column_stack([x, rng.uniform(0, 6.5, 6000), 2.9 + np.tan(np.radians(4)) * (x - 4)])
    res = fix_world_frame(np.concatenate([pts, ceil]), cams)
    assert res.status == STATUS_OK, res.detail
    center_x = np.mean([T[0, 3] for T in cams.values()])
    assert center_x == pytest.approx(4.0)
    assert res.evidence["room"]["ceiling_height_m"] == pytest.approx(2.9, abs=0.03)


# ----- 바닥 법선 + 카메라별 실측 높이 기울기(피치·롤) 보정 (S15P31A101-542)


TRUE_HEIGHTS = {cid: float(c[2]) for cid, c in CAMS.items()}


def _doc_from_world_frame(res):
    from simai_ai.calibration.criteria import CalibrationJudgement, ScaleDecision
    from simai_ai.calibration.document import CameraCalibration, build_calibration_doc
    from simai_ai.tracking.system_health import CalibrationState

    K = [[620.0, 0.0, 448.0], [0.0, 620.0, 252.0], [0.0, 0.0, 1.0]]
    cams = [
        CameraCalibration(
            camera_id=cid,
            image_size=(896, 504),
            K=K,
            R=np.eye(3).tolist(),
            t=[0.0, 0.0, -float(h)],
        )
        for cid, h in res.evidence["room"]["camera_heights_m"].items()
    ]
    judgement = CalibrationJudgement(
        state=CalibrationState.PROVISIONAL,
        failures=(),
        scale=ScaleDecision(
            None,
            None,
            tuple(ScaleBasis(b) for b in res.scale_cross_checks),
            ScaleBasis(res.scale_basis),
        ),
    )
    return build_calibration_doc(
        calibration_version="cal-wf-tilt-test",
        provider_source="G1_AUTO",
        provider_version="simai-world-frame-0.1.0",
        created_at=1_775_900_000_000,
        cameras=cams,
        judgement=judgement,
        fallback_scale_basis=ScaleBasis.MODEL_ESTIMATE,
        site_id="test_site",
        world_axes_source=res.axes_source,
        floor_tilt_correction=res.floor_tilt_correction,
    )


@pytest.mark.parametrize(
    ("pitch_deg", "roll_deg"),
    [
        (1.6, -1.2),
        (-1.057, 0.8),
        (2.2, 1.5),
        (-2.4, -0.9),
    ],
)
def test_synthetic_4_cameras_known_pitch_roll_restores_tilt_under_0_2_deg(sim_run, pitch_deg, roll_deg):
    """알려진 피치·롤을 넣은 합성 카메라 4대 → +Z 기울기 오차 0.2° 이하로 복원한다."""
    from simai_ai.calibration.world_frame.frame import _rodrigues_tilt
    from simai_ai.providers.contract import check_calibration

    run = load_mapanything_run(sim_run.run_dir)
    pts = run.all_points()
    base_cams = run.cam2world()

    # 기준 실행(기울기 없음)에서 초기 바닥 평면 기저 Ra0 = [X0, Y0, u0] 와 바닥 중심점 p_bar 를 구한다.
    base_res = fix_world_frame(pts, base_cams)
    assert base_res.status == STATUS_OK
    Ra0 = base_res.rotation
    u0 = Ra0[2]
    floor_offset = float(base_res.evidence["floor"]["offset_ma"])
    on_floor = np.abs(pts @ u0 - floor_offset) < 0.03
    p_bar = pts[on_floor].mean(axis=0)

    pitch_rad = float(np.radians(pitch_deg))
    roll_rad = float(np.radians(roll_deg))
    # Ra_new = _rodrigues_tilt(pitch, roll) @ Ra0 이므로 u_true = Ra_new[2] 는 MapAnything 좌표에서 R_t^T 회전이다.
    R_true_tilt = Ra0.T @ _rodrigues_tilt(pitch_rad, roll_rad).T @ Ra0
    u_true = R_true_tilt @ u0

    # 진짜 수평면 법선이 u_true 인 세계에서 4대 카메라가 바닥 위로 정확히 TRUE_HEIGHTS 높이를 갖도록 카메라를 배치한다.
    # 점구름 바닥은 여전히 u0 이므로, 카메라 높이를 쓰지 않으면 +Z 가 u_true 에서 hypot(pitch, roll) 만큼 기울어진다.
    tilted_cams = {}
    for cid, T in base_cams.items():
        T2 = np.eye(4, dtype=float)
        T2[:3, :3] = R_true_tilt @ T[:3, :3]
        T2[:3, 3] = p_bar + R_true_tilt @ (T[:3, 3] - p_bar)
        tilted_cams[cid] = T2

    uncorrected = fix_world_frame(pts, tilted_cams)
    uncorrected_err_deg = float(np.degrees(np.arccos(np.clip(uncorrected.rotation[2] @ u_true, -1.0, 1.0))))
    assert uncorrected_err_deg == pytest.approx(np.hypot(pitch_deg, roll_deg), abs=0.05)

    corrected = fix_world_frame(pts, tilted_cams, camera_heights=TRUE_HEIGHTS)
    assert corrected.status == STATUS_OK, corrected.detail
    restored_err_deg = float(np.degrees(np.arccos(np.clip(corrected.rotation[2] @ u_true, -1.0, 1.0))))
    assert restored_err_deg <= 0.2, f"복원 기울기 오차 {restored_err_deg:.4f}° > 0.2°"

    tc = corrected.floor_tilt_correction
    assert tc is not None
    assert tc["status"] == "APPLIED" and tc["applied"] is True
    assert tc["pitch_rad"] == pytest.approx(pitch_rad, abs=3e-3)
    assert tc["roll_rad"] == pytest.approx(roll_rad, abs=3e-3)
    assert tc["observation_sources"] == ["POINTCLOUD_FLOOR_RANSAC", "MEASURED_CAMERA_HEIGHT"]
    assert tc["estimation_method"] == "REGULARIZED_SMALL_ROTATION_LSQ"
    assert tc["residual_rms_after_cm"] < 0.25
    assert tc["residual_rms_after_cm"] < tc["residual_rms_before_cm"]

    doc = _doc_from_world_frame(corrected)
    assert check_calibration(doc) == []


def test_session12_lobby_camera_poses_tilt_regression():
    """12차 로비 실측 포즈(cal-lobby-init-f0ab1c6b-jba2-2f6cc097, -1.057° 기울기) 회귀 시험."""
    from simai_ai.providers.contract import check_calibration

    # 12차 로비 Stage A 보정 문서(cal-lobby-init-f0ab1c6b-jba2-2f6cc097)의 4대 카메라 World 포즈
    # 실측 높이: cam_A=2.44m, cam_B=2.44m, cam_C=2.48m, cam_D=2.48m
    # 보정 전 카메라 중심 Z = [2.3592, 2.3821, 2.5417, 2.5528] m (잔차 [-8.08, -5.79, +6.17, +7.28] cm, RMS 6.88 cm)
    s12_cameras = {
        "cam_A": {
            "R": [
                [-0.5209663869565954, 0.8202460714413591, -0.23620062338819683],
                [0.42895767323042644, 0.012345857489691019, -0.9032402673442959],
                [-0.7379631498380582, -0.5718777809455245, -0.3582824551715579],
            ],
            "t": [-1.5475323907466019, -1.1606291214736633, 10.530864208538762],
        },
        "cam_B": {
            "R": [
                [0.585409626667118, 0.7759073302701651, 0.2350823142002199],
                [0.4180981866147502, -0.04049671299117791, -0.9074987699544654],
                [-0.6946148894505797, 0.6295459983455127, -0.34811249895580015],
            ],
            "t": [-5.422958902132357, -0.8964899585500822, 5.517899610615518],
        },
        "cam_C": {
            "R": [
                [0.6780788233947153, -0.6757894454456194, -0.2889943234878501],
                [-0.46215817478187105, -0.08629388468759952, -0.8825888896115979],
                [0.5715058087404219, 0.7320259701745558, -0.3708356679300734],
            ],
            "t": [1.1549301007187054, 2.2965516076709993, 0.4882764669098269],
        },
        "cam_D": {
            "R": [
                [-0.6007211482029713, -0.7730572128716535, 0.20375635221268215],
                [-0.46078597809623345, 0.1265301381199789, -0.8784454686917584],
                [0.6533072856840662, -0.6215888405010396, -0.4322232534189608],
            ],
            "t": [5.145086814624316, 1.334117116440272, 5.620211379925911],
        },
    }
    cam2world = {}
    for cid, cam in s12_cameras.items():
        R = np.asarray(cam["R"], dtype=float)
        t = np.asarray(cam["t"], dtype=float)
        c = -R.T @ t
        T = np.eye(4, dtype=float)
        T[:3, :3] = R.T
        T[:3, 3] = c
        cam2world[cid] = T

    # 12차 초기 World 좌표계에서 바닥 점구름은 Z=0 이고 경계 벽은 X=0, Y=0 다.
    rng = np.random.default_rng(12)
    floor_pts = np.column_stack([rng.uniform(0.0, 9.0, 6000), rng.uniform(0.0, 9.0, 6000), rng.normal(0.0, 0.003, 6000)])
    wx0 = np.column_stack([rng.normal(0.0, 0.003, 2500), rng.uniform(0.0, 9.0, 2500), rng.uniform(0.25, 2.5, 2500)])
    wy0 = np.column_stack([rng.uniform(0.0, 9.0, 2500), rng.normal(0.0, 0.003, 2500), rng.uniform(0.25, 2.5, 2500)])
    pts = np.concatenate([floor_pts, wx0, wy0])

    measured = {"cam_A": 2.44, "cam_B": 2.44, "cam_C": 2.48, "cam_D": 2.48}
    res = fix_world_frame(pts, cam2world, camera_heights=measured)
    assert res.status == STATUS_OK, res.detail
    tc = res.floor_tilt_correction
    assert tc is not None and tc["status"] == "APPLIED" and tc["applied"] is True
    assert tc["tilt_deg"] == pytest.approx(1.057, abs=0.06)
    assert tc["residual_rms_before_cm"] == pytest.approx(6.88, abs=0.2)
    assert tc["residual_rms_after_cm"] < 1.0

    # 각 카메라 중심에서 실측 높이만큼 내려간 바닥 접지점들의 최소제곱 평면 법선과 복원된 +Z 의 각도 차 <= 0.2°
    ids = sorted(measured)
    foot_pts = np.array([cam2world[c][:3, 3] - np.array([0.0, 0.0, measured[c]]) for c in ids])
    u_true = np.linalg.svd(foot_pts - foot_pts.mean(axis=0), full_matrices=False)[2][2]
    if u_true[2] < 0:
        u_true = -u_true
    tilt_err_deg = float(np.degrees(np.arccos(np.clip(res.rotation[2] @ u_true, -1.0, 1.0))))
    assert tilt_err_deg <= 0.2, f"12차 실측 포즈 수평 복원 오차 {tilt_err_deg:.4f}° > 0.2°"
    assert check_calibration(_doc_from_world_frame(res)) == []


def test_session12_local_mapanything_run_regression():
    """노트북 로컬에 12차 MapAnything 산출물이 있으면 실제 점구름에서도 기울기 보정이 작동하는지 확인한다."""
    from pathlib import Path

    from simai_ai.calibration.world_frame.inputs import load_prepared

    s12_root = Path("C:/Users/SSAFY/simai-local/align-535/ma_single")
    if not (s12_root / "mapanything" / "raw.npz").is_file():
        pytest.skip(f"로컬 12차 MapAnything 데이터 없음: {s12_root}")

    prep = load_prepared(s12_root / "prepared")
    run = load_mapanything_run(s12_root / "mapanything", prepared=prep)
    measured = {"cam_A": 2.44, "cam_B": 2.44, "cam_C": 2.48, "cam_D": 2.48}
    res = fix_world_frame(
        run.all_points(),
        run.cam2world(),
        camera_heights=measured,
        config=WorldFrameConfig(camera_height_max_spread=0.5),
    )
    assert res.status == STATUS_OK, res.detail
    tc = res.floor_tilt_correction
    assert tc is not None and tc["status"] == "APPLIED" and tc["applied"] is True
    assert 0.2 <= tc["tilt_deg"] <= 2.0
    assert tc["residual_rms_after_cm"] < tc["residual_rms_before_cm"]
    assert tc["residual_rms_after_cm"] < 1.6


def test_floor_tilt_correction_no_heights_and_two_cameras(sim_run):
    """높이 없음·2대만 있음·모르는 카메라 ID 포함 시 동작 확인."""
    from simai_ai.providers.contract import check_calibration

    run = load_mapanything_run(sim_run.run_dir)
    pts, cams = run.all_points(), run.cam2world()

    # 1) camera_heights=None 이면 floor_tilt_correction 도 None 이고 문서에 싣지 않는다.
    res_none = fix_world_frame(pts, cams, camera_heights=None)
    assert res_none.status == STATUS_OK
    assert res_none.floor_tilt_correction is None
    doc_none = _doc_from_world_frame(res_none)
    assert "floor_tilt_correction" not in doc_none
    assert check_calibration(doc_none) == []

    # 2) 높이를 아는 카메라가 2대뿐이고 모르는 카메라 ID 가 섞인 경우:
    #    모르는 카메라는 빼고, 3대 미만이면 지금 방식 그대로(RANSAC 바닥 법선 유지, 피치·롤은 비운다).
    two_heights = {
        "cam_A": TRUE_HEIGHTS["cam_A"],
        "cam_B": TRUE_HEIGHTS["cam_B"],
        "cam_unknown": 2.50,
    }
    res_two = fix_world_frame(pts, cams, camera_heights=two_heights)
    assert res_two.status == STATUS_OK
    assert res_two.scale_basis == "MEASURED_CAMERA_HEIGHT"
    assert np.allclose(res_two.rotation, res_none.rotation)
    tc = res_two.floor_tilt_correction
    assert tc is not None
    assert tc["status"] == "INSUFFICIENT_CAMERAS" and tc["applied"] is False
    assert tc["pitch_rad"] is None and tc["roll_rad"] is None and tc["tilt_deg"] is None
    assert tc["used_cameras"] == ["cam_A", "cam_B"]
    assert tc["missing_cameras"] == ["cam_C", "cam_D"]
    assert tc["unknown_cameras"] == ["cam_unknown"]
    assert tc["estimation_method"] == "FLOOR_RANSAC_ONLY"
    assert "2대 < 3대" in tc["rejection_reason"]
    assert check_calibration(_doc_from_world_frame(res_two)) == []


def test_floor_tilt_correction_rejects_excessive_tilt_and_height_outlier(sim_run):
    """일정 이상(3°) 기울기 보정과 단일 카메라 높이 이상값은 거부하고 이유를 기록한다."""
    from simai_ai.calibration.world_frame.frame import _rodrigues_tilt
    from simai_ai.providers.contract import check_calibration

    run = load_mapanything_run(sim_run.run_dir)
    pts, base_cams = run.all_points(), run.cam2world()
    base_res = fix_world_frame(pts, base_cams)
    Ra0 = base_res.rotation
    u0 = Ra0[2]
    floor_offset = float(base_res.evidence["floor"]["offset_ma"])
    p_bar = pts[np.abs(pts @ u0 - floor_offset) < 0.03].mean(axis=0)

    # 1) 4.5° 기울기(> 3.0° 상한) → MAX_TILT_EXCEEDED 로 거부하고 RANSAC 바닥 법선을 유지한다.
    R_big = Ra0.T @ _rodrigues_tilt(float(np.radians(4.5)), 0.0) @ Ra0
    big_tilt_cams = {}
    for cid, T in base_cams.items():
        T2 = np.eye(4, dtype=float)
        T2[:3, :3] = R_big @ T[:3, :3]
        T2[:3, 3] = p_bar + R_big @ (T[:3, 3] - p_bar)
        big_tilt_cams[cid] = T2

    res_big = fix_world_frame(pts, big_tilt_cams, camera_heights=TRUE_HEIGHTS)
    assert res_big.status == STATUS_OK
    tc_big = res_big.floor_tilt_correction
    assert tc_big is not None
    assert tc_big["status"] == "REJECTED" and tc_big["applied"] is False
    assert tc_big["tilt_deg"] == pytest.approx(4.5, abs=0.2)
    assert "MAX_TILT_EXCEEDED" in tc_big["rejection_reason"]
    assert np.allclose(res_big.rotation[2], base_res.rotation[2])
    assert check_calibration(_doc_from_world_frame(res_big)) == []

    # 2) 단일 카메라 높이 이상값(한 대만 25 cm 높게 잘못 입력) → 평면 잔차 한도 초과(CAMERA_HEIGHT_OUTLIER)로 거부한다.
    outlier_heights = dict(TRUE_HEIGHTS)
    outlier_heights["cam_B"] += 0.25
    res_out = fix_world_frame(
        pts,
        base_cams,
        camera_heights=outlier_heights,
        config=WorldFrameConfig(camera_height_max_spread=0.5),
    )
    assert res_out.status == STATUS_OK
    tc_out = res_out.floor_tilt_correction
    assert tc_out is not None
    assert tc_out["status"] == "REJECTED" and tc_out["applied"] is False
    assert "CAMERA_HEIGHT_OUTLIER" in tc_out["rejection_reason"]
    assert np.allclose(res_out.rotation, base_res.rotation)
    assert check_calibration(_doc_from_world_frame(res_out)) == []
