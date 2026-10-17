# Tier 0 카메라 간 연결(Cross-view Association) 설계

S15P31A101-34. 기획서 §5.2(추적 3계층, 계산 순서, 2대 관측 경쟁 후보), §6.2, 요구사항 FR-60, 부록 D-G2-04 기준이다.
코드는 `ai/src/simai_ai/tracking/cross_view.py`(연결), `tracking/assignment.py`(헝가리안)이고, 시험은 `ai/tests/tracking/` 의 `test_cross_view.py`, `test_assignment.py`, `test_cross_view_sim.py`, 측정 스크립트는 `sim_cross_view.py` 다.

## 범위

| 단계(§5.2 계산 순서) | 이 문서 | 다른 티켓 |
|---|---|---|
| 1. 공통 시각 보간 | `LocalTrackletBuffer` 가 `observation_2d` 를 (카메라, 로컬 트랙)마다 모아 `geometry.interpolation` 으로 보간 | 보간 규칙 자체는 S15P31A101-26 |
| 2. 연결 후보 생성 | 에피폴라 거리, 시간, 연결 이력, 임시 바닥 가설 | |
| 3. 후보별 삼각측량 검증 | 2-view 삼각측량 재투영 잔차·일관 관절 비율·관절 높이 범위, 클러스터 전체 `triangulate_skeleton` | 관절 유효성 게이트(VALID/UNCERTAIN/UNAVAILABLE 상세)는 S15P31A101-29 |
| 4. ContactEstimator | | 접지점, 지지 표면 |
| 5. 최종 연결과 World Track 갱신 | view 클러스터와 `association_id`(이 계층 내부 번호), `IDENTITY_UNCERTAIN` 사유 | World ID·칼만 필터·늦은 재연결·ID 교정은 World Track Manager(S15P31A101-37) |

이 모듈은 위치를 정하지 않는다. 클러스터의 관절 3D 는 `triangulate_skeleton` 결과(모두 `observed`) 그대로이고, 위험 판정·접지는 하지 않는다.

## 입력과 출력

| 항목 | 형식 |
|---|---|
| 입력 | `Observation2DFrame`(Local Tracker 출력) → `LocalTrackletBuffer.views_at(t)` → `LocalView(camera_id, local_track_id, InterpolatedView)` 목록. 보정은 `CameraRig` |
| 출력 | `AssociationFrame(t, calibration_version, clusters, pairs)`. `ViewCluster(association_id, views, skeleton, uncertain_reasons, competitors)`, `PairEvidence(a, b, common_joints, epipolar_px, floor_gap_m, residual_px, consistent_joints, reject, marginal, cost)` |

`ViewCluster.identity_uncertain` 이 참이면 World Track Manager 가 `identity_confidence = IDENTITY_UNCERTAIN` 으로 낸다. `association_id` 는 World ID 가 아니다.

## 알고리즘

1. **후보 근거(카메라가 다른 view 쌍마다).** 두 view 모두 신뢰도 0.3 이상인 공통 관절이 4개 미만이면 근거 없음(`FEW_COMMON_JOINTS`). 공통 관절의 대칭 에피폴라 거리 평균(정규화 좌표 거리 × 초점 거리, 근사 픽셀)이 문턱을 넘으면 탈락.
2. **임시 바닥 가설(비용 전용).** 발목(양쪽 중점) 시선이 수평면 Z=h 와 만나는 XY 끼리의 거리를 h ∈ [0, 1.0 m] 에서 최소로 본다(h=0 이 바닥 Z=0 가설, 상한은 단상·책상). 비용은 min(거리/1 m, 0.5) 로 거리에 단조 비감소이고 가설이 없을 때와 같은 값(0.5)에서 멈춘다. 먼 거리는 **탈락이 아니라 "가설로 판단할 수 없음"** 이라 더 벌하지 않는다(처음엔 1 m 안만 거리 비용, 밖은 0.5 라 0.9 m 짝이 5 m 짝보다 비싼 비단조였다, 2차 리뷰). 가설 높이보다 높은 면(책상 위 의자 약 1.2 m, R3 의 2 m 고소, §8 "고소 작업자는 Z로 직접 표현") 위 사람은 시선이 1 m 이하 수평면에서 만나지 않기 때문이다. 처음 구현은 이 거리로 하드 탈락시켜 지지 표면이 약 1.1 m 를 넘는 사람이 4대에 다 보여도 연결되지 않았다(리뷰에서 발견). 지금은 탈락을 에피폴라·삼각측량(재투영 잔차, 관절 높이 범위) 근거에만 맡기고, 지지 표면 1.2·1.5·2.0 m 사람이 4대 한 클러스터로 연결되는 시험(`test_person_on_high_surface_is_connected`)을 둔다. **이 가설은 World 위치·`ground_anchor_xyz`·Z 로 쓰지 않고 결과에 XY 를 내보내지 않는다**(§5.2, AGENTS.md "모르면 비운다"). 0.5 m 남짓 붙은 두 사람은 이 근거로 가릴 수 없다.
3. **후보별 삼각측량 검증.** 공통 관절을 2-view 선형 DLT(`geometry.triangulation` 과 같은 식, 신뢰도 가중)로 한 번에 풀어 재투영 오차를 잰다. 이상치(20 px 초과)를 뺀 일관 관절이 공통 관절의 60% 미만이면 `TRIANGULATION`, 관절 Z 가 [−0.3, 4.5] m 밖인 비율이 25% 초과면 `IMPLAUSIBLE_HEIGHT`(엇갈린 시선의 허상 교점. 상한 4.5 m 는 R3 고소 2 m 면 위 키 큰 사람이 손을 든 높이 약 4.3 m 를 담는다), 잔차 중앙값이 12 px 초과면 `RESIDUAL`. 8 px 초과면 잔차 경계(`marginal`). 후보 쌍은 시각마다 view 수의 제곱만큼 생겨 관절별 가우스-뉴턴까지 하는 `triangulate_skeleton` 을 쓰면 10 Hz 를 지키기 어렵다(SIM 2명·8 view 에서 tick 당 약 90 ms → 벡터화 후 약 27 ms, 노트북 CPU). 그래서 후보 검증만 벡터화하고, 클러스터 최종 관절은 `triangulate_skeleton` 그대로 쓴다.
4. **쌍 비용** = 에피폴라/10 px + 잔차/8 px + min(바닥 가설 거리/1 m, 0.5)(가설이 없으면 0.5).
5. **연결 이력(궤적 일관성).** 로컬 트랙 쌍이 같은 클러스터였던 시각 수(`link_age`). 병합 비용에서 최대 0.3(10 tick = 1 s 에서 꽉 참)을 뺀다. 기하가 탈락시킨 쌍을 살리지는 않는다. 둘 다 보이는데 갈라지면 지우고(애매함 때문에 못 합친 경우는 유지), 한쪽이 안 보이면 2 s 동안 들고 있다. **보너스(0.3)를 애매함 여유(0.5)보다 작게 둔다**: 이력만으로는 기하가 같은 경쟁 후보를 이길 수 없어, 로컬 트랙 ID 가 옆 사람으로 넘어간 경우에도 애매함 표시가 남는다(`test_history_cannot_beat_geometrically_equal_rival`). 기하가 조금(비용 0.2 이상) 앞설 때만 이력이 흔들림을 막는다. 처음 값(보너스 = 여유 = 0.5)은 이력이 꽉 차면 기하가 같은 경쟁 후보도 애매하지 않게 되어 잘못된 연결이 최대 2 s 남을 수 있었다. 대가로 붙어 걷는 두 사람의 완전 연결 비율이 조금 내려갔다(아래 표). 애매함 여유 0.5 는 쌍 비용 척도에서 에피폴라 약 5 px 또는 잔차 약 4 px 차이다.
6. **제약 있는 최선 우선 병합.** view 하나씩인 클러스터에서 시작해 합칠 수 있는 쌍 중 비용이 가장 낮은 쌍부터 합친다. 병합 비용은 두 클러스터 사이 view 쌍 비용 평균 − 이력 보너스 평균이고, 3.0 이상이면 합치지 않는다.
   - **같은 카메라 금지:** 카메라가 겹치는 두 클러스터는 합치지 않는다. 그래서 클러스터마다 카메라당 view 하나, view 마다 클러스터 하나(카메라별 1:1)다.
   - **완전 연결:** 기하가 반대하는 쌍이 하나라도 있으면 합치지 않는다. 근거 없는 쌍(`FEW_COMMON_JOINTS`)은 반대로 보지 않되 비용 1.0 으로 세고, 근거 있는 쌍이 하나는 있어야 한다.
   - 카메라를 차례로 헝가리안 배정하는 방식(Chen et al.)을 먼저 구현했으나, SIM 교차 시나리오에서 앞 카메라의 약한 연결(잔차 6 px, 관절 5개)이 먼저 굳어 뒤 카메라의 반대 근거를 가리는 오병합이 나와 바꿨다. 헝가리안 1:1 배정은 시각 사이 클러스터를 잇는 데(`association_id` 물려주기, 겹치는 트랙 수 최대) 쓴다.
7. **애매하면 합치지 않는다(`IDENTITY_UNCERTAIN`).**
   - `AMBIGUOUS_ASSIGNMENT`: 고른 병합 (A, B) 와 서로 배타적인 경쟁 병합(A 와 합칠 수 있고 B 와 카메라가 겹치는 C, 또는 그 반대)의 비용 차가 0.5 미만이면 이번 시각에 막는다. 뒤에 근거가 쌓여 한 클러스터가 되면 표시를 지운다.
   - `MARGINAL_RESIDUAL`: 잔차 경계 쌍이 든 병합은 막는다.
   - `TWO_VIEW_COMPETITOR`: 2대뿐인 클러스터에 다른 트랙도 기하로 맞으면 제3의 카메라로 가릴 수 없으므로 표시한다(§5.2 2대 관측 경쟁 후보의 클러스터 단위 표시. 관절 단위 판정은 S15P31A101-29).
   - `VIEW_SPLIT`: 클러스터 전체 `triangulate_skeleton` 에서 관절 대부분이 이상치로 빠진 view 는 떼어 낸다.

**입력 계약.** 동기화가 무효인 카메라(System Health `HealthReport.sync_invalid_cameras`)는 Tier 0 조립부가 `associate(t, views, exclude_cameras=...)` 로 넘긴다. 문자열 하나를 넘기면 `TypeError`, 보정에 없는 카메라 이름이면 `ValueError` 다(동기화 무효 카메라가 조용히 융합되지 않게). 그 카메라의 view 는 그 시각 연결에 쓰지 않고 `AssociationFrame.excluded` 에 키만 남는다(연결 이력은 안 보이는 트랙처럼 2 s 유지). 프레임별 촬영 시각 불확실성은 `LocalTrackletBuffer`(보간)가 거른다.

수치는 모두 초기값이다. SIM 은 픽셀 잡음 σ=1 px 라 실제 검출 잡음보다 훨씬 작으므로, 문턱(에피폴라 20 px, 잔차 12 px, 새 클러스터 3.0)은 SIM 에 맞추지 않고 실제 잡음을 견디도록 넉넉히 두었다. G2·A0 녹화로 조정한다.

## SIM 측정 (상한, 결론 아님)

`uv run python tests/tracking/sim_cross_view.py` (ai/). 시나리오 crossing(내장, 0.6 m 떨어진 평행선을 마주 걸어 엇갈림), side_by_side(스크립트에서 정의, 0.5 m 간격으로 나란히 대각선·직선 걷기), 시드 1·2·3, 10 Hz tick. 변형 stable 은 시뮬레이터 트랙 ID 를 로컬 트랙으로 그대로, fragmented 는 카메라마다 1.5 s 마다 로컬 트랙 ID 를 새로 붙여 Local Tracker 조각남을 흉내 냈다.
**SIM 입력의 상한 성능(`input_provenance.is_upper_bound = true`)이다. 제품 성능이나 G2 결론으로 쓰지 않는다.**

| 시나리오/시드/변형 | 같은 카메라 오병합 | 사람 섞인 클러스터 | 쌍 정밀도 | 쌍 재현율 | 완전 연결 비율 | ID 전환 | 구성 흔들림 tick | IDENTITY_UNCERTAIN 클러스터 |
|---|---|---|---|---|---|---|---|---|
| crossing/1/stable | 0 | 0 | 1.000 | 0.993 | 0.986 | 0 | 1/70 | 0/140 |
| crossing/1/fragmented | 0 | 0 | 1.000 | 0.993 | 0.986 | 0 | 3/70 | 0/140 |
| crossing/2/stable | 0 | 0 | 1.000 | 0.993 | 0.986 | 0 | 1/70 | 0/142 |
| crossing/2/fragmented | 0 | 0 | 1.000 | 0.989 | 0.978 | 0 | 3/70 | 0/143 |
| crossing/3/stable | 0 | 0 | 1.000 | 0.989 | 0.978 | 0 | 2/70 | 0/141 |
| crossing/3/fragmented | 0 | 0 | 1.000 | 0.992 | 0.986 | 0 | 2/70 | 0/140 |
| side_by_side/1/stable | 0 | 0 | 1.000 | 0.986 | 0.976 | 1 | 4/85 | 10/172 |
| side_by_side/1/fragmented | 0 | 0 | 1.000 | 0.966 | 0.929 | 0 | 14/85 | 28/180 |
| side_by_side/2/stable | 0 | 0 | 1.000 | 0.983 | 0.970 | 0 | 5/85 | 11/175 |
| side_by_side/2/fragmented | 0 | 0 | 1.000 | 0.965 | 0.929 | 0 | 10/85 | 24/182 |
| side_by_side/3/stable | 0 | 0 | 1.000 | 0.974 | 0.952 | 0 | 6/85 | 16/176 |
| side_by_side/3/fragmented | 0 | 0 | 1.000 | 0.966 | 0.929 | 0 | 13/85 | 25/180 |

- 쌍 정밀도·재현율: 같은 tick 의 카메라가 다른 view 쌍마다 "같은 클러스터" 예측을 정답과 비교. 완전 연결 비율: 2대 이상에 보인 (tick, 사람) 중 그 사람의 view 가 전부 한 순수 클러스터에 든 비율(crossing 138, side_by_side 168). ID 전환: 사람마다 view 를 가장 많이 담은 클러스터의 `association_id` 가 바뀐 횟수. 구성 흔들림: 직전 tick 과 지금 모두 있는 로컬 트랙끼리의 묶음이 달라진 tick 수.
- 놓친 연결은 대부분 엇갈림·모퉁이에서 가림으로 공통 관절이 줄거나 경쟁 후보가 생겨 일부러 합치지 않은 것이다(갈라진 ID 가 잘못 합친 ID 보다 덜 위험, §5.2).

**음성 대조**(`--no-exclusive`, 같은 카메라 금지 제약과 그에 딸린 경쟁 병합 검사를 끔): side_by_side 는 모든 시드·변형에서 같은 카메라 오병합이 1~2건(stable 2, fragmented 1) 생기고 같은 수의 사람 섞인 클러스터가 생겨 시험이 실패한다. crossing 은 완전 연결만으로도 오병합이 나지 않아 음성 대조로는 실패하지 않는다. SIM 없이 빠르게 같은 성질을 보는 단위 시험(`test_same_camera_constraint_alone_blocks_shifted_duplicate`, 6 px 어긋난 같은 카메라 중복 검출은 애매함 검사에 걸리지 않아 제약만으로 막힌다)과 그 음성 대조도 둔다.

**시험 범위.** `test_cross_view_sim.py` 는 시뮬레이터 출력을 한 번 만들어 정상·음성 대조가 함께 쓰고, stable 은 시드 3개 모두, fragmented 는 시드 1 만, 음성 대조는 side_by_side 시드 3개만 돈다(약 25 s). 위 전체 격자는 측정 스크립트로 잰다.

## 남은 일

- 2-view 허상 오병합: 서로 다른 사람이 각각 한 카메라에만 보이고 두 시선이 맞으면 2대 클러스터가 생기고, 같은 카메라 경쟁 후보가 없으면 `TWO_VIEW_COMPETITOR` 도 붙지 않는다(2차 리뷰 무작위 50% 가림 200장면에서 7건 중 4건 무표시). 부록 D-G2-06(2대 영역 오연결률) 측정 대상이다. World Track Manager(-37)로 넘길 때 2대뿐인 클러스터는 ID 확신을 낮춰(예: MEDIUM 이하, 새 World ID 와 병합 보류) 다루는 방안을 함께 정한다.
- SIM 시험의 fragmented 변형은 시드 1 만 CI 에서 돈다(시드 2·3 은 측정 스크립트). 시간 여유가 생기면 시드 하나를 더 넣는다.

- 실제 녹화(A0)·G2 로 문턱과 비용 척도를 조정한다. SIM 잡음(σ=1 px)이 실제보다 작아 지금 값은 SIM 결론이 아니다.
- 처리 시간: 리뷰 측정(노트북 CPU)으로 무작위 10명·40 view 에서 tick 당 평균 175 ms(최대 194 ms)라 10 Hz 예산을 넘는다. 약 2/3 가 클러스터 최종 `triangulate_skeleton`(관절별 가우스-뉴턴)이다. Pod(H100 노드) CPU 에서 다시 재고, 필요하면 최종 삼각측량을 벡터화한다.
- 붙어 걷는 두 사람의 구성 흔들림: 위 표 side_by_side fragmented 에서 85 tick 중 10~14 tick, 리뷰의 잡음 1.5 px 실험에서 50 tick 중 16~20회. 잘못 합치지는 않지만 World Track Manager 로 넘길 때 애매한 tick 에 직전 구성을 유지하는 장치를 -37 과 함께 검토한다.
- 평균 병합 비용은 맞는 view 수를 보상하지 않는다. 카메라 기준선과 평행하게 선 배치에서는 3대가 맞는 클러스터와 2-view 허상 쌍이 동점이 되어 계속 갈라진다(안전하지만 G2 ID 유지율에 불리). 다른 카메라가 허상 쌍에 반대 근거를 내는지 보는 방식이나 view 수 가중을 G2 전에 검토한다.
- World Track Manager(S15P31A101-37)가 `association_id`·클러스터를 받아 World ID, `identity_revision`, 늦은 재연결을 만든다. 이전 프레임 3D 재투영(기존 World Track 궤적) 근거는 World Track 이 생긴 뒤 비용에 더한다.
- ReID 는 쓰지 않았다(§5.2 보조 근거, 필요 시 추가).
