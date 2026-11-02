"""PostgreSQL 읽기 전용 법령 그래프 검색."""
from collections import Counter
from itertools import zip_longest

COLUMNS = "node_id, node_kind, layer, parent_id, source_name, article_ref, title, body, rule_tags, embedding IS NOT NULL AS embedded, embedding_model, embedding_revision"
KINDS = ["REFERENCES", "MUTATIS_MUTANDIS", "NOTWITHSTANDING", "DELEGATES", "PARENT_CHILD", "ANNEX_RELATES", "MITIGATES", "REQUIRES_CONTROL"]


def _round_robin(groups, limit=8):
    selected = {}
    titles = Counter()
    for row in zip_longest(*groups):
        for node in row:
            if node is None or node["node_id"] in selected:
                continue
            title = "".join((node.get("title") or "").split())
            key = (node.get("source_name"), title)
            if title and titles[key] >= 2:
                continue
            selected[node["node_id"]] = node
            if title:
                titles[key] += 1
            if len(selected) == limit:
                return list(selected.values())
    return list(selected.values())

class PgGraphStore:
    def __init__(self, dsn):
        self.dsn = dsn

    def _query(self, sql, params=()):
        import psycopg  # 선택 의존성(extra risk_api): 컨테이너에만 있고 기본 설치(nightly CI)에는 없다
        from psycopg.rows import dict_row

        with psycopg.connect(self.dsn, row_factory=dict_row, options="-c default_transaction_read_only=on") as conn:
            return conn.execute(sql, params).fetchall()

    def corpus_empty(self):
        return not self._query("SELECT 1 FROM rag_node LIMIT 1")

    def seeds(self, basis, rules, keywords, zones):
        basis = [x for x in basis if isinstance(x, str)]
        a = self._query(f"SELECT {COLUMNS} FROM rag_node WHERE node_id = ANY(%s) ORDER BY node_id LIMIT 8", (basis,))
        c = self._query(f"SELECT {COLUMNS} FROM rag_node WHERE (title || ' ' || body) %% ANY(%s) ORDER BY (SELECT max(similarity(title || ' ' || body, k)) FROM unnest(%s::text[]) k) DESC, node_id LIMIT 8", (keywords, keywords)) if keywords else []
        semantic, missing = [], []
        templates = {rule: [] for rule in rules}
        for rule in rules:
            for zone in zones or ["GENERAL"]:
                rows = self._query("SELECT template_key, embedding_model, embedding_revision FROM rag_query_template WHERE template_key = ANY(%s) ORDER BY CASE WHEN template_key = %s THEN 0 ELSE 1 END LIMIT 1", ([f"{rule}.{zone}", f"{rule}.GENERAL"], f"{rule}.{zone}"))
                if not rows:
                    missing.append(f"{rule}.{zone}")
                    continue
                t = rows[0]
                templates[rule].append(t["template_key"])
                found = self._query(f"SELECT {COLUMNS}, embedding <=> (SELECT embedding FROM rag_query_template WHERE template_key = %s) AS distance FROM rag_node WHERE embedding IS NOT NULL AND embedding_model = %s AND embedding_revision = %s ORDER BY distance, node_id LIMIT 32", (t["template_key"], t["embedding_model"], t["embedding_revision"]))
                semantic.append(found)
        tagged = []
        for rule in rules:
            tagged.append(self._query(f"""SELECT * FROM (SELECT {COLUMNS},
            (SELECT min(rag_node.embedding <=> t.embedding) FROM rag_query_template t
             WHERE t.template_key = ANY(%s) AND t.rule = %s
               AND t.embedding_model = rag_node.embedding_model AND t.embedding_revision = rag_node.embedding_revision) AS distance,
            (SELECT max(similarity(rag_node.title || ' ' || rag_node.body, k)) FROM unnest(%s::text[]) k) AS keyword_score
            FROM rag_node WHERE %s = ANY(rule_tags)) ranked
            ORDER BY distance ASC NULLS LAST, CASE WHEN distance IS NULL THEN keyword_score END DESC NULLS LAST,
                     (layer = 'OSH_STANDARD') DESC, node_id LIMIT 32""", (templates[rule], rule, keywords, rule)))
        return [a, _round_robin(tagged), c, _round_robin(semantic)], missing

    def expand(self, ids):
        edges = self._query("SELECT edge_id, src_node_id, dst_node_id, edge_kind, relation_label FROM rag_edge WHERE (src_node_id = ANY(%s) OR dst_node_id = ANY(%s)) AND edge_kind = ANY(%s) AND review_status = ANY(%s) ORDER BY edge_id", (ids, ids, KINDS, ["auto", "spec", "reviewed"]))
        counts = Counter()
        seeds = set(ids)
        for e in edges:
            if e["src_node_id"] in seeds and e["dst_node_id"] not in seeds:
                counts[e["dst_node_id"]] += 1
            if e["dst_node_id"] in seeds and e["src_node_id"] not in seeds:
                counts[e["src_node_id"]] += 1
        neighbors = sorted(counts, key=lambda node_id: (-counts[node_id], node_id))[:24]
        found = self._query(f"SELECT {COLUMNS} FROM rag_node WHERE node_id = ANY(%s)", (neighbors,))
        by_id = {node["node_id"]: node for node in found}
        nodes = [by_id[node_id] for node_id in neighbors if node_id in by_id]
        return nodes, edges
