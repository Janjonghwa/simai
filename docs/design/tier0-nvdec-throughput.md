# Tier 0 NVDEC 디코딩 처리량 (c3)

S15P31A101-212. 기획서 §5.1(디코딩과 추론 분리, GPU 하드웨어 디코더), §3.2 표(Tier 0 = 엔진 + 하드웨어 디코더 4개), §6.6(지연 예산), §12.7(I0 압축 프레임), 요구사항 FR-48·FR-50(2304×1296 25 fps H.264 4대) 기준이다.
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

## 다음 GPU 세션에서 다시 잴 것

큐 c 항목을 그대로 돌린다(`python deploy/scripts/session1_live.py all`, c3 은 노트북 실측 약 36초, H100 은 더 짧을 것으로 본다). c3 만 다시 잴 때:

```
cd ai && python -c "import sys, json; sys.path.insert(0, '../deploy/scripts'); import session1_live as s; print(json.dumps(s.c3_decode_throughput(), ensure_ascii=False))"
```

1. `nvdec_per_frame_ms_p95` ≤ 10, `nvdec_total_fps` ≥ 100 이면 기준 통과. 20261021b(10.565 ms, 86.1 fps)와 비교한다.
2. `nvdec_` vs `nvdec_thr0_` vs `nvdec_thr1_`: 기본 4 가 자동보다 나쁘지 않은지. 크게 다르면 `DEFAULT_CONVERT_THREADS` 를 고친다.
3. `nvdec_nv12_`: 이것만으로 10 ms 근처면 남은 병목은 NVDEC 동기 디코딩·내려받기다(아래 버린 대안의 GPU 변환 티켓).
4. `nvdec_par_` vs `cpu_par_`: LIVE 구조의 합계. 시험 무늬는 압축이 쉬워 CPU 디코딩 비용의 하한이므로, 디코더 선택은 실제 카메라 녹화로 c4/수동 측정을 한 번 더 보고 정한다.
5. c4 의 `frame_age_ms_p95_max`·`pipeline_lag_ms_p95` 를 20261021b(82 ms, 144 ms)와 비교한다.
6. c2(GPU 시험)에 `test_nvdec_bgr_matches_cpu_bgr` 가 더해졌다(NVDEC BGR = CPU BGR, 차이 ≤ 2).

## 버린 대안

- `to_ndarray(format=..., threads=k)` 만 바꾸기: 그림마다 문맥을 만드는 비용이 남는다.
- nv12 → yuv420p → bgr24 2단계 swscale: nv12→yuv420p 도 일반 경로라 15.9 ms 로 나아지지 않았다.
- numpy 로 nv12 의 UV 를 나눠 yuv420p 를 만든 뒤 직행 경로: 약 10 ms 로 문맥 재사용보다 느리다.
- OpenCV `cvtColor(COLOR_YUV2BGR_NV12)`: tier0 이미지에는 있으나(`io` extra) 개발 환경에 없어 재지 못했다. 문맥 재사용으로 기준 안에 들면 경로를 늘리지 않는다.
- GPU 에서 변환(`scale_cuda` 필터, 또는 `is_hw_owned` + DLPack 으로 torch 에 넘겨 검출기에 GPU 텐서): 내려받기와 CPU 변환을 모두 없앨 수 있지만 `DecodedPicture`(numpy BGR)와 검출기 입력이 바뀐다. Pod 값(3번)을 보고 별도 티켓으로 정한다(-183 "장치에 둔 채 넘기는 것은 실측 뒤").
- 디코더 스레드(`thread_type`·`thread_count`): hwaccel 에서 차이가 없었다(위 분해 표).
- c3 기준 바꾸기: 명세에 없는 설계 기준이라 이 티켓에서 바꾸지 않는다(아래 제안).

## 제안 (다음 명세·설계 정리로)

- c3 판정을 LIVE 구조로: 카메라별 스레드(`*_par_`)에서 합계 ≥ 100 fps 에 여유(예: 1.5배)를 두고, 카메라별 그림 p95 는 한 프레임 간격(40 ms)보다 충분히 작게. 한 스레드 번갈아 측정은 기록으로 남긴다.
- 디코더 선택: Pod CPU 가 실제 영상에서도 여유 있게 4대를 디코딩하면 `input.decoder` 를 설정값으로 두고(지금도 `nvdec`/`cpu` 선택 가능) 기본을 실측으로 정한다. 명세 §5.1 은 "GPU 하드웨어 디코더"라 기본을 CPU 로 바꾸려면 명세 변경이 필요하다.

## 잔여 위험

- Pod 에서 원인·효과는 아직 추정이다(같은 PyAV 경로라 같은 원인으로 본다). 다음 세션 c3 변형으로 확인한다.
- 변환 스레드 4 × 카메라 4 = 16 스레드가 검출·판정과 CPU 를 나눈다. Pod 의 vCPU 할당량이 작으면 `nvdec_par_` 와 c4 지연으로 본다.
- 시험 무늬 영상은 디코딩이 쉬워 실제 카메라 영상의 CPU 디코딩 비용을 낮게 본다(NVDEC 쪽은 내려받기·변환이 내용과 거의 무관).
