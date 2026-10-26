"""조항 분할·조항 ID·임베딩 입력 (S15P31A101-27, docs/design/rag-clause-index.md)."""

from collections import Counter, defaultdict

from simai_ai.rag.chunk import CLAUSE_ID_RE, LIMIT_CHARS, chunk_article, embedding_text, strip_amendment_tags
from simai_ai.rag.seed import load_chunks

LAW = "산업안전보건기준에 관한 규칙"


def _article(units, head="제42조(추락의 방지)", article="제42조"):
    return {"article": article, "title": "추락의 방지", "effective": "20250901", "head": head, "units": units}


def test_short_article_is_one_chunk_without_suffix():
    [c] = chunk_article(_article(["① 가", "② 나"]), law_name=LAW, source_version="2025-09-01")
    assert c.clause_id == "산업안전보건기준규칙 제42조"
    assert c.chunk_index == 0
    assert c.body == "제42조(추락의 방지)\n① 가\n② 나"


def test_long_article_packs_units_and_suffixes_every_chunk():
    units = ["① " + "가" * 400, "② " + "나" * 400, "③ " + "다" * 400, "④ " + "라" * 50]
    cs = chunk_article(_article(units), law_name=LAW, source_version="2025-09-01")
    assert [c.clause_id for c in cs] == [f"산업안전보건기준규칙 제42조#{i}" for i in range(len(cs))]
    assert [c.chunk_index for c in cs] == list(range(len(cs)))
    assert all(c.body.startswith("제42조(추락의 방지)\n") for c in cs)
    assert all(len(c.body) <= LIMIT_CHARS for c in cs)
    # 단위를 중간에서 끊지 않고 순서대로 모두 담는다
    assert [u for c in cs for u in c.body.split("\n")[1:]] == units
    assert len(cs) == 2  # ①② 한 청크(약 810자), ③④ 한 청크


def test_unit_longer_than_limit_stays_whole():
    units = ["① " + "가" * (LIMIT_CHARS + 50), "② 나"]
    cs = chunk_article(_article(units), law_name=LAW, source_version="2025-09-01")
    assert [c.body.split("\n")[1:] for c in cs] == [[units[0]], [units[1]]]


def test_ho_only_article_repeats_intro_head():
    head = "제20조(출입의 금지 등) 사업주는 다음 각 호의 작업 또는 장소에 … 출입을 금지해야 한다."
    units = [f"{i}. " + "가" * 300 for i in range(1, 5)]
    cs = chunk_article(_article(units, head=head, article="제20조"), law_name=LAW, source_version="2025-09-01")
    assert len(cs) > 1 and all(c.body.startswith(head + "\n") for c in cs)


def test_branch_article_id():
    [c] = chunk_article(_article(["① 가"], head="제66조의2(걸침비계의 구조)", article="제66조의2"), law_name=LAW, source_version="x")
    assert c.clause_id == "산업안전보건기준규칙 제66조의2"
    assert CLAUSE_ID_RE.match(c.clause_id)


def test_amendment_tags_removed_only_from_embedding_input():
    body = (
        "제42조(추락의 방지)\n② 사업주는 … 해야 한다. <개정 2017.12.28, 2021.5.28>\n"
        "③ … 사용하여야 한다. <신설 2017.12.28, 2022.10.18> [본조신설 2012.5.31]\n"
        "① … 장소[작업발판의 끝ㆍ개구부(開口部) 등을 제외한다]"
    )
    assert strip_amendment_tags(body) == (
        "제42조(추락의 방지)\n② 사업주는 … 해야 한다.\n③ … 사용하여야 한다.\n① … 장소[작업발판의 끝ㆍ개구부(開口部) 등을 제외한다]"
    )
    assert embedding_text(LAW, body).startswith(LAW + " 제42조(추락의 방지)\n")
    assert "<개정" not in embedding_text(LAW, body)


def test_committed_clauses_ids_are_valid_and_unique():
    cs = load_chunks()
    ids = [c.clause_id for c in cs]
    assert all(CLAUSE_ID_RE.match(i) for i in ids), [i for i in ids if not CLAUSE_ID_RE.match(i)]
    assert not [i for i, n in Counter(ids).items() if n > 1]
    by_article = defaultdict(list)
    for c in cs:
        by_article[c.article].append(c)
    for article, group in by_article.items():
        assert [c.chunk_index for c in group] == list(range(len(group))), article
        suffixed = ["#" in c.clause_id for c in group]
        assert suffixed == [len(group) > 1] * len(group), article
        assert all(c.clause_id.split("#")[0] == f"산업안전보건기준규칙 {article}" for c in group)


def test_committed_clauses_include_r3_basis():
    ids = {c.clause_id for c in load_chunks()}
    assert {"산업안전보건기준규칙 제32조", "산업안전보건기준규칙 제44조"} <= ids
    body = {c.clause_id: c.body for c in load_chunks()}
    assert "높이 또는 깊이 2미터 이상의 추락할 위험이 있는 장소에서 하는 작업: 안전대" in body["산업안전보건기준규칙 제32조"]
    assert "추락할 위험이 있는 높이 2미터 이상의 장소" in body["산업안전보건기준규칙 제44조"]
