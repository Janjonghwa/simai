"""R2·R3 공용: 높이 프로파일(운영·교실 시험), 위험 가장자리, 서 있는 면 (명세 7.2 R2·R3, 7.4 교실 시험, 7.5, 5.2).

**높이 프로파일(7.4).** R2·R3 의 높이차 기준은 운영 2 m(7.2: 국내 산업안전 기준에서 추락 방지 조치를 요구하는 높이로 흔히
쓰는 값)다. 교실은 40 cm 단상으로 시험하고, 시험 때만 높이 임계값을 단상 높이에 맞춘다. 규칙 논리는 같다. 시험용 임계값
세트는 별도 rule_version(시험 프로파일)으로 두고 화면에 표시한다(7.4, 부록 E). 그래서 프로파일은 규칙 버전으로만 고르고
(등록부, 구성 묶음 rules 구성 요소의 rule_versions) 실행 중에 바꾸지 않는다.
- 운영: `OPERATIONAL_HEIGHT_PROFILE` 2.0 m, 규칙 버전 = 기본 버전(예: R2 v1).
- 교실 시험: `CLASSROOM_TEST_PROFILE` 0.35 m, 규칙 버전 = 기본 버전 + `TEST_PROFILE_VERSION_OFFSET`(1000, 예: R2 v1001).
  단상 높이 0.40 m 에 맞추되 상판 높이 등록 허용오차(G4 ±5 cm)만큼 낮췄다(등록 높이가 0.38 m 로 들어와도 검출한다).
  40 cm 단상 위·책상(0.7 m 안팎) 위가 모두 고소 위치가 된다. 이 값은 운영 안전 기준이 아니다.
- R3 의 '교실은 바닥 기준으로 단순화'(7.2)는 교실 시험 프로파일에만 쓴다(`floor_reference=True`). 운영 R3 는 서 있는 면과
  인접한 낮은 면의 높이차(서 있는 층 위험 가장자리의 높이차)로 판정한다(7.2, FR-68).
버전 번호만 보고 시험 프로파일인지 알 수 있다(`is_test_profile_version`). 화면 표시는 그 값을 쓴다.

**위험 가장자리(7.5).** R2·R3 의 가장자리는 등록 작업면 OBB 윗면의 둘레이고, 높이차는 상판 높이와 인접한 낮은 면(바닥 또는
다른 작업면)의 차이다. 변마다 바깥쪽으로 `ADJACENT_PROBE_M`(5 cm) 떨어진 선을 따라 다른 작업면 footprint 에 드는 구간을 잘라,
구간마다 그 밖에서 가장 높은 면(겹친 작업면 중 최고 상판, 없으면 바닥 0)을 인접 면으로 본다. 높이차가 기준 이상인 구간만
위험 가장자리다. 그래서
- 같은 높이로 맞붙은 단상 두 개의 맞닿은 변, 더 높은 작업면에 덮인 변은 위험 가장자리가 아니다.
- 낮은 작업면에 일부만 닿은 변은 닿은 구간과 바닥으로 떨어지는 구간이 나뉜다. 작업면은 회전(yaw)해도 된다.
- 5 cm 보다 넓은 틈은 바닥으로 본다(안전 쪽). 5 cm 는 발이 빠지지 않는 틈으로 잡은 초기값이다(명세에 수치 없음).
  같은 5 cm 를 기하 허용오차로 쓴다: 바깥 선을 5 cm 밖에서 재므로 회전한 작업면이 비스듬히 겹치면 위험 구간의 끝이 실제 교점에서
  최대 약 5 cm 어긋날 수 있다(R2 발생 거리 1.0 m 에 비해 작고, 판정 범위에는 점유 반경 0.3 m 가 더해진다).
안전 난간 정보는 작업면 형식에 없어 모든 가장자리를 난간 없는 것으로 본다(7.2 R3 '안전 난간 없는 구역', 안전 쪽).

**서 있는 면(5.2).** 높이 규칙은 ground_anchor_xyz 의 Z(서 있는 면 높이)를 쓰고, 없으면 '확인 필요'로 처리한다.
- KNOWN: 접지점이 있고 z_validity 가 VALID. ContactEstimator 가 이번 시각 발목 근거로 그 표면을 확인했다
  (contact_validity 가 UNCERTAIN 이어도 걸으며 같은 표면 높이를 관측한 것이라 높이는 관측값이다).
- HELD: 접지점이 있지만 z_validity 가 VALID 가 아님(UNCERTAIN). 가려진 동안 마지막 유효 지지 표면을 유지한 값이다
  (ContactEstimator surface_source inferred, 5.2 '고소 위치였던 사람이 가려지면 마지막 유효 높이를 유지'). 판정에 쓰되 확인 필요
  이유 PREDICTED(위치가 관측이 아니라 마지막 추정값, 7.1·계약 confidence_reason)를 붙인다. world_state 계약에
  surface_source 가 들어오면 그 값(inferred)으로 바로 가린다.
- UNKNOWN: 접지점이 없다(지지 표면 미확인, z_validity UNAVAILABLE). Z 를 0 으로 채우지 않는다(AGENTS 규칙 2). 규칙은 수평
  불확실성 범위가 걸치는 작업면 위에 있을 수 있다고 보고 '확인 필요'(Z_UNKNOWN)로 판정한다(7.1 '불확실성 범위가 조건에 조금이라도
  걸리면' 발생, R1 VOLUME_3D 와 같은 처리). 단, VALID 관측 골반(양쪽 엉덩이가 VALID·observed, 5.2 관절 유효성 게이트)이 그 상판보다
  `PELVIS_BELOW_TOP_MARGIN_M`(5 cm) 넘게 낮으면 그 위에 서 있을 수 없으므로(깊이 쪼그려도 골반은 디딘 면 위에 있다) 후보에서 뺀다.
  책상에 앉아 다리가 가려진 사람이 책상 상판 위 고소 위치로 판정되는 것을 막는다. 골반이 VALID 가 아니거나 없으면 빼지 않는다
  (모르면 '위에 없음'으로 확정하지 않음).
같은 높이(±`LEVEL_TOLERANCE_M`)의 작업면 중 지지 표면 ID 가 같거나 수평 범위가 footprint 에 걸치는 것을 씨앗으로 잡고, 씨앗과
**이어진 같은 층 작업면**(같은 높이이고 footprint 사이 틈이 `ADJACENT_PROBE_M` 이하인 것을 잇달아 모은 묶음)을 모두 '서 있는 층'으로
본다. 한 상판을 OBB 하나로 등록하든 여러 개로 나눠 등록하든 같은 외곽 가장자리를 보게 한다(나눈 OBB 의 맞닿은 이음새는 위험
가장자리가 아니고, 사람이 닿지 않은 다른 OBB 의 바깥 변은 위험 가장자리다). 틈이 5 cm 보다 넓은 작업면은 잇지 않는다(그 틈의 변은
양쪽 모두 위험 가장자리라 거리가 줄지 않는다). UNKNOWN 의 후보 작업면도 같은 방식으로 묶음 전체로 넓힌다.
유지 중(HELD) 접지점의 σz 증가로 서 있는 층을 넓히지 않는다. 유지가 맞는지(골반 높이·footprint·유지 상한)는 ContactEstimator 가
보고 모순이면 유지를 끊어 UNKNOWN 으로 넘긴다. σz 로 넓히면 다리가 단상에 가려진 채 옆에 선 바닥 사람이 1~2초 만에 고소
위치·가장자리 경보를 받는다.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.work_surface import FLOOR_ID, FLOOR_Z_M, WorkSurface
from simai_ai.risk.world import JointLayout, Validity, WorldTrack
from simai_ai.risk.zone_geometry import BOUNDARY_TOLERANCE_M, HorizontalRange, region_distance

TEST_PROFILE_VERSION_OFFSET = 1000
"""시험 프로파일 규칙 버전 = 기본 버전 + 이 값 (7.4 '별도 rule_version')"""
OPERATIONAL_HEIGHT_THRESHOLD_M = 2.0
"""운영 높이차 기준 (명세 7.2 R2·R3 '2m 이상')"""
CLASSROOM_TEST_HEIGHT_THRESHOLD_M = 0.35
"""교실 시험 높이차 기준. 40 cm 단상(7.4)에 맞추고 상판 높이 등록 허용오차 5 cm(G4)만큼 낮춘 값. 운영 기준이 아니다"""
ADJACENT_PROBE_M = 0.05
"""가장자리 바깥 인접 면을 찾는 거리. 이보다 좁은 틈은 맞붙은 것으로 본다(초기값)"""
LEVEL_TOLERANCE_M = 0.05
"""접지점 Z 와 상판 높이가 이 안이면 같은 층으로 본다(ContactEstimator 접지점 Z 는 등록 상판 높이 그대로다)"""
HEIGHT_TOLERANCE_M = 1e-9
"""높이차 비교의 부동소수점 여유. 기준과 같은 높이차는 기준 이상으로 본다(안전 쪽)"""
PELVIS_BELOW_TOP_MARGIN_M = 0.05
"""접지점을 모를 때 VALID 골반이 상판보다 이만큼 넘게 낮으면 그 작업면 위에 있지 않다고 본다(삼각측량 오차 여유, 초기값)"""
COCO17_HIPS = (11, 12)
"""COCO17 왼쪽·오른쪽 엉덩이 (world.JointLayout.COCO17 순서)"""


@dataclass(frozen=True)
class HeightProfile:
    """높이 규칙 임계값 세트 (7.4). test=True 면 시험 프로파일이고 규칙 버전에 TEST_PROFILE_VERSION_OFFSET 을 더한다.

    floor_reference: R3 높이차를 바닥(0) 기준으로 단순화하는지(7.2 '교실은 바닥 기준으로 단순화'). 교실 시험 프로파일만 True 다.
    False 면 서 있는 층의 인접한 낮은 면과의 높이차로 판정한다.
    """

    name: str
    height_threshold_m: float
    test: bool
    floor_reference: bool = False

    def __post_init__(self) -> None:
        value = self.height_threshold_m
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
            raise RiskInputError(f"HeightProfile {self.name}: height_threshold_m 는 양의 유한한 수여야 한다")

    def rule_version(self, base_version: int) -> int:
        return base_version + TEST_PROFILE_VERSION_OFFSET if self.test else base_version


OPERATIONAL_HEIGHT_PROFILE = HeightProfile("operational", OPERATIONAL_HEIGHT_THRESHOLD_M, test=False)
CLASSROOM_TEST_PROFILE = HeightProfile(
    "classroom-test-podium-40cm", CLASSROOM_TEST_HEIGHT_THRESHOLD_M, test=True, floor_reference=True
)


def is_test_profile_version(rule_version: int) -> bool:
    """규칙 버전이 시험 프로파일(7.4)인지. 화면의 '시험 프로파일(rule_version)' 표시가 쓴다."""
    return rule_version > TEST_PROFILE_VERSION_OFFSET


@dataclass(frozen=True)
class HazardEdge:
    """위험 가장자리 한 구간: 작업면 surface_id 윗면 둘레의 선분 start → end 와 그 바깥 인접 면과의 높이차."""

    surface_id: str
    start: tuple[float, float]
    end: tuple[float, float]
    height_diff_m: float
    adjacent_surface_id: str

    @property
    def segment(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """zone_geometry 가 다각형으로 받는 두 점(선분). 짝수-홀수 규칙상 안쪽이 없어 선분까지의 거리만 잰다."""
        return (self.start, self.end)


def hazard_edges(
    surfaces: Sequence[WorkSurface],
    threshold_m: float,
    *,
    probe_m: float = ADJACENT_PROBE_M,
    tolerance_m: float = HEIGHT_TOLERANCE_M,
) -> tuple[HazardEdge, ...]:
    """등록 작업면들의 위험 가장자리(높이차 ≥ threshold_m 인 윗면 둘레 구간). 작업면 ID·변 순서로 결정적이다."""
    ordered = sorted(surfaces, key=lambda s: s.surface_id)
    out: list[HazardEdge] = []
    for surface in ordered:
        others = [o for o in ordered if o.surface_id != surface.surface_id]
        corners = surface.footprint
        for i in range(4):
            a, b = corners[i], corners[(i + 1) % 4]
            ex, ey = b[0] - a[0], b[1] - a[1]
            length = math.hypot(ex, ey)
            nx, ny = ey / length, -ex / length  # 반시계 다각형의 바깥쪽(진행 방향 오른쪽)
            pa = (a[0] + nx * probe_m, a[1] + ny * probe_m)
            pb = (b[0] + nx * probe_m, b[1] + ny * probe_m)
            breaks = {0.0, 1.0}
            for other in others:
                interval = _clip_to_surface(pa, pb, other)
                if interval is not None:
                    breaks.update(interval)
            ts = sorted(breaks)
            pieces: list[tuple[float, float, float, str]] = []  # (t0, t1, 높이차, 인접 면)
            for t0, t1 in pairwise(ts):
                if (t1 - t0) * length <= BOUNDARY_TOLERANCE_M:
                    continue
                tm = (t0 + t1) / 2.0
                probe = (pa[0] + (pb[0] - pa[0]) * tm, pa[1] + (pb[1] - pa[1]) * tm)
                covering = [o for o in others if o.contains_xy(probe)]
                if covering:
                    top = max(covering, key=lambda o: (o.top_height_m, o.surface_id))
                    adjacent_z, adjacent_id = top.top_height_m, top.surface_id
                else:
                    adjacent_z, adjacent_id = FLOOR_Z_M, FLOOR_ID
                diff = surface.top_height_m - adjacent_z
                if diff < threshold_m - tolerance_m:
                    continue
                if pieces and pieces[-1][1] == t0 and pieces[-1][2:] == (diff, adjacent_id):
                    pieces[-1] = (pieces[-1][0], t1, diff, adjacent_id)
                else:
                    pieces.append((t0, t1, diff, adjacent_id))
            for t0, t1, diff, adjacent_id in pieces:
                out.append(
                    HazardEdge(
                        surface_id=surface.surface_id,
                        start=(a[0] + ex * t0, a[1] + ey * t0),
                        end=(a[0] + ex * t1, a[1] + ey * t1),
                        height_diff_m=diff,
                        adjacent_surface_id=adjacent_id,
                    )
                )
    return tuple(out)


def _clip_to_surface(
    p: tuple[float, float], q: tuple[float, float], surface: WorkSurface
) -> tuple[float, float] | None:
    """선분 p → q 중 작업면 footprint(경계 포함) 안에 드는 매개변수 구간 [t0, t1] (Liang–Barsky). 없으면 None."""
    pu, pv = surface.local_xy(p)
    qu, qv = surface.local_xy(q)
    du, dv = qu - pu, qv - pv
    hx, hy = surface.size_xy[0] / 2.0, surface.size_xy[1] / 2.0
    t0, t1 = 0.0, 1.0
    for d, start, low, high in ((du, pu, -hx, hx), (dv, pv, -hy, hy)):
        if d == 0.0:
            if start < low or start > high:
                return None
            continue
        ta, tb = (low - start) / d, (high - start) / d
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1:
            return None
    return (t0, t1)


class StandingBasis(StrEnum):
    """서 있는 면을 아는 정도 (머리말 '서 있는 면')."""

    KNOWN = "KNOWN"
    HELD = "HELD"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Standing:
    """한 사람의 서 있는 면.

    - KNOWN·HELD: z = 접지점 Z(서 있는 면 높이), surfaces = 서 있는 층(씨앗과 이어진 같은 높이 작업면 묶음. 바닥이면 보통
      비어 있음).
    - UNKNOWN: z = None, surfaces = 수평 불확실성 범위가 footprint 에 걸치는 작업면과 그와 이어진 같은 층 작업면(그 위에 있을 수 있음).
    """

    basis: StandingBasis
    z: float | None
    surfaces: tuple[WorkSurface, ...]


def standing(
    track: WorldTrack,
    surfaces: Iterable[WorkSurface],
    ranges: Sequence[HorizontalRange],
    *,
    level_tolerance_m: float = LEVEL_TOLERANCE_M,
    tolerance_m: float = BOUNDARY_TOLERANCE_M,
) -> Standing:
    """접지점과 수평 불확실성 범위(R1 정의, r1_zone_entry.horizontal_ranges)로 서 있는 면을 정한다."""

    def overlaps(surface: WorkSurface) -> bool:
        return any(r.overlaps(surface.footprint, tolerance_m=tolerance_m) for r in ranges)

    surfaces = tuple(surfaces)
    anchor = track.ground_anchor_xyz
    if anchor is None:
        pelvis_z = valid_pelvis_z(track)
        seeds = [
            s
            for s in surfaces
            if overlaps(s) and (pelvis_z is None or pelvis_z >= s.top_height_m - PELVIS_BELOW_TOP_MARGIN_M)
        ]
        return Standing(
            StandingBasis.UNKNOWN, None, connected_level(seeds, surfaces, level_tolerance_m=level_tolerance_m)
        )
    z = anchor[2]
    basis = StandingBasis.KNOWN if track.z_validity is Validity.VALID else StandingBasis.HELD
    seeds = [
        s
        for s in surfaces
        if abs(s.top_height_m - z) <= level_tolerance_m and (s.surface_id == track.support_surface_id or overlaps(s))
    ]
    return Standing(basis, z, connected_level(seeds, surfaces, level_tolerance_m=level_tolerance_m))


def connected_level(
    seeds: Iterable[WorkSurface],
    surfaces: Sequence[WorkSurface],
    *,
    level_tolerance_m: float = LEVEL_TOLERANCE_M,
    probe_m: float = ADJACENT_PROBE_M,
) -> tuple[WorkSurface, ...]:
    """씨앗 작업면과 이어진 같은 층 작업면을 모두 모은다(입력 순서 유지).

    두 작업면은 상판 높이 차가 level_tolerance_m 이하이고 footprint 사이 틈이 probe_m 이하(겹침·맞닿음 포함)면 이어졌다고 본다.
    hazard_edges 가 틈 probe_m 이하의 맞닿은 변을 위험 가장자리에서 빼는 기준과 같다. 이어짐은 잇달아 전해진다(L·U 자 묶음).
    """
    chosen = {s.surface_id for s in seeds}
    queue = [s for s in surfaces if s.surface_id in chosen]
    while queue:
        current = queue.pop()
        for other in surfaces:
            if other.surface_id in chosen:
                continue
            if abs(other.top_height_m - current.top_height_m) > level_tolerance_m:
                continue
            if region_distance(current.footprint, other.footprint) <= probe_m + BOUNDARY_TOLERANCE_M:
                chosen.add(other.surface_id)
                queue.append(other)
    return tuple(s for s in surfaces if s.surface_id in chosen)


def valid_pelvis_z(track: WorldTrack) -> float | None:
    """양쪽 엉덩이가 모두 VALID 인 Tier 0 관측(observed)일 때 골반(중점) Z. 아니면 None(추정 관절·UNCERTAIN 은 쓰지 않음)."""
    skeleton = track.skeleton
    if skeleton is None or skeleton.joint_layout is not JointLayout.COCO17 or len(skeleton.joints) <= max(COCO17_HIPS):
        return None
    zs = []
    for index in COCO17_HIPS:
        joint = skeleton.observed_joint(index)
        if joint is None or skeleton.validity[index] is not Validity.VALID:
            return None
        zs.append(joint[2])
    return sum(zs) / len(zs)
