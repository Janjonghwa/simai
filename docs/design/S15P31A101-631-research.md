# S15P31A101-631: 변경 의심 구역 VLM 의견 및 전후 카메라 사진 — 사전 조사

- **티켓:** `S15P31A101-631`
- **요약:** 높이 비교(`S15P31A101-630`)가 "변경 의심"으로 잡은 구역마다 지난 세팅·이번 세팅 카메라 사진에서 그 구역이 찍힌 영역을 잘라 GPU VLM(Cosmos3-Nano)에게 무엇이 바뀌었는지와 유지/수정/삭제 권고를 묻고, 세팅 구역 목록 화면에 한 줄 의견과 전후 사진을 함께 표시한다.
- **성격:** 사전 조사 전용 문서 (`ai/`, `server/`, `front/`, `contracts/` 제품 코드 변경 없음)
- **기준 브랜치/커밋:** `dev` (`49ca9ff5`)

---

## 1. 지난 세팅 사진이 남는가?

**결론:** **새로 3D 공간을 만든 세팅(`mode=NEW`)의 카메라 사진은 S3와 DB(`setup_session_artifact`)에 세팅 완료 후에도 영구 보관되며 `setup_session_id`(또는 `scene_version` / `calibration_version`)로 즉시 다시 찾을 수 있다.** 단, **이전 모델 재사용(`mode=REUSE` / `REUSE_SCENE`, `S15P31A101-614`)으로 끝난 세팅은 현재 워커가 사진 업로드를 건너뛰고 계약(`site_setup.schema.json`)도 금지하고 있어 사진이 남지 않는 공백**이 있다(5절 위험 1 참조).

### 1.1 세팅 단계별 카메라 사진/프레임 저장 위치 (S3 키, DB 테이블·컬럼)

현장 세팅(`site_setup`) 동안 생성·저장되는 카메라 이미지는 아래 4가지다.

| 구분 | 생성 단계 | 해상도 · 형식 · 얼굴 정책 | S3 키 형식 | DB 테이블 · 주요 컬럼 |
|---|---|---|---|---|
| **① 세팅 대표 프레임 (`CAMERA_FRAME`, `-479`)** | `(g) PREVIEW_AND_VLM_DRAFT` (`publish_frames`) | 긴 변 최대 `1280 px` 리사이즈 JPEG (품질 90), 등록 행 `face_policy = 'BLURRED'` (단, 업로드 바이트 자체는 `clean_background` 원본이고 VLM 투입 직전 워커가 흐림 처리) | `job-objects/site_setup/{job_id}/{lease_id}/frame-{camera_id}.jpg` | `setup_session_artifact` (`kind = 'CAMERA_FRAME'`, `version = '{camera_id}@{sha256[:12]}'`, `camera_id`, `object_uri`, `sha256`, `size_bytes`, `face_policy`) |
| **② 세팅 굽기 배경 사진 (`BAKE_IMAGE`, `-593`)** | `(g) PREVIEW_AND_VLM_DRAFT` (`publish_bake_images`) | 원본 해상도(예: `2304×1296`) 사람 지운 배경(`clean-background` 중앙값), 8 MiB 이하 JPEG 무변경 또는 품질 90 재인코딩, `face_policy = 'OBSERVED'` | `job-objects/site_setup/{job_id}/{lease_id}/bake-{camera_id}.jpg` | `setup_session_artifact` (`kind = 'BAKE_IMAGE'`, `version = '{camera_id}@{lease_id}'`, `camera_id`, `bundle_id = 'bundle-<32hex>'`, `object_uri`, `sha256`, `size_bytes`, `face_policy`) |
| **③ Pod 로컬 깨끗한 배경 묶음 (`CleanBackground`)** | `(a) COLLECTING_BACKGROUND` | 원본 해상도 PNG/JPEG + `clean_backgrounds.json` | S3 미업로드 (Pod 로컬 `$SIMAI_STATE_DIR/clean-background/{cal_ver}/bundle-{hex}/`) | DB 미기록 (Pod 컨테이너 디스크라 인스턴스 종료 시 소멸, `docs/spec/03-system.md` 3.12) |
| **④ 맵 보강 촬영 미디어 (`setup_supplement_media`, `-605`·`-607`)** | 미리보기 후 관리자 선택 업로드 (`SUPPLEMENT_REBAKE`) | 휴대폰·DSLR 동영상/사진 (`mp4`, `mov`, `jpg`, `png`) | `setup/{setup_session_id}/supplement/{media_id}.{ext}` | `setup_supplement_media` (`media_id`, `setup_session_id`, `kind`, `object_key`, `status = 'UPLOADED'`) |

- **코드 근거 (`file:line`):**
  - **워커 업로드:**
    - `CAMERA_FRAME` 리사이즈·업로드: `ai/services/worker/site_setup_stages.py:205-225` (`_frame_jpeg`), `ai/services/worker/site_setup_stages.py:451-472` (`publish_frames`), `ai/services/worker/site_setup.py:175-185, 665-687, 971, 1002-1003` (`MAX_CAMERA_FRAME_BYTES = 4 MiB`).
    - `BAKE_IMAGE` 원본 해상도·업로드: `ai/services/worker/site_setup_stages.py:228-239` (`_bake_image_jpeg`), `ai/services/worker/site_setup_stages.py:475-499` (`publish_bake_images`), `ai/services/worker/site_setup.py:188-208, 689-723, 974, 1004-1005` (`MAX_BAKE_IMAGE_BYTES = 8 MiB`).
    - 워커 전용 서명 PUT/GET(`POST /api/worker/jobs/{job_id}/objects/sign`): `server/src/main/java/com/ssafy/simai/domain/job/JobObjectSignService.java:43-46, 84-98` (`JOB_OBJECT_PREFIX = "job-objects/"`).
  - **서버 DB 스키마 및 아티팩트 등록:**
    - `server/src/main/resources/db/migration/V37__setup_session_artifact.sql:6-17` (`setup_session_artifact` 기본 테이블).
    - `server/src/main/resources/db/migration/V38__setup_session_artifact_camera_frame.sql:7-26` (`CAMERA_FRAME` 제약·`camera_id`·`face_policy` 및 인덱스 `setup_session_artifact_frame (setup_session_id, camera_id)`).
    - `server/src/main/resources/db/migration/V48__setup_session_artifact_bake_image.sql:8-30` (`BAKE_IMAGE` 제약·`bundle_id` 및 인덱스 `setup_session_artifact_bake (setup_session_id, camera_id)`).
    - `server/src/main/java/com/ssafy/simai/domain/setup/SetupArtifactRegistrar.java:60` (`OBJECT_PREFIX = "job-objects/site_setup/"`), `SetupArtifactRegistrar.java:209-251` (`registerCameraFrames`), `SetupArtifactRegistrar.java:253-295` (`registerBakeImages`), `SetupArtifactRegistrar.java:418-436` (`insertArtifactRecord`).
  - **보강 촬영 미디어(`setup_supplement_media`)와의 관계:**
    - `server/src/main/resources/db/migration/V53__setup_supplement_capture.sql:7-32, 46-47, 67`, `server/src/main/java/com/ssafy/simai/domain/setup/SetupSupplementService.java:46-55, 134-215`.
    - 보강 촬영은 고정 4대 카메라(`camera_id`)가 아닌 이동식 기기 촬영본이라 고정 카메라 보정 기반 전후 구역 비교의 직접 비교 쌍이 아니다. 다시 굽기(`SUPPLEMENT_REBAKE`) 시에도 고정 카메라 배경 사진은 첫 대여의 `BAKE_IMAGE` 묶음을 `setup_supplement_rebake.rebake_base.bake_images`로 그대로 이어받는다(`SceneCameraImageService.java:67-73`).
  - **참고 — 오프라인/수동 `static_reconstruct` 입력 사진(`-592`):**
    - `server/src/main/java/com/ssafy/simai/domain/scene/SceneCameraImageService.java:47, 154-169` (`static-inputs/{bundle_id}/{file}.jpg|.png`, `analysis_job.input` JSONB 기록).

### 1.2 보관 기간·삭제 정책 및 세팅 완료 후 존속 여부

- **세팅 완료(`POST /api/setup-sessions/{id}/complete`) 후에도 삭제되지 않고 그대로 남는다:**
  - `server/src/main/java/com/ssafy/simai/domain/setup/SetupCompletionService.java:94-160`은 세션 구역 목록 적용(`zoneApplier.apply`), `work_unit.setup_completed_at` 갱신, `scene_version` 활성화, `config_bundle` 발행만 수행하며 `setup_session_artifact` 행이나 S3 `job-objects/site_setup/...` 객체를 전혀 삭제하지 않는다.
- **주기적 자동 삭제 대상이 아니다(발표 후 30일 안 일괄 파기 대상):**
  - `docs/spec/11-data.md:107-122`(11.5 영상·프레임 보관 및 파기)와 `docs/design/server-data-retention.md:10-17, 36`에 따라 EC2 자동 삭제 스케줄러(`server/src/main/java/com/ssafy/simai/domain/clip/ClipPurgeService.java:61-110`)는 14일 지난 `media_clip`(보존 제외)과 미완료 `EVENT_CLIP` `upload_grant`만 지운다.
  - `setup_session_artifact`의 `CAMERA_FRAME`·`BAKE_IMAGE`에는 만료·삭제 배치가 없다.

### 1.3 세션 ID로 다시 찾을 수 있는가?

- **`setup_session_id`로 즉시 조회 가능하다:**
  - `CAMERA_FRAME` 조회 쿼리(현재 `vlm_zone_draft` 입력 조립에 사용 중): `server/src/main/java/com/ssafy/simai/domain/setup/SetupService.java:998-1006`
    ```sql
    SELECT camera_id, object_uri, face_policy FROM setup_session_artifact
    WHERE setup_session_id = :id AND kind = 'CAMERA_FRAME' ORDER BY camera_id, registered_at DESC
    ```
  - `BAKE_IMAGE` 조회 쿼리(현재 `/api/scenes/{sceneVersion}/camera-images`에서 사용 중): `server/src/main/java/com/ssafy/simai/domain/scene/SceneCameraImageService.java:60-75` (`setup_session_artifact`에서 `kind = 'BAKE_IMAGE'` 조회).
- **"지난 세팅 세션"을 찾는 연결 고리:**
  - 현재 현장의 활성 Scene 버전(`scene_version`) 또는 직전 완료 작업(`work_unit.active_setup_session_id` / `setup_completed_at IS NOT NULL`)으로부터 직전 세팅의 `setup_session_id`와 당시 `calibration_version`(직전 카메라의 `K, R, t`)을 역추적할 수 있다(`setup_session.calibration_version`, `setup_session.scene_version`, `setup_session.preview_scene_version`).
  - 또한 VLM 호출용으로는 `CAMERA_FRAME`(`1280 px`, `face_policy='BLURRED'`)과 `BAKE_IMAGE`(원본 해상도 사람 지운 배경)가 모두 존재하는데, `BAKE_IMAGE` 역시 사람 영역 픽셀을 시간 중앙값으로 지운 `clean-background` 사진이므로 워커의 얼굴 처리 관문(`gate_input_frames`)에 통과시켜 VLM 입력으로 쓸 수 있고 화면 표시용 서명 GET URL과도 원본을 일치시킬 수 있다.

---

## 2. 구역을 사진에 투영할 수 있는가?

**결론:** **`ai/`에 이미 World 다각형·높이를 카메라 픽셀로 투영하고 가장 잘 보이는 카메라를 골라 잘라내는(crop) 함수들이 구현되어 있다.** 단, 기존 `zone_draft.py`는 단일 평면(`z_plane`)만 투영하고 왜곡 계수를 생략하므로, 3D 구역 상자(바닥 `z_min_m` + 상판/천장 `z_max_m` 8꼭짓점)와 왜곡 모델(`OPENCV_PINHOLE` / `OPENCV_FISHEYE`), 그리고 **전후 두 보정 문서에서 공통으로 잘 보이는 카메라 1대(또는 쌍) 선택**으로 살짝 확장하면 된다.

### 2.1 `ai/` 및 `front/`의 기존 구역→카메라 픽셀 투영 코드

1. **`ai/src/simai_ai/vlm/zone_draft.py` — 핀홀 투영 + 해상도 비례 보정 + 후보 영역 크롭 (가장 직접적인 재사용 대상):**
   - `CameraPose` 및 `cameras_from_calibration(doc)`: `ai/src/simai_ai/vlm/zone_draft.py:108-161` — 보정 문서 JSON(`cameras[].K` 또는 `intrinsics`, `extrinsics.R`/`t`, `image_size`)에서 카메라별 `CameraPose` 사전을 생성.
   - `project_world_points(points_xyz, pose)`: `ai/src/simai_ai/vlm/zone_draft.py:119-128` — World `(N, 3)` 좌표를 $X_c = X_w R^T + t$, $u = f_x (X_c/Z_c) + c_x$, $v = f_y (X_c/Z_c) + c_y$로 픽셀 `(N, 2)`와 카메라 앞(`Z_c > 0.05`) 여부로 투영.
   - `_fit_cameras_to_frames(cameras, frames)`: `ai/services/worker/vlm_zone_draft.py:200-220` — `CAMERA_FRAME`처럼 이미지가 긴 변 `1280 px`로 축소된 경우, 원본 `pose.image_size` 대비 실제 프레임 `(frame_w, frame_h)` 비율만큼 `K[0, :]`와 `K[1, :]`를 비례 축소해 투영·크롭 좌표를 정확히 맞춤.
   - `VlmFrame.with_crop(x1, y1, x2, y2)`: `ai/src/simai_ai/vlm/face_blur.py:224-231` — 얼굴 처리된 `VlmFrame`에서 픽셀 사각형 영역을 잘라 새 `VlmFrame`으로 반환.

2. **`ai/src/simai_ai/vlm/overlay.py` — 왜곡 계수 포함 OpenCV 투영 + 근접 평면/화면 경계 다각형 클리핑 + 구역 반투명 오버레이:**
   - `clip_polygon(points, axis, bound, greater)`: `ai/src/simai_ai/vlm/overlay.py:22-36` — Sutherland–Hodgman 알고리즘으로 카메라 앞 근접 평면(`NEAR = 0.01 m`, 74줄) 및 이미지 4변(`0..w-1`, `0..h-1`, 78~79줄)에 대해 다각형을 안전하게 자름(카메라 뒤로 넘어가는 꼭짓점의 무한대 발산 방지).
   - `project(camera, camera_points)`: `ai/src/simai_ai/vlm/overlay.py:39-52` — `distortion.model`이 `OPENCV_FISHEYE`면 `cv2.fisheye.projectPoints`, `NONE`/`OPENCV_PINHOLE`이면 `cv2.projectPoints`로 왜곡 계수를 반영해 픽셀 투영.
   - `overlay(...)`: `ai/src/simai_ai/vlm/overlay.py:55-86` — 구역 `polygon_xy`와 `height_m`을 투영해 빨간색 반투명 다각형(`cv2.fillPoly` + `cv2.polylines`)을 `VlmFrame` 사본에 덧그림.

3. **`ai/src/simai_ai/calibration/coverage.py` — NumPy 순수 함수 기반 왜곡 포함 투영(OpenCV 미의존):**
   - `_project(cam: RigCamera, points: np.ndarray)`: `ai/src/simai_ai/calibration/coverage.py:389-405` — `simai_ai.geometry.distortion`의 `radial_branch`·`distort`를 사용해 왜곡 식이 접히는 반지름(`branch.r_max`) 바깥은 `NaN`으로 걸러내며 원본 픽셀로 투영.

4. **`front/apps/admin/src/features/viewer3d/scene/vdtMath.ts` — 프론트엔드 CPU/셰이더 투영:**
   - `convertVdtCameraToThree` 및 `evaluateCameraWeightCpu`: `front/apps/admin/src/features/viewer3d/scene/vdtMath.ts:550-631, 753-786` — World→Three→OpenCV 카메라 좌표 변환(`rThreeToCv`, `tThreeToCv`)과 `normalizedIntrinsics`로 정규화 이미지 좌표 `(uImg, vImg)`를 계산하는 순수 TS 함수가 이미 있다.
   - 따라서 전후 사진을 서버/워커에서 잘라 S3에 올리지 않고도, **프론트엔드가 원본 `BAKE_IMAGE`(또는 `CAMERA_FRAME`) 서명 GET URL과 구역 크롭 정규화 좌표 `bbox_norm: [u1, v1, u2, v2]`(또는 워커가 계산한 픽셀 `crop_bbox_px`)를 받아 CSS `object-view-box` / `<canvas>`로 잘라 표시하거나, 워커가 전후 크롭 이미지를 만들어 올리는 방식 중 선택**할 수 있다(4절·5절에서 비교).

### 2.2 구역이 가장 잘 보이는 카메라를 고르는 기존 기준과 확장 방안

- **기존 기준 (`select_candidate_crops`, `ai/src/simai_ai/vlm/zone_draft.py:839-895`):**
  1. 구역 다각형 꼭짓점들을 `z_plane = top_height_m or 0.0`에 놓고 각 카메라로 투영(`project_world_points`, 852~863줄).
  2. 카메라 앞(`valid`)이면서 화면 안(`0 <= u < w`, `0 <= v < h`)에 들어오는 꼭짓점 개수 `visible_count`를 계산(864~865줄).
  3. `visible_count >= 2`인 카메라만 추려 `(-visible_count, camera_id)` 순으로 정렬해 상위 `max_views`(기본 2대)를 선택하고, 투영 경계 상자에 `pad_ratio = 0.25`(양쪽 25% 여유, 최소 `min_crop_px = 64`)를 붙여 `frame.with_crop(x1, y1, x2, y2)`로 잘라냄(869~890줄).
- **변경 의심 구역(`-631`) 전후 비교를 위한 개선 기준:**
  1. **상자 볼륨 투영:** 바닥 꼭짓점(`z = z_min_m` 또는 `0.0`)뿐 아니라 작업면 상판/상단 높이(`z = top_height_m` 또는 `min(z_max_m, 1.5)`) 꼭짓점까지 함께 투영(또는 `overlay.py:22-36`의 `clip_polygon`으로 근접 평면 클리핑)해야 단상·작업대 위 물체 변화가 크롭 상자 위로 잘리지 않는다.
  2. **전후 교집합 카메라 선택:** 지난 세팅 보정($\text{ pose}_{\text{prev}}(c)$)과 이번 세팅 보정($\text{pose}_{\text{curr}}(c)$)이 같은 `camera_id`를 공유할 때, **전·후 두 보정 모두에서 구역이 화면 안에 가장 잘 들어오는(꼭짓점 가시 비율 + 투영 픽셀 면적 $\text{bw}\times\text{bh}$ 점수 합산) 같은 `camera_id`를 1순위로 선택**한다. 만약 카메라 위치/방향이 크게 바뀌어 같은 카메라에서 안 보이면 전·후 각각 최고 점수 카메라(`before_camera_id`, `after_camera_id`)를 독립 선택한다.
  3. **경계선 표시:** `overlay.py:81-85`처럼 잘라내기 전(또는 크롭 위)에 구역 바닥 다각형 윤곽선(`cv2.polylines`)을 얇게 그려 주면 VLM과 관리자 모두 사진 속 어느 영역이 해당 구역인지 한눈에 식별할 수 있다.

---

## 3. VLM 호출 경로를 재사용할 수 있는가?

**결론:** **기존 VLM 클라이언트(`simai_ai.vlm.client.VlmClient`), 얼굴 처리 관문(`gate_input_frames` / `prepare_vlm_frames`), 그리고 `vlm_zone_draft` I5 작업 경로를 그대로 재사용할 수 있다.** 특히 **별도 작업 유형을 새로 만들지 않고 기존 `vlm_zone_draft` 작업에 "변경 의심 기존 구역 목록 + 지난 세팅 보정·프레임 참조"를 함께 실어 한 번의 작업 대여 안에서 처리하는 방안이 가장 유리**하다. (이유: 워커 프로세스는 한 번에 작업 1개만 대여해 실행하므로 `site_setup`이 정밀 텍스처를 굽는 동안에는 다른 I5 작업이 대여되지 않는 구조적 특성이 있기 때문이다 — 아래 3.2절 상세).

### 3.1 기존 VLM 근거(`vlm_rationale`) 및 구역 초안(`vlm_zone_draft`) 처리 경로

| 항목 | 이벤트 근거 (`vlm_rationale`, `-25`·`-329`) | 세팅 구역 초안 (`vlm_zone_draft`, `-455`·`-479`) |
|---|---|---|
| **계약 스키마** | `contracts/messages/vlm_rationale.schema.json:1-88` | `contracts/messages/vlm_zone_draft.schema.json:1-235` |
| **작업 요청/콜백** | `contracts/messages/job_request.schema.json:90-111` (`priority: "VLM_FAST"`), `job_callback.schema.json:166-183` | `contracts/messages/job_request.schema.json:223-243` (`priority: "BACKGROUND"`), `job_callback.schema.json:285-306` |
| **워커 처리기** | `ai/services/worker/vlm_rationale.py:76-196` (`VlmRationaleHandler`) | `ai/services/worker/vlm_zone_draft.py:49-198` (`VlmZoneDraftHandler`), `ai/src/simai_ai/vlm/zone_draft.py:984-1037` |
| **워커 조립/게이트** | `ai/services/worker/__main__.py:97-110, 222-229` (`vlm_gate` `/health` 200 확인 후 대여) | `ai/services/worker/__main__.py:94, 104-105` (pose 가중치·VLM 설정 있으면 `VlmClient` 활성, 없으면 `NO_OPINION` 폴백) |
| **이미지 입력 방식** | `input.frames[]` (`camera_id`, `frame_uri`, `local_copy`, `face_policy: "BLURRED"\|"EXCLUDED"`) → `vlm_frames.py:212-256` (`gate_input_frames`) → `face_blur.py:257-310` (`prepare_vlm_frames`, YOLO26x-pose 얼굴 모자이크) → `overlay.py:55-117` → `VlmClient.complete` (`http://127.0.0.1:8000/v1/chat/completions`, `data:image/jpeg;base64,...`) | `input.frames[]` (`camera_id`, `frame_uri`, `face_policy: "BLURRED"`) → `gate_input_frames` → `_fit_cameras_to_frames` → 후보별 `select_candidate_crops` (`frame.with_crop`) → `VlmClient.complete` (`RESPONSE_FORMAT` `json_schema` 강제) |
| **서버 작업 등록** | `VlmRationaleIngestor.java` (`event_frame_ready` 수신 시) | `SetupService.java:967-1050` (`enqueueZoneDraftJob`: `PREVIEW_AND_VLM_DRAFT` 하트비트 수신 시 `analysis_job` `QUEUED` 삽입) |
| **서버 결과 저장** | `vlm_assessment` 테이블 (`VlmAssessmentService.java`) | `setup_zone_draft_run` + `setup_zone_draft_item` 테이블 (`V36__setup_zone_draft.sql`, `SetupService.java:466-604` `onZoneDraftFinished`) |

### 3.2 세팅 중(맵 굽는 동안 GPU가 켜져 있을 때) 새 VLM 호출을 끼워 넣을 자리와 워커 단일 대여 구조 분석

코드 조사 결과, **반드시 짚고 넘어가야 할 워커 실행 구조**가 확인되었다:

1. **현재 `SetupService.enqueueZoneDraftJob` 호출 시점 (`SetupService.java:418-420, 967-1050`):**
   - `site_setup` 워커가 `(g) PREVIEW_AND_VLM_DRAFT` 단계에서 미리보기 맵(`PREVIEW_GLB`)과 `CAMERA_FRAME`·`BAKE_IMAGE`를 S3에 올린 직후 `READY_FOR_ZONES`(진행률 85%) 하트비트를 보내고, 곧바로 같은 `site_setup` 스레드 안에서 `(h) BAKING_TEXTURE`(Open3D TSDF + `texrecon` 정밀 굽기)로 진입한다(`ai/services/worker/site_setup.py:797-904`).
   - 서버 `SetupService.onProgress`(`SetupService.java:418-420`)는 이 하트비트를 받아 `enqueueZoneDraftJob`으로 `vlm_zone_draft` 작업을 `analysis_job` 테이블에 `QUEUED`로 넣는다.
2. **워커 프로세스의 단일 작업 실행 구조 (`ai/services/worker/runner.py:141-160, 223-263`):**
   - `services.worker`(`Worker.run`)는 `while not stop.is_set(): self.run_once()`로 도는 **단일 대여 루프**다.
   - 즉, 워커 프로세스가 `site_setup` 대여를 잡고 `(h) BAKING_TEXTURE`를 수행하는 동안에는 `run_once()`가 끝나지 않아 다음 `lease_once()`를 호출하지 않는다(단일 워커 프로세스 기준 `vlm_zone_draft`는 `site_setup`이 끝나거나 별도 워커 프로세스가 있을 때 대여됨).
   - 반면 vLLM 서버(`127.0.0.1:8000`, Cosmos3-Nano)는 같은 GPU Pod에서 **별도 상시 프로세스**로 떠 있고(`docs/spec/05-processing.md` 5.4), `(h) BAKING_TEXTURE`의 `texrecon`·Open3D TSDF는 주로 **CPU 코어**를 사용한다(`docs/spec/05-processing.md` 5.3 "GPU Pod 의 전체 CPU 코어를 전력으로 써서").
3. **끼워 넣을 자리 선택지 2가지:**
   - **선택지 A (권장, 최소 변경·계약 일관성): 기존 `vlm_zone_draft` 작업(또는 그 직후 체인)에 변경 의심 구역 의견 요청(`suspected_zones[]`)을 통합**
     - `vlm_zone_draft`는 이미 세팅의 `calibration_ref`(이번 보정)와 `frames`(이번 세팅 프레임)를 받고, `VlmZoneDraftHandler`가 `gate_input_frames`로 얼굴 블러 처리된 `frames_by_camera`와 `_fit_cameras_to_frames`를 준비한다(`ai/services/worker/vlm_zone_draft.py:83-86`).
     - 여기에 `previous_calibration_ref`·`previous_frames[]`·`suspected_zones[]`를 선택 필드로 추가하면, 워커가 이번 프레임 얼굴 처리를 한 번만 수행한 뒤 초안 후보(`candidates`)와 변경 의심 기존 구역(`suspected_zones`)에 대해 연속으로 `vlm_client.complete(...)`를 호출할 수 있다(구역당 전·후 크롭 2장 입력, `json_schema` 응답으로 `change_summary` + `recommended_action: "KEEP" | "MODIFY" | "REMOVE"` 수신).
   - **선택지 B (진짜 병렬 실행이 필요할 때): `site_setup`의 `(g) PREVIEW_AND_VLM_DRAFT` 단계 내부(또는 백그라운드 스레드)에서 로컬 vLLM 직접 호출, 혹은 `-630` 높이 비교가 끝나는 시점에 별도 I5 작업(`vlm_zone_opinion`) 큐잉**
     - 만약 `-630`(높이 비교)이 `PREVIEW_AND_VLM_DRAFT` 시점의 미리보기 점구름/메시로 서버 또는 워커에서 즉시 판정된다면, 선택지 A처럼 `vlm_zone_draft`에 태우거나 `site_setup`의 `(h) BAKING_TEXTURE` 시작과 동시에 워커 내부 보조 스레드에서 vLLM(`127.0.0.1:8000`)을 부르면 `BAKING_TEXTURE`(CPU 병목)와 VLM 추론(GPU vLLM)이 **실제로 동시에** 돌아 관리자가 구역 단계에 들어갔을 때 즉시 의견을 볼 수 있다.

---

## 4. 화면에 붙일 자리와 사진 URL을 받는 방식

### 4.1 세팅 구역 목록 카드/패널 구조 (`front/apps/admin`)

> **참고:** 지시서에 언급된 `SetupZonePlanCard`라는 파일명은 현재 `dev`(`49ca9ff5`)에는 아직 없으며(형제 티켓 `-629`에서 분리·도입 중일 가능성 높음), 현재 `dev` 기준 세팅 3단계(구역 단계) 오른쪽 컬럼(`SetupZoneStep.tsx:145-147`)은 아래 컴포넌트들로 구성되어 있다.

1. **세팅 3단계 오른쪽 컬럼 배치 (`front/apps/admin/src/features/setup/SetupPage.tsx:596-642`):**
   - `SetupZoneStep` (`front/apps/admin/src/features/setup/SetupZoneStep.tsx:45-150`): 왼쪽 큰 3D 뷰어(`minmax(0,1fr)`), 오른쪽 스크롤 컬럼(`minmax(320px,420px)`, 145줄).
   - 오른쪽 컬럼 자식 컴포넌트 순서(`SetupPage.tsx:619-640`):
     1. `AiZoneDraftsPanel` (`front/apps/admin/src/features/setup/SetupPanels.tsx:413-534`) — AI 위험 구역 초안 카드 목록(`item.rationale`, 등급 배지, 버리기/수락 버튼).
     2. `ZonesPage` (`front/apps/admin/src/features/zones/ZonesPage.tsx:44-104`, `placement="inline" addOnDemand`) — 세션 구역 목록(`ZoneSourceContext`, `setupZonePlan.ts`)을 읽고 편집하는 패널(`ZoneSelect` 드롭다운 `ZoneSelect.tsx:22-41`, `ZoneAttributesCard`, `SurfaceCard`, `ZoneActions`).
     3. `SetupCompleteCard` (`front/apps/admin/src/features/setup/SetupCompleteCard.tsx:33-60`) — 세팅 완료 후 상태 카드.
2. **기존 구역이 세팅 구역 목록(`setup_session_zone_plan`)에 들어오는 경로:**
   - `front/apps/admin/src/features/setup/useSeedSetupZonePlan.ts:27-63` 및 `setupZonePlan.ts:228-242` (`liveZonesAsItems`): 이전 모델 재사용(`mode === 'REUSE'`)이거나 기존 관제 구역을 이어받을 때 `setup_session_zone_plan.items`에 `{ zone_id, source: 'MANUAL', candidate_id: null, zone: null, surface: null }` 항목으로 들어간다.
3. **변경 의심 구역의 VLM 의견 + 전후 사진을 붙일 자리:**
   - **자리 1 (구역별 검토 카드 — `-629`/`-630`의 세팅 구역 목록 카드 또는 `SetupPanels.tsx` 신설 패널):**
     - `AiZoneDraftsPanel`(`SetupPanels.tsx:447-528`)과 동일한 카드 패턴(`rounded-lg border px-3 py-2.5 text-xs`)으로, **변경 의심(`suspected`) 구역 항목**마다:
       - 상단: 구역 이름 + `"변경 의심"` 배지 + VLM 권고 배지(`유지 권고` `bg-ok/15 text-ok` / `수정 권고` `bg-delay/15 text-delay` / `삭제 권고` `bg-sev-critical/15 text-sev-critical`, 또는 `AI 의견 없음` `bg-surface-3 text-muted-foreground`)
       - 중단: **VLM 한 줄 의견(`opinion` / `rationale`)** (예: `"단상 위 적재물이 치워져 높이가 낮아졌습니다. 구역 범위 수정을 권고합니다."`)
       - 하단: **전·후 카메라 사진 2칸 나란히(`grid-cols-2 gap-2`)** — 왼쪽 `"지난 세팅 · 카메라 A"`, 오른쪽 `"이번 세팅 · 카메라 A"` 썸네일(클릭 시 확대 또는 해당 구역 3D 선택 `selectTarget({ type: 'zone', id: zoneId }, { focus: true })`).
       - 액션: 관리자가 직접 확인 후 `[유지]` / `[수정 열기]`(3D 편집 상자 선택) / `[목록에서 빼기(삭제)]`(`applyCommitToPlan` `ARCHIVE`)를 누를 수 있게 연결(자동으로 구역을 바꾸거나 지우지 않음).
   - **자리 2 (`ZonesPage.tsx` 안에서 변경 의심 구역을 골랐을 때):**
     - `ZonesPage.tsx:86-96`의 `ZoneAttributesCard` 위(또는 아래)에 선택된 구역(`edit.zone.zoneId`)에 대한 변경 의심 VLM 의견·전후 사진 접이식 알림 상자를 함께 보여 주면, 구역 상자를 3D 손잡이로 고치면서 전후 사진을 바로 대조할 수 있다.

### 4.2 사진 URL을 받는 방식 (기존 서명 GET 재사용 여부)

현재 서버(`server/`)에는 S3 비공개 객체를 프론트엔드에 내려주는 **임시 서명 GET URL(`ObjectStorage.presignGet`)** 패턴이 이미 확립되어 있다:

1. **`SceneCameraImageService` (`GET /api/scenes/{sceneVersion}/camera-images`, `S15P31A101-592`·`-593`):**
   - `server/src/main/java/com/ssafy/simai/domain/scene/SceneCameraImageService.java:49-50, 77-115, 226-237`:
     - `SIGN_TTL = Duration.ofMinutes(5)` (5분 유효 서명 GET URL).
     - `setup_session_artifact`에서 `kind = 'BAKE_IMAGE'`(또는 `static_reconstruct` 입력)의 `object_uri`를 찾아 `storage.presignGet(key, SIGN_TTL)`로 카메라별 `{ camera_id, url }` 배열과 `expires_at_ms`를 반환.
   - 프론트엔드 호출 함수도 이미 존재함: `front/apps/admin/src/features/layout/api.ts:60-74` (`requestSceneCameraImages(http, sceneVersion)`).
2. **전후 사진을 내려주는 두 가지 방법 비교:**
   - **방법 1 (권장 — 워커가 구역 크롭 전·후 이미지를 만들어 S3에 올리고, 서버가 의견 조회 API에서 5분 서명 GET URL을 붙여 반환):**
     - 워커가 VLM에 넣기 위해 이미 전·후 프레임을 내려받아 얼굴 비식별(`prepare_vlm_frames`, `BLURRED`) + 구역 다각형 표시 + 구역 영역 크롭(`with_crop`)을 수행한다.
     - 이 잘라낸 전·후 이미지(예: `zone-opinion-{zone_id}-before.jpg`, `zone-opinion-{zone_id}-after.jpg`, 각 수십 KB)를 워커가 `job-objects/site_setup/{job_id}/{lease_id}/...`에 올리면:
       1. 브라우저에 내려가는 사진도 **얼굴이 모자이크(`BLURRED`)된 상태**라 개인정보/얼굴 노출 위험이 없고,
       2. 브라우저가 4~8 MiB짜리 원본 배경 사진 2장을 통째로 받지 않아도 되며,
       3. 구역 경계선이 그려진 정확한 크롭 영역을 VLM과 관리자가 100% 동일하게 본다.
   - **방법 2 (서버가 기존 `CAMERA_FRAME` 또는 `BAKE_IMAGE`의 서명 GET URL + 크롭 정규화 좌표 `crop_box_norm: [x1, y1, x2, y2]`를 반환):**
     - 워커가 새 이미지를 S3에 추가로 올리지 않고, 결과 JSON에 `before_camera_id`, `after_camera_id`와 각 이미지 내 정규화 크롭 영역 `[u1, v1, u2, v2]`만 적어 돌려준다.
     - 서버는 지난 세팅 `setup_session_id`와 이번 세팅 `setup_session_id`의 `setup_session_artifact`(`CAMERA_FRAME` 또는 `BAKE_IMAGE`)에 대해 `storage.presignGet(key, Duration.ofMinutes(5))`로 서명 GET URL(`before_image_url`, `after_image_url`, `expires_at_ms`)을 만들어 내려주고, 프론트엔드가 CSS/Canvas로 해당 영역을 확대해 보여 준다.
     - **절충안:** 계약에 `before_crop_uri` / `after_crop_uri`(선택)와 `before_crop_box_norm` / `after_crop_box_norm`을 모두 두어, 크롭 이미지 업로드가 있으면 그 서명 URL을 쓰고 없으면 원본 `CAMERA_FRAME`/`BAKE_IMAGE` 서명 URL + 크롭 박스로 표시할 수 있게 하면 가장 안전하다.

---

## 5. 위험·막힘 및 `docs/spec/` 어긋남 점검

### 5.1 핵심 위험·막힘 5가지와 해결 방안

1. **[위험 1 · 치명적] `REUSE_SCENE`(이전 모델 재사용, `-614`) 모드에서는 카메라 사진이 S3/DB에 남지 않는다**
   - **원인:** `ai/services/worker/site_setup.py:775-795, 956-958` 및 `contracts/messages/site_setup.schema.json#/$defs/reuse_rules`에서 `mode=REUSE_SCENE`이 채택되면 `(g) PREVIEW_AND_VLM_DRAFT` 단계를 건너뛰며, `result.artifacts`에 `camera_frames`와 `bake_images`를 넣는 것이 스키마(`"not": { "required": ["camera_frames"] }`)로 막혀 있다.
   - **왜 문제인가:** 기존 구역을 같은 World 좌표로 비교(`-630`, `-631`)할 수 있는 세팅은 (1) `mode=REUSE_SCENE`이 채택되었거나, (2) `mode=FULL_SETUP`(`NEW`)에서 `world_anchor.accepted == true`(`-584`)로 기존 World에 정렬된 경우다. 그런데 `REUSE_SCENE`에서 사진을 올리지 않으면 **이번 세팅(After) 사진이 S3에 없고, 그 다음 세팅에서 쓸 지난 세팅(Before) 사진도 없어진다.**
   - **해결:** `REUSE_SCENE`에서도 `(a) COLLECTING_BACKGROUND`는 이미 수행하므로(`bg_bundle` 보유), `publish_frames`(및 필요 시 `publish_bake_images`)는 `REUSE_SCENE`에서도 실행해 `artifacts.camera_frames`를 올리도록 `site_setup.schema.json`, `site_setup.py`, `SetupArtifactRegistrar.java`를 함께 열어 주어야 한다.

2. **[위험 2 · 구조적] 단일 워커 프로세스의 `BAKING_TEXTURE` 실행 중 별도 I5 작업 대여 지연**
   - **원인:** `ai/services/worker/runner.py:146-160`의 워커 루프는 한 번에 작업 1개만 대여한다. `site_setup`이 `(h) BAKING_TEXTURE`(수십 초~분 단위)를 도는 동안 같은 워커는 큐에 쌓인 다른 작업을 대여하지 못한다.
   - **해결:**
     - 단기/기본: 변경 의심 구역 VLM 평가를 별도 I5 작업으로 쪼개기보다 `vlm_zone_draft` 작업(또는 `site_setup`의 `(g)` 단계 직후 백그라운드 스레드)과 통합하거나, `REUSE_SCENE`처럼 `(h) BAKING_TEXTURE`를 건너뛰는 경우에는 `site_setup`이 즉시 끝나므로 바로 이어서 `vlm_zone_draft`가 대여되어 수 초 내에 끝난다.
     - `FULL_SETUP`에서 `BAKING_TEXTURE` 도중에도 VLM 초안·의견을 즉시 내려면, `site_setup` 핸들러가 `(g)` 완료 직후(`READY_FOR_ZONES` 하트비트 전송 직후) `(h)`를 돌리기 전에 VLM 평가를 먼저 수행하거나 보조 스레드로 돌리는 구조 개선을 검토할 수 있다.

3. **[위험 3 · 선행 티켓 결합] `-630`(높이 비교로 "변경 의심" 구역 판정) 및 `-629`(`SetupZonePlanCard`)와의 경계**
   - **현황:** 현재 `dev` 브랜치에는 아직 `-630`과 `-629`가 병합되어 있지 않다.
   - **해결:** `-631`의 VLM 의견 생성 입력은 `-630`의 내부 구현에 강결합하지 않고, **표준화된 변경 의심 구역 입력 구조(`zone_id`, `zone_name`, `polygon_xy`, `z_min_m`, `z_max_m`, `top_height_m`, `height_diff_summary`: 예 `prev_height_m`, `curr_height_m`, `delta_m`, `suspected_reason`)** 를 인터페이스로 정의해 받는다. 이렇게 하면 `-630`이 서버/워커 어디에서 계산되든 같은 구조로 연결된다.

4. **[위험 4 · 좌표 정합] 전·후 세팅이 서로 다른 World 좌표계일 때 투영 불가**
   - **원인:** `world_anchor.accepted == false`(예: 첫 세팅이거나 카메라/현장이 완전히 달라져 World 재고정이 기각된 경우)이면 지난 세팅 구역의 World `polygon_xy`를 이번 세팅 카메라 보정(`calibration_version`)에 투영할 수 없다(좌표계가 다름, AGENTS 규칙 2·3).
   - **해결:** 전후 비교와 VLM 의견 생성은 **`world_anchor.accepted == true`(같은 World 좌표계 유지)** 이고 **지난 세팅의 `calibration_version` 문서와 프레임(`CAMERA_FRAME` 또는 `BAKE_IMAGE`)이 존재할 때만** 수행한다. 하나라도 없으면 지어내지 않고 의견 생성을 건너뛴다(`status: "NOT_APPLICABLE"` 또는 빈 목록, AGENTS 규칙 2 "모르면 비운다").

5. **[위험 5 · 프라이버시/규칙 준수] 얼굴 비식별·외부 전송 금지·자동 반영 금지**
   - **AGENTS 규칙 4·8 및 지시서 제약:**
     - 사진은 절대 외부 LLM(`gpt-6.1-sol`)으로 보내지 않고 **같은 GPU Pod 내 루프백 vLLM(`http://127.0.0.1:8000`, Cosmos3-Nano)** 으로만 보낸다(`VlmClient`가 이미 루프백과 `VlmFrame`만 허용함, `ai/src/simai_ai/vlm/client.py:101-104, 178-183`).
     - 전·후 프레임 모두 반드시 `gate_input_frames`(`prepare_vlm_frames`, `face_policy: "BLURRED"`)를 거쳐 `VlmFrame`으로 변환한 뒤 크롭해 VLM에 넣는다.
     - VLM의 `KEEP`(유지)·`MODIFY`(수정)·`REMOVE`(삭제) 권고는 **화면 배지와 한 줄 설명으로만 표시**하며, `setup_session_zone_plan`이나 `zone` 테이블을 자동으로 수정·삭제하지 않는다.
     - VLM 비가용·시간 초과·파싱 오류 시에도 세팅이나 구역 편집을 막지 않고 `"AI 의견 없음"`(`vlm_status: "NO_OPINION"`)으로 폴백한다(`zone_draft.py:8, 993-995`와 동일 원칙).

### 5.2 `docs/spec/`와 어긋날 수 있는 점

- `docs/spec/05-processing.md:179` (5.4 용도 4 `vlm_zone_draft`) 및 `docs/spec/08-features.md:22` (8.2 F-06):
  - 현재 명세 5.4 용도 4는 초기/작업별 세팅에서 "새 위험구역·고소 작업면 초안 제안(`vlm_zone_draft`)"만 기술하고 있고, **이전 세팅 대비 높이 변화가 감지된 기존 구역에 대해 전후 사진을 비교해 유지/수정/삭제 의견을 내는 흐름(`-630`·`-631`)** 은 아직 한 줄로 반영되어 있지 않다.
  - 다만 명세 5.4의 대원칙("역할: 설명과 제안만 한다. 경보 판단 권한은 없다", "발행 전 초안은 Tier 0 위험 판정에 절대 쓰지 않는다") 및 5.4 용도 5("정적 공간 변화 의견")와 완전히 부합하므로, 구현 티켓 또는 명세 동기화 시 5.4 용도 4와 12.5/12.6에 변경 의심 구역 VLM 의견 필드를 함께 부기하면 된다(AGENTS 규칙: `docs/spec/` 수정은 사용자 확인 후 진행).

---

## 6. 새로 필요한 계약 필드 초안 (`contracts/`)

기존 `contracts/messages/vlm_zone_draft.schema.json`을 마이너 확장(예: `1.1.0`)하여 한 번의 VLM 세팅 작업에서 새 구역 초안(`drafts`)과 변경 의심 기존 구역 의견(`zone_opinions`)을 함께 다루는 초안이다. (별도 작업 유형으로 분리할 경우에도 동일한 `$defs` 구조를 그대로 쓴다.)

### 6.1 워커 작업 입력 (`vlm_zone_draft.schema.json#/$defs/input` 추가 필드 초안)

```json
{
  "previous_setup_session_id": { "$ref": "../common/defs.schema.json#/$defs/uuid" },
  "previous_calibration_version": { "$ref": "../common/defs.schema.json#/$defs/version_id" },
  "previous_calibration_ref": { "$ref": "../common/defs.schema.json#/$defs/object_uri" },
  "previous_calibration_hash": { "$ref": "../common/defs.schema.json#/$defs/sha256" },
  "previous_frames": {
    "type": "array",
    "maxItems": 8,
    "items": { "$ref": "#/$defs/frame_input" },
    "description": "지난 세팅의 카메라별 대표 프레임(CAMERA_FRAME 또는 BAKE_IMAGE, face_policy=BLURRED)"
  },
  "suspected_zones": {
    "type": "array",
    "maxItems": 64,
    "items": { "$ref": "#/$defs/suspected_zone_input" },
    "description": "높이 비교(-630)가 변경 의심으로 표시한 기존 구역 목록"
  }
}
```

#### `$defs/suspected_zone_input`
```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["zone_id", "name", "zone_kind", "severity", "polygon_xy", "z_min_m", "z_max_m"],
  "properties": {
    "zone_id": { "type": "string", "minLength": 1, "maxLength": 128 },
    "name": { "type": "string", "minLength": 1, "maxLength": 160 },
    "zone_kind": { "type": "string", "minLength": 1, "maxLength": 64 },
    "severity": { "$ref": "#/$defs/draft_severity" },
    "polygon_xy": {
      "type": "array",
      "minItems": 3,
      "maxItems": 256,
      "items": { "$ref": "../common/defs.schema.json#/$defs/vec2" }
    },
    "z_min_m": { "type": "number" },
    "z_max_m": { "type": "number" },
    "top_height_m": { "type": ["number", "null"] },
    "previous_height_m": { "type": ["number", "null"], "description": "지난 세팅 점군/메시에서 관측된 구역 대표 높이(m)" },
    "current_height_m": { "type": ["number", "null"], "description": "이번 세팅 점군/메시에서 관측된 구역 대표 높이(m)" },
    "height_delta_m": { "type": ["number", "null"], "description": "이번 - 지난 높이 차이(m, -630 계산값)" },
    "suspected_reason": { "type": ["string", "null"], "maxLength": 200 }
  }
}
```

### 6.2 워커 작업 결과 (`vlm_zone_draft.schema.json#/$defs/result` 추가 필드 초안)

```json
{
  "zone_opinions": {
    "type": "array",
    "maxItems": 64,
    "items": { "$ref": "#/$defs/zone_opinion_item" }
  }
}
```

#### `$defs/zone_opinion_item`
```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "zone_id",
    "vlm_status",
    "recommended_action",
    "rationale",
    "before_camera_id",
    "after_camera_id"
  ],
  "properties": {
    "zone_id": { "type": "string", "minLength": 1, "maxLength": 128 },
    "vlm_status": { "$ref": "#/$defs/vlm_opinion_status" },
    "recommended_action": {
      "type": ["string", "null"],
      "enum": ["KEEP", "MODIFY", "REMOVE", null],
      "description": "VLM 권고(유지 KEEP · 수정 MODIFY · 삭제 REMOVE). NO_OPINION 이면 null"
    },
    "rationale": {
      "type": "string",
      "minLength": 1,
      "maxLength": 300,
      "description": "무엇이 바뀌었는지와 권고 이유 한 줄(한국어). NO_OPINION 이면 'AI 의견 없음'"
    },
    "confidence": {
      "oneOf": [
        { "type": "number", "minimum": 0, "maximum": 1 },
        { "type": "null" }
      ]
    },
    "before_camera_id": {
      "oneOf": [
        { "$ref": "../common/defs.schema.json#/$defs/camera_id" },
        { "type": "null" }
      ]
    },
    "after_camera_id": {
      "oneOf": [
        { "$ref": "../common/defs.schema.json#/$defs/camera_id" },
        { "type": "null" }
      ]
    },
    "before_crop_box_norm": {
      "oneOf": [
        {
          "type": "array",
          "minItems": 4,
          "maxItems": 4,
          "items": { "type": "number", "minimum": 0, "maximum": 1 }
        },
        { "type": "null" }
      ],
      "description": "지난 세팅 프레임 내 잘라낸 영역 정규화 좌표 [u1, v1, u2, v2]"
    },
    "after_crop_box_norm": {
      "oneOf": [
        {
          "type": "array",
          "minItems": 4,
          "maxItems": 4,
          "items": { "type": "number", "minimum": 0, "maximum": 1 }
        },
        { "type": "null" }
      ],
      "description": "이번 세팅 프레임 내 잘라낸 영역 정규화 좌표 [u1, v1, u2, v2]"
    }
  }
}
```

### 6.3 서버 → 프론트엔드 REST 응답 확장 초안 (`GET /api/setup-sessions/{id}/zone-drafts` 또는 `GET /api/setup-sessions/{id}/zones`)

서버가 프론트엔드에 내려줄 때는 `zone_opinions[]` 각 항목에 `before_camera_id` / `after_camera_id`에 대응하는 `setup_session_artifact`(`CAMERA_FRAME` 또는 `BAKE_IMAGE`)의 5분 유효 서명 GET URL을 붙여 내려준다:

- `before_image_url`: `string | null` (지난 세팅 해당 카메라 프레임 서명 GET URL)
- `after_image_url`: `string | null` (이번 세팅 해당 카메라 프레임 서명 GET URL)
- `image_expires_at_ms`: `number | null`

---

## 7. 추천 구현 순서 (5단계 작업 단위)

1. **[1단위 · 계약 및 `REUSE_SCENE` 프레임 보존 열기 (`contracts/` + `ai/` + `server/`)]**
   - `site_setup.schema.json`에서 `mode=REUSE_SCENE`일 때도 `artifacts.camera_frames` 업로드를 허용하고, `site_setup.py`가 `REUSE_SCENE`에서도 `publish_frames`를 호출해 `setup_session_artifact`(`CAMERA_FRAME`)에 이번 세팅 대표 프레임이 항상 남도록 수정.
   - `vlm_zone_draft.schema.json`에 `previous_calibration_ref`, `previous_frames`, `suspected_zones`, `zone_opinions` 선택 필드를 마이너 버전으로 추가.
2. **[2단위 · AI 구역 전후 투영·크롭 및 VLM 변경 의견 추론 모듈 (`ai/src/simai_ai/vlm/`)]**
   - 구역 3D 상자(`polygon_xy` + `[z_min_m, top_height_m/z_max_m]`)를 지난 보정(`prev_pose`)과 이번 보정(`curr_pose`)에 모두 투영해 공통 최적 카메라를 고르고, 얼굴 처리된 `VlmFrame`에서 전·후 크롭 쌍(`before_crop`, `after_crop`)과 정규화 크롭 좌표(`[u1, v1, u2, v2]`)를 뽑는 순수 함수 작성 + 단위 테스트(`pytest`).
   - Cosmos3-Nano에 전·후 크롭 2장과 높이 변화 수치를 넣어 `{ recommended_action: "KEEP"|"MODIFY"|"REMOVE", rationale, confidence }` JSON을 받는 프롬프트·파서 및 `NO_OPINION` 폴백 구현.
3. **[3단위 · 워커 `VlmZoneDraftHandler` 연동 (`ai/services/worker/vlm_zone_draft.py`)]**
   - 입력에 `suspected_zones`와 `previous_frames`·`previous_calibration_ref`가 주어지면 `gate_input_frames`로 전·후 프레임을 얼굴 비식별 처리한 뒤 2단위 함수를 호출해 `result.zone_opinions`를 채워 반환하도록 연결 + 워커 단위 테스트.
4. **[4단위 · 서버 직전 세팅 프레임 조회 · 작업 큐잉 · 서명 GET URL 응답 (`server/`)]**
   - `SetupService.enqueueZoneDraftJob`에서 `world_anchor.accepted == true`이고 변경 의심 구역(`-630`)이 있을 때 직전 완료 세팅의 `calibration_ref`와 `CAMERA_FRAME` 목록을 조회해 `vlm_zone_draft` 입력에 포함.
   - 콜백 수신 시 구역별 의견을 저장(`setup_zone_opinion` 테이블 또는 `setup_zone_draft_run` JSONB 컬럼)하고, 조회 시 `before_image_url`·`after_image_url`(5분 서명 GET)을 붙여 반환.
5. **[5단위 · 관리자 세팅 구역 화면 전후 사진·의견 표시 (`front/apps/admin`)]**
   - 세팅 구역 목록 카드(`SetupPanels.tsx` / `-629` `SetupZonePlanCard`)에서 변경 의심 구역 항목에 VLM 권고 배지(`유지 권고` / `수정 권고` / `삭제 권고` / `AI 의견 없음`), 한 줄 의견(`rationale`), 전·후 카메라 크롭 썸네일 2장을 표시하고, 관리자가 `[유지]`·`[수정]`·`[삭제]` 동작을 직접 선택할 수 있게 연결(자동 수정 없음).

---

## 8. 남은 질문 (구현 착수 전 결정 사항)

1. **`-630`(높이 비교) 산출 시점과 주체:**
   - 높이 비교(`-630`)가 워커(`site_setup` 또는 `vlm_zone_draft`) 안에서 점구름/메시로 계산되는지, 아니면 서버에서 계산되어 내려오는지에 따라 `suspected_zones`를 서버가 작업 입력에 넣을지 워커가 직접 뽑아 곧바로 VLM에 넘길지가 결정된다. (워커가 미리보기 점구름에서 높이 비교 후 같은 작업 안에서 바로 VLM을 부르면 왕복이 한 번 줄어든다.)
2. **화면 전후 사진의 크롭 주체 (워커 S3 업로드 vs 브라우저 정규화 박스 크롭):**
   - 브라우저에 보여줄 전·후 사진을 워커가 얼굴 블러 + 구역 테두리 오버레이까지 마친 작은 크롭 JPEG로 S3에 올릴지(프라이버시·전송량에 유리), 아니면 기존 `BAKE_IMAGE`/`CAMERA_FRAME` 서명 URL에 정규화 박스 `crop_box_norm`만 내려 프론트엔드에서 잘라 보여줄지 결정이 필요하다.
3. **`FULL_SETUP`에서 `BAKING_TEXTURE` 진행 중 VLM 응답 시점:**
   - 단일 워커 환경에서 정밀 텍스처 굽기(`BAKING_TEXTURE`)가 끝날 때까지 기다리지 않고 `READY_FOR_ZONES` 직후 즉시 VLM 구역 초안·의견을 받으려면, `site_setup`의 `(g)` 단계 끝에서 VLM 평가를 인라인(또는 보조 스레드)으로 먼저 수행할지 여부를 함께 정하면 좋다.
