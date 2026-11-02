-- 정본: docs/design/db-tables.md (-415)
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
    rule_tags text[] NOT NULL DEFAULT '{}', attrs jsonb NOT NULL DEFAULT '{}',
    embedding_input text, embedding vector(1024), embedding_model text, embedding_revision text,
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
    relation_label text, attrs jsonb NOT NULL DEFAULT '{}',
    loaded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (src_node_id, dst_node_id, edge_kind)
);
CREATE INDEX IF NOT EXISTS rag_edge_dst_kind_idx ON rag_edge (dst_node_id, edge_kind);

ALTER TABLE rag_query_template ADD COLUMN rule text, ADD COLUMN target text, ADD COLUMN query_input text;
ALTER TABLE work_unit ADD COLUMN layout_id bigint REFERENCES layout_plan(layout_id), ADD COLUMN work_type text;
CREATE TABLE report (
    report_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_kind text NOT NULL DEFAULT 'TBM' CHECK (report_kind IN ('TBM', 'DAILY')),
    work_id bigint REFERENCES work_unit(work_id),
    layout_id bigint REFERENCES layout_plan(layout_id), layout_revision integer,
    report_version integer NOT NULL CHECK (report_version >= 1),
    status text NOT NULL CHECK (status IN ('GENERATING', 'SUCCEEDED', 'FAILED')),
    failure_reason text, work_type text, rule_version bigint,
    risk_items jsonb NOT NULL DEFAULT '[]', zone_history jsonb NOT NULL DEFAULT '[]',
    sections jsonb NOT NULL DEFAULT '[]', citations jsonb NOT NULL DEFAULT '[]',
    subgraph jsonb NOT NULL DEFAULT '{"nodes":[],"edges":[]}', steps jsonb NOT NULL DEFAULT '[]',
    llm_model text, created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
    UNIQUE (work_id, report_version)
);
CREATE UNIQUE INDEX report_one_generating_per_work ON report(work_id) WHERE status = 'GENERATING';
