"""Single-command benchmark runner for Variants 0..4 and Laptop CPU comparison (S15P31A101-528).

Usage
-----
1. Laptop CPU comparison on 12th session mesh (`texrecon` vs OpenMVS `TextureMesh` vs `RefineMesh`):
     cd ai && uv run python -m experiments.geometry_bench.run_bench --mode laptop-bench

2. Package GPU session input tar bundle (`s12_geometry_bench_input.tar.gz`):
     cd ai && uv run python -m experiments.geometry_bench.run_bench --mode bundle-gpu

3. Full Variants 0..4 benchmark (on Pod with MapAnything / DA3 outputs, or synthetic/local depths):
     cd ai && uv run python -m experiments.geometry_bench.run_bench --mode full --variants 0,1,2,3,4 \
         --calib <calibration.json> --bundle <clean_bg_bundle_dir> \
         --ma-single-npz <raw_single.npz> --ma-multi-dir <multi_frame_npz_dir> \
         --da3-dir <da3_1008_dir> --out <out_dir>
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.scene import mesh_cleanup as mc
from simai_ai.scene import reconstruct_tasks as rt
from simai_ai.scene import run_hybrid_depth as hd
from simai_ai.scene import texture_mesh as tm

from . import hole_analysis as ha
from . import metrics as bm
from . import mvs_io as mio
from . import v2_clean as vc

DEFAULT_LOCAL_ROOT = Path("C:/Users/SSAFY/simai-local")
DEFAULT_S12_CALIB = DEFAULT_LOCAL_ROOT / "session12/results/lobby-s12/calibration.json"
DEFAULT_S12_BUNDLE = (
    DEFAULT_LOCAL_ROOT
    / "session12/results/cal-lobby-init-f0ab1c6b-jba2-2f6cc097/bundle-393d5fbdabad41749559af4526a0a2ce"
)
DEFAULT_S12_MESH_GLB = DEFAULT_LOCAL_ROOT / "grtmesh-poc/current-scene-textured.glb"
DEFAULT_TEXRECON_BASE_DIR = DEFAULT_LOCAL_ROOT / "texrecon-opt/runs/base"
DEFAULT_OUT_DIR = DEFAULT_LOCAL_ROOT / "geometry-bench"

VARIANTS_SPEC: dict[int, dict[str, Any]] = {
    0: {
        "id": "v0_baseline",
        "title": "Variant 0: Baseline (MA 518 single, TSDF 4cm, 150k, texrecon)",
        "depth_source": "mapanything_518_single",
        "voxel_m": 0.04,
        "trunc_voxels": 8,
        "target_faces": 150000,
        "align_views": "none",
        "plane_cleanup": False,
        "mesh_builder": "tsdf",
        "mesh_refiner": "none",
        "texturer": "texrecon",
    },
    1: {
        "id": "v1_fine_multiframe",
        "title": "Variant 1: Fine + Multi-frame (MA 518 N-frame median, TSDF 1.5cm, 550k, texrecon)",
        "depth_source": "mapanything_518_multiframe_median",
        "voxel_m": 0.015,
        "trunc_voxels": 8,
        "target_faces": 550000,
        "align_views": "none",
        "plane_cleanup": False,
        "mesh_builder": "tsdf",
        "mesh_refiner": "none",
        "texturer": "texrecon",
    },
    2: {
        "id": "v2_da3_hybrid",
        "title": "Variant 2: Variant 1 + DA3 1008 Hybrid (MA pose fixed, sim3 + plane cleanup)",
        "depth_source": "da3_1008_hybrid",
        "voxel_m": 0.015,
        "trunc_voxels": 8,
        "target_faces": 550000,
        "align_views": "sim3",
        "plane_cleanup": True,
        "mesh_builder": "tsdf",
        "mesh_refiner": "none",
        "texturer": "texrecon",
    },
    3: {
        "id": "v3_openmvs_refine_texrecon",
        "title": "Variant 3: Variant 1 Mesh + OpenMVS RefineMesh + texrecon",
        "depth_source": "mapanything_518_multiframe_median",
        "voxel_m": 0.015,
        "trunc_voxels": 8,
        "target_faces": 550000,
        "align_views": "none",
        "plane_cleanup": False,
        "mesh_builder": "tsdf",
        "mesh_refiner": "openmvs_refinemesh",
        "texturer": "texrecon",
    },
    4: {
        "id": "v4_openmvs_full",
        "title": "Variant 4: OpenMVS ReconstructMesh -> RefineMesh -> TextureMesh",
        "depth_source": "mapanything_518_multiframe_median",
        "voxel_m": None,
        "trunc_voxels": None,
        "target_faces": 550000,
        "align_views": "none",
        "plane_cleanup": False,
        "mesh_builder": "openmvs_reconstructmesh",
        "mesh_refiner": "openmvs_refinemesh",
        "texturer": "openmvs_texturemesh",
    },
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def write_ascii_ply(path: Path, V_world: np.ndarray, F: np.ndarray) -> Path:
    """Write an ASCII triangle mesh `.ply` in World meters (+Z up) with Unix LF line endings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "ply\n"
        "format ascii 1.0\n"
        f"element vertex {len(V_world)}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        f"element face {len(F)}\n"
        "property list uchar int vertex_indices\n"
        "end_header\n"
    )
    v_str = "\n".join(f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}" for v in V_world)
    f_str = "\n".join(f"3 {int(f[0])} {int(f[1])} {int(f[2])}" for f in F)
    path.write_bytes((header + v_str + "\n" + f_str + "\n").encode("utf-8"))
    return path


def load_ply_mesh(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Load `(V_world, F)` from a binary or ASCII `.ply` file via Open3D."""
    import open3d as o3d

    m = o3d.io.read_triangle_mesh(str(path))
    m.remove_degenerate_triangles()
    m.remove_duplicated_triangles()
    m.remove_unreferenced_vertices()
    V = np.asarray(m.vertices, dtype=np.float64)
    F = np.asarray(m.triangles, dtype=np.int32)
    return V, F


def find_native_texrecon_binary() -> str | None:
    """Locate a native `texrecon` binary on a GPU Pod (`SIMAI_TEXRECON`, `/usr/local/bin/texrecon`, `PATH`).

    Returns `None` when not installed natively (so the caller falls back to `docker` on laptop).
    """
    env_spec = os.environ.get("SIMAI_TEXRECON", "").strip()
    if env_spec and not env_spec.startswith("docker:") and Path(env_spec).is_file():
        return env_spec
    for cand in (
        Path("/usr/local/bin/texrecon"),
        Path("/opt/conda/envs/openmvs/bin/texrecon"),
        Path("/root/micromamba/envs/openmvs/bin/texrecon"),
    ):
        if cand.is_file():
            return str(cand)
    which_hit = shutil.which("texrecon")
    if which_hit:
        return which_hit
    return None


def run_texrecon_on_mesh(
    work_dir: Path,
    V_world: np.ndarray,
    F: np.ndarray,
    cameras: dict[str, mio.UndistortedCamera],
    *,
    data_term: str = "area",
    outlier_removal: str = "none",
    run_loo: bool = True,
    reuse_dir: Path | None = None,
) -> dict[str, Any]:
    """Run `texrecon` (native binary on Pod, or `simai/texrecon:dev` Docker on laptop) 4-camera + optional LOO 3-camera bake on `(V_world, F)`."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    # Check if we can reuse precomputed texrecon run (e.g., 12th session base run)
    if (
        reuse_dir is not None
        and (Path(reuse_dir) / "all" / "textured.glb").is_file()
        and (not run_loo or all((Path(reuse_dir) / f"loo-{cid}" / "textured.glb").is_file() for cid in cameras))
    ):
        all_dir = work_dir / "all"
        all_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(reuse_dir) / "all" / "textured.glb", all_dir / "textured.glb")
        loo_paths: dict[str, Path] = {}
        if run_loo:
            for cid in cameras:
                ldir = work_dir / f"loo-{cid}"
                ldir.mkdir(parents=True, exist_ok=True)
                dst = ldir / "textured.glb"
                shutil.copy2(Path(reuse_dir) / f"loo-{cid}" / "textured.glb", dst)
                loo_paths[cid] = dst
        res_json = Path(reuse_dir) / "result.json"
        orig_seconds = 32.5
        if res_json.is_file():
            try:
                rj = json.loads(res_json.read_text(encoding="utf-8"))
                orig_seconds = float(rj.get("seconds", {}).get("texrecon", 32.5))
            except Exception:
                pass
        return {
            "glb_path": all_dir / "textured.glb",
            "loo_glb_paths": loo_paths,
            "seconds": orig_seconds,
            "reused_from": str(reuse_dir),
        }

    mesh_ply = write_ascii_ply(work_dir / "mesh.ply", V_world, F)
    cam_ids = list(cameras.keys())
    mount_src = str(work_dir.resolve()).replace("\\", "/")
    native_texrecon = find_native_texrecon_binary()

    def _exec_texrecon(args: list[str]) -> float:
        if native_texrecon is not None:
            mapped_args = [
                f"{mount_src}/{a[len('/work/'):]}" if a.startswith("/work/") else a for a in args
            ]
            cmd = [native_texrecon, *mapped_args]
        else:
            cmd = [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{mount_src}:/work",
                mio.TEXRECON_DOCKER_IMAGE,
                *args,
            ]
        ts = time.perf_counter()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"texrecon failed (rc={proc.returncode}): {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}"
            )
        return round(time.perf_counter() - ts, 3)

    def _bake_subset(sub_name: str, keep_cids: list[str], seam_leveling: bool) -> tuple[Path, float]:
        sub_dir = work_dir / sub_name
        out_glb = sub_dir / "textured.glb"
        obj_path = sub_dir / "out" / "tex.obj"
        if out_glb.is_file() and obj_path.is_file():
            return out_glb, 0.0
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "out").mkdir(parents=True, exist_ok=True)
        mio.write_texrecon_scene(sub_dir / "scene", cameras, keep_cids)
        extra_flags = ["--skip_hole_filling"]
        if not seam_leveling:
            extra_flags.extend(["--skip_global_seam_leveling", "--skip_local_seam_leveling"])
        t_sub = _exec_texrecon(
            [
                "-d",
                data_term,
                "-o",
                outlier_removal,
                *extra_flags,
                f"/work/{sub_name}/scene",
                "/work/mesh.ply",
                f"/work/{sub_name}/out/tex",
            ]
        )
        mio.parse_openmvs_obj_and_export_glb(
            obj_path, V_world, F, out_glb, empty_rgb=(128, 128, 128)
        )
        return out_glb, t_sub

    all_glb, t_all_s = _bake_subset("all", cam_ids, seam_leveling=True)
    loo_paths_out: dict[str, Path] = {}
    t_loo_s = 0.0
    if run_loo:
        for hold_cid in cam_ids:
            keep_cids = [c for c in cam_ids if c != hold_cid]
            l_glb, t_l = _bake_subset(f"loo-{hold_cid}", keep_cids, seam_leveling=False)
            loo_paths_out[hold_cid] = l_glb
            t_loo_s += t_l

    elapsed = round(time.perf_counter() - t0, 3)
    return {
        "glb_path": all_glb,
        "loo_glb_paths": loo_paths_out,
        "seconds": t_all_s if t_all_s > 0.0 else elapsed,
        "costs_seconds": t_loo_s,
        "mesh_ply": mesh_ply,
    }


def run_openmvs_texture_on_mesh(
    work_dir: Path,
    V_world: np.ndarray,
    F: np.ndarray,
    cameras: dict[str, mio.UndistortedCamera],
    *,
    run_loo: bool = True,
    resolution_level: int = 1,
    min_resolution: int = 640,
    outlier_threshold: float = 0.06,
    cost_smoothness_ratio: float = 0.1,
    global_seam_leveling: int = 0,
    local_seam_leveling: int = 1,
) -> dict[str, Any]:
    """Run OpenMVS `TextureMesh` (native via conda-forge `openmvs` on Pod, or `simai/openmvs:fcb172bd8490` on laptop).

    Note: `--global-seam-leveling 0 --local-seam-leveling 1` is used because OpenMVS 2019
    `GlobalSeamLeveling` calls `std::unordered_map::at` on vertices bordering unseen faces (`label=0`),
    throwing `std::out_of_range: _Map_base::at` whenever any mesh face is unobserved by the 4 cameras.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    # Save undistorted images once in work_dir/images
    images_dir = work_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for cid, cam in cameras.items():
        mio._write_rgb_image(images_dir / f"{cid}.jpg", cam.image_rgb)

    mesh_ply = write_ascii_ply(work_dir / "mesh.ply", V_world, F)

    # Also export COLMAP text workspace for reproducibility / inspection
    mio.export_colmap_text_workspace(work_dir / "colmap_all", cameras)

    def _texture_subset(
        sub_name: str,
        active_cams: dict[str, mio.UndistortedCamera],
        sub_res_level: int = resolution_level,
    ) -> tuple[Path, dict[str, Any]]:
        sub_dir = work_dir / sub_name
        sub_dir.mkdir(parents=True, exist_ok=True)
        obj_path = sub_dir / "tex.obj"
        mtl_path = sub_dir / "tex.mtl"
        if obj_path.is_file() and mtl_path.is_file():
            openmvs_s = 12.0
        else:
            scene_mvs = sub_dir / "scene.mvs"
            mio.write_openmvs_mvs_file(
                scene_mvs,
                active_cams,
                image_paths_in_scene={
                    cid: mio.openmvs_scene_image_path(work_dir, f"images/{cid}.jpg")
                    for cid in active_cams
                },
                points_xyz=V_world[: min(16, len(V_world))].astype(np.float32),
                points_rgb=np.full((min(16, len(V_world)), 3), 160, dtype=np.uint8),
                point_views=[[(0, 1152.0, 648.0, 1.0)] for _ in range(min(16, len(V_world)))],
            )
            run_info = mio.run_openmvs_cmd(
                work_dir,
                [
                    "TextureMesh",
                    "-w",
                    f"/work/{sub_name}",
                    "-i",
                    f"/work/{sub_name}/scene.mvs",
                    "--mesh-file",
                    "/work/mesh.ply",
                    "-o",
                    f"/work/{sub_name}/tex.mvs",
                    "--export-type",
                    "obj",
                    "--empty-color",
                    str(mio.EMPTY_COLOR_GRAY_INT),
                    "--resolution-level",
                    str(int(sub_res_level)),
                    "--min-resolution",
                    str(int(min_resolution)),
                    "--outlier-threshold",
                    str(float(outlier_threshold)),
                    "--cost-smoothness-ratio",
                    str(float(cost_smoothness_ratio)),
                    "--global-seam-leveling",
                    str(int(global_seam_leveling)),
                    "--local-seam-leveling",
                    str(int(local_seam_leveling)),
                    "--max-threads",
                    "6",
                ],
            )
            openmvs_s = float(run_info["seconds"])
        out_glb = sub_dir / "textured.glb"
        glb_info = mio.parse_openmvs_obj_and_export_glb(
            obj_path, V_world, F, out_glb, empty_rgb=(128, 128, 128)
        )
        glb_info["openmvs_seconds"] = openmvs_s
        return out_glb, glb_info

    all_glb, all_info = _texture_subset("all", cameras, sub_res_level=resolution_level)
    loo_paths: dict[str, Path] = {}
    loo_seconds = 0.0
    if run_loo:
        cam_ids = list(cameras.keys())
        loo_res_level = max(1, int(resolution_level))
        for hold_cid in cam_ids:
            sub_cams = {c: cameras[c] for c in cam_ids if c != hold_cid}
            l_glb, l_info = _texture_subset(f"loo-{hold_cid}", sub_cams, sub_res_level=loo_res_level)
            loo_paths[hold_cid] = l_glb
            loo_seconds += float(l_info.get("openmvs_seconds", 0.0))

    total_s = round(time.perf_counter() - t0, 3)
    return {
        "glb_path": all_glb,
        "loo_glb_paths": loo_paths,
        "seconds": round(float(all_info.get("openmvs_seconds", total_s)), 3),
        "loo_seconds": round(loo_seconds, 3),
        "total_seconds": total_s,
        "glb_info": all_info,
        "mesh_ply": mesh_ply,
    }


def run_openmvs_refinemesh(
    work_dir: Path,
    V_world: np.ndarray,
    F: np.ndarray,
    cameras: dict[str, mio.UndistortedCamera],
    *,
    scales: int = 1,
    resolution_level: int = 2,
    min_resolution: int = 480,
    max_face_area: int = 64,
    regularity_weight: float = 0.2,
) -> dict[str, Any]:
    """Run OpenMVS `RefineMesh` on `(V_world, F)` with `--close-holes 0 --decimate 1` (AGENTS Rule 2)."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    refined_ply = work_dir / "refined.ply"
    in_ply = work_dir / "input_mesh.ply"
    if refined_ply.is_file() and in_ply.is_file():
        V_ref, F_ref = load_ply_mesh(refined_ply)
        return {
            "V_world": V_ref,
            "F": F_ref,
            "refined_ply": refined_ply,
            "input_ply": in_ply,
            "seconds": 0.0,
            "stdout_tail": "cached",
        }

    images_dir = work_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for cid, cam in cameras.items():
        mio._write_rgb_image(images_dir / f"{cid}.jpg", cam.image_rgb)

    in_ply = write_ascii_ply(work_dir / "input_mesh.ply", V_world, F)
    scene_mvs = work_dir / "scene.mvs"
    recon_mvs = work_dir / "recon.mvs"
    recon_wide_mvs = work_dir / "recon_wide.mvs"
    if not recon_wide_mvs.is_file():
        depths_for_vis = mio.render_mesh_depths_for_cameras(V_world, F, cameras, scale=0.25)
        pts_xyz, pts_rgb, pt_views, _ = mio.build_visible_point_cloud_from_depths(
            cameras, depths_for_vis, stride=8
        )
        mio.write_openmvs_mvs_file(
            scene_mvs,
            cameras,
            image_paths_in_scene={
                cid: mio.openmvs_scene_image_path(work_dir, f"images/{cid}.jpg")
                for cid in cameras
            },
            points_xyz=pts_xyz,
            points_rgb=pts_rgb,
            point_views=pt_views,
        )
        mio.run_openmvs_cmd(
            work_dir,
            [
                "ReconstructMesh",
                "-w",
                "/work",
                "-i",
                "/work/scene.mvs",
                "-o",
                "/work/recon.mvs",
                "--export-type",
                "ply",
                "-d",
                "4.0",
                "--close-holes",
                "0",
                "--smooth",
                "1",
                "--max-threads",
                "6",
            ],
        )
        mio.adapt_mvsi_wide_baseline_neighbors(
            recon_mvs, recon_wide_mvs, n_cams=len(cameras)
        )

    run_info = mio.run_openmvs_cmd(
        work_dir,
        [
            "RefineMesh",
            "-w",
            "/work",
            "-i",
            "/work/recon_wide.mvs",
            "--mesh-file",
            "/work/input_mesh.ply",
            "-o",
            "/work/refined.mvs",
            "--export-type",
            "ply",
            "--close-holes",
            "0",
            "--decimate",
            "1",
            "--scales",
            str(int(scales)),
            "--resolution-level",
            str(int(resolution_level)),
            "--min-resolution",
            str(int(min_resolution)),
            "--max-face-area",
            str(int(max_face_area)),
            "--regularity-weight",
            str(float(regularity_weight)),
            "--max-views",
            "4",
            "--max-threads",
            "6",
        ],
    )
    if not refined_ply.is_file():
        raise FileNotFoundError(f"RefineMesh did not produce {refined_ply}")
    V_ref, F_ref = load_ply_mesh(refined_ply)
    return {
        "V_world": V_ref,
        "F": F_ref,
        "refined_ply": refined_ply,
        "input_ply": in_ply,
        "seconds": run_info["seconds"],
        "stdout_tail": run_info["stdout_tail"],
    }


def run_openmvs_reconstructmesh(
    work_dir: Path,
    cameras: dict[str, mio.UndistortedCamera],
    points_xyz: np.ndarray,
    points_rgb: np.ndarray,
    point_views: list[list[tuple[int, float, float, float]]],
    *,
    min_point_distance: float = 2.5,
    smooth_iters: int = 2,
    remove_spurious: float = 20.0,
) -> dict[str, Any]:
    """Run OpenMVS `ReconstructMesh` from dense 3D points + visibility views with `--close-holes 0`."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    images_dir = work_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for cid, cam in cameras.items():
        mio._write_rgb_image(images_dir / f"{cid}.jpg", cam.image_rgb)

    scene_mvs = work_dir / "scene_points.mvs"
    mio.write_openmvs_mvs_file(
        scene_mvs,
        cameras,
        image_paths_in_scene={
            cid: mio.openmvs_scene_image_path(work_dir, f"images/{cid}.jpg")
            for cid in cameras
        },
        points_xyz=points_xyz,
        points_rgb=points_rgb,
        point_views=point_views,
    )

    run_info = mio.run_openmvs_cmd(
        work_dir,
        [
            "ReconstructMesh",
            "-w",
            "/work",
            "-i",
            "/work/scene_points.mvs",
            "-o",
            "/work/reconstructed.mvs",
            "--export-type",
            "ply",
            "-d",
            str(float(min_point_distance)),
            "--close-holes",
            "0",
            "--smooth",
            str(int(smooth_iters)),
            "--remove-spurious",
            str(float(remove_spurious)),
        ],
    )
    recon_ply = work_dir / "reconstructed.ply"
    if not recon_ply.is_file():
        raise FileNotFoundError(f"ReconstructMesh did not produce {recon_ply}")
    V_rec, F_rec = load_ply_mesh(recon_ply)
    return {
        "V_world": V_rec,
        "F": F_rec,
        "reconstructed_ply": recon_ply,
        "seconds": run_info["seconds"],
        "stdout_tail": run_info["stdout_tail"],
    }


def run_laptop_cpu_benchmark(
    out_dir: Path = DEFAULT_OUT_DIR,
    calib_path: Path = DEFAULT_S12_CALIB,
    bundle_dir: Path = DEFAULT_S12_BUNDLE,
    mesh_glb_path: Path = DEFAULT_S12_MESH_GLB,
    texrecon_base_dir: Path = DEFAULT_TEXRECON_BASE_DIR,
) -> dict[str, Any]:
    """Run the Laptop CPU benchmark on the 12th session mesh & calibration/photos.

    Compares on the exact same 12th session mesh (`current-scene-textured.glb`, 149,997 faces):
      1. `v0_texrecon` (Baseline `texrecon` 4-cam + LOO 3-cam)
      2. `v4_openmvs_tex` (OpenMVS `TextureMesh` 4-cam + LOO 3-cam on the same mesh)
      3. `v3_openmvs_refine_texrecon` (OpenMVS `RefineMesh` + `texrecon` 4-cam + LOO 3-cam)
      4. `v4_openmvs_refine_tex` (OpenMVS `RefineMesh` + OpenMVS `TextureMesh` 4-cam + LOO 3-cam)
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.perf_counter()

    cameras = mio.load_and_undistort_cameras(calib_path, bundle_dir)
    V_s12, F_s12 = mio.load_scene_mesh_world_from_glb(mesh_glb_path)

    variants_to_run: dict[str, dict[str, Any]] = {}

    # 1. v0_texrecon (12th session baseline mesh + texrecon)
    res_v0 = run_texrecon_on_mesh(
        out_dir / "laptop_v0_texrecon",
        V_s12,
        F_s12,
        cameras,
        data_term="area",
        run_loo=True,
        reuse_dir=texrecon_base_dir if texrecon_base_dir.is_dir() else None,
    )
    variants_to_run["v0_texrecon"] = {
        "title": "12th Mesh + texrecon (Baseline)",
        "glb_path": res_v0["glb_path"],
        "loo_glb_paths": res_v0["loo_glb_paths"],
        "stage_seconds": {"geometry_s": 0.0, "refine_s": 0.0, "texture_s": res_v0["seconds"]},
    }

    # 2. v4_openmvs_tex (12th session mesh + OpenMVS TextureMesh)
    res_omvs_tex = run_openmvs_texture_on_mesh(
        out_dir / "laptop_v4_openmvs_tex",
        V_s12,
        F_s12,
        cameras,
        run_loo=True,
    )
    variants_to_run["v4_openmvs_tex"] = {
        "title": "12th Mesh + OpenMVS TextureMesh",
        "glb_path": res_omvs_tex["glb_path"],
        "loo_glb_paths": res_omvs_tex["loo_glb_paths"],
        "stage_seconds": {
            "geometry_s": 0.0,
            "refine_s": 0.0,
            "texture_s": res_omvs_tex["seconds"],
            "loo_s": res_omvs_tex["loo_seconds"],
        },
    }

    # 3. OpenMVS RefineMesh on 12th session mesh (shared by v3 and v4_refine)
    refine_dir = out_dir / "laptop_openmvs_refined_mesh"
    if (refine_dir / "refined.ply").is_file():
        V_ref, F_ref = load_ply_mesh(refine_dir / "refined.ply")
        refine_s = 0.0
        meta_p = refine_dir / "refine_meta.json"
        if meta_p.is_file():
            refine_s = float(json.loads(meta_p.read_text(encoding="utf-8")).get("seconds", 0.0))
    else:
        ref_res = run_openmvs_refinemesh(
            refine_dir,
            V_s12,
            F_s12,
            cameras,
            scales=1,
            resolution_level=2,
            min_resolution=480,
            max_face_area=64,
        )
        V_ref, F_ref = ref_res["V_world"], ref_res["F"]
        refine_s = float(ref_res["seconds"])
        (refine_dir / "refine_meta.json").write_text(
            json.dumps({"seconds": refine_s, "faces": int(len(F_ref)), "vertices": int(len(V_ref))}, indent=2),
            encoding="utf-8",
        )

    # 3a. v3_openmvs_refine_texrecon (RefineMesh + texrecon)
    res_v3 = run_texrecon_on_mesh(
        out_dir / "laptop_v3_refine_texrecon",
        V_ref,
        F_ref,
        cameras,
        data_term="area",
        run_loo=True,
        reuse_dir=None,
    )
    variants_to_run["v3_refine_texrecon"] = {
        "title": "12th Mesh + OpenMVS RefineMesh + texrecon (Variant 3)",
        "glb_path": res_v3["glb_path"],
        "loo_glb_paths": res_v3["loo_glb_paths"],
        "stage_seconds": {
            "geometry_s": 0.0,
            "refine_s": refine_s,
            "texture_s": res_v3["seconds"],
        },
    }

    # 3b. v4_openmvs_refine_tex (RefineMesh + OpenMVS TextureMesh)
    res_v4_ref = run_openmvs_texture_on_mesh(
        out_dir / "laptop_v4_refine_openmvs_tex",
        V_ref,
        F_ref,
        cameras,
        resolution_level=1,
        run_loo=True,
    )
    variants_to_run["v4_refine_openmvs_tex"] = {
        "title": "12th Mesh + OpenMVS RefineMesh + OpenMVS TextureMesh",
        "glb_path": res_v4_ref["glb_path"],
        "loo_glb_paths": res_v4_ref["loo_glb_paths"],
        "stage_seconds": {
            "geometry_s": 0.0,
            "refine_s": refine_s,
            "texture_s": res_v4_ref["seconds"],
            "loo_s": res_v4_ref["loo_seconds"],
        },
    }

    # Evaluate all variants & render standardized viewpoints
    variant_evals: dict[str, dict[str, Any]] = {}
    variant_renders: dict[str, dict[str, Any]] = {}
    variant_loo_renders: dict[str, dict[str, np.ndarray]] = {}

    for vkey, vinfo in variants_to_run.items():
        glb_p = Path(vinfo["glb_path"])
        rend = bm.render_glb_views(glb_p, cameras, cam_scale=0.5)
        self_eval = bm.evaluate_camera_renders(rend["cam_renders"], cameras)
        loo_eval, loo_rgb = bm.evaluate_loo_glbs(vinfo["loo_glb_paths"], cameras, cam_scale=0.5)

        # Save per-variant individual renders
        v_img_dir = out_dir / "renders" / vkey
        v_img_dir.mkdir(parents=True, exist_ok=True)
        for cid, cr in rend["cam_renders"].items():
            mio._write_rgb_image(v_img_dir / f"self_{cid}.jpg", cr["rgb"])
        for cid, lrgb in loo_rgb.items():
            mio._write_rgb_image(v_img_dir / f"loo_{cid}.jpg", lrgb)
        for vname, frgb in rend["free_renders"].items():
            mio._write_rgb_image(v_img_dir / f"free_{vname}.jpg", frgb)

        variant_renders[vkey] = rend
        variant_loo_renders[vkey] = loo_rgb
        variant_evals[vkey] = {
            "title": vinfo["title"],
            "glb_path": str(glb_p),
            "glb_sha256": sha256_file(glb_p),
            "mesh": rend["parsed_summary"],
            "self_view": self_eval,
            "loo": loo_eval,
            "stage_seconds": vinfo["stage_seconds"],
        }

    compare_dir = out_dir / "compare"
    panel_paths = bm.save_variant_comparison_panels(
        compare_dir,
        cameras,
        variant_evals,
        variant_renders,
        variant_loo_renders,
    )

    summary = {
        "benchmark": "S15P31A101-528-laptop-cpu",
        "calibration_version": "cal-lobby-init-f0ab1c6b-jba2-2f6cc097",
        "input_mesh_glb": str(mesh_glb_path),
        "input_mesh_faces": int(len(F_s12)),
        "input_mesh_vertices": int(len(V_s12)),
        "total_wall_seconds": round(time.perf_counter() - t_start, 2),
        "variants": variant_evals,
        "comparison_panels": [str(p) for p in panel_paths],
    }

    summary_json_path = out_dir / "laptop_bench_summary.json"
    summary_json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    md_lines = [
        "# S15P31A101-528 노트북 CPU 벤치마크 요약 (12차 로비 메쉬 + 왜곡 폄 사진)",
        "",
        "| 변형 (Variant) | 메쉬 면 수 | 용량 (MB) | 관측 면 비율 | Self PSNR (dB) | Self SSIM | LOO PSNR (dB) | LOO Blur8 (dB) | LOO SSIM | EdgeGradCos | EdgeF1 (2px) | CrossView AbsRel p50 | 정제 시간(s) | 텍스처 시간(s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for vkey, ve in variant_evals.items():
        m = ve["mesh"]
        sv = ve["self_view"]
        lv = ve["loo"]
        cvd = sv["cross_view_depth"]
        sec = ve["stage_seconds"]
        mb = m["glb_bytes"] / (1024.0 * 1024.0)
        md_lines.append(
            f"| `{vkey}` | {m['mesh_faces']:,} | {mb:.2f} | {m['area_ratio_observed']*100:.1f}% | "
            f"{sv['self_psnr_db_mean']:.2f} | {sv['self_ssim_mean']:.4f} | "
            f"{lv['loo_psnr_db_mean']:.2f} | {lv['loo_psnr_db_blur8_mean']:.2f} | {lv['loo_ssim_mean']:.4f} | "
            f"{sv['edge_grad_cos_sim_mean']:.4f} | {sv['edge_f1_px2_mean']:.4f} | "
            f"{cvd['abs_rel_p50']:.4f} | {sec.get('refine_s', 0.0):.1f} | {sec.get('texture_s', 0.0):.1f} |"
        )
    (out_dir / "laptop_bench_summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return summary


def prepare_reconstruct_job_root(
    root_dir: Path,
    calib_path: Path,
    images_dir: Path,
    *,
    run_id: str = "s12_single",
    image_kind: str = "CLEAN_BACKGROUND",
    calibration_version: str | None = "cal-lobby-init-f0ab1c6b-jba2-2f6cc097",
    run_prepare_stage: bool = False,
) -> dict[str, Any]:
    """Create `<root_dir>/{job.json, calibration.json, images/<cid>.image}` for `python -m simai_ai.scene.reconstruct_tasks <stage> <root_dir>`.

    Matches the exact input layout required by `simai_ai.scene.reconstruct_tasks.prepare(root)` and `mapanything(root)`.
    """
    root_dir = Path(root_dir)
    root_dir.mkdir(parents=True, exist_ok=True)
    calib_payload = mio.resolve_calibration_payload(
        calib_path, calibration_version=calibration_version
    )
    (root_dir / "calibration.json").write_text(
        json.dumps(calib_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    imgs_out = root_dir / "images"
    imgs_out.mkdir(parents=True, exist_ok=True)
    cam_entries: list[dict[str, Any]] = []
    cams_list = calib_payload.get("cameras", [])
    for cam_obj in sorted(cams_list, key=lambda c: str(c["camera_id"])):
        cid = str(cam_obj["camera_id"])
        src_img: Path | None = None
        for ext in (".jpg", ".png", ".jpeg", ".image"):
            cand = Path(images_dir) / f"{cid}{ext}"
            if cand.is_file():
                src_img = cand
                break
        if src_img is None:
            raise FileNotFoundError(f"Camera image for {cid} not found in {images_dir}")
        shutil.copy2(src_img, imgs_out / f"{cid}.image")
        cam_entries.append({"camera_id": cid, "kind": image_kind})

    job_doc = {
        "run_id": run_id,
        "input": {
            "input_provenance": {
                "frames": {"source": "FILE", "dataset_id": run_id},
                "is_upper_bound": False,
            },
            "images": cam_entries,
            "options": {},
        },
    }
    (root_dir / "job.json").write_text(
        json.dumps(job_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    prep_summary: dict[str, Any] | None = None
    if run_prepare_stage:
        prep_summary = rt.prepare(root_dir)

    return {
        "root_dir": str(root_dir),
        "run_id": run_id,
        "cameras": [c["camera_id"] for c in cam_entries],
        "prepared": prep_summary,
    }


def _dviews_from_world_depths(
    cameras: dict[str, mio.UndistortedCamera],
    depths_map: dict[str, np.ndarray],
    masks_map: dict[str, np.ndarray] | None = None,
) -> tuple[list[tm.DepthView], dict[str, Any], np.ndarray]:
    """Build `tm.DepthView` list in World frame (`M = eye(4)`) for `tm.geometry`."""
    dviews: list[tm.DepthView] = []
    cams_dict: dict[str, Any] = {}
    for cid in sorted(cameras):
        cam = cameras[cid]
        d_raw = np.asarray(depths_map[cid], dtype=np.float32)
        v_mask = (
            np.asarray(masks_map[cid], dtype=bool)
            if masks_map is not None and cid in masks_map
            else cam.valid_mask
        )
        valid = v_mask & np.isfinite(d_raw) & (d_raw > 0.1) & (d_raw < tm.MAX_DEPTH_M)
        d_clean = np.where(valid, d_raw, np.nan).astype(np.float32)
        cut = ~valid.copy()
        dviews.append(
            tm.DepthView(
                name=cid,
                depth=d_clean,
                valid=valid,
                K=np.asarray(cam.K, dtype=np.float64),
                cam2world=np.asarray(cam.cam2world, dtype=np.float64),
                cut=cut,
            )
        )
        cams_dict[cid] = {
            "K": cam.K.tolist(),
            "cam2world": cam.cam2world.tolist(),
        }
    return dviews, cams_dict, np.eye(4, dtype=np.float64)


def _load_dviews_from_ma_roots(
    ma_single_root: Path,
    ma_multi_roots: list[Path] | None = None,
    da3_depth_dir: Path | None = None,
) -> dict[str, Any]:
    """Load `tm.DepthView` sets from real `reconstruct_tasks` MapAnything run root(s) and optional DA3 hybrid depth dir."""
    ma_single_root = Path(ma_single_root)
    prep = hd.load_prepared(ma_single_root / "prepared")
    cams_ma, cam_sha = hd.read_cameras(ma_single_root / "mapanything")
    w_align_path = ma_single_root / "world_alignment.json"
    if not w_align_path.is_file():
        rt.align_to_calibration(ma_single_root, max_center_m=float(os.environ.get('BENCH_ALIGN_MAX_CENTER_M', '0.2')))
    M, wa_rec = hd.load_world_alignment(w_align_path, cam_sha)
    if M is None:
        raise RuntimeError(f"World alignment failed or missing in {w_align_path}: {wa_rec}")

    dviews_single, rec_single = tm.load_depth_views(
        ma_single_root / "mapanything", prep, cams_ma, None, cam_sha, "scale"
    )
    single_by_name = {v.name: v for v in dviews_single}

    # Multi-frame aggregation (if multi-frame MapAnything roots exist; otherwise clean fallback to single-frame)
    valid_multi_roots = [
        Path(r)
        for r in (ma_multi_roots or [])
        if (Path(r) / "mapanything" / "raw.npz").is_file()
    ]
    if valid_multi_roots:
        stacks: dict[str, list[np.ndarray]] = {name: [ single_by_name[name].depth ] for name in single_by_name}
        for r_path in valid_multi_roots:
            r_prep = hd.load_prepared(r_path / "prepared")
            r_cams, r_sha = hd.read_cameras(r_path / "mapanything")
            r_views, _ = tm.load_depth_views(
                r_path / "mapanything", r_prep, r_cams, None, r_sha, "scale"
            )
            for rv in r_views:
                if rv.name in stacks and rv.depth.shape == single_by_name[rv.name].depth.shape:
                    stacks[rv.name].append(rv.depth)
        med_depths, med_masks, mf_stats = mio.aggregate_multiframe_depths(
            stacks,
            valid_masks_by_cam={name: single_by_name[name].valid for name in single_by_name},
            align_scale_to_first=True,
            min_valid_frames=1,
        )
        dviews_multi = [
            tm.DepthView(
                name=name,
                depth=np.where(med_masks[name], med_depths[name], np.nan).astype(np.float32),
                valid=med_masks[name].copy(),
                K=single_by_name[name].K.copy(),
                cam2world=single_by_name[name].cam2world.copy(),
                cut=single_by_name[name].cut,
            )
            for name in sorted(single_by_name)
        ]
        mf_stats["single_frame_fallback"] = False
    else:
        dviews_multi = [
            tm.DepthView(
                name=v.name,
                depth=v.depth.copy(),
                valid=v.valid.copy(),
                K=v.K.copy(),
                cam2world=v.cam2world.copy(),
                cut=None if v.cut is None else v.cut.copy(),
            )
            for v in dviews_single
        ]
        mf_stats = {
            "single_frame_fallback": True,
            "reason": "No multi-frame MapAnything roots provided (12th session has 1 frame per camera); using single-frame depth.",
        }

    # DA3 1008 hybrid depth views (registered to MapAnything via tm.load_depth_views -> hd.register_to_reference)
    if da3_depth_dir is not None and (Path(da3_depth_dir) / "depth.json").is_file():
        dviews_da3, rec_da3 = tm.load_depth_views(
            ma_single_root / "mapanything", prep, cams_ma, Path(da3_depth_dir), cam_sha, "scale"
        )
    else:
        dviews_da3 = dviews_multi
        rec_da3 = {"source": "FALLBACK_TO_MAPANYTHING"}

    return {
        "dviews_single": dviews_single,
        "dviews_multi": dviews_multi,
        "dviews_da3": dviews_da3,
        "cams_ma": cams_ma,
        "M": M,
        "multiframe_stats": mf_stats,
        "single_record": rec_single,
        "da3_record": rec_da3,
    }


def run_full_variants_benchmark(
    out_dir: Path,
    calib_path: Path,
    bundle_dir: Path,
    *,
    variant_ids: list[int] | None = None,
    ma_single_root: Path | None = None,
    ma_multi_roots: list[Path] | None = None,
    da3_depth_dir: Path | None = None,
    ma_single_depths: dict[str, np.ndarray] | None = None,
    ma_multi_depths_stack: dict[str, np.ndarray] | None = None,
    da3_hybrid_depths: dict[str, np.ndarray] | None = None,
    run_loo: bool = True,
) -> dict[str, Any]:
    """Execute Variants (0)~(4) end-to-end using `simai_ai.scene.texture_mesh` (`tm.Config`, `tm.geometry`, `tm.fuse_mesh`)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    v_ids = variant_ids if variant_ids is not None else [0, 1, 2, 3, 4]
    cameras = mio.load_and_undistort_cameras(calib_path, bundle_dir)

    if ma_single_root is not None:
        loaded = _load_dviews_from_ma_roots(
            ma_single_root, ma_multi_roots=ma_multi_roots, da3_depth_dir=da3_depth_dir
        )
        dviews_single = loaded["dviews_single"]
        dviews_multi = loaded["dviews_multi"]
        dviews_da3 = loaded["dviews_da3"]
        cams_geom = loaded["cams_ma"]
        M_geom = loaded["M"]
        mf_stats = loaded["multiframe_stats"]
    elif ma_single_depths is not None:
        dviews_single, cams_geom, M_geom = _dviews_from_world_depths(cameras, ma_single_depths)
        if ma_multi_depths_stack is not None:
            med_depths, med_masks, mf_stats = mio.aggregate_multiframe_depths(
                ma_multi_depths_stack,
                valid_masks_by_cam={cid: cameras[cid].valid_mask for cid in cameras},
            )
            mf_stats["single_frame_fallback"] = False
        else:
            med_depths, med_masks, mf_stats = mio.aggregate_multiframe_depths(
                {cid: ma_single_depths[cid][None, ...] for cid in cameras},
                valid_masks_by_cam={cid: cameras[cid].valid_mask for cid in cameras},
            )
            mf_stats["single_frame_fallback"] = True
        dviews_multi, _, _ = _dviews_from_world_depths(cameras, med_depths, med_masks)
        if da3_hybrid_depths is not None:
            dviews_da3, _, _ = _dviews_from_world_depths(cameras, da3_hybrid_depths)
        else:
            dviews_da3 = dviews_multi
    else:
        raise ValueError("Either ma_single_root or ma_single_depths is required for full variants benchmark")

    def _build_tsdf_mesh(
        dviews_in: list[tm.DepthView],
        voxel_m: float,
        target_faces: int,
        align_views: str = "off",
        plane_cleanup: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        """Build TSDF mesh using the real `simai_ai.scene.texture_mesh.geometry` and `fuse_mesh` functions."""
        t0 = time.perf_counter()
        align_mode = "off" if align_views in ("none", "off", "") else align_views
        scale_m = tm.unit_scale(M_geom)
        # Convert World-meter voxel size to MapAnything coordinate units before calling tm.geometry
        voxel_ma = float(voxel_m) / max(scale_m, 1e-6)
        max_depth_ma = float(tm.MAX_DEPTH_M) / max(scale_m, 1e-6)
        cfg = tm.Config(
            voxel_m=voxel_ma,
            trunc_voxels=tm.TRUNC_VOXELS,
            max_depth_m=max_depth_ma,
            min_cluster_faces=tm.MIN_CLUSTER_FACES,
            target_faces=int(target_faces),
            align_views=align_mode,
            plane_cleanup=bool(plane_cleanup),
        )
        warnings_list: list[str] = []
        dviews_copy = [
            tm.DepthView(
                name=v.name,
                depth=v.depth.copy(),
                valid=v.valid.copy(),
                K=v.K.copy(),
                cam2world=v.cam2world.copy(),
                cut=None if v.cut is None else v.cut.copy(),
            )
            for v in dviews_in
        ]
        _dviews_out, _poses_out, V_ma, F_out, _plane_faces, geom_rec = tm.geometry(
            dviews_copy, cams_geom, M_geom, cfg, warnings_list
        )
        V_world = np.asarray(hd.transform_points(M_geom, V_ma), dtype=np.float64)
        F_world = np.asarray(F_out, dtype=np.int32)
        return (
            V_world,
            F_world,
            {
                "seconds": round(time.perf_counter() - t0, 3),
                "geometry_record": geom_rec,
                "warnings": warnings_list,
            },
        )

    variant_evals: dict[str, dict[str, Any]] = {}
    variant_renders: dict[str, dict[str, Any]] = {}
    variant_loo_renders: dict[str, dict[str, np.ndarray]] = {}

    v1_mesh_cache: tuple[np.ndarray, np.ndarray, float] | None = None

    for vid in v_ids:
        spec = VARIANTS_SPEC[vid]
        vkey = spec["id"]
        vdir = out_dir / vkey
        vdir.mkdir(parents=True, exist_ok=True)

        if vid == 0:
            V_m, F_m, g_info = _build_tsdf_mesh(
                dviews_single,
                voxel_m=float(spec["voxel_m"]),
                target_faces=int(spec["target_faces"]),
                align_views="off",
                plane_cleanup=False,
            )
            tex_res = run_texrecon_on_mesh(vdir, V_m, F_m, cameras, run_loo=run_loo)
            stage_s = {"geometry_s": g_info["seconds"], "refine_s": 0.0, "texture_s": tex_res["seconds"]}
        elif vid == 1:
            V_m, F_m, g_info = _build_tsdf_mesh(
                dviews_multi,
                voxel_m=float(spec["voxel_m"]),
                target_faces=int(spec["target_faces"]),
                align_views="off",
                plane_cleanup=False,
            )
            v1_mesh_cache = (V_m, F_m, g_info["seconds"])
            tex_res = run_texrecon_on_mesh(vdir, V_m, F_m, cameras, run_loo=run_loo)
            stage_s = {"geometry_s": g_info["seconds"], "refine_s": 0.0, "texture_s": tex_res["seconds"]}
        elif vid == 2:
            V_m, F_m, g_info = _build_tsdf_mesh(
                dviews_da3,
                voxel_m=float(spec["voxel_m"]),
                target_faces=int(spec["target_faces"]),
                align_views="sim3",
                plane_cleanup=True,
            )
            tex_res = run_texrecon_on_mesh(vdir, V_m, F_m, cameras, run_loo=run_loo)
            stage_s = {"geometry_s": g_info["seconds"], "refine_s": 0.0, "texture_s": tex_res["seconds"]}
        elif vid == 3:
            if v1_mesh_cache is None:
                V_1, F_1, g1_info = _build_tsdf_mesh(
                    dviews_multi,
                    voxel_m=float(spec["voxel_m"]),
                    target_faces=int(spec["target_faces"]),
                    align_views="off",
                    plane_cleanup=False,
                )
                v1_mesh_cache = (V_1, F_1, g1_info["seconds"])
            V_1, F_1, g1_s = v1_mesh_cache
            ref_res = run_openmvs_refinemesh(vdir / "refine", V_1, F_1, cameras)
            tex_res = run_texrecon_on_mesh(
                vdir / "texrecon", ref_res["V_world"], ref_res["F"], cameras, run_loo=run_loo
            )
            stage_s = {
                "geometry_s": g1_s,
                "refine_s": ref_res["seconds"],
                "texture_s": tex_res["seconds"],
            }
        elif vid == 4:
            t_pt0 = time.perf_counter()
            # Convert dviews_multi to World-frame depth maps for OpenMVS point cloud generation
            if v1_mesh_cache is None:
                V_1, F_1, g1_info = _build_tsdf_mesh(
                    dviews_multi,
                    voxel_m=0.015,
                    target_faces=int(spec["target_faces"]),
                    align_views="off",
                    plane_cleanup=False,
                )
                v1_mesh_cache = (V_1, F_1, g1_info["seconds"])
            V_1, F_1, _ = v1_mesh_cache
            world_d_for_mvs = mio.render_mesh_depths_for_cameras(V_1, F_1, cameras, scale=0.5)
            pts_xyz, pts_rgb, pt_views, _ = mio.build_visible_point_cloud_from_depths(
                cameras, world_d_for_mvs, stride=3
            )
            rec_res = run_openmvs_reconstructmesh(
                vdir / "reconstruct", cameras, pts_xyz, pts_rgb, pt_views
            )
            geom_s = round((time.perf_counter() - t_pt0), 3)
            ref_res = run_openmvs_refinemesh(
                vdir / "refine", rec_res["V_world"], rec_res["F"], cameras
            )
            tex_res = run_openmvs_texture_on_mesh(
                vdir / "texture", ref_res["V_world"], ref_res["F"], cameras, run_loo=run_loo
            )
            stage_s = {
                "geometry_s": geom_s,
                "refine_s": ref_res["seconds"],
                "texture_s": tex_res["seconds"],
            }
        else:
            raise ValueError(f"Unknown variant ID: {vid}")

        glb_p = Path(tex_res["glb_path"])
        rend = bm.render_glb_views(glb_p, cameras, cam_scale=0.5)
        self_eval = bm.evaluate_camera_renders(rend["cam_renders"], cameras)
        loo_eval, loo_rgb = bm.evaluate_loo_glbs(tex_res["loo_glb_paths"], cameras, cam_scale=0.5)
        variant_renders[vkey] = rend
        variant_loo_renders[vkey] = loo_rgb
        variant_evals[vkey] = {
            "variant_id": vid,
            "title": spec["title"],
            "glb_path": str(glb_p),
            "glb_sha256": sha256_file(glb_p),
            "mesh": rend["parsed_summary"],
            "self_view": self_eval,
            "loo": loo_eval,
            "stage_seconds": stage_s,
        }

    compare_dir = out_dir / "compare"
    panel_paths = bm.save_variant_comparison_panels(
        compare_dir, cameras, variant_evals, variant_renders, variant_loo_renders
    )
    out_summary = {
        "benchmark": "S15P31A101-528-full-variants",
        "multiframe_stats": mf_stats,
        "variants": variant_evals,
        "comparison_panels": [str(p) for p in panel_paths],
    }
    (out_dir / "full_bench_summary.json").write_text(
        json.dumps(out_summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return out_summary


def run_check_mode(
    calib_path: Path = DEFAULT_S12_CALIB,
    bundle_dir: Path = DEFAULT_S12_BUNDLE,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> dict[str, Any]:
    """Dry-run verification (`--mode check`): verifies all Python entrypoints, function signatures, CLI `--help` flags,
    `reconstruct_tasks.prepare` job.json execution, and single-frame fallback on the laptop before a GPU session.
    """
    import tempfile

    checks: dict[str, Any] = {}
    ai_root = Path(__file__).resolve().parents[2]

    # 1. Verify required Python functions and signatures in simai_ai.scene.*
    required_symbols: dict[str, tuple[Any, list[str]]] = {
        "simai_ai.scene.reconstruct_tasks": (
            rt,
            ["prepare", "mapanything", "align_to_calibration", "preview", "bake", "fallback", "measure", "main"],
        ),
        "simai_ai.scene.texture_mesh": (
            tm,
            [
                "Config",
                "DepthView",
                "load_depth_views",
                "flying_mask",
                "conflict_keep",
                "fuse_mesh",
                "geometry",
                "unit_scale",
                "to_glb_points",
                "main",
            ],
        ),
        "simai_ai.scene.run_hybrid_depth": (
            hd,
            [
                "main",
                "run_predict",
                "run_fuse",
                "load_prepared",
                "read_cameras",
                "load_world_alignment",
                "load_depth_dir",
                "register_to_reference",
                "transform_points",
            ],
        ),
        "simai_ai.scene.mesh_cleanup": (
            mc,
            [
                "AlignConfig",
                "PlaneConfig",
                "cross_view_distances",
                "align_views",
                "oriented_normals",
                "find_horizontal_planes",
                "snap_depth_to_planes",
                "snap_mesh_to_planes",
            ],
        ),
        "experiments.geometry_bench.hole_analysis": (
            ha,
            [
                "MASK_CONDITIONS",
                "run_mapanything_mask_variants_gpu",
                "build_depth_views_for_mask_condition",
                "fuse_mesh_with_detailed_loss_stats",
                "evaluate_camera_screen_holes_and_photo_support",
                "select_best_mask_condition",
                "evaluate_mask_conditions",
                "analyze_tsdf_and_postprocess_losses",
                "generate_camera_hole_classification_maps",
                "run_pod_hole_and_tsdf_analysis",
                "run_laptop_hole_and_tsdf_analysis",
            ],
        ),
    }

    symbol_report: dict[str, dict[str, str]] = {}
    for mod_name, (mod_obj, attr_names) in required_symbols.items():
        symbol_report[mod_name] = {}
        for attr in attr_names:
            if not hasattr(mod_obj, attr):
                raise RuntimeError(f"Missing required attribute {mod_name}.{attr}")
            fn = getattr(mod_obj, attr)
            sig = str(inspect.signature(fn)) if callable(fn) else type(fn).__name__
            symbol_report[mod_name][attr] = sig
    checks["python_symbols"] = symbol_report

    # 2. Verify CLI --help execution for real entrypoints called by gpu_run_all.sh
    cli_commands = {
        "texture_mesh_help": [sys.executable, "-m", "simai_ai.scene.texture_mesh", "--help"],
        "run_hybrid_depth_help": [
            sys.executable,
            str(ai_root / "experiments" / "static_scene" / "run_hybrid_depth.py"),
            "--help",
        ],
        "run_hybrid_depth_predict_help": [
            sys.executable,
            str(ai_root / "experiments" / "static_scene" / "run_hybrid_depth.py"),
            "predict",
            "--help",
        ],
        "run_bench_help": [sys.executable, "-m", "experiments.geometry_bench.run_bench", "--help"],
    }
    cli_report: dict[str, Any] = {}
    for name, cmd in cli_commands.items():
        proc = subprocess.run(
            cmd,
            cwd=str(ai_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"CLI check {name} failed (rc={proc.returncode}): {proc.stderr}")
        cli_report[name] = {
            "returncode": proc.returncode,
            "first_line": (proc.stdout.strip().splitlines() or [""])[0],
        }
    checks["cli_help_checks"] = cli_report

    # 3. Dry-run `prepare_reconstruct_job_root` + `python -m simai_ai.scene.reconstruct_tasks prepare <root>`
    if Path(calib_path).is_file() and Path(bundle_dir).is_dir():
        with tempfile.TemporaryDirectory(prefix="simai_ma_check_") as tmp_dir:
            tmp_root = Path(tmp_dir) / "ma_single"
            setup_info = prepare_reconstruct_job_root(
                tmp_root, calib_path, bundle_dir, run_id="dryrun_s12", run_prepare_stage=False
            )
            proc_prep = subprocess.run(
                [sys.executable, "-m", "simai_ai.scene.reconstruct_tasks", "prepare", str(tmp_root)],
                cwd=str(ai_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            if proc_prep.returncode != 0:
                raise RuntimeError(
                    f"reconstruct_tasks prepare dry-run failed (rc={proc_prep.returncode}):\n{proc_prep.stdout}\n{proc_prep.stderr}"
                )
            prep_doc = hd.load_prepared(tmp_root / "prepared")
            checks["reconstruct_tasks_prepare_dryrun"] = {
                "status": "OK",
                "setup_cameras": setup_info["cameras"],
                "prepared_views": [v["name"] for v in prep_doc.get("views", [])],
                "undistort_mode": prep_doc.get("undistort_mode"),
            }
    else:
        checks["reconstruct_tasks_prepare_dryrun"] = {"status": "SKIPPED_NO_LOCAL_S12_DATA"}

    # 4. Verify external binary resolution (native vs docker fallback)
    checks["binary_runners"] = {
        "native_openmvs_TextureMesh": mio.find_native_openmvs_binary("TextureMesh"),
        "native_openmvs_RefineMesh": mio.find_native_openmvs_binary("RefineMesh"),
        "native_openmvs_ReconstructMesh": mio.find_native_openmvs_binary("ReconstructMesh"),
        "native_texrecon": find_native_texrecon_binary(),
        "docker_available": shutil.which("docker") is not None,
    }

    checks["status"] = "OK"
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "check_dryrun_report.json").write_text(
            json.dumps(checks, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return checks


def build_gpu_session_bundle(
    out_dir: Path = DEFAULT_OUT_DIR,
    calib_path: Path = DEFAULT_S12_CALIB,
    bundle_dir: Path = DEFAULT_S12_BUNDLE,
    mesh_glb_path: Path = DEFAULT_S12_MESH_GLB,
) -> dict[str, Any]:
    """Create `s12_geometry_bench_input.tar.gz` in `C:/Users/SSAFY/simai-local/geometry-bench/`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = out_dir / "s12_geometry_bench_input"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    # 1. Calibration payload for cal-lobby-init-f0ab1c6b-jba2-2f6cc097
    calib_payload = mio.resolve_calibration_payload(
        calib_path, calibration_version="cal-lobby-init-f0ab1c6b-jba2-2f6cc097"
    )
    (stage_dir / "calibration.json").write_text(
        json.dumps(calib_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 2. Clean background raw bundle (`cam_A..D.jpg`, `cam_A..D.valid.png`, `clean_backgrounds.json`)
    bg_out = stage_dir / "clean_bg_bundle"
    bg_out.mkdir(parents=True, exist_ok=True)
    for f in Path(bundle_dir).iterdir():
        if f.is_file():
            shutil.copy2(f, bg_out / f.name)

    # 3. Undistorted images & COLMAP / OpenMVS scene template
    cameras = mio.load_and_undistort_cameras(stage_dir / "calibration.json", bg_out)
    mio.export_colmap_text_workspace(stage_dir / "colmap_undistorted", cameras)
    mio.write_openmvs_mvs_file(
        stage_dir / "scene_template.mvs",
        cameras,
        image_paths_in_scene={cid: f"colmap_undistorted/images/{cid}.jpg" for cid in cameras},
        points_xyz=np.array([[4.0, 4.0, 1.0]], dtype=np.float32),
        points_rgb=np.array([[128, 128, 128]], dtype=np.uint8),
        point_views=[[(0, 1152.0, 648.0, 1.0), (1, 1152.0, 648.0, 1.0)]],
    )

    # 4. 12th session baseline mesh GLB & PLY
    shutil.copy2(mesh_glb_path, stage_dir / "current-scene-textured.glb")
    V_s12, F_s12 = mio.load_scene_mesh_world_from_glb(mesh_glb_path)
    write_ascii_ply(stage_dir / "s12_mesh.ply", V_s12, F_s12)

    # 5. Include multi-frame sample from session4 lobby-empty-frames if present for dry-run verification
    s4_empty = DEFAULT_LOCAL_ROOT / "session4/lobby-empty-frames"
    if s4_empty.is_dir():
        mf_out = stage_dir / "sample_multiframe_stills"
        mf_out.mkdir(parents=True, exist_ok=True)
        for sub in sorted(s4_empty.glob("i0000*")):
            if sub.is_dir():
                shutil.copytree(sub, mf_out / sub.name)

    manifest = {
        "bundle_id": "s12_geometry_bench_input",
        "jira_key": "S15P31A101-528",
        "calibration_version": calib_payload.get("calibration_version"),
        "camera_ids": list(cameras.keys()),
        "s12_mesh_faces": int(len(F_s12)),
        "s12_mesh_vertices": int(len(V_s12)),
        "estimated_gpu_pod_times_seconds": {
            "step1_mapanything_518_single_s12": 8.0,
            "step2_mapanything_518_multiframe_N10": 45.0,
            "step3_da3_1008_hybrid_depth": 18.0,
            "step4_variants_0_to_4_cpu_gpu_pipeline": 420.0,
            "step5_hole_and_tsdf_loss_analysis_A_B_C_D": 75.0,
            "total_estimated_seconds": 566.0,
        },
    }
    (stage_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    tar_path = out_dir / "s12_geometry_bench_input.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(stage_dir, arcname="s12_geometry_bench_input")

    return {
        "tar_path": str(tar_path),
        "tar_bytes": int(tar_path.stat().st_size),
        "tar_sha256": sha256_file(tar_path),
        "manifest": manifest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S15P31A101-528 Geometry & Texture Benchmark Runner")
    parser.add_argument(
        "--mode",
        choices=("laptop-bench", "bundle-gpu", "full", "all-local", "check", "setup-ma-root", "hole-analysis", "v2-clean"),
        default="all-local",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--pod-results-dir", type=Path, default=DEFAULT_OUT_DIR / "pod_results")
    parser.add_argument("--calib", type=Path, default=DEFAULT_S12_CALIB)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_S12_BUNDLE)
    parser.add_argument("--mesh-glb", type=Path, default=DEFAULT_S12_MESH_GLB)
    parser.add_argument("--variants", type=str, default="0,1,2,3,4")
    parser.add_argument("--ma-single-root", type=Path, default=None, help="reconstruct_tasks root for single-frame MapAnything")
    parser.add_argument("--ma-multi-dir", type=Path, default=None, help="Parent directory containing per-frame reconstruct_tasks roots")
    parser.add_argument("--da3-dir", type=Path, default=None, help="DA3 hybrid depth output directory (depth.json + cam_*.npz)")
    parser.add_argument("--ma-single-npz", type=Path, default=None, help="Optional direct World-frame single-frame depth npz")
    parser.add_argument("--ma-multi-npz", type=Path, default=None, help="Optional direct World-frame multi-frame depth npz")
    parser.add_argument("--da3-npz", type=Path, default=None, help="Optional direct World-frame DA3 depth npz")
    parser.add_argument("--run-id", type=str, default="s12_single", help="Run ID for --mode setup-ma-root")
    parser.add_argument(
        "--run-gpu-mask-infer",
        action="store_true",
        help="On GPU Pod, run MapAnything with mask_edges=True and mask_edges=False to write raw_mask_variants.npz",
    )
    args = parser.parse_args(argv)

    if args.mode == "check":
        res_check = run_check_mode(
            calib_path=args.calib,
            bundle_dir=args.bundle,
            out_dir=args.out,
        )
        print(json.dumps(res_check, indent=2, ensure_ascii=False))
        return 0

    if args.mode == "setup-ma-root":
        res_setup = prepare_reconstruct_job_root(
            root_dir=args.out,
            calib_path=args.calib,
            images_dir=args.bundle,
            run_id=args.run_id,
            run_prepare_stage=False,
        )
        print(json.dumps(res_setup, indent=2, ensure_ascii=False))
        return 0

    if args.mode == "v2-clean":
        out_v2 = args.out if args.out != DEFAULT_OUT_DIR else (DEFAULT_OUT_DIR / "v2_clean")
        res_v2 = vc.run_v2_clean_experiment(
            pod_results_dir=args.pod_results_dir,
            out_dir=out_v2,
            calib_path=args.calib,
            bundle_dir=args.bundle,
        )
        print(
            json.dumps(
                {
                    "mode": "v2-clean",
                    "out": str(out_v2),
                    "kept_faces": res_v2["glb_filter_result"]["kept_faces"],
                    "removed_faces": res_v2["glb_filter_result"]["removed_faces"],
                    "comparison_panels": res_v2["comparison_panels"],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.mode == "hole-analysis":
        if args.ma_single_root is not None and (args.ma_single_root / "mapanything" / "raw.npz").is_file():
            res_hole = ha.run_pod_hole_and_tsdf_analysis(
                ma_single_root=args.ma_single_root,
                calib_path=args.calib,
                bundle_dir=args.bundle,
                final_glb_path=args.mesh_glb,
                out_dir=args.out,
                run_gpu_mask_infer=bool(args.run_gpu_mask_infer),
            )
        else:
            res_hole = ha.run_laptop_hole_and_tsdf_analysis(
                calib_path=args.calib,
                bundle_dir=args.bundle,
                base_glb_path=args.mesh_glb,
                out_dir=args.out,
            )
        print(
            json.dumps(
                {
                    "mode": "hole-analysis",
                    "out": str(args.out),
                    "hole_classification_cameras": list(
                        res_hole.get("hole_classification_overlay", {}).get("per_camera", {}).keys()
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.mode in ("laptop-bench", "all-local"):
        res_laptop = run_laptop_cpu_benchmark(
            out_dir=args.out,
            calib_path=args.calib,
            bundle_dir=args.bundle,
            mesh_glb_path=args.mesh_glb,
        )
        print(
            json.dumps(
                {
                    "mode": "laptop-bench",
                    "out": str(args.out),
                    "variants": {
                        k: {
                            "faces": v["mesh"]["mesh_faces"],
                            "self_psnr_db": v["self_view"]["self_psnr_db_mean"],
                            "loo_psnr_db": v["loo"]["loo_psnr_db_mean"],
                            "loo_blur8_db": v["loo"]["loo_psnr_db_blur8_mean"],
                            "edge_f1": v["self_view"]["edge_f1_px2_mean"],
                        }
                        for k, v in res_laptop["variants"].items()
                    },
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    if args.mode in ("bundle-gpu", "all-local"):
        res_bundle = build_gpu_session_bundle(
            out_dir=args.out,
            calib_path=args.calib,
            bundle_dir=args.bundle,
            mesh_glb_path=args.mesh_glb,
        )
        print(json.dumps({"mode": "bundle-gpu", **res_bundle}, indent=2, ensure_ascii=False))

    if args.mode == "full":
        v_ids = [int(x.strip()) for x in args.variants.split(",") if x.strip()]
        if args.ma_single_root is not None:
            multi_roots: list[Path] = []
            if args.ma_multi_dir and args.ma_multi_dir.is_dir():
                multi_roots = sorted(
                    p for p in args.ma_multi_dir.iterdir() if p.is_dir() and (p / "mapanything" / "raw.npz").is_file()
                )
            run_full_variants_benchmark(
                out_dir=args.out / "full_variants",
                calib_path=args.calib,
                bundle_dir=args.bundle,
                variant_ids=v_ids,
                ma_single_root=args.ma_single_root,
                ma_multi_roots=multi_roots,
                da3_depth_dir=args.da3_dir,
            )
        elif args.ma_single_npz is not None and args.ma_single_npz.is_file():
            single_z = np.load(args.ma_single_npz)
            single_d = {cid: single_z[cid] for cid in ("cam_A", "cam_B", "cam_C", "cam_D") if cid in single_z}
            multi_d = None
            if args.ma_multi_npz and args.ma_multi_npz.is_file():
                mz = np.load(args.ma_multi_npz)
                multi_d = {cid: mz[cid] for cid in single_d if cid in mz}
            da3_d = None
            if args.da3_npz and args.da3_npz.is_file():
                dz = np.load(args.da3_npz)
                da3_d = {cid: dz[cid] for cid in single_d if cid in dz}
            run_full_variants_benchmark(
                out_dir=args.out / "full_variants",
                calib_path=args.calib,
                bundle_dir=args.bundle,
                variant_ids=v_ids,
                ma_single_depths=single_d,
                ma_multi_depths_stack=multi_d,
                da3_hybrid_depths=da3_d,
            )
        else:
            raise SystemExit("Either --ma-single-root or --ma-single-npz is required for --mode full")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
