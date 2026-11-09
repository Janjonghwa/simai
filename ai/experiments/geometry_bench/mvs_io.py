"""OpenMVS / COLMAP input-output conversion and multi-frame depth utilities (S15P31A101-528).

Coordinate & Unit Conversion Rationale
======================================
1. World Frame (`WORLD`, AGENTS.md Rule 3):
   - Unit: meters (m), right-handed 3D coordinate system.
   - Axes: +Z points UP (zenith), floor plane is Z = 0.

2. Camera Frame (`CAMERA` / OpenCV / COLMAP / texrecon):
   - Unit: translation in meters (m); intrinsics (fx, fy, cx, cy) in pixels (px).
   - Axes: +x right, +y down, +z forward (optical axis).
   - World-to-camera transform (`calibration.json` extrinsics R, t):
       X_c = R_w2c @ X_w + t_w2c
   - Camera optical center in World coordinates:
       C_w = -R_w2c.T @ t_w2c
   - Camera-to-world 4x4 homogeneous matrix (`cam2world`):
       [[R_w2c.T, C_w],
        [0, 0, 0,   1]]

3. COLMAP Text Model (`sparse/{cameras,images,points3D}.txt`):
   - `cameras.txt`: `CAMERA_ID PINHOLE W H fx fy cx cy` (undistorted pinhole camera in px).
   - `images.txt`:
       Line 1: `IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME`
         where (QW, QX, QY, QZ) is the Hamilton unit quaternion of `R_w2c` (with QW >= 0)
         and (TX, TY, TZ) = `t_w2c` in meters.
       Line 2: `X Y POINT3D_ID ...` (2D pixel projections in undistorted image and 1-based 3D point ID).
   - `points3D.txt`:
       `POINT3D_ID X Y Z R G B ERROR IMAGE_ID POINT2D_IDX ...`
         where (X, Y, Z) are in World meters (+Z up) and `TRACK[]` lists visible (IMAGE_ID, POINT2D_IDX) pairs.

4. OpenMVS `.mvs` Binary Scene (`InterfaceMVS` serialization used by `simai/openmvs:fcb172bd8490`):
   - `platforms`: 1 platform per camera (`platform001`, ...).
   - `camera`: `name="PINHOLE"`, normalized 3x3 intrinsics `K_norm`:
       fx_norm = fx / max(W, H), fy_norm = fy / max(W, H),
       cx_norm = cx / W,         cy_norm = cy / H
     and identity relative rotation `R_rel = I_3`, zero relative center `C_rel = (0, 0, 0)`.
   - `pose`: `R = R_w2c` (3x3 float64 row-major world-to-camera rotation) and
     `C = C_w = -R_w2c.T @ t_w2c` (3 float64 camera center in World meters).
   - `images`: `name` (path to undistorted image), `platformID`, `cameraID=0`, `poseID=0`.
   - `vertices`: `X` (3 float32 in World meters) + `views` list of `(uint32 imageID, float32 confidence)`.
   - `verticesColor`: `uint8[3]` in **BGR** order per vertex.

5. Texture UV & glTF 2.0 Binary `.glb` (`GLTF_Y_UP`):
   - OBJ / OpenMVS `TextureMesh` UV has origin at bottom-left `(u, v_obj)`.
     glTF 2.0 UV has origin at top-left: `v_gltf = 1.0 - v_obj`.
   - World `(x, y, z)` (+Z up) -> glTF `(x, z, -y)` (+Y up) is applied in ONE place
     via `simai_ai.scene.texture_mesh.to_glb_points` (AGENTS.md Rule 3).
   - Unobserved faces are never hallucinated (AGENTS.md Rule 2); they are separated into an
     `unobserved` primitive with neutral gray `baseColorFactor=[0.5, 0.5, 0.5, 1.0]`
     (or colored with `--empty-color 8421504` = `0x808080` RGB(128, 128, 128)).
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

from simai_ai.scene import texture_mesh as tm


def _encode_rgb_png_bytes(rgb: np.ndarray) -> bytes:
    """Encode `(H, W, 3)` uint8 RGB array to PNG bytes (uses `cv2` if installed, else stdlib `zlib`)."""
    arr = np.ascontiguousarray(rgb, dtype=np.uint8)
    if cv2 is not None:
        ok, enc = cv2.imencode(".png", cv2.cvtColor(arr, cv2.COLOR_RGB2BGR))
        if ok:
            return enc.tobytes()
    h, w = arr.shape[:2]
    raw_rows = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw_rows, level=6)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _decode_png_bytes_rgb(data: bytes) -> np.ndarray | None:
    """Decode PNG/JPEG bytes to `(H, W, 3)` uint8 RGB (uses `cv2` if installed, else minimal PNG decoder)."""
    if cv2 is not None:
        arr_u8 = np.frombuffer(data, dtype=np.uint8)
        bgr = cv2.imdecode(arr_u8, cv2.IMREAD_COLOR)
        if bgr is not None:
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    off = 8
    w = h = 0
    idat_parts: list[bytes] = []
    while off + 8 <= len(data):
        length = struct.unpack(">I", data[off : off + 4])[0]
        tag = data[off + 4 : off + 8]
        chunk_data = data[off + 8 : off + 8 + length]
        off += 12 + length
        if tag == b"IHDR":
            w, h = struct.unpack(">II", chunk_data[:8])
        elif tag == b"IDAT":
            idat_parts.append(chunk_data)
        elif tag == b"IEND":
            break
    if w <= 0 or h <= 0 or not idat_parts:
        return None
    raw = zlib.decompress(b"".join(idat_parts))
    row_bytes = w * 3 + 1
    if len(raw) < h * row_bytes:
        return None
    out = np.empty((h, w, 3), dtype=np.uint8)
    for y in range(h):
        start = y * row_bytes + 1
        out[y] = np.frombuffer(raw, dtype=np.uint8, count=w * 3, offset=start).reshape(w, 3)
    return out


def _write_rgb_image(path: Path, rgb: np.ndarray) -> None:
    """Save `(H, W, 3)` uint8 RGB image to disk."""
    path = Path(path)
    if cv2 is not None:
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        if path.suffix.lower() in (".jpg", ".jpeg"):
            cv2.imwrite(str(path), bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        else:
            cv2.imwrite(str(path), bgr)
        return
    path.write_bytes(_encode_rgb_png_bytes(rgb))


def _read_rgb_image(path: Path) -> np.ndarray | None:
    """Read an image file into `(H, W, 3)` uint8 RGB."""
    path = Path(path)
    if not path.is_file():
        return None
    if cv2 is not None:
        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is not None:
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return _decode_png_bytes_rgb(path.read_bytes())

OPENMVS_DOCKER_IMAGE = "simai/openmvs:fcb172bd8490"
OPENMVS_DOCKER_DIGEST = (
    "openmvs/openmvs-ubuntu@sha256:fcb172bd84903d679684e618b45dc6f7a7621de0da87e6dc40f8fb084016e35a"
)
TEXRECON_DOCKER_IMAGE = "simai/texrecon:dev"
EMPTY_COLOR_GRAY_INT = 8421504  # 0x808080 -> RGB(128, 128, 128)


@dataclass
class UndistortedCamera:
    """Undistorted pinhole camera in World meters (+Z up) and OpenCV camera axes."""

    camera_id: str
    width: int
    height: int
    K: np.ndarray  # (3, 3) float64 pixel intrinsics (cx=W/2, cy=H/2)
    R_w2c: np.ndarray  # (3, 3) float64 world-to-camera rotation
    t_w2c: np.ndarray  # (3,) float64 world-to-camera translation in meters
    cam2world: np.ndarray  # (4, 4) float64 camera-to-world matrix
    image_rgb: np.ndarray  # (H, W, 3) uint8 undistorted RGB photo
    valid_mask: np.ndarray  # (H, W) bool valid observation mask

    @property
    def center_world(self) -> np.ndarray:
        return (-self.R_w2c.T @ self.t_w2c).astype(np.float64)


def to_glb_points(points_world: np.ndarray) -> np.ndarray:
    """Convert World Z-up `(x, y, z)` meters to glTF Y-up `(x, z, -y)` via `texture_mesh.to_glb_points`."""
    pts = np.asarray(points_world)
    if pts.shape[-1] != 3:
        raise ValueError(f"Expected (..., 3), got {pts.shape}")
    orig_shape = pts.shape
    flat = pts.reshape(-1, 3).astype(np.float64)
    yup = tm.to_glb_points(np.eye(4, dtype=np.float64), flat)
    return yup.reshape(orig_shape).astype(pts.dtype if np.issubdtype(pts.dtype, np.floating) else np.float64)


def from_glb_points(points_yup: np.ndarray) -> np.ndarray:
    """Inverse of `texture_mesh.to_glb_points`: glTF Y-up (x, y, z) -> World Z-up (x, -z, y)."""
    pts = np.asarray(points_yup)
    if pts.shape[-1] != 3:
        raise ValueError(f"Expected (..., 3), got {pts.shape}")
    out = np.empty_like(pts)
    out[..., 0] = pts[..., 0]
    out[..., 1] = -pts[..., 2]
    out[..., 2] = pts[..., 1]
    return out


def rotmat_to_quaternion_wxyz(R: np.ndarray) -> np.ndarray:
    """Convert 3x3 proper rotation matrix `R` to Hamilton unit quaternion `(qw, qx, qy, qz)` with `qw >= 0`."""
    R_arr = np.asarray(R, dtype=np.float64)
    if R_arr.shape != (3, 3):
        raise ValueError(f"Expected (3, 3) rotation matrix, got {R_arr.shape}")
    u, _, vt = np.linalg.svd(R_arr)
    R_ortho = u @ vt
    if np.linalg.det(R_ortho) < 0:
        u[:, -1] *= -1.0
        R_ortho = u @ vt
    qx, qy, qz, qw = Rotation.from_matrix(R_ortho).as_quat()
    q = np.array([qw, qx, qy, qz], dtype=np.float64)
    q /= max(float(np.linalg.norm(q)), 1e-12)
    if q[0] < 0.0:
        q = -q
    return q


def quaternion_wxyz_to_rotmat(q_wxyz: np.ndarray) -> np.ndarray:
    """Convert Hamilton unit quaternion `(qw, qx, qy, qz)` to 3x3 rotation matrix."""
    q = np.asarray(q_wxyz, dtype=np.float64).reshape(4)
    q = q / max(float(np.linalg.norm(q)), 1e-12)
    qw, qx, qy, qz = float(q[0]), float(q[1]), float(q[2]), float(q[3])
    return np.asarray(Rotation.from_quat([qx, qy, qz, qw]).as_matrix(), dtype=np.float64)


def resolve_calibration_payload(
    calib_path_or_dir: Path,
    calibration_version: str = "cal-lobby-init-f0ab1c6b-jba2-2f6cc097",
) -> dict[str, Any]:
    """Load calibration dict from a JSON file or search bundle directory for `calibration_version`."""
    p = Path(calib_path_or_dir)
    if p.is_file():
        d = json.loads(p.read_text(encoding="utf-8"))
        payload = d.get("payload", d)
        if payload.get("calibration_version") == calibration_version:
            return payload
        # If a sibling state/config/bundles directory exists with the requested version, prefer it
        bundles_dir = p.parent / "state" / "config" / "bundles"
        if bundles_dir.is_dir():
            for bf in sorted(bundles_dir.glob("*/calibration.json")):
                bd = json.loads(bf.read_text(encoding="utf-8"))
                bp = bd.get("payload", bd)
                if bp.get("calibration_version") == calibration_version:
                    return bp
        return payload
    if p.is_dir():
        fallback_payload: dict[str, Any] | None = None
        direct_cal = p / "calibration.json"
        candidates = ([direct_cal] if direct_cal.is_file() else []) + [
            bf for bf in sorted(p.rglob("calibration.json")) if bf != direct_cal
        ]
        for bf in candidates:
            bd = json.loads(bf.read_text(encoding="utf-8"))
            bp = bd.get("payload", bd)
            if fallback_payload is None and isinstance(bp, dict) and "cameras" in bp:
                fallback_payload = bp
            if bp.get("calibration_version") == calibration_version:
                return bp
        if fallback_payload is not None:
            return fallback_payload
    raise FileNotFoundError(
        f"Calibration ({calibration_version}) not found at {calib_path_or_dir}"
    )


def _find_camera_image_path(bundle_dir: Path, cid: str) -> Path | None:
    """Locate camera image under `bundle_dir` supporting `.jpg`, `.png`, `.image`, `images/`, and `prepared/`."""
    bdir = Path(bundle_dir)
    candidates = [
        bdir / f"{cid}.jpg",
        bdir / f"{cid}.png",
        bdir / f"{cid}.image",
        bdir / "images" / f"{cid}.image",
        bdir / "images" / f"{cid}.png",
        bdir / "images" / f"{cid}.jpg",
        bdir / "prepared" / f"{cid}.png",
        bdir / "prepared" / f"{cid}.jpg",
        bdir / "prepared" / "images" / f"{cid}.png",
        bdir / "prepared" / "images" / f"{cid}.jpg",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def load_and_undistort_cameras(
    calib_path: Path,
    bundle_dir: Path,
    camera_ids: tuple[str, ...] = ("cam_A", "cam_B", "cam_C", "cam_D"),
    calibration_version: str = "cal-lobby-init-f0ab1c6b-jba2-2f6cc097",
) -> dict[str, UndistortedCamera]:
    """Load `calibration.json` and undistort `cam_*.{jpg,png,image}` + optional `cam_*.valid.png`.

    Follows the exact `texrecon_opt/common.py` / `-314` undistortion convention:
    `cv2.getOptimalNewCameraMatrix(K_raw, dist, (W, H), 0, (W, H), centerPrincipalPoint=True)`
    with `cv2.BORDER_REPLICATE`.
    """
    calib = resolve_calibration_payload(calib_path, calibration_version=calibration_version)
    cam_entries = {c["camera_id"]: c for c in calib["cameras"]}
    out: dict[str, UndistortedCamera] = {}

    for cid in camera_ids:
        if cid not in cam_entries:
            raise KeyError(f"Camera {cid} not found in {calib_path}")
        entry = cam_entries[cid]
        img_path = _find_camera_image_path(Path(bundle_dir), cid)
        bgr = None
        if img_path is not None and cv2 is not None:
            raw_bytes = np.frombuffer(img_path.read_bytes(), dtype=np.uint8)
            bgr = cv2.imdecode(raw_bytes, cv2.IMREAD_COLOR)
        if bgr is None:
            raise FileNotFoundError(
                f"Cannot read camera image for {cid} under {bundle_dir} (checked .jpg/.png/.image in root, images/, prepared/)"
            )
        h, w = bgr.shape[:2]

        if "intrinsics" in entry:
            K_raw = np.asarray(entry["intrinsics"]["K"], dtype=np.float64)
            dist_raw = entry["intrinsics"].get("distortion", [0, 0, 0, 0, 0])
        else:
            K_raw = np.asarray(entry["K"], dtype=np.float64)
            dist_raw = entry.get("distortion", [0, 0, 0, 0, 0])

        if isinstance(dist_raw, dict):
            dist = np.asarray(dist_raw.get("coeffs", [0, 0, 0, 0, 0]), dtype=np.float64)
        else:
            dist = np.asarray(dist_raw, dtype=np.float64)

        if "extrinsics" in entry:
            R_w2c = np.asarray(entry["extrinsics"]["R"], dtype=np.float64)
            t_w2c = np.asarray(entry["extrinsics"]["t"], dtype=np.float64).reshape(3)
        else:
            R_w2c = np.asarray(entry["R"], dtype=np.float64)
            t_w2c = np.asarray(entry["t"], dtype=np.float64).reshape(3)

        K_pinhole, _ = cv2.getOptimalNewCameraMatrix(
            K_raw, dist, (w, h), 0, (w, h), centerPrincipalPoint=True
        )
        K_pinhole = np.asarray(K_pinhole, dtype=np.float64)
        map1, map2 = cv2.initUndistortRectifyMap(
            K_raw, dist, None, K_pinhole, (w, h), cv2.CV_32FC1
        )
        bgr_undist = cv2.remap(
            bgr, map1, map2, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
        )
        rgb_undist = np.ascontiguousarray(bgr_undist[:, :, ::-1])

        valid_path = Path(bundle_dir) / f"{cid}.valid.png"
        if valid_path.is_file():
            v_raw = cv2.imread(str(valid_path), cv2.IMREAD_GRAYSCALE)
            if v_raw is not None and v_raw.shape == (h, w):
                v_undist = cv2.remap(
                    v_raw,
                    map1,
                    map2,
                    interpolation=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE,
                )
                mask_undist = v_undist > 127
            else:
                mask_undist = np.ones((h, w), dtype=bool)
        else:
            mask_undist = np.ones((h, w), dtype=bool)

        c2w = np.eye(4, dtype=np.float64)
        c2w[:3, :3] = R_w2c.T
        c2w[:3, 3] = -R_w2c.T @ t_w2c

        out[cid] = UndistortedCamera(
            camera_id=cid,
            width=w,
            height=h,
            K=K_pinhole,
            R_w2c=R_w2c,
            t_w2c=t_w2c,
            cam2world=c2w,
            image_rgb=rgb_undist,
            valid_mask=mask_undist,
        )
    return out


def aggregate_multiframe_depths(
    depth_frames_by_cam: dict[str, np.ndarray | list[np.ndarray]],
    valid_masks_by_cam: dict[str, np.ndarray | list[np.ndarray]] | None = None,
    conf_frames_by_cam: dict[str, np.ndarray | list[np.ndarray]] | None = None,
    *,
    min_valid_frames: int = 1,
    min_depth_m: float = 0.1,
    max_depth_m: float = 20.0,
    min_conf: float = 0.0,
    align_scale_to_first: bool = True,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, Any]]:
    """Aggregate N person-free background frame depths per camera using pixel-wise median.

    Variant (1) Multi-frame depth rule (AGENTS.md Rule 2):
    - Invalid / unobserved / person-masked pixels are represented as NaN during aggregation
      and marked False in `combined_valid_masks` (depth set to 0.0 only where masked out,
      NEVER treated as a 0m observation).
    - If `align_scale_to_first=True`, each frame k >= 1 is scale-aligned to frame 0 using
      the median ratio `median(d_0 / d_k)` over co-valid pixels before computing the
      per-pixel median across frames.
    """
    median_depths: dict[str, np.ndarray] = {}
    combined_masks: dict[str, np.ndarray] = {}
    stats: dict[str, Any] = {}

    for cid, raw_frames in depth_frames_by_cam.items():
        stack = np.asarray(raw_frames, dtype=np.float64)
        if stack.ndim == 2:
            stack = stack[None, ...]
        if stack.ndim != 3:
            raise ValueError(f"{cid}: expected (N, H, W) depth stack, got {stack.shape}")
        n_frames, h, w = stack.shape

        valid = np.isfinite(stack) & (stack >= min_depth_m) & (stack <= max_depth_m)
        if valid_masks_by_cam and cid in valid_masks_by_cam:
            m_stack = np.asarray(valid_masks_by_cam[cid], dtype=bool)
            if m_stack.ndim == 2:
                m_stack = np.broadcast_to(m_stack[None, ...], (n_frames, h, w))
            valid &= m_stack
        if conf_frames_by_cam and cid in conf_frames_by_cam and min_conf > 0.0:
            c_stack = np.asarray(conf_frames_by_cam[cid], dtype=np.float64)
            if c_stack.ndim == 2:
                c_stack = np.broadcast_to(c_stack[None, ...], (n_frames, h, w))
            valid &= np.isfinite(c_stack) & (c_stack >= min_conf)

        work = np.where(valid, stack, np.nan)
        frame_scales: list[float] = [1.0]
        if align_scale_to_first and n_frames > 1:
            ref = work[0]
            ref_valid = np.isfinite(ref)
            for k in range(1, n_frames):
                cur = work[k]
                both = ref_valid & np.isfinite(cur) & (cur > 1e-6)
                if int(both.sum()) >= 64:
                    scale_k = float(np.median(ref[both] / cur[both]))
                    if np.isfinite(scale_k) and 0.25 <= scale_k <= 4.0:
                        work[k] = cur * scale_k
                        frame_scales.append(round(scale_k, 6))
                    else:
                        frame_scales.append(1.0)
                else:
                    frame_scales.append(1.0)

        valid_count = np.sum(np.isfinite(work), axis=0)
        keep_mask = valid_count >= max(1, int(min_valid_frames))

        # Suppress All-NaN slice RuntimeWarning by filling completely empty pixels with 0 temporarily
        safe_work = np.where(keep_mask[None, :, :], work, 0.0)
        med = np.nanmedian(safe_work, axis=0)
        med = np.where(keep_mask & np.isfinite(med), med, 0.0).astype(np.float32)

        abs_dev = np.abs(work - med[None, :, :])
        safe_dev = np.where(keep_mask[None, :, :], abs_dev, 0.0)
        mad = np.nanmedian(safe_dev, axis=0)
        rel_mad = np.where(keep_mask & (med > 1e-6), mad / np.maximum(med, 1e-6), np.nan)
        finite_rel_mad = rel_mad[np.isfinite(rel_mad)]

        median_depths[cid] = med
        combined_masks[cid] = keep_mask
        stats[cid] = {
            "num_frames": int(n_frames),
            "height": int(h),
            "width": int(w),
            "min_valid_frames": int(min_valid_frames),
            "coverage_ratio": round(float(keep_mask.mean()), 5),
            "mean_valid_frames_per_pixel": round(float(valid_count[keep_mask].mean()), 3)
            if bool(keep_mask.any())
            else 0.0,
            "temporal_rel_mad_p50": round(float(np.percentile(finite_rel_mad, 50)), 6)
            if finite_rel_mad.size
            else None,
            "temporal_rel_mad_p90": round(float(np.percentile(finite_rel_mad, 90)), 6)
            if finite_rel_mad.size
            else None,
            "frame_scales": frame_scales,
        }

    return median_depths, combined_masks, stats


def build_visible_point_cloud_from_depths(
    cameras: dict[str, UndistortedCamera],
    depths_by_cam: dict[str, np.ndarray],
    valid_masks_by_cam: dict[str, np.ndarray] | None = None,
    *,
    stride: int = 4,
    max_depth_m: float = 15.0,
    edge_band_px: int = 5,
    covis_rel_tol: float = 0.05,
) -> tuple[np.ndarray, np.ndarray, list[list[tuple[int, float, float, float]]], dict[str, Any]]:
    """Back-project cleaned depth maps into 3D World points + RGB + per-point visible camera list.

    Returns:
      - `points_xyz`: `(N, 3)` float32 in World meters (+Z up)
      - `points_rgb`: `(N, 3)` uint8 RGB
      - `point_views`: list of length `N`, where `point_views[i]` is a list of
        `(cam_idx_0based, u_px, v_px, confidence)` for all cameras that observe point `i`
        within `covis_rel_tol` depth consistency.
      - `stats`: summary dict.
    """
    cam_ids = list(cameras.keys())
    cleaned_depths: dict[str, np.ndarray] = {}
    for cid in cam_ids:
        d = np.asarray(depths_by_cam[cid], dtype=np.float32)
        cam = cameras[cid]
        if d.shape != (cam.height, cam.width):
            d = cv2.resize(d, (cam.width, cam.height), interpolation=cv2.INTER_NEAREST)
        vmask = cam.valid_mask.copy()
        if valid_masks_by_cam and cid in valid_masks_by_cam:
            m = np.asarray(valid_masks_by_cam[cid], dtype=bool)
            if m.shape != (cam.height, cam.width):
                m = (
                    cv2.resize(
                        m.astype(np.uint8),
                        (cam.width, cam.height),
                        interpolation=cv2.INTER_NEAREST,
                    )
                    > 0
                )
            vmask &= m
        d_masked = np.where(vmask & np.isfinite(d) & (d > 0.1) & (d <= max_depth_m), d, 0.0).astype(np.float32)
        fly = tm.flying_mask(d_masked, d_masked > 0.1)
        d_clean = np.where(~fly, d_masked, 0.0).astype(np.float32)
        cleaned_depths[cid] = d_clean

    all_xyz: list[np.ndarray] = []
    all_rgb: list[np.ndarray] = []
    all_src_cam: list[np.ndarray] = []
    all_src_uv: list[np.ndarray] = []

    step = max(1, int(stride))
    for src_idx, cid in enumerate(cam_ids):
        cam = cameras[cid]
        d = cleaned_depths[cid]
        ys, xs = np.where((d > 0.1) & (d <= max_depth_m))
        if ys.size == 0:
            continue
        keep = ((ys % step) == 0) & ((xs % step) == 0)
        ys = ys[keep]
        xs = xs[keep]
        if ys.size == 0:
            continue
        z = d[ys, xs].astype(np.float64)
        fx, fy = float(cam.K[0, 0]), float(cam.K[1, 1])
        cx, cy = float(cam.K[0, 2]), float(cam.K[1, 2])
        x_cam = (xs.astype(np.float64) - cx) * z / fx
        y_cam = (ys.astype(np.float64) - cy) * z / fy
        pts_cam = np.stack([x_cam, y_cam, z], axis=1)
        pts_world = (pts_cam @ cam.R_w2c) + cam.center_world[None, :]
        rgb = cam.image_rgb[ys, xs]

        all_xyz.append(pts_world.astype(np.float32))
        all_rgb.append(rgb.astype(np.uint8))
        all_src_cam.append(np.full(ys.shape[0], src_idx, dtype=np.int32))
        all_src_uv.append(np.stack([xs.astype(np.float32), ys.astype(np.float32)], axis=1))

    if not all_xyz:
        return (
            np.zeros((0, 3), dtype=np.float32),
            np.zeros((0, 3), dtype=np.uint8),
            [],
            {"num_points": 0, "multi_view_ratio": 0.0},
        )

    points_xyz = np.concatenate(all_xyz, axis=0)
    points_rgb = np.concatenate(all_rgb, axis=0)
    src_cam = np.concatenate(all_src_cam, axis=0)
    src_uv = np.concatenate(all_src_uv, axis=0)

    n_pts = points_xyz.shape[0]
    point_views: list[list[tuple[int, float, float, float]]] = [
        [(int(src_cam[i]), float(src_uv[i, 0]), float(src_uv[i, 1]), 1.0)] for i in range(n_pts)
    ]

    pts_w64 = points_xyz.astype(np.float64)
    for tgt_idx, cid in enumerate(cam_ids):
        cam = cameras[cid]
        d_tgt = cleaned_depths[cid]
        pts_c = (pts_w64 @ cam.R_w2c.T) + cam.t_w2c[None, :]
        z_c = pts_c[:, 2]
        in_front = (src_cam != tgt_idx) & (z_c > 0.1) & (z_c <= max_depth_m)
        cand_indices = np.flatnonzero(in_front)
        if cand_indices.size == 0:
            continue
        zc_sub = z_c[cand_indices]
        u = float(cam.K[0, 0]) * (pts_c[cand_indices, 0] / zc_sub) + float(cam.K[0, 2])
        v = float(cam.K[1, 1]) * (pts_c[cand_indices, 1] / zc_sub) + float(cam.K[1, 2])
        ui = np.rint(u).astype(np.int32)
        vi = np.rint(v).astype(np.int32)
        in_bounds = (ui >= 0) & (ui < cam.width) & (vi >= 0) & (vi < cam.height)
        cand_indices = cand_indices[in_bounds]
        if cand_indices.size == 0:
            continue
        ui = ui[in_bounds]
        vi = vi[in_bounds]
        u = u[in_bounds]
        v = v[in_bounds]
        zc_sub = zc_sub[in_bounds]

        d_obs = d_tgt[vi, ui].astype(np.float64)
        rel_diff = np.abs(zc_sub - d_obs) / np.maximum(zc_sub, 1e-6)
        vis_ok = (d_obs > 0.1) & (rel_diff <= covis_rel_tol)
        for idx_pt, u_val, v_val, r_val in zip(
            cand_indices[vis_ok], u[vis_ok], v[vis_ok], rel_diff[vis_ok], strict=False
        ):
            weight = float(max(0.1, 1.0 - (r_val / max(covis_rel_tol, 1e-6))))
            point_views[int(idx_pt)].append((int(tgt_idx), float(u_val), float(v_val), weight))

    # OpenMVS cList::FindFirst binary-searches point.views by imageID (cam_idx), so sort ascending!
    for idx_pt in range(n_pts):
        if len(point_views[idx_pt]) > 1:
            point_views[idx_pt].sort(key=lambda item: int(item[0]))

    multi_view_count = sum(1 for vlist in point_views if len(vlist) >= 2)
    stats = {
        "num_points": int(n_pts),
        "stride": int(step),
        "multi_view_points": int(multi_view_count),
        "multi_view_ratio": round(float(multi_view_count / max(1, n_pts)), 5),
        "mean_views_per_point": round(
            float(sum(len(v) for v in point_views) / max(1, n_pts)), 3
        ),
    }
    return points_xyz, points_rgb, point_views, stats


def render_mesh_depths_for_cameras(
    V_world: np.ndarray,
    F: np.ndarray,
    cameras: dict[str, UndistortedCamera],
    *,
    scale: float = 0.5,
) -> dict[str, np.ndarray]:
    """Raycast `(V_world, F)` from each camera to produce metric depth maps `(H, W)` float32."""
    import open3d as o3d

    t_mesh = o3d.t.geometry.TriangleMesh()
    t_mesh.vertex.positions = o3d.core.Tensor(np.ascontiguousarray(V_world, dtype=np.float32))
    t_mesh.triangle.indices = o3d.core.Tensor(np.ascontiguousarray(F, dtype=np.int32))
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(t_mesh)

    depths: dict[str, np.ndarray] = {}
    for cid, cam in cameras.items():
        rw = max(1, int(round(cam.width * scale)))
        rh = max(1, int(round(cam.height * scale)))
        sx = rw / float(cam.width)
        sy = rh / float(cam.height)
        fx = float(cam.K[0, 0]) * sx
        fy = float(cam.K[1, 1]) * sy
        cx = float(cam.K[0, 2]) * sx
        cy = float(cam.K[1, 2]) * sy

        vv, uu = np.meshgrid(
            np.arange(rh, dtype=np.float32),
            np.arange(rw, dtype=np.float32),
            indexing="ij",
        )
        dirs_cam = np.stack([(uu - cx) / fx, (vv - cy) / fy, np.ones_like(uu)], axis=-1)
        dirs_world = np.einsum("ij,hwj->hwi", cam.R_w2c.T.astype(np.float32), dirs_cam)
        origins = np.broadcast_to(cam.center_world.astype(np.float32), dirs_world.shape)
        rays = np.concatenate([origins, dirs_world], axis=-1).reshape(-1, 6)

        ans = scene.cast_rays(o3d.core.Tensor(np.ascontiguousarray(rays, dtype=np.float32)))
        t_hit = ans["t_hit"].numpy().reshape(rh, rw)
        d_small = np.where(np.isfinite(t_hit) & (t_hit > 0.05) & (t_hit < 30.0), t_hit, 0.0).astype(
            np.float32
        )
        if (rh, rw) != (cam.height, cam.width):
            d_full = cv2.resize(
                d_small, (cam.width, cam.height), interpolation=cv2.INTER_NEAREST
            )
        else:
            d_full = d_small
        depths[cid] = np.where(cam.valid_mask, d_full, 0.0).astype(np.float32)
    return depths


def export_colmap_text_workspace(
    out_dir: Path,
    cameras: dict[str, UndistortedCamera],
    points_xyz: np.ndarray | None = None,
    points_rgb: np.ndarray | None = None,
    point_views: list[list[tuple[int, float, float, float]]] | None = None,
) -> dict[str, Path]:
    """Write COLMAP text model (`sparse/{cameras,images,points3D}.txt`) + undistorted `images/`.

    Compatible with `InterfaceCOLMAP -w <out_dir> -i <out_dir> -o <out_dir>/scene.mvs --image-folder <out_dir>/images`.
    Note: OpenMVS `InterfaceCOLMAP` requires Unix LF (`\\n`) line endings and at least one
    valid 3D point with a 2D track per image.
    """
    out_dir = Path(out_dir)
    sparse_dir = out_dir / "sparse"
    images_dir = out_dir / "images"
    sparse_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    cam_ids = list(cameras.keys())
    n_cams = len(cam_ids)

    pts_xyz = (
        np.asarray(points_xyz, dtype=np.float64)
        if points_xyz is not None and len(points_xyz) > 0
        else np.array([[4.0, 4.0, 1.0]], dtype=np.float64)
    )
    n_pts = pts_xyz.shape[0]
    pts_rgb = (
        np.asarray(points_rgb, dtype=np.uint8)
        if points_rgb is not None and len(points_rgb) == n_pts
        else np.full((n_pts, 3), 180, dtype=np.uint8)
    )
    if point_views is not None and len(point_views) == n_pts:
        views_list = point_views
    else:
        first_cam = cameras[cam_ids[0]]
        views_list = [
            [(i, 0.5 * first_cam.width, 0.5 * first_cam.height, 1.0) for i in range(n_cams)]
            for _ in range(n_pts)
        ]

    # Build 2D keypoints per image and 3D track references
    img_points2d: list[list[tuple[float, float, int]]] = [[] for _ in range(n_cams)]
    pt_tracks: list[list[tuple[int, int]]] = [[] for _ in range(n_pts)]

    for pt_idx in range(n_pts):
        pt_id_1based = pt_idx + 1
        for cam_idx, u_px, v_px, _weight in views_list[pt_idx]:
            if 0 <= cam_idx < n_cams:
                p2d_idx = len(img_points2d[cam_idx])
                img_points2d[cam_idx].append((float(u_px), float(v_px), pt_id_1based))
                pt_tracks[pt_idx].append((cam_idx + 1, p2d_idx))

    # Deduplicate identical camera intrinsics so InterfaceCOLMAP's ReadCamerasText works cleanly
    cam_lines: list[str] = []
    cam_id_map: dict[int, int] = {}
    unique_cam_specs: list[tuple[int, int, float, float, float, float]] = []
    for idx0, cid in enumerate(cam_ids):
        cam = cameras[cid]
        spec = (
            int(cam.width),
            int(cam.height),
            round(float(cam.K[0, 0]), 6),
            round(float(cam.K[1, 1]), 6),
            round(float(cam.K[0, 2]), 6),
            round(float(cam.K[1, 2]), 6),
        )
        if spec in unique_cam_specs:
            cam_id_map[idx0] = unique_cam_specs.index(spec) + 1
        else:
            unique_cam_specs.append(spec)
            cid_1 = len(unique_cam_specs)
            cam_id_map[idx0] = cid_1
            cam_lines.append(
                f"{cid_1} PINHOLE {spec[0]} {spec[1]} {spec[2]:.8f} {spec[3]:.8f} {spec[4]:.8f} {spec[5]:.8f}\n"
            )

    img_lines: list[str] = []
    for idx0, cid in enumerate(cam_ids):
        cam = cameras[cid]
        img_id_1 = idx0 + 1
        colmap_cam_id = cam_id_map[idx0]
        img_name = f"{cid}.jpg"
        img_path = images_dir / img_name
        _write_rgb_image(img_path, cam.image_rgb)

        qw, qx, qy, qz = rotmat_to_quaternion_wxyz(cam.R_w2c)
        tx, ty, tz = float(cam.t_w2c[0]), float(cam.t_w2c[1]), float(cam.t_w2c[2])
        img_lines.append(
            f"{img_id_1} {qw:.10f} {qx:.10f} {qy:.10f} {qz:.10f} "
            f"{tx:.10f} {ty:.10f} {tz:.10f} {colmap_cam_id} {img_name}\n"
        )
        if img_points2d[idx0]:
            p2d_tokens = [f"{u:.2f} {v:.2f} {pid}" for u, v, pid in img_points2d[idx0]]
            img_lines.append(" ".join(p2d_tokens) + "\n")
        else:
            img_lines.append(f"{0.5 * cam.width:.2f} {0.5 * cam.height:.2f} 1\n")

    pt_lines: list[str] = []
    for pt_idx in range(n_pts):
        if not pt_tracks[pt_idx]:
            continue
        pt_id_1 = pt_idx + 1
        x, y, z = float(pts_xyz[pt_idx, 0]), float(pts_xyz[pt_idx, 1]), float(pts_xyz[pt_idx, 2])
        r, g, b = int(pts_rgb[pt_idx, 0]), int(pts_rgb[pt_idx, 1]), int(pts_rgb[pt_idx, 2])
        track_str = " ".join(f"{im_id} {p2d_i}" for im_id, p2d_i in pt_tracks[pt_idx])
        pt_lines.append(f"{pt_id_1} {x:.6f} {y:.6f} {z:.6f} {r} {g} {b} 0.1 {track_str}\n")

    cameras_txt = sparse_dir / "cameras.txt"
    images_txt = sparse_dir / "images.txt"
    points3d_txt = sparse_dir / "points3D.txt"
    cameras_txt.write_bytes("".join(cam_lines).encode("utf-8"))
    images_txt.write_bytes("".join(img_lines).encode("utf-8"))
    points3d_txt.write_bytes("".join(pt_lines).encode("utf-8"))

    return {
        "workspace": out_dir,
        "sparse_dir": sparse_dir,
        "images_dir": images_dir,
        "cameras_txt": cameras_txt,
        "images_txt": images_txt,
        "points3d_txt": points3d_txt,
    }


def _pack_mvs_string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _unpack_mvs_string(buf: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<Q", buf, offset)
    offset += 8
    s = buf[offset : offset + length].decode("utf-8")
    return s, offset + length


def write_openmvs_mvs_file(
    path: Path,
    cameras: dict[str, UndistortedCamera],
    image_paths_in_scene: dict[str, str] | None = None,
    points_xyz: np.ndarray | None = None,
    points_rgb: np.ndarray | None = None,
    point_views: list[list[tuple[int, float, float, float]]] | None = None,
) -> Path:
    """Directly serialize an OpenMVS `.mvs` binary scene (`InterfaceMVS` format).

    Matches the exact byte layout produced by `InterfaceCOLMAP` in `simai/openmvs:fcb172bd8490`.
    """
    cam_ids = list(cameras.keys())
    n_cams = len(cam_ids)
    chunks: list[bytes] = []

    # 1. Platforms (1 per camera)
    chunks.append(struct.pack("<Q", n_cams))
    for idx0, cid in enumerate(cam_ids):
        cam = cameras[cid]
        plat_name = f"platform{idx0 + 1:03d}"
        chunks.append(_pack_mvs_string(plat_name))
        # cameras vector (size 1)
        chunks.append(struct.pack("<Q", 1))
        chunks.append(_pack_mvs_string("PINHOLE"))
        scale = float(max(cam.width, cam.height))
        K_norm = np.array(
            [
                [float(cam.K[0, 0]) / scale, 0.0, float(cam.K[0, 2]) / scale],
                [0.0, float(cam.K[1, 1]) / scale, float(cam.K[1, 2]) / scale],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        R_rel = np.eye(3, dtype=np.float64)
        C_rel = np.zeros(3, dtype=np.float64)
        chunks.append(K_norm.tobytes(order="C"))
        chunks.append(R_rel.tobytes(order="C"))
        chunks.append(C_rel.tobytes(order="C"))

        # poses vector (size 1): R_w2c (3x3 float64) and C_w = -R_w2c.T @ t_w2c (3 float64)
        chunks.append(struct.pack("<Q", 1))
        R_pose = np.ascontiguousarray(cam.R_w2c, dtype=np.float64)
        C_pose = np.ascontiguousarray(cam.center_world, dtype=np.float64)
        chunks.append(R_pose.tobytes(order="C"))
        chunks.append(C_pose.tobytes(order="C"))

    # 2. Images
    chunks.append(struct.pack("<Q", n_cams))
    for idx0, cid in enumerate(cam_ids):
        img_ref = (
            image_paths_in_scene[cid]
            if image_paths_in_scene and cid in image_paths_in_scene
            else f"images/{cid}.jpg"
        )
        chunks.append(_pack_mvs_string(img_ref))
        # platformID, cameraID, poseID (uint32 each)
        chunks.append(struct.pack("<III", idx0, 0, 0))

    # 3. Vertices + Views + Normals + Colors
    pts_xyz = (
        np.asarray(points_xyz, dtype=np.float32)
        if points_xyz is not None
        else np.zeros((0, 3), dtype=np.float32)
    )
    n_pts = pts_xyz.shape[0]
    pts_rgb = (
        np.asarray(points_rgb, dtype=np.uint8)
        if points_rgb is not None and len(points_rgb) == n_pts
        else np.full((n_pts, 3), 180, dtype=np.uint8)
    )
    views_list = (
        point_views
        if point_views is not None and len(point_views) == n_pts
        else [[] for _ in range(n_pts)]
    )

    valid_indices = [i for i in range(n_pts) if len(views_list[i]) > 0]
    chunks.append(struct.pack("<Q", len(valid_indices)))
    for i in valid_indices:
        x, y, z = float(pts_xyz[i, 0]), float(pts_xyz[i, 1]), float(pts_xyz[i, 2])
        chunks.append(struct.pack("<fff", x, y, z))
        vlist = sorted(views_list[i], key=lambda item: int(item[0]))
        chunks.append(struct.pack("<Q", len(vlist)))
        for cam_idx, _u, _v, conf in vlist:
            chunks.append(struct.pack("<If", int(cam_idx), float(conf)))

    # verticesNormal (0)
    chunks.append(struct.pack("<Q", 0))
    # verticesColor (BGR uint8[3] per vertex)
    chunks.append(struct.pack("<Q", len(valid_indices)))
    if valid_indices:
        rgb_sub = pts_rgb[valid_indices]
        bgr_sub = np.ascontiguousarray(rgb_sub[:, ::-1], dtype=np.uint8)
        chunks.append(bgr_sub.tobytes(order="C"))

    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"".join(chunks))
    return out_path


def read_openmvs_mvs_summary(path: Path) -> dict[str, Any]:
    """Parse an OpenMVS `.mvs` binary scene file and return summary metadata for verification."""
    buf = Path(path).read_bytes()
    off = 0
    (n_platforms,) = struct.unpack_from("<Q", buf, off)
    off += 8
    platforms: list[dict[str, Any]] = []
    for _ in range(n_platforms):
        p_name, off = _unpack_mvs_string(buf, off)
        (n_cams,) = struct.unpack_from("<Q", buf, off)
        off += 8
        cams = []
        for _ in range(n_cams):
            c_name, off = _unpack_mvs_string(buf, off)
            K_norm = np.frombuffer(buf, dtype=np.float64, count=9, offset=off).reshape(3, 3).copy()
            off += 72
            R_rel = np.frombuffer(buf, dtype=np.float64, count=9, offset=off).reshape(3, 3).copy()
            off += 72
            C_rel = np.frombuffer(buf, dtype=np.float64, count=3, offset=off).copy()
            off += 24
            cams.append({"name": c_name, "K_norm": K_norm, "R_rel": R_rel, "C_rel": C_rel})
        (n_poses,) = struct.unpack_from("<Q", buf, off)
        off += 8
        poses = []
        for _ in range(n_poses):
            R_pose = np.frombuffer(buf, dtype=np.float64, count=9, offset=off).reshape(3, 3).copy()
            off += 72
            C_pose = np.frombuffer(buf, dtype=np.float64, count=3, offset=off).copy()
            off += 24
            poses.append({"R_w2c": R_pose, "C_world": C_pose})
        platforms.append({"name": p_name, "cameras": cams, "poses": poses})

    (n_images,) = struct.unpack_from("<Q", buf, off)
    off += 8
    images: list[dict[str, Any]] = []
    for _ in range(n_images):
        im_name, off = _unpack_mvs_string(buf, off)
        plat_id, cam_id, pose_id = struct.unpack_from("<III", buf, off)
        off += 12
        images.append(
            {"name": im_name, "platform_id": plat_id, "camera_id": cam_id, "pose_id": pose_id}
        )

    (n_vertices,) = struct.unpack_from("<Q", buf, off)
    off += 8
    total_views = 0
    first_vertex_xyz = None
    for v_i in range(n_vertices):
        x, y, z = struct.unpack_from("<fff", buf, off)
        if v_i == 0:
            first_vertex_xyz = (x, y, z)
        off += 12
        (n_views,) = struct.unpack_from("<Q", buf, off)
        off += 8 + 8 * n_views
        total_views += int(n_views)

    (n_normals,) = struct.unpack_from("<Q", buf, off)
    off += 8 + 12 * n_normals
    (n_colors,) = struct.unpack_from("<Q", buf, off)
    off += 8 + 3 * n_colors

    return {
        "num_platforms": int(n_platforms),
        "num_images": int(n_images),
        "num_vertices": int(n_vertices),
        "num_colors": int(n_colors),
        "total_vertex_views": int(total_views),
        "first_vertex_xyz": first_vertex_xyz,
        "platforms": platforms,
        "images": images,
        "bytes_consumed": int(off),
        "file_size": len(buf),
    }


def adapt_mvsi_wide_baseline_neighbors(
    mvsi_in_path: Path,
    mvsi_out_path: Path | None = None,
    *,
    n_cams: int = 4,
    min_shared_points: int = 500,
    max_wide_angle_rad: float = 1.65,
    clamped_angle_rad: float = 0.70,
) -> dict[str, Any]:
    """Adapt neighbor-view angles in an OpenMVS internal (`MVS\\x00`) archive for wide-baseline corner cameras.

    Technical rationale:
    - OpenMVS `RefineMesh` (`MeshRefine::ThSelectNeighbors` -> `MVS::Scene::FilterNeighborViews`)
      filters `image.neighbors` using a hardcoded narrow-baseline threshold `fMaxAngle = 45.0 deg`
      (`0.785398 rad`, `.rodata` `0x233730` in `simai/openmvs:fcb172bd8490`).
    - In a 4-camera room-corner CCTV rig (`cam_A..D` at ~90 deg perimeter intervals), adjacent
      corner cameras observe shared room surfaces at median parallax angles of 60--80 deg
      (`1.05--1.40 rad`), which `FilterNeighborViews` would otherwise reject (`neighborsMap` empty).
    - This helper decompresses the zlib payload of an internal `MVS\\x00` scene archive produced by
      `ReconstructMesh`, clamps `ViewInfo.angle` (`<= max_wide_angle_rad`) into `clamped_angle_rad`
      (`< 0.7854 rad`) for adjacent camera pairs with `>= min_shared_points` co-visible points,
      and recompresses the archive.
    """
    mvsi_in_path = Path(mvsi_in_path)
    out_path = Path(mvsi_out_path) if mvsi_out_path is not None else mvsi_in_path
    raw = mvsi_in_path.read_bytes()
    if len(raw) < 20 or raw[:4] != b"MVS\x00":
        raise ValueError(f"Expected OpenMVS internal archive ('MVS\\x00'), got {raw[:4]!r} in {mvsi_in_path}")

    hdr = raw[:20]
    decomp = bytearray(zlib.decompress(raw[20:]))
    scan_limit = min(len(decomp) - 20, 65536)
    updated_pairs = 0
    for off in range(0, max(0, scan_limit)):
        id_val, pts_val, scl, ang, scr = struct.unpack_from("<IIfff", decomp, off)
        if (
            0 <= id_val < n_cams
            and min_shared_points <= pts_val <= 5_000_000
            and 0.25 <= scl <= 4.0
            and 0.78 <= ang <= max_wide_angle_rad
            and 0.05 <= scr <= 10.0
        ):
            new_scl = min(max(float(scl), 0.5), 2.5)
            new_ang = min(float(ang), float(clamped_angle_rad))
            new_scr = max(float(scr), 0.25)
            struct.pack_into("<IIfff", decomp, off, id_val, pts_val, new_scl, new_ang, new_scr)
            updated_pairs += 1

    comp = zlib.compress(bytes(decomp))
    out_hdr = hdr[:12] + struct.pack("<Q", len(decomp))
    out_path.write_bytes(out_hdr + comp)
    return {
        "mvsi_path": str(out_path),
        "updated_neighbor_entries": int(updated_pairs),
        "uncompressed_bytes": len(decomp),
    }


def write_texrecon_scene(
    scene_dir: Path,
    cameras: dict[str, UndistortedCamera],
    active_cam_ids: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Write `<cid>.cam` and `<cid>.jpg` for `texrecon` (`mvs-texturing`)."""
    scene_dir = Path(scene_dir)
    if scene_dir.exists():
        shutil.rmtree(scene_dir)
    scene_dir.mkdir(parents=True, exist_ok=True)
    cam_ids = list(active_cam_ids) if active_cam_ids is not None else list(cameras.keys())
    for cid in cam_ids:
        cam = cameras[cid]
        cv2.imwrite(
            str(scene_dir / f"{cid}.jpg"),
            cv2.cvtColor(cam.image_rgb, cv2.COLOR_RGB2BGR),
            [int(cv2.IMWRITE_JPEG_QUALITY), 95],
        )
        f_norm = float(cam.K[0, 0]) / float(max(cam.width, cam.height))
        paspect = float(cam.K[1, 1]) / float(cam.K[0, 0])
        ppx = float(cam.K[0, 2]) / float(cam.width)
        ppy = float(cam.K[1, 2]) / float(cam.height)
        r_flat = " ".join(f"{float(v):.10g}" for v in cam.R_w2c.reshape(-1))
        t_flat = " ".join(f"{float(v):.10g}" for v in cam.t_w2c.reshape(-1))
        cam_txt = f"{t_flat} {r_flat}\n{f_norm:.10g} 0 0 {paspect:.10g} {ppx:.10g} {ppy:.10g}\n"
        (scene_dir / f"{cid}.cam").write_text(cam_txt, encoding="utf-8")


def _is_file_quiet(path: Path) -> bool:
    """`path.is_file()` 이되, 읽을 권한이 없는 후보(예: 일반 사용자에게 /root/...)는 없는 것으로 본다."""
    try:
        return path.is_file()
    except OSError:
        return False


def find_native_openmvs_binary(bin_name: str) -> str | None:
    """Locate a native OpenMVS binary (`TextureMesh`, `RefineMesh`, `ReconstructMesh`, `InterfaceCOLMAP`) on the host/Pod.

    Checks `SIMAI_OPENMVS_BIN_DIR`, `PATH`, `/usr/local/bin/OpenMVS`, and common conda/micromamba prefixes.
    Returns `None` when not installed natively (so the caller falls back to `docker` on laptop).
    """
    import os

    search_dirs: list[Path] = []
    env_dir = os.environ.get("SIMAI_OPENMVS_BIN_DIR", "").strip()
    if env_dir:
        search_dirs.append(Path(env_dir))
    conda_prefix = os.environ.get("CONDA_PREFIX", "").strip()
    if conda_prefix:
        search_dirs.extend([Path(conda_prefix) / "bin" / "OpenMVS", Path(conda_prefix) / "bin"])
    search_dirs.extend(
        [
            Path("/usr/local/bin/OpenMVS"),
            Path("/usr/local/bin"),
            Path("/opt/conda/envs/openmvs/bin/OpenMVS"),
            Path("/opt/conda/envs/openmvs/bin"),
            Path("/root/micromamba/envs/openmvs/bin/OpenMVS"),
            Path("/root/micromamba/envs/openmvs/bin"),
        ]
    )
    for d in search_dirs:
        cand = d / bin_name
        if _is_file_quiet(cand):
            return str(cand)
    which_hit = shutil.which(bin_name)
    if which_hit:
        return which_hit
    return None


def openmvs_scene_image_path(work_dir: Path, rel_posix: str) -> str:
    """Return image path string to store inside `scene.mvs` (native path on Pod if native OpenMVS exists, else `/work/...`)."""
    rel_clean = rel_posix.lstrip("/")
    if rel_clean.startswith("work/"):
        rel_clean = rel_clean[len("work/") :]
    if find_native_openmvs_binary("TextureMesh") is not None:
        return (Path(work_dir).resolve() / rel_clean).as_posix()
    return f"/work/{rel_clean}"


def run_openmvs_cmd(
    work_dir: Path,
    cmd_args: list[str],
    *,
    docker_image: str = OPENMVS_DOCKER_IMAGE,
    timeout_s: int = 900,
) -> dict[str, Any]:
    """Run an OpenMVS CLI binary natively when installed (e.g. inside a RunPod GPU Pod via conda-forge `openmvs`),
    or fall back to `docker` (`simai/openmvs:fcb172bd8490`) on laptop.
    """
    work_dir = Path(work_dir).resolve()
    mount_src = str(work_dir).replace("\\", "/")
    native_bin = find_native_openmvs_binary(cmd_args[0]) if cmd_args else None

    if native_bin is not None:
        def _map_arg(a: str) -> str:
            if a == "/work":
                return mount_src
            if a.startswith("/work/"):
                return f"{mount_src}/{a[len('/work/'):]}"
            return a

        full_cmd = [native_bin, *[_map_arg(a) for a in cmd_args[1:]]]
        exec_mode = "native"
    else:
        full_cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{mount_src}:/work",
            "-w",
            "/work",
            docker_image,
            "sh",
            "-c",
            "export PATH=$PATH:/usr/local/bin/OpenMVS && " + " ".join(cmd_args),
        ]
        exec_mode = "docker"

    t0 = time.perf_counter()
    proc = subprocess.run(
        full_cmd,
        cwd=str(work_dir) if native_bin is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    elapsed = round(time.perf_counter() - t0, 3)
    if proc.returncode != 0:
        raise RuntimeError(
            f"OpenMVS command failed ({exec_mode}, rc={proc.returncode}, {elapsed}s): {' '.join(cmd_args)}\n"
            f"STDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
        )
    return {
        "cmd": cmd_args,
        "exec_mode": exec_mode,
        "seconds": elapsed,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-1500:],
    }


def parse_openmvs_obj_and_export_glb(
    obj_path: Path,
    full_V_world: np.ndarray,
    full_F: np.ndarray,
    out_glb_path: Path,
    *,
    empty_rgb: tuple[int, int, int] = (128, 128, 128),
    color_tol: int = 4,
) -> dict[str, Any]:
    """Convert OpenMVS `TextureMesh` OBJ+MTL+PNG output into a provenance-aware GLB (`GLTF_Y_UP`).

    - Faces whose sampled texture patch color is NOT the `empty_rgb` placeholder are exported
      as `observed` textured primitive(s).
    - Faces covered by `empty_rgb` (or missing from the textured OBJ) are exported as an
      `unobserved` untextured primitive with neutral gray `baseColorFactor=[0.5, 0.5, 0.5, 1.0]`
      (AGENTS.md Rule 2: never hallucinate unobserved geometry/colors).
    - World coordinates `(x, y, z)` (+Z up) are converted in ONE place via `tm.to_glb_points`
      to `GLTF_Y_UP` `(x, z, -y)`.
    """
    obj_path = Path(obj_path)
    work_dir = obj_path.parent
    vs: list[list[float]] = []
    vts: list[list[float]] = []
    mtl_file: str | None = None
    cur_mtl = "default"
    faces_by_mtl: dict[str, list[tuple[tuple[int, int, int], tuple[int, int, int]]]] = {}

    for raw_line in obj_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("mtllib "):
            mtl_file = line.split(None, 1)[1].strip()
        elif line.startswith("usemtl "):
            cur_mtl = line.split(None, 1)[1].strip()
        elif line.startswith("v "):
            parts = line.split()
            vs.append([float(parts[1]), float(parts[2]), float(parts[3])])
        elif line.startswith("vt "):
            parts = line.split()
            vts.append([float(parts[1]), float(parts[2])])
        elif line.startswith("f "):
            parts = line.split()[1:]
            if len(parts) != 3:
                continue
            vi_list: list[int] = []
            ti_list: list[int] = []
            for p in parts:
                toks = p.split("/")
                vi_list.append(int(toks[0]) - 1)
                ti_list.append(int(toks[1]) - 1 if len(toks) > 1 and toks[1] else 0)
            faces_by_mtl.setdefault(cur_mtl, []).append((tuple(vi_list), tuple(ti_list)))  # type: ignore[arg-type]

    mtl_map: dict[str, Path] = {}
    if mtl_file and (work_dir / mtl_file).is_file():
        m_name = None
        for raw_line in (work_dir / mtl_file).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if line.startswith("newmtl "):
                m_name = line.split(None, 1)[1].strip()
            elif line.startswith("map_Kd ") and m_name:
                tex_rel = line.split(None, 1)[1].strip()
                mtl_map[m_name] = work_dir / tex_rel

    V_obj_world = np.asarray(vs, dtype=np.float32) if vs else np.asarray(full_V_world, dtype=np.float32)
    V_obj_yup = to_glb_points(V_obj_world)
    VT_obj = np.asarray(vts, dtype=np.float32) if vts else np.zeros((1, 2), dtype=np.float32)

    bin_chunks: list[bytes] = []
    buffer_views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []
    images_gltf: list[dict[str, Any]] = []
    textures_gltf: list[dict[str, Any]] = []
    materials_gltf: list[dict[str, Any]] = []
    primitives_gltf: list[dict[str, Any]] = []

    def _append_bv(data: bytes, target: int | None = None) -> int:
        pad = (4 - (len(data) % 4)) % 4
        offset = sum(len(c) for c in bin_chunks)
        bin_chunks.append(data + (b"\x00" * pad))
        bv: dict[str, Any] = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            bv["target"] = target
        buffer_views.append(bv)
        return len(buffer_views) - 1

    def _append_acc(
        bv_idx: int, count: int, comp_type: int, acc_type: str, vmin: list[float] | None = None, vmax: list[float] | None = None
    ) -> int:
        acc: dict[str, Any] = {
            "bufferView": bv_idx,
            "byteOffset": 0,
            "componentType": comp_type,
            "count": count,
            "type": acc_type,
        }
        if vmin is not None:
            acc["min"] = vmin
            acc["max"] = vmax
        accessors.append(acc)
        return len(accessors) - 1

    observed_faces_count = 0
    unobserved_tris_yup: list[np.ndarray] = []
    observed_area_m2 = 0.0
    unobserved_area_m2 = 0.0
    covered_obj_tris_world: list[np.ndarray] = []

    for mtl_name, flist in faces_by_mtl.items():
        if not flist:
            continue
        v_idx = np.array([f[0] for f in flist], dtype=np.int32)
        vt_idx = np.array([f[1] for f in flist], dtype=np.int32)
        covered_obj_tris_world.append(V_obj_world[v_idx])

        tex_path = mtl_map.get(mtl_name)
        tex_rgb = _read_rgb_image(tex_path) if (tex_path is not None and tex_path.is_file()) else None

        if tex_rgb is None:
            tris_yup = V_obj_yup[v_idx]
            tris_w = V_obj_world[v_idx]
            areas = 0.5 * np.linalg.norm(
                np.cross(tris_w[:, 1] - tris_w[:, 0], tris_w[:, 2] - tris_w[:, 0]), axis=1
            )
            unobserved_area_m2 += float(areas.sum())
            unobserved_tris_yup.append(tris_yup)
            continue

        th, tw = tex_rgb.shape[:2]
        uv_tri = VT_obj[vt_idx]  # (M, 3, 2) in OBJ UV (bottom-left origin)
        uv_cent = uv_tri.mean(axis=1)
        px_x = np.clip(np.rint(uv_cent[:, 0] * (tw - 1)).astype(np.int32), 0, tw - 1)
        px_y = np.clip(np.rint((1.0 - uv_cent[:, 1]) * (th - 1)).astype(np.int32), 0, th - 1)
        c_cent = tex_rgb[px_y, px_x].astype(np.int32)

        # Also check the 3 triangle vertices in UV space
        vx_x = np.clip(np.rint(uv_tri[:, :, 0] * (tw - 1)).astype(np.int32), 0, tw - 1)
        vx_y = np.clip(np.rint((1.0 - uv_tri[:, :, 1]) * (th - 1)).astype(np.int32), 0, th - 1)
        c_verts = tex_rgb[vx_y, vx_x].astype(np.int32)  # (M, 3, 3)

        empty_arr = np.array(empty_rgb, dtype=np.int32)
        is_empty_cent = np.all(np.abs(c_cent - empty_arr[None, :]) <= color_tol, axis=1)
        is_empty_verts = np.all(
            np.all(np.abs(c_verts - empty_arr[None, None, :]) <= color_tol, axis=2), axis=1
        )
        is_unobs = is_empty_cent & is_empty_verts

        tris_w = V_obj_world[v_idx]
        areas = 0.5 * np.linalg.norm(
            np.cross(tris_w[:, 1] - tris_w[:, 0], tris_w[:, 2] - tris_w[:, 0]), axis=1
        )

        if bool(is_unobs.any()):
            unobs_v = v_idx[is_unobs]
            unobserved_tris_yup.append(V_obj_yup[unobs_v])
            unobserved_area_m2 += float(areas[is_unobs].sum())

        obs_mask = ~is_unobs
        if not bool(obs_mask.any()):
            continue

        obs_v = v_idx[obs_mask]
        obs_vt = vt_idx[obs_mask]
        observed_faces_count += int(obs_v.shape[0])
        observed_area_m2 += float(areas[obs_mask].sum())

        n_obs = obs_v.shape[0]
        pos = np.ascontiguousarray(V_obj_yup[obs_v].reshape(-1, 3), dtype=np.float32)
        uv = VT_obj[obs_vt].reshape(-1, 2).copy()
        uv[:, 1] = 1.0 - uv[:, 1]  # OBJ -> glTF UV
        uv = np.ascontiguousarray(uv, dtype=np.float32)
        idx_arr = np.arange(n_obs * 3, dtype=np.uint32)

        pos_bv = _append_bv(pos.tobytes(), 34962)
        pos_acc = _append_acc(
            pos_bv,
            len(pos),
            5126,
            "VEC3",
            pos.min(axis=0).tolist(),
            pos.max(axis=0).tolist(),
        )
        uv_bv = _append_bv(uv.tobytes(), 34962)
        uv_acc = _append_acc(uv_bv, len(uv), 5126, "VEC2")
        idx_bv = _append_bv(idx_arr.tobytes(), 34963)
        idx_acc = _append_acc(idx_bv, len(idx_arr), 5125, "SCALAR")

        # Encode texture as PNG if not already PNG
        if tex_path is not None and tex_path.suffix.lower() == ".png":
            img_bytes = tex_path.read_bytes()
        else:
            img_bytes = _encode_rgb_png_bytes(tex_rgb)

        img_bv = _append_bv(img_bytes, None)
        images_gltf.append({"bufferView": img_bv, "mimeType": "image/png", "name": mtl_name})
        textures_gltf.append({"source": len(images_gltf) - 1, "sampler": 0})
        materials_gltf.append(
            {
                "name": mtl_name,
                "doubleSided": True,
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": len(textures_gltf) - 1},
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
            }
        )
        primitives_gltf.append(
            {
                "attributes": {"POSITION": pos_acc, "TEXCOORD_0": uv_acc},
                "indices": idx_acc,
                "material": len(materials_gltf) - 1,
                "mode": 4,
            }
        )

    # Include any faces from (full_V_world, full_F) omitted from the OBJ (e.g. texrecon --skip_hole_filling)
    total_obj_faces = sum(len(fl) for fl in faces_by_mtl.values())
    if full_F is not None and len(full_F) > total_obj_faces and covered_obj_tris_world:
        obj_tris_w = np.concatenate(covered_obj_tris_world, axis=0)
        c_obj = np.round(obj_tris_w.mean(axis=1) * 1e4).astype(np.int64)
        full_tris_w = np.asarray(full_V_world, dtype=np.float32)[np.asarray(full_F, dtype=np.int32)]
        c_full = np.round(full_tris_w.mean(axis=1) * 1e4).astype(np.int64)
        obj_keys = { (int(row[0]), int(row[1]), int(row[2])) for row in c_obj }
        missing_mask = np.array(
            [(int(r[0]), int(r[1]), int(r[2])) not in obj_keys for r in c_full],
            dtype=bool,
        )
        if bool(missing_mask.any()):
            miss_w = full_tris_w[missing_mask]
            miss_areas = 0.5 * np.linalg.norm(
                np.cross(miss_w[:, 1] - miss_w[:, 0], miss_w[:, 2] - miss_w[:, 0]), axis=1
            )
            unobserved_area_m2 += float(miss_areas.sum())
            full_V_yup = to_glb_points(np.asarray(full_V_world, dtype=np.float32))
            unobserved_tris_yup.append(full_V_yup[np.asarray(full_F, dtype=np.int32)[missing_mask]])

    unobserved_faces_count = 0
    if unobserved_tris_yup:
        unobs_all = np.concatenate(unobserved_tris_yup, axis=0)
        unobserved_faces_count = int(unobs_all.shape[0])
        pos_u = np.ascontiguousarray(unobs_all.reshape(-1, 3), dtype=np.float32)
        idx_u = np.arange(unobserved_faces_count * 3, dtype=np.uint32)
        pos_bv = _append_bv(pos_u.tobytes(), 34962)
        pos_acc = _append_acc(
            pos_bv,
            len(pos_u),
            5126,
            "VEC3",
            pos_u.min(axis=0).tolist(),
            pos_u.max(axis=0).tolist(),
        )
        idx_bv = _append_bv(idx_u.tobytes(), 34963)
        idx_acc = _append_acc(idx_bv, len(idx_u), 5125, "SCALAR")
        materials_gltf.append(
            {
                "name": "unobserved",
                "doubleSided": True,
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.5, 0.5, 0.5, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
            }
        )
        primitives_gltf.append(
            {
                "attributes": {"POSITION": pos_acc},
                "indices": idx_acc,
                "material": len(materials_gltf) - 1,
                "mode": 4,
            }
        )

    bin_blob = b"".join(bin_chunks)
    gltf_doc: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "simai-geometry-bench-openmvs"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "scene"}],
        "meshes": [{"name": "scene", "primitives": primitives_gltf}],
        "materials": materials_gltf,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_blob)}],
        "extras": {
            "simai": {
                "output_frame": "WORLD",
                "glb_axis_convention": "GLTF_Y_UP",
                "surface_provenance": {
                    "observed_faces": observed_faces_count,
                    "interpolated_faces": 0,
                    "unobserved_faces": unobserved_faces_count,
                },
            }
        },
    }
    if textures_gltf:
        gltf_doc["samplers"] = [
            {"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}
        ]
        gltf_doc["textures"] = textures_gltf
        gltf_doc["images"] = images_gltf

    json_bytes = json.dumps(gltf_doc, separators=(",", ":")).encode("utf-8")
    json_pad = (4 - (len(json_bytes) % 4)) % 4
    json_chunk = json_bytes + (b" " * json_pad)

    total_len = 12 + 8 + len(json_chunk) + 8 + len(bin_blob)
    header = struct.pack("<III", 0x46546C67, 2, total_len)
    chunk0 = struct.pack("<II", len(json_chunk), 0x4E4F534A) + json_chunk
    chunk1 = struct.pack("<II", len(bin_blob), 0x004E4942) + bin_blob

    out_glb_path = Path(out_glb_path)
    out_glb_path.parent.mkdir(parents=True, exist_ok=True)
    out_glb_path.write_bytes(header + chunk0 + chunk1)

    total_area = max(observed_area_m2 + unobserved_area_m2, 1e-9)
    return {
        "glb_path": str(out_glb_path),
        "glb_bytes": int(total_len),
        "total_faces": int(observed_faces_count + unobserved_faces_count),
        "observed_faces": int(observed_faces_count),
        "interpolated_faces": 0,
        "unobserved_faces": int(unobserved_faces_count),
        "area_m2": round(float(observed_area_m2 + unobserved_area_m2), 3),
        "area_ratio_observed": round(float(observed_area_m2 / total_area), 5),
        "area_ratio_interpolated": 0.0,
        "area_ratio_unobserved": round(float(unobserved_area_m2 / total_area), 5),
    }


def load_scene_mesh_world_from_glb(glb_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Load `(V_world, F)` in World meters (+Z up) from a scene GLB, skipping `estimated_*` nodes."""
    raw = Path(glb_path).read_bytes()
    magic, _version, _total = struct.unpack_from("<III", raw, 0)
    if magic != 0x46546C67:
        raise ValueError(f"Not a GLB file: {glb_path}")
    j_len, j_type = struct.unpack_from("<II", raw, 12)
    if j_type != 0x4E4F534A:
        raise ValueError("Missing JSON chunk in GLB")
    doc = json.loads(raw[20 : 20 + j_len].decode("utf-8"))
    b_off = 20 + j_len
    b_len, _b_type = struct.unpack_from("<II", raw, b_off)
    bin_blob = raw[b_off + 8 : b_off + 8 + b_len]

    comp_dtype = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
    type_cnt = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}

    def _read_acc(idx: int) -> np.ndarray:
        acc = doc["accessors"][idx]
        bv = doc["bufferViews"][acc["bufferView"]]
        dt = np.dtype(comp_dtype[acc["componentType"]])
        n_comp = type_cnt[acc["type"]]
        start = int(bv.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
        cnt = int(acc["count"])
        arr = np.frombuffer(bin_blob, dtype=dt, count=cnt * n_comp, offset=start)
        return arr.reshape(cnt, n_comp) if n_comp > 1 else arr

    vs_yup: list[np.ndarray] = []
    fs_list: list[np.ndarray] = []
    v_offset = 0

    nodes = doc.get("nodes", [])
    meshes = doc.get("meshes", [])
    for node in nodes:
        name = str(node.get("name", ""))
        if name.startswith("estimated_") or name.startswith("completed_"):
            continue
        m_idx = node.get("mesh")
        if m_idx is None:
            continue
        for prim in meshes[m_idx].get("primitives", []):
            if prim.get("mode", 4) != 4:
                continue
            pos = _read_acc(prim["attributes"]["POSITION"]).astype(np.float64)
            if "indices" in prim:
                tri = _read_acc(prim["indices"]).astype(np.int32).reshape(-1, 3)
            else:
                tri = np.arange(len(pos), dtype=np.int32).reshape(-1, 3)
            vs_yup.append(pos)
            fs_list.append(tri + v_offset)
            v_offset += len(pos)

    if not vs_yup:
        raise ValueError(f"No scene mesh primitives found in {glb_path}")

    V_yup = np.vstack(vs_yup)
    F_all = np.vstack(fs_list)
    V_world = from_glb_points(V_yup)
    V_q = np.round(V_world * 1e6).astype(np.int64)
    _, unique_idx, inv_idx = np.unique(V_q, axis=0, return_index=True, return_inverse=True)
    V_clean = V_world[unique_idx].astype(np.float64)
    F_clean = inv_idx[F_all].astype(np.int32)
    non_deg = (
        (F_clean[:, 0] != F_clean[:, 1])
        & (F_clean[:, 1] != F_clean[:, 2])
        & (F_clean[:, 2] != F_clean[:, 0])
    )
    return V_clean, F_clean[non_deg]
