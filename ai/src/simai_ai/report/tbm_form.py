"""날짜 작업 TBM 양식(report.form) AI 초안과 종료 미팅 문장 (S15P31A101-616, 계약 d: 양식 칸 JSON).

입력은 계약 `tbm_form_draft_request`(처리기가 스키마로 먼저 검사하고 `check_request` 로 일관성을 본다), 결과는
`tbm_form_draft_result` 다. 종료 미팅은 `tbm_form_ending_request` → `tbm_form_ending_result` 이고 기존 작업 마무리(wrap_up)를 쓴다.

- 외부 LLM 에는 텍스트만 보낸다. 입력 계약이 리더·참석자·계정·이미지 필드를 막고, 여기서도 필요한 값만 골라 JSON 텍스트로 넣은 뒤
  `assert_text_only` 로 확인한다(AGENTS 규칙 8).
- 작업명·장소·위험성평가 실시는 LLM 이 아니라 입력에서 코드가 정한다(지어낼 여지가 없게). LLM 은 위험 3줄·중점 위험·점검 조치·
  일일 미팅 문단만 쓴다.
- 점검 예·아니오(checks[*].yes)는 늘 null 이다. 현장에서 사람이 확인하는 답이고 이 입력에는 그 근거가 없다(AGENTS 규칙 4).
- LLM 응답은 정해진 키·줄 수·점검 순서·글자 수·인용 허용 목록·본문 조문 번호·확률 표현을 검사한다. 걸리면 사유만 덧붙여(거절한
  초안은 다시 보내지 않는다) 한 번 더 만들고, 그래도 걸리거나 LLM 을 쓸 수 없으면 규칙 태그·구역으로 만든 결정적 대체 초안이다.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from simai_ai.report import wrap_up
from simai_ai.report.llm import ChatClient, ChatResult, LlmConfigError, LlmError, LlmTimeout
from simai_ai.report.tbm import _ARTICLE_IN_TEXT, _FORBIDDEN_IN_REQUEST, _PERCENT, assert_text_only
from simai_ai.report.tbm_form_templates import (
    CHECK_ACTIONS,
    CHECK_CODES,
    CHECKS,
    DEFAULT_WHERE,
    GENERAL_RISKS,
    TAG_PRIORITY,
    TAG_THEMES,
    ZONE_KIND_TAGS,
)
from simai_ai.risk.names import REPORT_NAME_GUIDE

PROMPT_VERSION = "tbm-form-1"
MAX_ATTEMPTS = 2  # 첫 초안 + 검사로 버린 뒤 한 번 더
RISK_ROWS = 3
MAX_SHORT = 100  # 작업명·직접 쓴 작업
MAX_PLACE = 200
MAX_TEXT = 500  # 위험·대책·조치
MAX_REVIEW = 2000  # 일일·종료 미팅
MAX_CLAUSE_BODY = 600
OUTPUT_KEYS = {"risks", "focus", "checks", "review_daily", "citations"}
DETERMINISTIC = "deterministic"

SYSTEM_PROMPT = f"""너는 건설 현장 TBM(작업 전 안전 미팅) 일지 양식을 채우는 보조다. 사용자 메시지의 JSON 사실만으로 한국어로 쓴다.
지킬 것:
1. 답은 JSON 객체 하나다: {{"risks": [{{"risk": 위험 요인, "measure": 대책, "rule_tags": [그 줄이 다룬 규칙 태그]}} 정확히 3개],
   "focus": {{"risk": 중점 위험, "measure": 중점 대책}}, "checks": [{{"code": 점검 코드, "action": 조치 제안}} checks 와 같은 순서로 5개],
   "review_daily": 일일 미팅 요약 문단, "citations": [근거로 삼은 clauses 의 node_id]}}. 다른 키를 넣지 않는다.
2. risks 는 rule_tags 의 위험 주제를 먼저 다룬다. 각 줄의 rule_tags 에는 입력 rule_tags 중 그 줄이 다룬 것만 쓰고, 없으면 [] 로 둔다.
   focus 는 세 줄 가운데 가장 중요한 위험이다. 장소는 zones 이름만 쓴다.
3. checks 의 action 은 그 점검 항목에서 오늘 할 조치 제안이다. 점검을 했다·안 했다·이상 없다처럼 확인 결과를 쓰지 않는다(사람이 현장에서 확인한다).
4. 글자 수: risk·measure·action 은 각 {MAX_TEXT}자, review_daily 는 {MAX_REVIEW}자 이내. 빈 칸을 두지 않는다.
5. citations 에는 clauses 의 node_id 만 쓴다. 본문에 조문 번호를 쓸 때도 clauses 의 조만 쓰고, clauses 가 비었으면 조문 번호를 쓰지 않는다.
6. 입력에 없는 법령·수치·사람·이름·사고 사실을 지어내지 않는다. 확률·퍼센트로 표현하지 않는다.
7. existing 에 이미 채운 칸이 있으면 그 내용과 어긋나지 않게 쓴다.
(프롬프트 {PROMPT_VERSION})"""


# ---------------------------------------------------------------------------------------------------------------
# 입력


def check_request(req: Mapping[str, Any]) -> list[str]:
    """스키마 밖 일관성 검사(스키마는 처리기가 먼저 본다). 빈 목록이면 통과."""
    problems: list[str] = []
    coded = [t for t in req["tasks"] if "code" in t]
    union = set().union(*(t["rule_tags"] for t in coded)) if coded else set()
    if union != set(req["rule_tags"]):
        problems.append("rule_tags 는 작업내용 rule_tags 의 합집합이어야 한다")
    codes = [t["code"] for t in coded]
    texts = [t["text"].strip() for t in req["tasks"] if "text" in t]
    if len(set(codes)) != len(codes) or len(set(texts)) != len(texts):
        problems.append("같은 작업내용이 두 번 있다")
    if any(not t for t in texts):
        problems.append("직접 쓴 작업이 비었다")
    if any("\x00" in s or _FORBIDDEN_IN_REQUEST.search(s) for s in _strings(req)):
        problems.append("주소(URL·객체 주소·data URI)나 NUL 을 싣지 않는다")
    return problems


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _task_labels(req: Mapping[str, Any]) -> list[str]:
    return [(t["label"] if "code" in t else t["text"]).strip() for t in req["tasks"]]


def _tags(req: Mapping[str, Any]) -> list[str]:
    """합집합 태그를 위험 우선순위대로."""
    union = set(req["rule_tags"])
    return [t for t in TAG_PRIORITY if t in union]


def task_name(req: Mapping[str, Any]) -> str:
    """작업명: 작업내용 이름을 ', ' 로 잇고, 100자를 넘으면 '첫 이름 외 N건'."""
    labels = _task_labels(req)
    joined = ", ".join(labels)
    if len(joined) <= MAX_SHORT:
        return joined
    head = f" 외 {len(labels) - 1}건"
    return labels[0][: MAX_SHORT - len(head)] + head


def place(req: Mapping[str, Any]) -> str:
    """장소: 세팅 구역 이름(중복 없이 순서대로). 200자를 넘으면 들어가는 데까지 + ' 외 N곳'. 구역이 없으면 ''(모르면 비운다)."""
    names = list(dict.fromkeys(z["name"].strip() for z in req["zones"] if z["name"].strip()))
    if not names:
        return ""
    joined = ", ".join(names)
    if len(joined) <= MAX_PLACE:
        return joined
    for n in range(len(names) - 1, 0, -1):
        text = ", ".join(names[:n]) + f" 외 {len(names) - n}곳"
        if len(text) <= MAX_PLACE:
            return text
    return names[0][: MAX_PLACE - 10] + f" 외 {len(names) - 1}곳"


def _where(tag: str, zones: Sequence[Mapping[str, Any]]) -> str:
    """태그와 관련된 첫 구역 이름 + ' 주변'. 없으면 '작업 구간'. 구역의 rule_id 가 먼저, 그다음 구역 유형."""
    for match in (lambda z: z["rule_id"] == tag, lambda z: tag in ZONE_KIND_TAGS.get(z["zone_kind"], ())):
        for z in zones:
            if z["name"].strip() and match(z):
                return f"{z['name'].strip()} 주변"
    return DEFAULT_WHERE


# ---------------------------------------------------------------------------------------------------------------
# 결정적 대체 초안


def fallback_draft(req: Mapping[str, Any]) -> dict[str, Any]:
    """규칙 태그·구역으로 만든 초안(LLM 없음). 늘 계약 form_draft 를 통과한다(시험이 모든 태그·점검 코드를 본다)."""
    zones = req["zones"]
    tags = _tags(req)
    rows = [
        {"risk": TAG_THEMES[t].risk.format(where=_where(t, zones)), "measure": TAG_THEMES[t].measure.format(where=_where(t, zones))}
        for t in tags[:RISK_ROWS]
    ]
    for risk, measure in GENERAL_RISKS[: RISK_ROWS - len(rows)]:
        rows.append({"risk": risk, "measure": measure})
    checks = []
    for code in CHECK_CODES:
        related = [t for t in tags if TAG_THEMES[t].check == code]
        specific, generic = CHECK_ACTIONS[code]
        action = specific.format(where=_where(related[0], zones)) if related else generic
        checks.append({"code": code, "yes": None, "action": _clip(action, MAX_TEXT)})
    name, where = task_name(req), place(req)
    daily = [f"{req['report_date']} {name} 작업 전 TBM."]
    if where:
        daily.append(f"장소: {where}.")
    daily.append("주요 위험: " + "; ".join(r["risk"] for r in rows) + ".")
    daily.append(f"중점 관리: {rows[0]['risk']}. 대책: {rows[0]['measure']}.")
    daily.append("작업 전 점검 항목은 현장에서 확인한 뒤 표시한다.")
    return {
        "task_name": name,
        "place": where,
        "risk_assessed": True if tags else None,
        "risks": [{k: _clip(v, MAX_TEXT) for k, v in r.items()} for r in rows],
        "focus": {k: _clip(v, MAX_TEXT) for k, v in rows[0].items()},
        "checks": checks,
        "review": {"daily": _clip(" ".join(daily), MAX_REVIEW)},
    }


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------------------------------------------
# LLM 메시지와 검사


def build_messages(req: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, str]]:
    """입력 → LLM 메시지(텍스트만). 필요한 값만 골라 넣는다(리더·참석자·계정은 입력 계약에 없다)."""
    facts: dict[str, Any] = {
        "report_date": req["report_date"],
        "task_name": task_name(req),
        "place": place(req),
        "tasks": [{"label": t["label"]} if "code" in t else {"text": t["text"].strip()} for t in req["tasks"]],
        "rule_tags": [{"rule_tag": t, "name": TAG_THEMES[t].name, "check_code": TAG_THEMES[t].check} for t in _tags(req)],
        "zones": [{"name": z["name"], "zone_kind": z["zone_kind"], "rule_id": z["rule_id"]} for z in req["zones"]],
        "checks": [{"code": code, "label": label} for code, label in CHECKS],
        "existing": _existing_facts(req.get("existing") or {}),
        "clauses": [
            {"node_id": n.get("node_id"), "source_name": n.get("source_name"), "article_ref": n.get("article_ref"),
             "title": n.get("title"), "body": (n.get("body") or "")[:MAX_CLAUSE_BODY]}
            for n in nodes
        ],
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + REPORT_NAME_GUIDE},
        {"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True)},
    ]
    assert_text_only(messages)
    return messages


def _existing_facts(existing: Mapping[str, Any]) -> dict[str, Any]:
    """이미 채운 칸 중 비어 있지 않은 것만(빈 칸은 초안이 채울 곳)."""
    out: dict[str, Any] = {}
    for key in ("task_name", "place"):
        if isinstance(existing.get(key), str) and existing[key].strip():
            out[key] = existing[key].strip()
    if isinstance(existing.get("risk_assessed"), bool):
        out["risk_assessed"] = existing["risk_assessed"]
    risks = [{k: v.strip() for k, v in r.items() if v.strip()} for r in existing.get("risks") or []]
    if any(risks):
        out["risks"] = risks
    focus = {k: v.strip() for k, v in (existing.get("focus") or {}).items() if v.strip()}
    if focus:
        out["focus"] = focus
    checks = [
        {"code": c["code"]} | ({"yes": c["yes"]} if isinstance(c.get("yes"), bool) else {})
        | ({"action": c["action"].strip()} if (c.get("action") or "").strip() else {})
        for c in existing.get("checks") or []
    ]
    if any(len(c) > 1 for c in checks):
        out["checks"] = [c for c in checks if len(c) > 1]
    daily = ((existing.get("review") or {}).get("daily") or "").strip()
    if daily:
        out["review"] = {"daily": daily}
    return out


def _unfence(content: str) -> str:
    """```json … ``` 한 겹은 벗긴다(내용은 그대로 검사한다)."""
    body = content.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, flags=re.DOTALL)
    return m.group(1) if m else body


def check_output(content: str, req: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]] = ()) -> tuple[dict[str, Any] | None, list[str]]:
    """LLM 응답 → (정리한 출력, 문제 목록). 문제가 하나라도 있으면 출력은 None. 문제 문장에 응답 값을 넣지 않는다."""
    try:
        doc = json.loads(_unfence(content))
    except (ValueError, TypeError):
        return None, ["JSON 객체가 아니다"]
    if not isinstance(doc, dict) or set(doc) != OUTPUT_KEYS:
        return None, [f"최상위 키는 {sorted(OUTPUT_KEYS)} 만 둔다"]
    allowed_ids = {n.get("node_id") for n in nodes}
    articles = {m.group(0).replace(" ", "") for n in nodes for m in _ARTICLE_IN_TEXT.finditer(n.get("article_ref") or "")}
    union = set(req["rule_tags"])
    problems: list[str] = []

    def text(value: Any, limit: int, where: str) -> str:
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: 비지 않은 글자여야 한다")
            return ""
        value = value.strip()
        if len(value) > limit:
            problems.append(f"{where}: {limit}자를 넘는다")
        if "\x00" in value:
            problems.append(f"{where}: NUL 을 쓰지 않는다")
        if _PERCENT.search(value) or "확률" in value:
            problems.append(f"{where}: 확률·퍼센트 표현을 쓰지 않는다")
        if any(m.group(0).replace(" ", "") not in articles for m in _ARTICLE_IN_TEXT.finditer(value)):
            problems.append(f"{where}: 입력 clauses 에 없는 조문 번호")
        return value

    def pair(value: Any, keys: set[str], where: str) -> dict[str, str]:
        if not isinstance(value, dict) or set(value) != keys:
            problems.append(f"{where}: 키는 {sorted(keys)} 만 둔다")
            return {}
        return {"risk": text(value["risk"], MAX_TEXT, f"{where}.risk"), "measure": text(value["measure"], MAX_TEXT, f"{where}.measure")}

    risks, tagged = [], False
    if not isinstance(doc["risks"], list) or len(doc["risks"]) != RISK_ROWS:
        problems.append(f"risks 는 정확히 {RISK_ROWS}줄이다")
    else:
        for i, row in enumerate(doc["risks"]):
            risks.append(pair(row, {"risk", "measure", "rule_tags"}, f"risks[{i}]"))
            tags = row.get("rule_tags") if isinstance(row, dict) else None
            if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags) or len(set(tags)) != len(tags):
                problems.append(f"risks[{i}].rule_tags: 중복 없는 문자열 배열이어야 한다")
            elif not set(tags) <= union:
                problems.append(f"risks[{i}].rule_tags: 입력 rule_tags 밖의 태그")
            else:
                tagged |= bool(tags)
    if union and risks and not tagged:
        problems.append("risks 가 입력 rule_tags 의 위험을 하나도 다루지 않았다")
    focus = pair(doc["focus"], {"risk", "measure"}, "focus")
    checks = []
    if not isinstance(doc["checks"], list) or len(doc["checks"]) != len(CHECK_CODES):
        problems.append(f"checks 는 정확히 {len(CHECK_CODES)}줄이다")
    else:
        for i, (row, code) in enumerate(zip(doc["checks"], CHECK_CODES)):
            if not isinstance(row, dict) or set(row) != {"code", "action"}:
                problems.append(f"checks[{i}]: 키는 code·action 만 둔다")
                continue
            if row["code"] != code:
                problems.append(f"checks[{i}]: 코드는 {code} 이다(순서 고정)")
            checks.append({"code": code, "action": text(row["action"], MAX_TEXT, f"checks[{i}].action")})
    daily = text(doc["review_daily"], MAX_REVIEW, "review_daily")
    cited: list[str] = []
    if not isinstance(doc["citations"], list):
        problems.append("citations 는 배열이다")
    else:
        for c in doc["citations"]:
            if not isinstance(c, str) or c not in allowed_ids:
                problems.append("citations: 입력 clauses 에 없는 node_id")
            elif c in cited:
                problems.append("citations: 같은 인용이 두 번")
            else:
                cited.append(c)
    if problems:
        return None, problems
    return {"risks": risks, "focus": focus, "checks": checks, "review_daily": daily, "citations": cited}, []


def assemble(req: Mapping[str, Any], out: Mapping[str, Any]) -> dict[str, Any]:
    """검사한 LLM 출력 + 코드가 정한 칸 → form_draft. 점검 yes 는 null 이다."""
    return {
        "task_name": task_name(req),
        "place": place(req),
        "risk_assessed": True if req["rule_tags"] else None,
        "risks": [dict(r) for r in out["risks"]],
        "focus": dict(out["focus"]),
        "checks": [{"code": c["code"], "yes": None, "action": c["action"]} for c in out["checks"]],
        "review": {"daily": out["review_daily"]},
    }


def form_problems(form: Mapping[str, Any]) -> list[str]:
    """form_draft 의 줄 수·점검 순서·글자 수·yes null 을 다시 본다(스키마 검증은 처리기가 응답 전체로 한 번 더 한다)."""
    problems: list[str] = []

    def length(value: Any, lo: int, hi: int, where: str) -> None:
        if not isinstance(value, str) or not lo <= len(value) <= hi:
            problems.append(f"{where}: {lo}~{hi}자 글자가 아니다")

    length(form.get("task_name"), 1, MAX_SHORT, "task_name")
    length(form.get("place"), 0, MAX_PLACE, "place")
    if form.get("risk_assessed") not in (True, False, None):
        problems.append("risk_assessed: 참·거짓·null 이 아니다")
    risks = form.get("risks")
    if not isinstance(risks, list) or len(risks) != RISK_ROWS:
        problems.append("risks: 3줄이 아니다")
        risks = []
    for i, row in enumerate([*risks, form.get("focus")]):
        where = f"risks[{i}]" if i < len(risks) else "focus"
        row = row if isinstance(row, Mapping) else {}
        length(row.get("risk"), 1, MAX_TEXT, f"{where}.risk")
        length(row.get("measure"), 1, MAX_TEXT, f"{where}.measure")
    checks = form.get("checks")
    if not isinstance(checks, list) or [c.get("code") for c in checks] != list(CHECK_CODES):
        problems.append("checks: 5줄 코드·순서가 아니다")
    else:
        for i, c in enumerate(checks):
            if c.get("yes") is not None:
                problems.append(f"checks[{i}].yes: 초안은 null 이다")
            length(c.get("action"), 0, MAX_TEXT, f"checks[{i}].action")
    length((form.get("review") or {}).get("daily"), 1, MAX_REVIEW, "review.daily")
    return problems


# ---------------------------------------------------------------------------------------------------------------
# 생성


def _retrieve(store: Any, req: Mapping[str, Any], steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """법령 그래프 조문 허용 목록. 그래프가 없거나 실패하면 빈 목록(초안은 조문 번호 없이 만든다)."""
    if store is None:
        steps.append(_step("CANDIDATE_SEARCH", "SKIPPED", "법령 그래프 설정 없음"))
        return []
    from simai_ai.report.retrieval import retrieve

    coded = [t for t in req["tasks"] if "code" in t]
    pseudo = {
        "risk_items": [{"rule_id": t} for t in _tags(req)],
        "zones": [{"zone_kind": z["zone_kind"]} for z in req["zones"]],
        "work_type": {"code": coded[0]["code"], "label": ", ".join(t["label"] for t in coded)} if coded else None,
        "legal_basis": [],
    }
    try:
        if store.corpus_empty():
            steps.append(_step("CANDIDATE_SEARCH", "FAILED", "코퍼스가 비었다"))
            return []
        nodes, _graph, count, _missing = retrieve(store, pseudo)
    except Exception:  # noqa: BLE001 - DB·드라이버 오류는 초안을 막지 않는다(조문 없이 만든다)
        steps.append(_step("CANDIDATE_SEARCH", "FAILED", "법령 그래프를 읽지 못했다"))
        return []
    steps.append(_step("CANDIDATE_SEARCH", "SUCCEEDED" if nodes else "FAILED", f"후보 {count}건, 허용 조문 {len(nodes)}건"))
    return list(nodes)


def _step(step: str, status: str, detail: str) -> dict[str, Any]:
    return {"step": step, "status": status, "detail": detail[:500]}


def generate_form_draft(
    req: Mapping[str, Any],
    client_factory: Callable[[], ChatClient],
    store: Any = None,
) -> dict[str, Any]:
    """입력(검사 통과한 tbm_form_draft_request) → tbm_form_draft_result. LLM 이 실패해도 대체 초안으로 늘 결과를 낸다."""
    steps: list[dict[str, Any]] = []
    nodes = _retrieve(store, req, steps)
    results: list[ChatResult] = []
    form: dict[str, Any] | None = None
    cited: list[str] = []
    reason: str | None = None
    model: str | None = None
    try:
        client = client_factory()
        model = client.model
        messages = build_messages(req, nodes)
        problems: list[str] = []
        for attempt in range(MAX_ATTEMPTS):
            name = "LLM_RETRY" if attempt else "LLM_DRAFT"
            if attempt:
                messages = messages[:2] + [{
                    "role": "user",
                    "content": "앞 초안은 다음 이유로 쓸 수 없었다. 규칙을 지켜 처음부터 다시 쓴다:\n- " + "\n- ".join(problems[:20]),
                }]
                assert_text_only(messages)
            start = time.monotonic()
            try:
                result = client.complete(messages)
            except LlmError:
                steps.append(_step(name, "FAILED", "LLM 호출 실패"))
                raise
            results.append(result)
            steps.append(_step(name, "SUCCEEDED", f"초안 수신 {round((time.monotonic() - start) * 1000)} ms"))
            out, problems = check_output(result.content, req, nodes)
            candidate = assemble(req, out) if out is not None else None
            if candidate is not None:
                problems = form_problems(candidate)
            steps.append(_step("CODE_VERIFY", "FAILED" if problems else "SUCCEEDED", "; ".join(problems[:5]) or "검사 통과"))
            if not problems:
                form, cited = candidate, out["citations"]
                break
        else:
            reason = "VALIDATION_FAILED"
    except LlmConfigError:
        reason = "LLM_NOT_CONFIGURED"
    except (LlmTimeout, TimeoutError):
        reason = "LLM_TIMEOUT"
    except (LlmError, OSError):
        reason = "LLM_UNAVAILABLE"
    if form is None:
        form = fallback_draft(req)
        cited = []
        steps.append(_step("FALLBACK", "SUCCEEDED", f"결정적 대체 초안({reason})"))
    by_id = {n.get("node_id"): n for n in nodes}
    return {
        "request_id": req["request_id"],
        "form_draft": form,
        "fallback": reason is not None,
        "fallback_reason": reason,
        "citations": [_citation(by_id[c]) for c in cited],
        "generator": {
            "model": (results[-1].model if results and results[-1].model else model) or DETERMINISTIC,
            "prompt_version": PROMPT_VERSION,
            "attempts": len(results),
            "input_tokens": _sum(r.input_tokens for r in results),
            "output_tokens": _sum(r.output_tokens for r in results),
        },
        "steps": steps[:10],
    }


def _citation(node: Mapping[str, Any]) -> dict[str, Any]:
    def cap(value: Any, limit: int) -> str | None:
        return value[:limit] if isinstance(value, str) else None

    return {
        "node_id": str(node["node_id"])[:200],
        "source_name": cap(node.get("source_name"), 200),
        "article_ref": cap(node.get("article_ref"), 200),
        "title": cap(node.get("title"), 500),
        "excerpt": (node.get("body") or "")[:200],
    }


def _sum(values: Iterable[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


# ---------------------------------------------------------------------------------------------------------------
# 종료 미팅(B10)


def generate_ending(req: Mapping[str, Any], client_factory: Callable[[], ChatClient]) -> dict[str, Any]:
    """tbm_form_ending_request → tbm_form_ending_result. 기존 작업 마무리(wrap_up)의 숫자 검사·1회 재작성·결정적 문장을 그대로 쓰고
    2000자 상한만 더한다. 날짜·작업명은 요청 계약에 없어 제목은 '작업 종료 미팅'으로 둔다.
    집계가 맞지 않으면 wrap_up.validate_request 가 ValueError(처리기는 400)."""
    data = {"work_type": None, "window_ms": list(req["window_ms"]), "facts": req["facts"]}
    out = wrap_up.run(data, client_factory, max_chars=MAX_REVIEW)
    text = out["text"] if len(out["text"]) <= MAX_REVIEW else _clip_sentences(out["text"], MAX_REVIEW)
    return {
        "request_id": req["request_id"],
        "ending": text,
        "fallback": out["fallback"],
        "fallback_reason": out["reason"],
        "model": out["model"],
    }


def _clip_sentences(text: str, limit: int) -> str:
    """결정적 문장이 상한을 넘으면(규칙·구역이 아주 많을 때) 마지막 온전한 문장까지만."""
    cut = text[:limit]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > 0 else _clip(text, limit)
