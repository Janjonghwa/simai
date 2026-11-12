"""TBM 양식 초안의 고정 사전과 결정적 대체 문장(초안 대체 문장, S15P31A101-616).

LLM 이 없거나 두 초안이 모두 검사를 통과하지 못하면 이 문장들로 양식 칸을 채운다(fallback true).
문장은 규칙 태그(명세 7장 R1~R11)·점검 코드(서버 TbmForm.CHECKS)마다 하나씩이고, 자리 표시는 `{where}`(관련 구역 이름 + ' 주변',
없으면 '작업 구간') 하나뿐이다. 사람·수치·조문 번호를 넣지 않는다. 목록은 설계 docs/design/S15P31A101-616-tbm-form-draft.md 에도 적는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from simai_ai.risk.names import RULE_DESCRIPTIONS

# 점검 5줄: 코드 → 서버 문구(서버가 label 을 덮으므로 여기 문구는 LLM 에 뜻을 알려 주는 데만 쓴다). 순서가 양식 줄 순서다.
CHECKS: tuple[tuple[str, str], ...] = (
    ("PPE", "안전모 등 보호구 착용 상태 확인"),
    ("ZONE_CONTROL", "작업 구간 위험구역 표시 및 접근 통제"),
    ("OPENING_GUARD", "개구부 덮개·난간 설치 확인"),
    ("WORK_SURFACE_FALL", "고소 작업면 상태 및 추락 방호 확인"),
    ("HEALTH", "작업자 건강 상태 확인"),
)
CHECK_CODES: tuple[str, ...] = tuple(code for code, _ in CHECKS)


@dataclass(frozen=True)
class TagTheme:
    name: str  # 규칙 이름(명세 7장, wrap_up.RULE_DESCRIPTIONS 와 같다)
    check: str  # 이 위험과 맞닿은 점검 코드
    risk: str
    measure: str


# 규칙 태그 → 위험 주제. 예: R2(개구부·가장자리) → OPENING_GUARD, R3·R4(고소·낙상) → WORK_SURFACE_FALL.
TAG_THEMES: dict[str, TagTheme] = {
    "R1": TagTheme(RULE_DESCRIPTIONS["R1"], "ZONE_CONTROL",
                   "{where} 위험구역에 작업자가 들어가 다칠 위험",
                   "{where} 위험구역 경계를 표시하고 작업자 외 출입을 통제한다"),
    "R2": TagTheme(RULE_DESCRIPTIONS["R2"], "OPENING_GUARD",
                   "{where} 개구부·단부 가장자리에 다가가 떨어질 위험",
                   "개구부 덮개를 고정하고 단부에 안전난간을 설치한 뒤 작업한다"),
    "R3": TagTheme(RULE_DESCRIPTIONS["R3"], "WORK_SURFACE_FALL",
                   "{where} 높은 작업면에서 균형을 잃고 떨어질 위험",
                   "작업발판·난간 상태를 확인하고 안전대를 걸고 작업한다"),
    "R4": TagTheme(RULE_DESCRIPTIONS["R4"], "WORK_SURFACE_FALL",
                   "{where} 자재·장애물에 걸리거나 미끄러져 넘어질 위험",
                   "통로의 자재와 장애물을 치우고 바닥 상태를 확인한 뒤 작업한다"),
    "R5": TagTheme(RULE_DESCRIPTIONS["R5"], "ZONE_CONTROL",
                   "{where} 위험구역 쪽으로 이동하다 모르고 들어갈 위험",
                   "이동 동선을 위험구역과 떨어뜨려 정하고 동선을 작업자에게 알린다"),
    "R6": TagTheme(RULE_DESCRIPTIONS["R6"], "ZONE_CONTROL",
                   "{where} 위험구역 안 작업자가 가려져 위험을 놓칠 위험",
                   "위험구역 안에서는 2인 1조로 작업하고 서로 위치를 확인한다"),
    "R7": TagTheme(RULE_DESCRIPTIONS["R7"], "ZONE_CONTROL",
                   "{where} 자재·시야 가림으로 위험구역 감시가 약해질 위험",
                   "시야를 가리는 자재를 치우고 위험구역 감시자를 둔다"),
    "R8": TagTheme(RULE_DESCRIPTIONS["R8"], "ZONE_CONTROL",
                   "{where} 작업 장비에 가까이 다가가 부딪히거나 끼일 위험",
                   "장비 작업 반경을 표시하고 신호수를 배치한다"),
    "R9": TagTheme(RULE_DESCRIPTIONS["R9"], "PPE",
                   "안전모를 쓰지 않아 떨어지는 물체에 머리를 다칠 위험",
                   "작업 전 안전모 착용과 턱끈 체결을 서로 확인한다"),
    "R10": TagTheme(RULE_DESCRIPTIONS["R10"], "ZONE_CONTROL",
                    "{where} 확인되지 않은 물체가 있는 곳에 다가갈 위험",
                    "작업 전 주변을 확인하고 확인되지 않은 곳은 출입을 통제한다"),
    "R11": TagTheme(RULE_DESCRIPTIONS["R11"], "PPE",
                    "안전조끼를 입지 않아 장비 운전자 눈에 띄지 않을 위험",
                    "작업 전 안전조끼 착용을 서로 확인한다"),
}

# 위험 3줄을 고르는 순서: 떨어짐(개구부·고소·낙상) → 구역 진입 → 장비·보호구 → 감시 상태
TAG_PRIORITY: tuple[str, ...] = ("R2", "R3", "R4", "R1", "R5", "R8", "R9", "R11", "R6", "R7", "R10")

# 규칙 태그가 모자랄 때(직접 쓴 작업만 있거나 태그가 1~2개) 채우는 일반 위험
GENERAL_RISKS: tuple[tuple[str, str], ...] = (
    ("작업 통로에 놓인 자재에 걸려 넘어질 위험", "작업 전 통로를 정리하고 자재는 정해진 곳에 쌓는다"),
    ("보호구를 갖추지 않고 작업에 들어갈 위험", "안전모·안전화·장갑 착용을 작업 전에 서로 확인한다"),
    ("작업 순서를 공유하지 않아 함께 작업하다 부딪힐 위험", "작업 순서와 담당을 TBM 에서 정하고 바뀌면 다시 알린다"),
)

# 점검 코드 → (관련 태그가 있을 때 조치, 없을 때 조치). HEALTH 는 태그와 무관하다.
CHECK_ACTIONS: dict[str, tuple[str, str]] = {
    "PPE": ("안전모 턱끈 체결 등 보호구 착용 상태를 작업 전 서로 확인한다",
            "안전모·안전화 등 보호구 착용을 작업 전 서로 확인한다"),
    "ZONE_CONTROL": ("{where} 위험구역 경계 표시와 출입 통제 상태를 확인한다",
                     "작업 구간의 위험구역 표시와 출입 통제 상태를 확인한다"),
    "OPENING_GUARD": ("{where} 개구부 덮개 고정과 안전난간 설치를 확인한다",
                      "작업 구간에 개구부가 있는지 확인하고 있으면 덮개·난간을 설치한다"),
    "WORK_SURFACE_FALL": ("{where} 작업발판 고정·난간·안전대 걸이 설비를 확인한다",
                          "높은 곳 작업이 있으면 작업발판과 추락 방호 설비를 확인한다"),
    "HEALTH": ("작업 전 작업자 컨디션과 음주·피로 여부를 확인한다",
               "작업 전 작업자 컨디션과 음주·피로 여부를 확인한다"),
}

# 구역 유형 → 관련 규칙 태그(구역의 rule_id 가 없을 때 장소 이름을 붙일 곳을 고른다)
ZONE_KIND_TAGS: dict[str, tuple[str, ...]] = {
    "OPENING": ("R1", "R2", "R5"),
    "FLOOR_EDGE": ("R2",),
    "ELEVATED_WORK_SURFACE": ("R3", "R4"),
    "SCAFFOLD": ("R3", "R4"),
    "EQUIPMENT_PARKING": ("R8",),
    "LIFTING": ("R1", "R8"),
}

DEFAULT_WHERE = "작업 구간"
