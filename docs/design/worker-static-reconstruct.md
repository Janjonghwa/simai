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

`open3d`는 기존 TSDF/렌더 구현의 의존성인데 ai/uv.lock에 없었다. Lead 가 `scene` 묶음에 `open3d>=0.19`(Linux x86_64)를 더하고 다시 잠갔다(open3d 0.20.0, 잠금은 추가만 있고 바뀐 판 없음). `worker` 묶음이 `scene` 을 포함하므로 통합 이미지를 다시 빌드하면 texrecon 과 함께 들어간다. 다시 빌드하기 전 이미지에서는 위 폴백(정점색)으로 돈다. 9차 실 GPU·texrecon 빌드/실행·S3/EC2 왕복·정렬 문턱 실측, -363 배경 자동 제출·-365 접수/Scene 등록이 남는다. 실패/취소 때 임시 작업 폴더는 정리된다. 실패 로그는 유효 대여가 남아 있을 때 failed-이름.log 객체로 보존하고 오류 메시지에 주소를 남긴다. 취소·대여 상실 뒤에는 업로드하지 않으므로 그때의 로그 보존은 후속 보완 대상이다.
