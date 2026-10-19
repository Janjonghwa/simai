# 무마커 내부 파라미터 추정 설계

S15P31A101-116, S15P31A101-192(기본 경로 결정·실행 도구). 기획서 §4.3 1단계(교실 직선 단일 영상 추정 → 같은 렌즈 4대 초기값 공유 → 3단계 번들 조정에서 카메라별 정밀화), 부록 B(모델·라이선스), §11.5(원본 영상 비저장), 요구사항 FR-23 기준이다.
코드는 `ai/src/simai_ai/calibration/intrinsics/`, 시험은 `ai/tests/calibration/intrinsics/` 에 있다. 마커·체커보드(ChArUco)는 어떤 경로에서도 쓰지 않는다.

## 후보 조사 (2026-10-17 확인)

| 후보 | 코드 라이선스 | 가중치 라이선스 | 카메라 모델(왜곡) | 입력 해상도 | CPU 추론 | 출처 |
|---|---|---|---|---|---|---|
| GeoCalib (ECCV 2024) | Apache-2.0 | CC-BY-4.0 (출처 표시) | `pinhole`, `simple_radial`(k1), `radial`(k1, k2), `simple_divisional`(강한 어안). 주점은 영상 중심 고정 | 320 으로 크기 조정, 32 의 배수(`resize: 320`, 기준 변은 코드에서 확인 못 함) | 됨(코드가 CUDA 없으면 CPU. 속도 미측정) | https://github.com/cvg/GeoCalib , 가중치 `releases/download/v1.0/geocalib-{pinhole,distorted}.tar` |
| AnyCalib (ICCV 2025) | Apache-2.0 | Apache-2.0 | 모델 무관 FoV 필드 → `pinhole`, `radial:k`(Brown-Conrady k1~k4), `kb:k`(Kannala-Brandt), `ucm`, `eucm`, `division:k`. 주점도 추정 | 학습 해상도 약 320²(102,400 화소)에 맞춰 크기 조정, 종횡비 0.5~2 | 됨(순수 torch, DINOv2 ViT-L 백본 내장. 속도 미측정) | https://github.com/javrtg/AnyCalib , 가중치 `releases/download/v1.0.0/anycalib_{pinhole,gen,dist,edit}.pt` |
| Perspective Fields (CVPR 2023) | **Adobe Research License: 비상업 전용, 재배포 금지** | 같음 | roll·pitch·vfov(+ 주점). 렌즈 왜곡 없음 | — | — | https://github.com/jinlinyi/PerspectiveFields |
| WildCamera (NeurIPS 2023) | Apache-2.0 | 확인 못 함(Hugging Face, 라이선스 표기 없음) | 핀홀 4자유도(초점·주점). 왜곡 없음 | — | — | https://github.com/ShngJZ/WildCamera |
| 고전 기준(이 저장소) | 저장소 코드 | 없음 | OpenCV 방사 k1, k2 (plumb-line) + 직교 소실점 초점. 주점은 영상 중심 가정 | 원본(2304x1296) 그대로 또는 정수 배 축소 | 됨(numpy 만) | `plumbline.py` |

**채택(S15P31A101-192, 2026-10-19 사용자 결정).** 세 방법(고전·AnyCalib·GeoCalib) 비교 실험은 하지 않는다. 기본 경로는 **AnyCalib(`anycalib_dist`, `radial:2`)**, 예비는 **GeoCalib(`distorted`, `radial`)**, 그다음이 고전 기준이다. 근거: AnyCalib 은 Brown-Conrady 왜곡 영상으로 학습해 C110 같은 광각 렌즈를 겨냥하고, `radial:2` 는 OpenCV `[k1, k2]` 와 같은 식이며(fx, fy, cx, cy, k1, k2), 주점도 추정한다. 코드·가중치 모두 Apache-2.0 이다. 공식 벤치마크로 우열이 나와 있어 재측정하지 않는다(운영 GPU 기준 판단 원칙). GeoCalib 은 가중치 CC-BY-4.0(출처 표시 의무)이고 주점을 영상 중심으로 고정한다. 결과는 초기값이며 최종값은 번들 조정(-104, 4.3 3단계)이 카메라별로 다듬는다. Perspective Fields 는 비상업 라이선스이고 왜곡을 내지 않아 제외, WildCamera 는 왜곡을 내지 않고 가중치 라이선스가 불명이라 제외한다.

이전 판(S15P31A101-116)의 "고전 기준 잠정 기본, C110 실제 프레임에서 세 방법 비교 후 확정"은 위 결정으로 대체됐다. 고전 기준 코드는 지우지 않고 역할을 둘로 정리했다: (1) 가중치·torch 없이 도는 마지막 예비 경로, (2) 학습 결과의 직선성 확인 기준(아래 "백엔드 순서"). 고전 기준은 실패를 직선성·소실점 일치로 스스로 판정할 수 있어 확인 기준으로 맞다.

## 백엔드 순서 (`chain.py`)

| 경우 | 동작 | 기록 |
|---|---|---|
| AnyCalib 이 돌아 OK | 직선성 확인 후 그대로 | method `ANYCALIB`, `backend.selected` anycalib, `fallback` false |
| AnyCalib 사용 불가(`LearnedBackendUnavailable`: torch·anycalib 없음, 가중치 경로를 주지 않음, 로딩 단계의 가중치 파일 없음·해시 불일치. `learned` 가 로딩 경계에서 이 예외로 바꾼다) | 다음 백엔드(GeoCalib → 고전 기준) | `attempts[]` 에 UNAVAILABLE 과 사유, `fallback` true, `run.unavailable_backends` |
| 학습 백엔드 모두 사용 불가 | 고전 기준 값 | method `PLUMB_LINE_VP`, model 없음, `fallback` true. AnyCalib 결과처럼 보이지 않는다 |
| 순서에 고전 기준이 없고(`--backends anycalib` 등) 모두 사용 불가 | 비움 | method `NONE`, status FAILED |
| 백엔드가 돌았는데 FAILED(모델이 실패를 알림, 초점 범위 밖) 또는 추론 중 예외(이미 읽은 모델의 추론에서 올라온 FileNotFoundError 포함) | **대체하지 않고 비움** | 그 백엔드의 method, status FAILED, 사유 |
| 학습 결과가 OK 인데 직선성 확인 실패 | 비움 | method 는 그 학습 모델, status FAILED, `straightness_check` FAILED·RMS·한도 |

- **실패는 대체하지 않는 이유.** 사용 불가는 환경 문제(설치·가중치)라 다른 방법으로 넘어가도 같은 영상을 다른 방법으로 본 것뿐이다. 반면 모델이 그 영상에서 실패했다면 그 영상 자체가 추정하기 어렵다는 근거이고, 더 약한 방법으로 채우면 "모르면 비운다"를 어긴다. 실패한 카메라는 같은 렌즈 공유 단계가 빼고 합치므로(4대 중 과반) 공통 초기값은 여전히 나온다. 버린 대안: 실패 시에도 다음 백엔드로 넘어가기(채워진 값의 출처가 섞이고 실패 근거가 지워짐).
- 사용 불가로 판정된 백엔드는 같은 실행의 다른 카메라에서 다시 시도하지 않는다(가중치 sha256 을 카메라마다 다시 계산하지 않는다).
- AnyCalib `cam_id` 기본값을 `simple_radial:2` 에서 `radial:2` 로 바꿨다(fx·fy 를 따로 추정하고 평균을 쓰며 차이가 1% 를 넘으면 메모).

**직선성 확인.** 고전 기준을 같은 배경 영상에 돌려, 이상치 사슬을 뺀(곧게 펴진) 교실 직선을 학습 결과의 K·왜곡·주점으로 펴고 직선 맞춤 수직 거리 RMS(원본 픽셀, 사슬마다 원본/펴진 길이 비를 곱해 고전 기준과 같은 단위)를 잰다.

| 수치 | 값 | 근거 |
|---|---|---|
| 절대 한도 `max_rms_px` | 1.0 px | 고전 기준 자신의 실패 기준(`max_straightness_px`)과 같은 초기값 |
| 상대 한도 `reference_factor` | 고전 기준 RMS × 3 | 압축 잡음·흐림으로 고전 기준 RMS 자체가 커진 실제 영상에서 학습 결과를 과하게 지우지 않게 |
| 개선 비율 | 펴기 전 RMS > 1 px 이면 편 뒤 RMS ≤ 0.5 × 펴기 전 | 고전 기준 자신의 실패 기준(`max_improvement_ratio`)과 같다. 학습 결과를 예비 경로보다 느슨하게 보지 않는다 |
| 접힘 | 직선 위 점에서 왜곡 제거 불가면 실패 | 영상 안에서 접히는 모델은 쓸 수 없다 |

SIM 측정(광각 k1 −0.30·k2 0.09, 1152x648 cam1, 펴기 전 1.58 px, 고전 기준 0.117 px, 상한): 정답 모델 0.117 px 통과, 왜곡 0(k1 = k2 = 0) 1.58 px 실패, 왜곡 절반 1.09 px 실패, 왜곡 0.6배 0.91 px 는 1 px 안이지만 개선 비율로 실패, 왜곡 0.7배 0.71 px 통과, 초점만 5% 틀림 0.22 px 통과. 즉 이 확인은 왜곡을 크게 놓친 결과를 거르는 장치이고 초점 오차는 거의 잡지 못한다(초점은 같은 렌즈 공유 편차와 번들 조정이 다룬다).

- 고전 기준이 직선을 충분히 찾지 못해(FAILED) 확인할 수 없으면 학습 결과를 지우지 않고 `UNVERIFIED` 로 남긴다. 확인할 수 없는 것과 확인에 실패한 것은 다르다(학습 모델은 직선이 적은 장면에서도 추정할 수 있다). 버린 대안: 확인 불가도 실패로 비우기(직선이 적은 화면의 카메라를 근거 없이 버림).
- 확인은 학습 결과가 OK 일 때만 하고, 고전 기준 결과는 자기 자신으로 확인하지 않는다.
- 위 한도는 SIM 으로 정한 초기값이다. C110 실제 배경 영상에서 고전 기준 RMS 와 학습 결과 RMS 를 기록으로 남기므로 첫 실행 뒤 다시 본다.

**번들 조정으로 넘기는 표시.** 직선성 확인을 하지 못한(UNVERIFIED) 카메라는 `to_bundle_adjustment_init` 이 `CameraIntrinsicsInit.flags` 에 `STRAIGHTNESS_UNVERIFIED` 를 붙인다(값은 지우지 않는다). 한계: 지금 번들 조정 풀이(`calibration.bundle.solver`)는 `principal_point_fixed` 와 무관하게 주점을 초기값으로 고정하고 초점·k1·k2 만 다듬는다. AnyCalib 이 추정한 주점의 오차는 번들 조정이 고치지 않는다.

**같은 렌즈 공유의 주점.** AnyCalib 은 주점을 추정한다. 합친 카메라 중 주점을 추정한 카메라가 있으면 그 주점의 성분별 중앙값을 공통 주점으로 쓰고 `principal_point_assumed` false 로 둔다(이전에는 영상 중심에 두면서 가정 아님으로 표시될 수 있었다). 모두 가정이면 영상 중심이다.

## 실행 도구 (`capture.py`·`runner.py`·`cli.py`)

`ai/` 에서 `uv run python -m simai_ai.calibration.intrinsics.cli` (PyAV 는 dev 그룹·extras io 에 있다). 입력은 카메라 RTSP(`--session` 의 gateway/recorder 세션 TOML `[[cameras]]`, 또는 `--rtsp CAM=HOST[:PORT][/PATH]`)나 시험용 영상 파일(`--file CAM=PATH`)이고 여러 대를 동시에 받는다.

1. 카메라마다 스레드 하나로 PyAV(RTSP 는 TCP)를 열어 `--duration`(기본 45 s) 동안 `--interval`(기본 3 s) 간격으로 프레임을 메모리로 고른다(최대 `--max-frames` 30장, 2304x1296 BGR 한 장 약 9 MB). 간격은 영상 시각(pts) 기준이고 pts 가 없으면 단조 시계다. 벽시계 상한은 duration + 30 s.
2. 화소별 시간 중앙값으로 배경을 만든다(행 묶음 계산). 지나가는 사람은 지워진다.
3. 카메라별로 백엔드 순서대로 추정 → 같은 렌즈 공유 → 기록(`simai.intrinsics_estimate` 0.2.0) JSON 하나만 쓴다. 기본 위치 `ai/experiments/recordings/intrinsics/`(.gitignore).

- **영상 비저장(명세 11.5).** 수업 중인 사람이 찍히므로 프레임·배경·중간 결과를 디스크(임시 폴더 포함)에 쓰지 않는다. 배경은 추정 뒤 버린다. 기록에는 파라미터·수집 요약만 있고 `run.images_stored` false 다. 시험이 임시 폴더를 빈 폴더로 돌려 실행 뒤에도 비어 있고 작업·출력 폴더에 기록 말고 새 파일이 없음을 확인한다. 얼굴 관련 처리는 없다.
- **파일 입력은 로컬 일반 파일만.** 경로 문자열을 FFmpeg 에 넘기면 프로토콜 접두어를 해석한다(`cache:file:<영상>` 은 읽은 영상 바이트를 작업 폴더의 `ffcache*` 에 쓴다. 리뷰에서 재현). 그래서 일반 파일인지 확인한 뒤 파이썬 바이너리 핸들로 PyAV 에 넘긴다(프로토콜 해석 없음). RTSP 는 `rtsp://` 로 만든 주소만 연다.
- **RTSP host 검사.** `CameraInput` 생성 경계에서 host 를 호스트 이름·IPv4·[IPv6] 형식으로만 받고, 계정(`user:pass@`)·포트·경로·공백이 섞이면 값을 보여 주지 않고 거부한다(`--session`·`--rtsp` 모두. repr·기록·로그에 남지 않는다). path 도 `@`·공백을 받지 않는다.
- **벽시계 상한의 범위.** 입력을 연 뒤부터 재고 프레임이 나올 때마다 검사한다. 열기 시간은 빠지고, 프레임이 오지 않는 동안은 PyAV open·read timeout(기본 10 s)이 막으므로 실제 종료는 상한 + 최대 read timeout 까지 늦을 수 있다.
- **디코더.** services/tier0 의 `PyAvDecoder`(-182)는 I0 압축 프레임(Annex B + codec_info)을 받는 디코더라 RTSP 디먹싱이 없다. 이 도구는 PyAV 의 `av.open` 디먹싱·디코딩을 직접 쓴다(gateway/recorder 의 RTSP 열기와 같은 옵션). ai 패키지는 gateway 패키지를 import 하지 않는다(import-linter `no-gateway` 계약 추가). 세션 TOML 은 같은 형식을 직접 읽는다.
- **계정.** 저장소 루트 `.env` 의 `RTSP_USER_<CAM>`/`RTSP_PASS_<CAM>`(없으면 `RTSP_USER`/`RTSP_PASS`), 환경 변수가 먼저다(gateway/recorder 와 같은 규칙). PyAV 예외 메시지에는 계정이 든 URL 이 그대로 들어 있어서(실측: `ExitError(..., 'rtsp://user:pass@…')`) 예외 문자열을 쓰지 않고 예외 종류만 남긴다. 계정이 없는 카메라는 FAILED 와 키 이름만 남는다. `--rtsp` 값에 `@` 가 있으면 값을 보여 주지 않고 거부한다.
- **모르면 비운다.** 열기·디코딩 실패, 고른 프레임이 `--min-frames`(기본 5) 미만, 수집 중 영상 크기 변경이면 그 카메라는 FAILED 이고 추정하지 않는다(영상 크기를 모르므로 `estimates` 에 넣지 않고 `cameras[]` 에 사유). 이미 충분한 프레임을 고른 뒤 끊기면 그 프레임으로 배경을 만들고 사유를 남긴다.
- **입력 출처.** RTSP 는 `input_provenance.frames.source` LIVE, 파일은 `--frames-source`(FILE 기본, 합성 영상이면 SIM → is_upper_bound true). 한 실행에 RTSP 와 파일을 섞지 않는다(출처가 하나여야 한다). 카메라별 입력 설명(host·port·path 또는 파일 이름, 계정 없음), 사용 프레임 수·디코딩 프레임 수·고른 시간 범위, 백엔드 경로, 실패 사유는 `cameras[]` 에, 수집 설정·백엔드 순서는 `run` 에 있다.
- 종료 코드: 0 기록을 썼고 OK 카메라가 있음, 3 기록은 썼지만 OK 카메라가 없음, 2 인자·설정 오류.

**잔여 위험.** 시간의 절반 넘게 같은 자리에 앉아 있는 사람은 중앙값 배경에 남는다. 사람 윤곽은 곡선이라 고전 기준은 이상치 사슬로 빼지만 학습 모델 결과에 얼마나 영향이 있는지는 실제 영상에서 확인하지 않았다(가능하면 빈 교실·쉬는 시간에 실행). 학습 모델 실제 추론(torch·가중치)은 이 저장소에서 돌려 보지 않았다. 출력 텐서 순서·`success` 처리는 AnyCalib 코드를 읽고 맞췄다(`test_learned.py` 의 실제 추론 시험은 패키지·가중치가 있을 때만 돈다).

**실제 카메라 실행 절차(남은 일).**
1. 노트북에 torch(CPU)와 AnyCalib 을 설치한다: `uv pip install torch "anycalib @ git+https://github.com/javrtg/AnyCalib"` (pyproject·uv.lock 에는 넣지 않았다. 아래 "남긴 일").
2. 가중치 `anycalib_dist.pt` 를 한 번 내려받아(현장 LTE 가 아닌 망에서) sha256 을 재고 `docs/data/` manifest 에 경로·해시를 적는다. 가중치는 커밋하지 않는다.
3. 저장소 루트 `.env` 에 카메라 계정, `gateway/recorder/sessions/classroom.toml` 에 host 를 둔다.
4. `cd ai && uv run python -m simai_ai.calibration.intrinsics.cli --session ../gateway/recorder/sessions/classroom.toml --anycalib-weights <경로> --anycalib-sha256 sha256:<hex>` (빈 교실이면 좋다).
5. 출력 기록에서 카메라별 `method` 가 ANYCALIB 인지, `straightness_check` 상태·RMS, `fallback`, 공통값 합의를 본다. 학습 백엔드가 없으면 `fallback` true 와 사유가 남는다(그 값은 고전 기준 값이다).

**남긴 일.** torch·anycalib·geocalib 는 PyPI 배포가 없어(git 설치) pyproject extras 로 선언하면 uv.lock 이 torch 휠 메타데이터·git 저장소를 풀어야 해서 이번에는 넣지 않았다. 기록 형식(`simai.intrinsics_estimate`)의 contracts/ 계약화는 하지 않았다(번들 조정 -104 와 함께 제안). AnyCalib 초점 불확실성(`intrinsics_icovs`)은 아직 옮기지 않는다.

## 구조

| 모듈 | 역할 |
|---|---|
| `estimate.py` | `IntrinsicsEstimator` 프로토콜(영상 → `IntrinsicsEstimate`), 결과(K, 왜곡, 불확실성, 방법·모델 버전, status), `WeightsRef`(경로 + sha256 확인), OpenCV 방사 모델 계산, 같은 광선 재투영 차이 |
| `lines.py` | 에지 사슬 검출(numpy): 가우시안 → Sobel → 비최대 억제 + 서브픽셀 → 방향이 맞는 이웃 연결 → 3차 다항식 잔차로 모서리 분할 |
| `plumbline.py` | 고전 기준: 사슬이 곧게 펴지도록 픽셀 단위 방사 계수 (b1, b2) 를 LM 으로 맞추고(이상치 사슬 반복 제거), 펴진 직선으로 RANSAC 소실점, 서로 맞는 직교 세 방향에서 f² = −(v1−c)·(v2−c) |
| `learned.py` | GeoCalib·AnyCalib 어댑터. 선택 의존성(torch + git 설치 패키지), 없으면 쓸 때 `LearnedBackendUnavailable`. 코드가 가중치를 내려받지 않는다 |
| `shared.py` | 같은 렌즈 4대: 초점·왜곡(·추정한 주점) 중앙값으로 공통값, 카메라별 편차(같은 광선 재투영 p95), 이상치 표시 후 빼고 다시 합침, 과반 합의가 없으면 비움 |
| `record.py` | 기록(`simai.intrinsics_estimate` 0.2.0, input_provenance 포함, 실행 도구의 `cameras`·`run` 절)과 번들 조정(-104) 초기값 `BundleAdjustmentInit` |
| `chain.py` | 백엔드 순서(AnyCalib → GeoCalib → 고전 기준), 사용 불가만 대체, 학습 결과의 직선성 확인, 경로 기록 |
| `capture.py` | RTSP·파일 → 메모리 프레임 선택 → 시간 중앙값 배경(디스크에 쓰지 않음) |
| `runner.py`·`cli.py` | 여러 카메라 동시 수집 → 추정 → 같은 렌즈 공유 → 기록 JSON |

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
