"""World Track Manager: Cross-view 클러스터를 최종 World ID 로 잇고 World 칼만 필터로 위치·속도·불확실성을 낸다
(명세 5.2 추적 3계층의 3층, 계산 순서 5단계, ID 교정 기록, 6.1 시간 모델, 6.2 World Track 상태).

입력은 공통 시각 t 의 `cross_view.AssociationFrame`(카메라별 로컬 트랙을 같은 사람끼리 묶은 view 클러스터)이고,
출력은 World ID 마다의 상태(`WorldTrackSnapshot`)다. `association_id` 는 Cross-view 내부 번호라 World ID 로 쓰지 않는다.
I1 `world_state` 문서 조립은 `tracking.world_state` 가 한다.

**World ID.** `WorldIdIssuer` 가 `w-{세션}-{번호}` 로 발급한다. 세션 접두어는 호출자가 주입한다(서비스 시작 시각 등,
`session_prefix_from_ms`). 재시작하면 접두어가 달라 이전 ID 와 겹치지 않고, 한 세션 안에서 번호는 늘기만 해 LOST·병합된
ID 를 다시 쓰지 않는다.

**연결(계산 순서 5단계).** 시각마다 아래 순서로 클러스터를 World Track 에 1:1 로 붙인다.

1. 클러스터 기준점: 클러스터 삼각측량의 골반 XY(양쪽 엉덩이), 없으면 삼각측량된 관절 XY 평균. 1대뿐인 클러스터는
   3D 가 없어 기준점이 없다. 기준점은 연결 비용에만 쓰고 위치로 내보내지 않는다.
2. 비용 = 예측 위치와 기준점의 수평 거리 / `gate_scale_m` − 로컬 트랙 연결 이력 보너스. 3D 거리 게이트 반경은
   `gate_base_m + max_speed_mps × (t − t_last_valid)` 이고 `gate_max_m` 에서 멈춘다(사람이 그 시간에 갈 수 있는 거리).
   게이트 밖은 연결 이력이 있어도 붙이지 않는다(로컬 트랙 ID 가 옆 사람으로 넘어간 경우). 연결 이력 보너스
   (`key_bonus`)는 애매함 여유(`ambiguity_margin`)보다 작게 둔다. 이력만으로는 기하가 같은 경쟁 후보를 이길 수 없다.
3. Cross-view 가 IDENTITY_UNCERTAIN 으로 표시하지 않은 클러스터를 헝가리안 1:1 로 배정한다. 고른 짝과 경쟁 짝(같은
   클러스터를 다투는 다른 Track, 같은 Track 을 다투는 다른 클러스터)의 비용 차가 여유보다 작으면 배정은 하되 관련
   Track 에 IDENTITY_UNCERTAIN 을 붙인다(위치는 맞고 신원만 불확실. 붙어 선 두 사람을 둘 다 놓치지 않는다).
4. IDENTITY_UNCERTAIN 클러스터는 억지로 붙이지 않는다. 남은 Track 중 게이트 안 후보가 하나뿐이고 경쟁 Track 이
   없으며 다른 Track 의 로컬 트랙이 섞이지 않았을 때만 붙이고 IDENTITY_UNCERTAIN 을 붙인다. 후보가 여럿이면 붙이지도
   새로 만들지도 않는다. 이번 시각에 이미 다른 클러스터를 받은 Track 은 후보에서 뺀다(그 사람일 수 없다). 게이트 안에
   남은 Track 이 없으면 IDENTITY_UNCERTAIN 인 새 ID 를 만든다(사람을 빠뜨리지 않는다. 잘못 합친 ID 보다 갈라진 ID 가
   덜 위험). 다만 그 클러스터의 로컬 트랙이 이번 시각에 이미 갱신된 Track 의 것이고 그 Track 이 클러스터 가까이
   (`duplicate_radius_m`)에 있으면 Cross-view 가 한 사람을 갈라 놓은 나머지 view 이므로 새 ID 를 만들지 않는다.
   이때 그 Track 에 IDENTITY_UNCERTAIN(CROSS_VIEW_UNCERTAIN)을 붙인다(가까이 선 새 사람일 수도 있다).
   멀면 로컬 트래커가 옛 ID 를 다른 사람에게 넘긴 것일 수 있어 새 ID 를 만든다.
5. 1대뿐인 클러스터와 붙지 않은 클러스터는 위치를 갱신하지 않는다. 그 로컬 트랙의 주인 Track 이 이번 시각에 갱신되지
   않았으면 연결 이력만 이어 둔다(가림 동안 남은 카메라 한 대의 트랙으로, 가림이 끝났을 때 같은 Track 으로 돌아오게).
6. 배정되지 않은 확실한 클러스터의 로컬 키 중 하나 이상이 최근(초기값 1 s) 이번 시각 갱신된 Track 에 속했고,
   그 Track 의 골반 0.6 m 안이고 카메라가 겹치지 않으면
   새 ID 를 억제하고 그 Track 에 CROSS_VIEW_UNCERTAIN 을 붙인다. 위치·관절·로컬 소유권은 합치지 않는다.
   기존 병합 금지 반증이나 이번 시각 같은 카메라의 별도 검출(1대 클러스터 포함)이 있으면 억제하지 않는다.
   그 밖의 배정되지 않은 클러스터 중 골반이 있는 것만 새 World Track(TENTATIVE)이 된다. 1대 클러스터나 골반 없는 클러스터로는
   만들지 않는다(2대 미만은 지어내지 않음). 다른 Track 의 예측 위치 가까이(`duplicate_radius_m`)에 생기면 같은 사람의
   두 번째 ID 일 수 있어 그 쌍을 POSSIBLE_DUPLICATE 후보로 둔다.
7. 같은 사람 두 ID 로 보이는 Track 은 **그 시각에는 합치지 않는다.** 붙인 클러스터에 이번 시각 갱신되지 않은 다른
   Track 의 로컬 트랙이 들어 있고 그 Track 이 가까워도, 한 시각만으로는 로컬 트래커 ID 전환(옆 사람에게 ID 가 넘어감)과
   구별할 근거가 없으므로 둘 다 남기고 POSSIBLE_DUPLICATE 후보 쌍으로 둔다. 후보 쌍은 둘 다 살아 있고 가까운 동안 매
   시각 IDENTITY_UNCERTAIN(POSSIBLE_DUPLICATE)을 다시 붙인다. 근거가 쌓이면 아래 늦은 재연결의 병합으로 합친다.

**시점 분할 그림자·동시 활성 중복 (S15P31A101-657, 14차 3명 → 89 ID).** 한 사람을 두 카메라 묶음이 따로 본 것(2+2)은
"카메라가 겹치지 않고, 방향별 불확실성 반경(두 묶음 삼각측량 공분산을 두 골반 방향으로 잰 σ 로 0.6~1.35 m) 안이고, 두 묶음
view 를 엉덩이마다 한 점으로 풀어도 재투영 오차가 20 px 안"인 경우로 판정한다(`_geometric_shadow`). 그러면
(a) 이번 시각 갱신된 확정 Track 의 그림자 클러스터는 소유 이력·IDENTITY_UNCERTAIN 과 무관하게 새 ID·늦은 재연결보다 먼저
걸러 내고(IDENTITY_UNCERTAIN 은 붙이지 않음), (b) 이미 둘 다 살아 있는 두 Track 은 근거가 4시각 이어지면 `_merge` 로 합치고
(MERGE·aliases), (c) 확정 Track 근처 저신뢰 TENTATIVE 는 확정을 6회로 늦추며, (d) 분신 쌍은 Re-ID 경쟁자·crowd 판정에서 서로를
뺀다. **안전장치:** 같은 시각 같은 카메라에서 서로 다른 로컬 트랙으로 따로 보인 두 사람(그 근거를 distinct_ttl_ms 안에 본
쌍 포함)은 절대 억제·병합하지 않는다. 0.5 m 옆 다른 사람은 한 점 재투영이 수십 px 벌어져 걸리지 않는다. 기립 키(머리 − 발목)는
재입장 지연 병합의 보조 근거로만 쓴다(확실히 다르면 후보 제외, 외형 애매 시 하나만 맞으면 동률 깨기).

같은 카메라의 두 검출은 Cross-view 클러스터에서 이미 갈라져 있고(카메라당 view 하나), 여기서는 Track 하나에 시각마다
클러스터 하나만 붙이므로 같은 카메라의 두 검출이 한 World ID 에 동시에 묶이지 않는다.

**늦은 재연결과 ID 교정(S15P31A101-40, 명세 5.2).** 근거는 로컬 트랙 연결 이력(카메라 안 로컬 트래커가 이어 온 트랙
ID)과 위치뿐이다. 외형·얼굴·재식별은 쓰지 않는다. 로컬 트랙 ID 는 다시 발급되지 않지만 로컬 트래커가 같은 ID 를 다른
사람 검출에 붙이는 ID 전환은 막지 못한다. 그래서 **카메라 한 대의 옛 로컬 트랙은 같은 사람의 근거로 세지 않고**, 서로
다른 카메라 `reconnect_min_cameras`(2) 대의 로컬 트래커가 같은 옛 Track 의 로컬 트랙을 이어 왔을 때만 근거로 본다.
두 카메라가 같은 상황(사라진 자리에 다른 사람이 나타남)에서 함께 ID 를 바꾸면 이 기준도 뚫린다(완전한 독립 근거가
아니다, 설계 문서 잔여 위험).
애매하면 잇지 않는다(잘못 합친 ID 보다 갈라진 ID 가 덜 위험, 잘못 이은 신원은 옛 사람의 위험을 해소시킨다).

- 소실 후보: CONFIRMED 였다가 LOST 가 된 Track 은 LOST 를 한 번 낸 뒤 마지막 측정부터 `reconnect_window_ms` 동안 소실
  후보로 남는다(world_state 에는 내지 않는다). 로컬 트랙 연결 이력도 그동안 남긴다(위치 비용 보너스에 쓰는 이력은
  `key_ttl_ms` 그대로). 1대 클러스터가 그 로컬 트랙을 계속 보이면 이력이 이어진다(5단계와 같다).
- 같은 ID 로 잇기: 새 Track 이 될 클러스터(6단계)의 옛 로컬 트랙이 모두 한 소실·가림 Track X 의 것이고 2대 이상이면 새
  ID 대신 X 에 붙인다. 조건: 클러스터가 IDENTITY_UNCERTAIN 이 아니고, 이번 시각 갱신된 다른 Track 의 로컬 트랙이 섞이지
  않았고, X 의 다른 로컬 트랙이 이번 시각 다른 클러스터에 있지 않고, 클러스터 기준점이 X 의 마지막 위치에서 사람이 갈
  수 있는 거리(`gate_base_m + max_speed_mps × 공백`) 안이고, X 의 마지막 위치 `reconnect_crowd_radius_m` 안에 다른
  소실·가림 Track 이 없다(가까이 있던 두 사람이 함께 사라지면 로컬 트랙이 서로 넘어갔을 수 있다). ID 가 바뀌지 않으므로
  identity_revision 은 그대로이고 `WorldTrackFrame.reassociations` 에 남는다.
- 근거가 모자라면(옛 로컬 트랙 1대, 위 조건 위반) 새 ID 를 만들고, 넘겨받은 옛 로컬 트랙의 주인 X 가 그 뒤 따로
  갱신되지 않고 남아 있는 동안(소실 후보 포함) 새 Track 에 IDENTITY_UNCERTAIN(REASSOCIATION_AMBIGUOUS)을 매 시각 붙인다.
  Risk 는 IDENTITY_UNCERTAIN 이면 해소를 확인하지 않는다. X 의 위험은 X 가 관측되지 않으므로 그대로 남는다.
- 병합(ID 교정): 이미 새 ID Y 를 받은 뒤 Y 에 붙은 클러스터에 갱신되지 않은 Track X 의 옛 로컬 트랙(지금 X 가 들고
  있거나 Y 가 X 에게서 넘겨받은 것)이 2대 이상이면, 위 조건과 함께 X·Y 가 병합 금지 쌍이 아닐 때 근거로 센다. 근거
  시각이 `merge_confirm_frames` 번 쌓이면 먼저 만든 ID 로 합친다. 도달 거리는 Y 의 위치 측정 전부(첫 관측부터)와 이번
  시각 기준점으로 잰다(지금 시각으로만 재면 Y 가 제자리에 있어도 시간이 지나 통과한다). 한 번이라도 갈 수 없는 곳에서
  관측됐으면, 그리고 같은 ID 로 잇기를 거리 때문에 거절했던 새 Track 이면 X·Y 를 병합 금지 쌍으로 둔다. 이어서 세기 전에 직전 근거가 `key_ttl_ms` 안인지 먼저
  보고, 넘었거나 반대 근거가 나오면 처음부터 센다. 남는 ID 의 identity_revision 을 올리고 사라지는 ID 를 별칭(aliases)에
  더하며 `IdentityRevision` 을 추가만 한다(덮어쓰지 않음).
- 병합 금지 쌍: 같은 시각 같은 카메라의 서로 다른 검출이 가리키는 Track(그 로컬 트랙의 주인, 그 클러스터를 받은 Track)
  은 서로 다른 사람이다. 위치 갱신 여부와 상관없이(1대 클러스터, 가림 중인 Track 포함) 병합 전에 기록하고, 병합하면
  남는 ID 로 이어진다.
- 잇거나 합친 Track 은 자기 로컬 트랙만으로(2대 이상) 위치가 갱신된 시각이 `merge_confirm_frames` 번 쌓일 때까지
  IDENTITY_UNCERTAIN(LATE_REASSOCIATION)을 매 시각 다시 붙인다. 시간이 지났다고 풀지 않는다.
- 수동 교정(CORRECTION)은 내지 않는다(운영자 명령 계약 승인 뒤).

**분할(ID 교정, S15P31A101-201, 명세 5.2).** 근거는 병합과 같은 로컬 트랙 연결 이력과 위치이고 기준도 병합과 대칭이다.
- 분할 후보: 이번 시각 갱신된 Track W 의 로컬 트랙(이번 시각 이전 주인이 W)이 서로 다른 카메라 `reconnect_min_cameras`(2)
  대 이상에서 **같은 카메라의 두 검출로 동시에** 갈라져, 한쪽은 W 가 받고 다른 쪽 클러스터가 새 ID N 이 됐다(6단계, 사람을
  빠뜨리지 않으려고 N 은 지금처럼 바로 만든다). 같은 시각 같은 카메라의 두 검출은 한 사람일 수 없으므로(병합 금지 쌍과 같은
  근거) W 는 두 사람의 관측을 묶고 있었다. 두 클러스터 중 하나라도 IDENTITY_UNCERTAIN 이면 후보로 두지 않는다. 한 카메라만
  갈라지면(로컬 ID 전환과 구별할 수 없음) 후보가 아니다.
- 지속: 그 뒤 W 와 N 이 둘 다 위치 측정을 받은 시각마다 N 의 클러스터에 W 에게서 넘겨받은 로컬 트랙이, W 의 클러스터에 같은
  카메라의 W 로컬 트랙이 2대 이상 함께 있으면 근거다. 두 골반의 수평 거리가 `duplicate_radius_m`(0.35 m, 같은 사람 두 ID
  기준과 같은 경계) 넘게 떨어진 근거 시각이 `merge_confirm_frames`(3) 번 쌓이면 분할한다. 가까우면(같은 사람일 수 있음)이나
  클러스터가 불확실하면 처음부터 센다. 한쪽이 가려진 시각은 세지도 버리지도 않는다. 근거가 `key_ttl_ms`(2 s) 넘게 끊기면
  후보를 버린다(분할하지 않음, 모르면 그대로). 만료는 이번 시각의 근거를 보기 전에 시각으로 판정한다(입력 정지 뒤 복귀 포함).
- 교정 결과: 원래 ID W 는 분할을 처음 본 시각에 W 가 받은 쪽(연결 비용, 곧 예측 위치에 가까운 쪽)에 남고 갈라져 나간 쪽은
  N 이다. 두 ID 의 identity_revision 을 둘 중 큰 값 + 1 로 함께 올리고 `IdentityRevision(SPLIT, world_id=N,
  previous_world_id=W, since=후보 시작 시각)` 을 추가만 한다. 별칭은 바꾸지 않는다(빼지 않음, N 에 W 를 넣지 않음).
  N 은 그 시각부터 살아 있는 동안 `split_from = W` 를 들고(world_state 2.1.0 `split_from`, 바꾸지 않음), 병합으로 사라지면
  함께 사라진다(남는 ID 는 자기 split_from 만 지킨다). 체크포인트에는 넣지 않는다(재시작하면 모두 새 ID).
  W 의 관절 게이트 이력은 지운다. 후보인 동안과 분할 뒤 두 Track 이 자기 로컬 트랙만으로(2대 이상) 위치가 갱신된 시각이
  `merge_confirm_frames` 번 쌓일 때까지 둘 다 IDENTITY_UNCERTAIN(IDENTITY_SPLIT)이다. W·N 은 병합 금지 쌍이라 다시 합치지
  않는다(진동 없음).

**재시작 뒤 복원 Track 매칭(S15P31A101-54, 명세 3.2·부록 C).** `restore` 로 체크포인트의 Track(`TrackCheckpoint`)을
받으면 재관측과 맞출 **후보**로만 둔다. 위치 정본이 아니므로 world_state·export_tracks·연결·칼만 필터에 넣지 않고
(복원 위치가 OBSERVED 로 새지 않는다), 복원 Track 의 World ID 를 다시 쓰지도 않는다. 재시작하면 로컬 트래커가 새로
시작해 늦은 재연결의 근거(서로 다른 카메라 2대의 옛 로컬 트랙)가 없고 남는 것은 위치뿐이다. 위치만으로는 잇지 않으므로
(위 늦은 재연결과 같은 근거 기준, 사라진 자리에 다른 사람이 나타난 경우와 구별할 수 없다) 재관측된 사람은 늘 새 ID 를
받는다. 새 Track 의 첫 위치 측정이 복원 Track 의 마지막 위치에서 그 시간에 갈 수 있는 거리(늦은 재연결과 같은 식) 안이면
그 복원 Track 을 후보로 남기고(`WorldTrackSnapshot.restored_candidates`, 가까운 순), 복원 Track 이 남아 있는 동안
IDENTITY_UNCERTAIN(RESTORED_TRACK_CANDIDATE)을 붙인다. 갈 수 없는 거리면 그 사람이 아니다(`restored_excluded`). 도달
거리는 첫 관측으로 잰다(시간이 지나 통과하지 않게). 체크포인트가 첫 프레임보다 `restore_max_age_ms`(30 s) 넘게 오래됐으면
복원하지 않고, 복원 Track 도 마지막 관측부터 그 시간이 지나면 후보에서 뺀다. 이전 이벤트와의 연결(related_event_id)은
`recovery.relink` 가 이 후보로 정한다.

**Risk 전달(G3).** Tier 0 조립부는 `WorldTrackFrame.revisions` 를 그 시각 판정을 `LifecycleTracker.apply` 하기 전에
병합은 `LifecycleTracker.correct_identity(revision.to_correction())`, 분할은 `LifecycleTracker.split_identity(
revision.to_split())` 로 넘긴다. 병합하면 진행 중인 이벤트는 사라지는 ID 에서 남는 ID 로 이어지고(원본 World ID 는
origin_world_ids 에 남음) 자동 해소되지 않는다(D-G3-10). 분할하면 이벤트는 W 에 그대로 남고 N 을 분할 상대로 기억해 W·N
모두 위험이 없다고 확인될 때만 해소한다.

**World 칼만 필터.** 골반 XYZ 를 측정으로 받는 등속 모델(상태 x y z vx vy vz, 백색 가속도 잡음). 측정 잡음은 관절
유효성 게이트의 두 엉덩이 삼각측량 표준편차로 만들고, 골반이 UNCERTAIN 이면 키운다. 골반이 없거나 UNAVAILABLE 이면
측정이 없는 시각이다(t_last_valid 를 늘리지 않음, 6.1 "예측 위치를 관측처럼 취급하지 않는다").
내보내는 `position_xy` 는 t_last_valid 의 필터 위치이고, 관측이 없는 시각에는 속도로 옮기지 않고 σ 만
`max_speed_mps × (t − t_last_valid)` 만큼 키운다. 위치 예측은 시간 모델(`time_model.evaluate_track`)이 staleness 로
한다. 연결 게이트에 쓰는 예측은 `max_extrapolation_ms`(6.1 PREDICTED 상한)까지만 속도로 옮긴다.

**생명주기와 관측 상태.** 생명주기 TENTATIVE → CONFIRMED → OCCLUDED → LOST 는 관리용이고, 관측 상태
(OBSERVED/PREDICTED/UNKNOWN)는 `world_state` 조립 때 시간 모델(`time_model.classify_observation`)로 따로 정한다.
명세에 수치가 없어 확정·가림·소실 시간은 `WorldTrackConfig` 의 초기값이다(G2 에서 조정).

관절 유효성 게이트(`geometry.joint_gate.JointGate`)는 Track 마다 하나씩 두고, 병합으로 `identity_revision` 이 오르거나
소실 후보를 다시 이으면 `reset()` 한다. 게이트에는 Cross-view 클러스터의 왜곡 제거·삼각측량 결과(`ViewCluster.undistorted`,
`ViewCluster.skeleton`)를 넘겨 다시 계산하지 않는다(S15P31A101-168, 입력이 같을 때만 다시 쓴다).
이 모듈의 관절·위치는 모두 Tier 0 관측(observed)이다. 접지(`ground_anchor_xyz`)는 만들지 않는다.
현재 시각을 읽지 않는다. 시각은 UTC int64 ms, 길이는 미터다.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from simai_ai.geometry.joint_gate import (
    GatedSkeleton,
    GateIssue,
    HeadZ,
    JointGate,
    JointGateConfig,
    triangulation_covariance,
)
from simai_ai.geometry.rig import CameraRig, UndistortedKeypoints
from simai_ai.geometry.triangulation import (
    COCO17_L_HIP,
    COCO17_R_HIP,
    JointValidity,
    SkeletonEstimate,
    joint_consistency_px,
)
from simai_ai.reid.collector import LocalTrackFeature
from simai_ai.reid.gallery import GalleryConfig, ReidGalleryStore, quantize_orientation_bin
from simai_ai.risk.lifecycle import IdentityCorrection, IdentitySplit
from simai_ai.tracking.assignment import linear_sum_assignment
from simai_ai.tracking.cross_view import AssociationFrame, TrackKey, ViewCluster
from simai_ai.tracking.time_model import ObservationState, TimeModelConfig, TrackTimeInput, require_ms

COCO17_L_SHOULDER = 5
COCO17_R_SHOULDER = 6
COCO17_L_ANKLE = 15
COCO17_R_ANKLE = 16
ReidFeatureLookup = Callable[[str, str, int], LocalTrackFeature | None]

JOINT_LAYOUT = "COCO17"
"""골반·head_z 정의가 있는 관절 배치만 받는다 (joint_gate 와 같음)"""
_FORBIDDEN = 1e9
"""헝가리안 금지 짝 비용 (유한 값, 결과에서 걸러낸다)"""
_SESSION_RE = re.compile(r"^[a-z0-9]{1,16}$")


class TrackLifecycle(StrEnum):
    """World Track 관리 생명주기 (contracts common track_lifecycle, 명세 5.2). 관측 상태와 별도다."""

    TENTATIVE = "TENTATIVE"
    CONFIRMED = "CONFIRMED"
    OCCLUDED = "OCCLUDED"
    LOST = "LOST"


class IdentityConfidence(StrEnum):
    """ID 확신 (contracts common identity_confidence). 위치 확신(σ)과 따로 관리한다 (명세 5.2)."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    IDENTITY_UNCERTAIN = "IDENTITY_UNCERTAIN"


class IdentityFlag(StrEnum):
    """IDENTITY_UNCERTAIN 을 붙인 사유."""

    CROSS_VIEW_UNCERTAIN = "CROSS_VIEW_UNCERTAIN"
    """Cross-view 가 IDENTITY_UNCERTAIN 으로 표시한 클러스터를 붙였거나, 이 Track 이 다투던 그런 클러스터를 붙이지 않았다"""
    AMBIGUOUS_TRACK_MATCH = "AMBIGUOUS_TRACK_MATCH"
    """고른 짝과 경쟁 짝의 비용 차가 애매함 여유보다 작다"""
    TWO_VIEW_COMPETITOR_JOINT = "TWO_VIEW_COMPETITOR_JOINT"
    """관절 유효성 게이트가 2대 관측 경쟁 후보 관절을 찾았다 (D-G2-06)"""
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    """같은 사람의 두 번째 ID 일 수 있다: 다른 Track 가까이에 새 ID 가 생겼거나, 한 클러스터에 가까운 다른 Track 의 로컬
    트랙이 섞였다. 두 Track 이 가까운 동안 계속 붙는다"""
    LATE_REASSOCIATION = "LATE_REASSOCIATION"
    """소실·가림 Track 을 로컬 트랙 연결 이력으로 다시 이었거나 ID 를 병합했고, 아직 일관성 확인 중이다 (S15P31A101-40)"""
    REASSOCIATION_AMBIGUOUS = "REASSOCIATION_AMBIGUOUS"
    """갱신되지 않은 다른 Track 의 옛 로컬 트랙을 넘겨받았지만 근거가 모자라(카메라 1대, 여러 Track, 근처의 다른 소실
    Track, 멀거나 다른 곳에도 보임) 잇지 않았다. 그 Track 이 남아 있는 동안 계속 붙는다(돌아온 사람일 수 있다)"""
    IDENTITY_SPLIT = "IDENTITY_SPLIT"
    """이 ID 의 로컬 트랙이 2대 이상 카메라에서 동시에 두 사람으로 갈라졌다(분할 후보)거나, 분할 뒤 일관성 확인 중이다
    (S15P31A101-201). 분할 전 관측이 둘 중 누구의 것인지 모른다"""
    RESTORED_TRACK_CANDIDATE = "RESTORED_TRACK_CANDIDATE"
    """재시작 뒤 처음 관측된 곳이 복원 Track(재시작 전 Track)이 갈 수 있는 거리 안이다. 위치만으로는 같은 사람인지 알 수
    없어 잇지 않았다(S15P31A101-54). 그 복원 Track 이 후보로 남아 있는 동안 계속 붙는다(재시작 전의 그 사람일 수 있다)"""


class RestoreState(StrEnum):
    """복원 Track 처리 상태 (`WorldTrackManager.restore_status`)."""

    NONE = "NONE"
    """restore 를 부르지 않았다(재시작 복구가 아님)"""
    PENDING = "PENDING"
    """restore 를 불렀고 첫 프레임을 기다린다(체크포인트 나이는 첫 프레임 시각으로 잰다)"""
    ACCEPTED = "ACCEPTED"
    """체크포인트가 restore_max_age_ms 안이라 복원 Track 을 후보로 받았다(Track 이 없었을 수도 있다)"""
    DISCARDED = "DISCARDED"
    """체크포인트가 오래됐거나 시각을 믿을 수 없어 복원 Track 을 버렸다(reason)"""


class RevisionKind(StrEnum):
    """ID 교정 종류 (명세 5.2 병합·분할·교정). MERGE·SPLIT 을 낸다(CORRECTION 은 자리만, 운영자 명령 계약 승인 뒤)."""

    MERGE = "MERGE"
    SPLIT = "SPLIT"
    CORRECTION = "CORRECTION"


class RevisionReason(StrEnum):
    """병합 근거 (`IdentityRevision.reason`)."""

    LATE_REASSOCIATION = "LATE_REASSOCIATION"
    """소실 후보(LOST)의 로컬 트랙이 이미 새 ID 를 받은 Track 에 이어졌다"""
    DUPLICATE_MERGE = "DUPLICATE_MERGE"
    """가림 중인(LOST 전) Track 의 로컬 트랙이 다른 ID 의 Track 에 이어졌다 (POSSIBLE_DUPLICATE 쌍 포함)"""
    SPLIT_OBSERVED = "SPLIT_OBSERVED"
    """한 ID 의 로컬 트랙이 2대 이상 카메라에서 같은 카메라의 두 검출로 갈라져, 떨어진 두 위치에서 이어졌다 (S15P31A101-201)"""


@dataclass(frozen=True)
class IdentityRevision:
    """ID 교정 기록 한 건. 기록은 추가만 한다.

    MERGE: previous_world_id 가 world_id 로 합쳐졌다(별칭이 됐다). SPLIT: world_id(새 ID)가 previous_world_id(원래 ID 를
    지킨 쪽)에서 갈라져 나왔다. 두 ID 모두 identity_revision 이 이 값이 됐고 별칭은 바뀌지 않는다(S15P31A101-201).

    필드 이름은 Risk Engine 의 `risk.lifecycle.IdentityCorrection`(previous_world_id, world_id, identity_revision)과
    같다. 이벤트에는 발생 당시의 원본 World ID 를 남기고, 교정이 진행 중인 위험을 자동 해소하지 않는다(명세 5.2).
    """

    t: int
    kind: RevisionKind
    world_id: str
    previous_world_id: str
    identity_revision: int
    """교정 뒤 world_id 의 identity_revision"""
    reason: str
    evidence: tuple[TrackKey, ...] = ()
    """근거가 된 (카메라, 로컬 트랙). SPLIT 은 새 ID 가 원래 ID 에게서 넘겨받은 로컬 트랙"""
    since: int | None = None
    """SPLIT: 갈라짐을 처음 본 시각(분할 후보 시작). t 는 분할을 확정한 시각이다"""

    def to_correction(self) -> IdentityCorrection:
        """Risk Engine 에 넘길 교정(`LifecycleTracker.correct_identity`). 병합만 넘길 수 있다."""
        if self.kind is not RevisionKind.MERGE:
            raise ValueError(f"{self.kind} 는 Risk 교정으로 넘길 형식이 없다 (MERGE 만)")
        return IdentityCorrection(
            previous_world_id=self.previous_world_id,
            world_id=self.world_id,
            identity_revision=self.identity_revision,
        )

    def to_split(self) -> IdentitySplit:
        """Risk Engine 에 넘길 분할(`LifecycleTracker.split_identity`). 분할만 넘길 수 있다."""
        if self.kind is not RevisionKind.SPLIT:
            raise ValueError(f"{self.kind} 는 Risk 분할로 넘길 형식이 아니다 (SPLIT 만)")
        return IdentitySplit(
            world_id=self.previous_world_id, new_world_id=self.world_id, identity_revision=self.identity_revision
        )

    def to_payload(self) -> dict[str, Any]:
        """저널·리플레이 기록용 순수 데이터 (I1 계약 형식이 아니다. 계약 제안은 설계 문서)."""
        return {
            "t": self.t,
            "kind": str(self.kind),
            "world_id": self.world_id,
            "previous_world_id": self.previous_world_id,
            "identity_revision": self.identity_revision,
            "reason": self.reason,
            "evidence": [list(k) for k in self.evidence],
            "since": self.since,
        }


def session_prefix_from_ms(t_ms: int) -> str:
    """세션 접두어: 서비스 시작 시각(UTC ms, 호출자가 주입)을 36진수 소문자로 쓴 값. 재시작마다 달라진다."""
    require_ms("t_ms", t_ms)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out, n = "", t_ms
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


class WorldIdIssuer:
    """세션 단위 익명 World ID 발급기. 한 번 낸 ID 는 다시 내지 않는다(LOST·병합된 ID 포함)."""

    def __init__(self, session: str) -> None:
        if not isinstance(session, str) or not _SESSION_RE.match(session):
            raise ValueError(f"세션 접두어는 소문자·숫자 1~16자여야 한다: {session!r}")
        self.session = session
        self._next = 1
        self._issued: list[str] = []

    @property
    def issued(self) -> tuple[str, ...]:
        return tuple(self._issued)

    def issue(self) -> str:
        world_id = f"w-{self.session}-{self._next:04d}"
        self._next += 1
        self._issued.append(world_id)
        return world_id


@dataclass(frozen=True)
class WorldTrackConfig:
    """World Track Manager 설정. 명세에 수치가 없는 값은 모두 초기값이며 G2·A0 에서 조정한다.

    시간 모델과 겹치는 값(`max_speed_mps`, `max_extrapolation_ms`)은 `TimeModelConfig` 기본값(2.0 m/s, PREDICTED
    상한 500 ms, 명세 6.1)과 같게 둔다. 근거는 docs/design/tier0-world-track.md.
    """

    confirm_hits: int = 3
    """TENTATIVE → CONFIRMED 에 필요한 위치 측정 횟수 (10 Hz 에서 0.3 s)"""
    tentative_max_gap_ms: int = 300
    """TENTATIVE 가 이 시간 넘게 측정이 없으면 LOST"""
    occluded_after_ms: int = 200
    """CONFIRMED 가 이 시간 넘게 측정이 없으면 OCCLUDED (6.1 OBSERVED 상한과 같은 초기값)"""
    lost_after_ms: int = 3000
    """짧은 재연결 창: 마지막 측정 뒤 이 시간까지 기존 거리 게이트로 같은 ID 를 유지한다.
    넘으면 LOST 이며 2대 로컬 이력에 의한 늦은 재연결만 허용한다. 관측 상태의 PREDICTED 상한과는 별도다"""
    reconnect_window_ms: int = 10_000
    """LOST 가 된 CONFIRMED Track 을 마지막 측정부터 이 시간 동안 소실 후보로 둔다(로컬 트랙 연결 이력도 이 시간 동안
    남긴다). lost_after_ms 와 같게 두면 늦은 재연결을 끈다. 명세 6.4 재개 시간 초기값(10 s)과 같게 둔 초기값이다"""
    reconnect_crowd_radius_m: float = 1.5
    """잇거나 합칠 Track 의 마지막 위치 이 반경 안에 다른 소실·가림 Track 이 있으면 잇지 않는다(로컬 트랙이 서로
    넘어갔을 수 있다). gate_max_m 과 같은 초기값"""
    reconnect_min_cameras: int = 2
    """늦은 재연결·병합에 필요한 옛 로컬 트랙의 카메라 수. 로컬 트래커는 같은 ID 를 다른 사람 검출에 붙일 수 있으므로
    (ID 전환) 카메라 한 대의 로컬 트랙만으로는 같은 사람이라고 보지 않는다. 2 미만은 받지 않는다"""
    merge_confirm_frames: int = 3
    """이미 새 ID 를 받은 Track 과의 병합(ID 교정)에 필요한 근거 시각 수 (10 Hz 에서 0.3 s, confirm_hits 와 같음).
    근거 로컬 트랙이 key_ttl_ms 넘게 빠지거나 반대 근거가 나오면 처음부터 센다"""
    gate_base_m: float = 0.5
    gate_max_m: float = 1.5
    gate_scale_m: float = 0.5
    """거리 비용 척도. 비용 1 = 0.5 m"""
    ambiguity_margin: float = 0.3
    """고른 짝과 경쟁 짝의 비용 차가 이보다 작으면 애매하다 (0.15 m 차이)"""
    key_bonus: float = 0.2
    """클러스터의 로컬 트랙이 모두 이 Track 의 것일 때 깎는 비용. ambiguity_margin 보다 작게 둔다"""
    key_ttl_ms: int = 2000
    """Track 이 로컬 트랙 연결 이력을 들고 있는 시간 (Cross-view history_ttl_ms 와 같음)"""
    duplicate_radius_m: float = 0.35
    """같은 사람 두 ID 후보로 보는 두 Track 사이 수평 거리. 같은 사람이 갈라진 두 클러스터의 골반은 거의 같은 곳이고,
    서로 다른 두 사람의 골반은 어깨 폭(약 0.45 m)보다 가깝기 어렵다"""
    split_suppression_enabled: bool = True
    """반증이 없으면 같은 시각 갱신된 Track 과 카메라가 겹치지 않는 근처 나머지 클러스터의 새 ID 를 억제한다"""
    split_suppression_radius_m: float = 0.6
    """분할 나머지 억제 반경. duplicate_radius_m(병합·분할 판정)은 바꾸지 않는다"""
    split_suppress_ownerless: bool = False
    """재생 비교용: True 면 최근 소유 이력이 없는 후보도 억제한다(1차 S3 동작). 반증 예외는 유지한다"""
    split_suppression_owner_ttl_ms: int = 1000
    """분할 나머지의 로컬 키 하나 이상이 억제 근거 Track 에 속했던 최근 시간 창(경계 포함)"""
    split_suppression_require_owner: bool = False
    """-413 S1·S2 비교용 추가 제약: 모든 로컬 트랙의 옛 주인이 같을 때만 억제한다"""
    shadow_suppression_enabled: bool = True
    """-657 시점 분할 그림자 억제: 이번 시각 갱신된 확정(CONFIRMED·OCCLUDED) Track 과 카메라가 겹치지 않는 근처 클러스터가
    방향별 불확실성 반경 안이고 두 묶음의 view 를 한 점으로 풀어도 재투영이 맞으면(같은 사람을 두 카메라 묶음이 따로 본 것)
    소유 이력·IDENTITY_UNCERTAIN 여부와 무관하게 새 ID 를 만들지 않는다. 같은 시각 같은 카메라의 별도 검출이나 병합 금지
    반증이 있으면 억제하지 않는다. 위치·관절·로컬 소유권은 합치지 않는다"""
    shadow_radius_base_m: float = 0.6
    """방향별 반경의 바닥값 (split_suppression_radius_m 과 같은 초기값). 시선 수직(옆 사람이 서는 방향)에서는 이 값 근처다"""
    shadow_radius_sigma_k: float = 2.5
    """방향별 반경 = base + k × σ_dir. σ_dir 는 두 묶음 삼각측량 공분산의 수평 성분을 두 골반을 잇는 방향으로 잰 값"""
    shadow_radius_max_m: float = 1.35
    """방향별 반경의 상한 (예각 2대 깊이 오차 0.5~1.0 m 를 덮고 gate_max_m 1.5 m 보다 작게)"""
    shadow_keypoint_sigma_px: float = 8.0
    """방향별 반경에 쓰는 2D 엉덩이 keypoint 1σ 가정(px). 몸통 회전·숙임에서 엉덩이 점이 10~18 px 밀리는 것을 2σ 로 덮는다.
    관절 게이트의 keypoint_sigma_px(2 px, VALID 판정용)와 다르다"""
    shadow_reprojection_px: float = 20.0
    """두 묶음 view 를 한 점으로 푼 엉덩이 재투영 오차 상한(px). triangulation.outlier_reprojection_error_px 와 같은 값이다.
    0.5 m 옆에 선 다른 사람은 5 m 거리에서 약 50 px 이상 벌어진다"""
    shadow_min_keypoint_confidence: float = 0.3
    """그림자 판정에 쓰는 엉덩이 keypoint 신뢰도 하한 (삼각측량 min_keypoint_confidence 와 같음)"""
    confirm_hits_near_confirmed: int = 6
    """-657 확정 지연: 카메라가 겹치지 않는 확정 Track 이 confirm_delay_radius_m 안에 있는 동안 저신뢰(클러스터
    IDENTITY_UNCERTAIN 또는 골반 VALID 아님)로 갱신된 TENTATIVE 의 확정 횟수 (10 Hz 에서 0.6 s). 전역 confirm_hits 는 그대로다"""
    confirm_delay_radius_m: float = 1.35
    """확정 지연을 거는 확정 Track 과의 수평 거리 (shadow_radius_max_m 과 같은 값)"""
    active_merge_enabled: bool = True
    """-657 동시 활성 중복 병합: 같은 시각 둘 다 갱신된 두 Track 이 카메라가 겹치지 않고 그림자 판정(방향별 반경 + 한 점 재투영)
    을 active_merge_confirm_ticks 번 연속 통과하면 먼저 만든 ID 로 합친다(MERGE·aliases). 같은 카메라 별도 검출이 병합 금지
    쌍 TTL 안에 있었거나 분할 쌍이면 합치지 않는다"""
    active_merge_confirm_ticks: int = 4
    """동시 활성 병합에 필요한 연속 근거 시각 수 (10 Hz 에서 0.4 s). 근거가 key_ttl_ms 넘게 끊기거나 반대 근거가 나오면 처음부터"""
    stature_enabled: bool = True
    """-657 키 보조 근거: Re-ID 지연 병합(재입장)에서 기립 키(머리 높이 − 발목 높이, 관측값만)가 확실히 다른 후보를 빼고,
    외형이 애매할 때 키로 하나만 남으면 그 후보로 잇는다(보호구로 외형이 비슷해지는 경우). 기하 게이트·위치 정본은 그대로다"""
    stature_window: int = 30
    """Track 마다 남기는 기립 키 표본 수 (10 Hz 에서 기립 3 s)"""
    stature_min_samples: int = 10
    """키를 쓰는 최소 기립 표본 수 (기립 1 s). 모자라면 키를 쓰지 않는다(모르면 비움)"""
    stature_min_tolerance_m: float = 0.07
    """키 일치 허용치 바닥값. 2대 관측 머리·발목 Z 표본 잡음(각 약 3~5 cm)과 자세 흔들림을 덮는다"""
    stature_sigma_k: float = 2.5
    """키 일치 허용치 = max(바닥값, k × √(σa² + σb²)). σ 는 표본 MAD × 1.4826 (표본 하나의 퍼짐, 중앙값 오차보다 보수적)"""
    standing_min_height_m: float = 1.2
    """기립 표본: 머리 − 발목 높이 하한 (웅크림·누움·바닥 앉기를 뺀다)"""
    standing_min_pelvis_ratio: float = 0.45
    """기립 표본: (골반 − 발목) / (머리 − 발목) 하한. 서기 약 0.55, 의자에 앉기 약 0.3~0.4"""
    distinct_ttl_ms: int = 15_000
    """-657 병합 금지 쌍 유지 시간: 같은 시각 같은 카메라의 별도 검출(또는 도달 불가 관측)을 마지막으로 본 뒤 이 시간 동안만
    병합 금지로 본다. 함께 보이는 동안은 매 시각 갱신된다. 분할로 생긴 쌍은 시간과 무관하게 남는다(진동 방지)"""
    uncertain_hold_ms: int = 1000
    """IDENTITY_UNCERTAIN 을 마지막 사유 뒤 이 시간 동안 유지한다 (깜박임 방지, 보수적). uncertain_hold_per_reason 이면
    상태 사유(분할·재연결·중복 후보·복원 후보)에만 쓰고, 관측 순간 사유는 uncertain_hold_transient_ms 를 쓴다"""
    uncertain_hold_transient_ms: int = 300
    """S15P31A101-698 ID-5: 매 시각 다시 판정되는 관측 순간 사유(TWO_VIEW_COMPETITOR_JOINT, AMBIGUOUS_TRACK_MATCH,
    CROSS_VIEW_UNCERTAIN)의 유지 시간(3 tick). 근거가 이어지면 매 시각 다시 붙으므로 불확실이 끊기지 않는다"""
    uncertain_hold_per_reason: bool = True
    """S15P31A101-698 ID-5: 사유별 유지 시간 사용. False 면 모든 사유에 uncertain_hold_ms(이전 동작, 재생 비교용)"""
    uncertain_hold_near_m: float = 1.0
    """S15P31A101-698 ID-5 보완: 관측 순간 사유가 붙을 때 다른 Track(LOST 제외)이 이 거리(m, XY) 안에 있으면 그 사유도
    uncertain_hold_ms 동안 유지한다. 로비 맞바꿈 구간 표시를 76 → 87 % 로 올리고 로비 불확실 시간은 거의 그대로(28.9 → 29.0 %)다.
    대가로 모여 서서 대화하는 장면의 불확실 시간이 늘어난다(Panoptic 41.8 → 51.2 %). 0 이면 끈다"""
    uncertain_jump_margin_m: float = 0.5
    """S15P31A101-698 ID-5 보완 A: 측정 위치가 직전 측정에서 max_speed_mps × 경과 시간 + 이 값(m)보다 멀리 뛰면 다른 사람
    관측을 넘겨받았을 수 있다고 보고, 그 Track 과 uncertain_jump_neighbor_m 안의 Track 에 AMBIGUOUS_TRACK_MATCH 를 붙여
    uncertain_hold_ms 동안 유지한다. 0 이면 끈다"""
    uncertain_jump_neighbor_m: float = 1.0
    """S15P31A101-698 ID-5 보완 A: 위치 급변 Track 의 상대로 볼 거리(m, XY)"""
    uncertain_hold_hidden_m: float = 1.0
    """S15P31A101-698 ID-5 보완 B: 가려진(그 시각 갱신 없음) Track 이 이 거리(m, XY) 안에 다른 Track(LOST 제외)을 두고 있으면
    이미 붙어 있는 관측 순간 사유를 끄지 않는다(새 사유는 만들지 않는다). 0 이면 끈다"""
    two_view_identity_torso_only: bool = True
    """S15P31A101-698 ID-6: 2대 클러스터도 몸통 관절(양 엉덩이·양 어깨)이 2대 관측 경쟁 후보일 때만 신원을
    IDENTITY_UNCERTAIN 으로 올린다. 끝 관절(손목·발목·얼굴)의 경쟁은 관절 게이트가 그 관절만 UNCERTAIN 으로 둔다.
    False 면 2대 클러스터의 어느 관절 경쟁이든 신원 불확실(이전 동작, 재생 비교용)"""
    max_speed_mps: float = 2.0
    max_extrapolation_ms: int = 500
    accel_sigma_xy_mps2: float = 2.0
    accel_sigma_z_mps2: float = 1.0
    min_measurement_sigma_m: float = 0.03
    uncertain_measurement_scale: float = 3.0
    """골반이 UNCERTAIN 이면 측정 표준편차를 이만큼 키운다"""
    init_velocity_sigma_mps: float = 1.0
    min_cameras_for_high: int = 3
    """이 수 이상의 카메라로 갱신된 CONFIRMED Track 만 HIGH. 2대뿐이면 MEDIUM (2대 영역 오연결, D-G2-06)"""
    restore_max_age_ms: int = 30_000
    """재시작 뒤 첫 프레임 시각 − 체크포인트 Track 시각(tracks_t)이 이보다 크면 복원 Track 을 받지 않는다. 받은 복원 Track 도
    마지막 관측부터 이 시간이 지나면 후보에서 뺀다. 이전 이벤트 연결(recovery.relink)은 첫 프레임부터 이 시간까지만 한다.
    명세 부록 C 의 기준 시간 초기값(30초)"""
    temporal_time_weight: float = 0.0
    """시간 간선 비용에서 공백 시간(`dt_ms / reconnect_window_ms`) 가중치 (S15P31A101-464)"""
    appearance_weight: float = 1.0
    """시간 간선 비용에서 외형 비용(`appearance_cost`, S15P31A101-463 2단계 COPE Re-ID 자리) 가중치"""
    reid_enabled: bool = True
    """Re-ID 외형 갤러리·분할 판정·swap 교정·DORMANT 재입장 사용 여부 (S15P31A101-463)"""
    reid_dormant_ttl_ms: int = 1_800_000
    """CONFIRMED 트랙이 reconnect_window_ms(10 s)를 넘긴 뒤 DORMANT 로 보관하는 시간 (기본 30분 = 1,800,000 ms)"""
    reid_split_same_min_sim: float = 0.60
    """분할 나머지 클러스터가 활성 트랙 갤러리와 같은 사람으로 판정되기 위한 최소 유사도 (주인 없는 나머지·확장 반경 포함)"""
    reid_split_different_max_sim: float = 0.42
    """분할 나머지 억제 후보라도 외형 유사도가 이 값 미만이면 다른 사람으로 판단해 억제를 풀고 새 ID 를 허용한다"""
    reid_split_margin: float = 0.10
    """분할 나머지 외형 병합·억제 시 동시 활성 다른 트랙 대비 필요한 최소 유사도 마진"""
    reid_split_max_radius_m: float = 1.2
    """외형이 일치하는 같은 사람 분할 나머지를 억제할 수 있는 최대 수평 거리 (단일 tick 도달 게이트 안)"""
    reid_reentry_min_sim: float = 0.65
    """DORMANT·LOST 트랙 재입장(Re-entry) 시 기존 World ID 를 이어받기 위한 보수적 최소 외형 유사도"""
    reid_reentry_margin: float = 0.10
    """DORMANT·LOST 트랙 재입장 시 2위 후보(활성·소실·휴면 포함) 대비 최소 유사도 마진"""
    reid_swap_min_sim: float = 0.58
    """근접 교차 후 두 트랙의 교차 외형 유사도가 swap 교정을 발동하기 위한 최소 유사도"""
    reid_swap_margin: float = 0.10
    """근접 교차 후 각 트랙의 교차 유사도가 자기 유사도보다 높아야 하는 최소 마진"""
    reid_swap_proximity_m: float = 1.5
    """두 트랙이 근접 교차했다고 기록하는 수평 거리 (1.0 m 이내에서는 갤러리 갱신을 멈춰 오염을 막는다)"""
    reid_swap_window_ms: int = 10_000
    """근접 교차 뒤 swap 교정을 감시하는 시간 창"""
    reid_swap_confirm_frames: int = 1
    """교차 유사도 우위가 연속 관측되어 swap 되돌리기를 확정하는 횟수"""
    reid_swap_correction_enabled: bool = True
    """근접 교차 뒤 ID swap 복원 사용 여부"""
    reid_dormant_enabled: bool = True
    """CONFIRMED 트랙 소실 뒤 DORMANT 보존 및 재입장 연결 사용 여부"""
    reid_gallery_freeze_distance_m: float = 1.0
    """두 활성 트랙 간 수평 거리가 이 값 이하일 때 갤러리 오염 방지를 위해 갤러리 갱신을 동결한다"""
    reid_gallery_min_visibility: float = 0.25
    reid_gallery_min_quality: float = 0.25
    reid_gallery_max_entries: int = 16
    reid_gallery_max_per_slot: int = 2
    reid_min_co_visibility: float = 0.35
    reid_split_respect_distinct: bool = True
    """S15P31A101-684 ID-01: Re-ID 확장 억제 분기도 병합 금지 반증(_is_distinct)을 먼저 적용한다. False 는 이전 동작(재생 비교용)"""
    reid_query_visibility: bool = True
    """S15P31A101-684 ID-03: 갤러리 외형 비교에 질의 특징의 부위 가시성을 넘겨 공통 가시 부위가 없으면 비교하지 않는다.
    False 는 이전 동작(가시성 없이 비교, 재생 비교용)"""
    shadow_clears_ambiguity: bool = True
    """S15P31A101-684 ID-02: 시점 분할 그림자로 확인해 거른 클러스터와 다툰 것만으로 붙은 신원 애매 사유를 이번 시각에서
    지운다. 그림자가 아닌 진짜 경쟁 클러스터가 함께 있으면 지우지 않는다. False 는 이전 동작(재생 비교용)"""
    joint_gate: JointGateConfig = field(default_factory=JointGateConfig)

    def __post_init__(self) -> None:
        for name in (
            "split_suppression_enabled",
            "split_suppress_ownerless",
            "split_suppression_require_owner",
            "shadow_suppression_enabled",
            "active_merge_enabled",
            "stature_enabled",
            "reid_enabled",
            "reid_swap_correction_enabled",
            "reid_dormant_enabled",
            "reid_split_respect_distinct",
            "reid_query_visibility",
            "shadow_clears_ambiguity",
            "uncertain_hold_per_reason",
            "two_view_identity_torso_only",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} 는 bool 이어야 한다")
        for name in (
            "confirm_hits",
            "merge_confirm_frames",
            "reid_swap_confirm_frames",
            "confirm_hits_near_confirmed",
            "active_merge_confirm_ticks",
            "stature_window",
            "stature_min_samples",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 는 1 이상이어야 한다")
        if self.distinct_ttl_ms < 0:
            raise ValueError("distinct_ttl_ms 는 0 이상이어야 한다")
        for name in (
            "shadow_radius_base_m",
            "shadow_radius_max_m",
            "shadow_keypoint_sigma_px",
            "shadow_reprojection_px",
            "confirm_delay_radius_m",
        ):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} 는 양수여야 한다")
        if self.shadow_radius_sigma_k < 0 or not self.shadow_radius_base_m <= self.shadow_radius_max_m:
            raise ValueError("shadow_radius_sigma_k ≥ 0, shadow_radius_base_m ≤ shadow_radius_max_m 이어야 한다")
        if not 0 <= self.shadow_min_keypoint_confidence <= 1:
            raise ValueError("shadow_min_keypoint_confidence 는 0~1 이어야 한다")
        for name in (
            "tentative_max_gap_ms",
            "occluded_after_ms",
            "lost_after_ms",
            "reconnect_window_ms",
            "key_ttl_ms",
            "split_suppression_owner_ttl_ms",
            "uncertain_hold_ms",
            "uncertain_hold_transient_ms",
            "max_extrapolation_ms",
            "restore_max_age_ms",
            "temporal_time_weight",
            "appearance_weight",
            "reid_dormant_ttl_ms",
            "reid_swap_window_ms",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 는 0 이상이어야 한다")
        if not self.occluded_after_ms <= self.lost_after_ms <= self.reconnect_window_ms:
            raise ValueError("occluded_after_ms ≤ lost_after_ms ≤ reconnect_window_ms 여야 한다")
        if self.reconnect_min_cameras < 2:
            raise ValueError("reconnect_min_cameras 는 2 이상이어야 한다 (로컬 트랙 하나는 독립 근거가 아니다)")
        if self.reconnect_crowd_radius_m < 0:
            raise ValueError("reconnect_crowd_radius_m 은 0 이상이어야 한다")
        for name in ("uncertain_hold_near_m", "uncertain_jump_margin_m", "uncertain_jump_neighbor_m", "uncertain_hold_hidden_m"):
            if not getattr(self, name) >= 0:
                raise ValueError(f"{name} 은 0 이상이어야 한다")
        for name in (
            "gate_base_m",
            "gate_max_m",
            "gate_scale_m",
            "duplicate_radius_m",
            "split_suppression_radius_m",
            "max_speed_mps",
            "accel_sigma_xy_mps2",
            "accel_sigma_z_mps2",
            "min_measurement_sigma_m",
            "init_velocity_sigma_mps",
        ):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} 는 양수여야 한다")
        if not self.gate_base_m <= self.gate_max_m:
            raise ValueError("gate_base_m ≤ gate_max_m 여야 한다")
        if not 0 <= self.key_bonus < self.ambiguity_margin:
            raise ValueError("0 ≤ key_bonus < ambiguity_margin 이어야 한다 (이력만으로 기하가 같은 경쟁 후보를 이기지 않게)")
        if self.uncertain_measurement_scale < 1:
            raise ValueError("uncertain_measurement_scale 은 1 이상이어야 한다")
        if self.min_cameras_for_high < 2:
            raise ValueError("min_cameras_for_high 는 2 이상이어야 한다")

    @classmethod
    def from_time_model(cls, time_model: TimeModelConfig, **overrides: Any) -> WorldTrackConfig:
        """시간 모델 설정의 속도 상한·PREDICTED 상한을 그대로 쓰는 설정."""
        values: dict[str, Any] = {
            "max_speed_mps": time_model.max_speed_mps,
            "max_extrapolation_ms": time_model.predicted_max_gap_ms,
        }
        values.update(overrides)
        return cls(**values)


class _Kalman:
    """골반 XYZ 등속 칼만 필터. 상태 [x, y, z, vx, vy, vz], 백색 가속도 잡음."""

    def __init__(self, cfg: WorldTrackConfig, t: int, z: np.ndarray, sigma: np.ndarray) -> None:
        self.cfg = cfg
        self._init(t, z, sigma)

    def _init(self, t: int, z: np.ndarray, sigma: np.ndarray) -> None:
        self.t = t
        self.x = np.concatenate([z, np.zeros(3)])
        v2 = self.cfg.init_velocity_sigma_mps**2
        self.P = np.diag(np.concatenate([sigma**2, [v2, v2, v2]]))

    def _fq(self, dt: float) -> tuple[np.ndarray, np.ndarray]:
        F = np.eye(6)
        F[:3, 3:] = dt * np.eye(3)
        q = np.array([self.cfg.accel_sigma_xy_mps2**2, self.cfg.accel_sigma_xy_mps2**2, self.cfg.accel_sigma_z_mps2**2])
        Q = np.zeros((6, 6))
        Q[:3, :3] = np.diag(q * dt**4 / 4.0)
        Q[:3, 3:] = Q[3:, :3] = np.diag(q * dt**3 / 2.0)
        Q[3:, 3:] = np.diag(q * dt**2)
        return F, Q

    def predict_xy(self, t: int) -> np.ndarray:
        """연결 게이트용 예측 XY. 속도는 수평 속도 상한으로 자르고 `max_extrapolation_ms` 까지만 옮긴다."""
        dt = min(t - self.t, self.cfg.max_extrapolation_ms) / 1000.0
        v = self.x[3:5].copy()
        speed = float(np.hypot(v[0], v[1]))
        if speed > self.cfg.max_speed_mps:
            v *= self.cfg.max_speed_mps / speed
        return self.x[:2] + v * dt

    def update(self, t: int, z: np.ndarray, sigma: np.ndarray) -> None:
        if t - self.t > self.cfg.max_extrapolation_ms:
            # 오래 못 본 뒤 다시 잡았다: 묵은 속도로 옮기지 않고 측정에서 다시 시작한다
            self._init(t, z, sigma)
            return
        F, Q = self._fq((t - self.t) / 1000.0)
        x = F @ self.x
        P = F @ self.P @ F.T + Q
        H = np.hstack([np.eye(3), np.zeros((3, 3))])
        S = H @ P @ H.T + np.diag(sigma**2)
        K = P @ H.T @ np.linalg.inv(S)
        self.x = x + K @ (z - H @ x)
        IKH = np.eye(6) - K @ H
        self.P = IKH @ P @ IKH.T + K @ np.diag(sigma**2) @ K.T  # Joseph 형식 (대칭·양정치 유지)
        self.t = t

    @property
    def sigma_xyz(self) -> np.ndarray:
        return np.sqrt(np.maximum(np.diag(self.P)[:3], 0.0))


@dataclass(frozen=True)
class WorldTrackSnapshot:
    """공통 시각 t 의 World Track 하나.

    position_xy·position_xy_sigma·velocity 는 t_last_valid 의 필터 값에 관측 공백 동안의 σ 확대를 더한 것이다
    (속도로 옮기지 않음). pelvis_xyz·skeleton·head_z·cameras 는 이번 시각에 클러스터가 붙었을 때만 있다.
    """

    world_id: str
    t: int
    t_last_valid: int
    lifecycle: TrackLifecycle
    identity_confidence: IdentityConfidence
    identity_revision: int
    position_xy: tuple[float, float]
    position_xy_sigma: tuple[float, float]
    velocity: tuple[float, float, float]
    pelvis_xyz: tuple[float, float, float] | None = None
    skeleton: GatedSkeleton | None = None
    head_z: HeadZ | None = None
    cameras: tuple[str, ...] = ()
    association_id: str | None = None
    """이번 시각에 붙은 Cross-view 클러스터 (World ID 가 아니다)"""
    local_keys: tuple[TrackKey, ...] = ()
    """이번 시각에 붙은 (카메라, 로컬 트랙)"""
    identity_flags: tuple[IdentityFlag, ...] = ()
    """유지 중인 IDENTITY_UNCERTAIN 사유"""
    identity_flags_raised: tuple[IdentityFlag, ...] = ()
    """identity_flags 중 이번 시각에 새로 붙은(갱신된) 사유. 나머지는 uncertain_hold_ms 유지로만 남은 것이다. 판정에 쓰지 않는
    계측용 값이다(S15P31A101-684, `tracking.diagnostics`)"""
    aliases: tuple[str, ...] = ()
    """병합으로 이 ID 에 합쳐진 이전 ID"""
    filter_sigma_xy: tuple[float, float] = (0.0, 0.0)
    """t_last_valid 의 필터 σ (공백 확대 전)"""
    restored_candidates: tuple[str, ...] = ()
    """재시작 전 Track(복원 Track) 중 이 사람일 수 있는 것 (첫 관측이 갈 수 있는 거리 안, 가까운 순). 신원 근거가 아니라
    이전 이벤트 연결(related_event_id)의 후보다. 위치를 주지 않는다 (S15P31A101-54)"""
    restored_excluded: tuple[str, ...] = ()
    """첫 관측 때 남아 있던 복원 Track 중 갈 수 없는 거리여서 이 사람이 아닌 것"""
    split_from: str | None = None
    """분할로 이 ID 가 갈라져 나온 원래 World ID (분할 확정 시각부터 살아 있는 동안 같은 값, S15P31A101-201)"""
    avatar_update_hold: bool = False
    """ID 신뢰도가 낮을 때(IDENTITY_UNCERTAIN, 분할 판정 대기, swap 의심) 아바타 외형 갱신을 보류하는 플래그 (S15P31A101-463)"""
    reid_similarity: float | None = None
    """이번 시각 클러스터와 자기 갤러리의 외형 유사도 (관측 특징이 있을 때만)"""
    reid_margin: float | None = None
    """이번 시각 동시 활성 다른 트랙 갤러리 대비 외형 유사도 마진"""

    def __post_init__(self) -> None:
        if (
            self.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
            or IdentityFlag.IDENTITY_SPLIT in self.identity_flags
            or IdentityFlag.CROSS_VIEW_UNCERTAIN in self.identity_flags
        ):
            object.__setattr__(self, "avatar_update_hold", True)

    @property
    def hold_appearance_update(self) -> bool:
        """`avatar_update_hold` 의 별칭."""
        return self.avatar_update_hold

    @property
    def updated(self) -> bool:
        """이번 시각에 위치 측정이 들어왔는지."""
        return self.t_last_valid == self.t

    def time_input(self) -> TrackTimeInput:
        """시간 모델(`time_model.evaluate_track`) 입력. 위치·σ 는 t_last_valid 시각의 필터 값이다(σ 이중 확대 방지)."""
        return TrackTimeInput(
            world_id=self.world_id,
            t_last_valid=self.t_last_valid,
            position_xy=self.position_xy,
            position_xy_sigma=self.filter_sigma_xy,
            velocity=self.velocity,
        )


@dataclass(frozen=True)
class WorldTrackFrame:
    """공통 시각 t 의 World Track Manager 결과.

    tracks: LOST 가 아닌 Track 과 이번 시각에 LOST 가 된 Track(한 번만). world_id 순서. 병합으로 사라진 ID 는 없다
        (revisions 의 previous_world_id).
    revisions: 이번 시각에 생긴 ID 교정 기록(병합). Risk 에는 판정 반영 전에 `to_correction()` 으로 넘긴다.
    input_provenance: 이 결과를 만든 입력 출처(`common/defs.schema.json#/$defs/input_provenance`). world_state 조립이
        payload.input_provenance(계약 1.1.0)로 싣는다. SIM·DATASET_GT 입력이면 is_upper_bound 가 참이다.
    reassociations: 이번 시각에 소실 후보(LOST 를 낸 Track)를 같은 ID 로 다시 이은 World ID. ID 가 바뀌지 않으므로 교정
        기록은 아니다.
    """

    t: int
    calibration_version: str
    tracks: tuple[WorldTrackSnapshot, ...]
    revisions: tuple[IdentityRevision, ...] = ()
    input_provenance: Mapping[str, Any] | None = None
    reassociations: tuple[str, ...] = ()

    def track(self, world_id: str) -> WorldTrackSnapshot | None:
        for tr in self.tracks:
            if tr.world_id == world_id:
                return tr
        return None


_TRACK_CHECKPOINT_KEYS = (
    "world_id",
    "created_t",
    "t_last_valid",
    "lifecycle",
    "identity_revision",
    "aliases",
    "position_xyz",
    "filter_sigma_xyz",
    "velocity",
)


@dataclass(frozen=True)
class TrackCheckpoint:
    """체크포인트에 남기는 World Track 하나(명세 3.2 재시작 복구, S15P31A101-51). Tier 0 내부 형식이다(계약 아님).

    값은 t_last_valid 시각의 필터 상태다(위치·σ·속도). 관측 상태는 저장하지 않는다. 되살린 Track 은 재시작 뒤 아직
    아무 카메라로도 다시 보지 않았으므로 늘 UNKNOWN 이다(`observation_state`). 기록에 관측 상태를 넣어도 받지 않는다
    (모르는 필드). 재시작 뒤에는 `WorldTrackManager.restore` 가 재관측과 맞출 후보로만 쓴다(같은 World ID 로 잇지
    않고, 오래된(30초 넘은) 체크포인트의 Track 은 버린다, S15P31A101-54).
    """

    world_id: str
    created_t: int
    t_last_valid: int
    lifecycle: TrackLifecycle
    identity_revision: int
    aliases: tuple[str, ...]
    position_xyz: tuple[float, float, float]
    filter_sigma_xyz: tuple[float, float, float]
    velocity: tuple[float, float, float]

    @property
    def observation_state(self) -> ObservationState:
        """되살린 Track 의 관측 상태. 재관측 전이므로 늘 UNKNOWN 이다(OBSERVED 로 내보내지 않는다, 6.1)."""
        return ObservationState.UNKNOWN

    def to_record(self) -> dict[str, Any]:
        return {
            "world_id": self.world_id,
            "created_t": self.created_t,
            "t_last_valid": self.t_last_valid,
            "lifecycle": str(self.lifecycle),
            "identity_revision": self.identity_revision,
            "aliases": list(self.aliases),
            "position_xyz": list(self.position_xyz),
            "filter_sigma_xyz": list(self.filter_sigma_xyz),
            "velocity": list(self.velocity),
        }

    @classmethod
    def from_record(cls, doc: Any) -> TrackCheckpoint:
        """to_record 의 역. 빠진 필드·모르는 필드(관측 상태 포함)·형식 위반은 ValueError(객체가 아니면 TypeError)."""
        if not isinstance(doc, Mapping):
            raise TypeError("TrackCheckpoint: 객체여야 한다")
        keys = set(doc)
        if keys != set(_TRACK_CHECKPOINT_KEYS):
            raise ValueError(
                f"TrackCheckpoint: 필드가 맞지 않는다 (빠짐 {sorted(set(_TRACK_CHECKPOINT_KEYS) - keys)},"
                f" 모름 {sorted(keys - set(_TRACK_CHECKPOINT_KEYS))})"
            )
        world_id = doc["world_id"]
        if not isinstance(world_id, str) or not world_id:
            raise ValueError("TrackCheckpoint.world_id 가 비었다")
        aliases = doc["aliases"]
        if not isinstance(aliases, list | tuple) or not all(isinstance(a, str) and a for a in aliases):
            raise ValueError("TrackCheckpoint.aliases 는 문자열 배열이어야 한다")
        revision = doc["identity_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError(f"TrackCheckpoint.identity_revision 은 0 이상 정수: {revision!r}")
        created_t = require_ms("TrackCheckpoint.created_t", doc["created_t"])
        t_last_valid = require_ms("TrackCheckpoint.t_last_valid", doc["t_last_valid"])
        if t_last_valid < created_t:
            raise ValueError("TrackCheckpoint: t_last_valid 가 created_t 보다 앞이다")
        return cls(
            world_id=world_id,
            created_t=created_t,
            t_last_valid=t_last_valid,
            lifecycle=TrackLifecycle(doc["lifecycle"]),
            identity_revision=revision,
            aliases=tuple(aliases),
            position_xyz=_finite3(doc["position_xyz"], "position_xyz"),
            filter_sigma_xyz=_finite3(doc["filter_sigma_xyz"], "filter_sigma_xyz", nonnegative=True),
            velocity=_finite3(doc["velocity"], "velocity"),
        )


def _finite3(value: Any, name: str, nonnegative: bool = False) -> tuple[float, float, float]:
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError(f"TrackCheckpoint.{name} 는 길이 3 배열이어야 한다")
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            raise ValueError(f"TrackCheckpoint.{name} 에 유한하지 않은 값: {v!r}")
        if nonnegative and v < 0:
            raise ValueError(f"TrackCheckpoint.{name} 는 음수가 아니어야 한다: {v!r}")
        out.append(float(v))
    return out[0], out[1], out[2]


@dataclass(frozen=True)
class RestoreStatus:
    """복원 Track 처리 결과 (S15P31A101-54).

    tracks_t: restore 로 받은 체크포인트 Track 시각. decided_t: 그것을 판단한 재시작 뒤 첫 프레임 시각.
    restored: 받은 복원 Track 의 World ID(그 시각에 이미 마지막 관측부터 restore_max_age_ms 가 지난 것은 뺀다).
    link_until: 이전 이벤트 연결을 할 마지막 시각(decided_t + restore_max_age_ms). 판단 전이거나 restore 를 부르지 않았으면
    None.
    """

    state: RestoreState = RestoreState.NONE
    reason: str | None = None
    tracks_t: int | None = None
    decided_t: int | None = None
    restored: tuple[str, ...] = ()
    link_until: int | None = None

    def link_open(self, t: int) -> bool:
        """시각 t 에 재시작 전 이벤트와의 연결을 할지 (판단을 마쳤고 연결 시간 안)."""
        return self.link_until is not None and t <= self.link_until


@dataclass
class _Track:
    world_id: str
    created_t: int
    order: int
    kf: _Kalman
    gate: JointGate
    t_last_valid: int
    hits: int = 1
    lifecycle: TrackLifecycle = TrackLifecycle.TENTATIVE
    identity_revision: int = 0
    aliases: list[str] = field(default_factory=list)
    keys: dict[TrackKey, int] = field(default_factory=dict)
    """로컬 트랙 → 마지막으로 이 Track 에 붙은 시각. 재연결·분할 억제·일반 키 창 중 최댓값까지 남긴다.
    비용 보너스·일반 주인 판단은 `key_ttl_ms`, 분할 나머지 판단은 `split_suppression_owner_ttl_ms` 창을 쓴다"""
    flags: dict[IdentityFlag, int] = field(default_factory=dict)
    """IDENTITY_UNCERTAIN 사유 → 마지막으로 붙은 시각"""
    long_hold_flags: dict[IdentityFlag, int] = field(default_factory=dict)
    """-698 ID-5 보완: 맞바꿈 위험(가까운 사람·swap 의심·위치 급변) 중에 붙은 관측 순간 사유 → 그 시각.
    flags 의 같은 시각이면 uncertain_hold_ms 로 유지"""
    jump_t: int = -1
    """-698 ID-5 보완 A: 직전 측정에서 갈 수 없는 거리로 뛴 측정을 받은 시각"""
    hidden_hold_t: int = -1
    """-698 ID-5 보완 B: 가려진 채 가까이 다른 사람이 있던 마지막 시각. 그 뒤 uncertain_hold_transient_ms 까지 관측 순간 사유를
    끄지 않는다"""
    reacquired_until: int = -1
    """연결 이력 없이 게이트만으로 다시 잡은 뒤 MEDIUM 으로 두는 시각"""
    origin_of: dict[TrackKey, tuple[str, int]] = field(default_factory=dict)
    """갱신되지 않은 다른 Track(가림·소실 후보)에게서 넘겨받은 로컬 트랙 → (그 Track, 넘겨받은 시각). 그 Track 이 그 뒤
    다시 갱신되지 않고 남아 있는 동안 이 Track 은 그 사람일 수도 있어 IDENTITY_UNCERTAIN(REASSOCIATION_AMBIGUOUS)이고,
    병합 근거를 셀 때 그 Track 의 로컬 트랙으로 본다"""
    path: list[tuple[int, np.ndarray]] = field(default_factory=list)
    """위치 측정 (시각, 골반 XY) 이력. `reconnect_window_ms` 까지 남긴다. 병합 근거의 도달 거리를 이 Track 의 관측
    전부로 잰다(지금 시각으로만 재면 제자리에 있어도 시간이 지나 통과한다, 리뷰 F4)"""
    confirm_left: int = 0
    """늦은 재연결·병합 뒤 일관성 확인이 남은 시각 수. 0 이 될 때까지 IDENTITY_UNCERTAIN(LATE_REASSOCIATION)을 매 시각
    다시 붙인다(시간이 지났다고 풀지 않는다)"""
    restored_candidates: tuple[str, ...] = ()
    """첫 위치 측정이 갈 수 있는 거리 안이었던 복원 Track (가까운 순). 첫 관측에서 정하고 바꾸지 않는다(병합하면 합친다)"""
    restored_excluded: frozenset[str] = frozenset()
    """첫 위치 측정 때 후보였던(살아 있던) 복원 Track 중 갈 수 없는 거리였던 것: 이 사람이 아니다"""
    split_left: int = 0
    """분할 뒤 일관성 확인이 남은 시각 수. 0 이 될 때까지 IDENTITY_UNCERTAIN(IDENTITY_SPLIT)을 매 시각 다시 붙인다"""
    split_from: str | None = None
    """분할로 이 ID 가 갈라져 나온 원래 World ID. 한 번 정하면 바꾸지 않는다(world_state 2.1.0 split_from)"""
    swap_suspected_until: int = -1
    """근접 교차·외형 마진 부족으로 swap 이 의심되어 아바타 외형 갱신과 갤러리 갱신을 보류하는 시각"""
    last_reid_similarity: float | None = None
    last_reid_margin: float | None = None
    reid_checked_reentry: bool = False
    confirm_needed: int = 0
    stature: list[float] = field(default_factory=list)
    """-657 기립 키 표본(머리 − 발목, m, 관측값만). 최근 stature_window 개. 소실·장기 보류 동안 Track 과 함께 남는다"""
    """-657 이 TENTATIVE 의 확정 횟수 하한(확정 Track 근처 저신뢰 갱신이면 confirm_hits_near_confirmed). 0 이면 confirm_hits"""

    @property
    def last_xy(self) -> np.ndarray:
        """t_last_valid 의 필터 XY (늦은 재연결 거리 기준)."""
        return self.kf.x[:2]


@dataclass
class _Evidence:
    """병합 근거: 갱신되지 않은 Track `stale` 의 로컬 트랙(keys)이 갱신된 Track 에 이어진 시각 수와 마지막 시각."""

    stale: str
    keys: frozenset[TrackKey]
    count: int
    last_t: int


@dataclass
class _ShadowEvidence:
    """동시 활성 두 Track 이 같은 사람의 시점 분할로 보인 연속 근거 (-657)."""

    count: int
    last_t: int


@dataclass
class _Split:
    """분할 후보: 갱신된 Track `source` 의 로컬 트랙(keys)이 같은 카메라의 다른 검출로 갈라져 새 Track `child` 가 됐다."""

    source: str
    child: str
    keys: frozenset[TrackKey]
    count: int
    """두 골반이 duplicate_radius_m 넘게 떨어진 근거 시각 수"""
    last_t: int
    """마지막 근거 시각 (key_ttl_ms 넘게 끊기면 후보를 버린다)"""
    since: int


@dataclass
class _Cluster:
    cluster: ViewCluster
    anchor: np.ndarray | None
    has_pelvis: bool

    @property
    def uncertain(self) -> bool:
        return self.cluster.identity_uncertain


@dataclass
class _Applied:
    cluster: ViewCluster
    gated: GatedSkeleton
    pelvis: tuple[float, float, float] | None


AppearanceCostFn = Callable[[str, ViewCluster, int], float]
"""시간 그래프 연결에서 (world_id, cluster, t) 의 외형 비용(코사인 거리 등)을 돌려주는 콜백 타입.
기본은 0.0 이고 S15P31A101-463 2단계(COPE Re-ID)가 꽂는다 (S15P31A101-464)."""


@dataclass(frozen=True)
class TemporalEdgeCost:
    """끊긴/활성 World Track 과 새 클러스터 사이의 시간 그래프 간선 비용 ( 위치 + 시간 + 외형, S15P31A101-464)."""

    track_id: str
    association_id: str
    dt_ms: int
    distance_m: float
    gate_radius_m: float
    position_cost: float
    time_cost: float
    key_bonus: float
    appearance_cost: float = 0.0
    feasible: bool = True
    reject_reason: str | None = None

    @property
    def total_cost(self) -> float:
        if not self.feasible:
            return math.inf
        return float(self.position_cost + self.time_cost + self.appearance_cost - self.key_bonus)


def temporal_edge_cost(
    track_id: str,
    track_xy: np.ndarray | Sequence[float],
    t_last_valid: int,
    cluster: ViewCluster,
    cluster_xy: np.ndarray | Sequence[float],
    t: int,
    config: WorldTrackConfig,
    *,
    shared_keys: int = 0,
    appearance_cost: float = 0.0,
    clamp_gate: bool = True,
    reject_reason: str | None = None,
) -> TemporalEdgeCost:
    """World Track 과 클러스터 사이의 시간 그래프 간선 비용(위치 + 시간 + 외형 − 키 이력 보너스)을 계산한다 (S15P31A101-464).

    이번 티켓에서는 `appearance_cost = 0.0` 이 기본이며, S15P31A101-463 2단계(COPE Re-ID)가 기하 게이트 통과 후보에
    코사인 거리를 넣을 수 있도록 인터페이스를 분리한다.
    """
    dt_ms = int(t - t_last_valid)
    tx, ty = float(track_xy[0]), float(track_xy[1])
    cx, cy = float(cluster_xy[0]), float(cluster_xy[1])
    d = float(math.hypot(cx - tx, cy - ty))
    reach = config.gate_base_m + config.max_speed_mps * max(dt_ms, 0) / 1000.0
    radius = min(config.gate_max_m, reach) if clamp_gate else reach
    reason = reject_reason
    if reason is None and d > radius:
        reason = "unreachable"
    feasible = reason is None
    pos_cost = d / config.gate_scale_m
    time_cost = (max(dt_ms, 0) / max(config.reconnect_window_ms, 1)) * config.temporal_time_weight
    app_cost = float(appearance_cost) * config.appearance_weight
    total_keys = max(len(cluster.keys), 1)
    bonus = config.key_bonus * (shared_keys / total_keys)
    return TemporalEdgeCost(
        track_id=track_id,
        association_id=cluster.association_id,
        dt_ms=dt_ms,
        distance_m=d,
        gate_radius_m=float(radius),
        position_cost=float(pos_cost),
        time_cost=float(time_cost),
        key_bonus=float(bonus),
        appearance_cost=float(app_cost),
        feasible=feasible,
        reject_reason=reason,
    )


def resolve_temporal_graph(
    edges: Sequence[TemporalEdgeCost],
    *,
    ambiguity_margin: float = 0.3,
    max_cost: float = _FORBIDDEN,
) -> tuple[dict[str, str], set[str]]:
    """시간 그래프 간선 후보들을 헝가리안 1:1 배정으로 풀어 `(track_id -> association_id, 애매한 track_id 집합)` 을 돌려준다."""
    feasible_edges = [e for e in edges if e.feasible and math.isfinite(e.total_cost) and e.total_cost < max_cost]
    if not feasible_edges:
        return {}, set()
    track_ids = sorted({e.track_id for e in feasible_edges})
    assoc_ids = sorted({e.association_id for e in feasible_edges})
    by_pair = {(e.track_id, e.association_id): e.total_cost for e in feasible_edges}
    matrix = [[by_pair.get((tid, aid), _FORBIDDEN) for aid in assoc_ids] for tid in track_ids]
    matched: dict[str, str] = {}
    for r, c in linear_sum_assignment(matrix):
        tid, aid = track_ids[r], assoc_ids[c]
        val = by_pair.get((tid, aid), math.inf)
        if math.isfinite(val) and val < max_cost:
            matched[tid] = aid
    ambiguous: set[str] = set()
    for tid, aid in matched.items():
        chosen = by_pair[(tid, aid)]
        rival_t = [o for o in track_ids if o != tid and by_pair.get((o, aid), math.inf) - chosen < ambiguity_margin]
        rival_a = [o for o in assoc_ids if o != aid and by_pair.get((tid, o), math.inf) - chosen < ambiguity_margin]
        if rival_t or rival_a:
            ambiguous.add(tid)
            ambiguous.update(rival_t)
    return matched, ambiguous


class WorldTrackManager:
    """공통 시각마다 Cross-view 결과로 World Track 을 갱신한다. 시각은 늘어나는 순서로만 넣는다."""

    def __init__(
        self,
        issuer: WorldIdIssuer,
        config: WorldTrackConfig | None = None,
        input_provenance: Mapping[str, Any] | None = None,
        appearance_cost_fn: AppearanceCostFn | None = None,
        feature_lookup: ReidFeatureLookup | None = None,
    ) -> None:
        self.issuer = issuer
        self.config = config if config is not None else WorldTrackConfig()
        self.input_provenance = dict(input_provenance) if input_provenance is not None else None
        self.appearance_cost_fn = appearance_cost_fn
        self.feature_lookup = feature_lookup
        self._tick_features: dict[TrackKey, LocalTrackFeature | None] = {}
        self._gallery_sim_memo: dict[tuple[str, tuple[TrackKey, ...]], tuple[Any, int, float | None]] = {}
        """이번 tick (World ID, 클러스터 키) → (갤러리, 그 version, 유사도). tick 특징을 다시 읽을 때 비운다(S15P31A101-663)"""
        self.galleries = ReidGalleryStore(
            GalleryConfig(dormant_ttl_ms=max(1, self.config.reid_dormant_ttl_ms))
        )
        self._tracks: dict[str, _Track] = {}
        self._dormant: dict[str, _Track] = {}
        """소실 후보: LOST 를 한 번 낸 CONFIRMED Track (늦은 재연결용. world_state 에는 내지 않는다)"""
        self._reid_dormant: dict[str, _Track] = {}
        """장기 보류(DORMANT) Track: reconnect_window_ms 뒤에도 reid_dormant_ttl_ms 동안 재입장 후보로 유지한다(world_state 미노출)"""
        self._recent_proximity: dict[frozenset[str], int] = {}
        """최근 교차·근접한 Track 쌍 → 마지막 근접 시각(ms)"""
        self._swap_counts: dict[tuple[str, str], int] = {}
        """연속 교차 유사도 우위 횟수 (wid_a, wid_b) -> count"""
        self._revisions: list[IdentityRevision] = []
        self._duplicates: set[frozenset[str]] = set()
        """같은 사람 두 ID 로 보이는 World ID 쌍 (POSSIBLE_DUPLICATE 를 다시 붙인다)"""
        self._distinct: dict[frozenset[str], int] = {}
        """같은 시각 같은 카메라의 다른 검출로 따로 보인(또는 도달 불가로 관측된) World ID 쌍 → 마지막 근거 시각. 서로 다른 두
        사람이라 distinct_ttl_ms 동안 병합·억제하지 않는다(-657 전에는 시간 제한 없음)"""
        self._distinct_fixed: set[frozenset[str]] = set()
        """분할로 생긴 병합 금지 쌍. 시간과 무관하게 남는다(진동 방지, S15P31A101-201)"""
        self._shadow_pairs: dict[frozenset[str], _ShadowEvidence] = {}
        """동시 활성 두 Track 의 시점 분할 근거 (-657). key_ttl_ms 넘게 끊기면 버린다"""
        self._twins: dict[frozenset[str], int] = {}
        """시점 분할 근거가 한 번이라도 있었던 쌍(분신) → 마지막 근거 시각. 둘 다 살아 있는 동안(소실·장기 보류 포함) 남겨,
        재입장 Re-ID 경쟁자·늦은 재연결 crowd 판정에서 서로를 빼는 데 쓴다(14차: 분신끼리 margin 을 0 으로 만들어 재입장 실패).
        그 뒤 병합 금지 근거가 생기면 분신으로 보지 않는다 (-657)"""
        self._rig: CameraRig | None = None
        """이번 update 의 보정 (그림자 판정 전용)"""
        self._evidence: dict[str, _Evidence] = {}
        """갱신된 Track ID → 병합 근거 (갱신되지 않은 Track 의 로컬 트랙이 이어진 시각 수)"""
        self._splits: dict[str, _Split] = {}
        """갈라져 나간 새 Track ID → 분할 후보 (S15P31A101-201)"""
        self._last_t: int | None = None
        self._order = 0
        self._restore_input: tuple[tuple[TrackCheckpoint, ...], int | None] | None = None
        self._restored: dict[str, TrackCheckpoint] = {}
        """후보로 남은 복원 Track (S15P31A101-54). 연결·world_state·export_tracks 에 넣지 않는다"""
        self._restore_status = RestoreStatus()
        self.diag: Counter[str] = Counter()
        """분기 사유별 누계(계측 전용, S15P31A101-413). 판정에 쓰지 않는다. `tracking.diagnostics` 가 읽는다"""

    @property
    def revisions(self) -> tuple[IdentityRevision, ...]:
        """지금까지의 ID 교정 기록 (추가만 한다)."""
        return tuple(self._revisions)

    @property
    def duplicate_pair_count(self) -> int:
        """지금 중복 의심 쌍 수: 시점 분할 근거가 이어지는 동시 활성 쌍과 POSSIBLE_DUPLICATE 쌍 (계측 전용, -657)."""
        return len(set(self._shadow_pairs) | self._duplicates)

    @property
    def lost_candidates(self) -> tuple[str, ...]:
        """늦은 재연결을 기다리는 소실 후보 World ID."""
        return tuple(sorted(self._dormant))

    @property
    def dormant_candidates(self) -> tuple[str, ...]:
        """장기 보류(DORMANT) 재입장 후보 World ID (world_state 에는 내보내지 않는다)."""
        return tuple(sorted(self._reid_dormant))

    @property
    def restore_status(self) -> RestoreStatus:
        """복원 Track 처리 상태 (S15P31A101-54)."""
        return self._restore_status

    @property
    def restored_alive(self) -> tuple[str, ...]:
        """지금 후보로 남은 복원 Track 의 World ID."""
        return tuple(sorted(self._restored))

    def restore(self, tracks: Iterable[TrackCheckpoint], tracks_t: int | None) -> None:
        """재시작 복구: 체크포인트의 Track 을 재관측과 맞출 후보로 받는다(명세 3.2·부록 C, S15P31A101-54).

        첫 update 전에 한 번만 부른다. 받을지는 첫 프레임 시각으로 정한다: 첫 프레임 시각 − tracks_t 가
        restore_max_age_ms 보다 크거나, 음수(시각 역행)거나, Track 이 있는데 tracks_t 를 모르면 버린다(모르면 비운다).
        복원 Track 은 후보일 뿐 위치 정본이 아니다. world_state·export_tracks·연결·필터에 넣지 않고 World ID 도 다시 쓰지
        않는다. 이번 세션 발급기의 접두어로 시작하는 World ID 는 받지 않는다(다른 세션의 Track 이어야 한다).
        """
        if self._last_t is not None or self._restore_input is not None:
            raise ValueError("restore 는 첫 update 전에 한 번만 부른다")
        tracks = tuple(tracks)
        if tracks_t is not None:
            require_ms("tracks_t", tracks_t)
        own = f"w-{self.issuer.session}-"
        seen: set[str] = set()
        for tr in tracks:
            if not isinstance(tr, TrackCheckpoint):
                raise TypeError("restore 는 TrackCheckpoint 만 받는다")
            if tr.world_id.startswith(own):
                raise ValueError(f"이번 세션의 World ID 는 복원 Track 이 될 수 없다: {tr.world_id}")
            if tr.world_id in seen:
                raise ValueError(f"같은 World ID 의 복원 Track 이 둘 이상이다: {tr.world_id}")
            seen.add(tr.world_id)
        self._restore_input = (tracks, tracks_t)
        self._restore_status = RestoreStatus(RestoreState.PENDING, tracks_t=tracks_t)

    @property
    def last_t(self) -> int | None:
        """마지막으로 넣은 공통 시각. 아직 없으면 None."""
        return self._last_t

    def temporal_prior_groups(
        self, t: int | None = None, ttl_ms: int | None = None
    ) -> tuple[frozenset[TrackKey], ...]:
        """최근 창(`ttl_ms`) 안에 같은 활성 World Track 에 속했던 카메라별 최신 로컬 트랙 묶음 (S15P31A101-464)."""
        ref_t = self._last_t if t is None else require_ms("t", t)
        if ref_t is None:
            return ()
        window = self.config.split_suppression_owner_ttl_ms if ttl_ms is None else int(ttl_ms)
        groups: list[frozenset[TrackKey]] = []
        for tr in sorted(self._tracks.values(), key=lambda item: item.order):
            by_cam: dict[str, tuple[int, TrackKey]] = {}
            for k, kt in tr.keys.items():
                if ref_t - kt <= window:
                    prev = by_cam.get(k[0])
                    if prev is None or (kt, k) > prev:
                        by_cam[k[0]] = (kt, k)
            if len(by_cam) >= 2:
                groups.append(frozenset(k for _, k in by_cam.values()))
        return tuple(groups)

    def export_tracks(self) -> tuple[TrackCheckpoint, ...]:
        """체크포인트용 Track 상태(명세 3.2 재시작 복구, S15P31A101-51). 살아 있는 Track 만(소실 후보 제외), world_id 순서.

        상태를 바꾸지 않는다. restore 로 받은 복원 Track(후보)은 내보내지 않는다(위치 정본이 아니다). 재관측 매칭은
        `restore`(S15P31A101-54).
        """
        out = []
        for tr in sorted(self._tracks.values(), key=lambda tr: tr.world_id):
            sigma = tr.kf.sigma_xyz
            out.append(
                TrackCheckpoint(
                    world_id=tr.world_id,
                    created_t=tr.created_t,
                    t_last_valid=tr.t_last_valid,
                    lifecycle=tr.lifecycle,
                    identity_revision=tr.identity_revision,
                    aliases=tuple(tr.aliases),
                    position_xyz=(float(tr.kf.x[0]), float(tr.kf.x[1]), float(tr.kf.x[2])),
                    filter_sigma_xyz=(float(sigma[0]), float(sigma[1]), float(sigma[2])),
                    velocity=(float(tr.kf.x[3]), float(tr.kf.x[4]), float(tr.kf.x[5])),
                )
            )
        return tuple(out)

    def update(
        self, frame: AssociationFrame, rig: CameraRig, sync_invalid_cameras: Collection[str] = ()
    ) -> WorldTrackFrame:
        """Cross-view 결과 하나로 World Track 을 갱신한다.

        sync_invalid_cameras: 동기화가 무효인 카메라. Cross-view 에 넘긴 `exclude_cameras` 와 같은 값을 넘긴다.
        관절 유효성 게이트가 그 카메라 관측을 삼각측량과 경쟁 후보 검사에서 뺀다.
        """
        cfg = self.config
        t = require_ms("frame.t", frame.t)
        if self._last_t is not None and t <= self._last_t:
            raise ValueError(f"시각은 늘어나는 순서로 넣는다: 직전 {self._last_t}, 받은 값 {t}")
        if frame.calibration_version != rig.calibration_version:
            raise ValueError(f"보정 버전이 다르다: 연결 {frame.calibration_version}, rig {rig.calibration_version}")
        if isinstance(sync_invalid_cameras, str):
            raise TypeError("sync_invalid_cameras 는 카메라 ID 모음이어야 한다")
        excluded = frozenset(sync_invalid_cameras)
        _check_clusters(frame)
        # 비동기 결과는 tick 경계에서 로컬 키당 한 번만 읽는다. 같은 tick의 후보/갤러리가
        # 서로 다른 임베딩을 보지 않으며, 이후 도착한 결과는 다음 tick에 반영한다(-499).
        self._tick_features = {}
        self._gallery_sim_memo = {}
        if self.feature_lookup is not None and cfg.reid_enabled:
            for key in sorted({key for cluster in frame.clusters for key in cluster.keys}):
                self._tick_features[key] = self.feature_lookup(key[0], key[1], t)
        self._last_t = t
        self._rig = rig
        self._restore_step(t)
        self._distinct = {p: pt for p, pt in self._distinct.items() if t - pt <= cfg.distinct_ttl_ms}
        self._shadow_pairs = {p: ev for p, ev in self._shadow_pairs.items() if t - ev.last_t <= cfg.key_ttl_ms}

        keep_ms = max(cfg.key_ttl_ms, cfg.reconnect_window_ms, cfg.split_suppression_owner_ttl_ms)
        for tr in self._all():
            tr.keys = {k: kt for k, kt in tr.keys.items() if t - kt <= keep_ms}
            tr.path = [(pt, xy) for pt, xy in tr.path if t - pt <= cfg.reconnect_window_ms]
        for wid in [w for w, tr in self._dormant.items() if t - tr.t_last_valid > cfg.reconnect_window_ms]:
            expired = self._dormant.pop(wid)
            if (
                cfg.reid_enabled
                and cfg.reid_dormant_enabled
                and cfg.reid_dormant_ttl_ms > cfg.reconnect_window_ms
                and t - expired.t_last_valid <= cfg.reid_dormant_ttl_ms
            ):
                self._reid_dormant[wid] = expired
                self.galleries.mark_dormant(wid, expired.t_last_valid)
                self.diag["lifecycle.dormant"] += 1
            else:
                self.galleries.remove(wid)
        for wid in [w for w, tr in self._reid_dormant.items() if t - tr.t_last_valid > cfg.reid_dormant_ttl_ms]:
            del self._reid_dormant[wid]
            self.galleries.remove(wid)
            self.diag["lifecycle.dormant_expired"] += 1
        self.galleries.prune_expired(t)
        self._forget_gone()
        # 비용 보너스·주인 판단은 key_ttl_ms 안의 연결 이력만, 늦은 재연결 근거는 소실 후보까지 남긴 이력 전부
        owner = {
            k: tr.world_id for tr in self._tracks.values() for k, kt in tr.keys.items() if t - kt <= cfg.key_ttl_ms
        }
        holder = {k: tr.world_id for tr in self._all() for k in tr.keys}
        # 이번 tick 의 소유권 이동·1대 관측 이력 갱신 전 스냅샷. 새 ID 가 같은 tick 의 주인이 되는 일을 막는다.
        recent_owner = {
            k: tr.world_id for tr in self._all() for k, kt in tr.keys.items()
            if t - kt <= cfg.split_suppression_owner_ttl_ms
        }
        held: dict[str, set[TrackKey]] = {}
        for k, wid in holder.items():
            held.setdefault(wid, set()).add(k)
        cluster_of = {k: c for c in frame.clusters for k in c.keys}
        clusters = [_prepare(c) for c in frame.clusters]
        tracks = sorted(self._tracks.values(), key=lambda tr: tr.order)
        predicted = {tr.world_id: tr.kf.predict_xy(t) for tr in tracks}

        def cost(tr: _Track, c: _Cluster) -> float:
            """게이트 밖이면 inf."""
            assert c.anchor is not None
            # 중간에 빈 tick 이 없어도 창 만료를 적용한다. 마지막 관측 시각만 근거로 삼는다.
            if tr.lifecycle is not TrackLifecycle.TENTATIVE and t - tr.t_last_valid > cfg.lost_after_ms:
                return math.inf
            keys = c.cluster.keys
            shared = sum(1 for k in keys if owner.get(k) == tr.world_id)
            if self.appearance_cost_fn is not None:
                app_cost = float(self.appearance_cost_fn(tr.world_id, c.cluster, t))
            elif self.feature_lookup is not None and cfg.reid_enabled:
                sim = self._cluster_gallery_sim(tr.world_id, c.cluster, t)
                app_cost = (1.0 - sim) * 0.35 if sim is not None else 0.0
            else:
                app_cost = 0.0
            edge = temporal_edge_cost(
                tr.world_id,
                predicted[tr.world_id],
                tr.t_last_valid,
                c.cluster,
                c.anchor,
                t,
                cfg,
                shared_keys=shared,
                appearance_cost=app_cost,
                clamp_gate=True,
            )
            return edge.total_cost

        anchored = [c for c in clusters if c.anchor is not None]
        self.diag["cluster.single_view"] += sum(1 for c in clusters if c.cluster.skeleton is None)
        self.diag["cluster.no_anchor"] += sum(1 for c in clusters if c.cluster.skeleton is not None and c.anchor is None)
        self.diag["cluster.no_pelvis"] += sum(1 for c in anchored if not c.has_pelvis)
        costs = {(tr.world_id, id(c)): cost(tr, c) for tr in tracks for c in anchored}
        assigned: dict[str, _Cluster] = {}
        taken: set[int] = set()
        blocked: set[int] = set()
        """새 ID 를 만들지 않을 클러스터 (애매해서 붙이지 않음)"""
        flags: dict[str, set[IdentityFlag]] = {tr.world_id: set() for tr in tracks}

        # 3단계: IDENTITY_UNCERTAIN 이 아닌 클러스터의 1:1 배정
        certain = [c for c in anchored if not c.uncertain]
        if tracks and certain:
            matrix = [[min(costs[(tr.world_id, id(c))], _FORBIDDEN) for c in certain] for tr in tracks]
            for r, col in linear_sum_assignment(matrix):
                tr, c = tracks[r], certain[col]
                chosen = costs[(tr.world_id, id(c))]
                if not math.isfinite(chosen):
                    continue
                assigned[tr.world_id] = c
                taken.add(id(c))
        # -684 ID-02: AMBIGUOUS_TRACK_MATCH 의 원인이 "아직 아무 Track 도 받지 않은 경쟁 클러스터"뿐인 Track 과 그 클러스터들.
        # 6단계에서 그 클러스터가 모두 이 Track 의 시점 분할 그림자로 확인되면 같은 사람을 두 묶음이 본 것이라 사유를 지운다
        ambiguity_rivals: dict[str, set[int]] = {}
        ambiguity_fixed: set[str] = set()
        """다른 Track 과 다퉈 애매한 Track (그림자 확인으로 지우지 않는다)"""
        for wid, c in assigned.items():
            chosen = costs[(wid, id(c))]
            rival_tracks = [
                tr.world_id
                for tr in tracks
                if tr.world_id != wid and costs[(tr.world_id, id(c))] - chosen < cfg.ambiguity_margin
            ]
            rival_clusters = [
                o for o in anchored if o is not c and costs[(wid, id(o))] - chosen < cfg.ambiguity_margin
            ]
            if rival_tracks or rival_clusters:
                flags[wid].add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
                if rival_tracks or any(oc is o for o in rival_clusters for oc in assigned.values()):
                    ambiguity_fixed.add(wid)
                else:
                    ambiguity_rivals.setdefault(wid, set()).update(id(o) for o in rival_clusters)
                for other in rival_tracks:
                    flags[other].add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
                    ambiguity_fixed.add(other)
                for o in rival_clusters:
                    for other, oc in assigned.items():
                        if oc is o:
                            flags[other].add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
                            ambiguity_fixed.add(other)

        # 4단계: IDENTITY_UNCERTAIN 클러스터는 후보가 하나뿐일 때만 붙인다
        for c in anchored:
            if not c.uncertain:
                continue
            # 이번 시각에 이미 다른 클러스터를 받은 Track 은 이 클러스터의 사람일 수 없다
            feasible = [
                tr for tr in tracks if tr.world_id not in assigned and math.isfinite(costs[(tr.world_id, id(c))])
            ]
            if not feasible:
                # 게이트 안에 남은 Track 이 없다: 6단계에서 IDENTITY_UNCERTAIN 인 새 ID (사람을 빠뜨리지 않는다).
                # 단 로컬 트랙이 이번 시각에 이미 갱신된 Track 의 것이면 그 사람의 갈라진 나머지 view 라 만들지 않는다
                # (그 Track 이 클러스터 가까이 있을 때만. 로컬 트래커가 옛 ID 를 다른 사람에게 넘긴 경우는 막지 않는다)
                near_owners = {
                    owner[k]
                    for k in c.cluster.keys
                    if owner.get(k) in assigned
                    and np.linalg.norm(c.anchor - predicted[owner[k]]) <= cfg.duplicate_radius_m
                }
                if near_owners and self.feature_lookup is not None and cfg.reid_enabled:
                    filtered_owners: set[str] = set()
                    for wid in near_owners:
                        sim = self._cluster_track_sim(wid, c.cluster, assigned[wid].cluster, t)
                        if sim is not None and sim < cfg.reid_split_different_max_sim:
                            self.diag["reid.split_preserved_different"] += 1
                            continue
                        filtered_owners.add(wid)
                    near_owners = filtered_owners
                self.diag["uncertain.blocked_near_owner" if near_owners else "uncertain.no_track_in_gate"] += 1
                if near_owners:
                    blocked.add(id(c))
                    # 막을 때도 표시는 남긴다 (명세 5.2: 새 ID 또는 IDENTITY_UNCERTAIN). 옆에 새 사람일 수도 있다
                    for wid in near_owners:
                        flags[wid].add(IdentityFlag.CROSS_VIEW_UNCERTAIN)
                continue
            blocked.add(id(c))
            best = min(feasible, key=lambda tr: costs[(tr.world_id, id(c))])
            chosen = costs[(best.world_id, id(c))]
            rivals = [
                tr for tr in feasible if tr is not best and costs[(tr.world_id, id(c))] - chosen < cfg.ambiguity_margin
            ]
            foreign = any(owner.get(k) not in (None, best.world_id) for k in c.cluster.keys)
            self.diag["uncertain.attached" if not rivals and not foreign else "uncertain.ambiguous_not_attached"] += 1
            if not rivals and not foreign:
                assigned[best.world_id] = c
                taken.add(id(c))
                flags[best.world_id].add(IdentityFlag.CROSS_VIEW_UNCERTAIN)
            else:
                for tr in feasible:
                    flags[tr.world_id].add(IdentityFlag.CROSS_VIEW_UNCERTAIN)

        # 교차·근접 뒤 swap 판정·복구 (S15P31A101-463)
        self._check_and_revert_swaps(assigned, predicted, flags, t)

        # 7단계: 같은 사람 두 ID 로 보이는 Track 은 이 시각에는 합치지 않고 둘 다 남긴 채 표시한다.
        # 로컬 트랙 ID 하나가 옆 사람에게 넘어간 경우(로컬 트래커 ID 전환)와 한 시각만으로는 구별할 근거가 없기 때문이다.
        for wid, c in assigned.items():
            for k in c.cluster.keys:
                other_id = owner.get(k)
                if other_id is None or other_id == wid or other_id in assigned:
                    continue
                if np.linalg.norm(c.anchor - predicted[other_id]) <= cfg.duplicate_radius_m:
                    self._duplicates.add(frozenset((wid, other_id)))

        # 같은 시각 같은 카메라의 서로 다른 검출은 서로 다른 사람이다: 위치 갱신과 상관없이 로컬 트랙 주인과 배정으로
        # 병합 금지 쌍을 먼저 기록한다 (1대 클러스터로만 보인 Track 포함)
        self._mark_distinct(frame, holder, {wid: c.cluster for wid, c in assigned.items()})

        # 병합 근거 (S15P31A101-40): 갱신된 Track 에 갱신되지 않은 Track 의 로컬 트랙이 이어진 시각 수를 센다.
        # 근거 로컬 트랙이 잠깐 빠진 시각(key_ttl_ms 안)은 세지 않고 넘기고, 반대 근거가 나오면 버린다
        stale_ids = {tr.world_id for tr in tracks if tr.world_id not in assigned} | set(self._dormant)
        found: dict[str, _Evidence] = {}
        vetoed: set[str] = set()
        for wid, c in assigned.items():
            ev, veto = self._merge_evidence(wid, c, holder, held, cluster_of, stale_ids, t)
            if ev is not None:
                found[wid] = ev
            elif veto:
                vetoed.add(wid)
        for wid, ev in self._evidence.items():
            if wid not in found and wid not in vetoed and ev.stale in stale_ids and t - ev.last_t <= cfg.key_ttl_ms:
                found[wid] = ev
        claims = Counter(ev.stale for ev in found.values())
        self._evidence = {wid: ev for wid, ev in found.items() if claims[ev.stale] == 1}  # 두 Track 이 다투면 애매

        # 갱신 적용 (관절 유효성 게이트 → 칼만 필터)
        applied: dict[str, _Applied] = {}
        for wid, c in assigned.items():
            tr = self._tracks[wid]
            reacquired = not any(owner.get(k) == wid for k in c.cluster.keys)
            result = self._apply(tr, c.cluster, frame, rig, excluded, t)
            applied[wid] = result
            for k in c.cluster.keys:
                self._take_key(k, tr, t, set(assigned))
                owner[k] = wid
            if _competitor_flag(result.gated, c.cluster, cfg):
                flags[wid].add(IdentityFlag.TWO_VIEW_COMPETITOR_JOINT)
            if result.pelvis is not None and reacquired and tr.lifecycle is TrackLifecycle.OCCLUDED:
                tr.reacquired_until = t + cfg.uncertain_hold_ms

        # 5단계: 1대뿐인 클러스터(와 붙지 않은 클러스터)는 위치를 갱신하지 않고 연결 이력만 잇는다 (소실 후보 포함)
        current = {k: tr for tr in self._all() for k in tr.keys}
        for c in clusters:
            if id(c) in taken:
                continue
            for k in c.cluster.keys:
                tr = current.get(k)
                if tr is not None and tr.world_id not in assigned:
                    tr.keys[k] = t

        # 6단계: 새 World Track. 소실·가림 Track 의 로컬 트랙이면 먼저 같은 ID 로 잇는다(늦은 재연결).
        # 기존 Track(예측 위치)과 이번 시각에 먼저 생긴 Track 가까이면 POSSIBLE_DUPLICATE
        reassociated: list[str] = []
        linked: set[str] = set()
        """이번 시각 늦은 재연결·병합한 Track (일관성 확인은 다음 시각부터 센다)"""
        nearby: dict[str, np.ndarray] = dict(predicted)
        for c in anchored:
            if id(c) in taken or id(c) in blocked or not c.has_pelvis:
                continue
            # -657: 확정 Track 의 시점 분할 그림자(카메라 비겹침 + 방향별 반경 + 한 점 재투영)는 늦은 재연결·새 ID 보다 먼저
            # 거른다. 그 사람이 이번 시각 이미 갱신된 확정 Track 이므로 다른 ID 로 잇거나 새로 만들면 한 사람 두 ID 다.
            # 두 묶음이 한 점으로 맞으므로 신원이 애매하지 않아 IDENTITY_UNCERTAIN 은 붙이지 않는다(설계 문서).
            shadow_of = self._shadow_target(c, applied, holder, cluster_of) if cfg.shadow_suppression_enabled else None
            if shadow_of is not None:
                rivals = ambiguity_rivals.get(shadow_of)
                if rivals is not None:
                    rivals.discard(id(c))
                self.diag["new_track.suppressed_disjoint_shadow"] += 1
                continue  # ID·위치·로컬 트랙 소유권은 바꾸지 않는다
            target, ambiguous, _unreachable = self._reconnect_target(c, holder, held, cluster_of, applied, t)
            reid_reconnected = False
            if target is None and self.feature_lookup is not None and cfg.reid_enabled:
                reid_target, reid_ambig = self._reid_reconnect_target(
                    c, holder, held, cluster_of, applied, t
                )
                if reid_target is not None:
                    target = reid_target
                    ambiguous = False
                    reid_reconnected = True
                elif reid_ambig:
                    ambiguous = True
            if target is not None:
                was_lost = target in self._dormant or target in self._reid_dormant
                result = self._reconnect(target, c.cluster, frame, rig, excluded, t)
                self.diag["reconnect.done" if result is not None else "reconnect.no_measurement"] += 1
                if reid_reconnected and result is not None:
                    self.diag["reid.reentry_reconnected"] += 1
                if result is not None:
                    tr = self._tracks[target]
                    applied[target] = result
                    taken.add(id(c))
                    for k in c.cluster.keys:
                        self._take_key(k, tr, t, set(assigned) | set(applied))
                        owner[k] = target
                    tr.confirm_left = cfg.merge_confirm_frames
                    linked.add(target)
                    new_flags = flags.setdefault(target, set())
                    if _competitor_flag(result.gated, c.cluster, cfg):
                        new_flags.add(IdentityFlag.TWO_VIEW_COMPETITOR_JOINT)
                    if was_lost:
                        reassociated.append(target)
                    if result.pelvis is not None:
                        nearby[target] = np.array(result.pelvis[:2])
                    continue
            gate = JointGate(cfg.joint_gate)
            gated = self._gate(gate, c.cluster, frame, rig, excluded)
            measurement = self._measurement(gated)
            if measurement is None:
                self.diag["new_track.no_measurement"] += 1
                continue
            z, sigma = measurement
            suppressed_by = self._split_remainder_target(c, applied, holder, cluster_of, recent_owner, t)
            if suppressed_by is not None:
                flags.setdefault(suppressed_by, set()).add(IdentityFlag.CROSS_VIEW_UNCERTAIN)
                self.diag["new_track.split_remainder_suppressed"] += 1
                continue  # ID·위치·로컬 트랙 소유권은 바꾸지 않는다
            self.diag["new_track.created"] += 1
            self._order += 1
            tr = _Track(
                world_id=self.issuer.issue(),
                created_t=t,
                order=self._order,
                kf=_Kalman(cfg, t, z, sigma),
                gate=gate,
                t_last_valid=t,
            )
            tr.path.append((t, z[:2].copy()))
            self._relate_restored(tr, z[:2], t)
            self._tracks[tr.world_id] = tr
            for k in c.cluster.keys:
                self._take_key(k, tr, t, set(assigned) | set(applied))
                owner[k] = tr.world_id
            # -657: 첫 관측이 옛 주인의 마지막 위치에서 갈 수 없는 거리여도 병합 금지 쌍으로 두지 않는다. 예각 2대의 깊이 오차
            # (0.5 m 넘음)로 태어난 그림자를 영구히 다른 사람으로 묶어 동시 활성 병합·분할 억제를 막았다. 거리만으로는 ID 전환과
            # 깊이 오차를 구별할 수 없고, 같은 카메라 동시 검출이 진짜 반증이다(그건 _mark_distinct 가 기록한다)
            applied[tr.world_id] = _Applied(c.cluster, gated, (float(z[0]), float(z[1]), float(z[2])))
            self._split_candidate(tr.world_id, c, holder, applied, t)
            new_flags = set()
            if c.uncertain:
                new_flags.add(IdentityFlag.CROSS_VIEW_UNCERTAIN)
            if _competitor_flag(gated, c.cluster, cfg):
                new_flags.add(IdentityFlag.TWO_VIEW_COMPETITOR_JOINT)
            if ambiguous:
                new_flags.add(IdentityFlag.REASSOCIATION_AMBIGUOUS)
            for other_id, xy in nearby.items():
                if np.linalg.norm(c.anchor - xy) <= cfg.duplicate_radius_m:
                    self._duplicates.add(frozenset((tr.world_id, other_id)))
            flags[tr.world_id] = new_flags
            nearby[tr.world_id] = z[:2]

        if cfg.stature_enabled:
            self._update_stature(applied)

        # 병합 (ID 교정): 근거 시각이 merge_confirm_frames 번 쌓였으면 먼저 만든 ID 로 합친다
        revisions: list[IdentityRevision] = []
        for wid, ev in sorted(self._evidence.items()):
            if ev.count < cfg.merge_confirm_frames or wid not in self._evidence:
                continue
            if ev.stale in applied or wid not in self._tracks or self._find(ev.stale) is None:
                del self._evidence[wid]  # 이번 시각 따로 갱신됐거나 사라졌다
                continue
            rec = self._merge(ev.stale, wid, ev, t, applied, flags)
            self.diag["revision.merge"] += 1
            revisions.append(rec)
            linked.add(rec.world_id)

        # 비동기 Re-ID 지연 도착 재입장 병합 (S15P31A101-463): 첫 프레임에 특징이 아직 없던 새 Track 이 직후 특징을 얻었을 때
        if self.feature_lookup is not None and cfg.reid_enabled:
            for rec, was_lost in self._reid_late_reentry_merge(applied, held, cluster_of, flags, t):
                self.diag["revision.merge"] += 1
                self.diag["reid.reentry_merged"] += 1
                revisions.append(rec)
                linked.add(rec.world_id)
                if was_lost and rec.world_id not in reassociated:
                    reassociated.append(rec.world_id)

        # 병합 금지 쌍: 이번 시각 새로 생기거나 다시 이은 Track 까지 (다음 시각 근거에 쓴다)
        self._mark_distinct(frame, holder, {wid: a.cluster for wid, a in applied.items()})

        # 동시 활성 중복 병합 (-657): 둘 다 이번 시각 갱신된 두 Track 이 같은 사람의 시점 분할로 연속해 보이면 합친다.
        # 기존 병합(_merge)을 그대로 써서 aliases·MERGE 교정 기록이 워치 배정·이벤트 승계로 이어진다
        for rec in self._active_duplicate_merges(applied, holder, cluster_of, flags, t):
            self.diag["revision.merge"] += 1
            self.diag["revision.active_merge"] += 1
            revisions.append(rec)
            linked.add(rec.world_id)

        # 분할 (ID 교정, S15P31A101-201): 떨어진 근거 시각이 merge_confirm_frames 번 쌓이면 새 ID 를 원래 ID 에서 가른다
        for rec in self._split_step(t, applied, holder, flags):
            self.diag["revision.split"] += 1
            revisions.append(rec)
            linked.update((rec.world_id, rec.previous_world_id))

        # 늦은 재연결·병합 뒤 일관성 확인: 자기 로컬 트랙만으로(reconnect_min_cameras 대 이상) 위치가 갱신된 시각을 센다.
        # 다 셀 때까지 LATE_REASSOCIATION 을 매 시각 붙인다. 넘겨받은 로컬 트랙의 옛 주인이 남아 있으면 애매 표시
        for wid, tr in self._tracks.items():
            own = wid not in linked and _own_update(applied.get(wid), holder, wid, cfg)
            if tr.confirm_left > 0 and own:
                tr.confirm_left -= 1
            if tr.split_left > 0 and own:
                tr.split_left -= 1
            if tr.confirm_left > 0:
                flags.setdefault(wid, set()).add(IdentityFlag.LATE_REASSOCIATION)
            if tr.split_left > 0:
                flags.setdefault(wid, set()).add(IdentityFlag.IDENTITY_SPLIT)
            if self._ambiguous(tr):
                flags.setdefault(wid, set()).add(IdentityFlag.REASSOCIATION_AMBIGUOUS)

        # 같은 사람 두 ID 후보 쌍: 둘 다 살아 있고 가까운 동안 매 시각 다시 표시한다(한 사람 두 ID 가 표시 없이 남지 않게)
        for pair in list(self._duplicates):
            a, b = sorted(pair)
            ta, tb = self._tracks.get(a), self._tracks.get(b)
            if ta is None or tb is None:
                self._duplicates.discard(pair)
                continue
            if np.linalg.norm(ta.kf.predict_xy(t) - tb.kf.predict_xy(t)) > cfg.duplicate_radius_m:
                self._duplicates.discard(pair)
                continue
            for wid in (a, b):
                flags.setdefault(wid, set()).add(IdentityFlag.POSSIBLE_DUPLICATE)

        # 재시작 뒤 복원 Track 후보: 그 복원 Track 이 남아 있는 동안 매 시각 표시한다(재시작 전의 그 사람일 수 있다)
        for wid, tr in self._tracks.items():
            if any(r in self._restored for r in tr.restored_candidates):
                flags.setdefault(wid, set()).add(IdentityFlag.RESTORED_TRACK_CANDIDATE)

        # -684 ID-02: 애매함의 원인이던 경쟁 클러스터가 모두 이 Track 의 그림자로 확인됐으면 그 사유를 지운다. 다른 Track 과
        # 다퉜거나(ambiguity_fixed), swap 의심 중이면(같은 사유를 swap 검사도 붙인다) 그대로 둔다
        if cfg.shadow_clears_ambiguity:
            for wid, rivals in ambiguity_rivals.items():
                tr = self._tracks.get(wid)
                if rivals or wid in ambiguity_fixed or tr is None or tr.swap_suspected_until >= t:
                    continue
                if IdentityFlag.AMBIGUOUS_TRACK_MATCH in flags.get(wid, ()):
                    flags[wid].discard(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
                    self.diag["identity.shadow_ambiguity_cleared"] += 1

        # -698 ID-5 보완: 관측 순간 사유는 300 ms 만 유지하지만, 맞바꿈 위험 중에 붙은 것은 상태 사유처럼 uncertain_hold_ms
        # 동안 유지한다. ID 맞바꿈은 여러 tick 에 걸쳐 일어나 짧은 유지로는 표시가 중간중간 꺼진다(2026-11-14 로비 녹화 497~506 s).
        # - C: swap 의심(swap_suspected_until) 중. 의심이 붙이는 AMBIGUOUS_TRACK_MATCH 가 의심 기간보다 먼저 꺼지지 않게 한다
        # - A: 위치 급변(jump_t == t). 뛴 Track 과 uncertain_jump_neighbor_m 안의 Track 에 AMBIGUOUS_TRACK_MATCH 를 붙인다
        # - B: 다른 사람(LOST 가 아닌 Track)이 uncertain_hold_near_m 안에 있음(0 이면 끔)
        risky: dict[str, str] = {}
        if cfg.uncertain_hold_per_reason:
            live = [(w, o) for w, o in self._tracks.items() if o.lifecycle is not TrackLifecycle.LOST]
            jumped = [o for _, o in live if o.jump_t == t]
            for j in jumped:
                for w, o in live:
                    if o is j or float(np.linalg.norm(o.kf.x[:2] - j.kf.x[:2])) <= cfg.uncertain_jump_neighbor_m:
                        flags.setdefault(w, set()).add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
                        risky[w] = "jump"
            for w, o in live:
                if w not in risky and o.swap_suspected_until >= t:
                    risky[w] = "swap_suspect"
            if cfg.uncertain_hold_near_m > 0:
                for w, fs in flags.items():
                    o = self._tracks.get(w)
                    if w in risky or o is None or not any(f in _TRANSIENT_FLAGS for f in fs):
                        continue
                    if any(w2 != w and float(np.linalg.norm(o2.kf.x[:2] - o.kf.x[:2])) <= cfg.uncertain_hold_near_m
                           for w2, o2 in live):
                        risky[w] = "near"
        # - B: 가려진(이번 시각 갱신 없음) 채 uncertain_hold_hidden_m 안에 다른 사람이 있으면, 이미 붙어 있는 관측 순간 사유를
        #   끄지 않는다(새 사유를 만들지는 않는다). 가려진 동안에는 사유가 다시 붙지 않아 300 ms 뒤 꺼지는데, 맞바꿈은 대개
        #   사람이 붙어 가려진 사이에 일어난다(로비 녹화 497~506 s 의 꺼진 tick 대부분)
        if cfg.uncertain_hold_per_reason and cfg.uncertain_hold_hidden_m > 0:
            live = [(w, o) for w, o in self._tracks.items() if o.lifecycle is not TrackLifecycle.LOST]
            for w, o in live:
                if o.t_last_valid >= t or not any(f in _TRANSIENT_FLAGS for f in o.flags):
                    continue
                if any(w2 != w and float(np.linalg.norm(o2.kf.x[:2] - o.kf.x[:2])) <= cfg.uncertain_hold_hidden_m
                       for w2, o2 in live):
                    o.hidden_hold_t = t
                    self.diag["identity.transient_hold_hidden"] += 1
        for wid, fs in flags.items():
            tr = self._tracks.get(wid)
            if tr is None:
                continue
            long_hold = wid in risky
            for f in fs:
                tr.flags[f] = t
                if long_hold and f in _TRANSIENT_FLAGS:
                    tr.long_hold_flags[f] = t
                else:
                    tr.long_hold_flags.pop(f, None)
            if long_hold:
                self.diag[f"identity.transient_hold_{risky[wid]}"] += 1

        # 확정 지연 (-657): 카메라가 겹치지 않는 확정 Track 근처에서 저신뢰로 갱신된 TENTATIVE 는 확정을 늦춘다(그림자일 수 있다)
        self._delay_confirmation(applied)

        # 생명주기
        newly_lost: list[tuple[_Track, bool]] = []
        for tr in list(self._tracks.values()):
            gap = t - tr.t_last_valid
            was_confirmed = tr.lifecycle is not TrackLifecycle.TENTATIVE
            if tr.lifecycle is TrackLifecycle.TENTATIVE:
                if gap == 0 and tr.hits >= max(cfg.confirm_hits, tr.confirm_needed):
                    tr.lifecycle = TrackLifecycle.CONFIRMED
                    self.diag["lifecycle.confirmed"] += 1
                elif gap > cfg.tentative_max_gap_ms:
                    tr.lifecycle = TrackLifecycle.LOST
                    self.diag["lifecycle.lost_tentative"] += 1
            elif gap == 0:
                tr.lifecycle = TrackLifecycle.CONFIRMED
            elif gap > cfg.lost_after_ms:
                tr.lifecycle = TrackLifecycle.LOST
                self.diag["lifecycle.lost_confirmed"] += 1
            elif gap > cfg.occluded_after_ms:
                tr.lifecycle = TrackLifecycle.OCCLUDED
            if tr.lifecycle is TrackLifecycle.LOST:
                newly_lost.append((tr, was_confirmed))

        # World Track 갤러리 갱신 및 경쟁 유사도 margin 계산 (S15P31A101-463)
        self._update_galleries(applied, t)

        snapshots = [self._snapshot(tr, t, applied.get(tr.world_id)) for tr in self._tracks.values()]
        for tr, was_confirmed in newly_lost:
            self._tracks.pop(tr.world_id, None)
            gap_lost = t - tr.t_last_valid
            if was_confirmed and cfg.reconnect_window_ms > cfg.lost_after_ms and gap_lost <= cfg.reconnect_window_ms:
                self._dormant[tr.world_id] = tr  # LOST 는 한 번만 내고 소실 후보로 둔다
            elif (
                was_confirmed
                and cfg.reid_enabled
                and cfg.reid_dormant_enabled
                and cfg.reid_dormant_ttl_ms > cfg.lost_after_ms
                and gap_lost <= cfg.reid_dormant_ttl_ms
            ):
                self._reid_dormant[tr.world_id] = tr
                self.galleries.mark_dormant(tr.world_id, tr.t_last_valid)
                self.diag["lifecycle.dormant"] += 1
            else:
                self.galleries.remove(tr.world_id)
        self._forget_gone()
        return WorldTrackFrame(
            t=t,
            calibration_version=frame.calibration_version,
            tracks=tuple(sorted(snapshots, key=lambda s: s.world_id)),
            revisions=tuple(revisions),
            input_provenance=self.input_provenance,
            reassociations=tuple(sorted(reassociated)),
        )

    # ------------------------------------------------------------------ 늦은 재연결·ID 교정

    def _split_remainder_target(
        self,
        c: _Cluster,
        applied: Mapping[str, _Applied],
        holder: Mapping[TrackKey, str],
        cluster_of: Mapping[TrackKey, ViewCluster],
        recent_owner: Mapping[TrackKey, str],
        t: int = 0,
    ) -> str | None:
        """S3: 새 ID 직전에만 검사한다. 예측 위치·옛 위치는 억제 근거로 쓰지 않는다.

        직전까지 근거 Track 에 속했던 키가 하나 이상인 나머지만 억제한다. 소유 이력은 tick 시작 시 고정한다.
        측정은 합치지 않는다. 기존 병합 금지 쌍과 이번 시각 같은 카메라의 두 검출은 억제의 반증이다.
        위치 갱신에 쓰이지 않은 1대 클러스터의 검출도 로컬 트랙 주인을 통해 반증으로 쓴다.
        가까운 후보가 여럿이면 거리·World ID 순으로 하나를 고르고 불확실 표시를 남긴다.
        Re-ID 특징이 있으면:
        - 외형이 명확히 다른 경우(`sim < reid_split_different_max_sim`) 억제를 막아 근접 두 사람(`<n`)을 보호한다.
        - 외형이 같은 사람으로 확인되면(`sim >= reid_split_same_min_sim`, margin 충족) 소유 이력이 끊겼거나
          `c.uncertain` 이거나 반경 `reid_split_max_radius_m` 안인 분리 나머지도 억제한다.
        """
        cfg = self.config
        reid_on = self.feature_lookup is not None and cfg.reid_enabled
        if not cfg.split_suppression_enabled or c.anchor is None:
            return None
        if c.uncertain and not reid_on:
            return None
        cameras = set(c.cluster.camera_ids)
        owners = {holder.get(k) for k in c.cluster.keys}
        candidates: list[tuple[float, str]] = []
        reid_scores: dict[str, float] = {}
        if reid_on:
            for wid, a in applied.items():
                sim = self._cluster_track_sim(wid, c.cluster, a.cluster, t)
                if sim is not None:
                    reid_scores[wid] = sim

        for wid, a in applied.items():
            if a.pelvis is None or cameras.intersection(a.cluster.camera_ids):
                continue
            visible = set(a.cluster.keys) | {k for k in cluster_of if holder.get(k) == wid}
            if any(k[0] == other[0] and k != other for k in c.cluster.keys for other in visible):
                continue  # 주인의 다른 검출이 1대 클러스터에만 있어도 두 사람이다
            distance = math.dist(c.anchor, a.pelvis[:2])
            has_recent_owner = any(recent_owner.get(k) == wid for k in c.cluster.keys)
            is_distinct = any(old is not None and self._is_distinct(old, wid) for old in owners)
            sim = reid_scores.get(wid)

            # 1) 외형이 명확히 다르면(< reid_split_different_max_sim) 억제를 취소하여 두 사람(<n)을 보호한다
            if reid_on and sim is not None and sim < cfg.reid_split_different_max_sim:
                if has_recent_owner and distance <= cfg.split_suppression_radius_m:
                    self.diag["reid.split_preserved_different"] += 1
                continue

            # 2) 기본 기하·소유자 조건 충족 시 (또는 c.uncertain 인데 최근 소유자 + 외형 모순 없음)
            if (
                (cfg.split_suppress_ownerless or has_recent_owner)
                and not is_distinct
                and (not cfg.split_suppression_require_owner or owners == {wid})
                and distance <= cfg.split_suppression_radius_m
            ):
                if not c.uncertain or (reid_on and has_recent_owner):
                    candidates.append((distance, wid))
                    continue

            # 3) Re-ID 외형 일치로 확인된 분리 나머지 억제 (소유 이력 만료 또는 최대 반경 reid_split_max_radius_m 내).
            # 외형이 같아도 동시 관측으로 이미 다른 사람이라고 확인된 쌍은 억제하지 않는다(-684 ID-01, 같은 복장 두 사람)
            if is_distinct and cfg.reid_split_respect_distinct:
                continue
            if reid_on and sim is not None and distance <= cfg.reid_split_max_radius_m:
                shadows = self._shadows_of(wid)  # 같은 사람의 분신 Track 은 경쟁자가 아니다(-657)
                best_other = max(
                    (s for other_w, s in reid_scores.items() if other_w != wid and other_w not in shadows), default=-1.0
                )
                margin = sim - best_other if best_other >= 0.0 else sim
                min_sim = cfg.reid_split_same_min_sim if not has_recent_owner else (cfg.reid_split_same_min_sim - 0.05)
                if sim >= min_sim and margin >= cfg.reid_split_margin:
                    self.diag["reid.split_suppressed_same"] += 1
                    candidates.append((distance, wid))
        return min(candidates)[1] if candidates else None

    # ------------------------------------------------------------------ 시점 분할 그림자 (-657)

    def _is_distinct(self, a: str, b: str) -> bool:
        """병합 금지 쌍인지: 같은 시각 같은 카메라의 별도 검출(또는 도달 불가 관측)을 distinct_ttl_ms 안에 봤거나 분할 쌍."""
        pair = frozenset((a, b))
        if pair in self._distinct_fixed:
            return True
        seen = self._distinct.get(pair)
        return seen is not None and (self._last_t is None or self._last_t - seen <= self.config.distinct_ttl_ms)

    def _shadows_of(self, world_id: str) -> set[str]:
        """world_id 의 분신(시점 분할 근거가 지금 있거나 있었던 Track, 병합 금지 쌍 제외). Re-ID 경쟁자·crowd 판정에서 뺀다."""
        out: set[str] = set()
        for pair in (*self._shadow_pairs, *self._twins):
            if world_id in pair:
                out |= pair - {world_id}
        return {w for w in out if not self._is_distinct(world_id, w)}

    def _undistorted(self, cluster: ViewCluster) -> list[UndistortedKeypoints]:
        """클러스터 view 의 왜곡 제거 결과 (Cross-view 가 푼 것을 다시 쓰고, 없으면 지금 푼다)."""
        assert self._rig is not None
        out = []
        for v in cluster.views:
            u = cluster.undistorted.get(v.camera_id)
            if u is None:
                u = self._rig.cameras[v.camera_id].undistort_keypoints(v.view.keypoints)
            out.append(u)
        return out

    def _directional_radius(
        self,
        cluster_a: ViewCluster,
        pelvis_a: Sequence[float],
        cluster_b: ViewCluster,
        pelvis_b: Sequence[float],
    ) -> float:
        """두 골반을 잇는 방향의 결합 삼각측량 표준편차로 정한 그림자 반경: clip(base + k·σ_dir, base, max).

        공분산은 각 클러스터 카메라로 그 골반을 풀 때의 σ²(JᵀJ)⁻¹(keypoint σ = shadow_keypoint_sigma_px)의 수평 2×2 블록이다.
        예각 2대의 깊이 방향은 σ 가 커서 반경이 넓어지고, 시선 수직 방향(옆 사람이 서는 쪽)은 base 근처로 좁게 남는다.
        공분산을 구하지 못하면(시선이 거의 평행) 깊이를 모르므로 상한을 쓴다.
        """
        cfg = self.config
        assert self._rig is not None
        delta = np.asarray(pelvis_a[:2], dtype=float) - np.asarray(pelvis_b[:2], dtype=float)
        dist = float(np.linalg.norm(delta))
        if dist < 1e-9:
            return cfg.shadow_radius_base_m
        u = delta / dist
        total = np.zeros((2, 2))
        for cluster, pelvis in ((cluster_a, pelvis_a), (cluster_b, pelvis_b)):
            cams = [self._rig.cameras[cid] for cid in cluster.camera_ids]
            cov = triangulation_covariance(cams, np.asarray(pelvis, dtype=float), cfg.shadow_keypoint_sigma_px)
            if cov is None or not np.all(np.isfinite(cov)):
                return cfg.shadow_radius_max_m
            total += cov[:2, :2]
        sigma = math.sqrt(max(float(u @ total @ u), 0.0))
        radius = cfg.shadow_radius_base_m + cfg.shadow_radius_sigma_k * sigma
        return min(max(radius, cfg.shadow_radius_base_m), cfg.shadow_radius_max_m)

    def _geometric_shadow(
        self,
        cluster_a: ViewCluster,
        pelvis_a: Sequence[float],
        cluster_b: ViewCluster,
        pelvis_b: Sequence[float],
    ) -> bool:
        """두 클러스터(카메라 비겹침)가 같은 사람을 따로 본 것으로 기하가 맞는지: 수평 거리 ≤ 방향별 반경이고, 두 묶음 view 를
        엉덩이마다 한 점으로 풀어도 재투영 오차가 shadow_reprojection_px 안이다. 모르면(관측 부족) 맞지 않는 것으로 본다."""
        cfg = self.config
        if set(cluster_a.camera_ids) & set(cluster_b.camera_ids):
            return False
        dist = math.dist(pelvis_a[:2], pelvis_b[:2])
        if dist > cfg.shadow_radius_max_m:
            return False
        if dist > self._directional_radius(cluster_a, pelvis_a, cluster_b, pelvis_b):
            return False
        err = joint_consistency_px(
            [self._undistorted(cluster_a), self._undistorted(cluster_b)],
            (COCO17_L_HIP, COCO17_R_HIP),
            cfg.shadow_min_keypoint_confidence,
        )
        return err is not None and err <= cfg.shadow_reprojection_px

    def _co_observed_now(
        self,
        keys: Iterable[TrackKey],
        world_id: str,
        own_keys: Iterable[TrackKey],
        holder: Mapping[TrackKey, str],
        cluster_of: Mapping[TrackKey, ViewCluster],
    ) -> bool:
        """이번 시각 같은 카메라에서 keys 와 world_id 의 다른 검출(받은 클러스터 또는 world_id 가 든 다른 로컬 트랙)이 따로 보였는지.

        안전장치(-657): 같은 시각 같은 카메라의 서로 다른 검출은 서로 다른 사람이라 절대 억제·병합하지 않는다.
        """
        visible = set(own_keys) | {k for k in cluster_of if holder.get(k) == world_id}
        return any(k[0] == other[0] and k != other for k in keys for other in visible)

    def _shadow_target(
        self,
        c: _Cluster,
        applied: Mapping[str, _Applied],
        holder: Mapping[TrackKey, str],
        cluster_of: Mapping[TrackKey, ViewCluster],
    ) -> str | None:
        """새 ID 후보 c 가 이번 시각 갱신된 확정 Track 의 시점 분할 그림자면 그 Track. 아니면 None.

        조건: 그 Track 이 확정(CONFIRMED·OCCLUDED, 이번 시각 만든 TENTATIVE 는 아님)이고 골반 측정으로 갱신됐으며 받은 클러스터와
        카메라가 하나도 겹치지 않는다. 반증(같은 시각 같은 카메라 별도 검출, c 의 옛 주인과의 병합 금지 쌍)이 없다. 기하가 맞는다
        (`_geometric_shadow`). 소유 이력·c 의 IDENTITY_UNCERTAIN 은 보지 않는다(주인 없는 그림자·불확실 그림자 구멍).
        여럿이면 가까운 것.
        """
        if c.anchor is None or c.cluster.skeleton is None:
            return None
        pelvis_c = c.cluster.skeleton.pelvis_xyz()
        if pelvis_c is None:
            return None
        owners = {o for o in (holder.get(k) for k in c.cluster.keys) if o is not None}
        cameras = set(c.cluster.camera_ids)
        found: list[tuple[float, str]] = []
        for wid, a in applied.items():
            tr = self._tracks.get(wid)
            if tr is None or tr.lifecycle is TrackLifecycle.TENTATIVE or a.pelvis is None:
                continue
            if cameras & set(a.cluster.camera_ids):
                continue
            if self._co_observed_now(c.cluster.keys, wid, a.cluster.keys, holder, cluster_of):
                continue
            if any(old != wid and self._is_distinct(old, wid) for old in owners):
                continue
            if self._geometric_shadow(c.cluster, pelvis_c, a.cluster, a.pelvis):
                found.append((math.dist(c.anchor, a.pelvis[:2]), wid))
        return min(found)[1] if found else None

    def _active_duplicate_merges(
        self,
        applied: dict[str, _Applied],
        holder: Mapping[TrackKey, str],
        cluster_of: Mapping[TrackKey, ViewCluster],
        flags: dict[str, set[IdentityFlag]],
        t: int,
    ) -> list[IdentityRevision]:
        """둘 다 이번 시각 갱신된 두 Track 의 시점 분할 근거를 세고, active_merge_confirm_ticks 번 연속이면 먼저 만든 ID 로 합친다.

        근거: 카메라 비겹침, 병합 금지 쌍(TTL)·분할 쌍·분할 후보가 아님, 이번 시각 같은 카메라 별도 검출 없음, 기하가 맞음
        (`_geometric_shadow`). 반대 근거가 나오면 처음부터 센다. 한쪽이 갱신되지 않은 시각은 세지도 버리지도 않는다(key_ttl_ms 넘게
        끊기면 tick 시작에서 버린다). 한 시각에 한 Track 은 한 번만 합친다. 근거 세기는 active_merge_enabled 와 무관하게 한다
        (Re-ID 경쟁자·crowd 판정 제외와 계측에 쓴다).
        """
        cfg = self.config
        live = sorted(
            (w for w, a in applied.items() if a.pelvis is not None and w in self._tracks),
            key=lambda w: self._tracks[w].order,
        )
        splitting = {sp.source for sp in self._splits.values()} | set(self._splits)
        ready: list[tuple[int, int, str, str]] = []
        for i, older in enumerate(live):
            for newer in live[i + 1 :]:
                pair = frozenset((older, newer))
                a, b = applied[older], applied[newer]
                assert a.pelvis is not None and b.pelvis is not None
                if (
                    set(a.cluster.camera_ids) & set(b.cluster.camera_ids)  # 같은 카메라 자리를 따로 가졌다: 두 사람이다
                    or self._is_distinct(older, newer)
                    or older in splitting
                    or newer in splitting
                    or self._co_observed_now(b.cluster.keys, older, a.cluster.keys, holder, cluster_of)
                    or self._co_observed_now(a.cluster.keys, newer, b.cluster.keys, holder, cluster_of)
                    or not self._geometric_shadow(a.cluster, a.pelvis, b.cluster, b.pelvis)
                ):
                    self._shadow_pairs.pop(pair, None)
                    continue
                prev = self._shadow_pairs.get(pair)
                count = prev.count + 1 if prev is not None and t - prev.last_t <= cfg.key_ttl_ms else 1
                self._shadow_pairs[pair] = _ShadowEvidence(count, t)
                self._twins[pair] = t
                self.diag["duplicate.shadow_pair_ticks"] += 1
                if cfg.active_merge_enabled and count >= cfg.active_merge_confirm_ticks:
                    ready.append((self._tracks[older].order, self._tracks[newer].order, older, newer))
        out: list[IdentityRevision] = []
        done: set[str] = set()
        for _, _, older, newer in sorted(ready):
            if older in done or newer in done or older not in self._tracks or newer not in self._tracks:
                continue
            ev = _Evidence(newer, frozenset(applied[newer].cluster.keys), cfg.active_merge_confirm_ticks, t)
            rec = self._merge(newer, older, ev, t, applied, flags)
            applied.pop(newer, None)  # 남는 ID(older)가 이번 시각 측정을 이미 받았다. 사라진 ID 의 측정은 쓰지 않는다
            done |= {older, newer}
            out.append(rec)
        return out

    def _delay_confirmation(self, applied: Mapping[str, _Applied]) -> None:
        """카메라가 겹치지 않는 확정 Track 이 confirm_delay_radius_m 안에 있는 동안 저신뢰로 갱신된 TENTATIVE 의 확정 횟수를 올린다."""
        cfg = self.config
        for wid, a in applied.items():
            tr = self._tracks.get(wid)
            if tr is None or tr.lifecycle is not TrackLifecycle.TENTATIVE or a.pelvis is None:
                continue
            low = a.cluster.identity_uncertain or a.gated.pelvis_validity() is not JointValidity.VALID
            if not low or tr.confirm_needed >= cfg.confirm_hits_near_confirmed:
                continue
            for other, b in applied.items():
                ot = self._tracks.get(other)
                if (
                    other != wid
                    and ot is not None
                    and ot.lifecycle is not TrackLifecycle.TENTATIVE
                    and b.pelvis is not None
                    and not set(a.cluster.camera_ids) & set(b.cluster.camera_ids)
                    and math.dist(a.pelvis[:2], b.pelvis[:2]) <= cfg.confirm_delay_radius_m
                ):
                    tr.confirm_needed = cfg.confirm_hits_near_confirmed
                    self.diag["lifecycle.confirm_delayed"] += 1
                    break

    def _all(self) -> list[_Track]:
        """살아 있는 Track 과 소실 후보."""
        return [*self._tracks.values(), *self._dormant.values()]

    def _find(self, world_id: str) -> _Track | None:
        return self._tracks.get(world_id) or self._dormant.get(world_id)

    # ------------------------------------------------------------------ 기립 키 보조 근거 (-657)

    def _update_stature(self, applied: Mapping[str, _Applied]) -> None:
        """이번 시각 갱신된 Track 의 기립 키 표본을 더한다(`standing_stature`, 관측값만)."""
        cfg = self.config
        for wid, a in applied.items():
            tr = self._tracks.get(wid)
            if tr is None:
                continue
            value = standing_stature(a.gated, cfg)
            if value is not None:
                tr.stature.append(value)
                del tr.stature[: -cfg.stature_window]

    def _stature_stats(self, tr: _Track) -> tuple[float, float] | None:
        """(중앙값, 표본 σ = MAD × 1.4826). 표본이 모자라면 None(모름)."""
        if len(tr.stature) < self.config.stature_min_samples:
            return None
        v = np.array(tr.stature, dtype=float)
        med = float(np.median(v))
        return med, float(np.median(np.abs(v - med))) * 1.4826

    def _stature_gap(self, a: _Track, b: _Track) -> tuple[float, float] | None:
        """(키 차이 |Δ|, 허용치). 한쪽이라도 모르면 None."""
        sa, sb = self._stature_stats(a), self._stature_stats(b)
        if sa is None or sb is None:
            return None
        tol = max(self.config.stature_min_tolerance_m, self.config.stature_sigma_k * math.hypot(sa[1], sb[1]))
        return abs(sa[0] - sb[0]), tol

    def _stature_rejects(self, tr: _Track, cand: _Track) -> bool:
        """키 차이가 허용치의 2배를 넘으면(확실히 다르면) 그 후보를 뺀다. 모르면 빼지 않는다."""
        gap = self._stature_gap(tr, cand)
        return gap is not None and gap[0] > 2.0 * gap[1]

    def _stature_breaks_tie(self, tr: _Track, best_id: str, rivals: Sequence[str]) -> bool:
        """최선 후보는 키 허용치 안이고, 외형으로 겨룬 다른 후보는 모두 키를 알며 허용치의 2배 이상 다른지."""
        best = self._find_with_dormant(best_id)
        gap = self._stature_gap(tr, best) if best is not None else None
        if gap is None or gap[0] > gap[1]:
            return False
        for rid in rivals:
            other = self._find_with_dormant(rid)
            g = self._stature_gap(tr, other) if other is not None else None
            if g is None or g[0] < 2.0 * g[1]:
                return False
        return True

    def _order_of(self, world_id: str) -> int:
        tr = self._find_with_dormant(world_id)
        return tr.order if tr is not None else 0

    def _find_with_dormant(self, world_id: str) -> _Track | None:
        return self._tracks.get(world_id) or self._dormant.get(world_id) or self._reid_dormant.get(world_id)

    def _forget_gone(self) -> None:
        """사라진 ID 가 든 병합 금지 쌍·병합 근거를 지운다."""
        alive = set(self._tracks) | set(self._dormant)
        alive_reid = alive | set(self._reid_dormant)
        self._distinct = {p: pt for p, pt in self._distinct.items() if p <= alive_reid}
        self._distinct_fixed = {p for p in self._distinct_fixed if p <= alive_reid}
        self._shadow_pairs = {p: ev for p, ev in self._shadow_pairs.items() if p <= alive}
        self._twins = {p: pt for p, pt in self._twins.items() if p <= alive_reid}
        self._evidence = {w: ev for w, ev in self._evidence.items() if w in alive and ev.stale in alive}
        self._recent_proximity = {p: pt for p, pt in self._recent_proximity.items() if p <= alive}
        self._swap_counts = {k: v for k, v in self._swap_counts.items() if k[0] in alive and k[1] in alive}

    def _take_key(self, key: TrackKey, tr: _Track, t: int, fresh: Collection[str]) -> None:
        """로컬 트랙을 이 Track 으로 옮긴다. 한 로컬 트랙은 Track 하나(소실 후보 포함)에만 있다.

        이번 시각 갱신되지 않은 Track(fresh 밖)에게서 넘겨받으면 그 출처를 `origin_of` 에 남긴다(그 사람일 수도 있다).
        """
        previous: _Track | None = None
        for other in self._all():
            if other is not tr and key in other.keys:
                other.keys.pop(key)
                other.origin_of.pop(key, None)
                previous = other
        if previous is not None:
            if previous.world_id in fresh:
                tr.origin_of.pop(key, None)
            else:
                tr.origin_of[key] = (previous.world_id, t)
        tr.keys[key] = t

    def _mark_distinct(
        self, frame: AssociationFrame, holder: Mapping[TrackKey, str], attached: Mapping[str, ViewCluster]
    ) -> None:
        """같은 시각 같은 카메라의 서로 다른 검출이 가리키는 Track 들을 병합 금지 쌍으로 둔다.

        검출 하나가 가리키는 Track 은 그 로컬 트랙의 주인(이번 시각 이전, 소실 후보 포함)과 그 클러스터를 받은 Track 이다.
        위치 측정 여부와 상관없다. 같은 검출이 가리키는 두 Track(주인과 받은 Track)은 짝짓지 않는다(그것이 병합 근거다).
        """
        by_cluster = {id(c): wid for wid, c in attached.items()}
        per_camera: dict[str, list[set[str]]] = {}
        for c in frame.clusters:
            got = by_cluster.get(id(c))
            for v in c.views:
                ids = {w for w in (holder.get(v.key), got) if w is not None}
                if ids:
                    per_camera.setdefault(v.camera_id, []).append(ids)
        for detections in per_camera.values():
            for i, a in enumerate(detections):
                for b in detections[i + 1 :]:
                    for p in a:
                        for q in b:
                            if p != q:
                                self._distinct[frozenset((p, q))] = frame.t

    def _ambiguous(self, tr: _Track) -> bool:
        """넘겨받은 로컬 트랙의 옛 주인이 그 뒤 다시 갱신되지 않고 남아 있어 이 Track 이 그 사람일 수도 있는지.

        옛 주인이 사라졌거나(소실 후보 끝), 그 뒤 따로 갱신됐거나, 병합 금지 쌍이면 그 출처는 지운다.
        """
        for key, (origin, taken) in list(tr.origin_of.items()):
            other = self._find(origin)
            if (
                key not in tr.keys
                or other is None
                or other is tr
                or other.t_last_valid > taken
                or self._is_distinct(origin, tr.world_id)
            ):
                del tr.origin_of[key]
        return bool(tr.origin_of)

    def _link_check(
        self,
        stale_id: str,
        c: _Cluster,
        held: Mapping[str, Collection[TrackKey]],
        cluster_of: Mapping[TrackKey, ViewCluster],
        crowd: Collection[str],
        t: int,
        observed: Sequence[tuple[int, np.ndarray]] = (),
    ) -> str | None:
        """갱신되지 않은 Track(stale_id)과 클러스터 c 를 같은 사람으로 이으면 안 되는 이유. 이어도 되면 None.

        - "elsewhere": stale_id 의 다른 로컬 트랙이 이번 시각 다른 클러스터에 있다(그 사람은 다른 곳에 있다).
        - "unreachable": 이번 시각 기준점, 또는 observed(잇거나 합칠 상대 Track 의 위치 측정) 중 stale_id 의 마지막 관측
          뒤의 것 하나라도, 마지막 위치에서 그 시간에 갈 수 있는 거리(gate_base_m + max_speed_mps × (그 관측 시각 −
          t_last_valid)) 밖이다. 병합은 상대의 첫 관측부터 재야 한다(지금 시각으로만 재면 기다리면 통과한다).
        - "crowd": 마지막 위치 reconnect_crowd_radius_m 안에 다른 소실·가림 Track(crowd)이 있다(로컬 트랙이 서로
          넘어갔을 수 있다).
        """
        cfg = self.config
        tr = self._find(stale_id)
        if tr is None or c.anchor is None:
            return "elsewhere"
        for k in held.get(stale_id, ()):
            other = cluster_of.get(k)
            if other is not None and other is not c.cluster:
                return "elsewhere"
        for when, xy in [*observed, (t, c.anchor)]:
            if when < tr.t_last_valid:
                continue
            reach = cfg.gate_base_m + cfg.max_speed_mps * (when - tr.t_last_valid) / 1000.0
            if float(np.linalg.norm(xy - tr.last_xy)) > reach:
                return "unreachable"
        shadows = self._shadows_of(stale_id)  # 같은 사람의 분신 Track 은 crowd 가 아니다(-657)
        for wid in crowd:
            other_tr = self._find(wid)
            if other_tr is None or wid in shadows:
                continue
            if float(np.linalg.norm(other_tr.last_xy - tr.last_xy)) <= cfg.reconnect_crowd_radius_m:
                return "crowd"
        return None

    def _merge_evidence(
        self,
        wid: str,
        c: _Cluster,
        holder: Mapping[TrackKey, str],
        held: Mapping[str, Collection[TrackKey]],
        cluster_of: Mapping[TrackKey, ViewCluster],
        stale_ids: set[str],
        t: int,
    ) -> tuple[_Evidence | None, bool]:
        """이번 시각 클러스터 c 를 받은 Track wid 의 병합 근거. (근거, 반대 근거가 있었는지).

        c 의 로컬 트랙 중 갱신되지 않은 Track X 의 것(지금 X 가 들고 있거나, wid 가 X 에게서 넘겨받은 것)이 서로 다른
        카메라 `reconnect_min_cameras` 대 이상이면 근거다. 한 대뿐이면 세지 않는다(로컬 ID 전환과 구별할 수 없음, 반대
        근거도 아님). 클러스터가 IDENTITY_UNCERTAIN 이거나, 여러 Track 의 로컬 트랙이 섞였거나, X 가 이번 시각 따로
        갱신됐거나, 병합 금지 쌍이거나, `_link_check` 를 못 채우면 반대 근거다(갈 수 없는 거리였으면 병합 금지 쌍으로 둔다). 직전 근거가 key_ttl_ms 안일 때만 이어서 센다
        (합치기 전에 TTL 을 먼저 본다).
        """
        cfg = self.config
        y = self._tracks[wid]
        self._ambiguous(y)  # 무효가 된 출처를 지운다
        claims: dict[str, set[TrackKey]] = {}
        for k in c.cluster.keys:
            h = holder.get(k)
            if h is not None and h != wid:
                claims.setdefault(h, set()).add(k)
            elif h == wid and k in y.origin_of:
                claims.setdefault(y.origin_of[k][0], set()).add(k)
        claims = {x: ks for x, ks in claims.items() if self._find(x) is not None}
        if not claims:
            return None, False
        if c.uncertain or len(claims) != 1:
            return None, True
        ((stale_id, keys),) = claims.items()
        if stale_id not in stale_ids or self._is_distinct(stale_id, wid):
            return None, True
        reason = self._link_check(stale_id, c, held, cluster_of, stale_ids - {stale_id}, t, y.path)
        if reason == "unreachable":
            self._distinct[frozenset((stale_id, wid))] = t  # 갈 수 없는 곳에서 관측된 적이 있다: 같은 사람이 아니다
        if reason is not None:
            return None, True
        if _cameras(keys) < cfg.reconnect_min_cameras:
            return None, False
        prev = self._evidence.get(wid)
        if prev is not None and prev.stale == stale_id and t - prev.last_t <= cfg.key_ttl_ms:
            return _Evidence(stale_id, prev.keys | frozenset(keys), prev.count + 1, t), False
        return _Evidence(stale_id, frozenset(keys), 1, t), False

    def _reconnect_target(
        self,
        c: _Cluster,
        holder: Mapping[TrackKey, str],
        held: Mapping[str, Collection[TrackKey]],
        cluster_of: Mapping[TrackKey, ViewCluster],
        applied: Mapping[str, _Applied],
        t: int,
    ) -> tuple[str | None, bool, str | None]:
        """새 Track 이 될 클러스터를 같은 ID 로 이을 소실·가림 Track.

        (대상, 애매해서 잇지 않았는지, 갈 수 없는 거리라 잇지 않은 Track). 마지막 값은 새로 만든 Track 과 병합 금지 쌍이 된다.
        """
        if c.uncertain:
            self.diag["reconnect_skip.cluster_uncertain"] += 1
            return None, False, None
        holders = {holder[k] for k in c.cluster.keys if k in holder}
        if not holders or holders & set(applied):
            # 로컬 트랙 근거가 없거나, 이번 시각 갱신된 Track 의 로컬 트랙이 섞였다(갈라진 view·ID 전환): 지금 동작대로
            self.diag["reconnect_skip.no_holder" if not holders else "reconnect_skip.holder_updated_now"] += 1
            return None, False, None
        if len(holders) > 1:
            self.diag["reconnect_skip.multiple_holders"] += 1
            return None, True, None
        (stale_id,) = holders
        crowd = [w for w in (*self._tracks, *self._dormant) if w != stale_id and w not in applied]
        reason = self._link_check(stale_id, c, held, cluster_of, crowd, t)
        if reason is not None:
            self.diag[f"reconnect_skip.{reason}"] += 1
            return None, True, (stale_id if reason == "unreachable" else None)
        if _cameras(k for k in c.cluster.keys if holder.get(k) == stale_id) < self.config.reconnect_min_cameras:
            self.diag["reconnect_skip.single_camera_evidence"] += 1
            return None, True, None  # 카메라 한 대의 로컬 트랙: 로컬 ID 전환일 수 있다
        return stale_id, False, None

    def _reconnect(
        self,
        world_id: str,
        cluster: ViewCluster,
        frame: AssociationFrame,
        rig: CameraRig,
        excluded: frozenset[str],
        t: int,
    ) -> _Applied | None:
        """클러스터를 Track 에 잇는다. 소실·장기 보류 후보는 위치 측정이 있을 때만 살리고 게이트 이력을 새로 시작한다."""
        if world_id in self._tracks:
            return self._apply(self._tracks[world_id], cluster, frame, rig, excluded, t)
        tr = self._dormant.get(world_id) or self._reid_dormant.get(world_id)
        if tr is None:
            return None
        gate = JointGate(self.config.joint_gate)
        gated = self._gate(gate, cluster, frame, rig, excluded)
        if self._measurement(gated) is None:
            return None  # 위치 없이 살리면 다음 생명주기에서 다시 LOST 가 된다
        self._dormant.pop(world_id, None)
        self._reid_dormant.pop(world_id, None)
        self.galleries.mark_active(world_id, t)
        self._tracks[world_id] = tr
        tr.gate = gate
        return self._apply(tr, cluster, frame, rig, excluded, t, gated)

    def _merge(
        self,
        stale_id: str,
        live_id: str,
        ev: _Evidence,
        t: int,
        applied: dict[str, _Applied],
        flags: dict[str, set[IdentityFlag]],
    ) -> IdentityRevision:
        """두 ID 를 먼저 만든 ID 로 합치고 교정 기록을 남긴다. 위치·필터는 이번 시각 측정을 받은 live 의 것을 잇는다."""
        stale = self._find_with_dormant(stale_id)
        live = self._tracks[live_id]
        assert stale is not None
        was_lost = stale_id in self._dormant or stale_id in self._reid_dormant
        survivor, retired = (stale, live) if stale.order < live.order else (live, stale)
        revision = max(stale.identity_revision, live.identity_revision) + 1
        confirmed = not (stale.lifecycle is TrackLifecycle.TENTATIVE and live.lifecycle is TrackLifecycle.TENTATIVE)
        if survivor is stale:
            survivor.kf = live.kf
            survivor.gate = live.gate
            survivor.t_last_valid = live.t_last_valid
            survivor.reacquired_until = live.reacquired_until
            if live_id in applied:
                applied[stale_id] = applied.pop(live_id)
        survivor.lifecycle = TrackLifecycle.CONFIRMED if confirmed else TrackLifecycle.TENTATIVE
        survivor.hits += retired.hits
        survivor.path = sorted([*survivor.path, *retired.path], key=lambda p: p[0])
        survivor.identity_revision = revision
        survivor.aliases.extend([retired.world_id, *retired.aliases])
        survivor.gate.reset()  # 이력이 다른 사람 것일 수 있다 (joint_gate)
        for k, kt in retired.keys.items():
            survivor.keys[k] = max(kt, survivor.keys.get(k, kt))
        for f, ft in retired.flags.items():
            survivor.flags[f] = max(ft, survivor.flags.get(f, ft))
        for f, ft in retired.long_hold_flags.items():
            survivor.long_hold_flags[f] = max(ft, survivor.long_hold_flags.get(f, ft))
        candidates = [*survivor.restored_candidates, *retired.restored_candidates]
        survivor.restored_candidates = tuple(dict.fromkeys(candidates))
        survivor.restored_excluded = (survivor.restored_excluded | retired.restored_excluded) - set(candidates)
        origins = {**survivor.origin_of, **retired.origin_of}
        survivor.origin_of = {k: v for k, v in origins.items() if v[0] not in (stale_id, live_id)}
        survivor.confirm_left = self.config.merge_confirm_frames
        survivor.split_left = max(survivor.split_left, retired.split_left)
        merged_flags = flags.setdefault(survivor.world_id, set())
        merged_flags |= flags.pop(retired.world_id, set())
        self._tracks.pop(retired.world_id, None)
        self._dormant.pop(retired.world_id, None)
        self._dormant.pop(survivor.world_id, None)
        self._reid_dormant.pop(retired.world_id, None)
        self._reid_dormant.pop(survivor.world_id, None)
        self._tracks[survivor.world_id] = survivor
        old, new = retired.world_id, survivor.world_id
        self.galleries.merge(new, old, t)
        self._duplicates = {p for p in (_rename(p, old, new) for p in self._duplicates) if len(p) == 2}
        renamed: dict[frozenset[str], int] = {}
        for p, pt in self._distinct.items():
            q = _rename(p, old, new)
            if len(q) == 2:
                renamed[q] = max(pt, renamed.get(q, pt))
        self._distinct = renamed
        self._distinct_fixed = {q for q in (_rename(p, old, new) for p in self._distinct_fixed) if len(q) == 2}
        self._shadow_pairs = {p: ev for p, ev in self._shadow_pairs.items() if not p & {old, new}}
        self._twins = {p: pt for p, pt in self._twins.items() if old not in p}
        self._evidence = {w: e for w, e in self._evidence.items() if {w, e.stale}.isdisjoint({old, new})}
        record = IdentityRevision(
            t=t,
            kind=RevisionKind.MERGE,
            world_id=new,
            previous_world_id=old,
            identity_revision=revision,
            reason=str(RevisionReason.LATE_REASSOCIATION if was_lost else RevisionReason.DUPLICATE_MERGE),
            evidence=tuple(sorted(ev.keys)),
        )
        self._revisions.append(record)
        return record

    # ------------------------------------------------------------------ 분할 (S15P31A101-201)

    def _split_candidate(
        self, child: str, c: _Cluster, holder: Mapping[TrackKey, str], applied: Mapping[str, _Applied], t: int
    ) -> None:
        """새로 만든 Track child 가 이번 시각 갱신된 다른 Track 의 로컬 트랙을 2대 이상 카메라에서 갈라 가졌으면 분할 후보로 둔다.

        카메라마다 child 의 클러스터와 그 Track 의 클러스터에 둘 다 그 Track 의 로컬 트랙(이번 시각 이전 주인)이 있어야 한다
        (같은 시각 같은 카메라의 두 검출). 두 클러스터 중 하나라도 IDENTITY_UNCERTAIN 이거나 그런 Track 이 여럿이면 두지 않는다.
        """
        cfg = self.config
        if c.uncertain:
            return
        sources = []
        for source in sorted({holder[k] for k in c.cluster.keys if k in holder}):
            a = applied.get(source)
            if source not in self._tracks or a is None or a.pelvis is None or a.cluster.identity_uncertain:
                continue
            keys = frozenset(k for k in c.cluster.keys if holder.get(k) == source)
            if _split_cameras(a.cluster, c.cluster, keys, holder, source) >= cfg.reconnect_min_cameras:
                sources.append((source, keys))
        if len(sources) != 1:
            return  # 없거나, 여러 Track 에서 갈라졌다(어느 쪽인지 모른다)
        ((source, keys),) = sources
        far = _horizontal(applied[source].pelvis, applied[child].pelvis) > cfg.duplicate_radius_m
        self._splits[child] = _Split(source, child, keys, 1 if far else 0, t, t)

    def _split_step(
        self,
        t: int,
        applied: Mapping[str, _Applied],
        holder: Mapping[TrackKey, str],
        flags: dict[str, set[IdentityFlag]],
    ) -> list[IdentityRevision]:
        """분할 후보의 근거를 세고, 쌓였으면 분할한다. 후보인 동안 두 Track 에 IDENTITY_SPLIT 을 붙인다."""
        cfg = self.config
        out: list[IdentityRevision] = []
        for child, sp in sorted(self._splits.items()):
            if sp.source not in self._tracks or child not in self._tracks:
                del self._splits[child]  # 사라졌거나 소실 후보가 됐거나 병합됐다
                continue
            if t - sp.last_t > cfg.key_ttl_ms:
                # 근거가 끊겼다: 분할하지 않는다(모르면 그대로). 이번 시각의 근거로 되살리지 않는다(전체 입력이 멈춰
                # update 가 불리지 않다가 돌아온 경우에도 시각으로 판정한다, 리뷰 R2)
                del self._splits[child]
                continue
            a, b = applied.get(sp.source), applied.get(child)
            if sp.since != t and a is not None and b is not None and a.pelvis is not None and b.pelvis is not None:
                # 둘 다 위치 측정을 받은 시각만 본다(가려진 시각은 세지도 버리지도 않는다)
                certain = not (a.cluster.identity_uncertain or b.cluster.identity_uncertain)
                apart = certain and _horizontal(a.pelvis, b.pelvis) > cfg.duplicate_radius_m
                if self.feature_lookup is not None and cfg.reid_enabled:
                    sim = self._cluster_track_sim(sp.source, b.cluster, a.cluster, t)
                    if sim is not None and sim >= cfg.reid_split_same_min_sim:
                        apart = False
                if _split_cameras(a.cluster, b.cluster, sp.keys, holder, sp.source, child) >= cfg.reconnect_min_cameras:
                    sp.count = sp.count + 1 if apart else 0
                    sp.last_t = t
                elif not apart:
                    sp.count = 0  # 가까워졌거나(같은 사람일 수 있다) 연결이 불확실하다: 처음부터 센다
            for wid in (sp.source, child):
                flags.setdefault(wid, set()).add(IdentityFlag.IDENTITY_SPLIT)
            if sp.count >= cfg.merge_confirm_frames:
                del self._splits[child]
                out.append(self._split(sp, t))
        return out

    def _split(self, sp: _Split, t: int) -> IdentityRevision:
        """child 를 source 에서 가른다. 두 ID 의 identity_revision 을 함께 올리고 교정 기록을 남긴다(별칭은 그대로)."""
        cfg = self.config
        source, child = self._tracks[sp.source], self._tracks[sp.child]
        revision = max(source.identity_revision, child.identity_revision) + 1
        for tr in (source, child):
            tr.identity_revision = revision
            tr.split_left = cfg.merge_confirm_frames
        child.split_from = sp.source  # 새로 만든 Track 이라 아직 없다(분할 후보는 새 Track 에만 생긴다)
        source.gate.reset()  # 분할 전 관절 이력은 다른 사람 것일 수 있다 (joint_gate)
        pair = frozenset((sp.source, sp.child))
        self._distinct_fixed.add(pair)  # 같은 카메라의 두 검출로 보였다: 다시 합치지 않는다(진동 방지, 시간 제한 없음)
        self._duplicates.discard(pair)
        record = IdentityRevision(
            t=t,
            kind=RevisionKind.SPLIT,
            world_id=sp.child,
            previous_world_id=sp.source,
            identity_revision=revision,
            reason=str(RevisionReason.SPLIT_OBSERVED),
            evidence=tuple(sorted(sp.keys)),
            since=sp.since,
        )
        self._revisions.append(record)
        return record

    # ------------------------------------------------------------------ 재시작 뒤 복원 Track (S15P31A101-54)

    def _restore_step(self, t: int) -> None:
        """첫 프레임이면 복원 Track 을 받을지 정하고, 마지막 관측부터 restore_max_age_ms 가 지난 복원 Track 을 뺀다."""
        limit = self.config.restore_max_age_ms
        if self._restore_input is not None:
            tracks, tracks_t = self._restore_input
            self._restore_input = None
            reason: str | None = None
            if tracks and tracks_t is None:
                reason = "체크포인트 Track 시각(tracks_t)을 모른다"
            elif tracks_t is not None and t < tracks_t:
                reason = f"재시작 뒤 첫 프레임({t})이 체크포인트 Track 시각({tracks_t})보다 앞이다(시각 역행)"
            elif tracks_t is not None and t - tracks_t > limit:
                reason = f"체크포인트가 {t - tracks_t} ms 전 것이다(상한 {limit} ms)"
            if reason is None:
                self._restored = {tr.world_id: tr for tr in tracks if t - tr.t_last_valid <= limit}
            self._restore_status = RestoreStatus(
                RestoreState.DISCARDED if reason is not None else RestoreState.ACCEPTED,
                reason=reason,
                tracks_t=tracks_t,
                decided_t=t,
                restored=tuple(sorted(self._restored)),
                link_until=t + limit,
            )
        for wid in [w for w, tr in self._restored.items() if t - tr.t_last_valid > limit]:
            del self._restored[wid]

    def _relate_restored(self, tr: _Track, xy: np.ndarray, t: int) -> None:
        """새 Track 의 첫 위치 측정으로 복원 Track 후보를 정한다. 잇지 않는다(위치만으로는 같은 사람이라 할 수 없다).

        도달 거리는 늦은 재연결(`_link_check`)과 같은 식 gate_base_m + max_speed_mps × (t − 복원 Track 의 t_last_valid)
        이다. 첫 관측으로만 재므로 기다린다고 후보가 되지 않는다.
        """
        cfg = self.config
        near: list[tuple[float, str]] = []
        far: set[str] = set()
        for wid, r in self._restored.items():
            d = float(np.hypot(xy[0] - r.position_xyz[0], xy[1] - r.position_xyz[1]))
            reach = cfg.gate_base_m + cfg.max_speed_mps * (t - r.t_last_valid) / 1000.0
            if d <= reach:
                near.append((d, wid))
            else:
                far.add(wid)
        tr.restored_candidates = tuple(wid for _, wid in sorted(near))
        tr.restored_excluded = frozenset(far)

    # ------------------------------------------------------------------ Re-ID 외형 보조 판정 (S15P31A101-463)

    def _cluster_features(self, cluster: ViewCluster, t: int) -> list[LocalTrackFeature]:
        if self.feature_lookup is None or not self.config.reid_enabled:
            return []
        out: list[LocalTrackFeature] = []
        for k in cluster.keys:
            feat = self._tick_features.get(k)
            if feat is not None:
                out.append(feat)
        return out

    def _cluster_gallery_sim(self, world_id: str, cluster: ViewCluster, t: int) -> float | None:
        gallery = self.galleries.get(world_id)
        if gallery is None or len(gallery) == 0:
            return None
        # 결과는 갤러리 항목과 클러스터 키의 tick 특징(update 첫머리에 한 번 읽음)으로만 정해진다. 같은 tick 에 같은
        # 갤러리 객체·같은 version 이면 다시 쓴다(배정 비용·스왑 검사·갤러리 갱신이 같은 짝을 여러 번 묻는다, S15P31A101-663)
        memo_key = (world_id, cluster.keys)
        hit = self._gallery_sim_memo.get(memo_key)
        if hit is not None and hit[0] is gallery and hit[1] == gallery.version:
            return hit[2]
        feats = self._cluster_features(cluster, t)
        if not feats:
            result = None
        else:
            best = -1.0
            # -684 ID-03: 질의 특징의 부위 가시성을 넘긴다. 공통으로 보이는 부위가 없으면 갤러리가 비교하지 않는다(None)
            use_visibility = self.config.reid_query_visibility
            for f in feats:
                sim = gallery.similarity(
                    f.embedding,
                    camera_id=f.camera_id,
                    query_visibility=getattr(f, "visibility", None) if use_visibility else None,
                    face_embedding=getattr(f, "face_embedding", None),
                )
                if sim is not None and sim > best:
                    best = sim
            result = best if best >= 0.0 else None
        self._gallery_sim_memo[memo_key] = (gallery, gallery.version, result)
        return result

    def _cluster_track_sim(
        self,
        world_id: str,
        cluster: ViewCluster,
        other_cluster: ViewCluster | None,
        t: int,
    ) -> float | None:
        """클러스터와 대상 Track(갤러리 + 같은 시각 다른 클러스터 특징) 간 최대 코사인 유사도."""
        feats = self._cluster_features(cluster, t)
        if not feats:
            return None
        best = self._cluster_gallery_sim(world_id, cluster, t)
        if other_cluster is not None:
            other_feats = self._cluster_features(other_cluster, t)
            for fa in feats:
                for fb in other_feats:
                    sim = float(np.dot(fa.embedding, fb.embedding))
                    if best is None or sim > best:
                        best = sim
        return best

    def _reid_reconnect_target(
        self,
        c: _Cluster,
        holder: Mapping[TrackKey, str],
        held: Mapping[str, Collection[TrackKey]],
        cluster_of: Mapping[TrackKey, ViewCluster],
        applied: Mapping[str, _Applied],
        t: int,
    ) -> tuple[str | None, bool]:
        """기하 재연결이 로컬 트랙 ID 소실로 불발됐을 때, 기하 도달 가능 + 외형 유사도로 DORMANT/LOST/미배정 Track 에 잇는다."""
        cfg = self.config
        if not cfg.reid_dormant_enabled or c.anchor is None:
            return None, False
        feats = self._cluster_features(c.cluster, t)
        if not feats:
            return None, False

        # 현재 활성 배정된 Track 들과의 유사도 (이미 화면에 있는 사람의 분리 나머지를 DORMANT 에 잘못 붙이는 것을 방지)
        active_sims: dict[str, float] = {}
        for wid, a in applied.items():
            sim = self._cluster_track_sim(wid, c.cluster, a.cluster, t)
            if sim is not None:
                active_sims[wid] = sim

        holders = {holder[k] for k in c.cluster.keys if k in holder}
        candidates: list[tuple[float, str]] = []
        for cand_id, cand in [
            *self._reid_dormant.items(),
            *self._dormant.items(),
            *((w, tr) for w, tr in self._tracks.items() if w not in applied),
        ]:
            if cand_id in applied:
                continue
            if cand.lifecycle is TrackLifecycle.TENTATIVE and cand.hits < cfg.confirm_hits:
                continue
            # 그 후보의 다른 로컬 트랙이 이번 시각 다른 클러스터에 있으면 다른 곳에 있는 사람이다
            if any(
                (other := cluster_of.get(k)) is not None and other is not c.cluster
                for k in held.get(cand_id, ())
            ):
                continue
            if holders and all(self._is_distinct(h, cand_id) for h in holders):
                continue
            dt_ms = t - cand.t_last_valid
            if dt_ms <= 0 or dt_ms > cfg.reid_dormant_ttl_ms:
                continue
            reach = cfg.gate_base_m + cfg.max_speed_mps * (dt_ms / 1000.0)
            if float(np.linalg.norm(c.anchor - cand.last_xy)) > reach:
                continue
            sim = self._cluster_gallery_sim(cand_id, c.cluster, t)
            if sim is not None:
                candidates.append((sim, cand_id))

        if not candidates:
            return None, False
        # 유사도가 같으면 먼저 만든 ID (분신끼리 같은 외형이면 원래 ID 로 돌아온다, -657)
        candidates.sort(key=lambda c: (c[0], -self._order_of(c[1])), reverse=True)
        best_sim, best_id = candidates[0]
        # 같은 사람의 분신 Track(시점 분할 근거가 있었던 쌍)은 경쟁자가 아니다(-657, 14차 reentry_ambiguous 원인)
        twins = self._shadows_of(best_id)
        second_sim = max((s for s, w in candidates[1:] if w not in twins), default=-1.0)
        active_rival = max(
            (s for w, s in active_sims.items() if w not in twins), default=-1.0
        )
        rival_sim = max(second_sim, active_rival)
        margin = best_sim - rival_sim if rival_sim >= 0.0 else best_sim

        # 1대 로컬 트랙이 유지된 경우(single_camera_evidence)는 재입장 임계값을 소폭 완화
        has_single_cam_holder = holders == {best_id}
        min_sim = (cfg.reid_reentry_min_sim - 0.04) if has_single_cam_holder else cfg.reid_reentry_min_sim
        if best_sim >= min_sim and margin >= cfg.reid_reentry_margin:
            return best_id, False
        if best_sim >= cfg.reid_split_same_min_sim and margin < cfg.reid_reentry_margin:
            self.diag["reid.reentry_ambiguous"] += 1
            return None, True
        return None, False

    def _reid_late_reentry_merge(
        self,
        applied: dict[str, _Applied],
        held: Mapping[str, Collection[TrackKey]],
        cluster_of: Mapping[TrackKey, ViewCluster],
        flags: dict[str, set[IdentityFlag]],
        t: int,
    ) -> list[tuple[IdentityRevision, bool]]:
        """새로 생성된 Track 의 첫 프레임 이후 비동기 Re-ID 특징이 도착했을 때 DORMANT/LOST 후보와 병합한다."""
        cfg = self.config
        out: list[tuple[IdentityRevision, bool]] = []
        if not cfg.reid_dormant_enabled:
            return out
        for wid, a in list(applied.items()):
            tr = self._tracks.get(wid)
            if tr is None or tr.reid_checked_reentry:
                continue
            if t - tr.created_t > max(cfg.key_ttl_ms * 3, 2500):
                tr.reid_checked_reentry = True
                continue
            feats = self._cluster_features(a.cluster, t)
            if not feats:
                continue
            # -657: 키 표본이 모자라면 창 안에서 다시 본다(외형이 애매할 때 키 동률 깨기). 이으면 아래에서 끝낸다
            tr.reid_checked_reentry = not (cfg.stature_enabled and len(tr.stature) < cfg.stature_min_samples)
            if not tr.path:
                continue
            first_t, first_xy = tr.path[0]

            # 다른 활성 Track 과의 유사도 (자기 분신은 경쟁자가 아니다, -657)
            active_sims: dict[str, float] = {}
            shadows = self._shadows_of(wid)
            for other_wid, other_a in applied.items():
                if other_wid == wid or other_wid in shadows:
                    continue
                sim = self._cluster_track_sim(other_wid, a.cluster, other_a.cluster, t)
                if sim is not None:
                    active_sims[other_wid] = sim

            candidates: list[tuple[float, str]] = []
            for cand_id, cand in [
                *self._reid_dormant.items(),
                *self._dormant.items(),
                *((w, st) for w, st in self._tracks.items() if w not in applied and st.order < tr.order),
            ]:
                if cand.order >= tr.order:
                    continue
                if cand.lifecycle is TrackLifecycle.TENTATIVE and cand.hits < cfg.confirm_hits:
                    continue
                if self._is_distinct(cand_id, wid):
                    continue
                if any(
                    (other := cluster_of.get(k)) is not None and other is not a.cluster
                    for k in held.get(cand_id, ())
                ):
                    continue
                dt_ms = first_t - cand.t_last_valid
                if dt_ms < 0 or dt_ms > cfg.reid_dormant_ttl_ms:
                    continue
                reach = cfg.gate_base_m + cfg.max_speed_mps * (dt_ms / 1000.0)
                if float(np.linalg.norm(first_xy - cand.last_xy)) > reach:
                    continue
                sim = self._cluster_gallery_sim(cand_id, a.cluster, t)
                if sim is not None:
                    if cfg.stature_enabled and self._stature_rejects(tr, cand):
                        self.diag["reid.stature_rejected"] += 1
                        continue  # 기립 키가 확실히 다르다: 그 사람이 아니다(-657)
                    candidates.append((sim, cand_id))

            if not candidates:
                continue
            candidates.sort(key=lambda c: (c[0], -self._order_of(c[1])), reverse=True)
            best_sim, best_id = candidates[0]
            twins = self._shadows_of(best_id)  # 분신은 경쟁자가 아니다(-657)
            second_sim = max((s for s, w in candidates[1:] if w not in twins), default=-1.0)
            active_max_sim = max((s for w, s in active_sims.items() if w not in twins), default=-1.0)
            rival_sim = max(second_sim, active_max_sim)
            margin = best_sim - rival_sim if rival_sim >= 0.0 else best_sim
            if cfg.stature_enabled and best_sim >= cfg.reid_reentry_min_sim and margin < cfg.reid_reentry_margin:
                # 외형이 애매하다: 키 허용치 안이 최선 후보 하나뿐이고 겨룬 후보가 모두 허용치의 2배 넘게 다르면 잇는다(-657)
                rivals = [w for s, w in candidates[1:] if w not in twins and s >= cfg.reid_split_same_min_sim]
                rivals += [w for w, s in active_sims.items() if w not in twins and s >= cfg.reid_split_same_min_sim]
                if self._stature_breaks_tie(tr, best_id, rivals):
                    self.diag["reid.stature_tiebreak"] += 1
                    margin = cfg.reid_reentry_margin
            if best_sim >= cfg.reid_reentry_min_sim and margin >= cfg.reid_reentry_margin:
                tr.reid_checked_reentry = True
                was_lost = best_id in self._dormant or best_id in self._reid_dormant
                ev = _Evidence(best_id, frozenset(a.cluster.keys), cfg.merge_confirm_frames, t)
                rec = self._merge(best_id, wid, ev, t, applied, flags)
                out.append((rec, was_lost))
        return out

    def _check_and_revert_swaps(
        self,
        assigned: dict[str, _Cluster],
        predicted: Mapping[str, np.ndarray],
        flags: dict[str, set[IdentityFlag]],
        t: int,
    ) -> None:
        """교차·근접 직후 두 활성 Track 의 갤러리 대비 교차 유사도가 자기 유사도보다 일관되게 높으면 배정을 되돌린다."""
        cfg = self.config
        active_ids = sorted(assigned)
        for i, wid_a in enumerate(active_ids):
            ca = assigned[wid_a]
            if ca.anchor is None:
                continue
            for wid_b in active_ids[i + 1 :]:
                cb = assigned[wid_b]
                if cb.anchor is None:
                    continue
                dist = float(np.linalg.norm(ca.anchor - cb.anchor))
                pred_dist = float(np.linalg.norm(predicted[wid_a] - predicted[wid_b]))
                if min(dist, pred_dist) <= cfg.reid_swap_proximity_m:
                    self._recent_proximity[frozenset((wid_a, wid_b))] = t

        self._recent_proximity = {
            p: pt for p, pt in self._recent_proximity.items() if t - pt <= cfg.reid_swap_window_ms
        }
        if self.feature_lookup is None or not cfg.reid_enabled or not cfg.reid_swap_correction_enabled:
            return

        for pair, prox_t in list(self._recent_proximity.items()):
            if len(pair) != 2:
                continue
            wid_a, wid_b = sorted(pair)
            if wid_a not in assigned or wid_b not in assigned:
                continue
            ca, cb = assigned[wid_a], assigned[wid_b]
            if ca.anchor is None or cb.anchor is None:
                continue
            sim_aa = self._cluster_gallery_sim(wid_a, ca.cluster, t)
            sim_ab = self._cluster_gallery_sim(wid_a, cb.cluster, t)
            sim_bb = self._cluster_gallery_sim(wid_b, cb.cluster, t)
            sim_ba = self._cluster_gallery_sim(wid_b, ca.cluster, t)
            if sim_aa is None or sim_ab is None or sim_bb is None or sim_ba is None:
                continue

            adv_a = sim_ab - sim_aa
            adv_b = sim_ba - sim_bb
            key = (wid_a, wid_b)
            ta, tb = self._tracks[wid_a], self._tracks[wid_b]
            if (
                sim_ab >= cfg.reid_swap_min_sim
                and sim_ba >= cfg.reid_swap_min_sim
                and adv_a >= cfg.reid_swap_margin
                and adv_b >= cfg.reid_swap_margin
            ):
                # 기하 도달 가능성 확인 (교차 시각 또는 직전 관측 시각 기준)
                dt_a = max(t - ta.t_last_valid, t - prox_t, 100)
                dt_b = max(t - tb.t_last_valid, t - prox_t, 100)
                reach_a = max(cfg.reid_swap_proximity_m * 1.5, cfg.gate_base_m + cfg.max_speed_mps * (dt_a / 1000.0))
                reach_b = max(cfg.reid_swap_proximity_m * 1.5, cfg.gate_base_m + cfg.max_speed_mps * (dt_b / 1000.0))
                if (
                    float(np.linalg.norm(cb.anchor - ta.last_xy)) > reach_a
                    or float(np.linalg.norm(ca.anchor - tb.last_xy)) > reach_b
                ):
                    continue

                count = self._swap_counts.get(key, 0) + 1
                self._swap_counts[key] = count
                ta.swap_suspected_until = max(ta.swap_suspected_until, t + cfg.uncertain_hold_ms)
                tb.swap_suspected_until = max(tb.swap_suspected_until, t + cfg.uncertain_hold_ms)
                if count >= cfg.reid_swap_confirm_frames:
                    # 만약 이전 프레임에 이미 스왑된 궤적을 따라가고 있었다면 칼만 상태도 함께 교환한다
                    cur_err = math.dist(ca.anchor, predicted[wid_a]) + math.dist(cb.anchor, predicted[wid_b])
                    swap_err = math.dist(cb.anchor, predicted[wid_a]) + math.dist(ca.anchor, predicted[wid_b])
                    if swap_err > cur_err:
                        ta.kf, tb.kf = tb.kf, ta.kf
                        ta.t_last_valid, tb.t_last_valid = tb.t_last_valid, ta.t_last_valid
                    ta.gate.reset()
                    tb.gate.reset()
                    ta.identity_revision += 1
                    tb.identity_revision += 1
                    assigned[wid_a], assigned[wid_b] = cb, ca
                    self._swap_counts.pop(key, None)
                    self._recent_proximity.pop(pair, None)
                    self.diag["reid.swap_reverted"] += 1
                else:
                    flags[wid_a].add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
                    flags[wid_b].add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
            else:
                if adv_a > 0.0 and adv_b > 0.0:
                    flags[wid_a].add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
                    flags[wid_b].add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
                    ta.swap_suspected_until = max(ta.swap_suspected_until, t + cfg.uncertain_hold_ms)
                    tb.swap_suspected_until = max(tb.swap_suspected_until, t + cfg.uncertain_hold_ms)
                self._swap_counts.pop(key, None)

    def _update_galleries(self, applied: Mapping[str, _Applied], t: int) -> None:
        """활성 Track 의 갤러리를 갱신하고 Track 별 유사도·경쟁 margin 을 기록한다.

        두 활성 Track 간 거리가 1.0 m 이하이거나 신원이 불확실(IDENTITY_UNCERTAIN / 분할 대기 / swap 의심)한 동안에는
        갤러리 오염을 막기 위해 갤러리 갱신을 동결한다.
        """
        if self.feature_lookup is None or not self.config.reid_enabled:
            return
        cfg = self.config
        active_positions = {
            wid: np.array([float(tr.kf.x[0]), float(tr.kf.x[1])], dtype=float)
            for wid, tr in self._tracks.items()
        }
        close_freeze: set[str] = set()
        wids = sorted(active_positions)
        for i, wa in enumerate(wids):
            for wb in wids[i + 1 :]:
                if frozenset((wa, wb)) in self._shadow_pairs:
                    continue  # 같은 사람의 분신끼리는 서로 오염시키지 않는다(-657)
                if float(np.linalg.norm(active_positions[wa] - active_positions[wb])) <= cfg.reid_gallery_freeze_distance_m:
                    close_freeze.add(wa)
                    close_freeze.add(wb)

        split_involved = {sp.source for sp in self._splits.values()} | set(self._splits.keys())

        for wid, tr in self._tracks.items():
            a = applied.get(wid)
            if a is None:
                continue
            feats = self._cluster_features(a.cluster, t)
            if not feats:
                continue

            # 현재 갤러리와의 유사도 및 다른 활성 Track 대비 margin 기록 (같은 사람의 분신 Track 은 경쟁자가 아니다, -657)
            shadows = self._shadows_of(wid)
            own_sim = self._cluster_gallery_sim(wid, a.cluster, t)
            rival_sim = -1.0
            for other_wid in self._tracks:
                if other_wid == wid or other_wid in shadows:
                    continue
                s = self._cluster_gallery_sim(other_wid, a.cluster, t)
                if s is not None and s > rival_sim:
                    rival_sim = s
            tr.last_reid_similarity = own_sim
            if own_sim is not None:
                tr.last_reid_margin = own_sim - rival_sim if rival_sim >= 0.0 else own_sim

            held_flags = {f for f, ft in tr.flags.items() if t - ft <= cfg.uncertain_hold_ms}
            freeze = (
                wid in close_freeze
                or wid in split_involved
                or bool(held_flags)
                or a.cluster.identity_uncertain
                or t <= tr.swap_suspected_until
            )
            if freeze:
                self.diag["reid.gallery_frozen"] += 1
                continue

            orient_bin = self._estimate_orientation_bin(a.gated)
            gallery = self.galleries.get_or_create(wid, t)
            for f in feats:
                vis_ratio = (
                    f.visibility.visibility_ratio()
                    if hasattr(f.visibility, "visibility_ratio")
                    else float(f.visibility)
                )
                qual = float(getattr(f, "quality_score", getattr(f, "quality", 1.0)))
                if vis_ratio < cfg.reid_gallery_min_visibility or qual < cfg.reid_gallery_min_quality:
                    continue
                gallery.update(
                    embedding=f.embedding,
                    camera_id=f.camera_id,
                    t_ms=t,
                    quality=qual,
                    visibility=f.visibility,
                    orientation_bin=orient_bin if orient_bin != 0 else f.orientation_bin,
                    face_embedding=getattr(f, "face_embedding", None),
                )
            if tr.last_reid_similarity is None:
                own_sim = self._cluster_gallery_sim(wid, a.cluster, t)
                rival_sim = -1.0
                for other_wid in self._tracks:
                    if other_wid == wid or other_wid in shadows:
                        continue
                    s = self._cluster_gallery_sim(other_wid, a.cluster, t)
                    if s is not None and s > rival_sim:
                        rival_sim = s
                tr.last_reid_similarity = own_sim
                if own_sim is not None:
                    tr.last_reid_margin = own_sim - rival_sim if rival_sim >= 0.0 else own_sim

    @staticmethod
    def _estimate_orientation_bin(gated: GatedSkeleton | None) -> int:
        if gated is None or len(gated.joints) <= max(COCO17_L_SHOULDER, COCO17_R_SHOULDER):
            return 0
        ls = gated.joints[COCO17_L_SHOULDER].xyz
        rs = gated.joints[COCO17_R_SHOULDER].xyz
        if ls is None or rs is None:
            return 0
        dx = float(rs[0] - ls[0])
        dy = float(rs[1] - ls[1])
        if abs(dx) < 1e-4 and abs(dy) < 1e-4:
            return 0
        # 어깨 벡터에 수직인 몸통 정면 방위각
        yaw = math.atan2(dx, -dy)
        return quantize_orientation_bin(yaw, num_bins=4)

    # ------------------------------------------------------------------ 내부

    def _measurement(self, gated: GatedSkeleton) -> tuple[np.ndarray, np.ndarray] | None:
        """게이트를 거친 골반 측정과 표준편차. 골반이 없거나 UNAVAILABLE 이거나 불확실성을 모르면 None."""
        pelvis = gated.pelvis_xyz()
        validity = gated.pelvis_validity()
        if pelvis is None or validity is JointValidity.UNAVAILABLE:
            return None
        hips = [gated.joints[COCO17_L_HIP].sigma_xyz_m, gated.joints[COCO17_R_HIP].sigma_xyz_m]
        if any(s is None for s in hips):
            return None
        sl, sr = np.array(hips[0]), np.array(hips[1])
        if not (np.all(np.isfinite(sl)) and np.all(np.isfinite(sr))):
            return None
        sigma = np.maximum(np.sqrt(sl**2 + sr**2) / 2.0, self.config.min_measurement_sigma_m)
        if validity is not JointValidity.VALID:
            sigma = sigma * self.config.uncertain_measurement_scale
        return np.array(pelvis, dtype=float), sigma

    def _gate(
        self, gate: JointGate, cluster: ViewCluster, frame: AssociationFrame, rig: CameraRig, excluded: frozenset[str]
    ) -> GatedSkeleton:
        """관절 유효성 게이트. Cross-view 클러스터의 왜곡 제거·삼각측량 결과를 넘긴다(입력이 같을 때만 게이트가 다시 쓴다)."""
        index = getattr(self, "_others_index", None)
        if index is None or index.frame is not frame:
            # 같은 시각(같은 연결 결과)의 경쟁 후보 색인은 한 번만 만든다(S15P31A101-726)
            index = self._others_index = _OthersIndex(frame)
        others, others_undistorted = index.others(cluster)
        return gate.update(
            [v.view for v in cluster.views],
            rig,
            others,
            JOINT_LAYOUT,
            excluded,
            undistorted=cluster.undistorted,
            others_undistorted=others_undistorted,
            skeleton=cluster.skeleton,
        )

    def _apply(
        self,
        tr: _Track,
        cluster: ViewCluster,
        frame: AssociationFrame,
        rig: CameraRig,
        excluded: frozenset[str],
        t: int,
        gated: GatedSkeleton | None = None,
    ) -> _Applied:
        if gated is None:
            gated = self._gate(tr.gate, cluster, frame, rig, excluded)
        measurement = self._measurement(gated)
        if measurement is None:
            return _Applied(cluster, gated, None)
        z, sigma = measurement
        cfg = self.config
        if cfg.uncertain_jump_margin_m > 0 and tr.path:
            # -698 ID-5 보완 A: 직전 측정에서 사람이 갈 수 없는 거리로 뛴 측정은 다른 사람 관측을 넘겨받았을 수 있다
            pt, pxy = tr.path[-1]
            if t > pt and float(np.linalg.norm(z[:2] - pxy)) > (
                cfg.max_speed_mps * (t - pt) / 1000.0 + cfg.uncertain_jump_margin_m
            ):
                tr.jump_t = t
        tr.kf.update(t, z, sigma)
        tr.t_last_valid = t
        tr.hits += 1
        tr.path.append((t, z[:2].copy()))
        return _Applied(cluster, gated, (float(z[0]), float(z[1]), float(z[2])))

    def _snapshot(self, tr: _Track, t: int, applied: _Applied | None) -> WorldTrackSnapshot:
        cfg = self.config
        held = {
            f for f, ft in tr.flags.items()
            if t - ft <= (cfg.uncertain_hold_ms if tr.long_hold_flags.get(f) == ft else _hold_ms(cfg, f))
            or (f in _TRANSIENT_FLAGS and t - tr.hidden_hold_t <= cfg.uncertain_hold_transient_ms)
        }
        tr.flags = {f: ft for f, ft in tr.flags.items() if f in held}
        tr.long_hold_flags = {f: ft for f, ft in tr.long_hold_flags.items() if f in held}
        cameras: tuple[str, ...] = ()
        if applied is not None:
            cameras = tuple(sorted(applied.gated.camera_ids))
        if held:
            confidence = IdentityConfidence.IDENTITY_UNCERTAIN
        elif tr.lifecycle is TrackLifecycle.TENTATIVE:
            confidence = IdentityConfidence.LOW
        elif (
            tr.lifecycle is not TrackLifecycle.CONFIRMED
            or applied is None
            or len(cameras) < cfg.min_cameras_for_high
            or t <= tr.reacquired_until
        ):
            confidence = IdentityConfidence.MEDIUM
        else:
            confidence = IdentityConfidence.HIGH
        filt = tr.kf.sigma_xyz
        grow = cfg.max_speed_mps * (t - tr.t_last_valid) / 1000.0
        head = applied.gated.head_z(cfg.joint_gate.head_top_offset_m) if applied is not None else None
        split_pending = any(
            sp.source == tr.world_id or sp.child == tr.world_id for sp in self._splits.values()
        )
        avatar_hold = (
            confidence is IdentityConfidence.IDENTITY_UNCERTAIN
            or split_pending
            or t <= tr.swap_suspected_until
        )
        return WorldTrackSnapshot(
            world_id=tr.world_id,
            t=t,
            t_last_valid=tr.t_last_valid,
            lifecycle=tr.lifecycle,
            identity_confidence=confidence,
            identity_revision=tr.identity_revision,
            position_xy=(float(tr.kf.x[0]), float(tr.kf.x[1])),
            position_xy_sigma=(float(filt[0] + grow), float(filt[1] + grow)),
            velocity=(float(tr.kf.x[3]), float(tr.kf.x[4]), float(tr.kf.x[5])),
            pelvis_xyz=applied.pelvis if applied is not None else None,
            skeleton=applied.gated if applied is not None else None,
            head_z=head,
            cameras=cameras,
            association_id=applied.cluster.association_id if applied is not None else None,
            local_keys=tuple(sorted(applied.cluster.keys)) if applied is not None else (),
            identity_flags=tuple(sorted(held)),
            identity_flags_raised=tuple(sorted(f for f in held if tr.flags[f] == t)),
            aliases=tuple(tr.aliases),
            filter_sigma_xy=(float(filt[0]), float(filt[1])),
            restored_candidates=tr.restored_candidates,
            restored_excluded=tuple(sorted(tr.restored_excluded)),
            split_from=tr.split_from,
            avatar_update_hold=avatar_hold,
            reid_similarity=tr.last_reid_similarity,
            reid_margin=tr.last_reid_margin,
        )


def standing_stature(gated: GatedSkeleton, cfg: WorldTrackConfig) -> float | None:
    """기립 키 표본 = 머리 높이(head_z, 관측 귀·눈 기반) − 두 발목 Z 평균 (-657). 지지 표면 높이를 몰라도 되게 발목을 뺀다.

    머리·골반·두 발목이 모두 관측(좌표 있음)이고 기립 자세(머리 − 발목 ≥ standing_min_height_m, (골반 − 발목)/(머리 − 발목) ≥
    standing_min_pelvis_ratio)일 때만 값을 낸다. 낙상·앉기·웅크림·관측 부족이면 None(지어내지 않음). head_z 는 귀·눈
    keypoint 로 만들어 안전모 유무의 영향이 작다고 본다(설계 문서). 신원 보조 근거로만 쓰고 내보내지 않는다.
    """
    if len(gated.joints) <= COCO17_R_ANKLE:
        return None
    head = gated.head_z(cfg.joint_gate.head_top_offset_m).value
    pelvis = gated.pelvis_xyz()
    left, right = gated.joints[COCO17_L_ANKLE].xyz, gated.joints[COCO17_R_ANKLE].xyz
    if head is None or pelvis is None or left is None or right is None:
        return None
    foot = (left[2] + right[2]) / 2.0
    height = head - foot
    if height < cfg.standing_min_height_m or (pelvis[2] - foot) < cfg.standing_min_pelvis_ratio * height:
        return None
    return float(height)


def _check_clusters(frame: AssociationFrame) -> None:
    """입력 계약: 클러스터마다 카메라당 view 하나, view 마다 클러스터 하나, 모든 view 가 공통 시각 t (명세 5.2).

    같은 카메라의 두 검출이 한 World ID 에 묶이지 않는 것을 Cross-view 주석에만 기대지 않고 여기서도 확인한다.
    """
    seen: set[TrackKey] = set()
    for c in frame.clusters:
        cams = c.camera_ids
        if len(cams) != len(set(cams)):
            raise ValueError(f"{c.association_id}: 같은 카메라의 view 가 둘 이상이다 {sorted(cams)}")
        for v in c.views:
            if v.t != frame.t:
                raise ValueError(f"{c.association_id}: 공통 시각 {frame.t} 가 아닌 view ({v.key}, {v.t})")
            if v.key in seen:
                raise ValueError(f"view {v.key} 가 두 클러스터에 있다")
            seen.add(v.key)


def _prepare(cluster: ViewCluster) -> _Cluster:
    """클러스터 기준점: 골반 XY, 없으면 삼각측량된 관절 XY 평균. 1대뿐이면 None (연결 비용 전용, 위치 아님)."""
    sk: SkeletonEstimate | None = cluster.skeleton
    if sk is None:
        return _Cluster(cluster, None, False)
    if sk.joint_layout != JOINT_LAYOUT:
        raise ValueError(f"World Track Manager 는 {JOINT_LAYOUT} 만 받는다: {sk.joint_layout}")
    pelvis = sk.pelvis_xyz()
    if pelvis is not None:
        return _Cluster(cluster, np.array(pelvis[:2], dtype=float), True)
    xy = [j.xyz[:2] for j in sk.joints if j.xyz is not None]
    if not xy:
        return _Cluster(cluster, None, False)
    return _Cluster(cluster, np.mean(np.array(xy, dtype=float), axis=0), False)


_TRANSIENT_FLAGS = frozenset(
    {IdentityFlag.TWO_VIEW_COMPETITOR_JOINT, IdentityFlag.AMBIGUOUS_TRACK_MATCH, IdentityFlag.CROSS_VIEW_UNCERTAIN}
)
"""S15P31A101-698 ID-5: 매 시각 다시 판정되는 관측 순간 사유. 나머지(분할·재연결·중복·복원 후보)는 상태 사유다"""

_TORSO_JOINTS = (COCO17_L_SHOULDER, COCO17_R_SHOULDER, COCO17_L_HIP, COCO17_R_HIP)


def _hold_ms(cfg: WorldTrackConfig, flag: IdentityFlag) -> int:
    """사유의 IDENTITY_UNCERTAIN 유지 시간(-698 ID-5)."""
    if cfg.uncertain_hold_per_reason and flag in _TRANSIENT_FLAGS:
        return cfg.uncertain_hold_transient_ms
    return cfg.uncertain_hold_ms


def _competitor_flag(gated: GatedSkeleton, cluster: ViewCluster, cfg: WorldTrackConfig | None = None) -> bool:
    """관절 유효성 게이트의 2대 관측 경쟁 후보를 Track 의 IDENTITY_UNCERTAIN 으로 올릴지.

    경쟁 후보 관절 자체는 게이트가 이미 UNCERTAIN 으로 낮췄다(위험 규칙은 "확인 필요"). 3대 이상이 맞춘 사람은 위치 근거인
    엉덩이 관절이 경쟁 후보일 때만 신원을 불확실로 둔다. 2대뿐인 클러스터는 연결 근거가 약하므로, -698 ID-6 부터는 몸통
    관절(양 어깨·양 엉덩이) 중 하나가 경쟁 후보일 때 신원을 불확실로 둔다. 손목·발목·얼굴만의 경쟁은 그 관절만 UNCERTAIN 이다.
    cfg.two_view_identity_torso_only 가 False 면 2대 클러스터는 어느 관절 경쟁이든 신원 불확실(이전 동작)이다.
    """
    if not gated.identity_uncertain_candidate:
        return False
    if len(cluster.views) <= 2:
        if cfg is None or not cfg.two_view_identity_torso_only:
            return True
        return any(
            j < len(gated.joints) and GateIssue.TWO_VIEW_COMPETITOR in gated.joints[j].reasons for j in _TORSO_JOINTS
        )
    return any(
        GateIssue.TWO_VIEW_COMPETITOR in gated.joints[j].reasons for j in (COCO17_L_HIP, COCO17_R_HIP)
    )


class _OthersIndex:
    """한 연결 결과(AssociationFrame)의 카메라별 view 색인 (관절 유효성 게이트의 2대 관측 경쟁 후보 검사용).

    `others(cluster)` 는 같은 시각 다른 클러스터의 카메라별 keypoint 와 그 왜곡 제거 결과다. 왜곡 제거 결과는 keypoint 와
    같은 카메라·같은 순서이고, 한 카메라에서 하나라도 없으면 그 카메라는 넣지 않는다(게이트가 순서가 어긋난 결과를 쓰지
    않도록. 넣지 않은 카메라는 게이트가 다시 푼다). 클러스터마다 frame 전체 view 를 다시 모으지 않도록 카메라별 (클러스터
    위치, keypoint, 왜곡 제거 결과) 목록을 frame 마다 한 번 만들고 자기 위치만 거른다. 카메라마다 클러스터 순서·view 순서가
    같아 목록 내용·순서가 클러스터마다 모으던 이전 계산과 같다(S15P31A101-726, 시험으로 확인).
    """

    def __init__(self, frame: AssociationFrame) -> None:
        self.frame = frame
        self._by_camera: dict[str, list[tuple[int, Sequence[Any], UndistortedKeypoints | None]]] = {}
        for p, c in enumerate(frame.clusters):
            for v in c.views:
                self._by_camera.setdefault(v.camera_id, []).append((p, v.view.keypoints, c.undistorted.get(v.camera_id)))

    def others(
        self, cluster: ViewCluster
    ) -> tuple[dict[str, list[Sequence[Any]]], dict[str, list[UndistortedKeypoints]]]:
        own = {p for p, c in enumerate(self.frame.clusters) if c is cluster}
        others: dict[str, list[Sequence[Any]]] = {}
        undistorted: dict[str, list[UndistortedKeypoints]] = {}
        for cid, entries in self._by_camera.items():
            rows = [(kps, u) for p, kps, u in entries if p not in own]
            if not rows:
                continue
            others[cid] = [kps for kps, _ in rows]
            if all(u is not None for _, u in rows):
                undistorted[cid] = [u for _, u in rows]  # type: ignore[misc]
        return others, undistorted


def _cameras(keys: Iterable[TrackKey]) -> int:
    """로컬 트랙들이 나온 서로 다른 카메라 수."""
    return len({k[0] for k in keys})


def _own_update(a: _Applied | None, holder: Mapping[TrackKey, str], wid: str, cfg: WorldTrackConfig) -> bool:
    """일관성 확인 한 시각: 자기 로컬 트랙만으로(reconnect_min_cameras 대 이상) 위치가 갱신됐는지."""
    return (
        a is not None
        and a.pelvis is not None
        and all(holder.get(k) == wid for k in a.cluster.keys)
        and _cameras(a.cluster.keys) >= cfg.reconnect_min_cameras
    )


def _split_cameras(
    kept: ViewCluster,
    other: ViewCluster,
    keys: Collection[TrackKey],
    holder: Mapping[TrackKey, str],
    source: str,
    child: str | None = None,
) -> int:
    """source 의 로컬 트랙이 같은 카메라의 두 검출(kept, other)로 갈라진 카메라 수.

    kept 의 그 카메라 view 는 지금 주인이 source 인 로컬 트랙, other 의 같은 카메라 view 는 keys(source 에게서 넘겨받은
    로컬 트랙) 중 지금 주인이 source(또는 child) 인 것이어야 한다.
    """
    kept_cams = {v.camera_id for v in kept.views if holder.get(v.key) == source}
    owners = {source} if child is None else {source, child}
    other_cams = {v.camera_id for v in other.views if v.key in keys and holder.get(v.key) in owners}
    return len(kept_cams & other_cams)


def _horizontal(p: tuple[float, float, float] | None, q: tuple[float, float, float] | None) -> float:
    if p is None or q is None:
        raise ValueError("분할 거리: 골반 측정이 없다")
    return float(math.hypot(p[0] - q[0], p[1] - q[1]))


def _rename(pair: frozenset[str], old: str, new: str) -> frozenset[str]:
    return frozenset(new if w == old else w for w in pair)
