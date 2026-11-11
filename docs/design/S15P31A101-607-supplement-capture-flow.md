# S15P31A101-607 세팅 맵 보강 촬영과 다시 굽기

관련 명세: 3.2(Tier 0 정본, Visual 층은 위치·판정에 들어가지 않음), 8.1(현장 세팅), 12.5(I5 작업), 12.6(현장 세팅 REST), 14(객체 저장소 서명 주소). 계약: `contracts/messages/site_setup.schema.json` 1.10.0(-605, 묶음 v7.33.0, job_request 3.9.0·job_callback 2.17.0·job_heartbeat 2.9.0)의 입력 `supplement_media[]`·`mode: SUPPLEMENT_REBAKE`·`rebake_base`, 결과 `supplement`. 앞 문서 `docs/design/site-setup-pipeline.md`(세팅 워커·서버 등록), `docs/design/S15P31A101-593-setup-bake-images.md`(굽기 입력 사진), `docs/design/S15P31A101-602-setup-camera-summary.md`(세션 조회 `cameras`·`pair_holdout`).

이 브랜치는 dev 가 아니라 -605 브랜치(`S15P31A101-605-map-supplement-capture`, 계약 판 다시 매김 d9685994·워커 68575084 까지 merge) 위에 있다. -605 가 먼저 머지돼야 한다.

## 문제

고정 카메라 4대로 구운 맵에는 책상·기둥 뒤, 카메라 바로 아래처럼 아무 카메라도 못 보는 빈 곳이 남는다(코치 피드백, 2026-11-11 사용자 결정). 미리보기 맵이 나온 뒤 관리자가 그 빈 곳을 휴대폰·액션캠·DSLR 로 따로 찍어 올리면, 워커가 그 파일과 고정 카메라 4대 입력으로 맵만 다시 굽는 길(계약 -605)이 있지만 그 파일을 올리고 작업을 넣고 결과를 받는 서버·화면이 없었다.

## 한 일

- 서버
  - 마이그레이션 `V53__setup_supplement_capture.sql`: `setup_supplement_media`(파일 기록), `setup_supplement_rebake`(다시 굽기 요청·진행·결과).
  - `SetupSupplementService`: 업로드 서명·확인·목록에서 빼기, 다시 굽기 요청·취소, 하트비트·결과 처리, 세션 조회의 `supplement`.
  - `SetupController`: 아래 API 5개.
  - `SetupService`: site_setup 하트비트·콜백을 mode(다시 굽기 작업인지)로 갈라 넘기고, APPLIED 면 정식 Scene 과 같은 절차로 현재화. 세팅 시작은 같은 현장 다시 굽기가 진행 중이면 409. 세션 조회에 `supplement` 를 싣는다.
  - `SetupArtifactRegistrar.registerRebakeScene`: APPLIED GLB 를 세션 귀속 SCENE 후보로 등록(정식 Scene 과 같은 경로·크기·체크섬 검사).
  - `SceneCameraImageService`: 다시 구운 Scene 도 기준 굽기 입력 사진(원래 세팅 대여)으로 카메라 시점 VDT 사진을 준다.
  - `ObjectStorage.signPut`: sha256 이 null 이면 해시를 서명에 묶지 않는다(큰 동영상).
  - 오류 코드 `ErrorCode.Setup.SUPPLEMENT_*`·`INVALID_SUPPLEMENT_MEDIA`.
- front: 세팅 구역 단계 오른쪽 칸의 「맵 보강 촬영(선택)」 카드, 휴대폰 화면 `/m/supplement/$sessionId`, mock(`mocks/setup.ts`), vitest. 자세한 파일은 아래 「front」 절.
- 계약은 바꾸지 않았다(-605 그대로).

## 사용 흐름과 화면 상태

1. 세팅 1·2단계 뒤 미리보기 맵이 나오면(세션 `READY_FOR_ZONES` 이후) 구역 단계 오른쪽 칸에 「맵 보강 촬영(선택)」 카드가 보인다. 「건너뛰기」 하면 한 줄로 접히고 다시 열 수 있다.
2. 카드: 빈 곳 안내(글 목록) → 휴대폰 QR·링크 → PC 끌어 놓기(여러 파일, 액션캠·DSLR 파일) → 올린 목록(진행률·상태·빼기).
3. 휴대폰은 QR 로 `/m/supplement/<세션>` 을 열고(로그인이 없으면 로그인 뒤 돌아옴) 동영상·사진을 찍거나 갤러리에서 골라 올린다. 촬영 요령(고정 카메라도 보이는 곳에서 시작, 천천히, 기본 1x 렌즈, 초광각·어안 금지, 무늬 없는 벽만 찍지 않기)을 같이 보인다.
4. 정식 맵 굽기가 끝나면(세션 `COMPLETED`) 「다시 굽기」가 켜진다. 그 전에는 올리기만 되고 단추는 사유와 함께 꺼져 있다.
5. 다시 굽기 중: 진행률(BAKING_TEXTURE)·취소. 끝나면 쉬운 말로 결과: 반영됨(새 맵이 현재 맵이 됐는지 함께) / 원래 맵 유지 + 사유, 파일별 「붙지 않음」 사유.

| 세션·다시 굽기 상태 | 올리기 | 다시 굽기 단추 | 사유 코드(`rebake_blocked_reason`) |
|---|---|---|---|
| 미리보기 전, 실패 | 불가 | 불가 | `SESSION_NOT_READY` |
| 더 새 세팅 세션이 있음 | 불가 | 불가 | `SUPERSEDED` |
| READY_FOR_ZONES·BAKING_TEXTURE(원래 작업 진행 중) | 가능 | 불가 | `SETUP_JOB_RUNNING` |
| COMPLETED, 다시 굽기 진행 중 | 가능 | 불가(취소 가능) | `REBAKE_RUNNING` |
| COMPLETED, 관제 중(IN_PROGRESS) 작업 있음 | 가능 | 불가 | `LIVE_WORK` |
| COMPLETED, 올리기를 마친 파일 없음 | 가능 | 불가 | `NO_MEDIA` |
| COMPLETED, site_setup 워커 없음 | 가능 | 불가 | `NO_WORKER` |
| COMPLETED, 보정 문서·굽기 입력 사진 모자람 | 가능 | 불가 | `BASE_MISSING` |
| 그 밖 | 가능 | 가능 | null |

## API

모두 관리자(ADMIN) JWT. 세션 조회만 인증된 열람자.

### 업로드 서명 `POST /api/setup-sessions/{id}/supplement-media` → 201

```json
{ "content_type": "video/mp4", "size_bytes": 58213440, "source": "PHONE_UPLOAD", "file_name": "VID_20261111.mp4" }
```
선택: `sha256`(16진수 64자, 주면 서명에 묶음), `captured_at_ms`, `device: {make, model}`(모르면 비움).

```json
{
  "media_id": "sup-3f9c0a1b2c3d4e5f",
  "kind": "VIDEO",
  "source": "PHONE_UPLOAD",
  "content_type": "video/mp4",
  "file_name": "VID_20261111.mp4",
  "size_bytes": 58213440,
  "sha256": null,
  "captured_at_ms": null,
  "device": null,
  "status": "PENDING_UPLOAD",
  "created_at_ms": 1762833600000,
  "uploaded_at_ms": null,
  "upload": {
    "method": "PUT",
    "url": "https://<버킷>.s3.ap-northeast-2.amazonaws.com/setup/<세션>/supplement/sup-3f9c0a1b2c3d4e5f.mp4?X-Amz-…",
    "headers": { "content-type": "video/mp4" },
    "expires_at_ms": 1762833900000
  }
}
```
키는 서버가 정한다: `setup/<setup_session_id>/supplement/<media_id><.mp4|.mov|.jpg|.png>`(계약 예시 주소와 같은 모양). 형식·크기(·해시)는 서명에 묶여 저장소가 다른 내용을 거부한다.

### 올리기 확인 `POST /api/setup-sessions/{id}/supplement-media/{media_id}/complete` → 200

저장소 HEAD 로 있음·크기·(해시를 알렸고 저장소가 기록했으면) 체크섬을 본 뒤 `status: UPLOADED`, `uploaded_at_ms`. 없거나 다르면 409 `SUPPLEMENT_UPLOAD_INCOMPLETE`. 이미 확인했으면 그대로 돌려준다.

### 목록에서 빼기 `DELETE /api/setup-sessions/{id}/supplement-media/{media_id}` → 204

`removed_at` 만 남긴다(덮어쓰지 않음, 객체도 지우지 않음). 진행 중 다시 굽기가 쓰는 파일은 409 `SUPPLEMENT_MEDIA_IN_USE`.

### 다시 굽기 `POST /api/setup-sessions/{id}/supplement-rebake` → 202, 취소 `POST …/supplement-rebake/cancel` → 200

본문 없음. 응답은 세션 조회와 같은 모양. 막히면 위 표의 사유에 맞는 409(`SUPPLEMENT_SESSION_NOT_READY`·`SUPPLEMENT_SESSION_SUPERSEDED`·`SUPPLEMENT_SETUP_JOB_RUNNING`·`SUPPLEMENT_REBAKE_IN_PROGRESS`·`SETUP_BLOCKED_BY_LIVE_WORK`·`SUPPLEMENT_NO_MEDIA`·`SETUP_WORKER_UNAVAILABLE`·`SUPPLEMENT_REBAKE_BASE_MISSING`). 취소는 진행 중이 없으면 409 `SUPPLEMENT_REBAKE_NOT_RUNNING`.

### 세션 조회 `GET /api/setup-sessions/{id}` 의 `supplement`

계약 스키마가 없는 조회 응답이라 -602 의 `cameras`·`pair_holdout` 처럼 최상위 snake_case 객체로 싣고, 모르는 값은 null 이다. 생성·취소·재시도·다시 굽기 응답도 같은 모양이다.

```json
"supplement": {
  "media": [ { "media_id": "sup-…", "kind": "VIDEO", "status": "UPLOADED", "…": "업로드 응답과 같은 칸(upload 제외)" } ],
  "limits": { "max_items": 32, "max_video_bytes": 1073741824, "max_image_bytes": 52428800, "max_total_bytes": 4294967296,
              "content_types": ["video/mp4", "video/quicktime", "image/jpeg", "image/png"] },
  "can_upload": true,
  "can_rebake": false,
  "rebake_blocked_reason": "REBAKE_RUNNING",
  "rebake": {
    "rebake_id": "…", "job_id": "job-…", "status": "SUCCEEDED", "stage": "COMPLETED", "progress_pct": 100,
    "media_ids": ["sup-…"], "base_scene_version": "scene-job-…", "scene_version": "scene-job-…-rebake",
    "outcome": "APPLIED", "reason": null,
    "result": { "status": "APPLIED", "reason": null, "input_role": "SETUP_SUPPLEMENT_CAPTURE", "media_count": 2,
                "frames_selected": 34, "frames_registered": 31, "alignment": { "…": "…" },
                "media": [ { "media_id": "sup-…", "status": "USED", "frames_selected": 32, "frames_registered": 30 } ] },
    "scene_activated": true,
    "failure_code": null, "failure_message": null,
    "requested_at_ms": 1762833600000, "completed_at_ms": 1762834100000
  }
}
```
- `rebake` 는 그 세션의 가장 최근 다시 굽기(없으면 null). `status`: QUEUED·RUNNING(첫 하트비트 또는 작업 대여됨)·SUCCEEDED·FAILED·CANCELLED. `result` 는 워커의 `supplement` 요약 원문.
- `scene_activated`: APPLIED Scene 을 이 세션 이름(`setup:<세션>`)으로 현재 Scene 에 올렸는지(`scene_activation` 이력). APPLIED 가 아니면 null.

## 작업 입력(job_request)

세션의 처음 site_setup 입력(`analysis_job.input`: setup_session_id·work_unit_id·site_id·camera_heights_m·calibration_target·height_tolerance_cm)을 복사하고 아래를 더한다. 등록할 때 `AnalysisJobService` 가 계약(job_request)으로 검사한다(어긋나면 400 `INVALID_JOB`, 시험에서도 대여 응답을 계약으로 검사한다).

| 칸 | 출처 |
|---|---|
| `mode` | `SUPPLEMENT_REBAKE` |
| `supplement_media[]` | 올리기를 마치고 빼지 않은 파일(만든 순, 최대 32). `uri` = `s3://<버킷>/<키>`, `source`·`captured_at_ms`·`device`·`sha256` 은 있을 때만, `size_bytes`. `camera_id` 없음 |
| `rebake_base.calibration_version` | 세션 `calibration_version` |
| `rebake_base.calibration_document` | 그 보정을 이 세션이 등록한 `setup_session_artifact` CALIBRATION 행(주소·sha256·크기) |
| `rebake_base.preview_scene_version` | 세션 `preview_scene_version` |
| `rebake_base.scene_version` | 세션 `scene_version`(정식 맵이 없으면 미리보기. -597 굽기 회수 세션 대비) |
| `rebake_base.bake_images` | 원래 세팅 작업이 알린 BAKE_IMAGE(-593) 중 한 대여의 묶음. 정식 Scene 을 등록한 대여를 먼저, 없으면 가장 최근 대여. 세션 카메라마다 하나, 묶음 ID 하나여야 한다(아니면 `BASE_MISSING`) |

작업: `site_setup`, 최대 시도 2, 시간 상한 30분(계약 예시와 같음. 배경 수집·보정이 없고 프레임 고르기·등록·굽기만 한다).

## 결과 받기

다시 굽기 작업의 하트비트·콜백도 기존 site_setup 경로(`JobLeaseService` → `JobProgress`·`JobFinished` → `SetupService`)로 온다. `SetupService` 가 그 작업이 `setup_supplement_rebake.job_id` 이면 `SetupSupplementService` 로 넘긴다(세션의 원래 작업 처리와 섞이지 않는다).

- 하트비트: `mode = SUPPLEMENT_REBAKE`, `status = BAKING_TEXTURE`, `setup_session_id`·`calibration_version`·`preview_scene_version` 이 기준과 같아야 한다. 진행률은 같은 대여 안에서 줄지 않는다. 다시 굽기 행만 바꾸고 세션 본체(상태·진행률)는 그대로다. 어긋나면 400 `INVALID_SETUP_PROGRESS`.
- 결과 SUCCEEDED: `status = COMPLETED`, 진행률 100, mode·세션·기준 보정·미리보기 일치, `supplement` 필수, `supplement.media[].media_id` 는 요청한 파일만.
  - APPLIED: `scene_version` ≠ 기준, `artifacts` 는 `scene` 하나만. 이 다시 굽기 작업·대여 경로의 GLB 를 HEAD(크기·체크섬)로 보고 세션 귀속 SCENE 후보(`quality.notes` 에 `mode=SUPPLEMENT_REBAKE; input_role=SETUP_SUPPLEMENT_CAPTURE; rebake_base_scene=…; safety_use=false`)로 등록, 세션 `scene_version` 을 새 버전으로 옮긴다(다음 다시 굽기의 기준. 이력은 다시 굽기 행).
  - FALLBACK·SKIPPED: `scene_version` = 기준, `artifacts` 없음. 아무 Scene 도 등록·활성화하지 않는다. 작업은 SUCCEEDED, 화면은 사유와 파일별 결과만.
  - 어긋나면 400 으로 제출 전체가 되돌아간다(워커가 다시 보낸다).
- 실패·시간 초과·취소: 다시 굽기 행만 FAILED·CANCELLED(사유 코드·문구). 원래 맵은 그대로다.

## 활성화 규칙

APPLIED Scene 은 세팅 정식 Scene 과 같은 절차(`SetupService.promoteSetupSceneAfterCommit`)로, 결과 커밋 뒤 별도 트랜잭션에서 **자동으로** 현재 Scene 에 올린다. 단,

- 지금 현재 Scene 이 그 다시 굽기의 기준 맵(`rebake_base.scene_version`)일 때만(`SceneActivationService.activateIfCurrent`). 그 사이 관리자가 다른 Scene 을 골랐으면 건드리지 않고 후보로만 남는다(`scene_activated: false`, 화면은 "새 맵을 만들었지만 지금 맵은 바꾸지 않았습니다").
- 같은 현장에 관제 중(IN_PROGRESS) 작업이 있으면 올리지 않는다(관제 중 맵이 바뀌지 않게).
- 이 세션이 등록한 보정·Scene 일 때만. 이력은 `scene_activation` 에 actor `setup:<세션>` 으로 추가(덮어쓰지 않음, 되돌리기는 기존 Scene 화면에서).

자동으로 정한 이유: 관리자가 「다시 굽기」를 누른 것 자체가 맵을 바꾸겠다는 뜻이고, 품질 관문(정렬·재투영이 나빠지면 FALLBACK)은 워커가 이미 본다. 세팅 정식 Scene 도 같은 조건으로 자동 교체하므로 화면·운영 규칙이 하나로 맞는다.

## 상한과 근거

| 값 | 정한 것 | 근거 |
|---|---|---|
| 파일 수 | 세션당 32(빼지 않은 것) | 계약 `supplement_media` maxItems |
| 동영상 한 개 | 1 GiB | 4K30 H.264 약 2.5분, 1080p 약 8분. 빈 곳을 천천히 한 바퀴(1~2분)면 충분하고, 현장 Wi-Fi 업로드·워커 내려받기 시간을 묶는다. 단일 PUT(5 GB 한계) 안 |
| 사진 한 장 | 50 MiB | DSLR 최고화질 JPEG 10~25 MB, 4K PNG 20~40 MB |
| 세션 전체 | 4 GiB | 워커가 30분 예산 안에서 내려받아 프레임을 고를 양(GPU 인스턴스 임시 디스크 포함). 계약 size_bytes 상한(4 GiB)과 같다 |
| 형식 | video/mp4, video/quicktime, image/jpeg, image/png | 휴대폰(안드로이드 mp4, iPhone mov)·액션캠·DSLR 기본 출력. HEIC 는 워커 디코더가 보장되지 않아 받지 않는다(iOS 는 `image/*` 입력을 JPEG 로 바꿔 준다) |
| 해시 | 선택 | 휴대폰이 큰 동영상의 SHA-256 을 미리 계산하면 메모리·시간이 크다. front 는 64 MiB 이하만 계산해 보내고, 서버는 받았을 때만 서명에 묶는다. 없으면 HEAD 크기만 확인한다 |
| 서명 유효 시간 | 기존 `simai.storage.upload-ttl`(기본 5분) | PUT 시작 시각만 본다(긴 업로드도 시작이 유효 시간 안이면 된다). 실패하면 새로 받는다 |

## 상태 가드

- 올리기: 세션이 READY_FOR_ZONES·BAKING_TEXTURE·COMPLETED 이고 보정·미리보기 버전이 있음, 같은 현장에 더 새 세팅 세션이 없음. 정식 맵 굽기 도중에도 올릴 수 있다(기다리는 동안 찍게).
- 다시 굽기: 위 + 세션의 원래 site_setup 작업이 끝남(QUEUED·LEASED 아님 → 사실상 COMPLETED), 이 세션의 다시 굽기가 진행 중이 아님(부분 유일 색인 `setup_supplement_rebake_one_running` + 세션 행 잠금 + 현장 행 잠금), 관제 중 작업 없음(세팅 시작과 같은 LIVE 가드), 올리기를 마친 파일 1개 이상, site_setup 워커 있음, 기준 자료가 모두 있음.
- 같은 현장 다시 굽기가 진행 중이면 새 세팅 시작도 409 `SETUP_ALREADY_IN_PROGRESS`(GPU 인스턴스는 하나다).
- AI 구역 초안(vlm_zone_draft) 작업은 맵을 바꾸지 않아 막지 않는다.

## 버린 대안

- **휴대폰을 카메라로 등록**: 카메라 연결 관리·스트림·보정에 들어가 Tier 0 World 와 관제가 흔들린다. 보강 촬영 기기는 한 번 쓰고 끝나는 파일 출처일 뿐이라 `camera` 표·스트림에 넣지 않았다(계약에도 `camera_id` 가 없다).
- **기존 `POST /api/uploads/sign` 에 목적 추가**: 그 경로는 기기 토큰·Tier 0·반장 JWT 체인이고 sha256 이 필수다. 세션 귀속·상한·확인(HEAD)·목록 관리가 필요해 세팅 도메인에 전용 표와 API 를 두었다.
- **세션의 원래 작업(job_id)을 다시 쓰기**: `setup_session.job_id` 는 유일하고 세션 단계·진행률이 그 작업에 묶여 있다. 다시 굽기는 별도 작업·별도 행으로 두어 세션 본체(COMPLETED)를 흔들지 않는다.
- **사람이 확인한 뒤에만 활성화**: 단계가 하나 늘고, 정식 Scene 자동 교체와 규칙이 갈린다. 대신 기준 맵이 그대로일 때만 바꾸는 조건으로 관리자 선택을 지킨다.
- **결과 GLB 를 그대로 덮어쓰기**: 덮어쓰지 않는다(규칙 6). 새 scene_version 후보 + 활성화 이력.
- **동영상 해시 필수**: 휴대폰에서 1 GiB 해시 계산은 느리고 메모리가 크다. 선택으로 두고 크기는 늘 확인한다.

## 잔여 위험

- 브라우저에서 촬영 시각·기기 제조사·모델(EXIF·동영상 메타데이터)을 읽지 않아 `captured_at_ms`·`device` 는 비어 간다(지어내지 않음). 출처 기록이 약하다.
- 해시 없이 올린 큰 동영상은 저장소가 내용 해시를 대조하지 않는다(크기만). 워커도 대조할 해시가 없다.
- 업로드 서명만 받고 올리지 않거나 빼기한 파일의 객체는 지우지 않는다(저장소 비용 작음, 보관 정리는 넘긴 일).
- 다시 굽기 도중 관제(작업 시작)가 시작되면 작업은 계속 돌고(워커가 Tier 0 에 양보), 결과가 APPLIED 여도 관제 중이라 현재화하지 않는다. 관리자가 관제 뒤 Scene 화면에서 고를 수 있다.
- 실제 워커(-605 이후 구현)와 실기기(휴대폰 촬영·S3 CORS) 업로드는 이 티켓에서 검증하지 못했다. 버킷 CORS 가 `x-amz-checksum-sha256` 를 허용해야 해시 묶은 PUT 이 된다(기존 서명 업로드와 같은 조건).
- DB 마이그레이션 번호: V49(-602 `V49__setup_session_pair_holdout`)·V50(-597 `V50__setup_session_bake_skipped`)·V51(-609 `V51__tbm_daily_form`, 핫픽스 !556)·V52(-606 `V52__floor_coverage`)를 피해 V53. Flyway outOfOrder 가 꺼져 있어 머지 순서는 -602(V49) → -597(V50) → -609 핫픽스(V51) → -606(V52) → -607(V53) 이어야 한다(Lead 결정). V53 이 먼저 들어가면 뒤의 앞 번호가 순서 검사에 걸린다.

## 넘긴 일

- 빈 곳 안내를 맵 위 구멍 표시(시각화)로 바꾸기(지금은 글 목록).
- 촬영 메타데이터(EXIF·QuickTime) 읽어 `captured_at_ms`·`device` 채우기.
- 빼기·오래된 보강 촬영 객체 보관 기간 정리.

## front

- `features/setup/supplement.ts`: 파일 검사(형식·종류별 크기·개수·전체 상한), 사유·파일 상태·막힌 사유의 한국어 문구(계약 reason 전부), 휴대폰 주소(`${origin}/m/supplement/<세션>`), 오류 코드 → 문구(`isSetupBlockedByLiveWork` 를 쓰지 않고 코드로 가름).
- `features/setup/supplementUpload.ts`: 한 번에 하나씩 올리는 대기열. 차례가 오면 서명 → XHR PUT(진행률, content-length·host 머리글 뺌) → 확인. 실패하면 DELETE 로 자리를 비우고 행을 「실패 · 다시 올리기」로. 64 MiB 이하이고 `crypto.subtle` 이 있을 때만 SHA-256 을 계산한다.
- `features/setup/useSupplementCapture.ts`: 두 화면이 같이 쓰는 훅(올리기·빼기·다시 굽기·취소, 세션 조회 무효화).
- `SupplementCaptureCard.tsx`(세팅 구역 단계 오른쪽 칸 끝, 미리보기 이후만 `supplementVisible`), `SupplementUploadList.tsx`, `SupplementQr.tsx`(`uqr` 0.1.2, MIT, 인라인 SVG·`.paper` 바탕이라 다크 모드에서도 읽힘), `SupplementPhonePage.tsx`.
- 휴대폰 화면: `routes/_mobile.tsx`(관제 상단 바·도크 없는 레이아웃, `_app` 과 같은 로그인 가드 → 로그인 뒤 돌아옴), `routes/_mobile/m.supplement.$sessionId.tsx`. 동영상 찍기(`accept="video/*" capture="environment"`)·사진 찍기·갤러리에서 고르기(`multiple`), 촬영 요령, 올리기가 막히면 사유.
- `setup.store.ts`: 세션별 「건너뛰기」 표시. `useSetupStep1.ts`: 끝난 세션이어도 다시 굽기가 진행 중이면 계속 조회.
- mock: `mocks/setupSupplement.ts`(같은 출처 `/mock-storage/…` PUT, 다시 굽기 1.5초 대기 → 10초 BAKING_TEXTURE → 결과. 사진만 3장 미만이면 FALLBACK TOO_FEW_REGISTERED, 아니면 APPLIED, 이름에 wide·gopro 가 있으면 WIDE_FOV_REJECTED). 개발 서버 세팅 mock 은 `autoProgress` 로 시간에 따라 READY_FOR_ZONES(약 13초)·COMPLETED(약 35초)까지 가서 다시 굽기까지 해 볼 수 있다.
- 촬영 시각·기기(EXIF)는 읽지 않아 보내지 않는다.

## 시험

- `SetupSupplementIntegrationTest`(6): 업로드 서명·확인·형식·상한·빼기, 미리보기 전·정식 굽기 중 가드, 관제 중·파일 없음 가드, job_request 조립·계약 검사·하트비트·APPLIED 등록·현재화·VDT 사진, FALLBACK 유지, 취소 뒤 다시 요청.
- `SetupSupplementServiceTest`(3): 형식 → 종류, 파일 이름 다듬기, 상한 값.
- front: `supplement.test.ts`·`supplementUpload.test.ts`·`useSupplementCapture.test.tsx`(mock 으로 APPLIED·FALLBACK·막힘·취소)·`mocks/setupSupplement.test.ts`.
