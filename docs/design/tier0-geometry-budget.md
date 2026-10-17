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
