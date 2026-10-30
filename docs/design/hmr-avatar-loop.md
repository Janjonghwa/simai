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

매 사람·카메라 추론 직전에 `AdmissionGate.check().admit`을 확인한다. 표시 파일 없음·오래됨·모르는 지연·600ms 초과·대기열 버림은
추론을 멈춘다. 정상 30초 뒤 재개한다. 상시 서비스는 I5 대여를 하지 않으므로 `hmr_*` LIVE 차단 목록을 바꿀 필요가 없다.
I5 배치 HMR은 계속 차단된다. 개발용 표시 파일 없음 허용 설정도 이 서비스에서는 무시한다.

## Tier 0 공유 통로

기본 `$SIMAI_ROOT/state/avatar-input.npz`, `SIMAI_AVATAR_CHANNEL`로 경로를 바꾼다.
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

Tier 0 판정 스레드는 최신 참조 한 건을 `offer`할 뿐이다. lock을 즉시 얻지 못하면 버리고, 디스크 I/O는 daemon이 한다.
daemon은 최대 5Hz로 쓰며 대기 중인 참조를 최신으로 바꾼다. 디스크 실패는 errors로 세고 Tier 0를 멈추지 않는다.
Frame.image는 전달 뒤 수정하지 않는 기존 소유권을 따른다. 원자 교체 파일과 최대 한 임시 파일만 유지하므로 프레임 이력을 저장하지 않는다.
공유 메모리 대신 파일 기반 로컬 IPC를 고른 이유는 독자 종료·재시작 때 자원 추적·seqlock 복구가 필요 없기 때문이다.

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

1. 기동 AdmissionGate가 정상 상태를 30초 유지해야 모델과 송신 스레드를 만든다. 이후 매 회차·카메라 추론 전에도 검사한다.
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
