"""열린 공간 World 고정(-277): 바닥 두 점(축 출처 MANUAL_FLOOR_POINTS)과 카메라 높이 실측 축척(MEASURED_CAMERA_HEIGHT).

합성 교실(SIM, 상한, 제품 성능 아님). 정답 World 의 바닥 점을 MapAnything world 로 옮겨 광선을 만들고(프레임 시험), 원본(왜곡된)
그림 픽셀에서 광선을 만드는 경로(`floor_point_rays`)와 실행 도구(`fix --floor-points --camera-heights`)를 함께 본다.
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np
import pytest

from simai_ai.calibration.criteria import ScaleBasis, ScaleEstimate
from simai_ai.calibration.extrinsics.initial import load_world_alignment
from simai_ai.calibration.world_frame import cli
from simai_ai.calibration.world_frame.frame import (
    AXES_AMBIGUOUS,
    AXES_MANUAL_FLOOR_POINTS,
    AXES_WALL_AND_FLOOR,
    AXES_WALLS,
    FLOOR_NOT_FOUND,
    FLOOR_POINTS_INVALID,
    STATUS_FAILED,
    STATUS_OK,
    FloorPointRays,
    Plane,
    WorldFrameConfig,
    camera_height_scale,
    fix_world_frame,
)
from simai_ai.calibration.world_frame.inputs import floor_point_rays, load_mapanything_run
from simai_ai.providers.contract import camera_center, check_calibration, schema_errors

from .conftest import CAMS, HW, ORIG_FACTOR, intrinsics, intrinsics_record, make_run

SCALE_MA = 1.04
P1 = (3.0, 2.0, 0.0)  # 원점으로 찍을 바닥 점(정답 World), cam_A 에서 보임
P2 = (6.0, 1.5, 0.0)  # +X 방향 점, 두 점 사이 3.04 m
TRUE_HEIGHTS = {c: xyz[2] for c, xyz in CAMS.items()}
NOW = 1_792_600_000_000


def _ma(run, p):
    return run.world_to_ma[:3, :3] @ np.asarray(p, float) + run.world_to_ma[:3, 3]


def _rays(run, p1=P1, p2=P2, cam="cam_A", observed=True):
    c = _ma(run, CAMS[cam])
    q1, q2 = _ma(run, p1), _ma(run, p2)
    obs = (q1, q2) if observed else (None, None)
    return FloorPointRays(cam, c, (q1 - c, q2 - c), obs, {"test": True})


def _fix(run, **kw):
    r = load_mapanything_run(run.run_dir)
    return fix_world_frame(r.all_points(), r.cam2world(), **kw)


def _truth_to_world(res, run):
    return res.matrix() @ run.world_to_ma


def _apply(M, p):
    return M[:3, :3] @ np.asarray(p, float) + M[:3, 3]


def _assert_manual_frame(res, run, scale):
    """원점 = P1, +X = P2 − P1 방향, +Z = 상방, 오른손, 길이 = 정답 × scale. 허용 3 cm = 평면 허용 거리(바닥 추정 오차가
    광선 교점을 그만큼 옮긴다)."""
    assert res.status == STATUS_OK, res.detail
    assert res.axes_source == AXES_MANUAL_FLOOR_POINTS
    C = _truth_to_world(res, run)
    A = C[:3, :3] / scale
    assert np.allclose(A @ A.T, np.eye(3), atol=2e-3) and np.linalg.det(A) > 0
    assert abs(A[2, 2] - 1) < 2e-3  # +Z = 상방
    assert np.allclose(_apply(C, P1), 0, atol=0.03)
    sep = np.linalg.norm(np.subtract(P2, P1))
    assert np.allclose(_apply(C, P2), [sep * scale, 0, 0], atol=0.03)
    # 오른손: P1 에서 X 의 왼쪽(정답 World 에서 반시계 90°) 바닥 점은 +Y
    d = np.subtract(P2, P1)[:2] / sep
    left = np.array([P1[0] - d[1], P1[1] + d[0], 0.0])
    assert _apply(C, left)[1] == pytest.approx(scale, abs=0.03)


# ----- 바닥 두 점: 벽 하나·벽 없음


def test_one_wall_room_with_floor_points_is_fixed(tmp_path):
    run = make_run(tmp_path, drop_labels=(3, 4))  # y=0, y=W 벽 안 보임 → 벽 방향 하나
    res = _fix(run, floor_points=_rays(run))
    _assert_manual_frame(res, run, SCALE_MA)  # 스케일 근거 없음 → 모델 길이 그대로
    axes = res.evidence["axes"]
    assert "ONE_WALL_DIRECTION" in axes["walls_status"]  # 벽 한 방향 자동 경로보다 사람 지정이 이긴다
    comp = axes["walls_comparison"]  # 벽 하나의 축(±x)과 X = (3, −0.5) 방향의 각도 9.46° 를 기록만
    assert comp["walls"] == "ONE_WALL" and abs(comp["angle_to_wall_axes_deg"]) == pytest.approx(9.46, abs=0.5)
    assert res.warnings == () and res.evidence["origin"]["source"] == "FLOOR_POINT"
    pts = axes["floor_points"]["points"]
    assert pts["origin"]["floor_check"] == pts["x_axis"]["floor_check"] == "ON_FLOOR"
    assert axes["floor_points"]["separation_m"] == pytest.approx(3.04 * SCALE_MA, abs=0.01)
    q = axes["floor_quadrant_fractions"]  # 판정 없이 기록만(합이 1)
    assert sum(q.values()) == pytest.approx(1.0, abs=1e-3) and min(q.values()) > 0
    assert res.evidence["origin"]["rule"] == axes["rule"]


def test_open_space_without_walls_needs_floor_points(tmp_path):
    run = make_run(tmp_path, drop_labels=(1, 2, 3, 4))  # 경계 벽이 하나도 안 보임(열린 공간)
    plain = _fix(run)
    assert plain.status == STATUS_FAILED and plain.reason == AXES_AMBIGUOUS and plain.rotation is None
    assert "NO_WALL" in plain.detail and "--floor-points" in plain.detail
    assert plain.axes_source is None
    _assert_manual_frame(_fix(run, floor_points=_rays(run)), run, SCALE_MA)


def test_same_points_give_same_transform(tmp_path):
    run = make_run(tmp_path, drop_labels=(3, 4))
    a = _fix(run, floor_points=_rays(run))
    b = _fix(run, floor_points=_rays(run))
    assert np.array_equal(a.matrix(), b.matrix())
    # 광선 길이(픽셀 → 광선 규약)와 관계없이 같은 교점
    c = _rays(run)
    longer = FloorPointRays(c.camera_id, c.center, tuple(3.0 * d for d in c.directions), c.observed)
    assert np.allclose(_fix(run, floor_points=longer).matrix(), a.matrix(), atol=1e-9)


def test_floor_points_beat_walls_and_angle_is_recorded(sim_run):
    walls = _fix(sim_run)
    assert walls.status == STATUS_OK and walls.axes_source == AXES_WALLS  # 회귀: 두 점이 없으면 벽 경로 그대로
    assert walls.evidence["axes"]["source"] == AXES_WALLS
    along = _fix(sim_run, floor_points=_rays(sim_run, P1, (6.0, 2.0, 0.0)))  # 벽(+x)과 나란한 X
    assert along.axes_source == AXES_MANUAL_FLOOR_POINTS
    assert abs(along.evidence["axes"]["walls_comparison"]["angle_to_wall_axes_deg"]) < 0.5
    assert along.warnings == ()
    p2 = (P1[0] + 3 * math.cos(math.radians(30)), P1[1] + 3 * math.sin(math.radians(30)), 0.0)
    tilted = _fix(sim_run, floor_points=_rays(sim_run, P1, p2))
    assert tilted.status == STATUS_OK and tilted.axes_source == AXES_MANUAL_FLOOR_POINTS  # 사람 지정이 이긴다
    assert abs(tilted.evidence["axes"]["walls_comparison"]["angle_to_wall_axes_deg"]) == pytest.approx(30, abs=0.5)
    assert any("경계 벽 축" in w for w in tilted.warnings)
    assert np.allclose(_apply(_truth_to_world(tilted, sim_run), P1), 0, atol=0.01)


# ----- 벽 한 방향 자동 경로(WALL_AND_FLOOR)


def test_one_wall_direction_fixes_axes_from_wall_and_floor(tmp_path):
    run = make_run(tmp_path, drop_labels=(3, 4))  # x=0, x=L 벽만 보임(한 방향)
    res = _fix(run)
    assert res.status == STATUS_OK and res.axes_source == AXES_WALL_AND_FLOOR, res.detail
    C = _truth_to_world(res, run)
    A = C[:3, :3] / SCALE_MA
    assert np.allclose(A @ A.T, np.eye(3), atol=2e-3) and np.linalg.det(A) > 0 and abs(A[2, 2] - 1) < 2e-3
    # X = 지지 점이 가장 많은 벽(x=0 또는 x=L)의 안쪽 법선 → 정답 ±x
    assert abs(abs(A[0, 0]) - 1) < 2e-3
    # 원점 = 그 벽과 바닥의 교선 위, 카메라 중심 평균(y = 3.25)의 수선 발
    o = np.linalg.solve(C[:3, :3], -C[:3, 3])
    assert min(abs(o[0]), abs(o[0] - 8.0)) < 0.03 and o[1] == pytest.approx(3.25, abs=0.03) and abs(o[2]) < 0.02
    axes = res.evidence["axes"]
    assert res.evidence["origin"]["source"] == "CAMERA_LAYOUT_ON_WALL"
    assert axes["floor_outside_x_fraction"] <= 0.05 and axes["x_wall_inliers"] > 0
    q = axes["floor_quadrant_fractions"]  # Y 쪽은 기록만: 원점이 방 가운데라 양쪽에 바닥이 있다
    assert q["+X+Y"] > 0.3 and q["+X-Y"] > 0.3
    assert any("원점" in w for w in res.warnings)


def test_wall_and_floor_axes_are_reproducible_but_origin_follows_cameras(tmp_path):
    """벽은 영구 구조라 축은 같고, 카메라를 벽을 따라 옮기면 원점이 Y 로 움직인다(잔여 위험)."""
    run = make_run(tmp_path, drop_labels=(3, 4))
    loaded = load_mapanything_run(run.run_dir)
    pts, cams = loaded.all_points(), loaded.cam2world()
    base = fix_world_frame(pts, cams)
    shift = run.world_to_ma[:3, :3] @ [0, 1.0, 0]  # 정답 World 에서 +y 1 m
    moved = {c: np.vstack([np.column_stack([T[:3, :3], T[:3, 3] + shift]), T[3]]) for c, T in cams.items()}
    res = fix_world_frame(pts, moved)
    assert res.axes_source == base.axes_source == AXES_WALL_AND_FLOOR
    assert np.allclose(res.rotation, base.rotation, atol=1e-9)
    d = (res.origin - base.origin) @ base.rotation.T
    assert abs(abs(d[1]) - SCALE_MA) < 0.01 and abs(d[0]) < 1e-6 and abs(d[2]) < 1e-6


def test_wall_priority_two_walls_over_one_wall(sim_run):
    res = _fix(sim_run)
    assert res.axes_source == AXES_WALLS and res.evidence["origin"]["source"] == "WALL_CORNER"


# ----- 바닥 두 점: 비우는 경우


def _up_ma(run):
    return run.world_to_ma[:3, :3] @ [0, 0, 1.0] / SCALE_MA


def test_ray_nearly_parallel_to_floor_or_upward_fails(sim_run):
    c = _ma(sim_run, CAMS["cam_A"])
    up = _up_ma(sim_run)
    horiz = sim_run.world_to_ma[:3, :3] @ [1.0, 0.8, 0] / SCALE_MA
    horiz /= np.linalg.norm(horiz)
    good = _ma(sim_run, P2) - c

    def rays(d):
        return FloorPointRays("cam_A", c, (d, good), (None, None))

    for deg, ok in ((9.9, False), (10.1, True)):  # 바닥과 이루는 각 하한 10°
        d = math.cos(math.radians(deg)) * horiz - math.sin(math.radians(deg)) * up
        res = _fix(sim_run, floor_points=rays(d))
        if ok:
            assert res.status == STATUS_OK, res.detail
        else:
            assert res.status == STATUS_FAILED and res.reason == FLOOR_POINTS_INVALID and "RAY_GRAZING" in res.detail
            assert res.rotation is None and res.axes_source is None
    up_res = _fix(sim_run, floor_points=rays(horiz + 0.2 * up))  # 수평 위(교점이 카메라 뒤)
    assert up_res.reason == FLOOR_POINTS_INVALID and "BEHIND_CAMERA" in up_res.detail
    assert up_res.evidence["floor"]  # 바닥은 찾았다(근거는 남는다)


def test_points_too_close_fail_after_scale(sim_run):
    near = (3.9, 2.0, 0.0)  # 정답 0.9 m (모델 0.936)
    res = _fix(sim_run, floor_points=_rays(sim_run, P1, near))
    assert res.reason == FLOOR_POINTS_INVALID and "POINTS_TOO_CLOSE" in res.detail
    # 모델 길이 1.0192 m(정답 0.98 m × 1.04): 모델 스케일이면 받고, 카메라 높이로 미터를 되찾으면(0.98 m) 거부한다
    edge = (3.98, 2.0, 0.0)
    assert _fix(sim_run, floor_points=_rays(sim_run, P1, edge)).status == STATUS_OK
    res = _fix(sim_run, floor_points=_rays(sim_run, P1, edge), camera_heights=TRUE_HEIGHTS)
    assert res.reason == FLOOR_POINTS_INVALID and "POINTS_TOO_CLOSE" in res.detail
    assert res.evidence["scale"]["basis"] == "MEASURED_CAMERA_HEIGHT"  # 축을 못 정해도 스케일 근거는 남긴다


def test_point_off_floor_is_refused(sim_run):
    desk_top = _ma(sim_run, (4.5, 3.0, 0.75))  # 책상 윗면을 찍었다(광선은 그 뒤 바닥과 만난다)
    r = _rays(sim_run, P1, (4.5, 3.0, 0.0))
    res = _fix(sim_run, floor_points=FloorPointRays(r.camera_id, r.center, r.directions, (r.observed[0], desk_top)))
    assert res.reason == FLOOR_POINTS_INVALID and "POINT_NOT_ON_FLOOR" in res.detail
    assert res.evidence["axes"]["floor_points"]["points"]["x_axis"]["floor_check"] == "OFF_FLOOR"
    # 근처 점이 없으면(사람 마스크 등) 확인 못 함으로 기록하고 진행한다
    ok = _fix(sim_run, floor_points=_rays(sim_run, observed=False))
    assert ok.status == STATUS_OK
    assert ok.evidence["axes"]["floor_points"]["points"]["origin"]["floor_check"] == "NOT_CHECKED"


def test_no_floor_stays_empty_even_with_floor_points(tmp_path):
    run = make_run(tmp_path, drop_labels=(0,))
    res = _fix(run, floor_points=_rays(run, observed=False), camera_heights=TRUE_HEIGHTS)
    assert res.status == STATUS_FAILED and res.reason == FLOOR_NOT_FOUND and res.rotation is None


# ----- 카메라 높이 축척


def test_camera_heights_restore_metric_scale(sim_run):
    res = _fix(sim_run, camera_heights=TRUE_HEIGHTS)
    assert res.status == STATUS_OK and res.axes_source == AXES_WALLS
    assert res.scale_basis == "MEASURED_CAMERA_HEIGHT" and res.scale == pytest.approx(1 / SCALE_MA, rel=0.005)
    assert "MODEL_ESTIMATE" in res.scale_cross_checks  # 4% < 5%
    for cid, h in TRUE_HEIGHTS.items():
        assert res.evidence["room"]["camera_heights_m"][cid] == pytest.approx(h, abs=0.01)
    rec = res.evidence["scale"]["camera_heights"]
    assert rec["cameras"] == 4 and rec["spread"] < 0.005 and "rejected" not in rec
    # 벽 경로의 축·원점은 스케일 근거와 관계없이 같다(회귀)
    base = _fix(sim_run)
    assert np.allclose(res.rotation, base.rotation) and np.allclose(res.origin, base.origin)
    # 두 점과 함께: 길이가 미터로 돌아온다
    _assert_manual_frame(_fix(sim_run, floor_points=_rays(sim_run), camera_heights=TRUE_HEIGHTS), sim_run, 1.0)


def test_measured_object_outranks_camera_heights(sim_run):
    res = _fix(sim_run, camera_heights=TRUE_HEIGHTS, scale_estimates=[
        ScaleEstimate(ScaleBasis.MEASURED_OBJECT, 1 / SCALE_MA, label="cone-1"),
        ScaleEstimate(ScaleBasis.MEASURED_OBJECT, 1 / SCALE_MA, label="pole-1"),
    ])
    assert res.scale_basis == "MEASURED_OBJECT"
    assert res.scale_cross_checks[0] == "MEASURED_CAMERA_HEIGHT"  # 우선순위: 정물 > 카메라 높이 > …
    with pytest.raises(ValueError, match="camera_heights"):
        _fix(sim_run, scale_estimates=[ScaleEstimate(ScaleBasis.MEASURED_CAMERA_HEIGHT, 1.0)])


def test_spread_or_too_few_cameras_reject_camera_heights(sim_run):
    off = dict(TRUE_HEIGHTS, cam_B=TRUE_HEIGHTS["cam_B"] * 1.06)  # 한 대가 6% 어긋남
    res = _fix(sim_run, camera_heights=off)
    assert res.scale_basis == "MODEL_ESTIMATE"  # 쓰지 않고 다음 근거로(지어내지 않음)
    assert "어긋남" in res.evidence["scale"]["camera_heights"]["rejected"]
    one = _fix(sim_run, camera_heights={"cam_A": 2.6, "cam_X": 2.6})
    rec = one.evidence["scale"]["camera_heights"]
    assert one.scale_basis == "MODEL_ESTIMATE" and "2대" in rec["rejected"]
    assert rec["unknown_cameras"] == ["cam_X"] and rec["missing_cameras"] == ["cam_B", "cam_C", "cam_D"]


def test_camera_height_spread_boundary():
    floor = Plane(np.array([0, 0, 1.0]), 0.0, 1000, 0.01)
    centers = np.array([[0, 0, 2.0], [1, 0, 2.0], [2, 0, 2.0]])
    cfg = WorldFrameConfig()
    for f, ok in ((1.029, True), (1.031, False)):  # 중앙값에서 3% 경계
        est, rec = camera_height_scale(floor, centers, ["a", "b", "c"], {"a": 2.0, "b": 2.0, "c": 2.0 * f}, cfg)
        assert (est is not None) is ok, rec
    est, _ = camera_height_scale(floor, centers, ["a", "b", "c"], {"a": 2.6, "b": 2.6, "c": 2.62}, cfg)
    assert est.basis is ScaleBasis.MEASURED_CAMERA_HEIGHT and est.scale == pytest.approx(1.3) and est.sample_count == 3
    with pytest.raises(ValueError):
        camera_height_scale(floor, centers, ["a"], {"a": -1.0}, cfg)


# ----- 원본(왜곡된) 그림 픽셀 → 광선


W0, H0 = HW[1] * ORIG_FACTOR, HW[0] * ORIG_FACTOR
DIST = np.array([-0.05, 0.01, 0.0, 0.0, 0.0])


def _k_prepared():
    """모델 K 를 원본 해상도로(펴진 K'). 896×504 → 224×126 은 정확히 1/4 이고 자르지 않는다."""
    K = intrinsics()
    f = ORIG_FACTOR
    return np.array([[K[0, 0] * f, 0, (K[0, 2] + 0.5) * f - 0.5], [0, K[1, 1] * f, (K[1, 2] + 0.5) * f - 0.5], [0, 0, 1.0]])


def _k_source():
    return np.asarray(intrinsics_record()["shared_lens"]["common"]["camera"]["K"], float)


def _prepared_doc(run, cam="cam_A"):
    return {"views": [{"name": f"{c}_01", "camera_id": c, "image_size": [W0, H0], "K_undistorted": _k_prepared().tolist(),
                       "source_camera": {"K": _k_source().tolist(), "distortion": {"model": "OPENCV_PINHOLE",
                                                                                   "coeffs": DIST.tolist()}}}
                      for c in CAMS]}


def _pixels(run, points, cam="cam_A", space="ORIGINAL"):
    """정답 World 점 → 그 카메라의 원본(왜곡) 또는 펴진 그림 픽셀."""
    import cv2

    from .conftest import TARGET, look

    R = look(CAMS[cam], TARGET)
    t = -R @ np.asarray(CAMS[cam])
    if space == "PREPARED":
        uv, _ = cv2.projectPoints(np.asarray(points, float), cv2.Rodrigues(R)[0], t, _k_prepared(), None)
    else:
        uv, _ = cv2.projectPoints(np.asarray(points, float), cv2.Rodrigues(R)[0], t, _k_source(), DIST)
    return uv.reshape(-1, 2)


def _spec(px, cam="cam_A", space="ORIGINAL"):
    return {"format": "simai.floor_points", "camera_id": cam, "pixel_space": space,
            "origin_px": px[0].tolist(), "x_axis_px": px[1].tolist()}


def test_original_pixels_become_rays_through_the_floor_points(sim_run):
    pytest.importorskip("cv2")
    run = load_mapanything_run(sim_run.run_dir)
    prep = _prepared_doc(sim_run)
    rays = floor_point_rays(run, prep, _spec(_pixels(sim_run, [P1, P2])), K=_k_source(), dist=DIST)
    exact = _rays(sim_run)
    for d, e in zip(rays.directions, exact.directions, strict=True):
        assert math.degrees(math.acos(min(1.0, float(d @ e / np.linalg.norm(e))))) < 0.05
    assert rays.record["observed_points"] == [25, 25]  # 5×5 모델 픽셀 이웃 점(바닥 확인용)
    assert max(rays.record["model_k_vs_prepared_k_deg"]) < 0.01  # 모델 K 와 펴진 K' 의 규약이 맞다
    res = fix_world_frame(run.all_points(), run.cam2world(), floor_points=rays, camera_heights=TRUE_HEIGHTS)
    _assert_manual_frame(res, sim_run, 1.0)
    # 펴진 그림 픽셀로 줘도 같은 광선
    prepared = floor_point_rays(run, prep, _spec(_pixels(sim_run, [P1, P2], space="PREPARED"), space="PREPARED"),
                                K=_k_source(), dist=DIST)
    for a, b in zip(prepared.directions, rays.directions, strict=True):
        assert np.allclose(a, b, atol=1e-4)


def test_pixel_on_desk_or_outside_image_is_refused(sim_run):
    pytest.importorskip("cv2")
    run = load_mapanything_run(sim_run.run_dir)
    prep = _prepared_doc(sim_run)
    # 책상 아래 바닥 점을 겨눈 픽셀은 책상 윗면을 본다 → 근처 MapAnything 점이 바닥 위 0.78 → 비움
    rays = floor_point_rays(run, prep, _spec(_pixels(sim_run, [P1, (4.5, 3.0, 0.0)])), K=_k_source(), dist=DIST)
    res = fix_world_frame(run.all_points(), run.cam2world(), floor_points=rays)
    assert res.reason == FLOOR_POINTS_INVALID and "POINT_NOT_ON_FLOOR" in res.detail
    px = _pixels(sim_run, [P1, P2])
    with pytest.raises(ValueError, match="밖"):
        floor_point_rays(run, prep, _spec(np.array([px[0], [W0 + 5.0, 10.0]])), K=_k_source(), dist=DIST)
    with pytest.raises(ValueError, match="다르다"):  # 입력 폴더가 편 K·왜곡과 다른 기록
        floor_point_rays(run, prep, _spec(px), K=_k_source() * 1.01, dist=DIST)
    with pytest.raises(ValueError, match="없다"):
        floor_point_rays(run, prep, _spec(px, cam="cam_X"), K=_k_source(), dist=DIST)


# ----- 실행 도구


def _with_prepared_k(sim):
    """make_run(masks=True) 입력 폴더에 펴진 K'·원본 K·왜곡을 넣고 MapAnything 실행 기록의 해시를 맞춘다."""
    path = sim.prepared_dir / "prepared.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    extra = {v["name"]: v for v in _prepared_doc(sim)["views"]}
    for v in doc["views"]:
        v |= {k: extra[v["name"]][k] for k in ("K_undistorted", "source_camera")}
    path.write_text(json.dumps(doc), encoding="utf-8")
    metrics = sim.run_dir / "metrics.json"
    m = json.loads(metrics.read_text(encoding="utf-8"))
    m["prepared_sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    metrics.write_text(json.dumps(m), encoding="utf-8")


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _cli(tmp_path, sim, *extra, out="wf.json"):
    intr = _write(tmp_path, "intr.json", intrinsics_record())
    code = cli.main(["fix", "--run-dir", str(sim.run_dir), "--intrinsics", str(intr), "--out", str(tmp_path / out), *extra],
                    now_ms=NOW)
    path = tmp_path / out
    return code, (json.loads(path.read_text(encoding="utf-8")) if path.exists() else None), path


def test_cli_floor_points_and_camera_heights_write_axes_source(tmp_path):
    pytest.importorskip("cv2")
    sim = make_run(tmp_path, drop_labels=(3, 4), masks=True)  # 벽 한 방향
    _with_prepared_k(sim)
    pts = _write(tmp_path, "floor.json", _spec(_pixels(sim, [P1, P2])) | {"image": "cam_A_01.jpg"})
    heights = _write(tmp_path, "heights.json", TRUE_HEIGHTS)  # 맨 대응 형식도 받는다
    code, rec, out = _cli(tmp_path, sim, "--prepared", str(sim.prepared_dir), "--floor-points", str(pts),
                          "--camera-heights", str(heights))
    assert code == cli.EXIT_OK, rec and rec.get("detail")
    assert rec["format_version"] == cli.RESULT_VERSION and rec["axes_source"] == "MANUAL_FLOOR_POINTS"
    doc = rec["calibration"]
    assert schema_errors(doc, "calibration") == [] and check_calibration(doc) == []
    assert doc["schema_version"] == "2.1.0" and doc["world_axes_source"] == "MANUAL_FLOOR_POINTS"
    assert doc["scale_basis"] == "MEASURED_CAMERA_HEIGHT" and doc["state"] == "PROVISIONAL"
    for c in doc["cameras"]:
        assert camera_center(c["R"], c["t"])[2] == pytest.approx(TRUE_HEIGHTS[c["camera_id"]], abs=0.02)
    assert rec["inputs"]["floor_points"]["pixel_space"] == "ORIGINAL" and rec["inputs"]["camera_heights"]["heights_m"]
    assert "MANUAL_FLOOR_POINTS" in rec["note"]
    # -214 가 읽고 축 출처를 보정 문서까지 옮긴다
    assert load_world_alignment(out).record["axes_source"] == "MANUAL_FLOOR_POINTS"
    # 같은 입력이면 같은 변환(재현성)
    _, again, _ = _cli(tmp_path, sim, "--prepared", str(sim.prepared_dir), "--floor-points", str(pts),
                       "--camera-heights", str(heights), out="again.json")
    assert again["mapanything_to_world"] == rec["mapanything_to_world"]


def test_cli_one_wall_without_inputs_is_wall_and_floor(tmp_path):
    sim = make_run(tmp_path, drop_labels=(3, 4))
    code, rec, out = _cli(tmp_path, sim, "--frames-source", "SIM")
    assert code == cli.EXIT_OK, rec.get("detail")
    assert rec["axes_source"] == "WALL_AND_FLOOR" and rec["origin_source"] == "CAMERA_LAYOUT_ON_WALL"
    doc = rec["calibration"]
    assert doc["world_axes_source"] == "WALL_AND_FLOOR" and doc["scale_basis"] == "MODEL_ESTIMATE"
    assert check_calibration(doc) == [] and load_world_alignment(out).record["axes_source"] == "WALL_AND_FLOOR"


def test_cli_without_floor_points_keeps_walls_and_records_source(tmp_path):
    sim = make_run(tmp_path)
    heights = _write(tmp_path, "h.json", {"format": "simai.camera_heights", "heights_m": TRUE_HEIGHTS, "method": "줄자"})
    code, rec, out = _cli(tmp_path, sim, "--frames-source", "SIM", "--camera-heights", str(heights))
    assert code == cli.EXIT_OK
    assert rec["axes_source"] == "WALLS" and rec["calibration"]["world_axes_source"] == "WALLS"
    assert rec["calibration"]["scale_basis"] == "MEASURED_CAMERA_HEIGHT"
    assert rec["inputs"]["camera_heights"]["method"] == "줄자"
    assert load_world_alignment(out).record["axes_source"] == "WALLS"


def test_cli_refuses_bad_floor_point_and_height_inputs(tmp_path, capsys):
    pytest.importorskip("cv2")
    sim = make_run(tmp_path, masks=True)
    _with_prepared_k(sim)
    pts = _write(tmp_path, "floor.json", _spec(_pixels(sim, [P1, P2])))
    # 1) 입력 폴더 없이 두 점(픽셀 → 광선 규약을 모름)
    plain = make_run(tmp_path, name="plain")
    code, rec, _ = _cli(tmp_path, plain, "--frames-source", "SIM", "--floor-points", str(pts), out="a.json")
    assert code == cli.EXIT_CONFIG and rec is None and "--prepared" in capsys.readouterr().err
    # 2) 결과에 없는 카메라의 높이
    bad_h = _write(tmp_path, "bad_h.json", {"cam_A": 2.6, "cam_Z": 2.6})
    code, rec, _ = _cli(tmp_path, sim, "--prepared", str(sim.prepared_dir), "--camera-heights", str(bad_h), out="b.json")
    assert code == cli.EXIT_CONFIG and rec is None and "cam_Z" in capsys.readouterr().err
    # 3) 높이가 양수가 아님, 4) 픽셀 공간 이름이 틀림
    neg = _write(tmp_path, "neg.json", {"cam_A": 0})
    code, _, _ = _cli(tmp_path, sim, "--prepared", str(sim.prepared_dir), "--camera-heights", str(neg), out="c.json")
    assert code == cli.EXIT_CONFIG
    wrong = _write(tmp_path, "wrong.json", _spec(_pixels(sim, [P1, P2])) | {"pixel_space": "MODEL"})
    code, _, _ = _cli(tmp_path, sim, "--prepared", str(sim.prepared_dir), "--floor-points", str(wrong), out="d.json")
    assert code == cli.EXIT_CONFIG
    # 5) 무효한 두 점(너무 가까움)은 입력 오류가 아니라 고정 못 함(사유 기록)
    close = _write(tmp_path, "close.json", _spec(_pixels(sim, [P1, (3.5, 2.0, 0.0)])))
    code, rec, _ = _cli(tmp_path, sim, "--prepared", str(sim.prepared_dir), "--floor-points", str(close), out="e.json")
    assert code == cli.EXIT_NOT_FIXED and rec["reason"] == "FLOOR_POINTS_INVALID" and "calibration" not in rec


def test_version_store_records_world_axes_source(cameras, t0):
    from simai_ai.calibration import CalibrationEvidence, CalibrationVersionStore
    from simai_ai.calibration.document import build_calibration_doc

    store = CalibrationVersionStore(site_id="classroom-1")
    result = store.propose(provider_source="G1_AUTO", provider_version="g1", cameras=cameras,
                           evidence=CalibrationEvidence(), at_ms=t0, world_axes_source="MANUAL_FLOOR_POINTS")
    assert result.doc["world_axes_source"] == "MANUAL_FLOOR_POINTS" and check_calibration(result.doc) == []
    plain = CalibrationVersionStore().propose(provider_source="G1_AUTO", provider_version="g1", cameras=cameras,
                                              evidence=CalibrationEvidence(), at_ms=t0)
    assert "world_axes_source" not in plain.doc  # 모르면 싣지 않는다
    with pytest.raises(ValueError):
        build_calibration_doc(calibration_version="v", provider_source="G1_AUTO", provider_version="x", created_at=t0,
                              cameras=cameras, judgement=result.judgement, fallback_scale_basis="MODEL_ESTIMATE",
                              world_axes_source="CORNER")
