"""조항·질문 템플릿·임베딩 → EC2 `rag_clause`·`rag_query_template` 시드 (Flyway 반복 마이그레이션).

임베딩 계산(embed, 모델 필요)과 시드 생성(sql, 표준 라이브러리만)을 나눈다. 임베딩은 `data/embeddings.json` 에 float32 그대로
커밋하므로 시드는 다시 만들어도 비트 단위로 같다. `ai/` 에서:

    uv run --env-file ../.env python -m simai_ai.rag.seed fetch            # Open API → data/clauses.json
    uv run --env-file ../.env python -m simai_ai.rag.seed check-current    # 현행 판이 고정 MST 와 같은지
    uv run --extra rag python -m simai_ai.rag.seed embed                   # → data/embeddings.json (CPU, 약 1분)
    uv run python -m simai_ai.rag.seed sql --write ../server/src/main/resources/db/migration/R__rag_clauses.sql

`sql --check` 는 시드 파일이 최신인지만 본다(tests/rag/test_seed.py 가 같은 검사를 한다).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any

from simai_ai.rag import law
from simai_ai.rag.chunk import Chunk, chunks
from simai_ai.rag.embed import DIM, MODEL, REVISION, SETTINGS

DATA = Path(__file__).with_name("data")
CLAUSES = DATA / "clauses.json"
TEMPLATES = DATA / "query_templates.json"
EMBEDDINGS = DATA / "embeddings.json"
TEMPLATE_KEY_RE = re.compile(r"^(R[1-5]|TBM)\.[A-Z_]+$")


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, doc: Any) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def load_chunks() -> list[Chunk]:
    return chunks(_load(CLAUSES))


def load_templates() -> list[dict[str, str]]:
    return _load(TEMPLATES)["templates"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inputs() -> dict[str, dict[str, str]]:
    """임베딩할 문자열. 질문 템플릿은 문장 그대로 넣는다(문서와 같은 모델·설정)."""
    return {
        "clauses": {c.clause_id: c.embedding_input for c in load_chunks()},
        "templates": {t["key"]: t["text"] for t in load_templates()},
    }


def pack(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def unpack(b64: str) -> tuple[float, ...]:
    raw = base64.b64decode(b64)
    return struct.unpack(f"<{len(raw) // 4}f", raw)


def f32_text(x: float) -> str:
    """float32 값을 다시 읽으면 같은 float32 가 되는 가장 짧은 십진 표기(pgvector 는 float32 로 읽는다)."""
    for p in range(1, 10):
        s = f"{x:.{p}g}"
        if struct.unpack("<f", struct.pack("<f", float(s)))[0] == x:
            return s
    raise AssertionError(x)


def embed_all(embedder: Any, runtime: dict[str, Any]) -> dict[str, Any]:
    doc: dict[str, Any] = {"model": MODEL, "revision": REVISION, "settings": SETTINGS, "runtime": runtime}
    for kind, items in inputs().items():
        keys = list(items)
        vectors = embedder.embed([items[k] for k in keys])
        doc[kind] = {k: {"input_sha256": _sha(items[k]), "f32": pack(v)} for k, v in zip(keys, vectors, strict=True)}
    return doc


def vectors() -> dict[str, dict[str, tuple[float, ...]]]:
    """커밋된 임베딩. 조항·템플릿이 바뀌었는데 임베딩을 다시 만들지 않았으면 멈춘다."""
    emb = _load(EMBEDDINGS)
    if (emb["model"], emb["revision"], emb["settings"]) != (MODEL, REVISION, SETTINGS):
        raise ValueError("embeddings.json 의 모델·revision·설정이 코드와 다르다. seed embed 를 다시 돌린다")
    out: dict[str, dict[str, tuple[float, ...]]] = {}
    for kind, items in inputs().items():
        stored = emb[kind]
        stale = sorted(set(items) ^ set(stored)) + [k for k in items if k in stored and stored[k]["input_sha256"] != _sha(items[k])]
        if stale:
            raise ValueError(f"embeddings.json 이 {kind} 와 어긋난다({stale[:5]}…). seed embed 를 다시 돌린다")
        out[kind] = {k: unpack(stored[k]["f32"]) for k in items}
        bad = [k for k, v in out[kind].items() if len(v) != DIM]
        if bad:
            raise ValueError(f"{DIM}차원이 아니다: {bad}")
    return out


def _sql(v: str | int | None) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    return "'" + v.replace("'", "''") + "'"


def _date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _vec(v: tuple[float, ...]) -> str:
    return "'[" + ",".join(f32_text(x) for x in v) + "]'"


def _upsert(table: str, key: str, cols: list[str], stamp: str | None) -> list[str]:
    """시드에 없는 행은 지우고, 바뀐 행만 고친다(같으면 loaded_at 도 그대로)."""
    all_cols = [key, *cols] + ([stamp] if stamp else [])
    sets = [f"{c} = EXCLUDED.{c}" for c in cols + ([stamp] if stamp else [])]
    return [
        f"DELETE FROM {table} t WHERE NOT EXISTS (SELECT 1 FROM {table}_seed s WHERE s.{key} = t.{key});",
        f"INSERT INTO {table} ({', '.join(all_cols)})",
        f"SELECT {', '.join([key, *cols] + (['now()'] if stamp else []))} FROM {table}_seed",
        f"ON CONFLICT ({key}) DO UPDATE SET {', '.join(sets)}",
        f"WHERE ({', '.join(f'{table}.{c}' for c in cols)}) IS DISTINCT FROM ({', '.join(f'EXCLUDED.{c}' for c in cols)});",
    ]


def render(doc: dict[str, Any] | None = None) -> str:
    doc = _load(CLAUSES) if doc is None else doc
    vecs = vectors()
    clause_rows = [
        f"    ({_sql(c.clause_id)}, {_sql(c.law_name)}, {_sql(c.article)}, {_sql(c.title)}, {_sql(c.body)}, "
        f"{_vec(vecs['clauses'][c.clause_id])}, {_sql(MODEL)}, {_sql(REVISION)}, {c.chunk_index}, {_sql(c.source_version)})"
        for c in chunks(doc)
    ]
    template_rows = [
        f"    ({_sql(t['key'])}, {_sql(t['text'])}, {_vec(vecs['templates'][t['key']])}, {_sql(MODEL)}, {_sql(REVISION)})"
        for t in load_templates()
    ]
    clause_cols = ["law_name", "article", "title", "body", "embedding", "embedding_model", "embedding_revision", "chunk_index", "source_version"]
    template_cols = ["text", "embedding", "embedding_model", "embedding_revision"]
    lines = [
        "-- 자동 생성 파일이다. 손으로 고치지 않는다.",
        "-- 만든 곳: ai/src/simai_ai/rag/seed.py (조항 data/clauses.json, 질문 템플릿 data/query_templates.json, 임베딩 data/embeddings.json).",
        "--   ai/ 에서 uv run python -m simai_ai.rag.seed sql --write ../server/src/main/resources/db/migration/R__rag_clauses.sql",
        "-- Flyway 반복 마이그레이션이라 내용이 바뀔 때마다 다시 실행된다. 시드에 없는 행은 지우고 바뀐 행만 고친다(설계: docs/design/rag-clause-index.md).",
        f"-- 법령: {doc['law_name']} MST {doc['mst']} (공포 {_date(doc['promulgation_date'])} 제{int(doc['promulgation_no'])}호, "
        f"시행 {_date(doc['effective_date'])}).",
        f"-- 임베딩: {MODEL} @ {REVISION}, dense {DIM}차원 float32 L2 정규화(문서·질문 같은 revision·설정, 명세 5.4).",
        "CREATE TEMP TABLE rag_clause_seed (",
        "    clause_id text NOT NULL,",
        "    law_name text NOT NULL,",
        "    article text NOT NULL,",
        "    title text,",
        "    body text NOT NULL,",
        f"    embedding vector({DIM}) NOT NULL,",
        "    embedding_model text NOT NULL,",
        "    embedding_revision text NOT NULL,",
        "    chunk_index integer NOT NULL,",
        "    source_version text NOT NULL",
        ") ON COMMIT DROP;",
        "",
        "CREATE TEMP TABLE rag_query_template_seed (",
        "    template_key text NOT NULL,",
        "    text text NOT NULL,",
        f"    embedding vector({DIM}) NOT NULL,",
        "    embedding_model text NOT NULL,",
        "    embedding_revision text NOT NULL",
        ") ON COMMIT DROP;",
        "",
        "INSERT INTO rag_clause_seed (clause_id, " + ", ".join(clause_cols) + ") VALUES",
        ",\n".join(clause_rows) + ";",
        "",
        "INSERT INTO rag_query_template_seed (template_key, " + ", ".join(template_cols) + ") VALUES",
        ",\n".join(template_rows) + ";",
        "",
        *_upsert("rag_clause", "clause_id", clause_cols, "loaded_at"),
        "",
        *_upsert("rag_query_template", "template_key", template_cols, None),
        "",
        "-- 문서와 질문이 모두 한 모델·revision 인지 확인한다(섞이면 검색 점수를 비교할 수 없다)",
        "DO $$",
        "BEGIN",
        "    IF (SELECT count(DISTINCT (embedding_model, embedding_revision)) FROM (",
        "            SELECT embedding_model, embedding_revision FROM rag_clause",
        "            UNION ALL SELECT embedding_model, embedding_revision FROM rag_query_template) r) > 1 THEN",
        "        RAISE EXCEPTION 'rag_clause·rag_query_template 의 임베딩 모델·revision 이 섞였다';",
        "    END IF;",
        "END $$;",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAG 조항 수집·임베딩·DB 시드(Flyway 반복 마이그레이션) 생성")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch", help="Open API 에서 고정 MST 판을 받아 data/clauses.json 을 만든다")
    p_fetch.add_argument("--from-file", type=Path, help="이미 받은 lawService JSON 응답을 쓴다(망 없이)")
    sub.add_parser("check-current", help="현행 판이 고정 MST 와 다르면 종료 코드 1")
    sub.add_parser("embed", help="조항·질문 템플릿을 임베딩해 data/embeddings.json 을 만든다(extra rag)")
    p_sql = sub.add_parser("sql", help="시드 SQL")
    group = p_sql.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", type=Path, help="이 경로에 쓴다")
    group.add_argument("--check", type=Path, help="이 파일이 최신인지 확인한다(다르면 종료 코드 1)")
    args = parser.parse_args(argv)

    if args.cmd == "fetch":
        raw = args.from_file.read_bytes() if args.from_file else law.fetch()
        doc = law.normalize(raw)
        _dump(CLAUSES, doc)
        print(f"{CLAUSES}: 조 {len(doc['articles'])}개, 응답 sha256 {doc['response_sha256']}")
        return 0
    if args.cmd == "check-current":
        current = law.current_mst()
        if current != law.MST:
            print(f"{law.LAW_NAME} 현행 MST {current} 가 고정 MST {law.MST} 와 다르다(개정). 판을 올릴지 정한다", file=sys.stderr)
            return 1
        print(f"현행 MST {current} = 고정 MST")
        return 0
    if args.cmd == "embed":
        from simai_ai.rag.embed import BgeM3, runtime

        _dump(EMBEDDINGS, embed_all(BgeM3(), runtime()))
        return 0
    text = render()
    if args.write:
        args.write.write_text(text, encoding="utf-8", newline="\n")
        return 0
    current_text = args.check.read_text(encoding="utf-8") if args.check.exists() else None
    if current_text != text:
        print(f"{args.check} 가 조항·임베딩과 다르다. seed sql --write 로 다시 만든다", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
