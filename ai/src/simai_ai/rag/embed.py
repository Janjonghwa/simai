"""BAAI/bge-m3 dense 임베딩 (명세 5.4·11장: 1024차원, float32, L2 정규화, cosine. 문서·질문에 같은 revision·설정).

dense 출력 = 마지막 은닉 상태의 CLS 토큰을 L2 정규화한 값(FlagEmbedding BGEM3FlagModel 의 dense 와 같은 계산).
CPU fp32, 배치 1(패딩 없음)로 돌린다. 같은 기계·같은 판이면 다시 돌려도 비트 단위로 같다(설계 문서 실측).
torch·transformers 는 extra `rag` 에만 있으므로 함수 안에서 불러온다.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MODEL = "BAAI/bge-m3"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"  # main, 2024-07-03. 바꾸면 모든 조항·질문을 다시 임베딩한다
DIM = 1024
MAX_LENGTH = 8192
SETTINGS = {"pooling": "cls", "normalize": "l2", "dtype": "float32", "device": "cpu", "batch_size": 1, "max_length": MAX_LENGTH}


class BgeM3:
    def __init__(self, revision: str = REVISION, *, local_files_only: bool = False) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(MODEL, revision=revision, local_files_only=local_files_only)
        self._model = AutoModel.from_pretrained(
            MODEL, revision=revision, dtype=torch.float32, local_files_only=local_files_only
        ).eval()

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        torch = self._torch
        out = []
        with torch.inference_mode():
            for text in texts:
                batch = self._tok([text], truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
                cls = self._model(**batch).last_hidden_state[:, 0]
                out.append(torch.nn.functional.normalize(cls, dim=-1)[0].tolist())
        return out


def runtime() -> dict[str, Any]:
    import torch
    import transformers

    return {"torch": torch.__version__, "transformers": transformers.__version__}
