"""체크포인트·저널을 거친 재시작에서 구역 대상(R7) 복원 이벤트의 해소 근거 (명세 3.2, D-G3-09, S15P31A101-174).

복원 보호는 체크포인트·저널에 싣지 않는다. recover → LifecycleTracker.restore 가 복원하는 모든 활성 이벤트에 다시 건다.
그래서 근거 없이 지난 시간이나 해소 유지 시간을 다 채우지 못한 근거는 재시작을 거쳐도 해소로 이어지지 않는다.
"""

from simai_ai.recovery import RecoverySource
from simai_ai.risk import Evaluation, Finding, Origin, Severity, WorldState
from simai_ai.risk.lifecycle import Lifecycle, Verification

ZONE_ID = "z-04"


def r7() -> Finding:
    return Finding(
        rule_id="R7",
        rule_version=1,
        severity=Severity.CAUTION,
        world_ids=(),
        zone_id=ZONE_ID,
        reasons=("monitoring_degraded", "camera_no_signal:cam3"),
        metrics={"healthy_cameras": 1.0, "required_cameras": 2.0, "covering_cameras": 2.0},
    )


def zone_step(site, sd, *, findings=(), evidence=False):
    """site_driver.Site.step 과 같은 판정 한 번. evidence=True 면 R7 이 z-04 의 감시 확보를 확인한 판정이다."""
    site.clock.advance(100)
    t_obs = site.clock.now_ms() - 100
    world = WorldState.from_payload(
        {"t_latest_global": t_obs, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": []}
    )
    evaluation = Evaluation(
        origin=Origin.LIVE,
        evaluated_at=site.clock.now_ms(),
        observation_time=t_obs,
        calibration_version="cal-7",
        scene_version="scene-3",
        rules=(),
        zones=(),
        findings=tuple(findings),
        zone_clear_evidence=frozenset({("R7", ZONE_ID)}) if evidence else frozenset(),
    )
    step = site.tracker.apply(evaluation, world, config_version="cfg-12", evidence_ref=sd.WORLD_STATE_MSG_ID)
    site.journal.append_step(step)
    site.checkpointer.update_inputs(config_version="cfg-12", last_capture_time=t_obs)
    return step


def resolved(steps):
    return [t for s in steps for t in s.transitions if t.transition is Lifecycle.RESOLVED]


def test_two_restarts_through_checkpoint_keep_r7_until_evidence(tmp_path, clock, sd):
    # 첫 실행: R7 발생, 체크포인트
    site = sd.Site(tmp_path, clock)
    (raised,) = zone_step(site, sd, findings=[r7()]).transitions
    assert raised.transition is Lifecycle.RAISED
    zone_step(site, sd, findings=[r7()])
    site.checkpoint()
    site.close()

    # 재시작 1: 근거 없이 60초 → 해소 0건. 해소 유지 시간(200ms)에 못 미친 근거 100ms 를 남기고 체크포인트
    clock.advance(5000)
    site = sd.Site(tmp_path, clock)
    assert site.result.source is RecoverySource.CHECKPOINT
    assert site.tracker.restore_protected() == (raised.event_id,)
    steps = [zone_step(site, sd) for _ in range(600)]
    assert resolved(steps) == []
    steps = [zone_step(site, sd, evidence=True) for _ in range(2)]  # 0ms, 100ms: 아직 해소 아님
    assert resolved(steps) == []
    [state] = site.tracker.active()
    assert state.verification is Verification.VERIFIED  # 근거가 있는 판정에서만 VERIFIED
    site.checkpoint()
    site.close()

    # 재시작 2: 체크포인트에 VERIFIED 로 남아 있어도 다시 UNVERIFIABLE·보호. 근거 없는 판정으로는 해소하지 않는다
    clock.advance(5000)
    site = sd.Site(tmp_path, clock)
    assert site.result.source is RecoverySource.CHECKPOINT
    [state] = site.result.restored
    assert state.event_id == raised.event_id and state.verification is Verification.UNVERIFIABLE
    assert site.tracker.restore_protected() == (raised.event_id,)
    steps = [zone_step(site, sd) for _ in range(600)]
    assert resolved(steps) == []

    # 근거가 생기면 정상 해소(해소 유지 시간 200ms)
    steps = [zone_step(site, sd, evidence=True) for _ in range(3)]
    [cleared] = resolved(steps)
    assert cleared.event_id == raised.event_id and site.tracker.active() == ()
    assert sd.journal_truth(tmp_path, clock) == {}
    site.close()


def test_restart_from_journal_only_is_also_protected(tmp_path, clock, sd):
    """체크포인트 없이 저널만으로 복원해도(재적용) 보호가 걸린다."""
    site = sd.Site(tmp_path, clock)
    (raised,) = zone_step(site, sd, findings=[r7()]).transitions
    site.close()

    clock.advance(5000)
    site = sd.Site(tmp_path, clock)
    assert site.result.source is RecoverySource.JOURNAL
    assert site.tracker.restore_protected() == (raised.event_id,)
    assert resolved([zone_step(site, sd) for _ in range(100)]) == []
    site.close()
