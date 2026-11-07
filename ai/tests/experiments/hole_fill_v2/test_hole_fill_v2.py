"""`experiments.hole_fill_v2` 핵심 기하·투영·인페인팅·위상 정제 단위 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

AI_ROOT = Path(__file__).resolve().parents[3]
if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))
if str(AI_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(AI_ROOT / "src"))

from experiments.hole_fill_v2.bake_glb import bake_plane_observed_texture  # noqa: E402
from experiments.hole_fill_v2.core import (  # noqa: E402
    SRC_FILLED,
    SRC_SEE_THROUGH,
    blend_atlas_at_hole_boundary,
    blend_unobserved_vertex_colors,
    compute_camera_image_quality_maps,
    compute_open_boundary_distances,
    compute_surface_source_summary,
    extract_and_bake_horizontal_furniture_planes,
    feather_observed_filled_boundary,
    filter_small_connected_components,
    inpaint_tone_plane,
    match_fill_to_boundary_lab,
    oklab_to_srgb,
    optimize_wall_camera_seam_weights,
    recolor_remaining_furniture_faces,
    select_camera_colors_with_outlier_removal,
    srgb_to_oklab,
    strip_planar_mesh_faces,
)
from simai_ai.scene import glb_enhance as ge  # noqa: E402
from simai_ai.scene import glb_fill as gf  # noqa: E402
from simai_ai.scene.glb_inpaint import InpaintConfig, classic_inpaint_plane  # noqa: E402


def _make_synthetic_cam_vis(
    cam_id: str,
    pos_world: tuple[float, float, float],
    img_rgb_u8: np.ndarray,
    depth_map: np.ndarray,
) -> gf.CamVis:
    """바닥(World Z=0, XY 평면)을 수직 아래(-Z_world)로 내려다보는 합성 카메라 및 깊이 그림자 맵 생성."""
    # OpenCV 카메라 좌표계:
    #   +X_cv = +X_world
    #   +Y_cv = -Y_world
    #   +Z_cv = -Z_world (카메라가 +Z_world 높이에서 아래쪽 -Z_world 바닥을 바라봄)
    R = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, -1.0, 0.0],
            [0.0, 0.0, -1.0],
        ],
        dtype=np.float64,
    )
    center = np.array(pos_world, dtype=np.float64)
    t = -R @ center
    H, W = img_rgb_u8.shape[:2]
    K = np.array(
        [
            [float(W), 0.0, W * 0.5],
            [0.0, float(H), H * 0.5],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    cam = ge.CheckCamera(
        camera_id=cam_id,
        K=K,
        R=R,
        t=t,
        image=np.ascontiguousarray(img_rgb_u8, dtype=np.uint8),
        valid=None,
    )
    return gf.CamVis(
        cam=cam,
        center=center,
        depth=np.ascontiguousarray(depth_map, dtype=np.float32),
        scale=1.0,
        seen_px=np.zeros(0, dtype=np.int64),
        cos=np.zeros(0, dtype=np.float64),
    )


def _make_synthetic_floor_spec(tex_h: int = 64, tex_w: int = 64) -> dict:
    """World X in [-0.5, 0.5], World Y in [-0.5, 0.5], World Z = 0.0 합성 바닥 받침 평면 명세."""
    ut, vt = np.meshgrid(
        -0.5 + (np.arange(tex_w) + 0.5) / tex_w,
        0.5 - (np.arange(tex_h) + 0.5) / tex_h,
    )
    P_tex_world = np.stack([ut, vt, np.zeros_like(ut)], axis=-1).reshape(-1, 3)
    return {
        "role": "floor",
        "is_floor": True,
        "width_m": 1.0,
        "height_m": 1.0,
        "area_m2": 1.0,
        "tex_hw": (tex_h, tex_w),
        "texel_m": 1.0 / tex_w,
        "normal_world": np.array([0.0, 0.0, 1.0], dtype=np.float64),
        "fallback_srgb": np.array([0.70, 0.70, 0.68], dtype=np.float64),
        "P_tex_world": P_tex_world,
    }


def test_depth_gate_rejects_foreground_occluder_and_empty_hole() -> None:
    """깊이 일치 게이트(5cm)가 전경 물체(의자/책상)와 TSDF 구멍(d0=0)에 사진을 잘못 박지 않는지 검증."""
    H, W = 64, 64
    # 바닥 실제 색: 회백색 (180, 178, 172)
    floor_u8 = np.array([180, 178, 172], dtype=np.uint8)
    # 전경 물체(책상 상판, z_cv = 1.2m) 색: 진한 빨강 (245, 20, 20)
    fg_u8 = np.array([245, 20, 20], dtype=np.uint8)
    # 구멍 뒤편 배경 색: 파랑 (20, 40, 245)
    hole_bg_u8 = np.array([20, 40, 245], dtype=np.uint8)

    img1 = np.broadcast_to(floor_u8, (H, W, 3)).copy()
    img2 = np.broadcast_to(floor_u8, (H, W, 3)).copy()

    # 카메라는 World Z = +2.0m 상공 -> 바닥(Z=0)까지의 실제 깊이는 z_cv = 2.0m
    depth1 = np.full((H, W), 2.0, dtype=np.float32)
    depth2 = np.full((H, W), 2.0, dtype=np.float32)

    # 우측 영역(x: 38..54, y: 18..46)에는 전경 물체(z_cv = 1.2m)가 바닥을 가림
    img1[18:46, 38:54] = fg_u8
    img2[18:46, 38:54] = fg_u8
    depth1[18:46, 38:54] = 1.2
    depth2[18:46, 38:54] = 1.2

    # 좌측 영역(x: 10..26, y: 18..46)은 TSDF 구멍(d0 = 0.0, 형상 없음)
    img1[18:46, 10:26] = hole_bg_u8
    img2[18:46, 10:26] = hole_bg_u8
    depth1[18:46, 10:26] = 0.0
    depth2[18:46, 10:26] = 0.0

    cv1 = _make_synthetic_cam_vis("cam_A", (0.0, 0.0, 2.0), img1, depth1)
    cv2_vis = _make_synthetic_cam_vis("cam_B", (0.03, 0.0, 2.0), img2, depth2)
    gains = np.ones((2, 3), dtype=np.float64)

    spec = _make_synthetic_floor_spec(H, W)
    baked = bake_plane_observed_texture(
        spec,
        [cv1, cv2_vis],
        gains,
        depth_tol_m=0.05,
    )

    obs_mask = baked["inpaint_mask"] == 0
    unobs_mask = baked["inpaint_mask"] > 0

    # 1. 관측된 바닥 픽셀에는 전경 빨간색(R-G = 225)이나 구멍 파란색(B-G = 205)이 유입되지 않아야 함
    #    (경계 1px bilinear 보간 최대 18 이하 vs 전경 내부 225)
    obs_rgb = baked["albedo"][obs_mask].astype(np.int32)
    assert len(obs_rgb) > 400
    assert int(np.max(obs_rgb[:, 0] - obs_rgb[:, 1])) < 25, "전경 빨간 물체 색이 바닥에 잘못 투영됨"
    assert int(np.max(obs_rgb[:, 2] - obs_rgb[:, 1])) < 25, "TSDF 구멍(d0=0) 영역이 바닥에 잘못 투영됨"

    # 2. 가려진 영역과 구멍 영역은 미관측(inpaint_mask == 255, source == SRC_GRAY)으로 남아야 함
    assert baked["unobserved_ratio"] > 0.15
    assert np.all(baked["source"][obs_mask] == gf.SRC_PROJECTED)
    assert np.all(baked["source"][unobs_mask] == gf.SRC_GRAY)


def test_tone_and_classic_fill_all_holes_and_record_surface_source() -> None:
    """`tone` 과 `classic` 두 방식이 구멍 픽셀을 모두 채우고 `surface_source` 에 관측/채움 구분을 남기는지 검증."""
    H, W = 64, 64
    floor_u8 = np.array([165, 170, 176], dtype=np.uint8)
    img = np.broadcast_to(floor_u8, (H, W, 3)).copy()
    depth = np.full((H, W), 2.0, dtype=np.float32)
    # 중앙 구멍 1개 생성 (d0 = 0.0)
    depth[20:44, 20:44] = 0.0

    cv1 = _make_synthetic_cam_vis("cam_A", (0.0, 0.0, 2.0), img, depth)
    cv2_vis = _make_synthetic_cam_vis("cam_B", (0.02, 0.0, 2.0), img, depth)
    spec = _make_synthetic_floor_spec(H, W)
    baked = bake_plane_observed_texture(spec, [cv1, cv2_vis], np.ones((2, 3)), depth_tol_m=0.05)

    obs_albedo = baked["albedo"]
    inpaint_mask = baked["inpaint_mask"]
    src_init = baked["source"]
    hole_mask = inpaint_mask > 0
    assert int(hole_mask.sum()) > 100

    # (a) tone 변형 (격자 없음 & 격자 있음)
    raw_tone = inpaint_tone_plane(obs_albedo, inpaint_mask, src_init, texel_m=spec["texel_m"], with_grid=False)
    fin_tone, src_tone = feather_observed_filled_boundary(
        obs_albedo, raw_tone, inpaint_mask, feather_m=0.04, texel_m=spec["texel_m"]
    )
    assert fin_tone.shape == (H, W, 3)
    # 관측 픽셀은 바이트 단위로 그대로 보존되어야 함 (규칙 2)
    np.testing.assert_array_equal(fin_tone[~hole_mask], obs_albedo[~hole_mask])
    # 구멍 픽셀이 모두 자연스러운 밝기로 채워져야 함
    assert int(fin_tone[hole_mask].min()) > 120
    # surface_source 에 관측(SRC_PROJECTED=3)과 채움(SRC_FILLED=4)이 엄격히 구분되어야 함
    assert np.all(src_tone[~hole_mask] == gf.SRC_PROJECTED)
    assert np.all(src_tone[hole_mask] == SRC_FILLED)

    summary_tone = compute_surface_source_summary(src_tone, "tone", spec["texel_m"])
    assert summary_tone["texel_counts"]["unobserved"] == 0
    assert summary_tone["texel_counts"]["observed_projection"] == int((~hole_mask).sum())
    assert summary_tone["texel_counts"]["filled_tone"] == int(hole_mask.sum())

    # (c) classic 변형 (가중치 파일 없이 동작하는 OpenCV Telea + 패치 합성)
    raw_classic, _ = classic_inpaint_plane(obs_albedo, inpaint_mask, src_init, config=InpaintConfig())
    fin_classic, src_classic = feather_observed_filled_boundary(
        obs_albedo, raw_classic, inpaint_mask, feather_m=0.04, texel_m=spec["texel_m"]
    )
    assert fin_classic.shape == (H, W, 3)
    np.testing.assert_array_equal(fin_classic[~hole_mask], obs_albedo[~hole_mask])
    assert int(fin_classic[hole_mask].min()) > 120
    assert np.all(src_classic[~hole_mask] == gf.SRC_PROJECTED)
    assert np.all(src_classic[hole_mask] == SRC_FILLED)


def test_floating_fragment_removal_preserves_main_mesh() -> None:
    """떠다니는 작은 조각만 제거되고 본 메시(방 구조물)는 100% 보존되는지 검증."""
    nx, ny = 15, 15  # 14 * 14 * 2 = 392 faces, 면적 4.0 m^2
    xs = np.linspace(0.0, 2.0, nx)
    ys = np.linspace(0.0, 2.0, ny)
    xv, yv = np.meshgrid(xs, ys, indexing="xy")
    V_main = np.stack([xv.ravel(), yv.ravel(), np.zeros(nx * ny)], axis=1)
    faces_main = []
    for iy in range(ny - 1):
        for ix in range(nx - 1):
            i00 = iy * nx + ix
            i10 = i00 + 1
            i01 = (iy + 1) * nx + ix
            i11 = i01 + 1
            faces_main.append([i00, i10, i11])
            faces_main.append([i00, i11, i01])
    F_main = np.array(faces_main, dtype=np.int64)

    # 공중에 떠 있는 작은 파편: 삼각형 2개 (면적 0.01 m^2)
    V_frag = np.array(
        [
            [0.5, 0.5, 1.5],
            [0.6, 0.5, 1.5],
            [0.5, 0.6, 1.5],
            [0.6, 0.6, 1.5],
        ],
        dtype=np.float64,
    )
    F_frag = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int64) + len(V_main)

    V_all = np.vstack([V_main, V_frag])
    F_all = np.vstack([F_main, F_frag])

    keep_face, report = filter_small_connected_components(
        V_all,
        F_all,
        min_faces=300,
        min_area_m2=0.25,
    )
    assert report["total_components"] == 2
    assert report["removed_components"] == 1
    assert report["removed_faces"] == 2
    assert np.all(keep_face[: len(F_main)])
    assert not np.any(keep_face[len(F_main) :])


def test_open_boundary_distances_and_boundary_blending() -> None:
    """열린 구멍 경계 거리 계산 및 아틀라스/정점색 소프트 블렌딩 검증."""
    # World XY 바닥 위 1m x 1m 사각형 (외곽 4개 모서리가 열린 경계, 중앙 정점 4번은 경계에서 0.5m 거리)
    V_world = np.array(
        [
            [0.0, 0.0, 0.02],
            [1.0, 0.0, 0.02],
            [1.0, 1.0, 0.02],
            [0.0, 1.0, 0.02],
            [0.5, 0.5, 0.02],
        ],
        dtype=np.float64,
    )
    F = np.array(
        [
            [0, 1, 4],
            [1, 2, 4],
            [2, 3, 4],
            [3, 0, 4],
        ],
        dtype=np.int64,
    )
    dists, stats = compute_open_boundary_distances(V_world, F, max_dist_m=0.12)
    assert stats["open_edges"] == 4
    np.testing.assert_allclose(dists[:4], 0.0, atol=1e-6)
    assert np.isclose(dists[4], 0.12)

    floor_info = {
        "corners_world": [
            [-0.5, -0.5, -0.015],
            [1.5, -0.5, -0.015],
            [1.5, 1.5, -0.015],
            [-0.5, 1.5, -0.015],
        ]
    }
    floor_tex = np.full((32, 32, 3), 200, dtype=np.uint8)

    # 1) 미관측 정점색(COLOR_0) 경계 블렌딩
    col_lin = np.full((5, 3), 0.05, dtype=np.float64)
    blended_lin, n_mod_v = blend_unobserved_vertex_colors(
        V_world,
        F,
        col_lin,
        dists,
        floor_info,
        floor_tex,
        [],
        [],
        feather_band_m=0.08,
    )
    assert n_mod_v >= 4
    assert float(blended_lin[0, 0]) > float(col_lin[0, 0])

    # 2) UV 아틀라스 경계 텍셀 블렌딩
    uv = np.array(
        [
            [0.1, 0.1],
            [0.9, 0.1],
            [0.9, 0.9],
            [0.1, 0.9],
            [0.5, 0.5],
        ],
        dtype=np.float64,
    )
    atlas = np.full((32, 32, 3), 40, dtype=np.uint8)
    blended_atlas, n_mod_px = blend_atlas_at_hole_boundary(
        atlas,
        V_world,
        F,
        uv,
        dists,
        floor_info,
        floor_tex,
        [],
        [],
        feather_band_m=0.08,
    )
    assert n_mod_px > 0
    assert int(blended_atlas.max()) > 40


def test_oklab_roundtrip_and_match_fill_to_boundary_lab() -> None:
    """2차 지시 2-(b): Oklab 정·역변환 정확도 및 채움 영역 경계 Lab 평균·표준편차 보정 검증."""
    rng = np.random.default_rng(7)
    srgb_samples = rng.uniform(0.05, 0.95, size=(128, 3))
    lab = srgb_to_oklab(srgb_samples)
    recovered = oklab_to_srgb(lab)
    np.testing.assert_allclose(recovered, srgb_samples, atol=1e-5)

    # 관측 영역은 따뜻한 우드월 톤(평균 ~ [195, 170, 135]), 채움 영역은 초기 편차가 있는 어두운 톤(~ [145, 125, 100])
    H, W = 64, 64
    obs_albedo = np.broadcast_to(np.array([195, 170, 135], dtype=np.uint8), (H, W, 3)).copy()
    filled_albedo = obs_albedo.copy()
    inpaint_mask = np.zeros((H, W), dtype=np.uint8)
    inpaint_mask[18:46, 18:46] = 255
    filled_albedo[18:46, 18:46] = np.array([145, 125, 100], dtype=np.uint8)

    corrected, meta = match_fill_to_boundary_lab(
        obs_albedo,
        filled_albedo,
        inpaint_mask,
        band_m=0.12,
        texel_m=0.01,
    )
    assert meta["applied"] is True
    # 관측 픽셀은 바이트 단위로 불변이어야 함
    np.testing.assert_array_equal(corrected[inpaint_mask == 0], obs_albedo[inpaint_mask == 0])
    # 보정 뒤 채움 영역의 평균 RGB가 관측 우드월 톤([195, 170, 135])과 3 이내로 일치해야 함
    fill_mean_after = corrected[inpaint_mask > 0].mean(axis=0)
    np.testing.assert_allclose(fill_mean_after, [195.0, 170.0, 135.0], atol=3.0)


def test_strip_planar_mesh_faces_strips_floor_and_wall_preserves_furniture() -> None:
    """2차 지시 1번: 바닥·벽 받침 평면 위 메시(거리 <= 6cm, 법선 <= 25도)는 걷어내고 3D 가구는 보존하는지 검증."""
    # 1) 바닥 메시 (z = 0.02m, 40개 삼각형)
    xs = np.linspace(0.5, 2.5, 6)
    ys = np.linspace(0.5, 2.5, 5)
    xv, yv = np.meshgrid(xs, ys, indexing="xy")
    V_floor = np.stack([xv.ravel(), yv.ravel(), np.full(xv.size, 0.02)], axis=1)
    F_floor_list = []
    for j in range(4):
        for i in range(5):
            v00 = j * 6 + i
            F_floor_list.append([v00, v00 + 1, v00 + 7])
            F_floor_list.append([v00, v00 + 7, v00 + 6])
    F_floor = np.array(F_floor_list, dtype=np.int64)

    # 2) 벽 메시 (x = 0.01m, u=y in [1.0, 3.0], z in [0.2, 2.0], 40개 삼각형)
    uy = np.linspace(1.0, 3.0, 6)
    uz = np.linspace(0.2, 2.0, 5)
    ug, zg = np.meshgrid(uy, uz, indexing="xy")
    V_wall = np.stack([np.full(ug.size, 0.01), ug.ravel(), zg.ravel()], axis=1)
    F_wall = F_floor + len(V_floor)

    # 3) 3D 가구 메시 (x in [1.0, 2.0], y in [1.0, 2.0], z = 0.75m 상판, 200개 삼각형, 면적 1.0 m^2)
    fx = np.linspace(1.0, 2.0, 11)
    fy = np.linspace(1.0, 2.0, 11)
    fxg, fyg = np.meshgrid(fx, fy, indexing="xy")
    V_furn = np.stack([fxg.ravel(), fyg.ravel(), np.full(fxg.size, 0.75)], axis=1)
    F_furn_list = []
    base = len(V_floor) + len(V_wall)
    for j in range(10):
        for i in range(10):
            v00 = base + j * 11 + i
            F_furn_list.append([v00, v00 + 1, v00 + 12])
            F_furn_list.append([v00, v00 + 12, v00 + 11])
    F_furn = np.array(F_furn_list, dtype=np.int64)

    V_all = np.vstack([V_floor, V_wall, V_furn])
    F_all = np.vstack([F_floor, F_wall, F_furn])
    keep_in = np.ones(len(F_all), dtype=bool)

    floor_info = {"mesh_floor_median_z_world_m": 0.02}
    wall_infos = [
        {
            "wall_id": "wall_0",
            "n_world_xy": [1.0, 0.0],
            "t_world_xy": [0.0, 1.0],
            "d_world_m": 0.0,
            "u_range_m": [0.5, 3.5],
            "z_range_m": [0.0, 2.5],
        }
    ]

    keep_out, rep = strip_planar_mesh_faces(
        V_all,
        F_all,
        keep_in,
        floor_info,
        wall_infos,
        dist_tol_m=0.06,
        angle_tol_deg=25.0,
        post_min_faces=50,
        post_min_area_m2=0.10,
    )
    n_fl = len(F_floor)
    n_wl = len(F_wall)
    assert not np.any(keep_out[:n_fl]), "바닥 평면 위 삼각형은 모두 걷어내져야 함"
    assert not np.any(keep_out[n_fl : n_fl + n_wl]), "벽 평면 위 삼각형은 모두 걷어내져야 함"
    assert np.all(keep_out[n_fl + n_wl :]), "3D 가구 상판 삼각형은 100% 유지되어야 함"
    assert rep["per_plane"]["floor"]["removed_faces"] == n_fl
    assert rep["per_plane"]["wall_0"]["removed_faces"] == n_wl


def test_strip_planar_mesh_faces_only_strips_where_backing_observed_ge_90pct() -> None:
    """3차 지시 2번: 받침 평면이 observed_projection >= 90% 인 구간의 삼각형만 걷어내고 미관측(filled) 위 삼각형은 남기는지 검증."""
    # 벽 메시: u=y in [1.0, 3.0], z in [0.5, 1.5], x = 0.01 m
    # 왼쪽 절반 u in [1.0, 1.8] 은 받침 평면이 미관측(False, 왼쪽 끝 액자 상황), 오른쪽 절반 u in [2.2, 3.0] 은 관측(True)
    V_left = np.array(
        [
            [0.01, 1.1, 0.6],
            [0.01, 1.7, 0.6],
            [0.01, 1.7, 1.4],
            [0.01, 1.1, 1.4],
        ],
        dtype=np.float64,
    )
    F_left = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)

    V_right = np.array(
        [
            [0.01, 2.3, 0.6],
            [0.01, 2.9, 0.6],
            [0.01, 2.9, 1.4],
            [0.01, 2.3, 1.4],
        ],
        dtype=np.float64,
    )
    F_right = np.array([[4, 5, 6], [4, 6, 7]], dtype=np.int64)

    V_all = np.vstack([V_left, V_right])
    F_all = np.vstack([F_left, F_right])
    keep_in = np.ones(len(F_all), dtype=bool)

    floor_info = {
        "mesh_floor_median_z_world_m": 0.0,
        "corners_world": [[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [4.0, 4.0, 0.0], [0.0, 4.0, 0.0]],
    }
    wall_infos = [
        {
            "wall_id": "wall_0",
            "n_world_xy": [1.0, 0.0],
            "t_world_xy": [0.0, 1.0],
            "d_world_m": 0.0,
            "u_range_m": [1.0, 3.0],
            "z_range_m": [0.0, 2.0],
        }
    ]
    obs_mask_wall = np.zeros((64, 64), dtype=bool)
    obs_mask_wall[:, 32:] = True  # 우측 절반(u >= 2.0)만 observed_projection

    keep_out, rep = strip_planar_mesh_faces(
        V_all,
        F_all,
        keep_in,
        floor_info,
        wall_infos,
        dist_tol_m=0.06,
        angle_tol_deg=25.0,
        post_min_faces=50,
        post_min_area_m2=0.50,
        plane_obs_masks={"wall_0": obs_mask_wall},
        min_obs_coverage=0.90,
    )
    assert np.all(keep_out[:2]), "받침 평면이 미관측(filled)인 왼쪽 삼각형 2개는 고립 조각 제거에서도 보호되어 남아야 함"
    assert not np.any(keep_out[2:]), "받침 평면이 100% 관측(observed_projection)인 오른쪽 삼각형 2개는 걷어내져야 함"
    assert rep["kept_unobserved_backing_faces"] == 2
    assert rep["plane_stripped_faces"] == 2


def test_see_through_opening_and_sharp_camera_selection() -> None:
    """3차 지시 3번·추가: 단일 최우수 카메라 지배적 선택(sharp_exponent) 및 개구부(see_through) 보존 검증."""
    # 1) 두 카메라 후보(정면 가중치 0.90 vs 측면 가중치 0.35)에서 sharp_exponent=12 적용 시 정면 카메라 색이 99% 이상 지배
    cand_mask = np.array([[True], [True]], dtype=bool)
    cand_srgb = np.array([[[0.10, 0.35, 0.85]], [[0.20, 0.40, 0.75]]], dtype=np.float64)
    cand_w = np.array([[0.90], [0.35]], dtype=np.float64)
    nb_srgb = np.array([[0.12, 0.36, 0.82]], dtype=np.float64)

    soft_rgb, _, _ = select_camera_colors_with_outlier_removal(
        cand_mask, cand_srgb, cand_w, nb_srgb, is_floor=False, sharp_exponent=1.0
    )
    sharp_rgb, _, _ = select_camera_colors_with_outlier_removal(
        cand_mask, cand_srgb, cand_w, nb_srgb, is_floor=False, sharp_exponent=12.0
    )
    # sharp_exponent=12 일 때 정면 카메라(cam 0)와의 색 오차가 soft 평균보다 훨씬 작아야 함
    err_soft = float(np.linalg.norm(soft_rgb[0] - cand_srgb[0, 0]))
    err_sharp = float(np.linalg.norm(sharp_rgb[0] - cand_srgb[0, 0]))
    assert err_sharp < err_soft * 0.05

    # 2) see_through 마스크가 있을 때 채우지 않고 SRC_SEE_THROUGH(5) 출처로 기록되는지 검증
    H, W = 32, 32
    obs_albedo = np.full((H, W, 3), 180, dtype=np.uint8)
    fill_albedo = np.full((H, W, 3), 140, dtype=np.uint8)
    inpaint_mask = np.zeros((H, W), dtype=np.uint8)
    inpaint_mask[8:16, 8:16] = 255  # 일반 채움 대상 구멍
    see_through_mask = np.zeros((H, W), dtype=bool)
    see_through_mask[20:28, 20:28] = True  # 문·창문 개구부 (inpaint_mask 에서는 0)

    out_img, src_map = feather_observed_filled_boundary(
        obs_albedo,
        fill_albedo,
        inpaint_mask,
        feather_m=0.04,
        texel_m=0.005,
        see_through_mask=see_through_mask,
    )
    assert np.all(src_map[see_through_mask] == SRC_SEE_THROUGH)
    assert np.all(src_map[8:16, 8:16] == SRC_FILLED)
    summary = compute_surface_source_summary(src_map, "lama", 0.003)
    assert summary["texel_counts"]["see_through"] == 64
    assert summary["texel_counts"]["filled_lama"] == 64
    assert summary["source_codes"]["see_through"] == int(SRC_SEE_THROUGH)


def test_compute_camera_image_quality_maps_sharpness_and_exposure() -> None:
    """4차 지시 3번: 패치 단위 라플라시안 선명도와 과노출·저노출 포화 픽셀 비율이 사진 품질 가중치에 반영되는지 검증."""
    H, W = 64, 64
    img = np.full((H, W, 3), 140, dtype=np.uint8)
    # 좌상단(4..28, 4..28): 선명한 고주파 체커보드 패턴 (정상 노출 80 / 180)
    yy, xx = np.mgrid[4:28, 4:28]
    checker = ((yy + xx) % 2) == 0
    patch_sharp = np.where(checker[:, :, None], 180, 80).astype(np.uint8)
    img[4:28, 4:28] = patch_sharp

    # 우하단(36..60, 36..60): 과노출 포화 하이라이트 (254, 254, 254)
    img[36:60, 36:60] = 254

    qmaps = compute_camera_image_quality_maps(img, patch_size_px=9)
    w_sharp = qmaps["w_sharp_map"]
    w_exp = qmaps["w_exposure_map"]
    w_photo = qmaps["w_photo_map"]

    # 선명한 체커 패치 중심(16, 16)이 평탄한 영역(16, 48)보다 w_sharp 가 높아야 함
    assert float(w_sharp[16, 16]) > float(w_sharp[16, 48]) + 0.30
    # 과노출 포화 패치 중심(48, 48)은 정상 노출 패치(16, 16)보다 w_exposure 가 크게 낮아야 함
    assert float(w_exp[48, 48]) < float(w_exp[16, 16]) * 0.35
    assert float(w_photo[16, 16]) > float(w_photo[48, 48]) * 2.5


def test_optimize_wall_camera_seam_weights_avoids_high_gradient_sign() -> None:
    """3차 추가(ssafy_sign): 두 카메라 전환 이음새가 고주파 안내판 구간을 피하고 평탄한 벽 열에 놓여 단일 카메라로 하드 선택되는지 검증."""
    H, W = 48, 240
    N = H * W
    cand_mask = np.ones((2, N), dtype=bool)
    cand_srgb = np.full((2, N, 3), 0.85, dtype=np.float64)
    # 열 70..130 에 고주파 글자/안내판(카메라 0 과 카메라 1 사이 시차 오차 포함) 배치, 열 150..210 은 평탄한 흰 벽
    srgb_3d = cand_srgb.reshape(2, H, W, 3)
    yy, xx = np.mgrid[12:36, 70:130]
    stripes0 = ((xx % 4) < 2)[:, :, None]
    stripes1 = (((xx + 2) % 4) < 2)[:, :, None]  # 시차로 2픽셀 어긋난 두 번째 카메라 투영
    srgb_3d[0, 12:36, 70:130] = np.where(stripes0, 0.10, 0.92)
    srgb_3d[1, 12:36, 70:130] = np.where(stripes1, 0.10, 0.92)

    # 기본 기하 가중치는 열 100(안내판 한가운데)에서 교차하도록 설정
    x_grid = np.broadcast_to(np.linspace(0.0, 1.0, W)[None, :], (H, W))
    w0 = np.clip(1.0 - 0.8 * x_grid, 0.1, 1.0)
    w1 = np.clip(0.2 + 0.8 * x_grid, 0.1, 1.0)
    cand_w = np.stack([w0.ravel(), w1.ravel()], axis=0)

    new_w, meta = optimize_wall_camera_seam_weights(
        cand_mask, cand_srgb, cand_w, (H, W)
    )
    assert meta["seam_optimized"] is True
    seam_col = int(meta["seam_column"])
    # 이음새가 고주파 안내판 구간(70..130) 내부에 놓이지 않고 평탄한 벽면 바깥 열에 놓여야 함
    assert not (70 <= seam_col <= 130), f"seam_col={seam_col} 이 안내판 내부에 놓임"

    # 안내판 구간(열 70..130) 전체에서 단일 카메라(cam 0) 가중치가 지배적이어야 함
    w_3d = new_w.reshape(2, H, W)
    sign_ratio = float(np.mean(w_3d[0, 12:36, 70:130] > w_3d[1, 12:36, 70:130] * 100.0))
    assert sign_ratio == 1.0


def test_extract_and_bake_horizontal_furniture_planes_and_recolor_remaining() -> None:
    """5차 지시 2-(a),(b): 수평 가구 상판 평면 추출·단일 카메라 베이킹 및 남은 가구 면 다중 카메라 일관성 재선택 검증."""
    # 1) 원형 테이블 상판(z ≈ 0.73m, 1.0m x 1.0m 격자, 중앙에 TSDF 움푹 패임 2개 포함) + 수직 다리 측면 삼각형 4개
    nx, ny = 9, 9
    xs = np.linspace(-0.45, 0.45, nx)
    ys = np.linspace(-0.45, 0.45, ny)
    xv, yv = np.meshgrid(xs, ys, indexing="xy")
    zv = np.full(xv.size, 0.73, dtype=np.float64)
    # 중앙 정점 하나는 TSDF 오차로 3cm 아래로 패여 법선이 기울어진 상태
    center_idx = (ny // 2) * nx + (nx // 2)
    zv[center_idx] = 0.70
    V_top = np.stack([xv.ravel(), yv.ravel(), zv], axis=1)
    faces_top = []
    for iy in range(ny - 1):
        for ix in range(nx - 1):
            i00 = iy * nx + ix
            i10 = i00 + 1
            i01 = (iy + 1) * nx + ix
            i11 = i01 + 1
            faces_top.append([i00, i10, i11])
            faces_top.append([i00, i11, i01])
    F_top = np.array(faces_top, dtype=np.int64)

    # 상판 외곽(x = -0.45)에서 아래(z = 0.45m)로 내려가는 수직 측면(비평면 가구 면)
    base_v = len(V_top)
    V_side = np.array(
        [
            [-0.45, -0.20, 0.73],
            [-0.45, 0.20, 0.73],
            [-0.45, 0.20, 0.45],
            [-0.45, -0.20, 0.45],
        ],
        dtype=np.float64,
    )
    # 상판 외곽 정점(0, nx)과 공유하는 정점 2개를 직접 연결해 수밀 경계 보존을 함께 검증
    F_side = np.array(
        [
            [0, nx, base_v + 2],
            [0, base_v + 2, base_v + 3],
        ],
        dtype=np.int64,
    )
    V_all = np.vstack([V_top, V_side])
    F_all = np.vstack([F_top, F_side])
    keep_mask = np.ones(len(F_all), dtype=bool)

    H, W = 64, 64
    # 카메라 0(정면 상공 2.0m): 깨끗한 흰 상판(230, 230, 228) 관측
    img0 = np.full((H, W, 3), 230, dtype=np.uint8)
    depth0 = np.full((H, W), 2.0 - 0.73, dtype=np.float32)
    # 카메라 1(측면 상공): 파란 의자 등받이(35, 70, 210)가 일부 투영선에 끼어든 오염 카메라
    img1 = np.full((H, W, 3), 228, dtype=np.uint8)
    img1[20:44, 20:44] = np.array([35, 70, 210], dtype=np.uint8)
    depth1 = np.full((H, W), 2.0 - 0.73, dtype=np.float32)

    cv0 = _make_synthetic_cam_vis("cam_A", (0.0, 0.0, 2.0), img0, depth0)
    cv1 = _make_synthetic_cam_vis("cam_B", (0.05, 0.0, 2.0), img1, depth1)
    cams = [cv0.cam, cv1.cam]
    depths = [cv0.depth, cv1.depth]

    keep_after, fplanes, freport = extract_and_bake_horizontal_furniture_planes(
        V_all,
        F_all,
        keep_mask,
        cams,
        depths,
        min_area_m2=0.10,
        room_xy_bounds_m=(-1.0, 1.0, -1.0, 1.0),
    )
    assert freport["num_furniture_planes"] == 1
    # 상판 삼각형(내부 움푹 패인 중앙 삼각형 포함)은 모두 수평 가구 평면으로 추출되고 측면 2개만 남아야 함
    assert not np.any(keep_after[: len(F_top)])
    assert np.all(keep_after[len(F_top) :])
    # 로컬 평면 메시의 내부 정점은 평탄화되어 Z ≈ 0.73m 로 매끈해짐
    fp_v_local = fplanes[0]["V_world"]
    assert float(np.min(fp_v_local[:, 2])) > 0.718

    # 구워진 상판 텍스처에 파란 의자 색(B - R > 80)이 유입되지 않고 흰 상판 톤이 유지되어야 함
    fp_tex = fplanes[0]["image"]
    fp_foot = fplanes[0]["foot_mask"]
    obs_pixels = fp_tex[fp_foot].astype(np.int32)
    assert int(np.max(obs_pixels[:, 2] - obs_pixels[:, 0])) < 25
    assert float(obs_pixels.mean()) > 200.0

    # 2-(b) 남은 가구 면(측면/림 F_side)의 오염 색상/다중 카메라 일관성 재선택 검증
    V_test = V_all.copy()
    V_test[base_v + 2] = [-0.47, -0.3375, 0.71]
    V_test[base_v + 3] = [-0.47, -0.45, 0.71]
    recolored_srgb, is_furn, r_stats = recolor_remaining_furniture_faces(
        V_test,
        F_all,
        keep_after,
        cams,
        depths,
        furniture_planes=fplanes,
        room_xy_bounds_m=(-1.0, 1.0, -1.0, 1.0),
    )
    assert r_stats["furniture_faces"] == 2
    assert r_stats["observed_faces"] >= 1
    assert int(is_furn.sum()) == 2
    # 측면 가구 면이 흰 상판 테두리 톤(> 0.80)으로 일관되게 선택되어야 함
    assert float(recolored_srgb[is_furn].mean()) > 0.80





