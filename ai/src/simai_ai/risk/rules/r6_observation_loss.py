"""R6 위험 구역 내 관측 상실 (명세 7.2 R6, 6.4, 15.1 G3).

명세: 구역 안에서 마지막으로 보인 사람이 UNKNOWN 이면 '원래 규칙 유지 + UNVERIFIABLE'. R6 은 자기 등급의
새 이벤트를 만들지 않고, 원래 규칙(R1 등 구역 규칙)의 이벤트를 자동 해소하지 않은 채 확인 불가로 둔다.

두 겹으로 구현한다.
1. lifecycle(S15P31A101-76): 판정이 없어도 대상이 유효하게 관측되지 않으면(UNKNOWN·PREDICTED·트랙 없음·신원
   불확실·gap 초과) 해소 확인을 처음부터 다시 세고 검증 상태만 UNVERIFIABLE 로 둔다. 다시 관측되면 그때부터
   해소 유지 시간을 센다(해소 확인 재시작). 어떤 규칙의 이벤트든 같다.
2. ObservationLossHold(이 모듈): 구역 규칙을 감싸, UNKNOWN 이 된 사람이 마지막 위치 기준으로 구역 안(불확실성
   범위 포함)이면 그 규칙 이름으로 holding 판정을 더한다. holding 은 활성 이벤트를 유지만 하고 새 이벤트를 만들거나
   등급을 올리거나 내리지 않는다(lifecycle). 그래서 원래 규칙의 이벤트에
   - 확인 필요(confidence_reasons=PREDICTED, needs_check=True)와 근거(observation_lost, last_seen_in_zone)가 붙고,
   - 구역(zone_id)은 원래 이벤트의 구역으로 남는다. 2σ 원이 여러 구역에 걸치면 구역마다 holding 을 내고,
     lifecycle 이 이벤트의 현재 구역과 같은 holding 을 먼저 고른다. 다른 구역의 holding 만 있어도(마지막 추정이
     이웃 구역 쪽으로 밀린 경우) lifecycle 은 holding 으로 구역·등급·수치를 바꾸지 않고 유지·확인 필요만 반영한다.
     그래서 관측을 잃은 사람이 들어간 적 없는 이웃 구역으로 옮겨 가지 않는다.
   감싼 규칙이 그 사람·구역에 이미 판정을 냈으면 더하지 않는다(원래 규칙의 판정이 우선).

R6 이 원래 규칙 이름으로 판정을 내는 이유: 엔진은 규칙이 자기 ID·버전의 판정만 내게 하고(RuleContractError),
lifecycle 은 (규칙, 사람) 키로 이벤트를 유지한다. 원래 규칙의 이벤트를 유지하려면 그 규칙 이름의 holding 이어야 한다.
R6 고유 ID 의 이벤트는 만들지 않는다(같은 사람에게 경보가 두 번 울리지 않게 한다).

쓰는 법(구역 규칙 담당 이슈, 예: S15P31A101-71 R1):
    registry.register("R1", 1, lambda: ObservationLossHold(ZoneEntryRule()))

- '마지막으로 보인 위치'는 world_state 의 position_xy(UNKNOWN 이면 마지막 추정)와 position_xy_sigma 다.
  구역 다각형까지의 수평 거리가 sigma_k × max(sigma) 이하이면 구역 안으로 본다. holding 은 해소를 막을 뿐
  새 경보를 내지 않으므로 넓게 잡아도 오경보가 생기지 않는다.
- VOLUME_3D 구역도 XY 로만 본다. UNKNOWN 이면 높이를 모르므로 높이로 구역 밖이라고 확정하지 않는다.
- 트랙이 world_state 에서 사라진 사람은 위치가 없어 holding 을 내지 못한다. lifecycle 이 해소하지 않고 UNVERIFIABLE 로 둔다.
"""

import math
from collections.abc import Iterator
from dataclasses import replace

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import ConfidenceReason, Severity
from simai_ai.risk.rule import Finding, Rule, RuleContext, RuleSpec
from simai_ai.risk.world import ObservationState, WorldTrack
from simai_ai.risk.zone import Zone

R6_RULE_ID = "R6"  # 명세 7.2 의 규칙 번호. 이벤트의 rule_id 는 감싼 규칙의 ID 다.

REASON_OBSERVATION_LOST = "observation_lost"
REASON_LAST_SEEN_IN_ZONE = "last_seen_in_zone"

_R6_READS = ("tracks.observation_state", "tracks.position_xy", "tracks.position_xy_sigma", "zones")


class ObservationLossHold:
    """구역 규칙에 R6(관측 상실 시 원래 규칙 유지)을 붙인다.

    - inner: 구역(RuleContext.zones)으로 사람을 판정하는 규칙.
    - sigma_k: 마지막 위치 불확실성 배수(초기값 2, 수평 약 95%).
    - severity: holding 판정의 등급. None 이면 구역 등급. lifecycle 은 holding 으로 이벤트 등급을 바꾸지 않으므로(올리지도 내리지도 않음) 근거 표시용이다.
    """

    def __init__(self, inner: Rule, *, sigma_k: float = 2.0, severity: Severity | None = None) -> None:
        if isinstance(sigma_k, bool) or not isinstance(sigma_k, int | float) or not math.isfinite(sigma_k):
            raise RiskInputError("R6.sigma_k 는 유한한 수여야 한다")
        if sigma_k < 0:
            raise RiskInputError("R6.sigma_k 는 0 이상이어야 한다")
        self._inner = inner
        self._sigma_k = float(sigma_k)
        self._severity = None if severity is None else Severity(severity)
        reads = inner.spec.reads + tuple(r for r in _R6_READS if r not in inner.spec.reads)
        self._spec = replace(inner.spec, reads=reads)

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    @property
    def inner(self) -> Rule:
        return self._inner

    def evaluate(self, ctx: RuleContext) -> Iterator[Finding]:
        judged: set[tuple[tuple[str, ...], str | None]] = set()
        for finding in self._inner.evaluate(ctx):
            judged.add((finding.world_ids, finding.zone_id))
            yield finding
        for track in sorted(ctx.world.tracks, key=lambda t: t.world_id):
            if track.observation_state is not ObservationState.UNKNOWN:
                continue
            for zone in ctx.zones:
                if ((track.world_id,), zone.zone_id) in judged:
                    continue
                distance = self._distance_to_zone(track, zone)
                if distance is None:
                    continue
                yield Finding(
                    rule_id=self._spec.rule_id,
                    rule_version=self._spec.rule_version,
                    severity=zone.severity if self._severity is None else self._severity,
                    world_ids=(track.world_id,),
                    zone_id=zone.zone_id,
                    # 위치는 관측이 아니라 마지막 추정값이다(7.1 PREDICTED).
                    confidence_reasons=(ConfidenceReason.PREDICTED,),
                    reasons=(REASON_OBSERVATION_LOST, REASON_LAST_SEEN_IN_ZONE),
                    # 관측 상실 경과(gap)는 매 판정 바뀌어 수치 갱신 전이를 1초마다 만들므로 싣지 않는다.
                    # 경과는 전이 근거(TrackEvidence.t_last_valid)와 observation_time 으로 알 수 있다.
                    metrics={"zone_distance_m": distance},
                    holding=True,
                )

    def _distance_to_zone(self, track: WorldTrack, zone: Zone) -> float | None:
        """마지막 위치가 구역 안(불확실성 범위 포함)이면 다각형까지 거리(안이면 0), 아니면 None."""
        point = track.position_xy
        if point_in_polygon(point, zone.polygon_xy):
            return 0.0
        distance = distance_to_polygon(point, zone.polygon_xy)
        margin = self._sigma_k * max(abs(s) for s in track.position_xy_sigma)
        return distance if distance <= margin else None


def point_in_polygon(point: tuple[float, ...], polygon: tuple[tuple[float, ...], ...]) -> bool:
    """수평 다각형 안(경계 포함)인지. 짝수-홀수 규칙."""
    if distance_to_polygon(point, polygon) == 0.0:
        return True
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def distance_to_polygon(point: tuple[float, ...], polygon: tuple[tuple[float, ...], ...]) -> float:
    """점에서 다각형 경계까지의 수평 거리(m)."""
    x, y = point
    best = math.inf
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length2))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best
