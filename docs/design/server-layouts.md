# 가상 레이아웃 API (S15P31A101-30)

TBM 계획에서 놓는 가상 개구부·가설 비계·장비(PLANNED)를 저장한다. 명세 8.3 F-02, 12.6 `/api/layouts`, 11.1 `layout_plan`.
계산은 이 API 가 하지 않는다. M3 에서는 TBM 보고서(`POST /api/reports/tbm`, S15P31A101-33·-101)가 `layoutId` 로 이 레이아웃을 읽어
Risk Engine 컨테이너에 PLANNED 계산을 요청한다. 단독 평가 `/api/layouts/{id}/evaluate` 는 확장(부록 C.4)이라 두지 않았다
(박종화 2026-10-21 범위 의견과 같음).

## 코드

| 파일 | 하는 일 |
|---|---|
| `server/src/main/resources/db/migration/V23__layout_plan.sql` | 표(db-tables 정의 그대로) |
| `server/.../domain/layout/LayoutController.java` | `GET·POST /api/layouts`, `GET·PUT /api/layouts/{id}` |
| `server/.../domain/layout/LayoutRepository.java` | 검사, 저장, revision |
| `server/.../domain/layout/LayoutRequest.java` | 본문 |
| `server/src/test/java/.../domain/layout/LayoutIntegrationTest.java` | 통합 시험 |

## 요청·응답

```json
{ "name": "2층 비계 계획", "revision": 1,
  "items": [ { "item_id": "s-1", "kind": "SCAFFOLD", "center": [6.0, 4.0, 1.6], "size": [6.0, 1.2, 3.2],
               "yaw_rad": 0.0, "label": "비계" } ] }
```

응답(하나): `{layout_id, name, revision, layout_version: "lv-{revision}", source: "PLANNED", items, created_at, updated_at}`.
목록은 `items` 대신 `item_count`. 시각은 epoch ms.

## 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 권한 | 목록·만들기·고치기 관리자, 하나 조회 열람 이상 | 명세 12.6 표 |
| 항목 형식 | `kind` OPENING·SCAFFOLD·EQUIPMENT, OBB(`center`·`size`·`yaw_rad`), `label`, 화면이 정하는 `item_id` | 관제 앱 `viewer3d/planned/plannedLayout.ts`(S15P31A101-186)와 같은 모양. Risk Engine 계약(`risk_plan_request.layout` 의 zones·work_surfaces)으로 바꾸는 일은 계산을 요청하는 쪽(-101)이 한다 |
| 수정 | `layout_plan` 은 지금 revision 을 바꾸고 `revision` 을 1 올림. 각 revision 의 이름·항목은 `layout_plan_revision` 에 추가만 하고 `GET /api/layouts/{id}/revisions/{revision}` 으로 읽음. 응답 `layout_version` 은 `lv-{revision}` | db-tables `layout_plan.revision`("PUT 마다 증가, 보고서는 계산한 revision 을 참조")과 계약 `layout_version`("덮어쓰지 않고 새 버전을 더한다"). 보고서가 계산한 배치를 나중에도 다시 본다 |
| 동시 수정 | 본문 `revision`(고치기 시작한 값)이 있으면 지금 값과 같을 때만 바꿈, 아니면 409. 비교와 바꾸기를 한 문장으로. 응답은 같은 트랜잭션 안에서 다시 읽음 | 두 관리자가 같은 계획을 고칠 때 한쪽 변경이 조용히 사라지지 않게. 없으면 검사하지 않음(지금 화면 호환, 아래 남은 일) |
| 검사 | 이름 1~100자, 항목 200개 이내, `item_id` 1~64자 겹치지 않게, 수는 유한·절댓값 10 km 이내, `size` > 0, `yaw_rad` 절댓값 π 이내(관제 앱 `YAW_LIMIT_RAD`), `label` 100자. 앞뒤 공백 제거·코드 포인트로 셈·NUL 거부 | 관제 앱 검사와 같은 범위. 도 단위 각도·잘못 넣은 큰 좌표를 걸러 낸다 |
| 삭제 | 두지 않음 | 명세 12.6 에 없다. 보고서가 레이아웃을 가리킨다 |

## 시험

`LayoutIntegrationTest`(MockMvc, 임시 DB):
- 만들기(공백 정리·label null·revision 1·lv-1), 열람 계정 조회, revision 1 로 고치기 → 2, 낡은 revision → 409, revision 없이 → 3, 목록은 개수만
- 열람 계정 목록·만들기·고치기 403, 미인증 401, 없는 레이아웃 404
- 이름 공백·101자·NUL, items 없음·빈 본문, 201개, ID 겹침·빈 ID, 모르는 kind, size 0·음수, yaw 3.2, 좌표 20 km, label 101자, center 2개, yaw 없음, revision 0 → 400. 200개·100자·yaw ±π 경계 통과

## 남은 일

- 관제 앱 연결(추창만): 계획 화면 저장을 이 API 로(지금은 화면 상태뿐).
- TBM 보고서(-33·-101)가 이 레이아웃을 `risk_plan_request.layout` 으로 바꿔 계산을 요청한다.
- V23 은 열린 MR !322 의 V22(`static_object.zone_id`) 다음 번호다. !322 가 먼저 머지돼야 Flyway 순서가 맞는다(반대로 머지·배포되면 V22 를 늦게 적용할 수 없어 기동이 멈춘다).
- 관제 앱이 저장할 때 `revision` 을 보내게 되면 필수로 바꾼다. 지금은 빼면 검사하지 않아 동시 수정 때 나중 저장이 이긴다.
- 본문 JSON 타입이 틀리면(`items` 가 글자 등) 다른 API 처럼 Spring 기본 400 이 나가고 `code` 가 없다(전역 처리기 공통 과제).
