"""선택적 얼굴 백엔드 인터페이스와 최소 픽셀 크기·정면성 문턱 시험 (S15P31A101-463)."""

from __future__ import annotations

import numpy as np

from simai_ai.reid import (
    FaceConfig,
    FaceEmbeddingResult,
    InsightFaceOnnxEmbedder,
    NoOpFaceEmbedder,
    create_face_embedder,
    estimate_face_region_from_pose,
)


def _pose_with_head_span(span_px: float, *, frontal: bool = True) -> list[list[float]]:
    kps = [[0.0, 0.0, 0.0] for _ in range(17)]
    cx, cy = 100.0, 80.0
    half = span_px * 0.5
    if frontal:
        kps[0] = [cx, cy, 0.92]            # 코
        kps[1] = [cx - half, cy - 5, 0.90] # 왼눈
        kps[2] = [cx + half, cy - 5, 0.90] # 오른눈
    kps[3] = [cx - half, cy, 0.85]         # 왼귀
    kps[4] = [cx + half, cy, 0.85]         # 오른귀
    return kps


def test_estimate_face_region_from_pose_checks_size_and_frontal() -> None:
    cfg = FaceConfig(min_face_size_px=40.0, head_span_scale=2.0)

    # 충분히 큰 정면 얼굴 (span 25px * scale 2.0 = 50px >= 40px)
    large_kps = _pose_with_head_span(25.0, frontal=True)
    region_large = estimate_face_region_from_pose(large_kps, config=cfg)
    assert region_large is not None
    assert region_large.passes_size_gate is True
    assert region_large.min_side_px >= 40.0
    assert region_large.frontal_landmarks_visible == 3

    # 멀리 있어 작은 얼굴 (span 12px * scale 2.0 = 24px < 40px)
    small_kps = _pose_with_head_span(12.0, frontal=True)
    region_small = estimate_face_region_from_pose(small_kps, config=cfg)
    assert region_small is not None
    assert region_small.passes_size_gate is False
    assert region_small.min_side_px < 40.0

    # 뒷모습 (귀만 보이고 코·눈이 안 보임) -> None
    back_kps = _pose_with_head_span(30.0, frontal=False)
    assert estimate_face_region_from_pose(back_kps, config=cfg) is None


def test_create_face_embedder_returns_noop_when_weights_absent() -> None:
    embedder = create_face_embedder(FaceConfig(weights_dir=None))
    assert isinstance(embedder, NoOpFaceEmbedder)
    assert embedder.available is False

    crop = np.zeros((120, 80, 3), dtype=np.uint8)
    assert embedder.extract(crop, keypoints_coco17=_pose_with_head_span(30.0)) is None


def test_insightface_embedder_with_injected_predictor_enforces_gates() -> None:
    calls = 0

    def _fake_predictor(crop: np.ndarray, region) -> FaceEmbeddingResult | None:
        nonlocal calls
        calls += 1
        del crop
        size = region.min_side_px if region is not None else 50.0
        return FaceEmbeddingResult(
            embedding=np.array([3.0, 4.0, 0.0, 0.0], dtype=np.float32),
            bbox_xyxy=(10.0, 10.0, 60.0, 60.0),
            det_score=0.9,
            face_size_px=size,
        )

    cfg = FaceConfig(min_face_size_px=40.0, head_span_scale=2.0)
    embedder = InsightFaceOnnxEmbedder(cfg, predictor=_fake_predictor)
    assert embedder.available is True

    crop = np.zeros((200, 120, 3), dtype=np.uint8)

    # 1) 작은 얼굴(< 40px)은 predictor 호출 전에 건너뛴다.
    small_res = embedder.extract(crop, keypoints_coco17=_pose_with_head_span(10.0))
    assert small_res is None
    assert calls == 0

    # 2) 충분히 큰 정면 얼굴(>= 40px)은 L2 정규화된 임베딩을 반환한다.
    large_res = embedder.extract(crop, keypoints_coco17=_pose_with_head_span(25.0))
    assert large_res is not None
    assert calls == 1
    assert np.allclose(large_res.embedding, [0.6, 0.8, 0.0, 0.0], atol=1e-6)
    assert large_res.face_size_px >= 40.0
