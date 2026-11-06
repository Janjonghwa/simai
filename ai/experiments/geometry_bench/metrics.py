"""Evaluation metrics and standardized viewpoint rendering for geometry_bench (S15P31A101-528).

Reuses `-314` (`simai_ai.scene.texture_mesh` and `simai_ai.scene.mesh_cleanup`) metrics first:
- Self-view PSNR (`psnr_db`, `psnr_db_blur8`), SSIM (`ssim`), RGB MAE, coverage
- Leave-One-Out (LOO) 3-camera -> held-out 4th camera PSNR (`psnr_db`, `psnr_db_blur8`), SSIM, coverage
- Cross-view depth inconsistency (`abs_rel_p50`, `abs_rel_p90`, `within_1pct`, `p2p_m_p50`, `p2p_m_p90`)
  via `mesh_cleanup.cross_view_distances`
- Edge sharpness (photo edge vs rendered edge):
  - `grad_cos_sim`: Sobel gradient magnitude cosine similarity inside eroded observed mask
  - `grad_mag_ratio`: rendered / photo Sobel gradient magnitude ratio
  - `edge_f1_px2`: Canny edge F1 score at <= 2 px tolerance
  - `edge_chamfer_px`: symmetric Chamfer distance (px) between photo and rendered Canny edges
  - `depth_edge_photo_dist_px`: mean distance (px) from rendered 3D depth discontinuities to photo Canny edges
- Mesh face count, vertex count, surface area (m^2), GLB byte size, and stage wall-clock times
- Standardized renders at `cam_A..D`, `top`, `obl1`, `obl2`, `obl3`
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import binary_erosion, distance_transform_edt, sobel

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

from simai_ai.scene import mesh_cleanup as mc
from simai_ai.scene import texture_mesh as tm
from simai_ai.scene import texture_provenance as tp

from .mvs_io import UndistortedCamera, _decode_png_bytes_rgb, from_glb_points

STANDARD_FREE_VIEWS: dict[str, tuple[list[float], list[float], float]] = {
    "top": ([4.1, 4.2, 9.5], [4.1, 4.15, 0.0], 70.0),
    "obl1": ([4.0, 4.0, 3.6], [0.3, 9.0, 0.3], 80.0),
    "obl2": ([4.0, 4.0, 3.6], [8.2, 1.0, 0.4], 80.0),
    "obl3": ([4.0, 5.0, 3.0], [1.0, 0.2, 0.5], 80.0),
}


def parse_glb_primitives_world(glb_path: Path) -> dict[str, Any]:
    """Parse a `.glb` file (`GLTF_Y_UP`) into World-frame triangles (`+Z` up), UVs, textures, and face provenance.

    Returns a dict with:
      - `V_world`: `(N_v, 3)` float32 (concatenated per-triangle-vertex positions in World meters)
      - `F`: `(N_f, 3)` int32 triangle indices
      - `uv`: `(N_f, 3, 2)` float32 glTF UVs (top-left origin)
      - `face_tex_idx`: `(N_f,)` int32 texture index (`-1` if untextured / flat color)
      - `face_flat_rgb`: `(N_f, 3)` uint8 flat baseColorFactor RGB for untextured primitives
      - `face_is_observed`: `(N_f,)` bool (`True` for textured observed/interpolated primitives, `False` for `unobserved`)
      - `face_is_interpolated`: `(N_f,)` bool (`True` if primitive/material is named `interpolated`)
      - `textures_rgb`: list of `(H, W, 3)` uint8 RGB images
      - `unique_vertices`: int
      - `area_m2`: float
      - `glb_bytes`: int
    """
    raw = Path(glb_path).read_bytes()
    magic, _version, total_len = struct.unpack_from("<III", raw, 0)
    if magic != 0x46546C67:
        raise ValueError(f"Invalid GLB magic in {glb_path}")
    j_len, _j_type = struct.unpack_from("<II", raw, 12)
    doc = json.loads(raw[20 : 20 + j_len].decode("utf-8"))
    b_off = 20 + j_len
    b_len, _b_type = struct.unpack_from("<II", raw, b_off)
    bin_blob = raw[b_off + 8 : b_off + 8 + b_len]

    comp_dtype = {
        5120: np.int8,
        5121: np.uint8,
        5122: np.int16,
        5123: np.uint16,
        5125: np.uint32,
        5126: np.float32,
    }
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

    textures_rgb: list[np.ndarray] = []
    for img_entry in doc.get("images", []):
        bv = doc["bufferViews"][img_entry["bufferView"]]
        start = int(bv.get("byteOffset", 0))
        end = start + int(bv["byteLength"])
        decoded = _decode_png_bytes_rgb(bin_blob[start:end])
        if decoded is None:
            textures_rgb.append(np.full((4, 4, 3), 128, dtype=np.uint8))
        else:
            textures_rgb.append(decoded)

    tex_to_img: dict[int, int] = {}
    for t_i, tex_entry in enumerate(doc.get("textures", [])):
        tex_to_img[t_i] = int(tex_entry.get("source", t_i))

    materials = doc.get("materials", [])
    vs_world_list: list[np.ndarray] = []
    fs_list: list[np.ndarray] = []
    uv_list: list[np.ndarray] = []
    tex_idx_list: list[np.ndarray] = []
    flat_rgb_list: list[np.ndarray] = []
    obs_list: list[np.ndarray] = []
    interp_list: list[np.ndarray] = []
    v_offset = 0

    for node in doc.get("nodes", []):
        node_name = str(node.get("name", ""))
        if node_name.startswith("estimated_") or node_name.startswith("completed_"):
            continue
        m_idx = node.get("mesh")
        if m_idx is None:
            continue
        for prim in doc["meshes"][m_idx].get("primitives", []):
            if prim.get("mode", 4) != 4:
                continue
            pos_yup = _read_acc(prim["attributes"]["POSITION"]).astype(np.float32)
            pos_world = from_glb_points(pos_yup)
            if "indices" in prim:
                tri = _read_acc(prim["indices"]).astype(np.int32).reshape(-1, 3)
            else:
                tri = np.arange(len(pos_world), dtype=np.int32).reshape(-1, 3)
            n_f = tri.shape[0]
            if n_f == 0:
                continue

            if "TEXCOORD_0" in prim["attributes"]:
                uv_v = _read_acc(prim["attributes"]["TEXCOORD_0"]).astype(np.float32)
                uv_f = uv_v[tri]
            else:
                uv_f = np.zeros((n_f, 3, 2), dtype=np.float32)

            mat_idx = prim.get("material")
            img_idx = -1
            flat_rgb = np.array([128, 128, 128], dtype=np.uint8)
            mat_name = ""
            if mat_idx is not None and 0 <= mat_idx < len(materials):
                mat = materials[mat_idx]
                mat_name = str(mat.get("name", "")).lower()
                pbr = mat.get("pbrMetallicRoughness", {})
                if "baseColorTexture" in pbr:
                    t_i = int(pbr["baseColorTexture"]["index"])
                    img_idx = tex_to_img.get(t_i, -1)
                elif "baseColorFactor" in pbr:
                    bcf = pbr["baseColorFactor"][:3]
                    flat_rgb = np.clip(np.rint(np.array(bcf) * 255.0), 0, 255).astype(np.uint8)

            is_unobs = (img_idx < 0) or ("unobserved" in mat_name)
            is_interp = "interpolated" in mat_name

            vs_world_list.append(pos_world)
            fs_list.append(tri + v_offset)
            uv_list.append(uv_f)
            tex_idx_list.append(np.full(n_f, img_idx, dtype=np.int32))
            flat_rgb_list.append(np.broadcast_to(flat_rgb[None, :], (n_f, 3)).copy())
            obs_list.append(np.full(n_f, not is_unobs, dtype=bool))
            interp_list.append(np.full(n_f, is_interp, dtype=bool))
            v_offset += len(pos_world)

    if not vs_world_list:
        raise ValueError(f"No triangle primitives found in {glb_path}")

    V_world = np.vstack(vs_world_list).astype(np.float32)
    F = np.vstack(fs_list).astype(np.int32)
    uv = np.vstack(uv_list).astype(np.float32)
    face_tex_idx = np.concatenate(tex_idx_list, axis=0)
    face_flat_rgb = np.vstack(flat_rgb_list).astype(np.uint8)
    face_is_obs = np.concatenate(obs_list, axis=0)
    face_is_interp = np.concatenate(interp_list, axis=0)

    V_q = np.round(V_world.astype(np.float64) * 1e6).astype(np.int64)
    unique_v = int(np.unique(V_q, axis=0).shape[0])
    tris = V_world[F].astype(np.float64)
    areas = 0.5 * np.linalg.norm(
        np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1
    )
    total_area = float(areas.sum())
    obs_area = float(areas[face_is_obs & ~face_is_interp].sum())
    interp_area = float(areas[face_is_interp].sum())
    unobs_area = float(areas[~face_is_obs].sum())
    denom = max(total_area, 1e-9)

    return {
        "V_world": V_world,
        "F": F,
        "uv": uv,
        "face_tex_idx": face_tex_idx,
        "face_flat_rgb": face_flat_rgb,
        "face_is_observed": face_is_obs,
        "face_is_interpolated": face_is_interp,
        "textures_rgb": textures_rgb,
        "mesh_faces": int(F.shape[0]),
        "mesh_vertices": unique_v,
        "mesh_area_m2": round(total_area, 3),
        "observed_faces": int(np.sum(face_is_obs & ~face_is_interp)),
        "interpolated_faces": int(np.sum(face_is_interp)),
        "unobserved_faces": int(np.sum(~face_is_obs)),
        "area_ratio_observed": round(obs_area / denom, 5),
        "area_ratio_interpolated": round(interp_area / denom, 5),
        "area_ratio_unobserved": round(unobs_area / denom, 5),
        "glb_bytes": int(total_len),
    }


def _shade_raycast_hits(
    prim_id: np.ndarray,
    uv_bary: np.ndarray,
    t_hit: np.ndarray,
    parsed_glb: dict[str, Any],
    *,
    bg_rgb: tuple[int, int, int] = (30, 34, 42),
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sample texture / flat color at raycast hits and return `(rgb, depth_m, covered_mask, observed_mask)`."""
    import open3d as o3d

    invalid_id = int(o3d.t.geometry.RaycastingScene.INVALID_ID)
    h, w = prim_id.shape
    hit_mask = (prim_id != invalid_id) & np.isfinite(t_hit) & (t_hit > 0.05)

    rgb = np.broadcast_to(np.array(bg_rgb, dtype=np.uint8)[None, None, :], (h, w, 3)).copy()
    depth_m = np.where(hit_mask, t_hit, 0.0).astype(np.float32)
    observed_mask = np.zeros((h, w), dtype=bool)

    if not bool(hit_mask.any()):
        return rgb, depth_m, hit_mask, observed_mask

    f_ids = prim_id[hit_mask].astype(np.int32)
    b_u = uv_bary[hit_mask, 0].astype(np.float32)
    b_v = uv_bary[hit_mask, 1].astype(np.float32)
    b_w = np.clip(1.0 - b_u - b_v, 0.0, 1.0)

    face_obs = parsed_glb["face_is_observed"][f_ids]
    observed_mask[hit_mask] = face_obs

    f_tex = parsed_glb["face_tex_idx"][f_ids]
    f_uv = parsed_glb["uv"][f_ids]  # (K, 3, 2)
    uv_hit = (
        b_w[:, None] * f_uv[:, 0, :]
        + b_u[:, None] * f_uv[:, 1, :]
        + b_v[:, None] * f_uv[:, 2, :]
    )

    colors_hit = parsed_glb["face_flat_rgb"][f_ids].copy()
    textures = parsed_glb["textures_rgb"]
    for t_i, tex_img in enumerate(textures):
        sel = f_tex == t_i
        if not bool(sel.any()):
            continue
        th, tw = tex_img.shape[:2]
        u_s = np.clip(uv_hit[sel, 0], 0.0, 1.0)
        v_s = np.clip(uv_hit[sel, 1], 0.0, 1.0)
        px = np.clip(np.rint(u_s * (tw - 1)).astype(np.int32), 0, tw - 1)
        py = np.clip(np.rint(v_s * (th - 1)).astype(np.int32), 0, th - 1)
        colors_hit[sel] = tex_img[py, px]

    rgb[hit_mask] = colors_hit
    return rgb, depth_m, hit_mask, observed_mask


def render_glb_views(
    glb_path: Path,
    cameras: dict[str, UndistortedCamera],
    *,
    cam_scale: float = 0.5,
    free_size: tuple[int, int] = (960, 540),
    free_views: dict[str, tuple[list[float], list[float], float]] | None = None,
) -> dict[str, Any]:
    """Render a GLB from all camera viewpoints (`cam_A..D`) and free viewpoints (`top`, `obl1..3`)."""
    import open3d as o3d

    parsed = parse_glb_primitives_world(glb_path)
    t_mesh = o3d.t.geometry.TriangleMesh()
    t_mesh.vertex.positions = o3d.core.Tensor(np.ascontiguousarray(parsed["V_world"], dtype=np.float32))
    t_mesh.triangle.indices = o3d.core.Tensor(np.ascontiguousarray(parsed["F"], dtype=np.int32))
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(t_mesh)

    cam_renders: dict[str, dict[str, np.ndarray]] = {}
    for cid, cam in cameras.items():
        rw = max(1, int(round(cam.width * cam_scale)))
        rh = max(1, int(round(cam.height * cam_scale)))
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
        # Normalize ray directions so t_hit equals Euclidean distance, then convert to camera z-depth
        norms = np.linalg.norm(dirs_cam, axis=-1, keepdims=True)
        dirs_world_unit = dirs_world / np.maximum(norms, 1e-8)
        origins = np.broadcast_to(cam.center_world.astype(np.float32), dirs_world_unit.shape)
        rays = np.concatenate([origins, dirs_world_unit], axis=-1).reshape(-1, 6)

        ans = scene.cast_rays(o3d.core.Tensor(np.ascontiguousarray(rays, dtype=np.float32)))
        prim_id = ans["primitive_ids"].numpy().reshape(rh, rw)
        uv_bary = ans["primitive_uvs"].numpy().reshape(rh, rw, 2)
        t_euclid = ans["t_hit"].numpy().reshape(rh, rw)
        z_depth = t_euclid / norms[..., 0]

        rgb, depth_m, cov_mask, obs_mask = _shade_raycast_hits(
            prim_id, uv_bary, z_depth, parsed, bg_rgb=(0, 0, 0)
        )
        cam_renders[cid] = {
            "rgb": rgb,
            "depth_m": depth_m,
            "covered_mask": cov_mask,
            "observed_mask": obs_mask,
        }

    free_renders: dict[str, np.ndarray] = {}
    fviews = free_views if free_views is not None else STANDARD_FREE_VIEWS
    fw, fh = int(free_size[0]), int(free_size[1])
    for vname, (eye_l, target_l, fov_deg) in fviews.items():
        eye = np.asarray(eye_l, dtype=np.float64)
        target = np.asarray(target_l, dtype=np.float64)
        forward = target - eye
        forward /= max(float(np.linalg.norm(forward)), 1e-8)
        up_guess = np.array([0.0, 0.0, 1.0], dtype=np.float64)
        if abs(float(np.dot(forward, up_guess))) > 0.95:
            up_guess = np.array([0.0, 1.0, 0.0], dtype=np.float64)
        right = np.cross(forward, up_guess)
        right /= max(float(np.linalg.norm(right)), 1e-8)
        down = np.cross(forward, right)
        R_c2w = np.column_stack([right, down, forward]).astype(np.float32)

        fx_f = 0.5 * fw / math.tan(math.radians(fov_deg) * 0.5)
        cx_f, cy_f = 0.5 * fw, 0.5 * fh
        vv, uu = np.meshgrid(
            np.arange(fh, dtype=np.float32),
            np.arange(fw, dtype=np.float32),
            indexing="ij",
        )
        dirs_c = np.stack([(uu - cx_f) / fx_f, (vv - cy_f) / fx_f, np.ones_like(uu)], axis=-1)
        dirs_w = np.einsum("ij,hwj->hwi", R_c2w, dirs_c)
        origins = np.broadcast_to(eye.astype(np.float32), dirs_w.shape)
        rays = np.concatenate([origins, dirs_w], axis=-1).reshape(-1, 6)

        ans = scene.cast_rays(o3d.core.Tensor(np.ascontiguousarray(rays, dtype=np.float32)))
        prim_id = ans["primitive_ids"].numpy().reshape(fh, fw)
        uv_bary = ans["primitive_uvs"].numpy().reshape(fh, fw, 2)
        t_hit = ans["t_hit"].numpy().reshape(fh, fw)
        rgb_f, _, _, _ = _shade_raycast_hits(
            prim_id, uv_bary, t_hit, parsed, bg_rgb=(28, 32, 40)
        )
        free_renders[vname] = rgb_f

    return {
        "parsed_summary": {
            k: v
            for k, v in parsed.items()
            if k
            not in (
                "V_world",
                "F",
                "uv",
                "face_tex_idx",
                "face_flat_rgb",
                "face_is_observed",
                "face_is_interpolated",
                "textures_rgb",
            )
        },
        "cam_renders": cam_renders,
        "free_renders": free_renders,
    }


def compute_edge_sharpness_for_view(
    photo_rgb: np.ndarray,
    render_rgb: np.ndarray,
    render_depth_m: np.ndarray,
    eval_mask: np.ndarray,
) -> dict[str, float | None]:
    """Compute edge sharpness & geometric-photometric edge alignment metrics on a single view."""
    h, w = render_rgb.shape[:2]
    if photo_rgb.shape[:2] != (h, w):
        photo_rs = cv2.resize(photo_rgb, (w, h), interpolation=cv2.INTER_AREA)
    else:
        photo_rs = photo_rgb
    if eval_mask.shape[:2] != (h, w):
        mask_rs = (
            cv2.resize(eval_mask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
        )
    else:
        mask_rs = eval_mask.astype(bool)

    # Erode mask by 5 px so silhouette boundary steps do not skew interior sharpness
    kernel = np.ones((5, 5), dtype=np.uint8)
    if cv2 is not None:
        inner_mask = cv2.erode(mask_rs.astype(np.uint8), kernel, iterations=1) > 0
    else:
        inner_mask = binary_erosion(mask_rs, structure=kernel.astype(bool))
    if int(inner_mask.sum()) < 200:
        inner_mask = mask_rs
    if int(inner_mask.sum()) < 50:
        return {
            "grad_cos_sim": None,
            "grad_mag_ratio": None,
            "edge_f1_px2": None,
            "edge_chamfer_px": None,
            "depth_edge_photo_dist_px": None,
        }

    p_gray = (
        0.299 * photo_rs[..., 0].astype(np.float32)
        + 0.587 * photo_rs[..., 1].astype(np.float32)
        + 0.114 * photo_rs[..., 2].astype(np.float32)
    )
    r_gray = (
        0.299 * render_rgb[..., 0].astype(np.float32)
        + 0.587 * render_rgb[..., 1].astype(np.float32)
        + 0.114 * render_rgb[..., 2].astype(np.float32)
    )

    if cv2 is not None:
        p_gx = cv2.Sobel(p_gray, cv2.CV_32F, 1, 0, ksize=3)
        p_gy = cv2.Sobel(p_gray, cv2.CV_32F, 0, 1, ksize=3)
        r_gx = cv2.Sobel(r_gray, cv2.CV_32F, 1, 0, ksize=3)
        r_gy = cv2.Sobel(r_gray, cv2.CV_32F, 0, 1, ksize=3)
    else:
        p_gx = sobel(p_gray, axis=1)
        p_gy = sobel(p_gray, axis=0)
        r_gx = sobel(r_gray, axis=1)
        r_gy = sobel(r_gray, axis=0)
    p_mag = np.hypot(p_gx, p_gy)
    r_mag = np.hypot(r_gx, r_gy)

    pm = p_mag[inner_mask].astype(np.float64)
    rm = r_mag[inner_mask].astype(np.float64)
    denom = float(np.linalg.norm(pm) * np.linalg.norm(rm))
    grad_cos = float(np.dot(pm, rm) / denom) if denom > 1e-9 else 0.0
    grad_ratio = float(rm.mean() / max(float(pm.mean()), 1e-6))

    if cv2 is not None:
        p_edges = (cv2.Canny(photo_rs, 60, 150) > 0) & inner_mask
        r_edges = (cv2.Canny(render_rgb, 60, 150) > 0) & inner_mask
    else:
        p_edges = (p_mag > 80.0) & inner_mask
        r_edges = (r_mag > 80.0) & inner_mask

    if bool(p_edges.any()) and bool(r_edges.any()):
        dist_to_p = distance_transform_edt(~p_edges)
        dist_to_r = distance_transform_edt(~r_edges)
        d_r2p = dist_to_p[r_edges]
        d_p2r = dist_to_r[p_edges]
        prec = float(np.mean(d_r2p <= 2.0))
        rec = float(np.mean(d_p2r <= 2.0))
        edge_f1 = (2.0 * prec * rec / (prec + rec)) if (prec + rec) > 1e-9 else 0.0
        chamfer = 0.5 * (float(np.mean(d_r2p)) + float(np.mean(d_p2r)))
    else:
        dist_to_p = distance_transform_edt(~p_edges) if bool(p_edges.any()) else None
        edge_f1 = 0.0
        chamfer = None

    # 3D mesh depth discontinuity alignment to photo edges
    if cv2 is not None:
        d_gx = cv2.Sobel(render_depth_m.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3)
        d_gy = cv2.Sobel(render_depth_m.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3)
    else:
        d_gx = sobel(render_depth_m.astype(np.float32), axis=1)
        d_gy = sobel(render_depth_m.astype(np.float32), axis=0)
    d_mag = np.hypot(d_gx, d_gy)
    depth_edges = (d_mag > 0.08) & inner_mask & (render_depth_m > 0.1)
    if dist_to_p is not None and bool(depth_edges.any()):
        depth_edge_dist = float(np.mean(dist_to_p[depth_edges]))
    else:
        depth_edge_dist = None

    return {
        "grad_cos_sim": round(grad_cos, 4),
        "grad_mag_ratio": round(grad_ratio, 4),
        "edge_f1_px2": round(edge_f1, 4),
        "edge_chamfer_px": round(chamfer, 3) if chamfer is not None else None,
        "depth_edge_photo_dist_px": round(depth_edge_dist, 3)
        if depth_edge_dist is not None
        else None,
    }


def evaluate_camera_renders(
    cam_renders: dict[str, dict[str, np.ndarray]],
    cameras: dict[str, UndistortedCamera],
) -> dict[str, Any]:
    """Compute Self-view PSNR, blur8 PSNR, SSIM, MAE, coverage, edge sharpness, and cross-view depth consistency."""
    per_cam: dict[str, Any] = {}
    psnrs: list[float] = []
    psnrs_blur8: list[float] = []
    ssims: list[float] = []
    maes: list[float] = []
    covs_obs: list[float] = []
    covs_tot: list[float] = []
    grad_cos_list: list[float] = []
    edge_f1_list: list[float] = []
    chamfer_list: list[float] = []

    depth_views_for_cv: list[tm.DepthView] = []

    for cid, cam in cameras.items():
        rend = cam_renders[cid]
        rgb_r = rend["rgb"]
        depth_r = rend["depth_m"]
        cov_r = rend["covered_mask"]
        obs_r = rend["observed_mask"]

        rh, rw = rgb_r.shape[:2]
        photo_rs = (
            cv2.resize(cam.image_rgb, (rw, rh), interpolation=cv2.INTER_AREA)
            if (rh, rw) != (cam.height, cam.width)
            else cam.image_rgb
        )
        valid_rs = (
            cv2.resize(
                cam.valid_mask.astype(np.uint8), (rw, rh), interpolation=cv2.INTER_NEAREST
            )
            > 0
            if (rh, rw) != (cam.height, cam.width)
            else cam.valid_mask
        )

        eval_mask = obs_r & valid_rs
        cov_mask = cov_r & valid_rs
        valid_px = max(1, int(valid_rs.sum()))
        cov_obs = float(eval_mask.sum() / valid_px)
        cov_tot = float(cov_mask.sum() / valid_px)

        psnr_val = tp.psnr(photo_rs, rgb_r, eval_mask)
        ssim_val = tp.ssim(photo_rs, rgb_r, eval_mask)
        psnr_b8 = tp.psnr_blurred(photo_rs, rgb_r, eval_mask, 8.0)
        mae_val = (
            float(np.abs(photo_rs[eval_mask].astype(np.float32) - rgb_r[eval_mask].astype(np.float32)).mean())
            if bool(eval_mask.any())
            else None
        )

        edge_m = compute_edge_sharpness_for_view(photo_rs, rgb_r, depth_r, eval_mask)

        if psnr_val is not None:
            psnrs.append(psnr_val)
        if psnr_b8 is not None:
            psnrs_blur8.append(psnr_b8)
        if ssim_val is not None:
            ssims.append(ssim_val)
        if mae_val is not None:
            maes.append(mae_val)
        covs_obs.append(cov_obs)
        covs_tot.append(cov_tot)
        if edge_m["grad_cos_sim"] is not None:
            grad_cos_list.append(float(edge_m["grad_cos_sim"]))
        if edge_m["edge_f1_px2"] is not None:
            edge_f1_list.append(float(edge_m["edge_f1_px2"]))
        if edge_m["edge_chamfer_px"] is not None:
            chamfer_list.append(float(edge_m["edge_chamfer_px"]))

        per_cam[cid] = {
            "psnr_db": round(psnr_val, 3) if psnr_val is not None else None,
            "psnr_db_blur8": round(psnr_b8, 3) if psnr_b8 is not None else None,
            "ssim": round(ssim_val, 4) if ssim_val is not None else None,
            "mae_rgb": round(mae_val, 3) if mae_val is not None else None,
            "coverage_observed": round(cov_obs, 4),
            "coverage_total": round(cov_tot, 4),
            "edge_sharpness": edge_m,
        }

        # Prepare DepthView entry for cross_view_distances at render resolution
        sx = rw / float(cam.width)
        sy = rh / float(cam.height)
        K_scaled = np.array(
            [
                [float(cam.K[0, 0]) * sx, 0.0, float(cam.K[0, 2]) * sx],
                [0.0, float(cam.K[1, 1]) * sy, float(cam.K[1, 2]) * sy],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        d_valid = eval_mask & np.isfinite(depth_r) & (depth_r > 0.1)
        depth_views_for_cv.append(
            tm.DepthView(
                name=cid,
                depth=np.where(d_valid, depth_r, 0.0).astype(np.float32),
                valid=d_valid,
                K=K_scaled,
                cam2world=cam.cam2world,
            )
        )

    cross_view = mc.cross_view_distances(depth_views_for_cv, unit_scale=1.0, max_points=10_000)
    cv_all = cross_view.get("all", {})
    abs_rel = cv_all.get("abs_rel_depth", {})
    p2p = cv_all.get("point_to_plane_m", {})

    return {
        "per_camera": per_cam,
        "self_psnr_db_mean": round(float(np.mean(psnrs)), 3) if psnrs else None,
        "self_psnr_db_blur8_mean": round(float(np.mean(psnrs_blur8)), 3) if psnrs_blur8 else None,
        "self_ssim_mean": round(float(np.mean(ssims)), 4) if ssims else None,
        "self_mae_rgb_mean": round(float(np.mean(maes)), 3) if maes else None,
        "coverage_observed_mean": round(float(np.mean(covs_obs)), 4) if covs_obs else 0.0,
        "coverage_total_mean": round(float(np.mean(covs_tot)), 4) if covs_tot else 0.0,
        "edge_grad_cos_sim_mean": round(float(np.mean(grad_cos_list)), 4)
        if grad_cos_list
        else None,
        "edge_f1_px2_mean": round(float(np.mean(edge_f1_list)), 4) if edge_f1_list else None,
        "edge_chamfer_px_mean": round(float(np.mean(chamfer_list)), 3) if chamfer_list else None,
        "cross_view_depth": {
            "abs_rel_p50": abs_rel.get("p50"),
            "abs_rel_p90": abs_rel.get("p90"),
            "abs_rel_mean": abs_rel.get("mean"),
            "within_1pct": cv_all.get("within_1pct"),
            "within_2pct": cv_all.get("within_2pct"),
            "within_5pct": cv_all.get("within_5pct"),
            "p2p_m_p50": p2p.get("p50"),
            "p2p_m_p90": p2p.get("p90"),
            "p2p_m_mean": p2p.get("mean"),
        },
    }


def evaluate_loo_glbs(
    loo_glb_paths: dict[str, Path],
    cameras: dict[str, UndistortedCamera],
    *,
    cam_scale: float = 0.5,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Evaluate Leave-One-Out (LOO) 3-camera bakes rendered from the held-out 4th camera."""
    per_cam: dict[str, Any] = {}
    loo_renders_rgb: dict[str, np.ndarray] = {}
    psnrs: list[float] = []
    psnrs_b8: list[float] = []
    ssims: list[float] = []
    maes: list[float] = []
    covs: list[float] = []
    edge_f1s: list[float] = []

    for held_cid, glb_path in loo_glb_paths.items():
        if held_cid not in cameras or not Path(glb_path).is_file():
            continue
        cam = cameras[held_cid]
        res = render_glb_views(
            Path(glb_path),
            {held_cid: cam},
            cam_scale=cam_scale,
            free_views={},
        )
        rend = res["cam_renders"][held_cid]
        rgb_r = rend["rgb"]
        depth_r = rend["depth_m"]
        obs_r = rend["observed_mask"]
        loo_renders_rgb[held_cid] = rgb_r

        rh, rw = rgb_r.shape[:2]
        photo_rs = (
            cv2.resize(cam.image_rgb, (rw, rh), interpolation=cv2.INTER_AREA)
            if (rh, rw) != (cam.height, cam.width)
            else cam.image_rgb
        )
        valid_rs = (
            cv2.resize(
                cam.valid_mask.astype(np.uint8), (rw, rh), interpolation=cv2.INTER_NEAREST
            )
            > 0
            if (rh, rw) != (cam.height, cam.width)
            else cam.valid_mask
        )

        eval_mask = obs_r & valid_rs
        cov = float(eval_mask.sum() / max(1, int(valid_rs.sum())))
        psnr_val = tp.psnr(photo_rs, rgb_r, eval_mask)
        ssim_val = tp.ssim(photo_rs, rgb_r, eval_mask)
        psnr_b8 = tp.psnr_blurred(photo_rs, rgb_r, eval_mask, 8.0)
        mae_val = (
            float(
                np.abs(
                    photo_rs[eval_mask].astype(np.float32) - rgb_r[eval_mask].astype(np.float32)
                ).mean()
            )
            if bool(eval_mask.any())
            else None
        )
        edge_m = compute_edge_sharpness_for_view(photo_rs, rgb_r, depth_r, eval_mask)

        if psnr_val is not None:
            psnrs.append(psnr_val)
        if psnr_b8 is not None:
            psnrs_b8.append(psnr_b8)
        if ssim_val is not None:
            ssims.append(ssim_val)
        if mae_val is not None:
            maes.append(mae_val)
        covs.append(cov)
        if edge_m["edge_f1_px2"] is not None:
            edge_f1s.append(float(edge_m["edge_f1_px2"]))

        per_cam[held_cid] = {
            "psnr_db": round(psnr_val, 3) if psnr_val is not None else None,
            "psnr_db_blur8": round(psnr_b8, 3) if psnr_b8 is not None else None,
            "ssim": round(ssim_val, 4) if ssim_val is not None else None,
            "mae_rgb": round(mae_val, 3) if mae_val is not None else None,
            "coverage_observed": round(cov, 4),
            "edge_sharpness": edge_m,
        }

    summary = {
        "per_camera": per_cam,
        "loo_psnr_db_mean": round(float(np.mean(psnrs)), 3) if psnrs else None,
        "loo_psnr_db_blur8_mean": round(float(np.mean(psnrs_b8)), 3) if psnrs_b8 else None,
        "loo_ssim_mean": round(float(np.mean(ssims)), 4) if ssims else None,
        "loo_mae_rgb_mean": round(float(np.mean(maes)), 3) if maes else None,
        "loo_coverage_mean": round(float(np.mean(covs)), 4) if covs else 0.0,
        "loo_edge_f1_px2_mean": round(float(np.mean(edge_f1s)), 4) if edge_f1s else None,
    }
    return summary, loo_renders_rgb


def _label_tile(rgb: np.ndarray, text: str, target_hw: tuple[int, int] | None = None) -> np.ndarray:
    img = rgb
    if target_hw is not None and (img.shape[0], img.shape[1]) != target_hw:
        img = cv2.resize(img, (target_hw[1], target_hw[0]), interpolation=cv2.INTER_AREA)
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    cv2.rectangle(bgr, (0, 0), (min(bgr.shape[1], max(430, len(text) * 11 + 20)), 36), (18, 20, 26), -1)
    cv2.putText(
        bgr,
        text,
        (10, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.58,
        (235, 245, 255),
        1,
        cv2.LINE_AA,
    )
    return bgr


def save_variant_comparison_panels(
    out_dir: Path,
    cameras: dict[str, UndistortedCamera],
    variant_evals: dict[str, dict[str, Any]],
    variant_renders: dict[str, dict[str, Any]],
    variant_loo_renders: dict[str, dict[str, np.ndarray]] | None = None,
) -> list[Path]:
    """Save side-by-side comparison panels across variants for `cam_A..D`, LOO, and `top`/`obl1..3`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    v_names = list(variant_renders.keys())
    if not v_names:
        return saved

    tile_hw = (360, 640)  # (H, W) per tile

    # 1. Self-view camera panels (Photo + all variants)
    for cid, cam in cameras.items():
        tiles = [_label_tile(cam.image_rgb, f"{cid} Photo (Undistorted)", tile_hw)]
        for vn in v_names:
            rgb_v = variant_renders[vn]["cam_renders"][cid]["rgb"]
            m_cam = variant_evals[vn]["self_view"]["per_camera"][cid]
            psnr_s = f"{m_cam['psnr_db']:.2f}dB" if m_cam["psnr_db"] is not None else "n/a"
            ef1_s = (
                f"{m_cam['edge_sharpness']['edge_f1_px2']:.3f}"
                if m_cam["edge_sharpness"]["edge_f1_px2"] is not None
                else "n/a"
            )
            tiles.append(_label_tile(rgb_v, f"{vn} | PSNR {psnr_s} | EdgeF1 {ef1_s}", tile_hw))
        panel = np.hstack(tiles)
        p_out = out_dir / f"compare_self_{cid}.jpg"
        cv2.imwrite(str(p_out), panel, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        saved.append(p_out)

    # 2. LOO panels (Held-out Photo + all variants with LOO)
    if variant_loo_renders:
        for cid, cam in cameras.items():
            tiles = [_label_tile(cam.image_rgb, f"{cid} Held-out Photo (LOO)", tile_hw)]
            for vn in v_names:
                if vn not in variant_loo_renders or cid not in variant_loo_renders[vn]:
                    continue
                rgb_loo = variant_loo_renders[vn][cid]
                m_loo = variant_evals[vn].get("loo", {}).get("per_camera", {}).get(cid, {})
                p_val = m_loo.get("psnr_db")
                b8_val = m_loo.get("psnr_db_blur8")
                lbl = f"{vn} LOO | {p_val:.2f}dB (b8:{b8_val:.2f})" if p_val is not None else f"{vn} LOO"
                tiles.append(_label_tile(rgb_loo, lbl, tile_hw))
            if len(tiles) > 1:
                panel = np.hstack(tiles)
                p_out = out_dir / f"compare_loo_{cid}.jpg"
                cv2.imwrite(str(p_out), panel, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                saved.append(p_out)

    # 3. Free viewpoint panels (`top`, `obl1`, `obl2`, `obl3`)
    free_hw = (540, 960)
    for vname in STANDARD_FREE_VIEWS:
        tiles = []
        for vn in v_names:
            f_rgb = variant_renders[vn]["free_renders"].get(vname)
            if f_rgb is None:
                continue
            f_cnt = variant_evals[vn]["mesh"]["mesh_faces"]
            tiles.append(_label_tile(f_rgb, f"{vn} ({f_cnt:,} faces) | view={vname}", free_hw))
        if tiles:
            panel = np.hstack(tiles)
            p_out = out_dir / f"compare_free_{vname}.jpg"
            cv2.imwrite(str(p_out), panel, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            saved.append(p_out)

    return saved
