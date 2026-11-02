"""Qwen3-Embedding-0.6B 문서·질의 임베딩(결정 2026-11-02).

- revision 고정(아래 REVISION). 1024차원, 마지막 토큰 풀링(왼쪽 패딩), L2 정규화, float32. 모델 카드(같은 revision README)의
  transformers 사용법 그대로다.
- 문서(노드)는 지시문 없이 "[법령명 조문(제목)] 본문", 질의(표준 질의)는 모델 카드 권장 형식
  `Instruct: {과제 설명}\\nQuery:{질의}`. 카드가 다국어에서도 지시문은 영어로 쓰라고 권해 과제 설명은 영어다.
- 길이순으로 묶어 배치로 돌린다(배치 1 과 수치가 1e-5 안팎으로 다를 수 있다. 같은 기계·같은 배치면 다시 돌려도 같다).
- torch·transformers 는 extra `rag` 에만 있어 함수 안에서 불러온다. 노트북 CPU(또는 RTX 4050 6GB)에서 돈다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MODEL = "Qwen/Qwen3-Embedding-0.6B"
REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"  # main, 2026-04-20 수정. 바꾸면 노드·질의를 모두 다시 임베딩한다
DIM = 1024
MAX_LENGTH = 8192
QUERY_INSTRUCTION = (
    "Given a Korean description of a construction-site safety risk, "
    "retrieve the Korean occupational safety law provisions that require the relevant protective measures"
)
SETTINGS = {"pooling": "last_token", "padding_side": "left", "normalize": "l2", "dtype": "float32", "max_length": MAX_LENGTH,
            "document_prefix": "", "query_format": "Instruct: {instruction}\\nQuery:{query}"}


def query_input(text: str) -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery:{text}"


class Qwen3Embedder:
    def __init__(self, revision: str = REVISION, *, device: str = "cpu", local_files_only: bool = False) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self.device = device
        self._tok = AutoTokenizer.from_pretrained(MODEL, revision=revision, padding_side="left", local_files_only=local_files_only)
        self._model = AutoModel.from_pretrained(MODEL, revision=revision, dtype=torch.float32, local_files_only=local_files_only).to(device).eval()

    def embed(self, texts: Sequence[str], batch_size: int = 8) -> list[list[float]]:
        torch = self._torch
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out: list[list[float] | None] = [None] * len(texts)
        with torch.inference_mode():
            s = 0
            while s < len(order):
                step = batch_size
                while step > 1 and len(texts[order[min(s + step - 1, len(order) - 1)]]) * step > 6000:
                    step //= 2
                idx = order[s:s + step]
                s += step
                batch = self._tok([texts[i] for i in idx], padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt").to(self.device)
                hidden = self._model(**batch).last_hidden_state
                mask = batch["attention_mask"]
                if bool((mask[:, -1] == 1).all()):  # 왼쪽 패딩: 마지막 자리가 마지막 토큰
                    pooled = hidden[:, -1]
                else:
                    pooled = hidden[torch.arange(hidden.shape[0], device=hidden.device), mask.sum(dim=1) - 1]
                vecs = torch.nn.functional.normalize(pooled.float(), p=2, dim=-1).cpu()
                for k, i in enumerate(idx):
                    out[i] = vecs[k].tolist()
        return [v for v in out if v is not None]

    def token_count(self, text: str) -> int:
        return len(self._tok(text)["input_ids"])


def runtime() -> dict[str, Any]:
    import torch
    import transformers

    return {"torch": torch.__version__, "transformers": transformers.__version__}


def weights_sha256(revision: str = REVISION) -> str | None:
    """HF 캐시의 model.safetensors sha256(manifest 용). 캐시에 없으면 None."""
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    path = try_to_load_from_cache(MODEL, "model.safetensors", revision=revision)
    if not isinstance(path, str):
        return None
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
