"""S15P31A101-528 / -539: 4-stage post-processing mesh cleanup filter (`MESH_CLEAN_V2`).

Ports the `-528` (`v2_clean.py`) 4-stage triangle filter into `simai_ai.scene` so it runs
in-place on `textured.glb` (preserving original UVs, materials, and PNG/JPEG texture buffers):
  (a) Long-edge triangle removal (`max_edge_m = max_edge_voxel_mult * voxel_m`, default `8 * 0.02 m = 0.160 m`)
  (b) Out-of-room XY bounds & above-ceiling height removal in calibrated World Z (`+Z` up)
  (c) All-camera grazing triangle removal (`max_c |cos(theta_c)| < grazing_min_abs_cos` across frustum cameras)
  (d) Small connected component removal (`min_cluster_faces` on 1 mm vertex-welded triangle adjacency graph)
"""

from __future__ import annotations

import copy
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components


@dataclass(frozen=True)
class CleanCamera:
    """Camera pose and intrinsics in World coordinates (`+Z` up) for grazing angle checks."""

    camera_id: str
    K: np.ndarray  # (3, 3) float64
    R_w2c: np.ndarray  # (3, 3) float64
    t_w2c: np.ndarray  # (3,) float64
    width: int
    height: int

    @property
    def center_world(self) -> np.ndarray:
        return -self.R_w2c.T @ self.t_w2c.reshape(3)


def from_glb_points(pts_yup: np.ndarray) -> np.ndarray:
    """Convert glTF Y-up `(x_g, y_g, z_g)` back to World `(x, -z_g, y_g)` (`+Z` up)."""
    pts = np.asarray(pts_yup, dtype=np.float32)
    return np.stack([pts[:, 0], -pts[:, 2], pts[:, 1]], axis=-1)


def parse_glb_triangles_world(glb_path_or_bytes: Path | bytes) -> dict[str, Any]:
    """Parse triangle vertices (converted to World `+Z` up) and face observation flags from a GLB."""
    raw = glb_path_or_bytes if isinstance(glb_path_or_bytes, (bytes, bytearray)) else Path(glb_path_or_bytes).read_bytes()
    magic, _version, _total_len = struct.unpack_from("<III", raw, 0)
    if magic != 0x46546C67:
        raise ValueError("Invalid GLB magic")
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

    tex_to_img: dict[int, int] = {}
    for t_i, tex_entry in enumerate(doc.get("textures", [])):
        tex_to_img[t_i] = int(tex_entry.get("source", t_i))

    materials = doc.get("materials", [])
    vs_world_list: list[np.ndarray] = []
    fs_list: list[np.ndarray] = []
    obs_list: list[np.ndarray] = []
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

            mat_idx = prim.get("material")
            img_idx = -1
            mat_name = ""
            if mat_idx is not None and 0 <= mat_idx < len(materials):
                mat = materials[mat_idx]
                mat_name = str(mat.get("name", "")).lower()
                pbr = mat.get("pbrMetallicRoughness", {})
                if "baseColorTexture" in pbr:
                    t_i = int(pbr["baseColorTexture"]["index"])
                    img_idx = tex_to_img.get(t_i, -1)

            is_unobs = (img_idx < 0) or ("unobserved" in mat_name)
            vs_world_list.append(pos_world)
            fs_list.append(tri + v_offset)
            obs_list.append(np.full(n_f, not is_unobs, dtype=bool))
            v_offset += len(pos_world)

    if not vs_world_list:
        return {
            "V_world": np.zeros((0, 3), dtype=np.float32),
            "F": np.zeros((0, 3), dtype=np.int32),
            "face_is_observed": np.zeros(0, dtype=bool),
        }

    return {
        "V_world": np.vstack(vs_world_list).astype(np.float32),
        "F": np.vstack(fs_list).astype(np.int32),
        "face_is_observed": np.concatenate(obs_list, axis=0),
    }


def filter_glb_triangles_bytes(
    raw_glb: bytes,
    keep_face_mask: np.ndarray,
) -> tuple[bytes, dict[str, Any]]:
    """Filter triangles of a `.glb` (`GLTF_Y_UP`) in-place, preserving UVs, materials, and image bytes."""
    magic, version, _total_len = struct.unpack_from("<III", raw_glb, 0)
    if magic != 0x46546C67:
        raise ValueError("Invalid GLB magic")
    j_len, _j_type = struct.unpack_from("<II", raw_glb, 12)
    doc = json.loads(raw_glb[20 : 20 + j_len].decode("utf-8"))
    b_off = 20 + j_len
    b_len, _b_type = struct.unpack_from("<II", raw_glb, b_off)
    bin_blob = raw_glb[b_off + 8 : b_off + 8 + b_len]

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

            for attr_name, comp_t, a_type in (
                ("TEXCOORD_0", 5126, "VEC2"),
                ("NORMAL", 5126, "VEC3"),
                ("COLOR_0", 5126, "VEC4"),
            ):
                if attr_name in prim["attributes"]:
                    old_acc = doc["accessors"][prim["attributes"][attr_name]]
                    arr_old = _read_acc(prim["attributes"][attr_name]).astype(np.float32)
                    arr_new = np.ascontiguousarray(arr_old[used_v], dtype=np.float32)
                    bv_a = _append_bv(arr_new.tobytes(), target=34962)
                    acc_a = _append_acc(bv_a, 5126, len(arr_new), old_acc["type"])
                    new_attrs[attr_name] = acc_a

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

    out_bytes = (
        struct.pack("<III", 0x46546C67, version, total_bytes)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(bin_chunk), 0x004E4942)
        + bin_chunk
    )
    return out_bytes, {
        "input_faces": int(face_cursor),
        "kept_faces": int(kept_total_faces),
        "removed_faces": int(face_cursor - kept_total_faces),
        "out_glb_bytes": int(total_bytes),
    }


def compute_cleanup_stage_masks(
    parsed_glb: dict[str, Any],
    cameras: Sequence[CleanCamera] | dict[str, CleanCamera],
    *,
    voxel_m: float = 0.02,
    max_edge_voxel_mult: float = 8.0,
    xy_bounds: tuple[float, float, float, float] | None = None,
    z_min_m: float = -0.10,
    z_ceiling_m: float | None = None,
    z_wall_max_m: float | None = None,
    ceiling_nz_thresh: float = 0.45,
    interior_xy_bounds: tuple[float, float, float, float] | None = None,
    grazing_min_abs_cos: float = 0.25,
    min_cluster_faces: int = 500,
) -> dict[str, Any]:
    """Compute sequential boolean keep masks `(a) -> (b) -> (c) -> (d)` for 2 cm TSDF mesh."""
    V = np.asarray(parsed_glb["V_world"], dtype=np.float32)
    F = np.asarray(parsed_glb["F"], dtype=np.int32)
    obs = np.asarray(parsed_glb["face_is_observed"], dtype=bool)
    n_total = len(F)
    cam_list = list(cameras.values()) if isinstance(cameras, dict) else list(cameras)

    if n_total == 0:
        empty = np.zeros(0, dtype=bool)
        return {
            "masks": {
                "raw": empty,
                "a_long_edge": empty,
                "b_room_ceiling_bounds": empty,
                "c_grazing_all_cameras": empty,
                "d_small_components": empty,
            },
            "parameters": {},
        }

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

    cam_zs = [float(c.center_world[2]) for c in cam_list]
    if z_ceiling_m is None:
        z_ceiling_m = min(cam_zs) - 0.01 if cam_zs else 2.35
    if z_wall_max_m is None:
        z_wall_max_m = (max(cam_zs) + 0.10) if cam_zs else 2.65

    if xy_bounds is None:
        if cam_list:
            cx_all = [float(c.center_world[0]) for c in cam_list]
            cy_all = [float(c.center_world[1]) for c in cam_list]
            xy_bounds = (
                min(min(cx_all) - 1.0, -0.35),
                max(max(cx_all) + 2.0, 8.40),
                min(min(cy_all) - 1.0, -0.45),
                max(max(cy_all) + 5.0, 12.10),
            )
        else:
            xy_bounds = (-0.35, 8.40, -0.45, 12.10)

    if interior_xy_bounds is None:
        x0, x1, y0, y1 = xy_bounds
        interior_xy_bounds = (x0 + 0.95, x1 - 1.20, y0 + 1.35, y1 - 1.60)

    # (a) Long-edge filter
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
        (max_z > z_ceiling_m) & ((abs_nz > ceiling_nz_thresh) | in_interior_xy)
    )
    keep_b = keep_a & in_xy_zmin & (~is_ceiling_slab)

    # (c) All-camera grazing angle filter
    if cam_list:
        max_abs_cos_fov = np.zeros(n_total, dtype=np.float32)
        for cam in cam_list:
            ray = cent - cam.center_world[None, :].astype(np.float32)
            ray_u = ray / np.maximum(np.linalg.norm(ray, axis=1, keepdims=True), 1e-9)
            abs_cos = np.abs(np.sum(n_unit * ray_u, axis=1))
            pts_c = (cent @ cam.R_w2c.T.astype(np.float32)) + cam.t_w2c[None, :].astype(np.float32)
            in_front = pts_c[:, 2] > 0.1
            u = float(cam.K[0, 0]) * (pts_c[:, 0] / np.maximum(pts_c[:, 2], 1e-6)) + float(cam.K[0, 2])
            v = float(cam.K[1, 1]) * (pts_c[:, 1] / np.maximum(pts_c[:, 2], 1e-6)) + float(cam.K[1, 2])
            in_fov = in_front & (u >= 0.0) & (u < float(cam.width)) & (v >= 0.0) & (v < float(cam.height))
            max_abs_cos_fov = np.where(in_fov, np.maximum(max_abs_cos_fov, abs_cos), max_abs_cos_fov)
        keep_c = keep_b & (max_abs_cos_fov >= float(grazing_min_abs_cos))
    else:
        keep_c = keep_b.copy()

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
        if not np.any(valid_comp) and len(counts) > 0:
            # Keep largest component if all are smaller than threshold (e.g. unit test small meshes)
            valid_comp[int(np.argmax(counts))] = True
        num_components_after_d = int(valid_comp.sum())
        keep_d[idx_c[valid_comp[labels]]] = True

    return {
        "masks": {
            "raw": np.ones(n_total, dtype=bool),
            "a_long_edge": keep_a,
            "b_room_ceiling_bounds": keep_b,
            "c_grazing_all_cameras": keep_c,
            "d_small_components": keep_d,
        },
        "parameters": {
            "voxel_m": float(voxel_m),
            "max_edge_voxel_mult": float(max_edge_voxel_mult),
            "max_edge_m": round(max_edge_m, 4),
            "xy_bounds_m": [round(float(x), 4) for x in xy_bounds],
            "interior_xy_bounds_m": [round(float(x), 4) for x in interior_xy_bounds],
            "z_min_m": float(z_min_m),
            "z_ceiling_m": round(float(z_ceiling_m), 4),
            "z_wall_max_m": round(float(z_wall_max_m), 4),
            "grazing_min_abs_cos": float(grazing_min_abs_cos),
            "min_cluster_faces": int(min_cluster_faces),
            "components_before_d": int(num_components_before_d),
            "components_after_d": int(num_components_after_d),
            "input_faces": int(n_total),
            "kept_faces": int(keep_d.sum()),
            "removed_faces": int(n_total - int(keep_d.sum())),
        },
    }


def clean_textured_glb_bytes(
    raw_glb: bytes,
    cameras: Sequence[CleanCamera] | dict[str, CleanCamera],
    *,
    voxel_m: float = 0.02,
    max_edge_voxel_mult: float = 8.0,
    xy_bounds: tuple[float, float, float, float] | None = None,
    z_min_m: float = -0.10,
    z_ceiling_m: float | None = None,
    z_wall_max_m: float | None = None,
    grazing_min_abs_cos: float = 0.25,
    min_cluster_faces: int = 500,
) -> tuple[bytes, dict[str, Any]]:
    """Run the 4-stage `MESH_CLEAN_V2` filter on `raw_glb` and return `(cleaned_glb_bytes, report)`."""
    parsed = parse_glb_triangles_world(raw_glb)
    res = compute_cleanup_stage_masks(
        parsed,
        cameras,
        voxel_m=voxel_m,
        max_edge_voxel_mult=max_edge_voxel_mult,
        xy_bounds=xy_bounds,
        z_min_m=z_min_m,
        z_ceiling_m=z_ceiling_m,
        z_wall_max_m=z_wall_max_m,
        grazing_min_abs_cos=grazing_min_abs_cos,
        min_cluster_faces=min_cluster_faces,
    )
    keep_mask = res["masks"]["d_small_components"]
    if len(keep_mask) == 0 or bool(keep_mask.all()):
        return raw_glb, res["parameters"]
    out_bytes, f_rep = filter_glb_triangles_bytes(raw_glb, keep_mask)
    report = {**res["parameters"], **f_rep}
    return out_bytes, report


def clean_glb_file(
    in_glb: Path,
    out_glb: Path,
    cameras: Sequence[CleanCamera] | dict[str, CleanCamera],
    **kwargs: Any,
) -> dict[str, Any]:
    """Read `in_glb`, run `clean_textured_glb_bytes`, write `out_glb`, and return the cleanup report."""
    cleaned_bytes, rep = clean_textured_glb_bytes(in_glb.read_bytes(), cameras, **kwargs)
    out_glb.parent.mkdir(parents=True, exist_ok=True)
    out_glb.write_bytes(cleaned_bytes)
    return rep

