"""실제 COPE 에서 정밀도 변경의 임베딩 차이를 검증한다."""

from dataclasses import replace

import pytest


@pytest.mark.gpu
def test_real_cope_fp16_cosine_difference() -> None:
    np = pytest.importorskip("numpy")
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("CUDA 없음")
    from simai_ai.reid import COPEConfig, COPEEmbedder

    cfg = COPEConfig.from_env(device="cuda", batch_size=2)
    if not (cfg.cope_root.is_dir() and cfg.weights_path.is_file() and cfg.clip_path.is_file()):
        pytest.skip("COPE 저장소 또는 가중치 없음")
    embedder = COPEEmbedder(cfg)
    crops = [np.random.default_rng(i).integers(0, 256, (160, 80, 3), dtype=np.uint8) for i in range(2)]
    fp32 = embedder.embed(crops)
    embedder._config = replace(cfg, fp16=True)
    fp16 = embedder.embed(crops)
    assert fp16.dtype == np.float32
    assert np.allclose(np.linalg.norm(fp16, axis=1), 1.0, atol=1e-5)
    assert np.all(1.0 - np.sum(fp32 * fp16, axis=1) <= 1e-2)
