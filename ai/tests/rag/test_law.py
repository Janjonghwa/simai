"""법령 Open API 응답 정규화 (S15P31A101-27)."""

import hashlib
import json

import pytest

from simai_ai.rag import law
from simai_ai.rag.seed import CLAUSES


def _response(units, law_id=law.LAW_ID):
    doc = {
        "법령": {
            "기본정보": {"법령ID": law_id, "법령명_한글": law.LAW_NAME, "공포일자": "20250901", "공포번호": "00450", "시행일자": "20250901"},
            "조문": {"조문단위": units},
        }
    }
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


ARTICLE_WITH_HANG = {
    "조문여부": "조문", "조문번호": "42", "조문제목": "추락의 방지", "조문시행일자": "20250901",
    "조문내용": "제42조(추락의 방지)",
    "항": [
        {"항내용": "① 사업주는 … 설치하여야 한다."},
        {"항내용": "  ② 사업주는 … 조치를 해야 한다. <개정 2017.12.28>", "호": [
            {"호내용": "1. 가"},
            {"호내용": "2. 나", "목": [{"목내용": "가. ㄱ"}, {"목내용": ["나. ㄴ", " "]}]},
        ]},
    ],
}
ARTICLE_HO_ONLY = {
    "조문여부": "조문", "조문번호": "13", "조문제목": "안전난간", "조문시행일자": "20250901",
    "조문내용": "제13조(안전난간) 사업주는 … 설치해야 한다.",
    "항": {"호": [{"호내용": "1. 상부 난간대"}, {"호내용": "2. 발끝막이판"}]},
}
BRANCH = {"조문여부": "조문", "조문번호": "66", "조문가지번호": "2", "조문제목": "걸침비계의 구조", "조문시행일자": "20250901",
          "조문내용": "제66조의2(걸침비계의 구조) 사업주는 …"}
CHAPTER = {"조문여부": "전문", "조문번호": "42", "조문내용": "제6장 추락 또는 붕괴에 의한 위험 방지"}


def test_normalize_keeps_units_and_original_text():
    raw = _response([CHAPTER, ARTICLE_HO_ONLY, ARTICLE_WITH_HANG, BRANCH])
    doc = law.normalize(raw, articles=("13", "42", "66-2"))
    assert doc["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (doc["mst"], doc["effective_date"], doc["promulgation_no"]) == (law.MST, "20250901", "00450")
    a13, a42, a66 = doc["articles"]
    assert a42 == {
        "article": "제42조", "title": "추락의 방지", "effective": "20250901", "head": "제42조(추락의 방지)",
        "units": ["① 사업주는 … 설치하여야 한다.", "② 사업주는 … 조치를 해야 한다. <개정 2017.12.28>\n1. 가\n2. 나\n가. ㄱ\n나. ㄴ"],
    }
    assert a13["units"] == ["1. 상부 난간대", "2. 발끝막이판"]  # 항 번호 없는 조문은 호가 단위
    assert (a66["article"], a66["units"]) == ("제66조의2", [])


def test_normalize_rejects_missing_article_and_other_law():
    with pytest.raises(ValueError, match="조문이 없다"):
        law.normalize(_response([ARTICLE_WITH_HANG]), articles=("42", "43"))
    with pytest.raises(ValueError, match="다른 법령"):
        law.normalize(_response([ARTICLE_WITH_HANG], law_id="000001"), articles=("42",))


def test_committed_clause_file_matches_pinned_edition():
    doc = json.loads(CLAUSES.read_text(encoding="utf-8"))
    assert (doc["law_id"], doc["mst"], doc["law_name"]) == (law.LAW_ID, law.MST, law.LAW_NAME)
    numbers = [a["article"] for a in doc["articles"]]
    expected = [f"제{n.split('-')[0]}조" + (f"의{n.split('-')[1]}" if "-" in n else "") for n in law.ARTICLES]
    assert sorted(numbers) == sorted(expected) and len(numbers) == len(set(numbers))
    assert all(a["units"] or a["head"] for a in doc["articles"])
