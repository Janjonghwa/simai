"""MapAnything known-K 입력 준비: 왜곡 펴기 + 펴진 K + 사람 영역 마스크 (명세 4.3 1단계, S15P31A101-102).

- 왜곡: 내부 파라미터 기록(`simai.intrinsics_estimate`, AnyCalib `radial:2`)의 같은 렌즈 공통값을 -214 와 같이
  `bundle_init_from_record` 로 읽어 카메라별 K·[k1, k2] 로 편다. 펴진 K 는 `cv2.getOptimalNewCameraMatrix`(alpha 0, 주점 가운데,
  같은 크기)이고, 펴기 표가 원본 영상 밖을 읽으면(강한 왜곡에서 새 K 가 잘못 잡힘) 오류다(검은 테두리를 장면으로 넣지 않는다).
  회전은 바꾸지 않으므로 MapAnything 이 낸 자세는 원래(왜곡 있는) 카메라의 자세와 같다.
- 사람 마스크: 펴진 영상에서 분할 모델(기본 ultralytics YOLO seg, person 클래스)로 찾은 사람 영역의 합을 팽창해 PNG(0/255)로
  둔다. MapAnything 입력 영상은 지우지 않고(모델이 보는 장면을 바꾸지 않음) 결과 점에서 뺀다(`inputs`).
- 출력 폴더는 새 폴더만 받는다(덮어쓰지 않음). 사람이 찍힌 영상이므로 커밋하지 않는 위치(`ai/experiments/recordings/`)에 두고
  GPU 세션에만 올린다(명세 11.5, 내부 테스트 용도). 얼굴 관련 처리는 없다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from simai_ai.calibration.intrinsics.estimate import WeightsRef
from simai_ai.calibration.intrinsics.record import bundle_init_from_record
from simai_ai.calibration.world_frame.inputs import PREPARED_FORMAT

PREPARED_VERSION = "0.1.0"
PERSON_CLASS = 0  # COCO person


class PersonSegmenter(Protocol):
    def describe(self) -> dict[str, Any]: ...

    def person_mask(self, image_bgr: np.ndarray) -> tuple[np.ndarray, int]:
        """(사람 영역 bool 마스크, 사람 수)."""
        ...


class UltralyticsPersonSegmenter:
    """ultralytics YOLO seg(extras detection). 가중치는 경로·sha256 을 확인하고 쓴다(내려받지 않는다)."""

    def __init__(self, weights: WeightsRef, *, conf: float = 0.25, imgsz: int = 1280, device: str | None = None) -> None:
        from ultralytics import YOLO

        self._weights = weights
        self._model = YOLO(str(weights.verify()), task="segment")
        self._conf, self._imgsz, self._device = conf, imgsz, device

    def describe(self) -> dict[str, Any]:
        import ultralytics

        return {"model": Path(self._weights.path).name, "sha256": self._weights.sha256, "conf": self._conf,
                "imgsz": self._imgsz, "ultralytics": ultralytics.__version__}

    def person_mask(self, image_bgr: np.ndarray) -> tuple[np.ndarray, int]:
        h, w = image_bgr.shape[:2]
        r = self._model.predict(image_bgr, classes=[PERSON_CLASS], conf=self._conf, imgsz=self._imgsz,
                                retina_masks=True, verbose=False, device=self._device)[0]
        mask = np.zeros((h, w), dtype=bool)
        if r.masks is None:
            return mask, 0
        data = r.masks.data.cpu().numpy() > 0.5
        for m in data:
            if m.shape != (h, w):
                import cv2

                m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
            mask |= m
        return mask, int(len(data))


def undistort(image: np.ndarray, K: np.ndarray, dist: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(펴진 영상, 펴진 K). 같은 크기, 왜곡 0."""
    import cv2

    h, w = image.shape[:2]
    newK, _ = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), 0, (w, h), centerPrincipalPoint=True)
    mx, my = cv2.initUndistortRectifyMap(K, dist, None, newK, (w, h), cv2.CV_32FC1)
    if not (np.isfinite(mx).all() and mx.min() >= -0.5 and mx.max() <= w - 0.5 and my.min() >= -0.5 and my.max() <= h - 0.5):
        raise ValueError("펴진 영상이 원본 밖을 읽는다(새 K 가 왜곡 모델의 유효 범위를 넘음)")
    return cv2.remap(image, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE), newK


def prepare_inputs(
    images: Mapping[str, Path],
    intrinsics_record: Mapping[str, Any],
    out_dir: Path,
    *,
    segmenter: PersonSegmenter | None,
    input_provenance: Mapping[str, Any],
    dilate_px: int = 15,
    intrinsics_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """카메라별 영상 한 장 → 펴진 영상·마스크·prepared.json. segmenter None 이면 마스크를 만들지 않는다(기록에 남김)."""
    import cv2

    out_dir = Path(out_dir)
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} 가 이미 있다(덮어쓰지 않는다)")
    init = bundle_init_from_record(intrinsics_record)
    known = {c.camera_id for c in init.cameras}
    missing = sorted(set(images) - known)
    if missing:
        raise ValueError(f"내부 파라미터 기록에 없는 카메라: {missing}")
    loaded = {}
    for cid, path in sorted(images.items()):
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"{cid}: 영상을 읽지 못함 {Path(path).name}")
        cam = init.camera(cid).camera
        if [img.shape[1], img.shape[0]] != list(cam["image_size"]):
            raise ValueError(f"{cid}: 영상 크기 {[img.shape[1], img.shape[0]]} 가 내부 파라미터 {cam['image_size']} 와 다르다")
        K = np.asarray(cam["K"], dtype=float)
        und, newK = undistort(img, K, np.asarray(cam["distortion"]["coeffs"], dtype=float))
        loaded[cid] = (Path(path), img, cam, K, und, newK)
    out_dir.mkdir(parents=True)  # 모든 카메라를 편 뒤에 만든다(실패하면 폴더를 남기지 않는다)
    views = []
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * dilate_px + 1,) * 2) if dilate_px > 0 else None
    for cid, (path, img, cam, K, und, newK) in loaded.items():
        name = cid
        cv2.imwrite(str(out_dir / f"{name}.jpg"), und, [cv2.IMWRITE_JPEG_QUALITY, 95])
        view: dict[str, Any] = {
            "name": name, "camera_id": cid, "image": f"{name}.jpg", "image_size": [img.shape[1], img.shape[0]],
            "K_undistorted": newK.tolist(), "source_image": path.name,
            "source_camera": {"K": K.tolist(), "distortion": dict(cam["distortion"])}, "mask": None,
        }
        if segmenter is not None:
            mask, count = segmenter.person_mask(und)
            if kernel is not None and mask.any():
                mask = cv2.dilate(mask.astype(np.uint8), kernel) > 0
            cv2.imwrite(str(out_dir / f"{name}.mask.png"), mask.astype(np.uint8) * 255)
            view |= {"mask": f"{name}.mask.png", "person_count": count, "person_mask_ratio": round(float(mask.mean()), 5)}
        views.append(view)
    doc = {
        "format": PREPARED_FORMAT,
        "format_version": PREPARED_VERSION,
        "input_provenance": dict(input_provenance),
        "intrinsics": dict(intrinsics_ref or {}),
        "undistortion": "cv2.getOptimalNewCameraMatrix(alpha=0, centerPrincipalPoint) + initUndistortRectifyMap",
        "segmenter": None if segmenter is None else {**segmenter.describe(), "dilate_px": dilate_px},
        "person_masks": segmenter is not None,
        "views": views,
        "note": "사람이 찍힌 영상: 커밋·외부 공유 금지(내부 테스트 용도, 명세 11.5)",
    }
    (out_dir / "prepared.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
