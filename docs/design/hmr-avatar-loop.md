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
