"""법령정보지식베이스 API 로 그래프를 교차 확인한다(정답이 아니라 독립 출처와의 일치 정도).

1. 관련법령(lsRlt, 법령 단위): 법제처가 매긴 법령 간 관계(하위법 등)와 그래프의 법령 쌍(참조·위임·준용·불구하고·외부 인용
   엣지가 하나라도 있는 쌍)을 비교한다. 대상 법령끼리는 양쪽 모두에 있어야 하고, 대상 밖 법령은 외부 인용 노드로 본다.
2. 연관법령(aiRltLs, 조문 단위 검색): 개념 이름(안전난간·비계 …)으로 검색한 조문 중 대상 법령 조문이 그래프의 개념 엣지
   (HAS_HAZARD·INVOLVES_EQUIPMENT·REQUIRES_CONTROL 후보)에 얼마나 들어 있는지 본다. aiRltLs 는 참조 관계가 아니라 검색이라
   참조 엣지 정확도가 아니라 개념 연결 재현의 참고값이다(긴 문장 질의는 엉뚱한 법령을 돌려준다: "개구부 추락 방지" → 동물보호법 등).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from simai_ai.rag.graph import catalog
from simai_ai.rag.graph.build import Graph, build, node_id
from simai_ai.rag.graph.enrich import enrich, lexicon
from simai_ai.rag.graph.source_api import Client, parse_xml_root

_LAW_EDGES = ("REFERS_TO", "MUTATIS_MUTANDIS", "NOTWITHSTANDING", "DELEGATES", "CITES_EXTERNAL", "APPENDIX_RELATES")
_CONCEPT_EDGES = ("HAS_HAZARD", "INVOLVES_EQUIPMENT", "REQUIRES_CONTROL")


def law_pairs(g: Graph) -> Counter[tuple[str, str]]:
    out: Counter[tuple[str, str]] = Counter()
    for e in g.edges.values():
        if e.edge_type not in _LAW_EDGES:
            continue
        a, b = g.nodes[e.src].law_name, g.nodes[e.dst].law_name
        if a and b and a != b:
            out[tuple(sorted((a, b)))] += 1  # type: ignore[arg-type]
    return out


def ai_related(client: Client, query: str) -> list[dict[str, str]]:
    raw = client.get("lawSearch.do", {"target": "aiRltLs", "type": "XML", "search": "0", "query": query}, f"ai/aiRltLs_{query}.xml")
    root: ET.Element = parse_xml_root(raw)
    return [{c.tag: (c.text or "").strip() for c in x} for x in root.findall("법령조문")]


def _article_label(row: dict[str, str]) -> str:
    n, b = int(row.get("조문번호") or 0), int(row.get("조문가지번호") or 0)
    return f"제{n}조" + (f"의{b}" if b else "")


def run(client: Client, out: Path) -> dict[str, Any]:
    from simai_ai.rag.graph.__main__ import load_docs

    docs = load_docs(out)
    g = build(docs)
    enrich(g)
    pairs = law_pairs(g)
    names = {d.law_name for d in docs}
    rows = []
    for d in docs:
        if catalog.spec(d.law_key).level == "ADMIN_RULE":
            continue
        for r in client.related_laws(d.law_id):
            other = r.get("관련법령명", "")
            in_catalog = other in names
            key = tuple(sorted((d.law_name, other)))
            rows.append({"base": d.law_name, "related": other, "relation": r.get("법령간관계"), "in_catalog": in_catalog,
                         "graph_edges": pairs.get(key, 0)})  # type: ignore[arg-type]
    cat_rows = [r for r in rows if r["in_catalog"]]
    lsrlt_pairs = {tuple(sorted((r["base"], r["related"]))) for r in rows}
    graph_only = sorted([list(k) + [v] for k, v in pairs.items() if k[0] in names and k[1] in names and k not in lsrlt_pairs])

    concept_rows = []
    for c in lexicon()["concepts"]:
        hits = [r for r in ai_related(client, c["name"].split("·")[0]) if catalog.by_name(r.get("법령명", ""))]
        cid = f"개념 {c['id']}"
        linked = {e.src for e in g.edges.values() if e.dst == cid and e.edge_type in _CONCEPT_EDGES}
        found = [h for h in hits if node_id(catalog.by_name(h["법령명"]), _article_label(h)) in linked]  # type: ignore[arg-type]
        concept_rows.append({"concept": c["id"], "query": c["name"].split("·")[0], "ai_hits_in_catalog": len(hits), "in_graph": len(found),
                             "missing": [f"{h['법령명']} {_article_label(h)}({h.get('조문제목')})" for h in hits if h not in found]})
    hit_total = sum(r["ai_hits_in_catalog"] for r in concept_rows)
    summary = {
        "lsRlt_rows": len(rows),
        "lsRlt_catalog_pairs": len(cat_rows),
        "lsRlt_catalog_pairs_with_graph_edges": sum(1 for r in cat_rows if r["graph_edges"] > 0),
        "lsRlt_external_rows": len(rows) - len(cat_rows),
        "graph_catalog_pairs_not_in_lsRlt": len(graph_only),
        "aiRltLs_catalog_hits": hit_total,
        "aiRltLs_hits_linked_in_graph": sum(r["in_graph"] for r in concept_rows),
    }
    return {"summary": summary, "lsRlt": rows, "graph_pairs_not_in_lsRlt": graph_only, "aiRltLs_concepts": concept_rows}
