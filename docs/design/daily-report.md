# 일일 안전 일지 API

S15P31A101-33(일일 일지 부분). 명세 FR-122, 8.3, 12.2(EC2 내부 Python 컨테이너 `POST /v1/report/generate`), 12.6(`/api/reports/daily`),
5.4(외부 LLM 텍스트 전용·개인정보 금지). 법령 그래프 검색·4단계 생성·검증은 [`tbm-legal-graph.md`](tbm-legal-graph.md)(-415, -417)를 그대로 쓴다.
TBM 보고서 API 는 -418 이 만들었고, 이 이슈의 TBM 부분(`POST·GET /api/reports/tbm`)은 그쪽으로 끝났다.

- 서버: `server/.../domain/report/` — `DailyReportController`·`DailyReportService`·`DailyReportRepository`, 공용 호출부 `RiskEngineReportClient`(TBM 과 함께 씀)
- Python: `ai/src/simai_ai/report/tbm_graph.py`(DAILY 입력 사실·섹션 키·검사), `retrieval.py`(이벤트 규칙으로 후보 검색), `ai/services/risk_engine/app.py`(DAILY 허용)
- 표: V36 — `report` 에 `site_id`·`report_date` 제약·`events`·`event_summary`
- 시험: `server/src/test/.../domain/report/DailyReportIntegrationTest.java`, `ai/tests/report/test_tbm_graph.py`·`ai/tests/services/risk_engine/test_report_api.py`

## 흐름

1. 관리자가 `POST /api/reports/daily` `{"report_date": "2026-11-03"}` 를 보낸다(비우면 오늘). 서버는 그 현장·날짜의 새 `report_version` 행을
   `GENERATING` 으로 넣고 202 와 `report_id` 를 준다. 같은 날짜를 생성 중이면 409 `DAILY_REPORT_IN_PROGRESS` 와 그 `report_id`.
2. 가상 스레드에서 그날 Asia/Seoul 0시 이상 다음 날 0시 미만에 발생한 LIVE `risk_event` 를 읽어 사실 목록(`events`, 발생 순 최대 100건)과
   집계(`event_summary`)를 만들고 행에 먼저 저장한다. 엔진이 실패해도 조회 화면은 근거 표를 보여 줄 수 있다.
3. 활성 규칙 묶음에서 그날 난 규칙의 고정 근거 조항(`legal_basis`), 이벤트가 난 구역의 종류·이름(`zones`)을 붙여 Risk Engine
   `POST /v1/report/generate`(`report_kind` DAILY)를 부른다.
4. 컨테이너는 TBM 과 같은 하이브리드 검색·1단계 확장으로 허용 조문을 정하고, 외부 LLM 이 `summary`·`events`·`measures`·`legal` 네 섹션을 쓰면
   코드가 인용·본문 조문·확률 표현·섹션 키를 검사한다(1회 재작성).
5. 서버는 결과를 `SUCCEEDED`(본문·인용·서브그래프·단계) 또는 `FAILED`(사유)로 마감한다. `GET /api/reports/daily/{id}`·`?date=` 로 본다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 하루의 경계 | Asia/Seoul 0시 이상 다음 날 0시 미만, `raised_at` 기준 | 현장이 한국이고 현장별 시간대 칸이 없다. 그날 "발생한" 위험을 되돌아보는 일지다 |
| 날짜 입력 | `report_date` YYYY-MM-DD, 비우면 오늘, 오늘보다 뒤는 400 `INVALID_DAILY_DATE`, 지난 날짜는 받음 | 아직 일어나지 않은 날은 쓸 사실이 없다. 지난 날 일지를 나중에 만들 수 있어야 한다 |
| 묶는 단위 | 현장·날짜 하나에 일지, 재생성은 새 `report_version`(TBM 의 작업별 버전과 같은 방식) | 일일 일지는 작업이 아니라 하루를 본다. 덮어쓰지 않고 이력을 남긴다 |
| 넘기는 이벤트 칸 | `rule_id`·`severity`·`zone_id`·`raised_at_ms`·`resolved`(lifecycle RESOLVED)·`needs_check` | 사람 ID(`world_ids`)·근거 문장(`rationale`)·작업명·인원은 넘기지 않는다(5.4·11.4-5). Python 도 이 칸만 골라 LLM 에 넣는다 |
| 이벤트 수 상한 | 엔진에 100건, 넘으면 높은 등급부터 고르고 발생 순으로 둠. 집계는 전부, 잘렸으면 `event_summary.events_truncated` 참. 구역·근거 조항은 고른 100건 기준 | LLM 입력 길이. 늦게 난 CRITICAL 이 빠지지 않게. 건수·등급·규칙 합계는 집계로 정확히 넘긴다 |
| 동시 생성 | 같은 날짜를 동시에 요청하면 늦은 쪽 409(`ON CONFLICT DO NOTHING`, 트랜잭션을 깨지 않음). 먼저 끝나 버전만 겹친 드문 경우 `report_id` -1 | 유일 색인 위반 예외로 받으면 같은 트랜잭션에서 다시 조회할 수 없다 |
| 생성 중으로 남는 경로 | 작업을 넘기지 못하면(서버 종료 중) 바로 `FAILED`(`INTERRUPTED`), 마감 기록 실패는 한 번 더 시도, 그래도 안 되면 기동 때 복구 | 생성 중 행이 남으면 그 날짜가 409 로 막힌다 |
| 이벤트가 없는 날 | 그대로 생성하고, 고정 근거 조항은 활성 규칙 전체 | "이벤트 없음" 도 일지다. 후보 검색에 쓸 규칙이 없어 TBM 의 공종 없음과 같은 대체를 쓴다 |
| 섹션 | `summary`(하루 요약)·`events`(이벤트 되돌아보기)·`measures`(다음 작업 대책)·`legal`(법령 근거 요약), 넷 다 필수 | TBM 의 `hazards`·`measures`·`focus`·`legal` 과 같은 4칸 구조. 예전 I5 설계의 SUMMARY·EVENT_REVIEW·RECOMMENDATION 을 옮김 |
| 후보 검색 규칙 | TBM 은 `risk_items`, DAILY 는 `events` 의 규칙(R1~R7) | 같은 `retrieve` 를 쓰고 입력만 다르다 |
| 조회 권한 | 생성은 관리자, 조회는 열람 이상 | TBM 보고서 API 와 같다(12.6) |
| 실패 사유 | 엔진 연결 실패 `ENGINE_UNAVAILABLE`, 읽기 시간 초과 `ENGINE_TIMEOUT`, 그 밖은 엔진의 `failure_reason`(`CITATION_VIOLATION`·`LLM_TIMEOUT` 등) | TBM 과 같은 값 |
| 서버 재시작 | `GENERATING` 으로 남은 일지는 TBM 과 함께 `FAILED`(`INTERRUPTED`) | `TbmReportRepository.failInterrupted` 가 종류를 가리지 않는다 |
| 엔진 호출부 | `RiskEngineReportClient` 로 떼어 TBM·일일 일지가 같은 주소·타임아웃을 씀 | 같은 컨테이너·경로다. 시험은 `TbmReportService.configureRiskEngine` 으로 둘 다 바꾼다 |

## 시험

`DailyReportIntegrationTest`(가짜 Risk Engine HTTP 서버):

- 그날 경계(0시 1분·23시 59분 포함, 전날 23시 59분·다음 날 0시·REPLAY 제외), 집계, 엔진 요청의 사람 ID·근거 문장 없음, 구역 종류, 재생성 버전 2, 날짜별 조회
- 토큰 없음 401·열람 403, 내일·잘못된 형식 400, 없는 보고서 404, 빈 날짜 조회
- 같은 날짜 생성 중 409 와 `report_id`, 이벤트 없는 날 생성
- 엔진 500 → `FAILED`(`ENGINE_UNAVAILABLE`)이어도 그날 사실은 남음, 엔진의 `CITATION_VIOLATION`

Python: DAILY 섹션·사실 칸 거르기·TBM 섹션 키 거부·이벤트 규칙 후보 검색·100건 상한, 엔드포인트의 DAILY 허용과 모르는 종류 400.

## 남은 일

- 관제 웹 일일 일지 화면(생성 버튼·본문·근거 표·그래프 패널): `/tbm` 화면(-419)처럼 프론트 이슈로.
- 실제 외부 LLM·법령 그래프 코퍼스로 일일 일지 문장 품질 확인(G7 인용 평가 표본에 일일 일지 조합 넣기, `tbm-legal-graph.md`).
- 현장이 여럿이 되면 `risk_event` 에 현장 칸이 없어 하루 이벤트를 현장별로 나눌 수 없다(지금은 현장 하나).
