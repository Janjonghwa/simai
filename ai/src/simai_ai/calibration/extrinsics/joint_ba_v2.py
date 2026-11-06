"""V2 Stage A rotation-only Joint Bundle Adjustment for multi-camera site setup.

Promoted from ``ai/experiments/calib_v2_joint_ba`` (S15P31A101-449 -> S15P31A101-465).
Refines camera rotations ``R`` on top of fixed camera centers ``C`` in the
MapAnything + floor/wall auto-defined World frame (+Z up, floor Z=0, meters),
using variable-projected 3D background feature tracks (``ProjectedProblem`` with
analytic implicit stationarity Jacobians) and spatial holdout validation.
Stage B/C camera center movement is intentionally excluded because multi-view
background features without floor/wall/height constraints drift camera Z by ~28 cm.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import cv2
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix
from scipy.spatial.transform import Rotation

from simai_ai.calibration.document import content_hash
from simai_ai.calibration.world_frame.scale_props import (
    TriangulatedCone,
    build_object_cross_checks_from_cone,
    detect_and_triangulate_cone_from_images,
    estimate_joint_scale_and_floor,
)
from simai_ai.geometry.rig import RigCamera
from simai_ai.geometry.triangulation import triangulate_normalized_many
from simai_ai.providers.contract import schema_errors

CAMS: tuple[str, ...] = ("cam_A", "cam_B", "cam_C", "cam_D")
PAIRS: tuple[tuple[int, int], ...] = ((0, 1), (1, 2), (2, 3), (3, 0))

DEFAULT_STAGE_A_CONFIG: dict[str, Any] = {
    "grid": [4, 4],
    "holdout_fraction": 0.2,
    "seed": 449,
    "essential_threshold_px": 1.0,
    "min_angle_deg": 2.0,
    "min_train_per_pair": 8,
    "min_holdout_per_pair": 3,
    "huber_px": 2.0,
    "rotation_sigma_deg": 5.0,
    "height_sigma_m": 0.01,
    "center_sigma_B_m": 0.02,
    "center_sigma_C_m": 0.1,
    "max_nfev": 4000,
    "svd_relative_threshold": 1e-6,
    "confirmed_floor_tracks": [],
    "x_scale_rotation_rad": 0.005,
    "x_scale_center_m": 0.01,
    "x_scale_point_m": 0.05,
    "projected_point_max_iterations": 100,
    "projected_point_gradient_tol": 1e-4,
    "projected_point_step_tol": 1e-12,
    "projected_point_fallback_nfev": 200,
    "stage_target_seconds": 10.0,
    "background_windows": 2,
    "multiwindow_merge_radius_px": 2.0,
    "holdout_worsening_tolerance_px": 1.0,
}


class StageAError(RuntimeError):
    """Structured failure raised during Stage A rotation BA or verification."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class ObjectScaleCrossChecker(Protocol):
    """Extension hook for S15P31A101-462 (traffic cone / opening sign cross-check)."""

    def __call__(
        self,
        calibration_doc: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class StageAResult:
    """Output of Stage A rotation-only Joint BA."""

    calibration_doc: dict[str, Any]
    calibration_version: str
    anchor_camera_id: str
    stage_seconds: float
    converged: bool
    valid_points: int
    before_holdout: dict[str, Any]
    after_holdout: dict[str, Any]
    pair_metrics: dict[str, Any]
    pose_changes: dict[str, Any]
    height_checks: dict[str, dict[str, Any]]
    object_cross_checks: list[dict[str, Any]]
    solver_info: dict[str, Any]
    summary_metrics: dict[str, Any]
    scale_source: str | None = None
    scale_estimation: dict[str, Any] | None = None
    cone_observation: TriangulatedCone | None = None


# ---------------------------------------------------------------------------
# Pure control policy, spatial holdout split, quarantine, and robust loss
# ---------------------------------------------------------------------------


def spatial_split(
    uv: Sequence[Sequence[float]],
    size: Sequence[int],
    grid: Sequence[int] = (4, 4),
    fraction: float = 0.2,
    seed: int = 449,
) -> tuple[list[int], list[int]]:
    """Exact rounded quota, allocated proportionally across occupied source cells."""
    if not 0 < fraction < 1 or min(size) <= 0 or min(grid) <= 0:
        raise ValueError("Invalid split parameters")
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (x, y) in enumerate(uv):
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("Nonfinite pixel")
        cell = (
            min(grid[0] - 1, max(0, int(x * grid[0] / size[0]))),
            min(grid[1] - 1, max(0, int(y * grid[1] / size[1]))),
        )
        cells[cell].append(i)
    quota = {cell: math.floor(len(ids) * fraction) for cell, ids in cells.items()}
    target = round(len(uv) * fraction)
    remainder_order = sorted(
        cells,
        key=lambda c: (-(len(cells[c]) * fraction - quota[c]), c),
    )
    for cell in remainder_order[: target - sum(quota.values())]:
        quota[cell] += 1
    held: set[int] = set()
    for cell, ids in cells.items():
        ordered = sorted(
            ids,
            key=lambda i: hashlib.sha256(
                f"{seed}:{float(uv[i][0]):.8f}:{float(uv[i][1]):.8f}:{i}".encode()
            ).digest(),
        )
        held.update(ordered[: quota[cell]])
    return [i for i in range(len(uv)) if i not in held], sorted(held)


def quarantine_shared_keypoints(
    tracks: Sequence[Mapping[str, Any]],
    merge_radius_px: float = 0.0,
) -> list[dict[str, Any]]:
    """A held-out keypoint must not re-enter BA through another adjacent edge or window."""
    held_nodes = {
        tuple(node)
        for t in tracks
        if t["split"] == "holdout"
        for node in t["nodes"]
    }
    if merge_radius_px <= 0:
        return [
            dict(t)
            for t in tracks
            if t["split"] == "holdout"
            or not any(tuple(n) in held_nodes for n in t["nodes"])
        ]
    held_pixels: dict[int, list[Sequence[float]]] = defaultdict(list)
    for t in tracks:
        if t["split"] == "holdout":
            for c, px in zip(t.get("pair", []), t.get("uv", [])):
                held_pixels[int(c)].append(px)
    r2 = merge_radius_px**2

    def near_held(track: Mapping[str, Any]) -> bool:
        if any(tuple(n) in held_nodes for n in track["nodes"]):
            return True
        for c, px in zip(track.get("pair", []), track.get("uv", [])):
            if any(
                (px[0] - hx[0]) ** 2 + (px[1] - hx[1]) ** 2 <= r2
                for hx in held_pixels[int(c)]
            ):
                return True
        return False

    return [
        dict(t)
        for t in tracks
        if t["split"] == "holdout" or not near_held(t)
    ]


def deduplicate_multiwindow_matches(
    candidates: Sequence[Mapping[str, Any]],
    merge_radius_px: float = 2.0,
) -> list[dict[str, Any]]:
    """Merge multi-window matches that refer to the same physical 2-view feature."""
    r2 = merge_radius_px**2
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        ua, ub = cand["uv"]
        if any(
            (ua[0] - k["uv"][0][0]) ** 2 + (ua[1] - k["uv"][0][1]) ** 2 <= r2
            and (ub[0] - k["uv"][1][0]) ** 2 + (ub[1] - k["uv"][1][1]) ** 2 <= r2
            for k in kept
        ):
            continue
        kept.append(dict(cand))
    return kept


def percentile(values: Sequence[float], q: float) -> float:
    sorted_vals = sorted(values)
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def statistics(values: Sequence[float]) -> dict[str, Any]:
    """Do not silently improve metrics by dropping invalid projections."""
    finite = [float(x) for x in values if math.isfinite(x)]
    valid = len(finite) == len(values) and bool(values)
    return {
        "n": len(values),
        "invalid": len(values) - len(finite),
        "median": percentile(finite, 0.5) if valid else None,
        "p90": percentile(finite, 0.9) if valid else None,
        "p95": percentile(finite, 0.95) if valid else None,
    }


def worsened(before: Mapping[str, Any], after: Mapping[str, Any], tolerance_px: float = 0.0) -> bool:
    return any(
        after[k] is None or before[k] is None or after[k] > before[k] + tolerance_px
        for k in ("median", "p90")
    )


def select_anchor(
    v0: Mapping[str, Any],
    cameras: Sequence[str] = CAMS,
) -> str:
    runs = [r for r in v0["runs"] if r["feature"] == "aliked"]
    errors = runs[0]["reproj_current_cal"]
    return min(cameras, key=lambda c: errors[c]["2view"]["median"])


def huber_terms(z: float, delta: float) -> tuple[float, float, float]:
    """rho(z), rho'(z), rho''(z), z = squared unscaled pixel residual."""
    if z <= delta * delta:
        return float(z), 1.0, 0.0
    root = math.sqrt(z)
    return (
        float(2 * delta * root - delta * delta),
        float(delta / root),
        float(-delta / (2 * z * root)),
    )


# ---------------------------------------------------------------------------
# Geometry & Projected Stage A Bundle Adjustment
# ---------------------------------------------------------------------------


def read_calibration(
    source: Mapping[str, Any] | Path | str,
    *,
    cameras_order: Sequence[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], np.ndarray, np.ndarray, np.ndarray, tuple[str, ...]]:
    """Read and validate a CalibrationProvider document into ``(doc, cameras, K, R, C, camera_ids)``."""
    if isinstance(source, (Path, str)):
        doc = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        doc = copy.deepcopy(dict(source))
    if doc.get("extrinsic_convention") != "OPENCV_WORLD_TO_CAMERA":
        raise ValueError("Expected OPENCV_WORLD_TO_CAMERA")
    by_id = {str(c["camera_id"]): c for c in doc["cameras"]}
    cam_ids = (
        tuple(cameras_order)
        if cameras_order is not None
        else tuple(str(c["camera_id"]) for c in doc["cameras"])
    )
    cameras = [by_id[c] for c in cam_ids]
    if any(
        c.get("distortion", {}).get("model") not in ("OPENCV_PINHOLE", "NONE")
        for c in cameras
    ):
        raise ValueError("Only fixed OpenCV pinhole or NONE distortion is supported")
    K = np.array([c["K"] for c in cameras], dtype=float)
    R = np.array([c["R"] for c in cameras], dtype=float)
    t = np.array([c["t"] for c in cameras], dtype=float)
    C = -np.einsum("nji,nj->ni", R, t)
    for rotation in R:
        if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-3):
            raise ValueError("Nonorthogonal rotation")
        if np.linalg.det(rotation) < 0:
            raise ValueError("Reflected rotation")
    return doc, cameras, K, R, C, cam_ids


def _normalize_pixels(uv: np.ndarray, K: np.ndarray) -> np.ndarray:
    """Convert pixel coordinates ``(N, 2)`` to normalized camera coordinates using ``K^-1``."""
    if len(uv) == 0:
        return np.zeros((0, 2), dtype=float)
    hom = np.column_stack((np.asarray(uv, dtype=float), np.ones(len(uv), dtype=float)))
    norm_h = hom @ np.linalg.inv(K).T
    return norm_h[:, :2] / norm_h[:, 2:3]


def _undistort_pixels(uv: np.ndarray, K: np.ndarray, distortion: Mapping[str, Any]) -> np.ndarray:
    coeffs = np.asarray(distortion["coeffs"], dtype=float)
    if not coeffs.size or not np.any(coeffs):
        return np.asarray(uv, dtype=float).copy()
    return cv2.undistortPoints(np.asarray(uv, dtype=float)[:, None, :], K, coeffs, P=K)[:, 0, :]


def _fit_essential_eight_point(na: np.ndarray, nb: np.ndarray) -> np.ndarray | None:
    """Normalized 8-point algorithm with rank-2 singular value projection."""
    if len(na) < 8:
        return None
    xa = np.column_stack((na, np.ones(len(na), dtype=float)))
    xb = np.column_stack((nb, np.ones(len(nb), dtype=float)))
    A = np.column_stack(
        [
            xb[:, 0] * xa[:, 0],
            xb[:, 0] * xa[:, 1],
            xb[:, 0] * xa[:, 2],
            xb[:, 1] * xa[:, 0],
            xb[:, 1] * xa[:, 1],
            xb[:, 1] * xa[:, 2],
            xb[:, 2] * xa[:, 0],
            xb[:, 2] * xa[:, 1],
            xb[:, 2] * xa[:, 2],
        ]
    )
    _, _, Vt = np.linalg.svd(A)
    E_raw = Vt[-1].reshape(3, 3)
    U, S, Vt_e = np.linalg.svd(E_raw)
    s_mean = 0.5 * (S[0] + S[1])
    return U @ np.diag([s_mean, s_mean, 0.0]) @ Vt_e


def _fit_essential_ransac(
    na: np.ndarray,
    nb: np.ndarray,
    threshold: float,
    *,
    seed: int = 449,
    max_iters: int = 10000,
) -> np.ndarray | None:
    """5점 RANSAC, 정규화 좌표와 임계값을 사용한다."""
    if len(na) < 8:
        return None
    cv2.setRNGSeed(seed)
    E, _ = cv2.findEssentialMat(np.asarray(na, dtype=np.float64), np.asarray(nb, dtype=np.float64),
                                np.eye(3), method=cv2.RANSAC, prob=0.9999,
                                threshold=threshold, maxIters=max_iters)
    return E if E is not None and E.shape == (3, 3) else None


def triangulate(
    uv: np.ndarray,
    pair: Sequence[int],
    K: np.ndarray,
    R: np.ndarray,
    C: np.ndarray,
    *,
    cameras: Sequence[str] = CAMS,
) -> np.ndarray:
    """Reuse Tier 0 geometry's public batch DLT on undistorted pixel observations."""
    rigs = [
        RigCamera.from_calibration(
            {
                "camera_id": cameras[c] if c < len(cameras) else f"cam_{c}",
                "K": K[c].tolist(),
                "R": R[c].tolist(),
                "t": (-R[c] @ C[c]).tolist(),
                "image_size": [2304, 1296],
                "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [0.0] * 5},
            }
        )
        for c in pair
    ]
    normalized = np.stack(
        [
            _normalize_pixels(uv[:, s], K[c])
            for s, c in enumerate(pair)
        ],
        axis=1,
    )
    return triangulate_normalized_many(rigs, normalized, refine_iterations=0, targets=uv)


def project(
    points: np.ndarray,
    camera: int,
    K: np.ndarray,
    R: np.ndarray,
    C: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    xc = (points - C[camera]) @ R[camera].T
    depth = xc[:, 2]
    safe = np.where(np.abs(depth) < 1e-8, np.where(depth < 0, -1e-8, 1e-8), depth)
    pixel = xc @ K[camera].T
    return pixel[:, :2] / safe[:, None], depth


def initial_valid(
    uv: np.ndarray,
    pair: Sequence[int],
    K: np.ndarray,
    R: np.ndarray,
    C: np.ndarray,
    angle: float,
) -> np.ndarray:
    xyz = triangulate(uv, pair, K, R, C)
    rays = [xyz - C[c] for c in pair]
    with np.errstate(divide="ignore", invalid="ignore"):
        cosine = np.sum(rays[0] * rays[1], axis=1) / (
            np.linalg.norm(rays[0], axis=1) * np.linalg.norm(rays[1], axis=1)
        )
        angles = np.degrees(np.arccos(np.clip(cosine, -1, 1)))
    keep = np.isfinite(xyz).all(axis=1) & (angles >= angle)
    for c in pair:
        keep &= project(xyz, c, K, R, C)[1] > 0
    return keep


def sampson(E: np.ndarray, na: np.ndarray, nb: np.ndarray) -> np.ndarray:
    xa = np.column_stack((na, np.ones(len(na))))
    xb = np.column_stack((nb, np.ones(len(nb))))
    exa, etxb = xa @ E.T, xb @ E
    return np.abs(np.sum(xb * exa, axis=1)) / np.sqrt(
        np.sum(exa[:, :2] ** 2 + etxb[:, :2] ** 2, axis=1).clip(1e-20)
    )


def load_tracks(
    source: Mapping[str, Any] | Path | str,
    config: Mapping[str, Any] | None = None,
    *,
    cameras: Sequence[str] = CAMS,
    pairs: Sequence[tuple[int, int]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay fixed observations/splits from a saved ``tracks.json`` path or dict."""
    cfg = {**DEFAULT_STAGE_A_CONFIG, **(dict(config) if config is not None else {})}
    if isinstance(source, (Path, str)):
        source_path = str(Path(source).resolve())
        saved = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        source_path = "<memory>"
        saved = copy.deepcopy(dict(source))

    pair_tuple = (
        tuple(pairs)
        if pairs is not None
        else (
            PAIRS
            if len(cameras) == 4
            else tuple((k, (k + 1) % len(cameras)) for k in range(len(cameras)))
        )
    )
    tracks = saved.get("tracks", [])
    if not tracks or len({t["id"] for t in tracks}) != len(tracks):
        raise ValueError("Empty tracks or duplicate track IDs")
    for track in tracks:
        uv = np.asarray(track["uv"], dtype=float)
        if tuple(track["pair"]) not in pair_tuple or track["split"] not in ("train", "holdout"):
            raise ValueError(f"Invalid track pair/split: {track['id']}")
        if uv.shape != (2, 2) or not np.isfinite(uv).all():
            raise ValueError(f"Invalid track pixels: {track['id']}")
    merge_radius_px = float(cfg.get("multiwindow_merge_radius_px", 2.0))
    if len(quarantine_shared_keypoints(tracks, merge_radius_px=merge_radius_px)) != len(tracks):
        raise ValueError("Saved holdout keypoint re-enters training")
    pairs_meta: dict[str, Any] = {}
    for a, b in pair_tuple:
        key = f"{cameras[a]}-{cameras[b]}"
        rec = dict(saved.get("pairs", {}).get(key, {}))
        for split in ("train", "holdout"):
            count = sum(t["pair"] == [a, b] and t["split"] == split for t in tracks)
            if split in rec and rec[split] != count:
                raise ValueError(f"Saved track count mismatch: {key}/{split}")
            rec[split] = count
        if rec["train"] < cfg["min_train_per_pair"]:
            raise ValueError(f"Insufficient saved training tracks: {key}")
        rec["holdout_insufficient"] = rec["holdout"] < cfg["min_holdout_per_pair"]
        pairs_meta[key] = rec
    return (
        [dict(t) for t in tracks],
        {
            "backgrounds": saved.get("backgrounds", {}),
            "pairs": pairs_meta,
            "quarantined_train_tracks": saved.get("quarantined_train_tracks"),
            "from_tracks": source_path,
            "feature_extraction_reused": True,
        },
    )


class Problem:
    """Small camera block plus independent 2-view points; fixed anchor R and C."""

    solver = "joint"

    def __init__(
        self,
        tracks: Sequence[Mapping[str, Any]],
        K: np.ndarray,
        R0: np.ndarray,
        C0: np.ndarray,
        anchor: int,
        config: Mapping[str, Any],
        center_sigma: float | None,
        *,
        cameras: Sequence[str] = CAMS,
        pairs: Sequence[tuple[int, int]] | None = None,
    ) -> None:
        self.tracks = list(tracks)
        self.K = K
        self.R0 = R0
        self.C0 = C0
        self.cameras = tuple(cameras)
        self.n_cams = len(self.cameras)
        self.pairs = (
            tuple(pairs)
            if pairs is not None
            else (
                PAIRS
                if self.n_cams == 4
                else tuple((k, (k + 1) % self.n_cams) for k in range(self.n_cams))
            )
        )
        self.free = [i for i in range(self.n_cams) if i != anchor]
        self.cfg = dict(config)
        self.sigma = center_sigma
        self.dof = 3 if center_sigma is None else 6
        self.nc = len(self.free) * self.dof
        self.uv = np.array([t["uv"] for t in self.tracks], dtype=float)
        self.pair = np.array([t["pair"] for t in self.tracks], dtype=int)
        confirmed = set(self.cfg.get("confirmed_floor_tracks", []))
        self.floor = [i for i, t in enumerate(self.tracks) if t["id"] in confirmed]
        self.pair_ids = {
            pair: np.flatnonzero(np.all(self.pair == pair, axis=1))
            for pair in self.pairs
        }

    def camera_poses(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        R, C = self.R0.copy(), self.C0.copy()
        for k, c in enumerate(self.free):
            offset = k * self.dof
            R[c] = Rotation.from_rotvec(x[offset : offset + 3]).as_matrix() @ self.R0[c]
            if self.dof == 6:
                C[c] += x[offset + 3 : offset + 6]
        return R, C

    def unpack(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        R, C = self.camera_poses(x)
        return R, C, x[self.nc :].reshape(-1, 3)

    def pack_cameras(self, R: np.ndarray, C: np.ndarray) -> np.ndarray:
        x = np.zeros(self.nc)
        for k, c in enumerate(self.free):
            offset = k * self.dof
            rotvec = Rotation.from_matrix(R[c] @ self.R0[c].T).as_rotvec()
            x[offset : offset + 3] = 0.0 if np.linalg.norm(rotvec) < 1e-12 else rotvec
            if self.dof == 6:
                delta = C[c] - self.C0[c]
                x[offset + 3 : offset + 6] = 0.0 if np.linalg.norm(delta) < 1e-12 else delta
        return x

    def points_at_poses(self, R: np.ndarray, C: np.ndarray) -> np.ndarray:
        xyz = np.empty((len(self.tracks), 3))
        for pair, ids in self.pair_ids.items():
            if len(ids):
                xyz[ids] = triangulate(
                    self.uv[ids], pair, self.K, R, C, cameras=self.cameras
                )
        if not np.isfinite(xyz).all():
            raise ValueError("Nonfinite training triangulation; candidate rejected")
        return xyz

    def pack(self, R: np.ndarray, C: np.ndarray) -> np.ndarray:
        return np.concatenate((self.pack_cameras(R, C), self.points_at_poses(R, C).ravel()))

    def pixel_residual(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        R, C, xyz = self.unpack(x)
        residual, depth, _ = self.point_pixels(xyz, R, C)
        return residual.ravel(), depth

    def point_pixels(
        self,
        xyz: np.ndarray,
        R: np.ndarray,
        C: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """All two-view projections and analytic point Jacobians, in track order."""
        rotations, intrinsics = R[self.pair], self.K[self.pair]
        xc = np.einsum("nsij,nsj->nsi", rotations, xyz[:, None] - C[self.pair])
        depth = xc[:, :, 2]
        safe = np.where(np.abs(depth) < 1e-8, np.where(depth < 0, -1e-8, 1e-8), depth)
        pixel = np.einsum("nsij,nsj->nsi", intrinsics, xc)
        predicted = pixel[:, :, :2] / safe[:, :, None]
        kr = np.einsum("nsij,nsjk->nsik", intrinsics, rotations)
        dz = rotations[:, :, 2] * (np.abs(depth) >= 1e-8)[:, :, None]
        jac = (
            kr[:, :, :2] - predicted[:, :, :, None] * dz[:, :, None]
        ) / safe[:, :, None, None]
        return predicted - self.uv, depth, jac

    def residual(self, x: np.ndarray) -> np.ndarray:
        pix, depth = self.pixel_residual(x)
        R, C, xyz = self.unpack(x)
        prior: list[float] = []
        for c in self.free:
            prior.extend(
                Rotation.from_matrix(R[c] @ self.R0[c].T).as_rotvec()
                / np.radians(self.cfg["rotation_sigma_deg"])
            )
            if self.sigma is not None:
                prior.extend((C[c] - self.C0[c]) / self.sigma)
        heights = self.cfg["heights_m"]
        prior.extend(
            (C[:, 2] - np.array([heights[c] for c in self.cameras]))
            / self.cfg["height_sigma_m"]
        )
        prior.extend(xyz[self.floor, 2] / 0.015)
        return np.concatenate(
            (pix, np.array(prior), np.minimum(depth.ravel() - 0.01, 0) / 0.01)
        )

    def loss(self, z: np.ndarray) -> np.ndarray:
        rho = np.vstack((z.copy(), np.ones_like(z), np.zeros_like(z)))
        ids = np.flatnonzero(z[: 4 * len(self.tracks)] > self.cfg["huber_px"] ** 2)
        root = np.sqrt(z[ids])
        delta = self.cfg["huber_px"]
        rho[0, ids] = 2 * delta * root - delta**2
        rho[1, ids] = delta / root
        rho[2, ids] = -delta / (2 * z[ids] * root)
        return rho

    def height_prior_summary(self, x: np.ndarray) -> dict[str, Any]:
        _, C, _ = self.unpack(x)
        sigma = self.cfg["height_sigma_m"]
        heights = self.cfg["heights_m"]
        return {
            "sigma_m": sigma,
            "loss": "quadratic",
            "hard_constraint": False,
            "cameras": {
                cid: {
                    "residual_m": float(C[c, 2] - heights[cid]),
                    "residual_sigma": float((C[c, 2] - heights[cid]) / sigma),
                    "cost": float(0.5 * ((C[c, 2] - heights[cid]) / sigma) ** 2),
                    "optimized": c in self.free and self.sigma is not None,
                }
                for c, cid in enumerate(self.cameras)
            },
        }

    def solver_options(self) -> dict[str, Any]:
        return {"jac_sparsity": self.sparsity()}

    def sparsity(self):
        n = len(self.tracks)
        pattern = lil_matrix(
            (len(self.residual(self.pack(self.R0, self.C0))), self.nc + 3 * n),
            dtype=int,
        )
        depth_start = pattern.shape[0] - 2 * n
        for i, pair in enumerate(self.pair):
            for c in pair:
                if c in self.free:
                    k = self.free.index(int(c)) * self.dof
                    pattern[4 * i : 4 * i + 4, k : k + self.dof] = 1
                    pattern[depth_start + 2 * i : depth_start + 2 * i + 2, k : k + self.dof] = 1
            pattern[4 * i : 4 * i + 4, self.nc + 3 * i : self.nc + 3 * i + 3] = 1
        pattern[4 * n :, : self.nc] = 1
        prior_rows = (
            3 * len(self.free)
            + (3 * len(self.free) if self.sigma is not None else 0)
            + self.n_cams
        )
        for j, i in enumerate(self.floor):
            pattern[4 * n + prior_rows + j, self.nc + 3 * i + 2] = 1
        for i in range(n):
            pattern[
                depth_start + 2 * i : depth_start + 2 * i + 2,
                self.nc + 3 * i : self.nc + 3 * i + 3,
            ] = 1
        return pattern.tocsr()

    def solve(
        self,
        start_R: np.ndarray,
        start_C: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, Any, dict[str, Any]]:
        started = time.perf_counter()
        x0 = self.pack(start_R, start_C)
        initial_cost = self.cost_summary(x0)
        scale = np.full_like(x0, self.cfg.get("x_scale_point_m", 0.05))
        for k in range(len(self.free)):
            scale[k * self.dof : k * self.dof + 3] = self.cfg.get("x_scale_rotation_rad", 0.005)
            if self.dof == 6:
                scale[k * self.dof + 3 : k * self.dof + 6] = self.cfg.get("x_scale_center_m", 0.01)
        calls = 0

        def counted_residual(x: np.ndarray) -> np.ndarray:
            nonlocal calls
            calls += 1
            return self.residual(x)

        result = least_squares(
            counted_residual,
            x0,
            **self.solver_options(),
            loss=self.loss,
            f_scale=1.0,
            x_scale=scale,
            method="trf",
            max_nfev=self.cfg["max_nfev"],
            ftol=1e-8,
            xtol=1e-8,
            gtol=1e-8,
        )
        R, C, _ = self.unpack(result.x)
        blockers = [] if result.success else ["solver_not_converged"]
        if np.any(self.pixel_residual(result.x)[1] <= 0):
            blockers.append("training_invalid_depths")
        projection = getattr(self, "projection_info", None)
        if projection is not None and not projection["converged"]:
            blockers.append("point_projection_not_converged")
        info: dict[str, Any] = {
            "seconds": time.perf_counter() - started,
            "success": bool(result.success),
            "solver": self.solver,
            "variables": len(x0),
            "residual_calls": calls,
            "adoption_forbidden": bool(blockers),
            "adoption_blockers": blockers,
            "message": result.message,
            "nfev": result.nfev,
            "njev": result.njev,
            "status": int(result.status),
            "cost": float(result.cost),
            "initial_cost": initial_cost,
            "final_cost": self.cost_summary(result.x),
            "camera_parameter_step_norm": float(
                np.linalg.norm(result.x[: self.nc] - x0[: self.nc])
            ),
            "height_prior": self.height_prior_summary(result.x),
            "optimality": float(result.optimality),
        }
        if projection is not None:
            info["point_projection"] = dict(projection)
        return R, C, result, info

    def cost_summary(self, x: np.ndarray) -> dict[str, float]:
        residual = self.residual(x)
        rho = self.loss(residual**2)[0]
        pixel_end = 4 * len(self.tracks)
        depth_start = len(residual) - 2 * len(self.tracks)
        return {
            "total": float(0.5 * rho.sum()),
            "pixel_huber": float(0.5 * rho[:pixel_end].sum()),
            "quadratic_priors": float(0.5 * rho[pixel_end:depth_start].sum()),
            "depth_barrier": float(0.5 * rho[depth_start:].sum()),
            "pixel_rms_px": float(np.sqrt(np.mean(residual[:pixel_end] ** 2))),
        }

    def observability(self, x: np.ndarray) -> dict[str, Any]:
        base, depth = self.pixel_residual(x)
        n = len(self.tracks)
        camera_jac = np.empty((4 * n, self.nc))
        nonpixel_jac = np.empty((len(self.residual(x)) - 4 * n, self.nc))
        step = 1e-6
        for j in range(self.nc):
            xp, xm = x.copy(), x.copy()
            xp[j] += step
            xm[j] -= step
            camera_jac[:, j] = (
                (self.pixel_residual(xp)[0] - self.pixel_residual(xm)[0]) / (2 * step) * 0.01
            )
            nonpixel_jac[:, j] = (
                (self.residual(xp)[4 * n :] - self.residual(xm)[4 * n :]) / (2 * step) * 0.01
            )
        R, C, xyz = self.unpack(x)
        point_jac = self.point_pixels(xyz, R, C)[2].reshape(n, 4, 3) * 0.01
        prior_rows = (
            3 * len(self.free)
            + (3 * len(self.free) if self.sigma is not None else 0)
            + self.n_cams
        )
        reduced, augmented = [], [nonpixel_jac[:prior_rows]]
        floor_order = {i: j for j, i in enumerate(self.floor)}
        for i in range(n):
            jp, jc = point_jac[i], camera_jac[4 * i : 4 * i + 4]
            reduced.append(jc - jp @ np.linalg.pinv(jp) @ jc)
            jps, jcs = [jp], [jc]
            if i in floor_order:
                jps.append(np.array([[0.0, 0.0, 0.01 / 0.015]]))
                row = prior_rows + floor_order[i]
                jcs.append(nonpixel_jac[row : row + 1])
            jps.append(R[self.pair[i], 2] * (depth[i] < 0.01)[:, None])
            row = prior_rows + len(self.floor) + 2 * i
            jcs.append(nonpixel_jac[row : row + 2])
            jp_aug, jc_aug = np.vstack(jps), np.vstack(jcs)
            augmented.append(jc_aug - jp_aug @ np.linalg.pinv(jp_aug) @ jc_aug)
        reduced_mat = np.vstack(reduced)
        augmented_mat = np.vstack(augmented)

        def describe(matrix: np.ndarray) -> dict[str, Any]:
            singular = np.linalg.svd(matrix, compute_uv=False)
            return {
                "singular_values": singular.tolist(),
                "smallest": float(singular[-1]),
                "smallest_relative": float(singular[-1] / singular[0]) if singular[0] else 0.0,
                "weak_modes": int(
                    np.sum(singular <= singular[0] * self.cfg["svd_relative_threshold"])
                ),
            }

        return {
            "data_only_schur": describe(reduced_mat),
            "with_quadratic_priors_schur": describe(augmented_mat),
            "column_units": "0.01 rad rotations; 0.01 m centers/points",
            "pixel_rows": len(base),
        }


class ProjectedProblem(Problem):
    """Variable projection: 9/18 camera parameters and independent robust point fits."""

    solver = "projected"

    def pack(self, R: np.ndarray, C: np.ndarray) -> np.ndarray:
        return self.pack_cameras(R, C)

    def solver_options(self) -> dict[str, Any]:
        return {"tr_solver": "exact", "jac": self.profiled_jacobian}

    def point_system(
        self,
        xyz: np.ndarray,
        R: np.ndarray,
        C: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        pixel, depth, jac = self.point_pixels(xyz, R, C)
        n = len(xyz)
        residual = np.zeros((n, 7))
        derivatives = np.zeros((n, 7, 3))
        residual[:, :4] = pixel.reshape(n, 4)
        derivatives[:, :4] = jac.reshape(n, 4, 3)
        residual[:, 4:6] = np.minimum(depth - 0.01, 0) / 0.01
        derivatives[:, 4:6] = R[self.pair, 2] * (depth < 0.01)[:, :, None] / 0.01
        residual[self.floor, 6] = xyz[self.floor, 2] / 0.015
        derivatives[self.floor, 6, 2] = 1 / 0.015
        weights = self.point_weights(residual)
        squared = residual**2
        delta = self.cfg["huber_px"]
        squared[:, :4] = np.where(
            weights[:, :4] < 1,
            2 * delta * np.abs(residual[:, :4]) - delta**2,
            squared[:, :4],
        )
        return 0.5 * squared.sum(axis=1), residual, derivatives

    def point_weights(self, residual: np.ndarray) -> np.ndarray:
        weights = np.ones_like(residual)
        weights[:, :4] = np.minimum(
            1.0,
            self.cfg["huber_px"] / np.maximum(np.abs(residual[:, :4]), 1e-15),
        )
        return weights

    def point_curvature(self, residual: np.ndarray) -> np.ndarray:
        curvature = np.ones_like(residual)
        curvature[:, :4] = np.abs(residual[:, :4]) < self.cfg["huber_px"]
        return curvature

    def point_hessian(
        self,
        xyz: np.ndarray,
        R: np.ndarray,
        C: np.ndarray,
        residual: np.ndarray,
        jac: np.ndarray,
    ) -> np.ndarray:
        psi = self.point_weights(residual) * residual
        hessian = np.einsum("nri,nr,nrj->nij", jac, self.point_curvature(residual), jac)
        depth = np.einsum("nsi,nsi->ns", R[self.pair, 2], xyz[:, None] - C[self.pair])
        safe = np.where(np.abs(depth) < 1e-8, 1.0, depth)
        factor = (np.abs(depth) >= 1e-8) / safe
        jp = jac[:, :4].reshape(-1, 2, 2, 3)
        a = np.einsum(
            "nsq,nsqi,nsj,ns->nij",
            psi[:, :4].reshape(-1, 2, 2),
            jp,
            R[self.pair, 2],
            factor,
        )
        return hessian - a - a.transpose(0, 2, 1)

    @staticmethod
    def skew(vector: np.ndarray) -> np.ndarray:
        vector = np.asarray(vector)
        matrix = np.zeros((*vector.shape[:-1], 3, 3))
        matrix[..., 0, 1], matrix[..., 0, 2] = -vector[..., 2], vector[..., 1]
        matrix[..., 1, 0], matrix[..., 1, 2] = vector[..., 2], -vector[..., 0]
        matrix[..., 2, 0], matrix[..., 2, 1] = -vector[..., 1], vector[..., 0]
        return matrix

    @classmethod
    def rotation_left_jacobian(cls, vector: np.ndarray) -> np.ndarray:
        theta2 = float(vector @ vector)
        cross = cls.skew(vector)
        if theta2 < 1e-8:
            a = 0.5 - theta2 / 24 + theta2**2 / 720
            b = 1 / 6 - theta2 / 120 + theta2**2 / 5040
        else:
            theta = np.sqrt(theta2)
            a, b = (1 - np.cos(theta)) / theta2, (theta - np.sin(theta)) / theta**3
        return np.eye(3) + a * cross + b * cross @ cross

    def fixed_point_camera_system(
        self,
        x: np.ndarray,
        xyz: np.ndarray,
        R: np.ndarray,
        C: np.ndarray,
        residual: np.ndarray,
        jp: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        n = len(xyz)
        jc = np.zeros((n, 7, self.nc))
        cross = np.zeros((n, 3, self.nc))
        psi = self.point_weights(residual) * residual
        curvature = self.point_curvature(residual)
        for k, c in enumerate(self.free):
            offset = k * self.dof
            left = self.rotation_left_jacobian(x[offset : offset + 3])
            dr = np.stack([self.skew(left[:, j]) @ R[c] for j in range(3)], axis=-1)
            for side in range(2):
                ids = np.flatnonzero(self.pair[:, side] == c)
                if not len(ids):
                    continue
                xc = (xyz[ids] - C[c]) @ R[c].T
                depth = xc[:, 2]
                moving = np.abs(depth) >= 1e-8
                safe = np.where(moving, depth, np.where(depth < 0, -1e-8, 1e-8))
                predicted = (xc @ self.K[c].T)[:, :2] / safe[:, None]
                dpi = np.broadcast_to(self.K[c, :2], (len(ids), 2, 3)).copy()
                dpi[:, :, 2] -= predicted * moving[:, None]
                dpi /= safe[:, None, None]
                dxc = np.zeros((len(ids), 3, self.dof))
                dxc[:, :, :3] = -self.skew(xc) @ left
                if self.dof == 6:
                    dxc[:, :, 3:] = -R[c]
                camera_pixels = dpi @ dxc
                rows = slice(2 * side, 2 * side + 2)
                columns = slice(offset, offset + self.dof)
                jc[ids, rows, columns] = camera_pixels
                djp = np.zeros((len(ids), 2, 3, self.dof))
                djp[:, :, :, :3] = np.einsum("nqi,ijk->nqjk", dpi, dr)
                djp -= (
                    camera_pixels[:, :, None, :] * R[c, 2][None, None, :, None]
                    + jp[ids, rows, :, None] * dxc[:, None, 2:3, :]
                ) * (moving / safe)[:, None, None, None]
                cross[ids, :, columns] += np.einsum(
                    "nqi,nq,nqj->nij", jp[ids, rows], curvature[ids, rows], camera_pixels
                )
                cross[ids, :, columns] += np.einsum("nq,nqij->nij", psi[ids, rows], djp)
                active_depth = depth < 0.01
                depth_jc = dxc[:, 2] * active_depth[:, None] / 0.01
                jc[ids, 4 + side, columns] = depth_jc
                cross[ids, :, columns] += jp[ids, 4 + side, :, None] * depth_jc[:, None]
                cross[ids, :, offset : offset + 3] += (
                    psi[ids, 4 + side, None, None]
                    * dr[2][None]
                    * active_depth[:, None, None]
                    / 0.01
                )
        return jc, cross

    def profiled_jacobian(self, x: np.ndarray) -> np.ndarray:
        R, C, xyz = self.unpack(x)
        _, residual, jp = self.point_system(xyz, R, C)
        jc, cross = self.fixed_point_camera_system(x, xyz, R, C, residual, jp)
        hessian = self.point_hessian(xyz, R, C, residual, jp)
        eigen = np.linalg.eigvalsh(hessian)
        regular = eigen[:, 0] > 1e-10 * np.maximum(eigen[:, -1], 1.0)
        if np.any(~regular):
            weights = self.point_weights(residual)
            hessian[~regular] = np.einsum(
                "nri,nr,nrj->nij", jp[~regular], weights[~regular], jp[~regular]
            )
            cross[~regular] = np.einsum(
                "nri,nr,nrj->nij", jp[~regular], weights[~regular], jc[~regular]
            )
        point_response = -np.linalg.pinv(hessian, rcond=1e-12, hermitian=True) @ cross
        local = jc + jp @ point_response
        n = len(xyz)
        prior_rows = len(self.free) * self.dof + self.n_cams
        matrix = np.zeros((4 * n + prior_rows + len(self.floor) + 2 * n, self.nc))
        matrix[: 4 * n] = local[:, :4].reshape(4 * n, self.nc)
        row = 4 * n
        for k, c in enumerate(self.free):
            offset = k * self.dof
            matrix[row : row + 3, offset : offset + 3] = np.eye(3) / np.radians(
                self.cfg["rotation_sigma_deg"]
            )
            row += 3
            if self.dof == 6:
                matrix[row : row + 3, offset + 3 : offset + 6] = np.eye(3) / self.sigma
                matrix[4 * n + len(self.free) * self.dof + c, offset + 5] = (
                    1 / self.cfg["height_sigma_m"]
                )
                row += 3
        row += self.n_cams
        matrix[row : row + len(self.floor)] = local[self.floor, 6]
        matrix[row + len(self.floor) :] = local[:, 4:6].reshape(2 * n, self.nc)
        self.projection_info["jacobian"] = "analytic_implicit_stationarity"
        self.projection_info["schur_tangent_fallback_points"] = int(np.sum(~regular))
        return matrix

    def unpack(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if hasattr(self, "_cached_x") and np.array_equal(x, self._cached_x):
            return self._cached_unpack
        R, C = self.camera_poses(x)
        xyz = self.points_at_poses(R, C)
        tolerance = self.cfg.get("projected_point_gradient_tol", 1e-4)
        target = min(tolerance * 0.1, 1e-8)
        step_tolerance = self.cfg.get("projected_point_step_tol", 1e-12)
        limit = self.cfg.get("projected_point_max_iterations", 100)
        point_iterations = np.zeros(len(xyz), dtype=int)
        last_step = np.zeros(len(xyz))
        stalled = np.zeros(len(xyz), dtype=bool)
        iterations = 0
        for iterations in range(1, limit + 1):
            cost, residual, jac = self.point_system(xyz, R, C)
            weights = self.point_weights(residual)
            gradient = np.einsum("nri,nr->ni", jac, weights * residual)
            active = (np.max(np.abs(gradient), axis=1) > target) & ~stalled
            if not np.any(active):
                break
            point_iterations[active] += 1
            irls = np.einsum("nri,nr,nrj->nij", jac, weights, jac)
            hessian = self.point_hessian(xyz, R, C, residual, jac)
            eigen, basis = np.linalg.eigh(hessian)
            floor = 1e-6 * np.maximum(np.trace(irls, axis1=1, axis2=2), 1.0)
            eigen = np.maximum(eigen, floor[:, None])
            step = np.einsum(
                "nij,nj->ni",
                basis,
                np.einsum("nji,nj->ni", basis, gradient) / eigen,
            )
            radius = 0.5 * (1 + np.linalg.norm(xyz, axis=1))
            step *= np.minimum(
                1.0, radius / np.maximum(np.linalg.norm(step, axis=1), 1e-30)
            )[:, None]
            step[~active] = 0
            alpha = np.ones(len(xyz))
            accepted = ~active
            origin = xyz.copy()
            slope = np.sum(gradient * step, axis=1)
            for _ in range(30):
                candidate = origin - alpha[:, None] * step
                trial, tr, tj = self.point_system(candidate, R, C)
                slack = 1e-12 * np.maximum(cost, 1.0)
                sufficient = trial <= cost - 1e-4 * alpha * slope
                near = np.abs(trial - cost) <= slack
                trial_gradient = np.einsum("nri,nr->ni", tj, self.point_weights(tr) * tr)
                improved = np.max(np.abs(trial_gradient), axis=1) < np.max(
                    np.abs(gradient), axis=1
                )
                good = ~accepted & np.isfinite(trial) & (sufficient | (near & improved))
                xyz[good] = candidate[good]
                last_step[good] = np.linalg.norm(candidate[good] - origin[good], axis=1)
                accepted |= good
                if np.all(accepted):
                    break
                alpha[~accepted] *= 0.5
            stalled |= active & ~accepted
            stalled |= (
                active
                & accepted
                & (last_step <= step_tolerance * (1 + np.linalg.norm(xyz, axis=1)))
            )
        _, residual, jac = self.point_system(xyz, R, C)
        gradient = np.einsum("nri,nr->ni", jac, self.point_weights(residual) * residual)
        fallback = np.flatnonzero(np.max(np.abs(gradient), axis=1) > target)
        fallback_nfev = 0
        anchor = next(c for c in range(self.n_cams) if c not in self.free)
        for i in fallback:
            single = ProjectedProblem(
                [self.tracks[int(i)]],
                self.K,
                self.R0,
                self.C0,
                anchor,
                self.cfg,
                self.sigma,
                cameras=self.cameras,
                pairs=self.pairs,
            )

            def fun(point: np.ndarray) -> np.ndarray:
                return single.point_system(point[None], R, C)[1][0]  # noqa: B023 - 같은 반복 안에서 바로 쓴다

            def derivative(point: np.ndarray) -> np.ndarray:
                return single.point_system(point[None], R, C)[2][0]  # noqa: B023 - 같은 반복 안에서 바로 쓴다

            fit = least_squares(
                fun,
                xyz[int(i)],
                jac=derivative,
                loss=single.loss,
                x_scale=self.cfg.get("x_scale_point_m", 0.05),
                max_nfev=self.cfg.get("projected_point_fallback_nfev", 200),
                ftol=None,
                xtol=1e-14,
                gtol=target * 0.1,
            )
            fallback_nfev += fit.nfev
            old = single.point_system(xyz[int(i) : int(i) + 1], R, C)
            new = single.point_system(fit.x[None], R, C)
            old_gradient = np.max(
                np.abs(old[2][0].T @ (single.point_weights(old[1])[0] * old[1][0]))
            )
            new_gradient = np.max(
                np.abs(new[2][0].T @ (single.point_weights(new[1])[0] * new[1][0]))
            )
            slack = 1e-12 * max(float(old[0][0]), 1.0)
            if new[0][0] < old[0][0] or (
                new[0][0] <= old[0][0] + slack and new_gradient < old_gradient
            ):
                last_step[int(i)] = np.linalg.norm(fit.x - xyz[int(i)])
                xyz[int(i)] = fit.x
        _, residual, jac = self.point_system(xyz, R, C)
        gradient = np.einsum("nri,nr->ni", jac, self.point_weights(residual) * residual)
        worst = float(np.max(np.abs(gradient)))
        self.projection_info: dict[str, Any] = {
            "converged": worst <= tolerance,
            "max_point_gradient": worst,
            "gradient_tolerance": tolerance,
            "solve_gradient_target": target,
            "iterations": iterations,
            "max_iterations": limit,
            "max_point_iterations": int(point_iterations.max()),
            "max_last_step_m": float(last_step.max()),
            "step_tolerance": step_tolerance,
            "unconverged_points": int(np.sum(np.max(np.abs(gradient), axis=1) > tolerance)),
            "stalled_before_fallback": int(stalled.sum()),
            "fallback_points": len(fallback),
            "fallback_nfev": fallback_nfev,
        }
        self._cached_x, self._cached_unpack = x.copy(), (R, C, xyz)
        return R, C, xyz

    def observability(self, x: np.ndarray) -> dict[str, Any]:
        _, _, xyz = self.unpack(x)
        joint = Problem(
            self.tracks,
            self.K,
            self.R0,
            self.C0,
            next(c for c in range(self.n_cams) if c not in self.free),
            self.cfg,
            self.sigma,
            cameras=self.cameras,
            pairs=self.pairs,
        )
        return joint.observability(np.concatenate((x, xyz.ravel())))


def evaluate(
    tracks: Sequence[Mapping[str, Any]],
    K: np.ndarray,
    R: np.ndarray,
    C: np.ndarray,
    *,
    cameras: Sequence[str] = CAMS,
    pairs: Sequence[tuple[int, int]] | None = None,
    split: str = "holdout",
) -> dict[str, Any]:
    """Evaluate 2-view reprojection statistics on ``split`` (default ``"holdout"``)."""
    pair_tuple = (
        tuple(pairs)
        if pairs is not None
        else (
            PAIRS
            if len(cameras) == 4
            else tuple((k, (k + 1) % len(cameras)) for k in range(len(cameras)))
        )
    )
    by_camera: dict[str, list[float]] = {c: [] for c in cameras}
    by_pair: dict[str, dict[str, Any]] = {}
    invalid = 0
    for pair in pair_tuple:
        chosen = [
            t
            for t in tracks
            if t["pair"] == list(pair) and (split == "all" or t["split"] == split)
        ]
        if not chosen:
            by_pair[f"{cameras[pair[0]]}-{cameras[pair[1]]}"] = statistics([])
            continue
        uv = np.array([t["uv"] for t in chosen], dtype=float)
        xyz = triangulate(uv, pair, K, R, C, cameras=cameras)
        errors: list[float] = []
        for side, c in enumerate(pair):
            predicted, depth = project(xyz, c, K, R, C)
            error = np.linalg.norm(predicted - uv[:, side], axis=1)
            error[(depth <= 0) | ~np.isfinite(xyz).all(axis=1)] = np.inf
            by_camera[cameras[c]].extend(error.tolist())
            errors.extend(error.tolist())
        by_pair[f"{cameras[pair[0]]}-{cameras[pair[1]]}"] = statistics(errors)
        invalid += sum(not math.isfinite(e) for e in errors)
    return {
        "cameras": {c: statistics(v) for c, v in by_camera.items()},
        "pairs": by_pair,
        "invalid_observations": invalid,
    }


def pose_change(
    R: np.ndarray,
    C: np.ndarray,
    R0: np.ndarray,
    C0: np.ndarray,
    heights: Mapping[str, float],
    *,
    cameras: Sequence[str] = CAMS,
) -> dict[str, dict[str, Any]]:
    return {
        cid: {
            "rotation_deg": float(np.degrees(Rotation.from_matrix(R[i] @ R0[i].T).magnitude())),
            "center_cm": float(np.linalg.norm(C[i] - C0[i]) * 100),
            "center_delta_xyz_cm": ((C[i] - C0[i]) * 100).tolist(),
            "height_residual_cm": float((C[i, 2] - heights[cid]) * 100),
        }
        for i, cid in enumerate(cameras)
    }


# ---------------------------------------------------------------------------
# Height checks, static object cross-check hook (-462), and Stage A pipeline
# ---------------------------------------------------------------------------


def compute_height_checks(
    camera_heights_m: Mapping[str, float],
    restored_heights_m: Mapping[str, float],
    *,
    tolerance_cm: float = 5.0,
) -> dict[str, dict[str, Any]]:
    """Compare measured input camera heights with restored camera center Z coordinates."""
    if tolerance_cm <= 0:
        raise ValueError("tolerance_cm must be > 0")
    checks: dict[str, dict[str, Any]] = {}
    for cam_id, input_h in camera_heights_m.items():
        if cam_id not in restored_heights_m:
            raise KeyError(f"Missing restored height for camera {cam_id!r}")
        in_m = float(input_h)
        rest_m = float(restored_heights_m[cam_id])
        if in_m <= 0 or rest_m <= 0:
            raise ValueError(
                f"Camera heights must be > 0 m for {cam_id!r}: input={in_m}, restored={rest_m}"
            )
        diff_cm = round((rest_m - in_m) * 100.0, 2)
        checks[cam_id] = {
            "input_height_m": round(in_m, 4),
            "restored_height_m": round(rest_m, 4),
            "difference_cm": diff_cm,
            "within_tolerance": bool(
                abs(rest_m - in_m) * 100.0 <= float(tolerance_cm) + 1e-9
            ),
        }
    return checks


def evaluate_object_cross_checks(
    calibration_doc: Mapping[str, Any],
    *,
    checker: ObjectScaleCrossChecker | None = None,
) -> list[dict[str, Any]]:
    """Run optional S15P31A101-462 static object (traffic cone / sign) scale cross-checks."""
    if checker is None:
        return []
    results: list[dict[str, Any]] = []
    for item in checker(calibration_doc):
        kind = str(item["kind"])
        if kind not in ("TRAFFIC_CONE", "OPENING_SIGN"):
            raise ValueError(f"Unsupported object_cross_check kind: {kind!r}")
        entry: dict[str, Any] = {
            "kind": kind,
            "passed": bool(item["passed"]),
        }
        if item.get("message"):
            entry["message"] = str(item["message"])[:500]
        for num_key in ("scale", "measured_m", "expected_m", "residual_cm", "floor_z_m"):
            if item.get(num_key) is not None:
                entry[num_key] = float(item[num_key])
        if item.get("cameras"):
            entry["cameras"] = [str(c) for c in item["cameras"]]
        results.append(entry)
    return results


def extract_tracks_from_backgrounds(
    backgrounds_by_cam: Mapping[str, Sequence[np.ndarray | Path | str]],
    calibration_doc: Mapping[str, Any] | Path | str,
    *,
    config: Mapping[str, Any] | None = None,
    matcher: Callable[[np.ndarray, np.ndarray], Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Extract and geometrically filter background feature tracks from clean backgrounds.

    Fits the Essential matrix on the training split ONLY (so holdout observations
    never steer geometric filtering) and applies keypoint quarantine across pairs.
    """
    cfg = {**DEFAULT_STAGE_A_CONFIG, **(dict(config) if config is not None else {})}
    _, cameras, K, R, C, cam_ids = read_calibration(calibration_doc)
    pairs = (
        PAIRS
        if len(cam_ids) == 4
        else tuple((k, (k + 1) % len(cam_ids)) for k in range(len(cam_ids)))
    )
    loaded: dict[str, list[np.ndarray]] = {}
    for idx, cid in enumerate(cam_ids):
        frames = backgrounds_by_cam.get(cid, ())
        if not frames:
            raise StageAError(
                "BACKGROUND_INCOMPLETE",
                f"No clean background images found for camera {cid!r}.",
                retryable=False,
            )
        imgs: list[np.ndarray] = []
        for f in frames:
            if isinstance(f, (Path, str)):
                p = Path(f)
                if not p.is_file():
                    raise StageAError(
                        "BACKGROUND_INCOMPLETE",
                        f"Could not read background image {f!s} for camera {cid!r}.",
                        retryable=False,
                    )
                from PIL import Image

                with Image.open(p) as pil_im:
                    im = np.asarray(pil_im.convert("RGB"))
            else:
                im = np.asarray(f)
            if im.ndim < 2 or im.size == 0:
                raise StageAError(
                    "BACKGROUND_INCOMPLETE",
                    f"Empty background image for camera {cid!r}.",
                    retryable=False,
                )
            imgs.append(im)
        loaded[cid] = imgs

    def _default_matcher(_img_a: np.ndarray, _img_b: np.ndarray) -> list[dict[str, Any]]:
        return []

    match_fn = matcher if matcher is not None else _default_matcher
    windows = max(1, min(len(v) for v in loaded.values()))
    merge_radius_px = float(cfg.get("multiwindow_merge_radius_px", 2.0))

    tracks: list[dict[str, Any]] = []
    pairs_out: dict[str, Any] = {}
    for pair_index, (a, b) in enumerate(pairs):
        cid_a, cid_b = cam_ids[a], cam_ids[b]
        raw_candidates: list[dict[str, Any]] = []
        for w in range(windows):
            offset = (pair_index * windows + w) * 1_000_000
            for idx_m, cand in enumerate(match_fn(loaded[cid_a][w], loaded[cid_b][w])):
                nodes = cand.get("nodes", [[a, idx_m], [b, idx_m]])
                raw_candidates.append(
                    {
                        "uv": [list(cand["uv"][0]), list(cand["uv"][1])],
                        "nodes": [
                            [a, offset + int(nodes[0][1])],
                            [b, offset + int(nodes[1][1])],
                        ],
                    }
                )
        deduped = (
            deduplicate_multiwindow_matches(raw_candidates, merge_radius_px)
            if windows > 1
            else raw_candidates
        )
        if len(deduped) < 8:
            raise StageAError(
                "INSUFFICIENT_FEATURES",
                f"Insufficient raw feature matches on pair {cid_a}-{cid_b}: {len(deduped)}",
                retryable=False,
            )
        uv = np.array([c["uv"] for c in deduped], dtype=float)
        # 매칭은 원본 영상에서 수행하고, 이후의 모든 기하는 핀홀 픽셀을 쓴다.
        for side, camera_index in enumerate((a, b)):
            uv[:, side] = _undistort_pixels(uv[:, side], K[camera_index], cameras[camera_index]["distortion"])
        fit, hold = spatial_split(
            uv[:, 0].tolist(),
            cameras[a]["image_size"],
            cfg["grid"],
            cfg["holdout_fraction"],
            cfg["seed"],
        )
        if len(fit) < 8:
            raise StageAError(
                "INSUFFICIENT_FEATURES",
                f"Insufficient training matches on pair {cid_a}-{cid_b}: {len(fit)}",
                retryable=False,
            )
        na = _normalize_pixels(uv[:, 0], K[a])
        nb = _normalize_pixels(uv[:, 1], K[b])
        threshold = cfg["essential_threshold_px"] / np.mean([K[a, 0, 0], K[b, 0, 0]])
        E = _fit_essential_ransac(
            na[fit], nb[fit], threshold, seed=int(cfg.get("seed", 449))
        )
        if E is None or E.shape != (3, 3):
            raise StageAError(
                "INSUFFICIENT_FEATURES",
                f"Essential matrix fit failed on pair {cid_a}-{cid_b}.",
                retryable=False,
            )
        valid = sampson(E, na, nb) <= threshold
        valid &= initial_valid(uv, (a, b), K, R, C, cfg["min_angle_deg"])
        hold_set = set(hold)
        for i in np.flatnonzero(valid):
            tracks.append(
                {
                    "id": f"{cid_a}-{cid_b}:{int(i)}",
                    "pair": [a, b],
                    "uv": uv[int(i)].tolist(),
                    "nodes": deduped[int(i)]["nodes"],
                    "split": "holdout" if int(i) in hold_set else "train",
                }
            )
        pairs_out[f"{cid_a}-{cid_b}"] = {
            "raw": len(deduped),
            "raw_before_dedup": len(raw_candidates),
            "raw_train": len(fit),
            "raw_holdout": len(hold),
            "geometric_kept": int(valid.sum()),
            "essential": E.tolist(),
        }

    before_q = len(tracks)
    tracks = quarantine_shared_keypoints(tracks, merge_radius_px=merge_radius_px)
    for a, b in pairs:
        key = f"{cam_ids[a]}-{cam_ids[b]}"
        rec = pairs_out[key]
        for split in ("train", "holdout"):
            rec[split] = sum(t["pair"] == [a, b] and t["split"] == split for t in tracks)
        if rec["train"] < cfg["min_train_per_pair"]:
            raise StageAError(
                "INSUFFICIENT_FEATURES",
                f"Insufficient training tracks after quarantine on {key}: {rec['train']}",
                retryable=False,
            )
        rec["holdout_insufficient"] = rec["holdout"] < cfg["min_holdout_per_pair"]

    return {
        "tracks": tracks,
        "backgrounds": {cid: {"windows": len(loaded[cid])} for cid in cam_ids},
        "pairs": pairs_out,
        "quarantined_train_tracks": before_q - len(tracks),
    }


def _derive_new_calibration_version(
    base_version: str,
    R: np.ndarray,
    cameras: Sequence[str],
    content_digest: str | None = None,
) -> str:
    """새 보정 버전 = 이전 버전 + 회전 + (있으면) 문서 내용 해시(content_hash). 내용을 넣으면 같은 버전이 곧 같은 보정 내용이다.

    재시도 세션이 같은 버전에 다른 내용(중심·재투영 지표 등)을 올려 서버가 거절하는 일을 막는다(S15P31A101-473)."""
    digest = hashlib.sha256()
    digest.update(base_version.encode("utf-8"))
    for idx, cid in enumerate(cameras):
        digest.update(cid.encode("utf-8"))
        digest.update(np.asarray(R[idx], dtype=np.float64).round(8).tobytes())
    if content_digest is not None:
        digest.update(content_digest.encode("utf-8"))
    short_hash = digest.hexdigest()[:8]
    return f"{base_version}-jba2-{short_hash}"[:160]


def _select_anchor_camera(
    tracks: Sequence[Mapping[str, Any]],
    K: np.ndarray,
    R0: np.ndarray,
    C0: np.ndarray,
    cameras: Sequence[str],
    pairs: Sequence[tuple[int, int]],
    *,
    anchor_camera_id: str | None = None,
    v0_results: Mapping[str, Any] | Path | str | None = None,
) -> tuple[int, str]:
    if anchor_camera_id is not None:
        if anchor_camera_id not in cameras:
            raise ValueError(f"Unknown anchor_camera_id {anchor_camera_id!r}")
        return list(cameras).index(anchor_camera_id), anchor_camera_id
    if v0_results is not None:
        v0_doc = (
            json.loads(Path(v0_results).read_text(encoding="utf-8"))
            if isinstance(v0_results, (Path, str))
            else dict(v0_results)
        )
        name = select_anchor(v0_doc, cameras=cameras)
        return list(cameras).index(name), name
    all_eval = evaluate(
        tracks, K, R0, C0, cameras=cameras, pairs=pairs, split="all"
    )
    best_idx = min(
        range(len(cameras)),
        key=lambda i: (
            all_eval["cameras"][cameras[i]]["median"]
            if all_eval["cameras"][cameras[i]]["median"] is not None
            else float("inf"),
            i,
        ),
    )
    return best_idx, cameras[best_idx]


def run_stage_a_rotation_ba(
    initial_calibration: Mapping[str, Any] | Path | str,
    tracks_source: Mapping[str, Any] | Path | str,
    *,
    camera_heights_m: Mapping[str, float] | None = None,
    height_tolerance_cm: float = 5.0,
    anchor_camera_id: str | None = None,
    v0_results: Mapping[str, Any] | Path | str | None = None,
    new_calibration_version: str | None = None,
    created_at_ms: int | None = None,
    config: Mapping[str, Any] | None = None,
    stage_target_seconds: float | None = None,
    object_cross_checker: ObjectScaleCrossChecker | None = None,
    backgrounds_by_cam: Mapping[str, Sequence[np.ndarray | Path | str] | np.ndarray | Path | str] | None = None,
    cone_observation: TriangulatedCone | None = None,
    disagreement_tolerance: float = 0.05,
    record_scale_estimation: bool = False,
) -> StageAResult:
    """Run V2 Stage A rotation-only Projected Joint BA (`center_sigma=None`).

    Produces a new immutable CalibrationProvider document with a new
    ``calibration_version`` and recomputed ``content_hash`` without mutating
    ``initial_calibration``.
    """
    stage_started = time.perf_counter()
    cfg = {**DEFAULT_STAGE_A_CONFIG, **(dict(config) if config is not None else {})}
    tolerance_px = float(cfg["holdout_worsening_tolerance_px"])
    if not math.isfinite(tolerance_px) or tolerance_px < 0:
        raise ValueError("holdout_worsening_tolerance_px must be finite and nonnegative")
    if stage_target_seconds is not None:
        cfg["stage_target_seconds"] = float(stage_target_seconds)

    try:
        doc_in, _, K, R0, C0, cam_ids = read_calibration(initial_calibration)
    except Exception as exc:
        raise StageAError(
            "INSUFFICIENT_FEATURES",
            f"Invalid initial calibration for Stage A Joint BA: {exc}",
            retryable=False,
        ) from exc

    pairs = (
        PAIRS
        if len(cam_ids) == 4
        else tuple((k, (k + 1) % len(cam_ids)) for k in range(len(cam_ids)))
    )
    restored_heights = {cam_ids[i]: float(C0[i, 2]) for i in range(len(cam_ids))}
    heights_m = (
        {cid: float(camera_heights_m[cid]) for cid in cam_ids}
        if camera_heights_m is not None
        else dict(restored_heights)
    )
    cfg["heights_m"] = heights_m

    try:
        tracks, metadata = load_tracks(
            tracks_source,
            cfg,
            cameras=cam_ids,
            pairs=pairs,
        )
    except Exception as exc:
        raise StageAError(
            "INSUFFICIENT_FEATURES",
            f"Insufficient or invalid background feature tracks: {exc}",
            retryable=False,
        ) from exc

    train = [t for t in tracks if t["split"] == "train"]
    if len(train) < len(pairs) * int(cfg["min_train_per_pair"]):
        raise StageAError(
            "INSUFFICIENT_FEATURES",
            f"Insufficient total training tracks: {len(train)}",
            retryable=False,
        )

    anchor_idx, anchor_name = _select_anchor_camera(
        tracks,
        K,
        R0,
        C0,
        cam_ids,
        pairs,
        anchor_camera_id=anchor_camera_id,
        v0_results=v0_results,
    )

    baseline = evaluate(tracks, K, R0, C0, cameras=cam_ids, pairs=pairs)
    problem = ProjectedProblem(
        train,
        K,
        R0,
        C0,
        anchor_idx,
        cfg,
        None,
        cameras=cam_ids,
        pairs=pairs,
    )
    R_a, C_a, _, info = problem.solve(R0, C0)
    held = evaluate(tracks, K, R_a, C_a, cameras=cam_ids, pairs=pairs)
    all_after = evaluate(tracks, K, R_a, C_a, cameras=cam_ids, pairs=pairs, split="all")
    poses_diff = pose_change(R_a, C_a, R0, C0, heights_m, cameras=cam_ids)

    stage_seconds = float(time.perf_counter() - stage_started)
    within_time = stage_seconds <= float(cfg["stage_target_seconds"])
    training_invalid_depths = int(
        np.sum(problem.pixel_residual(problem.pack(R_a, C_a))[1] <= 0)
    )

    worsened_Evaluated_pairs: list[str] = []
    pair_metrics: dict[str, Any] = {}
    pair_holdout_medians: dict[str, float | None] = {}
    for a, b in pairs:
        key = f"{cam_ids[a]}-{cam_ids[b]}"
        pair_meta = metadata["pairs"][key]
        insuff = bool(pair_meta.get("holdout_insufficient", False))
        is_worse = worsened(baseline["pairs"][key], held["pairs"][key], tolerance_px)
        if not insuff and is_worse:
            worsened_Evaluated_pairs.append(key)
        med = held["pairs"][key]["median"]
        pair_holdout_medians[key] = round(float(med), 4) if med is not None else None
        pair_metrics[key] = {
            **pair_meta,
            "before_holdout": baseline["pairs"][key],
            "after_holdout": held["pairs"][key],
            "worsened": is_worse,
        }

    max_center_shift_cm = max(
        float(poses_diff[cid]["center_cm"]) for cid in cam_ids
    )
    converged = bool(info["success"]) and not bool(info["adoption_forbidden"])
    if (
        not converged
        or training_invalid_depths > 0
        or not within_time
        or len(worsened_Evaluated_pairs) > 0
        or max_center_shift_cm > 1e-7
    ):
        raise StageAError(
            "VALIDATION_REJECTED",
            (
                f"Stage A rotation BA rejected by validation gate: "
                f"converged={converged}, blockers={info['adoption_blockers']}, "
                f"invalid_depths={training_invalid_depths}, "
                f"stage_seconds={stage_seconds:.3f}s (limit {cfg['stage_target_seconds']}s), "
                f"worsened_pairs={worsened_Evaluated_pairs}, "
                f"max_center_shift_cm={max_center_shift_cm:.2e}"
            ),
            retryable=False,
        )

    # Construct immutable new CalibrationProvider document (never overwriting input).
    new_doc = copy.deepcopy(doc_in)
    base_version = str(doc_in["calibration_version"])
    # 버전은 내용(content_hash, 버전·생성 시각 제외)을 채운 뒤 정한다. 아래 자리값은 content_hash 에 들어가지 않는다
    new_doc["calibration_version"] = base_version
    if created_at_ms is not None:
        new_doc["created_at"] = int(created_at_ms)

    cam_idx_by_id = {cid: idx for idx, cid in enumerate(cam_ids)}
    cone = cone_observation
    if cone is None and backgrounds_by_cam:
        rot_doc = copy.deepcopy(doc_in)
        for cam_entry in rot_doc["cameras"]:
            cid = str(cam_entry["camera_id"])
            idx = cam_idx_by_id[cid]
            R_new = R_a[idx]
            t_new = -R_new @ C0[idx]
            cam_entry["R"] = [[float(v) for v in row] for row in R_new]
            cam_entry["t"] = [float(v) for v in t_new]
        cone, _ = detect_and_triangulate_cone_from_images(rot_doc, backgrounds_by_cam)

    scale_est = estimate_joint_scale_and_floor(
        restored_heights,
        heights_m,
        cone=cone,
        disagreement_tolerance=disagreement_tolerance,
        height_tolerance_cm=height_tolerance_cm,
    )
    if cone is not None:
        s_applied = float(scale_est["applied_scale"])
        dz_applied = float(scale_est["floor_offset_m"])
        C_final = s_applied * C_a - np.array([0.0, 0.0, dz_applied], dtype=float)
        restored_heights = {cam_ids[i]: float(C_final[i, 2]) for i in range(len(cam_ids))}
        poses_diff = pose_change(R_a, C_final, R0, C0, heights_m, cameras=cam_ids)
    else:
        C_final = C0

    for cam_entry in new_doc["cameras"]:
        cid = str(cam_entry["camera_id"])
        idx = cam_idx_by_id[cid]
        R_new = R_a[idx]
        t_new = -R_new @ C_final[idx]
        cam_entry["R"] = [[float(v) for v in row] for row in R_new]
        cam_entry["t"] = [float(v) for v in t_new]
        cam_entry["camera_height_m"] = round(float(C_final[idx, 2]), 4)
        cam_stat = held["cameras"][cid]
        if cam_stat["median"] is None:
            cam_stat = all_after["cameras"][cid]
        if cam_stat["median"] is not None and cam_stat["p95"] is not None:
            cam_entry["reprojection_error_px"] = {
                "median": round(float(cam_stat["median"]), 4),
                "p95": round(float(cam_stat["p95"]), 4),
            }

    if cone is not None or record_scale_estimation:
        new_doc["scale_source"] = str(scale_est["scale_source"])
        new_doc["scale_estimation"] = scale_est
        if scale_est["scale_source"] == "JOINT":
            cross_basis = [str(x) for x in new_doc.get("scale_cross_check_basis", [])]
            if "MEASURED_OBJECT" not in cross_basis:
                cross_basis.append("MEASURED_OBJECT")
            new_doc["scale_cross_check_basis"] = cross_basis

    new_doc["content_hash"] = content_hash(new_doc)
    resolved_version = (
        new_calibration_version
        if new_calibration_version is not None and new_calibration_version != base_version
        else _derive_new_calibration_version(base_version, R_a, cam_ids, new_doc["content_hash"])
    )
    new_doc["calibration_version"] = resolved_version
    errs = schema_errors(new_doc, "calibration")
    if errs:
        raise StageAError(
            "VALIDATION_REJECTED",
            f"Refined calibration document failed schema validation: {errs}",
            retryable=False,
        )

    height_checks = compute_height_checks(
        heights_m,
        restored_heights,
        tolerance_cm=height_tolerance_cm,
    )
    object_checks = build_object_cross_checks_from_cone(cone, scale_est)
    object_checks.extend(
        evaluate_object_cross_checks(
            new_doc,
            checker=object_cross_checker,
        )
    )
    summary_metrics: dict[str, Any] = {
        "anchor_camera_id": anchor_name,
        "converged": converged,
        "stage_seconds": round(stage_seconds, 4),
        "valid_points": len(tracks),
        "pair_holdout_median_px": pair_holdout_medians,
        "holdout_worsening_tolerance_px": tolerance_px,
        "pair_holdout": {key: {"before": value["before_holdout"], "after": value["after_holdout"]}
                         for key, value in pair_metrics.items()},
    }
    if cone is not None or record_scale_estimation:
        summary_metrics["scale_source"] = str(scale_est["scale_source"])
        summary_metrics["scale_estimation"] = scale_est
    return StageAResult(
        calibration_doc=new_doc,
        calibration_version=resolved_version,
        anchor_camera_id=anchor_name,
        stage_seconds=stage_seconds,
        converged=converged,
        valid_points=len(tracks),
        before_holdout=baseline,
        after_holdout=held,
        pair_metrics=pair_metrics,
        pose_changes=poses_diff,
        height_checks=height_checks,
        object_cross_checks=object_checks,
        solver_info=info,
        summary_metrics=summary_metrics,
        scale_source=str(scale_est["scale_source"]),
        scale_estimation=scale_est,
        cone_observation=cone,
    )
