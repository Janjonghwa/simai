# 무마커 내부 파라미터 추정 설계

S15P31A101-116. 기획서 §4.3 1단계(교실 직선 단일 영상 추정 → 같은 렌즈 4대 초기값 공유 → 3단계 번들 조정에서 카메라별 정밀화), 부록 B(모델·라이선스), §19.1(도구 선택과 라이선스 미결), 요구사항 FR-23 기준이다.
코드는 `ai/src/simai_ai/calibration/intrinsics/`, 시험은 `ai/tests/calibration/intrinsics/` 에 있다. 마커·체커보드(ChArUco)는 어떤 경로에서도 쓰지 않는다.

## 후보 조사 (2026-10-17 확인)

| 후보 | 코드 라이선스 | 가중치 라이선스 | 카메라 모델(왜곡) | 입력 해상도 | CPU 추론 | 출처 |
|---|---|---|---|---|---|---|
| GeoCalib (ECCV 2024) | Apache-2.0 | CC-BY-4.0 (출처 표시) | `pinhole`, `simple_radial`(k1), `radial`(k1, k2), `simple_divisional`(강한 어안). 주점은 영상 중심 고정 | 320 으로 크기 조정, 32 의 배수(`resize: 320`, 기준 변은 코드에서 확인 못 함) | 됨(코드가 CUDA 없으면 CPU. 속도 미측정) | https://github.com/cvg/GeoCalib , 가중치 `releases/download/v1.0/geocalib-{pinhole,distorted}.tar` |
| AnyCalib (ICCV 2025) | Apache-2.0 | Apache-2.0 | 모델 무관 FoV 필드 → `pinhole`, `radial:k`(Brown-Conrady k1~k4), `kb:k`(Kannala-Brandt), `ucm`, `eucm`, `division:k`. 주점도 추정 | 학습 해상도 약 320²(102,400 화소)에 맞춰 크기 조정, 종횡비 0.5~2 | 됨(순수 torch, DINOv2 ViT-L 백본 내장. 속도 미측정) | https://github.com/javrtg/AnyCalib , 가중치 `releases/download/v1.0.0/anycalib_{pinhole,gen,dist,edit}.pt` |
| Perspective Fields (CVPR 2023) | **Adobe Research License: 비상업 전용, 재배포 금지** | 같음 | roll·pitch·vfov(+ 주점). 렌즈 왜곡 없음 | — | — | https://github.com/jinlinyi/PerspectiveFields |
| WildCamera (NeurIPS 2023) | Apache-2.0 | 확인 못 함(Hugging Face, 라이선스 표기 없음) | 핀홀 4자유도(초점·주점). 왜곡 없음 | — | — | https://github.com/ShngJZ/WildCamera |
| 고전 기준(이 저장소) | 저장소 코드 | 없음 | OpenCV 방사 k1, k2 (plumb-line) + 직교 소실점 초점. 주점은 영상 중심 가정 | 원본(2304x1296) 그대로 또는 정수 배 축소 | 됨(numpy 만) | `plumbline.py` |

**채택(잠정).** C110 실제 프레임 비교 전까지의 잠정 결정이다. 기본 경로는 고전 기준(plumb-line + 소실점)으로 둔다. 이유는 모델·가중치·GPU 없이 돌고, 결과가 OpenCV 방사 모델 그대로라 번들 조정에 바로 넘길 수 있고, 실패를 직선성·소실점 일치로 스스로 판정할 수 있어서다. 아래 SIM 수치는 구현이 맞는지 보는 상한일 뿐 채택 근거가 아니다. 학습 모델은 같은 인터페이스의 비교·대체 경로로 둔다. 둘 중에는 **AnyCalib(`anycalib_dist`, `simple_radial:2`)** 을 1순위로 둔다: 코드·가중치 모두 Apache-2.0 이라 상업 제약이 없고, Brown-Conrady 왜곡 영상으로 학습해 C110 같은 광각 렌즈를 겨냥하며, `radial:2` 가 OpenCV `[k1, k2]` 와 같은 식이다. GeoCalib(`distorted`, `radial`)은 2순위다(가중치 CC-BY-4.0 은 상업 사용 가능하나 출처 표시 의무). Perspective Fields 는 비상업 라이선스이고 왜곡을 내지 않아 제외, WildCamera 는 왜곡을 내지 않고 가중치 라이선스가 불명이라 제외한다.

C110 실제 프레임에서 세 방법(고전·AnyCalib·GeoCalib)을 비교해 기본 경로를 확정하는 것은 남은 일이다(가중치는 Network Volume 에 두고 경로·해시만 기록).

## 구조

| 모듈 | 역할 |
|---|---|
| `estimate.py` | `IntrinsicsEstimator` 프로토콜(영상 → `IntrinsicsEstimate`), 결과(K, 왜곡, 불확실성, 방법·모델 버전, status), `WeightsRef`(경로 + sha256 확인), OpenCV 방사 모델 계산, 같은 광선 재투영 차이 |
| `lines.py` | 에지 사슬 검출(numpy): 가우시안 → Sobel → 비최대 억제 + 서브픽셀 → 방향이 맞는 이웃 연결 → 3차 다항식 잔차로 모서리 분할 |
| `plumbline.py` | 고전 기준: 사슬이 곧게 펴지도록 픽셀 단위 방사 계수 (b1, b2) 를 LM 으로 맞추고(이상치 사슬 반복 제거), 펴진 직선으로 RANSAC 소실점, 서로 맞는 직교 세 방향에서 f² = −(v1−c)·(v2−c) |
| `learned.py` | GeoCalib·AnyCalib 어댑터. 선택 의존성(torch + git 설치 패키지), 없으면 쓸 때 `LearnedBackendUnavailable`. 코드가 가중치를 내려받지 않는다 |
| `shared.py` | 같은 렌즈 4대: 초점·왜곡 중앙값으로 공통값, 카메라별 편차(같은 광선 재투영 p95), 이상치 표시 후 빼고 다시 합침, 과반 합의가 없으면 비움 |
| `record.py` | 기록(`simai.intrinsics_estimate` 0.1.0, input_provenance 포함)과 번들 조정(-104) 초기값 `BundleAdjustmentInit` |

**왜곡 표현.** 픽셀 단위 방사 모델 ρ_d = ρ_u (1 + b1 ρ_u² + b2 ρ_u⁴) (ρ 는 반대각선 D 로 나눈 반지름)는 OpenCV 방사 모델과 k1 = b1 (f/D)², k2 = b2 (f/D)⁴ 로 정확히 같다. 직선성은 초점과 무관하므로 왜곡은 초점 없이 정해지고, 초점을 못 정한 카메라의 왜곡도 공유 단계에서 쓴다.

**모르면 비운다.**
- 초점: 서로 맞는 직교 소실점 셋(쌍별 초점 차이 5% 이내)이 없으면(정면·수평 카메라, 비직교 방향, 맞지 않는 모델) 초점을 비우고 `NO_FOCAL` 로 둔다. 쌍 하나로 정하는 것은 `require_orthogonal_triple=False` 로만 켜고 메모를 남긴다.
- 왜곡: 편 뒤 직선성 RMS 가 1px 를 넘거나, 펴기 전 1px 넘게 휘어 있었는데 개선 비율이 0.5 보다 크거나, 계수가 허용 범위(b1 ∈ [−1.5, 1], b2 ∈ [−1, 2]) 밖이거나 영상 안에서 접히거나, 직선 길이 70% 이상을 펴지 못하면 `FAILED` 로 두고 왜곡도 비운다(곡선만 있는 장면에서 왜곡을 지어내지 않는다).
- 학습 모델: AnyCalib `success` 가 False 이거나 값이 NaN·음수·초점 범위 밖이면 `FAILED`(값 비움).
- 공유: `FAILED` 카메라는 합치지 않는다. 편차는 같은 광선 재투영 p95 를 반대각선으로 나눈 값(해상도 무관, 대략 초점 상대 차이의 2배)이다. 0.08(초점 약 4%)까지는 같은 렌즈의 정상 산포로 보고, 이상치 한도는 min(max(0.08, 3×중앙값), 0.15) 다. 이상치를 뺀 뒤 과반이 남지 않거나 남은 카메라의 편차 중앙값이 0.08 을 넘으면 `NO_CONSENSUS` 로 공통값을 비운다(2대 대 2대, 1대 대 1대). 공통 초점이 없으면 번들 조정 초기값을 만들지 않는다.
- 위 기준값(직선성 1px, 개선 비율 0.5, b 범위, 길이 비율 0.7, 편차 0.08·0.15)은 SIM 으로 정한 초기값이며 C110 실제 프레임(압축 잡음·흐림)으로 다시 확인할 대상이다.

**계약.** contracts/ 는 바꾸지 않았다. 결과의 `camera` 는 `calibration.schema.json` 의 `camera_calibration` 중 내부 파라미터 부분(camera_id, image_size, K, distortion `OPENCV_PINHOLE [k1, k2, 0, 0, 0]`)이고, 외부 파라미터는 이 단계에서 모르므로 없다. 기록 형식은 모듈 형식이다(provider_source 를 적지 않는다. 보정 버전이 아니다).

## SIM 시험 결과 (상한)

시뮬레이터 basic 시나리오 카메라(C110 사양, 가로 105°)로 직선만 있는 합성 교실을 렌더했다(사람·실제 영상 없음). **입력이 완벽할 때의 상한이며 제품 성능이 아니다**(is_upper_bound). 해상도 1152x648(절반), 정답 초점 441.98px.

| 경우 | 초점 오차 | k1 / k2 (정답) | 직선성 RMS 전 → 후 | 같은 광선 재투영 p95 |
|---|---|---|---|---|
| 광각 배럴 k1 −0.30, k2 0.09, 4대 | 0.01~0.025% | −0.2996~−0.2998 / 0.0897~0.0900 | 2.57 → 0.002~0.009 px | 0.04~1.0 px |
| 약한 배럴 + 접선(p1 0.0005, p2 −0.0003) | 0.36% | −0.1204 / 0.0203 (−0.12 / 0.02) | 1.22 → 0.022 px (정답 모델 0.018) | 3.4 px |
| 왜곡 없음 | 0.06% | 0.0017 / −0.0002 | 0 → 0.017 px | 1.8 px |
| 같은 렌즈 4대 공유 | 0.02% | −0.2997 / 0.0898 | — | 카메라별 0.10~0.62 px |
| cam4 만 좁은 화각(80°) | 공통값 0.02% | — | — | cam4 328 px → 이상치 표시 |

- 접선 왜곡은 모델링하지 않으므로 그만큼 초점이 치우친다(0.36%). 남는 차이는 번들 조정이 카메라별로 다듬는다.
- 원본 해상도(2304x1296) 1대 시험에서 초점 오차 0.07%, 렌더 약 8초·추정 약 7초(노트북 CPU)였다.
- 같은 광선 재투영 차이는 자세를 다시 맞추지 않은 엄격한 지표다.

## 발견 사항

`geometry.rig.RigCamera.normalize` 의 왜곡 제거(고정점 20회)는 강한 광각 배럴 왜곡(예: k1 −0.30, k2 0.09, f 884, 2304x1296)의 영상 모서리에서 수렴하지 않았다(모서리 픽셀이 수백 픽셀 어긋난 광선으로 돌아감). S15P31A101-166 에서 geometry 쪽을 구간 보호 뉴턴법(`geometry.distortion`)으로 고쳤고, 이 패키지의 `distort_normalized`·`undistort_normalized`·`undistort_radii`·`undistort_pixels` 도 같은 함수를 쓴다. 풀 수 없는 점(왜곡 식이 접히는 영역 등)은 NaN 이고, 같은 광선 재투영 차이는 기준 모델이 격자 안에서 접히면 inf 다.
