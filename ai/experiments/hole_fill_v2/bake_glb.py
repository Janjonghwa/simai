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
    SRC_SEE_THROUGH,
    blend_atlas_at_hole_boundary,
    blend_unobserved_vertex_colors,
    compute_camera_image_quality_maps,
    compute_neighbor_observed_median_srgb,
    compute_open_boundary_distances,
    compute_surface_source_summary,
    extract_and_bake_horizontal_furniture_planes,
    feather_observed_filled_boundary,
    filter_small_connected_components,
    inpaint_lama_plane,
    inpaint_tone_plane,
    match_fill_to_boundary_lab,
    optimize_wall_camera_seam_weights,
    recolor_remaining_furniture_faces,
    select_camera_colors_with_outlier_removal,
    smoothstep,
    srgb_to_oklab,
    strip_planar_mesh_faces,
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
DEFAULT_DA3_GLB = LOCAL_ROOT / "geometry-bench" / "v2_clean" / "v2_da3_clean" / "all" / "textured.glb"
DEFAULT_WM2_GLB = LOCAL_ROOT / "depth-537" / "work" / "wm2_pose_2cm_clean" / "all" / "textured.glb"
DEFAULT_FINAL_OUT_DIR = LOCAL_ROOT / "final-glb"
POSE_CUDA_PYTHON = LOCAL_ROOT / "pose-cuda" / "Scripts" / "python.exe"

TEXEL_M = 0.005  # 5.0 mm / 텍셀 (카메라 3.5~6m 거리에서 1 px ≈ 3.3~5.7 mm 물리 해상도에 정합)
GRID_CELL_M = 0.06  # 받침 평면 기하 격자 간격 (6 cm: TSDF 곡률/창문 오목부 뒤로 안전하게 물림)
DEPTH_MATCH_TOL_M = 0.05  # 카메라 깊이 맵과 5 cm 이내 일치 게이트
COLOR_SIGMA_OKLAB = 0.056  # 카메라 간 Oklab 색 거리 σ (겹침 인라이어 p75=0.0506, 전체 쌍 p50=0.0542 근거)
PLANE_FEATHER_M = 0.04  # 1차 평면 내 관측 ↔ 채움 경계 페더링 폭 (4 cm = 8 텍셀)
PLANE_FEATHER_R2_M = 0.10  # 2차 지시 2-(b) 평면 내 관측 ↔ 채움 경계 페더링 폭 (10 cm = 20 텍셀)
LAB_MATCH_BAND_M = 0.12  # 2차 지시 2-(b) 경계 근처 관측 텍셀 Lab 색 통계 맞춤 폭 (12 cm)
MESH_BOUNDARY_FEATHER_M = 0.08  # 1차 TSDF 메시 열린 구멍 경계 소프트 블렌딩 폭 (8 cm)
MESH_BOUNDARY_FEATHER_R2_M = 0.10  # 2차 남은 메시(평면 밖)와 평면 사이 경계 연결 폭 (10 cm)
PLANE_STRIP_DIST_M = 0.06  # 2차 지시 1번: 받침 평면에서 거리 <= 6 cm (RANSAC inlier 4 cm + TSDF 노이즈)
PLANE_STRIP_ANGLE_DEG = 25.0  # 2차 지시 1번: 받침 평면 법선과 각도 <= 25 deg
WALL_TEXEL_R3_M = 0.003  # 3차 지시 3번: 벽 받침 평면 해상도 3.0 mm/texel (고주파 글자·액자 선명도 확보)
MIN_OBS_COVERAGE_R3 = 0.90  # 3차 지시 2번: 삼각형 아래 받침 평면이 observed_projection >= 90% 일 때만 걷어냄
SHARP_EXPONENT_WALL_R3 = 12.0  # 3차 지시 3번: 정면·근접 단일 최우수 카메라 지배적 선택 지수 (벽)
SHARP_EXPONENT_FLOOR_R3 = 6.0  # 3차 지시 3번: 정면·근접 단일 최우수 카메라 지배적 선택 지수 (바닥)
SEE_THROUGH_TOL_M = 0.15  # 3차 추가: 평면 깊이보다 15 cm 이상 더 먼 깊이가 관측되면 개구부(see_through)로 판정
MIN_COMPONENT_FACES = 300
MIN_COMPONENT_AREA_M2 = 0.25


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def encode_png_any(img: np.ndarray) -> bytes:
    """(H, W), (H, W, 3), 또는 (H, W, 4) uint8 배열을 무손실 PNG 바이트로 인코딩한다."""
    a = np.ascontiguousarray(img, dtype=np.uint8)
    if a.ndim == 2 or (a.ndim == 3 and a.shape[2] == 3):
        return gf.encode_png(a)
    if a.ndim == 3 and a.shape[2] == 4:
        bgra = np.ascontiguousarray(a[:, :, [2, 1, 0, 3]])
        ok, buf = cv2.imencode(".png", bgra)
        if not ok:
            raise RuntimeError("RGBA PNG 인코딩 실패")
        return buf.tobytes()
    raise ValueError(f"지원하지 않는 이미지 차원: {a.shape}")


def encode_jpeg_rgb(img_rgb: np.ndarray, quality: int = 92) -> bytes:
    """(H, W, 3) uint8 RGB 배열을 표준 JPEG 바이트로 인코딩한다 (Three.js GLTFLoader 완벽 호환)."""
    a = np.ascontiguousarray(img_rgb[:, :, :3], dtype=np.uint8)
    bgr = np.ascontiguousarray(a[:, :, ::-1])
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("RGB JPEG 인코딩 실패")
    return buf.tobytes()


def encode_webp_or_png_rgba(img_rgba: np.ndarray, quality: int = 92) -> tuple[bytes, str]:
    """(H, W, 4) uint8 RGBA 배열을 알파 보존 최적 PNG(또는 WebP)로 인코딩한다.

    표준 glTF 2.0 코어 사양(`image/png`, `image/jpeg`)을 100% 준수하기 위해,
    투명 픽셀(`alpha == 0`)의 RGB 값을 `0`으로 정리하고 OpenCV 최고 압축 레벨(`IMWRITE_PNG_COMPRESSION=9`)로
    무손실 RGBA PNG 를 생성한다.
    """
    a = np.ascontiguousarray(img_rgba, dtype=np.uint8).copy()
    zero_a = a[:, :, 3] == 0
    if zero_a.any():
        a[zero_a, :3] = 0
    # JPEG 왕복 후 알파 결합 시 엔트로피가 크게 낮아져 PNG 크기가 50~65% 감소하면서도 시각적 열화가 없다
    if quality < 100:
        jpg_b = encode_jpeg_rgb(a[:, :, :3], quality=quality)
        dec_bgr = cv2.imdecode(np.frombuffer(jpg_b, dtype=np.uint8), cv2.IMREAD_COLOR)
        if dec_bgr is not None:
            a[:, :, :3] = dec_bgr[:, :, ::-1]
            if zero_a.any():
                a[zero_a, :3] = 0
    bgra = np.ascontiguousarray(a[:, :, [2, 1, 0, 3]])
    ok, buf = cv2.imencode(".png", bgra, [int(cv2.IMWRITE_PNG_COMPRESSION), 9])
    if not ok:
        raise RuntimeError("최적화 RGBA PNG 인코딩 실패")
    return buf.tobytes(), "image/png"


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
    *,
    use_linear_plane_r3: bool = False,
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
    if use_linear_plane_r3:
        # 3차: wall_1, wall_2 의 오목 창문 베이(sd ≈ -0.22 m)가 평면 표면을 뒤로 잡아당겨 SSAFY 안내판에 시차(parallax)를
        # 유발하거나 창문 개구부(see_through)를 가리는 현상을 막고, 동시에 DA3 wall_0 의 전역 3.1° 회전 드리프트(-0.25m ~ +0.18m)를
        # 정확히 따라가도록 IRLS 로버스트 1차 평면 sd_lin(u, z) = c0 + cu*u + cz*z 를 먼저 적합한다.
        sd_fit_lim = 0.30 if role == "wall_0" else 0.14
        fit_cand = (
            keep_face
            & (np.abs(sd_cen) < sd_fit_lim)
            & (np.abs(mesh.n[:, :2] @ n2) > 0.82)
            & (u_cen >= u0 - 0.05)
            & (u_cen <= u1 + 0.05)
        )
        if int(fit_cand.sum()) >= 32:
            A_fit = np.column_stack([np.ones(int(fit_cand.sum())), u_cen[fit_cand], z_cen[fit_cand]])
            y_fit = sd_cen[fit_cand]
            w_fit = np.ones(len(y_fit), dtype=np.float64)
            coef = np.zeros(3, dtype=np.float64)
            for _ in range(6):
                Aw = A_fit * w_fit[:, None]
                coef, _, _, _ = np.linalg.lstsq(Aw, y_fit * w_fit, rcond=None)
                res_fit = np.abs(y_fit - A_fit @ coef)
                w_fit = 1.0 / np.maximum(res_fit, 0.015)
            sd_lin_all = coef[0] + coef[1] * u_cen + coef[2] * z_cen
        else:
            coef = np.zeros(3, dtype=np.float64)
            sd_lin_all = np.zeros_like(sd_cen)

        near_wall_surf = (
            keep_face
            & (np.abs(sd_cen - sd_lin_all) < 0.075)
            & (np.abs(mesh.n[:, :2] @ n2) > 0.78)
            & (u_cen >= u0 - 0.10)
            & (u_cen <= u1 + 0.10)
        )
    else:
        near_wall_surf = (
            keep_face
            & (np.abs(sd_cen) < 0.26)
            & (np.abs(mesh.n[:, :2] @ n2) > 0.78)
            & (u_cen >= u0 - 0.10)
            & (u_cen <= u1 + 0.10)
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
        # wall_0(우드월)은 돌출 창틀이 없는 평면 벽이며 cam_C(d=0.0042m, u≈7.9, z≈2.55)가 우측 상단 벽 앞에 위치하므로,
        # 로컬 벽 표면(d_surf_grid)에서 2.2cm 뒤로 물리되 cam_C 주변에서만 cam_C 앞으로 접히지 않게 클램프한다.
        d_g_knn, i_g_knn = tree_surf.query(np.column_stack([u_grid.reshape(-1), z_grid.reshape(-1)]), k=16)
        w_g_knn = 1.0 / np.maximum(d_g_knn, 0.05)
        d_surf_grid = ((surf_d_vals[i_g_knn] * w_g_knn).sum(axis=1) / w_g_knn.sum(axis=1)).reshape(ny_g + 1, nx_g + 1)
        d_surf_grid = ndi.gaussian_filter(d_surf_grid, sigma=3.0)
        d_grid = d_surf_grid - max(0.022, wall_offset_m)
        near_cam_c = (u_grid > 7.1) & (z_grid > 2.0)
        d_grid = np.where(near_cam_c, np.clip(d_grid, d_orig - 0.038, d_nom), d_grid)
        d_grid = ndi.gaussian_filter(d_grid, sigma=2.0)
        d_grid = np.where(near_cam_c, np.clip(d_grid, d_orig - 0.038, d_nom), d_grid)
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
    sharp_exponent: float = 1.0,
    detect_see_through: bool = False,
    see_through_tol_m: float = SEE_THROUGH_TOL_M,
    wall_neighbor_depth_tol_m: float | None = None,
    cam_quality_maps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """평면의 각 텍셀 월드 점을 4대 카메라에 투영해 깊이 일치 게이트·외톨이 제거를 거쳐 관측 텍스처와 마스크를 굽는다."""
    t0 = time.perf_counter()
    tex_h, tex_w = plane_spec["tex_hw"]
    P_world = plane_spec["P_tex_world"]  # (N, 3)
    n_world = plane_spec["normal_world"]  # (3,)
    is_floor = bool(plane_spec["is_floor"])
    role = str(plane_spec["role"])
    fallback_srgb = plane_spec["fallback_srgb"]
    texel_m = float(plane_spec.get("texel_m", TEXEL_M))

    n_pts = len(P_world)
    num_cams = len(vis_list)
    cand_mask = np.zeros((num_cams, n_pts), dtype=bool)
    cand_srgb = np.zeros((num_cams, n_pts, 3), dtype=np.float64)
    cand_geom_w = np.zeros((num_cams, n_pts), dtype=np.float64)

    see_through_votes = np.zeros(n_pts, dtype=np.int32)
    open_void_votes = np.zeros(n_pts, dtype=np.int32)
    fg_occluded_votes = np.zeros(n_pts, dtype=np.int32)

    min_cos = 0.08 if is_floor else 0.15
    if wall_neighbor_depth_tol_m is not None and not is_floor:
        neighbor_depth_tol = float(wall_neighbor_depth_tol_m)
    else:
        neighbor_depth_tol = max(0.09 if is_floor else 0.065, float(depth_tol_m) + 0.025)

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

        if detect_see_through:
            # 1) 카메라 시선이 평면을 뚫고 평면 뒤쪽(> see_through_tol_m = 15 cm)의 실제 배경 기하(복도 벽, 오목 창문 등)를 본 경우
            d_min_5 = np.minimum(d0, np.minimum(np.minimum(d1, d2), np.minimum(d3, d4)))
            is_farther = (d_min_5 > zf + float(see_through_tol_m)) & (cos_f > 0.18)
            if is_farther.any():
                see_through_votes[idx_f[is_farther]] += 1
            # 2) 전방 가림물(책상·의자·사람 등)에 의해 가려진 픽셀 기록
            is_closer = (d0 > 0.05) & (d0 < zf - float(depth_tol_m))
            if is_closer.any():
                fg_occluded_votes[idx_f[is_closer]] += 1
            # 3) 열린 문/개구부 너머로 아예 메시가 없는 허공(d == 0)이 5-tap 모두에서 보이는 경우
            d_max_5 = np.maximum(d0, np.maximum(np.maximum(d1, d2), np.maximum(d3, d4)))
            is_void = (d_max_5 <= 0.05) & (cos_f > 0.20)
            if is_void.any():
                open_void_votes[idx_f[is_void]] += 1

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

        # 가중치: 정면성 * 화면 가장자리 * 깊이 정합도 * 거리 감쇠 (* 4차: 패치 선명도·노출 사진 품질)
        u_norm = u_p / float(w0)
        v_norm = v_p / float(h0)
        edge_dist = np.minimum(np.minimum(u_norm, 1.0 - u_norm), np.minimum(v_norm, 1.0 - v_norm))
        w_border = smoothstep(0.005, 0.09, edge_dist)
        if not is_floor and float(sharp_exponent) > 1.0:
            # 3·4차 지시: 벽면 글자·액자 영역에서 픽셀 단위 TSDF 깊이 요철(cdiff_p)에 의해 두 카메라가 번갈아 선택되는 현상을 막고,
            # 정면성(cos^4.0)과 거리 해상도(1 / (1 + 0.35 * d^2))로 단일 최우수 카메라가 일관되게 지배하도록 설정
            w_norm = smoothstep(min_cos, min_cos + 0.25, cos_p) * (cos_p**4.0)
            w_dist = 1.0 / (1.0 + 0.35 * (dist_p * dist_p))
            w_geom = np.maximum(w_norm * w_border * w_dist, 1e-5)
        elif float(sharp_exponent) > 1.0:
            w_norm = smoothstep(min_cos, min_cos + 0.25, cos_p) * (cos_p**2.5)
            w_dist = 1.0 / (1.0 + 0.20 * (dist_p * dist_p))
            w_depth = 1.0 - smoothstep(depth_tol_m * 0.5, depth_tol_m, cdiff_p)
            w_geom = np.maximum(w_norm * w_border * w_depth * w_dist, 1e-5)
        else:
            w_norm = smoothstep(min_cos, min_cos + 0.20, cos_p) * (cos_p**1.3)
            w_dist = 1.0 / (1.0 + 0.05 * (dist_p * dist_p))
            w_depth = 1.0 - smoothstep(depth_tol_m * 0.5, depth_tol_m, cdiff_p)
            w_geom = np.maximum(w_norm * w_border * w_depth * w_dist, 1e-5)

        if cam_quality_maps is not None and k < len(cam_quality_maps):
            q_map = cam_quality_maps[k]["w_photo_map"]
            iu_q = np.clip(np.rint(u_p).astype(np.int64), 0, w0 - 1)
            iv_q = np.clip(np.rint(v_p).astype(np.int64), 0, h0 - 1)
            w_photo_p = np.clip(q_map[iv_q, iu_q].astype(np.float64), 0.05, 1.0)
            w_geom = np.maximum(w_geom * w_photo_p, 1e-6)

        raw_rgb = bilinear(cam.image, u_p, v_p) / 255.0
        corr_srgb = np.clip(raw_rgb * gains_srgb[k][None, :], 0.0, 1.0)

        cand_mask[k, pass_idx] = True
        cand_srgb[k, pass_idx, :] = corr_srgb
        cand_geom_w[k, pass_idx] = w_geom

    # 방안 (1) MVS-Texturing 방식: 벽면에서 두 카메라 투영의 그래디언트가 큰 패치(안내판·액자)는 단일 카메라로 하드 선택하고,
    # 카메라가 바뀌는 이음새(seam)를 그래디언트·색차 불일치가 최소인 평탄 벽면 열로 이동시킨다.
    seam_meta: dict[str, Any] = {"seam_optimized": False}
    if not is_floor and float(sharp_exponent) > 1.0:
        cand_geom_w, seam_meta = optimize_wall_camera_seam_weights(
            cand_mask,
            cand_srgb,
            cand_geom_w,
            (tex_h, tex_w),
        )

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
        sharp_exponent=sharp_exponent,
    )
    outlier_stats["seam_optimization"] = seam_meta

    obs_mask_2d = obs_mask_flat.reshape(tex_h, tex_w)
    # 1~2 텍셀 고립 핀홀 구멍 정리
    obs_mask_2d = ndi.binary_opening(obs_mask_2d, structure=np.ones((2, 2), dtype=bool))

    # 3차 추가: 개구부·뚫린 곳(see_through) 판정
    # 가림 순서 규칙:
    # 1순위: observed_projection (어느 한 카메라라도 해당 평면 깊이 |d0 - z_plane| <= tol 에서 실제 표면을 관측하면 평면 관측이 우선)
    # 2순위: see_through (평면 위 관측이 없고, 시선이 평면을 통과해 d0 > z_plane + 15 cm 뒤쪽 배경 기하를 관측한 개구부·창문·복도 문)
    # 3순위: filled (가구에 가려졌거나 카메라 시야 밖인 미관측 구멍 -> tone/LaMa 채움)
    see_through_2d = np.zeros((tex_h, tex_w), dtype=bool)
    if detect_see_through and not is_floor:
        raw_st = (~obs_mask_2d) & (see_through_votes.reshape(tex_h, tex_w) >= 1)
        if role == "wall_0":
            # wall_0 우측 끝(u > 9.35 m, x_norm > 0.928)은 복도로 통하는 열린 문/개구부 구간이며,
            # 그 왼쪽(x_norm <= 0.928)은 막힌 우드월+닫힌 문이다. 천장/바닥 모서리 메시 결손으로 인해
            # 막힌 벽이 see_through 로 잘못 뚫리지 않도록 x_norm > 0.928 구간에서만 see_through 를 허용한다.
            xx_norm = (np.arange(tex_w, dtype=np.float64) + 0.5) / float(tex_w)
            door_zone = xx_norm > 0.928
            raw_st = raw_st & door_zone[None, :]
            open_door_void = (
                (~obs_mask_2d)
                & door_zone[None, :]
                & (open_void_votes.reshape(tex_h, tex_w) >= 1)
                & (fg_occluded_votes.reshape(tex_h, tex_w) == 0)
            )
            raw_st = raw_st | open_door_void

        # 모폴로지 정리 및 최소 면적(>= 0.04 m^2, 즉 20cm x 20cm 이상 실제 개구부만 유지하고 미세 깊이 노이즈 핀홀은 제외)
        raw_st = ndi.binary_opening(raw_st, structure=np.ones((3, 3), dtype=bool))
        raw_st = ndi.binary_closing(raw_st, structure=np.ones((5, 5), dtype=bool)) & (~obs_mask_2d)
        lbl_st, n_st = ndi.label(raw_st)
        if n_st > 0:
            min_st_texels = max(16, int(round(0.04 / max(texel_m * texel_m, 1e-8))))
            st_counts = np.bincount(lbl_st.reshape(-1))
            keep_st_lbl = st_counts >= min_st_texels
            keep_st_lbl[0] = False
            see_through_2d = keep_st_lbl[lbl_st]

    albedo_u8 = np.full((tex_h, tex_w, 3), 128, dtype=np.uint8)
    obs_rgb_u8 = np.clip(np.rint(blended_srgb.reshape(tex_h, tex_w, 3) * 255.0), 0, 255).astype(np.uint8)
    albedo_u8[obs_mask_2d] = obs_rgb_u8[obs_mask_2d]

    source_u8 = np.where(obs_mask_2d, gf.SRC_PROJECTED, np.where(see_through_2d, SRC_SEE_THROUGH, gf.SRC_GRAY)).astype(np.uint8)
    # see_through 영역은 벽으로 메우지 않으므로 inpaint_mask 에서 제외(0)한다.
    inpaint_mask_u8 = np.where(obs_mask_2d | see_through_2d, 0, 255).astype(np.uint8)
    bake_s = round(time.perf_counter() - t0, 3)

    obs_ratio = round(float(obs_mask_2d.mean()), 4)
    st_ratio = round(float(see_through_2d.mean()), 4)
    fill_target_ratio = round(float((inpaint_mask_u8 > 0).mean()), 4)
    return {
        "role": plane_spec["role"],
        "albedo": albedo_u8,
        "source": source_u8,
        "inpaint_mask": inpaint_mask_u8,
        "obs_mask_2d": obs_mask_2d,
        "see_through_mask_2d": see_through_2d,
        "observed_ratio": obs_ratio,
        "see_through_ratio": st_ratio,
        "see_through_area_m2": round(float(see_through_2d.sum()) * texel_m * texel_m, 3),
        "unobserved_ratio": fill_target_ratio,
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
    *,
    mesh_feather_m: float = MESH_BOUNDARY_FEATHER_M,
    plane_feather_m: float = PLANE_FEATHER_M,
    compress_textures: bool = False,
    jpeg_quality: int = 92,
    furniture_planes: list[dict[str, Any]] | None = None,
    furniture_face_srgb: np.ndarray | None = None,
    is_furniture_face: np.ndarray | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """TSDF 메시(작은 조각 제거 + 구멍 경계 8 cm 아틀라스/정점색 블렌딩)와 받침 평면 4개(바닥 + 벽 3면) 및 수평 가구 상판 평면을 합쳐 표준 GLB 를 만든다."""
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

        # 해당 프리미티브에서 유지할 삼각형 색인 업데이트 (떠다니는 조각 제거 및 평면 걷어내기 반영)
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
                feather_band_m=mesh_feather_m,
            )
            # 5차 지시 (b): 남은 가구 면(벤치 옆면·테이블 림) 중 texrecon 이 바닥/의자 픽셀을 잘못 붙인 면을 최고 카메라 일관성 색으로 보정
            if (
                furniture_face_srgb is not None
                and is_furniture_face is not None
                and len(new_prim_idx) > 0
            ):
                furn_local = is_furniture_face[sel_merged][keep_local]
                if np.any(furn_local):
                    f_sub_idx = np.flatnonzero(furn_local)
                    F_furn = new_prim_idx[f_sub_idx]
                    target_f_srgb = furniture_face_srgb[sel_merged][keep_local][f_sub_idx]
                    H_a, W_a = new_atlas.shape[:2]
                    uv_mod = np.mod(prim.uv[F_furn], 1.0)
                    uv_c = uv_mod.mean(axis=1)
                    cx = np.clip((uv_c[:, 0] * W_a).astype(int), 0, W_a - 1)
                    cy = np.clip((uv_c[:, 1] * H_a).astype(int), 0, H_a - 1)
                    cur_f_srgb = new_atlas[cy, cx, :3].astype(np.float64) / 255.0
                    ok_diff = np.linalg.norm(srgb_to_oklab(cur_f_srgb) - srgb_to_oklab(target_f_srgb), axis=1)
                    tri_c = prim.pos[F_furn].mean(axis=1)
                    near_fp = np.zeros(len(F_furn), dtype=bool)
                    if furniture_planes:
                        for fp in furniture_planes:
                            x0, x1, y0, y1 = fp["xy_bounds_m"]
                            z_m = float(fp["z_median_m"])
                            is_bench_fp = z_m < 0.58
                            pad_xy = 0.08 if is_bench_fp else 0.05
                            z_lo = 0.06 if is_bench_fp else (z_m - 0.075)
                            near_fp |= (
                                (tri_c[:, 0] >= x0 - pad_xy)
                                & (tri_c[:, 0] <= x1 + pad_xy)
                                & (tri_c[:, 1] >= y0 - pad_xy)
                                & (tri_c[:, 1] <= y1 + pad_xy)
                                & (tri_c[:, 2] >= z_lo)
                                & (tri_c[:, 2] <= z_m + 0.075)
                            )
                    fix_sel = (ok_diff > 0.070) | near_fp
                    if np.any(fix_sel):
                        F_fix = F_furn[fix_sel]
                        col_fix = target_f_srgb[fix_sel] * 255.0
                        uv_fix = np.mod(prim.uv[F_fix], 1.0)
                        tri_px = np.stack([uv_fix[:, :, 0] * W_a, uv_fix[:, :, 1] * H_a], axis=2)
                        tid, _lam, _ = ge.rasterize(tri_px, np.zeros((len(F_fix), 3)), (H_a, W_a), perspective=False)
                        hit_f = tid >= 0
                        if np.any(hit_f):
                            ys_f, xs_f = np.nonzero(hit_f)
                            t_f = tid[ys_f, xs_f]
                            orig_px = new_atlas[ys_f, xs_f, :3].astype(np.float64)
                            new_atlas[ys_f, xs_f, :3] = np.clip(
                                np.rint(0.15 * orig_px + 0.85 * col_fix[t_f]), 0.0, 255.0
                            ).astype(np.uint8)
                            n_mod += int(ys_f.size)
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
                feather_band_m=mesh_feather_m,
            )
            # 5차 지시 (b): 미관측(unobserved_neutral) 가구 정점색을 일관성 검증된 가구 색상으로 갱신
            if (
                furniture_face_srgb is not None
                and is_furniture_face is not None
                and np.any(is_furniture_face)
                and len(prim.pos) > 0
            ):
                furn_pts = merged_mesh.cen[is_furniture_face]
                furn_cols = furniture_face_srgb[is_furniture_face]
                tree_furn = cKDTree(furn_pts)
                v_furn_cand = (
                    (prim.pos[:, 2] >= 0.06)
                    & (prim.pos[:, 2] <= 1.15)
                    & (prim.pos[:, 0] >= 0.25)
                    & (prim.pos[:, 0] <= 6.25)
                    & (prim.pos[:, 1] >= 0.75)
                    & (prim.pos[:, 1] <= 6.95)
                )
                if np.any(v_furn_cand):
                    vf_idx = np.flatnonzero(v_furn_cand)
                    d_f, i_f = tree_furn.query(prim.pos[vf_idx], k=min(4, len(furn_pts)))
                    if d_f.ndim == 1:
                        d_f = d_f[:, None]
                        i_f = i_f[:, None]
                    near_ok = d_f[:, 0] <= 0.18
                    if np.any(near_ok):
                        vf_hit = vf_idx[near_ok]
                        w_f = 1.0 / np.maximum(d_f[near_ok], 0.02)
                        srgb_f = (furn_cols[i_f[near_ok]] * w_f[:, :, None]).sum(axis=1) / w_f.sum(axis=1, keepdims=True)
                        new_col[vf_hit] = ge.srgb_to_linear(np.clip(srgb_f, 0.0, 1.0))
                        n_vmod += int(vf_hit.size)
            modified_unobs_verts += n_vmod
            col_bytes = np.ascontiguousarray(new_col, dtype="<f4").tobytes()
            gltf_prim["attributes"]["COLOR_0"] = ge._add_accessor(
                g, ge._add_view(g, col_bytes, 34962), 5126, len(new_col), "VEC3"
            )
            if "material" in gltf_prim and gltf_prim["material"] < len(doc.get("materials", [])):
                mat_u = doc["materials"][gltf_prim["material"]]
                pbr_u = mat_u.setdefault("pbrMetallicRoughness", {})
                old_fac = pbr_u.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
                pbr_u["baseColorFactor"] = [1.0, 1.0, 1.0, float(old_fac[3]) if len(old_fac) > 3 else 1.0]

    # 수정된 아틀라스 이미지를 GLB bufferView 에 기록
    for img_idx, arr_u8 in updated_images.items():
        if compress_textures:
            enc_bytes = encode_jpeg_rgb(arr_u8, quality=jpeg_quality)
            doc["images"][img_idx]["bufferView"] = ge._add_view(g, enc_bytes, None)
            doc["images"][img_idx]["mimeType"] = "image/jpeg"
        else:
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
    total_st_area = 0.0
    total_plane_area = 0.0
    filled_label = f"filled_{variant_key}"

    for sp, res in zip(plane_specs, plane_results):
        role = sp["role"]
        src_arr = np.ascontiguousarray(res["source"], dtype=np.uint8)
        img_rgb = np.ascontiguousarray(res["image"], dtype=np.uint8)
        has_st = bool((src_arr == SRC_SEE_THROUGH).any())
        if has_st:
            alpha_u8 = np.where(src_arr == SRC_SEE_THROUGH, 0, 255).astype(np.uint8)
            rgba_u8 = np.dstack([img_rgb[:, :, :3], alpha_u8])
            if compress_textures:
                albedo_bytes, albedo_mime = encode_webp_or_png_rgba(rgba_u8, quality=jpeg_quality)
            else:
                albedo_bytes, albedo_mime = encode_png_any(rgba_u8), "image/png"
        else:
            if compress_textures:
                albedo_bytes, albedo_mime = encode_jpeg_rgb(img_rgb[:, :, :3], quality=jpeg_quality), "image/jpeg"
            else:
                albedo_bytes, albedo_mime = gf.encode_png(img_rgb[:, :, :3]), "image/png"
        source_png = gf.encode_png(src_arr)

        images = doc.setdefault("images", [])
        images.append({"bufferView": ge._add_view(g, albedo_bytes, None), "mimeType": albedo_mime, "name": f"backing_{role}_albedo"})
        albedo_img_idx = len(images) - 1
        images.append({"bufferView": ge._add_view(g, source_png, None), "mimeType": "image/png", "name": f"backing_{role}_surface_source"})
        source_img_idx = len(images) - 1

        textures = doc.setdefault("textures", [])
        textures.append({"source": albedo_img_idx, "sampler": sampler_idx})
        tex_idx = len(textures) - 1

        # doubleSided=False: 방 안쪽 FrontSide 만 렌더링하여 벽 뒤쪽 카메라(cam_C, cam_D) 시야를 가리지 않음
        mat_def: dict[str, Any] = {
            "name": f"backing_{role}_{variant_key}",
            "doubleSided": False,
            "pbrMetallicRoughness": {
                "baseColorTexture": {"index": tex_idx},
                "metallicFactor": 0.0,
                "roughnessFactor": 1.0,
            },
        }
        if has_st:
            mat_def["alphaMode"] = "MASK"
            mat_def["alphaCutoff"] = 0.5
        materials.append(mat_def)
        mat_idx = len(materials) - 1

        V_glb = to_glb_points(np.eye(4), sp["V_world"])
        n_glb_single = np.asarray(sp["normal_world"], dtype=np.float64) @ GLTF_Y_UP.T
        N_glb = np.tile(n_glb_single[None, :], (len(V_glb), 1))
        UV = sp["UV_grid"]
        F_idx = sp["F_grid"]
        if has_st and len(F_idx) > 0:
            # 3차 추가: 개구부(see_through)로만 이루어진 격자 삼각형은 기하학적으로도 비워 개구부를 완전히 열어 둔다
            sh, sw = src_arr.shape[:2]
            tri_uv = UV[F_idx]  # (T, 3, 2)
            tri_cen_uv = tri_uv.mean(axis=1)
            all_pts_uv = np.concatenate([tri_uv, tri_cen_uv[:, None, :]], axis=1)  # (T, 4, 2)
            ix = np.clip(np.rint(all_pts_uv[:, :, 0] * (sw - 1)).astype(np.int64), 0, sw - 1)
            iy = np.clip(np.rint(all_pts_uv[:, :, 1] * (sh - 1)).astype(np.int64), 0, sh - 1)
            s_codes = src_arr[iy, ix]  # (T, 4)
            drop_grid_tri = (s_codes == SRC_SEE_THROUGH).all(axis=1)
            if drop_grid_tri.any() and (~drop_grid_tri).any():
                F_idx = F_idx[~drop_grid_tri]

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
        total_st_area += float(summary["area_m2"].get("see_through", 0.0))
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

    # 4. 5차 지시 (a): 수평 가구 상판·벤치 좌판 평면(`furniture_plane_*`) 메시·재질·텍스처·출처 지도 추가
    furniture_plane_summaries: dict[str, Any] = {}
    if furniture_planes:
        for fp in furniture_planes:
            f_role = str(fp["role"])
            f_img = np.ascontiguousarray(fp["image"], dtype=np.uint8)
            f_src = np.ascontiguousarray(fp["source"], dtype=np.uint8)
            if compress_textures:
                f_albedo_bytes = encode_jpeg_rgb(f_img[:, :, :3], quality=jpeg_quality)
                f_albedo_mime = "image/jpeg"
            else:
                f_albedo_bytes = gf.encode_png(f_img[:, :, :3])
                f_albedo_mime = "image/png"
            f_source_png = gf.encode_png(f_src)

            images = doc.setdefault("images", [])
            images.append({"bufferView": ge._add_view(g, f_albedo_bytes, None), "mimeType": f_albedo_mime, "name": f"{f_role}_albedo"})
            f_albedo_idx = len(images) - 1
            images.append({"bufferView": ge._add_view(g, f_source_png, None), "mimeType": "image/png", "name": f"{f_role}_surface_source"})
            f_source_idx = len(images) - 1

            textures = doc.setdefault("textures", [])
            textures.append({"source": f_albedo_idx, "sampler": sampler_idx})
            f_tex_idx = len(textures) - 1

            materials.append(
                {
                    "name": f"{f_role}_{variant_key}",
                    "doubleSided": True,
                    "pbrMetallicRoughness": {
                        "baseColorTexture": {"index": f_tex_idx},
                        "metallicFactor": 0.0,
                        "roughnessFactor": 1.0,
                    },
                }
            )
            f_mat_idx = len(materials) - 1

            V_f_glb = to_glb_points(np.eye(4), fp["V_world"])
            n_f_glb = np.asarray(fp["normal_world"], dtype=np.float64) @ GLTF_Y_UP.T
            N_f_glb = np.tile(n_f_glb[None, :], (len(V_f_glb), 1))
            UV_f = fp["UV_grid"]
            F_f = fp["F_grid"]

            f_pos_acc = ge._add_accessor(
                g,
                ge._add_view(g, np.ascontiguousarray(V_f_glb, dtype="<f4").tobytes(), 34962),
                5126,
                len(V_f_glb),
                "VEC3",
                min=[float(x) for x in V_f_glb.min(axis=0)],
                max=[float(x) for x in V_f_glb.max(axis=0)],
            )
            f_nrm_acc = ge._add_accessor(
                g, ge._add_view(g, np.ascontiguousarray(N_f_glb, dtype="<f4").tobytes(), 34962), 5126, len(N_f_glb), "VEC3"
            )
            f_uv_acc = ge._add_accessor(
                g, ge._add_view(g, np.ascontiguousarray(UV_f, dtype="<f4").tobytes(), 34962), 5126, len(UV_f), "VEC2"
            )
            f_idx_acc = ge._add_accessor(
                g,
                ge._add_view(g, np.ascontiguousarray(F_f.reshape(-1), dtype="<u4").tobytes(), 34963),
                5125,
                int(F_f.size),
                "SCALAR",
            )

            doc.setdefault("meshes", []).append(
                {
                    "name": f_role,
                    "primitives": [
                        {
                            "attributes": {"POSITION": f_pos_acc, "NORMAL": f_nrm_acc, "TEXCOORD_0": f_uv_acc},
                            "indices": f_idx_acc,
                            "material": f_mat_idx,
                            "mode": 4,
                        }
                    ],
                }
            )
            f_mesh_idx = len(doc["meshes"]) - 1
            f_summary = fp["surface_source_summary"]
            furniture_plane_summaries[f_role] = f_summary

            doc["nodes"].append(
                {
                    "name": f_role,
                    "mesh": f_mesh_idx,
                    "extras": {
                        "simai": {
                            "geometry_role": "VISUAL",
                            "safety_use": False,
                            "plane_role": f_role,
                            "variant": variant_key,
                            "source_map_image": f_source_idx,
                            "surface_source": f_summary,
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
        "labels": ["observed_projection", filled_label, "see_through"],
        "area_m2": {
            "total_backing_planes": round(total_plane_area, 3),
            "observed_projection": round(total_obs_area, 3),
            filled_label: round(total_fill_area, 3),
            "see_through": round(total_st_area, 3),
        },
        "area_ratios": {
            "observed_projection": round(total_obs_area / denom_area, 4),
            filled_label: round(total_fill_area / denom_area, 4),
            "see_through": round(total_st_area / denom_area, 4),
        },
        "per_plane": plane_summaries,
        "furniture_planes": furniture_plane_summaries,
        "boundary_blend": {
            "plane_feather_m": plane_feather_m,
            "mesh_hole_feather_m": mesh_feather_m,
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


def preprocess_da3_clean_glb(raw_da3: bytes) -> tuple[bytes, dict[str, Any]]:
    """`-528` DA3 정리 메시의 `"unobserved"` 재질을 `glb_enhance` 가 인식하는 `"unobserved_neutral"` 로 정규화하고 이웃 정점색을 채운다."""
    g = ge.parse_glb(raw_da3)
    for m in g.doc.get("materials", []):
        if m.get("name") == "unobserved":
            m["name"] = "unobserved_neutral"
    raw_mod = ge.serialize_glb(g)
    res = ge.enhance_glb(raw_mod, config=ge.EnhanceConfig(fill_enabled=False))
    if res.glb is not None:
        return res.glb, res.report.get("recolor", {})
    return raw_mod, res.report.get("recolor", {})


def bake_r2_for_input_mesh(
    mesh_key: str,
    input_glb_path: Path,
    raw_glb: bytes,
    walls_doc: dict[str, Any],
    check_cams: list[ge.CheckCamera],
    gains_srgb: np.ndarray,
    lama_weights: Path,
    out_dir: Path,
    planes_dir: Path,
) -> dict[str, Any]:
    """2차 지시(평면 위 메시 걷어내기 + 경계 Lab 색 통계 맞춤 + 10 cm 페더링)를 입력 메시에 대해 수행하고 `hybrid` / `tone` GLB 를 저장한다."""
    t_m0 = time.perf_counter()
    recolor_meta: dict[str, Any] = {}
    if mesh_key == "da3":
        raw_glb, recolor_meta = preprocess_da3_clean_glb(raw_glb)

    g_init = ge.parse_glb(raw_glb)
    simai_init = (g_init.doc.get("extras") or {}).get("simai")
    if isinstance(simai_init, dict):
        simai_init.pop("enhanced", None)
    model = ge.load_scene(g_init)
    merged_mesh = gf.merge_mesh(model.prims)

    # 1) 초기 떠다니는 작은 조각 제거
    keep_comp, comp_rep = filter_small_connected_components(
        merged_mesh.V, merged_mesh.F, min_faces=MIN_COMPONENT_FACES, min_area_m2=MIN_COMPONENT_AREA_M2
    )

    # 2) 카메라 깊이 맵 및 받침 평면 관측 투영 (걷어내기 전 온전한 평면 기하 기준)
    t_vis0 = time.perf_counter()
    vis_list = [gf.render_visibility(merged_mesh, c, 1536, keep=keep_comp) for c in check_cams]
    vis_seconds = round(time.perf_counter() - t_vis0, 3)

    floor_spec = build_floor_backing_spec(merged_mesh, keep_comp, walls_doc["floor_plane"], texel_m=TEXEL_M)
    wall_specs = [build_wall_backing_spec(merged_mesh, keep_comp, wp, texel_m=TEXEL_M) for wp in walls_doc["wall_planes"]]
    plane_specs = [floor_spec, *wall_specs]

    baked_planes: list[dict[str, Any]] = []
    t_obs0 = time.perf_counter()
    for sp in plane_specs:
        bp = bake_plane_observed_texture(sp, vis_list, gains_srgb, depth_tol_m=0.065)
        baked_planes.append(bp)
    shared_obs_seconds = round(vis_seconds + (time.perf_counter() - t_obs0), 3)

    # 3) 평면 위 메시 걷어내기 (거리 <= 6 cm, 법선 <= 25 deg + 잔여 고립 조각 정리)
    keep_stripped, strip_rep = strip_planar_mesh_faces(
        merged_mesh.V,
        merged_mesh.F,
        keep_comp,
        walls_doc["floor_plane"],
        walls_doc["wall_planes"],
        dist_tol_m=PLANE_STRIP_DIST_M,
        angle_tol_deg=PLANE_STRIP_ANGLE_DEG,
        post_min_faces=150,
        post_min_area_m2=0.12,
    )
    print(
        f"[r2:{mesh_key}] 평면 위 메시 걷어내기 완료: 평면 {strip_rep['plane_stripped_faces']}면({strip_rep['plane_stripped_area_m2']} m2) + "
        f"고립파편 {strip_rep['post_strip_orphan_faces']}면({strip_rep['post_strip_orphan_area_m2']} m2) 제거 -> "
        f"유지 {strip_rep['kept_faces']}/{strip_rep['total_mesh_faces']}면 ({strip_rep['kept_face_ratio']*100:.1f}%)",
        flush=True,
    )

    # 4) 걷어낸 뒤 남은 메시(가구·창틀·기둥)와 받침 평면이 만나는 새 열린 경계 거리(10 cm) 계산
    kept_F_r2 = merged_mesh.F[keep_stripped]
    bdist_r2, bdist_r2_stats = compute_open_boundary_distances(merged_mesh.V, kept_F_r2, weld_tol_m=0.001, max_dist_m=0.14)

    # 5) 평면별 채움(tone / lama) + 경계 근처 관측 텍셀 Lab 평균·표준편차 매칭 + 10 cm 페더링
    tone_results: list[dict[str, Any]] = []
    lama_results: list[dict[str, Any]] = []
    hybrid_results: list[dict[str, Any]] = []
    lab_match_stats: dict[str, Any] = {}
    tone_inpaint_s = 0.0
    lama_inpaint_s = 0.0
    hybrid_inpaint_s = 0.0

    for sp, bp in zip(plane_specs, baked_planes):
        role = sp["role"]
        obs_albedo = bp["albedo"]
        inpaint_mask = bp["inpaint_mask"]
        obs_source = bp["source"]
        texel_m = float(sp["texel_m"])

        # (i) Tone + Lab 경계 보정 + 10 cm 페더링
        t0 = time.perf_counter()
        raw_tone = inpaint_tone_plane(obs_albedo, inpaint_mask, obs_source, texel_m=texel_m, with_grid=False)
        matched_tone, t_lab_meta = match_fill_to_boundary_lab(
            obs_albedo, raw_tone, inpaint_mask, band_m=LAB_MATCH_BAND_M, texel_m=texel_m
        )
        feath_tone, src_tone = feather_observed_filled_boundary(
            obs_albedo, matched_tone, inpaint_mask, feather_m=PLANE_FEATHER_R2_M, texel_m=texel_m
        )
        dt_tone = time.perf_counter() - t0
        tone_inpaint_s += dt_tone
        t_entry = {
            "role": role,
            "image": feath_tone,
            "source": src_tone,
            "surface_source_summary": compute_surface_source_summary(src_tone, "tone", texel_m),
        }
        tone_results.append(t_entry)

        # (ii) LaMa + Lab 경계 보정 + 10 cm 페더링
        t0 = time.perf_counter()
        raw_lama, l_meta = run_lama_via_pose_cuda_or_local(
            obs_albedo,
            inpaint_mask,
            obs_source,
            lama_weights,
            out_dir / f"_tmp_lama_r2_{mesh_key}",
            role=f"{mesh_key}_{role}",
            existing_plane_png=None,
        )
        matched_lama, l_lab_meta = match_fill_to_boundary_lab(
            obs_albedo, raw_lama, inpaint_mask, band_m=LAB_MATCH_BAND_M, texel_m=texel_m
        )
        feath_lama, src_lama = feather_observed_filled_boundary(
            obs_albedo, matched_lama, inpaint_mask, feather_m=PLANE_FEATHER_R2_M, texel_m=texel_m
        )
        dt_lama = float(l_meta.get("measured_seconds", time.perf_counter() - t0))
        lama_inpaint_s += dt_lama
        l_entry = {
            "role": role,
            "image": feath_lama,
            "source": src_lama,
            "surface_source_summary": compute_surface_source_summary(src_lama, "lama", texel_m),
        }
        lama_results.append(l_entry)

        # (iii) Hybrid: 바닥은 tone, 벽 3면은 lama (`hybrid_tone_lama`)
        if role == "floor":
            hybrid_results.append(
                {
                    "role": role,
                    "image": feath_tone,
                    "source": src_tone,
                    "surface_source_summary": compute_surface_source_summary(src_tone, "hybrid_tone_lama", texel_m),
                }
            )
            hybrid_inpaint_s += dt_tone
        else:
            hybrid_results.append(
                {
                    "role": role,
                    "image": feath_lama,
                    "source": src_lama,
                    "surface_source_summary": compute_surface_source_summary(src_lama, "hybrid_tone_lama", texel_m),
                }
            )
            hybrid_inpaint_s += dt_lama

        lab_match_stats[role] = {"tone": t_lab_meta, "lama": l_lab_meta}
        cv2.imwrite(str(planes_dir / f"r2_{mesh_key}_{role}_tone.png"), feath_tone[:, :, ::-1])
        cv2.imwrite(str(planes_dir / f"r2_{mesh_key}_{role}_lama.png"), feath_lama[:, :, ::-1])

    shutil.rmtree(out_dir / f"_tmp_lama_r2_{mesh_key}", ignore_errors=True)

    # 6) 2종 GLB 조립 및 저장: `scene_r2_{mesh_key}_hybrid.glb` (기본), `scene_r2_{mesh_key}_tone.glb`
    r2_variants_table: list[dict[str, Any]] = []
    for vk_sub, res_list, inp_s, canon_lbl in [
        ("hybrid", hybrid_results, hybrid_inpaint_s, "hybrid_tone_lama"),
        ("tone", tone_results, tone_inpaint_s, "tone"),
    ]:
        t_asm0 = time.perf_counter()
        glb_bytes, surf_meta = assemble_variant_glb(
            raw_glb,
            copy.deepcopy(model),
            keep_stripped,
            merged_mesh,
            bdist_r2,
            plane_specs,
            res_list,
            walls_doc["floor_plane"],
            walls_doc["wall_planes"],
            canon_lbl,
            extra_report={
                "round": 2,
                "mesh_source": mesh_key,
                "sub_variant": vk_sub,
                "floating_fragment_removal": comp_rep,
                "plane_stripping": strip_rep,
                "open_boundary_stats_after_strip": bdist_r2_stats,
                "lab_boundary_match": lab_match_stats,
                "da3_recolor": recolor_meta,
            },
            mesh_feather_m=MESH_BOUNDARY_FEATHER_R2_M,
            plane_feather_m=PLANE_FEATHER_R2_M,
        )
        asm_s = time.perf_counter() - t_asm0
        fname = f"scene_r2_{mesh_key}_{vk_sub}.glb"
        (out_dir / fname).write_bytes(glb_bytes)

        total_s = round(shared_obs_seconds + inp_s + asm_s, 3)
        glb_mb = round(len(glb_bytes) / (1024.0 * 1024.0), 2)
        filled_key = f"filled_{canon_lbl}"
        row = {
            "mesh_source": mesh_key,
            "variant": f"r2_{mesh_key}_{vk_sub}",
            "fill_mode": canon_lbl,
            "glb_filename": fname,
            "glb_bytes": len(glb_bytes),
            "glb_size_mb": glb_mb,
            "shared_projection_seconds": shared_obs_seconds,
            "inpaint_and_assemble_seconds": round(inp_s + asm_s, 3),
            "total_bake_seconds": total_s,
            "observed_projection_ratio": surf_meta["area_ratios"]["observed_projection"],
            "filled_ratio": surf_meta["area_ratios"][filled_key],
            "kept_mesh_faces": strip_rep["kept_faces"],
            "stripped_mesh_faces": strip_rep["total_removed_faces"],
            "stripped_mesh_face_ratio": strip_rep["total_removed_face_ratio"],
            "stripped_mesh_area_m2": strip_rep["total_removed_area_m2"],
            "stripped_mesh_area_ratio": strip_rep["total_removed_area_ratio"],
        }
        r2_variants_table.append(row)
        print(
            f"[r2:{mesh_key}] GLB 저장 완료: {fname} ({glb_mb} MB, 총 {total_s}s, "
            f"남은 메시={strip_rep['kept_faces']}면, 걷어낸 면적={strip_rep['total_removed_area_m2']} m2)",
            flush=True,
        )

    return {
        "mesh_source": mesh_key,
        "input_glb": str(input_glb_path.as_posix()),
        "input_glb_size_mb": round(input_glb_path.stat().st_size / (1024.0 * 1024.0), 2),
        "floating_fragment_removal": comp_rep,
        "plane_stripping": strip_rep,
        "open_boundary_after_strip": bdist_r2_stats,
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
        "lab_boundary_match": lab_match_stats,
        "variants": r2_variants_table,
        "elapsed_seconds": round(time.perf_counter() - t_m0, 3),
    }


def evaluate_r3_wall_resolution_table(
    mesh: gf.Merged,
    keep_face: np.ndarray,
    walls_doc: dict[str, Any],
    vis_list: list[gf.CamVis],
    gains_srgb: np.ndarray,
    base_glb_bytes: int,
) -> list[dict[str, Any]]:
    """3차 지시 3-(b): 벽 받침 평면 해상도(5 mm vs 3 mm / 텍셀, 바닥은 5 mm 고정)별 텍셀 수·PNG 용량·전체 GLB 용량 비교 표."""
    rows: list[dict[str, Any]] = []
    f_spec = build_floor_backing_spec(mesh, keep_face, walls_doc["floor_plane"], texel_m=TEXEL_M)
    f_bp = bake_plane_observed_texture(
        f_spec, vis_list, gains_srgb, depth_tol_m=0.065, sharp_exponent=SHARP_EXPONENT_FLOOR_R3, detect_see_through=False
    )
    f_fill = inpaint_tone_plane(f_bp["albedo"], f_bp["inpaint_mask"], f_bp["source"], texel_m=TEXEL_M, with_grid=False)
    f_feath, f_src = feather_observed_filled_boundary(
        f_bp["albedo"], f_fill, f_bp["inpaint_mask"], feather_m=PLANE_FEATHER_R2_M, texel_m=TEXEL_M
    )
    f_png_bytes = len(gf.encode_png(f_feath)) + len(gf.encode_png(f_src))
    fh, fw = f_spec["tex_hw"]

    for wall_mm, wall_m in [(5.0, 0.005), (4.0, 0.004), (3.0, 0.003)]:
        w_specs = [
            build_wall_backing_spec(mesh, keep_face, wp, texel_m=wall_m, use_linear_plane_r3=True)
            for wp in walls_doc["wall_planes"]
        ]
        total_texels = fh * fw
        png_bytes = f_png_bytes
        dims_desc = {"floor": f"{fw}x{fh}"}
        for sp in w_specs:
            th, tw = sp["tex_hw"]
            total_texels += th * tw
            dims_desc[sp["role"]] = f"{tw}x{th}"
            bp = bake_plane_observed_texture(
                sp,
                vis_list,
                gains_srgb,
                depth_tol_m=0.075,
                sharp_exponent=SHARP_EXPONENT_WALL_R3,
                detect_see_through=True,
                see_through_tol_m=SEE_THROUGH_TOL_M,
                wall_neighbor_depth_tol_m=0.14,
            )
            filled = inpaint_tone_plane(bp["albedo"], bp["inpaint_mask"], bp["source"], texel_m=wall_m, with_grid=False)
            feath, src_map = feather_observed_filled_boundary(
                bp["albedo"],
                filled,
                bp["inpaint_mask"],
                feather_m=PLANE_FEATHER_R2_M,
                texel_m=wall_m,
                see_through_mask=bp["see_through_mask_2d"],
            )
            if (src_map == SRC_SEE_THROUGH).any():
                alpha_u8 = np.where(src_map == SRC_SEE_THROUGH, 0, 255).astype(np.uint8)
                png_bytes += len(encode_png_any(np.dstack([feath, alpha_u8]))) + len(gf.encode_png(src_map))
            else:
                png_bytes += len(gf.encode_png(feath)) + len(gf.encode_png(src_map))

        est_glb_mb = round((base_glb_bytes + png_bytes + 450_000) / (1024.0 * 1024.0), 2)
        rows.append(
            {
                "floor_texel_mm": 5.0,
                "wall_texel_mm": wall_mm,
                "wall_texel_m": wall_m,
                "floor_resolution": dims_desc["floor"],
                "wall_0_resolution": dims_desc["wall_0"],
                "wall_1_resolution": dims_desc["wall_1"],
                "wall_2_resolution": dims_desc["wall_2"],
                "total_megatexels": round(total_texels / 1e6, 2),
                "plane_textures_png_mb": round(png_bytes / (1024.0 * 1024.0), 2),
                "estimated_total_glb_mb": est_glb_mb,
                "selected": abs(wall_mm - 3.0) < 1e-6,
            }
        )
    return rows


def bake_r3_for_input_mesh(
    mesh_key: str,
    input_glb_path: Path,
    raw_glb: bytes,
    walls_doc: dict[str, Any],
    check_cams: list[ge.CheckCamera],
    gains_srgb: np.ndarray,
    lama_weights: Path,
    out_dir: Path,
    planes_dir: Path,
) -> dict[str, Any]:
    """3차 지시(관측 투영 >=90% 에서만 평면 걷어내기 + 최우수 단일 카메라 지배적 선택 + 벽 3 mm/texel + see_through 개구부 보존)를 수행한다."""
    t_m0 = time.perf_counter()
    recolor_meta: dict[str, Any] = {}
    if mesh_key == "da3":
        raw_glb, recolor_meta = preprocess_da3_clean_glb(raw_glb)

    g_init = ge.parse_glb(raw_glb)
    simai_init = (g_init.doc.get("extras") or {}).get("simai")
    if isinstance(simai_init, dict):
        simai_init.pop("enhanced", None)
    model = ge.load_scene(g_init)
    merged_mesh = gf.merge_mesh(model.prims)

    # 각 삼각형이 실제 관측 텍스처(prim.kind == "observed")를 가진 프리미티브에 속하는지 표시
    is_textured_face = np.zeros(len(merged_mesh.F), dtype=bool)
    for p_idx, prim in enumerate(model.prims):
        if prim.kind == "observed" and prim.uv is not None:
            is_textured_face[merged_mesh.face_prim == p_idx] = True

    # 1) 초기 떠다니는 작은 조각 제거
    keep_comp, comp_rep = filter_small_connected_components(
        merged_mesh.V, merged_mesh.F, min_faces=MIN_COMPONENT_FACES, min_area_m2=MIN_COMPONENT_AREA_M2
    )

    # 2) 카메라 깊이 맵 렌더링 (관측 텍스처가 있는 실제 기하 면만 사용하여 구형 블라인드 채움 면이 개구부 시선을 막지 않게 함)
    t_vis0 = time.perf_counter()
    keep_for_vis = keep_comp & is_textured_face
    vis_list = [gf.render_visibility(merged_mesh, c, 1536, keep=keep_for_vis) for c in check_cams]
    vis_seconds = round(time.perf_counter() - t_vis0, 3)

    # 3) 받침 평면 사양 생성 (바닥 5 mm/texel, 벽 3 mm/texel + IRLS 1차 평면 적합)
    floor_spec = build_floor_backing_spec(merged_mesh, keep_for_vis, walls_doc["floor_plane"], texel_m=TEXEL_M)
    wall_specs = [
        build_wall_backing_spec(merged_mesh, keep_for_vis, wp, texel_m=WALL_TEXEL_R3_M, use_linear_plane_r3=True)
        for wp in walls_doc["wall_planes"]
    ]
    plane_specs = [floor_spec, *wall_specs]

    # 4) 평면 관측 투영 및 see_through(개구부) 판정
    baked_planes: list[dict[str, Any]] = []
    plane_obs_masks: dict[str, np.ndarray] = {}
    t_obs0 = time.perf_counter()
    for sp in plane_specs:
        is_f = bool(sp["is_floor"])
        bp = bake_plane_observed_texture(
            sp,
            vis_list,
            gains_srgb,
            depth_tol_m=0.065 if is_f else 0.075,
            sharp_exponent=SHARP_EXPONENT_FLOOR_R3 if is_f else SHARP_EXPONENT_WALL_R3,
            detect_see_through=not is_f,
            see_through_tol_m=SEE_THROUGH_TOL_M,
            wall_neighbor_depth_tol_m=None if is_f else 0.14,
        )
        baked_planes.append(bp)
        plane_obs_masks[sp["role"]] = bp["obs_mask_2d"]
        print(
            f"[r3:{mesh_key}] 평면 관측/개구부 굽기 [{sp['role']}]: 해상도 {sp['tex_hw'][1]}x{sp['tex_hw'][0]} "
            f"({sp['texel_m']*1000:.1f}mm/texel), 관측={bp['observed_ratio']*100:.1f}%, "
            f"개구부(see_through)={bp['see_through_ratio']*100:.1f}%({bp['see_through_area_m2']} m2), "
            f"채움대상={bp['unobserved_ratio']*100:.1f}%",
            flush=True,
        )
    shared_obs_seconds = round(vis_seconds + (time.perf_counter() - t_obs0), 3)

    # 5) 강화된 평면 위 메시 걷어내기:
    #    삼각형 아래 받침 평면이 observed_projection >= 90% 인 곳에서만 걷어내고,
    #    filled / see_through 구간 위의 관측 텍스처 삼각형(예: 왼쪽 끝 액자, 벽 끝단)은 그대로 보존한다.
    keep_stripped, strip_rep = strip_planar_mesh_faces(
        merged_mesh.V,
        merged_mesh.F,
        keep_comp,
        walls_doc["floor_plane"],
        walls_doc["wall_planes"],
        dist_tol_m=PLANE_STRIP_DIST_M,
        angle_tol_deg=PLANE_STRIP_ANGLE_DEG,
        post_min_faces=150,
        post_min_area_m2=0.12,
        plane_obs_masks=plane_obs_masks,
        min_obs_coverage=MIN_OBS_COVERAGE_R3,
        is_textured_face=is_textured_face,
    )
    print(
        f"[r3:{mesh_key}] 강화된 평면 걷어내기 완료: 기하후보 {strip_rep['geom_candidate_faces']}면 중 "
        f"관측>=90% {strip_rep['plane_stripped_faces']}면({strip_rep['plane_stripped_area_m2']} m2) 제거, "
        f"미관측받침 위 보존 {strip_rep['kept_unobserved_backing_faces']}면({strip_rep['kept_unobserved_backing_area_m2']} m2) 유지 -> "
        f"최종 유지 {strip_rep['kept_faces']}/{strip_rep['total_mesh_faces']}면 ({strip_rep['kept_face_ratio']*100:.1f}%)",
        flush=True,
    )

    # 6) 걷어낸 뒤 남은 메시와 받침 평면이 만나는 열린 경계 거리(10 cm) 계산
    kept_F_r3 = merged_mesh.F[keep_stripped]
    bdist_r3, bdist_r3_stats = compute_open_boundary_distances(merged_mesh.V, kept_F_r3, weld_tol_m=0.001, max_dist_m=0.14)

    # 7) 평면별 채움(tone / lama) + 경계 Lab 색 통계 맞춤 + 10 cm 페더링 (see_through 영역은 채우지 않고 투명/제외 유지)
    tone_results: list[dict[str, Any]] = []
    lama_results: list[dict[str, Any]] = []
    hybrid_results: list[dict[str, Any]] = []
    lab_match_stats: dict[str, Any] = {}
    tone_inpaint_s = 0.0
    lama_inpaint_s = 0.0
    hybrid_inpaint_s = 0.0

    for sp, bp in zip(plane_specs, baked_planes):
        role = sp["role"]
        obs_albedo = bp["albedo"]
        inpaint_mask = bp["inpaint_mask"]
        obs_source = bp["source"]
        st_mask_2d = bp["see_through_mask_2d"]
        texel_m = float(sp["texel_m"])

        # (i) Tone + Lab 경계 보정 + 10 cm 페더링
        t0 = time.perf_counter()
        raw_tone = inpaint_tone_plane(obs_albedo, inpaint_mask, obs_source, texel_m=texel_m, with_grid=False)
        matched_tone, t_lab_meta = match_fill_to_boundary_lab(
            obs_albedo, raw_tone, inpaint_mask, band_m=LAB_MATCH_BAND_M, texel_m=texel_m
        )
        feath_tone, src_tone = feather_observed_filled_boundary(
            obs_albedo,
            matched_tone,
            inpaint_mask,
            feather_m=PLANE_FEATHER_R2_M,
            texel_m=texel_m,
            see_through_mask=st_mask_2d,
        )
        dt_tone = time.perf_counter() - t0
        tone_inpaint_s += dt_tone
        t_entry = {
            "role": role,
            "image": feath_tone,
            "source": src_tone,
            "surface_source_summary": compute_surface_source_summary(src_tone, "tone", texel_m),
        }
        tone_results.append(t_entry)

        # (ii) LaMa + Lab 경계 보정 + 10 cm 페더링
        t0 = time.perf_counter()
        raw_lama, l_meta = run_lama_via_pose_cuda_or_local(
            obs_albedo,
            inpaint_mask,
            obs_source,
            lama_weights,
            out_dir / f"_tmp_lama_r3_{mesh_key}",
            role=f"r3_{mesh_key}_{role}",
            existing_plane_png=None,
        )
        matched_lama, l_lab_meta = match_fill_to_boundary_lab(
            obs_albedo, raw_lama, inpaint_mask, band_m=LAB_MATCH_BAND_M, texel_m=texel_m
        )
        feath_lama, src_lama = feather_observed_filled_boundary(
            obs_albedo,
            matched_lama,
            inpaint_mask,
            feather_m=PLANE_FEATHER_R2_M,
            texel_m=texel_m,
            see_through_mask=st_mask_2d,
        )
        dt_lama = float(l_meta.get("measured_seconds", time.perf_counter() - t0))
        lama_inpaint_s += dt_lama
        l_entry = {
            "role": role,
            "image": feath_lama,
            "source": src_lama,
            "surface_source_summary": compute_surface_source_summary(src_lama, "lama", texel_m),
        }
        lama_results.append(l_entry)

        # (iii) Hybrid: 바닥은 tone, 벽 3면은 lama (`hybrid_tone_lama`)
        if role == "floor":
            hybrid_results.append(
                {
                    "role": role,
                    "image": feath_tone,
                    "source": src_tone,
                    "surface_source_summary": compute_surface_source_summary(src_tone, "hybrid_tone_lama", texel_m),
                }
            )
            hybrid_inpaint_s += dt_tone
        else:
            hybrid_results.append(
                {
                    "role": role,
                    "image": feath_lama,
                    "source": src_lama,
                    "surface_source_summary": compute_surface_source_summary(src_lama, "hybrid_tone_lama", texel_m),
                }
            )
            hybrid_inpaint_s += dt_lama

        lab_match_stats[role] = {"tone": t_lab_meta, "lama": l_lab_meta}
        cv2.imwrite(str(planes_dir / f"r3_{mesh_key}_{role}_tone.png"), feath_tone[:, :, ::-1])
        cv2.imwrite(str(planes_dir / f"r3_{mesh_key}_{role}_lama.png"), feath_lama[:, :, ::-1])
        cv2.imwrite(str(planes_dir / f"r3_{mesh_key}_{role}_source.png"), src_lama)

    shutil.rmtree(out_dir / f"_tmp_lama_r3_{mesh_key}", ignore_errors=True)

    # 8) 2종 GLB 조립 및 저장: `scene_r3_{mesh_key}_hybrid.glb`, `scene_r3_{mesh_key}_tone.glb`
    r3_variants_table: list[dict[str, Any]] = []
    for vk_sub, res_list, inp_s, canon_lbl in [
        ("hybrid", hybrid_results, hybrid_inpaint_s, "hybrid_tone_lama"),
        ("tone", tone_results, tone_inpaint_s, "tone"),
    ]:
        t_asm0 = time.perf_counter()
        glb_bytes, surf_meta = assemble_variant_glb(
            raw_glb,
            copy.deepcopy(model),
            keep_stripped,
            merged_mesh,
            bdist_r3,
            plane_specs,
            res_list,
            walls_doc["floor_plane"],
            walls_doc["wall_planes"],
            canon_lbl,
            extra_report={
                "round": 3,
                "mesh_source": mesh_key,
                "sub_variant": vk_sub,
                "floating_fragment_removal": comp_rep,
                "plane_stripping": strip_rep,
                "open_boundary_stats_after_strip": bdist_r3_stats,
                "lab_boundary_match": lab_match_stats,
                "da3_recolor": recolor_meta,
            },
            mesh_feather_m=MESH_BOUNDARY_FEATHER_R2_M,
            plane_feather_m=PLANE_FEATHER_R2_M,
        )
        asm_s = time.perf_counter() - t_asm0
        fname = f"scene_r3_{mesh_key}_{vk_sub}.glb"
        (out_dir / fname).write_bytes(glb_bytes)

        total_s = round(shared_obs_seconds + inp_s + asm_s, 3)
        glb_mb = round(len(glb_bytes) / (1024.0 * 1024.0), 2)
        filled_key = f"filled_{canon_lbl}"
        row = {
            "mesh_source": mesh_key,
            "variant": f"r3_{mesh_key}_{vk_sub}",
            "fill_mode": canon_lbl,
            "glb_filename": fname,
            "glb_bytes": len(glb_bytes),
            "glb_size_mb": glb_mb,
            "shared_projection_seconds": shared_obs_seconds,
            "inpaint_and_assemble_seconds": round(inp_s + asm_s, 3),
            "total_bake_seconds": total_s,
            "observed_projection_ratio": surf_meta["area_ratios"]["observed_projection"],
            "filled_ratio": surf_meta["area_ratios"][filled_key],
            "see_through_ratio": surf_meta["area_ratios"].get("see_through", 0.0),
            "observed_area_m2": surf_meta["area_m2"]["observed_projection"],
            "filled_area_m2": surf_meta["area_m2"][filled_key],
            "see_through_area_m2": surf_meta["area_m2"].get("see_through", 0.0),
            "kept_mesh_faces": strip_rep["kept_faces"],
            "kept_unobserved_backing_faces": strip_rep["kept_unobserved_backing_faces"],
            "stripped_mesh_faces": strip_rep["total_removed_faces"],
            "stripped_mesh_face_ratio": strip_rep["total_removed_face_ratio"],
            "stripped_mesh_area_m2": strip_rep["total_removed_area_m2"],
            "stripped_mesh_area_ratio": strip_rep["total_removed_area_ratio"],
            "per_plane_surface_source": surf_meta["per_plane"],
        }
        r3_variants_table.append(row)
        print(
            f"[r3:{mesh_key}] GLB 저장 완료: {fname} ({glb_mb} MB, 총 {total_s}s, "
            f"남은 메시={strip_rep['kept_faces']}면, 걷어낸 면적={strip_rep['total_removed_area_m2']} m2, "
            f"see_through={row['see_through_area_m2']} m2)",
            flush=True,
        )

    res_table_r3 = None
    if mesh_key == "da3":
        res_table_r3 = evaluate_r3_wall_resolution_table(
            merged_mesh, keep_for_vis, walls_doc, vis_list, gains_srgb, len(raw_glb)
        )

    return {
        "mesh_source": mesh_key,
        "input_glb": str(input_glb_path.as_posix()),
        "input_glb_size_mb": round(input_glb_path.stat().st_size / (1024.0 * 1024.0), 2),
        "floating_fragment_removal": comp_rep,
        "plane_stripping": strip_rep,
        "open_boundary_after_strip": bdist_r3_stats,
        "plane_observation_stats": [
            {
                "role": bp["role"],
                "tex_size_wh": [plane_specs[i]["tex_hw"][1], plane_specs[i]["tex_hw"][0]],
                "texel_m": plane_specs[i]["texel_m"],
                "area_m2": plane_specs[i]["area_m2"],
                "observed_ratio": bp["observed_ratio"],
                "see_through_ratio": bp["see_through_ratio"],
                "see_through_area_m2": bp["see_through_area_m2"],
                "unobserved_ratio": bp["unobserved_ratio"],
                "bake_observed_seconds": bp["bake_observed_seconds"],
                "outlier_stats": bp["outlier_stats"],
            }
            for i, bp in enumerate(baked_planes)
        ],
        "wall_resolution_capacity_table_r3": res_table_r3,
        "lab_boundary_match": lab_match_stats,
        "variants": r3_variants_table,
        "elapsed_seconds": round(time.perf_counter() - t_m0, 3),
    }


def _build_furniture_cameras_and_depths(
    mesh_key: str,
    check_cams: list[ge.CheckCamera],
    gains_srgb: np.ndarray,
    vis_list: list[gf.CamVis],
) -> tuple[list[ge.CheckCamera], list[np.ndarray]]:
    """5차 지시: 메시 좌표계에 정합된 카메라 포즈(`wm2`는 `ma_sim3`, `da3`는 `check_cams`)와 카메라별 깊이 맵(m)을 준비한다."""
    ma_root = LOCAL_ROOT / "align-535" / "ma_single"
    wm2_pose_dir = LOCAL_ROOT / "depth-537" / "depth_adapters_537" / "wm2_pose"
    if mesh_key == "wm2" and ma_root.is_dir() and wm2_pose_dir.is_dir():
        try:
            from simai_ai.scene import mesh_cleanup as mc
            from simai_ai.scene import run_hybrid_depth as hd
            from simai_ai.scene import texture_mesh as tm

            ma_run = ma_root / "mapanything"
            prep = hd.load_prepared(ma_root / "prepared")
            cams_ma, sha = hd.read_cameras(ma_run)
            M, _ = hd.load_world_alignment(ma_root / "world_alignment.json", sha)
            s_scale = tm.unit_scale(M)
            A = np.asarray(M, dtype=np.float64)[:3, :3]
            b = np.asarray(M, dtype=np.float64)[:3, 3]
            dviews, _ = tm.load_depth_views(ma_run, prep, cams_ma, wm2_pose_dir, sha, "scale")
            res = mc.align_views(dviews, mc.AlignConfig(mode="sim3"))
            dviews_sim3 = {v.name: v for v in res.views}

            sim3_cams: list[ge.CheckCamera] = []
            cam_depths: list[np.ndarray] = []
            for k, c in enumerate(check_cams):
                dv = dviews_sim3[c.camera_id]
                c2w_ma = np.asarray(dv.cam2world, dtype=np.float64)
                R_ma = c2w_ma[:3, :3]
                s_r = float(np.linalg.norm(R_ma[:, 0]))
                R_c2w_w = (A / s_scale) @ (R_ma / s_r)
                U, _, Vt = np.linalg.svd(R_c2w_w)
                R_c2w_w = U @ Vt
                pos_w = c2w_ma[:3, 3] @ A.T + b
                R_w2c = R_c2w_w.T
                t_w2c = -R_w2c @ pos_w
                Hc, Wc = c.image.shape[:2]
                Hd, Wd = dv.depth.shape[:2]
                K_sim3 = np.asarray(dv.K, dtype=np.float64).copy()
                K_sim3[0, :] *= Wc / float(Wd)
                K_sim3[1, :] *= Hc / float(Hd)
                gain = gains_srgb[k].reshape(1, 1, 3)
                img_g = np.clip(c.image.astype(np.float32) * gain, 0.0, 255.0).astype(np.uint8)
                sim3_cams.append(ge.CheckCamera(camera_id=c.camera_id, K=K_sim3, R=R_w2c, t=t_w2c, image=img_g, valid=None))
                d_m = (dv.depth * s_scale * s_r).astype(np.float32)
                d_m[~dv.valid] = np.nan
                d_m_res = cv2.resize(d_m, (Wc, Hc), interpolation=cv2.INTER_NEAREST)
                cam_depths.append(d_m_res)
            return sim3_cams, cam_depths
        except Exception as exc:
            print(f"[warn] ma_sim3 포즈 로드 실패, 기본 포즈로 폴백: {exc}", flush=True)

    fallback_cams: list[ge.CheckCamera] = []
    fallback_depths: list[np.ndarray] = []
    for k, (c, v) in enumerate(zip(check_cams, vis_list)):
        Hc, Wc = c.image.shape[:2]
        gain = gains_srgb[k].reshape(1, 1, 3)
        img_g = np.clip(c.image.astype(np.float32) * gain, 0.0, 255.0).astype(np.uint8)
        fallback_cams.append(ge.CheckCamera(camera_id=c.camera_id, K=c.K, R=c.R, t=c.t, image=img_g, valid=c.valid))
        d_vis = v.depth.astype(np.float32).copy()
        d_vis[d_vis <= 0.0] = np.nan
        d_res = cv2.resize(d_vis, (Wc, Hc), interpolation=cv2.INTER_NEAREST)
        fallback_depths.append(d_res)
    return fallback_cams, fallback_depths


def bake_r4_final_for_input_mesh(
    mesh_key: str,
    input_glb_path: Path,
    raw_glb: bytes,
    walls_doc: dict[str, Any],
    check_cams: list[ge.CheckCamera],
    gains_srgb: np.ndarray,
    cam_quality_maps: list[dict[str, Any]],
    lama_weights: Path,
    final_out_dir: Path,
    planes_dir: Path,
    *,
    also_save_uncompressed: bool = False,
    jpeg_quality: int = 92,
) -> dict[str, Any]:
    """4·5차 지시(최종 GLB): 1~3차 채택안 통합 + 사진 품질 가중치 + 5차 수평 가구 상판 평면 굽기·남은 가구 면 일관성 선택 + 텍스처 압축을 수행해 `scene_final_{mesh_key}.glb` 를 생성한다."""
    t_m0 = time.perf_counter()
    recolor_meta: dict[str, Any] = {}
    if mesh_key in ("wm2", "da3"):
        raw_glb, recolor_meta = preprocess_da3_clean_glb(raw_glb)

    g_init = ge.parse_glb(raw_glb)
    simai_init = (g_init.doc.get("extras") or {}).get("simai")
    if isinstance(simai_init, dict):
        simai_init.pop("enhanced", None)
    model = ge.load_scene(g_init)
    merged_mesh = gf.merge_mesh(model.prims)

    is_textured_face = np.zeros(len(merged_mesh.F), dtype=bool)
    for p_idx, prim in enumerate(model.prims):
        if prim.kind == "observed" and prim.image is not None and prim.uv is not None:
            is_textured_face[merged_mesh.face_prim == p_idx] = True

    # 1) 초기 떠다니는 작은 조각 제거 (min_faces=300, min_area_m2=0.25)
    keep_comp, comp_rep = filter_small_connected_components(
        merged_mesh.V, merged_mesh.F, min_faces=MIN_COMPONENT_FACES, min_area_m2=MIN_COMPONENT_AREA_M2
    )

    # 2) 카메라 깊이 그림자 맵 렌더링 (관측 텍스처가 있는 실제 기하 면 기준)
    t_vis0 = time.perf_counter()
    keep_for_vis = keep_comp & is_textured_face
    vis_list = [gf.render_visibility(merged_mesh, c, 1536, keep=keep_for_vis) for c in check_cams]
    vis_seconds = round(time.perf_counter() - t_vis0, 3)

    # 3) 받침 평면 사양 생성 (바닥 5 mm/texel, 벽 3 mm/texel + IRLS 로버스트 1차 평면 적합)
    floor_spec = build_floor_backing_spec(merged_mesh, keep_for_vis, walls_doc["floor_plane"], texel_m=TEXEL_M)
    wall_specs = [
        build_wall_backing_spec(merged_mesh, keep_for_vis, wp, texel_m=WALL_TEXEL_R3_M, use_linear_plane_r3=True)
        for wp in walls_doc["wall_planes"]
    ]
    plane_specs = [floor_spec, *wall_specs]

    # 4) 평면 관측 투영 (기하 가중치 × 사진 품질 가중치 + 공간 스무딩 단일 카메라 지배 선택) 및 see_through 판정
    baked_planes: list[dict[str, Any]] = []
    plane_obs_masks: dict[str, np.ndarray] = {}
    t_obs0 = time.perf_counter()
    for sp in plane_specs:
        is_f = bool(sp["is_floor"])
        bp = bake_plane_observed_texture(
            sp,
            vis_list,
            gains_srgb,
            depth_tol_m=0.065 if is_f else 0.075,
            sharp_exponent=SHARP_EXPONENT_FLOOR_R3 if is_f else SHARP_EXPONENT_WALL_R3,
            detect_see_through=not is_f,
            see_through_tol_m=SEE_THROUGH_TOL_M,
            wall_neighbor_depth_tol_m=None if is_f else 0.14,
            cam_quality_maps=cam_quality_maps,
        )
        baked_planes.append(bp)
        plane_obs_masks[sp["role"]] = bp["obs_mask_2d"]
        print(
            f"[r4:{mesh_key}] 평면 관측/개구부 굽기 [{sp['role']}]: 해상도 {sp['tex_hw'][1]}x{sp['tex_hw'][0]} "
            f"({sp['texel_m']*1000:.1f}mm/texel), 관측={bp['observed_ratio']*100:.1f}%, "
            f"개구부(see_through)={bp['see_through_ratio']*100:.1f}%({bp['see_through_area_m2']} m2), "
            f"채움대상={bp['unobserved_ratio']*100:.1f}%",
            flush=True,
        )
    shared_obs_seconds = round(vis_seconds + (time.perf_counter() - t_obs0), 3)

    # 5) 강화된 평면 위 메시 걷어내기 (받침 평면 observed_projection >= 90% 인 곳만 걷어내고, filled/see_through 위 액자·명패 보존)
    keep_stripped, strip_rep = strip_planar_mesh_faces(
        merged_mesh.V,
        merged_mesh.F,
        keep_comp,
        walls_doc["floor_plane"],
        walls_doc["wall_planes"],
        dist_tol_m=PLANE_STRIP_DIST_M,
        angle_tol_deg=PLANE_STRIP_ANGLE_DEG,
        post_min_faces=150,
        post_min_area_m2=0.12,
        plane_obs_masks=plane_obs_masks,
        min_obs_coverage=MIN_OBS_COVERAGE_R3,
        is_textured_face=is_textured_face,
    )
    print(
        f"[r4:{mesh_key}] 강화된 평면 걷어내기 완료: 기하후보 {strip_rep['geom_candidate_faces']}면 중 "
        f"관측>=90% {strip_rep['plane_stripped_faces']}면({strip_rep['plane_stripped_area_m2']} m2) 제거, "
        f"미관측받침 위 보존 {strip_rep['kept_unobserved_backing_faces']}면({strip_rep['kept_unobserved_backing_area_m2']} m2) 유지 -> "
        f"최종 유지 {strip_rep['kept_faces']}/{strip_rep['total_mesh_faces']}면 ({strip_rep['kept_face_ratio']*100:.1f}%)",
        flush=True,
    )

    # 5.5) 5차 지시 (a) + (b): 수평 가구 상판·벤치 좌판 평면 추출·텍스처 굽기 및 남은 가구 면 최고 카메라 일관성 선택
    t_furn0 = time.perf_counter()
    furn_cams, furn_depths_m = _build_furniture_cameras_and_depths(mesh_key, check_cams, gains_srgb, vis_list)
    keep_after_furn, furniture_planes, furn_plane_rep = extract_and_bake_horizontal_furniture_planes(
        merged_mesh.V,
        merged_mesh.F,
        keep_stripped,
        furn_cams,
        furn_depths_m,
        cam_quality_maps,
    )
    furn_face_srgb, is_furn_face, furn_face_rep = recolor_remaining_furniture_faces(
        merged_mesh.V,
        merged_mesh.F,
        keep_after_furn,
        furn_cams,
        furn_depths_m,
        cam_quality_maps,
        furniture_planes=furniture_planes,
    )
    furn_seconds = round(time.perf_counter() - t_furn0, 3)
    shared_obs_seconds = round(shared_obs_seconds + furn_seconds, 3)
    for fp in furniture_planes:
        cv2.imwrite(str(planes_dir / f"final_{mesh_key}_{fp['role']}_albedo.png"), fp["image"][:, :, ::-1])
    print(
        f"[r5:{mesh_key}] 수평 가구 상판 평면 {furn_plane_rep['num_furniture_planes']}개 "
        f"({furn_plane_rep['absorbed_faces']}면, {furn_plane_rep['absorbed_area_m2']} m2) 추출·굽기 및 "
        f"남은 가구 {furn_face_rep['furniture_faces']}면 일관성 선택 완료 ({furn_seconds}s)",
        flush=True,
    )

    # 6) 열린 경계 거리(10 cm) 계산 (수평 가구 상판 분리 전 keep_stripped 기준으로 계산하여 가구 상판 테두리가 바닥 구멍으로 오인되지 않게 함)
    kept_F_r4 = merged_mesh.F[keep_stripped]
    bdist_r4, bdist_r4_stats = compute_open_boundary_distances(merged_mesh.V, kept_F_r4, weld_tol_m=0.001, max_dist_m=0.14)

    # 7) 채택 조합(`hybrid_tone_lama`: 바닥=tone, 벽 3면=lama) 채움 + 경계 Lab 색 통계 맞춤 + 10 cm 페더링
    hybrid_results: list[dict[str, Any]] = []
    lab_match_stats: dict[str, Any] = {}
    hybrid_inpaint_s = 0.0

    for sp, bp in zip(plane_specs, baked_planes):
        role = sp["role"]
        obs_albedo = bp["albedo"]
        inpaint_mask = bp["inpaint_mask"]
        obs_source = bp["source"]
        st_mask_2d = bp["see_through_mask_2d"]
        texel_m = float(sp["texel_m"])

        if role == "floor":
            t0 = time.perf_counter()
            raw_tone = inpaint_tone_plane(obs_albedo, inpaint_mask, obs_source, texel_m=texel_m, with_grid=False)
            matched_tone, t_lab_meta = match_fill_to_boundary_lab(
                obs_albedo, raw_tone, inpaint_mask, band_m=LAB_MATCH_BAND_M, texel_m=texel_m
            )
            feath_img, src_map = feather_observed_filled_boundary(
                obs_albedo,
                matched_tone,
                inpaint_mask,
                feather_m=PLANE_FEATHER_R2_M,
                texel_m=texel_m,
                see_through_mask=st_mask_2d,
            )
            dt_inp = time.perf_counter() - t0
            hybrid_inpaint_s += dt_inp
            lab_match_stats[role] = {"mode": "tone", **t_lab_meta}
        else:
            t0 = time.perf_counter()
            existing_albedo_png = planes_dir / f"final_{mesh_key}_{role}_albedo.png"
            raw_lama, l_meta = run_lama_via_pose_cuda_or_local(
                obs_albedo,
                inpaint_mask,
                obs_source,
                lama_weights,
                final_out_dir / f"_tmp_lama_r4_{mesh_key}",
                role=f"r4_{mesh_key}_{role}",
                existing_plane_png=existing_albedo_png if existing_albedo_png.is_file() else None,
            )
            matched_lama, l_lab_meta = match_fill_to_boundary_lab(
                obs_albedo, raw_lama, inpaint_mask, band_m=LAB_MATCH_BAND_M, texel_m=texel_m
            )
            feath_img, src_map = feather_observed_filled_boundary(
                obs_albedo,
                matched_lama,
                inpaint_mask,
                feather_m=PLANE_FEATHER_R2_M,
                texel_m=texel_m,
                see_through_mask=st_mask_2d,
            )
            dt_inp = float(l_meta.get("measured_seconds", time.perf_counter() - t0))
            hybrid_inpaint_s += dt_inp
            lab_match_stats[role] = {"mode": "lama", **l_lab_meta}

        hybrid_results.append(
            {
                "role": role,
                "image": feath_img,
                "source": src_map,
                "surface_source_summary": compute_surface_source_summary(src_map, "hybrid_tone_lama", texel_m),
            }
        )
        cv2.imwrite(str(planes_dir / f"final_{mesh_key}_{role}_albedo.png"), feath_img[:, :, ::-1])
        cv2.imwrite(str(planes_dir / f"final_{mesh_key}_{role}_source.png"), src_map)

    shutil.rmtree(final_out_dir / f"_tmp_lama_r4_{mesh_key}", ignore_errors=True)

    # 8) 압축 최종 GLB 조립 및 저장 (`scene_final_{mesh_key}.glb`)
    t_asm0 = time.perf_counter()
    comp_glb_bytes, surf_meta = assemble_variant_glb(
        raw_glb,
        copy.deepcopy(model),
        keep_after_furn,
        merged_mesh,
        bdist_r4,
        plane_specs,
        hybrid_results,
        walls_doc["floor_plane"],
        walls_doc["wall_planes"],
        "hybrid_tone_lama",
        extra_report={
            "round": 5,
            "mesh_source": mesh_key,
            "compressed": True,
            "jpeg_quality": jpeg_quality,
            "floating_fragment_removal": comp_rep,
            "plane_stripping": strip_rep,
            "horizontal_furniture_planes": furn_plane_rep,
            "remaining_furniture_faces": furn_face_rep,
            "open_boundary_stats_after_strip": bdist_r4_stats,
            "lab_boundary_match": lab_match_stats,
            "recolor": recolor_meta,
        },
        mesh_feather_m=MESH_BOUNDARY_FEATHER_R2_M,
        plane_feather_m=PLANE_FEATHER_R2_M,
        compress_textures=True,
        jpeg_quality=jpeg_quality,
        furniture_planes=furniture_planes,
        furniture_face_srgb=furn_face_srgb,
        is_furniture_face=is_furn_face,
    )
    comp_asm_s = time.perf_counter() - t_asm0
    comp_fname = f"scene_final_{mesh_key}.glb"
    (final_out_dir / comp_fname).write_bytes(comp_glb_bytes)
    comp_mb = round(len(comp_glb_bytes) / (1024.0 * 1024.0), 2)
    comp_total_s = round(shared_obs_seconds + hybrid_inpaint_s + comp_asm_s, 3)
    print(
        f"[r5:{mesh_key}] 압축 최종 GLB 저장 완료: {comp_fname} ({comp_mb} MB, 총 {comp_total_s}s)",
        flush=True,
    )

    uncomp_info = None
    if also_save_uncompressed:
        t_u0 = time.perf_counter()
        uncomp_glb_bytes, _ = assemble_variant_glb(
            raw_glb,
            copy.deepcopy(model),
            keep_after_furn,
            merged_mesh,
            bdist_r4,
            plane_specs,
            hybrid_results,
            walls_doc["floor_plane"],
            walls_doc["wall_planes"],
            "hybrid_tone_lama",
            extra_report={
                "round": 5,
                "mesh_source": mesh_key,
                "compressed": False,
                "floating_fragment_removal": comp_rep,
                "plane_stripping": strip_rep,
                "horizontal_furniture_planes": furn_plane_rep,
                "remaining_furniture_faces": furn_face_rep,
                "open_boundary_stats_after_strip": bdist_r4_stats,
                "lab_boundary_match": lab_match_stats,
                "recolor": recolor_meta,
            },
            mesh_feather_m=MESH_BOUNDARY_FEATHER_R2_M,
            plane_feather_m=PLANE_FEATHER_R2_M,
            compress_textures=False,
            furniture_planes=furniture_planes,
            furniture_face_srgb=furn_face_srgb,
            is_furniture_face=is_furn_face,
        )
        uncomp_asm_s = time.perf_counter() - t_u0
        uncomp_fname = f"scene_final_{mesh_key}_uncompressed.glb"
        (final_out_dir / uncomp_fname).write_bytes(uncomp_glb_bytes)
        uncomp_mb = round(len(uncomp_glb_bytes) / (1024.0 * 1024.0), 2)
        uncomp_total_s = round(shared_obs_seconds + hybrid_inpaint_s + uncomp_asm_s, 3)
        uncomp_info = {
            "glb_filename": uncomp_fname,
            "glb_bytes": len(uncomp_glb_bytes),
            "glb_size_mb": uncomp_mb,
            "total_bake_seconds": uncomp_total_s,
        }
        print(
            f"[r5:{mesh_key}] 비압축 비교용 GLB 저장 완료: {uncomp_fname} ({uncomp_mb} MB, 총 {uncomp_total_s}s)",
            flush=True,
        )

    filled_key = "filled_hybrid_tone_lama"
    return {
        "mesh_source": mesh_key,
        "input_glb": str(input_glb_path.as_posix()),
        "input_glb_size_mb": round(input_glb_path.stat().st_size / (1024.0 * 1024.0), 2),
        "compressed_glb": {
            "glb_filename": comp_fname,
            "glb_bytes": len(comp_glb_bytes),
            "glb_size_mb": comp_mb,
            "jpeg_quality": jpeg_quality,
            "shared_projection_seconds": shared_obs_seconds,
            "inpaint_seconds": round(hybrid_inpaint_s, 3),
            "assemble_seconds": round(comp_asm_s, 3),
            "total_bake_seconds": comp_total_s,
        },
        "uncompressed_glb": uncomp_info,
        "floating_fragment_removal": comp_rep,
        "plane_stripping": strip_rep,
        "horizontal_furniture_planes": furn_plane_rep,
        "remaining_furniture_faces": furn_face_rep,
        "open_boundary_after_strip": bdist_r4_stats,
        "surface_source": {
            "observed_projection_ratio": surf_meta["area_ratios"]["observed_projection"],
            "filled_ratio": surf_meta["area_ratios"][filled_key],
            "see_through_ratio": surf_meta["area_ratios"].get("see_through", 0.0),
            "observed_area_m2": surf_meta["area_m2"]["observed_projection"],
            "filled_area_m2": surf_meta["area_m2"][filled_key],
            "see_through_area_m2": surf_meta["area_m2"].get("see_through", 0.0),
            "per_plane": surf_meta["per_plane"],
        },
        "plane_observation_stats": [
            {
                "role": bp["role"],
                "tex_size_wh": [plane_specs[i]["tex_hw"][1], plane_specs[i]["tex_hw"][0]],
                "texel_m": plane_specs[i]["texel_m"],
                "area_m2": plane_specs[i]["area_m2"],
                "observed_ratio": bp["observed_ratio"],
                "see_through_ratio": bp["see_through_ratio"],
                "see_through_area_m2": bp["see_through_area_m2"],
                "unobserved_ratio": bp["unobserved_ratio"],
                "bake_observed_seconds": bp["bake_observed_seconds"],
                "outlier_stats": bp["outlier_stats"],
            }
            for i, bp in enumerate(baked_planes)
        ],
        "lab_boundary_match": lab_match_stats,
        "elapsed_seconds": round(time.perf_counter() - t_m0, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="S15P31A101-532 받침 평면 굽기 및 미관측 채움 변형 비교")
    parser.add_argument("--vdt-dir", type=Path, default=DEFAULT_VDT_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--final-out-dir", type=Path, default=DEFAULT_FINAL_OUT_DIR)
    parser.add_argument("--lama-weights", type=Path, default=DEFAULT_LAMA_WEIGHTS)
    parser.add_argument("--da3-glb", type=Path, default=DEFAULT_DA3_GLB)
    parser.add_argument("--wm2-glb", type=Path, default=DEFAULT_WM2_GLB)
    parser.add_argument("--round2-only", action="store_true", help="1차 GLB가 이미 있을 때 2차(평면 걷어내기 + 색 일관성 + DA3)만 빠르게 실행")
    parser.add_argument("--round3-only", action="store_true", help="1·2차 GLB가 이미 있을 때 3차(강화된 걷어내기 + 3mm 벽 + 단일 카메라 선명화 + see_through)만 실행")
    parser.add_argument("--round4-only", action="store_true", help="4차 최종 GLB(WM2 + DA3 + 사진 품질 가중치 + 텍스처 압축 ~30MB) 실행")
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    planes_dir = out_dir / "planes"
    planes_dir.mkdir(parents=True, exist_ok=True)

    t_start = time.perf_counter()
    raw_glb, cams_doc, walls_doc, check_cams, gains_srgb = load_vdt_inputs(args.vdt_dir)

    # 비교 뷰어를 위해 원본 GLB 및 cameras.json 복사 (wall_close, ssafy_sign, left_frame, door_corridor 근접 시점 추가)
    orig_glb_dst = out_dir / "scene_orig.glb"
    shutil.copy2(args.vdt_dir / "current-scene-textured.glb", orig_glb_dst)
    cams_out_doc = copy.deepcopy(cams_doc)
    fviews = cams_out_doc.setdefault("free_views", {})
    fviews["wall_close"] = {
        "name": "wall_close",
        "position_glb": [2.85, 1.38, -4.15],
        "target_glb": [0.05, 1.28, -4.35],
        "up_glb": [0.0, 1.0, 0.0],
        "hfov_deg": 75.0,
        "vfov_deg": 48.0,
        "position_world": [2.85, 4.15, 1.38],
        "target_world": [0.05, 4.35, 1.28],
    }
    fviews["ssafy_sign"] = {
        "name": "ssafy_sign",
        "position_glb": [3.55, 1.45, -2.05],
        "target_glb": [3.55, 1.45, 0.14],
        "up_glb": [0.0, 1.0, 0.0],
        "hfov_deg": 65.0,
        "vfov_deg": 42.0,
        "position_world": [3.55, 2.05, 1.45],
        "target_world": [3.55, -0.14, 1.45],
    }
    fviews["left_frame"] = {
        "name": "left_frame",
        "position_glb": [2.25, 1.45, -1.70],
        "target_glb": [0.05, 1.45, -1.68],
        "up_glb": [0.0, 1.0, 0.0],
        "hfov_deg": 68.0,
        "vfov_deg": 44.0,
        "position_world": [2.25, 1.70, 1.45],
        "target_world": [0.05, 1.68, 1.45],
    }
    fviews["door_corridor"] = {
        "name": "door_corridor",
        "position_glb": [2.55, 1.42, -8.85],
        "target_glb": [0.05, 1.38, -9.25],
        "up_glb": [0.0, 1.0, 0.0],
        "hfov_deg": 72.0,
        "vfov_deg": 46.0,
        "position_world": [2.55, 8.85, 1.42],
        "target_world": [0.05, 9.25, 1.38],
    }
    (out_dir / "cameras.json").write_text(json.dumps(cams_out_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(args.vdt_dir / "walls.json", out_dir / "walls.json")

    prev_rep_path = out_dir / "report.json"
    if args.round4_only:
        final_dir: Path = args.final_out_dir
        final_dir.mkdir(parents=True, exist_ok=True)
        final_planes_dir = final_dir / "planes"
        final_planes_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(args.vdt_dir / "current-scene-textured.glb", final_dir / "scene_orig.glb")
        (final_dir / "cameras.json").write_text(json.dumps(cams_out_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copy2(args.vdt_dir / "walls.json", final_dir / "walls.json")

        t_q0 = time.perf_counter()
        cam_q_maps = [compute_camera_image_quality_maps(c.image, patch_size_px=17) for c in check_cams]
        q_seconds = round(time.perf_counter() - t_q0, 3)
        cam_quality_stats = {check_cams[k].camera_id: cam_q_maps[k]["stats"] for k in range(len(check_cams))}
        print(f"[r4] 카메라 4대 사진 품질 지도(라플라시안 선명도·포화 노출) 계산 완료 ({q_seconds}s)", flush=True)

        r4_wm2 = None
        if args.wm2_glb.is_file():
            r4_wm2 = bake_r4_final_for_input_mesh(
                "wm2",
                args.wm2_glb,
                args.wm2_glb.read_bytes(),
                walls_doc,
                check_cams,
                gains_srgb,
                cam_q_maps,
                args.lama_weights,
                final_dir,
                final_planes_dir,
                also_save_uncompressed=True,
                jpeg_quality=92,
            )
        r4_da3 = None
        if args.da3_glb.is_file():
            r4_da3 = bake_r4_final_for_input_mesh(
                "da3",
                args.da3_glb,
                args.da3_glb.read_bytes(),
                walls_doc,
                check_cams,
                gains_srgb,
                cam_q_maps,
                args.lama_weights,
                final_dir,
                final_planes_dir,
                also_save_uncompressed=True,
                jpeg_quality=92,
            )

        round4_doc = {
            "parameters": {
                "floor_texel_m": TEXEL_M,
                "wall_texel_m": WALL_TEXEL_R3_M,
                "min_obs_coverage": MIN_OBS_COVERAGE_R3,
                "sharp_exponent_wall": SHARP_EXPONENT_WALL_R3,
                "sharp_exponent_floor": SHARP_EXPONENT_FLOOR_R3,
                "see_through_tol_m": SEE_THROUGH_TOL_M,
                "plane_strip_dist_m": PLANE_STRIP_DIST_M,
                "plane_strip_angle_deg": PLANE_STRIP_ANGLE_DEG,
                "lab_match_band_m": LAB_MATCH_BAND_M,
                "plane_feather_m": PLANE_FEATHER_R2_M,
                "mesh_boundary_feather_m": MESH_BOUNDARY_FEATHER_R2_M,
                "photo_quality": {
                    "patch_size_px": 17,
                    "overexp_thresh_u8": 248,
                    "underexp_thresh_u8": 12,
                    "formula": "w_photo = (w_sharp ** 0.75) * (w_exposure ** 1.25)",
                    "compute_seconds": q_seconds,
                    "per_camera_stats": cam_quality_stats,
                },
                "texture_compression": {
                    "mesh_atlas_and_floor_format": "image/jpeg (quality=92)",
                    "wall_alpha_mask_format": "image/png (RGBA alphaMode=MASK, entropy-optimized level 9)",
                    "surface_source_map_format": "image/png (8-bit lossless provenance)",
                },
            },
            "final_wm2": r4_wm2,
            "final_da3": r4_da3,
            "total_elapsed_seconds": round(time.perf_counter() - t_start, 3),
        }
        (final_dir / "report.json").write_text(json.dumps(round4_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        if prev_rep_path.is_file():
            report_doc = json.loads(prev_rep_path.read_text(encoding="utf-8"))
            report_doc["round4"] = round4_doc
            prev_rep_path.write_text(json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done:round4] 4차 최종 GLB 및 보고서 저장 완료: {final_dir / 'report.json'} (총 {round4_doc['total_elapsed_seconds']}s)", flush=True)
        return
    if args.round3_only and prev_rep_path.is_file():
        report_doc = json.loads(prev_rep_path.read_text(encoding="utf-8"))
        r3_12th = bake_r3_for_input_mesh(
            "12th",
            args.vdt_dir / "current-scene-textured.glb",
            raw_glb,
            walls_doc,
            check_cams,
            gains_srgb,
            args.lama_weights,
            out_dir,
            planes_dir,
        )
        r3_da3 = None
        if args.da3_glb.is_file():
            r3_da3 = bake_r3_for_input_mesh(
                "da3",
                args.da3_glb,
                args.da3_glb.read_bytes(),
                walls_doc,
                check_cams,
                gains_srgb,
                args.lama_weights,
                out_dir,
                planes_dir,
            )
        report_doc["round3"] = {
            "parameters": {
                "floor_texel_m": TEXEL_M,
                "wall_texel_r3_m": WALL_TEXEL_R3_M,
                "min_obs_coverage_r3": MIN_OBS_COVERAGE_R3,
                "sharp_exponent_wall_r3": SHARP_EXPONENT_WALL_R3,
                "sharp_exponent_floor_r3": SHARP_EXPONENT_FLOOR_R3,
                "see_through_tol_m": SEE_THROUGH_TOL_M,
                "plane_strip_dist_m": PLANE_STRIP_DIST_M,
                "plane_strip_angle_deg": PLANE_STRIP_ANGLE_DEG,
                "lab_match_band_m": LAB_MATCH_BAND_M,
                "plane_feather_r2_m": PLANE_FEATHER_R2_M,
                "mesh_boundary_feather_r2_m": MESH_BOUNDARY_FEATHER_R2_M,
            },
            "mesh_12th": r3_12th,
            "mesh_da3_clean": r3_da3,
        }
        prev_rep_path.write_text(json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done:round3] 3차 보고서 갱신 완료: {prev_rep_path} (총 {round(time.perf_counter() - t_start, 3)}s)", flush=True)
        return
    if args.round2_only and prev_rep_path.is_file():
        report_doc = json.loads(prev_rep_path.read_text(encoding="utf-8"))
        r2_12th = bake_r2_for_input_mesh(
            "12th",
            args.vdt_dir / "current-scene-textured.glb",
            raw_glb,
            walls_doc,
            check_cams,
            gains_srgb,
            args.lama_weights,
            out_dir,
            planes_dir,
        )
        r2_da3 = None
        if args.da3_glb.is_file():
            r2_da3 = bake_r2_for_input_mesh(
                "da3",
                args.da3_glb,
                args.da3_glb.read_bytes(),
                walls_doc,
                check_cams,
                gains_srgb,
                args.lama_weights,
                out_dir,
                planes_dir,
            )
        report_doc["round2"] = {
            "parameters": {
                "plane_strip_dist_m": PLANE_STRIP_DIST_M,
                "plane_strip_angle_deg": PLANE_STRIP_ANGLE_DEG,
                "lab_match_band_m": LAB_MATCH_BAND_M,
                "plane_feather_r2_m": PLANE_FEATHER_R2_M,
                "mesh_boundary_feather_r2_m": MESH_BOUNDARY_FEATHER_R2_M,
                "camera_color_gains_srgb": {
                    check_cams[k].camera_id: [round(float(x), 4) for x in gains_srgb[k]] for k in range(len(check_cams))
                },
            },
            "mesh_12th": r2_12th,
            "mesh_da3_clean": r2_da3,
        }
        prev_rep_path.write_text(json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done:round2] 2차 보고서 갱신 완료: {prev_rep_path} (총 {round(time.perf_counter() - t_start, 3)}s)", flush=True)
        return

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
    }

    r2_12th = bake_r2_for_input_mesh(
        "12th",
        args.vdt_dir / "current-scene-textured.glb",
        raw_glb,
        walls_doc,
        check_cams,
        gains_srgb,
        args.lama_weights,
        out_dir,
        planes_dir,
    )
    r2_da3 = None
    if args.da3_glb.is_file():
        r2_da3 = bake_r2_for_input_mesh(
            "da3",
            args.da3_glb,
            args.da3_glb.read_bytes(),
            walls_doc,
            check_cams,
            gains_srgb,
            args.lama_weights,
            out_dir,
            planes_dir,
        )
    report_doc["round2"] = {
        "parameters": {
            "plane_strip_dist_m": PLANE_STRIP_DIST_M,
            "plane_strip_angle_deg": PLANE_STRIP_ANGLE_DEG,
            "lab_match_band_m": LAB_MATCH_BAND_M,
            "plane_feather_r2_m": PLANE_FEATHER_R2_M,
            "mesh_boundary_feather_r2_m": MESH_BOUNDARY_FEATHER_R2_M,
            "camera_color_gains_srgb": {
                check_cams[k].camera_id: [round(float(x), 4) for x in gains_srgb[k]] for k in range(len(check_cams))
            },
        },
        "mesh_12th": r2_12th,
        "mesh_da3_clean": r2_da3,
    }
    report_doc["total_pipeline_seconds"] = round(time.perf_counter() - t_start, 3)

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] 전체 보고서 저장: {report_path} (총 {report_doc['total_pipeline_seconds']}s)", flush=True)


if __name__ == "__main__":
    main()
