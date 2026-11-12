import json
import os

import pytest
from simai_ai.report.llm import ChatResult, LlmUnavailable
from simai_ai.report.tbm_graph import generate

NODE = {"node_id": "법령 제43조", "node_kind": "ARTICLE", "layer": "RULE", "source_name": "법령", "article_ref": "제43조", "title": "제목", "body": "본문", "rule_tags": ["R1"], "embedded": True, "embedding_model": "model", "embedding_revision": "rev"}
REQUEST = {"report_kind": "TBM", "risk_items": [{"rule_id": "R1", "severity": "HIGH", "name": "비밀인물"}], "zones": [{"zone_kind": "OPENING", "place": "비밀장소"}], "work_type": None}

def draft(ref="법령 제43조", body="제43조 준수"):
    return json.dumps({"sections": [{"key": k, "title": k, "body": body, "citations": [ref]} for k in ("hazards", "measures", "focus", "legal")]}, ensure_ascii=False)

class Store:
    def __init__(self, nodes=True, empty=False): self.nodes, self.empty = nodes, empty
    def corpus_empty(self): return self.empty
    def seeds(self, *args): return ([[NODE] if self.nodes else [], [], [], []], ["R1.OPENING"])
    def expand(self, ids): return [], []

class Client:
    model = "fake"
    def __init__(self, responses): self.responses, self.sent = iter(responses), []
    def complete(self, msgs):
        self.sent.append(msgs)
        value = next(self.responses)
        if isinstance(value, Exception): raise value
        return ChatResult(content=value)

def run(responses, store=None):
    client = Client(responses)
    return generate(REQUEST, store or Store(), lambda: client), client

def test_success_and_privacy():
    out, client = run([draft()])
    assert out["status"] == "SUCCEEDED" and out["subgraph"]["nodes"][0]["cited"]
    assert "비밀인물" not in str(client.sent) and "비밀장소" not in str(client.sent)
    assert "템플릿 없음" in out["steps"][0]["detail"]

def test_retry_success():
    out, _ = run([draft("invented"), draft()])
    assert out["status"] == "SUCCEEDED" and out["llm"]["calls"] == 2
    assert [x["step"] for x in out["steps"]][-3:] == ["CODE_VERIFY", "LLM_RETRY", "CODE_VERIFY"]

def test_retry_failure_has_no_fabricated_citations():
    out, _ = run([draft(body="제99조"), draft("invented")])
    assert out["failure_reason"] == "CITATION_VIOLATION" and not out["sections"] and not out["citations"]
    assert not out["subgraph"]["nodes"][0]["cited"]

def test_unavailable_and_no_candidates():
    assert run([LlmUnavailable("down")])[0]["failure_reason"] == "LLM_UNAVAILABLE"
    assert run([], Store(nodes=False))[0]["failure_reason"] == "NO_CANDIDATES"
    assert run([], Store(empty=True))[0]["failure_reason"] == "CORPUS_EMPTY"

@pytest.mark.skipif(not os.getenv("SIMAI_TEST_RAG_PG_DSN"), reason="시험 DB 설정 없음")
def test_postgres_seeded_graph():
    from services.risk_engine.graph_store import PgGraphStore
    from simai_ai.report.retrieval import retrieve
    store = PgGraphStore(os.environ["SIMAI_TEST_RAG_PG_DSN"])
    assert not store.corpus_empty()
    request = dict(REQUEST, risk_items=[{"rule_id": "R1"}, {"rule_id": "R2"}],
                   work_type={"code": "OPENING_WORK", "label": "개구부"},
                   legal_basis=["산업안전보건기준규칙 제43조"])
    nodes, graph, _, _ = retrieve(store, request)
    ids = {n["node_id"] for n in nodes}
    # 제42조는 긴 조 임베딩이 희석돼 R2 태그 조문 중 의미 거리 7위라 기준에서 뺐다(MR 남은 일)
    assert {f"산업안전보건기준규칙 제{i}조" for i in (13, 43)} <= ids
    assert any(n["hop"] == 1 for n in graph["nodes"])
    assert all("굴착공사" not in n["source_name"] for n in nodes)


def test_expansion_and_prefilter():
    from simai_ai.report.retrieval import retrieve
    other = dict(NODE, node_id="법령 제44조", article_ref="제44조")
    excluded = dict(NODE, node_id="CONCEPT:X", node_kind="CONCEPT")
    class Graph(Store):
        def expand(self, ids):
            return [other, excluded], [{"edge_id": "e", "src_node_id": NODE["node_id"], "dst_node_id": other["node_id"], "edge_kind": "REFERENCES", "relation_label": None}]
    nodes, graph, _, _ = retrieve(Graph(), REQUEST)
    assert [n["node_id"] for n in nodes] == [NODE["node_id"], other["node_id"]]
    assert [n["hop"] for n in graph["nodes"]] == [0, 1] and len(graph["edges"]) == 1


def test_seed_interleaving_and_neighbor_ranking_and_article_merge():
    from simai_ai.report.retrieval import retrieve

    def node(name, **kwargs):
        return dict(NODE, node_id=name, article_ref=name, title=name, **kwargs)

    article = node("article")
    paragraph = node("paragraph", node_kind="PARAGRAPH", parent_id="article")
    groups = [[article, paragraph], [node(f"b{i}") for i in range(8)],
              [node(f"c{i}") for i in range(8)], [node(f"d{i}") for i in range(8)]]
    neighbors = [node("low"), node("high"), node("standard", layer="OSH_STANDARD")]
    class Graph(Store):
        def seeds(self, *args): return groups, []
        def expand(self, ids):
            self.ids = ids
            return neighbors, [
                {"edge_id": str(i), "src_node_id": src, "dst_node_id": dst, "edge_kind": "REFERENCES"}
                for i, (src, dst) in enumerate([(ids[0], "low"), (ids[0], "high"), (ids[1], "high"), (ids[0], "standard")])
            ]
    store = Graph()
    allowed, graph, _, _ = retrieve(store, REQUEST)
    assert store.ids == ["article", "paragraph", "d0", "c0", "b0", "d1", "c1", "b1", "d2", "c2", "b2", "d3"]
    ids = [n["node_id"] for n in allowed]
    assert "paragraph" not in ids and "article" in ids
    assert ids[-3:] == ["high", "standard", "low"]
    assert any(n["hop"] == 1 for n in graph["nodes"])


def test_duplicate_seeds_do_not_consume_slots():
    from simai_ai.report.retrieval import retrieve
    class Graph(Store):
        def seeds(self, *args):
            return [[NODE], [NODE, dict(NODE, node_id="b", title="b")], [NODE, dict(NODE, node_id="c", title="c")], [NODE, dict(NODE, node_id="d", title="d")]], []
        def expand(self, ids):
            self.ids = ids
            return [], []
    store = Graph()
    retrieve(store, REQUEST)
    assert store.ids == [NODE["node_id"], "d", "c", "b"]


def test_postgres_expansion_limits_by_edge_count():
    from services.risk_engine.graph_store import PgGraphStore
    class Graph(PgGraphStore):
        def _query(self, sql, params=()):
            if "FROM rag_edge" in sql:
                return [{"edge_id": str(i), "src_node_id": "seed", "dst_node_id": f"n{i:02}"} for i in range(25)] + [
                    {"edge_id": "extra", "src_node_id": "seed", "dst_node_id": "n24"}]
            return [dict(NODE, node_id=node_id) for node_id in reversed(params[0])]
    nodes, _ = Graph("unused").expand(["seed"])
    assert len(nodes) == 24 and nodes[0]["node_id"] == "n24"
    assert "n23" not in {n["node_id"] for n in nodes}


def test_postgres_seed_paths_interleave_rules_despite_distance_gap():
    from services.risk_engine.graph_store import PgGraphStore

    class Graph(PgGraphStore):
        def _query(self, sql, params=()):
            if "FROM rag_query_template WHERE" in sql and "SELECT template_key" in sql:
                return [{"template_key": params[1], "embedding_model": "m", "embedding_revision": "r"}]
            if " AS distance FROM rag_node" in sql:
                rule = params[0].split(".")[0]
                return [dict(NODE, node_id=f"{rule}-{i}", title=f"{rule}-{i}", distance=(0.01 if rule == "R1" else 0.9) + i / 100) for i in range(8)]
            if "FROM rag_node WHERE %s = ANY(rule_tags)" in sql:
                rule = params[-1]
                return [dict(NODE, node_id=f"tag-{rule}-{i}", title=f"tag-{rule}-{i}") for i in range(8)]
            return []

    groups, missing = Graph("unused").seeds([], ["R1", "R2"], [], ["OPENING"])
    assert not missing
    assert [n["node_id"] for n in groups[3][:4]] == ["R1-0", "R2-0", "R1-1", "R2-1"]
    assert [n["node_id"] for n in groups[1][:4]] == ["tag-R1-0", "tag-R2-0", "tag-R1-1", "tag-R2-1"]


def test_seed_title_diversity_across_paths_and_laws():
    from simai_ai.report.retrieval import retrieve

    def node(name, title, source="법령"):
        return dict(NODE, node_id=name, title=title, source_name=source)

    class Graph(Store):
        def seeds(self, *args):
            return [[node("a", "출입의 금지")], [node("b", "출입의금지"), node("c", "출입의 금지")],
                    [node("d", "출입의 금지", "다른 법령")], [node("e", "다른 제목")]], []
        def expand(self, ids):
            self.ids = ids
            return [], []

    store = Graph()
    retrieve(store, REQUEST)
    assert store.ids == ["a", "e", "d", "b"]


def test_round_robin_skips_repeated_titles_to_fill_slots():
    from services.risk_engine.graph_store import _round_robin

    repeated = [dict(NODE, node_id=str(i), title="출입 의 금지") for i in range(5)]
    distinct = dict(NODE, node_id="distinct", title="안전난간")
    assert [n["node_id"] for n in _round_robin([repeated + [distinct]])] == ["0", "1", "distinct"]


DAILY = {"report_kind": "DAILY", "report_date": "2026-11-03", "rule_version": 3,
         "events": [{"rule_id": "R2", "severity": "CRITICAL", "zone_id": "z-1", "raised_at_ms": 1, "resolved": True,
                     "needs_check": False, "world_ids": ["w-비밀"], "rationale": "비밀메모"}],
         "event_summary": {"event_count": 1, "resolved_count": 1, "severity_counts": {"CRITICAL": 1}, "rule_counts": {"R2": 1}, "note": "비밀"},
         "zones": [{"zone_id": "z-1", "zone_kind": "OPENING", "place": "비밀장소"}]}


def daily_draft(keys=("summary", "events", "measures", "legal"), ref="법령 제43조"):
    return json.dumps({"sections": [{"key": k, "title": k, "body": "제43조 준수", "citations": [ref]} for k in keys]}, ensure_ascii=False)


def test_daily_success_uses_daily_sections_and_drops_private_fields():
    client = Client([daily_draft()])
    out = generate(DAILY, Store(), lambda: client)
    assert out["status"] == "SUCCEEDED" and [s["key"] for s in out["sections"]] == ["summary", "events", "measures", "legal"]
    sent = str(client.sent)
    assert "w-비밀" not in sent and "비밀메모" not in sent and "비밀장소" not in sent and "'note'" not in sent
    facts = json.loads(client.sent[0][1]["content"])
    assert facts["report_kind"] == "DAILY" and facts["report_date"] == "2026-11-03"
    assert facts["events"] == [{"rule_id": "R2", "severity": "CRITICAL", "zone_id": "z-1", "raised_at_ms": 1, "resolved": True, "needs_check": False}]
    assert facts["event_summary"] == {"event_count": 1, "resolved_count": 1, "severity_counts": {"CRITICAL": 1}, "rule_counts": {"R2": 1}}
    assert out["steps"][0]["input_count"] == 1


def test_daily_rejects_tbm_section_keys():
    out = generate(DAILY, Store(), lambda: Client([draft(), draft()]))
    assert out["failure_reason"] == "CITATION_VIOLATION" and not out["sections"]


def test_daily_retrieval_uses_event_rules_and_caps_events():
    from simai_ai.report.retrieval import retrieve
    class Graph(Store):
        def seeds(self, basis, rules, keywords, zones):
            self.rules, self.zones = rules, zones
            return super().seeds()
    store = Graph()
    retrieve(store, DAILY)
    assert store.rules == ["R2"] and store.zones == ["OPENING"]
    many = dict(DAILY, events=[dict(DAILY["events"][0], raised_at_ms=i) for i in range(150)])
    client = Client([daily_draft()])
    generate(many, Store(), lambda: client)
    assert len(json.loads(client.sent[0][1]["content"])["events"]) == 100


def test_daily_passes_truncation_flag_and_tolerates_null_lists():
    from simai_ai.report.retrieval import retrieve
    client = Client([daily_draft()])
    generate(dict(DAILY, event_summary=dict(DAILY["event_summary"], events_truncated=True)), Store(), lambda: client)
    assert json.loads(client.sent[0][1]["content"])["event_summary"]["events_truncated"] is True
    retrieve(Store(), dict(DAILY, events=None, risk_items=None))
    out = generate(dict(DAILY, events=None), Store(), lambda: Client([daily_draft()]))
    assert out["status"] == "SUCCEEDED" and out["steps"][0]["input_count"] == 0


@pytest.mark.parametrize("kind", ["TBM", "DAILY"])
def test_report_prompt_uses_shared_display_names(kind):
    from simai_ai.report.tbm_graph import messages

    prompt = messages({"report_kind": kind}, [NODE])
    assert "R2=추락 위험(가장자리)" in prompt[0]["content"]
    assert "R6=위험구역 안 시야 끊김" in prompt[0]["content"]
    assert "CRITICAL=위험" in prompt[0]["content"]
    assert "HIGH=경고" in prompt[0]["content"]
