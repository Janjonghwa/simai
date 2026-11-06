"""시점 의존 텍스처(View-Dependent Texture, S15P31A101-527) 1단계 준비 스크립트.

역할:
1. 12차 보정(`calibration.json` 최신)과 사람 없는 배경 사진(`cam_{A..D}.jpg`, 2304x1296)을 읽는다.
2. 렌즈 왜곡(OPENCV_PINHOLE, k1≈-0.29)을 펴서 새 내부 행렬 K 를 구하고, 웹 전송·렌더용으로 긴 변 2048(2048x1152) JPEG 로 저장한다.
3. 카메라별 외부 파라미터(World -> Camera R, t)를 GLB/Three.js 좌표계(`GLTF_Y_UP`) 기준으로 변환해 `cameras.json` 에 기록한다.
4. 카메라 간 노출·색온도 차이를 줄이기 위해 2대 이상 동시에 관측되는 메시 삼각형 중심의 평균색 비율(`color_gain_srgb`)을 계산해 함께 기록한다.
5. 12차 맵 GLB(`current-scene-textured.glb`)를 산출물 폴더(`C:/Users/SSAFY/simai-local/vdt/`)에 그대로 복사한다.

좌표계 및 축 변환 근거 (`ai/src/simai_ai/scene/texture_mesh.py` 및 `front/.../worldCoordinates.ts` 정본 규약):
- World 좌표계 (`WORLD`): 미터(m), 오른손 좌표계, +Z 상방(Up), 바닥 Z=0.
- GLB / Three.js 좌표계 (`GLTF_Y_UP`): 미터(m), 오른손 좌표계, +Y 상방(Up).
  `texture_mesh.py` 의 `GLTF_Y_UP` 행렬 G (det(G) = +1, 직교 회전 행렬):
      G = [[1,  0,  0],
           [0,  0,  1],
           [0, -1,  0]]
  열벡터 기준:
      p_glb   = G @ p_world = (x_world, z_world, -y_world)^T
      p_world = G^T @ p_glb = (x_glb,  -z_glb,    y_glb)^T
- OpenCV 카메라 좌표계 (`OPENCV_WORLD_TO_CAMERA`, +X 오른쪽, +Y 아래, +Z 정면 깊이):
  보정 원본 식:
      p_cam_cv = R_w2c @ p_world + t_w2c
  여기에 p_world = G^T @ p_glb 를 대입하면 GLB 월드 정점 p_glb 를 OpenCV 카메라 좌표로 바로 옮기는 식이 된다:
      p_cam_cv = (R_w2c @ G^T) @ p_glb + t_w2c
               = R_glb2cv @ p_glb + t_glb2cv
  따라서 GLB 좌표계 기준 월드→카메라 변환은:
      R_glb2cv = R_w2c @ G^T
      t_glb2cv = t_w2c
  카메라 광학 중심(위치)과 방향 벡터(GLB 좌표계):
      C_glb       = G @ (-R_w2c^T @ t_w2c) = -R_glb2cv^T @ t_glb2cv
      forward_glb = R_glb2cv[2, :]   (OpenCV +Z 축)
      up_glb      = -R_glb2cv[1, :]  (OpenCV -Y 축)
- Three.js / OpenGL 카메라 좌표계 (+X 오른쪽, +Y 위, -Z 정면):
  OpenCV 카메라 축에서 Y·Z 부호를 뒤집는 대각 행렬 S = diag(1, -1, -1) (det(S) = +1)를 곱하면:
      p_cam_gl = S @ p_cam_cv = (S @ R_glb2cv) @ p_glb + (S @ t_glb2cv)
  이 4x4 행렬이 Three.js 카메라의 `matrixWorldInverse`(viewMatrix)가 되며, 그 역행렬이 `matrixWorld` 다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

AI_SRC = Path(__file__).resolve().parents[2] / "src"
if str(AI_SRC) not in sys.path:
    sys.path.insert(0, str(AI_SRC))

from simai_ai.scene import glb_enhance as ge  # noqa: E402
from simai_ai.scene import glb_fill as gf  # noqa: E402
from simai_ai.scene.texture_mesh import GLTF_Y_UP, glb_axis_convention  # noqa: E402

LOCAL_ROOT = Path("C:/Users/SSAFY/simai-local")
DEFAULT_GLB = LOCAL_ROOT / "grtmesh-poc/current-scene-textured.glb"
DEFAULT_CAL_GLOB_DIR = LOCAL_ROOT / "session12/results/lobby-s12/state/config/bundles"
DEFAULT_RESULTS_DIR = LOCAL_ROOT / "session12/results"
DEFAULT_OUT_DIR = LOCAL_ROOT / "vdt"

LONG_SIDE_PX = 2048
JPEG_QUALITY = 92
NEAR_M = 0.1
FAR_M = 25.0

# 확인용 자유 시점 (World m: eye, target, hfov_deg) - texrecon_opt/common.py 와 동일
FREE_VIEWS_WORLD: dict[str, tuple[list[float], list[float], float]] = {
    "top": ([4.1, 4.2, 9.5], [4.1, 4.15, 0.0], 70.0),
    "obl1": ([4.0, 4.0, 3.6], [0.3, 9.0, 0.3], 80.0),
    "obl2": ([4.0, 4.0, 3.6], [8.2, 1.0, 0.4], 80.0),
    "obl3": ([4.0, 5.0, 3.0], [1.0, 0.2, 0.5], 80.0),
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def find_latest_calibration(bundles_dir: Path) -> tuple[Path, dict[str, Any]]:
    candidates = sorted(bundles_dir.glob("*/calibration.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"calibration.json 을 찾을 수 없다: {bundles_dir}")
    latest = candidates[-1]
    raw = json.loads(latest.read_text(encoding="utf-8"))
    payload = raw.get("payload", raw)
    return latest, payload


def find_photo_bundle(results_dir: Path, cal_version: str) -> Path:
    cal_dir = results_dir / cal_version
    if cal_dir.is_dir():
        bundles = sorted(cal_dir.glob("bundle-*"), key=lambda p: p.stat().st_mtime)
        if bundles:
            return bundles[-1]
    all_bundles = sorted(results_dir.glob("cal-lobby-init-*/bundle-*"), key=lambda p: p.stat().st_mtime)
    if not all_bundles:
        raise FileNotFoundError(f"배경 사진 번들을 찾을 수 없다: {results_dir}")
    return all_bundles[-1]


def undistort_and_resize(
    image_bgr: np.ndarray,
    valid_mask: np.ndarray | None,
    K: np.ndarray,
    dist_coeffs: np.ndarray,
    long_side: int = LONG_SIDE_PX,
) -> tuple[np.ndarray, np.ndarray | None, np.ndarray, np.ndarray, tuple[int, int]]:
    """왜곡을 펴서 새 K(원본 크기 기준)를 구하고, 긴 변 long_side 로 축소한 이미지·마스크와 축소된 K 를 반환한다."""
    h0, w0 = image_bgr.shape[:2]
    K_undist_full, _ = cv2.getOptimalNewCameraMatrix(K, dist_coeffs, (w0, h0), 0, (w0, h0), centerPrincipalPoint=True)
    mx, my = cv2.initUndistortRectifyMap(K, dist_coeffs, None, K_undist_full, (w0, h0), cv2.CV_32FC1)
    undist_bgr = cv2.remap(image_bgr, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    scale = long_side / max(w0, h0)
    w_new = int(round(w0 * scale))
    h_new = int(round(h0 * scale))
    resized_bgr = cv2.resize(undist_bgr, (w_new, h_new), interpolation=cv2.INTER_AREA)

    sx = w_new / float(w0)
    sy = h_new / float(h0)
    K_scaled = K_undist_full.copy()
    K_scaled[0, 0] *= sx
    K_scaled[0, 2] *= sx
    K_scaled[1, 1] *= sy
    K_scaled[1, 2] *= sy

    undist_valid = None
    if valid_mask is not None:
        vu = cv2.remap(valid_mask, mx, my, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        undist_valid = vu > 127

    return undist_bgr, undist_valid, K_undist_full, K_scaled, (w_new, h_new), resized_bgr


def build_opengl_projection_from_intrinsics(
    K: np.ndarray,
    width: int,
    height: int,
    near: float = NEAR_M,
    far: float = FAR_M,
) -> np.ndarray:
    """OpenCV 핀홀 내부 행렬 K 와 이미지 크기 (width, height) 로부터 OpenGL/Three.js 4x4 투영 행렬(행 우선)을 만든다.

    OpenGL 카메라 공간 점 (x_gl, y_gl, z_gl) = (x_cv, -y_cv, -z_cv) 에 대해:
      w_clip = -z_gl = z_cv
      x_ndc  = x_clip / w_clip = 2 * (fx * x_cv / z_cv + cx) / W - 1
      y_ndc  = y_clip / w_clip = 1 - 2 * (fy * y_cv / z_cv + cy) / H
    가 되어, 셰이더에서 계산한 정규화 영상 좌표 (u_norm, 1 - v_norm) 과 깊이 렌더 타깃 UV 가 정확히 일치한다.
    """
    fx = float(K[0, 0])
    fy = float(K[1, 1])
    cx = float(K[0, 2])
    cy = float(K[1, 2])
    W = float(width)
    H = float(height)

    P = np.zeros((4, 4), dtype=np.float64)
    P[0, 0] = 2.0 * fx / W
    P[0, 2] = 1.0 - 2.0 * cx / W
    P[1, 1] = 2.0 * fy / H
    P[1, 2] = 2.0 * cy / H - 1.0
    P[2, 2] = -(far + near) / (far - near)
    P[2, 3] = -(2.0 * far * near) / (far - near)
    P[3, 2] = -1.0
    return P


def compute_multiview_color_gains(
    glb_path: Path,
    check_cams: list[ge.CheckCamera],
) -> tuple[np.ndarray, dict[str, Any]]:
    """2대 이상 카메라에 동시에 가림 없이 보이는 메시 삼각형 중심의 평균색을 맞춰 카메라별 RGB 보정 계수를 구한다."""
    g = ge.parse_glb(glb_path.read_bytes())
    simai = (g.doc.get("extras") or {}).get("simai")
    if isinstance(simai, dict):
        simai.pop("enhanced", None)
    model = ge.load_scene(g)
    mesh = gf.merge_mesh(model.prims)
    vis = [gf.render_visibility(mesh, c, 1152) for c in check_cams]

    num_faces = len(mesh.F)
    num_cams = len(check_cams)
    valid_obs = np.zeros((num_cams, num_faces), dtype=bool)
    colors = np.zeros((num_cams, num_faces, 3), dtype=np.float64)

    for k, (c, v) in enumerate(zip(check_cams, vis)):
        h0, w0 = c.image.shape[:2]
        u, vv, z = gf.project(c, mesh.cen)
        ui = np.clip(np.round(u).astype(int), 0, w0 - 1)
        vi = np.clip(np.round(vv).astype(int), 0, h0 - 1)
        in_img = (u >= 16) & (u < w0 - 16) & (vv >= 16) & (vv < h0 - 16) & (z > 0.2)
        center = gf.cam_center(c)
        to_cam = center[None, :] - mesh.cen
        to_cam /= np.maximum(np.linalg.norm(to_cam, axis=1, keepdims=True), 1e-9)
        cos = (mesh.n * to_cam).sum(axis=1)
        mask_ok = c.valid[vi, ui] if c.valid is not None else np.ones(num_faces, dtype=bool)
        ok = in_img & (v.seen_px >= 2) & (cos > 0.25) & mask_ok
        valid_obs[k] = ok
        colors[k, ok] = c.image[vi[ok], ui[ok]].astype(np.float64)

    overlap = valid_obs.sum(axis=0) >= 2
    gains = np.ones((num_cams, 3), dtype=np.float64)
    if int(overlap.sum()) >= 100:
        for _ in range(12):
            corr = colors * gains[:, None, :]
            denom = np.maximum(valid_obs.sum(axis=0)[:, None], 1)
            mean_c = np.where(valid_obs[:, :, None], corr, 0.0).sum(axis=0) / denom
            for k in range(num_cams):
                sel = overlap & valid_obs[k]
                if int(sel.sum()) >= 50:
                    gains[k] = mean_c[sel].mean(axis=0) / np.maximum(colors[k, sel].mean(axis=0), 1e-6)
            gains /= np.prod(gains, axis=0, keepdims=True) ** (1.0 / num_cams)

    stats = {
        "overlapping_faces": int(overlap.sum()),
        "total_faces": int(num_faces),
        "per_camera_visible_faces": {
            check_cams[k].camera_id: int(valid_obs[k].sum()) for k in range(num_cams)
        },
        "per_camera_raw_mean_rgb": {
            check_cams[k].camera_id: [round(float(x), 2) for x in colors[k, valid_obs[k]].mean(axis=0)]
            for k in range(num_cams)
        },
        "per_camera_gain_srgb": {
            check_cams[k].camera_id: [round(float(x), 4) for x in gains[k]]
            for k in range(num_cams)
        },
    }
    return gains, stats, mesh, valid_obs, colors


def _intersect_2d_lines(n1: np.ndarray, d1: float, n2: np.ndarray, d2: float) -> np.ndarray:
    """2D 직선 n1·xy = d1 과 n2·xy = d2 의 교점 (x, y) 를 반환한다."""
    A = np.array([[n1[0], n1[1]], [n2[0], n2[1]]], dtype=np.float64)
    b = np.array([d1, d2], dtype=np.float64)
    return np.linalg.solve(A, b)


def build_floor_and_wall_layers(
    mesh: gf.Merged,
    valid_obs: np.ndarray,
    colors: np.ndarray,
    gains: np.ndarray,
) -> dict[str, Any]:
    """2차 지시(3·4번): 바닥 미관측 표시 층(World Z=-0.015m)과 find_wall_planes 경계 벽 평면(3면)을 계산한다."""
    G = GLTF_Y_UP
    cfg = ge.EnhanceConfig()
    keep = np.ones(len(mesh.F), dtype=bool)

    z0, floor_info = gf.floor_level(mesh, keep, cfg)
    planes, rejected = gf.find_wall_planes(mesh, keep, z0, cfg)

    # 카메라 노출 보정이 적용된 삼각형별 관측 평균색 (0..1 sRGB)
    corr_colors = colors * gains[:, None, :]
    obs_count = valid_obs.sum(axis=0)
    has_obs = obs_count >= 1
    face_mean_rgb255 = np.zeros((len(mesh.F), 3), dtype=np.float64)
    face_mean_rgb255[has_obs] = (
        np.where(valid_obs[:, has_obs, None], corr_colors[:, has_obs, :], 0.0).sum(axis=0)
        / np.maximum(obs_count[has_obs, None], 1)
    )
    face_mean_srgb = np.clip(face_mean_rgb255 / 255.0, 0.0, 1.0)

    # 1) 바닥 미관측 표시 층 (World Z = z0 - 0.015m, TSDF 바닥 중앙값 +4.61cm 와 z-fighting 방지)
    z_floor_world = float(z0 - 0.015)
    near_floor = keep & (np.abs(mesh.n[:, 2]) > 0.70) & (np.abs(mesh.cen[:, 2] - z0) < 0.12)
    floor_xy = mesh.V[mesh.F[near_floor]][:, :, :2].reshape(-1, 2)
    xy_min = floor_xy.min(axis=0)
    xy_max = floor_xy.max(axis=0)

    floor_obs = near_floor & has_obs
    if int(floor_obs.sum()) >= 20:
        # 이상치(테이블 반사 등) 영향을 줄이기 위해 중앙값(median) 톤 사용
        raw_floor_srgb = np.median(face_mean_srgb[floor_obs], axis=0)
    else:
        raw_floor_srgb = np.array([0.28, 0.30, 0.34], dtype=np.float64)
    # 미관측 표시 층임을 정직하게 나타내도록 약간 차분하게 감쇠한 단색 톤
    calm_floor_srgb = np.clip(raw_floor_srgb * 0.88, 0.14, 0.55)

    # 수용된 3개 벽 평면(좌 x≈0.07, 우 x≈8.24, 뒤 y≈-0.16)과 앞쪽 열린 경계(바닥 bounds + 여유 0.45m)로 바닥 사각형 구성
    wall_offset_m = 0.018
    margin_open_m = 0.45
    x_lo = float(xy_min[0] - margin_open_m)
    x_hi = float(xy_max[0] + margin_open_m)
    y_lo = float(xy_min[1] - margin_open_m)
    y_hi = float(xy_max[1] + margin_open_m)

    # 수용된 벽 평면을 방향별로 분류 (방 안쪽을 향하는 법선 n 기준)
    left_pl = next((p for p in planes if p["n"][0] > 0.7), None)
    right_pl = next((p for p in planes if p["n"][0] < -0.7), None)
    back_pl = next((p for p in planes if p["n"][1] > 0.7), None)
    front_pl = next((p for p in planes if p["n"][1] < -0.7), None)

    n_left = np.array(left_pl["n"], dtype=np.float64) if left_pl else np.array([1.0, 0.0])
    d_left = float(left_pl["d"]) - wall_offset_m if left_pl else x_lo

    n_right = np.array(right_pl["n"], dtype=np.float64) if right_pl else np.array([-1.0, 0.0])
    d_right = float(right_pl["d"]) - wall_offset_m if right_pl else -x_hi

    n_back = np.array(back_pl["n"], dtype=np.float64) if back_pl else np.array([0.0, 1.0])
    d_back = float(back_pl["d"]) - wall_offset_m if back_pl else y_lo

    if front_pl:
        n_front = np.array(front_pl["n"], dtype=np.float64)
        d_front = float(front_pl["d"]) - wall_offset_m
    else:
        # 열린 +Y 방향은 뒤쪽 벽과 평행하게 맵 바닥 최대 범위 + 여유(또는 좌측 벽 최대 u 범위)까지 덮음
        n_front = -n_back
        max_wall_y = max([ float(xy_max[1] + margin_open_m) ] + [ float(p["u_range"][1]) for p in planes if abs(p["t"][1]) > 0.7 ])
        d_front = -max_wall_y

    # 반시계 방향(위에서 볼 때 +Z 법선) 바닥 꼭짓점 4개: 뒤-좌 -> 뒤-우 -> 앞-우 -> 앞-좌
    c_bl = _intersect_2d_lines(n_left, d_left, n_back, d_back)
    c_br = _intersect_2d_lines(n_right, d_right, n_back, d_back)
    c_fr = _intersect_2d_lines(n_right, d_right, n_front, d_front)
    c_fl = _intersect_2d_lines(n_left, d_left, n_front, d_front)

    floor_corners_world = [
        [round(float(c[0]), 4), round(float(c[1]), 4), round(z_floor_world, 4)]
        for c in (c_bl, c_br, c_fr, c_fl)
    ]
    floor_corners_glb = [
        [round(float(v), 4) for v in (G @ np.array(cw, dtype=np.float64))]
        for cw in floor_corners_world
    ]
    floor_normal_world = [0.0, 0.0, 1.0]
    floor_normal_glb = [round(float(v), 4) for v in (G @ np.array(floor_normal_world))]

    # 2) 수용된 벽 평면 (find_wall_planes 결과 그대로, 수용 평면이 없는 방향에는 만들지 않음)
    wall_entries: list[dict[str, Any]] = []
    for idx, pl in enumerate(planes):
        n2 = np.asarray(pl["n"], dtype=np.float64)
        t2 = np.asarray(pl["t"], dtype=np.float64)
        d_orig = float(pl["d"])
        d_render = d_orig - wall_offset_m  # TSDF 벽면보다 1.8cm 바깥쪽에 두어 z-fighting 방지
        u0, u1 = float(pl["u_range"][0]), float(pl["u_range"][1])
        z_bot = z_floor_world
        z_top = min(float(pl["z_top"]), float(z0 + cfg.fill_wall_max_height_m))

        # 해당 벽 근처 관측 삼각형들의 대표 색상 계산
        dist_to_wall = np.abs(mesh.V[:, :2] @ n2 - d_orig)
        near_wall = keep & (dist_to_wall[mesh.F] < 0.12).all(axis=1) & (np.abs(mesh.n[:, 2]) < 0.35)
        wall_obs = near_wall & has_obs
        if int(wall_obs.sum()) >= 20:
            raw_wall_srgb = np.median(face_mean_srgb[wall_obs], axis=0)
        else:
            raw_wall_srgb = np.array([0.42, 0.44, 0.47], dtype=np.float64)
        calm_wall_srgb = np.clip(raw_wall_srgb * 0.85, 0.18, 0.65)

        # 벽 사각형 꼭짓점 4개: (u0, z_bot), (u1, z_bot), (u1, z_top), (u0, z_top)
        def _wall_pt_world(u_val: float, z_val: float) -> list[float]:
            wx = n2[0] * d_render + t2[0] * u_val
            wy = n2[1] * d_render + t2[1] * u_val
            return [round(float(wx), 4), round(float(wy), 4), round(float(z_val), 4)]

        w_corners_world = [
            _wall_pt_world(u0, z_bot),
            _wall_pt_world(u1, z_bot),
            _wall_pt_world(u1, z_top),
            _wall_pt_world(u0, z_top),
        ]
        w_corners_glb = [
            [round(float(v), 4) for v in (G @ np.array(cw, dtype=np.float64))]
            for cw in w_corners_world
        ]
        w_normal_world = [round(float(n2[0]), 6), round(float(n2[1]), 6), 0.0]
        w_normal_glb = [round(float(v), 6) for v in (G @ np.array(w_normal_world))]
        w_u_axis_world = [round(float(t2[0]), 6), round(float(t2[1]), 6), 0.0]
        w_u_axis_glb = [round(float(v), 6) for v in (G @ np.array(w_u_axis_world))]

        wall_entries.append(
            {
                "wall_id": f"wall_{idx}",
                "index": idx,
                "n_world_xy": [round(float(n2[0]), 6), round(float(n2[1]), 6)],
                "t_world_xy": [round(float(t2[0]), 6), round(float(t2[1]), 6)],
                "d_world_m": round(d_orig, 4),
                "d_render_world_m": round(d_render, 4),
                "u_range_m": [round(u0, 4), round(u1, 4)],
                "z_range_m": [round(z_bot, 4), round(z_top, 4)],
                "n_faces": int(pl["n_faces"]),
                "area_m2": float(pl["area_m2"]),
                "length_m": float(pl["length_m"]),
                "normal_world": w_normal_world,
                "normal_glb": w_normal_glb,
                "u_axis_world": w_u_axis_world,
                "u_axis_glb": w_u_axis_glb,
                "corners_world": w_corners_world,
                "corners_glb": w_corners_glb,
                "observed_median_srgb": [round(float(x), 4) for x in raw_wall_srgb],
                "calm_indicator_srgb": [round(float(x), 4) for x in calm_wall_srgb],
                "near_observed_faces": int(wall_obs.sum()),
            }
        )

    return {
        "format": "simai.vdt_walls",
        "format_version": "0.1.0",
        "jira_key": "S15P31A101-527",
        "source_module": "simai_ai.scene.glb_fill.find_wall_planes",
        "floor_level_info": floor_info,
        "floor_plane": {
            "z_world_m": round(z_floor_world, 4),
            "y_glb_m": round(z_floor_world, 4),
            "mesh_floor_median_z_world_m": round(float(floor_info.get("area_weighted_median_z_m", 0.0461)), 4),
            "offset_below_world_zero_m": 0.015,
            "observed_xy_bounds_world_m": {
                "min": [round(float(xy_min[0]), 4), round(float(xy_min[1]), 4)],
                "max": [round(float(xy_max[0]), 4), round(float(xy_max[1]), 4)],
            },
            "normal_world": floor_normal_world,
            "normal_glb": floor_normal_glb,
            "corners_world": floor_corners_world,
            "corners_glb": floor_corners_glb,
            "observed_median_srgb": [round(float(x), 4) for x in raw_floor_srgb],
            "calm_indicator_srgb": [round(float(x), 4) for x in calm_floor_srgb],
            "grid_step_m": 1.0,
        },
        "accepted_wall_count": len(wall_entries),
        "wall_planes": wall_entries,
        "rejected_wall_candidates": rejected,
    }



def convert_camera_to_glb(
    cam_entry: dict[str, Any],
    K_scaled: np.ndarray,
    K_undist_full: np.ndarray,
    image_rel_path: str,
    size_wh: tuple[int, int],
    orig_size_wh: tuple[int, int],
    color_gain: np.ndarray,
) -> dict[str, Any]:
    """보정 카메라 1대의 파라미터를 GLB (`GLTF_Y_UP`) 좌표계 기준으로 변환한다."""
    G = GLTF_Y_UP  # [[1, 0, 0], [0, 0, 1], [0, -1, 0]]: World -> GLB
    S = np.diag([1.0, -1.0, -1.0])  # OpenCV camera -> OpenGL/Three.js camera

    R_w2c = np.asarray(cam_entry["R"], dtype=np.float64)
    t_w2c = np.asarray(cam_entry["t"], dtype=np.float64).reshape(3)

    # 1) GLB 좌표계 기준 월드(GLB) -> OpenCV 카메라 변환:
    #    p_cam_cv = R_w2c @ (G^T @ p_glb) + t_w2c = (R_w2c @ G^T) @ p_glb + t_w2c
    R_glb2cv = R_w2c @ G.T
    t_glb2cv = t_w2c.copy()

    T_glb2cv = np.eye(4, dtype=np.float64)
    T_glb2cv[:3, :3] = R_glb2cv
    T_glb2cv[:3, 3] = t_glb2cv

    # 2) 카메라 중심 위치 및 축 방향 (World 및 GLB):
    pos_world = -R_w2c.T @ t_w2c
    pos_glb = G @ pos_world  # == -R_glb2cv.T @ t_glb2cv
    forward_glb = R_glb2cv[2, :]  # OpenCV +Z (광축 정면)
    up_glb = -R_glb2cv[1, :]  # OpenCV -Y (화면 위쪽)
    right_glb = R_glb2cv[0, :]  # OpenCV +X (화면 오른쪽)

    # 3) Three.js / OpenGL 카메라 행렬 (viewMatrix = matrixWorldInverse, matrixWorld = 역행렬)
    R_glb2gl = S @ R_glb2cv
    t_glb2gl = S @ t_glb2cv
    view_gl = np.eye(4, dtype=np.float64)
    view_gl[:3, :3] = R_glb2gl
    view_gl[:3, 3] = t_glb2gl
    world_gl = np.linalg.inv(view_gl)

    w_new, h_new = size_wh
    proj_gl = build_opengl_projection_from_intrinsics(K_scaled, w_new, h_new, near=NEAR_M, far=FAR_M)

    fy = float(K_scaled[1, 1])
    vfov_deg = float(np.degrees(2.0 * np.arctan((h_new * 0.5) / fy)))

    return {
        "camera_id": cam_entry["camera_id"],
        "image": image_rel_path,
        "image_size": [w_new, h_new],
        "original_image_size": [orig_size_wh[0], orig_size_wh[1]],
        "K": [[round(float(v), 6) for v in row] for row in K_scaled],
        "K_undistorted_full": [[round(float(v), 6) for v in row] for row in K_undist_full],
        "R": [[round(float(v), 8) for v in row] for row in R_glb2cv],
        "t": [round(float(v), 8) for v in t_glb2cv],
        "world_to_cam_4x4": [[round(float(v), 8) for v in row] for row in T_glb2cv],
        "position_glb": [round(float(v), 6) for v in pos_glb],
        "position_world": [round(float(v), 6) for v in pos_world],
        "forward_glb": [round(float(v), 6) for v in forward_glb],
        "up_glb": [round(float(v), 6) for v in up_glb],
        "right_glb": [round(float(v), 6) for v in right_glb],
        "look_target_glb": [round(float(v), 6) for v in (pos_glb + forward_glb * 4.5)],
        "vfov_deg": round(vfov_deg, 4),
        "near": NEAR_M,
        "far": FAR_M,
        "three_view_matrix_col_major": [round(float(v), 8) for v in view_gl.T.reshape(-1)],
        "three_matrix_world_col_major": [round(float(v), 8) for v in world_gl.T.reshape(-1)],
        "three_projection_matrix_col_major": [round(float(v), 8) for v in proj_gl.T.reshape(-1)],
        "color_gain_srgb": [round(float(v), 4) for v in color_gain],
        "reprojection_error_px": cam_entry.get("reprojection_error_px"),
    }


def convert_free_views_to_glb() -> dict[str, dict[str, Any]]:
    G = GLTF_Y_UP
    out: dict[str, dict[str, Any]] = {}
    aspect = 16.0 / 9.0
    for name, (eye_w, target_w, hfov_deg) in FREE_VIEWS_WORLD.items():
        eye_glb = (G @ np.asarray(eye_w, dtype=np.float64)).tolist()
        target_glb = (G @ np.asarray(target_w, dtype=np.float64)).tolist()
        vfov_deg = float(np.degrees(2.0 * np.arctan(np.tan(np.radians(hfov_deg) * 0.5) / aspect)))
        # top 시점처럼 시선이 +Y/-Y 축에 거의 평행할 때의 up 벡터
        fwd = np.asarray(target_glb) - np.asarray(eye_glb)
        fwd /= np.linalg.norm(fwd)
        if abs(fwd[1]) > 0.95:
            # World +Y 는 GLB -Z
            up_glb = [0.0, 0.0, -1.0]
        else:
            up_glb = [0.0, 1.0, 0.0]
        out[name] = {
            "name": name,
            "position_glb": [round(float(v), 4) for v in eye_glb],
            "target_glb": [round(float(v), 4) for v in target_glb],
            "up_glb": up_glb,
            "hfov_deg": hfov_deg,
            "vfov_deg": round(vfov_deg, 4),
            "position_world": eye_w,
            "target_world": target_w,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="시점 의존 텍스처(VDT) 입력 준비 스크립트")
    parser.add_argument("--glb", type=Path, default=DEFAULT_GLB, help="입력 12차 맵 GLB 경로")
    parser.add_argument("--cal-bundles-dir", type=Path, default=DEFAULT_CAL_GLOB_DIR, help="보정 번들 디렉터리")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR, help="세션 결과 디렉터리")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="산출물 출력 폴더")
    parser.add_argument("--long-side", type=int, default=LONG_SIDE_PX, help="저장할 JPEG 긴 변 해상도")
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    img_dir = out_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)

    # 1. GLB 그대로 복사
    dst_glb = out_dir / "current-scene-textured.glb"
    shutil.copy2(args.glb, dst_glb)

    # 2. 최신 calibration.json 및 배경 사진 번들 찾기
    cal_path, cal = find_latest_calibration(args.cal_bundles_dir)
    cal_version = str(cal["calibration_version"])
    photo_bundle = find_photo_bundle(args.results_dir, cal_version)

    # 3. 각 카메라 왜곡 펴기 + 리사이즈 저장
    check_cams: list[ge.CheckCamera] = []
    prepared_items: list[dict[str, Any]] = []
    for cam_entry in cal["cameras"]:
        cid = cam_entry["camera_id"]
        src_img_path = photo_bundle / f"{cid}.jpg"
        src_valid_path = photo_bundle / f"{cid}.valid.png"
        bgr = cv2.imread(str(src_img_path), cv2.IMREAD_COLOR)
        if bgr is None:
            raise FileNotFoundError(f"카메라 사진을 읽을 수 없다: {src_img_path}")
        valid_raw = cv2.imread(str(src_valid_path), cv2.IMREAD_GRAYSCALE) if src_valid_path.is_file() else None

        K = np.asarray(cam_entry["K"], dtype=np.float64)
        dist = np.asarray(cam_entry["distortion"]["coeffs"], dtype=np.float64)
        h0, w0 = bgr.shape[:2]

        undist_bgr, undist_valid, K_undist_full, K_scaled, (w_new, h_new), resized_bgr = undistort_and_resize(
            bgr, valid_raw, K, dist, long_side=args.long_side
        )

        rel_img = f"images/{cid}.jpg"
        out_img_path = out_dir / rel_img
        cv2.imwrite(str(out_img_path), resized_bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])

        check_cams.append(
            ge.CheckCamera(
                camera_id=cid,
                K=K_undist_full,
                R=np.asarray(cam_entry["R"], dtype=np.float64),
                t=np.asarray(cam_entry["t"], dtype=np.float64).reshape(3),
                image=np.ascontiguousarray(undist_bgr[:, :, ::-1]),
                valid=undist_valid,
            )
        )
        prepared_items.append(
            {
                "cam_entry": cam_entry,
                "K_scaled": K_scaled,
                "K_undist_full": K_undist_full,
                "rel_img": rel_img,
                "size_wh": (w_new, h_new),
                "orig_size_wh": (w0, h0),
            }
        )

    # 4. 다중 시점 겹침 영역 기반 카메라별 노출/색 보정 계수 계산
    gains, color_stats, merged_mesh, valid_obs, cam_colors = compute_multiview_color_gains(args.glb, check_cams)

    # 4-2. 2차 지시: 바닥 미관측 표시 층(World Z=-0.015m) + find_wall_planes 수용 벽 평면(3면) 내보내기
    walls_doc = build_floor_and_wall_layers(merged_mesh, valid_obs, cam_colors, gains)
    walls_json_path = out_dir / "walls.json"
    walls_json_path.write_text(json.dumps(walls_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    # 5. GLB 좌표계 기준 cameras.json 작성
    cameras_out = []
    for idx, item in enumerate(prepared_items):
        cameras_out.append(
            convert_camera_to_glb(
                cam_entry=item["cam_entry"],
                K_scaled=item["K_scaled"],
                K_undist_full=item["K_undist_full"],
                image_rel_path=item["rel_img"],
                size_wh=item["size_wh"],
                orig_size_wh=item["orig_size_wh"],
                color_gain=gains[idx],
            )
        )

    axis_conv = glb_axis_convention(np.eye(4))
    doc_out = {
        "format": "simai.vdt_cameras",
        "format_version": "0.1.0",
        "jira_key": "S15P31A101-527",
        "axis_convention": axis_conv,
        "axis_transform_note": (
            "texture_mesh.py 의 GLTF_Y_UP = [[1,0,0],[0,0,1],[0,-1,0]] 규약에 따라 "
            "World(m, +Z up) 정점 p_world 가 GLB(m, +Y up) 정점 p_glb = G @ p_world = (x, z, -y) 로 저장됨. "
            "따라서 p_world = G^T @ p_glb 이고, OpenCV 월드->카메라 식 p_cv = R_w2c @ p_world + t_w2c 는 "
            "GLB 정점 기준 p_cv = (R_w2c @ G^T) @ p_glb + t_w2c 가 됨 (R = R_w2c @ G^T, t = t_w2c)."
        ),
        "world_to_glb_matrix": GLTF_Y_UP.tolist(),
        "glb_to_world_matrix": GLTF_Y_UP.T.tolist(),
        "scene_glb": "current-scene-textured.glb",
        "scene_glb_sha256": sha256_file(dst_glb),
        "calibration_version": cal_version,
        "calibration_path": str(cal_path.as_posix()),
        "photo_bundle_path": str(photo_bundle.as_posix()),
        "color_leveling": color_stats,
        "cameras": cameras_out,
        "free_views": convert_free_views_to_glb(),
    }

    cameras_json_path = out_dir / "cameras.json"
    cameras_json_path.write_text(json.dumps(doc_out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6. 같은 폴더에서 python -m http.server 로 바로 띄울 수 있도록 index.html, vdt.js 복사
    here = Path(__file__).resolve().parent
    for fname in ("index.html", "vdt.js"):
        src_f = here / fname
        if src_f.is_file():
            shutil.copy2(src_f, out_dir / fname)

    print(f"[prepare] 완료: {cameras_json_path}")
    print(f"[prepare] 바닥·벽 평면 완료: {walls_json_path} (벽 {walls_doc['accepted_wall_count']}면 수용)")
    print(f"[prepare] GLB 복사: {dst_glb} ({dst_glb.stat().st_size / (1024*1024):.2f} MB)")
    for c in cameras_out:
        print(
            f"  - {c['camera_id']}: {c['image']} ({c['image_size'][0]}x{c['image_size'][1]}), "
            f"pos_glb={c['position_glb']}, gain={c['color_gain_srgb']}"
        )


if __name__ == "__main__":
    main()
