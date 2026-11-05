"""EXP-01 텍스처 메시(S15P31A101-298): MapAnything Run(+ prepared 입력) → 하나로 합친 배경 메시에 관측 텍스처를 구운 glTF
+ 텍셀 출처 지도 + Run 기록. 노트북 CPU 만 쓴다(GPU 없음).

설계·정한 수치·버린 대안은 docs/design/exp01-textured-mesh.md. 순서(명세 5.3 복원 후처리, 부록 C.9):
  0층  카메라별 깊이(MapAnything raw.npz, 또는 --depth 의 혼합 깊이를 MapAnything 깊이에 정합) → 떠 있는 점·시점 간 충돌 점 제거
       → TSDF 하나로 합침(Open3D, MIT) → 작은 조각 제거·단순화. 텍스처 소스에서는 사람 마스크·깊이 경계 띠·가림 면을 뺀다.
  1층  mvs-texturing texrecon(Waechter 2014, BSD-3, Docker CPU 이미지 deploy/texrecon): 데이터 비용을 texrecon 이 계산하고
       위 거르기를 데이터 비용 파일(-D)에 적용한 뒤 시점 선택. 데이터 항 area·gmi 를 모두 굽는다.
  2층  관측 없는 면의 연결 성분 중 HoleRule(미터·원본 픽셀·둘러싸임)을 만족하는 것만 texrecon 구멍 채우기(-L 라벨로 통제)에 맡긴다.
  큰 미관측: 텍스처 없이 중립 회색 재질(지어내지 않음). 같은 평면 복사·확산 inpainting 은 만들지 않는다.
  형상 정리(S15P31A101-314, 옵션, mesh_cleanup.py): --align-views sim3|scale 은 TSDF 전 카메라 간 깊이 정렬(기준 카메라 고정, 보정된
  자세로 텍스처도 투영), --plane-cleanup 은 수평 평면(바닥·책상·소파 윗면)을 찾아 깊이·정점을 평면에 맞추고 그 면을 ESTIMATED_PLANE 으로
  표시한다(World 정렬이 있을 때만).
  확인: (a) 한 카메라 빼고 굽기 → 그 카메라 시점 렌더 vs 실제 사진(사람 마스크 밖·관측 영역 PSNR·SSIM), (b) 기존 gridmesh.glb
       (정점 색) 대비 같은 시점 그림, (c) 출처 비율. 개발 교실 관측 기반 지표이고 DATASET_GT 가 아니다.

    # 이미지 한 번 빌드(인터넷 443, GPU 없음)
    docker build -f deploy/texrecon/Dockerfile -t simai/texrecon:dev .
    # 굽기(결과는 저장소 밖, 사람이 찍힌 그림이 들어가므로 커밋하지 않는다)
    uv run --project ai --with open3d==0.19.0 --with opencv-python-headless --with trimesh --with pillow \\
        python ai/experiments/static_scene/texture_mesh.py --ma-run <MapAnything Run> --prepared <prepare 출력> \\
        --out <새 결과 폴더> [--depth <혼합 깊이 폴더>] [--world-alignment <world_frame fix 결과>] [--texture-images <빈 교실 사진 폴더>]

좌표: 계산은 MapAnything world(OpenCV cam2world)에서 한다. --world-alignment 가 status OK 면 glb 정점을 World(m, 오른손, +Z 상방,
바닥 Z=0)로 옮긴 뒤 glTF 규약(+Y 상방, World (x, y, z) → (x, z, −y), extras axis_convention GLTF_Y_UP)으로 낸다(contracts
scene_output assets.axis_convention: GLTF_VISUAL 은 GLTF_Y_UP, 변환은 내보내기 어댑터 한 곳 = to_glb_points. 관제 앱은 회전을 더하지
않는다). 없거나 FAILED 면 MapAnything world 그대로 두고 그렇게 적는다(World·Scene 으로 쓰지 않는다). metrics·출처 지도·확인 지표는 World.
이 결과는 보기용 Visual Geometry 다. Tier 0 위치·판정과 Operational Geometry 에 넣지 않는다(명세 5.3).

종료 코드: 0 성공, 2 입력 오류(다른 입력으로 돈 결과 섞기, 결과 폴더가 비어 있지 않음 등), 3 결과 없음, 4 texrecon 실행 실패.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import mesh_cleanup as mc
from . import run_hybrid_depth as hd
from . import texture_provenance as tp

REPO = Path(__file__).resolve().parents[4]
RESULT_FORMAT = "simai.textured_scene"
RESULT_VERSION = "0.1.0"
RUN_RECORD_SCHEMA_VERSION = "2.2.0"
MVS_TEXTURING = "mvs-texturing@f3374298ac959cb5afe47a14e4d35d2ac7fbdbb1"  # deploy/texrecon/Dockerfile MVST_COMMIT
EXIT_OK, EXIT_CONFIG, EXIT_EMPTY, EXIT_TEXRECON = 0, 2, 3, 4
NEUTRAL_RGB = (128, 128, 128)  # 미관측 면의 중립 기본 재질(텍스처 없음)

# 정한 수치(근거는 설계 문서 "정한 수치")
VOXEL_M = 0.02  # TSDF 칸. MapAnything 모델 해상도 픽셀이 3 m 에서 약 1.1 cm, 10 m 에서 3.8 cm
TRUNC_VOXELS = 8  # TSDF 절단 거리 = 8 칸(16 cm). 시점 간 깊이 차 |Δd|/d p50 1~2%(5 m 에서 5~10 cm)가 서로 깎지 않게
MAX_DEPTH_M = 15.0  # 교실 대각선(약 14 m) 밖 깊이는 통합하지 않는다
MIN_CLUSTER_FACES = 200  # 이보다 작은 떨어진 조각은 지운다(떠 있는 점 잔여)
DEFAULT_BAKE_TARGET_FACES = 150_000  # 세팅 굽기 가속 기본 목표 면 수(40만 → 15만, S15P31A101-451)
HIGH_QUALITY_TARGET_FACES = 400_000  # 11차 기준 고밀도 비교용 목표 면 수
TARGET_FACES = DEFAULT_BAKE_TARGET_FACES  # 단순화 목표 면 수(texrecon 시간·아틀라스 크기와 형상 오차의 절충)
EDGE_BAND_PX = 5  # 깊이 경계 띠 반폭(원본 픽셀, 깊이 격자로 올림). MapAnything 모델 해상도에서는 2 px(원본 약 9 px)
BLUR_SIGMA_PX = 8.0  # 빼고 굽기 보조 지표: 두 그림을 비교 영역 안에서만 σ 8 px 정규화 흐림한 뒤 PSNR(수 px~수십 px 어긋남과 색 차이를 가르는 용도)
OCCLUSION_TOL = 0.05  # 텍스처 가림: 이 카메라 깊이가 면보다 5% 넘게 앞이면 메시에 없는 가림체로 본다(run_hybrid_depth 충돌 기준과 같다)


def resolve_num_threads(num_threads: int) -> int:
    """0 이하이면 가용 CPU 코어 전체(`os.cpu_count() or 1`)로 풀고, 양수면 그대로 돌려준다."""
    if int(num_threads) <= 0:
        return max(1, os.cpu_count() or 1)
    return int(num_threads)


def configure_cpu_threads(num_threads: int) -> int:
    """TSDF·단순화(Open3D/OpenMP)와 texrecon 이 쓸 CPU 스레드 수를 맞추고 유효 스레드 수를 돌려준다."""
    effective = resolve_num_threads(num_threads)
    os.environ["OMP_NUM_THREADS"] = str(effective)
    return effective


# --------------------------------------------------------------------------- 공통


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class DepthView:
    name: str
    depth: np.ndarray  # (h, w) z, 무효 NaN
    valid: np.ndarray
    K: np.ndarray
    cam2world: np.ndarray
    cut: np.ndarray | None = None  # 깊이 모델이 스스로 지운 픽셀(경계 띠 판정용, 신뢰도·사람 마스크 제외)


@dataclass
class Config:
    voxel_m: float = VOXEL_M
    trunc_voxels: int = TRUNC_VOXELS
    max_depth_m: float = MAX_DEPTH_M
    min_cluster_faces: int = MIN_CLUSTER_FACES
    target_faces: int = TARGET_FACES
    edge_band_px: int = EDGE_BAND_PX
    occlusion_tol: float = OCCLUSION_TOL
    align: str = "scale"
    terms: tuple[str, ...] = ("area", "gmi")
    loo_terms: tuple[str, ...] = ("area", "gmi")
    hole: tp.HoleRule = field(default_factory=tp.HoleRule)
    num_threads: int = 0
    align_views: str = "off"  # off | sim3 | scale (-314)
    plane_cleanup: bool = False  # 수평 평면 정리 (-314)
    check_regions: dict[str, tuple[float, ...]] = field(default_factory=dict)  # 이름 → World x0, x1, y0, y1, z0, z1: 가구 윗면 확인 영역


# --------------------------------------------------------------------------- 입력


def load_depth_views(ma_run: Path, prepared: dict[str, Any], cams: dict[str, Any], depth_dir: Path | None,
                     cameras_sha: str, align: str) -> tuple[list[DepthView], dict[str, Any]]:
    """카메라별 깊이(MapAnything world, MapAnything 단위). depth_dir 가 없으면 MapAnything raw.npz(모델 해상도, 신뢰도 하위 10%·사람
    마스크 뺌, world_frame 과 같은 규칙), 있으면 혼합 깊이(원본 해상도)를 MapAnything 깊이에 정합(run_hybrid_depth 와 같은 규칙)."""
    from simai_ai.calibration.world_frame.inputs import load_mapanything_run

    ref = {v.name: v for v in load_mapanything_run(Path(ma_run), prepared=prepared).views}
    rec: dict[str, Any] = {"source": "MAPANYTHING_RAW" if depth_dir is None else "HYBRID_DEPTH"}
    views = []
    if depth_dir is None:
        raw = np.load(Path(ma_run) / "raw.npz")
        for name in sorted(ref):
            r = ref[name]
            cut = ~raw[f"{name}__mask"].astype(bool)  # MapAnything apply_mask·mask_edges 가 지운 픽셀
            views.append(DepthView(name, np.where(r.valid, r.depth, np.nan).astype(np.float32), r.valid.copy(),
                                   np.asarray(r.K, float), np.asarray(cams[name]["cam2world"], float), cut))
        rec["hw"] = {v.name: list(v.depth.shape) for v in views}
        return views, rec
    doc, arrays = hd.load_depth_dir(depth_dir, prepared, cameras_sha)
    rec.update({"depth_json_sha256": doc["_sha256"], "adapter": doc.get("adapter"), "registration": {}})
    pviews = {v["name"]: v for v in prepared["views"]}
    for name in sorted(pviews):
        pv, a = pviews[name], arrays.get(name)
        if a is None or name not in ref:
            raise ValueError(f"view {name!r} 가 깊이 폴더나 MapAnything 결과에 없다")
        depth = a["depth"].astype(np.float32)
        valid = a["valid"].astype(bool) & np.isfinite(depth) & (depth > 0)
        cut = ~valid.copy()
        mask = hd.read_mask(prepared, pv)
        if mask is not None:
            valid &= ~mask
        K = np.asarray(pv["K_undistorted"], float)
        r = ref[name]
        reg = hd.register_to_reference(depth, valid, K, r.depth, r.valid, r.K, align)
        rec["registration"][name] = reg.record()
        if reg.status != "OK":
            continue  # 정합 실패 카메라는 형상에 넣지 않는다(모르면 비운다). 텍스처 거르기의 깊이 검사도 없다
        depth = np.where(valid, reg.scale * depth + reg.shift, np.nan).astype(np.float32)
        views.append(DepthView(name, depth, valid & (depth > 0), K, np.asarray(cams[name]["cam2world"], float), cut))
    rec["hw"] = {v.name: list(v.depth.shape) for v in views}
    return views, rec


def load_texture_sources(prepared: dict[str, Any], texture_dir: Path | None, empty_room: bool) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """카메라별 텍스처 그림과 사람 마스크. 기본은 prepared 의 펴진 그림·마스크. --texture-images 면 그 폴더의 <view>.jpg|png(같은 펴진
    크기)와 <view>.mask.png. 마스크가 없는데 빈 교실이라고 선언(--texture-empty-room)하지 않았으면 거절한다(사람이 섞인다)."""
    import cv2

    out, rec = {}, {"source": "PREPARED" if texture_dir is None else "TEXTURE_IMAGES", "views": {}}
    for pv in prepared["views"]:
        name = pv["name"]
        w, h = pv["image_size"]
        if texture_dir is None:
            img_path = Path(prepared["_dir"]) / pv["image"]
            person = hd.read_mask(prepared, pv)
        else:
            cand = [texture_dir / f"{name}{ext}" for ext in (".jpg", ".png", ".jpeg")]
            img_path = next((p for p in cand if p.is_file()), None)
            if img_path is None:
                raise ValueError(f"{texture_dir} 에 {name} 텍스처 그림이 없다")
            mpath = texture_dir / f"{name}.mask.png"
            if mpath.is_file():
                m = cv2.imread(str(mpath), cv2.IMREAD_GRAYSCALE)
                if m is None or m.shape != (h, w):
                    raise ValueError(f"{mpath}: 마스크를 읽지 못했거나 크기가 {w}×{h} 가 아니다")
                person = m > 127
            elif empty_room:
                person = None
            else:
                raise ValueError(f"{name}: 사람 마스크가 없다. 빈 교실 사진이면 --texture-empty-room 으로 선언한다")
        img = cv2.imread(str(img_path))
        if img is None or img.shape[:2] != (h, w):
            raise ValueError(f"{img_path}: 그림을 읽지 못했거나 크기가 펴진 입력 {w}×{h} 와 다르다")
        out[name] = {"path": img_path, "rgb": cv2.cvtColor(img, cv2.COLOR_BGR2RGB), "person": person, "hw": (h, w),
                     "K": np.asarray(pv["K_undistorted"], float)}
        rec["views"][name] = {"image": img_path.name, "sha256": sha256_file(img_path),
                              "person_mask": None if person is None else round(float(person.mean()), 5)}
    rec["human_removal"] = ("MASK_COMPOSITE" if all(v["person"] is not None for v in out.values()) else
                            "EMPTY_ROOM_DECLARED" if texture_dir is not None and empty_room else "PARTIAL")
    return out, rec


# --------------------------------------------------------------------------- 0층: 형상


def flying_mask(depth: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """앞·뒤 표면 사이에 떠 있는 픽셀(run_hybrid_depth.edge_sharpness 와 같은 규칙: 3×3 폭 > FLYING_JUMP, 이웃 최소·최대에서 둘 다
    FLYING_GAP 넘게 떨어진 중간값)."""
    d = np.where(valid, depth, np.nan).astype(np.float64)
    h, w = d.shape
    pad = np.pad(d, 1, constant_values=np.nan)
    lo, hi = np.full((h, w), np.inf), np.full((h, w), -np.inf)
    for dy in range(3):
        for dx in range(3):
            if dy == 1 and dx == 1:
                continue
            n = pad[dy:dy + h, dx:dx + w]
            lo, hi = np.fmin(lo, n), np.fmax(hi, n)
    with np.errstate(invalid="ignore"):
        disc = (hi - lo) > hd.FLYING_JUMP * lo
        return valid & disc & (d > lo * (1 + hd.FLYING_GAP)) & (d < hi * (1 - hd.FLYING_GAP))


def conflict_keep(views: list[DepthView]) -> dict[str, np.ndarray]:
    """시점 간 깊이 비교(run_hybrid_depth.visibility_filter 와 같은 규칙)를 픽셀 마스크로: 다른 카메라가 본 표면보다 5% 넘게
    앞(그 카메라에서는 빈 공간)인 경우가 3% 안 지지보다 많으면 뺀다. 다른 카메라에 안 보이는 점은 남긴다."""
    fv = [hd.FuseView(v.name, v.depth, v.valid, v.K, v.cam2world) for v in views]
    out = {}
    for vi in fv:
        v, u = np.nonzero(vi.valid)
        X = vi.backproject(v, u)
        support = np.zeros(len(u), np.int32)
        conflict = np.zeros(len(u), np.int32)
        for vj in fv:
            if vj is vi:
                continue
            q, z = vj.project(X)
            front = z > 0
            qx, qy = np.where(front, q[:, 0], -1.0), np.where(front, q[:, 1], -1.0)
            dj, okj = hd.sample_depth(vj.depth, vj.valid, qx, qy)
            idx = np.nonzero(okj & front)[0]
            rel = (z[idx] - dj[idx]) / dj[idx]
            support[idx] += np.abs(rel) <= hd.SUPPORT_TOL
            lo = hd.neighbor_min(vj.depth, vj.valid, qx[idx], qy[idx])
            conflict[idx] += z[idx] < lo * (1 - hd.CONFLICT_TOL)
        keep = np.zeros_like(vi.valid)
        keep[v, u] = conflict <= support
        out[vi.name] = keep
    return out


def fuse_mesh(views: list[DepthView], cfg: Config) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """떠 있는 점·충돌 점을 뺀 깊이를 TSDF 로 합쳐 메시 하나를 만든다(Open3D, CPU). 반환 (정점, 면, 기록)."""
    import open3d as o3d

    if o3d.__version__.startswith("0.20."):
        # shortcut: open3d 0.20.0(Windows 휠에서 확인) 의 ScalableTSDFVolume 이 평면 하나도 면 0 개를 낸다. 고쳐진 판이 나오면 푼다
        raise ValueError(f"open3d {o3d.__version__} 의 TSDF 추출이 비어 있다(0.19.0 을 쓴다: --with open3d==0.19.0)")
    effective_threads = configure_cpu_threads(cfg.num_threads)
    t_conflict = time.perf_counter()
    keep = conflict_keep(views)
    conflict_s = round(time.perf_counter() - t_conflict, 3)
    rec: dict[str, Any] = {
        "views": {},
        "open3d": o3d.__version__,
        "num_threads": effective_threads,
        "requested_threads": int(cfg.num_threads),
    }
    t_tsdf = time.perf_counter()
    integ = o3d.pipelines.integration
    vol = integ.ScalableTSDFVolume(voxel_length=cfg.voxel_m, sdf_trunc=cfg.voxel_m * cfg.trunc_voxels,
                                   color_type=integ.TSDFVolumeColorType.NoColor)
    for v in views:
        fly = flying_mask(v.depth, v.valid)
        use = v.valid & ~fly & keep[v.name]
        n = max(int(v.valid.sum()), 1)
        rec["views"][v.name] = {"valid": int(v.valid.sum()), "flying_ratio": round(float(fly.sum()) / n, 5),
                                "conflict_ratio": round(float((v.valid & ~keep[v.name]).sum()) / n, 5), "used": int(use.sum())}
        h, w = v.depth.shape
        d = np.where(use, v.depth, 0).astype(np.float32)
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(np.zeros((h, w, 3), np.uint8)), o3d.geometry.Image(d), depth_scale=1.0,
            depth_trunc=cfg.max_depth_m, convert_rgb_to_intensity=False)
        K = v.K
        intr = o3d.camera.PinholeCameraIntrinsic(w, h, K[0, 0], K[1, 1], K[0, 2], K[1, 2])
        vol.integrate(rgbd, intr, np.linalg.inv(v.cam2world))
    mesh = vol.extract_triangle_mesh()
    tsdf_s = round(time.perf_counter() - t_tsdf, 3)
    t_clean = time.perf_counter()
    rec["tsdf_faces"] = len(mesh.triangles)

    def clean(m):
        m.remove_degenerate_triangles()
        m.remove_duplicated_triangles()
        m.remove_duplicated_vertices()
        m.remove_non_manifold_edges()
        m.remove_unreferenced_vertices()
        return m

    mesh = clean(mesh)
    if not len(mesh.triangles):
        raise RuntimeError("TSDF 메시에 면이 없다")
    tri_c, n_tri, _ = mesh.cluster_connected_triangles()
    tri_c, n_tri = np.asarray(tri_c), np.asarray(n_tri)
    small = n_tri[tri_c] < cfg.min_cluster_faces
    rec["removed_small_cluster_faces"] = int(small.sum())
    mesh.remove_triangles_by_mask(small)
    mesh = clean(mesh)
    clean_s = round(time.perf_counter() - t_clean, 3)
    faces_before_decimation = int(len(mesh.triangles))
    t_dec = time.perf_counter()
    decimated = faces_before_decimation > cfg.target_faces
    if decimated:
        mesh = clean(mesh.simplify_quadric_decimation(cfg.target_faces))
    decimate_s = round(time.perf_counter() - t_dec, 3)
    rec.update({
        "faces_before_decimation": faces_before_decimation,
        "target_faces": int(cfg.target_faces),
        "decimated": bool(decimated),
        "conflict_s": conflict_s,
        "tsdf_s": tsdf_s,
        "clean_s": clean_s,
        "decimate_s": decimate_s,
    })
    V = np.asarray(mesh.vertices, float)
    F = np.asarray(mesh.triangles, np.int64)
    _, first = np.unique(tp.face_keys(F), return_index=True)  # 같은 정점 세 개의 면은 하나만(texrecon 이 지우면 면 번호가 어긋난다)
    F = F[np.sort(first)]
    rec.update({"faces": int(len(F)), "vertices": int(len(V)), "area_m2": round(float(tp.face_areas(V, F).sum()), 3),
                "voxel_m": cfg.voxel_m, "trunc_m": cfg.voxel_m * cfg.trunc_voxels})
    return V, F, rec


def geometry(dviews: list[DepthView], cams: dict[str, Any], M: np.ndarray | None, cfg: Config, warnings: list[str]):
    """0층 형상 + 정리(-314). 카메라 간 거리 분포(정렬 전·후) → [정렬] → [수평 평면: 찾기 → 깊이를 평면에 → TSDF → 정점을 평면에]
    → 확인 영역 지표. 반환 (깊이 시점(정렬된 자세·평면에 맞춘 깊이), 텍스처 자세, V, F, 평면 면 마스크 또는 None, 기록)."""
    s = unit_scale(M)
    rec: dict[str, Any] = {"cross_view_before": mc.cross_view_distances(dviews, unit_scale=s)}
    if cfg.align_views != "off":
        res = mc.align_views(dviews, mc.AlignConfig(mode=cfg.align_views))
        dviews = res.views
        rec["alignment"] = res.record
        rec["cross_view_after"] = mc.cross_view_distances(dviews, unit_scale=s)
    poses = {name: np.asarray(c["cam2world"], float) for name, c in cams.items()}
    poses.update({v.name: np.asarray(v.cam2world, float) for v in dviews})
    if M is not None:  # 평면에 맞추기 전 입력 깊이의 겹(정렬 효과)
        rec["regions"] = {k: {"layers": mc.region_layers(dviews, M, r[:4], r[4], r[5])} for k, r in cfg.check_regions.items()}
    planes: list[mc.Plane] = []
    pcfg = mc.PlaneConfig()
    if cfg.plane_cleanup and M is None:
        warnings.append("World 정렬이 없어 수평 평면 정리를 하지 않았다(+Z 를 모른다)")
    elif cfg.plane_cleanup:
        A, b = np.asarray(M, float)[:3, :3], np.asarray(M, float)[:3, 3]
        Xs, Ns = [], []
        for v in dviews:
            P, N, ok = mc.oriented_normals(v)
            Xs.append(P[ok] @ A.T + b)
            Ns.append(N[ok] @ (A / s).T)
        planes, prec = mc.find_horizontal_planes(np.concatenate(Xs), np.concatenate(Ns), pcfg)
        snapped = []
        rec["planes"] = {**prec, "depth_snap": {}}
        for v in dviews:
            v2, srec = mc.snap_depth_to_planes(v, planes, M, pcfg)
            snapped.append(v2)
            rec["planes"]["depth_snap"][v.name] = srec
        dviews = snapped
    V, F, fuse_rec = fuse_mesh(dviews, cfg)
    plane_faces = None
    if planes:
        V, plane_faces, mrec = mc.snap_mesh_to_planes(V, F, planes, M, pcfg)
        rec["planes"]["mesh_snap"] = mrec
    elif cfg.plane_cleanup and M is not None:
        plane_faces = np.zeros(len(F), bool)
    if M is not None:
        for k, r in cfg.check_regions.items():
            rec["regions"][k]["flatness"] = mc.region_flatness(hd.transform_points(M, V), F, r[:4], r[4], r[5])
    rec["fuse"] = fuse_rec
    return dviews, poses, V, F, plane_faces, rec


def write_ply(path: Path, V: np.ndarray, F: np.ndarray) -> None:
    head = (f"ply\nformat binary_little_endian 1.0\nelement vertex {len(V)}\nproperty float x\nproperty float y\n"
            f"property float z\nelement face {len(F)}\nproperty list uchar int vertex_indices\nend_header\n")
    fr = np.empty(len(F), dtype=[("n", "u1"), ("i", "<i4", (3,))])
    fr["n"], fr["i"] = 3, F
    with open(path, "wb") as f:
        f.write(head.encode("ascii"))
        f.write(V.astype("<f4").tobytes())
        f.write(fr.tobytes())


# --------------------------------------------------------------------------- texrecon


class Texrecon:
    """texrecon 실행기. spec 이 docker:<이미지> 면 컨테이너 하나를 띄워 두고 입력을 docker cp 로 넣고 결과를 꺼낸다(폴더를 붙이지
    않는다: 이 노트북 Docker Desktop 은 Windows 폴더 붙이기가 멈췄다. 네트워크 없음). 아니면 그 실행 파일로 돈다.
    경로는 모두 root(결과 폴더) 아래여야 한다. opts 안의 Path 는 입력 파일로 보고 넣는다."""

    def __init__(self, spec: str, root: Path, num_threads: int = 0) -> None:
        self.spec, self.root = spec, root.resolve()
        self.requested_threads = int(num_threads)
        self.num_threads = resolve_num_threads(num_threads)
        self.image = spec.split(":", 1)[1] if spec.startswith("docker:") else None
        self.cid: str | None = None
        self.pushed: dict[str, tuple[int, int]] = {}
        self.calls: list[dict[str, Any]] = []

    def describe(self) -> dict[str, Any]:
        if self.image:
            r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", self.image], capture_output=True, text=True)
            if r.returncode != 0:
                raise ValueError(f"texrecon 이미지 {self.image} 가 없다(docker build -f deploy/texrecon/Dockerfile -t {self.image} .)")
            return {"runner": "docker", "image": self.image, "image_id": r.stdout.strip(), "mvs_texturing": MVS_TEXTURING,
                    "num_threads": self.num_threads, "requested_threads": self.requested_threads}
        if not Path(self.spec).is_file():
            raise ValueError(f"texrecon 실행 파일 {self.spec} 이 없다")
        return {"runner": "native", "path": self.spec, "sha256": sha256_file(Path(self.spec)),
                "mvs_texturing": "unknown(직접 빌드)", "num_threads": self.num_threads,
                "requested_threads": self.requested_threads}

    def __enter__(self) -> "Texrecon":
        if self.image:
            r = self._docker(["run", "-d", "--rm", "--network", "none", "--entrypoint", "sleep", self.image, "infinity"])
            self.cid = r.stdout.strip()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.cid:
            subprocess.run(["docker", "rm", "-f", self.cid], capture_output=True, text=True)
            self.cid = None

    def _docker(self, args: list[str], timeout: float = 3600) -> subprocess.CompletedProcess:
        r = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if r.returncode != 0:
            raise TexreconError(f"docker {args[0]} 실패({r.returncode}): {(r.stderr or r.stdout)[-400:]}")
        return r

    def _rel(self, p: Path) -> str:
        return Path(p).resolve().relative_to(self.root).as_posix()

    def _arg(self, p: Path) -> str:
        return f"/work/{self._rel(p)}" if self.image else str(Path(p).resolve())

    def _push(self, p: Path) -> None:
        st = Path(p).stat()
        stamp = (st.st_size, st.st_mtime_ns)
        rel = self._rel(p)
        if self.pushed.get(rel) == stamp:
            return
        self._docker(["exec", self.cid, "mkdir", "-p", f"/work/{Path(rel).parent.as_posix()}"])
        if Path(p).is_dir():
            self._docker(["exec", self.cid, "rm", "-rf", f"/work/{rel}"])
        self._docker(["cp", str(Path(p).resolve()), f"{self.cid}:/work/{rel}"])
        self.pushed[rel] = stamp

    def run(self, scene: Path, mesh: Path, out_prefix: Path, opts: list[str | Path]) -> float:
        out_prefix.parent.mkdir(parents=True, exist_ok=True)
        inputs = [scene, mesh] + [o for o in opts if isinstance(o, Path)]
        args = [self._arg(o) if isinstance(o, Path) else o for o in opts]
        args += [f"--num_threads={self.num_threads}"] if self.num_threads > 0 else []
        args += [self._arg(scene), self._arg(mesh), self._arg(out_prefix)]
        t0 = time.perf_counter()
        if self.image:
            if not self.cid:
                raise TexreconError("texrecon 컨테이너가 없다(with Texrecon(...) 안에서 부른다)")
            for p in inputs:
                self._push(p)
            out_dir = f"/work/{self._rel(out_prefix.parent)}"
            self._docker(["exec", self.cid, "mkdir", "-p", out_dir])
            cmd = ["docker", "exec", self.cid, "texrecon", *args]
        else:
            cmd = [self.spec, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if self.image:
            subprocess.run(["docker", "cp", f"{self.cid}:{out_dir}/.", str(out_prefix.parent)], capture_output=True, text=True)
        dt = round(time.perf_counter() - t0, 2)
        (out_prefix.parent / "texrecon.log").write_text(" ".join(cmd) + "\n\n" + r.stdout + "\n" + r.stderr, encoding="utf-8")
        self.calls.append({"out": self._rel(out_prefix), "opts": [self._rel(o) if isinstance(o, Path) else o for o in opts],
                           "seconds": dt, "returncode": r.returncode})
        if r.returncode != 0:
            raise TexreconError(f"texrecon 실패({r.returncode}): {(r.stderr or r.stdout)[-400:]}")
        return dt


class TexreconError(RuntimeError):
    pass


SKIP_LEVELING = ["--skip_global_seam_leveling", "--skip_local_seam_leveling", "--skip_hole_filling"]


def write_scene(scene: Path, tex: dict[str, dict[str, Any]], poses: dict[str, np.ndarray], names: list[str]) -> None:
    """poses = 카메라 이름 → cam2world(정렬했으면 보정된 자세)."""
    scene.mkdir(parents=True)
    for name in names:
        t = tex[name]
        shutil.copyfile(t["path"], scene / f"{name}{t['path'].suffix.lower()}")
        (scene / f"{name}.cam").write_text(tp.cam_file_text(t["K"], np.asarray(poses[name], float), t["hw"]), encoding="ascii")


# --------------------------------------------------------------------------- 1·2층: 굽기


def record_texrecon_times(times: dict[str, float], term: str, seconds: dict[str, float]) -> None:
    """단계별 실행 시간과 그 합계를 함께 기록한다."""
    times[f"texrecon_{term}_s"] = round(sum(seconds.values()), 3)
    for stage, duration in seconds.items():
        times[f"texrecon_{term}_{stage}_s"] = duration


@dataclass
class Bake:
    tag: str
    labels: np.ndarray
    eligible: np.ndarray
    cls: np.ndarray
    cam: np.ndarray
    present: np.ndarray
    obj: dict[str, Any]  # parse_obj + 'full_face'(출력 면 → 전체 면), 'atlases'(재질별 RGB 배열)
    holes: dict[str, Any]
    seconds: dict[str, float]


def unit_scale(M: np.ndarray | None) -> float:
    """메시(MapAnything) 단위 1 의 미터: World 닮음 변환 M 의 축척 det(M[:3,:3])^(1/3). M 이 없으면 1(미터 아님, 기록에 적는다)."""
    return 1.0 if M is None else float(np.cbrt(np.linalg.det(np.asarray(M, float)[:3, :3])))


def bake(tr: Texrecon, out: Path, scene: Path, mesh_ply: Path, V: np.ndarray, F: np.ndarray, costs: tp.DataCosts,
         entries: np.ndarray, views: list[tp.TexView], cfg: Config, pairs: np.ndarray, tag: str, scale: float = 1.0,
         plane: np.ndarray | None = None) -> Bake:
    """거른 데이터 비용 → 시점 선택(-D) → 구멍 판정 → 관측·작은 구멍 면만 최종 굽기(-L, 구멍 채우기·이음매 보정)."""
    from PIL import Image

    out.mkdir(parents=True)
    tp.write_spt(out / "costs_filtered.spt", costs, entries)
    sec = {"select": tr.run(scene, mesh_ply, out / "select" / "tex", ["-D", out / "costs_filtered.spt", *SKIP_LEVELING])}
    labels = tp.read_labeling(out / "select" / "tex_labeling.vec")
    if len(labels) != len(F):
        raise TexreconError(f"라벨 수 {len(labels)} 가 면 수 {len(F)} 와 다르다(texrecon 이 면을 지웠다)")
    eligible, recs = tp.classify_holes(V, F, labels, views, cfg.hole, pairs, scale)
    keep = (labels > 0) | eligible
    used = np.unique(F[keep])
    remap = np.full(len(V), -1, np.int64)
    remap[used] = np.arange(len(used))
    sub_faces = np.nonzero(keep)[0]
    Fs = remap[F[sub_faces]]
    write_ply(out / "sub.ply", V[used], Fs)
    tp.write_labeling(out / "sub_labeling.vec", labels[sub_faces])
    sec["final"] = tr.run(scene, out / "sub.ply", out / "final" / "tex", ["-L", out / "sub_labeling.vec"])
    obj = tp.parse_obj(out / "final" / "tex.obj")
    full_face = sub_faces[tp.map_faces(Fs, obj["Fv"])]
    present = np.zeros(len(F), bool)
    present[full_face] = True
    cls, cam = tp.face_provenance(labels, eligible, present, plane)
    obj["full_face"] = full_face
    obj["atlases"] = [np.asarray(Image.open(out / "final" / obj["textures"][m]).convert("RGB")) for m in obj["materials"]]
    small = [r for r in recs if r["inradius_m"] is not None]
    holes = {"components": len(recs), "enclosed": sum(r["enclosed"] for r in recs),
             "small_checked": len(small), "eligible": sum(r["eligible"] for r in recs),
             "eligible_faces": int(eligible.sum()), "filled_faces": int((cls == tp.INTERPOLATED).sum()),
             "eligible_not_filled_faces": int((eligible & ~present).sum()),
             "observed_label_missing_faces": int(((labels > 0) & ~present).sum())}
    (out / "holes.json").write_text(json.dumps({"rule": asdict(cfg.hole), "unit_scale_m": scale, "summary": holes, "checked": small},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    return Bake(tag, labels, eligible, cls, cam, present, obj, holes, sec)


def write_provenance(b: Bake, out: Path) -> tuple[list[dict[str, Any]], list[np.ndarray], list[np.ndarray]]:
    """재질(아틀라스)마다 출처 지도(0/1/2)·관측 카메라 지도(시점 번호 + 1). 무손실 8비트 단일 채널 PNG, 아틀라스와 같은 UV·해상도."""
    from PIL import Image

    obj, maps, covered, files = b.obj, [], [], []
    for k, m in enumerate(obj["materials"]):
        sel = obj["Fm"] == k
        hw = obj["atlases"][k].shape[:2]
        ff = obj["full_face"][sel]
        uv = obj["VT"][obj["Ft"][sel]]
        src, cam_map, cov = tp.provenance_maps(uv, b.cls[ff], b.cam[ff], hw)
        Image.fromarray(src, mode="L").save(out / f"surface_source_{k:04d}.png")
        Image.fromarray(cam_map, mode="L").save(out / f"observed_camera_{k:04d}.png")
        maps.append(src)
        covered.append(cov)
        files.append({"material": m, "albedo": f"albedo_{k:04d}.png", "surface_source": f"surface_source_{k:04d}.png",
                      "observed_camera": f"observed_camera_{k:04d}.png", "size": [int(hw[1]), int(hw[0])]})
    return files, maps, covered


GLTF_Y_UP = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]])  # World (x, y, z) → glTF (x, z, −y), 회전(det +1, 감김 방향 유지)


def glb_axis_convention(M: np.ndarray | None) -> str:
    return "GLTF_Y_UP" if M is not None else "MAPANYTHING_WORLD_OPENCV(NOT_WORLD)"


def to_glb_points(M: np.ndarray | None, X: np.ndarray) -> np.ndarray:
    """메시(MapAnything) 좌표 → glb 정점. World 정렬(M)이 있으면 World 로 옮긴 뒤 glTF 규약(+Y 상방, (x, z, −y))으로 바꾼다
    (contracts scene_output assets.axis_convention: GLTF_VISUAL 은 GLTF_Y_UP, 변환은 내보내기 어댑터 한 곳). 여기가 그 한 곳이고,
    받는 쪽(관제 앱 SceneBackground)은 회전을 더하지 않는다. M 이 없으면 World 가 아니라 MapAnything world 그대로 둔다."""
    if M is None:
        return np.asarray(X, float)
    return hd.transform_points(M, np.asarray(X, float)) @ GLTF_Y_UP.T


def export_glb(path: Path, V: np.ndarray, F: np.ndarray, b: Bake, M: np.ndarray | None, extras: dict[str, Any]) -> int:
    """관측·보간·추정 평면 면은 아틀라스마다 텍스처 메시 하나, 미관측 면은 중립 회색 메시 하나. 정점은 to_glb_points(World 면
    glTF +Y 상방), UV·출처 지도는 그대로. 반환 glb 의 삼각형 수."""
    import trimesh
    from PIL import Image

    obj = b.obj
    scene = trimesh.Scene()
    sub_V = to_glb_points(M, obj["V"])
    tris = 0
    for k, m in enumerate(obj["materials"]):
        sel = np.nonzero(obj["Fm"] == k)[0]
        corners = np.column_stack([obj["Fv"][sel].reshape(-1), obj["Ft"][sel].reshape(-1)])
        uniq, inv = np.unique(corners, axis=0, return_inverse=True)
        mat = trimesh.visual.material.PBRMaterial(name=f"observed_{k:04d}", baseColorTexture=Image.fromarray(obj["atlases"][k]),
                                                  metallicFactor=0.0, roughnessFactor=1.0)
        mesh = trimesh.Trimesh(sub_V[uniq[:, 0]], inv.reshape(-1, 3),
                               visual=trimesh.visual.TextureVisuals(uv=obj["VT"][uniq[:, 1]], material=mat), process=False)
        scene.add_geometry(mesh, geom_name=f"textured_{k:04d}", node_name=f"textured_{k:04d}")
        tris += len(sel)
    un = np.nonzero(b.cls == tp.UNOBSERVED)[0]
    if len(un):
        used, inv = np.unique(F[un], return_inverse=True)
        mat = trimesh.visual.material.PBRMaterial(name="unobserved_neutral", baseColorFactor=[*NEUTRAL_RGB, 255],
                                                  metallicFactor=0.0, roughnessFactor=1.0)
        mesh = trimesh.Trimesh(to_glb_points(M, V[used]), inv.reshape(-1, 3),
                               visual=trimesh.visual.TextureVisuals(material=mat), process=False)
        scene.add_geometry(mesh, geom_name="unobserved_neutral", node_name="unobserved_neutral")
        tris += len(un)
    path.write_bytes(inject_extras(scene.export(file_type="glb"), extras))
    return tris


def inject_extras(glb: bytes, extras: dict[str, Any]) -> bytes:
    """glb JSON 덩어리 최상위에 extras.simai 를 넣는다(glTF 2.0 extras, 확장 선언 불필요)."""
    magic, ver, _ = struct.unpack_from("<4sII", glb, 0)
    if magic != b"glTF" or ver != 2:
        raise ValueError("glb 2.0 이 아니다")
    jlen, jtype = struct.unpack_from("<I4s", glb, 12)
    if jtype != b"JSON":
        raise ValueError("glb 첫 덩어리가 JSON 이 아니다")
    doc = json.loads(glb[20:20 + jlen].decode("utf-8"))
    doc.setdefault("extras", {})["simai"] = extras
    js = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    rest = glb[20 + jlen:]
    return struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js) + len(rest)) + struct.pack("<I4s", len(js), b"JSON") + js + rest


# --------------------------------------------------------------------------- 확인: 렌더·지표


class Renderer:
    """Open3D 광선 투사로 메시를 카메라 시점에서 그린다(CPU). 면 번호와 무게중심 좌표를 받아 색은 numpy 로 칠한다."""

    def __init__(self, V: np.ndarray, F: np.ndarray) -> None:
        import open3d as o3d

        self.scene = o3d.t.geometry.RaycastingScene()
        self.scene.add_triangles(V.astype(np.float32), F.astype(np.uint32))
        self.F = F

    def cast(self, K: np.ndarray, cam2world: np.ndarray, hw: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
        import open3d as o3d

        rays = tp.pinhole_rays(K, cam2world, hw).astype(np.float32)
        ans = self.scene.cast_rays(o3d.core.Tensor(rays))
        prim = ans["primitive_ids"].numpy().astype(np.int64)
        prim[prim == 4294967295] = -1
        return prim, ans["primitive_uvs"].numpy().astype(np.float64)


def depth_coverage(rend: "Renderer", V: np.ndarray, F: np.ndarray, views: list[DepthView], tol: float = 0.05) -> dict[str, float]:
    """형상 지표: 카메라마다 쓴 깊이 픽셀 중 메시를 그 시점에서 그린 깊이가 tol(상대) 안인 비율(합친 메시가 입력 표면을 잃지 않았는지)."""
    out = {}
    for v in views:
        prim, bary = rend.cast(v.K, v.cam2world, v.depth.shape)
        hit = prim >= 0
        P = V[F[np.maximum(prim, 0)]]
        X = P[..., 0, :] * (1 - bary[..., :1] - bary[..., 1:2]) + P[..., 1, :] * bary[..., :1] + P[..., 2, :] * bary[..., 1:2]
        z = ((X - v.cam2world[:3, 3]) @ v.cam2world[:3, :3])[..., 2]
        with np.errstate(invalid="ignore"):
            ok = v.valid & hit & (np.abs(z - v.depth) <= tol * v.depth)
        out[v.name] = round(float(ok.sum()) / max(int(v.valid.sum()), 1), 4)
    return out


def shade_bake(prim: np.ndarray, bary: np.ndarray, b: Bake) -> tuple[np.ndarray, np.ndarray]:
    """텍스처 메시 색과 출처(맞지 않은 픽셀 255)."""
    h, w = prim.shape
    rgb = np.zeros((h, w, 3), np.uint8)
    src = np.full((h, w), 255, np.uint8)
    hit = prim >= 0
    src[hit] = b.cls[prim[hit]]
    rgb[hit] = NEUTRAL_RGB
    obj = b.obj
    out_of = np.full(len(b.cls), -1, np.int64)
    out_of[obj["full_face"]] = np.arange(len(obj["full_face"]))
    oi = np.where(hit, out_of[np.maximum(prim, 0)], -1)
    tex = oi >= 0
    ys, xs = np.nonzero(tex)
    o = oi[tex]
    u, v = bary[tex, 0], bary[tex, 1]
    t = obj["VT"][obj["Ft"][o]]  # (n, 3, 2)
    uv = t[:, 0] * (1 - u - v)[:, None] + t[:, 1] * u[:, None] + t[:, 2] * v[:, None]
    mat = obj["Fm"][o]
    for k, atlas in enumerate(obj["atlases"]):
        s = mat == k
        px = tp.obj_uv_to_px(uv[s], atlas.shape[:2])
        rgb[ys[s], xs[s]] = np.clip(np.rint(tp.bilinear(atlas, px[:, 0], px[:, 1])), 0, 255).astype(np.uint8)
    return rgb, src


def load_gridmesh(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """run_mapanything 의 gridmesh.glb(정점 색). 노드 변환(OpenCV → OpenGL 뒤집기)은 무시하고 기하 정점(MapAnything world)을 쓴다."""
    import trimesh

    s = trimesh.load(str(path))
    Vs, Fs, Cs, off = [], [], [], 0
    for g in s.geometry.values():
        Vs.append(np.asarray(g.vertices, float))
        Fs.append(np.asarray(g.faces, np.int64) + off)
        Cs.append(np.asarray(g.visual.vertex_colors)[:, :3])
        off += len(g.vertices)
    return np.concatenate(Vs), np.concatenate(Fs), np.concatenate(Cs)


def shade_vertex_colors(prim: np.ndarray, bary: np.ndarray, F: np.ndarray, C: np.ndarray) -> np.ndarray:
    rgb = np.zeros(prim.shape + (3,), np.uint8)
    hit = prim >= 0
    f = F[prim[hit]]
    u, v = bary[hit, 0:1], bary[hit, 1:2]
    rgb[hit] = np.clip(np.rint(C[f[:, 0]] * (1 - u - v) + C[f[:, 1]] * u + C[f[:, 2]] * v), 0, 255).astype(np.uint8)
    return rgb


def shade_geometry(prim: np.ndarray, V: np.ndarray, F: np.ndarray, cam2world: np.ndarray) -> np.ndarray:
    """형상 확인용 회색 음영: 면 법선과 시선(카메라 앞 방향)의 |cos|. 뾰족함·겹친 면·구멍이 색 없이 보인다. 맞지 않은 픽셀은 검정."""
    n = mc.face_normals(V, F)
    fwd = np.asarray(cam2world, float)[:3, 2]
    lam = np.abs(np.nan_to_num(n @ fwd))
    g = np.zeros(prim.shape, np.uint8)
    hit = prim >= 0
    g[hit] = np.clip(40 + 215 * lam[prim[hit]], 0, 255).astype(np.uint8)
    return np.repeat(g[..., None], 3, -1)


SRC_COLORS = np.array([[220, 60, 60], [240, 200, 40], [60, 180, 90], [60, 120, 240]], np.uint8)  # 미관측 빨강, 보간 노랑, 관측 초록, 추정 평면 파랑


def overlay(rgb: np.ndarray, src: np.ndarray) -> np.ndarray:
    out = rgb.copy()
    m = src < len(SRC_COLORS)
    out[m] = (0.5 * rgb[m] + 0.5 * SRC_COLORS[src[m]]).astype(np.uint8)
    return out


def tile(images: list[np.ndarray], scale: float, labels: list[str]) -> np.ndarray:
    import cv2

    small = [cv2.resize(im, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) for im in images]
    for im, text in zip(small, labels):
        cv2.putText(im, text, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return np.concatenate([np.concatenate(small[:2], 1), np.concatenate(small[2:4], 1)], 0)


def save_jpg(path: Path, rgb: np.ndarray) -> None:
    import cv2

    cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])


# --------------------------------------------------------------------------- 본체


def run(a: argparse.Namespace, cfg: Config) -> dict[str, Any]:
    started = now_ms()
    a.git = git_info()  # 실행을 시작할 때의 코드(끝날 때 작업 트리가 바뀌어도 이 값)
    t_all = time.perf_counter()
    out: Path = a.out
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} 가 비어 있지 않다(결과를 덮어쓰지 않는다)")
    prepared = hd.load_prepared(a.prepared)
    ma_rec = hd.check_run_matches_prepared(a.ma_run, prepared)
    cams, cameras_sha = hd.read_cameras(a.ma_run)
    M, wa_rec = (None, {"status": None, "note": "World 정렬 입력 없음"})
    if a.world_alignment is not None:
        M, wa_rec = hd.load_world_alignment(a.world_alignment, cameras_sha)
    tr = Texrecon(a.texrecon, out, cfg.num_threads)
    out.mkdir(parents=True, exist_ok=True)
    tr_rec = tr.describe()
    with tr:
        return _run(a, cfg, out, tr, tr_rec, prepared, ma_rec, cams, cameras_sha, M, wa_rec, started, t_all)


def _run(a: argparse.Namespace, cfg: Config, out: Path, tr: Texrecon, tr_rec: dict[str, Any], prepared: dict[str, Any],
         ma_rec: dict[str, Any], cams: dict[str, Any], cameras_sha: str, M: np.ndarray | None, wa_rec: dict[str, Any],
         started: int, t_all: float) -> dict[str, Any]:
    warnings: list[str] = []
    times: dict[str, float] = {}

    # 0층: 형상
    t0 = time.perf_counter()
    t_ld = time.perf_counter()
    dviews, depth_rec = load_depth_views(a.ma_run, prepared, cams, a.depth, cameras_sha, cfg.align)
    times["load_depth_s"] = round(time.perf_counter() - t_ld, 3)
    if not dviews:
        raise RuntimeError("형상에 쓸 카메라 깊이가 없다")
    dviews, poses, V, F, plane_faces, cleanup_rec = geometry(dviews, cams, M, cfg, warnings)
    geo_rec = cleanup_rec.pop("fuse")
    for k_time in ("conflict_s", "tsdf_s", "clean_s", "decimate_s"):
        if k_time in geo_rec:
            times[k_time] = geo_rec[k_time]
    (out / "mesh").mkdir()
    write_ply(out / "mesh" / "mesh.ply", V, F)
    pairs = tp.face_pairs(F)
    rend = Renderer(V, F)
    t_dc = time.perf_counter()
    geo_rec["depth_coverage"] = depth_coverage(rend, V, F, dviews)
    times["depth_coverage_s"] = round(time.perf_counter() - t_dc, 3)
    times["geometry_s"] = round(time.perf_counter() - t0, 1)

    # 텍스처 소스(사람 마스크·깊이 경계 띠·가림 검사)
    tex, tex_rec = load_texture_sources(prepared, a.texture_images, a.texture_empty_room)
    names = sorted(tex)  # texrecon 시점 순서(.cam 이름순)
    dv = {v.name: v for v in dviews}
    views = []
    for name in names:
        t = tex[name]
        d = dv.get(name)
        edge = None
        if d is not None:
            r = int(np.ceil(cfg.edge_band_px * d.depth.shape[1] / t["hw"][1]))
            edge = tp.dilate(tp.discontinuity_mask(d.depth, d.valid, hd.FLYING_JUMP, d.cut), r)
        else:
            warnings.append(f"{name}: 깊이가 없어(정합 실패) 깊이 경계·가림 검사 없이 텍스처 소스로 쓴다")
        views.append(tp.TexView(name, t["K"], np.asarray(poses[name], float), t["hw"], t["person"],
                                None if d is None else d.depth, None if d is None else d.valid,
                                None if d is None else d.K, edge))
    write_scene(out / "scene", tex, poses, names)

    # 1·2층: 데이터 항마다 굽기
    report: dict[str, Any] = {"bakes": {}, "loo": {}}
    bakes: dict[str, Bake] = {}
    filtered: dict[str, np.ndarray] = {}
    costs_by_term: dict[str, tp.DataCosts] = {}
    frame = "WORLD" if M is not None else "MAPANYTHING_WORLD"
    scale = unit_scale(M)
    unit = "m" if M is not None else "MapAnything 모델 축척(미터 아님, 실측 미검증)"
    # 형상·구멍 판정은 MapAnything 단위로 계산한다. 미터 기준·면적은 World 축척을 곱해 미터로(World 가 아니면 1, 미터 아님)
    geo_rec["area_m2"] = round(geo_rec["area_m2"] * scale ** 2, 3)
    geo_rec["unit"] = unit
    geo_rec["unit_scale_m"] = scale
    for term in sorted(set(cfg.terms) | set(cfg.loo_terms)):
        t0 = time.perf_counter()
        bdir = out / f"bake-{term}"
        t_dcost = time.perf_counter()
        tr.run(out / "scene", out / "mesh" / "mesh.ply", bdir / "costs" / "tex", ["-d", term, *SKIP_LEVELING])
        costs = tp.read_spt(bdir / "costs" / "tex_data_costs.spt")
        entries, frec = tp.filter_costs(costs, V, F, views, cfg.occlusion_tol)
        costs_by_term[term], filtered[term] = costs, entries
        data_costs_s = round(time.perf_counter() - t_dcost, 3)
        times[f"data_costs_{term}_s"] = data_costs_s
        report["bakes"][term] = {"filter": frec, "cost_entries": int(len(costs.entries)), "kept_entries": int(len(entries))}
        if term in cfg.terms:
            b = bake(tr, bdir / "all", out / "scene", out / "mesh" / "mesh.ply", V, F, costs, entries, views, cfg, pairs, term, scale,
                     plane_faces)
            bakes[term] = b
            record_texrecon_times(times, term, b.seconds)
            t_prov = time.perf_counter()
            files, maps, covered = write_provenance(b, bdir / "all")
            times[f"provenance_{term}_s"] = round(time.perf_counter() - t_prov, 3)
            for k, f_ in enumerate(files):
                shutil.copyfile(bdir / "all" / "final" / b.obj["textures"][b.obj["materials"][k]], bdir / "all" / f_["albedo"])
            ratios = tp.area_ratios(V, F, b.cls)
            texel = tp.texel_ratios(maps, covered)
            per_cam = {names[c]: round(float(tp.face_areas(V, F)[b.cam == c].sum() / max(tp.face_areas(V, F).sum(), 1e-12)), 5)
                       for c in range(len(names))}
            extras = {
                "format": RESULT_FORMAT, "format_version": RESULT_VERSION, "jira_key": a.jira_key, "experiment_id": "EXP-01",
                "geometry_role": "VISUAL", "safety_use": False, "frame": frame,
                "axis_convention": glb_axis_convention(M),
                "unit": unit,
                "data_term": term,
                "surface_source": {"values": tp.SURFACE_VALUES, "reserved_not_emitted": tp.RESERVED_VALUES,
                                   "encoding": "무손실 8비트 단일 채널 PNG, 알베도 아틀라스와 같은 UV·해상도, 겹치면 신뢰가 낮은 값(미관측 < 보간 < 추정 평면 < 관측, 관측으로 승격 안 됨). 최근접 표본으로 읽는다. 0/1/2 만 읽는 쪽은 3 을 0 으로 읽는다",
                                   "observed_camera_encoding": "값 = camera_ids 번호 + 1, 0 = 관측 아님·모름",
                                   "camera_ids": names, "maps": files, "ratios_area": ratios, "ratios_texel": texel,
                                   "observed_area_by_camera": per_cam},
                "unobserved": {"material_rgb": list(NEUTRAL_RGB), "node": "unobserved_neutral",
                               "note": "형상은 관측 깊이(TSDF)이고 색 근거가 없다. 색을 지어내지 않았다"},
                "geometry_source": "OBSERVED_DEPTH_TSDF" + ("+HORIZONTAL_PLANE_FIT" if plane_faces is not None else ""),
                "geometry_cleanup": {"align_views": cfg.align_views, "plane_cleanup": plane_faces is not None,
                                     "estimated_plane_note": "ESTIMATED_PLANE 면은 색이 관측 픽셀이나 정점을 맞춘 수평 평면에 올렸다(관측 형상 그대로가 아니다)"},
            }
            t_exp = time.perf_counter()
            tris = export_glb(bdir / "all" / "textured.glb", V, F, b, M, extras)
            times[f"export_glb_{term}_s"] = round(time.perf_counter() - t_exp, 3)
            report["bakes"][term].update({"ratios_area": ratios, "ratios_texel": texel, "observed_area_by_camera": per_cam,
                                          "holes": b.holes, "atlases": len(files), "glb_triangles": tris,
                                          "glb_bytes": (bdir / "all" / "textured.glb").stat().st_size,
                                          "texrecon_s": b.seconds, "glb": (bdir / "all" / "textured.glb").relative_to(out).as_posix()})
        times[f"bake_{term}_s"] = round(time.perf_counter() - t0, 1)

    # 확인 (a) 한 카메라 빼고 굽기
    t0 = time.perf_counter()
    casts = {name: rend.cast(tex[name]["K"], np.asarray(poses[name], float), tex[name]["hw"]) for name in names}
    (out / "compare").mkdir()
    for term in cfg.loo_terms:
        rows = {}
        for ci, name in enumerate(names):
            ent = filtered[term][filtered[term]["view"] != ci]
            b = bake(tr, out / f"bake-{term}" / f"loo-{name}", out / "scene", out / "mesh" / "mesh.ply", V, F,
                     costs_by_term[term], ent, views, cfg, pairs, f"{term}-loo-{name}", scale, plane_faces)
            prim, bary = casts[name]
            rgb, src = shade_bake(prim, bary, b)
            photo = tex[name]["rgb"]
            person = tex[name]["person"] if tex[name]["person"] is not None else np.zeros(prim.shape, bool)
            usable = ~person & (prim >= 0)
            m = usable & np.isin(src, tp.OBSERVED_COLOR)
            rows[name] = {"psnr_db": tp.psnr(rgb, photo, m), "ssim": tp.ssim(rgb, photo, m),
                          "psnr_db_blur8": tp.psnr_blurred(rgb, photo, m, BLUR_SIGMA_PX),
                          "observed_pixels": int(m.sum()),
                          "coverage": round(float(m.sum()) / max(int(usable.sum()), 1), 4),
                          "interpolated_pixels": int((usable & (src == tp.INTERPOLATED)).sum())}
            save_jpg(out / "compare" / f"loo-{term}-{name}.jpg",
                     tile([photo, rgb, overlay(rgb, src), np.where(m[..., None], np.abs(rgb.astype(int) - photo).astype(np.uint8), 0)],
                          0.25, [f"{name} photo", f"LOO {term} render", "provenance", "|diff| observed"]))
        ok = [r for r in rows.values() if r["psnr_db"] is not None]
        report["loo"][term] = {"cameras": rows,
                               "psnr_db_mean": round(float(np.mean([r["psnr_db"] for r in ok])), 3) if ok else None,
                               "ssim_mean": round(float(np.mean([r["ssim"] for r in ok if r["ssim"] is not None])), 4) if ok else None,
                               "psnr_db_blur8_mean": round(float(np.mean([r["psnr_db_blur8"] for r in ok])), 3) if ok else None,
                               "coverage_mean": round(float(np.mean([r["coverage"] for r in rows.values()])), 4)}
    times["loo_s"] = round(time.perf_counter() - t0, 1)

    # 자기 시점 재현(기준선): 네 카메라로 구운 텍스처를 각 카메라 시점에서 그려, 그 카메라가 고른 면만 사진과 비교한다.
    # 굽기·재표본·이음매 보정의 손실 상한이다(빼고 굽기와의 차이 = 카메라 사이 정합 오차 몫)
    report["self_view"] = {}
    for term, b in bakes.items():
        rows = {}
        for ci, name in enumerate(names):
            prim, bary = casts[name]
            rgb, src = shade_bake(prim, bary, b)
            photo = tex[name]["rgb"]
            person = tex[name]["person"] if tex[name]["person"] is not None else np.zeros(prim.shape, bool)
            m = ~person & (prim >= 0) & np.isin(src, tp.OBSERVED_COLOR) & (b.cam[np.maximum(prim, 0)] == ci)
            rows[name] = {"psnr_db": tp.psnr(rgb, photo, m), "ssim": tp.ssim(rgb, photo, m), "pixels": int(m.sum())}
        ok = [r for r in rows.values() if r["psnr_db"] is not None]
        report["self_view"][term] = {"cameras": rows,
                                     "psnr_db_mean": round(float(np.mean([r["psnr_db"] for r in ok])), 3) if ok else None,
                                     "ssim_mean": round(float(np.mean([r["ssim"] for r in ok if r["ssim"] is not None])), 4) if ok else None}

    # 확인 (b) 기존 gridmesh.glb(정점 색)와 같은 시점 그림
    grid = a.baseline_glb if a.baseline_glb is not None else Path(a.ma_run) / "gridmesh.glb"
    primary = cfg.terms[0] if cfg.terms else None
    if grid.is_file() and primary:
        gV, gF, gC = load_gridmesh(grid)
        grend = Renderer(gV, gF)
        for name in names:
            K, T, hw = tex[name]["K"], np.asarray(cams[name]["cam2world"], float), tex[name]["hw"]
            gp, gb = grend.cast(K, T, hw)
            g_rgb = shade_vertex_colors(gp, gb, gF, gC)
            rgb, src = shade_bake(*casts[name], bakes[primary])
            save_jpg(out / "compare" / f"baseline-{name}.jpg",
                     tile([tex[name]["rgb"], g_rgb, rgb, overlay(rgb, src)], 0.25,
                          [f"{name} photo", "gridmesh.glb (vertex color)", f"textured ({primary})", "provenance"]))
        report["baseline"] = {"glb": str(grid), "sha256": sha256_file(grid)}
    else:
        warnings.append(f"기준 gridmesh.glb 가 없어 (b) 비교 그림을 만들지 않았다: {grid}")
    # 형상 확인(-314): 음영 형상·텍스처·출처(파랑 = 추정 평면)를 같은 시점에서. gridmesh 는 원래 자세, 이 메시는 정렬된 자세
    for name in names if primary else []:
        rgb, src = shade_bake(*casts[name], bakes[primary])
        save_jpg(out / "compare" / f"geometry-{name}.jpg",
                 tile([tex[name]["rgb"], shade_geometry(casts[name][0], V, F, poses[name]), rgb, overlay(rgb, src)], 0.25,
                      [f"{name} photo", "geometry (|cos|)", f"textured ({primary})", "provenance (blue = plane)"]))

    times["total_s"] = round(time.perf_counter() - t_all, 1)
    rec = {
        "format": RESULT_FORMAT, "format_version": RESULT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": a.jira_key, "experiment_id": "EXP-01", "status": "OK",
        "inputs": {"mapanything": ma_rec, "cameras_json_sha256": cameras_sha, "prepared_sha256": prepared["_sha256"],
                   "depth": depth_rec, "texture": tex_rec, "world_alignment": wa_rec,
                   "input_provenance": prepared.get("input_provenance")},
        "output_frame": {"frame": frame, "glb_axis_convention": glb_axis_convention(M), "note": ("World(+Z 상방, 바닥 Z=0), 보정 PROVISIONAL" if M is not None else
                                                  "World 고정 결과가 없거나 FAILED 라 MapAnything world(cam_A 기준, OpenCV 축) 그대로다. World·Scene 으로 쓰지 않는다")},
        "config": {**{k: v for k, v in asdict(cfg).items() if k != "hole"}, "hole": asdict(cfg.hole)},
        "texrecon": {**tr_rec, "calls": tr.calls},
        "threading": {"requested_num_threads": int(cfg.num_threads), "effective_num_threads": tr.num_threads},
        "geometry": geo_rec, "cleanup": cleanup_rec, **report, "times": times, "warnings": warnings,
        "metrics_note": "개발 교실 관측 기반 지표(DATASET_GT 아님). 한 카메라 빼고 굽기는 텍스처만 뺐고 형상(TSDF)은 네 카메라로 만들었다",
    }
    (out / "metrics.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    write_run_record(a, cfg, rec, out, started, prepared)
    return rec


def flat_metrics(rec: dict[str, Any]) -> dict[str, Any]:
    """run_record.metrics(평면 숫자·문자). 이름은 설계 문서 "Run 기록 지표"에 정의한 것만."""
    m: dict[str, Any] = {"mesh_faces": rec["geometry"]["faces"], "mesh_vertices": rec["geometry"]["vertices"],
                         "mesh_area_m2": rec["geometry"]["area_m2"], "output_frame": rec["output_frame"]["frame"],
                         "glb_axis_convention": rec["output_frame"]["glb_axis_convention"],
                         "mesh_faces_before_decimation": rec["geometry"].get("faces_before_decimation"),
                         "mesh_target_faces": rec["geometry"].get("target_faces"),
                         "mesh_decimated": rec["geometry"].get("decimated"),
                         "num_threads": (rec.get("threading") or {}).get("effective_num_threads")}
    for term, b in rec["bakes"].items():
        if "ratios_area" not in b:
            continue
        for k, v in b["ratios_area"].items():
            m[f"{term}_area_ratio_{k.lower()}"] = v
        for k, v in b["ratios_texel"].items():
            m[f"{term}_texel_ratio_{k.lower()}"] = v
        m[f"{term}_glb_triangles"] = b["glb_triangles"]
        m[f"{term}_interpolated_faces"] = b["holes"]["filled_faces"]
    for term, lo in rec["loo"].items():
        m[f"loo_{term}_psnr_db_mean"] = lo["psnr_db_mean"]
        m[f"loo_{term}_ssim_mean"] = lo["ssim_mean"]
        m[f"loo_{term}_coverage_mean"] = lo["coverage_mean"]
        m[f"loo_{term}_psnr_db_blur8_mean"] = lo["psnr_db_blur8_mean"]
        for cam, r in lo["cameras"].items():
            m[f"loo_{term}_{cam}_psnr_db"] = r["psnr_db"]
            m[f"loo_{term}_{cam}_ssim"] = r["ssim"]
    for term, sv in rec.get("self_view", {}).items():
        m[f"self_{term}_psnr_db_mean"] = sv["psnr_db_mean"]
        m[f"self_{term}_ssim_mean"] = sv["ssim_mean"]
    c = rec.get("cleanup") or {}
    for when in ("before", "after"):
        a = (c.get(f"cross_view_{when}") or {}).get("all") or {}
        if a.get("abs_rel_depth"):
            m[f"cross_view_{when}_abs_rel_p50"] = a["abs_rel_depth"]["p50"]
            m[f"cross_view_{when}_abs_rel_p90"] = a["abs_rel_depth"]["p90"]
            m[f"cross_view_{when}_within_1pct"] = a["within_1pct"]
        if a.get("point_to_plane_m"):
            m[f"cross_view_{when}_p2p_m_p50"] = a["point_to_plane_m"]["p50"]
            m[f"cross_view_{when}_p2p_m_p90"] = a["point_to_plane_m"]["p90"]
    if "alignment" in c:
        m["align_mode"] = c["alignment"]["mode"]
        m["align_reference"] = c["alignment"]["reference"]
        m["align_converged"] = c["alignment"]["converged"]
        m["align_iterations"] = c["alignment"]["iterations"]
        for cam, st in c["alignment"]["status"].items():
            m[f"align_{cam}_status"] = st
            t = c["alignment"]["totals"][cam]
            m[f"align_{cam}_scale"], m[f"align_{cam}_rot_deg"], m[f"align_{cam}_center_shift"] = t["scale"], t["rot_deg"], t["center_shift"]
    if "planes" in c:
        m["planes"] = c["planes"]["planes"]
        if "mesh_snap" in c["planes"]:
            m["plane_faces"] = c["planes"]["mesh_snap"]["plane_faces"]
    for k, r in (c.get("regions") or {}).items():
        if (r.get("layers") or {}).get("layer_spread_m"):
            m[f"region_{k}_layer_spread_m_p50"] = r["layers"]["layer_spread_m"]["p50"]
        sm = (r.get("flatness") or {}).get("summary")
        if sm:
            for key in ("rms_m", "within_1cm", "rough_area_ratio", "top_tilt_gt10_ratio", "top_tilt_mean_deg"):
                m[f"region_{k}_{key}"] = sm.get(key)
    m.update({k: v for k, v in rec["times"].items()})
    return m


def git_info() -> dict[str, Any]:
    def g(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=30)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    return {"git_commit": g("rev-parse", "HEAD") or "0000000", "branch": g("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
            "dirty_worktree": bool(g("status", "--porcelain"))}


def write_run_record(a: argparse.Namespace, cfg: Config, rec: dict[str, Any], out: Path, started: int,
                     prepared: dict[str, Any]) -> None:
    from simai_ai.providers.contract import schema_errors

    prov = dict(prepared.get("input_provenance") or {"frames": {"source": "FILE"}, "is_upper_bound": False})
    prov["calibration"] = {"source": "MODEL_ESTIMATE",
                           "calibration_version": f"mapanything-{rec['inputs']['mapanything'].get('run_id')}"}
    results = [{"kind": "REPORT", "uri": (out / "metrics.json").as_posix(), "sha256": sha256_file(out / "metrics.json")}]
    for term, b in rec["bakes"].items():
        if "glb" in b:
            p = out / b["glb"]
            results.append({"kind": "OTHER", "uri": p.as_posix(), "sha256": sha256_file(p)})
    for p in sorted((out / "compare").glob("*.jpg")):
        results.append({"kind": "RENDER", "uri": p.as_posix()})
    tr = rec["texrecon"]
    record = {
        "schema_version": RUN_RECORD_SCHEMA_VERSION, "run_id": a.run_id, "experiment_id": "EXP-01",
        "jira_key": a.jira_key, "owner": a.owner, **a.git,
        "env_hash": tr.get("image_id") or tr.get("sha256") or "unknown",
        "model_version": f"{MVS_TEXTURING} + open3d TSDF + MapAnything {rec['inputs']['mapanything'].get('model_version')}",
        "config": {"path": "ai/src/simai_ai/scene/texture_mesh.py", "sha256": sha256_file(Path(__file__)),
                   "overrides": rec["config"]},
        "input_provenance": prov,
        "gpu": {"model": "none (CPU, 노트북)", "driver": "n/a", "cuda": "n/a", "provider": "LOCAL"},
        "started_at": started, "finished_at": now_ms(), "status": "SUCCEEDED", "experiment_state": "ITERATE",
        "metrics": flat_metrics(rec), "results": results,
        "notes": ("개발 교실 관측 기반 지표이고 DATASET_GT·SIM 이 아니다(제품 성능 근거로 쓰기 전 리뷰). "
                  f"출력 좌표 {rec['output_frame']['frame']}. " + " ".join(rec["warnings"]))[:2000],
    }
    if rec["inputs"]["mapanything"].get("weights_hash"):
        record["weights_hash"] = rec["inputs"]["mapanything"]["weights_hash"]
    errors = schema_errors(record, "run_record")
    if errors:
        raise RuntimeError("Run 기록이 계약을 통과하지 못했다: " + "; ".join(errors[:5]))
    (out / "run_record.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ma-run", type=Path, required=True, help="run_mapanything.py 출력(cameras.json, raw.npz, metrics.json)")
    ap.add_argument("--prepared", type=Path, required=True, help="world_frame.cli prepare 출력 폴더(펴진 그림·K·사람 마스크)")
    ap.add_argument("--out", type=Path, required=True, help="새 결과 폴더(저장소 밖, 비어 있어야 한다)")
    ap.add_argument("--depth", type=Path, help="run_hybrid_depth predict 출력(혼합 깊이). 없으면 MapAnything raw.npz 깊이")
    ap.add_argument("--align", choices=["none", "scale", "scale_shift"], default="scale", help="--depth 를 MapAnything 깊이에 맞추는 방식")
    ap.add_argument("--world-alignment", type=Path, help="world_frame.cli fix 결과(status OK 면 glb 를 World 로)")
    ap.add_argument("--texture-images", type=Path, help="텍스처 소스 그림 폴더(<view>.jpg|png, <view>.mask.png). 기본: prepared 그림")
    ap.add_argument("--texture-empty-room", action="store_true", help="--texture-images 가 빈 교실 사진이라 마스크가 없다고 선언")
    ap.add_argument("--texrecon", default="docker:simai/texrecon:dev", help="docker:<이미지> 또는 texrecon 실행 파일")
    ap.add_argument("--terms", default="area,gmi", help="굽는 데이터 항(첫 항이 비교 그림의 기본)")
    ap.add_argument("--loo-terms", default="area,gmi", help="한 카메라 빼고 굽기를 할 데이터 항(빈 문자열이면 안 함)")
    ap.add_argument("--baseline-glb", type=Path, help="비교할 정점 색 메시(기본: <ma-run>/gridmesh.glb)")
    ap.add_argument("--voxel", type=float, default=VOXEL_M)
    ap.add_argument("--target-faces", type=int, default=TARGET_FACES)
    ap.add_argument("--edge-band-px", type=int, default=EDGE_BAND_PX)
    ap.add_argument("--hole-max-area", type=float, default=tp.HoleRule.max_area_m2)
    ap.add_argument("--hole-max-inradius", type=float, default=tp.HoleRule.max_inradius_m)
    ap.add_argument("--hole-max-inradius-px", type=float, default=tp.HoleRule.max_inradius_px)
    ap.add_argument("--num-threads", type=int, default=0, help="texrecon 스레드(1 이면 결정적, 0 = 전부)")
    ap.add_argument("--align-views", choices=["off", "sim3", "scale"], default="off",
                    help="TSDF 전 카메라 간 깊이 정렬(-314): sim3 = 카메라별 회전·이동·축척, scale = 깊이 배율만")
    ap.add_argument("--plane-cleanup", action="store_true", help="수평 평면 정리(-314, World 정렬 필요): 면은 ESTIMATED_PLANE")
    ap.add_argument("--check-region", action="append", default=[],
                    help="가구 윗면 확인 영역 이름=x0,x1,y0,y1,z0,z1 (World m, 여러 번). 입력 깊이 겹·메시 평평함을 metrics 에")
    ap.add_argument("--jira-key", default="S15P31A101-298", help="Run 기록·glb extras 의 Jira 키")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--owner", default=None, help="Run 기록 owner(기본 git user.name)")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    split = lambda s: tuple(x for x in s.split(",") if x)  # noqa: E731
    cfg = Config(voxel_m=a.voxel, target_faces=a.target_faces, edge_band_px=a.edge_band_px, align=a.align,
                 terms=split(a.terms), loo_terms=split(a.loo_terms), num_threads=a.num_threads,
                 hole=tp.HoleRule(a.hole_max_area, a.hole_max_inradius, a.hole_max_inradius_px),
                 align_views=a.align_views, plane_cleanup=a.plane_cleanup)
    for spec in a.check_region:
        name, _, vals = spec.partition("=")
        try:
            region = tuple(float(x) for x in vals.split(","))
        except ValueError:
            region = ()
        if not name or len(region) != 6:
            print(f"입력 오류: --check-region 은 이름=x0,x1,y0,y1,z0,z1: {spec}", file=sys.stderr)
            return EXIT_CONFIG
        cfg.check_regions[name] = region
    a.run_id = a.run_id or f"exp01-tex-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    if a.owner is None:
        r = subprocess.run(["git", "-C", str(REPO), "config", "user.name"], capture_output=True, text=True)
        a.owner = r.stdout.strip() or "unknown"
    bad = [t for t in cfg.terms + cfg.loo_terms if t not in ("area", "gmi")]
    try:
        if bad or not cfg.terms:
            raise ValueError(f"데이터 항은 area·gmi 만: {bad or '(비어 있음)'}")
        rec = run(a, cfg)
    except ValueError as e:
        print(f"입력 오류: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except TexreconError as e:
        print(f"texrecon: {e}", file=sys.stderr)
        return EXIT_TEXRECON
    except RuntimeError as e:
        print(f"결과 없음: {e}", file=sys.stderr)
        return EXIT_EMPTY
    _summary(rec)
    return EXIT_OK


def _summary(rec: dict[str, Any]) -> None:
    g = rec["geometry"]
    print(f"frame {rec['output_frame']['frame']}, mesh {g['faces']} faces / {g['vertices']} vertices, {rec['times']}")
    for term, b in rec["bakes"].items():
        if "ratios_area" in b:
            print(f"[{term}] area ratio {b['ratios_area']} texel {b['ratios_texel']} holes {b['holes']}")
    for term, lo in rec["loo"].items():
        print(f"[LOO {term}] psnr {lo['psnr_db_mean']} (blur8 {lo['psnr_db_blur8_mean']}) ssim {lo['ssim_mean']} coverage {lo['coverage_mean']} "
              + " ".join(f"{k}:{v['psnr_db']}/{v['ssim']}" for k, v in lo["cameras"].items()))
    for term, sv in rec.get("self_view", {}).items():
        print(f"[self {term}] psnr {sv['psnr_db_mean']} ssim {sv['ssim_mean']}")
    c = rec.get("cleanup") or {}
    for when in ("before", "after"):
        x = (c.get(f"cross_view_{when}") or {}).get("all")
        if x:
            print(f"[cross-view {when}] |Δd|/d {x['abs_rel_depth']} p2p m {x['point_to_plane_m']}")
    if "alignment" in c:
        print(f"[align] ref {c['alignment']['reference']} status {c['alignment']['status']} totals {c['alignment']['totals']}")
    if "planes" in c:
        print(f"[planes] {c['planes']['planes']} mesh {(c['planes'].get('mesh_snap') or {}).get('plane_faces')} faces")
    for k, r in (c.get("regions") or {}).items():
        print(f"[region {k}] layers {(r.get('layers') or {}).get('layer_spread_m')} flatness {(r.get('flatness') or {}).get('summary')}")
    if rec["warnings"]:
        print("warnings: " + " | ".join(rec["warnings"]))


if __name__ == "__main__":
    raise SystemExit(main())
