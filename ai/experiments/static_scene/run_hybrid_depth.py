"""EXP-01 혼합 경로: MapAnything 자세·K 고정 + 원본 해상도 깊이 → 정합·융합·지표 (S15P31A101-283).

설계는 docs/design/exp01-hybrid-depth.md. MapAnything(known-K, 모델 입력 518×294)의 자세(cam2world)는 그대로 두고, 깊이만
원본 해상도(펴진 2304×1296)에 가까운 모델로 다시 낸 뒤 카메라별로 MapAnything 깊이에 맞춰(축척, 선택적으로 이동) 한
점구름으로 합친다. 두 단계로 나눈다.

  predict  깊이 모델 어댑터만 돈다(GPU 세션). simai_ai 를 import 하지 않아 깊이 모델 전용 가상환경에서도 돈다.
           어댑터: fake(MapAnything 깊이를 원본 해상도로 올린 것, 노트북 경로 점검·기준선), da3(Depth Anything 3, 자세 조건),
           moge(MoGe-2 단안 + 보정 K). 출력 폴더(`simai.hybrid_depth`): depth.json + <view>.depth.npz(원본 해상도 z 깊이).
  fuse     정합·사람 마스크·융합·지표(노트북 CPU 가능, simai_ai 사용). 출력: hybrid_metrics.json, fused.ply,
           (trimesh 가 있으면) pointcloud.glb·gridmesh.glb.
  all      predict 뒤 fuse(노트북에서 fake 어댑터로 끝까지 시험).

    # 노트북(가짜 어댑터, 마스크·색에 OpenCV 필요)
    uv run --project ai --with opencv-python-headless python ai/experiments/static_scene/run_hybrid_depth.py all \\
        --ma-run <MapAnything Run 폴더> --prepared <prepare 출력 폴더> --adapter fake --out <결과 폴더>
    # GPU 세션(깊이만) → 노트북(fuse)
    python run_hybrid_depth.py predict --ma-run … --prepared … --adapter da3 --model <가중치 폴더> --out <깊이 폴더>
    uv run --project ai --with opencv-python-headless python …/run_hybrid_depth.py fuse --ma-run … --prepared … \\
        --depth <깊이 폴더> --out <결과 폴더> [--world-alignment <world_frame.cli fix 결과>]

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
    return {"n": int(len(x)), "p5": round(float(np.percentile(x, 5)), nd), "p50": round(float(np.median(x)), nd),
            "p95": round(float(np.percentile(x, 95)), nd), "mean": round(float(x.mean()), nd)}


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
        raise ValueError(f"MapAnything Run 의 prepared_sha256({m.get('prepared_sha256')}) 이 입력 폴더({prepared['_sha256']})와 다르다")
    return {k: m.get(k) for k in ("run_id", "model_version", "weights_hash", "input_hash", "prepared_sha256", "model_input_hw")}


def read_cameras(ma_run: Path) -> tuple[dict[str, dict[str, Any]], str]:
    raw = (Path(ma_run) / "cameras.json").read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    conv = str(doc.get("convention", ""))
    if "cam2world" not in conv or "opencv" not in conv.lower():
        raise ValueError(f"cameras.json convention 이 cam2world·OpenCV 가 아니다: {conv!r}")
    return {v["name"]: v for v in doc["views"]}, sha256_bytes(raw)


# --------------------------------------------------------------------------- 깊이 다시 표본(같은 카메라, 다른 K)


def sample_depth(src: np.ndarray, valid: np.ndarray, x: np.ndarray, y: np.ndarray,
                 edge_tol: float = EDGE_TOL) -> tuple[np.ndarray, np.ndarray]:
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
        bil = 1.0 / ((1 - fx) * (1 - fy) / d00 + fx * (1 - fy) / d01 + (1 - fx) * fy / d10 + fx * fy / d11)
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


def resample_depth(src: np.ndarray, src_valid: np.ndarray, K_src: np.ndarray, K_dst: np.ndarray,
                   dst_hw: tuple[int, int], edge_tol: float = EDGE_TOL) -> tuple[np.ndarray, np.ndarray]:
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

    def __init__(self, ma_run: Path, conf_percentile: float = 10.0, scale: dict[str, float] | None = None) -> None:
        self.raw = np.load(Path(ma_run) / "raw.npz")
        self.conf_percentile = conf_percentile
        self.scale = scale or {}  # 시험용: 카메라별로 깊이에 곱한다(정합이 되돌리는지)

    def describe(self) -> dict[str, Any]:
        return {"adapter": self.name, "model_version": "FAKE_UPSAMPLED_MAPANYTHING", "weights_hash": None,
                "pose_conditioned": True, "metric": "MAPANYTHING_UNITS", "conf_percentile": self.conf_percentile,
                "test_scale": self.scale or None,
                "note": "MapAnything 깊이를 올린 것. 깊이 모델 성능이 아니라 정합·융합·지표 경로 점검과 기준선이다"}

    def predict(self, views: list[PredictView]) -> list[DepthOut]:
        out = []
        for v in views:
            t0 = time.perf_counter()
            d = self.raw[f"{v.name}__depth_z"].astype(np.float32)
            K = self.raw[f"{v.name}__intrinsics"].astype(float)
            m = self.raw[f"{v.name}__mask"].astype(bool) & np.isfinite(d) & (d > 0)
            conf = self.raw[f"{v.name}__conf"].astype(np.float32) if f"{v.name}__conf" in self.raw else None
            if conf is not None and m.any() and self.conf_percentile > 0:
                m &= conf >= np.percentile(conf[m], self.conf_percentile)
            d = d * float(self.scale.get(v.name, 1.0))
            depth, valid = resample_depth(d, m, K, v.K, v.image_hw)
            out.append(DepthOut(depth, valid, None, tuple(d.shape),
                                {"infer_ms": round((time.perf_counter() - t0) * 1000, 1)}))
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

    def __init__(self, model: str, process_res: int = 504, process_res_method: str = "upper_bound_resize",
                 use_pose: bool = True, use_ray_pose: bool = False) -> None:
        self.model_path = model
        self.process_res = process_res
        self.process_res_method = process_res_method
        self.use_pose = use_pose
        self.use_ray_pose = use_ray_pose
        self._perf: dict[str, Any] = {}

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"adapter": self.name, "model_path": str(self.model_path), "process_res": self.process_res,
                                "process_res_method": self.process_res_method, "pose_conditioned": self.use_pose,
                                "use_ray_pose": self.use_ray_pose,
                                "metric": "INPUT_POSE_UNITS" if self.use_pose else "MODEL_METRIC_OR_RELATIVE"}
        try:
            from importlib.metadata import version
            info["package_version"] = version("depth_anything_3")
        except Exception:  # noqa: BLE001
            info["package_version"] = None
        info["commit"] = os.environ.get("SIMAI_DA3_COMMIT") or None
        info["weights_hash"] = _weights_hash(Path(self.model_path))
        return info | self._perf

    def predict(self, views: list[PredictView]) -> list[DepthOut]:
        import intrinsics_check  # 같은 폴더(스크립트 실행 시 sys.path 첫 항목), 표준 라이브러리·numpy 만(S15P31A101-312)
        import torch
        from depth_anything_3.api import DepthAnything3

        dev = torch.device("cuda")
        t0 = time.perf_counter()
        model = DepthAnything3.from_pretrained(self.model_path).to(dev).eval()
        torch.cuda.synchronize()
        load_s = time.perf_counter() - t0
        images = [str(v.image_path) for v in views]
        kw: dict[str, Any] = {"process_res": self.process_res, "process_res_method": self.process_res_method,
                              "use_ray_pose": self.use_ray_pose}
        w2c = np.stack([np.linalg.inv(v.cam2world) for v in views]).astype(np.float32)  # OpenCV world→camera (N, 4, 4)
        if self.use_pose:  # extrinsics 만 주고 intrinsics 를 빼면 공식 코드가 실패한다(둘 다 준다)
            kw |= {"extrinsics": w2c, "intrinsics": np.stack([v.K for v in views]).astype(np.float32)}
        times, pose_check = [], None
        for align in ((False, True) if self.use_pose else (None, None)):
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
        self._perf = {"load_s": round(load_s, 1), "infer_ms": times, "peak_alloc_mb": round(torch.cuda.max_memory_allocated() / 2**20, 1),
                      "pose_check_aligned_pred_vs_input": pose_check,
                      "is_metric": _plain(getattr(pred, "is_metric", None)), "scale_factor": _plain(getattr(pred, "scale_factor", None))}
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
            kv.append({"name": v.name, "output": Kd,
                       "expected": intrinsics_check.expected_axis_resize_k(v.K, v.image_hw[::-1], d.shape[::-1])})
            full, ok = resample_depth(d, m, Kd, v.K, v.image_hw)
            c = None
            if conf is not None:
                c, _ = resample_depth(conf[i], m, Kd, v.K, v.image_hw, edge_tol=np.inf)
            out.append(DepthOut(full, ok, c, tuple(d.shape), {"intrinsics_depth_grid": Kd.tolist(), "intrinsics_note": k_note}))
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


def k_for_depth_grid(K: np.ndarray, depth_hw: tuple[int, int], image_hw: tuple[int, int]) -> tuple[np.ndarray, str]:
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
    span = np.median(np.linalg.norm(cin[:, None] - cin[None], axis=-1)[np.triu_indices(len(cin), 1)]) if len(cin) > 1 else 1.0
    out = []
    for Ei, Eo in zip(w2c_in, w2c_out):
        dR = Ei[:3, :3] @ Eo[:3, :3].T
        ang = math.degrees(math.acos(float(np.clip((np.trace(dR) - 1) / 2, -1, 1))))
        out.append({"rot_deg": round(ang, 3), "center_rel": round(float(np.linalg.norm(center(Ei) - center(Eo)) / span), 4)})
    return out


class MoGeAdapter:
    """MoGe-2 단안(GPU 세션 전용). 카메라마다 따로, 펴진 K 의 가로 화각을 fov_x 로 준다. 자세는 쓰지 않는다(정합이 축척을 맞춘다)."""

    name = "moge"

    def __init__(self, model: str, resolution_level: int = 9) -> None:
        self.model_path = model
        self.resolution_level = resolution_level
        self._perf: dict[str, Any] = {}

    def describe(self) -> dict[str, Any]:
        return {"adapter": self.name, "model_path": str(self.model_path), "pose_conditioned": False,
                "resolution_level": self.resolution_level, "metric": "MODEL_METRIC",
                "weights_hash": _weights_hash(Path(self.model_path))} | self._perf

    def predict(self, views: list[PredictView]) -> list[DepthOut]:
        import cv2
        import torch
        from moge.model.v2 import MoGeModel

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
        self._perf = {"infer_ms": times, "peak_alloc_mb": round(torch.cuda.max_memory_allocated() / 2**20, 1)}
        return out


def _weights_hash(path: Path) -> str | None:
    files = sorted(p for p in (path.glob("*.safetensors") if path.is_dir() else [path]) if p.is_file())
    if not files:
        return None
    return sha256_bytes("".join(f"{f.name}:{sha256_file(f)}\n" for f in files).encode())


def predict_views(ma_run: Path, prepared: dict[str, Any]) -> list[PredictView]:
    cams, _ = read_cameras(ma_run)
    out = []
    for v in prepared["views"]:
        if v["name"] not in cams:
            raise ValueError(f"cameras.json 에 view {v['name']!r} 가 없다")
        w, h = v["image_size"]
        out.append(PredictView(v["name"], Path(prepared["_dir"]) / v["image"], (int(h), int(w)),
                               np.asarray(v["K_undistorted"], float), np.asarray(cams[v["name"]]["cam2world"], float)))
    return out


def run_predict(adapter, ma_run: Path, prepared: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    views = predict_views(ma_run, prepared)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} 가 비어 있지 않다(결과를 덮어쓰지 않는다)")
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    head: dict[str, Any] = {"format": DEPTH_FORMAT, "format_version": DEPTH_VERSION,
                            "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        results = adapter.predict(views)
    except Exception as e:  # noqa: BLE001  GPU 세션: OOM·적재 실패도 결과로 남긴다(2304 처리 등)
        kind = "OOM" if "OutOfMemory" in type(e).__name__ else "ERROR"
        fail = head | {"result": kind, "error": f"{type(e).__name__}: {e}"[:800], "adapter": adapter.describe(),
                       "prepared_sha256": prepared["_sha256"]}
        (out_dir / "depth.json").write_text(json.dumps(fail, ensure_ascii=False, indent=1), encoding="utf-8")
        raise RuntimeError(f"깊이 모델 {kind}: {fail['error'][:200]}") from e
    total_ms = round((time.perf_counter() - t0) * 1000, 1)
    doc: dict[str, Any] = head | {"result": "SUCCESS", "adapter": adapter.describe(), "total_ms": total_ms,
                           "ma_run": {"dir": Path(ma_run).name, "cameras_json_sha256": read_cameras(ma_run)[1]},
                           "prepared_sha256": prepared["_sha256"], "depth": "z(카메라 앞), 원본(펴진) 해상도, 무효 NaN", "views": []}
    for v, r in zip(views, results):
        file = f"{v.name}.depth.npz"
        arrays = {"depth": r.depth.astype(np.float32), "valid": r.valid}
        if r.conf is not None:
            arrays["conf"] = r.conf.astype(np.float32)
        np.savez_compressed(out_dir / file, **arrays)
        doc["views"].append({"name": v.name, "file": file, "image_hw": list(v.image_hw), "K": v.K.tolist(),
                             "native_hw": list(r.native_hw), "valid_ratio": round(float(r.valid.mean()), 4), **r.meta})
    (out_dir / "depth.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
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
        return {"status": self.status, "reason": self.reason,
                "scale": None if self.scale is None else round(self.scale, 5),
                "shift": None if self.shift is None else round(self.shift, 5), "pairs": self.pairs,
                "inlier_ratio": round(self.inlier_ratio, 4),
                "abs_rel_err_before": self.rel_err_before, "abs_rel_err_after": self.rel_err_after}


def fit_scale_shift(d: np.ndarray, ref: np.ndarray, mode: str, tol: float = REG_TOL,
                    min_pairs: int = REG_MIN_PAIRS, min_inlier: float = REG_MIN_INLIER) -> Registration:
    """ref ≈ s·d (+ t). 축척은 로그 비의 중앙값에서 시작해 인라이어(|ref − s·d − t| ≤ tol·ref)로 다시 맞춘다."""
    d = np.asarray(d, float)
    ref = np.asarray(ref, float)
    ok = np.isfinite(d) & np.isfinite(ref) & (d > 0) & (ref > 0)
    d, ref = d[ok], ref[ok]
    before = stats(np.abs(d - ref) / ref)
    if len(d) < min_pairs:
        return Registration("FAILED", None, None, int(len(d)), 0.0, before, {}, f"정합 표본 {len(d)} < {min_pairs}")
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
                    return Registration("FAILED", None, None, int(len(d)), 0.0, before, {}, "축척이 0 이하")
    inl = np.abs(ref - (s * d + t)) <= tol * ref
    ratio = float(inl.mean())
    after = stats(np.abs(s * d + t - ref) / ref)
    if ratio < min_inlier:
        return Registration("FAILED", s, t, int(len(d)), ratio, before, after, f"인라이어 {ratio:.2f} < {min_inlier}")
    return Registration("OK", s, t, int(len(d)), ratio, before, after)


def register_to_reference(depth: np.ndarray, valid: np.ndarray, K: np.ndarray, ref_depth: np.ndarray,
                          ref_valid: np.ndarray, ref_K: np.ndarray, mode: str, max_pairs: int = 200_000,
                          seed: int = 0) -> Registration:
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


def cross_view_consistency(views: list[FuseView], samples: int = 20_000, covis_tol: float = 0.20,
                           roundtrip_tol: float = 0.05, seed: int = 0) -> dict[str, Any]:
    """카메라 간 깊이 일관성(원본 px). i 의 점을 j 에 투영해 j 깊이와의 상대 차 |z − d_j|/d_j 를 잰다(0.2 안 = 같이 보임으로
    본다, 넘으면 가림). 5% 안이면 j 점을 i 로 되돌려 처음 픽셀과의 거리(왕복 재투영, world_frame 의 지표를 원본 해상도에서)."""
    rng = np.random.default_rng(seed)
    out: dict[str, Any] = {"method": "CROSS_VIEW_DEPTH", "covis_tol": covis_tol, "roundtrip_tol": roundtrip_tol, "cameras": {}}
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
            dj, okj = sample_depth(vj.depth, vj.valid, np.where(front, q[:, 0], -1), np.where(front, q[:, 1], -1))
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
            "covisible_points": int(len(rel)), "abs_rel_depth": stats(rel),
            "within_1pct": round(float(np.mean(rel <= 0.01)), 4), "within_5pct": round(float(np.mean(rel <= 0.05)), 4),
            "roundtrip_px": stats(rt, 3) if len(rt) else None,
        }
    return out


# --------------------------------------------------------------------------- 융합


def visibility_filter(views: list[FuseView], stride: int = 1, support_tol: float = SUPPORT_TOL,
                      conflict_tol: float = CONFLICT_TOL) -> tuple[list[dict[str, np.ndarray]], dict[str, Any]]:
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
        rec[vi.name] = {"points": int(len(u)), "kept": int(keep.sum()),
                        "dropped_conflict_ratio": round(float((~keep).sum() / n), 4),
                        "supported_ratio": round(float((support > 0).sum() / n), 4),
                        "single_view_ratio": round(float((seen == 0).sum() / n), 4)}
        col = vi.colors[v[keep], u[keep]] if vi.colors is not None else None
        kept.append({"xyz": X[keep], "rgb": col, "support": support[keep]})
    return kept, rec


def voxel_merge(chunks: list[dict[str, np.ndarray]], voxel: float) -> tuple[np.ndarray, np.ndarray | None, np.ndarray]:
    """칸(voxel m)마다 점 평균·색 평균. 반환 (xyz, rgb 또는 None, 칸의 점 수)."""
    xyz = np.concatenate([c["xyz"] for c in chunks]) if chunks else np.zeros((0, 3))
    has_rgb = bool(chunks) and all(c["rgb"] is not None for c in chunks)
    rgb = np.concatenate([c["rgb"] for c in chunks]).astype(np.float64) if has_rgb else None
    if not len(xyz) or voxel <= 0:
        return xyz, (rgb.astype(np.uint8) if rgb is not None else None), np.ones(len(xyz), np.int64)
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


def edge_sharpness(depth: np.ndarray, valid: np.ndarray, jump: float = FLYING_JUMP, gap: float = FLYING_GAP) -> dict[str, Any]:
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
            n = d[1 + dy:h - 1 + dy, 1 + dx:w - 1 + dx]
            allv &= np.isfinite(n)
            lo = np.fmin(lo, n)
            hi = np.fmax(hi, n)
    with np.errstate(invalid="ignore"):
        disc = allv & ((hi - lo) > jump * lo)
        flying = disc & (c > lo * (1 + gap)) & (c < hi * (1 - gap))
    nd = int(disc.sum())
    return {"discontinuity_pixels": nd, "discontinuity_ratio": round(nd / max(int(allv.sum()), 1), 5),
            "flying_ratio": round(float(flying.sum()) / nd, 4) if nd else None}


def floor_metrics(xyz: np.ndarray, cam2world: list[np.ndarray], seed: int = 0, max_points: int = 200_000,
                  cell: float = 0.05) -> dict[str, Any]:
    """출력 좌표의 점에서 world_frame 과 같은 규칙(find_floor)으로 바닥을 찾고 rms(3 cm 안 인라이어)·면 밀도를 잰다."""
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
    return {"status": "OK", "normal": np.round(n, 5).tolist(), "offset": round(float(floor.offset), 4),
            "rms": round(float(np.sqrt(np.mean(dist[inl] ** 2))), 5), "inliers": int(inl.sum()),
            "occupied_area_m2": round(area, 2), "points_per_m2": round(float(inl.sum()) / area, 1) if area else None,
            "camera_height": stats(floor.distance(centers), 3), "plane_tol": cfg.plane_tol_m, "cell": cell}


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
    props = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")] + ([("red", "u1"), ("green", "u1"), ("blue", "u1")] if rgb is not None else [])
    arr = np.empty(n, dtype=props)
    arr["x"], arr["y"], arr["z"] = xyz.T.astype(np.float32)
    if rgb is not None:
        arr["red"], arr["green"], arr["blue"] = rgb.T
    head = ["ply", "format binary_little_endian 1.0", f"element vertex {n}",
            "property float x", "property float y", "property float z"]
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
    q = np.nan_to_num(np.stack([d[:-1, :-1], d[:-1, 1:], d[1:, :-1], d[1:, 1:]]), nan=0.0)  # 무효 칸은 아래 idx 조건이 뺀다
    ok = (a >= 0) & (b >= 0) & (c >= 0) & (e >= 0) & ((q.max(0) - q.min(0)) <= edge_tol * q.min(0))
    faces = np.concatenate([np.column_stack([a[ok], c[ok], b[ok]]), np.column_stack([b[ok], c[ok], e[ok]])])
    col = view.colors[::stride, ::stride][m] if view.colors is not None else None
    return transform_points(M, X), faces, col


def export_glb(out: Path, xyz: np.ndarray, rgb: np.ndarray | None, meshes: list) -> list[str]:
    """trimesh 가 있으면 점구름·격자 메시 glb. 없으면 쓰지 않고 그 사실을 돌려준다."""
    try:
        import trimesh
    except ImportError:
        return ["trimesh 없음: glb 를 쓰지 않았다(fused.ply 만)"]
    pc = trimesh.PointCloud(xyz, colors=None if rgb is None else np.column_stack([rgb, np.full(len(rgb), 255, np.uint8)]))
    trimesh.Scene(pc).export(str(out / "pointcloud.glb"))
    if meshes:
        scene = trimesh.Scene()
        for name, (V, F, C) in meshes:
            if len(F):
                scene.add_geometry(trimesh.Trimesh(V, F, vertex_colors=C, process=False), node_name=name)
        scene.export(str(out / "gridmesh.glb"))
    return []


# --------------------------------------------------------------------------- fuse 본체


def load_depth_dir(depth_dir: Path, prepared: dict[str, Any], cameras_sha: str) -> tuple[dict[str, Any], dict[str, dict[str, np.ndarray]]]:
    raw = (Path(depth_dir) / "depth.json").read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    if doc.get("format") != DEPTH_FORMAT:
        raise ValueError(f"{depth_dir}: {DEPTH_FORMAT} 가 아니다")
    if doc.get("result") != "SUCCESS":
        raise ValueError(f"{depth_dir}: 깊이 결과가 SUCCESS 가 아니다({doc.get('result')}: {doc.get('error', '')[:200]})")
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
    import cv2

    m = cv2.imread(str(Path(prepared["_dir"]) / view["mask"]), cv2.IMREAD_GRAYSCALE)
    if m is None:
        raise ValueError(f"{view['name']}: 사람 마스크 {view['mask']} 를 읽지 못함")
    if [m.shape[1], m.shape[0]] != list(view["image_size"]):
        raise ValueError(f"{view['name']}: 마스크 크기가 영상과 다르다")
    return m > 127


def read_colors(prepared: dict[str, Any], view: dict[str, Any]) -> np.ndarray | None:
    import cv2

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


def run_fuse(ma_run: Path, prepared: dict[str, Any], depth_dir: Path, out_dir: Path, cfg: FuseConfig,
             world_alignment: Path | None = None) -> dict[str, Any]:
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
            raise ValueError(f"{name}: 깊이 크기 {depth.shape} 가 영상 크기 {pv['image_size']} 와 다르다")
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
        views.append(FuseView(name, depth, valid, K, np.asarray(cams[name]["cam2world"], float), colors))
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
        floor["world_z0_rms"] = round(float(np.sqrt(np.mean(z[near] ** 2))), 5) if near.any() else None
    out_dir.mkdir(parents=True, exist_ok=True)
    write_ply(out_dir / "fused.ply", xyz.astype(np.float32), rgb)
    meshes = [(v.name, grid_mesh(v, cfg.mesh_stride, M)) for v in views] if cfg.mesh else []
    warnings += export_glb(out_dir, xyz, rgb, meshes)
    total_px = sum(int(v.valid.sum()) for v in views)
    native = {v["name"]: v.get("native_hw") for v in depth_doc["views"]}
    frame = ("WORLD" if M is not None else "MAPANYTHING_WORLD")
    rec = {
        "format": RESULT_FORMAT, "format_version": RESULT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "jira_key": "S15P31A101-283", "experiment_id": "EXP-01",
        "status": "OK",
        "adapter": depth_doc.get("adapter"),
        "inputs": {"mapanything": ma_rec, "cameras_json_sha256": cameras_sha, "prepared_sha256": prepared["_sha256"],
                   "depth_json_sha256": depth_doc["_sha256"], "world_alignment": wa_rec,
                   "input_provenance": prepared.get("input_provenance")},
        "output_frame": {"frame": frame, "unit": "m" if M is not None else "MapAnything 단위(모델 추정 m, 실측 미검증)",
                         "note": ("World(+Z 상방, 바닥 Z=0), 보정 PROVISIONAL" if M is not None else
                                  "World 고정 결과가 없어 MapAnything world(cam_A 기준, OpenCV 축) 그대로다. World·Scene 으로 쓰지 않는다")},
        "config": cfg.__dict__.copy(),
        "registration": {"mode": cfg.align, "cameras": regs,
                         "scale_spread": round(float(max(scales) / min(scales)), 5) if scales else None},
        "consistency": consistency,
        "fusion": {"visibility": vis, "points_before_voxel": int(sum(len(c["xyz"]) for c in chunks)),
                   "points": int(len(xyz)), "points_per_voxel": stats(cnt, 2), "voxel_m": cfg.voxel,
                   "support_tol": SUPPORT_TOL, "conflict_tol": CONFLICT_TOL},
        "floor": floor,
        "density": {"native_hw": native, "valid_pixels": total_px, "fused_points": int(len(xyz)),
                    "native_px_per_sample": {k: round(pviews[k]["image_size"][0] / v[1], 3) for k, v in native.items() if v},
                    "person_pixels_removed": persons or None},
        "edges": edges,
        "performance": {"predict": {k: depth_doc.get("adapter", {}).get(k) for k in ("load_s", "infer_ms", "peak_alloc_mb")},
                        "predict_total_ms": depth_doc.get("total_ms"), "fuse_s": round(time.perf_counter() - t_start, 1)},
        "g4": {"status": "NOT_MEASURED", "note": "개발 교실 줄자 실측이 없다(명세 5.3 G4 10 cm)"},
        "outputs": sorted(p.name for p in out_dir.iterdir()) + ["hybrid_metrics.json"],
        "warnings": warnings,
    }
    (out_dir / "hybrid_metrics.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


# --------------------------------------------------------------------------- CLI


def make_adapter(a: argparse.Namespace):
    if a.adapter == "fake":
        scale = dict(_pair(s) for s in (a.fake_scale or []))
        return FakeMapAnythingAdapter(a.ma_run, scale={k: float(v) for k, v in scale.items()})
    if not a.model:
        raise ValueError(f"--adapter {a.adapter} 는 --model 이 필요하다")
    if a.adapter == "da3":
        return DepthAnything3Adapter(a.model, process_res=a.process_res, use_pose=not a.no_pose, use_ray_pose=a.ray_pose)
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
        p.add_argument("--ma-run", type=Path, required=True, help="run_mapanything.py 출력(cameras.json, raw.npz, metrics.json)")
        p.add_argument("--prepared", type=Path, required=True, help="world_frame.cli prepare 출력 폴더")
        p.add_argument("--out", type=Path, required=True, help="predict: 깊이 폴더, fuse·all: 결과 폴더")
        if cmd in ("predict", "all"):
            p.add_argument("--adapter", choices=["fake", "da3", "moge"], required=True)
            p.add_argument("--model", help="da3·moge 가중치 폴더(저장 위치 weights/…)")
            p.add_argument("--process-res", type=int, default=504, help="da3 처리 해상도(긴 변 상한)")
            p.add_argument("--no-pose", action="store_true", help="da3 를 자세 없이(비교용)")
            p.add_argument("--ray-pose", action="store_true", help="da3 자세를 ray 헤드로(공식: 조금 느리고 더 정확)")
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
            p.add_argument("--world-alignment", type=Path, help="world_frame.cli fix 결과(status OK 면 World 로 출력)")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        prepared = load_prepared(a.prepared)
        if a.cmd in ("predict", "all"):
            depth_dir = a.out / "depth" if a.cmd == "all" else a.out
            if a.cmd == "all" and a.out.exists() and any(a.out.iterdir()):
                raise ValueError(f"{a.out} 가 비어 있지 않다(결과를 덮어쓰지 않는다)")
            doc = run_predict(make_adapter(a), a.ma_run, prepared, depth_dir)
            print(f"predict {doc['adapter']['adapter']}: {[v['name'] for v in doc['views']]} {doc['total_ms']} ms → {depth_dir}")
        if a.cmd in ("fuse", "all"):
            depth_dir = a.depth if a.cmd == "fuse" else a.out / "depth"
            out_dir = a.out if a.cmd == "fuse" else a.out / "fused"
            cfg = FuseConfig(align=a.align, stride=a.stride, voxel=a.voxel, mesh_stride=a.mesh_stride,
                             colors=not a.no_color, mesh=not a.no_mesh)
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
    cons = {k: (v or {}).get("abs_rel_depth", {}).get("p50") for k, v in rec["consistency"]["cameras"].items()}
    rt = {k: ((v or {}).get("roundtrip_px") or {}).get("p50") for k, v in rec["consistency"]["cameras"].items()}
    print(f"frame {rec['output_frame']['frame']}, points {rec['fusion']['points']}, scale {regs}")
    print(f"cross-view |Δd|/d p50 {cons}, roundtrip px p50 {rt}")
    print(f"floor {rec['floor'].get('status')} rms {rec['floor'].get('rms')} density {rec['floor'].get('points_per_m2')}/m², "
          f"fuse {rec['performance']['fuse_s']} s, warnings {len(rec['warnings'])}")


if __name__ == "__main__":
    raise SystemExit(main())
