"""ContactEstimator: 사람이 서 있는 지지 표면과 접지점 추정 (명세 5.2 접지 추정·World 위치의 정의·계산 순서 4단계, 7.5).

발목 위치를 접지점으로 쓰지 않는다. 23점 모델 채택 전(COCO17)이라 발뒤꿈치·발끝이 없으므로 발목 근거만 쓴다.
근거는 관절 유효성 게이트가 VALID 로 낸 발목(2대 이상 다시점 삼각측량, `geometry.joint_gate`)의 높이와 정지 여부,
지지 표면 후보(바닥 Z=0 과 활성 구성 묶음의 등록 작업면 상판 높이, `contact.surfaces`)다. UNCERTAIN·UNAVAILABLE 발목,
HMR·정적 3D·VLM 결과는 쓰지 않는다(AGENTS 규칙 1). 접지점 Z 는 발목 Z 에서 빼서 만들지 않고 등록된 상판 높이(바닥은 0)를
쓴다(R2·R3 가 그 Z 를 서 있는 면 높이로 쓴다, 5.2).

**시각마다 한 사람(World Track)의 판단.**

0. 마지막 발목 근거 뒤 `hold_max_ms` 가 지났으면 그 표면 이력을 먼저 버린다(HOLD_EXPIRED). 입력 공백 뒤 미확인 발목
   하나로 만료된 표면을 되살리지 않는다.
1. 발목 근거: 이번 시각 골격의 VALID 발목 중 높이 σ 가 `max_ankle_sigma_z_m` 이하인 것. 가장 낮은 발목을 디딤발
   후보로 본다(걸을 때 든 발은 더 높다).
2. 높이 대조: 디딤발 XY 에 있을 수 있는 후보(바닥, footprint 안의 작업면) 중 발목 Z 가
   `상판 + ankle_height_m ± (ankle_band_m + sigma_k·σz_발목)` 안인 것. 둘 이상이면 애매(AMBIGUOUS_SURFACE), 없으면
   NO_MATCH. 표면 높이 σ 는 띠를 넓히지 않고 접지점 σz 로만 간다.
   **바닥 확정의 분해능.** 바닥 띠 위 끝은 0.08 + 0.04 + 2·σz(최대 0.02) = 0.16 m 이하다. 미등록 상판이 약 8 cm
   이하이면 발목 높이 개인차·삼각측량 오차 안이라 바닥과 구별되지 않는다(바닥 접지점 σz 기본값 0.03 m 는 그 대부분을
   2σ 로 덮는 초기값이고 G2 에서 조정한다, `contact.surfaces`). 10 cm 이상 미등록 상판 위 발목은 바닥 띠 밖이라 UNKNOWN 이다.
2a. 골반 공간 일치: VALID 골반이 있으면 접촉 발목과의 수평 거리가 `max_pelvis_ankle_xy_m` 이하이고, 작업면이면 골반
   XY 가 footprint(+`hold_pelvis_margin_m`) 안이어야 한다. 아니면 접지점을 비우고 UNCERTAIN(PELVIS_ANKLE_INCONSISTENT)
   이다. 골반 높이만 내려간 것(웅크림·앉기)은 모순으로 보지 않는다.
3. 확인: 디딤발이 정지(직전 VALID 위치 대비 속도 ≤ `stationary_speed_mps`)했거나, 두 발목이 모두 같은 표면 높이 띠
   안이면 "접촉 확인"이다. 높이만 맞으면 확인 전이다.
4. 새 지지 표면(지금 유지 중인 것과 다른 표면, 처음 잡는 표면 포함)은 접촉 확인이 `confirm_frames` 번 이어져야
   VALID 가 된다. 그 전에는 UNCERTAIN(접지점 없음)이다. 발목 하나의 오연결로 단상 위 사람을 바닥으로 바꾸지 않게 한다.
5. 유지 중인 표면과 높이가 맞으면(확인 전이어도) 그 표면을 이번 시각 관측(observed)으로 갱신한다.

**가림 동안 유지(고소 위치였던 사람이 가려지면, 5.2).** 발목 근거가 없으면(골격 없음, VALID 발목 없음, 보인 발목이
하나뿐이고 표면보다 높음) 마지막 유효 지지 표면과 접지점을 유지한다(surface_source inferred). z_validity·contact_validity
는 UNCERTAIN, 접지점 σz 는 `hold_sigma_z_growth_mps × 경과`만큼, σxy 는 골반이 없으면 `max_speed_mps × 경과`만큼
커진다(골반이 있으면 골반 XY 와 그 σ). 바닥으로 바꾸지 않는다. 아래 경우에는 유지를 끊고 UNKNOWN(접지점 비움)으로 둔다.

- 마지막 발목 근거 뒤 `hold_max_ms` 가 지남(무기한 유지 금지, HOLD_EXPIRED, 0단계). 경계 시각은 유지한다(≤).
- 발목이 유지 중인 표면과 맞지 않음: 가장 낮은 VALID 발목이 표면 띠보다 낮거나, footprint 밖이거나, VALID 발목이 둘
  이상인데 모두 띠보다 높거나, 하나뿐인 VALID 발목이 다른 후보 표면 높이에 있음(HOLD_CONTRADICTED,
  LEFT_SURFACE_FOOTPRINT). 하나뿐인 발목이 어느 후보 높이에도 없이 높으면 든 발로 보고 유지한다(RAISED_SINGLE_ANKLE).
- 골반 XY 가 작업면 footprint(+`hold_pelvis_margin_m`) 밖(LEFT_SURFACE_FOOTPRINT): 발이 가려진 채 단상에서 내려왔을 수 있다.
- VALID 골반 높이 − 표면 높이가 접촉 확인 때 잰 값에서 `pelvis_height_tolerance_m` 넘게 바뀜(PELVIS_HEIGHT_CHANGED).
- 활성 구성에서 그 작업면이 빠졌거나 높이가 바뀜(SURFACE_UNREGISTERED), ID 교정으로 identity_revision 이 바뀜
  (IDENTITY_REVISED: 이력이 다른 사람 것일 수 있다).

마지막으로 발목으로 확인한 접지점의 수평 위치·σ·시각은 Track 이 살아 있는 동안 기억해 `SupportContact.observed_*` 로
낸다. world_state 조립이 골반보다 최근 관측이면 position_xy 로 쓴다(명세 5.2 "골반이 없으면 검증된 접지점").

지지 표면을 모르면 접지점을 비우고 z_validity 를 UNAVAILABLE 로 둔다. Z=0 으로 채우지 않는다(AGENTS 규칙 2). 2대 미만에서
보인 사람은 World Track Manager 가 골격을 내지 않으므로 발목 근거가 없고, 유지 규칙만 적용된다(D-G2-03).

임계값은 모두 초기값이다(명세에 수치 없음, G2 에서 조정). 현재 시각을 읽지 않고 frame.t 만 쓴다. 시각은 UTC int64 ms,
길이는 미터다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from simai_ai.contact.surfaces import SupportSurface, SurfaceSet
from simai_ai.geometry.joint_gate import GatedSkeleton
from simai_ai.geometry.triangulation import JointValidity
from simai_ai.tracking.time_model import require_ms
from simai_ai.tracking.world_state import SupportContact
from simai_ai.tracking.world_track import TrackLifecycle, WorldTrackFrame, WorldTrackSnapshot

COCO17_L_ANKLE, COCO17_R_ANKLE = 15, 16
JOINT_LAYOUT = "COCO17"


class SurfaceSource(StrEnum):
    """지지 표면 출처 (AGENTS 규칙 7). 표면이 바닥인지 등록 작업면인지는 support_surface_id 가 말한다.

    I1 world_state 1.3.0 트랙의 surface_source 로 나간다(접지점이 있을 때만, S15P31A101-170).
    """

    OBSERVED = "observed"
    """이번 시각 VALID 발목이 그 표면 높이에 있다"""
    INFERRED = "inferred"
    """발목 근거가 없어 마지막 유효 지지 표면을 유지한다"""


class ContactReason(StrEnum):
    """접지 판단 근거 코드 (VALID 관측이 아닌 이유와 유지·해제 사유)."""

    TRACK_NOT_UPDATED = "TRACK_NOT_UPDATED"
    """이번 시각 골격이 없다 (2대 이상 관측 클러스터가 붙지 않음)"""
    NO_VALID_ANKLE = "NO_VALID_ANKLE"
    """VALID 발목이 없다 (가림, 1대 관측, UNCERTAIN)"""
    RAISED_SINGLE_ANKLE = "RAISED_SINGLE_ANKLE"
    """VALID 발목이 하나뿐이고 유지 중인 표면보다 높다 (든 발일 수 있어 근거로 쓰지 않는다)"""
    NO_MATCHING_SURFACE = "NO_MATCHING_SURFACE"
    """디딤발 높이에 맞는 후보가 없다 (미등록 작업면, 높이 사이)"""
    AMBIGUOUS_SURFACE = "AMBIGUOUS_SURFACE"
    """디딤발 높이에 맞는 후보가 둘 이상이다"""
    NOT_CONFIRMED = "NOT_CONFIRMED"
    """높이는 맞지만 정지·두 발 근거가 없다"""
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    """새 지지 표면의 접촉 확인이 confirm_frames 번 이어지지 않았다"""
    HOLD = "HOLD"
    """마지막 유효 지지 표면을 유지한다"""
    HOLD_EXPIRED = "HOLD_EXPIRED"
    HOLD_CONTRADICTED = "HOLD_CONTRADICTED"
    LEFT_SURFACE_FOOTPRINT = "LEFT_SURFACE_FOOTPRINT"
    PELVIS_HEIGHT_CHANGED = "PELVIS_HEIGHT_CHANGED"
    PELVIS_ANKLE_INCONSISTENT = "PELVIS_ANKLE_INCONSISTENT"
    """VALID 골반과 접촉 발목이 수평으로 맞지 않는다 (골반이 발목에서 너무 멀거나 작업면 밖)"""
    SURFACE_UNREGISTERED = "SURFACE_UNREGISTERED"
    IDENTITY_REVISED = "IDENTITY_REVISED"


@dataclass(frozen=True)
class ContactConfig:
    """ContactEstimator 설정. 명세에 수치가 없어 모두 초기값이며 G2 실측으로 다시 정한다."""

    ankle_height_m: float = 0.08
    """서 있을 때 발목 keypoint 의 지지 표면 위 높이 (성인 복사뼈 7~9 cm, 신발 포함)"""
    ankle_band_m: float = 0.04
    """발목 높이 허용 폭(±). 띠 = 상판 + ankle_height_m ± (ankle_band_m + sigma_k·σz_발목). 바닥 띠 위 끝이 10 cm 상판
    위 발목(0.18 m)에 닿지 않게 좁게 둔다"""
    sigma_k: float = 2.0
    max_ankle_sigma_z_m: float = 0.02
    """높이 근거로 쓰는 발목의 삼각측량 σz 상한. 넘으면 띠가 넓어져 낮은 상판과 바닥을 가르지 못하므로 쓰지 않는다"""
    max_pelvis_ankle_xy_m: float = 0.7
    """VALID 골반과 접촉 발목 평균의 수평 거리 상한 (선 사람의 다리 길이 안, 보폭 포함)"""
    footprint_margin_m: float = 0.10
    """발목 XY 가 작업면 footprint 를 이만큼 벗어나도 후보로 본다 (가장자리에 선 발, 삼각측량 오차)"""
    stationary_speed_mps: float = 0.5
    """디딤발 정지 판정 속도 상한 (VALID 발목 σ 수 cm 의 10 Hz 차분 잡음보다 넉넉히)"""
    speed_max_gap_ms: int = 300
    """직전 VALID 발목이 이보다 오래됐으면 속도를 모른다"""
    confirm_frames: int = 2
    """새 지지 표면을 VALID 로 올리는 데 필요한 연속 접촉 확인 횟수"""
    confirm_window_ms: int = 300
    """접촉 확인 사이 간격이 이보다 길면 연속으로 보지 않는다"""
    hold_max_ms: int = 3000
    """마지막 발목 근거 뒤 지지 표면 유지 상한 (World Track 소실 시간 lost_after_ms 와 같은 초기값)"""
    hold_sigma_z_growth_mps: float = 0.1
    """유지 중 접지점 σz 증가 속도"""
    max_speed_mps: float = 2.0
    """유지 중 골반이 없을 때 접지점 σxy 증가 속도 (명세 6.1 속도 상한 초기값)"""
    hold_pelvis_margin_m: float = 0.30
    """유지 중 골반 XY 가 작업면 footprint 를 이만큼 넘게 벗어나면 유지를 끊는다"""
    pelvis_height_tolerance_m: float = 0.25
    """유지 중 VALID 골반 높이(표면 기준)가 접촉 확인 때 값에서 이만큼 넘게 바뀌면 유지를 끊는다"""

    def __post_init__(self) -> None:
        for name in (
            "ankle_height_m",
            "ankle_band_m",
            "sigma_k",
            "footprint_margin_m",
            "stationary_speed_mps",
            "hold_sigma_z_growth_mps",
            "max_speed_mps",
            "hold_pelvis_margin_m",
            "pelvis_height_tolerance_m",
            "max_ankle_sigma_z_m",
            "max_pelvis_ankle_xy_m",
        ):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0):
                raise ValueError(f"{name} 는 0 이상의 유한한 수여야 한다: {v!r}")
        for name in ("speed_max_gap_ms", "confirm_window_ms", "hold_max_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 는 0 이상이어야 한다")
        if self.confirm_frames < 1:
            raise ValueError("confirm_frames 는 1 이상이어야 한다")


@dataclass(frozen=True)
class ContactEstimate:
    """한 사람의 공통 시각 t 접지 판단. contact 는 world_state 조립(`world_state_payload` contacts)에 넘기는 값이다."""

    world_id: str
    t: int
    contact: SupportContact
    reasons: tuple[ContactReason, ...] = ()
    geometry_version: str | None = None
    candidate_surface_id: str | None = None
    """UNCERTAIN 일 때 확인 중인 표면 (contact.support_surface_id 와 같다)"""

    @property
    def surface_source(self) -> SurfaceSource | None:
        return SurfaceSource(self.contact.surface_source) if self.contact.surface_source is not None else None


@dataclass(frozen=True)
class ContactFrame:
    """공통 시각 t 의 ContactEstimator 결과. estimates 는 World ID → 판단."""

    t: int
    geometry_version: str | None
    estimates: Mapping[str, ContactEstimate]

    def contacts(self) -> dict[str, SupportContact]:
        """`world_state_payload(..., contacts=...)` 에 넘길 값."""
        return {wid: e.contact for wid, e in self.estimates.items()}


@dataclass(frozen=True)
class _Ankle:
    xyz: tuple[float, float, float]
    sigma: tuple[float, float, float]
    speed: float | None


@dataclass
class _Held:
    surface: SupportSurface
    anchor_xy: tuple[float, float]
    anchor_sigma_xy: tuple[float, float]
    t: int
    """마지막 발목 근거 시각"""
    stand_height: float | None
    """접촉 확인 때 VALID 골반 Z − 표면 Z"""


@dataclass
class _State:
    identity_revision: int
    ankles: dict[int, tuple[int, tuple[float, float, float]]] = field(default_factory=dict)
    held: _Held | None = None
    pending: tuple[str, int, int] | None = None
    """(표면 ID, 연속 확인 횟수, 마지막 확인 시각)"""
    last_obs: tuple[int, tuple[float, float], tuple[float, float]] | None = None
    """마지막으로 발목으로 확인한 접지점 (시각, XY, σxy)"""


class ContactEstimator:
    """World Track Manager 결과마다 사람별 지지 표면·접지점을 추정한다. 시각은 늘어나는 순서로만 넣는다."""

    def __init__(self, config: ContactConfig | None = None) -> None:
        self.config = config if config is not None else ContactConfig()
        self._states: dict[str, _State] = {}
        self._last_t: int | None = None

    def update(self, frame: WorldTrackFrame, surfaces: SurfaceSet) -> ContactFrame:
        """surfaces 는 이 판정 시각의 활성 구성 묶음에서 만든 후보(`SurfaceSet.from_active_config`)."""
        t = require_ms("frame.t", frame.t)
        if self._last_t is not None and t <= self._last_t:
            raise ValueError(f"시각은 늘어나는 순서로 넣는다: 직전 {self._last_t}, 받은 값 {t}")
        self._last_t = t
        present = {tr.world_id for tr in frame.tracks}
        for wid in list(self._states):
            if wid not in present:
                del self._states[wid]
        estimates: dict[str, ContactEstimate] = {}
        for tr in frame.tracks:
            if tr.t != t:
                raise ValueError(f"{tr.world_id}: Track 시각 {tr.t} 가 frame 시각 {t} 와 다르다")
            estimates[tr.world_id] = self._estimate(tr, t, surfaces)
            if tr.lifecycle is TrackLifecycle.LOST:
                self._states.pop(tr.world_id, None)
        return ContactFrame(t, surfaces.geometry_version, estimates)

    # ------------------------------------------------------------------ 내부

    def _estimate(self, tr: WorldTrackSnapshot, t: int, surfaces: SurfaceSet) -> ContactEstimate:
        cfg = self.config
        reasons: list[ContactReason] = []
        state = self._states.get(tr.world_id)
        if state is not None and state.identity_revision != tr.identity_revision:
            state = None
            reasons.append(ContactReason.IDENTITY_REVISED)
        if state is None:
            state = _State(tr.identity_revision)
            self._states[tr.world_id] = state

        held = state.held
        if held is not None:
            current = surfaces.get(held.surface.surface_id)
            if current is None or current != held.surface:
                held = state.held = None
                reasons.append(ContactReason.SURFACE_UNREGISTERED)
        if held is not None and t - held.t > cfg.hold_max_ms:
            # 0단계: 만료를 재사용 분기보다 먼저 본다 (중간 tick 유무와 무관하게 frame.t 와 마지막 근거 시각으로 정한다)
            held = state.held = None
            reasons.append(ContactReason.HOLD_EXPIRED)

        skeleton = tr.skeleton if tr.skeleton is not None and tr.skeleton.t == t else None
        ankles = self._ankles(state, skeleton, t)
        if skeleton is None:
            reasons.append(ContactReason.TRACK_NOT_UPDATED)
        elif not ankles:
            reasons.append(ContactReason.NO_VALID_ANKLE)
        pelvis_valid = (
            skeleton is not None and tr.pelvis_xyz is not None and skeleton.pelvis_validity() is JointValidity.VALID
        )

        def result(contact: SupportContact, candidate: str | None = None) -> ContactEstimate:
            if state.last_obs is not None:
                obs_t, obs_xy, obs_sigma = state.last_obs
                contact = replace(contact, observed_xy=obs_xy, observed_xy_sigma=obs_sigma, observed_at=obs_t)
            return ContactEstimate(
                tr.world_id, t, contact, tuple(dict.fromkeys(reasons)), surfaces.geometry_version, candidate
            )

        match: SupportSurface | None = None
        confirmed = False
        contact_ankles: list[_Ankle] = []
        stance: _Ankle | None = None
        if ankles:
            stance = min(ankles, key=lambda a: a.xyz[2])
            matches = [s for s in surfaces.candidates(stance.xyz[:2], cfg.footprint_margin_m) if self._in_band(stance, s)]
            if len(matches) > 1:
                reasons.append(ContactReason.AMBIGUOUS_SURFACE)
            elif len(matches) == 1:
                match = matches[0]
                contact_ankles = [
                    a for a in ankles if match.contains_xy(a.xyz[:2], cfg.footprint_margin_m) and self._in_band(a, match)
                ]
                stationary = stance.speed is not None and stance.speed <= cfg.stationary_speed_mps
                confirmed = stationary or len(contact_ankles) >= 2
                if not stationary:
                    contact_ankles = contact_ankles if confirmed else [stance]
            else:
                reasons.append(ContactReason.NO_MATCHING_SURFACE)

        # 2a: VALID 골반과 접촉 발목의 수평 모순 (발목 오연결, 무릎 가림 중 상체만 다른 곳 등): 확정하지 않는다
        if match is not None and pelvis_valid and self._pelvis_inconsistent(tr, match, contact_ankles):
            state.held = None
            state.pending = None
            reasons.append(ContactReason.PELVIS_ANKLE_INCONSISTENT)
            return result(_uncertain(match.surface_id), match.surface_id)

        # 유지 중인 표면과 맞는 관측: 이번 시각 관측으로 갱신 (확인 전이어도 높이 근거가 있다)
        if held is not None and match is not None and match.surface_id == held.surface.surface_id:
            state.pending = None
            anchor = self._observe(state, match, contact_ankles, tr, pelvis_valid, t, refresh_height=confirmed)
            if not confirmed:
                reasons.append(ContactReason.NOT_CONFIRMED)
            return result(
                self._valid(anchor, match, "VALID" if confirmed else "UNCERTAIN", "VALID", SurfaceSource.OBSERVED, t)
            )

        # 유지 중인 표면과 맞지 않는 발목 근거: 모순이면 유지를 끊는다. 든 발 하나뿐이면 근거로 쓰지 않는다
        if held is not None and stance is not None:
            s = held.surface
            low, high = self._band(stance, s)
            if not s.contains_xy(stance.xyz[:2], cfg.footprint_margin_m):
                held = state.held = None
                reasons.append(ContactReason.LEFT_SURFACE_FOOTPRINT)
            elif stance.xyz[2] < low or (len(ankles) >= 2 and stance.xyz[2] > high):
                held = state.held = None
                reasons.append(ContactReason.HOLD_CONTRADICTED)
            elif match is not None:
                # 보인 발목 하나가 다른 표면 높이에 있다: 든 발인지 그 표면에 올라섰는지 모르므로 유지하지 않는다
                held = state.held = None
                reasons.append(ContactReason.HOLD_CONTRADICTED)
            elif stance.xyz[2] > high:
                reasons.append(ContactReason.RAISED_SINGLE_ANKLE)  # 든 발 하나: 근거로 쓰지 않고 유지한다
            # 그 밖(유지 중인 표면 띠 안인데 다른 후보와 애매): 유지한다

        # 새 표면의 접촉 확인
        if match is not None and confirmed:
            pend = state.pending
            if pend is not None and pend[0] == match.surface_id and t - pend[2] <= cfg.confirm_window_ms:
                count = pend[1] + 1
            else:
                count = 1
            if count >= cfg.confirm_frames:
                state.pending = None
                anchor = self._observe(state, match, contact_ankles, tr, pelvis_valid, t, refresh_height=True)
                return result(self._valid(anchor, match, "VALID", "VALID", SurfaceSource.OBSERVED, t))
            state.pending = (match.surface_id, count, t)
            reasons.append(ContactReason.PENDING_CONFIRMATION)
            return result(_uncertain(match.surface_id), match.surface_id)
        if match is not None:
            reasons.append(ContactReason.NOT_CONFIRMED)
            if held is None:
                return result(_uncertain(match.surface_id), match.surface_id)
        if stance is not None and held is None and ContactReason.AMBIGUOUS_SURFACE in reasons:
            return result(_uncertain(None))

        # 발목 근거 없음: 마지막 유효 지지 표면 유지
        if held is None:
            return result(SupportContact())
        age = t - held.t
        s = held.surface
        if tr.pelvis_xyz is not None and skeleton is not None:
            if not s.contains_xy(tr.pelvis_xyz[:2], cfg.hold_pelvis_margin_m):
                state.held = None
                reasons.append(ContactReason.LEFT_SURFACE_FOOTPRINT)
                return result(SupportContact())
            if (
                pelvis_valid
                and held.stand_height is not None
                and abs(tr.pelvis_xyz[2] - s.top_z - held.stand_height) > cfg.pelvis_height_tolerance_m
            ):
                state.held = None
                reasons.append(ContactReason.PELVIS_HEIGHT_CHANGED)
                return result(SupportContact())
            xy = (tr.pelvis_xyz[0], tr.pelvis_xyz[1])
            sigma_xy = (tr.position_xy_sigma[0], tr.position_xy_sigma[1])
        else:
            grow = cfg.max_speed_mps * age / 1000.0
            xy = held.anchor_xy
            sigma_xy = (held.anchor_sigma_xy[0] + grow, held.anchor_sigma_xy[1] + grow)
        reasons.append(ContactReason.HOLD)
        sigma_z = s.sigma_z + cfg.hold_sigma_z_growth_mps * age / 1000.0
        return result(
            SupportContact(
                ground_anchor_xyz=(xy[0], xy[1], s.top_z),
                ground_anchor_sigma=(sigma_xy[0], sigma_xy[1], sigma_z),
                support_surface_id=s.surface_id,
                support_surface_validity="VALID",
                contact_validity="UNCERTAIN",
                z_validity="UNCERTAIN",
                surface_source=str(SurfaceSource.INFERRED),
            )
        )

    def _ankles(self, state: _State, skeleton: GatedSkeleton | None, t: int) -> list[_Ankle]:
        """이번 시각 VALID 발목과 직전 VALID 위치 대비 속도. 직전 위치 기록을 갱신한다."""
        cfg = self.config
        for j, (jt, _) in list(state.ankles.items()):
            if t - jt > cfg.speed_max_gap_ms:
                del state.ankles[j]
        if skeleton is None:
            return []
        if skeleton.joint_layout != JOINT_LAYOUT:
            raise ValueError(f"ContactEstimator 는 {JOINT_LAYOUT} 만 받는다: {skeleton.joint_layout}")
        out: list[_Ankle] = []
        for j in (COCO17_L_ANKLE, COCO17_R_ANKLE):
            joint = skeleton.joints[j]
            if (
                joint.validity is not JointValidity.VALID
                or joint.xyz is None
                or joint.sigma_xyz_m is None
                or not joint.sigma_xyz_m[2] <= cfg.max_ankle_sigma_z_m
            ):
                state.ankles.pop(j, None)
                continue
            xyz = tuple(float(v) for v in joint.xyz)
            sigma = tuple(float(v) for v in joint.sigma_xyz_m)
            if not (all(math.isfinite(v) for v in xyz) and all(math.isfinite(v) and v >= 0 for v in sigma)):
                state.ankles.pop(j, None)
                continue
            speed = None
            prev = state.ankles.get(j)
            if prev is not None and t > prev[0]:
                speed = math.dist(xyz, prev[1]) / ((t - prev[0]) / 1000.0)
            state.ankles[j] = (t, xyz)  # type: ignore[assignment]
            out.append(_Ankle(xyz, sigma, speed))  # type: ignore[arg-type]
        return out

    def _band(self, ankle: _Ankle, surface: SupportSurface) -> tuple[float, float]:
        cfg = self.config
        center = surface.top_z + cfg.ankle_height_m
        half = cfg.ankle_band_m + cfg.sigma_k * ankle.sigma[2]
        return center - half, center + half

    def _pelvis_inconsistent(self, tr: WorldTrackSnapshot, surface: SupportSurface, ankles: list[_Ankle]) -> bool:
        cfg = self.config
        assert tr.pelvis_xyz is not None and ankles
        px, py = tr.pelvis_xyz[0], tr.pelvis_xyz[1]
        ax = sum(a.xyz[0] for a in ankles) / len(ankles)
        ay = sum(a.xyz[1] for a in ankles) / len(ankles)
        if math.hypot(px - ax, py - ay) > cfg.max_pelvis_ankle_xy_m:
            return True
        return not surface.contains_xy((px, py), cfg.hold_pelvis_margin_m)

    def _in_band(self, ankle: _Ankle, surface: SupportSurface) -> bool:
        low, high = self._band(ankle, surface)
        return low <= ankle.xyz[2] <= high

    def _observe(
        self,
        state: _State,
        surface: SupportSurface,
        ankles: list[_Ankle],
        tr: WorldTrackSnapshot,
        pelvis_valid: bool,
        t: int,
        *,
        refresh_height: bool,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """발목 근거로 유지 상태를 갱신하고 접지점 XY 와 σxy 를 돌려준다."""
        n = len(ankles)
        xy = (sum(a.xyz[0] for a in ankles) / n, sum(a.xyz[1] for a in ankles) / n)
        sigma_xy = (
            math.sqrt(sum(a.sigma[0] ** 2 for a in ankles)) / n,
            math.sqrt(sum(a.sigma[1] ** 2 for a in ankles)) / n,
        )
        prev = state.held
        stand = prev.stand_height if prev is not None and prev.surface == surface else None
        if refresh_height and pelvis_valid and tr.pelvis_xyz is not None:
            stand = tr.pelvis_xyz[2] - surface.top_z
        state.held = _Held(surface, xy, sigma_xy, t, stand)
        state.last_obs = (t, xy, sigma_xy)
        return xy, sigma_xy

    @staticmethod
    def _valid(
        anchor: tuple[tuple[float, float], tuple[float, float]],
        surface: SupportSurface,
        contact_validity: str,
        z_validity: str,
        source: SurfaceSource,
        t: int,
    ) -> SupportContact:
        (x, y), (sx, sy) = anchor
        return SupportContact(
            ground_anchor_xyz=(x, y, surface.top_z),
            ground_anchor_sigma=(sx, sy, surface.sigma_z),
            support_surface_id=surface.surface_id,
            support_surface_validity="VALID",
            contact_validity=contact_validity,
            z_validity=z_validity,
            surface_source=str(source),
            observed_xy=(x, y),
            observed_xy_sigma=(sx, sy),
            observed_at=t,
        )


def _uncertain(surface_id: str | None) -> SupportContact:
    """높이는 맞지만 확인 전이거나 후보가 애매하다: 접지점은 비운다."""
    return SupportContact(
        support_surface_id=surface_id,
        support_surface_validity="UNCERTAIN",
        contact_validity="UNCERTAIN",
        z_validity="UNAVAILABLE",
    )
