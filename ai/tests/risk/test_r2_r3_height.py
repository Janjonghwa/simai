"""R2 가장자리 근접·R3 고소 위치 (명세 7.2 R2·R3, 7.5, 5.2, 7.1, 7.4, 6.4, 15.1 G6).

단위 시험과 보고 전 자기 반례 시험(경계 거리, 단상 위 가림 유지(HELD), 접지점이 사라질 때 활성 이벤트 유지, 작업면 겹침·회전,
바닥 사람)을 함께 둔다. 반례 시험 일부는 핵심 처리를 일부러 끈 음성 대조(monkeypatch)로 시험이 그 처리에 실제로 걸리는지 확인한다.
"""

import math

import pytest

from simai_ai.config.bundle import freeze_json
from simai_ai.contact.surfaces import SurfaceSet
from simai_ai.risk import (
    CLASSROOM_TEST_PROFILE,
    OPERATIONAL_HEIGHT_PROFILE,
    Channel,
    Confidence,
    EdgeProximityRule,
    ElevatedPositionRule,
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    Origin,
    RiskEngine,
    RuleContext,
    RuleContractError,
    Severity,
    WorkSurface,
    WorldState,
    channels_for,
    default_registry,
    work_surfaces_from_operational_geometry,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import ConfidenceReason
from simai_ai.risk.rules import height_common
from simai_ai.risk.rules.height_common import (
    TEST_PROFILE_VERSION_OFFSET,
    HeightProfile,
    StandingBasis,
    hazard_edges,
    is_test_profile_version,
    standing,
)
from simai_ai.risk.rules.r1_zone_entry import horizontal_ranges
from simai_ai.risk.rules.r2_edge_proximity import (
    REASON_EDGE_CLEAR,
    REASON_EDGE_RAISE,
    REASON_STANDING_HELD,
    REASON_STANDING_ON_SURFACE,
    REASON_Z_UNKNOWN_ON_SURFACE,
)
from simai_ai.risk.rules.r3_elevated_position import (
    REASON_STANDING_ELEVATED,
    REASON_Z_UNKNOWN_ON_ELEVATED,
)
from simai_ai.risk.rules.r3_elevated_position import REASON_STANDING_HELD as R3_REASON_HELD

T0 = 1760342400100
SIGMA = 0.05  # 범위 반경 = 2·0.05 + 0.3 = 0.4 m

PODIUM = {"surface_id": "podium-1", "top_height_m": 0.4, "center_xy": [4.5, 6.75], "size_xy": [4.0, 1.1]}
# footprint x 2.5~6.5, y 6.2~7.3


def surface(**overrides) -> WorkSurface:
    return WorkSurface.from_json(PODIUM | overrides)


def track(
    xy=(4.5, 6.75),
    *,
    anchor="podium",
    world_id="w-1",
    state="OBSERVED",
    cameras=("cam1", "cam3"),
    sigma=SIGMA,
    z_validity=None,
    contact_validity="VALID",
    support_id=None,
    identity="HIGH",
    t_last_valid=T0,
):
    """anchor: "podium"(0.4 m 상판), "floor", None(접지 미확인), 또는 [x, y, z]."""
    if anchor == "podium":
        anchor_xyz, sid = [xy[0], xy[1], 0.4], "podium-1"
    elif anchor == "floor":
        anchor_xyz, sid = [xy[0], xy[1], 0.0], "floor"
    elif anchor is None:
        anchor_xyz, sid = None, None
    else:
        anchor_xyz, sid = list(anchor), "podium-1"
    sid = support_id if support_id is not None else sid
    known = anchor_xyz is not None
    return {
        "world_id": world_id,
        "t_last_valid": t_last_valid,
        "observation_state": state,
        "track_lifecycle": "CONFIRMED",
        "identity_confidence": identity,
        "identity_revision": 0,
        "position_xy": list(xy),
        "position_xy_sigma": [sigma, sigma],
        "pelvis_xyz": [xy[0], xy[1], 1.3],
        "ground_anchor_xyz": anchor_xyz,
        "ground_anchor_sigma": [sigma, sigma, 0.02] if known else None,
        "support_surface_id": sid,
        "support_surface_validity": "VALID" if known else "UNCERTAIN",
        "contact_validity": contact_validity if known else "UNCERTAIN",
        "z_validity": (z_validity or "VALID") if known else "UNAVAILABLE",
        "head_z": 2.1,
        "cameras": list(cameras),
    }


def world(*tracks, t=T0) -> WorldState:
    return WorldState.from_payload(
        {"t_latest_global": t, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": list(tracks)}
    )


def ctx(*tracks, surfaces=(), t=T0) -> RuleContext:
    return RuleContext(now_ms=t, world=world(*tracks, t=t), zones=(), work_surfaces=tuple(surfaces))


def r2(profile=CLASSROOM_TEST_PROFILE, **kw):
    return EdgeProximityRule(profile=profile, **kw)


def r3(profile=CLASSROOM_TEST_PROFILE, **kw):
    return ElevatedPositionRule(profile=profile, **kw)


# --- 작업면 형식 ----------------------------------------------------------------


def test_footprint_rotation_and_contains():
    s = surface(center_xy=[0.0, 0.0], size_xy=[2.0, 1.0], yaw_rad=math.pi / 2)
    corners = s.footprint
    # 90도 돌리면 가로 2 m 가 y 축으로 간다(반시계 순서 유지)
    assert [tuple(round(v, 9) for v in c) for c in corners] == [(0.5, -1.0), (0.5, 1.0), (-0.5, 1.0), (-0.5, -1.0)]
    assert s.contains_xy((0.4, 0.9)) and not s.contains_xy((0.9, 0.4))
    assert s.contains_xy((0.5, 1.0))  # 경계 포함


@pytest.mark.parametrize(
    "bad",
    [
        {"top_height_m": 0.0},
        {"top_height_m": -0.1},
        {"size_xy": [0.0, 1.0]},
        {"surface_id": "floor"},
        {"surface_id": ""},
        {"center_xy": [1.0]},
        {"yaw_rad": float("nan")},
        {"top_height_sigma_m": -0.01},
    ],
)
def test_work_surface_rejects_bad_values(bad):
    with pytest.raises(RiskInputError):
        surface(**bad)


def test_operational_geometry_drops_same_items_as_contact_estimator():
    """R2·R3 와 ContactEstimator 가 같은 작업면 집합을 본다(한쪽만 빼면 단상 위 사람의 판정 근거가 어긋난다)."""
    geometry = {
        "version": "geom-3",
        "work_surfaces": [
            PODIUM,
            PODIUM | {"surface_id": "desk-1", "top_height_m": 0.75, "center_xy": [1.0, 1.0], "yaw_rad": 0.3},
            PODIUM | {"surface_id": "podium-1"},  # 겹치는 ID
            PODIUM | {"surface_id": "floor"},
            PODIUM | {"surface_id": "low", "top_height_m": 0.0},
            {"surface_id": "no-size", "top_height_m": 0.4, "center_xy": [0, 0]},
            "not-an-object",
        ],
    }
    # 구성 묶음은 읽기 전용 JSON(Mapping·tuple)으로 보관된다(config.bundle.freeze_json). 두 형태 모두 같은 결과여야 한다.
    for doc in (geometry, freeze_json(geometry)):
        mine = work_surfaces_from_operational_geometry(doc)
        theirs = SurfaceSet.from_operational_geometry(doc)
        assert [s.surface_id for s in mine.surfaces] == [s.surface_id for s in theirs.work_surfaces]
        assert [s.surface_id for s in mine.surfaces] == ["podium-1", "desk-1"]
        assert [label for label, _ in mine.rejected] == [label for label, _ in theirs.rejected]
        assert mine.geometry_version == "geom-3"


def test_engine_rejects_duplicate_surface_ids():
    engine = RiskEngine([r3()], FixedClock(T0))
    with pytest.raises(RuleContractError):
        engine.evaluate(world(track()), [], Origin.LIVE, work_surfaces=[surface(), surface()])


# --- 프로파일 ------------------------------------------------------------------


def test_profiles_and_versions():
    assert OPERATIONAL_HEIGHT_PROFILE.height_threshold_m == 2.0  # 명세 7.2
    assert not OPERATIONAL_HEIGHT_PROFILE.test
    assert CLASSROOM_TEST_PROFILE.test and CLASSROOM_TEST_PROFILE.height_threshold_m < 0.4  # 40 cm 단상 검출
    assert r2(OPERATIONAL_HEIGHT_PROFILE).spec.rule_version == 1
    assert r2().spec.rule_version == 1 + TEST_PROFILE_VERSION_OFFSET
    assert r3().spec.rule_version == 1001
    assert is_test_profile_version(1001) and not is_test_profile_version(1)
    with pytest.raises(RiskInputError):
        HeightProfile("bad", 0.0, test=True)
    registry = default_registry()
    for rule_id in ("R2", "R3", "R5"):
        assert registry.create(rule_id, 1).profile is OPERATIONAL_HEIGHT_PROFILE
        assert registry.create(rule_id, 1001).profile is CLASSROOM_TEST_PROFILE


@pytest.mark.parametrize("top", [0.40, 0.66])
def test_r3_bundle_rule_version_decides_low_work_surface(top):
    """S15P31A101-654(14차 로비): 0.40~0.66 m 작업면은 묶음이 R3 v1001(현장 기준 교실 시험)을 실을 때만 발동한다.
    묶음 rules 의 rule_version 으로 등록부에서 고른 규칙 그대로 판정한다(운영 v1 은 2.0 m 기준이라 판정 없음)."""
    on_top = track(anchor=[4.5, 6.75, top])
    surfaces = [surface(top_height_m=top)]
    registry = default_registry()
    (f,) = registry.create("R3", 1001).evaluate(ctx(on_top, surfaces=surfaces))
    assert f.rule_version == 1001 and f.reasons == (REASON_STANDING_ELEVATED,)
    assert f.metrics["height_diff_m"] == pytest.approx(top)
    assert list(registry.create("R3", 1).evaluate(ctx(on_top, surfaces=surfaces))) == []


# --- 위험 가장자리 ---------------------------------------------------------------


def test_single_podium_edges_by_profile():
    edges = hazard_edges([surface()], CLASSROOM_TEST_PROFILE.height_threshold_m)
    assert len(edges) == 4
    assert all(e.adjacent_surface_id == "floor" and e.height_diff_m == pytest.approx(0.4) for e in edges)
    assert hazard_edges([surface()], OPERATIONAL_HEIGHT_PROFILE.height_threshold_m) == ()  # 40 cm 는 운영 고소 아님


def test_threshold_equal_height_is_hazardous():
    """경계값: 높이차가 기준과 같으면 위험 가장자리다(안전 쪽)."""
    assert len(hazard_edges([surface(top_height_m=0.35)], 0.35)) == 4
    assert hazard_edges([surface(top_height_m=0.349)], 0.35) == ()


def _edge_lengths(edges, surface_id):
    return sorted(round(math.dist(e.start, e.end), 6) for e in edges if e.surface_id == surface_id)


def test_abutting_same_height_seam_is_not_an_edge():
    a = surface(surface_id="a", center_xy=[1.0, 0.0], size_xy=[2.0, 1.0])
    b = surface(surface_id="b", center_xy=[3.0, 0.0], size_xy=[2.0, 1.0])
    edges = hazard_edges([a, b], 0.35)
    # 각자 맞닿은 변(x = 2.0)을 뺀 세 변만
    assert _edge_lengths(edges, "a") == [1.0, 2.0, 2.0]
    assert _edge_lengths(edges, "b") == [1.0, 2.0, 2.0]
    assert not any(abs(e.start[0] - 2.0) < 1e-9 and abs(e.end[0] - 2.0) < 1e-9 for e in edges)


@pytest.mark.parametrize(("gap", "seam_edges"), [(0.03, 0), (0.10, 2)])
def test_gap_wider_than_probe_counts_as_floor(gap, seam_edges):
    a = surface(surface_id="a", center_xy=[1.0, 0.0], size_xy=[2.0, 1.0])
    b = surface(surface_id="b", center_xy=[3.0 + gap, 0.0], size_xy=[2.0, 1.0])
    edges = hazard_edges([a, b], 0.35)
    seam = [e for e in edges if abs(e.start[0] - e.end[0]) < 1e-9 and 1.9 < e.start[0] < 2.2]
    assert len(seam) == seam_edges


def test_lower_step_splits_front_edge():
    """낮은 계단(0.2 m)이 단상 앞변 가운데 2 m 에 붙으면 그 구간은 높이차 0.2 로 위험 가장자리가 아니다."""
    step = surface(surface_id="step", top_height_m=0.2, center_xy=[4.5, 5.95], size_xy=[2.0, 0.5])
    edges = hazard_edges([surface(), step], 0.35)
    front = sorted(
        (
            e
            for e in edges
            if e.surface_id == "podium-1" and abs(e.start[1] - 6.2) < 1e-9 and abs(e.end[1] - 6.2) < 1e-9
        ),
        key=lambda e: min(e.start[0], e.end[0]),
    )
    assert [(round(min(e.start[0], e.end[0]), 6), round(max(e.start[0], e.end[0]), 6)) for e in front] == [
        (2.5, 3.5),
        (5.5, 6.5),
    ]
    # 계단 윗면(0.2 m)은 운영·시험 기준 모두 위험 가장자리가 없다
    assert not [e for e in edges if e.surface_id == "step"]
    # 높이차 기준을 0.2 로 낮추면 계단에 닿은 구간도 위험 가장자리(높이차 0.2, 인접 step)
    low = hazard_edges([surface(), step], 0.2)
    covered = [e for e in low if e.surface_id == "podium-1" and e.adjacent_surface_id == "step"]
    assert len(covered) == 1 and covered[0].height_diff_m == pytest.approx(0.2)


def test_higher_overlapping_surface_covers_edge():
    """더 높은 책상(0.75 m)이 단상 오른쪽 끝을 덮으면 덮인 단상 변 구간은 위험 가장자리가 아니고, 책상 변 중 단상 위로 떨어지는
    구간은 높이차 0.35 m 다(단상 위로 떨어짐)."""
    desk = surface(surface_id="desk", top_height_m=0.75, center_xy=[6.5, 6.75], size_xy=[1.0, 0.6])
    edges = hazard_edges([surface(), desk], 0.35)
    podium_right = [
        e for e in edges if e.surface_id == "podium-1" and abs(e.start[0] - 6.5) < 1e-9 and abs(e.end[0] - 6.5) < 1e-9
    ]
    lengths = sorted(round(math.dist(e.start, e.end), 6) for e in podium_right)
    assert lengths == [0.25, 0.25]  # 1.1 m 중 책상(0.6 m)에 덮인 구간을 뺀 위·아래
    desk_left = [
        e for e in edges if e.surface_id == "desk" and abs(e.start[0] - 6.0) < 1e-9 and abs(e.end[0] - 6.0) < 1e-9
    ]
    assert len(desk_left) == 1 and desk_left[0].adjacent_surface_id == "podium-1"
    assert desk_left[0].height_diff_m == pytest.approx(0.35)


def test_rotated_overlapping_surfaces():
    """회전한 작업면 둘이 같은 높이로 겹치면 겹친 쪽 변은 끊기고, 결과는 순서와 관계없이 같다."""
    a = surface(surface_id="a", center_xy=[0.0, 0.0], size_xy=[2.0, 2.0], yaw_rad=math.pi / 4)
    b = surface(surface_id="b", center_xy=[1.2, 0.0], size_xy=[2.0, 2.0], yaw_rad=math.pi / 4)
    edges = hazard_edges([a, b], 0.35)
    assert edges == hazard_edges([b, a], 0.35)
    # 각 변 끝이 다른 작업면 안이면 그 구간은 위험이 아니다
    for e in edges:
        mid = ((e.start[0] + e.end[0]) / 2, (e.start[1] + e.end[1]) / 2)
        other = b if e.surface_id == "a" else a
        assert not other.contains_xy(mid, margin_m=0.04)
    total = sum(math.dist(e.start, e.end) for e in edges)
    assert total < 16.0  # 겹친 둘레는 빠진다


# --- 서 있는 면 ------------------------------------------------------------------


def test_standing_basis():
    s = [surface()]
    t = world(track()).tracks[0]
    assert standing(t, s, horizontal_ranges(t)).basis is StandingBasis.KNOWN
    held = world(track(z_validity="UNCERTAIN", contact_validity="UNCERTAIN")).tracks[0]
    assert standing(held, s, horizontal_ranges(held)).basis is StandingBasis.HELD
    walking = world(track(contact_validity="UNCERTAIN")).tracks[0]  # 걸으며 같은 표면 높이 관측(z VALID)
    assert standing(walking, s, horizontal_ranges(walking)).basis is StandingBasis.KNOWN
    unknown = world(track(anchor=None)).tracks[0]
    st = standing(unknown, s, horizontal_ranges(unknown))
    assert st.basis is StandingBasis.UNKNOWN and st.z is None and [x.surface_id for x in st.surfaces] == ["podium-1"]


def test_far_same_height_surface_is_not_my_level():
    other = surface(surface_id="podium-2", center_xy=[4.5, 1.0])
    t = world(track()).tracks[0]
    st = standing(t, [surface(), other], horizontal_ranges(t))
    assert [s.surface_id for s in st.surfaces] == ["podium-1"]


# --- R3 ----------------------------------------------------------------------


def test_r3_on_podium():
    (f,) = r3().evaluate(ctx(track(), surfaces=[surface()]))
    assert f.rule_id == "R3" and f.rule_version == 1001 and f.severity is Severity.HIGH
    assert f.zone_id is None and not f.needs_check and not f.holding
    assert f.reasons == (REASON_STANDING_ELEVATED,)
    assert f.metrics["height_diff_m"] == pytest.approx(0.4)
    assert list(r3(OPERATIONAL_HEIGHT_PROFILE).evaluate(ctx(track(), surfaces=[surface()]))) == []


def test_r3_floor_person_beside_podium_is_not_elevated():
    """바닥 사람은 R3 아님: 범위가 단상 footprint 에 걸쳐도 접지점이 바닥이면 판정 없음."""
    beside = track(xy=(4.5, 6.0), anchor="floor")
    assert list(r3().evaluate(ctx(beside, surfaces=[surface()]))) == []
    # 바닥에 선 채 단상 footprint 안쪽 XY(OBB 가 보수적으로 큼)여도 접지점이 바닥이면 고소 아님
    assert list(r3().evaluate(ctx(track(xy=(4.5, 6.3), anchor="floor"), surfaces=[surface()]))) == []


def test_r3_held_on_podium_is_predicted():
    (f,) = r3().evaluate(ctx(track(z_validity="UNCERTAIN", contact_validity="UNCERTAIN"), surfaces=[surface()]))
    assert f.reasons == (R3_REASON_HELD,) and f.confidence_reasons == (ConfidenceReason.PREDICTED,)
    assert not f.holding and f.severity is Severity.HIGH  # 확인 필요여도 등급 유지(7.3)


def test_r3_walking_on_podium_is_not_needs_check():
    (f,) = r3().evaluate(ctx(track(contact_validity="UNCERTAIN"), surfaces=[surface()]))
    assert not f.needs_check


@pytest.mark.parametrize(
    ("xy", "expected"),
    [
        ((4.5, 6.75), True),  # footprint 안
        ((4.5, 6.2 - 0.4), True),  # 범위(0.4 m)가 앞변에 정확히 닿음
        ((4.5, 6.2 - 0.41), False),  # 1 cm 더 밖
        ((1.0, 1.0), False),
    ],
)
def test_r3_unknown_z_needs_check_on_elevated_footprint(xy, expected):
    findings = list(r3().evaluate(ctx(track(xy=xy, anchor=None), surfaces=[surface()])))
    if not expected:
        assert findings == []
        return
    (f,) = findings
    assert f.reasons == (REASON_Z_UNKNOWN_ON_ELEVATED,) and ConfidenceReason.Z_UNKNOWN in f.confidence_reasons
    assert not f.holding and f.metrics["height_diff_m"] == pytest.approx(0.4)


def test_r3_unknown_track_not_judged_and_track_reasons():
    assert list(r3().evaluate(ctx(track(state="UNKNOWN"), surfaces=[surface()]))) == []
    (f,) = r3().evaluate(
        ctx(track(state="PREDICTED", cameras=("cam1",), identity="IDENTITY_UNCERTAIN"), surfaces=[surface()])
    )
    assert f.confidence_reasons == (
        ConfidenceReason.PREDICTED,
        ConfidenceReason.SINGLE_VIEW,
        ConfidenceReason.IDENTITY_UNCERTAIN,
    )


def test_r3_no_surfaces_unknown_z_no_finding():
    """등록 작업면이 없으면 접지 미확인 사람을 고소 위치로 볼 근거가 없다."""
    assert list(r3().evaluate(ctx(track(anchor=None)))) == []


# --- R2 ----------------------------------------------------------------------

STAGE = {"surface_id": "stage", "top_height_m": 0.4, "center_xy": [3.0, 3.0], "size_xy": [6.0, 6.0]}


def stage_track(x, **kw):
    """가로 6 m 무대(왼쪽 변 x = 0) 가운데 높이에 선 사람. 왼쪽 변까지 거리 = x."""
    return track(xy=(x, 3.0), anchor=[x, 3.0, 0.4], support_id="stage", **kw)


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (1.4, "raise"),  # 범위 간격 = 1.4 − 0.4 = 1.0 (발생 경계 포함)
        (1.4 + 1e-6, "hold"),
        (1.6, "hold"),
        (1.7 - 1e-6, "hold"),
        (
            1.7 + 1e-6,
            None,
        ),  # 간격 1.3 m 이상이면 판정 없음. 정확히 1.7 은 1.7 − 0.4 의 부동소수점 끝자리로 유지(안전 쪽)
        (3.0, None),
    ],
)
def test_r2_boundary_distances(x, expected):
    findings = list(r2().evaluate(ctx(stage_track(x), surfaces=[WorkSurface.from_json(STAGE)])))
    if expected is None:
        assert findings == []
        return
    (f,) = findings
    assert f.severity is Severity.CRITICAL and f.zone_id is None
    assert f.holding is (expected == "hold")
    assert f.reasons == ((REASON_EDGE_CLEAR if expected == "hold" else REASON_EDGE_RAISE), REASON_STANDING_ON_SURFACE)
    assert f.metrics["edge_distance_m"] == pytest.approx(x)
    assert f.metrics["height_diff_m"] == pytest.approx(0.4)


def test_r2_on_small_podium_always_near_edge():
    (f,) = r2().evaluate(ctx(track(), surfaces=[surface()]))
    assert not f.holding and f.metrics["edge_distance_m"] == pytest.approx(0.55)
    assert list(r2(OPERATIONAL_HEIGHT_PROFILE).evaluate(ctx(track(), surfaces=[surface()]))) == []


def test_r2_floor_person_beside_edge_not_judged():
    assert list(r2().evaluate(ctx(track(xy=(4.5, 6.0), anchor="floor"), surfaces=[surface()]))) == []


def test_r2_unknown_z_on_footprint_needs_check():
    (f,) = r2().evaluate(ctx(track(anchor=None), surfaces=[surface()]))
    assert f.reasons == (REASON_EDGE_RAISE, REASON_Z_UNKNOWN_ON_SURFACE)
    assert f.confidence_reasons == (ConfidenceReason.Z_UNKNOWN,) and not f.holding
    assert list(r2().evaluate(ctx(track(xy=(4.5, 5.7), anchor=None), surfaces=[surface()]))) == []


def test_r2_held_is_predicted():
    (f,) = r2().evaluate(ctx(track(z_validity="UNCERTAIN", contact_validity="UNCERTAIN"), surfaces=[surface()]))
    assert f.reasons[1] == REASON_STANDING_HELD and f.confidence_reasons == (ConfidenceReason.PREDICTED,)


def test_r2_seam_between_same_height_stages_not_an_edge():
    a = WorkSurface.from_json(STAGE | {"surface_id": "a", "center_xy": [1.5, 2.0], "size_xy": [3.0, 4.0]})
    b = WorkSurface.from_json(STAGE | {"surface_id": "b", "center_xy": [4.5, 2.0], "size_xy": [3.0, 4.0]})
    on_seam = track(xy=(3.0, 2.0), anchor=[3.0, 2.0, 0.4], support_id="a")
    assert list(r2().evaluate(ctx(on_seam, surfaces=[a, b]))) == []  # 앞뒤 변까지 2.0 m → 간격 1.6 m


def test_r2_desk_person_uses_desk_edges_only():
    """단상(0.4) 옆 책상(0.75) 위 사람은 책상 가장자리만 본다. 책상 변 중 단상 쪽은 높이차 0.35 다."""
    desk = surface(surface_id="desk", top_height_m=0.75, center_xy=[7.0, 6.75], size_xy=[1.0, 1.1])
    person = track(xy=(7.2, 6.75), anchor=[7.2, 6.75, 0.75], support_id="desk")
    (f,) = r2().evaluate(ctx(person, surfaces=[surface(), desk]))
    assert f.metrics["edge_distance_m"] == pytest.approx(0.3)  # 오른쪽 변(x = 7.5)
    assert f.metrics["height_diff_m"] == pytest.approx(0.75)


# --- R5 가장자리 연결 --------------------------------------------------------------


def r5_track(xy, velocity, anchor, **kw):
    t = track(xy=xy, anchor=anchor, support_id="stage" if anchor not in (None, "floor") else None, **kw)
    t["velocity"] = list(velocity)
    return t


def test_r5_predicts_r2_edge_on_stage():
    s = WorkSurface.from_json(STAGE)
    rule = default_registry().create("R5", 1001)
    # 왼쪽 변까지 3.0 m, 1 m/s 로 다가감 → R2 발생 영역(범위 간격 1.0 m)까지 3.0 − 0.4 − 1.0 = 1.6 s
    (f,) = rule.evaluate(ctx(r5_track((3.0, 3.0), (-1.0, 0.0, 0.0), [3.0, 3.0, 0.4]), surfaces=[s]))
    assert f.rule_version == 1001 and f.zone_id is None and not f.holding
    assert f.reasons == ("predicted_entry", "target_edge")
    assert f.metrics["time_to_entry_s"] == pytest.approx(1.6)
    assert f.metrics["edge_distance_m"] == pytest.approx(3.0)
    # 운영 프로파일은 0.4 m 무대 가장자리를 위험으로 보지 않는다
    assert (
        list(
            default_registry()
            .create("R5", 1)
            .evaluate(ctx(r5_track((3.0, 3.0), (-1, 0, 0), [3.0, 3.0, 0.4]), surfaces=[s]))
        )
        == []
    )


def test_r5_edge_not_for_floor_or_unknown_z_and_hands_over_to_r2():
    s = WorkSurface.from_json(STAGE)
    rule = default_registry().create("R5", 1001)
    floor = r5_track((-2.5, 3.0), (1.0, 0.0, 0.0), "floor")
    assert list(rule.evaluate(ctx(floor, surfaces=[s]))) == []
    unknown = r5_track((3.0, 3.0), (-1.0, 0.0, 0.0), None)
    assert list(rule.evaluate(ctx(unknown, surfaces=[s]))) == []
    # 이미 R2 발생 영역 안이면 R2 가 맡는다
    near = r5_track((1.3, 3.0), (-1.0, 0.0, 0.0), [1.3, 3.0, 0.4])
    assert list(rule.evaluate(ctx(near, surfaces=[s]))) == []
    assert len(list(r2().evaluate(ctx(near, surfaces=[s])))) == 1


def test_r5_velocity_unknown_holds_edge_target():
    s = WorkSurface.from_json(STAGE)
    t = r5_track((2.0, 3.0), (0.0, 0.0, 0.0), [2.0, 3.0, 0.4])
    t["velocity"] = None
    (f,) = default_registry().create("R5", 1001).evaluate(ctx(t, surfaces=[s]))
    assert f.holding and f.reasons == ("input_unknown", "velocity_missing", "target_edge")
    assert f.metrics == {"edge_distance_m": pytest.approx(2.0)}


# --- 생명주기 반례(활성 이벤트 유지, 해소, 채널) ------------------------------------


class _Run:
    """R2·R3(시험 프로파일) + 기본 생명주기. 단계마다 판정 → 전이."""

    def __init__(self, origin=Origin.LIVE, surfaces=None):
        self.clock = FixedClock(T0)
        registry = default_registry()
        self.engine = RiskEngine([registry.create("R2", 1001), registry.create("R3", 1001)], self.clock)
        self.tracker = LifecycleTracker(origin=origin, gateway_id="tier0-01", clock=self.clock)
        self.surfaces = [surface()] if surfaces is None else surfaces
        self.origin = origin
        self.t = T0
        self.transitions = []
        self.signals = []

    def step(self, *tracks, dt=100):
        self.t += dt
        tr = []
        for x in tracks:
            x = dict(x)
            x["t_last_valid"] = self.t
            tr.append(x)
        w = world(*tr, t=self.t)
        self.clock.set(self.t)
        evaluation = self.engine.evaluate(w, [], self.origin, work_surfaces=self.surfaces)
        result = self.tracker.apply(evaluation, w, config_version="cfg-1")
        self.transitions += result.transitions
        self.signals += result.local_signals
        return result

    def active(self):
        return {s.rule_id: s for s in self.tracker.active()}

    def kinds(self, rule_id):
        return [t.transition for t in self.transitions if t.state.rule_id == rule_id]


def test_anchor_lost_on_podium_keeps_active_events():
    """반례: 단상 위 활성 R2·R3 중 접지점이 사라져도(유지 끝, z UNAVAILABLE) footprint 위면 30초 동안 해소 0건."""
    run = _Run()
    for _ in range(3):
        run.step(track())
    assert set(run.active()) == {"R2", "R3"}
    for _ in range(300):
        run.step(track(anchor=None))
    assert set(run.active()) == {"R2", "R3"}
    assert Lifecycle.RESOLVED not in run.kinds("R2") + run.kinds("R3")
    assert run.active()["R3"].confidence_reasons == (ConfidenceReason.Z_UNKNOWN,)
    # 다시 바닥 접지(단상 앞 바닥) → 3초 뒤 해소
    for _ in range(31):
        run.step(track(xy=(4.5, 5.5), anchor="floor"))
    assert run.active() == {}
    assert run.kinds("R2")[-1] is Lifecycle.RESOLVED and run.kinds("R3")[-1] is Lifecycle.RESOLVED


def test_anchor_lost_negative_control(monkeypatch):
    """음성 대조: UNKNOWN 경로가 '판정 없음'을 내면(모르는 상태를 안전으로) 위 시나리오는 3초 뒤 해소된다.
    즉 위 시험은 UNKNOWN 판정 처리에 실제로 걸린다."""
    original = height_common.standing

    def unknown_is_safe(track, surfaces, ranges, **kw):
        st = original(track, surfaces, ranges, **kw)
        return height_common.Standing(st.basis, st.z, ()) if st.basis is StandingBasis.UNKNOWN else st

    monkeypatch.setattr("simai_ai.risk.rules.r2_edge_proximity.standing", unknown_is_safe)
    monkeypatch.setattr("simai_ai.risk.rules.r3_elevated_position.standing", unknown_is_safe)
    run = _Run()
    for _ in range(3):
        run.step(track())
    for _ in range(40):
        run.step(track(anchor=None))
    assert run.active() == {}


def test_held_then_observation_lost_never_resolves():
    """반례: 단상 위에서 가려져 유지(HELD) → 트랙 UNKNOWN → 트랙 없음이 1분 이어져도 해소 0건(UNVERIFIABLE)."""
    run = _Run()
    for _ in range(3):
        run.step(track())
    for _ in range(20):
        run.step(track(z_validity="UNCERTAIN", contact_validity="UNCERTAIN"))
    assert ConfidenceReason.PREDICTED in run.active()["R3"].confidence_reasons
    for _ in range(100):
        run.step(track(state="UNKNOWN", anchor=None))
    for _ in range(500):
        run.step()
    assert set(run.active()) == {"R2", "R3"}
    assert all(s.verification.value == "UNVERIFIABLE" for s in run.active().values())
    assert Lifecycle.RESOLVED not in run.kinds("R2") + run.kinds("R3")


def test_single_frame_flicker_no_event_and_floor_person_no_event():
    """반례: 단상 위 순간 검출 한 프레임은 이벤트 0건(3프레임 중 2), 단상 옆 바닥 사람은 1분 동안 이벤트 0건."""
    run = _Run()
    run.step(track(xy=(4.5, 5.0), anchor="floor"))
    run.step(track())
    for _ in range(600):
        run.step(track(xy=(4.5, 5.9), anchor="floor"))
    assert run.transitions == []


def test_live_channels_and_non_live_no_signals():
    for origin in Origin:
        run = _Run(origin=origin)
        for _ in range(3):
            run.step(track())
        severities = {s.rule_id: s.severity for s in run.active().values()}
        assert severities == {"R2": Severity.CRITICAL, "R3": Severity.HIGH}
        if origin is Origin.LIVE:
            assert sorted(s.event_id for s in run.signals) == sorted(s.event_id for s in run.active().values())
        else:
            assert run.signals == []
    assert Channel.LOCAL_ALERT in channels_for(Severity.CRITICAL, Confidence.HIGH, Origin.LIVE).channels
    assert Channel.LOCAL_ALERT in channels_for(Severity.HIGH, Confidence.LOW, Origin.LIVE).channels


# --- 접지점을 모를 때 VALID 골반으로 '그 위에 없음'을 가린다 ----------------------------------


def with_hips(t, hip_z, *, validity="VALID", source="observed"):
    """COCO17 골격에 양쪽 엉덩이(11, 12)만 넣는다."""
    joints = [None] * 17
    joints[11] = [t["position_xy"][0] - 0.1, t["position_xy"][1], hip_z]
    joints[12] = [t["position_xy"][0] + 0.1, t["position_xy"][1], hip_z]
    t = dict(t)
    t["skeleton"] = {
        "joint_layout": "COCO17",
        "joints": joints,
        "visible": [j is not None for j in joints],
        "validity": [validity if j is not None else "UNAVAILABLE" for j in joints],
        "joint_source": [source] * 17,
    }
    return t


DESK = {"surface_id": "desk-1", "top_height_m": 0.75, "center_xy": [2.0, 2.0], "size_xy": [1.2, 0.6]}


def test_seated_at_desk_with_hidden_feet_is_not_on_desk():
    """반례: 책상(0.75 m)에 앉아 다리가 가려져 접지점이 없는 사람(VALID 골반 0.5 m)은 책상 위 고소·가장자리가 아니다."""
    desk = WorkSurface.from_json(DESK)
    seated = with_hips(track(xy=(2.0, 1.45), anchor=None), 0.5)  # 책상 앞변(y = 1.7) 0.25 m 앞, 범위가 footprint 에 걸침
    assert list(r3().evaluate(ctx(seated, surfaces=[desk]))) == []
    assert list(r2().evaluate(ctx(seated, surfaces=[desk]))) == []


@pytest.mark.parametrize(
    ("hip_z", "validity", "source", "judged"),
    [
        (0.5, "UNCERTAIN", "observed", True),  # UNCERTAIN 골반은 근거로 쓰지 않는다(모르면 위에 없음으로 확정 안 함)
        (0.5, "VALID", "inferred", True),  # HMR 추정 관절은 위험 판정에 쓰지 않는다(규칙 1)
        (0.70, "VALID", "observed", True),  # 상판 − 5 cm 이내는 빼지 않는다(삼각측량 오차 여유)
        (0.69, "VALID", "observed", False),
        (1.6, "VALID", "observed", True),  # 책상 위에 선 사람
    ],
)
def test_pelvis_exclusion_needs_valid_observed_hips(hip_z, validity, source, judged):
    desk = WorkSurface.from_json(DESK)
    t = with_hips(track(xy=(2.0, 2.0), anchor=None), hip_z, validity=validity, source=source)
    findings = list(r3().evaluate(ctx(t, surfaces=[desk])))
    assert bool(findings) is judged
    if judged:
        assert findings[0].confidence_reasons == (ConfidenceReason.Z_UNKNOWN,)


# --- 낮은 신뢰 새 발생 보류: 접지 모름(Z_UNKNOWN) + 예측 위치(PREDICTED) (7.1·7.2, S15P31A101-725) -------------------


@pytest.mark.parametrize("make_rule", [r2, r3], ids=["R2", "R3"])
@pytest.mark.parametrize(
    ("anchor", "state", "holding"),
    [
        (None, "PREDICTED", True),  # 접지 모름 + 예측 위치 → 새 발생 보류(holding)
        (None, "OBSERVED", False),  # 접지 모름 + 관측 위치 → 발생
        ("podium", "PREDICTED", False),  # 접지 있음 + 예측 위치 → 발생
    ],
)
def test_low_confidence_onset_withheld_only_when_z_unknown_and_predicted(make_rule, anchor, state, holding):
    (f,) = make_rule().evaluate(ctx(track(anchor=anchor, state=state), surfaces=[surface()]))
    assert f.holding is holding
    expected = set()
    if anchor is None:
        expected.add(ConfidenceReason.Z_UNKNOWN)
    if state == "PREDICTED":
        expected.add(ConfidenceReason.PREDICTED)
    assert set(f.confidence_reasons) == expected


def test_low_confidence_onset_held_anchor_predicted_still_raises():
    """유지 접지점(HELD)은 서 있는 면을 마지막 관측값으로 아는 상태라 예측 위치여도 보류하지 않는다."""
    t = track(state="PREDICTED", z_validity="UNCERTAIN", contact_validity="UNCERTAIN")
    for rule in (r2(), r3()):
        (f,) = rule.evaluate(ctx(t, surfaces=[surface()]))
        assert not f.holding


def test_r2_low_confidence_in_clear_band_stays_holding():
    """R2 유지 띠(1.0~1.3 m)는 원래 holding 이다. 보류 조건이 띠 판정을 바꾸지 않는다."""
    t = track(xy=(1.6, 3.0), anchor=None, state="PREDICTED")
    (f,) = r2().evaluate(ctx(t, surfaces=[WorkSurface.from_json(STAGE)]))
    assert f.holding and f.reasons == (REASON_EDGE_CLEAR, REASON_Z_UNKNOWN_ON_SURFACE)


def test_z_unknown_predicted_person_near_podium_raises_nothing():
    """17차 형태: 접지 모르는 예측 위치 사람이 단상 위·옆을 1분 지나도 R2·R3 이벤트 0건. 관측 위치가 되면 지속 조건대로 발생."""
    run = _Run()
    for i in range(600):
        run.step(track(xy=(3.0 + 0.005 * i, 6.75), anchor=None, state="PREDICTED"))
    assert run.transitions == [] and run.signals == []
    run.step(track(anchor=None))  # 접지 모름 + 관측 위치: 1프레임(3프레임 중 2 미충족)
    assert run.active() == {}
    run.step(track(anchor=None))
    assert set(run.active()) == {"R2", "R3"}
    assert all(ConfidenceReason.Z_UNKNOWN in s.confidence_reasons for s in run.active().values())


def test_z_unknown_predicted_then_anchor_appears_raises():
    """접지 모름 + 예측 위치 다음에 접지점이 생기면(예측 위치 그대로) 지속 조건대로 정상 발생."""
    run = _Run()
    for _ in range(10):
        run.step(track(anchor=None, state="PREDICTED"))
    assert run.transitions == []
    for _ in range(2):
        run.step(track(state="PREDICTED"))
    assert set(run.active()) == {"R2", "R3"}


def test_low_confidence_onset_negative_control(monkeypatch):
    """음성 대조: 보류 조건을 끄면 같은 17차 형태 입력이 R2·R3 를 연다. 즉 위 시험은 보류 조건에 실제로 걸린다."""
    monkeypatch.setattr("simai_ai.risk.rules.r2_edge_proximity.low_confidence_onset", lambda track, basis: False)
    monkeypatch.setattr("simai_ai.risk.rules.r3_elevated_position.low_confidence_onset", lambda track, basis: False)
    run = _Run()
    for _ in range(3):
        run.step(track(anchor=None, state="PREDICTED"))
    assert set(run.active()) == {"R2", "R3"}


def test_open_events_kept_when_later_z_unknown_and_predicted():
    """이미 열린 R2·R3 는 이후 접지 모름 + 예측 위치가 이어져도 유지(해소 0건, 등급 그대로)되고, 바닥으로 내려오면 3초 뒤 해소."""
    run = _Run()
    for _ in range(3):
        run.step(track())
    opened = {r: s.event_id for r, s in run.active().items()}
    assert set(opened) == {"R2", "R3"}
    for _ in range(300):
        run.step(track(anchor=None, state="PREDICTED"))
    active = run.active()
    assert {r: s.event_id for r, s in active.items()} == opened
    assert active["R2"].severity is Severity.CRITICAL and active["R3"].severity is Severity.HIGH
    assert set(active["R3"].confidence_reasons) == {ConfidenceReason.Z_UNKNOWN, ConfidenceReason.PREDICTED}
    assert Lifecycle.RESOLVED not in run.kinds("R2") + run.kinds("R3")
    for _ in range(31):
        run.step(track(xy=(4.5, 5.5), anchor="floor"))
    assert run.active() == {}
    assert run.kinds("R2")[-1] is Lifecycle.RESOLVED and run.kinds("R3")[-1] is Lifecycle.RESOLVED


def test_pending_then_z_unknown_predicted_uses_pending_loss_path():
    """지속 대기 중(관측 위치 발생 판정 1프레임) 예측 위치로 바뀌면 기존 지속 대기 관측 상실 처리대로 바로 발생한다(6.4).
    보류 조건은 새 대기를 시작하지 않을 뿐, 이미 관측으로 시작한 대기의 안전 처리는 바꾸지 않는다."""
    run = _Run()
    run.step(track(anchor=None))
    assert run.active() == {}
    run.step(track(anchor=None, state="PREDICTED"))
    assert set(run.active()) == {"R2", "R3"}
    assert all(s.verification.value == "UNVERIFIABLE" for s in run.active().values())
