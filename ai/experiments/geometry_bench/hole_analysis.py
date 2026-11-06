"""MapAnything Mask Options (A/B/C/D), TSDF Surface-Area Loss Ablation, and Per-Camera Hole Classification (S15P31A101-528).

Implements Lead Additional Instructions 2 & 3:
  (1) MapAnything 4 mask conditions:
      - A (`A_baseline`)     : default (`apply_mask=True`, `mask_edges=True`, `conf_percentile=10.0`)
      - B (`B_no_edge_mask`) : edge mask off (`apply_mask=True`, `mask_edges=False`, `conf_percentile=10.0`)
      - C (`C_conf_relaxed`) : confidence threshold relaxed (`apply_mask=True`, `mask_edges=True`, `conf_percentile=2.0` & `0.0`)
      - D (`D_both_off`)     : both edge mask off & confidence relaxed (`apply_mask=True`, `mask_edges=False`, `conf_percentile=2.0`)
  (2) Priority metrics ①~④ and automatic condition selection ('reduce holes without increasing false geometry'):
      ① Same-camera viewpoint black hole screen occupancy ratio (`same_cam_black_hole_ratio`)
      ② Fraction of newly created surface backed by occlusion-verified real photo (`new_faces_photo_supported_ratio`)
      ③ Floating cluster area (`floating_cluster_area_m2`) and triangle count (`removed_small_cluster_faces`)
      ④ Multi-camera depth reprojection / cross-view consistency error (`cross_view_reproj_median_px`, `cross_view_abs_m_p50`)
  (3) TSDF & post-processing surface-area loss analysis on Condition A depth:
      - Voxel size & truncation distance sweep
      - Stage-by-stage area waterfall & single-switch OFF ablations (`flying_mask`, `conflict_keep`, `min_cluster_faces`, `target_faces`)
  (4) Per-camera hole classification color map PNGs (`<cid>_hole_class.png`, matching `cameras.json` resolution for `-527` overlay):
      - Class A: Input depth missing × Mesh face missing (Red)
      - Class B: Input depth present × Mesh face missing (Amber/Yellow)
      - Class C: Input depth present × Mesh face present, but texture/render unobserved (Blue)
      (Visual diagnostic maps only; never included in reconstruction metrics.)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.calibration.world_frame import inputs as wfi
from simai_ai.scene import mesh_cleanup as mc
from simai_ai.scene import run_hybrid_depth as hd
from simai_ai.scene import texture_mesh as tm
from simai_ai.scene import texture_provenance as tp

from . import metrics as bm
from . import mvs_io as mio

# Color palette (RGB uint8) for per-camera hole classification PNGs (-527 overlay compatible)
HOLE_CLASS_COLORS: dict[str, tuple[int, int, int]] = {
    "A_input_missing_mesh_missing": (230, 55, 55),       # Red: missing in input depth & missing in mesh
    "B_input_valid_mesh_missing": (245, 190, 40),        # Amber: present in input depth, lost in TSDF/cleanup
    "C_input_valid_mesh_render_unobs": (55, 130, 240),   # Blue: present in input & mesh, but unobserved/dark in render
    "OK_input_valid_mesh_observed": (60, 185, 95),       # Green: present in input & mesh & observed texture
    "D_input_missing_mesh_present": (80, 200, 200),      # Cyan: missing in this camera's depth, filled by other cameras
    "OUTSIDE_VALID_MASK": (28, 28, 32),                  # Dark gray: outside undistorted valid lens mask
}


@dataclass(frozen=True)
class MaskConditionSpec:
    condition_id: str
    label: str
    mask_edges: bool
    conf_percentile: float
    rationale: str


MASK_CONDITIONS: tuple[MaskConditionSpec, ...] = (
    MaskConditionSpec(
        condition_id="A_baseline",
        label="A: 기준 (mask_edges=ON, conf_p=10%)",
        mask_edges=True,
        conf_percentile=10.0,
        rationale="현행 기본 설정 (하위 10% 신뢰도 절단 + 깊이 불연속 테두리 마스킹)",
    ),
    MaskConditionSpec(
        condition_id="B_no_edge_mask",
        label="B: 테두리 마스크 끔 (mask_edges=OFF, conf_p=10%)",
        mask_edges=False,
        conf_percentile=10.0,
        rationale="기둥·가구·벽 경계에서 mask_edges 가 잘라낸 유효 표면 복원 여부 확인",
    ),
    MaskConditionSpec(
        condition_id="C_conf_relaxed_p2",
        label="C: 신뢰도 완화 (mask_edges=ON, conf_p=2%)",
        mask_edges=True,
        conf_percentile=2.0,
        rationale="단색 바닥·벽면이 하위 10% 신뢰도에 몰려 잘리는 현상을 완화(하위 2% 극단치만 제거)",
    ),
    MaskConditionSpec(
        condition_id="C2_conf_relaxed_p0",
        label="C2: 신뢰도 절단 없음 (mask_edges=ON, conf_p=0%)",
        mask_edges=True,
        conf_percentile=0.0,
        rationale="신뢰도 백분위 절단을 완전히 껐을 때의 상한 확인",
    ),
    MaskConditionSpec(
        condition_id="D_both_off",
        label="D: 둘 다 완화 (mask_edges=OFF, conf_p=2%)",
        mask_edges=False,
        conf_percentile=2.0,
        rationale="테두리 마스크 해제 + 신뢰도 문턱 완화(2%) 동시 적용",
    ),
)


def run_mapanything_mask_variants_gpu(
    ma_root: Path,
    *,
    model_dir: Path | None = None,
) -> Path:
    """On a GPU Pod, run MapAnything inference with `mask_edges=True` and `mask_edges=False` on `<ma_root>/prepared`
    and save `<ma_root>/mapanything/raw_mask_variants.npz`.

    Uses the exact same model weights and `preprocess_inputs` as `simai_ai.scene.run_mapanything`.
    """
    import os
    import sys

    import torch

    ma_root = Path(ma_root)
    ma_out = ma_root / "mapanything"
    ma_out.mkdir(parents=True, exist_ok=True)
    out_npz = ma_out / "raw_mask_variants.npz"
    if out_npz.is_file():
        return out_npz

    simai_root = Path(os.environ.get("SIMAI_ROOT", "/workspace/simai"))
    ma_repo = Path(os.environ.get("SIMAI_MAPANYTHING_REPO") or simai_root / "third_party" / "map-anything")
    if str(ma_repo) not in sys.path:
        sys.path.insert(0, str(ma_repo))

    from mapanything.models import MapAnything
    from mapanything.utils.geometry import depthmap_to_world_frame
    from mapanything.utils.image import preprocess_inputs
    from simai_ai.scene.run_mapanything import build_prepared_views, prepared_views

    weights_path = Path(model_dir) if model_dir is not None else Path(
        os.environ.get("SIMAI_MAPANYTHING_WEIGHTS", str(simai_root / "weights" / "map-anything"))
    )
    specs = prepared_views(ma_root / "prepared")
    names = [sp["name"] for sp in specs]
    src = build_prepared_views(specs)
    views = preprocess_inputs(src)

    model = MapAnything.from_pretrained(str(weights_path)).to("cuda").eval()
    kw_default = dict(
        memory_efficient_inference=True,
        minibatch_size=None,
        use_amp=True,
        amp_dtype="bf16",
        apply_mask=True,
        mask_edges=True,
    )
    kw_no_edge = dict(
        memory_efficient_inference=True,
        minibatch_size=None,
        use_amp=True,
        amp_dtype="bf16",
        apply_mask=True,
        mask_edges=False,
    )
    with torch.no_grad():
        out_def = model.infer(views, **kw_default)
        out_no_edge = model.infer(views, **kw_no_edge)
    torch.cuda.synchronize()

    payload: dict[str, Any] = {"names": np.array(names)}
    for pred_def, pred_ne, name in zip(out_def, out_no_edge, names):
        g = lambda pr, k: pr[k][0].float().cpu().numpy() if torch.is_tensor(pr[k]) else np.asarray(pr[k])  # noqa: E731
        depth = g(pred_def, "depth_z")[..., 0]
        K = g(pred_def, "intrinsics")
        T = g(pred_def, "camera_poses")
        conf = g(pred_def, "conf")
        mask_def = g(pred_def, "mask")[..., 0].astype(bool)
        mask_ne = g(pred_ne, "mask")[..., 0].astype(bool)
        pts, valid = depthmap_to_world_frame(
            pred_def["depth_z"][0].squeeze(-1), pred_def["intrinsics"][0], pred_def["camera_poses"][0]
        )
        pts_np = pts.float().cpu().numpy()
        valid_np = valid.cpu().numpy().astype(bool) & np.isfinite(pts_np).all(-1)
        payload[f"{name}__depth_z"] = depth
        payload[f"{name}__conf"] = conf
        payload[f"{name}__mask"] = mask_def & valid_np
        payload[f"{name}__mask_no_edge"] = mask_ne & valid_np
        payload[f"{name}__intrinsics"] = K
        payload[f"{name}__camera_pose"] = T

    np.savez_compressed(out_npz, **payload)
    return out_npz


def build_depth_views_for_mask_condition(
    ma_run: Path,
    prepared: dict[str, Any],
    cams: dict[str, Any],
    spec: MaskConditionSpec,
) -> tuple[list[tm.DepthView], wfi.MapAnythingRun, dict[str, Any]]:
    """Construct `tm.DepthView` list and `wfi.MapAnythingRun` for a given `MaskConditionSpec` (A/B/C/D).

    Supports both `raw_mask_variants.npz` (with explicit `__mask_no_edge`) and standard `raw.npz`
    (where `depth_z` and `conf` are stored unmasked for every model pixel `(H, W)`).
    """
    ma_run = Path(ma_run)
    variants_npz = ma_run / "raw_mask_variants.npz"
    raw_path = variants_npz if variants_npz.is_file() else (ma_run / "raw.npz")
    raw = np.load(raw_path)

    cam_map_obj = wfi.load_mapanything_cameras(ma_run / "cameras.json", None)
    view_map = cam_map_obj.record["view_map"]
    prepared_views = {v["name"]: v for v in prepared.get("views", [])} if prepared else {}

    dviews: list[tm.DepthView] = []
    wfi_views: list[wfi.View] = []
    per_cam_stats: dict[str, Any] = {}

    for cid, name in sorted(view_map.items()):
        depth = raw[f"{name}__depth_z"].astype(np.float64)
        conf = raw[f"{name}__conf"].astype(np.float64)
        K = np.asarray(raw[f"{name}__intrinsics"], dtype=np.float64)
        c2w = np.asarray(cams[name]["cam2world"], dtype=np.float64)

        mask_def = raw[f"{name}__mask"].astype(bool)
        finite_pos = np.isfinite(depth) & (depth > 0.05) & np.isfinite(conf)

        if spec.mask_edges:
            base_mask = mask_def & finite_pos
        else:
            if f"{name}__mask_no_edge" in raw:
                base_mask = raw[f"{name}__mask_no_edge"].astype(bool) & finite_pos
            else:
                # In standard raw.npz, depth_z & conf are saved unmasked across the full frame;
                # pixels removed by mask_edges are finite positive depth pixels bordering mask_def.
                base_mask = finite_pos

        valid = base_mask.copy()
        if valid.any() and spec.conf_percentile > 0.0:
            thr = float(np.percentile(conf[valid], spec.conf_percentile))
            valid &= conf >= thr
        else:
            thr = float(np.min(conf[valid])) if valid.any() else 0.0

        person_pixels = None
        if prepared and name in prepared_views:
            pv = prepared_views[name]
            if pv.get("mask"):
                mask_img = mio._read_gray_mask(Path(prepared["_dir"]) / pv["mask"])
                if mask_img is not None:
                    person = wfi.mask_to_model_res(mask_img > 127, depth.shape)
                    person_pixels = int((person & valid).sum())
                    valid &= ~person

        cut = ~base_mask
        dviews.append(
            tm.DepthView(
                name=name,
                depth=np.where(valid, depth, np.nan).astype(np.float32),
                valid=valid.copy(),
                K=K,
                cam2world=c2w,
                cut=cut,
            )
        )
        wfi_views.append(
            wfi.View(
                camera_id=cid,
                name=name,
                depth=depth,
                K=K,
                cam2world=c2w,
                valid=valid.copy(),
                person_pixels=person_pixels,
            )
        )
        total_px = int(depth.size)
        per_cam_stats[cid] = {
            "total_model_pixels": total_px,
            "base_mask_pixels": int(base_mask.sum()),
            "valid_pixels": int(valid.sum()),
            "valid_pixel_ratio": round(float(valid.sum()) / max(total_px, 1), 5),
            "conf_threshold": round(thr, 5),
        }

    mean_valid_ratio = round(
        float(np.mean([v["valid_pixel_ratio"] for v in per_cam_stats.values()])), 5
    )
    ma_run_obj = wfi.MapAnythingRun(
        views=tuple(wfi_views),
        record={"condition_id": spec.condition_id, "per_camera": per_cam_stats},
    )
    return dviews, ma_run_obj, {"per_camera": per_cam_stats, "valid_pixel_ratio_mean": mean_valid_ratio}


def compute_floor_projected_hole_area(
    V_world: np.ndarray,
    F: np.ndarray,
    *,
    grid_res_m: float = 0.04,
    room_xy_bounds: tuple[float, float, float, float] | None = None,
) -> dict[str, float]:
    """Compute floor-projected mesh coverage and hole area (m^2) by casting vertical rays (-Z) onto `(V_world, F)`."""
    import open3d as o3d

    if len(V_world) == 0 or len(F) == 0:
        return {"room_floor_area_m2": 0.0, "covered_floor_area_m2": 0.0, "floor_projected_hole_area_m2": 0.0, "floor_hole_ratio": 1.0}

    if room_xy_bounds is None:
        p2 = np.percentile(V_world[:, :2], 2, axis=0)
        p98 = np.percentile(V_world[:, :2], 98, axis=0)
        x0, y0 = float(p2[0]), float(p2[1])
        x1, y1 = float(p98[0]), float(p98[1])
    else:
        x0, x1, y0, y1 = room_xy_bounds

    xs = np.arange(x0, x1, grid_res_m, dtype=np.float32)
    ys = np.arange(y0, y1, grid_res_m, dtype=np.float32)
    if len(xs) == 0 or len(ys) == 0:
        return {"room_floor_area_m2": 0.0, "covered_floor_area_m2": 0.0, "floor_projected_hole_area_m2": 0.0, "floor_hole_ratio": 1.0}

    gx, gy = np.meshgrid(xs, ys)
    n_rays = gx.size
    origins = np.column_stack([gx.reshape(-1), gy.reshape(-1), np.full(n_rays, 3.2, dtype=np.float32)])
    dirs = np.tile(np.array([[0.0, 0.0, -1.0]], dtype=np.float32), (n_rays, 1))
    rays = np.concatenate([origins, dirs], axis=1)

    t_mesh = o3d.t.geometry.TriangleMesh()
    t_mesh.vertex.positions = o3d.core.Tensor(np.ascontiguousarray(V_world, dtype=np.float32))
    t_mesh.triangle.indices = o3d.core.Tensor(np.ascontiguousarray(F, dtype=np.int32))
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(t_mesh)

    ans = scene.cast_rays(o3d.core.Tensor(rays))
    t_hit = ans["t_hit"].numpy()
    hit = np.isfinite(t_hit) & (t_hit > 0.05) & (t_hit < 6.0)

    cell_area = float(grid_res_m * grid_res_m)
    total_area = float(n_rays) * cell_area
    cov_area = float(int(hit.sum())) * cell_area
    hole_area = max(0.0, total_area - cov_area)
    return {
        "room_floor_area_m2": round(total_area, 3),
        "covered_floor_area_m2": round(cov_area, 3),
        "floor_projected_hole_area_m2": round(hole_area, 3),
        "floor_hole_ratio": round(hole_area / max(total_area, 1e-9), 5),
    }


def fuse_mesh_with_detailed_loss_stats(
    views: list[tm.DepthView],
    cfg: tm.Config,
    M: np.ndarray | None = None,
    *,
    apply_flying_filter: bool = True,
    apply_conflict_filter: bool = True,
    apply_manifold_clean: bool = True,
    apply_small_cluster_removal: bool = True,
    apply_decimation: bool = True,
    precomputed_keep_map: dict[str, np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Run TSDF fusion while recording exact surface area (m^2 in World) and face counts lost at each stage."""
    import open3d as o3d

    t0 = time.perf_counter()
    scale_m = tm.unit_scale(M)
    area_scale = float(scale_m * scale_m)

    if apply_conflict_filter:
        keep_map = precomputed_keep_map if precomputed_keep_map is not None else tm.conflict_keep(views)
    else:
        keep_map = {v.name: np.ones_like(v.valid, dtype=bool) for v in views}

    integ = o3d.pipelines.integration
    vol = integ.ScalableTSDFVolume(
        voxel_length=float(cfg.voxel_m),
        sdf_trunc=float(cfg.voxel_m * cfg.trunc_voxels),
        color_type=integ.TSDFVolumeColorType.NoColor,
    )

    pixel_stats: dict[str, Any] = {}
    flying_ratios: list[float] = []
    conflict_ratios: list[float] = []

    for v in views:
        fly = tm.flying_mask(v.depth, v.valid) if apply_flying_filter else np.zeros_like(v.valid, dtype=bool)
        use = v.valid & ~fly & keep_map[v.name]
        n_val = max(int(v.valid.sum()), 1)
        f_rat = float(fly.sum()) / n_val
        c_rat = float((v.valid & ~keep_map[v.name]).sum()) / n_val
        flying_ratios.append(f_rat)
        conflict_ratios.append(c_rat)
        pixel_stats[v.name] = {
            "valid_px": int(v.valid.sum()),
            "used_px": int(use.sum()),
            "flying_ratio": round(f_rat, 5),
            "conflict_ratio": round(c_rat, 5),
        }
        h, w = v.depth.shape
        d = np.where(use, v.depth, 0.0).astype(np.float32)
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(np.zeros((h, w, 3), np.uint8)),
            o3d.geometry.Image(d),
            depth_scale=1.0,
            depth_trunc=float(cfg.max_depth_m),
            convert_rgb_to_intensity=False,
        )
        K = v.K
        intr = o3d.camera.PinholeCameraIntrinsic(w, h, K[0, 0], K[1, 1], K[0, 2], K[1, 2])
        vol.integrate(rgbd, intr, np.linalg.inv(v.cam2world))

    mesh = vol.extract_triangle_mesh()

    def _mesh_area_world(m_obj) -> tuple[int, float]:
        if len(m_obj.triangles) == 0:
            return 0, 0.0
        V_arr = np.asarray(m_obj.vertices, dtype=np.float64)
        F_arr = np.asarray(m_obj.triangles, dtype=np.int64)
        return int(len(F_arr)), round(float(tp.face_areas(V_arr, F_arr).sum() * area_scale), 3)

    raw_faces, raw_area_m2 = _mesh_area_world(mesh)

    def _clean(m_obj):
        m_obj.remove_degenerate_triangles()
        m_obj.remove_duplicated_triangles()
        m_obj.remove_duplicated_vertices()
        m_obj.remove_non_manifold_edges()
        m_obj.remove_unreferenced_vertices()
        return m_obj

    if apply_manifold_clean:
        mesh = _clean(mesh)
    clean_faces, clean_area_m2 = _mesh_area_world(mesh)

    removed_small_faces = 0
    floating_cluster_area_m2 = 0.0
    if len(mesh.triangles) > 0:
        tri_c, n_tri, _ = mesh.cluster_connected_triangles()
        tri_c_np = np.asarray(tri_c)
        n_tri_np = np.asarray(n_tri)
        small_mask = n_tri_np[tri_c_np] < int(cfg.min_cluster_faces)
        removed_small_faces = int(small_mask.sum())
        if removed_small_faces > 0:
            V_tmp = np.asarray(mesh.vertices, dtype=np.float64)
            F_tmp = np.asarray(mesh.triangles, dtype=np.int64)
            floating_cluster_area_m2 = round(
                float(tp.face_areas(V_tmp, F_tmp[small_mask]).sum() * area_scale), 3
            )
            if apply_small_cluster_removal:
                mesh.remove_triangles_by_mask(small_mask)
                if apply_manifold_clean:
                    mesh = _clean(mesh)

    post_cluster_faces, post_cluster_area_m2 = _mesh_area_world(mesh)

    if apply_decimation and cfg.target_faces > 0 and len(mesh.triangles) > cfg.target_faces:
        mesh = mesh.simplify_quadric_decimation(int(cfg.target_faces))
        if apply_manifold_clean:
            mesh = _clean(mesh)

    V_out = np.asarray(mesh.vertices, dtype=np.float64)
    F_out = np.asarray(mesh.triangles, dtype=np.int64)
    if len(F_out) > 0:
        _, first = np.unique(tp.face_keys(F_out), return_index=True)
        F_out = F_out[np.sort(first)]
    final_faces = int(len(F_out))
    final_area_m2 = round(float(tp.face_areas(V_out, F_out).sum() * area_scale), 3) if final_faces > 0 else 0.0

    V_world = np.asarray(hd.transform_points(M, V_out), dtype=np.float64)
    F_world = np.asarray(F_out, dtype=np.int32)

    stats = {
        "voxel_m_world": round(float(cfg.voxel_m * scale_m), 4),
        "trunc_voxels": int(cfg.trunc_voxels),
        "trunc_m_world": round(float(cfg.voxel_m * cfg.trunc_voxels * scale_m), 4),
        "raw_tsdf_faces": raw_faces,
        "raw_tsdf_area_m2": raw_area_m2,
        "after_clean_faces": clean_faces,
        "after_clean_area_m2": clean_area_m2,
        "removed_small_cluster_faces": removed_small_faces,
        "floating_cluster_area_m2": floating_cluster_area_m2,
        "after_cluster_faces": post_cluster_faces,
        "after_cluster_area_m2": post_cluster_area_m2,
        "final_faces": final_faces,
        "final_area_m2": final_area_m2,
        "area_lost_clean_m2": round(max(0.0, raw_area_m2 - clean_area_m2), 3),
        "area_lost_small_clusters_m2": round(max(0.0, clean_area_m2 - post_cluster_area_m2), 3),
        "area_lost_decimation_m2": round(max(0.0, post_cluster_area_m2 - final_area_m2), 3),
        "flying_pixel_ratio_mean": round(float(np.mean(flying_ratios)), 5),
        "conflict_pixel_ratio_mean": round(float(np.mean(conflict_ratios)), 5),
        "per_view_pixels": pixel_stats,
        "seconds": round(time.perf_counter() - t0, 3),
    }
    return V_world, F_world, stats


def _build_pinhole_rays_world(
    K: np.ndarray,
    R_w2c: np.ndarray,
    center_world: np.ndarray,
    width: int,
    height: int,
) -> np.ndarray:
    """Build `(H, W, 6)` float32 ray array `(ox, oy, oz, dx, dy, dz)` in World coordinates (`+Z` up)."""
    fx = float(K[0, 0])
    fy = float(K[1, 1])
    cx = float(K[0, 2])
    cy = float(K[1, 2])
    vv, uu = np.meshgrid(
        np.arange(height, dtype=np.float32),
        np.arange(width, dtype=np.float32),
        indexing="ij",
    )
    dirs_cam = np.stack([(uu - cx) / fx, (vv - cy) / fy, np.ones_like(uu)], axis=-1)
    dirs_world = np.einsum("ij,hwj->hwi", R_w2c.T.astype(np.float32), dirs_cam)
    norms = np.linalg.norm(dirs_world, axis=-1, keepdims=True)
    dirs_unit = dirs_world / np.maximum(norms, 1e-8)
    origins = np.broadcast_to(
        np.asarray(center_world, dtype=np.float32).reshape(1, 1, 3),
        dirs_unit.shape,
    )
    return np.concatenate([origins, dirs_unit], axis=-1).astype(np.float32)


def _resize_mask_nearest(mask: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """Nearest-neighbor boolean mask resize without requiring OpenCV."""
    h, w = mask.shape[:2]
    if (h, w) == (target_h, target_w):
        return mask.astype(bool)
    yi = np.clip((np.arange(target_h, dtype=np.float64) * h / target_h).astype(np.int32), 0, h - 1)
    xi = np.clip((np.arange(target_w, dtype=np.float64) * w / target_w).astype(np.int32), 0, w - 1)
    return mask[np.ix_(yi, xi)].astype(bool)


def evaluate_camera_screen_holes_and_photo_support(
    V_world: np.ndarray,
    F: np.ndarray,
    cameras: dict[str, mio.UndistortedCamera],
    baseline_hit_masks: dict[str, np.ndarray] | None = None,
    *,
    raycast_scale: float = 0.5,
    occlusion_tol: float = 0.05,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Compute Priority Metrics ① (same-camera black hole occupancy ratio) and ② (photo-supported ratio of newly created surface)."""
    import open3d as o3d

    t_mesh = o3d.t.geometry.TriangleMesh()
    t_mesh.vertex.positions = o3d.core.Tensor(np.ascontiguousarray(V_world, dtype=np.float32))
    t_mesh.triangle.indices = o3d.core.Tensor(np.ascontiguousarray(F, dtype=np.int32))
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(t_mesh)
    invalid_id = int(o3d.t.geometry.RaycastingScene.INVALID_ID)

    # Precompute face normals in World frame to check grazing angle / front-facing photo support
    tris = V_world[F]
    fn = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    fn_norm = np.linalg.norm(fn, axis=1, keepdims=True)
    fn_unit = np.divide(fn, np.maximum(fn_norm, 1e-12))

    per_cam: dict[str, Any] = {}
    hit_masks_out: dict[str, np.ndarray] = {}
    hole_ratios: list[float] = []
    new_supported_px_total = 0
    new_total_px_total = 0

    for cid, cam in sorted(cameras.items()):
        rw = max(1, int(round(cam.width * raycast_scale)))
        rh = max(1, int(round(cam.height * raycast_scale)))
        sx = rw / float(cam.width)
        sy = rh / float(cam.height)
        K_s = cam.K.copy()
        K_s[0, :] *= sx
        K_s[1, :] *= sy

        rays = _build_pinhole_rays_world(K_s, cam.R_w2c, cam.center_world, rw, rh)
        ans = scene.cast_rays(o3d.core.Tensor(rays))
        prim = ans["primitive_ids"].numpy()
        t_hit = ans["t_hit"].numpy()
        hit = (prim != invalid_id) & np.isfinite(t_hit) & (t_hit > 0.05)

        val_mask = _resize_mask_nearest(cam.valid_mask, rw, rh)
        valid_lens_px = max(int(val_mask.sum()), 1)
        black_hole_mask = val_mask & ~hit
        black_hole_ratio = float(black_hole_mask.sum()) / valid_lens_px
        hole_ratios.append(black_hole_ratio)
        hit_masks_out[cid] = hit & val_mask

        # Check photo support (within valid undistorted photo mask, non-grazing |cos| >= 0.15, depth within room bounds)
        photo_supported = np.zeros_like(hit, dtype=bool)
        if bool((hit & val_mask).any()):
            h_idx = hit & val_mask
            f_ids = prim[h_idx].astype(np.int32)
            ray_d = rays[h_idx, 3:6]
            cos_inc = np.abs(np.sum(fn_unit[f_ids] * ray_d, axis=1))
            photo_supported[h_idx] = (cos_inc >= 0.15) & (t_hit[h_idx] <= tm.MAX_DEPTH_M)

        new_px = 0
        new_sup_px = 0
        if baseline_hit_masks is not None and cid in baseline_hit_masks:
            new_mask = (hit & val_mask) & ~baseline_hit_masks[cid]
            new_px = int(new_mask.sum())
            new_sup_px = int((new_mask & photo_supported).sum())
            new_total_px_total += new_px
            new_supported_px_total += new_sup_px

        per_cam[cid] = {
            "same_cam_black_hole_ratio": round(black_hole_ratio, 5),
            "same_cam_mesh_coverage_ratio": round(1.0 - black_hole_ratio, 5),
            "new_pixels_vs_A": new_px,
            "new_pixels_photo_supported": new_sup_px,
            "new_pixels_photo_supported_ratio": (
                round(float(new_sup_px) / new_px, 5) if new_px > 0 else 1.0
            ),
        }

    overall_new_sup_ratio = (
        round(float(new_supported_px_total) / new_total_px_total, 5)
        if new_total_px_total > 0
        else 1.0
    )
    summary = {
        "same_cam_black_hole_ratio_mean": round(float(np.mean(hole_ratios)), 5),
        "new_faces_photo_supported_ratio": overall_new_sup_ratio,
        "new_pixels_total_vs_A": int(new_total_px_total),
        "new_pixels_photo_supported_total": int(new_supported_px_total),
        "per_camera": per_cam,
    }
    return summary, hit_masks_out


def select_best_mask_condition(conditions_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Select the optimal MapAnything mask condition based on Lead Instruction 3-(2):
    'Reduce holes (①) without increasing false geometry (② photo support, ③ floating cluster area, ④ cross-view error).'
    """
    base = conditions_results["A_baseline"]
    base_hole = float(base["priority_metrics"]["1_same_cam_black_hole_ratio_mean"])
    base_float_area = float(base["priority_metrics"]["3_floating_cluster_area_m2"])
    base_reproj = float(base["priority_metrics"]["4_cross_view_reproj_median_px"] or 0.0)

    candidates: list[tuple[float, str, dict[str, Any]]] = []
    rejected: dict[str, str] = {}

    for cid, res in conditions_results.items():
        pm = res["priority_metrics"]
        hole_r = float(pm["1_same_cam_black_hole_ratio_mean"])
        photo_sup = float(pm["2_new_faces_photo_supported_ratio"])
        float_a = float(pm["3_floating_cluster_area_m2"])
        reproj_px = float(pm["4_cross_view_reproj_median_px"] or 0.0)

        delta_float = float_a - base_float_area
        delta_reproj = reproj_px - base_reproj

        if cid != "A_baseline":
            if photo_sup < 0.50:
                rejected[cid] = f"Rejected: new_faces_photo_supported_ratio ({photo_sup:.3f}) < 0.50"
                continue
            if delta_float > 0.75:
                rejected[cid] = f"Rejected: floating_cluster_area_m2 increased by +{delta_float:.3f} m^2 (> 0.75 m^2)"
                continue
            if delta_reproj > 1.50:
                rejected[cid] = f"Rejected: cross_view_reproj_median_px increased by +{delta_reproj:.3f} px (> 1.50 px)"
                continue

        candidates.append((hole_r, cid, pm))

    candidates.sort(key=lambda item: item[0])
    best_hole, best_id, best_pm = candidates[0]
    return {
        "selected_condition_id": best_id,
        "selected_same_cam_black_hole_ratio": best_hole,
        "hole_reduction_vs_A": round(base_hole - best_hole, 5),
        "selection_rule": (
            "Minimize ① same_cam_black_hole_ratio_mean subject to: "
            "② new_faces_photo_supported_ratio >= 0.50, "
            "③ delta_floating_cluster_area_m2 <= 0.75 m^2, "
            "④ delta_cross_view_reproj_median_px <= 1.50 px"
        ),
        "rejected_conditions": rejected,
        "selected_priority_metrics": best_pm,
    }


def evaluate_mask_conditions(
    ma_run: Path,
    prepared: dict[str, Any],
    cams_ma: dict[str, Any],
    M: np.ndarray | None,
    cameras_world: dict[str, mio.UndistortedCamera],
    *,
    voxel_m_world: float = 0.02,
    trunc_voxels: int = 8,
    target_faces: int = 150000,
) -> dict[str, Any]:
    """Run all MapAnything mask conditions (A/B/C/D) and produce the comparison table and selection decision."""
    scale_m = tm.unit_scale(M)
    voxel_ma = float(voxel_m_world) / max(scale_m, 1e-6)
    max_depth_ma = float(tm.MAX_DEPTH_M) / max(scale_m, 1e-6)
    cfg = tm.Config(
        voxel_m=voxel_ma,
        trunc_voxels=int(trunc_voxels),
        max_depth_m=max_depth_ma,
        min_cluster_faces=tm.MIN_CLUSTER_FACES,
        target_faces=int(target_faces),
    )

    orig_widths = {cid: cam.width for cid, cam in cameras_world.items()}
    results: dict[str, dict[str, Any]] = {}
    baseline_hit_masks: dict[str, np.ndarray] | None = None
    baseline_faces_count = 0
    room_xy_bounds: tuple[float, float, float, float] | None = None

    for spec in MASK_CONDITIONS:
        dviews, ma_run_obj, valid_stats = build_depth_views_for_mask_condition(
            ma_run, prepared, cams_ma, spec
        )
        cv_dist = mc.cross_view_distances(dviews, unit_scale=scale_m)
        reproj = wfi.cross_view_reprojection(ma_run_obj, original_width=orig_widths)
        reproj_meds = [
            v["median_px"]
            for v in reproj.get("cameras", {}).values()
            if isinstance(v, dict) and v.get("median_px") is not None
        ]
        reproj_p95s = [
            v["p95_px"]
            for v in reproj.get("cameras", {}).values()
            if isinstance(v, dict) and v.get("p95_px") is not None
        ]
        reproj_med_mean = round(float(np.mean(reproj_meds)), 3) if reproj_meds else None
        reproj_p95_mean = round(float(np.mean(reproj_p95s)), 3) if reproj_p95s else None

        V_world, F_world, fuse_stats = fuse_mesh_with_detailed_loss_stats(dviews, cfg, M)

        if room_xy_bounds is None and len(V_world) > 0:
            p2 = np.percentile(V_world[:, :2], 2, axis=0)
            p98 = np.percentile(V_world[:, :2], 98, axis=0)
            room_xy_bounds = (float(p2[0]), float(p98[0]), float(p2[1]), float(p98[1]))

        floor_hole = compute_floor_projected_hole_area(
            V_world, F_world, room_xy_bounds=room_xy_bounds
        )
        screen_eval, hit_masks = evaluate_camera_screen_holes_and_photo_support(
            V_world,
            F_world,
            cameras_world,
            baseline_hit_masks=baseline_hit_masks,
        )
        if spec.condition_id == "A_baseline":
            baseline_hit_masks = hit_masks
            baseline_faces_count = int(fuse_stats["raw_tsdf_faces"])

        cv_overall = cv_dist.get("overall", {})
        results[spec.condition_id] = {
            "label": spec.label,
            "mask_edges": spec.mask_edges,
            "conf_percentile": spec.conf_percentile,
            "rationale": spec.rationale,
            "valid_pixel_stats": valid_stats,
            "tsdf_mesh_stats": {
                **fuse_stats,
                "delta_raw_faces_vs_A": int(fuse_stats["raw_tsdf_faces"]) - baseline_faces_count,
            },
            "floor_hole_stats": floor_hole,
            "cross_view_depth": cv_overall,
            "cross_view_reprojection": {
                "median_px_mean": reproj_med_mean,
                "p95_px_mean": reproj_p95_mean,
                "per_camera": reproj.get("cameras", {}),
            },
            "screen_hole_and_support": screen_eval,
            "priority_metrics": {
                "1_same_cam_black_hole_ratio_mean": screen_eval["same_cam_black_hole_ratio_mean"],
                "2_new_faces_photo_supported_ratio": screen_eval["new_faces_photo_supported_ratio"],
                "3_floating_cluster_area_m2": fuse_stats["floating_cluster_area_m2"],
                "3_removed_small_cluster_faces": fuse_stats["removed_small_cluster_faces"],
                "4_cross_view_reproj_median_px": reproj_med_mean,
                "4_cross_view_abs_m_p50": cv_overall.get("abs_m_p50"),
                "4_cross_view_abs_rel_p50": cv_overall.get("abs_rel_p50"),
            },
        }

    selection = select_best_mask_condition(results)
    return {
        "conditions": results,
        "selection": selection,
    }


def analyze_tsdf_and_postprocess_losses(
    dviews_A: list[tm.DepthView],
    M: np.ndarray | None,
    cameras_world: dict[str, mio.UndistortedCamera],
) -> dict[str, Any]:
    """Lead Instruction 3-(3): Analyze surface area lost across TSDF voxel/truncation settings and each post-processing step."""
    scale_m = tm.unit_scale(M)
    max_depth_ma = float(tm.MAX_DEPTH_M) / max(scale_m, 1e-6)

    def _cfg(vox_m: float, trunc_vox: int, min_clust: int = 200, targ_faces: int = 150000) -> tm.Config:
        return tm.Config(
            voxel_m=float(vox_m) / max(scale_m, 1e-6),
            trunc_voxels=int(trunc_vox),
            max_depth_m=max_depth_ma,
            min_cluster_faces=int(min_clust),
            target_faces=int(targ_faces),
        )

    # Precompute conflict_keep once across dviews_A (avoids 10x redundant cross-camera projections)
    keep_map_cached = tm.conflict_keep(dviews_A)

    # 1. Stage-by-stage waterfall on (voxel=0.02m, trunc=8)
    base_cfg = _cfg(0.02, 8, 200, 150000)
    V_s0, F_s0, st_s0 = fuse_mesh_with_detailed_loss_stats(
        dviews_A,
        base_cfg,
        M,
        apply_flying_filter=False,
        apply_conflict_filter=False,
        apply_manifold_clean=False,
        apply_small_cluster_removal=False,
        apply_decimation=False,
        precomputed_keep_map=keep_map_cached,
    )
    V_s1, F_s1, st_s1 = fuse_mesh_with_detailed_loss_stats(
        dviews_A,
        base_cfg,
        M,
        apply_flying_filter=True,
        apply_conflict_filter=False,
        apply_manifold_clean=False,
        apply_small_cluster_removal=False,
        apply_decimation=False,
        precomputed_keep_map=keep_map_cached,
    )
    V_final, F_final, st_full = fuse_mesh_with_detailed_loss_stats(
        dviews_A,
        base_cfg,
        M,
        apply_flying_filter=True,
        apply_conflict_filter=True,
        apply_manifold_clean=True,
        apply_small_cluster_removal=True,
        apply_decimation=True,
        precomputed_keep_map=keep_map_cached,
    )

    waterfall = [
        {
            "stage": "S0_raw_tsdf_unfiltered",
            "description": "TSDF 통합 직후 (flying_mask OFF, conflict_keep OFF, 후처리 전)",
            "faces": st_s0["raw_tsdf_faces"],
            "area_m2": st_s0["raw_tsdf_area_m2"],
            "delta_area_m2": 0.0,
        },
        {
            "stage": "S1_after_flying_mask",
            "description": "깊이 경계 부유 점 제거 (flying_mask ON, conflict_keep OFF)",
            "faces": st_s1["raw_tsdf_faces"],
            "area_m2": st_s1["raw_tsdf_area_m2"],
            "delta_area_m2": round(st_s1["raw_tsdf_area_m2"] - st_s0["raw_tsdf_area_m2"], 3),
        },
        {
            "stage": "S2_after_conflict_keep",
            "description": "카메라 간 깊이 충돌 점 제거 (flying_mask ON + conflict_keep ON)",
            "faces": st_full["raw_tsdf_faces"],
            "area_m2": st_full["raw_tsdf_area_m2"],
            "delta_area_m2": round(st_full["raw_tsdf_area_m2"] - st_s1["raw_tsdf_area_m2"], 3),
        },
        {
            "stage": "S3_after_manifold_clean",
            "description": "비다양체 모서리·퇴화·중복 삼각형 제거 (clean)",
            "faces": st_full["after_clean_faces"],
            "area_m2": st_full["after_clean_area_m2"],
            "delta_area_m2": round(st_full["after_clean_area_m2"] - st_full["raw_tsdf_area_m2"], 3),
        },
        {
            "stage": "S4_after_small_cluster_removal",
            "description": f"작은 부유 조각 제거 (min_cluster_faces={base_cfg.min_cluster_faces})",
            "faces": st_full["after_cluster_faces"],
            "area_m2": st_full["after_cluster_area_m2"],
            "delta_area_m2": round(st_full["after_cluster_area_m2"] - st_full["after_clean_area_m2"], 3),
        },
        {
            "stage": "S5_after_quadric_decimation",
            "description": f"목표 면 수 단순화 (target_faces={base_cfg.target_faces})",
            "faces": st_full["final_faces"],
            "area_m2": st_full["final_area_m2"],
            "delta_area_m2": round(st_full["final_area_m2"] - st_full["after_cluster_area_m2"], 3),
        },
    ]

    # 2. Single-switch OFF ablations on (voxel=0.02m, trunc=8)
    switch_specs = [
        ("all_postproc_on", dict(apply_flying_filter=True, apply_conflict_filter=True, apply_small_cluster_removal=True, apply_decimation=True)),
        ("flying_mask_off", dict(apply_flying_filter=False, apply_conflict_filter=True, apply_small_cluster_removal=True, apply_decimation=True)),
        ("conflict_keep_off", dict(apply_flying_filter=True, apply_conflict_filter=False, apply_small_cluster_removal=True, apply_decimation=True)),
        ("small_cluster_removal_off", dict(apply_flying_filter=True, apply_conflict_filter=True, apply_small_cluster_removal=False, apply_decimation=True)),
        ("decimation_off", dict(apply_flying_filter=True, apply_conflict_filter=True, apply_small_cluster_removal=True, apply_decimation=False)),
    ]
    switch_ablations: dict[str, Any] = {}
    for sw_name, sw_kw in switch_specs:
        if sw_name == "all_postproc_on":
            V_sw, F_sw, st_sw = V_final, F_final, st_full
        else:
            V_sw, F_sw, st_sw = fuse_mesh_with_detailed_loss_stats(
                dviews_A, base_cfg, M, precomputed_keep_map=keep_map_cached, **sw_kw
            )
        scr_sw, _ = evaluate_camera_screen_holes_and_photo_support(V_sw, F_sw, cameras_world)
        fl_sw = compute_floor_projected_hole_area(V_sw, F_sw)
        switch_ablations[sw_name] = {
            "final_faces": st_sw["final_faces"],
            "final_area_m2": st_sw["final_area_m2"],
            "floating_cluster_area_m2": st_sw["floating_cluster_area_m2"],
            "floor_projected_hole_area_m2": fl_sw["floor_projected_hole_area_m2"],
            "same_cam_black_hole_ratio_mean": scr_sw["same_cam_black_hole_ratio_mean"],
        }

    # 3. Voxel & truncation distance sweep
    voxel_trunc_grid = [
        ("vox4cm_trunc8_32cm", 0.04, 8, 150000),
        ("vox2cm_trunc8_16cm", 0.02, 8, 150000),
        ("vox2cm_trunc12_24cm", 0.02, 12, 150000),
        ("vox1.5cm_trunc8_12cm", 0.015, 8, 550000),
        ("vox1.5cm_trunc12_18cm", 0.015, 12, 550000),
    ]
    voxel_sweep: dict[str, Any] = {}
    for tag, vm, tv, tf in voxel_trunc_grid:
        c_vt = _cfg(vm, tv, 200, tf)
        if tag == "vox2cm_trunc8_16cm":
            V_vt, F_vt, st_vt = V_final, F_final, st_full
        else:
            V_vt, F_vt, st_vt = fuse_mesh_with_detailed_loss_stats(
                dviews_A, c_vt, M, precomputed_keep_map=keep_map_cached
            )
        scr_vt, _ = evaluate_camera_screen_holes_and_photo_support(V_vt, F_vt, cameras_world)
        fl_vt = compute_floor_projected_hole_area(V_vt, F_vt)
        voxel_sweep[tag] = {
            "voxel_m": vm,
            "trunc_voxels": tv,
            "trunc_m": round(vm * tv, 4),
            "target_faces": tf,
            "raw_tsdf_area_m2": st_vt["raw_tsdf_area_m2"],
            "final_faces": st_vt["final_faces"],
            "final_area_m2": st_vt["final_area_m2"],
            "floor_projected_hole_area_m2": fl_vt["floor_projected_hole_area_m2"],
            "same_cam_black_hole_ratio_mean": scr_vt["same_cam_black_hole_ratio_mean"],
        }

    return {
        "stage_waterfall_vox2cm_trunc8": waterfall,
        "single_switch_off_ablations": switch_ablations,
        "voxel_truncation_sweep": voxel_sweep,
    }


def generate_camera_hole_classification_maps(
    out_dir: Path,
    glb_path: Path,
    cameras: dict[str, mio.UndistortedCamera],
    input_depth_valid_by_cam: dict[str, np.ndarray] | None = None,
) -> dict[str, Any]:
    """Lead Instruction 3-(4): Save per-camera hole classification PNGs (`<cid>_hole_class.png`)
    at the exact camera resolution (`cameras.json` / `<cid>.jpg`) for `-527` viewer overlay.

    Classification per pixel `(H, W)`:
      - Class A (`A_input_missing_mesh_missing`, Red)  : `~input_valid` & `~mesh_hit`
      - Class B (`B_input_valid_mesh_missing`, Amber)  : `input_valid` & `~mesh_hit`
      - Class C (`C_input_valid_mesh_render_unobs`, Blue): `input_valid` & `mesh_hit` & `~observed_texture`
      - OK (`OK_input_valid_mesh_observed`, Green)     : `input_valid` & `mesh_hit` & `observed_texture`
      - Class D (`D_input_missing_mesh_present`, Cyan) : `~input_valid` & `mesh_hit`

    Note: Visual diagnostic artifact only; never included in reconstruction metrics.
    """
    import open3d as o3d

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parsed = bm.parse_glb_primitives_world(glb_path)
    t_mesh = o3d.t.geometry.TriangleMesh()
    t_mesh.vertex.positions = o3d.core.Tensor(np.ascontiguousarray(parsed["V_world"], dtype=np.float32))
    t_mesh.triangle.indices = o3d.core.Tensor(np.ascontiguousarray(parsed["F"], dtype=np.int32))
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(t_mesh)
    invalid_id = int(o3d.t.geometry.RaycastingScene.INVALID_ID)

    # If no explicit MapAnything model-res valid mask is passed, derive input validity from multi-view depth support
    per_cam_report: dict[str, Any] = {}
    files_manifest: dict[str, dict[str, str]] = {}

    for cid, cam in sorted(cameras.items()):
        h, w = cam.height, cam.width
        rays = _build_pinhole_rays_world(cam.K, cam.R_w2c, cam.center_world, w, h)
        ans = scene.cast_rays(o3d.core.Tensor(rays))
        prim = ans["primitive_ids"].numpy()
        t_hit = ans["t_hit"].numpy()
        mesh_hit = (prim != invalid_id) & np.isfinite(t_hit) & (t_hit > 0.05)

        obs_mask = np.zeros((h, w), dtype=bool)
        if bool(mesh_hit.any()):
            f_ids = prim[mesh_hit].astype(np.int32)
            obs_mask[mesh_hit] = parsed["face_is_observed"][f_ids]

        lens_valid = cam.valid_mask.astype(bool)
        if input_depth_valid_by_cam is not None and cid in input_depth_valid_by_cam:
            in_val_raw = input_depth_valid_by_cam[cid].astype(bool)
            if in_val_raw.shape != (h, w):
                in_val = _resize_mask_nearest(in_val_raw, w, h) & lens_valid
            else:
                in_val = in_val_raw & lens_valid
        else:
            # On laptop when only undistorted photo + valid mask is present without raw.npz,
            # estimate input depth validity from valid lens region minus extreme dark/saturated sky regions
            luma = (
                0.299 * cam.image_rgb[:, :, 0].astype(np.float32)
                + 0.587 * cam.image_rgb[:, :, 1].astype(np.float32)
                + 0.114 * cam.image_rgb[:, :, 2].astype(np.float32)
            )
            in_val = lens_valid & (luma >= 8.0) & (luma <= 252.0)

        class_A = lens_valid & (~in_val) & (~mesh_hit)
        class_B = lens_valid & in_val & (~mesh_hit)
        class_C = lens_valid & in_val & mesh_hit & (~obs_mask)
        class_OK = lens_valid & in_val & mesh_hit & obs_mask
        class_D = lens_valid & (~in_val) & mesh_hit
        outside = ~lens_valid

        color_map = np.zeros((h, w, 3), dtype=np.uint8)
        color_map[outside] = HOLE_CLASS_COLORS["OUTSIDE_VALID_MASK"]
        color_map[class_A] = HOLE_CLASS_COLORS["A_input_missing_mesh_missing"]
        color_map[class_B] = HOLE_CLASS_COLORS["B_input_valid_mesh_missing"]
        color_map[class_C] = HOLE_CLASS_COLORS["C_input_valid_mesh_render_unobs"]
        color_map[class_OK] = HOLE_CLASS_COLORS["OK_input_valid_mesh_observed"]
        color_map[class_D] = HOLE_CLASS_COLORS["D_input_missing_mesh_present"]

        png_path = out_dir / f"{cid}_hole_class.png"
        overlay_path = out_dir / f"{cid}_hole_overlay.jpg"
        mio._write_rgb_image(png_path, color_map)

        blend = (0.52 * cam.image_rgb.astype(np.float32) + 0.48 * color_map.astype(np.float32)).astype(np.uint8)
        mio._write_rgb_image(overlay_path, blend)

        denom = max(int(lens_valid.sum()), 1)
        per_cam_report[cid] = {
            "resolution_wh": [int(w), int(h)],
            "valid_lens_pixels": int(lens_valid.sum()),
            "A_input_missing_mesh_missing_ratio": round(float(class_A.sum()) / denom, 5),
            "B_input_valid_mesh_missing_ratio": round(float(class_B.sum()) / denom, 5),
            "C_input_valid_mesh_render_unobs_ratio": round(float(class_C.sum()) / denom, 5),
            "OK_input_valid_mesh_observed_ratio": round(float(class_OK.sum()) / denom, 5),
            "D_input_missing_mesh_present_ratio": round(float(class_D.sum()) / denom, 5),
            "total_black_hole_ratio_A_plus_B": round(float((class_A | class_B).sum()) / denom, 5),
        }
        files_manifest[cid] = {
            "hole_class_png": str(png_path),
            "hole_overlay_jpg": str(overlay_path),
        }

    doc = {
        "format": "simai.hole_classification_overlay",
        "purpose": "DIAGNOSTIC_OVERLAY_ONLY_NOT_FOR_METRICS",
        "viewer_compatibility": "S15P31A101-527 camera overlay (matches cameras.json undistorted resolution)",
        "legend_rgb": {k: list(v) for k, v in HOLE_CLASS_COLORS.items()},
        "per_camera": per_cam_report,
        "files": files_manifest,
    }
    (out_dir / "hole_classification.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return doc


def synthesize_depth_views_from_mesh(
    cameras: dict[str, mio.UndistortedCamera],
    glb_path: Path,
    model_width: int = 518,
    model_height: int = 294,
    edge_drop_grad_thresh: float = 0.0,
) -> tuple[list[tm.DepthView], dict[str, np.ndarray]]:
    """Build real `tm.DepthView` list by raycasting an existing mesh at MapAnything resolution `(518, 294)`.

    Used on the laptop (when GPU `raw.npz` is not locally present) to exercise the exact
    `analyze_tsdf_and_postprocess_losses` and `generate_camera_hole_classification_maps` code paths
    against the 12th session mesh and `cameras.json`.
    """
    import open3d as o3d

    parsed = bm.parse_glb_primitives_world(glb_path)
    t_mesh = o3d.t.geometry.TriangleMesh()
    t_mesh.vertex.positions = o3d.core.Tensor(np.ascontiguousarray(parsed["V_world"], dtype=np.float32))
    t_mesh.triangle.indices = o3d.core.Tensor(np.ascontiguousarray(parsed["F"], dtype=np.int32))
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(t_mesh)
    invalid_id = int(o3d.t.geometry.RaycastingScene.INVALID_ID)

    views: list[tm.DepthView] = []
    valid_masks_by_cam: dict[str, np.ndarray] = {}

    for cid, cam in sorted(cameras.items()):
        sx = float(model_width) / float(cam.width)
        sy = float(model_height) / float(cam.height)
        K_m = np.array(
            [
                [cam.K[0, 0] * sx, 0.0, cam.K[0, 2] * sx],
                [0.0, cam.K[1, 1] * sy, cam.K[1, 2] * sy],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        rays = _build_pinhole_rays_world(K_m, cam.R_w2c, cam.center_world, model_width, model_height)
        ans = scene.cast_rays(o3d.core.Tensor(rays))
        prim = ans["primitive_ids"].numpy().reshape(model_height, model_width)
        t_hit = ans["t_hit"].numpy().reshape(model_height, model_width)
        hit = (prim != invalid_id) & np.isfinite(t_hit) & (t_hit > 0.1)

        # Convert ray distance t_hit along normalized world ray into pinhole camera Z depth
        u = np.arange(model_width, dtype=np.float64)
        v = np.arange(model_height, dtype=np.float64)
        uu, vv = np.meshgrid(u, v)
        x_n = (uu - K_m[0, 2]) / K_m[0, 0]
        y_n = (vv - K_m[1, 2]) / K_m[1, 1]
        ray_norm_cam = np.sqrt(x_n * x_n + y_n * y_n + 1.0)
        depth_z = np.where(hit, t_hit / np.maximum(ray_norm_cam, 1e-8), np.nan).astype(np.float32)

        valid_m = _resize_mask_nearest(cam.valid_mask, model_width, model_height) & np.isfinite(depth_z) & (depth_z > 0.1)
        if edge_drop_grad_thresh > 0.0:
            d_fill = np.where(valid_m, depth_z, 0.0)
            gx = np.abs(np.diff(d_fill, axis=1, prepend=d_fill[:, :1]))
            gy = np.abs(np.diff(d_fill, axis=0, prepend=d_fill[:1, :]))
            grad = np.hypot(gx, gy) / np.maximum(d_fill, 0.25)
            valid_m = valid_m & (grad <= edge_drop_grad_thresh)

        views.append(
            tm.DepthView(
                name=cid,
                depth=np.where(valid_m, depth_z, np.nan).astype(np.float32),
                valid=valid_m.copy(),
                K=K_m,
                cam2world=cam.cam2world.copy(),
                cut=~valid_m,
            )
        )
        valid_masks_by_cam[cid] = valid_m

    return views, valid_masks_by_cam


def format_hole_analysis_markdown(report: dict[str, Any]) -> str:
    """Format the full MapAnything mask comparison + TSDF loss + hole classification report as Markdown."""
    lines: list[str] = [
        "# MapAnything 마스크 옵션 비교 (A/B/C/D) · TSDF 면 손실 분해 · 카메라별 구멍 분류",
        "",
    ]

    mask_eval = report.get("mask_conditions_evaluation")
    if mask_eval and "conditions" in mask_eval:
        sel = mask_eval.get("selection", {})
        lines.extend(
            [
                "## 1. MapAnything 마스크 조건 4종 비교 (A / B / C / D)",
                "",
                f"- **선택된 최적 조건**: `{sel.get('selected_condition_id', 'A_baseline')}`",
                f"- **선택 기준**: {sel.get('selection_rule', '')}",
                "",
                "| 조건 | 설명 | 유효 깊이 픽셀 비율 (평균) | ① 동일 시점 검은 구멍 점유율 (평균) | ② 새로 생긴 면의 실제 사진 지지율 | ③ 떠 있는 조각 면적 (m²) | 제거된 작은 조각 삼각형 수 | ④ 다중 카메라 깊이 불일치 p50 (m) | ④ 다중 카메라 재투영 오차 중앙값 (px) | TSDF 메시 면적 (m²) | 바닥 투영 구멍 면적 (m²) |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for cond_id, row in mask_eval["conditions"].items():
            pm = row["priority_metrics"]
            v_rat = row["valid_pixel_stats"]["valid_pixel_ratio_mean"]
            m_area = row["tsdf_mesh_stats"]["final_area_m2"]
            fl_hole = row["floor_hole_stats"]["floor_projected_hole_area_m2"]
            p4_abs_m = pm.get("4_cross_view_abs_m_p50")
            p4_rep = pm.get("4_cross_view_reproj_median_px")
            p4_abs_s = f"{p4_abs_m:.4f}" if p4_abs_m is not None else "n/a"
            p4_rep_s = f"{p4_rep:.2f}" if p4_rep is not None else "n/a"
            lines.append(
                f"| `{cond_id}` | {row['label']} "
                f"| {v_rat * 100:.1f}% "
                f"| **{pm['1_same_cam_black_hole_ratio_mean'] * 100:.2f}%** "
                f"| **{pm['2_new_faces_photo_supported_ratio'] * 100:.1f}%** "
                f"| **{pm['3_floating_cluster_area_m2']:.3f}** "
                f"| {pm['3_removed_small_cluster_faces']:,} "
                f"| **{p4_abs_s}** "
                f"| {p4_rep_s} "
                f"| {m_area:.2f} "
                f"| {fl_hole:.2f} |"
            )
        lines.append("")

    tsdf_loss = report.get("tsdf_and_postprocess_loss_analysis")
    if tsdf_loss:
        wf = tsdf_loss.get("stage_waterfall_vox2cm_trunc8", [])
        lines.extend(
            [
                "## 2. TSDF 추출 및 `texture_mesh` 후처리 단계별 면적 손실 폭포(Waterfall)",
                "",
                "| 단계 | 설명 | 남아 있는 삼각형 수 | 남아 있는 표면적 (m²) | 직전 단계 대비 면적 변화 (m²) |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for row in wf:
            lines.append(
                f"| `{row['stage']}` | {row['description']} | {row['faces']:,} "
                f"| {row['area_m2']:.2f} | {row['delta_area_m2']:+.3f} |"
            )
        lines.extend(
            [
                "",
                "### 후처리 개별 스위치 OFF 비교 (동일 깊이 입력 기준)",
                "",
                "| 실험 ID | 최종 삼각형 수 | 최종 메시 면적 (m²) | 떠 있는 조각 면적 (m²) | 바닥 투영 구멍 면적 (m²) | ① 동일 시점 검은 구멍 점유율 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for sw_id, sw in tsdf_loss.get("single_switch_off_ablations", {}).items():
            lines.append(
                f"| `{sw_id}` | {sw['final_faces']:,} "
                f"| {sw['final_area_m2']:.2f} | {sw['floating_cluster_area_m2']:.3f} "
                f"| {sw['floor_projected_hole_area_m2']:.2f} "
                f"| {sw['same_cam_black_hole_ratio_mean'] * 100:.2f}% |"
            )
        lines.extend(
            [
                "",
                "### 복셀 크기 × 절단 거리(Truncation) 격자 비교",
                "",
                "| 격자 ID | `voxel_m` (m) | `trunc_voxels` | `trunc_m` (m) | 원본 TSDF 면적 (m²) | 최종 삼각형 수 | 최종 메시 면적 (m²) | 바닥 구멍 면적 (m²) | ① 동일 시점 검은 구멍 점유율 |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for g_id, gr in tsdf_loss.get("voxel_truncation_sweep", {}).items():
            lines.append(
                f"| `{g_id}` | {gr['voxel_m']:.3f} | {gr['trunc_voxels']} | {gr['trunc_m']:.3f} "
                f"| {gr['raw_tsdf_area_m2']:.2f} | {gr['final_faces']:,} | {gr['final_area_m2']:.2f} "
                f"| {gr['floor_projected_hole_area_m2']:.2f} "
                f"| {gr['same_cam_black_hole_ratio_mean'] * 100:.2f}% |"
            )
        lines.append("")

    hc = report.get("hole_classification_overlay")
    if hc and "per_camera" in hc:
        lines.extend(
            [
                "## 3. 카메라별 구멍 분류 색 지도 (A / B / C, `-527` 뷰어 겹쳐보기 호환)",
                "",
                "- **A (빨강 `[230, 55, 55]`)**: 깊이 입력 없음 × 최종 메시 면 없음 (원본 깊이/마스크 결손)",
                "- **B (노랑 `[245, 190, 40]`)**: 깊이 입력 있음 × 최종 메시 면 없음 (TSDF·후처리에서 잘려 나간 면)",
                "- **C (파랑 `[55, 130, 240]`)**: 깊이 입력 있음 × 최종 메시 면 있음 × 렌더 미관측/어두움 (텍스처/렌더 문제)",
                "- **OK (초록 `[60, 185, 95]`)**: 깊이 입력 있음 × 최종 메시 면 있음 × 관측 텍스처 정상",
                "",
                "| 카메라 | 해상도 | A: 입력 없음·면 없음 | B: 입력 있음·면 없음 (TSDF 손실) | C: 입력 있음·면 있음 (렌더 미관측) | OK: 정상 관측 면 | 전체 검은 구멍 (A+B) |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for cid, c_row in hc["per_camera"].items():
            w, h = c_row["resolution_wh"]
            lines.append(
                f"| `{cid}` | `{w}×{h}` "
                f"| **{c_row['A_input_missing_mesh_missing_ratio'] * 100:.2f}%** "
                f"| **{c_row['B_input_valid_mesh_missing_ratio'] * 100:.2f}%** "
                f"| **{c_row['C_input_valid_mesh_render_unobs_ratio'] * 100:.2f}%** "
                f"| {c_row['OK_input_valid_mesh_observed_ratio'] * 100:.2f}% "
                f"| {c_row['total_black_hole_ratio_A_plus_B'] * 100:.2f}% |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def run_pod_hole_and_tsdf_analysis(
    ma_single_root: Path,
    calib_path: Path,
    bundle_dir: Path,
    final_glb_path: Path,
    out_dir: Path,
    run_gpu_mask_infer: bool = False,
) -> dict[str, Any]:
    """Run the full Lead Instruction 2 & 3 suite on the GPU Pod (or against an existing `ma_single` root)."""
    ma_single_root = Path(ma_single_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cameras = mio.load_and_undistort_cameras(calib_path, bundle_dir)

    if run_gpu_mask_infer:
        run_mapanything_mask_variants_gpu(ma_single_root)

    ma_run = ma_single_root / "mapanything"
    prepared = hd.load_prepared(ma_single_root / "prepared")
    cams_ma, cam_sha = hd.read_cameras(ma_run)
    w_align_path = ma_single_root / "world_alignment.json"
    M = hd.load_world_alignment(w_align_path, cam_sha)[0] if w_align_path.is_file() else None

    mask_eval = evaluate_mask_conditions(
        ma_run=ma_run,
        prepared=prepared,
        cams_ma=cams_ma,
        M=M,
        cameras_world=cameras,
    )

    dviews_A, _, stats_A = build_depth_views_for_mask_condition(
        ma_run=ma_run,
        prepared=prepared,
        cams=cams_ma,
        spec=MASK_CONDITIONS[0],
    )
    tsdf_loss = analyze_tsdf_and_postprocess_losses(
        dviews_A=dviews_A,
        M=M,
        cameras_world=cameras,
    )

    cam_map_obj = wfi.load_mapanything_cameras(ma_run / "cameras.json", None)
    name_to_cid = {name: cid for cid, name in cam_map_obj.record["view_map"].items()}
    input_valid_by_cam: dict[str, np.ndarray] = {}
    for v in dviews_A:
        cid = name_to_cid.get(v.name, v.name)
        input_valid_by_cam[cid] = v.valid.copy()

    hole_class = generate_camera_hole_classification_maps(
        out_dir=out_dir / "hole_classification",
        glb_path=final_glb_path,
        cameras=cameras,
        input_depth_valid_by_cam=input_valid_by_cam,
    )

    report = {
        "mode": "pod_full_hole_and_tsdf_analysis",
        "ma_single_root": str(ma_single_root),
        "mask_conditions_evaluation": mask_eval,
        "tsdf_and_postprocess_loss_analysis": tsdf_loss,
        "hole_classification_overlay": hole_class,
        "condition_A_depth_stats": stats_A,
    }
    (out_dir / "hole_and_tsdf_analysis.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "hole_and_tsdf_analysis.md").write_text(
        format_hole_analysis_markdown(report), encoding="utf-8"
    )
    return report


def run_laptop_hole_and_tsdf_analysis(
    calib_path: Path,
    bundle_dir: Path,
    base_glb_path: Path,
    out_dir: Path,
) -> dict[str, Any]:
    """Run camera hole classification PNG generation + mesh-raycast TSDF/post-process loss analysis on the laptop.

    Generates `-527` compatible `<cid>_hole_class.png` (2304x1296) and `hole_and_tsdf_analysis.json`
    immediately on the laptop using the 12th session mesh and calibration.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cameras = mio.load_and_undistort_cameras(calib_path, bundle_dir)

    views_A, valid_masks_A = synthesize_depth_views_from_mesh(
        cameras=cameras,
        glb_path=base_glb_path,
        model_width=518,
        model_height=294,
        edge_drop_grad_thresh=0.08,
    )
    tsdf_loss = analyze_tsdf_and_postprocess_losses(
        dviews_A=views_A,
        M=None,
        cameras_world=cameras,
    )
    hole_class = generate_camera_hole_classification_maps(
        out_dir=out_dir / "hole_classification",
        glb_path=base_glb_path,
        cameras=cameras,
        input_depth_valid_by_cam=valid_masks_A,
    )
    report = {
        "mode": "laptop_hole_and_tsdf_analysis",
        "note": (
            "Laptop diagnostic run using 12th session current-scene-textured.glb and cameras.json. "
            "Full MapAnything raw.npz A/B/C/D mask sweep runs on the GPU Pod via `--mode hole-analysis --ma-single-root`."
        ),
        "tsdf_and_postprocess_loss_analysis": tsdf_loss,
        "hole_classification_overlay": hole_class,
    }
    (out_dir / "hole_and_tsdf_analysis.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "hole_and_tsdf_analysis.md").write_text(
        format_hole_analysis_markdown(report), encoding="utf-8"
    )
    return report

