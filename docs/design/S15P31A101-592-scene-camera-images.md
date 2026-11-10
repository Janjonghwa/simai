# S15P31A101-592 Scene 입력 카메라 사진 서명 주소

관련 명세: 13장(관제 3D 뷰어), 3.2(Visual 층은 위치·판정에 들어가지 않음), 12.6(REST), 14(객체 저장소 서명 주소). 앞 티켓 `docs/design/S15P31A101-590-camera-view-vdt.md`(카메라 시점 VDT 자리 `vdt.cameraImageUrls`).

## 한 일

- 서버: `GET /api/scenes/{scene_version}/camera-images`(열람 이상, 인증만). 그 Scene 을 구운 사람 지운 배경 사진의 카메라별 GET 서명 주소를 준다.
- 계약: `contracts/messages/scene_camera_images.schema.json` 1.0.0 신설(contracts v7.25.0, 마이너). 응답 `{ scene_version, bundle_id|null, expires_at_ms|null, images: [{ camera_id, url }] }`. 묶음을 모르면 사진도 없고(스키마), 사진이 있으면 만료 시각이 있다(스키마).
- 관제 화면: `SpatialViewportHost` 가 지금 Scene 버전으로 이 API 를 불러 `Viewer3D` 에 `vdt={{ cameraImageUrls }}` 를 넘긴다. 사진이 없으면 넘기지 않는다(시점 이동만). `enabled` 를 넘기지 않으므로 VDT 는 카메라 시점에 맞아 있는 동안만 켜진다(-590 그대로).
- msw mock: 같은 경로. mock Scene 은 데모 glTF 라 묶음 null·빈 목록, 기본 Scene 은 서버처럼 404.

## 코드·시험 위치

| 내용 | 위치 |
|---|---|
| 묶음 연결·키 제한·서명 | `server/.../domain/scene/SceneCameraImageService.java`, 경로 `SceneController` |
| 서버 시험 | `SceneCameraImageServiceTest`(단위, 계약 검사 포함), `SceneCameraImageIntegrationTest`(DB 경로, CI) |
| 계약·예시 | `contracts/messages/scene_camera_images.schema.json`, `contracts/examples/scene_camera_images*.json`, 반례 `examples/invalid/scene_camera_images.*` |
| 요청·zod | `front/apps/admin/src/features/layout/api.ts`(`requestSceneCameraImages`) |
| 다시 받기 계산·뷰어 입력 모양(순수) | `features/layout/sceneCameraImages.ts` |
| 훅·호스트 연결 | `features/layout/useSceneCameraImages.ts`, `features/layout/SpatialViewportHost.tsx` |
| mock | `front/apps/admin/src/mocks/scenes.ts` |
| front 시험 | `__tests__/features/layout/sceneCameraImages.test.ts`, `useSceneCameraImages.test.tsx`, `SpatialViewportHostScene.test.tsx`(카메라 시점 사진 묶음) |

## Scene ↔ 입력 묶음 연결

- 새 열·표·마이그레이션 없음. 이미 DB 에 있다: `scene_version.job_id`(static_reconstruct 결과 Scene 만, `SceneJobListener`) → `analysis_job.input` 의 `clean_background_bundle_id`·`images[{camera_id, uri, kind}]`(`StaticReconstructSubmitService` 가 묶음 ID 를 넣어 저장). 사진 키를 `<cam>.jpg` 로 짐작하지 않고 작업 입력에 적힌 주소를 쓴다.
- 서명하는 것: `kind = CLEAN_BACKGROUND` 이고 주소가 `s3://<버킷>/static-inputs/<그 묶음>/<파일>.jpg|.png` 인 항목, 카메라당 처음 것, 카메라 ID 순, 16장까지. 다른 버킷·다른 묶음·`..`·보정 JSON 은 서명하지 않는다.
- 비우는 경우: 관리자 등록(`SceneAdminController`)·세팅 세션(`SetupArtifactRegistrar`) Scene 은 `job_id` 가 없어 `bundle_id: null`·빈 목록. 저장소 미설정·서명 실패는 `bundle_id` 만 주고 빈 목록. 없는 Scene 은 404 `SCENE_VERSION_NOT_FOUND`(시연용 기본 Scene 은 DB 행이 아니므로 404, 화면은 묻지 않는다).

## 명세에 없어 정한 값

| 값 | 정한 것 | 근거 |
|---|---|---|
| 경로 | `GET /api/scenes/{scene_version}/camera-images` | 화면이 이미 가진 Scene 버전으로 물어 `map_cameras` 와 같은 Scene 의 사진만 받는다. `current` 로 물으면 두 요청 사이에 활성 Scene 이 바뀔 때 다른 Scene 사진이 붙을 수 있다. 기존 `/api/scenes/{v}/commit` 꼴을 따름 |
| 권한 | 인증만(열람 이상) | `GET /api/scenes/current` 와 같다. 관제 역할도 카메라 시점 VDT 를 본다 |
| 서명 만료 | 기존 `simai.storage.download-ttl`(5분) | glTF·클립과 같은 관례. 응답 `expires_at_ms` 는 사진 중 가장 이른 만료 |
| 다시 받기 | 만료 1분 전(하한 5초) | 평면도(`floorPlanReload.ts`)와 같은 여유. EC2·브라우저 시계 차이를 본다 |
| 실패 재시도 | 연결 실패·5xx·서명 실패(묶음은 앎) 1분 뒤, 4xx·묶음 모름은 다시 묻지 않음 | 묶음을 모르는 Scene 은 다시 물어도 같다. Scene 이 바뀌면 키가 바뀌어 새로 묻는다 |
| 사진 종류 | `CLEAN_BACKGROUND` 만 | VDT 자리는 사람 지운 배경용(-590). `FRAME` 은 사람이 찍혀 배경에 사람이 투영된다 |

## 버린 대안

- 최신 `static-inputs` 묶음을 Scene 에 붙이기: 다른 시점의 사진이면 VDT 가 어긋난다(지시·규칙 2).
- 세팅 세션 `CAMERA_FRAME`(`setup_session_artifact`)을 세팅 Scene 사진으로 쓰기: VLM 입력용 대표 프레임이라 얼굴 정책이 BLURRED·EXCLUDED 이고, 재시도가 다른 배경 묶음을 쓸 수 있어(V38 주석) GLB 를 구운 입력과 같다는 근거가 없다.
- `GET /api/scenes/current` 응답에 사진 주소를 싣기: 관제·뷰어가 쓰는 본문을 넓히고 사진 서명이 Scene 조회마다 돈다. 따로 두면 사진이 필요한 화면만 부른다.
- 새 열(`scene_version.bundle_id`)·마이그레이션: 연결이 이미 `job_id` 로 있다.

## 남은 위험

- 지금 운영 Scene 이 세팅 세션에서 나왔거나 관리자가 등록한 것이면 사진이 없어 VDT 는 여전히 켜지지 않는다. 맵 재생성(-428, static_reconstruct)으로 나온 Scene 을 활성화해야 주소가 나온다. 세팅 Scene 의 입력 사진을 남기려면 세팅 워커가 굽기 입력 묶음을 알려야 한다(넘긴 일).
- 주소를 다시 받으면(4분마다) 주소가 바뀌어, 그때 카메라 시점에 맞아 있으면 뷰어가 VDT 를 다시 장착한다(사진 다시 읽기·그림자 맵 다시 굽기, 잠깐 원래 텍스처). 뷰어가 주소만 바뀐 경우 다시 장착하지 않게 하는 것은 뷰어 쪽 일이다.
- 다시 받기가 실패하면 만료된 주소가 남는다. 그 사이 카메라를 누르면 사진 읽기가 실패해 원래 텍스처로 남는다(-590 의 실패 경로).
- 브라우저가 S3 사진을 읽으려면 버킷 CORS 가 GET 을 허용해야 한다(glTF 를 같은 방식으로 읽고 있어 허용돼 있다고 봄, 운영에서 확인 필요).
- `static-inputs` 객체를 지우는 보관 정책이 생기면 오래된 Scene 은 404 사진 주소를 받는다(지금은 지우지 않음).
- DB 통합 시험(`SceneCameraImageIntegrationTest`)은 이 노트북에 시험 DB(TimescaleDB)가 없어 CI 에서만 돈다.

## 넘긴 일

- 세팅 세션 Scene 의 굽기 입력 사진 연결(세팅 워커가 입력 묶음·사진 주소를 산출물로 알리고 EC2 가 남기기).
- 뷰어: 사진 주소만 바뀌면 VDT 를 다시 장착하지 않기.
- (발견) `SpatialViewportHost` 의 `readSceneVersion(scene)` 은 `ViewerSceneInput`(`{ ok, raw }`)에서 `scene_version` 을 읽어 늘 null 이다(`ToolPalette`·재생성 도구의 `currentSceneVersion`). 이 티켓 범위 밖이라 고치지 않았다.
