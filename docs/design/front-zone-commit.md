# 구역 편집 저장을 `/api/zones/commit` 한 번으로 (S15P31A101-410)

관제 앱 구역 편집은 구역 → 작업면(하나씩) → 구성 묶음 발행을 요청 3~4개로 나눠 보내, 중간에 실패하면 일부만 저장됐다. 구역과 작업면의
연결도 서버에 칸이 없던 때처럼 화면이 "작업면 OBB 중심이 든 가장 작은 구역"으로 계산했다. 서버가 한 트랜잭션으로 저장하는
`POST /api/zones/commit` 과 작업면 `zone_id` 를 넣었으므로([server-zone-commit.md](server-zone-commit.md), S15P31A101-397),
화면 저장을 요청 한 번으로 바꾸고 연결을 서버 값으로 읽는다.

## 관련 명세

- 3.2 구성 묶음, 6.2 동기화, 12.6 REST: 구역·작업면은 새 버전을 더하고(FR-16 덮어쓰지 않는다), 구성 묶음을 발행해야 Tier 0 가
  받는다(FR-17). `/commit` 의 `publish` 가 묶음 발행이다. 명세 12.6 REST 표에는 아직 `POST /api/zones/commit` 이 없다(BE 가 명세 정리에서 더한다).
- 요구사항 FR-15·16·17 은 그대로다. 바뀌는 것은 화면이 요청을 나누는 방식뿐이다.

## 코드·시험 위치

모두 `front/apps/admin/src` 아래.

| 위치 | 내용 |
|---|---|
| `features/zones/zoneCommit.ts` | 순수 계산. 화면 상태 → `ZoneCommitRequest`(`saveRequestOf`·`createRequestOf`·`archiveRequestOf`). 바뀐 것이 없으면 `null` |
| `features/zones/useZoneCommitMutation.ts` | 보내기. 버전 확인 → `commitZone`(publish:true) → 묶음 실패면 `publish:false` 한 번 더 → 응답 캐시 반영, 실패 때 목록 다시 받기 |
| `features/zones/commitCache.ts` | 응답을 구역·작업면·묶음 목록 캐시에 넣는 도우미(`applyCommitted`, `putBundle`, `invalidateCommitted`). `putBundle` 은 발행(`useConfigApply`)도 쓴다 |
| `features/zones/commitErrors.ts` | 실패 문구 표(오류 `code` 우선)와 보내기 직전 확인에서 멈춘 저장(`CommitBlockedError`) |
| `features/zones/useZoneCommit.ts` | 화면 흐름. 한 번에 하나만 보내고(`useExclusiveRun`) 성공하면 그 구역 초안을 통째로 지운다 |
| `features/zones/zoneSurfaceLink.ts` | `linkSurfaces` 가 작업면의 `zoneId` 로 잇는다(기하 계산 제거). `pointInPolygon` 은 작업면을 바꿀 때 중심 검사에만 남는다 |
| `features/zones/useZoneEditing.ts` | `surfaceOutside` 는 작업면을 바꿀 때만, `saveReady` 는 작업면 목록을 받은 뒤에만 |
| `features/zones/api.ts`·`types.ts`, `features/surfaces/types.ts`·`surfaces.ts` | `commitZone`, `ZoneCommitRequest`·`zoneCommitResultSchema`, `staticObjectItemSchema.zone_id`, `WorkSurface.zoneId` |
| `mocks/zones.ts`·`staticObjects.ts`·`staticObjectStore.ts`·`hexId.ts` | `POST /api/zones/commit` mock(한 트랜잭션 흉내, 실패하면 구역·작업면 되돌림), 작업면 `zone_id`, 저장소 분리 |
| `__tests__/features/zones/` | `zoneCommit`(경우별 본문), `zoneSurfaceLink`, `useZoneCommitMutation`(캐시 반영·묶음 실패 재전송·버전 확인), `commitErrors`, `ZonesPage`(요청 횟수·본문) |
| `__tests__/mocks/` | `staticObjects`(`zone_id`·PUT 연결 유지·풀기), `zones`(commit 성공·되돌림·권한·모양 400) |

## 저장 경우별 본문

| 경우 | `zone` | `surface` |
|---|---|---|
| 새 구역 | `CREATE` + 새 구역 본문 | 없음 |
| 새 구역 + 작업면 | `CREATE` | `CREATE` |
| 구역만 고침 | `UPDATE` + 본문 | 없음 |
| 작업면만 고침 | `KEEP` | `UPDATE`(받은 상판 높이 표준편차를 다시 보냄) |
| 작업면 추가 | `KEEP` | `CREATE` |
| 지우고 다시 추가 | `KEEP` | `REPLACE`(옛 작업면 ID) |
| 작업면 삭제 | `KEEP` | `ARCHIVE` |
| 구역 + 작업면 | `UPDATE` | 위 중 하나(구역을 옮길 때 같이 옮긴 작업면 포함) |
| 구역 삭제 | `ARCHIVE` | 없음(서버가 이은 작업면도 보관한다. 붙이면 400) |
| 바뀐 것 없음 | 보내지 않는다(작업면 없는 `KEEP` 은 서버가 400) | |
| 다시 보내기 | 기존 `POST /api/config-bundles` | |

## 정한 값과 근거 (명세에 없음)

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 묶음을 못 만들 때 | 같은 본문을 `publish:false` 로 한 번 더 보내 저장만 하고, '저장했지만 현장 판정으로 보내지 못했습니다' 띠와 「다시 보내기」를 보인다 | `publish` 기본 true 라 보정 문서 없음·공개 주소 미설정 현장에서는 서버가 구역 저장까지 되돌린다. 사용자 결정. 묶음 실패로 보는 코드는 `CURRENT_CALIBRATION_MISSING`·`CALIBRATION_MISSING`·`RULES_MISSING`·`PUBLIC_URL_NOT_SET` 넷뿐이다(`SITE_NOT_SINGLE` 은 저장도 못 하는 현장이라 다시 보내지 않는다) |
| 작업면 중심 검사 | 작업면을 새로 만들거나 바꿀 때만 중심이 구역 안인지 본다. 구역만 고칠 때는 막지 않는다 | 사용자 결정. 연결은 서버가 저장한 값이라 구역 다각형을 고쳐도 그대로이고, 막으면 구역만 고치려는 사람이 작업면 때문에 못 저장한다 |
| 보내기 직전 버전 확인 | 유지. 구역은 `UPDATE`·`ARCHIVE`, 작업면은 `UPDATE`·`REPLACE`·`ARCHIVE` 대상의 지금 버전을 GET 으로 다시 받아 고치기 시작한 버전과 다르면 보내지 않는다. `KEEP`·`CREATE` 는 덮지 않으니 보지 않는다 | 서버가 기준 버전을 받지 않아 그 사이 다른 관리자의 변경을 덮지 않으려면 필요하다 |
| 오류 문구 | 서버 오류 `code` 를 먼저 보고, 없으면 403, 결과 모름, 일반 순서 | `/commit` 의 404·409 는 뜻이 여럿(`ZONE_NOT_FOUND`·`STATIC_OBJECT_NOT_FOUND`, `CONCURRENT_EDIT`·`SURFACE_ZONE_TAKEN`·`SITE_NOT_SINGLE`)이라 상태만으로는 문구를 못 정한다. 있던 문구는 그대로 옮기고 없는 것만 더했다 |
| 실패 뒤 목록 | `needsRefetch`(409·404·5xx·결과 모름·응답 오류·시간 초과)면 구역·작업면·묶음 세 목록을 모두 다시 받는다 | 서버가 이미 커밋했을 수 있고(응답을 못 받음) 한 번 저장이 세 목록을 모두 바꾼다 |
| 성공 뒤 초안 | 그 구역의 초안과 되돌리기 기록을 통째로 지운다 | 한 트랜잭션이라 일부만 저장되지 않는다. 예전의 부분 저장 처리(`withoutSaved`)가 필요 없다. 실패하면 서버에 아무것도 없으니 초안을 그대로 둔다 |
| 응답 반영 | 작업면 목록에서 뺄 것은 화면의 `linked` 가 아니라 응답 `archived_surface_id` | REPLACE·구역 ARCHIVE 에서 서버가 실제로 보관한 것을 그대로 따른다 |
| 연결 읽기 | `linkSurfaces`: 지금 구역에 걸린 작업면만, 한 구역에 여럿이면 작업면 ID 순 첫 번째 | `zone_id` 는 보관한 구역 ID 일 수 있다. 서버 V22 가 기존 데이터를 채운 규칙과 같게 한다 |

## 버린 대안

- **저장 때 `publish` 를 빼고 묶음은 따로 발행**: 요청이 둘로 갈려 "저장했지만 현장에 안 갔다"가 늘 생긴다. 서버가 한 번에 하는 길을 두고 이쪽을 택하지 않았다.
- **묶음 실패 때 저장을 포기하고 오류만 보이기**: 보정 문서가 아직 없는 현장에서는 구역을 아예 못 고친다. 사용자가 저장만 하는 길을 골랐다.
- **연결을 화면 기하로 계속 계산**: 서버 값과 어긋난다(구역을 고칠수록 서버는 연결을 두고 화면은 풀었다). 서버 값 한 곳을 기준으로 한다.
- **보내기 직전 버전 확인을 없애기**: 서버가 기준 버전을 받지 않아 다른 관리자의 변경을 조용히 덮는다.
- **`useZoneSave`·`useSurfaceSave` 유지**: 쓰는 곳이 없어져 지웠다. 다시 쓰는 조각(버전 확인, 캐시 반영, 문구 표)만 `useZoneCommitMutation`·`commitCache`·`commitErrors` 로 옮겼다. `api.ts` 의 예전 쓰기 함수(`createZone`·`updateZone`·`deleteZone`, 작업면의 `createStaticObject`·`updateStaticObject`·`deleteStaticObject`)는 계획대로 남겼다. 지금 쓰는 곳은 `deleteZone`·`deleteStaticObject` 를 부르는 mock 시험뿐이고 나머지 넷은 쓰는 곳이 없다(정리할지는 사용자 결정).

## 잔여 위험

- **실서버 확인 전**이다. 서버(`ZoneCommitService`)와 mock 이 같은 규칙인지는 코드를 읽어 맞췄고, EC2 배포 뒤 `VITE_API_MOCK=false` 로 저장·작업면 추가·다시 추가·삭제·구역 삭제·묶음 실패를 한 번씩 확인해야 한다.
- 저장 직전 버전 확인(GET)과 `/commit` 사이에 다른 관리자가 저장하면 서버가 같은 순간 부딪친 저장만 409 로 막는다. 그보다 앞서 끝난 저장은 확인 GET 에서 걸린다.
- 구역을 옮기거나 키워 작업면 중심이 구역 밖에 남아도 서버 연결은 그대로라 저장된다(사용자 결정). 이 상태에서 작업면을 고치면 그때 중심 검사가 걸린다.
- 다른 구역(`z-02`)에 이어진 작업면을 이 구역에 붙이려는 요청은 서버가 409 `SURFACE_ZONE_TAKEN` 으로 거절한다. 화면은 구역마다 자기 작업면만 보이므로 보통 일어나지 않고, 다른 관리자와 겹칠 때만 난다.
- 옛 `DELETE /api/zones/{id}` 는 작업면을 보관하지 않는다. 화면 삭제를 `ARCHIVE` 로 옮겨 함께 보관되지만, 다른 경로로 지운 구역에는 보관되지 않은 작업면이 남아 `zone_id` 가 보관된 구역을 가리킬 수 있다(화면은 지금 구역과 맞춰 읽어 그 작업면을 어느 구역에도 잇지 않는다).

## 넘긴 일

- 구성 동기화 표시(UNVERIFIED·STALE, FR-17)는 이 이슈 범위 밖이다(S15P31A101-409, 보류).
- 명세 12.6 REST 표에 `POST /api/zones/commit` 추가는 BE 몫이다.
- 작업면 mock 의 PUT 은 이어받은 구역이 보관됐어도 연결을 풀지 않는다(서버는 조용히 푼다). mock 저장소가 구역을 몰라서이며, 화면은 지금 구역과 맞춰 읽으므로 보이는 차이는 없다.
