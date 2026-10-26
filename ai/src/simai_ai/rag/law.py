"""산업안전보건기준에 관한 규칙 원문 수집·정규화 (국가법령정보 공동활용 Open API).

- 판은 법령일련번호(MST)로 고정한다. 같은 MST 는 언제 받아도 같은 판이라 다시 받아 대조할 수 있다.
- 원 응답 전체(약 1.3 MB)는 커밋하지 않고, 고른 조문만 정규화해 `data/clauses.json` 에 둔다. 응답 sha256 을 함께 적는다.
- 인증값 OC 는 `.env` 의 SIMAI_LAW_API_OC 다(open.law.go.kr 에 등록한 서버 IP 에서만 응답한다).
- 법령 본문은 저작권법 제7조 제1호로 보호받지 못하는 저작물이라 저장소에 넣어도 된다.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from typing import Any

API = "https://www.law.go.kr/DRF"
OC_ENV = "SIMAI_LAW_API_OC"
LAW_ID = "007363"
LAW_NAME = "산업안전보건기준에 관한 규칙"
MST = "273603"  # 2025-09-01 시행(고용노동부령 제450호). 바꾸면 조항 파일·임베딩·시드를 다시 만든다

# 처음 넣는 30개 조(결정: .handoff/27-decision.md B, 근거 표는 docs/design/rag-clause-index.md). 가지 조문은 "66-2" 형식
ARTICLES = (
    "3", "9", "13", "14", "20", "22", "23", "24", "30", "32", "35", "36", "37", "38", "42",
    "43", "44", "45", "46", "48", "56", "57", "63", "67", "68", "86", "171", "172", "186", "187",
    "624",
)
_TIMEOUT_S = 60


def _get(path: str, params: dict[str, str]) -> bytes:
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:  # noqa: S310 (https 고정 주소)
        return resp.read()


def _oc(oc: str | None) -> str:
    oc = oc or os.environ.get(OC_ENV)
    if not oc:
        raise SystemExit(f"{OC_ENV} 가 없다(.env). uv run --env-file ../.env … 로 실행한다")
    return oc


def _json(raw: bytes, what: str) -> dict[str, Any]:
    doc = json.loads(raw)
    if "result" in doc and "msg" in doc:  # 인증 실패 등 오류 응답
        raise SystemExit(f"{what}: {doc['result']} {doc['msg']}")
    return doc


def fetch(oc: str | None = None, mst: str = MST) -> bytes:
    """MST 판의 법령 본문 JSON 응답 그대로."""
    raw = _get("lawService.do", {"OC": _oc(oc), "target": "law", "MST": mst, "type": "JSON"})
    _json(raw, "lawService")
    return raw


def current_mst(oc: str | None = None) -> str:
    """현행 판의 MST. 고정 MST 와 다르면 개정된 것이다."""
    doc = _json(_get("lawSearch.do", {"OC": _oc(oc), "target": "law", "type": "JSON", "query": LAW_NAME}), "lawSearch")
    laws = doc["LawSearch"]["law"]
    laws = laws if isinstance(laws, list) else [laws]
    hits = [x for x in laws if x["법령ID"] == LAW_ID and x["현행연혁코드"] == "현행"]
    if len(hits) != 1:
        raise SystemExit(f"현행 {LAW_NAME} 를 하나로 찾지 못했다: {len(hits)}건")
    return str(hits[0]["법령일련번호"])


def _list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _text(v: Any) -> list[str]:
    """API 는 한 줄 문자열이나 줄 목록을 준다. 각 줄 앞뒤 공백을 걷는다."""
    return [line.strip() for line in _list(v) if isinstance(line, str) and line.strip()]


def _units(article: dict[str, Any]) -> list[str]:
    """조문의 나눔 단위: 항이 있으면 항(그 호·목 포함), 없으면 호(그 목 포함)."""
    units: list[str] = []
    for hang in _list(article.get("항")):
        lines = _text(hang.get("항내용"))
        hos = _list(hang.get("호"))
        if lines:  # 항 하나 = 한 단위
            for ho in hos:
                lines += _text(ho.get("호내용"))
                for mok in _list(ho.get("목")):
                    lines += _text(mok.get("목내용"))
            units.append("\n".join(lines))
        else:  # 항 번호 없이 호만 있는 조문: 호 하나 = 한 단위
            for ho in hos:
                ho_lines = _text(ho.get("호내용"))
                for mok in _list(ho.get("목")):
                    ho_lines += _text(mok.get("목내용"))
                units.append("\n".join(ho_lines))
    return units


def article_key(article: dict[str, Any]) -> str:
    branch = article.get("조문가지번호") or ""
    return article["조문번호"] + (f"-{branch}" if branch else "")


def normalize(raw: bytes, articles: tuple[str, ...] = ARTICLES) -> dict[str, Any]:
    """lawService JSON 응답 → 조항 파일 내용. 고른 조문을 조문 순서로, 원문 줄 그대로(개정 꼬리표 포함) 담는다."""
    law = json.loads(raw)["법령"]
    info = law["기본정보"]
    if info["법령ID"] != LAW_ID:
        raise ValueError(f"다른 법령이다: {info['법령ID']}")
    found = {}
    for unit in law["조문"]["조문단위"]:
        if unit.get("조문여부") == "조문" and article_key(unit) in articles:
            found[article_key(unit)] = unit
    missing = [a for a in articles if a not in found]
    if missing:
        raise ValueError(f"조문이 없다: {missing}")
    out = []
    for key, unit in found.items():
        number, _, branch = key.partition("-")
        out.append({
            "article": f"제{number}조" + (f"의{branch}" if branch else ""),
            "title": unit.get("조문제목") or None,
            "effective": unit["조문시행일자"],
            "head": " ".join(_text(unit.get("조문내용"))),
            "units": _units(unit),
        })
    return {
        "law_id": LAW_ID,
        "law_name": info["법령명_한글"],
        "mst": MST,
        "promulgation_date": info["공포일자"],
        "promulgation_no": info["공포번호"],
        "effective_date": info["시행일자"],
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "articles": out,
    }
