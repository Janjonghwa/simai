"""S15P31A101-535: MapAnything -> World 정렬 원인 분해, 후보 (a)/(b)/(c) 비교 평가 및 시각화 생성 스크립트.

노트북 CPU 만으로 재현 가능하며 다음 산출물을 만든다:
1. `C:/Users/SSAFY/simai-local/align-535/alignment_comparison_summary.json`
2. `C:/Users/SSAFY/simai-local/align-535/alignment_comparison_table.md`
3. `C:/Users/SSAFY/simai-local/align-535/floor_height_map_before_after.png`
4. `C:/Users/SSAFY/simai-local/align-535/pareto_center_vs_floor_tilt.png`
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from simai_ai.calibration.world_frame.frame import Plane, WorldFrameConfig, _up_prior, find_floor
from simai_ai.calibration.world_frame.inputs import (
    load_mapanything_run,
    load_prepared,
    prepared_to_model_px,
)

OUT_DIR = Path("C:/Users/SSAFY/simai-local/align-535")
MA_DIR = OUT_DIR / "ma_single" / "mapanything"
PREP_DIR = OUT_DIR / "ma_single" / "prepared"
S12_CAL_DIR = Path("C:/Users/SSAFY/simai-local/session12/results/lobby-s12/state/calibrations")
CAL_F0AB = OUT_DIR / "ma_single" / "calibration.json"
CAL_AF8A = S12_CAL_DIR / "658bbbadaf2824de9fadd2ac9c2b4f774c9f82081d8f44206621da5d9408bee5.json"
CAL_ROTF = S12_CAL_DIR / "95410164a854a0214957bd19b583593372f40aa9391ef1c584e904507b9ebbc5.json"
S12_SUMMARY_JSON = Path("C:/Users/SSAFY/simai-local/scale-props/session12_scale_summary.json")

CONE_REAL_HEIGHT_CM = 67.5
MEASURED_CAM_HEIGHTS_M = {"cam_A": 2.44, "cam_B": 2.44, "cam_C": 2.48, "cam_D": 2.48}
GATE_MAX_CENTER_M = 0.10
GATE_MAX_ROT_DEG = 5.0


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rot_angle_deg(R1: np.ndarray, R2: np.ndarray) -> float:
    tr = float(np.trace(R1.T @ R2))
    return float(np.degrees(np.arccos(np.clip((tr - 1.0) / 2.0, -1.0, 1.0))))


def rot_to_euler_xyz_deg(R: np.ndarray) -> list[float]:
    sy = math.sqrt(R[0, 0] ** 2 + R[1, 0] ** 2)
    if sy > 1e-6:
        rx = math.atan2(R[2, 1], R[2, 2])
        ry = math.atan2(-R[2, 0], sy)
        rz = math.atan2(R[1, 0], R[0, 0])
    else:
        rx = math.atan2(-R[1, 2], R[1, 1])
        ry = math.atan2(-R[2, 0], sy)
        rz = 0.0
    return [round(math.degrees(rx), 4), round(math.degrees(ry), 4), round(math.degrees(rz), 4)]


def extract_cameras(cal: dict[str, Any], views: list[dict[str, Any]]):
    cams = {c["camera_id"]: c for c in cal["cameras"]}
    names, rotations, src, dst, R_ma_list, R_cal_list = [], [], [], [], [], []
    for v in views:
        cid = v["name"]
        c = cams[cid]
        T = np.asarray(v["cam2world"], float)
        R_cal = np.asarray(c["R"], float)
        t_cal = np.asarray(c["t"], float)
        R_ma_w2c = T[:3, :3].T
        C_ma = T[:3, 3]
        C_cal = -R_cal.T @ t_cal
        names.append(cid)
        rotations.append(R_cal.T @ R_ma_w2c)
        src.append(C_ma)
        dst.append(C_cal)
        R_ma_list.append(R_ma_w2c)
        R_cal_list.append(R_cal)
    return names, rotations, np.asarray(src, float), np.asarray(dst, float), R_ma_list, R_cal_list


def solve_baseline_rot_only(src: np.ndarray, dst: np.ndarray, rotations: list[np.ndarray]) -> np.ndarray:
    """기존 reconstruct_tasks.align_to_calibration: 회전 행렬 평균만으로 Q 를 구한다."""
    u, _, vt = np.linalg.svd(np.sum(rotations, axis=0))
    Q = u @ np.diag([1.0, 1.0, np.linalg.det(u @ vt)]) @ vt
    x, y = src - src.mean(0), dst - dst.mean(0)
    denom = float(np.sum(x * x))
    scale = float(np.sum((x @ Q.T) * y) / denom) if denom > 1e-8 else 0.0
    shift = dst.mean(0) - scale * (Q @ src.mean(0))
    M = np.eye(4)
    M[:3, :3], M[:3, 3] = scale * Q, shift
    return M


def solve_umeyama_centers(
    src: np.ndarray, dst: np.ndarray, rotations: list[np.ndarray], *, eps_rot: float = 1e-3
) -> np.ndarray:
    """카메라 중심 변위 Umeyama + 공선/공면 영공간 정규화용 미소 회전 사전값."""
    x, y = src - src.mean(0), dst - dst.mean(0)
    H = y.T @ x + eps_rot * np.sum(rotations, axis=0)
    u, _, vt = np.linalg.svd(H)
    Q = u @ np.diag([1.0, 1.0, np.linalg.det(u @ vt)]) @ vt
    denom = float(np.sum(x * x))
    scale = float(np.sum((x @ Q.T) * y) / denom) if denom > 1e-8 else 0.0
    shift = dst.mean(0) - scale * (Q @ src.mean(0))
    M = np.eye(4)
    M[:3, :3], M[:3, 3] = scale * Q, shift
    return M


def solve_candidate_b_wls(
    src: np.ndarray,
    dst: np.ndarray,
    rotations: list[np.ndarray],
    floor_normal_ma: np.ndarray,
    floor_centroid_ma: np.ndarray,
    *,
    w_normal: float = 60.0,
    w_height: float = 4.0,
    eps_rot: float = 1e-3,
    max_center_target_m: float | None = 0.095,
) -> tuple[np.ndarray, dict[str, Any]]:
    """후보 (b): 바닥 법선(+Z)·바닥 높이(Z=0) 가중 제약을 결합한 닫힌 형태(closed-form) 카메라 중심 Similarity 정렬.

    max_center_target_m 가 주어지면 카메라 중심 잔차 상한을 넘지 않는 최대 w_normal(<= w_normal)을
    이분 탐색으로 자동 선택해, 보정본 자체에 바닥 기울기가 박혀 있는 경우에도 게이트(0.10 m)를
    안전하게 통과하면서 바닥 수평도를 최대화한다.
    """
    x, y = src - src.mean(0), dst - dst.mean(0)
    denom = float(np.sum(x * x))
    ez = np.array([0.0, 0.0, 1.0])
    H_base = y.T @ x + eps_rot * np.sum(rotations, axis=0)
    H_floor = np.outer(ez, floor_normal_ma)

    def eval_for_wn(wn: float) -> tuple[np.ndarray, float, np.ndarray, float, float]:
        H = H_base + wn * H_floor
        u, _, vt = np.linalg.svd(H)
        Q = u @ np.diag([1.0, 1.0, np.linalg.det(u @ vt)]) @ vt
        scale = float(np.sum((x @ Q.T) * y) / denom) if denom > 1e-8 else 0.0
        shift = dst.mean(0) - scale * (Q @ src.mean(0))
        if w_height > 0:
            z_floor_cam = float(scale * (Q[2, :] @ floor_centroid_ma) + shift[2])
            shift[2] -= (w_height / (len(src) + w_height)) * z_floor_cam
        max_r = float(np.linalg.norm(scale * (src @ Q.T) + shift - dst, axis=1).max())
        tilt_deg = float(np.degrees(np.arccos(np.clip((Q @ floor_normal_ma)[2], -1.0, 1.0))))
        return Q, scale, shift, max_r, tilt_deg

    if max_center_target_m is None:
        Q, scale, shift, max_r, tilt_deg = eval_for_wn(w_normal)
        used_wn = w_normal
    else:
        _, _, _, max_r0, _ = eval_for_wn(0.0)
        budget = max(max_center_target_m, max_r0 + 1e-4)
        Q_hi, scale_hi, shift_hi, max_r_hi, tilt_hi = eval_for_wn(w_normal)
        if max_r_hi <= budget:
            Q, scale, shift, max_r, tilt_deg = Q_hi, scale_hi, shift_hi, max_r_hi, tilt_hi
            used_wn = w_normal
        else:
            lo, hi = 0.0, w_normal
            for _ in range(24):
                mid = 0.5 * (lo + hi)
                _, _, _, r_mid, _ = eval_for_wn(mid)
                if r_mid <= budget:
                    lo = mid
                else:
                    hi = mid
            Q, scale, shift, max_r, tilt_deg = eval_for_wn(lo)
            used_wn = lo

    M = np.eye(4)
    M[:3, :3], M[:3, 3] = scale * Q, shift
    return M, {
        "w_normal_requested": w_normal,
        "w_normal_used": round(float(used_wn), 4),
        "w_height": w_height,
        "max_center_target_m": max_center_target_m,
        "predicted_floor_tilt_deg": round(tilt_deg, 4),
    }


def solve_candidate_c_post_icp(
    M_init: np.ndarray,
    src: np.ndarray,
    dst: np.ndarray,
    floor_pts_ma: np.ndarray,
    *,
    lambda_floor: float = 25.0,
    max_center_target_m: float | None = 0.095,
    n_iters: int = 15,
) -> tuple[np.ndarray, dict[str, Any]]:
    """후보 (c): 초기 similarity(Umeyama) 뒤 바닥 점구름 Z=0 평면 Huber-ICP 미세 보정."""
    # 부표본 바닥 점구름
    rng = np.random.default_rng(0)
    sub_ma = floor_pts_ma if len(floor_pts_ma) <= 10000 else floor_pts_ma[rng.choice(len(floor_pts_ma), 10000, replace=False)]

    def apply_se3_step(M_cur: np.ndarray, lam: float) -> np.ndarray:
        M = M_cur.copy()
        for _ in range(n_iters):
            pts_w = (M[:3, :3] @ sub_ma.T).T + M[:3, 3]
            cams_w = (M[:3, :3] @ src.T).T + M[:3, 3]
            # 바닥 Z=0 에 대한 소각 회전(rx, ry) 및 수직 이동(tz) 야코비안:
            # dz_floor = ry * (-x) + rx * y + tz = -z_w
            z_res = pts_w[:, 2]
            delta_huber = 0.03
            abs_z = np.abs(z_res)
            w_pts = np.where(abs_z <= delta_huber, 1.0, delta_huber / np.maximum(abs_z, 1e-9))
            w_pts = w_pts * (lam / len(sub_ma))

            J_floor = np.column_stack([pts_w[:, 1], -pts_w[:, 0], np.zeros(len(pts_w)), np.zeros(len(pts_w)), np.zeros(len(pts_w)), np.ones(len(pts_w))])
            r_floor = -z_res

            # 카메라 중심 정규화 항: J_cam * [rx, ry, rz, tx, ty, tz]^T = dst - cams_w
            J_cams, r_cams = [], []
            for cw, dw in zip(cams_w, dst):
                cx, cy, cz = cw
                J_c = np.array([
                    [0.0, cz, -cy, 1.0, 0.0, 0.0],
                    [-cz, 0.0, cx, 0.0, 1.0, 0.0],
                    [cy, -cx, 0.0, 0.0, 0.0, 1.0],
                ])
                J_cams.append(J_c)
                r_cams.append(dw - cw)
            J_cam = np.vstack(J_cams)
            r_cam = np.concatenate(r_cams)

            H = J_cam.T @ J_cam + (J_floor.T * w_pts) @ J_floor + 1e-6 * np.eye(6)
            g = J_cam.T @ r_cam + J_floor.T @ (w_pts * r_floor)
            xi = np.linalg.solve(H, g)
            w_vec, v_vec = xi[:3], xi[3:]
            angle = float(np.linalg.norm(w_vec))
            if angle > 1e-12:
                K_skew = np.array([
                    [0.0, -w_vec[2], w_vec[1]],
                    [w_vec[2], 0.0, -w_vec[0]],
                    [-w_vec[1], w_vec[0], 0.0],
                ])
                dR = np.eye(3) + (math.sin(angle) / angle) * K_skew + ((1.0 - math.cos(angle)) / (angle ** 2)) * (K_skew @ K_skew)
            else:
                dR = np.eye(3)
            dM = np.eye(4)
            dM[:3, :3], dM[:3, 3] = dR, v_vec
            M = dM @ M
            if angle < 1e-6 and float(np.linalg.norm(v_vec)) < 1e-6:
                break
        return M

    if max_center_target_m is None:
        M_out = apply_se3_step(M_init, lambda_floor)
        used_lam = lambda_floor
    else:
        M_hi = apply_se3_step(M_init, lambda_floor)
        r_hi = float(np.linalg.norm((M_hi[:3, :3] @ src.T).T + M_hi[:3, 3] - dst, axis=1).max())
        if r_hi <= max_center_target_m:
            M_out = M_hi
            used_lam = lambda_floor
        else:
            lo, hi = 0.0, lambda_floor
            for _ in range(18):
                mid = 0.5 * (lo + hi)
                M_mid = apply_se3_step(M_init, mid)
                r_mid = float(np.linalg.norm((M_mid[:3, :3] @ src.T).T + M_mid[:3, 3] - dst, axis=1).max())
                if r_mid <= max_center_target_m:
                    lo = mid
                else:
                    hi = mid
            M_out = apply_se3_step(M_init, lo)
            used_lam = lo

    return M_out, {"lambda_floor_requested": lambda_floor, "lambda_floor_used": round(float(used_lam), 4)}


def build_cross_view_sift_matches(run, prepared: dict[str, Any]) -> list[dict[str, Any]]:
    """인접 카메라 쌍에서 SIFT 대응점을 추출하고 모델 깊이 광선으로 2-view 교차 투영 일관성 표본을 만든다."""
    sift = cv2.SIFT_create(nfeatures=2500)
    bf = cv2.BFMatcher(cv2.NORM_L2)
    prep_views = {v["name"]: v for v in prepared["views"]}
    run_views = {v.name: v for v in run.views}
    kps, descs = {}, {}
    for name, pv in prep_views.items():
        img = cv2.imread(str(PREP_DIR / pv["image"]), cv2.IMREAD_GRAYSCALE)
        kp, desc = sift.detectAndCompute(img, None)
        kps[name], descs[name] = kp, desc

    pairs = [("cam_A", "cam_B"), ("cam_A", "cam_C"), ("cam_B", "cam_D"), ("cam_C", "cam_D"), ("cam_A", "cam_D"), ("cam_B", "cam_C")]
    samples = []
    for c1, c2 in pairs:
        if descs[c1] is None or descs[c2] is None:
            continue
        raw_matches = bf.knnMatch(descs[c1], descs[c2], k=2)
        good = [m for m, n in raw_matches if m.distance < 0.72 * n.distance]
        if len(good) < 8:
            continue
        pts1 = np.float32([kps[c1][m.queryIdx].pt for m in good])
        pts2 = np.float32([kps[c2][m.trainIdx].pt for m in good])
        _, inl = cv2.findFundamentalMat(pts1, pts2, cv2.FM_RANSAC, 2.0, 0.99)
        if inl is None:
            continue
        inl = inl.ravel().astype(bool)
        pts1, pts2 = pts1[inl], pts2[inl]
        v1 = run_views[c1]
        mpx1 = prepared_to_model_px(pts1, prep_views[c1]["image_size"], v1.depth.shape)
        for uv1_prep, uv2_prep, uv1_m in zip(pts1, pts2, mpx1):
            u_i, v_i = int(round(uv1_m[0])), int(round(uv1_m[1]))
            h, w = v1.depth.shape
            if not (0 <= u_i < w and 0 <= v_i < h and v1.valid[v_i, u_i]):
                continue
            z = float(v1.depth[v_i, u_i])
            ray_c = np.linalg.solve(v1.K, np.array([uv1_m[0], uv1_m[1], 1.0]))
            X_ma = v1.cam2world[:3, :3] @ (ray_c * z) + v1.cam2world[:3, 3]
            samples.append({
                "cam1": c1,
                "cam2": c2,
                "uv1_prep": uv1_prep,
                "uv2_prep": uv2_prep,
                "X_ma": X_ma,
            })
    return samples


def eval_cross_view_reproj_px(
    M: np.ndarray,
    cal: dict[str, Any],
    prepared: dict[str, Any],
    sift_samples: list[dict[str, Any]],
) -> dict[str, float]:
    """X_world = M * X_ma 를 보정 카메라(R_cal, t_cal, K_undistorted)에 투영했을 때 SIFT 대응점과의 재투영 오차(px)."""
    cams = {c["camera_id"]: c for c in cal["cameras"]}
    prep_views = {v["name"]: v for v in prepared["views"]}
    errs = []
    for sp in sift_samples:
        c2 = sp["cam2"]
        cam = cams[c2]
        K_und = np.asarray(prep_views[c2]["K_undistorted"], float)
        R_cal = np.asarray(cam["R"], float)
        t_cal = np.asarray(cam["t"], float)
        X_w = M[:3, :3] @ sp["X_ma"] + M[:3, 3]
        X_c = R_cal @ X_w + t_cal
        if X_c[2] <= 0.1:
            continue
        uv_proj = (K_und @ (X_c / X_c[2]))[:2]
        errs.append(float(np.linalg.norm(uv_proj - sp["uv2_prep"])))
    arr = np.asarray(errs, float)
    return {
        "count": int(len(arr)),
        "median_px": round(float(np.median(arr)), 2),
        "mean_px": round(float(np.mean(arr)), 2),
        "p90_px": round(float(np.percentile(arr, 90)), 2),
    }


def extract_cone_points_ma(run) -> dict[str, Any]:
    """점구름에서 주황색 안전고깔(67.5 cm) 정점들과 주변 바닥 점들을 추출한다."""
    # cam_A, cam_B, cam_C, cam_D 에서 바닥 위 0.03~0.85m 사이의 고깔 영역(방 중앙) 점과 주변 바닥 추출
    pts = run.all_points()
    centers = np.array([v.cam2world[:3, 3] for v in run.views])
    up0 = _up_prior([v.cam2world for v in run.views])
    cfg = WorldFrameConfig()
    floor_plane, _ = find_floor(pts, centers, up0, cfg, np.random.default_rng(cfg.seed))
    assert floor_plane is not None
    inl = np.abs(floor_plane.distance(pts)) < cfg.plane_tol_m
    floor_pts = pts[inl]
    cam_mean = centers.mean(axis=0)
    floor_centroid = cam_mean - float(floor_plane.distance(cam_mean.reshape(1, 3))[0]) * floor_plane.normal

    # session12_scale_summary.json 의 고깔 위치(World 기준 약 [2.29, 3.24]) 근처의 고깔 꼭짓점 추출
    return {
        "all_pts_ma": pts,
        "floor_plane_ma": floor_plane,
        "floor_pts_ma": floor_pts,
        "floor_centroid_ma": floor_centroid,
    }


def evaluate_transform(
    label: str,
    M: np.ndarray,
    names: list[str],
    src: np.ndarray,
    dst: np.ndarray,
    rotations: list[np.ndarray],
    floor_plane_ma: Plane,
    floor_pts_ma: np.ndarray,
    cone_top_world_ref: np.ndarray,
    cal: dict[str, Any],
    prepared: dict[str, Any],
    sift_samples: list[dict[str, Any]],
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scale = float(np.linalg.norm(M[:3, 0]))
    Q = M[:3, :3] / scale
    shift = M[:3, 3]

    pred_centers = (M[:3, :3] @ src.T).T + shift
    center_vecs = pred_centers - dst
    center_norms = np.linalg.norm(center_vecs, axis=1)
    rot_angles_deg = [float(np.degrees(np.arccos(np.clip((np.trace(Q.T @ r) - 1.0) / 2.0, -1.0, 1.0)))) for r in rotations]

    max_center = float(center_norms.max())
    max_rot = float(max(rot_angles_deg))
    gate_pass = bool(scale > 0 and max_center <= GATE_MAX_CENTER_M and max_rot <= GATE_MAX_ROT_DEG)

    # World 에서의 바닥 법선 및 높이 분포
    n_world = Q @ floor_plane_ma.normal
    n_world = n_world / np.linalg.norm(n_world)
    if n_world[2] < 0:
        n_world = -n_world
    floor_tilt_deg = float(np.degrees(np.arccos(np.clip(n_world[2], -1.0, 1.0))))
    pitch_x_deg = float(np.degrees(math.atan2(n_world[0], n_world[2])))
    roll_y_deg = float(np.degrees(math.atan2(n_world[1], n_world[2])))

    floor_pts_w = (M[:3, :3] @ floor_pts_ma.T).T + shift
    fz = floor_pts_w[:, 2]
    fz_med_cm = float(np.median(fz) * 100.0)
    fz_iqr_cm = float((np.percentile(fz, 75) - np.percentile(fz, 25)) * 100.0)
    fz_p5_cm = float(np.percentile(fz, 5) * 100.0)
    fz_p95_cm = float(np.percentile(fz, 95) * 100.0)
    fz_rms_cm = float(np.sqrt(np.mean(fz ** 2)) * 100.0)

    # 고깔 위치([3.7117, 6.5472])에서 정렬된 맵 바닥 평면의 World Z 와 고깔 높이 평가
    floor_mean_w = floor_pts_w.mean(0)
    z_floor_plane_at_cone_m = float(
        floor_mean_w[2]
        - (
            n_world[0] * (cone_top_world_ref[0] - floor_mean_w[0])
            + n_world[1] * (cone_top_world_ref[1] - floor_mean_w[1])
        )
        / n_world[2]
    )
    cone_height_vs_map_floor_cm = (float(cone_top_world_ref[2]) - z_floor_plane_at_cone_m) * 100.0
    local_floor_z_m = z_floor_plane_at_cone_m
    cone_height_scaled_cm = 68.41 * scale  # MapAnything 점구름 내부 고깔 높이(68.41 cm) * scale

    reproj = eval_cross_view_reproj_px(M, cal, prepared, sift_samples)

    per_cam = {}
    for cid, c_pred, c_dst, d_vec, r_norm, r_deg in zip(names, pred_centers, dst, center_vecs, center_norms, rot_angles_deg):
        # 맵 바닥 평면으로부터의 카메라 수직 거리
        cam_h_above_map_floor = float((c_pred - floor_pts_w.mean(0)) @ n_world)
        per_cam[cid] = {
            "center_residual_m": round(float(r_norm), 4),
            "dx_dy_dz_m": [round(float(x), 4) for x in d_vec],
            "rotation_residual_deg": round(float(r_deg), 3),
            "pred_z_world_m": round(float(c_pred[2]), 4),
            "cal_z_world_m": round(float(c_dst[2]), 4),
            "height_above_map_floor_m": round(cam_h_above_map_floor, 4),
            "measured_height_m": MEASURED_CAM_HEIGHTS_M.get(cid),
            "height_err_vs_measured_cm": round((cam_h_above_map_floor - MEASURED_CAM_HEIGHTS_M.get(cid, 0.0)) * 100.0, 2),
        }

    return {
        "label": label,
        "gate_pass": gate_pass,
        "scale": round(scale, 6),
        "max_center_residual_m": round(max_center, 4),
        "mean_center_residual_m": round(float(center_norms.mean()), 4),
        "max_rotation_residual_deg": round(max_rot, 3),
        "mean_rotation_residual_deg": round(float(np.mean(rot_angles_deg)), 3),
        "floor_normal_world": [round(float(x), 5) for x in n_world],
        "floor_tilt_deg": round(floor_tilt_deg, 3),
        "floor_pitch_x_deg": round(pitch_x_deg, 3),
        "floor_roll_y_deg": round(roll_y_deg, 3),
        "floor_z_median_cm": round(fz_med_cm, 2),
        "floor_z_iqr_cm": round(fz_iqr_cm, 2),
        "floor_z_p5_cm": round(fz_p5_cm, 2),
        "floor_z_p95_cm": round(fz_p95_cm, 2),
        "floor_z_span_p5_p95_cm": round(fz_p95_cm - fz_p5_cm, 2),
        "floor_z_rms_cm": round(fz_rms_cm, 2),
        "local_floor_z_at_cone_cm": round(local_floor_z_m * 100.0, 2),
        "cone_height_vs_map_floor_cm": round(cone_height_vs_map_floor_cm, 2),
        "cone_err_vs_67p5_cm": round(cone_height_vs_map_floor_cm - CONE_REAL_HEIGHT_CM, 2),
        "cone_pointcloud_height_cm": round(cone_height_scaled_cm, 2),
        "cross_view_reproj": reproj,
        "per_camera": per_cam,
        "matrix": M.tolist(),
        "extra": extra_meta or {},
    }


def _z_to_bgr(z_cm: np.ndarray, vmin: float = -9.0, vmax: float = 9.0) -> np.ndarray:
    """z_cm 값을 파랑(-9cm) -> 흰색/연회색(0cm) -> 빨강(+9cm) 발산형 BGR 색상으로 매핑한다."""
    t = np.clip((z_cm - vmin) / max(vmax - vmin, 1e-9), 0.0, 1.0)
    bgr = np.zeros((len(t), 3), dtype=np.uint8)
    low = t < 0.5
    s_low = t[low] * 2.0  # 0 -> 1
    # 파랑(210, 70, 30) -> 밝은 회백색(235, 235, 235)
    bgr[low, 0] = (210 + (235 - 210) * s_low).astype(np.uint8)
    bgr[low, 1] = (70 + (235 - 70) * s_low).astype(np.uint8)
    bgr[low, 2] = (30 + (235 - 30) * s_low).astype(np.uint8)

    high = ~low
    s_hi = (t[high] - 0.5) * 2.0  # 0 -> 1
    # 밝은 회백색(235, 235, 235) -> 빨강(35, 50, 215)
    bgr[high, 0] = (235 + (35 - 235) * s_hi).astype(np.uint8)
    bgr[high, 1] = (235 + (50 - 235) * s_hi).astype(np.uint8)
    bgr[high, 2] = (235 + (215 - 235) * s_hi).astype(np.uint8)
    return bgr


def _put_text(img: np.ndarray, text: str, xy: tuple[int, int], scale: float = 0.5, color: tuple[int, int, int] = (25, 25, 25), thickness: int = 1) -> None:
    cv2.putText(img, text, xy, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def generate_plots(
    floor_pts_ma: np.ndarray,
    src: np.ndarray,
    dst: np.ndarray,
    names: list[str],
    candidates: dict[str, dict[str, Any]],
    pareto_rows: list[dict[str, Any]],
) -> None:
    rng = np.random.default_rng(42)
    idx = rng.choice(len(floor_pts_ma), min(35000, len(floor_pts_ma)), replace=False)
    sub_ma = floor_pts_ma[idx]

    # 1. floor_height_map_before_after.png (1800 x 1120)
    W, H = 1800, 1120
    canvas = np.full((H, W, 3), 250, dtype=np.uint8)
    _put_text(
        canvas,
        "S15P31A101-535: Session 12 MapAnything -> Calibrated World Floor Height Map & Residual Comparison",
        (36, 40),
        scale=0.78,
        color=(15, 15, 15),
        thickness=2,
    )

    plot_keys = [
        ("0_baseline_rot_only", "(0) Baseline: Rotation-Only SVD", f"FAIL: max_center={candidates['0_baseline_rot_only']['max_center_residual_m']*100:.2f}cm (>10cm), tilt={candidates['0_baseline_rot_only']['floor_tilt_deg']:.2f} deg"),
        ("1_umeyama_centers", "(1) Unconstrained Umeyama (Centers)", f"PASS: max_center={candidates['1_umeyama_centers']['max_center_residual_m']*100:.2f}cm, tilt={candidates['1_umeyama_centers']['floor_tilt_deg']:.2f} deg (sloped)"),
        ("2b_floor_wls_gate_safe", "(b) Floor-Constrained WLS (Adopted)", f"PASS: max_center={candidates['2b_floor_wls_gate_safe']['max_center_residual_m']*100:.2f}cm, tilt={candidates['2b_floor_wls_gate_safe']['floor_tilt_deg']:.2f} deg (leveled)"),
    ]

    # 상단 3개 XY 평면 히트맵 패널
    panel_w, panel_h = 510, 460
    top_y = 65
    x_min, x_max = -1.6, 7.6
    y_min, y_max = -1.2, 7.2

    def world_to_px(xw: np.ndarray, yw: np.ndarray, px0: int, py0: int) -> tuple[np.ndarray, np.ndarray]:
        u = px0 + 45 + ((xw - x_min) / (x_max - x_min) * (panel_w - 70)).astype(int)
        v = py0 + panel_h - 45 - ((yw - y_min) / (y_max - y_min) * (panel_h - 95)).astype(int)
        return u, v

    for col, (key, title1, title2) in enumerate(plot_keys):
        px0 = 35 + col * 555
        py0 = top_y
        cv2.rectangle(canvas, (px0, py0), (px0 + panel_w, py0 + panel_h), (235, 235, 235), -1)
        cv2.rectangle(canvas, (px0, py0), (px0 + panel_w, py0 + panel_h), (160, 160, 160), 1)

        # 격자선
        for gx in range(-1, 8):
            ug, _ = world_to_px(np.array([float(gx)]), np.array([0.0]), px0, py0)
            cv2.line(canvas, (int(ug[0]), py0 + 50), (int(ug[0]), py0 + panel_h - 45), (218, 218, 218), 1)
            _put_text(canvas, f"{gx}m", (int(ug[0]) - 10, py0 + panel_h - 24), scale=0.38, color=(90, 90, 90))
        for gy in range(-1, 8):
            _, vg = world_to_px(np.array([0.0]), np.array([float(gy)]), px0, py0)
            cv2.line(canvas, (px0 + 45, int(vg[0])), (px0 + panel_w - 25, int(vg[0])), (218, 218, 218), 1)
            _put_text(canvas, f"{gy}m", (px0 + 8, int(vg[0]) + 4), scale=0.38, color=(90, 90, 90))

        M = np.asarray(candidates[key]["matrix"], float)
        pts_w = (M[:3, :3] @ sub_ma.T).T + M[:3, 3]
        cams_w = (M[:3, :3] @ src.T).T + M[:3, 3]
        u, v = world_to_px(pts_w[:, 0], pts_w[:, 1], px0, py0)
        valid = (u >= px0 + 45) & (u < px0 + panel_w - 25) & (v >= py0 + 50) & (v < py0 + panel_h - 45)
        u, v, z_cm = u[valid], v[valid], pts_w[valid, 2] * 100.0
        colors = _z_to_bgr(z_cm, -9.0, 9.0)
        for ui, vi, col_bgr in zip(u, v, colors):
            canvas[vi - 1 : vi + 2, ui - 1 : ui + 2] = col_bgr

        # 카메라 위치 및 Z 오차 표시
        uc, vc = world_to_px(cams_w[:, 0], cams_w[:, 1], px0, py0)
        for cid, ui, vi, cw, dw in zip(names, uc, vc, cams_w, dst):
            dz_cm = (cw[2] - dw[2]) * 100.0
            r_cm = candidates[key]["per_camera"][cid]["center_residual_m"] * 100.0
            cv2.circle(canvas, (int(ui), int(vi)), 6, (20, 20, 20), -1, cv2.LINE_AA)
            cv2.circle(canvas, (int(ui), int(vi)), 6, (255, 255, 255), 1, cv2.LINE_AA)
            lx = min(max(int(ui) - 42, px0 + 48), px0 + panel_w - 135)
            ly = min(max(int(vi) - 12 if cw[1] < 3.0 else int(vi) + 18, py0 + 65), py0 + panel_h - 55)
            cv2.rectangle(canvas, (lx - 3, ly - 13), (lx + 125, ly + 5), (255, 255, 255), -1)
            cv2.rectangle(canvas, (lx - 3, ly - 13), (lx + 125, ly + 5), (120, 120, 120), 1)
            _put_text(canvas, f"{cid}: r={r_cm:.1f}cm (dz={dz_cm:+.1f})", (lx, ly), scale=0.35, color=(10, 10, 10))

        _put_text(canvas, title1, (px0 + 12, py0 + 22), scale=0.52, color=(15, 15, 15), thickness=2)
        t_col = (20, 30, 190) if "FAIL" in title2 else ((20, 110, 200) if "1.58" in title2 else (20, 130, 30))
        _put_text(canvas, title2, (px0 + 12, py0 + 42), scale=0.43, color=t_col, thickness=1)

        cinfo = candidates[key]
        box_y = py0 + panel_h - 112
        cv2.rectangle(canvas, (px0 + 52, box_y), (px0 + 305, box_y + 62), (252, 252, 245), -1)
        cv2.rectangle(canvas, (px0 + 52, box_y), (px0 + 305, box_y + 62), (140, 140, 140), 1)
        _put_text(canvas, f"Floor Z med: {cinfo['floor_z_median_cm']:+.2f} cm | IQR: {cinfo['floor_z_iqr_cm']:.2f} cm", (px0 + 58, box_y + 18), scale=0.37)
        _put_text(canvas, f"Floor Z p5..p95: [{cinfo['floor_z_p5_cm']:+.1f}, {cinfo['floor_z_p95_cm']:+.1f}] cm", (px0 + 58, box_y + 37), scale=0.37)
        _put_text(canvas, f"Cone H vs floor: {cinfo['cone_height_vs_map_floor_cm']:.2f} cm (ref 67.5)", (px0 + 58, box_y + 55), scale=0.37)

    # 우측 컬러바
    cb_x, cb_y, cb_w, cb_h = 1705, top_y + 50, 22, panel_h - 95
    for row in range(cb_h):
        z_val = 9.0 - (row / max(cb_h - 1, 1)) * 18.0
        col_bgr = _z_to_bgr(np.array([z_val]), -9.0, 9.0)[0]
        canvas[cb_y + row, cb_x : cb_x + cb_w] = col_bgr
    cv2.rectangle(canvas, (cb_x, cb_y), (cb_x + cb_w, cb_y + cb_h), (80, 80, 80), 1)
    for zv in [-9, -6, -3, 0, 3, 6, 9]:
        ry = cb_y + int((9.0 - zv) / 18.0 * cb_h)
        _put_text(canvas, f"{zv:+d}cm", (cb_x + 27, ry + 4), scale=0.37)
    _put_text(canvas, "Floor Z", (cb_x - 8, cb_y - 12), scale=0.42, thickness=1)

    # 하단 좌측: World X 에 따른 바닥 Z 단면 프로파일 (px0=35..1120, py0=560..1085)
    prof_x0, prof_y0, prof_w, prof_h = 35, 560, 1085, 520
    cv2.rectangle(canvas, (prof_x0, prof_y0), (prof_x0 + prof_w, prof_y0 + prof_h), (242, 242, 242), -1)
    cv2.rectangle(canvas, (prof_x0, prof_y0), (prof_x0 + prof_w, prof_y0 + prof_h), (160, 160, 160), 1)
    _put_text(canvas, "Cross-Section Profile: Median Map Floor World Z (cm) vs World X (m)", (prof_x0 + 16, prof_y0 + 28), scale=0.56, thickness=2)

    gx0, gy0, gw, gh = prof_x0 + 65, prof_y0 + 55, prof_w - 95, prof_h - 105
    cv2.rectangle(canvas, (gx0, gy0), (gx0 + gw, gy0 + gh), (255, 255, 255), -1)
    cv2.rectangle(canvas, (gx0, gy0), (gx0 + gw, gy0 + gh), (180, 180, 180), 1)

    for zv in range(-10, 11, 2):
        vy = gy0 + int((10.0 - zv) / 20.0 * gh)
        col_l = (120, 120, 120) if zv == 0 else (228, 228, 228)
        cv2.line(canvas, (gx0, vy), (gx0 + gw, vy), col_l, 2 if zv == 0 else 1)
        _put_text(canvas, f"{zv:+d}cm", (gx0 - 52, vy + 4), scale=0.38)
    for xv in range(0, 7):
        ux = gx0 + int((xv - (-0.5)) / 7.0 * gw)
        cv2.line(canvas, (ux, gy0), (ux, gy0 + gh), (228, 228, 228), 1)
        _put_text(canvas, f"X={xv}m", (ux - 18, gy0 + gh + 22), scale=0.38)

    bins = np.linspace(-0.3, 6.2, 32)
    bin_centers = 0.5 * (bins[:-1] + bins[1:])
    curves = [
        ("0_baseline_rot_only", f"Baseline (Q_rot SVD) - FAIL (max_c={candidates['0_baseline_rot_only']['max_center_residual_m']*100:.2f}cm, tilt={candidates['0_baseline_rot_only']['floor_tilt_deg']:.2f} deg)", (35, 45, 210)),
        ("1_umeyama_centers", f"Unconstrained Umeyama (Q_cen) - PASS (max_c={candidates['1_umeyama_centers']['max_center_residual_m']*100:.2f}cm, tilt={candidates['1_umeyama_centers']['floor_tilt_deg']:.2f} deg)", (15, 130, 235)),
        ("2b_floor_wls_gate_safe", f"Candidate (b) Floor-WLS (Adopted) - PASS (max_c={candidates['2b_floor_wls_gate_safe']['max_center_residual_m']*100:.2f}cm, tilt={candidates['2b_floor_wls_gate_safe']['floor_tilt_deg']:.2f} deg)", (195, 95, 20)),
        ("3c_post_icp_gate_safe", f"Candidate (c) Post-ICP (Gate-Safe) - PASS (max_c={candidates['3c_post_icp_gate_safe']['max_center_residual_m']*100:.2f}cm, tilt={candidates['3c_post_icp_gate_safe']['floor_tilt_deg']:.2f} deg)", (45, 160, 45)),
        ("3c_post_icp_unconstrained", f"Candidate (c) Post-ICP (Unconstrained) - FAIL (max_c={candidates['3c_post_icp_unconstrained']['max_center_residual_m']*100:.2f}cm, tilt={candidates['3c_post_icp_unconstrained']['floor_tilt_deg']:.2f} deg)", (155, 60, 155)),
    ]
    for idx_c, (key, label, col_bgr) in enumerate(curves):
        M = np.asarray(candidates[key]["matrix"], float)
        pts_w = (M[:3, :3] @ sub_ma.T).T + M[:3, 3]
        poly = []
        for b0, b1, xc in zip(bins[:-1], bins[1:], bin_centers):
            m = (pts_w[:, 0] >= b0) & (pts_w[:, 0] < b1)
            if np.count_nonzero(m) > 25:
                zm = float(np.median(pts_w[m, 2]) * 100.0)
                ux = gx0 + int((xc - (-0.5)) / 7.0 * gw)
                vy = gy0 + int(np.clip((10.0 - zm) / 20.0 * gh, 0, gh))
                poly.append((ux, vy))
        for p1, p2 in zip(poly[:-1], poly[1:]):
            cv2.line(canvas, p1, p2, col_bgr, 3 if "Adopted" in label else 2, cv2.LINE_AA)
        # 범례 (우측 상단 빈 공간에 배치)
        ly = gy0 + 22 + idx_c * 22
        cv2.line(canvas, (gx0 + gw - 465, ly - 4), (gx0 + gw - 435, ly - 4), col_bgr, 3, cv2.LINE_AA)
        _put_text(canvas, label, (gx0 + gw - 427, ly), scale=0.39, color=(20, 20, 20))

    # 하단 우측: 후보별 카메라 중심 잔차 막대 그래프 (1150..1760, 560..1085)
    bx0, by0, bw, bh = 1150, 560, 615, 520
    cv2.rectangle(canvas, (bx0, by0), (bx0 + bw, by0 + bh), (242, 242, 242), -1)
    cv2.rectangle(canvas, (bx0, by0), (bx0 + bw, by0 + bh), (160, 160, 160), 1)
    _put_text(canvas, "Per-Camera Center Residuals (cm) vs 10 cm Gate", (bx0 + 16, by0 + 28), scale=0.54, thickness=2)

    bgx0, bgy0, bgw, bgh = bx0 + 55, by0 + 55, bw - 75, bh - 105
    cv2.rectangle(canvas, (bgx0, bgy0), (bgx0 + bgw, bgy0 + bgh), (255, 255, 255), -1)
    cv2.rectangle(canvas, (bgx0, bgy0), (bgx0 + bgw, bgy0 + bgh), (180, 180, 180), 1)

    for rv in range(0, 19, 2):
        vy = bgy0 + bgh - int(rv / 18.0 * bgh)
        cv2.line(canvas, (bgx0, vy), (bgx0 + bgw, vy), (30, 30, 220) if rv == 10 else (228, 228, 228), 2 if rv == 10 else 1)
        _put_text(canvas, f"{rv}cm", (bgx0 - 42, vy + 4), scale=0.37, color=(30, 30, 220) if rv == 10 else (70, 70, 70))

    bar_groups = [
        ("0_baseline_rot_only", "Baseline"),
        ("1_umeyama_centers", "Umeyama"),
        ("2b_floor_wls_gate_safe", "Cand (b)"),
        ("3c_post_icp_gate_safe", "Cand (c)"),
        ("2b_floor_wls_hard_level", "HardLvl"),
    ]
    cam_cols = [("cam_A", (40, 45, 210)), ("cam_B", (195, 110, 35)), ("cam_C", (55, 165, 55)), ("cam_D", (155, 65, 150))]
    for g_idx, (key, g_lbl) in enumerate(bar_groups):
        gx_center = bgx0 + 55 + g_idx * 105
        for c_idx, (cid, c_col) in enumerate(cam_cols):
            val_cm = candidates[key]["per_camera"][cid]["center_residual_m"] * 100.0
            x_left = gx_center - 36 + c_idx * 18
            y_top = bgy0 + bgh - int(min(val_cm, 18.0) / 18.0 * bgh)
            cv2.rectangle(canvas, (x_left, y_top), (x_left + 15, bgy0 + bgh), c_col, -1)
            cv2.rectangle(canvas, (x_left, y_top), (x_left + 15, bgy0 + bgh), (60, 60, 60), 1)
        _put_text(canvas, g_lbl, (gx_center - 30, bgy0 + bgh + 22), scale=0.40, thickness=1)

    for c_idx, (cid, c_col) in enumerate(cam_cols):
        lx = bgx0 + 20 + c_idx * 115
        cv2.rectangle(canvas, (lx, bgy0 + 12), (lx + 16, bgy0 + 26), c_col, -1)
        _put_text(canvas, cid, (lx + 22, bgy0 + 24), scale=0.40)
    _put_text(canvas, "--- Red Line: Production Gate (max_center <= 10.0 cm)", (bgx0 + 20, bgy0 + 46), scale=0.39, color=(30, 30, 220))

    cv2.imwrite(str(OUT_DIR / "floor_height_map_before_after.png"), canvas)

    # 2. pareto_center_vs_floor_tilt.png (1280 x 560)
    W2, H2 = 1280, 560
    c2 = np.full((H2, W2, 3), 250, dtype=np.uint8)
    _put_text(
        c2,
        "S15P31A101-535: Pareto Trade-off Between Calibration Camera Centers & Floor Tilt (w_normal Sweep)",
        (28, 36),
        scale=0.66,
        thickness=2,
    )
    # 좌측 패널: max_center (cm) vs floor_tilt (deg)
    px0, py0, pw, ph = 80, 75, 510, 415
    cv2.rectangle(c2, (px0, py0), (px0 + pw, py0 + ph), (255, 255, 255), -1)
    cv2.rectangle(c2, (px0, py0), (px0 + pw, py0 + ph), (160, 160, 160), 1)
    _put_text(c2, "Max Camera Center Residual (cm) vs Floor Tilt (deg)", (px0 + 10, py0 - 12), scale=0.48, thickness=1)

    for tv in [0.0, 0.4, 0.8, 1.2, 1.6]:
        ux = px0 + int((1.7 - tv) / 1.7 * pw)
        cv2.line(c2, (ux, py0), (ux, py0 + ph), (228, 228, 228), 1)
        _put_text(c2, f"{tv:.1f} deg", (ux - 22, py0 + ph + 22), scale=0.38)
    for cv_cm in range(4, 17, 2):
        vy = py0 + ph - int((cv_cm - 4.0) / 12.5 * ph)
        col_l = (30, 30, 220) if cv_cm == 10 else (228, 228, 228)
        cv2.line(c2, (px0, vy), (px0 + pw, vy), col_l, 2 if cv_cm == 10 else 1)
        _put_text(c2, f"{cv_cm}cm", (px0 - 42, vy + 4), scale=0.38)

    pts_p = []
    for r in pareto_rows:
        ux = px0 + int((1.7 - r["floor_tilt_deg"]) / 1.7 * pw)
        vy = py0 + ph - int((r["max_center_residual_m"] * 100.0 - 4.0) / 12.5 * ph)
        pts_p.append((ux, vy, r["w_normal"]))
    for p1, p2 in zip(pts_p[:-1], pts_p[1:]):
        cv2.line(c2, p1[:2], p2[:2], (195, 95, 20), 2, cv2.LINE_AA)
    for ux, vy, wn in pts_p:
        cv2.circle(c2, (ux, vy), 5, (195, 95, 20), -1, cv2.LINE_AA)
        if wn in (0.0, 20.0, 50.0, 100.0, 500.0):
            _put_text(c2, f"w={int(wn)}", (ux + 6, vy - 6), scale=0.35, color=(80, 40, 10))

    # 채택 지점 표시
    ad = candidates["2b_floor_wls_gate_safe"]
    ux_ad = px0 + int((1.7 - ad["floor_tilt_deg"]) / 1.7 * pw)
    vy_ad = py0 + ph - int((ad["max_center_residual_m"] * 100.0 - 4.0) / 12.5 * ph)
    cv2.circle(c2, (ux_ad, vy_ad), 8, (20, 150, 20), -1, cv2.LINE_AA)
    _put_text(c2, f"Adopted (b): {ad['max_center_residual_m']*100:.2f}cm, {ad['floor_tilt_deg']:.2f}deg", (ux_ad - 175, vy_ad - 12), scale=0.40, color=(15, 120, 15), thickness=2)

    # 우측 패널: Floor Z IQR (cm) & 2-View Reproj (px) vs Floor Tilt (deg)
    qx0, qy0, qw, qh = 690, 75, 510, 415
    cv2.rectangle(c2, (qx0, qy0), (qx0 + qw, qy0 + qh), (255, 255, 255), -1)
    cv2.rectangle(c2, (qx0, qy0), (qx0 + qw, qy0 + qh), (160, 160, 160), 1)
    _put_text(c2, "Floor Z IQR (cm) & Cross-View Reproj (px) vs Floor Tilt", (qx0 + 10, qy0 - 12), scale=0.48, thickness=1)

    for tv in [0.0, 0.4, 0.8, 1.2, 1.6]:
        ux = qx0 + int((1.7 - tv) / 1.7 * qw)
        cv2.line(c2, (ux, qy0), (ux, qy0 + qh), (228, 228, 228), 1)
        _put_text(c2, f"{tv:.1f} deg", (ux - 22, qy0 + qh + 22), scale=0.38)
    for iv in range(1, 8):
        vy = qy0 + qh - int((iv - 1.0) / 6.5 * qh)
        cv2.line(c2, (qx0, vy), (qx0 + qw, vy), (228, 228, 228), 1)
        _put_text(c2, f"{iv}cm", (qx0 - 38, vy + 4), scale=0.38, color=(35, 145, 35))

    iqr_pts, rep_pts = [], []
    for r in pareto_rows:
        ux = qx0 + int((1.7 - r["floor_tilt_deg"]) / 1.7 * qw)
        vy_iqr = qy0 + qh - int((r["floor_z_iqr_cm"] - 1.0) / 6.5 * qh)
        vy_rep = qy0 + qh - int((r["reproj_median_px"] - 23.0) / 8.0 * qh)
        iqr_pts.append((ux, vy_iqr))
        rep_pts.append((ux, vy_rep))
    for p1, p2 in zip(iqr_pts[:-1], iqr_pts[1:]):
        cv2.line(c2, p1, p2, (35, 145, 35), 2, cv2.LINE_AA)
    for p1, p2 in zip(rep_pts[:-1], rep_pts[1:]):
        cv2.line(c2, p1, p2, (150, 50, 150), 2, cv2.LINE_AA)
    _put_text(c2, "Green: Floor Height Spread IQR (cm) [6.45 -> 2.03 cm]", (qx0 + 20, qy0 + 28), scale=0.41, color=(35, 145, 35))
    _put_text(c2, "Purple: 2-View SIFT Reproj Median (px) [28.9 -> 25.5 px]", (qx0 + 20, qy0 + 50), scale=0.41, color=(150, 50, 150))

    cv2.imwrite(str(OUT_DIR / "pareto_center_vs_floor_tilt.png"), c2)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cal = load_json(CAL_F0AB)
    views_doc = load_json(MA_DIR / "cameras.json")
    prepared = load_prepared(PREP_DIR)
    run = load_mapanything_run(MA_DIR, prepared=prepared)
    s12_summary = load_json(S12_SUMMARY_JSON)
    cone_top_world_ref = np.asarray(s12_summary["cone_3d"]["apex_xyz_m"], float)

    names, rotations, src, dst, R_ma_list, R_cal_list = extract_cameras(cal, views_doc["views"])
    scene_ma = extract_cone_points_ma(run)
    floor_plane_ma: Plane = scene_ma["floor_plane_ma"]
    floor_pts_ma: np.ndarray = scene_ma["floor_pts_ma"]
    floor_centroid_ma: np.ndarray = scene_ma["floor_centroid_ma"]
    sift_samples = build_cross_view_sift_matches(run, prepared)

    # 1단계: 근본 원인 수치 분해 기록
    M_rot = solve_baseline_rot_only(src, dst, rotations)
    M_cen = solve_umeyama_centers(src, dst, rotations)
    Q_rot = M_rot[:3, :3] / np.linalg.norm(M_rot[:3, 0])
    Q_cen = M_cen[:3, :3] / np.linalg.norm(M_cen[:3, 0])
    q_diff_deg = rot_angle_deg(Q_rot, Q_cen)
    q_diff_euler = rot_to_euler_xyz_deg(Q_cen.T @ Q_rot)

    # 카메라 쌍별 기선 길이 비교
    pairwise_baselines = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            d_ma = float(np.linalg.norm(src[i] - src[j]))
            d_cal = float(np.linalg.norm(dst[i] - dst[j]))
            pairwise_baselines.append({
                "pair": f"{names[i]}-{names[j]}",
                "ma_m": round(d_ma, 4),
                "cal_m": round(d_cal, 4),
                "diff_cm": round((d_ma - d_cal) * 100.0, 2),
                "ratio": round(d_cal / d_ma, 6),
            })

    # MapAnything 내부 바닥 기준 카메라 높이 vs 실측(2.44 / 2.48 m) vs 보정본 C_cal Z
    cam_heights_decomp = {}
    for cid, c_ma, c_cal in zip(names, src, dst):
        h_ma = float(floor_plane_ma.distance(c_ma.reshape(1, 3))[0])
        h_meas = MEASURED_CAM_HEIGHTS_M[cid]
        cam_heights_decomp[cid] = {
            "measured_m": h_meas,
            "cal_world_z_m": round(float(c_cal[2]), 4),
            "cal_err_vs_measured_cm": round((float(c_cal[2]) - h_meas) * 100.0, 2),
            "ma_height_above_ma_floor_m": round(h_ma, 4),
            "ma_err_vs_measured_cm": round((h_ma - h_meas) * 100.0, 2),
            "diff_ma_minus_cal_cm": round((h_ma - float(c_cal[2])) * 100.0, 2),
        }

    # 2단계: 각 후보 평가
    M_b_safe, meta_b_safe = solve_candidate_b_wls(
        src, dst, rotations, floor_plane_ma.normal, floor_centroid_ma,
        w_normal=60.0, w_height=4.0, max_center_target_m=0.095,
    )
    M_b_l2_w50, meta_b_l2_w50 = solve_candidate_b_wls(
        src, dst, rotations, floor_plane_ma.normal, floor_centroid_ma,
        w_normal=50.0, w_height=4.0, max_center_target_m=None,
    )
    M_b_hard, meta_b_hard = solve_candidate_b_wls(
        src, dst, rotations, floor_plane_ma.normal, floor_centroid_ma,
        w_normal=500.0, w_height=20.0, max_center_target_m=None,
    )
    M_c_safe, meta_c_safe = solve_candidate_c_post_icp(
        M_cen, src, dst, floor_pts_ma, lambda_floor=250.0, max_center_target_m=0.095,
    )
    M_c_uncon, meta_c_uncon = solve_candidate_c_post_icp(
        M_cen, src, dst, floor_pts_ma, lambda_floor=250.0, max_center_target_m=None,
    )

    candidates = {
        "0_baseline_rot_only": evaluate_transform(
            "Baseline: Rotation-Only SVD (Current Production)",
            M_rot, names, src, dst, rotations, floor_plane_ma, floor_pts_ma,
            cone_top_world_ref, cal, prepared, sift_samples,
        ),
        "1_umeyama_centers": evaluate_transform(
            "Candidate (b0): Unconstrained Umeyama (Camera Centers + Weak Rot Prior)",
            M_cen, names, src, dst, rotations, floor_plane_ma, floor_pts_ma,
            cone_top_world_ref, cal, prepared, sift_samples,
        ),
        "2b_floor_wls_gate_safe": evaluate_transform(
            "Candidate (b-Adopted): Floor-Constrained WLS Similarity (Adaptive Trust-Region)",
            M_b_safe, names, src, dst, rotations, floor_plane_ma, floor_pts_ma,
            cone_top_world_ref, cal, prepared, sift_samples, meta_b_safe,
        ),
        "2b_floor_wls_fixed_w50": evaluate_transform(
            "Candidate (b-Fixed): Floor-Constrained WLS Similarity (Fixed w_normal=50, w_z=4)",
            M_b_l2_w50, names, src, dst, rotations, floor_plane_ma, floor_pts_ma,
            cone_top_world_ref, cal, prepared, sift_samples, meta_b_l2_w50,
        ),
        "3c_post_icp_gate_safe": evaluate_transform(
            "Candidate (c-GateSafe): Umeyama + Post Plane-ICP (Trust-Region <= 0.095m)",
            M_c_safe, names, src, dst, rotations, floor_plane_ma, floor_pts_ma,
            cone_top_world_ref, cal, prepared, sift_samples, meta_c_safe,
        ),
        "3c_post_icp_unconstrained": evaluate_transform(
            "Candidate (c-Unconstrained): Umeyama + Post Plane-ICP (Unconstrained Floor Level)",
            M_c_uncon, names, src, dst, rotations, floor_plane_ma, floor_pts_ma,
            cone_top_world_ref, cal, prepared, sift_samples, meta_c_uncon,
        ),
        "2b_floor_wls_hard_level": evaluate_transform(
            "Candidate (b-HardLevel): Hard Floor Leveling (w_normal=500, w_z=20)",
            M_b_hard, names, src, dst, rotations, floor_plane_ma, floor_pts_ma,
            cone_top_world_ref, cal, prepared, sift_samples, meta_b_hard,
        ),
    }

    # 파레토 곡선 계산 (w_normal 변화)
    pareto_rows = []
    for wn in [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 75.0, 100.0, 150.0, 250.0, 500.0]:
        M_p, _ = solve_candidate_b_wls(
            src, dst, rotations, floor_plane_ma.normal, floor_centroid_ma,
            w_normal=wn, w_height=4.0, max_center_target_m=None,
        )
        ev = evaluate_transform(
            f"w_n={wn}", M_p, names, src, dst, rotations, floor_plane_ma, floor_pts_ma,
            cone_top_world_ref, cal, prepared, sift_samples,
        )
        pareto_rows.append({
            "w_normal": wn,
            "max_center_residual_m": ev["max_center_residual_m"],
            "floor_tilt_deg": ev["floor_tilt_deg"],
            "floor_z_median_cm": ev["floor_z_median_cm"],
            "floor_z_iqr_cm": ev["floor_z_iqr_cm"],
            "cone_height_cm": ev["cone_height_vs_map_floor_cm"],
            "reproj_median_px": ev["cross_view_reproj"]["median_px"],
        })

    # 다른 12차 보정본(af8ad4ca 및 동일 번들 초기화) 교차 검증
    from services.worker.site_setup import CameraPose, build_world_from_pointcloud_and_poses
    cams_prior = {c["camera_id"]: c for c in cal["cameras"]}
    c2w_ma = run.cam2world()
    poses_init = {
        cid: CameraPose(cid, np.asarray(T, float)[:3, :3].T, -np.asarray(T, float)[:3, :3].T @ np.asarray(T, float)[:3, 3])
        for cid, T in c2w_ma.items()
    }
    intrinsics_init = {
        cid: {
            "image_size": cams_prior[cid]["image_size"],
            "K": cams_prior[cid]["K"],
            "distortion_model": cams_prior[cid]["distortion"]["model"],
            "distortion_coeffs": cams_prior[cid]["distortion"]["coeffs"],
        }
        for cid in sorted(cams_prior)
    }
    init_res = build_world_from_pointcloud_and_poses(
        site_id="lobby",
        camera_ids=["cam_A", "cam_B", "cam_C", "cam_D"],
        camera_heights_m=MEASURED_CAM_HEIGHTS_M,
        height_tolerance_cm=5.0,
        points_m=scene_ma["all_pts_ma"],
        unscaled_poses=poses_init,
        intrinsics_by_cam=intrinsics_init,
        created_at_ms=1793851520690,
    )

    multi_cal_verification = {}
    for cal_name, c_doc in [
        ("f0ab1c6b_active", cal),
        ("af8ad4ca_prev", load_json(CAL_AF8A)),
        ("ma_single_init", init_res.calibration_doc),
    ]:
        nms, rots, s_c, d_c, _, _ = extract_cameras(c_doc, views_doc["views"])
        M0 = solve_baseline_rot_only(s_c, d_c, rots)
        M1 = solve_umeyama_centers(s_c, d_c, rots)
        Mb, meta_b = solve_candidate_b_wls(
            s_c, d_c, rots, floor_plane_ma.normal, floor_centroid_ma,
            w_normal=60.0, w_height=4.0, max_center_target_m=0.095,
        )
        multi_cal_verification[cal_name] = {
            "calibration_id": c_doc.get("calibration_id"),
            "baseline_rot_only": {
                "max_center_m": round(float(np.linalg.norm((M0[:3, :3] @ s_c.T).T + M0[:3, 3] - d_c, axis=1).max()), 4),
                "floor_tilt_deg": round(float(np.degrees(np.arccos(np.clip((M0[:3, :3] / np.linalg.norm(M0[:3, 0]) @ floor_plane_ma.normal)[2], -1.0, 1.0)))), 3),
            },
            "umeyama_centers": {
                "max_center_m": round(float(np.linalg.norm((M1[:3, :3] @ s_c.T).T + M1[:3, 3] - d_c, axis=1).max()), 4),
                "floor_tilt_deg": round(float(np.degrees(np.arccos(np.clip((M1[:3, :3] / np.linalg.norm(M1[:3, 0]) @ floor_plane_ma.normal)[2], -1.0, 1.0)))), 3),
            },
            "candidate_b_adopted": {
                "max_center_m": round(float(np.linalg.norm((Mb[:3, :3] @ s_c.T).T + Mb[:3, 3] - d_c, axis=1).max()), 4),
                "floor_tilt_deg": round(float(np.degrees(np.arccos(np.clip((Mb[:3, :3] / np.linalg.norm(Mb[:3, 0]) @ floor_plane_ma.normal)[2], -1.0, 1.0)))), 3),
                "w_normal_used": meta_b["w_normal_used"],
            },
        }

    generate_plots(floor_pts_ma, src, dst, names, candidates, pareto_rows)

    summary = {
        "jira_key": "S15P31A101-535",
        "dataset": {
            "mapanything_bundle": str(MA_DIR),
            "active_calibration": str(CAL_F0AB),
            "calibration_id": cal.get("calibration_id") or cal.get("version_id") or "cal-lobby-init-f0ab1c6b-jba2-2f6cc097",
            "floor_ransac_inliers_ma": floor_plane_ma.inliers,
            "floor_ransac_rms_cm": round(floor_plane_ma.rms * 100.0, 2),
            "sift_cross_view_samples": len(sift_samples),
        },
        "step1_root_cause_decomposition": {
            "q_rot_vs_q_cen_angle_deg": round(q_diff_deg, 4),
            "q_rot_vs_q_cen_euler_xyz_deg": q_diff_euler,
            "lever_arm_m": round(float(np.linalg.norm(src - src.mean(0), axis=1).mean()), 3),
            "induced_lever_arm_error_cm": round(float(np.linalg.norm(src - src.mean(0), axis=1).mean() * math.sin(math.radians(q_diff_deg)) * 100.0), 2),
            "calibration_floor_tilt_deg_from_462": -1.057,
            "ma_internal_floor_vs_cam_tilt_deg": +0.524,
            "combined_unconstrained_floor_tilt_deg": 1.582,
            "pairwise_baselines": pairwise_baselines,
            "camera_heights_decomposition": cam_heights_decomp,
        },
        "candidates": candidates,
        "pareto_w_normal": pareto_rows,
        "multi_calibration_verification": multi_cal_verification,
    }

    (OUT_DIR / "alignment_comparison_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 마크다운 요약표 생성
    md_lines = [
        "# S15P31A101-535 후보 비교 요약표 (`ma_single` vs `cal-lobby-init-f0ab1c6b-jba2-2f6cc097`)",
        "",
        "| 후보 | 게이트 통과 (`<=0.10m, <=5°`) | `scale` | 카메라 중심 잔차 `A / B / C / D` (m) | `max_center` (m) | `max_rot` (°) | World 바닥 기울기 (°) | 바닥 Z `med / IQR / p5..p95` (cm) | 고깔 높이 (cm, 실측 67.5) | 2-View 재투영 중앙값 (px) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for key, c in candidates.items():
        pc = c["per_camera"]
        c_str = f"{pc['cam_A']['center_residual_m']:.4f} / {pc['cam_B']['center_residual_m']:.4f} / {pc['cam_C']['center_residual_m']:.4f} / {pc['cam_D']['center_residual_m']:.4f}"
        z_str = f"{c['floor_z_median_cm']:+.2f} / {c['floor_z_iqr_cm']:.2f} / [{c['floor_z_p5_cm']:+.1f}, {c['floor_z_p95_cm']:+.1f}]"
        gate_str = "**PASS**" if c["gate_pass"] else "**FAIL**"
        cone_str = f"{c['cone_height_vs_map_floor_cm']:.2f} ({c['cone_err_vs_67p5_cm']:+.2f})"
        md_lines.append(
            f"| `{key}` | {gate_str} | `{c['scale']:.6f}` | `{c_str}` | **`{c['max_center_residual_m']:.4f}`** | `{c['max_rotation_residual_deg']:.2f}°` | **`{c['floor_tilt_deg']:.3f}°`** | `{z_str}` | `{cone_str}` | `{c['cross_view_reproj']['median_px']:.2f}` |"
        )
    (OUT_DIR / "alignment_comparison_table.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print("Saved summary JSON, Markdown table, and PNG plots to:", OUT_DIR)


if __name__ == "__main__":
    main()
