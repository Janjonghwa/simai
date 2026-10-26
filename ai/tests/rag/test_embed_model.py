"""bge-m3 로 다시 계산한 임베딩이 커밋된 값과 같은지 (S15P31A101-27).

extra `rag`(torch·transformers)와 고정 revision 가중치가 로컬 캐시에 있을 때만 돈다(CI 는 건너뜀). 같은 기계·같은 판이면
비트 단위로 같지만, 다른 기계(BLAS·CPU 명령어)에서도 통과하도록 허용오차로 비교한다(docs/design/rag-clause-index.md).
"""

import math

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")
hub = pytest.importorskip("huggingface_hub")

from simai_ai.rag import seed  # noqa: E402
from simai_ai.rag.embed import DIM, MODEL, REVISION  # noqa: E402

TOL = 1e-5

if not isinstance(hub.try_to_load_from_cache(MODEL, "pytorch_model.bin", revision=REVISION), str):
    pytest.skip(f"{MODEL}@{REVISION[:8]} 가중치가 로컬 캐시에 없다", allow_module_level=True)


@pytest.fixture(scope="module")
def model():
    from simai_ai.rag.embed import BgeM3

    return BgeM3(local_files_only=True)


def _sample():
    """짧은 조항 3개·나뉜 청크 1개·질문 템플릿 2개(시험 시간을 줄인다)."""
    items = seed.inputs()
    clauses = sorted(items["clauses"].items(), key=lambda kv: len(kv[1]))
    split = next(kv for kv in clauses if "#" in kv[0])
    picks = [("clauses", k, t) for k, t in [*clauses[:3], split]]
    picks += [("templates", k, t) for k, t in list(items["templates"].items())[:2]]
    return picks


def test_recomputed_embeddings_match_committed(model):
    committed = seed.vectors()
    picks = _sample()
    got = model.embed([t for _, _, t in picks])
    for (kind, key, _), v in zip(picks, got, strict=True):
        assert len(v) == DIM
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6
        diff = max(abs(a - b) for a, b in zip(v, committed[kind][key], strict=True))
        assert diff <= TOL, (key, diff)


def test_rerun_is_bitwise_identical(model):
    text = _sample()[0][2]
    assert model.embed([text]) == model.embed([text])
