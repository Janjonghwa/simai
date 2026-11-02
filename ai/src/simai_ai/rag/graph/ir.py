"""수집 경로가 함께 쓰는 중간 표현(LawDoc)과 조문 줄 분류.

Open API(조문단위·항·호·목)와 웹 본문 HTML(문단 줄)은 모양이 다르다. 둘 다 "조 머리(제N조(제목))와 문서 순서의 줄"로 펴서
같은 분류기(`ArticleBuilder.add_line`)에 넣는다. 그래서 같은 판이면 두 경로가 같은 LawDoc 을 만든다(시험).

줄 분류(줄 앞머리로만 정한다):
- 항: 원문자 ①~㊿ → `Paragraph`
- 호: `1.` `1의2.` → 마지막 항의 호(항이 없으면 조의 호)
- 목: `가.`~`하.` → 마지막 호의 목
- 그 밖: 바로 앞 요소의 이어지는 줄(1) 같은 세목, 표 설명 등)
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_CIRCLED = {chr(0x2460 + i): i + 1 for i in range(20)}  # ①-⑳
_CIRCLED.update({chr(0x3251 + i): i + 21 for i in range(15)})  # ㉑-㉟
_CIRCLED.update({chr(0x32B1 + i): i + 36 for i in range(15)})  # ㊱-㊿
SUBITEM_CHARS = "가나다라마바사아자차카타파하"
_ITEM_RE = re.compile(r"^(\d+)(?:의(\d+))?\.(?=\s|$)")
_SUBITEM_RE = re.compile(rf"^([{SUBITEM_CHARS}])\.(?=\s|$)")
LABEL_RE = re.compile(r"^제(\d+)조(?:의(\d+))?\s*(?:\((.*)\))?\s*$")
_HEAD_RE = re.compile(r"^(제\d+조(?:의\d+)?)\s*(\((?:[^()]|\([^()]*\))*\))?\s*(.*)$", re.S)
_DELETED_RE = re.compile(r"^삭제\s*(<[^<>]*>)?\s*$")


@dataclass
class SubItem:
    number: str  # "가"
    text: str


@dataclass
class Item:
    number: int
    branch: int  # 1의2 → 2, 없으면 0
    text: str
    subitems: list[SubItem] = field(default_factory=list)


@dataclass
class Paragraph:
    number: int
    text: str
    items: list[Item] = field(default_factory=list)


@dataclass
class Article:
    number: int
    branch: int
    title: str | None
    body: str  # 항 번호 없는 본문(항이 있으면 보통 비어 있다)
    paragraphs: list[Paragraph] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)  # 항 없이 바로 달린 호
    chapter: str | None = None  # "제6장 … > 제1절 …"
    effective: str | None = None  # 조문 시행일 YYYYMMDD(API 만 준다)
    deleted: bool = False

    @property
    def label(self) -> str:
        return f"제{self.number}조" + (f"의{self.branch}" if self.branch else "")


@dataclass
class Appendix:
    number: int
    branch: int
    title: str
    text: str | None = None  # 웹 본문은 제목만 준다(내용은 HWP·PDF 첨부)

    @property
    def label(self) -> str:
        return f"별표{self.number}" + (f"의{self.branch}" if self.branch else "")


@dataclass
class LawDoc:
    law_key: str
    law_id: str
    law_name: str
    mst: str  # 법령일련번호(웹 lsiSeq)
    law_kind: str | None  # 법종구분(법률·대통령령·고용노동부령)
    promulgation_date: str
    promulgation_no: str
    effective_date: str  # 이 본문이 보여 주는 시행일(YYYYMMDD)
    source: str  # api-xml | api-json | web-html
    source_url: str  # OC 를 뺀 주소
    raw_sha256: str
    fetched_at: str  # UTC ISO
    articles: list[Article] = field(default_factory=list)
    appendices: list[Appendix] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(doc: dict[str, Any]) -> LawDoc:
        def items(xs: list[dict[str, Any]]) -> list[Item]:
            return [Item(x["number"], x["branch"], x["text"], [SubItem(**s) for s in x["subitems"]]) for x in xs]

        arts = [
            Article(
                a["number"], a["branch"], a["title"], a["body"],
                [Paragraph(p["number"], p["text"], items(p["items"])) for p in a["paragraphs"]],
                items(a["items"]), a.get("chapter"), a.get("effective"), a.get("deleted", False),
            )
            for a in doc["articles"]
        ]
        rest = {k: v for k, v in doc.items() if k not in ("articles", "appendices")}
        return LawDoc(**rest, articles=arts, appendices=[Appendix(**x) for x in doc["appendices"]])


def paragraph_number(text: str) -> int | None:
    return _CIRCLED.get(text[:1])


def parse_label(label: str) -> tuple[int, int, str | None]:
    m = LABEL_RE.match(label.strip())
    if not m:
        raise ValueError(f"조 머리가 아니다: {label!r}")
    return int(m.group(1)), int(m.group(2) or 0), (m.group(3) or None)


def split_head(line: str) -> tuple[str, str]:
    """"제9조(작업발판 등) 사업주는 …" → ("제9조(작업발판 등)", "사업주는 …"). 제목 안 괄호 한 겹까지 허용."""
    m = _HEAD_RE.match(line.strip())
    if not m:
        raise ValueError(f"조 머리로 시작하지 않는다: {line[:40]!r}")
    return (m.group(1) + (m.group(2) or "")), m.group(3).strip()


class ArticleBuilder:
    """조 하나를 줄 단위로 채운다."""

    def __init__(self, label: str, *, chapter: str | None = None, effective: str | None = None) -> None:
        number, branch, title = parse_label(label)
        self.article = Article(number, branch, title, "", chapter=chapter, effective=effective)

    def _last_item(self) -> Item | None:
        a = self.article
        if a.paragraphs and a.paragraphs[-1].items:
            return a.paragraphs[-1].items[-1]
        if not a.paragraphs and a.items:
            return a.items[-1]
        return None

    def add_line(self, line: str) -> None:
        line = " ".join(line.split())
        if not line:
            return
        a = self.article
        if (n := paragraph_number(line)) is not None:
            a.paragraphs.append(Paragraph(n, line))
            return
        if m := _ITEM_RE.match(line):
            item = Item(int(m.group(1)), int(m.group(2) or 0), line)
            (a.paragraphs[-1].items if a.paragraphs else a.items).append(item)
            return
        last_item = self._last_item()
        if (m := _SUBITEM_RE.match(line)) and last_item is not None:
            last_item.subitems.append(SubItem(m.group(1), line))
            return
        # 이어지는 줄: 가장 마지막에 생긴 요소에 붙인다
        if last_item is not None and last_item.subitems:
            last_item.subitems[-1].text += "\n" + line
        elif last_item is not None:
            last_item.text += "\n" + line
        elif a.paragraphs:
            a.paragraphs[-1].text += "\n" + line
        else:
            a.body = f"{a.body}\n{line}" if a.body else line

    def finish(self) -> Article:
        a = self.article
        if not a.paragraphs and not a.items and _DELETED_RE.match(a.body):
            a.deleted = True
        return a


def article_lines(a: Article) -> list[str]:
    """조 본문을 문서 순서의 줄로(머리 줄 제외). 임베딩 입력·전체 본문에 쓴다."""
    out = [a.body] if a.body else []

    def items(xs: list[Item]) -> None:
        for it in xs:
            out.append(it.text)
            out.extend(s.text for s in it.subitems)

    items(a.items)
    for p in a.paragraphs:
        out.append(p.text)
        items(p.items)
    return out
