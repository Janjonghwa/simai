# S15P31A101-791 시연 계정 맵을 손질한 11-17 맵으로 바꾸고 이전 맵을 제한·추천 없이 고르게 한다

관련 명세: 4.3(현장 세팅 자동 보정), 4.5(보정·Scene 계보), 8.1·13.4(설치·보정 화면), 12.6(세팅 REST). 계약 변경 없음, DB 변경 없음.
연관 설계: `S15P31A101-689-reuse-always-selectable.md`(재사용을 늘 고르게 함), `S15P31A101-712-reuse-camera-mismatch.md`(카메라 시점 확인),
`S15P31A101-714-reuse-chosen-map.md`(최근 맵 고르기). 시연 계정의 GLB 맵은 -770, 화면 손질은 -782·-786 에서 했다.

## 문제

1. 시연 계정이 보는 3D 맵은 11-16 13:31 맵 기반 GLB(`map-2026-11-16-1331.glb`)다. 11-17 16:38 맵을 손질한 GLB(`map-2026-11-17-1638-clean.glb`,
   사물함·창문 벽·단상·책상·창턱을 다듬음)로 바꿔야 한다.
2. 새 GLB 는 **11-17 16:38 맵 좌표**다. 11-16 맵과는 벽이 최대 20 cm, 바닥이 7 cm 어긋나서 시연은 11-17 16:38 맵(또는 그 보정)을
   기준으로 해야 사람·구역 위치가 맞는다(사용자 결정 2026-11-18).
3. 시연 계정의 「이전 맵 재사용」은 카메라 같은 자리 확인(-712)과 추천 표시 때문에 시점이 바뀐 맵을 고를 수 없고, 서버도 409 로
   막아 시연이 끊긴다. 보이는 맵 수는 -786 에서 정한 최근 2개 그대로 둔다.

## 바꾼 동작 (시연 계정만, 일반 계정은 그대로)

### 3D 맵

- `features/demo/assets/` 의 GLB 를 `map-2026-11-17-1638-clean.glb`(21.8 MB)로 바꾸고 11-16 GLB 를 지웠다. `demoMap.ts` 는 폴더의
  `.glb` 하나를 그대로 쓰므로 코드 변경은 주석뿐이다.
- 11-17 16:38 맵의 좌표·물체 위치는 그대로이고 면을 지우거나 색을 고치는 손질만 했다. **각 사물과 벽 사이 거리는 건드리지 않았다**(사람을 올리는
  카메라·보정 기준이라서). 손질 스크립트는 저장소 밖이다.

### 이전 맵 재사용 (프론트)

- `demoReuseOpen(reuse)`(`features/demo/demoSetup.ts`): EXCLUDED(보정이 없거나 완료 전)가 아닌 맵은 모두 `selectable: true`, 추천 해제, 상태 문구
  비움. 고를 맵이 하나라도 있으면 `blocked`·`note` 를 지우고 기본 선택은 가장 최근 완료 맵이다. 하나도 없으면 막는 이유를 그대로 둔다.
- `demoReuseShown`: 최근 완료 2개만 보이는 -786 의 규칙(`DEMO_REUSE_MAP_COUNT = 2`)은 그대로 둔다. 새 맵이면 목록 없이 보인다.
- `SetupPage` 가 시연 계정일 때만 `demoReuseOpen` 을 거치고, `SetupModeChoice` 의 `openMaps` 로 안내 글에서 「같은 자리인 맵만」을 뺀다.
- 목업(`mocks/setup.ts`)은 시연 계정 사용자 id 로 재사용 확인과 시작 응답을 만든다.

### 이전 맵 재사용 (서버)

- `SetupDemoAccounts`(`domain/setup`): `simai.setup.demo-user-ids`(환경변수 `SIMAI_DEMO_USER_IDS`, 기본 `1` = 프론트 `demoAccount` 와 같은 실서버
  시연 계정)에 든 사용자만 true.
- `SetupReuseCheck.require(siteId, chosen, open)`: `open` 이면 시점 확인을 건너뛰고 EXCLUDED 가 아닌 맵 중 고른 맵(없으면 가장 최근 `completedAt`)을
  쓴다. 맵이 없으면 `SETUP_REUSE_SCENE_MISSING`, 고른 맵이 EXCLUDED 면 `SETUP_REUSE_MAP_UNAVAILABLE`(409) 그대로.
  세팅 세션에 남기는 `reuse_view_check` 는 `demo_account: true` 를 가진 비어 있지 않은 객체로 둔다(V78 의 CHECK 제약이 null 을 허용하지 않음).
- `toJson(r, open)`: 시연 계정의 `GET /api/setup-sessions/reuse-check` 가 `available`·`selectable` 을 열고 `recommended` 를 비운다.
- `SetupService.createReuse` 와 새 `reuseCheck(userId)` 가 `SetupDemoAccounts.isDemo(userId)` 로 갈라 쓴다. 일반 계정 경로는 `open=false` 로 그대로다.

## 시험

- 프론트 `__tests__/features/setup/demoReuseOpen.test.ts` 4건(열기·EXCLUDED 유지·막는 이유 유지·최근 2개만 보임), 통과 확인.
- 서버 `SetupReuseModeIntegrationTest` 에 시연 계정 시험 2건(모든 비제외 맵 선택 가능·추천 없음·EXCLUDED 409·CHANGED 맵으로 시작·기본은 최근 맵)을
  더하고 `application-test.yaml` 은 `demo-user-ids: ""` 로 일반 계정 시험을 지킨다.

## 정한 값과 근거

| 항목 | 값 | 근거 |
|---|---|---|
| 좌표 기준 | 11-17 16:38 맵 | 사용자 결정. GLB 가 그 좌표라 11-16 맵 기준으로 다시 맞추면 거리가 바뀐다(거리 고정 원칙) |
| 서버 열기 범위 | 시연 계정만 | 사용자 결정. 일반 계정의 재사용 안전 검사(-712)는 그대로 |
| 기본 선택 | 가장 최근 `completedAt` 의 비제외 맵 | 추천 표시를 지워도 시작 버튼이 바로 동작해야 함 |
| 시연 계정 id | 기본 1, `SIMAI_DEMO_USER_IDS` 로 바꿈 | 프론트 `demoAccount` 와 같은 값 |

## 버린 대안

- **11-16 맵 기준으로 모델을 옮김**: 사물·벽 거리를 바꾸게 되어 보정이 어긋난다. 거리 고정 원칙에 어긋나 버렸다.
- **프론트만 열기**: 서버 `SetupReuseCheck` 가 시점이 바뀐 맵을 409 로 막아 시작이 실패한다. 서버도 시연 계정만 열어야 한다.
- **EXCLUDED 맵까지 열기**: 보정이 없거나 완료 전인 맵은 재사용 시작이 실패해 시연 중 오류가 난다. 꺼 두고 이유를 보인다.
- **모든 계정에서 안전 검사 해제**: 사용자가 시연 계정만으로 한정했다.

## 잔여 위험

- 서버 변경은 이 PC 에 JDK 21 이 없어(JDK 17 만 있음) **로컬에서 컴파일·시험하지 못했다.** 서버 시험은 GitLab CI 결과로 확인한다.
- 시점이 바뀐 맵으로 재사용을 시작하면 그 맵의 보정으로 사람 위치가 계산되어, 카메라가 실제로 움직였다면 위치가 틀어진다(시연 계정 한정, 이유 문구도 숨김).
- GLB 는 11-17 16:38 좌표다. 시연에서 다른 날짜 맵을 고르면 3D 맵과 서버 맵이 최대 20 cm 어긋난다.
- 실서버 시연 계정이 user_id 1 이 아니면 `SIMAI_DEMO_USER_IDS` 를 바꿔야 서버 쪽이 열린다.
- 스캔이라 못 본 곳의 구멍·흐린 곳(커피머신 자리 등)은 그대로다.
- 저장소 규칙은 대형 3D 산출물을 커밋하지 않지만 시연 GLB 는 -770 처럼 코드에 넣는다(21.8 MB 로, 교체하는 11-16 GLB 27.2 MB 보다 작다).

## 넘긴 일

- 새 맵 만들기 흐름, 일반 계정의 재사용 검사 변경: 이 티켓 범위 밖.
- 맵 손질 스크립트의 저장소 반영: 저장소 밖 자료라 따로 정하지 않았다.
