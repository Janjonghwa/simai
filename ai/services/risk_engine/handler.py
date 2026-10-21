"""risk_plan_request 본문 → (HTTP 상태, risk_plan_result 본문). FastAPI 없이 부를 수 있다(시험·Tier 0 이미지 대조).

오류 판정 순서(계약 risk_plan_result 설명, 위반이 여럿이면 먼저 걸린 것 하나):
(1) JSON 객체가 아니거나 payload.request_id 가 UUID 가 아님 → INVALID_REQUEST(request_id null). NaN·Infinity·범위를 넘는 수도 JSON 이 아니다.
(2) payload.source 가 있고 PLANNED 가 아님 → SOURCE_NOT_PLANNED. (3) 그 밖의 스키마 위반 → INVALID_REQUEST.
(4) 규칙 묶음 → UNKNOWN_RULE. (5) 레이아웃 → INVALID_LAYOUT. (6) 그 뒤 예외 → ENGINE_ERROR(retryable 은 이것만 true).
응답도 계약 스키마로 검증하고 보낸다. 맞지 않으면 ENGINE_ERROR 이고, 그것도 계약 문서로 만들 수 없으면 본문 없는 500 이다
(계약 '본문 없는 실패': Spring 이 다시 보낼 수 있는 실패로 본다). 계약 밖 risk_plan_result 를 보내지 않는다.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import math
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from simai_ai.providers.contract import schema_errors as _schema_errors
from simai_ai.risk.clock import SystemClock
from simai_ai.risk.plan import PlanLayoutError, PlanRuleError, evaluate_plan

log = logging.getLogger(__name__)

PLAN_PATH = "/v1/plan/evaluate"
SOURCE_ID = "risk-engine-01"
RESULT_SCHEMA_VERSION = "1.0.0"
MAX_BODY_BYTES = 4 * 1024 * 1024
"""요청 본문 상한. 레이아웃 하나는 수십 KB 다. 앱은 받는 중에 넘으면 나머지를 읽지 않고 INVALID_REQUEST(too_large)"""
MAX_MESSAGE_CHARS = 1000
HTTP_STATUS = {
    "INVALID_REQUEST": 400,
    "SOURCE_NOT_PLANNED": 400,
    "INVALID_LAYOUT": 400,
    "UNKNOWN_RULE": 422,
    "ENGINE_ERROR": 500,
}
_UUID = {"$ref": "https://simai.dev/contracts/common/defs.schema.json#/$defs/uuid"}


def schema_errors(doc: Any, name: str | Mapping[str, Any]) -> list[str]:
    """계약 스키마 검증. 스키마 파일이 없으면(이미지·SIMAI_CONTRACTS_DIR 오류) 입력 오류가 아니라 내부 오류로 올린다."""
    errors = _schema_errors(doc, name)
    if isinstance(name, str) and errors and errors[0].startswith("대응 스키마 없음"):
        raise RuntimeError(errors[0])
    return errors


def engine_version() -> str:
    return importlib.metadata.version("simai-ai")


class _Rejected(Exception):
    def __init__(self, code: str, message: str, request_id: str | None) -> None:
        super().__init__(message)
        self.code, self.message, self.request_id = code, message, request_id


def _reject_constant(name: str) -> float:
    raise ValueError(f"JSON 이 아닌 수 {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"범위를 넘는 수 {text[:40]}")
    return value


def _too_large_message(size: int | None) -> str:
    got = "" if size is None else f"{size} B 이상 "
    return f"본문이 너무 크다({got}> {MAX_BODY_BYTES} B)"


def _read(body: bytes) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """(1): (request_id, 요청 문서, payload)."""
    if len(body) > MAX_BODY_BYTES:
        raise _Rejected("INVALID_REQUEST", _too_large_message(len(body)), None)
    try:
        doc = json.loads(body, parse_constant=_reject_constant, parse_float=_finite_float)
    except RecursionError:  # 깊게 중첩된 배열·객체: 요청이 읽을 수 없는 것이지 내부 오류가 아니다
        raise _Rejected("INVALID_REQUEST", "본문이 JSON 이 아니다: 중첩이 너무 깊다", None) from None
    except (ValueError, UnicodeDecodeError) as e:
        raise _Rejected("INVALID_REQUEST", f"본문이 JSON 이 아니다: {e}", None) from None
    payload = doc.get("payload") if isinstance(doc, dict) else None
    if not isinstance(payload, dict):
        raise _Rejected("INVALID_REQUEST", "본문이 risk_plan_request 1.x 가 아니다: payload 가 객체가 아니다", None)
    if "request_id" not in payload:
        raise _Rejected("INVALID_REQUEST", "본문이 risk_plan_request 1.x 가 아니다: payload.request_id 없음", None)
    request_id = payload["request_id"]
    if schema_errors(request_id, _UUID):
        raise _Rejected("INVALID_REQUEST", "본문이 risk_plan_request 1.x 가 아니다: payload.request_id 가 UUID 가 아니다", None)
    return request_id, doc, payload


def _validate(request_id: str, doc: dict[str, Any], payload: dict[str, Any]) -> None:
    """(2)~(3): 출처, 스키마."""
    if "source" in payload and payload["source"] != "PLANNED":
        raise _Rejected("SOURCE_NOT_PLANNED", f"PLANNED 만 받는다: source={payload['source']!r}"[:200], request_id)
    try:
        errors = schema_errors(doc, "risk_plan_request")
    except RecursionError:
        raise _Rejected("INVALID_REQUEST", "risk_plan_request 스키마 위반: 중첩이 너무 깊다", request_id) from None
    if errors:
        raise _Rejected("INVALID_REQUEST", f"risk_plan_request 스키마 위반 {len(errors)}건: {errors[0]}", request_id)


def _envelope(payload: dict[str, Any], now_ms: Callable[[], int]) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "msg_type": "risk_plan_result",
        "msg_id": str(uuid.uuid4()),
        "sent_at": now_ms(),
        "source_id": SOURCE_ID,
        "payload": payload,
    }


def _error(code: str, message: str, request_id: str | None, now_ms: Callable[[], int]) -> dict[str, Any]:
    return _envelope(
        {
            "request_id": request_id,
            "source": "PLANNED",
            "status": "ERROR",
            "computed_at": now_ms(),
            "engine_version": engine_version(),
            "error": {"code": code, "message": message[:MAX_MESSAGE_CHARS], "retryable": code == "ENGINE_ERROR"},
        },
        now_ms,
    )


def _ok(request_id: str, payload: Mapping[str, Any], now_ms: Callable[[], int]) -> dict[str, Any]:
    outcome = evaluate_plan(payload)
    layout = payload["layout"]
    return _envelope(
        {
            "request_id": request_id,
            "source": "PLANNED",
            "status": "OK",
            "computed_at": now_ms(),
            "engine_version": engine_version(),
            "evaluation_time": payload["evaluation_time"],
            "layout_id": layout["layout_id"],
            "layout_version": layout["layout_version"],
            "rules": payload["rules"],
            "positions": [p.to_json() for p in outcome.positions],
            "items": list(outcome.items),
        },
        now_ms,
    )


def _finish(doc: dict[str, Any], request_id: str | None, now_ms: Callable[[], int]) -> tuple[int, bytes]:
    """계약 검사와 엄격한 직렬화를 거친 응답. 계약 문서로 못 만들면 ENGINE_ERROR, 그것도 안 되면 본문 없는 500."""
    for attempt in (doc, None):
        try:
            if attempt is None:
                attempt = _error("ENGINE_ERROR", "계산 결과가 계약에 맞지 않는다", request_id, now_ms)
            errors = schema_errors(attempt, "risk_plan_result")
            if errors:
                raise ValueError(f"응답이 risk_plan_result 계약에 맞지 않는다: {errors[:3]}")
            content = json.dumps(attempt, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except Exception:  # noqa: BLE001 - 계약 밖 문서(NaN metrics, request_id 없는 ENGINE_ERROR 등)는 보내지 않는다
            log.exception("PLANNED 결과를 보낼 수 없다 request_id=%s", request_id)
            continue
        return HTTP_STATUS.get(attempt["payload"].get("error", {}).get("code"), 200), content
    return 500, b""


def too_large(size: int | None, now_ms: Callable[[], int] | None = None) -> tuple[int, bytes]:
    """받는 중에 상한을 넘은 본문(앱이 나머지를 읽지 않고 부른다) → INVALID_REQUEST(request_id null)."""
    now_ms = SystemClock().now_ms if now_ms is None else now_ms
    return _finish(_error("INVALID_REQUEST", _too_large_message(size), None, now_ms), None, now_ms)


def handle(body: bytes, now_ms: Callable[[], int] | None = None) -> tuple[int, bytes]:
    """요청 본문 → (HTTP 상태, 응답 바이트). 예외를 밖으로 내지 않는다. 본문이 비면 계약 '본문 없는 실패'(500)다."""
    now_ms = SystemClock().now_ms if now_ms is None else now_ms
    request_id: str | None = None
    try:
        request_id, request, payload = _read(body)
        _validate(request_id, request, payload)
        doc = _ok(request_id, payload, now_ms)
    except _Rejected as e:
        doc = _error(e.code, e.message, e.request_id, now_ms)
    except PlanRuleError as e:
        doc = _error("UNKNOWN_RULE", str(e), request_id, now_ms)
    except PlanLayoutError as e:
        doc = _error("INVALID_LAYOUT", str(e), request_id, now_ms)
    except Exception as e:  # noqa: BLE001 - 입력 검사를 통과한 뒤의 예외는 모두 ENGINE_ERROR(내부 경로·값은 로그에만)
        log.exception("PLANNED 판정 중 내부 오류 request_id=%s", request_id)
        doc = _error("ENGINE_ERROR", f"계산 중 내부 오류({type(e).__name__})", request_id, now_ms)
    return _finish(doc, request_id, now_ms)
