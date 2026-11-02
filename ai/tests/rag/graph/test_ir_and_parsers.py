"""중간 표현(ir) 및 Open API·웹 본문 파서 시험 (S15P31A101-416)."""

from __future__ import annotations

import json
import pytest

from simai_ai.rag.graph import catalog, ir, source_api, source_web


def test_article_builder_classifies_paragraphs_items_subitems_and_continuations():
    b = ir.ArticleBuilder("제42조(추락의 방지)", chapter="제1편 총칙 > 제6장 추락 방지", effective="20260302")
    b.add_line("① 사업주는 추락 위험이 있는 장소에 작업발판을 설치하여야 한다.")
    b.add_line("② 제1항에 따른 작업발판이 곤란한 경우 다음 각 호의 기준에 맞는 추락방호망을 설치해야 한다.")
    b.add_line("1. 추락방호망의 설치위치는 가능하면 작업면으로부터 가까운 지점에 설치할 것")
    b.add_line("1의2. 수직보호망은 견고하게 고정할 것")
    b.add_line("가. 그물코의 크기는 10센티미터 이하일 것")
    b.add_line("1) 신품일 것")
    b.add_line("나. 시험성적서를 확인할 것")
    art = b.finish()

    assert (art.number, art.branch, art.label, art.title, art.deleted) == (42, 0, "제42조", "추락의 방지", False)
    assert len(art.paragraphs) == 2
    assert len(art.paragraphs[1].items) == 2
    it1, it2 = art.paragraphs[1].items
    assert (it1.number, it1.branch) == (1, 0)
    assert (it2.number, it2.branch) == (1, 2)
    assert [s.number for s in it2.subitems] == ["가", "나"]
    assert it2.subitems[0].text == "가. 그물코의 크기는 10센티미터 이하일 것\n1) 신품일 것"

    roundtrip = ir.LawDoc.from_json(
        ir.LawDoc(
            law_key="OSHA_STD", law_id="007363", law_name=catalog.OSHA_STD, mst="273603", law_kind="고용노동부령",
            promulgation_date="20250901", promulgation_no="00450", effective_date="20260302",
            source="api-xml", source_url="https://example.test", raw_sha256="00" * 32, fetched_at="2026-11-02T00:00:00+00:00",
            articles=[art], appendices=[ir.Appendix(1, 0, "별표 제목", "본문")],
        ).to_json()
    )
    assert roundtrip.articles[0] == art


def test_deleted_article_and_ho_only_article():
    deleted = ir.ArticleBuilder("제15조")
    deleted.add_line("삭제 <2020. 1. 16.>")
    assert deleted.finish().deleted is True

    ho_only = ir.ArticleBuilder("제13조(안전난간의 구조 및 설치요건)")
    ho_only.add_line("사업주는 다음 각 호의 기준에 맞는 구조로 설치해야 한다.")
    ho_only.add_line("1. 상부 난간대")
    ho_only.add_line("2. 발끝막이판")
    art = ho_only.finish()
    assert art.body == "사업주는 다음 각 호의 기준에 맞는 구조로 설치해야 한다."
    assert not art.paragraphs and [i.number for i in art.items] == [1, 2]


def test_api_xml_json_and_web_html_produce_same_articles():
    spec = catalog.spec("OSHA_STD")
    xml_bytes = """<?xml version="1.0" encoding="UTF-8"?>
    <법령>
      <기본정보>
        <법령ID>007363</법령ID>
        <법령일련번호>273603</법령일련번호>
        <법령명_한글>산업안전보건기준에 관한 규칙</법령명_한글>
        <법종구분>고용노동부령</법종구분>
        <공포일자>20250901</공포일자>
        <공포번호>00450</공포번호>
        <시행일자>20260302</시행일자>
      </기본정보>
      <조문>
        <조문단위>
          <조문여부>전문</조문여부>
          <조문내용>제1편 총칙</조문내용>
        </조문단위>
        <조문단위>
          <조문여부>전문</조문여부>
          <조문내용>제6장 추락 또는 붕괴에 의한 위험 방지</조문내용>
        </조문단위>
        <조문단위>
          <조문여부>조문</조문여부>
          <조문시행일자>20260302</조문시행일자>
          <조문내용>제13조(안전난간의 구조) 사업주는 다음 각 호의 기준에 따라 설치해야 한다.</조문내용>
          <항>
            <호><호내용>1. 상부 난간대</호내용></호>
            <호><호내용>2. 발끝막이판</호내용></호>
          </항>
        </조문단위>
        <조문단위>
          <조문여부>조문</조문여부>
          <조문시행일자>20260302</조문시행일자>
          <조문내용>제42조(추락의 방지)</조문내용>
          <항>
            <항내용>① 사업주는 작업발판을 설치하여야 한다.</항내용>
          </항>
          <항>
            <항내용>② 사업주는 다음 각 호의 기준에 맞는 추락방호망을 설치하여야 한다.</항내용>
            <호>
              <호내용>1. 작업면으로부터 가까운 지점에 설치할 것</호내용>
              <목><목내용>가. 수평으로 설치</목내용></목>
            </호>
          </항>
        </조문단위>
      </조문>
      <별표>
        <별표단위>
          <별표구분>별표</별표구분>
          <별표번호>0001</별표번호>
          <별표가지번호>00</별표가지번호>
          <별표제목>관리대상 유해물질의 종류(제420조 관련)</별표제목>
          <별표내용>1. 유기화합물</별표내용>
        </별표단위>
      </별표>
    </법령>
    """.encode("utf-8")

    json_bytes = json.dumps({
        "법령": source_api.xml_to_dict(source_api.parse_xml_root(xml_bytes))
    }, ensure_ascii=False).encode("utf-8")

    web_html = """
    <html><body>
      <div>[시행 2026. 3. 2.] [고용노동부령 제450호, 2025. 9. 1., 일부개정]</div>
      <input type="hidden" id="lsId" value="007363" />
      <input type="hidden" id="lsNm" value="산업안전보건기준에 관한 규칙" />
      <input type="hidden" id="lsiSeq" value="273603" />
      <input type="hidden" id="ancYd" value="20250901" />
      <input type="hidden" id="ancNo" value="00450" />
      <input type="hidden" id="efYd" value="20260302" />
      <p class="gtit">제1편 총칙</p>
      <p class="gtit">제6장 추락 또는 붕괴에 의한 위험 방지</p>
      <p class="pty1_p4"><label>제13조(안전난간의 구조)</label> 사업주는 다음 각 호의 기준에 따라 설치해야 한다.</p>
      <p class="pty1_de2h">1. 상부 난간대</p>
      <p class="pty1_de2h">2. 발끝막이판</p>
      <p class="pty1_p4"><label>제42조(추락의 방지)</label> ① 사업주는 작업발판을 설치하여야 한다.</p>
      <p class="pty1_de2_1">② 사업주는 다음 각 호의 기준에 맞는 추락방호망을 설치하여야 한다.</p>
      <p class="pty1_de2h">1. 작업면으로부터 가까운 지점에 설치할 것</p>
      <p class="pty1_de3">가. 수평으로 설치</p>
      <p class="pty1_de2">[전문개정 2020. 1. 16.]</p>
      <a class="blu">[별표 1] 관리대상 유해물질의 종류(제420조 관련)</a>
      <p class="pty3">부칙</p>
      <p class="pty1_p4"><label>제1조(시행일)</label> 이 규칙은 공포한 날부터 시행한다.</p>
    </body></html>
    """.encode("utf-8")

    d_xml = source_api.parse_law_xml(xml_bytes, spec, source_url="https://example.test/xml")
    d_json = source_api.parse_law_json(json_bytes, spec, source_url="https://example.test/json")
    d_web = source_web.parse(web_html, spec, source_url="https://example.test/web")

    # 조문 시행일자(effective)는 API 전용이므로 그 외 모든 구조·본문·장절이 일치해야 한다
    for a_xml, a_json, a_web in zip(d_xml.articles, d_json.articles, d_web.articles, strict=True):
        assert a_xml == a_json
        assert (a_xml.number, a_xml.branch, a_xml.title, a_xml.body, a_xml.paragraphs, a_xml.items, a_xml.chapter) == (
            a_web.number, a_web.branch, a_web.title, a_web.body, a_web.paragraphs, a_web.items, a_web.chapter
        )
    assert [(x.label, x.title) for x in d_xml.appendices] == [(x.label, x.title) for x in d_web.appendices]


def test_admrul_xml_and_related_and_secret_redaction():
    spec = catalog.spec("STD_FALL")
    admrul_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <행정규칙>
      <행정규칙기본정보>
        <행정규칙ID>34901</행정규칙ID>
        <행정규칙일련번호>2100000186039</행정규칙일련번호>
        <행정규칙명>추락재해방지표준안전작업지침</행정규칙명>
        <행정규칙종류>고시</행정규칙종류>
        <발령일자>20200107</발령일자>
        <발령번호>2020-8</발령번호>
        <시행일자>20200116</시행일자>
      </행정규칙기본정보>
      <조문내용>제1장 총칙</조문내용>
      <조문내용>제1조(목적) 이 지침은 「산업안전보건법」 제27조에 따라 추락재해 방지에 필요한 사항을 정함을 목적으로 한다.</조문내용>
    </행정규칙>
    """.encode("utf-8")
    doc = source_api.parse_admrul_xml(admrul_xml, spec, source_url="https://example.test/adm")
    assert doc.law_id == "34901" and len(doc.articles) == 1
    assert doc.articles[0].chapter == "제1장 총칙"

    secret = "my-secret-oc-value"
    assert secret not in source_api.redact(f"https://www.law.go.kr/DRF/lawService.do?OC={secret}&target=eflaw", secret)
    assert secret.encode() not in source_api.scrub(f"<link>/DRF/lawSearch.do?OC={secret}</link>".encode(), secret)

    err_xml = b"<?xml version='1.0'?><Response><result>fail</result><msg>IP mismatch</msg></Response>"
    with pytest.raises(source_api.LawApiError, match="fail IP mismatch"):
        source_api.check_error(err_xml)
