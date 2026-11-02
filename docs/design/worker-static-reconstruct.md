# 분석 워커 static_reconstruct (S15P31A101-364)

관련 명세: §3.2(Tier 0 우선·LIVE 중 배치 금지), §3.12(실행 환경·저장소), §5.3(정적 3D), §12.5(I5). 계약은 기존 `job_request` 3.3.0·`job_callback` 2.2.0·`scene_output` 2.1.0을 변경하지 않는다. 지시서에 적힌 §5.5는 현재 PPE 절이며 정적 3D 정본은 §5.3이다.

코드: `ai/services/worker/static_reconstruct.py`, `ai/src/simai_ai/scene/reconstruct*.py`. 기존 실험 6개 파일의 구현을 `scene/`으로 옮겼으며 실험 경로는 같은 구현의 호환 진입점이다. 모델 전용 가상환경에서도 이제 `simai-ai` 패키지를 `--no-deps`로 설치해야 한다. 무거운 모델은 실제 단계에서만 import한다(MapAnything 실험의 기존 torch import는 유지).

## 입력과 불변 조건

I5 `input`은 자유 객체이므로 아래 프로파일은 티켓의 구현 입력이다. 새 JSON Schema나 I1 계약을 만들지 않는다.

| 필드 | 의미 |
|---|---|
| `calibration_version`, `calibration_ref`, `calibration_hash` | 보정 버전·s3 객체 주소·문서 원본 바이트의 `sha256:<hex64>` |
| `images[]` | 서로 다른 카메라 2~16대. `camera_id`, `uri`(s3), `kind` |
| `images[].kind` | `CLEAN_BACKGROUND`: 이미 사람 지운 원본 렌즈 영상. `FRAME`: 사람 가림이 필요한 원본 프레임 |
| `input_provenance` | 기존 common 계약. frames.camera_ids가 입력과 같고 calibration 출처·버전이 문서와 같아야 함 |
| `options.align_views` | `off`(기본) / `scale` / `sim3` (-314) |
| `options.plane_cleanup` | bool, 기본 false. 수평 평면 정리(-314), ESTIMATED_PLANE로 표시 |

보정은 PROVISIONAL·CALIBRATED만 받는다. SIM·DATASET_GT 입력/보정은 `is_upper_bound: true`를 요구한다. 모든 결과는 `CANDIDATE`, `VISUAL`이고 운영 Scene 자동 확정·Tier 0 위치·위험 입력 변경은 하지 않는다. 스툴 치수는 -357 목록의 0.415m/0.425m를 재사용하며 축척 점검만 한다.

사람 제거 배경은 -358의 원본 해상도 이미지이며 왜곡을 아직 펴지 않은 입력이어야 한다. 미관측 영역을 임의로 채운 이미지를 CLEAN_BACKGROUND라고 선언하면 안 된다. 이 프로파일은 배경 생성 방법 자체를 검증하지 않으므로 호출자가 원본·촬영 구간과 완성 여부를 확인한다.

## 실행 단계와 정한 값

| 단계 | 진행률 끝 | 상한(초) | 내용·근거 |
|---|---:|---:|---|
| download | .15 | 120 | ObjectStore로 보정·이미지 받기, 보정 원본 해시·계약·버전 확인 |
| prepare | .25 | 180 | 기존 undistort 재사용, NONE/PINHOLE만 지원. FRAME만 seg, 기존 15px 팽창 |
| mapanything | .55 | 1200 | 기존 prepared known-K 모드·기본 518 해상도·추론 1회. 기존 세션 큐의 1200초 상한 재사용 |
| bake | .80 | 1800 | TSDF→texrecon area 1회→기존 구멍 규칙→출처 지도. LOO 실험 비교는 생략. CPU 굽기 여유 30분 |
| fallback | .85 | 120 | 필요할 때만. 모델 깊이 격자 메시+관측 정점색, 사람 마스크·불연속 면 제외 |
| measure | .90 | 180 | World로 옮긴 모델 K·자세로 기존 스툴 검출·삼각측량·독립 표본 집계 |
| upload | 1.0 | 180 | glb·출처 지도·metrics·run_record·단계 로그 업로드, scene_output 검사 후 반환 |

전체 실행 상한은 EC2 대여 timeout_ms가 별도로 관리한다. 단계 상한 합계는 3780초이므로 세션 시험은 약 4000초 이상의 작업 상한을 설정한다. I/O 상한은 기존 ObjectStore의 소켓 제한과 단계 사이 경과 검사로 적용하며, 개별 다운로드 중간의 강제 종료는 기존 러너의 프로세스 종료 정책에 따른다.

각 계산 단계는 별도 프로세스다. parent가 취소되거나 단계 상한을 넘으면 Linux 프로세스 그룹 전체(그 안의 texrecon 포함)를 종료한다. Windows 시험은 taskkill /T를 사용한다. prepare·MapAnything 자식은 부모의 torch 상한을 다시 설정한다. 모델/허브의 자동 네트워크 다운로드는 끄며 가중치는 bootstrap이 준비한다.

MapAnything의 모델 좌표를 World라고 이름만 바꾸지 않는다. 입력 보정의 카메라 회전·중심과 모델 카메라 사이 similarity를 구하고 중심 최대 잔차 0.10m·회전 최대 5도를 요구한다. 이는 Visual 층의 초기 정렬 실패를 막는 보수적 시작값이며 G1 합격 기준이 아니다. 동일 위치 카메라·음수/비유한 축척·잘못된 회전은 실패한다. 정렬은 메시 변환에만 적용하고 보정 문서를 쓰지 않는다. World→glTF `(x,z,-y)`는 기존 `texture_mesh.to_glb_points` 한 곳에서 수행한다.

`SIMAI_TEXRECON`(실행 파일 경로, 없으면 PATH의 texrecon)을 찾지 못하면 `TEXRECON_NOT_FOUND`로 정점색 폴백한다. 기존 TSDF 실행 의존성 `open3d`가 없으면 `TSDF_OPEN3D_NOT_FOUND`로 같은 폴백을 한다. `texture: null`, 출처 지도 없음, 이유는 metrics에 남긴다. 출처 지도가 없는 폴백에서 observed_surface_ratio=1은 **내보낸 관측 깊이 면만** 분모이며 방 전체 커버리지가 아니다. 미관측 공간의 형상·텍스처를 생성하지 않는다.

실행 파일이 존재하지만 texrecon 자체가 실패하면 폴백으로 숨기지 않고 실패한다. FRAME에서 seg가 없으면 `STATIC_SEGMENTER_MISSING`; CLEAN_BACKGROUND는 seg를 열지 않는다. seg는 `SIMAI_SEG_WEIGHTS`의 기존 로컬 파일만 사용한다. 이 티켓은 seg 다운로드 목록을 수정하지 않는다(-362 담당).

## 산출물·실패 확인

- 콜백 result는 scene_output 2.1.0 자체다. `texture`나 `metrics_uri`를 임의의 루트 필드로 추가하지 않는다. metrics·run_record·로그 객체 주소는 `quality.notes`에 쓴다.
- assets: GLTF_VISUAL, 굽기에 성공한 경우 아틀라스 순서의 SURFACE_SOURCE_MAP. 지도의 0/1/2/3/4 표와 면적 비율을 기존 계약에 싣는다. 추정 평면을 OBSERVED 비율에 합치지 않는다.
- `metrics.json`: `texture`, `texture_fallback_reason`, `scale_check`, `stage_seconds`, `image_hashes`, `input_provenance`, MapAnything·굽기 원본 지표. scale이 null이면 이유·표본 수를 확인한다.
- `run_record.json`: 기존 run_record 계약, texture_available·observed_surface_ratio·scale_check·각 단계 `_s`. 입력 출처·git·가중치/이미지 환경 정보는 아는 값만 사용한다. 실행 이미지 digest를 모르면 unknown으로 둔다. SIM/GT 지표는 제품 성능으로 쓰지 않는다.
- 실패 코드: STATIC_INPUT_INVALID, CALIBRATION_HASH_MISMATCH, CALIBRATION_INVALID, STATIC_SEGMENTER_MISSING, STATIC_MODEL_MISSING, STATIC_WORLD_ALIGNMENT_FAILED, STATIC_{단계}_EMPTY/TOOL_FAILED/TIMEOUT, STATIC_OUTPUT_INVALID, STATIC_OBJECT_FAILED. 저장소 오류만 기존 retryable 정책을 전달한다.
- LIVE 진입 시 기존 AdmissionGate가 static_reconstruct를 대여 대상에서 제외한다. 이미 받은 대여와 실행 중 작업은 기존 러너가 LIVE_EXP_BLOCKED로 시도 횟수를 쓰지 않고 반납한다. 핸들러는 취소 시 업로드를 시작하지 않는다.

## 9차 세션 절차

1. 원본 자료는 노트북 `C:/Users/SSAFY/simai-local/clean-bg-1029/cam_A.jpg` ~ `cam_D.jpg`라고 가정한다. 이 문서 작성/시험 중 실제 사진을 읽거나 저장소에 넣지 않았다. 운영자가 사진과 보정 문서를 객체 저장소에 올리고 보정 **원본 바이트** sha256을 계산한다. 사람 제거 배경 자동 제출은 -363 후속 연결이다.
2. Pod는 bootstrap으로 static 가중치를 미리 받는다. 통합 워커와 같은 저장 위치에 `SIMAI_MAPANYTHING_MODEL`을 지정하거나 기본 `$SIMAI_ROOT/weights/map-anything`을 사용한다. FRAME 시험만 SIMAI_SEG_WEIGHTS를 지정한다. 모델 실행 중 네트워크는 사용하지 않는다.
3. 아래 예의 보정 버전/해시·객체 주소·촬영 출처를 실제 값으로 바꿔 `static-input.json`으로 저장한다. 해시의 0은 예시이며 그대로 실행하면 해시 검증에 실패해야 한다.

```json
{
  "calibration_version": "cal-session9",
  "calibration_ref": "s3://simai-site/session9/calibration.json",
  "calibration_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "images": [
    {"camera_id": "cam_A", "uri": "s3://simai-site/clean-bg-1029/cam_A.jpg", "kind": "CLEAN_BACKGROUND"},
    {"camera_id": "cam_B", "uri": "s3://simai-site/clean-bg-1029/cam_B.jpg", "kind": "CLEAN_BACKGROUND"},
    {"camera_id": "cam_C", "uri": "s3://simai-site/clean-bg-1029/cam_C.jpg", "kind": "CLEAN_BACKGROUND"},
    {"camera_id": "cam_D", "uri": "s3://simai-site/clean-bg-1029/cam_D.jpg", "kind": "CLEAN_BACKGROUND"}
  ],
  "input_provenance": {
    "frames": {"source": "FILE", "dataset_id": "clean-bg-1029", "camera_ids": ["cam_A", "cam_B", "cam_C", "cam_D"]},
    "calibration": {"source": "G1_AUTO", "calibration_version": "cal-session9"},
    "is_upper_bound": false
  },
  "options": {"align_views": "off", "plane_cleanup": false}
}
```

4. EC2 쪽 -365의 관리자 CLI가 제공하는 형식으로 넣는다. 이 티켓은 CLI를 추가하거나 실제 요청을 보내지 않는다.

```sh
admin job enqueue --type static_reconstruct --input-file static-input.json
```

5. LIVE가 끝났고 Tier 0 표시 파일이 신선한지 확인한 뒤 작업을 대여한다. 워커 heartbeat 진행률 0→.15→.25→.55→.80→.90→1, lease 유지와 최종 SUCCEEDED를 확인한다. LIVE 중에는 시작하지 않고, 도중 LIVE가 켜지면 반납되는지 확인한다.
6. 콜백에서 CANDIDATE·VISUAL·보정 버전·출처, GLTF_Y_UP·자산 해시를 확인한다. quality.notes의 metrics/run_record/prepare.log/mapanything.log/bake.log/measure.log 주소를 확인한다. 텍스처 경로는 TSDF 면 수·texrecon 로그·출처 지도 값·관측/추정 평면 비율을, 폴백 경로는 texture:null·이유·정점색 메시를 확인한다. 원본 보정 해시가 바뀌지 않아야 한다.
7. 다운로드한 결과를 회전 추가 없이 뷰어에서 확인한다. 스툴은 3개 이상 독립 다중 시점 표본이 없으면 scale:null이 정상이다. 운영 Scene 확정은 별도 절차다. 결과 묶음을 노트북으로 회수하고 GPU Pod를 Stop한다.

## 배포·버린 대안·남은 일

`deploy/tier0/Dockerfile` unified에 `deploy/texrecon/Dockerfile`과 같은 mvs-texturing/MVE/rayint 고정 커밋 빌드 단계를 추가했다. 최종 이미지와 같은 Ubuntu/ABI에서 빌드하고 실행 파일·라이선스만 복사한다. Lead 빌드 명령은 저장소 루트의 `docker buildx bake unified`. 이번 작업은 이미지 빌드·푸시를 실행하지 않는다.

실험 파일을 워커가 직접 import하거나 sys.path/importlib로 우회하는 대안은 의존 방향 위반이라 버렸다. 같은 알고리즘을 워커에 복사하는 대안은 실험과 운영의 동작 차이를 만들므로 버렸다. 외부 명령을 같은 처리기 스레드에서 무기한 실행하는 대안은 LIVE 선점 시 GPU를 반환하지 못하므로 버렸다. 스툴 축척으로 보정을 자동 덮어쓰는 대안은 Tier 0 정본·버전 규칙 위반이라 쓰지 않는다.

`open3d`는 기존 TSDF/렌더 구현의 의존성인데 ai/uv.lock에 없었다. `open3d>=0.19`로 잠갔을 때 들어간 `open3d 0.20.0`은 Linux 휠에서도 `ScalableTSDFVolume` 추출이 면 0개(빈 메시, `STATIC_BAKE_EMPTY`)를 내는 결함이 있어(`docs/design/exp01-textured-mesh.md`와 같은 원인), Lead 가 `scene`·`hmr` 묶음을 `open3d==0.19.0`(Linux x86_64)으로 고정하고 다시 잠갔다(S15P31A101-374). `worker` 묶음이 `scene` 을 포함하므로 통합 이미지를 다시 빌드하면 texrecon 과 함께 들어간다. 다시 빌드하기 전 이미지에서는 위 폴백(정점색)으로 돈다. 9차 실 GPU·texrecon 빌드/실행·S3/EC2 왕복·정렬 문턱 실측, -363 배경 자동 제출·-365 접수/Scene 등록이 남는다. 실패/취소 때 임시 작업 폴더는 정리된다. 실패 로그는 유효 대여가 남아 있을 때 failed-이름.log 객체로 보존하고 오류 메시지에 주소를 남긴다. 취소·대여 상실 뒤에는 업로드하지 않으므로 그때의 로그 보존은 후속 보완 대상이다.

## S15P31A101-372: 배경 묶음 업로드 및 작업 입력 생성 세션 도구(`simai_ai.scene.clean_bg_job`)

`-363` `BackgroundCleanBackground` 가 Pod 의 `$SIMAI_STATE_DIR/clean-background/` 에 모은 사람 지운 배경 묶음과 `-364` `static_reconstruct` 워커 입력 및 `-365` EC2 관리자 CLI 사이를 한 번에 잇는 세션 도구다(`python -m simai_ai.scene.clean_bg_job`).

### 1. 입력·검증 규칙

- **배경 이미지 입력(택1)**:
  - `--bundle <dir>`: Tier 0 의 `clean-background/` 루트(`status.json` 포함) 또는 `bundle-<uuid>/` 폴더(`COMPLETE` + `clean_backgrounds.json` 포함).
    - `status.json` 의 `complete == true`, `calibration_version`, `bundle` 경로와 묶음 폴더의 `COMPLETE`·`clean_backgrounds.json`(`ready == true`, `complete == true`, `camera_generations`)을 교차 확인한다. `status.json` 이 없는 상위 폴더에서 `bundle-*` 를 글로빙으로 고르지 않는다.
    - Pod 의 `/workspace/simai/state/clean-background` 폴더를 노트북으로 복사해 온 경우에도 `<bundle_root>/<calibration_version>/<Path(status.bundle).name>` 경로를 자동으로 찾는다.
    - 각 카메라 JPEG/PNG 파일의 `sha256` 해시와 실제 디코딩 해상도가 `clean_backgrounds.json` 및 보정 문서의 `image_size` 와 일치하는지 업로드 전에 먼저 확인한다.
  - `--images-dir <dir>` 또는 `--image <camera_id>=<path>`(2~16회 반복): 보정 카메라 ID 와 일치하는 배경 이미지를 직접 지정할 때 쓴다(`--frame-source LIVE|FILE|SIM|DATASET`, `--dataset-id`, `--dataset-hash` 지원).
- **보정 문서 입력**:
  - `--calibration <path>`: 활성 보정 문서(`calibration.json`). `calibration` 스키마 통과 여부와 `state in {"PROVISIONAL", "CALIBRATED"}` 를 확인하고 원문 바이트의 `sha256:<hex64>`(`calibration_hash`)를 계산한다.
  - `--calibration-ref s3://<bucket>/<key>` 또는 `--upload-calibration`(`AVATAR_ASSET` `application/octet-stream` 서명 업로드로 보정 문서를 함께 올림).
- **서명 업로드 및 실패 중단**:
  - `--dry-run` 이 아니면 `GET /api/calibrations/current` 로 EC2 의 현재·최근·직전 보정과 `calibration_version` 을 먼저 비교하고(서버에 버전별 조회가 없어 목록에 없으면 경고만 출력: 등록돼 있지만 현재가 아닌 보정도 있고, 정말 미등록이면 `SceneJobListener` 가 결과를 거절한다. 기계 토큰이라 401/403 이거나 `--skip-calibration-check` 면 확인 없이 경고), `POST /api/uploads/sign` + `PUT upload_url` 로 각 카메라 이미지를 올린다(사진은 `EVENT_FRAME`[Tier 0·DEVICE 토큰] 또는 `AVATAR_ASSET`[워커 토큰], 보정 문서 `application/octet-stream` 은 `AVATAR_ASSET`[워커 토큰]만 허용하므로 `--upload-calibration` 이면 워커 토큰·`AVATAR_ASSET` 하나로 보정 문서와 사진을 모두 올림. 토큰 종류와 용도가 맞지 않으면 업로드 전에 안내하고 중단, S15P31A101-374).
  - 로컬 검증이나 서명·업로드 중 하나라도 실패하면 즉시 멈추고 `--output` 파일을 만들지 않는다. 토큰과 서명 URL 쿼리는 표준 출력·오류·로그에 남기지 않는다.
- **작업 입력 계약 검증**:
  - 생성한 입력 사전을 `simai_ai.scene.reconstruct.validate_input` 과 I5 `job_request` 스키마(`schema_errors(..., "job_request")`)로 검증한 뒤 `--output` 에 저장한다.

### 2. 9차 세션 실행 절차 (묶음 확인 → 도구 → enqueue → 결과 확인 → activate)

1. **배경 묶음 완료 확인 (Pod 또는 회수한 폴더)**:
   ```sh
   jq '{calibration_version, ready, complete, bundle}' /workspace/simai/state/clean-background/status.json
   ```
2. **네트워크 없이 로컬 검증(`--dry-run`) 또는 실제 서명 업로드 후 작업 입력 JSON 생성**:
   ```sh
   # 로컬 검증만 수행할 때
   uv run --directory ai python -m simai_ai.scene.clean_bg_job \
     --bundle /workspace/simai/state/clean-background \
     --calibration /workspace/simai/state/calibration/active.json \
     --calibration-ref s3://<bucket>/calibrations/<cal_ver>/calibration.json \
     --output /tmp/static_reconstruct_input.json \
     --dry-run

   # 객체 저장소에 배경 이미지를 서명 업로드하고 작업 입력 파일을 만들 때
   SIMAI_EC2_BASE_URL=https://<ec2-host> \
   uv run --directory ai python -m simai_ai.scene.clean_bg_job \
     --bundle /workspace/simai/state/clean-background \
     --calibration /workspace/simai/state/calibration/active.json \
     --calibration-ref s3://<bucket>/calibrations/<cal_ver>/calibration.json \
     --output /tmp/static_reconstruct_input.json
   ```
3. **LIVE 감시를 멈춘 창에서 EC2 에 `static_reconstruct` 작업 등록(`enqueue`)**:
   - 주의: LIVE 감시 중(`tier0.live` 신선)에는 Worker 가 `static_reconstruct` 작업을 거절·반납(`LIVE_EXP_BLOCKED`)하므로 LIVE 감시를 멈춘 뒤 실행한다.
   ```sh
   docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml \
     run --rm -T server admin job enqueue \
     --type static_reconstruct \
     --input-file /tmp/static_reconstruct_input.json \
     --timeout-ms 4200000 --max-attempts 2
   ```
4. **작업 완료(`SUCCEEDED`) 뒤 자동 등록된 `CANDIDATE` 장면 버전 확인**:
   ```sh
   docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml \
     run --rm -T server admin scene list --site <site_id>
   ```
5. **시각 산출물(`GLTF_VISUAL`, `SURFACE_SOURCE_MAP`, `metrics.json`) 검토 뒤 장면 활성화(`CANDIDATE` → `COMMITTED`)**:
   ```sh
   docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml \
     run --rm -T server admin scene activate \
     --site <site_id> --version <scene_version> --actor <관리자> --reason "<사유>"
   ```

## S15P31A101-403: 배경 묶음 static_reconstruct 자동 제출

관련 명세: §3.2(Tier 0 우선·LIVE 중 배치 금지), §5.3(정적 3D), §12.5(I5), AGENTS 규칙 1·5·6. -363 이 모은 완성 묶음을 -372 도구로 사람이 올리고 `admin job enqueue` 하던 일을 분석 워커가 자동으로 한다. 결과 Scene 처리(-365 `SceneJobListener` 의 CANDIDATE 등록, 관리자 활성화)와 CANDIDATE 정책은 그대로다. 배경 사진은 Visual 층 입력일 뿐 Tier 0 위치·위험 판정에 들어가지 않는다.

- 코드: `ai/services/worker/auto_static.py`(조건·기록·제출), `ai/services/worker/__main__.py`(`start_auto_static`, `--once` 가 아닐 때 보조 스레드), 서버 `server/.../domain/scene/StaticReconstructSubmitService.java`·`WorkerSceneJobController.java`.
- 계약: `contracts/messages/static_reconstruct_submit`·`static_reconstruct_submit_result` 1.0.0 신설(묶음 6.11.0). 기존 I5 메시지는 그대로다.
- 시험: `ai/tests/services/worker/test_auto_static.py`, `server/src/test/.../domain/scene/WorkerStaticReconstructSubmitIntegrationTest.java`.

### 흐름

1. 분석 워커 보조 스레드 `auto-static-reconstruct` 가 `poll_s` 마다 조건을 본다(아래 표). Tier 0 는 이 일을 하지 않는다.
2. 조건을 모두 만족하면 -372 의 함수를 그대로 쓴다: `load_calibration` → `prepare_from_bundle(묶음 폴더)`(해시·해상도·`COMPLETE`·`camera_generations` 교차 확인) → 가짜 주소로 `build_job_input` 사전 검사 → `sign_and_upload_object` 로 보정 문서(`application/json`)와 카메라 사진을 정적 3D 입력 목적 `STATIC_RECONSTRUCT_INPUT`(워커 토큰만, 키 `static-inputs/<bundle_id>/<카메라 ID>.jpg`·`calibration.json`) 서명 업로드 → `build_job_input`.
3. `POST /api/worker/scene-jobs/static-reconstruct`(워커 토큰 체인 `/api/worker/**`, `SecurityConfig` 변경 없음)에 `{bundle_id, input}` 을 보낸다.
4. EC2 는 묶음 ID 로 advisory 잠금을 잡고, 같은 묶음 작업이 있으면 input 을 보지 않고 그 작업과 지금 상태를 200 으로 돌려준다. 없으면 이 현장의 PROVISIONAL·CALIBRATED 보정인지(Scene 등록 규칙과 같음, 아니면 409 `SCENE_CALIBRATION_INVALID`) 보고, 관리자 등록과 같은 `AnalysisJobService.enqueue`(job_request 계약 검사, BACKGROUND)로 등록해 201 을 돌려준다. 저장하는 input 에는 `clean_background_bundle_id` 를 싣는다(출처·멱등 조회 키).
5. 워커는 결과를 기록 파일에 추가한다. 작업이 끝날 때까지 같은 요청을 다시 보내 상태만 본다(같은 묶음은 같은 작업이 오므로 새 작업이 생기지 않는다). 끝나면 `FINISHED` 를 남긴다.
6. 대여·실행은 지금과 같다. LIVE 가 다시 켜지면 기존 AdmissionGate 가 static_reconstruct 를 반납한다(`LIVE_EXP_BLOCKED`·`LIVE_PREEMPTED`).

### 조건·수치와 근거

| 조건 | 판단 | 값·근거 |
|---|---|---|
| 1 완성 묶음·활성 보정 | `state_dir/clean-background/status.json` 이 `complete: true` 이고 그 `bundle`(이름 `bundle-<32자리 16진수>`) 폴더에 `COMPLETE` 가 있다. 묶음 보정 버전이 Tier 0 보정 버전 저장소(`state_dir/calibrations/active.json` 의 `active`, `simai_ai.calibration.runtime`)의 활성 버전이고, 상태(문서 값을 전환 이력의 마지막 값이 덮음, 저장소와 같은 규칙)가 PROVISIONAL·CALIBRATED 다 | -363 소비자 규칙(상태 포인터 + `COMPLETE`, 디렉터리 검색으로 고르지 않음). 문서 경로 규칙은 시험이 `RuntimeCalibrationStore.path` 와 맞춰 본다 |
| 2 LIVE 아님 | 대여와 같은 `AdmissionGate`(Tier 0 표시 파일 신선도)를 이 스레드 전용 인스턴스로 본다. `live` 면(모름 포함) 아무것도 올리지 않는다. 업로드 하나마다 다시 보고, LIVE 가 되면 바로 멈추며 실패로 세지 않는다 | 접수 문은 한 스레드 전제라 대여 루프와 상태를 섞지 않는다. 표시 파일이 없을 때 LIVE 아님으로 보는 것은 Tier 0 정상 종료 기록이 있을 때(아래 '관제 뒤 운영 흐름')와 개발 모드 `SIMAI_WORKER_DEV_NO_LIVE=1` 뿐이다. 기록 없음·끊김(15초 넘게 갱신 없음)·모름은 LIVE |
| 3 LIVE 뒤 대기 | 마지막으로 LIVE(또는 모름)를 본 뒤 `SIMAI_AUTO_STATIC_QUIET_S`(기본 300초). 프로세스 시작 시각을 마지막 LIVE 로 본다 | Lead 기본값. 접수 문의 `live_linger_s` 60초·`resume_after_s` 30초와 Tier 0 재기동 틈(장면 회수 최악 약 40초 + 엔진 준비)보다 길어, 잠깐 끊긴 관제 사이에 올리지 않는다 |
| 4 낸 적 없음 | 기록 파일에 그 묶음의 `SUBMITTED` 가 없다 | 재시작 뒤에도 같다. 응답을 잃어 기록 전에 꺼졌으면 다시 내도 EC2 가 같은 작업을 돌려준다(작업은 하나) |
| 5 한 번에 하나 | `FINISHED` 가 없는 `SUBMITTED` 가 있으면 새 묶음을 내지 않고 그 작업 상태만 본다 | QUEUED·LEASED 는 진행 중, SUCCEEDED·FAILED·TIMED_OUT·CANCELLED 는 끝 |
| 주기 | `SIMAI_AUTO_STATIC_POLL_S`(기본 30초) | 파일 stat·작은 JSON 읽기뿐이다. 표시 파일 최대 나이 15초의 두 배, 대기 300초 대비 10% 오차 |
| 실패 재시도 | 검사·업로드·등록 실패는 `SUBMIT_FAILED` 를 남기고 60초에서 2배씩, 상한 3600초 백오프. 백오프는 기록 파일의 이어진 실패 수·마지막 실패 시각(UTC ms)으로 계산해 재시작 뒤에도 잇는다 | EC2 재시작·망 단절은 수 분 안에 풀리고, 계속 실패하는 묶음도 시간당 한 번(사진 4장 약 4MB)이면 부담이 없다 |
| 끝난 작업 | FAILED·TIMED_OUT·CANCELLED 는 `FINISHED`(작업 실패 코드 포함)만 남기고 자동으로 다시 내지 않는다 | 같은 입력으로 다시 돌려도 같은 실패일 가능성이 크고 GPU 시간이 크다. 운영자는 -372 도구로 손으로 낸다(새 작업). EC2 쪽 시도 2회는 그대로 쓴다 |
| 작업 상한 | `timeout_ms` 4,200,000·`max_attempts` 2(서버 고정) | -372 안내 명령과 같다. 단계 상한 합 3780초 + 여유 |
| 끄기 | `SIMAI_AUTO_STATIC_RECONSTRUCT=0`(기본 켬). `--once` 실행(`simai-bootstrap scene`)에서는 띄우지 않는다 | 한 번만 처리하고 끝나는 프로세스는 대기 시간을 채울 수 없다 |

기록 파일은 `state_dir/worker/auto_static_reconstruct.jsonl`(`SIMAI_STATE_DIR`, 기본 `$SIMAI_ROOT/state`)에 줄마다 JSON 을 추가만 하고 fsync 한다. 사건은 `SUBMITTED`(묶음·보정 버전·작업 ID·새로 만듦·상태·상태 조회에 다시 보낼 요청 payload), `SUBMIT_FAILED`(오류 요약 500자), `FINISHED`(작업 ID·끝 상태·실패 코드)다. 끝 줄이 깨졌으면(쓰다가 꺼짐) 그 줄만 건너뛰고, 파일을 읽지 못하면 모름으로 보고 내지 않는다. 토큰·서명 주소는 기록·로그에 남기지 않는다(-372 함수가 이미 가린다).

### 관제 뒤 운영 흐름: Tier 0 정상 종료 기록과 `simai-bootstrap after-live`

운영 흐름은 세션 끝 → 운영자가 Tier 0 를 정상 종료 → Pod 를 몇 분 더 두고 배경 복원 → Terminate 다. 이 흐름에서 개발 플래그 없이 자동 제출이 일어나게 했다.

- **정상 종료 기록**: Tier 0 `stop()` 이 모든 종료 단계(판정·링크 닫기·저널 비우기·마지막 체크포인트·저널 닫기)를 성공하면 표시 파일을 지운 뒤 그 옆에 `tier0.live.stopped`(`{"version":1,"phase":"stopped_clean","stopped_at":<UTC ms>,"pid":...}`, 원자 쓰기)를 남긴다. 종료 단계가 하나라도 실패하면 남기지 않는다(표시 파일에 `shutdown_failed`, 지금과 같음). 표시 파일을 먼저 지우므로 그 사이에 꺼지면 기록이 없다(모름 → LIVE, 안전한 쪽).
- **접수 문**(`simai_ai.gpu.admission`): 표시 파일이 있으면 지금과 같다(있으면 LIVE, 15초 넘게 갱신 없으면 끊김 → 모름 → LIVE). 표시 파일이 없을 때 기록이 읽히고 형식이 맞으면 '정상 종료로 끝남' → LIVE 아님(마지막 LIVE 뒤 `live_linger_s` 60초는 그대로 기다림), 기록이 없으면 지금처럼 모름 → LIVE, 기록을 못 읽거나 형식이 틀리면 모름 → LIVE. 대여(static_reconstruct 대여 허용)와 자동 제출이 같은 판단을 쓴다.
- **기동 쪽 지우기**: 엔진 준비(진입점 `engine_cache`) 동안은 표시 파일이 아직 없다. 지난 기록이 남아 있으면 이 틈을 LIVE 아님으로 볼 수 있어, Tier 0 시작 경로가 GPU 를 잡기 전에 기록을 지운다: `simai-bootstrap tier0`(시작 잠금 직후), `lobby`(시작 잠금 직후, 작업 폴더의 표시 파일 기준), 감시 프로세스 `start_tier0`, `Tier0Service.start`(표시 파일을 쓰기 전). 그래도 남는 경우(지우기 실패)는 로그를 남기고, 표시 파일이 생기면 그쪽이 이긴다.
- **표시 파일 자체에 쓰지 않은 까닭(Lead 지시와 다르게 한 점)**: 표시 파일이 있으면 LIVE 로 보는 소비자가 이미 있다. GPU 실험 큐 `live_guard`(`marker_files`, 있으면 LIVE), `simai-bootstrap scene` 의 30초 신선도, Tier 0 다음 시작의 '이전 실행이 정상 종료하지 않음'(표시 파일이 남아 있으면) 판단이다. 정상 종료를 같은 파일에 쓰면 이들이 모두 바뀌어야 한다. 옆 파일이면 그들의 판단은 그대로다.

**관제 뒤 워커를 어떻게 남기나: `simai-bootstrap after-live`(하위 명령)를 골랐다.** 로비 Supervisor 에 'Tier 0 가 정상 종료하면 워커를 남기는 옵션'을 넣는 안보다 기존 구조에 맞는다.
1. 로비 워커의 torch 상한은 vLLM·아바타와 나눈 몫(`tier0,vllm,worker` 0.15, 아바타 포함 0.05)이고 static_reconstruct 의 MapAnything·TSDF 자식도 그 값(`SIMAI_WORKER_TORCH_MEMORY_FRACTION`)을 다시 건다. 관제 뒤에는 워커를 분석 워커 예산 전체(0.70)로 다시 띄워야 하므로 남기는 것보다 새로 띄우는 것이 맞다.
2. 로비는 Tier 0 만 골라 끄는 운영 경로가 없다(SIGTERM·Ctrl+C 는 워커 → vLLM → Tier 0 를 함께 끈다). Supervisor 의 'Tier 0 가 끝나면 나머지를 내리고 Tier 0 종료 코드로 끝난다' 약속도 그대로 둔다.
3. 관제 밖 GPU 일회성 작업은 이미 장면 준비 모드(`scene`)가 맡는다: Tier 0 시작 잠금·표시 파일·GPU 사용 중 메모리(1 GiB 초과면 vLLM 등이 남은 것) 확인, static 가중치 확인, Tier 0 가 시작하면 그룹 SIGTERM → SIGKILL(Tier 0 우선), 워커 토큰 파일. `after-live` 는 이 위에 로비 작업 폴더(`--work`, 기본 `$SIMAI_ROOT/runs/lobby-live`)의 `state/` 를 `SIMAI_TIER0_MARKER`·`SIMAI_STATE_DIR` 로 넘기고, 정상 종료 기록이 없거나 표시 파일이 남아 있으면 띄우지 않고 75 로 끝낸다(띄워도 워커가 아무것도 하지 않으므로). 워커는 `--once` 없이 `SIMAI_AUTO_STATIC_EXIT_WHEN_DONE=1` 로 돈다. `SIMAI_WORKER_DEV_NO_LIVE`·로비의 좁은 torch 상한은 넘기지 않는다.
4. 장면 모드 자식 환경에 있던 `SIMAI_ALLOW_WITHOUT_TIER0_MARKER=1` 은 읽는 코드가 없다(워커는 `SIMAI_WORKER_DEV_NO_LIVE` 를 읽는다). 그래서 지금까지 장면 모드로 띄운 워커는 표시 파일이 없으면 static_reconstruct 를 대여하지 않았다. 정상 종료 기록이 생기면서 이 경로도 동작한다(그 변수는 건드리지 않았다).

`SIMAI_AUTO_STATIC_EXIT_WHEN_DONE=1` 이면 대기 시간이 지난 뒤 낼 일이 끝났을 때(자동 제출 작업이 끝남, 이미 낸 묶음, 완성 묶음 없음, 활성 보정과 다름 등) 워커가 스스로 끝난다(종료 코드 0). 실패·백오프·진행 중·LIVE 에는 끝내지 않는다.

**세션 끝 운영 절차**(Pod SSH, `<...>` 는 세션 값):

1. 로비를 끝낸다: `simai-bootstrap lobby` 를 띄운 셸에서 Ctrl+C(또는 그 프로세스에 SIGTERM). 로그에 `정상 종료 기록을 남겼다: <work>/state/tier0.live.stopped` 가 나와야 한다. 나오지 않으면(종료 실패) 배경 복원을 자동으로 하지 않는다. -372 도구로 손으로 낸다.
2. 완성 묶음이 있는지 본다: `jq '{calibration_version, complete, bundle}' <work>/state/clean-background/status.json`. `complete: true` 가 아니면 낼 묶음이 없다(4대 모두 충분히 모이지 않음). 3으로 가도 바로 끝난다.
3. 배경 복원을 띄운다:
   ```sh
   simai-bootstrap after-live --work /workspace/simai/runs/lobby-live \
     --worker-token-file /root/.simai_worker_token --ec2-base-url https://<EC2>
   ```
   종료 코드 75 는 정상 종료 기록이 없거나 GPU 를 다른 프로세스가 쓰는 것이다(로그의 이유를 본다).
4. 기다린다: 워커 시작 뒤 300초(`SIMAI_AUTO_STATIC_QUIET_S`) → 업로드·등록(수십 초) → 대여·실행(단계 상한 합 3780초, 4대 기준 실측은 10차 세션에서 잰다). 로그 `배경 묶음 자동 제출: SUBMITTED` → `... 작업 job-... 끝: SUCCEEDED` → `낼 일이 끝났다 ... 워커를 끝낸다` 뒤 명령이 0 으로 끝난다. FAILED 면 `quality.notes`·실패 로그 주소를 본다(자동으로 다시 내지 않는다).
5. 결과 확인: EC2 에서 `admin scene list --site <site_id>` 의 CANDIDATE, 필요하면 결과 묶음을 노트북으로 가져온 뒤 Pod 를 Terminate 한다. 기다리는 동안 Tier 0 를 다시 띄우면 워커가 멈추고(75) Tier 0 가 먼저다.

### 버린 대안

- **Tier 0 안에서 제출**: 업로드·HTTP 재시도·JPEG 디코드(해상도 확인)가 판정 프로세스의 CPU·GIL·실패 경로를 늘린다. Tier 0 는 import 규칙(`tier0-no-worker-stack`)상 `simai_ai.scene` 을 쓸 수 없어 -372 함수를 재사용하지도 못한다.
- **EC2 가 묶음을 가져가기**: EC2 는 Pod 파일을 볼 수 없고 Pod 로 들어가는 경로도 없다(I1 은 Tier 0 → EC2). 사진을 올리는 쪽은 Pod 여야 한다.
- **시간표(cron) 제출**: 관제 끝 시각을 모르고, 정해진 시각에 LIVE 가 다시 켜져 있으면 관제 중에 올린다. 이미 있는 접수 문(표시 파일 신선도)을 다시 보는 쪽이 정확하다.
- **새 DB 표(제출 기록 표)**: 같은 시기 다른 티켓과 Flyway 번호가 겹칠 수 있고 공용 시험 DB 에 먼저 적용된다. 멱등 키를 작업 input 에 싣고 advisory 잠금으로 줄 세워 표 없이 해결했다.
- **작업 상태 GET 엔드포인트**: 워커 전송 계층은 POST 뿐이다. 같은 요청 재전송이 상태 조회를 겸한다(같은 묶음은 같은 작업).
- **로비 Supervisor 에 'Tier 0 정상 종료 뒤 워커 남기기' 옵션**: 위 '관제 뒤 운영 흐름' 1·2·3. 남긴 워커는 좁은 torch 상한을 그대로 가지고, Tier 0 만 끄는 운영 경로도 없다.
- **표시 파일에 phase=stopped 를 쓰기**: 표시 파일이 있으면 LIVE 로 보는 기존 소비자(GPU 큐·장면 모드·Tier 0 다음 시작)를 모두 바꿔야 한다. 옆 파일로 뒀다.
- **업로드를 `AVATAR_ASSET` 으로 하기(첫 구현)**: 사진이 `avatars/` 아래, 보정 문서가 `.glb` 키로 들어가 목적·보존 규칙이 섞인다. 서버에 `STATIC_RECONSTRUCT_INPUT`(워커만, JPEG·PNG·JSON, 64 MiB, 키 `static-inputs/`)을 더했다. 묶음 ID·이름을 주면 키가 정해지고 같은 내용만 다시 받으므로(다른 내용은 만료 뒤에도 409, 규칙 6) 응답을 잃고 다시 내도 객체가 늘지 않는다.
- **EC2 가 가진 보정 주소 쓰기**: 서버 `calibration_version.calibration_ref` 는 `ec2:calibration_document/...` 라 워커 객체 서명(s3 주소만)으로 받을 수 없다. -372 의 `--upload-calibration` 경로처럼 보정 문서를 함께 올린다.

### 잔여 위험과 10차 GPU 세션 확인

- **자동 제출은 관제 뒤 `after-live` 에서만 일어난다**: 로비가 도는 동안은 표시 파일 때문에 LIVE 이고, 로비 워커는 Tier 0 와 함께 내려간다. 운영자가 세션 끝에 `after-live` 를 띄우지 않으면 묶음은 Pod 에 남기만 한다(볼륨이 없으면 Terminate 때 사라진다).
- **정상 종료 기록의 수명**: 기록은 다음 Tier 0 시작이 지운다. 지우기에 실패하거나(볼륨 오류) Tier 0 를 이 문서의 시작 경로 밖(직접 `python -m services.tier0`)에서 띄우면 엔진 준비 틈에 기록이 남을 수 있다. `Tier0Service.start` 도 지우므로 틈은 진입점의 엔진 준비 시간이다.
- **업로드 목적 마이그레이션 번호**: `V26__static_reconstruct_input_upload.sql`(V24 -400 `cctv_remote_usage`, V25 -408 `vlm_within_deadline` 다음, 2026-11-02 dev 에 맞춰 옮김).
- 키 없이 올린 세션 도구(-372, `bundle_id` 없음)는 다시 낼 때마다 새 키다(작업은 하나, 고아 객체는 남는다).
- EC2 DB 를 초기화(첫 운영 초기화)한 뒤 보정이 다시 등록되지 않으면, 끝나지 않은 `SUBMITTED` 의 상태 조회가 409 로 계속 실패해 새 묶음을 막는다. 이때 운영자가 기록 파일을 다른 이름으로 옮겨 보관한다(지우지 않는다).
- 서버의 묶음 조회(`input->>'clean_background_bundle_id'`)는 색인이 없다. 작업 수가 적은 MVP 기준이다.
- Tier 0 보정 저장소 형식(`active.json`, 버전 sha256 파일 이름)에 기댄다. 형식이 바뀌면 `NO_ACTIVE_CALIBRATION` 으로 멈춘다(지어내지 않는다).
- 10차 세션에서 볼 것: 로비 Ctrl+C 뒤 `tier0.live.stopped` 생김, `after-live` 시작 로그(`정상 종료 확인 → 분석 워커만`)와 워커 접수 문 `Tier 0 가 정상 종료했다`, 업로드 키 `static-inputs/bundle-.../cam*.jpg`·`calibration.json`, 워커 로그 `배경 묶음 자동 제출: QUIET → SUBMITTED`·`배경 묶음 ... → static_reconstruct 작업 job-...`, 기록 파일의 `SUBMITTED`/`FINISHED`, EC2 `analysis_job` 한 행과 `clean_background_bundle_id`, 서버 로그 `배경 묶음 ... 등록했다`, SUCCEEDED 뒤 `admin scene list` 의 CANDIDATE, 도중 Tier 0 를 켜면 업로드 중단(`LIVE`)·실행 중 작업 `LIVE_PREEMPTED` 반납, 워커 재시작 뒤 `ALREADY_SUBMITTED`, 작업이 끝난 뒤 워커가 스스로 끝남(0), MapAnything 단계의 GPU 메모리(분석 워커 예산 0.70)와 전체 소요 시간.
