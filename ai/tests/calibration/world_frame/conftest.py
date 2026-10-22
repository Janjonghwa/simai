"""World 고정 시험용 합성 교실(SIM): 상자 방을 광선 추적해 MapAnything 결과 폴더(cameras.json, raw.npz)를 만든다.

MapAnything world 는 첫 카메라(cam_A)의 OpenCV 축이고 길이는 SCALE_MA 배다(모델 메트릭 오차 흉내). 정답 World 는 방 모서리
(0, 0, 0), 방은 [0, L]×[0, W]×[0, H] 이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

ROOM = (8.0, 6.5, 2.9)
HW = (126, 224)  # 모델 해상도
ORIG_FACTOR = 4  # 원본 해상도 = 모델 × 4 (사람 마스크)
CAMS = {
    "cam_A": (0.35, 0.35, 2.6),
    "cam_B": (7.65, 0.35, 2.55),
    "cam_C": (7.65, 6.15, 2.65),
    "cam_D": (0.35, 6.15, 2.6),
}
TARGET = (4.0, 3.25, 0.6)
DESK = ((3.0, 2.5, 0.0), (5.0, 3.7, 0.75))
PERSON = ((5.6, 1.8, 0.0), (6.1, 2.2, 1.7))


def look(center, target):
    f = np.subtract(target, center) / np.linalg.norm(np.subtract(target, center))
    r = np.cross(f, [0, 0, 1.0])
    r /= np.linalg.norm(r)
    return np.vstack([r, np.cross(f, r), f])  # world → camera


def intrinsics(hw=HW, fov_deg=85.0):
    h, w = hw
    f = 0.5 * w / np.tan(np.radians(fov_deg / 2))
    return np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])


def _box_hit(o, d, lo, hi):
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = (np.asarray(lo) - o) / d
        t2 = (np.asarray(hi) - o) / d
    tmin = np.nanmax(np.minimum(t1, t2), axis=1)
    tmax = np.nanmin(np.maximum(t1, t2), axis=1)
    hit = (tmax >= tmin) & (tmax > 0)
    return np.where(hit, np.maximum(tmin, 0), np.inf)


def raycast(R, c, K, hw, *, desk=True, person=True):
    """(depth_z, label) — label 0 바닥, 1 벽 x=0, 2 x=L, 3 y=0, 4 y=W, 5 천장, 6 책상, 7 사람."""
    h, w = hw
    v, u = np.mgrid[0:h, 0:w]
    rc = np.linalg.solve(K, np.vstack([u.ravel() + 0.0, v.ravel() + 0.0, np.ones(h * w)]))  # z = 1
    d = (R.T @ rc).T
    o = np.asarray(c, dtype=float)
    L, W, H = ROOM
    planes = [(2, 0.0), (0, 0.0), (0, L), (1, 0.0), (1, W), (2, H)]
    ts = []
    for axis, val in planes:
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (val - o[axis]) / d[:, axis]
        t = np.where(t > 1e-6, t, np.inf)
        ts.append(t)
    ts.append(_box_hit(o, d, *DESK) if desk else np.full(h * w, np.inf))
    ts.append(_box_hit(o, d, *PERSON) if person else np.full(h * w, np.inf))
    T = np.vstack(ts)
    label = np.argmin(T, axis=0)
    depth = T[label, np.arange(h * w)]  # rc 의 z = 1 이라 t = depth_z
    return depth.reshape(h, w), label.reshape(h, w)


@dataclass
class SyntheticRun:
    run_dir: Path
    world_to_ma: np.ndarray  # 4×4, X_ma = M X_world
    labels: dict = field(default_factory=dict)
    prepared_dir: Path | None = None


def make_run(tmp_path: Path, *, scale_ma=1.04, drop_labels=(), desk=True, person=True, masks=False,
             name="run") -> SyntheticRun:
    run_dir = tmp_path / name
    run_dir.mkdir()
    K = intrinsics()
    RA = look(CAMS["cam_A"], TARGET)
    tA = -RA @ np.asarray(CAMS["cam_A"])
    M = np.eye(4)
    M[:3, :3] = scale_ma * RA
    M[:3, 3] = scale_ma * tA
    raw, views, labels, prepared_views = {}, [], {}, []
    if masks:
        prep = tmp_path / f"{name}_prepared"
        prep.mkdir()
    for cid, c in CAMS.items():
        R = look(c, TARGET)
        depth, label = raycast(R, c, K, HW, desk=desk, person=person)
        mask = ~np.isin(label, list(drop_labels))
        T = np.eye(4)
        T[:3, :3] = RA @ R.T
        T[:3, 3] = (M @ np.append(c, 1.0))[:3]
        name_v = f"{cid}_01"
        raw[f"{name_v}__depth_z"] = (scale_ma * depth).astype(np.float32)
        raw[f"{name_v}__conf"] = np.ones(HW, np.float32) * 2
        raw[f"{name_v}__mask"] = mask
        raw[f"{name_v}__intrinsics"] = K.astype(np.float32)
        raw[f"{name_v}__camera_pose"] = T.astype(np.float32)
        views.append({"name": name_v, "model_input_hw": list(HW), "intrinsics_model_res": K.tolist(), "cam2world": T.tolist()})
        labels[cid] = label
        if masks:
            import cv2

            big = np.kron((label == 7).astype(np.uint8), np.ones((ORIG_FACTOR, ORIG_FACTOR), np.uint8)) * 255
            cv2.imwrite(str(prep / f"{name_v}.mask.png"), big)
            prepared_views.append({"name": name_v, "camera_id": cid, "image": f"{name_v}.jpg", "mask": f"{name_v}.mask.png",
                                   "image_size": [HW[1] * ORIG_FACTOR, HW[0] * ORIG_FACTOR]})
    np.savez_compressed(run_dir / "raw.npz", names=np.array([v["name"] for v in views]), **raw)
    (run_dir / "cameras.json").write_text(json.dumps({
        "run_id": f"SIM_{name}", "convention": "cam2world 4x4, OpenCV, MapAnything world, m (NOT_VERIFIED)", "views": views,
    }), encoding="utf-8")
    run = SyntheticRun(run_dir, M, labels)
    if masks:
        (prep / "prepared.json").write_text(json.dumps({
            "format": "simai.mapanything_inputs", "format_version": "0.1.0",
            "input_provenance": {"frames": {"source": "SIM", "dataset_id": "sim/world-frame"}, "is_upper_bound": True},
            "person_masks": True, "views": prepared_views,
        }), encoding="utf-8")
        run.prepared_dir = prep
        import hashlib

        sha = "sha256:" + hashlib.sha256((prep / "prepared.json").read_bytes()).hexdigest()
        (run_dir / "metrics.json").write_text(json.dumps({"run_id": f"SIM_{name}", "result": "SUCCESS",
                                                          "args": {"mode": "prepared"}, "prepared_sha256": sha}),
                                              encoding="utf-8")
    return run


def intrinsics_record(size=(HW[1] * ORIG_FACTOR, HW[0] * ORIG_FACTOR), f=None, k=(-0.05, 0.01), cams=tuple(CAMS),
                      source="SIM"):
    """bundle_init_from_record 가 받는 최소 내부 파라미터 기록(기본 SIM, source="LIVE" 면 실제 카메라처럼)."""
    w, h = size
    f = f or float(intrinsics()[0, 0] * ORIG_FACTOR)
    cam = {"camera_id": "shared-lens", "image_size": [w, h],
           "K": [[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]],
           "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [k[0], k[1], 0.0, 0.0, 0.0]}}
    return {
        "format": "simai.intrinsics_estimate", "format_version": "0.2.0", "created_at": 1792549780848,
        "input_provenance": {"frames": {"source": source, "dataset_id": "sim/world-frame"}, "is_upper_bound": source == "SIM"},
        "estimates": [{"camera_id": c} for c in cams],
        "shared_lens": {"consensus": True, "common": {
            "camera_id": "shared-lens", "status": "OK", "method": "SHARED_LENS", "method_version": "0.1.0",
            "image_size": [w, h], "principal_point_assumed": False, "uncertainty": {}, "camera": cam,
        }, "deviations": [], "outlier_camera_ids": []},
    }


@pytest.fixture
def sim_run(tmp_path):
    return make_run(tmp_path)
