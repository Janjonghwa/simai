# Tier 0 기하 단계 처리 시간과 10 Hz 예산

S15P31A101-168. 기획서 §5.2(3D 골격·World 상태 10 Hz, 계산 순서 1~3단계), §6.1·§6.6(지연 예산, capture_to_risk p95 600 ms), 요구사항 FR-61(World XYZ·3D 골격 10 Hz), NFR-17(교실 최대 10명) 기준이다.
코드는 `ai/src/simai_ai/geometry/` 의 `rig.py`(묶음 왜곡 제거), `triangulation.py`(관절·사람 묶음 삼각측량), `joint_gate.py`(공분산 묶음, 결과 다시 쓰기), `ai/src/simai_ai/tracking/cross_view.py`(쌍 묶음 근거, 배정 갱신)에 있다.
시험은 `ai/tests/geometry/test_batch_undistort_regression.py`(이전 구현과의 수치 비교), 측정 스크립트는 `ai/tests/geometry/bench_tier0_geometry.py` 다.

## 문제

카메라 4대 × 10명 × 17관절에서 이전 구현(dev 95ba73d)은 관절·카메라마다 1점씩 왜곡을 제거하고(`RigCamera.normalize` 호출 한 번에 수백 µs의 고정 비용), 관절마다 Python 반복으로 가우스-뉴턴을 풀었다. Cross-view 는 view 쌍마다, 관절 게이트는 관절마다 같은 계산을 따로 했다. 노트북 CPU 에서 World Track 갱신 한 번의 기하 시간이 10명 기준 약 1.1 s 로 10 Hz(100 ms)를 크게 넘었다(아래 표 "이전").

## 바꾼 구조

계산 식과 판정 규칙은 그대로 두고 같은 식을 묶어서 계산한다. 공개 함수 시그니처는 그대로이고(새 인자는 모두 키워드 전용·기본값 있음), 새 묶음 API 를 더해 기존 함수가 그것을 쓰게 했다.

| 단계 | 이전 | 지금 |
|---|---|---|
| 왜곡 제거 | 관절·카메라마다 1점씩, 삼각측량·재투영·게이트·Cross-view 가 각자 | 카메라마다 그 시각의 view 전체를 한 번에(`RigCamera.undistort_many`). 결과 `UndistortedKeypoints`(정규화 좌표, 성공 마스크, 신뢰도, 이상적 핀홀 픽셀)를 다음 단계가 다시 쓴다 |
| 삼각측량 | 관절마다 DLT·가우스-뉴턴(카메라 Python 반복) | 사람 여러 명의 관절 전체를 한 번에 1차 풀이(`triangulate_skeletons`, 관절마다 쓴 카메라는 마스크, 감쇠·멈춤은 관절마다). 이상치·퇴화 관절만 기존 관절별 순차 경로(이상치 카메라를 하나씩 빼고 다시 풀기)로 넘긴다 |
| Cross-view 후보 근거 | view 쌍마다 에피폴라 거리·2-view DLT | 카메라 쌍마다 view 전체의 에피폴라 거리 행렬, 문턱을 넘은 쌍의 공통 관절을 이어 붙여 2-view DLT 한 번 |
| Cross-view 배정 | 병합할 때마다 모든 클러스터 쌍의 비용을 다시 모음 | 비용 행렬을 두고 합친 클러스터의 행·열만 다시 계산 |
| Cross-view 클러스터 검증 | 클러스터마다 `triangulate_skeleton` | 처음 클러스터 전체를 `triangulate_skeletons` 한 번, view 를 떼어 낸 클러스터만 다시 |
| 관절 게이트 | 관절마다 공분산·고윳값, 경쟁 후보마다 1점씩 왜곡 제거·삼각측량 | 공분산·고윳값 묶음(`triangulation_covariances`), 경쟁 후보는 카메라·관절마다 한 번에 왜곡 제거·삼각측량 |

다시 쓰기(재계산을 건너뛰기)는 입력이 같다고 확인될 때만 한다. 확인할 수 없으면 다시 계산하므로 결과는 늘 새로 계산한 것과 같다.

- `UndistortedKeypoints.matches(camera, keypoints)`: 같은 카메라 모델(보정 값 전체)로 같은 keypoint 를 푼 결과일 때만 쓴다. camera_id 가 같아도 다른 보정이면 쓰지 않는다.
- `SkeletonEstimate.reusable_for(views, rig, joint_layout, config)`: view(순서·카메라·keypoint·시각), 보정(버전·상태·카메라 모델), 관절 배치, `TriangulationConfig` 가 모두 같을 때만 참이다. 동기화 무효 카메라를 빼서 view 가 줄면 다시 계산한다.
- World Track Manager(S15P31A101-37)는 클러스터마다 `JointGate.update(views, rig, others, undistorted=c.undistorted, others_undistorted=..., skeleton=c.skeleton)` 로 넘기면 왜곡 제거·삼각측량을 다시 하지 않는다. `others_undistorted` 는 `others` 와 같은 카메라·같은 순서로 다른 클러스터의 `ViewCluster.undistorted` 를 모은 것이다.
- Cross-view 클러스터 삼각측량에 넘기는 view 순서를 병합 순서에서 키 순서(`ViewCluster.views` 순서)로 바꿨다. 관절 게이트가 같은 순서의 view 로 결과를 다시 쓰게 하려는 것이고, 관절의 `camera_ids`·`rejected_camera_ids` 나열 순서만 바뀐다(값·판정은 같다).

Tier 0 규칙은 그대로다. 위치·관절의 정본은 이 삼각측량이고(`joint_source = observed`), 2대 미만 관절은 비우고, 왜곡을 제거할 수 없는 keypoint(NaN·실패 마스크, S15P31A101-166)는 관측으로 세지 않는다.

## 결과가 같다는 확인

`test_batch_undistort_regression.py` 는 이전 구현의 고정 사본(`tests/geometry/legacy_geometry.py`, dev 95ba73d 함수 본문 그대로)과 지금 구현에 같은 무작위 장면을 넣어 비교한다.

- 렌즈: 광각 핀홀(k1 −0.30, k2 0.09), 왜곡 없음, 접히는 렌즈(k1 −0.30 만, 영상 모서리 왜곡 제거 실패), 접선 계수, 유리 모델 8계수, 어안, 보정 상태 DRIFTED.
- 장면: 깨끗한 장면과 거친 장면(이상치 keypoint 30~80 px, NaN 값, 영상 모서리 keypoint, 카메라 통째 가림으로 2대 관측), 사람이 2.4 × 2.4 m 에 몰린 장면(애매한 배정·잔차 경계·2대 경쟁 후보).
- 비교 대상: `triangulate_skeleton`·`triangulate_skeletons`·`triangulate_point`·`reprojection_errors`·`epipolar_distance_px`, `JointGate.update`(8 tick, 속도·뼈 길이 이력 포함), `CrossViewAssociator.associate`(4~6 tick, 쌍 근거·클러스터·association_id·사유·연결 이력).
- 허용 오차: 범주형(관절 상태·사유·쓴/뺀 카메라·클러스터 구성·association_id·IDENTITY_UNCERTAIN 사유)은 완전히 같아야 한다. 수치는 정규화 좌표 1e-12, XYZ 1e-6 m, 재투영 오차·에피폴라 거리 1e-5 px, 시선각 1e-6 rad, 삼각측량 표준편차 1e-9 m, 속도 1e-6 m/s, 쌍 비용 1e-6. 묶음 연산은 같은 식을 행렬로 계산해 마지막 자리가 다를 수 있고, 가우스-뉴턴은 걸음이 1e-9 m 미만이면 멈추므로 멈추는 반복이 하나 달라지면 해가 1e-9 m 수준으로 다를 수 있어서다. 실측 차이는 1e-8 m 이하였다(리뷰에서 이전 커밋 코드와 직접 비교한 최대 2.7e-8 m).
- 기존 시험(삼각측량·관절 게이트·Cross-view·SIM 정확도)은 고치지 않고 모두 통과한다.

## 측정 (노트북 CPU)

`uv run python tests/geometry/bench_tier0_geometry.py --ticks 30` (ai/). 이전 구현은 dev 95ba73d 의 `ai/src` 를 꺼내 PYTHONPATH 앞에 두고 같은 스크립트로 쟀다(스크립트가 묶음 API 가 없으면 이전 방식으로 부른다).

```sh
git archive 95ba73d ai/src | tar -x -C /tmp/base
PYTHONPATH=/tmp/base/ai/src uv run python tests/geometry/bench_tier0_geometry.py --ticks 30
```

장면: 교실 모서리 높이 2.7 m 카메라 4대(2304 × 1296, 수평 화각 105°, 광각 배럴 왜곡), 사람은 무작위 위치·방향에서 tick(10 Hz)마다 걷고 픽셀 잡음 σ 1 px, 신뢰도 기준 미만 10%, 검출 없음 3%. 30 tick(앞 2 tick 은 데우기로 뺌). SIM 합성 입력의 처리 시간이며 정확도 지표가 아니다.

환경: Intel Core Ultra(Family 6 Model 170), 22 논리 코어, Windows 11, Python 3.11.15, numpy 2.4.6. 단일 스레드 numpy 연산이다. 같은 노트북에서 다른 작업(병렬 시험)이 함께 돌아 같은 측정을 반복하면 ±20% 정도 흔들렸다.

단계 정의(각 단계는 따로 잰다):

| 단계 | 내용 |
|---|---|
| undistort | view 4N 개 전체 왜곡 제거(지금: 카메라마다 한 번, 이전: view 마다 한 번) |
| triangulate | 정답 연결로 사람마다 `triangulate_skeleton`(자체 왜곡 제거 포함) |
| triangulate_batch | 같은 입력을 `triangulate_skeletons` 한 번으로(이전: triangulate 와 같다) |
| joint_gate | 정답 연결로 사람마다 `JointGate.update`(자체 왜곡 제거·삼각측량, 다른 사람 검출 others) |
| cross_view | `CrossViewAssociator.associate`(왜곡 제거·후보 근거·배정·클러스터 삼각측량) |
| pipeline | World Track 갱신 한 번의 기하: cross_view + 클러스터마다 `JointGate.update`(지금: 클러스터 결과 다시 쓰기) |

결과(ms, 평균 / p95):

| 사람 | view | 단계 | 이전 | 지금 |
|---:|---:|---|---:|---:|
| 1 | 4 | undistort | 1.4 / 2.2 | 1.6 / 2.0 |
| 1 | 4 | triangulate | 43.7 / 48.1 | 3.6 / 4.5 |
| 1 | 4 | joint_gate | 45.2 / 49.5 | 4.5 / 6.1 |
| 1 | 4 | cross_view | 47.1 / 53.1 | 6.7 / 7.6 |
| 1 | 4 | **pipeline** | 91.5 / 100.3 | **7.3 / 8.3** |
| 5 | 20 | undistort | 6.3 / 6.8 | 1.9 / 2.2 |
| 5 | 20 | triangulate | 221.1 / 251.7 | 17.1 / 19.9 |
| 5 | 20 | triangulate_batch | 224.4 / 254.0 | 7.2 / 9.2 |
| 5 | 20 | joint_gate | 257.3 / 287.6 | 26.1 / 30.3 |
| 5 | 20 | cross_view | 248.6 / 289.2 | 15.8 / 18.7 |
| 5 | 20 | **pipeline** | 494.4 / 529.9 | **20.2 / 23.2** |
| 10 | 40 | undistort | 14.1 / 17.9 | 2.5 / 3.4 |
| 10 | 40 | triangulate | 473.1 / 586.7 | 33.9 / 38.0 |
| 10 | 40 | triangulate_batch | 461.0 / 564.4 | 11.2 / 13.0 |
| 10 | 40 | joint_gate | 569.7 / 670.3 | 53.9 / 63.6 |
| 10 | 40 | cross_view | 539.7 / 623.2 | 31.3 / 35.6 |
| 10 | 40 | **pipeline** | 1107.2 / 1358.7 | **41.7 / 47.3** |

- 10명 기준 기하 전체(pipeline)가 약 27배 빨라져 p95 47 ms 다. 남은 시간의 대부분은 Cross-view(후보 근거 약 10 ms, 배정 약 6 ms, 클러스터 삼각측량 약 5 ms)와 관절 게이트의 관절별 Python 판정이다.
- 사람마다 따로 부르는 경로(triangulate, joint_gate 행)는 사람 수에 비례해 늘고, 클러스터 결과를 다시 쓰지 않는 관절 게이트는 10명에서 p95 64 ms 라 단독으로도 예산을 넘는다. World Track Manager 는 클러스터 결과를 넘겨야 한다(위 "다시 쓰기").

## 10 Hz 기하 단계 목표

| 항목 | 목표 | 판정 위치 | 지금(노트북 CPU) |
|---|---|---|---|
| 기하 전체(pipeline: Cross-view + 클러스터별 관절 게이트), 카메라 4대 × 10명 | tick 당 p95 ≤ 50 ms | 운영 Pod(일본 AP-JP-1 H100 노드) CPU | p95 47.3 ms (충족, 여유 작음) |
| 같은 조건, 5명 | p95 ≤ 25 ms | 같음 | p95 23.2 ms |

- 근거: World Track 갱신 주기 100 ms(§5.2 10 Hz) 안에 Local Tracker, ContactEstimator, World Track Manager(칼만 필터·ID 관리), Risk Engine, world_state·health 송신이 함께 들어가야 한다. 기하 단계에 절반(50 ms)을 주고 나머지 절반을 다른 CPU 단계와 흔들림 여유로 둔다. tick 처리가 주기를 넘으면 대기열이 쌓여 capture_to_risk(p95 600 ms, §6.6)와 T_lag 가 늘어난다.
- 판정은 §6.6 대로 운영 환경 측정으로 한다. 노트북 측정은 병목 분석용이다. Pod CPU 가 노트북보다 단일 스레드가 느리면 목표를 넘을 수 있으므로 아래 측정으로 확인한다.
- 사람 수에 거의 비례하므로(1명 7 ms → 10명 42 ms 평균) 10명을 넘는 장면은 목표 밖이다(NFR-17 최대 10명).

## Pod(H100 노드) CPU 측정 계획

GPU 최소 사용 원칙에 따라 이번에는 재지 않았다. 다음 GPU 검증 큐에 넣는다(GPU 는 쓰지 않고 Pod CPU 만 쓴다. LIVE 관제 시간에는 돌리지 않는다).

```sh
cd <Pod 의 저장소 체크아웃>/ai
uv sync
uv run python tests/geometry/bench_tier0_geometry.py --people 1 5 10 --ticks 100     --json /workspace/simai/runs/tier0-geometry-budget/<날짜>.json
```

- GPU 큐(`ai/experiments/gpu_queue`)에 Job 하나로 넣을 수 있다. GPU 메모리를 쓰지 않으므로 다른 Job 앞뒤 아무 곳에 둬도 된다.

- 판정: 10명 pipeline p95 ≤ 50 ms, 5명 ≤ 25 ms. 넘으면 남은 병목(Cross-view 후보 근거·배정, 관절 게이트의 관절별 Python 판정)을 묶음 연산으로 더 옮기거나 클러스터·사람 단위 병렬화를 검토한다.
- Tier 0 가 GPU 추론과 같은 프로세스에서 돌 때의 측정(검출 후처리·Local Tracker 와 CPU 를 나눠 쓸 때)은 Tier 0 조립(S15P31A101-37 이후)에서 다시 한다.

## S15P31A101-237: 호스트 CPU 여유를 위한 2차 묶음

GPU 검증(c5a)에서 같은 코드의 10명 pipeline p95 가 1차 세션 34.75 ms(통과), 2차 세션(Xeon 8480C 호스트) 57.38 ms(실패)였다. 단계가 모두 약 2배 느려 호스트 CPU 영향이다. GPU 사이트를 고정하지 않으므로 코드 쪽 여유를 늘린다. 판정 식·문턱·null 규칙은 그대로 두고 Python 반복과 numpy 호출 수만 줄였다. 구조 변경(멀티프로세스, GPU 이전)은 하지 않았다.

### 프로파일 (dev abe9529, 노트북, 10명, tick 당 ms, 함수 래퍼 타이머라 실제보다 조금 크다)

| 순위 | 위치 | 시간 | 원인 |
|---:|---|---:|---|
| 1 | Cross-view 후보 근거 `_pairs` | 11.0 | 2-view DLT 배치 SVD 4.8(view 쌍 약 85 × 공통 관절), 쌍마다 `_evidence`(np.median·바닥 가설) 3.4, 쌍 근거 객체·정렬 |
| 2 | 관절 게이트 `JointGate.update`(클러스터 10개) | 9.7 | 2대 관측 경쟁 후보 검사 4.9(후보마다 가우스-뉴턴 10회), 공분산 2.9 |
| 3 | 클러스터 삼각측량 `_validate` | 7.4 | `_first_pass` 5.8: (사람, 관절) 행마다 Python 으로 배열 채우기, 가우스-뉴턴 10회 |
| 4 | 배정 `_assign` | 4.0 | 병합마다 모든 후보 쌍을 Python 으로 정렬·막은 병합 frozenset 확인, `_linkage` tick 당 약 1500회, `np.delete` |
| 5 | 왜곡 제거 `undistort_many` | 1.9 | 카메라 4대 × 뉴턴 반복(카메라마다 계수가 달라 더 묶지 않는다) |

### 바꾼 것

| 단계 | 이전 | 지금 | 같은 결과인 이유 |
|---|---|---|---|
| Cross-view 후보 근거 | 쌍마다 `_evidence` | 카메라 쌍마다 `_evidence_many`: 일관된 관절 수·잔차 중앙값·높이 범위 밖 수·바닥 가설 거리를 (쌍, 관절) 행렬로 한 번에, 판정은 공통 `_verdict`. 2-view DLT 입력은 카메라별 쌓은 배열(`_ViewStack`)에서 한 번에 골라 낸다. 쌍 근거는 키 순서로 바로 내 정렬하지 않는다 | 중앙값은 np.median 과 같은 식(홀수 가운데, 짝수 가운데 둘 평균). 바닥 가설 거리는 같은 식의 원소별 계산(마지막 자리만 다를 수 있다) |
| 배정 | 병합마다 후보 목록 정렬, 막은 병합 집합 | 비용 최소를 numpy 로 찾고 같은 비용일 때만 (가장 작은 키, i, j)로 고른다. 막은 병합·합칠 수 없는 쌍(veto: 같은 카메라, 기하가 반대하는 view 쌍)·카메라 소속을 행렬로 두고, 합친 클러스터의 veto 는 두 행의 OR. veto 인 쌍과 view 하나씩인 클러스터의 탈락 쌍은 `_linkage` 를 부르지 않는다. 행을 지우지 않고 alive 로 뺀다 | `_linkage` 는 veto 쌍에서 늘 inf 이고 클러스터는 커지기만 하므로 합친 클러스터의 키 집합은 처음 생긴 것(막은 병합이 없다) |
| 클러스터 삼각측량 | (사람, 관절) 행마다 배열 채우기 | 사람마다 (관절, 카메라) 배열을 한 번에 채운다. 가우스-뉴턴은 멈추지 않은 점만 계산한다 | 점마다 식이 따로라 비트 단위로 같다(시험으로 확인) |
| 경쟁 후보 검사 | 후보마다 `triangulate_normalized_many`(가우스-뉴턴 10회)의 유한함 | `solvable_normalized_many`: DLT 해와 카메라 앞 확인만 | 가우스-뉴턴은 쓴 카메라 모두의 앞이고 비용을 줄이는 유한한 걸음만 받아들이므로 해 유무가 정밀화 전과 같다 |
| 공분산 | 관절마다 mask 채우기 | 한 번에 채우기, 카메라 목록은 경쟁 후보 검사가 필요할 때만 | 같은 입력 |

### 결과가 같다는 확인

`ai/tests/geometry/test_geometry_speedup_regression.py`. 기준은 바꾸기 직전 모듈의 고정 사본 `legacy_batch_{triangulation,joint_gate,cross_view}.py`(dev abe9529, import 경로만 바꿈. rig 는 바꾸지 않아 지금 모듈을 쓴다)다. 같은 보정 문서로 rig 를 두 개 만들어 결과 객체 전체를 필드마다 비교한다.

- 장면: 렌즈 5종(광각, 왜곡 없음, 접히는 렌즈, 어안, DRIFTED) × 1·5·10명 × 깨끗한/거친 입력(이상치, NaN, 왜곡 제거 실패, 카메라 통째 가림으로 2대·1대 관측), 몰린 장면(애매한 배정·잔차 경계·2대 경쟁 후보). World Track 갱신의 기하(연결 → 클러스터별 게이트, 연결 이력·head_z 포함)와 사람별 경로(others 를 모르는 tick 포함).
- 허용 오차: 범주형(상태·사유·쓴/뺀 카메라·클러스터·association_id·탈락 사유·null 여부·joint_source)은 같아야 하고, 실수는 절대 오차 1e-9(m, px, rad, 비용). 삼각측량 좌표는 비트 단위로 같았다.
- 반례: 같은 비용 동률·문턱 경계 비용(3.0, 애매함 0.5)·막은 병합·veto·근거 없는 쌍·연결 이력을 섞은 합성 배정 60개(같은 카메라 금지를 끈 모드 포함), 쌍 묶음 근거 = 쌍 하나씩(np.median, 짝수·홀수 개), 해 유무 = 정밀화한 삼각측량의 유한함(카메라 뒤·기선 위 시선·NaN), 가우스-뉴턴 비트 동일(카메라 뒤 시작점 포함).
- 리뷰 보강(정해진 입력): 두 카메라 기선에서 ε(1 ~ 1e-13, 0) 떨어진 점의 시선(DLT 해 유일성 문턱 양쪽, 정확히 기선 위), 기선 연장선(카메라 뒤), 무한·NaN·같은 좌표 입력에서 해 유무·정밀화 삼각측량·2-view 연결 삼각측량이 비트 단위로 같다. 기선 근처·아주 먼 관절(공분산 조건수 1e12 문턱 너머)의 표준편차가 같다. 1 ULP 차이로 근접한 배정 비용(동률 옆, 3.0 바로 아래, 애매함 0.5 안·밖)에서 배정이 같다. 바닥 가설 거리 묶음 계산은 1e-12 ~ 1e12 규모 4000건에서 쌍 하나씩 계산과 상대 1e-12 안에서 같다(비트 단위로는 CPU·numpy 빌드의 연산 순서에 따라 다를 수 있다: EC2 x86 에서 최대 상대 5.6e-15, S15P31A101-257).
- 음성 대조: 경쟁 후보 검사를 끄면, 바닥 가설 거리를 1e-6 배 바꾸면, 가우스-뉴턴을 2회에서 멈추면, 동률을 뒤에서부터 고르면 위 비교가 실패한다.
- 기존 시험(`test_batch_undistort_regression.py` 의 S15P31A101-168 이전 구현 비교 포함)은 고치지 않고 통과한다.

### 측정 (노트북, 10명)

같은 노트북에서 다른 작업(다른 Owner 의 시험)이 함께 돌아 순차 측정(`bench_tier0_geometry.py` 를 이전 `ai/src` 와 번갈아 3회)은 10명 이전 평균이 45~68 ms 로 크게 흔들렸다. 그래서 같은 프로세스에서 tick 마다 이전 사본과 지금 구현을 번갈아 재는 A/B(같은 입력, 60 tick)를 3회 했다. 처음 3회는 tick 마다 이전 → 지금 순서였고, 두 번째 실행이 캐시·CPU 상태에서 유리할 수 있어 짝수 tick 은 이전 먼저, 홀수 tick 은 지금 먼저로 순서를 교대해 3회 더 쟀다(아래 "교대").

| 사람 | 이전 평균 / p95 (ms) | 지금 평균 / p95 (ms) | 비율 평균 / p95 |
|---:|---:|---:|---:|
| 10 | 55.0 / 77.7 | 36.5 / 53.3 | 0.66 / 0.69 |
| 10 | 56.3 / 113.2 | 35.7 / 66.0 | 0.64 / 0.58 |
| 10 | 51.4 / 86.5 | 32.1 / 48.8 | 0.63 / 0.56 |
| 5 | 16.7 / 21.2 ~ 17.7 / 21.8 | 12.8 / 14.7 ~ 13.5 / 16.6 | 0.75~0.77 / 0.69~0.76 |
| 1 | 5.4~5.5 / 6.2~6.5 | 5.5~5.6 / 6.4~6.7 | 1.0 / 1.0 |
| 10 (교대) | 105.1 / 146.4 | 68.9 / 92.3 | 0.66 / 0.63 |
| 10 (교대) | 50.1 / 72.3 | 33.9 / 47.4 | 0.68 / 0.66 |
| 10 (교대) | 72.8 / 154.8 | 47.3 / 99.8 | 0.65 / 0.65 |
| 5 (교대) | 18.1 / 25.5 ~ 20.3 / 30.7 | 14.1 / 19.7 ~ 15.9 / 23.1 | 0.74~0.78 / 0.75~0.81 |

교대 측정은 부하가 더 커서 절대값이 크게 흔들렸지만 비율은 순서를 고정했을 때와 같은 범위다(10명 평균 0.65~0.68, p95 0.63~0.66).

부하가 적던 작업 초반의 단독 측정은 이전 10명 pipeline 평균 33.4 / p95 38.2 ms, 지금 약 21~23 / 25~28 ms 였다.

- 목표(10명 p95 절반 이하)에는 못 미쳤다. 10명 기준 약 35~40% 줄었다(사람 수가 적으면 줄어드는 몫이 작다. 1명은 그대로).
- 남은 시간(지금, 10명 tick 당 약 21 ms): 2-view DLT 배치 SVD 약 3.4 ms(LAPACK 4×4 SVD 약 1100개), 클러스터 가우스-뉴턴 약 2 ms·DLT SVD 약 1 ms, 관절 게이트 약 6 ms(클러스터마다 공분산·경쟁 후보의 numpy 호출 고정 비용), 왜곡 제거 약 2 ms, 배정 약 2 ms. 대부분 numpy 호출 한 번의 고정 비용(수 µs)이 클러스터·카메라 쌍 수만큼 쌓인 것이라 같은 계산 식을 유지하는 한 Python 단에서 더 줄일 몫이 작다.

### 버린 대안

- 2-view DLT 의 SVD 를 AᵀA 고윳값 분해(eigh)로 바꾸기: 약 2 ms 빠르지만 해 유일성 판정(두 번째 특이값 ≤ 1e-9 × 최대)을 고윳값으로는 같은 정밀도로 할 수 없다. 연결 판정의 수치 방법이 바뀌므로 하지 않았다.
- 클러스터 전체의 관절 게이트를 한 번에(공분산·경쟁 후보 묶음): 게이트 약 6 ms 중 대부분을 줄일 수 있지만 World Track Manager 가 Track 마다 `JointGate.update` 를 부르는 구조(`world_track.py`)를 바꿔야 한다. 범위 밖이라 제안으로 남긴다.
- 멀티프로세스(카메라 쌍·클러스터 병렬), GPU 이전: 구조 변경이라 하지 않았다(제안). 호스트 CPU 가 느린 사이트에서도 50 ms 를 지키려면 이 둘 중 하나가 필요할 수 있다.
- 왜곡 제거를 카메라 4대 한 번에: 카메라마다 계수가 달라 같은 호출로 묶을 수 없다.

### 남은 일

- Pod CPU 측정을 다음 GPU 검증 큐에 다시 넣는다(위 "Pod(H100 노드) CPU 측정 계획"과 같은 명령). 2차 세션 같은 호스트에서 10명 p95 가 50 ms 안인지 본다.
- 넘으면 클러스터 묶음 게이트(World Track 호출부 변경) 또는 병렬화를 별도 티켓으로 한다.

## S15P31A101-245: 유한하지 않은 입력의 LAPACK 멈춤

관련 명세: 5.2(삼각측량·관절 유효성 게이트·Cross-view 연결). 결과 정의(해 없음 = NaN·ok False, 2대 미만 null, joint_source)는 바꾸지 않았다.

### 문제

-235 사전 판정(Linux CPU 컨테이너)에서 `test_degenerate_rays_match_previous` 가 멈췄다(`docs/design/unified-env.md` "잔여 위험"). Linux 휠 numpy 의 OpenBLAS(numpy 2.3.5·OpenBLAS 0.3.30, numpy 2.4.6·0.3.31 둘 다)에서 LAPACK SVD 가 ±inf 가 든 행렬에서 끝나지 않는다. Windows 휠에서는 같은 입력이 끝난다.
노트북 Docker(numpy 2.3.5)에서 호출 종류별로 다시 쟀다(호출마다 새 프로세스, 10~20초 제한, 무작위 행렬 5개에 값 하나):

| 호출 | ±inf | NaN |
|---|---|---|
| `svd`(3×3·4×4·8×4) | 5개 중 1~2개 멈춤, `np.ones((3,4,4))`+inf 는 멈춤 | `LinAlgError` |
| `lstsq` | 멈춤 | 멈춤 |
| `cond`(SVD) | inf 반환 | `LinAlgError` |
| `eigvalsh` | `LinAlgError` | `LinAlgError` |
| `inv`·`solve`·`det`(LU), `norm(·, 2)` 2×2 | 끝남 | 끝남(`norm` 2 는 `LinAlgError`) |

멈춤은 값 배치에 따라 달라 미리 알 수 없다. 그래서 SVD 계열 호출 앞에서 유한하지 않은 행렬을 거르는 것으로 정했다.

### Tier 0 경로의 LAPACK 호출 전수 조사

Tier 0 서비스(`services.tier0` 전체)가 import 하는 `simai_ai` 모듈(calibration 의 criteria·document·drift·monitor·state·structure·versions, contact, geometry, tracking, risk, recovery, io, config, eventlog, gpu, providers)과 Tier 0 에 주입할 수 있는 보정 감시(`calibration/watch.py` → `recalibration.realign_rotation`, `drift_watch_factory`)에서 LAPACK 계열(`np.linalg` 의 svd·eig·eigh·eigvals·eigvalsh·lstsq·pinv·matrix_rank·qr·cholesky·det·slogdet·inv·solve·cond·행렬 노름, `Polynomial.roots`·`np.roots`(동반 행렬 `eigvals`), `polyfit`(`lstsq`))과 `scipy.linalg` 를 모두 찾았다. scipy 는 이 경로에 없다.
벡터 노름 `np.linalg.norm(x, axis=…)` 은 LAPACK 을 부르지 않는다(제곱합의 제곱근). 줄 번호는 이 변경 뒤 기준이다.

| 위치 | 호출 | 판정 루프에서 실행 | 입력 출처 | 유한하지 않은 값이 올 수 있나 | 이전 | 지금 |
|---|---|---|---|---|---|---|
| `geometry/triangulation.py:663`·`:677` `_dlt_batch`·`_dlt_one_by_one` | `svd` | 매 tick(사람 묶음 1차 풀이·관절별 경로·보정 감시) | `triangulate_normalized_many`·`solvable_normalized_many`(공개 함수)와 사람 묶음 1차 풀이의 정규화 좌표·신뢰도 | 공개 함수 입력에 inf·NaN 이 오면 그대로. 사람 경로는 `usable`(왜곡 제거 성공·유한 신뢰도)로 거르지만 아주 큰 유한 신뢰도(예 1e308)는 계수 행렬을 inf 로 넘치게 한다 | 막지 않음(유한 확인이 SVD 뒤). inf 는 멈춤, NaN 은 묶음 `LinAlgError` 뒤 점별 SVD 에서 inf 행이 다시 멈춤 | 계수가 유한하지 않은 점은 0 행렬로 바꿔 풀고 해 없음 |
| `geometry/triangulation.py:761`·`:767` `_solve_3x3` | `solve` | 매 tick | 가우스-뉴턴 정규 방정식(유한한 DLT 해에서 시작) | 넘침으로 생길 수 있음 | LU 라 멈추지 않음 | 그대로 |
| `geometry/joint_gate.py:462` `triangulation_covariances` | `cond`(SVD) | 매 tick(관절 게이트) | 관절 XYZ·카메라 행렬로 만든 정보 행렬 | 카메라 중심에 아주 가까운 점(카메라 z ≲ 1e-150)이면 야코비안이 넘쳐 inf·NaN | 막지 않음. NaN 이면 `LinAlgError` 로 관절 게이트 전체가 예외 | 정보 행렬이 유한하지 않은 점은 공분산을 구하지 못한 점(ok False, 표준편차 inf) |
| `geometry/joint_gate.py:467`·`:495` | `inv`(LU)·`eigvalsh` | 매 tick(관절 게이트) | 조건수 1e12 이하로 통과한 정보 행렬·그 역행렬(공분산) | 정보 행렬이 유한하고 조건수가 작아도 아주 작으면(초점거리 1e-154 등) 역행렬 × σ² 가 넘쳐 inf·NaN | 막지 않음. Windows 는 `eigvalsh` `LinAlgError`, 아니면 NaN 표준편차로 `MAX_SIGMA_EXCEEDED` 비교가 거짓이 될 수 있음 | 공분산이 유한하지 않은 점도 구하지 못한 점(cov NaN, ok False) |
| `geometry/joint_gate.py:530`, `geometry/rig.py:67` | `inv(K)` | joint_gate 는 매 tick(경쟁 후보), rig 는 기동·보정 교체 때 | 보정 K | 아래 보정 검사 전에는 가능 | LU 라 멈추지 않음 | 보정 검사로 막음 |
| `geometry/rig.py:71` `RigCamera` | `norm(K[:2,:2], 2)`(SVD) | 기동·보정 교체 때 | 보정 문서 K. `FileCalibrationProvider` 는 `json.loads` 기본값이라 `NaN`·`Infinity` 를 받는다(구성 묶음 경로 `config/verify.py` 는 거부) | 파일 보정 경로에서 가능. `PinholeCamera` 검사(fx·fy > 0, R 직교)는 NaN 을 그냥 통과시킨다 | 막지 않음 | `PinholeCamera` 가 K·R·t 의 유한하지 않은 값을 `ValueError` 로 거절 |
| `tracking/cross_view.py:1001` `_two_view_arrays` | `svd` | 매 tick(Cross-view 연결) | 카메라 쌍 공통 관절(관측 마스크 통과)의 정규화 좌표·신뢰도 | 마스크가 inf·NaN 을 거르지만 아주 큰 유한 신뢰도는 넘침 | 막지 않음. inf 는 멈춤, NaN 은 `LinAlgError` 로 연결 전체가 예외 | 계수가 유한하지 않은 관절은 XYZ NaN·재투영 오차 inf(해 없음) |
| `geometry/distortion.py:126` `radial_branch`(`RigCamera` 생성·`zone_coverage`) | `Polynomial.roots`(동반 행렬 `eigvals`, `dgeev`) | 기동·보정 교체 때, 구역 관측 범위 계산 때 | 왜곡 계수로 만든 가지 다항식 | 계수에 NaN·inf(`check_model` 은 개수만 봤다), 또는 아주 큰 유한 계수(k4 1e200)의 곱이 넘침 | 막지 않음 | `check_model` 이 유한하지 않은 계수를 `ValueError`, 다항식 계수가 넘치면 `radial_branch` 가 `ValueError`(가지를 모르므로 거절). `undistort_tolerance_px` 도 유한한 양수만 |
| `calibration/recalibration.py:328`·`:329` `_kabsch`(보정 감시가 DRIFTED 에서 부름) | `svd`·`det` | 선택형: `drift_watch_factory` 로 보정 감시를 주입했을 때만, DRIFTED 에서 | 기준 카메라 쌍 삼각측량 점에서 대상 카메라 중심으로의 단위 방향 | 점이 대상 카메라 중심과 같으면 0/0 = NaN. 유한한 R·t 라도 중심 −R⁻¹·t 가 넘치면(t ≈ 1.7e308, 계약·기하 검사 통과) inf/inf = NaN | 막지 않음(`LinAlgError`) | 거리가 유한한 양수인 대응만 쓴다(Kabsch 에는 유한한 단위 벡터만). 중심이 넘치는 보정은 `PinholeCamera` 가 `ValueError` 로 거절 |
| `tracking/world_track.py:480` | `inv(S)`(2×2 칼만 혁신 공분산) | 매 tick | 트랙 상태 공분산 + 측정 잡음 | 측정값이 아니라 공분산에서 온다 | LU 라 멈추지 않음 | 그대로 |

"판정 루프에서 실행" 은 import 여부와 다르다. 기본 서비스 import 에는 scipy 가 없고, 선택형 보정 감시(`calibration.watch`)를 import 하면 번들 조정·내부 파라미터 도구와 scipy 가 함께 import 되지만 그 함수들은 LIVE 주기에서 부르지 않는다(재보정 실행은 호출자 몫).

Tier 0 판정 루프에서 부르지 않는 보정·장면 도구(설치 때 노트북에서, 또는 재보정 작업으로 실행): `calibration/bundle`(`solver.py` `inv`·`solve`, scipy 희소 행렬·`Rotation`, `checks.py` `lstsq`·`eigvalsh`), `calibration/intrinsics`(`eigh`·`solve`·`inv`·`svd`), `calibration/extrinsics/initial.py`(`svd`·`det`), `calibration/world_frame`(`svd`·`solve`), `calibration/recalibration.py` 의 `run_recalibration`(번들 조정, 호출자가 LIVE 주기 밖에서 실행), `calibration/coverage.py`(`radial_branch`, 위 고침이 함께 막는다), `calibration/intrinsics/lines.py`(`polyfit`), `scene/bev.py`(`inv(K)`). 판정 루프 밖이라 이 티켓에서 고치지 않았다(아래 "남은 위험").

### 바꾼 것

- `_dlt_batch`: 계수 행렬이 유한하지 않은 점은 0 행렬로 바꿔 SVD 하고 `ok & finite` 를 돌려준다. 묶음 SVD 는 행렬마다 따로 푸는 gufunc 라 유한한 점의 결과는 비트 단위로 같다. `_solve_batch`·`solvable_normalized_many` 둘 다 이 함수를 지난다.
- `_two_view_arrays`: 같은 방식으로 거르고 `ok` 에 `finite` 를 더한다.
- `triangulation_covariances`: `ok = front & isfinite(info)`, 역행렬 뒤에도 공분산이 유한하지 않은 점은 `ok` False·cov NaN.
- `distortion.check_model`: 계수가 유한하지 않으면 `ValueError`. `radial_branch`: 가지 다항식 계수가 넘치면 근을 구하지 않고 `ValueError`. `RigCamera`: `undistort_tolerance_px` 는 유한한 양수.
- `recalibration.realign_rotation`: 대상 카메라 중심까지 거리가 유한한 양수인 대응만 쓴다(Kabsch SVD 에 NaN 을 넣지 않는다).
- `PinholeCamera`: 유한한 R·t 에서 계산한 중심 −R⁻¹·t 가 넘치면 `ValueError`(중심은 시선각·재보정 방향에 쓰인다).
- `PinholeCamera.__post_init__`: K·R·t 에 유한하지 않은 값이 있으면 `ValueError`(기존 보정 검사와 같은 예외).
- 비용: 거르기(`isfinite`+`where`)는 (170, 8, 4) 7 µs, (1100, 4, 4) 29 µs 로 같은 묶음의 SVD(564 µs, 2.7 ms)의 1~1.3% 다(노트북).

### 결과가 같다는 확인과 시험

- `test_geometry_speedup_regression.py::test_degenerate_rays_match_previous`: 이전 구현(`legacy_batch_triangulation.py`, 고정 사본이라 고치지 않음)도 inf 행을 SVD 에 넣어 Linux 에서 멈추므로 이전 구현에는 유한한 행만 넣고 비트 단위로 비교한다. 무한·NaN 행은 이전 구현의 정의(해 없음)대로 NaN 인지 본다.
  Windows 에서는 따로 이전 구현에 전체 행을 넣은 결과가 유한한 행만 넣은 결과·새 구현 전체 행 결과와 비트 단위로 같음을 확인했다(커밋하지 않은 일회 점검).
- `tests/geometry/test_nonfinite_lapack.py`(새 반례): inf·−inf·NaN 이 섞인 묶음(카메라 2·4대), 전부 유한하지 않은 묶음, 한 카메라만 유한하지 않음(cam1 x 만 inf, 마지막 카메라 x·y inf, 넘치는 신뢰도 1e308, 사람 단위 경로에서 그 카메라만 빠짐), Cross-view 2-view, 공분산 넘침(중심이 원점인 카메라 z 1e-170), 공분산 역행렬 넘침(초점거리 1e-154, `_position_sigmas` 표준편차 inf), 보정 값 거절, 왜곡 계수 거절(NaN·±inf·k4 1e200·허용치 inf). `tests/calibration/test_watch.py::test_realign_rotation_skips_correspondences_without_direction`(기준점 하나가 대상 카메라 중심과 같아도 회전을 맞추고 Kabsch 입력은 유한한 단위 벡터), `::test_realign_rotation_rejects_overflowing_camera_center`(리뷰 재현: 중심이 넘치는 R·t 는 `ValueError`). 새 비교는 `tobytes()` 비트 비교다(`assert_array_equal` 은 +0·−0 을 구분하지 않는다).
- 시간 제한: `pytest-timeout` 은 의존성에 없고, 멈춤은 C 호출 안이라 스레드로 끊을 수 없다. 기존 방식(`tests/test_winsock_retry.py` 의 `subprocess.run(..., timeout=60)`)을 따라 `tests/geometry/bounded.py` 가 시험 본문을 새 프로세스에서 60초 제한으로 부른다. 60초는 프로세스 시작·import 를 넉넉히 덮는 값이다(계산은 1초 안).
- 음성 대조(Linux 컨테이너, 이 변경의 소스 4개만 dev 로 되돌림): 위 7개 시험 모두 실패 — 시간 초과 5(degenerate·섞인 묶음·전부 비유한·한 카메라·Cross-view), 공분산 `LinAlgError` 1, 보정 거절 없음 1. Windows 에서는 Cross-view(`LinAlgError`)·보정 거절 2개만 실패한다(멈춤은 Linux 에서만 재현).
  리뷰 반영분(공분산 역행렬·왜곡 계수·재보정 Kabsch)은 Docker 가 꺼져 Windows 에서만 음성 대조했다: 그 고침만 되돌리면 새 시험 3개 모두 실패(공분산 ok True 로 남음, 왜곡 계수 ValueError 없음, Kabsch `LinAlgError`).

### 버린 대안

- 유한한 행만 골라 SVD(`A[finite]`): 결과는 같지만 색인·되돌려 넣기 코드가 더 길다. 0 행렬은 특이값이 0 이라 원래 판정으로도 해가 없다.
- `pytest-timeout` 추가: 몇 줄 때문에 dev 의존성을 늘리지 않았다. 스레드 방식 제한은 C 호출 안의 멈춤을 끊지 못해 프로세스 전체가 남는다.
- 이전 구현 고정 사본(`legacy_batch_*.py`)에 같은 거르기 넣기: 비교 기준이 바뀌므로 하지 않았다.
- SVD 를 다른 분해(`eigh` 등)로 바꾸기: -237 에서 버린 이유(해 유일성 판정 정밀도)와 같다.

### 남은 위험

- 리뷰 반영분(공분산 역행렬·왜곡 계수·재보정 Kabsch)은 Linux 컨테이너에서 다시 돌리지 않았다(Docker 데몬 꺼짐). 거르는 위치가 LAPACK 호출 앞이라 OS 와 무관하게 같은 경로를 탄다.

- 보정 도구(번들 조정·내부 파라미터·외부 파라미터 초기값·World 좌표계·재보정)의 `svd`·`lstsq` 는 거르지 않았다. 입력은 왜곡 제거가 성공한 관측이라 inf·NaN 이 들어갈 길은 좁지만, 넣으면 Linux 에서 그 도구가 멈출 수 있다. 판정 루프 밖이라 별도 티켓으로 넘긴다.
- `_two_view_arrays` 는 유한한 입력의 SVD 미수렴 `LinAlgError` 를 잡지 않는다(이전과 같음, 재현 사례 없음).
- 멈춤 조건은 OpenBLAS 판에 따라 달라질 수 있다. 시험은 이 노트북 Docker 의 numpy 2.3.5·OpenBLAS 0.3.30 에서 확인했다.
