"""R3 고소 위치 (명세 7.2 R3, 5.2 높이 규칙·접지, 7.1 불확실성 범위·확인 필요, 7.4 교실 시험, 7.5, 15.1 G6).

명세: 서 있는 면(등록 작업면)과 인접한 낮은 면의 높이 차 2 m 이상, 안전 난간 없는 구역(교실은 바닥 기준으로 단순화). HIGH.
높이 기준은 프로파일로 정한다(height_common: 운영 2.0 m = R3 v1, 교실 시험 0.35 m = R3 v1001). 높이차의 기준 면도 프로파일이 정한다.
- 운영(floor_reference=False): 서 있는 층(이어진 같은 높이 작업면 묶음)의 둘레 중 인접한 낮은 면과 높이차가 기준 이상인 구간
  (= 그 층의 R2 위험 가장자리)이 있으면 고소 위치다. height_diff_m 은 그런 구간 중 가장 큰 높이차다. 예: 3 m 상판을 2.5 m 상판이
  둘러싸면 인접 높이차 0.5 m 라 고소 위치가 아니다. 서 있는 층이 등록 작업면으로 확인되지 않으면(접지점 높이에 등록 작업면이 없음)
  인접 면을 알 수 없어 바닥 기준으로 보수적으로 판정한다(standing_surface_unregistered).
- 교실 시험(floor_reference=True): 교실 단순화대로 서 있는 면 높이 − 바닥(0).
난간 정보가 없어 모든 작업면을 난간 없는 것으로 본다(안전 쪽).

사람(OBSERVED·PREDICTED 트랙)마다 판정한다. UNKNOWN 트랙은 위치를 쓰지 않으므로(6.1) 판정하지 않고, 활성 이벤트는
lifecycle 이 해소하지 않은 채 UNVERIFIABLE 로 둔다(6.4).

**서 있는 면(height_common.standing).**
- KNOWN: 위 높이차 ≥ 기준이면 발생(standing_surface_elevated). 기준 미만(바닥 등)이면 판정 없음 → 유효 관측 3초 뒤 해소.
- HELD(가려진 동안 유지한 마지막 지지 표면, z_validity UNCERTAIN): 같은 판정 + 확인 필요 PREDICTED(standing_surface_held).
  5.2 '고소 위치였던 사람이 가려지면 마지막 유효 높이를 유지하고 바닥 위치로 바꾸지 않는다'.
- UNKNOWN(접지점 없음): 수평 불확실성 범위(k=2 타원 + 0.3 m, R1 과 같은 범위)가 고소 위치인 작업면(위 높이차 기준) footprint 에
  조금이라도 걸치면 발생 + 확인 필요 Z_UNKNOWN(z_unknown_on_elevated_footprint). 그 위에 있을 수 있는 사람을 '고소 위치 아님'으로 확정하지
  않는다(5.2 '없으면 확인 필요', 7.1). 걸치는 높은 작업면이 없으면 높은 곳에 있을 근거가 없어 판정하지 않는다. VALID 관측 골반이
  상판보다 낮은 작업면은 그 위에 있을 수 없어 뺀다(height_common, 책상에 앉아 다리가 가려진 사람).
  그래서 활성 R3 은 접지점이 사라져도(단상 위에서 가려져 유지가 끝남) 범위가 footprint 에 걸치는 한 해소되지 않는다. 접지점을 모른
  채 범위가 모든 높은 작업면 밖으로 나가면 수평 위치로 '그 위에 없음'을 추론해 판정을 내지 않는다(Z 를 확인한 해소가 아님).
발생·해소 띠(히스테리시스)는 명세에 수치가 없어 두지 않는다. 해소는 판정 없이 유효 관측이 3초(lifecycle 기본) 이어질 때다.
발생 지속 조건(3프레임 중 2)·재개는 lifecycle 이 한다.

**확인 필요 이유(7.1).** 위의 Z_UNKNOWN·PREDICTED 와 R1 과 같은 트랙 이유(PREDICTED 위치, SINGLE_VIEW, IDENTITY_UNCERTAIN).
보정 상태가 PROVISIONAL 등 CALIBRATED 가 아니면 PROVISIONAL_CALIB(명세 4.4, D-G3-03, RuleContext.calibration_confidence).

판정 대상은 사람이고 구역이 아니라 zone_id 는 비운다. 수치(metrics): height_diff_m(판정에 쓴 높이차. 운영은 인접 낮은 면과의
높이차, 교실 시험은 서 있는 면 높이 − 바닥. UNKNOWN 이면 걸친 작업면 중 가장 큰 값).
"""

import math
from collections.abc import Iterable, Iterator

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import ConfidenceReason, Severity
from simai_ai.risk.rule import Finding, RuleContext, RuleSpec
from simai_ai.risk.rules.height_common import (
    HEIGHT_TOLERANCE_M,
    OPERATIONAL_HEIGHT_PROFILE,
    HazardEdge,
    HeightProfile,
    StandingBasis,
    hazard_edges,
    standing,
)
from simai_ai.risk.rules.r1_zone_entry import horizontal_ranges, track_confidence_reasons
from simai_ai.risk.work_surface import FLOOR_Z_M, WorkSurface
from simai_ai.risk.world import ObservationState
from simai_ai.risk.zone_geometry import BOUNDARY_TOLERANCE_M, OCCUPANCY_RADIUS_M, UNCERTAINTY_K

R3_RULE_ID = "R3"
R3_RULE_VERSION = 1
"""운영 프로파일 버전. 교실 시험 프로파일은 R3_RULE_VERSION + TEST_PROFILE_VERSION_OFFSET(1001)"""
R3_SEVERITY = Severity.HIGH
"""명세 7.2 R3 심각도"""

REASON_STANDING_ELEVATED = "standing_surface_elevated"
REASON_STANDING_HELD = "standing_surface_held"
REASON_Z_UNKNOWN_ON_ELEVATED = "z_unknown_on_elevated_footprint"
REASON_STANDING_UNREGISTERED = "standing_surface_unregistered"

_R3_READS = (
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


class ElevatedPositionRule:
    """R3 고소 위치. 상태를 갖지 않는다(시간 확인은 lifecycle 이 한다).

    - profile: 높이 프로파일. 규칙 버전은 profile.rule_version(R3_RULE_VERSION).
    - k·occupancy_radius_m: 수평 불확실성 범위(명세 7.1 k=2, 0.3 m). Z 를 모를 때 작업면에 걸치는지 본다.
    - tolerance_m: 경계 접촉 허용오차.
    """

    def __init__(
        self,
        *,
        profile: HeightProfile = OPERATIONAL_HEIGHT_PROFILE,
        k: float = UNCERTAINTY_K,
        occupancy_radius_m: float = OCCUPANCY_RADIUS_M,
        tolerance_m: float = BOUNDARY_TOLERANCE_M,
    ) -> None:
        for name, value in (("k", k), ("occupancy_radius_m", occupancy_radius_m), ("tolerance_m", tolerance_m)):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise RiskInputError(f"R3.{name} 는 0 이상 유한한 수여야 한다")
        self._profile = profile
        self._threshold = profile.height_threshold_m
        self._k = float(k)
        self._radius = float(occupancy_radius_m)
        self._tol = float(tolerance_m)
        self._spec = RuleSpec(rule_id=R3_RULE_ID, rule_version=profile.rule_version(R3_RULE_VERSION), reads=_R3_READS)

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    @property
    def profile(self) -> HeightProfile:
        return self._profile

    def evaluate(self, ctx: RuleContext) -> Iterator[Finding]:
        edges = () if self._profile.floor_reference else hazard_edges(ctx.work_surfaces, self._threshold)
        for track in sorted(ctx.world.tracks, key=lambda t: t.world_id):
            if track.observation_state is ObservationState.UNKNOWN:
                continue  # 정확한 위치 사용 중단(6.1). 활성 이벤트는 lifecycle 이 UNVERIFIABLE 로 유지한다
            ranges = horizontal_ranges(track, k=self._k, radius_m=self._radius)
            st = standing(track, ctx.work_surfaces, ranges, tolerance_m=self._tol)
            confidence: set[ConfidenceReason] = set()
            if st.basis is StandingBasis.UNKNOWN:
                height = self._height_diff(st.surfaces, edges)
                if height is None:
                    continue
                reason = REASON_Z_UNKNOWN_ON_ELEVATED
                confidence.add(ConfidenceReason.Z_UNKNOWN)
            else:
                assert st.z is not None  # KNOWN·HELD 는 접지점 Z 가 있다
                if self._profile.floor_reference or not st.surfaces:
                    # 교실 단순화, 또는 등록 작업면으로 확인되지 않은 높이(인접 면 모름 → 바닥 기준으로 보수적)
                    height = st.z - FLOOR_Z_M
                    if not self._elevated(height):
                        continue
                    unregistered = not self._profile.floor_reference
                else:
                    found = self._height_diff(st.surfaces, edges)
                    if found is None:
                        continue
                    height, unregistered = found, False
                if st.basis is StandingBasis.HELD:
                    reason = REASON_STANDING_HELD
                    confidence.add(ConfidenceReason.PREDICTED)
                else:
                    reason = REASON_STANDING_ELEVATED
                if unregistered:
                    reason = REASON_STANDING_UNREGISTERED
            confidence |= track_confidence_reasons(track) | ctx.calibration_confidence()
            yield Finding(
                rule_id=self._spec.rule_id,
                rule_version=self._spec.rule_version,
                severity=R3_SEVERITY,
                world_ids=(track.world_id,),
                zone_id=None,
                confidence_reasons=tuple(confidence),
                reasons=(reason,),
                metrics={"height_diff_m": height},
            )

    def _elevated(self, height_diff: float) -> bool:
        return height_diff >= self._threshold - HEIGHT_TOLERANCE_M

    def _height_diff(self, surfaces: Iterable[WorkSurface], edges: tuple[HazardEdge, ...]) -> float | None:
        """작업면들(서 있는 층·후보) 중 고소 위치인 것의 가장 큰 높이차. 없으면 None.

        교실 시험은 상판 높이 − 바닥, 운영은 그 작업면 둘레 위험 가장자리(인접한 낮은 면과 높이차 ≥ 기준)의 높이차.
        """
        if self._profile.floor_reference:
            values = [s.top_height_m - FLOOR_Z_M for s in surfaces if self._elevated(s.top_height_m - FLOOR_Z_M)]
        else:
            ids = {s.surface_id for s in surfaces}
            values = [e.height_diff_m for e in edges if e.surface_id in ids]
        return max(values) if values else None
