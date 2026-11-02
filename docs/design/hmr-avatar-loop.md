# Pod HMR 상시 아바타 루프 (S15P31A101-324)

명세 3.2·5.6·12.2(I6)·20.3·20.4와 `avatar-pose-stream.md`의 송신 측 구현이다. 이번 범위는 body 모드·회색 스키닝 메시다.
Tier 0 World ID·위치·접지를 읽기만 하며 I1·위험 판정에는 HMR 결과를 보내지 않는다. I6 계약 스키마는 변경하지 않았다.

## 실행과 자원

통합 이미지에서 `SIMAI_SERVICES=tier0,vllm,worker,avatar`로 선택한다. 기본 서비스 목록은 그대로여서 기본은 꺼짐이다.
`tier0,avatar`만 선택할 수도 있다. 감시 프로세스가 Tier 0 준비 뒤 `SIMAI_AVATAR_WEIGHTS_SET=avatar`를 받아 검증하며,
이 묶음은 기존 exp02의 SAM 3D Body 체크포인트·MHR 자산 두 개만 참조한다. 검출·FOV 모델은 다시 띄우지 않는다.
Fast 저장소의 model_config.yaml과 가중치 링크를 fastcfg 폴더에 준비한다. 공식 모델 로더는 `simai_ai.hmr.loader`에서 실험과 공유한다.

avatar 선택 시 GPU 전체 대비 분석 워커 예산 0.70을 vLLM 0.55 + HMR 0.10 + I5 torch 0.05로 나눈다.
감시 프로세스가 I5에 `SIMAI_WORKER_TORCH_MEMORY_FRACTION`을 주고, HMR은 `SIMAI_AVATAR_GPU_MEMORY_FRACTION`으로 torch 할당을 제한한다.
음수 잔여 예산은 기동 전에 거부한다. vLLM을 선택하지 않으면 그 몫은 I5에 남는다. CUDA 문맥·외부 할당까지 포함한 실제 VRAM은 GPU에서 검증해야 한다.
각 프로세스는 기존과 같이 별도 감시·재기동되며 아바타 실패는 Tier 0를 종료하지 않는다.

매 사람·카메라 추론 직전에 `AdmissionGate.check().admit`을 확인한다. 표시 파일 없음·오래됨·모르는 지연·600ms 초과는 즉시,
큐 대기 초과로 버린 프레임은 5초 창당 3건 이상이면 추론을 멈추고(2건 이하는 넘김), 통과 10초 뒤 재개한다(S15P31A101-412,
`tier0-gpu-priority.md`). 상시 서비스는 I5 대여를 하지 않으므로 `hmr_*` LIVE 차단 목록을 바꿀 필요가 없다.
I5 배치 HMR은 계속 차단된다. 개발용 표시 파일 없음 허용 설정도 이 서비스에서는 무시한다.

## Tier 0 공유 통로

기본 경로는 `avatar_channel_path(env, root)`가 정한다: `SIMAI_AVATAR_CHANNEL` 명시값 → POSIX `/dev/shm`이 쓰기 가능한 디렉터리면 `/dev/shm/simai-avatar-input.npz`(tmpfs) → 그 외 `$SIMAI_ROOT/state/avatar-input.npz` 폴백(S15P31A101-385).
독립 Tier 0는 `SIMAI_AVATAR_SHARE=1`일 때만 공유한다. 감시 모드에서는 avatar 선택 시 Tier 0 자식에만 이 설정을 넣는다.

NPZ는 로컬 IPC이며 `allow_pickle=False`로 읽는다. 파일은 임시 파일에 다 쓴 뒤 `os.replace`하므로 독자는 완성된 한 묶음을 읽는다.
`metadata`는 UTF-8 JSON을 담은 uint8 배열, `image_N`은 카메라별 uint8 H×W×3 BGR 배열이다.

```text
metadata.format = simai.avatar-input@1
metadata.world = {t (= t_latest_global), calibration_version, coordinate_frame,
                  input_provenance, tracks[{World 상태 그대로 + per_camera}]}
metadata.calibration = {보정 버전·출처·좌표 규약·카메라 K/R/t/왜곡/해상도}
metadata.frames[camera_id] = {array: image_N, capture_time_ms, frame_ref}
per_camera = {camera_id, local_track_id, bbox, keypoints_2d, joint_layout,
              estimated_capture_time, frame_ref}
```

Tracker가 수락한 실제 프레임과 2D 관측을 같은 epoch/sequence/촬영 시각으로 묶는다. World Track의 local_keys로 ID를 붙인다.
전역 최신 시각과 100ms 넘게 다른 카메라는 공유 관측에서 뺀다. HMR에서도 frame_ref가 같은지 검사한다.
모르는 관절·pelvis·접지는 null 그대로이며 프레임 촬영 시각을 송신 시각으로 바꾸지 않는다.

Tier 0 판정 스레드는 직전 공유 시각으로부터 `period_s`(기본 0.2s, 5Hz)가 지났을 때만 스냅샷을 조립해 최신 참조 한 건을 `offer`한다(보정이 교체된 tick은 즉시 공유, S15P31A101-385). lock을 즉시 얻지 못하면 버리고, 파일 I/O는 daemon이 한다.
daemon은 최대 5Hz로 쓰며(`period_s - write_elapsed`만큼 대기) 대기 중인 참조를 최신으로 바꾼다. 파일 실패는 errors로 세고 Tier 0를 멈추지 않는다.
Frame.image는 전달 뒤 수정하지 않는 기존 소유권을 따른다. 원자 교체 파일과 최대 한 임시 파일만 유지하므로 프레임 이력을 저장하지 않는다.
POSIX 공유 메모리 세그먼트(`multiprocessing.shared_memory`) 대신 tmpfs(`/dev/shm`) 위 원자 교체 파일 기반 로컬 IPC를 고른 이유는 독자 종료·재시작 때 자원 추적·seqlock 복구가 필요 없으면서도 컨테이너 overlayfs 디스크 쓰기를 피할 수 있기 때문이다.

## 체형·송신

`AvatarLoop`는 입력·모델·보정 Provider·업로드·송신·시계를 주입받는다. 보정 K로 왜곡을 펴고 Tier 0 bbox만 HMR에 넣는다.
`aggregate_frame`으로 카메라 후보를 모으며 대표 포즈를 선택한다. 첫 30개 안정 프레임의 체형 중앙값으로 GLB를 굽고 고정한다.
ID 불확실·교정 시 누적을 비우고, 30개 안정 프레임 후 새 버전을 만든다. 기존 같은 ID의 확정 자산은 그동안 유지하고 새 ID는 물려받지 않는다.
GLB 업로드 재시도는 같은 버전·같은 바이트를 유지한다. texture/mask는 null, 관측 비율은 0, 미관측은 1이다.

I6는 `wss://<ec2>/ws/worker/avatar`, `Authorization: Bearer <SIMAI_WORKER_TOKEN>`이다. 워커 환경 파일 주입은 I5와 같은 도우미를 쓴다.
송신 스레드는 사람별 미송신 포즈 하나만 두며 밀리면 교체한다. 재접속은 1→2→4→8→10초, epoch·sequence는 유지한다.
연결 전 포즈는 버리고 현재 자산을 먼저 알린다. 살아 있는 자산은 10초마다 재알림하고 30초 갱신이 없으면 비운다.
포즈는 500ms 넘게 오래되면 버린다. 묶음은 64명, 512KiB 이하다. root translation을 싣지 않으며 회전은 기존 MHR 축각 라디안이다.

EC2 -325 업로드 목적은 `AVATAR_ASSET` 이다(-325 서버 `UploadPurpose`, 객체 키 `avatars/<world_id>/e<epoch>/v<version>/mesh.glb`):

```json
POST /api/uploads/sign
{"purpose":"AVATAR_ASSET", "world_id":"w-...", "stream_epoch":123,
 "appearance_version":0, "content_type":"model/gltf-binary", "size_bytes":123,
 "sha256":"접두어 없는 64자리 hex"}
```

응답은 기존 SignedUpload `{object_uri, method: PUT, upload_url, headers, ...}`다.
메시지에는 `s3://<bucket>/.../<world_id>/e<epoch>/v<version>/mesh.glb`만 싣는다. 서명 URL에는 워커 토큰을 보내지 않는다.
본문 필드(world_id·stream_epoch·appearance_version)는 -325 서버 `SignRequest` 와 같다. I1 계약은 확장하지 않는다.

## 시험·GPU 확인·위험

시험 위치는 `tests/hmr/test_avatar_loop.py`, `tests/services/tier0/test_avatar_channel.py`,
`tests/deploy/test_simai_supervisor.py`다. 가짜 HMR·가짜 소켓·공유 채널로 지연 차단, 스키마·스키마 밖 규칙,
ID 교정, 재접속/재알림/버림, 디스크 정체 시 Tier 0 비차단, 감시 등록·GPU 상한 합을 확인한다.

로컬 결과: 관련 시험 138 passed, 4 skipped(POSIX 전용), 1 deselected. import 규칙 6 kept, 0 broken.
Tier 0 전체 시험은 오래 걸려 중단했다. 그중 실패한 R1 발생 대기(`test_tier0_e2e.py`)와 저널 backlog 대기
(`test_tier0_service.py::test_stop_flushes_slow_journal_and_ends_threads`)는 변경 전 HEAD의 Tier 0 파일을 쓴
worktree 내부 사본에서도 같은 지점에서 실패했다. 후자는 최종 관련 시험에서 제외했고, 이번 이슈 범위 밖이라 수정하지 않았다.

GPU에서는 공식 모델 출력 키·shape/scale 조립·127관절 순서·손 null, 30프레임 GLB와 회전의 일치,
-325 워커 인증/서명 API/실제 wss 연결, 단절 후 최신 포즈, Tier 0 p95·최대 지연과 전체 VRAM,
다인·다시점 2~5Hz 및 500ms 신선도, Linux 프로세스 종료를 확인한다. Pod를 켜거나 실험 Run을 실행하지 않았다.

남은 위험은 GPU 미실측, 파일 IPC의 CPU·메모리 대역폭, 이미 시작한 CUDA 호출을 중간 취소할 수 없음,
4카메라 순차 추론이 500ms를 넘을 때 메시를 계속 버릴 가능성, 위 업로드 API 본문 통합이다.
500ms를 넘는 경우 포즈를 억지로 보내지 않아 뷰어는 캡슐을 유지한다. 색 텍스처 누적은 EXP-02 2단계로 넘긴다.

## S15P31A101-351: EC2 재기동 뒤 재연결·운영 진단

관련 명세는 3.2(Tier 0 우선), 5.6(HMR 표시), 12.2(I6), 12.4(`/topic/avatar`)다.
계약·I1·server·front는 변경하지 않는다. 변경 위치는 `services/avatar/{transport,__main__}.py`,
`src/simai_ai/hmr/{stream,diagnostics}.py`이며 시험은 `tests/services/avatar/`와 기존 `tests/hmr/test_avatar_loop.py`다.

### 재현과 변경

기존 송신기는 `send()`가 실패해야 소켓을 버렸다. 자산 확정 전, 입력 없음, AdmissionGate 차단 등 송신할 것이 없는 동안에는
서버가 닫은 소켓도 계속 보관했다. 메모리 가짜 WebSocket 서버의 연결 → 종료 → 연결 거부 → 재기동 시나리오로
접속 시도 횟수가 1에서 멈추는 실패를 재현했다. **이 결함의 재현과 실제 8차 세션의 원인 확정은 구분한다.**
당시 EC2 로그와 예외 정보가 없어 세션의 연결 실패 이유는 확정할 수 없다.

매 tick에서 `recv(timeout=0)`로 종료를 확인한다. I6 앱 메시지는 단방향이며 수신 대기는 하지 않는다.
패킷이 조용히 끊기는 경우를 위해 WebSocket ping 간격·응답 제한을 각각 10초로 명시한다(최대 약 20초 + 스케줄링/종료 시간 뒤 감지).
이는 표시 신선도 500ms를 늘리는 값이 아니다. 연결 거부·502·401/403·소켓 종료는 기존 1→2→4→8→10초 백오프로 계속 재시도한다.
새 프로세스나 프레임 재처리를 통한 복구는 자산·epoch를 불필요하게 바꾸므로 사용하지 않는다.
재접속 직전 대기 포즈는 버리고 현재 자산을 재알림한 뒤 새 포즈만 보낸다. epoch와 batch_sequence는 유지한다.
자산의 기존 10초 재알림·30초 미갱신 제거 정책도 유지한다.

연결·재연결·끊김의 예외 종류/메시지와 재시도 초, 자산 업로드 성공/실패, 60초마다 성공한 포즈 묶음 수를 기록한다.
`dropped_poses`는 기존 누적값이며 분당 수치는 `pose_batches`다. 입력 쪽도 최대 60초마다 공유 입력 없음,
중복/오래된 입력, gate 차단 사유, 후보/선택 수, 누적 샘플/자산 수를 남긴다.
서비스 회차 실패는 예외 종류와 메시지를 한 줄로 기록하며 워커 토큰·URL·인증값은 제거하고 메시지는 300자로 제한한다.
업로드 서명 거부에는 HTTP 상태가 남는다. 영구적인 토큰 거부도 재시도하므로 인증 설정이 복구되지 않으면 계속 실패한다.

### 송신 시작 조건과 8차 세션 추정

1. 기동 AdmissionGate가 통과 상태를 10초(S15P31A101-412 전에는 30초) 유지해야 모델과 송신 스레드를 만든다. 이후 매 회차·카메라 추론 전에도 검사한다.
2. 공유 NPZ에 새 World 시각이 있어야 한다. 입력은 500ms 이내, per_camera/frame_ref는 실제 영상과 일치하고 카메라 시각 차이는 100ms 이하여야 한다.
3. ID가 확실한 트랙의 HMR 후보가 집계를 통과해야 체형 샘플이 쌓인다. 같은 ID/revision에 최소 30개 유효 샘플과 안정 프레임이 필요하다.
4. GLB 생성 → 서명 API → PUT이 성공해야 `avatar_asset`을 등록하고 포즈를 대기열에 넣는다. 최종 포즈도 500ms 이내·gate 허용 조건을 통과해야 한다.
5. WebSocket 연결은 30개 샘플이나 업로드를 기다리지 않는다. 따라서 자산이 없다는 이유만으로 EC2 연결 줄이 없는 현상을 설명할 수는 없다.

읽기 전용 자료 `20261030-s8/s8-l1-avatar.log`에는 완료된 `process_one_image`가 없고,
`s8-l3-avatar.log`에는 완료 로그 27회·RGB 안내 27회·회차 실패 2회(388·415행)가 있다.
실패는 추론 완료 로그 뒤에 나오지만 예외 종류/메시지가 없다. **추정:** 제공된 구간에서는 모델 시작/공유 입력/gate를
적어도 일부 통과했으나, 총 27번의 완료 호출만으로는 한 사람의 30개 체형 샘플을 채울 수 없어 자산 확정 전일 가능성이 크다.
자료가 전체 실행을 담았다고 보장할 수 없고, 어댑터 후처리·집계 실패나 ID 변경·gate 정지도 배제할 수 없다.
EC2 접속 실패(502·인증·망)와 회차 실패의 정확한 원인은 새 로그로 다음 세션에서 확인해야 한다.

공유 통로는 uint8 BGR이고 `FastBodyModel.infer()`는 왜곡 제거 뒤 RGB로 한 번 변환한다.
서로 다른 채널 값 `[10,20,30]`이 모델에 `[30,20,10]`으로 전달되고 원본 BGR 배열은 유지되는 시험을 추가했다.
RGB 안내만으로 색 순서 오류라고 판단하지 않는다. 변환 코드는 바꾸지 않았다.

### 서버 계약 대조와 관제 구독 확인(코드만)

| 구간 | 대조 결과 |
| --- | --- |
| 인증/업로드 | `/ws/worker/avatar` 워커 Bearer와 `POST /api/uploads/sign`의 `purpose=AVATAR_ASSET`이 서버와 일치한다. `world_id`, `stream_epoch`, `appearance_version`, `content_type=model/gltf-binary`, `size_bytes`, 접두어 없는 sha256도 `UploadSignService.SignRequest`와 일치한다. |
| PUT | 응답은 감싸지 않은 `SignedUpload`: `upload_url`, `method=PUT`, `headers`, `object_uri`. 서명 헤더와 GLB 바이트만 HTTPS PUT에 보내고 워커 Bearer는 보내지 않는다. 객체 경로는 `avatars/{world_id}/e{epoch}/v{version}/mesh.glb`다. 서버 최대 자산 크기는 64MiB다. |
| `avatar_asset` | 1.0.0 봉투와 World ID/epoch/revision/appearance version, 이전 버전, 생성 시각, 보정/모델/출처, mesh/skeleton/shape/texture/surface_source/face_policy 필드는 서버 파서와 일치한다. mesh 해시는 `sha256:` 접두어를 유지한다. |
| `avatar_pose_batch` | 1.0.0, epoch/sequence/dropped/context와 poses의 World ID/revision/촬영 시각/version/inferred/MHR 회전 필드가 일치한다. 위치는 없다. 기존 스키마·스키마 밖 규칙 시험도 통과한다. |

관제 `lib/stomp.ts`는 연결마다 `/topic/avatar`를 다시 구독하고 `finally`에서 Source로 넘긴다.
`SpatialViewportHost`는 `lib/avatarMeshFetch.ts`의 Bearer·401 갱신을 쓰는 바이너리 조회 함수를 뷰어에 전달한다.
서버 `AvatarController`는 등록 자산을 인증 확인 뒤 S3 GET 서명 주소로 302 리다이렉트한다.
따라서 브라우저→API의 경로/인증, 리다이렉트 목적지의 CORS·GET 허용·서명 만료·버킷 권한은 실제 배포에서 확인해야 한다.
기본 `fetchAvatarMesh`만 따로 쓰는 경로에는 Bearer가 없지만 현재 관제 Host는 인증 함수를 주입한다.

뷰어는 자산 sha256·관절 수·부모 해시·pelvis 인덱스와 HTTPS의 `crypto.subtle`을 요구한다.
ID/revision·보정 불일치, Tier 0 pelvis 없음, HMR 지연/포즈 중단 500ms, 자산 부재·로딩·검증 실패면 캡슐이다.
자산 읽기 실패는 10초 뒤 다시 시도한다. Pod 재연결 성공만으로 메시 표시 성공까지 보장하지 않는다.

### 검증과 남은 일

`ai/`에서 외부 네트워크 없이 실행했다. `UV_CACHE_DIR=ai/.uv-cache`(실행 cwd 기준 `.uv-cache`),
`PYTHONUTF8=1`, `PYTHONDONTWRITEBYTECODE=1`을 적용하고 임시 시험 폴더도 worktree 안에 고정했다.

- 수정 전: `uv run --offline pytest tests/services/avatar -q -p no:cacheprovider --basetemp=.pytest-tmp-351` → **1 failed, 3 passed**(유휴 서버 종료 뒤 재접속 누락).
- 최종: `uv run --offline pytest tests/services/avatar tests/hmr tests/services/tier0/test_avatar_channel.py -q -p no:cacheprovider --basetemp=.pytest-tmp-351` → **72 passed, 3 skipped**(외부 MHR 실자산 없음).
- `uv run --offline lint-imports` → **6 kept, 0 broken**.
- 메모리 가짜 서버로 종료/재기동/자산 재알림/새 포즈·순번 유지, 실제 websockets `InvalidStatus`로 502/401/403, 가짜 전송으로 연결 거부·네트워크 오류와 서명/PUT·비밀값 제거·분당 집계를 검증했다. 네트워크 금지 지시에 따라 실제 포트를 여는 서버 시험은 하지 않았다.
- 리뷰 생략(사용자 지시). 전체 pytest·GPU 실행·실제 EC2/S3/브라우저 검증은 하지 않았다.

Lead 후속: 실제 Pod에서 30샘플/업로드 성공/분당 송신 로그를 확인하고 EC2 배포 재기동 뒤 재접속과 관제 메시까지 확인한다.
남은 위험은 당시 예외 원인 미확정, 지속 인증 거부, GPU 순차 추론/IPC 지연으로 500ms 신선도 탈락,
실제 모델 GLB 검증 및 S3/CORS 설정이다. 정답/합성 시험 결과를 제품 성능 수치로 해석하지 않는다.

## S15P31A101-362: YOLO26x-seg 프롬프트·텍스처 마스크

관련 명세는 3.2(Tier 0 우선), 5.6(mask 프롬프트·inferred 표시)다. 계약·Tier 0 관측을 바꾸지 않는다.
코드는 `hmr/{segmentation,prompt,runtime,stream,appearance_stream}.py`, 조립은 `services/avatar/__main__.py`다.
시험은 `tests/hmr/test_segmentation.py`, manifest 회귀는 `tests/deploy/test_simai_supervisor.py`에 있다.

### 주기와 좌표

서비스의 기존 200ms 회차에서 새롭고 500ms 이내인 World 입력을 HMR에 처리할 때마다 seg도 한 번 실행한다.
같은 촬영 시각·프레임 참조 불일치·100ms 넘는 카메라 시차·불확실 ID·LOST는 seg 입력에서도 제외한다.
쓸 사람이 있는 카메라만 묶어 `YOLO.predict(list[BGR])` 한 번으로 실행하며 사람 클래스 0, conf 0.25,
imgsz 1280, retina_masks를 쓴다. 1280은 기존 오프라인 HMR 프롬프트 경로와 같은 초기값이며 GPU에서 조정할 수 있다.
장치는 HMR 파라미터의 device를 그대로 쓰고 기존 아바타 프로세스 GPU 메모리 상한 안에서 함께 돈다.
seg 배치 직전과 이후의 사람·카메라 HMR 호출 직전에 AdmissionGate를 다시 확인한다.

`HmrImageMap(camera, camera.K)`가 만든 왜곡을 편 BGR 그림을 seg와 HMR이 공유한다.
`build_prompts`의 bbox IoU ≥ 0.5·인스턴스 일대일 배정을 그대로 쓰며 중복 World ID는 건너뛴다.
Fast SAM 3D Body에 `(1,H,W)` uint8 `masks`를 함께 넣고, 배정되지 않은 사람은 `masks` 인자를 생략한다.
이번 변경은 mask 프롬프트만 추가한다. body 모드의 keypoint 재프롬프트 설정은 기존대로 유지한다.

같은 배정 마스크를 `HmrImageMap.mask_to_original`로 원본 왜곡 영상에 돌려 외형 누적에 전달한다.
원본 픽셀을 HMR 좌표로 사상해 최근접 표본을 읽으며 사상 실패·영상 밖은 False다(마스크를 퍼뜨리지 않음).
보정 버전별 사상 표를 캐시하고 버전이 바뀌면 비운다. 역변환 표는 64행씩 계산해 큰 중간 배열을 제한한다.
1080p 4카메라에서 정방향 float32 x/y 표와 역변환 int32 표는 합계 약 95MiB다.
사람별 원본 마스크는 해당 회차의 `(world_id,camera_id)` 사전에만 두며 상류 per_camera에 필드를 쓰지 않는다.
`AppearanceView.mask`에서 기존 bbox/다른 사람 가림막·메시 실루엣 안쪽 감쇠(-361)와 교집합으로 사용한다.

### 설정·폴백

- `SIMAI_AVATAR_SEG=auto|0|1`: 기본 auto는 CUDA가 있으면 켜고 없으면 끈다. 0은 모델·가중치·CUDA 검사도 생략한다.
  1은 CPU 시험도 허용한다. 서비스에서는 HMR과 같은 장치를 주입한다.
- `SIMAI_AVATAR_SEG_WEIGHTS`: 기본 `$SIMAI_ROOT/weights/yolo/yolo26x-seg.pt`.
  `SIMAI_AVATAR_SEG_WEIGHTS_SHA256`은 접두어 유무를 모두 받으며 기본은 `docs/data/weights.md`의 해시다.
- `deploy/weights.json`에 새 이름으로 `avatar`·`exp02` 묶음을 추가했다. bootstrap은 이 env와 `_SHA256`을 채운다.
  URL·해시·라이선스는 기존 가중치 문서와 같다. 잠금 파일과 의존성은 바꾸지 않는다.

ultralytics는 첫 seg 호출에서만 import하며, 먼저 로컬 파일과 해시를 확인해 런타임 자동 다운로드를 막는다.
가중치 없음·해시 오류·seg 추론/출력/역변환 실패는 한 번 경고하고 해당 프로세스의 seg를 끈 뒤 bbox로 계속한다.
실패한 모델 참조도 놓아 HMR이 GPU 메모리를 다시 쓸 수 있게 한다. 재시작해야 seg를 다시 시도한다.
예외 본문 대신 종류만 기록한다. 마스크가 없거나 IoU가 낮은 정상 결과는 경고 없이 bbox로 처리한다.
retina 마스크 크기가 다르면 실패로 본다. letterbox를 포함한 마스크를 단순 resize하는 대안은 좌표가 틀릴 수 있어 쓰지 않는다.
bootstrap의 다운로드·해시 검사 실패는 기존 규칙대로 분석 서비스 시작을 막는다. 여기의 bbox 폴백은 **서비스 실행 중** seg 실패에 적용된다.

### CPU 합성 측정·9차 GPU 세션

측정 스크립트는 `.agy/seg_cpu.py`, 기록은 `.agy/seg-cpu-run.json`이다.
실가중치 없이 합성 640×480 4카메라의 사각형을 밝기 임계값으로 분할하는 가짜 seg를 CPU에서 한 번 실행한다.
측정 지표는 `fake_seg_batch_ms`(가짜 seg 호출만), `prepare_total_ms`(그림 준비·최초 역변환 표·프롬프트까지)다.
이는 배선 비용 확인이며 YOLO26x-seg 지연·제품 성능·게이트 통과 근거가 아니다. 수치는 `.agy/notes.md`에 남긴다.

9차 세션에서 매 배치의 다음 INFO 줄을 수집한다.

```text
아바타 seg batch cameras=4 instances=... matched=... elapsed_ms=... device=cuda:0
```

`elapsed_ms`는 seg 호출 앞뒤 CUDA synchronize를 포함하는 벽시계 시간이다. 첫 호출은 가중치 검증·적재·워밍업도 포함한다.
그림 준비·역변환·HMR·텍스처·송신 시간은 포함하지 않으므로 처음/후속 호출을 나눠 보고 기존 Tier 0 지연·VRAM·포즈 신선도 로그와 대조한다.
가중치 실패는 `아바타 seg 끔: <예외 종류>; bbox만 사용 (재시작 전까지)`로 확인한다.
실제 YOLO 결과와 Fast SAM mask 프롬프트의 GPU 통합, 다인 일대일 배정/윤곽, Tier 0 p95·최대 지연,
아바타 프로세스의 기존 0.10 GPU 예산 내 공존, 최초 역변환 표 생성 때 신선도 탈락은 GPU 세션에서 확인한다.
CUDA 호출은 중간 취소할 수 없으며 seg 도입으로 기존 500ms 신선도 조건을 완화하지 않는다.


## S15P31A101-376: 입력 신선도(`max_age_ms`)와 접수 안정 대기(`AdmissionGate`)의 Tier 0 지연 예산 정합

관련 명세는 3.2(GPU 공유·Tier 0 우선), 5.6(HMR 2~5 Hz 보조 스트림), 6.6(`capture_to_risk` p95 600ms), 13.2·20.4(관제 화면 포즈 신선도 `t_latest_global - capture_time_ms ≤ 500ms`), 20.5(안전 경로 격리)다. 변경 위치는 `ai/src/simai_ai/gpu/{admission,__init__}.py`, `ai/src/simai_ai/hmr/stream.py`, `ai/services/avatar/transport.py`, 시험은 `ai/tests/gpu/test_admission.py`, `ai/tests/hmr/test_avatar_loop.py`다.

### 9차 세션 실패 원인과 수치 근거

9차 GPU 세션(`s9-j4-avatar.log`, `s9-j7-avatar.log`)에서 Tier 0 `capture_to_risk` 보정 지연(`T_lag`)은 평시 300~550ms(예산 600ms 이내)였고 가끔 단발 표본 1개가 672ms로 튀었다. 그러나 아바타 루프는 두 곳에서 막혀 `samples_max=0/30 assets=0`에 머물렀다.

1. **Pod 벽시계 기준 신선도 상한(`DEFAULT_MAX_AGE_MS = 1,100ms`)**:
   - 명세 13.2·20.4의 500ms는 관제 화면(브라우저)에서 **같은 촬영 시계축 위의 두 시각 차이**(`world_state.t_latest_global - avatar.capture_time_ms ≤ 500ms`)에 대한 상한이다.
   - 반면 Pod의 `AvatarLoop.step()`과 `AvatarSender.tick()`이 비교하는 `clock_ms() - t`(`t = snapshot["world"]["t"] = t_latest_global`)는 **Pod 현재 벽시계 시각과 카메라 촬영 시각의 차이**이므로, (a) Tier 0가 촬영 프레임을 받아 판정·공유 통로에 내보내기까지의 지연과 (b) 아바타 루프가 공유 통로를 읽고 `YOLO26x-seg`·`Fast SAM 3D Body` 다중 뷰 추론·송신을 마치는 시간을 모두 포함한다.
   - 기존 `max_age_ms=500`은 Tier 0 지연이 정상 예산(300~550ms ≤ 600ms) 안에 있어도 `clock_ms() - t`가 즉시 500ms를 넘기거나 HMR 추론 직후 500ms를 넘겨 입력·포즈를 전부 버렸다(`s9-j7-avatar.log`: `중복/신선도 초과 입력 people=0 samples_max=0/30`).
   - 따라서 `simai_ai.hmr.stream`에서 분석 워커 접수 문과 같은 Tier 0 지연 예산 상수(`TIER0_LAG_BUDGET_MS = DEFAULT_LAG_BUDGET_MS = 600`, 명세 6.6 `capture_to_risk` p95 600ms)에 아바타 스트림 처리·표시 여유(`HMR_STREAM_SLACK_MS = 500`, 명세 5.6 2~5 Hz 주기 및 명세 13.2/20.4 500ms 여유)를 더한 `DEFAULT_MAX_AGE_MS = TIER0_LAG_BUDGET_MS + HMR_STREAM_SLACK_MS = 1_100`을 기본값으로 두고, `AvatarSender`도 같은 `DEFAULT_MAX_AGE_MS`를 쓰도록 맞췄다.

2. **`AdmissionGate` 30초 안정 대기의 단발 초과 허용(`max_over_budget_samples = 1`)** — S15P31A101-412 에서 없앴다(명세 3.2 창 최댓값 기준과
   어긋나고, 재개가 10초로 줄어 단발 초과 비용이 작아짐). 지금 규칙은 `tier0-gpu-priority.md` 의 S15P31A101-412 절. 아래는 당시 기록이다:
   - Tier 0는 약 1 Hz로 `T_lag` 표본을 `LoadWindow`에 넣고 5초마다 `$SIMAI_ROOT/state/tier0.live`를 갱신한다. `resume_after_s = 30.0` 창은 5초 표시 파일 6개(약 30개 표본)에 해당한다.
   - 명세 6.6의 목표는 `capture_to_risk` **p95 ≤ 600ms**이므로 30초(30표본) 창에서 단발 표본 1개(`1/30 = 3.3% ≤ 5%`)가 600ms를 넘더라도 p95는 여전히 600ms 이하를 만족한다. 반면 30초 창에서 2개 이상(`2/30 = 6.7% > 5%`) 넘거나 한 5초 창에서 2개 이상 넘거나 두 표시 파일 창 연속으로 넘으면 p95 600ms 위반 또는 지속 과부하다.
   - 기존 `AdmissionGate`는 `lag_ms_max > 600`이 단 1회만 나와도 `_ok_since`를 무조건 `None`으로 리셋해 `안정 대기 26/30s`에서 다시 0초로 돌아갔다(`s9-j4-avatar.log`).
   - `AdmissionPolicy`에 `max_over_budget_samples: int = 1`을 추가하고, **이미 예산 이내로 동작 중이던 Tier 0(`_ok_since is not None`)**에서 `resume_after_s`(30초) 창 안의 초과 표본 합계·연속 초과 표본 수·단일 창 초과 표본 수가 모두 `max_over_budget_samples`(1개) 이하일 때만 안정 대기 시계(`_ok_since`)와 열림 상태를 유지한다.
   - 아바타 루프는 5초짜리 표시 파일 하나가 유지되는 동안 0.2초 폴링 및 카메라별 추론 전 확인으로 `gate.check()`를 수십 번 호출하므로, `(st_mtime_ns, raw_bytes)`로 표시 파일 갱신을 식별해 같은 파일의 단발 표본을 중복 합산하지 않는다.
   - 지연을 모르는 상태(표시 파일 없음/오래됨/깨짐, `phase != "running"`, GPU 선확보 미완, `samples == 0`, `unknown ≥ 1`, `queue_wait_exceeded ≥ 1`), 초기 기동 직후(`_ok_since is None`)의 초과, 단일 창 다수 표본 초과(`≥ 2`), 연속 창 초과(예: `678ms → 666ms`), `resume_after_s` 내 반복 초과는 기존과 똑같이 즉시 차단하고 30초 안정 대기를 처음부터 다시 센다.

### `AdmissionGate` 호출자 영향 범위

- `services/avatar`(`__main__.py` 기동 대기 및 `AvatarLoop.step()`): 9차 로그 패턴(평시 300~550ms + 가끔 672ms 단발 1회)에서 30초 안정 대기가 리셋되지 않고 통과하며, 열려 있는 동안에도 단발 1회 스파이크로 추론이 끊기지 않는다.
- `services/worker`(`runner.py`, VLM 설명·보정 BA·TBM 보고서 등 I5 Job): 같은 `AdmissionGate` 기본 정책을 공유한다. 명세 3.2·6.6 기준이 동일하므로 공통 적용이 타당하며, `DEFAULT_LIVE_BLOCKED`(`static_reconstruct`, `hmr_refine`, `hmr_appearance_update`) 차단과 지시문 4(불명·연속 초과 즉시 차단)는 그대로 유지된다.

## S15P31A101-385: 공유 통로 /dev/shm 이전·5 Hz 스냅샷 조립 게이팅·메타데이터 우선 읽기

관련 명세는 3.2(Tier 0 우선), 3.12(저장 위치), 5.6(상시 아바타 루프)이다. 계약 스키마(`contracts/`)는 바꾸지 않는다.
변경 위치는 `src/simai_ai/io/avatar_channel.py`, `services/tier0/{perception,service}.py`, `services/avatar/__main__.py`, `src/simai_ai/hmr/stream.py`이며 시험은 `tests/services/tier0/test_avatar_channel.py`, `tests/hmr/test_avatar_loop.py`다.

### 배경과 변경

1. **공유 파일 경로 `/dev/shm` 이전 (`avatar_channel_path`)**:
   - 2304×1296×3 BGR 4장(비압축 34.17 MiB, 35.83 MB)을 5 Hz로 `$SIMAI_ROOT/state/avatar-input.npz`에 쓰면 컨테이너 overlayfs에 초당 약 179 MB 쓰기·읽기가 발생한다.
   - `avatar_channel_path(env, root)`는 `SIMAI_AVATAR_CHANNEL`이 지정되면 그대로 쓰고, 지정되지 않았을 때 POSIX `/dev/shm`이 쓰기 가능한 디렉터리면 `/dev/shm/simai-avatar-input.npz`(tmpfs)를 기본값으로 선택하며, 그 외 환경(Windows 노트북 등)에서는 기존 `$SIMAI_ROOT/state/avatar-input.npz`로 폴백한다.
2. **Tier 0 `Perception._share_avatar` 5 Hz(200 ms) 스냅샷 조립 게이팅 및 보정 직렬화 캐시**:
   - 기존에는 10 Hz `Perception.step`마다(`judge_lock` 보유 중) 모든 트랙의 `per_camera` 사전·관절 목록과 카메라 4대의 `K/R/t` `.tolist()`를 조립해 `LatestChannelWriter.offer`에 넘기고, writer 주기가 0.2 s(5 Hz)라 절반을 덮어쓰며 버렸다(`dropped` 증가).
   - `Perception._share_avatar`는 직전 공유 시각(`_last_avatar_t`)으로부터 `period_s`(기본 0.2 s = 200 ms)가 지나지 않았으면(`0 <= t_latest - last_t < period_ms`) 트랙·보정·참조 사전을 조립하지 않고 즉시 반환한다.
   - 같은 `CameraRig` 인스턴스 동안 보정 직렬화 사전(`_avatar_calibration_doc`)을 재사용하고, `replace_rig`로 보정이 교체되면 캐시와 `_last_avatar_t`를 함께 비워 교체 직후 tick은 200 ms 전이어도 새 보정으로 즉시 공유한다.
   - `LatestChannelWriter._run`은 `_write` 소요 시간을 뺀 남은 주기(`period_s - elapsed`)만 대기해 쓰기 시간 누적에 따른 주기 밀림을 막는다.
3. **소비자(`LatestChannelReader`·`AvatarLoop`) 메타데이터 우선 읽기**:
   - `LatestChannelWriter._write`는 NPZ 첫 엔트리로 `metadata`를 기록한다.
   - `LatestChannelReader.latest(predicate=...)`는 `self.path.read_bytes()`로 34.17 MiB 전체를 메모리에 복제한 뒤 읽던 방식을 바꿔, 열린 파일 핸들(`self.path.open("rb")`)에서 `metadata`만 먼저 읽고 `predicate(doc)`가 거짓(중복 `t <= last_t` 또는 신선도 `max_age_ms` 초과)이면 `image_0..3` 배열을 읽지 않고 `(doc, {})`를 반환한다.

### 합성 측정과 남은 위험

노트북 CPU 합성 벤치마크(5명×4카메라×17관절 10 Hz 200 tick 및 2304×1296×3×4 NPZ 34.17 MiB, 제품 성능 지표 아님):
- `_share_avatar` 200 tick 총 소요: 게이팅 전 7.57 ms(`offer` 200회, 절반 덮어쓰기) → 5 Hz 게이팅·보정 캐시 적용 후 4.19 ms(`offer` 100회).
- 34.17 MiB NPZ 읽기 중앙값(10회): 기존 `BytesIO(read_bytes())` 전체 읽기 35.89 ms → 단일 파일 핸들 통과 읽기 27.94 ms, 중복·신선도 초과(`predicate` 거부) 시 메타데이터만 읽기 **0.298 ms**(영상 배열 34.17 MiB 읽기·할당 생략).
- 남은 위험: 9차 조사(`findings-tier0-control-drops.md`)의 j6에서 아바타 공유를 꺼도 4~5명 구간의 느린 `perception.step`은 남았으므로 이 변경만으로 `perception.step` 병목이나 `max_age_ms=500` 탈락이 모두 해소되지는 않는다. Pod `/dev/shm` 실측과 다인 동시 부하 검증은 다음 GPU 세션에서 확인한다.
