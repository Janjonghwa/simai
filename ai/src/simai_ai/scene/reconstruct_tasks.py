"""분리 프로세스에서 실행하는 복원 단계. CLI/실험과 동일한 scene 구현을 사용한다."""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

import numpy as np

from .reconstruct import ALIGNMENT_REFERENCE_FILE, ReconstructionError, read_json, sha256, write_json


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


def _mapanything_model() -> str:
    model = os.environ.get("SIMAI_MAPANYTHING_MODEL") or str(Path(os.environ.get("SIMAI_ROOT", "/workspace/simai")) / "weights" / "map-anything")
    if not Path(model).is_dir():
        raise ReconstructionError("STATIC_MODEL_MISSING", "MapAnything 가중치 폴더 없음")
    return model


def run_mapanything_prepared(prepared_dir: Path, out_dir: Path) -> None:
    """prepared 모드 MapAnything 한 번 → out_dir(cameras.json·raw.npz·metrics.json). K 없는 view 는 K 없이 넣는다(-605)."""
    model = _mapanything_model()
    from . import run_mapanything
    # 기존 prepared 모드와 같은 K 전용 추론. 모델 세계를 보정 세계로 정렬하는 것은 다음 단계다.
    sys.argv = ["mapanything", "--run-id", out_dir.name, "--runs-dir", str(out_dir.parent), "--mode", "prepared",
                "--prepared", str(prepared_dir), "--model", model, "--repeat", "1"]
    if run_mapanything.main():
        raise ReconstructionError("STATIC_MAPANYTHING_TOOL_FAILED", "MapAnything 추론 실패")


def mapanything(root: Path) -> None:
    run_mapanything_prepared(root / "prepared", root / "mapanything")


def _person_mask_fn():
    """prepare 와 같은 사람 분할기(SIMAI_SEG_WEIGHTS). 없으면 None(보조 촬영은 사람을 지울 수 없어 건너뛴다)."""
    from simai_ai.calibration.intrinsics.estimate import WeightsRef
    from simai_ai.calibration.world_frame.prepare import UltralyticsPersonSegmenter

    weights = Path(os.environ.get("SIMAI_SEG_WEIGHTS", ""))
    if not weights.is_file():
        return None
    seg = UltralyticsPersonSegmenter(WeightsRef(str(weights), sha256(weights.read_bytes())))
    return lambda image: seg.person_mask(image)[0]


def supplement(root: Path) -> None:
    """세팅 보조 촬영 단계(S15P31A101-605): supplement/request.json → 묶음별 MapAnything·고정 카메라 정렬·관문 → 병합·교체.

    결과 요약은 supplement/result.json. 보조 촬영 실패는 이 단계를 실패시키지 않는다(요약 FALLBACK·SKIPPED)."""
    from . import supplement_capture as sc

    _, conf = sc.load_request(root)
    cfg = sc.SupplementConfig.from_env(os.environ, **{k: conf.get(k) for k in ("max_frames", "chunk_frames", "time_budget_s")})
    sc.run_supplement(root, infer=run_mapanything_prepared, person_mask=_person_mask_fn(), cfg=cfg)


ALIGN_MAX_CENTER_M = 0.10
ALIGN_MAX_ROTATION_DEG = 5.0
ALIGN_FLOOR_W_NORMAL = 120.0
ALIGN_FLOOR_W_HEIGHT = 4.0
ALIGN_ROT_PRIOR_EPS = 1e-3
ALIGN_TRUST_REGION_RATIO = 0.95
log = logging.getLogger(__name__)


def _resolve_floor_constraint(job_doc: dict, explicit: bool | None) -> bool:
    """바닥 평면 제약 활성화 여부를 결정한다(기본값 True, job options 또는 환경변수로 재정의 가능)."""
    if explicit is not None:
        return bool(explicit)
    opts = (job_doc.get("input") or {}).get("options") or {}
    if "floor_constraint" in opts and opts["floor_constraint"] is not None:
        return bool(opts["floor_constraint"])
    env = os.environ.get("SIMAI_ALIGN_FLOOR_CONSTRAINT", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return True


def _mapanything_points(root: Path, *, ma_dir: Path | None = None, prepared_dir: Path | None = None,
                        names: set[str] | None = None) -> np.ndarray | None:
    """MapAnything 모델 좌표의 점구름 (N, 3): `raw.npz` 가 있으면 시점별 유효 점, 아니면 `pointcloud.npz` 정점. 없으면 None.

    names 가 있으면 그 view 의 점만 모은다(보조 촬영 프레임을 World 정렬에서 빼는 데 쓴다, S15P31A101-605)."""
    ma_dir = ma_dir or root / "mapanything"
    prepared_dir = prepared_dir or root / "prepared"
    if (ma_dir / "raw.npz").is_file():
        from simai_ai.calibration.world_frame.inputs import load_mapanything_run, load_prepared

        prepared = load_prepared(prepared_dir) if (prepared_dir / "prepared.json").is_file() else None
        run = load_mapanything_run(ma_dir, prepared=prepared)
        if names is None:
            return run.all_points()
        views = [v for v in run.views if v.name in names]
        return np.concatenate([v.points()[0] for v in views]) if views else None
    if (ma_dir / "pointcloud.npz").is_file():
        with np.load(ma_dir / "pointcloud.npz") as pc:
            if "points_xyz" in pc.files:
                return np.asarray(pc["points_xyz"], dtype=float)
    return None


def write_floor_coverage(root: Path, M: np.ndarray) -> dict | None:
    """바닥 감시 범위(S15P31A101-606)를 `floor_coverage.json` 으로 쓴다. 바닥을 모르면 쓰지 않고 None.

    MapAnything 점구름을 World 로 옮겨(M = mapanything_to_world) 관측 바닥 영역을 뽑고, 작업 폴더 `calibration.json`
    (세팅이면 등록한 새 보정 문서, `publish` 가 미리보기 전에 바꿔 둔다)으로 칸마다 사람이 통째로 보이는 카메라 수를 센다.
    표시용 자료라 미리보기·굽기를 실패시키지 않는다(부르는 쪽이 예외를 삼킨다).
    """
    from simai_ai.calibration.floor_coverage import floor_coverage_from_points

    out = root / "floor_coverage.json"
    out.unlink(missing_ok=True)
    pts = _mapanything_points(root)
    if pts is None or len(pts) == 0:
        return None
    M = np.asarray(M, float)
    world = pts @ M[:3, :3].T + M[:3, 3]
    doc = floor_coverage_from_points(read_json(root / "calibration.json"), world)
    if doc is not None:
        write_json(out, doc)
    return doc


def _extract_floor_plane_ma(
    root: Path, src: np.ndarray, cam2world_list: list[np.ndarray], *,
    ma_dir: Path | None = None, prepared_dir: Path | None = None, names: set[str] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """MapAnything 점구름(`raw.npz` 또는 `pointcloud.npz`)이 있으면 RANSAC 바닥 평면을 찾는다.

    names 가 있으면 그 view(고정 카메라)의 점만 쓴다. 보조 촬영 프레임 점은 World 정렬에 넣지 않는다(S15P31A101-605)."""
    from simai_ai.calibration.world_frame.frame import WorldFrameConfig, _up_prior, find_floor

    try:
        pts = _mapanything_points(root, ma_dir=ma_dir, prepared_dir=prepared_dir, names=names)
    except Exception:  # noqa: BLE001 - 점구름 보조 읽기 실패 시 카메라 중심 정렬로 안전하게 폴백
        return None
    if pts is None or len(pts) < 50:
        return None
    cfg = WorldFrameConfig()
    try:
        up0 = _up_prior(cam2world_list)
        floor_plane, _ = find_floor(pts, src, up0, cfg, np.random.default_rng(cfg.seed))
    except Exception:  # noqa: BLE001
        return None
    if floor_plane is None:
        return None
    inl = np.abs(floor_plane.distance(pts)) < cfg.plane_tol_m
    floor_pts = pts[inl] if np.count_nonzero(inl) >= 10 else pts
    cam_mean = src.mean(axis=0)
    floor_ref = cam_mean - float(floor_plane.distance(cam_mean.reshape(1, 3))[0]) * floor_plane.normal
    return floor_plane.normal, floor_ref, floor_pts


def _solve_similarity_alignment(
    src: np.ndarray,
    dst: np.ndarray,
    rotations: list[np.ndarray],
    floor_info: tuple[np.ndarray, np.ndarray, np.ndarray] | None,
    *,
    max_center_m: float,
) -> tuple[np.ndarray, float, np.ndarray, dict[str, object]]:
    """카메라 중심 변위 + 미소 회전 사전값 + 선택적 바닥 법선(+Z)·높이(Z=0) 제약으로 (Q, scale, shift)를 구한다.

    Stage A 회전 전용 BA 로 인해 카메라 회전 행렬 평균(Q_rot)과 카메라 중심 배치(Q_cen)가 ~1.5° 어긋날 때
    발생하던 12~15 cm 팔 길이(lever-arm) 중심 오차를 제거하고, 바닥 점구름이 있으면 카메라 중심 게이트의
    신뢰 영역(0.95 * max_center_m) 안에서 바닥 법선(+Z)과 바닥 높이(Z=0)를 함께 수평 정렬한다(S15P31A101-535).
    """
    x, y = src - src.mean(0), dst - dst.mean(0)
    denom = float(np.sum(x * x))
    H_base = y.T @ x + ALIGN_ROT_PRIOR_EPS * np.sum(rotations, axis=0)

    if floor_info is None:
        u, _, vt = np.linalg.svd(H_base)
        Q = u @ np.diag([1.0, 1.0, np.linalg.det(u @ vt)]) @ vt
        scale = float(np.sum((x @ Q.T) * y) / denom) if denom > 1e-8 else 0.0
        shift = dst.mean(0) - scale * (Q @ src.mean(0))
        return Q, scale, shift, {"floor_constrained": False}

    floor_normal_ma, floor_ref_ma, floor_pts_ma = floor_info
    ez = np.array([0.0, 0.0, 1.0])
    H_floor = np.outer(ez, floor_normal_ma)

    def eval_wn(wn: float) -> tuple[np.ndarray, float, np.ndarray, float]:
        u, _, vt = np.linalg.svd(H_base + wn * H_floor)
        Q_w = u @ np.diag([1.0, 1.0, np.linalg.det(u @ vt)]) @ vt
        s_w = float(np.sum((x @ Q_w.T) * y) / denom) if denom > 1e-8 else 0.0
        sh_w = dst.mean(0) - s_w * (Q_w @ src.mean(0))
        if wn > 0.0 and ALIGN_FLOOR_W_HEIGHT > 0.0:
            w_z = ALIGN_FLOOR_W_HEIGHT * min(1.0, wn / ALIGN_FLOOR_W_NORMAL)
            z_floor = float(s_w * (Q_w[2, :] @ floor_ref_ma) + sh_w[2])
            sh_w[2] -= (w_z / (len(src) + w_z)) * z_floor
        max_r = float(np.linalg.norm(s_w * (src @ Q_w.T) + sh_w - dst, axis=1).max())
        return Q_w, s_w, sh_w, max_r

    _, _, _, r0 = eval_wn(0.0)
    budget = max(ALIGN_TRUST_REGION_RATIO * max_center_m, r0 + 1e-4)
    Q_hi, s_hi, sh_hi, r_hi = eval_wn(ALIGN_FLOOR_W_NORMAL)
    if r_hi <= budget:
        Q, scale, shift, used_wn = Q_hi, s_hi, sh_hi, ALIGN_FLOOR_W_NORMAL
    else:
        lo, hi = 0.0, ALIGN_FLOOR_W_NORMAL
        for _ in range(24):
            mid = 0.5 * (lo + hi)
            if eval_wn(mid)[3] <= budget:
                lo = mid
            else:
                hi = mid
        Q, scale, shift, _ = eval_wn(lo)
        used_wn = lo

    n_world = Q @ floor_normal_ma
    tilt_deg = float(np.degrees(np.arccos(np.clip(abs(float(n_world[2])), -1.0, 1.0))))
    fz = (scale * (floor_pts_ma @ Q.T) + shift)[:, 2]
    return Q, scale, shift, {
        "floor_constrained": bool(used_wn > 0.0),
        "floor_w_normal": round(float(used_wn), 4),
        "floor_tilt_deg": round(tilt_deg, 4),
        "floor_z_median_m": round(float(np.median(fz)), 5),
        "floor_z_iqr_m": round(float(np.percentile(fz, 75) - np.percentile(fz, 25)), 5),
    }


def solve_fixed_alignment(
    root: Path,
    *,
    ma_dir: Path | None = None,
    prepared_dir: Path | None = None,
    max_center_m: float = ALIGN_MAX_CENTER_M,
    max_rotation_deg: float = ALIGN_MAX_ROTATION_DEG,
    floor_constraint: bool | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """MapAnything Run(ma_dir, 기본 root/mapanything) → 보정 World similarity M 과 기록. 쓰지 않는다.

    고정 카메라(job.json 의 images = 보정 카메라)만으로 푼다. Run 에 다른 view(세팅 보조 촬영 프레임)가 있어도 정렬·바닥 점에
    넣지 않는다(World 는 고정 카메라 4대만, S15P31A101-605). 고정 카메라가 하나라도 빠지면 실패다.
    정렬 기준 문서(`ALIGNMENT_REFERENCE_FILE`)가 있으면 calibration.json 대신 그 문서로 맞춘다(S15P31A101-760: 걷기 다듬기 보정은 같은 World
    에서 카메라를 옮긴 것이라 MapAnything 중심 그대로인 처음 보정으로 정렬한다)."""
    ma_dir = ma_dir or root / "mapanything"
    reference = root / ALIGNMENT_REFERENCE_FILE
    cal = read_json(reference if reference.is_file() else root / "calibration.json")
    cams = {c["camera_id"]: c for c in cal["cameras"]}
    all_views = read_json(ma_dir / "cameras.json")["views"]
    job_doc = read_json(root / "job.json")
    requested = {im["camera_id"] for im in job_doc["input"]["images"]}
    views = [v for v in all_views if v["name"] in requested]
    if {v["name"] for v in views} != requested:
        raise ReconstructionError("STATIC_WORLD_ALIGNMENT_FAILED", "모델 출력과 입력 카메라 불일치")
    rotations, src, dst, cam2world_list = [], [], [], []
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
        cam2world_list.append(T)
    if len(src) < 2 or len(src) != len({v["name"] for v in views}):
        raise ReconstructionError("STATIC_WORLD_ALIGNMENT_FAILED", "독립 카메라 2대 필요")
    src, dst = np.asarray(src), np.asarray(dst)
    use_floor = _resolve_floor_constraint(job_doc, floor_constraint)
    floor_info = (_extract_floor_plane_ma(root, src, cam2world_list, ma_dir=ma_dir, prepared_dir=prepared_dir,
                                          names=requested) if use_floor else None)
    Q, scale, shift, floor_meta = _solve_similarity_alignment(
        src, dst, rotations, floor_info, max_center_m=max_center_m
    )
    residual = np.linalg.norm(scale * src @ Q.T + shift - dst, axis=1)
    angles = [np.arccos(np.clip((np.trace(Q.T @ r) - 1) / 2, -1, 1)) for r in rotations]
    if scale <= 0 or not np.isfinite(scale) or residual.max() > max_center_m or max(angles) > np.deg2rad(max_rotation_deg):
        details = ", ".join(
            f"{view['name']}: center={center:.4f}m rotation={np.degrees(angle):.3f}deg"
            for view, center, angle in zip(views, residual, angles)
        )
        message = (f"카메라 일치 조건 미달 (center<={max_center_m:g}m rotation<={max_rotation_deg:g}deg): "
                   f"scale={scale:.6g}; {details}")
        log.warning("%s", message)
        raise ReconstructionError("STATIC_WORLD_ALIGNMENT_FAILED", message)
    M = np.eye(4)
    M[:3, :3], M[:3, 3] = scale * Q, shift
    return M, {"format": "simai.world_alignment", "status": "OK",
               "cameras_json_sha256": sha256((ma_dir / "cameras.json").read_bytes()),
               "mapanything_to_world": M.tolist(), "calibration": cal,
               "max_center_residual_m": float(residual.max()), "scale": scale,
               "alignment_reference": reference.is_file(), **floor_meta}


def align_to_calibration(
    root: Path,
    *,
    max_center_m: float = ALIGN_MAX_CENTER_M,
    max_rotation_deg: float = ALIGN_MAX_ROTATION_DEG,
    floor_constraint: bool | None = None,
) -> np.ndarray:
    """카메라 중심·회전과 선택적 바닥 평면(+Z, Z=0) 제약을 이용한 similarity. 보정 문서는 읽기만 한다."""
    M, doc = solve_fixed_alignment(root, max_center_m=max_center_m, max_rotation_deg=max_rotation_deg,
                                   floor_constraint=floor_constraint)
    write_json(root / "world_alignment.json", doc)
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
        return ge.enhance_file(glb, config=cfg, cameras=cameras, options=options)
    except Exception as exc:  # noqa: BLE001
        return {"applied": False, "reason": f"ENHANCE_ERROR:{type(exc).__name__}", "purpose": "VISUAL_ONLY", "safety_use": False}


def _enhance_summary(rep: dict) -> dict:
    """metrics 에 싣는 요약: 적용 여부·검사 점수 전후·채운 면적."""
    chk = rep.get("check") or {}
    fl = rep.get("floor") or {}
    rc = rep.get("recolor") or {}
    fi = rep.get("fill") or {}
    ss = fi.get("surface_source") or {}
    inp = rep.get("inpaint") or {}
    out = {"fill_applied": bool(fi.get("applied")) and bool(rep.get("applied")), "fill_reason": fi.get("reason"),
           "inpaint_applied": bool(inp.get("applied")) and bool(rep.get("applied")), "inpaint_mode": inp.get("mode"),
           "inpaint_backend": inp.get("backend_used"), "inpaint_reason": inp.get("reason"),
           "stretched_removed_faces": (fi.get("stretched") or {}).get("removed_faces"),
           "stretched_removed_area_ratio": (fi.get("stretched") or {}).get("removed_area_ratio"),
           "fill_added_triangles": fi.get("added_triangles"),
           "fill_area_m2": ss.get("fill_area_m2"),
           "fill_ratio": ss.get("fill_ratio"),  # ESTIMATED_PLANE = 사진 투영(관측색·추정 평면), UNOBSERVED = 회색
           "fill_share_of_surface": ss.get("fill_share_of_surface"),
           "wall_planes_accepted": len(((fi.get("wall_planes") or {}).get("accepted")) or []),
           "applied": bool(rep.get("applied")), "reason": rep.get("reason"), "purpose": "VISUAL_ONLY", "safety_use": False,
           "check_mode": chk.get("mode"), "check_accepted": chk.get("accepted"),
           "check_mae_before": chk.get("mae_mean_before"), "check_mae_after": chk.get("mae_mean_after"),
           "floor_filled_area_m2": fl.get("filled_area_m2") if fl.get("applied") else None,
           "floor_empty_ratio_before": fl.get("empty_ratio_before"), "floor_empty_ratio_after": fl.get("empty_ratio_after"),
           "neutral_recolored_area_m2": rc.get("recolored_area_m2") if rc.get("applied") else None,
           "neutral_gray_area_m2_before": rc.get("unobserved_area_m2_before"),
           "neutral_gray_area_m2_after": rc.get("unobserved_gray_area_m2_after"),
           "seconds": rep.get("seconds"), "detail": rep}
    return out


def _bake_vertex_color(root: Path, opts: dict) -> None:
    """texrecon 없이 MapAnything 깊이·정점색 메시를 만들고 -511 평면 채우기 + -514 인페인팅을 적용한다."""
    from .preview_scene import build_preview_from_pointcloud, build_preview_from_views

    M = np.asarray(read_json(root / "world_alignment.json")["mapanything_to_world"], float)
    stride = max(1, int(opts.get("preview_stride", 1)))
    pc_path = root / "mapanything" / "pointcloud.npz"
    if pc_path.is_file():
        with np.load(pc_path) as pc:
            pts = np.asarray(pc["points_xyz"], float)
            cols = np.asarray(pc["colors_rgb"])
            faces = np.asarray(pc["faces"], np.int64) if "faces" in pc.files else None
        if len(pts) == 0:
            raise ReconstructionError("STATIC_MAPANYTHING_EMPTY", "점구름 정점 없음")
        glb_bytes, meta = build_preview_from_pointcloud(pts, cols, M=M, faces=faces, stride=stride)
    else:
        views = _load_colored_fuse_views(root)
        try:
            glb_bytes, meta = build_preview_from_views(views, M=M, stride=stride)
        except ValueError as exc:
            raise ReconstructionError("STATIC_MAPANYTHING_EMPTY", str(exc)) from exc
    if meta["vertex_count"] == 0:
        raise ReconstructionError("STATIC_MAPANYTHING_EMPTY", "정점색 메시 정점 없음")
    glb = root / "textured" / "scene.glb"
    glb.parent.mkdir(parents=True, exist_ok=True)
    glb.write_bytes(glb_bytes)
    enhance = _enhance_summary(enhance_visual_glb(root, glb, opts))
    tri_count = int(meta["triangle_count"])
    if enhance["applied"]:
        tri_count += int(enhance["detail"].get("triangles_delta") or 0)
    meta["triangle_count"] = tri_count
    write_json(root / "bake.json", {
        "texture": "none",
        "fallback_reason": None,
        "assets": [{"kind": "GLTF_VISUAL", "path": glb.relative_to(root).as_posix(),
                    "content_type": "model/gltf-binary", "triangle_count": tri_count}],
        "observed_surface_ratio": 1.0,
        "postprocess": ["OUTLIER_REMOVAL", "WORLD_ALIGNMENT"],
        "metrics": meta,
        "enhance": enhance,
        "auxiliary": [p.relative_to(root).as_posix()
                      for p in sorted((glb.parent / ((enhance["detail"].get("fill_atlas") or {}).get("dir") or "-")).glob("*"))
                      if p.is_file()],
    })


def _resolve_final_map_pipeline(opts: dict) -> bool:
    """최종 맵 파이프라인(S15P31A101-539) 활성화 여부를 결정한다(기본값 True)."""
    if "final_map_pipeline" in opts and opts["final_map_pipeline"] is not None:
        return bool(opts["final_map_pipeline"])
    env = os.environ.get("SIMAI_FINAL_MAP_PIPELINE", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return True


def _ensure_wm2_depth_dir(
    root: Path,
    opts: dict,
    *,
    use_calibration_pose: bool = False,
) -> Path | None:
    """사전 계산된 깊이 폴더가 있으면 반환하고, 없으면 WorldMirror 2.0 추론을 실행한다. 가중치가 없으면 None."""
    calib_depth_dir = root / "depth_calib"
    ma_depth_dir = root / "depth"
    if use_calibration_pose and (calib_depth_dir / "depth.json").is_file():
        return calib_depth_dir
    if not use_calibration_pose and (ma_depth_dir / "depth.json").is_file():
        return ma_depth_dir

    wm2_model = (
        os.environ.get("SIMAI_WM2_MODEL")
        or os.environ.get("SIMAI_WORLDMIRROR_MODEL")
        or str(Path(os.environ.get("SIMAI_ROOT", "/workspace/simai")) / "weights" / "worldmirror2")
    )
    if not Path(wm2_model).exists():
        if (ma_depth_dir / "depth.json").is_file():
            return ma_depth_dir
        if (calib_depth_dir / "depth.json").is_file():
            return calib_depth_dir
        return None
    from . import run_hybrid_depth as hd

    target_dir = calib_depth_dir if use_calibration_pose else ma_depth_dir
    if (target_dir / "depth.json").is_file():
        return target_dir
    prep = hd.load_prepared(root / "prepared")
    adapter = hd.WorldMirrorAdapter(
        variant="worldmirror2",
        model=str(wm2_model),
        process_res=int(opts.get("process_res", 952)),
        use_pose=True,
    )
    calib_arg = (root / "calibration.json") if use_calibration_pose else None
    hd.run_predict(adapter, root / "mapanything", prep, target_dir, calibration=calib_arg)
    return target_dir


def run_final_map_pipeline(
    root: Path,
    opts: dict,
    *,
    texrecon: str | None,
    failure_out: dict[str, str] | None = None,
) -> bool:
    """S15P31A101-539 최종 맵 파이프라인(운영 기본: 대체 경로 1 MA_ALIGNED_WM2, 게이트 통과·요청 시 방식 2 -> 2 cm TSDF + mesh_clean + 받침·가구 평면·LaMa 굽기)을 실행한다."""

    def _fail(reason: str) -> bool:
        log.warning("final_map_pipeline 중단 -> FALLBACK2_LEGACY_TEXRECON 전환: %s", reason)
        if failure_out is not None:
            failure_out["reason"] = reason
        return False

    from . import final_map_bake as fmb
    from . import mesh_clean as mclean
    from . import pose_depth_fit as pdf
    from . import run_hybrid_depth as hd
    from . import texture_mesh as tm

    prep = hd.load_prepared(root / "prepared")
    req_path = str(
        opts.get("reconstruction_path") or os.environ.get("SIMAI_RECONSTRUCTION_PATH") or "fallback1"
    ).strip().lower()

    if req_path in ("method2", "method2_calib_wm2"):
        depth_m2_dir = _ensure_wm2_depth_dir(root, opts, use_calibration_pose=True)
        if depth_m2_dir is None:
            return _fail("FINAL_MAP_WM2_DEPTH_MISSING")
        fit_m2 = pdf.fit_and_evaluate_poses_and_depths(
            ma_run=root / "mapanything",
            prepared=prep,
            depth_dir=depth_m2_dir,
            world_alignment_path=root / "world_alignment.json",
            calibration_path=root / "calibration.json",
            mode="method2",
        )
        recorded_fit_summary = fit_m2.pose_depth_fit_summary
        active_fit = fit_m2
    elif req_path == "auto":
        depth_m2_dir = _ensure_wm2_depth_dir(root, opts, use_calibration_pose=True)
        if depth_m2_dir is None:
            return _fail("FINAL_MAP_WM2_DEPTH_MISSING")
        fit_m2 = pdf.fit_and_evaluate_poses_and_depths(
            ma_run=root / "mapanything",
            prepared=prep,
            depth_dir=depth_m2_dir,
            world_alignment_path=root / "world_alignment.json",
            calibration_path=root / "calibration.json",
            mode="method2",
        )
        recorded_fit_summary = fit_m2.pose_depth_fit_summary
        if fit_m2.status == "PASSED":
            active_fit = fit_m2
        else:
            log.info(
                "Method 2 게이트 미달(%s) -> Fallback 1(MA_ALIGNED_WM2)로 자동 전환",
                recorded_fit_summary.get("failure_reason"),
            )
            depth_fb1_dir = _ensure_wm2_depth_dir(root, opts, use_calibration_pose=False) or depth_m2_dir
            active_fit = pdf.fit_and_evaluate_poses_and_depths(
                ma_run=root / "mapanything",
                prepared=prep,
                depth_dir=depth_fb1_dir,
                world_alignment_path=root / "world_alignment.json",
                calibration_path=root / "calibration.json",
                mode="fallback1",
            )
    else:
        # 운영 기본 경로: Fallback 1 (FALLBACK1_MA_ALIGNED_WM2)
        depth_fb1_dir = _ensure_wm2_depth_dir(root, opts, use_calibration_pose=False)
        if depth_fb1_dir is None:
            return _fail("FINAL_MAP_WM2_DEPTH_MISSING")
        depth_m2_dir = (root / "depth_calib") if (root / "depth_calib" / "depth.json").is_file() else depth_fb1_dir
        fit_m2 = pdf.fit_and_evaluate_poses_and_depths(
            ma_run=root / "mapanything",
            prepared=prep,
            depth_dir=depth_m2_dir,
            world_alignment_path=root / "world_alignment.json",
            calibration_path=root / "calibration.json",
            mode="method2",
        )
        active_fit = pdf.fit_and_evaluate_poses_and_depths(
            ma_run=root / "mapanything",
            prepared=prep,
            depth_dir=depth_fb1_dir,
            world_alignment_path=root / "world_alignment.json",
            calibration_path=root / "calibration.json",
            mode="fallback1",
        )
        recorded_fit_summary = fit_m2.pose_depth_fit_summary

    out_dir = root / "textured"
    out_dir.mkdir(parents=True, exist_ok=True)
    cleaned_glb = out_dir / "input_cleaned.glb"

    if not cleaned_glb.is_file():
        if not texrecon:
            return _fail("TEXRECON_NOT_FOUND")
        try:
            import open3d  # noqa: F401
        except ImportError:
            return _fail("TSDF_OPEN3D_NOT_FOUND")

        fitted_dir = root / "depth_fitted"
        fitted_dir.mkdir(parents=True, exist_ok=True)
        npz_d: dict[str, np.ndarray] = {}
        c_views: list[dict[str, object]] = []
        for fv in active_fit.views:
            npz_d[f"{fv.camera_id}__depth_z"] = np.asarray(fv.depth_m, dtype=np.float32)
            npz_d[f"{fv.camera_id}__mask"] = np.asarray(fv.valid, dtype=bool)
            npz_d[f"{fv.camera_id}__conf"] = np.ones_like(fv.depth_m, dtype=np.float32)
            npz_d[f"{fv.camera_id}__intrinsics"] = np.asarray(fv.K, dtype=np.float32)
            npz_d[f"{fv.camera_id}__camera_pose"] = np.asarray(fv.cam2world, dtype=np.float32)
            c_views.append(
                {
                    "name": fv.camera_id,
                    "cam2world": fv.cam2world.tolist(),
                    "intrinsics_model_res": fv.K.tolist(),
                    "model_input_hw": [int(fv.height), int(fv.width)],
                }
            )
        np.savez_compressed(fitted_dir / "raw.npz", **npz_d)
        write_json(fitted_dir / "cameras.json", {"convention": "cam2world 4x4, OpenCV", "views": c_views})
        # texture_mesh 는 ma-run 폴더의 metrics.json(prepared_sha256·result)으로 같은 입력인지 확인한다 → MapAnything Run 의 것을 그대로 둔다.
        ma_metrics_path = root / "mapanything" / "metrics.json"
        if ma_metrics_path.is_file():
            shutil.copyfile(ma_metrics_path, fitted_dir / "metrics.json")
        else:
            write_json(
                fitted_dir / "metrics.json",
                {"result": "SUCCESS", "prepared_sha256": prep.get("_sha256"), "model_version": "wm2-hybrid"},
            )
        id_align_path = fitted_dir / "world_alignment_identity.json"
        write_json(
            id_align_path,
            {
                "format": "simai.world_alignment",
                "status": "OK",
                "cameras_json_sha256": sha256((fitted_dir / "cameras.json").read_bytes()),
                "mapanything_to_world": np.eye(4).tolist(),
                "scale": 1.0,
                "max_center_residual_m": 0.0,
            },
        )
        voxel_m = float(opts.get("voxel_m") or os.environ.get("SIMAI_FINAL_MAP_VOXEL_M") or 0.02)
        target_faces = int(opts.get("target_faces") or os.environ.get("SIMAI_FINAL_MAP_TARGET_FACES") or 300_000)
        num_threads = int(opts.get("num_threads") or os.environ.get("SIMAI_BAKE_NUM_THREADS") or 0)
        tsdf_dir = root / "textured_raw"
        if tsdf_dir.exists():
            shutil.rmtree(tsdf_dir, ignore_errors=True)
        args = [
            "--ma-run",
            str(fitted_dir),
            "--prepared",
            str(root / "prepared"),
            "--out",
            str(tsdf_dir),
            "--world-alignment",
            str(id_align_path),
            "--texrecon",
            texrecon,
            "--terms",
            "area",
            "--loo-terms",
            "",
            "--voxel",
            str(voxel_m),
            "--num-threads",
            str(num_threads),
            "--target-faces",
            str(target_faces),
            "--align-views",
            "off",
            "--run-id",
            read_json(root / "job.json")["run_id"],
            "--jira-key",
            "S15P31A101-539",
        ]
        prev_omp = os.environ.get("OMP_NUM_THREADS")
        try:
            code = tm.main(args)
        finally:
            if prev_omp is None:
                os.environ.pop("OMP_NUM_THREADS", None)
            else:
                os.environ["OMP_NUM_THREADS"] = prev_omp
        if code:
            return _fail(f"FINAL_MAP_TEXTURE_MESH_FAILED:exit_{code}")
        raw_rec = read_json(tsdf_dir / "metrics.json")
        raw_glb_path = tsdf_dir / raw_rec["bakes"]["area"]["glb"]
        clean_cams = [
            mclean.CleanCamera(
                camera_id=fv.camera_id,
                K=fv.K,
                R_w2c=fv.R_w2c,
                t_w2c=fv.t_w2c,
                width=int(fv.width),
                height=int(fv.height),
            )
            for fv in active_fit.views
        ]
        mclean.clean_glb_file(raw_glb_path, cleaned_glb, clean_cams)

    env_lama = os.environ.get("SIMAI_LAMA_WEIGHTS")
    if env_lama:
        lama_weights = Path(env_lama)
    else:
        lama_weights = Path(os.environ.get("SIMAI_ROOT", "/workspace/simai")) / "weights" / "lama" / "big-lama.pt"
        if not lama_weights.is_file() and fmb.DEFAULT_LAMA_WEIGHTS.is_file():
            lama_weights = fmb.DEFAULT_LAMA_WEIGHTS

    walls_doc = read_json(root / "walls.json") if (root / "walls.json").is_file() else None
    pose_mode = "method2" if active_fit.reconstruction_path == "METHOD2_CALIB_WM2" else "fallback1"
    bake_rep = fmb.run_final_map_bake_on_glb(
        cleaned_glb,
        active_fit,
        root / "prepared",
        out_dir,
        lama_weights=lama_weights,
        walls_doc=walls_doc,
        pose_mode=pose_mode,
        output_glb_name="scene_final_wm2.glb",
    )

    final_glb = out_dir / "scene_final_wm2.glb"
    surf_info = bake_rep.get("surface_source") or {}
    obs_ratio = float(surf_info.get("observed_projection_ratio", 0.7))
    fill_ratio = float(surf_info.get("filled_ratio", 0.3))
    st_ratio = float(surf_info.get("see_through_ratio", 0.0))
    ratios: dict[str, float] = {
        "OBSERVED": round(obs_ratio, 4),
        "ESTIMATED_PLANE": round(fill_ratio, 4),
    }
    if st_ratio > 0.0:
        ratios["SEE_THROUGH"] = round(st_ratio, 4)

    source_maps = sorted((out_dir / "planes").glob("final_wm2_*_source.png"))
    if not source_maps:
        source_maps = sorted(out_dir.glob("surface_source_*.png"))

    g_final = fmb.ge.parse_glb(final_glb.read_bytes())
    simai_ext = (g_final.doc.get("extras") or {}).get("simai") or {}
    tri_count = (simai_ext.get("surface_source") or {}).get("kept_mesh_faces")

    gltf_asset: dict[str, object] = {
        "kind": "GLTF_VISUAL",
        "path": final_glb.relative_to(root).as_posix(),
        "content_type": "model/gltf-binary",
    }
    if tri_count is not None:
        gltf_asset["triangle_count"] = int(tri_count)
    assets: list[dict[str, object]] = [gltf_asset]
    for sm in source_maps:
        assets.append(
            {
                "kind": "SURFACE_SOURCE_MAP",
                "path": sm.relative_to(root).as_posix(),
                "content_type": "image/png",
                "ratios": ratios,
                "has_see_through": st_ratio > 0.0,
            }
        )

    postprocess = [
        "DEPTH_ALIGNMENT",
        "OUTLIER_REMOVAL",
        "TSDF_FUSION",
        "DECIMATION",
        "TEXTURE",
        "WORLD_ALIGNMENT",
        "MESH_CLEAN_V2",
        "BACKING_PLANES",
        "FURNITURE_PLANES",
        "LAMA_INPAINT",
    ]
    coobs_med = active_fit.pose_depth_fit_summary.get("coobserved_surface_3d_median_m")
    write_json(
        root / "bake.json",
        {
            "texture": "texrecon",
            "fallback_reason": None,
            "reconstructor": "WORLDMIRROR2_HYBRID",
            "uses_known_pose": active_fit.reconstruction_path == "METHOD2_CALIB_WM2",
            "reconstruction_path": active_fit.reconstruction_path,
            "pose_depth_fit": recorded_fit_summary,
            "map_cameras": active_fit.map_cameras,
            "coobserved_point_disagreement_median_m": coobs_med,
            "assets": assets,
            "observed_surface_ratio": round(obs_ratio, 4),
            "postprocess": postprocess,
            "metrics": {
                "final_map_bake": bake_rep,
                "method2_gate_details": fit_m2.gate_details,
                "active_gate_details": active_fit.gate_details,
            },
            "enhance": {"applied": True, "check_mode": "final_map_v5", "purpose": "VISUAL_ONLY", "safety_use": False},
        },
    )
    return True


def bake(root: Path) -> None:
    if not (root / "world_alignment.json").is_file():
        align_to_calibration(root)
    opts = read_json(root / "job.json")["input"].get("options", {})
    texture_mode = str(opts.get("texture") or os.environ.get("SIMAI_BAKE_TEXTURE", "") or "texrecon").strip()
    if texture_mode == "none":
        _bake_vertex_color(root, opts)
        return
    spec = os.environ.get("SIMAI_TEXRECON", "").strip()
    texrecon = shutil.which(spec or "texrecon")
    final_map_failure_reason: str | None = None
    if _resolve_final_map_pipeline(opts):
        failure_info: dict[str, str] = {}
        try:
            if run_final_map_pipeline(root, opts, texrecon=texrecon, failure_out=failure_info):
                return
            final_map_failure_reason = failure_info.get("reason") or "FINAL_MAP_PIPELINE_RETURNED_FALSE"
        except Exception as exc:  # noqa: BLE001
            final_map_failure_reason = f"FINAL_MAP_PIPELINE_ERROR:{type(exc).__name__}:{exc}"
            log.warning("final_map_pipeline 예외 발생 -> FALLBACK2_LEGACY_TEXRECON 폴백: %s", final_map_failure_reason)
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
    prev_omp = os.environ.get("OMP_NUM_THREADS")
    try:
        code = tm.main(args)
    finally:
        if prev_omp is None:
            os.environ.pop("OMP_NUM_THREADS", None)
        else:
            os.environ["OMP_NUM_THREADS"] = prev_omp
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
    if enhance["applied"] and tri_count is not None:  # 채움 면·옛 바닥 사각형은 더하고 지운 늘어난 면은 뺀다(report.triangles_delta)
        tri_count = int(tri_count) + int(enhance["detail"].get("triangles_delta") or 0)
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
    bake_doc: dict[str, object] = {
        "texture": "texrecon",
        "fallback_reason": final_map_failure_reason,
        "assets": assets,
        "observed_surface_ratio": ratios["OBSERVED"],
        "postprocess": [*post, "TEXTURE", "WORLD_ALIGNMENT"],
        "metrics": rec,
        "enhance": enhance,
        "target_faces": target_faces,
        "num_threads": tm.resolve_num_threads(num_threads),
        "stage_times": rec.get("times"),
        "auxiliary": [p.relative_to(root).as_posix() for p in sorted(glb.parent.glob("observed_camera_*.png"))]
                     + [p.relative_to(root).as_posix() for p in sorted((glb.parent / ((enhance["detail"].get("fill_atlas") or {}).get("dir") or "-")).glob("*"))
                        if p.is_file()],
    }
    if final_map_failure_reason is not None:
        bake_doc["reconstruction_path"] = "FALLBACK2_LEGACY_TEXRECON"
    write_json(root / "bake.json", bake_doc)


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
    try:
        write_floor_coverage(root, M)
    except Exception as exc:  # noqa: BLE001 - 감시 범위는 표시용이다. 미리보기를 실패시키지 않는다
        log.warning("바닥 감시 범위를 만들지 못해 건너뛴다: %s: %s", type(exc).__name__, exc)
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
    map_cams = None
    if (root / "bake.json").is_file():
        mc_doc = read_json(root / "bake.json").get("map_cameras")
        if isinstance(mc_doc, dict) and mc_doc:
            map_cams = mc_doc
    rig, detections, masks = {}, {}, {}
    for p in read_json(root / "prepared" / "prepared.json")["views"]:
        cid = p["camera_id"]
        if cid not in cams:
            continue  # 세팅 보조 촬영 프레임(S15P31A101-605)은 보정 카메라가 아니라 축척 점검에 넣지 않는다
        if map_cams and cid in map_cams:
            from .intrinsics_check import expected_axis_resize_k

            mc = map_cams[cid]
            K = expected_axis_resize_k(
                np.asarray(mc["K"], float),
                tuple(mc["image_size"]),
                tuple(p["image_size"]),
            )
            R = np.asarray(mc["R"], float)
            t = np.asarray(mc["t"], float)
        else:
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
    from simai_ai.gpu.cv_threads import limit_cv2_threads

    limit_cv2_threads(os.environ)
    stage, root_arg = sys.argv[1:]
    root = Path(root_arg)
    try:
        if stage in ("prepare", "mapanything", "supplement"):
            from simai_ai.gpu.budget import WorkerGpuSettings
            from simai_ai.gpu.reserve import FAILED, cap_process_memory
            settings = WorkerGpuSettings.from_env(os.environ)
            fraction = float(os.environ.get("SIMAI_WORKER_TORCH_MEMORY_FRACTION") or settings.budget.worker_fraction)
            if not 0 < fraction <= settings.budget.worker_fraction or cap_process_memory(fraction).state == FAILED:
                raise ReconstructionError("STATIC_GPU_BUDGET_FAILED", "자식 프로세스 GPU 상한 설정 실패")
        {"prepare": prepare, "mapanything": mapanything, "preview": preview, "bake": bake, "fallback": fallback,
         "measure": measure, "supplement": supplement}[stage](root)
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
