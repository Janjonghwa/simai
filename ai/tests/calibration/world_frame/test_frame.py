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


def test_one_wall_direction_is_ambiguous(tmp_path):
    run = make_run(tmp_path, drop_labels=(3, 4))  # y=0, y=W 벽이 안 보임 → 벽 방향 하나
    _, res = _fix(run)
    assert res.status == STATUS_FAILED and res.reason == AXES_AMBIGUOUS
    assert "ONE_WALL_DIRECTION" in res.detail
    assert "floor" in res.evidence  # 바닥은 찾았지만 축이 없으면 변환을 내지 않는다


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
