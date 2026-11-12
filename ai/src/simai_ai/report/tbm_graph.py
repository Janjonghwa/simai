"""EC2 TBM 보고서·일일 일지 생성과 결정적 인용 검사."""
import json
import re
import time

from simai_ai.risk.names import REPORT_NAME_GUIDE
from simai_ai.report.llm import LlmError, LlmTimeout
from simai_ai.report.retrieval import retrieve
from simai_ai.report.tbm import _ARTICLE_IN_TEXT, _PERCENT, assert_text_only

KEYS = {"hazards", "measures", "focus", "legal"}
# 일일 일지(DAILY, FR-122): 그날 LIVE 이벤트 요약·되돌아보기·다음 날 대책·법령 근거
DAILY_KEYS = {"summary", "events", "measures", "legal"}
EVENT_FIELDS = ("rule_id", "severity", "zone_id", "raised_at_ms", "resolved", "needs_check")
# 일일 일지에 넣는 이벤트 상한. 그 이상은 event_summary 집계로만 넘긴다(LLM 입력 길이)
MAX_DAILY_EVENTS = 100
ITEM_FIELDS = ("rule_id", "rule_version", "severity", "zone_id", "position_ids", "metrics", "reason_codes", "confidence_reasons")
# metrics 는 계약에서 정의된 숫자 칸만 보낸다.
METRICS = {"zone_distance_m", "edge_distance_m", "height_diff_m", "horizontal_distance_m", "vertical_distance_m", "height_difference_m", "distance_m", "height_m", "speed_mps", "duration_ms", "threshold_m", "overlap_ratio"}

def _pick(obj, fields):
    def safe(value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [v for v in value if isinstance(v, (str, int, float, bool))]
        return None
    return {k: safe(obj[k]) for k in fields if k in obj}

def _keys(kind):
    return DAILY_KEYS if kind == "DAILY" else KEYS


def _daily_messages(request, nodes):
    events = [_pick(e, EVENT_FIELDS) for e in (request.get("events") or []) if isinstance(e, dict)][:MAX_DAILY_EVENTS]
    summary = request.get("event_summary") if isinstance(request.get("event_summary"), dict) else {}
    facts = {"report_kind": "DAILY", "report_date": request.get("report_date") if isinstance(request.get("report_date"), str) else None,
             "rule_version": request.get("rule_version"),
             "event_summary": {k: v for k, v in summary.items() if k in {"event_count", "resolved_count", "needs_check_count"} and isinstance(v, int) and not isinstance(v, bool)}
             | ({"events_truncated": summary["events_truncated"]} if isinstance(summary.get("events_truncated"), bool) else {})
             | {k: {kk: vv for kk, vv in (summary.get(k) or {}).items() if isinstance(kk, str) and isinstance(vv, int) and not isinstance(vv, bool)}
                for k in ("severity_counts", "rule_counts") if isinstance(summary.get(k), dict)},
             "events": events,
             "zones": [_pick(z, ("zone_id", "zone_kind", "label")) for z in request.get("zones", []) if isinstance(z, dict)],
             "clauses": [{k: n.get(k) for k in ("node_id", "source_name", "article_ref", "title")} | {"body": n["body"][:600]} for n in nodes]}
    result = [{"role": "system", "content": "일일 안전 일지를 입력 사실과 허용 조문만으로 작성하라. 이벤트 수·등급·규칙은 입력 그대로 쓰고 지어내지 말라. 위험 등급을 바꾸거나 확률·퍼센트를 쓰지 말라. 이벤트가 없으면 없었다고 쓴다. events_truncated 가 참이면 events 는 등급이 높은 일부이고 건수는 event_summary 가 정확하다. JSON 객체 sections 배열에 summary(하루 요약), events(이벤트 되돌아보기), measures(다음 작업 대책), legal(법령 근거 요약) 각각 하나를 넣고 각 항목은 key, title, body, citations만 쓴다. citations에는 제공된 node_id만 쓴다."}, {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}]
    result[0]["content"] += "\n" + REPORT_NAME_GUIDE
    assert_text_only(result)
    return result


def messages(request, nodes):
    if request.get("report_kind") == "DAILY":
        return _daily_messages(request, nodes)
    work = request.get("work_type") or {}
    facts = {"report_kind": "TBM", "rule_version": request.get("rule_version"),
             "work_type": _pick(work, ("code", "label")),
             "risk_items": [], "zones": [], "zone_history": [],
             "clauses": [{k: n.get(k) for k in ("node_id", "source_name", "article_ref", "title")} | {"body": n["body"][:600]} for n in nodes]}
    for item in request.get("risk_items", []):
        if isinstance(item, dict):
            clean = _pick(item, ITEM_FIELDS)
            clean["metrics"] = {k: v for k, v in (item.get("metrics") or {}).items() if k in METRICS and isinstance(v, (int, float)) and not isinstance(v, bool)}
            facts["risk_items"].append(clean)
    facts["zones"] = [_pick(z, ("zone_id", "zone_kind", "label")) for z in request.get("zones", []) if isinstance(z, dict)]
    facts["zone_history"] = [_pick(z, ("zone_id", "days", "live_event_count", "rules", "max_severity", "last_at_ms")) for z in request.get("zone_history", []) if isinstance(z, dict)]
    result = [{"role": "system", "content": "안전 보고서를 입력 사실과 허용 조문만으로 작성하라. 위험 등급을 바꾸거나 확률·퍼센트를 쓰지 말라. JSON 객체 sections 배열에 hazards, measures, focus, legal 각각 하나를 넣고 각 항목은 key, title, body, citations만 쓴다. citations에는 제공된 node_id만 쓴다."}, {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}]
    result[0]["content"] += "\n" + REPORT_NAME_GUIDE
    assert_text_only(result)
    return result

def check(content, nodes, kind="TBM"):
    keys = _keys(kind)
    try:
        doc = json.loads(content)
    except (ValueError, TypeError):
        return None, ["JSON 객체가 아니다"]
    if not isinstance(doc, dict) or set(doc) != {"sections"} or not isinstance(doc["sections"], list):
        return None, ["sections 형식 오류"]
    allowed = {n["node_id"] for n in nodes}
    articles = {m.group(0).replace(" ", "") for n in nodes for m in _ARTICLE_IN_TEXT.finditer(n.get("article_ref") or "")}
    problems, cited, seen = [], [], []
    for s in doc["sections"]:
        if not isinstance(s, dict) or set(s) != {"key", "title", "body", "citations"}:
            problems.append("섹션 형식 오류")
            continue
        key, title, body, refs = s["key"], s["title"], s["body"], s["citations"]
        if not isinstance(key, str) or key not in keys or key in seen:
            problems.append("섹션 key 오류")
        seen.append(key)
        if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip() or not isinstance(refs, list):
            problems.append("섹션 내용 오류")
            continue
        for ref in refs:
            if not isinstance(ref, str) or ref not in allowed:
                problems.append("허용되지 않은 인용")
            elif ref not in cited:
                cited.append(ref)
        for m in _ARTICLE_IN_TEXT.finditer(body):
            if m.group(0).replace(" ", "") not in articles:
                problems.append("허용되지 않은 본문 조문")
        if _PERCENT.search(body) or "확률" in body:
            problems.append("확률·퍼센트 표현")
    if set(x for x in seen if isinstance(x, str)) != keys or len(seen) != 4:
        problems.append("4개 섹션 필요")
    return (doc["sections"], cited) if not problems else None, problems

def generate(request, store, client_factory):
    steps = []
    result = {"status": "FAILED", "failure_reason": None, "sections": [], "citations": [], "subgraph": {"nodes": [], "edges": []}, "steps": steps, "llm": {"model": None, "calls": 0}, "corpus": {"embedding_model": None, "embedding_revision": None}}
    def step(name, start, status, inc, out, detail):
        steps.append({"step": name, "label": name, "status": status, "input_count": inc, "output_count": out, "duration_ms": round((time.monotonic()-start)*1000), "detail": detail})
    start = time.monotonic()
    if store.corpus_empty():
        step("CANDIDATE_SEARCH", start, "FAILED", 0, 0, "코퍼스가 비었다")
        result["failure_reason"] = "CORPUS_EMPTY"
        return result
    nodes, graph, count, missing = retrieve(store, request)
    result["subgraph"] = graph
    facts_in = request.get("events") if request.get("report_kind") == "DAILY" else request.get("risk_items")
    step("CANDIDATE_SEARCH", start, "SUCCEEDED", len(facts_in) if isinstance(facts_in, list) else 0, count, "템플릿 없음: " + ", ".join(missing) if missing else "후보 검색 완료")
    start = time.monotonic()
    step("PRE_FILTER", start, "SUCCEEDED" if nodes else "FAILED", count, len(nodes), "임베딩 조문 허용 목록")
    if not nodes:
        result["failure_reason"] = "NO_CANDIDATES"
        return result
    result["corpus"] = {"embedding_model": nodes[0].get("embedding_model"), "embedding_revision": nodes[0].get("embedding_revision")}
    try:
        client = client_factory()
        result["llm"]["model"] = client.model
        msgs = messages(request, nodes)
        for attempt in range(2):
            if attempt:
                msgs = msgs[:2] + [{"role": "user", "content": "다음 위반을 고쳐 다시 작성하라: " + "; ".join(problems)}]
                assert_text_only(msgs)
            start = time.monotonic()
            result["llm"]["calls"] += 1
            try:
                response = client.complete(msgs)
            except LlmError:
                step("LLM_DRAFT" if not attempt else "LLM_RETRY", start, "FAILED", len(nodes), 0, "LLM 호출 실패")
                raise
            step("LLM_DRAFT" if not attempt else "LLM_RETRY", start, "SUCCEEDED", len(nodes), 1, "초안 수신")
            start = time.monotonic()
            draft, problems = check(response.content, nodes, request.get("report_kind", "TBM"))
            step("CODE_VERIFY", start, "SUCCEEDED" if draft else "FAILED", 1, int(bool(draft)), "; ".join(problems) if problems else "검사 통과")
            if draft:
                sections, cited = draft
                result.update(status="SUCCEEDED", sections=sections, citations=[{k: n.get(k) for k in ("node_id", "source_name", "article_ref", "title")} | {"excerpt": n["body"][:200]} for n in nodes if n["node_id"] in cited])
                for node in graph["nodes"]:
                    node["cited"] = node["node_id"] in cited
                return result
        result["failure_reason"] = "CITATION_VIOLATION"
    except LlmTimeout:
        result["failure_reason"] = "LLM_TIMEOUT"
    except (LlmError, ValueError):
        result["failure_reason"] = "LLM_UNAVAILABLE"
    return result
