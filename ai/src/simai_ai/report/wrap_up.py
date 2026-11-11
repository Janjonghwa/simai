"""작업 종료 집계만으로 문장을 만든다. 숫자와 그 항목을 함께 검증하고 최대 한 번 재작성한다."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from .llm import ChatClient, LlmConfigError, LlmError, LlmTimeout

SEVERITIES = ("CRITICAL", "HIGH", "CAUTION", "INFO")
WORK_TYPES = frozenset(("SCAFFOLD_INSTALL", "OPENING_WORK", "HIGH_PLACE", "MATERIAL_HANDLING",
                        "EQUIPMENT_OP", "FLOOR_FINISHING", "GENERAL"))
COUNT_FIELDS = ("event_count", "operator_ack_count", "operator_close_count", "outside_work_entry_count", "unacknowledged_critical_count")
RULE_DESCRIPTIONS = {
    "R1": "위험 구역 진입", "R2": "가장자리 근접", "R3": "고소 위치", "R4": "낙상 의심",
    "R5": "진입 예측", "R6": "위험 구역 내 관측 상실", "R7": "구역 감시 능력 저하",
    # 명세 7장 R8~R10, R11 조끼(-549). 이 사전에 없는 규칙이 오면 요청 전체를 거절해 대체 문장으로 간다.
    "R8": "장비 근접", "R9": "안전모 미착용", "R10": "불확실 점유 근접", "R11": "조끼 미착용",
}


def validate_request(data: Any) -> dict[str, Any]:
    """닫힌 입력으로 이름·좌표·영상 및 임의 자유 텍스트가 프롬프트에 섞이지 않게 한다."""
    if not isinstance(data, dict) or set(data) != {"work_type", "window_ms", "facts"}:
        raise ValueError("invalid wrap-up request")
    if data["work_type"] is not None and (
        not isinstance(data["work_type"], str) or data["work_type"] not in WORK_TYPES
    ):
        raise ValueError("invalid work type")
    window = data["window_ms"]
    if not isinstance(window, list) or len(window) != 2 or any(type(v) is not int or v < 0 for v in window):
        raise ValueError("invalid window")
    if window[1] < window[0] or window[1] > 253_402_300_799_999:
        raise ValueError("invalid window")
    facts = data["facts"]
    if not isinstance(facts, dict) or set(facts) != {*COUNT_FIELDS, "severity_counts", "rule_counts", "max_severity", "phase_counts", "first_event_min", "last_event_min", "top_zones"}:
        raise ValueError("invalid facts")
    for key in COUNT_FIELDS:
        if type(facts[key]) is not int or not 0 <= facts[key] <= 2**63 - 1:
            raise ValueError("invalid count")
    severities, rules = facts["severity_counts"], facts["rule_counts"]
    if not isinstance(severities, dict) or set(severities) != set(SEVERITIES) or not isinstance(rules, dict):
        raise ValueError("invalid breakdown")
    if any(not isinstance(key, str) or not key in RULE_DESCRIPTIONS for key in rules):
        raise ValueError("invalid rule")
    if any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in (*severities.values(), *rules.values())):
        raise ValueError("invalid breakdown count")
    if sum(severities.values()) != facts["event_count"] or sum(rules.values()) != facts["event_count"]:
        raise ValueError("inconsistent total")
    zones = facts["top_zones"]
    if not isinstance(zones, list) or len(zones) > 3:
        raise ValueError("invalid zones")
    names = set()
    for zone in zones:
        if not isinstance(zone, dict) or set(zone) != {"name", "count"}:
            raise ValueError("invalid zone")
        name, count = zone["name"], zone["count"]
        if (not isinstance(name, str) or not 1 <= len(name) <= 40 or name != name.strip()
                or any(unicodedata.category(c).startswith("C") for c in name)
                or name in names or type(count) is not int or not 1 <= count <= facts["event_count"]):
            raise ValueError("invalid zone")
        names.add(name)
    if (sum(z["count"] for z in zones) > facts["event_count"]
            or any(zones[i]["count"] < zones[i + 1]["count"] for i in range(len(zones) - 1))
            or bool(zones) != bool(facts["event_count"])):
        raise ValueError("inconsistent zones")
    if facts["unacknowledged_critical_count"] > severities["CRITICAL"]:
        raise ValueError("invalid unacknowledged critical count")
    phases = facts["phase_counts"]
    if (not isinstance(phases, dict) or set(phases) != {"early", "middle", "late"}
            or any(type(v) is not int or v < 0 for v in phases.values())
            or sum(phases.values()) != facts["event_count"]):
        raise ValueError("invalid phase counts")
    first, last = facts["first_event_min"], facts["last_event_min"]
    if facts["event_count"] == 0:
        if first is not None or last is not None:
            raise ValueError("invalid event times")
    elif (type(first) is not int or type(last) is not int
          or not 0 <= first <= last <= (window[1] - window[0]) // 60000):
        raise ValueError("invalid event times")
    maximum = next((s for s in SEVERITIES if severities[s]), None)
    if facts["max_severity"] != maximum or any(
        facts[k] > facts["event_count"] for k in ("operator_ack_count", "operator_close_count")
    ):
        raise ValueError("inconsistent facts")
    return data


def fact_clauses(facts: dict[str, Any]) -> list[str]:
    return [
        f"전체 이벤트 {facts['event_count']}건",
        *(f"등급 {s} {facts['severity_counts'][s]}건" for s in SEVERITIES),
        *(f"규칙 {r} {RULE_DESCRIPTIONS[r]} {n}건" for r, n in sorted(facts["rule_counts"].items())),
        *(f"구역 {z['name']} 이벤트 {z['count']}건" for z in facts["top_zones"]),
        f"최고 등급 {facts['max_severity'] or '없음'}",
        f"운영자 확인 {facts['operator_ack_count']}건",
        f"운영자 미확인 CRITICAL {facts['unacknowledged_critical_count']}건",
        f"운영자 종료 {facts['operator_close_count']}건",
        f"작업 외 진입 {facts['outside_work_entry_count']}건",
        *(f"작업 {label} 이벤트 {facts['phase_counts'][key]}건" for key, label in
          (("early", "초반"), ("middle", "중반"), ("late", "후반"))),
        *([f"첫 이벤트 시작 후 {facts['first_event_min']}분", f"마지막 이벤트 시작 후 {facts['last_event_min']}분"]
          if facts["first_event_min"] is not None else []),
    ]


def unwrap_paragraph(text: Any) -> str | None:
    """JSON이나 코드 블록으로 감싼 응답은 재작성을 요청한다."""
    if not isinstance(text, str):
        return None
    body = text.strip()
    if not body or body.startswith(('{', '[', '"', '```')):
        return None
    return body



def numbers_match(text: str, facts: dict[str, Any]) -> bool:
    """수치 집합만 비교하면 등급별 숫자를 서로 바꾼 오류를 놓친다. 필수 항목·숫자 쌍을 대조한다.

    정확한 사실 절을 한 번씩 제거한 뒤 남은 숫자(규칙 ID 포함)·추가 건수·확률을 거절한다.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 8_000:
        return False
    rest = text
    for clause in fact_clauses(facts):
        pattern = r"(?<![\w.+−-])" + re.escape(clause) + r"(?![\w])"
        rest, count = re.subn(pattern, "", rest, count=1)
        if count != 1:
            return False
    # 절 밖에는 제한된 연결 문구만 허용해 임의 구역명·규칙 설명도 막는다.
    rest = re.sub(r"작업 중|집계 내용은 다음과 같다|기록을 확인했다|로 집계됐다|로 기록됐다|집계됐다|기록됐다|그리고|또한|이며|이다", "", rest)
    if re.search(r"[^\s,.!?·]", rest):
        return False
    # 한글 수사로 수치를 추가하거나 허용 절 앞에 부호·소수점을 붙이는 우회도 거절한다.
    return not re.search(
        r"\d|[%％]|CRITICAL|HIGH|CAUTION|INFO|최고\s*등급|"
        r"[영일이삼사오육칠팔구십백천만한두세네다섯여섯일곱여덟아홉]+\s*(?:건|명|회|분|퍼센트)", rest
    )


def deterministic(facts: dict[str, Any]) -> str:
    return ". ".join(fact_clauses(facts)) + "."


def generate(data: dict[str, Any], client_factory: Callable[[], ChatClient]) -> dict[str, Any]:
    """/v1/report/wrap-up 응답: {text, fallback, model}."""
    out = run(data, client_factory)
    return {"text": out["text"], "fallback": out["fallback"], "model": out["model"]}


def run(data: dict[str, Any], client_factory: Callable[[], ChatClient], *, max_chars: int = 8_000) -> dict[str, Any]:
    """마무리 문단 생성. 결과에 대체 사유(reason)를 더한다(S15P31A101-616 종료 미팅이 같이 쓴다).

    max_chars 를 넘는 LLM 문단은 형식 위반으로 보고 한 번 다시 쓰게 한다. 결정적 문장은 그대로 돌려주므로 길이는 부르는 쪽이 본다.
    reason: None(LLM 문단), LLM_NOT_CONFIGURED, LLM_TIMEOUT, LLM_UNAVAILABLE, VALIDATION_FAILED.
    """
    data = validate_request(data)
    facts = data["facts"]
    required = deterministic(facts)
    messages = [
        {"role": "system", "content": (
            "작업 마무리를 집계 사실만으로 한국어로 작성한다. 아래 필수 사실 절을 각각 정확히 한 번 그대로 포함한다. "
            "다른 수치·날짜·시간·확률·사고 유무·안전 판정·추측은 추가하지 않는다. "
            "절 사이에는 작업 중, 그리고, 또한, 집계됐다, 기록됐다 같은 연결 문구만 쓴다. 필수 절 바깥에서는 규칙 ID도 쓰지 않는다. 설명이나 JSON 없이 문단만 반환한다."
        )},
        {"role": "user", "content": json.dumps({"work_type": data["work_type"], "facts": facts,
                                                 "required_clauses": required}, ensure_ascii=False)},
    ]
    reason = "VALIDATION_FAILED"
    try:
        client = client_factory()
        for attempt in range(2):
            result = client.complete(messages)
            paragraph = unwrap_paragraph(result.content)
            if paragraph is not None and len(paragraph) <= max_chars and numbers_match(paragraph, facts):
                return {"text": paragraph, "fallback": False, "model": result.model or client.model, "reason": None}
            if attempt == 0:
                # 거절된 초안을 다시 보내지 않는다(지어낸 내용이 다음 입력의 사실로 섞이지 않게).
                if paragraph is None:
                    why = "형식 검사 실패(JSON·코드 블록 금지, 문단 글만)."
                elif len(paragraph) > max_chars:
                    why = f"길이 초과({max_chars}자 이하)."
                else:
                    why = "항목별 숫자 검사 실패."
                messages.append({"role": "user", "content": f"{why} 필수 사실 절만 정확히 써서 문단 글로만 한 번 재작성한다."})
    except LlmConfigError:
        reason = "LLM_NOT_CONFIGURED"
    except (LlmTimeout, TimeoutError):
        reason = "LLM_TIMEOUT"
    except (LlmError, OSError, ValueError):
        reason = "LLM_UNAVAILABLE"
    return {"text": required, "fallback": True, "model": "deterministic", "reason": reason}
