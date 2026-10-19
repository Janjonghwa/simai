# Tier 0 3D 관절 유효성 게이트 설계

S15P31A101-29. 기획서 §5.2(3D 관절 유효성 게이트, World 위치의 정의 `head_z`), §3.9(촬영 시각 계약), §7.1(`JOINT_UNCERTAIN`, `IDENTITY_UNCERTAIN`), 부록 D(D-G2-06) 기준이다.
코드는 `ai/src/simai_ai/geometry/joint_gate.py`, 시험은 `ai/tests/geometry/test_joint_gate.py`, `test_sim_joint_gate.py` 에 있다.

## 범위

§5.2 계산 순서 3단계(후보별 삼각측량과 검증)의 관절 판정을 맡는다. `triangulation.triangulate_skeleton`(S15P31A101-26)의 최소 기준(관측 수, 재투영 잔차, 시선각, 보정 상태) 위에 삼각측량 불확실성, 시간 일관성, 2대 관측 경쟁 후보를 더한다.

| 입력 | 형식 |
|---|---|
| 같은 사람의 카메라별 보간 keypoint | `InterpolatedView` (카메라 간 연결은 호출자가 이미 함) |
| 보정 | `CameraRig` (CalibrationProvider) |
| 같은 시각 다른 사람의 검출 | `others = {camera_id: [keypoints, ...]}` (경쟁 후보 검사용, **필수 인자**. 다른 사람이 없으면 `{}`, 모르면 `None`) |
| 동기화 무효 카메라 | `sync_invalid_cameras` (`SystemHealthReport.sync_invalid_cameras`) |

| 출력 | 내용 |
|---|---|
| `GatedSkeleton.to_skeleton()` | `world_state.schema.json#/$defs/skeleton` (joints, visible, validity, joint_source = observed) |
| `GatedJoint.reasons` | VALID 가 아닌 사유 코드 (`JointIssue` + `GateIssue`) |
| `GatedJoint.sigma_xyz_m`, `sigma_max_m`, `speed_mps` | 판정 근거 값 |
| `GatedSkeleton.identity_uncertain_candidate` | 2대 관측 경쟁 후보가 있는 관절이 있음 → 연결을 `IDENTITY_UNCERTAIN` 후보로 |
| `GatedSkeleton.head_z(offset)` | `HeadZ(value, validity, sigma_m, joints)` |

`JointGate` 는 World Track 하나에 하나씩 두는 상태 객체다(직전 VALID 위치, 뼈 길이 표본). World ID 병합·분할·교정(`identity_revision` 증가) 때는 `reset()` 한다. 현재 시각을 읽지 않고 공통 시각 t 만 쓰며, t 가 앞으로 가지 않으면 오류다.

## 판정

| 상태 | 조건 |
|---|---|
| UNAVAILABLE | 좌표 없음: 유효 관측 2대 미만(`TOO_FEW_VIEWS`), 퇴화(`DEGENERATE`), 이상치 제거 후 2대 미만(`INCONSISTENT_VIEWS`). 지어내지 않는다 |
| UNCERTAIN | 좌표는 있고 사유가 하나 이상: 아래 표 |
| VALID | 사유 없음 |

| 사유 | 기준 (초기값) | 근거 |
|---|---|---|
| `HIGH_REPROJECTION_ERROR` | 최대 재투영 잔차 > 8 px | §5.2 재투영 잔차 (`TriangulationConfig`) |
| `NARROW_RAY_ANGLE` | 시선 직선 사이 각 최댓값 < 5° | §5.2 두 시선 사이 각도 (`TriangulationConfig`) |
| `UNTRUSTED_CALIBRATION` | 보정 UNCALIBRATED·DRIFTED | §4.4 |
| `HIGH_TRIANGULATION_UNCERTAINTY` | 3D 공분산 가장 큰 축 표준편차 > 5 cm (keypoint σ = 2 px 가정) | §5.2 삼각측량 불확실성 |
| `SPEED_LIMIT` | 직전 VALID 위치 대비 속도 > 6 m/s (직전 VALID 가 500 ms 이내일 때만) | §5.2 시간 일관성(속도 상한) |
| `BONE_LENGTH_CHANGE` | 뼈 길이가 사람별 기준(최근 30 표본 중앙값, 5 표본 이상)에서 max(6 cm, 25 %) 넘게 바뀜. 두 끝점 모두 | §5.2 시간 일관성(뼈 길이 변화) |
| `TWO_VIEW_COMPETITOR` | 관측 2대뿐이고, 한쪽 관측의 에피폴라선 15 px 안에 다른 사람의 같은 관절이 있고 그 짝의 두 시선이 두 카메라 앞에서 만남 | §5.2 2대 관측 경쟁 후보, D-G2-06 |
| `COMPETITOR_CHECK_UNAVAILABLE` | 관측 2대뿐인데 경쟁 후보를 확인할 수 없음: `others=None`(다른 사람 검출을 모름), 또는 그 관절의 다른 사람 keypoint 에 NaN·inf 가 있거나 왜곡을 제거할 수 없음 | 모르면 VALID 아님 (§5.2, D-G2-06) |
| `FEW_VIEWS_FOR_VALID` | 관측 수 < `min_views_for_valid` (기본 2, 끄면 아무 일 없음) | 보수 설정용 |

- 게이트는 등급을 낮추기만 한다. 삼각측량이 UNCERTAIN·UNAVAILABLE 로 둔 관절을 VALID 로 올리지 않는다.
- **2대 관측.** 명세 §5.2 는 2대 관측 관절을 "에피폴라선 위에 경쟁 후보가 있으면" VALID 로 두지 않는다고 정한다. 그래서 기본값은 경쟁 후보가 없고 기하 기준을 모두 통과한 2대 관측 관절은 VALID 다. 2대 관측을 모두 막아야 하면 `min_views_for_valid = 3` 으로 바꾼다(명세 개정 필요).
- `others` 는 필수 인자다. "다른 사람 없음"은 빈 컨테이너(`{}`)로 명시한다. `None` 은 "모름"이라 경쟁 후보가 없다고 볼 수 없으므로 2대 관측 관절에 `COMPETITOR_CHECK_UNAVAILABLE` 을 붙여 UNCERTAIN 으로 둔다(3대 이상 관절은 영향 없음). 이 사람에게 묶인 검출은 `others` 에 넣지 않는다(카메라 간 연결 모듈 몫).
- `others` 의 keypoint 목록 길이가 관절 배치와 다르면 ValueError 다(`triangulate_skeleton` 과 같은 규칙). 다른 사람 keypoint 에 NaN·inf 가 있으면 그 후보만 건너뛰지 않고 그 관절의 경쟁 후보 검사를 "알 수 없음"으로 본다(같은 관절에 확실한 경쟁 후보가 따로 있으면 `TWO_VIEW_COMPETITOR`). 다른 사람의 잘못된 검출 하나로 이 사람 update 가 예외로 죽지 않는다. 이 사람 keypoint 에 NaN·inf 가 있거나 왜곡을 제거할 수 없으면 `triangulate_skeleton` 이 그 관측을 세지 않는다(S15P31A101-166, 이전에는 `LinAlgError`).
- 경쟁 후보 띠 15 px 는 정답 기준 띠 10 px(SIM 측정의 D-G2-06 정의)에 관측 잡음 여유 5 px 를 더한 값이다. 10 px 로 두면 SIM noisy 에서 띠 경계의 정답 경쟁 후보 관절이 VALID 로 새어 나왔다(basic/noisy 3, podium/noisy 1, crossing/noisy/2cam 40건). 15 px 에서는 모두 0 이다.
- 삼각측량 불확실성은 쓴 카메라의 이상적 핀홀 픽셀 투영 야코비안 J 로 σ²(JᵀJ)⁻¹ 이다. 시선각이 5° 를 넘어도 기선이 짧거나 멀면 깊이 표준편차가 커서 이 기준에 걸린다(예: 0.7 m 떨어진 두 카메라가 4 m 앞을 볼 때 시선각 약 10°, 깊이 σ 약 6 cm). 몬테카를로 산포와 20 % 안에서 맞는지 시험한다.
- 속도는 직전 VALID 위치와 비교한다. 한 프레임 튄 관절은 기준점이 되지 않는다. 순간이동으로 막힌 뒤 새 위치에 머물면 시간이 지나 속도가 상한 아래로 내려가거나 이력이 500 ms 를 넘어 다시 VALID 가 된다(영구히 막히지 않는다).
- 뼈 길이 기준 표본은 시간 검사 전 기하 기준을 통과한 끝점끼리만 쓴다. 시간 검사 결과로 표본을 고르면 처음 잡힌 잘못된 기준이 고정될 수 있다. 얼굴 관절 사이 뼈는 짧아 keypoint 잡음과 구분되지 않아 뺐다. 어느 끝점이 틀렸는지 모르므로 두 끝점 모두 UNCERTAIN 으로 둔다(보수 쪽).
- 동기화 무효 카메라(§3.9)는 삼각측량과 경쟁 후보 검사에서 뺀다. 남은 관측이 2대 미만이면 그 관절은 비운다.

## head_z

§5.2 정의대로 VALID 인 양쪽 귀·눈 Z 평균 + 고정 오프셋이다. VALID 귀·눈이 없으면 UNCERTAIN 귀·눈으로 값을 내고 UNCERTAIN 으로 표시한다(낙상 R4 는 "확인 필요"). 좌표가 있는 귀·눈이 없으면 비운다. 오프셋 초기값 0.12 m 는 성인 귀·눈 높이에서 머리 꼭대기까지의 대략값이다. 귀·눈 가운데 일부만 VALID 이면 평균 높이가 달라지므로(귀가 눈보다 약간 낮다) 그 편향도 G2 에서 함께 본다.

**World Track 에 싣는 방법.** `world_state` 2.0.0(S15P31A101-179, 사용자 결정 2026-10-19)은 `HeadZ.validity` 를 `head_z_validity` 로 늘 싣고, VALID·UNCERTAIN 이면 값·σ 를, UNAVAILABLE 이면 null 을 싣는다(`tracking/world_state.py`). 값은 같은 Track skeleton 의 관측(observed) 귀·눈이 `HeadZ.joints` 와 유효성까지 맞을 때만 싣고, 어긋나면 UNAVAILABLE 이다. 소비자는 값이 있어도 `head_z_validity` 를 확인하고 UNCERTAIN 값을 확정 근거로 쓰지 않는다. (이전: 1.4.0 은 VALID 일 때만 값, -172.)

## G2·A0 에서 조정할 값

| 설정 (`JointGateConfig`) | 초기값 | 다시 정할 근거 |
|---|---|---|
| `triangulation.max_reprojection_error_px` | 8 px | 실측 보정 재투영 오차(G1)와 YOLO26x-pose keypoint 오차 |
| `triangulation.min_ray_angle_rad` | 5° | G2 머리 Z 오차와 교실 카메라 배치 |
| `keypoint_sigma_px` | 2 px | YOLO26x-pose 관절별 픽셀 오차(D-G0-05) |
| `max_position_sigma_m` | 5 cm | G2 머리 Z p95 10 cm |
| `max_joint_speed_mps`, `max_history_gap_ms` | 6 m/s, 500 ms | 교실 동작 녹화(팔 동작, 앉기, 낙상 시늉) |
| `bone_abs_tolerance_m`, `bone_rel_tolerance`, `bone_window`, `bone_min_samples` | 6 cm, 25 %, 30, 5 | 실측 keypoint 의 뼈 길이 흔들림 |
| `competitor_epipolar_px` | 15 px (정답 띠 10 px + 잡음 여유 5 px) | D-G2-06 2대 영역 오연결률, 실측 keypoint 잡음 |
| `head_top_offset_m` | 0.12 m | G2 머리 높이(줄자 키 + 서 있는 면 높이) |

## SIM 측정

`ai/` 에서 `uv run python tests/geometry/sim_joint_gate.py` 로 다시 만든다. 시뮬레이터 실행과 입력 읽기는 `sim_accuracy.py` 와 같다(입력은 `observations/`·`calibration.json`, 정답은 오차 계산에만). 카메라 간 연결은 시뮬레이터 정답이고, `others` 는 같은 시각 다른 사람들의 보간 keypoint 다. `crossing/noisy/2cam` 은 cam1·cam2 두 대만 쓴 변형이다.

**입력 출처:** `input_provenance = {frames: SIM, calibration: SIM, is_upper_bound: true}`. 아래 값은 입력이 완벽할 때의 상한이며 제품 성능이 아니다(§20.4 규칙 3). G2 합격 판정에 쓰지 않는다.

결과(2026-10-17, 노트북 CPU, 단위 cm, 기본 설정). "기하만"은 삼각측량 최소 기준(S15P31A101-26)만 본 VALID 이다. head_z 오차는 시뮬레이터 몸의 오프셋(키 × 0.0675/1.70)으로 잰 것이라 삼각측량·게이트 부분만 본다:

| 시나리오/변형 | VALID 비율 | 기하만 VALID 비율 | VALID 오차 중앙값 | VALID p95 | VALID 최대 | 불연속 밖 VALID 최대 | UNCERTAIN 오차 p95 | 5 cm 넘는 VALID (게이트/기하만) | head_z 오차 p95 | UNCERTAIN 사유 | IDENTITY_UNCERTAIN 후보 tick | 정답 경쟁 후보 2대 관절 (VALID) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| basic/clean | 0.976 | 0.989 | 0.00 | 0.06 | 10.37 | 0.14 | 12.59 | 12/13 | 1.23 | BONE_LENGTH_CHANGE 4, HIGH_REPROJECTION_ERROR 15, TWO_VIEW_COMPETITOR 67 | 24 | 54 (0) |
| basic/noisy | 0.976 | 0.990 | 0.51 | 1.05 | 10.03 | 2.50 | 12.88 | 11/14 | 1.22 | BONE_LENGTH_CHANGE 4, HIGH_REPROJECTION_ERROR 10, TWO_VIEW_COMPETITOR 67 | 23 | 54 (0) |
| podium/clean | 0.960 | 0.975 | 0.00 | 0.07 | 17.77 | 0.14 | 17.01 | 40/46 | 1.14 | BONE_LENGTH_CHANGE 8, HIGH_REPROJECTION_ERROR 43, TWO_VIEW_COMPETITOR 54 | 26 | 32 (0) |
| podium/noisy | 0.961 | 0.978 | 0.48 | 1.29 | 21.37 | 3.29 | 15.61 | 49/60 | 0.83 | BONE_LENGTH_CHANGE 16, HIGH_REPROJECTION_ERROR 34, TWO_VIEW_COMPETITOR 58 | 28 | 32 (0) |
| crossing/clean | 0.984 | 0.984 | 0.01 | 0.05 | 6.25 | 0.09 | 6.85 | 2/2 | 0.02 | HIGH_REPROJECTION_ERROR 4 | 0 | 0 (0) |
| crossing/noisy | 0.985 | 0.985 | 0.53 | 1.17 | 15.35 | 2.33 | 14.38 | 9/9 | 0.32 | HIGH_REPROJECTION_ERROR 1 | 0 | 0 (0) |
| crossing/noisy/2cam | 0.585 | 0.770 | 0.75 | 1.55 | 18.32 | 2.72 | 1.46 | 8/11 | 1.78 | TWO_VIEW_COMPETITOR 441 | 130 | 360 (0) |

- 지어낸 관절(2대 미만인데 좌표 있음)은 모든 경우 0 이다.
- UNCERTAIN 관절의 오차 p95(7~17 cm)가 VALID p95(0.05~1.6 cm)보다 훨씬 크다. 4대 시나리오에서 게이트가 더 낮춘 관절(VALID 비율 0~1.7 %p 감소)은 주로 2대에만 보이고 다른 사람의 같은 관절이 에피폴라선 가까이 온 경우다(`TWO_VIEW_COMPETITOR`).
- 5 cm 넘는 VALID 관절은 모두 시뮬레이터 움직임 불연속(경유점, 단상 오르내림) ±100 ms 안에 있다(불연속 밖 VALID 최대 3.3 cm). 40 ms 간격 두 프레임 선형 보간이 순간적인 변화를 틀리게 만든 경우로, 카메라끼리 서로 맞는 틀린 3D 라 재투영·시선각·불확실성으로 잡히지 않고, 크기(최대 21 cm, 100 ms)가 속도 상한(6 m/s) 아래다. 게이트는 그중 일부(basic 1~3건, podium 6~11건)만 더 걸러낸다. 실제 사람의 움직임은 연속이라 이 크기는 생기기 어렵다(tier0-triangulation.md).
- **D-G2-06 독립 계수.** "정답 경쟁 후보 2대 관절 (VALID)" 열은 게이트 자신의 플래그가 아니라 정답 3D 투영으로 센다: 게이트가 2대로 삼각측량한 관절에서, 한쪽 카메라의 이 사람 정답 관절 투영의 에피폴라선 10 px 안에 다른 쪽 카메라에 보인(관측 신뢰도 기준 이상) 다른 사람의 같은 관절 정답 투영이 있으면 경쟁 후보다. 괄호 안이 그중 VALID 로 판정된 수이며 모든 변형에서 0 이다. `TWO_VIEW_COMPETITOR` 사유 수는 게이트 기준(15 px, 관측 픽셀)이라 정답 기준 수보다 많다.
- `crossing/noisy/2cam`: 엇갈리는 두 사람의 같은 관절이 에피폴라선 가까이 올 때 VALID 로 두지 않는다. 이 SIM 은 연결이 정답이라 막힌 관절의 오차 p95 는 1.5 cm 로 작다. 경쟁 후보 검사는 연결이 맞는지 모르는 상태에서의 보수 판정이고, 2대 영역에서 VALID 비율이 77 %→58 % 로 줄어드는 비용이 있다. 오연결률은 연결 모듈(S15P31A101-34)과 함께 G2 에서 잰다.
- 삼각측량 불확실성과 속도 상한은 이 SIM(9 × 7.5 m 교실, 모서리 4대)에서 걸리지 않았다. 단위 시험(짧은 기선, 순간이동)으로 확인한다.

### 허용 오차

회귀를 잡기 위한 SIM 상한 기준이다(`tests/geometry/test_sim_joint_gate.py`). G2 합격 기준이 아니다.

| 항목 | clean | noisy |
|---|---|---|
| 불연속 밖 VALID 관절 최대 오차 | 1 cm 이하 | 6 cm 이하 |
| head_z 오차 p95 (SIM 오프셋) | 2 cm 이하 | 3 cm 이하 |
| VALID 비율 (4대) | 0.95 이상 | 0.95 이상 |
| VALID 비율 ≤ 기하만 VALID 비율 | 늘 | 늘 |
| 지어낸 관절 | 0 | 0 |
| 정답 기준 경쟁 후보가 있는 2대 관절의 VALID (D-G2-06) | 0 | 0 |
| `crossing/noisy/2cam` 경쟁 후보 사유·IDENTITY_UNCERTAIN 후보·정답 경쟁 후보 | | 1건 이상 |

## 음성 대조

임계값을 느슨하게 하면 판정 단언이 실패하는지 단위 시험으로 본다: 속도 상한 100 m/s(순간이동 통과), 뼈 길이 상대 허용 10배(손목 급변 통과), 불확실성 상한 1 m(짧은 기선 통과), 에피폴라 기준 1 px(3 px 떨어진 경쟁 후보 놓침).

## 한계와 남은 일

- 경쟁 후보는 같은 관절만 본다. 다른 관절(예: 왼손목과 오른손목 혼동)은 보지 않는다.
- 뼈 길이 위반은 두 끝점 모두 낮춘다. 한쪽 끝점이 이미 기하 기준에서 UNCERTAIN 이면 그쪽만 낮추는 세분화는 실측을 보고 정한다.
- 속도 상한은 관절 하나에 하나다. 손목처럼 빠른 관절에 따로 둘지는 교실 동작 녹화로 정한다.
- 불확실성은 픽셀 잡음만 본다. 보정 오차와 시간 보간 오차(촬영 시각 불확실성 × 속도)는 넣지 않았다.
- `head_z` 오프셋의 불확실성은 `sigma_m` 에 넣지 않았다. World Track 조립에서 `head_z_sigma` 로 쓸 때 정한다.
- 처리 시간: 공분산은 관절 전체를 한 번에 계산하고, Cross-view 클러스터의 왜곡 제거·삼각측량 결과(`undistorted`, `others_undistorted`, `skeleton`)를 넘기면 입력이 같을 때만 다시 쓴다(S15P31A101-168, `tier0-geometry-budget.md`). World Track Manager 는 이 인자로 넘겨야 10명 기준 10 Hz 예산 안에 든다.
- 게이트 결과를 World Track(`world_state`)에 싣고 `IDENTITY_UNCERTAIN` 을 identity_confidence 에 반영하는 조립은 World Track Manager·Tier 0 서비스 조립에서 한다.
