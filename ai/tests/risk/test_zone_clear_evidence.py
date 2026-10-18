"""구역 대상 이벤트 전체의 해소 근거 (명세 6.4, 3.2, S15P31A101-180).

복원하지 않은 구역 대상(R7) 이벤트도 판정이 사라진 판정에서는 R7 의 감시 확보 근거(Evaluation.zone_clear_evidence)가
있을 때만 해소를 센다. 구성 묶음 전환으로 R7·구역이 빠지거나 감시 입력이 없어 판정이 사라진 것은 위험 조건이 사라졌다는
유효 관측이 아니다. R7 이 판정에 남아 있는 정상 경로의 해소 시각·전이, 사람 대상 이벤트, 복원 이벤트(-174)는 그대로다.
"""

import pytest

from simai_ai.risk import (
    CameraMonitor,
    CameraStatus,
    Evaluation,
    Finding,
    FixedClock,
    Lifecycle,
    MonitoringState,
    Origin,
    RiskEngine,
    Severity,
    WorldState,
    ZoneEntryRule,
    ZoneMonitoringRule,
)
from simai_ai.risk.lifecycle import Change, LifecycleTracker, Verification
from tests.risk.test_restored_zone_event import (
    HOLD_MS,
    OTHER,
    T0,
    ZONE,
    Site,
    _r7,
    _restarted_with_r7,
    _tracker,
    world,
)


def _live_r7() -> Site:
    """복원하지 않은 R7 이벤트 하나가 z-04 에 활성인 판정 루프(카메라 1대 끊김으로 발생)."""
    clock = FixedClock(T0)
    site = Site(clock, _tracker(clock), T0)
    [raised] = site.tick(monitoring="degraded").transitions
    assert raised.transition is Lifecycle.RAISED and raised.state.verification is Verification.VERIFIED
    assert site.tracker.restore_protected() == ()
    return site


def _changes(site: Site):
    return [(t.transition, t.changes) for t in site.transitions]


# --- 반례 1: 구성 전환·구역 삭제·입력 없음으로 판정이 사라진 동안 -----------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rules": ()},  # 구성에서 R7 이 빠짐(티켓 재현: 이전에는 3 s 뒤 해소)
        {"rules": (ZoneEntryRule(),)},  # 다른 규칙만 남음
        {"zones": ()},  # R7 은 남고 구역만 빠짐
        {"zones": (OTHER,)},  # 다른 구역만 남음(그 구역의 근거는 z-04 근거가 아니다)
        {"monitoring": None},  # 감시 상태 입력 없음(R7 은 monitoring_unknown 판정으로 유지)
        {"monitoring": MonitoringState(cameras=(), zone_cameras={})},  # 관측 카메라 목록 모름
    ],
    ids=["no-rules", "other-rule", "no-zone", "other-zone", "no-monitoring", "coverage-unknown"],
)
def test_non_restored_zone_event_is_not_resolved_without_evidence(kwargs):
    site = _live_r7()
    site.run(600, **kwargs)  # 60 s
    [state] = _r7(site.tracker)
    assert site.resolved() == []
    if "monitoring" in kwargs:
        # R7 이 계속 저하 판정을 내므로 유지다(기존과 같음). 검증 상태는 판정 중 기존 정책(lag)이다.
        assert state.verification is Verification.VERIFIED
    else:
        # 판정이 사라졌는데 근거가 없다: 해소를 세지 않고 UNVERIFIABLE 로 한 번만 바뀐다.
        assert state.verification is Verification.UNVERIFIABLE
        assert _changes(site) == [
            (Lifecycle.RAISED, (Change.RAISED,)),
            (Lifecycle.UPDATED, (Change.VERIFICATION,)),
        ]


def test_manual_evaluation_without_evidence_does_not_resolve_zone_event():
    """근거 칸을 비운 직접 만든 Evaluation(규칙 없이 판정만 사라짐)으로는 60 s 뒤에도 해소하지 않는다."""
    clock = FixedClock(T0)
    tracker = _tracker(clock)
    r7 = Finding(rule_id="R7", rule_version=1, severity=Severity.CAUTION, world_ids=(), zone_id="z", reasons=("x",))
    clock.set(T0 + 10)
    tracker.apply(Evaluation(Origin.LIVE, T0 + 10, T0, "c", "s", (), (), (r7,)), world(T0), config_version="cfg")
    for k in range(1, 601):
        t = T0 + 100 * k
        clock.set(t + 10)
        step = tracker.apply(Evaluation(Origin.LIVE, t + 10, t, "c", "s", (), (), ()), world(t), config_version="cfg")
        assert all(tr.transition is not Lifecycle.RESOLVED for tr in step.transitions)
    [state] = tracker.active()
    assert state.verification is Verification.UNVERIFIABLE


def test_what_if_without_monitoring_is_not_evidence():
    """WHAT_IF 가상 카메라 장애로 생긴 R7 이벤트는 감시 상태를 빼면(R7 이 판정하지 않음) 근거가 없어 남는다."""
    clock = FixedClock(T0)
    tracker = LifecycleTracker(origin=Origin.WHAT_IF, gateway_id="tier0-01", clock=clock)
    fault = MonitoringState(
        cameras=(CameraMonitor("cam1", CameraStatus.OK, T0), CameraMonitor("cam3", CameraStatus.NO_SIGNAL, T0)),
        zone_cameras={"z-04": ("cam1", "cam3")},
    )
    for k in range(0, 601):
        t = T0 + 100 * k
        clock.set(t + 10)
        evaluation = RiskEngine([ZoneMonitoringRule()], clock).evaluate(
            world(t), [ZONE], Origin.WHAT_IF, monitoring=fault if k == 0 else None
        )
        assert evaluation.zone_clear_evidence == frozenset()
        tracker.apply(evaluation, world(t), config_version="cfg")
    [state] = tracker.active()
    assert state.rule_id == "R7" and state.verification is Verification.UNVERIFIABLE


def test_lag_still_blocks_evidence_for_non_restored():
    """근거가 있어도 입력이 늦으면(lag 초과) 해소를 세지 않는다(근거는 기존 검증 정책에 더하는 조건이다)."""
    site = _live_r7()
    site.run(100, lag_ms=700)
    assert site.resolved() == [] and _r7(site.tracker)[0].verification is Verification.UNVERIFIABLE


# --- 반례 2: 정상 경로(R7 이 판정에 남고 근거가 나옴)는 기존과 같다 ----------------------------------------


def test_normal_path_resolves_at_same_time_with_same_transitions():
    site = _live_r7()
    site.run(30, monitoring="degraded")
    t_back = site.t + 100  # 카메라가 돌아온 첫 판정의 관측 시각
    while not site.resolved():
        site.tick()
        assert site.t - t_back <= HOLD_MS
    assert site.t - t_back == HOLD_MS  # 판정이 사라진 첫 판정부터 해소 유지 시간
    # 전이는 발생과 해소뿐이다(판정 중·근거 확보 중 검증 상태 전이 없음, 이전 동작과 같다)
    assert _changes(site) == [(Lifecycle.RAISED, (Change.RAISED,)), (Lifecycle.RESOLVED, (Change.CLEARED,))]
    assert site.resolved()[0].state.verification is Verification.VERIFIED


def test_evidence_after_config_returns_resolves_after_full_hold():
    """R7 이 10 s 빠짐(해소 없음) → 돌아와 근거가 나오면 그 판정에서 VERIFIED, 그 뒤 해소 유지 시간을 처음부터 센다."""
    site = _live_r7()
    site.run(100, rules=())
    assert site.resolved() == []
    first = site.tick()
    assert [t.changes for t in first.transitions] == [(Change.VERIFICATION,)]
    assert first.transitions[0].state.verification is Verification.VERIFIED
    t_back = site.t
    while not site.resolved():
        site.tick()
    assert site.t - t_back == HOLD_MS


def test_config_switch_mid_hold_restarts_count():
    """근거 2.9 s → R7 이 한 판정 동안 빠짐 → 해소 유지 시간을 처음부터 다시 센다(빠진 판정이 해소를 앞당기지 않는다)."""
    site = _live_r7()
    site.run(30)  # 근거 판정 30개: 관측 시각 2.9 s
    site.tick(rules=())
    assert site.resolved() == []
    site.run(30)  # 새 근거 판정부터 2.9 s
    assert site.resolved() == []
    site.run(1)
    assert len(site.resolved()) == 1


def test_resumed_zone_event_also_needs_evidence():
    """해소 뒤 재개한 이벤트(새 활성, 복원 아님)도 구성에서 R7 이 빠지면 근거 없이 해소하지 않는다."""
    site = _live_r7()
    site.run(31)
    [resolved] = site.resolved()
    resumed = site.tick(monitoring="degraded").transitions
    assert [t.changes for t in resumed] == [(Change.RESUMED,)] and resumed[0].event_id == resolved.event_id
    site.run(600, rules=())
    assert len(site.resolved()) == 1 and _r7(site.tracker)[0].verification is Verification.UNVERIFIABLE


# --- 반례 3: 사람 대상·복원 이벤트는 그대로 -------------------------------------------------------------


def test_person_event_resolves_without_zone_evidence(track_json):
    """사람 대상 이벤트는 구역 근거와 무관하게 대상 재관측으로 해소한다. 같은 판정에서 구역 대상은 남는다."""
    clock = FixedClock(T0)
    tracker = _tracker(clock)
    track = track_json()
    wid = track["world_id"]
    r1 = Finding(rule_id="R1", rule_version=1, severity=Severity.HIGH, world_ids=(wid,), zone_id="z-04", reasons=())
    r7 = Finding(rule_id="R7", rule_version=1, severity=Severity.CAUTION, world_ids=(), zone_id="z-04", reasons=("x",))

    def step(t, findings):
        w = WorldState.from_payload(
            {
                "t_latest_global": t,
                "calibration_version": "cal-7",
                "scene_version": "scene-3",
                "tracks": [track | {"t_last_valid": t}],
            }
        )
        clock.set(t + 10)
        return tracker.apply(
            Evaluation(Origin.LIVE, t + 10, t, "cal-7", "scene-3", (), (), tuple(findings)), w, config_version="cfg"
        )

    for k in range(3):  # R1 은 3프레임 중 2, R7 은 즉시
        step(T0 + 100 * k, [r1, r7])
    resolved = []
    for k in range(3, 3 + 40):
        resolved += [t.state.rule_id for t in step(T0 + 100 * k, []).transitions if t.transition is Lifecycle.RESOLVED]
    assert resolved == ["R1"]
    [state] = tracker.active()
    assert state.rule_id == "R7" and state.verification is Verification.UNVERIFIABLE


def test_restored_zone_event_still_unverifiable_while_degraded():
    """복원 이벤트(-174)는 저하 판정이 오는 동안에도 근거를 요구한다(VERIFIED 로 돌아가는 전이 없음)."""
    site = _restarted_with_r7()
    site.run(100, monitoring="degraded")
    assert all(Change.VERIFICATION not in t.changes for t in site.transitions)
    assert _r7(site.tracker)[0].verification is Verification.UNVERIFIABLE
    assert site.tracker.restore_protected() != ()
