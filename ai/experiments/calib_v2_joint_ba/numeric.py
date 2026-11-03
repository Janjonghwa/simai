"""Offline two-view joint BA. Never imports or updates production calibration state."""

from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
import scipy
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix
from scipy.spatial.transform import Rotation
from simai_ai.geometry.rig import RigCamera
from simai_ai.geometry.triangulation import triangulate_normalized_many

from control import (
    CAMS, PAIRS, deduplicate_multiwindow_matches, quarantine_shared_keypoints,
    select_anchor, spatial_split, statistics, worsened,
)


def read_calibration(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc["extrinsic_convention"] != "OPENCV_WORLD_TO_CAMERA":
        raise ValueError("Expected OPENCV_WORLD_TO_CAMERA")
    by_id = {c["camera_id"]: c for c in doc["cameras"]}
    cameras = [by_id[c] for c in CAMS]
    if any(c["distortion"]["model"] != "OPENCV_PINHOLE" for c in cameras):
        raise ValueError("Only fixed OpenCV pinhole distortion is supported")
    K = np.array([c["K"] for c in cameras], dtype=float)
    R = np.array([c["R"] for c in cameras], dtype=float)
    t = np.array([c["t"] for c in cameras], dtype=float)
    C = -np.einsum("nji,nj->ni", R, t)
    for rotation in R:
        if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-5):
            raise ValueError("Nonorthogonal rotation")
        if np.linalg.det(rotation) < 0:
            raise ValueError("Reflected rotation")
    return doc, cameras, K, R, C


def median_backgrounds(video, samples, chunk_rows, windows=1):
    """Two streaming passes; only selected frames retained, median in row chunks.

    32 frames at 2304x1296x3 take ~273 MiB; no full float64 frame stack.
    One camera at a time, no decoded recording is retained.
    When windows > 1, splits selected frames into contiguous time windows.
    """
    import av

    with av.open(str(video), format="h264") as container:
        total = sum(1 for _ in container.decode(video=0))
    if not total:
        raise ValueError(f"Empty recording: {video}")
    indices = sorted(set(np.linspace(0, total - 1, min(samples, total)).astype(int).tolist()))
    selected = set(indices)
    picked = []
    with av.open(str(video), format="h264") as container:
        for i, frame in enumerate(container.decode(video=0)):
            if i in selected:
                picked.append(frame.to_ndarray(format="bgr24"))
    if len(picked) != len(indices):
        raise ValueError("Decode changed between background passes")
    groups = [list(g) for g in np.array_split(np.arange(len(picked)), max(1, int(windows))) if len(g)]
    results = []
    for g in groups:
        result = np.empty_like(picked[0])
        for row in range(0, result.shape[0], chunk_rows):
            block = np.stack([picked[k][row:row + chunk_rows] for k in g])
            result[row:row + chunk_rows] = np.median(block, axis=0).astype(np.uint8)
        results.append(result)
    meta = {"decoded_frames": total, "selected_indices": indices,
            "background_windows": len(results),
            "window_indices": [[indices[k] for k in g] for g in groups],
            "retained_frame_bytes": sum(f.nbytes for f in picked)}
    return results, meta


def median_background(video, samples, chunk_rows):
    results, meta = median_backgrounds(video, samples, chunk_rows, windows=1)
    return results[0], meta


def triangulate(uv, pair, K, R, C):
    # Reuse Tier 0 geometry's public batch DLT. The images have already been undistorted.
    rigs = [RigCamera.from_calibration({"camera_id": CAMS[c], "K": K[c].tolist(),
            "R": R[c].tolist(), "t": (-R[c] @ C[c]).tolist(), "image_size": [2304, 1296],
            "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [0.0] * 5}}) for c in pair]
    normalized = np.stack([cv2.undistortPoints(uv[:, s, None], K[c], None)[:, 0]
                           for s, c in enumerate(pair)], axis=1)
    return triangulate_normalized_many(rigs, normalized, refine_iterations=0, targets=uv)


def project(points, camera, K, R, C):
    xc = (points - C[camera]) @ R[camera].T
    depth = xc[:, 2]
    safe = np.where(np.abs(depth) < 1e-8, np.where(depth < 0, -1e-8, 1e-8), depth)
    pixel = xc @ K[camera].T
    return pixel[:, :2] / safe[:, None], depth


def initial_valid(uv, pair, K, R, C, angle):
    xyz = triangulate(uv, pair, K, R, C)
    rays = [xyz - C[c] for c in pair]
    with np.errstate(divide="ignore", invalid="ignore"):
        cosine = np.sum(rays[0] * rays[1], axis=1) / (
            np.linalg.norm(rays[0], axis=1) * np.linalg.norm(rays[1], axis=1))
        angles = np.degrees(np.arccos(np.clip(cosine, -1, 1)))
    keep = np.isfinite(xyz).all(axis=1) & (angles >= angle)
    for c in pair:
        keep &= project(xyz, c, K, R, C)[1] > 0
    return keep


def sampson(E, na, nb):
    xa = np.column_stack((na, np.ones(len(na))))
    xb = np.column_stack((nb, np.ones(len(nb))))
    exa, etxb = xa @ E.T, xb @ E
    return np.abs(np.sum(xb * exa, axis=1)) / np.sqrt(
        np.sum(exa[:, :2] ** 2 + etxb[:, :2] ** 2, axis=1).clip(1e-20))


def build_tracks(config, cameras, K, R, C, out):
    import torch
    from lightglue import ALIKED, LightGlue
    from lightglue.utils import rbd

    device = config["device"]
    torch.set_num_threads(config["threads"])
    cv2.setNumThreads(config["threads"])
    torch.manual_seed(config["seed"])
    cv2.setRNGSeed(config["seed"])
    extractor = ALIKED(max_num_keypoints=config["max_keypoints"]).eval().to(device)
    matcher = LightGlue(features="aliked").eval().to(device)
    windows = int(config.get("background_windows", 1))
    merge_radius_px = float(config.get("multiwindow_merge_radius_px", 2.0)) if windows > 1 else 0.0
    features, keypoints, backgrounds = [], [], {}
    scale = config["feature_scale"]
    for c, cid in enumerate(CAMS):
        started = time.perf_counter()
        bg_list, meta = median_backgrounds(
            Path(config["recording"]) / cid / "epoch_0000.h264",
            config["background_samples"], config["median_chunk_rows"], windows=windows)
        cam_features, cam_keypoints = [], []
        for w, background in enumerate(bg_list):
            if tuple(background.shape[1::-1]) != tuple(cameras[c]["image_size"]):
                raise ValueError(f"Resolution mismatch: {cid}")
            image = cv2.undistort(background, K[c], np.array(cameras[c]["distortion"]["coeffs"]))
            suffix = f"background_{cid}.png" if w == 0 else f"background_{cid}_w{w}.png"
            if not cv2.imwrite(str(out / suffix), image):
                raise OSError("Cannot save background")
            small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            tensor = torch.from_numpy(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)).permute(2, 0, 1)
            with torch.inference_mode():
                feature = extractor.extract(tensor.float().div(255).to(device), resize=None)
            cam_keypoints.append(rbd(feature)["keypoints"].cpu().numpy() / scale)
            cam_features.append({k: v.cpu() for k, v in feature.items()})
        features.append(cam_features)
        keypoints.append(cam_keypoints)
        meta["seconds"] = time.perf_counter() - started
        backgrounds[cid] = meta
    tracks, pairs = [], {}
    for a, b in PAIRS:
        started = time.perf_counter()
        raw_candidates = []
        for w in range(windows):
            fa = {k: v.to(device) for k, v in features[a][w].items()}
            fb = {k: v.to(device) for k, v in features[b][w].items()}
            with torch.inference_mode():
                matches = rbd(matcher({"image0": fa, "image1": fb}))["matches"].cpu().numpy()
            kpa, kpb = keypoints[a][w], keypoints[b][w]
            offset = w * 1_000_000
            for m0, m1 in matches:
                raw_candidates.append({
                    "uv": [kpa[int(m0)].tolist(), kpb[int(m1)].tolist()],
                    "nodes": [[a, offset + int(m0)], [b, offset + int(m1)]],
                })
        deduped = deduplicate_multiwindow_matches(raw_candidates, merge_radius_px) if windows > 1 else raw_candidates
        uv = np.array([c["uv"] for c in deduped], dtype=float)
        fit, hold = spatial_split(uv[:, 0].tolist(), cameras[a]["image_size"],
                                  config["grid"], config["holdout_fraction"], config["seed"])
        # Fit the E model ONLY on train. The holdout cannot steer geometric filtering.
        if len(fit) < 8:
            raise ValueError(f"Insufficient raw fit matches: {CAMS[a]}-{CAMS[b]}")
        na = cv2.undistortPoints(uv[:, 0, None], K[a], None)[:, 0]
        nb = cv2.undistortPoints(uv[:, 1, None], K[b], None)[:, 0]
        threshold = config["essential_threshold_px"] / np.mean([K[a, 0, 0], K[b, 0, 0]])
        E, mask = cv2.findEssentialMat(na[fit], nb[fit], np.eye(3), cv2.RANSAC, 0.999, threshold)
        if E is None or E.shape != (3, 3):
            raise ValueError(f"Essential fit failed: {CAMS[a]}-{CAMS[b]}")
        valid = (sampson(E, na, nb) <= threshold)
        valid &= initial_valid(uv, (a, b), K, R, C, config["min_angle_deg"])
        hold_set = set(hold)
        for i in np.flatnonzero(valid):
            tracks.append({"id": f"{CAMS[a]}-{CAMS[b]}:{int(i)}", "pair": [a, b],
                           "uv": uv[i].tolist(), "nodes": deduped[int(i)]["nodes"],
                           "split": "holdout" if i in hold_set else "train"})
        pairs[f"{CAMS[a]}-{CAMS[b]}"] = {
            "raw": len(deduped), "raw_before_dedup": len(raw_candidates),
            "raw_train": len(fit), "raw_holdout": len(hold),
            "geometric_kept": int(valid.sum()), "seconds": time.perf_counter() - started,
            "essential": E.tolist()}
    before = len(tracks)
    tracks = quarantine_shared_keypoints(tracks, merge_radius_px=merge_radius_px)
    for a, b in PAIRS:
        rec = pairs[f"{CAMS[a]}-{CAMS[b]}"]
        for split in ("train", "holdout"):
            rec[split] = sum(t["pair"] == [a, b] and t["split"] == split for t in tracks)
        if rec["train"] < config["min_train_per_pair"]:
            raise ValueError(f"Insufficient split after verification/quarantine: {CAMS[a]}-{CAMS[b]} {rec}")
        # 약한 쌍(11차 로비 C-D): 학습 점은 BA 에 쓰되 홀드아웃이 모자라면 그 쌍의 홀드아웃 지표는 판단에 쓰지 않는다.
        rec["holdout_insufficient"] = rec["holdout"] < config["min_holdout_per_pair"]
    metadata = {"backgrounds": backgrounds, "pairs": pairs,
                "quarantined_train_tracks": before - len(tracks)}
    (out / "tracks.json").write_text(json.dumps({"tracks": tracks, **metadata}, indent=2), encoding="utf-8")
    return tracks, metadata


def load_tracks(path, config):
    """Replay fixed observations/splits without decoding or fitting another E model."""
    source = Path(path).resolve()
    saved = json.loads(source.read_text(encoding="utf-8"))
    tracks = saved["tracks"]
    if not tracks or len({t["id"] for t in tracks}) != len(tracks):
        raise ValueError("Empty tracks or duplicate track IDs")
    for track in tracks:
        uv = np.asarray(track["uv"], dtype=float)
        if tuple(track["pair"]) not in PAIRS or track["split"] not in ("train", "holdout"):
            raise ValueError(f"Invalid track pair/split: {track['id']}")
        if uv.shape != (2, 2) or not np.isfinite(uv).all():
            raise ValueError(f"Invalid track pixels: {track['id']}")
    merge_radius_px = float(config.get("multiwindow_merge_radius_px", 2.0)) if int(config.get("background_windows", 1)) > 1 else 0.0
    if len(quarantine_shared_keypoints(tracks, merge_radius_px=merge_radius_px)) != len(tracks):
        raise ValueError("Saved holdout keypoint re-enters training")
    pairs = {}
    for a, b in PAIRS:
        key = f"{CAMS[a]}-{CAMS[b]}"
        rec = dict(saved.get("pairs", {}).get(key, {}))
        for split in ("train", "holdout"):
            count = sum(t["pair"] == [a, b] and t["split"] == split for t in tracks)
            if split in rec and rec[split] != count:
                raise ValueError(f"Saved track count mismatch: {key}/{split}")
            rec[split] = count
        if rec["train"] < config["min_train_per_pair"]:
            raise ValueError(f"Insufficient saved training tracks: {key}")
        # Keep the Lead's weak-pair policy: train remains usable; report low holdout n.
        rec["holdout_insufficient"] = rec["holdout"] < config["min_holdout_per_pair"]
        pairs[key] = rec
    return tracks, {"backgrounds": saved.get("backgrounds", {}), "pairs": pairs,
                    "quarantined_train_tracks": saved.get("quarantined_train_tracks"),
                    "from_tracks": str(source), "feature_extraction_reused": True}


class Problem:
    """Small camera block plus independent 2-view points; fixed anchor R and C."""

    solver = "joint"

    def __init__(self, tracks, K, R0, C0, anchor, config, center_sigma):
        self.tracks, self.K, self.R0, self.C0 = tracks, K, R0, C0
        self.free = [i for i in range(4) if i != anchor]
        self.cfg, self.sigma = config, center_sigma
        self.dof = 3 if center_sigma is None else 6
        self.nc = len(self.free) * self.dof
        self.uv = np.array([t["uv"] for t in tracks])
        self.pair = np.array([t["pair"] for t in tracks])
        self.floor = [i for i, t in enumerate(tracks) if t["id"] in config["confirmed_floor_tracks"]]
        self.pair_ids = {pair: np.flatnonzero(np.all(self.pair == pair, axis=1)) for pair in PAIRS}

    def camera_poses(self, x):
        R, C = self.R0.copy(), self.C0.copy()
        for k, c in enumerate(self.free):
            offset = k * self.dof
            R[c] = Rotation.from_rotvec(x[offset:offset + 3]).as_matrix() @ self.R0[c]
            if self.dof == 6:
                C[c] += x[offset + 3:offset + 6]
        return R, C

    def unpack(self, x):
        R, C = self.camera_poses(x)
        return R, C, x[self.nc:].reshape(-1, 3)

    def pack_cameras(self, R, C):
        x = np.zeros(self.nc)
        for k, c in enumerate(self.free):
            offset = k * self.dof
            rotvec = Rotation.from_matrix(R[c] @ self.R0[c].T).as_rotvec()
            x[offset:offset + 3] = 0.0 if np.linalg.norm(rotvec) < 1e-12 else rotvec
            if self.dof == 6:
                delta = C[c] - self.C0[c]
                x[offset + 3:offset + 6] = 0.0 if np.linalg.norm(delta) < 1e-12 else delta
        return x

    def points_at_poses(self, R, C):
        xyz = np.empty((len(self.tracks), 3))
        for pair, ids in self.pair_ids.items():
            if len(ids):
                xyz[ids] = triangulate(self.uv[ids], pair, self.K, R, C)
        if not np.isfinite(xyz).all():
            raise ValueError("Nonfinite training triangulation; candidate rejected")
        return xyz

    def pack(self, R, C):
        x = np.concatenate((self.pack_cameras(R, C), self.points_at_poses(R, C).ravel()))
        return x

    def pixel_residual(self, x):
        R, C, xyz = self.unpack(x)
        residual, depth, _ = self.point_pixels(xyz, R, C)
        return residual.ravel(), depth

    def point_pixels(self, xyz, R, C):
        """All two-view projections and analytic point Jacobians, in track order."""
        rotations, intrinsics = R[self.pair], self.K[self.pair]
        xc = np.einsum("nsij,nsj->nsi", rotations, xyz[:, None] - C[self.pair])
        depth = xc[:, :, 2]
        safe = np.where(np.abs(depth) < 1e-8, np.where(depth < 0, -1e-8, 1e-8), depth)
        pixel = np.einsum("nsij,nsj->nsi", intrinsics, xc)
        predicted = pixel[:, :, :2] / safe[:, :, None]
        kr = np.einsum("nsij,nsjk->nsik", intrinsics, rotations)
        # The denominator clamp is locally constant in its tiny guard interval.
        dz = rotations[:, :, 2] * (np.abs(depth) >= 1e-8)[:, :, None]
        jac = (kr[:, :, :2] - predicted[:, :, :, None] * dz[:, :, None]) / safe[:, :, None, None]
        return predicted - self.uv, depth, jac

    def residual(self, x):
        pix, depth = self.pixel_residual(x)
        R, C, xyz = self.unpack(x)
        prior = []
        for c in self.free:
            prior.extend(Rotation.from_matrix(R[c] @ self.R0[c].T).as_rotvec()
                         / np.radians(self.cfg["rotation_sigma_deg"]))
            if self.sigma is not None:
                prior.extend((C[c] - self.C0[c]) / self.sigma)
        # Includes constant anchor/Stage-A height residuals; they cannot move a fixed camera.
        prior.extend((C[:, 2] - np.array([self.cfg["heights_m"][c] for c in CAMS]))
                     / self.cfg["height_sigma_m"])
        prior.extend(xyz[self.floor, 2] / 0.015)
        # Quadratic barrier, never robustified. A final behind-camera point fails validation.
        return np.concatenate((pix, np.array(prior), np.minimum(depth.ravel() - 0.01, 0) / 0.01))

    def loss(self, z):
        rho = np.vstack((z.copy(), np.ones_like(z), np.zeros_like(z)))
        ids = np.flatnonzero(z[:4 * len(self.tracks)] > self.cfg["huber_px"] ** 2)
        root = np.sqrt(z[ids])
        delta = self.cfg["huber_px"]
        rho[0, ids] = 2 * delta * root - delta ** 2
        rho[1, ids] = delta / root
        rho[2, ids] = -delta / (2 * z[ids] * root)
        return rho

    def height_prior_summary(self, x):
        _, C, _ = self.unpack(x)
        sigma = self.cfg["height_sigma_m"]
        return {"sigma_m": sigma, "loss": "quadratic", "hard_constraint": False,
                "cameras": {cid: {"residual_m": float(C[c, 2] - self.cfg["heights_m"][cid]),
                    "residual_sigma": float((C[c, 2] - self.cfg["heights_m"][cid]) / sigma),
                    "cost": float(0.5 * ((C[c, 2] - self.cfg["heights_m"][cid]) / sigma) ** 2),
                    "optimized": c in self.free and self.sigma is not None}
                    for c, cid in enumerate(CAMS)}}

    def solver_options(self):
        return {"jac_sparsity": self.sparsity()}

    def sparsity(self):
        n = len(self.tracks)
        # Camera block is tiny; allowing conservative prior dependencies is inexpensive.
        pattern = lil_matrix((len(self.residual(self.pack(self.R0, self.C0))), self.nc + 3 * n), dtype=int)
        depth_start = pattern.shape[0] - 2 * n
        for i, pair in enumerate(self.pair):
            for c in pair:
                if c in self.free:
                    k = self.free.index(c) * self.dof
                    pattern[4 * i:4 * i + 4, k:k + self.dof] = 1
                    pattern[depth_start + 2 * i:depth_start + 2 * i + 2, k:k + self.dof] = 1
            pattern[4 * i:4 * i + 4, self.nc + 3 * i:self.nc + 3 * i + 3] = 1
        pattern[4 * n:, :self.nc] = 1
        # Floor/depth rows are few and separated from feature rows.
        prior_rows = 3 * len(self.free) + (3 * len(self.free) if self.sigma is not None else 0) + 4
        for j, i in enumerate(self.floor):
            pattern[4 * n + prior_rows + j, self.nc + 3 * i + 2] = 1
        for i in range(n):
            pattern[depth_start + 2 * i:depth_start + 2 * i + 2,
                    self.nc + 3 * i:self.nc + 3 * i + 3] = 1
        return pattern.tocsr()

    def solve(self, start_R, start_C):
        started = time.perf_counter()
        x0 = self.pack(start_R, start_C)
        initial_cost = self.cost_summary(x0)
        # x_scale="jac" 는 Huber 손실과 함께 쓰면 카메라 열이 사실상 움직이지 않았다(2026-11-04 Lead 진단: 카메라 이동 1e-11,
        # ftol/xtol 조기 종료). 변수 종류별 크기(회전 rad·중심 m·점 m)를 직접 준다.
        scale = np.full_like(x0, self.cfg.get("x_scale_point_m", 0.05))
        for k in range(len(self.free)):
            scale[k * self.dof:k * self.dof + 3] = self.cfg.get("x_scale_rotation_rad", 0.005)
            if self.dof == 6:
                scale[k * self.dof + 3:k * self.dof + 6] = self.cfg.get("x_scale_center_m", 0.01)
        calls = 0
        def counted_residual(x):
            nonlocal calls
            calls += 1
            return self.residual(x)
        result = least_squares(counted_residual, x0, **self.solver_options(),
                               loss=self.loss, f_scale=1.0, x_scale=scale, method="trf",
                               max_nfev=self.cfg["max_nfev"], ftol=1e-8, xtol=1e-8, gtol=1e-8)
        R, C, _ = self.unpack(result.x)
        blockers = [] if result.success else ["solver_not_converged"]
        if np.any(self.pixel_residual(result.x)[1] <= 0):
            blockers.append("training_invalid_depths")
        projection = getattr(self, "projection_info", None)
        if projection is not None and not projection["converged"]:
            blockers.append("point_projection_not_converged")
        info = {"seconds": time.perf_counter() - started, "success": bool(result.success),
                              "solver": self.solver, "variables": len(x0), "residual_calls": calls,
                              "adoption_forbidden": bool(blockers), "adoption_blockers": blockers,
                              "message": result.message, "nfev": result.nfev, "njev": result.njev,
                              "status": int(result.status), "cost": float(result.cost),
                              "initial_cost": initial_cost, "final_cost": self.cost_summary(result.x),
                              "camera_parameter_step_norm": float(np.linalg.norm(result.x[:self.nc] - x0[:self.nc])),
                              "height_prior": self.height_prior_summary(result.x),
                              "optimality": float(result.optimality)}
        if projection is not None:
            info["point_projection"] = dict(projection)
        return R, C, result, info

    def cost_summary(self, x):
        """Use the same mixed Huber/quadratic objective as least_squares (half sum rho)."""
        residual = self.residual(x)
        rho = self.loss(residual ** 2)[0]
        pixel_end = 4 * len(self.tracks)
        depth_start = len(residual) - 2 * len(self.tracks)
        return {"total": float(0.5 * rho.sum()),
                "pixel_huber": float(0.5 * rho[:pixel_end].sum()),
                "quadratic_priors": float(0.5 * rho[pixel_end:depth_start].sum()),
                "depth_barrier": float(0.5 * rho[depth_start:].sum()),
                "pixel_rms_px": float(np.sqrt(np.mean(residual[:pixel_end] ** 2)))}

    def observability(self, x):
        """Raw data Jacobian vs prior-augmented Jacobian, eliminate independent points.

        Dimensionless columns: 0.01 rad rotation, 0.01 m centers and points.
        No robust weights or solver-modified Jacobian: priors must not hide data null modes.
        """
        base, depth = self.pixel_residual(x)
        n = len(self.tracks)
        camera_jac = np.empty((4 * n, self.nc))
        nonpixel_jac = np.empty((len(self.residual(x)) - 4 * n, self.nc))
        step = 1e-6
        for j in range(self.nc):
            xp, xm = x.copy(), x.copy()
            xp[j] += step
            xm[j] -= step
            camera_jac[:, j] = (self.pixel_residual(xp)[0] - self.pixel_residual(xm)[0]) / (2 * step) * 0.01
            nonpixel_jac[:, j] = (self.residual(xp)[4 * n:] - self.residual(xm)[4 * n:]) / (2 * step) * 0.01
        R, C, xyz = self.unpack(x)
        point_jac = self.point_pixels(xyz, R, C)[2].reshape(n, 4, 3) * 0.01
        prior_rows = 3 * len(self.free) + (3 * len(self.free) if self.sigma is not None else 0) + 4
        reduced, augmented = [], [nonpixel_jac[:prior_rows]]
        floor_order = {i: j for j, i in enumerate(self.floor)}
        for i in range(n):
            jp, jc = point_jac[i], camera_jac[4 * i:4 * i + 4]
            reduced.append(jc - jp @ np.linalg.pinv(jp) @ jc)
            # Floor and depth priors only couple to this point. Eliminate each 3-column
            # block separately, avoiding an O((3N)^3) dense least-squares diagnostic.
            jps, jcs = [jp], [jc]
            if i in floor_order:
                jps.append(np.array([[0., 0., 0.01 / 0.015]]))
                row = prior_rows + floor_order[i]
                jcs.append(nonpixel_jac[row:row + 1])
            jps.append(R[self.pair[i], 2] * (depth[i] < 0.01)[:, None])
            row = prior_rows + len(self.floor) + 2 * i
            jcs.append(nonpixel_jac[row:row + 2])
            jp, jc = np.vstack(jps), np.vstack(jcs)
            augmented.append(jc - jp @ np.linalg.pinv(jp) @ jc)
        reduced = np.vstack(reduced)
        augmented = np.vstack(augmented)
        def describe(matrix):
            singular = np.linalg.svd(matrix, compute_uv=False)
            return {"singular_values": singular.tolist(), "smallest": float(singular[-1]),
                    "smallest_relative": float(singular[-1] / singular[0]) if singular[0] else 0.0,
                    "weak_modes": int(np.sum(singular <= singular[0] * self.cfg["svd_relative_threshold"]))}
        return {"data_only_schur": describe(reduced), "with_quadratic_priors_schur": describe(augmented),
                "column_units": "0.01 rad rotations; 0.01 m centers/points", "pixel_rows": len(base)}


class ProjectedProblem(Problem):
    """Variable projection: 9/18 camera parameters and independent robust point fits.

    Each evaluation starts from public Tier 0 batch DLT at the CURRENT cameras.
    Batched, safeguarded Newton/IRLS refines the SAME Huber/floor/depth objective.
    Point stationarity, not a fixed iteration count, is the stopping condition.
    The outer Jacobian differentiates the point stationarity equations; outer finite
    differences never subtract two approximate inner fits. No holdout or warm starts.
    """

    solver = "projected"

    def pack(self, R, C):
        return self.pack_cameras(R, C)

    def solver_options(self):
        # Tiny dense camera problem: exact trust-region solve, no iterative LSMR.
        return {"tr_solver": "exact", "jac": self.profiled_jacobian}

    def point_system(self, xyz, R, C):
        pixel, depth, jac = self.point_pixels(xyz, R, C)
        n = len(xyz)
        residual = np.zeros((n, 7))  # 4 pixels + 2 depth barriers + optional floor
        derivatives = np.zeros((n, 7, 3))
        residual[:, :4] = pixel.reshape(n, 4)
        derivatives[:, :4] = jac.reshape(n, 4, 3)
        residual[:, 4:6] = np.minimum(depth - 0.01, 0) / 0.01
        derivatives[:, 4:6] = R[self.pair, 2] * (depth < 0.01)[:, :, None] / 0.01
        residual[self.floor, 6] = xyz[self.floor, 2] / 0.015
        derivatives[self.floor, 6, 2] = 1 / 0.015
        weights = self.point_weights(residual)
        squared = residual ** 2
        delta = self.cfg["huber_px"]
        squared[:, :4] = np.where(weights[:, :4] < 1,
                                  2 * delta * np.abs(residual[:, :4]) - delta ** 2,
                                  squared[:, :4])
        return 0.5 * squared.sum(axis=1), residual, derivatives

    def point_weights(self, residual):
        weights = np.ones_like(residual)
        weights[:, :4] = np.minimum(1., self.cfg["huber_px"] / np.maximum(np.abs(residual[:, :4]), 1e-15))
        return weights

    def point_curvature(self, residual):
        # d(psi)/dr for componentwise Huber, NOT the IRLS weight psi/r.
        curvature = np.ones_like(residual)
        curvature[:, :4] = np.abs(residual[:, :4]) < self.cfg["huber_px"]
        return curvature

    def point_hessian(self, xyz, R, C, residual, jac):
        """Exact 3x3 point Hessian away from Huber/depth/clamp breakpoints."""
        psi = self.point_weights(residual) * residual
        hessian = np.einsum("nri,nr,nrj->nij", jac, self.point_curvature(residual), jac)
        depth = np.einsum("nsi,nsi->ns", R[self.pair, 2], xyz[:, None] - C[self.pair])
        safe = np.where(np.abs(depth) < 1e-8, 1., depth)
        # d2(pixel)/dX2 = -(J_pixel outer R_z + R_z outer J_pixel)/depth.
        # Inside the denominator guard the pixel projection is linear in X.
        factor = (np.abs(depth) >= 1e-8) / safe
        jp = jac[:, :4].reshape(-1, 2, 2, 3)
        a = np.einsum("nsq,nsqi,nsj,ns->nij", psi[:, :4].reshape(-1, 2, 2),
                      jp, R[self.pair, 2], factor)
        return hessian - a - a.transpose(0, 2, 1)

    @staticmethod
    def skew(vector):
        vector = np.asarray(vector)
        matrix = np.zeros((*vector.shape[:-1], 3, 3))
        matrix[..., 0, 1], matrix[..., 0, 2] = -vector[..., 2], vector[..., 1]
        matrix[..., 1, 0], matrix[..., 1, 2] = vector[..., 2], -vector[..., 0]
        matrix[..., 2, 0], matrix[..., 2, 1] = -vector[..., 1], vector[..., 0]
        return matrix

    @classmethod
    def rotation_left_jacobian(cls, vector):
        theta2 = float(vector @ vector)
        cross = cls.skew(vector)
        if theta2 < 1e-8:
            a = 0.5 - theta2 / 24 + theta2 ** 2 / 720
            b = 1 / 6 - theta2 / 120 + theta2 ** 2 / 5040
        else:
            theta = np.sqrt(theta2)
            a, b = (1 - np.cos(theta)) / theta2, (theta - np.sin(theta)) / theta ** 3
        return np.eye(3) + a * cross + b * cross @ cross

    def fixed_point_camera_system(self, x, xyz, R, C, residual, jp):
        """Return J_camera and d(gradient_point)/d(camera), holding X fixed.

        Seven local rows per point: four pixels, two depth barriers, one floor.
        Rotation increments use Exp(w) R0, hence the SO(3) left Jacobian at w.
        Both the Huber weight derivative and changing point Jacobian are included.
        """
        n = len(xyz)
        jc = np.zeros((n, 7, self.nc))
        cross = np.zeros((n, 3, self.nc))
        psi = self.point_weights(residual) * residual
        curvature = self.point_curvature(residual)
        for k, c in enumerate(self.free):
            offset = k * self.dof
            left = self.rotation_left_jacobian(x[offset:offset + 3])
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
                # d(J_point)/dc = dpi dR - (J_camera R_z + J_point dxc_z)/z.
                djp = np.zeros((len(ids), 2, 3, self.dof))
                djp[:, :, :, :3] = np.einsum("nqi,ijk->nqjk", dpi, dr)
                djp -= (camera_pixels[:, :, None, :] * R[c, 2][None, None, :, None]
                        + jp[ids, rows, :, None] * dxc[:, None, 2:3, :]) \
                    * (moving / safe)[:, None, None, None]
                cross[ids, :, columns] += np.einsum(
                    "nqi,nq,nqj->nij", jp[ids, rows], curvature[ids, rows], camera_pixels)
                cross[ids, :, columns] += np.einsum("nq,nqij->nij", psi[ids, rows], djp)
                active_depth = depth < 0.01
                depth_jc = dxc[:, 2] * active_depth[:, None] / 0.01
                jc[ids, 4 + side, columns] = depth_jc
                cross[ids, :, columns] += jp[ids, 4 + side, :, None] * depth_jc[:, None]
                cross[ids, :, offset:offset + 3] += (
                    psi[ids, 4 + side, None, None] * dr[2][None]
                    * active_depth[:, None, None] / 0.01)
        return jc, cross

    def profiled_jacobian(self, x):
        R, C, xyz = self.unpack(x)
        _, residual, jp = self.point_system(xyz, R, C)
        jc, cross = self.fixed_point_camera_system(x, xyz, R, C, residual, jp)
        hessian = self.point_hessian(xyz, R, C, residual, jp)
        eigen = np.linalg.eigvalsh(hessian)
        # At a flat Huber minimum the minimizer need not be differentiable/unique.
        # Use the IRLS Schur tangent there; envelope gradient still agrees at stationarity.
        regular = eigen[:, 0] > 1e-10 * np.maximum(eigen[:, -1], 1.)
        if np.any(~regular):
            weights = self.point_weights(residual)
            hessian[~regular] = np.einsum("nri,nr,nrj->nij", jp[~regular], weights[~regular], jp[~regular])
            cross[~regular] = np.einsum("nri,nr,nrj->nij", jp[~regular], weights[~regular], jc[~regular])
        point_response = -np.linalg.pinv(hessian, rcond=1e-12, hermitian=True) @ cross
        local = jc + jp @ point_response
        n = len(xyz)
        # Match Problem.residual exactly: pixels, camera/height priors, floor, depths.
        prior_rows = len(self.free) * self.dof + 4
        matrix = np.zeros((4 * n + prior_rows + len(self.floor) + 2 * n, self.nc))
        matrix[:4 * n] = local[:, :4].reshape(4 * n, self.nc)
        row = 4 * n
        for k, c in enumerate(self.free):
            offset = k * self.dof
            matrix[row:row + 3, offset:offset + 3] = np.eye(3) / np.radians(self.cfg["rotation_sigma_deg"])
            row += 3
            if self.dof == 6:
                matrix[row:row + 3, offset + 3:offset + 6] = np.eye(3) / self.sigma
                matrix[4 * n + len(self.free) * self.dof + c, offset + 5] = 1 / self.cfg["height_sigma_m"]
                row += 3
        row += 4
        matrix[row:row + len(self.floor)] = local[self.floor, 6]
        matrix[row + len(self.floor):] = local[:, 4:6].reshape(2 * n, self.nc)
        self.projection_info["jacobian"] = "analytic_implicit_stationarity"
        self.projection_info["schur_tangent_fallback_points"] = int(np.sum(~regular))
        return matrix

    def unpack(self, x):
        if hasattr(self, "_cached_x") and np.array_equal(x, self._cached_x):
            return self._cached_unpack
        R, C = self.camera_poses(x)
        xyz = self.points_at_poses(R, C)
        tolerance = self.cfg.get("projected_point_gradient_tol", 1e-4)
        # Leave margin for the public stationarity check and envelope gradient.
        target = min(tolerance * 0.1, 1e-8)
        step_tolerance = self.cfg.get("projected_point_step_tol", 1e-12)
        limit = self.cfg.get("projected_point_max_iterations", 100)
        point_iterations = np.zeros(len(xyz), dtype=int)
        last_step = np.zeros(len(xyz))
        stalled = np.zeros(len(xyz), dtype=bool)
        iterations = 0
        for iterations in range(1, limit + 1):
            cost, residual, jac = self.point_system(xyz, R, C)
            # Recompute weights at the accepted X BEFORE forming normal equations.
            weights = self.point_weights(residual)
            gradient = np.einsum("nri,nr->ni", jac, weights * residual)
            active = (np.max(np.abs(gradient), axis=1) > target) & ~stalled
            if not np.any(active):
                break
            point_iterations[active] += 1
            # IRLS is a safe PSD model but converges very slowly on saturated pixels.
            # A modified exact Newton model accounts for changing Huber weights and
            # perspective curvature, with a positive eigenvalue floor for descent.
            irls = np.einsum("nri,nr,nrj->nij", jac, weights, jac)
            hessian = self.point_hessian(xyz, R, C, residual, jac)
            eigen, basis = np.linalg.eigh(hessian)
            floor = 1e-6 * np.maximum(np.trace(irls, axis1=1, axis2=2), 1.)
            eigen = np.maximum(eigen, floor[:, None])
            step = np.einsum("nij,nj->ni", basis,
                             np.einsum("nji,nj->ni", basis, gradient) / eigen)
            # Bound the proposal, not the point: large Newton steps are shortened
            # before line search; subsequent iterations can still move further.
            radius = 0.5 * (1 + np.linalg.norm(xyz, axis=1))
            step *= np.minimum(1., radius / np.maximum(np.linalg.norm(step, axis=1), 1e-30))[:, None]
            step[~active] = 0
            alpha = np.ones(len(xyz))
            accepted = ~active
            origin = xyz.copy()
            slope = np.sum(gradient * step, axis=1)
            for _ in range(30):
                # Every trial is relative to the same origin, including points
                # already accepted with a different step length.
                candidate = origin - alpha[:, None] * step
                trial, tr, tj = self.point_system(candidate, R, C)
                slack = 1e-12 * np.maximum(cost, 1.)
                sufficient = trial <= cost - 1e-4 * alpha * slope
                # At machine-precision cost changes require a BETTER gradient,
                # rather than repeatedly accepting a zero/equal-cost stalled step.
                near = np.abs(trial - cost) <= slack
                trial_gradient = np.einsum("nri,nr->ni", tj, self.point_weights(tr) * tr)
                improved = np.max(np.abs(trial_gradient), axis=1) < np.max(np.abs(gradient), axis=1)
                good = ~accepted & np.isfinite(trial) & (sufficient | (near & improved))
                xyz[good] = candidate[good]
                last_step[good] = np.linalg.norm(candidate[good] - origin[good], axis=1)
                accepted |= good
                if np.all(accepted):
                    break
                alpha[~accepted] *= 0.5
            stalled |= active & ~accepted
            # A small step alone NEVER certifies convergence. Stalled fits receive
            # an independent TRF/IRLS fallback, then the same raw gradient audit.
            stalled |= active & accepted & (last_step <= step_tolerance * (1 + np.linalg.norm(xyz, axis=1)))
        _, residual, jac = self.point_system(xyz, R, C)
        gradient = np.einsum("nri,nr->ni", jac, self.point_weights(residual) * residual)
        fallback = np.flatnonzero(np.max(np.abs(gradient), axis=1) > target)
        fallback_nfev = 0
        anchor = next(c for c in range(4) if c not in self.free)
        for i in fallback:
            # Rare difficult tracks only; no global camera/point solve or warm start.
            single = ProjectedProblem([self.tracks[i]], self.K, self.R0, self.C0,
                                      anchor, self.cfg, self.sigma)
            def fun(point):
                return single.point_system(point[None], R, C)[1][0]
            def derivative(point):
                return single.point_system(point[None], R, C)[2][0]
            fit = least_squares(fun, xyz[i], jac=derivative, loss=single.loss,
                                x_scale=self.cfg.get("x_scale_point_m", 0.05),
                                max_nfev=self.cfg.get("projected_point_fallback_nfev", 200),
                                ftol=None, xtol=1e-14, gtol=target * 0.1)
            fallback_nfev += fit.nfev
            old = single.point_system(xyz[i:i + 1], R, C)
            new = single.point_system(fit.x[None], R, C)
            old_gradient = np.max(np.abs(old[2][0].T @ (single.point_weights(old[1])[0] * old[1][0])))
            new_gradient = np.max(np.abs(new[2][0].T @ (single.point_weights(new[1])[0] * new[1][0])))
            slack = 1e-12 * max(float(old[0][0]), 1.)
            if new[0][0] < old[0][0] or (new[0][0] <= old[0][0] + slack and new_gradient < old_gradient):
                last_step[i] = np.linalg.norm(fit.x - xyz[i])
                xyz[i] = fit.x
        _, residual, jac = self.point_system(xyz, R, C)
        gradient = np.einsum("nri,nr->ni", jac, self.point_weights(residual) * residual)
        worst = float(np.max(np.abs(gradient)))
        self.projection_info = {"converged": worst <= tolerance, "max_point_gradient": worst,
                                "gradient_tolerance": tolerance, "solve_gradient_target": target,
                                "iterations": iterations, "max_iterations": limit,
                                "max_point_iterations": int(point_iterations.max()),
                                "max_last_step_m": float(last_step.max()), "step_tolerance": step_tolerance,
                                "unconverged_points": int(np.sum(np.max(np.abs(gradient), axis=1) > tolerance)),
                                "stalled_before_fallback": int(stalled.sum()),
                                "fallback_points": len(fallback), "fallback_nfev": fallback_nfev}
        self._cached_x, self._cached_unpack = x.copy(), (R, C, xyz)
        return R, C, xyz

    def observability(self, x):
        # Diagnose RAW full BA Jacobians, not derivatives of the profiled residual.
        # Otherwise eliminated points/priors could conceal the original data null modes.
        _, _, xyz = self.unpack(x)
        joint = Problem(self.tracks, self.K, self.R0, self.C0,
                        next(c for c in range(4) if c not in self.free), self.cfg, self.sigma)
        return joint.observability(np.concatenate((x, xyz.ravel())))


def evaluate(tracks, K, R, C):
    by_camera = {c: [] for c in CAMS}
    by_pair, invalid = {}, 0
    for pair in PAIRS:
        chosen = [t for t in tracks if t["pair"] == list(pair) and t["split"] == "holdout"]
        if not chosen:
            by_pair[f"{CAMS[pair[0]]}-{CAMS[pair[1]]}"] = statistics([])
            continue
        uv = np.array([t["uv"] for t in chosen])
        xyz = triangulate(uv, pair, K, R, C)
        errors = []
        for side, c in enumerate(pair):
            predicted, depth = project(xyz, c, K, R, C)
            error = np.linalg.norm(predicted - uv[:, side], axis=1)
            error[(depth <= 0) | ~np.isfinite(xyz).all(axis=1)] = np.inf
            by_camera[CAMS[c]].extend(error.tolist())
            errors.extend(error.tolist())
        by_pair[f"{CAMS[pair[0]]}-{CAMS[pair[1]]}"] = statistics(errors)
        invalid += sum(not np.isfinite(e) for e in errors)
    return {"cameras": {c: statistics(v) for c, v in by_camera.items()}, "pairs": by_pair,
            "invalid_observations": invalid}


def pose_change(R, C, R0, C0, heights):
    return {cid: {"rotation_deg": float(np.degrees(Rotation.from_matrix(R[i] @ R0[i].T).magnitude())),
                  "center_cm": float(np.linalg.norm(C[i] - C0[i]) * 100),
                  "center_delta_xyz_cm": ((C[i] - C0[i]) * 100).tolist(),
                  "height_residual_cm": float((C[i, 2] - heights[cid]) * 100)} for i, cid in enumerate(CAMS)}


def run(config, out, from_tracks=None, solver="projected"):
    started = time.perf_counter()
    cv2.setNumThreads(config["threads"])
    doc, cameras, K, R0, C0 = read_calibration(config["calibration"])
    anchor_name = select_anchor(json.loads(Path(config["v0_results"]).read_text(encoding="utf-8")))
    anchor = CAMS.index(anchor_name)
    if config["confirmed_floor_tracks"]:
        raise ValueError("This recording has no independently confirmed floor labels; keep the list empty")
    if from_tracks is None:
        tracks, metadata = build_tracks(config, cameras, K, R0, C0, out)
    else:
        tracks, metadata = load_tracks(from_tracks, config)
        (out / "tracks.json").write_text(json.dumps({"tracks": tracks, **metadata}, indent=2), encoding="utf-8")
    train = [t for t in tracks if t["split"] == "train"]
    problem_type = {"joint": Problem, "projected": ProjectedProblem}[solver]
    baseline = evaluate(tracks, K, R0, C0)
    stages = {"initial": {"holdout": baseline, "seconds": 0.0,
                           "pose_change": pose_change(R0, C0, R0, C0, config["heights_m"])}}
    poses, solutions = {}, {}
    R, C = R0.copy(), C0.copy()
    for name, sigma in (("A_rotation", None), ("B_strong", config["center_sigma_B_m"]),
                        ("C_relaxed_diagnostic", config["center_sigma_C_m"])):
        stage_started = time.perf_counter()
        problem = problem_type(train, K, R0, C0, anchor, config, sigma)
        R, C, result, info = problem.solve(R, C)
        held = evaluate(tracks, K, R, C)
        diagnostic_started = time.perf_counter()
        observability = problem.observability(result.x)
        info["observability_seconds"] = time.perf_counter() - diagnostic_started
        info.update(holdout=held, pose_change=pose_change(R, C, R0, C0, config["heights_m"]),
                    worsened_pairs=[p for p in baseline["pairs"] if worsened(baseline["pairs"][p], held["pairs"][p])],
                    observability=observability)
        info["stage_seconds"] = time.perf_counter() - stage_started
        info["within_time_target"] = info["stage_seconds"] <= config.get("stage_target_seconds", 10.)
        info["training_invalid_depths"] = int(np.sum(problem.pixel_residual(result.x)[1] <= 0))
        # Stage C is always measured for this experiment; never eligible for automatic use.
        info["diagnostic_only"] = name.startswith("C")
        stages[name], poses[name], solutions[name] = info, (R.copy(), C.copy()), result
        print(f"{name} ({solver}): solve={info['seconds']:.2f}s, stage={info['stage_seconds']:.2f}s, "
              f"success={info['success']}, nfev={info['nfev']}, "
              f"cost={info['initial_cost']['total']:.6f}->{info['final_cost']['total']:.6f}, "
              f"optimality={info['optimality']:.6g}, "
              f"{'채택 금지' if info['adoption_forbidden'] else '수렴 (독립 검증 필요)'}", flush=True)
    sensitivity = []
    reference_R, reference_C = poses["C_relaxed_diagnostic"]
    base_baselines = np.array([np.linalg.norm(reference_C[b] - reference_C[a]) for a, b in PAIRS])
    rng = np.random.default_rng(config["seed"])
    for label, multiplier, perturb, scale in (("weight_half", 0.5, False, 1.0),
                                               ("weight_double", 2.0, False, 1.0),
                                               ("initial_jitter", 1.0, True, 1.0),
                                               ("initial_scale_plus_2pct", 1.0, False, 1.02)):
        # Change the initialization AND prior means to expose dependence on initial geometry.
        Rp, Cp = R0.copy(), C0.copy()
        Cp = C0[anchor] + scale * (Cp - C0[anchor])
        if perturb:
            for c in range(4):
                if c != anchor:
                    Rp[c] = Rotation.from_rotvec(rng.normal(0, np.radians(0.5), 3)).as_matrix() @ Rp[c]
                    Cp[c] += rng.normal(0, 0.02, 3)
        problem = problem_type(train, K, Rp, Cp, anchor, config, config["center_sigma_C_m"] * multiplier)
        Rs, Cs, result, info = problem.solve(Rp, Cp)
        baselines = np.array([np.linalg.norm(Cs[b] - Cs[a]) for a, b in PAIRS])
        delta = np.linalg.norm(Cs - reference_C, axis=1) * 100
        relative = baselines / base_baselines - 1
        sensitivity.append({"label": label, **info, "max_center_difference_cm": float(delta.max()),
                            "center_difference_cm": dict(zip(CAMS, delta.tolist())),
                            "baseline_relative_change": relative.tolist(),
                            "median_baseline_scale_ratio": float(np.median(baselines / base_baselines)),
                            "holdout": evaluate(tracks, K, Rs, Cs)})
    unstable = any(s["adoption_forbidden"] or s["max_center_difference_cm"] > config["stability_center_cm"]
                   or max(abs(v) for v in s["baseline_relative_change"]) > config["stability_baseline_fraction"]
                   for s in sensitivity)
    weak = stages["C_relaxed_diagnostic"]["observability"]["data_only_schur"]["weak_modes"] > 0
    all_converged = all(s["success"] and not s["adoption_forbidden"] for s in stages.values() if "success" in s)
    all_converged &= all(s["success"] and not s["adoption_forbidden"] for s in sensitivity)
    result_doc = {"calibration_version": doc["calibration_version"], "anchor": anchor_name,
                  "solver": solver, "all_solves_converged": bool(all_converged),
                  "adoption_forbidden": True,
                  "adoption_blockers": (["solver_not_converged_or_invalid"] if not all_converged else [])
                      + ["independent_metric_validation_missing"],
                  "anchor_reason": "V0 ALIKED 2-view median minimum: D 1.80 < A 2.19 < B 6.19 < C 6.64 px; stationarity unverified",
                  "environment": {"numpy": np.__version__, "scipy": scipy.__version__,
                                  "opencv": cv2.__version__},
                  "input": metadata, "stages": stages, "sensitivity": sensitivity,
                  "translation_unstable": unstable, "data_jacobian_weak": weak,
                  "verdict": "HOLD", "translation_adopted": False,
                  "reason": "No independent floor/object measurement; two-view reprojection cannot establish metric accuracy",
                  "total_seconds": time.perf_counter() - started}
    (out / "measurements.json").write_text(json.dumps(result_doc, indent=2, allow_nan=False), encoding="utf-8")
    # Diagnostic arrays only; no calibration.json or publication API is produced.
    np.savez_compressed(out / "diagnostic_poses.npz", **{
        f"{name}_{kind}": values[i] for name, values in poses.items() for i, kind in enumerate(("R", "C"))})
    return result_doc
