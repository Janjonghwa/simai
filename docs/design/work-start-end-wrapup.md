# 작업 시작·종료와 TBM 마무리 (S15P31A101-439)

근거는 명세 §8.3·§12.6, `work-session-flow.md`, Lead 확정 `.agy/interface.md` A절과 `.agy/inst2.md`다.
REST와 내부 보고서 HTTP에 해당하는 별도 `contracts/` 메시지가 없어 계약 스키마는 변경하지 않는다.

## 구현과 시험 위치

- 서버 `domain/work/WorkSessionService`, `WorkUnitRepository`, `WorkUnitController`: 관리자 start(200)·end(202), PLANNED → IN_PROGRESS → DONE과 UTC epoch ms 실제 시각.
- `V30__work_session_wrap_up.sql`: 상태·시각 일관성, 현장당 진행 작업 하나인 부분 유일 인덱스, 마무리·사실·폴백 기록.
- `domain/report/WorkWrapUpRepository`, `TbmReportService`: 종료 트랜잭션에서 집계·새 버전 예약, 커밋 후 기존 실행기로 비동기 HTTP, 결과 또는 결정론 요약 저장.
- `ai/src/simai_ai/report/wrap_up.py`, `ai/services/risk_engine/app.py`: `/v1/report/wrap-up`, 닫힌 입력 검사, 항목별 숫자 검사·1회 재작성·폴백. 기존 LLM 설정을 재사용하며 GPU는 쓰지 않는다.
- 서버 시험 `WorkSessionIntegrationTest`, Python 시험 `tests/report/test_wrap_up.py`·`tests/services/risk_engine/test_wrap_up_api.py`.

## 정한 세부 사항

- 사이트 행 → 작업 행 순으로 잠근다. 다른 작업을 동시에 시작해도 직렬화되며, DB 부분 유일 인덱스가 마지막으로 중복을 막는다. 보고서 생성도 같은 작업 행을 잠그므로 종료의 버전 예약과 충돌하지 않는다.
- 상태가 PLANNED가 아니면 시작은 `WORK_INVALID_STATE`, 다른 진행 작업이 있으면 `WORK_ALREADY_IN_PROGRESS`와 해당 `work_id`를 반환한다.
- 실제 시각은 밀리초로 절단하며 같은 밀리초의 시작·종료도 허용한다. 시계가 뒤로 간 경우 종료는 시작 이상으로 기록한다.
- 목록과 이벤트 수는 실제 작업 구간을 우선 사용한다. 진행 작업은 예정 종료가 오래 지났어도 목록에 남는다.
- 집계는 `raised_at`이 양 끝을 포함한 실제 구간 안인 LIVE 이벤트다. 대상 구역이 null이면 현장 전체다. 현재 `risk_event`에는 site_id가 없으므로 기존 `SiteLookup`의 단일 현장 보장을 사용한다.
- 사실 키는 `event_count`, `severity_counts`, `rule_counts`, `max_severity`, `operator_ack_count`, `operator_close_count`, `outside_work_entry_count`다. 확인·종료 수는 해당 구간에 확인 또는 ACCEPTED 종료가 기록된 **서로 다른 대상 이벤트 수**다. 중복 확인으로 수를 부풀리지 않는다. 작업 외 진입은 다른 구역의 R1이며, 다른 규칙이나 zone_id가 null인 이벤트는 진입으로 세지 않는다. 현장 전체 작업에서는 작업 외 진입이 0이다.
- 생성 중 보고서가 있으면 종료를 수락하고 `SKIPPED/REPORT_GENERATING`과 그 ID를 반환한다. 성공한 보고서가 하나도 없으면 `SKIPPED/NO_REPORT`다. 성공한 버전 중 최신 본문을 복사하되 새 버전 번호는 전체 이력의 최댓값+1이다.
- 새 버전에는 이전 잠금·해시·서명을 복사하지 않는다. 마무리 생성 중에도 기존 sections·citations·risk_items 등 본문은 보존한다. 서버 재기동 시 중단된 마무리도 FAILED/INTERRUPTED로 마감한다.
- 숫자 값의 집합만 비교하면 CRITICAL/HIGH의 수를 뒤바꾼 오류를 놓친다. 따라서 모든 항목명·숫자 쌍과 최고 등급을 포함한 필수 사실 절을 정확히 대조하고, 추가 숫자·확률·규칙 ID·등급을 거절한다. 텍스트는 최대 8,000자(짧은 마무리 문단의 응답 상한)다. 키·요청 오류·LLM 타임아웃·두 초안 검증 실패는 결정론 문장으로 대체한다.
- 내부 HTTP의 본문 상한은 기존 `MAX_BODY_BYTES`(4 MiB)를 재사용한다. 시간 구간은 서버의 범위(9999년 말)와 같고 0 길이도 허용한다. 사람 이름·작업명·조·구역 ID·영상·좌표는 프롬프트에 넣지 않는다.

## 통합과 잔여 위험

- 서버 컴파일·시험은 `.agy/inst3.md`에 따라 Lead가 실행한다. 여기서는 Gradle을 실행하지 않는다.
- -440은 V29를 사용한다. V30은 `locked_at`·`content_sha256`·`wrap_up_summary`를 IF NOT EXISTS로 추가해 독립 worktree에서도 조회가 가능하다. `report_signature`가 없으면 signature_count=0, 있으면 report_id·report_version별 실제 수를 조회한다. Lead 통합 시 -440과 겹치는 보고서 응답·DDL을 함께 확인해야 한다.
- 이전 DB 설계 `db-tables.md`의 wrap_up_summary=jsonb 묶음 설명과 Lead 공통 인터페이스 A의 문자열 응답이 다르다. 이번 구현은 최신 확정 인터페이스대로 summary=text, facts=jsonb 별도 열로 저장한다. 기획서 §8.3의 문단+사실 보존 의미는 유지한다.
- 기존 TBM 실행기와 같이 DB 예약 후 프로세스 종료 시 생성 작업이 유실될 수 있다. 재기동 복구는 FAILED/INTERRUPTED이며 자동 재시도는 두지 않는다.
- 종료 직후 지연 도착한 과거 LIVE 이벤트는 이미 고정한 사실에 추가되지 않는다. 종료 트랜잭션 시점 DB 집계 요구를 따른다.
