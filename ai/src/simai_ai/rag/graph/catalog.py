"""대상 법령 목록과 위임 관계.

노드 ID 는 `{short} {조문 위치}` 다(예: `산업안전보건기준규칙 제42조제2항제1호`, `산업안전보건기준규칙 별표1`). 조 단위 ID 는
기존 `rag_clause.clause_id`(`산업안전보건기준규칙 제N조`)와 같은 꼴이라 두 색인을 같은 키로 잇는다.

위임: 하위 법령 조문이 상위 법령 조문을 가리키고(예: "법 제38조제1항에 따른"), 그 상위 조문에 하위 법령으로 넘기는 문구
(`delegation_cue`, 예: "대통령령")가 있으면 상위 조 → 하위 조 위임 엣지를 만든다(build.py).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LawSpec:
    key: str
    name: str  # 정식 이름(「」 안에 쓰는 이름)
    short: str  # 노드 ID 접두사
    level: str  # LAW(법률) | DECREE(대통령령) | MINISTRY_RULE(부령) | ADMIN_RULE(고시 등 행정규칙)
    parents: tuple[str, ...] = ()  # 위임 상위 법령 key
    delegation_cue: str | None = None  # 상위 조문에서 이 법령으로 넘기는 문구
    default_abbr: tuple[tuple[str, str], ...] = ()  # 본문에 "(이하 “법”이라 한다)" 정의가 없을 때의 약칭 → 법령 이름


OSHA = "산업안전보건법"
OSHA_DECREE = "산업안전보건법 시행령"
OSHA_RULE = "산업안전보건법 시행규칙"
OSHA_STD = "산업안전보건기준에 관한 규칙"
SAPA = "중대재해 처벌 등에 관한 법률"
SAPA_DECREE = "중대재해 처벌 등에 관한 법률 시행령"

_OSHA_ABBR = (("법", OSHA), ("영", OSHA_DECREE), ("규칙", OSHA_RULE))
_OSHA_PARENTS = ("OSHA", "OSHA_DECREE", "OSHA_RULE", "OSHA_STD")

LAWS: tuple[LawSpec, ...] = (
    LawSpec("OSHA", OSHA, "산업안전보건법", "LAW"),
    LawSpec("OSHA_DECREE", OSHA_DECREE, "산업안전보건법시행령", "DECREE", ("OSHA",), "대통령령", _OSHA_ABBR[:1]),
    LawSpec("OSHA_RULE", OSHA_RULE, "산업안전보건법시행규칙", "MINISTRY_RULE", ("OSHA", "OSHA_DECREE"), "고용노동부령", _OSHA_ABBR[:2]),
    LawSpec("OSHA_STD", OSHA_STD, "산업안전보건기준규칙", "MINISTRY_RULE", ("OSHA", "OSHA_DECREE"), "고용노동부령", _OSHA_ABBR),
    LawSpec("SAPA", SAPA, "중대재해처벌법", "LAW"),
    LawSpec("SAPA_DECREE", SAPA_DECREE, "중대재해처벌법시행령", "DECREE", ("SAPA",), "대통령령", (("법", SAPA),)),
    # 고용노동부 고시(행정규칙 API). 위임은 고시의 목적 조(제1조)가 가리키는 상위 조문에서 고시 전체로 잇는다(build.py)
    LawSpec("STD_TEMP_WORKS", "가설공사 표준안전 작업지침", "가설공사표준안전작업지침", "ADMIN_RULE", _OSHA_PARENTS, "고시"),
    LawSpec("STD_FALL", "추락재해방지표준안전작업지침", "추락재해방지표준안전작업지침", "ADMIN_RULE", _OSHA_PARENTS, "고시"),
    LawSpec("STD_EXCAVATION", "굴착공사 표준안전 작업지침", "굴착공사표준안전작업지침", "ADMIN_RULE", _OSHA_PARENTS, "고시"),
    LawSpec("RISK_ASSESSMENT", "사업장 위험성평가에 관한 지침", "사업장위험성평가지침", "ADMIN_RULE", _OSHA_PARENTS, "고시"),
)
BY_KEY = {s.key: s for s in LAWS}
BY_NAME = {s.name: s for s in LAWS}


def spec(key: str) -> LawSpec:
    return BY_KEY[key]


def by_name(name: str) -> LawSpec | None:
    """「」 안 이름 → 대상 법령. 띄어쓰기 차이는 무시한다(「산업안전보건법시행령」 같은 표기)."""
    hit = BY_NAME.get(name)
    if hit:
        return hit
    squeezed = name.replace(" ", "")
    return next((s for s in LAWS if s.name.replace(" ", "") == squeezed), None)
