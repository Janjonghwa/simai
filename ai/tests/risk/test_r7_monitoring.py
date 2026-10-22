"""R7 구역 감시 능력 저하 (명세 7.2 R7, 6.2, 6.5, 7.3, 15.1 G3)."""

import itertools
import random

import pytest

from simai_ai.risk import (
    CameraMonitor,
    CameraStatus,
    Channel,
    Confidence,
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    MonitoringState,
    Origin,
    RiskEngine,
    RuleContext,
    Severity,
    WorldState,
    Zone,
    ZoneMonitoringRule,
    channels_for,
    default_registry,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.tracking.system_health import CalibrationState, evaluate_health
from simai_ai.tracking.time_model import CameraClock, TimeModelConfig

T0 = 1760342400100

ZONE = Zone.from_json(
    {
        "zone_id": "z-04",
        "zone_version": "zone-1",
        "judge_type": "GROUND_FOOTPRINT",
        "polygon_xy": [[3.0, 4.5], [4.0, 4.5], [4.0, 5.5], [3.0, 5.5]],
        "z_range": None,
        "severity": "CAUTION",
    }
)


def world(t_latest_global=T0, tracks=()):
    return WorldState(
        t_latest_global=t_latest_global, calibration_version="cal-7", scene_version="scene-3", tracks=tracks
    )


def monitoring(cameras, coverage=None):
    return MonitoringState(
        cameras=tuple(CameraMonitor(cid, CameraStatus(status), last) for cid, status, last in cameras),
        zone_cameras={"z-04": ("cam1", "cam3")} if coverage is None else coverage,
    )


def judge(mon, t_latest_global=T0, zones=(ZONE,), rule=None):
    rule = ZoneMonitoringRule() if rule is None else rule
    ctx = RuleContext(now_ms=t_latest_global + 100, world=world(t_latest_global), zones=zones, monitoring=mon)
    return list(rule.evaluate(ctx))


def test_two_healthy_cameras_keep_the_zone_monitored():
    assert judge(monitoring([("cam1", "OK", T0), ("cam3", "OK", T0 - 400)])) == []


def test_zone_target_finding_is_system_caution_without_people():
    [f] = judge(monitoring([("cam1", "OK", T0), ("cam3", "NO_SIGNAL", T0 - 50)]))
    assert (f.rule_id, f.rule_version, f.world_ids, f.zone_id) == ("R7", 1, (), "z-04")
    # 구역 등급과 무관하게 시스템 CAUTION (7.2). 시스템 상태는 확실하므로 확인 필요가 아니다.
    assert f.severity is Severity.CAUTION and not f.needs_check and not f.holding
    assert f.reasons == ("monitoring_degraded", "camera_no_signal:cam3")
    assert dict(f.metrics) == {"healthy_cameras": 1.0, "required_cameras": 2.0, "covering_cameras": 2.0}


@pytest.mark.parametrize(
    ("age", "degraded"),
    [(0, False), (500, False), (501, True), (5000, True)],
)
def test_stall_threshold_matches_system_health(age, degraded):
    findings = judge(monitoring([("cam1", "OK", T0), ("cam3", "OK", T0 - age)]))
    assert bool(findings) is degraded
    if degraded:
        assert findings[0].reasons == ("monitoring_degraded", "camera_stalled:cam3")


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("NO_SIGNAL", "camera_no_signal:cam3"),
        ("WAITING_KEYFRAME", "camera_waiting_keyframe:cam3"),
        ("SYNC_INVALID", "camera_sync_invalid:cam3"),
    ],
)
def test_camera_status_faults(status, reason):
    [f] = judge(monitoring([("cam1", "OK", T0), ("cam3", status, T0)]))
    assert f.reasons == ("monitoring_degraded", reason)


def test_ok_camera_without_frames_is_not_healthy():
    [f] = judge(monitoring([("cam1", "OK", T0), ("cam3", "OK", None)]))
    assert f.reasons == ("monitoring_degraded", "camera_no_frame:cam3")


def test_unknown_inputs_are_not_treated_as_monitored():
    # 상태 보고에 없는 카메라
    [f] = judge(monitoring([("cam1", "OK", T0)]))
    assert f.reasons == ("monitoring_degraded", "camera_unreported:cam3")
    # 구역의 관측 카메라 목록 없음
    [f] = judge(monitoring([("cam1", "OK", T0), ("cam3", "OK", T0)], coverage={}))
    assert f.reasons == ("monitoring_degraded", "coverage_unknown")
    assert "covering_cameras" not in f.metrics
    # 감시 상태 입력 자체가 없음
    [f] = judge(None)
    assert f.reasons == ("monitoring_degraded", "monitoring_unknown")


def test_zone_seen_by_one_camera_by_design_is_degraded():
    [f] = judge(monitoring([("cam1", "OK", T0)], coverage={"z-04": ("cam1",)}))
    assert f.reasons == ("monitoring_degraded", "coverage_insufficient")
    assert f.metrics["covering_cameras"] == 1.0


def test_three_covering_cameras_tolerate_one_fault():
    cams = [("cam1", "OK", T0), ("cam2", "NO_SIGNAL", T0), ("cam3", "OK", T0)]
    assert judge(monitoring(cams, coverage={"z-04": ("cam1", "cam2", "cam3")})) == []


def test_parameters_are_validated():
    with pytest.raises(RiskInputError):
        ZoneMonitoringRule(min_cameras=0)
    with pytest.raises(RiskInputError):
        ZoneMonitoringRule(camera_stall_ms=-1)
    assert judge(monitoring([("cam1", "OK", T0)]), rule=ZoneMonitoringRule(min_cameras=1)) == []


def test_default_registry_builds_r7():
    rule = default_registry().create("R7", 1)
    assert isinstance(rule, ZoneMonitoringRule)


# --- 채널(7.3)·출처(7.4) ---------------------------------------------------------


def test_r7_channels_follow_the_73_table():
    """R7 은 관제 화면 안내만 쓴다. 현장 소리·관제 화면 경보·반장 PWA 로 보내지 않는다(7.2, 7.3)."""
    [f] = judge(monitoring([("cam1", "OK", T0), ("cam3", "NO_SIGNAL", T0)]))
    confidence = Confidence.LOW if f.needs_check else Confidence.HIGH
    plan = channels_for(f.severity, confidence, Origin.LIVE)
    assert Channel.CONTROL_SCREEN_NOTICE in plan.channels
    assert not plan.channels & {Channel.LOCAL_ALERT, Channel.CONTROL_SCREEN_ALERT, Channel.FOREMAN_PWA}
    for origin in (Origin.PLANNED, Origin.WHAT_IF, Origin.REPLAY):
        assert channels_for(f.severity, confidence, origin).channels == frozenset()


@pytest.mark.parametrize(
    ("origin", "expected"),
    [(Origin.LIVE, 1), (Origin.REPLAY, 1), (Origin.PLANNED, 0), (Origin.WHAT_IF, 0)],
)
def test_missing_monitoring_is_unknown_only_for_camera_origins(origin, expected):
    """감시 상태가 없을 때: LIVE·REPLAY 는 '모름'(저하), PLANNED·WHAT_IF 는 카메라 개념이 없어 판정하지 않는다."""
    engine = RiskEngine([ZoneMonitoringRule()], FixedClock(T0 + 100))
    evaluation = engine.evaluate(world(), [ZONE], origin)
    assert len(evaluation.findings) == expected
    assert all(f.reasons == ("monitoring_degraded", "monitoring_unknown") for f in evaluation.findings)


def test_rule_context_without_camera_concept_skips_r7_only_when_monitoring_missing():
    ctx = RuleContext(now_ms=T0 + 100, world=world(), zones=(ZONE,), monitoring_expected=False)
    assert list(ZoneMonitoringRule().evaluate(ctx)) == []
    # 가상 카메라 장애 시나리오처럼 감시 상태를 넘기면 같은 기준으로 판정한다
    mon = monitoring([("cam1", "OK", T0)])
    ctx = RuleContext(now_ms=T0 + 100, world=world(), zones=(ZONE,), monitoring=mon, monitoring_expected=False)
    [f] = ZoneMonitoringRule().evaluate(ctx)
    assert f.reasons == ("monitoring_degraded", "camera_unreported:cam3")


@pytest.mark.parametrize("origin", list(Origin))
def test_alert_candidates_only_for_live(origin):
    clock = FixedClock(T0 + 100)
    engine = RiskEngine([ZoneMonitoringRule()], clock)
    evaluation = engine.evaluate(world(), [ZONE], origin, monitoring=monitoring([("cam1", "OK", T0)]))
    assert len(evaluation.findings) == 1
    assert len(evaluation.alert_candidates()) == (1 if origin is Origin.LIVE else 0)


# --- G3: 카메라 분리 → 구역 저하 표시 1초 이내, 복구 시 해소 ---------------------------------


def _run_disconnect(origin, tick_ms=100):
    """10Hz 판정. cam3 의 마지막 프레임이 T0 이고 그 뒤로 프레임이 없다(상태 보고는 OK 그대로)."""
    clock = FixedClock(T0)
    engine = RiskEngine([ZoneMonitoringRule()], clock)
    tracker = LifecycleTracker(origin=origin, gateway_id="tier0-01", clock=clock)
    raised_at = None
    signals = []
    for k in range(1, 31):
        t = T0 + k * tick_ms
        clock.set(t + 50)  # 판정 지연 50ms
        mon = monitoring([("cam1", "OK", t), ("cam3", "OK", T0)])
        evaluation = engine.evaluate(world(t), [ZONE], origin, monitoring=mon)
        step = tracker.apply(evaluation, world(t), config_version="cfg-12")
        signals += step.local_signals
        if raised_at is None and any(tr.transition is Lifecycle.RAISED for tr in step.transitions):
            raised_at = clock.now_ms()
    return raised_at, signals, tracker, clock, engine


def test_camera_disconnect_is_shown_within_one_second():
    raised_at, signals, tracker, _, _ = _run_disconnect(Origin.LIVE)
    assert raised_at is not None and raised_at - T0 <= 1000
    [state] = tracker.active()
    assert (state.rule_id, state.zone_id, state.world_ids, state.severity) == ("R7", "z-04", (), Severity.CAUTION)
    assert signals == [] and not state.local_alerted  # 관제 화면 안내만, 현장 경보 없음


@pytest.mark.parametrize("origin", [Origin.PLANNED, Origin.WHAT_IF, Origin.REPLAY])
def test_non_live_disconnect_makes_events_but_no_alerts(origin):
    raised_at, signals, tracker, _, _ = _run_disconnect(origin)
    assert raised_at is not None and len(tracker.active()) == 1
    assert signals == []


def test_recovery_resolves_after_clear_hold():
    _, _, tracker, clock, engine = _run_disconnect(Origin.LIVE)
    t_back = clock.now_ms()
    resolved = None
    for k in range(1, 40):
        t = t_back + k * 100
        clock.set(t + 50)
        mon = monitoring([("cam1", "OK", t), ("cam3", "OK", t)])
        step = tracker.apply(
            engine.evaluate(world(t), [ZONE], Origin.LIVE, monitoring=mon), world(t), config_version="c"
        )
        if any(tr.transition is Lifecycle.RESOLVED for tr in step.transitions):
            resolved = t
            assert step.local_signals == ()  # 현장 경보를 내지 않았으므로 해제 신호도 없다
            break
    assert resolved is not None
    assert tracker.active() == ()


def test_whole_pipeline_stall_does_not_resolve_r7():
    """영상 전체가 멈추면(lag 초과) 구역 이벤트도 해소를 확인하지 않는다(lifecycle 검증 정책)."""
    _, _, tracker, clock, engine = _run_disconnect(Origin.LIVE)
    t_frozen = T0 + 3000
    mon = monitoring([("cam1", "OK", t_frozen), ("cam3", "OK", t_frozen)])
    for k in range(1, 60):
        clock.set(t_frozen + 1000 + k * 100)  # 판정 시계만 간다
        step = tracker.apply(
            engine.evaluate(world(t_frozen), [ZONE], Origin.LIVE, monitoring=mon), world(t_frozen), config_version="c"
        )
        assert all(tr.transition is not Lifecycle.RESOLVED for tr in step.transitions)
    assert len(tracker.active()) == 1


# --- System Health 와 일관 -------------------------------------------------------


_STATUS_TO_SYNC = {"OK": True, "NO_SIGNAL": True, "WAITING_KEYFRAME": True, "SYNC_INVALID": False}


def test_r7_healthy_cameras_are_a_subset_of_system_health_active_cameras():
    """R7 이 정상으로 센 카메라는 System Health 도 수신 중으로 본다(R7 이 더 엄격할 수는 있다)."""
    rng = random.Random(73)
    config = TimeModelConfig()
    for _ in range(500):
        cams = []
        for cid in ("cam1", "cam2", "cam3", "cam4"):
            status = rng.choice(list(_STATUS_TO_SYNC))
            last = rng.choice([None, T0 - rng.randint(0, 1200)])
            cams.append((cid, status, last))
        times = [last for _, status, last in cams if _STATUS_TO_SYNC[status] and last is not None]
        if not times:
            continue
        t_latest_global = max(times)
        report = evaluate_health(
            [CameraClock(cid, last, _STATUS_TO_SYNC[status]) for cid, status, last in cams],
            CalibrationState.CALIBRATED,
            gateway_link_ok=True,
            ec2_link_ok=True,
            t_risk_now=t_latest_global + 100,
            config=config,
        )
        coverage = {f"z-{a}-{b}": (a, b) for a, b in itertools.combinations(("cam1", "cam2", "cam3", "cam4"), 2)}
        zones = tuple(
            Zone(zone_id, "zone-1", ZONE.judge_type, ZONE.polygon_xy, None, Severity.HIGH) for zone_id in coverage
        )
        rule = ZoneMonitoringRule(min_cameras=config.min_cameras, camera_stall_ms=config.camera_stall_ms)
        degraded = {f.zone_id for f in judge(monitoring(cams, coverage), t_latest_global, zones, rule)}
        active = set(report.active_cameras)
        for zone_id, pair in coverage.items():
            if zone_id not in degraded:
                assert set(pair) <= active
            # 상태가 OK·SYNC_INVALID 뿐이면(상태 보고가 끊김을 아직 모름) 두 판단이 같다.
            if all(status in ("OK", "SYNC_INVALID") for cid, status, _ in cams if cid in pair):
                assert (zone_id not in degraded) == (set(pair) <= active)


# --- 입력 형식 -------------------------------------------------------------------


def test_monitoring_from_health_camera_items():
    mon = MonitoringState.from_json(
        {
            "cameras": [
                {"id": "cam3", "fps": 9.8, "last_capture": None, "sync_offset_ms": None, "status": "NO_SIGNAL"},
                {"id": "cam1", "fps": 10.0, "last_capture": T0, "sync_offset_ms": 2.0, "status": "OK"},
            ],
            "zone_cameras": {"z-04": ["cam3", "cam1"]},
        }
    )
    assert [c.camera_id for c in mon.cameras] == ["cam1", "cam3"]
    assert mon.zone_cameras["z-04"] == ("cam1", "cam3")
    assert mon.camera("cam3").last_capture is None and mon.camera("cam9") is None


@pytest.mark.parametrize(
    "obj",
    [
        {"cameras": [], "zone_cameras": {}, "extra": 1},
        {"cameras": [{"id": "cam1", "status": "BROKEN", "last_capture": T0}], "zone_cameras": {}},
        {"cameras": [{"id": "cam1", "status": "OK", "last_capture": 1.5}], "zone_cameras": {}},
        {"cameras": [{"id": "cam1", "status": "OK"}], "zone_cameras": {}},
        {"cameras": [], "zone_cameras": {"z-04": ["cam1", "cam1"]}},
        {
            "cameras": [
                {"id": "cam1", "status": "OK", "last_capture": T0},
                {"id": "cam1", "status": "OK", "last_capture": T0},
            ],
            "zone_cameras": {},
        },
        {"cameras": []},
    ],
)
def test_monitoring_rejects_bad_input(obj):
    with pytest.raises(RiskInputError):
        MonitoringState.from_json(obj)


# --- 융합에서 뺀 카메라 (S15P31A101-219, docs/design/tier0-fusion-time.md) ---


def _excluded(*ids, coverage=None, last=T0 - 400):
    cams = (CameraMonitor("cam1", CameraStatus.OK, T0),) + tuple(
        CameraMonitor(c, CameraStatus.OK, last, fusion_excluded=True) for c in ids)
    return MonitoringState(cameras=cams, zone_cameras={"z-04": ("cam1", "cam3")} if coverage is None else coverage)


def test_camera_left_out_of_fusion_is_not_a_healthy_monitor():
    """수신 중(400 ms, 중단 기준 500 ms 안)이어도 늦어서 융합에서 뺀 카메라는 그 구역을 관측하지 못한다: 저하로 발생하고
    감시 확보 근거(clear_zones)도 아니다. 같은 카메라가 융합에 쓰이면 정상이다(위 test_two_healthy_cameras_…)."""
    rule = ZoneMonitoringRule()
    mon = _excluded("cam3")
    [f] = judge(mon)
    assert f.reasons == ("monitoring_degraded", "camera_fusion_excluded:cam3")
    assert f.metrics["healthy_cameras"] == 1.0
    ctx = RuleContext(now_ms=T0 + 100, world=world(T0), zones=(ZONE,), monitoring=mon)
    assert list(rule.clear_zones(ctx)) == []
    three = _excluded("cam3", coverage={"z-04": ("cam1", "cam2", "cam3")})
    three = MonitoringState(cameras=three.cameras + (CameraMonitor("cam2", CameraStatus.OK, T0),),
                            zone_cameras=three.zone_cameras)
    assert judge(three) == []  # 남은 2대가 보는 구역은 그대로 감시된다


def test_clock_offset_camera_can_push_normal_cameras_out_and_r7_shows_it():
    """리뷰 F2 반례: 한 대(cam1)만 +140 ms 앞서고 정상 세 대가 40 ms 위상차면, 앞선 cam1 과 cam2 가 남아(2대라 되돌림 없음)
    정상 cam3·cam4 가 빠진다. 동기화 측정이 없는 동안 이 제외는 막지 못하지만, cam3·cam4 로만 2대 관측하는 구역은 R7 저하로
    드러나고 감시 복구 근거가 되지 않는다."""
    from simai_ai.tracking.time_model import choose_fusion_time

    clocks = [CameraClock("cam1", T0, True), CameraClock("cam2", T0 - 140, True),
              CameraClock("cam3", T0 - 160, True), CameraClock("cam4", T0 - 180, True)]
    fusion = choose_fusion_time(clocks, TimeModelConfig())
    assert fusion is not None and fusion.lagging == ("cam3", "cam4")
    mon = MonitoringState(
        cameras=tuple(CameraMonitor(c.camera_id, CameraStatus.OK, c.last_capture, c.camera_id in fusion.lagging)
                      for c in clocks),
        zone_cameras={"z-04": ("cam3", "cam4")})
    [f] = judge(mon)
    assert f.reasons == ("monitoring_degraded", "camera_fusion_excluded:cam3", "camera_fusion_excluded:cam4")
