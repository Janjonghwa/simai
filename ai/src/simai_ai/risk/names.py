"""관제·서버와 맞춘 알림·보고서 표시 이름(-660, 확장 규칙 R8·R10은 명세 7.2)."""

SEVERITY_LABELS = {"CRITICAL": "위험", "HIGH": "경고", "CAUTION": "주의", "INFO": "안내"}
RULE_DESCRIPTIONS = {
    "R1": "위험구역 진입",
    "R2": "추락 위험(가장자리)",
    "R3": "고소 작업면 위",
    "R4": "낙상 의심",
    "R5": "위험구역 접근",
    "R6": "위험구역 안 시야 끊김",
    "R7": "감시 저하",
    "R8": "장비 근접",
    "R9": "안전모 미착용",
    "R10": "불확실 점유 근접",
    "R11": "조끼 미착용",
}


REPORT_NAME_GUIDE = (
    "규칙을 지칭할 때 다음 표시 이름을 그대로 쓴다: "
    + "; ".join(f"{rule}={name}" for rule, name in RULE_DESCRIPTIONS.items())
    + ". 등급 표시 이름: "
    + "; ".join(f"{severity}={name}" for severity, name in SEVERITY_LABELS.items())
    + ". 입력 규칙 ID와 등급 값 자체는 바꾸지 않는다."
)
