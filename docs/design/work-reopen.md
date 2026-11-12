# 작업 완료 되돌리기 (시연 복구)

S15P31A101-649. 작업 상태는 PLANNED → IN_PROGRESS → DONE 한 방향이다(명세 8.3, `work-start-end-wrapup.md`). 날짜 작업은 하루 하나라
시연·리허설 중 실수로 완료를 누르면 그날 작업을 다시 시작할 수 없다(2026-11-12 시연에서 48번 작업이 시작 13초 만에 완료됨).
관리자가 서버 API 로 완료를 계획으로 되돌리고, 다시 시작은 화면의 시작 단추로 한다. 관제 앱 화면은 붙이지 않는다.

## 코드·시험

| 위치 | 내용 |
|---|---|
| `server/.../domain/work/WorkUnitController.java` | `POST /api/work-units/{id}/reopen`(ADMIN) |
| `server/.../domain/work/WorkSessionService.java` `reopen` | 한 트랜잭션, 덮어쓰는 첫 실행 시각을 WARN 로그로 남김 |
| `server/.../domain/work/WorkUnitRepository.java` `reopenSession` | 잠금(시작·종료와 같은 `lockSession`)·검사·`DONE → PLANNED`, 실제 시각 비움 |
| `ErrorCode.Work.WORK_REPORT_SUBMITTED`·`WORK_WRAP_UP_GENERATING` | 제출한 TBM 보고서, 마무리 생성 중이면 409 |
| `WorkSessionIntegrationTest` | 되돌린 뒤 다시 시작·종료, 날짜 작업 종료 미팅이 새 사실로 다시 채워짐, 409 다섯 가지, 권한·404 |

## 사용법

```bash
# 관리자 토큰으로(Swagger /api/docs 에서도 된다)
curl -X POST -H "Authorization: Bearer $TOKEN" https://simai.live/api/work-units/48/reopen
# 응답: 작업(status PLANNED, actual_started_at·actual_ended_at null). 그다음 관제 앱에서 작업 시작
```

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 되돌릴 상태 | DONE → PLANNED(진행 중으로 바로 가지 않음) | 시작이 세팅 완료·서명 확인, 레이아웃 구역 다시 잇기(`LayoutZoneService.sync`), 안전모·조끼 요구 묶음 발행을 한다. 진행 중으로 바로 되돌리면 이 일을 따로 다시 해야 하고 어긋날 곳이 생긴다 |
| 구성 묶음 | 발행하지 않음 | 완료 때 이미 요구 없음(NOT_REQUIRED) 묶음이 나갔고 PLANNED 도 요구 없음이다 |
| 받는 작업 | 작업 날짜가 오늘(Asia/Seoul)인 것만, 예전 작업(daily=false) 포함 | 실행 기록(실제 시각)을 지우는 일이라 지난 기록은 바꾸지 않는다 |
| 제출한 TBM 보고서 | 있으면 409 `WORK_REPORT_SUBMITTED` | 제출본은 그 실행의 기록(서명·해시)이다 |
| 다른 작업 진행 중 | 409 `WORK_ALREADY_IN_PROGRESS`(`work_id`) | 되돌려도 시작할 수 없다(현장에 진행 중 하나) |
| 기록 | 이전 실제 시작·종료 시각과 요청자를 WARN 로그 | 표를 더하지 않는다(시연용, 화면 없음). 이벤트·저널은 건드리지 않는다(규칙 6) |
| 종료 미팅(wrap-up) 생성 중 | 409 `WORK_WRAP_UP_GENERATING`(그 작업의 TBM 보고서 `status` 또는 `wrap_up_status` 가 GENERATING) | 생성 결과가 되돌린 뒤 옛 사실로 양식을 채운다. 같은 `request_id`(`rpt-{id}-end`)라 구분할 장치가 없다. 예전 작업은 다음 종료가 `REPORT_GENERATING` 으로 건너뛴다 |
| 날짜 작업 종료 미팅 문구 | 잠기지 않은 TBM 보고서의 `form.review.ending` 을 `""` 로 비우고 `form_revision` +1 | `completeEnding` 은 비어 있을 때만 채운다. 비우지 않으면 다시 종료해도 첫 실행(예: 이벤트 0건) 문구가 남는다. 사람이 고친 문구도 첫 실행 것이라 함께 비운다. 낙관적 잠금 값을 올려 열린 양식 화면이 덮어쓰지 않게 한다 |
| 예전 작업 마무리(새 버전) | 그대로 | 다음 종료가 SUCCEEDED 판에서 새 버전을 만든다 |

## 되돌리지 않는 것 (잔여 위험)

- 첫 실행 동안의 LIVE 이벤트는 그대로 남는다. 작업 구간 집계(`event_counts`·마무리 facts)는 새 실제 시각 기준이라 첫 실행 이벤트는 빠진다.
- 완료 때 보관한 레이아웃 구역은 보관 기록으로 남고, 다시 시작하면 새 구역 버전이 생긴다.
- 실제 시각을 덮어써 첫 실행 기록은 로그에만 남는다.
- 자정 경계: 자정을 넘겨 완료한 날짜 작업은 작업 날짜가 어제라 되돌릴 수 없다. 23:59 에 되돌리고 자정 뒤에 시작하면 날짜 작업 시작이
  `WORK_NOT_TODAY` 로 막혀 PLANNED 로 남는다(시연 시간대에는 일어나지 않는다).
- 되돌린 뒤 다른 경로로 구성 묶음이 발행되면 `ppe_policy.effective_from_ms`(진행 중 작업이 없으면 마지막 실제 종료 시각)가 첫 실행
  종료보다 이른 값이 될 수 있다. 요구 값은 NOT_REQUIRED 그대로다.

## 남은 일

- 관제 앱 화면 단추는 만들지 않는다(필요해지면 별도 이슈).
