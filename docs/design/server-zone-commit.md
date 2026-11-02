# 구역↔작업면 연결과 한 번 저장 (S15P31A101-397)

관제 앱 구역 편집은 구역 → 작업면 → 구성 묶음 발행을 요청 3~4개로 나눠 보내, 중간에 실패하면 일부만 저장됐다
(front `features/zones/zoneCommit.ts`). 서버의 구역에는 작업면을 잇는 칸이 없어 화면이 "작업면 OBB 중심이 든 가장 작은 구역"으로
연결을 계산했다(`zoneSurfaceLink.ts`). 연결을 서버에 두고, 구역·작업면 저장과 묶음 만들기를 한 트랜잭션으로 하는 API 를 더했다.

## 코드

| 파일 | 하는 일 |
|---|---|
| `server/src/main/resources/db/migration/V22__static_object_zone.sql` | `static_object.zone_id`, 기존 작업면 채우기, 지금 버전 중 구역당 하나(부분 유일 인덱스) |
| `server/.../domain/geometry/StaticObjectRepository.java` | `zone_id` 검사·저장·응답, `linkedTo`·`zoneOf` |
| `server/.../domain/zone/ZoneCommitService.java`·`ZoneCommitRequest.java` | 요청 모양 검사, 트랜잭션 안 구역 → 작업면 |
| `server/.../domain/config/ConfigBundleService.java` | `publishWith`: 바꾸기와 묶음 만들기를 한 트랜잭션, 송신은 커밋 뒤 |
| `server/.../domain/zone/ZoneController.java` | `POST /api/zones/commit`(관리자) |
| `server/src/test/java/.../domain/zone/ZoneCommitIntegrationTest.java` | 통합 시험 |

## `POST /api/zones/commit`

```json
{
  "zone":    { "op": "CREATE | UPDATE | ARCHIVE | KEEP", "zone_id": "z-04", "body": { "...": "ZoneRequest" } },
  "surface": { "op": "CREATE | UPDATE | REPLACE | ARCHIVE", "object_id": "ws-1", "body": { "...": "StaticObjectRequest" } },
  "publish": true
}
```

| zone.op | 필요한 것 | 하는 일 |
|---|---|---|
| CREATE | body | 새 구역 |
| UPDATE | zone_id, body | 새 버전 |
| ARCHIVE | zone_id (surface 없음) | 이은 작업면을 보관하고 구역을 보관 |
| KEEP | zone_id, surface | 구역은 그대로(잠그기만), 작업면만 바꿈 |

| surface.op | 필요한 것 | 하는 일 |
|---|---|---|
| CREATE | body | 새 작업면을 이 구역에 잇는다 |
| UPDATE | object_id, body | 새 버전, 이 구역에 잇는다 |
| REPLACE | object_id, body | 옛 작업면 보관 뒤 새 작업면을 잇는다(화면의 "지우고 다시 추가") |
| ARCHIVE | object_id | 보관 |

응답: `{zone_id, zone(새 버전|null), surface(새 버전|null), archived_surface_id, bundle(만든 묶음|null)}`. 상태 200.

## 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 원자성 | 구역·작업면·묶음(`config_bundle*`)을 한 트랜잭션. 어느 단계든 실패하면 모두 되돌림. Tier 0 송신은 커밋 뒤 | 일부 저장을 막는 것이 목적. 송신을 트랜잭션 안에서 하면 되돌린 묶음을 Tier 0 가 받을 수 있다 |
| 묶음 규칙 프로필 | 이전 묶음의 것을 이어 씀 | 구역 편집은 프로필을 바꾸지 않는다. 바꿀 때는 기존 `POST /api/config-bundles` |
| `publish` | 기본 true, false 면 묶음 없이 저장만 | 여러 구역을 고친 뒤 한 번 발행하는 흐름도 남긴다 |
| 연결 수 | 지금 버전 중 구역당 작업면 하나(DB 부분 유일 인덱스 + 409 `SURFACE_ZONE_TAKEN`) | 관제 앱이 구역마다 작업면 하나로 다룬다 |
| 잇는 대상 | 작업면만, 이 현장의 지금 구역만(아니면 400 `INVALID_SURFACE_ZONE`) | 장애물은 구역에 속하지 않는다 |
| 기존 `PUT /api/static-objects/{id}` | `zone_id` 를 빼면 이전 연결 유지, 빈 글자면 풀기. 이어받을 구역이 이미 보관됐거나 작업면을 장애물로 바꾸면 조용히 푼다 | 지금 관제 앱 저장이 연결을 지우지도, 예전 `DELETE /api/zones` 뒤에 막히지도 않게 |
| 다른 구역의 작업면 | 지금 구역에 이어진 작업면은 다른 구역 저장으로 고치거나 가져오지 않음(409). 보관된 구역에 걸린 작업면은 가져올 수 있음 | 다른 구역 연결이 몰래 바뀌지 않게 |
| 잠금 순서 | 모든 `/commit` 경로가 구역 → 작업면(ARCHIVE 도 구역을 먼저 잠금) | 서로 다른 순서로 잠그면 교착 |
| 동시 연결 경합 | 검사 뒤 다른 요청이 먼저 이으면 부분 유일 인덱스 위반을 409 `SURFACE_ZONE_TAKEN` 으로 바꾼다 | 500 대신 같은 오류 코드 |
| 기존 데이터 | V22 가 지금 작업면을 관제 앱 규칙(중심을 덮는 가장 작은 지금 구역, 한 구역에 작업면 ID 순 첫 번째)으로 채움. 지난 버전은 비움 | 배포 뒤 화면이 보던 연결과 같게 |
| 구역 보관 때 작업면 | 같이 보관 | 관제 앱의 삭제 흐름(`commitArchive`)과 같다 |

## 시험

`ZoneCommitIntegrationTest`(실제 포트, 임시 DB):
- 새 구역 + 새 작업면 → 이어짐, 묶음 항목에 둘 다
- 실패 되돌림: 작업면 값 오류(400)·구역당 하나 위반(409) 때 구역 새 버전·작업면·묶음 수가 그대로
- KEEP + UPDATE, 예전 PUT 의 연결 유지·풀기·다시 잇기, REPLACE, 구역 ARCHIVE 때 작업면도 보관하고 묶음에서 빠짐
- 반장 403, 요청 모양 반례 400, 다른 구역 작업면 409, 없는 구역 404, 장애물 연결·없는 구역 400, 구역당 하나 409

기존 `ConfigBundleIntegrationTest` 도 다시 통과했다.

## 남은 일

- 관제 앱 연결(추창만): `zoneCommit.ts` 의 요청 3~4개를 이 API 한 번으로, `zoneSurfaceLink.ts` 계산을 응답의 `zone_id` 로 바꾼다.
- 명세 12.6 REST 표에 `POST /api/zones/commit` 추가(명세 정리 차례에).
- 연결은 저장된 값이 기준이다. 구역 다각형을 고쳐 작업면 중심이 밖으로 나가도 연결은 그대로다. 관제 앱이 아직 기하로 계산하는 동안은 화면과 어긋날 수 있다.
- `publish` 기본 true 라 묶음을 만들 수 없으면(보정 문서 없음, 공개 주소 미설정) 구역 저장도 되돌린다. 그런 현장에서는 `publish: false` 로 저장만 한다.
- 시험 없음: 같은 구역에 대한 동시 요청(잠금 순서·409 변환은 코드로만 확인), V22 기존 데이터 채우기(빈 DB 에서 문법만 확인), Tier 0 송신 시점.
- 예전 `DELETE /api/zones/{id}` 는 이은 작업면을 보관하지 않는다(그대로 둠). 화면이 `/commit` 의 ARCHIVE 로 옮기면 함께 보관된다.
