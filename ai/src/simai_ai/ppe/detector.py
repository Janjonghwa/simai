"""PPE 전체 화면 검출기 (명세 5.5 입력: 원본 전체 화면 → 640, FP32, confidence 0.5, NMS 0.7).

가중치는 deploy/weights.json `ppe` 묶음으로 받은 파일만 쓴다. 불러오기 전에 크기·sha256 을 확인하고(자동 다운로드 없음),
불러온 뒤 클래스 이름·순서를 확인한다. 둘 중 하나라도 다르면 시작하지 않는다.
원본 프레임은 메모리에서만 쓰고 저장하지 않는다(5.5, 11.5). 사람 연결·상태 판정은 association·state 몫이다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .classes import ACCEPTED_NAMES, Candidate, normalize_candidates, validate_names

IMGSZ = 640
CONF = 0.5
IOU = 0.7
MAX_DET = 300


def file_sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def verify_checkpoint(path: Path, expected_bytes: int | None, expected_sha256: str) -> Path:
    """크기(알면)와 sha256(16진수 64자, 'sha256:' 접두 허용)이 맞을 때만 경로를 돌려준다."""
    want = expected_sha256.removeprefix("sha256:")
    if not path.is_file():
        raise ValueError(f"PPE 체크포인트가 없다: {path}")
    if expected_bytes is not None and path.stat().st_size != expected_bytes:
        raise ValueError(f"PPE 체크포인트 크기가 다르다: {path.stat().st_size} != {expected_bytes}")
    if file_sha256(path) != want:
        raise ValueError("PPE 체크포인트 sha256 이 다르다")
    return path


class PpeDetector:
    """한 프레임씩(batch 1, 동시 추론 1) 추론한다. 주기·대기열·Tier 0 지연 문은 PPE 서비스 몫이다."""

    def __init__(self, weights: Path, *, sha256: str, size_bytes: int | None = None, device: str | int = 0) -> None:
        path = verify_checkpoint(Path(weights), size_bytes, sha256)  # 역직렬화 전에 확인한다
        import torch
        from ultralytics import YOLO

        torch.backends.cuda.matmul.allow_tf32 = False  # FP32 그대로(RunPod 동일성 확인 조건과 같다)
        torch.backends.cudnn.allow_tf32 = False
        self._model = YOLO(str(path), task="detect")
        self.model_names = validate_names(self._model.names)
        self.name_policy = ACCEPTED_NAMES[self.model_names]
        self.checkpoint_sha256 = sha256.removeprefix("sha256:")
        self._device = device

    def infer(self, frame_bgr: Any) -> list[Candidate]:
        """원본 해상도 HWC uint8 BGR 프레임 하나 → 후보(원본 픽셀 xyxy). 검출이 없으면 빈 목록."""
        if getattr(frame_bgr, "ndim", None) != 3 or frame_bgr.shape[2] != 3 or str(frame_bgr.dtype) != "uint8":
            raise ValueError("원본 HWC uint8 BGR 프레임이어야 한다")
        height, width = frame_bgr.shape[:2]
        boxes = self._model.predict(
            source=frame_bgr, device=self._device, imgsz=IMGSZ, conf=CONF, iou=IOU, max_det=MAX_DET,
            rect=False, augment=False, quantize=32, save=False, save_txt=False, save_crop=False, show=False,
            verbose=False,
        )[0].boxes
        return normalize_candidates(boxes.xyxy.cpu().tolist(), boxes.conf.cpu().tolist(), boxes.cls.cpu().tolist(),
                                    width, height)
