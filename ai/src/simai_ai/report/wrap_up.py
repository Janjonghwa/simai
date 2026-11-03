"""작업 종료 집계만으로 문장을 만든다. 숫자와 그 항목을 함께 검증하고 최대 한 번 재작성한다."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .llm import ChatClient, LlmError

SEVERITIES = ("CRITICAL", "HIGH", "CAUTION", "INFO")
WORK_TYPES = frozenset(("SCAFFOLD_INSTALL", "OPENING_WORK", "HIGH_PLACE", "MATERIAL_HANDLING",
                        "EQUIPMENT_OP", "FLOOR_FINISHING", "GENERAL"))
COUNT_FIELDS = ("event_count", "operator_ack_count", "operator_close_count", "outside_work_entry_count")


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
    if not isinstance(facts, dict) or set(facts) != {*COUNT_FIELDS, "severity_counts", "rule_counts", "max_severity"}:
        raise ValueError("invalid facts")
    for key in COUNT_FIELDS:
        if type(facts[key]) is not int or not 0 <= facts[key] <= 2**63 - 1:
            raise ValueError("invalid count")
    severities, rules = facts["severity_counts"], facts["rule_counts"]
    if not isinstance(severities, dict) or set(severities) != set(SEVERITIES) or not isinstance(rules, dict):
        raise ValueError("invalid breakdown")
    if any(not isinstance(key, str) or not re.fullmatch(r"R[1-7]", key) for key in rules):
        raise ValueError("invalid rule")
    if any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in (*severities.values(), *rules.values())):
        raise ValueError("invalid breakdown count")
    if sum(severities.values()) != facts["event_count"] or sum(rules.values()) != facts["event_count"]:
        raise ValueError("inconsistent total")
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
        *(f"규칙 {r} {n}건" for r, n in sorted(facts["rule_counts"].items())),
        f"최고 등급 {facts['max_severity'] or '없음'}",
        f"운영자 확인 {facts['operator_ack_count']}건",
        f"운영자 종료 {facts['operator_close_count']}건",
        f"작업 외 진입 {facts['outside_work_entry_count']}건",
    ]


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
    # 한글 수사로 수치를 추가하거나 허용 절 앞에 부호·소수점을 붙이는 우회도 거절한다.
    return not re.search(
        r"\d|[%％]|CRITICAL|HIGH|CAUTION|INFO|최고\s*등급|"
        r"[영일이삼사오육칠팔구십백천만한두세네다섯여섯일곱여덟아홉]+\s*(?:건|명|회|퍼센트)", rest
    )


def deterministic(facts: dict[str, Any]) -> str:
    return ". ".join(fact_clauses(facts)) + "."


def generate(data: dict[str, Any], client_factory: Callable[[], ChatClient]) -> dict[str, Any]:
    data = validate_request(data)
    facts = data["facts"]
    required = deterministic(facts)
    messages = [
        {"role": "system", "content": (
            "작업 마무리를 집계 사실만으로 한국어로 작성한다. 아래 필수 사실 절을 각각 정확히 한 번 그대로 포함한다. "
            "다른 수치·날짜·시간·확률·사고 유무·안전 판정·추측은 추가하지 않는다. "
            "필수 절 바깥에서는 규칙 ID도 쓰지 않는다. 설명이나 JSON 없이 문단만 반환한다."
        )},
        {"role": "user", "content": json.dumps({"work_type": data["work_type"], "facts": facts,
                                                 "required_clauses": required}, ensure_ascii=False)},
    ]
    try:
        client = client_factory()
        for attempt in range(2):
            result = client.complete(messages)
            if numbers_match(result.content, facts):
                return {"text": result.content.strip(), "fallback": False, "model": result.model or client.model}
            if attempt == 0:
                # 거절된 초안을 다시 보내지 않는다(지어낸 내용이 다음 입력의 사실로 섞이지 않게).
                messages.append({"role": "user", "content": "항목별 숫자 검사 실패. 필수 사실 절만 정확히 써서 한 번 재작성한다."})
    except (LlmError, TimeoutError, OSError, ValueError):
        pass
    return {"text": required, "fallback": True, "model": "deterministic"}
