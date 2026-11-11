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
- 아래 새 버전 규칙은 양식이 없는 예전 작업 보고서(`mode: NEW_VERSION`)에만 적용한다. 날짜 작업 보고서는 맨 아래 'S15P31A101-616 날짜 작업 종료 미팅'을 따른다.
- 생성 중 보고서가 있으면 종료를 수락하고 `SKIPPED/REPORT_GENERATING`과 그 ID를 반환한다. 성공한 보고서가 하나도 없으면 `SKIPPED/NO_REPORT`다. 성공한 버전 중 최신 본문을 복사하되 새 버전 번호는 전체 이력의 최댓값+1이다.
- 새 버전에는 이전 잠금·해시·서명을 복사하지 않는다. 마무리 생성 중에도 기존 sections·citations·risk_items 등 본문은 보존한다. 서버 재기동 시 중단된 마무리도 FAILED/INTERRUPTED로 마감한다.
- 숫자 값의 집합만 비교하면 CRITICAL/HIGH의 수를 뒤바꾼 오류를 놓친다. 따라서 모든 항목명·숫자 쌍과 최고 등급을 포함한 필수 사실 절을 정확히 대조하고, 추가 숫자·확률·규칙 ID·등급을 거절한다. 텍스트는 최대 8,000자(짧은 마무리 문단의 응답 상한)다. 키·요청 오류·LLM 타임아웃·두 초안 검증 실패는 결정론 문장으로 대체한다.
- 내부 HTTP의 본문 상한은 기존 `MAX_BODY_BYTES`(4 MiB)를 재사용한다. 시간 구간은 서버의 범위(9999년 말)와 같고 0 길이도 허용한다. 사람 이름·작업명·조 이름·구역 ID·영상·좌표는 프롬프트에 넣지 않는다. 등록 구역 표시명만 아래 제한에 따라 넣는다.

## 통합과 잔여 위험

- 서버 컴파일·시험은 `.agy/inst3.md`에 따라 Lead가 실행한다. 여기서는 Gradle을 실행하지 않는다.
- -440은 V29를 사용한다. V30은 `locked_at`·`content_sha256`·`wrap_up_summary`를 IF NOT EXISTS로 추가해 독립 worktree에서도 조회가 가능하다. `report_signature`가 없으면 signature_count=0, 있으면 report_id·report_version별 실제 수를 조회한다. Lead 통합 시 -440과 겹치는 보고서 응답·DDL을 함께 확인해야 한다.
- 이전 DB 설계 `db-tables.md`의 wrap_up_summary=jsonb 묶음 설명과 Lead 공통 인터페이스 A의 문자열 응답이 다르다. 이번 구현은 최신 확정 인터페이스대로 summary=text, facts=jsonb 별도 열로 저장한다. 기획서 §8.3의 문단+사실 보존 의미는 유지한다.
- 기존 TBM 실행기와 같이 DB 예약 후 프로세스 종료 시 생성 작업이 유실될 수 있다. 재기동 복구는 FAILED/INTERRUPTED이며 자동 재시도는 두지 않는다.
- 종료 직후 지연 도착한 과거 LIVE 이벤트는 이미 고정한 사실에 추가되지 않는다. 종료 트랜잭션 시점 DB 집계 요구를 따른다.

## S15P31A101-551 시간대 사실 보강

내부 요청 `facts`에 `phase_counts`(정수 `early`, `middle`, `late`), `first_event_min`, `last_event_min`을 필수로 추가한다. 범위 내 대상 LIVE 이벤트의 발생 시각을 작업 시작부터 종료까지 3등분한다. 경계는 다음 구간, 종료 시각은 후반, 길이 0인 구간은 초반이다. 첫·마지막 분은 시작 이후 경과 밀리초를 60000으로 나눈 내림값이며 이벤트가 없으면 둘 다 null이다. 시간대 합은 전체 이벤트 수와 같아야 하고 분은 구간 내 순서와 범위를 검사한다. 대상 구역과 확인 기준은 기존 집계와 같다.

필수 절은 각각 정확히 한 번 필요하고 절 밖 숫자·등급·규칙 ID는 거절한다. JSON과 코드 블록은 거절 후 한 번 재작성하며 실패 시 결정론 문장으로 대체한다. 허용된 연결 문구만 쓰게 하며 새 수치·판정·추측을 금지한다.

추가 사실 `top_zones`는 대상 이벤트에서 발생 시점에 유효한 등록 구역 표시명별 건수를 합쳐 상위 3개를 건수 내림차순, 동률이면 이름순으로 정한다. 이름 없는 이벤트나 유효한 구역 버전이 없거나 표시명이 40자를 넘거나 공백·제어문자 등으로 유효하지 않으면 `현장 전체`로 센다. 같은 표시명은 합산한다. 입력은 `name`과 양의 정수 `count`만 가진 닫힌 객체 배열이며 중복 이름, 내림차순 위반, 전체 초과를 거절한다. `unacknowledged_critical_count`는 작업 구간 안 운영자 확인 기록이 없는 대상 CRITICAL 이벤트 수이며 CRITICAL 전체 건수 이하로 검사한다. 규칙 문구는 외부 입력이 아닌 Risk Engine의 R1~R7 고정 사전이다.

각 규칙 설명, 구역 이름과 건수, 미확인 CRITICAL은 각각 필수 사실 절로 정확히 한 번 요구한다. 표시명의 숫자도 전체 절과 함께 제거한다. 절 밖에는 제한된 연결 문구만 허용하여 지어낸 구역명이나 설명을 거절한다. 자유 요약은 허용되지 않으므로 표현의 다양성은 제한된다. 구역 표시명은 관리자 등록 내용이어서 사람 이름을 넣지 않는 운영 기준에 의존한다.

규칙 설명은 risk-engine `RULE_DESCRIPTIONS` 와 서버 대체 문장의 같은 고정 사전(명세 7장 R1~R10, R11 조끼 -549)에서 온다. 사전에 없는 규칙 ID 가 오면 요청 전체를 거절해 대체 문장으로 간다(R9 안전모 이벤트가 있는 작업에서 마무리가 통째로 대체 문장이 되던 예전 `R[1-7]` 제한을 없앰).

가짜 LLM 시험 예시(시간대·구역별 건수·미확인 수 등은 시험용 가상 값): 작업 중 전체 이벤트 46건, 등급 CRITICAL 7건, 등급 HIGH 0건, 등급 CAUTION 39건, 등급 INFO 0건. 집계 내용은 다음과 같다. 규칙 R1 위험 구역 진입 7건, 규칙 R5 진입 예측 32건, 규칙 R7 구역 감시 능력 저하 7건, 구역 개구부 1 이벤트 30건, 구역 개구부 2 이벤트 16건, 최고 등급 CRITICAL, 운영자 확인 5건, 운영자 미확인 CRITICAL 4건, 운영자 종료 0건, 작업 외 진입 4건, 작업 초반 이벤트 12건, 작업 중반 이벤트 20건, 작업 후반 이벤트 14건, 첫 이벤트 시작 후 2분, 마지막 이벤트 시작 후 58분. 기록을 확인했다.

위험도나 원인의 변화 추정, 구역 ID·작업명 사용은 제외했다. 시간대별 수치는 작업 길이에 의존하며 원인이나 위험도 변화를 뜻하지 않는다.

## S15P31A101-616 날짜 작업 종료 미팅

최신 TBM 보고서에 양식(`report.form`)이 있으면(날짜 작업, -609) 종료는 **새 버전을 만들지 않는다**. 작업 전에 받은 서명이 `report_version` 에 묶여 있어서다.

- 종료 트랜잭션이 그 보고서 행을 잠그고 같은 행에 `wrap_up_status=GENERATING`·`wrap_up_facts`(위 집계 그대로)를 적는다. 응답 `wrap_up` 은 `{mode: "FORM_ENDING", status: "GENERATING", report_id: <같은 행>}` 이다. 잠긴 행이면 `SKIPPED/REPORT_LOCKED`.
- 커밋 뒤 `/v1/report/wrap-up` 대신 `/v1/report/form-ending`(계약 `tbm_form_ending_request/result`)을 부르고, 같은 행의 `form.review.ending` 이 비어 있을 때만 그 문장을 채운다(채우면 `form_revision` +1). 사람이 이미 쓴 종료 미팅은 덮지 않는다.
- 실패하면 위 결정론 문장(`wrapUpFallback`, 2000자에서 자름)을 채운다. `wrap_up_fallback`·`wrap_up_model` 은 기록하고, 서명 해시가 덮는 `wrap_up_summary` 는 쓰지 않는다. 초안 상태(`status`)는 바꾸지 않는다.
- 재기동 때 멈춘 날짜 작업 종료 미팅은 INTERRUPTED 가 아니라 저장된 사실의 결정론 문장으로 마감한다(`TbmFormAiService.recoverEndings`).
- 날짜 작업 보고서에는 예전 생성 경로(`POST /api/reports/tbm`)도 409 `REPORT_FORM_MANAGED` 로 새 버전을 만들지 않는다.
- 자세한 규칙: `docs/design/S15P31A101-616-tbm-form-draft.md` '2단계 서버 연결'.
