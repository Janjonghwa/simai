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

**Detectron2 용도.** 우리 경로(YOLO26-Pose 검출)에서는 `sam_3d_body/visualization/utils.py`가 관절 메타데이터를 읽는 `detectron2.config.LazyConfig`(순수 Python)만 쓴다(ViTDet 검출기 옵션은 쓰지 않음). 볼륨 환경(E-GATE-0)은 공식 설치를 따라 CUDA 확장까지 빌드하느라 nvcc(micromamba cuda-toolkit)가 필요했다. S15P31A101-238 에서 CUDA 확장 없이(`FORCE_CUDA=0`, CPU 전용 C++ 확장) 빌드해 LazyConfig 읽기·스켈레톤·EGL 렌더가 되는 것을 노트북(WSL)에서 확인했고, exp02 이미지에서 CUDA devel 빌드 단계·GPU 세대별 컴파일을 뺐다(근거·확인 내용은 `docs/design/exp-images.md` "Detectron2 CUDA 확장 제거", GPU 확인은 3차 세션 g0).

**K 입력 (S15P31A101-238).** HMR 은 카메라 K 로 사람을 카메라 좌표에 올린다. E-GATE-0 은 K 를 MoGe2 FOV 추정으로 얻었지만(공개 영상 14장에는 K 가 없다), 교실 C110 에는 무마커 보정 K 가 있고
(AnyCalib, -192: cam_A fx 1,257.8 px → 수평 약 85°) MoGe 계열 추정(70~73°)과 어긋난다. 그래서 `run_image.py --mode official` 은 알려진 K 를 먼저 쓴다(코드 `fs3db_common.resolve_k`·`prepare_image`, 시험 `ai/tests/experiments/hmr/test_hmr_k_input.py`).

| 순위 | 입력(`run_image.py` 인자) | 받는 형식 | 기록(`k_input`) |
|---|---|---|---|
| 1 | 보정 K `--calibration` | `simai.intrinsics_estimate` 기록(`calibration.intrinsics.cli`) 또는 calibration 계약 문서(contracts/providers/calibration.schema.json) | `source` CALIBRATION, `calibration_source`(G1_AUTO 등)·`calibration_state`, 계약 문서면 `calibration_version` |
| 2 | 데이터셋 K `--dataset-k` | calibration 계약 문서(DATASET_GT) 또는 `{"dataset_id", "cameras": [{camera_id, image_size, K, distortion}]}` | `source` DATASET, `calibration_source` DATASET_GT(문서에 없으면) |
| 3 | 없음 | MoGe2 FOV 추정(E-GATE-0 와 같은 경로) | `source` MOGE_ESTIMATE, 보정 출처·상태를 적지 않는다(계약의 "이미지만 넣어 모델이 카메라를 추정하면 calibration 생략"과 같다) |

- 어휘는 계약의 것을 쓴다: `calibration_source`(G1_AUTO·DATASET_GT·SIM·MODEL_ESTIMATE), `calibration_state`(PROVISIONAL·CALIBRATED…). `intrinsics_estimate` 기록은 무마커 1단계 초기값(번들 조정 전)이라 G1_AUTO·PROVISIONAL 로 적는다.
  모든 경우에 파일 이름·sha256·camera_id·주어진 K·왜곡·쓴 K(`K_used`)·펴기 여부를 그림별 행에 남기고, 최상위 `k_input`(`source`·`count`·`undistorted`)은 `exp_job.py` 가 Run 기록 지표(`k_input.source`, `k_input.count.*`)로 넘긴다.
  모든 그림이 같은 K 파일이면 최상위 `k_input.input_provenance`(`calibration.source`·`calibration_version`, 없으면 `<파일>@<sha256>`, `is_upper_bound` = DATASET_GT·SIM 이면 true)를 함께 남긴다. 큐 Job 정의가 이 값을 Run 기록 `input_provenance` 에 옮긴다(리뷰 R4, g4).
- 순위는 인자 순서와 상관없다. 후보를 못 쓰면 이유를 `skipped[]` 에 적고 다음 순위로 간다(`calibration.intrinsics` 백엔드 순서의 "사용 불가만 대체"와 같은 방식):
  파일을 못 읽음, 카메라를 하나로 정할 수 없음(`--camera-id` 없고 카메라가 여럿이면 파일 이름 접두 `cam_A_01` → `cam_A` 가 하나로 맞아야 함), `status` 가 OK 가 아님,
  `image_size` 가 그림과 다름(**K 를 늘리거나 줄이지 않는다**: 스트림 해상도가 바뀌면 보정이 DRIFTED 라는 계약 규칙과 같다), 유한한 핀홀 K 가 아님, OPENCV_PINHOLE·NONE 밖의 왜곡(어안 등),
  카메라 목록·항목·`distortion` 이 객체가 아님, 계수 개수가 0·4·5·8 이 아님, NONE 에 0 이 아닌 계수. 어떤 형식 오류도 실행을 끊지 않고 이유가 된다(리뷰 M3).
- 모든 그림에 K 가 있으면 MoGe 를 띄우지 않는다. K 가 없는데 MoGe 도 없으면 실행하지 않는다(추정기의 기본 K 로 조용히 돌지 않는다).
- split·split_pose 모드는 K 파일을 받지 않는다(그 모드의 bbox·K 캐시는 원본 그림 기준이라 펴진 그림과 맞지 않는다). E-GATE-0 재현(H0·H1)은 지금처럼 MoGe 경로다.
- **왜곡은 펴서 넣는다.** SAM 3D Body 는 핀홀 투영을 가정한다. C110 보정(k1 −0.27, k2 0.046)은 화면 모서리에서 반지름이 약 24%(수백 px) 줄어드는 강한 배럴 왜곡이라 그대로 넣으면 가장자리 사람의 광선·깊이가 틀린다.
  새 K 는 `cv2.getOptimalNewCameraMatrix(K, D, (W, H), alpha=1, (W, H), centerPrincipalPoint=True)`(원본 화소를 모두 담는 K, 같은 크기, 주점 중앙)이고 `cv2.undistort(img, K, D, None, K_new)` 로 펴서
  HMR 에 K_new 를 준다(`k_input.K_used`, `image_space` UNDISTORTED_ALPHA1). 계수가 모두 0 이면 펴지 않는다. 새 K 가 유한하지 않으면 그 그림을 실패로 둔다(지어내지 않는다).
  펴기 시간은 `undistort_ms`(CPU, `process_one_image_ms` 밖)로 따로 남긴다. 결과 2D 를 원본 픽셀로 되돌리는 식은 `fs3db_common.to_original_pixels`: K_given · distortion(K_used⁻¹ · p) 이다(새 K 와 상관없이 정확하다. 시험은 `simai_ai.geometry.distortion` 으로 편 점의 왕복 1e-6 px).
  왜곡 계수는 OPENCV_PINHOLE 0·4·5·8개만 받는다(`simai_ai.geometry.distortion` 과 같은 범위, 되돌리기 식을 정확히 아는 것). NONE 은 0 이 아닌 계수를 받지 않는다.
  - **측정(2026-10-22, 노트북 CPU, 리뷰 R1).** 교실 사진 `classroom-stills-20261021` 20장(cam_A~D × 5, 2304×1296)과 보정 기록 `intrinsics-20261021-022940z.json` 으로 yolo11m-pose(conf 0.5, 선행 PoC 가중치) 사람 93명을 원본에서 찾고,
    관절·bbox 를 `simai_ai.geometry.distortion` 으로 펴진 좌표에 옮겨 셌다(사진·검출 결과는 저장하지 않았다. 스크립트는 scratchpad 에서 지움):

    | 새 K | 관절이 모두 그림 안 | 일부만 안(중심은 안) | 중심이 그림 밖(빠짐) | bbox 높이 중앙값 | 초점(원 K 대비) |
    |---|---|---|---|---|---|
    | 같은 K(첫 구현) | 43 | 8 | **42 (45%)** | 369.6 px | 1.0 |
    | alpha=1(채택) | 93 | 0 | 0 | 236.6 px | 0.586~0.640 (cam_C·A·D·B) |
    | (원본, 펴기 전) | | | | 268.6 px | |

    같은 K 는 중심을 원본보다 늘려(높이 1.38배) 좌우·위아래 끝의 사람을 거의 절반 잃는다. alpha=1 은 모두 담고 사람 높이가 원본의 0.88배라 HMR crop(512)에는 차이가 작다.
    alpha=1 의 원본 가장자리 6점(모서리·좌우 중앙)은 네 카메라 모두 펴진 그림 안에 든다(왜곡 식이 이 범위에서 접히지 않는다). 그래서 alpha=1 로 바꿨다.
  - 버린 대안: 왜곡을 그대로 넣기(위 이유로 가장자리가 틀린다), 같은 K(위 측정: 45% 빠짐), 더 큰 출력 크기(같은 초점으로 원본 화각을 담으려면 약 3,650 px 폭. 그림이 커져 검출·메모리가 늘고 HMR crop 해상도 이득은 작다),
    그림 대신 사람 crop 만 펴기(Fast 공식 API 수정이 필요하다).
  - 잔여 위험: alpha=1 펴진 그림은 모서리에 빈(검은) 영역이 생긴다. 원본 가장자리의 왜곡 보정 정확도는 AnyCalib 직선성 확인이 UNVERIFIED 라 확인하지 않았다. 사람 검출기는 선행 PoC 의 yolo11m-pose 였고(우리 경로는 YOLO26x-Pose) 정답 수가 없는 세기다.
- **MoGe 는 남긴다.** E-GATE-0 재현 입력(공개 영상 14장)·라이브 서버(`live_server.py`, 첫 프레임 MoGe2 로 K 고정)·K 를 모르는 입력의 예비 경로라서 exp02 가중치 묶음(MoGe2 ViT-S·ViT-L)과 이미지 의존성(MoGe @74fbce0)에서 빼지 않는다.
  라이브 서버의 K 입력은 이번 범위가 아니다(남은 일).
- 3차 세션 확인 항목 g4(교실 사진 20장 + 보정 기록, `fov_moge2` 를 띄우지 않고 20장 모두 CALIBRATION)는 `docs/design/exp-images.md` "3차 세션 항목".

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
