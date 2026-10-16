# EXP-02 인체 메시·외형 — 실험 설계와 E-GATE-0

- Jira: 에픽 S15P31A101-16, 스토리 S15P31A101-86(E-GATE-0) → -87(E-GATE-1 다시점 집계·root 정렬) → -88(MHR→glTF) → -89(외형 갱신) → -105(E-GATE-2~4)
- 기획서: §5.6, §20.3, §20.4(INT-01 계약, 아바타 구동 규칙, 입력 대체 계약), §20.6
- 선행 PoC: `Moge-3-test/pocs/human_fast_sam3d_g3_lite`(RTX 4050 6GB, 2026-10-14~15), `human_sam3d_g3_lite`(원본 구현). 스크립트는 앞의 PoC에서 옮겨 왔다.

## 1. 연구 질문

Fast SAM 3D Body의 MHR 메시·포즈를 World ID에 결합해 움직이는 메시 아바타를 만들고, 같은 인물의 관측이 쌓일수록 실제 외형에 가까워지게 할 수 있는가?

**Fast SAM 3D Body란.** 별도 모델이 아니라 Meta SAM 3D Body의 가중치(`facebook/sam-3d-body-dinov3`, SAM License, HF 승인 필요)를 그대로 쓰는 가속 추론 코드다(`yangtiming/Fast-SAM-3D-Body`, MIT). torch.compile, 병렬 디코더, FP16 config, 단계 생략(keypoint prompt 등)으로 빠르게 한다.

## 2. 파이프라인

| 단계 | 내용 | 계약 |
|---|---|---|
| 1 입력 | 카메라별 프레임, World 트랙(WorldTrackProvider: world_id·카메라별 bbox·keypoint·pelvis_xyz) | world_tracks |
| 2 마스크·프롬프트 | YOLO26-seg 마스크, 안정 관절(몸통·고관절) keypoint 프롬프트 | — |
| 3 HMR | 카메라별 Fast SAM 3D Body → MHR 파라미터·메시(카메라 좌표) | — |
| 4 다시점 집계 | 카메라당 최대 1개 제약 클러스터링 → 대표 포즈. **root는 Tier 0 pelvis에 고정** | human_mesh_output.pose_frame |
| 5 시간 처리 | world_id별 체형 N프레임 누적 후 고정, 포즈 평활화 | avatar_asset.shape |
| 6 외형 누적 | (동의 승인 후) 가시성·시선각 가중 텍셀 누적, observed 마스크, 얼굴 제외 | avatar_asset.texture·surface_source |
| 7 내보내기 | 스키닝 glTF(MHR→glTF), 프레임별 포즈 | avatar_asset.mesh_geometry |

## 3. 게이트와 판정 기준(사전 등록)

| 게이트 | 확인 | 합격 기준 |
|---|---|---|
| **E-GATE-0** | H100 설치·재현, 지연·VRAM, MHR 출력 구조 | (1) 공식 e2e(검출+FOV+HMR)가 14장 모두 유한한 출력, (2) 2D keypoint와 (3D+cam_t) 투영 차이 평균 1px 이하(좌표계 확인), (3) 1~5명 지연·VRAM 표, (4) off 대비 opt 출력 차이는 선행 PoC 결과를 인용(off 대비 2D 0.65 px·정점 차이, 재측정 안 함) |
| E-GATE-1 | 다시점 집계·Tier 0 root 정렬, 다인·교차·가림·재등장, 연속 프레임 | 한 카메라 제외 관절 재투영·실루엣 IoU·발-ground_anchor 오차(G5 5cm)를 기준선(원본·기본 골격) 대비 기록 |
| E-GATE-2 | human_mesh_output 계약, MHR→스키닝 glTF, Three.js에서 움직임 | 스키마 통과, 뷰어에서 포즈대로 움직임 |
| E-GATE-3·4 | 독립 재현, 실제 입력(TIER0_LOG) 재평가, 개인정보 점검(얼굴 픽셀 미저장), Lead 판정 | 기획서 §20.6. 정답 입력만으로는 ADOPT 불가 |

## 4. 실행 환경

| 항목 | 값 |
|---|---|
| GPU / 데이터센터 | NVIDIA H100 80GB HBM3 / AP-JP-1 (2026-10-16 이전. 6.1~6.3 은 A40 46GB / ca-mtl-1) |
| 환경 | micromamba env `/workspace/simai/envs/fast_sam3db`: Python 3.11, cuda-toolkit 12.4, torch 2.5.1+cu124, Detectron2 @a1ce2f9(현재 GPU 아키텍처), MoGe @74fbce0, ultralytics, websockets 14.1. 잠금 파일 `envs/fast_sam3db.lock.txt` |
| 공식 설치와 차이 | conda→micromamba, Detectron2 는 현재 GPU 아키텍처로 빌드(`TORCH_CUDA_ARCH_LIST`, H100 9.0), TensorRT·pyrealsense2 제외(E-GATE-0 범위 밖), setuptools<81, MoGe 뒤 torch 재고정(PoC와 같은 조치), 검출기 YOLO11m-Pose → **YOLO26x-Pose**(최신 버전으로 바로 감, YOLO11 비교 생략 — 2026-10-16 결정) |
| 코드 | Fast-SAM-3D-Body @d72aa36(PoC와 같음), 수정 없음 |
| 가중치 | `/workspace/simai/weights/sam-3d-body-dinov3`(HF 원본 config bfloat16), `sam-3d-body-dinov3-fastcfg`(같은 가중치 링크 + Fast config float16), YOLO26x-Pose |
| 설치 | `ai/experiments/hmr/setup_runpod.sh`(환경), `... weights`(가중치, HF_TOKEN 필요 — 토큰은 볼륨 `/workspace/.simai-private/hf.env`에만) |
| 실행 | `run_image.py`(이미지별 추론·메시·지표), `benchmark.py`(1~5명 지연·VRAM), `det_bench.py`(검출기 지연), `live_server.py`·`live_client.py`(라이브, 노트북 웹캠·C110), 공통 `fs3db_common.py` |
| GPU 큐 | `queue_min.sh`(설치 직후 최소 검증: 가중치·H6·H0·H1·라이브 10분, 실행 완료), `queue_egate0.sh`(다음 세션: H2~H5 + C110 opt_compile 라이브 15분). GPU 는 큐를 돌릴 때만 켠다 |
| 산출물 | `/workspace/simai/runs/exp02/<run_id>/`, 로컬 사본 `ai/experiments/hmr/runs/`(커밋하지 않음) |

**Detectron2 용도.** 우리 경로(YOLO26-Pose 검출)에서는 `sam_3d_body/visualization/utils.py`가 관절 메타데이터를 읽는 `detectron2.config.LazyConfig`(순수 Python)만 쓴다(ViTDet 검출기 옵션은 쓰지 않음). 공식 설치를 따라 CUDA 확장까지 빌드하느라 nvcc(micromamba cuda-toolkit)가 필요하다. 후속(Docker 이미지 작업 때): CUDA 확장 없이 설치해 렌더가 동작하는지 확인 → 되면 uv 만으로 설치하고 GPU 아키텍처별 재빌드를 없앤다.

**PoC와 다른 입력.** PoC는 bbox·K를 원본 SAM 3D Body PoC의 출력(ViTDet bbox, MoGe2-L K)에서 읽었다. 여기에는 그 파일이 없어 `baseline_inputs()`가 YOLO26x-Pose bbox(conf≥0.5)와 MoGe2-L K를 직접 계산해 `runs/exp02/_inputs/`에 캐시한다. bbox 출처가 달라 PoC 수치와는 근사 비교만 한다.

**입력 데이터.** PoC와 같은 14장(`datasets/hmr_poc14`): 1명 전신 5장, 가림·상반신·화면 밖 6장, 다인원 3장. 공개 영상에서 뽑은 프레임이며 우리 참여자가 아니다(외형 실험에 쓰지 않음).

## 5. E-GATE-0 Run 계획

| Run | 설정 | 확인 |
|---|---|---|
| H1 | `run_image.py --mode official --preset opt`, 14장 | 공식 e2e 동작, 유한 출력, 투영 차이, MHR 출력 키·크기 (**완료**, 6.4) |
| H6 | `det_bench.py --half`, 2K 다인원, imgsz 640/1280 × 카메라 1/4대 | Tier 0 에서 YOLO26x-Pose 실시간 가능 여부 (**완료**, 6.4) |
| H2 | `benchmark.py --preset opt --modes split split_pose` | 1~5명 batch·seq 지연, VRAM |
| H4 | `benchmark.py --preset opt_compile` | torch.compile 효과 |
| L7 | C110 실제 카메라 라이브(opt_compile, 손 포함) | 실제 입력 지연·fps, 지연 튐 반복 여부 |

PoC 참고 수치(4050): 1명 full e2e 652ms, body 288ms, 5명 full 2,468ms. HMR만 원본 대비 1.44~1.94배 빠름. 원본 대비 2D 1.89px·정점 11.4mm 차이.

**재측정하지 않는 것(2026-10-17).** off 프리셋 비교(지연 기준선·출력 차이)는 위 PoC 결과로 갈음한다. 가속 설정이 결과를 얼마나 바꾸는지는 GPU 종류와 관계가 없고, 이미 답이 있는 비교에 H100 시간을 쓰지 않는다. 모델·설정 선택은 공식 벤치마크와 운영 GPU(H100) 실측으로 한다.

## 6. 결과

### 6.1 설치

> 6.1~6.3 은 캐나다 ca-mtl-1 A40 결과다. 2026-10-16 개발 기준을 일본 AP-JP-1 H100 으로 옮기면서 캐나다 볼륨을 정리했고, 원본 run 파일·잠금 파일은 보존하지 않았다(수치는 이 문서에만 남음). E-GATE-0 판정은 H100 재측정(6.4)으로 한다.

- `setup_runpod.sh` 성공(2026-10-16, 약 23분). 잠금 파일 `envs/fast_sam3db.lock.txt` sha256 `9aafc298e0d21192…`.
- 가중치 sha256 앞 16자: model.ckpt `b5a2f9d305dd0262`, mhr_model.pt `352e271a6c42729c`, yolo11m-pose.pt `29b17eaf3a3117cb` (전체 값은 `weights/hmr_weights.sha256`).
- 컨테이너에 glvnd EGL 로더가 없어 pyrender 렌더가 실패했다(NVIDIA EGL 벤더 라이브러리만 있음). env 에 conda-forge `libegl libglvnd` 를 넣고 `libEGL.so` 링크를 만들어 해결, 설치 스크립트에 반영.
- H0 스모크(t1_front_taichi, official/opt): SUCCESS, 1명, 투영 차이 0.0 px.

### 6.2 라이브 추론 (Run L1·L2, 계획 외 추가)

노트북 웹캠 → SSH 터널 → A40(`live_server.py`, opt, 검출+HMR, K 는 첫 프레임 MoGe2 로 고정) → 오버레이를 노트북으로 반환(`live_client.py`). 처리 중 들어온 이전 프레임은 버린다. 프레임은 저장하지 않는다.

| 설정 | 종단 지연 p50 / p95 | 실제 추론 fps | 서버 처리(1명) |
|---|---|---|---|
| L1: 1280px, JPEG q85 | 1,379 / 4,944 ms | 약 1.5 | 320~570 ms |
| L2: 640px, JPEG q70 | 935 / 1,518 ms | 약 2.1~2.3 | 310~340 ms (HMR 260~300) |

- 첫 프레임과 처음 나온 인원 수는 1회 워밍업으로 1.4~6.4 s 걸린다. 시연 전 1~5명 워밍업을 미리 해 둬야 한다.
- 네트워크(노트북↔ca-mtl-1): Tailscale 직접 연결 실패, DERP(토론토) 중계, 왕복 185 ms, 업로드 약 2.4 Mbps, 다운로드 약 4.8 Mbps(SSH 로 3 MB 전송 측정).
- 해석: GPU 처리(약 0.33 s)보다 업로드·왕복이 지연을 더 만든다. 해상도를 줄이면 서버 처리는 그대로이고 종단 지연만 0.45 s 줄었다.
- **Tier 0 영향**: C110 stream1 4대는 약 4.4 Mbps 가 필요해 이 네트워크 업로드(2.4 Mbps)를 넘는다. 시연 전 확인: 시연 장소 업로드 속도, Tailscale 직접 연결, 일본 리전 이전 후 왕복 지연.

**443 WebSocket 경로 (Run L3, 2026-10-16).** SSAFY망은 SSH·임의 포트·UDP 를 막고 443 만 통과한다. Tailscale 이 DERP(토론토) 중계를 타서 업로드가 막혔으므로, RunPod HTTPS 프록시(`https://<POD_ID>-8888.proxy.runpod.net`, 443)로 WebSocket + 토큰 인증 경로를 만들었다(토큰 없으면 401 확인).

| 측정 | Tailscale(DERP) | 443 프록시 |
|---|---|---|
| 업로드(3 MB 1회) | 약 2.4 Mbps | 약 9.6 Mbps |
| 웹캠 1280px e2e p50 / p95 | 1,379 / 4,944 ms | 942 / 1,401 ms |
| 테스트 영상 640px / 1280px e2e p50 | 935 / 1,379 ms | 687 / 693 ms |

- 서버 구간(`server_ms`, 수신→결과 전송: 대기+처리)은 275~480 ms 였으므로 e2e 942 ms 중 **약 0.5~0.65 s 가 네트워크**(업·다운로드와 왕복)다. 640px 와 1280px 가 거의 같아 대역폭이 아니라 거리·경로가 원인이다.
- GPU 사용률(0.25 s 간격): 스트리밍 중 0 이 아닌 구간 평균 59%, 전력 평균 143 W. 다음 프레임을 기다리는 동안 GPU 가 쉬고, 디코더는 작은 연산이 많아 사용률이 100% 가 되지 않는다.
- 남은 확인: 443 프록시가 미국을 경유하는지(일본 Pod 에서도 돌아갈 수 있음).
- **결정(2026-10-16)**: 공유기 도입 후 노트북↔RunPod 은 Tailscale 없이 Pod 공개 IP:포트 SSH 직접 연결을 기본으로 하고, 443 프록시는 예비로 둔다. 서버·클라이언트는 두 경로를 모두 지원한다(`--url ws://127.0.0.1:8765/` + SSH 터널 / `.env` 의 `LIVE_URL`).
- 다음 측정(일본 시험 때): SSH 직접 vs 443 지연 비교, 손 제외(body)·compile 적용 라이브 지연.

### 6.3 벤치마크 (H1~H5)

H1(공식 opt, 14장) SUCCESS. H2(opt) 완료. H3 진행 중 라이브 테스트를 위해 중단, H4·H5 미실행.

| H2 조건 (A40, t3_multi_f050) | 1명 p50 | 5명 p50 | 초당 처리 인원(5명) |
|---|---|---|---|
| 공식 e2e(검출+HMR), full(손 포함) | 304 ms | 888 ms | 5.6 |
| 공식 e2e, body(손 제외) | 201 ms | 437 ms | 10.0 |
| HMR 만, 병렬 decoder, 순차 | 298 ms | 1,521 ms | 3.2 |
| HMR 만, 병렬 decoder, batch | 298 ms | 858 ms | 6.0 |

### 6.4 일본 H100 최소 검증 (2026-10-16, AP-JP-1, H100 80GB HBM3)

- 설치: `setup_runpod.sh` 성공(약 54분, 볼륨 쓰기가 캐나다보다 1.5~2.5배 느림). Detectron2 arch 9.0 재빌드. 잠금 파일 sha256 `44cab6dd0a4818a1…`. 검출기 YOLO26x-Pose.
- 네트워크(노트북→Pod, SSH 직접): ping 43 ms(42~46), 업로드 약 39 Mbps, 다운로드 약 51 Mbps.
- **H6 검출 시간**(YOLO26x-Pose FP16, 2304×1296 다인원 사진, 5명 모두 검출):

| imgsz | 카메라 1대 | 카메라 4대 동시 | 메모리 |
|---|---|---|---|
| 640 | 15.4 ms | 21.5 ms (5.4 ms/프레임) | 321 MB |
| 1280 | 18.4 ms | 38.2 ms (9.5 ms/프레임) | 770 MB |

  → 4대 1280px 동시 처리도 카메라당 약 26 fps 가능. Tier 0 실시간 검출에 충분.
- **H1 공식 경로 14장**(opt, YOLO26x + MoGe2 + HMR): 14/14 SUCCESS, 모두 유한 출력, 투영 차이 0.0 px → **E-GATE-0 기준 (1)(2) 충족**. 1명 total 270~340 ms(HMR 200~275), 4~5명 HMR 2.5~2.8 s(첫 실행 포함)·0.25 s.
- **L4 라이브**(SSH 터널, 테스트 영상 0·1·5명 혼합, opt, 워밍업 12.6 s):

| 설정 | e2e p50 / p95 | 캐나다 443 (같은 영상) |
|---|---|---|
| 1280px, inflight 2 | 141~144 / 846~911 ms | 693 ms (p50) |
| 640px, inflight 2 | 130 / 819 ms | 687 ms |
| 1280px, inflight 1 | 171 / 605 ms | — |

  서버 처리: 1명 217 ms(HMR 185, 검출 22), 5명 389 ms(HMR 333). A40 대비 1명 약 0.7배, 5명 약 0.45배. 테스트 영상의 늘린 1명 장면은 0명으로 검출됨(영상 제작 시 비율 왜곡 추정, 실제 카메라로 재확인). 서버 종료 시 live_stats.json 은 남지 않았다(로그 수치로 기록).
- **L5 웹캠 라이브**(노트북 웹캠, 1명, 손 포함 full, 1280px, inflight 2): e2e p50 **522 ms** / p95 665 ms (캐나다 443: 942 / 1,401 ms), 추론 약 4 fps. 서버 처리 190~295 ms(HMR 120~265, 검출 15~26), 수신→전송 290~500 ms → 앞 프레임 대기 약 0.1~0.2 s, 네트워크 약 0.13 s. 이제 지연의 대부분이 GPU 처리+대기라 다음 단계는 inflight 1·body·compile. 웹캠은 10 fps 로 잡힘.
- **L6 웹캠 라이브**(결과 데이터만 응답, 노트북이 실시간 웹캠 + 오버레이 + Open3D 3D 메시 창을 그림 — 선행 PoC 와 같은 화면, 손 포함 full, 1280px, inflight 2): e2e p50 **450 ms** / p95 744 ms, 추론 약 3.6~4.5 fps, 서버 처리 150~280 ms(HMR 128~275). 영상이 실시간이라 체감 렉이 크게 줄었다. 한 프레임 서버 시간 2.4 s 튐(원인 미확인).
- **결정(2026-10-16)**: 기본 모드는 손 포함(full). 손 품질·지연이 충분해 손 제외(body) 측정은 하지 않는다.
- 미실행(다음 검증 세션): H2·H4 벤치마크, compile 라이브, 실제 카메라(C110) 라이브, 긴 실행에서 지연 튐 반복 여부.
