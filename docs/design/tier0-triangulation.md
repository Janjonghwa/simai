# Tier 0 시간 보간·다시점 삼각측량 설계

S15P31A101-26. 기획서 §3.9(촬영 시각 계약), §5.2(삼각측량, 계산 순서, World 위치의 정의, 3D 관절 유효성 게이트), §6.1(시간 모델), §12.7(역순·지연 도착), §15.1(G0·G2) 기준이다.
코드는 `ai/src/simai_ai/geometry/` 의 `interpolation.py`, `rig.py`, `triangulation.py` 이고, 시험은 `ai/tests/geometry/` 에 있다.

## 범위

§5.2 계산 순서 가운데 1단계(카메라별 2D keypoint 를 공통 시각으로 시간 보간)와 3단계(후보별 삼각측량과 검증)의 기본 부품을 만든다.

| 단계 | 이 문서 | 다른 티켓 |
|---|---|---|
| 1. 공통 시각 보간 | `interpolation.py` | |
| 2. 카메라 간 연결 후보 | | Cross-view Association. 이 모듈은 이미 묶인 사람 한 명의 카메라별 관측을 받는다 |
| 3. 삼각측량과 검증 | `triangulation.py` (최소 기준) | 관절 유효성 게이트 상세(시간 일관성, 삼각측량 불확실성)는 S15P31A101-29 |
| 4. ContactEstimator | | 접지점(`ground_anchor_xyz`), 지지 표면, `z_validity` |
| 5. World Track 갱신 | | `position_xy`, 속도, 관측 상태, `head_z` |

이 모듈은 Tier 0 위치 정본의 재료(관절 3D)를 만든다. 결과 관절은 모두 `joint_source = observed` 이고, HMR·정적 3D·VLM 결과를 받지 않는다. 접지점은 만들지 않는다(Z=0 대체 금지).

## 입력과 출력

| 항목 | 형식 | 비고 |
|---|---|---|
| 2D keypoint | `KeypointSample(camera_id, estimated_capture_time, timestamp_source, timestamp_uncertainty_ms, keypoints, frame_ref)` | §3.9 촬영 시각 계약 필드. keypoints 는 joint_layout 순서의 `(u, v, conf)` 또는 None. 원본 해상도 픽셀 |
| 보정 | `CalibrationProvider` → `CameraRig.from_provider()` | `calibration.schema.json` 그대로. World(m, 오른손, +Z)·`OPENCV_WORLD_TO_CAMERA` 가 아니면 받지 않는다. 핀홀은 `coordinates.PinholeCamera` 를 그대로 쓴다 |
| 보간 결과 | `InterpolatedView(camera_id, t, keypoints, timestamp_source, timestamp_uncertainty_ms, source_times)` | 쓰지 않은 프레임은 `FrameDrop(camera_id, t, reason, estimated_capture_time, frame_ref)` 로 남긴다 |
| 삼각측량 결과 | `SkeletonEstimate` | `to_skeleton()` 은 `world_state.schema.json#/$defs/skeleton` 문서, `pelvis_xyz()` 는 양쪽 엉덩이 중점(하나라도 없으면 None), `camera_ids` 는 `world_track.cameras` |
| 관절별 근거 | `JointEstimate(xyz, validity, camera_ids, rejected_camera_ids, reprojection_error_px, ray_angle_rad, issues)` | S15P31A101-29 게이트의 입력 |

시각은 UTC int64 ms, 각도는 라디안, 길이는 미터다. 현재 시각을 직접 읽지 않는다(공통 시각 t 는 호출자가 정한다).

## 시간 보간

| 규칙 | 초기값 | 근거 |
|---|---|---|
| 촬영 시각 불확실성이 기준을 넘거나 값이 없는(NaN·음수 포함) 프레임은 쓰지 않는다. t 를 감싸는 프레임이 그렇다면 그 카메라는 시각 t 계산에서 빠진다(`TIMESTAMP_UNCERTAIN`) | 25 ms | §3.9 |
| t 앞뒤로 가장 가까운 두 프레임을 선형 보간한다. t 와 같은 시각의 프레임이 있으면 그것만 쓴다 | | §3.9, §5.2 1단계 |
| 감싸는 프레임이 t 에서 보간 창보다 멀면 버리고 기록한다(`OUTSIDE_WINDOW`). 창은 t 에서 각 프레임까지의 거리라 t − 200 ms 와 t + 200 ms 프레임은 쓰고 201 ms 는 버린다 | 200 ms | §12.7 |
| 외삽하지 않는다. t 뒤 프레임이 아직 없으면 `NO_FRAME_AFTER`, 앞 프레임이 없으면 `NO_FRAME_BEFORE` | | 모르면 비운다 |
| 관절은 두 프레임 모두에 있을 때만 보간한다. 신뢰도는 둘 중 작은 값이다 | | 지어내지 않는다 |
| 늦게 도착한 프레임은 받은 최신 촬영 시각에서 창 안이면 정렬해 쓰고, 창을 넘기면 버리고 기록한다(`KeypointBuffer`, `LATE_ARRIVAL`) | 200 ms | §12.7 |

- 공통 시각 t 는 모든 유효 카메라에 t 이후 프레임이 도착한 시각(카메라별 최신 촬영 시각의 최솟값 근처)으로 고르는 것이 기본이다. 외삽하지 않으므로 t 를 너무 최신으로 잡으면 카메라가 빠진다. t 를 정하는 정책은 World Track 갱신 주기(10 Hz)와 함께 Tier 0 조립에서 정한다.
- 보간 결과의 `timestamp_uncertainty_ms` 는 쓴 프레임 불확실성의 최댓값이다. 두 프레임의 `timestamp_source` 가 다르면 더 약한 근거인 `RECEIVE_ESTIMATED` 로 적는다.
- 동기화가 무효인 카메라(`sync_offset_ms` 측정 결과)를 빼는 판단은 System Health·시간 모델(tracking) 몫이다. 이 모듈은 프레임에 붙은 불확실성만 본다.

## 삼각측량

1. 보정의 렌즈 왜곡을 제거해 정규화 좌표를 얻는다(`geometry.distortion`). OpenCV 핀홀(계수 4·5·8개)과 어안(4개)을 지원하고 왜곡 식은 OpenCV 와 같다. 반지름 방정식을 구간을 지키는 뉴턴법(구간 밖으로 나가면 이분법)으로 풀고, 접선 계수가 있으면 2차원 감쇠 뉴턴법으로 다듬는다. `cv2.undistortPoints` 식의 고정점 반복은 강한 광각 배럴 왜곡(k1 −0.30, k2 0.09, C110 2304x1296)의 모서리에서 발산해 수백 px 어긋난 광선을 냈다(S15P31A101-166).
   - 왜곡 식이 0 에서 처음 단조성을 잃는 반지름(도함수 ≤ 10⁻⁶, 유리 모델 분모 ≤ 10⁻⁶, 핀홀 상한 r = 10, 어안 상한 θ = 90°)은 r² 에 대한 다항식의 근과 극값으로 찾는다(표본 사이의 좁은 접힘도 놓치지 않는다). 그 너머의 픽셀, 수렴하지 않은 픽셀, 다시 왜곡한 픽셀이 입력과 허용치(`undistort_tolerance_px`, 초기값 0.01 px)보다 다른 픽셀, 유한하지 않은 픽셀은 광선을 지어내지 않고 NaN(+ `normalize_with_mask` 실패 마스크)으로 돌려준다.
2. 신뢰도가 기준(초기값 0.3) 이상이고, 값(u, v, 신뢰도)이 모두 유한하고, 왜곡을 제거할 수 있는 관측만 센다. 2대 미만이면 삼각측량하지 않는다(null, `visible = false`, `UNAVAILABLE`, `TOO_FEW_VIEWS`). 풀 수 없는 관측 때문에 예외로 멈추지 않는다.
3. 정규화 좌표 선형 DLT(행마다 x·P₃ − P₁, y·P₃ − P₂, P = [R | t], 신뢰도 가중)로 초기값을 구한다. 두 번째로 작은 특이값이 최대 특이값의 10⁻⁹ 이하이면 해 공간이 2차원 이상(모든 시선이 한 직선, 공선)이라 해가 유일하지 않으므로 null(`DEGENERATE`)이다.
4. 가중 픽셀 재투영 오차 제곱합을 가우스-뉴턴(레벤버그-마콰트 감쇠, 최대 10회)으로 줄인다. 재투영 오차는 왜곡을 제거한 이상적 핀홀 픽셀 공간에서 잰다.
5. 해가 어느 카메라 뒤에 있거나 해가 없으면 그 관절은 null(`DEGENERATE`)이다.
6. 가장 나쁜 카메라의 재투영 오차가 이상치 기준(초기값 20 px)을 넘으면 그 카메라를 빼고 다시 푼다. 2대가 남았는데도 기준을 넘으면 서로 맞는 관측 쌍이 없다고 보고 null(`INCONSISTENT_VIEWS`)이다. 최소 관측 수를 채우려고 이상치 관측을 세지 않는다. 뺀 카메라는 뺀 순서대로 `rejected_camera_ids` 에 남기고, null 이면 마지막 두 카메라도 이어 붙인다.

### 관절 상태 (최소 기준)

| 상태 | 조건 |
|---|---|
| UNAVAILABLE | 유효 관측 2대 미만(`TOO_FEW_VIEWS`), 해가 없거나 유일하지 않음(`DEGENERATE`), 이상치(재투영 오차 > 20 px)를 빼고 나면 2대 미만(`INCONSISTENT_VIEWS`). 좌표는 null |
| UNCERTAIN | 좌표는 있지만 아래 중 하나: 최대 재투영 오차 > 8 px(20 px 이하), 시선각 최댓값 < 5°(0.0873 rad), 보정 상태가 UNCALIBRATED·DRIFTED |
| VALID | 위에 해당하지 않음 |

- 재투영 기준 8 px 는 G5 "한 카메라 제외 재투영 오차 p95 8px" 와 같은 크기로 잡았다. 실측 보정의 재투영 오차(G1 중앙값 2 px, p95 5 px)와 YOLO26x-pose keypoint 오차를 보고 S15P31A101-29 에서 다시 정한다.
- 재투영 오차 8~20 px 는 좌표를 두고 UNCERTAIN, 20 px 초과는 그 관측을 뺀다(남은 관측이 2대 미만이면 null). 두 기준은 다른 정책이다.
- 시선각이 작으면 깊이와 높이가 불안정하다(§5.2). 시선각은 카메라 중심과 점을 잇는 두 직선 사이 각(0~90°)이다. 방향 벡터 사이 각(0~180°)으로 재면 서로 마주 보는 카메라의 반대 방향 시선(180° 근처)이 좋은 배치로 잘못 통과한다. 이 경우 두 시선이 거의 한 직선이라 1 px 차이가 수십 cm 오차가 되고, 재투영 오차는 0 에 가까워 걸러지지 않는다. 교실 모서리 4대 배치에서도 대각선 맞은편 두 카메라만 보는 관절은 이 조건에 걸릴 수 있다.
- 보정 PROVISIONAL 은 관절을 막지 않는다. 높이 규칙의 "확인 필요"는 Risk Engine 이 보정 상태로 처리한다(§4.4, G3).
- 시간 일관성(속도 상한, 뼈 길이 변화)과 삼각측량 불확실성은 S15P31A101-29 가 `JointEstimate` 의 근거 필드 위에 더한다.

## SIM 측정

`ai/` 에서 `uv run python tests/geometry/sim_accuracy.py --baseline` 으로 다시 만든다. 시뮬레이터 내장 시나리오 3종(`basic`, `podium` = 단상, `crossing`)을 돌리고, 입력은 `observations/` 와 `calibration.json` 만 쓴다. 정답(`truth/world_truth.jsonl`)은 오차 계산에만 쓴다.
카메라 간 연결은 이 티켓 범위가 아니어서 시뮬레이터 정답 연결(`world_tracks.jsonl` 의 `per_camera`)을 썼다. World tick(10 Hz)마다 사람별로 보간 → 삼각측량하고, 모든 관절의 정답 3D 와 비교한다.

**입력 출처:** `input_provenance = {frames: SIM, calibration: SIM, is_upper_bound: true}`. 아래 값은 입력이 완벽할 때의 상한이며 제품 성능이 아니다(§20.4 규칙 3). 실측 오차는 보정 오차와 keypoint 오차가 지배한다.

| 변형 | 내용 |
|---|---|
| clean | 픽셀 잡음 σ = 0, 촬영 지터 0, 보고 시각 오차 0. 카메라별 촬영 시각 오프셋(0·7·13·21 ms)은 그대로 둔다 |
| noisy | 내장 시나리오 그대로: 픽셀 잡음 σ = 1 px, 촬영 지터 ±3 ms, 보고 시각 오차 podium ±2 ms·crossing ±4 ms |
| `/nearest` | 같은 입력을 보간 없이 카메라마다 촬영 시각이 가장 가까운 프레임으로 푼 비교 기준 |

결과(2026-10-17, 노트북 CPU, 단위 cm). 시선각을 직선 사이 각으로 바꾸고 이상치 2대 null 규칙을 더한 뒤 다시 돌려도 모든 값이 같았다:

| 시나리오/변형 | 추정 관절 비율 | VALID 비율 | 관절 오차 중앙값 | p95 | 최대 | VALID p95 | VALID 최대 | 불연속 밖 VALID 최대 | Z p95 | 골반 XY p95 | 버린 프레임 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| basic/clean | 0.992 | 0.989 | 0.00 | 0.07 | 15.07 | 0.06 | 10.37 | 0.14 | 0.04 | 0.01 | NO_FRAME_BEFORE 6 |
| basic/clean/nearest | 0.999 | 0.995 | 0.00 | 1.87 | 32.48 | 1.84 | 13.13 | 3.66 | 0.58 | 1.41 | - |
| basic/noisy | 0.992 | 0.990 | 0.51 | 1.08 | 15.25 | 1.07 | 10.03 | 2.50 | 0.58 | 0.62 | NO_FRAME_BEFORE 6 |
| basic/noisy/nearest | 0.999 | 0.996 | 0.81 | 2.07 | 32.15 | 2.05 | 7.61 | 4.22 | 0.88 | 1.53 | - |
| podium/clean | 0.986 | 0.975 | 0.00 | 0.10 | 18.66 | 0.07 | 17.77 | 0.14 | 0.05 | 0.02 | NO_FRAME_BEFORE 6, OUTSIDE_WINDOW 2 |
| podium/clean/nearest | 0.998 | 0.983 | 0.00 | 1.95 | 31.62 | 1.78 | 31.62 | 3.13 | 0.74 | 1.21 | - |
| podium/noisy | 0.986 | 0.978 | 0.49 | 1.49 | 21.37 | 1.34 | 21.37 | 3.29 | 0.67 | 0.69 | NO_FRAME_BEFORE 6, OUTSIDE_WINDOW 2 |
| podium/noisy/nearest | 0.998 | 0.990 | 0.79 | 2.32 | 32.04 | 2.21 | 32.04 | 3.71 | 0.91 | 1.48 | - |
| crossing/clean | 0.985 | 0.984 | 0.01 | 0.05 | 7.13 | 0.05 | 6.25 | 0.09 | 0.02 | 0.01 | NO_FRAME_BEFORE 6 |
| crossing/clean/nearest | 1.000 | 0.996 | 0.98 | 1.63 | 20.57 | 1.61 | 3.85 | 2.60 | 0.34 | 1.19 | - |
| crossing/noisy | 0.985 | 0.985 | 0.53 | 1.17 | 15.35 | 1.17 | 15.35 | 2.33 | 0.63 | 0.67 | NO_FRAME_BEFORE 8 |
| crossing/noisy/nearest | 1.000 | 0.997 | 1.05 | 2.09 | 20.48 | 2.08 | 11.77 | 3.40 | 0.86 | 1.46 | - |

- 추정 관절 비율 = 좌표를 낸 관절 / 정답 관절 전부(가림 포함). 2대 미만으로 빠진 관절은 null 이고 지어낸 관절(2대 미만인데 좌표가 있는 관절)은 모든 경우 0 이다.
- 시간 보간으로 관절 오차 p95 가 보간 없는 경우보다 noisy 에서 0.5~0.65배, clean 에서 1/20~1/30 로 줄었다. 카메라 간 촬영 시각 차이(최대 21 ms)에 걸음 속도가 곱해진 오차를 없앤 것이다.
- 최대 오차(7~21 cm)는 모두 시뮬레이터 움직임이 불연속인 시각에 나온다. 시뮬레이터 사람은 경유점에서 속도·방향·팔다리 흔들림이, 단상을 오를 때 높이(0.2 m)가 한 프레임 사이에 바뀐다. 이 순간을 40 ms 간격 두 프레임으로 선형 보간하면 실제와 다르다. 경유점과 지지 표면이 바뀐 시각 ±100 ms 를 뺀 VALID 최대 오차는 clean 0.14 cm, noisy 3.3 cm 다. 실제 사람의 움직임은 연속이라 이 크기의 오차는 생기기 어렵지만, 빠른 팔 동작에서는 보간 오차가 커질 수 있으므로 S15P31A101-29 의 시간 일관성 검사가 잡도록 한다.
- 버린 프레임: `NO_FRAME_BEFORE` 는 시작 시각에 시각 오프셋이 있는 카메라의 첫 프레임이 아직 없는 경우, `OUTSIDE_WINDOW` 는 podium 에서 학생이 교사에 가려 cam3 의 검출이 280 ms(5773→6053 ms) 끊긴 동안이다. 그 시각에는 cam3 를 빼고 나머지 카메라로 푼다.
- 시뮬레이터는 `capture_jitter_ms + timestamp_error_ms` 가 프레임 간격 절반(20 ms)보다 작아야 해서 불확실성 25 ms 초과 제외는 SIM 으로 재현하지 않고 단위 시험(`test_interpolation.py`)으로 확인한다.

### 허용 오차

회귀를 잡기 위한 SIM 상한 기준이다(`tests/geometry/test_sim_accuracy.py`). G2 합격 기준이 아니며, G2(World XY 중앙값 10 cm·p95 25 cm, 머리 Z p95 10 cm)는 실측 교실에서 따로 잰다.

| 항목 | clean | noisy |
|---|---|---|
| 관절 오차 p95 | 0.5 cm 이하 | 3 cm 이하 |
| 불연속 밖 VALID 관절 최대 오차 | 1 cm 이하 | 6 cm 이하 |
| 관절 Z 오차 p95 | 0.5 cm 이하 | 2 cm 이하 |
| 골반 XY 오차 p95 | 0.5 cm 이하 | 2 cm 이하 |
| 추정 관절 비율 | 0.95 이상 | 0.95 이상 |
| 지어낸 관절(2대 미만인데 좌표 있음) | 0 | 0 |
| `input_provenance.is_upper_bound` | true | true |

시간 보간이 보간 없는 경우보다 관절 오차 p95 가 작은지도 `basic` 으로 확인한다.

## 한계와 남은 일

- 카메라 간 연결은 시뮬레이터 정답을 썼다. 연결 오류(다른 사람 관측이 섞임)가 있으면 3대 이상에서는 이상치 제거가 일부 막지만, 2대에서는 재투영 오차 20 px 이하이면 UNCERTAIN 이 될 뿐이다. 연결 모듈과 함께 다시 잰다.
- 관절 유효성 게이트의 시간 일관성(속도 상한, 뼈 길이 변화)과 삼각측량 불확실성(공분산)은 S15P31A101-29 에서 더했다(`tier0-joint-gate.md`).
- 공통 시각 t 를 고르는 정책과 `KeypointBuffer` 를 카메라·트랙별로 관리하는 조립은 Tier 0 서비스 조립에서 한다.
- 실측 보정·YOLO26x-pose keypoint 로 재투영 기준(8 px)과 신뢰도 기준(0.3)을 다시 정한다.
- `head_z`(머리 높이)는 COCO17 에 머리 꼭대기 관절이 없어 여기서 정하지 않았다. 얼굴 관절 Z 와 머리 꼭대기의 차이를 어떻게 다룰지 World Track 쪽에서 정한다.
