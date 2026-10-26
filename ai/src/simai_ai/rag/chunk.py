"""조항 → 청크·조항 ID·임베딩 입력 (docs/design/rag-clause-index.md "분할 규칙").

- 조 단위가 기본이다. 조 본문(머리 줄 + 단위)이 LIMIT_CHARS 를 넘으면 단위(항, 항이 없으면 호) 경계에서 앞에서부터
  LIMIT_CHARS 이하로 묶는다. 단위 하나가 넘으면 그 단위만 한 청크다(항·호 중간에서 끊지 않는다).
- 모든 청크는 조의 머리 줄로 시작한다: 항이 있는 조는 "제N조(제목)", 호만 있는 조는 "제N조(제목) 사업주는 … 각 호 …" 라
  청크 하나만 읽어도 뜻이 통한다.
- ID: 청크가 하나면 "산업안전보건기준규칙 제N조", 여럿이면 모든 청크가 "…제N조#k"(k = chunk_index, 0부터).
- body 는 원문 그대로(개정 꼬리표 포함), 임베딩 입력은 법령 이름을 앞에 붙이고 개정 꼬리표를 뺀 문자열이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

ID_PREFIX = "산업안전보건기준규칙"
LIMIT_CHARS = 1000
CLAUSE_ID_RE = re.compile(r"^산업안전보건기준규칙 제[1-9][0-9]*조(의[1-9][0-9]*)?(#(0|[1-9][0-9]*))?$")
# <개정 2017.12.28, 2021.5.28>, <신설 2024.6.28>, [본조신설 2012.5.31] 처럼 날짜가 든 꼬리표
_AMEND_TAG_RE = re.compile(r"\s*(<[^<>]*\d{4}\.\s?\d{1,2}\.\s?\d{1,2}\.?[^<>]*>|\[[^\[\]]*\d{4}\.\s?\d{1,2}\.\s?\d{1,2}\.?[^\[\]]*\])")


@dataclass(frozen=True)
class Chunk:
    clause_id: str
    law_name: str
    article: str
    title: str | None
    body: str
    chunk_index: int
    source_version: str

    @property
    def embedding_input(self) -> str:
        return embedding_text(self.law_name, self.body)


def strip_amendment_tags(text: str) -> str:
    return _AMEND_TAG_RE.sub("", text)


def embedding_text(law_name: str, body: str) -> str:
    return f"{law_name} {strip_amendment_tags(body)}"


def _pack(head: str, units: list[str], limit: int) -> list[str]:
    bodies: list[list[str]] = []
    for unit in units:
        if bodies and len("\n".join([head, *bodies[-1], unit])) <= limit:
            bodies[-1].append(unit)
        else:
            bodies.append([unit])
    return ["\n".join([head, *b]) for b in bodies] or [head]


def chunk_article(article: dict[str, Any], *, law_name: str, source_version: str, limit: int = LIMIT_CHARS) -> list[Chunk]:
    head, units = article["head"], article["units"]
    whole = "\n".join([head, *units])
    bodies = [whole] if len(whole) <= limit else _pack(head, units, limit)
    base = f"{ID_PREFIX} {article['article']}"
    return [
        Chunk(
            clause_id=base if len(bodies) == 1 else f"{base}#{i}",
            law_name=law_name,
            article=article["article"],
            title=article["title"],
            body=body,
            chunk_index=i,
            source_version=source_version,
        )
        for i, body in enumerate(bodies)
    ]


def chunks(doc: dict[str, Any], limit: int = LIMIT_CHARS) -> list[Chunk]:
    """조항 파일(law.normalize 결과) → 청크 목록. source_version 은 조문 시행일(YYYY-MM-DD)."""
    out: list[Chunk] = []
    for a in doc["articles"]:
        d = a["effective"]
        out += chunk_article(a, law_name=doc["law_name"], source_version=f"{d[:4]}-{d[4:6]}-{d[6:]}", limit=limit)
    return out
