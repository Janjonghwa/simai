# 녹화 세션으로 외부 파라미터 보정 실행 설계

S15P31A101-214. 기획서 §4.2(경계별 변환), §4.3(자동 캘리브레이션 1~3단계, 번들 조정 구현 범위, 게이지 고정), §4.4(판정), §11.5(영상 비저장), §20.4(출처·상한), 요구사항 FR-23 기준이다.
코드는 `ai/src/simai_ai/calibration/extrinsics/`(+ `calibration/intrinsics/record.py` 의 `bundle_init_from_record`), 시험은 `ai/tests/calibration/extrinsics/`·`ai/tests/calibration/bundle/test_extrinsics_cli.py` 에 있다.
번들 조정·판정은 새로 만들지 않고 `calibration.bundle.adjust_human_bundle`(-104)·`CalibrationVersionStore.propose`(criteria)를 그대로 부른다.

## 명세 4.3 단계 대응

| 명세 | 이 도구 | 입력 → 출력 |
|---|---|---|
| 1단계 내부 파라미터(무마커) | 하지 않음. `intrinsics.cli` 기록을 읽음(`bundle_init_from_record`) | `simai.intrinsics_estimate` → `BundleAdjustmentInit`(같은 렌즈 공통값, 이상치·직선성 미확인 표시) |
| 1단계 MapAnything 상대 자세 | 하지 않음(GPU 세션). `cameras.json` 을 입력 파일로 받음(`initial.load_mapanything_cameras`) | cam2world(OpenCV, MapAnything world) → World → 카메라 (R, t) |
| 2단계 좌표계 고정 | -102 몫. `--world-alignment`(MapAnything world → World 닮음 변환)를 받거나, 없으면 임시 정렬(`alignment_from_people`) | World 기준 초기 외부 파라미터 |
| 3단계 정밀화 입력(2D keypoint) | `extract`: 녹화 → YOLO pose keypoint 파일. `keypoints.build_observations`: 한 명 대응·공통 시각 | `simai.keypoint_sequences` JSONL → `PersonObservation` |
| 3단계 정밀화 | `adjust_human_bundle` 그대로 | 카메라별 K·왜곡·R·t, 재투영 오차, 커버리지, 바닥 평면 차이, CCVS 검산, 퇴화 표시 |
| 4.4 판정·4.5 버전 | `HumanBundleResult.propose_to` 그대로(실행마다 새 메모리 저장소) | 계약 calibration 문서(`proposal.doc`), 실패 기준 목록 |

## 실행 순서 (교실 실제 실행)

1. 보정 걷기를 녹화한다(gateway/recorder, 4대, 한 명이 1~2분 방 전체를 걷는다. 다른 사람은 화면에 없게).
2. 내부 파라미터: `uv run python -m simai_ai.calibration.intrinsics.cli --session …` (이미 교실 결과 `intrinsics-20261021-022940z.json` 이 있다. 직선성 UNVERIFIED).
3. GPU 세션: 교실 사진 4대(`classroom-stills-20261021/`, 카메라마다 한 장)로 `run_mapanything.py` → `cameras.json`. 같은 세션에서 keypoint 추출도 돌린다:
   `uv run --extra io --extra detection python -m simai_ai.calibration.extrinsics.cli extract --session <녹화> --model yolo26x-pose.engine --device cuda:0`
4. -102 가 World 정렬을 내면 `--world-alignment` 로 넣는다. 없으면 임시 정렬로 돌고 버전 제안은 하지 않는다(종료 코드 4).
5. 노트북: `uv run python -m simai_ai.calibration.extrinsics.cli adjust --keypoints … --intrinsics … --cameras-json … [--view cam_A=cam_A_01 …] [--measured-objects …] [--walkable-area …]`
6. 결과(`ai/experiments/recordings/extrinsics/extrinsics-<UTC>.json`)의 bundle.status·flags, 카메라별 재투영 오차, proposal.failed_criteria 를 본다. UNCERTAIN 이면 걷기 재수행·정물 추가(4.3 대체 경로).

## keypoint 파일과 추출

- 파일은 JSONL 이다. 첫 줄 머리(형식·input_provenance·검출기·카메라별 영상 크기·집계), 다음 줄부터 프레임 한 장(시뮬레이터 `observations/*.jsonl` 과 같은 모양). 영상·이미지·crop 은 넣지 않는다. 같은 경로가 있으면 덮어쓰지 않는다.
- 녹화 시각: `frames.jsonl` 헤더의 `estimated_capture_time_ms` 를 그대로 쓴다. 바이트 위치(offset·size)로 접근 단위를 꺼내 `PyAvDecoder` 에 열쇠와 함께 넣고, 나온 그림은 그 열쇠의 헤더 시각을 받는다. 교실 녹화 4초 창에서 카메라별 실제 프레임 수가 89·102·104·95 장(공칭 25 fps 면 100 장)이었다. 공칭 fps 로 시각을 다시 만들면 카메라 사이가 몇 분 만에 1초 넘게 어긋난다.
- 창 시작 앞 마지막 키프레임부터 디코딩하고 창 안만 `every_n` 장마다 검출한다. DecodeError 는 세고 다음 키프레임까지 건너뛴다(메운 그림을 관측으로 쓰지 않는다).
- 출처는 `VideoFileFrameSource.from_manifest` 와 같은 규칙이다(frames FILE, dataset_id `recording/…`, 내부 테스트 녹화는 `recording-dev_test/…`). is_test_input 은 머리에 남긴다.
- 검출기: 운영 기본 YOLO26x-pose(Tier 0 와 같은 모델, 명세 4.3 "Tier 0 keypoint 재사용"), 노트북 확인은 작은 모델 허용. 모델 이름·가중치 sha256·imgsz·ultralytics 버전을 머리에 남긴다. `services/tier0/yolo_detector` 는 src 가 services 를 import 할 수 없어 쓰지 못하고, 같은 변환을 15줄로 다시 썼다(박스·신뢰도·keypoint 를 그대로 옮김).

## 사람 대응·시각 정렬

- 한 명 걷기 규칙: 카메라 한 대의 한 프레임에 신뢰도 0.5 이상인 사람이 정확히 한 명일 때만 그 사람을 보정 걷는 사람으로 본다. 두 명 이상 보인 프레임은 쓰지 않고 센다. 사람이 보인 프레임 중 20% 넘게 두 명 이상이면 경고한다.
- 공통 시각은 300 ms 간격이다(번들 조정 접지 판단의 이웃 표본 창 `stance_window_ms` 300 과 같게). 보간은 `geometry.interpolation`(창 200 ms, 시각 불확실성 25 ms 초과 프레임 제외, 외삽 없음)을 그대로 쓴다.
- 한 명 프레임이 있는 카메라가 2대 미만이거나 관측이 없으면 오류(종료 코드 3)다. 관측이 30 개 미만이면 경고한다.

## 초기 외부 파라미터와 World

- cameras.json: `convention` 에 cam2world·OpenCV 가 없으면 오류. 회전은 ‖RᵀR − I‖ ≤ 1e-3 이고 det > 0 이어야 하며(float32 잡음은 SVD 로 다듬음), 카메라 쌍 거리가 0.3~60 m 밖이면 단위가 미터가 아니라고 보고 오류다. R = Rcwᵀ, t = −R·c.
- view ↔ camera_id: `--view CAM=NAME` 또는 이름 자동 짝짓기(view 이름 = camera_id 또는 `camera_id_번호`). 카메라 하나에 view 가 여럿이면 오류(추측하지 않는다).
- `--world-alignment` 형식(-102 `world_frame.cli fix` 결과, `docs/design/world-frame-from-mapanything.md`): `{"format": "simai.world_alignment", "mapanything_to_world": 4×4 [sR | T]}`. det ≤ 0(왼손)이면 오류. `status` 가 OK 가 아니면(바닥 없음·축 모호) 오류, `cameras_json_sha256`·`inputs.intrinsics.sha256` 이 있으면 `--cameras-json`·`--intrinsics` 의 sha256 과 같아야 한다(다른 MapAnything 실행·다른 내부 파라미터 기록의 정렬 거절).
- 임시 정렬(INTERIM_PERSON_VERTICAL): 초기 카메라로 어깨·발목을 삼각측량해 발목 중점 → 어깨 중점 단위 벡터의 평균을 상방으로 둔다(25° 넘게 벗어난 표본을 빼고 세 번 다시 평균, 표본 20 개 미만이면 오류, 길이 0.6~2.2 m 밖 표본 제외). 바닥은 표본별 낮은 발목 높이의 중앙값 − 0.08 m(BundleConfig 발목 접지 높이). 벽 방향은 MapAnything X 축을 수평에 투영한 방향, 원점은 MapAnything 원점 아래다. 스케일은 바꾸지 않는다. 번들 조정 게이지(벽 방향·수평 원점·스케일을 초기값에 묶음)가 이 임시 축을 그대로 유지하므로, 이 결과는 버전으로 제안하지 않는다(4.1: World 축·원점은 첫 CALIBRATED 때 한 번 정함).

## 출처 (AGENTS 7, 명세 20.4 규칙 2·3)

keypoint 파일만이 아니라 모든 입력의 input_provenance 를 합친다(`initial.combine_input_provenance`). frames·tracks 는 keypoint 파일 머리 그대로이고, calibration 은 초기 카메라가 MapAnything 추정이므로 MODEL_ESTIMATE(`mapanything-<run_id>`)다. 내부 파라미터 기록·World 정렬·실측 정물 관측(선택 `input_provenance`) 중 하나라도 SIM·DATASET_GT 면 calibration 을 그 출처(SIM 우선)로 두어 결과 전체가 상한(is_upper_bound)이 되고, 번들 조정 provider_source 도 SIM·DATASET_GT 가 되어 CALIBRATED 가 될 수 없다. 입력별 상한 출처는 결과의 `input_sources` 에 남긴다. 각 입력 파일의 sha256(World 정렬은 행렬까지)도 남긴다.

## 종료 코드와 결과 파일

| 코드 | 뜻 |
|---|---|
| 0 | 조정 CONVERGED, -102 World, 저장소가 후보를 받음(PROVISIONAL 또는 CALIBRATED) |
| 2 | 인자·입력 오류(형식, 단위, 영상 크기 불일치, 검출기 extras 없음, 결과 파일이 이미 있음 등). 결과 파일을 쓰지 않는다 |
| 3 | 대응 불가, 임시 정렬 표본 부족, 조정 UNCERTAIN·FAILED, 저장소 거절. 결과 파일에 사유 |
| 4 | 임시 World 라 제안하지 않음. 조정 결과는 결과 파일에 있다 |

결과 파일·keypoint 파일은 덮어쓰지 않는다(AGENTS 6). 결과 파일(`simai.extrinsics_run` 0.1.0, 계약이 아닌 이 도구 형식): input_provenance, 입력 파일 이름·sha256, 대응 집계, world_frame, bundle(번들 조정 기록 그대로), cameras(계약 camera_calibration 형식), proposal(adopted·state·failed_criteria·reasons·doc) 또는 proposal_skipped.

## SIM 시험 결과 (상한, 한 명 24초 걷기, 4대, COCO17 만, 교란 2°·0.15 m·초점 +4%·k1 +0.03)

| 항목 | 값 |
|---|---|
| 재투영 오차 중앙값 / p95 | 0.64~0.70 px / 1.65~1.80 px |
| 카메라 쌍 상대 회전 오차 (초기 → 결과) | 0.78~3.92° → 0.002~0.024° |
| 카메라 높이 오차 (-102 정렬) | −0.013 ~ −0.017 m |
| 내려다보는 각 오차 | ±0.012° |
| 카메라별 회전 오차 | 2.0° → 0.84°(남은 몫은 게이지로 묶은 공통 yaw: 초기값 평균 벽 방향) |
| 판정 | PROVISIONAL (SOURCE·SCALE_PRIMARY 등 미달. SIM 은 값과 무관하게 CALIBRATED 불가, 스케일 MODEL_ESTIMATE) |
| 임시 정렬 | CONVERGED, 카메라 높이는 cameras.json 스케일(1.03)을 이어받음, 제안 안 함 |
| 두 사람 내내 함께 걷기 | 한 명 프레임 0 → 대응 오류, 조정·제안 안 함 |

교실 녹화(rec-20261021-031045z-classroom-dev-test, 우연히 찍힌 사람, 내부 시험용)로는 추출 단계만 4초 창·5장마다·yolo26n-pose CPU 로 돌려 형식을 확인했다(결과 파일은 scratchpad, 커밋 안 함). 두 카메라는 사람이 없고 두 카메라는 내내 여러 명이 보여 대응 오류가 났다(한 명 걷기 규칙이 의도대로 거름).

## 로비 걷기 이상치 (S15P31A101-313)

로비 보정 걷기(rec-20261028-020819z-walk-calibration, 약 3분, YOLO26x-pose, 3장마다 = 약 8 Hz)로 `adjust` 를 돌리면 UNCERTAIN(TOO_MANY_OUTLIERS)이었다: 관측 14,714 중 6,542(44.5%) 제거(허용 10%), 1차 풀이 재투영 중앙값 3.3~6.1 px·p95 35~45 px. 원인은 둘이었다.

### 원인 1: 카메라 PTS 시각선이 깨졌다 (4대 모두)

- frames.jsonl 의 source_pts 가 13장 묶음마다 앞뒤로 약 370~440 ms 번갈아 어긋난다(+410 ms, −360 ms 점프가 번갈아). 묶음 안 간격(40 ms)과 장기 비율(40.006 ms/장)은 맞다. 녹화의 추정 촬영 시각은 이 PTS 를 따르므로(PTS 시간선 대비 p95 수 ms) 같이 틀린다. "도착 지연 400~750 ms 흔들림"은 사실 이 어긋남이다: 수신 시각은 frame_sequence 에 선형이고(하한 위 흔들림 p99 75~180 ms) 촬영 시각만 묶음째 400 ms 앞당겨졌다.
- 영상 내용은 frame_sequence 순서가 맞다: 엉덩이 중점 120 ms 걸음 크기 중앙값이 frame_sequence 순서 16 px, 촬영 시각 순서 43 px(p90 29~39 px 대 94~122 px). 디코더는 패킷마다 일련번호를 붙여 그림을 열쇠에 묶으므로 그림↔줄 대응은 맞다.
- 결과: 촬영 시각으로 정렬하면 두 묶음이 섞여 보간이 0.4 s 떨어진 두 순간을 잇고, 공통 시각 595 개 중 4대 모두 보인 시각이 184 개뿐이었다(OUTSIDE_WINDOW 1,376).
- Lead 분석의 "cam_A 만 +55 ms" 는 깨진 PTS 시간선 위의 값이다. frame_sequence 시계로 다시 매긴 뒤 기하로 잰 카메라별 보정값은 −4·+6·−4·+4 ms(A·B·C·D)이고, 반씩 나눠 재면 ±20 ms 안에서 흔들린다(앞 반 +4·−10·−4·+8, 뒤 반 −18·+22·−4·−2).

### 원인 2: 관절마다 검출 잡음이 다른데 기준은 하나였다

시각을 바로잡고(같은 8 px 기준) 1차 풀이 잔차를 관절별로 나누면 머리(코·눈·귀) 중앙값 1.0~1.6 px·8 px 초과 1~3%, 어깨·팔 2.3~3.3 px·9~16%, 엉덩이·무릎 5.1~6.0 px·32~40%, 발목 3.5~4.0 px·24~29% 다. 카메라별 평균 벡터는 0.0~1.4 px 로 치우침이 없고(무작위 잡음), 좌우 뒤바뀜으로 설명되는 관측은 0, 뼈 길이 사전을 꺼도 같다(18.2% → 18.3%), 신뢰도와도 거의 무관하다. 이 비율은 COCO keypoint 평가의 관절별 표준편차 κ(사람 표기 흔들림)와 같다: κ 를 눈 κ(0.025)로 나눈 배율(엉덩이 4.28, 무릎 3.48, 어깨 3.16, 코 1.04)로 잔차를 나누면 관절별 중앙값이 0.9~1.6 로 고르다. 같은 8 px 기준은 엉덩이·무릎의 정상 관측을 1/3 넘게 버린다.

### 수정

| 단계 | 위치 | 내용 |
|---|---|---|
| 시각선 다시 매기기 | `extrinsics/sync.repair_timelines` | 카메라·epoch 마다 이웃 행의 촬영 시각 차이가 frame_sequence 차이 × 프레임 간격에서 150 ms 넘게 벗어난 비율이 2% 를 넘으면, 촬영 시각을 frame_sequence 선형 시계(최소제곱 기울기, 잔차 중앙값 절편)로 다시 매긴다. 원래 값은 `estimated_capture_time_ms_original` 에 남긴다. keypoint 파일만으로 한다 |
| 카메라별 시각 보정값 | `extrinsics/sync.estimate_time_offsets` | 초기 카메라(MapAnything, World 정렬 전)로 카메라 하나를 빼고 삼각측량한 관절을 그 카메라에 투영해, keypoint 를 보정값만큼 옮겨 보간한 위치와의 오차(30 px 에서 자른 평균, 2차 영상 변형을 뺀 뒤)가 가장 작은 값을 ±400 ms 격자(10 ms → 2 ms)로 찾는다. 3번 돌아가며 반복하고 평균 0 으로 묶는다 |
| 보정값 적용 | `keypoints.build_observations(time_offsets_ms=…)` | 모든 카메라 값이 정해졌을 때만 촬영 시각에 더한 뒤 공통 시각에 맞춘다 |
| 관절별 잡음 | `bundle/skeleton.KEYPOINT_NOISE_SCALE`, `BundleConfig.joint_noise_scaling`(기본 켬), `solver.Problem.obs_scale` | 재투영 잔차를 pixel_sigma × 배율로 나누고, 이상치 기준도 outlier_px(8) × 배율로 둔다(눈 8 px, 엉덩이 34 px). 정물 끝점은 배율 1 |
| 실행 도구 | `cli adjust` | 위 두 단계를 기본으로 하고 `--no-time-sync` 로 끌 수 있다. 결과 파일 `timing`(시각선 점검·보정값 근거·적용값), `correspondence.time_offsets_ms`, `bundle.outliers.joint_noise_scaling` |

명세에 없어 정한 수치와 근거:

- 깨진 걸음 150 ms·2%: B 프레임 재정렬(1~2장, 80 ms 이하)·수신 흔들림보다 크고 로비 어긋남(370~440 ms)보다 작다. 로비 4대 모두 깨진 걸음 22.9% 였다. 드문 끊김·재연결은 2% 안이다.
- 보정값 둔함 검사 0.5 px: 최적 ±50 ms 점수가 최적보다 0.5 px 이상 나빠야 정한다. 로비는 1.1~1.9 px 였다. 초기 보정 오차가 크고 걷는 길이 한 방향 고리이면 위치에 따른 오차와 시각 오차가 섞여 둔해진다(합성 장면 2°·15 cm 교란에서 0.2~0.4 px, 값이 수십~수백 ms 틀림). 그때는 정하지 않고(모르면 비운다) 경고만 남긴다.
- 관절별 배율은 데이터로 다시 재지 않는 고정값(COCO κ)이다. 데이터에서 잡음을 재 기준을 넓히면 보정이 나쁠 때 기준도 같이 넓어져 이상치 비율 검사(10%)가 무력해진다. 고정 배율이면 눈·코(배율 1)는 여전히 8 px 기준이고, 40~80 px 검출 실수 25% 시험(`test_too_many_outliers_is_uncertain`)도 그대로 불확실이다. outlier_px 8·max_outlier_ratio 10% 는 바꾸지 않았다. 발 끝(19점 입력)은 COCO-WholeBody 엄지발가락 κ 0.068.

### 로비 결과 (같은 keypoint 파일, -102 World 정렬 wf-lobby.json)

| 조합 | 제거 비율 | 상태 |
|---|---|---|
| 수정 전 (PTS 시각, 같은 8 px) | 44.5% | UNCERTAIN |
| 관절별 잡음만 | 13.7% | UNCERTAIN |
| 시각 바로잡기만 | 24.9% | UNCERTAIN |
| 둘 다 (기본) | 1.0% | CONVERGED → PROVISIONAL |

기본 실행: 4대 모두 시각선 다시 매김, 보정값 −4·+6·−4·+4 ms, 공통 시각 관측 328 → 589(4대 모두 보인 시각 184 → 561), 관측 32,198 중 325(1.0%) 제거, 접지점 781, 바닥 평면 차이 0.019 m, CCVS 높이 검산 차이 0.2~0.9 cm. 재투영은 모든 관절·원본 픽셀이다.

| 카메라 | 재투영 중앙값 / p95 (px) | 높이 MapAnything → 조정 (m) | 내려다보는 각 변화 (°) | 회전 변화 (°) | 중심 이동 (m) | 초점 (px, 변화) |
|---|---|---|---|---|---|---|
| cam_A | 3.13 / 12.6 | 2.112 → 2.095 (−0.016) | −0.01 | 0.06 | 0.019 | 1272.5 (+13.5) |
| cam_B | 3.84 / 15.9 | 2.113 → 2.096 (−0.017) | +0.12 | 0.55 | 0.051 | 1294.8 (+35.8) |
| cam_C | 3.44 / 12.3 | 2.065 → 2.170 (+0.104) | −0.77 | 0.80 | 0.119 | 1306.5 (+47.5) |
| cam_D | 3.23 / 12.1 | 2.101 → 2.148 (+0.047) | −0.55 | 0.66 | 0.055 | 1264.7 (+5.6) |

- 판정은 PROVISIONAL 이다. 미달 기준: REPROJECTION_MEDIAN(≤ 2 px)·REPROJECTION_P95(≤ 5 px), SCALE_PRIMARY(정물 없음, MODEL_ESTIMATE), CHECKPOINT_COUNT·HEAD_HEIGHT(검산점 없음), FLOOR_COVERAGE(걷기 영역 미입력), PAIRWISE_POSITION. 재투영 지표는 계약대로 원본 픽셀·모든 관절로 잰 값이라 엉덩이·무릎 잡음(σ 약 6 px)이 그대로 들어간다. 수정 전 1.3~1.7 px·p95 6.5 px 는 관측 44% 를 버린 뒤의 값이라 비교 대상이 아니다. 재투영 기준을 관절 배율로 나눈 값이나 머리·어깨 관절로 잴지는 판정 기준(4.4) 문제라 이 이슈에서 바꾸지 않았다(Lead 판단).
- 새 보정 문서는 덮어쓰지 않고 새 버전(`cal-walk-<UTC>`, PROVISIONAL, G1_AUTO)으로 scratchpad 에 두었다(사람이 찍힌 녹화에서 나온 값이라 커밋하지 않음). 저장소 반영은 이 도구 밖이다.

### 시험

- `ai/tests/calibration/extrinsics/test_sync.py`(합성 4대, 시뮬레이터 없음): 깨진 PTS 묶음 → 다시 매김, 멀쩡한 시각선(흔들림 ±15 ms, B 프레임 80 ms)은 그대로, 한 카메라 +150 ms → 보정값 차이 −150±6 ms, 다시 매긴 뒤 +90 ms 어긋남까지 카메라 사이 6 ms 안, 이미 맞으면 0, 2대·점 부족이면 정하지 않음, 보정값 적용.
- `ai/tests/calibration/bundle/test_sim_bundle.py::test_joint_noise_scaling_keeps_noisy_hips_as_inliers`: κ 비례 잡음(눈 σ 1.5 px, 엉덩이 6.4 px) + 검출 실수 2% → 같은 기준은 17.0% 제거·UNCERTAIN, 관절별 기준은 2.1%·CONVERGED(높이 6 cm·내려다보는 각 0.3° 안).
- `ai/tests/calibration/bundle/test_extrinsics_cli.py`: 3대 PTS 묶음 420 ms 어긋남 → `--no-time-sync` 13.3% 제거·UNCERTAIN, 기본 0.1%·CONVERGED(재투영 중앙값 2 px 이하, 높이 6 cm 안). SIM 이라 상한이다.

## 버린 대안

- 깨진 PTS 를 녹화 수신 시각(received_at_ms)의 하한 선형 시계로 다시 매기기: 수신 시각이 frame_sequence 에 더 잘 맞지만(기울기 차 0.01 ms/장, 끝에서 약 ±20 ms) 녹화 세션이 있어야 하고 keypoint 파일 형식이 바뀐다. keypoint 파일만으로 다시 매기고 카메라 사이 상수 차이는 기하 보정값으로 정했다. 녹화기 PTS 처리 자체는 gateway 범위다(넘긴 일).
- 데이터에서 관절별 잡음을 재 기준을 넓히기(MAD): 보정이 나쁘면 기준도 넓어져 이상치 비율 안전장치가 무력해진다.
- 시각 보정값을 번들 조정 변수로 넣기: 해석적 야코비안·보간 미분이 새로 필요하다. 로비는 다시 매긴 뒤 보정값이 ±6 ms 라 격자 탐색으로 충분했다.
- 허용치 완화(8 px → 15 px, 10% → 30%): 머리 관절의 실제 이상치까지 받아들이고 보정이 나쁜 경우를 거르지 못한다.
- `VideoFileFrameSource.from_manifest` 로 읽기: 촬영 시각을 시작 시각 + index/공칭 fps 로 다시 만들고 세그먼트를 하나만 읽는다. 보정에는 카메라 간 시각이 중요해 헤더 시각을 쓰는 PyAvDecoder 경로를 택했다(io 는 고치지 않음, -215 범위).
- Cross-view 연결로 여러 명 대응: 초기 보정이 거친 상태에서의 대응·오대응 처리가 새 범위다. 명세 4.3 은 한 명 걷기로 시작하므로 한 명 규칙으로 줄였다.
- MapAnything 바닥 평면·중력으로 상방 정하기: cameras.json 에 바닥·중력이 없다(점군은 GPU 세션 산출물). 사람 세로축(CCVS 와 같은 가정)은 keypoint 만으로 된다.
- 임시 World 결과도 제안: 판정이 축·원점 미고정을 알 수 없어, 스케일 근거가 갖춰지면 임시 축으로 CALIBRATED 가 될 수 있다. 제안하지 않는 쪽을 택했다.

## 잔여 위험·넘긴 일

- RECEIVE_ESTIMATED 시각(불확실성 20 ms)의 카메라 간 실제 오차는 재지 않았다. 1.4 m/s 걷기에서 20 ms 는 약 3 cm 다. 시계 차이(-160)·PTS 기반 시각이 들어오면 그대로 따라간다.
- 한 명 규칙은 한 카메라에만 다른 사람이 보이는 경우(서로 다른 두 사람을 각각 한 명씩 본 카메라)를 막지 못한다. 번들 조정의 초기 재투영 150 px 거름·강건 손실·이상치 제거가 거르지만, 보정 걷기 때 다른 사람이 화면에 없어야 한다.
- 교실 실제 실행은 아직이다(보정 걷기 녹화, MapAnything cameras.json, -102 World 정렬이 필요). 교실 녹화에서 YOLO26x-pose 추출은 GPU 세션 큐 항목이다.
- World 정렬 파일 형식은 -102 가 정했다(위). 형식이 바뀌면 `load_world_alignment` 만 고친다.
- 결과 파일의 proposal.doc 을 운영 보정 저장소(EC2)에 넣는 경로는 이 도구 밖이다.
- (-313) 깨진 PTS 는 녹화만의 문제가 아니다. 같은 카메라의 LIVE 추정 촬영 시각도 PTS 를 따르면 Tier 0 의 카메라 간 시간 보간(5.2 1단계)이 프레임 절반에서 약 0.4 s 어긋난 순간을 잇는다. 녹화기·I0 수신의 촬영 시각 모델(gateway·io)을 고치는 일은 이 이슈 밖이라 Lead 에 넘긴다.
- (-313) 다시 매긴 카메라의 절편은 PTS 두 수준 중 행이 많은 쪽을 따른다. 카메라 사이 상수는 기하 보정값이 정하지만, 보정값이 둔해 비워지면(초기 보정이 나쁘거나 한 방향 고리 걷기) 깨진 카메라와 멀쩡한 카메라 사이에 최대 약 200 ms 가 남을 수 있다. 그때는 이상치 비율 검사가 UNCERTAIN 으로 막는다. 보정 걷기는 방을 여러 방향으로 가로지른다.
- (-313) 로비 보정값은 반씩 나눠 재면 ±20 ms 안에서 흔들린다(1.4 m/s 에서 약 3 cm). keypoint 파일 시각으로 맞춘 프레임 간격이 수신 시각 기준과 0.01 ms/장 다르다(3분 녹화 끝에서 약 ±20 ms, 4대가 같은 쪽이라 카메라 사이 차이는 약 5 ms).
- (-313) 재투영 판정 기준(중앙값 2 px·p95 5 px)은 모든 관절의 원본 픽셀이라, 엉덩이·무릎 검출 잡음 때문에 실제 걷기 보정은 CALIBRATED 에 닿기 어렵다(로비 3.1~3.8 px·p95 12~16 px). 기준을 관절 배율로 나눈 값으로 잴지는 4.4 판정 기준 결정이다.
