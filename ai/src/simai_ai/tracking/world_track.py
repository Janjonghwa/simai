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
7. 같은 사람 두 ID 로 보이는 Track 은 **합치지 않는다.** 붙인 클러스터에 이번 시각 갱신되지 않은 다른 Track 의 로컬
   트랙이 들어 있고 그 Track 이 가까워도, 로컬 트래커 ID 전환(옆 사람에게 ID 가 넘어감)과 구별할 근거가 없으므로 둘 다
   남기고 POSSIBLE_DUPLICATE 후보 쌍으로 둔다. 후보 쌍은 둘 다 살아 있고 가까운 동안 매 시각 IDENTITY_UNCERTAIN
   (POSSIBLE_DUPLICATE)을 다시 붙인다. 병합·분할·교정과 늦은 재연결은 S15P31A101-40 이다. 여기서는 ID 교정 기록 자리
   (`IdentityRevision`, `identity_revision`, 별칭)만 두고 교정은 내지 않는다(identity_revision 은 0 그대로).

같은 카메라의 두 검출은 Cross-view 클러스터에서 이미 갈라져 있고(카메라당 view 하나), 여기서는 Track 하나에 시각마다
클러스터 하나만 붙이므로 같은 카메라의 두 검출이 한 World ID 에 동시에 묶이지 않는다.

**World 칼만 필터.** 골반 XYZ 를 측정으로 받는 등속 모델(상태 x y z vx vy vz, 백색 가속도 잡음). 측정 잡음은 관절
유효성 게이트의 두 엉덩이 삼각측량 표준편차로 만들고, 골반이 UNCERTAIN 이면 키운다. 골반이 없거나 UNAVAILABLE 이면
측정이 없는 시각이다(t_last_valid 를 늘리지 않음, 6.1 "예측 위치를 관측처럼 취급하지 않는다").
내보내는 `position_xy` 는 t_last_valid 의 필터 위치이고, 관측이 없는 시각에는 속도로 옮기지 않고 σ 만
`max_speed_mps × (t − t_last_valid)` 만큼 키운다. 위치 예측은 시간 모델(`time_model.evaluate_track`)이 staleness 로
한다. 연결 게이트에 쓰는 예측은 `max_extrapolation_ms`(6.1 PREDICTED 상한)까지만 속도로 옮긴다.

**생명주기와 관측 상태.** 생명주기 TENTATIVE → CONFIRMED → OCCLUDED → LOST 는 관리용이고, 관측 상태
(OBSERVED/PREDICTED/UNKNOWN)는 `world_state` 조립 때 시간 모델(`time_model.classify_observation`)로 따로 정한다.
명세에 수치가 없어 확정·가림·소실 시간은 `WorldTrackConfig` 의 초기값이다(G2 에서 조정).

관절 유효성 게이트(`geometry.joint_gate.JointGate`)는 Track 마다 하나씩 두고, 병합으로 `identity_revision` 이 오르면
`reset()` 한다. 이 모듈의 관절·위치는 모두 Tier 0 관측(observed)이다. 접지(`ground_anchor_xyz`)는 만들지 않는다.
현재 시각을 읽지 않는다. 시각은 UTC int64 ms, 길이는 미터다.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from simai_ai.geometry.joint_gate import GatedSkeleton, GateIssue, HeadZ, JointGate, JointGateConfig
from simai_ai.geometry.rig import CameraRig
from simai_ai.geometry.triangulation import COCO17_L_HIP, COCO17_R_HIP, JointValidity, SkeletonEstimate
from simai_ai.tracking.assignment import linear_sum_assignment
from simai_ai.tracking.cross_view import AssociationFrame, TrackKey, ViewCluster
from simai_ai.tracking.time_model import TimeModelConfig, TrackTimeInput, require_ms

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


class RevisionKind(StrEnum):
    """ID 교정 종류 (명세 5.2 병합·분할·교정). 기록 자리만 두고 지금은 내지 않는다(S15P31A101-40 이 채운다)."""

    MERGE = "MERGE"
    SPLIT = "SPLIT"
    CORRECTION = "CORRECTION"


@dataclass(frozen=True)
class IdentityRevision:
    """ID 교정 기록 한 건. previous_world_id 가 world_id 로 합쳐졌다(별칭이 됐다).

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
    """OCCLUDED 가 마지막 측정 뒤 이 시간을 넘으면 LOST. 그 뒤 다시 보이면 새 ID 다(늦은 재연결은 S15P31A101-40)"""
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
    joint_gate: JointGateConfig = field(default_factory=JointGateConfig)

    def __post_init__(self) -> None:
        for name in ("confirm_hits",):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 는 1 이상이어야 한다")
        for name in (
            "tentative_max_gap_ms",
            "occluded_after_ms",
            "lost_after_ms",
            "key_ttl_ms",
            "uncertain_hold_ms",
            "max_extrapolation_ms",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 는 0 이상이어야 한다")
        if not self.occluded_after_ms <= self.lost_after_ms:
            raise ValueError("occluded_after_ms ≤ lost_after_ms 여야 한다")
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

    tracks: LOST 가 아닌 Track 과 이번 시각에 LOST 가 된 Track(한 번만). world_id 순서.
    revisions: 이번 시각에 생긴 ID 교정 기록. 지금은 늘 비어 있다(교정은 S15P31A101-40).
    input_provenance: 이 결과를 만든 입력 출처(`common/defs.schema.json#/$defs/input_provenance`). world_state 조립이
        payload.input_provenance(계약 1.1.0)로 싣는다. SIM·DATASET_GT 입력이면 is_upper_bound 가 참이다.
    """

    t: int
    calibration_version: str
    tracks: tuple[WorldTrackSnapshot, ...]
    revisions: tuple[IdentityRevision, ...] = ()
    input_provenance: Mapping[str, Any] | None = None

    def track(self, world_id: str) -> WorldTrackSnapshot | None:
        for tr in self.tracks:
            if tr.world_id == world_id:
                return tr
        return None


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
    """로컬 트랙 → 마지막으로 이 Track 에 붙은 시각"""
    flags: dict[IdentityFlag, int] = field(default_factory=dict)
    """IDENTITY_UNCERTAIN 사유 → 마지막으로 붙은 시각"""
    reacquired_until: int = -1
    """연결 이력 없이 게이트만으로 다시 잡은 뒤 MEDIUM 으로 두는 시각"""


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
        self._revisions: list[IdentityRevision] = []
        self._duplicates: set[frozenset[str]] = set()
        """같은 사람 두 ID 로 보이는 World ID 쌍 (POSSIBLE_DUPLICATE 를 다시 붙인다)"""
        self._last_t: int | None = None
        self._order = 0

    @property
    def revisions(self) -> tuple[IdentityRevision, ...]:
        """지금까지의 ID 교정 기록 (추가만 한다)."""
        return tuple(self._revisions)

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

        for tr in self._tracks.values():
            tr.keys = {k: kt for k, kt in tr.keys.items() if t - kt <= cfg.key_ttl_ms}
        owner = {k: tr.world_id for tr in self._tracks.values() for k in tr.keys}
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

        # 7단계: 같은 사람 두 ID 로 보이는 Track 은 합치지 않고 둘 다 남긴 채 표시한다 (병합·교정은 S15P31A101-40).
        # 로컬 트랙 ID 하나가 옆 사람에게 넘어간 경우(로컬 트래커 ID 전환)와 구별할 근거가 없기 때문이다.
        for wid, c in assigned.items():
            for k in c.cluster.keys:
                other_id = owner.get(k)
                if other_id is None or other_id == wid or other_id in assigned:
                    continue
                if np.linalg.norm(c.anchor - predicted[other_id]) <= cfg.duplicate_radius_m:
                    self._duplicates.add(frozenset((wid, other_id)))

        # 갱신 적용 (관절 유효성 게이트 → 칼만 필터)
        applied: dict[str, _Applied] = {}
        for wid, c in assigned.items():
            tr = self._tracks[wid]
            reacquired = not any(owner.get(k) == wid for k in c.cluster.keys)
            result = self._apply(tr, c.cluster, frame, rig, excluded, t)
            applied[wid] = result
            for k in c.cluster.keys:
                old = owner.get(k)
                if old is not None and old != wid and old in self._tracks:
                    self._tracks[old].keys.pop(k, None)
                tr.keys[k] = t
                owner[k] = wid
            if _competitor_flag(result.gated, c.cluster):
                flags[wid].add(IdentityFlag.TWO_VIEW_COMPETITOR_JOINT)
            if result.pelvis is not None and reacquired and tr.lifecycle is TrackLifecycle.OCCLUDED:
                tr.reacquired_until = t + cfg.uncertain_hold_ms

        # 5단계: 1대뿐인 클러스터(와 붙지 않은 클러스터)는 위치를 갱신하지 않고 연결 이력만 잇는다
        for c in clusters:
            if id(c) in taken:
                continue
            for k in c.cluster.keys:
                wid = owner.get(k)
                if wid is not None and wid not in assigned:
                    self._tracks[wid].keys[k] = t

        # 6단계: 새 World Track. 기존 Track(예측 위치)과 이번 시각에 먼저 생긴 Track 가까이면 POSSIBLE_DUPLICATE
        nearby: dict[str, np.ndarray] = dict(predicted)
        for c in anchored:
            if id(c) in taken or id(c) in blocked or not c.has_pelvis:
                continue
            gate = JointGate(cfg.joint_gate)
            gated = gate.update(
                [v.view for v in c.cluster.views], rig, _others(frame, c.cluster), JOINT_LAYOUT, excluded
            )
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
            self._tracks[tr.world_id] = tr
            for k in c.cluster.keys:
                old = owner.get(k)
                if old is not None and old in self._tracks:
                    self._tracks[old].keys.pop(k, None)
                tr.keys[k] = t
                owner[k] = tr.world_id
            applied[tr.world_id] = _Applied(c.cluster, gated, (float(z[0]), float(z[1]), float(z[2])))
            new_flags = set()
            if c.uncertain:
                new_flags.add(IdentityFlag.CROSS_VIEW_UNCERTAIN)
            if _competitor_flag(gated, c.cluster):
                new_flags.add(IdentityFlag.TWO_VIEW_COMPETITOR_JOINT)
            for other_id, xy in nearby.items():
                if np.linalg.norm(c.anchor - xy) <= cfg.duplicate_radius_m:
                    self._duplicates.add(frozenset((tr.world_id, other_id)))
            flags[tr.world_id] = new_flags
            nearby[tr.world_id] = z[:2]

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

        for wid, fs in flags.items():
            tr = self._tracks.get(wid)
            if tr is None:
                continue
            for f in fs:
                tr.flags[f] = t

        # 생명주기
        newly_lost: list[_Track] = []
        for tr in list(self._tracks.values()):
            gap = t - tr.t_last_valid
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
                newly_lost.append(tr)

        snapshots = [self._snapshot(tr, t, applied.get(tr.world_id)) for tr in self._tracks.values()]
        for tr in newly_lost:
            self._tracks.pop(tr.world_id, None)
        return WorldTrackFrame(
            t=t,
            calibration_version=frame.calibration_version,
            tracks=tuple(sorted(snapshots, key=lambda s: s.world_id)),
            revisions=(),
            input_provenance=self.input_provenance,
        )

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

    def _apply(
        self, tr: _Track, cluster: ViewCluster, frame: AssociationFrame, rig: CameraRig, excluded: frozenset[str], t: int
    ) -> _Applied:
        gated = tr.gate.update([v.view for v in cluster.views], rig, _others(frame, cluster), JOINT_LAYOUT, excluded)
        measurement = self._measurement(gated)
        if measurement is None:
            return _Applied(cluster, gated, None)
        z, sigma = measurement
        tr.kf.update(t, z, sigma)
        tr.t_last_valid = t
        tr.hits += 1
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


def _others(frame: AssociationFrame, cluster: ViewCluster) -> dict[str, list[Sequence[Any]]]:
    """같은 시각 다른 클러스터의 카메라별 keypoint (관절 유효성 게이트의 2대 관측 경쟁 후보 검사용)."""
    others: dict[str, list[Sequence[Any]]] = {}
    for c in frame.clusters:
        if c is cluster:
            continue
        for v in c.views:
            others.setdefault(v.camera_id, []).append(v.view.keypoints)
    return others
