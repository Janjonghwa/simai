# TBM 보고서 서비스 (S15P31A101-417)

근거: 명세 5.4·8.3·12.2, 인터페이스 `.agy/tbm-interface.md` A. 구현은 `ai/src/simai_ai/report/retrieval.py`, `tbm_graph.py`, `ai/services/risk_engine/graph_store.py`와 API에 있다. 시험은 `ai/tests/report/test_tbm_graph.py`, `ai/tests/services/risk_engine/test_report_api.py`.

법령 ID·규칙 태그·pg_trgm·사전 임베딩 템플릿의 네 경로마다 8개, 합친 시드 12개, 양방향 1-hop 확장 24개, 임베딩된 조·항·호·목·별표 허용 목록 16개다. 미검수 엣지는 제외한다. LLM에는 허용 조문 본문 앞 600자와 화이트리스트 사실만 보낸다. 검증 실패 시 한 번만 재작성하고 실패본은 반환하지 않는다. 템플릿이 없으면 의미 검색을 생략하고 단계 기록에 남긴다.

실시간 질의 임베딩과 torch는 컨테이너 자원·의존성을 피하려고 제외했다. 등급·법령 적절성은 LLM 설명의 의미까지 결정적으로 검증할 수 없으므로 G7 사람 평가가 필요하다. 법령 본문의 주소는 텍스트 전용 검사에서 거부될 수 있다. 운영 전 실제 시드 DB와 GMS 연결·90초 서버 타임아웃을 검증해야 한다. 두 차례 LLM 호출의 합산 시간은 별도 제한하지 않으므로 호출별 타임아웃을 서버 제한에 맞춰 설정한다.
