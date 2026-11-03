"""FastAPI 앱: POST /v1/plan/evaluate(risk_plan_request → risk_plan_result), GET /healthz (명세 3.8, 12.2).

본문은 Pydantic 모델로 받지 않는다. 계약 스키마(contracts/)로 handler 가 검증하고 오류 판정 순서·HTTP 상태를 정한다
(FastAPI 자동 422 는 계약의 오류 순서와 다르다, 손으로 쓴 모델은 명세 21.2 '스키마에서 생성'에 어긋난다).
문서 경로(/docs, /openapi.json)는 끈다. 판정은 스레드 풀에서 돌려 이벤트 루프를 막지 않는다.
본문은 받는 중에 상한(MAX_BODY_BYTES)을 센다. Content-Length 가 상한을 넘으면 읽기 전에, 헤더가 없거나 거짓이어도 실제로 받은
바이트가 넘는 순간 나머지를 읽지 않고 거절한다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

from simai_ai.risk.plan import POSITION_RULE
from simai_ai.risk.rule_catalog import catalog

from .handler import (
    MAX_BODY_BYTES,
    PLAN_PATH,
    engine_version,
    handle,
    schema_errors,
    too_large,
)


def health() -> dict[str, Any]:
    """엔진 버전과 규칙 등록부(등록된 (ID, 버전)과 설정값 지문). Tier 0 와 같은 등록부인지 지문으로 대조한다."""
    entries = [dataclasses.asdict(e) for e in catalog()]
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {
        "status": "ok",
        "engine_version": engine_version(),
        "registry_digest": f"sha256:{digest}",
        "rules": [{"rule_id": e["rule_id"], "rule_version": e["rule_version"]} for e in entries],
        "position_rule": POSITION_RULE,
    }


def create_app(now_ms: Callable[[], int] | None = None) -> FastAPI:
    # 계약 스키마를 기동 때 읽는다(없으면 요청을 받기 전에 RuntimeError 로 실패)
    schema_errors({}, "risk_plan_request")
    schema_errors({}, "risk_plan_result")
    status = health()
    app = FastAPI(title="simai Risk Engine", docs_url=None, redoc_url=None, openapi_url=None)

    def respond(code: int, content: bytes) -> Response:
        # 빈 본문은 계약 '본문 없는 실패'(JSON 아님)
        return Response(content=content, status_code=code, media_type="application/json" if content else "text/plain")

    @app.post(PLAN_PATH)
    async def evaluate(request: Request) -> Response:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return respond(*too_large(int(declared), now_ms))
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return respond(*too_large(size, now_ms))
            chunks.append(chunk)
        code, content = await run_in_threadpool(handle, b"".join(chunks), now_ms)
        return respond(code, content)

    @app.post("/v1/report/generate")
    async def report(request: Request):
        # 보고서 의존성은 해당 경로에서만 불러온다.
        import os

        from fastapi import HTTPException

        from simai_ai.report.llm import LlmSettings, make_client
        from simai_ai.report.tbm_graph import generate

        from .graph_store import PgGraphStore

        if int(request.headers.get("content-length", "0") or 0) > MAX_BODY_BYTES:
            raise HTTPException(413)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400) from None
        if not isinstance(data, dict) or data.get("report_kind") != "TBM":
            raise HTTPException(400)
        return await run_in_threadpool(generate, data, PgGraphStore(os.environ.get("SIMAI_RAG_PG_DSN", "")),
                                       lambda: make_client(LlmSettings.from_env(os.environ)))

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return status

    @app.post("/v1/report/wrap-up")
    async def wrap_up(request: Request):
        import os

        from fastapi import HTTPException

        from simai_ai.report.llm import LlmSettings, make_client
        from simai_ai.report.wrap_up import generate, validate_request

        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise HTTPException(413)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise HTTPException(413)
            chunks.append(chunk)
        try:
            data = validate_request(json.loads(b"".join(chunks)))
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400) from None
        return await run_in_threadpool(generate, data, lambda: make_client(LlmSettings.from_env(os.environ)))

    return app
