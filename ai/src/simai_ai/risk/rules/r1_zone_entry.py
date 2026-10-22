"""R1 위험 구역 진입 (명세 7.2 R1, 8.2 구역 판정 유형, 7.1 불확실성 범위·확인 필요, 6.4, 15.1 G6-R1).

사람(OBSERVED·PREDICTED 트랙) × 판정 시각에 유효한 구역마다 판정한다. UNKNOWN 트랙은 위치를 쓰지 않으므로(6.1)
판정하지 않고, 구역 안에서 관측을 잃은 사람은 R6(ObservationLossHold)이 원래 이벤트를 유지한다. 배포 등록부에는
`ObservationLossHold(ZoneEntryRule())` 로 등록한다(registry.default_registry).

**수평(XY).** 명세 8.2 대로 position_xy + 수평 불확실성(position_xy_sigma)으로 판정한다. 불확실성 범위는 수평 공분산
k=2 타원 + 점유 반경 0.3 m 다(7.1, zone_geometry.HorizontalRange). 접지점(ground_anchor_xyz, 지지 표면 위 발 위치)이
있으면 그 XY(σ 는 ground_anchor_sigma 의 XY, 없으면 position_xy_sigma)도 같은 방식으로 보고, 둘 중 하나라도 걸리면
걸린 것으로 본다(보수적 결합: 접지점이 position_xy 기준 진입을 지우지 않는다). 간격은 둘 중 작은 쪽을 쓴다.
경계에서 BOUNDARY_TOLERANCE_M(1e-9 m) 이하는 닿은 것으로 본다(부동소수점 오차로 발생을 놓치지 않게).

**높이(Z, VOLUME_3D 만).** 사람이 차지하는 높이 범위 = 서 있는 면 Z(접지점 Z) ~ VALID 관측 관절 중 가장 높은 Z
(없으면 head_z)다(8.2). 추정(inferred, HMR) 관절은 쓰지 않는다(규칙 1). 3D 불확실성으로 아래 끝은
k·ground_anchor_sigma.z, head_z 를 쓴 위 끝은 k·head_z_sigma 만큼 넓힌다.
- 두 끝을 모두 알면 구역 높이 범위와의 겹침으로 판정한다(겹침 = 발생, 간격 0.3 m 미만 = 유지, 그 밖 = 판정 없음).
- 서 있는 면 Z 나 위 끝 중 하나라도 모르면 높이로 판정하지 않는다. XY 가 걸리면 발생하되 확인 필요(`Z_UNKNOWN`),
  XY 가 유지 띠면 유지 + `Z_UNKNOWN` 이다. 아는 한쪽 끝만으로 '진입 없음'을 확정하지 않고 Z=0 으로 채우지 않는다
  (8.2 'XY는 다각형 안인데 Z(서 있는 면 또는 head_z)를 모르면', 규칙 2).
GROUND_FOOTPRINT 는 높이를 보지 않는다(8.2 표).

**발생·해소(히스테리시스, 7.1·7.2).**
- 발생: 불확실성 범위가 구역에 걸리고(VOLUME_3D 는 높이도 겹치거나 모름) → holding=False.
- 유지: 걸리지는 않지만 범위가 구역에서 해소 간격(0.3 m) 안쪽이면 holding=True(활성 이벤트만 유지, 새 이벤트 없음).
  VOLUME_3D 높이도 같은 간격으로 본다.
- 해소: 위 둘 다 아니면 판정을 내지 않는다. 판정 없이 유효 관측이 3초 이어지면 lifecycle 이 해소한다
  (LifecyclePolicy.clear_hold_ms 기본 3000 ms = 명세 R1 '0.3m 바깥에서 3초 유지').
발생 지속 조건(3프레임 중 2)과 10초 안 재개는 lifecycle 이 한다(S15P31A101-156). 규칙은 프레임마다 판정만 낸다.

**심각도.** 구역 유형별 = 구역의 severity(8.2 구역 속성). 확인 필요여도 내리지 않는다(7.3).

**확인 필요 이유(7.1).** 판정마다 아래 이유를 Finding.confidence_reasons(common.ConfidenceReason)로 낸다. 확인 필요
(needs_check)는 이유 목록이 비어 있지 않은 상태라 Finding 이 이유에서 맞춘다(규칙은 needs_check 를 따로 정하지 않는다).
판정 근거 코드(reasons)에는 이유를 겹쳐 적지 않는다.
- Z_UNKNOWN: VOLUME_3D 에서 서 있는 면 Z 나 점유 높이 위 끝을 모름(위).
- PREDICTED: 위치가 관측이 아니라 예측값.
- SINGLE_VIEW: 이 상태에 기여한 카메라가 2대 미만.
- JOINT_UNCERTAIN: VOLUME_3D 위 끝을 VALID 관절 없이 head_z 로 정했는데 skeleton 에 VALID 관절이 없다(head_z 가
  UNCERTAIN 관절에서 왔다, geometry.joint_gate.HeadZ).
- IDENTITY_UNCERTAIN: 카메라 간 연결이 애매함.
- PROVISIONAL_CALIB: R1 은 내지 않는다(명세 4.4·7.1, 계약 confidence_reason 'R2·R3·R4 에만'). R1 은 아래 넓은 불확실성으로
  보정 상태를 판정에 넣는다.

**보정이 CALIBRATED 가 아닐 때(명세 4.4 '넓은 불확실성', 7.1).** RuleContext.calibration_state 가 PROVISIONAL·DRIFTED·
UNCALIBRATED(방어적으로) 이면 불확실성 범위를 보정 여유 R1_CALIBRATION_MARGIN_M(0.3 m)만큼 넓힌다: 수평은 점유 반경에 더하고
(범위 둘레를 0.3 m 바깥으로), VOLUME_3D 높이는 아래·위 끝을 0.3 m 씩 넓힌다. 발생·유지·해소 기준(겹침·해소 간격 0.3 m·3초)은
그대로라 발생은 0.3 m 더 일찍, 해소는 0.3 m 더 벗어나야 한다(안전 쪽). 0.3 m = CALIBRATED 기준의 위치 불일치·머리 높이 오차 한도
15 cm(4.4)를 보정 오차 σ 로 가정하고 7.1 과 같은 k=2 를 곱한 초기 안전 여유다(측정된 σ 가 아님, G1·G2 실측 뒤 재검토,
docs/design/risk-r1-calibration-margin.md). 넓힌 판정에는 근거 코드 calibration_<상태 소문자>(reasons 끝)와 수치 calibration_margin_m
을 붙인다. CALIBRATED 와 보정 상태 없음(None, PLANNED 계획 위치)은 넓히지 않고 결과가 전과 같다. 보정 여유·넓힘 상태는 규칙 값
(rule_version 시드의 calibration_margin·calibration_widened_states)이고, R1 v1 을 같은 버전에서 보완한 것은 첫 운영 배포 전 예외다
(명세 11.4, Lead 승인 2026-10-22).

판정 근거 코드(reasons 앞쪽): xy_inside_polygon(위치가 다각형 안), xy_uncertainty_overlap(위치는 밖이지만 범위가 걸림),
xy_within_clear_margin(유지 띠), VOLUME_3D 는 z_overlap / z_within_clear_margin 이 뒤따른다. 보정 여유로 넓혔으면 맨 끝에
calibration_provisional / calibration_drifted / calibration_uncalibrated.
수치(metrics): zone_distance_m(position_xy·접지점 중 가까운 쪽에서 다각형까지 수평 거리, 안이면 0),
VOLUME_3D 는 아는 끝만 occupancy_z_min_m·occupancy_z_max_m(불확실성 넓힘 전 값), 넓혔으면 calibration_margin_m.
"""

import math
from collections.abc import Iterator
from dataclasses import dataclass

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import (
    CALIBRATION_STATES,
    PROVISIONAL_CALIB_STATES,
    ConfidenceReason,
)
from simai_ai.risk.rule import Finding, RuleContext, RuleSpec
from simai_ai.risk.world import (
    IdentityConfidence,
    ObservationState,
    Validity,
    WorldTrack,
)
from simai_ai.risk.zone import Zone, ZoneJudgeType
from simai_ai.risk.zone_geometry import (
    BOUNDARY_TOLERANCE_M,
    OCCUPANCY_RADIUS_M,
    UNCERTAINTY_K,
    HorizontalRange,
    distance_outside_polygon,
)

R1_RULE_ID = "R1"
R1_RULE_VERSION = 1
R1_CLEAR_MARGIN_M = 0.3
"""해소 간격(명세 7.2 R1 '0.3m 바깥'). 불확실성 범위가 구역에서 이만큼 벗어나야 유지 판정도 내지 않는다"""
R1_CALIBRATION_MARGIN_M = 0.3
"""보정 여유 기본값(명세 4.4 '넓은 불확실성', 7.1). 보정이 CALIBRATED 가 아니면 불확실성 범위를 수평·높이 모두 이만큼 넓힌다.
측정된 σ 가 아니라 설계 가정의 초기 안전 여유다: CALIBRATED 기준 한도 15 cm(위치 불일치·머리 높이 오차의 최대값, 4.4)를
σ 로 가정하고 × k=2. G1·G2 실측 뒤 다시 정한다(docs/design/risk-r1-calibration-margin.md)"""
R1_WIDENED_STATES = PROVISIONAL_CALIB_STATES
"""보정 여유를 쓰는 보정 상태 = CALIBRATED 가 아닌 상태(R2·R3·R4 의 PROVISIONAL_CALIB 와 같은 집합, UNCALIBRATED 는 방어적으로)"""

# 판정 근거 코드
REASON_XY_INSIDE = "xy_inside_polygon"
REASON_XY_UNCERTAINTY_OVERLAP = "xy_uncertainty_overlap"
REASON_XY_CLEAR_MARGIN = "xy_within_clear_margin"
REASON_Z_OVERLAP = "z_overlap"
REASON_Z_CLEAR_MARGIN = "z_within_clear_margin"


_MIN_VIEWS = 2

_R1_READS = (
    "tracks.observation_state",
    "tracks.position_xy",
    "tracks.position_xy_sigma",
    "tracks.ground_anchor_xyz",
    "tracks.ground_anchor_sigma",
    "tracks.head_z",
    "tracks.head_z_sigma",
    "tracks.skeleton",
    "tracks.cameras",
    "tracks.identity_confidence",
    "zones",
)


@dataclass(frozen=True)
class OccupancyHeight:
    """사람이 차지하는 높이 범위(명세 8.2). 모르는 끝은 None.

    joint_uncertain: 위 끝을 UNCERTAIN 관절에서 온 head_z 로 정했는지.
    """

    bottom: float | None
    top: float | None
    bottom_sigma: float = 0.0
    top_sigma: float = 0.0
    joint_uncertain: bool = False


class ZoneEntryRule:
    """R1 위험 구역 진입. 상태를 갖지 않는다(히스테리시스는 holding 으로, 시간 확인은 lifecycle 이 한다).

    - k: 수평·수직 불확실성 배수(명세 7.1 k=2).
    - occupancy_radius_m: 사람 점유 반경(7.1 0.3 m).
    - clear_margin_m: 해소 간격(7.2 R1 0.3 m).
    - tolerance_m: 경계 접촉 허용오차. 간격이 이 값 이하면 닿은 것(걸림)으로 본다(안전 쪽).
    - calibration_margin_m: 보정이 넓힘 상태일 때 범위를 넓히는 거리(4.4 넓은 불확실성, 초기 안전 여유 0.3 m).
    - calibration_widened_states: 보정 여유를 쓰는 보정 상태. None 이면 R1_WIDENED_STATES(규칙을 만들 때 읽는다).
    두 값은 규칙 값이라 rule_catalog 가 rule_version 시드에 적는다(11.4-1).
    """

    def __init__(
        self,
        *,
        k: float = UNCERTAINTY_K,
        occupancy_radius_m: float = OCCUPANCY_RADIUS_M,
        clear_margin_m: float = R1_CLEAR_MARGIN_M,
        tolerance_m: float = BOUNDARY_TOLERANCE_M,
        calibration_margin_m: float = R1_CALIBRATION_MARGIN_M,
        calibration_widened_states: frozenset[str] | None = None,
    ) -> None:
        params = (
            ("k", k),
            ("occupancy_radius_m", occupancy_radius_m),
            ("clear_margin_m", clear_margin_m),
            ("tolerance_m", tolerance_m),
            ("calibration_margin_m", calibration_margin_m),
        )
        for name, value in params:
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise RiskInputError(f"R1.{name} 는 0 이상 유한한 수여야 한다")
        self._k = float(k)
        self._radius = float(occupancy_radius_m)
        self._margin = float(clear_margin_m)
        self._tol = float(tolerance_m)
        widened = R1_WIDENED_STATES if calibration_widened_states is None else frozenset(calibration_widened_states)
        if not widened <= CALIBRATION_STATES:
            raise RiskInputError(f"R1.calibration_widened_states: 모르는 보정 상태 {sorted(widened - CALIBRATION_STATES)}")
        self._calibration_margin = float(calibration_margin_m)
        self._calibration_widened_states = widened
        self._spec = RuleSpec(rule_id=R1_RULE_ID, rule_version=R1_RULE_VERSION, reads=_R1_READS)

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    def evaluate(self, ctx: RuleContext) -> Iterator[Finding]:
        state = ctx.calibration_state
        widen = state is not None and state in self._calibration_widened_states and self._calibration_margin > 0.0
        calibration_reason = f"calibration_{state.lower()}" if widen and state is not None else None
        margin = self._calibration_margin if calibration_reason is not None else 0.0
        for track in sorted(ctx.world.tracks, key=lambda t: t.world_id):
            if track.observation_state is ObservationState.UNKNOWN:
                continue  # 정확한 위치 사용 중단(6.1). 관측 상실은 R6 이 맡는다
            ranges = horizontal_ranges(track, k=self._k, radius_m=self._radius + margin)
            height = occupancy_height(track)
            for zone in ctx.zones:
                finding = self._judge(track, zone, ranges, height, margin, calibration_reason)
                if finding is not None:
                    yield finding

    def _judge(
        self,
        track: WorldTrack,
        zone: Zone,
        ranges: tuple[HorizontalRange, ...],
        height: OccupancyHeight,
        margin: float,
        calibration_reason: str | None,
    ) -> Finding | None:
        # position_xy(명세 8.2)와 접지점 XY 중 하나라도 걸리면 걸린 것이다(작은 간격).
        cutoff = max(self._margin, self._tol)
        gap = min(r.gap_to(zone.polygon_xy, exact_below=cutoff) for r in ranges)
        touching = gap <= self._tol
        if not touching and gap >= self._margin:
            return None
        center_distance = min(distance_outside_polygon(r.center, zone.polygon_xy) for r in ranges)
        if touching:
            reasons = [REASON_XY_INSIDE if center_distance == 0.0 else REASON_XY_UNCERTAINTY_OVERLAP]
        else:
            reasons = [REASON_XY_CLEAR_MARGIN]
        raising = touching
        metrics = {"zone_distance_m": center_distance}
        confidence: set[ConfidenceReason] = set()

        if zone.judge_type is ZoneJudgeType.VOLUME_3D and zone.z_range is not None:  # Zone 이 z_range 를 보장한다
            if height.bottom is None or height.top is None:
                # 서 있는 면이나 위 끝을 모르면 높이로 판정하지 않는다(8.2). XY 판정 그대로 + 확인 필요.
                confidence.add(ConfidenceReason.Z_UNKNOWN)
            else:
                z_gap = self._height_gap(height.bottom, height.top, height, zone.z_range, margin)
                if z_gap <= self._tol:
                    reasons.append(REASON_Z_OVERLAP)
                elif z_gap < self._margin:
                    raising = False
                    reasons.append(REASON_Z_CLEAR_MARGIN)
                else:
                    return None
            if height.joint_uncertain:
                confidence.add(ConfidenceReason.JOINT_UNCERTAIN)
            if height.bottom is not None:
                metrics["occupancy_z_min_m"] = height.bottom
            if height.top is not None:
                metrics["occupancy_z_max_m"] = height.top

        if calibration_reason is not None:
            reasons.append(calibration_reason)
            metrics["calibration_margin_m"] = margin
        confidence |= track_confidence_reasons(track)
        return Finding(
            rule_id=self._spec.rule_id,
            rule_version=self._spec.rule_version,
            severity=zone.severity,
            world_ids=(track.world_id,),
            zone_id=zone.zone_id,
            confidence_reasons=tuple(confidence),  # Finding 이 선언 순서로 정렬하고 needs_check 를 맞춘다
            reasons=tuple(reasons),
            metrics=metrics,
            holding=not raising,
        )

    def _height_gap(
        self, bottom: float, top: float, height: OccupancyHeight, z_range: tuple[float, ...], margin: float
    ) -> float:
        """점유 높이(불확실성·보정 여유로 넓힘)와 구역 높이 [zmin, zmax] 사이 간격. 겹치면(닿음 포함) 0."""
        lo = bottom - self._k * height.bottom_sigma - margin
        hi = top + self._k * height.top_sigma + margin
        return max(z_range[0] - hi, lo - z_range[1], 0.0)


def horizontal_ranges(
    track: WorldTrack, *, k: float = UNCERTAINTY_K, radius_m: float = OCCUPANCY_RADIUS_M
) -> tuple[HorizontalRange, ...]:
    """수평 판정에 쓰는 불확실성 범위들. 첫째는 늘 position_xy(명세 8.2), 접지점이 있으면 그 XY 범위를 더한다."""
    ranges = [HorizontalRange.of(track.position_xy, track.position_xy_sigma, k=k, radius_m=radius_m)]
    if track.ground_anchor_xyz is not None:
        sigma = track.ground_anchor_sigma[:2] if track.ground_anchor_sigma is not None else track.position_xy_sigma
        ranges.append(HorizontalRange.of(track.ground_anchor_xyz[:2], sigma, k=k, radius_m=radius_m))
    return tuple(ranges)


def occupancy_height(track: WorldTrack) -> OccupancyHeight:
    """명세 8.2: 서 있는 면 Z(접지점 Z) ~ VALID 관측 관절 중 가장 높은 Z, 없으면 head_z. 모르면 None(Z=0 대체 없음)."""
    bottom = None if track.ground_anchor_xyz is None else track.ground_anchor_xyz[2]
    bottom_sigma = 0.0 if track.ground_anchor_sigma is None else abs(track.ground_anchor_sigma[2])
    top = None
    has_valid_joint = False
    skeleton = track.skeleton
    if skeleton is not None:
        for i, validity in enumerate(skeleton.validity):
            joint = skeleton.observed_joint(i)
            if validity is Validity.VALID and joint is not None:
                has_valid_joint = True
                top = joint[2] if top is None else max(top, joint[2])
    if top is not None:
        return OccupancyHeight(bottom, top, bottom_sigma=bottom_sigma)
    if track.head_z is None:
        return OccupancyHeight(bottom, None, bottom_sigma=bottom_sigma)
    top_sigma = 0.0 if track.head_z_sigma is None else abs(track.head_z_sigma)
    return OccupancyHeight(
        bottom,
        track.head_z,
        bottom_sigma=bottom_sigma,
        top_sigma=top_sigma,
        # skeleton 이 있는데 VALID 관절이 없으면 head_z 는 UNCERTAIN 귀·눈에서 왔다(joint_gate.HeadZ).
        joint_uncertain=skeleton is not None and not has_valid_joint,
    )


def track_confidence_reasons(track: WorldTrack) -> set[ConfidenceReason]:
    """트랙 상태에서 나오는 확인 필요 이유(명세 7.1). 구역과 무관한 것만."""
    reasons: set[ConfidenceReason] = set()
    if track.observation_state is ObservationState.PREDICTED:
        reasons.add(ConfidenceReason.PREDICTED)
    if len(set(track.cameras)) < _MIN_VIEWS:
        reasons.add(ConfidenceReason.SINGLE_VIEW)
    if track.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN:
        reasons.add(ConfidenceReason.IDENTITY_UNCERTAIN)
    return reasons
