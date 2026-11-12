# 새 모델 + World 이어짐 세팅의 구역 확인 (S15P31A101-629)

-615 세팅 완료(C17)의 후속이다. 관련 명세: 3.2(작업 시작 전 세팅), 8.2(구역), 12.6(세팅 REST).
앞선 약속: -612 세션 구역 목록(`docs/design/setup-session-zones.md`), -615 완료(`docs/design/S15P31A101-615-setup-complete.md`).

## 왜

새 모델(NEW)이라도 World 다시 맞추기가 채택되면(`world_anchor.accepted`) 지금 관제 구역 좌표를 그대로 쓸 수 있다. 다만 현장 변경이
작을 수도 클 수도 있으므로, 지금 구역을 미리 채워 두고 사람이 하나씩 확인·수정·삭제하게 한다(사용자 결정).

## 바꾼 흐름

| 세팅 | 구역 단계에 처음 들어올 때(목록 revision 0) | 확인 표시 |
|---|---|---|
| 이전 모델 재사용(REUSE) | 지금 관제 구역으로 채움(zone_id 유지, 본문 없음) | 없음 |
| 새 모델 + World 이어짐 | 지금 관제 구역으로 채움 | 각 항목 `needs_review: true` (레이아웃이 만든 구역 제외) |
| 새 모델 + World 끊김 | 채우지 않음(빈 목록) | - |

- 채우기는 서버가 한다: `POST /api/setup-sessions/{id}/zones/seed`(ADMIN). 저장한 적 없는 목록만 채우고, 이미 있으면 그대로 돌려준다.
  화면(`useSeedSetupZonePlan`)은 같은 World 세션의 빈 목록에서 한 번 부른다. 예전에는 화면이 `GET /api/zones` 로 받아 PUT 했다(REUSE 만).
- 확인: 「이어 쓴 구역 확인」 카드(`SetupZoneReviewCard`)가 확인 필요를 위로 정렬하고 개수를 보인다. 구역마다 확인·수정·삭제, 「남은 n개 모두 확인」.
  확인·삭제·일괄 확인은 목록 PUT(revision 잠금)이다. 수정은 그 구역을 골라 구역 편집 도구로 열고, 저장(새 버전·작업면)하면 확인된 것으로 바뀐다.
- 완료: 확인 필요가 남으면 마지막 단추가 `다음 · 확인 필요 n개`(단독 화면은 `작업 관리로 · …`)로 꺼진다. 서버도 409 로 거부한다.
- 「지금 구역 불러오기」 단추는 -615 설계 문서에만 있고 코드에는 없었다. 자동 채우기로 대신하므로 만들지 않았다.

## 저장 형태

`setup_session_zone_plan.items`(jsonb) 항목에 `needs_review`(boolean, 없으면 false)를 더했다. 마이그레이션은 없다(jsonb).
서버는 저장 때 `zone_id` 가 있고 `zone`·`surface` 가 없는 항목만 true 를 남긴다(새 구역·고친 구역은 확인된 것으로 본다).
계약(`contracts/`) 변경은 없다(세션 구역 목록은 계약 스키마가 없는 응답).

## 오류 코드

| 조건 | 코드 |
|---|---|
| 세팅 완료 때 확인 필요 항목이 남음 | 409 `SETUP_ZONES_UNCONFIRMED`, 본문 `unconfirmed_count`, `detail` 에 남은 개수 |

검사 순서: 적용 서비스(`SetupZoneApplier.apply`)가 목록을 잠근 뒤 World 불일치·STALE 검사 다음, 보관·적용 전에 본다.

## 3D 표시

화면 구역(`planZonesView`)과 3D 입력(`toViewerZones`)에 `needs_review: true` 를 싣고, 뷰어(`officialZones` → `ZoneView.needsReview`)가
같은 심각도 색으로 채움 0.35배·흐린 촘촘한 점선(0.06/0.14 m)·이름표 `· 확인 필요` 로 그린다. 좌표 변환은 건드리지 않았다.
뷰어 `CONTRACT.md`·`CHANGELOG.md` 에 더하기로 적었다.

## 명세에 없어 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 레이아웃이 만든 구역 | 확인 표시 없음 | 세팅 목록으로 고치지 못하는(레이아웃이 관리) 구역이라 확인을 강제하면 풀 수 없는 경우가 생긴다 |
| 채우기 위치 | 서버(seed) | 모드·World 판정을 서버가 알고, 같은 트랜잭션에서 비었는지 보고 채운다 |
| 채울 구역이 100개 초과 | 채우지 않음(경고 로그) | 목록 상한(-612). 반만 채우면 완료 때 나머지가 보관된다 |
| 확인 카드 노출 | 새 모델 + World 이어짐(또는 확인 필요가 남았을 때) | 재사용은 확인 표시가 없다 |

## 시험

- 서버: `SetupZonePlanIntegrationTest`(채우기 3종, 확인·수정·일괄 확인 저장, 미확인 남으면 적용 거부), `SetupCompleteIntegrationTest`(HTTP 409·`unconfirmed_count`, 확인 뒤 완료).
- 프론트: `setupZonePlan.test.tsx`, `SetupPage.test.tsx`(채우기·카드·단추 끄기·서버 거부 안내), `setupActions.test.ts`, `zoneGeometry.test.ts`, `officialGeometry.test.ts`, `mocks/setupWorkFlow.test.ts`.

## 잔여 위험·넘긴 일

- 2D 비상 평면(`features/plan`)은 확인 전 구역을 구별하지 않는다.
- 구역 편집 도구의 고르기 드롭다운은 확인 필요를 표시하지 않는다(카드에서 본다).
- 확인은 "사람이 봤다"는 표시일 뿐 좌표가 맞는지는 판정하지 않는다.
- 개발 목업은 새 세션을 World 이어짐으로 두어(`worldAnchorAccepted`) 이 흐름을 보인다.
- 명세 12.6 세팅 REST 표에 `POST /api/setup-sessions/{id}/zones/seed` 와 `SETUP_ZONES_UNCONFIRMED` 를 더한다(명세 정리 MR).
