"""노트북 2cm 복셀·30만 면 메시 굽기 및 -528 v2_clean 정리·8시점 비교 래퍼 (S15P31A101-537).

운영 코드(`ai/src/simai_ai/`) 및 `-528` 벤치마크 코드(`ai/experiments/geometry_bench/`)를 수정하지 않고,
래퍼에서 `run_bench.VARIANTS_SPEC[2]` 를 `voxel_m=0.02`, `target_faces=300000` 으로 덮어써서
WorldMirror 2.0 (`wm2_pose`, `wm2_pose_depthprior`) 및 Pi3X (`pi3x_pose`, `pi3x_pose_depthprior`)
4개 후보를 동일한 v2 경로(카메라별 sim3 정렬 + plane cleanup → TSDF → texrecon → LOO) 및
`v2_clean.py` 4단계 정리 필터(`voxel_m=0.02`, `max_edge_m=0.16m`)로 평가·비교한다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

AI_ROOT = Path(__file__).resolve().parents[2]
if str(AI_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(AI_ROOT / "src"))
if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))

from simai_ai.scene import run_hybrid_depth as hd  # noqa: E402
from simai_ai.scene import texture_mesh as tm  # noqa: E402

from experiments.geometry_bench import metrics as bm  # noqa: E402
from experiments.geometry_bench import mvs_io as mio  # noqa: E402
from experiments.geometry_bench import run_bench as rb  # noqa: E402
from experiments.geometry_bench import v2_clean as vc  # noqa: E402

DEFAULT_LOCAL_ROOT = Path("C:/Users/SSAFY/simai-local")
DEFAULT_DEPTH_ROOT = DEFAULT_LOCAL_ROOT / "depth-537" / "depth_adapters_537"
DEFAULT_MA_SINGLE = DEFAULT_LOCAL_ROOT / "align-535" / "ma_single"
DEFAULT_CALIB = (
    DEFAULT_LOCAL_ROOT / "geometry-bench" / "s12_geometry_bench_input" / "calibration.json"
)
DEFAULT_BUNDLE = (
    DEFAULT_LOCAL_ROOT / "geometry-bench" / "s12_geometry_bench_input" / "clean_bg_bundle"
)
DEFAULT_DA3_CLEAN_DIR = DEFAULT_LOCAL_ROOT / "geometry-bench" / "v2_clean"
DEFAULT_OUT_ROOT = DEFAULT_LOCAL_ROOT / "depth-537"

CANDIDATE_SPECS: dict[str, dict[str, str]] = {
    "wm2_pose": {
        "clean_key": "wm2_clean",
        "raw_key": "wm2_raw",
        "title_raw": "WM2 (pose, 2.0cm, 300k raw)",
        "title_clean": "WM2 정리 (pose, 2.0cm)",
    },
    "pi3x_pose": {
        "clean_key": "pi3x_clean",
        "raw_key": "pi3x_raw",
        "title_raw": "Pi3X (pose, 2.0cm, 300k raw)",
        "title_clean": "Pi3X 정리 (pose, 2.0cm)",
    },
    "wm2_pose_depthprior": {
        "clean_key": "wm2_depthprior_clean",
        "raw_key": "wm2_depthprior_raw",
        "title_raw": "WM2+깊이조건 (pose+depth, 2.0cm, 300k raw)",
        "title_clean": "WM2+깊이조건 정리 (2.0cm)",
    },
    "pi3x_pose_depthprior": {
        "clean_key": "pi3x_depthprior_clean",
        "raw_key": "pi3x_depthprior_raw",
        "title_raw": "Pi3X+깊이조건 (pose+depth, 2.0cm, 300k raw)",
        "title_clean": "Pi3X+깊이조건 정리 (2.0cm)",
    },
}


class DockerCpTexreconRunner:
    """Windows Docker Desktop 의 `-v` 바인드 마운트 멈춤을 피하기 위해 `docker cp` + `docker exec` 로 `texrecon` 을 돌린다."""

    def __init__(self, image: str = mio.TEXRECON_DOCKER_IMAGE, num_threads: int = 22) -> None:
        self.image = image
        self.num_threads = int(num_threads)
        self.cid: str | None = None

    def __enter__(self) -> "DockerCpTexreconRunner":
        return self

    def _ensure_container(self) -> str:
        if self.cid is not None:
            return self.cid
        r = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "-u",
                "0",
                "--network",
                "none",
                "--entrypoint",
                "sleep",
                self.image,
                "infinity",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Failed to start texrecon container: {r.stderr or r.stdout}")
        self.cid = r.stdout.strip()
        return self.cid

    def __exit__(self, *_exc: Any) -> None:
        if self.cid:
            subprocess.run(["docker", "rm", "-f", self.cid], capture_output=True, text=True)
            self.cid = None

    def _docker(self, args: list[str], timeout: float = 1800.0) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"docker {' '.join(args[:3])} failed (rc={r.returncode}):\n{r.stdout[-1000:]}\n{r.stderr[-1000:]}"
            )
        return r

    def run_on_mesh(
        self,
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
        """`rb.run_texrecon_on_mesh` 와 동일한 입출력 계약을 유지하되 `docker cp` + `docker exec` 를 사용한다."""
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()

        mesh_ply = work_dir / "mesh.ply"
        if not mesh_ply.is_file():
            tm.write_ply(mesh_ply, V_world, F)

        mesh_pushed = False

        def _ensure_mesh_pushed() -> None:
            nonlocal mesh_pushed
            if not mesh_pushed:
                cid = self._ensure_container()
                self._docker(["exec", cid, "sh", "-c", "rm -rf /work/*"])
                self._docker(["cp", str(mesh_ply.resolve()), f"{cid}:/work/mesh.ply"])
                mesh_pushed = True

        cam_ids = list(cameras.keys())
        sub_timings: dict[str, dict[str, float]] = {}

        def _bake_subset(
            sub_name: str, keep_cids: list[str], seam_leveling: bool
        ) -> tuple[Path, float]:
            sub_dir = work_dir / sub_name
            out_glb = sub_dir / "textured.glb"
            obj_path = sub_dir / "out" / "tex.obj"
            if out_glb.is_file() and out_glb.stat().st_size > 1024:
                sub_timings[sub_name] = {"texrecon_s": 0.0, "glb_export_s": 0.0, "cached": 1.0}
                return out_glb, 0.0

            _ensure_mesh_pushed()
            sub_dir.mkdir(parents=True, exist_ok=True)
            out_local = sub_dir / "out"
            out_local.mkdir(parents=True, exist_ok=True)
            scene_local = sub_dir / "scene"
            mio.write_texrecon_scene(scene_local, cameras, keep_cids)

            self._docker(
                [
                    "exec",
                    self.cid,
                    "sh",
                    "-c",
                    f"rm -rf /work/{sub_name} && mkdir -p /work/{sub_name}/out",
                ]
            )
            self._docker(
                [
                    "cp",
                    str(scene_local.resolve()),
                    f"{self.cid}:/work/{sub_name}/scene",
                ]
            )

            extra_flags = ["--skip_hole_filling"]
            if not seam_leveling:
                extra_flags.extend(["--skip_global_seam_leveling", "--skip_local_seam_leveling"])
            if self.num_threads > 0:
                extra_flags.append(f"--num_threads={self.num_threads}")

            ts = time.perf_counter()
            self._docker(
                [
                    "exec",
                    self.cid,
                    "texrecon",
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
            t_tex = round(time.perf_counter() - ts, 3)

            self._docker(
                [
                    "cp",
                    f"{self.cid}:/work/{sub_name}/out/.",
                    str(out_local.resolve()),
                ]
            )
            t_exp0 = time.perf_counter()
            mio.parse_openmvs_obj_and_export_glb(
                obj_path, V_world, F, out_glb, empty_rgb=(128, 128, 128)
            )
            t_exp = round(time.perf_counter() - t_exp0, 3)
            shutil.rmtree(out_local, ignore_errors=True)
            shutil.rmtree(scene_local, ignore_errors=True)
            sub_timings[sub_name] = {"texrecon_s": t_tex, "glb_export_s": t_exp}
            return out_glb, t_tex

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
        timing_meta = work_dir / "texrecon_timings.json"
        if t_all_s == 0.0 and timing_meta.is_file():
            saved_t = json.loads(timing_meta.read_text(encoding="utf-8"))
            t_all_s = float(saved_t.get("seconds", 0.0))
            t_loo_s = float(saved_t.get("costs_seconds", 0.0))
            elapsed = float(saved_t.get("total_wall_seconds", elapsed))
            sub_timings = saved_t.get("sub_timings", sub_timings)
        else:
            timing_meta.write_text(
                json.dumps(
                    {
                        "seconds": round(t_all_s, 3),
                        "costs_seconds": round(t_loo_s, 3),
                        "total_wall_seconds": elapsed,
                        "sub_timings": sub_timings,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return {
            "glb_path": all_glb,
            "loo_glb_paths": loo_paths_out,
            "seconds": round(t_all_s, 3),
            "costs_seconds": round(t_loo_s, 3),
            "total_wall_seconds": elapsed,
            "sub_timings": sub_timings,
            "mesh_ply": mesh_ply,
        }


def build_and_clean_candidate(
    cand_name: str,
    depth_dir: Path,
    ma_single_root: Path,
    cameras: dict[str, mio.UndistortedCamera],
    out_root: Path,
    tex_runner: DockerCpTexreconRunner,
    *,
    voxel_m: float = 0.02,
    target_faces: int = 300000,
    cam_scale: float = 0.5,
) -> dict[str, Any]:
    """단일 깊이 후보를 -528 Variant 2 경로(복셀 2cm, 목표 면 30만)로 굽고 `v2_clean` 4단계 필터를 적용·평가한다."""
    spec_meta = CANDIDATE_SPECS[cand_name]
    raw_dir = out_root / "work" / f"{cand_name}_2cm_raw"
    clean_dir = out_root / "work" / f"{cand_name}_2cm_clean"
    raw_dir.mkdir(parents=True, exist_ok=True)
    clean_dir.mkdir(parents=True, exist_ok=True)

    # 1. run_bench.VARIANTS_SPEC[2] 를 래퍼에서 덮어쓰기 (운영/벤치 코드 파일 수정 없음)
    rb.VARIANTS_SPEC[2]["voxel_m"] = float(voxel_m)
    rb.VARIANTS_SPEC[2]["target_faces"] = int(target_faces)
    v2_spec = rb.VARIANTS_SPEC[2]

    # 2. 깊이 로드 및 MapAnything 기준 카메라별 scale 정합
    t_ld0 = time.perf_counter()
    prep = hd.load_prepared(ma_single_root / "prepared")
    cams_ma, cam_sha = hd.read_cameras(ma_single_root / "mapanything")
    M_geom, wa_rec = hd.load_world_alignment(ma_single_root / "world_alignment.json", cam_sha)
    if M_geom is None:
        raise RuntimeError(f"World alignment missing: {wa_rec}")
    dviews_cand, depth_rec = tm.load_depth_views(
        ma_single_root / "mapanything", prep, cams_ma, depth_dir, cam_sha, "scale"
    )
    load_depth_s = round(time.perf_counter() - t_ld0, 3)

    # 3. TSDF 메시 생성 (sim3 정렬 + plane_cleanup + TSDF 2cm + 30만 면 감축)
    raw_mesh_ply = raw_dir / "mesh.ply"
    raw_mesh_npz = raw_dir / "mesh_vf.npz"
    geom_meta_path = raw_dir / "geometry_meta.json"
    if raw_mesh_ply.is_file() and raw_mesh_npz.is_file() and geom_meta_path.is_file():
        with np.load(raw_mesh_npz) as zf:
            V_world = np.asarray(zf["V_world"], dtype=np.float64)
            F_world = np.asarray(zf["F_world"], dtype=np.int32)
        geom_info = json.loads(geom_meta_path.read_text(encoding="utf-8"))
        geometry_s = float(geom_info.get("geometry_s", 0.0))
        fuse_rec = geom_info.get("fuse_rec", {})
    else:
        t_geo0 = time.perf_counter()
        scale_m = tm.unit_scale(M_geom)
        voxel_ma = float(v2_spec["voxel_m"]) / max(scale_m, 1e-6)
        max_depth_ma = float(tm.MAX_DEPTH_M) / max(scale_m, 1e-6)
        cfg = tm.Config(
            voxel_m=voxel_ma,
            trunc_voxels=int(v2_spec["trunc_voxels"]),
            max_depth_m=max_depth_ma,
            min_cluster_faces=tm.MIN_CLUSTER_FACES,
            target_faces=int(v2_spec["target_faces"]),
            align_views=str(v2_spec["align_views"]),
            plane_cleanup=bool(v2_spec["plane_cleanup"]),
        )
        warnings_list: list[str] = []
        _dv_out, _poses_out, V_ma, F_out, _plane_faces, geom_rec = tm.geometry(
            dviews_cand, cams_ma, M_geom, cfg, warnings_list
        )
        V_world = np.asarray(hd.transform_points(M_geom, V_ma), dtype=np.float64)
        F_world = np.asarray(F_out, dtype=np.int32)
        geometry_s = round(time.perf_counter() - t_geo0, 3)
        tm.write_ply(raw_mesh_ply, V_world, F_world)
        np.savez(raw_mesh_npz, V_world=V_world, F_world=F_world)
        fuse_rec = geom_rec.get("fuse", {})
        geom_info = {
            "geometry_s": geometry_s,
            "load_depth_s": load_depth_s,
            "voxel_m_world": float(v2_spec["voxel_m"]),
            "target_faces": int(v2_spec["target_faces"]),
            "fuse_rec": fuse_rec,
            "warnings": warnings_list,
        }
        geom_meta_path.write_text(
            json.dumps(geom_info, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # 4. texrecon (4-cam all + 4 LOO) 실행
    tex_res = tex_runner.run_on_mesh(raw_dir, V_world, F_world, cameras, run_loo=True)
    raw_all_glb = Path(tex_res["glb_path"])
    raw_loo_glbs = {cid: Path(p) for cid, p in tex_res["loo_glb_paths"].items()}

    # 5. v2_clean.py 4단계 정리 필터 적용 (복셀 2cm 비례: max_edge_m = 8 * 0.02 = 0.16m)
    clean_all_glb = clean_dir / "all" / "textured.glb"
    clean_loo_glbs: dict[str, Path] = {
        cid: clean_dir / f"loo-{cid}" / "textured.glb" for cid in raw_loo_glbs
    }
    eval_cache_path = clean_dir / "eval_cache.json"

    if (
        eval_cache_path.is_file()
        and clean_all_glb.is_file()
        and clean_all_glb.stat().st_size > 1024
        and all(p.is_file() and p.stat().st_size > 1024 for p in clean_loo_glbs.values())
    ):
        cached = json.loads(eval_cache_path.read_text(encoding="utf-8"))
        raw_rend = bm.render_glb_views(raw_all_glb, cameras, cam_scale=cam_scale)
        _, raw_loo_rgb = bm.evaluate_loo_glbs(raw_loo_glbs, cameras, cam_scale=cam_scale)
        clean_rend = bm.render_glb_views(clean_all_glb, cameras, cam_scale=cam_scale)
        _, clean_loo_rgb = bm.evaluate_loo_glbs(clean_loo_glbs, cameras, cam_scale=cam_scale)
        return {
            "candidate": cand_name,
            "raw_key": spec_meta["raw_key"],
            "clean_key": spec_meta["clean_key"],
            "raw_eval": cached["raw_eval"],
            "clean_eval": cached["clean_eval"],
            "raw_renders": raw_rend,
            "raw_loo_renders": raw_loo_rgb,
            "clean_renders": clean_rend,
            "clean_loo_renders": clean_loo_rgb,
            "depth_registration": depth_rec,
        }

    t_cln0 = time.perf_counter()
    parsed_raw = bm.parse_glb_primitives_world(raw_all_glb)
    stage_res = vc.compute_v2_cleanup_stage_masks(
        parsed_raw,
        cameras,
        voxel_m=float(v2_spec["voxel_m"]),
        max_edge_voxel_mult=8.0,
    )
    waterfall = vc.evaluate_stage_waterfall(
        parsed_raw, stage_res["masks"], cameras, cam_scale=0.25
    )
    keep_final = stage_res["masks"]["d_small_components"]
    glb_filter_info = vc.filter_glb_triangles_in_place(raw_all_glb, clean_all_glb, keep_final)
    ply_filter_info = vc.filter_ply_triangles(raw_mesh_ply, clean_dir / "mesh.ply", keep_final)

    # LOO GLB 에도 동일한 4단계 정리 필터 적용
    for cid, l_glb in raw_loo_glbs.items():
        l_parsed = bm.parse_glb_primitives_world(l_glb)
        l_stage = vc.compute_v2_cleanup_stage_masks(
            l_parsed,
            cameras,
            voxel_m=float(v2_spec["voxel_m"]),
            max_edge_voxel_mult=8.0,
        )
        out_l_glb = clean_loo_glbs[cid]
        vc.filter_glb_triangles_in_place(l_glb, out_l_glb, l_stage["masks"]["d_small_components"])
    clean_filter_s = round(time.perf_counter() - t_cln0, 3)

    # 6. Raw 및 Clean 시점 렌더 & 정량 지표 평가
    t_ev0 = time.perf_counter()
    raw_rend = bm.render_glb_views(raw_all_glb, cameras, cam_scale=cam_scale)
    raw_self_eval = bm.evaluate_camera_renders(raw_rend["cam_renders"], cameras)
    raw_loo_eval, raw_loo_rgb = bm.evaluate_loo_glbs(raw_loo_glbs, cameras, cam_scale=cam_scale)

    clean_rend = bm.render_glb_views(clean_all_glb, cameras, cam_scale=cam_scale)
    clean_self_eval = bm.evaluate_camera_renders(clean_rend["cam_renders"], cameras)
    clean_loo_eval, clean_loo_rgb = bm.evaluate_loo_glbs(
        clean_loo_glbs, cameras, cam_scale=cam_scale
    )
    eval_s = round(time.perf_counter() - t_ev0, 3)

    total_s = round(
        load_depth_s
        + geometry_s
        + float(tex_res["total_wall_seconds"])
        + clean_filter_s
        + eval_s,
        3,
    )
    stage_seconds = {
        "load_register_depth_s": load_depth_s,
        "geometry_total_s": geometry_s,
        "conflict_s": float(fuse_rec.get("conflict_s", 0.0)),
        "tsdf_s": float(fuse_rec.get("tsdf_s", 0.0)),
        "mesh_clean_s": float(fuse_rec.get("clean_s", 0.0)),
        "decimate_s": float(fuse_rec.get("decimate_s", 0.0)),
        "texrecon_all_s": float(tex_res["seconds"]),
        "texrecon_loo_s": float(tex_res["costs_seconds"]),
        "texrecon_wall_s": float(tex_res["total_wall_seconds"]),
        "v2_clean_filter_s": clean_filter_s,
        "eval_render_s": eval_s,
        "candidate_total_s": total_s,
    }

    raw_eval_doc = {
        "title": spec_meta["title_raw"],
        "voxel_m": float(v2_spec["voxel_m"]),
        "target_faces": int(v2_spec["target_faces"]),
        "glb_path": str(raw_all_glb),
        "glb_sha256": rb.sha256_file(raw_all_glb),
        "mesh": raw_rend["parsed_summary"],
        "self_view": raw_self_eval,
        "loo": raw_loo_eval,
        "stage_seconds": stage_seconds,
    }
    clean_eval_doc = {
        "title": spec_meta["title_clean"],
        "voxel_m": float(v2_spec["voxel_m"]),
        "target_faces": int(v2_spec["target_faces"]),
        "glb_path": str(clean_all_glb),
        "glb_sha256": rb.sha256_file(clean_all_glb),
        "mesh": clean_rend["parsed_summary"],
        "self_view": clean_self_eval,
        "loo": clean_loo_eval,
        "stage_seconds": stage_seconds,
        "filter_parameters": stage_res["parameters"],
        "glb_filter_result": glb_filter_info,
        "ply_filter_result": ply_filter_info,
        "stage_waterfall": waterfall,
    }
    eval_cache_path.write_text(
        json.dumps({"raw_eval": raw_eval_doc, "clean_eval": clean_eval_doc}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "candidate": cand_name,
        "raw_key": spec_meta["raw_key"],
        "clean_key": spec_meta["clean_key"],
        "raw_eval": raw_eval_doc,
        "clean_eval": clean_eval_doc,
        "raw_renders": raw_rend,
        "raw_loo_renders": raw_loo_rgb,
        "clean_renders": clean_rend,
        "clean_loo_renders": clean_loo_rgb,
        "depth_registration": depth_rec,
    }


def load_da3_clean_reference(
    da3_clean_dir: Path,
    cameras: dict[str, mio.UndistortedCamera],
    *,
    cam_scale: float = 0.5,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, np.ndarray]]:
    """`-528` 의 DA3 정리 결과(`C:/Users/SSAFY/simai-local/geometry-bench/v2_clean/`, 1.5cm, 550k)를 로드한다."""
    da3_glb = Path(da3_clean_dir) / "v2_da3_clean" / "all" / "textured.glb"
    summary_json = Path(da3_clean_dir) / "v2_clean_summary.json"
    if not da3_glb.is_file():
        raise FileNotFoundError(f"DA3 clean GLB not found: {da3_glb}")

    rend = bm.render_glb_views(da3_glb, cameras, cam_scale=cam_scale)
    self_eval = bm.evaluate_camera_renders(rend["cam_renders"], cameras)

    loo_eval: dict[str, Any] = {}
    if summary_json.is_file():
        s_doc = json.loads(summary_json.read_text(encoding="utf-8"))
        loo_eval = s_doc.get("variants", {}).get("v2_da3_clean", {}).get("loo", {})

    # v2_clean/compare/compare_loo_cam_{A..D}.jpg 의 마지막 타일(v2_da3_clean)에서 LOO 렌더 추출
    loo_rgb: dict[str, np.ndarray] = {}
    cmp_dir = Path(da3_clean_dir) / "compare"
    for cid in cameras:
        p_loo = cmp_dir / f"compare_loo_{cid}.jpg"
        if p_loo.is_file():
            bgr = cv2.imread(str(p_loo), cv2.IMREAD_COLOR)
            if bgr is not None and bgr.shape[1] >= 3200:
                rgb_tile = cv2.cvtColor(bgr[:, 2560:3200], cv2.COLOR_BGR2RGB)
                rh, rw = rend["cam_renders"][cid]["rgb"].shape[:2]
                loo_rgb[cid] = cv2.resize(rgb_tile, (rw, rh), interpolation=cv2.INTER_AREA)

    eval_entry = {
        "title": "DA3 정리 (-528, 1.5cm, 550k*)",
        "voxel_m": 0.015,
        "target_faces": 550000,
        "condition_note": "1.5cm 복셀 / 목표 면 55만 (-528 Pod 결과; 537 후보는 2.0cm / 30만)",
        "glb_path": str(da3_glb),
        "glb_sha256": rb.sha256_file(da3_glb),
        "mesh": rend["parsed_summary"],
        "self_view": self_eval,
        "loo": loo_eval,
        "stage_seconds": {
            "note": "-528 사전 생성 결과 로드",
        },
    }
    return eval_entry, rend, loo_rgb


def save_individual_renders(
    renders_root: Path,
    vkey: str,
    rend: dict[str, Any],
    loo_rgb: dict[str, np.ndarray],
) -> None:
    """변형별 8시점(`self_cam_A..D`, `free_top`, `free_obl1..3`) 및 `loo_cam_A..D` 개별 이미지를 저장한다."""
    vdir = renders_root / vkey
    vdir.mkdir(parents=True, exist_ok=True)
    for cid, cr in rend["cam_renders"].items():
        mio._write_rgb_image(vdir / f"self_{cid}.jpg", cr["rgb"])
    for cid, lrgb in loo_rgb.items():
        mio._write_rgb_image(vdir / f"loo_{cid}.jpg", lrgb)
    for vname, frgb in rend["free_renders"].items():
        mio._write_rgb_image(vdir / f"free_{vname}.jpg", frgb)


def format_comparison_markdown(summary: dict[str, Any]) -> str:
    """`[DA3 정리(1.5cm) | WM2 정리 | WM2+깊이조건 정리 | Pi3X 정리 | Pi3X+깊이조건 정리]` 마크다운 보고서를 생성한다."""
    lines = [
        "# S15P31A101-537: 다시점·자세 조건 깊이 어댑터 (`WorldMirror 2.0` vs `Pi3X` vs `DA3`) 메시·정리 비교",
        "",
        "> **조건 차이 명시**: `da3_clean` 은 `-528` 산출물(**복셀 1.5 cm, 목표 면 55만**)이며, "
        "`wm2_clean` · `wm2_depthprior_clean` · `pi3x_clean` · `pi3x_depthprior_clean` 4종은 노트북 CPU 실행 조건(**복셀 2.0 cm, 목표 면 30만**, `v2_clean` 긴 변 임계 `8×voxel = 0.16 m`)이다.",
        "",
        "## 1. 정리 후 5종 정량 지표 비교 (`[DA3 정리 | WM2 정리 | WM2+깊이조건 정리 | Pi3X 정리 | Pi3X+깊이조건 정리]`)",
        "",
        "| 변형 | 조건 (복셀 / 목표 면) | 남은 면 수 | 관측 / 미관측 면 | 면적 (m²) | GLB (MB) | Self PSNR (dB) | Self Blur8 (dB) | Self SSIM | Self 관측 덮음 | Self 전체 덮음 | EdgeGradCos | Edge F1 (≤2px) | CrossView AbsRel p50 | LOO PSNR (dB) | LOO Blur8 (dB) | LOO SSIM | LOO 관측 덮음 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for vkey, ve in summary["clean_variants"].items():
        m = ve["mesh"]
        sv = ve["self_view"]
        lv = ve.get("loo", {})
        cvd = sv.get("cross_view_depth", {})
        mb = m["glb_bytes"] / (1024.0 * 1024.0)
        cond_str = f"{ve['voxel_m']*100:.1f} cm / {ve['target_faces']//1000}k"
        if vkey == "da3_clean":
            cond_str += " (-528)"
        lines.append(
            f"| `{vkey}` | {cond_str} | {m['mesh_faces']:,} | {m['observed_faces']:,} / {m['unobserved_faces']:,} | "
            f"{m['mesh_area_m2']:.2f} | {mb:.2f} | {sv['self_psnr_db_mean']:.2f} | "
            f"{sv['self_psnr_db_blur8_mean']:.2f} | {sv['self_ssim_mean']:.4f} | "
            f"{sv['coverage_observed_mean']*100:.2f}% | {sv['coverage_total_mean']*100:.2f}% | "
            f"{sv['edge_grad_cos_sim_mean']:.4f} | {sv['edge_f1_px2_mean']:.4f} | "
            f"{cvd.get('abs_rel_p50', 0.0):.5f} | {lv.get('loo_psnr_db_mean', 0.0):.2f} | "
            f"{lv.get('loo_psnr_db_blur8_mean', 0.0):.2f} | {lv.get('loo_ssim_mean', 0.0):.4f} | "
            f"{lv.get('loo_coverage_mean', 0.0)*100:.2f}% |"
        )

    lines.extend(
        [
            "",
            "## 2. 카메라별 전체 / 관측 화면 덮음 (`cam_A..D`, 정리 후)",
            "",
            "| 변형 | 조건 | `cam_A` (전체/관측) | `cam_B` (전체/관측) | `cam_C` (전체/관측) | `cam_D` (전체/관측) | 자유시점 미관측 판 비율 (`obl3` / `obl2`) |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for vkey, ve in summary["clean_variants"].items():
        pc = ve["self_view"]["per_camera"]
        cond_str = f"{ve['voxel_m']*100:.1f}cm/{ve['target_faces']//1000}k"
        cells = []
        for cid in ("cam_A", "cam_B", "cam_C", "cam_D"):
            c_m = pc.get(cid, {})
            cells.append(
                f"{c_m.get('coverage_total', 0)*100:.1f}% / {c_m.get('coverage_observed', 0)*100:.1f}%"
            )
        wf = ve.get("stage_waterfall", [])
        fu = wf[-1].get("free_view_unobserved_screen_ratio", {}) if wf else {}
        fu_str = (
            f"{fu.get('obl3', 0)*100:.1f}% / {fu.get('obl2', 0)*100:.1f}%"
            if fu
            else "2.9% / 4.9% (-528)"
        )
        lines.append(f"| `{vkey}` | {cond_str} | " + " | ".join(cells) + f" | {fu_str} |")

    lines.extend(
        [
            "",
            "## 3. 정리 전(Raw 2cm, 300k) vs 정리 후(Clean 2cm) 비교 (4개 신규 후보)",
            "",
            "| 후보 | Raw 면 수 → Clean 면 수 (제거율) | Raw Self PSNR → Clean | Raw Self 관측 덮음 → Clean | Raw LOO PSNR → Clean | Raw LOO 덮음 → Clean |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for cand_name, c_meta in CANDIDATE_SPECS.items():
        rk = c_meta["raw_key"]
        ck = c_meta["clean_key"]
        if rk not in summary["raw_variants"] or ck not in summary["clean_variants"]:
            continue
        rv = summary["raw_variants"][rk]
        cv = summary["clean_variants"][ck]
        rf = rv["mesh"]["mesh_faces"]
        cf = cv["mesh"]["mesh_faces"]
        rem_pct = (rf - cf) / max(1, rf) * 100.0
        lines.append(
            f"| `{cand_name}` | {rf:,} → {cf:,} (-{rem_pct:.1f}%) | "
            f"{rv['self_view']['self_psnr_db_mean']:.2f} → {cv['self_view']['self_psnr_db_mean']:.2f} dB | "
            f"{rv['self_view']['coverage_observed_mean']*100:.2f}% → {cv['self_view']['coverage_observed_mean']*100:.2f}% | "
            f"{rv['loo']['loo_psnr_db_mean']:.2f} → {cv['loo']['loo_psnr_db_mean']:.2f} dB | "
            f"{rv['loo']['loo_coverage_mean']*100:.2f}% → {cv['loo']['loo_coverage_mean']*100:.2f}% |"
        )

    lines.extend(
        [
            "",
            "## 4. 단계별 실행 시간 (Pod GPU 추론 + 노트북 CPU 메시·텍스처·정리·평가)",
            "",
            "| 후보 | Pod GPU 추론 (s) | Pod Peak VRAM (MB) | 깊이 정합 (s) | 형상/TSDF 합계 (s) (`conflict`/`tsdf`/`decimate`) | `texrecon` 4카메라 (s) | `texrecon` LOO 4회 (s) | `v2_clean` 필터 (s) | 렌더·지표 평가 (s) | 노트북 합계 (s) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    pod_stats = summary.get("pod_gpu_runs", {})
    for cand_name, c_meta in CANDIDATE_SPECS.items():
        ck = c_meta["clean_key"]
        if ck not in summary["clean_variants"]:
            continue
        sec = summary["clean_variants"][ck]["stage_seconds"]
        ps = pod_stats.get(cand_name, {})
        pod_s = f"{ps.get('total_ms', 0.0)/1000.0:.1f}" if ps.get("total_ms") else "n/a"
        pod_mb = f"{ps.get('peak_alloc_mb', 0.0):.0f}" if ps.get("peak_alloc_mb") else "n/a"
        lines.append(
            f"| `{cand_name}` | {pod_s} | {pod_mb} | {sec['load_register_depth_s']:.2f} | "
            f"{sec['geometry_total_s']:.1f} ({sec['conflict_s']:.1f}/{sec['tsdf_s']:.1f}/{sec['decimate_s']:.1f}) | "
            f"{sec['texrecon_all_s']:.1f} | {sec['texrecon_loo_s']:.1f} | "
            f"{sec['v2_clean_filter_s']:.1f} | {sec['eval_render_s']:.1f} | "
            f"**{sec['candidate_total_s']:.1f}** |"
        )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="S15P31A101-537 Laptop 2cm Mesh & v2_clean Comparison Runner"
    )
    parser.add_argument("--depth-root", type=Path, default=DEFAULT_DEPTH_ROOT)
    parser.add_argument("--ma-single", type=Path, default=DEFAULT_MA_SINGLE)
    parser.add_argument("--calib", type=Path, default=DEFAULT_CALIB)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--da3-clean-dir", type=Path, default=DEFAULT_DA3_CLEAN_DIR)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument(
        "--candidates",
        type=str,
        default="wm2_pose,pi3x_pose,wm2_pose_depthprior,pi3x_pose_depthprior",
        help="Comma-separated candidate list (pose two first by default)",
    )
    parser.add_argument("--voxel-m", type=float, default=0.02)
    parser.add_argument("--target-faces", type=int, default=300000)
    parser.add_argument("--num-threads", type=int, default=22)
    args = parser.parse_args(argv)

    t_start = time.perf_counter()
    out_root = Path(args.out_root)
    compare_dir = out_root / "compare"
    renders_root = compare_dir / "renders"
    compare_dir.mkdir(parents=True, exist_ok=True)
    renders_root.mkdir(parents=True, exist_ok=True)

    cameras = mio.load_and_undistort_cameras(args.calib, args.bundle)

    # Pod GPU 실행 기록 로드
    pod_gpu_runs: dict[str, Any] = {}
    pod_jsonl = Path(args.depth_root) / "pod_runs.jsonl"
    if pod_jsonl.is_file():
        for line in pod_jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("rc") == 0:
                pod_gpu_runs[str(row["job"])] = row

    cand_list = [c.strip() for c in args.candidates.split(",") if c.strip()]
    cand_results: dict[str, dict[str, Any]] = {}

    with DockerCpTexreconRunner(num_threads=args.num_threads) as tex_runner:
        for cand_name in cand_list:
            depth_dir = Path(args.depth_root) / cand_name
            if not (depth_dir / "depth.json").is_file():
                print(f"[SKIP] {cand_name}: depth.json not found in {depth_dir}", flush=True)
                continue
            print(f"[START] {cand_name} (voxel={args.voxel_m}m, target_faces={args.target_faces}) ...", flush=True)
            res = build_and_clean_candidate(
                cand_name,
                depth_dir,
                args.ma_single,
                cameras,
                out_root,
                tex_runner,
                voxel_m=args.voxel_m,
                target_faces=args.target_faces,
            )
            cand_results[cand_name] = res
            sec = res["clean_eval"]["stage_seconds"]
            sv = res["clean_eval"]["self_view"]
            lv = res["clean_eval"]["loo"]
            print(
                f"[DONE] {cand_name} in {sec['candidate_total_s']:.1f}s "
                f"(geo={sec['geometry_total_s']:.1f}s, tex_all={sec['texrecon_all_s']:.1f}s, tex_loo={sec['texrecon_loo_s']:.1f}s) | "
                f"Clean faces={res['clean_eval']['mesh']['mesh_faces']:,}, "
                f"Self PSNR={sv['self_psnr_db_mean']:.2f}dB, CovObs={sv['coverage_observed_mean']*100:.1f}%, "
                f"LOO PSNR={lv['loo_psnr_db_mean']:.2f}dB, LOO Cov={lv['loo_coverage_mean']*100:.1f}%",
                flush=True,
            )

    # DA3 (-528 1.5cm) 정리 결과 로드 및 5종 비교 순서 정렬:
    # [da3_clean | wm2_clean | wm2_depthprior_clean | pi3x_clean | pi3x_depthprior_clean]
    da3_eval, da3_rend, da3_loo_rgb = load_da3_clean_reference(args.da3_clean_dir, cameras)
    save_individual_renders(renders_root, "da3_clean", da3_rend, da3_loo_rgb)

    ordered_clean_keys = [
        ("da3_clean", None),
        ("wm2_clean", "wm2_pose"),
        ("wm2_depthprior_clean", "wm2_pose_depthprior"),
        ("pi3x_clean", "pi3x_pose"),
        ("pi3x_depthprior_clean", "pi3x_pose_depthprior"),
    ]

    clean_evals: dict[str, dict[str, Any]] = {}
    clean_renders: dict[str, dict[str, Any]] = {}
    clean_loo_renders: dict[str, dict[str, np.ndarray]] = {}
    raw_evals: dict[str, dict[str, Any]] = {}

    for c_key, cand_name in ordered_clean_keys:
        if cand_name is None:
            clean_evals[c_key] = da3_eval
            clean_renders[c_key] = da3_rend
            clean_loo_renders[c_key] = da3_loo_rgb
        elif cand_name in cand_results:
            cr = cand_results[cand_name]
            clean_evals[c_key] = cr["clean_eval"]
            clean_renders[c_key] = cr["clean_renders"]
            clean_loo_renders[c_key] = cr["clean_loo_renders"]
            raw_evals[cr["raw_key"]] = cr["raw_eval"]
            save_individual_renders(
                renders_root, c_key, cr["clean_renders"], cr["clean_loo_renders"]
            )
            save_individual_renders(
                renders_root, cr["raw_key"], cr["raw_renders"], cr["raw_loo_renders"]
            )

    saved_panels = bm.save_variant_comparison_panels(
        compare_dir,
        cameras,
        clean_evals,
        clean_renders,
        variant_loo_renders=clean_loo_renders,
    )

    summary = {
        "ticket": "S15P31A101-537",
        "experiment": "depth_adapters_worldmirror2_pi3x_vs_da3_clean",
        "condition_note": (
            "da3_clean is from S15P31A101-528 (voxel 1.5 cm, target_faces 550,000); "
            "wm2_clean, wm2_depthprior_clean, pi3x_clean, pi3x_depthprior_clean are baked on laptop "
            "with voxel 2.0 cm, target_faces 300,000, and v2_clean max_edge_m = 8 * 0.02 = 0.16 m."
        ),
        "total_laptop_wall_s": round(time.perf_counter() - t_start, 2),
        "pod_gpu_runs": pod_gpu_runs,
        "clean_variants": clean_evals,
        "raw_variants": raw_evals,
        "comparison_panels": [str(p) for p in saved_panels],
    }

    (compare_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (compare_dir / "summary.md").write_text(
        format_comparison_markdown(summary), encoding="utf-8"
    )
    print(f"[ALL DONE] Summary written to {compare_dir / 'summary.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
