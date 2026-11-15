"""R2 개구부·가장자리 근접 (명세 7.2 R2, 7.5 작업면 가장자리, 5.2 높이 규칙·접지, 7.1 불확실성 범위·확인 필요, 7.4, 15.1 G6).

명세: 등록 작업면 OBB 윗면 둘레(7.5) 중 인접 면과 높이차 2 m 이상인 가장자리까지 1.0 m 이내 / 1.3 m 이상에서 3초. CRITICAL.
높이 기준은 프로파일로 정한다(height_common: 운영 2.0 m = R2 v1, 교실 시험 0.35 m = R2 v1001). 거리(1.0·1.3 m)는 두 프로파일이
같다(7.4: 시험 때는 높이 임계값만 바꾸고 규칙 논리는 같다). 개구부(바닥 구멍)는 등록 형식이 없어 다루지 않는다.

사람(OBSERVED·PREDICTED 트랙)마다 판정한다. UNKNOWN 트랙은 판정하지 않는다(6.1, 활성 이벤트는 lifecycle 이 UNVERIFIABLE 로 유지).

**어느 가장자리인가.** 가장자리에서 떨어질 수 있는 사람은 그 작업면 위(같은 층)에 선 사람이다. 바닥에 선 사람은 단상 가장자리
옆에 있어도 R2 가 아니다. 서 있는 면은 height_common.standing 으로 정한다.
- KNOWN·HELD: 서 있는 층(접지점 Z 와 같은 높이이고 지지 표면 ID 가 같거나 수평 범위가 걸치는 작업면과, 그와 이어진 같은 층
  작업면 묶음 전체, height_common.connected_level)의 위험 가장자리만 본다. 한 상판을 OBB 여러 개로 나눠 등록해도 하나로 등록한
  것과 같은 외곽 가장자리를 본다.
  HELD(가려진 동안 유지한 마지막 지지 표면)는 확인 필요 PREDICTED 를 붙인다(standing_surface_held).
- UNKNOWN(접지점 없음): 수평 범위가 footprint 에 걸치는 작업면(그 위에 있을 수 있음)의 위험 가장자리를 보고 확인 필요 Z_UNKNOWN 을
  붙인다(z_unknown_on_surface_footprint). 모르면 '가장자리에 없음'으로 확정하지 않는다(5.2, 7.1). 걸치는 작업면이 없으면 그 위에
  있을 근거가 없어 판정하지 않는다(VALID 관측 골반이 상판보다 낮은 작업면은 뺀다, height_common). 그래서 활성 R2 는 접지점이
  사라져도 범위가 footprint 에 걸치는 한 해소되지 않는다. 접지점을 모른 채 범위가 작업면 밖으로 나가면 수평 위치로 '그 위에
  없음'을 추론해 판정을 내지 않는다(Z 를 확인한 해소가 아님, lifecycle 검증 정책은 Z 를 보지 않는다).

**거리와 히스테리시스(7.1·7.2).** 거리는 R1 과 같은 수평 불확실성 범위(position_xy 와 접지점 XY 각각 k=2 축 정렬 타원 + 점유
반경 0.3 m, zone_geometry.HorizontalRange)에서 위험 가장자리 선분까지의 간격이다(둘 중 작은 쪽).
- 발생: 간격 ≤ 1.0 m(raise_distance_m) → holding=False(edge_within_raise_distance).
- 유지: 1.0 m < 간격 < 1.3 m(clear_distance_m) → holding=True(edge_within_clear_distance). 활성 이벤트만 유지한다.
- 그 밖: 판정 없음. 판정 없이 유효 관측이 3초(lifecycle 기본 clear_hold_ms) 이어지면 해소된다(명세 '1.3m 이상에서 3초').
발생 지속 조건(3프레임 중 2)·재개는 lifecycle 이 한다. 경계는 BOUNDARY_TOLERANCE_M 이하를 닿은 것으로 본다(안전 쪽).

**낮은 신뢰 새 발생 보류(7.1·7.2, S15P31A101-725).** 접지점을 모르고(StandingBasis.UNKNOWN, 확인 필요 Z_UNKNOWN) 위치도 관측이
아닌 예측(observation_state PREDICTED)이면 발생 거리 안이어도 holding=True 로 낸다(low_confidence_onset). 새 이벤트를 만들지 않고
이미 열린 R2 만 유지한다(유지·해소·등급은 그대로). 접지점이 생기거나(KNOWN·HELD) 관측 위치(OBSERVED)가 되면 그 프레임부터 정상 발생
판정을 낸다. R1 의 '확실한 위치만 새로 발생'(수정 53)과 같은 방향이되 둘 다 모를 때만 막는다(17차: R2 27건 모두 Z_UNKNOWN, 21건 PREDICTED).

**확인 필요 이유(7.1).** 위의 Z_UNKNOWN·PREDICTED 와 R1 과 같은 트랙 이유(PREDICTED 위치, SINGLE_VIEW, IDENTITY_UNCERTAIN).
보정 상태가 PROVISIONAL 등 CALIBRATED 가 아니면 PROVISIONAL_CALIB(명세 4.4, D-G3-03, RuleContext.calibration_confidence).

판정 대상은 사람이고 구역이 아니라 zone_id 는 비운다. 근거 코드(reasons): 거리 코드 + 서 있는 면 코드(standing_on_surface /
standing_surface_held / z_unknown_on_surface_footprint). 수치(metrics, 계약 rule_metrics 예시 이름): edge_distance_m(position_xy·
접지점 중 가까운 쪽에서 가장 가까운 위험 가장자리까지 수평 거리, 불확실성 범위 넓힘 전), height_diff_m(불확실성 범위가 가장
가까운 위험 가장자리의 높이차).
"""

import math
from collections.abc import Iterator

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import ConfidenceReason, Severity
from simai_ai.risk.rule import Finding, RuleContext, RuleSpec
from simai_ai.risk.rules.height_common import (
    OPERATIONAL_HEIGHT_PROFILE,
    HeightProfile,
    StandingBasis,
    hazard_edges,
    low_confidence_onset,
    standing,
)
from simai_ai.risk.rules.r1_zone_entry import horizontal_ranges, track_confidence_reasons
from simai_ai.risk.world import ObservationState
from simai_ai.risk.zone_geometry import (
    BOUNDARY_TOLERANCE_M,
    OCCUPANCY_RADIUS_M,
    UNCERTAINTY_K,
    distance_to_polygon,
)

R2_RULE_ID = "R2"
R2_RULE_VERSION = 1
"""운영 프로파일 버전. 교실 시험 프로파일은 R2_RULE_VERSION + TEST_PROFILE_VERSION_OFFSET(1001)"""
R2_SEVERITY = Severity.CRITICAL
"""명세 7.2 R2 심각도"""
R2_RAISE_DISTANCE_M = 1.0
"""발생 거리(명세 7.2 R2 '1.0m 이내')"""
R2_CLEAR_DISTANCE_M = 1.3
"""해소 거리(명세 7.2 R2 '1.3m 이상에서 3초'). 이보다 가까우면 유지 판정"""

REASON_EDGE_RAISE = "edge_within_raise_distance"
REASON_EDGE_CLEAR = "edge_within_clear_distance"
REASON_STANDING_ON_SURFACE = "standing_on_surface"
REASON_STANDING_HELD = "standing_surface_held"
REASON_Z_UNKNOWN_ON_SURFACE = "z_unknown_on_surface_footprint"

_R2_READS = (
    "tracks.observation_state",
    "tracks.position_xy",
    "tracks.position_xy_sigma",
    "tracks.ground_anchor_xyz",
    "tracks.ground_anchor_sigma",
    "tracks.support_surface_id",
    "tracks.z_validity",
    "tracks.cameras",
    "tracks.identity_confidence",
    "work_surfaces",
)


class EdgeProximityRule:
    """R2 가장자리 근접. 상태를 갖지 않는다(히스테리시스는 holding 으로, 시간 확인은 lifecycle 이 한다).

    - profile: 높이 프로파일. 규칙 버전은 profile.rule_version(R2_RULE_VERSION).
    - raise_distance_m·clear_distance_m: 발생·해소 거리(명세 1.0·1.3 m).
    - k·occupancy_radius_m: 수평 불확실성 범위(명세 7.1 k=2, 0.3 m).
    - tolerance_m: 경계 접촉 허용오차.
    """

    def __init__(
        self,
        *,
        profile: HeightProfile = OPERATIONAL_HEIGHT_PROFILE,
        raise_distance_m: float = R2_RAISE_DISTANCE_M,
        clear_distance_m: float = R2_CLEAR_DISTANCE_M,
        k: float = UNCERTAINTY_K,
        occupancy_radius_m: float = OCCUPANCY_RADIUS_M,
        tolerance_m: float = BOUNDARY_TOLERANCE_M,
    ) -> None:
        params = (
            ("raise_distance_m", raise_distance_m),
            ("clear_distance_m", clear_distance_m),
            ("k", k),
            ("occupancy_radius_m", occupancy_radius_m),
            ("tolerance_m", tolerance_m),
        )
        for name, value in params:
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise RiskInputError(f"R2.{name} 는 0 이상 유한한 수여야 한다")
        if not raise_distance_m <= clear_distance_m:
            raise RiskInputError("R2: raise_distance_m ≤ clear_distance_m 여야 한다")
        self._profile = profile
        self._raise = float(raise_distance_m)
        self._clear = float(clear_distance_m)
        self._k = float(k)
        self._radius = float(occupancy_radius_m)
        self._tol = float(tolerance_m)
        self._spec = RuleSpec(rule_id=R2_RULE_ID, rule_version=profile.rule_version(R2_RULE_VERSION), reads=_R2_READS)

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    @property
    def profile(self) -> HeightProfile:
        return self._profile

    def evaluate(self, ctx: RuleContext) -> Iterator[Finding]:
        edges = hazard_edges(ctx.work_surfaces, self._profile.height_threshold_m)
        if not edges:
            return
        for track in sorted(ctx.world.tracks, key=lambda t: t.world_id):
            if track.observation_state is ObservationState.UNKNOWN:
                continue  # 정확한 위치 사용 중단(6.1). 활성 이벤트는 lifecycle 이 UNVERIFIABLE 로 유지한다
            ranges = horizontal_ranges(track, k=self._k, radius_m=self._radius)
            st = standing(track, ctx.work_surfaces, ranges, tolerance_m=self._tol)
            level = {s.surface_id for s in st.surfaces}
            mine = [e for e in edges if e.surface_id in level]
            if not mine:
                continue
            cutoff = max(self._clear, self._tol)
            best_gap, best_edge = min(
                ((min(r.gap_to(e.segment, exact_below=cutoff) for r in ranges), e) for e in mine),
                key=lambda pair: pair[0],
            )
            touching = best_gap <= self._raise + self._tol
            if not touching and best_gap >= self._clear:
                continue
            raising = touching and not low_confidence_onset(track, st.basis)
            confidence: set[ConfidenceReason] = set()
            if st.basis is StandingBasis.UNKNOWN:
                basis_reason = REASON_Z_UNKNOWN_ON_SURFACE
                confidence.add(ConfidenceReason.Z_UNKNOWN)
            elif st.basis is StandingBasis.HELD:
                basis_reason = REASON_STANDING_HELD
                confidence.add(ConfidenceReason.PREDICTED)
            else:
                basis_reason = REASON_STANDING_ON_SURFACE
            confidence |= track_confidence_reasons(track) | ctx.calibration_confidence()
            distance = min(distance_to_polygon(r.center, e.segment) for r in ranges for e in mine)
            yield Finding(
                rule_id=self._spec.rule_id,
                rule_version=self._spec.rule_version,
                severity=R2_SEVERITY,
                world_ids=(track.world_id,),
                zone_id=None,
                confidence_reasons=tuple(confidence),
                reasons=(REASON_EDGE_RAISE if touching else REASON_EDGE_CLEAR, basis_reason),
                metrics={"edge_distance_m": distance, "height_diff_m": best_edge.height_diff_m},
                holding=not raising,
            )
