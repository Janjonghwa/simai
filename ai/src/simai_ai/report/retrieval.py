"""법령 그래프 후보 검색 경계와 상한."""
from typing import Protocol
from collections import Counter
from itertools import zip_longest

class GraphStore(Protocol):
    def seeds(self, basis, rules, keywords, zones): ...
    def expand(self, ids): ...
    def corpus_empty(self): ...

def retrieve(store: GraphStore, request):
    rules = sorted({i.get("rule_id") for i in request.get("risk_items", []) if isinstance(i, dict) and i.get("rule_id") in {f"R{x}" for x in range(1, 8)}})
    zones = sorted({z.get("zone_kind") for z in request.get("zones", []) if isinstance(z, dict) and isinstance(z.get("zone_kind"), str)})
    work = request.get("work_type") or {}
    keywords = [v for v in (work.get("code"), work.get("label"), *zones) if isinstance(v, str) and v]
    groups, missing = store.seeds(request.get("legal_basis", []), rules, keywords, zones)
    seeds = {}
    titles = Counter()

    def add_seed(node):
        if node is None or node["node_id"] in seeds or len(seeds) >= 12:
            return
        title = "".join((node.get("title") or "").split())
        key = (node.get("source_name"), title)
        if title and titles[key] >= 2:
            return
        seeds[node["node_id"]] = node
        if title:
            titles[key] += 1

    for node in groups[0][:8]:
        add_seed(node)
    for round_nodes in zip_longest(groups[3][:8], groups[2][:8], groups[1][:8]):
        for node in round_nodes:
            add_seed(node)
    expanded, edges = store.expand(list(seeds)) if seeds else ([], [])
    nodes = dict(seeds)
    for node in expanded:
        if node["node_id"] not in nodes and len(nodes) - len(seeds) < 24:
            nodes[node["node_id"]] = node
    counts = Counter()
    for edge in edges:
        src, dst = edge["src_node_id"], edge["dst_node_id"]
        if src in seeds and dst not in seeds:
            counts[dst] += 1
        if dst in seeds and src not in seeds:
            counts[src] += 1
    eligible = [n for n in nodes.values() if n.get("embedded") and n.get("node_kind") in {"ARTICLE", "PARAGRAPH", "ITEM", "SUBITEM", "ANNEX"}]
    article_ids = {n["node_id"] for n in eligible if n["node_kind"] == "ARTICLE"}
    eligible = [n for n in eligible if not (n["node_kind"] == "PARAGRAPH" and n.get("parent_id") in article_ids)]
    seed_nodes = [n for n in eligible if n["node_id"] in seeds][:10]
    neighbors = sorted((n for n in eligible if n["node_id"] not in seeds), key=lambda n: (-counts[n["node_id"]], n.get("layer") != "OSH_STANDARD", n["node_id"]))[:6]
    allowed = seed_nodes + neighbors
    ids = {n["node_id"] for n in allowed}
    graph_nodes = [{k: n.get(k) for k in ("node_id", "node_kind", "layer", "source_name", "article_ref", "title", "rule_tags")} | {"hop": 0 if n["node_id"] in seeds else 1, "cited": False} for n in allowed]
    graph_edges = [{k: e.get(k) for k in ("edge_id", "src_node_id", "dst_node_id", "edge_kind", "relation_label")} for e in edges if e["src_node_id"] in ids and e["dst_node_id"] in ids]
    return allowed, {"nodes": graph_nodes, "edges": graph_edges}, len(nodes), missing
