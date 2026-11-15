# 셀프서비스 LIVE 의 Tier 0 런타임 기본값 점검 (S15P31A101-727)

셀프서비스 자동 부팅(GPU 켜기 → `simai-gpu-boot`)으로 뜬 Tier 0 가 실제로 어떤 디코더·곁가지 분리·CUDA 스트림 우선순위·CPU 고정·보조
스레드로 도는지 코드로 따라가 표로 남기고, 근거가 있는 것만 시연 LIVE 기본값을 바꾼다. 외부 검토(carbon 3절 1번 "운영 스위치 즉시
최적화": NVDEC·`offload_side_work`·고우선순위 스트림·보조 스레드 끄기)와 그 반론(astra: 디코더 교체는 과거 CPU 가 더 빨랐음)을 함께
본다. **결론: 기본값 변경 없음.** 이유는 3절. 판정 경로 코드는 -726(같은 결과 최적화)·-728(프로세스 분리)이 맡는다.

같은 티켓에서 `docs/design/S15P31A101-717-avatar-gate-judge-p95.md` 의 예산 설명과 평균 분해 수치를 정정했다(4절).

## 관련 명세 절

| 절 | 이 문서에서 |
|---|---|
| 3.2 | Tier 0 가 위치·판정 정본, 자원 우선. 보조 스레드는 판정 정본을 덮지 않는다 |
| 3.12 | 앞·뒤 CPU 코어 격리(`SIMAI_TIER0_CPUS=auto`/`SIMAI_SIDECAR_CPUS=auto`), `SIMAI_CV2_THREADS=1`, 최소 vCPU 8 |
| 5.1 | 기본 CPU 디코딩, NVDEC 은 선택(v1.3 수정 26) |
| 6.6 | capture_to_risk p95 목표 600 ms, 구간 p95 합을 전체 p95 로 해석하지 않음, 단계별 계측 |

## 1. 값이 정해지는 경로

1. `deploy/scripts/simai_gpu_boot.py` — Pod 환경은 RunPod 템플릿 env + 서버 `plainEnv`(-702 표: 부팅 비밀·EC2 주소·공급자·가격 등)뿐이다.
   Tier 0 런타임 스위치(`SIMAI_TIER0_OFFLOAD_SIDE_WORK`·`SIMAI_TIER0_CUDA_STREAM_PRIORITY`·`SIMAI_TIER0_CPUS`·`SIMAI_TIER0_HUMAN_CALIBRATION`
   등)는 어느 쪽에도 없다. `lobby_argv` 는 `--camera-ids`·`--measured-heights`·`--scene-version`·`--config-version`·포트·`--services`
   (80GB급 `tier0,vllm,worker,avatar,ppe`, -718)만 넘기고 `--no-live-calibration`·`--no-clean-background` 는 넘기지 않는다.
2. `deploy/scripts/simai_bootstrap.py` `lobby` → `prepare_lobby_work` 가 `tier0-live.json` 을 쓴다:
   `input = {mode: live, decoder: "cpu", max_queue_wait_ms: 150, queue_depth: 1}`, `tick_ms: 100`,
   `live_calibration = {enabled: true, interval_ms: 60000, timeout_s: 120, buffer_duration_ms: 120000, measured_camera_heights, safety_enabled: true}`,
   `clean_background = {enabled: true}`(`SIMAI_CLEAN_BACKGROUND` 기본 1).
3. `Supervisor` 가 Tier 0 환경에 `SIMAI_AVATAR_SHARE=1`(아바타 또는 PPE 가 있으면)·`SIMAI_PPE_RELAY=1`(PPE)·수치 연산 스레드
   `SIMAI_TIER0_BLAS_THREADS`(기본 1)를 넣고, `tier0_cpus(auto)` 결과로 Tier 0 프로세스 그룹을 묶는다(자식이 물려받음).
4. `services.tier0.__main__` 이 cv2 스레드를 1 로(`SIMAI_CV2_THREADS` 기본), `services/tier0/settings.py` `load_settings` 가 나머지를 기본값으로 채운다.

확인: 위 `tier0-live.json` 꼴을 빈 환경으로 `load_settings` 에 넣으면 decoder cpu·대기 150·깊이 1, `offload_side_work=False`,
`cuda_stream_priority=normal`, `human_calibration_enabled=False`, clean_background 켬(offload 자식, 표본 5 s·세팅 1 s), rotation_recovery
켬(15 s·CPU 500 ms), event_frames·camera_check 켬, reid 켬(상한 8 crop/s), roi_filter `min_people=4` 가 나온다(아래 "검증").

## 2. Tier 0 가 실제로 받는 값

"17차"는 17차 GPU 세션 Pod3(H100 SXM, 셀프서비스 부팅 `lobby-gpu-20261115T…`, 14차 녹화 재생 2~3명) `tier0.log` 의 07:20:43
`[tier0] stage_timing` 줄(30.1 s 창)이다.

### 운영 스위치

| 항목 | 실제 값 | 정해지는 곳 | 17차 로그 |
|---|---|---|---|
| `input.decoder` | `cpu` | lobby 가 명시(`settings.DEFAULT_DECODER` 도 cpu) | `frame_receive_decode` 벽 p50 5.4·p95 21.2 ms(스레드 CPU 1.6·8.9), 카메라 `frame_age` p95 117~118 ms |
| BGR 변환 | 검출 스레드 `frame_dequeue` 에서(늦춘 변환, -386) | 코드 경로 | 벽 p50 9.3~11.1·p95 15~25 ms, CPU 약 4 ms |
| `offload_side_work` | `false` | 설정 기본 false, 환경 변수 없음 | `side_work.offload=false`, `avatar-ipc` 스레드 3.7%, 판정 안 `avatar_share` p95 0.06~0.07 ms |
| `cuda_stream_priority` | `normal` | 환경 변수 기본 | — |
| Tier 0 CPU 고정 | `auto`: 쓸 수 있는 vCPU n = min(affinity, cgroup 할당량 올림) 이 8 이상이면 앞쪽 n − n//2 개, 아니면 안 묶음 | `SIMAI_TIER0_CPUS` 없음 → auto | 범위 로그는 부팅 표준 오류에만 있고 보관하지 않았다. 프로세스 CPU 327% |
| 사이드카 CPU 고정 | `auto`: 같은 n 이 8 이상이면 뒤쪽 n//2 개(vLLM·워커·아바타·PPE) | `SIMAI_SIDECAR_CPUS` 없음 → auto | 위와 같음 |
| cv2 스레드 | 1 | `SIMAI_CV2_THREADS` 기본 | `cv2 스레드: 1` |
| BLAS·OMP 스레드 | 1 | `SIMAI_TIER0_BLAS_THREADS` 기본 | — |
| 사람 관절 보정 | 꺼짐 | `SIMAI_TIER0_HUMAN_CALIBRATION` 기본 0 | `사람 관절 보정 꺼짐` |
| 아바타 입력 공유 | 켬 | Supervisor `SIMAI_AVATAR_SHARE=1` | `avatar-ipc` 3.7% |

### 판정 프로세스 안 보조 스레드와 주기

| 스레드 | 부팅 상태 | 조건 | 주기 | 17차 스레드 CPU |
|---|---|---|---|---|
| `tier0-live-calibration` | **꺼짐** | `human_calibration_enabled and live_calibration.enabled`. tier0-live.json 의 `enabled: true` 는 사람 관절 보정이 꺼져 있어 효과가 없다 | 60 s | 없음 |
| `tier0-drift-watch` | **꺼짐** | `human_calibration_enabled and drift_watch_enabled` | 1 Hz 표본 | 없음 |
| `tier0-clean-background` | 켬 | `clean_background.enabled` + 검출 입력. 계산은 spawn 자식(-563) | 표본 5 s, 세팅 배경 촬영 요청 중 1 s | 3.4% |
| `tier0-rotation-recovery` | 켬 | clean_background 가 있고 `rotation_recovery.enabled`. 계산은 subprocess | 15 s(CPU 상한 500 ms) | 0.5% |
| `tier0-event-frames`·`tier0-event-s3` | 켬 | LIVE 입력 + I1 | CRITICAL R1~R4 최초 RAISED 때만 | 0.3·0.4% |
| `tier0-camera-check-tap` | 켬 | 검출 입력. 워커 요청 파일이 있을 때만 씀 | 요청 때만 | 0.0% |
| `tier0-reid-crop`·`tier0-reid` | 설정 켬, COPE 묶음이 있을 때만 실제로 돈다 | 17차는 `/srv/simai/reid/cope` 없음 → 비활성 | 상한 8 crop/s, 갱신 750 ms | 0.3%(크롭 스레드만) |
| `avatar-ipc` | 켬 | `SIMAI_AVATAR_SHARE=1` | 5 Hz | 3.7% |
| `tier0-housekeeping` | 켬 | 늘 | 10 Hz 깨어남, 표시 파일 5 s, 계측 줄 30 s | 3.7% |
| `i1-publisher`·`i1-writer` | 켬 | I1 | 100 Hz tick·메시지마다 | 2.5·2.1% |
| `tier0-checkpointer`·`event-journal-writer` | 켬 | 늘 | 1 s·이벤트마다 | 2.0·0.9% |
| (판정 중심) `tier0-detect`·`tier0-judge`·`i0-video-conn`(4) | 켬 | 늘 | 20 Hz·10 Hz·100 fps 합 | 50.9·45.0·30.1% |
| (C 스레드) PyAV·swscale·TensorRT·CUDA | — | — | — | `native:python` 179.6% |

보조 스레드(위 표의 판정 중심·native 를 뺀 것) 합은 약 20% 이고, 그중 판정 정본에 결과가 들어가지 않는 일(배경 표본·이벤트 프레임·
카메라 확인·아바타 공유)은 약 8% 다. 판정 스레드 45%·검출 51%·영상 수신 30% 에 비하면 작다.

## 3. 결정과 근거

**바꾼 기본값 없음.** 항목별로 근거를 본 결과다.

| 항목 | 결정 | 근거 |
|---|---|---|
| `input.decoder` | **cpu 유지**(NVDEC 안 씀) | (1) 2차 GPU 세션 H100 c3: CPU 카메라별 스레드 그림 p95 3.05 ms·합계 1,575.7 fps, NVDEC 카메라별 11.94 ms·214.7 fps. NVDEC 은 BGR 변환 없이 nv12 를 내려받기만 해도 4.16 ms 로 CPU 의 디코딩+변환 전체(2.01 ms p50)보다 길다(`tier0-nvdec-throughput.md`). (2) NVDEC 출력 nv12 → BGR 은 여전히 CPU swscale 이고 nv12 는 직행 SIMD 경로가 없어 yuv420p 보다 비싸다(노트북 문맥 재사용 기준 NVDEC 6.49 vs CPU 1.75 ms). 17차의 BGR 변환은 검출 스레드 `frame_dequeue`(CPU 약 4 ms)에 있으므로 NVDEC 으로 바꾸면 검출 스레드 몫이 오히려 늘 것으로 본다(추정). (3) PyAV 디코딩·swscale 은 GIL 을 놓는다. `i0-video-conn` 30% 는 디코딩 C 시간을 포함한 스레드 CPU 라 GIL 몫과 같지 않다. carbon 의 "NVDEC 만으로 영상 수신 CPU 31% 상당 부분 절감"은 잰 값과 반대이고, astra 는 같은 근거로 디코더 교체를 하순위로 봤다. (4) 17차 디코딩 p95 21 ms 는 카메라 프레임 간격 40 ms 안이다 |
| `offload_side_work` | **false 유지** | -662 기본값 기준(B 가 A 보다 3~5명 judge p95 를 5% 넘게 줄일 때 켬)을 잰 적이 없다. 17차 판정 경로의 `avatar_share` 는 p95 0.07 ms 이고(-650 뒤 크롭 쓰기는 이미 `avatar-ipc` 스레드), 켬이 다른 프로세스로 옮기는 것은 그 스레드(3.7%)의 NPZ·파일 쓰기 부분뿐이다(크롭 준비·복사는 부모에 남음). 기대 효과가 판정 p95 의 창 간 흔들림보다 작다. 켜도 깨지는 기능은 없다(같은 결과 시험) — 근거 없는 변경을 하지 않을 뿐이다. -728 이 프로세스 경계를 바꾸므로 A/B 는 그 뒤에 한다 |
| `cuda_stream_priority` | **normal 유지** | 부팅 엔진은 `dynamic=True`·`e2e`(NMS 내장, `…_e2e_i1280_b4_…engine`)라 ultralytics 가 `execute_v2` 로 돌리고 엔진 커널은 우선순위 스트림을 타지 않는다(`yolo_detector.py` 설명, -662). 바뀌는 것은 전·후처리뿐이다. 17차 `detect_trt` 벽/CPU p50 58/33 ms 의 차이가 GPU 대기인지 GIL 대기인지 가르지 못했고 GPU 대조도 없다 |
| Tier 0·사이드카 CPU 고정 | **auto 유지** | 17차 Tier 0 프로세스 327%(30 s 평균)로, 할당량 23.8 Pod 기준 앞쪽 12 코어 몫(-662) 안이다. 묶음을 풀 근거(스로틀·`*_wake_lag` 악화)가 없다. 17차 `judge_wake_lag` p95 5.2 ms·`detect_wake_lag` p95 4.2 ms |
| live_calibration·drift_watch | **이미 꺼짐, 그대로** | 부팅은 `SIMAI_TIER0_HUMAN_CALIBRATION` 을 주지 않아 두 스레드가 뜨지 않는다. carbon 의 "live_calibration 끄기"는 이미 그 상태다. tier0-live.json 의 `live_calibration.enabled: true` 를 false 로 바꿔도 동작은 같아 건드리지 않았다(손 lobby 에서 사람 관절 보정을 켤 때 쓰는 값) |
| clean_background | **켬 유지** | 끄면 깨진다: (a) 세팅 1단계 배경 촬영(-686: 워커가 `clean_background/regenerate.request.json` 을 쓰고 Tier 0 가 새 표본만으로 묶음을 만든다. 없으면 `BACKGROUND_INCOMPLETE`), (b) 관제 뒤 배경 복원(`after-live`·`auto_static` 이 `state/clean-background` 묶음을 올린다), (c) 회전 복구(`service.py` 는 clean_background 가 있어야 rotation_recovery 를 만든다). 비용은 스레드 3.4%이고 계산은 자식 프로세스(-563) |
| rotation_recovery | **켬 유지** | 끄면 카메라가 돌아갔을 때 자동 복구·`REQUIRES_SETUP` 기록·틀어짐 카메라 제외가 없다(11차 무보행 회전 보정). 15 s 주기, 계산은 subprocess, 스레드 0.5% |
| event_frames | **켬 유지** | 평시 0(R1~R4 최초 RAISED 때만). 끄면 VLM 근거·사건 프레임이 없다 |
| camera_check_tap | **켬 유지** | 세팅 「카메라 확인」 입력. 요청이 없으면 쓰지 않는다(0.0%) |
| Re-ID | 이 티켓에서 안 다룸 | 결과가 world_track `feature_lookup` 으로 판정 ID 에 들어가 운영 스위치가 아니다(-662 표). 17차는 COPE 묶음이 없어 꺼져 있었다(아래 남은 위험) |
| ROI `min_people` 4 | 이 티켓에서 안 다룸 | carbon 은 4 → 2 를 제안했지만 판정 경로 값이고(-574: 낮추면 R5 접근 경보가 늦음) 운영 스위치가 아니다 |

## 4. -717 문서 정정

`docs/design/S15P31A101-717-avatar-gate-judge-p95.md` 를 고쳤다(코드 변경 없음).

1. **"구간 p95 를 더한 값은 전체 p95 보다 크므로 보수적"은 틀림.** 분위수는 더하기에 준가법이 아니어서 구간 p95 합이 전체 p95 의
   상한이 아니다(반례: 독립 두 구간이 각각 96% 0 ms·4% 100 ms 면 구간 p95 합 0, 전체 p95 100 ms). 명세 6.6 은 구간 p95 합을 전체
   p95 로 읽지 않고 끝에서 끝까지 직접 잰다. 200 ms 는 p95 목표를 나눈 배분이고 보장이 아니라고 고쳤다. 식에 업로드·디코딩·추론 대기·
   I1 송신이 빠져 있고 `fusion_max_lag_ms` 는 구간 시간이 아니라 카메라 간 시각 차 한도라는 것도 적었다.
2. **600 ms 는 상한이 아니라 p95 목표**(6.6 "초기 목표")로 바꿨다(관련 명세 표·정한 수치 표·3절).
3. **perception 56.8 안에 50.3 + 10.2 = 60.5 가 들어간 수치 불일치.** 같은 07:20:43 줄을 다시 읽었다. 단계마다 (단계, 인원) 칸의 최근
   256 표본을 따로 요약하고, `judge_tick`·`perception` 은 새 융합 시각이 없어 일찍 끝난 틱도 직전 인원 칸에 넣는다(교차 시점·world_track
   칸에는 안 들어감). 07:20:13 → 07:20:43 누계 증가가 perception 2명 +271, 교차 시점 +254 라 perception 창의 약 6% 가 빈 틱이다. 빼고
   환산하면 인지 틱 perception ≈ 60.6 ms 로 교차 시점 50.3 + world_track 10.2 + 기타 0.3 = 60.8 과 맞고, judge_tick ≈ 72.7 ms 다.
   평균만 환산하고 p95 는 칸끼리 더하지 않는다고 적었다.

## 5. 다음 GPU 세션에서 볼 것

1. 부팅 표준 오류(컨테이너 로그)의 `CPU 묶기(...)` 한 줄을 보관한다(17차는 남기지 않아 실제 Tier 0·사이드카 코어 범위를 모른다). c0 의
   `usable_vcpu` 와 함께 본다. 8~11 vCPU 사이트면 Tier 0 몫이 4~6 코어라 `frame_age`·`*_wake_lag` 를 따로 본다.
2. -728 뒤 `SIMAI_TIER0_OFFLOAD_SIDE_WORK=1` A/B(-662 6절 칸 B), `SIMAI_TIER0_CUDA_STREAM_PRIORITY=high` A/B(칸 E). 아바타·PPE 를 켠
   상태의 `detect_trt` p95 와 끝단 지연으로 판정한다.
3. Re-ID 묶음이 캐시에 있으면 Re-ID 가 켜진다. 17차는 꺼진 상태로 잰 값이라 켠 상태의 판정·검출 p95 를 따로 남긴다.

## 검증

- 1절 꼴의 `tier0-live.json` 을 이 브랜치의 `load_settings` 에 빈 환경(`SIMAI_ROOT` 만)으로 넣어 2절 값을 확인했다(노트북 임시 스크립트).
- `ai/tests/deploy/test_simai_supervisor.py -k "lobby or cpu"`(lobby 설정 파일·Tier 0/사이드카 CPU 고정 규칙) 47 통과. 코드·설정은 바꾸지 않았다.
- 17차 수치는 `session17/pod3/srv/simai/logs/tier0.log`(노트북, 저장소 밖)의 07:15~07:21 `stage_timing` 줄에서 읽었다.

## 남은 위험

- 17차 값은 녹화 재생(촬영 시각 어긋남 188~342 ms, -716·-721) 입력이라 교차 시점 비용이 부풀었을 수 있다. 운영 스위치 결정(NVDEC·
  보조 스레드)은 그 교란과 무관한 디코딩·스레드 CPU 값으로 했다.
- NVDEC 이 검출 스레드 몫을 늘린다는 것은 nv12 변환 비용에서 끌어낸 추정이다. NVDEC 은 선택 경로로 남아 있어(`input.decoder: nvdec`)
  필요하면 GPU 세션에서 직접 비교할 수 있다.
- 사람 관절 보정을 켜는 손 lobby 에서는 live_calibration(60 s, 돌 때 무거움)·drift_watch 가 함께 뜬다. 셀프서비스 부팅은 해당 없음.
