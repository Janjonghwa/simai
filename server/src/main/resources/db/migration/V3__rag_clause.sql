-- simAI V3: RAG 조항 색인 (S15P31A101-27, M3)
-- 설계: docs/design/db-tables.md `rag_clause`·`rag_query_template`, docs/design/rag-clause-index.md. 기획서 3.8·5.4·11장.
-- 적용한 뒤에는 이 파일을 고치지 않는다(Flyway 체크섬). 행은 ai 에서 생성한 반복 마이그레이션 R__rag_clauses.sql 이 채운다.
-- 임베딩은 BAAI/bge-m3 dense 1024차원(float32, L2 정규화, cosine). 문서·질문은 같은 모델 revision 이어야 한다(R__ 가 확인).

-- pgvector. timescale/timescaledb-ha 이미지에 들어 있다. 신뢰 확장이 아니므로 Flyway 계정에 권한이 없으면 DB 관리자가 먼저 만든다
CREATE EXTENSION IF NOT EXISTS vector;

-- H. rag_clause (M3, 일반): 안전 수칙 조항 청크와 임베딩. 조항 원문은 국가법령정보 Open API(고정 MST 판)에서 받는다
CREATE TABLE rag_clause (
    clause_id text NOT NULL PRIMARY KEY,
    law_name text NOT NULL,
    article text NOT NULL,
    title text,
    body text NOT NULL,
    embedding vector(1024) NOT NULL,
    embedding_model text NOT NULL,
    embedding_revision text NOT NULL,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    source_version text NOT NULL,
    loaded_at timestamptz NOT NULL
);

-- H. rag_query_template (M3, 일반): TBM 질문 템플릿과 미리 계산한 질문 임베딩. EC2 는 키로 벡터를 골라 rag_clause 를 검색한다
CREATE TABLE rag_query_template (
    template_key text NOT NULL PRIMARY KEY,
    text text NOT NULL,
    embedding vector(1024) NOT NULL,
    embedding_model text NOT NULL,
    embedding_revision text NOT NULL
);

CREATE INDEX rag_clause_embedding_hnsw_idx ON rag_clause USING hnsw (embedding vector_cosine_ops);

COMMENT ON TABLE rag_clause IS '안전 수칙 조항과 임베딩 (M3)';
COMMENT ON COLUMN rag_clause.clause_id IS '예: 산업안전보건기준규칙 제43조. 청크가 여럿이면 clause_id#chunk_index';
COMMENT ON COLUMN rag_clause.embedding IS 'BAAI/bge-m3 dense, float32, L2 정규화. HNSW 인덱스 vector_cosine_ops';
COMMENT ON COLUMN rag_clause.embedding_model IS 'BAAI/bge-m3';
COMMENT ON COLUMN rag_clause.embedding_revision IS '가중치 revision. 문서·질문이 같은 값이어야 한다(운영 색인 생성 전에 고정)';
COMMENT ON COLUMN rag_clause.chunk_index IS '조항을 나눈 순서(0부터)';
COMMENT ON COLUMN rag_clause.source_version IS '조문 시행일(YYYY-MM-DD)';
COMMENT ON TABLE rag_query_template IS 'TBM 질문 템플릿과 질문 임베딩 (M3)';
COMMENT ON COLUMN rag_query_template.template_key IS '예: R3.ELEVATED (규칙 ID.대상 종류)';
COMMENT ON COLUMN rag_query_template.embedding IS 'rag_clause 와 같은 모델·revision·설정의 질문 임베딩';
