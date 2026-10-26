"""RAG 조항 색인 (S15P31A101-27, 명세 5.4·11장, docs/design/rag-clause-index.md).

산업안전보건기준에 관한 규칙의 조항을 국가법령정보 Open API 에서 받아(law) 조 단위로 나누고(chunk), BAAI/bge-m3 로
임베딩해(embed) EC2 `rag_clause`·`rag_query_template` 시드 SQL(Flyway 반복 마이그레이션)을 만든다(seed).
검색은 EC2(S15P31A101-223), 인용 검사는 분석 워커 tbm_report(S15P31A101-225)가 한다.
"""
