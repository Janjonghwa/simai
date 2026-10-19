"""R5 진입 예측 (명세 7.2 R5, 7.1, 6.4, 7.3, 부록 D-G6-03, FR-70)."""

import math
import random

import pytest

from simai_ai.risk import (
    Channel,
    EntryPredictionRule,
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    Origin,
    RiskEngine,
    RuleContext,
    Severity,
    WorldState,
    Zone,
    channels_for,
    default_registry,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import ConfidenceReason
from simai_ai.risk.rules.r5_entry_prediction import (
    R5_HORIZON_MS,
    REASON_INPUT_UNKNOWN,
    REASON_POSITION_STALE,
    REASON_PREDICTED_CLEAR_HORIZON,
    REASON_PREDICTED_ENTRY,
    REASON_PREDICTED_TOUCHING_NOW,
    REASON_SPEED_IMPLAUSIBLE,
    REASON_TRACK_NOT_CONFIRMED,
    REASON_TRACK_NOT_OBSERVED,
    REASON_VELOCITY_MISSING,
    REASON_Z_CLEAR_MARGIN,
    REASON_Z_OVERLAP,
    first_touch_range_s,
    first_touch_s,
)
from simai_ai.risk.zone_geometry import HorizontalRange, distance_outside_polygon

T0 = 1760342400100
SQUARE = [[3.0, 4.5], [4.0, 4.5], [4.0, 5.5], [3.0, 5.5]]  # 왼쪽 변 x = 3.0
SIGMA = 0.05  # 범위 반경 = 2·0.05 + 0.3 = 0.4 m


def zone(judge="GROUND_FOOTPRINT", z_range=None, severity="HIGH", zone_id="z-04", polygon=SQUARE):
    return Zone.from_json(
        {
            "zone_id": zone_id,
            "zone_version": "zone-1",
            "judge_type": judge,
            "polygon_xy": polygon,
            "z_range": z_range,
            "severity": severity,
        }
    )


def track(
    xy=(1.0, 5.0),
    velocity=(1.0, 0.0, 0.0),
    *,
    world_id="w-0012",
    state="OBSERVED",
    lifecycle="CONFIRMED",
    identity="HIGH",
    cameras=("cam1", "cam3"),
    sigma=SIGMA,
    anchor="same",
    head_z=1.7,
    t_last_valid=T0,
):
    support = anchor is not None
    anchor_xyz = [xy[0], xy[1], 0.0] if anchor == "same" else anchor
    return {
        "world_id": world_id,
        "t_last_valid": t_last_valid,
        "observation_state": state,
        "track_lifecycle": lifecycle,
        "identity_confidence": identity,
        "identity_revision": 0,
        "position_xy": list(xy),
        "position_xy_sigma": list(sigma) if isinstance(sigma, tuple) else [sigma, sigma],
        "pelvis_xyz": [xy[0], xy[1], 0.92],
        "ground_anchor_xyz": anchor_xyz,
        "support_surface_id": "floor" if support else None,
        "support_surface_validity": "VALID" if support else "UNKNOWN",
        "contact_validity": "VALID" if support else "UNAVAILABLE",
        "z_validity": "VALID" if support else "UNCERTAIN",
        "head_z": head_z,
        "velocity": None if velocity is None else list(velocity),
        "cameras": list(cameras),
    }


def world(*tracks, t=T0):
    return WorldState.from_payload(
        {"t_latest_global": t, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": list(tracks)}
    )


def judge(*tracks, zones=None, now=T0 + 100, rule=None):
    """판정 시각 now(기본: 관측 100 ms 뒤 → staleness 100 ms)."""
    rule = rule or EntryPredictionRule()
    ctx = RuleContext(now_ms=now, world=world(*tracks), zones=tuple(zones or [zone()]))
    return list(rule.evaluate(ctx))


def test_approach_raises_caution_with_lead_time():
    # 범위 앞끝이 1.6 m 뒤 x = 3.0 에 닿는다. 관측은 100 ms 전이므로 판정 시각부터 1.5 초.
    (f,) = judge(track())
    assert (f.rule_id, f.rule_version, f.severity) == ("R5", 1, Severity.CAUTION)
    assert f.world_ids == ("w-0012",) and f.zone_id == "z-04"
    assert f.holding is False and f.reasons == (REASON_PREDICTED_ENTRY,)
    assert f.needs_check is False and f.confidence_reasons == ()
    assert f.metrics["time_to_entry_s"] == pytest.approx(1.5)
    assert f.metrics["speed_mps"] == pytest.approx(1.0)
    assert f.metrics["zone_distance_m"] == pytest.approx(2.0)


def test_severity_is_caution_even_for_critical_zone():
    (f,) = judge(track(), zones=[zone(severity="CRITICAL")])
    assert f.severity is Severity.CAUTION


def test_horizon_boundary_and_clear_band():
    # 선행 2.0 초(= horizon)는 발생, 2.0~2.5 초는 유지, 2.5 초 넘으면 판정 없음.
    (at_horizon,) = judge(track(xy=(0.5, 5.0)))
    assert at_horizon.holding is False and at_horizon.metrics["time_to_entry_s"] == pytest.approx(2.0)
    (band,) = judge(track(xy=(0.2, 5.0)))
    assert band.holding is True and band.reasons == (REASON_PREDICTED_CLEAR_HORIZON,)
    assert band.metrics["time_to_entry_s"] == pytest.approx(2.3)
    assert judge(track(xy=(-0.1, 5.0))) == []


def test_lead_time_counts_from_judgement_time():
    # 같은 관측을 늦게 판정하면 그만큼 선행 시간이 줄어든다(이벤트 생성 시점 기준, 명세 R5 설명).
    (f,) = judge(track(), now=T0 + 400)
    assert f.metrics["time_to_entry_s"] == pytest.approx(1.2)


def test_moving_away_or_passing_by_is_not_predicted():
    assert judge(track(velocity=(-1.0, 0.0, 0.0))) == []
    # 구역 아래 변(y = 4.5)에서 0.75 m 떨어진 선을 따라 지나감
    assert judge(track(xy=(2.0, 3.75), velocity=(1.2, 0.0, 0.0))) == []


def test_slow_closing_is_not_predicted():
    # 구역 아래 변(y = 4.5) 0.75 m 옆을 지나가며 속도 잡음으로 구역 쪽(+y)으로 0.25 m/s 기울었다.
    # 범위(0.4 m)가 간격 0.35 m 를 1.4 초에 메우지만 다가가는 속력 0.25 m/s < 0.4 m/s 라 판정 없음.
    passing = track(xy=(2.8, 3.75), velocity=(0.5, 0.25, 0.0))
    assert judge(passing) == []
    # 하한을 0 으로 두면 기운 지나감도 낸다(관측 1.4 초 뒤, 판정 시각부터 1.3 초)
    (g,) = judge(passing, rule=EntryPredictionRule(min_closing_speed_mps=0.0))
    assert g.metrics["time_to_entry_s"] == pytest.approx(1.3)
    # 구역 쪽으로 0.5 m/s 로 곧장 다가가면 판정한다(관측 0.7 초 뒤, 판정 시각부터 0.6 초)
    (f,) = judge(track(xy=(3.5, 3.75), velocity=(0.0, 0.5, 0.0)))
    assert f.metrics["time_to_entry_s"] == pytest.approx(0.6)


def test_already_touching_is_left_to_r1():
    # 범위(0.4 m)가 이미 구역에 걸린다 → R1 이 맡는다
    assert judge(track(xy=(2.65, 5.0))) == []
    assert judge(track(xy=(3.5, 5.0))) == []
    # 관측 범위는 안 걸리지만(간격 0.05 m) 판정 시각(100 ms 뒤)으로 옮긴 범위가 걸리면 '진입 중' 발생(선행 0초).
    # R1 은 관측 위치만 보므로 다음 관측에서 R1 이 맡는다.
    (f,) = judge(track(xy=(2.55, 5.0)))
    assert f.holding is False and f.metrics["time_to_entry_s"] == 0.0
    assert f.reasons == (REASON_PREDICTED_ENTRY, REASON_PREDICTED_TOUCHING_NOW)


def test_crossing_already_passed_by_judgement_time_is_not_predicted():
    # 폭 0.1 m 구역을 4 m/s 로 지나감. 관측 시각에는 구역 앞이지만 판정 시각(500 ms 뒤)에는 범위가 이미 구역을 지났다.
    # 지난 교차를 선행 0초 예측으로 내지 않는다(리뷰 F2).
    thin = zone(polygon=[[3.0, 4.5], [3.1, 4.5], [3.1, 5.5], [3.0, 5.5]])
    for x in (2.19, 2.59):
        assert judge(track(xy=(x, 5.0), velocity=(4.0, 0.0, 0.0)), zones=[thin], now=T0 + 500) == []
    # 등록부 R1+R5 와 생명주기로 두 관측을 넣어도 R5 이벤트가 생기지 않는다
    registry = default_registry()
    clock = FixedClock(T0)
    engine = RiskEngine([registry.create("R1", 1), registry.create("R5", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    for dt, x in ((0, 2.19), (100, 2.59)):
        clock.set(T0 + dt + 500)
        state = world(track(xy=(x, 5.0), velocity=(4.0, 0.0, 0.0), t_last_valid=T0 + dt), t=T0 + dt)
        step = tracker.apply(engine.evaluate(state, [thin], Origin.LIVE), state, config_version="cfg-12")
        assert all(t.state.rule_id != "R5" for t in step.transitions)


def test_ground_anchor_range_counts_like_r1():
    # 접지점이 골반보다 구역 쪽에 있으면 접지점 범위가 먼저 닿는다
    (f,) = judge(track(xy=(1.0, 5.0), anchor=[1.5, 5.0, 0.0]))
    assert f.metrics["time_to_entry_s"] == pytest.approx(1.0)
    assert f.metrics["zone_distance_m"] == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"velocity": None}, REASON_VELOCITY_MISSING),
        ({"velocity": (5.0, 0.0, 0.0)}, REASON_SPEED_IMPLAUSIBLE),  # 실내에서 나기 어려운 속력(필터 오류)
        ({"state": "PREDICTED"}, REASON_TRACK_NOT_OBSERVED),
        ({"state": "UNKNOWN"}, REASON_TRACK_NOT_OBSERVED),
        ({"lifecycle": "TENTATIVE"}, REASON_TRACK_NOT_CONFIRMED),
        ({"lifecycle": "OCCLUDED"}, REASON_TRACK_NOT_CONFIRMED),
    ],
    ids=["no-velocity", "implausible", "predicted", "unknown", "tentative", "occluded"],
)
def test_unknown_velocity_only_holds(kwargs, code):
    # 속도를 모르면 발생 판정은 없고, 가장 가까운 구역으로 유지(holding) 하나만 낸다(해소 근거로 쓰지 않음, 리뷰 F3).
    (f,) = judge(track(**kwargs))
    assert f.holding is True and f.reasons == (REASON_INPUT_UNKNOWN, code)
    assert f.severity is Severity.CAUTION and f.zone_id == "z-04"
    assert f.confidence_reasons == ()  # PREDICTED 를 붙이지 않는다(Lead 결정)
    assert dict(f.metrics) == {"zone_distance_m": pytest.approx(2.0)}


def test_slow_known_velocity_is_not_judged():
    # 속도를 알고 실제로 느리면(멈춰 섬) '모름'이 아니다: 판정 없음(해소 근거가 된다)
    assert judge(track(velocity=(0.1, 0.0, 0.0))) == []
    assert judge(track(velocity=(0.0, 0.0, 0.0))) == []


def test_unknown_velocity_hold_picks_nearest_zone_and_skips_r1_domain():
    far = zone(zone_id="z-far", polygon=[[10.0, 4.5], [11.0, 4.5], [11.0, 5.5], [10.0, 5.5]])
    (f,) = judge(track(xy=(8.0, 5.0), velocity=None), zones=[zone(), far])
    assert f.zone_id == "z-far"
    # 지금 범위가 구역에 걸리면 R1 이 맡으므로 내지 않는다
    assert judge(track(xy=(3.5, 5.0), velocity=None), zones=[zone(), far]) == []


def test_stale_track_only_holds():
    assert judge(track(), now=T0 + 500)[0].holding is False
    (f,) = judge(track(), now=T0 + 501)
    assert f.holding is True and f.reasons == (REASON_INPUT_UNKNOWN, REASON_POSITION_STALE)


def _active_r5_then(later, *, rule_kwargs=None):
    """1 m/s 접근으로 R5 를 발생시킨 뒤 later(관측 시각 오프셋 → track kwargs)를 넣고 전이를 돌려준다."""
    clock = FixedClock(T0)
    engine = RiskEngine([EntryPredictionRule(**(rule_kwargs or {}))], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    out = []
    frames = [(0, {}), (100, {})] + list(later)
    for dt, kwargs in frames:
        t = T0 + dt
        clock.set(t + 100)
        args = {"t_last_valid": t, **kwargs}
        state = world(track(**args), t=t)
        step = tracker.apply(engine.evaluate(state, [zone()], Origin.LIVE), state, config_version="cfg-12")
        out += [(dt, x.transition, x.state.verification) for x in step.transitions]
    return out, tracker


def test_active_r5_not_resolved_while_velocity_missing():
    # 리뷰 F3 회귀: 속도가 null 이 된 뒤 새 위치 관측이 3초 넘게 이어져도 활성 R5 를 해소하지 않는다.
    lost = [(dt, {"velocity": None}) for dt in range(200, 4300, 500)]
    transitions, tracker = _active_r5_then(lost)
    assert transitions[0][1] is Lifecycle.RAISED
    assert Lifecycle.RESOLVED not in [x[1] for x in transitions]
    assert len(tracker.active()) == 1
    # 속도 0 이 실제로 관측되면(멈춰 섬) 3초 뒤 해소한다
    stopped = [(dt, {"velocity": (0.0, 0.0, 0.0)}) for dt in range(200, 3700, 500)]
    transitions, tracker = _active_r5_then(stopped)
    assert [x[1] for x in transitions] == [Lifecycle.RAISED, Lifecycle.RESOLVED]
    assert tracker.active() == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lifecycle": "TENTATIVE"},
        {"velocity": (5.0, 0.0, 0.0)},
        {"state": "PREDICTED"},
    ],
    ids=["tentative", "implausible", "predicted"],
)
def test_active_r5_not_resolved_while_input_unknown(kwargs):
    later = [(dt, kwargs) for dt in range(200, 4300, 500)]
    transitions, tracker = _active_r5_then(later)
    assert Lifecycle.RESOLVED not in [x[1] for x in transitions]
    assert len(tracker.active()) == 1


def test_active_r5_not_resolved_while_position_stale():
    # 관측 gap 은 짧지만 판정 lag 로 staleness 가 500 ms 를 넘는 동안에도 해소하지 않는다
    clock = FixedClock(T0)
    engine = RiskEngine([EntryPredictionRule()], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    transitions = []
    for dt, lag in [(0, 100), (100, 100)] + [(dt, 550) for dt in range(200, 4300, 500)]:
        t = T0 + dt
        clock.set(t + lag)
        state = world(track(t_last_valid=t), t=t)
        step = tracker.apply(engine.evaluate(state, [zone()], Origin.LIVE), state, config_version="cfg-12")
        transitions += [x.transition for x in step.transitions]
    assert transitions[0] is Lifecycle.RAISED and Lifecycle.RESOLVED not in transitions
    assert len(tracker.active()) == 1


def test_vertical_velocity_is_ignored():
    (f,) = judge(track(velocity=(1.0, 0.0, 3.0)))
    assert f.metrics["speed_mps"] == pytest.approx(1.0)


def test_track_confidence_reasons():
    (f,) = judge(track(identity="IDENTITY_UNCERTAIN", cameras=("cam1",)))
    assert f.severity is Severity.CAUTION and f.needs_check is True
    assert f.confidence_reasons == (ConfidenceReason.SINGLE_VIEW, ConfidenceReason.IDENTITY_UNCERTAIN)


def test_volume_zone_height():
    floor = zone("VOLUME_3D", [0.0, 2.5], zone_id="z-floor")
    (f,) = judge(track(), zones=[floor])
    assert f.holding is False and f.reasons == (REASON_PREDICTED_ENTRY, REASON_Z_OVERLAP)
    # 머리 위 구역: 점유 높이 위 끝 1.7 m, 구역 아래 끝 1.9 m → 간격 0.2 m(해소 간격 안) → 유지만
    near = zone("VOLUME_3D", [1.9, 4.0], zone_id="z-near")
    (g,) = judge(track(), zones=[near])
    assert g.holding is True and g.reasons == (REASON_PREDICTED_ENTRY, REASON_Z_CLEAR_MARGIN)
    # 간격 0.3 m 이상이면 판정 없음
    assert judge(track(), zones=[zone("VOLUME_3D", [2.0, 4.0], zone_id="z-above")]) == []


def test_two_zones_on_path_are_both_predicted_in_one_event():
    near = zone(zone_id="z-near", polygon=[[2.0, 4.5], [2.5, 4.5], [2.5, 5.5], [2.0, 5.5]])
    findings = judge(track(xy=(0.0, 5.0)), zones=[near, zone()])
    assert sorted((f.zone_id, f.holding) for f in findings) == [("z-04", True), ("z-near", False)]
    # 생명주기는 (규칙, 사람) 키로 이벤트 하나를 만든다
    registry = default_registry()
    clock = FixedClock(T0)
    engine = RiskEngine([registry.create("R5", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    raised = []
    for i in range(3):
        t = T0 + 100 * i
        clock.set(t + 100)
        state = world(track(xy=(0.0 + 0.1 * i, 5.0), t_last_valid=t), t=t)
        step = tracker.apply(engine.evaluate(state, [near, zone()], Origin.LIVE), state, config_version="cfg-12")
        raised += [x for x in step.transitions if x.transition is Lifecycle.RAISED]
    assert len(raised) == 1 and raised[0].state.zone_id == "z-near"


def test_volume_zone_unknown_height_judges_by_xy_with_z_unknown():
    above = zone("VOLUME_3D", [2.0, 4.0], zone_id="z-above")
    (f,) = judge(track(anchor=None), zones=[above])
    assert f.holding is False and f.reasons == (REASON_PREDICTED_ENTRY,)
    assert f.confidence_reasons == (ConfidenceReason.Z_UNKNOWN,)


def test_each_zone_is_judged():
    far = zone(zone_id="z-far", polygon=[[10.0, 4.5], [11.0, 4.5], [11.0, 5.5], [10.0, 5.5]])
    findings = judge(track(), zones=[zone(), far])
    assert [f.zone_id for f in findings] == ["z-04"]


def test_concave_zone_notch_is_not_entered():
    # ㄷ 자 구역의 오목한 홈(x 3~4, y 4.9~5.1)으로 들어가는 사람은 홈 안쪽 끝 벽(x = 4)에 닿을 때 진입한다.
    notch = [[3.0, 4.0], [5.0, 4.0], [5.0, 6.0], [3.0, 6.0], [3.0, 5.6], [4.0, 5.6], [4.0, 4.4], [3.0, 4.4]]
    (f,) = judge(track(xy=(2.0, 5.0), sigma=0.0), zones=[zone(polygon=notch)])
    # 범위 반경 0.3 m 원이 홈 벽(y 4.4·5.6, 거리 0.6 m)에는 닿지 않고 x = 3.7 에서 끝 벽에 닿는다: 1.7 m → 1.6 초
    assert f.metrics["time_to_entry_s"] == pytest.approx(1.6)


def _dense_first_touch(center, radius, velocity, polygon, limit_s, step_s=1e-4):
    """독립 오라클: 0.1 ms 간격 표본에서 원 중심과 다각형의 거리가 반경 이하가 되는 첫 시각."""
    for k in range(round(limit_s / step_s) + 1):
        t = k * step_s
        p = (center[0] + velocity[0] * t, center[1] + velocity[1] * t)
        if distance_outside_polygon(p, polygon) <= radius:
            return t
    return None


def test_first_touch_matches_dense_continuous_scan():
    rng = random.Random(98)
    polygons = [
        SQUARE,
        [[3.0, 4.0], [5.0, 4.0], [5.0, 6.0], [3.0, 6.0], [3.0, 5.6], [4.0, 5.6], [4.0, 4.4], [3.0, 4.4]],
        [[3.0, 4.0], [4.5, 5.0], [3.0, 6.0]],
    ]
    checked = touched = 0
    for _ in range(40):
        polygon = rng.choice(polygons)
        center = (rng.uniform(0.5, 6.5), rng.uniform(2.5, 7.5))
        radius = rng.uniform(0.3, 0.7)
        if distance_outside_polygon(center, polygon) <= radius:
            continue
        speed, heading = rng.uniform(0.2, 4.0), rng.uniform(-math.pi, math.pi)
        velocity = (speed * math.cos(heading), speed * math.sin(heading))
        got = first_touch_s(center, radius, velocity, polygon, limit_s=2.5)
        want = _dense_first_touch(center, radius, velocity, polygon, 2.5)
        checked += 1
        if want is None:
            # 표본 사이의 아주 짧은 스침만 해석해가 잡을 수 있다. 그때는 접촉 시각의 거리가 반경과 같아야 한다.
            if got is not None:
                p = (center[0] + velocity[0] * got, center[1] + velocity[1] * got)
                assert distance_outside_polygon(p, polygon) == pytest.approx(radius, abs=1e-6)
            continue
        touched += 1
        assert got is not None and want - 1.5e-4 <= got <= want + 1e-9, (polygon, center, radius, velocity)
    assert checked >= 25 and touched >= 5


_DIAG = 0.4 / math.sqrt(2)


def test_first_touch_catches_vertex_tangent_between_samples():
    # 꼭짓점 (3, 4.5) 에 1000.5 ms 에 접선으로 닿는 원(리뷰 F4). ms 격자 표본으로는 놓쳤다.
    # 접촉 허용오차(1e-9 m)만큼 넓힌 반경이라 접선에서는 약 0.02 ms 일찍 닿는다.
    tc = 1.0005
    center = (3.0 - _DIAG - tc, 4.5 - _DIAG + tc)
    assert first_touch_s(center, 0.4, (1.0, -1.0), SQUARE, limit_s=2.5) == pytest.approx(tc, abs=1e-4)
    # 같은 꼭짓점을 1.4 mm 비켜 가면 닿지 않는다
    miss = (center[0] - 0.001, center[1] - 0.001)
    assert first_touch_s(miss, 0.4, (1.0, -1.0), SQUARE, limit_s=2.5) is None


def test_first_touch_short_penetration_inside_one_ms():
    # 꼭짓점을 0.1 µm 깊이로 약 0.4 ms 동안만 스치는 원도 잡는다(1 ms 사이 침투)
    tc, depth = 1.0005, 1e-7
    shift = depth / math.sqrt(2)
    center = (3.0 - _DIAG - tc + shift, 4.5 - _DIAG + tc + shift)
    t = first_touch_s(center, 0.4, (1.0, -1.0), SQUARE, limit_s=2.5, tolerance_m=0.0)
    assert t is not None and tc - 0.001 < t < tc


def test_first_touch_at_zero_and_none_without_speed():
    assert first_touch_s((3.5, 5.0), 0.0, (0.0, 0.0), SQUARE, limit_s=0.1) == 0.0
    assert first_touch_s((0.0, 0.0), 0.0, (0.0, 0.0), SQUARE, limit_s=0.1) is None
    assert first_touch_s((0.0, 5.0), 0.4, (1.0, 0.0), SQUARE, limit_s=0.1) is None
    assert first_touch_s((0.0, 5.0), 0.4, (1.0, 0.0), SQUARE, limit_s=2.6) == pytest.approx(2.6)


def test_closing_speed_boundary_is_compared_before_rounding():
    # 정확히 0.4 m/s 정면 접근(리뷰 F1). 접촉이 정수 ms 가 아니어도(1.40025 초) 하한 경계에서 버리지 않는다.
    (f,) = judge(track(xy=(1.9999, 5.0), velocity=(0.4, 0.0, 0.0)))
    assert f.holding is False and f.metrics["time_to_entry_s"] == 1.4  # 표시는 ms 반올림
    # 하한보다 느리면(0.39 m/s) 내지 않는다
    assert judge(track(xy=(2.0, 5.0), velocity=(0.39, 0.0, 0.0))) == []


def test_closing_speed_boundary_on_consecutive_frames():
    # 0.4 m/s 로 100 ms 마다 0.04 m 씩 다가가는 17프레임: R1 인계 전 16프레임 모두 R5 발생 판정(리뷰 F1 회귀).
    # 마지막 프레임은 관측 범위가 구역에 걸려 R1 이 맡는다.
    registry = default_registry()
    clock = FixedClock(T0)
    engine = RiskEngine([registry.create("R1", 1), registry.create("R5", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    r5_findings = raised = r1_last = 0
    for i in range(17):
        t = T0 + 100 * i
        clock.set(t + 100)
        state = world(track(xy=(1.9999 + 0.04 * i, 5.0), velocity=(0.4, 0.0, 0.0), t_last_valid=t), t=t)
        evaluation = engine.evaluate(state, [zone()], Origin.LIVE)
        r5_findings += sum(1 for f in evaluation.findings if f.rule_id == "R5" and not f.holding)
        r1_last = sum(1 for f in evaluation.findings if f.rule_id == "R1" and not f.holding)
        step = tracker.apply(evaluation, state, config_version="cfg-12")
        raised += sum(1 for x in step.transitions if x.state.rule_id == "R5" and x.transition is Lifecycle.RAISED)
    assert r5_findings == 16 and raised == 1 and r1_last == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"horizon_ms": 0},
        {"horizon_ms": 1.5},
        {"clear_extra_ms": -1},
        {"max_staleness_ms": True},
        {"min_speed_mps": 0.0},
        {"min_speed_mps": 2.0, "max_speed_mps": 1.0},
        {"min_closing_speed_mps": -0.1},
        {"k": -1.0},
        {"occupancy_radius_m": math.nan},
    ],
)
def test_invalid_parameters(kwargs):
    with pytest.raises(RiskInputError):
        EntryPredictionRule(**kwargs)


def test_registry_creates_r5():
    rule = default_registry().create("R5", 1)
    assert isinstance(rule, EntryPredictionRule) and rule.spec.key == ("R5", 1)
    assert "tracks.velocity" in rule.spec.reads
    assert R5_HORIZON_MS == 2000


def _approach_run(origin):
    """10 Hz 로 x = 0.5 에서 1 m/s 로 구역에 다가간다. 엔진(R1+R5)·생명주기를 거친 전이와 신호를 돌려준다."""
    registry = default_registry()
    clock = FixedClock(T0)
    engine = RiskEngine([registry.create("R1", 1), registry.create("R5", 1)], clock)
    tracker = LifecycleTracker(origin=origin, gateway_id="tier0-01", clock=clock)
    transitions, signals, candidates = [], [], []
    for i in range(30):
        t = T0 + 100 * i
        clock.set(t + 50)
        state = world(track(xy=(0.5 + 0.1 * i, 5.0), t_last_valid=t), t=t)
        evaluation = engine.evaluate(state, [zone()], origin)
        candidates += evaluation.alert_candidates()
        step = tracker.apply(evaluation, state, config_version="cfg-12")
        transitions += step.transitions
        signals += step.local_signals
    return transitions, signals, candidates


def test_live_r5_is_control_screen_notice_only():
    transitions, signals, candidates = _approach_run(Origin.LIVE)
    r5 = [t for t in transitions if t.state.rule_id == "R5"]
    assert r5[0].transition is Lifecycle.RAISED and r5[0].state.severity is Severity.CAUTION
    # 첫 판정(선행 1.95 초)은 지속 조건 대기, 두 번째 프레임에서 발생: 선행 1.85 초
    assert r5[0].state.metrics["time_to_entry_s"] == pytest.approx(1.85)
    plan = channels_for(Severity.CAUTION, r5[0].state.confidence, Origin.LIVE)
    # 7.3 표: 관제 화면 안내. WATCH_WEAK_VIBRATION 은 확장(부록 C.6) 워치 경로라 MVP 에는 발송 단말이 없다(dev 기존 표).
    assert plan.channels == {Channel.CONTROL_SCREEN_NOTICE, Channel.WATCH_WEAK_VIBRATION}
    # 현장 경보는 R1(HIGH 구역) 이벤트에서만 난다
    assert signals and {s.rule_id for s in signals if hasattr(s, "rule_id")} == {"R1"}
    r1 = [t for t in transitions if t.state.rule_id == "R1" and t.transition is Lifecycle.RAISED]
    assert r1 and r1[0].state.observation_time - r5[0].state.observation_time >= 1000
    assert {c.finding.rule_id for c in candidates} >= {"R5"}


@pytest.mark.parametrize("origin", [Origin.PLANNED, Origin.REPLAY, Origin.WHAT_IF])
def test_non_live_r5_never_alerts(origin):
    transitions, signals, candidates = _approach_run(origin)
    assert any(t.state.rule_id == "R5" for t in transitions)
    assert signals == [] and candidates == []


# --- 축마다 다른 σ (2차 리뷰 N1). 운영 Tier 0 의 position_xy_sigma 는 축마다 다르다. ---

ANISO = (0.05, 0.15)  # 타원 반축 (0.1, 0.3) + 0.3 m. 긴 반축 원(0.6 m)으로 넓히면 x 쪽에 0.2 m 띠가 생긴다


@pytest.mark.parametrize(
    ("xy", "velocity"),
    [
        ((2.5, 5.0), (-0.3, 0.0, 0.0)),  # 구역에서 멀어짐(관측 타원 간격 약 0.1 m)
        ((2.5, 5.0), (-1.0, 0.0, 0.0)),
        ((2.45, 5.0), (0.0, 1.0, 0.0)),  # 변과 나란히
        ((2.5, 5.0), (0.0, -0.25, 0.0)),
        ((2.6, 4.0), (-0.7, -0.7, 0.0)),  # 꼭짓점 (3, 4.5) 근처에서 대각선으로 멀어짐
    ],
    ids=["away-slow", "away-fast", "parallel", "parallel-slow", "corner-away"],
)
def test_anisotropic_sigma_moving_away_or_parallel_is_not_predicted(xy, velocity):
    assert judge(track(xy=xy, velocity=velocity, sigma=ANISO)) == []


def test_anisotropic_sigma_keeps_ellipse_lead_time():
    # 정면 접근은 타원의 x 반축(0.1 m, 48각형 외접)으로 잰다: 1.1 → 3.0 − 0.1002 − 0.3, 약 1.5 초.
    # 긴 반축 원(0.3 m)으로 넓히면 1.3 초가 되던 것을 R1 과 같은 범위로 되돌렸다.
    (f,) = judge(track(xy=(1.0, 5.0), sigma=ANISO))
    assert f.holding is False and f.metrics["time_to_entry_s"] == pytest.approx(1.5, abs=1e-3)
    # 같은 σ 에서 판정 시각 범위가 이미 걸리는 접근(구역 쪽 0.5 m/s, 관측 간격 약 0.03 m)은 '진입 중' 0초 발생
    (g,) = judge(track(xy=(2.57, 5.0), velocity=(0.5, 0.0, 0.0), sigma=ANISO))
    assert g.reasons == (REASON_PREDICTED_ENTRY, REASON_PREDICTED_TOUCHING_NOW)
    # 같은 위치에서 위아래(긴 반축 쪽)로 다가가면 y 반축 0.3 m 로 잰다
    (h,) = judge(track(xy=(3.5, 3.5), velocity=(0.0, 1.0, 0.0), sigma=ANISO))
    assert h.metrics["time_to_entry_s"] == pytest.approx(4.5 - 3.6 - 0.3 * 1.00214 - 0.3, abs=2e-3)


def test_anisotropic_parallel_walk_never_raises():
    # 긴 구역 옆(관측 타원 간격 약 0.1 m)을 1 m/s 로 40프레임 나란히 걸어도 R5 이벤트가 생기지 않는다(R1 은 유지 판정만)
    long_zone = zone(polygon=[[3.0, 0.0], [4.0, 0.0], [4.0, 20.0], [3.0, 20.0]])
    registry = default_registry()
    clock = FixedClock(T0)
    engine = RiskEngine([registry.create("R1", 1), registry.create("R5", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    r5 = 0
    for i in range(40):
        t = T0 + 100 * i
        clock.set(t + 100)
        state = world(track(xy=(2.5, 1.0 + 0.1 * i), velocity=(0.0, 1.0, 0.0), sigma=ANISO, t_last_valid=t), t=t)
        evaluation = engine.evaluate(state, [long_zone], Origin.LIVE)
        r5 += sum(1 for f in evaluation.findings if f.rule_id == "R5")
        step = tracker.apply(evaluation, state, config_version="cfg-12")
        assert all(x.state.rule_id != "R5" for x in step.transitions)
    assert r5 == 0


def _dense_range_touch(rng, velocity, polygon, limit_s, step_s=1e-3):
    """독립 오라클: 1 ms 표본에서 R1 과 같은 overlaps 가 처음 참이 되는 시각."""
    for k in range(round(limit_s / step_s) + 1):
        t = k * step_s
        moved = HorizontalRange(
            center=(rng.center[0] + velocity[0] * t, rng.center[1] + velocity[1] * t),
            semi_axes=rng.semi_axes,
            radius_m=rng.radius_m,
        )
        if moved.overlaps(polygon):
            return t
    return None


def test_first_touch_range_matches_dense_scan_for_ellipses():
    # 축별 σ·대각선 접근·꼭짓점 근처·오목 다각형을 섞은 무작위 경우를 1 ms 표본 오라클과 대조한다
    rng = random.Random(198)
    polygons = [
        SQUARE,
        [[3.0, 4.0], [5.0, 4.0], [5.0, 6.0], [3.0, 6.0], [3.0, 5.6], [4.0, 5.6], [4.0, 4.4], [3.0, 4.4]],
        [[3.0, 4.0], [4.5, 5.0], [3.0, 6.0]],
    ]
    checked = touched = 0
    for _ in range(40):
        polygon = rng.choice(polygons)
        r = HorizontalRange.of(
            (rng.uniform(1.0, 6.0), rng.uniform(3.0, 7.0)), (rng.uniform(0.0, 0.2), rng.uniform(0.0, 0.2))
        )
        if r.overlaps(polygon):
            continue
        # 구역 쪽 ±60° 로 향하게 해 접근·꼭짓점 스침·비켜 감이 고루 나오게 한다
        px = sum(v[0] for v in polygon) / len(polygon)
        py = sum(v[1] for v in polygon) / len(polygon)
        heading = math.atan2(py - r.center[1], px - r.center[0]) + rng.uniform(-1.05, 1.05)
        speed = rng.uniform(0.3, 3.0)
        velocity = (speed * math.cos(heading), speed * math.sin(heading))
        got = first_touch_range_s(r, velocity, polygon, limit_s=2.5)
        want = _dense_range_touch(r, velocity, polygon, 2.5)
        checked += 1
        if want is None:
            if got is not None:  # 1 ms 사이 스침은 해석해만 잡는다. 그때 접촉 시각의 간격은 0 이어야 한다
                moved = HorizontalRange(
                    center=(r.center[0] + velocity[0] * got, r.center[1] + velocity[1] * got),
                    semi_axes=r.semi_axes,
                    radius_m=r.radius_m,
                )
                assert moved.gap_to(polygon) == pytest.approx(0.0, abs=1e-6)
            continue
        touched += 1
        assert got is not None and want - 1.5e-3 <= got <= want + 1e-9, (polygon, r, velocity)
    assert checked >= 25 and touched >= 10


def test_first_touch_range_ellipse_vertex_and_edge_cases():
    # 1) 타원 꼭짓점(x 반축 끝)이 구역 변에 닿는 정면 접근
    r = HorizontalRange.of((1.0, 5.0), ANISO)
    t = first_touch_range_s(r, (1.0, 0.0), SQUARE, limit_s=2.5)
    assert t == pytest.approx(3.0 - 1.0 - 0.1 * 1.0021 - 0.3, abs=1e-3)
    # 2) 구역 꼭짓점 (3, 4.5) 이 타원 변에 닿는 대각선 접근(반대 속도 경로)
    r2 = HorizontalRange.of((2.0, 3.5), ANISO)
    t2 = first_touch_range_s(r2, (0.5, 0.5), SQUARE, limit_s=5.0)
    want2 = _dense_range_touch(r2, (0.5, 0.5), SQUARE, 5.0)
    assert t2 is not None and want2 - 1.5e-3 <= t2 <= want2 + 1e-9
    # 3) 그 꼭짓점을 비켜 나란히 지나감
    assert first_touch_range_s(HorizontalRange.of((2.0, 4.0), ANISO), (0.0, -1.0), SQUARE, limit_s=2.5) is None
    # 4) 지금 걸리면 0
    assert first_touch_range_s(HorizontalRange.of((2.75, 5.0), ANISO), (-1.0, 0.0), SQUARE, limit_s=2.5) == 0.0


def test_pending_r5_then_velocity_lost_creates_no_event():
    # 2차 리뷰 R2: 지속 조건을 세는 중(발생 판정 1프레임)에 속도를 잃으면 이벤트 없이 사라지는 것이 의도한 정책이다.
    # 위치는 계속 관측되므로(lifecycle 의 지속 대기 중 관측 상실 발생 대상 아님) 없는 위험을 지어내지 않는다.
    clock = FixedClock(T0)
    engine = RiskEngine([EntryPredictionRule()], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)
    transitions = []
    for dt, velocity in [(0, (1.0, 0.0, 0.0))] + [(dt, None) for dt in range(100, 5100, 500)]:
        t = T0 + dt
        clock.set(t + 100)
        state = world(track(velocity=velocity, t_last_valid=t), t=t)
        step = tracker.apply(engine.evaluate(state, [zone()], Origin.LIVE), state, config_version="cfg-12")
        transitions += step.transitions
    assert transitions == [] and tracker.active() == ()


# --- world_state 2.0.0: UNCERTAIN head_z 값이 계약으로 와도 판정은 1.4.0 때와 같다 (S15P31A101-179) ---------------


@pytest.mark.parametrize("head", [1.5, 1.7])
def test_uncertain_head_z_value_from_v2_keeps_z_unknown_like_v14(head):
    """회귀: 머리 위 VOLUME_3D 구역(z 2.0~4.0)으로 다가가는 사람의 head_z 가 UNCERTAIN. 2.0.0 값(1.5·1.7)은 확정값이 아니므로
    1.4.0(UNCERTAIN + null)과 같이 위 끝을 모르는 Z_UNKNOWN 예측이다(값을 확정값으로 쓰면 판정 없음)."""
    above = zone("VOLUME_3D", [2.0, 4.0], zone_id="z-above")
    v2 = dict(track(head_z=head), head_z_sigma=0.04, head_z_validity="UNCERTAIN")
    v14 = dict(track(head_z=None), head_z_validity="UNCERTAIN")
    (f,) = judge(v2, zones=[above])
    assert f.holding is False and f.reasons == (REASON_PREDICTED_ENTRY,)
    assert f.confidence_reasons == (ConfidenceReason.Z_UNKNOWN,)
    assert judge(v2, zones=[above]) == judge(v14, zones=[above])
    # VALID 값이면 확정 위 끝이라 지금처럼 판정 없음(간격 0.3 m 이상)
    assert judge(dict(track(head_z=head), head_z_validity="VALID"), zones=[above]) == []
