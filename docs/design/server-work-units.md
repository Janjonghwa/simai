# TBM 작업 저장·목록 API

히스토리 화면(S15P31A101-320)은 `GET /api/work-units` 로 TBM 작업 목록을 받아 작업 구간을 다시 본다. 서버에 TBM 작업을 둘 곳이 없어
표 `work_unit`(V21)과 저장·목록 API 를 만들었다. 읽기만 만들면 늘 빈 목록이라 등록·수정·삭제까지 둔다(곽나혁 결정 2026-11-02).
TBM 화면(추창만)의 작업은 지금 데모 상수와 브라우저 localStorage 에 있으므로, 화면이 이 API 로 저장하도록 바꾸면 이어진다.

## 근거

| 출처 | 내용 |
|---|---|
| 관제 앱 `front/apps/admin/src/features/history/types.ts` | `workUnitSchema`(work_id·date·name·from_ms·to_ms·zone_id·place·planned_people·event_counts) — 응답 모양을 맞췄다 |
| 관제 앱 `features/history/api.ts` | `GET /api/work-units?days=7` |
| NFR-14 | 작업자 이름을 두지 않는다. 담당은 조 이름만 |
| 명세 | 이 API 는 명세 12.6 표에 아직 없다(남은 일) |

## 코드

| 파일 | 하는 일 |
|---|---|
| `server/src/main/resources/db/migration/V21__work_unit.sql` | 표 |
| `server/.../domain/work/WorkUnitController.java` | `GET /api/work-units?days=`, `GET /{id}`, `POST`, `PUT /{id}`, `DELETE /{id}` |
| `server/.../domain/work/WorkUnitRepository.java` | 검사, 저장, 조회와 이벤트 수 |
| `server/.../domain/work/WorkUnitRequest.java` | 본문 |
| `server/src/test/java/.../domain/work/WorkUnitIntegrationTest.java` | 통합 시험 |

## 요청·응답

본문(POST·PUT 같음, PUT 은 전체 바꾸기):

```json
{ "date": "2026-11-02", "name": "양중 작업", "from_ms": 1793570400000, "to_ms": 1793577600000,
  "zone_id": "z-04", "place": null, "planned_people": 3, "lead": "B조" }
```

응답 작업 하나(목록은 `{"works": [...]}`):

```json
{ "work_id": "12", "date": "2026-11-02", "name": "양중 작업", "from_ms": 1793570400000, "to_ms": 1793577600000,
  "zone_id": "z-04", "place": "개구부 구역", "planned_people": 3, "lead": "B조",
  "event_counts": { "CRITICAL": 1, "HIGH": 2 } }
```

## 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 권한 | 쓰기 관리자, 읽기 로그인한 모든 역할 | 구역 API(`/api/zones`)와 같음 |
| 날짜·시각 | 화면이 `date`(현장 시각 YYYY-MM-DD)와 `from_ms`·`to_ms`(epoch ms)를 함께 보낸다 | 서버에 현장 시간대 설정이 없다. 화면이 현장 시각을 안다 |
| 작업 길이 | `from_ms < to_ms`, 24시간 이내, `to_ms` 는 9999년 말까지 | 하루 작업. 잘못 넣은 큰 구간이 리플레이를 무겁게 하지 않게 |
| 목록 범위 | 끝난 지 `days` 일(기본 7, 1~90)이 안 된 작업, 늦게 시작한 것부터 | 화면은 `days=7`. 시간대 없이 시각으로 자른다 |
| `event_counts` | 구간 [from, to] 에 시작한(`raised_at`) LIVE 이벤트의 CRITICAL·HIGH 수. 구역 작업은 그 구역만, 구역 밖 작업은 현장 전체 | 다시 보기는 그 구간의 현장 전체를 보인다. 구역 작업은 그 구역 위험이 관심사 |
| `place` | 저장한 장소가 없으면 구역의 최신 이름 | 화면은 place 를 "구역 이름 또는 장소"로 보인다 |
| 구역 검사 | 이 현장에 그 zone_id 의 버전이 하나라도 있으면 됨(보관된 구역 포함) | 지난 작업의 구역이 보관돼도 고칠 수 있게. zone 키가 (zone_id, zone_version)이라 FK 를 걸 수 없다 |
| 회의록 내용 | 두지 않는다(작업 목록만) | 위험성 평가·대책 문장은 분석 워커 `tbm_report`(RAG + 외부 LLM, `report` 표)가 쓴다(박종화 담당). 필요하면 보고서를 `work_id` 로 잇는다 |
| 글자 | 앞뒤 공백 제거, 코드 포인트로 이름·장소 100자·조 50자, NUL 거부 | PostgreSQL text·jsonb 는 NUL 을 받지 않는다 |
| `work_id` | 응답에서 글자 | 화면 스키마가 `z.string()` |
| `contract` 칸 | 넣지 않는다 | 화면의 `contract: 'DEMO_UNCONFIRMED'` 는 서버가 없을 때의 가정 표시다. 서버 연결 때 걷어 낸다 |

| 인덱스 | `risk_event` 에 LIVE 부분 인덱스 `(zone_id, raised_at)`·`(raised_at)` | 목록이 작업마다 구간 이벤트를 센다. 기존 인덱스는 `(lifecycle, severity)` 뿐 |

## 시험

`WorkUnitIntegrationTest`(MockMvc, 임시 DB):
- 등록·조회·전체 바꾸기·삭제, 구역 이름으로 채운 place, 구간 안 이벤트 수(끝 시각 포함, 다른 구역·구간 밖·REPLAY 제외), 구역 밖 작업은 현장 전체
- days 범위로 지난 작업 빼기, days 0·91 은 400
- 반장 쓰기 403, 미인증 401, 이름 공백·101자, 날짜 형식·없는 날짜, 시각 역순·같음·25시간, 인원 -1·101·없음, NUL, 장소 101자·조 51자, 9999년 넘는 시각, 빈 본문·본문 없음 400, 고칠 때 없는 구역 400, 없는 구역 400, 100자·24시간·100명 경계 통과, 없는 작업 404

## 남은 일

- 관제 앱 연결(추창만): TBM 화면 저장을 이 API 로 바꾸고, 히스토리 응답 스키마의 `contract` 를 걷어 낸다. 걷어 내기 전에는 응답이 그 스키마(`contract` 필수)를 통과하지 못한다.
- `date` 와 `from_ms` 가 서로 맞는지는 보지 않는다(서버에 현장 시간대가 없다). 화면이 함께 만든다.
- 명세 12.6 REST 표와 `docs/requirements.md` 에 이 API 를 더하는 일(명세 정리 차례에).
- 같은 작업을 두 관리자가 동시에 고치면 나중 저장이 이긴다(버전 검사 없음). 관리자 한두 명이라 두었다.
