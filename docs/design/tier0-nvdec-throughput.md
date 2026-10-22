# Tier 0 디코딩 처리량과 기본 디코더 (c3)

S15P31A101-212(NVDEC 처리량 원인·색 변환 문맥), S15P31A101-236(기본 디코더 CPU, c3 판정 CPU 카메라별 스레드, 아래 '결론'). 기획서 §5.1(디코딩과 추론 분리, 기본 CPU 디코딩·NVDEC 선택), §3.2 표(Tier 0 = 엔진, NVDEC 을 고를 때만 하드웨어 디코더 4개 추가), §6.6(지연 예산), §12.7(I0 압축 프레임), 요구사항 FR-48·FR-50(2304×1296 25 fps H.264 4대) 기준이다.
코드는 `ai/src/simai_ai/io/pyav_decoder.py`, 측정은 `deploy/scripts/session1_live.py` 의 c3(`bench_decoders`·`c3_decode_throughput`), 시험은 `ai/tests/io/test_pyav_decoder.py`·`ai/tests/deploy/test_session1_c3.py` 다.

## 문제

GPU 1차 세션(2026-10-21, H100 80GB, 드라이버 595.91.07, CUDA 13.2, 이미지 torch 2.14.1+cu130, PyAV 18.1.0 lock)에서 c3 이 두 번 다 실패했다.

| Run | NVDEC 그림 p50 / p95 | NVDEC 합계 | CPU 그림 p50 / p95 | CPU 합계 |
|---|---|---|---|---|
| 20261021(첫 실행) | - / 10.552 ms | 86.6 fps | - | 360 fps |
| 20261021b | 10.001 / 10.565 ms | 86.1 fps | 3.274 / 4.026 ms | 305.1 fps |

같은 Pod(CPU 224개 보임)에서 CPU 디코딩이 NVDEC 보다 3.5배 빨랐다. c4 LIVE 기동(decoder nvdec, 카메라별 스레드)은 통과했다(ready 4/4, decode_failed 0, frame_age p95 82 ms, pipeline_lag p95 144 ms).

## c3 측정 방식 (확인)

- 입력: `live_video.encode` 프레임 번호 무늬 H.264, 2304×1296, 25 fps, 10초 = 250 프레임, GOP 50, B 프레임 없음. 4대가 같은 스트림을 쓴다.
- 순서: 디코더 4개를 한 스레드에서 번갈아(프레임마다 cam1→cam4). 디코더 열기(configure)는 시간 밖, 첫 프레임은 시간 안(1000 장 중 4 장이라 p95 에 영향 거의 없음).
- 그림 한 장 시간 = `decode()` 한 번: 패킷 보내기·받기(`avcodec_send_packet`/`receive_frame`), hwaccel 그림의 GPU→CPU 내려받기(PyAV `_transfer_hwframe` 의 `av_hwframe_transfer_data`, nv12), nv12→bgr24 swscale 변환, numpy 배열(bgr24 는 한 평면이라 복사 없는 view). LIVE 의 `DecodedPicture` 와 같은 내용이다.
- 기준(≤ 10 ms, ≥ 100 fps)의 출처는 `docs/design/runpod-environment.md` c3 행(-183 설계)이고 명세에는 디코딩 구간 예산이 없다(§6.6 은 capture_to_risk p95 600 ms 등 끝-끝만). 100 fps 는 LIVE 합계(4대 × 25 fps)와 같고, 한 스레드 ≤ 10 ms 는 "스레드 하나가 4대를 다 맡아도 따라간다"는 보수적 조건이다. LIVE 는 카메라마다 수신·디코딩 스레드가 따로라(`VideoIngestServer`·`CameraIngest`, `tier0-service.md`) 카메라 하나는 한 프레임 간격(40 ms) 안에만 끝내면 밀리지 않는다. 그래서 c3 실패와 c4 통과가 함께 나왔다.

## 원인

**확인됨(노트북 RTX 4050 Laptop, 드라이버 591.44, 같은 PyAV 18.1.0·FFmpeg 8.1.2 휠).** 병목은 NVDEC 디코딩이 아니라 PyAV 의 색 변환 문맥이다.

1. `VideoFrame.to_ndarray(format=...)` → `VideoFrame.reformat` 은 변환 문맥(`VideoReformatter`, 안에 `SwsContext`)을 **그림 객체마다** 만든다(`if not self.reformatter: self.reformatter = VideoReformatter()`). 디코더가 내는 그림은 매번 새 객체라 swscale 초기화(필터 계수·변환 그래프)와 슬라이스 스레드 묶음 생성을 그림마다 치른다.
2. PyAV 가 넘기는 `threads` 기본값은 0 이고 FFmpeg 은 0 을 "보이는 CPU 수"로 푼다. 노트북 22, Pod 는 224 다.
3. NVDEC 출력은 nv12 다(NVDEC 의 8비트 4:2:0 출력 형식). swscale 의 x86 직행 경로(yuv420p→bgr24 SIMD)는 nv12 에 없어 일반 경로를 타므로 초기화·변환 모두 yuv420p 보다 크다. 노트북에서 CPU 로 만든 nv12 그림도 똑같이 느려(→bgr24 약 18 ms) GPU 메모리 문제가 아님을 확인했다.

노트북 분해(디코더 1개, 2304×1296, 150 프레임, ms p50):

| 경우 | 디코딩(+내려받기) | 변환 | 합 |
|---|---|---|---|
| CPU, 변환 없음 | 0.21 | - | 0.21 |
| CPU + bgr24 (지금까지 방식) | 0.30 | 2.58 | 2.89 |
| NVDEC, 내려받기 없음(`is_hw_owned`) | 1.84 | - | 1.84 |
| NVDEC + 내려받기(nv12) | 3.44 | 0.21 | 3.64 |
| NVDEC + bgr24 (지금까지 방식) | 3.84 | 15.94 | 19.97 |
| NVDEC, thread_count=1 / thread_type NONE | 3.19 / 3.16 | - | 차이 없음 |

새 그림마다 변환(실제 디코딩 순서, 100 프레임 × 3회, 디코딩+변환 ms p50 / p95):

| 변환 | NVDEC | CPU |
|---|---|---|
| 그림마다 새 문맥(지금까지) | 21.16 / 26.75 | 6.20 / 8.38 |
| 디코더마다 문맥 하나, threads 0(자동) | 6.49 / 7.48 | 1.75 / 2.15 |
| 문맥 하나, threads 1 | 11.92 / 18.61 | 3.36 / 4.99 |
| 문맥 하나, threads 2 | 8.64 / 12.27 | 2.27 / 3.21 |
| 문맥 하나, threads 4 | 6.84 / 8.72 | 2.12 / 2.45 |

**추정(Pod).** 이미지가 같은 lock(PyAV 18.1.0)이라 같은 경로를 탄다. Pod 는 자동 스레드 수가 224 라 그림마다 만드는 스레드 묶음이 더 크다. Pod 의 CPU 경로(yuv420p)도 같은 비용을 치렀지만 직행 경로라 덜 드러났다. NVDEC 디코딩·내려받기 자체 시간은 Pod 에서 아직 모른다(아래 `nvdec_nv12` 변형으로 잰다).

## 고친 것

- `PyAvDecoder` 가 configure 마다 `VideoReformatter` 하나를 만들고 모든 그림을 그것으로 변환한다(`reformat(frame, format=output_format, threads=convert_threads).to_ndarray()`). 출력 그림은 변환마다 새로 할당되므로 대기열에 남은 이전 그림이 바뀌지 않는다. 크기가 스트림 안에서 바뀌어도 swscale 이 다시 설정한다(시험). 디코더는 카메라마다 따로이고 카메라별 lock 아래에서 불리므로 문맥을 나눠 쓰지 않는다.
- `convert_threads` 기본 4. 노트북에서 4 ≈ 자동(0)이고, 0 은 Pod 에서 카메라마다 224 스레드(4대면 약 900)가 되어 CPU 할당량(cgroup)과 다툴 수 있어 묶었다. 인터페이스(`Decoder`, 열쇠 묶기, `DecodedPicture` BGR)는 그대로다.
- c3 판정(한 스레드 번갈아, ≤ 10 ms·≥ 100 fps·넣은 수 = 나온 수)은 그대로 두고, 비교용 변형을 같은 실행에서 앞 5초(프레임 125 × 4대)로 더 잰다(판정 안 함, 실패해도 `<변형>_error` 로 남고 다음 변형이 돈다).

| 값 접두 | 내용 | 보는 것 |
|---|---|---|
| `nvdec_` | 판정: NVDEC, 한 스레드 번갈아, 10초 | 기준 통과 여부 |
| `cpu_` | CPU, 한 스레드 번갈아, 10초 | 비교 |
| `nvdec_par_` / `cpu_par_` | 카메라마다 스레드 하나(LIVE 구조) | LIVE 합계 처리량, 디코더 선택 재료 |
| `nvdec_thr0_` / `nvdec_thr1_` | 변환 스레드 자동 / 1 | `convert_threads` 기본값 |
| `nvdec_nv12_` | BGR 변환 없이 nv12 그대로 | 디코딩+내려받기 하한(남은 몫이 NVDEC·PCIe 인지 변환인지) |

노트북(RTX 4050) 전후, c3 와 같은 측정(250 프레임 × 4대, 한 스레드 번갈아):

| | NVDEC p50 / p95 / 합계 | CPU p50 / p95 / 합계 |
|---|---|---|
| 고치기 전 | 25.90 / 36.37 ms / 37.5 fps | 6.74 / 7.89 ms / 147.6 fps |
| 고친 뒤(1회) | 6.71 / 9.04 ms / 135.9 fps | 1.35 / 2.00 ms / 676.6 fps |
| 고친 뒤(c3 전체 실행, 35.6초) | 8.33 / 10.43 ms / 108.6 fps | 1.73 / 2.19 ms / 548.6 fps |

같은 c3 실행의 변형: `nvdec_par` 224.5 fps(p95 22.3 ms), `cpu_par` 985.7 fps, `nvdec_thr0` 126.7 fps, `nvdec_thr1` 54.5 fps, `nvdec_nv12` 162.9 fps(p50 4.98 ms). 노트북 GPU 는 노트북용이라 기준 판정은 Pod 값으로 한다. NVDEC 그림은 CPU 그림과 화소가 같았다(최대 차이 0, GPU 시험은 ≤ 2 허용).

## 결론: 기본 디코더 CPU, NVDEC 은 선택 (S15P31A101-236)

**2차 GPU 세션(2026-10-22, H100, 같은 이미지 lock) c3** (결과 `ai/experiments/runs/gpu-session1/20261022-s2-tier0/queue/runs/*c_tier0_live*/metrics.json`, gitignored):

| 측정 | 그림 p50 / p95 | 합계 |
|---|---|---|
| NVDEC 한 스레드 번갈아(이전 판정) | 6.95 / 9.67 ms | 112.2 fps (통과) |
| NVDEC 카메라별 스레드 | 7.40 / 11.94 ms | 214.7 fps |
| NVDEC 변환 스레드 0 / 1 | 5.50 / 6.57 · 12.54 / 12.90 ms | 119.0 · 65.1 fps |
| NVDEC nv12 그대로(변환 없음) | 4.16 / 4.39 ms | 142.7 fps |
| CPU 한 스레드 번갈아 | 1.98 / 2.48 ms | 492.3 fps |
| CPU 카메라별 스레드 | 2.01 / 3.05 ms | 1,575.7 fps |

c4 LIVE(당시 decoder nvdec)는 통과(ready 4/4, decode_failed 0, frame_age p95 최대 79 ms, pipeline_lag p95 143 ms). 원인 고침(-212) 뒤에도
NVDEC 은 디코딩+내려받기만으로 4.2 ms(nv12)이고 BGR 변환은 CPU 에서 따로 치른다. CPU 는 디코딩+변환 전체가 그보다 짧다.

**노트북 실제 교실 녹화(2304×1296, cam_A `epoch_0000.h264` 250 프레임, GOP 50, 접근 단위마다 슬라이스 1개, CPU 22개, PyAV 18.1.0):**

| 디코딩 스레드 / 변환 스레드 | 구조 | 그림 p50 / p95 | 합계 |
|---|---|---|---|
| 자동(0) / 4 (지금 기본값) | 카메라 1대 | 3.90 / 5.38 ms | 213.6 fps |
| 1 / 4 | 카메라 1대 | 4.26 / 5.50 ms | 219.3 fps |
| 1 / 1 | 카메라 1대 | 6.30 / 8.21 ms | 151.7 fps |
| 자동 / 자동 | 카메라 1대 | 3.60 / 4.82 ms | 251.5 fps |
| 자동 / 4 (지금 기본값) | 4대, 카메라별 스레드 | 5.59 / 8.22 ms | 652.1 fps |
| 1 / 4 | 4대, 카메라별 스레드 | 6.50 / 8.62 ms | 586.0 fps |
| 1 / 1 | 4대, 카메라별 스레드 | 7.02 / 8.86 ms | 541.5 fps |
| 2 / 2 | 4대, 카메라별 스레드 | 6.02 / 8.90 ms | 612.3 fps |

측정 스크립트는 노트북 임시(저장소 밖)였고, 같은 녹화를 4대에 넣었다. 1/1(코어 하나) 기준 그림 한 장 약 6.3~6.6 ms 라 4대 × 25 fps 는 코어
약 0.6~0.7 개를 쓴다(Lead 측정 threads=1 4.9 ms 기준이면 약 0.5 개).

**결정.**

- LIVE·file 입력 기본 디코더를 `cpu` 로 바꿨다(`services/tier0/settings.py` `DEFAULT_DECODER`). `nvdec` 은 `input.decoder` 로 계속 고를 수 있고,
  골랐는데 NVDEC 을 열지 못하면 지금처럼 CPU 로 바꾸지 않고 시작하지 않는다(종료 코드 4). 모르는 값(대소문자 다름, null 등)은 기본값으로 바꾸지
  않고 설정 오류다.
- 근거: Pod 에서 CPU 가 LIVE 구조 합계 7배, 그림 p95 4배 낫다. NVDEC 도 색 변환은 CPU 라 CPU 를 아끼지 못한다. 시연 때 GPU 는 검출·HMR·VLM 이
  나눠 쓴다(-235). 실제 카메라 영상에서도 노트북 CPU 로 4대 합계 540 fps 이상이다.
- 명세 5.1 의 "GPU 하드웨어 디코더"를 "기본 CPU, NVDEC 선택"으로, 3.2 예산표의 하드웨어 디코더를 "NVDEC 을 고를 때만"으로 바꿨다(v1.3 수정 26).

**CPU 디코더 스레드 설정 확인(LIVE 구조).** 코드는 바꾸지 않았다.

- 카메라별 스레드: 수신·디코딩은 카메라마다 스레드 하나(`VideoIngestServer`·`CameraIngest`, 카메라별 lock)라 카메라 하나는 한 프레임 간격
  40 ms 안에만 끝내면 된다. 실제 녹화 4대 동시에서 그림 p95 8.2~8.9 ms(어느 설정이든)로 여유가 크다. PyAV 디코딩·swscale 은 GIL 을 놓으므로
  4대가 서로 다른 코어에서 돈다.
- `thread_type = SLICE`, `thread_count` 자동(0): Tapo C110 스트림은 접근 단위마다 슬라이스가 하나라 슬라이스 스레드가 일을 나누지 못한다(자동과
  1 이 같다). 자동은 FFmpeg 이 min(보이는 CPU + 1, 16, (높이+15)/16) 개 스레드를 만들지만 할 일이 없으면 잠들어 있어 CPU 를 쓰지 않는다. FRAME
  스레드는 출력이 스레드 수만큼 늦어 쓰지 않는다(그대로).
- `convert_threads = 4`(카메라당 swscale 스레드): 카메라 1대에서 1 → 4 가 그림 6.3 → 4.3 ms 로 줄이고, 4대 동시에서는 합계 차이가 작다(541 → 586~652
  fps). 총 CPU 일은 스레드 수와 무관하게 같고, 코어가 적으면 지연만 조금 는다. 4대 × 4 = 16 스레드라 vCPU 가 적은 사이트에서도 프레임 간격 안에
  들어온다(1/1 최악 p95 8.9 ms). 그대로 둔다.
- 코어 수가 적은 사이트: 명세 3.12 최소 사양에 vCPU 가 없었다. 디코딩 몫(코어 1 이하)에 Tier 0 판정 루프(파이썬 한 스레드, 기하 10명 Pod
  p95 57 ms 로 이미 빠듯함, c5a)·검출 전후처리·수신 TLS·분석 워커를 더해 **컨테이너 vCPU 8개 이상**을 초기값으로 적었다(v1.3 수정 26). 시작
  절차는 아직 vCPU 로 멈추지 않는다. 2차 세션 metrics 의 `cpu_count` 224 는 호스트 값이라 할당량을 모른다. 큐 c0 가 다음 세션부터 기록한다.

**c3 판정을 바꿨다(`deploy/scripts/session1_live.py`).** 판정은 기본 디코더 CPU, 카메라마다 스레드 하나(LIVE 구조), 10초 전체: 그림 한 장 p95 ≤
10 ms(프레임 간격 40 ms 의 1/4, 이전 기준 값 유지), 4대 합계 ≥ 100 fps, 넣은 수 = 나온 수. NVDEC(한 스레드·카메라별·변환 스레드 0·1·nv12)과 CPU
한 스레드 번갈아는 참고 측정(판정 안 함, 실패하면 `<이름>_error`)이다. NVDEC 은 선택 디코더라 c1(열기)·c2(GPU 시험)도 선택 단계(기록만)로
바꿨다. NVDEC 이 없는 GPU 여도 큐 c 는 기본 경로로 판정한다. c4·d LIVE 하네스는 설정에 디코더를 적지 않아 서비스 기본값(cpu)으로 돈다.

## 다음 GPU 세션에서 확인할 것

큐 c 항목을 그대로 돌린다(`python deploy/scripts/session1_live.py all`). c3 만 다시 잴 때:

```
cd ai && python -c "import sys, json; sys.path.insert(0, '../deploy/scripts'); import session1_live as s; print(json.dumps(s.c3_decode_throughput(), ensure_ascii=False))"
```

1. c0: `usable_vcpu`(cgroup 할당량·`sched_getaffinity`·호스트 수 중 작은 값, 출처 `usable_vcpu_source`)와 `cpu_model`. cgroup 은 `/proc/self/cgroup` 의 자기 그룹 경로를 `/proc/self/mountinfo` 의 cgroup2·cpu 컨트롤러(v1 공동 마운트 `cpu,cpuacct` 포함) 마운트에 붙여 잎부터 기준점까지 읽고 가장 작은 값을 쓴다(읽은 파일 `cgroup_vcpu_file`). 못 읽으면 `cgroup_vcpu` 는 null 이다. 8 미만이면 명세 3.12 초기값과 c3·c4·c5a 를 같이 본다.
2. c3: `cpu_par_per_frame_ms_p95` ≤ 10, `cpu_par_total_fps` ≥ 100, 넣은 수 = 나온 수. 2차 세션 cpu_par(5초 변형) 3.05 ms·1,575.7 fps 와 비교한다.
3. c4: 기본 디코더 CPU 로 `frame_age_ms_p95_max`·`pipeline_lag_ms_p95` 를 2차 세션(NVDEC, 79 ms·143 ms)과 비교한다.
4. 참고: `nvdec_*` 은 기록만. NVDEC 이 기본을 다시 이길 때(예: GPU 변환 경로를 따로 만든 뒤)만 기본값을 다시 본다.

## 버린 대안

- `to_ndarray(format=..., threads=k)` 만 바꾸기: 그림마다 문맥을 만드는 비용이 남는다.
- nv12 → yuv420p → bgr24 2단계 swscale: nv12→yuv420p 도 일반 경로라 15.9 ms 로 나아지지 않았다.
- numpy 로 nv12 의 UV 를 나눠 yuv420p 를 만든 뒤 직행 경로: 약 10 ms 로 문맥 재사용보다 느리다.
- OpenCV `cvtColor(COLOR_YUV2BGR_NV12)`: tier0 이미지에는 있으나(`io` extra) 개발 환경에 없어 재지 못했다. 문맥 재사용으로 기준 안에 들면 경로를 늘리지 않는다.
- GPU 에서 변환(`scale_cuda` 필터, 또는 `is_hw_owned` + DLPack 으로 torch 에 넘겨 검출기에 GPU 텐서): 내려받기와 CPU 변환을 모두 없앨 수 있지만 `DecodedPicture`(numpy BGR)와 검출기 입력이 바뀐다. Pod 값(3번)을 보고 별도 티켓으로 정한다(-183 "장치에 둔 채 넘기는 것은 실측 뒤").
- 디코더 스레드(`thread_type`·`thread_count`): hwaccel 에서 차이가 없었다(위 분해 표).
- c3 기준 바꾸기: 명세에 없는 설계 기준이라 이 티켓에서 바꾸지 않는다(아래 제안).

## 제안 (다음 명세·설계 정리로)

- (-236 에서 반영) c3 판정을 LIVE 구조(CPU 카메라별 스레드)로, 기본 디코더를 실측으로 CPU 로 정하고 명세 5.1 을 바꿨다(위 '결론'). 합계 기준에
  여유 배수(예: 1.5배)는 두지 않았다: 100 fps 는 4대 × 25 fps 그대로이고, 실측이 기준의 15배라 배수가 판정을 바꾸지 않는다.

## 잔여 위험

- (-212) Pod 에서 원인·효과는 2차 세션 변형으로 확인했다(변환 스레드 0·4 ≈, 1 은 절반, nv12 그대로가 가장 빠름).
- 변환 스레드 4 × 카메라 4 = 16 스레드와 디코딩이 검출·판정과 CPU 를 나눈다. 컨테이너 vCPU 할당량을 아직 모른다(c0 로 기록). 할당량이 작으면
  c3 `cpu_par_` 와 c4 지연, c5a 기하 시간으로 본다.
- c3 의 시험 무늬 영상은 디코딩이 쉬워 실제 카메라 영상의 CPU 디코딩 비용을 낮게 본다. 실제 교실 녹화는 노트북에서만 쟀다(위 표, 무늬 대비
  약 2~3배 비용이지만 기준의 5배 이상 여유). Pod 에서 실제 녹화 재생은 c5 수동 절차로 본다.
- NVDEC 을 고르면 GPU 에 디코더 문맥 4개가 올라가고 색 변환은 여전히 CPU 다. 선택 경로라 큐는 참고로만 잰다.
