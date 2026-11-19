"""관제·서버와 맞춘 알림·보고서 표시 이름(-660)과 화면·보고서 표시 번호(-804).

내부 rule_id(DB·계약·Tier 0·Risk Engine·API·로그)는 R1~R7·R9·R11을 유지하고,
사람이 보는 화면·보고서 문자열에서만 표시 번호(R9→R8, R11→R9)로 R1~R9를 이어 보인다.
"""

SEVERITY_LABELS = {"CRITICAL": "위험", "HIGH": "경고", "CAUTION": "주의", "INFO": "안내"}
RULE_DESCRIPTIONS = {
    "R1": "위험구역 진입",
    "R2": "추락 위험(가장자리)",
    "R3": "고소 작업면 위",
    "R4": "낙상 의심",
    "R5": "위험구역 접근",
    "R6": "위험구역 안 시야 끊김",
    "R7": "감시 저하",
    "R9": "안전모 미착용",
    "R11": "조끼 미착용",
}
DISPLAY_RULE_IDS = {
    "R1": "R1",
    "R2": "R2",
    "R3": "R3",
    "R4": "R4",
    "R5": "R5",
    "R6": "R6",
    "R7": "R7",
    "R9": "R8",
    "R11": "R9",
}


def display_rule_id(rule_id: str) -> str:
    """내부 rule_id를 사람이 보는 표시 번호(R1~R9)로 바꾼다. R9→R8, R11→R9, 모르는 값은 그대로 둔다."""
    return DISPLAY_RULE_IDS.get(rule_id, rule_id)


REPORT_NAME_GUIDE = (
    "규칙을 지칭할 때 다음 표시 이름을 그대로 쓴다: "
    + "; ".join(f"{rule}={name}" for rule, name in RULE_DESCRIPTIONS.items())
    + ". 화면·보고서 문장에서 규칙 번호를 적을 때는 표시 번호를 쓴다("
    + ", ".join(
        f"{rule}→{display}" for rule, display in DISPLAY_RULE_IDS.items() if rule != display
    )
    + ", R1~R7은 그대로)"
    + ". 등급 표시 이름: "
    + "; ".join(f"{severity}={name}" for severity, name in SEVERITY_LABELS.items())
    + ". 입력 규칙 ID와 등급 값 자체는 바꾸지 않는다."
)
