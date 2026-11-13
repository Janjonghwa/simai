"""YOLO26x-pose 검출기 어댑터 (명세 5.1 검출, 3.12 TensorRT 엔진, deploy/tier0 SIMAI_TIER0_ENGINE).

Tier 0 이미지의 진입점(deploy/tier0/entrypoint.sh)이 엔진을 준비하고 경로를 SIMAI_TIER0_ENGINE 으로 넘긴다. 여기서는 그
엔진(또는 시험용 .pt 가중치)을 ultralytics 로 열어 디코딩된 프레임 묶음을 넣고, 결과를 Local Tracker 입력
(tracking.local_tracker.CameraDetections)으로 옮긴다. 검출 값은 그대로 옮기고 고치지 않는다(박스·신뢰도·keypoint).

ultralytics 는 extras(detection·tier0)에만 있어 이 모듈은 만들 때 처음 import 한다. 없으면 DetectorUnavailable.

고우선순위 CUDA 스트림(S15P31A101-662 자원 격리, 선택 SIMAI_TIER0_CUDA_STREAM_PRIORITY=high, 기본 normal): 검출(전처리·엔진·
후처리)을 우선순위가 높은 torch CUDA 스트림에서 돌린다. ultralytics 8.4 TensorRT 백엔드는 정적 엔진이면 처음에 잡은 CUDA 그래프를
`graph.replay()` 로 다시 돌리는데, replay 는 지금 스트림에 올라가므로 엔진 커널도 이 스트림을 탄다. 동적 엔진·NMS 내장 엔진은
`execute_v2`(TensorRT 내부 동기 실행)라 엔진 부분은 우선순위를 타지 않고 전·후처리만 탄다. 같은 GPU 의 아바타·PPE·VLM 커널보다
Tier 0 커널을 먼저 내보내게 하려는 것이고, 메모리·결과 값은 바꾸지 않는다. CUDA 가 없으면 아무것도 하지 않는다.
"""

from __future__ import annotations

import contextlib
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

    def __init__(self, model_path: str, *, imgsz: int = DEFAULT_IMGSZ, half: bool = True, device: Any = None,
                 high_priority_stream: bool = False) -> None:
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
        self._high_priority = high_priority_stream
        self._stream: Any = None  # 검출 스레드에서 처음 detect 할 때 만든다
        self.stream_priority: int | None = None

    def _stream_context(self) -> Any:
        if not self._high_priority:
            return contextlib.nullcontext()
        if self._stream is None:
            try:
                import torch

                if not torch.cuda.is_available():
                    self._high_priority = False
                    return contextlib.nullcontext()
                # 음수가 높은 우선순위다. 장치가 주는 가장 높은 값을 쓴다(priority_range = (가장 낮음, 가장 높음))
                highest = torch.cuda.Stream.priority_range()[1]
                self._stream = torch.cuda.Stream(priority=highest)
                self.stream_priority = highest
            except Exception:  # noqa: BLE001 - 우선순위 스트림을 못 만들면 기본 스트림으로 검출한다
                logging.getLogger(__name__).warning("고우선순위 CUDA 스트림을 만들지 못했다: 기본 스트림으로 검출한다",
                                                    exc_info=True)
                self._high_priority = False
                return contextlib.nullcontext()
        import torch

        return torch.cuda.stream(self._stream)

    def detect(self, frames: Sequence[Frame]) -> list[CameraDetections]:
        frames = list(frames)
        if not frames:
            return []
        if any(f.image is None for f in frames):
            raise ValueError("검출기 입력은 디코딩된 영상 프레임이다(image 가 없다)")
        kwargs: dict[str, Any] = {"imgsz": self._imgsz, "half": self._half, "verbose": False}
        if self._device is not None:
            kwargs["device"] = self._device
        with self._stream_context():  # 결과를 CPU 값으로 옮기는 것까지 같은 스트림에서 끝낸다
            results = self._model.predict([f.image for f in frames], **kwargs)
            return [detections_from_result(f, r) for f, r in zip(frames, results, strict=True)]
