# 날짜 작업·달력 목록·오늘만 되는 검사 (S15P31A101-610)

작업 관리 새 흐름(하루 = 작업 하나, 2026-11-10 결정)의 서버 쪽 A1~A3 이다. 요청서는 추창만의 「작업 관리 새 흐름 — 서버·AI 요청」(11-11 검토 답변 반영본)이다.
관련 명세: 8.3(작업·TBM), 11.4-5(NFR-14). 앞 이슈: -609(빈 보고서·양식 칸, `docs/design/tbm-daily-form.md`).

## 만든 것

| 항목 | 위치 |
|---|---|
| V54: `work_unit.daily`·`setup_completed_at`, `name` NULL 허용(날짜 작업만), `UNIQUE (site_id, work_date) WHERE daily` | `server/src/main/resources/db/migration/V54__daily_work_unit.sql` |
| `PUT /api/work-units/daily/{date}` (ADMIN): 날짜 작업 만들기·찾기 + 빈 TBM 보고서 | `WorkUnitController.daily`, `WorkUnitRepository.findOrCreateDaily` |
| `GET /api/work-units?from=&to=`: 작업 날짜 구간(양끝 포함, 92일 이내) | `WorkUnitController.list`, `WorkUnitRepository.findByDateRange` |
| 작업 응답에 `daily`·`progress`·`tbm_report{report_id,draft_status,submitted_at}`·`setup_completed_at` | `WorkUnitRepository.view` |
| 날짜 작업 이름: 양식 저장 때 작업내용으로 다시 만듦 | `TbmReportRepository.renameDailyWork`, `TbmForm.workName` |
| 오늘만: 날짜 작업 시작은 작업 날짜가 오늘이고 참석자 서명이 1명 이상일 때 | `WorkUnitRepository.startSession`·`requireToday` |
| 시험 | `server/src/test/.../work/DailyWorkUnitIntegrationTest.java` |

## 진행 칩(`progress`)

뒤 단계부터 본다.

| 값 | 조건 |
|---|---|
| `SUBMITTED` | 최신 TBM 보고서에 `submitted_at` 이 있음(-611 에서 칸을 더하며 조회를 바꾼다. 그전에는 늘 null) |
| `ENDED` | 작업 `DONE` |
| `IN_PROGRESS` | 작업 `IN_PROGRESS` |
| `SETUP_DONE` | `setup_completed_at` 있음(세팅 완료 C17 이 기록) |
| `SETUP` | 이 작업의 `setup_session` 이 하나라도 있음 |
| `REPORT` | 그 밖(보고서만 있음) |

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 지난 날짜 | 이미 있으면 돌려주고, 없으면 409 `WORK_DATE_PAST` | 요청서 A1(오늘 이전은 만들지 않음). 지난 날짜 다시 보기는 목록·단건 조회로 한다 |
| 계획 시간 | 그날 0시~다음 날 0시(Asia/Seoul) | 요청서 A1. `starts_at`·`ends_at` NOT NULL·CHECK 를 바꾸지 않는다 |
| `event_counts` | 날짜 작업은 실제 시작 전에는 세지 않는다 | 요청서 A4. 실제 시작 뒤에는 기존처럼 실제 시작~종료(진행 중이면 지금까지) |
| 날짜 구간 상한 | 92일 | 달력 한 달(앞뒤 주 포함 6주)이 넉넉히 들어가고, 기존 `days` 상한(90)과 비슷 |
| 이름 | 첫 작업내용(항목 이름 또는 직접 쓴 글) + ' 외 N건', 100자를 넘으면 첫 항목을 줄임(…), 없으면 NULL(응답은 빈 글자). 바뀔 때만 저장 | 요청서 A1. 화면들이 `name` 을 글자로 검사해 null 이면 목록에서 빠진다 |
| 지난 날짜 다시 열기 | 작업만 돌려주고 빈 보고서를 새로 만들지 않는다 | 지난 날짜에 새 기록을 만들지 않는다 |
| 예전 수정·삭제 API | 날짜 작업이면 409 `WORK_INVALID_STATE` | 날짜·시간·이름을 서버가 정하고, 날짜를 옮기면 하루 하나 색인에 걸린다 |
| 작업 시작 서명 범위 | 최신 보고서 `report_version` 의 서명만 센다 | 예전 버전 서명으로 시작하지 않게 |
| 작업 시작 서명 | 최신 TBM 보고서의 PARTICIPANT 서명 1명 이상, 아니면 409 `WORK_NO_SIGNATURE` | 요청서 A3(선택)을 넣었다. 화면 검사만으로는 다른 PC·API 호출을 막지 못한다 |
| 예전 작업 | 날짜·서명 검사를 하지 않는다 | 예전 작업(daily=false)의 시험·데이터를 바꾸지 않는다 |
| 빈 보고서 만들기 | 날짜 작업과 같은 트랜잭션 | 보고서 없는 날짜 작업이 남지 않게 |

## 세팅 쪽에 넘긴 것

- 세팅 시작(C13)·세팅 완료(C17)의 '오늘만' 검사는 `WorkUnitRepository.requireToday(siteId, workId, now)` 를 `SetupService` 에서 부른다(박종화).
  `SetupService` 는 C13·C17 로 함께 고치는 중이라 충돌을 피해 이 이슈에서는 고치지 않았다.
- 세팅 완료 때 `work_unit.setup_completed_at` 을 기록한다(C17).

## 남은 일

- -611: `report.submitted_at` 추가(진행 칩 `SUBMITTED` 가 그때부터 켜진다).
