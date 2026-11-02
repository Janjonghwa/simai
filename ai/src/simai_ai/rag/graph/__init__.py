"""건설안전 법령 지식 그래프 오프라인 파이프라인 (S15P31A101-416, docs/design/legal-graph-pipeline.md).

TBM 보고서 RAG 가 조문을 벡터로 찾은 뒤 참조·위임·준용 관계로 넓혀 읽도록, 법령을 노드(법령·조·항·호·목·별표·개념)와
엣지(상하위·참조·준용·불구하고·위임·별표 관련·개념 연결·위험 규칙 태그 후보)로 만든다. PostgreSQL 노드 표 + 엣지 표에 넣을
JSONL·SQL 을 낸다(별도 그래프 DB 없음).

- `catalog`: 대상 법령과 위임 관계, 노드 ID 접두사
- `ir`: 수집 경로(Open API·웹 본문)가 함께 쓰는 중간 표현(LawDoc)과 줄 분류(항·호·목)
- `source_api`: 국가법령정보 Open API(DRF) 클라이언트·XML/JSON 파서. OC 는 환경변수 `LAW_GO_KR_OC` 로만 받는다
- `source_web`: 키가 없을 때의 대체 경로. 국가법령정보센터 웹 "법령 본문" HTML 을 같은 중간 표현으로 바꾼다
- `refs`: 조문 참조 결정적 추출(정규식, 오탐 줄이는 규칙)
- `build`: 중간 표현 → 노드·엣지(상하위·참조·위임·별표·개념·규칙 태그 후보)
- `embed_qwen`: Qwen3-Embedding-0.6B(revision 고정) 문서·질의 임베딩
- `export`: JSONL·SQL·검수 CSV·manifest
- `kosha`: KOSHA GUIDE·재해사례 수집기 틀(이용 조건 확인 전에는 내용을 넣지 않는다)

표준 라이브러리만 쓴다(임베딩만 extra `rag` 의 torch·transformers). Tier 0·risk 경로는 이 패키지를 import 하지 않는다.
"""
