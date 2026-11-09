"""임베더 인터페이스, 배치 전처리, 가중치 SHA-256 해시 검사 시험 (S15P31A101-463)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
from PIL import Image  # noqa: E402

from simai_ai.reid import (
    COPE_EMBEDDING_DIM,
    COPEConfig,
    COPEEmbedder,
    DEFAULT_CLIP_PATH,
    DEFAULT_COPE_ROOT,
    DEFAULT_COPE_WEIGHTS,
    WeightVerificationError,
    compute_file_sha256,
    l2_normalize_rows,
    preprocess_person_crop,
    verify_file_sha256,
)


class _FakeCOPEModel:
    """실제 가중치 없이 배치 분할·입력 텐서 크기(B, 3, 256, 128)·정규화를 검증하는 가짜 모델."""

    def __init__(self) -> None:
        self.recorded_batch_shapes: list[tuple[int, ...]] = []
        self.recorded_get_image: list[bool | None] = []

    def __call__(
        self,
        x: torch.Tensor,
        cam_label: torch.Tensor | None = None,
        view_label: torch.Tensor | None = None,
        get_image: bool | None = None,
    ) -> torch.Tensor | tuple[torch.Tensor, torch.Tensor]:
        del cam_label, view_label
        self.recorded_batch_shapes.append(tuple(x.shape))
        self.recorded_get_image.append(get_image)
        b = x.shape[0]
        feats = torch.zeros((b, COPE_EMBEDDING_DIM), dtype=torch.float32, device=x.device)
        # 입력 채널 평균값을 앞 차원에 넣어 배치 순서 보존을 확인한다.
        feats[:, 0] = x[:, 0].mean(dim=(1, 2)) + 2.0
        feats[:, 1] = x[:, 1].mean(dim=(1, 2)) + 3.0
        feats[:, 2] = torch.arange(1, b + 1, dtype=torch.float32, device=x.device)
        if get_image:
            return feats
        scores = torch.full((b,), 0.85, dtype=torch.float32, device=x.device)
        return feats, scores


def test_verify_file_sha256_passes_and_rejects_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "dummy.bin"
    target.write_bytes(b"simai-reid-cope-test-payload")
    actual = compute_file_sha256(target)

    assert verify_file_sha256(target, actual) == actual
    assert verify_file_sha256(target, f"SHA256:{actual.upper()}") == actual

    with pytest.raises(WeightVerificationError, match="SHA-256 불일치"):
        verify_file_sha256(target, "0" * 64)

    with pytest.raises(FileNotFoundError):
        verify_file_sha256(tmp_path / "missing.bin", actual)


def test_COPEEmbedder_rejects_bad_weights_hash_before_loading(tmp_path: Path) -> None:
    fake_root = tmp_path / "cope"
    (fake_root / "cope").mkdir(parents=True)
    (fake_root / "cope" / "model.py").write_text("# stub\n", encoding="utf-8")
    weights = tmp_path / "weights.pth"
    clip_pt = tmp_path / "ViT-B-16.pt"
    weights.write_bytes(b"not-the-real-weights")
    clip_pt.write_bytes(b"not-the-real-clip")

    cfg = COPEConfig(
        cope_root=fake_root,
        weights_path=weights,
        clip_path=clip_pt,
        weights_sha256="f" * 64,
        clip_sha256="e" * 64,
        device="cpu",
    )
    with pytest.raises(WeightVerificationError):
        COPEEmbedder(cfg)


def test_preprocess_person_crop_resizes_to_256x128_and_swaps_bgr() -> None:
    bgr = np.zeros((100, 50, 3), dtype=np.uint8)
    bgr[:, :, 0] = 0    # B = 0
    bgr[:, :, 1] = 128  # G = 128
    bgr[:, :, 2] = 255  # R = 255

    tensor_arr = preprocess_person_crop(bgr, size_hw=(256, 128), input_format="bgr")
    assert tensor_arr is not None
    assert tensor_arr.shape == (3, 256, 128)
    assert tensor_arr.dtype == np.float32
    # R 채널(0번)은 (255/255 - 0.5)/0.5 = +1.0, B 채널(2번)은 (0 - 0.5)/0.5 = -1.0
    assert np.allclose(tensor_arr[0], 1.0, atol=1e-4)
    assert np.allclose(tensor_arr[2], -1.0, atol=1e-4)

    pil_img = Image.fromarray(bgr[:, :, ::-1], mode="RGB")
    pil_arr = preprocess_person_crop(pil_img, size_hw=(256, 128))
    assert pil_arr is not None
    assert np.allclose(tensor_arr, pil_arr, atol=1e-5)

    assert preprocess_person_crop(np.zeros((1, 10, 3), dtype=np.uint8)) is None
    assert preprocess_person_crop(None) is None


def test_fake_cope_embedder_batches_and_normalizes() -> None:
    fake_model = _FakeCOPEModel()
    cfg = COPEConfig(device="cpu", batch_size=2, input_size_hw=(256, 128))
    embedder = COPEEmbedder(cfg, model=fake_model)

    crops = [
        np.full((80, 40, 3), 20 * (i + 1), dtype=np.uint8)
        for i in range(5)
    ]
    # 중간에 빈 crop 하나를 끼워도 인덱스가 밀리지 않고 영벡터로 남는지 확인한다.
    crops.insert(2, np.zeros((0, 0, 3), dtype=np.uint8))

    feats, scores = embedder.embed_with_scores(crops)
    assert feats.shape == (6, COPE_EMBEDDING_DIM)
    assert scores.shape == (6,)
    assert fake_model.recorded_batch_shapes == [
        (2, 3, 256, 128),
        (2, 3, 256, 128),
        (1, 3, 256, 128),
    ]

    for idx in (0, 1, 3, 4, 5):
        norm = float(np.linalg.norm(feats[idx]))
        assert np.isclose(norm, 1.0, atol=1e-5)
        assert np.isclose(scores[idx], 0.85, atol=1e-5)

    assert np.allclose(feats[2], 0.0)
    assert scores[2] == 0.0


def test_l2_normalize_rows_handles_zero_and_1d() -> None:
    vec = np.array([3.0, 4.0], dtype=np.float32)
    normed = l2_normalize_rows(vec)
    assert normed.shape == (1, 2)
    assert np.allclose(normed, [[0.6, 0.8]], atol=1e-6)

    zeros = np.zeros((2, 4), dtype=np.float32)
    assert np.allclose(l2_normalize_rows(zeros), 0.0)


@pytest.mark.skipif(
    not (
        Path(DEFAULT_COPE_ROOT).is_dir()
        and Path(DEFAULT_COPE_WEIGHTS).is_file()
        and Path(DEFAULT_CLIP_PATH).is_file()
    ),
    reason="로컬 COPE 저장소·가중치가 없는 환경(CI)에서는 건너뛴다",
)
def test_real_cope_embedder_cpu_smoke() -> None:
    cfg = COPEConfig.from_env(device="cpu", batch_size=2)
    embedder = COPEEmbedder(cfg)
    rng = np.random.default_rng(42)
    c1 = rng.integers(0, 255, size=(160, 80, 3), dtype=np.uint8)
    c2 = rng.integers(0, 255, size=(200, 90, 3), dtype=np.uint8)

    feats = embedder.embed([c1, c2])
    assert feats.shape == (2, COPE_EMBEDDING_DIM)
    assert feats.dtype == np.float32
    assert np.allclose(np.linalg.norm(feats, axis=1), 1.0, atol=1e-5)
    # 같은 crop 을 다시 넣으면 결정론적으로 동일한 벡터가 나온다.
    feats_again = embedder.embed([c1])
    assert float(np.dot(feats[0], feats_again[0])) > 0.9999
