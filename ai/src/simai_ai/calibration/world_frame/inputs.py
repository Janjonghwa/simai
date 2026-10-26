"""MapAnything 실행 결과 → 카메라별 점(MapAnything world), 사람 마스크 적용, 카메라 간 재투영 일관성 (S15P31A101-102).

입력은 `run_mapanything.py` 출력 폴더의 `cameras.json`(카메라 → view 대응·cam2world)과 `raw.npz`(view 마다 depth_z·conf·
mask·intrinsics, 모델 해상도)다. camera_id ↔ view 대응과 단위 검사는 -214 의 `load_mapanything_cameras` 를 그대로 쓴다.

**사람 마스크.** `prepare` 가 만든 입력 폴더(`simai.mapanything_inputs`)를 함께 주면 view 마다 원본 해상도 사람 마스크를
MapAnything 전처리와 같은 규칙(`crop_resize_if_necessary`: 가로·세로 비 중 큰 쪽으로 덮도록 줄이고 가운데를 자름, map-anything
@3d10cf7 `utils/cropping.py`)으로 모델 해상도로 옮겨 그 픽셀의 점을 뺀다. 줄일 때 면적 평균이 0 보다 크면 사람으로 본다
(경계 픽셀도 뺀다).

**바닥 두 점(-277).** 설치자가 카메라 하나의 그림에서 찍은 두 점(원점, +X 방향 점)을 MapAnything world 광선으로 바꾼다.
기본은 원본(왜곡된) 그림 픽셀(`ORIGINAL`)이고 펴진 그림 픽셀(`PREPARED`)도 받는다. 원본 픽셀은 내부 파라미터 기록의 K·왜곡으로
펴서(`cv2.undistortPoints`, P = 입력 폴더의 펴진 K') 펴진 그림 픽셀로 옮기고(`prepare` 의 `initUndistortRectifyMap` 과 같은
규약), 사람 마스크와 같은 crop·resize 규칙으로 모델 해상도 픽셀로 옮겨 MapAnything 이 점을 만들 때 쓴 K(raw.npz intrinsics)와
그 카메라 자세로 광선을 만든다(바닥 평면을 만든 점과 같은 규약). 픽셀 좌표는 OpenCV 규약(왼쪽 위 픽셀 중심 = (0, 0))이다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.calibration.extrinsics.initial import load_mapanything_cameras
from simai_ai.calibration.world_frame.frame import FloorPointRays

PREPARED_FORMAT = "simai.mapanything_inputs"
FLOOR_POINTS_FORMAT = "simai.floor_points"
CAMERA_HEIGHTS_FORMAT = "simai.camera_heights"
PIXEL_SPACES = ("ORIGINAL", "PREPARED")


def mask_to_model_res(mask: np.ndarray, model_hw: Sequence[int], *, min_fraction: float | None = None) -> np.ndarray:
    """원본 해상도 bool 마스크 → MapAnything 모델 해상도(H, W). 덮도록 줄인 뒤 가운데 자르기.

    min_fraction 이 없으면 조금이라도 덮인 픽셀이 True(사람 마스크: 경계 픽셀도 뺀다), 있으면 덮인 면적 비율이 그 값 이상인
    픽셀만 True(실측 정물 -100: 0.5 면 픽셀 중심이 마스크 안인 픽셀과 거의 같다).
    """
    import cv2

    h, w = mask.shape
    th, tw = int(model_hw[0]), int(model_hw[1])
    rw, rh, left, top = _crop_resize(w, h, th, tw)
    small = cv2.resize(mask.astype(np.float32), (rw, rh), interpolation=cv2.INTER_AREA)[top:top + th, left:left + tw]
    return small > 0 if min_fraction is None else small >= min_fraction


def _crop_resize(w: int, h: int, th: int, tw: int) -> tuple[int, int, int, int]:
    """(줄인 폭, 줄인 높이, 왼쪽 자름, 위 자름). 덮도록 줄인 뒤 가운데 자르기(map-anything `crop_resize_if_necessary`)."""
    s = max(tw / w, th / h) + 1e-8
    rw, rh = int(w * s), int(h * s)
    left = int(np.round(0.5 * (w * s - tw)))
    top = int(np.round(0.5 * (h * s - th)))
    return rw, rh, min(max(left, 0), rw - tw), min(max(top, 0), rh - th)


def prepared_to_model_px(uv: np.ndarray, image_size: Sequence[int], model_hw: Sequence[int]) -> np.ndarray:
    """펴진(원본 해상도) 그림 픽셀 → 모델 해상도 픽셀. 마스크와 같은 crop·resize, 픽셀 중심 규약(u + 0.5 를 줄인다)."""
    w, h = int(image_size[0]), int(image_size[1])
    th, tw = int(model_hw[0]), int(model_hw[1])
    rw, rh, left, top = _crop_resize(w, h, th, tw)
    uv = np.asarray(uv, dtype=float).reshape(-1, 2)
    return np.column_stack([(uv[:, 0] + 0.5) * rw / w - 0.5 - left, (uv[:, 1] + 0.5) * rh / h - 0.5 - top])


@dataclass(frozen=True)
class View:
    camera_id: str
    name: str
    depth: np.ndarray  # (H, W) 카메라 z
    K: np.ndarray  # 모델 해상도
    cam2world: np.ndarray
    valid: np.ndarray  # (H, W) 쓸 점(MapAnything 마스크 ∧ 신뢰도 ∧ 사람 아님)
    person_pixels: int | None  # None = 사람 마스크를 적용하지 않음

    def points(self, valid: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
        m = self.valid if valid is None else valid
        v, u = np.nonzero(m)
        z = self.depth[v, u].astype(float)
        rays = np.linalg.solve(self.K, np.vstack([u + 0.0, v + 0.0, np.ones_like(z)]))
        Xc = rays * z
        X = (self.cam2world[:3, :3] @ Xc).T + self.cam2world[:3, 3]
        return X, np.column_stack([u, v]).astype(float)


@dataclass(frozen=True)
class MapAnythingRun:
    views: tuple[View, ...]
    record: dict[str, Any]

    def all_points(self) -> np.ndarray:
        return np.concatenate([v.points()[0] for v in self.views]) if self.views else np.zeros((0, 3))

    def cam2world(self) -> dict[str, np.ndarray]:
        return {v.camera_id: v.cam2world for v in self.views}


def load_prepared(path: Path) -> dict[str, Any]:
    """prepare 출력 폴더(또는 그 안의 prepared.json)."""
    path = Path(path)
    file = path / "prepared.json" if path.is_dir() else path
    doc = json.loads(file.read_text(encoding="utf-8"))
    if doc.get("format") != PREPARED_FORMAT:
        raise ValueError(f"{file.name}: MapAnything 입력 폴더 형식이 아니다: {doc.get('format')!r}")
    doc["_dir"] = str(file.parent)
    doc["_sha256"] = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
    return doc


def load_mapanything_run(
    run_dir: Path,
    *,
    view_map: Mapping[str, str] | None = None,
    prepared: Mapping[str, Any] | None = None,
    conf_percentile: float = 10.0,
) -> MapAnythingRun:
    run_dir = Path(run_dir)
    cams = load_mapanything_cameras(run_dir / "cameras.json", view_map)
    cam_doc = json.loads((run_dir / "cameras.json").read_text(encoding="utf-8"))
    poses = {v["name"]: np.asarray(v["cam2world"], dtype=float) for v in cam_doc["views"]}
    raw_path = run_dir / "raw.npz"
    raw = np.load(raw_path)
    prepared_views = {v["name"]: v for v in prepared["views"]} if prepared else {}
    views, per_view = [], []
    for cid, name in sorted(cams.record["view_map"].items()):
        depth = raw[f"{name}__depth_z"].astype(float)
        conf = raw[f"{name}__conf"].astype(float)
        valid = raw[f"{name}__mask"].astype(bool) & np.isfinite(depth) & (depth > 0)
        if valid.any() and conf_percentile > 0:
            valid &= conf >= np.percentile(conf[valid], conf_percentile)
        person_pixels = None
        if prepared:
            pv = prepared_views.get(name)
            if pv is None:
                raise ValueError(f"입력 폴더에 view {name!r} 가 없다(다른 입력으로 돌린 MapAnything 결과)")
            if pv.get("mask"):
                import cv2  # extras calibration(마스크를 쓸 때만)

                full = cv2.imread(str(Path(prepared["_dir"]) / pv["mask"]), cv2.IMREAD_GRAYSCALE)
                if full is None:
                    raise ValueError(f"{name}: 사람 마스크 {pv['mask']} 를 읽지 못함")
                if [full.shape[1], full.shape[0]] != list(pv["image_size"]):
                    raise ValueError(f"{name}: 마스크 크기 {full.shape[::-1]} 가 영상 크기 {pv['image_size']} 와 다르다")
                person = mask_to_model_res(full > 127, depth.shape)
                person_pixels = int((person & valid).sum())
                valid &= ~person
        views.append(View(cid, name, depth, raw[f"{name}__intrinsics"].astype(float), poses[name], valid, person_pixels))
        per_view.append({"camera_id": cid, "view": name, "model_hw": list(depth.shape), "points": int(valid.sum()),
                         "person_pixels_removed": person_pixels})
    record = {
        "run_dir": run_dir.name,
        "cameras_json": dict(cams.record),
        "raw_npz_sha256": "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "views": per_view,
        "person_masks_applied": bool(views) and all(v.person_pixels is not None for v in views),
        "prepared": None if not prepared else {"sha256": prepared["_sha256"], "format_version": prepared.get("format_version")},
    }
    metrics = run_dir / "metrics.json"
    if metrics.is_file():
        m = json.loads(metrics.read_text(encoding="utf-8"))
        keys = ("run_id", "model_version", "weights_hash", "input_hash", "result", "prepared_sha256")
        record["mapanything"] = {k: m.get(k) for k in keys}
        record["mapanything"]["mode"] = (m.get("args") or {}).get("mode")
    return MapAnythingRun(tuple(views), record)


def cross_view_reprojection(
    run: MapAnythingRun, *, samples: int = 4000, depth_tol: float = 0.05, seed: int = 0,
    original_width: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """카메라 간 왕복 재투영 오차(모델 해상도 px, 원본 폭을 알면 원본 px 로도).

    카메라 i 의 점을 카메라 j 에 투영해 j 의 깊이와 depth_tol(상대) 안이면 같은 표면으로 보고, 그 픽셀의 j 점을 다시 i 에
    투영해 처음 픽셀과의 거리를 잰다. 2D 관측으로 잰 재투영 오차가 아니라 MapAnything 깊이·자세의 카메라 간 일관성이다
    (계약 reprojection_error_px 에는 넣지 않는다. 관측 재투영 오차는 번들 조정 -214 몫).
    """
    rng = np.random.default_rng(seed)
    out: dict[str, Any] = {"method": "MAPANYTHING_CROSS_VIEW_ROUNDTRIP", "depth_tol": depth_tol, "cameras": {}}
    for vi in run.views:
        v_idx, u_idx = np.nonzero(vi.valid)
        if len(u_idx) == 0:
            out["cameras"][vi.camera_id] = None
            continue
        pick = rng.choice(len(u_idx), min(samples, len(u_idx)), replace=False)
        m = np.zeros_like(vi.valid)
        m[v_idx[pick], u_idx[pick]] = True
        X, pix = vi.points(m)
        errs = []
        for vj in run.views:
            if vj is vi:
                continue
            q, z = _project(vj, X)
            h, w = vj.depth.shape
            qi = np.round(q).astype(int)
            ok = (z > 0) & (qi[:, 0] >= 0) & (qi[:, 0] < w) & (qi[:, 1] >= 0) & (qi[:, 1] < h)
            idx = np.nonzero(ok)[0]
            ok2 = vj.valid[qi[idx, 1], qi[idx, 0]]
            idx = idx[ok2]
            zj = vj.depth[qi[idx, 1], qi[idx, 0]]
            same = np.abs(z[idx] - zj) <= depth_tol * zj
            idx, zj = idx[same], zj[same]
            if not len(idx):
                continue
            rays = np.linalg.solve(vj.K, np.vstack([qi[idx, 0] + 0.0, qi[idx, 1] + 0.0, np.ones(len(idx))]))
            Y = (vj.cam2world[:3, :3] @ (rays * zj)).T + vj.cam2world[:3, 3]
            back, _ = _project(vi, Y)
            errs.append(np.linalg.norm(back - pix[idx], axis=1))
        if not errs:
            out["cameras"][vi.camera_id] = None
            continue
        e = np.concatenate(errs)
        rec = {"pairs_points": int(len(e)), "median_model_px": round(float(np.median(e)), 3),
               "p95_model_px": round(float(np.percentile(e, 95)), 3)}
        if original_width and vi.camera_id in original_width:
            f = original_width[vi.camera_id] / vi.depth.shape[1]
            rec |= {"median_px": round(rec["median_model_px"] * f, 3), "p95_px": round(rec["p95_model_px"] * f, 3)}
        out["cameras"][vi.camera_id] = rec
    return out


def _project(view: View, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    Rcw, c = view.cam2world[:3, :3], view.cam2world[:3, 3]
    Xc = (X - c) @ Rcw  # = Rcw^T (X − c)
    z = Xc[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        uv = (Xc @ view.K.T)[:, :2] / z[:, None]
    return uv, z


def load_foot_points(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    """`{"format": "simai.foot_contacts", "points_mapanything": [[x,y,z], …], "input_provenance"?}` (MapAnything world)."""
    raw = Path(path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    if doc.get("format") != "simai.foot_contacts":
        raise ValueError(f"{Path(path).name}: 발 접지점 형식이 아니다: {doc.get('format')!r}")
    pts = np.asarray(doc.get("points_mapanything", []), dtype=float).reshape(-1, 3)
    return pts, {"path": Path(path).name, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                 "points": int(len(pts)), "input_provenance": doc.get("input_provenance")}


# --------------------------------------------------------------------------- 바닥 두 점·카메라 높이 (-277)


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and bool(np.isfinite(x))


def load_floor_points(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """`{"format": "simai.floor_points", "camera_id", "pixel_space"?: ORIGINAL|PREPARED (기본 ORIGINAL),
    "origin_px": [u, v], "x_axis_px": [u, v], "image"?, "note"?, "input_provenance"?}` → (문서, 입력 기록)."""
    raw = Path(path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    name = Path(path).name
    if doc.get("format") != FLOOR_POINTS_FORMAT:
        raise ValueError(f"{name}: 바닥 두 점 형식이 아니다: {doc.get('format')!r}")
    if not isinstance(doc.get("camera_id"), str) or not doc["camera_id"]:
        raise ValueError(f"{name}: camera_id 가 없다")
    space = doc.setdefault("pixel_space", "ORIGINAL")
    if space not in PIXEL_SPACES:
        raise ValueError(f"{name}: pixel_space 는 {PIXEL_SPACES} 중 하나다: {space!r}")
    for key in ("origin_px", "x_axis_px"):
        v = doc.get(key)
        if not (isinstance(v, list) and len(v) == 2 and all(_is_number(x) for x in v)):
            raise ValueError(f"{name}: {key} 는 유한한 수 두 개 [u, v] 다: {v!r}")
    return doc, {"path": name, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "camera_id": doc["camera_id"],
                 "pixel_space": space, "origin_px": doc["origin_px"], "x_axis_px": doc["x_axis_px"],
                 "image": doc.get("image"), "input_provenance": doc.get("input_provenance")}


def load_camera_heights(path: Path) -> tuple[dict[str, float], dict[str, Any]]:
    """실측 카메라 높이(바닥 → 렌즈 중심, m). `{"format": "simai.camera_heights", "heights_m": {"cam_A": 2.71, …},
    "method"?, "measured_at"?, "input_provenance"?}` 또는 맨 대응 `{"cam_A": 2.71, …}`."""
    raw = Path(path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    name = Path(path).name
    if not isinstance(doc, dict):
        raise ValueError(f"{name}: 카메라 높이는 JSON 객체다")
    if "format" in doc:
        if doc["format"] != CAMERA_HEIGHTS_FORMAT:
            raise ValueError(f"{name}: 카메라 높이 형식이 아니다: {doc['format']!r}")
        heights = doc.get("heights_m")
    else:
        heights, doc = doc, {"heights_m": doc}
    if not isinstance(heights, dict) or not heights:
        raise ValueError(f"{name}: 카메라 높이가 없다(heights_m)")
    out = {}
    for cid, v in heights.items():
        if not _is_number(v) or v <= 0:
            raise ValueError(f"{name}: {cid} 높이는 양의 유한한 수(m)다: {v!r}")
        out[str(cid)] = float(v)
    return out, {"path": name, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "heights_m": dict(out),
                 "method": doc.get("method"), "measured_at": doc.get("measured_at"),
                 "input_provenance": doc.get("input_provenance")}


def floor_point_rays(
    run: MapAnythingRun,
    prepared: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    K: np.ndarray,
    dist: np.ndarray,
    window: int = 2,
) -> FloorPointRays:
    """바닥 두 점(`load_floor_points` 문서) → MapAnything world 광선.

    픽셀이 그림 밖이거나 펴진 그림·MapAnything 입력(가운데 자른 범위) 밖이면 입력 오류다(MapAnything 이 보지 않은 곳).
    K·dist 는 입력 폴더를 만든 내부 파라미터 기록의 그 카메라 값이다(원본 픽셀을 펼 때만 쓴다). 각 점 근처(모델 픽셀
    ±window)의 MapAnything 점 중앙값을 함께 내어 frame 이 그 점이 바닥 위인지 확인하게 한다(점이 3개 미만이면 None).
    """
    cid = spec["camera_id"]
    view = next((v for v in run.views if v.camera_id == cid), None)
    if view is None:
        raise ValueError(f"바닥 두 점의 카메라 {cid!r} 가 MapAnything 결과에 없다")
    pv = next((v for v in prepared["views"] if v["name"] == view.name), None)
    if pv is None or not pv.get("K_undistorted"):
        raise ValueError(f"입력 폴더에 view {view.name!r} 의 펴진 K 가 없다")
    Kp = np.asarray(pv["K_undistorted"], dtype=float)
    w, h = (int(x) for x in pv["image_size"])
    px = np.array([spec["origin_px"], spec["x_axis_px"]], dtype=float)
    space = spec.get("pixel_space", "ORIGINAL")
    rec: dict[str, Any] = {"camera_id": cid, "view": view.name, "pixel_space": space, "pixels": px.tolist()}

    def inside(q: np.ndarray, width: int, height: int) -> bool:
        return bool(np.all((q[:, 0] >= -0.5) & (q[:, 0] <= width - 0.5) & (q[:, 1] >= -0.5) & (q[:, 1] <= height - 0.5)))

    if space == "ORIGINAL":
        import cv2  # extras calibration

        K, dist = np.asarray(K, dtype=float), np.asarray(dist, dtype=float)
        src = pv.get("source_camera") or {}
        if src and not (np.allclose(src["K"], K, rtol=1e-6) and np.allclose(src["distortion"]["coeffs"], dist, atol=1e-9)):
            raise ValueError(f"{cid}: 입력 폴더가 편 K·왜곡과 내부 파라미터 기록 값이 다르다")
        if not inside(px, w, h):
            raise ValueError(f"{cid}: 바닥 점 {px.tolist()} 가 원본 그림({w}×{h}) 밖이다")
        prep = cv2.undistortPoints(px.reshape(-1, 1, 2), K, dist, P=Kp).reshape(-1, 2)
    else:
        prep = px
    rec["prepared_px"] = np.round(prep, 3).tolist()
    if not inside(prep, w, h):
        raise ValueError(f"{cid}: 바닥 점이 펴진 그림 밖이다(펴진 픽셀 {rec['prepared_px']}, MapAnything 이 보지 않은 테두리). "
                         "그림 안쪽의 바닥 점을 고른다")
    hh, ww = view.depth.shape
    model = prepared_to_model_px(prep, (w, h), (hh, ww))
    rec["model_px"] = np.round(model, 3).tolist()
    if not inside(model, ww, hh):
        raise ValueError(f"{cid}: 바닥 점이 MapAnything 입력(가운데 자른 범위) 밖이다(모델 픽셀 {rec['model_px']})")
    ones = np.ones(2)
    rays = np.linalg.solve(view.K, np.vstack([model[:, 0], model[:, 1], ones]))  # 점구름과 같은 K(MapAnything 출력)
    rays_kp = np.linalg.solve(Kp, np.vstack([prep[:, 0], prep[:, 1], ones]))  # 펴진 K' 로 만든 광선(규약 확인용, 기록만)
    cos = np.sum(rays * rays_kp, axis=0) / (np.linalg.norm(rays, axis=0) * np.linalg.norm(rays_kp, axis=0))
    rec["model_k_vs_prepared_k_deg"] = [round(float(np.degrees(np.arccos(min(1.0, float(c))))), 4) for c in cos]
    R = view.cam2world[:3, :3]
    dirs = tuple((R @ r) / np.linalg.norm(r) for r in rays.T)
    observed, counts = [], []
    for u, v in np.round(model).astype(int):
        m = np.zeros_like(view.valid)
        m[max(v - window, 0):v + window + 1, max(u - window, 0):u + window + 1] = True
        m &= view.valid
        counts.append(int(m.sum()))
        observed.append(np.median(view.points(m)[0], axis=0) if m.sum() >= 3 else None)
    rec["observed_points"] = counts
    return FloorPointRays(cid, view.cam2world[:3, 3].copy(), (dirs[0], dirs[1]), (observed[0], observed[1]), rec)
