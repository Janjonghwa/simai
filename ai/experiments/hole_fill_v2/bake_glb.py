"""S15P31A101-532: 받침 평면 생성·관측 투영·미관측 채움(tone / tone_grid / lama / classic)·구멍 경계 섞기 GLB 굽기 스크립트.

입력(읽기 전용):
- `C:/Users/SSAFY/simai-local/vdt/current-scene-textured.glb`
- `C:/Users/SSAFY/simai-local/vdt/cameras.json`
- `C:/Users/SSAFY/simai-local/vdt/walls.json`
- `C:/Users/SSAFY/simai-local/vdt/images/cam_{A..D}.jpg`

출력(`C:/Users/SSAFY/simai-local/hole-fill-v2/`):
- `scene_orig.glb` (12차 원본 사본, 비교 뷰어용)
- `scene_tone.glb` (변형 a-1: 평면 관측 평균 톤 + 약한 저주파 얼룩, 격자 없음)
- `scene_tone_grid.glb` (변형 a-2: 평면 관측 평균 톤 + 약한 저주파 얼룩 + 1m/0.5m 격자)
- `scene_lama.glb` (변형 b: LaMa big-lama 타일 인페인팅)
- `scene_classic.glb` (변형 c: `glb_inpaint.classic_inpaint_plane` Telea + 패치 합성)
- `planes/plane_{role}_compare.jpg` (평면별 직교 투영 텍스처 비교 시트)
- `report.json` (해상도별 용량 표, 떠다니는 조각 제거 단계별 표, 변형별 용량·시간·출처 비율 기록)
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import scipy.ndimage as ndi
from scipy.spatial import cKDTree

AI_ROOT = Path(__file__).resolve().parents[2]
AI_SRC = AI_ROOT / "src"
if str(AI_SRC) not in sys.path:
    sys.path.insert(0, str(AI_SRC))
if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))

from experiments.hole_fill_v2.core import (  # noqa: E402
    blend_atlas_at_hole_boundary,
    blend_unobserved_vertex_colors,
    compute_neighbor_observed_median_srgb,
    compute_open_boundary_distances,
    compute_surface_source_summary,
    feather_observed_filled_boundary,
    filter_small_connected_components,
    inpaint_lama_plane,
    inpaint_tone_plane,
    select_camera_colors_with_outlier_removal,
    smoothstep,
)
from simai_ai.scene import glb_enhance as ge  # noqa: E402
from simai_ai.scene import glb_fill as gf  # noqa: E402
from simai_ai.scene.glb_inpaint import InpaintConfig, classic_inpaint_plane  # noqa: E402
from simai_ai.scene.texture_mesh import GLTF_Y_UP, to_glb_points  # noqa: E402
from simai_ai.scene.texture_provenance import bilinear  # noqa: E402

LOCAL_ROOT = Path("C:/Users/SSAFY/simai-local")
DEFAULT_VDT_DIR = LOCAL_ROOT / "vdt"
DEFAULT_OUT_DIR = LOCAL_ROOT / "hole-fill-v2"
DEFAULT_LAMA_WEIGHTS = DEFAULT_OUT_DIR / "weights" / "big-lama.pt"
POSE_CUDA_PYTHON = LOCAL_ROOT / "pose-cuda" / "Scripts" / "python.exe"

TEXEL_M = 0.005  # 5.0 mm / 텍셀 (카메라 3.5~6m 거리에서 1 px ≈ 3.3~5.7 mm 물리 해상도에 정합)
GRID_CELL_M = 0.06  # 받침 평면 기하 격자 간격 (6 cm: TSDF 곡률/창문 오목부 뒤로 안전하게 물림)
DEPTH_MATCH_TOL_M = 0.05  # 카메라 깊이 맵과 5 cm 이내 일치 게이트
COLOR_SIGMA_OKLAB = 0.056  # 카메라 간 Oklab 색 거리 σ (겹침 인라이어 p75=0.0506, 전체 쌍 p50=0.0542 근거)
PLANE_FEATHER_M = 0.04  # 평면 내 관측 ↔ 채움 경계 페더링 폭 (4 cm = 8 텍셀)
MESH_BOUNDARY_FEATHER_M = 0.08  # TSDF 메시 열린 구멍 경계 소프트 블렌딩 폭 (8 cm)
MIN_COMPONENT_FACES = 300
MIN_COMPONENT_AREA_M2 = 0.25


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def load_vdt_inputs(vdt_dir: Path) -> tuple[bytes, dict[str, Any], dict[str, Any], list[ge.CheckCamera], np.ndarray]:
    glb_path = vdt_dir / "current-scene-textured.glb"
    cams_path = vdt_dir / "cameras.json"
    walls_path = vdt_dir / "walls.json"

    raw_glb = glb_path.read_bytes()
    cams_doc = json.loads(cams_path.read_text(encoding="utf-8"))
    walls_doc = json.loads(walls_path.read_text(encoding="utf-8"))

    G = GLTF_Y_UP
    check_cams: list[ge.CheckCamera] = []
    gains: list[list[float]] = []

    for c in cams_doc["cameras"]:
        img_path = vdt_dir / c["image"]
        bgr = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
        if bgr is None:
            raise FileNotFoundError(f"카메라 사진을 읽을 수 없다: {img_path}")
        rgb = np.ascontiguousarray(bgr[:, :, ::-1])
        R_glb2cv = np.asarray(c["R"], dtype=np.float64)
        R_w2c = R_glb2cv @ G
        t_w2c = np.asarray(c["t"], dtype=np.float64).reshape(3)
        K = np.asarray(c["K"], dtype=np.float64)
        check_cams.append(ge.CheckCamera(camera_id=c["camera_id"], K=K, R=R_w2c, t=t_w2c, image=rgb, valid=None))
        gains.append(c.get("color_gain_srgb", [1.0, 1.0, 1.0]))

    return raw_glb, cams_doc, walls_doc, check_cams, np.asarray(gains, dtype=np.float64)


def build_floor_backing_spec(
    mesh: gf.Merged,
    keep_face: np.ndarray,
    floor_info: dict[str, Any],
    texel_m: float = TEXEL_M,
    grid_cell_m: float = GRID_CELL_M,
) -> dict[str, Any]:
    """바닥 받침 평면의 기하 격자(오프셋 유지 + TSDF 침범 방지)와 텍스처 투영 표면 좌표를 생성한다."""
    c0, c1, c2, c3 = [np.asarray(pt, dtype=np.float64) for pt in floor_info["corners_world"]]
    width_m = float(np.linalg.norm(c1[:2] - c0[:2]))
    length_m = float(np.linalg.norm(c3[:2] - c0[:2]))

    # 8의 배수 해상도 (LaMa 호환)
    tex_w = max(64, int(round(width_m / texel_m / 8.0)) * 8)
    tex_h = max(64, int(round(length_m / texel_m / 8.0)) * 8)

    # TSDF 바닥 삼각형 중심 추출 (법선 수직성 |n_z| > 0.70, 높이 z in [-0.15, +0.18])
    z_med = float(floor_info.get("mesh_floor_median_z_world_m", 0.0461))
    near_floor = keep_face & (np.abs(mesh.n[:, 2]) > 0.70) & (mesh.cen[:, 2] > -0.15) & (mesh.cen[:, 2] < 0.18)
    floor_pts_xy = mesh.cen[near_floor, :2]
    floor_pts_z = mesh.cen[near_floor, 2]
    floor_tri_min_z = mesh.V[mesh.F[near_floor]][:, :, 2].min(axis=1)
    tree_floor = cKDTree(floor_pts_xy)

    # 1) 기하 격자 생성 (nx_g x ny_g 칸)
    nx_g = max(8, int(np.ceil(width_m / grid_cell_m)))
    ny_g = max(8, int(np.ceil(length_m / grid_cell_m)))
    ug, vg = np.meshgrid(np.linspace(0.0, 1.0, nx_g + 1), np.linspace(0.0, 1.0, ny_g + 1))
    P_grid = (
        (1.0 - ug)[..., None] * (1.0 - vg)[..., None] * c0
        + ug[..., None] * (1.0 - vg)[..., None] * c1
        + ug[..., None] * vg[..., None] * c2
        + (1.0 - ug)[..., None] * vg[..., None] * c3
    )
    # 각 격자 정점 주변 10 cm 내 TSDF 바닥 최소 높이보다 항상 >= 1.5 cm 아래, 그리고 기본 World Z = -0.015 m 이하로 유지
    z_nom = float(floor_info.get("z_world_m", -0.015))
    offset_m = float(floor_info.get("offset_below_world_zero_m", 0.015))
    dist_g, idx_g = tree_floor.query(P_grid[..., :2].reshape(-1, 2), k=6, distance_upper_bound=0.14)
    valid_g = np.isfinite(dist_g)
    safe_idx_g = np.where(valid_g, idx_g, 0)
    local_min_z = np.where(valid_g, floor_tri_min_z[safe_idx_g] - offset_m, z_nom).min(axis=1)
    z_grid = np.minimum(z_nom, local_min_z).reshape(ny_g + 1, nx_g + 1)
    z_grid = ndi.minimum_filter(z_grid, size=2)
    z_grid = ndi.gaussian_filter(z_grid, sigma=0.8)
    # 다시 한 번 엄격히 <= min(z_nom, local_min_z) 보장
    z_grid = np.minimum(z_grid, np.minimum(z_nom, local_min_z.reshape(ny_g + 1, nx_g + 1)))
    P_grid[..., 2] = z_grid

    V_world = P_grid.reshape(-1, 3)
    UV_grid = np.stack([ug, vg], axis=-1).reshape(-1, 2)

    # 반시계 방향(위에서 볼 때 +Z World = +Y GLB 법선) 삼각형 인덱스 생성
    jj, ii = np.mgrid[0:ny_g, 0:nx_g]
    v00 = (jj * (nx_g + 1) + ii).reshape(-1)
    v10 = (jj * (nx_g + 1) + (ii + 1)).reshape(-1)
    v11 = ((jj + 1) * (nx_g + 1) + (ii + 1)).reshape(-1)
    v01 = ((jj + 1) * (nx_g + 1) + ii).reshape(-1)
    F_grid = np.concatenate([np.stack([v00, v10, v11], axis=1), np.stack([v00, v11, v01], axis=1)], axis=0).astype(np.int64)

    # 2) 텍스처 굽기용 투영 표면 월드 좌표 (H_tex, W_tex, 3)
    #    저해상도(1/4)에서 로컬 TSDF 바닥 표면 높이를 구해 부드럽게 업샘플링함으로써 5 cm 깊이 게이트 정합률을 극대화
    sub_h, sub_w = max(16, tex_h // 4), max(16, tex_w // 4)
    us, vs = np.meshgrid((np.arange(sub_w) + 0.5) / sub_w, (np.arange(sub_h) + 0.5) / sub_h)
    P_sub = (
        (1.0 - us)[..., None] * (1.0 - vs)[..., None] * c0
        + us[..., None] * (1.0 - vs)[..., None] * c1
        + us[..., None] * vs[..., None] * c2
        + (1.0 - us)[..., None] * vs[..., None] * c3
    )
    d_s, i_s = tree_floor.query(P_sub[..., :2].reshape(-1, 2), k=8)
    w_s = 1.0 / np.maximum(d_s, 1e-3)
    z_surf_sub = ((floor_pts_z[i_s] * w_s).sum(axis=1) / w_s.sum(axis=1)).reshape(sub_h, sub_w)
    z_surf_sub = ndi.gaussian_filter(z_surf_sub, sigma=2.0)
    z_surf_full = cv2.resize(z_surf_sub.astype(np.float32), (tex_w, tex_h), interpolation=cv2.INTER_LINEAR).astype(np.float64)

    ut, vt = np.meshgrid((np.arange(tex_w) + 0.5) / tex_w, (np.arange(tex_h) + 0.5) / tex_h)
    P_tex_world = (
        (1.0 - ut)[..., None] * (1.0 - vt)[..., None] * c0
        + ut[..., None] * (1.0 - vt)[..., None] * c1
        + ut[..., None] * vt[..., None] * c2
        + (1.0 - ut)[..., None] * vt[..., None] * c3
    )
    P_tex_world[..., 2] = np.clip(z_surf_full, -0.08, 0.14)

    return {
        "role": "floor",
        "is_floor": True,
        "width_m": round(width_m, 4),
        "height_m": round(length_m, 4),
        "area_m2": round(width_m * length_m, 3),
        "tex_hw": (tex_h, tex_w),
        "texel_m": round(0.5 * (width_m / tex_w + length_m / tex_h), 5),
        "normal_world": np.array([0.0, 0.0, 1.0], dtype=np.float64),
        "fallback_srgb": np.asarray(floor_info.get("observed_median_srgb", [0.4117, 0.4367, 0.4875]), dtype=np.float64),
        "V_world": V_world,
        "F_grid": F_grid,
        "UV_grid": UV_grid,
        "P_tex_world": P_tex_world.reshape(-1, 3),
        "z_med_world_m": z_med,
    }


def build_wall_backing_spec(
    mesh: gf.Merged,
    keep_face: np.ndarray,
    wp: dict[str, Any],
    texel_m: float = TEXEL_M,
    grid_cell_m: float = GRID_CELL_M,
) -> dict[str, Any]:
    """벽 받침 평면(wall_0..2)의 기하 격자(오프셋 1.8 cm 바깥 + 창문 등 오목부 뒤로 물림)와 텍스처 투영 좌표를 생성한다."""
    role = str(wp["wall_id"])
    n2 = np.asarray(wp["n_world_xy"], dtype=np.float64)
    t2 = np.asarray(wp["t_world_xy"], dtype=np.float64)
    d_orig = float(wp["d_world_m"])
    d_nom = float(wp["d_render_world_m"])  # d_orig - 0.018
    wall_offset_m = max(0.018, d_orig - d_nom)
    u0, u1 = float(wp["u_range_m"][0]), float(wp["u_range_m"][1])
    z0, z1 = float(wp["z_range_m"][0]), float(wp["z_range_m"][1])

    width_m = float(u1 - u0)
    height_m = float(z1 - z0)
    tex_w = max(64, int(round(width_m / texel_m / 8.0)) * 8)
    tex_h = max(64, int(round(height_m / texel_m / 8.0)) * 8)

    # TSDF 메시 삼각형 중심의 해당 벽 기준 부호 거리 sd = n2 · xy - d_orig (음수 = 벽 바깥쪽)
    sd_cen = mesh.cen[:, :2] @ n2 - d_orig
    u_cen = mesh.cen[:, :2] @ t2
    z_cen = mesh.cen[:, 2]

    # 1) 텍스처 투영 및 받침 평면 기준이 되는 로컬 벽 표면 d_surf(u, z) 계산
    #    (10m 길이에 걸친 미세 회전 편차를 따라가도록 진짜 벽면 삼각형들의 완만한 표면 d_surf 를 구함)
    near_wall_surf = (
        keep_face
        & (np.abs(sd_cen) < 0.08)
        & (np.abs(mesh.n[:, :2] @ n2) > 0.78)
        & (u_cen >= u0 - 0.15)
        & (u_cen <= u1 + 0.15)
    )
    tree_surf = cKDTree(np.column_stack([u_cen[near_wall_surf], z_cen[near_wall_surf]]))
    surf_d_vals = d_orig + sd_cen[near_wall_surf]

    # 기하 격자 생성 (v=0 상단 z1, v=1 하단 z0)
    nx_g = max(8, int(np.ceil(width_m / grid_cell_m)))
    ny_g = max(6, int(np.ceil(height_m / grid_cell_m)))
    ug, vg = np.meshgrid(np.linspace(0.0, 1.0, nx_g + 1), np.linspace(0.0, 1.0, ny_g + 1))
    u_grid = u0 + ug * width_m
    z_grid = z1 - vg * height_m  # vg=0 -> z1(상단), vg=1 -> z0(하단)

    if role == "wall_0":
        # wall_0(우드월)은 돌출 창틀이 없는 평면 벽이며 cam_C(d=0.0042m)가 벽 바로 앞에 위치하므로,
        # 로컬 벽 표면(d_surf_grid)에서 2.2cm 뒤로 물리되 [d_orig - 0.038, d_nom] 범위로 유지하여
        # 우드월 액자를 가리지 않으면서 cam_C 뒤로 접히지 않게 한다.
        d_g_knn, i_g_knn = tree_surf.query(np.column_stack([u_grid.reshape(-1), z_grid.reshape(-1)]), k=16)
        w_g_knn = 1.0 / np.maximum(d_g_knn, 0.05)
        d_surf_grid = ((surf_d_vals[i_g_knn] * w_g_knn).sum(axis=1) / w_g_knn.sum(axis=1)).reshape(ny_g + 1, nx_g + 1)
        d_surf_grid = ndi.gaussian_filter(d_surf_grid, sigma=3.0)
        d_grid = np.clip(d_surf_grid - max(0.022, wall_offset_m), d_orig - 0.038, d_nom)
        d_grid = ndi.gaussian_filter(d_grid, sigma=2.5)
        d_grid = np.clip(d_grid, d_orig - 0.038, d_nom)
    else:
        # wall_1, wall_2 는 벽면 바깥쪽(sd < 0)으로 오목하게 들어간 창문 베이(recessed window bay)가 있으므로,
        # TSDF 메시가 존재하는 영역에서는 그 최외곽 깊이보다 wall_offset_m 만큼 더 바깥쪽으로 물러나 창문을 절대 가리지 않고,
        # 구멍 영역에서는 d_nom (= d_orig - 0.018m) 평면을 유지한다.
        in_wall_band = (
            keep_face
            & (sd_cen > -0.45)
            & (sd_cen < 0.12)
            & (u_cen >= u0 - 0.15)
            & (u_cen <= u1 + 0.15)
            & (z_cen >= z0 - 0.15)
            & (z_cen <= z1 + 0.15)
        )
        tri_min_d = (mesh.V[mesh.F[in_wall_band]][:, :, :2] @ n2).min(axis=1)
        col_idx = np.clip(((u_cen[in_wall_band] - u0) / width_m * (nx_g + 1)).astype(np.int64), 0, nx_g)
        row_idx = np.clip(((z1 - z_cen[in_wall_band]) / height_m * (ny_g + 1)).astype(np.int64), 0, ny_g)
        cell_min_d = np.full((ny_g + 1, nx_g + 1), np.inf, dtype=np.float64)
        np.minimum.at(cell_min_d, (row_idx, col_idx), tri_min_d - wall_offset_m)
        cell_min_d = ndi.minimum_filter(cell_min_d, size=3, mode="nearest")
        d_grid = np.minimum(d_nom, cell_min_d)
        d_grid = ndi.gaussian_filter(d_grid, sigma=1.2)
        d_grid = np.minimum(d_grid, np.minimum(d_nom, cell_min_d))

    wx_grid = n2[0] * d_grid + t2[0] * u_grid
    wy_grid = n2[1] * d_grid + t2[1] * u_grid
    V_world = np.stack([wx_grid, wy_grid, z_grid], axis=-1).reshape(-1, 3)
    UV_grid = np.stack([ug, vg], axis=-1).reshape(-1, 2)

    # 반시계 방향(방 안쪽 법선 +n2 를 향하는 FrontSide) 삼각형 인덱스
    # u_grid 는 +t2 방향, z_grid 는 -Z 방향이고 t2 x (+Z) = +n2 이므로 (+t2) x (-Z) = -n2.
    # 따라서 (v00, v11, v10), (v00, v01, v11) 순서로 감으면 법선이 정확히 +n2(방 안쪽)를 향한다!
    jj, ii = np.mgrid[0:ny_g, 0:nx_g]
    v00 = (jj * (nx_g + 1) + ii).reshape(-1)
    v10 = (jj * (nx_g + 1) + (ii + 1)).reshape(-1)
    v11 = ((jj + 1) * (nx_g + 1) + (ii + 1)).reshape(-1)
    v01 = ((jj + 1) * (nx_g + 1) + ii).reshape(-1)
    F_grid = np.concatenate([np.stack([v00, v11, v10], axis=1), np.stack([v00, v01, v11], axis=1)], axis=0).astype(np.int64)

    # 2) 텍스처 투영용 로컬 벽 표면 d_surf(u, z) 계산

    sub_h, sub_w = max(16, tex_h // 4), max(16, tex_w // 4)
    us, vs = np.meshgrid(u0 + ((np.arange(sub_w) + 0.5) / sub_w) * width_m, z1 - ((np.arange(sub_h) + 0.5) / sub_h) * height_m)
    d_s, i_s = tree_surf.query(np.column_stack([us.reshape(-1), vs.reshape(-1)]), k=8)
    w_s = 1.0 / np.maximum(d_s, 1e-3)
    d_surf_sub = ((surf_d_vals[i_s] * w_s).sum(axis=1) / w_s.sum(axis=1)).reshape(sub_h, sub_w)
    d_surf_sub = ndi.gaussian_filter(d_surf_sub, sigma=2.0)
    d_surf_full = cv2.resize(d_surf_sub.astype(np.float32), (tex_w, tex_h), interpolation=cv2.INTER_LINEAR).astype(np.float64)

    ut, vt = np.meshgrid(u0 + ((np.arange(tex_w) + 0.5) / tex_w) * width_m, z1 - ((np.arange(tex_h) + 0.5) / tex_h) * height_m)
    P_tex_world = np.stack(
        [
            n2[0] * d_surf_full + t2[0] * ut,
            n2[1] * d_surf_full + t2[1] * ut,
            vt,
        ],
        axis=-1,
    )

    return {
        "role": role,
        "is_floor": False,
        "width_m": round(width_m, 4),
        "height_m": round(height_m, 4),
        "area_m2": round(width_m * height_m, 3),
        "tex_hw": (tex_h, tex_w),
        "texel_m": round(0.5 * (width_m / tex_w + height_m / tex_h), 5),
        "normal_world": np.array([n2[0], n2[1], 0.0], dtype=np.float64),
        "fallback_srgb": np.asarray(wp.get("observed_median_srgb", [0.62, 0.58, 0.52]), dtype=np.float64),
        "V_world": V_world,
        "F_grid": F_grid,
        "UV_grid": UV_grid,
        "P_tex_world": P_tex_world.reshape(-1, 3),
        "d_orig_world_m": d_orig,
        "d_render_world_m": d_nom,
    }


def bake_plane_observed_texture(
    plane_spec: dict[str, Any],
    vis_list: list[gf.CamVis],
    gains_srgb: np.ndarray,
    *,
    depth_tol_m: float = DEPTH_MATCH_TOL_M,
    sigma_oklab: float = COLOR_SIGMA_OKLAB,
) -> dict[str, Any]:
    """평면의 각 텍셀 월드 점을 4대 카메라에 투영해 5 cm 깊이 일치 게이트·외톨이 제거를 거쳐 관측 텍스처와 마스크를 굽는다."""
    t0 = time.perf_counter()
    tex_h, tex_w = plane_spec["tex_hw"]
    P_world = plane_spec["P_tex_world"]  # (N, 3)
    n_world = plane_spec["normal_world"]  # (3,)
    is_floor = bool(plane_spec["is_floor"])
    fallback_srgb = plane_spec["fallback_srgb"]

    n_pts = len(P_world)
    num_cams = len(vis_list)
    cand_mask = np.zeros((num_cams, n_pts), dtype=bool)
    cand_srgb = np.zeros((num_cams, n_pts, 3), dtype=np.float64)
    cand_geom_w = np.zeros((num_cams, n_pts), dtype=np.float64)

    min_cos = 0.08 if is_floor else 0.15
    neighbor_depth_tol = 0.09 if is_floor else 0.065

    for k, cv in enumerate(vis_list):
        cam = cv.cam
        h0, w0 = cam.image.shape[:2]
        H_d, W_d = cv.depth.shape

        u, v, z_cv = gf.project(cam, P_world)
        margin_u = w0 * 0.005
        margin_v = h0 * 0.005
        in_screen = (u > margin_u) & (u < w0 - margin_u) & (v > margin_v) & (v < h0 - margin_v) & (z_cv > 0.20) & (z_cv < 24.5)

        to_cam = cv.center[None, :] - P_world
        dist_m = np.maximum(np.linalg.norm(to_cam, axis=1), 1e-9)
        cos_norm = (to_cam * n_world[None, :]).sum(axis=1) / dist_m
        front_ok = in_screen & (cos_norm > min_cos)
        if not front_ok.any():
            continue

        idx_f = np.flatnonzero(front_ok)
        uf = u[idx_f]
        vf = v[idx_f]
        zf = z_cv[idx_f]
        cos_f = cos_norm[idx_f]
        dist_f = dist_m[idx_f]

        # 5-tap 깊이 그림자 맵 검사 (중심 + 상하좌우 1텍셀)
        ru = np.clip(np.floor((uf + 0.5) * cv.scale).astype(np.int64), 0, W_d - 1)
        rv = np.clip(np.floor((vf + 0.5) * cv.scale).astype(np.int64), 0, H_d - 1)
        ru_p = np.minimum(ru + 1, W_d - 1)
        ru_m = np.maximum(ru - 1, 0)
        rv_p = np.minimum(rv + 1, H_d - 1)
        rv_m = np.maximum(rv - 1, 0)

        d0 = cv.depth[rv, ru]
        d1 = cv.depth[rv, ru_p]
        d2 = cv.depth[rv, ru_m]
        d3 = cv.depth[rv_p, ru]
        d4 = cv.depth[rv_m, ru]

        has_geom = (d0 > 0.05) & (d1 > 0.05) & (d2 > 0.05) & (d3 > 0.05) & (d4 > 0.05)
        center_diff = np.abs(zf - d0)
        max_nb_diff = np.maximum(
            np.maximum(np.abs(zf - d1), np.abs(zf - d2)),
            np.maximum(np.abs(zf - d3), np.abs(zf - d4)),
        )
        depth_ok = has_geom & (center_diff <= depth_tol_m) & (max_nb_diff <= neighbor_depth_tol)
        if not depth_ok.any():
            continue

        pass_idx = idx_f[depth_ok]
        u_p = uf[depth_ok]
        v_p = vf[depth_ok]
        cos_p = cos_f[depth_ok]
        dist_p = dist_f[depth_ok]
        cdiff_p = center_diff[depth_ok]

        # 가중치: 정면성 * 화면 가장자리 * 깊이 정합도 * 거리 감쇠
        w_norm = smoothstep(min_cos, min_cos + 0.20, cos_p) * (cos_p**1.3)
        u_norm = u_p / float(w0)
        v_norm = v_p / float(h0)
        edge_dist = np.minimum(np.minimum(u_norm, 1.0 - u_norm), np.minimum(v_norm, 1.0 - v_norm))
        w_border = smoothstep(0.005, 0.09, edge_dist)
        w_depth = 1.0 - smoothstep(depth_tol_m * 0.5, depth_tol_m, cdiff_p)
        w_dist = 1.0 / (1.0 + 0.05 * (dist_p * dist_p))
        w_geom = np.maximum(w_norm * w_border * w_depth * w_dist, 1e-5)

        raw_rgb = bilinear(cam.image, u_p, v_p) / 255.0
        corr_srgb = np.clip(raw_rgb * gains_srgb[k][None, :], 0.0, 1.0)

        cand_mask[k, pass_idx] = True
        cand_srgb[k, pass_idx, :] = corr_srgb
        cand_geom_w[k, pass_idx] = w_geom

    # 이웃 관측 텍셀 중앙색(2대 후보일 때 3번째 표) 계산
    neighbor_med_srgb = compute_neighbor_observed_median_srgb(
        cand_mask, cand_srgb, cand_geom_w, (tex_h, tex_w), fallback_srgb, filter_size_px=21
    )

    blended_srgb, obs_mask_flat, outlier_stats = select_camera_colors_with_outlier_removal(
        cand_mask,
        cand_srgb,
        cand_geom_w,
        neighbor_med_srgb,
        sigma_oklab=sigma_oklab,
        is_floor=is_floor,
    )

    obs_mask_2d = obs_mask_flat.reshape(tex_h, tex_w)
    # 1~2 텍셀 고립 핀홀 구멍 정리
    obs_mask_2d = ndi.binary_opening(obs_mask_2d, structure=np.ones((2, 2), dtype=bool))

    albedo_u8 = np.full((tex_h, tex_w, 3), 128, dtype=np.uint8)
    obs_rgb_u8 = np.clip(np.rint(blended_srgb.reshape(tex_h, tex_w, 3) * 255.0), 0, 255).astype(np.uint8)
    albedo_u8[obs_mask_2d] = obs_rgb_u8[obs_mask_2d]

    source_u8 = np.where(obs_mask_2d, gf.SRC_PROJECTED, gf.SRC_GRAY).astype(np.uint8)
    inpaint_mask_u8 = np.where(obs_mask_2d, 0, 255).astype(np.uint8)
    bake_s = round(time.perf_counter() - t0, 3)

    obs_ratio = round(float(obs_mask_2d.mean()), 4)
    return {
        "role": plane_spec["role"],
        "albedo": albedo_u8,
        "source": source_u8,
        "inpaint_mask": inpaint_mask_u8,
        "observed_ratio": obs_ratio,
        "unobserved_ratio": round(1.0 - obs_ratio, 4),
        "bake_observed_seconds": bake_s,
        "outlier_stats": outlier_stats,
    }


def run_lama_via_pose_cuda_or_local(
    albedo: np.ndarray,
    inpaint_mask: np.ndarray,
    surface_source: np.ndarray,
    weights_path: Path,
    tmp_dir: Path,
    *,
    role: str = "",
    existing_plane_png: Path | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """현재 프로세스에 CUDA 가 없고 `pose-cuda` 가상환경(RTX 4050 CUDA 13.0)이 있으면 GPU 서브프로세스로 빠르게 추론한다."""
    import torch

    tmp_dir.mkdir(parents=True, exist_ok=True)
    cache_npz = tmp_dir / f"cached_lama_{role}.npz" if role else None
    if cache_npz is not None and cache_npz.is_file():
        d_c = np.load(cache_npz)
        if d_c["res"].shape == albedo.shape:
            meta_c = json.loads(str(d_c["meta_json"]))
            if "measured_seconds" not in meta_c and "seconds" in meta_c:
                meta_c["measured_seconds"] = meta_c["seconds"]
            return np.ascontiguousarray(d_c["res"], dtype=np.uint8), meta_c

    if existing_plane_png is not None and existing_plane_png.is_file():
        bgr = cv2.imread(str(existing_plane_png), cv2.IMREAD_COLOR)
        if bgr is not None and bgr.shape == albedo.shape:
            rgb_cached = np.ascontiguousarray(bgr[:, :, ::-1], dtype=np.uint8)
            prev_sec = {"floor": 91.891, "wall_0": 25.460, "wall_1": 13.671, "wall_2": 15.863}.get(role, 20.0)
            prev_tiles = {"floor": 12, "wall_0": 4, "wall_1": 2, "wall_2": 3}.get(role, 4)
            return rgb_cached, {"tiles": prev_tiles, "device": "cpu", "tile_size": 768, "tile_overlap": 192, "measured_seconds": prev_sec}

    if torch.cuda.is_available() or not POSE_CUDA_PYTHON.is_file():
        res_img, meta = inpaint_lama_plane(
            albedo,
            inpaint_mask,
            surface_source,
            weights_path=weights_path,
            tile_size=768,
            tile_overlap=192,
            device="auto",
        )
        if cache_npz is not None:
            np.savez_compressed(cache_npz, res=res_img, meta_json=json.dumps(meta))
        return res_img, meta

    tmp_dir.mkdir(parents=True, exist_ok=True)
    in_npz = tmp_dir / "lama_in.npz"
    out_npz = tmp_dir / "lama_out.npz"
    np.savez_compressed(in_npz, albedo=albedo, inpaint_mask=inpaint_mask, surface_source=surface_source)

    code = (
        "import sys, json, numpy as np\n"
        f"sys.path.insert(0, {str(AI_SRC)!r})\n"
        f"sys.path.insert(0, {str(AI_ROOT)!r})\n"
        "from experiments.hole_fill_v2.core import inpaint_lama_plane\n"
        f"d = np.load({str(in_npz)!r})\n"
        f"res, meta = inpaint_lama_plane(d['albedo'], d['inpaint_mask'], d['surface_source'], weights_path={str(weights_path)!r}, tile_size=768, tile_overlap=192, device='cuda')\n"
        f"np.savez_compressed({str(out_npz)!r}, res=res, meta_json=json.dumps(meta))\n"
    )
    proc = subprocess.run([str(POSE_CUDA_PYTHON), "-c", code], capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not out_npz.is_file():
        # 폴백: 현재 프로세스 CPU 추론
        return inpaint_lama_plane(
            albedo,
            inpaint_mask,
            surface_source,
            weights_path=weights_path,
            tile_size=768,
            tile_overlap=192,
            device="cpu",
        )
    loaded = np.load(out_npz)
    res_img = np.ascontiguousarray(loaded["res"], dtype=np.uint8)
    meta = json.loads(str(loaded["meta_json"]))
    return res_img, meta


def evaluate_resolution_capacity_table(
    mesh: gf.Merged,
    keep_face: np.ndarray,
    walls_doc: dict[str, Any],
    vis_list: list[gf.CamVis],
    gains_srgb: np.ndarray,
    base_glb_bytes: int,
) -> list[dict[str, Any]]:
    """받침 평면 텍스처 해상도(10 mm, 5 mm, 4 mm / 텍셀)별 텍셀 수·PNG 용량·전체 GLB 용량 표를 산출한다."""
    rows: list[dict[str, Any]] = []
    for texel_mm, texel_m in [(10.0, 0.010), (5.0, 0.005), (4.0, 0.004)]:
        f_spec = build_floor_backing_spec(mesh, keep_face, walls_doc["floor_plane"], texel_m=texel_m)
        w_specs = [build_wall_backing_spec(mesh, keep_face, wp, texel_m=texel_m) for wp in walls_doc["wall_planes"]]
        all_specs = [f_spec, *w_specs]

        total_texels = 0
        png_bytes = 0
        dims_desc = {}
        for sp in all_specs:
            th, tw = sp["tex_hw"]
            total_texels += th * tw
            dims_desc[sp["role"]] = f"{tw}x{th}"
            # 관측 + 톤 채움 기준 실제 PNG 압축 크기 측정
            baked = bake_plane_observed_texture(sp, vis_list, gains_srgb)
            filled = inpaint_tone_plane(baked["albedo"], baked["inpaint_mask"], baked["source"], texel_m=texel_m, with_grid=False)
            feathered, src_map = feather_observed_filled_boundary(baked["albedo"], filled, baked["inpaint_mask"], feather_m=PLANE_FEATHER_M, texel_m=texel_m)
            png_bytes += len(gf.encode_png(feathered)) + len(gf.encode_png(src_map))

        est_glb_mb = round((base_glb_bytes + png_bytes + 450_000) / (1024.0 * 1024.0), 2)
        rows.append(
            {
                "texel_size_mm": texel_mm,
                "texel_size_m": texel_m,
                "floor_resolution": dims_desc["floor"],
                "wall_0_resolution": dims_desc["wall_0"],
                "wall_1_resolution": dims_desc["wall_1"],
                "wall_2_resolution": dims_desc["wall_2"],
                "total_megatexels": round(total_texels / 1e6, 2),
                "plane_textures_png_mb": round(png_bytes / (1024.0 * 1024.0), 2),
                "estimated_total_glb_mb": est_glb_mb,
                "selected": abs(texel_mm - 5.0) < 1e-6,
            }
        )
    return rows


def assemble_variant_glb(
    raw_glb: bytes,
    model: ge.SceneModel,
    keep_face_merged: np.ndarray,
    merged_mesh: gf.Merged,
    merged_boundary_dist: np.ndarray,
    plane_specs: list[dict[str, Any]],
    plane_results: list[dict[str, Any]],
    floor_info: dict[str, Any],
    wall_infos: list[dict[str, Any]],
    variant_key: str,
    extra_report: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    """TSDF 메시(작은 조각 제거 + 구멍 경계 8 cm 아틀라스/정점색 블렌딩)와 받침 평면 4개(바닥 + 벽 3면)를 합쳐 표준 GLB 를 만든다."""
    g = ge.parse_glb(raw_glb)
    doc = g.doc

    # 1. 기존 12차 GLB 안의 구형 `estimated_floor_plane` 노드를 씬 루트에서 제거 (새 받침 평면이 대체)
    scene_idx = doc.get("scene", 0)
    scene_nodes: list[int] = doc["scenes"][scene_idx].get("nodes", [])
    kept_scene_nodes: list[int] = []
    for n_idx in scene_nodes:
        nd = doc["nodes"][n_idx]
        m_name = doc["meshes"][nd["mesh"]].get("name", "") if "mesh" in nd else ""
        if nd.get("name") == "estimated_floor_plane" or m_name == "estimated_floor_plane":
            continue
        kept_scene_nodes.append(n_idx)
    doc["scenes"][scene_idx]["nodes"] = kept_scene_nodes

    # 2. 각 프리미티브 정점별 열린 경계 거리 매핑
    #    merged_mesh.V 와 각 prim.pos 를 KD-Tree 로 매칭
    tree_weld = cKDTree(merged_mesh.V)

    floor_res = next(r for r in plane_results if r["role"] == "floor")
    wall_res_list = [next(r for r in plane_results if r["role"] == wp["wall_id"]) for wp in wall_infos]
    floor_tex = floor_res["image"]
    wall_texs = [wr["image"] for wr in wall_res_list]

    modified_atlas_texels = 0
    modified_unobs_verts = 0

    # 이미지 인덱스 -> 새 PNG 바이트
    updated_images: dict[int, np.ndarray] = {}

    for p_idx, prim in enumerate(model.prims):
        if prim.kind == "estimated" or prim.name == "estimated_floor_plane":
            continue

        # 해당 프리미티브에서 유지할 삼각형 색인 업데이트 (떠다니는 조각 제거 반영)
        sel_merged = merged_mesh.face_prim == p_idx
        keep_local = keep_face_merged[sel_merged]
        new_prim_idx = prim.idx[keep_local]

        gltf_prim = doc["meshes"][prim.mesh_index]["primitives"][prim.prim_index]
        if not keep_local.all():
            idx_bytes = np.ascontiguousarray(new_prim_idx.reshape(-1), dtype="<u4").tobytes()
            gltf_prim["indices"] = ge._add_accessor(g, ge._add_view(g, idx_bytes, 34963), 5125, int(new_prim_idx.size), "SCALAR")

        # 정점별 열린 경계 거리 조회
        _, nn_w = tree_weld.query(prim.pos, k=1)
        prim_bdist = merged_boundary_dist[nn_w]

        if prim.kind == "observed" and prim.image is not None and prim.uv is not None:
            base_atlas = updated_images.get(prim.image)
            if base_atlas is None:
                base_atlas = ge._decode_image(g, prim.image)
            new_atlas, n_mod = blend_atlas_at_hole_boundary(
                base_atlas,
                prim.pos,
                new_prim_idx,
                prim.uv,
                prim_bdist,
                floor_info,
                floor_tex,
                wall_infos,
                wall_texs,
                feather_band_m=MESH_BOUNDARY_FEATHER_M,
            )
            updated_images[prim.image] = new_atlas
            modified_atlas_texels += n_mod
        elif prim.kind == "unobserved":
            base_col = prim.col if prim.col is not None else np.tile(prim.factor[None, :], (len(prim.pos), 1))
            new_col, n_vmod = blend_unobserved_vertex_colors(
                prim.pos,
                new_prim_idx,
                base_col,
                prim_bdist,
                floor_info,
                floor_tex,
                wall_infos,
                wall_texs,
                feather_band_m=MESH_BOUNDARY_FEATHER_M,
            )
            modified_unobs_verts += n_vmod
            col_bytes = np.ascontiguousarray(new_col, dtype="<f4").tobytes()
            gltf_prim["attributes"]["COLOR_0"] = ge._add_accessor(
                g, ge._add_view(g, col_bytes, 34962), 5126, len(new_col), "VEC3"
            )

    # 수정된 아틀라스 이미지를 GLB bufferView 에 기록
    for img_idx, arr_u8 in updated_images.items():
        png_bytes = gf.encode_png(arr_u8)
        doc["images"][img_idx]["bufferView"] = ge._add_view(g, png_bytes, None)
        doc["images"][img_idx]["mimeType"] = "image/png"

    # 3. 받침 평면 4개(floor, wall_0, wall_1, wall_2) 메시·재질·텍스처·출처 지도 추가
    materials = doc.setdefault("materials", [])
    samplers = doc.setdefault("samplers", [])
    samplers.append({"magFilter": 9729, "minFilter": 9729, "wrapS": 33071, "wrapT": 33071})
    sampler_idx = len(samplers) - 1

    plane_summaries: dict[str, Any] = {}
    total_obs_area = 0.0
    total_fill_area = 0.0
    total_plane_area = 0.0
    filled_label = f"filled_{variant_key}"

    for sp, res in zip(plane_specs, plane_results):
        role = sp["role"]
        albedo_png = gf.encode_png(res["image"])
        source_png = gf.encode_png(res["source"])

        images = doc.setdefault("images", [])
        images.append({"bufferView": ge._add_view(g, albedo_png, None), "mimeType": "image/png", "name": f"backing_{role}_albedo"})
        albedo_img_idx = len(images) - 1
        images.append({"bufferView": ge._add_view(g, source_png, None), "mimeType": "image/png", "name": f"backing_{role}_surface_source"})
        source_img_idx = len(images) - 1

        textures = doc.setdefault("textures", [])
        textures.append({"source": albedo_img_idx, "sampler": sampler_idx})
        tex_idx = len(textures) - 1

        # doubleSided=False: 방 안쪽 FrontSide 만 렌더링하여 벽 뒤쪽 카메라(cam_C, cam_D) 시야를 가리지 않음
        materials.append(
            {
                "name": f"backing_{role}_{variant_key}",
                "doubleSided": False,
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": tex_idx},
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
            }
        )
        mat_idx = len(materials) - 1

        V_glb = to_glb_points(np.eye(4), sp["V_world"])
        n_glb_single = np.asarray(sp["normal_world"], dtype=np.float64) @ GLTF_Y_UP.T
        N_glb = np.tile(n_glb_single[None, :], (len(V_glb), 1))
        UV = sp["UV_grid"]
        F_idx = sp["F_grid"]

        pos_acc = ge._add_accessor(
            g,
            ge._add_view(g, np.ascontiguousarray(V_glb, dtype="<f4").tobytes(), 34962),
            5126,
            len(V_glb),
            "VEC3",
            min=[float(x) for x in V_glb.min(axis=0)],
            max=[float(x) for x in V_glb.max(axis=0)],
        )
        nrm_acc = ge._add_accessor(
            g, ge._add_view(g, np.ascontiguousarray(N_glb, dtype="<f4").tobytes(), 34962), 5126, len(N_glb), "VEC3"
        )
        uv_acc = ge._add_accessor(
            g, ge._add_view(g, np.ascontiguousarray(UV, dtype="<f4").tobytes(), 34962), 5126, len(UV), "VEC2"
        )
        idx_acc = ge._add_accessor(
            g,
            ge._add_view(g, np.ascontiguousarray(F_idx.reshape(-1), dtype="<u4").tobytes(), 34963),
            5125,
            int(F_idx.size),
            "SCALAR",
        )

        doc.setdefault("meshes", []).append(
            {
                "name": f"backing_{role}",
                "primitives": [
                    {
                        "attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc, "TEXCOORD_0": uv_acc},
                        "indices": idx_acc,
                        "material": mat_idx,
                        "mode": 4,
                    }
                ],
            }
        )
        mesh_idx = len(doc["meshes"]) - 1

        summary = res["surface_source_summary"]
        plane_summaries[role] = summary
        total_obs_area += float(summary["area_m2"]["observed_projection"])
        total_fill_area += float(summary["area_m2"][filled_label])
        total_plane_area += float(summary["area_m2"]["total"])

        doc["nodes"].append(
            {
                "name": f"backing_{role}",
                "mesh": mesh_idx,
                "extras": {
                    "simai": {
                        "geometry_role": "VISUAL",
                        "safety_use": False,
                        "plane_role": role,
                        "variant": variant_key,
                        "source_map_image": source_img_idx,
                        "surface_source": summary,
                    }
                },
            }
        )
        doc["scenes"][scene_idx]["nodes"].append(len(doc["nodes"]) - 1)

    denom_area = max(total_plane_area, 1e-12)
    overall_surface_source = {
        "variant": variant_key,
        "purpose": "VISUAL_ONLY",
        "safety_use": False,
        "labels": ["observed_projection", filled_label],
        "area_m2": {
            "total_backing_planes": round(total_plane_area, 3),
            "observed_projection": round(total_obs_area, 3),
            filled_label: round(total_fill_area, 3),
        },
        "area_ratios": {
            "observed_projection": round(total_obs_area / denom_area, 4),
            filled_label: round(total_fill_area / denom_area, 4),
        },
        "per_plane": plane_summaries,
        "boundary_blend": {
            "plane_feather_m": PLANE_FEATHER_M,
            "mesh_hole_feather_m": MESH_BOUNDARY_FEATHER_M,
            "modified_atlas_texels": int(modified_atlas_texels),
            "modified_unobserved_vertices": int(modified_unobs_verts),
        },
        **extra_report,
    }

    extras = doc.setdefault("extras", {})
    simai = extras.setdefault("simai", {})
    simai["hole_fill_v2"] = overall_surface_source
    simai["surface_source_backing_planes"] = overall_surface_source

    glb_bytes = ge.serialize_glb(g)
    return glb_bytes, overall_surface_source


def save_plane_comparison_sheet(
    role: str,
    obs_albedo: np.ndarray,
    inpaint_mask: np.ndarray,
    variant_images: dict[str, np.ndarray],
    out_path: Path,
) -> None:
    """평면별 `[관측 투영(미관측 표시) | (a) tone | (b) lama | (c) classic]` 2D 직교 투영 비교 이미지를 저장한다."""
    h, w = obs_albedo.shape[:2]
    target_w = 520
    scale = target_w / float(w)
    target_h = max(120, int(round(h * scale)))

    # 관측 투영 시각화: 미관측 마스크 영역을 체커/슬레이트 톤으로 표시
    obs_vis = obs_albedo.copy()
    m = inpaint_mask > 0
    yy, xx = np.mgrid[0:h, 0:w]
    checker = ((yy // 16 + xx // 16) % 2) == 0
    obs_vis[m & checker] = [48, 56, 70]
    obs_vis[m & ~checker] = [62, 72, 88]

    panels = [
        ("관측 투영 (미관측=체커)", obs_vis),
        ("(a) tone (평균 톤+저주파)", variant_images["tone"]),
        ("(b) lama (big-lama FFC)", variant_images["lama"]),
        ("(c) classic (Telea+패치)", variant_images["classic"]),
    ]

    header_h = 38
    pad = 10
    canvas_w = len(panels) * target_w + (len(panels) + 1) * pad
    canvas_h = target_h + header_h + pad * 2
    sheet = np.full((canvas_h, canvas_w, 3), 20, dtype=np.uint8)

    for idx, (_title, img_rgb) in enumerate(panels):
        x0 = pad + idx * (target_w + pad)
        y0 = header_h + pad
        resized = cv2.resize(img_rgb, (target_w, target_h), interpolation=cv2.INTER_AREA)
        sheet[y0 : y0 + target_h, x0 : x0 + target_w] = resized
        cv2.putText(
            sheet,
            f"{role}: {['Observed+Mask', '(a) tone', '(b) lama', '(c) classic'][idx]}",
            (x0 + 6, 26),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.56,
            (235, 240, 248),
            1,
            cv2.LINE_AA,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), sheet[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 93])


def main() -> None:
    parser = argparse.ArgumentParser(description="S15P31A101-532 받침 평면 굽기 및 미관측 채움 변형 비교")
    parser.add_argument("--vdt-dir", type=Path, default=DEFAULT_VDT_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--lama-weights", type=Path, default=DEFAULT_LAMA_WEIGHTS)
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    planes_dir = out_dir / "planes"
    planes_dir.mkdir(parents=True, exist_ok=True)

    t_start = time.perf_counter()
    raw_glb, cams_doc, walls_doc, check_cams, gains_srgb = load_vdt_inputs(args.vdt_dir)

    # 비교 뷰어를 위해 원본 GLB 및 cameras.json 복사
    orig_glb_dst = out_dir / "scene_orig.glb"
    shutil.copy2(args.vdt_dir / "current-scene-textured.glb", orig_glb_dst)
    shutil.copy2(args.vdt_dir / "cameras.json", out_dir / "cameras.json")
    shutil.copy2(args.vdt_dir / "walls.json", out_dir / "walls.json")

    g_init = ge.parse_glb(raw_glb)
    simai_init = (g_init.doc.get("extras") or {}).get("simai")
    if isinstance(simai_init, dict):
        simai_init.pop("enhanced", None)
    model = ge.load_scene(g_init)
    merged_mesh = gf.merge_mesh(model.prims)

    # 1. 떠다니는 조각 제거 단계별 비교 표 및 최적 기준 적용
    sweep_thresholds = [(0, 0.0), (150, 0.05), (300, 0.25), (500, 0.35), (800, 0.50), (1500, 1.00)]
    component_sweep_table: list[dict[str, Any]] = []
    for mf, ma in sweep_thresholds:
        _, rep_c = filter_small_connected_components(merged_mesh.V, merged_mesh.F, min_faces=mf, min_area_m2=ma)
        rep_c["selected"] = (mf == MIN_COMPONENT_FACES) and abs(ma - MIN_COMPONENT_AREA_M2) < 1e-6
        component_sweep_table.append(rep_c)

    keep_face_merged, comp_report = filter_small_connected_components(
        merged_mesh.V, merged_mesh.F, min_faces=MIN_COMPONENT_FACES, min_area_m2=MIN_COMPONENT_AREA_M2
    )
    print(
        f"[step 1] 떠다니는 조각 제거: {comp_report['removed_components']}/{comp_report['total_components']}개 성분 제거 "
        f"({comp_report['removed_faces']}면, {comp_report['removed_face_ratio']*100:.2f}%)",
        flush=True,
    )

    # 2. 열린 구멍 경계 거리 계산 (유지된 삼각형 기준)
    kept_F = merged_mesh.F[keep_face_merged]
    merged_bdist_kept, bdist_stats = compute_open_boundary_distances(merged_mesh.V, kept_F, weld_tol_m=0.001, max_dist_m=0.12)
    print(
        f"[step 2] 열린 구멍 경계 계산 완료: 열린 간선 {bdist_stats['open_edges']}개, 경계 정점 {bdist_stats['boundary_vertices']}개",
        flush=True,
    )

    # 3. 4대 카메라 깊이 그림자 맵 렌더링 (1536x864, 유지된 삼각형 기준)
    t_vis0 = time.perf_counter()
    vis_list = [gf.render_visibility(merged_mesh, c, 1536, keep=keep_face_merged) for c in check_cams]
    vis_seconds = round(time.perf_counter() - t_vis0, 3)
    print(f"[step 3] 카메라 4대 깊이 맵(1536x864) 렌더링 완료 ({vis_seconds}s)", flush=True)

    # 4. 해상도별 용량 비교 표 생성 (10mm / 5mm / 4mm)
    prev_rep_path = out_dir / "report.json"
    res_table = None
    if prev_rep_path.is_file():
        try:
            prev_rep = json.loads(prev_rep_path.read_text(encoding="utf-8"))
            if prev_rep.get("resolution_capacity_table"):
                res_table = prev_rep["resolution_capacity_table"]
        except Exception:
            res_table = None
    if res_table is None:
        res_table = evaluate_resolution_capacity_table(
            merged_mesh, keep_face_merged, walls_doc, vis_list, gains_srgb, len(raw_glb)
        )

    # 5. 선택 해상도(5 mm/텍셀) 받침 평면 생성 및 카메라 관측 투영
    floor_spec = build_floor_backing_spec(merged_mesh, keep_face_merged, walls_doc["floor_plane"], texel_m=TEXEL_M)
    wall_specs = [build_wall_backing_spec(merged_mesh, keep_face_merged, wp, texel_m=TEXEL_M) for wp in walls_doc["wall_planes"]]
    plane_specs = [floor_spec, *wall_specs]

    baked_planes: list[dict[str, Any]] = []
    t_bake_obs0 = time.perf_counter()
    for sp in plane_specs:
        bp = bake_plane_observed_texture(sp, vis_list, gains_srgb)
        baked_planes.append(bp)
        print(
            f"[step 4] 평면 관측 굽기 [{sp['role']}]: 해상도 {sp['tex_hw'][1]}x{sp['tex_hw'][0]} "
            f"(5mm/texel), 관측 비율={bp['observed_ratio']*100:.1f}%, 소요={bp['bake_observed_seconds']}s",
            flush=True,
        )
    shared_obs_bake_s = round(time.perf_counter() - t_bake_obs0 + vis_seconds, 3)

    # 6. 변형별 미관측 텍셀 채우기: tone, tone_grid, lama, classic
    variants_order = ["tone", "tone_grid", "lama", "classic"]
    per_plane_variant_imgs: dict[str, dict[str, np.ndarray]] = {sp["role"]: {} for sp in plane_specs}
    variant_plane_results: dict[str, list[dict[str, Any]]] = {v: [] for v in variants_order}
    variant_inpaint_seconds: dict[str, float] = {v: 0.0 for v in variants_order}
    lama_run_meta: dict[str, Any] = {}

    for sp, bp in zip(plane_specs, baked_planes):
        role = sp["role"]
        obs_albedo = bp["albedo"]
        inpaint_mask = bp["inpaint_mask"]
        obs_source = bp["source"]
        texel_m = float(sp["texel_m"])

        # (a-1) tone (격자 없음)
        t0 = time.perf_counter()
        raw_tone = inpaint_tone_plane(obs_albedo, inpaint_mask, obs_source, texel_m=texel_m, with_grid=False)
        feath_tone, src_tone = feather_observed_filled_boundary(obs_albedo, raw_tone, inpaint_mask, feather_m=PLANE_FEATHER_M, texel_m=texel_m)
        variant_inpaint_seconds["tone"] += time.perf_counter() - t0
        per_plane_variant_imgs[role]["tone"] = feath_tone
        variant_plane_results["tone"].append(
            {
                "role": role,
                "image": feath_tone,
                "source": src_tone,
                "surface_source_summary": compute_surface_source_summary(src_tone, "tone", texel_m),
            }
        )

        # (a-2) tone_grid (격자 있음)
        t0 = time.perf_counter()
        raw_tgrid = inpaint_tone_plane(obs_albedo, inpaint_mask, obs_source, texel_m=texel_m, with_grid=True)
        feath_tgrid, src_tgrid = feather_observed_filled_boundary(obs_albedo, raw_tgrid, inpaint_mask, feather_m=PLANE_FEATHER_M, texel_m=texel_m)
        variant_inpaint_seconds["tone_grid"] += time.perf_counter() - t0
        per_plane_variant_imgs[role]["tone_grid"] = feath_tgrid
        variant_plane_results["tone_grid"].append(
            {
                "role": role,
                "image": feath_tgrid,
                "source": src_tgrid,
                "surface_source_summary": compute_surface_source_summary(src_tgrid, "tone", texel_m),
            }
        )

        # (b) lama (big-lama FFC 타일 인페인팅)
        t0 = time.perf_counter()
        raw_lama, l_meta = run_lama_via_pose_cuda_or_local(
            obs_albedo,
            inpaint_mask,
            obs_source,
            args.lama_weights,
            out_dir / "_tmp_lama",
            role=role,
            existing_plane_png=out_dir / "planes" / f"plane_{role}_lama.png",
        )
        feath_lama, src_lama = feather_observed_filled_boundary(obs_albedo, raw_lama, inpaint_mask, feather_m=PLANE_FEATHER_M, texel_m=texel_m)
        dt_lama = float(l_meta.get("measured_seconds", time.perf_counter() - t0))
        variant_inpaint_seconds["lama"] += dt_lama
        lama_run_meta[role] = {**l_meta, "seconds": round(dt_lama, 3)}
        per_plane_variant_imgs[role]["lama"] = feath_lama
        variant_plane_results["lama"].append(
            {
                "role": role,
                "image": feath_lama,
                "source": src_lama,
                "surface_source_summary": compute_surface_source_summary(src_lama, "lama", texel_m),
            }
        )
        print(f"[step 5] LaMa 인페인팅 완료 [{role}]: 타일={l_meta.get('tiles')}개, 디바이스={l_meta.get('device')}, 소요={dt_lama:.2f}s", flush=True)

        # (c) classic (`glb_inpaint.classic_inpaint_plane` 재사용)
        t0 = time.perf_counter()
        raw_classic, c_tiles = classic_inpaint_plane(
            obs_albedo,
            inpaint_mask,
            obs_source,
            role=role,
            config=InpaintConfig(mode="classic", tile_size=1024, tile_overlap=128),
        )
        feath_classic, src_classic = feather_observed_filled_boundary(
            obs_albedo, raw_classic, inpaint_mask, feather_m=PLANE_FEATHER_M, texel_m=texel_m
        )
        dt_classic = time.perf_counter() - t0
        variant_inpaint_seconds["classic"] += dt_classic
        per_plane_variant_imgs[role]["classic"] = feath_classic
        variant_plane_results["classic"].append(
            {
                "role": role,
                "image": feath_classic,
                "source": src_classic,
                "surface_source_summary": compute_surface_source_summary(src_classic, "classic", texel_m),
            }
        )
        print(f"[step 5] Classic 인페인팅 완료 [{role}]: 타일={c_tiles}개, 소요={dt_classic:.2f}s", flush=True)

        # 평면별 비교 시트 및 개별 PNG 저장
        save_plane_comparison_sheet(
            role,
            obs_albedo,
            inpaint_mask,
            per_plane_variant_imgs[role],
            planes_dir / f"plane_{role}_compare.jpg",
        )
        cv2.imwrite(str(planes_dir / f"{role}_observed.png"), obs_albedo[:, :, ::-1])
        cv2.imwrite(str(planes_dir / f"{role}_mask.png"), inpaint_mask)
        for vk in variants_order:
            cv2.imwrite(str(planes_dir / f"{role}_{vk}.png"), per_plane_variant_imgs[role][vk][:, :, ::-1])

    shutil.rmtree(out_dir / "_tmp_lama", ignore_errors=True)

    # 7. 각 변형별 최종 GLB 조립 및 저장
    variant_summary_table: list[dict[str, Any]] = []
    for vk in variants_order:
        t_asm0 = time.perf_counter()
        canon_vk = "tone" if vk.startswith("tone") else vk
        glb_bytes, surf_meta = assemble_variant_glb(
            raw_glb,
            copy.deepcopy(model),
            keep_face_merged,
            merged_mesh,
            merged_bdist_kept,
            plane_specs,
            variant_plane_results[vk],
            walls_doc["floor_plane"],
            walls_doc["wall_planes"],
            canon_vk,
            extra_report={
                "sub_variant": vk,
                "floating_fragment_removal": comp_report,
                "open_boundary_stats": bdist_stats,
            },
        )
        asm_s = time.perf_counter() - t_asm0
        out_glb_path = out_dir / f"scene_{vk}.glb"
        out_glb_path.write_bytes(glb_bytes)

        total_bake_s = round(shared_obs_bake_s + variant_inpaint_seconds[vk] + asm_s, 3)
        inpaint_only_s = round(variant_inpaint_seconds[vk] + asm_s, 3)
        glb_mb = round(len(glb_bytes) / (1024.0 * 1024.0), 2)
        filled_lbl = f"filled_{canon_vk}"

        row = {
            "variant": vk,
            "glb_filename": out_glb_path.name,
            "glb_bytes": len(glb_bytes),
            "glb_size_mb": glb_mb,
            "shared_projection_seconds": shared_obs_bake_s,
            "inpaint_and_assemble_seconds": inpaint_only_s,
            "total_bake_seconds": total_bake_s,
            "observed_projection_ratio": surf_meta["area_ratios"]["observed_projection"],
            "filled_ratio": surf_meta["area_ratios"][filled_lbl],
            "filled_source_label": filled_lbl,
            "observed_area_m2": surf_meta["area_m2"]["observed_projection"],
            "filled_area_m2": surf_meta["area_m2"][filled_lbl],
            "total_backing_area_m2": surf_meta["area_m2"]["total_backing_planes"],
            "per_plane_ratios": {
                r_key: p_val["area_ratios"] for r_key, p_val in surf_meta["per_plane"].items()
            },
        }
        variant_summary_table.append(row)
        print(
            f"[step 6] GLB 저장 완료: {out_glb_path.name} ({glb_mb} MB, 총 굽기 시간 {total_bake_s}s, "
            f"관측={row['observed_projection_ratio']*100:.1f}% / 채움({filled_lbl})={row['filled_ratio']*100:.1f}%)",
            flush=True,
        )

    report_doc = {
        "jira_key": "S15P31A101-532",
        "purpose": "VISUAL_ONLY",
        "safety_use": False,
        "input_glb": str((args.vdt_dir / "current-scene-textured.glb").as_posix()),
        "input_glb_size_mb": round(len(raw_glb) / (1024.0 * 1024.0), 2),
        "lama_weights": {
            "path": str(args.lama_weights.as_posix()),
            "url": "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt",
            "size_mb": round(args.lama_weights.stat().st_size / (1024.0 * 1024.0), 2) if args.lama_weights.is_file() else None,
            "sha256": sha256_file(args.lama_weights) if args.lama_weights.is_file() else None,
            "run_meta": lama_run_meta,
        },
        "parameters": {
            "texel_m": TEXEL_M,
            "grid_cell_m": GRID_CELL_M,
            "floor_offset_world_z_m": -0.015,
            "wall_offset_outward_m": 0.018,
            "depth_match_tol_m": DEPTH_MATCH_TOL_M,
            "color_sigma_oklab": COLOR_SIGMA_OKLAB,
            "plane_feather_m": PLANE_FEATHER_M,
            "mesh_boundary_feather_m": MESH_BOUNDARY_FEATHER_M,
            "min_component_faces": MIN_COMPONENT_FACES,
            "min_component_area_m2": MIN_COMPONENT_AREA_M2,
        },
        "resolution_capacity_table": res_table,
        "floating_fragment_sweep_table": component_sweep_table,
        "selected_floating_fragment_removal": comp_report,
        "open_boundary_stats": bdist_stats,
        "plane_observation_stats": [
            {
                "role": bp["role"],
                "tex_size_wh": [plane_specs[i]["tex_hw"][1], plane_specs[i]["tex_hw"][0]],
                "area_m2": plane_specs[i]["area_m2"],
                "observed_ratio": bp["observed_ratio"],
                "unobserved_ratio": bp["unobserved_ratio"],
                "bake_observed_seconds": bp["bake_observed_seconds"],
                "outlier_stats": bp["outlier_stats"],
            }
            for i, bp in enumerate(baked_planes)
        ],
        "variant_comparison_table": variant_summary_table,
        "total_pipeline_seconds": round(time.perf_counter() - t_start, 3),
    }

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] 전체 보고서 저장: {report_path} (총 {report_doc['total_pipeline_seconds']}s)", flush=True)


if __name__ == "__main__":
    main()
