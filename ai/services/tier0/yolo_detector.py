"""YOLO26x-pose 검출기 어댑터 (명세 5.1 검출, 3.12 TensorRT 엔진, deploy/tier0 SIMAI_TIER0_ENGINE).

Tier 0 이미지의 진입점(deploy/tier0/entrypoint.sh)이 엔진을 준비하고 경로를 SIMAI_TIER0_ENGINE 으로 넘긴다. 여기서는 그
엔진(또는 시험용 .pt 가중치)을 ultralytics 로 열어 디코딩된 프레임 묶음을 넣고, 결과를 Local Tracker 입력
(tracking.local_tracker.CameraDetections)으로 옮긴다. 검출 값은 그대로 옮기고 고치지 않는다(박스·신뢰도·keypoint).

ultralytics 는 extras(detection·tier0)에만 있어 이 모듈은 만들 때 처음 import 한다. 없으면 DetectorUnavailable.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import Any

from simai_ai.providers.base import Frame
from simai_ai.tracking.local_tracker import CameraDetections, PoseDetection


class _HalfDeprecationOnce(logging.Filter):
    """ultralytics 의 해당 경고만 프로세스에서 한 번 보인다."""

    def __init__(self) -> None:
        super().__init__()
        self._seen = False
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "'half' is deprecated" not in message or "Use 'quantize' instead" not in message:
            return True
        with self._lock:
            if self._seen:
                return False
            self._seen = True
            return True


_HALF_WARNING_FILTER = _HalfDeprecationOnce()

DEFAULT_IMGSZ = 1280


class DetectorUnavailable(RuntimeError):
    """검출기를 열 수 없다(extras 없음, 엔진 경로 없음)."""


def _frame_ids(frame: Frame) -> tuple[int, int]:
    ref = frame.frame_ref
    return int(ref.get("stream_epoch", 0)), int(ref.get("frame_sequence", ref.get("frame_index", 0)))


def detections_from_result(frame: Frame, result: Any) -> CameraDetections:
    """ultralytics Results 하나 → CameraDetections. keypoint 가 없는 행은 None 관절로 둔다(지어내지 않음)."""
    boxes = getattr(result, "boxes", None)
    keypoints = getattr(result, "keypoints", None)
    dets: list[PoseDetection] = []
    if boxes is not None and len(boxes):
        xyxy = boxes.xyxy.tolist()
        conf = boxes.conf.tolist()
        kp_data = keypoints.data.tolist() if keypoints is not None else [None] * len(xyxy)
        for box, score, kps in zip(xyxy, conf, kp_data, strict=True):
            joints = tuple((float(k[0]), float(k[1]), float(k[2])) if len(k) >= 3 else None for k in (kps or ()))
            dets.append(PoseDetection(tuple(float(v) for v in box), min(1.0, max(0.0, float(score))), joints))
    epoch, sequence = _frame_ids(frame)
    return CameraDetections(
        camera_id=frame.camera_id,
        stream_epoch=epoch,
        frame_sequence=sequence,
        estimated_capture_time_ms=frame.estimated_capture_time,
        timestamp_source=frame.timestamp_source,
        timestamp_uncertainty_ms=frame.timestamp_uncertainty_ms,
        detections=tuple(dets),
    )


class UltralyticsPoseDetector:
    """엔진(.engine)·가중치(.pt)를 ultralytics YOLO 로 연다. detect 는 묶음 추론(카메라마다 한 장)."""

    def __init__(self, model_path: str, *, imgsz: int = DEFAULT_IMGSZ, half: bool = True, device: Any = None) -> None:
        if not model_path:
            raise DetectorUnavailable("검출기 엔진 경로가 없다(SIMAI_TIER0_ENGINE)")
        try:
            from ultralytics import YOLO
            from ultralytics.utils import LOGGER
        except ImportError as e:
            raise DetectorUnavailable("ultralytics 가 없다(extras detection·tier0)") from e
        if _HALF_WARNING_FILTER not in LOGGER.filters:
            LOGGER.addFilter(_HALF_WARNING_FILTER)
        self.model_path = model_path
        self._model = YOLO(model_path, task="pose")
        self._imgsz = imgsz
        self._half = half
        self._device = device

    def detect(self, frames: Sequence[Frame]) -> list[CameraDetections]:
        frames = list(frames)
        if not frames:
            return []
        if any(f.image is None for f in frames):
            raise ValueError("검출기 입력은 디코딩된 영상 프레임이다(image 가 없다)")
        kwargs: dict[str, Any] = {"imgsz": self._imgsz, "half": self._half, "verbose": False}
        if self._device is not None:
            kwargs["device"] = self._device
        results = self._model.predict([f.image for f in frames], **kwargs)
        return [detections_from_result(f, r) for f, r in zip(frames, results, strict=True)]
