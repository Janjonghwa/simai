"""R1 위험 구역 진입 (명세 7.2, 8.2; 수정 52).

발생은 OBSERVED 이고 기여 카메라가 2대 이상이거나 접지점이 있는 위치만 판정한다.
접지점이 있으면 그 XY, 없으면 position_xy 점이 다각형 안이나 경계에 있어야 한다.
VOLUME_3D 는 점유 높이 범위가 구역과 겹쳐야 한다. 높이 양 끝 중 모르는 끝이 있어도 XY 가 안이면 발생하고
확인 필요(Z_UNKNOWN)를 붙인다. 모르는 끝은 열린 쪽으로 보고, 아는 끝만으로 구역과 겹칠 수 없다고 밝혀질 때만
판정하지 않는다(예: 위 끝이 구역 최저 높이보다 낮음). 모르는 상태를 안전으로 판단하지 않는다.
수평 불확실성 타원, 점유 반경, 보정 여유만으로 걸린 경우에는 발생하지 않는다.
높이의 불확실성과 보정 여유는 기존처럼 높이 겹침 판정에 적용한다.

유지는 기존과 같이 position_xy 또는 접지점의 불확실성 범위, 해소 간격 0.3 m,
보정 여유를 사용한다. 불확실한 프레임의 판정은 holding 으로 활성 이벤트만 유지한다.
UNKNOWN 관측 상실은 R6 이 담당하고, 발생 지속 3프레임 중 2 및 해소 3초는 lifecycle 이 담당한다.
심각도는 구역 severity 를 쓴다. rule_version 1 유지는 첫 운영 배포 전 보완 예외다
(명세 11.4, Lead 승인 2026-11-03).

근거 코드는 xy_inside_polygon, xy_uncertainty_overlap, xy_within_clear_margin 및
VOLUME_3D 의 z_overlap(높이 양 끝을 모두 알 때), z_within_clear_margin 이다. 보정 여유를 사용하면
calibration_<상태>와 calibration_margin_m 을 기록한다. 발생에는 PREDICTED 가 붙지 않고
Z_UNKNOWN 은 붙을 수 있으며 유지에도 붙을 수 있다. SINGLE_VIEW, JOINT_UNCERTAIN,
IDENTITY_UNCERTAIN 은 해당 조건에 따라 기록한다. PROVISIONAL_CALIB 는 R1 에 쓰지 않는다.
zone_distance_m 은 position_xy 와 접지점 중 다각형에 가까운 거리이며, 점유 높이 수치는 넓히기 전 값이다.
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
        point = track.ground_anchor_xyz[:2] if track.ground_anchor_xyz is not None else track.position_xy
        raising = (
            track.observation_state is ObservationState.OBSERVED
            and (len(set(track.cameras)) >= _MIN_VIEWS or track.ground_anchor_xyz is not None)
            and distance_outside_polygon(point, zone.polygon_xy) <= self._tol
        )
        if raising:
            reasons[0] = REASON_XY_INSIDE  # 점이 경계 허용오차 안에 있으면 안쪽으로 기록한다
        metrics = {"zone_distance_m": center_distance}
        confidence: set[ConfidenceReason] = set()

        if zone.judge_type is ZoneJudgeType.VOLUME_3D and zone.z_range is not None:  # Zone 이 z_range 를 보장한다
            # 모르는 끝은 열어 두고 아는 끝만으로 겹침을 가린다. 겹칠 수 없다고 밝혀지면 판정하지 않는다.
            z_gap = self._height_gap(height, zone.z_range, margin)
            if z_gap <= self._tol:
                if height.bottom is not None and height.top is not None:
                    reasons.append(REASON_Z_OVERLAP)
            elif z_gap < self._margin:
                raising = False
                reasons.append(REASON_Z_CLEAR_MARGIN)
            else:
                return None
            if height.bottom is None or height.top is None:
                confidence.add(ConfidenceReason.Z_UNKNOWN)  # 높이를 몰라도 XY 가 안이면 발생한다(확인 필요)
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

    def _height_gap(self, height: OccupancyHeight, z_range: tuple[float, ...], margin: float) -> float:
        """점유 높이(불확실성·보정 여유로 넓힘)와 구역 높이 [zmin, zmax] 사이 간격. 겹치면(닿음 포함) 0.
        모르는 끝(None)은 끝이 없는 것으로 본다(아래 끝은 -inf, 위 끝은 +inf)."""
        lo = -math.inf if height.bottom is None else height.bottom - self._k * height.bottom_sigma - margin
        hi = math.inf if height.top is None else height.top + self._k * height.top_sigma + margin
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
