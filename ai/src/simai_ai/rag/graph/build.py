"""LawDoc 목록 → 노드·엣지(그래프).

노드 ID 는 `{법령 short} {위치}`(catalog.py). 노드 종류:
LAW · ARTICLE · PARAGRAPH · ITEM · SUBITEM · APPENDIX · EXTERNAL_LAW(대상 밖 법령, 「」 인용만) ·
CONCEPT(위험요인·설비·방호조치, enrich.py) · RISK_RULE(R1~R7, enrich.py). 2단계: GUIDE · CASE.

엣지 종류(src → dst):
- CONTAINS: 상위 → 하위(법령 → 조 → 항 → 호 → 목, 법령 → 별표). 구조라 status=auto.
- REFERS_TO: 본문이 가리키는 위치(attrs.cue 따른|null). MUTATIS_MUTANDIS: "…를 준용". NOTWITHSTANDING: "…에도 불구하고".
- CITES_EXTERNAL: 대상 밖 법령 인용(dst EXTERNAL_LAW, attrs.loc 에 조문 위치).
- DELEGATES: 상위 조 → 하위 조(하위 조가 상위 조를 가리키고 상위 조에 위임 문구가 있음, method=ref+cue),
  상위 조 → 하위 법령(하위 법령 목적 조가 "…에서 위임한/…에 따라" 로 상위 조를 가리킴, method=purpose).
- APPENDIX_RELATES: 별표 → 조("[별표 1] …(제16조ㆍ제17조 및 제225조 관련)").
- 개념·규칙 태그 엣지는 enrich.py.

가장 구체적인 노드(그 문장이 있는 항·호·목)에서 엣지를 낸다. 조 단위 집계는 CONTAINS 로 올라가면 된다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from simai_ai.rag.chunk import strip_amendment_tags
from simai_ai.rag.graph import catalog
from simai_ai.rag.graph.ir import Appendix, Article, Item, LawDoc, article_lines
from simai_ai.rag.graph.refs import Context, Loc, Ref, abbreviations, extract

EMBED_APPENDIX_CHARS = 2000
_REF_EDGE = {None: "REFERS_TO", "따른": "REFERS_TO", "준용": "MUTATIS_MUTANDIS", "불구하고": "NOTWITHSTANDING"}
_PURPOSE_CUE_RE = re.compile(r"위임|에\s?따라|에\s?따른")


@dataclass
class Node:
    node_id: str
    node_type: str
    law_key: str | None
    law_name: str | None
    label: str | None  # 법령 안 위치(제42조제2항). 법령·개념 노드는 null
    title: str | None  # 조 제목·별표 제목·개념 이름
    text: str  # 그 노드의 원문(하위 포함)
    parent_id: str | None
    ordinal: int
    attrs: dict[str, Any] = field(default_factory=dict)
    embedding_input: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Edge:
    src: str
    dst: str
    edge_type: str
    method: str  # structure | regex | ref+cue | purpose | keyword | lexicon | spec
    status: str  # auto | candidate(후보, 사람 검수 전) | spec(명세 근거) | reviewed
    evidence: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.edge_type)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], Edge] = field(default_factory=dict)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    dropped: Counter[str] = field(default_factory=Counter)

    def add_node(self, n: Node) -> Node:
        if n.node_id in self.nodes:
            raise ValueError(f"노드 ID 가 겹친다: {n.node_id}")
        self.nodes[n.node_id] = n
        return n

    def add_edge(self, e: Edge) -> None:
        if e.src == e.dst:
            return
        old = self.edges.get(e.key)
        if old is None:
            self.edges[e.key] = e
        elif old.status == "candidate" and e.status in ("auto", "spec"):
            self.edges[e.key] = e

    def ancestors(self, node_id: str) -> set[str]:
        out, cur = set(), self.nodes.get(node_id)
        while cur is not None and cur.parent_id:
            out.add(cur.parent_id)
            cur = self.nodes.get(cur.parent_id)
        return out

    def article_of(self, node_id: str) -> str | None:
        cur = self.nodes.get(node_id)
        while cur is not None and cur.node_type not in ("ARTICLE", "APPENDIX", "LAW"):
            cur = self.nodes.get(cur.parent_id) if cur.parent_id else None
        return cur.node_id if cur is not None and cur.node_type in ("ARTICLE", "APPENDIX") else None

    def stats(self) -> dict[str, Any]:
        return {
            "nodes": dict(sorted(Counter(n.node_type for n in self.nodes.values()).items())),
            "edges": dict(sorted(Counter(e.edge_type for e in self.edges.values()).items())),
            "edges_by_status": dict(sorted(Counter(e.status for e in self.edges.values()).items())),
            "nodes_by_law": dict(sorted(Counter(n.law_key for n in self.nodes.values() if n.law_key).items())),
            "embedded_nodes": sum(1 for n in self.nodes.values() if n.embedding_input),
            "unresolved_refs": len(self.unresolved),
            "dropped_refs": dict(sorted(self.dropped.items())),
        }


def node_id(spec: catalog.LawSpec, label: str = "") -> str:
    return f"{spec.short} {label}".strip()


def _clean(text: str) -> str:
    return " ".join(strip_amendment_tags(text).split())


def _head(doc: LawDoc, a: Article) -> str:
    return f"{a.label}({a.title})" if a.title else a.label


def article_embedding_input(doc: LawDoc, a: Article) -> str:
    """"[법령명 조문(제목)] 본문"(결정 2026-11-02). 개정 꼬리표는 뺀다."""
    return f"[{doc.law_name} {_head(doc, a)}] " + " ".join(_clean(t) for t in article_lines(a))


def _item_text(it: Item) -> str:
    return "\n".join([it.text, *(s.text for s in it.subitems)])


def _add_items(g: Graph, doc: LawDoc, spec: catalog.LawSpec, parent: Node, prefix: str, items: list[Item]) -> None:
    for i, it in enumerate(items):
        label = f"{prefix}제{it.number}호" + (f"의{it.branch}" if it.branch else "")
        node = g.add_node(Node(node_id(spec, label), "ITEM", spec.key, doc.law_name, label, None, _item_text(it), parent.node_id, i))
        for j, s in enumerate(it.subitems):
            sl = f"{label}{s.number}목"
            g.add_node(Node(node_id(spec, sl), "SUBITEM", spec.key, doc.law_name, sl, None, s.text, node.node_id, j))


def _add_law(g: Graph, doc: LawDoc) -> None:
    spec = catalog.spec(doc.law_key)
    root = g.add_node(Node(node_id(spec), "LAW", spec.key, doc.law_name, None, doc.law_name, "", None, 0, {
        "law_id": doc.law_id, "level": spec.level, "law_kind": doc.law_kind, "mst": doc.mst,
        "effective_date": doc.effective_date, "promulgation_date": doc.promulgation_date,
        "promulgation_no": doc.promulgation_no, "source": doc.source, "raw_sha256": doc.raw_sha256,
    }))
    seen: set[str] = set()
    for i, a in enumerate(doc.articles):
        if a.label in seen:  # 같은 번호 조가 두 번(부칙 섞임 등) 오면 앞의 것만
            g.dropped["duplicate_article"] += 1
            continue
        seen.add(a.label)
        lines = article_lines(a)
        art = g.add_node(Node(
            node_id(spec, a.label), "ARTICLE", spec.key, doc.law_name, a.label, a.title,
            "\n".join([_head(doc, a), *lines]), root.node_id, i,
            {"chapter": a.chapter, "deleted": a.deleted, "effective": a.effective or doc.effective_date},
            None if a.deleted else article_embedding_input(doc, a),
        ))
        _add_items(g, doc, spec, art, a.label, a.items)
        multi = len(a.paragraphs) >= 2
        for j, p in enumerate(a.paragraphs):
            pl = f"{a.label}제{p.number}항"
            if node_id(spec, pl) in g.nodes:  # 같은 항 번호가 두 번(원문 오류) — 뒤 것은 앞 항의 이어지는 줄로 본다
                g.dropped["duplicate_paragraph"] += 1
                continue
            ptext = "\n".join([p.text, *(_item_text(it) for it in p.items)])
            emb = f"[{doc.law_name} {_head(doc, a)} 제{p.number}항] {_clean(ptext)}" if multi and not a.deleted else None
            pn = g.add_node(Node(node_id(spec, pl), "PARAGRAPH", spec.key, doc.law_name, pl, a.title, ptext, art.node_id, j, {}, emb))
            _add_items(g, doc, spec, pn, pl, p.items)
    for i, x in enumerate(doc.appendices):
        text = x.text or x.title
        emb = f"[{doc.law_name} 별표 {x.label[2:]}] {x.title}" + (f" {_clean(x.text)[:EMBED_APPENDIX_CHARS]}" if x.text else "")
        g.add_node(Node(node_id(spec, x.label), "APPENDIX", spec.key, doc.law_name, x.label, x.title, text, root.node_id,
                        len(doc.articles) + i, {"content_available": x.text is not None}, emb))


def _structure_edges(g: Graph) -> None:
    for n in list(g.nodes.values()):
        if n.parent_id:
            g.add_edge(Edge(n.parent_id, n.node_id, "CONTAINS", "structure", "auto"))


def resolve(g: Graph, ref: Ref) -> tuple[str | None, str]:
    """참조 → (노드 ID, 해석 수준 exact|ancestor|law|external|unresolved)."""
    spec = catalog.by_name(ref.law)
    if spec is None:
        return None, "external"
    if ref.loc.depth() == 0:
        return node_id(spec), "law"
    loc = ref.loc
    tries = [loc, Loc(loc.article, loc.paragraph, loc.item, None, loc.appendix), Loc(loc.article, loc.paragraph, None, None, loc.appendix),
             Loc(loc.article, None, None, None, loc.appendix)]
    if loc.item and loc.paragraph is not None:  # 항 없이 호가 바로 달린 조를 "제N조제1항제2호" 로 쓴 경우
        tries.insert(2, Loc(loc.article, None, loc.item, loc.subitem))
    for i, t in enumerate(tries):
        nid = node_id(spec, t.label())
        if t.depth() and nid in g.nodes:
            return nid, "exact" if i == 0 else "ancestor"
    return None, "unresolved"


def _external_node(g: Graph, name: str) -> str:
    nid = f"외부법령 {name}"
    if nid not in g.nodes:
        g.add_node(Node(nid, "EXTERNAL_LAW", None, name, None, name, "", None, 0))
    return nid


def _evidence(text: str, ref: Ref, pad: int = 25) -> str:
    return " ".join(text[max(0, ref.start - pad):ref.end + pad].split())


def _ref_edges(g: Graph, src: str, text: str, ctx: Context, *, edge_type_override: str | None = None) -> list[tuple[Ref, str]]:
    out = []
    for ref in extract(text, ctx):
        dst, level = resolve(g, ref)
        if level == "external":
            ext = _external_node(g, ref.law)
            loc = ref.loc.label() or None
            old = g.edges.get((src, ext, "CITES_EXTERNAL"))
            if old is not None:  # 한 노드가 같은 외부 법령의 여러 위치를 인용하면 위치를 모은다
                if loc and loc not in old.attrs["locs"]:
                    old.attrs["locs"].append(loc)
                continue
            g.add_edge(Edge(src, ext, "CITES_EXTERNAL", "regex", "auto", _evidence(text, ref), {"locs": [loc] if loc else [], "cue": ref.cue}))
            continue
        if dst is None:
            if ref.form != "range":
                g.unresolved.append({"src": src, "law": ref.law, "loc": ref.loc.label(), "surface": ref.surface, "form": ref.form})
            continue
        if dst == src or dst in g.ancestors(src):
            g.dropped["self_or_ancestor"] += 1
            continue
        etype = edge_type_override or _REF_EDGE[ref.cue]
        g.add_edge(Edge(src, dst, etype, "regex", "auto", _evidence(text, ref), {"cue": ref.cue, "form": ref.form, "resolved": level}))
        out.append((ref, dst))
        if ref.form == "range" and ref.loc.depth() == 1 and ref.loc.article:  # 범위 안 가지 조문(제38조의2 등)
            spec = catalog.by_name(ref.law)
            n = ref.loc.article[0]
            b = 2
            while spec and node_id(spec, f"제{n}조의{b}") in g.nodes:
                g.add_edge(Edge(src, node_id(spec, f"제{n}조의{b}"), etype, "regex", "auto", _evidence(text, ref), {"cue": ref.cue, "form": "range"}))
                b += 1
    return out


def _law_text(doc: LawDoc) -> str:
    return "\n".join(t for a in doc.articles for t in article_lines(a))


def _article_refs(g: Graph, doc: LawDoc, spec: catalog.LawSpec, abbr: dict[str, str]) -> list[tuple[str, Ref, str]]:
    """조마다 본문·항·호·목을 문서 순서로 읽어 참조 엣지를 만든다. (출발 노드, 참조, 도착 노드) 목록을 돌려준다."""
    found: list[tuple[str, Ref, str]] = []
    for a in doc.articles:
        art_id = node_id(spec, a.label)
        if a.deleted or art_id not in g.nodes:
            continue
        ctx = Context(doc.law_name, abbr, (a.number, a.branch))

        def run(src: str, text: str) -> None:
            for ref, dst in _ref_edges(g, src, text, ctx):
                found.append((src, ref, dst))

        def items(prefix: str, xs: list[Item], para: int | None) -> None:
            for it in xs:
                il = f"{prefix}제{it.number}호" + (f"의{it.branch}" if it.branch else "")
                ctx.at(paragraph=para, item=(it.number, it.branch))
                run(node_id(spec, il), it.text)
                for s in it.subitems:
                    run(node_id(spec, f"{il}{s.number}목"), s.text)

        if a.body:
            ctx.at()
            run(art_id, a.body)
        items(a.label, a.items, None)
        for p in a.paragraphs:
            pid = node_id(spec, f"{a.label}제{p.number}항")
            ctx.at(paragraph=p.number)
            run(pid if pid in g.nodes else art_id, p.text)
            items(f"{a.label}제{p.number}항", p.items, p.number)
    return found


def _appendix_refs(g: Graph, doc: LawDoc, spec: catalog.LawSpec, abbr: dict[str, str]) -> None:
    for x in doc.appendices:
        src = node_id(spec, x.label)
        m = re.search(r"\(([^()]*관련)\)\s*$", x.title)
        if m:
            _ref_edges(g, src, m.group(1), Context(doc.law_name, abbr), edge_type_override="APPENDIX_RELATES")
        if x.text:
            _ref_edges(g, src, x.text, Context(doc.law_name, abbr))


def _delegation_edges(g: Graph, refs: dict[str, list[tuple[str, Ref, str]]]) -> None:
    """하위 법령 조문의 참조 중 상위 법령으로 가는 것에서 위임 엣지를 만든다.

    - purpose: 하위 법령 목적 조가 "…에서 위임한/…에 따라" 로 상위 조를 가리키면 상위 조 → 하위 법령 전체.
    - ref+cue: 가리킨 상위 위치(항·호까지 구체적인 노드의 본문)에 하위 법령으로 넘기는 문구(대통령령·고용노동부령)가 있으면
      상위 조 → 하위 조. 정의 조처럼 조 전체에만 문구가 있는 경우를 피하려고 가리킨 노드 본문에서 찾는다.
    """
    for key, found in refs.items():
        child = catalog.spec(key)
        if not child.parents:
            continue
        for src, ref, dst in found:
            if g.nodes[dst].law_key not in child.parents:
                continue
            dst_art, src_art = g.article_of(dst), g.article_of(src)
            if dst_art is None or src_art is None:
                continue
            src_article = g.nodes[src_art]
            if src_article.title == "목적" and _PURPOSE_CUE_RE.search(src_article.text):
                g.add_edge(Edge(dst_art, node_id(child), "DELEGATES", "purpose", "auto", ref.surface, {"via": src_art}))
                continue
            cue = child.delegation_cue
            if cue and cue in g.nodes[dst].text:
                g.add_edge(Edge(dst_art, src_art, "DELEGATES", "ref+cue", "auto", ref.surface, {"via": src, "cue": cue, "ref_node": dst}))


def build(docs: list[LawDoc]) -> Graph:
    g = Graph()
    for d in docs:
        _add_law(g, d)
    _structure_edges(g)
    refs: dict[str, list[tuple[str, Ref, str]]] = {}
    for d in docs:
        spec = catalog.spec(d.law_key)
        abbr = abbreviations(_law_text(d), spec.default_abbr)
        refs[d.law_key] = _article_refs(g, d, spec, abbr)
        _appendix_refs(g, d, spec, abbr)
    _delegation_edges(g, refs)
    return g


__all__ = ["Appendix", "Edge", "Graph", "Node", "build", "node_id", "resolve"]
