"""그래프 산출물: JSONL(정본)·임베딩·DB 적재 SQL·검수 CSV·manifest.

`--out`(저장소 밖, 예: C:/Users/SSAFY/simai-local/legal-graph-416) 아래:
    graph/nodes.jsonl, graph/edges.jsonl, graph/unresolved.jsonl, graph/stats.json
    embeddings/embeddings.jsonl(노드·질의, float32 base64 리틀엔디언 + 입력 sha256), embeddings/meta.json
    sql/legal_graph_schema.sql(제안 DDL), sql/legal_graph_seed.sql(적재)
    manifest.json
`--repo-data`(저장소 docs/data/legal-graph) 에는 manifest.json 과 사람 검수 표(CSV)만 쓴다.

검수 표는 다시 만들어도 사람이 적은 칸(검수결과·검수자·비고)을 키로 이어 붙인다. 규칙 태그 검수결과가 "승인"이면 엣지 status 를
reviewed 로 올리고 "거절"이면 엣지를 뺀다(build 결과에 적용).
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from simai_ai.rag.graph import catalog, kosha
from simai_ai.rag.graph.build import Graph
from simai_ai.rag.graph.enrich import LEXICON
from simai_ai.rag.graph.ir import LawDoc

TEMPLATES = Path(__file__).resolve().parents[1] / "data" / "graph" / "query_templates.json"
RULE_REVIEW = "rule_tag_review.csv"
CONCEPT_REVIEW = "concept_link_review.csv"
G7_DRAFT = "g7_eval_draft.csv"
_HUMAN_COLS = ("검수결과", "검수자", "비고")
DIM = 1024


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_templates() -> list[dict[str, Any]]:
    return json.loads(TEMPLATES.read_text(encoding="utf-8"))["templates"]


# ---------------------------------------------------------------- 검수 표


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, cols: list[str], rows: list[dict[str, Any]], key: tuple[str, ...]) -> None:
    """사람이 적은 칸은 같은 키의 이전 행에서 이어 붙인다. 엑셀이 읽도록 BOM 을 붙인다."""
    old = {tuple(r.get(k, "") for k in key): r for r in _read_csv(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            prev = old.get(tuple(str(r.get(k, "")) for k in key), {})
            w.writerow({c: (prev.get(c, "") if c in _HUMAN_COLS else r.get(c, "")) for c in cols})


def apply_reviews(g: Graph, review_csv: Path, concept_csv: Path | None = None) -> int:
    """규칙 태그·개념 연결 검수 결과를 그래프에 적용한다. 바뀐 엣지 수."""
    n = 0
    for r in _read_csv(review_csv):
        key = (r["노드ID"], f"위험규칙 {r['규칙']}", "TAGGED_RULE")
        e = g.edges.get(key)
        if e is None or e.status == "spec":
            continue
        if r.get("검수결과") == "승인":
            e.status, n = "reviewed", n + 1
        elif r.get("검수결과") == "거절":
            del g.edges[key]
            n += 1
    if concept_csv is not None:
        for r in _read_csv(concept_csv):
            key = (r["노드ID"], f"개념 {r['개념']}", r["엣지"])
            e = g.edges.get(key)
            if e is None:
                continue
            if r.get("검수결과") == "승인":
                e.status, n = "reviewed", n + 1
            elif r.get("검수결과") == "거절":
                del g.edges[key]
                n += 1
    return n


def _excerpt(g: Graph, nid: str, n: int = 160) -> str:
    return " ".join(g.nodes[nid].text.split())[:n]


def rule_review_rows(g: Graph) -> list[dict[str, Any]]:
    status_ko = {"candidate": "후보", "spec": "명세근거", "reviewed": "검수확정"}
    rows = []
    for e in g.edges.values():
        if e.edge_type != "TAGGED_RULE":
            continue
        n, rule = g.nodes[e.src], g.nodes[e.dst]
        rows.append({"규칙": rule.attrs["rule"], "규칙이름": rule.title, "노드ID": n.node_id, "법령": n.law_name, "조문": n.label,
                     "제목": n.title or "", "점수": e.attrs.get("score", ""), "맞은낱말": " ".join(e.attrs.get("matched", [])),
                     "상태": status_ko.get(e.status, e.status), "발췌": _excerpt(g, n.node_id)})
    rows.sort(key=lambda r: (r["규칙"], r["상태"] != "명세근거", -(r["점수"] or 0), r["노드ID"]))
    return rows


def concept_review_rows(g: Graph) -> list[dict[str, Any]]:
    status_ko = {"candidate": "후보", "reviewed": "검수확정"}
    rows = []
    for e in g.edges.values():
        if e.edge_type not in ("HAS_HAZARD", "INVOLVES_EQUIPMENT", "REQUIRES_CONTROL"):
            continue
        n, c = g.nodes[e.src], g.nodes[e.dst]
        rows.append({"개념": c.node_id.removeprefix("개념 "), "개념이름": c.title, "엣지": e.edge_type, "노드ID": n.node_id, "제목": n.title or "",
                     "점수": e.attrs.get("score", ""), "맞은낱말": " ".join(e.attrs.get("matched", [])), "상태": status_ko.get(e.status, e.status)})
    rows.sort(key=lambda r: (r["개념"], -(r["점수"] or 0), r["노드ID"]))
    return rows


def g7_rows(g: Graph) -> list[dict[str, Any]]:
    tagged = {(e.src, g.nodes[e.dst].attrs.get("rule")) for e in g.edges.values() if e.edge_type == "TAGGED_RULE"}
    rows = []
    for t in load_templates():
        for nid in t.get("expected_draft", []):
            node = g.nodes.get(nid)
            art = g.article_of(nid) if node else None
            rows.append({"질의키": t["key"], "규칙": t["rule"], "대상": t["target"], "질의": t["text"], "기대노드ID": nid,
                         "조문제목": (g.nodes[art].title if art else "") or "", "노드있음": "Y" if node else "N",
                         "규칙태그후보": "Y" if (art or nid, t["rule"]) in tagged else "N", "출처": "Owner 초안(조문 제목 기준)"})
    return rows


# ---------------------------------------------------------------- manifest


def _law_entries(docs: list[LawDoc], out: Path) -> list[dict[str, Any]]:
    log_path = out / "fetch_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    by_sha = {x["sha256"]: x for x in log}
    rows = []
    for d in docs:
        f = by_sha.get(d.raw_sha256, {})
        rows.append({"law_key": d.law_key, "law_name": d.law_name, "law_id": d.law_id, "level": catalog.spec(d.law_key).level,
                     "law_kind": d.law_kind, "mst": d.mst, "effective_date": d.effective_date, "promulgation_date": d.promulgation_date,
                     "promulgation_no": d.promulgation_no, "source": d.source, "source_url": d.source_url, "fetched_at": d.fetched_at,
                     "raw_sha256": d.raw_sha256, "raw_bytes": f.get("bytes"), "articles": len(d.articles), "appendices": len(d.appendices)})
    return rows


def write_manifest(out: Path, docs: list[LawDoc], g_stats: dict[str, Any] | None, repo_data: Path | None) -> dict[str, Any]:
    mpath = out / "manifest.json"
    m: dict[str, Any] = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
    m["ticket"] = "S15P31A101-416"
    m["design"] = "docs/design/legal-graph-pipeline.md"
    if docs:
        m["laws"] = _law_entries(docs, out)
    if g_stats is not None:
        m["graph"] = g_stats
    files = {}
    for rel in ("graph/nodes.jsonl", "graph/edges.jsonl", "graph/unresolved.jsonl", "embeddings/embeddings.jsonl",
                "sql/legal_graph_schema.sql", "sql/legal_graph_seed.sql", "crosscheck.json"):
        p = out / rel
        if p.exists():
            files[rel] = {"sha256": _sha_file(p), "bytes": p.stat().st_size}
    m["files"] = files
    m["inputs"] = {"lexicon_sha256": _sha_file(LEXICON), "query_templates_sha256": _sha_file(TEMPLATES)}
    m["kosha"] = kosha.STATUS
    emb_meta = out / "embeddings" / "meta.json"
    if emb_meta.exists():
        m["embedding"] = json.loads(emb_meta.read_text(encoding="utf-8"))
    cc_path = out / "crosscheck.json"
    if cc_path.exists():
        m["crosscheck"] = json.loads(cc_path.read_text(encoding="utf-8"))["summary"]
    text = json.dumps(m, ensure_ascii=False, indent=1) + "\n"
    mpath.write_text(text, encoding="utf-8", newline="\n")
    if repo_data is not None:
        repo_data.mkdir(parents=True, exist_ok=True)
        (repo_data / "manifest.json").write_text(text, encoding="utf-8", newline="\n")
    return m


# ---------------------------------------------------------------- 그래프


def write_graph(g: Graph, docs: list[LawDoc], out: Path, *, repo_data: Path | None = None) -> None:
    review_dir = repo_data or (out / "review")
    applied = apply_reviews(g, review_dir / RULE_REVIEW, review_dir / CONCEPT_REVIEW)
    if applied:
        print(f"검수 결과 {applied}건 적용")
    nodes = sorted((n.to_json() for n in g.nodes.values()), key=lambda r: r["node_id"])
    edges = sorted((e.to_json() for e in g.edges.values()), key=lambda r: (r["src"], r["dst"], r["edge_type"]))
    _jsonl(out / "graph" / "nodes.jsonl", nodes)
    _jsonl(out / "graph" / "edges.jsonl", edges)
    _jsonl(out / "graph" / "unresolved.jsonl", g.unresolved)
    stats = g.stats()
    (out / "graph" / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    _write_csv(review_dir / RULE_REVIEW, ["규칙", "규칙이름", "노드ID", "법령", "조문", "제목", "점수", "맞은낱말", "상태", "발췌", *_HUMAN_COLS],
               rule_review_rows(g), ("규칙", "노드ID"))
    _write_csv(review_dir / CONCEPT_REVIEW, ["개념", "개념이름", "엣지", "노드ID", "제목", "점수", "맞은낱말", "상태", *_HUMAN_COLS],
               concept_review_rows(g), ("개념", "노드ID"))
    _write_csv(review_dir / G7_DRAFT, ["질의키", "규칙", "대상", "질의", "기대노드ID", "조문제목", "노드있음", "규칙태그후보", "출처", *_HUMAN_COLS],
               g7_rows(g), ("질의키", "기대노드ID"))
    write_manifest(out, docs, stats, repo_data)


# ---------------------------------------------------------------- 임베딩


def embedding_inputs(out: Path) -> list[tuple[str, str, str]]:
    """(종류, 키, 입력 문자열). 노드는 nodes.jsonl 의 embedding_input, 질의는 지시문을 붙인 표준 질의."""
    from simai_ai.rag.graph.embed_qwen import query_input

    rows = [("node", n["node_id"], n["embedding_input"]) for n in _read_jsonl(out / "graph" / "nodes.jsonl") if n.get("embedding_input")]
    rows += [("query", t["key"], query_input(t["text"])) for t in load_templates()]
    return rows


def write_embeddings(out: Path, embedder: Any, *, batch_size: int = 8, limit: int | None = None, repo_data: Path | None = None) -> None:
    from simai_ai.rag.graph import embed_qwen
    from simai_ai.rag.seed import pack

    path = out / "embeddings" / "embeddings.jsonl"
    old = {(r["kind"], r["key"]): r for r in _read_jsonl(path)} if path.exists() else {}
    items = embedding_inputs(out)
    if limit:
        items = items[:limit]
    todo = [(k, key, text) for k, key, text in items if old.get((k, key), {}).get("input_sha256") != _sha(text)]
    t0 = time.perf_counter()
    vecs = embedder.embed([t for _, _, t in todo], batch_size=batch_size) if todo else []
    seconds = time.perf_counter() - t0
    fresh = {(k, key): {"kind": k, "key": key, "input_sha256": _sha(text), "f32": pack(v)} for (k, key, text), v in zip(todo, vecs, strict=True)}
    rows = [fresh.get((k, key)) or old[(k, key)] for k, key, _ in items]
    _jsonl(path, rows)
    meta = {"model": embed_qwen.MODEL, "revision": embed_qwen.REVISION, "dim": embed_qwen.DIM, "settings": embed_qwen.SETTINGS,
            "query_instruction": embed_qwen.QUERY_INSTRUCTION, "batch_size": batch_size, "device": getattr(embedder, "device", None),
            "runtime": embed_qwen.runtime(), "weights_sha256": embed_qwen.weights_sha256(),
            "vectors": len(rows), "computed_this_run": len(todo), "seconds_this_run": round(seconds, 1),
            "embeddings_sha256": _sha_file(path)}
    (out / "embeddings" / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: meta[k] for k in ("vectors", "computed_this_run", "seconds_this_run", "device")}, ensure_ascii=False))
    write_manifest(out, [], None, repo_data)


def load_vectors(out: Path) -> dict[tuple[str, str], tuple[float, ...]]:
    from simai_ai.rag.seed import unpack

    return {(r["kind"], r["key"]): unpack(r["f32"]) for r in _read_jsonl(out / "embeddings" / "embeddings.jsonl")}


# ---------------------------------------------------------------- SQL

NODE_KIND = {"APPENDIX": "ANNEX"}
EDGE_KIND = {"CONTAINS": "PARENT_CHILD", "REFERS_TO": "REFERENCES", "APPENDIX_RELATES": "ANNEX_RELATES"}
LAW_LAYER = {"LAW": "STATUTE", "DECREE": "DECREE", "MINISTRY_RULE": "RULE", "ADMIN_RULE": "ADMIN_RULE"}

SCHEMA_SQL = f"""-- 정본: docs/design/db-tables.md (-415)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS rag_source (
    law_key text PRIMARY KEY, law_id text NOT NULL, law_name text NOT NULL,
    layer text NOT NULL, mst text NOT NULL, effective_date date NOT NULL,
    promulgation_no text, source_url text NOT NULL, raw_sha256 text NOT NULL,
    fetched_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_node (
    node_id text PRIMARY KEY, node_kind text NOT NULL, layer text NOT NULL,
    law_key text REFERENCES rag_source (law_key),
    parent_id text REFERENCES rag_node (node_id) DEFERRABLE INITIALLY DEFERRED,
    source_name text NOT NULL, article_ref text, title text NOT NULL,
    chunk_index integer NOT NULL DEFAULT 0, body text NOT NULL,
    rule_tags text[] NOT NULL DEFAULT '{{}}', attrs jsonb NOT NULL DEFAULT '{{}}',
    embedding_input text, embedding vector({DIM}), embedding_model text, embedding_revision text,
    source_version text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_node_kind_idx ON rag_node (node_kind);
CREATE INDEX IF NOT EXISTS rag_node_embedding_hnsw_idx ON rag_node USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS rag_node_rule_tags_idx ON rag_node USING gin (rule_tags);
CREATE INDEX IF NOT EXISTS rag_node_text_trgm_idx ON rag_node USING gin ((title || ' ' || body) gin_trgm_ops);
CREATE TABLE IF NOT EXISTS rag_edge (
    edge_id text PRIMARY KEY,
    src_node_id text NOT NULL REFERENCES rag_node (node_id) DEFERRABLE INITIALLY DEFERRED,
    dst_node_id text NOT NULL REFERENCES rag_node (node_id) DEFERRABLE INITIALLY DEFERRED,
    edge_kind text NOT NULL, method text NOT NULL, review_status text NOT NULL,
    relation_label text, attrs jsonb NOT NULL DEFAULT '{{}}',
    loaded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (src_node_id, dst_node_id, edge_kind)
);
CREATE INDEX IF NOT EXISTS rag_edge_dst_kind_idx ON rag_edge (dst_node_id, edge_kind);
CREATE TABLE IF NOT EXISTS rag_query_template (
    template_key text PRIMARY KEY, rule text NOT NULL, target text NOT NULL,
    text text NOT NULL, query_input text NOT NULL, embedding vector({DIM}) NOT NULL,
    embedding_model text NOT NULL, embedding_revision text NOT NULL
);
"""


def _layer(law: dict[str, Any]) -> str:
    if law["law_key"] in ("SAPA", "SAPA_DECREE"):
        return "SAPA"
    if law["law_key"] == "OSHA_STD":
        return "OSH_STANDARD"
    return LAW_LAYER[law["level"]]


def _article_ref(label: str | None) -> str | None:
    if not label:
        return None
    return re.sub(r"(?<=조)(?=제)|(?<=항)(?=제)|(?<=호)(?=제)|(?<=별표)(?=\d)", " ", label)


def _q(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return "'" + str(v).replace("'", "''") + "'"


def _date(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _vec(v: tuple[float, ...] | None) -> str:
    from simai_ai.rag.seed import f32_text

    return "NULL" if v is None else "'[" + ",".join(f32_text(x) for x in v) + "]'"


def write_sql(out: Path, *, repo_data: Path | None = None) -> None:
    """적재 SQL(한 트랜잭션). 같은 키는 덮어쓰고 시드에 없는 행은 지운다(법령 판을 올리면 통째로 바뀌는 파생 색인이다)."""
    from simai_ai.rag.graph import embed_qwen

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    vecs = load_vectors(out) if (out / "embeddings" / "embeddings.jsonl").exists() else {}
    nodes = _read_jsonl(out / "graph" / "nodes.jsonl")
    edges = _read_jsonl(out / "graph" / "edges.jsonl")
    model, rev = embed_qwen.MODEL, embed_qwen.REVISION
    laws = {law["law_key"]: law for law in manifest["laws"]}
    node_by_id = {n["node_id"]: n for n in nodes}
    tags: dict[str, list[dict[str, str]]] = {}
    for e in edges:
        if e["edge_type"] == "TAGGED_RULE" and e["status"] != "rejected":
            rule = node_by_id[e["dst"]]["attrs"]["rule"]
            tags.setdefault(e["src"], []).append({"rule": rule, "review_status": e["status"]})
    lexicon_version = "sha256:" + _sha_file(LEXICON)
    lines = ["-- 자동 생성(simai_ai.rag.graph sql). 손으로 고치지 않는다.", "BEGIN;", "SET CONSTRAINTS ALL DEFERRED;",
             "DELETE FROM rag_edge;", "DELETE FROM rag_query_template;", "DELETE FROM rag_node;", "DELETE FROM rag_source;"]
    for law in manifest["laws"]:
        lines.append("INSERT INTO rag_source (law_key, law_id, law_name, layer, mst, effective_date, promulgation_no, source_url, raw_sha256, fetched_at) VALUES (" + ", ".join([
            _q(law["law_key"]), _q(law["law_id"]), _q(law["law_name"]), _q(_layer(law)), _q(law["mst"]), _q(_date(law["effective_date"])),
            _q(law["promulgation_no"]), _q(law["source_url"]), _q(law["raw_sha256"]), _q(law["fetched_at"])]) + ");")
    for n in nodes:
        v = vecs.get(("node", n["node_id"]))
        kind = NODE_KIND.get(n["node_type"], n["node_type"])
        law = laws.get(n["law_key"])
        layer = _layer(law) if law else {"EXTERNAL_LAW": "EXTERNAL", "CONCEPT": "CONCEPT", "RISK_RULE": "RISK_RULE"}[kind]
        source_name = n["law_name"] or kind
        ref = _article_ref(n["label"])
        attrs = {**n["attrs"], "ordinal": n["ordinal"], "label": n["label"]}
        node_tags = tags.get(n["node_id"], [])
        if node_tags:
            attrs["rule_tag_reviews"] = node_tags
        version = (f"시행일 {_date(law['effective_date'])} · {law['promulgation_no']} · {law['mst']}" if law
                   else ("출처 버전 미확인" if kind == "EXTERNAL_LAW" else lexicon_version))
        array = "ARRAY[" + ", ".join(_q(x["rule"]) for x in sorted(node_tags, key=lambda x: x["rule"])) + "]::text[]"
        lines.append("INSERT INTO rag_node (node_id, node_kind, layer, law_key, parent_id, source_name, article_ref, title, chunk_index, body, "
                     "rule_tags, attrs, embedding_input, embedding, embedding_model, embedding_revision, source_version) VALUES (" + ", ".join([
                         _q(n["node_id"]), _q(kind), _q(layer), _q(n["law_key"]), _q(n["parent_id"]), _q(source_name), _q(ref),
                         _q(n["title"] or ref or source_name), "0", _q(n["text"]), array, _q(attrs) + "::jsonb", _q(n["embedding_input"]),
                         _vec(v), _q(model if v is not None else None), _q(rev if v is not None else None), _q(version)]) + ");")
    for e in edges:
        kind = EDGE_KIND.get(e["edge_type"], e["edge_type"])
        edge_id = f"{e['src']}--{kind}-->{e['dst']}"
        lines.append("INSERT INTO rag_edge (edge_id, src_node_id, dst_node_id, edge_kind, method, review_status, relation_label, attrs) VALUES (" + ", ".join([
            _q(edge_id), _q(e["src"]), _q(e["dst"]), _q(kind), _q(e["method"]), _q(e["status"]), _q(e["evidence"]), _q(e["attrs"]) + "::jsonb"]) + ");")
    for t in load_templates():
        v = vecs.get(("query", t["key"]))
        if v is None:
            continue
        lines.append("INSERT INTO rag_query_template (template_key, rule, target, text, query_input, embedding, embedding_model, embedding_revision) VALUES (" + ", ".join([
            _q(t["key"]), _q(t["rule"]), _q(t["target"]), _q(t["text"]), _q(embed_qwen.query_input(t["text"])), _vec(v), _q(model), _q(rev)]) + ");")
    lines += ["COMMIT;", ""]
    (out / "sql").mkdir(parents=True, exist_ok=True)
    (out / "sql" / "legal_graph_schema.sql").write_text(SCHEMA_SQL, encoding="utf-8", newline="\n")
    (out / "sql" / "legal_graph_seed.sql").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    write_manifest(out, [], None, repo_data)
