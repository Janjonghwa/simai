"""Qwen3-Embedding-0.6B 임베딩 모듈 시험 (S15P31A101-416).

extra `rag`(torch·transformers)와 고정 revision 가중치가 로컬 캐시에 있을 때만 돈다(CI 는 건너뜀).
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")
hub = pytest.importorskip("huggingface_hub")

from simai_ai.rag.graph import embed_qwen  # noqa: E402

if not isinstance(hub.try_to_load_from_cache(embed_qwen.MODEL, "model.safetensors", revision=embed_qwen.REVISION), str):
    pytest.skip(f"{embed_qwen.MODEL}@{embed_qwen.REVISION[:8]} 가중치가 로컬 캐시에 없다", allow_module_level=True)


@pytest.fixture(scope="module")
def embedder():
    return embed_qwen.Qwen3Embedder(local_files_only=True)


def test_qwen3_embedding_dim_norm_and_determinism(embedder):
    doc = "[산업안전보건기준에 관한 규칙 제44조(안전대의 부착설비 등)] ① 사업주는 추락할 위험이 있는 높이 2미터 이상의 장소에서 안전대 부착설비를 설치하여야 한다."
    q = embed_qwen.query_input("높이 2미터 이상 추락 위험 장소에서 안전대 부착설비 설치")
    vecs = embedder.embed([doc, q], batch_size=2)
    assert len(vecs) == 2
    for v in vecs:
        assert len(v) == embed_qwen.DIM == 1024
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6
    # 관련 조문과 질의의 cosine 유사도는 양수이고 충분히 높아야 한다
    cosine = sum(a * b for a, b in zip(vecs[0], vecs[1], strict=True))
    assert cosine > 0.5
    # 같은 입력 재실행은 비트 단위로 같고, 배치 1 과의 차이는 1e-5 이내다
    assert embedder.embed([doc], batch_size=1) == embedder.embed([doc], batch_size=1)
    single = embedder.embed([doc], batch_size=1)[0]
    assert max(abs(a - b) for a, b in zip(vecs[0], single, strict=True)) < 1e-5
