# Tier 0 World Track Manager 설계

S15P31A101-37. 기획서 §5.2(추적 3계층, 계산 순서 5단계, World 위치의 정의, ID 교정 기록), §6.1(시간 모델), §6.2(World Track 상태), §15.1 G2(ID 유지 시간 비율), 부록 D-G2-04 기준이다.
코드는 `ai/src/simai_ai/tracking/world_track.py`(World ID·연결·칼만 필터·생명주기), `tracking/world_state.py`(I1 `world_state` 조립)이고, 시험은 `ai/tests/tracking/` 의 `test_world_track.py`, `test_world_track_sim.py`, 측정 스크립트는 `sim_world_track.py` 다.

## 범위

| 단계 | 이 문서 | 다른 티켓 |
|---|---|---|
| 계산 순서 1~3 (보간, 연결 후보, 후보별 삼각측량) | 입력으로 받는다(`AssociationFrame`) | Cross-view(S15P31A101-34), 삼각측량·관절 게이트(-26·-29) |
| 계산 순서 4 (ContactEstimator) | 결과를 받을 자리(`SupportContact`)만. 없으면 모름 | ContactEstimator |
| 계산 순서 5 (최종 연결과 World Track 갱신) | World ID 발급, 클러스터 → World Track 1:1 연결, 칼만 필터, 생명주기, `world_state` 조립 | |
| ID 교정 기록·늦은 재연결 | `IdentityRevision`·`identity_revision`·별칭 기록 자리만. 자동 병합·교정은 하지 않고 같은 사람 두 ID 후보는 표시만 한다 | 병합·분할·교정·늦은 재연결은 S15P31A101-40 |

## 입력과 출력

| 항목 | 형식 |
|---|---|
| 입력 | `WorldTrackManager.update(frame: AssociationFrame, rig: CameraRig, sync_invalid_cameras=())`. Cross-view 에 넘긴 `exclude_cameras` 와 같은 값을 넘긴다 |
| 출력 | `WorldTrackFrame(t, calibration_version, tracks, revisions, input_provenance)`. `WorldTrackSnapshot` 은 World ID 마다 위치·σ·속도·생명주기·ID 확신·이번 시각의 골반·관절(`GatedSkeleton`)·`head_z`·카메라 |
| I1 문서 | `world_state_payload(frame, t_latest_global, scene_version, contacts=None)` → `world_state_message(payload, msg_id=, sent_at=)` |

`association_id` 는 Cross-view 내부 번호다. World ID 로 쓰지 않고 `WorldTrackSnapshot.association_id` 에 출처로만 남긴다.

## World ID

`WorldIdIssuer(session)` 이 `w-{세션}-{번호:04d}` 로 낸다. 세션 접두어는 Tier 0 조립부가 서비스 시작 시각(주입한 시계)으로 `session_prefix_from_ms` 를 써서 만든다(36진수). 재시작하면 접두어가 달라 이전 세션 ID 와 겹치지 않고, 세션 안 번호는 늘기만 해 LOST 된 ID 를 다시 쓰지 않는다. 계약(`world_id`)은 "세션 단위 익명 World ID" 문자열이라 형식 변경이 없다.

## 연결 (계산 순서 5단계)

1. **클러스터 기준점.** 클러스터 삼각측량(`ViewCluster.skeleton`)의 골반 XY, 골반이 없으면 삼각측량된 관절 XY 평균. 1대 클러스터는 3D 가 없어 기준점이 없다. 기준점은 비용에만 쓰고 위치로 내보내지 않는다.
2. **비용과 3D 거리 게이트.** 비용 = |예측 XY − 기준점| / 0.5 m − 0.2 × (클러스터 로컬 트랙 중 이 Track 의 것 비율). 게이트 반경 = min(1.5 m, 0.5 m + 2.0 m/s × (t − t_last_valid)) (사람이 그 시간에 갈 수 있는 거리, 속도 상한은 §6.1 초기값). 게이트 밖이면 로컬 트랙 연결 이력이 있어도 붙이지 않는다(로컬 트랙 ID 가 옆 사람으로 넘어간 경우). 연결 이력 보너스(0.2)를 애매함 여유(0.3)보다 작게 둬 이력만으로 기하가 같은 경쟁 후보를 이기지 못하게 한다(Cross-view 와 같은 원칙).
3. **헝가리안 1:1.** Cross-view 가 IDENTITY_UNCERTAIN 으로 표시하지 않은 클러스터를 배정한다. 고른 짝과 경쟁 짝(같은 클러스터를 다투는 다른 Track, 같은 Track 을 다투는 다른 클러스터)의 비용 차가 0.3 미만이면 **배정은 하고** 관련 Track 에 IDENTITY_UNCERTAIN(`AMBIGUOUS_TRACK_MATCH`)을 붙인다. 붙어 선 두 사람을 둘 다 갱신하지 않으면 위치까지 잃어 둘 다 PREDICTED → UNKNOWN 이 되므로, 위치는 갱신하고 신원만 불확실로 둔다(§5.2 "ID 확신과 위치 확신도 별도로 관리").
4. **IDENTITY_UNCERTAIN 클러스터는 억지로 붙이지 않는다.** 남은 Track 중 게이트 안 후보가 하나뿐이고(여유 안 경쟁 Track 없음), 다른 Track 의 로컬 트랙이 섞이지 않았을 때만 붙이고 `CROSS_VIEW_UNCERTAIN` 을 붙인다. 후보가 여럿이면 붙이지도 새로 만들지도 않는다(새로 만들면 같은 사람 두 번째 ID). 이번 시각에 이미 다른 클러스터를 받은 Track 은 이 클러스터의 사람일 수 없으므로 후보에서 뺀다. 게이트 안에 남은 Track 이 없으면 IDENTITY_UNCERTAIN 인 새 ID 를 만든다(§5.2 "새 ID를 만들거나 IDENTITY_UNCERTAIN", 사람을 빠뜨리면 위험 판정 입력에서 빠진다). 다만 그 클러스터의 로컬 트랙이 이번 시각에 이미 갱신된 Track 의 것이고 그 Track 이 클러스터 0.35 m(`duplicate_radius_m`) 안이면 Cross-view 가 한 사람을 갈라 놓은 나머지 view 라 만들지 않고, 그 Track 에 `CROSS_VIEW_UNCERTAIN` 을 붙인다(가까이 선 새 사람일 수도 있으므로 표시는 남긴다, 3차 리뷰, `test_blocked_split_view_near_owner_flags_owner`). 거리 조건이 없던 구현은 로컬 트래커가 A 의 옛 ID 를 옆(0.45 m)이나 먼 곳(1.2 m)의 새 사람 B 에게 넘기면 그 로컬 트랙 소유가 끝날 때까지(2 s) B 를 빠뜨렸다(재리뷰 N1, `test_new_person_with_switched_local_id_is_not_dropped`). 처음 구현은 이미 갱신된 Track 도 후보로 세어, 그 옆(0.4 m)에 새로 나타난 사람의 IDENTITY_UNCERTAIN 클러스터를 계속 막아 world_state 에서 사람이 빠졌다(리뷰 F2). 로컬 트랙 조건을 빼면 SIM side_by_side 에서 갈라진 나머지 view 가 같은 사람 두 번째 ID 가 되어 유지 비율이 0.77 로 떨어졌다.
5. **1대 클러스터와 붙지 않은 클러스터**는 위치를 갱신하지 않는다. 그 로컬 트랙의 주인 Track 이 이번 시각에 갱신되지 않았으면 연결 이력만 이어 둔다(2 s). 가림 동안 남은 카메라 한 대의 트랙으로, 가림이 끝났을 때 같은 Track 으로 돌아오게 하는 장치다.
6. **새 World Track.** 배정되지 않은 클러스터 중 골반 측정이 있는 것만 TENTATIVE 로 만든다. 1대·골반 없는 클러스터로는 만들지 않는다(2대 미만은 지어내지 않음). 다른 Track(예측 위치)이나 같은 시각에 먼저 생긴 Track 의 0.35 m 안이면 그 쌍을 같은 사람 두 ID 후보로 둔다(7). 사람을 놓치는 것(위험 판정에서 빠짐)이 두 ID 보다 위험하므로 가깝다는 이유로 만들기를 막지 않는다.
7. **같은 사람 두 ID 는 합치지 않고 표시만 한다.** 붙인 클러스터에 이번 시각 갱신되지 않은 다른 Track 의 로컬 트랙이 있고 그 Track 이 0.35 m 안이거나, 새 ID 가 다른 Track 0.35 m 안에 생기면 그 쌍을 후보로 둔다. 후보 쌍은 둘 다 살아 있고 0.35 m 안인 동안 매 시각 둘 다 IDENTITY_UNCERTAIN(`POSSIBLE_DUPLICATE`)을 받는다. 처음 구현은 이 경우 먼저 만든 ID 로 자동 병합했는데, 로컬 트래커 ID 전환 한 번(옆 사람 ID 가 넘어감)과 근접만으로 서로 다른 두 사람이 한 ID 로 합쳐졌다(리뷰 F1, `test_local_id_switch_does_not_merge_two_people`). 둘을 구별할 근거가 없으므로 병합·분할·교정은 -40 으로 넘기고 `identity_revision` 은 0 그대로다. 0.35 m 는 같은 사람이 갈라진 두 클러스터 골반(거의 같은 곳)과 서로 다른 두 사람 골반(어깨 폭 약 0.45 m 보다 가깝기 어려움)을 가르는 값이다. 0.5 m 로 두면 나란히 걷는 두 사람(0.5 m 간격)이 계속 표시된다.

같은 카메라의 두 검출은 Cross-view 클러스터에서 이미 갈라져 있고(카메라당 view 하나), Track 하나에 시각마다 클러스터 하나만 붙이므로 한 World ID 에 같은 카메라의 두 검출이 동시에 묶이지 않는다(D-G2-04).

## World 칼만 필터

- 측정: 관절 유효성 게이트를 거친 골반(양쪽 엉덩이 중점). 측정 표준편차 = 두 엉덩이 삼각측량 σ 의 합성(√(σl²+σr²)/2, 하한 3 cm), 골반이 UNCERTAIN 이면 ×3. 골반이 없거나 UNAVAILABLE 이거나 σ 를 모르면(공분산 퇴화) 측정이 없는 시각이다. `t_last_valid` 는 측정이 있을 때만 바뀐다(§6.1 예측으로 유효기간을 늘리지 않음).
- 모델: 상태 [x y z vx vy vz], 등속 + 백색 가속도(수평 2.0, 수직 1.0 m/s²), Joseph 형식 갱신. 첫 측정의 속도 σ 1.0 m/s. 측정 공백이 500 ms(§6.1 PREDICTED 상한)를 넘은 뒤 다시 잡으면 묵은 속도로 옮기지 않고 측정에서 다시 시작한다.
- **내보내는 위치.** `position_xy` 는 t_last_valid 의 필터 XY 다. 측정이 없는 시각에는 속도로 옮기지 않고 `position_xy_sigma` 만 필터 σ + 2.0 m/s × (t − t_last_valid) 로 키운다. 이 원판은 그 사이 사람이 갈 수 있는 곳을 모두 담는다. 위치 예측은 Risk Engine 쪽 시간 모델(`evaluate_track`)이 staleness 로 한다. `time_model.TrackTimeInput` 은 위치를 t_last_valid 시각 값으로 받으므로 `WorldTrackSnapshot.time_input()` 은 공백 확대 전 필터 σ 를 넘긴다(σ 이중 확대 방지).
- 연결 게이트의 예측은 속도(수평 속도 상한으로 자름)로 최대 500 ms 까지만 옮긴다.

계약 문구(`position_xy`: "둘 다 없으면 예측값")와 시간 모델(위치는 t_last_valid 값)이 시각 기준을 다르게 읽을 수 있어, 이 구현의 "t_last_valid 위치 + 커진 σ" 를 계약 1.1.0 설명에 명시했다(Lead 승인, 의미 변경 없는 설명 보강).

## 생명주기와 관측 상태

| 전이 | 조건 (초기값, 명세에 수치 없음) |
|---|---|
| 생성 → TENTATIVE | 골반 측정이 있는 새 클러스터 |
| TENTATIVE → CONFIRMED | 위치 측정 3회 (10 Hz 0.3 s) |
| TENTATIVE → LOST | 측정 공백 300 ms 초과 |
| CONFIRMED → OCCLUDED | 측정 공백 200 ms 초과 (§6.1 OBSERVED 상한과 같은 초기값) |
| OCCLUDED → CONFIRMED | 측정 |
| CONFIRMED·OCCLUDED → LOST | 측정 공백 3 s 초과. LOST 는 그 시각에 한 번만 내고 지운다. 그 뒤 다시 보이면 새 ID(늦은 재연결은 -40) |

관측 상태는 생명주기와 따로 `world_state` 조립 때 gap = t_latest_global − t_last_valid 를 `time_model.classify_observation` 에 넣어 정한다(0~200 ms OBSERVED, ~500 ms PREDICTED, 넘으면 UNKNOWN). 그래서 TENTATIVE·PREDICTED, OCCLUDED·OBSERVED 같은 조합이 있을 수 있다.

**ID 확신(`identity_confidence`).** 사유(`IdentityFlag`)가 마지막으로 붙은 뒤 1 s 동안 IDENTITY_UNCERTAIN 을 유지한다(깜박임 방지, 보수적. Risk Engine 은 IDENTITY_UNCERTAIN 이면 해소를 확인하지 않는다). 그 밖에는 TENTATIVE → LOW, OCCLUDED·이번 시각 미갱신·2대로만 갱신·연결 이력 없이 다시 잡은 뒤 1 s → MEDIUM, 3대 이상으로 갱신된 CONFIRMED → HIGH.

| 사유 | 붙는 때 |
|---|---|
| `CROSS_VIEW_UNCERTAIN` | Cross-view IDENTITY_UNCERTAIN 클러스터를 붙였거나, 이 Track 이 다투던 그런 클러스터를 붙이지 않았다 |
| `AMBIGUOUS_TRACK_MATCH` | 연결 비용 차가 여유보다 작다 |
| `TWO_VIEW_COMPETITOR_JOINT` | 관절 게이트의 2대 관측 경쟁 후보가 2대 클러스터에 있거나 엉덩이 관절에 있다. 3대 이상이 맞춘 사람의 손목 하나가 경쟁 후보라고 신원 전체를 불확실로 두지 않는다(그 관절은 게이트가 이미 UNCERTAIN). 처음에는 관절 하나라도 경쟁 후보면 붙였는데, SIM 나란히 걷기에서 거의 모든 tick 이 IDENTITY_UNCERTAIN 이 되어 바꿨다 |
| `POSSIBLE_DUPLICATE` | 같은 사람 두 ID 후보 쌍(7)이 0.35 m 안에 있는 동안 매 시각 |

## world_state 조립

- 필드는 계약 그대로다. `skeleton`·`pelvis_xyz`·`cameras` 는 이번 시각에 클러스터가 붙은 Track 만 채운다. 관절은 모두 observed 다. 클러스터가 붙었는데 골반 측정이 없는 시각(엉덩이가 가려짐)에는 관측 상태가 PREDICTED·UNKNOWN 이어도 이번 시각의 `skeleton`·`head_z`(VALID)·`cameras` 는 싣는다. 관절 관측 자체는 이번 시각의 사실이고, 위치(`position_xy`)만 측정이 없다는 뜻이다.
- 접지: ContactEstimator 가 아직 없어 `ground_anchor_xyz = null`, `support_surface_validity = UNKNOWN`, `contact_validity = UNAVAILABLE`, `z_validity = UNAVAILABLE` 이다. 결과가 생기면 `SupportContact` 로 넘긴다. `SupportContact` 는 지지 표면이 VALID 가 아니거나 z_validity 가 UNAVAILABLE 인데 접지점이 있으면 만들 때 거부한다(Z=0 대체 금지, 계약 불변 규칙).
- `head_z`: 게이트가 VALID 로 낸 값만 싣는다. UNCERTAIN·UNAVAILABLE 이면 null(사용자 결정, tier0-joint-gate.md).
- `input_provenance`: `WorldTrackManager` 에 넘긴 출처를 `payload.input_provenance`(계약 1.1.0 선택 필드, Lead 승인)로 싣는다. 넘기지 않으면 필드를 넣지 않는다. SIM·DATASET_GT 입력이면 `is_upper_bound = true`.
- 봉투 `schema_version` 은 world_state 가 마지막으로 바뀐 1.1.0, `msg_id`·`sent_at` 은 호출자가 넣는다(현재 시각을 읽지 않는다).

## SIM 측정 (상한, 결론 아님)

`uv run python tests/tracking/sim_world_track.py` (ai/). 파이프라인은 LocalTrackletBuffer → CrossViewAssociator → WorldTrackManager → world_state 조립, 10 Hz tick, 시드 1·2·3.
시나리오: crossing(내장), side_by_side(sim_cross_view.py 정의), occlusion(crossing 에서 east 를 2.0~3.2 s 동안 cam1 에만 보이게 하고, 가렸던 카메라는 가림 뒤 새 로컬 트랙 ID), exit_reentry(east 를 2.0~3.5 s 동안 모든 카메라에서 지우고 새 로컬 트랙 ID 로 재등장), exit_long(1.5~5.0 s, 소실 시간 3 s 초과), id_switch(crossing 에서 엇갈리는 3.0 s 부터 cam1 의 두 사람 로컬 트랙 ID 를 맞바꿈, 정답은 시각별).
**SIM 입력의 상한 성능(`input_provenance.is_upper_bound = true`)이다. 제품 성능이나 G2 결론으로 쓰지 않는다.** G2 는 팀원이 단 ID 정답으로 실제 녹화에서 판정한다(§15.1).

| 시나리오/시드 | ID 유지 비율 | (tick, 사람) | ID 바뀜 | 같은 사람 두 ID 동시 확정 | 두 사람 한 ID | ID 재사용 | 발급 ID | ID 교정 | 섞인 클러스터 갱신 | 판정 불가 갱신 | IDENTITY_UNCERTAIN (tick, Track) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| crossing/1 | 1.000 | 138 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| crossing/2 | 1.000 | 138 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| crossing/3 | 1.000 | 138 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| side_by_side/1 | 1.000 | 168 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 46 |
| side_by_side/2 | 1.000 | 168 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 56 |
| side_by_side/3 | 1.000 | 168 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 58 |
| occlusion/1 | 1.000 | 125 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| occlusion/2 | 1.000 | 125 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| occlusion/3 | 1.000 | 125 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| exit_reentry/1 | 1.000 | 122 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| exit_reentry/2 | 1.000 | 122 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| exit_reentry/3 | 1.000 | 122 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| exit_long/1 | 0.863 | 102 | 1 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 |
| exit_long/2 | 0.863 | 102 | 1 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 |
| exit_long/3 | 0.863 | 102 | 1 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 |
| id_switch/1 | 1.000 | 138 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| id_switch/2 | 1.000 | 138 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| id_switch/3 | 1.000 | 138 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |

- ID 유지 비율: 2대 이상에 보인 (tick, 사람) 중 그 사람에게 붙은 World ID 가 그 사람의 주 ID 인 비율(붙은 ID 가 없는 tick 은 실패). 같은 사람 두 ID 동시 확정: 같은 사람에게 CONFIRMED·OBSERVED Track 이 둘 이상인 (tick, 사람). 두 사람 한 ID: 두 사람 이상에게 붙은 ID(클러스터 view 다수 기준)였던 적이 있는 World ID. 섞인 클러스터 갱신: 붙은 클러스터에 다른 사람 view 가 섞인 (tick, Track)(Cross-view 단계의 섞임). 판정 불가 갱신: 클러스터 view 의 다수 사람이 없는(동수) (tick, Track). 그 tick 은 누구에게 붙었는지 세지 않는다(이름 순서로 다수를 정하지 않음). id_switch 의 정답은 view 를 보간한 프레임 중 tick 에 가장 가까운 프레임의 촬영 시각으로 붙인다. tick 시각으로 붙이면 바꾼 직후 tick(바꾸기 전 프레임 −2 ms 와 뒤 프레임 +48 ms 의 보간)이 섞임으로 잘못 세어졌다(이전 측정의 id_switch/2 2건). ID 재사용: LOST 뒤 다시 나온 ID + 중복 발급.
- exit_long 의 유지 비율이 낮은 것은 소실 시간을 넘긴 재등장에 새 ID 를 주는 지금 동작 그대로다. 늦은 재연결(-40)이 다룰 경우다.
- IDENTITY_UNCERTAIN 은 (tick, Track) 수다(Track 2개 × tick 70~85). side_by_side 는 Cross-view 가 IDENTITY_UNCERTAIN 으로 표시한 클러스터(`CROSS_VIEW_UNCERTAIN`, 1 s 유지)와 2대 클러스터의 관절 경쟁 후보(`TWO_VIEW_COMPETITOR_JOINT`)에서 온다.
- occlusion·exit_reentry 의 IDENTITY_UNCERTAIN 은 가려진 east 의 Track(예측 위치 고정)을 west 가 지나갈 때 비용이 겹친 `AMBIGUOUS_TRACK_MATCH` 다. 가려진 사람 옆을 지나는 사람의 신원을 불확실로 두는 것은 의도한 보수적 동작이다.

참고(시험 밖): Local Tracker ID 조각남(sim_cross_view.py 의 fragmented, 1.5 s 마다 새 로컬 ID)으로 crossing·side_by_side 를 돌리면 유지 비율 crossing 1.000, side_by_side 0.988~0.994, 발급 ID 2, 같은 사람 두 ID 동시 확정·두 사람 한 ID 0건, IDENTITY_UNCERTAIN (tick, Track) 91~134 였다.

**시험 범위.** `test_world_track_sim.py` 는 시드 1 의 여섯 시나리오를 돌려 ID 재사용·같은 사람 두 ID 동시 확정·두 사람 한 ID·ID 교정 0건, world_state 스키마 오류 0건, 섞인 클러스터 갱신·판정 불가 갱신 0건, 유지 비율 회귀 하한(0.95)을 본다(약 40 s). 시드 2·3 은 측정 스크립트로 잰다.

## 남은 일

- 계약: `world_state` 1.1.0(마이너, Lead 승인)으로 ① `payload.input_provenance` 선택 필드, ② `position_xy` 설명에 "관측이 없을 때 t_last_valid 위치 유지, σ 를 2 m/s × 공백만큼 확대, 이중 외삽 금지"를 넣었다. 남은 제안: ID 교정 이력(별칭·`IdentityRevision`)을 I1 로 보낼 형식(-40 과 함께), `head_z_validity`.
- 늦은 재연결·ID 병합·교정·분할 이력 본격 구현(S15P31A101-40): LOST 뒤 재등장 연결, `POSSIBLE_DUPLICATE` 쌍의 병합(로컬 ID 전환과 구별할 근거, 예: 여러 카메라의 연결 이력·함께 갱신된 기간), 잘못 합친 ID 분할, 리플레이 교정.
- 생명주기·게이트·여유 수치는 명세에 없어 초기값이다. G2·A0 녹화로 조정한다(특히 소실 3 s, 게이트 0.5~1.5 m, 여유 0.3).
- ContactEstimator 가 생기면 `SupportContact` 로 접지·지지 표면을 싣는다. 고소 위치에서 가려진 사람의 "마지막 유효 높이 유지"(§5.2)는 그때 Z 쪽 상태와 함께 정한다.
- 처리 시간: 클러스터마다 관절 게이트가 삼각측량을 한 번 더 한다(Cross-view 의 클러스터 삼각측량과 중복). 성능 작업(-168)과 함께 게이트가 클러스터 결과를 재사용하는 공개 함수를 검토한다.
- Cross-view 설계 문서의 "붙어 걷는 두 사람의 구성 흔들림"은 World Track 의 연결 이력·1:1 배정으로 SIM 에서 ID 바뀜 없이 흡수됐다. 실제 잡음에서 다시 본다.
