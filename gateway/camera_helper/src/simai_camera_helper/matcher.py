"""Four CPU ALIKED extractions, six LightGlue pairs, and V0-style Essential geometry.

Weights are supplied by the caller. Neither a cache lookup nor a download is allowed.
Initialize before starting other torch model-loading threads: upstream ALIKED has no
local-weight constructor, so its hub loader is replaced temporarily under a lock.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from .judge import CAMERAS, DEFAULT_PARAMS, PAIRS, PairMetrics, Params, occupied_cells

LIGHTGLUE_COMMIT = "5a650c444718caffc2669dc0094cde908f42dd73"
_LOAD_LOCK = threading.Lock()


@dataclass(frozen=True)
class Intrinsics:
    K: object
    distortion: object
    image_size: tuple[int, int]


def load_intrinsics(path: Path) -> dict[str, Intrinsics]:
    """Read lens values only. Extrinsic matrices and camera centers are never accessed."""
    import numpy as np

    doc = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for camera in doc["cameras"]:
        cid = camera["camera_id"]
        if cid not in CAMERAS:
            continue
        K = np.asarray(camera["K"], dtype=np.float64)
        dist = np.asarray(camera["distortion"]["coeffs"], dtype=np.float64)
        size = tuple(camera["image_size"])
        if camera["distortion"]["model"] != "OPENCV_PINHOLE":
            raise ValueError(f"{cid}: only OPENCV_PINHOLE distortion is supported")
        if (K.shape != (3, 3) or not np.isfinite(K).all() or K[0, 0] <= 0 or K[1, 1] <= 0
                or not np.allclose(K[2], [0, 0, 1]) or dist.ndim != 1
                or len(dist) not in (4, 5, 8, 12, 14) or not np.isfinite(dist).all()
                or len(size) != 2 or min(size) <= 0):
            raise ValueError(f"{cid}: invalid lens intrinsics")
        if cid in result:
            raise ValueError(f"duplicate camera: {cid}")
        result[cid] = Intrinsics(K, dist, size)
    if set(result) != set(CAMERAS):
        raise ValueError("stored lens intrinsics must include all four cameras")
    return result


def prepare_image(image, lens: Intrinsics | None, width: int):
    """Resize then undistort using the correspondingly scaled K, preserving its field of view."""
    import cv2
    import numpy as np

    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise ValueError("expected a uint8 BGR image")
    h, w = image.shape[:2]
    if min(h, w, width) <= 0:
        raise ValueError("image dimensions and requested width must be positive")
    height = max(1, round(h * width / w))
    small = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    if lens is None:
        # A 90-degree horizontal field of view is an explicit approximation, not calibration.
        K = np.array([[width / 2, 0, (width - 1) / 2],
                      [0, width / 2, (height - 1) / 2], [0, 0, 1]], dtype=np.float64)
        return small, K
    K = np.diag([width / lens.image_size[0], height / lens.image_size[1], 1.0]) @ lens.K
    return cv2.undistort(small, K, lens.distortion), K


def geometric_metrics(a: str, b: str, pa, pb, Ka, Kb, size_a: tuple[int, int],
                      size_b: tuple[int, int], threshold_px: float) -> PairMetrics:
    """Counts are Essential RANSAC AND recoverPose cheirality, as in V0 (not raw RANSAC)."""
    import cv2
    import numpy as np

    raw = len(pa)
    if raw != len(pb):
        raise ValueError("correspondence lengths differ")
    if raw < 8:
        return PairMetrics(a, b, raw, 0)
    na = cv2.undistortPoints(np.asarray(pa, np.float64)[:, None], Ka, None)[:, 0]
    nb = cv2.undistortPoints(np.asarray(pb, np.float64)[:, None], Kb, None)[:, 0]
    # Points are already undistorted. Pixel threshold is in the resized analysis image.
    threshold = threshold_px / float(np.mean([Ka[0, 0], Kb[0, 0]]))
    E, mask = cv2.findEssentialMat(na, nb, np.eye(3), cv2.RANSAC, 0.999, threshold)
    if E is None or E.shape != (3, 3) or mask is None:
        return PairMetrics(a, b, raw, 0)
    _, _, _, positive = cv2.recoverPose(E, na, nb, np.eye(3), mask=mask)
    keep = positive.ravel() > 0
    count = int(keep.sum())
    ca = cb = None
    na_img = np.clip(np.asarray(pa, np.float64)[keep] / size_a, 0, 1)
    nb_img = np.clip(np.asarray(pb, np.float64)[keep] / size_b, 0, 1)
    if count >= 5:
        ca = tuple(float(v) for v in np.mean(na_img, axis=0))
        cb = tuple(float(v) for v in np.mean(nb_img, axis=0))
    # 한 구석 몰림(S15P31A101-761): 인라이어가 있는 4x4 칸 수(Stage A 공간 홀드아웃 격자와 같다)
    return PairMetrics(a, b, raw, count, ca, cb, occupied_cells(na_img.tolist()), occupied_cells(nb_img.tolist()))


class CameraMatcher:
    """GPU when available (device="auto"), else CPU. Reuse one instance between rounds; inference stores only four feature sets."""

    def __init__(self, aliked_weights: Path, lightglue_weights: Path, params: Params = DEFAULT_PARAMS,
                 intrinsics: dict[str, Intrinsics] | None = None, *, cpu_threads: int = 4,
                 device: str = "auto"):
        paths = {"aliked": Path(aliked_weights), "lightglue": Path(lightglue_weights)}
        for name, path in paths.items():
            if not path.is_file():
                raise FileNotFoundError(f"{name} weights missing: {path}; automatic download is disabled")
            # Check readability before importing torch or constructing any model.
            with path.open("rb") as stream:
                if not stream.read(1):
                    raise ValueError(f"{name} weights are empty: {path}")
        if cpu_threads < 1:
            raise ValueError("cpu_threads must be positive")
        if device not in ("auto", "cuda", "cpu"):
            raise ValueError("device must be auto, cuda or cpu")
        if params.intrinsics_source == "STORED" and (intrinsics is None or set(intrinsics) != set(CAMERAS)):
            raise ValueError("STORED requires all four lens intrinsics")
        if params.intrinsics_source == "DEFAULT" and intrinsics is not None:
            raise ValueError("DEFAULT must not silently use stored intrinsics")
        import cv2
        import torch
        from lightglue import ALIKED, LightGlue

        self.params, self.intrinsics = params, intrinsics
        torch.set_num_threads(cpu_threads)
        cv2.setNumThreads(cpu_threads)
        self.cpu_threads = cpu_threads
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("device=cuda requested but CUDA is not available")
        # 노트북 RTX 4050 실측: 1024 px·2048 점 한 번 약 1.8 s(CPU 16~24 s). auto 는 CUDA 가 있으면 GPU, 없으면 CPU.
        self.device = "cuda" if device != "cpu" and torch.cuda.is_available() else "cpu"
        loaded = {}
        expected = {
            ALIKED.checkpoint_url.format("aliked-n16"): paths["aliked"],
            LightGlue.url.format(LightGlue.version, "aliked_lightglue"): paths["lightglue"],
        }

        def local_only(url, *args, **kwargs):
            if url not in expected:
                raise RuntimeError(f"unexpected weight request; download prohibited: {url}")
            state = torch.load(expected[url], map_location="cpu", weights_only=True)
            loaded[url] = state
            return state

        with _LOAD_LOCK, patch.object(torch.hub, "load_state_dict_from_url", side_effect=local_only):
            self.extractor = ALIKED(model_name="aliked-n16", max_num_keypoints=params.max_keypoints)
            self.matcher = LightGlue(features="aliked", flash=False)
        # The official checkpoint predates the deterministic confidence_thresholds buffer.
        # Allow only that buffer to be absent; never silently keep random learned parameters.
        state = loaded[LightGlue.url.format(LightGlue.version, "aliked_lightglue")]
        for i in range(self.matcher.conf.n_layers):
            state = {k.replace(f"self_attn.{i}", f"transformers.{i}.self_attn")
                     .replace(f"cross_attn.{i}", f"transformers.{i}.cross_attn"): v for k, v in state.items()}
        incompatible = self.matcher.load_state_dict(state, strict=False)
        missing = set(incompatible.missing_keys) - {"confidence_thresholds"}
        if missing or incompatible.unexpected_keys:
            raise ValueError(f"incompatible LightGlue weights: missing={sorted(missing)}, "
                             f"unexpected={incompatible.unexpected_keys}")
        self.extractor = self.extractor.eval().to(self.device)
        self.matcher = self.matcher.eval().to(self.device)

    def match(self, images: dict) -> list[PairMetrics]:
        import cv2
        import numpy as np
        import torch
        from lightglue.utils import rbd

        if set(images) != set(CAMERAS):
            raise ValueError("provide all four camera keys (None for unavailable video)")
        features, keypoints, matrices, sizes = {}, {}, {}, {}
        with torch.inference_mode():
            for cid in CAMERAS:
                if images[cid] is None:
                    continue
                lens = self.intrinsics[cid] if self.intrinsics is not None else None
                image, K = prepare_image(images[cid], lens, self.params.image_width_px)
                rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                tensor = torch.from_numpy(rgb).permute(2, 0, 1).float().div_(255).to(self.device)
                f = self.extractor.extract(tensor, resize=None)
                features[cid], keypoints[cid] = f, rbd(f)["keypoints"].cpu().numpy()
                matrices[cid], sizes[cid] = K, (image.shape[1], image.shape[0])
            results = []
            for a, b in PAIRS:
                if a not in features or b not in features:
                    results.append(PairMetrics(a, b))
                    continue
                if not len(keypoints[a]) or not len(keypoints[b]):
                    results.append(PairMetrics(a, b, 0, 0))
                    continue
                matches = rbd(self.matcher({"image0": features[a], "image1": features[b]}))
                m = np.asarray(matches["matches"].cpu().numpy(), dtype=np.int64)
                pa, pb = keypoints[a][m[:, 0]], keypoints[b][m[:, 1]]
                results.append(geometric_metrics(a, b, pa, pb, matrices[a], matrices[b],
                                                sizes[a], sizes[b], self.params.ransac_threshold_px))
        return results


def warp_yaw(image, K, angle_rad: float):
    """Synthetic +yaw image homography K R K^-1; image x moves right for positive yaw.

    Apply to an undistorted image only. This warps existing content and cannot
    simulate newly revealed scene pixels or prove real camera-turn performance.
    """
    import cv2
    import numpy as np

    c, s = np.cos(angle_rad), np.sin(angle_rad)
    R = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)
    H = K @ R @ np.linalg.inv(K)
    return cv2.warpPerspective(image, H, (image.shape[1], image.shape[0]))
