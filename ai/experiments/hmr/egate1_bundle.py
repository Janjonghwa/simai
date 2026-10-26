"""E-GATE-1 카메라별 HMR 결과 묶음 형식 (S15P31A101-87, docs/design/hmr-multiview-aggregation.md '결과 묶음').

GPU Job(`run_multiview_hmr.py`)이 쓰고 노트북 평가(`egate1_eval.py`)가 읽는다. numpy 만 쓴다(torch·cv2 없음).

    <out>/metrics.json         Run 요약(exp_job.py 가 Run 기록 지표로 줄인다)
    <out>/hmr_frames.jsonl     (시각, 카메라) 그림 하나에 한 줄. 아래 IMAGE_FIELDS
    <out>/arrays/<key>.npz     그 그림의 사람별 배열 p{i}__<이름>(ARRAY_FIELDS)
    <out>/faces.npy            MHR 면 (F, 3) int32 — 실루엣 평가용. MHR 자산(SAM License)에서 나온 값이라 묶음을 팀 밖에 내지 않는다

좌표: bbox·keypoints_2d 는 원본(왜곡된) 그림 픽셀(Tier 0 per_camera 와 같은 공간), keypoints_cam·vertices_cam 은 그 카메라 좌표(m, OpenCV,
`pred_* + pred_cam_t`), 마스크·K_used 는 HMR 에 넣은 그림(왜곡을 편 그림) 좌표다. 사람이 찍힌 자료에서 나온 값이라 커밋하지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

BUNDLE_FORMAT = "simai.egate1_hmr_bundle"
BUNDLE_VERSION = "0.1.0"
FRAMES_FILE = "hmr_frames.jsonl"
ARRAYS_DIR = "arrays"
FACES_FILE = "faces.npy"
# 사람별 배열: keypoints_cam (70, 3) f32 필수, camera_from_rig (3, 3) f64, joint_rotations (127, 3) f32(모르는 관절 NaN),
# vertices_cam (V, 3) f16, mask_box (4,) int32 [x0, y0, x1, y1) HMR 그림, mask_bits packbits(마스크 crop), mask_shape (2,) int32
ARRAY_FIELDS = ("keypoints_cam", "camera_from_rig", "joint_rotations", "vertices_cam", "mask_box", "mask_bits", "mask_shape")


def pack_mask(mask: np.ndarray) -> dict[str, np.ndarray] | None:
    """(H, W) bool → 사람 영역 crop 만 비트로 묶은 배열. 빈 마스크는 None."""
    m = np.asarray(mask, dtype=bool)
    ys, xs = np.nonzero(m)
    if not len(xs):
        return None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    crop = m[y0:y1, x0:x1]
    return {"mask_box": np.array([x0, y0, x1, y1], np.int32), "mask_bits": np.packbits(crop.ravel()),
            "mask_shape": np.array(crop.shape, np.int32)}


def unpack_mask(arrays: dict[str, np.ndarray], image_size: tuple[int, int]) -> np.ndarray | None:
    if "mask_bits" not in arrays:
        return None
    w, h = image_size
    x0, y0, x1, y1 = (int(v) for v in arrays["mask_box"])
    shape = tuple(int(v) for v in arrays["mask_shape"])
    crop = np.unpackbits(arrays["mask_bits"], count=shape[0] * shape[1]).astype(bool).reshape(shape)
    out = np.zeros((h, w), dtype=bool)
    out[y0:y1, x0:x1] = crop
    return out


def joint_rotations_array(rotations) -> np.ndarray:
    """계약 joint_rotations(목록, 모르는 관절 None) → (J, 3) f32, None 은 NaN."""
    return np.array([[np.nan] * 3 if r is None else list(r) for r in rotations], dtype=np.float32)


def joint_rotations_list(arr: np.ndarray) -> tuple:
    return tuple(None if not np.isfinite(r).all() else [float(x) for x in r] for r in np.asarray(arr, dtype=float))


def image_key(instant: int, camera_id: str) -> str:
    return f"i{int(instant):05d}_{camera_id}"


@dataclass
class BundlePerson:
    candidate_id: str
    bbox: tuple[float, float, float, float]
    score: float
    keypoints_2d: list | None
    prompt: dict
    arrays: dict[str, np.ndarray]


@dataclass
class BundleImage:
    instant: int
    camera_id: str
    capture_time_ms: int
    frame_ref: dict | None
    image_size: tuple[int, int]
    K_used: np.ndarray | None
    undistorted: bool
    result: str
    hmr_ms: float | None
    people: list[BundlePerson] = field(default_factory=list)


def write_image(out_dir: Path, row: dict[str, Any], people_arrays: list[dict[str, np.ndarray]]) -> None:
    """그림 한 장의 줄과 배열을 더한다(이미 있는 줄·파일을 덮어쓰지 않는다)."""
    out_dir = Path(out_dir)
    (out_dir / ARRAYS_DIR).mkdir(parents=True, exist_ok=True)
    key = image_key(row["instant"], row["camera_id"])
    if people_arrays:
        path = out_dir / ARRAYS_DIR / f"{key}.npz"
        if path.exists():
            raise FileExistsError(f"{path} 가 이미 있다(덮어쓰지 않는다)")
        flat = {f"p{i}__{k}": v for i, arrs in enumerate(people_arrays) for k, v in arrs.items() if k in ARRAY_FIELDS}
        np.savez_compressed(path, **flat)
    with open(out_dir / FRAMES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_bundle(path: Path) -> tuple[list[BundleImage], np.ndarray | None]:
    path = Path(path)
    faces = np.load(path / FACES_FILE) if (path / FACES_FILE).is_file() else None
    images = []
    for line_no, line in enumerate((path / FRAMES_FILE).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        key = image_key(row["instant"], row["camera_id"])
        arrays: dict[str, np.ndarray] = {}
        npz = path / ARRAYS_DIR / f"{key}.npz"
        if row.get("people"):
            if not npz.is_file():
                raise ValueError(f"{FRAMES_FILE}:{line_no}: 사람이 있는데 {npz.name} 가 없다")
            with np.load(npz) as z:
                arrays = {k: z[k] for k in z.files}
        people = []
        for i, p in enumerate(row.get("people") or ()):
            own = {k.split("__", 1)[1]: v for k, v in arrays.items() if k.startswith(f"p{i}__")}
            if "keypoints_cam" not in own:
                raise ValueError(f"{FRAMES_FILE}:{line_no}: 사람 {i} 의 keypoints_cam 이 없다")
            people.append(BundlePerson(p["candidate_id"], tuple(float(v) for v in p["bbox"]), float(p.get("score", 1.0)),
                                       p.get("keypoints_2d"), p.get("prompt") or {}, own))
        K = row.get("K_used")
        images.append(BundleImage(
            instant=int(row["instant"]), camera_id=row["camera_id"], capture_time_ms=int(row["capture_time_ms"]),
            frame_ref=row.get("frame_ref"), image_size=(int(row["image_size"][0]), int(row["image_size"][1])),
            K_used=None if K is None else np.asarray(K, dtype=float), undistorted=bool(row.get("undistorted")),
            result=row.get("result", "SUCCESS"), hmr_ms=row.get("hmr_ms"), people=people,
        ))
    return images, faces
