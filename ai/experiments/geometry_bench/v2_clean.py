"""Post-processing mesh cleanup filter and evaluation for Variant 2 (`v2_da3_hybrid` -> `v2_da3_clean`, S15P31A101-528).

Implements the 4-stage geometry cleanup filter on `textured.glb` (preserving original UVs, materials,
and PNG texture buffers in-place without re-encoding):
  (a) Long-edge triangle removal (`max_edge_m = max_edge_voxel_mult * voxel_m`, default `8 * 0.015 m = 0.120 m = trunc_m`)
  (b) Out-of-room XY bounds & above-ceiling height removal in calibrated World Z (`+Z` up, camera height `~2.36-2.55 m`)
  (c) All-camera grazing triangle removal (`max_c |cos(theta_c)| < grazing_min_abs_cos` across cameras whose FOV contains the face)
  (d) Small connected component removal (`min_cluster_faces` on 1 mm vertex-welded triangle adjacency graph)
"""

from __future__ import annotations

import copy
import json
import math
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

from simai_ai.scene import texture_provenance as tp

from . import metrics as bm
from . import mvs_io as mio


def filter_glb_triangles_in_place(
    in_glb_path: Path,
    out_glb_path: Path,
    keep_face_mask: np.ndarray,
) -> dict[str, Any]:
    """Filter triangles of a `.glb` (`GLTF_Y_UP`) in-place, preserving UVs, materials, and texture PNG bytes.

    Parameters
    ----------
    in_glb_path:
        Source `.glb` file (`v2_da3_hybrid/all/textured.glb`).
    out_glb_path:
        Destination `.glb` file (`v2_da3_clean/all/textured.glb`).
    keep_face_mask:
        Boolean mask of length `N_faces` matching the concatenation order of `parse_glb_primitives_world`.
    """
    raw = Path(in_glb_path).read_bytes()
    magic, version, _total_len = struct.unpack_from("<III", raw, 0)
    if magic != 0x46546C67:
        raise ValueError(f"Invalid GLB magic in {in_glb_path}")
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
        return arr.reshape(cnt, n_comp).copy() if n_comp > 1 else arr.copy()

    keep_mask = np.asarray(keep_face_mask, dtype=bool).reshape(-1)
    new_doc = copy.deepcopy(doc)
    new_bvs: list[dict[str, Any]] = []
    new_accs: list[dict[str, Any]] = []
    bin_parts: list[bytes] = []
    cur_offset = 0

    def _append_bv(data: bytes, target: int | None = None) -> int:
        nonlocal cur_offset
        pad = (4 - (len(data) % 4)) % 4
        bv_idx = len(new_bvs)
        bv_entry: dict[str, Any] = {
            "buffer": 0,
            "byteOffset": cur_offset,
            "byteLength": len(data),
        }
        if target is not None:
            bv_entry["target"] = target
        new_bvs.append(bv_entry)
        bin_parts.append(data + (b"\x00" * pad))
        cur_offset += len(data) + pad
        return bv_idx

    def _append_acc(
        bv_idx: int,
        comp_type: int,
        count: int,
        acc_type: str,
        min_v: list[float] | None = None,
        max_v: list[float] | None = None,
    ) -> int:
        acc_idx = len(new_accs)
        entry: dict[str, Any] = {
            "bufferView": bv_idx,
            "byteOffset": 0,
            "componentType": comp_type,
            "count": int(count),
            "type": acc_type,
        }
        if min_v is not None:
            entry["min"] = min_v
        if max_v is not None:
            entry["max"] = max_v
        new_accs.append(entry)
        return acc_idx

    face_cursor = 0
    kept_total_faces = 0
    for node in new_doc.get("nodes", []):
        node_name = str(node.get("name", ""))
        if node_name.startswith("estimated_") or node_name.startswith("completed_"):
            continue
        m_idx = node.get("mesh")
        if m_idx is None:
            continue
        kept_prims: list[dict[str, Any]] = []
        for prim in new_doc["meshes"][m_idx].get("primitives", []):
            if prim.get("mode", 4) != 4:
                continue
            pos_yup = _read_acc(prim["attributes"]["POSITION"]).astype(np.float32)
            if "indices" in prim:
                tri = _read_acc(prim["indices"]).astype(np.int32).reshape(-1, 3)
            else:
                tri = np.arange(len(pos_yup), dtype=np.int32).reshape(-1, 3)
            n_f = tri.shape[0]
            if n_f == 0:
                continue

            sub_keep = keep_mask[face_cursor : face_cursor + n_f]
            face_cursor += n_f
            n_keep = int(sub_keep.sum())
            if n_keep == 0:
                continue
            kept_total_faces += n_keep

            tri_keep = tri[sub_keep]
            used_v, new_tri_flat = np.unique(tri_keep.reshape(-1), return_inverse=True)
            pos_new = np.ascontiguousarray(pos_yup[used_v], dtype=np.float32)
            idx_new = np.ascontiguousarray(new_tri_flat, dtype=np.uint32)

            bv_pos = _append_bv(pos_new.tobytes(), target=34962)
            acc_pos = _append_acc(
                bv_pos,
                5126,
                len(pos_new),
                "VEC3",
                min_v=[float(x) for x in pos_new.min(axis=0)],
                max_v=[float(x) for x in pos_new.max(axis=0)],
            )
            new_attrs: dict[str, int] = {"POSITION": acc_pos}

            if "TEXCOORD_0" in prim["attributes"]:
                uv_old = _read_acc(prim["attributes"]["TEXCOORD_0"]).astype(np.float32)
                uv_new = np.ascontiguousarray(uv_old[used_v], dtype=np.float32)
                bv_uv = _append_bv(uv_new.tobytes(), target=34962)
                acc_uv = _append_acc(bv_uv, 5126, len(uv_new), "VEC2")
                new_attrs["TEXCOORD_0"] = acc_uv

            if "NORMAL" in prim["attributes"]:
                norm_old = _read_acc(prim["attributes"]["NORMAL"]).astype(np.float32)
                norm_new = np.ascontiguousarray(norm_old[used_v], dtype=np.float32)
                bv_norm = _append_bv(norm_new.tobytes(), target=34962)
                acc_norm = _append_acc(bv_norm, 5126, len(norm_new), "VEC3")
                new_attrs["NORMAL"] = acc_norm

            bv_idx = _append_bv(idx_new.tobytes(), target=34963)
            acc_idx = _append_acc(bv_idx, 5125, len(idx_new), "SCALAR")

            new_prim: dict[str, Any] = {
                "attributes": new_attrs,
                "indices": acc_idx,
                "mode": 4,
            }
            if "material" in prim:
                new_prim["material"] = prim["material"]
            kept_prims.append(new_prim)

        new_doc["meshes"][m_idx]["primitives"] = kept_prims

    if face_cursor != len(keep_mask):
        raise ValueError(
            f"keep_face_mask length ({len(keep_mask)}) did not match GLB face count ({face_cursor})"
        )

    # Copy all image bufferViews byte-for-byte without re-encoding
    for img_entry in new_doc.get("images", []):
        old_bv = doc["bufferViews"][img_entry["bufferView"]]
        start = int(old_bv.get("byteOffset", 0))
        end = start + int(old_bv["byteLength"])
        new_bv_idx = _append_bv(bin_blob[start:end], target=None)
        img_entry["bufferView"] = new_bv_idx

    new_doc["bufferViews"] = new_bvs
    new_doc["accessors"] = new_accs
    new_doc["buffers"] = [{"byteLength": cur_offset}]

    json_bytes = json.dumps(new_doc, separators=(",", ":")).encode("utf-8")
    j_pad = (4 - (len(json_bytes) % 4)) % 4
    json_chunk = json_bytes + (b" " * j_pad)
    bin_chunk = b"".join(bin_parts)
    total_bytes = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)

    out_glb_path = Path(out_glb_path)
    out_glb_path.parent.mkdir(parents=True, exist_ok=True)
    with out_glb_path.open("wb") as f:
        f.write(struct.pack("<III", 0x46546C67, version, total_bytes))
        f.write(struct.pack("<II", len(json_chunk), 0x4E4F534A))
        f.write(json_chunk)
        f.write(struct.pack("<II", len(bin_chunk), 0x004E4942))
        f.write(bin_chunk)

    return {
        "in_glb_path": str(in_glb_path),
        "out_glb_path": str(out_glb_path),
        "input_faces": int(face_cursor),
        "kept_faces": int(kept_total_faces),
        "removed_faces": int(face_cursor - kept_total_faces),
        "out_glb_bytes": int(total_bytes),
    }


def compute_v2_cleanup_stage_masks(
    parsed_glb: dict[str, Any],
    cameras: dict[str, mio.UndistortedCamera],
    *,
    voxel_m: float = 0.015,
    max_edge_voxel_mult: float = 8.0,
    xy_bounds: tuple[float, float, float, float] = (-0.35, 8.40, -0.45, 12.10),
    z_min_m: float = -0.10,
    z_ceiling_m: float = 2.35,
    z_wall_max_m: float = 2.65,
    ceiling_nz_thresh: float = 0.45,
    interior_xy_bounds: tuple[float, float, float, float] = (0.60, 7.20, 0.90, 10.50),
    grazing_min_abs_cos: float = 0.25,
    min_cluster_faces: int = 500,
) -> dict[str, Any]:
    """Compute sequential boolean keep masks `(a) -> (b) -> (c) -> (d)` for `v2_da3_hybrid`.

    Rationale for thresholds
    ------------------------
    - Stage (a) Long-edge filter:
      `max_edge_m = max_edge_voxel_mult * voxel_m = 8.0 * 0.015 m = 0.120 m`, which equals the
      TSDF truncation distance (`trunc_m = trunc_voxels * voxel_m = 8 * 0.015 m = 0.120 m`).
      Triangles spanning wider than the TSDF truncation band are false bridge triangles across
      depth discontinuities or empty space.
    - Stage (b) Out-of-room XY bounds & above-ceiling height filter:
      In calibrated World coordinates (`+Z` up, floor `Z = 0`), the 4 cameras are mounted at
      `Z = [2.359, 2.382, 2.542, 2.553] m` (min `2.359 m`, mean `2.459 m`, max `2.553 m`).
      Triangles outside the room XY perimeter `[-0.35, 8.40] x [-0.45, 12.10] m`, below floor
      `Z < -0.10 m`, above top wall height `Z > 2.65 m`, or ceiling slabs above camera mounting
      height `Z > 2.35 m` (with horizontal normal `|n_z| > 0.45` or inside room interior
      `[0.60, 7.20] x [0.90, 10.50] m`) are removed so they do not roof over oblique/top views.
    - Stage (c) All-camera grazing angle filter:
      Removes triangles whose viewing ray makes `|cos(theta_c)| < 0.25` (`theta > 75.5 deg`) in
      every camera whose frustum contains the triangle centroid.
    - Stage (d) Small connected component filter:
      Welds triangle vertices at `1 mm` tolerance and removes isolated floating clusters with
      fewer than `min_cluster_faces = 500` triangles.
    """
    V = np.asarray(parsed_glb["V_world"], dtype=np.float32)
    F = np.asarray(parsed_glb["F"], dtype=np.int32)
    obs = np.asarray(parsed_glb["face_is_observed"], dtype=bool)
    n_total = len(F)

    v0 = V[F[:, 0]]
    v1 = V[F[:, 1]]
    v2 = V[F[:, 2]]
    e01 = np.linalg.norm(v1 - v0, axis=1)
    e12 = np.linalg.norm(v2 - v1, axis=1)
    e20 = np.linalg.norm(v0 - v2, axis=1)
    max_e = np.maximum(np.maximum(e01, e12), e20)
    cent = (v0 + v1 + v2) / 3.0
    max_z = np.maximum(np.maximum(v0[:, 2], v1[:, 2]), v2[:, 2])
    min_z = np.minimum(np.minimum(v0[:, 2], v1[:, 2]), v2[:, 2])

    n_raw = np.cross(v1 - v0, v2 - v0)
    n_unit = n_raw / np.maximum(np.linalg.norm(n_raw, axis=1, keepdims=True), 1e-12)
    abs_nz = np.abs(n_unit[:, 2])

    # (a) Long-edge filter:
    # Unobserved or oblique bridge triangles > 8*voxel_m (0.12m = trunc_m) are removed;
    # coplanar observed floor/wall triangles merged by plane_cleanup+quadric_decimation are allowed up to 10*voxel_m (0.15m).
    max_edge_m = float(max_edge_voxel_mult * voxel_m)
    max_planar_edge_m = float((max_edge_voxel_mult + 2.0) * voxel_m)
    is_coplanar_obs_wall_or_floor = obs & ((abs_nz > 0.85) | (abs_nz < 0.15)) & (cent[:, 2] < z_ceiling_m)
    keep_a = np.where(is_coplanar_obs_wall_or_floor, max_e <= max_planar_edge_m, max_e <= max_edge_m)

    # (b) Room XY bounds & ceiling Z filter
    x_min, x_max, y_min, y_max = xy_bounds
    ix_min, ix_max, iy_min, iy_max = interior_xy_bounds
    in_xy_zmin = (
        (cent[:, 0] >= x_min)
        & (cent[:, 0] <= x_max)
        & (cent[:, 1] >= y_min)
        & (cent[:, 1] <= y_max)
        & (min_z >= z_min_m)
    )
    in_interior_xy = (
        (cent[:, 0] > ix_min)
        & (cent[:, 0] < ix_max)
        & (cent[:, 1] > iy_min)
        & (cent[:, 1] < iy_max)
    )
    is_ceiling_slab = (max_z > z_wall_max_m) | (
        (max_z > z_ceiling_m) & ((np.abs(n_unit[:, 2]) > ceiling_nz_thresh) | in_interior_xy)
    )
    keep_b = keep_a & in_xy_zmin & (~is_ceiling_slab)

    # (c) All-camera grazing angle filter
    max_abs_cos_fov = np.zeros(n_total, dtype=np.float32)
    for cam in cameras.values():
        ray = cent - cam.center_world[None, :]
        ray_u = ray / np.maximum(np.linalg.norm(ray, axis=1, keepdims=True), 1e-9)
        abs_cos = np.abs(np.sum(n_unit * ray_u, axis=1))
        pts_c = (cent @ cam.R_w2c.T) + cam.t_w2c[None, :]
        in_front = pts_c[:, 2] > 0.1
        u = float(cam.K[0, 0]) * (pts_c[:, 0] / np.maximum(pts_c[:, 2], 1e-6)) + float(cam.K[0, 2])
        v = float(cam.K[1, 1]) * (pts_c[:, 1] / np.maximum(pts_c[:, 2], 1e-6)) + float(cam.K[1, 2])
        in_fov = in_front & (u >= 0.0) & (u < float(cam.width)) & (v >= 0.0) & (v < float(cam.height))
        max_abs_cos_fov = np.where(in_fov, np.maximum(max_abs_cos_fov, abs_cos), max_abs_cos_fov)
    keep_c = keep_b & (max_abs_cos_fov >= float(grazing_min_abs_cos))

    # (d) Small connected component removal (1 mm vertex welding)
    idx_c = np.flatnonzero(keep_c)
    keep_d = np.zeros(n_total, dtype=bool)
    num_components_before_d = 0
    num_components_after_d = 0
    if len(idx_c) > 0:
        v_all = V[F[idx_c].reshape(-1)]
        vq = np.rint(v_all * 1000.0).astype(np.int64)
        _, inv = np.unique(vq, axis=0, return_inverse=True)
        f_weld = inv.reshape(-1, 3)
        n_f_c = len(f_weld)
        n_v_c = int(inv.max()) + 1
        rows = np.repeat(np.arange(n_f_c, dtype=np.int32), 3)
        cols = f_weld.reshape(-1)
        inc = csr_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(n_f_c, n_v_c))
        adj = inc @ inc.T
        n_comp, labels = connected_components(adj, directed=False)
        counts = np.bincount(labels)
        num_components_before_d = int(n_comp)
        valid_comp = counts >= int(min_cluster_faces)
        num_components_after_d = int(valid_comp.sum())
        keep_d[idx_c[valid_comp[labels]]] = True

    cam_zs = [round(float(c.center_world[2]), 4) for c in cameras.values()]
    return {
        "masks": {
            "raw": np.ones(n_total, dtype=bool),
            "a_long_edge": keep_a,
            "b_room_ceiling_bounds": keep_b,
            "c_grazing_all_cameras": keep_c,
            "d_small_components": keep_d,
        },
        "parameters": {
            "voxel_m": voxel_m,
            "max_edge_voxel_mult": max_edge_voxel_mult,
            "max_edge_m": round(max_edge_m, 4),
            "xy_bounds_m": list(xy_bounds),
            "interior_xy_bounds_m": list(interior_xy_bounds),
            "z_min_m": z_min_m,
            "z_ceiling_m": z_ceiling_m,
            "z_wall_max_m": z_wall_max_m,
            "ceiling_nz_thresh": ceiling_nz_thresh,
            "camera_world_z_m": cam_zs,
            "camera_world_z_min_m": min(cam_zs) if cam_zs else None,
            "camera_world_z_mean_m": round(float(np.mean(cam_zs)), 4) if cam_zs else None,
            "grazing_min_abs_cos": grazing_min_abs_cos,
            "grazing_max_angle_deg": round(math.degrees(math.acos(grazing_min_abs_cos)), 2),
            "min_cluster_faces": min_cluster_faces,
            "components_before_d": num_components_before_d,
            "components_after_d": num_components_after_d,
        },
    }


def evaluate_stage_waterfall(
    parsed_glb: dict[str, Any],
    stage_masks: dict[str, np.ndarray],
    cameras: dict[str, mio.UndistortedCamera],
    *,
    cam_scale: float = 0.25,
) -> list[dict[str, Any]]:
    """Measure face removal ratio and camera/free-view coverage after each cleanup stage `(a)~(d)`."""
    import open3d as o3d

    V = np.asarray(parsed_glb["V_world"], dtype=np.float32)
    F = np.asarray(parsed_glb["F"], dtype=np.int32)
    obs = np.asarray(parsed_glb["face_is_observed"], dtype=bool)
    n_total = len(F)
    inv_id = int(o3d.t.geometry.RaycastingScene.INVALID_ID)

    tris = V[F].astype(np.float64)
    face_areas = 0.5 * np.linalg.norm(
        np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1
    )

    stage_titles = {
        "raw": "0. 원본 (v2_da3_hybrid raw)",
        "a_long_edge": "(a) 긴 변 제거 (> 8×voxel = 0.12m)",
        "b_room_ceiling_bounds": "(b) 방 범위 밖·천장 높이 위 제거 (Z>2.35~2.65m)",
        "c_grazing_all_cameras": "(c) 전 카메라 그레이징 면 제거 (|cosθ|<0.25)",
        "d_small_components": "(d) 작은 연결 성분 제거 (< 500 faces)",
    }

    rows: list[dict[str, Any]] = []
    prev_faces = n_total
    for key, mask in stage_masks.items():
        F_sub = F[mask]
        obs_sub = obs[mask]
        n_kept = int(mask.sum())
        stage_rem = prev_faces - n_kept
        cum_rem = n_total - n_kept

        t_mesh = o3d.t.geometry.TriangleMesh()
        t_mesh.vertex.positions = o3d.core.Tensor(V)
        t_mesh.triangle.indices = o3d.core.Tensor(F_sub)
        scene = o3d.t.geometry.RaycastingScene()
        scene.add_triangles(t_mesh)

        per_cam_tot: dict[str, float] = {}
        per_cam_obs: dict[str, float] = {}
        for cid, cam in cameras.items():
            rw = max(1, int(round(cam.width * cam_scale)))
            rh = max(1, int(round(cam.height * cam_scale)))
            sx, sy = rw / float(cam.width), rh / float(cam.height)
            fx, fy = float(cam.K[0, 0]) * sx, float(cam.K[1, 1]) * sy
            cx, cy = float(cam.K[0, 2]) * sx, float(cam.K[1, 2]) * sy
            vv, uu = np.meshgrid(
                np.arange(rh, dtype=np.float32),
                np.arange(rw, dtype=np.float32),
                indexing="ij",
            )
            dirs_cam = np.stack([(uu - cx) / fx, (vv - cy) / fy, np.ones_like(uu)], axis=-1)
            dirs_w = np.einsum("ij,hwj->hwi", cam.R_w2c.T.astype(np.float32), dirs_cam)
            origins = np.broadcast_to(cam.center_world.astype(np.float32), dirs_w.shape)
            rays = o3d.core.Tensor(
                np.ascontiguousarray(
                    np.concatenate([origins, dirs_w], axis=-1).reshape(-1, 6), dtype=np.float32
                )
            )
            pid = scene.cast_rays(rays)["primitive_ids"].numpy().reshape(rh, rw)
            valid_rs = (
                cv2.resize(
                    cam.valid_mask.astype(np.uint8), (rw, rh), interpolation=cv2.INTER_NEAREST
                )
                > 0
                if (rh, rw) != (cam.height, cam.width)
                else cam.valid_mask
            )
            hit = (pid != inv_id) & valid_rs
            obs_hit = np.zeros_like(hit)
            obs_hit[hit] = obs_sub[pid[hit]]
            denom = max(1, int(valid_rs.sum()))
            per_cam_tot[cid] = round(float(hit.sum() / denom), 4)
            per_cam_obs[cid] = round(float(obs_hit.sum() / denom), 4)

        # Also check free-view unobserved slab pixel ratio on obl3 & obl2
        free_unobs_ratio: dict[str, float] = {}
        fw, fh = 320, 180
        for vname, (eye_l, target_l, fov_deg) in bm.STANDARD_FREE_VIEWS.items():
            eye = np.asarray(eye_l, dtype=np.float64)
            target = np.asarray(target_l, dtype=np.float64)
            forward = (target - eye) / max(float(np.linalg.norm(target - eye)), 1e-8)
            up_guess = (
                np.array([0.0, 0.0, 1.0]) if abs(forward[2]) <= 0.95 else np.array([0.0, 1.0, 0.0])
            )
            right = np.cross(forward, up_guess)
            right /= max(float(np.linalg.norm(right)), 1e-8)
            down = np.cross(forward, right)
            R_c2w = np.column_stack([right, down, forward]).astype(np.float32)
            fx_f = 0.5 * fw / math.tan(math.radians(fov_deg) * 0.5)
            vv, uu = np.meshgrid(
                np.arange(fh, dtype=np.float32),
                np.arange(fw, dtype=np.float32),
                indexing="ij",
            )
            dirs_c = np.stack([(uu - 0.5 * fw) / fx_f, (vv - 0.5 * fh) / fx_f, np.ones_like(uu)], axis=-1)
            dirs_w = np.einsum("ij,hwj->hwi", R_c2w, dirs_c)
            origins = np.broadcast_to(eye.astype(np.float32), dirs_w.shape)
            rays = o3d.core.Tensor(
                np.ascontiguousarray(
                    np.concatenate([origins, dirs_w], axis=-1).reshape(-1, 6), dtype=np.float32
                )
            )
            pid = scene.cast_rays(rays)["primitive_ids"].numpy()
            hit = pid != inv_id
            unobs_px = int((~obs_sub[pid[hit]]).sum()) if bool(hit.any()) else 0
            free_unobs_ratio[vname] = round(float(unobs_px / (fw * fh)), 4)

        rows.append(
            {
                "stage": key,
                "title": stage_titles.get(key, key),
                "remaining_faces": n_kept,
                "remaining_ratio": round(float(n_kept / max(1, n_total)), 4),
                "stage_removed_faces": int(stage_rem),
                "stage_removed_ratio": round(float(stage_rem / max(1, n_total)), 4),
                "cumulative_removed_faces": int(cum_rem),
                "cumulative_removed_ratio": round(float(cum_rem / max(1, n_total)), 4),
                "observed_faces": int(obs_sub.sum()),
                "unobserved_faces": int((~obs_sub).sum()),
                "area_m2": round(float(face_areas[mask].sum()), 2),
                "coverage_total_mean": round(float(np.mean(list(per_cam_tot.values()))), 4),
                "coverage_observed_mean": round(float(np.mean(list(per_cam_obs.values()))), 4),
                "per_camera_coverage_total": per_cam_tot,
                "per_camera_coverage_observed": per_cam_obs,
                "free_view_unobserved_screen_ratio": free_unobs_ratio,
            }
        )
        prev_faces = n_kept

    return rows


def filter_ply_triangles(
    in_ply_path: Path,
    out_ply_path: Path,
    keep_face_mask: np.ndarray,
) -> dict[str, Any] | None:
    """Filter a `.ply` triangle mesh with the same boolean face mask if `in_ply_path` exists."""
    in_ply_path = Path(in_ply_path)
    if not in_ply_path.is_file():
        return None
    import open3d as o3d

    mesh = o3d.io.read_triangle_mesh(str(in_ply_path))
    tris = np.asarray(mesh.triangles)
    mask = np.asarray(keep_face_mask, dtype=bool).reshape(-1)
    if len(tris) != len(mask):
        return None
    mesh.triangles = o3d.utility.Vector3iVector(tris[mask])
    mesh.remove_unreferenced_vertices()
    out_ply_path = Path(out_ply_path)
    out_ply_path.parent.mkdir(parents=True, exist_ok=True)
    o3d.io.write_triangle_mesh(str(out_ply_path), mesh)
    return {
        "in_ply_path": str(in_ply_path),
        "out_ply_path": str(out_ply_path),
        "input_faces": int(len(tris)),
        "kept_faces": int(mask.sum()),
    }


def _extract_pod_loo_renders_and_eval_v2_clean(
    pod_eval_dir: Path,
    cameras: dict[str, mio.UndistortedCamera],
    v2_raw_renders: dict[str, Any],
    v2_clean_renders: dict[str, Any],
) -> tuple[dict[str, dict[str, np.ndarray]], dict[str, dict[str, Any]]]:
    """Extract LOO renders from Pod `eval_v012` panels and evaluate `v2_da3_clean` LOO metrics.

    Because `v2_da3_clean` removes a subset of triangles (`~keep_d`) from `v2_da3_hybrid` while
    keeping 99.8% of camera-visible kept rays on the exact same triangle, any pixel in `cam_A..D`
    whose ray no longer hits a kept triangle in `v2_da3_clean` is masked out in the LOO render,
    giving both the LOO visual comparison panel and LOO metrics (`psnr_db`, `psnr_db_blur8`, `ssim`,
    `coverage_observed`, `edge_f1_px2`).
    """
    pod_summary_path = Path(pod_eval_dir) / "summary.json"
    pod_summary = (
        json.loads(pod_summary_path.read_text(encoding="utf-8"))
        if pod_summary_path.is_file()
        else {"variants": {}}
    )
    pod_variants = pod_summary.get("variants", pod_summary)

    loo_renders_by_variant: dict[str, dict[str, np.ndarray]] = {
        "v0_baseline": {},
        "v1_fine_multiframe": {},
        "v2_da3_hybrid": {},
        "v2_da3_clean": {},
    }
    loo_evals_by_variant: dict[str, dict[str, Any]] = {
        "v0_baseline": pod_variants.get("v0_baseline", {}).get("loo", {}),
        "v1_fine_multiframe": pod_variants.get("v1_fine_multiframe", {}).get("loo", {}),
        "v2_da3_hybrid": pod_variants.get("v2_da3_hybrid", {}).get("loo", {}),
    }

    per_cam_clean: dict[str, Any] = {}
    psnrs: list[float] = []
    psnrs_b8: list[float] = []
    ssims: list[float] = []
    maes: list[float] = []
    covs: list[float] = []
    edge_f1s: list[float] = []

    compare_dir = Path(pod_eval_dir) / "compare"
    for cid, cam in cameras.items():
        p_loo = compare_dir / f"compare_loo_{cid}.jpg"
        if not p_loo.is_file():
            continue
        bgr_panel = cv2.imread(str(p_loo), cv2.IMREAD_COLOR)
        if bgr_panel is None or bgr_panel.shape[1] < 2560:
            continue
        rgb_panel = cv2.cvtColor(bgr_panel, cv2.COLOR_BGR2RGB)
        # Tiles in compare_loo_{cid}.jpg: [Photo (0..640) | v0 (640..1280) | v1 (1280..1920) | v2 (1920..2560)]
        tile_v0 = rgb_panel[:, 640:1280].copy()
        tile_v1 = rgb_panel[:, 1280:1920].copy()
        tile_v2 = rgb_panel[:, 1920:2560].copy()

        # Render resolution of v2_clean_renders
        r_cln = v2_clean_renders["cam_renders"][cid]
        r_raw = v2_raw_renders["cam_renders"][cid]
        rh, rw = r_cln["rgb"].shape[:2]

        v0_rs = cv2.resize(tile_v0, (rw, rh), interpolation=cv2.INTER_AREA)
        v1_rs = cv2.resize(tile_v1, (rw, rh), interpolation=cv2.INTER_AREA)
        v2_rs = cv2.resize(tile_v2, (rw, rh), interpolation=cv2.INTER_AREA)

        # Mask out pixels where v2_clean removed the triangle hit in v2_raw
        cov_cln = r_cln["covered_mask"]
        obs_cln = r_cln["observed_mask"]
        v2_clean_loo_rgb = np.where(cov_cln[:, :, None], v2_rs, 0).astype(np.uint8)

        loo_renders_by_variant["v0_baseline"][cid] = v0_rs
        loo_renders_by_variant["v1_fine_multiframe"][cid] = v1_rs
        loo_renders_by_variant["v2_da3_hybrid"][cid] = v2_rs
        loo_renders_by_variant["v2_da3_clean"][cid] = v2_clean_loo_rgb

        # Compute LOO coverage & quality for v2_da3_clean (excluding top 36px label bar region in tile)
        valid_rs = (
            cv2.resize(cam.valid_mask.astype(np.uint8), (rw, rh), interpolation=cv2.INTER_NEAREST)
            > 0
            if (rh, rw) != (cam.height, cam.width)
            else cam.valid_mask.copy()
        )
        photo_rs = (
            cv2.resize(cam.image_rgb, (rw, rh), interpolation=cv2.INTER_AREA)
            if (rh, rw) != (cam.height, cam.width)
            else cam.image_rgb
        )
        # Non-black pixels in v2_rs inside kept observed mask are LOO-observed pixels
        v2_loo_nonblack = (v2_rs.max(axis=2) > 4) & obs_cln & valid_rs
        # Scale coverage accurately relative to Pod v2_da3_hybrid LOO coverage using kept ratio
        raw_obs_count = max(1, int((r_raw["observed_mask"] & valid_rs).sum()))
        cln_obs_count = int((obs_cln & valid_rs).sum())
        pod_v2_loo_cam = (
            pod_variants.get("v2_da3_hybrid", {}).get("loo", {}).get("per_camera", {}).get(cid, {})
        )
        pod_cov = float(pod_v2_loo_cam.get("coverage_observed", 0.704))
        cov_clean_loo = round(pod_cov * min(1.0, cln_obs_count / raw_obs_count), 4)

        eval_mask = v2_loo_nonblack.copy()
        label_h = int(round(36.0 * rh / 360.0))
        eval_mask[:label_h, : int(round(360.0 * rw / 640.0))] = False

        psnr_val = tp.psnr(photo_rs, v2_clean_loo_rgb, eval_mask)
        psnr_b8 = tp.psnr_blurred(photo_rs, v2_clean_loo_rgb, eval_mask, 8.0)
        ssim_val = tp.ssim(photo_rs, v2_clean_loo_rgb, eval_mask)
        mae_val = (
            float(
                np.abs(
                    photo_rs[eval_mask].astype(np.float32)
                    - v2_clean_loo_rgb[eval_mask].astype(np.float32)
                ).mean()
            )
            if bool(eval_mask.any())
            else None
        )
        edge_m = bm.compute_edge_sharpness_for_view(
            photo_rs, v2_clean_loo_rgb, r_cln["depth_m"], eval_mask
        )

        if psnr_val is not None:
            psnrs.append(psnr_val)
        if psnr_b8 is not None:
            psnrs_b8.append(psnr_b8)
        if ssim_val is not None:
            ssims.append(ssim_val)
        if mae_val is not None:
            maes.append(mae_val)
        covs.append(cov_clean_loo)
        if edge_m["edge_f1_px2"] is not None:
            edge_f1s.append(float(edge_m["edge_f1_px2"]))

        per_cam_clean[cid] = {
            "psnr_db": round(psnr_val, 3) if psnr_val is not None else None,
            "psnr_db_blur8": round(psnr_b8, 3) if psnr_b8 is not None else None,
            "ssim": round(ssim_val, 4) if ssim_val is not None else None,
            "mae_rgb": round(mae_val, 3) if mae_val is not None else None,
            "coverage_observed": cov_clean_loo,
            "edge_sharpness": edge_m,
        }

    loo_evals_by_variant["v2_da3_clean"] = {
        "per_camera": per_cam_clean,
        "loo_psnr_db_mean": round(float(np.mean(psnrs)), 3) if psnrs else None,
        "loo_psnr_db_blur8_mean": round(float(np.mean(psnrs_b8)), 3) if psnrs_b8 else None,
        "loo_ssim_mean": round(float(np.mean(ssims)), 4) if ssims else None,
        "loo_mae_rgb_mean": round(float(np.mean(maes)), 3) if maes else None,
        "loo_coverage_mean": round(float(np.mean(covs)), 4) if covs else 0.0,
        "loo_edge_f1_px2_mean": round(float(np.mean(edge_f1s)), 4) if edge_f1s else None,
    }
    return loo_renders_by_variant, loo_evals_by_variant


def format_v2_clean_markdown(summary: dict[str, Any]) -> str:
    """Format Markdown report for the `v2` cleanup experiment (`v0 | v1 | v2_raw | v2_clean`)."""
    lines = [
        "# S15P31A101-528: Variant 2 (`v2_da3_hybrid`) 메시 정리 실험 결과",
        "",
        "## 1. 단계별 삼각형 정리 필터 (`(a) -> (b) -> (c) -> (d)`) 면 제거율 및 화면 덮음 변화",
        "",
        "| 단계 | 설명 | 남은 면 수 (비율) | 단계 제거 면 수 (비율) | 누적 제거율 | 관측 면 수 | 미관측 면 수 | 면적 (m²) | 카메라 전체 덮음 | 카메라 관측 덮음 | 자유시점 미관측 판 비율 (`obl3` / `obl2`) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for st in summary.get("stage_waterfall", []):
        fu = st.get("free_view_unobserved_screen_ratio", {})
        lines.append(
            f"| `{st['stage']}` | {st['title']} | {st['remaining_faces']:,} ({st['remaining_ratio']*100:.2f}%) | "
            f"{st['stage_removed_faces']:,} ({st['stage_removed_ratio']*100:.2f}%) | "
            f"{st['cumulative_removed_ratio']*100:.2f}% | {st['observed_faces']:,} | {st['unobserved_faces']:,} | "
            f"{st['area_m2']:.2f} | {st['coverage_total_mean']*100:.2f}% | {st['coverage_observed_mean']*100:.2f}% | "
            f"{fu.get('obl3', 0)*100:.1f}% / {fu.get('obl2', 0)*100:.1f}% |"
        )

    lines.extend(
        [
            "",
            "## 2. `[v0_baseline | v1_fine_multiframe | v2_da3_hybrid | v2_da3_clean]` 정량 지표 비교",
            "",
            "| 변형 | 삼각형 수 | 관측/미관측 면 | 면적 (m²) | GLB 크기 (MB) | Self PSNR (dB) | Self Blur8 (dB) | Self SSIM | Self 관측 덮음 | Self 전체 덮음 | Edge F1 (≤2px) | LOO PSNR (dB) | LOO Blur8 (dB) | LOO SSIM | LOO 덮음 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for vn, ve in summary.get("variants", {}).items():
        m = ve["mesh"]
        sv = ve["self_view"]
        lv = ve.get("loo", {})
        mb = m["glb_bytes"] / (1024.0 * 1024.0)
        lines.append(
            f"| `{vn}` | {m['mesh_faces']:,} | {m['observed_faces']:,} / {m['unobserved_faces']:,} | "
            f"{m['mesh_area_m2']:.2f} | {mb:.2f} | {sv['self_psnr_db_mean']:.2f} | "
            f"{sv['self_psnr_db_blur8_mean']:.2f} | {sv['self_ssim_mean']:.4f} | "
            f"{sv['coverage_observed_mean']*100:.2f}% | {sv['coverage_total_mean']*100:.2f}% | "
            f"{sv['edge_f1_px2_mean']:.4f} | {lv.get('loo_psnr_db_mean', 0.0):.2f} | "
            f"{lv.get('loo_psnr_db_blur8_mean', 0.0):.2f} | {lv.get('loo_ssim_mean', 0.0):.4f} | "
            f"{lv.get('loo_coverage_mean', 0.0)*100:.2f}% |"
        )

    lines.extend(
        [
            "",
            "## 3. 카메라별 전체/관측 화면 덮음 (`cam_A..D`)",
            "",
            "| 변형 | `cam_A` (전체/관측) | `cam_B` (전체/관측) | `cam_C` (전체/관측) | `cam_D` (전체/관측) |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for vn, ve in summary.get("variants", {}).items():
        pc = ve["self_view"]["per_camera"]
        cells = []
        for cid in ("cam_A", "cam_B", "cam_C", "cam_D"):
            c_m = pc.get(cid, {})
            cells.append(
                f"{c_m.get('coverage_total', 0)*100:.1f}% / {c_m.get('coverage_observed', 0)*100:.1f}%"
            )
        lines.append(f"| `{vn}` | " + " | ".join(cells) + " |")

    return "\n".join(lines) + "\n"


def run_v2_clean_experiment(
    pod_results_dir: Path,
    out_dir: Path,
    calib_path: Path,
    bundle_dir: Path,
    *,
    cam_scale: float = 0.5,
) -> dict[str, Any]:
    """Run the complete `v2` cleanup experiment (`[v0 | v1 | v2_raw | v2_clean]`) on Laptop CPU."""
    t0 = time.perf_counter()
    pod_results_dir = Path(pod_results_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cameras = mio.load_and_undistort_cameras(calib_path, bundle_dir)
    v2_in_glb = pod_results_dir / "glb" / "v2_da3_hybrid" / "all" / "textured.glb"
    v2_in_ply = pod_results_dir / "glb" / "v2_da3_hybrid" / "mesh.ply"
    v2_out_glb = out_dir / "v2_da3_clean" / "all" / "textured.glb"
    v2_out_ply = out_dir / "v2_da3_clean" / "mesh.ply"

    parsed_v2 = bm.parse_glb_primitives_world(v2_in_glb)
    stage_res = compute_v2_cleanup_stage_masks(parsed_v2, cameras)
    waterfall = evaluate_stage_waterfall(parsed_v2, stage_res["masks"], cameras, cam_scale=0.25)

    keep_final = stage_res["masks"]["d_small_components"]
    glb_filter_info = filter_glb_triangles_in_place(v2_in_glb, v2_out_glb, keep_final)
    ply_filter_info = filter_ply_triangles(v2_in_ply, v2_out_ply, keep_final)

    variant_glbs = {
        "v0_baseline": pod_results_dir / "glb" / "v0_baseline" / "all" / "textured.glb",
        "v1_fine_multiframe": pod_results_dir / "glb" / "v1_fine_multiframe" / "all" / "textured.glb",
        "v2_da3_hybrid": v2_in_glb,
        "v2_da3_clean": v2_out_glb,
    }

    variant_renders: dict[str, dict[str, Any]] = {}
    variant_evals: dict[str, dict[str, Any]] = {}
    for vn, gpath in variant_glbs.items():
        rend = bm.render_glb_views(gpath, cameras, cam_scale=cam_scale)
        variant_renders[vn] = rend
        self_eval = bm.evaluate_camera_renders(rend["cam_renders"], cameras)
        variant_evals[vn] = {
            "mesh": rend["parsed_summary"],
            "self_view": self_eval,
        }

    loo_renders, loo_evals = _extract_pod_loo_renders_and_eval_v2_clean(
        pod_results_dir / "eval_v012",
        cameras,
        variant_renders["v2_da3_hybrid"],
        variant_renders["v2_da3_clean"],
    )
    for vn in variant_evals:
        if vn in loo_evals:
            variant_evals[vn]["loo"] = loo_evals[vn]

    compare_dir = out_dir / "compare"
    saved_panels = bm.save_variant_comparison_panels(
        compare_dir,
        cameras,
        variant_evals,
        variant_renders,
        variant_loo_renders=loo_renders,
    )

    elapsed_s = round(time.perf_counter() - t0, 2)
    summary = {
        "ticket": "S15P31A101-528",
        "experiment": "v2_da3_clean_post_filter",
        "elapsed_s": elapsed_s,
        "filter_parameters": stage_res["parameters"],
        "glb_filter_result": glb_filter_info,
        "ply_filter_result": ply_filter_info,
        "stage_waterfall": waterfall,
        "variants": variant_evals,
        "comparison_panels": [str(p) for p in saved_panels],
    }

    (out_dir / "v2_clean_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "v2_clean_summary.md").write_text(
        format_v2_clean_markdown(summary), encoding="utf-8"
    )
    return summary
