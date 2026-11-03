"""선택적 얼굴 검출·임베딩 백엔드 인터페이스와 최소 픽셀 문턱 게이트 (S15P31A101-463).

규칙 (사용자 결정, 2026-11-04):
- 얼굴은 충분히 크게 보일 때(`min_face_size_px`, 기본 40px 이상)만 보조 점수로 쓴다.
- 생성형 얼굴 복원·표정 추정은 하지 않는다(지어낸 얼굴 금지).
- 가중치(`SIMAI_FACE_WEIGHTS_DIR`, InsightFace buffalo 계열 ONNX)가 없거나 선택 의존성이 없으면
  자동 다운로드하지 않고 안전하게 건너뛴다(`NoOpFaceEmbedder`).
"""

from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .embedder import l2_normalize_rows, verify_file_sha256

DEFAULT_MIN_FACE_SIZE_PX = 40.0
DEFAULT_FACE_EMBEDDING_DIM = 512


@dataclass(frozen=True)
class FaceConfig:
    """얼굴 보조 특징 추출 설정."""

    min_face_size_px: float = DEFAULT_MIN_FACE_SIZE_PX
    min_det_score: float = 0.60
    min_keypoint_conf: float = 0.35
    require_frontal_keypoints: bool = True
    min_frontal_landmarks: int = 2
    head_span_scale: float = 2.2
    weights_dir: Path | None = None
    recognition_onnx_name: str = "w600k_r50.onnx"
    detection_onnx_name: str = "det_10g.onnx"
    weights_sha256: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.min_face_size_px <= 0:
            raise ValueError(f"min_face_size_px 는 0보다 커야 합니다: {self.min_face_size_px}")

    @classmethod
    def from_env(cls, **overrides: Any) -> FaceConfig:
        raw_dir = os.environ.get("SIMAI_FACE_WEIGHTS_DIR") or os.environ.get("SIMAI_INSIGHTFACE_ROOT")
        weights_dir = Path(raw_dir) if raw_dir else None
        min_px = float(os.environ.get("SIMAI_FACE_MIN_PX", str(DEFAULT_MIN_FACE_SIZE_PX)))
        kwargs: dict[str, Any] = {
            "weights_dir": weights_dir,
            "min_face_size_px": min_px,
        }
        kwargs.update(overrides)
        return cls(**kwargs)


@dataclass(frozen=True)
class FaceRegionCandidate:
    """2D 포즈 관절 또는 얼굴 검출기로 추정한 얼굴 영역 후보."""

    bbox_xyxy: tuple[float, float, float, float]
    width_px: float
    height_px: float
    min_side_px: float
    frontal_landmarks_visible: int
    passes_size_gate: bool


@dataclass(frozen=True)
class FaceEmbeddingResult:
    """크기·정면성 문턱을 통과한 얼굴 임베딩 결과."""

    embedding: Any
    bbox_xyxy: tuple[float, float, float, float]
    det_score: float
    face_size_px: float


def estimate_face_region_from_pose(
    keypoints_coco17: Sequence[Any] | None,
    person_bbox_xyxy: Sequence[float] | None = None,
    config: FaceConfig | None = None,
) -> FaceRegionCandidate | None:
    """COCO 17관절의 머리 관절(0~4: 코·눈·귀)로 얼굴 크기와 정면 관측 여부를 판정한다.

    코(0)·왼눈(1)·오른눈(2) 중 보이는 관절 수가 `min_frontal_landmarks` 미만이면
    뒷모습이거나 얼굴이 가려진 것으로 보고 None 을 반환한다.
    """
    cfg = config or FaceConfig()
    if keypoints_coco17 is None or len(keypoints_coco17) < 5:
        return None

    head_pts: list[tuple[float, float, float]] = []
    frontal_count = 0
    for idx in range(5):
        kp = keypoints_coco17[idx]
        if kp is None or len(kp) < 2:
            continue
        x, y = float(kp[0]), float(kp[1])
        conf = float(kp[2]) if len(kp) >= 3 else 1.0
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(conf)):
            continue
        if x == 0.0 and y == 0.0 and conf == 0.0:
            continue
        if conf >= cfg.min_keypoint_conf:
            head_pts.append((x, y, conf))
            if idx in (0, 1, 2):
                frontal_count += 1

    if cfg.require_frontal_keypoints and frontal_count < cfg.min_frontal_landmarks:
        return None
    if len(head_pts) < 2:
        return None

    xs = [p[0] for p in head_pts]
    ys = [p[1] for p in head_pts]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    max_dist = max(math.hypot(p[0] - cx, p[1] - cy) for p in head_pts)
    base_span = max(span_x, span_y, max_dist * 2.0)
    side = base_span * cfg.head_span_scale

    x1 = cx - side * 0.5
    y1 = cy - side * 0.55
    x2 = cx + side * 0.5
    y2 = cy + side * 0.55

    if person_bbox_xyxy is not None and len(person_bbox_xyxy) >= 4:
        px1, py1, px2, py2 = (
            float(person_bbox_xyxy[0]),
            float(person_bbox_xyxy[1]),
            float(person_bbox_xyxy[2]),
            float(person_bbox_xyxy[3]),
        )
        x1 = max(px1, x1)
        y1 = max(py1, y1)
        x2 = min(px2, x2)
        y2 = min(py2, y2)

    width_px = max(0.0, x2 - x1)
    height_px = max(0.0, y2 - y1)
    min_side = min(width_px, height_px)
    return FaceRegionCandidate(
        bbox_xyxy=(round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)),
        width_px=round(width_px, 2),
        height_px=round(height_px, 2),
        min_side_px=round(min_side, 2),
        frontal_landmarks_visible=frontal_count,
        passes_size_gate=min_side >= cfg.min_face_size_px,
    )


class FaceEmbedder(ABC):
    """선택적 얼굴 검출+임베딩 인터페이스."""

    def __init__(self, config: FaceConfig | None = None) -> None:
        self._config = config or FaceConfig()

    @property
    def config(self) -> FaceConfig:
        return self._config

    @property
    @abstractmethod
    def available(self) -> bool:
        """얼굴 임베딩 모델이 준비되어 있는지 여부."""

    @property
    def embedding_dim(self) -> int:
        return DEFAULT_FACE_EMBEDDING_DIM

    def extract(
        self,
        crop_bgr: Any,
        *,
        keypoints_coco17: Sequence[Any] | None = None,
        person_bbox_xyxy: Sequence[float] | None = None,
    ) -> FaceEmbeddingResult | None:
        """사람 crop 에서 최소 크기(`min_face_size_px`)와 정면 기준을 통과할 때만 얼굴 임베딩을 추출한다."""
        if not self.available or crop_bgr is None:
            return None

        import numpy as np

        arr = np.asarray(crop_bgr)
        if arr.ndim != 3 or arr.shape[0] < self._config.min_face_size_px or arr.shape[1] < self._config.min_face_size_px:
            return None

        pose_region: FaceRegionCandidate | None = None
        if keypoints_coco17 is not None:
            pose_region = estimate_face_region_from_pose(
                keypoints_coco17,
                person_bbox_xyxy=person_bbox_xyxy,
                config=self._config,
            )
            if pose_region is None or not pose_region.passes_size_gate:
                return None

        raw = self._extract_impl(arr, pose_region=pose_region)
        if raw is None:
            return None
        if raw.face_size_px < self._config.min_face_size_px or raw.det_score < self._config.min_det_score:
            return None

        normed = l2_normalize_rows(raw.embedding).reshape(-1)
        if float(np.linalg.norm(normed)) <= 1e-12:
            return None
        return FaceEmbeddingResult(
            embedding=normed,
            bbox_xyxy=raw.bbox_xyxy,
            det_score=float(raw.det_score),
            face_size_px=float(raw.face_size_px),
        )

    @abstractmethod
    def _extract_impl(
        self,
        crop_bgr: Any,
        *,
        pose_region: FaceRegionCandidate | None = None,
    ) -> FaceEmbeddingResult | None:
        """하위 구현체가 실제 얼굴 검출·임베딩을 수행한다."""


class NoOpFaceEmbedder(FaceEmbedder):
    """얼굴 가중치가 없을 때 건너뛰는 기본 구현체."""

    @property
    def available(self) -> bool:
        return False

    def _extract_impl(
        self,
        crop_bgr: Any,
        *,
        pose_region: FaceRegionCandidate | None = None,
    ) -> FaceEmbeddingResult | None:
        del crop_bgr, pose_region
        return None


class InsightFaceOnnxEmbedder(FaceEmbedder):
    """InsightFace buffalo 계열 ONNX 또는 주입된 추론 함수 기반 얼굴 임베더."""

    def __init__(
        self,
        config: FaceConfig | None = None,
        *,
        predictor: Callable[[Any, FaceRegionCandidate | None], FaceEmbeddingResult | None] | None = None,
    ) -> None:
        super().__init__(config)
        self._predictor = predictor
        self._rec_session: Any | None = None

        if self._predictor is None:
            self._rec_session = self._try_load_onnx_session()

    @property
    def available(self) -> bool:
        return self._predictor is not None or self._rec_session is not None

    def _try_load_onnx_session(self) -> Any | None:
        weights_dir = self._config.weights_dir
        if weights_dir is None:
            return None
        rec_path = Path(weights_dir) / self._config.recognition_onnx_name
        if not rec_path.is_file():
            return None
        if self._config.weights_sha256 and self._config.recognition_onnx_name in self._config.weights_sha256:
            verify_file_sha256(rec_path, self._config.weights_sha256[self._config.recognition_onnx_name])
        try:
            import onnxruntime as ort
        except ImportError:
            return None
        return ort.InferenceSession(str(rec_path), providers=["CPUExecutionProvider"])

    def _extract_impl(
        self,
        crop_bgr: Any,
        *,
        pose_region: FaceRegionCandidate | None = None,
    ) -> FaceEmbeddingResult | None:
        if self._predictor is not None:
            return self._predictor(crop_bgr, pose_region)
        if self._rec_session is None:
            return None

        import cv2
        import numpy as np

        h, w = crop_bgr.shape[:2]
        if pose_region is not None:
            face_size = pose_region.min_side_px
            bbox = pose_region.bbox_xyxy
        else:
            face_size = float(min(w, h))
            bbox = (0.0, 0.0, float(w), float(h))

        if face_size < self._config.min_face_size_px:
            return None

        resized = cv2.resize(crop_bgr[:, :, :3], (112, 112), interpolation=cv2.INTER_LINEAR)
        rgb = resized[:, :, ::-1].astype(np.float32)
        blob = ((rgb - 127.5) / 127.5).transpose(2, 0, 1)[None, ...]
        input_name = self._rec_session.get_inputs()[0].name
        outputs = self._rec_session.run(None, {input_name: blob})
        vec = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        return FaceEmbeddingResult(
            embedding=vec,
            bbox_xyxy=bbox,
            det_score=1.0,
            face_size_px=face_size,
        )


def create_face_embedder(config: FaceConfig | None = None) -> FaceEmbedder:
    """설정에 얼굴 가중치가 있으면 ONNX 임베더를, 없으면 NoOpFaceEmbedder 를 반환한다."""
    cfg = config or FaceConfig.from_env()
    if cfg.weights_dir is not None:
        candidate = InsightFaceOnnxEmbedder(cfg)
        if candidate.available:
            return candidate
    return NoOpFaceEmbedder(cfg)
