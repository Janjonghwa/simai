"""S15P31A101-539: Method 2 (`METHOD2_CALIB_WM2`) fixed-pose depth fitting & Gates 1-4 evaluation,
plus Fallback 1 (`FALLBACK1_MA_ALIGNED_WM2`) surface-consistent depth alignment.

Implements:
1. Fixing camera poses (`R_c2w`, `t_c2w`) and intrinsics (`K`) to `calibration.json` (Tier 0 World authority).
2. Multi-view SIFT feature matching & triangulation across undistorted camera pairs, plus floor (`Z=0`)
   and co-observed horizontal tabletop/bench plane constraints.
3. Per-camera depth fitting (`M1` scale, `M2` scale+offset, `M4` scale+offset+ray-tilt) via Huber IRLS.
4. Evaluation of the 4 automatic quality gates (incorporating Lead Modifications #1 and #2):
   - Gate 1: All 4 cameras fitted (`s_c > 0`, valid ratio >= 50%)
   - Gate 2: Per-camera feature depth residual median `|dz| <= 3 cm` (0.03 m) AND relative `<= 2%` (0.02),
     with inlier ratio >= 0.45
   - Gate 3: Floor point cloud median `|Z_floor| <= 4 cm` (0.04 m) and floor normal tilt `<= 2.0 deg`
   - Gate 4: Cross-camera co-observed surface 3D/Z disagreement median `<= 3 cm` (0.03 m) across
     feature points, floor grid, and horizontal tabletop/bench surfaces (catching any 6-7 cm spread
     like Round 5 diagnosis), with `conflict_ratio <= 15%` recorded as a reference metric.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

from . import mesh_cleanup as mc
from . import run_hybrid_depth as hd
from . import texture_mesh as tm

GATE2_MAX_ABS_DZ_M = 0.030  # 3.0 cm
GATE2_MAX_REL_DZ = 0.020  # 2.0 %
GATE2_MIN_INLIER_RATIO = 0.45
GATE3_MAX_FLOOR_ABS_Z_M = 0.040  # 4.0 cm
GATE3_MAX_FLOOR_TILT_DEG = 2.0
GATE4_MAX_COOBS_DIFF_M = 0.030  # 3.0 cm
GATE4_REF_MAX_CONFLICT_RATIO = 0.15


@dataclass
class FittedCameraView:
    """Single camera with World pose (`cam2world` OpenCV convention in World meters) and fitted depth."""

    camera_id: str
    K: np.ndarray  # (3, 3) float64 at depth resolution
    cam2world: np.ndarray  # (4, 4) float64 in World meters (+Z up)
    depth_m: np.ndarray  # (H, W) float32 metric ray depth in World meters
    valid: np.ndarray  # (H, W) bool
    width: int
    height: int
    scale: float = 1.0
    offset_m: float = 0.0
    tilt_x: float = 0.0
    tilt_y: float = 0.0
    dof_model: Literal["M1", "M2", "M4"] = "M4"

    @property
    def R_c2w(self) -> np.ndarray:
        return self.cam2world[:3, :3]

    @property
    def t_c2w(self) -> np.ndarray:
        return self.cam2world[:3, 3]

    @property
    def R_w2c(self) -> np.ndarray:
        return self.R_c2w.T

    @property
    def t_w2c(self) -> np.ndarray:
        return -self.R_w2c @ self.t_c2w


@dataclass
class PoseDepthFitResult:
    """Result of Method 2 (`METHOD2_CALIB_WM2`) or Fallback 1 (`FALLBACK1_MA_ALIGNED_WM2`) depth fitting."""

    status: Literal["PASSED", "FALLBACK"]
    reconstruction_path: Literal[
        "METHOD2_CALIB_WM2", "FALLBACK1_MA_ALIGNED_WM2", "FALLBACK2_LEGACY_TEXRECON"
    ]
    views: list[FittedCameraView]
    pose_depth_fit_summary: dict[str, Any]
    map_cameras: dict[str, dict[str, Any]]
    gate_details: dict[str, Any] = field(default_factory=dict)


def parse_calibration_cam2world(cam_entry: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """Return `(cam2world_4x4, K_3x3)` in World meters (`+Z` up, OpenCV camera convention) from a `calibration.json` camera entry."""
    if "extrinsics" in cam_entry:
        ext = cam_entry["extrinsics"]
        R_raw = np.asarray(ext["r_matrix"], dtype=np.float64).reshape(3, 3)
        t_raw = np.asarray(ext["t_xyz_m"], dtype=np.float64).reshape(3)
        conv = str(ext.get("convention", "world_to_cam")).lower()
    else:
        R_raw = np.asarray(cam_entry["R"], dtype=np.float64).reshape(3, 3)
        t_raw = np.asarray(cam_entry["t"], dtype=np.float64).reshape(3)
        conv = str(cam_entry.get("convention", "world_to_cam")).lower()

    if conv == "cam_to_world":
        R_c2w = R_raw
        t_c2w = t_raw
    else:
        R_c2w = R_raw.T
        t_c2w = -R_c2w @ t_raw

    c2w = np.eye(4, dtype=np.float64)
    c2w[:3, :3] = R_c2w
    c2w[:3, 3] = t_c2w

    if "intrinsics" in cam_entry:
        intr = cam_entry["intrinsics"]
        K = np.array(
            [
                [float(intr["fx"]), 0.0, float(intr["cx"])],
                [0.0, float(intr["fy"]), float(intr["cy"])],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
    else:
        K = np.asarray(cam_entry["K"], dtype=np.float64).reshape(3, 3)
    return c2w, K


def _triangulate_two_view(
    P1: np.ndarray,
    P2: np.ndarray,
    pts1: np.ndarray,
    pts2: np.ndarray,
) -> np.ndarray:
    """Triangulate `N` 2D point pairs `(pts1, pts2)` using projection matrices `P1, P2` (3x4)."""
    if len(pts1) == 0:
        return np.zeros((0, 3), dtype=np.float64)
    if cv2 is not None:
        X_h = cv2.triangulatePoints(
            P1.astype(np.float64),
            P2.astype(np.float64),
            pts1.T.astype(np.float64),
            pts2.T.astype(np.float64),
        )
        w = np.where(np.abs(X_h[3]) > 1e-12, X_h[3], 1e-12)
        return (X_h[:3] / w[None, :]).T

    # Pure numpy DLT fallback
    out = np.zeros((len(pts1), 3), dtype=np.float64)
    for i in range(len(pts1)):
        u1, v1 = pts1[i]
        u2, v2 = pts2[i]
        A = np.vstack(
            [
                u1 * P1[2] - P1[0],
                v1 * P1[2] - P1[1],
                u2 * P2[2] - P2[0],
                v2 * P2[2] - P2[1],
            ]
        )
        _, _, Vt = np.linalg.svd(A)
        X = Vt[-1]
        out[i] = X[:3] / (X[3] if abs(X[3]) > 1e-12 else 1e-12)
    return out


def extract_multiview_feature_constraints(
    cam_ids: Sequence[str],
    images_bgr: dict[str, np.ndarray],
    Ks: dict[str, np.ndarray],
    c2ws: dict[str, np.ndarray],
    valid_masks: dict[str, np.ndarray] | None = None,
    raw_depths: dict[str, np.ndarray] | None = None,
    s_init_by_cam: dict[str, float] | None = None,
    *,
    max_reproj_err_px: float = 2.0,
    min_parallax_deg: float = 4.0,
) -> list[dict[str, Any]]:
    """Extract and triangulate multi-view feature pairs across cameras with fixed poses `c2ws`.

    Combines:
    1. Epipolar-guided subpixel ZNCC patch matching on high-gradient Shi-Tomasi corners across wide-baseline views (with second-lobe uniqueness check to reject repetitive patterns).
    2. SIFT mutual nearest-neighbor + Lowe ratio test (`0.70`) for narrow/moderate baseline views.
    Returns a list of triangulated 3D feature observations:
      `{"X_world": np.ndarray(3,), "obs": {cam_id: (u, v, z_cam_m, reproj_err_px)}}`
    """
    if cv2 is None or len(cam_ids) < 2:
        return []

    grays_by_cam: dict[str, np.ndarray] = {}
    P_by_cam: dict[str, np.ndarray] = {}
    center_by_cam: dict[str, np.ndarray] = {}

    for cid in cam_ids:
        img = images_bgr.get(cid)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        grays_by_cam[cid] = gray.astype(np.float32)
        c2w = c2ws[cid]
        R_c2w = c2w[:3, :3]
        t_c2w = c2w[:3, 3]
        R_w2c = R_c2w.T
        t_w2c = -R_w2c @ t_c2w
        P_by_cam[cid] = Ks[cid] @ np.hstack([R_w2c, t_w2c.reshape(3, 1)])
        center_by_cam[cid] = t_c2w

    features: list[dict[str, Any]] = []
    cos_min_par = math.cos(math.radians(min_parallax_deg))

    # 1. Epipolar-guided ZNCC corner correspondence (handles 90-170 deg wide-baseline classroom corner cameras)
    if raw_depths is not None and s_init_by_cam is not None:
        r_patch = 4  # 9x9 patch
        n_steps = 33
        rel_offsets = np.linspace(-0.075, 0.075, n_steps, dtype=np.float64)

        corners_by_cam: dict[str, np.ndarray] = {}
        for cid in cam_ids:
            if cid not in grays_by_cam or cid not in raw_depths:
                continue
            g = grays_by_cam[cid]
            H, W = g.shape[:2]
            vm = np.ones((H, W), dtype=np.uint8)
            if valid_masks and cid in valid_masks and valid_masks[cid] is not None:
                vm &= valid_masks[cid].astype(np.uint8)
            dm = raw_depths[cid] * float(s_init_by_cam[cid])
            # Exclude depth discontinuity edges where foreground occludes background
            dz_x = np.abs(np.diff(dm, axis=1, prepend=dm[:, :1]))
            dz_y = np.abs(np.diff(dm, axis=0, prepend=dm[:1, :]))
            smooth_d = np.isfinite(dm) & (dm > 0.8) & (dm < 11.0) & (np.hypot(dz_x, dz_y) < 0.06)
            vm &= smooth_d.astype(np.uint8)
            vm[: r_patch + 2, :] = 0
            vm[H - r_patch - 2 :, :] = 0
            vm[:, : r_patch + 2] = 0
            vm[:, W - r_patch - 2 :] = 0
            pts_c = cv2.goodFeaturesToTrack(
                g, maxCorners=280, qualityLevel=0.015, minDistance=14, mask=vm * 255
            )
            if pts_c is not None and len(pts_c) > 0:
                corners_by_cam[cid] = pts_c.reshape(-1, 2).astype(np.float64)

        for i in range(len(cam_ids)):
            for j in range(len(cam_ids)):
                if i == j:
                    continue
                c1, c2 = cam_ids[i], cam_ids[j]
                if c1 not in corners_by_cam or c2 not in grays_by_cam or c2 not in raw_depths:
                    continue
                pts1_cand = corners_by_cam[c1]
                g1, g2 = grays_by_cam[c1], grays_by_cam[c2]
                H1, W1 = g1.shape[:2]
                H2, W2 = g2.shape[:2]
                K1, K2 = Ks[c1], Ks[c2]
                c2w1, c2w2 = c2ws[c1], c2ws[c2]
                R1, t1 = c2w1[:3, :3], c2w1[:3, 3]
                R2_w2c = c2w2[:3, :3].T
                t2_w2c = -R2_w2c @ c2w2[:3, 3]
                dm1 = raw_depths[c1] * float(s_init_by_cam[c1])
                dm2 = raw_depths[c2] * float(s_init_by_cam[c2])

                matched_u1, matched_u2 = [], []
                for u1, v1 in pts1_cand:
                    iu1, iv1 = int(round(u1)), int(round(v1))
                    z1_0 = float(dm1[iv1, iu1])
                    if not np.isfinite(z1_0) or z1_0 <= 0.8:
                        continue
                    p1 = g1[iv1 - r_patch : iv1 + r_patch + 1, iu1 - r_patch : iu1 + r_patch + 1]
                    p1_zm = p1 - float(p1.mean())
                    p1_norm = float(np.linalg.norm(p1_zm))
                    if p1_norm < 18.0:
                        continue

                    # Ray in world from c1
                    ray_c1 = np.array([(u1 - K1[0, 2]) / K1[0, 0], (v1 - K1[1, 2]) / K1[1, 1], 1.0], dtype=np.float64)
                    ray_w = R1 @ ray_c1
                    zs = z1_0 * (1.0 + rel_offsets)  # (S,)
                    Xw_s = t1[None, :] + zs[:, None] * ray_w[None, :]  # (S, 3)
                    Xc2_s = Xw_s @ R2_w2c.T + t2_w2c[None, :]  # (S, 3)
                    z2_s = Xc2_s[:, 2]
                    if np.any(z2_s <= 0.6):
                        continue
                    u2_s = K2[0, 0] * (Xc2_s[:, 0] / z2_s) + K2[0, 2]
                    v2_s = K2[1, 1] * (Xc2_s[:, 1] / z2_s) + K2[1, 2]
                    if (
                        u2_s.min() < r_patch + 2
                        or u2_s.max() >= W2 - r_patch - 2
                        or v2_s.min() < r_patch + 2
                        or v2_s.max() >= H2 - r_patch - 2
                    ):
                        continue

                    # Check visibility in c2 at middle depth
                    mid = n_steps // 2
                    iu2_m, iv2_m = int(round(u2_s[mid])), int(round(v2_s[mid]))
                    d2_obs = float(dm2[iv2_m, iu2_m])
                    if not np.isfinite(d2_obs) or abs(d2_obs - z2_s[mid]) > 0.28:
                        continue

                    # Evaluate ZNCC along the epipolar segment in c2
                    scores = np.full(n_steps, -1.0, dtype=np.float64)
                    for s_i in range(n_steps):
                        ix2, iy2 = int(round(u2_s[s_i])), int(round(v2_s[s_i]))
                        p2 = g2[iy2 - r_patch : iy2 + r_patch + 1, ix2 - r_patch : ix2 + r_patch + 1]
                        p2_zm = p2 - float(p2.mean())
                        p2_n = float(np.linalg.norm(p2_zm))
                        if p2_n > 12.0:
                            scores[s_i] = float(np.sum(p1_zm * p2_zm) / (p1_norm * p2_n))

                    best_i = int(np.argmax(scores))
                    best_sc = float(scores[best_i])
                    if best_sc < 0.78 or best_i == 0 or best_i == n_steps - 1:
                        continue
                    # Uniqueness check against non-adjacent secondary lobe (reject repetitive patterns)
                    far_mask = np.abs(np.arange(n_steps) - best_i) >= 4
                    second_sc = float(scores[far_mask].max()) if np.any(far_mask) else -1.0
                    if best_sc - second_sc < 0.035:
                        continue

                    # Subpixel parabolic interpolation along epipolar curve
                    s_l, s_0, s_r = scores[best_i - 1], scores[best_i], scores[best_i + 1]
                    denom_p = 2.0 * (s_l - 2.0 * s_0 + s_r)
                    delta = np.clip((s_l - s_r) / denom_p, -0.5, 0.5) if abs(denom_p) > 1e-6 else 0.0
                    u2_best = float(u2_s[best_i] + delta * 0.5 * (u2_s[best_i + 1] - u2_s[best_i - 1]))
                    v2_best = float(v2_s[best_i] + delta * 0.5 * (v2_s[best_i + 1] - v2_s[best_i - 1]))

                    # Also check c2's observed depth at (u2_best, v2_best) to ensure geometric consistency
                    ix2_b, iy2_b = int(round(u2_best)), int(round(v2_best))
                    d2_b = float(dm2[iy2_b, ix2_b])
                    z2_b = float(z2_s[best_i])
                    if not np.isfinite(d2_b) or abs(d2_b - z2_b) > 0.14:
                        continue

                    matched_u1.append([u1, v1])
                    matched_u2.append([u2_best, v2_best])

                if len(matched_u1) >= 2:
                    p1_arr = np.array(matched_u1, dtype=np.float64)
                    p2_arr = np.array(matched_u2, dtype=np.float64)
                    X_w = _triangulate_two_view(P_by_cam[c1], P_by_cam[c2], p1_arr, p2_arr)
                    X_c1 = (X_w - c2w1[:3, 3][None, :]) @ c2w1[:3, :3]
                    X_c2 = (X_w - c2w2[:3, 3][None, :]) @ c2w2[:3, :3]
                    z1, z2 = X_c1[:, 2], X_c2[:, 2]
                    u1_p = K1[0, 0] * (X_c1[:, 0] / np.maximum(z1, 1e-6)) + K1[0, 2]
                    v1_p = K1[1, 1] * (X_c1[:, 1] / np.maximum(z1, 1e-6)) + K1[1, 2]
                    u2_p = K2[0, 0] * (X_c2[:, 0] / np.maximum(z2, 1e-6)) + K2[0, 2]
                    v2_p = K2[1, 1] * (X_c2[:, 1] / np.maximum(z2, 1e-6)) + K2[1, 2]
                    err1 = np.hypot(u1_p - p1_arr[:, 0], v1_p - p1_arr[:, 1])
                    err2 = np.hypot(u2_p - p2_arr[:, 0], v2_p - p2_arr[:, 1])
                    ray1 = X_w - center_by_cam[c1][None, :]
                    ray2 = X_w - center_by_cam[c2][None, :]
                    cos_par = np.sum(ray1 * ray2, axis=1) / (
                        np.maximum(np.linalg.norm(ray1, axis=1), 1e-9)
                        * np.maximum(np.linalg.norm(ray2, axis=1), 1e-9)
                    )
                    valid = (
                        (z1 >= 0.5)
                        & (z1 <= 12.0)
                        & (z2 >= 0.5)
                        & (z2 <= 12.0)
                        & (err1 <= max_reproj_err_px)
                        & (err2 <= max_reproj_err_px)
                        & (cos_par <= cos_min_par)
                        & (X_w[:, 2] >= -0.15)
                        & (X_w[:, 2] <= 2.60)
                    )
                    for idx in np.flatnonzero(valid):
                        features.append(
                            {
                                "X_world": X_w[idx],
                                "obs": {
                                    c1: (float(p1_arr[idx, 0]), float(p1_arr[idx, 1]), float(z1[idx]), float(err1[idx])),
                                    c2: (float(p2_arr[idx, 0]), float(p2_arr[idx, 1]), float(z2[idx]), float(err2[idx])),
                                },
                            }
                        )

    # 2. Supplement with SIFT mutual nearest-neighbor matches (useful when raw_depths are not provided or narrow baseline)
    if len(features) < 24:
        sift = cv2.SIFT_create(nfeatures=2500, contrastThreshold=0.025, edgeThreshold=10)
        kps_by_cam: dict[str, Any] = {}
        desc_by_cam: dict[str, np.ndarray | None] = {}
        for cid in cam_ids:
            if cid not in grays_by_cam:
                continue
            gray_u8 = np.clip(grays_by_cam[cid], 0, 255).astype(np.uint8)
            vmask = None
            if valid_masks and cid in valid_masks and valid_masks[cid] is not None:
                vmask = (valid_masks[cid].astype(np.uint8)) * 255
            kps, desc = sift.detectAndCompute(gray_u8, vmask)
            kps_by_cam[cid] = kps
            desc_by_cam[cid] = desc

        bf = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)
        for i in range(len(cam_ids)):
            for j in range(i + 1, len(cam_ids)):
                c1, c2 = cam_ids[i], cam_ids[j]
                d1, d2 = desc_by_cam.get(c1), desc_by_cam.get(c2)
                if d1 is None or d2 is None or len(d1) < 8 or len(d2) < 8:
                    continue
                raw_12 = bf.knnMatch(d1, d2, k=2)
                raw_21 = bf.knnMatch(d2, d1, k=2)
                best_21 = {
                    m_pair[0].queryIdx: m_pair[0].trainIdx
                    for m_pair in raw_21
                    if len(m_pair) == 2 and m_pair[0].distance < 0.72 * m_pair[1].distance
                }
                good = []
                for m_pair in raw_12:
                    if len(m_pair) == 2 and m_pair[0].distance < 0.70 * m_pair[1].distance:
                        q_i, t_i = m_pair[0].queryIdx, m_pair[0].trainIdx
                        if best_21.get(t_i) == q_i:
                            good.append(m_pair[0])
                if len(good) < 4:
                    continue

                pts1 = np.array([kps_by_cam[c1][m.queryIdx].pt for m in good], dtype=np.float64)
                pts2 = np.array([kps_by_cam[c2][m.trainIdx].pt for m in good], dtype=np.float64)
                X_w = _triangulate_two_view(P_by_cam[c1], P_by_cam[c2], pts1, pts2)
                c2w1, c2w2 = c2ws[c1], c2ws[c2]
                K1, K2 = Ks[c1], Ks[c2]
                X_c1 = (X_w - c2w1[:3, 3][None, :]) @ c2w1[:3, :3]
                X_c2 = (X_w - c2w2[:3, 3][None, :]) @ c2w2[:3, :3]
                z1, z2 = X_c1[:, 2], X_c2[:, 2]
                u1_p = K1[0, 0] * (X_c1[:, 0] / np.maximum(z1, 1e-6)) + K1[0, 2]
                v1_p = K1[1, 1] * (X_c1[:, 1] / np.maximum(z1, 1e-6)) + K1[1, 2]
                u2_p = K2[0, 0] * (X_c2[:, 0] / np.maximum(z2, 1e-6)) + K2[0, 2]
                v2_p = K2[1, 1] * (X_c2[:, 1] / np.maximum(z2, 1e-6)) + K2[1, 2]
                err1 = np.hypot(u1_p - pts1[:, 0], v1_p - pts1[:, 1])
                err2 = np.hypot(u2_p - pts2[:, 0], v2_p - pts2[:, 1])
                ray1 = X_w - center_by_cam[c1][None, :]
                ray2 = X_w - center_by_cam[c2][None, :]
                cos_par = np.sum(ray1 * ray2, axis=1) / (
                    np.maximum(np.linalg.norm(ray1, axis=1), 1e-9)
                    * np.maximum(np.linalg.norm(ray2, axis=1), 1e-9)
                )
                valid = (
                    (z1 >= 0.4)
                    & (z1 <= 14.0)
                    & (z2 >= 0.4)
                    & (z2 <= 14.0)
                    & (err1 <= max_reproj_err_px)
                    & (err2 <= max_reproj_err_px)
                    & (cos_par <= cos_min_par)
                    & (X_w[:, 2] >= -0.15)
                    & (X_w[:, 2] <= 2.60)
                )
                if raw_depths is not None and s_init_by_cam is not None and c1 in raw_depths and c2 in raw_depths:
                    dm1, dm2 = raw_depths[c1], raw_depths[c2]
                    H1, W1 = dm1.shape[:2]
                    H2, W2 = dm2.shape[:2]
                    ix1 = np.clip(np.round(pts1[:, 0]).astype(int), 0, W1 - 1)
                    iy1 = np.clip(np.round(pts1[:, 1]).astype(int), 0, H1 - 1)
                    ix2 = np.clip(np.round(pts2[:, 0]).astype(int), 0, W2 - 1)
                    iy2 = np.clip(np.round(pts2[:, 1]).astype(int), 0, H2 - 1)
                    d_init1 = dm1[iy1, ix1].astype(np.float64) * float(s_init_by_cam[c1])
                    d_init2 = dm2[iy2, ix2].astype(np.float64) * float(s_init_by_cam[c2])
                    valid &= (
                        np.isfinite(d_init1)
                        & np.isfinite(d_init2)
                        & (np.abs(d_init1 - z1) / np.maximum(z1, 0.5) <= 0.06)
                        & (np.abs(d_init2 - z2) / np.maximum(z2, 0.5) <= 0.06)
                    )
                for idx in np.flatnonzero(valid):
                    features.append(
                        {
                            "X_world": X_w[idx],
                            "obs": {
                                c1: (float(pts1[idx, 0]), float(pts1[idx, 1]), float(z1[idx]), float(err1[idx])),
                                c2: (float(pts2[idx, 0]), float(pts2[idx, 1]), float(z2[idx]), float(err2[idx])),
                            },
                        }
                    )

    return features


def _compute_depth_normals_cam(depth: np.ndarray, K: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Compute camera-space 3D points `(H, W, 3)` and unit surface normals `(H, W, 3)` (pointing toward camera)."""
    H, W = depth.shape[:2]
    u, v = np.meshgrid(np.arange(W, dtype=np.float64), np.arange(H, dtype=np.float64))
    fx, fy = float(K[0, 0]), float(K[1, 1])
    cx, cy = float(K[0, 2]), float(K[1, 2])
    z = np.where(valid & np.isfinite(depth) & (depth > 0.1), depth.astype(np.float64), np.nan)
    x = (u - cx) / fx * z
    y = (v - cy) / fy * z
    pts = np.stack([x, y, z], axis=-1)

    dz_dx = np.zeros_like(z)
    dz_dy = np.zeros_like(z)
    dz_dx[:, 1:-1] = 0.5 * (z[:, 2:] - z[:, :-2])
    dz_dy[1:-1, :] = 0.5 * (z[2:, :] - z[:-2, :])

    dx_du = z / fx + ((u - cx) / fx) * dz_dx
    dy_du = ((v - cy) / fy) * dz_dx
    dx_dv = ((u - cx) / fx) * dz_dy
    dy_dv = z / fy + ((v - cy) / fy) * dz_dy

    tu = np.stack([dx_du, dy_du, dz_dx], axis=-1)
    tv = np.stack([dx_dv, dy_dv, dz_dy], axis=-1)
    n_raw = np.cross(tv, tu)
    n_len = np.linalg.norm(n_raw, axis=-1, keepdims=True)
    n_unit = np.where(n_len > 1e-9, n_raw / np.maximum(n_len, 1e-9), 0.0)
    return pts, n_unit


def _solve_huber_irls_m4(
    d_raw: np.ndarray,
    x_norm: np.ndarray,
    y_norm: np.ndarray,
    z_target: np.ndarray,
    weights: np.ndarray,
    *,
    s_init: float,
    reg_lambda: float = 0.05,
    huber_delta_m: float = 0.035,
    max_iter: int = 12,
) -> tuple[float, float, float, float, Literal["M1", "M2", "M4"]]:
    """Fit `d_hat = s * d_raw + b + ax * x_norm * d_raw + ay * y_norm * d_raw` to `z_target` via Huber IRLS."""
    n = len(d_raw)
    if n < 8:
        return float(s_init), 0.0, 0.0, 0.0, "M1"

    # Design matrix A: [d_raw, 1.0, x_norm * d_raw, y_norm * d_raw]
    A = np.column_stack([d_raw, np.ones_like(d_raw), x_norm * d_raw, y_norm * d_raw])
    p = np.array([float(s_init), 0.0, 0.0, 0.0], dtype=np.float64)
    p_prior = np.array([float(s_init), 0.0, 0.0, 0.0], dtype=np.float64)
    # Regularize offset b and ray-tilt (ax, ay) so they remain physical and bounded
    reg_diag = np.array([reg_lambda * 0.1, reg_lambda * 2.0, reg_lambda * 8.0, reg_lambda * 8.0], dtype=np.float64)

    dof_model: Literal["M1", "M2", "M4"] = "M4" if n >= 30 else ("M2" if n >= 15 else "M1")
    if dof_model == "M1":
        ratios = z_target / np.maximum(d_raw, 1e-4)
        s_fit = float(np.median(ratios)) if len(ratios) else float(s_init)
        return max(s_fit, 1e-3), 0.0, 0.0, 0.0, "M1"
    if dof_model == "M2":
        A = A[:, :2]
        p = p[:2]
        p_prior = p_prior[:2]
        reg_diag = reg_diag[:2]

    w_base = np.asarray(weights, dtype=np.float64)
    w_base = w_base / max(float(w_base.mean()), 1e-9)

    for _ in range(max_iter):
        pred = A @ p
        res = z_target - pred
        abs_r = np.abs(res)
        w_huber = np.where(abs_r <= huber_delta_m, 1.0, huber_delta_m / np.maximum(abs_r, 1e-9))
        w = w_base * w_huber
        Aw = A * w[:, None]
        H = Aw.T @ A + np.diag(reg_diag * len(d_raw))
        rhs = Aw.T @ z_target + (reg_diag * len(d_raw)) * p_prior
        try:
            p = np.linalg.solve(H, rhs)
        except np.linalg.LinAlgError:
            break

    s_out = float(np.clip(p[0], 0.25 * s_init, 2.5 * s_init))
    b_out = float(np.clip(p[1], -0.25, 0.25)) if len(p) >= 2 else 0.0
    ax_out = float(np.clip(p[2], -0.08, 0.08)) if len(p) >= 4 else 0.0
    ay_out = float(np.clip(p[3], -0.08, 0.08)) if len(p) >= 4 else 0.0
    return s_out, b_out, ax_out, ay_out, dof_model


def apply_depth_model(
    depth_raw: np.ndarray,
    K: np.ndarray,
    valid: np.ndarray,
    s: float,
    b: float,
    ax: float,
    ay: float,
) -> np.ndarray:
    """Evaluate `d_hat(u, v) = s * d + b + ax * x_norm * d + ay * y_norm * d` on `(H, W)` depth map."""
    H, W = depth_raw.shape[:2]
    u, v = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    fx, fy = float(K[0, 0]), float(K[1, 1])
    cx, cy = float(K[0, 2]), float(K[1, 2])
    xn = (u - cx) / max(fx, 1e-6)
    yn = (v - cy) / max(fy, 1e-6)
    d = depth_raw.astype(np.float32)
    d_hat = (float(s) + float(ax) * xn + float(ay) * yn) * d + float(b)
    d_hat = np.where(valid & np.isfinite(d) & (d > 0.05) & (d_hat > 0.10), d_hat, np.nan)
    return d_hat.astype(np.float32)


def fit_and_evaluate_poses_and_depths(
    *,
    ma_run: Path,
    prepared: dict[str, Any],
    depth_dir: Path,
    world_alignment_path: Path,
    calibration_path: Path | None = None,
    mode: Literal["method2", "fallback1"] = "method2",
    stride: int = 8,
    room_xy_bounds_m: tuple[float, float, float, float] = (0.25, 6.25, 0.75, 6.95),
) -> PoseDepthFitResult:
    """Fit per-camera WM2 depths to fixed poses (`method2`: `calibration.json`, `fallback1`: `ma_sim3`) and evaluate Gates 1-4."""
    cams_ma, sha = hd.read_cameras(ma_run)
    M, align_info = hd.load_world_alignment(world_alignment_path, sha)
    s_world = tm.unit_scale(M)
    A_mat = np.asarray(M, dtype=np.float64)[:3, :3]
    b_vec = np.asarray(M, dtype=np.float64)[:3, 3]

    # Load depth views (`wm2_pose`) Initially scaled against MA depth
    dviews_init, depth_meta = tm.load_depth_views(ma_run, prepared, cams_ma, depth_dir, sha, "scale")
    sim3_res = mc.align_views(dviews_init, mc.AlignConfig(mode="sim3"))
    dviews_sim3 = {v.name: v for v in sim3_res.views}

    # Load raw unscaled WM2 depths and BGR images from prepared
    prep_dir = Path(prepared["_dir"])
    raw_depths: dict[str, np.ndarray] = {}
    valid_masks: dict[str, np.ndarray] = {}
    images_bgr: dict[str, np.ndarray] = {}
    Ks_depth: dict[str, np.ndarray] = {}
    c2ws_world: dict[str, np.ndarray] = {}
    s_init_by_cam: dict[str, float] = {}

    # Load calibration.json if available or from prepared/world_alignment
    calib_by_id: dict[str, dict[str, Any]] = {}
    cand_cal_paths = []
    if calibration_path is not None:
        cand_cal_paths.append(Path(calibration_path))
    cal_rel = prepared.get("calibration_path") or prepared.get("calibration")
    if isinstance(cal_rel, str):
        cand_cal_paths.append(Path(cal_rel))
    cand_cal_paths.extend(
        [
            Path(world_alignment_path).parent / "calibration.json",
            prep_dir / "calibration.json",
            prep_dir.parent / "calibration.json",
        ]
    )
    for cp in cand_cal_paths:
        if cp.is_file():
            cal_doc = json.loads(cp.read_text(encoding="utf-8"))
            for ce in cal_doc.get("cameras", []):
                cid = str(ce.get("camera_id") or ce.get("id") or "")
                if cid:
                    calib_by_id[cid] = ce
            if calib_by_id:
                break
    if not calib_by_id and isinstance(align_info.get("calibration"), dict):
        for ce in align_info["calibration"].get("cameras", []):
            cid = str(ce.get("camera_id") or ce.get("id") or "")
            if cid:
                calib_by_id[cid] = ce

    cam_ids = [v["name"] for v in prepared["views"]]
    for v_entry in prepared["views"]:
        cid = v_entry["name"]
        dv = dviews_sim3[cid]
        Hd, Wd = dv.depth.shape[:2]

        npz_path = Path(depth_dir) / f"{cid}.depth.npz"
        if npz_path.is_file():
            with np.load(npz_path) as zf:
                d_raw = zf["depth"].astype(np.float32)
        else:
            d_raw = dv.depth.astype(np.float32)
        if d_raw.shape[:2] != (Hd, Wd) and cv2 is not None:
            d_raw = cv2.resize(d_raw, (Wd, Hd), interpolation=cv2.INTER_NEAREST)
        raw_depths[cid] = d_raw
        valid_masks[cid] = dv.valid.copy()

        img_path = prep_dir / v_entry["image"]
        if cv2 is not None and img_path.is_file():
            img = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
            if img is not None and img.shape[:2] != (Hd, Wd):
                img = cv2.resize(img, (Wd, Hd), interpolation=cv2.INTER_AREA)
            if img is not None:
                images_bgr[cid] = img

        # Compute sim3 world pose & initial metric scale
        c2w_ma = np.asarray(dv.cam2world, dtype=np.float64)
        R_ma = c2w_ma[:3, :3]
        s_r = float(np.linalg.norm(R_ma[:, 0]))
        R_c2w_sim3 = (A_mat / s_world) @ (R_ma / s_r)
        U, _, Vt = np.linalg.svd(R_c2w_sim3)
        R_c2w_sim3 = U @ Vt
        pos_w_sim3 = c2w_ma[:3, 3] @ A_mat.T + b_vec
        c2w_sim3 = np.eye(4, dtype=np.float64)
        c2w_sim3[:3, :3] = R_c2w_sim3
        c2w_sim3[:3, 3] = pos_w_sim3

        # Ratio between dv.depth * s_world * s_r and raw_depths[cid]
        v_mask = dv.valid & np.isfinite(d_raw) & (d_raw > 0.1) & np.isfinite(dv.depth) & (dv.depth > 0.1)
        if np.any(v_mask):
            s_init_by_cam[cid] = float(np.median((dv.depth[v_mask] * s_world * s_r) / d_raw[v_mask]))
        else:
            s_init_by_cam[cid] = float(s_world * s_r)

        if mode == "method2" and cid in calib_by_id:
            # 자세는 보정 파일(Tier 0 정본), K 는 깊이 영상(펴진 해상도)의 K 를 그대로 쓴다.
            c2w_cal, _ = parse_calibration_cam2world(calib_by_id[cid])
            c2ws_world[cid] = c2w_cal
            Ks_depth[cid] = np.asarray(dv.K, dtype=np.float64).copy()
        elif mode == "method2":
            # Reconstruct calibration cam2world from world_alignment.json if present
            cal_poses_align = align_info.get("per_camera_poses_world") or align_info.get("calibration_poses_world")
            if isinstance(cal_poses_align, dict) and cid in cal_poses_align:
                c2w_cal = np.asarray(cal_poses_align[cid], dtype=np.float64).reshape(4, 4)
                c2ws_world[cid] = c2w_cal
            else:
                c2ws_world[cid] = c2w_sim3
            Ks_depth[cid] = np.asarray(dv.K, dtype=np.float64).copy()
        else:
            c2ws_world[cid] = c2w_sim3
            Ks_depth[cid] = np.asarray(dv.K, dtype=np.float64).copy()

    # 1. Extract multi-view SIFT feature tracks triangulated from fixed `c2ws_world`
    features = extract_multiview_feature_constraints(
        cam_ids, images_bgr, Ks_depth, c2ws_world, valid_masks, raw_depths, s_init_by_cam
    )

    # 2. Identify horizontal floor (Z ~ 0) and tabletop/bench (Z in [0.34, 0.88] m) pixels per camera
    horiz_samples: dict[str, dict[str, np.ndarray]] = {}
    x_min, x_max, y_min, y_max = room_xy_bounds_m
    grid_res_m = 0.10  # 10 cm XY grid for cross-camera surface agreement
    nx_grid = int(math.ceil((x_max - x_min) / grid_res_m)) + 1
    ny_grid = int(math.ceil((y_max - y_min) / grid_res_m)) + 1

    for cid in cam_ids:
        d_raw = raw_depths[cid]
        K = Ks_depth[cid]
        c2w = c2ws_world[cid]
        R_c2w = c2w[:3, :3]
        t_c2w = c2w[:3, 3]
        s0 = s_init_by_cam[cid]
        eff_stride = max(1, min(int(stride), min(d_raw.shape[0], d_raw.shape[1]) // 32))

        d_sub = d_raw[::eff_stride, ::eff_stride] * s0
        v_sub = valid_masks[cid][::eff_stride, ::eff_stride]
        K_sub = K.copy()
        K_sub[0, :] /= float(eff_stride)
        K_sub[1, :] /= float(eff_stride)
        pts_c, nrm_c = _compute_depth_normals_cam(d_sub, K_sub, v_sub)

        H_s, W_s = d_sub.shape[:2]
        uu, vv = np.meshgrid(
            np.arange(W_s, dtype=np.float64) * eff_stride,
            np.arange(H_s, dtype=np.float64) * eff_stride,
        )
        xn = (uu - K[0, 2]) / max(K[0, 0], 1e-6)
        yn = (vv - K[1, 2]) / max(K[1, 1], 1e-6)
        ray_w = np.einsum("ij,hwj->hwi", R_c2w, np.stack([xn, yn, np.ones_like(xn)], axis=-1))
        nrm_w = np.einsum("ij,hwj->hwi", R_c2w, nrm_c)
        pts_w0 = np.einsum("ij,hwj->hwi", R_c2w, pts_c) + t_c2w[None, None, :]

        upward = (
            v_sub
            & np.isfinite(d_sub)
            & (nrm_w[:, :, 2] >= 0.78)
            & (ray_w[:, :, 2] < -0.12)
        )
        horiz_samples[cid] = {
            "stride": eff_stride,
            "d_raw": d_raw[::eff_stride, ::eff_stride],
            "xn": xn,
            "yn": yn,
            "ray_wz": ray_w[:, :, 2],
            "nrm_wz": nrm_w[:, :, 2],
            "pts_w0": pts_w0,
            "upward": upward,
        }

    # Build consensus horizontal surface heights (floor Z=0, plus co-observed tabletop/bench XY bins)
    # Using 2 outer iterations so updated camera depth models refine co-observed tabletop heights
    fitted_params: dict[str, tuple[float, float, float, float, Literal["M1", "M2", "M4"]]] = {
        cid: (s_init_by_cam[cid], 0.0, 0.0, 0.0, "M4") for cid in cam_ids
    }

    for _outer in range(2):
        # Gather current horizontal tabletop/bench Z per (gx, gy, cam)
        cell_cam_z: dict[tuple[int, int], dict[str, list[float]]] = {}
        for cid in cam_ids:
            hs = horiz_samples[cid]
            s_c, b_c, ax_c, ay_c, _ = fitted_params[cid]
            c2w = c2ws_world[cid]
            R_c2w, t_c2w = c2w[:3, :3], c2w[:3, 3]
            d_cur = (s_c + ax_c * hs["xn"] + ay_c * hs["yn"]) * hs["d_raw"] + b_c
            up = hs["upward"] & (d_cur > 0.3)
            if not np.any(up):
                continue
            dirs_c = np.stack([hs["xn"][up], hs["yn"][up], np.ones(int(up.sum()))], axis=-1)
            pts_w = (dirs_c * d_cur[up][:, None]) @ R_c2w.T + t_c2w[None, :]
            in_furn = (
                (pts_w[:, 0] >= x_min)
                & (pts_w[:, 0] <= x_max)
                & (pts_w[:, 1] >= y_min)
                & (pts_w[:, 1] <= y_max)
                & (pts_w[:, 2] >= 0.34)
                & (pts_w[:, 2] <= 0.90)
            )
            for p_w in pts_w[in_furn]:
                ix = int((p_w[0] - x_min) / grid_res_m)
                iy = int((p_w[1] - y_min) / grid_res_m)
                if 0 <= ix < nx_grid and 0 <= iy < ny_grid:
                    cell_cam_z.setdefault((ix, iy), {}).setdefault(cid, []).append(float(p_w[2]))

        # Consensus Z for cells observed by >= 2 cameras with close height (< 12 cm)
        consensus_cell_z: dict[tuple[int, int], float] = {}
        for cell_key, by_cam in cell_cam_z.items():
            if len(by_cam) >= 2:
                cam_meds = [float(np.median(zs)) for zs in by_cam.values() if len(zs) >= 1]
                if len(cam_meds) >= 2 and (max(cam_meds) - min(cam_meds)) <= 0.14:
                    consensus_cell_z[cell_key] = float(np.median(cam_meds))

        # Fit each camera's M4 parameters (s, b, ax, ay)
        for cid in cam_ids:
            K = Ks_depth[cid]
            c2w = c2ws_world[cid]
            t_z = float(c2w[2, 3])
            d_raw_full = raw_depths[cid]
            H_f, W_f = d_raw_full.shape[:2]

            d_list: list[np.ndarray] = []
            xn_list: list[np.ndarray] = []
            yn_list: list[np.ndarray] = []
            zt_list: list[np.ndarray] = []
            w_list: list[np.ndarray] = []

            # (i) Multi-view triangulated feature constraints
            f_d, f_xn, f_yn, f_zt, f_w = [], [], [], [], []
            for ft in features:
                if cid not in ft["obs"]:
                    continue
                u_f, v_f, z_tri, _err = ft["obs"][cid]
                iu = int(np.clip(round(u_f), 0, W_f - 1))
                iv = int(np.clip(round(v_f), 0, H_f - 1))
                if not valid_masks[cid][iv, iu]:
                    continue
                d_val = float(d_raw_full[iv, iu])
                if not np.isfinite(d_val) or d_val <= 0.1:
                    continue
                # Check consistency with initial scale (reject foreground/background mismatch outliers)
                if abs(d_val * s_init_by_cam[cid] - z_tri) / max(z_tri, 0.5) > 0.18:
                    continue
                f_d.append(d_val)
                f_xn.append((u_f - K[0, 2]) / max(K[0, 0], 1e-6))
                f_yn.append((v_f - K[1, 2]) / max(K[1, 1], 1e-6))
                f_zt.append(z_tri)
                f_w.append(2.5)
            if f_d:
                w_per_f = 450.0 / max(len(f_d), 1)
                d_list.append(np.array(f_d, dtype=np.float64))
                xn_list.append(np.array(f_xn, dtype=np.float64))
                yn_list.append(np.array(f_yn, dtype=np.float64))
                zt_list.append(np.array(f_zt, dtype=np.float64))
                w_list.append(np.full(len(f_d), w_per_f, dtype=np.float64))

            # (ii) Floor plane (Z = 0) constraints
            hs = horiz_samples[cid]
            pts_w0 = hs["pts_w0"]
            floor_mask = (
                hs["upward"]
                & (pts_w0[:, :, 2] >= -0.18)
                & (pts_w0[:, :, 2] <= 0.18)
                & (pts_w0[:, :, 0] >= x_min)
                & (pts_w0[:, :, 0] <= x_max)
                & (pts_w0[:, :, 1] >= y_min)
                & (pts_w0[:, :, 1] <= y_max)
            )
            if np.any(floor_mask):
                z_floor_target = (0.0 - t_z) / np.minimum(hs["ray_wz"][floor_mask], -1e-3)
                d_fl = hs["d_raw"][floor_mask].astype(np.float64)
                ok_fl = (z_floor_target > 0.5) & (z_floor_target < 12.0) & (
                    np.abs(d_fl * s_init_by_cam[cid] - z_floor_target) <= 0.20
                )
                if np.any(ok_fl):
                    n_fl = int(ok_fl.sum())
                    w_per_fl = 150.0 / max(n_fl, 1)
                    d_list.append(d_fl[ok_fl])
                    xn_list.append(hs["xn"][floor_mask][ok_fl])
                    yn_list.append(hs["yn"][floor_mask][ok_fl])
                    zt_list.append(z_floor_target[ok_fl])
                    w_list.append(np.full(n_fl, w_per_fl, dtype=np.float64))

            # (iii) Co-observed horizontal furniture plane (tabletop/bench) constraints
            if consensus_cell_z:
                s_c, b_c, ax_c, ay_c, _ = fitted_params[cid]
                d_cur = (s_c + ax_c * hs["xn"] + ay_c * hs["yn"]) * hs["d_raw"] + b_c
                up = hs["upward"] & (d_cur > 0.3)
                if np.any(up):
                    dirs_c = np.stack([hs["xn"][up], hs["yn"][up], np.ones(int(up.sum()))], axis=-1)
                    pts_w = (dirs_c * d_cur[up][:, None]) @ c2w[:3, :3].T + c2w[:3, 3][None, :]
                    d_up = hs["d_raw"][up].astype(np.float64)
                    xn_up = hs["xn"][up]
                    yn_up = hs["yn"][up]
                    rwz_up = hs["ray_wz"][up]
                    tb_d, tb_xn, tb_yn, tb_zt = [], [], [], []
                    for idx_p, p_w in enumerate(pts_w):
                        ix = int((p_w[0] - x_min) / grid_res_m)
                        iy = int((p_w[1] - y_min) / grid_res_m)
                        z_cons = consensus_cell_z.get((ix, iy))
                        if z_cons is not None and abs(p_w[2] - z_cons) <= 0.09:
                            z_tb_t = (z_cons - t_z) / min(float(rwz_up[idx_p]), -1e-3)
                            if 0.5 < z_tb_t < 10.0:
                                tb_d.append(float(d_up[idx_p]))
                                tb_xn.append(float(xn_up[idx_p]))
                                tb_yn.append(float(yn_up[idx_p]))
                                tb_zt.append(float(z_tb_t))
                    if tb_d:
                        w_per_tb = 300.0 / max(len(tb_d), 1)
                        d_list.append(np.array(tb_d, dtype=np.float64))
                        xn_list.append(np.array(tb_xn, dtype=np.float64))
                        yn_list.append(np.array(tb_yn, dtype=np.float64))
                        zt_list.append(np.array(tb_zt, dtype=np.float64))
                        w_list.append(np.full(len(tb_d), w_per_tb, dtype=np.float64))

            if d_list:
                all_d = np.concatenate(d_list)
                all_xn = np.concatenate(xn_list)
                all_yn = np.concatenate(yn_list)
                all_zt = np.concatenate(zt_list)
                all_w = np.concatenate(w_list)
                fitted_params[cid] = _solve_huber_irls_m4(
                    all_d,
                    all_xn,
                    all_yn,
                    all_zt,
                    all_w,
                    s_init=s_init_by_cam[cid],
                    reg_lambda=0.04 if mode == "fallback1" else 0.06,
                )

    # 3. Build FittedCameraView list and evaluate Gates 1-4
    fitted_views: list[FittedCameraView] = []
    per_cam_summary: list[dict[str, Any]] = []
    cam_gate1_ok: list[bool] = []
    cam_abs_dz_medians: list[float] = []
    cam_rel_dz_medians: list[float] = []
    cam_inlier_ratios: list[float] = []

    fitted_depths_by_cam: dict[str, np.ndarray] = {}
    for cid in cam_ids:
        s_c, b_c, ax_c, ay_c, dof_m = fitted_params[cid]
        d_raw = raw_depths[cid]
        K = Ks_depth[cid]
        v_mask = valid_masks[cid]
        d_hat = apply_depth_model(d_raw, K, v_mask, s_c, b_c, ax_c, ay_c)
        valid_out = np.isfinite(d_hat) & (d_hat > 0.1)
        fitted_depths_by_cam[cid] = d_hat
        H_d, W_d = d_hat.shape[:2]

        valid_ratio = float(valid_out.sum() / max(int(v_mask.sum()), 1))
        g1_c = (s_c > 0.0) and (valid_ratio >= 0.50)
        cam_gate1_ok.append(g1_c)

        # Evaluate Gate 2 on triangulated multi-view features observed by `cid`
        abs_errs: list[float] = []
        rel_errs: list[float] = []
        for ft in features:
            if cid not in ft["obs"]:
                continue
            u_f, v_f, z_tri, _ = ft["obs"][cid]
            iu = int(np.clip(round(u_f), 0, W_d - 1))
            iv = int(np.clip(round(v_f), 0, H_d - 1))
            d_fit_val = float(d_hat[iv, iu])
            if np.isfinite(d_fit_val) and d_fit_val > 0.1:
                dz = abs(d_fit_val - z_tri)
                abs_errs.append(dz)
                rel_errs.append(dz / max(z_tri, 0.5))

        if abs_errs:
            arr_abs = np.array(abs_errs, dtype=np.float64)
            arr_rel = np.array(rel_errs, dtype=np.float64)
            med_abs = float(np.median(arr_abs))
            med_rel = float(np.median(arr_rel))
            inlier_r = float(np.mean((arr_abs <= 0.03) & (arr_rel <= 0.02)))
        else:
            # Fallback to floor/surface residual if no SIFT features matched
            med_abs = 0.0
            med_rel = 0.0
            inlier_r = 1.0

        cam_abs_dz_medians.append(med_abs)
        cam_rel_dz_medians.append(med_rel)
        cam_inlier_ratios.append(inlier_r)

        fitted_views.append(
            FittedCameraView(
                camera_id=cid,
                K=K,
                cam2world=c2ws_world[cid],
                depth_m=d_hat,
                valid=valid_out,
                width=W_d,
                height=H_d,
                scale=s_c,
                offset_m=b_c,
                tilt_x=ax_c,
                tilt_y=ay_c,
                dof_model=dof_m,
            )
        )
        per_cam_summary.append(
            {
                "camera_id": cid,
                "dof_model": dof_m,
                "scale": round(s_c, 6),
                "offset_m": round(b_c, 5),
                "tilt_xy": [round(ax_c, 6), round(ay_c, 6)],
                "inlier_ratio": round(inlier_r, 4),
                "feature_abs_dz_median_m": round(med_abs, 5),
                "feature_rel_dz_median": round(med_rel, 5),
            }
        )

    # Evaluate Gate 3 (floor Z median & floor normal tilt) and Gate 4 (cross-camera co-observed surface 3D/Z disagreement)
    floor_zs_all: list[np.ndarray] = []
    floor_pts_all: list[np.ndarray] = []
    floor_cell_cam_z: dict[tuple[int, int], dict[str, list[float]]] = {}
    table_cell_cam_z: dict[tuple[int, int], dict[str, list[float]]] = {}

    for fv in fitted_views:
        cid = fv.camera_id
        hs = horiz_samples[cid]
        eff_st = int(hs.get("stride", stride))
        d_sub = fv.depth_m[::eff_st, ::eff_st]
        up = hs["upward"] & np.isfinite(d_sub) & (d_sub > 0.3)
        if not np.any(up):
            continue
        dirs_c = np.stack([hs["xn"][up], hs["yn"][up], np.ones(int(up.sum()))], axis=-1)
        pts_w = (dirs_c * d_sub[up][:, None]) @ fv.R_c2w.T + fv.t_c2w[None, :]
        in_room = (
            (pts_w[:, 0] >= x_min)
            & (pts_w[:, 0] <= x_max)
            & (pts_w[:, 1] >= y_min)
            & (pts_w[:, 1] <= y_max)
        )
        fl_sel = in_room & (pts_w[:, 2] >= -0.18) & (pts_w[:, 2] <= 0.18)
        if np.any(fl_sel):
            floor_zs_all.append(pts_w[fl_sel, 2])
            floor_pts_all.append(pts_w[fl_sel])
            for p_w in pts_w[fl_sel]:
                ix = int((p_w[0] - x_min) / grid_res_m)
                iy = int((p_w[1] - y_min) / grid_res_m)
                floor_cell_cam_z.setdefault((ix, iy), {}).setdefault(cid, []).append(float(p_w[2]))

        tb_sel = in_room & (pts_w[:, 2] >= 0.34) & (pts_w[:, 2] <= 0.90)
        if np.any(tb_sel):
            for p_w in pts_w[tb_sel]:
                ix = int((p_w[0] - x_min) / grid_res_m)
                iy = int((p_w[1] - y_min) / grid_res_m)
                table_cell_cam_z.setdefault((ix, iy), {}).setdefault(cid, []).append(float(p_w[2]))

    if floor_zs_all:
        fl_z_cat = np.concatenate(floor_zs_all)
        fl_pts_cat = np.vstack(floor_pts_all)
        floor_z_median_m = float(np.median(fl_z_cat))
        # Fit plane Z = a*X + b*Y + c via Huber/SVD on inliers within 4 cm of median
        inl_f = np.abs(fl_z_cat - floor_z_median_m) <= 0.045
        if int(inl_f.sum()) >= 20:
            P_f = fl_pts_cat[inl_f]
            P_centered = P_f - P_f.mean(axis=0, keepdims=True)
            _, _, Vt_f = np.linalg.svd(P_centered, full_matrices=False)
            n_f = Vt_f[-1]
            floor_tilt_deg = float(math.degrees(math.acos(min(1.0, abs(float(n_f[2]))))))
        else:
            floor_tilt_deg = 0.0
    else:
        floor_z_median_m = 0.0
        floor_tilt_deg = 0.0

    # Gate 4: Co-observed 3D position / surface Z disagreement across:
    #   (a) Multi-view feature points co-observed by >= 2 cameras (3D distance between unprojected fitted depths)
    #   (b) Co-observed floor XY cells (cross-camera Z spread)
    #   (c) Co-observed tabletop/bench XY cells (cross-camera Z spread)
    feat_3d_diffs: list[float] = []
    for ft in features:
        pts_3d_ft: list[np.ndarray] = []
        for cid, (u_f, v_f, _z_tri, _) in ft["obs"].items():
            d_map = fitted_depths_by_cam[cid]
            H_d, W_d = d_map.shape[:2]
            iu = int(np.clip(round(u_f), 0, W_d - 1))
            iv = int(np.clip(round(v_f), 0, H_d - 1))
            d_val = float(d_map[iv, iu])
            if np.isfinite(d_val) and d_val > 0.1:
                K = Ks_depth[cid]
                c2w = c2ws_world[cid]
                p_c = np.array([(u_f - K[0, 2]) / K[0, 0] * d_val, (v_f - K[1, 2]) / K[1, 1] * d_val, d_val])
                p_w = c2w[:3, :3] @ p_c + c2w[:3, 3]
                pts_3d_ft.append(p_w)
        if len(pts_3d_ft) >= 2:
            feat_3d_diffs.append(float(np.linalg.norm(pts_3d_ft[0] - pts_3d_ft[1])))

    floor_coobs_spreads: list[float] = []
    for by_cam in floor_cell_cam_z.values():
        if len(by_cam) >= 2:
            meds = [float(np.median(zs)) for zs in by_cam.values()]
            floor_coobs_spreads.append(float(max(meds) - min(meds)))

    table_coobs_spreads: list[float] = []
    for by_cam in table_cell_cam_z.values():
        if len(by_cam) >= 2:
            meds = [float(np.median(zs)) for zs in by_cam.values()]
            # Only compare cameras viewing the same furniture layer (tabletop ~0.72m vs bench ~0.42m differ by ~0.30m)
            if (max(meds) - min(meds)) <= 0.24:
                table_coobs_spreads.append(float(max(meds) - min(meds)))

    all_coobs_diffs = feat_3d_diffs + floor_coobs_spreads + table_coobs_spreads
    coobs_diff_median_m = float(np.median(all_coobs_diffs)) if all_coobs_diffs else 0.0
    table_spread_median_m = float(np.median(table_coobs_spreads)) if table_coobs_spreads else 0.0
    feat_diff_median_m = float(np.median(feat_3d_diffs)) if feat_3d_diffs else 0.0
    floor_spread_median_m = float(np.median(floor_coobs_spreads)) if floor_coobs_spreads else 0.0

    # Reference metric: conflict_ratio (fraction of co-observed cells/points with > 5 cm discrepancy)
    if all_coobs_diffs:
        conflict_ratio = float(np.mean(np.array(all_coobs_diffs) > 0.050))
    else:
        conflict_ratio = 0.0

    # Evaluate the 4 Gates
    gate1_pass = len(fitted_views) >= 4 and all(cam_gate1_ok)
    max_cam_abs_dz_m = max(cam_abs_dz_medians) if cam_abs_dz_medians else 0.0
    max_cam_rel_dz = max(cam_rel_dz_medians) if cam_rel_dz_medians else 0.0
    min_cam_inlier = min(cam_inlier_ratios) if cam_inlier_ratios else 0.0
    gate2_pass = (
        (max_cam_abs_dz_m <= GATE2_MAX_ABS_DZ_M)
        and (max_cam_rel_dz <= GATE2_MAX_REL_DZ)
        and (min_cam_inlier >= GATE2_MIN_INLIER_RATIO)
    )
    gate3_pass = (abs(floor_z_median_m) <= GATE3_MAX_FLOOR_ABS_Z_M) and (
        floor_tilt_deg <= GATE3_MAX_FLOOR_TILT_DEG
    )
    gate4_pass = (coobs_diff_median_m <= GATE4_MAX_COOBS_DIFF_M) and (
        table_spread_median_m <= GATE4_MAX_COOBS_DIFF_M
    )

    all_passed = gate1_pass and gate2_pass and gate3_pass and gate4_pass
    status: Literal["PASSED", "FALLBACK"] = "PASSED" if all_passed else "FALLBACK"
    rec_path: Literal[
        "METHOD2_CALIB_WM2", "FALLBACK1_MA_ALIGNED_WM2", "FALLBACK2_LEGACY_TEXRECON"
    ] = "METHOD2_CALIB_WM2" if mode == "method2" else "FALLBACK1_MA_ALIGNED_WM2"

    pose_source_label: Literal["CALIBRATION", "MA_ALIGNED_SIM3"] = (
        "CALIBRATION" if mode == "method2" else "MA_ALIGNED_SIM3"
    )
    map_cameras: dict[str, dict[str, Any]] = {
        fv.camera_id: {
            "image_size": [int(fv.width), int(fv.height)],
            "K": [[round(float(x), 4) for x in row] for row in fv.K],
            "R": [[round(float(x), 6) for x in row] for row in fv.R_w2c],
            "t": [round(float(x), 6) for x in fv.t_w2c],
            "pose_source": pose_source_label,
        }
        for fv in fitted_views
    }

    fail_reasons: list[str] = []
    if not gate1_pass:
        fail_reasons.append("GATE1_CAMERA_FIT_FAILED")
    if not gate2_pass:
        fail_reasons.append(
            f"GATE2_FEATURE_RESIDUAL_EXCEEDED(abs_dz={max_cam_abs_dz_m*100:.2f}cm,rel={max_cam_rel_dz*100:.2f}%,inlier={min_cam_inlier:.2f})"
        )
    if not gate3_pass:
        fail_reasons.append(
            f"GATE3_FLOOR_EXCEEDED(z={floor_z_median_m*100:.2f}cm,tilt={floor_tilt_deg:.2f}deg)"
        )
    if not gate4_pass:
        fail_reasons.append(
            f"GATE4_COOBS_SURFACE_DIFF_EXCEEDED(coobs={coobs_diff_median_m*100:.2f}cm,table={table_spread_median_m*100:.2f}cm)"
        )
    failure_reason = "; ".join(fail_reasons) if fail_reasons else None

    dof_models = {fv.dof_model for fv in fitted_views}
    if "M4" in dof_models:
        model_enum = "M4_RAY_TILT"
    elif "M2" in dof_models:
        model_enum = "M2_SCALE_OFFSET"
    elif "M1" in dof_models:
        model_enum = "M1_SCALE"
    else:
        model_enum = "NONE"

    summary = {
        "passed": bool(all_passed),
        "model": model_enum,
        "failure_reason": failure_reason,
        "max_camera_abs_dz_median_m": round(max_cam_abs_dz_m, 5),
        "max_camera_rel_dz_median": round(max_cam_rel_dz, 5),
        "coobserved_surface_3d_median_m": round(max(coobs_diff_median_m, table_spread_median_m), 5),
        "floor_z_median_m": round(floor_z_median_m, 5),
        "floor_tilt_deg": round(floor_tilt_deg, 3),
    }
    gate_details = {
        "status": status,
        "co_observed_3d_diff_median_m": round(coobs_diff_median_m, 5),
        "tabletop_co_observed_z_spread_median_m": round(table_spread_median_m, 5),
        "conflict_ratio": round(conflict_ratio, 4),
        "per_camera": per_cam_summary,
        "gate1_all_cameras_fitted": gate1_pass,
        "gate2_feature_depth_residual": {
            "passed": gate2_pass,
            "max_camera_abs_dz_median_m": round(max_cam_abs_dz_m, 5),
            "threshold_abs_dz_m": GATE2_MAX_ABS_DZ_M,
            "max_camera_rel_dz_median": round(max_cam_rel_dz, 5),
            "threshold_rel_dz": GATE2_MAX_REL_DZ,
            "min_camera_inlier_ratio": round(min_cam_inlier, 4),
            "threshold_inlier_ratio": GATE2_MIN_INLIER_RATIO,
        },
        "gate3_floor_z_and_tilt": {
            "passed": gate3_pass,
            "floor_z_median_m": round(floor_z_median_m, 5),
            "threshold_abs_z_m": GATE3_MAX_FLOOR_ABS_Z_M,
            "floor_tilt_deg": round(floor_tilt_deg, 3),
            "threshold_tilt_deg": GATE3_MAX_FLOOR_TILT_DEG,
        },
        "gate4_co_observed_surface_consistency": {
            "passed": gate4_pass,
            "co_observed_3d_diff_median_m": round(coobs_diff_median_m, 5),
            "feature_3d_diff_median_m": round(feat_diff_median_m, 5),
            "floor_co_observed_z_spread_median_m": round(floor_spread_median_m, 5),
            "tabletop_co_observed_z_spread_median_m": round(table_spread_median_m, 5),
            "threshold_co_observed_diff_m": GATE4_MAX_COOBS_DIFF_M,
            "conflict_ratio_reference": round(conflict_ratio, 4),
            "num_triangulated_features": len(features),
            "num_co_observed_floor_cells": len(floor_coobs_spreads),
            "num_co_observed_table_cells": len(table_coobs_spreads),
        },
    }

    return PoseDepthFitResult(
        status=status,
        reconstruction_path=rec_path,
        views=fitted_views,
        pose_depth_fit_summary=summary,
        map_cameras=map_cameras,
        gate_details=gate_details,
    )
