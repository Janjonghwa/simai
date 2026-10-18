"""복원한 구역 대상(R7) 이벤트의 해소 근거 (명세 3.2 재시작 복구, 6.4, 부록 D-G3-09, S15P31A101-174).

재시작 뒤 restore 로 넣은 구역 대상 이벤트는 사람이 없어 재관측으로 해소를 확인할 수 없다. 판정이 없는 것만으로, 또는
규칙·구역이 판정 구성에서 빠진 동안에는 해소를 세지 않고, R7 이 그 구역의 정상 관측 카메라 확보를 확인한 판정
(Evaluation.zone_clear_evidence)에서만 VERIFIED 로 본다. 사람 대상 복원 이벤트는 그대로다. 복원하지 않은 구역 대상 이벤트의
근거 요구(S15P31A101-180)는 test_zone_clear_evidence.py 에 있다.
"""

import pytest

from simai_ai.risk import (
    CameraMonitor,
    CameraStatus,
    Evaluation,
    Finding,
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
    ZoneClearEvidence,
    ZoneEntryRule,
    ZoneMonitoringRule,
)
from simai_ai.risk.lifecycle import Change, Verification

T0 = 1760342400100
MSG = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
HOLD_MS = 3000  # LifecyclePolicy 기본 해소 유지 시간


def _zone(zone_id: str, x0: float) -> Zone:
    return Zone.from_json(
        {
            "zone_id": zone_id,
            "zone_version": "zone-1",
            "judge_type": "GROUND_FOOTPRINT",
            "polygon_xy": [[x0, 4.5], [x0 + 1.0, 4.5], [x0 + 1.0, 5.5], [x0, 5.5]],
            "z_range": None,
            "severity": "CAUTION",
        }
    )


ZONE = _zone("z-04", 3.0)
OTHER = _zone("z-05", 6.0)
COVERAGE = {"z-04": ("cam1", "cam3"), "z-05": ("cam1", "cam3")}


def world(t: int, tracks=()) -> WorldState:
    return WorldState(t_latest_global=t, calibration_version="cal-7", scene_version="scene-3", tracks=tuple(tracks))


def healthy(t: int, coverage=None) -> MonitoringState:
    return MonitoringState(
        cameras=(CameraMonitor("cam1", CameraStatus.OK, t), CameraMonitor("cam3", CameraStatus.OK, t)),
        zone_cameras=COVERAGE if coverage is None else coverage,
    )


def degraded(t: int) -> MonitoringState:
    return MonitoringState(
        cameras=(CameraMonitor("cam1", CameraStatus.OK, t), CameraMonitor("cam3", CameraStatus.NO_SIGNAL, t - 50)),
        zone_cameras=COVERAGE,
    )


class Site:
    """10Hz 판정 하나(판정 지연 10ms). 엔진의 규칙·구역·감시 상태를 단계마다 바꿀 수 있다(구성 전환 흉내)."""

    def __init__(self, clock: FixedClock, tracker: LifecycleTracker, t: int) -> None:
        self.clock = clock
        self.tracker = tracker
        self.t = t
        self.transitions = []

    def tick(self, *, rules=(ZoneMonitoringRule(),), zones=(ZONE,), monitoring="healthy", tracks=(), lag_ms=10):
        self.t += 100
        self.clock.set(self.t + lag_ms)
        mon = {"healthy": healthy, "degraded": degraded}.get(monitoring, lambda _t: monitoring)(self.t)
        engine = RiskEngine(rules, self.clock)
        evaluation = engine.evaluate(world(self.t, tracks), zones, Origin.LIVE, monitoring=mon)
        step = self.tracker.apply(evaluation, world(self.t, tracks), config_version="cfg-12", evidence_ref=MSG)
        self.transitions += step.transitions
        return step

    def run(self, n: int, **kwargs) -> None:
        for _ in range(n):
            self.tick(**kwargs)

    def resolved(self):
        return [t for t in self.transitions if t.transition is Lifecycle.RESOLVED]


def _tracker(clock: FixedClock) -> LifecycleTracker:
    return LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=clock)


def _restarted_with_r7(*, extra_findings=(), tracks=()) -> Site:
    """재시작 전 z-04 에 R7 이벤트(그리고 extra_findings)를 만들고, 새 추적기로 복원한 판정 루프를 돌려준다."""
    clock = FixedClock(T0)
    before = Site(clock, _tracker(clock), T0)
    before.tick(monitoring="degraded", tracks=tracks)
    for _ in range(2 if extra_findings else 0):  # 사람 대상은 발생 지속 조건(3프레임 중 2)을 채운다
        evaluation = Evaluation(
            origin=Origin.LIVE,
            evaluated_at=before.t + 110,
            observation_time=before.t + 100,
            calibration_version="cal-7",
            scene_version="scene-3",
            rules=(),
            zones=(),
            findings=tuple(extra_findings),
        )
        clock.set(before.t + 110)
        before.tracker.apply(evaluation, world(before.t + 100, tracks), config_version="cfg-12", evidence_ref=MSG)
        before.t += 100
    states = before.tracker.active()
    assert any(s.rule_id == "R7" for s in states)
    clock.set(before.t + 5000)
    after = _tracker(clock)
    after.restore(states)
    return Site(clock, after, before.t + 5000)


def _r7(tracker: LifecycleTracker):
    return [s for s in tracker.active() if s.rule_id == "R7"]


# --- 리뷰 재현: 근거 없이 오래 남는다 ---------------------------------------------------------------


def test_restored_r7_without_any_evidence_is_never_resolved():
    """리뷰 재현(S15P31A101-54 리뷰 2절): 규칙·구역·판정이 모두 없는 판정을 60초 넣어도 해소·VERIFIED 0건."""
    clock = FixedClock(T0)
    pre = _tracker(clock)
    finding = Finding(
        rule_id="R7", rule_version=1, severity=Severity.CAUTION, world_ids=(), zone_id="z", reasons=("x",)
    )
    pre.apply(
        Evaluation(Origin.LIVE, T0 + 10, T0, "c", "s", (), (), (finding,)), world(T0), config_version="cfg"
    )
    post = _tracker(clock)
    post.restore(pre.active())
    for k in range(1, 601):
        t = T0 + 100 * k
        clock.set(t + 10)
        step = post.apply(Evaluation(Origin.LIVE, t + 10, t, "c", "s", (), (), ()), world(t), config_version="cfg")
        assert step.transitions == ()
    [state] = post.active()
    assert state.verification is Verification.UNVERIFIABLE
    assert post.restore_protected() == (state.event_id,)


def test_restored_r7_stays_while_zone_is_still_degraded():
    site = _restarted_with_r7()
    site.run(600, monitoring="degraded")
    [state] = _r7(site.tracker)
    assert site.resolved() == [] and state.verification is Verification.UNVERIFIABLE
    # 저하 판정이 계속 와도 검증 상태가 VERIFIED 로 바뀌는 전이가 없다(감시 확보 근거가 아니다)
    assert all(Change.VERIFICATION not in t.changes for t in site.transitions)


# --- 근거가 생기면 정상 해소 ----------------------------------------------------------------------


def test_restored_r7_resolves_after_cameras_return_for_clear_hold():
    site = _restarted_with_r7()
    site.run(30, monitoring="degraded")
    first = site.tick()  # 카메라 복구: 첫 근거
    assert [t.changes for t in first.transitions] == [(Change.VERIFICATION,)]
    assert first.transitions[0].state.verification is Verification.VERIFIED
    t_back = site.t
    while not site.resolved():
        site.tick()
        assert site.t - t_back <= HOLD_MS
    assert site.t - t_back == HOLD_MS  # 복원하지 않은 이벤트와 같은 해소 유지 시간
    assert _r7(site.tracker) == [] and site.tracker.restore_protected() == ()


def test_evidence_must_last_the_whole_hold_and_restarts_on_degradation():
    site = _restarted_with_r7()
    site.run(20)  # 근거 2초
    site.tick(monitoring="degraded")  # 다시 저하 → 해소 확인 처음부터
    site.run(30)  # 첫 근거 판정부터 2.9초
    assert site.resolved() == []
    site.run(1)
    assert len(site.resolved()) == 1


# --- 구성 전환으로 규칙·구역이 빠진 동안 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rules": ()},  # 구성에서 R7 이 빠짐
        {"rules": (ZoneEntryRule(),)},  # 다른 규칙만 남음
        {"zones": ()},  # 구역이 빠짐
        {"zones": (OTHER,)},  # 다른 구역만 남음(그 구역의 근거는 z-04 근거가 아니다)
        {"monitoring": None},  # 감시 상태 입력 없음(R7 은 monitoring_unknown 판정)
        {"monitoring": MonitoringState(cameras=(), zone_cameras={})},  # 관측 카메라 목록 모름
    ],
    ids=["no-rules", "other-rule", "no-zone", "other-zone", "no-monitoring", "coverage-unknown"],
)
def test_missing_rule_zone_or_input_is_not_evidence(kwargs):
    site = _restarted_with_r7()
    site.run(600, **kwargs)
    [state] = _r7(site.tracker)
    assert site.resolved() == [] and state.verification is Verification.UNVERIFIABLE


def test_config_switch_mid_hold_restarts_count():
    """근거 2.9초 → R7 이 구성에서 10초 빠짐(해소 없음) → 돌아오면 해소 유지 시간을 처음부터 다시 센다."""
    site = _restarted_with_r7()
    site.run(30)
    site.run(100, rules=())
    assert site.resolved() == []
    [state] = _r7(site.tracker)
    assert state.verification is Verification.UNVERIFIABLE
    t_back = site.t
    while not site.resolved():
        site.tick()
    assert site.t - t_back == HOLD_MS + 100  # 첫 근거 판정에서 VERIFIED, 그 뒤 3초


def test_lag_still_blocks_evidence():
    """근거가 있어도 입력이 늦으면(lag 초과) 해소를 세지 않는다(기존 검증 정책을 대신하지 않고 더한다)."""
    site = _restarted_with_r7()
    site.run(100, lag_ms=700)
    assert site.resolved() == [] and _r7(site.tracker)[0].verification is Verification.UNVERIFIABLE


# --- 사람 대상 복원 이벤트는 그대로 ----------------------------------------------------


def test_restored_person_event_keeps_reobservation_policy(track_json):
    """사람 대상 복원 이벤트는 구역 근거와 무관하게 대상 World ID 재관측으로 확인한다(기존 정책)."""
    track = WorldState.from_payload(
        {"t_latest_global": T0, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": [track_json()]}
    ).tracks[0]
    wid = track.world_id
    r1 = Finding(rule_id="R1", rule_version=1, severity=Severity.HIGH, world_ids=(wid,), zone_id="z-04", reasons=())
    site = _restarted_with_r7(extra_findings=(r1,))

    def observed(t):
        return (track.__class__(**{**track.__dict__, "t_last_valid": t}),)

    # 재관측 전: 둘 다 남는다
    site.run(100, monitoring="degraded")
    assert {s.rule_id for s in site.tracker.active()} == {"R1", "R7"}
    # 같은 World ID 재관측(구역은 여전히 저하): R1 만 기존대로 해소된다
    for _ in range(40):
        site.tick(monitoring="degraded", tracks=observed(site.t + 100))
    assert [t.state.rule_id for t in site.resolved()] == ["R1"]
    assert [s.rule_id for s in site.tracker.active()] == ["R7"]


def test_resumed_event_after_restored_resolution_is_normal():
    """복원 이벤트가 근거로 해소된 뒤 재개(같은 event_id)하면 새 활성이라 복원 보호가 없다."""
    site = _restarted_with_r7()
    site.run(31)
    [resolved] = site.resolved()
    site.tick(monitoring="degraded")
    [state] = _r7(site.tracker)
    assert state.event_id == resolved.event_id and site.tracker.restore_protected() == ()


# --- 엔진·R7 의 근거 -----------------------------------------------------------------------------


def test_r7_clear_zones_only_for_known_healthy_coverage():
    rule = ZoneMonitoringRule()
    assert isinstance(rule, ZoneClearEvidence)

    def clear(mon, zones=(ZONE, OTHER)):
        return list(rule.clear_zones(RuleContext(now_ms=T0 + 10, world=world(T0), zones=zones, monitoring=mon)))

    assert clear(healthy(T0)) == ["z-04", "z-05"]
    assert clear(degraded(T0)) == []
    assert clear(None) == []
    assert clear(healthy(T0, coverage={"z-04": ("cam1", "cam3")})) == ["z-04"]
    assert clear(healthy(T0, coverage={"z-04": ("cam1",)})) == []  # 설치상 1대: 저하


def test_engine_collects_evidence_for_active_zones_without_findings():
    clock = FixedClock(T0 + 10)
    mon = MonitoringState(
        cameras=(CameraMonitor("cam1", CameraStatus.OK, T0), CameraMonitor("cam3", CameraStatus.OK, T0)),
        zone_cameras={"z-04": ("cam1", "cam3"), "z-05": ("cam1",)},
    )
    engine = RiskEngine([ZoneMonitoringRule()], clock)
    evaluation = engine.evaluate(world(T0), [ZONE, OTHER], Origin.LIVE, monitoring=mon)
    assert [f.zone_id for f in evaluation.findings] == ["z-05"]
    assert evaluation.zone_clear_evidence == frozenset({("R7", "z-04")})
    # R7 이 없는 구성은 근거가 없다
    without_r7 = RiskEngine([], clock).evaluate(world(T0), [ZONE], Origin.LIVE, monitoring=mon)
    assert without_r7.zone_clear_evidence == frozenset()


class _LyingRule:
    """판정을 낸 구역과 구성에 없는 구역까지 근거로 주장하는 규칙(엔진이 걸러야 한다)."""

    def __init__(self) -> None:
        self.spec = ZoneMonitoringRule().spec

    def evaluate(self, ctx):
        yield Finding(
            rule_id="R7", rule_version=1, severity=Severity.CAUTION, world_ids=(), zone_id="z-04", reasons=("x",)
        )

    def clear_zones(self, ctx):
        return ["z-04", "z-05", "z-99"]


def test_engine_drops_evidence_that_conflicts_with_findings_or_inactive_zones():
    evaluation = RiskEngine([_LyingRule()], FixedClock(T0 + 10)).evaluate(world(T0), [ZONE, OTHER], Origin.LIVE)
    assert evaluation.zone_clear_evidence == frozenset({("R7", "z-05")})
