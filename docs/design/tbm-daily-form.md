# 날짜 작업 TBM 보고서 양식 칸·리더 계정 (S15P31A101-609)

작업 관리 새 흐름(하루 = 작업 하나, 2026-11-10 결정)의 서버 쪽 B5~B8 이다. 요청서는 추창만의 「작업 관리 새 흐름 — 서버·AI 요청」(11-11 검토 답변 반영본)이다.
관련 명세: 8.3(TBM 보고서), 11.3·11.4-5(NFR-14 개인정보), 14장(권한).

## 만든 것

| 항목 | 위치 |
|---|---|
| V51(처음 V49, -602 와 겹쳐 옮김): `report.form`·`form_revision`·`leader_user_id`, 상태 `PENDING`, `app_user.org`·`title` | `server/src/main/resources/db/migration/V51__tbm_daily_form.sql` |
| 양식 칸 모양·검사·빈 양식 | `server/.../domain/report/TbmForm.java` |
| 빈 보고서 만들기 `TbmReportService.createDailyBlank(workId, leaderUserId)` | `TbmReportService`·`TbmReportRepository.createBlank` |
| 양식 저장 `PUT /api/reports/tbm/{id}/form` (ADMIN) | `TbmReportController`·`TbmReportRepository.updateForm` |
| 조회 응답에 `form`·`form_revision`·`draft_status`·`leader` | `TbmReportRepository.view` |
| 작업내용 항목 `GET /api/work-tasks` | `server/.../domain/work/WorkTasks.java` |
| 계정 소속·직책 넣기 `admin set-profile --login <아이디> [--label] [--org] [--title]` | `global/command/SetProfileCommand.java`, `UserService.setProfile` |
| 시험 | `server/src/test/.../report/TbmDailyFormIntegrationTest.java` |

## 흐름

1. 날짜 작업을 만들 때(-610 `PUT /api/work-units/daily/{date}`) 같은 트랜잭션에서 `createDailyBlank` 를 부른다.
   보고서는 `report_version=1`, `status=PENDING`, 빈 양식, `leader_user_id`=요청 계정이다. 이미 있으면 그 보고서를 돌려준다(작업 행 잠금).
2. 화면은 양식을 통째로 `PUT .../form {form, form_revision}` 으로 저장한다. 서버는 아는 칸만 골라 다시 만들고(점검 문구는 서버 값으로 덮음) `form_revision` 을 1 올린다.
   새 `report_version` 을 만들지 않는다. 서명이 `report_version` 에 묶여 있어서 새 버전이 생기면 작업 전에 받은 서명이 무효가 되기 때문이다.
3. AI 초안(B9, S15P31A101-616)이 같은 행을 `GENERATING → SUCCEEDED·FAILED` 로 바꾸며 빈 칸만 채우고 채웠으면 `form_revision` 을 1 올린다.
   관리자 `POST /api/reports/tbm/{id}/form-draft`(202) 또는 세팅 완료(C17, -615)가 `TbmFormAiService.tryStartDraftForWork` 로 시작한다.
   PENDING·FAILED·SUCCEEDED 에서 시작할 수 있고 GENERATING 이면 409. 자세한 규칙은 `docs/design/S15P31A101-616-tbm-form-draft.md` '2단계 서버 연결'.
4. 작업 종료(B10, -616)는 날짜 작업 보고서에 새 버전을 만들지 않고 같은 행의 `review.ending` 이 비어 있을 때만 채운다(채우면 `form_revision` +1).

## 양식 칸

```json
{
  "info": { "date_time_ms": 1762750000000, "same_day": true, "task_name": "",
            "tasks": [{ "code": "SLAB_OPENING_COVER" }, { "text": "직접 쓴 작업" }],
            "place": "", "risk_assessed": null },
  "risks": [{ "risk": "", "measure": "" }, { "risk": "", "measure": "" }, { "risk": "", "measure": "" }],
  "focus": { "risk": "", "measure": "" },
  "checks": [{ "code": "PPE", "label": "안전모 등 보호구 착용 상태 확인", "yes": null, "action": "" }, "... 5줄"],
  "review": { "daily": "", "ending": "" }
}
```

- 리더·참석자는 양식에 넣지 않는다. 리더는 `leader{user_id,name,org,title}`(계정), 참석자는 `report_signature`.
- 점검 5줄 코드·순서: `PPE`, `ZONE_CONTROL`, `OPENING_GUARD`, `WORK_SURFACE_FALL`, `HEALTH`.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 빈 보고서 상태 | `status=PENDING`, 응답 `draft_status=null` | 요청서의 `draft_status` 는 GENERATING·SUCCEEDED·FAILED 뿐. 상태 칸을 하나 더 두지 않고 기존 `status` 를 넓혀, B9 가 기존 `completeSucceeded`(WHERE status='GENERATING')를 그대로 쓰게 했다 |
| 빈 보고서 일시 | 작업 날짜가 오늘·지난 날이면 만든 시각, 미래면 그날 07:30 Asia/Seoul | 요청서 B5 |
| 양식 동시 수정 | `form_revision` 낙관적 잠금, 다르면 409 `REPORT_FORM_CONFLICT` | 요청서 B6. 두 PC 가 같은 보고서를 열 수 있다 |
| 저장 거부 | 최신 버전이 아니면 409 `REPORT_VERSION_STALE`, `locked_at` 이 있으면 409 `REPORT_LOCKED` | 예전 버전은 고치지 않는다. 잠금 시점은 -611 에서 '첫 서명' → '제출' 로 옮긴다 |
| 빠진 칸 | 글자 칸은 `""`, 예·아니오 칸은 `null` 로 채운다. 모르는 칸은 버린다 | 통째 저장이라 화면 판이 조금 달라도 저장되게 했다. 줄 수(위험 3·점검 5)와 점검 코드·순서는 엄격히 본다 |
| 글자 길이 | 작업명·직접 쓴 작업 100자, 장소 200자, 위험·대책·조치 500자, 일일·종료 미팅 2000자 | 양식 한 장(A4)에 들어가는 정도. 넘으면 400 `INVALID_REPORT_FORM` |
| 작업내용 | 최대 20개, `{code}`(알려진 항목) 또는 `{text}`(앞뒤 공백 제거, 1~100자), 중복 금지 | 요청서 B7 |
| 작업내용 `rule_tags` | 슬래브 개구부 R1·R2·R5, 고소 작업면·거푸집·외벽 비계 R2·R3·R4, 철근 R1·R2·R4, 자재 R1·R5, 바닥 마감 R1·R4 | 명세 7장 규칙표(R1 구역 진입, R2 개구부·가장자리, R3 고소, R4 낙상, R5 진입 예측)와 기존 공종 태그 |
| 소속·직책 | `app_user.org`·`title` 1~40자, `admin set-profile` 로 넣음 | 계정 생성이 관리 명령뿐이라 같은 방식. 운영 계정 `admin1~6` 은 시드가 아니라 운영 DB 에 있다 |
| 기존 생성 경로 리더 | `POST /api/reports/tbm` 도 요청 계정을 `leader_user_id` 로 남긴다 | 예전 작업 보고서도 같은 응답 모양을 갖게 |

## 시험

`TbmDailyFormIntegrationTest`: 오늘·미래 빈 보고서, 중복 호출 하나, 양식 저장(버전 그대로·revision 증가·점검 문구 덮기·모르는 칸 버림), revision 충돌 409,
모양 오류 400(줄 수·모르는 작업·중복·점검 순서·길이·타입), VIEWER 403, 잠김 409, 예전 버전 409, 없는 보고서 404, 작업내용 7개, 소속 40자.
기존 `TbmReportIntegrationTest`·`ReportSignatureIntegrationTest` 도 통과.

## 남은 일

- -610: 날짜 작업 생성에서 `createDailyBlank` 호출, 양식의 작업내용(`info.tasks`)으로 작업 이름(첫 항목 + ' 외 N건') 만들기와 `name NOT NULL` 풀기.
- -611: 서명 조건(보고서 있음 + 제출 전), 첫 서명 잠금 없앰, 제출 때 잠금·`content_sha256`(form + 서명 목록).
- B9·B10: S15P31A101-616 2단계에서 했다(초안은 같은 행의 빈 칸만, 종료 미팅은 새 버전 없이 `review.ending`).
- 운영: `admin1~6` 계정에 `admin set-profile` 로 소속·직책을 넣는다.
- B9: 날짜 작업 초안은 `POST /api/reports/tbm`(새 버전 추가) 대신 `POST /api/reports/tbm/{id}/form-draft` 로 같은 행을 GENERATING 으로 바꿔 채운다(-616). `POST /api/reports/tbm` 은 레이아웃이 있는 예전 작업만 받고(`WORK_HAS_NO_LAYOUT`), 최신 보고서에 양식이 있으면 409 `REPORT_FORM_MANAGED` 로 새 버전을 만들지 않는다.
- 작업 종료의 새 버전은 이제 양식이 없는 예전 작업에만 생긴다(-616 B10). 그 경로의 `form` 복사는 보통 NULL 이고 `leader_user_id` 복사는 그대로다.
- -611: 잠금 해시(`content_sha256`)에 `form` 을 넣는다. 지금 해시는 sections·citations·wrap_up_summary 만 덮는다.
- 리더 이름(`display_label`)은 결재자 이름(`approved_by_name`)처럼 보고서를 읽을 수 있는 모든 역할에 보인다. `display_label` 은 화면 표시용 별칭이라 같은 규칙을 따랐다.
