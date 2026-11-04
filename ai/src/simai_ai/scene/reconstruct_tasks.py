"""분리 프로세스에서 실행하는 복원 단계. CLI/실험과 동일한 scene 구현을 사용한다."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import numpy as np

from .reconstruct import ReconstructionError, read_json, sha256, write_json


def prepare(root: Path) -> None:
    import cv2
    from simai_ai.calibration.intrinsics.estimate import WeightsRef
    from simai_ai.calibration.world_frame.prepare import UltralyticsPersonSegmenter, undistort

    job, cal = read_json(root / "job.json"), read_json(root / "calibration.json")
    inp = job["input"]
    need_seg = any(im["kind"] == "FRAME" for im in inp["images"])
    seg = None
    if need_seg:
        weights = Path(os.environ.get("SIMAI_SEG_WEIGHTS", ""))
        if not weights.is_file():
            raise ReconstructionError("STATIC_SEGMENTER_MISSING", "FRAME 입력에는 SIMAI_SEG_WEIGHTS 가 필요하다")
        seg = UltralyticsPersonSegmenter(WeightsRef(str(weights), sha256(weights.read_bytes())))
    out = root / "prepared"
    out.mkdir()
    cameras = {c["camera_id"]: c for c in cal["cameras"]}
    views = []
    for im in inp["images"]:
        cid = im["camera_id"]
        image = cv2.imread(str(root / "images" / f"{cid}.image"))
        c = cameras[cid]
        if image is None or list(image.shape[1::-1]) != c["image_size"]:
            raise ReconstructionError("STATIC_INPUT_INVALID", f"{cid}: 이미지 형식·해상도 오류")
        K = np.asarray(c["K"], float)
        if c["distortion"]["model"] == "NONE":
            und, newK = image, K
        elif c["distortion"]["model"] == "OPENCV_PINHOLE":
            und, newK = undistort(image, K, np.asarray(c["distortion"]["coeffs"], float))
        else:
            raise ReconstructionError("STATIC_INPUT_INVALID", "prepare 는 PINHOLE·NONE 보정만 지원")
        mask_name = None
        if im["kind"] == "FRAME":
            mask, _ = seg.person_mask(und)
            mask = cv2.dilate(mask.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)))
            mask_name = f"{cid}.mask.png"
            if not cv2.imwrite(str(out / mask_name), mask * 255):
                raise OSError("마스크 쓰기 실패")
        if not cv2.imwrite(str(out / f"{cid}.jpg"), und, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise OSError("영상 쓰기 실패")
        views.append({"name": cid, "camera_id": cid, "image": f"{cid}.jpg", "mask": mask_name,
                      "image_size": c["image_size"], "K_undistorted": newK.tolist(),
                      "source_image": f"{cid}.image", "source_camera": {"K": c["K"], "distortion": c["distortion"]}})
    write_json(out / "prepared.json", {"format": "simai.mapanything_inputs", "format_version": "0.1.0",
        "views": views, "input_provenance": inp["input_provenance"], "person_masks": need_seg,
        "segmenter": seg.describe() if seg else None, "human_removal": "MASK_COMPOSITE"})


def mapanything(root: Path) -> None:
    from . import run_mapanything
    model = os.environ.get("SIMAI_MAPANYTHING_MODEL") or str(Path(os.environ.get("SIMAI_ROOT", "/workspace/simai")) / "weights" / "map-anything")
    if not Path(model).is_dir():
        raise ReconstructionError("STATIC_MODEL_MISSING", "MapAnything 가중치 폴더 없음")
    # 기존 prepared 모드와 같은 K 전용 추론. 모델 세계를 보정 세계로 정렬하는 것은 다음 단계다.
    sys.argv = ["mapanything", "--run-id", "mapanything", "--runs-dir", str(root), "--mode", "prepared",
                "--prepared", str(root / "prepared"), "--model", model, "--repeat", "1"]
    if run_mapanything.main():
        raise ReconstructionError("STATIC_MAPANYTHING_TOOL_FAILED", "MapAnything 추론 실패")


def align_to_calibration(root: Path) -> np.ndarray:
    """카메라 회전·중심을 이용한 similarity. 보정 문서는 읽기만 한다."""
    cal = read_json(root / "calibration.json")
    cams = {c["camera_id"]: c for c in cal["cameras"]}
    views = read_json(root / "mapanything" / "cameras.json")["views"]
    requested = {im["camera_id"] for im in read_json(root / "job.json")["input"]["images"]}
    if {v["name"] for v in views} != requested:
        raise ReconstructionError("STATIC_WORLD_ALIGNMENT_FAILED", "모델 출력과 입력 카메라 불일치")
    rotations, src, dst = [], [], []
    for v in views:
        c = cams[v["name"]]
        T = np.asarray(v["cam2world"], float)
        R = np.asarray(c["R"], float)
        if T.shape != (4, 4) or not np.isfinite(T).all() or not np.allclose(T[3], [0, 0, 0, 1]):
            raise ReconstructionError("STATIC_WORLD_ALIGNMENT_FAILED", "모델 자세 형식 오류")
        for rot in (R, T[:3, :3]):
            if not np.allclose(rot.T @ rot, np.eye(3), atol=1e-4) or not np.isclose(np.linalg.det(rot), 1, atol=1e-4):
                raise ReconstructionError("STATIC_WORLD_ALIGNMENT_FAILED", "회전 행렬 오류")
        rotations.append(R.T @ T[:3, :3].T)
        src.append(T[:3, 3])
        dst.append(-R.T @ np.asarray(c["t"], float))
    if len(src) < 2 or len(src) != len({v["name"] for v in views}):
        raise ReconstructionError("STATIC_WORLD_ALIGNMENT_FAILED", "독립 카메라 2대 필요")
    u, _, vt = np.linalg.svd(np.sum(rotations, axis=0))
    Q = u @ np.diag([1., 1., np.linalg.det(u @ vt)]) @ vt
    src, dst = np.asarray(src), np.asarray(dst)
    x, y = src - src.mean(0), dst - dst.mean(0)
    denom = float(np.sum(x * x))
    scale = float(np.sum((x @ Q.T) * y) / denom) if denom > 1e-8 else 0
    shift = dst.mean(0) - scale * Q @ src.mean(0)
    residual = np.linalg.norm(scale * src @ Q.T + shift - dst, axis=1)
    angles = [np.arccos(np.clip((np.trace(Q.T @ r) - 1) / 2, -1, 1)) for r in rotations]
    if scale <= 0 or not np.isfinite(scale) or residual.max() > .10 or max(angles) > np.deg2rad(5):
        raise ReconstructionError("STATIC_WORLD_ALIGNMENT_FAILED", "카메라 중심 0.10m·회전 5도 일치 조건 미달")
    M = np.eye(4)
    M[:3, :3], M[:3, 3] = scale * Q, shift
    write_json(root / "world_alignment.json", {"format": "simai.world_alignment", "status": "OK",
        "cameras_json_sha256": sha256((root / "mapanything" / "cameras.json").read_bytes()),
        "mapanything_to_world": M.tolist(), "calibration": cal,
        "max_center_residual_m": float(residual.max()), "scale": scale})
    return M


def enhance_visual_glb(root: Path, glb: Path, options: dict | None = None) -> dict:
    """결과 GLB 에 시각 보강(바닥 평면·미관측 색, S15P31A101-481)을 제자리 적용하고 report 를 돌려준다.

    실패·불채택이면 원본을 그대로 두고 이유만 남긴다(단계를 실패시키지 않는다). 시각용이며 위험 판정에 쓰지 않는다.
    """
    from . import glb_enhance as ge

    cfg = ge.EnhanceConfig.from_env(options=options)
    if not cfg.enabled:
        return {"applied": False, "reason": "DISABLED", "purpose": "VISUAL_ONLY", "safety_use": False}
    cameras = None
    try:
        if (root / "calibration.json").is_file() and (root / "prepared" / "prepared.json").is_file():
            cameras = ge.cameras_from_prepared(root) or None
    except Exception:  # noqa: BLE001 - 검사 입력 읽기 실패는 메시 지표 검사로 대체한다
        cameras = None
    try:
        return ge.enhance_file(glb, config=cfg, cameras=cameras)
    except Exception as exc:  # noqa: BLE001
        return {"applied": False, "reason": f"ENHANCE_ERROR:{type(exc).__name__}", "purpose": "VISUAL_ONLY", "safety_use": False}


def _enhance_summary(rep: dict) -> dict:
    """metrics 에 싣는 요약: 적용 여부·검사 점수 전후·채운 면적."""
    chk = rep.get("check") or {}
    fl = rep.get("floor") or {}
    rc = rep.get("recolor") or {}
    out = {"applied": bool(rep.get("applied")), "reason": rep.get("reason"), "purpose": "VISUAL_ONLY", "safety_use": False,
           "check_mode": chk.get("mode"), "check_accepted": chk.get("accepted"),
           "check_mae_before": chk.get("mae_mean_before"), "check_mae_after": chk.get("mae_mean_after"),
           "floor_filled_area_m2": fl.get("filled_area_m2") if fl.get("applied") else None,
           "floor_empty_ratio_before": fl.get("empty_ratio_before"), "floor_empty_ratio_after": fl.get("empty_ratio_after"),
           "neutral_recolored_area_m2": rc.get("recolored_area_m2") if rc.get("applied") else None,
           "neutral_gray_area_m2_before": rc.get("unobserved_area_m2_before"),
           "neutral_gray_area_m2_after": rc.get("unobserved_gray_area_m2_after"),
           "seconds": rep.get("seconds"), "detail": rep}
    return out


def bake(root: Path) -> None:
    if not (root / "world_alignment.json").is_file():
        align_to_calibration(root)
    spec = os.environ.get("SIMAI_TEXRECON", "").strip()
    texrecon = shutil.which(spec or "texrecon")
    if not texrecon:
        write_json(root / "bake.json", {"fallback_reason": "TEXRECON_NOT_FOUND", "texture": None})
        return
    try:
        import open3d  # noqa: F401 - 기존 TSDF 구현에 필요한 외부 실행 환경
    except ImportError:
        write_json(root / "bake.json", {"fallback_reason": "TSDF_OPEN3D_NOT_FOUND", "texture": None})
        return
    from . import texture_mesh as tm
    opts = read_json(root / "job.json")["input"].get("options", {})
    raw_threads = opts.get("num_threads")
    if raw_threads is None:
        env_threads = os.environ.get("SIMAI_BAKE_NUM_THREADS", "").strip()
        raw_threads = int(env_threads) if env_threads else 0
    num_threads = int(raw_threads)
    raw_faces = opts.get("target_faces")
    if raw_faces is None:
        env_faces = os.environ.get("SIMAI_BAKE_TARGET_FACES", "").strip()
        raw_faces = int(env_faces) if env_faces else tm.DEFAULT_BAKE_TARGET_FACES
    target_faces = int(raw_faces)
    args = ["--ma-run", str(root / "mapanything"), "--prepared", str(root / "prepared"),
            "--out", str(root / "textured"), "--world-alignment", str(root / "world_alignment.json"),
            "--texrecon", texrecon, "--terms", "area", "--loo-terms", "",
            "--num-threads", str(num_threads), "--target-faces", str(target_faces),
            "--align-views", opts.get("align_views", "off"), "--run-id", read_json(root / "job.json")["run_id"],
            "--jira-key", "S15P31A101-364"]
    if opts.get("plane_cleanup", False):
        args.append("--plane-cleanup")
    code = tm.main(args)
    if code:
        raise ReconstructionError("STATIC_BAKE_EMPTY" if code == tm.EXIT_EMPTY else "STATIC_BAKE_TOOL_FAILED", f"texture_mesh 종료 {code}")
    rec = read_json(root / "textured" / "metrics.json")
    b = rec["bakes"]["area"]
    glb = root / "textured" / b["glb"]
    from .texture_provenance import SURFACE_VALUES
    ratios = b["ratios_area"]
    ratios = {k: v for k, v in ratios.items() if k in SURFACE_VALUES}
    maps = sorted(glb.parent.glob("surface_source_*.png"))
    if not maps:
        raise ReconstructionError("STATIC_BAKE_EMPTY", "출처 지도 없음")
    geo = rec.get("geometry") or {}
    tri_count = b.get("glb_triangles") if "glb_triangles" in b else geo.get("faces")
    enhance = _enhance_summary(enhance_visual_glb(root, glb, opts))
    if enhance["applied"] and tri_count is not None and (enhance["detail"].get("floor") or {}).get("applied"):
        tri_count = int(tri_count) + 2
    gltf_asset: dict[str, object] = {"kind": "GLTF_VISUAL", "path": glb.relative_to(root).as_posix(), "content_type": "model/gltf-binary"}
    if tri_count is not None:
        gltf_asset["triangle_count"] = int(tri_count)
    assets = [gltf_asset]
    assets += [{"kind": "SURFACE_SOURCE_MAP", "path": p.relative_to(root).as_posix(), "content_type": "image/png", "ratios": ratios} for p in maps]
    post = ["OUTLIER_REMOVAL", "TSDF_FUSION"]
    if opts.get("align_views", "off") != "off":
        post.insert(0, "CROSS_VIEW_ALIGNMENT")
    if opts.get("plane_cleanup"):
        post.append("PLANE_CLEANUP")
    if geo.get("decimated", True):
        post.append("DECIMATION")
    write_json(root / "bake.json", {"texture": "texrecon", "fallback_reason": None, "assets": assets,
        "observed_surface_ratio": ratios["OBSERVED"], "postprocess": [*post, "TEXTURE", "WORLD_ALIGNMENT"], "metrics": rec, "enhance": enhance,
        "target_faces": target_faces, "num_threads": tm.resolve_num_threads(num_threads),
        "stage_times": rec.get("times"),
        "auxiliary": [p.relative_to(root).as_posix() for p in sorted(glb.parent.glob("observed_camera_*.png"))]})


def _load_colored_fuse_views(root: Path) -> list:
    import cv2
    from simai_ai.calibration.world_frame.inputs import load_mapanything_run
    from . import run_hybrid_depth as hd

    prepared = hd.load_prepared(root / "prepared")
    run = load_mapanything_run(root / "mapanything", prepared=prepared)
    source = {v["name"]: v for v in prepared["views"]}
    views = []
    for v in run.views:
        p = source[v.name]
        image = cv2.cvtColor(cv2.imread(str(root / "prepared" / p["image"])), cv2.COLOR_BGR2RGB)
        h, w = v.depth.shape
        yy, xx = np.mgrid[:h, :w]
        rays = np.linalg.inv(v.K) @ np.stack([xx.ravel(), yy.ravel(), np.ones(h * w)])
        uv = np.asarray(p["K_undistorted"]) @ rays
        rgb = cv2.remap(
            image,
            uv[0].reshape(h, w).astype(np.float32),
            uv[1].reshape(h, w).astype(np.float32),
            cv2.INTER_LINEAR,
        )
        views.append(hd.FuseView(v.name, v.depth, v.valid, v.K, v.cam2world, rgb))
    return views


def preview(root: Path) -> None:
    """빠른 미리보기 3D 맵(`preview_scene.glb`)과 `preview.json`을 만든다(명세 5.3, S15P31A101-451)."""
    from .preview_scene import (
        DEFAULT_PREVIEW_STRIDE,
        build_preview_from_pointcloud,
        build_preview_from_views,
    )

    if not (root / "world_alignment.json").is_file():
        align_to_calibration(root)
    M = np.asarray(read_json(root / "world_alignment.json")["mapanything_to_world"], float)
    opts = read_json(root / "job.json")["input"].get("options", {})
    raw_stride = opts.get("preview_stride")
    if raw_stride is None:
        env_stride = os.environ.get("SIMAI_PREVIEW_STRIDE", "").strip()
        raw_stride = int(env_stride) if env_stride else DEFAULT_PREVIEW_STRIDE
    stride = max(1, int(raw_stride))

    pc_path = root / "mapanything" / "pointcloud.npz"
    if pc_path.is_file():
        with np.load(pc_path) as pc:
            pts = np.asarray(pc["points_xyz"], float)
            cols = np.asarray(pc["colors_rgb"])
            faces = np.asarray(pc["faces"], np.int64) if "faces" in pc.files else None
        if len(pts) == 0:
            raise ReconstructionError("STATIC_MAPANYTHING_EMPTY", "미리보기 점구름 정점 없음")
        glb_bytes, meta = build_preview_from_pointcloud(
            pts, cols, M=M, faces=faces, stride=max(1, int(opts.get("preview_stride", 1)))
        )
    else:
        views = _load_colored_fuse_views(root)
        try:
            glb_bytes, meta = build_preview_from_views(views, M=M, stride=stride)
        except ValueError as exc:
            raise ReconstructionError("STATIC_MAPANYTHING_EMPTY", str(exc)) from exc

    if meta["vertex_count"] == 0:
        raise ReconstructionError("STATIC_MAPANYTHING_EMPTY", "미리보기 메시 정점 없음")

    (root / "preview_scene.glb").write_bytes(glb_bytes)
    enhance = _enhance_summary(enhance_visual_glb(root, root / "preview_scene.glb", opts))
    if enhance["applied"] and (enhance["detail"].get("floor") or {}).get("applied"):
        meta["triangle_count"] = int(meta["triangle_count"]) + 2
    write_json(
        root / "preview.json",
        {
            "scene_tier": "PREVIEW",
            "preview_scene": True,
            "texture": None,
            "fallback_reason": "PREVIEW_FAST_PATH",
            "assets": [
                {
                    "kind": "GLTF_VISUAL",
                    "path": "preview_scene.glb",
                    "content_type": "model/gltf-binary",
                    "triangle_count": int(meta["triangle_count"]),
                }
            ],
            "observed_surface_ratio": 1.0,
            "postprocess": ["OUTLIER_REMOVAL", "WORLD_ALIGNMENT"],
            "metrics": meta,
            "enhance": enhance,
        },
    )


def fallback(root: Path) -> None:
    """모델 깊이 격자 메시. 사람 마스크를 적용하고 World→glTF 는 공통 내보내기 어댑터만 쓴다."""
    import cv2
    import trimesh
    from simai_ai.calibration.world_frame.inputs import load_mapanything_run
    from . import run_hybrid_depth as hd
    from .texture_mesh import to_glb_points
    prepared = hd.load_prepared(root / "prepared")
    run = load_mapanything_run(root / "mapanything", prepared=prepared)
    source = {v["name"]: v for v in prepared["views"]}
    M = np.asarray(read_json(root / "world_alignment.json")["mapanything_to_world"], float)
    scene = trimesh.Scene()
    count = 0
    for v in run.views:
        p = source[v.name]
        image = cv2.cvtColor(cv2.imread(str(root / "prepared" / p["image"])), cv2.COLOR_BGR2RGB)
        h, w = v.depth.shape
        yy, xx = np.mgrid[:h, :w]
        rays = np.linalg.inv(v.K) @ np.stack([xx.ravel(), yy.ravel(), np.ones(h*w)])
        uv = np.asarray(p["K_undistorted"]) @ rays
        rgb = cv2.remap(image, uv[0].reshape(h, w).astype(np.float32), uv[1].reshape(h, w).astype(np.float32), cv2.INTER_LINEAR)
        view = hd.FuseView(v.name, v.depth, v.valid, v.K, v.cam2world, rgb)
        V, F, C = hd.grid_mesh(view, 1, None)
        if len(F):
            scene.add_geometry(trimesh.Trimesh(to_glb_points(M, V), F, vertex_colors=C, process=False), node_name=v.name)
            count += len(F)
    if not count:
        raise ReconstructionError("STATIC_MAPANYTHING_EMPTY", "관측 깊이 메시 면 없음")
    scene.metadata["simai"] = {"geometry_role": "VISUAL", "safety_use": False, "texture": None,
                               "surface_source": "OBSERVED_DEPTH_VERTEX_COLOR", "axis_convention": "GLTF_Y_UP"}
    (root / "scene.glb").write_bytes(scene.export(file_type="glb"))
    enhance = _enhance_summary(enhance_visual_glb(root, root / "scene.glb", read_json(root / "job.json")["input"].get("options", {})))
    if enhance["applied"] and (enhance["detail"].get("floor") or {}).get("applied"):
        count += 2
    rec = read_json(root / "bake.json")
    rec.update(enhance=enhance, assets=[{"kind": "GLTF_VISUAL", "path": "scene.glb", "content_type": "model/gltf-binary"}],
               observed_surface_ratio=1.0, postprocess=["OUTLIER_REMOVAL", "WORLD_ALIGNMENT"],
               metrics={"triangle_count": count, "surface_source_map": None,
                        "observed_ratio_basis": "내보낸 관측 깊이 면만 분모. 방 전체 커버리지 아님"})
    write_json(root / "bake.json", rec)


def measure(root: Path) -> None:
    import cv2
    from simai_ai.calibration.world_frame.measured_objects import load_catalog
    from simai_ai.calibration.world_frame.stool_tops import HSVStoolDetector, measure_stools
    from simai_ai.geometry.rig import RigCamera
    from simai_ai.calibration.world_frame.inputs import prepared_to_model_px
    from .run_hybrid_depth import transform_pose
    cal = read_json(root / "calibration.json")
    cams = {c["camera_id"]: c for c in cal["cameras"]}
    objects, catalog = load_catalog()
    obj = next(o for o in objects if o.measurement["kind"] == "STOOL_TOP")
    detector = HSVStoolDetector(obj.detector)
    output_cameras = {v["name"]: v for v in read_json(root / "mapanything" / "cameras.json")["views"]}
    M = np.asarray(read_json(root / "world_alignment.json")["mapanything_to_world"], float)
    rig, detections, masks = {}, {}, {}
    for p in read_json(root / "prepared" / "prepared.json")["views"]:
        cid = p["camera_id"]
        model = output_cameras[p["name"]]
        T = transform_pose(M, np.asarray(model["cam2world"], float))
        # 내보낸 메시와 같은 모델 K·자세로 점검한다. 입력 보정으로 점검 결과를 미리 고정하지 않는다.
        px = prepared_to_model_px(np.array([[0., 0.], [1., 0.], [0., 1.]]), p["image_size"], model["model_input_hw"])
        A = np.array([[px[1, 0]-px[0, 0], 0, px[0, 0]], [0, px[2, 1]-px[0, 1], px[0, 1]], [0, 0, 1]])
        K = np.linalg.solve(A, np.asarray(model["intrinsics_model_res"], float))
        R, t = T[:3, :3].T, -T[:3, :3].T @ T[:3, 3]
        c = {**cams[cid], "K": K.tolist(), "R": R.tolist(), "t": t.tolist(), "distortion": {"model": "NONE", "coeffs": []}}
        rig[cid] = RigCamera.from_calibration(c)
        image = cv2.imread(str(root / "prepared" / p["image"]))
        detections[cid] = detector.detect(image)
        if p["mask"]:
            masks[cid] = cv2.imread(str(root / "prepared" / p["mask"]), cv2.IMREAD_GRAYSCALE) > 127
    scale, count, rec = measure_stools(obj, rig, detections, person_masks=masks)
    write_json(root / "scale_check.json", {"scale": scale, "sample_count": count, "result": rec,
        "catalog": catalog, "calibration_changed": False, "purpose": "SCALE_CHECK_ONLY",
        "input_provenance": read_json(root / "job.json")["input"]["input_provenance"]})


def main() -> int:
    stage, root_arg = sys.argv[1:]
    root = Path(root_arg)
    try:
        if stage in ("prepare", "mapanything"):
            from simai_ai.gpu.budget import WorkerGpuSettings
            from simai_ai.gpu.reserve import FAILED, cap_process_memory
            settings = WorkerGpuSettings.from_env(os.environ)
            fraction = float(os.environ.get("SIMAI_WORKER_TORCH_MEMORY_FRACTION") or settings.budget.worker_fraction)
            if not 0 < fraction <= settings.budget.worker_fraction or cap_process_memory(fraction).state == FAILED:
                raise ReconstructionError("STATIC_GPU_BUDGET_FAILED", "자식 프로세스 GPU 상한 설정 실패")
        {"prepare": prepare, "mapanything": mapanything, "preview": preview, "bake": bake, "fallback": fallback, "measure": measure}[stage](root)
        return 0
    except ReconstructionError as e:
        write_json(root / f"{stage}.error.json", {"code": e.code, "message": str(e)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        code = f"STATIC_{stage.upper()}_INPUT_INVALID" if isinstance(e, ValueError) else f"STATIC_{stage.upper()}_TOOL_FAILED"
        write_json(root / f"{stage}.error.json", {"code": code, "message": type(e).__name__})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
