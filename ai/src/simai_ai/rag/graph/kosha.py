"""2단계 원천(KOSHA GUIDE·재해사례) 수집기 틀. 내용은 아직 넣지 않는다.

이용 조건 확인 결과(2026-11-02, 공공데이터포털 각 데이터 상세 "이용허락범위"):

| 데이터 | 주소 | 이용허락범위 | 판단 |
|---|---|---|---|
| 한국산업안전보건공단_기술지원규정(코샤가이드) 조회 서비스(API) | https://www.data.go.kr/data/15144147/openapi.do | 제한 없음 | 규정명·번호·공표일·다운로드 링크(메타데이터)만 준다. 메타데이터 노드(GUIDE)는 넣을 수 있다 |
| 한국산업안전보건공단_안전보건기술지침(KOSHA Guide) 목록(파일) | https://www.data.go.kr/data/15116595/fileData.do | 공공누리 제3유형(출처표시, 변경금지) | 목록 파일. 요약·분할은 변경에 해당할 수 있어 원문 그대로만 |
| KOSHA GUIDE 본문(PDF, 산업안전포털) | https://portal.kosha.or.kr | 확인 못 함 | 포털 저작권정책을 찾지 못했다. 본문·요약은 넣지 않는다 |
| 한국산업안전보건공단_국내재해사례 게시판 정보 조회서비스(API) | https://www.data.go.kr/data/15121001/openapi.do | 제한 없음 | 산업안전포털 국내재해사례 게시판(업종별, 등록일 역순). 요청: ServiceKey·business·keyword·callApiId·pageNo·numOfRows, JSON |
| 한국산업안전보건공단_국내재해사례 게시판 첨부파일 정보 조회서비스(API) | https://www.data.go.kr/data/15121008/openapi.do | 제한 없음 | 첨부파일 목록 |
| 한국산업안전보건공단_산업재해 고위험요인(SIF) 아카이브(파일) | https://www.data.go.kr/data/15140383/fileData.do | 공공누리 제3유형(출처표시, 변경금지) | 요약 노드는 변경 우려. 넣지 않는다 |

공공데이터포털 API 는 포털 인증키(serviceKey)가 필요하다(법령 OC 와 다르다, 아직 없다). 키는 환경변수 `DATA_GO_KR_SERVICE_KEY`
로만 받는다. 재해사례는 "제한 없음"이지만 사례 요약을 만들면 사람 이름·사업장 등이 섞이는지 먼저 본다(저장 전 검수).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SERVICE_KEY_ENV = "DATA_GO_KR_SERVICE_KEY"
GUIDE_API = "https://apis.data.go.kr/B552468/koshaguide"
CASE_API = "https://apis.data.go.kr/B552468/disaster_api02"

STATUS = {
    "checked_at": "2026-11-02",
    "kosha_guide_metadata_api": {"url": "https://www.data.go.kr/data/15144147/openapi.do", "license": "제한 없음", "ingest": "metadata-only, 키 필요"},
    "kosha_guide_list_file": {"url": "https://www.data.go.kr/data/15116595/fileData.do", "license": "공공누리 제3유형(출처표시, 변경금지)", "ingest": "no"},
    "kosha_guide_body": {"url": "https://portal.kosha.or.kr", "license": "확인 못 함", "ingest": "no"},
    "accident_case_api": {"url": "https://www.data.go.kr/data/15121001/openapi.do", "license": "제한 없음", "ingest": "가능(키·개인정보 검수 뒤)"},
    "accident_case_attachment_api": {"url": "https://www.data.go.kr/data/15121008/openapi.do", "license": "제한 없음", "ingest": "가능(키 필요)"},
    "sif_archive_file": {"url": "https://www.data.go.kr/data/15140383/fileData.do", "license": "공공누리 제3유형(출처표시, 변경금지)", "ingest": "no"},
}


@dataclass(frozen=True)
class GuideRecord:
    """GUIDE 노드 하나(메타데이터만). 노드 ID `KOSHA {규정번호}`, 엣지 GUIDE_FOR(지침 → 조, 사람 검수 후보)."""

    guide_no: str
    title: str
    published: str
    download_url: str


@dataclass(frozen=True)
class CaseRecord:
    """CASE 노드 하나. 노드 ID `재해사례 {게시글 ID}`, 엣지 CASE_OF(사례 → 위험요인 개념·조, 후보)."""

    case_id: str
    business: str
    title: str
    summary: str
    registered: str


def service_key() -> str:
    key = os.environ.get(SERVICE_KEY_ENV)
    if not key:
        raise RuntimeError(f"{SERVICE_KEY_ENV} 가 없다(공공데이터포털 활용신청 뒤 발급). 이용 조건: kosha.STATUS")
    return key


def fetch_guides(*_: object, **__: object) -> list[GuideRecord]:
    """코샤가이드 조회 서비스 → GuideRecord. 키 발급과 Lead 확인 전에는 부르지 않는다."""
    raise NotImplementedError("KOSHA GUIDE 수집은 serviceKey 발급·Lead 확인 뒤 구현한다(메타데이터만)")


def fetch_cases(*_: object, **__: object) -> list[CaseRecord]:
    """국내재해사례 게시판 조회 서비스 → CaseRecord. 키 발급과 개인정보 검수 절차가 정해진 뒤 구현한다."""
    raise NotImplementedError("재해사례 수집은 serviceKey 발급·개인정보 검수 절차가 정해진 뒤 구현한다")
