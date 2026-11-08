"""EXP-01 혼합 경로: MapAnything 자세·K 고정 + 원본 해상도 깊이 → 정합·융합·지표 (S15P31A101-283, S15P31A101-537).

설계는 docs/design/exp01-hybrid-depth.md. MapAnything(known-K, 모델 입력 518×294)의 자세(cam2world)는 그대로 두고, 깊이만
원본 해상도(펴진 2304×1296)에 가까운 모델로 다시 낸 뒤 카메라별로 MapAnything 깊이에 맞춰(축척, 선택적으로 이동) 한
점구름으로 합친다. 두 단계로 나눈다.

  predict  깊이 모델 어댑터만 돈다(GPU 세션). simai_ai 를 import 하지 않아 깊이 모델 전용 가상환경에서도 돈다.
           어댑터:
             - fake: MapAnything 깊이를 원본 해상도로 올린 것(노트북 경로 점검·기준선)
             - da3: Depth Anything 3 (자세·K 조건)
             - moge: MoGe-2 단안 + 보정 K
             - worldmirror2: Tencent HY-World-2.0 (WorldMirror 2.0, 다중 시점 자세·K·선택적 사전 깊이 조건)
             - worldmirror1: Tencent HunyuanWorld-Mirror 1.0 (다중 시점 자세·K·선택적 사전 깊이 조건)
             - pi3x: Pi3X (다중 시점 자세·K·선택적 사전 깊이 조건)
           출력 폴더(`simai.hybrid_depth`): depth.json + <view>.depth.npz(원본 해상도 z 깊이).
  fuse     정합·사람 마스크·융합·지표(노트북 CPU 가능, simai_ai 사용). 출력: hybrid_metrics.json, fused.ply,
           (trimesh 가 있으면) pointcloud.glb·gridmesh.glb.
  all      predict 뒤 fuse(노트북에서 fake 어댑터로 끝까지 시험).

    # 노트북(가짜 어댑터, 마스크·색에 OpenCV 필요)
    uv run --project ai --with opencv-python-headless python ai/experiments/static_scene/run_hybrid_depth.py all \\
        --ma-run <MapAnything Run 폴더> --prepared <prepare 출력 폴더> --adapter fake --out <결과 폴더>
    # GPU 세션(깊이만) → 노트북(fuse)
    python ai/experiments/static_scene/run_hybrid_depth.py predict --ma-run … --prepared … \\
        --adapter worldmirror2 --model <가중치 폴더 또는 HF ID> [--prior-depth] [--no-pose] --out <깊이 폴더>
    uv run --project ai --with opencv-python-headless python ai/experiments/static_scene/run_hybrid_depth.py fuse \\
        --ma-run … --prepared … --depth <깊이 폴더> --out <결과 폴더> [--world-alignment <world_frame.cli fix 결과>]

좌표: 깊이·자세는 MapAnything world(OpenCV cam2world, 모델 추정 m)에서 다룬다. `--world-alignment`(status OK)가 있으면 출력
점·메시를 World(m, 오른손, +Z 상방, 바닥 Z=0)로 옮기고, 없거나 FAILED 면 MapAnything world 그대로 두고 그렇게 적는다(World 로
쓰지 않는다). glb 정점은 출력 좌표 그대로다(Three.js (x, z, −y) 변환은 뷰어 어댑터 한 곳, AGENTS 3).
이 결과는 보기용 Visual Geometry 다. Tier 0 위치·판정과 바닥·OBB 등 Operational Geometry 에 넣지 않는다(명세 5.3).

종료 코드: 0 성공, 2 입력·연결 오류(다른 입력으로 돈 결과 섞기 등), 3 결과 없음(정합된 카메라·점 없음).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

DEPTH_FORMAT = "simai.hybrid_depth"
DEPTH_VERSION = "0.1.0"
RESULT_FORMAT = "simai.hybrid_depth_result"
RESULT_VERSION = "0.1.0"
PREPARED_FORMAT = "simai.mapanything_inputs"

EXIT_OK, EXIT_CONFIG, EXIT_EMPTY = 0, 2, 3

# 정한 수치(근거는 설계 문서 "정한 수치")
EDGE_TOL = 0.05  # 보간할 네 이웃 깊이 폭(상대). 넘으면 가장 가까운 이웃 값(불연속을 가로질러 섞지 않는다)
REG_TOL = 0.05  # 정합 인라이어: |d_ref − s·d − t| ≤ 5%·d_ref (교실 깊이 1~12 m 에서 5~60 cm)
REG_MIN_PAIRS = 500  # 정합 표본이 이보다 적으면 그 카메라는 정합 실패(모르면 비운다)
REG_MIN_INLIER = 0.30  # 인라이어 비율이 이보다 낮으면 정합 실패
SUPPORT_TOL = 0.03  # 융합: 다른 카메라 깊이와 3% 안이면 지지
CONFLICT_TOL = 0.05  # 융합: 다른 카메라가 본 표면보다 5% 넘게 앞(빈 공간)이면 충돌
FLYING_JUMP = 0.10  # 가장자리: 3×3 이웃 깊이 폭이 10% 넘으면 불연속 주변
FLYING_GAP = 0.02  # 가장자리: 이웃 최소·최대에서 둘 다 2% 넘게 떨어진 중간값 = 떠 있는 점

DEFAULT_MODEL_PATHS: dict[str, str] = {
    "worldmirror2": "tencent/HY-World-2.0",
    "worldmirror1": "tencent/HunyuanWorld-Mirror",
    "pi3x": "yyfz233/Pi3X",
}

DEFAULT_PROCESS_RES: dict[str, int] = {
    "da3": 504,
    "worldmirror2": 952,
    "worldmirror1": 518,
    "pi3x": 672,
}


def _load_intrinsics_check() -> Any:
    """Import sibling `intrinsics_check.py` without requiring `simai_ai`."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import intrinsics_check  # type: ignore[import-not-found]

        return intrinsics_check
    except ImportError:
        from simai_ai.scene import intrinsics_check as ic_mod

        return ic_mod


def _ensure_simai_ai_on_path() -> None:
    """Ensure `ai/src` is on `sys.path` when running `fuse` or `floor_metrics`."""
    p2 = Path(__file__).resolve().parents[2]
    for cand in (p2, p2 / "src"):
        if (cand / "simai_ai").is_dir() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
            break


# --------------------------------------------------------------------------- 공통


def sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def stats(x: np.ndarray, nd: int = 4) -> dict[str, float]:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if not len(x):
        return {}
    return {
        "n": int(len(x)),
        "p5": round(float(np.percentile(x, 5)), nd),
        "p50": round(float(np.median(x)), nd),
        "p95": round(float(np.percentile(x, 95)), nd),
        "mean": round(float(x.mean()), nd),
    }


def load_prepared(path: Path) -> dict[str, Any]:
    file = Path(path) / "prepared.json" if Path(path).is_dir() else Path(path)
    raw = file.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    if doc.get("format") != PREPARED_FORMAT:
        raise ValueError(f"{file}: MapAnything 입력 폴더 형식이 아니다: {doc.get('format')!r}")
    doc["_dir"] = str(file.parent)
    doc["_sha256"] = sha256_bytes(raw)
    return doc


def check_run_matches_prepared(ma_run: Path, prepared: dict[str, Any]) -> dict[str, Any]:
    """MapAnything Run 이 이 입력 폴더로 돈 것인지(metrics.json 의 prepared_sha256). 다르면 ValueError."""
    m_path = Path(ma_run) / "metrics.json"
    if not m_path.is_file():
        raise ValueError(f"{m_path} 가 없다(어느 입력으로 돈 결과인지 확인할 수 없다)")
    m = json.loads(m_path.read_text(encoding="utf-8"))
    if m.get("result") != "SUCCESS":
        raise ValueError(f"MapAnything Run 결과가 SUCCESS 가 아니다: {m.get('result')!r}")
    if m.get("prepared_sha256") != prepared["_sha256"]:
        raise ValueError(
            f"MapAnything Run 의 prepared_sha256({m.get('prepared_sha256')}) 이 입력 폴더({prepared['_sha256']})와 다르다"
        )
    return {
        k: m.get(k)
        for k in (
            "run_id",
            "model_version",
            "weights_hash",
            "input_hash",
            "prepared_sha256",
            "model_input_hw",
        )
    }


def read_cameras(ma_run: Path) -> tuple[dict[str, dict[str, Any]], str]:
    raw = (Path(ma_run) / "cameras.json").read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    conv = str(doc.get("convention", ""))
    if "cam2world" not in conv or "opencv" not in conv.lower():
        raise ValueError(f"cameras.json convention 이 cam2world·OpenCV 가 아니다: {conv!r}")
    return {v["name"]: v for v in doc["views"]}, sha256_bytes(raw)


def read_calibration_cameras(calib_path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    """Read `calibration.json` and return `({camera_id: {"name": camera_id, "cam2world": 4x4_list}}, sha256)`."""
    raw = Path(calib_path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for c in doc.get("cameras", []):
        cid = str(c["camera_id"])
        if "extrinsics" in c:
            ext = c["extrinsics"]
            R_raw = np.asarray(ext["r_matrix"], dtype=np.float64).reshape(3, 3)
            t_raw = np.asarray(ext["t_xyz_m"], dtype=np.float64).reshape(3)
            conv = str(ext.get("convention", "world_to_cam")).lower()
        else:
            R_raw = np.asarray(c["R"], dtype=np.float64).reshape(3, 3)
            t_raw = np.asarray(c["t"], dtype=np.float64).reshape(3)
            conv = str(c.get("convention", "world_to_cam")).lower()
        if conv == "cam_to_world":
            R_c2w, t_c2w = R_raw, t_raw
        else:
            R_c2w = R_raw.T
            t_c2w = -R_c2w @ t_raw
        c2w = np.eye(4, dtype=np.float64)
        c2w[:3, :3] = R_c2w
        c2w[:3, 3] = t_c2w
        out[cid] = {"name": cid, "cam2world": c2w.tolist()}
    return out, sha256_bytes(raw)


# --------------------------------------------------------------------------- 깊이 다시 표본(같은 카메라, 다른 K)


def sample_depth(
    src: np.ndarray,
    valid: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    edge_tol: float = EDGE_TOL,
) -> tuple[np.ndarray, np.ndarray]:
    """src(H, W) 의 소수 픽셀 좌표 (x, y)(정수 = 픽셀 중심) 값(역깊이 쌍선형). 네 이웃이 모두 유효해야 하고, 네 값의 폭이
    edge_tol(상대)을 넘으면(깊이 불연속) 섞지 않고 가장 가까운 이웃 값을 쓴다. 반환 (값, 유효)."""
    h, w = src.shape
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    out = np.full(x.shape, np.nan, dtype=np.float32)
    inside = (x >= 0) & (y >= 0) & (x <= w - 1) & (y <= h - 1)
    xi, yi = x[inside], y[inside]
    x0 = np.minimum(np.floor(xi).astype(np.int64), w - 2 if w > 1 else 0)
    y0 = np.minimum(np.floor(yi).astype(np.int64), h - 2 if h > 1 else 0)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (xi - x0).astype(np.float32), (yi - y0).astype(np.float32)
    d00, d01, d10, d11 = src[y0, x0], src[y0, x1], src[y1, x0], src[y1, x1]
    ok = valid[y0, x0] & valid[y0, x1] & valid[y1, x0] & valid[y1, x1]
    lo = np.minimum(np.minimum(d00, d01), np.minimum(d10, d11))
    hi = np.maximum(np.maximum(d00, d01), np.maximum(d10, d11))
    with np.errstate(divide="ignore", invalid="ignore"):
        smooth = (hi - lo) <= edge_tol * lo
    # 역깊이(1/z)를 보간한다: 평면에서 1/z 는 픽셀 좌표의 1차 함수라 평면을 휘지 않는다(z 를 보간하면 휜다)
    with np.errstate(divide="ignore", invalid="ignore"):
        bil = 1.0 / (
            (1 - fx) * (1 - fy) / d00
            + fx * (1 - fy) / d01
            + (1 - fx) * fy / d10
            + fx * fy / d11
        )
    # 불연속: 가장 가까운 이웃(그 이웃만 유효하면 된다)
    xn = np.where(fx < 0.5, x0, x1)
    yn = np.where(fy < 0.5, y0, y1)
    near = src[yn, xn]
    near_ok = valid[yn, xn]
    val = np.where(ok & smooth, bil, near)
    good = (ok & smooth) | (~(ok & smooth) & near_ok)
    good &= np.isfinite(val) & (val > 0)
    res = np.full(xi.shape, np.nan, dtype=np.float32)
    res[good] = val[good]
    out[inside] = res
    v = np.zeros(x.shape, bool)
    v[inside] = good
    return out, v


def neighbor_min(src: np.ndarray, valid: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """(x, y) 를 둘러싼 네 픽셀 중 유효한 것의 최소 깊이(없으면 inf). 좌표는 영상 안이어야 한다."""
    h, w = src.shape
    x0 = np.clip(np.floor(x).astype(np.int64), 0, max(w - 2, 0))
    y0 = np.clip(np.floor(y).astype(np.int64), 0, max(h - 2, 0))
    lo = np.full(len(x0), np.inf, np.float32)
    for dy in (0, 1):
        for dx in (0, 1):
            yy, xx = np.minimum(y0 + dy, h - 1), np.minimum(x0 + dx, w - 1)
            lo = np.where(valid[yy, xx], np.fmin(lo, src[yy, xx]), lo)
    return lo


def resample_depth(
    src: np.ndarray,
    src_valid: np.ndarray,
    K_src: np.ndarray,
    K_dst: np.ndarray,
    dst_hw: tuple[int, int],
    edge_tol: float = EDGE_TOL,
) -> tuple[np.ndarray, np.ndarray]:
    """같은 카메라(같은 중심·회전)의 z 깊이를 K_src 격자에서 K_dst 격자(dst_hw)로 옮긴다. 광선이 같아 z 가 같다."""
    H, W = int(dst_hw[0]), int(dst_hw[1])
    A = np.asarray(K_src, float) @ np.linalg.inv(np.asarray(K_dst, float))
    u = np.arange(W, dtype=float)[None, :]
    v = np.arange(H, dtype=float)[:, None]
    x = A[0, 0] * u + A[0, 1] * v + A[0, 2]
    y = A[1, 0] * u + A[1, 1] * v + A[1, 2]
    x, y = np.broadcast_to(x, (H, W)), np.broadcast_to(y, (H, W))
    return sample_depth(src.astype(np.float32), src_valid.astype(bool), x, y, edge_tol)


# --------------------------------------------------------------------------- 어댑터(predict)


@dataclass
class PredictView:
    name: str
    image_path: Path
    image_hw: tuple[int, int]
    K: np.ndarray  # 원본(펴진) 해상도
    cam2world: np.ndarray  # MapAnything world, OpenCV


@dataclass
class DepthOut:
    depth: np.ndarray  # (H, W) 원본 해상도 z 깊이, 무효 NaN
    valid: np.ndarray
    conf: np.ndarray | None
    native_hw: tuple[int, int]  # 모델이 실제로 깊이를 낸 해상도(올리기 전)
    meta: dict[str, Any] = field(default_factory=dict)


class FakeMapAnythingAdapter:
    """가짜 어댑터: MapAnything 깊이(모델 해상도)를 원본 해상도로 올린다. 정보는 늘지 않는다(기준선·경로 점검용)."""

    name = "fake"

    def __init__(
        self,
        ma_run: Path,
        conf_percentile: float = 10.0,
        scale: dict[str, float] | None = None,
    ) -> None:
        self.raw = np.load(Path(ma_run) / "raw.npz")
        self.conf_percentile = conf_percentile
        self.scale = scale or {}  # 시험용: 카메라별로 깊이에 곱한다(정합이 되돌리는지)

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "model_version": "FAKE_UPSAMPLED_MAPANYTHING",
            "weights_hash": None,
            "pose_conditioned": True,
            "metric": "MAPANYTHING_UNITS",
            "conf_percentile": self.conf_percentile,
            "test_scale": self.scale or None,
            "note": "MapAnything 깊이를 올린 것. 깊이 모델 성능이 아니라 정합·융합·지표 경로 점검과 기준선이다",
        }

    def predict(self, views: list[PredictView]) -> list[DepthOut]:
        out = []
        for v in views:
            t0 = time.perf_counter()
            d = self.raw[f"{v.name}__depth_z"].astype(np.float32)
            K = self.raw[f"{v.name}__intrinsics"].astype(float)
            m = self.raw[f"{v.name}__mask"].astype(bool) & np.isfinite(d) & (d > 0)
            conf = (
                self.raw[f"{v.name}__conf"].astype(np.float32)
                if f"{v.name}__conf" in self.raw
                else None
            )
            if conf is not None and m.any() and self.conf_percentile > 0:
                m &= conf >= np.percentile(conf[m], self.conf_percentile)
            d = d * float(self.scale.get(v.name, 1.0))
            depth, valid = resample_depth(d, m, K, v.K, v.image_hw)
            out.append(
                DepthOut(
                    depth,
                    valid,
                    None,
                    tuple(d.shape),
                    {"infer_ms": round((time.perf_counter() - t0) * 1000, 1)},
                )
            )
        return out


class DepthAnything3Adapter:
    """Depth Anything 3 자세 조건 추론(GPU 세션 전용). 네 장을 한 번에 넣고 펴진 K 와 MapAnything 자세(world→camera)를 준다.

    공식 `DepthAnything3.inference`(ByteDance-Seed/Depth-Anything-3 @3d835ec `src/depth_anything_3/api.py`)를 직접 부른다
    (map-anything 의 DA3Wrapper 는 forward 를 바로 불러 입력 자세 축척 맞춤을 하지 않는다, 설계 문서 "조사").
    공식 동작: 자세를 넣어도 모델은 자세를 다시 예측하고, 예측 자세를 입력 자세에 Umeyama 로 맞춘다.
    `align_to_input_ext_scale=True` 면 출력 자세 = 입력 자세, 깊이 /= 그 축척(깊이가 입력 자세 단위 = MapAnything 단위를 따른다),
    False 면 맞춘 예측 자세를 돌려준다. 처음(데움) 호출을 False 로 돌려 예측 자세와 입력 자세의 차이를 기록하고, 둘째(기록)
    호출을 True 로 돌려 그 깊이를 쓴다. 깊이는 처리 해상도(14 배수)로 나오므로 출력 K 로 원본 해상도에 옮긴다.
    """

    name = "da3"

    def __init__(
        self,
        model: str,
        process_res: int = 504,
        process_res_method: str = "upper_bound_resize",
        use_pose: bool = True,
        use_ray_pose: bool = False,
    ) -> None:
        self.model_path = model
        self.process_res = process_res
        self.process_res_method = process_res_method
        self.use_pose = use_pose
        self.use_ray_pose = use_ray_pose
        self._perf: dict[str, Any] = {}

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "adapter": self.name,
            "model_path": str(self.model_path),
            "process_res": self.process_res,
            "process_res_method": self.process_res_method,
            "pose_conditioned": self.use_pose,
            "use_ray_pose": self.use_ray_pose,
            "metric": "INPUT_POSE_UNITS" if self.use_pose else "MODEL_METRIC_OR_RELATIVE",
        }
        try:
            from importlib.metadata import version

            info["package_version"] = version("depth_anything_3")
        except Exception:  # noqa: BLE001
            info["package_version"] = None
        info["commit"] = os.environ.get("SIMAI_DA3_COMMIT") or None
        info["weights_hash"] = _weights_hash(Path(self.model_path))
        return info | self._perf

    def predict(self, views: list[PredictView]) -> list[DepthOut]:
        intrinsics_check = _load_intrinsics_check()
        import torch  # type: ignore[import-not-found]
        from depth_anything_3.api import DepthAnything3  # type: ignore[import-not-found]

        dev = torch.device("cuda")
        t0 = time.perf_counter()
        model = DepthAnything3.from_pretrained(self.model_path).to(dev).eval()
        torch.cuda.synchronize()
        load_s = time.perf_counter() - t0
        images = [str(v.image_path) for v in views]
        kw: dict[str, Any] = {
            "process_res": self.process_res,
            "process_res_method": self.process_res_method,
            "use_ray_pose": self.use_ray_pose,
        }
        w2c = np.stack([np.linalg.inv(v.cam2world) for v in views]).astype(
            np.float32
        )  # OpenCV world→camera (N, 4, 4)
        if self.use_pose:  # extrinsics 만 주고 intrinsics 를 빼면 공식 코드가 실패한다(둘 다 준다)
            kw |= {
                "extrinsics": w2c,
                "intrinsics": np.stack([v.K for v in views]).astype(np.float32),
            }
        times, pose_check = [], None
        for align in (False, True) if self.use_pose else (None, None):
            call = kw | ({} if align is None else {"align_to_input_ext_scale": align})
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
            t1 = time.perf_counter()
            with torch.no_grad():
                pred = model.inference(images, **call)
            torch.cuda.synchronize()
            times.append(round((time.perf_counter() - t1) * 1000, 1))
            if align is False:
                pose_check = pose_difference(w2c, np.asarray(pred.extrinsics, float))
        self._perf = {
            "load_s": round(load_s, 1),
            "infer_ms": times,
            "peak_alloc_mb": round(torch.cuda.max_memory_allocated() / 2**20, 1),
            "pose_check_aligned_pred_vs_input": pose_check,
            "is_metric": _plain(getattr(pred, "is_metric", None)),
            "scale_factor": _plain(getattr(pred, "scale_factor", None)),
        }
        depth = np.asarray(pred.depth, dtype=np.float32)  # (N, h, w) 처리 해상도
        conf = getattr(pred, "conf", None)
        conf = None if conf is None else np.asarray(conf, dtype=np.float32)
        Kp = np.asarray(pred.intrinsics, dtype=float)
        out, kv = [], []
        for i, v in enumerate(views):
            d = depth[i]
            m = np.isfinite(d) & (d > 0)
            Kd, k_note = k_for_depth_grid(Kp[i], d.shape, v.image_hw)
            # 출력 K 검사(S15P31A101-312): 처리 해상도는 축마다 따로 늘인다(14 배수). 자세 없이(--no-pose)는 예측 K 와 펴진 K 비교
            kv.append(
                {
                    "name": v.name,
                    "output": Kd,
                    "expected": intrinsics_check.expected_axis_resize_k(
                        v.K, v.image_hw[::-1], d.shape[::-1]
                    ),
                }
            )
            full, ok = resample_depth(d, m, Kd, v.K, v.image_hw)
            c = None
            if conf is not None:
                c, _ = resample_depth(conf[i], m, Kd, v.K, v.image_hw, edge_tol=np.inf)
            out.append(
                DepthOut(
                    full,
                    ok,
                    c,
                    tuple(d.shape),
                    {"intrinsics_depth_grid": Kd.tolist(), "intrinsics_note": k_note},
                )
            )
        self._perf["intrinsics_check"] = intrinsics_check.check(kv)
        warn = intrinsics_check.warning(self._perf["intrinsics_check"])
        if warn:
            self._perf["warnings"] = [warn]
            print(f"경고: {warn}", file=sys.stderr, flush=True)
        return out


def _plain(x: Any) -> Any:
    """JSON 에 넣을 스칼라(텐서·numpy 값이면 float)."""
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    try:
        return float(np.asarray(x).reshape(-1)[0])
    except Exception:  # noqa: BLE001
        return str(x)


def k_for_depth_grid(
    K: np.ndarray, depth_hw: tuple[int, int], image_hw: tuple[int, int]
) -> tuple[np.ndarray, str]:
    """모델이 돌려준 K 가 깊이 격자(처리 해상도) 기준인지 원본 기준인지 주점으로 가린다. 원본 기준이면(주점이 깊이 격자 밖)
    크기 비로 옮긴다(픽셀 중심 정수 규약: x' = (x + 0.5)·s − 0.5). GPU 첫 실행에서 어느 쪽인지 기록으로 확인한다."""
    h, w = depth_hw
    if 0 <= K[0, 2] <= w and 0 <= K[1, 2] <= h:
        return np.asarray(K, float), "DEPTH_GRID"
    sx, sy = w / image_hw[1], h / image_hw[0]
    Kd = np.asarray(K, float).copy()
    Kd[0, 0] *= sx
    Kd[1, 1] *= sy
    Kd[0, 2] = (Kd[0, 2] + 0.5) * sx - 0.5
    Kd[1, 2] = (Kd[1, 2] + 0.5) * sy - 0.5
    return Kd, "SCALED_FROM_IMAGE"


def pose_difference(w2c_in: np.ndarray, w2c_out: np.ndarray) -> list[dict[str, float]]:
    """입력 자세와 (입력에 맞춘) 예측 자세의 차이: 회전 각(도), 중심 거리 / 카메라 간 거리 중앙값."""

    def center(E):
        return -E[:3, :3].T @ E[:3, 3]

    cin = np.array([center(E) for E in w2c_in])
    span = (
        np.median(np.linalg.norm(cin[:, None] - cin[None], axis=-1)[np.triu_indices(len(cin), 1)])
        if len(cin) > 1
        else 1.0
    )
    out = []
    for Ei, Eo in zip(w2c_in, w2c_out):
        dR = Ei[:3, :3] @ Eo[:3, :3].T
        ang = math.degrees(math.acos(float(np.clip((np.trace(dR) - 1) / 2, -1, 1))))
        out.append(
            {
                "rot_deg": round(ang, 3),
                "center_rel": round(float(np.linalg.norm(center(Ei) - center(Eo)) / span), 4),
            }
        )
    return out


class MoGeAdapter:
    """MoGe-2 단안(GPU 세션 전용). 카메라마다 따로, 펴진 K 의 가로 화각을 fov_x 로 준다. 자세는 쓰지 않는다(정합이 축척을 맞춘다)."""

    name = "moge"

    def __init__(self, model: str, resolution_level: int = 9) -> None:
        self.model_path = model
        self.resolution_level = resolution_level
        self._perf: dict[str, Any] = {}

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "model_path": str(self.model_path),
            "pose_conditioned": False,
            "resolution_level": self.resolution_level,
            "metric": "MODEL_METRIC",
            "weights_hash": _weights_hash(Path(self.model_path)),
        } | self._perf

    def predict(self, views: list[PredictView]) -> list[DepthOut]:
        import cv2  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]
        from moge.model.v2 import MoGeModel  # type: ignore[import-not-found]

        dev = torch.device("cuda")
        model = MoGeModel.from_pretrained(self.model_path).to(dev).eval()
        out, times = [], []
        torch.cuda.reset_peak_memory_stats()
        for v in views:
            img = cv2.cvtColor(cv2.imread(str(v.image_path)), cv2.COLOR_BGR2RGB)
            t = torch.tensor(img / 255.0, dtype=torch.float32, device=dev).permute(2, 0, 1)
            fov_x = math.degrees(2 * math.atan(v.image_hw[1] / 2 / v.K[0, 0]))
            torch.cuda.synchronize()
            t1 = time.perf_counter()
            with torch.no_grad():
                r = model.infer(t, fov_x=fov_x, resolution_level=self.resolution_level)
            torch.cuda.synchronize()
            times.append(round((time.perf_counter() - t1) * 1000, 1))
            d = r["depth"].float().cpu().numpy()
            m = r["mask"].cpu().numpy().astype(bool) & np.isfinite(d) & (d > 0)
            Kn = r["intrinsics"].float().cpu().numpy()  # 정규화 K
            h, w = d.shape
            Km = Kn * np.array([[w, 1, w], [1, h, h], [1, 1, 1]])
            Km[0, 2] -= 0.5  # 정규화 주점(0~1, 가장자리 기준) → 픽셀 중심 정수 규약
            Km[1, 2] -= 0.5
            full, ok = resample_depth(d, m, Km, v.K, v.image_hw)
            out.append(DepthOut(full, ok, None, (h, w), {"fov_x_deg": round(fov_x, 3)}))
        self._perf = {
            "infer_ms": times,
            "peak_alloc_mb": round(torch.cuda.max_memory_allocated() / 2**20, 1),
        }
        return out


# --------------------------------------------------------------------------- WorldMirror 2.0 / 1.0 & Pi3X 다중 시점 어댑터 (S15P31A101-537)


def compute_patch_aligned_hw(
    orig_hw: tuple[int, int],
    process_res: int,
    patch_size: int = 14,
) -> tuple[int, int]:
    """원본 `(H, W)` 의 긴 변을 `process_res` 이하 `patch_size`(14) 배수로 맞추고 짧은 변을 비율 유지 14 배수로 반올림한다."""
    h0, w0 = int(orig_hw[0]), int(orig_hw[1])
    if h0 <= 0 or w0 <= 0 or patch_size <= 0:
        raise ValueError(f"잘못된 해상도 orig_hw={orig_hw} 또는 patch_size={patch_size}")
    target_long = max(patch_size, (int(process_res) // patch_size) * patch_size)
    if w0 >= h0:
        w_proc = target_long
        scale = float(w_proc) / float(w0)
        h_proc = max(patch_size, int(round((h0 * scale) / float(patch_size))) * patch_size)
    else:
        h_proc = target_long
        scale = float(h_proc) / float(h0)
        w_proc = max(patch_size, int(round((w0 * scale) / float(patch_size))) * patch_size)
    return (h_proc, w_proc)


def normalize_c2w_relative_to_first(c2ws: np.ndarray) -> np.ndarray:
    """0번 카메라 기준 상대 c2w 변환 (`inv(c2w[0]) @ c2w[i]`). WorldMirror `pipeline.py:612` 및 Pi3X `pi3x.py:339` 규약."""
    arr = np.asarray(c2ws, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[1:] != (4, 4) or arr.shape[0] < 1:
        raise ValueError(f"c2ws shape 은 (N, 4, 4) 이어야 한다: {arr.shape}")
    first_inv = np.linalg.inv(arr[0])
    return np.einsum("ij,njk->nik", first_inv, arr)


def load_ma_prior_depths_for_grid(
    ma_run: Path,
    views: list[PredictView],
    proc_hw: tuple[int, int],
    k_proc_center: np.ndarray,
    *,
    conf_percentile: float = 10.0,
) -> np.ndarray:
    """MapAnything `raw.npz` 의 카메라별 `depth_z` 를 모델 처리 격자 `(proc_hw, k_proc_center)` 로 옮긴다.

    유효하지 않은 픽셀은 `0.0` 으로 채운다 (WorldMirror `normalize_depth` 및 Pi3X `encode` 의 무효 깊이 규약).
    반환 shape: `(N, H_proc, W_proc)` float32.
    """
    raw = np.load(Path(ma_run) / "raw.npz")
    prior_list: list[np.ndarray] = []
    for i, v in enumerate(views):
        d_key = f"{v.name}__depth_z"
        k_key = f"{v.name}__intrinsics"
        m_key = f"{v.name}__mask"
        if d_key not in raw or k_key not in raw:
            raise ValueError(f"{ma_run / 'raw.npz'} 에 {d_key} 또는 {k_key} 가 없다")
        d = raw[d_key].astype(np.float32)
        K_ma = raw[k_key].astype(float)
        m = raw[m_key].astype(bool) & np.isfinite(d) & (d > 0) if m_key in raw else (np.isfinite(d) & (d > 0))
        conf_key = f"{v.name}__conf"
        if conf_key in raw and m.any() and conf_percentile > 0:
            conf = raw[conf_key].astype(np.float32)
            m &= conf >= np.percentile(conf[m], conf_percentile)
        d_proc, ok_proc = resample_depth(d, m, K_ma, k_proc_center[i], proc_hw)
        prior_list.append(np.where(ok_proc & np.isfinite(d_proc) & (d_proc > 0), d_proc, 0.0).astype(np.float32))
    return np.stack(prior_list, axis=0).astype(np.float32)


def recover_pinhole_k_from_local_points(local_points: np.ndarray) -> np.ndarray:
    """Pi3X 출력 `local_points` (`(H, W, 3)`, 카메라 좌표) 로부터 픽셀 중심 정수 규약(`u = fx*(X/Z)+cx`) `3x3` K 복원."""
    pts = np.asarray(local_points, dtype=np.float64)
    if pts.ndim != 3 or pts.shape[2] != 3:
        raise ValueError(f"local_points shape 은 (H, W, 3) 이어야 한다: {pts.shape}")
    h, w, _ = pts.shape
    z = pts[..., 2]
    valid = np.isfinite(z) & (z > 1e-6) & np.isfinite(pts[..., 0]) & np.isfinite(pts[..., 1])
    if int(valid.sum()) < 16:
        f = float(max(h, w))
        return np.array([[f, 0.0, (w - 1) * 0.5], [0.0, f, (h - 1) * 0.5], [0.0, 0.0, 1.0]])
    v_grid, u_grid = np.mgrid[0:h, 0:w]
    xz = (pts[..., 0] / z)[valid]
    yz = (pts[..., 1] / z)[valid]
    u_val = u_grid[valid].astype(np.float64)
    v_val = v_grid[valid].astype(np.float64)
    ax = np.column_stack([xz, np.ones_like(xz)])
    ay = np.column_stack([yz, np.ones_like(yz)])
    fx, cx = (float(x) for x in np.linalg.lstsq(ax, u_val, rcond=None)[0])
    fy, cy = (float(x) for x in np.linalg.lstsq(ay, v_val, rcond=None)[0])
    if fx <= 0 or fy <= 0:
        f = float(max(h, w))
        return np.array([[f, 0.0, (w - 1) * 0.5], [0.0, f, (h - 1) * 0.5], [0.0, 0.0, 1.0]])
    return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float64)


def align_predicted_c2w_to_input_w2c(c2w_in: np.ndarray, c2w_pred: np.ndarray) -> np.ndarray:
    """모델이 예측한 상대/정규화 스케일의 `c2w_pred` `(N, 4, 4)` 를 0번 카메라 기준 + 카메라 간 거리 중앙값으로
    `c2w_in` 좌표계에 맞춘 뒤 OpenCV `w2c` `(N, 4, 4)` 로 반환한다 (`pose_difference` 진단용)."""
    cin = np.asarray(c2w_in, dtype=np.float64)
    cpred = np.asarray(c2w_pred, dtype=np.float64)
    rel_pred = normalize_c2w_relative_to_first(cpred)
    rel_in = normalize_c2w_relative_to_first(cin)
    if len(cin) > 1:
        d_in = np.linalg.norm(rel_in[1:, :3, 3], axis=-1)
        d_pred = np.linalg.norm(rel_pred[1:, :3, 3], axis=-1)
        valid = d_pred > 1e-7
        scale = float(np.median(d_in[valid] / d_pred[valid])) if valid.any() else 1.0
    else:
        scale = 1.0
    rel_pred_scaled = rel_pred.copy()
    rel_pred_scaled[:, :3, 3] *= scale
    c2w_aligned = np.einsum("ij,njk->nik", cin[0], rel_pred_scaled)
    return np.stack([np.linalg.inv(T) for T in c2w_aligned], axis=0)


def prepare_multiview_conditioning(
    views: list[PredictView],
    *,
    process_res: int,
    patch_size: int = 14,
    use_pose: bool = True,
    ma_run: Path | None = None,
    use_prior_depth: bool = False,
) -> dict[str, Any]:
    """WorldMirror 2.0 / 1.0 및 Pi3X 에 넣을 다중 시점 조건 입력을 표준 라이브러리·numpy 로 준비한다.

    반환 항목:
      - `proc_hw`: `(h_proc, w_proc)` (둘 다 `patch_size=14` 의 배수)
      - `k_proc_center`: `(N, 3, 3)` float64 — 픽셀 중심 정수 규약(`x' = (x+0.5)*s - 0.5`)의 처리 해상도 K
      - `k_proc_edge`: `(N, 3, 3)` float64 — 픽셀 가장자리 규약(`x' = (x+0.5)*s = cx_center + 0.5`)의 처리 해상도 K
      - `c2w_cv`: `(N, 4, 4)` float32 — 입력 OpenCV cam2world
      - `c2w_rel`: `(N, 4, 4)` float32 — 0번 카메라 기준 상대 OpenCV cam2world (`inv(c2w[0]) @ c2w[i]`)
      - `depth_prior`: `(N, h_proc, w_proc)` float32 또는 `None` — MapAnything 깊이 사전 조건(무효=0.0)
      - `cond_flags`: `[pose_flag, depth_flag, intrinsics_flag]` (WorldMirror `cond_flags`)
    """
    if not views:
        raise ValueError("views 가 비어 있다")
    intrinsics_check = _load_intrinsics_check()
    proc_hw = compute_patch_aligned_hw(views[0].image_hw, process_res, patch_size=patch_size)
    h_p, w_p = proc_hw
    k_center_list: list[np.ndarray] = []
    k_edge_list: list[np.ndarray] = []
    for v in views:
        kc = intrinsics_check.expected_axis_resize_k(v.K, v.image_hw[::-1], (w_p, h_p))
        ke = kc.copy()
        ke[0, 2] += 0.5
        ke[1, 2] += 0.5
        k_center_list.append(kc)
        k_edge_list.append(ke)
    k_proc_center = np.stack(k_center_list, axis=0)
    k_proc_edge = np.stack(k_edge_list, axis=0)

    c2w_cv = np.stack([v.cam2world for v in views], axis=0).astype(np.float64)
    c2w_rel = normalize_c2w_relative_to_first(c2w_cv).astype(np.float32)

    depth_prior: np.ndarray | None = None
    if use_prior_depth:
        if ma_run is None:
            raise ValueError("--prior-depth 는 ma_run 경로가 필요하다")
        depth_prior = load_ma_prior_depths_for_grid(ma_run, views, proc_hw, k_proc_center)

    pose_flag = 1 if use_pose else 0
    depth_flag = 1 if depth_prior is not None else 0
    intrs_flag = 1 if use_pose else 0
    return {
        "proc_hw": proc_hw,
        "k_proc_center": k_proc_center,
        "k_proc_edge": k_proc_edge,
        "c2w_cv": c2w_cv.astype(np.float32),
        "c2w_rel": c2w_rel,
        "depth_prior": depth_prior,
        "cond_flags": [pose_flag, depth_flag, intrs_flag],
    }


def _load_rgb_tensor_stack(views: list[PredictView], proc_hw: tuple[int, int]) -> np.ndarray:
    """`views` 영상을 `proc_hw = (h_proc, w_proc)` 로 리사이즈해 `(N, 3, h_proc, w_proc)` float32 `[0, 1]` 배열로 읽는다."""
    from PIL import Image  # type: ignore[import-not-found]

    h_p, w_p = int(proc_hw[0]), int(proc_hw[1])
    resample_bicubic = getattr(getattr(Image, "Resampling", Image), "BICUBIC")
    imgs: list[np.ndarray] = []
    for v in views:
        if not v.image_path.is_file():
            raise FileNotFoundError(f"입력 영상이 없다: {v.image_path}")
        with Image.open(v.image_path) as im:
            rgb = im.convert("RGB")
            if rgb.size != (w_p, h_p):
                rgb = rgb.resize((w_p, h_p), resample_bicubic)
            arr = np.asarray(rgb, dtype=np.float32) / 255.0
            imgs.append(np.transpose(arr, (2, 0, 1)))
    return np.stack(imgs, axis=0).astype(np.float32)


def _install_worldmirror_runtime_shims() -> None:
    """WorldMirror 전용 가상환경에서 쓰이지 않는 `gsplat`·`flash_attn`·`lightning_utilities` import 우회."""
    import importlib.util
    import logging
    import types

    if importlib.util.find_spec("gsplat") is None and "gsplat" not in sys.modules:
        gsplat_mod = types.ModuleType("gsplat")
        rendering_mod = types.ModuleType("gsplat.rendering")
        strategy_mod = types.ModuleType("gsplat.strategy")

        def _disabled_rasterization(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("gsplat rasterization is disabled in depth-only mode")

        rendering_mod.rasterization = _disabled_rasterization  # type: ignore[attr-defined]
        strategy_mod.DefaultStrategy = object  # type: ignore[attr-defined]
        gsplat_mod.rendering = rendering_mod  # type: ignore[attr-defined]
        gsplat_mod.strategy = strategy_mod  # type: ignore[attr-defined]
        sys.modules["gsplat"] = gsplat_mod
        sys.modules["gsplat.rendering"] = rendering_mod
        sys.modules["gsplat.strategy"] = strategy_mod

    if importlib.util.find_spec("flash_attn") is None and "flash_attn" not in sys.modules:
        import torch.nn.functional as F  # type: ignore[import-not-found]

        fa_mod = types.ModuleType("flash_attn")
        fa_iface = types.ModuleType("flash_attn.flash_attn_interface")

        def _sdpa_flash_attn_func(
            q: Any,
            k: Any,
            v: Any,
            dropout_p: float = 0.0,
            softmax_scale: float | None = None,
            causal: bool = False,
            **_kw: Any,
        ) -> Any:
            return F.scaled_dot_product_attention(
                q.transpose(1, 2),
                k.transpose(1, 2),
                v.transpose(1, 2),
                dropout_p=float(dropout_p),
                is_causal=bool(causal),
                scale=softmax_scale,
            ).transpose(1, 2)

        fa_iface.flash_attn_func = _sdpa_flash_attn_func  # type: ignore[attr-defined]
        fa_mod.flash_attn_interface = fa_iface  # type: ignore[attr-defined]
        fa_mod.flash_attn_func = _sdpa_flash_attn_func  # type: ignore[attr-defined]
        sys.modules["flash_attn"] = fa_mod
        sys.modules["flash_attn.flash_attn_interface"] = fa_iface

    if (
        importlib.util.find_spec("lightning_utilities") is None
        and "training.utils.logger" not in sys.modules
    ):
        training_mod = sys.modules.setdefault("training", types.ModuleType("training"))
        utils_mod = sys.modules.setdefault("training.utils", types.ModuleType("training.utils"))
        logger_mod = types.ModuleType("training.utils.logger")

        class _FallbackRankedLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
            def __init__(
                self,
                name: str = __name__,
                rank_zero_only: bool = False,
                extra: dict[str, Any] | None = None,
            ) -> None:
                super().__init__(logging.getLogger(name), extra or {})
                self.rank_zero_only = rank_zero_only

        logger_mod.RankedLogger = _FallbackRankedLogger  # type: ignore[attr-defined]
        training_mod.utils = utils_mod  # type: ignore[attr-defined]
        utils_mod.logger = logger_mod  # type: ignore[attr-defined]
        sys.modules["training.utils.logger"] = logger_mod


class WorldMirrorAdapter:
    """WorldMirror 2.0 (`worldmirror2`) 및 WorldMirror 1.0 (`worldmirror1`) 다중 시점 깊이 어댑터.

    공식 규약:
      - WorldMirror 2.0 (`Tencent-Hunyuan/HY-World-2.0` @ `df9988ef`, `hyworld2/worldrecon/pipeline.py:598-615`):
        - `cond_flags = [pose, depth, intrinsics]` (각 0 또는 1)
        - `views["camera_poses"]`: `(1, S, 4, 4)` OpenCV **c2w** (0번 카메라 기준 상대 변환 후 내부에서 `[0, 1]` 단위 정육면체로 정규화)
        - `views["camera_intrs"]`: `(1, S, 3, 3)` 처리 해상도 `(H_proc, W_proc)` 기준 픽셀 K
        - `views["depthmap"]`: `(1, S, H_proc, W_proc)` 카메라 좌표 Z-depth 사전 조건(무효=0.0)
        - 출력 `predictions["depth"]`: `(1, S, H_proc, W_proc, 1)` 카메라 좌표 Z-depth, `predictions["depth_conf"]`: `(1, S, H_proc, W_proc)`
        - `disable_heads=["gs", "normal", "points"]` 로 3DGS·법선·점구름 헤드 생략
      - WorldMirror 1.0 (`Tencent-Hunyuan/HunyuanWorld-Mirror` @ `c61300e4`, `src/models/models/worldmirror.py:136-260`):
        - 동일한 `cond_flags = [pose, depth, intrinsics]` (`src/models/models/visual_transformer.py:255, 348-369`)
        - 기본 처리 해상도 `process_res = 518` (`img_size=518`, `normalized_rope=False`)
      - 입력 자세는 토큰 조건으로만 쓰이며 출력 깊이는 모델 내부 정규화 스케일이므로 `fuse` 단계에서 MapAnything 기준 깊이에 정합한다.
    """

    def __init__(
        self,
        variant: str = "worldmirror2",
        model: str | None = None,
        *,
        process_res: int | None = None,
        use_pose: bool = True,
        use_prior_depth: bool = False,
        ma_run: Path | None = None,
        repo_dir: Path | None = None,
        runner: Any = None,
    ) -> None:
        if variant not in ("worldmirror2", "worldmirror1"):
            raise ValueError(f"지원하지 않는 WorldMirror 변종: {variant!r}")
        self.variant = variant
        self.name = variant
        self.model_path = model or DEFAULT_MODEL_PATHS[variant]
        self.process_res = int(process_res or DEFAULT_PROCESS_RES[variant])
        self.use_pose = bool(use_pose)
        self.use_prior_depth = bool(use_prior_depth)
        self.ma_run = Path(ma_run) if ma_run is not None else None
        self.repo_dir = Path(repo_dir) if repo_dir is not None else None
        self.runner = runner
        self._perf: dict[str, Any] = {}

    def _resolve_weights_dir_for_hash(self) -> Path:
        p = Path(self.model_path)
        if p.is_dir() and (p / "HY-WorldMirror-2.0").is_dir():
            return p / "HY-WorldMirror-2.0"
        return p

    def describe(self) -> dict[str, Any]:
        commit_env = "SIMAI_WM2_COMMIT" if self.variant == "worldmirror2" else "SIMAI_WM1_COMMIT"
        return {
            "adapter": self.name,
            "model_path": str(self.model_path),
            "process_res": self.process_res,
            "pose_conditioned": self.use_pose,
            "prior_depth_conditioned": self.use_prior_depth,
            "cond_flags": [
                1 if self.use_pose else 0,
                1 if self.use_prior_depth else 0,
                1 if self.use_pose else 0,
            ],
            "metric": "MODEL_NORMALIZED_UNIT_CUBE",
            "commit": os.environ.get(commit_env) or None,
            "weights_hash": _weights_hash(self._resolve_weights_dir_for_hash()),
        } | self._perf

    def _setup_sys_path(self) -> None:
        if self.variant == "worldmirror2":
            repo = self.repo_dir or Path(os.environ.get("SIMAI_WM2_REPO", "/root/HY-World-2.0"))
            candidates = [repo / "hyworld2" / "worldrecon", repo]
        else:
            repo = self.repo_dir or Path(
                os.environ.get("SIMAI_WM1_REPO", "/root/HunyuanWorld-Mirror")
            )
            candidates = [repo, repo / "src"]
        for c in reversed(candidates):
            cs = str(c)
            if c.is_dir() and cs not in sys.path:
                sys.path.insert(0, cs)

    def _infer_gpu(self, cond: dict[str, Any], imgs_chw: np.ndarray) -> dict[str, Any]:
        _install_worldmirror_runtime_shims()
        self._setup_sys_path()
        import torch  # type: ignore[import-not-found]

        dev = torch.device("cuda")
        t0 = time.perf_counter()
        p_model = Path(self.model_path)
        if self.variant == "worldmirror2":
            from hyworldmirror.models.models.worldmirror import (  # type: ignore[import-not-found]
                WorldMirror,
            )

            if p_model.is_dir() and (p_model / "config.json").is_file():
                model = WorldMirror.from_pretrained(str(p_model))
            elif (
                p_model.is_dir()
                and (p_model / "HY-WorldMirror-2.0" / "config.json").is_file()
            ):
                model = WorldMirror.from_pretrained(str(p_model / "HY-WorldMirror-2.0"))
            else:
                model = WorldMirror.from_pretrained(
                    self.model_path, subfolder="HY-WorldMirror-2.0"
                )
        else:
            from src.models.models.worldmirror import (  # type: ignore[import-not-found]
                WorldMirror,
            )

            model = WorldMirror.from_pretrained(self.model_path)
            model.enable_gs = False
            model.enable_pts = False
            model.enable_norm = False
            for attr in ("gs_head", "gs_renderer", "pts_head", "norm_head"):
                if hasattr(model, attr):
                    delattr(model, attr)

        model = model.to(dev).eval()
        torch.cuda.synchronize()
        load_s = round(time.perf_counter() - t0, 1)

        views_t: dict[str, Any] = {
            "img": torch.from_numpy(imgs_chw[None, ...]).to(dev, dtype=torch.float32),
        }
        if self.use_pose:
            views_t["camera_poses"] = torch.from_numpy(cond["c2w_rel"][None, ...]).to(
                dev, dtype=torch.float32
            )
            views_t["camera_intrs"] = torch.from_numpy(
                cond["k_proc_edge"][None, ...].astype(np.float32)
            ).to(dev, dtype=torch.float32)
        if self.use_prior_depth and cond["depth_prior"] is not None:
            views_t["depthmap"] = torch.from_numpy(cond["depth_prior"][None, ...]).to(
                dev, dtype=torch.float32
            )

        cond_flags = list(cond["cond_flags"])
        amp_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        t1 = time.perf_counter()
        with torch.no_grad(), torch.amp.autocast("cuda", dtype=amp_dtype):
            if self.variant == "worldmirror2":
                # 2.0 forward 는 disable_heads 를 받지 않는다(pipeline.py _disable_heads 는 enable_* 속성을 끈다).
                for attr in ("enable_gs", "enable_norm", "enable_pts"):
                    if hasattr(model, attr):
                        setattr(model, attr, False)
                preds = model(views=views_t, cond_flags=cond_flags)
            else:
                preds = model(views=views_t, cond_flags=cond_flags)
        torch.cuda.synchronize()
        infer_ms = round((time.perf_counter() - t1) * 1000, 1)
        peak_mb = round(torch.cuda.max_memory_allocated() / 2**20, 1)

        depth_t = preds["depth"][0]
        if depth_t.ndim == 4 and depth_t.shape[-1] == 1:
            depth_t = depth_t[..., 0]
        depth_np = depth_t.detach().float().cpu().numpy().astype(np.float32)
        conf_np = (
            preds["depth_conf"][0].detach().float().cpu().numpy().astype(np.float32)
            if "depth_conf" in preds
            else None
        )
        k_pred_np = (
            preds["camera_intrs"][0].detach().float().cpu().numpy().astype(np.float64)
            if "camera_intrs" in preds
            else cond["k_proc_edge"].copy()
        )
        c2w_pred_np = (
            preds["camera_poses"][0].detach().float().cpu().numpy().astype(np.float64)
            if "camera_poses" in preds
            else None
        )
        return {
            "depth": depth_np,
            "conf": conf_np,
            "camera_intrs_edge": k_pred_np,
            "camera_poses": c2w_pred_np,
            "load_s": load_s,
            "infer_ms": [infer_ms],
            "peak_alloc_mb": peak_mb,
        }

    def predict(self, views: list[PredictView]) -> list[DepthOut]:
        intrinsics_check = _load_intrinsics_check()
        cond = prepare_multiview_conditioning(
            views,
            process_res=self.process_res,
            patch_size=14,
            use_pose=self.use_pose,
            ma_run=self.ma_run,
            use_prior_depth=self.use_prior_depth,
        )
        proc_hw: tuple[int, int] = cond["proc_hw"]
        if self.runner is not None:
            imgs_chw = np.zeros((len(views), 3, proc_hw[0], proc_hw[1]), dtype=np.float32)
            res = self.runner(cond, imgs_chw)
        else:
            imgs_chw = _load_rgb_tensor_stack(views, proc_hw)
            res = self._infer_gpu(cond, imgs_chw)

        depth = np.asarray(res["depth"], dtype=np.float32)
        conf = None if res.get("conf") is None else np.asarray(res["conf"], dtype=np.float32)
        Kp_edge = np.asarray(
            res.get("camera_intrs_edge", cond["k_proc_edge"]), dtype=np.float64
        ).copy()
        # WorldMirror 의 transform_camera_vector 는 주점을 (w/2, h/2) 가장자리 규약으로 두므로 픽셀 중심 정수 규약(-0.5)으로 변환
        Kp = Kp_edge.copy()
        Kp[:, 0, 2] -= 0.5
        Kp[:, 1, 2] -= 0.5

        pose_check = None
        if res.get("camera_poses") is not None:
            w2c_in = np.stack([np.linalg.inv(v.cam2world) for v in views], axis=0)
            w2c_pred = align_predicted_c2w_to_input_w2c(
                cond["c2w_cv"].astype(np.float64), np.asarray(res["camera_poses"], dtype=np.float64)
            )
            pose_check = pose_difference(w2c_in, w2c_pred)

        out: list[DepthOut] = []
        kv: list[dict[str, Any]] = []
        for i, v in enumerate(views):
            d = depth[i]
            m = np.isfinite(d) & (d > 0)
            K_grid = cond["k_proc_center"][i]
            Kd_pred, k_note = k_for_depth_grid(Kp[i], d.shape, v.image_hw)
            kv.append(
                {
                    "name": v.name,
                    "output": Kd_pred,
                    "input": K_grid if self.use_pose else None,
                    "expected": intrinsics_check.expected_axis_resize_k(
                        v.K, v.image_hw[::-1], d.shape[::-1]
                    ),
                }
            )
            # DPT 깊이 헤드는 입력 영상(K_grid) 픽셀 격자에 정렬되어 있으므로 K_grid → v.K 로 원본 해상도에 옮긴다
            full, ok = resample_depth(d, m, K_grid, v.K, v.image_hw)
            c = None
            if conf is not None:
                c, _ = resample_depth(conf[i], m, K_grid, v.K, v.image_hw, edge_tol=np.inf)
            out.append(
                DepthOut(
                    full,
                    ok,
                    c,
                    tuple(d.shape),
                    {
                        "intrinsics_depth_grid": K_grid.tolist(),
                        "intrinsics_pred_camera_head": Kd_pred.tolist(),
                        "intrinsics_note": k_note,
                    },
                )
            )

        ic_rec = intrinsics_check.check(kv)
        self._perf = {
            "load_s": res.get("load_s", 0.0),
            "infer_ms": res.get("infer_ms", [0.0]),
            "peak_alloc_mb": res.get("peak_alloc_mb", 0.0),
            "pose_check_aligned_pred_vs_input": pose_check,
            "intrinsics_check": ic_rec,
        }
        warn = intrinsics_check.warning(ic_rec)
        if warn:
            self._perf["warnings"] = [warn]
            print(f"경고: {warn}", file=sys.stderr, flush=True)
        return out


class Pi3XAdapter:
    """Pi3X (`yyfz/Pi3` @ `9fa3ddb3`, `pi3/models/pi3x.py`) 다중 시점 깊이 어댑터.

    공식 규약:
      - `imgs`: `(1, N, 3, H_proc, W_proc)` float32 RGB `[0, 1]`, `H_proc, W_proc` 는 `patch_size=14` 배수
      - `poses`: `(1, N, 4, 4)` OpenCV **c2w** (`intrinsics` 필수 동반, `pi3x.py:319`)
      - `intrinsics`: `(1, N, 3, 3)` 처리 해상도 기준 픽셀 K (`get_pixel` 이 `u+0.5, v+0.5` 를 쓰므로 `k_proc_edge` 전달)
      - `depths`: `(1, N, H_proc, W_proc)` 카메라 좌표 Z-depth 사전 조건(무효=0.0)
      - 출력 `res["local_points"]`: `(1, N, H_proc, W_proc, 3)` 카메라 좌표 3D 점 (`local_points[..., 2]` 가 카메라 Z-depth `z * metric`)
      - 출력 `res["conf"]`: `(1, N, H_proc, W_proc, 1)` 신뢰도 logit → `sigmoid` 로 `[0, 1]` 변환 (`example_mm.py:125`)
    """

    name = "pi3x"

    def __init__(
        self,
        model: str | None = None,
        *,
        process_res: int | None = None,
        use_pose: bool = True,
        use_prior_depth: bool = False,
        ma_run: Path | None = None,
        repo_dir: Path | None = None,
        runner: Any = None,
    ) -> None:
        self.model_path = model or DEFAULT_MODEL_PATHS["pi3x"]
        self.process_res = int(process_res or DEFAULT_PROCESS_RES["pi3x"])
        self.use_pose = bool(use_pose)
        self.use_prior_depth = bool(use_prior_depth)
        self.ma_run = Path(ma_run) if ma_run is not None else None
        self.repo_dir = Path(repo_dir) if repo_dir is not None else None
        self.runner = runner
        self._perf: dict[str, Any] = {}

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "model_path": str(self.model_path),
            "process_res": self.process_res,
            "pose_conditioned": self.use_pose,
            "prior_depth_conditioned": self.use_prior_depth,
            "metric": "MODEL_METRIC_HEAD",
            "commit": os.environ.get("SIMAI_PI3_COMMIT") or None,
            "weights_hash": _weights_hash(Path(self.model_path)),
        } | self._perf

    def _setup_sys_path(self) -> None:
        repo = self.repo_dir or Path(os.environ.get("SIMAI_PI3_REPO", "/root/Pi3"))
        rs = str(repo)
        if repo.is_dir() and rs not in sys.path:
            sys.path.insert(0, rs)

    def _infer_gpu(self, cond: dict[str, Any], imgs_chw: np.ndarray) -> dict[str, Any]:
        self._setup_sys_path()
        import torch  # type: ignore[import-not-found]
        from pi3.models.pi3x import Pi3X  # type: ignore[import-not-found]

        dev = torch.device("cuda")
        t0 = time.perf_counter()
        model = Pi3X.from_pretrained(self.model_path).to(dev).eval()
        torch.cuda.synchronize()
        load_s = round(time.perf_counter() - t0, 1)

        imgs_t = torch.from_numpy(imgs_chw[None, ...]).to(dev, dtype=torch.float32)
        poses_t = (
            torch.from_numpy(cond["c2w_cv"][None, ...]).to(dev, dtype=torch.float32)
            if self.use_pose
            else None
        )
        depths_t = (
            torch.from_numpy(cond["depth_prior"][None, ...]).to(dev, dtype=torch.float32)
            if (self.use_prior_depth and cond["depth_prior"] is not None)
            else None
        )
        intrinsics_t = (
            torch.from_numpy(cond["k_proc_edge"][None, ...].astype(np.float32)).to(
                dev, dtype=torch.float32
            )
            if (self.use_pose or depths_t is not None)
            else None
        )

        amp_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        t1 = time.perf_counter()
        with torch.no_grad(), torch.amp.autocast("cuda", dtype=amp_dtype):
            res = model(
                imgs=imgs_t,
                poses=poses_t,
                depths=depths_t,
                intrinsics=intrinsics_t,
            )
        torch.cuda.synchronize()
        infer_ms = round((time.perf_counter() - t1) * 1000, 1)
        peak_mb = round(torch.cuda.max_memory_allocated() / 2**20, 1)

        local_pts = res["local_points"][0].detach().float().cpu().numpy().astype(np.float32)
        conf_logits = res["conf"][0]
        if conf_logits.ndim == 4 and conf_logits.shape[-1] == 1:
            conf_logits = conf_logits[..., 0]
        conf_np = torch.sigmoid(conf_logits.detach().float()).cpu().numpy().astype(np.float32)
        c2w_pred_np = (
            res["camera_poses"][0].detach().float().cpu().numpy().astype(np.float64)
            if "camera_poses" in res
            else None
        )
        metric_val = _plain(res.get("metric"))
        return {
            "local_points": local_pts,
            "conf": conf_np,
            "camera_poses": c2w_pred_np,
            "metric_scale": metric_val,
            "load_s": load_s,
            "infer_ms": [infer_ms],
            "peak_alloc_mb": peak_mb,
        }

    def predict(self, views: list[PredictView]) -> list[DepthOut]:
        intrinsics_check = _load_intrinsics_check()
        cond = prepare_multiview_conditioning(
            views,
            process_res=self.process_res,
            patch_size=14,
            use_pose=self.use_pose,
            ma_run=self.ma_run,
            use_prior_depth=self.use_prior_depth,
        )
        proc_hw: tuple[int, int] = cond["proc_hw"]
        if self.runner is not None:
            imgs_chw = np.zeros((len(views), 3, proc_hw[0], proc_hw[1]), dtype=np.float32)
            res = self.runner(cond, imgs_chw)
        else:
            imgs_chw = _load_rgb_tensor_stack(views, proc_hw)
            res = self._infer_gpu(cond, imgs_chw)

        local_pts = np.asarray(res["local_points"], dtype=np.float32)
        depth = local_pts[..., 2].astype(np.float32)
        if res.get("conf") is not None:
            conf = np.asarray(res["conf"], dtype=np.float32)
        elif res.get("conf_logits") is not None:
            logits = np.clip(np.asarray(res["conf_logits"], dtype=np.float32), -30.0, 30.0)
            conf = (1.0 / (1.0 + np.exp(-logits))).astype(np.float32)
        else:
            conf = None

        pose_check = None
        if res.get("camera_poses") is not None:
            w2c_in = np.stack([np.linalg.inv(v.cam2world) for v in views], axis=0)
            w2c_pred = align_predicted_c2w_to_input_w2c(
                cond["c2w_cv"].astype(np.float64), np.asarray(res["camera_poses"], dtype=np.float64)
            )
            pose_check = pose_difference(w2c_in, w2c_pred)

        out: list[DepthOut] = []
        kv: list[dict[str, Any]] = []
        for i, v in enumerate(views):
            d = depth[i]
            m = np.isfinite(d) & (d > 0)
            K_grid = cond["k_proc_center"][i]
            Kd_pred = recover_pinhole_k_from_local_points(local_pts[i])
            kv.append(
                {
                    "name": v.name,
                    "output": Kd_pred,
                    "input": K_grid if (self.use_pose or self.use_prior_depth) else None,
                    "expected": intrinsics_check.expected_axis_resize_k(
                        v.K, v.image_hw[::-1], d.shape[::-1]
                    ),
                }
            )
            full, ok = resample_depth(d, m, K_grid, v.K, v.image_hw)
            c = None
            if conf is not None:
                c, _ = resample_depth(conf[i], m, K_grid, v.K, v.image_hw, edge_tol=np.inf)
            out.append(
                DepthOut(
                    full,
                    ok,
                    c,
                    tuple(d.shape),
                    {
                        "intrinsics_depth_grid": K_grid.tolist(),
                        "intrinsics_recovered_from_rays": Kd_pred.tolist(),
                        "intrinsics_note": "DEPTH_GRID",
                    },
                )
            )

        ic_rec = intrinsics_check.check(kv)
        self._perf = {
            "load_s": res.get("load_s", 0.0),
            "infer_ms": res.get("infer_ms", [0.0]),
            "peak_alloc_mb": res.get("peak_alloc_mb", 0.0),
            "metric_scale": _plain(res.get("metric_scale")),
            "pose_check_aligned_pred_vs_input": pose_check,
            "intrinsics_check": ic_rec,
        }
        warn = intrinsics_check.warning(ic_rec)
        if warn:
            self._perf["warnings"] = [warn]
            print(f"경고: {warn}", file=sys.stderr, flush=True)
        return out


def _weights_hash(path: Path) -> str | None:
    files = sorted(
        p for p in (path.glob("*.safetensors") if path.is_dir() else [path]) if p.is_file()
    )
    if not files:
        return None
    return sha256_bytes("".join(f"{f.name}:{sha256_file(f)}\n" for f in files).encode())


def predict_views(
    ma_run: Path,
    prepared: dict[str, Any],
    *,
    calibration: Path | None = None,
) -> list[PredictView]:
    if calibration is not None:
        cams, _ = read_calibration_cameras(calibration)
        src_label = str(calibration)
    else:
        cams, _ = read_cameras(ma_run)
        src_label = "cameras.json"
    out = []
    for v in prepared["views"]:
        if v["name"] not in cams:
            raise ValueError(f"{src_label} 에 view {v['name']!r} 가 없다")
        w, h = v["image_size"]
        out.append(
            PredictView(
                v["name"],
                Path(prepared["_dir"]) / v["image"],
                (int(h), int(w)),
                np.asarray(v["K_undistorted"], float),
                np.asarray(cams[v["name"]]["cam2world"], float),
            )
        )
    return out


def run_predict(
    adapter,
    ma_run: Path,
    prepared: dict[str, Any],
    out_dir: Path,
    *,
    calibration: Path | None = None,
) -> dict[str, Any]:
    views = predict_views(ma_run, prepared, calibration=calibration)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} 가 비어 있지 않다(결과를 덮어쓰지 않는다)")
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    head: dict[str, Any] = {
        "format": DEPTH_FORMAT,
        "format_version": DEPTH_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        results = adapter.predict(views)
    except Exception as e:  # noqa: BLE001  GPU 세션: OOM·적재 실패도 결과로 남긴다(2304 처리 등)
        kind = "OOM" if "OutOfMemory" in type(e).__name__ else "ERROR"
        fail = head | {
            "result": kind,
            "error": f"{type(e).__name__}: {e}"[:800],
            "adapter": adapter.describe(),
            "prepared_sha256": prepared["_sha256"],
        }
        (out_dir / "depth.json").write_text(
            json.dumps(fail, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        raise RuntimeError(f"깊이 모델 {kind}: {fail['error'][:200]}") from e
    total_ms = round((time.perf_counter() - t0) * 1000, 1)
    pose_source = "CALIBRATION" if calibration is not None else "MAPANYTHING"
    doc: dict[str, Any] = head | {
        "result": "SUCCESS",
        "adapter": adapter.describe(),
        "pose_source": pose_source,
        "total_ms": total_ms,
        "ma_run": {"dir": Path(ma_run).name, "cameras_json_sha256": read_cameras(ma_run)[1]},
        "calibration_json_sha256": read_calibration_cameras(calibration)[1]
        if calibration is not None
        else None,
        "prepared_sha256": prepared["_sha256"],
        "depth": "z(카메라 앞), 원본(펴진) 해상도, 무효 NaN",
        "views": [],
    }
    for v, r in zip(views, results):
        file = f"{v.name}.depth.npz"
        arrays = {"depth": r.depth.astype(np.float32), "valid": r.valid}
        if r.conf is not None:
            arrays["conf"] = r.conf.astype(np.float32)
        np.savez_compressed(out_dir / file, **arrays)
        doc["views"].append(
            {
                "name": v.name,
                "file": file,
                "image_hw": list(v.image_hw),
                "K": v.K.tolist(),
                "native_hw": list(r.native_hw),
                "valid_ratio": round(float(r.valid.mean()), 4),
                **r.meta,
            }
        )
    (out_dir / "depth.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return doc


# --------------------------------------------------------------------------- 정합(fuse)


@dataclass(frozen=True)
class Registration:
    status: str  # OK / FAILED
    scale: float | None
    shift: float | None
    pairs: int
    inlier_ratio: float
    rel_err_before: dict[str, float]
    rel_err_after: dict[str, float]
    reason: str | None = None

    def record(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "scale": None if self.scale is None else round(self.scale, 5),
            "shift": None if self.shift is None else round(self.shift, 5),
            "pairs": self.pairs,
            "inlier_ratio": round(self.inlier_ratio, 4),
            "abs_rel_err_before": self.rel_err_before,
            "abs_rel_err_after": self.rel_err_after,
        }


def fit_scale_shift(
    d: np.ndarray,
    ref: np.ndarray,
    mode: str,
    tol: float = REG_TOL,
    min_pairs: int = REG_MIN_PAIRS,
    min_inlier: float = REG_MIN_INLIER,
) -> Registration:
    """ref ≈ s·d (+ t). 축척은 로그 비의 중앙값에서 시작해 인라이어(|ref − s·d − t| ≤ tol·ref)로 다시 맞춘다."""
    d = np.asarray(d, float)
    ref = np.asarray(ref, float)
    ok = np.isfinite(d) & np.isfinite(ref) & (d > 0) & (ref > 0)
    d, ref = d[ok], ref[ok]
    before = stats(np.abs(d - ref) / ref)
    if len(d) < min_pairs:
        return Registration(
            "FAILED", None, None, int(len(d)), 0.0, before, {}, f"정합 표본 {len(d)} < {min_pairs}"
        )
    if mode == "none":
        s, t = 1.0, 0.0
    else:
        r = np.log(ref / d)
        s, t = float(np.exp(np.median(r))), 0.0
        for _ in range(5):
            inl = np.abs(ref - (s * d + t)) <= tol * ref
            if inl.sum() < min_pairs:
                break
            if mode == "scale":
                s = float(np.exp(np.median(r[inl])))
            else:  # scale_shift: 인라이어 최소제곱(상대 오차 가중)
                w = 1.0 / ref[inl]
                A = np.column_stack([d[inl] * w, w])
                s, t = (float(x) for x in np.linalg.lstsq(A, ref[inl] * w, rcond=None)[0])
                if s <= 0:
                    return Registration(
                        "FAILED", None, None, int(len(d)), 0.0, before, {}, "축척이 0 이하"
                    )
    inl = np.abs(ref - (s * d + t)) <= tol * ref
    ratio = float(inl.mean())
    after = stats(np.abs(s * d + t - ref) / ref)
    if ratio < min_inlier:
        return Registration(
            "FAILED", s, t, int(len(d)), ratio, before, after, f"인라이어 {ratio:.2f} < {min_inlier}"
        )
    return Registration("OK", s, t, int(len(d)), ratio, before, after)


def register_to_reference(
    depth: np.ndarray,
    valid: np.ndarray,
    K: np.ndarray,
    ref_depth: np.ndarray,
    ref_valid: np.ndarray,
    ref_K: np.ndarray,
    mode: str,
    max_pairs: int = 200_000,
    seed: int = 0,
) -> Registration:
    """원본 해상도 깊이를 같은 카메라의 MapAnything 깊이(모델 해상도)에 맞춘다(자세 고정, 카메라별 축척·이동)."""
    rv, ru = np.nonzero(ref_valid)
    if len(ru) > max_pairs:
        pick = np.random.default_rng(seed).choice(len(ru), max_pairs, replace=False)
        rv, ru = rv[pick], ru[pick]
    A = np.asarray(K, float) @ np.linalg.inv(np.asarray(ref_K, float))
    x = A[0, 0] * ru + A[0, 1] * rv + A[0, 2]
    y = A[1, 0] * ru + A[1, 1] * rv + A[1, 2]
    d, ok = sample_depth(depth, valid, x, y)
    return fit_scale_shift(d[ok], ref_depth[rv[ok], ru[ok]], mode)


# --------------------------------------------------------------------------- 시점·투영


@dataclass
class FuseView:
    name: str
    depth: np.ndarray  # (H, W) MapAnything 단위, 무효 NaN
    valid: np.ndarray
    K: np.ndarray
    cam2world: np.ndarray
    colors: np.ndarray | None = None  # (H, W, 3) uint8 RGB

    def backproject(self, v: np.ndarray, u: np.ndarray, z: np.ndarray | None = None) -> np.ndarray:
        z = self.depth[v, u].astype(float) if z is None else z
        Kinv = np.linalg.inv(self.K)
        rays = Kinv @ np.vstack([u + 0.0, v + 0.0, np.ones(len(u))])
        return (self.cam2world[:3, :3] @ (rays * z)).T + self.cam2world[:3, 3]

    def project(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        Xc = (X - self.cam2world[:3, 3]) @ self.cam2world[:3, :3]
        z = Xc[:, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            uv = (Xc @ self.K.T)[:, :2] / z[:, None]
        return uv, z


def cross_view_consistency(
    views: list[FuseView],
    samples: int = 20_000,
    covis_tol: float = 0.20,
    roundtrip_tol: float = 0.05,
    seed: int = 0,
) -> dict[str, Any]:
    """카메라 간 깊이 일관성(원본 px). i 의 점을 j 에 투영해 j 깊이와의 상대 차 |z − d_j|/d_j 를 잰다(0.2 안 = 같이 보임으로
    본다, 넘으면 가림). 5% 안이면 j 점을 i 로 되돌려 처음 픽셀과의 거리(왕복 재투영, world_frame 의 지표를 원본 해상도에서)."""
    rng = np.random.default_rng(seed)
    out: dict[str, Any] = {
        "method": "CROSS_VIEW_DEPTH",
        "covis_tol": covis_tol,
        "roundtrip_tol": roundtrip_tol,
        "cameras": {},
    }
    for vi in views:
        vv, uu = np.nonzero(vi.valid)
        if not len(uu):
            out["cameras"][vi.name] = None
            continue
        pick = rng.choice(len(uu), min(samples, len(uu)), replace=False)
        v0, u0 = vv[pick], uu[pick]
        X = vi.backproject(v0, u0)
        rels, rts = [], []
        for vj in views:
            if vj is vi:
                continue
            q, z = vj.project(X)
            front = z > 0
            dj, okj = sample_depth(
                vj.depth, vj.valid, np.where(front, q[:, 0], -1), np.where(front, q[:, 1], -1)
            )
            okj &= front
            rel = np.full(len(z), np.inf)
            rel[okj] = np.abs(z[okj] - dj[okj]) / dj[okj]
            cov = rel <= covis_tol
            rels.append(rel[cov])
            rt = np.nonzero(rel <= roundtrip_tol)[0]
            if len(rt):
                Kinv = np.linalg.inv(vj.K)
                rays = Kinv @ np.vstack([q[rt, 0], q[rt, 1], np.ones(len(rt))])
                Y = (vj.cam2world[:3, :3] @ (rays * dj[rt])).T + vj.cam2world[:3, 3]
                back, _ = vi.project(Y)
                rts.append(np.linalg.norm(back - np.column_stack([u0[rt], v0[rt]]), axis=1))
        rel = np.concatenate(rels) if rels else np.zeros(0)
        rt = np.concatenate(rts) if rts else np.zeros(0)
        if not len(rel):
            out["cameras"][vi.name] = None
            continue
        out["cameras"][vi.name] = {
            "covisible_points": int(len(rel)),
            "abs_rel_depth": stats(rel),
            "within_1pct": round(float(np.mean(rel <= 0.01)), 4),
            "within_5pct": round(float(np.mean(rel <= 0.05)), 4),
            "roundtrip_px": stats(rt, 3) if len(rt) else None,
        }
    return out


# --------------------------------------------------------------------------- 융합


def visibility_filter(
    views: list[FuseView],
    stride: int = 1,
    support_tol: float = SUPPORT_TOL,
    conflict_tol: float = CONFLICT_TOL,
) -> tuple[list[dict[str, np.ndarray]], dict[str, Any]]:
    """카메라별 점(stride 격자)을 다른 카메라 깊이와 비교한다. 지지: 3% 안. 충돌: 다른 카메라가 본 표면(투영 위치 네 이웃의
    가장 가까운 깊이)보다 5% 넘게 앞(그 카메라에서는 빈 공간). 충돌 수가 지지 수보다 많으면 뺀다. 다른 카메라에 안 보이는 점(단일 시점)은 남기고 비율을 적는다."""
    kept, rec = [], {}
    for vi in views:
        m = vi.valid.copy()
        if stride > 1:
            g = np.zeros_like(m)
            g[::stride, ::stride] = True
            m &= g
        v, u = np.nonzero(m)
        X = vi.backproject(v, u)
        support = np.zeros(len(u), np.int32)
        conflict = np.zeros(len(u), np.int32)
        seen = np.zeros(len(u), np.int32)
        for vj in views:
            if vj is vi:
                continue
            q, z = vj.project(X)
            front = z > 0
            qx, qy = np.where(front, q[:, 0], -1.0), np.where(front, q[:, 1], -1.0)
            dj, okj = sample_depth(vj.depth, vj.valid, qx, qy)
            idx = np.nonzero(okj & front)[0]
            rel = (z[idx] - dj[idx]) / dj[idx]
            seen[idx] += 1
            support[idx] += np.abs(rel) <= support_tol
            # 충돌은 네 이웃 중 가장 가까운 표면보다도 앞일 때만(스치는 각도·모서리에서 보간 오차를 충돌로 보지 않는다)
            lo = neighbor_min(vj.depth, vj.valid, qx[idx], qy[idx])
            conflict[idx] += z[idx] < lo * (1 - conflict_tol)
        keep = conflict <= support
        n = max(len(u), 1)
        rec[vi.name] = {
            "points": int(len(u)),
            "kept": int(keep.sum()),
            "dropped_conflict_ratio": round(float((~keep).sum() / n), 4),
            "supported_ratio": round(float((support > 0).sum() / n), 4),
            "single_view_ratio": round(float((seen == 0).sum() / n), 4),
        }
        col = vi.colors[v[keep], u[keep]] if vi.colors is not None else None
        kept.append({"xyz": X[keep], "rgb": col, "support": support[keep]})
    return kept, rec


def voxel_merge(
    chunks: list[dict[str, np.ndarray]], voxel: float
) -> tuple[np.ndarray, np.ndarray | None, np.ndarray]:
    """칸(voxel m)마다 점 평균·색 평균. 반환 (xyz, rgb 또는 None, 칸의 점 수)."""
    xyz = np.concatenate([c["xyz"] for c in chunks]) if chunks else np.zeros((0, 3))
    has_rgb = bool(chunks) and all(c["rgb"] is not None for c in chunks)
    rgb = np.concatenate([c["rgb"] for c in chunks]).astype(np.float64) if has_rgb else None
    if not len(xyz) or voxel <= 0:
        return (
            xyz,
            (rgb.astype(np.uint8) if rgb is not None else None),
            np.ones(len(xyz), np.int64),
        )
    key = np.floor(xyz / voxel).astype(np.int64)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    out = np.zeros((len(cnt), 3))
    for k in range(3):
        out[:, k] = np.bincount(inv, weights=xyz[:, k], minlength=len(cnt)) / cnt
    col = None
    if rgb is not None:
        col = np.zeros((len(cnt), 3))
        for k in range(3):
            col[:, k] = np.bincount(inv, weights=rgb[:, k], minlength=len(cnt)) / cnt
        col = np.clip(np.round(col), 0, 255).astype(np.uint8)
    return out, col, cnt


# --------------------------------------------------------------------------- 지표


def edge_sharpness(
    depth: np.ndarray,
    valid: np.ndarray,
    jump: float = FLYING_JUMP,
    gap: float = FLYING_GAP,
) -> dict[str, Any]:
    """가장자리 선명도: 3×3 이웃 깊이 폭이 jump(상대)를 넘는 불연속 주변 픽셀 중, 깊이가 이웃 최소·최대에서 둘 다 gap 넘게 떨어진
    중간값(앞·뒤 표면 사이에 떠 있는 점)의 비율. 낮을수록 선명하다."""
    d = np.where(valid, depth, np.nan).astype(np.float32)
    h, w = d.shape
    if h < 3 or w < 3:
        return {"discontinuity_pixels": 0, "flying_ratio": None}
    c = d[1:-1, 1:-1]
    lo = np.full(c.shape, np.inf, np.float32)
    hi = np.full(c.shape, -np.inf, np.float32)
    allv = np.isfinite(c)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            n = d[1 + dy : h - 1 + dy, 1 + dx : w - 1 + dx]
            allv &= np.isfinite(n)
            lo = np.fmin(lo, n)
            hi = np.fmax(hi, n)
    with np.errstate(invalid="ignore"):
        disc = allv & ((hi - lo) > jump * lo)
        flying = disc & (c > lo * (1 + gap)) & (c < hi * (1 - gap))
    nd = int(disc.sum())
    return {
        "discontinuity_pixels": nd,
        "discontinuity_ratio": round(nd / max(int(allv.sum()), 1), 5),
        "flying_ratio": round(float(flying.sum()) / nd, 4) if nd else None,
    }


def floor_metrics(
    xyz: np.ndarray,
    cam2world: list[np.ndarray],
    seed: int = 0,
    max_points: int = 200_000,
    cell: float = 0.05,
) -> dict[str, Any]:
    """출력 좌표의 점에서 world_frame 과 같은 규칙(find_floor)으로 바닥을 찾고 rms(3 cm 안 인라이어)·면 밀도를 잰다."""
    _ensure_simai_ai_on_path()
    from simai_ai.calibration.world_frame.frame import WorldFrameConfig, _up_prior, find_floor

    cfg = WorldFrameConfig()
    rng = np.random.default_rng(seed)
    pts = xyz[np.isfinite(xyz).all(axis=1)]
    if len(pts) < 100:
        return {"status": "NO_POINTS"}
    sub = pts[rng.choice(len(pts), max_points, replace=False)] if len(pts) > max_points else pts
    centers = np.array([T[:3, 3] for T in cam2world])
    floor, _ = find_floor(sub, centers, _up_prior(cam2world), cfg, rng)
    if floor is None:
        return {"status": "FLOOR_NOT_FOUND"}
    dist = floor.distance(pts)
    inl = np.abs(dist) < cfg.plane_tol_m
    n = floor.normal
    a = np.cross(n, [1.0, 0, 0] if abs(n[0]) < 0.9 else [0, 1.0, 0])
    a /= np.linalg.norm(a)
    b = np.cross(n, a)
    q = pts[inl]
    cells = np.unique(np.floor(np.column_stack([q @ a, q @ b]) / cell).astype(np.int64), axis=0)
    area = float(len(cells) * cell * cell)
    return {
        "status": "OK",
        "normal": np.round(n, 5).tolist(),
        "offset": round(float(floor.offset), 4),
        "rms": round(float(np.sqrt(np.mean(dist[inl] ** 2))), 5),
        "inliers": int(inl.sum()),
        "occupied_area_m2": round(area, 2),
        "points_per_m2": round(float(inl.sum()) / area, 1) if area else None,
        "camera_height": stats(floor.distance(centers), 3),
        "plane_tol": cfg.plane_tol_m,
        "cell": cell,
    }


# --------------------------------------------------------------------------- 출력 좌표(World 정렬)


def load_world_alignment(path: Path, cameras_sha: str) -> tuple[np.ndarray | None, dict[str, Any]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("format") != "simai.world_alignment":
        raise ValueError(f"{path}: simai.world_alignment 가 아니다")
    if doc.get("cameras_json_sha256") != cameras_sha:
        raise ValueError("World 정렬 결과가 다른 MapAnything 실행(cameras.json)으로 만든 것이다")
    rec = {"path": Path(path).name, "status": doc.get("status"), "reason": doc.get("reason")}
    if doc.get("status") != "OK":
        return None, rec
    M = np.asarray(doc["mapanything_to_world"], float)
    rec["scale_basis"] = (doc.get("calibration") or {}).get("scale_basis")
    rec["state"] = (doc.get("calibration") or {}).get("state")
    return M, rec


def transform_points(M: np.ndarray | None, X: np.ndarray) -> np.ndarray:
    return X if M is None else X @ M[:3, :3].T + M[:3, 3]


def transform_pose(M: np.ndarray | None, T: np.ndarray) -> np.ndarray:
    if M is None:
        return T
    s = np.cbrt(np.linalg.det(M[:3, :3]))
    out = np.eye(4)
    out[:3, :3] = (M[:3, :3] / s) @ T[:3, :3]
    out[:3, 3] = M[:3, :3] @ T[:3, 3] + M[:3, 3]
    return out


# --------------------------------------------------------------------------- 파일 쓰기


def write_ply(path: Path, xyz: np.ndarray, rgb: np.ndarray | None) -> None:
    n = len(xyz)
    props = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")] + (
        [("red", "u1"), ("green", "u1"), ("blue", "u1")] if rgb is not None else []
    )
    arr = np.empty(n, dtype=props)
    arr["x"], arr["y"], arr["z"] = xyz.T.astype(np.float32)
    if rgb is not None:
        arr["red"], arr["green"], arr["blue"] = rgb.T
    head = [
        "ply",
        "format binary_little_endian 1.0",
        f"element vertex {n}",
        "property float x",
        "property float y",
        "property float z",
    ]
    if rgb is not None:
        head += ["property uchar red", "property uchar green", "property uchar blue"]
    head.append("end_header")
    with open(path, "wb") as f:
        f.write(("\n".join(head) + "\n").encode("ascii"))
        f.write(arr.tobytes())


def grid_mesh(view: FuseView, stride: int, M: np.ndarray | None, edge_tol: float = EDGE_TOL):
    """한 카메라 깊이의 격자 메시(네 꼭짓점이 유효하고 깊이 폭이 edge_tol 안인 칸만). 반환 (정점, 면, 색)."""
    d = view.depth[::stride, ::stride]
    m = view.valid[::stride, ::stride] & np.isfinite(d)
    h, w = d.shape
    vv, uu = np.mgrid[0:h, 0:w]
    idx = -np.ones((h, w), np.int64)
    idx[m] = np.arange(int(m.sum()))
    X = view.backproject((vv[m] * stride), (uu[m] * stride))
    a, b, c, e = idx[:-1, :-1], idx[:-1, 1:], idx[1:, :-1], idx[1:, 1:]
    q = np.nan_to_num(
        np.stack([d[:-1, :-1], d[:-1, 1:], d[1:, :-1], d[1:, 1:]]), nan=0.0
    )  # 무효 칸은 아래 idx 조건이 뺀다
    ok = (
        (a >= 0)
        & (b >= 0)
        & (c >= 0)
        & (e >= 0)
        & ((q.max(0) - q.min(0)) <= edge_tol * q.min(0))
    )
    faces = np.concatenate(
        [np.column_stack([a[ok], c[ok], b[ok]]), np.column_stack([b[ok], c[ok], e[ok]])]
    )
    col = view.colors[::stride, ::stride][m] if view.colors is not None else None
    return transform_points(M, X), faces, col


def export_glb(out: Path, xyz: np.ndarray, rgb: np.ndarray | None, meshes: list) -> list[str]:
    """trimesh 가 있으면 점구름·격자 메시 glb. 없으면 쓰지 않고 그 사실을 돌려준다."""
    try:
        import trimesh  # type: ignore[import-not-found]
    except ImportError:
        return ["trimesh 없음: glb 를 쓰지 않았다(fused.ply 만)"]
    pc = trimesh.PointCloud(
        xyz,
        colors=None if rgb is None else np.column_stack([rgb, np.full(len(rgb), 255, np.uint8)]),
    )
    trimesh.Scene(pc).export(str(out / "pointcloud.glb"))
    if meshes:
        scene = trimesh.Scene()
        for name, (V, F, C) in meshes:
            if len(F):
                scene.add_geometry(
                    trimesh.Trimesh(V, F, vertex_colors=C, process=False), node_name=name
                )
        scene.export(str(out / "gridmesh.glb"))
    return []


# --------------------------------------------------------------------------- fuse 본체


def load_depth_dir(
    depth_dir: Path, prepared: dict[str, Any], cameras_sha: str
) -> tuple[dict[str, Any], dict[str, dict[str, np.ndarray]]]:
    raw = (Path(depth_dir) / "depth.json").read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    if doc.get("format") != DEPTH_FORMAT:
        raise ValueError(f"{depth_dir}: {DEPTH_FORMAT} 가 아니다")
    if doc.get("result") != "SUCCESS":
        raise ValueError(
            f"{depth_dir}: 깊이 결과가 SUCCESS 가 아니다({doc.get('result')}: {doc.get('error', '')[:200]})"
        )
    if doc.get("prepared_sha256") != prepared["_sha256"]:
        raise ValueError("깊이 폴더가 다른 입력 폴더로 만든 것이다(prepared_sha256)")
    if (doc.get("ma_run") or {}).get("cameras_json_sha256") != cameras_sha:
        raise ValueError("깊이 폴더가 다른 MapAnything 실행(cameras.json, 자세 입력)으로 만든 것이다")
    arrays = {}
    for v in doc["views"]:
        z = np.load(Path(depth_dir) / v["file"])
        arrays[v["name"]] = {k: z[k] for k in z.files}
    doc["_sha256"] = sha256_bytes(raw)
    return doc, arrays


def read_mask(prepared: dict[str, Any], view: dict[str, Any]) -> np.ndarray | None:
    if not view.get("mask"):
        return None
    import cv2  # type: ignore[import-not-found]

    m = cv2.imread(str(Path(prepared["_dir"]) / view["mask"]), cv2.IMREAD_GRAYSCALE)
    if m is None:
        raise ValueError(f"{view['name']}: 사람 마스크 {view['mask']} 를 읽지 못함")
    if [m.shape[1], m.shape[0]] != list(view["image_size"]):
        raise ValueError(f"{view['name']}: 마스크 크기가 영상과 다르다")
    return m > 127


def read_colors(prepared: dict[str, Any], view: dict[str, Any]) -> np.ndarray | None:
    import cv2  # type: ignore[import-not-found]

    img = cv2.imread(str(Path(prepared["_dir"]) / view["image"]))
    return None if img is None else cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


@dataclass
class FuseConfig:
    align: str = "scale"  # none / scale / scale_shift
    stride: int = 1
    voxel: float = 0.01
    mesh_stride: int = 4
    samples: int = 20_000
    colors: bool = True
    mesh: bool = True


def run_fuse(
    ma_run: Path,
    prepared: dict[str, Any],
    depth_dir: Path,
    out_dir: Path,
    cfg: FuseConfig,
    world_alignment: Path | None = None,
) -> dict[str, Any]:
    _ensure_simai_ai_on_path()
    from simai_ai.calibration.world_frame.inputs import load_mapanything_run

    t_start = time.perf_counter()
    ma_rec = check_run_matches_prepared(ma_run, prepared)
    cams, cameras_sha = read_cameras(ma_run)
    depth_doc, arrays = load_depth_dir(depth_dir, prepared, cameras_sha)
    M, wa_rec = (None, {"status": None})
    if world_alignment is not None:
        M, wa_rec = load_world_alignment(world_alignment, cameras_sha)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} 가 비어 있지 않다(결과를 덮어쓰지 않는다)")
    # 정합 기준: MapAnything 깊이(모델 해상도, 신뢰도 하위 10%·사람 마스크 뺌, world_frame fix 와 같은 규칙)
    ref = {v.name: v for v in load_mapanything_run(Path(ma_run), prepared=prepared).views}
    pviews = {v["name"]: v for v in prepared["views"]}
    dviews = {v["name"]: v for v in depth_doc["views"]}
    warnings: list[str] = []
    regs, views, edges, persons = {}, [], {}, {}
    for name in sorted(pviews):
        if name not in dviews or name not in ref:
            raise ValueError(f"view {name!r} 가 깊이 폴더나 MapAnything 결과에 없다")
        pv, a = pviews[name], arrays[name]
        depth = a["depth"].astype(np.float32)
        valid = a["valid"].astype(bool) & np.isfinite(depth) & (depth > 0)
        if list(depth.shape) != [pv["image_size"][1], pv["image_size"][0]]:
            raise ValueError(
                f"{name}: 깊이 크기 {depth.shape} 가 영상 크기 {pv['image_size']} 와 다르다"
            )
        mask = read_mask(prepared, pv)
        if mask is not None:
            persons[name] = int((valid & mask).sum())
            valid &= ~mask
        K = np.asarray(pv["K_undistorted"], float)
        r = ref[name]
        reg = register_to_reference(depth, valid, K, r.depth, r.valid, r.K, cfg.align)
        regs[name] = reg.record()
        edges[name] = edge_sharpness(depth, valid)
        if reg.status != "OK":
            warnings.append(f"{name}: 정합 실패({reg.reason}) → 융합에서 뺐다")
            continue
        depth = np.where(valid, reg.scale * depth + reg.shift, np.nan).astype(np.float32)
        valid &= depth > 0
        colors = read_colors(prepared, pv) if cfg.colors else None
        views.append(
            FuseView(name, depth, valid, K, np.asarray(cams[name]["cam2world"], float), colors)
        )
    if not views:
        raise RuntimeError("정합된 카메라가 없다")
    scales = [regs[v.name]["scale"] for v in views]
    consistency = cross_view_consistency(views, samples=cfg.samples)
    chunks, vis = visibility_filter(views, stride=cfg.stride)
    xyz_ma, rgb, cnt = voxel_merge(chunks, cfg.voxel)
    if not len(xyz_ma):
        raise RuntimeError("융합 뒤 점이 없다")
    xyz = transform_points(M, xyz_ma)
    poses_out = [transform_pose(M, v.cam2world) for v in views]
    floor = floor_metrics(xyz, poses_out)
    if M is not None:
        z = xyz[:, 2]
        near = np.abs(z) < 0.05
        floor["world_z0_rms"] = (
            round(float(np.sqrt(np.mean(z[near] ** 2))), 5) if near.any() else None
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    write_ply(out_dir / "fused.ply", xyz.astype(np.float32), rgb)
    meshes = [(v.name, grid_mesh(v, cfg.mesh_stride, M)) for v in views] if cfg.mesh else []
    warnings += export_glb(out_dir, xyz, rgb, meshes)
    total_px = sum(int(v.valid.sum()) for v in views)
    native = {v["name"]: v.get("native_hw") for v in depth_doc["views"]}
    frame = "WORLD" if M is not None else "MAPANYTHING_WORLD"
    rec = {
        "format": RESULT_FORMAT,
        "format_version": RESULT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": "S15P31A101-283",
        "experiment_id": "EXP-01",
        "status": "OK",
        "adapter": depth_doc.get("adapter"),
        "inputs": {
            "mapanything": ma_rec,
            "cameras_json_sha256": cameras_sha,
            "prepared_sha256": prepared["_sha256"],
            "depth_json_sha256": depth_doc["_sha256"],
            "world_alignment": wa_rec,
            "input_provenance": prepared.get("input_provenance"),
        },
        "output_frame": {
            "frame": frame,
            "unit": "m" if M is not None else "MapAnything 단위(모델 추정 m, 실측 미검증)",
            "note": (
                "World(+Z 상방, 바닥 Z=0), 보정 PROVISIONAL"
                if M is not None
                else "World 고정 결과가 없어 MapAnything world(cam_A 기준, OpenCV 축) 그대로다. World·Scene 으로 쓰지 않는다"
            ),
        },
        "config": cfg.__dict__.copy(),
        "registration": {
            "mode": cfg.align,
            "cameras": regs,
            "scale_spread": round(float(max(scales) / min(scales)), 5) if scales else None,
        },
        "consistency": consistency,
        "fusion": {
            "visibility": vis,
            "points_before_voxel": int(sum(len(c["xyz"]) for c in chunks)),
            "points": int(len(xyz)),
            "points_per_voxel": stats(cnt, 2),
            "voxel_m": cfg.voxel,
            "support_tol": SUPPORT_TOL,
            "conflict_tol": CONFLICT_TOL,
        },
        "floor": floor,
        "density": {
            "native_hw": native,
            "valid_pixels": total_px,
            "fused_points": int(len(xyz)),
            "native_px_per_sample": {
                k: round(pviews[k]["image_size"][0] / v[1], 3) for k, v in native.items() if v
            },
            "person_pixels_removed": persons or None,
        },
        "edges": edges,
        "performance": {
            "predict": {
                k: depth_doc.get("adapter", {}).get(k)
                for k in ("load_s", "infer_ms", "peak_alloc_mb")
            },
            "predict_total_ms": depth_doc.get("total_ms"),
            "fuse_s": round(time.perf_counter() - t_start, 1),
        },
        "g4": {"status": "NOT_MEASURED", "note": "개발 교실 줄자 실측이 없다(명세 5.3 G4 10 cm)"},
        "outputs": sorted(p.name for p in out_dir.iterdir()) + ["hybrid_metrics.json"],
        "warnings": warnings,
    }
    (out_dir / "hybrid_metrics.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return rec


# --------------------------------------------------------------------------- CLI


def make_adapter(a: argparse.Namespace):
    if a.adapter == "fake":
        scale = dict(_pair(s) for s in (a.fake_scale or []))
        return FakeMapAnythingAdapter(a.ma_run, scale={k: float(v) for k, v in scale.items()})
    if a.adapter in ("worldmirror2", "worldmirror1"):
        return WorldMirrorAdapter(
            variant=a.adapter,
            model=a.model,
            process_res=a.process_res,
            use_pose=not a.no_pose,
            use_prior_depth=bool(getattr(a, "prior_depth", False)),
            ma_run=a.ma_run,
            repo_dir=getattr(a, "repo_dir", None),
        )
    if a.adapter == "pi3x":
        return Pi3XAdapter(
            model=a.model,
            process_res=a.process_res,
            use_pose=not a.no_pose,
            use_prior_depth=bool(getattr(a, "prior_depth", False)),
            ma_run=a.ma_run,
            repo_dir=getattr(a, "repo_dir", None),
        )
    if not a.model:
        raise ValueError(f"--adapter {a.adapter} 는 --model 이 필요하다")
    if a.adapter == "da3":
        return DepthAnything3Adapter(
            a.model,
            process_res=int(a.process_res or DEFAULT_PROCESS_RES["da3"]),
            use_pose=not a.no_pose,
            use_ray_pose=a.ray_pose,
        )
    return MoGeAdapter(a.model)


def _pair(s: str) -> tuple[str, str]:
    if "=" not in s:
        raise ValueError(f"이름=값 이 아니다: {s!r}")
    k, v = s.split("=", 1)
    return k, v


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("predict", "fuse", "all"):
        p = sub.add_parser(cmd)
        p.add_argument(
            "--ma-run",
            type=Path,
            required=True,
            help="run_mapanything.py 출력(cameras.json, raw.npz, metrics.json)",
        )
        p.add_argument("--prepared", type=Path, required=True, help="world_frame.cli prepare 출력 폴더")
        p.add_argument("--out", type=Path, required=True, help="predict: 깊이 폴더, fuse·all: 결과 폴더")
        if cmd in ("predict", "all"):
            p.add_argument(
                "--adapter",
                choices=["fake", "da3", "moge", "worldmirror2", "worldmirror1", "pi3x"],
                required=True,
            )
            p.add_argument("--model", help="깊이 모델 가중치 폴더 또는 HF 모델 ID(저장 위치 weights/…)")
            p.add_argument(
                "--process-res",
                type=int,
                default=None,
                help="처리 해상도(긴 변 상한, 14 배수. 기본: da3=504, wm2=952, wm1=518, pi3x=672)",
            )
            p.add_argument("--no-pose", action="store_true", help="자세·K 조건 없이 추론(비교용)")
            p.add_argument(
                "--prior-depth",
                action="store_true",
                help="MapAnything 깊이를 사전 깊이 조건으로 함께 전달(worldmirror2·worldmirror1·pi3x)",
            )
            p.add_argument(
                "--ray-pose",
                action="store_true",
                help="da3 자세를 ray 헤드로(공식: 조금 느리고 더 정확)",
            )
            p.add_argument(
                "--repo-dir",
                type=Path,
                default=None,
                help="공식 저장소 클론 경로(HY-World-2.0, HunyuanWorld-Mirror, Pi3)",
            )
            p.add_argument(
                "--calibration",
                type=Path,
                default=None,
                help="calibration.json 경로(제공 시 MapAnything 자세 대신 calibration.json 자세를 조건으로 전달)",
            )
            p.add_argument("--fake-scale", nargs="*", help="fake: 카메라=배율(정합 점검용)")
        if cmd in ("fuse", "all"):
            if cmd == "fuse":
                p.add_argument("--depth", type=Path, required=True, help="predict 출력 폴더")
            p.add_argument("--align", choices=["none", "scale", "scale_shift"], default="scale")
            p.add_argument("--stride", type=int, default=1)
            p.add_argument("--voxel", type=float, default=0.01)
            p.add_argument("--mesh-stride", type=int, default=4)
            p.add_argument("--no-mesh", action="store_true")
            p.add_argument("--no-color", action="store_true")
            p.add_argument(
                "--world-alignment",
                type=Path,
                help="world_frame.cli fix 결과(status OK 면 World 로 출력)",
            )
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        prepared = load_prepared(a.prepared)
        if a.cmd in ("predict", "all"):
            depth_dir = a.out / "depth" if a.cmd == "all" else a.out
            if a.cmd == "all" and a.out.exists() and any(a.out.iterdir()):
                raise ValueError(f"{a.out} 가 비어 있지 않다(결과를 덮어쓰지 않는다)")
            doc = run_predict(
                make_adapter(a),
                a.ma_run,
                prepared,
                depth_dir,
                calibration=getattr(a, "calibration", None),
            )
            print(
                f"predict {doc['adapter']['adapter']}: {[v['name'] for v in doc['views']]} {doc['total_ms']} ms → {depth_dir}"
            )
        if a.cmd in ("fuse", "all"):
            depth_dir = a.depth if a.cmd == "fuse" else a.out / "depth"
            out_dir = a.out if a.cmd == "fuse" else a.out / "fused"
            cfg = FuseConfig(
                align=a.align,
                stride=a.stride,
                voxel=a.voxel,
                mesh_stride=a.mesh_stride,
                colors=not a.no_color,
                mesh=not a.no_mesh,
            )
            rec = run_fuse(a.ma_run, prepared, depth_dir, out_dir, cfg, a.world_alignment)
            _summary(rec)
    except ValueError as e:
        print(f"입력 오류: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except RuntimeError as e:
        print(f"결과 없음: {e}", file=sys.stderr)
        return EXIT_EMPTY
    return EXIT_OK


def _summary(rec: dict[str, Any]) -> None:
    regs = {k: v["scale"] for k, v in rec["registration"]["cameras"].items()}
    cons = {
        k: (v or {}).get("abs_rel_depth", {}).get("p50")
        for k, v in rec["consistency"]["cameras"].items()
    }
    rt = {
        k: ((v or {}).get("roundtrip_px") or {}).get("p50")
        for k, v in rec["consistency"]["cameras"].items()
    }
    print(f"frame {rec['output_frame']['frame']}, points {rec['fusion']['points']}, scale {regs}")
    print(f"cross-view |Δd|/d p50 {cons}, roundtrip px p50 {rt}")
    print(
        f"floor {rec['floor'].get('status')} rms {rec['floor'].get('rms')} density {rec['floor'].get('points_per_m2')}/m², "
        f"fuse {rec['performance']['fuse_s']} s, warnings {len(rec['warnings'])}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
