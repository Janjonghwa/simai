# S15P31A101-642 작업 시작은 세팅 완료 뒤에만

관련 명세: 16장 v1.3 수정 54("세팅 완료 후에만 LIVE 관제 시작"), 3.2 5항(작업 시작은 미리보기 맵으로 관제하고 정식 굽기만 회수). 선행: -615(세팅 완료 C17, `work_unit.setup_completed_at`), -624(세팅 READY 전 시작 거절), -625(시작 때 대기 굽기 회수). Jira: <https://ssafy.atlassian.net/browse/S15P31A101-642>.

## 결함

-615 부터 새 맵·보정·구역은 세팅 완료 때 관제에 들어간다. 작업 시작은 세팅 완료를 보지 않아(-624 는 현장 세팅이 READY 전인지만 봄), 완료 없이 시작하면 이전 맵·보정·구역으로 관제했다. 날짜 화면도 세팅 → 작업 전 확인을 로컬 상태로 먼저 넘겨, 서버 상태와 화면 단계가 따로 움직일 수 있었다.

## 바꾼 것

| 위치 | 내용 |
|---|---|
| `ErrorCode.Work.WORK_SETUP_NOT_COMPLETED` | 409. -624 의 `WORK_SETUP_NOT_READY` 를 지우고 이것으로 합쳤다 |
| `WorkUnitRepository.startSession` | 검사 순서: PLANNED → (날짜 작업) 오늘 → **이 작업의 `setup_completed_at`** → (날짜 작업) 참석자 서명 → 진행 중 작업. 현장 단위 READY 전 세팅 검사는 지웠다 |
| 프런트 `features/work/api.ts` | `WORK_ERROR.SETUP_NOT_COMPLETED`(옛 `SETUP_NOT_READY` 대체) |
| `preflightErrors.ts` | `SETUP_REQUIRED_MESSAGE`("세팅을 완료해야 시작할 수 있습니다")·`isSetupRequiredError`. -625 의 세팅 대기 안내를 이 문구로 바꿨다 |
| `PreflightConfirmDialog`·`PreflightStage` | 서버 progress 가 세팅 완료 전이면 '작업 시작'을 끄고 안내와 '세팅으로' 단추. 409 도 같은 안내·단추 |
| `useStartWork` | 409 `WORK_SETUP_NOT_COMPLETED` 면 작업 캐시를 다시 받아 단계를 서버에 맞춘다 |
| `WorkDayPage` | 로컬이 먼저 넘기는 이동은 보고서 → 세팅뿐. 작업 전 확인은 서버 progress `SETUP_DONE` 일 때만 연다. 머리줄에 진행 상태(세팅 전·세팅 중·세팅 완료 · 시작 가능·관제 중·끝남) |
| `useCompleteSetup` | 완료 성공 때 작업 캐시 다시 받기를 기다린 뒤 끝난다. 완료 직후 날짜 화면이 바로 작업 전 확인(시작 가능)이다 |
| `mocks/work.ts` | 시작 검사를 서버와 같은 순서·코드로(세팅 완료 = `setup_completed_at_ms` 또는 `markSetupDone`) |
| 시험 | 서버: `WorkSessionIntegrationTest`(완료 전 409·묶음 없음·완료 뒤 200), `SetupSupplementIntegrationTest`(READY 만으로는 409, 완료 뒤 200), `DailyWorkUnitIntegrationTest`(세팅 완료가 서명보다 앞), 시작을 쓰는 기존 시험은 작업에 완료 시각을 둔다. 프런트: 확인창·작업 전 확인·날짜 화면·mock·오류 문구 |

## 정한 것과 근거

- **단계 기준은 서버 progress 하나다.** 서버 `progress` 의 `SETUP_DONE` 은 PLANNED 이고 `setup_completed_at` 이 있을 때다. 프런트에 `setup_completed_at` 칸을 따로 두지 않고 이 값으로 단계·시작 가능·진행 상태 글을 모두 정한다. 세팅 화면의 「세팅 완료」 카드도 같은 칼럼(`setup_completed_at_ms`)을 본다.
- **세팅 완료를 서명보다 먼저 본다.** 화면 순서(세팅 → 작업 전 확인 → 시작)와 같다.
- **예전 작업(daily=false)도 같은 기준이다.** 명세 수정 54 는 작업 종류를 나누지 않고, 예전 작업도 세팅 세션·세팅 완료를 할 수 있다(오늘 검사만 날짜 작업 전용). 화면은 예전 작업을 시작하지 않으므로 API 로만 영향이 있다.
- **-624 의 현장 단위 READY 전 검사를 지웠다.** 세팅 완료는 READY_FOR_ZONES 뒤에만 되고(`SETUP_NOT_READY_TO_COMPLETE`), 완료한 작업에는 새 세팅을 만들지 않는다(`SETUP_ALREADY_COMPLETED`). 그래서 이 작업의 READY 전 GPU 단계가 LIVE 중에 도는 경우는 이 기준이 막는다.
- **-641 의 "막는 세션을 409 에 싣기"는 세팅 시작(`SETUP_ALREADY_IN_PROGRESS`)에만 남긴다.** 작업 시작은 다른 세팅을 보지 않으므로 시작 409 에 실을 세션이 없다. 멈춘 세팅이 이 작업의 세팅을 막으면 세팅 단계에서 -640·-641 의 취소 경로로 푼다. 작업 전 확인의 막는 세팅 취소 단추(`BlockingSetupNotice`)와 그 시험은 지웠다.
- **-637 의 관제 작업 목록 무효화(`TBM_WORKS_KEY`)는 그대로 두고**, 작업 관리 캐시(`WORK_KEY`) 다시 받기만 완료가 기다린다.
- **-625 회수는 그대로 맞다.** 완료 뒤에도 정식 굽기는 계속 돈다. 시작 때 대기열 굽기는 `onWorkStarted` 가 회수하고, 대여 중 굽기는 하트비트 신호로 워커가 회수한다(-597). 완료 전 다시 굽기(-607)는 시작 때 회수하지 않고 대여만 LIVE 중 보류된다(-624, 기존과 같음).

## 버린 대안

- **현장 READY 전 검사를 함께 남기기**: 다른 작업의 세팅을 막는 두 번째 기준이 남고, 멈춘 세팅 하나가 모든 작업 시작을 막던 -625 S9 문제가 그대로다.
- **세팅 → 작업 전 확인 로컬 이동 유지 + 단추만 끄기**: 화면 단계와 서버 상태가 어긋나는 원인이 그대로다.

## 잔여 위험

- 같은 현장의 **다른 작업**(API 로만 만드는 예전 작업)이 READY 전 세팅을 돌리는 중에 이 작업을 시작하면 막지 않는다. 날짜 작업 흐름(하루 하나, 세팅은 오늘만)에서는 생기지 않는다.
- 옛 프런트는 새 코드를 몰라 일반 문구("시작하지 못했어요")를 보인다. 계약·마이그레이션은 없다.
- 달력 오늘 칸의 '작업 시작'(REPORT)은 하루 흐름을 여는 단추라 그대로 두었다.

## 배포 순서

제약 없음. 서버만 먼저 나가도 완료 전 시작은 409 로 막힌다.
