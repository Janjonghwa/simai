# TBM 서명은 작업 전에, 잠금은 제출 때 (S15P31A101-611)

작업 관리 새 흐름의 서버 쪽 B11~B12 다. 요청서는 추창만의 「작업 관리 새 흐름 — 서버·AI 요청」(11-11 검토 답변 반영본)이다.
관련 명세: 8.3(TBM 보고서·전자서명), 11.4-5(NFR-14 서명 실명 예외), 14장. 앞 이슈: -609(양식 칸), -610(날짜 작업).

## 바뀐 흐름

| 단계 | 전 | 후 |
|---|---|---|
| 서명 받는 때 | 최신 버전 + `SUCCEEDED` + 결재 전 | 보고서가 있고 최신 버전 + 제출 전 + 결재 전(초안 상태와 무관) |
| 첫 서명 | `locked_at`·`content_sha256` 을 기록해 잠금 | 잠그지 않는다. 서명마다 그때 내용 해시만 `report_signature.report_content_sha256` 에 남긴다 |
| 고지 확인 | `notice_acknowledged=true` 필수(400) | 선택. 받은 값(없으면 false)을 남긴다 |
| 서명 지우기 | 없음 | `DELETE /api/reports/tbm/{id}/signatures/{signatureId}` (ADMIN): 작업 `PLANNED` 이고 제출 전 |
| 잠금 | 첫 서명 | `POST /api/reports/tbm/{id}/submit` (ADMIN): 작업 `DONE` 이고 최신 버전이고 제출 전 |
| 제출 뒤 | — | 양식 수정·서명·서명 지우기·다시 제출이 409 `REPORT_LOCKED` |

## 만든 것

| 항목 | 위치 |
|---|---|
| V56: `report.submitted_at`·`submitted_by`, `report_signature.legal_notice_ack` CHECK 제거 | `server/src/main/resources/db/migration/V56__tbm_submit.sql` |
| 서명 조건·해시, 서명 지우기 | `ReportSignatureController.sign`·`delete` |
| 제출 | `TbmReportController.submit` → `TbmReportService.submit` → `TbmReportRepository.submit` |
| 해시 규칙(키 정렬 JSON 의 SHA-256) 공용 | `ReportHash` (전에는 서명 컨트롤러 안에 있었다) |
| 응답: 보고서 `submitted_at`·`submitted_by_name`, 작업 목록 `tbm_report.submitted_at`·진행 칩 `SUBMITTED` | `TbmReportRepository.view`, `WorkUnitRepository` |
| 시험 | `TbmSubmitIntegrationTest`(새), `ReportSignatureIntegrationTest`(바뀐 전제 반영) |

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 서명 해시 내용 | `form`·`sections`·`citations`·`wrap_up_summary`(키 정렬 JSON) | 날짜 작업은 양식이 참석자가 서명한 기록이다. 예전 보고서는 `form` 이 null 로 들어간다 |
| 제출 해시 내용 | `form`·`sections`·`citations`·`wrap_up_summary` + 이 작업 TBM 보고서의 서명 목록(`signature_id`·`report_version`·`signer_name`·`signer_role`·`image_sha256`·`signed_at_ms`, id 순) | 요청서 B12(양식 + 서명 목록). 작업 종료가 새 버전을 만드는 동안(B10 전) 서명은 이전 버전 행에 붙어 있으므로 작업의 모든 버전 서명을 넣는다 |
| 제출 조건 | 작업 `DONE`(아니면 409 `WORK_INVALID_STATE`), 최신 버전(아니면 409 `REPORT_VERSION_STALE`), 초안·종료 미팅 생성 중이 아님(아니면 409 `REPORT_NOT_READY`) | 요청서 B12. 잠근 뒤 생성이 행을 바꾸지 않게 |
| 제출 뒤 새 버전 | `POST /api/reports/tbm` 이 409 `REPORT_LOCKED` | 잠긴 보고서 뒤에 잠기지 않은 버전이 생기지 않게 |
| 서명 지우기 조건 | 작업 `PLANNED`(아니면 409 `WORK_INVALID_STATE`), 제출 전, 최신 버전(아니면 409 `REPORT_VERSION_STALE`), 참석자 서명만(결재 서명·없는 서명 404 `SIGNATURE_NOT_FOUND`) | 요청서 B11(작업 시작 전). 결재 서명은 `report.approved_at` 과 짝이다 |
| 지운 서명 | 행을 지우고 서명 이미지 객체는 남긴다 | 잘못 받은 서명이라 기록으로 둘 이유가 없다. 객체 정리는 저장소 보관 정책에 맡긴다 |
| 결재(APPROVER) | 서버 기능은 그대로 두고 잠그지 않는다 | 요청서: 화면에서 쓰지 않음(서버 기능은 남겨도 됨) |
| 예전 보고서 | 이미 첫 서명으로 `locked_at` 이 기록된 행은 그대로 잠긴 것으로 보고, 제출해도 `locked_at`·`content_sha256` 을 덮어쓰지 않는다(제출 기록만 더함) | 기록을 바꾸지 않는다(규칙 6) |

## 시험

- `TbmSubmitIntegrationTest`: 서명 지우기(권한 403·없는 서명 404·작업 시작 뒤 409), 제출(작업 종료 전 409·권한 403·기록·진행 칩 `SUBMITTED`), 제출 뒤 양식·서명·지우기·다시 제출 409 `REPORT_LOCKED`, 서명 목록이 다르면 해시가 다름.
- `ReportSignatureIntegrationTest`: 서명은 잠그지 않고 서명 행에 해시를 남김, 고지 확인 없이도 서명, 초안 상태(GENERATING)와 무관하게 서명, 결재는 잠그지 않음.

## 남은 일

- 명세 8.3 은 '첫 서명이 버전을 잠근다', '과거 서명은 보존한다'고 적혀 있다. 11-10 결정(작업 관리 새 흐름)으로 잠금을 제출로 옮기고 작업 전 서명 지우기를 더했으므로 명세 정리 MR 에서 8.3 을 고친다(`docs/spec/README.md` 갱신 규칙).
- 지금 관제 앱 서명 패널(`front/.../tbm/types.ts` `signatureResultSchema`)은 서명 응답의 `locked_at` 을 숫자로 기대한다. 이제 제출 전에는 null 이므로 새 화면(추창만)에서 null 을 받게 해야 한다.

- B10(박종화): 작업 종료가 새 버전을 만들지 않게 되면 서명·양식·제출이 한 행에 모인다. 그때 제출 해시의 '모든 버전 서명'은 한 버전 서명과 같아진다.
- 화면(추창만): 첫 서명 뒤에도 양식을 고칠 수 있고, 제출 버튼은 작업 종료 뒤에만 켠다.
