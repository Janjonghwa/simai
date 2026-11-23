"""R5 진입 예측 (명세 7.2 R5와 R5 설명, 7.1 불확실성 범위, 6.4, 7.3, 15.1 G6, 부록 D-G6-03, FR-70).

현재 속도로 1.5~2초 뒤 위치의 불확실성 범위가 R1 구역에 들어가는지 예측한다. 심각도는 늘 CAUTION 이다. 7.3 표대로
관제 화면 안내로 가고 현장 소리(노트북 로컬 경보)·반장 PWA 로는 가지 않는다. 채널은 alerting.channels_for 가 정하고
이 규칙은 채널을 고르지 않는다.

**속도를 모를 때(판정하지 않되 해소 근거로 쓰지 않음).** 속도는 world_state 의 velocity(World Track 칼만 필터 속도,
m/s)다. 계약에 속도 불확실성이 없어서, 아래 중 하나라도 해당하면 속도를 모르는 것으로 본다. 없는 속도를 지어내거나 0 으로
채우지 않고(규칙 2) 발생 판정을 내지 않는다. 대신 그 사람에게 가장 가까운 구역으로 유지 판정(holding) 하나를 낸다.
holding 은 새 이벤트를 만들지 않고 활성 R5 이벤트의 해소 확인만 처음부터 다시 세게 하므로(lifecycle), '속도를 모름'이
'진입 가능성이 없어짐'으로 읽혀 활성 R5 가 해소되지 않는다(6.4, 6 머리말: 모르는 상태를 안전으로 판단하지 않음).
근거 코드는 input_unknown 과 아래 세부 코드다.
- observation_state 가 OBSERVED 가 아니다(track_not_observed). PREDICTED·UNKNOWN 이면 속도가 최신 관측으로 갱신되지 않았다(6.1).
- track_lifecycle 이 CONFIRMED 가 아니다(track_not_confirmed). TENTATIVE 는 필터 속도가 아직 수렴하지 않았고(초기 속도 σ
  1 m/s), OCCLUDED·LOST 는 위치 측정이 끊겼다.
- velocity 가 없다(velocity_missing).
- 수평 속력이 max_speed_mps(4 m/s) 를 넘는다(speed_implausible). 실내에서 나기 어려운 값이라 필터 오류(ID 전환 등)로 본다.
- 트랙 staleness(T_gap + T_lag, 6.1)가 max_staleness_ms(500 ms)를 넘는다(position_stale).
지속 조건을 세는 중에 속도를 잃으면 이벤트 없이 사라진다(위치는 관측되므로 없는 위험을 지어내지 않음). 속도를 모르는 동안
활성 R5 는 VERIFIED 로 남는 한계가 있다(docs/design/risk-r5-entry-prediction.md).
지금 범위가 이미 구역에 걸린 사람에게는 이 holding 을 내지 않는다(R1 이 맡는다). 속도를 알고 느린 것(속력 min_speed_mps
0.2 m/s 미만, 실제로 멈춰 섬)은 '모름'이 아니므로 판정을 내지 않는다(해소 근거가 된다).

**예측.** 범위는 R1 과 같은 범위(zone_geometry.HorizontalRange: position_xy, 접지점이 있으면 그 XY 도. 각각 k=2 축 정렬
타원 + 점유 반경 0.3 m. 타원은 R1 과 같이 바깥에 접하는 48각형)를 그대로 쓴다. 크기는 그대로 둔다(명세 '현재 속도로'. 계약에
속도 불확실성이 생기면 시간에 따라 넓힌다). 긴 반축 원 같은 근사로 넓히지 않는다: σx ≠ σy(운영 Tier 0 에서 흔함)에서 원만
걸리는 띠의 사람이 방향과 관계없이 '진입 중'이 되기 때문이다(2차 리뷰 N1).
- 위치는 t_last_valid 의 값이라 먼저 staleness 만큼 속도로 옮겨 판정 시각(now)의 범위를 만든다. 접촉은 그 범위와 판정 시각
  이후 구간에서만 찾는다. 관측과 판정 사이에 이미 지나간 교차는 예측으로 내지 않는다.
- 처음 닿는 시간은 해석적으로 구한다(first_touch_range_s). 원(σx = σy)은 움직이는 원과 다각형 변 캡슐(양 끝 원 + 띠)의
  교차다(first_touch_s). 타원은 움직이는 48각형과 구역 다각형이 점유 반경 안으로 처음 다가가는 때로, 한쪽 꼭짓점과 다른 쪽
  변 사이에서만 생기므로 '범위 꼭짓점 → 구역 변'과 '구역 꼭짓점 → 범위 변'(반대 속도) 캡슐 교차 중 가장 이른 것이다.
  연속 시간이라 짧은 스침(꼭짓점 접선 포함)도 놓치지 않고, 오목 다각형도 된다. 경계는 BOUNDARY_TOLERANCE_M(1e-9 m) 이하를
  닿은 것으로 본다.
- 선행 시간(time_to_entry_s) = 판정 시각부터 처음 닿을 때까지. 표시값은 ms 로 반올림하고, 발생·유지·다가가는 속력 비교는
  반올림 전 연속 값으로 한다.

**R1 인계.** 관측 위치의 범위(R1 정의)가 이미 구역에 걸리면 R5 는 아무 판정도 내지 않는다(R1 이 발생). 관측 범위는
안 걸리지만 판정 시각으로 옮긴 같은 범위(R1 과 같은 overlaps)가 걸리면 '판정 시각에 진입 중'으로 보고 선행 0 초 발생
판정(predicted_touching_now)을 낸다. 관측에서 판정 시각까지 구역 쪽으로 움직여야만 생기므로 멀어지거나 나란히 걷는 사람에게는
나지 않는다. R1 은 관측 위치만 보므로 다음 관측에서 R1 이 맡는다.

**발생·유지(히스테리시스, 7.1).** 시간 기준으로 둔다.
- 발생: 선행 시간 ≤ horizon_ms(초기값 2초, 명세 1.5~2초) → holding=False.
- 유지: horizon_ms < 선행 시간 ≤ horizon_ms + clear_extra_ms(0.5초) → holding=True(활성 이벤트만 유지).
- 다가가는 속력: 판정 시각 범위와 구역의 간격 ÷ 선행 시간이 min_closing_speed_mps(0.4 m/s) 미만이면 판정 없음.
  구역 옆을 거의 나란히 지나가는 사람이 속도 잡음 때문에 구역 쪽으로 기운 예측을 내는 것을 거른다.
- 그 밖(닿지 않음·멀어짐·옆으로 지나감)은 판정 없음. 판정 없이 유효 관측이 이어지면 lifecycle 이 해소한다.
발생 지속 조건(3프레임 중 2)·해소 유지 시간·재개는 lifecycle 이 한다. 규칙은 프레임마다 판정만 낸다.

**높이(VOLUME_3D 만).** R1 과 같은 점유 높이(r1_zone_entry.occupancy_height)를 지금 값 그대로 쓴다. 높이가 겹치면
XY 대로, 해소 간격(0.3 m) 안쪽이면 유지만, 그보다 떨어지면 판정 없음. 서 있는 면이나 위 끝을 모르면 높이로 거르지 않고
XY 대로 내되 확인 필요(`Z_UNKNOWN`)를 붙인다(모르면 '진입 없음'으로 확정하지 않음).

**확인 필요 이유(7.1).** R1 과 같은 트랙 이유(SINGLE_VIEW, IDENTITY_UNCERTAIN)와 VOLUME_3D 의 Z_UNKNOWN·
JOINT_UNCERTAIN. PREDICTED 는 붙이지 않는다(Lead 결정): 판정에 쓰는 위치·속도는 OBSERVED 트랙의 관측값이고 PREDICTED
트랙은 판정하지 않는다. 앞날을 내다보는 것은 이 규칙의 정의다. 속도를 모를 때의 holding 에도 PREDICTED 를 붙이지 않는다
(PREDICTED·UNKNOWN 트랙의 활성 이벤트는 lifecycle 이 이미 UNVERIFIABLE 로 둔다).

**낮은 신뢰 새 발생 보류(7.1·7.2, S15P31A101-855).** 높이를 모르는(Z_UNKNOWN) 예측이 위치 예측(PREDICTED)으로만 발생하려
하면 새 R5 를 내지 않는다(R2·R3 의 S15P31A101-725 와 같은 조합, `r5_low_confidence_onset`). 이 규칙은 PREDICTED 트랙을 판정하지
않고 Z_UNKNOWN 은 R1 구역 대상에만, PREDICTED 는 가장자리 대상(HELD)에만 붙으므로 한 판정에 둘이 같이 오지 않는다. 둘이
겹치는 곳은 lifecycle 의 '지속 대기 중 관측 상실' 발생(마지막 발생 판정에 PREDICTED 를 더함)뿐이라 lifecycle 이 그 자리에서
이 함수로 걸러 대기를 버린다. 이미 열린 R5 의 유지·해소·확인 필요는 그대로고, 둘 중 하나만이면(관측 위치의 Z_UNKNOWN 이
지속 조건을 채움, 또는 높이를 아는 대기의 관측 상실) 지금처럼 발생한다.

**R2 가장자리(7.2 'R1·R2 구역 진입').** 등록 작업면이 있으면(RuleContext.work_surfaces) R2 와 같은 위험 가장자리
(height_common.hazard_edges, 높이 기준은 이 규칙의 프로파일)도 예측 대상이다. R2 의 발생 영역은 위험 가장자리 선분에서 1.0 m
(R2_RAISE_DISTANCE_M) 안이므로, 불확실성 범위의 점유 반경을 그만큼 넓힌 범위가 선분에 처음 닿는 때를 같은 계산
(first_touch_range_s, 두 점 다각형 = 선분)으로 구한다. 그래서 'R2 인계'도 R1 과 같다: 관측 범위가 이미 R2 발생 영역에 걸리면 R5 는
내지 않는다(R2 가 맡는다). 가장자리는 그 작업면 위(같은 층)에 선 사람에게만 대상이다(R2 와 같다). 접지점이 있을 때(KNOWN·HELD)
서 있는 층의 가장자리만 넣고, 접지점이 없으면(서 있는 층을 모름) 가장자리 예측을 하지 않는다. 그때 단상 footprint 위에 있을 수
있는 사람은 R2 자체가 확인 필요(Z_UNKNOWN)로 판정한다. 서 있는 층이 가려진 동안 유지한 마지막 지지 표면(HELD,
z_validity UNCERTAIN)이면 '그 층 위에 있다'는 전제가 마지막 추정값이라 가장자리 대상 판정에 확인 필요 PREDICTED 를 붙인다(R2·R3 와
같다). contact_validity 만 UNCERTAIN 인 걷기(z VALID)는 높이가 관측값이라 붙이지 않고, R1 구역 대상 판정의 기존 의미(PREDICTED
없음)는 그대로다. 가장자리 대상 판정은 zone_id 를 비우고 근거 코드 끝에 target_edge 를,
수치에 zone_distance_m 대신 edge_distance_m(가장 가까운 위험 가장자리까지 수평 거리)을 싣는다. 가장자리가 어느 높이부터 위험한지가
프로파일에 달려 있어 R5 도 운영(v1)·교실 시험(v1001) 프로파일 버전을 둔다. 작업면이 없으면 두 버전의 판정이 같다.

판정 근거 코드(reasons): predicted_entry(발생) / predicted_entry_within_clear_horizon(유지), 판정 시각에 이미 걸린 예측이면
predicted_touching_now 가 뒤따르고, VOLUME_3D 는 z_overlap / z_within_clear_margin 이 뒤따른다. 속도를 모를 때는
input_unknown + 세부 코드. 수치(metrics): time_to_entry_s(판정 시각부터 예상 진입까지, ms 반올림), speed_mps(수평 속력),
zone_distance_m(관측 position_xy·접지점 중 가까운 쪽에서 다각형까지 수평 거리. 가장자리 대상은 edge_distance_m). 속도를 모를
때는 zone_distance_m(가장자리 대상은 edge_distance_m) 만.
"""

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import ConfidenceReason, Severity
from simai_ai.risk.rule import Finding, RuleContext, RuleSpec
from simai_ai.risk.rules.height_common import (
    OPERATIONAL_HEIGHT_PROFILE,
    HazardEdge,
    HeightProfile,
    StandingBasis,
    hazard_edges,
    standing,
)
from simai_ai.risk.rules.r1_zone_entry import (
    OccupancyHeight,
    horizontal_ranges,
    occupancy_height,
    track_confidence_reasons,
)
from simai_ai.risk.rules.r2_edge_proximity import R2_RAISE_DISTANCE_M
from simai_ai.risk.world import ObservationState, TrackLifecycle, WorldTrack
from simai_ai.risk.zone import Zone, ZoneJudgeType
from simai_ai.risk.zone_geometry import (
    BOUNDARY_TOLERANCE_M,
    OCCUPANCY_RADIUS_M,
    UNCERTAINTY_K,
    HorizontalRange,
    Point,
    Polygon,
    distance_outside_polygon,
)

R5_RULE_ID = "R5"
R5_RULE_VERSION = 1
"""운영 프로파일 버전. 교실 시험 프로파일(가장자리 높이 기준 0.35 m)은 R5_RULE_VERSION + TEST_PROFILE_VERSION_OFFSET(1001)"""
R5_SEVERITY = Severity.CAUTION
"""명세 7.2 R5 심각도. 구역 등급과 관계없이 CAUTION 이다(관제 화면 안내, 7.3)"""
R5_HORIZON_MS = 2000
"""예측 시간(명세 7.2 R5 '1.5~2초', 초기값 2초). 업로드·판정·경보 지연과 사람 반응 시간을 반영한 값이다"""
R5_CLEAR_EXTRA_MS = 500
"""유지 띠: 선행 시간이 horizon 보다 이만큼 길어질 때까지는 활성 이벤트를 유지한다(히스테리시스, 7.1)"""
R5_MIN_SPEED_MPS = 0.2
"""이보다 느리면(속도를 알고 실제로 느림) 판정하지 않는다. 정지·제자리 흔들림을 진입 예측으로 보지 않는다"""
R5_MIN_CLOSING_SPEED_MPS = 0.4
"""구역 쪽으로 다가가는 평균 속력(판정 시각 간격 ÷ 선행 시간)의 하한. 구역 옆을 나란히 지나가는 사람에게 필터 속도
잡음(위치 잡음 σ 0.05 m 에서 속도 σ 약 0.15 m/s)으로 R5 가 나는 것을 줄인다. 이보다 느린 접근은 판정하지 않는다.
초기값이며 SIM 측정(tests/risk/test_r5_sim_g6.py)과 실영상으로 조정한다(명세화 대상)"""
R5_MAX_SPEED_MPS = 4.0
R5_MAX_STALENESS_MS = 500
"""트랙 staleness 상한. 명세 6.1 PREDICTED 상한(500 ms)과 같은 초기값"""
R5_CLEAR_MARGIN_M = 0.3
"""VOLUME_3D 높이 해소 간격(R1 과 같은 0.3 m)"""
_TIME_EPS_S = 1e-9
"""발생·유지 시간 경계 비교의 부동소수점 여유(초). 경계에 걸친 값은 안쪽(발생·유지)으로 본다"""

# 판정 근거 코드
REASON_PREDICTED_ENTRY = "predicted_entry"
REASON_PREDICTED_CLEAR_HORIZON = "predicted_entry_within_clear_horizon"
REASON_PREDICTED_TOUCHING_NOW = "predicted_touching_now"
REASON_Z_OVERLAP = "z_overlap"
REASON_Z_CLEAR_MARGIN = "z_within_clear_margin"
REASON_INPUT_UNKNOWN = "input_unknown"
REASON_TRACK_NOT_OBSERVED = "track_not_observed"
REASON_TRACK_NOT_CONFIRMED = "track_not_confirmed"
REASON_VELOCITY_MISSING = "velocity_missing"
REASON_SPEED_IMPLAUSIBLE = "speed_implausible"
REASON_POSITION_STALE = "position_stale"
REASON_TARGET_EDGE = "target_edge"

_R5_READS = (
    "tracks.observation_state",
    "tracks.track_lifecycle",
    "tracks.t_last_valid",
    "tracks.position_xy",
    "tracks.position_xy_sigma",
    "tracks.velocity",
    "tracks.ground_anchor_xyz",
    "tracks.ground_anchor_sigma",
    "tracks.head_z",
    "tracks.head_z_sigma",
    "tracks.skeleton",
    "tracks.cameras",
    "tracks.identity_confidence",
    "tracks.support_surface_id",
    "tracks.z_validity",
    "zones",
    "work_surfaces",
)


@dataclass(frozen=True)
class _Target:
    """진입을 예측할 수평 영역.

    - R1 구역: zone 이 있고 polygon 은 구역 다각형, reach_m = 0.
    - R2 가장자리: zone 이 없고 polygon 은 위험 가장자리 선분(두 점), reach_m = R2 발생 거리. 범위의 점유 반경에 reach_m 을
      더해 판정한다(선분에서 reach_m 안 = R2 발생 영역).
    """

    zone: Zone | None
    polygon: Polygon
    reach_m: float = 0.0
    held: bool = False  # 가장자리 대상: 서 있는 층이 HELD(마지막 추정 지지 표면)라 확인 필요 PREDICTED 를 붙인다

    @property
    def zone_id(self) -> str | None:
        return None if self.zone is None else self.zone.zone_id

    @property
    def distance_metric(self) -> str:
        return "zone_distance_m" if self.zone is not None else "edge_distance_m"

    def ranges(self, ranges: tuple[HorizontalRange, ...]) -> tuple[HorizontalRange, ...]:
        if self.reach_m == 0.0:
            return ranges
        return tuple(HorizontalRange(r.center, r.semi_axes, r.radius_m + self.reach_m) for r in ranges)


class EntryPredictionRule:
    """R5 진입 예측. 상태를 갖지 않는다(히스테리시스는 holding 으로, 시간 확인은 lifecycle 이 한다).

    - horizon_ms: 예측 시간(명세 1.5~2초, 초기값 2000 ms). 선행 시간이 이 안이면 발생.
    - clear_extra_ms: 유지 띠 폭. 선행 시간이 horizon_ms + clear_extra_ms 안이면 유지.
    - min_speed_mps: 이보다 느리면 판정하지 않는다(속도를 알고 실제로 느림 → 해소 근거).
    - max_speed_mps: 이보다 빠르면 속도를 믿지 않는다(속도 모름 → holding).
    - min_closing_speed_mps: 구역 쪽으로 다가가는 평균 속력(판정 시각 간격 ÷ 선행 시간)의 하한.
    - max_staleness_ms: 트랙 staleness 상한. 넘으면 속도 모름으로 본다.
    - k·occupancy_radius_m: 불확실성 범위(명세 7.1 k=2, 0.3 m). R1 과 같은 값이어야 'R1 이 맡음' 경계가 맞는다.
    - clear_margin_m: VOLUME_3D 높이 해소 간격.
    - tolerance_m: 경계 접촉 허용오차.
    - profile: 가장자리 높이 기준(height_common). 규칙 버전은 profile.rule_version(R5_RULE_VERSION).
    - edge_reach_m: R2 발생 거리(1.0 m). 위험 가장자리 선분에서 이 안이 R2 발생 영역이다.
    """

    def __init__(
        self,
        *,
        horizon_ms: int = R5_HORIZON_MS,
        clear_extra_ms: int = R5_CLEAR_EXTRA_MS,
        min_speed_mps: float = R5_MIN_SPEED_MPS,
        min_closing_speed_mps: float = R5_MIN_CLOSING_SPEED_MPS,
        max_speed_mps: float = R5_MAX_SPEED_MPS,
        max_staleness_ms: int = R5_MAX_STALENESS_MS,
        k: float = UNCERTAINTY_K,
        occupancy_radius_m: float = OCCUPANCY_RADIUS_M,
        clear_margin_m: float = R5_CLEAR_MARGIN_M,
        tolerance_m: float = BOUNDARY_TOLERANCE_M,
        profile: HeightProfile = OPERATIONAL_HEIGHT_PROFILE,
        edge_reach_m: float = R2_RAISE_DISTANCE_M,
    ) -> None:
        for name, value, minimum in (
            ("horizon_ms", horizon_ms, 1),
            ("clear_extra_ms", clear_extra_ms, 0),
            ("max_staleness_ms", max_staleness_ms, 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise RiskInputError(f"R5.{name} 는 {minimum} 이상 정수여야 한다")
        for name, value in (
            ("min_speed_mps", min_speed_mps),
            ("min_closing_speed_mps", min_closing_speed_mps),
            ("max_speed_mps", max_speed_mps),
            ("k", k),
            ("occupancy_radius_m", occupancy_radius_m),
            ("clear_margin_m", clear_margin_m),
            ("tolerance_m", tolerance_m),
            ("edge_reach_m", edge_reach_m),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise RiskInputError(f"R5.{name} 는 0 이상 유한한 수여야 한다")
        if not 0 < min_speed_mps <= max_speed_mps:
            raise RiskInputError("R5: 0 < min_speed_mps ≤ max_speed_mps 여야 한다")
        self._horizon_s = horizon_ms / 1000.0
        self._limit_s = (horizon_ms + clear_extra_ms) / 1000.0
        self._min_speed = float(min_speed_mps)
        self._min_closing = float(min_closing_speed_mps)
        self._max_speed = float(max_speed_mps)
        self._max_staleness = max_staleness_ms
        self._k = float(k)
        self._radius = float(occupancy_radius_m)
        self._margin = float(clear_margin_m)
        self._tol = float(tolerance_m)
        self._profile = profile
        self._edge_reach = float(edge_reach_m)
        self._spec = RuleSpec(rule_id=R5_RULE_ID, rule_version=profile.rule_version(R5_RULE_VERSION), reads=_R5_READS)

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    @property
    def profile(self) -> HeightProfile:
        return self._profile

    def evaluate(self, ctx: RuleContext) -> Iterator[Finding]:
        zone_targets = self._targets(ctx)
        edges = hazard_edges(ctx.work_surfaces, self._profile.height_threshold_m) if ctx.work_surfaces else ()
        if not zone_targets and not edges:
            return
        for track in sorted(ctx.world.tracks, key=lambda t: t.world_id):
            ranges = horizontal_ranges(track, k=self._k, radius_m=self._radius)
            targets = zone_targets + self._edge_targets(track, ctx, edges, ranges)
            if not targets:
                continue
            staleness_ms = max(0, ctx.staleness_ms(track))
            unknown = self._unknown_input(track, staleness_ms)
            if unknown is not None:
                hold = self._unknown_hold(track, targets, ranges, unknown)
                if hold is not None:
                    yield hold
                continue
            assert track.velocity is not None  # _unknown_input 이 보장한다
            velocity = (track.velocity[0], track.velocity[1])
            if math.sqrt(velocity[0] ** 2 + velocity[1] ** 2) < self._min_speed:
                continue  # 속도를 알고 실제로 느리다(멈춰 섬): 진입 예측 없음
            height = occupancy_height(track)
            for target in targets:
                finding = self._judge(track, target, ranges, height, velocity, staleness_ms / 1000.0)
                if finding is not None:
                    yield finding

    def _targets(self, ctx: RuleContext) -> tuple[_Target, ...]:
        """R1 구역 예측 대상."""
        return tuple(_Target(zone=z, polygon=z.polygon_xy) for z in ctx.zones)

    def _edge_targets(
        self,
        track: WorldTrack,
        ctx: RuleContext,
        edges: tuple[HazardEdge, ...],
        ranges: tuple[HorizontalRange, ...],
    ) -> tuple[_Target, ...]:
        """그 사람이 선 층의 R2 위험 가장자리 예측 대상. 접지점이 없으면(서 있는 층 모름) 비운다(R2 가 확인 필요로 맡는다)."""
        if not edges:
            return ()
        st = standing(track, ctx.work_surfaces, ranges, tolerance_m=self._tol)
        if st.basis is StandingBasis.UNKNOWN:
            return ()
        level = {s.surface_id for s in st.surfaces}
        held = st.basis is StandingBasis.HELD
        return tuple(
            _Target(zone=None, polygon=e.segment, reach_m=self._edge_reach, held=held)
            for e in edges
            if e.surface_id in level
        )

    def _unknown_input(self, track: WorldTrack, staleness_ms: int) -> str | None:
        """속도를 믿을 수 없으면 그 세부 코드, 믿을 수 있으면 None."""
        if track.observation_state is not ObservationState.OBSERVED:
            return REASON_TRACK_NOT_OBSERVED
        if track.track_lifecycle is not TrackLifecycle.CONFIRMED:
            return REASON_TRACK_NOT_CONFIRMED
        if track.velocity is None:
            return REASON_VELOCITY_MISSING
        if math.sqrt(track.velocity[0] ** 2 + track.velocity[1] ** 2) > self._max_speed:
            return REASON_SPEED_IMPLAUSIBLE
        if staleness_ms > self._max_staleness:
            return REASON_POSITION_STALE
        return None

    def _unknown_hold(
        self, track: WorldTrack, targets: tuple[_Target, ...], ranges: tuple[HorizontalRange, ...], code: str
    ) -> Finding | None:
        """속도를 모르는 사람의 유지 판정(가장 가까운 구역 하나). 지금 범위가 구역에 걸리면 R1 이 맡아 내지 않는다."""
        best: tuple[float, _Target] | None = None
        for target in targets:
            gap = min(r.gap_to(target.polygon) for r in target.ranges(ranges))
            if gap <= self._tol:
                return None
            if best is None or gap < best[0]:
                best = (gap, target)
        assert best is not None  # targets 가 비지 않는다
        target = best[1]
        confidence = track_confidence_reasons(track) - {ConfidenceReason.PREDICTED}
        if target.held:
            confidence.add(ConfidenceReason.PREDICTED)
        reasons = (REASON_INPUT_UNKNOWN, code) + (() if target.zone is not None else (REASON_TARGET_EDGE,))
        distance = min(distance_outside_polygon(r.center, target.polygon) for r in ranges)
        return Finding(
            rule_id=self._spec.rule_id,
            rule_version=self._spec.rule_version,
            severity=R5_SEVERITY,
            world_ids=(track.world_id,),
            zone_id=target.zone_id,
            confidence_reasons=tuple(confidence),
            reasons=reasons,
            metrics={target.distance_metric: distance},
            holding=True,
        )

    def _judge(
        self,
        track: WorldTrack,
        target: _Target,
        ranges: tuple[HorizontalRange, ...],
        height: OccupancyHeight,
        velocity: tuple[float, float],
        staleness_s: float,
    ) -> Finding | None:
        polygon = target.polygon
        center_ranges = ranges
        ranges = target.ranges(ranges)
        # 관측 위치의 범위가 이미 걸리면 R1(가장자리 대상이면 R2)이 맡는다.
        if any(r.overlaps(polygon, tolerance_m=self._tol) for r in ranges):
            return None
        zone = target.zone
        reasons: list[str] = []
        confidence: set[ConfidenceReason] = set()
        z_holding = False
        volume = zone is not None and zone.judge_type is ZoneJudgeType.VOLUME_3D and zone.z_range is not None
        if volume:
            assert zone is not None and zone.z_range is not None  # Zone 이 z_range 를 보장한다
            if height.bottom is None or height.top is None:
                confidence.add(ConfidenceReason.Z_UNKNOWN)
            else:
                lo = height.bottom - self._k * height.bottom_sigma
                hi = height.top + self._k * height.top_sigma
                z_gap = max(zone.z_range[0] - hi, lo - zone.z_range[1], 0.0)
                if z_gap > self._tol:
                    if z_gap >= self._margin:
                        return None
                    z_holding = True
            if height.joint_uncertain:
                confidence.add(ConfidenceReason.JOINT_UNCERTAIN)

        # 판정 시각의 범위(관측 위치를 staleness 만큼 옮김, R1 과 같은 범위)에서 앞으로만 찾는다.
        vx, vy = velocity
        lead_s: float | None = None
        gap_now = 0.0
        for r in ranges:
            now_range = HorizontalRange(
                center=(r.center[0] + vx * staleness_s, r.center[1] + vy * staleness_s),
                semi_axes=r.semi_axes,
                radius_m=r.radius_m,
            )
            t = first_touch_range_s(now_range, velocity, polygon, limit_s=self._limit_s, tolerance_m=self._tol)
            if t is not None and (lead_s is None or t < lead_s):
                lead_s = t
                gap_now = now_range.gap_to(polygon)
        if lead_s is None:
            return None
        if lead_s > 0.0 and gap_now + self._tol < self._min_closing * lead_s:
            return None  # 구역 쪽으로 다가가는 속력이 너무 느리다(옆으로 지나가며 속도 잡음으로 스침)
        within_horizon = lead_s <= self._horizon_s + _TIME_EPS_S
        raising = within_horizon and not z_holding
        reasons.append(REASON_PREDICTED_ENTRY if within_horizon else REASON_PREDICTED_CLEAR_HORIZON)
        if lead_s == 0.0:
            reasons.append(REASON_PREDICTED_TOUCHING_NOW)
        if volume and height.bottom is not None and height.top is not None:
            reasons.append(REASON_Z_CLEAR_MARGIN if z_holding else REASON_Z_OVERLAP)
        if zone is None:
            reasons.append(REASON_TARGET_EDGE)

        metrics = {
            "time_to_entry_s": round(lead_s, 3),  # 표시용 ms 반올림. 비교는 위에서 연속 값으로 했다
            "speed_mps": math.sqrt(vx * vx + vy * vy),
            target.distance_metric: min(distance_outside_polygon(r.center, polygon) for r in center_ranges),
        }
        confidence |= track_confidence_reasons(track)
        if target.held:
            confidence.add(ConfidenceReason.PREDICTED)
        return Finding(
            rule_id=self._spec.rule_id,
            rule_version=self._spec.rule_version,
            severity=R5_SEVERITY,
            world_ids=(track.world_id,),
            zone_id=target.zone_id,
            confidence_reasons=tuple(confidence),  # Finding 이 선언 순서로 정렬하고 needs_check 를 맞춘다
            reasons=tuple(reasons),
            metrics=metrics,
            holding=not raising,
        )


R5_LOW_CONFIDENCE_ONSET_REASONS = frozenset({ConfidenceReason.Z_UNKNOWN, ConfidenceReason.PREDICTED})
"""R5 새 발생을 보류하는 확인 필요 이유 조합(높이 모름 + 위치 예측, S15P31A101-725 와 같은 조합)"""


def r5_low_confidence_onset(finding: Finding) -> bool:
    """R5 새 발생을 보류할 낮은 신뢰 판정: 높이를 모르고(Z_UNKNOWN) 위치가 예측(PREDICTED)이다(S15P31A101-855).

    참이면 lifecycle 은 이 판정으로 새 이벤트를 만들지 않는다. 활성 이벤트의 유지·해소·확인 필요 이유는 바꾸지 않는다.
    둘 중 하나만이면 거짓(지금처럼 발생).
    """
    return finding.rule_id == R5_RULE_ID and R5_LOW_CONFIDENCE_ONSET_REASONS <= set(finding.confidence_reasons)


def first_touch_range_s(
    rng: HorizontalRange,
    velocity: Sequence[float],
    polygon: Polygon,
    *,
    limit_s: float,
    tolerance_m: float = BOUNDARY_TOLERANCE_M,
) -> float | None:
    """불확실성 범위(R1 정의)가 수평 속도로 움직일 때 다각형에 처음 닿는 시각(초, 0 이상). limit_s 안에 없으면 None.

    지금 걸리면(rng.overlaps, R1 과 같은 판정) 0. 원(σx = σy)은 first_touch_s 로 바로 구한다. 타원은 gap_to 와 같은 바깥 접
    48각형 P 와 점유 반경 r 로 본다. 처음 닿기 전에는 P 와 구역의 거리 > r 이고, 두 다각형 경계 사이 거리는 한쪽 꼭짓점과
    다른 쪽 변 사이에서 가장 작으므로, 첫 접촉은 'P 꼭짓점이 구역 변에 r 까지 다가감' 또는 '구역 꼭짓점이 P 변에 r 까지
    다가감(상대 속도 −v)' 중 가장 이른 것이다. 먼저 P 를 품는 원으로 limit_s 안 접촉이 없으면 바로 None 을 돌려준다.
    """
    if rng.overlaps(polygon, tolerance_m=tolerance_m):
        return 0.0
    a, b = rng.semi_axes
    if a == b:
        return first_touch_s(rng.center, a + rng.radius_m, velocity, polygon, limit_s=limit_s, tolerance_m=tolerance_m)
    cx, cy = rng.center
    offsets = [(x - cx, y - cy) for x, y in rng.ellipse_outline()]
    outer = max(math.sqrt(ox * ox + oy * oy) for ox, oy in offsets) + rng.radius_m
    if first_touch_s(rng.center, outer, velocity, polygon, limit_s=limit_s, tolerance_m=tolerance_m) is None:
        return None  # P 를 품는 원도 닿지 않는다
    vx, vy = float(velocity[0]), float(velocity[1])
    best: float | None = None
    for ox, oy in offsets:
        t = first_touch_s((cx + ox, cy + oy), rng.radius_m, (vx, vy), polygon, limit_s=limit_s, tolerance_m=tolerance_m)
        if t is not None and (best is None or t < best):
            best = t
    for zx, zy in polygon:
        relative = (float(zx) - cx, float(zy) - cy)
        t = first_touch_s(relative, rng.radius_m, (-vx, -vy), offsets, limit_s=limit_s, tolerance_m=tolerance_m)
        if t is not None and (best is None or t < best):
            best = t
    return best


def first_touch_s(
    center: Point,
    radius_m: float,
    velocity: Sequence[float],
    polygon: Polygon,
    *,
    limit_s: float,
    tolerance_m: float = BOUNDARY_TOLERANCE_M,
) -> float | None:
    """반경 radius_m 원이 center 에서 수평 속도로 움직일 때 다각형(영역)에 처음 닿는 시각(초, 0 이상). limit_s 안에 없으면 None.

    다각형 밖에서 출발한 원은 안으로 들어가기 전에 반드시 경계(변)에 반경 거리까지 다가가므로, 각 변을 반경만큼 넓힌 캡슐
    (양 끝 원 + 가운데 띠)과 반직선의 첫 교차 중 가장 이른 것이 답이다. 연속 시간 해석해라 짧은 스침도 놓치지 않는다.
    반경에 tolerance_m 을 더해 접선 접촉이 부동소수점 끝자리로 빠지지 않게 한다(7.1 '조금이라도 걸리면'). 오목 다각형도 된다.
    """
    cx, cy = float(center[0]), float(center[1])
    vx, vy = float(velocity[0]), float(velocity[1])
    reach = float(radius_m) + tolerance_m
    if distance_outside_polygon((cx, cy), polygon) <= reach:
        return 0.0
    a = vx * vx + vy * vy
    if a == 0.0:
        return None
    best = math.inf
    n = len(polygon)
    for i in range(n):
        ax, ay = float(polygon[i][0]), float(polygon[i][1])
        bx, by = float(polygon[(i + 1) % n][0]), float(polygon[(i + 1) % n][1])
        # 꼭짓점 A 의 원
        dx, dy = cx - ax, cy - ay
        b = dx * vx + dy * vy
        disc = b * b - a * (dx * dx + dy * dy - reach * reach)
        if disc >= 0.0:
            t = (-b - math.sqrt(disc)) / a
            if 0.0 <= t < best:
                best = t
        # 변 AB 의 띠(법선 거리 ≤ reach, 변 위 투영)
        ex, ey = bx - ax, by - ay
        length = math.sqrt(ex * ex + ey * ey)
        if length == 0.0:
            continue
        ux, uy = ex / length, ey / length
        nx, ny = -uy, ux
        d0 = dx * nx + dy * ny
        vn = vx * nx + vy * ny
        if d0 > reach and vn < 0.0:
            t = (reach - d0) / vn
        elif d0 < -reach and vn > 0.0:
            t = (-reach - d0) / vn
        else:
            continue  # 띠 안에서 출발(끝 원으로 들어감)하거나 띠와 나란하거나 멀어진다
        if 0.0 <= t < best:
            proj = (dx + vx * t) * ux + (dy + vy * t) * uy
            if 0.0 <= proj <= length:
                best = t
    return best if best <= limit_s else None
