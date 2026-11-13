# Tier 0 단계 지연 계측 보강·곁가지 프로세스 분리·자원 격리 (S15P31A101-662)

13차(혼잡 7명+ judge p95 378 ms, Tier 0 GPU 3%·CPU 270% 한 프로세스)와 14차(아바타 예산 150 ms 로 풀자 Tier 0 지연 600 ms+, PPE
관문 차단)에서 Tier 0 가 늦어진 원인을 GPU 세션에서 가를 수 있게 계측을 보강하고, 판정에 끼지 않는 곁가지 일 하나를 스위치(기본 끔)로
별도 프로세스에 빼며, 같은 Pod 안 자원 다툼을 줄이는 작은 격리를 더한다. **판정 중심(cross_view·world_track·JointGate)은 이 티켓에서
바꾸지 않는다.** 이미 잰 수치로 보면 주범은 곁가지가 아니라 판정 중심의 Python 루프이고(1절), 그 최적화는 Lead 가 후속 티켓으로 나눈다.

Lead 지시 변경(티켓 진행 중)을 반영했다: 계측은 새로 짜지 않고 기존 StageTiming·30초 `stage_timing` 줄·표시 파일에 빠진 것만 더한다,
곁가지 분리는 아바타 공유로 한정하고 event_frames·Re-ID 는 뺀다, 자원 격리를 더한다, I1 health 계약은 바꾸지 않는다.

## 관련 명세·코드·시험

| 명세 | 이 문서에서 |
|---|---|
| 3.2 Tier 0 가 위치·판정 정본, GPU·자원 우선, 지연 감시로 아바타 멈춤 | 판정 중심은 프로세스 안에 그대로, 곁가지만 분리, 자원 격리 |
| 5.1 디코딩과 추론 분리·최신성 | 수신 처리·디코딩·대기·검출 구간 계측(기존 + `frame_receive`) |
| 5.2 cross_view·world_track | 계측만 읽는다(동작 변경 없음) |
| 6.6 지연 예산(capture_to_risk p95 600 ms) | 단계별 p50·p95, 인원수별 judge_tick p95 표 |
| 12.3 I1 health | **바꾸지 않는다**(계약 변경 없음) |

| 구분 | 경로 |
|---|---|
| 계측 | `ai/src/simai_ai/tracking/stage_timing.py`(summary 에 p50), `ai/services/tier0/thread_cpu.py`(스레드 CPU), `ai/services/tier0/live_input.py`(`frame_receive`), `ai/services/tier0/i1_client.py`(I1 직렬화·송신), `ai/services/tier0/service.py`(루프 깨어남 지연, 30초 줄·status) |
| 곁가지 분리 | `ai/services/tier0/side_work.py`(`OffloadedChannelWriter`, 자식 `_avatar_writer_main`), `ai/services/tier0/perception.py`(`avatar_metadata` 순수 함수, `_share_avatar`), `ai/src/simai_ai/io/avatar_channel.py`(`_prepare`·`_publish` 나눔), `ai/services/tier0/settings.py`(`offload_side_work`) |
| 자원 격리 | `deploy/scripts/simai_bootstrap.py`(`tier0_cpus`, `SIMAI_TIER0_CPUS`), `ai/src/simai_ai/gpu/cv_threads.py`(`SIMAI_CV2_THREADS`, Tier 0·아바타·워커 진입점), `ai/services/tier0/yolo_detector.py`(`SIMAI_TIER0_CUDA_STREAM_PRIORITY`) |
| 시험 | `ai/tests/services/tier0/test_tier0_stage_timing.py`, `ai/tests/tracking/test_stage_timing.py`, `ai/tests/services/tier0/test_side_work_offload.py`, `ai/tests/services/tier0/test_resource_isolation.py`, `ai/tests/deploy/test_simai_supervisor.py`(CPU 고정) |

## 1. Tier 0 프로세스 안 스레드·주기 작업 목록

GIL 점유는 "순수 Python 계산이 차지하는 비중"의 추정이다(C 구간 PyAV 디코딩·swscale·TensorRT·numpy 복사·소켓 I/O 는 GIL 을 놓는다).
판정 결합은 그 일의 결과가 world_state·RiskEvent 에 들어가는지다.

| 스레드(이름) | 하는 일 | 주기 | GIL 점유 추정 | 판정 결합 |
|---|---|---|---|---|
| `i0-video-conn-*`(카메라 4) | I0 기록 읽기·헤더 검증·메시지 변환·클립 링 버퍼·H.264 디코딩(BGR 은 늦춤, -386) | 100 fps 합 | 중(검증·변환 Python, 디코딩은 C) | 있음(프레임) |
| `tier0-detect` | 대기열 꺼내기 + BGR 변환, YOLO26x-pose(TensorRT, 검출·포즈 한 번), ultralytics 전·후처리, Local Tracker | 20 Hz | **높음**(ultralytics Results·추적기 Python) | 있음 |
| `tier0-judge` | 관측 수거·ROI·cross_view 삼각측량·world_track·접촉·판정·world_state·저널 제출, 1 Hz health | 10 Hz | **매우 높음**(인원수에 초선형, 아래) | 정본 |
| `tier0-housekeeping` | 시계 교환 요청, 1 Hz 지연 표본·fps·클립 poll·경고, 5 s 표시 파일, 30 s 계측 줄 | 10 Hz 깨어남 | 낮음 | 없음 |
| `i1-publisher` | world_state 10 Hz·health 1 Hz 봉투, 출처(보정 전이·PPE outbox) 꺼내기 | 100 Hz tick | 낮음(아래 위험 참고) | PPE→R9 입력 |
| `i1-writer`·`i1-connection`·`i1-send-watchdog` | JSON 직렬화·TLS 송신, 수신·명령 처리 | 메시지마다 | 낮음(7명 world_state 직렬화 0.16 ms) | 송신 순서·msg_id |
| `event-journal-writer`·`tier0-checkpointer` | 저널 fsync, 1 s 체크포인트 | 이벤트·1 s | 낮음(I/O) | 복구 |
| `avatar-ipc`(아바타 켬) | 크롭 상자·크롭 복사·메타 JSON·`np.savez`·`os.replace` | 5 Hz | 낮음~중(아래 실측) | 없음 |
| `tier0-reid-crop`·`tier0-reid` | Re-ID 크롭·임베딩(COPE, GPU) | 관측마다(상한 8 crop/s) | 중 | **있음**(world_track `feature_lookup`) |
| `tier0-event-frames`·`tier0-event-s3_*` | CRITICAL R1~R4 최초 RAISED 때만 JPEG·S3 | 이벤트 때만 | 평시 0 | 없음 |
| `tier0-drift-watch` | 재투영 감시(1 Hz 표본) | 1 Hz | 낮음 | 있음(카메라 제외 -646) |
| `tier0-live-calibration` | 후보 보정(보정이 CALIBRATED 가 아닐 때만) | 60 s | 돌 때 높음 | 있음(채택 시 rig) |
| `tier0-clean-background` | 배경 표본 shm 넘기기(계산은 자식, -563) | 표본 5 s | 낮음 | 없음 |
| `tier0-rotation-recovery` | 회전 복구(계산은 subprocess) | 15 s | 낮음 | 있음(채택) |
| `tier0-camera-check-tap` | 세팅 카메라 확인 view 파일 쓰기(요청 있을 때만) | 요청 때만 | 평시 0 | 없음 |
| (C 스레드) PyAV·swscale·torch | 디코딩 슬라이스·색 변환·CUDA | - | GIL 없음 | - |

판정 중심이 주범이라는 근거(11-08 H100 녹화 재생, `docs/design/tier0-gpu-priority.md` -547 배경): `judge_tick` p95 는 0명 0.3 ms →
3명 97 ms → 4명 233 ms → 5명 333 ms → 6명 422 ms 로 인원수에 초선형이다(3명 최대 262 ms 중 cross_view 104 ms·world_track 170 ms).
곁가지 중 상시 도는 것은 아바타 공유뿐이고, 노트북 실측(아래 3절) 그 한 건이 5 Hz × 약 4 ms 다. 곁가지를 모두 빼도 judge 의 수백 ms 는
남는다. **판정 중심 최적화(cross_view 메모이제이션, world_track `_others` 재조립 제거, JointGate·갤러리 벡터화, `WorldState.from_payload`
역파싱 제거, ultralytics_local 관절 isfinite 벡터화)는 후속 티켓이다.**

이미 있던 계측(의미는 그대로 둔다):

| 위치 | 무엇을 재나 |
|---|---|
| `StageTiming`(판정·검출·입력, 30초 `[tier0] stage_timing` 줄·status `stage_timing`·`detector_timing`·`input_timing`) | 단계별 wall·thread CPU 의 mean·p95·max, 사람 수 구간(0~6·7+·unknown)별 최근 256개 |
| 표시 파일 `gpu_priority.processing`(`ProcessingWindow`) | judge·detect 처리 시간 5초 창 p95·max(아바타 관문 입력, -547) |
| 표시 파일 `gpu_priority.load`(`LoadWindow`) | 보정 T_lag 와 QUEUE_WAIT_EXCEEDED(분석 워커 관문) |
| `I0Ingest` 로그 | 카메라별 촬영→추론 시작 frame_age p95·p99, queue_wait 최대(status `live_input.cameras`) |
| I1 health `pipeline_lag_ms` | 보정 T_lag(끝-끝) |
| `ChannelTiming`(아바타 공유) | write_crop·write_npz 등, 60초 로그 |

## 2. 계측 보강(기존 줄·표시 파일에 더한 것만)

| 더한 것 | 어디 | 비용 |
|---|---|---|
| `p50` | `StageTiming.summary` 의 wall_ms·thread_cpu_ms(mean·p95·max 는 그대로) | 정렬 결과 한 번 더 읽기 |
| `frame_receive` | `I0Bridge.on_frame` 진입 → 메시지 변환·클립 링 버퍼 끝(소켓 읽기·TLS 제외). 디코딩은 기존 `frame_receive_decode` | perf_counter 2회 |
| `i1_serialize.world_state`·`i1_send.world_state`(·`.other`) | `I1Client._send` 의 `json.dumps`·`conn.send` | perf_counter 2회 |
| `judge_wake_lag`·`detect_wake_lag`·`housekeeping_wake_lag` | 루프가 `Event.wait(delay)` 뒤 예정보다 늦게 깨어난 만큼(OS 스케줄링 + GIL 다시 잡기 대기 근사) | perf_counter 2회 |
| `thread_cpu` | 30초마다 `/proc/self/task/*/stat` 로 스레드 묶음별 CPU%·프로세스 CPU%(Linux. Windows 는 프로세스만) | 30초마다 파일 수십 개 |
| `frame_age` | 카메라별 촬영→추론 시작 frame_age p95·queue_wait 최대(기존 I0Ingest 표본을 30초 줄에도 싣기) | 없음 |
| `side_work` | 곁가지 스위치·자식 상태(3절) | 없음 |

나가는 곳: 기존 30초 `[tier0] stage_timing {json}` 줄에 `frame_age`·`i1`·`loops`·`thread_cpu`·`side_work` 키를 더했고, status(표시 파일
`state/tier0.live`)에 `i1_timing`·`loop_timing`·`thread_cpu`·`avatar_share.offload` 를 더했다. 한 프레임 묶음의 단계 순서는
`frame_receive` → `frame_receive_decode`(H.264) → `frame_dequeue`(대기 + BGR 변환) → `detect_trt`(검출+포즈) → `local_track_2d` →
`observation_poll` → `roi_filter` → `cross_view_2d_triangulation` → `world_track` → `contact` → `avatar_share` → `risk_evaluate` →
`i1_offer` → `journal_and_alert_submit`, 그리고 송신 스레드의 `i1_serialize.world_state` → `i1_send.world_state` 다. judge_tick 전체는
기존 `judge_tick`(사람 수 구간별)이다.

**I1 health 는 바꾸지 않았다.** health payload 는 열린 객체라 스키마상 넣을 수는 있지만 EC2·관제 화면이 읽지 않는 진단값을 1 Hz 로 싣는
것은 I1 계약 의미 변경이고(Lead 승인 대상), GPU 세션 판독은 Tier 0 로그·표시 파일로 충분하다. 계약 묶음 판을 올리지 않았다.

깨어남 지연은 Windows 에서 타이머 간격(약 15.6 ms) 때문에 크게 나온다. Linux(Pod) 값만 읽는다.

## 3. 곁가지 프로세스 분리(스위치 `offload_side_work`, 기본 끔)

### 고른 것: 아바타 입력 공유(조립 + 크롭 + NPZ 쓰기)

- 근거: 상시 도는 곁가지 중 유일하게 프레임 크기 자료를 만지고(크롭 1~3 MB, -650), 14차 문제(아바타 켬)와 직접 맞닿는다. 결과는
  아바타·PPE 가 읽는 파일이고 world_state·RiskEvent 에 들어가지 않는다.
- 노트북 실측(RTX 4050 노트북, Windows NTFS, 2304×1296 4대·7명, 탐침 스레드 1 ms 잠의 깨어남 지연으로 GIL 다툼 근사):
  `_write` p50 3.83 ms·p95 5.27 ms(write_prepare 1.15, write_npz 2.10, write_replace 1.57, write_publication 1.98 ms 중앙값),
  탐침 깨어남 최대 1.47 ms(빈 상태 0.91 ms). 즉 5 Hz × 약 4 ms 로 크지 않고 상당 부분이 GIL 을 놓는 파일 I/O 다. **기대 효과는 작다**:
  판정 스레드에서 `_share_avatar` 조립(판정 lock 안)이 빠지고 zip·JSON·파일 쓰기가 다른 프로세스로 간다. GPU 세션에서 켬/끔으로 잰다.
- 나눈 방식(-563 과 같은 틀): 판정 스레드는 그 tick 의 참조만 잡아 `offer_deferred`(잠금 비대기, 최신 한 건) → 부모 조정 스레드
  `tier0-side-work` 가 판정 lock 밖에서 문서 조립(꺼짐 경로와 같은 순수 함수 `avatar_metadata`)·크롭 준비(`_prepare`) → 크롭 바이트를
  고정 `shared_memory` 칸 2개 중 빈 칸에 복사 → 작은 메타를 유계 큐에 `put_nowait`(차면 버림·계수) → spawn 자식(최상위 순수 함수
  `_avatar_writer_main`)이 `_publish`(꺼짐 경로와 같은 함수·형식)로 NPZ·완료 표식을 원자적으로 쓴다. 원본 프레임은 넘기지 않는다
  (4대 36 MB 를 5 Hz 로 복사하면 그 복사가 원래 쓰기 비용만큼 든다).
- 함정 확인:
  - /dev/shm: 칸 하나 상한 16 MiB(넘으면 그 건만 버림·`oversize`), 만들 때 남은 공간 + 4 MiB 여유를 보고 Linux 는 `posix_fallocate`
    로 미리 잡는다(tmpfs 가 차면 SIGBUS 대신 ENOSPC 로 그 건만 버림·`shm_full`). 배경 누적(-563, 40 MB 시작 기준)과 합쳐도 도커 기본
    64 MB 에서 칸 2개(보통 각 1~4 MB)는 들어간다.
  - LatestChannel 단일 작성자: 켬이면 자식만 쓴다. 자식을 상한 넘게 다시 띄우게 되면 자식을 먼저 내린 뒤 부모 안 쓰기로 넘어간다.
  - calibration_version 혼입: 한 문서의 world·보정 문서·크롭은 같은 tick 에서 잡은 rig·그림 참조로 만든다. 자식은 `world.t` 가 이미 쓴
    것보다 이르면 쓰지 않는다(`out_of_order`).
  - spawn: 자식 진입점은 최상위 함수이고 모듈은 numpy·avatar_channel 만 불러온다. 자식은 torch·cv2 를 불러오지 않고(CUDA 문맥 없음)
    첫 응답에 torch 적재 여부를 싣는다(status `torch_in_child`, 시험에서 false 확인). `python -m services.tier0` 의 `__main__` 은 spawn 이
    다시 실행하지 않는다.
  - 시각·순서: 판정 tick 의 `world.t`·카메라별 `capture_time_ms`·`frame_ref`·offer 시각 `produced_at_ms` 를 그대로 싣는다.
  - msg_id == evidence_ref·I1 송신 순서: I1 경로를 건드리지 않았다(같은 결과 시험이 msg_id 까지 비교).
- 장애: 자식이 죽었거나 한 칸이 5초 넘게 안 돌아오면(멈춤) 자식을 내리고 칸·큐를 새로 만들어 1초 간격으로 다시 띄운다. 5번을 넘으면
  부모 안 쓰기(꺼짐 경로)로 넘어간다. 판정은 멈추지 않는다. status `avatar_share.offload` 에 `mode`·`alive`·`restarts`·계수.
- 켜는 법: 설정 파일 `"offload_side_work": true` 또는 환경 `SIMAI_TIER0_OFFLOAD_SIDE_WORK=1`(환경이 우선, 0 이면 끔).

### 고르지 않은 것

| 후보 | 이유 |
|---|---|
| I1 writer 직렬화 | 7명 world_state `json.dumps` 0.16 ms(p95 0.29 ms) × 10 Hz. 프로세스 경계를 넘기려면 pickle(0.07~0.10 ms)이 다시 들어 이득이 없고, 소켓·outbox·ACK·운영자 명령은 판정과 같은 프로세스에 있어야 송신 순서·msg_id(= evidence_ref)를 지킨다 |
| event_frames(JPEG·S3) | CRITICAL R1~R4 최초 RAISED 때만 돈다(`event_frames.py`). 평시 효과 0. JPEG·S3 는 GIL 을 놓는다 |
| Re-ID 크롭·COPE | 결과가 world_track `feature_lookup` 으로 판정 ID 에 들어간다. 다른 프로세스로 옮기면 도착 시각이 바뀌어 file 재생 speed=max 결정론이 깨진다. GPU 모델이 CUDA 문맥을 하나 더 만든다 |
| drift_watch·live_calibration·rotation_recovery | 결과가 카메라 제외·보정 채택으로 판정에 들어간다. 회전 복구 계산은 이미 subprocess |
| camera_check_tap | 세팅 요청이 있을 때만 돈다 |

## 4. 자원 격리

| 무엇 | 기본 | 되돌리는 법 | 근거 |
|---|---|---|---|
| Tier 0 앞쪽 코어 고정(`simai_bootstrap.tier0_cpus`) | auto: 사이드카(vLLM·워커·아바타·PPE)를 뒤쪽 n//2 에 묶었을 때만 같은 n(= min(affinity, cgroup 할당량 올림))의 앞쪽 n − n//2. 사이드카를 묶지 않으면 Tier 0 도 묶지 않음 | `SIMAI_TIER0_CPUS=none`. 직접 목록 `0-5`(사이드카와 겹치면 사용 오류) | 그동안 사이드카만 뒤쪽에 묶고 Tier 0 는 묶지 않아, Tier 0 스레드가 사이드카 코어로 흘러가 서로 캐시·코어를 다퉜다. 할당량 23.8 Pod 이면 사이드카 212-223, Tier 0 0-11 |
| cv2 스레드 1(`simai_ai.gpu.cv_threads`, Tier 0·아바타·워커 진입점) | 1 | `SIMAI_CV2_THREADS=keep`(cv2 기본값) 또는 숫자 | cv2 는 보이는 CPU 수만큼(Pod 224) 스레드를 띄워 resize·imencode·remap 때 판정 스레드의 CPU 할당량을 빼앗는다. 배경 누적·회전 복구 자식은 이미 1(-563) |
| 검출 고우선순위 CUDA 스트림(`yolo_detector`) | normal(끔) | 기본값 | ultralytics 8.4.178 TensorRT 백엔드는 정적 엔진이면 잡아 둔 CUDA 그래프를 `graph.replay()` 로 돌리고 replay 는 지금 스트림에 올라간다. 그래서 `torch.cuda.stream(우선순위 스트림)` 안에서 predict 하면 엔진 커널도 우선순위를 탄다. 동적·NMS 내장 엔진은 `execute_v2`(내부 동기)라 엔진은 타지 않는다. 노트북에 torch·ultralytics 가 없어 실제 효과를 확인하지 못해 기본은 끄고 GPU 대조에서 켠다(`SIMAI_TIER0_CUDA_STREAM_PRIORITY=high`) |

Tier 0 가 띄우는 자식(배경 누적·곁가지·회전 복구·엔진 빌드)은 Tier 0 의 CPU 고정을 물려받는다(사이드카 코어를 쓰지 않는다).

## 5. 같은 결과 시험

- `test_side_work_offload.py::test_replay_outputs_are_identical_with_switch_off_and_on_even_when_child_dies`: 합성 녹화(실제 H.264)를
  file 입력 최대 속도(결정론)로 아바타 공유를 켜고 두 번 돌린다. 끔 대 켬(켬은 첫 쓰기 뒤 자식을 죽임)에서 world_state 봉투(msg_id·sent_at·
  payload)와 risk_event 전이(event_id 만 순번으로 바꿈)가 한 줄도 다르지 않고, 켬 쪽은 다시 띄움 ≥ 1·자식 torch 없음, 두 경로 모두
  공유 채널 파일이 같은 형식으로 나왔다. world_state·risk_event JSONL 은 I1 과 같은 봉투(Envelope)다(file 입력은 I1 을 열지 않는다).
- 같은 파일: 같은 문서·그림이면 분리 경로가 쓴 NPZ 의 메타·크롭 배열이 꺼짐 경로와 같다.
- 장애: 자식 죽임 → 다시 띄우고 계속 씀(호출자는 100건 넣는 데 0.5 s 미만), 멈춤 → 다시 띄움, 상한 넘음 → 부모 안 쓰기로 넘어감,
  칸 상한 넘는 건은 버리고 셈.

## 6. GPU 세션 비교 절차(14차 녹화 `live-20261112-135633`)

같은 커밋·같은 이미지·같은 보정(14차 세팅 보정과 묶음)에서 스위치만 바꿔 돈다. -650(아바타 부하 재설계) 효과와 섞이지 않게 모든 칸이
-650 이 들어간 같은 커밋이다. 녹화는 게이트웨이 재생(14차와 같은 방식, I0 LIVE 경로: 수신·디코딩·검출이 모두 돈다)으로 넣고, 아바타·PPE
서비스는 14차처럼 켠다. 칸마다 N = 10분(5명 구간이 들어가게 녹화 같은 구간), Tier 0 재시작 뒤 1분은 버린다. GPU 는 한 번의 큐로 묶는다.

| 칸 | `SIMAI_TIER0_OFFLOAD_SIDE_WORK` | `SIMAI_TIER0_CPUS` | `SIMAI_CV2_THREADS` | `SIMAI_TIER0_CUDA_STREAM_PRIORITY` |
|---|---|---|---|---|
| A 기준 | 0 | none | keep | normal |
| B 곁가지만 | 1 | none | keep | normal |
| C 자원 격리만 | 0 | auto | 1 | normal |
| D 둘 다 | 1 | auto | 1 | normal |
| E D + 우선순위 스트림 | 1 | auto | 1 | high |

모을 것: Tier 0 로그의 30초 `[tier0] stage_timing` 줄 전부, 30초마다 표시 파일 사본, 아바타 서비스 종료 요약(`AvatarLoop stopped`),
PPE 로그, `top -H`/`pidstat -t` 1분 표본(교차 확인).

| 지표 | 읽는 곳 |
|---|---|
| 인원수별 judge_tick p50·p95 | `stage_timing.judge.judge_tick.<사람 수>.wall_ms` |
| 단계별 p50·p95 | `stage_timing.input`(frame_receive·frame_receive_decode), `detector`(frame_dequeue·detect_trt·local_track_2d), `judge`(cross_view·world_track·avatar_share·risk_evaluate·i1_offer), `i1`(i1_serialize·i1_send) |
| GIL 대기 근사 | `stage_timing.loops.*_wake_lag` p95 |
| CPU% | `stage_timing.thread_cpu.process_pct`, 스레드 묶음별(`tier0-judge`·`tier0-detect`·`i0-video-conn`·`tier0-side-work`·`native:*`) |
| 프레임 나이 | `stage_timing.frame_age.<cam>.frame_age_ms_p95` |
| 아바타 접수율 | 아바타 관문 열린 시간 비율, `resets.over_budget`, poses_sent·dropped_poses |
| PPE 관문 차단 수 | PPE 로그의 Tier 0 지연 차단 횟수 |
| 곁가지 상태 | `stage_timing.side_work.avatar`(restarts·dropped·oversize·shm_full·torch_in_child) |

인원수별 judge_tick p95 표(칸마다 채운다, 단위 ms):

| 사람 수 | A | B | C | D | E |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |
| 6 | | | | | |
| 7+ | | | | | |

기본값 결정 기준:
- 곁가지 분리: B 가 A 보다 judge_tick p95(3~5명 구간)를 5% 넘게 줄이거나 `avatar_share` 구간 p95 를 없애고, 다시 띄움 0·`shm_full` 0·
  아바타 접수율이 같거나 높으면 기본 켬으로 바꾼다. 차이가 측정 잡음(창 사이 표준편차) 안이면 기본 끔으로 둔다(복잡도만 늘기 때문).
- CPU 고정·cv2: C 가 A 보다 judge·detect p95 와 `*_wake_lag` p95 를 줄이거나 같고 frame_age p95 가 나빠지지 않으면 그대로 둔다(기본
  켬). detect_trt·frame_dequeue 가 10% 넘게 늘면(cv2 1 로 letterbox resize 가 느려진 경우) `SIMAI_CV2_THREADS=2` 로 다시 잰다.
- 우선순위 스트림: E 가 D 보다 아바타·PPE 를 켠 상태의 detect_trt p95 를 줄이면 기본 high 로 바꾼다.
- 어느 칸도 judge_tick p95 를 3명 100 ms 아래로 못 내리면 판정 중심 최적화 후속 티켓을 먼저 한다(예상).

## 7. 명세에 없어 정한 수치

| 항목 | 값 | 근거 |
|---|---|---|
| 공유 메모리 칸 수·칸 상한 | 2개·16 MiB | 자식이 한 건 쓰는 동안 다음 한 건을 준비. 크롭 4대 1~3 MB(-650 목표), 사람이 화면을 채워도 여유 |
| /dev/shm 여유 | 4 MiB | -563 과 같은 값(공유 채널 NPZ·임시 파일 몫) |
| 멈춤 판정·다시 띄우기 간격·상한 | 5 s·1 s·5번 | 쓰기 한 건은 수 ms. 5 s 는 그 1000배라 오판이 없다. 5번 넘게 죽으면 원인이 고쳐질 때까지 부모 안 쓰기가 낫다(아바타 시연 필수) |
| Tier 0 코어 몫 | 같은 n 의 n − n//2 | 사이드카 n//2 의 나머지. 할당량 안에서 둘이 겹치지 않는다 |
| cv2 스레드 | 1 | -563 자식과 같다. 대조에서 detect 가 느려지면 2 |
| 스레드 CPU 표 행 수 | 32 | 로그 한 줄 크기 제한 |

## 8. 버린 대안

- **Tier 0 전체 Rust 포팅**: 판정 중심의 초선형 루프가 원인이면 알고리즘(메모이제이션·벡터화)으로 먼저 줄일 수 있고, 시연까지 남은 시간에
  판정 정본 전체를 다시 쓰고 검증할 수 없다.
- **판정 중심 프로세스 분리**: 검출 결과 → cross_view → world_track → 판정 → world_state 는 같은 프로세스가 정본이다(규칙 1). 나누면 관측
  전달 직렬화·순서·복구가 판정 경로에 들어온다. 대신 판정 중심 최적화를 후속 티켓으로 한다.
- **GPU 디코딩(NVDEC) 재시도**: Pod 실측 CPU 디코딩이 NVDEC 보다 3.5배 빨랐고 병목은 색 변환 문맥이었다(`tier0-nvdec-throughput.md`).
  디코딩은 GIL 을 놓아 판정 지연의 주범이 아니다.
- **I1 직렬화 분리**, **Re-ID 분리**, **event_frames 분리**: 3절 표.
- **원본 프레임을 공유 메모리로 넘겨 크롭까지 자식에서**: 4대 36 MB × 5 Hz 복사가 원래 쓰기 비용(약 4 ms)과 같아 이득이 없다.
- **새 30초 `stage_latency` 줄**: 처음 만들었다가 Lead 지시로 접고 기존 `stage_timing` 줄에 키만 더했다(판독 도구가 한 줄만 읽게).

## 9. 남은 위험·넘긴 일

- 곁가지 분리의 효과는 작을 수 있다(3절 실측). 판정 지연의 대부분은 판정 중심이다 → 후속 티켓(Lead).
- 켬 경로에서도 크롭 상자 계산·크롭 복사·메타 조립은 부모 프로세스(판정 lock 밖 조정 스레드)에 남는다. 메타 pickle 도 부모의 큐
  feeder 스레드가 한다.
- 자식의 완료 표식 `published_at_ms` 는 자식의 벽시계다(Tier 0 주입 시계가 아니다). 운영에서는 같은 시계다.
- 우선순위 CUDA 스트림은 GPU 에서 확인하지 못했다. 동적 배치 엔진이면 엔진 커널은 우선순위를 타지 않는다(`execute_v2`).
- CPU 고정은 사이드카를 묶을 때만 켜진다(8 vCPU 미만 Pod 는 그대로). 고정 뒤 Tier 0 의 할당량이 이전보다 작아질 수 있다(할당량 24 →
  Tier 0 12 코어). 대조 C 에서 frame_age·detect 를 본다.
- 발견(이 티켓에서 고치지 않음): `i1-publisher` 가 100 Hz tick 마다 `_calibration_messages` 에서 판정 lock 을 잡는다(비공정 RLock).
  판정 tick 사이 틈에 끼어 judge 를 늦출 수 있다. 다음 판정 중심 티켓에서 커서를 lock 밖으로 옮기는 것을 검토한다.
- 노트북 실측은 Windows NTFS 기준이다. Pod(/dev/shm tmpfs)에서는 파일 쓰기 비용이 더 작아 분리 효과가 더 작을 수 있다.
