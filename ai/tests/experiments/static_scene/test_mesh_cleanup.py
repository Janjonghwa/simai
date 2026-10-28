"""EXP-01 형상 정리(mesh_cleanup.py, S15P31A101-314): 작은 합성 방(SIM, 제품 성능 아님)으로 카메라 간 정렬·수평 평면 정리를 잰다.
numpy 만 쓴다(Open3D·OpenCV·Docker 없이 돈다).

반례: 카메라별 깊이 배율이 다르면 책상 판이 네 겹 → 정렬 뒤 한 겹, 자세가 조금 틀린 카메라(회전·이동)도 sim3 로 한 겹,
상한을 넘는 보정은 하지 않고 원래대로(모르면 비운다), 넓이가 작은 상자 윗면·둥근 덮개는 평면이 아니고 정점이 움직이지 않는다,
바닥 바로 위 몇 cm 의 어긋난 겹은 따로 평면으로 받지 않는다, 기운 판은 수평 평면이 아니다.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

AI = Path(__file__).resolve().parents[3]
STATIC = AI / "experiments" / "static_scene"


@pytest.fixture(scope="module")
def mc():
    spec = importlib.util.spec_from_file_location("static_scene_mesh_cleanup", STATIC / "mesh_cleanup.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m  # dataclass 가 모듈을 sys.modules 에서 찾는다
    spec.loader.exec_module(m)
    yield m
    sys.modules.pop(spec.name, None)


# --------------------------------------------------------------------------- 합성 방: 바닥·벽 네 면·책상 판, 모서리 카메라 네 대

# (축, 값, (다른 두 축 범위)) 사각형. 책상 판은 z = 0.7 의 1.2 × 0.8 m
RECTS = [
    (2, 0.0, ((-3, 3), (-3, 3))),
    (0, -3.0, ((-3, 3), (0, 3))), (0, 3.0, ((-3, 3), (0, 3))),
    (1, -3.0, ((-3, 3), (0, 3))), (1, 3.0, ((-3, 3), (0, 3))),
    (2, 0.7, ((-0.6, 0.6), (-0.4, 0.4))),
]
HW = (72, 96)
K = np.array([[70.0, 0, 47.5], [0, 70.0, 35.5], [0, 0, 1]])


@dataclass
class V:
    name: str
    depth: np.ndarray
    valid: np.ndarray
    K: np.ndarray
    cam2world: np.ndarray


def _look_at(c, target=(0, 0, 0.5)):
    c = np.asarray(c, float)
    z = np.asarray(target, float) - c
    z /= np.linalg.norm(z)
    x = np.cross(z, [0, 0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    T = np.eye(4)
    T[:3, :3] = np.column_stack([x, y, z])
    T[:3, 3] = c
    return T


def _render(T):
    """사각형들을 광선 투사한 z 깊이(OpenCV)."""
    h, w = HW
    v, u = np.mgrid[0:h, 0:w].astype(float)
    d = np.stack([u, v, np.ones_like(u)], -1) @ np.linalg.inv(K).T @ T[:3, :3].T  # z = 1 기준 방향(World)
    o = T[:3, 3]
    best = np.full((h, w), np.inf)
    for ax, val, rng in RECTS:
        others = [a for a in range(3) if a != ax]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (val - o[ax]) / d[..., ax]
        P = o + t[..., None] * d
        ok = np.isfinite(t) & (t > 1e-6)
        for a, (lo, hi) in zip(others, rng):
            ok &= (P[..., a] >= lo - 1e-9) & (P[..., a] <= hi + 1e-9)
        best = np.where(ok & (t < best), t, best)
    valid = np.isfinite(best)
    return np.where(valid, best, np.nan).astype(np.float32), valid


CENTERS = {"cam_A": (-2.5, -2.5, 2.4), "cam_B": (2.5, -2.5, 2.4), "cam_C": (2.5, 2.5, 2.4), "cam_D": (-2.5, 2.5, 2.4)}


def _views(scale=None, pose_err=None):
    """scale: 이름 → 깊이 배율(깊이 오차), pose_err: 이름 → (회전 벡터 rad, 중심 이동 m) — 깊이는 참 자세로 그리고 틀린 자세를 준다."""
    out = []
    for name, c in CENTERS.items():
        T = _look_at(c)
        depth, valid = _render(T)
        depth = depth * (scale or {}).get(name, 1.0)
        if pose_err and name in pose_err:
            w, t = pose_err[name]
            th = np.linalg.norm(w)
            k = np.asarray(w) / th
            Kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
            R = np.eye(3) + np.sin(th) * Kx + (1 - np.cos(th)) * Kx @ Kx
            T = T.copy()
            T[:3, :3] = R @ T[:3, :3]
            T[:3, 3] = T[:3, 3] + np.asarray(t)
        out.append(V(name, depth.astype(np.float32), valid, K.copy(), T))
    return out


def _plate_layers(mc, views):
    """카메라별 책상 판 점의 z 중앙값(판 위 영역, 위쪽 법선)."""
    med = {}
    for v in views:
        P, _, ok = mc.oriented_normals(v)
        X = P[ok]
        m = (np.abs(X[:, 0]) < 0.5) & (np.abs(X[:, 1]) < 0.3) & (X[:, 2] > 0.4) & (X[:, 2] < 1.0)
        if m.sum() >= 10:
            med[v.name] = float(np.median(X[m, 2]))
    return med


# --------------------------------------------------------------------------- 1. 카메라 간 정렬


def test_scale_errors_make_layers_and_alignment_merges_them(mc):
    views = _views(scale={"cam_B": 1.03, "cam_C": 0.97, "cam_D": 1.02})
    before = _plate_layers(mc, views)
    assert len(before) == 4 and max(before.values()) - min(before.values()) > 0.05  # 판이 5 cm 넘게 여러 겹
    res = mc.align_views(views, mc.AlignConfig(mode="scale", reference="cam_A", max_points=4000, min_pairs=100))
    after = _plate_layers(mc, res.views)
    assert max(after.values()) - min(after.values()) < 0.005
    tot = res.record["totals"]
    for name, f in (("cam_B", 1.03), ("cam_C", 0.97), ("cam_D", 1.02)):
        assert abs(tot[name]["scale"] * f - 1) < 2e-3  # 깊이 배율을 되돌렸다
        assert tot[name]["rot_deg"] == 0 and tot[name]["center_shift"] == 0  # scale 방식은 자세를 바꾸지 않는다
    assert res.record["status"] == {"cam_A": "REFERENCE", "cam_B": "OK", "cam_C": "OK", "cam_D": "OK"}
    assert res.record["converged"]
    # 원본 시점은 그대로(사본만 바뀐다)
    assert views[1].depth is not res.views[1].depth and np.allclose(views[1].cam2world, res.views[1].cam2world)


def test_sim3_fixes_small_pose_errors(mc):
    err = {"cam_B": (np.radians(0.6) * np.array([0.3, 0.8, 0.5]), (0.03, -0.02, 0.01)),
           "cam_C": (np.radians(0.5) * np.array([-0.6, 0.2, 0.77]), (-0.02, 0.03, 0.0))}
    views = _views(scale={"cam_D": 1.02}, pose_err=err)
    d0 = mc.cross_view_distances(views, max_points=4000)["all"]["point_to_plane_m"]["p50"]
    res = mc.align_views(views, mc.AlignConfig(mode="sim3", reference="cam_A", max_points=4000, min_pairs=100))
    d1 = mc.cross_view_distances(res.views, max_points=4000)["all"]["point_to_plane_m"]["p50"]
    assert d0 > 0.01 and d1 < 0.002
    after = _plate_layers(mc, res.views)
    assert max(after.values()) - min(after.values()) < 0.01
    assert all(s == "OK" for n, s in res.record["status"].items() if n != "cam_A")
    hist = res.record["history"]
    assert hist and hist[0]["median_abs_rel"] > hist[-1]["median_abs_rel"]


def test_correction_over_cap_is_not_applied(mc):
    views = _views(scale={"cam_B": 1.10})  # 10% 배율 차 > 상한 5%(대응 문턱은 넓혀 상한 판정까지 가게 한다)
    res = mc.align_views(views, mc.AlignConfig(mode="scale", reference="cam_A", max_points=4000, min_pairs=100, assoc_tol=0.15))
    assert res.record["status"]["cam_B"] == "CAPPED"
    np.testing.assert_array_equal(res.views[1].depth, views[1].depth)
    assert res.record["status"]["cam_C"] == "OK" and abs(res.record["totals"]["cam_C"]["scale"] - 1) < 1e-3  # 뺀 카메라가 끌지 않는다


# --------------------------------------------------------------------------- 2. 수평 평면


def _grid_mesh(x0, x1, y0, y1, z, n, rng=None, noise=0.0):
    xs, ys = np.linspace(x0, x1, n), np.linspace(y0, y1, n)
    X, Y = np.meshgrid(xs, ys)
    Z = np.full_like(X, z) + (rng.normal(0, noise, X.shape) if rng is not None else 0)
    Vv = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])
    idx = np.arange(n * n).reshape(n, n)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    return Vv, np.concatenate([np.column_stack([a, b, c]), np.column_stack([b, d, c])])


def _box(cx, cy, s, h, n=6):
    """바닥에 놓인 s × s × h 상자의 윗면(z = h) 격자와 옆면 하나(세운 격자)."""
    Vt, Ft = _grid_mesh(cx - s / 2, cx + s / 2, cy - s / 2, cy + s / 2, h, n)
    zs, xs = np.linspace(0, h, n), np.linspace(cx - s / 2, cx + s / 2, n)
    Z, X = np.meshgrid(zs, xs)
    Vs = np.column_stack([X.ravel(), np.full(X.size, cy - s / 2), Z.ravel()])
    idx = np.arange(n * n).reshape(n, n)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    Fs = np.concatenate([np.column_stack([a, b, c]), np.column_stack([b, d, c])])
    return np.concatenate([Vt, Vs]), np.concatenate([Ft, Fs + len(Vt)])


def _dome(cx, cy, r, n=25):
    """바닥 위 반구(r)."""
    xs = np.linspace(cx - r, cx + r, n)
    X, Y = np.meshgrid(xs, np.linspace(cy - r, cy + r, n))
    R2 = (X - cx) ** 2 + (Y - cy) ** 2
    Z = np.sqrt(np.clip(r * r - R2, 0, None))
    Vv = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])
    idx = np.arange(n * n).reshape(n, n)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    F = np.concatenate([np.column_stack([a, b, c]), np.column_stack([b, d, c])])
    keep = (R2.ravel()[F] < r * r * 0.98).all(1)
    return Vv, F[keep]


def _scene_mesh():
    rng = np.random.default_rng(0)
    parts = [_grid_mesh(-2, 2, -2, 2, 0.0, 81, rng, 0.006),  # 바닥(5 cm 격자, 잡음 σ 6 mm)
             _grid_mesh(-0.6, 0.6, -0.4, 0.4, 0.7, 25, rng, 0.006),  # 책상 판
             _box(1.2, 1.2, 0.2, 0.3),  # 윗면 0.04 m²(넓이 하한 0.1 m² 미만)
             _dome(-1.2, 1.2, 0.4),  # 둥근 덮개
             _grid_mesh(1.0, 1.6, -1.6, -1.0, 0.035, 13, rng, 0.002)]  # 바닥 위 3.5 cm 의 어긋난 겹(0.36 m²)
    Vs, Fs, off, tag = [], [], 0, []
    for k, (Vv, F) in enumerate(parts):
        Vs.append(Vv)
        Fs.append(F + off)
        tag.append(np.full(len(F), k))
        off += len(Vv)
    return np.concatenate(Vs), np.concatenate(Fs), np.concatenate(tag)


def _mesh_points(mc, Vv, F):
    C = Vv[F].mean(1)
    N = mc.face_normals(Vv, F)
    N *= np.sign(N[:, 2:3] + 1e-12)
    return C, N


def test_planes_found_only_for_wide_flat_horizontal_surfaces(mc):
    Vv, F, _ = _scene_mesh()
    C, N = _mesh_points(mc, Vv, F)
    planes, rec = mc.find_horizontal_planes(C, N)
    heights = sorted(round(p.record()["height_m_at_center"], 2) for p in planes)
    assert heights == [0.0, 0.7]  # 바닥·책상 판. 상자 윗면(작다)·덮개(굽음)·바닥 위 3.5 cm 겹(바닥의 겹)은 아니다
    assert any(r["reason"] == "LAYER_OF_ACCEPTED" for r in rec["rejected"])
    assert all(p.rms_m < 0.01 and p.n[2] > np.cos(np.radians(1)) for p in planes)


def test_tilted_board_is_not_a_horizontal_plane(mc):
    Vv, F = _grid_mesh(-0.6, 0.6, -0.4, 0.4, 0.0, 25)
    Vv[:, 2] = 0.7 + np.tan(np.radians(12)) * Vv[:, 0]  # 12° 기운 판(정점 법선은 후보 20° 안)
    C, N = _mesh_points(mc, Vv, F)
    planes, _ = mc.find_horizontal_planes(C, N)
    assert planes == [] or all(p.n[2] > np.cos(np.radians(5)) and p.area_m2 < 0.2 for p in planes)


def test_mesh_snap_flattens_planes_and_leaves_objects(mc):
    Vv, F, tag = _scene_mesh()
    C, N = _mesh_points(mc, Vv, F)
    cfg = mc.PlaneConfig()
    planes, _ = mc.find_horizontal_planes(C, N, cfg)
    M = np.eye(4)
    V2, is_plane, rec = mc.snap_mesh_to_planes(Vv, F, planes, M, cfg)
    floor, desk = tag == 0, tag == 1
    # 바닥·책상 판 정점은 평면에 정확히 올라간다(면 대부분이 평면 면)
    fl = np.unique(F[floor & is_plane])
    assert is_plane[floor].mean() > 0.95 and is_plane[desk].mean() > 0.95
    zf = [abs(planes[0].dist(V2[fl]) if planes[0].record()["height_m_at_center"] < 0.1 else planes[1].dist(V2[fl])).max()]
    assert zf[0] < 1e-9
    # 상자 윗면·옆면 위쪽, 덮개의 3 cm 위 정점은 움직이지 않는다(평면 아닌 물체)
    obj = np.unique(F[(tag == 2) | (tag == 3)])
    high = obj[Vv[obj, 2] > cfg.inlier_m + 1e-6]
    np.testing.assert_array_equal(V2[high], Vv[high])
    assert not is_plane[(tag == 2) & (Vv[F][:, :, 2].min(1) > 0.05)].any()
    assert not is_plane[(tag == 3) & (Vv[F][:, :, 2].min(1) > 0.05)].any()
    assert rec["plane_faces"] == int(is_plane.sum()) and rec["moved_m"]["p95"] <= cfg.inlier_m


def test_mesh_snap_respects_world_alignment(mc):
    """평면은 World 에서 찾고 메시 좌표(MapAnything)로 되돌린다: 닮음 변환 M 을 거쳐도 World 높이가 정확하다."""
    rng = np.random.default_rng(1)
    Vw, F = _grid_mesh(-1, 1, -1, 1, 0.7, 31, rng, 0.005)
    M = np.eye(4)
    ang = np.radians(30)
    M[:3, :3] = 1.3 * np.array([[1, 0, 0], [0, np.cos(ang), -np.sin(ang)], [0, np.sin(ang), np.cos(ang)]])
    M[:3, 3] = [0.2, -0.4, 1.0]
    Vm = (Vw - M[:3, 3]) @ np.linalg.inv(M[:3, :3]).T  # 메시 좌표
    C, N = _mesh_points(mc, Vw, F)
    planes, _ = mc.find_horizontal_planes(C, N)
    V2, is_plane, _ = mc.snap_mesh_to_planes(Vm, F, planes, M, mc.PlaneConfig())
    W2 = V2 @ M[:3, :3].T + M[:3, 3]
    assert is_plane.mean() > 0.95
    assert np.ptp(W2[np.unique(F[is_plane]), 2]) < 0.002  # 맞춘 평면(기울기 < 0.1°)


def test_depth_snap_moves_pixels_along_own_rays(mc):
    views = _views(scale={"cam_B": 1.006})  # 바닥 겹 약 1.5 cm(맞추기 거리 3 cm 안)
    Xs, Ns = [], []
    for v in views:
        P, N, ok = mc.oriented_normals(v)
        Xs.append(P[ok])
        Ns.append(N[ok])
    cfg = mc.PlaneConfig(min_area_m2=0.1)
    planes, _ = mc.find_horizontal_planes(np.concatenate(Xs), np.concatenate(Ns), cfg)
    assert any(abs(p.record()["height_m_at_center"] - 0.7) < 0.03 for p in planes)
    v2, rec = mc.snap_depth_to_planes(views[1], planes, np.eye(4), cfg)
    assert rec["snapped_ratio"] > 0.2
    P0 = mc.grid_points(views[1].depth, views[1].valid, K, views[1].cam2world)
    P1 = mc.grid_points(v2.depth, v2.valid, K, v2.cam2world)
    moved = np.nonzero(np.abs(v2.depth - views[1].depth) > 1e-6)
    c = views[1].cam2world[:3, 3]
    a, b = P0[moved] - c, P1[moved] - c
    cos = (a * b).sum(1) / np.linalg.norm(a, axis=1) / np.linalg.norm(b, axis=1)
    assert np.all(cos > 1 - 1e-9)  # 자기 광선 위에서만
    on = [np.abs(p.dist(P1[moved])) for p in planes]
    assert np.min(np.stack(on), axis=0).max() < 1e-6  # 움직인 점은 평면 위
    np.testing.assert_array_equal(v2.cam2world, views[1].cam2world)


# --------------------------------------------------------------------------- 3. 확인 지표


def test_region_layers_report_camera_offsets(mc):
    views = _views(scale={"cam_B": 1.03, "cam_C": 0.97})
    rec = mc.region_layers(views, np.eye(4), (-0.6, 0.6, -0.4, 0.4), z_min=0.3, z_max=1.2)
    desk = rec["planes"][0]
    assert abs(desk["height_m_at_center"] - 0.7) < 0.05
    assert desk["layer_spread_m"] > 0.05 and set(desk["camera_offset_m"]) == set(CENTERS)


def test_region_flatness_reads_rough_vs_flat(mc):
    rng = np.random.default_rng(2)
    Vf, F = _grid_mesh(-0.6, 0.6, -0.4, 0.4, 0.7, 25)
    Vr = Vf.copy()
    Vr[:, 2] += rng.normal(0, 0.015, len(Vr))
    flat = mc.region_flatness(Vf, F, (-1, 1, -1, 1))["summary"]
    rough = mc.region_flatness(Vr, F, (-1, 1, -1, 1))["summary"]
    assert flat["rms_m"] < 1e-6 and flat["within_1cm"] == 1.0 and flat["top_tilt_gt10_ratio"] == 0.0
    assert rough["rms_m"] > 0.006 and rough["top_tilt_gt10_ratio"] > 0.3  # 면 무게중심이라 σ/√3


def test_adjacent_furniture_at_close_heights_are_separate_planes(mc):
    """옆에 붙은 다른 높이의 윗면(0.40 m 의자 방석, 0.44 m 벤치)은 겹이 아니라 각각 평면이다(발자국이 겹치지 않는다)."""
    a = _grid_mesh(0.0, 0.8, 0.0, 0.8, 0.40, 17)
    b = _grid_mesh(0.8, 2.0, 0.0, 0.8, 0.44, 25)
    Vv = np.concatenate([a[0], b[0]])
    F = np.concatenate([a[1], b[1] + len(a[0])])
    C, N = _mesh_points(mc, Vv, F)
    planes, _ = mc.find_horizontal_planes(C, N)
    assert sorted(round(p.record()["height_m_at_center"], 2) for p in planes) == [0.4, 0.44]


def test_downward_surfaces_like_ceiling_are_not_planes(mc):
    """천장·덕트 아랫면(법선이 아래)은 맞추지 않는다: 위를 향한 면만 후보."""
    Vv, F = _grid_mesh(-1.5, 1.5, -1.5, 1.5, 2.5, 31)
    C, N = _mesh_points(mc, Vv, F)
    planes, rec = mc.find_horizontal_planes(C, -N)
    assert planes == [] and rec["candidates"] == 0
    planes_up, _ = mc.find_horizontal_planes(C, N)
    assert len(planes_up) == 1
