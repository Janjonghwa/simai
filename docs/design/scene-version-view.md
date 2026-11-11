# 버전을 지정한 Scene 보기 (S15P31A101-613)

작업 관리 새 흐름의 서버 쪽 C14 중 남은 하나다(이전 모델 목록·`tier` 칼럼은 11-11 검토 답변으로 빠졌다).
관련 명세: 12.6(`/api/scenes/current`), 13장(뷰어). 요청서는 추창만의 「작업 관리 새 흐름 — 서버·AI 요청」이다.

## 왜

세팅 완료(C17) 전에는 관제 Scene 이 바뀌지 않는다. 그래서 새로 만들기 길의 보정·구역 화면은 이 세션의 미리보기 Scene 을 버전으로 받아 그려야 한다.
이전 모델 쓰기 길은 지금 관제 모델이 곧 그 모델이라 `/api/scenes/current` 를 그대로 쓴다(REUSE 세션에는 미리보기 버전이 없다).

## 만든 것

| 항목 | 위치 |
|---|---|
| `GET /api/scenes/{sceneVersion}/view` (ADMIN) | `SceneController.view` → `SceneService.view` |
| current·view 가 같은 응답을 만드는 공용 메서드 | `SceneService.scene` |
| 시험 | `SceneServiceTest`(보기 응답 모양), `SceneVersionViewIntegrationTest`(실제 DB: CANDIDATE 보임, 거절·glb 없음·형식 오류 404, VIEWER 403) |

## 응답

`/api/scenes/current` 와 같다: `scene_version`, `state`(그 버전의 상태), `default=false`, `calibration_version`·`calibration_state`, `committed_at_ms`,
`quality`, `gltf_url`(서명 주소 5분), `surface_source_map_urls`, `map_cameras`. 다른 점 두 가지:

- `compatibility` 는 싣지 않는다. 관제 보정과 비교하는 값이라 아직 적용 전인 미리보기는 늘 어긋남(DIMMED·MISMATCH)으로 나온다.
- `current`(bool): 이 버전이 지금 관제 Scene 인지. 예전에 확정됐다가 바뀐 COMMITTED 와 구분한다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 볼 수 있는 상태 | `CANDIDATE`·`COMMITTED` 이고 `gltf_key` 가 있는 것. 아니면 404 `SCENE_VERSION_NOT_FOUND` | 거절·검증 중 Scene 과 glb 없는 행은 그릴 수 없다. 기본 배경으로 대신하지 않는다 |
| 권한 | ADMIN | 세팅 화면 전용. 열람 권한의 관제 화면은 `current` 를 쓴다 |
| 현재 Scene | 바꾸지 않는다(읽기만) | 요청서 C14 |
| 버전 형식 | 관리 API 와 같은 `^[a-z0-9][a-z0-9._-]{0,62}$`, 아니면 404 | 같은 식별자를 API 마다 다르게 받지 않게 |

## 남은 일

- 명세 12.6 API 표에 `GET /api/scenes/{sceneVersion}/view`(ADMIN)를 더한다(명세 정리 MR).
