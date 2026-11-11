# 세팅 보조 촬영으로 정적 3D 맵 빈칸 메우기 (S15P31A101-605)

- **티켓**: S15P31A101-605 (Owner: Claude 서브에이전트)
- **배경**: 코치 피드백(11-10)과 사용자 확인(11-11). 정적 3D 맵의 빈칸을 AI 생성으로 과하게 메우지 않는다. 그 대신 관리자가 세팅 때 따로 찍은 동영상·사진의 실제 관측으로 빈칸을 메운다.
- **범위**: ai(프레임 고르기, 공동 MapAnything, 고정 카메라 정렬, 관문, 병합, 굽기 연결, 오프라인 비교 실행기)와 contracts(입력, 결과 요약, 다시 굽기 진입점).
- **범위 밖**: 휴대폰·웹 업로드 UI와 서버 엔드포인트(-607), 커버리지 겹쳐 보기(-606), 명세 수정(아래 "명세 후속").
- **관련 명세**:
  - 3.2: GPU 우선순위, LIVE 중 배치 작업 금지
  - 4.1: World 좌표계
  - 4.3: 현장 세팅 자동 보정
  - 4.5: 보정 버전과 정적 Scene 계보
  - 5.3: 정적 3D, 현장 세팅 2단계 Scene, 표면 출처 지도
  - 12.2·12.5: I5 작업 대여·결과
  - 18.1: 최초 설치
- **관련 결정**:
  - 미리 만든 물체 도형을 맵에 넣지 않는다(-524 중단). 실측 치수는 축척 근거로만 쓴다.
  - 맵 방향(11-06): 최우선은 GLB 빈칸을 자연스럽게 메우는 것이다. 생성형 채움은 기본으로 끈다(-527·-528·-532).
  - 재구성기는 MapAnything 하나로 둔다(Lead·사용자 11-11). COLMAP·3DGS·NeRF 경로는 만들지 않는다. 이유는 고정 카메라 4대를 통한 World 정렬, 메시 기반 하류, GPU 시간, 일정이다. 프레임이 많을수록 기존 융합(TSDF + texrecon)이 결과를 좋게 만드는 구조로 둔다.
  - 동영상과 사진은 동등한 입력이다(Lead 11-11). 한 세트에 섞여도 되고 여러 기기에서 와도 된다.

## 1. 원칙

1. **World 는 고정 카메라 4대만으로 정한다.** 보조 촬영은 맵의 형상과 텍스처에만 들어간다. 보정, 위치, 판정, Tier 0 에는 들어가지 않는다(AGENTS 규칙 1). 정렬(`solve_fixed_alignment`)은 MapAnything 출력에서 고정 카메라 view 만 골라 지금과 같은 similarity(카메라 중심, 회전 사전값, 바닥 평면 제약)를 푼다. 바닥 평면 점도 고정 카메라 view 에서만 뽑는다. 보조 프레임 자세는 출력일 뿐이다.
2. **보조 촬영 기기는 등록 카메라가 아니다.** camera_id, 카메라 연결 관리, 스트림 어디에도 들어가지 않는다. 작업 폴더 안 view 이름 `sup_<media_id>_f<프레임>` 은 MapAnything 입출력의 열쇠일 뿐이다. 결과의 `map_cameras`(뷰어·축척 점검용)에서는 이 view 를 뺀다(`annotate_bake`).
3. **세팅을 실패시키지 않는다.** 보조 촬영의 어떤 실패도 "보조 촬영 없는 맵(지금과 같음) + 사유 코드"로 바뀐다. 처리 단계를 멈추는 것은 취소뿐이다.
4. **모르면 비운다, 관측과 추정을 가른다(규칙 2·7).**
   - 촬영 시각과 기기를 모르면 싣지 않는다. 업로드 시각으로 채우지 않는다.
   - 출처는 `input_role: SETUP_SUPPLEMENT_CAPTURE`, 촬영 시각 범위, 기기 make/model, 파일별 결과로 남긴다.
   - 사람은 같은 분할기 마스크로 지운다. 생성형 채움은 이 티켓에서 더하지 않는다.

## 2. 계약 (contracts v7.31.0, site_setup 1.8.0 · job_request 3.9.0 · job_callback 2.15.0 · job_heartbeat 2.7.0)

### 2.1 입력 `supplement_media` (선택, 1~32)

```json
{"media_id": "sup-01", "kind": "VIDEO", "uri": "s3://…/sup-01.mp4",
 "source": "PHONE_UPLOAD", "captured_at_ms": 1760342100000,
 "device": {"make": "samsung", "model": "SM-S921N"}, "sha256": "…", "size_bytes": 58213440}
```

- 필수: `media_id`, `kind`(VIDEO|IMAGE), `uri`(s3://).
- 선택: `source`(PHONE_UPLOAD|FILE_UPLOAD), `captured_at_ms`, `device{make?, model?}`, `sha256`, `size_bytes`.
- 항목은 닫힌 값 객체다. `camera_id` 를 실을 수 없다(반례 `invalid/job_request.site_setup_supplement_camera_id`).
- 기기와 무관하다(휴대폰, 액션캠, DSLR). 내부 파라미터는 파일마다 모르는 값으로 둔다.
- 워커는 `sha256` 이 있으면 받은 바이트와 맞춰 보고, 다르면 그 파일을 DOWNLOAD_FAILED 로 둔다.

### 2.2 결과 요약 `supplement` (progress·result 선택)

```json
{"status": "APPLIED", "reason": null, "input_role": "SETUP_SUPPLEMENT_CAPTURE",
 "media_count": 3, "frames_selected": 34, "frames_registered": 31,
 "captured_from_ms": 1760342100000, "captured_to_ms": 1760342100000,
 "alignment": {"baseline_center_residual_m": 0.041, "supplement_center_residual_m": 0.046,
               "baseline_fixed_reprojection_px": 1.8, "supplement_fixed_reprojection_px": 1.9},
 "media": [{"media_id": "sup-01", "status": "USED", "frames_selected": 32, "frames_registered": 30}]}
```

- `status`:
  - APPLIED: 보조 촬영을 넣은 맵이다. `reason` 은 null 이어야 한다(스키마로 강제).
  - FALLBACK: 넣어 봤지만 관문이나 실패에 걸려 보조 촬영 없는 맵이다.
  - SKIPPED: 넣어 보지 않았다.
  - FALLBACK 과 SKIPPED 는 사유 코드가 필수다(스키마로 강제).
- `reason`: NO_USABLE_FRAMES, MEDIA_UNREADABLE, MEDIA_DOWNLOAD_FAILED, TOO_FEW_REGISTERED, ALIGNMENT_WORSE, FIXED_REPROJECTION_WORSE, RECONSTRUCTION_FAILED, BAKE_FAILED, PATH_UNSUPPORTED, SEGMENTER_MISSING, TIME_BUDGET 중 하나다.
- 파일별 `media[].status`: USED, DOWNLOAD_FAILED, UNREADABLE, NO_FRAMES, NOT_REGISTERED, WIDE_FOV_REJECTED 중 하나다.

### 2.3 다시 굽기 진입점 `mode: SUPPLEMENT_REBAKE` (방식 a: site_setup 의 모드)

사용 흐름: 세팅이 미리보기 맵이나 READY_FOR_ZONES 이상에 도달한다. 관리자가 보조 촬영을 올린다. 서버는 "이 세팅 세션의 맵을 이 파일로 다시 구워라"를 요청한다.

- **새 job_type 을 만들지 않은 이유**: 서버는 이미 site_setup 결과를 세션 귀속(`setup_session_artifact`)과 Scene 등록에 잇고 있다. 새 작업 종류를 만들면 job_type enum 네 곳과 대여 job_types, 워커 등록을 모두 늘려야 한다. jsonschema2pojo 가 oneOf 객체 분기를 잘 다루지 못하므로 같은 입력 객체에 선택 필드만 더했다.
- **입력**:
  - 그 세션의 처음 입력을 그대로 싣는다(heights, target, tolerance).
  - 여기에 `mode`, `supplement_media`, `rebake_base` 를 더한다.
  - `rebake_base` 는 이 모드에서만 허용되고, 이 모드에서는 필수다.
- **`rebake_base` 필드** (모두 EC2 가 이미 세션 귀속으로 보관한 것):
  - `calibration_version`
  - `calibration_document`: object_ref. 세팅 결과 `artifacts.calibration_document`, -467.
  - `preview_scene_version`
  - `scene_version`: 지금 세션의 맵
  - `bake_images`: -593 BAKE_IMAGE 행. 카메라마다 하나.
- **워커가 하는 일** (`ProductionSiteSetupStages.rebake`):
  1. 이 객체들을 서명 GET 으로 받아 sha256 을 맞춘다.
  2. 작업 폴더(calibration.json, images/<카메라>.image, job.json)를 만든다.
  3. prepare → mapanything(고정 카메라) → supplement 를 돌리고, 적용됐을 때만 bake 와 GLB 업로드를 한다.
  4. 배경 수집, 내부 파라미터, Stage A, 보정 등록, 미리보기, 구역 초안은 하지 않는다.
  5. prepare 와 MapAnything 을 다시 도는 것은 관문의 기준(보조 촬영 없을 때 정렬·재투영)과 같은 입력 폴더를 만들기 위해서다. 기준 맵은 다시 굽지 않는다.
- **결과**:
  - `mode` 와 `supplement` 는 필수다.
  - `calibration_version` 과 `preview_scene_version` 은 기준 값 그대로다.
  - `height_checks` 는 기준 보정 문서의 카메라 중심 Z 로 다시 잰다.
  - `artifacts` 에 새 보정, 미리보기, 프레임, 굽기 사진은 실을 수 없다(스키마로 막음).
  - APPLIED: 새 `scene_version` 과 `artifacts.scene`. 서버는 세션 귀속 CANDIDATE 로 등록하고, 세션의 기존 활성화 절차를 따른다.
  - FALLBACK·SKIPPED: `scene_version` 은 기준 그대로이고 `artifacts.scene` 은 실을 수 없다(스키마로 막음). 새 Scene 을 등록하지도 활성화하지도 않는다. 작업 status 는 SUCCEEDED 다.
- **하트비트**: BAKING_TEXTURE(90) 다음 COMPLETED.

## 3. 처리 흐름 (`ai/src/simai_ai/scene/supplement_capture.py`)

1. **받기(워커)**: 서명 GET 으로 받아 `supplement/media/<media_id>.video|.image` 에 두고 `supplement/request.json` 을 쓴다.
2. **고르기 `select_frames`**:
   - 동영상은 2 fps 로 디코드하며 선명도(라플라시안 분산, 긴 변 640 px)와 dHash 를 매기고, 원본 프레임은 들고 있지 않는다.
   - 같은 파일 선명도 중앙값의 0.35 배 미만을 버린다. 사진은 3장 이상일 때만 사진끼리 비교한다.
   - 총 상한(기본 40)을 파일별 후보 수에 비례해 나누되, 파일마다 적어도 1장은 준다.
   - 동영상은 선명한 순서로 최소 간격 max(0.5 s, 0.6·길이/배정 수)를 지키며 고른다.
   - 전체에서 dHash 해밍 거리 5 이하인 프레임은 거의 같은 것으로 보고 뺀다.
   - 고른 프레임 번호만 다시 순차 디코드한다.
3. **입력 폴더 `build_joint_prepared`**:
   - 고정 카메라 view(알려진 K, 사람 마스크)가 앞에 온다. MapAnything 의 기준 view(첫 view)는 고정 카메라여야 하기 때문이다.
   - 보조 프레임은 `K_undistorted: null`, `view_role: SETUP_SUPPLEMENT`, `source_media`(media_id, 프레임 번호, 시각, 촬영 시각)를 갖는다.
   - 보조 프레임은 고정 카메라 화면비로 가운데를 자르고, 고정 카메라보다 크면 줄인다.
   - 사람 마스크는 prepare 와 같은 분할기를 쓰고 31 px 만큼 팽창한다.
4. **추론**: `run_mapanything` prepared 모드가 K 없는 view 에는 `intrinsics` 를 넣지 않는다. 출력 K 검사(-312)는 K 를 넘긴 view 만 본다.
5. **묶음별 정렬·등록·관문**:
   - 고정 카메라만으로 floor 제약 정렬(M_k)과 중심만 맞춘 정렬을 둘 다 푼다.
   - 고정 카메라끼리 왕복 재투영(`cross_view_reprojection`), 등록(`register_views`), 화각 검사를 한다.
6. **병합 `materialize_world_run`**:
   - 받아들인 묶음의 view 를 각자의 M_k 로 World 에 옮긴다. 깊이는 s_k 배, 회전은 Q_k·R, 중심은 M_k(C)다.
   - 이것을 World 좌표의 MapAnything Run 하나로 쓴다. `world_alignment.json` 은 항등이다.
   - 보조 view 의 K 는 모델 K 를 영상 해상도로 되돌려 채운다(`k_source: MODEL_ESTIMATE`).
   - `metrics.json` 의 `prepared_sha256` 은 병합 입력 폴더의 해시다. 원래 추론 입력 해시는 `supplement_merge.inference_prepared_sha256` 에 남긴다. 파생 Run 임을 숨기지 않는다.
   - 원래 폴더는 `prepared_base/`, `mapanything_base/`, `world_alignment_base.json` 으로 옮긴다(`restore_baseline` 으로 되돌림).
7. **굽기**:
   - 기존 bake 를 그대로 쓴다. 운영 기본은 폴백 1 MA_ALIGNED_WM2 + TSDF + texrecon 이고, 폴백 2 레거시 texrecon, 정점색 폴백도 같다.
   - 보조 view 의 깊이와 사진이 형상과 텍스처에 들어간다.
   - 보조 촬영을 넣은 굽기가 실패하면 보조 촬영 없이 다시 굽는다(FALLBACK/BAKE_FAILED). 다시 굽기 모드에서는 원래 맵을 유지한다.
   - bake.json 에 `supplement_capture` 출처를 기록한다.

### 3.1 같은 동영상 프레임의 K 공유

MapAnything 입력에는 "이 view 들은 같은 모르는 K" 를 넘길 방법이 없다(view 마다 intrinsics 가 있거나 없거나). 그래서 프레임마다 따로 추정된 K 를 쓴다. 추론 뒤 K 를 파일별 중앙값으로 바꾸면 그 K 로 낸 깊이·자세와 어긋나므로 바꾸지 않는다. 후속으로는 묶음 detail 에 파일별 fx 흩어짐을 기록해 확인하는 방법을 둔다.

### 3.2 묶음(chunk) 공동 추론

- 기본은 묶음 하나다(`max_frames` 40 = `chunk_frames` 40).
- 상한을 올리면(예: `SIMAI_SUPPLEMENT_MAX_FRAMES=120`) 보조 프레임을 시간 순서로 40장씩 나눈다.
- 묶음마다 MapAnything 을 한 번 돌리고, 같은 고정 4 view(알려진 K)를 늘 넣는다.
- 다음 묶음에는 앞 묶음에서 등록된 마지막 프레임 4장(`bridge_frames`)을 다시 넣는다. 고정 카메라와 직접 겹치지 않는 묶음도 등록이 이어지게 하려는 것이다. 병합할 때 중복은 처음 것만 남긴다.
- 묶음마다 고정 카메라만으로 정렬하고, 관문에 걸린 묶음만 뺀다(`detail.chunks[].reason`). 남은 묶음을 병합한다.
- 시간 예산(`time_budget_s` 600)을 넘으면 묶음을 더 돌리지 않는다. 첫 묶음은 돈다.
- 묶음마다 MapAnything 가중치를 다시 읽는다(묶음당 수 초~수십 초). 3묶음 이상을 자주 쓰면 모델을 한 프로세스에 두는 후속이 필요하다.

### 3.3 등록 판정

MapAnything 은 모든 view 에 자세를 낸다. 그래서 "등록"은 그 자세가 고정 카메라 기하와 이어지는지로 판단한다.

- 고정 view 에서 시작해, 이미 등록된 view 에 투영했을 때 깊이가 맞는 점이 150점 이상(2000점 표본 중)이고 맞음 / (맞음 + 빈 공간 위반) ≥ 0.5 인 보조 view 를 차례로 더한다.
- 상대 view 가 본 표면보다 뒤에 놓인 점은 가림일 수 있어 세지 않는다. 낮은 손 기기와 높은 고정 카메라는 서로 가리는 곳이 많다.
- 이 때문에 "고정 카메라도 보는 곳에서 촬영을 시작하라"는 안내가 필요하다. 사각지대만 찍은 클립은 이어지지 않아 NOT_REGISTERED 가 된다.

## 4. 정한 수치와 근거

| 수치 | 값 | 근거 |
|---|---|---|
| 총 프레임 상한 | 40 (env `SIMAI_SUPPLEMENT_MAX_FRAMES` 1~240, 실행기 `--max-frames`) | 고정 4 + 40 = 44 view 로 518 해상도 MapAnything 한 번이 H100 기준 수십 초 안이다. 30 초 클립을 2 fps 로 받은 후보 60장의 2/3 다. 40장이면 0.75 s 마다 1장이고, 천천히 걸으면(0.5 m/s) 약 40 cm 간격이라 이웃 프레임이 충분히 겹친다 |
| 묶음 크기 | 40 (env `SIMAI_SUPPLEMENT_CHUNK_FRAMES`) | 기본 상한과 같아 기본은 묶음 하나다 |
| 다리 프레임 | 4 | 다음 묶음 등록이 이어지기에 충분하고 묶음 크기의 10% 다 |
| 시간 예산 | 600 s (env `SIMAI_SUPPLEMENT_TIME_BUDGET_S`) | 세팅 실행 상한 안에서 굽기 시간을 남긴다. 워커는 남은 시간이 900 s 미만이면 시작하지 않는다(TIME_BUDGET) |
| 디코드 속도 | 2 fps | 0.5 s 최소 간격과 맞춘다. 30 초 클립이면 후보 60장이고 선명도 계산은 CPU 수 초다 |
| 최소 간격 | max(0.5 s, 0.6·길이/배정 수) | 클립 전체에 고르게 퍼뜨린다. 0.6 은 흐림·중복을 뺀 뒤에도 배정 수를 채울 여유다 |
| 흐림 기준 | 파일 중앙값 × 0.35 | 손떨림 프레임은 라플라시안 분산이 보통 1/3 아래로 떨어진다(합성 시험의 σ=6 흐림은 1/10 아래). 같은 파일 상대 기준이라 기기·해상도 차이를 타지 않는다 |
| 거의 같은 프레임 | dHash 64비트 해밍 ≤ 5 | 멈춰 선 구간과 연사 사진을 거른다. 0.5 s 넘게 걸어서 바뀐 장면은 보통 10 이상이다 |
| 등록 | 맞는 점 ≥ 150 / 2000, 맞음 비율 ≥ 0.5, 깊이 허용 5% | 5% 는 기존 `cross_view_reprojection` 의 depth_tol 이다. 합성 시험에서 바른 자세는 0.66~1.0, 틀린 자세는 0.22~0.29(가림을 세면)였다. 빈 공간 위반 기준이라 0.5 로 두었다 |
| 등록 비율 하한 | 30% (그리고 1장 이상) | 대부분 등록되지 않으면 자세가 고정 카메라와 이어지지 않은 것이다. 사진 1~2장도 받도록 절대 하한은 1장으로 둔다 |
| 정렬 악화 | 중심만 맞춘 similarity 의 최대 중심 잔차가 기준 + 2 cm 초과, 또는 floor 제약 정렬 자체 실패(>10 cm·>5°) | -535 기준 중심만 맞춘 잔차는 5~6 cm, 정렬 허용 상한은 10 cm 다. 남은 4 cm 의 절반을 악화 허용으로 둔다. floor 제약 정렬 잔차는 예산(9.5 cm)에 붙는 경향이 있어 비교 잣대로 쓰지 않는다 |
| 고정 재투영 악화 | 기준 × 1.25 + 0.5 px 초과 (모델 px, 카메라별 중앙값의 중앙값) | -535 ma_single 기준 카메라별 중앙값은 2.6~5.6 px(전체 약 4.1)다. 표본 시드가 고정이라 같은 Run 의 흔들림은 0.1 px 아래다. 25% 넘게 나빠지면 보조 프레임이 고정 카메라 기하를 끌어당긴 것으로 본다 |
| 초광각 거부 | EXIF 35 mm 환산 < 16 mm(가로 약 97°) 또는 MapAnything 추정 가로 화각 중앙값 > 100° | 휴대폰 1x 주 렌즈는 23~28 mm(약 65~75°), 초광각은 13~16 mm(100~120°), 액션캠 광각·어안은 그 이상이다 |

### 4.1 어안·초광각: 거부 (경고 받기 아님)

하류(TSDF, texrecon, 뷰어)가 모두 핀홀 K 를 가정하고, MapAnything 출력 K 도 핀홀이다. 강한 왜곡 프레임을 넣으면 가장자리 형상이 휘어 맵에 남는다. 관문의 고정 카메라 지표는 보조 프레임 쪽 국소 휘어짐을 잡지 못한다. 그래서 경고만 남기고 받으면 틀린 형상이 조용히 들어간다.

- 사진은 EXIF 로 미리 거른다.
- 동영상은 EXIF 가 없어(cv2 는 컨테이너 메타데이터를 읽지 않음) 추론 뒤 화각 추정으로 파일을 통째로 뺀다(WIDE_FOV_REJECTED).
- 다시 찍으라는 안내가 맞는 대응이다.

## 5. 촬영 안내 (관리자용, -607 화면 문구 근거)

- 고정 카메라도 보는 곳에서 촬영을 시작해 사각지대로 천천히 옮겨 간다(약 30 초, 걸음은 천천히). 사진이면 고정 카메라가 보는 곳이 함께 담긴 사진을 몇 장 섞는다.
- 주 렌즈(1x)를 쓴다. 초광각(0.5x)·어안·액션캠 광각 모드는 쓰지 않는다(거부된다).
- 가로로 든다. 세로 영상은 고정 카메라 화면비(16:9)로 가운데를 잘라 위아래가 크게 잘린다.
- 아무 무늬 없는 흰 벽만 찍지 않는다(깊이·겹침 판정이 약하다). 바닥 가장자리나 가구 일부가 함께 들어오게 찍는다.
- 사람이 지나가도 마스크로 지우지만, 되도록 비어 있을 때 찍는다.
- 촬영 뒤 물건을 옮기면 맵이 그 순간을 남긴다. 세팅 직전에 찍는다.

## 6. 버린 대안

- **COLMAP·3DGS·NeRF 등 별도 재구성기**: 사용자·Lead 결정으로 하지 않는다(World 정렬, 메시 하류, GPU 시간, 일정).
- **보조 촬영을 세팅 초기 MapAnything(보정 단계)에 같이 넣기**: 고정 카메라 자세 추정과 바닥 맞춤이 보조 프레임에 흔들려 World·보정이 보조 촬영에 따라 바뀐다(규칙 1 위반). 보조 촬영 실패가 세팅 실패가 된다.
- **보조 프레임 K 를 파일별 하나로 묶어 다시 넣기(2차 추론)**: GPU 를 두 번 쓰고, MapAnything 에 K 공유 입력이 없어 중앙값 K 를 known-K 로 넣는 근사다. 효과를 재지 않은 채 시간만 늘어 후속으로 둔다.
- **텍셀 단위 출처 값 새로 만들기(OBSERVED_SETUP_SUPPLEMENT)**: texrecon 의 면별 view 선택을 출처 지도로 옮기는 작업이 커서 이번에는 맵 단위 출처(bake.json `supplement_capture`, 계약 `input_role`)로 둔다.
- **세로 영상 90° 회전**: K 를 모르니 기하로는 맞지만 MapAnything 학습 분포(중력 위쪽)에서 벗어날 수 있어 가운데 자르기 + 가로 촬영 안내로 둔다.
- **새 job_type `site_setup_rebake`**: 2.3 참고.

## 7. 잔여 위험

- **오래된 사진·영상**:
  - 촬영 뒤 배치가 바뀌면 맵에 지난 모습이 남는다.
  - `captured_at_ms` 를 결과와 bake.json 에 남겨 확인할 수 있게 했지만 자동으로 거르지는 않는다(현장 세팅 시각과 비교하는 기준은 서버·운영 몫).
  - 다시 굽기는 같은 세션 안에서만 쓰게 한다(-607).
- **등록 실패**:
  - 고정 카메라와 겹치지 않는 클립, 무늬 없는 벽, 초광각은 NOT_REGISTERED·TOO_FEW_REGISTERED·WIDE_FOV_REJECTED 로 떨어진다.
  - 맵은 지금과 같다. 안내 문구(5장)와 사유 코드로 다시 찍게 한다.
- **축척·World 불일치**:
  - 보조 프레임은 고정 카메라 정렬의 축척 s_k 를 따른다. MapAnything 이 묶음 안에서 보조 쪽 축척을 다르게 잡으면 보조 표면이 고정 표면과 어긋날 수 있다(두 겹 벽).
  - 고정 카메라 지표는 이것을 직접 보지 않는다. 등록 판정(깊이 5% 일치)이 고정 카메라와 겹치는 곳에서만 부분적으로 막는다.
  - GPU 세션 비교에서 두 겹 여부를 눈으로 확인한다(8장 확인 항목).
- **방식 2(보정 자세 조건 WorldMirror)**: 보조 프레임에 보정 자세가 없어 지원하지 않는다(PATH_UNSUPPORTED). 운영 기본(폴백 1)과 레거시 경로만 보조 촬영을 받는다.
- **MapAnything 혼합 입력**: K 있는 view 와 K 없는 view 를 한 번에 넣는 것은 공식 API 가 view 별 선택 입력을 받는다는 것에 기댄다. 로컬에는 MapAnything 이 없어 GPU 세션에서 처음 확인한다(8장 첫 확인).
- **HEIC**: cv2 가 읽지 못해 UNREADABLE 이다. 업로드 쪽(-607)에서 JPEG 로 받거나 바꾸는 것을 권한다. pillow-heif 는 쓰지 않았다.
- **세팅 실행 시간**: 받기(동영상 수십 MB) + 분할기 + MapAnything 한 번이 굽기 앞에 붙는다. 서버 실행 상한에 가까우면 워커가 TIME_BUDGET 으로 건너뛴다.
- **얼굴**: 보조 사진에 찍힌 사람은 마스크로 지우고, 남은 배경 픽셀만 텍스처에 쓴다(규칙 8 의 외형 텍스처 기본 OBSERVED). 생성형 복원은 없다.

## 8. GPU 세션 실행 순서

GPU 는 이 확인을 큐로 몰아 돌리고 바로 끈다. LIVE 관제 중에는 돌리지 않는다(3.2).

0. **노트북 준비**: 휴대폰으로 사각지대를 찍는다(5장 안내, 동영상 30 초 1개와 사진 몇 장). 파일을 노트북 `C:/Users/SSAFY/simai-local/s14/phone/` 에 둔다.
1. **Pod 에 코드와 파일 올리기**(EC2 점프):
   ```sh
   rsync -av --exclude .git ai/ pod:/workspace/simai-605/ai/
   rsync -av C:/Users/SSAFY/simai-local/s14/phone/ pod:/workspace/s14/phone/
   ```
2. **기준 입력 만들기**: Pod 의 Tier 0 사람 지운 배경 묶음과 활성 보정 문서를 쓴다(세팅을 한 번 돌린 뒤). 보정 문서 파일 이름은 버전의
   sha256(`calibration_document_path`)이다.
   ```sh
   cd /workspace/simai-605/ai
   export PYTHONPATH=/workspace/simai-605/ai/src:/workspace/simai-605/ai   # 이미지 안 Python(통합 환경) 그대로, -539 큐와 같은 방식
   V=$(python3 -c "import json,os;print(json.load(open(os.environ['SIMAI_STATE_DIR']+'/calibrations/active.json'))['active'])")
   CAL="$SIMAI_STATE_DIR/calibrations/$(printf %s "$V" | sha256sum | cut -d' ' -f1).json"
   B=$(ls -d "$SIMAI_STATE_DIR/clean-background/$V"/bundle-* | tail -1)
   ```
3. **비교 실행** (워커와 같은 단계 프로세스·시간 상한, MapAnything·분할기 가중치는 워커와 같은 환경 변수 `SIMAI_MAPANYTHING_MODEL`·`SIMAI_SEG_WEIGHTS`):
   ```sh
   python -m simai_ai.scene.supplement_runner --bundle "$B" --calibration "$CAL"        --media /workspace/s14/phone/ --source PHONE_UPLOAD --gpu-provider RUNPOD --out /workspace/s14/supplement-cmp-40
   # 묶음 시험(선택): 상한 120 = 40장씩 3묶음
   python -m simai_ai.scene.supplement_runner --bundle "$B" --calibration "$CAL"        --media /workspace/s14/phone/ --max-frames 120 --gpu-provider RUNPOD --out /workspace/s14/supplement-cmp-120
   ```
4. **확인 순서**
   1. 혼합 입력 추론: `supplement-cmp-40/supplement/supplement.log` 와 `supplement/supplement/chunk_00/mapanything/metrics.json` 의
      `result` 가 SUCCESS 인지(실패면 `supplement/supplement/result.json` 의 묶음 사유 RECONSTRUCTION_FAILED). `intrinsics_check` 는 고정 4대만.
   2. `comparison.json`: `supplement.summary`(status·등록 수·alignment), `delta`(xy_hole_area_m2·floor_hole_area_m2 가 줄었는지),
      `supplement.chunks`(묶음별 사유·잔차), `seconds`.
   3. 두 GLB(`baseline/textured/…glb`, `supplement/textured/…glb`)를 뷰어로 열어 사각지대가 실제 관측으로 메워졌는지, 두 겹 벽·떠 있는
      조각이 없는지 본다.
   4. 결과 묶음을 노트북으로 가져온다: `rsync -av pod:/workspace/s14/supplement-cmp-40/ C:/Users/SSAFY/simai-local/s14/supplement-cmp-40/`
      (GLB·영상·작업 폴더는 저장소에 넣지 않는다. run_record.json 과 comparison.json 만 보고에 쓴다).
5. **세팅 경로 확인**(시간이 남으면): 서버(-607)가 준비됐으면 세팅에서 `supplement_media` 를 실어 끝까지 돌리고 결과 `supplement` 를 본다. 아니면 4단계까지만 한다.
6. Pod 를 바로 끈다.

## 9. 명세 후속 (명세 정리 MR 에서)

- 5.3: 현장 세팅에 "보조 촬영(동영상·사진)으로 사각지대 관측 보강"을 넣는다. World 는 고정 카메라만으로 정한다는 것, 관문 수치, 다시 굽기 모드를 함께 적는다.
- 12.5: I5 site_setup 1.8.0(`supplement_media`, `supplement`, `mode`, `rebake_base`)을 적는다.
- 13장(뷰어): 맵 출처에 `SETUP_SUPPLEMENT_CAPTURE` 와 촬영 시각을 보여 줄지(-606·-607) 정한다.
- 17장(위험): "오래된 보조 촬영"과 "보조 표면 축척 어긋남"을 넣는다.

## 10. 코드·시험 위치

- 코드:
  - `ai/src/simai_ai/scene/supplement_capture.py`: 고르기, 입력 폴더, 등록, 관문, 병합, 출처
  - `ai/src/simai_ai/scene/supplement_runner.py`: 오프라인 비교
  - `ai/src/simai_ai/scene/reconstruct_tasks.py`: `solve_fixed_alignment`, `supplement` 단계, `run_mapanything_prepared`
  - `ai/src/simai_ai/scene/run_mapanything.py`: K 없는 view
  - `ai/src/simai_ai/calibration/extrinsics/initial.py`: 보조 view 단위 검사 제외
  - `ai/services/worker/site_setup.py`·`site_setup_stages.py`: 굽기 전 보조 촬영, 다시 굽기
- 시험:
  - `ai/tests/scene/test_supplement_capture.py`: 합성 동영상 고르기, 섞인 입력 폴더, 관문 표, 가짜 MapAnything 전체·묶음·시간 예산, 실행기
  - `ai/tests/services/worker/test_site_setup_supplement.py`: 받기, 대체, 다시 굽기
  - `test_site_setup.py`: 결과 요약
- 계약: `contracts/messages/site_setup.schema.json`, 예시 `job_request.site_setup_supplement(_rebake)`·`job_callback.site_setup_supplement(_fallback|_rebake|_rebake_fallback)`, 반례 `invalid/job_*.site_setup_{supplement,rebake}_*`.
