"""그래프 구축(build·enrich)과 내보내기(export·manifest·검수 표) 시험 (S15P31A101-416)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from simai_ai.rag.chunk import CLAUSE_ID_RE
from simai_ai.rag.graph import build, catalog, enrich, export, ir

REPO_DATA = Path(__file__).resolve().parents[4] / "docs" / "data" / "legal-graph"


def _sample_docs() -> list[ir.LawDoc]:
    osha_a38 = ir.ArticleBuilder("제38조(안전조치)", effective="20261027")
    osha_a38.add_line("① 사업주는 추락 위험을 예방하기 위하여 필요한 조치를 하여야 한다.")
    osha_a38.add_line("④ 제1항부터 제3항까지의 규정에 따라 사업주가 하여야 할 조치에 관한 구체적인 사항은 고용노동부령으로 정한다.")

    osha_a39 = ir.ArticleBuilder("제39조(보건조치)", effective="20261027")
    osha_a39.add_line("사업주는 건강장해를 예방하기 위하여 필요한 조치를 하여야 한다.")

    std_a1 = ir.ArticleBuilder("제1조(목적)", effective="20260302")
    std_a1.add_line("이 규칙은 「산업안전보건법」 제38조 및 제39조에서 위임한 사항과 그 시행에 필요한 사항을 규정함을 목적으로 한다.")

    std_a32 = ir.ArticleBuilder("제32조(보호구의 지급 등)", effective="20260302")
    std_a32.add_line("① 사업주는 다음 각 호의 구분에 따라 보호구를 지급하고 착용하도록 하여야 한다. <개정 2024. 6. 28.>")
    std_a32.add_line("1. 물체가 떨어지거나 날아올 위험이 있는 작업: 안전모")
    std_a32.add_line("2. 높이 2미터 이상의 추락할 위험이 있는 장소에서 하는 작업: 안전대")
    std_a32.add_line("② 근로자는 제1항에 따라 지급된 보호구를 착용하여야 한다.")

    std_a43 = ir.ArticleBuilder("제43조(개구부 등의 방호 조치)", effective="20260302")
    std_a43.add_line("① 사업주는 작업발판 및 통로의 끝이나 개구부로서 근로자가 추락할 위험이 있는 장소에는 제13조에 따른 안전난간, 덮개 또는 추락방호망을 설치하여야 한다.")
    std_a43.add_line("② 제1항에도 불구하고 안전난간 설치가 매우 곤란한 경우에는 안전대를 착용하도록 하여야 한다.")

    std_a13 = ir.ArticleBuilder("제13조(안전난간의 구조 및 설치요건)", effective="20260302")
    std_a13.add_line("사업주는 안전난간을 설치하는 경우 다음 각 호의 기준에 맞는 구조로 설치해야 한다.")
    std_a13.add_line("1. 상부 난간대, 중간 난간대, 발끝막이판 및 난간기둥으로 구성할 것")

    std_del = ir.ArticleBuilder("제15조")
    std_del.add_line("삭제 <2020. 1. 16.>")

    osha = ir.LawDoc(
        law_key="OSHA", law_id="001766", law_name=catalog.OSHA, mst="291447", law_kind="법률",
        promulgation_date="20261027", promulgation_no="22122", effective_date="20261027",
        source="api-xml", source_url="https://example.test/osha", raw_sha256="11" * 32, fetched_at="2026-11-02T00:00:00+00:00",
        articles=[osha_a38.finish(), osha_a39.finish()], appendices=[],
    )
    std = ir.LawDoc(
        law_key="OSHA_STD", law_id="007363", law_name=catalog.OSHA_STD, mst="273603", law_kind="고용노동부령",
        promulgation_date="20250901", promulgation_no="00450", effective_date="20260302",
        source="api-xml", source_url="https://example.test/std", raw_sha256="22" * 32, fetched_at="2026-11-02T00:00:00+00:00",
        articles=[std_a1.finish(), std_a13.finish(), std_del.finish(), std_a32.finish(), std_a43.finish()],
        appendices=[ir.Appendix(1, 0, "별표 1 제목(제13조 및 제43조 관련)", "제32조제1항제2호의 규정을 준용한다.")],
    )
    return [osha, std]


def test_build_and_enrich_create_expected_nodes_and_edges():
    docs = _sample_docs()
    g = build.build(docs)
    enrich.enrich(g)

    # 조 단위 ID 는 기존 rag_clause.clause_id 정규식과 호환된다
    std_articles = [n.node_id for n in g.nodes.values() if n.law_key == "OSHA_STD" and n.node_type == "ARTICLE"]
    assert all(CLAUSE_ID_RE.match(nid) for nid in std_articles)

    # 삭제 조문은 임베딩 입력이 없고, 단항 조문은 조만 임베딩하며, 다항 조문은 조+항 모두 임베딩한다
    assert g.nodes["산업안전보건기준규칙 제15조"].embedding_input is None
    assert g.nodes["산업안전보건기준규칙 제13조"].embedding_input is not None
    assert g.nodes["산업안전보건기준규칙 제32조"].embedding_input.startswith("[산업안전보건기준에 관한 규칙 제32조(보호구의 지급 등)] ")
    assert "<개정" not in g.nodes["산업안전보건기준규칙 제32조"].embedding_input
    assert g.nodes["산업안전보건기준규칙 제32조제1항"].embedding_input.startswith("[산업안전보건기준에 관한 규칙 제32조(보호구의 지급 등) 제1항] ")

    # 상하위, 참조, 자기 상위(제43조제2항 -> 제43조제1항은 형제라 유지), 별표 관련, 준용, 목적 조 위임
    assert ("산업안전보건기준규칙", "산업안전보건기준규칙 제43조", "CONTAINS") in g.edges
    assert ("산업안전보건기준규칙 제43조제1항", "산업안전보건기준규칙 제13조", "REFERS_TO") in g.edges
    assert ("산업안전보건기준규칙 제43조제2항", "산업안전보건기준규칙 제43조제1항", "NOTWITHSTANDING") in g.edges
    assert ("산업안전보건기준규칙 별표1", "산업안전보건기준규칙 제13조", "APPENDIX_RELATES") in g.edges
    assert ("산업안전보건기준규칙 별표1", "산업안전보건기준규칙 제32조제1항제2호", "MUTATIS_MUTANDIS") in g.edges
    assert ("산업안전보건법 제38조", "산업안전보건기준규칙", "DELEGATES") in g.edges

    # 개념 연결 및 R3 명세 근거(status=spec) + R2 후보(status=candidate)
    assert ("산업안전보건기준규칙 제43조", "개념 EQUIP.OPENING", "INVOLVES_EQUIPMENT") in g.edges
    assert ("산업안전보건기준규칙 제43조", "개념 CONTROL.GUARDRAIL", "REQUIRES_CONTROL") in g.edges
    assert g.edges[("산업안전보건기준규칙 제32조", "위험규칙 R3", "TAGGED_RULE")].status == "spec"
    assert g.edges[("산업안전보건기준규칙 제43조", "위험규칙 R2", "TAGGED_RULE")].status == "candidate"


class _DummyEmbedder:
    device = "cpu"

    def embed(self, texts, batch_size=8):
        out = []
        for i, _ in enumerate(texts):
            v = [0.0] * export.DIM
            v[i % export.DIM] = 1.0
            out.append(v)
        return out


def test_export_preserves_human_reviews_and_writes_sql_and_manifest(tmp_path: Path):
    pytest.importorskip("torch")
    docs = _sample_docs()
    out = tmp_path / "out"
    repo = tmp_path / "repo"

    g1 = build.build(docs)
    enrich.enrich(g1)
    export.write_graph(g1, docs, out, repo_data=repo)

    # 사람이 규칙 태그 검수 표에 승인·거절을 기입한 뒤 다시 build/write_graph 를 돌리면 반영되고 메모가 보존된다
    rule_csv = repo / export.RULE_REVIEW
    with rule_csv.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
        cols = list(rows[0].keys())
    for r in rows:
        if r["규칙"] == "R2" and r["노드ID"] == "산업안전보건기준규칙 제43조":
            r["검수결과"], r["검수자"], r["비고"] = "승인", "홍길동", "개구부 핵심 조문"
    with rule_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    g2 = build.build(docs)
    enrich.enrich(g2)
    export.write_graph(g2, docs, out, repo_data=repo)
    assert g2.edges[("산업안전보건기준규칙 제43조", "위험규칙 R2", "TAGGED_RULE")].status == "reviewed"

    with rule_csv.open(encoding="utf-8-sig", newline="") as f:
        reloaded = { (r["규칙"], r["노드ID"]): r for r in csv.DictReader(f) }
    assert reloaded[("R2", "산업안전보건기준규칙 제43조")]["검수자"] == "홍길동"

    export.write_embeddings(out, _DummyEmbedder(), repo_data=repo)
    export.write_sql(out, repo_data=repo)

    manifest = json.loads((repo / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) >= {
        "graph/nodes.jsonl", "graph/edges.jsonl", "graph/unresolved.jsonl",
        "embeddings/embeddings.jsonl", "sql/legal_graph_schema.sql", "sql/legal_graph_seed.sql",
    }
    seed_sql = (out / "sql" / "legal_graph_seed.sql").read_text(encoding="utf-8")
    assert "INSERT INTO rag_source (law_key, " in seed_sql
    assert "INSERT INTO rag_node" in seed_sql
    assert "INSERT INTO rag_edge" in seed_sql
    assert "INSERT INTO rag_query_template (template_key, rule, target, text, query_input, " in seed_sql
    schema = (out / "sql" / "legal_graph_schema.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in schema
    assert "UNIQUE (src_node_id, dst_node_id, edge_kind)" in schema
    assert "gin_trgm_ops" in schema and "USING gin (rule_tags)" in schema
    assert "'OSH_STANDARD'" in seed_sql and "'STATUTE'" in seed_sql
    assert "'ANNEX'" in seed_sql and "'PARENT_CHILD'" in seed_sql and "'REFERENCES'" in seed_sql
    assert "'산업안전보건기준규칙 제43조--TAGGED_RULE-->위험규칙 R2'" in seed_sql
    assert '"review_status": "reviewed", "rule": "R2"' in seed_sql
    assert "제32조 제1항 제2호" in seed_sql
    assert "시행일 2026-03-02 · 00450 · 273603" in seed_sql
    assert "sha256:" in seed_sql
    assert export._layer({"law_key": "SAPA_DECREE", "level": "DECREE"}) == "SAPA"


def test_committed_repo_data_files_are_consistent():
    manifest = json.loads((REPO_DATA / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["ticket"] == "S15P31A101-416"
    assert len(manifest["laws"]) == len(catalog.LAWS) == 10
    assert manifest["inputs"]["lexicon_sha256"] == export._sha_file(enrich.LEXICON)
    assert manifest["inputs"]["query_templates_sha256"] == export._sha_file(export.TEMPLATES)
    assert sum(manifest["graph"]["nodes"].values()) == 7963
    assert sum(manifest["graph"]["edges"].values()) == 14265

    for csv_name in (export.RULE_REVIEW, export.CONCEPT_REVIEW, export.G7_DRAFT):
        p = REPO_DATA / csv_name
        assert p.exists() and p.stat().st_size > 0
