# 세팅 중 구역 목록과 세팅 완료 때 적용 (S15P31A101-612)

작업 관리 새 흐름의 서버 쪽 C16 이다. 요청서는 추창만의 「작업 관리 새 흐름 — 서버·AI 요청」(11-11 검토 답변 반영본)이다.
관련 명세: 8.2(구역), 5.3(작업면), 12.4(구성 묶음). 세팅 완료(C17)는 박종화가 만들고 이 이슈의 적용 서비스를 부른다.

## 왜

지금은 세팅 화면에서 AI 초안을 수락할 때마다 `POST /api/zones/commit` 으로 바로 관제 구역이 되고 구성 묶음이 나간다(V47 `setup_session_zone` 은 세션 연결만).
새 화면은 AI 위험구역을 처음부터 구역으로 깔아 두고 고치고 더하고 지운 뒤, 세팅 완료 때 한 번에 관제에 적용한다. 세팅 중에는 관제에 나가지 않아야 한다.

## 만든 것

| 항목 | 위치 |
|---|---|
| V57: `setup_session_zone_plan(setup_session_id PK, items jsonb, revision, updated_by, updated_at)` | `server/src/main/resources/db/migration/V57__setup_session_zone_plan.sql` |
| `GET`·`PUT /api/setup-sessions/{id}/zones` (ADMIN) | `zone/SetupZonePlanController`, `zone/SetupZonePlanService` |
| 세팅 완료 때 적용 `SetupZoneApplier.apply(setupSessionId, sameWorld, actorUserId)` | `zone/SetupZoneApplier` |
| 기존 확정 경로를 묶음 발행 없이 부르기 | `ZoneCommitService.applyInTransaction` |
| 저장 전 값 검사 | `ZoneRepository.validate`, `StaticObjectRepository.validate` |
| 시험 | `server/src/test/.../zone/SetupZonePlanIntegrationTest.java` |

## 목록 항목

```json
{ "items": [
    { "zone_id": "z-…", "source": "AI", "candidate_id": "cand-1", "zone": null, "surface": null },
    { "zone_id": "z-…", "source": "AI", "candidate_id": null, "zone": { "...zones/commit 의 zone body" }, "surface": null },
    { "zone_id": null, "source": "MANUAL", "candidate_id": null, "zone": { "..." }, "surface": { "...작업면 body" } }
  ],
  "revision": 3 }
```

| 항목 | 완료 때 |
|---|---|
| `zone_id` 있음, `zone` null | KEEP(그대로). `surface` 가 있으면 작업면만 바꾼다 |
| `zone_id` 있음, `zone` 있음 | UPDATE(같은 `zone_id` 의 새 버전) |
| `zone_id` 없음 | CREATE(`setup_session_zone` 에 세션 연결) |
| 목록에 없는 지금 관제 구역 | ARCHIVE(이은 작업면도) |
| `surface` 있음 | 그 구역에 이은 작업면이 있으면 UPDATE, 없으면 CREATE |

## 적용 서비스 약속(C17)

- 세팅 완료 트랜잭션 안에서만 부른다(`Propagation.MANDATORY`). 밖에서 부르면 예외다.
- 먼저 구역 저장(`zones/commit`)과 같은 잠금을 잡고, 세션이 작업에 지금 걸린 세션이며 `READY_FOR_ZONES`·`BAKING_TEXTURE`·`COMPLETED` 이고 작업이 `PLANNED` 인지 본다(아니면 409 `SETUP_ZONES_CLOSED`). 그래서 `setup_completed_at` 은 apply 뒤에 기록한다.
- 구성 묶음은 만들지 않는다. 세팅 완료가 Scene·보정과 함께 한 번 발행한다.
- `sameWorld` 는 부르는 쪽이 정한다: 이전 모델 쓰기(`REUSE_SCENE`)이거나 `world_anchor.accepted == true`.
  false 인데 목록에 기존 `zone_id` 가 있으면 409 `SETUP_ZONE_WORLD_MISMATCH`. 기존 구역 좌표가 새 3D 와 맞지 않기 때문이다.
- 목록의 `zone_id` 가 더 이상 지금 관제 구역이 아니면 409 `SETUP_ZONE_STALE`.
- 돌려주는 값: `ApplyResult(created, updated, kept, archived)` (zone_id 목록).

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 저장 단위 | 세션당 한 행, 목록 통째, `revision` 낙관적 잠금(다르면 409 `SETUP_ZONE_PLAN_CONFLICT`) | 요청서 C16(통째로 저장). 다른 PC·새로고침에서도 이어진다 |
| 고칠 수 있는 때 | 작업에 지금 걸린 세션(`active_setup_session_id`)이고, 실패(취소 포함)가 아니고, 작업 `PLANNED`, 세팅 완료 전. 아니면 409 `SETUP_ZONES_CLOSED`. 세션·작업 행을 잠가 세팅 완료와 줄을 세운다 | 완료 뒤 구역 수정은 `zones/commit` 으로 한다(요청서 C17) |
| 항목 상한 | 100개, `zone_id`·`candidate_id` 중복 금지 | 교실 현장 구역 수보다 넉넉하다 |
| 값 검사 | 저장 때 구역·작업면 본문을 만들 때와 같은 규칙으로 검사(다각형 꼬임·높이 범위 등) | 세팅 완료 때 처음 실패하지 않게 |
| KEEP·UPDATE 구분 | 화면이 고친 구역만 `zone` 본문을 보낸다 | 서버가 본문을 비교하지 않아도 되고, 고치지 않은 구역에 새 버전이 생기지 않는다 |
| 레이아웃이 만든 구역 | 보관 대상에서 빼고, 목록에 그대로(본문 없음) 있으면 KEEP 으로 둔다 | 레이아웃 구역은 `LayoutZoneService` 가 관리한다(`LAYOUT_ZONE_LOCKED`) |
| 위치 | `zone` 패키지 | 세팅(`setup`) 패키지는 C13·C17 로 박종화가 고치는 중이라 충돌을 피했다 |

## 남은 일

- C17(박종화): 세팅 완료 트랜잭션에서 `SetupZoneApplier.apply` 를 부르고, 같은 트랜잭션에서 구성 묶음을 한 번 발행한다.
- 화면(추창만): 세팅 구역 화면이 수락마다 `zones/commit` 대신 이 목록을 저장한다. 이전 모델 쓰기는 `GET /api/zones` 로 처음 목록을 채운다.
- 저장 때마다 모든 구역 다각형을 다시 검사한다(구역당 DB 한 번, 100개 이하). 느려지면 바뀐 항목만 검사한다.
- 작업면만 지우기(구역은 두고)는 목록으로 나타낼 수 없다. 필요하면 `surface: {"remove": true}` 같은 표시를 더한다.
