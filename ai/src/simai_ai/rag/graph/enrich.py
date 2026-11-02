"""개념 엔티티(위험요인·설비·방호조치)와 위험 규칙(R1~R7) 태그 후보.

`data/graph/lexicon.json` 의 낱말이 조문에 나오면 조 → 개념, 조 → 규칙 후보 엣지를 만든다. 낱말 대조라 빠르고 결정적이지만
뜻을 모르므로 모두 status=candidate(후보) 다. 사람 검수 표(export 의 CSV)에서 확정한 것만 reviewed 로 올린다.
명세가 근거 조항으로 정한 것(R3 = 제32조·제44조, 7.2)은 status=spec.

- 개념 엣지: 조 제목에 나오면 3점, 본문 횟수만큼 1점. 제목에 있거나 2점 이상이면 엣지(HAS_HAZARD·INVOLVES_EQUIPMENT·REQUIRES_CONTROL).
- 방호조치 → 위험요인: 사전의 짝(MITIGATES, method=lexicon, status=candidate).
- 규칙 태그: 낱말 가중치 합(제목에 나오면 두 배)이 rule_threshold 이상이면 TAGGED_RULE 후보.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from simai_ai.rag.chunk import strip_amendment_tags
from simai_ai.rag.graph.build import Edge, Graph, Node

LEXICON = Path(__file__).resolve().parents[1] / "data" / "graph" / "lexicon.json"
_CONCEPT_EDGE = {"HAZARD": "HAS_HAZARD", "EQUIPMENT": "INVOLVES_EQUIPMENT", "CONTROL": "REQUIRES_CONTROL"}


@cache
def lexicon() -> dict[str, Any]:
    return json.loads(LEXICON.read_text(encoding="utf-8"))


def concept_id(cid: str) -> str:
    return f"개념 {cid}"


def rule_id(rule: str) -> str:
    return f"위험규칙 {rule}"


def _body(n: Node) -> str:
    return strip_amendment_tags(n.text)


def _count(text: str, terms: list[str]) -> dict[str, int]:
    """긴 낱말부터 세고 센 자리는 지워 짧은 낱말이 같은 자리를 다시 세지 않게 한다(안전대 부착설비 ⊃ 안전대)."""
    out: dict[str, int] = {}
    for t in sorted(terms, key=len, reverse=True):
        c = text.count(t)
        if c:
            out[t] = c
            text = text.replace(t, " ")
    return out


def score_rule(title: str, body: str, terms: dict[str, int]) -> tuple[int, dict[str, int]]:
    hits = _count(body, list(terms))
    title_hits = _count(title, list(terms))
    score = sum(terms[t] for t in hits) + sum(terms[t] for t in title_hits)
    return score, hits


def enrich(g: Graph) -> None:
    lex = lexicon()
    for c in lex["concepts"]:
        g.add_node(Node(concept_id(c["id"]), "CONCEPT", None, None, None, c["name"], " ".join(c["terms"]), None, 0,
                        {"concept_type": c["type"], "terms": c["terms"]}))
    for r in lex["risk_rules"]:
        g.add_node(Node(rule_id(r["rule"]), "RISK_RULE", None, None, None, r["name"], r["meaning"], None, 0, {"rule": r["rule"]}))
    for ctrl, hz in lex["mitigates"]:
        g.add_edge(Edge(concept_id(ctrl), concept_id(hz), "MITIGATES", "lexicon", "candidate"))
    articles = [n for n in g.nodes.values() if n.node_type == "ARTICLE" and not n.attrs.get("deleted")]
    for n in articles:
        title, body = n.title or "", _body(n)
        for c in lex["concepts"]:
            hits = _count(body, c["terms"])
            in_title = bool(_count(title, c["terms"]))
            score = 3 * in_title + sum(hits.values())
            if in_title or score >= 2:
                g.add_edge(Edge(n.node_id, concept_id(c["id"]), _CONCEPT_EDGE[c["type"]], "keyword", "candidate", None,
                                {"score": score, "matched": sorted(hits)}))
        for r in lex["risk_rules"]:
            score, hits = score_rule(title, body, r["terms"])
            if score >= lex["rule_threshold"]:
                g.add_edge(Edge(n.node_id, rule_id(r["rule"]), "TAGGED_RULE", "keyword", "candidate", None,
                                {"score": score, "matched": sorted(hits)}))
    for s in lex["spec_basis"]:
        if s["node_id"] in g.nodes:
            g.add_edge(Edge(s["node_id"], rule_id(s["rule"]), "TAGGED_RULE", "spec", "spec", s["source"], {}))
