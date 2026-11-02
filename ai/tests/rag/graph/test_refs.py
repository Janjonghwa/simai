"""조문 참조 결정적 정규식 추출기 시험 (S15P31A101-416, ai/src/simai_ai/rag/graph/refs.py)."""

from __future__ import annotations

from simai_ai.rag.graph import catalog
from simai_ai.rag.graph.refs import Context, Loc, abbreviations, extract, mask


def _ctx(law_key: str = "OSHA_STD", article: tuple[int, int] = (42, 0), paragraph: int | None = 1, extra_abbr: dict[str, str] | None = None) -> Context:
    spec = catalog.spec(law_key)
    abbr = dict(spec.default_abbr)
    if extra_abbr:
        abbr.update(extra_abbr)
    return Context(spec.name, abbr, article=article, paragraph=paragraph)


def test_internal_locations_and_spacing():
    ctx = _ctx()
    refs = extract("제13조, 제38조의2제1항제2호의3가목, 제42조 제2항 및 별표 3의2에 따른 기준", ctx)
    assert [(r.law, r.loc.label(), r.form, r.cue) for r in refs] == [
        (catalog.OSHA_STD, "제13조", "explicit", "따른"),
        (catalog.OSHA_STD, "제38조의2제1항제2호의3가목", "inherited", "따른"),
        (catalog.OSHA_STD, "제42조제2항", "inherited", "따른"),
        (catalog.OSHA_STD, "별표3의2", "inherited", "따른"),
    ]


def test_relative_locations_and_same_anaphora():
    ctx = _ctx(article=(42, 0), paragraph=2)
    refs = extract("전항 또는 제3항제1호에 따른 조치와 법 제58조제2항제2호, 같은 조 제5항, 같은 항 제1호에 따른 승인", ctx)
    assert [(r.law, r.loc.label(), r.form, r.cue) for r in refs] == [
        (catalog.OSHA_STD, "제42조제1항", "relative", "따른"),
        (catalog.OSHA_STD, "제42조제3항제1호", "inherited", "따른"),
        (catalog.OSHA, "제58조제2항제2호", "explicit", "따른"),
        (catalog.OSHA, "제58조제5항", "relative", "따른"),
        (catalog.OSHA, "제58조제5항제1호", "relative", "따른"),
    ]


def test_cross_law_brackets_abbreviations_and_same_law_suffix():
    text = (
        "「산업안전보건법」(이하 “법”이라 한다) 제38조제1항, 영 제40조, "
        "「액화석유가스의 안전관리 및 사업법」 제5조 및 같은 법 시행령 제3조, 「건축법」에 따른다."
    )
    abbr = abbreviations(text, catalog.spec("OSHA_STD").default_abbr)
    ctx = Context(catalog.OSHA_STD, abbr, article=(10, 0), paragraph=1)
    refs = extract(text, ctx)
    assert [(r.law, r.loc.label(), r.form) for r in refs] == [
        ("산업안전보건법", "제38조제1항", "explicit"),
        ("산업안전보건법 시행령", "제40조", "explicit"),
        ("액화석유가스의 안전관리 및 사업법", "제5조", "explicit"),
        ("액화석유가스의 안전관리 및 사업법 시행령", "제3조", "explicit"),
        ("건축법", "", "law"),
    ]


def test_named_abbreviation_extracted_from_body():
    body = "「산업안전보건기준에 관한 규칙」(이하 “안전보건규칙”이라 한다) 제42조에 따른다."
    abbr = abbreviations(body, catalog.spec("OSHA_RULE").default_abbr)
    assert abbr["안전보건규칙"] == catalog.OSHA_STD
    ctx = Context(catalog.OSHA_RULE, abbr, article=(75, 0), paragraph=2)
    refs = extract("안전보건규칙 제5조 및 제7조부터 제9조까지의 규정에 따른 조치", ctx)
    assert [(r.law, r.loc.label(), r.form, r.cue) for r in refs] == [
        (catalog.OSHA_STD, "제5조", "explicit", "따른"),
        (catalog.OSHA_STD, "제7조", "inherited", "따른"),
        (catalog.OSHA_STD, "제8조", "range", "따른"),
        (catalog.OSHA_STD, "제9조", "inherited", "따른"),
    ]


def test_chained_inheritance_and_appendix_of_article():
    ctx = _ctx(law_key="OSHA_STD", article=(339, 0), paragraph=1)
    refs = extract("영 제42조제3항제1호 또는 제2호에 따른 물질과 제338조제1항의 별표 11을 준용한다", ctx)
    assert [(r.law, r.loc.label(), r.form, r.cue) for r in refs] == [
        (catalog.OSHA_DECREE, "제42조제3항제1호", "explicit", "따른"),
        (catalog.OSHA_DECREE, "제42조제3항제2호", "inherited", "따른"),
        (catalog.OSHA_STD, "제338조제1항", "explicit", None),
        (catalog.OSHA_STD, "별표11", "inherited", "준용"),
    ]


def test_cues_override_mutatis_and_follow():
    ctx = _ctx()
    r1 = extract("제10조 및 제11조에도 불구하고 달리 정한다.", ctx)
    r2 = extract("제12조제1항부터 제3항까지의 규정을 각각 준용한다.", ctx)
    assert [r.cue for r in r1] == ["불구하고", "불구하고"]
    assert [(r.loc.label(), r.cue) for r in r2] == [
        ("제12조제1항", "준용"),
        ("제12조제2항", "준용"),
        ("제12조제3항", "준용"),
    ]


def test_false_positives_masked_or_ignored():
    ctx = Context(catalog.OSHA_STD, {}, article=(42, 0), paragraph=1)
    text = (
        "고용노동부령 제450호 및 법률 제17907호, 별지 제4호서식, 제3자, 제1종, 제2편 제1장, "
        "같은 조치를 취하고 이 조항은 건축법 제2조와 법 제99조(정의 없음), 같은 법 제3조, 같은 조 제1항 "
        "<개정 2021. 11. 19.> [본조신설 2019. 12. 26.] [제610조에서 이동 <2020. 1. 16.>]"
    )
    assert "<개정" not in mask(text)
    assert "[제610조에서 이동" not in mask(text)
    assert extract(text, ctx) == []


def test_normal_words_ending_in_beop_or_ryeong_do_not_block_location():
    ctx = _ctx(article=(42, 0), paragraph=1)
    refs = extract("방법 제1호와 요령 제2항에 따른다.", ctx)
    assert [r.loc for r in refs] == [
        Loc(article=(42, 0), paragraph=1, item=(1, 0)),
        Loc(article=(42, 0), paragraph=2),
    ]
