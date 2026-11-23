# S15P31A101-842 열기만 한 날짜 작업은 달력에서 세지 않기

관련 명세: 16장 작업 관리 흐름(날짜 → TBM → 세팅 → 작업 전 확인 → 작업), AGENTS.md 규칙 6(덮어쓰지 않는다).
앞 티켓: -610(날짜 작업·달력 목록 `GET /api/work-units?from&to`·`PUT /api/work-units/daily/{date}`), -611(서명·제출), -696(실행 `work_run`), -751(사건 수).
사용자 요청 2026-11-23: 날짜 화면을 열기만 해도 `useOpenDay` 가 `PUT /daily` 로 그날 작업(daily)과 빈 TBM 보고서를 만들어 달력에 「작업 1건」이 뜬다(운영 11-23 work 68). 열기만 하고 아무것도 안 한 날은 달력 작업 수에서 뺀다.
**DB 변경 없음, 계약 변경 없음**(작업 REST 는 계약 밖). 기록은 지우지 않고 달력 목록에서 세는 것만 바꾼다.

## 판정 기준(서버 한 곳: `WorkUnitRepository.EMPTY_DAILY`)

아래를 모두 만족하는 날짜 작업을 「빈 날짜 작업」으로 보고 달력 목록(`findByDateRange`)에서 뺀다.

| 조건 | SQL | 근거 |
|---|---|---|
| 날짜 작업 | `w.daily` | 예전 작업(daily=false)은 사람이 직접 만든 것이라 늘 센다 |
| 시작한 적 없음 | `status = 'PLANNED'`, `actual_started_at IS NULL`, `work_run` 행 없음 | 실행이 한 번이라도 있으면 기록이다(되돌린 작업 -649 도 실행 행은 남는다) |
| 세팅을 시작한 적 없음 | `active_setup_session_id IS NULL`, `setup_completed_at IS NULL`, `setup_session` 행 없음 | 실패로 끝난 세팅도 사용자가 한 일이다 |
| 보고서가 만든 그대로 | 이 작업의 모든 보고서가 `TBM`·1판·`PENDING`·`form_revision = 0`·제출·결재 없음 | `createBlank` 가 1판 PENDING, `form_revision` 0(V51 기본값)으로 만든다. `form_revision` 은 양식 저장(`updateForm`)·AI 초안 채움·종료 미팅 채움·되돌리기가 1씩 올리므로 0 이면 아무도 저장하지 않은 서버 기본 양식이다. AI 초안은 작업내용이 저장돼 있어야 시작되고(REPORT_FORM_NO_TASKS), 시작하면 status 가 GENERATING 이 된다 |
| 서명 없음 | 이 작업 보고서의 `report_signature` 행 없음 | 양식 저장 없이 서명만 받아도 사용자가 한 일이다 |

- 이름(작업내용) 칸이 비었는지로 판정하지 않는다. 작업내용 체크를 모두 풀어 이름이 빈칸이어도 양식을 저장했으면(`form_revision ≥ 1`) 센다.
- 양식 내용(JSON)을 서버 기본값(`TbmForm.blank`)과 비교하지 않는다. 기본 양식의 일시는 만든 시각이라 같은 값을 다시 만들 수 없고, 같은 값으로 다시 저장한 것도 사용자가 저장한 것이다.
- 보고서가 아예 없는 날짜 작업(보고서가 생기기 전 예전 데이터)도 나머지 조건이면 빈 것으로 본다.

## 바뀐 것과 그대로인 것

| 경로 | 동작 |
|---|---|
| `GET /api/work-units?from&to`(달력 월 칸·월 요약·지난 날짜 화면·막힌 세팅 찾기) | 빈 날짜 작업을 뺀다. 달력은 서버 목록의 길이를 그대로 세므로(`calendarMath.summarizeWorks`·`summarizeMonth`) front 계산은 바꾸지 않았다. 오늘 칸은 작업이 없으면 `IDLE_PROGRESS = REPORT` 로 「오늘 작업 준비」 단추가 되어 열기만 한 날과 같은 단추다 |
| `PUT /api/work-units/daily/{date}`·`GET /api/work-units/{id}` | 그대로. 날짜 화면 열기·이어 쓰기·「오늘 작업 준비」 진입은 같은 작업·보고서를 쓴다 |
| `GET /api/work-units?days=`(작업 다시 보기 찾기) | 그대로(달력 숫자가 아니다) |
| 사건 수(`event_counts`·`incident_counts`) | 그대로. 날짜 작업은 시작 전에는 원래 0 이다 |

## 목업(`front/apps/admin/src/mocks/work.ts`)

- `isEmptyDaily` 가 같은 규칙(PLANNED·시작·실행·세팅 세션·세팅 완료·AI 초안 요청 없음, 보고서 1판·PENDING·form_revision 0·제출 없음, 서명 없음)으로 달력 목록에서 뺀다. 목업에서 오늘 날짜 화면을 열고 나와도 달력 숫자가 늘지 않는다.
- 서명 확인은 `signaturesOf`(빈 목록을 만들어 둔다) 대신 `signaturesByReport.get` 으로 해 저장 상태를 바꾸지 않는다. 연습 빌드 저장 형식(-806 `MOCK_SESSION_VERSION`)은 그대로다.

## 버린 대안

| 대안 | 버린 까닭 |
|---|---|
| 날짜 화면을 열 때 작업을 만들지 않고 첫 저장 때 만들기 | 날짜 화면·보고서·세팅·서명이 모두 `work_id`·`report_id` 를 전제로 움직인다(`useOpenDay`, 보고서 편집 낙관적 잠금, 세팅 세션 `work_unit_id`). 첫 저장 경로마다 만들기를 넣어야 하고 동시 저장 경쟁이 새로 생긴다. 숫자 하나 때문에 흐름 전체를 바꾸는 비용이 크다 |
| 빈 작업을 지우기(나갈 때 DELETE 또는 주기 정리) | 덮어쓰지 않는다 원칙(규칙 6)과 맞지 않고, 다른 탭·다른 관리자가 같은 날짜 화면을 열어 두었으면 그 화면의 `work_id` 가 사라진다. 날짜 작업은 수정·삭제 API 도 막혀 있다(`rejectDaily`) |
| 응답에 `empty` 같은 칸을 더하고 front 에서 거르기 | 판정이 서버·front 두 곳에 갈리고 front 스키마도 바꿔야 한다. 달력 목록을 쓰는 곳(달력·지난 날짜·막힌 세팅 찾기) 모두 빈 작업을 볼 까닭이 없어 서버에서 빼는 편이 단순하다 |
| 이름이 빈칸이면 빈 작업으로 보기 | 작업내용 체크를 풀고 저장하면 이름이 빈칸이 된다. 사용자가 다른 칸을 저장한 날까지 빠진다(요청에서 금지) |

## 남은 위험

- 서명을 받았다가 모두 지우고(시작 전 서명 삭제 -611) 양식은 한 번도 저장하지 않은 날은 다시 빈 작업으로 보인다. 서명 삭제가 행을 지우는 기존 동작이라 남은 흔적이 없다.
- 앞으로 날짜 작업에 사람 손이 닿는 새 경로(새 표나 `form_revision` 을 올리지 않는 보고서 갱신)가 생기면 `EMPTY_DAILY` 에도 조건을 더해야 한다. 조건이 빠지면 그날이 달력에서 빠질 뿐 기록은 남는다.
- 지난 날짜 화면(`PastDayPage`)은 빈 날짜 작업만 있던 날을 「작업 없음」으로 보인다(빈 보고서를 보여 줄 까닭이 없다고 봤다).
- 목업 규칙은 서버 SQL 을 손으로 옮긴 것이라 둘이 어긋날 수 있다. 서버 통합 시험(`DailyWorkUnitIntegrationTest`)과 목업 시험(`work.test.ts`)이 같은 경우(열기만·양식 저장·서명·세팅·예전 작업)를 각각 본다.

## 시험

- 서버 `DailyWorkUnitIntegrationTest`: (a) 열기만 한 오늘·내일 0건(다시 열어도 0건, 단건 조회는 됨) (b) 양식 저장(이름 빈칸) 1건 (c) 세팅 세션(FAILED) 1건 (d) 서명만 1건 (e) 지난 날 예전 작업·끝난 날짜 작업 그대로. 달력 구간 시험은 날짜 작업 양식을 한 번 저장하도록 고쳤다.
- front `__tests__/mocks/work.test.ts`(-842 묶음, 월 목록·권한 시험 갱신), `__tests__/features/work/useWorkHooks.test.tsx`(월 목록 훅).
