# Tier 0 World Track Manager 설계

S15P31A101-37(기본 추적), S15P31A101-40(늦은 재연결·ID 교정). 기획서 §5.2(추적 3계층, 계산 순서 5단계, World 위치의 정의, ID 교정 기록), §6.1(시간 모델), §6.2(World Track 상태), §6.4(재개), §15.1 G2(ID 유지 시간 비율)·G3(ID 교정 후 진행 중 위험 자동 해소 0건), 부록 D-G2-04·D-G3-10 기준이다.
코드는 `ai/src/simai_ai/tracking/world_track.py`(World ID·연결·칼만 필터·생명주기·늦은 재연결·ID 교정), `tracking/world_state.py`(I1 `world_state` 조립)이고, 시험은 `ai/tests/tracking/` 의 `test_world_track.py`, `test_world_track_reconnect.py`(늦은 재연결·교정·G3), `test_world_track_reuse.py`(-168 결과 다시 쓰기 동일성), `test_world_track_sim.py`, 측정 스크립트는 `sim_world_track.py`, `bench_world_track.py` 다.

## 범위

| 단계 | 이 문서 | 다른 티켓 |
|---|---|---|
| 계산 순서 1~3 (보간, 연결 후보, 후보별 삼각측량) | 입력으로 받는다(`AssociationFrame`) | Cross-view(S15P31A101-34), 삼각측량·관절 게이트(-26·-29) |
| 계산 순서 4 (ContactEstimator) | 결과를 받을 자리(`SupportContact`)만. 없으면 모름 | ContactEstimator |
| 계산 순서 5 (최종 연결과 World Track 갱신) | World ID 발급, 클러스터 → World Track 1:1 연결, 칼만 필터, 생명주기, `world_state` 조립 | |
| ID 교정 기록·늦은 재연결 | 소실 후보를 같은 ID 로 잇기, 병합(`IdentityRevision` MERGE, `identity_revision`·별칭), Risk 전달(`to_correction`) (S15P31A101-40) | 분할·수동 교정, I1 교정 이력 형식(남은 일) |

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
7. **같은 사람 두 ID 는 그 시각에는 합치지 않고 표시만 한다.** 붙인 클러스터에 이번 시각 갱신되지 않은 다른 Track 의 로컬 트랙이 있고 그 Track 이 0.35 m 안이거나, 새 ID 가 다른 Track 0.35 m 안에 생기면 그 쌍을 후보로 둔다. 후보 쌍은 둘 다 살아 있고 0.35 m 안인 동안 매 시각 둘 다 IDENTITY_UNCERTAIN(`POSSIBLE_DUPLICATE`)을 받는다. 처음 구현은 이 경우 먼저 만든 ID 로 자동 병합했는데, 로컬 트래커 ID 전환 한 번(옆 사람 ID 가 넘어감)과 근접만으로 서로 다른 두 사람이 한 ID 로 합쳐졌다(리뷰 F1, `test_local_id_switch_does_not_merge_two_people`). 한 시각만으로는 둘을 구별할 근거가 없으므로 그 시각에는 합치지 않는다. 근거가 쌓이면 아래 "늦은 재연결과 ID 교정"의 병합으로 합친다(-40). 0.35 m 는 같은 사람이 갈라진 두 클러스터 골반(거의 같은 곳)과 서로 다른 두 사람 골반(어깨 폭 약 0.45 m 보다 가깝기 어려움)을 가르는 값이다. 0.5 m 로 두면 나란히 걷는 두 사람(0.5 m 간격)이 계속 표시된다.

같은 카메라의 두 검출은 Cross-view 클러스터에서 이미 갈라져 있고(카메라당 view 하나), Track 하나에 시각마다 클러스터 하나만 붙이므로 한 World ID 에 같은 카메라의 두 검출이 동시에 묶이지 않는다(D-G2-04).

## 늦은 재연결과 ID 교정 (S15P31A101-40)

§5.2 "늦은 재연결(late re-association), ID 교정", "World ID를 조용히 덮어쓰지 않는다 … 진행 중인 위험을 자동 해소하지 않는다", "매칭이 애매하면 합치지 않고 새 ID를 만들거나 IDENTITY_UNCERTAIN" 을 구현한다.

**근거와 그 한계.** 근거는 로컬 트랙 연결 이력(카메라 안 로컬 트래커가 이어 온 트랙 ID)과 위치뿐이다. 외형·얼굴·재식별은 쓰지 않는다(AGENTS.md 규칙 8). 로컬 트랙 ID 는 다시 발급되지 않지만(`local_tracker`, `{camera_id}-t{n}`), 로컬 트래커가 같은 ID 를 다른 사람 검출에 붙이는 ID 전환은 막지 못한다. 그래서 **카메라 한 대의 옛 로컬 트랙은 같은 사람의 근거가 아니다.** 서로 다른 카메라 2대(`reconnect_min_cameras`, 2 미만 설정 거부)의 로컬 트래커가 같은 옛 Track 의 로컬 트랙을 이어 왔을 때만 근거로 센다. 위치만으로는 잇지 않는다. 2대 기준은 한 카메라의 우연한 ID 전환은 막지만 **독립 근거는 아니다**: 두 카메라의 오류는 같은 3D 상황(사라진 자리에 다른 사람이 나타남)에서 함께 나올 수 있다(아래 잔여 위험).

처음 구현은 옛 로컬 트랙 1대 + 위치로 LOST 신원을 복원했다. 리뷰(F1)에서 A 가 가려진 동안 cam1 로컬 트래커가 A 의 옛 ID 를 다른 사람 B 에게 붙이면 B 가 A 로 복원되고, B 가 구역 밖이므로 A 의 R1 경보가 해소(AlertClear)되는 것이 재현됐다. 잘못 이은 신원은 Risk 의 해소 규칙을 "유효 관측"으로 통과시킨다. 그래서 근거 기준을 올리고, 근거가 모자란 새 ID 는 IDENTITY_UNCERTAIN 으로 두어 해소를 막는다(`test_switched_local_id_does_not_restore_lost_identity`, `test_switched_local_id_keeps_alarm_of_hidden_person`).

**소실 후보.** CONFIRMED 였다가 LOST 가 된 Track 은 LOST 를 한 번 낸 뒤 마지막 측정부터 `reconnect_window_ms`(10 s, §6.4 재개 시간 초기값과 같게 둠) 동안 소실 후보로 남는다. world_state 에는 내지 않는다. 로컬 트랙 연결 이력은 살아 있는 Track·소실 후보 모두 이 시간까지 남기고, 위치 비용 보너스·주인 판단에는 지금처럼 `key_ttl_ms`(2 s) 안의 것만 쓴다. 1대 클러스터가 그 로컬 트랙을 계속 보이면 이력이 이어진다(5단계). TENTATIVE 에서 LOST 가 된 Track 은 후보로 두지 않는다.

**같은 ID 로 잇기.** 새 Track 이 될 클러스터(6단계, 골반 측정 있음)의 옛 로컬 트랙이 모두 한 소실·가림 Track X 의 것이면 새 ID 대신 X 에 붙인다. 아래를 모두 채워야 한다.

| 조건 | 이유 |
|---|---|
| X 의 옛 로컬 트랙이 서로 다른 카메라 2대 이상 | 한 대는 로컬 ID 전환과 구별할 수 없다 |
| Cross-view 가 IDENTITY_UNCERTAIN 으로 표시하지 않은 클러스터 | 연결 자체가 불확실하다 |
| 이번 시각 갱신된 다른 Track 의 로컬 트랙이 섞이지 않음 | 갈라진 나머지 view·로컬 ID 전환(-37 4단계 동작 그대로) |
| 다른 소실·가림 Track 의 로컬 트랙이 섞이지 않음 | 어느 쪽인지 모른다 |
| X 의 다른 로컬 트랙이 이번 시각 다른 클러스터에 없음 | X 는 다른 곳에 보이고 있다 |
| 기준점이 X 의 마지막 위치에서 0.5 m + 2 m/s × (이 관측 시각 − X 의 마지막 관측) 안. 병합은 Y 의 위치 측정 전부(첫 관측부터)와 이번 시각 기준점이 모두 이 안이어야 한다 | 사람이 그 시간에 갈 수 있는 거리(§6.1 속도 상한). 병합을 지금 시각으로만 재면 Y 가 제자리에 있어도 시간이 지나 통과한다(리뷰 F4) |
| X 의 마지막 위치 1.5 m(`reconnect_crowd_radius_m`) 안에 다른 소실·가림 Track 없음 | 가까이 있던 두 사람이 함께 사라지면(교차) 로컬 트랙이 서로 넘어갔을 수 있다 |

소실 후보는 이번 시각 골반 측정이 있을 때만 살리고 관절 게이트 이력을 새로 시작한다. ID 가 바뀌지 않으므로 `identity_revision` 은 그대로이고 `WorldTrackFrame.reassociations` 에 남는다.

**근거가 모자랄 때.** 새 ID 를 만들고, 그 Track 이 갱신되지 않은 Track X 에게서 넘겨받은 옛 로컬 트랙을 `origin_of` 에 남긴다. X 가 그 뒤 따로 갱신되지 않고 남아 있는 동안(소실 후보 포함, 최대 마지막 관측 + 10 s) 새 Track 에 IDENTITY_UNCERTAIN(`REASSOCIATION_AMBIGUOUS`)을 매 시각 붙인다. X 가 다른 곳에서 갱신되거나, 병합 금지 쌍이 되거나, 소실 후보가 끝나면 그 출처를 지운다. X 의 위험 이벤트는 X 가 관측되지 않으므로 UNVERIFIABLE 로 남고 해소되지 않는다.

**병합(ID 교정).** 이미 새 ID Y 를 받은 뒤 Y 에 붙은 클러스터에 갱신되지 않은 Track X 의 옛 로컬 트랙(지금 X 가 들고 있거나 Y 가 X 에게서 넘겨받은 것)이 2대 이상이면 근거로 센다(위 조건 + X·Y 가 병합 금지 쌍이 아님). 1대뿐이면 세지도 버리지도 않는다(넘겨받은 것을 기억하므로 나중에 두 번째 카메라가 되살아나면 함께 센다). 근거 시각이 `merge_confirm_frames`(3) 번 쌓이면 합친다. 이어서 세기 전에 직전 근거가 2 s(`key_ttl_ms`) 안인지 먼저 보고(리뷰 F3: 돌아온 시각에 만료를 건너뛰던 문제), 넘었거나 반대 근거(위 조건 위반, X 가 따로 갱신됨)가 나오면 처음부터 센다.

- 남는 ID 는 먼저 만든 ID 다(늦은 재연결이면 옛 ID 로 돌아간다). 위치·필터는 이번 시각 측정을 받은 Track 의 것을 잇고, 관절 게이트 이력은 지운다(다른 사람 것일 수 있다).
- 남는 ID 의 `identity_revision` = 두 ID 중 큰 값 + 1, 사라지는 ID 는 별칭(`aliases`)에 더한다. `IdentityRevision(t, MERGE, world_id=남는 ID, previous_world_id=사라지는 ID, identity_revision, reason, evidence=근거 로컬 트랙)` 을 `WorldTrackFrame.revisions` 와 `WorldTrackManager.revisions` 에 추가만 한다(덮어쓰지 않음). reason 은 X 가 소실 후보였으면 `LATE_REASSOCIATION`, 가림 중이었으면 `DUPLICATE_MERGE`(-37 의 POSSIBLE_DUPLICATE 쌍이 다시 한 클러스터로 묶인 경우 포함). 연쇄 병합은 revision 이 계속 오르고 별칭이 쌓인다.
- 두 Track 이 같은 소실 Track 의 서로 다른 옛 로컬 트랙을 동시에 가져가면 어느 쪽도 합치지 않는다.
- **도달 거리 기준 시각(리뷰 F4).** 병합 근거의 도달 거리는 Y 의 위치 측정 이력(`reconnect_window_ms` 까지 남김) 중 X 의 마지막 관측 뒤의 것 전부와 이번 시각 기준점으로 잰다. 처음 구현은 매 시각 "지금 − X 의 마지막 관측" 으로 다시 재서, A 가 사라지고 0.1 s 뒤 1.2 m·3.6 m 떨어진 곳에 나타난 B 가 제자리에 있어도 몇 시각 뒤 A 로 병합되고 A 의 경보가 해소됐다. 이제 Y 가 한 번이라도 갈 수 없는 곳에서 관측됐으면 X·Y 를 병합 금지 쌍으로 두고, 같은 ID 로 잇기를 거리 때문에 거절하고 새로 만든 Track 도 그 X 와 병합 금지 쌍이 된다. 시험: 리뷰 장면 두 개(경보 유지 포함), 경계(첫 관측 2.49 m 병합 / 2.51 m 거절, 공백 1 s), 천천히 걸어가는 Y 의 정상 병합, 여러 가림 Track 중 갈 수 있는 쪽만 병합.
- 사라지는 ID 는 그 시각부터 world_state 에 없다. 계약에 별칭 필드가 없어 I1 로는 `identity_revision` 증가만 보인다(아래 남은 일).

**병합 금지 쌍.** 같은 시각 같은 카메라의 서로 다른 두 검출은 한 사람일 수 없다. 검출 하나가 가리키는 Track 은 그 로컬 트랙의 주인(이번 시각 이전, 소실 후보 포함)과 그 클러스터를 받은 Track 이고, 같은 카메라의 다른 검출이 가리키는 Track 들과 병합 금지 쌍(`_distinct`)이 된다. 위치 갱신과 상관없이 병합 판단 전에 기록한다. 처음 구현은 위치가 갱신된 Track 의 클러스터로만 기록해, B 가 1대 클러스터로만(위치 갱신 없이) A 와 같은 카메라에 보인 반증을 놓치고 뒤에 B→A 를 병합했다(리뷰 F2). 같은 검출이 가리키는 두 Track(주인과 받은 Track)은 짝짓지 않는다(그것이 병합 근거다). 병합하면 사라지는 ID 의 쌍은 남는 ID 로 이어진다. Cross-view 가 한 사람을 (cam1, cam2)·(cam3, cam4) 로 가른 경우는 같은 카메라를 쓰지 않으므로 다시 한 클러스터로 묶이면 합친다. -37 리뷰 F1(로컬 ID 전환 한 번으로 두 사람 병합)·SIM id_switch 도 여기서 막힌다.

**일관성 확인.** 잇거나 합친 Track 은 자기 로컬 트랙만으로(2대 이상) 위치가 갱신된 시각이 `merge_confirm_frames`(3) 번 쌓일 때까지 IDENTITY_UNCERTAIN(`LATE_REASSOCIATION`)을 매 시각 다시 붙인다. 가려져 갱신이 없으면 시간이 지나도 풀지 않는다(처음 구현의 "1 s 뒤 자동 해제"를 없앴다, 리뷰 F1). 확인을 마친 뒤에만 보통의 1 s 유지(깜박임 방지)가 지나 풀린다.

**잔여 위험과 운영 영향.**
- 두 카메라의 로컬 트래커가 같은 상황에서 함께 사라진 사람의 옛 ID 를 다른 사람에게 붙이면 2대 기준을 채워 그 사람이 옛 ID 로 이어지고, 옛 사람의 진행 중 경보가 새 사람의 관측으로 해소될 수 있다(리뷰 S4 재현, `test_residual_risk_two_camera_simultaneous_switch_is_reconnected` 가 지금 동작을 고정). 기본 추적기 track_buffer(30 프레임, 약 3 s)를 World LOST(3 s) 넘게 두 카메라에서 함께 유지하고 둘 다 같은 다른 사람에게 붙여야 하므로 드물다. 후속 후보(Risk 쪽 별도 티켓): 늦은 재연결·병합 전에 발생한 이벤트는 교정 뒤 관측만으로 해소하지 않고 운영자 확인을 요구한다.
- 일관성 확인은 클러스터의 로컬 트랙이 모두 자기 것일 때만 센다. 로컬 키가 계속 조각나면 확인이 끝나지 않아 그 사람 이벤트의 해소가 늦어진다(안전 쪽, 리뷰 R2). 새 위험 발생은 막지 않는다.
- `REASSOCIATION_AMBIGUOUS` 는 옛 주인의 소실 후보가 끝날 때까지(최대 약 10 s + 유지 1 s) 남아, 그 새 ID 의 정상 해소가 최대 약 6~11 s 늦어진다(리뷰 R3). 신규 발생·현장 경보는 늦지 않는다(R1 은 IDENTITY_UNCERTAIN 을 확인 필요 사유로만 더하고 HIGH 는 LOCAL_ALERT).

**Risk 전달(G3, D-G3-10).** Risk Engine 에는 이미 `LifecycleTracker.correct_identity(IdentityCorrection)` 이 있다(사라지는 ID 의 활성 이벤트를 남는 ID 로 다시 키를 달고 origin_world_ids 에 원본을 남기며, 같은 키의 이벤트가 이미 있으면 related_event_id 로 잇기만 한다. 어느 쪽도 해소하지 않는다). `IdentityRevision.to_correction()` 이 그 입력을 만든다. Tier 0 조립부는 시각마다 `WorldTrackFrame.revisions` 를 그 시각 판정의 `LifecycleTracker.apply` **전에** 넘긴다. 순서를 지키면 새 ID 로 들어오는 같은 규칙 판정이 같은 이벤트를 갱신한다(중복 이벤트 없음). `risk/lifecycle.py` 는 고치지 않았다. 저널에는 `correct_identity` 가 낸 전이(IDENTITY_CORRECTED·IDENTITY_CONFLICT)가 다른 전이와 같이 기록되고, 교정 기록 자체는 `IdentityRevision.to_payload()` 로 저널 `append` 에 넣을 수 있다(조립부 몫).

시험(`test_world_track_reconnect.py`): 짧은 가림 뒤 같은 ID, 두 카메라 근거로 LOST 뒤 같은 ID, 다시 이은 뒤 가려지면 불확실 유지, 1대 근거는 잇지 않음(두 번째 카메라가 되살아나면 이음), 리뷰 F1 장면(B 가 A 로 복원되지 않음, A 소실 후보 동안 B 불확실), 근거 없는 재등장·거리 밖은 새 ID, 10 s 창 경계(9999/10000/10001 ms), crowd 경계(1.499/1.501 m), 교차 뒤 함께 사라진 두 사람, 두 소실 Track 의 로컬 트랙이 섞인 클러스터, 긴 소실 뒤 새 ID → 3시각 근거 뒤 교정(기록·별칭·world_state 스키마), 1대 되살림은 병합 안 함·불확실 유지, 근거 TTL 경계(1999/2000/2001/2100 ms), 짧은 빠짐 허용·반대 근거 재시작, 갈라진 한 사람 병합, 연쇄 병합, 두 Track 의 경쟁, 병합 금지 쌍(함께 보인 두 사람, 1대 클러스터 검출, 가림 중 검출, 병합으로 이어짐, 로컬 ID 전환), G3: 리뷰 F1 Risk 장면(숨은 A 의 경보 유지, AlertClear 없음), 교정 후 같은 규칙 이벤트 유지·연결, 이벤트 다시 키 달기, 같은 ID 재연결 이벤트 유지. 리뷰 장면 시험은 각 수정(카메라 수 기준, 병합 금지 기록, TTL 검사)을 빼면 실패하는 것을 확인했다.

## 재시작 뒤 복원 Track 매칭 (S15P31A101-54)

Jira -54(기획서 §3.2 재시작 복구, 부록 C "Track 체크포인트 복원·재관측 매칭"): 복원 Track 은 OBSERVED 가 아니고, 30초 넘은 체크포인트에서는 Track 을 복원하지 않으며, 새 World ID 를 주기 전에 복원 Track 과 먼저 매칭하고 애매하면 `related_event_id` 를 단다. 완료 조건은 재시작 때 같은 위험의 이벤트 중복 생성·활성 이벤트 자동 해소·복원 위치의 OBSERVED 처리 0건(G3)이다. 명세 §3.2 본문은 "MVP 는 World Track 을 체크포인트하지 않는다"라 Jira 와 다르다(정리는 -172). 여기서는 Jira 를 따르되 복원 Track 을 **재관측과 맞출 후보로만** 쓴다.

**근거 판단.** 늦은 재연결(-40)의 근거는 서로 다른 카메라 2대의 로컬 트래커가 이어 온 옛 로컬 트랙이고, 위치만으로는 잇지 않는다(사라진 자리에 다른 사람이 나타난 경우와 구별할 수 없고, 잘못 이은 신원은 옛 사람의 경보를 새 사람의 관측으로 해소시킨다, -40 리뷰 F1). 재시작하면 로컬 트래커가 새로 시작해 옛 로컬 트랙이 없다(체크포인트에도 로컬 트랙을 넣지 않는다. 재시작 뒤 같은 번호가 다른 사람에게 다시 나올 수 있다). 남는 근거는 마지막 위치·시각뿐이라 -40 기준으로는 **같은 ID 로 이을 근거가 늘 모자란다.** 그래서 재관측된 사람은 늘 새 ID 를 받고(복원 ID·revision·별칭을 물려받지 않음), 복원 이벤트도 새 ID 로 옮기지 않는다(`correct_identity` 를 쓰지 않음). -40 규칙의 재사용은 이렇다.

| -40 규칙 | 재시작 뒤 |
|---|---|
| 로컬 ID 하나와 위치만으로 잇지 않음 | 위치만 있으므로 잇지 않는다(후보·연결만) |
| 카메라 2대 근거 | 재시작 뒤에는 없다. 그래서 잇는 경우가 없다 |
| 병합 금지 반증 | 잇지 않으므로 병합 자체가 없다(더 강함) |
| 도달 거리를 첫 관측부터 잼 | 후보 판정에 그대로 쓴다: 첫 위치 측정이 0.5 m + 2 m/s × (첫 관측 − 복원 Track 의 t_last_valid) 밖이면 그 사람이 아니다(`restored_excluded`). 첫 관측으로만 재므로 기다린다고 후보가 되지 않는다 |
| 근거가 모자라면 새 ID + IDENTITY_UNCERTAIN | 후보가 있는 새 ID 에 IDENTITY_UNCERTAIN(`RESTORED_TRACK_CANDIDATE`)을 그 복원 Track 이 남아 있는 동안 붙인다(-40 의 `REASSOCIATION_AMBIGUOUS` 와 같은 방식) |

**구현.** `WorldTrackManager.restore(tracks, tracks_t)` 를 첫 update 전에 부른다.

- 30초 규칙(`restore_max_age_ms` 30 000, 부록 C 초기값): 첫 프레임 시각 − `tracks_t` > 30 s 면 버린다(`RestoreStatus` DISCARDED, 이유). 첫 프레임이 `tracks_t` 보다 앞(시각 역행)이거나 Track 이 있는데 `tracks_t` 를 모르면 버린다(모르면 비운다). 받은 복원 Track 도 마지막 관측부터 30 s 가 지나면 후보에서 뺀다(가림 중이던 Track 은 더 일찍 빠진다). 나이는 촬영 시각(체크포인트 `tracks_t` 와 재시작 뒤 첫 프레임)으로 잰다. 벽시계 `written_at` 은 쓰지 않는다.
- 복원 Track 은 `_tracks`·`_dormant`·로컬 트랙 이력·연결 비용·칼만 필터·world_state·export_tracks 어디에도 넣지 않는다. 그래서 복원 위치가 OBSERVED 로 나가는 경로가 없다(1대 검출로 복원 위치를 채우지도 않는다). 이번 세션 접두어의 World ID 는 받지 않는다.
- 새 Track 이 생길 때(6단계) 첫 골반 측정으로 후보(`restored_candidates`, 가까운 순)와 제외(`restored_excluded`)를 정하고 바꾸지 않는다. 재시작 뒤 생긴 두 ID 가 -40 근거로 병합되면 둘을 합친다(후보가 우선).
- 재시작 직후에는 공백이 길어 도달 거리가 커서(5 s 면 10.5 m) 교실의 대부분 사람이 후보가 된다. 재시작 뒤 길어야 30 s 동안 그 사람들의 해소가 늦어지는 것은 의도한 보수적 동작이다(재관측 전 System Health 도 UNSAFE_UNKNOWN). 새 위험 발생과 현장 경보는 늦지 않는다.

**이벤트 연결(`recovery.relink.RestoredEventLinker`).** `LifecycleTracker.apply(..., related=linker.for_frame(frame, manager.restore_status))` 로 새 이벤트(RAISED)에 `related_event_id` 를 단다. 새 이벤트(규칙 X, 대상 N)와 복원 이벤트(규칙 X, 대상 R)를 ① R 이 N 의 후보면(여럿이면 가까운 R), ② N 이 처음 보일 때 R 의 복원 Track 이 없었으면(체크포인트에 없음·30 s 넘음·이미 빠짐) 같은 구역일 때 잇는다. ③ R 이 N 의 제외면 잇지 않는다. 연결은 재시작 뒤 첫 프레임부터 30 s 까지다. 구역 대상(R7) 이벤트는 같은 키라 복원 이벤트가 그대로 갱신되므로 다루지 않는다. 계약 `risk_event_transition.related_event_id` 설명("재시작 복구 중 복원 Track 매칭이 애매해 새 이벤트를 만들었을 때 이전 이벤트")과 `risk_event.related_event_id` 를 그대로 쓴다.

**중복 이벤트의 뜻.** 위 판단대로 재관측된 사람은 새 ID 이므로 같은 위험은 새 이벤트가 되고 연결로 남는다(§3.2 "새 이벤트가 생기면 이전 이벤트와의 연결을 남긴다", D-G3-09 "재발생 이벤트의 related_event_id 누락 0건"). 이전 이벤트를 이어받는 방법(복원 이벤트를 새 ID 로 다시 키 달기)은 쓰지 않았다. 이어받으면 새 ID 의 관측으로 이전 이벤트가 해소될 수 있고, 그 새 ID 가 다른 사람이면 경보가 잘못 꺼진다(시험의 음성 대조가 재현한다).

**lifecycle 수정(최소).** `LifecycleTracker.apply` 에 `related` 키워드(기본 None, `RelatedEventFn = (EventKey, Finding) → event_id | None`)를 더하고 `_raise` 가 그 값을 `related_event_id` 로 싣는다. 리뷰 반영: ① 함수는 상태를 바꾸기 전에 활성 이벤트가 없는 키의 발생 판정마다 미리 부르고, 예외·잘못된 값(빈 문자열·문자열 아님)은 연결 없음으로 낮추고 로그를 남긴다(M2: 처음에는 `_raise` 안에서 검사해 예외를 냈고, 같은 tick 의 앞 키 전이·현장 경보가 반영만 되고 반환되지 않아 사라졌다). ② 지속 조건을 세는 동안 키별로 처음 얻은 연결을 기억했다가 발생할 때 싣고, 대기가 이벤트 없이 끝나면 버린다(M1: 첫 위험 프레임이 연결 시간 안이어도 지속 조건을 채운 다음 프레임이 창 밖이면 연결이 사라졌다). 기존 API(`restore`, `correct_identity`)로는 새 이벤트를 만들 때 연결을 달 수 없다(`correct_identity` 의 충돌 경로는 옛 이벤트 쪽에 새 이벤트를 다는 방향이고 키를 바꾸는 경로는 이어받기다). RAISED 전이에 처음부터 싣는 것이 전이를 하나 더 만들지 않고 저널·EC2 에 같은 값이 남는 가장 작은 변경이다. 재개(같은 event_id)와 갱신에는 쓰지 않는다.

**시험.** `tests/tracking/test_world_track_restore.py`(후보·제외·30 s 경계 29 900/30 000/30 100 ms·Track 별 만료·시각 역행·도달 거리 경계 0.89/0.91 m·두 복원 Track 경쟁·끝내 재관측 안 됨·병합 후 합치기·1대 검출로 복원 위치를 채우지 않음·world_state 계약), `tests/recovery/test_restore_track_match.py`(재시작 직후 같은 위험 재발생의 연결·계약 검증, 같은 자리에 다른 사람이 60 s 보여도 이전 경보 유지와 위치로 이었을 때 해소되는 음성 대조, 29.9/30.1 s 체크포인트의 연결, 연결 시간 닫힘, 갈 수 없던 사람은 같은 구역이어도 안 이음, 두 복원 Track 경쟁, 끝내 재관측 안 됨, 실제 저널·체크포인트·recover 를 거친 끝까지 시험과 outbox 메시지 계약). 30초 규칙·후보 표시·연결 훅·첫 관측 도달 거리·제외 규칙·Track 별 만료를 하나씩 되돌리면 해당 시험이 실패하는 것을 확인했다.

## 처리 시간 (-168 연결)

관절 게이트에 Cross-view 클러스터의 왜곡 제거 결과와 삼각측량(`ViewCluster.undistorted`, `ViewCluster.skeleton`)과 다른 클러스터의 왜곡 제거 결과(`others_undistorted`, `others` 와 같은 카메라·같은 순서)를 넘긴다. 게이트는 입력이 같다고 확인될 때만(`UndistortedKeypoints.matches`, `SkeletonEstimate.reusable_for`) 다시 쓰므로 결과는 다시 계산한 것과 같다. `test_world_track_reuse.py` 가 bench 장면(광각 왜곡, 3·10명, 8 tick)에서 다시 쓰기를 켠 것과 끈 것의 `WorldTrackFrame` 전체(관절 게이트 결과의 모든 필드 포함)를 재귀로 비교하고(범주형 완전 일치, 실수 1e-9), 다시 쓰기 경로가 실제로 돌았는지 센다.

`uv run python tests/tracking/bench_world_track.py --people 1 5 10 --ticks 30` (ai/, 노트북 CPU Intel Core Ultra Family 6 Model 170, 22 논리 코어, Windows 11, Python 3.11.15, numpy 2.4.6). 장면은 bench_tier0_geometry.py 와 같다. SIM 합성 입력의 처리 시간이며 정확도 지표가 아니다.

| 사람 | Cross-view (참고) | World Track 갱신 (다시 쓰기) | World Track 갱신 (다시 계산) |
|---:|---:|---:|---:|
| 1 | 7.1 / 9.1 | 0.9 / 1.3 | 5.9 / 7.4 |
| 5 | 17.8 / 21.2 | 6.3 / 9.5 | 32.4 / 37.7 |
| 10 | 32.4 / 36.1 | **14.5 / 18.4** | 57.4 / 66.9 |

(ms, 평균 / p95, 리뷰 수정 뒤 재측정.) 10명 기준 Cross-view + World Track 갱신이 평균 약 47 ms 로 10 Hz(100 ms) 안이다. 병합 금지 쌍 기록·일관성 확인을 더한 뒤에도 늘지 않았다(같은 노트북에서 ±20% 흔들림). 운영 판정은 Pod CPU 측정으로 한다(§6.6).

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
| CONFIRMED·OCCLUDED → LOST | 측정 공백 3 s 초과. LOST 는 그 시각에 한 번만 내고 world_state 에서 뺀다. 마지막 측정부터 10 s 동안 소실 후보로 남아 로컬 트랙 근거가 있으면 같은 ID 로 잇는다(-40) |
| 소실 후보 → CONFIRMED | 늦은 재연결(아래). 근거 없이 다시 보이면 새 ID |

관측 상태는 생명주기와 따로 `world_state` 조립 때 gap = t_latest_global − t_last_valid 를 `time_model.classify_observation` 에 넣어 정한다(0~200 ms OBSERVED, ~500 ms PREDICTED, 넘으면 UNKNOWN). 그래서 TENTATIVE·PREDICTED, OCCLUDED·OBSERVED 같은 조합이 있을 수 있다.

**ID 확신(`identity_confidence`).** 사유(`IdentityFlag`)가 마지막으로 붙은 뒤 1 s 동안 IDENTITY_UNCERTAIN 을 유지한다(깜박임 방지, 보수적. Risk Engine 은 IDENTITY_UNCERTAIN 이면 해소를 확인하지 않는다). 그 밖에는 TENTATIVE → LOW, OCCLUDED·이번 시각 미갱신·2대로만 갱신·연결 이력 없이 다시 잡은 뒤 1 s → MEDIUM, 3대 이상으로 갱신된 CONFIRMED → HIGH.

| 사유 | 붙는 때 |
|---|---|
| `CROSS_VIEW_UNCERTAIN` | Cross-view IDENTITY_UNCERTAIN 클러스터를 붙였거나, 이 Track 이 다투던 그런 클러스터를 붙이지 않았다 |
| `AMBIGUOUS_TRACK_MATCH` | 연결 비용 차가 여유보다 작다 |
| `TWO_VIEW_COMPETITOR_JOINT` | 관절 게이트의 2대 관측 경쟁 후보가 2대 클러스터에 있거나 엉덩이 관절에 있다. 3대 이상이 맞춘 사람의 손목 하나가 경쟁 후보라고 신원 전체를 불확실로 두지 않는다(그 관절은 게이트가 이미 UNCERTAIN). 처음에는 관절 하나라도 경쟁 후보면 붙였는데, SIM 나란히 걷기에서 거의 모든 tick 이 IDENTITY_UNCERTAIN 이 되어 바꿨다 |
| `POSSIBLE_DUPLICATE` | 같은 사람 두 ID 후보 쌍(7)이 0.35 m 안에 있는 동안 매 시각 |
| `LATE_REASSOCIATION` | 소실·가림 Track 을 로컬 트랙 근거로 다시 이었거나 ID 를 병합했고 일관성 확인(자기 로컬 트랙 2대 이상으로 3시각 갱신)이 끝나지 않았다(-40). 시간만으로 풀지 않는다 |
| `REASSOCIATION_AMBIGUOUS` | 갱신되지 않은 Track 의 옛 로컬 트랙을 넘겨받았지만 근거가 모자라 잇지 않았다(-40). 그 Track 이 남아 있는 동안 매 시각 붙는다(돌아온 사람일 수 있다) |

## world_state 조립

- 필드는 계약 그대로다. `skeleton`·`pelvis_xyz`·`cameras` 는 이번 시각에 클러스터가 붙은 Track 만 채운다. 관절은 모두 observed 다. 클러스터가 붙었는데 골반 측정이 없는 시각(엉덩이가 가려짐)에는 관측 상태가 PREDICTED·UNKNOWN 이어도 이번 시각의 `skeleton`·`head_z`(VALID)·`cameras` 는 싣는다. 관절 관측 자체는 이번 시각의 사실이고, 위치(`position_xy`)만 측정이 없다는 뜻이다.
- 접지: ContactEstimator 가 아직 없어 `ground_anchor_xyz = null`, `support_surface_validity = UNKNOWN`, `contact_validity = UNAVAILABLE`, `z_validity = UNAVAILABLE` 이다. 결과가 생기면 `SupportContact` 로 넘긴다. `SupportContact` 는 지지 표면이 VALID 가 아니거나 z_validity 가 UNAVAILABLE 인데 접지점이 있으면 만들 때 거부한다(Z=0 대체 금지, 계약 불변 규칙).
- `head_z`: 게이트가 VALID·UNCERTAIN 으로 낸 값(관측 귀·눈 근거가 같은 Track skeleton 에서 확인될 때만)을 싣고 UNAVAILABLE 이면 null, 유효성은 `head_z_validity`(world_state 2.0.0, 사용자 결정 2026-10-19, tier0-joint-gate.md).
- `input_provenance`: `WorldTrackManager` 에 넘긴 출처를 `payload.input_provenance`(계약 1.1.0 선택 필드, Lead 승인)로 싣는다. 넘기지 않으면 필드를 넣지 않는다. SIM·DATASET_GT 입력이면 `is_upper_bound = true`.
- 봉투 `schema_version` 은 world_state 가 마지막으로 바뀐 1.1.0, `msg_id`·`sent_at` 은 호출자가 넣는다(현재 시각을 읽지 않는다).

## SIM 측정 (상한, 결론 아님)

`uv run python tests/tracking/sim_world_track.py` (ai/). 파이프라인은 LocalTrackletBuffer → CrossViewAssociator → WorldTrackManager → world_state 조립, 10 Hz tick, 시드 1·2·3.
시나리오: crossing(내장), side_by_side(sim_cross_view.py 정의), occlusion(crossing 에서 east 를 2.0~3.2 s 동안 cam1 에만 보이게 하고, 가렸던 카메라는 가림 뒤 새 로컬 트랙 ID), exit_reentry(east 를 2.0~3.5 s 동안 모든 카메라에서 지우고 새 로컬 트랙 ID 로 재등장), exit_long(1.5~5.0 s, 소실 시간 3 s 초과), id_switch(crossing 에서 엇갈리는 3.0 s 부터 cam1 의 두 사람 로컬 트랙 ID 를 맞바꿈, 정답은 시각별), occlusion_long(-40: east 를 1.5~5.0 s 동안 cam1 에만 보이게 가림, 소실 시간을 넘겨 LOST. 돌아올 때 cam2 도 옛 로컬 트랙 ID 를 되살림), occlusion_long_single(-40: 같되 cam2 는 새 로컬 트랙 ID, 옛 로컬 트랙은 cam1 하나), exit_long_revive(-40: exit_long 과 같되 cam1·cam2 는 5.6 s 까지 가렸다가 옛 로컬 트랙 ID 로 되살림).
**SIM 입력의 상한 성능(`input_provenance.is_upper_bound = true`)이다. 제품 성능이나 G2 결론으로 쓰지 않는다.** G2 는 팀원이 단 ID 정답으로 실제 녹화에서 판정한다(§15.1).

| 시나리오/시드 | ID 유지 비율 | (tick, 사람) | ID 바뀜 | 같은 사람 두 ID 동시 확정 | 두 사람 한 ID | ID 재사용 | 늦은 재연결 | 발급 ID | ID 교정 | 섞인 클러스터 갱신 | 판정 불가 갱신 | IDENTITY_UNCERTAIN (tick, Track) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| crossing/1 | 1.000 | 138 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| crossing/2 | 1.000 | 138 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| crossing/3 | 1.000 | 138 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| side_by_side/1 | 1.000 | 168 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 46 |
| side_by_side/2 | 1.000 | 168 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 56 |
| side_by_side/3 | 1.000 | 168 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 58 |
| occlusion/1 | 1.000 | 125 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| occlusion/2 | 1.000 | 125 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| occlusion/3 | 1.000 | 125 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| exit_reentry/1 | 1.000 | 122 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| exit_reentry/2 | 1.000 | 122 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| exit_reentry/3 | 1.000 | 122 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 22 |
| exit_long/1 | 0.863 | 102 | 1 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 |
| exit_long/2 | 0.863 | 102 | 1 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 |
| exit_long/3 | 0.863 | 102 | 1 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 |
| id_switch/1 | 1.000 | 138 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| id_switch/2 | 1.000 | 138 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| id_switch/3 | 1.000 | 138 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |
| occlusion_long/1 | 1.000 | 102 | 0 | 0 | 0 | 0 | 1 | 2 | 0 | 0 | 0 | 13 |
| occlusion_long/2 | 1.000 | 102 | 0 | 0 | 0 | 0 | 1 | 2 | 0 | 0 | 0 | 13 |
| occlusion_long/3 | 1.000 | 103 | 0 | 0 | 0 | 0 | 1 | 2 | 0 | 0 | 0 | 13 |
| occlusion_long_single/1 | 0.863 | 102 | 1 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 19 |
| occlusion_long_single/2 | 0.863 | 102 | 1 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 19 |
| occlusion_long_single/3 | 0.854 | 103 | 1 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 19 |
| exit_long_revive/1 | 0.922 | 102 | 2 | 0 | 0 | 0 | 1 | 3 | 1 | 0 | 0 | 19 |
| exit_long_revive/2 | 0.922 | 102 | 2 | 0 | 0 | 0 | 1 | 3 | 1 | 0 | 0 | 19 |
| exit_long_revive/3 | 0.922 | 102 | 2 | 0 | 0 | 0 | 1 | 3 | 1 | 0 | 0 | 19 |

- ID 유지 비율: 2대 이상에 보인 (tick, 사람) 중 그 사람에게 붙은 World ID 가 그 사람의 주 ID 인 비율(붙은 ID 가 없는 tick 은 실패). 같은 사람 두 ID 동시 확정: 같은 사람에게 CONFIRMED·OBSERVED Track 이 둘 이상인 (tick, 사람). 두 사람 한 ID: 두 사람 이상에게 붙은 ID(클러스터 view 다수 기준)였던 적이 있는 World ID. 섞인 클러스터 갱신: 붙은 클러스터에 다른 사람 view 가 섞인 (tick, Track)(Cross-view 단계의 섞임). 판정 불가 갱신: 클러스터 view 의 다수 사람이 없는(동수) (tick, Track). 그 tick 은 누구에게 붙었는지 세지 않는다(이름 순서로 다수를 정하지 않음). id_switch 의 정답은 view 를 보간한 프레임 중 tick 에 가장 가까운 프레임의 촬영 시각으로 붙인다. tick 시각으로 붙이면 바꾼 직후 tick(바꾸기 전 프레임 −2 ms 와 뒤 프레임 +48 ms 의 보간)이 섞임으로 잘못 세어졌다(이전 측정의 id_switch/2 2건). ID 재사용: LOST 뒤 늦은 재연결·교정 기록 없이 다시 나온 ID + 중복 발급. 늦은 재연결: LOST 를 낸 뒤 같은 ID 로 다시 이은 수(`reassociations` 또는 남는 ID 로 교정).
- exit_long 은 소실 시간을 넘겨 모든 카메라에서 새 로컬 트랙 ID 로 돌아오므로 로컬 트랙 근거가 없다. 위치만으로 잇지 않으므로 새 ID 가 맞는 동작이다(-40 뒤에도 같음).
- occlusion_long 은 cam1 로컬 트랙이 가림 내내 이어지고 cam2 가 옛 ID 를 되살려 두 카메라 근거로, 4.5 s 쯤 LOST 를 낸 뒤 5.0 s 에 같은 ID 로 이었다(늦은 재연결 1, 유지 1.000). occlusion_long_single 은 옛 로컬 트랙이 cam1 하나라 잇지 않고 새 ID 를 줬고(발급 3, 유지 0.85~0.86), 새 ID 는 돌아온 뒤 끝까지 IDENTITY_UNCERTAIN(19)이다. exit_long_revive 는 5.0 s 에 새 ID 를 받았다가 5.6 s 에 cam1·cam2 옛 로컬 트랙이 되살아나 3시각 근거 뒤 옛 ID 로 교정됐다(ID 교정 1, ID 바뀜 2, 유지 0.922). 이 세 시나리오의 IDENTITY_UNCERTAIN 은 근거가 모자란 새 ID 의 `REASSOCIATION_AMBIGUOUS` 와 잇거나 합친 뒤 일관성 확인 중의 `LATE_REASSOCIATION` 이다.
- -40 뒤 기존 여섯 시나리오의 측정값은 그대로다(ID 교정 0, id_switch 의 두 사람은 같은 카메라에 함께 보여 병합 금지 쌍이다).
- IDENTITY_UNCERTAIN 은 (tick, Track) 수다(Track 2개 × tick 70~85). side_by_side 는 Cross-view 가 IDENTITY_UNCERTAIN 으로 표시한 클러스터(`CROSS_VIEW_UNCERTAIN`, 1 s 유지)와 2대 클러스터의 관절 경쟁 후보(`TWO_VIEW_COMPETITOR_JOINT`)에서 온다.
- occlusion·exit_reentry 의 IDENTITY_UNCERTAIN 은 가려진 east 의 Track(예측 위치 고정)을 west 가 지나갈 때 비용이 겹친 `AMBIGUOUS_TRACK_MATCH` 다. 가려진 사람 옆을 지나는 사람의 신원을 불확실로 두는 것은 의도한 보수적 동작이다.

참고(시험 밖): Local Tracker ID 조각남(sim_cross_view.py 의 fragmented, 1.5 s 마다 새 로컬 ID)으로 crossing·side_by_side 를 돌리면 유지 비율 crossing 1.000, side_by_side 0.988~0.994, 발급 ID 2, 같은 사람 두 ID 동시 확정·두 사람 한 ID 0건, IDENTITY_UNCERTAIN (tick, Track) 91~134 였다.

**시험 범위.** `test_world_track_sim.py` 는 시드 1 의 아홉 시나리오를 돌려 ID 재사용·같은 사람 두 ID 동시 확정·두 사람 한 ID 0건, ID 교정은 exit_long_revive 의 1건만, 늦은 재연결은 occlusion_long·exit_long_revive 의 1건씩(occlusion_long_single 은 0건·발급 3), world_state 스키마 오류 0건, 섞인 클러스터 갱신·판정 불가 갱신 0건, 유지 비율 회귀 하한(0.95), exit_long_revive 의 주 ID 가 처음 ID 인지를 본다. 시드 2·3 은 측정 스크립트로 잰다.

## 남은 일

- 계약: `world_state` 1.1.0(마이너, Lead 승인)으로 ① `payload.input_provenance` 선택 필드, ② `position_xy` 설명에 "관측이 없을 때 t_last_valid 위치 유지, σ 를 2 m/s × 공백만큼 확대, 이중 외삽 금지"를 넣었다. 남은 제안: `head_z_validity`, ID 교정 이력(아래).
- 계약(-40 결정): `world_state.world_track` 에 선택 필드 `aliases`(이 ID 로 합쳐진 이전 World ID 목록)를 넣는 것을 사용자가 승인했다. -169 머지 뒤 별도 계약 티켓에서 추가하고 world_state 조립이 `WorldTrackSnapshot.aliases` 를 싣는다. 이번 변경은 스키마를 고치지 않았다. 지금은 I1 로 `identity_revision` 증가만 보이고 사라진 ID 는 설명 없이 빠진다.
- 분할(SPLIT)·수동 교정(CORRECTION)은 별도 티켓이다: 잘못 합친 ID 를 다시 가르는 근거(예: 합친 뒤 같은 카메라의 두 검출로 동시에 보임)와, 분할 시 이벤트를 어느 ID 에 둘지 Risk 쪽 인터페이스(`IdentityCorrection` 은 병합 방향만 있다)가 필요하다. 리플레이 교정도 같이 정한다.
- Tier 0 조립부(-45 이후): 시각마다 `frame.revisions` 를 `LifecycleTracker.correct_identity(rev.to_correction())` 로 `apply` 전에 넘기고, `rev.to_payload()` 를 저널에 남긴다. Pod 재시작 체크포인트에는 살아 있는 Track 만 넣는다(-51, 소실 후보·교정 기록은 넣지 않음). 소실 후보였던 사람의 복원 이벤트는 위 재시작 연결의 ② (같은 규칙·구역)로만 이어진다.
- Risk `correct_identity` 의 충돌(IDENTITY_CONFLICT) 쪽 이벤트는 옛 ID 가 다시 관측되지 않으므로 UNVERIFIABLE 로 계속 남는다(자동 해소 없음, G3 쪽으로 안전). 운영자가 정리하는 흐름은 Risk·관제 화면 쪽 몫이다.
- 늦은 재연결 수치(`reconnect_window_ms` 10 s, `reconnect_crowd_radius_m` 1.5 m, `reconnect_min_cameras` 2, `merge_confirm_frames` 3)는 초기값이다. 카메라 4대 교실에서 2대 근거가 실제 로컬 트래커 재등장 동작으로 얼마나 자주 나오는지(늦은 재연결이 드물 수 있다)도 G2·A0 녹화로 본다. G2·A0 녹화(실제 로컬 트래커 재등장 동작)로 조정한다.
- 생명주기·게이트·여유 수치는 명세에 없어 초기값이다. G2·A0 녹화로 조정한다(특히 소실 3 s, 게이트 0.5~1.5 m, 여유 0.3).
- ContactEstimator 가 생기면 `SupportContact` 로 접지·지지 표면을 싣는다. 고소 위치에서 가려진 사람의 "마지막 유효 높이 유지"(§5.2)는 그때 Z 쪽 상태와 함께 정한다.
- Cross-view 설계 문서의 "붙어 걷는 두 사람의 구성 흔들림"은 World Track 의 연결 이력·1:1 배정으로 SIM 에서 ID 바뀜 없이 흡수됐다. 실제 잡음에서 다시 본다.
