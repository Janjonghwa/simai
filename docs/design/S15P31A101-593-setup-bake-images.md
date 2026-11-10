# S15P31A101-593 세팅 세션 Scene 의 카메라 시점 VDT 사진·자세

관련 명세: 3.2(Visual 층은 위치·판정에 들어가지 않음), 13장(관제 3D 뷰어), 12.5(I5 작업), 14(객체 저장소 서명 주소). 앞 티켓 `docs/design/S15P31A101-590-camera-view-vdt.md`(카메라 시점 VDT), `docs/design/S15P31A101-592-scene-camera-images.md`(Scene 입력 사진 API, 넘긴 일 "세팅 세션 Scene 의 굽기 입력 사진 연결"), `docs/design/site-setup-pipeline.md` 12·13절(세팅 워커·서버 등록).

## 문제

운영 활성 Scene 은 모두 세팅 세션이 만든 것(`scene-job-…`, `scene-preview-job-…`)이라 `scene_version.job_id` 가 비어 있고 `scene_output` 도 없다. 그래서
- `GET /api/scenes/{v}/camera-images` 가 묶음을 몰라 빈 목록(-592 경로는 static_reconstruct 작업 입력만 앎),
- `GET /api/scenes/current` 에 `map_cameras` 가 없어 뷰어가 VDT 설정을 만들 자세가 없다(`withVdtCameraImages` 는 `map_cameras` 와 사진이 둘 다 있어야 설정을 만든다. GLB 에도 `extras.simai.map_cameras` 가 없다).

## 한 일

- 계약 v7.26.0(마이너): `site_setup` 1.6.0 `artifacts.bake_images = { bundle_id, images[{camera_id, uri, sha256, size_bytes?, face_policy}] }`(`job_callback` 2.13.0·`job_heartbeat` 2.6.0), `scene_camera_images` 1.1.0 선택 `source`(`STATIC_RECONSTRUCT`·`SITE_SETUP`, 있으면 `bundle_id` 필수).
- 워커(`ai/services/worker/site_setup*.py`): (g) 미리보기 단계에서 작업 폴더 `images/<카메라>.image` 를 카메라당 1장 `bake-<카메라>.jpg` 로 올리고 미리보기 스냅샷부터 `artifacts.bake_images` 로 알린다. 어떤 실패도 세팅을 멈추지 않는다(로그만, 취소·시간 초과만 그대로).
- 서버 등록(`SetupArtifactRegistrar`): 프레임과 같은 검사(이 작업·이 대여 경로, 내려받아 크기·sha256·JPEG 시그니처, 세션 카메라, 스냅샷 안 카메라 중복 금지) 뒤 `setup_session_artifact` 에 `BAKE_IMAGE`(version `<camera_id>@<lease_id>`, `camera_id`·`face_policy`·`bundle_id`)로 추가.
- 마이그레이션 `V48__setup_session_artifact_bake_image.sql`: kind CHECK 에 `BAKE_IMAGE`, 열 `bundle_id`, 종류별 열 CHECK(`setup_session_artifact_frame_cols`) 다시 정의, 부분 색인 `(setup_session_id, job_id, lease_id) WHERE kind='BAKE_IMAGE'`.
- 서버 사진 API(`SceneCameraImageService`): job_id 경로가 묶음을 모르면 그 Scene 을 등록한 `setup_session_artifact`(SCENE·PREVIEW_SCENE) 행과 세션·작업·대여가 같은 `BAKE_IMAGE` 행을 찾아 서명한다(`source: SITE_SETUP`). 그 대여 결과 경로 바로 아래 `.jpg` 만, 카메라당 처음 것, 16장까지. 묶음 ID 가 둘 이상이면 모름.
- 서버 Scene 조회(`SceneService`): `scene_output.method.map_cameras` 가 없고 세팅 세션 Scene 이면 그 Scene 의 `calibration_version` 보정 문서 카메라 `image_size·K·R·t` 를 `pose_source: CALIBRATION` 으로 `map_cameras` 에 싣는다. 한 대라도 형식이 틀리면 싣지 않는다.
- front 변경 없음: zod 가 모르는 `source` 를 버리고, `map_cameras` 는 기존 `readSceneMapCameras` 형식 그대로다.

## Scene ↔ 사진 연결 근거(DB)

`setup_session_artifact` 의 SCENE·PREVIEW_SCENE 행은 그 GLB 를 올린 `job_id`·`lease_id` 를 갖는다(-467). 세팅 처리기는 대여 하나에서 배경 묶음을 한 번 골라(`_wait_for_backgrounds`) `initialize` 가 그 사진을 `images/` 에 쓰고 prepare·preview·bake 가 모두 그 폴더를 읽는다. 그래서 같은 세션·작업·대여의 BAKE_IMAGE 는 그 대여의 미리보기·정식 GLB 를 구운 바로 그 사진이다. 기존 CAMERA_FRAME 의 구역 초안 연결(`SetupService`, 미리보기를 등록한 대여의 프레임)과 같은 방식이다.

## 명세에 없어 정한 값

| 값 | 정한 것 | 근거 |
|---|---|---|
| 올리는 파일 | 작업 폴더 `images/<카메라>.image`(배경 묶음 `clean_backgrounds.json` 의 그 카메라 사진) | 미리보기·굽기 단계가 읽는 파일 그 자체. 다른 시점 사진이 섞이지 않는다 |
| 인코딩 | JPEG(Tier 0 배경은 이미 JPEG)이고 8 MiB 이하면 바이트 그대로, 아니면 같은 해상도 JPEG 품질 90 | 해상도를 줄이지 않으면 투영 정렬이 그대로다(뷰어는 K 를 image_size 로 정규화). 카메라 해상도 JPEG 는 보통 1 MB 안팎이라 4대 수 MB. 품질 90 은 대표 프레임(`FRAME_JPEG_QUALITY`)과 같다 |
| 크기 상한 | 워커·서버 모두 8 MiB | CAMERA_FRAME 상한(`MAX_FRAME_BYTES`)과 같다. 4K JPEG 도 들어간다 |
| 얼굴 정책 | OBSERVED(계약은 OBSERVED·BLURRED·EXCLUDED) | 같은 사진이 GLB 텍스처로 이미 구워져 있다. 외형 텍스처 규칙(AGENTS 규칙 8, 기본 OBSERVED)을 따른다. 사람 지운 배경이라 얼굴은 원칙상 없다 |
| 올리는 시점 | (g) 미리보기 단계, 미리보기 스냅샷부터 | 서버가 미리보기를 현재 Scene 으로 올릴 때(READY_FOR_ZONES) 관제 화면이 사진을 한 번 묻고, 묶음을 모르면 그 Scene 은 다시 묻지 않는다(`nextCameraImagesReloadMs`). 그 전에 등록돼야 미리보기에서도 VDT 가 켜진다. 대가는 미리보기 단계에 업로드 수 초 |
| 행 키 | version `<camera_id>@<lease_id>` | 재시도 대여가 같은 묶음(같은 바이트)을 써도 그 대여의 Scene 에 묶여야 한다. sha 로 키를 잡으면 두 번째 대여 행이 생기지 않는다 |
| 개수 | 1~16(카메라당 하나) | `scene_camera_images.images` maxItems 와 같다 |
| 세팅 Scene 자세 | 그 Scene 의 보정 문서 카메라 자세, `pose_source: CALIBRATION` | 세팅 미리보기·굽기는 등록한 보정 문서 World 에 정렬해 굽는다(-584, `publish` 가 작업 폴더 `calibration.json` 을 그 문서로 바꾼 뒤 preview·bake). 보정 문서 image_size 는 배경 사진 해상도와 같다(`prepare_from_bundle` 이 검사) |

## 버린 대안

- CAMERA_FRAME 을 VDT 사진으로 쓰기: 긴 변 1280 px 로 줄였고(정렬은 같지만 화질 손해), 얼굴 정책이 VLM 입력용(BLURRED·EXCLUDED)이며 버전이 sha 기준이라 재시도 대여에 묶이지 않는다. 용도가 달라 섞지 않았다.
- 정식 굽기 뒤(BAKING_TEXTURE)에 올리기: 구역 준비를 늦추지 않지만 미리보기 Scene 이 이미 현재 Scene 이 된 뒤라 관제 화면이 사진을 받지 못한다(위 표).
- 워커가 굽기 결과(`bake.json`)의 `map_cameras` 를 보내 서버가 저장: 정확하지만 계약 필드·저장 열이 더 필요하다. `scene_version.scene_output` 은 `scene_output` 계약 원문 자리라 부분 문서를 넣을 수 없다. 최종 맵 방식 2(`METHOD2_CALIB_WM2`)는 보정 자세를 그대로 써서 보정 문서 자세와 같고, 미리보기·폴백도 같은 보정 World 에 정렬하므로 최소 변경으로 보정 자세 대체를 먼저 두고 실측 뒤 판단한다(남은 위험).
- GLB `extras.simai.map_cameras` 에 자세를 써 넣기: 뷰어가 읽는 자리는 있지만 GLB 바이트를 굽기 뒤에 다시 쓰는 단계를 더해야 하고 three.js 가 문서 수준 extras 를 루트 userData 로 옮기는지에 기대야 한다.
- 모든 Scene 에 보정 자세 대체: 관리자 등록·static_reconstruct 레거시 Scene 은 어떤 자세로 구웠는지 모른다. 세팅 Scene 만 대체한다.
- `bundle_id` 없이 `source` 만으로 사진 허용(스키마 조건 완화): 세팅 워커가 묶음 ID 를 알고 있어(`bundle-<uuid hex>`) 그대로 주는 편이 "묶음을 모르면 사진도 없다" 규칙과 front 다시 받기 판단을 그대로 둔다.

## 코드·시험 위치

| 내용 | 위치 |
|---|---|
| 계약 | `contracts/messages/site_setup.schema.json`(`bake_image_ref`·`bake_images`), `scene_camera_images.schema.json`(`source`), 예시 `job_callback.site_setup`·`scene_camera_images.site_setup`, 반례 `job_heartbeat.site_setup_bad_bake_bundle`·`scene_camera_images.source_without_bundle` |
| 워커 | `ai/services/worker/site_setup.py`(`BakeImageRef`·`BakeImages`·`_publish_bake_inputs`), `site_setup_stages.py`(`publish_bake_images`·`_bake_image_jpeg`) |
| 워커 시험 | `ai/tests/services/worker/test_site_setup.py`(미리보기 스냅샷부터 실림·실패해도 세팅 성공·취소), `test_site_setup_stages.py`(바이트 그대로·PNG 재인코딩 해상도 불변·저장소 실패) |
| 서버 | `SetupArtifactRegistrar`(BAKE_IMAGE 등록), `SceneCameraImageService`(세팅 경로), `SceneService`(보정 자세 대체), `V48__setup_session_artifact_bake_image.sql` |
| 서버 시험 | `SceneCameraImageServiceTest`·`SceneServiceTest`(단위), `SetupResultRegisterIntegrationTest`(등록→사진 API→현재 Scene map_cameras, 거부 경우), DB 시험은 로컬 TimescaleDB 컨테이너로 돌림 |

## 남은 위험

- 세팅 굽기가 방식 2 가 아닌 경로(폴백 1 `MA_ALIGNED_SIM3`, 폴백 2 레거시 texrecon = MapAnything 자세 + `world_alignment.json`)로 구웠거나 미리보기(MapAnything 기반)이면 메시와 보정 자세가 정렬 잔차만큼 어긋나 VDT 가 밀려 보일 수 있다. 다음 GPU 세션에서 세팅 `bake.json` 의 `reconstruction_path` 와 화면 정렬을 확인하고, 어긋남이 크면 워커가 `bake.json` `map_cameras` 를 알리는 후속이 필요하다.
- 보정 문서는 왜곡 계수를 갖지만 VDT 는 핀홀로 투영한다(-592 경로와 같다). 광각 가장자리는 조금 어긋날 수 있다.
- Tier 0 배경은 유효하지 않은 픽셀을 검정(0)으로 둔다. VDT 에도 그 검정이 투영된다(-592 경로와 같다).
- 이 기능 이전에 만든 세팅 Scene(지금 운영 활성 Scene 포함)은 사진이 없어 여전히 VDT 가 켜지지 않는다. 새 세팅을 돌려야 한다. 보정 자세 `map_cameras` 는 기존 세팅 Scene 에도 실린다(사진이 없으면 뷰어는 시점 이동만).
- 사진 업로드 시간(4대 수 MB)이 미리보기 단계에 더해진다. Pod→S3 업로드가 느린 지역(9차 시드니 S3 지연)에서는 READY_FOR_ZONES 가 수 초 늦을 수 있다.
- 브라우저가 `job-objects/site_setup/…` 사진을 읽으려면 버킷 CORS 가 GET 을 허용해야 한다(-592 와 같은 가정, 운영 확인 필요).
