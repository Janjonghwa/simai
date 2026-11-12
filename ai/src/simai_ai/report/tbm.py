"""TBM 보고서·일일 일지 초안: 프롬프트·요청 조립·보내기 전 검사·인용 검사를 한곳에 둔다 (명세 5.4, 8.3, S15P31A101-225).

입력은 계약 `tbm_report.schema.json#/$defs/input`(처리기가 먼저 검사한다), 결과는 `#/$defs/result` 다.

- 보내는 것은 텍스트뿐이다: 입력에서 필요한 값만 골라 JSON 텍스트로 넣고, 보내기 전에 메시지가 문자열 두 칸(role, content)뿐이고
  객체 주소·URL·data URI 가 없는지 확인한다(`assert_text_only`). 입력 계약이 이미 이미지·사람 필드를 막는다.
- LLM 에는 JSON 객체로 답하게 한다(sections[].paragraphs[].citations). 인용은 구조화 필드로 받고, 본문 글자에서도 조항 표기를
  정규식으로 찾아 둘 다 검사한다(구조 필드를 빼먹고 본문에만 쓴 조항도 잡는다).
- 인용 규칙: 입력 clauses[].clause_id 와 같으면 허용. 조 단위 인용(`…제N조`)은 입력에 그 조(조 단위든 그 조의 청크 `#k` 든)가 있으면
  허용한다. 청크 인용(`…제N조#k`)은 그 청크가 입력에 있어야 한다(입력이 조 단위여도 다른 청크 번호를 지어낼 수 있으므로).
  본문에 쓴 `제N조` 표기는 입력에 있는 조여야 한다. 하나라도 어기면 그 초안을 버린다.
- 초안을 버리면 같은 작업 안에서 한 번만 다시 만든다(사유를 덧붙여). 두 번째도 버리면 `ReportRejected`.
- 위험 등급은 입력 plan.items 를 그대로 결과 risk_items 에 옮긴다. LLM 응답에 정해진 키 밖의 값(severity 등)이 있으면 초안을 버린다.
- 확률·퍼센트 표현을 쓴 초안은 버린다(8.3 등급 + 근거).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from simai_ai.report.llm import ChatClient, ChatResult
from simai_ai.risk.names import REPORT_NAME_GUIDE

PROMPT_VERSION = "tbm-report-1"
MAX_ATTEMPTS = 2  # 첫 초안 + 인용·형식 검사로 버린 뒤 한 번 더
DEFAULT_MAX_CHARS = 6000
SECTION_KINDS = {"TBM": ("SUMMARY", "RISK_ITEM", "RECOMMENDATION"), "DAILY": ("SUMMARY", "EVENT_REVIEW", "RECOMMENDATION")}

_ID_PREFIX = "산업안전보건기준규칙"
# 본문 글자의 조항 표기: '산업안전보건기준규칙 제42조#0', '제 43 조', '제44조의2' 꼴
_ARTICLE_IN_TEXT = re.compile(r"제\s*([1-9][0-9]*)\s*조(?:\s*의\s*([1-9][0-9]*))?(?:\s*#\s*(\d+))?")
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|퍼센트|프로)")
_FORBIDDEN_IN_REQUEST = re.compile(r"s3://|https?://|data:|file://", re.IGNORECASE)

SYSTEM_PROMPT = f"""너는 건설 현장 안전관리자를 돕는 보고서 작성 보조다. 사용자 메시지의 JSON 사실만으로 한국어 {{kind_name}} 초안을 쓴다.
지킬 것:
1. 조항 인용은 clauses 에 있는 clause_id 만 쓴다. 다른 법령·조항 번호를 지어내지 않는다. 본문에 조항 번호를 쓸 때도 clauses 의 조만 쓴다.
2. 위험 등급(severity)은 입력값을 그대로 설명한다. 바꾸거나 새로 매기지 않는다. 확률·퍼센트로 표현하지 않는다.
3. 입력에 없는 사람·이름·장소·사고 사실을 지어내지 않는다.
4. 답은 JSON 객체 하나다: {{{{"sections": [{{{{"kind": 섹션 종류, "title": 제목, "item_index": RISK_ITEM 일 때 위험 항목 번호,
   "paragraphs": [{{{{"text": 문단, "citations": [그 문단이 근거로 삼은 clause_id]}}}}]}}}}]}}}}. 다른 키를 넣지 않는다.
5. 섹션 종류는 {{kinds}} 만 쓴다. 본문 전체는 {{max_chars}}자 이내.
(프롬프트 {PROMPT_VERSION})"""


class ReportRejected(Exception):
    """두 번 만든 초안이 모두 인용·형식 검사를 통과하지 못했다."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class Draft:
    sections: list[dict[str, Any]]
    citations: list[str]


def build_messages(inp: Mapping[str, Any]) -> list[dict[str, str]]:
    """입력 → LLM 메시지(텍스트만). 사실은 필요한 값만 골라 JSON 텍스트로 넣는다."""
    kind = inp["report_kind"]
    max_chars = _max_chars(inp)
    facts: dict[str, Any] = {
        "report_kind": kind,
        "report_date": inp["report_date"],
        "clauses": [{"clause_id": c["clause_id"], "text": c["text"]} for c in inp["clauses"]],
    }
    if kind == "TBM":
        plan = inp["plan"]
        facts["risk_items"] = [
            {
                "item_index": i,
                "rule_id": it["rule_id"],
                "severity": it["severity"],
                "zone_id": it["zone_id"],
                "position_ids": it["position_ids"],
                "metrics": it["metrics"],
                "reason_codes": it["reason_codes"],
                "confidence_reasons": it["confidence_reasons"],
            }
            for i, it in enumerate(plan["items"])
        ]
        facts["positions"] = [
            {k: p[k] for k in ("position_id", "support_surface_id", "position_source") if k in p} for p in plan["positions"]
        ]
        facts["no_evaluated_positions"] = plan["no_evaluated_positions"]
    else:
        facts["events"] = [
            {k: e[k] for k in ("rule_id", "severity", "zone_id", "raised_at", "resolved_at", "resolution", "action_status")}
            for e in inp["events"]
        ]
    system = SYSTEM_PROMPT.format(
        kind_name="TBM 위험성 평가 보고서" if kind == "TBM" else "일일 안전 일지",
        kinds=", ".join(SECTION_KINDS[kind]),
        max_chars=max_chars,
    )
    messages = [
        {"role": "system", "content": system + "\n" + REPORT_NAME_GUIDE},
        {"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True)},
    ]
    assert_text_only(messages)
    return messages


def assert_text_only(messages: Sequence[Mapping[str, Any]]) -> None:
    """보내기 전 검사: 메시지는 role·content 두 문자열뿐이고 객체 주소·URL·data URI 가 없다(5.4 텍스트만). 어기면 ValueError."""
    for i, m in enumerate(messages):
        if not isinstance(m, Mapping) or set(m) != {"role", "content"}:
            raise ValueError(f"메시지 {i}: role·content 두 칸만 보낸다")
        if m["role"] not in ("system", "user", "assistant") or not isinstance(m["content"], str):
            raise ValueError(f"메시지 {i}: 텍스트 메시지가 아니다")
        if _FORBIDDEN_IN_REQUEST.search(m["content"]):
            raise ValueError(f"메시지 {i}: 주소(URL·객체 주소·data URI)를 보내지 않는다")


def check_draft(content: str, inp: Mapping[str, Any]) -> tuple[Draft | None, list[str]]:
    """LLM 응답 텍스트 → (초안, 문제 목록). 문제가 하나라도 있으면 초안은 None 이다."""
    try:
        doc = json.loads(content)
    except ValueError:
        return None, ["JSON 객체가 아니다"]
    if not isinstance(doc, dict) or set(doc) != {"sections"} or not isinstance(doc["sections"], list) or not doc["sections"]:
        return None, ["최상위는 sections(비지 않은 배열) 하나만 둔다"]
    kind = inp["report_kind"]
    allowed = [c["clause_id"] for c in inp["clauses"]]
    allowed_exact = set(allowed)
    allowed_articles = {_article(c) for c in allowed}
    n_items = len(inp["plan"]["items"]) if kind == "TBM" else 0
    problems: list[str] = []
    cited: list[str] = []
    total = 0
    for si, section in enumerate(doc["sections"]):
        where = f"sections[{si}]"
        if not isinstance(section, dict):
            problems.append(f"{where}: 객체가 아니다")
            continue
        extra = set(section) - {"kind", "title", "item_index", "paragraphs"}
        if extra:
            problems.append(f"{where}: 정해지지 않은 키 {sorted(extra)}")
        if section.get("kind") not in SECTION_KINDS[kind]:
            problems.append(f"{where}: 섹션 종류 {section.get('kind')!r} 는 {kind} 에 쓰지 않는다")
        if not isinstance(section.get("title"), str) or not section["title"].strip():
            problems.append(f"{where}: 제목이 없다")
        index = section.get("item_index")
        if section.get("kind") == "RISK_ITEM":
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < n_items:
                problems.append(f"{where}: item_index {index!r} 는 위험 항목 0~{n_items - 1} 이 아니다")
        elif "item_index" in section:
            problems.append(f"{where}: item_index 는 RISK_ITEM 에만")
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list) or not paragraphs:
            problems.append(f"{where}: 문단이 없다")
            continue
        for pi, para in enumerate(paragraphs):
            pw = f"{where}.paragraphs[{pi}]"
            if not isinstance(para, dict) or set(para) != {"text", "citations"}:
                problems.append(f"{pw}: text·citations 두 키만 둔다")
                continue
            text, citations = para["text"], para["citations"]
            if not isinstance(text, str) or not text.strip() or not isinstance(citations, list):
                problems.append(f"{pw}: 문단 형식이 아니다")
                continue
            total += len(text)
            for c in citations:
                if not isinstance(c, str) or not _citation_allowed(c, allowed_exact, allowed_articles):
                    problems.append(f"{pw}: 입력에 없는 조항 인용 {c!r}")
                elif c not in cited:
                    cited.append(c)
            for m in _ARTICLE_IN_TEXT.finditer(text):
                article = f"{_ID_PREFIX} 제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")
                if article not in allowed_articles:
                    problems.append(f"{pw}: 본문에 입력에 없는 조항 {m.group(0)!r}")
            if _PERCENT.search(text):
                problems.append(f"{pw}: 확률·퍼센트 표현을 쓰지 않는다")
            if len(set(citations)) != len(citations):
                problems.append(f"{pw}: 같은 인용이 두 번")
    if total > _max_chars(inp):
        problems.append(f"본문 {total}자가 상한 {_max_chars(inp)}자를 넘는다")
    if problems:
        return None, problems
    return Draft(sections=doc["sections"], citations=cited), []


def generate_report(
    inp: Mapping[str, Any],
    client: ChatClient,
    *,
    now_ms: Callable[[], int],
    cancelled: threading.Event | None = None,
    progress: Callable[[float], Any] | None = None,
) -> dict[str, Any]:
    """입력 → 결과(`tbm_report.schema.json#/$defs/result`). LLM 오류는 LlmError, 두 번 다 버리면 ReportRejected."""
    messages = build_messages(inp)
    results: list[ChatResult] = []
    problems: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("작업이 취소됐다")
        if attempt > 1:
            messages = messages[:2] + [{
                "role": "user",
                "content": "앞 초안은 다음 이유로 쓸 수 없었다. 규칙을 지켜 처음부터 다시 쓴다:\n- " + "\n- ".join(problems[:20]),
            }]
            assert_text_only(messages)
        result = client.complete(messages)
        results.append(result)
        if progress is not None:
            progress(0.4 * attempt)
        draft, problems = check_draft(result.content, inp)
        if draft is not None:
            return _result(inp, draft, results, client.model, attempt, now_ms())
    raise ReportRejected(problems)


def _result(inp, draft: Draft, results: list[ChatResult], requested_model: str, attempts: int, created_at: int):
    risk_items = []
    if inp["report_kind"] == "TBM":
        risk_items = [
            {
                "item_index": i,
                "rule_id": it["rule_id"],
                "rule_version": it["rule_version"],
                "severity": it["severity"],  # 입력값 그대로(LLM 이 바꾸지 못한다)
                "zone_id": it["zone_id"],
                "position_ids": list(it["position_ids"]),
            }
            for i, it in enumerate(inp["plan"]["items"])
        ]
    sections = []
    for s in draft.sections:
        out = {"kind": s["kind"], "title": s["title"].strip(),
               "paragraphs": [{"text": p["text"].strip(), "citations": list(p["citations"])} for p in s["paragraphs"]]}
        if s["kind"] == "RISK_ITEM":
            out["item_index"] = s["item_index"]
        sections.append(out)
    return {
        "report_kind": inp["report_kind"],
        "report_id": inp["report_id"],
        "sections": sections,
        "risk_items": risk_items,
        "citations": draft.citations,
        "generator": {
            "model": results[-1].model or requested_model,
            "prompt_version": PROMPT_VERSION,
            "attempts": attempts,
            "input_tokens": _sum(r.input_tokens for r in results),
            "output_tokens": _sum(r.output_tokens for r in results),
        },
        "created_at": created_at,
    }


def _citation_allowed(c: str, exact: set[str], articles: set[str]) -> bool:
    if c in exact:
        return True
    return "#" not in c and c in articles  # 조 단위 인용은 그 조(또는 그 조의 청크)가 입력에 있으면


def _article(clause_id: str) -> str:
    return clause_id.split("#", 1)[0]


def _max_chars(inp: Mapping[str, Any]) -> int:
    return int((inp.get("options") or {}).get("max_chars", DEFAULT_MAX_CHARS))


def _sum(values) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None

