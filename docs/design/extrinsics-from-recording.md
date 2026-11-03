# 녹화 세션으로 외부 파라미터 보정 실행 설계

> **상태 안내 (기획서 v1.3 수정 54·56, S15P31A101-438·-443):** 이 문서는 초기 걷기/사람 관절 기반 외부 파라미터 보정(`-214`·`-331`·`-352`)과 관제 중 사람 관절 후보 보정(`-356`, `LiveCandidateCalibrationRunner`)의 당시 설계·실험 기록이다. 현행 정본 보정 파이프라인은 **걷기·사람 관절 보정을 폐지**하고 **사람을 지운 정적 배경 특징점 V2 공동 번들 조정**과 **작업 중 배경 비교 중심 고정 회전 자동 복구(`rotfix`) 단일 경로**를 쓰는 [`docs/design/site-setup-pipeline.md`](site-setup-pipeline.md) 이다(이 문서의 구성 요소 중 **2회 연속 개선 확인(`CandidateConfirmation`)과 교체 후 60초 감시·자동 롤백(`SwapProbation`) 안전 게이트만 배경 기반 `rotfix` 에 재사용**하며, 사람 관절 기반 `drift_watch`·`monitor` 사람 LOO·`live_candidate`·`live_calibration`·`LiveCandidateCalibrationRunner` 경로는 명세에서 제거됨).

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

## 4대 공통 렌즈와 실측 카메라 높이 축척 (S15P31A101-321)

명세 4.3 3단계(정밀화: 카메라별 내부 파라미터·왜곡 계수를 함께 다듬음, 번들 조정 구현 범위), 4.3 스케일 근거 우선순위(실측 정물 > 카메라 높이 실측 > …, 카메라 높이 실측은 2대 이상·카메라끼리 일치할 때만, v1.3 수정 32), 4.4(판정) 기준이다. 계약은 바꾸지 않았다: 스케일 근거 값 `MEASURED_CAMERA_HEIGHT` 는 calibration 2.1.0(-277)에 이미 있고, 렌즈 모드·카메라별 비는 결과 파일의 도구 기록(`simai.human_bundle_adjustment` 0.2.0)에만 둔다.
코드는 `ai/src/simai_ai/calibration/bundle/`(`solver._Reduction`, `inputs.BundleConfig.lens_mode·fixed_lens·camera_height_*`, `checks.camera_height_scale`, `adjust_human_bundle(measured_camera_heights=…)`)와 `extrinsics/cli.py`(`--lens`, `--fixed-lens`, `--camera-heights`), 시험은 `ai/tests/calibration/bundle/test_solver.py`·`test_checks.py`·`test_sim_bundle.py`·`test_extrinsics_cli.py` 의 -321 절이다.

### 왜

Lead 분석(2026-10-29, 로비 걷기 한 세션, 실행 중 solver 를 바꿔 끼운 공유판)에서 카메라별 렌즈(cal-walk)와 4대 공통 렌즈(shared)를 비교했다.

| 지표 | 카메라별(walk) | 공통(shared) |
|---|---|---|
| 렌즈 | f 1264.7~1306.5 px, k1 −0.261~−0.326 | f 1284.27 px, k1 −0.29198, k2 0.05982 |
| 3대 이상 융합(Tier 0 쌍 판정, 전체) | 87.1% | 84.3% |
| 4대 융합 | 52.9% | 50.7% |
| 앞·뒤 절반으로 맞춘 판 사이 카메라 위치 차이 | 2.9~4.5 cm | 0.8~1.4 cm |
| 카메라 간 시야각 폭(광축에서 1150 px) | 1.35° (같은 카메라 절반 사이 변화 0.83°) | 0 |

카메라별 렌즈의 이득(융합 1~4 pt)은 대부분 절반 교차의 흔들림(1.6~2.6 pt) 크기이고, 카메라별 초점이 카메라 위치와 섞여(C 의 초점 +47 px 와 높이 +10 cm) 절반마다 위치를 3~4 cm 흔든다. 같은 모델 4대라 공통 렌즈가 더 안정하다. 다만 H2 교차의 3대 이상 융합 −4 pt 는 흔들림 밖이라 "렌즈 차이가 없다"를 증명하지는 못했다. 그래서 기본값은 카메라별(이전 동작) 그대로 두고 공통·고정을 고를 수 있게 했다.

같은 날 줄자로 잰 바닥 → 렌즈 중심 높이는 A 2.21·B 2.195·C 2.23·D 2.24 m 다(D 는 걷기 녹화 뒤 거치대를 옮긴 다음 잼). 공통판 높이(A 2.108·B 2.107·C 2.154)와의 비는 1.048·1.042·1.035 로 서로 0.7% 안에서 맞고, 정물 없이 MapAnything 메트릭을 이어받은 축척이 약 4% 작다는 근거가 된다.

### 렌즈 모드

| 모드 (`BundleConfig.lens_mode`, `--lens`) | 변수 | 내부 파라미터 사전 | 시작값 |
|---|---|---|---|
| `per_camera` (기본) | 카메라마다 ω·C·f·k1·k2 (이전 동작, 풀이 경로 그대로) | 카메라마다 σ_f·σ_k | 내부 파라미터 기록 |
| `shared` | 카메라마다 ω·C + 공통 f(=fx=fy)·k1·k2 | 카메라마다 행을 두되 σ × √(카메라 수) | 카메라 값 평균 |
| `fixed` | 카메라마다 ω·C | 상수(풀이에 영향 없음) | `--fixed-lens F,K1,K2`(4대 공통), 없으면 기록 값 그대로 |

- 주점은 어느 모드든 기록 값 고정이다(사람 관측으로는 회전과 섞인다, 이전과 같음). 접선 왜곡은 쓰지 않는다.
- 풀이: 잔차·해석 야코비안은 전체 변수로 그대로 계산하고, 변수만 줄인다(x_full = expand(x_red), J_red = J_full · T, T 는 0/1 행렬). 점 변수는 맨 뒤라 Schur 소거·LM·이상치 2라운드·게이지·바닥·판정은 바뀌지 않는다. `per_camera` 는 줄이지 않는다(T 없음).
- 기록: `bundle.lens`(모드, 공통 값 또는 고정 값의 출처 `INTRINSICS_INIT`·`CONFIG`), `bundle.config.lens_mode`. 보정 문서는 계약 그대로 카메라마다 K·왜곡을 싣는다(공통이면 4대가 같은 값). provider_version 은 `simai-human-bundle-0.2.0`.
- `fixed` 의 쓰임: 공통판 렌즈(1284.27, −0.29198, 0.05982)를 다음 걷기·카메라 이동 뒤 재보정에 그대로 쓰고 R·t 만 다시 맞출 때.

### 실측 카메라 높이 축척

입력은 world_frame(-277)과 같은 `simai.camera_heights` 파일(`--camera-heights`, 카메라별 바닥 → 렌즈 중심 m, 선택 `method`·`measured_at`·`input_provenance`)이다. keypoint 파일에 없는 카메라는 입력 오류(종료 2, 이름 오타)다.

1. 번들 조정을 끝까지(이상치 2라운드) 돈 뒤 카메라별 조정 높이 = World Z(바닥 Z=0 은 발 접지점이 정함)를 잰다.
2. `checks.camera_height_scale`: 비 = 실측 / 조정 높이, 근거 값 = 중앙값. 쓰지 않는 경우(이유를 `rejected` 에): 바닥을 접지점으로 정하지 못함(접지점 3개 미만이라 바닥 게이지가 초기값), 조정 높이가 바닥 위가 아닌 카메라, 쓸 수 있는 카메라 2대 미만, 중앙값에서 3% 넘게 벗어난 카메라가 있음(카메라별 이유를 `exceeding` 에, 그 카메라만 빼지 않고 근거 전체를 버림 — 명세 4.3·-277 과 같은 처리). 카메라별 잔차 = 중앙값 × 조정 높이 − 실측(m)을 함께 기록한다.
3. 실측 정물 제약이 없으면 World 를 원점 기준으로 그 배율만큼 늘린다(카메라 중심·점·뼈 길이, 회전 그대로). 바닥 Z=0·원점·벽 방향은 그대로이고 재투영은 바뀌지 않는다. 그 뒤 바닥 평면 차이·CCVS 검산·문서를 늘린 World 로 낸다. 근거 `ScaleEstimate(MEASURED_CAMERA_HEIGHT, 최종 World 에서 본 비 = 1, sample_count = 쓴 카메라 수)` 를 넣어 문서 `scale_basis` 가 `MEASURED_CAMERA_HEIGHT` 가 된다(CALIBRATED 주 근거가 아니라 판정은 SCALE_PRIMARY 미달 그대로).
4. 실측 정물이 있으면 정물이 축척을 정하고(우선순위가 높다) 카메라 높이 비는 늘리지 않고 교차 확인 근거로만 넣는다(정물과 5% 안이면 `scale_cross_check_basis`).
5. 쓰지 못하면 축척을 바꾸지 않고 기록 `CAMERA_HEIGHT_SCALE_REJECTED`(notes)와 이유를 남긴다. 상태(CONVERGED/UNCERTAIN)는 바꾸지 않는다(다음 근거로 가는 것이지 조정 실패가 아님).

기록: `bundle.scale.camera_heights`(실측·조정 높이·비·편차·잔차·한도·쓰지 않은 이유·적용 배율·적용 뒤 높이), `bundle.scale.source`(`MEASURED_OBJECT`·`MEASURED_CAMERA_HEIGHT`·`INITIAL_EXTRINSICS`), `inputs.camera_heights`(경로·sha256·값·출처).

명세에 없어 정한 수치와 근거:

- 공통 사전 σ × √n: 같은 사전 행 n 개가 공통 변수 하나에 걸리면 사전이 n 배로 세진다. σ 를 √n 배 넓히면 중심이 카메라 f0 평균, σ 그대로인 사전 하나와 같다. 로비에서 사전을 사실상 끈 판(σ × 100)도 f 1284.31·k (−0.29204, 0.05986)로 거의 같아(Lead 분석) 값은 데이터가 정한다.
- 높이 한도 2대·3%: -277 World 고정과 같은 값과 근거(줄자 ±2 cm·렌즈 중심 ±1~2 cm·바닥 평면 오차 → 카메라 한 대 약 1.1%(1σ)의 2~3σ). 로비 공통판은 0.64% 였다.
- 사후 닮음 확대: 사람 관측만으로는 축척이 게이지(재투영 불변)라 조정 뒤 늘린 것과 축척을 제약으로 넣고 푼 것이 같다. 다른 점은 절대 길이로 둔 접지 높이 가정(발 끝 2 cm·발목 8 cm)뿐이고 배율 4% 에서 0.3 cm 이하다.

### 로비 재현 (같은 입력, 노트북 CPU 약 43 초)

`adjust --keypoints keypoints-walk-20261028.jsonl(sha256 117b22af…) --intrinsics intrinsics-20261021-022940z.json --cameras-json <s4lobby cameras.json> --world-alignment wf-lobby.json --lens shared --camera-heights <A·B·C>`:

| 항목 | 값 |
|---|---|
| 상태 | CONVERGED → PROVISIONAL, 이상치 1.23%, 바닥 평면 차이 0.0168 m(늘린 World) |
| 공통 렌즈 | f 1284.285 px, k1 −0.291975, k2 0.059806 (Lead 공유판 1284.27, −0.29198, 0.05982) |
| 조정 높이 → 비 | A 2.1078 → 1.0485, B 2.1070 → 1.0418, C 2.1539 → 1.0353 (D 는 실측에서 뺌) |
| 근거 | 배율 1.041789, 퍼짐 0.64%, 잔차 A −1.41·B 0.00·C +1.39 cm, 늘린 뒤 D 2.239 m(실측 2.24) |
| 문서 | scale_basis MEASURED_CAMERA_HEIGHT, 재투영 중앙값 3.20·3.95·3.60·3.25 px |

기본(`--lens` 없음, 높이 없음)으로 같은 입력을 돌리면 cal-walk 문서(K·R·t·왜곡)와 차이 0.0 이다(카메라별 경로가 바뀌지 않음). D(조정 높이 2.1495)의 비는 1.0421 이라 넣어도 중앙값은 1.0419 로 거의 같다. D 는 녹화 뒤 거치대를 옮겼으므로 그 녹화로 만든 보정에는 넣지 않았다.

### 시험

- `test_solver.py`: 공통·고정 모드의 줄인 야코비안 = 유한 차분, 공통 시작값 = 카메라 평균·사전 σ ×2(4대), 고정 모드는 렌즈가 그대로, 카메라별 모드는 줄이지 않음(같은 잔차), 모르는 모드 거절.
- `test_checks.py`: 중앙값·잔차(보정 − 실측), 2대 미만·모르는 카메라, 한 대 +9% → 그 카메라와 이유를 남기고 버림, 바닥 미정·바닥 아래 카메라·0 m 입력 오류.
- `test_sim_bundle.py`(SIM 상한): 같은 렌즈 4대에서 공통 모드가 초점 +4%·k1 +0.03 교란을 정답 대비 f −0.003%·k1 +0.0024·k2 −0.0046 로 되찾고 4대가 한 값, 고정 모드는 K·왜곡을 바꾸지 않음(값을 주면 그 값, 주점은 기록), 기본은 카메라별(4대 초점이 다름), 초기 스케일 +5% 에서 정답 높이 3대로 축척 → 4대 모두 높이 2 cm 안·문서 MEASURED_CAMERA_HEIGHT·SCALE_PRIMARY 미달, 1대·한 대 +8% 면 축척을 바꾸지 않음(높이 없을 때와 같은 t), 정물이 있으면 정물이 주 근거·카메라 높이는 교차 확인.
- `test_extrinsics_cli.py`: `--lens shared --camera-heights` 끝까지(문서 MEASURED_CAMERA_HEIGHT, 입력 해시 기록), `--fixed-lens` 만·잘못된 형식·모르는 카메라 높이 → 종료 2.

### 버린 대안 (-321)

- 실행 중 solver 를 바꿔 끼우는 공유판(Lead 분석 방법): 재현은 되지만 저장소 밖 코드라 운영 경로가 아니다. 같은 식(J·T)을 solver 의 모드로 옮겼다.
- 공통 렌즈를 기본값으로: 절반 교차에서 흔들림 밖 차이가 한 번 있어 렌즈 개체 차이를 배제하지 못했다. 기본 전환은 걷기 세션을 더 본 뒤 Lead 결정으로 넘긴다.
- 카메라 높이를 풀이 제약(높이 사전 행)으로 넣기: 사람 관측만으로 축척이 게이지라 결과가 같고, 야코비안·게이지 행을 새로 만들어야 한다. 정물 제약과 함께 들어가면 우선순위(정물 > 카메라 높이)가 가중치 문제로 바뀐다.
- 어긋난 카메라만 빼고 나머지로 축척: 명세 4.3 은 카메라끼리 한도보다 어긋나면 그 근거를 버리고 다음 근거로 간다. 빼는 규칙은 2대·3대에서 어느 쪽이 틀렸는지 모른다.
- 2단계(-102, MapAnything 바닥 평면) 높이와 비교: 3단계 조정이 카메라 위치를 수 cm 옮긴다(C +10 cm). 문서에 실리는 카메라 높이는 3단계 결과라 그 값과 비교해야 문서의 축척이 실측과 맞는다.
- 공통 주점·접선 왜곡 변수화: 사람 관측으로는 회전과 섞여 정해지지 않는다(이전 결정 그대로).

### 남은 위험 (-321)

- 근거는 로비 걷기 한 세션(한 사람, 약 3분)이다. 절반 교차는 같은 기하를 공유해 초점–위치 흡수를 가르지 못한다. 카메라 개체 사이 렌즈 차이를 완전히 배제하지 못했다(H2 교차 3대 이상 융합 −4 pt 가 흔들림 밖). 다른 배치·다른 날 걷기로 다시 볼 일이다.
- 주점·접선 왜곡의 개체 차이는 두 판 모두 공통 고정이라 비교하지 않았다.
- 조정 높이의 바닥 Z=0 은 발 접지 가정에 기댄다. 로비는 발목 기준(FLOOR_FROM_ANKLES, 가정 8 cm ± 2 cm)이라 바닥이 2 cm 치우치면 높이 비가 약 1% 함께 치우친다(카메라끼리 같은 방향이라 일치 검사로 잡지 못함). 실측 정물이나 키를 아는 사람으로 교차 확인하기 전에는 PROVISIONAL 근거로 본다.
- 카메라 높이 실측은 렌즈 중심 위치를 눈으로 정한다(±1~2 cm). 로비 잔차 ±1.4 cm 는 그 크기다.
- MEASURED_CAMERA_HEIGHT 는 CALIBRATED 주 근거가 아니다. 이 축척으로도 판정은 PROVISIONAL 이다(SCALE_PRIMARY 미달).

### 넘긴 일 (-321)

- 명세 4.3 정리: 3단계 "카메라별 내부 파라미터"에 공통·고정 렌즈 선택을 적을지, 카메라 높이 비를 "2단계 바닥 평면"이 아니라 3단계 조정 결과(발 접지 바닥)에서도 잰다는 점을 명세 정리 MR 에 넘긴다.
- 운영 기본 렌즈 모드(공통으로 바꿀지)와 보정 문서에 렌즈 모드를 실을지(계약 필드)는 Lead 결정.
- 공통판 결과의 보정 버전 반영(EC2)은 이 도구 밖이다.

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
- (-313) 깨진 PTS 는 녹화만의 문제가 아니다. 같은 카메라의 LIVE 추정 촬영 시각도 PTS 를 따르면 Tier 0 의 카메라 간 시간 보간(5.2 1단계)이 프레임 절반에서 약 0.4 s 어긋난 순간을 잇는다. 녹화기·I0 수신의 촬영 시각 모델(gateway·io)을 고치는 일은 이 이슈 밖이라 Lead 에 넘긴다. → S15P31A101-315 에서 노트북 중계·녹화 도구·녹화 재생에 실시간 감지·프레임 시계를 넣었다(`docs/design/capture-time-broken-pts.md`).
- (-313) 다시 매긴 카메라의 절편은 PTS 두 수준 중 행이 많은 쪽을 따른다. 카메라 사이 상수는 기하 보정값이 정하지만, 보정값이 둔해 비워지면(초기 보정이 나쁘거나 한 방향 고리 걷기) 깨진 카메라와 멀쩡한 카메라 사이에 최대 약 200 ms 가 남을 수 있다. 그때는 이상치 비율 검사가 UNCERTAIN 으로 막는다. 보정 걷기는 방을 여러 방향으로 가로지른다.
- (-313) 로비 보정값은 반씩 나눠 재면 ±20 ms 안에서 흔들린다(1.4 m/s 에서 약 3 cm). keypoint 파일 시각으로 맞춘 프레임 간격이 수신 시각 기준과 0.01 ms/장 다르다(3분 녹화 끝에서 약 ±20 ms, 4대가 같은 쪽이라 카메라 사이 차이는 약 5 ms).
- (-313) 재투영 판정 기준(중앙값 2 px·p95 5 px)은 모든 관절의 원본 픽셀이라, 엉덩이·무릎 검출 잡음 때문에 실제 걷기 보정은 CALIBRATED 에 닿기 어렵다(로비 3.1~3.8 px·p95 12~16 px). 기준을 관절 배율로 나눈 값으로 잴지는 4.4 판정 기준 결정이다.

## 회차 영상의 여러 사람으로 외부 파라미터 갱신 (-331)

관련 명세는 §4.1~4.5, §5.2의 카메라 간 연관·삼각측량과 §20.4의 출처 규칙이다. §4.3에는 실행 도구의 단일인 제한이 아직 적혀 있다. 기존 `extract`/`adjust`의 한 명 규칙을 유지하고, 후속 다중인 대응을 별도 `session` 명령으로 구현했다. 이 구현 범위 차이와 요구사항 FR-23의 정리는 Lead의 명세 정리 MR로 넘긴다. I1 LIVE 계약과 운영 보정 전환은 바꾸지 않는다.

### 입력과 실행

```text
python -m simai_ai.calibration.extrinsics.cli session
  --recording <회차 녹화 디렉터리> --previous <직전 calibration.json>
  --camera-heights <실측 높이.json> --lens fixed
  --fixed-lens 1284.27,-0.29198,0.05982
  --model <로컬 YOLO pose 가중치> --out <새 디렉터리>
```

`--keypoints`로 같은 회차의 추출 캐시를 재사용할 수 있다. 회차 ID·카메라 집합·원본 해상도가 녹화와 같아야 한다. 카메라별 높이는 바닥→렌즈 중심 m이며, 로비 실측은 A/B/C/D = 2.21/2.195/2.23/2.24 m다. 렌즈 주점은 직전 보정을 계승하고 f/k1/k2는 위 공유값으로 고정한다. 영상과 이전 보정은 읽기만 하며, 결과 디렉터리가 있으면 거절한다.

### 대응과 최적화

1. `extract_keypoints`는 원래도 모든 검출을 기록한다. 단일인 선택을 호출하지 않고, 카메라별 기존 `CameraLocalTracker`(기본 TrackTrack, Re-ID 꺼짐)에 모두 넣는다. 검출 배열 순서를 사람 ID로 쓰지 않는다.
2. 기존 `interpolate_camera`로 공통 시각(기본 `300 ms`, CLI `--sample-period-ms` 조정 가능)을 만든다. 보간 창 200 ms, 촬영 시각 불확실성 상한 25 ms, 외삽 금지와 양쪽에서 관측된 관절만 보간하는 규칙을 그대로 쓴다. 깨진 PTS 점검은 기존 `repair_timelines`다. 단일인용 시각 오프셋 추정은 부르지 않는다.
3. `CrossViewAssociator`와 기본 `CrossViewConfig`로 묶고, 애매하지 않은 2대 이상 클러스터를 `(association_id,t)`의 `PersonObservation`으로 바꾼다. 활성 카메라가 2대 미만인 빈 시각은 `CrossViewAssociator._previous`가 비워져 시간 연속 ID가 끊기지 않도록 연관 호출에서 건너뛴다. 이 ID는 보정용 대응 ID이며 World ID/재식별 정본이 아니다. bundle은 이미 사람별 뼈 길이·접지 시계열을 분리하므로 입력/solver 계약을 넓힐 필요가 없었다.
4. 카메라별 평균 쌍 통과율이 가장 높은 카메라의 80% 미만이면 회전 초기값 복구를 시험한다. 이는 운영 게이트 완화가 아니라 비싼 초기화의 대상 선정이다. 대상을 뺀 다른 카메라들의 연관 결과에서 2대 이상 관측된 3D 관절(재투영 12 px 이내·광선 교차각 5° 이상)을 얻고, 대상 로컬 궤적 × 기준 3D 궤적 후보를 각각 PnP RANSAC(`SOLVEPNP_EPNP`·`SOLVEPNP_SQPNP` 중 인라이어가 많은 해)으로 맞춘다. 3대 기준 리그에서는 다중인 시 2대 클러스터에 `TWO_VIEW_COMPETITOR`가 붙을 수 있으므로 후보 수집 단계에서는 제외하지 않고 PnP RANSAC과 4대 전체 연관 점수로 거른다. 어깨~발목만 써서 머리 방향에 따른 검출 편향을 초기 회전에 넣지 않는다.
5. PnP 후보는 최소 3시각·30점, 인라이어 30점 및 45% 이상·재투영 12 px(운영 `CrossViewConfig.residual_gate_px`와 동일), 회전 20° 이내·중심 이동 0.75 m 이내다. 7° 거치대 회전과 수~수십 cm 평행이동을 함께 겪은 실제 카메라를 찾되 다른 사람 대응으로 방 반대쪽에 생기는 PnP 해를 버리기 위한 초기값 범위다. 채택 위치는 PnP의 이동이 아니라 기존 `realign_rotation`으로 맞춘 중심 고정 회전이며, 그 회전에서도 12 px 안의 관측이 50% 이상이어야 한다. 전체 기본 게이트 연관 점수(애매하지 않은 다중 카메라 클러스터의 view 수 제곱 합)가 좋아진 후보만 사용한다.
6. 연관→`adjust_human_bundle`(fixed 렌즈 + 기존 실측 카메라 높이 축척)→새 보정으로 재연관을 2~3회 반복한다(기본 3회). 첫 라운드의 탈락/미연결 view도 원본 ticks에 남으므로 다음 라운드에서 다시 후보가 된다. 중간 UNCERTAIN은 다음 라운드의 후보 초기값으로만 쓰고 최종 제안은 막는다.

### 출력과 지표 정의

`report.json`은 입력·해시·출처, 로컬 추적/보간 탈락 수, PnP 후보별 채택/탈락 사유, 라운드별 기본 게이트 통과율·융합 비율·bundle 원본 기록, 카메라 변화 각도·위치 cm·재투영 중앙값/p95·높이 잔차를 담는 내부 진단 형식이다. 계약 필드를 추가하지 않았다.

쌍 통과율 분모는 해당 카메라 쌍의 **모든 사람 후보 조합**이다. 사람 2명이 잘 분리돼도 정답 쌍 2/전체 후보 4 = 약 50%가 될 수 있으므로 대응 정확도라고 부르지 않는다. `fusion_3plus_ratio`/`fusion_4_ratio`는 2대 이상 클러스터 중 3대 이상/4대 클러스터 비율이다. 단독 view가 빠져 좋아 보이는 경우를 드러내도록 전체 view 중 융합된 view 비율(`view_fusion_*`), 카메라 수별 클러스터 개수, 4대 모두 관측된 시각 기준 최대 클러스터 비율(`largest_cluster_*_4view_ratio`)도 함께 기록한다.

최종 bundle이 CONVERGED이고 전체 카메라가 있으며, 높이 축척이 기존 2대 이상·비 중앙값 대비 3% 일치 검사를 통과해 적용됐을 때만 `calibration.json`을 쓴다. `site_id`와 World 축 출처를 계승하고, 새 버전/시각과 실제 측정치로 문서를 새로 만들며 `document.content_hash`와 `providers.contract.check_calibration`의 스키마·기하·상태 검사를 통과시킨다. 독립 검산이 없어 PROVISIONAL이다. 불확실하면 보고서의 진단용 `candidate_cameras`만 남기고 새 보정 문서는 만들지 않는다. SIM/GT 입력은 결과도 SIM/GT·상한이며 G1_AUTO 성능으로 바꾸지 않는다.

### 2026-10-29 로비 다중인 회차 실행 (`rec-20261029-015512z-lobby-railing-pose`)

직전 보정 `cal-walkshm-20261029-033008z` + 실측 높이(`A 2.21, B 2.195, C 2.23, D 2.24 m`) + 고정 렌즈(`1284.27, -0.29198, 0.05982`) + 420~432 s 구간(`every_n=8`, 카메라당 38프레임, `--sample-period-ms 100`)으로 실행해 `cal-session-20261029-075820-459z`(`PROVISIONAL`, `scale_basis = MEASURED_CAMERA_HEIGHT`)를 얻었다(`ai/experiments/runs/session-calib-1029/result/`).

- **부트스트랩**: `cam_D` 초기 평균 쌍 통과율 `7.3%`(`A/D 0%`, `B/D 0%`, `C/D 21.9%`). 9개 궤적 후보 중 `cam_D-t2 × xv-1`(84점 중 인라이어 46점, PnP 회전 `7.21°`, 중심고정 인라이어 `54.8%`)이 전체 연관 점수를 `228 → 477`로 높여 채택됐고, 오대응 8개 후보는 `PNP_SUPPORT`·`CENTER_FIXED_RESIDUAL`로 기각됐다.
- **교대 조정**: 라운드 1(`52`개 사람/시각, `UNCERTAIN: LOW_IMAGE_COVERAGE:cam_B`) → 라운드 2(`66`개, `CONVERGED`) → 라운드 3(`66`개, `CONVERGED`, 바닥 평면 불일치 `0.0468 m`, 높이 배율 편차 `2.10% <= 3%`).
- **카메라별 변화량 및 잔차**:
  - `cam_A`: 회전 `0.85°`, 위치 `13.7 cm`, 보정 높이 `2.172 m`(실측 대비 `-3.8 cm`), 재투영 `4.20 / 11.75 px`
  - `cam_B`: 회전 `0.37°`, 위치 `4.5 cm`, 보정 높이 `2.163 m`(실측 대비 `-3.2 cm`), 재투영 `3.42 / 12.17 px`
  - `cam_C`: 회전 `0.82°`, 위치 `3.5 cm`, 보정 높이 `2.278 m`(실측 대비 `+4.8 cm`), 재투영 `3.06 / 14.38 px`
  - `cam_D`: 회전 `4.36°`, 위치 `6.2 cm`, 보정 높이 `2.273 m`(실측 대비 `+3.3 cm`), 재투영 `2.52 / 12.37 px`
- **전후 연관·융합 지표(운영 기본 게이트)**:
  - 쌍 통과율(전체 / strict): `A/B 48.0%/44.7% → 48.0%/44.1%`, `A/C 35.5%/29.1% → 37.3%/36.4%`, `A/D 0.0%/0.0% → 40.5%/34.4%`, `B/C 32.1%/16.7% → 35.2%/34.0%`, `B/D 0.0%/0.0% → 34.9%/33.6%`, `C/D 21.9%/20.2% → 34.2%/33.3%`. (회전만 재정렬했던 `cal-rot1029`에서는 `B/D·C/D = 0%`였으나 위치+회전 교대 조정으로 6개 쌍 모두 회복됨.)
  - 다중 뷰 클러스터 `3+`/`4`대 비율: `16.2% / 0.0% → 69.6% / 29.1%` (전체 view 기준 `13.5% / 0.0% → 70.4% / 34.5%`).
  - 4대 동시 관측 24시각 기준 최대 클러스터 `3+`/`4`대 비율: `45.8% / 0.0% → 100.0% (24/24) / 79.2% (19/24)`.

### 버린 대안과 남은 위험

- 단일인 구간만 찾기/따로 걷기: 이번 회차의 다중인 입력을 해결하지 못하므로 사용하지 않았다.
- 다른 사람까지 받도록 운영 에피폴라/재투영 게이트 완화: 잘못된 ID 대응을 정밀화에 넣을 수 있어 사용하지 않았다.
- 배경 특징점/호모그래피: 추가로 직전 회차 영상을 요구한다. 이번 도구는 현재 영상과 직전 보정만으로 가능한 궤적 PnP 경로를 선택했다. 배경 순수 회전과의 독립 교차 확인은 하지 않았다.
- PnP의 자유로운 이동을 그대로 채택: 오대응/검출 잡음을 위치로 흡수할 수 있어 초기화는 중심 고정 회전만 쓴다. 실제 위치 조정은 다시점 bundle이 담당한다.
- `SOLVEPNP_EPNP` 단독 사용: 서 있는 사람의 준평면 3D 관절 궤적에서 `EPNP`가 퇴화해 인라이어 0점을 내는 경우가 있어 `SOLVEPNP_SQPNP`를 함께 시험해 인라이어가 많은 해를 택했다.
- 녹화 시작 직후(`0~4.8 s`) 창 사용: `RECEIVE_ESTIMATED` 시계 필터 초기 수렴 전에는 `timestamp_uncertainty_ms > 25 ms`(29~63 ms)라 보간 규칙(`<= 25 ms`)에서 전량 탈락한다. 추출 창은 시계 수렴 이후(`>= 5 s`) 구간을 써야 한다.
- 사람이 적거나 다른 2대에서 동시에 잘 보이지 않으면 PnP 기준 궤적이 부족하다. 한 직선·정지·가림이 많은 경우 접지 분포/높이 검산 실패로 새 문서가 생성되지 않을 수 있다.
- 회차 도중 카메라가 움직이면 하나의 자세로 맞출 수 없다. 이 도구는 한 회차 안의 자세가 일정하다고 가정하며 구간 분할·변화점 감지는 후속이다.
- 카메라 간 잔여 상수 시각 차이는 새로 추정하지 않는다. 빠른 움직임과 낮은 해상도 관절 잡음이 재투영 잔차를 키울 수 있다. 실패 시 불확실성 검사를 넓히지 않고 구간/관측 품질을 확인한다.
- 초기 보정의 공통 축척/World 오차는 새 회차 영상만으로 독립 검증되지 않는다. 발목 접지 가정과 실측 렌즈 중심 높이의 오차(약 1~2 cm)는 -321과 같다.

코드는 `ai/src/simai_ai/calibration/extrinsics/session*.py`, 합성 복구·혼합 대응·교대 조정·기존 경로 회귀 시험은 `ai/tests/calibration/extrinsics/test_session.py`에 있다. 실제 실행의 입력·명령·수치와 검증 로그는 git이 무시하는 `ai/experiments/runs/session-calib-1029/`, 요약은 `.agy/notes.md`에 남긴다.

## 회차 보정의 직전 World 유지와 독립 구간 검증 (-343)

`-331`의 첫 회차 보정(`cal-session-20261029-075820-459z`)은 움직이지 않은 `cam_A, cam_B, cam_C`의 중심이 `13.7 / 4.5 / 3.5 cm` 바뀌고 보정 높이가 `-3.8 / -3.2 / +4.8 cm`로 갈라졌으며, 9분 녹화 중 단일 12초 창(공통 시각 119개 중 다중 카메라 관측 35개)만 써서 최적화 구간과 같은 창에서만 지표를 확인했다. `-343`에서는 (1) 가만히 있었던 기준 카메라들로 직전 보정(`previous`)의 World 좌표계를 유지하고, (2) 녹화 전 구간에서 균등 추출한 창을 `fit`/`holdout`으로 교대 분할해 최소 데이터 기준과 독립 구간 검증을 통과할 때만 보정 문서를 내도록 보강했다.

### 직전 World 좌표계 정렬과 축척 결정

`adjust_human_bundle`은 관측된 사람들의 발목 접지(`Z = 0.08 m`)와 뼈 길이로 바닥 평면과 자유 게이지 World를 다시 잡는다. 관측 표본이나 단상 위 접지가 섞이면 자유 World 전체가 직전 보정 대비 회전·평행이동하면서 실제로는 가만히 있었던 카메라들의 위치와 높이가 함께 흔들린다. 이를 막기 위해 라운드별 bundle 직후 `align_world_to_previous`로 새 보정의 World를 직전 보정(`previous`)의 World로 정렬한다.

1. **기준 카메라(`anchor_cameras`) 선정**:
   - 기본값 `auto`(`select_anchor_cameras`): PnP 회전 초기값 복구가 필요했던 카메라(예: `cam_D`)를 먼저 제외한다. 남은 후보 카메라들의 회전 행렬 변화 `R_prev^T R_new`에서 Kabsch 평균 공통 World 회전 `R_w`를 구한 뒤, 개별 카메라의 잔여 회전 각도 `angle(R_prev, R_new R_w^T)`가 `1.5°`(`DEFAULT_ANCHOR_MAX_ROTATION_DEG`)를 넘는 카메라를 반복 제거한다.
   - 회전과 평행이동·축척을 정하려면 기준 카메라가 최소 2대(`min_anchors = 2`) 남아야 한다. 2대 미만이면 억지로 맞추지 않고 `INSUFFICIENT_ANCHOR_CAMERAS`로 보정 생성을 거절한다(종료 코드 `3`).
   - 현장 작업 기록으로 고정 카메라를 알고 있을 때는 CLI `--anchor-cameras cam_A,cam_B,cam_C`로 직접 지정할 수 있다(`selection_mode = "manual"`).
2. **회전·위치·축척 정렬(`align_world_to_previous`)**:
   - 기준 카메라들의 방향 행렬과 중심 변화 벡터 `C_new,i - \bar{C}_new`를 함께 써서 직전 World로의 회전 `R_w`를 구한다(중심 기저를 카메라 회전 행렬들의 Frobenius 노름에 맞춰 정규화해 방향과 중심 기하를 동시에 반영).
   - 기준 카메라 중심들의 가중 Umeyama 비로 유사변환 배율 `similarity_scale`을 계산하고, `mode="similarity"`(기본)에서는 `s = similarity_scale`, `mode="rigid"`에서는 `s = 1.0`으로 두어 `C'_i = s R_w C_i + t_w`, `R'_i = R_i R_w^T`, `t'_i = -R'_i C'_i`를 모든 카메라(`cam_D` 포함)에 일괄 적용한다.
3. **`similarity` 대 `rigid` 및 `MEASURED_CAMERA_HEIGHT` 축척 유지 근거**:
   - `2026-10-29` 전 구간 실행에서 라운드 1(초기 PnP 중심고정 상태에서 첫 bundle 직후)은 `adjust_human_bundle`의 실측 카메라 높이 축척(`scale = 1.0643`) 뒤 기준 카메라(`cam_A, cam_B, cam_C`) 유사변환 배율이 `similarity_scale = 0.9883`(높이 축척 대비 `-1.17%`)이었다. 라운드마다 World 정렬을 적용해 다음 라운드가 직전 보정의 축척과 좌표계에서 출발하도록 한 결과, 최종 라운드 3에서는 `similarity_scale = 1.000101`(차이 `0.01%`)로 수렴했다.
   - 라운드 3에서 `similarity`(`s = 1.000101`)와 `rigid`(`s = 1.0`)를 비교하면 기준 카메라 중심 잔차(`cam_A / cam_B / cam_C`)는 `similarity` `1.571 / 1.260 / 1.497 cm`(평균 `1.443 cm`), `rigid` `1.568 / 1.259 / 1.503 cm`(평균 `1.443 cm`)이고, 실측 높이 대비 카메라 높이 잔차는 두 방식 모두 `cam_A -2.18 cm, cam_B +0.63 cm, cam_C +1.53 cm, cam_D +1.31 cm`로 `0.01 cm` 안에서 일치한다.
   - 정렬 후 실측 카메라 높이 검산(`_camera_height_scale`, `height_scale_check_after`)에서도 4대 높이 비 중앙값은 `0.995642`(실측 높이 축척과 `0.44%` 차이), 카메라 간 최대 편차(`spread`)는 `1.44% <= 3.0%`로 기준을 넉넉히 통과한다.
   - 따라서 첫 라운드에서 직전 보정의 기준 카메라 간 기하 거리를 즉시 복원해 후속 라운드 삼각측량·연관 수렴을 돕는 `similarity`를 기본으로 쓰되, 정렬 후에도 `height_scale_check_after`의 `max_spread <= 3.0%` 검사를 강제해 `scale_basis = MEASURED_CAMERA_HEIGHT`를 유지한다. `report.json`의 `world_alignment`에는 `similarity_scale`, `height_scale_diff_ratio`, `height_residuals_m`(`before_alignment_m / after_similarity_m / after_rigid_m`), `anchor_center_residuals_similarity_cm / anchor_center_residuals_rigid_cm`를 모두 남긴다.

### 전 구간 다중 창 추출과 최소 데이터 기준·독립 구간 분할

1. **추출 간격(`every_n = 5`)과 다중 창(`start_s = 5.0, window_step_s = 24.0, window_duration_s = 9.0`)**:
   - 녹화 시작 후 `0~5.0 s` 구간은 `RECEIVE_ESTIMATED` 시계 필터가 수렴하기 전이라 `timestamp_uncertainty_ms > 25 ms`로 보간에서 탈락하므로 기본 `--start-s 5.0`으로 건너뛴다.
   - `25 fps`(프레임 주기 `40 ms`) 녹화에서 `-331`이 쓴 `every_n = 8`은 추출 프레임 간격이 `320 ms`라 보간 창(`±200 ms`) 바깥으로 밀리는 시각이 생긴다. 보간 창(`200 ms`) 이하를 보장하는 최대 간격인 `every_n = 5`(`200 ms`)를 `session` 기본값으로 고정했다.
   - 약 9분(`538.5 s`) 녹화 전체에서 `24 s`마다 `9 s` 창을 뽑아 총 23개 창(카메라당 `1,019~1,023`프레임, 합계 `4,080`프레임)을 균등 추출하고, 추출 설정과 소요 시간을 `keypoints.jsonl` 헤더의 `extraction_config`와 `report.json`의 `inputs.extraction_config`에 기록해 `--keypoints`로 재사용한다.
   - 창 사이의 `15 s` 공백을 가로질러 같은 로컬 궤적 ID가 이어지거나 `OUTSIDE_WINDOW` 보간 시도가 대량 발생하지 않도록, `track_session`은 각 추출 창 안에서만 독립적으로 `CameraLocalTracker`를 돌리고(`w{창번호}_{카메라}-t{id}`) 공통 시각(`sample_period_ms = 300`)도 추출 창 내부에서만 생성한다.
2. **`fit` / `holdout` 교대 분할(`split_session_tracks`)**:
   - 공통 시각이 생성된 추출 창들을 시간순으로 번갈아 짝수 창(`0, 2, 4, ...`)은 보정 추정에 쓰는 `fit`, 홀수 창(`1, 3, 5, ...`)은 보정 추정에 전혀 넣지 않는 `holdout`으로 나눈다(단일 연속 구간 입력일 때는 10초 단위 블록을 교대 분할).
   - 두 집합의 시각 교집합(`overlap_ticks`)은 항상 `0`이며, 녹화 초반·중반·후반의 다양한 동선과 구역이 `fit`과 `holdout` 양쪽에 고르게 들어간다.
3. **최소 데이터 기준(`SessionDataCriteria`)**:
   - 표본이 너무 적은 상태에서 과적합된 보정이 나가지 않도록 다음 하한을 모두 만족할 때만 추정을 진행한다:
     - `min_fit_ticks = 30`, `min_fit_active_ticks = 25` (2대 이상 카메라에 사람이 동시에 잡힌 `fit` 시각)
     - `min_holdout_ticks = 15`, `min_holdout_active_ticks = 10` (독립 검증용 `holdout` 활성 시각)
     - `min_fit_observations = 40` (`fit` 다중 카메라 사람 관측 수)
     - `min_fit_3plus_observations = 25` (3대 이상 카메라가 묶인 사람 관측 수)
     - `min_camera_3plus_observations = 15` (모든 카메라 각각이 3대 이상 융합 클러스터에 참여한 관측 수)
   - 하나라도 미달하면 `calibration.json`을 만들지 않고 `report.json`의 `data_sufficiency.reasons`와 `failure_reason`(`INSUFFICIENT_DATA:...`)에 사유를 적고 종료 코드 `3`(`EXIT_NOT_USABLE`)으로 끝낸다.

### 2026-10-29 로비 전 구간 재실행 결과 (`session-calib-1029-v2`)

`rec-20261029-015512z-lobby-railing-pose` 전 구간 캐시(`ai/experiments/runs/session-calib-1029-v2/keypoints.jsonl`, 23개 창·`4,080`프레임)와 직전 보정 `cal-walkshm-20261029-033008z`로 실행해 새 보정 **`cal-session-20261029-100247-699z`**(`PROVISIONAL`, `scale_basis = MEASURED_CAMERA_HEIGHT`, 파일 SHA256 `sha256:d59430fea6b71ae54db6c2b9ca2caa27f783b15eada903f9d401771ab6f60afd`)를 생성했다.

1. **표본 규모(`-331` 대비)**:
   - 공통 시각 `601`개(`fit` 10개 창 `300`시각 / 활성 `291`시각, `holdout` 10개 창 `301`시각 / 활성 `290`시각, `overlap_ticks = 0`).
   - `fit` 다중 카메라 사람 관측은 라운드 1 `435`개(3+대 `286`개, 4대 `143`개) → 라운드 2 `482`개 → 라운드 3 `488`개(3+대 `406`개, 4대 `271`개)로, `-331`(활성 `35`시각, 관측 `52 → 66`개) 대비 활성 시각 `8.3`배·사람 관측 `7.4`배다. 3개 라운드 모두 `CONVERGED`였다.
2. **World 정렬 전후 비교(단상 접지 기울기 제거 확인)**:
   - 이 녹화의 Stage 4(`375~460 s`)에는 참가자가 로비 단상(`Z > 0`) 위에 올라가 서 있는 구간이 포함된다. 정렬 전 자유 bundle은 단상 위 정지 발목을 바닥(`Z = 0.08 m`)으로 끌어당기면서 World를 직전 보정 대비 `1.305°`(회전축 `[-1.301°, -0.102°, +0.006°]`), 기준 카메라 중심 평균 `4.65 cm`(원점 평행이동 `11.93 cm`) 기울였고, 그 결과 정렬 전 카메라 높이 잔차가 `cam_A +3.03 cm, cam_B -9.05 cm, cam_C -7.96 cm, cam_D +7.77 cm`(위치 변화 `cam_A 4.67 cm, cam_B 9.11 cm, cam_C 9.48 cm`)로 벌어졌다.
   - `align_world_to_previous`가 자동 선정된 기준 카메라 `cam_A, cam_B, cam_C`(`cam_D`는 PnP 초기화 대상으로 자동 제외)로 `1.305°` World 기울기와 평행이동을 제거하자, 정지 카메라 3대의 위치 변화는 **`1.26~1.57 cm`**, 회전 변화는 **`0.37~0.62°`**, 4대 카메라 높이 잔차는 **`-2.18 ~ +1.53 cm`** 안으로 복원됐다.

| 카메라 | World 정렬 전 회전 / 위치 / 높이 잔차 | World 정렬 후 회전 / 위치 / 높이 잔차 | 보정 높이 (실측 높이) | `fit` bundle 재투영 (median / p95) |
| --- | --- | --- | --- | --- |
| `cam_A` (기준) | `1.50°` / `4.67 cm` / `+3.03 cm` | **`0.37°` / `1.57 cm` / `-2.18 cm`** | `2.188 m` (`2.210 m`) | `3.92 px` / `13.45 px` |
| `cam_B` (기준) | `1.09°` / `9.11 cm` / `-9.05 cm` | **`0.39°` / `1.26 cm` / `+0.63 cm`** | `2.201 m` (`2.195 m`) | `3.44 px` / `13.44 px` |
| `cam_C` (기준) | `1.55°` / `9.48 cm` / `-7.96 cm` | **`0.62°` / `1.50 cm` / `+1.53 cm`** | `2.245 m` (`2.230 m`) | `3.13 px` / `13.73 px` |
| `cam_D` (이동) | `4.45°` / `8.99 cm` / `+7.77 cm` | **`4.45°` / `4.82 cm` / `+1.31 cm`** | `2.253 m` (`2.240 m`) | `3.63 px` / `14.39 px` |

3. **`fit` 및 독립 `holdout` 전후 비교**:

| 지표 | `fit` before (`previous`) | `fit` after (`session-v2`) | `holdout` before (`previous`) | `holdout` after (`session-v2`) |
| --- | --- | --- | --- | --- |
| `cam_A/cam_B` 통과율 (전체 / strict) | `47.2%` / `44.8%` | `47.6%` / `45.1%` | `47.1%` / `43.0%` | `46.6%` / `42.4%` |
| `cam_A/cam_C` 통과율 (전체 / strict) | `45.9%` / `36.7%` | `47.2%` / `44.1%` | `46.4%` / `38.9%` | `48.4%` / `44.1%` |
| `cam_A/cam_D` 통과율 (전체 / strict) | `3.9%` / `2.2%` | **`51.9%` / `48.0%`** | `3.9%` / `3.2%` | **`60.7%` / `59.3%`** |
| `cam_B/cam_C` 통과율 (전체 / strict) | `36.8%` / `28.0%` | `42.7%` / `41.2%` | `29.1%` / `23.0%` | `40.6%` / `38.2%` |
| `cam_B/cam_D` 통과율 (전체 / strict) | `0.1%` / `0.0%` | **`44.6%` / `40.7%`** | `0.4%` / `0.2%` | **`44.5%` / `41.0%`** |
| `cam_C/cam_D` 통과율 (전체 / strict) | `26.4%` / `20.3%` | **`45.8%` / `42.6%`** | `33.0%` / `22.0%` | **`50.2%` / `48.5%`** |
| 다중 뷰 클러스터 `3+` / `4`대 비율 | `25.2%` / `0.0%` | **`76.3%` / `45.4%`** | `19.1%` / `0.0%` | **`75.3%` / `35.3%`** |
| 전체 view 융합 `3+` / `4`대 비율 | `21.9%` / `0.0%` | **`75.1%` / `49.7%`** | `16.3%` / `0.0%` | **`74.3%` / `40.2%`** |
| 4대 동시 관측 시각 최대 클러스터 `3+` / `4`대 | `45.0%` / `0.0%` (`282`시각) | **`97.5%` / `74.5%`** | `34.1%` / `0.0%` (`270`시각) | **`96.3%` / `61.9%`** |
| 3+대 삼각측량 재투영 전체 (median / p95, 관절 수) | `3.77 / 12.80 px` (`15,991`) | `3.80 / 13.13 px` (`23,004`) | `3.49 / 12.98 px` (`13,344`) | `3.92 / 15.03 px` (`20,006`) |
| `cam_D` 3+대 재투영 (median / p95, 관절 수) | `2.81 / 10.92 px` (`1,358`) | `3.76 / 13.43 px` (`5,296`) | `3.14 / 12.00 px` (`1,010`) | `3.79 / 14.96 px` (`5,283`) |

4. **`cam_D` 회전량 판정(`4.45°` vs `calib-1029-rot`의 `7.44°`)**:
   - `calib-1029-rot`(`7.44°` 순수 회전)와 이번 `cal-session-20261029-100247-699z`(`4.45°` 회전 + `4.82 cm` 중심 이동, 두 회전 행렬 사이 각도 `3.16°`)를 독립 `holdout`(10개 창, `301`시각)에서 동일하게 평가했다:
     - `calib-1029-rot`(`7.44°`, 기존 중심): `cam_A/cam_D 53.1% (38.9%)`만 부분 회복되고 **`cam_B/cam_D = 0.12%`**, **`cam_C/cam_D = 17.1% (strict 6.1%)`**, **4대 융합 `0.0%`**이며, `cam_D`가 3+대 클러스터에 들어간 관절은 `799`개(`median 3.97 px`, **`p95 17.03 px`**)뿐이다.
     - 새 `cam_D` 중심(`+4.82 cm` 이동)에 `7.44°` 회전을 넣은 경우: `cam_A/cam_D 40.4% (22.0%)`, **`cam_B/cam_D = 0.12%`**, **`cam_C/cam_D = 15.2% (7.1%)`**, **4대 융합 `0.0%`**, `cam_D` 3+대 관절 `343`개(`median 4.62 px`, **`p95 29.02 px`**)로 더 나빠진다.
     - 반면 **`cal-session-20261029-100247-699z`(`4.45°` + `4.82 cm`)**는 `holdout`에서 `cam_A/cam_D 60.7% (59.3%)`, `cam_B/cam_D 44.5% (41.0%)`, `cam_C/cam_D 50.2% (48.5%)`로 `cam_D` 관련 3개 쌍이 모두 `44~61%`로 고르게 열리고, 4대 동시 관측 시각의 `96.3%`에서 3+대·`61.9%`에서 4대 완전 융합이 성립하며, `cam_D` 3+대 관측 관절 수가 `5,283`개(`7.44°` 대비 `6.6`배)로 늘어나면서 재투영 오차가 `median 3.79 px, p95 14.96 px`(기준 카메라 `cam_A 4.02/15.32 px`, `cam_B 3.86/14.40 px`, `cam_C 4.04/15.23 px`와 동등 수준)로 유지된다.
   - 결론: `7.44°`는 단일 구간(`270~282 s`)에서 거치대 이동(`4.82 cm`)을 순수 회전(`+3.0°` 과회전)으로 억지 흡수해 `cam_B/cam_D` 에피폴라 기하를 깨뜨린 값이며, 전 구간 `holdout` 검증 결과 **`cam_D`의 실제 변화는 회전 `4.45°` + 중심 이동 `4.82 cm`가 맞다.**

### 버린 대안과 남은 위험 (-343)

- **버린 대안**:
  - **bundle 자유 World 그대로 출력**: 단상(`Stage 4`) 위 발목 접지가 바닥 평면을 `1.31°` 기울여 가만히 있던 `cam_B, cam_C` 높이를 `-8~-9 cm` 왜곡하므로 기각했다.
  - **창 경계를 무시한 단일 로컬 추적**: `24 s` 주기 추출 창 사이의 `15 s` 공백 동안 다른 사람이 비슷한 위치에 서면 같은 로컬 궤적으로 묶이거나 `OUTSIDE_WINDOW` 탈락이 폭증하므로 창별 독립 추적으로 분리했다.
  - **`every_n = 8`(`320 ms`) 유지**: 보간 창(`±200 ms`)보다 프레임 간격이 넓어 양쪽 프레임 보간이 끊기므로 `every_n = 5`(`200 ms`)로 낮췄다.
  - **중심 고정 순수 회전(`7.44°`) 채택**: 독립 `holdout`에서 `cam_B/cam_D 0.12%`, 4대 융합 `0.0%`로 기각됐다.
- **남은 위험**:
  - 녹화에 단상·계단처럼 `Z > 0`인 높은 바닥 위 정지 인원이 많으면 정렬 전 bundle의 바닥 평면 추정치가 기울어질 수 있다. 현재는 움직이지 않은 기준 카메라(`>= 2`대)로의 World 정렬이 이 기울기를 제거하고 정렬 후 카메라 높이 잔차(`max_spread <= 3%`)를 다시 검산하지만, 향후 접지 후보에서 높이 이상치를 먼저 걸러내면 정렬 전 bundle 자체의 평면 잔차도 더 줄일 수 있다.
  - 4대 중 3대 이상이 한꺼번에 움직인 회차에서는 자동 기준 카메라가 2대 미만이 되어 `session` 보정이 `INSUFFICIENT_ANCHOR_CAMERAS`로 거절된다(이 경우 보정 걷기 녹화로 전체 초기 보정을 다시 수행해야 한다).
  - 독립 실측 검산점(`checkpoints`)이 없으므로 출력 보정 상태는 계약대로 `PROVISIONAL`이다.


## 운영 중 관절 모음과 백그라운드 후보 보정 실행기 (-345)

`-331`과 `-343`이 회차 녹화 영상 파일에서 오프라인으로 외부 파라미터를 갱신했다면, `-345`는 운영 중 Tier 0가 이미 매 프레임 생성하는 2D 사람 관절·로컬 추적 결과를 메모리 고리 버퍼에 모아 두었다가, 실시간 파이프라인(`P0_LIVE`)을 방해하지 않는 별도 저우선순위 프로세스에서 후보 보정(`CalibrationDocument`)을 만들고 독립 `holdout` 개선이 확인될 때만 Tier 0에 제안(`propose_calibration(document, report)`)하는 온라인 후보 보정 경로를 구현한다. 코드는 `ai/src/simai_ai/calibration/live_candidate.py`(공개 재수출 `simai_ai.calibration`), 시험은 `ai/tests/calibration/test_live_candidate.py`에 있다.

### 데이터 양에 따른 보정 품질 곡선 측정 (2026-10-29 녹화 `session-calib-1029-v2`)

`rec-20261029-015512z-lobby-railing-pose` 전 구간 추출 캐시(`keypoints.jsonl`, 23개 창·`4,080`프레임)에서 고정된 전역 독립 `holdout`(홀수 창 10개, 공통 시각 `301`개 / 활성 `290`개)을 기준으로, `fit`에 넣는 녹화 분량(`30 s`, `1 m`, `2 m`, `5 m`, 전체 `~9 m`)과 시작 위치(초반 `early` / 중반 `mid` / 후반 `late`, 각 3회)를 바꿔 가며 직전 보정 `cal-walkshm-20261029-033008z`에서 출발한 보정 품질과 소요 시간을 측정했다(`ai/experiments/runs/live-candidate-curve/curve_summary.json`).

- **보정 전(`cal-walkshm-20261029-033008z`) 고정 `holdout` 기준선**:
  - `cam_D` 포함 쌍 통과율: `cam_A/cam_D` `3.92%`, `cam_B/cam_D` `0.35%`, `cam_C/cam_D` `32.99%`(평균 **`12.42%`**)
  - 다중 뷰 클러스터 `3+` / `4`대 융합률: **`19.10%` / `0.00%`**
  - 3+대 삼각측량 재투영 오차(전체): 중앙값 **`3.492 px`** / p95 **`12.983 px`**(관절 `13,344`개, `cam_D` `1,010`개)

| 녹화 분량 | 시작 구간 (`fit` 창) | 녹화 구간(s) | 공통 시각 (활성) | `fit` 다중인 관측 (R1→R3) | 수렴 여부 (R1/R2/R3) | `holdout` D쌍 평균 (A/D, B/D, C/D) | `holdout` 3+ / 4대 융합률 | `holdout` 재투영 중앙 / p95 (px) | 기준 카메라 중심 잔차 평균 / 최대 (cm) | `-343` 최종판 대비 `cam_D` 차이 (회전 / 위치) | CPU / Wall 시간 (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `30 s` | `early_w0` (`[0]`) | `0~9` | `30 (29)` | `57 → 0` | 미수렴 (`cam_D` 3+ `0`개) | `12.4%` (`3.9/0.4/33.0%`) | `19.1%` / `0.0%` | `3.492 / 12.983` | - | `4.455° / 4.82 cm` | `15.9 / 16.4` |
| `30 s` | `mid_w4` (`[4]`) | `192~201` | `30 (29)` | `45 → 44` | 미수렴 (`U/U/U`, 높이 검산) | `45.5%` (`55.6/30.7/50.2%`) | `56.1%` / `18.2%` | `4.082 / 14.141` | `4.26 / 5.37` | `2.159° / 17.62 cm` | `43.0 / 43.6` |
| `30 s` | `late_w7` (`[7]`) | `336~345` | `30 (29)` | `37 → 52` | 수렴 (`C/C/C`, 초기 관측 `37 < 40`) | `51.6%` (`60.7/44.0/50.1%`) | `73.5%` / `36.0%` | `4.073 / 15.259` | `2.94 / 3.75` | `0.460° / 5.50 cm` | `40.1 / 40.7` |
| `1 m` | `early_w0_1` (`[0,1]`) | `0~57` | `60 (58)` | `115 → 0` | 미수렴 (`cam_D` 3+ `0`개) | `12.4%` (`3.9/0.4/33.0%`) | `19.1%` / `0.0%` | `3.492 / 12.983` | - | `4.455° / 4.82 cm` | `30.2 / 30.7` |
| `1 m` | `mid_w4_5` (`[4,5]`) | `192~249` | `60 (58)` | `114 → 94` | **수렴** (`C/C/C`) | `51.6%` (`60.0/44.9/50.1%`) | `70.7%` / `33.9%` | `4.216 / 15.454` | `1.15 / 1.15` | `0.992° / 7.33 cm` | `136.0 / 95.1` |
| `1 m` | `late_w7_8` (`[7,8]`) | `336~393` | `60 (58)` | `68 → 92` | **수렴** (`C/C/C`) | **`51.2%`** (`60.6/43.0/50.2%`) | **`74.6%` / `29.6%`** | `3.993 / 14.868` | `2.88 / 3.49` | **`0.403° / 2.96 cm`** | `61.7 / 62.8` |
| `2 m` | `early_w0_2` (`[0..2]`) | `0~105` | `90 (87)` | `120 → 142` | **수렴** (`C/C/C`) | **`51.6%`** (`60.4/44.2/50.2%`) | **`74.8%` / `36.5%`** | `3.986 / 15.164` | `2.02 / 2.99` | **`0.170° / 2.71 cm`** | `109.1 / 96.0` |
| `2 m` | `mid_w3_5` (`[3..5]`) | `144~249` | `90 (87)` | `143 → 147` | R2 수렴 / R3 불확실 | `51.4%` (`60.6/43.7/50.1%`) | `75.4%` / `34.5%` | `4.084 / 15.302` | `4.01 / 5.18` | `0.344° / 3.36 cm` | `165.0 / 129.5` |
| `2 m` | `late_w6_8` (`[6..8]`) | `288~393` | `90 (88)` | `115 → 137` | 미수렴 (`U/U/U`, 높이 검산) | `51.9%` (`60.6/44.9/50.2%`) | `76.2%` / `34.3%` | `4.020 / 14.993` | `1.64 / 2.37` | `0.213° / 2.37 cm` | `167.6 / 119.4` |
| `5 m` | `early_w0_6` (`[0..6]`) | `0~297` | `210 (204)` | `322 → 334` | R1 수렴 / R3 불확실 | `51.7%` (`60.6/44.4/50.2%`) | `74.1%` / `35.2%` | `3.912 / 15.100` | `1.55 / 1.82` | `0.097° / 1.09 cm` | `521.1 / 311.0` |
| `5 m` | `mid_w1_7` (`[1..7]`) | `48~345` | `210 (204)` | `322 → 336` | **수렴** (`U/U/C`) | **`51.6%`** (`60.6/44.2/50.2%`) | **`74.6%` / `35.3%`** | `3.969 / 15.126` | `2.00 / 2.50` | **`0.120° / 2.91 cm`** | `514.7 / 320.4` |
| `5 m` | `late_w3_9` (`[3..9]`) | `144~441` | `210 (204)` | `321 → 348` | **수렴** (`U/U/C`) | **`51.6%`** (`60.4/44.3/50.2%`) | **`75.5%` / `36.8%`** | `3.967 / 15.039` | `1.85 / 2.34` | **`0.184° / 1.62 cm`** | `514.7 / 305.3` |
| 전체(`~9 m`) | `all_w0_9` (`[0..9]`) | `0~441` | `300 (291)` | `435 → 488` | **수렴** (`C/C/C`) | **`51.8%`** (`60.7/44.5/50.2%`) | **`75.3%` / `35.3%`** | `3.924 / 15.033` | `1.44 / 1.57` | **`0.000° / 0.00 cm`** | `717.7 / 416.0` |

- **곡선 해석 및 운영 기본값 도출**:
  1. **품질 포화 지점 (`2분`, `fit` `90`시각)**: `30 s`와 `1 m` 초반 구간(`early`)은 `cam_D` 시야에 다중인 교차 궤적이 부족해 PnP 초기화가 일어나지 않거나(`cam_D` 3+ 관측 `0`개) 위치가 `5.5~17.6 cm` 흔들린다. 반면 **`2 m`(`fit` `90`시각, 다중인 관측 `~120`개)**부터는 시작 위치와 무관하게 `holdout` D쌍 평균 통과율 **`51.4~51.9%`**, 3+대 융합률 **`74.8~76.2%`**, 4대 융합률 **`34.3~36.5%`**로 전체 9분(`51.8%` / `75.3%` / `35.3%`)과 **`1.5%p` 이내에서 완전히 포화**되며, `-343` 최종판 대비 `cam_D` 차이도 회전 **`0.17~0.34°`**, 위치 **`2.37~3.36 cm`** 안으로 수렴한다.
  2. **수렴 안정성 및 고리 버퍼 길이 (`5분`)**: `5 m`(`fit` `210`시각) 구간에서는 `-343` 대비 `cam_D` 회전 **`0.10~0.18°`**, 위치 **`1.09~2.91 cm`**, 기준 카메라 중심 잔차 평균 **`1.55~2.00 cm`**로 한층 더 조밀해진다. 따라서 운영 고리 버퍼 기본 시간 창은 **`5분`(`max_duration_ms = 300_000`)**(최대 10분 이내 조정 가능), 후보 보정 실행 주기는 **`60 s`**로 설정했다.
  3. **`reprojection_metrics` 군집 삼각측량 벡터화**: 곡선 측정 과정에서 `session_tracks.reprojection_metrics`가 관절마다 개별 삼각측량 루프를 돌아 평가 1회에 `29.1 s`가 걸리던 병목을 확인하고, 관측 카메라 조합(`ids = tuple(sorted(views))`)별로 묶어 `triangulate_normalized_many`와 `_project_points`로 일괄 계산하도록 벡터화해 수치 변화 없이(`0.0 px` 차이) **`29.1 s → 0.31 s` (`94`배)**로 단축했다.

### 운영 중 관절 고리 버퍼 (`LiveJointBuffer`)

GPU를 다시 돌리지 않고 Tier 0 perception/local-tracker 출력 또는 추출 검출 행을 그대로 받아 최근 시간 창만 유지하는 스레드 안전(`threading.Lock`) 고리 버퍼다.

- **입력 형식**:
  - `append_frame(frame: Observation2DFrame)`: Tier 0의 카메라별 프레임 출력(`camera_id`, `capture_timestamp_ms`, `timestamp_source`, `timestamp_uncertainty_ms`, `frame_sequence`, `tracked_detections: tuple[TrackedDetection, ...]`)을 직접 받는다. 각 `TrackedDetection`의 `local_track_id`를 보존해 카메라 로컬 추적을 다시 돌리지 않고도 바로 시간 보간에 쓸 수 있다.
  - `append_detection_row(row: Mapping[str, Any])`: `keypoints.jsonl`의 `type="frame"` 행(또는 원시 `detections` 목록)을 받아 버퍼에 넣는다. `window_index`가 있으면 창 경계를 보존한다.
- **메모리·용량 상한 (`LiveBufferConfig`)**:
  - `max_duration_ms = 300_000` (기본 5분): 카메라별 최신 프레임 시각 기준 `latest_ts - max_duration_ms` 이전 프레임을 즉시 버린다.
  - `max_frames_per_camera = 1_500`: 카메라당 최대 프레임 수 상한(5분 × `5 fps` 상당).
  - `max_total_samples = 24_000`: 전체 카메라 합산 사람 표본(`TrackedPersonSample`) 상한.
  - `max_bytes = 32 * 1024 * 1024` (32 MiB): 표본당 추정 바이트(`_estimate_frame_bytes`) 합계가 상한을 넘으면 가장 오래된 카메라 프레임부터 제거해 메모리 사용량이 무한히 자라지 않도록 보장한다.
- **카메라별 선택 비우기 (`reset_camera(camera_id)`)**:
  - `-346` 실시간 흔들림 감지가 특정 카메라를 `DRIFTED`로 판정해 알림을 보냈을 때, 이동 전 옛 관절이 이동 후 새 관절과 섞여 잘못된 평균 자세를 만들지 않도록 해당 카메라의 버퍼 프레임만 즉시 비우고(`reset_camera("cam_D")`), 움직이지 않은 다른 카메라들의 기준 궤적 버퍼는 그대로 유지한다.
- **`SessionTracks` 변환 (`to_session_tracks()`)**:
  - 이미 `local_track_id`가 부여된 Tier 0 프레임은 창별로 묶어 `interpolate_camera`로 공통 시각(`sample_period_ms = 300 ms`)을 만들고, 원시 검출 행은 `track_session`을 통해 창별 독립 추적과 시간 보간을 수행한다.

### 백그라운드 후보 보정 실행기 (`LiveCandidateCalibrationRunner`)

실시간 추론(`P0_LIVE`)의 지연 예산을 침범하지 않으면서 주기적으로 후보 보정을 계산하고 검증하는 실행기다.

1. **자원 격리와 `P0_LIVE` 우선순위 양보 (`LiveCandidateConfig`)**:
   - **우선순위 게이트 (`AdmissionGate` / `can_run`)**: 실행 시작 전 `admission_gate.check()`(명세 §3.4: `P0_LIVE > P1_CALIBRATION > P2_ANALYSIS`)와 선택 콜백 `can_run()`을 검사한다. `P0_LIVE` 과부하로 `P1_CALIBRATION`이 차단된 상태면 작업을 띄우지 않고 즉시 `SKIPPED_RESOURCE_BUSY`를 반환한다.
   - **단일 실행 보장**: 이전 백그라운드 보정 프로세스가 아직 돌고 있으면 겹쳐 띄우지 않고 `SKIPPED_ALREADY_RUNNING`을 반환한다.
   - **별도 `spawn` 프로세스 및 낮은 OS 우선순위**: `use_subprocess = True`(기본)에서 `multiprocessing.get_context("spawn")`으로 별도 워커 프로세스를 띄운다. 워커 시작 즉시 Windows에서는 `SetPriorityClass(..., BELOW_NORMAL_PRIORITY_CLASS = 0x00004000)`, POSIX에서는 `os.nice(10)`으로 OS 스케줄링 우선순위를 낮추고, `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `NUMEXPR_NUM_THREADS=1`로 BLAS 스레드 폭주를 막는다.
   - **시간 상한 (`timeout_s = 180.0`)**: 워커가 `timeout_s` 안에 결과를 내지 못하면 부모가 워커를 강제 종료(`terminate()` → `kill()`)하고 `FAILED_TIMEOUT`을 기록한다. 워커 결과 전달 시에는 대용량 파이프 교착을 막기 위해 `queue.get` 폴링 후 `proc.join`을 수행한다.
2. **2단계 추정 경로 (회전 빠른 길 → 전체 회차 보정)**:
   - 버퍼 스냅샷을 `split_session_tracks`로 교대 분할(`fit` / `holdout`, `overlap_ticks = 0`)하고 `SessionDataCriteria`를 먼저 검사한다. 미달 시 `INSUFFICIENT_DATA`로 즉시 중단한다.
   - **1단계 — 중심 고정 회전 빠른 길 (`try_fast_rotation_path`)**: 카메라 위치를 고정한 채 `realign_rotation`(다중인 시 `rotation_candidates`의 중심 고정 회전 후보 포함)으로 1대 이상의 카메라 회전만 빠르게 갱신해 본다. 회전 갱신 후 독립 `holdout`에서 개선 기준(`CandidateImprovementCriteria`)을 만족하고 모든 카메라 쌍의 통과율이 최소 하한(`fast_rotation_min_pair_pass_ratio = 0.25`) 이상이면 비싼 번들 조정 없이 즉시 채택(`mode = "fast_rotation"`)한다. (예: 순수 거치대 회전만 발생한 경우 수 초 내 해결. 반면 10/29 녹화처럼 `cam_D`가 회전 `4.45°` + 중심 `4.82 cm` 이동을 함께 겪어 순수 회전만으로는 `cam_B/cam_D` 통과율이 `0.1%`에 머무는 경우에는 자동으로 기각하고 전체 회차 보정으로 넘어간다.)
   - **2단계 — 전체 회차 보정 (`adjust_session`, `mode = "full_session"`)**: 회전 빠른 길이 기각되면 `-343`의 다중인 PnP 부트스트랩 + 3라운드 `adjust_human_bundle` + `align_world_to_previous`(직전 World 정렬) + 실측 카메라 높이 축척 검산을 수행한다.
3. **개선 판정 기준 (`CandidateImprovementCriteria`)**:
   - 후보 보정이 수렴(`CONVERGED`, 높이 축척 검산 및 World 기준 카메라 정렬 통과)했더라도, 독립 `holdout`에서 기존 활성 보정(`before`) 대비 **실질적인 개선**이 있을 때만 제안한다.
   - **기각 조건 (하나라도 해당하면 `NO_MEANINGFUL_IMPROVEMENT`)**:
     - `holdout` 3+대 융합률 변화 `< -1.0%p` (`max_fusion_3plus_drop = 0.01`)
     - `holdout` 4대 융합률 변화 `< -1.0%p` (`max_fusion_4_drop = 0.01`)
     - `holdout` 최소 카메라 쌍 통과율 변화 `< -2.0%p` (`max_min_pair_pass_drop = 0.02`)
     - `holdout` 전체 재투영 중앙값 증가 `> +0.60 px` (`max_reproj_median_increase_px = 0.60`) 또는 p95 증가 `> +2.50 px` (`max_reproj_p95_increase_px = 2.50`)
     - World 기준 카메라 중심 잔차 평균 `> 3.5 cm` 또는 최대 `> 5.0 cm`
   - **채택 조건 (기각 조건이 없고 아래 중 최소 하나 만족 시 `PROPOSED`)**:
     - `holdout` 3+대 융합률 증가 `>= +3.0%p` (`min_fusion_3plus_gain = 0.03`)
     - `holdout` 4대 융합률 증가 `>= +3.0%p` (`min_fusion_4_gain = 0.03`)
     - `holdout` 최소 카메라 쌍 통과율 증가 `>= +5.0%p` (`min_pair_pass_gain = 0.05`)
     - `holdout` 전체 재투영 중앙값 감소 `>= 0.25 px` (`min_reproj_median_reduction_px = 0.25`) (단, 융합 관절 수가 줄어든 착시가 아닌 경우)
   - **수치 근거**: 1단계 곡선 측정에서 이미 수렴한 보정들(`2 m`·`5 m`·`9 m`) 사이의 `holdout` 흔들림 폭이 3+대 융합률 `±1.4%p`, 4대 융합률 `±2.5%p`, 재투영 중앙값 `±0.17 px`였다. 채택 문턱(`+3.0%p` / `+5.0%p` / `-0.25 px`)을 이 흔들림 폭 바로 위로 두어, 실제 드리프트 복구(`+35~+65%p`)는 확실히 통과시키면서 이미 수렴한 뒤의 불필요한 보정 버전 교체(churn)는 모두 차단한다.

### Tier 0 넘김 인터페이스 (`propose_calibration(document, report)`)와 보고 스키마

`ai/services/tier0/`나 `contracts/`를 수정하지 않고, Tier 0가 실행기 생성 시 콜백 `propose_calibration: Callable[[CalibrationDocument, dict[str, Any]], None]`을 주입하거나 `runner.propose_calibration`을 호출해 새 보정 문서와 검증 요약을 넘겨받는다.

- `document`: `providers.contract.check_calibration`의 스키마·기하·상태 검사를 통과한 `CalibrationDocument`(`calibration_state = PROVISIONAL`, `scale_basis = MEASURED_CAMERA_HEIGHT` 등).
- `report`: `schema_name = "simai.live_candidate_calibration"`, `schema_version = "0.1.0"`을 따르는 JSON 직렬화 가능 `dict`:
  - `status`: `"PROPOSED"`
  - `mode`: `"fast_rotation"` | `"full_session"`
  - `triggered_at_ms`: 실행 시각(ms)
  - `elapsed_wall_s`, `elapsed_cpu_s`: 소요 시간(s)
  - `previous_calibration_version`, `previous_content_hash`: 기존 활성 보정 버전 및 해시
  - `candidate_calibration_version`, `candidate_content_hash`, `calibration_state`, `scale_basis`: 제안 보정 버전·해시·상태·축척 근거
  - `split`: `{"fit_ticks", "fit_active_ticks", "holdout_ticks", "holdout_active_ticks", "overlap_ticks": 0}`
  - `data_sufficiency`: `SessionDataCriteria` 통과 여부 및 세부 집계(`fit_observations`, `fit_3plus_observations`, `camera_3plus_observations`, `reasons`)
  - `holdout`:
    - `before`: 기존 활성 보정의 독립 `holdout` 지표(`fusion_3plus_ratio`, `fusion_4_ratio`, `view_fusion_3plus_ratio`, `view_fusion_4_ratio`, `min_pair_pass_ratio`, `mean_pair_pass_ratio`, `pair_pass_ratios`, `reproj_overall_median_px`, `reproj_overall_p95_px`, `reproj_overall_count`, `reproj_by_camera`)
    - `after`: 후보 보정의 독립 `holdout` 지표(동일 구조)
    - `delta`: `after - before` 변화량(`fusion_3plus_gain`, `fusion_4_gain`, `min_pair_pass_gain`, `mean_pair_pass_gain`, `reproj_median_change_px`, `reproj_p95_change_px`, `reproj_count_change`)
  - `improvement_decision`: `{"accepted": True, "reasons": [...], "deltas": {...}}`
  - `world_alignment`: 기준 카메라 목록(`anchor_cameras`), `similarity_scale`, 기준 카메라 중심 잔차 평균/최대(`anchor_center_residual_mean_cm`, `anchor_center_residual_max_cm`), 카메라별 높이 잔차(`height_residuals_m`)
  - `camera_deltas`: 직전 활성 보정 대비 카메라별 회전(`rotation_deg`)·중심 이동(`translation_cm`)

### 2026-10-29 녹화 시간순 재생 모의 실행 (`run_replay.py`)

`session-calib-1029-v2/keypoints.jsonl`의 `4,080`개 프레임을 촬영 시각 순서대로 `LiveJointBuffer(max_duration_ms = 300_000)`에 흘려 넣으면서 녹화 시각 `60 s`마다 `LiveCandidateCalibrationRunner`(`use_subprocess = True`)를 호출해 총 9회 주기를 모의 실행했다(`ai/experiments/runs/live-candidate-curve/replay_summary.json`). 초기 활성 보정은 `cal-walkshm-20261029-033008z`이다.

| 주기 | 녹화 경과 시각 | 버퍼 창 수 / 프레임 / 표본 | `fit` / `holdout` 시각 | 실행 결과 (`status`) | `holdout` 3+ 융합 (`before → after`) | `holdout` 4대 융합 (`before → after`) | `holdout` 최소 쌍 통과율 (`before → after`) | Wall / CPU (s) | 비고 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `60.0 s` (`1.0 m`) | `3` / `540` / `2,068` | `60` / `30` | `INSUFFICIENT_DATA` | - | - | - | `29.3 / 26.8` | `cam_D` 3+ 관측 `0 < 15`로 거절 |
| 2 | **`120.0 s` (`2.0 m`)** | `5` / `900` / `2,812` | `90` / `60` | **`PROPOSED` (`full_session`)** | **`12.7% → 77.5%` (`+64.8%p`)** | **`0.0% → 29.2%` (`+29.2%p`)** | **`0.0% → 42.1%` (`+42.1%p`)** | `97.1 / 94.1` | **`cal-session-20261029-015719-468z` 제안 및 활성 보정 교체** |
| 3 | `180.0 s` (`3.0 m`) | `8` / `1,440` / `4,244` | `120` / `120` | `NO_MEANINGFUL_IMPROVEMENT` | `78.5% → 77.5%` (`-1.0%p`) | `34.2% → 30.8%` (`-3.4%p`) | `43.5% → 43.2%` (`-0.2%p`) | `149.1 / 128.0` | 주기 2 보정이 이미 수렴해 교체 안 함 |
| 4 | `240.0 s` (`4.0 m`) | `10` / `1,500` / `4,335` | `120` / `113` | `NO_MEANINGFUL_IMPROVEMENT` | `73.7% → 73.1%` (`-0.6%p`) | `38.9% → 39.3%` (`+0.3%p`) | `45.5% → 45.4%` (`-0.0%p`) | `204.2 / 159.3` | 교체 안 함 |
| 5 | `300.0 s` (`5.0 m`) | `13` / `1,500` / `4,468` | `113` / `120` | `NOT_CONVERGED` | - | - | - | `191.4 / 152.9` | 높이 배율 편차 초과로 거절 |
| 6 | `360.0 s` (`6.0 m`) | `13` / `1,500` / `4,167` | `120` / `97` | `NO_MEANINGFUL_IMPROVEMENT` | `69.9% → 71.5%` (`+1.6%p`) | `31.1% → 32.7%` (`+1.6%p`) | `40.4% → 42.6%` (`+2.2%p`) | `181.2 / 145.0` | 개선 폭(`< 3%p`) 미달로 교체 안 함 |
| 7 | `420.0 s` (`7.0 m`) | `13` / `1,500` / `4,427` | `120` / `97` | `NOT_CONVERGED` | - | - | - | `266.2 / 199.1` | 높이 배율 편차 초과로 거절 |
| 8 | `480.0 s` (`8.0 m`) | `13` / `1,500` / `4,877` | `120` / `97` | `NO_MEANINGFUL_IMPROVEMENT` | `75.8% → 73.4%` (`-2.4%p`) | `35.0% → 30.9%` (`-4.1%p`) | `40.7% → 40.3%` (`-0.5%p`) | `258.5 / 201.9` | 교체 안 함 |
| 9 | `533.4 s` (`8.9 m`) | `12` / `1,500` / `4,901` | `118` / `92` | `NO_MEANINGFUL_IMPROVEMENT` | `73.7% → 73.0%` (`-0.7%p`) | `29.3% → 27.6%` (`-1.7%p`) | `38.6% → 39.2%` (`+0.5%p`) | `219.9 / 172.0` | 교체 안 함 |

- **모의 실행 핵심 결과**:
  - **첫 제안 시점**: 녹화 시작 후 **`2.0분`(`120.0 s`, 주기 2)** 만에 첫 후보 보정 `cal-session-20261029-015719-468z`가 제안됐다.
  - **총 제안 횟수**: 9분 녹화 전체(9회 주기) 동안 **단 `1`회**만 제안되었고, 이후 주기 3~9에서는 이미 채택된 2분 보정의 `holdout` 품질이 포화 상태이므로 불필요한 보정 교체가 한 번도 일어나지 않았다.
  - **`-343` 전 구간 최종판(`cal-session-20261029-100247-699z`)과의 일치도**:
    - 2분 시점에 제안된 `cal-session-20261029-015719-468z`와 `-343` 9분 전체 최종판 사이의 카메라별 차이는:
      - `cam_A`: 회전 **`0.138°`**, 위치 **`1.784 cm`**, 실측 대비 높이 잔차 `-1.58 cm`
      - `cam_B`: 회전 **`0.180°`**, 위치 **`1.156 cm`**, 실측 대비 높이 잔차 `+0.15 cm`
      - `cam_C`: 회전 **`0.180°`**, 위치 **`1.180 cm`**, 실측 대비 높이 잔차 `+1.41 cm`
      - `cam_D`: 회전 **`0.170°`**, 위치 **`2.712 cm`**, 실측 대비 높이 잔차 `+1.62 cm`
    - 전역 독립 `holdout`(10개 창, `301`시각)에서 평가한 성능도 `-343` 최종판과 사실상 동일하다:
      - D 포함 쌍 통과율 평균: **`51.59%`** (`A/D 60.44%`, `B/D 44.16%`, `C/D 50.17%`) vs `-343` **`51.78%`** (`A/D 60.67%`, `B/D 44.51%`, `C/D 50.17%`)
      - 3+대 / 4대 융합률: **`74.83%` / `36.54%`** vs `-343` **`75.26%` / `35.29%`**
      - 전체 3+대 재투영 오차: **`3.986 / 15.164 px`** (`20,484`관절) vs `-343` **`3.924 / 15.033 px`** (`20,006`관절)

### 버린 대안과 남은 위험 (-345)

- **버린 대안**:
  - **Tier 0 프로세스 내부 스레드에서 후보 보정 실행**: Python GIL과 SciPy/NumPy 연산이 `P0_LIVE` 프레임 처리 지연을 흔들고, 타임아웃 시 스레드를 강제 종료할 수 없으므로 `spawn` 별도 프로세스 + OS 우선순위 하향(`BELOW_NORMAL_PRIORITY_CLASS` / `nice(10)`) + 단일 BLAS 스레드 격리를 택했다.
  - **수렴할 때마다 무조건 새 보정 제안**: 이미 보정이 회복된 뒤에도 창 표본이 바뀔 때마다 카메라 위치가 `1~2 cm`씩 미세하게 다른 새 버전이 매분 제안되어 Tier 0 상태가 흔들린다. 독립 `holdout` 개선 문턱(`+3.0%p` 융합률 등)을 두어 유의미한 개선이 있을 때만 제안하도록 막았다.
  - **회전 빠른 길(`realign_rotation`)만으로 종료**: 평행이동(`4.82 cm`)이 함께 일어난 `cam_D`에서는 중심 고정 회전만으로는 `cam_B/cam_D` 에피폴라 게이트가 열리지 않는다(`0.1%`). 따라서 회전 빠른 길은 모든 카메라 쌍 통과율 하한(`>= 25%`)과 `holdout` 개선을 통과할 때만 채택하고, 아니면 자동으로 전체 회차 보정(`adjust_session`)으로 넘어가도록 2단계로 구성했다.
- **남은 위험**:
  - 단일 코어 저우선순위 서브프로세스에서 5분 버퍼 전체에 대한 3라운드 번들 조정은 약 `95~200 s`가 걸린다. 기본 `timeout_s = 180.0 s`(필요 시 조정 가능)와 카메라당 프레임 상한(`max_frames_per_camera = 1,500`)으로 상한을 두었으나, 실시간 부하가 높을 때는 OS 스케줄러에 의해 워커 실행 시간이 더 길어질 수 있다.
  - `-346` 실시간 흔들림 감지와 Tier 0 보정 교체 상태 기계(`propose_calibration` 수신 후 실제 전환 시점 결정)와의 배선은 후속 통합 범위다.

## S15P31A101-352: 실행 중 보정의 기준 카메라·축척·바닥 계승

관련 명세는 §4.1(World 유지), §4.3(선택 높이와 축척), §4.5(의미 있는 개선 AND 검증), §3.2(Tier 0 우선)다. Lead·사용자 추가 결정(2026-10-30)과 `.agy/notes.md` 1~3절의 분석을 구현했다. 계약 변경은 없다.

### 원인과 결정

| 원인 | 측정 근거 | 결정 |
|---|---|---|
| 중심을 보지 않는 회전 전용 기준 선택 | fastrot-2에서 D 회전 차이가 1.5° 아래로 내려가 전체 후보 기준에 D가 다시 포함, 중심 최대 5.53cm로 거부 | 전체 닮음 정렬과 카메라별 LOO 포즈 잔차를 기록하고 중심·회전을 함께 판정 |
| 발목 바닥과 실측 높이가 매번 World를 다시 정함 | 단상 접지·짧은 버퍼의 바닥 오차가 World 정렬 전에 개입 | session 번들은 상대 포즈만 풀고 기준 카메라의 직전 World·미터 축척·바닥을 계승 |
| 초기 데이터 부족 | 첫 후보 중심 7.68cm + 공통 재투영 A +1.351px, D +1.159px 악화 | 기존 데이터·holdout 악화 한계를 유지, 중심 5cm 한계를 완화하지 않음 |

### 기준 카메라 선택과 한계

- 모든 카메라의 중심·방향을 함께 써 닮음 정렬(`H_centers + H_rots`)하고 전체 잔차를 `all_cameras_alignment`에 기록한다. bootstrap 복구 카메라와 과거 빠른 회전에서 중심을 고치지 않은 카메라는 제외한다.
- 남은 카메라가 3대 이상이면 하나씩 제외해 정렬한다. 남은 중심 최대 잔차, RMS, 회전 최대 잔차 순으로 최소인 부분집합을 고른다. 제외한 카메라의 중심 잔차가 **3.5cm 초과 AND 남은 최대의 1.8배 초과**, 또는 회전 **1.5° 초과 AND 남은 회전 최대는 1.5° 이하**이면 제외하고 반복한다. 전체 후보별 카메라 잔차와 제외 이유를 `leave_one_out`, `excluded_cameras`에 남긴다.
- 3.5cm는 기존 정상 기준의 120초 최대 2.991cm보다 크고, 문제 D의 위치 변화 4.818cm보다 작다. 1.8배 조건은 균일한 추정 흔들림을 한 카메라의 이동으로 단정하지 않게 한다. 세 카메라 기준의 기존 **중심 5cm·회전 1.5°** 한계는 유지한다.
- 기준 2대이면 `strict_two_anchor_limits_applied=true`, **중심 2.5cm·회전 1.0°·축척 변화 2%**다. 기준 3대 이상 축척 변화 한계는 **3%**(기존 카메라 높이 배율 분산 한도와 같은 보수적 상한)다. 두 카메라일 때 검증 여유도가 줄어드는 점을 고려해 한계를 강화했다. 1대 이하, 중심 간 거리 퇴화, 이전 UNCALIBRATED는 거부한다.
- 축척 변화는 **정렬 후** 기준 카메라 쌍별 거리의 직전 대비 상대 변화 최대값이다. 임의 게이지를 직전 미터 좌표에 되돌리는 `similarity_scale` 자체는 World 축척 변화와 구분한다. 운영 검증기는 문서의 실제 중심·방향·거리 변화도 다시 계산하며 보고서의 작은 잔차 주장만 믿지 않는다.

### 축척과 바닥

`adjust_session`은 높이를 받지 않아도 실행한다. `adjust_human_bundle`에 높이를 넘기지 않고 `BundleConfig.floor_contacts_for_gauge=false`를 쓴다. 접지 관측은 검산 보고를 위해 유지하되 solver의 접지 잔차에서는 제외하고 초기 포즈의 바닥 게이지를 유지한다. 그 뒤 기준 카메라 중심 간 거리와 방향으로 직전 World에 닮음 정렬한다. 단일뷰 높이·발목 바닥 미상 플래그만으로 수렴한 상대 번들을 거부하지 않으며 수렴 실패, 이상치·화면 관측 부족 등 다른 실패는 계속 거른다.

높이를 주면 정렬 후 높이 잔차·배율을 `height_scale_check_after`, `height_residuals_m`에 보고한다. 카메라 간 배율 분산 **3%** 한계와 실측 대비 절대 높이 잔차 **10cm** 한계(기존 `BundleConfig.max_height_check_diff_m`의 보수적 값)를 넘으면 거부하며, 이 검산 배율을 카메라에 적용하지 않는다. 발목 바닥 진단은 각 라운드의 `floor_check`에 남긴다(정렬 전 상대 번들 진단, 해당 라운드 정렬 변환도 함께 기록).

보고서의 `scale_basis="PREVIOUS_CALIBRATION"`은 직전 축척 계승을 나타낸다. 계약 `CalibrationDocument.scale_basis`에는 이 enum이 없으므로 **문서에는 직전 `scale_basis`를 그대로 계승**한다(notes 3절의 명시적 처리 방식). 높이를 새 주 근거로 승격하거나 계약 enum을 추가하지 않는다. 높이가 있어도 검산이라는 의미는 같다.

### 채택·빠른 경로·재시작

채택은 `geometry_quality.accepted AND world_consistency.accepted`다. `(가) 기하 품질`은 기존 holdout의 재투영·융합 개선 및 악화 한계를 그대로 적용한다. `(나) World 일관성`은 위 기준 카메라 위치·회전·축척 하드 조건이다. 두 축의 사유를 별도 구조 및 `(가)/(나)` 접두 로그로 남긴다.

빠른 회전은 fit 쌍 통과율이 최상 카메라의 **80% 미만**인 카메라만 시도한다(bootstrap과 동일). 따라서 D가 실패한 뒤 정상 A/B/C를 연쇄 회전시키지 않는다. 중심을 고정한 대상은 불변 보고서의 `world_alignment.fixed_center_rotation_cameras`에 누적 기록한다. 실제 활성 버전과 보고서 버전을 확인해 다음 전체 후보에 전달하며, `RuntimeCalibrationStore.current_report()`로 재시작 후에도 복원한다. 전체 session으로 중심까지 보정하면 이 표시는 끝난다. 보정 계약 문서에는 메타데이터를 추가하지 않는다.

### 코드·시험과 남은 위험

구현: `calibration/extrinsics/session.py`, `session_cli.py`, `cli.py`, `calibration/bundle/{inputs,adjust,solver}.py`, `live_candidate.py`, `runtime.py`, `services/tier0/{live_calibration,service}.py`. 회귀: 기존 네 시험 파일에 높이 없음·검산·닮음 변환·LOO·다중 이동·2대 엄격 한계·축 분리·문서 직접 검산·재시작 메타데이터 시험을 추가했다. 합성 입력 지표는 제품 성능이 아니다.

버린 대안은 중심 5cm 한계 완화, 매번 높이로 재축척, 발목으로 바닥을 다시 정하기다. 남은 위험은 기준 2대의 검증 여유 부족, 모든 카메라가 함께 움직였을 때 절대 이동을 구분할 외부 기준 부재, 직전 축척의 오차 계승, 짧은 버퍼의 LOO 제외 흔들림이다. 이번 120초 재생에서는 D bootstrap 제외 후 C의 LOO 중심 잔차 3.694cm(남은 최대 0.551cm)로 C도 제외되어 A/B 엄격 모드가 적용됐다. 이는 C의 물리 이동 확정이 아니라 해당 후보의 정렬 기준 제외다. 실제 GPU LIVE 지연·장기 안정성 검증은 후속이다. 재생 결과와 정확한 명령은 `.agy/notes.md`의 Codex 구현 절에 남긴다.

## S15P31A101-356: 실행 중 자동 보정 안전장치

관련 명세는 §3.2(Tier 0 우선), §4.1(World 유지), §4.4(귀속·근거 없음), §4.5(의미 있는 개선 AND 검증), §11.4(버전 추가)다. 계약·Tier 0 연관 규칙·관제 UI는 바꾸지 않는다. 기본값은 `live_calibration.safety_enabled=true`이며, `false`로 기존 즉시 채택/60초 주기/전체 카메라 버퍼 초기화 동작을 선택할 수 있다. 전체 자동 보정은 기존 `enabled=false`로 끈다.

### 관측 절단과 복구 소유권

- 감시기는 카메라별 귀속 가능 오차를 같은 시각별 중앙값으로 묶는다. 정상 문턱을 넘는 관측이 서로 다른 **3시각** 연속으로 들어오면 첫 시각을 `change_point_ms`로 잡는다. 순간적인 오검출 1~2회를 이동으로 단정하지 않기 위한 초기값이며 물리 이동 시각의 정답은 아니다. 표본 간격이 **5초**를 넘으면 연속 근거를 버린다.
- DRIFTED 확정 때 해당 카메라의 변화 시각 **이전** 프레임만 후보 버퍼에서 뺀다. 이후의 프레임·원본 픽셀·null·출처는 보존한다. 변화 시각이 없으면 통지 시각부터 다시 모은다. 컷오프보다 늦게 도착한 옛 프레임도 거부하고 로컬 추적 구간을 분리해 이동 전후 보간을 막는다. 다른 카메라의 과거 관측은 유지한다.
- DRIFTED 카메라는 기준 쌍에서는 계속 제외하되 다른 정상 카메라가 준 귀속 가능 근거로 그 카메라의 오차를 계속 잰다. 직전 상승 수준 대비 **max(3px, 50%)** 이상 달라진 관측이 3시각 지속되면 새로운 변화 시각을 잡는다. 재이동에 따른 후보 세대 증가로 실행 중 결과·대기 후보·연속 확인을 무효화한다. 이 수치는 재이동 중 큰 변화만 보수적으로 솎기 위한 초기값이다.
- 실행 잠금 하나와 기존 세대 검사로 일반 주기와 복구 주기가 동시에 후보를 만들지 못한다. 복구 중에는 같은 실행기가 **15초** 간격으로 시도한다. 여러 카메라가 이동하면 카메라별 절단 시각을 함께 유지한다. 사람 없음·관측 부족이면 기존 `SessionDataCriteria`로 보류하고 DRIFTED 불확실성을 유지한다. 귀속 불확실 또는 해상도/화각 변경이면 기존 운영자 확인/내부 파라미터 재추정 보류가 우선한다.

### 최초 기준과 누적 편차

- 저장된 활성화 버전 중 **처음 CALIBRATED 문서**를 기준으로 삼는다. 아직 확정 문서가 없으면 처음 시작 문서를 고정 기준으로 쓰되 그 상태를 `reference_state`로 기록하며 확정 보정으로 승격하지 않는다. 활성 버전 목록은 추가만 하므로 재시작 후에도 같은 기준을 찾는다.
- 먼저 -352의 직전 보정 대비 PnP/LOO/빠른 회전 기준 판정을 그대로 적용한다. 그 기준 중 이전 drift 전이 또는 후보 보고서에서 한 번이라도 이동/기준 제외가 기록된 카메라는 최초 기준 카메라에서 뺀다. 남은 **2대 이상**의 최초 자세에 후보 전체를 닮음 정렬한다. 2대 미만이면 실패로 보류한다. 과거 제외는 실제 물리 이동 확정과 다르지만 보수적으로 최초 기준 자격을 다시 주지 않는다.
- 최초 기준 정렬 뒤 holdout 재투영·융합과 World 일관성을 다시 평가한다. 최종 저장 단계에서도 직전 활성 문서 대비 중심·회전·축척 하드 한계를 직접 검산한다. 따라서 최초 기준 정렬을 이유로 -352의 2대 엄격 한계나 관측 품질 조건을 완화하지 않는다. 높이는 검산, 바닥은 기존 World 게이지다.
- `cumulative_deviation`은 기준 버전/상태, 모든 카메라의 회전·중심 차이, 기준 쌍 거리의 축척 차이를 기록한다. **중심 5cm / 회전 1.5도 / 축척 3%** 초과는 경고다(-352의 3대 이상 World 한도를 재사용). 실제 이동한 카메라의 의도한 자세 변경도 경고에 포함되며 자동 기각과 구분한다. 활성 보고서·서비스 상태에 남기고 warning 로그와 기존 health의 보정 문제 플래그로 전달한다. I1 health enum/스키마는 추가하지 않는다.

### 연속 확인과 첫 채택 지연

- 동일 활성 버전에서 **두 번 연속** 기하 품질·World 검증이 개선되어야 자동 교체한다. 두 번째 실행에는 앞 실행의 마지막 관측 시각보다 새로운 관측이 있어야 한다. 같은 버퍼를 반복 평가한 결과는 확인 횟수를 늘리지 않는다. 계산 실패·개선 없음·자료 부족·재이동·버전 전환은 확인을 초기화한다.
- 첫 개선 이후에는 평상시에도 **15초** 뒤 추가 관측으로 확인한다. 5분짜리 창 전체가 바뀔 때까지 기다리지 않는 이유는 시작 보정이 크게 틀린 경우 첫 채택이 지나치게 늦어지기 때문이다. 별도 비중첩 최근 구간 우회 조건은 쓰지 않고, 요구된 두 연속 개선 경로를 택했다. 겹치는 창의 통계적 독립성을 보장하는 것은 아니다.
- 감시의 **약 35초 감지**, **복구 1~2분**은 사람이 충분히 보일 때의 목표값이며 보장값이 아니다. 자료 최소량, 후보 계산시간, 15초 재확인, GPU 부하/180초 타임아웃, 카메라 가림에 따라 늘어나거나 복구가 보류될 수 있다. 합성 시험 수치를 제품 성능으로 쓰지 않는다.

### 교체 후 감시와 되돌림

- 연속 확인을 거친 자동 교체만 **60초** 감시한다. 이 동안 추가 자동 후보를 보류하고, 신선하고 충분한 재투영 창의 SUSPECT/DRIFTED 또는 후보 holdout의 3+대 융합률보다 **5%p 초과 하락**이 **10초** 지속되면 직전 활성 자세로 되돌린다. 융합률은 기존과 같은 2대 이상 클러스터 분모로 30초 창에서 모으며 최소 20개 클러스터가 필요하다. 사람이 없거나 새 근거가 없으면 성공이나 악화로 단정하지 않는다. 5초 초과 공백은 악화 지속을 다시 시작한다.
- 60초는 기본 30초 창+20초 drift 지속과 10초 추가 안전 여유를 담는 초기 관찰 시간이다. 10초는 한두 시각의 흔들림을 피하면서 기존 drift 지속보다 먼저 악화를 막기 위한 값이며, 5%p는 채택 개선 폭 3%p보다 큰 운영 악화 문턱이다. 현장 검증 전 수치다.
- 되돌림은 과거 문서를 활성으로 덮어쓰지 않는다. 직전 자세·내부 파라미터·출처를 복사한 **새 보정 버전(PROVISIONAL)**, 새 내용 해시, 불변 `rollback_of`/`restored_from` 보고서를 보관하고 다음 tick에서 기존 `calibration_update(reason=RECALIBRATED)` 전이를 추가한다. 직전 버전도 현재 관측으로 검증된 것은 아니므로 CALIBRATED로 복원하지 않는다.
- 저장 실패 시 기존 rig를 유지하고 다음 신선한 근거에서 되돌림 보관을 재시도한다. 활성화 저장 실패는 기존 tick 재시도 경로를 따른다. 되돌림 이후에는 운영자 확인까지 자동 후보를 보류해 재채택 반복을 막는다. 재시작 시 되돌림 보류와 자동 교체의 원래 활성화 시각을 복원한다. 악화 지속 카운터는 다시 신선한 근거로 쌓는다.

### 코드·시험·버린 대안·남은 위험

구현은 `calibration/{monitor,live_candidate,live_safety,runtime}.py`와 `services/tier0/{drift_watch,live_calibration,service,settings}.py`다. `test_live_calibration_safety.py`는 갑작스러운 한 대 이동, 두 대 동시 이동 후 자료 부족, 이동 뒤 사람 없음, 복구 도중 재이동, 연속 확인, 최초 기준 정렬, 교체 뒤 악화와 새 버전 되돌림/재시작/계약 호환을 검증한다. 기존 `test_live_calibration.py`의 실제 후보 계산→tick 전환 시험도 두 번 확인하도록 갱신한다. 정확한 실행 명령·최종 결과는 `.agy/notes.md`에 남긴다. 별도 리뷰는 사용자 지시로 생략한다.

버린 대안은 감지 시 5분 버퍼 전체 초기화(이동 후 자료도 유실), 새 후보마다 직전 자세만 잇기(누적 오차), 한 번 개선이면 채택(중첩 창의 우연한 개선), 과거 문서 수정으로 되돌림(감사 이력 유실)이다.

남은 위험은 시계열 변화 시각의 추정 오차와 3px 미만 재이동 미검출, 두 대 이상 이동 때 기하 귀속 부족, 정상 카메라의 보수적 영구 기준 제외로 인한 복구 보류, 최초 기준 자체의 오차, 모두 함께 움직인 상황의 절대 World 검증 불가, 사람/연관 구성 변화가 융합률에 미치는 영향, 60초 뒤에 생기는 악화와 사람이 없는 동안의 미검증이다. 되돌린 자세가 지금 더 좋다는 보장은 없으므로 PROVISIONAL+운영자 확인을 유지한다. GPU LIVE 지연·현장 오경보·장기 안정성은 아직 측정하지 않았으며 **9차 GPU 세션 종료 후에만 merge**한다.
