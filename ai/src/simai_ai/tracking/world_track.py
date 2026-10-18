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
6. 배정되지 않은 클러스터 중 골반이 있는 것만 새 World Track(TENTATIVE)이 된다. 1대 클러스터나 골반 없는 클러스터로는
   만들지 않는다(2대 미만은 지어내지 않음). 다른 Track 의 예측 위치 가까이(`duplicate_radius_m`)에 생기면 같은 사람의
   두 번째 ID 일 수 있어 그 쌍을 POSSIBLE_DUPLICATE 후보로 둔다.
7. 같은 사람 두 ID 로 보이는 Track 은 **그 시각에는 합치지 않는다.** 붙인 클러스터에 이번 시각 갱신되지 않은 다른
   Track 의 로컬 트랙이 들어 있고 그 Track 이 가까워도, 한 시각만으로는 로컬 트래커 ID 전환(옆 사람에게 ID 가 넘어감)과
   구별할 근거가 없으므로 둘 다 남기고 POSSIBLE_DUPLICATE 후보 쌍으로 둔다. 후보 쌍은 둘 다 살아 있고 가까운 동안 매
   시각 IDENTITY_UNCERTAIN(POSSIBLE_DUPLICATE)을 다시 붙인다. 근거가 쌓이면 아래 늦은 재연결의 병합으로 합친다.

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
- 분할(SPLIT)과 수동 교정(CORRECTION)은 내지 않는다(별도 티켓).

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
`LifecycleTracker.correct_identity(revision.to_correction())` 로 넘긴다. 진행 중인 이벤트는 사라지는 ID 에서 남는 ID 로
이어지고(원본 World ID 는 origin_world_ids 에 남음) 자동 해소되지 않는다(D-G3-10).

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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from simai_ai.geometry.joint_gate import GatedSkeleton, GateIssue, HeadZ, JointGate, JointGateConfig
from simai_ai.geometry.rig import CameraRig, UndistortedKeypoints
from simai_ai.geometry.triangulation import COCO17_L_HIP, COCO17_R_HIP, JointValidity, SkeletonEstimate
from simai_ai.risk.lifecycle import IdentityCorrection
from simai_ai.tracking.assignment import linear_sum_assignment
from simai_ai.tracking.cross_view import AssociationFrame, TrackKey, ViewCluster
from simai_ai.tracking.time_model import ObservationState, TimeModelConfig, TrackTimeInput, require_ms

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
    """ID 교정 종류 (명세 5.2 병합·분할·교정). 지금은 MERGE 만 낸다(SPLIT·CORRECTION 은 자리만)."""

    MERGE = "MERGE"
    SPLIT = "SPLIT"
    CORRECTION = "CORRECTION"


class RevisionReason(StrEnum):
    """병합 근거 (`IdentityRevision.reason`)."""

    LATE_REASSOCIATION = "LATE_REASSOCIATION"
    """소실 후보(LOST)의 로컬 트랙이 이미 새 ID 를 받은 Track 에 이어졌다"""
    DUPLICATE_MERGE = "DUPLICATE_MERGE"
    """가림 중인(LOST 전) Track 의 로컬 트랙이 다른 ID 의 Track 에 이어졌다 (POSSIBLE_DUPLICATE 쌍 포함)"""


@dataclass(frozen=True)
class IdentityRevision:
    """ID 교정 기록 한 건. previous_world_id 가 world_id 로 합쳐졌다(별칭이 됐다). 기록은 추가만 한다.

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
    """근거가 된 (카메라, 로컬 트랙)"""

    def to_correction(self) -> IdentityCorrection:
        """Risk Engine 에 넘길 교정(`LifecycleTracker.correct_identity`). 병합만 넘길 수 있다."""
        if self.kind is not RevisionKind.MERGE:
            raise ValueError(f"{self.kind} 는 Risk 교정으로 넘길 형식이 없다 (MERGE 만)")
        return IdentityCorrection(
            previous_world_id=self.previous_world_id,
            world_id=self.world_id,
            identity_revision=self.identity_revision,
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
    """OCCLUDED 가 마지막 측정 뒤 이 시간을 넘으면 LOST. 그 뒤에는 늦은 재연결 근거가 있을 때만 같은 ID 로 잇는다"""
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
    uncertain_hold_ms: int = 1000
    """IDENTITY_UNCERTAIN 을 마지막 사유 뒤 이 시간 동안 유지한다 (깜박임 방지, 보수적)"""
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
    joint_gate: JointGateConfig = field(default_factory=JointGateConfig)

    def __post_init__(self) -> None:
        for name in ("confirm_hits", "merge_confirm_frames"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 는 1 이상이어야 한다")
        for name in (
            "tentative_max_gap_ms",
            "occluded_after_ms",
            "lost_after_ms",
            "reconnect_window_ms",
            "key_ttl_ms",
            "uncertain_hold_ms",
            "max_extrapolation_ms",
            "restore_max_age_ms",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 는 0 이상이어야 한다")
        if not self.occluded_after_ms <= self.lost_after_ms <= self.reconnect_window_ms:
            raise ValueError("occluded_after_ms ≤ lost_after_ms ≤ reconnect_window_ms 여야 한다")
        if self.reconnect_min_cameras < 2:
            raise ValueError("reconnect_min_cameras 는 2 이상이어야 한다 (로컬 트랙 하나는 독립 근거가 아니다)")
        if self.reconnect_crowd_radius_m < 0:
            raise ValueError("reconnect_crowd_radius_m 은 0 이상이어야 한다")
        for name in (
            "gate_base_m",
            "gate_max_m",
            "gate_scale_m",
            "duplicate_radius_m",
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
    aliases: tuple[str, ...] = ()
    """병합으로 이 ID 에 합쳐진 이전 ID"""
    filter_sigma_xy: tuple[float, float] = (0.0, 0.0)
    """t_last_valid 의 필터 σ (공백 확대 전)"""
    restored_candidates: tuple[str, ...] = ()
    """재시작 전 Track(복원 Track) 중 이 사람일 수 있는 것 (첫 관측이 갈 수 있는 거리 안, 가까운 순). 신원 근거가 아니라
    이전 이벤트 연결(related_event_id)의 후보다. 위치를 주지 않는다 (S15P31A101-54)"""
    restored_excluded: tuple[str, ...] = ()
    """첫 관측 때 남아 있던 복원 Track 중 갈 수 없는 거리여서 이 사람이 아닌 것"""

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
    """로컬 트랙 → 마지막으로 이 Track 에 붙은 시각. `reconnect_window_ms` 까지 남기고, 비용 보너스·주인 판단에는
    `key_ttl_ms` 안의 것만 쓴다"""
    flags: dict[IdentityFlag, int] = field(default_factory=dict)
    """IDENTITY_UNCERTAIN 사유 → 마지막으로 붙은 시각"""
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


class WorldTrackManager:
    """공통 시각마다 Cross-view 결과로 World Track 을 갱신한다. 시각은 늘어나는 순서로만 넣는다."""

    def __init__(
        self,
        issuer: WorldIdIssuer,
        config: WorldTrackConfig | None = None,
        input_provenance: Mapping[str, Any] | None = None,
    ) -> None:
        self.issuer = issuer
        self.config = config if config is not None else WorldTrackConfig()
        self.input_provenance = dict(input_provenance) if input_provenance is not None else None
        self._tracks: dict[str, _Track] = {}
        self._dormant: dict[str, _Track] = {}
        """소실 후보: LOST 를 한 번 낸 CONFIRMED Track (늦은 재연결용. world_state 에는 내지 않는다)"""
        self._revisions: list[IdentityRevision] = []
        self._duplicates: set[frozenset[str]] = set()
        """같은 사람 두 ID 로 보이는 World ID 쌍 (POSSIBLE_DUPLICATE 를 다시 붙인다)"""
        self._distinct: set[frozenset[str]] = set()
        """같은 시각 같은 카메라의 다른 검출로 따로 보인 적이 있는 World ID 쌍. 서로 다른 두 사람이라 병합하지 않는다"""
        self._evidence: dict[str, _Evidence] = {}
        """갱신된 Track ID → 병합 근거 (갱신되지 않은 Track 의 로컬 트랙이 이어진 시각 수)"""
        self._last_t: int | None = None
        self._order = 0
        self._restore_input: tuple[tuple[TrackCheckpoint, ...], int | None] | None = None
        self._restored: dict[str, TrackCheckpoint] = {}
        """후보로 남은 복원 Track (S15P31A101-54). 연결·world_state·export_tracks 에 넣지 않는다"""
        self._restore_status = RestoreStatus()

    @property
    def revisions(self) -> tuple[IdentityRevision, ...]:
        """지금까지의 ID 교정 기록 (추가만 한다)."""
        return tuple(self._revisions)

    @property
    def lost_candidates(self) -> tuple[str, ...]:
        """늦은 재연결을 기다리는 소실 후보 World ID."""
        return tuple(sorted(self._dormant))

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
        self._last_t = t
        self._restore_step(t)

        keep_ms = max(cfg.key_ttl_ms, cfg.reconnect_window_ms)
        for tr in self._all():
            tr.keys = {k: kt for k, kt in tr.keys.items() if t - kt <= keep_ms}
            tr.path = [(pt, xy) for pt, xy in tr.path if t - pt <= cfg.reconnect_window_ms]
        for wid in [w for w, tr in self._dormant.items() if t - tr.t_last_valid > cfg.reconnect_window_ms]:
            del self._dormant[wid]
        self._forget_gone()
        # 비용 보너스·주인 판단은 key_ttl_ms 안의 연결 이력만, 늦은 재연결 근거는 소실 후보까지 남긴 이력 전부
        owner = {
            k: tr.world_id for tr in self._tracks.values() for k, kt in tr.keys.items() if t - kt <= cfg.key_ttl_ms
        }
        holder = {k: tr.world_id for tr in self._all() for k in tr.keys}
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
            d = float(np.linalg.norm(c.anchor - predicted[tr.world_id]))
            radius = min(cfg.gate_max_m, cfg.gate_base_m + cfg.max_speed_mps * (t - tr.t_last_valid) / 1000.0)
            if d > radius:
                return math.inf
            keys = c.cluster.keys
            shared = sum(1 for k in keys if owner.get(k) == tr.world_id)
            return d / cfg.gate_scale_m - cfg.key_bonus * shared / len(keys)

        anchored = [c for c in clusters if c.anchor is not None]
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
                for other in rival_tracks:
                    flags[other].add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)
                for o in rival_clusters:
                    for other, oc in assigned.items():
                        if oc is o:
                            flags[other].add(IdentityFlag.AMBIGUOUS_TRACK_MATCH)

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
            if not rivals and not foreign:
                assigned[best.world_id] = c
                taken.add(id(c))
                flags[best.world_id].add(IdentityFlag.CROSS_VIEW_UNCERTAIN)
            else:
                for tr in feasible:
                    flags[tr.world_id].add(IdentityFlag.CROSS_VIEW_UNCERTAIN)

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
            if _competitor_flag(result.gated, c.cluster):
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
            target, ambiguous, unreachable = self._reconnect_target(c, holder, held, cluster_of, applied, t)
            if target is not None:
                was_lost = target in self._dormant
                result = self._reconnect(target, c.cluster, frame, rig, excluded, t)
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
                    if _competitor_flag(result.gated, c.cluster):
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
                continue
            z, sigma = measurement
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
            if unreachable is not None:
                # 첫 관측이 그 Track 의 마지막 관측에서 갈 수 없는 거리다: 같은 사람이 아니다(병합 근거로 세지 않는다)
                self._distinct.add(frozenset((unreachable, tr.world_id)))
            applied[tr.world_id] = _Applied(c.cluster, gated, (float(z[0]), float(z[1]), float(z[2])))
            new_flags = set()
            if c.uncertain:
                new_flags.add(IdentityFlag.CROSS_VIEW_UNCERTAIN)
            if _competitor_flag(gated, c.cluster):
                new_flags.add(IdentityFlag.TWO_VIEW_COMPETITOR_JOINT)
            if ambiguous:
                new_flags.add(IdentityFlag.REASSOCIATION_AMBIGUOUS)
            for other_id, xy in nearby.items():
                if np.linalg.norm(c.anchor - xy) <= cfg.duplicate_radius_m:
                    self._duplicates.add(frozenset((tr.world_id, other_id)))
            flags[tr.world_id] = new_flags
            nearby[tr.world_id] = z[:2]

        # 병합 (ID 교정): 근거 시각이 merge_confirm_frames 번 쌓였으면 먼저 만든 ID 로 합친다
        revisions: list[IdentityRevision] = []
        for wid, ev in sorted(self._evidence.items()):
            if ev.count < cfg.merge_confirm_frames or wid not in self._evidence:
                continue
            if ev.stale in applied or wid not in self._tracks or self._find(ev.stale) is None:
                del self._evidence[wid]  # 이번 시각 따로 갱신됐거나 사라졌다
                continue
            rec = self._merge(ev.stale, wid, ev, t, applied, flags)
            revisions.append(rec)
            linked.add(rec.world_id)

        # 병합 금지 쌍: 이번 시각 새로 생기거나 다시 이은 Track 까지 (다음 시각 근거에 쓴다)
        self._mark_distinct(frame, holder, {wid: a.cluster for wid, a in applied.items()})

        # 늦은 재연결·병합 뒤 일관성 확인: 자기 로컬 트랙만으로(reconnect_min_cameras 대 이상) 위치가 갱신된 시각을 센다.
        # 다 셀 때까지 LATE_REASSOCIATION 을 매 시각 붙인다. 넘겨받은 로컬 트랙의 옛 주인이 남아 있으면 애매 표시
        for wid, tr in self._tracks.items():
            if tr.confirm_left > 0 and wid not in linked:
                a = applied.get(wid)
                if (
                    a is not None
                    and a.pelvis is not None
                    and all(holder.get(k) == wid for k in a.cluster.keys)
                    and _cameras(a.cluster.keys) >= cfg.reconnect_min_cameras
                ):
                    tr.confirm_left -= 1
            if tr.confirm_left > 0:
                flags.setdefault(wid, set()).add(IdentityFlag.LATE_REASSOCIATION)
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

        for wid, fs in flags.items():
            tr = self._tracks.get(wid)
            if tr is None:
                continue
            for f in fs:
                tr.flags[f] = t

        # 생명주기
        newly_lost: list[tuple[_Track, bool]] = []
        for tr in list(self._tracks.values()):
            gap = t - tr.t_last_valid
            was_confirmed = tr.lifecycle is not TrackLifecycle.TENTATIVE
            if tr.lifecycle is TrackLifecycle.TENTATIVE:
                if gap == 0 and tr.hits >= cfg.confirm_hits:
                    tr.lifecycle = TrackLifecycle.CONFIRMED
                elif gap > cfg.tentative_max_gap_ms:
                    tr.lifecycle = TrackLifecycle.LOST
            elif gap == 0:
                tr.lifecycle = TrackLifecycle.CONFIRMED
            elif gap > cfg.lost_after_ms:
                tr.lifecycle = TrackLifecycle.LOST
            elif gap > cfg.occluded_after_ms:
                tr.lifecycle = TrackLifecycle.OCCLUDED
            if tr.lifecycle is TrackLifecycle.LOST:
                newly_lost.append((tr, was_confirmed))

        snapshots = [self._snapshot(tr, t, applied.get(tr.world_id)) for tr in self._tracks.values()]
        for tr, was_confirmed in newly_lost:
            self._tracks.pop(tr.world_id, None)
            if was_confirmed and cfg.reconnect_window_ms > cfg.lost_after_ms:
                self._dormant[tr.world_id] = tr  # LOST 는 한 번만 내고 소실 후보로 둔다
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

    def _all(self) -> list[_Track]:
        """살아 있는 Track 과 소실 후보."""
        return [*self._tracks.values(), *self._dormant.values()]

    def _find(self, world_id: str) -> _Track | None:
        return self._tracks.get(world_id) or self._dormant.get(world_id)

    def _forget_gone(self) -> None:
        """사라진 ID 가 든 병합 금지 쌍·병합 근거를 지운다."""
        alive = set(self._tracks) | set(self._dormant)
        self._distinct = {p for p in self._distinct if p <= alive}
        self._evidence = {w: ev for w, ev in self._evidence.items() if w in alive and ev.stale in alive}

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
                                self._distinct.add(frozenset((p, q)))

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
                or frozenset((origin, tr.world_id)) in self._distinct
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
        for wid in crowd:
            other_tr = self._find(wid)
            if other_tr is None:
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
        if stale_id not in stale_ids or frozenset((stale_id, wid)) in self._distinct:
            return None, True
        reason = self._link_check(stale_id, c, held, cluster_of, stale_ids - {stale_id}, t, y.path)
        if reason == "unreachable":
            self._distinct.add(frozenset((stale_id, wid)))  # 갈 수 없는 곳에서 관측된 적이 있다: 같은 사람이 아니다
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
            return None, False, None
        holders = {holder[k] for k in c.cluster.keys if k in holder}
        if not holders or holders & set(applied):
            # 로컬 트랙 근거가 없거나, 이번 시각 갱신된 Track 의 로컬 트랙이 섞였다(갈라진 view·ID 전환): 지금 동작대로
            return None, False, None
        if len(holders) > 1:
            return None, True, None
        (stale_id,) = holders
        crowd = [w for w in (*self._tracks, *self._dormant) if w != stale_id and w not in applied]
        reason = self._link_check(stale_id, c, held, cluster_of, crowd, t)
        if reason is not None:
            return None, True, (stale_id if reason == "unreachable" else None)
        if _cameras(k for k in c.cluster.keys if holder.get(k) == stale_id) < self.config.reconnect_min_cameras:
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
        """클러스터를 Track 에 잇는다. 소실 후보는 위치 측정이 있을 때만 살리고 게이트 이력을 새로 시작한다."""
        if world_id in self._tracks:
            return self._apply(self._tracks[world_id], cluster, frame, rig, excluded, t)
        tr = self._dormant[world_id]
        gate = JointGate(self.config.joint_gate)
        gated = self._gate(gate, cluster, frame, rig, excluded)
        if self._measurement(gated) is None:
            return None  # 위치 없이 살리면 다음 생명주기에서 다시 LOST 가 된다
        del self._dormant[world_id]
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
        stale = self._find(stale_id)
        live = self._tracks[live_id]
        assert stale is not None
        was_lost = stale_id in self._dormant
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
        candidates = [*survivor.restored_candidates, *retired.restored_candidates]
        survivor.restored_candidates = tuple(dict.fromkeys(candidates))
        survivor.restored_excluded = (survivor.restored_excluded | retired.restored_excluded) - set(candidates)
        origins = {**survivor.origin_of, **retired.origin_of}
        survivor.origin_of = {k: v for k, v in origins.items() if v[0] not in (stale_id, live_id)}
        survivor.confirm_left = self.config.merge_confirm_frames
        merged_flags = flags.setdefault(survivor.world_id, set())
        merged_flags |= flags.pop(retired.world_id, set())
        self._tracks.pop(retired.world_id, None)
        self._dormant.pop(retired.world_id, None)
        self._dormant.pop(survivor.world_id, None)
        self._tracks[survivor.world_id] = survivor
        old, new = retired.world_id, survivor.world_id
        self._duplicates = {p for p in (_rename(p, old, new) for p in self._duplicates) if len(p) == 2}
        self._distinct = {p for p in (_rename(p, old, new) for p in self._distinct) if len(p) == 2}
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
        others, others_undistorted = _others(frame, cluster)
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
        tr.kf.update(t, z, sigma)
        tr.t_last_valid = t
        tr.hits += 1
        tr.path.append((t, z[:2].copy()))
        return _Applied(cluster, gated, (float(z[0]), float(z[1]), float(z[2])))

    def _snapshot(self, tr: _Track, t: int, applied: _Applied | None) -> WorldTrackSnapshot:
        cfg = self.config
        held = {f for f, ft in tr.flags.items() if t - ft <= cfg.uncertain_hold_ms}
        tr.flags = {f: ft for f, ft in tr.flags.items() if f in held}
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
            aliases=tuple(tr.aliases),
            filter_sigma_xy=(float(filt[0]), float(filt[1])),
            restored_candidates=tr.restored_candidates,
            restored_excluded=tuple(sorted(tr.restored_excluded)),
        )


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


def _competitor_flag(gated: GatedSkeleton, cluster: ViewCluster) -> bool:
    """관절 유효성 게이트의 2대 관측 경쟁 후보를 Track 의 IDENTITY_UNCERTAIN 으로 올릴지.

    경쟁 후보 관절 자체는 게이트가 이미 UNCERTAIN 으로 낮췄다(위험 규칙은 "확인 필요"). 사람 신원까지 불확실하다고 보는
    것은 연결 근거가 2대뿐인 클러스터이거나, 위치 근거인 엉덩이 관절이 경쟁 후보일 때다. 3대 이상이 맞춘 사람의 손목
    하나가 2대 관측 경쟁 후보라고 신원 전체를 불확실로 두지 않는다.
    """
    if not gated.identity_uncertain_candidate:
        return False
    if len(cluster.views) <= 2:
        return True
    return any(
        GateIssue.TWO_VIEW_COMPETITOR in gated.joints[j].reasons for j in (COCO17_L_HIP, COCO17_R_HIP)
    )


def _others(
    frame: AssociationFrame, cluster: ViewCluster
) -> tuple[dict[str, list[Sequence[Any]]], dict[str, list[UndistortedKeypoints]]]:
    """같은 시각 다른 클러스터의 카메라별 keypoint 와 그 왜곡 제거 결과 (관절 유효성 게이트의 2대 관측 경쟁 후보 검사용).

    왜곡 제거 결과는 keypoint 와 같은 카메라·같은 순서다. 한 카메라에서 하나라도 없으면 그 카메라는 넣지 않는다(게이트가
    순서가 어긋난 결과를 쓰지 않도록. 넣지 않은 카메라는 게이트가 다시 푼다).
    """
    others: dict[str, list[Sequence[Any]]] = {}
    undistorted: dict[str, list[UndistortedKeypoints]] = {}
    missing: set[str] = set()
    for c in frame.clusters:
        if c is cluster:
            continue
        for v in c.views:
            others.setdefault(v.camera_id, []).append(v.view.keypoints)
            u = c.undistorted.get(v.camera_id)
            if u is None:
                missing.add(v.camera_id)
            else:
                undistorted.setdefault(v.camera_id, []).append(u)
    return others, {cid: us for cid, us in undistorted.items() if cid not in missing}


def _cameras(keys: Iterable[TrackKey]) -> int:
    """로컬 트랙들이 나온 서로 다른 카메라 수."""
    return len({k[0] for k in keys})


def _rename(pair: frozenset[str], old: str, new: str) -> frozenset[str]:
    return frozenset(new if w == old else w for w in pair)
