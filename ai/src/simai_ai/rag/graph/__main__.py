"""법령 그래프 파이프라인 CLI. `ai/` 에서(큰 산출물은 --out 아래, 저장소에는 manifest·검수 표만):

    # 1) 수집: Open API(LAW_GO_KR_OC, 등록 IP 필요). 키가 없으면 --web(국가법령정보센터 본문 HTML, 3초 간격)
    uv run python -m simai_ai.rag.graph fetch --out C:/Users/SSAFY/simai-local/legal-graph-416
    # 2) 그래프: 노드·엣지·검수 표·manifest
    uv run python -m simai_ai.rag.graph build --out C:/Users/SSAFY/simai-local/legal-graph-416
    # 3) 임베딩(extra rag, CPU 가능): 노드·표준 질의 → embeddings.jsonl
    uv run --extra rag python -m simai_ai.rag.graph embed --out C:/Users/SSAFY/simai-local/legal-graph-416
    # 4) DB 적재 SQL(정본 표 rag_source·rag_node·rag_edge·rag_query_template)
    uv run python -m simai_ai.rag.graph sql --out C:/Users/SSAFY/simai-local/legal-graph-416
    # 교차 확인: 법령정보지식베이스 관련법령(lsRlt)과 법령 단위 관계 비교
    uv run python -m simai_ai.rag.graph crosscheck --out C:/Users/SSAFY/simai-local/legal-graph-416
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from simai_ai.rag.graph import catalog
from simai_ai.rag.graph.ir import LawDoc


def _docs_dir(out: Path) -> Path:
    return out / "docs"


def load_docs(out: Path) -> list[LawDoc]:
    docs = []
    for spec in catalog.LAWS:
        p = _docs_dir(out) / f"{spec.key}.json"
        if p.exists():
            docs.append(LawDoc.from_json(json.loads(p.read_text(encoding="utf-8"))))
    return docs


def cmd_fetch(args: argparse.Namespace) -> int:
    out: Path = args.out
    _docs_dir(out).mkdir(parents=True, exist_ok=True)
    keys = args.laws or [s.key for s in catalog.LAWS]
    fetch_log: list[dict] = []
    if args.web:
        from simai_ai.rag.graph import source_web

        throttle = source_web.Throttle(args.interval or 3.0)
        for key in keys:
            spec = catalog.spec(key)
            if spec.level == "ADMIN_RULE":
                print(f"{key}: 행정규칙은 웹 경로를 만들지 않았다(Open API 사용)", file=sys.stderr)
                continue
            ed = source_web.current_edition(spec.name, throttle)
            url, raw = source_web.fetch_body(ed, throttle)
            cache = out / "cache" / "web" / f"{key}_{ed.lsi_seq}_{ed.ef_yd}.html"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(raw)
            doc = source_web.parse(raw, spec, source_url=url)
            _write_doc(out, doc)
            fetch_log.append({"url": url, "path": str(cache), "bytes": len(raw), "sha256": doc.raw_sha256, "fetched_at": doc.fetched_at, "cached": False})
    else:
        from simai_ai.rag.graph import source_api

        client = source_api.Client(out / "cache" / "api", min_interval_s=args.interval or 1.0)
        for key in keys:
            doc = client.fetch(catalog.spec(key))
            _write_doc(out, doc)
        fetch_log = [vars(f) for f in client.log]
    (out / "fetch_log.json").write_text(json.dumps(fetch_log, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def _write_doc(out: Path, doc: LawDoc) -> None:
    p = _docs_dir(out) / f"{doc.law_key}.json"
    p.write_text(json.dumps(doc.to_json(), ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    print(f"{doc.law_key} {doc.law_name}: MST {doc.mst} 시행 {doc.effective_date} 조 {len(doc.articles)} 별표 {len(doc.appendices)} ({doc.source})")


def cmd_build(args: argparse.Namespace) -> int:
    from simai_ai.rag.graph import build, enrich, export

    docs = load_docs(args.out)
    if not docs:
        print("docs/ 가 비었다. fetch 를 먼저 돌린다", file=sys.stderr)
        return 1
    g = build.build(docs)
    enrich.enrich(g)
    export.write_graph(g, docs, args.out, repo_data=args.repo_data)
    print(json.dumps(g.stats(), ensure_ascii=False, indent=1))
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    from simai_ai.rag.graph import embed_qwen, export

    export.write_embeddings(args.out, embed_qwen.Qwen3Embedder(device=args.device), batch_size=args.batch_size, limit=args.limit, repo_data=args.repo_data)
    return 0


def cmd_sql(args: argparse.Namespace) -> int:
    from simai_ai.rag.graph import export

    export.write_sql(args.out, repo_data=args.repo_data)
    return 0


def cmd_crosscheck(args: argparse.Namespace) -> int:
    from simai_ai.rag.graph import crosscheck, export, source_api

    client = source_api.Client(args.out / "cache" / "api", min_interval_s=args.interval or 1.0)
    report = crosscheck.run(client, args.out)
    (args.out / "crosscheck.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    export.write_manifest(args.out, [], None, args.repo_data)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="건설안전 법령 지식 그래프 오프라인 파이프라인(S15P31A101-416)")
    parser.add_argument("--out", type=Path, required=True, help="큰 산출물 폴더(저장소 밖)")
    parser.add_argument("--repo-data", type=Path, default=None, help="manifest·검수 표를 쓸 저장소 폴더(docs/data/legal-graph)")
    parser.add_argument("--interval", type=float, default=None, help="요청 간격 초(API 1, 웹 3)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch")
    p.add_argument("--web", action="store_true", help="Open API 대신 웹 본문 HTML")
    p.add_argument("--laws", nargs="*", help="catalog key(기본: 전부)")
    p = sub.add_parser("build")
    p.add_argument("--repo-data", type=Path, default=argparse.SUPPRESS, help="manifest·검수 표를 쓸 저장소 폴더(docs/data/legal-graph)")
    p = sub.add_parser("embed")
    p.add_argument("--device", default="cpu")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--limit", type=int, default=None, help="시험용: 앞의 N개만")
    p.add_argument("--repo-data", type=Path, default=argparse.SUPPRESS)
    p = sub.add_parser("sql")
    p.add_argument("--repo-data", type=Path, default=argparse.SUPPRESS)
    p = sub.add_parser("crosscheck")
    p.add_argument("--repo-data", type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return {"fetch": cmd_fetch, "build": cmd_build, "embed": cmd_embed, "sql": cmd_sql, "crosscheck": cmd_crosscheck}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
