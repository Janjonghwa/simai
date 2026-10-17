"""시계 차이 운영 시나리오: 시간 모델 → System Health → Risk 생명주기까지 연결 (명세 3.9, 6.1, 6.4, 6.5).

- 영상 정상 + 시계 교환 실패(유지 상한 전/후) + 기존 위험 + 신규 진입 (리뷰 F3, Lead 정책)
- Tier 0 벽시계 역행·전진 뒤에도 판정 루프·활성 이벤트·경보 유지 (리뷰 F4)
"""

import importlib.util
from pathlib import Path

import pytest

from simai_ai.risk import (
    AlertClear,
    Finding,
    Lifecycle,
    LifecycleTracker,
    LocalAlert,
    ObservationLossHold,
    Origin,
    RiskEngine,
    RuleContext,
    RuleSpec,
    Verification,
    WorldState,
    Zone,
)
from simai_ai.risk.rules.r6_observation_loss import point_in_polygon
from simai_ai.tracking.clock_sync import build_clock_sync_response
from simai_ai.tracking.decision_time import ClockAwareVerification, DecisionClock
from simai_ai.tracking.system_health import (
    CalibrationState,
    HealthReason,
    ProgressWatchdog,
    SystemHealth,
    evaluate_health,
)
from simai_ai.tracking.time_model import CameraClock, ClockOffset, ObservationState, TrackTimeInput, evaluate_track

_spec = importlib.util.spec_from_file_location("clock_sim", Path(__file__).with_name("test_clock_sync.py"))
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

ZONE = Zone.from_json(
    {
        "zone_id": "z-04",
        "zone_version": "zone-1",
        "judge_type": "GROUND_FOOTPRINT",
        "polygon_xy": [[3.0, 4.5], [4.0, 4.5], [4.0, 5.5], [3.0, 5.5]],
        "z_range": None,
        "severity": "CRITICAL",
    }
)
INSIDE = (3.4, 5.0)
OUTSIDE = (6.0, 5.0)
CAMS = ("cam1", "cam2", "cam3", "cam4")
PROCESSING_MS = 100  # 촬영 → 판정 처리 지연(참 시각)


class InsideZoneRule:
    """시험용 구역 규칙(R1 흉내): OBSERVED·PREDICTED 사람의 위치가 구역 안이면 구역 등급으로 판정한다."""

    spec = RuleSpec(rule_id="R1", rule_version=1, reads=("tracks.position_xy", "zones"))

    def evaluate(self, ctx: RuleContext):
        for track in ctx.world.tracks:
            if track.observation_state.value == "UNKNOWN":
                continue
            for zone in ctx.zones:
                if point_in_polygon(track.position_xy, zone.polygon_xy):
                    yield Finding("R1", 1, zone.severity, (track.world_id,), zone.zone_id, reasons=("inside",))


def _world_track(state, t_last_valid):
    """시간 모델 결과를 world_state 의 world_track 으로 옮긴다(시험용 최소 매핑)."""
    observed = state.observation_state is ObservationState.OBSERVED
    return {
        "world_id": state.world_id,
        "t_last_valid": t_last_valid,
        "observation_state": str(state.observation_state),
        "track_lifecycle": "CONFIRMED" if observed else "OCCLUDED",
        "identity_confidence": "HIGH",
        "identity_revision": 0,
        "position_xy": list(state.position_xy),
        "position_xy_sigma": list(state.position_xy_sigma),
        "pelvis_xyz": None,
        "ground_anchor_xyz": None,
        "support_surface_id": None,
        "support_surface_validity": "UNKNOWN",
        "contact_validity": "UNAVAILABLE",
        "z_validity": "UNAVAILABLE",
        "head_z": None,
        "cameras": ["cam1", "cam3"] if observed else [],
    }


class Pipeline:
    """Tier 0 한 판정 주기: 시계 교환 → 시간 모델 → System Health → Risk Engine·생명주기 (10 Hz)."""

    def __init__(self, laptop_offset_ms):
        self.world = sim.World(laptop_offset_ms=laptop_offset_ms)
        self.clock, self.channel, self.sync = sim.make(self.world)
        self.decision = DecisionClock(self.clock)
        self.engine = RiskEngine([ObservationLossHold(InsideZoneRule())], self.decision)
        self.tracker = LifecycleTracker(
            origin=Origin.LIVE,
            gateway_id="tier0-01",
            clock=self.decision,
            verification=ClockAwareVerification(
                lambda: self.sync.capture_clock(self.link_ok), generation_source=lambda: self.sync.generation
            ),
        )
        self.watchdog = ProgressWatchdog()
        self.laptop_answers = True
        self.link_ok = True  # 노트북 I0 제어 채널 (영상 채널은 별도라 끊겨도 프레임은 온다)
        self.decision_times = []

    def lose_link(self):
        self.link_ok = False
        self.sync.link_lost()

    def restore_link(self):
        self.link_ok = True
        self.laptop_answers = True

    def exchange_if_due(self):
        if not self.link_ok or not self.sync.request_due():
            return
        if self.laptop_answers:
            sim.exchange(self.world, self.sync, self.channel, up_ms=15, hold_ms=1, down_ms=15)
        else:
            self.sync.send_request()  # 노트북이 답하지 않는다(연결은 살아 있음)

    def tick(self, positions):
        """positions: {world_id: (x, y)}. 모든 사람이 최신 프레임에서 관측된다(영상 정상)."""
        self.exchange_if_due()
        self.world.advance(100)
        capture = sim.math.floor(self.world.laptop_float() - PROCESSING_MS)  # 노트북 시계 촬영 시각
        t_risk_now = self.decision.now_ms()
        self.decision_times.append(t_risk_now)
        offset = self.sync.capture_clock(self.link_ok)
        cams = [CameraClock(c, capture, True) for c in CAMS]
        self.watchdog.observe(capture, self.clock.now().mono_ns)
        health = evaluate_health(
            cams,
            CalibrationState.CALIBRATED,
            self.link_ok,
            True,
            t_risk_now,
            capture_clock=offset,
            progress_age_ms=self.watchdog.age_ms(self.clock.now().mono_ns),
        )
        states = [
            evaluate_track(TrackTimeInput(wid, capture, xy, (0.05, 0.05)), capture, t_risk_now, capture_clock=offset)
            for wid, xy in positions.items()
        ]
        world = WorldState.from_payload(
            {
                "t_latest_global": capture,
                "calibration_version": "cal-1",
                "scene_version": "scene-1",
                "tracks": [_world_track(s, capture) for s in states],
            }
        )
        step = self.tracker.apply(self.engine.evaluate(world, [ZONE], Origin.LIVE), world, config_version="cfg-1")
        return health, states, step


def _raised(step):
    return [t.state.world_ids for t in step.transitions if t.transition is Lifecycle.RAISED]


def _resolved(step):
    return [t.state.world_ids for t in step.transitions if t.transition is Lifecycle.RESOLVED]


def _alerts(step):
    return [s for s in step.local_signals if isinstance(s, LocalAlert)]


def _clears(step):
    return [s for s in step.local_signals if isinstance(s, AlertClear)]


def _run(p, positions, n=3):
    """n 틱 실행하고 발생·경보를 모은다. 기본 3틱은 발생 지속 조건(3프레임 중 2프레임, 6.4)을 채운다."""
    raised, alerts = [], []
    for _ in range(n):
        health, states, step = p.tick(positions)
        raised += _raised(step)
        alerts += _alerts(step)
    return health, states, raised, alerts


@pytest.mark.parametrize("laptop_offset", [300.0, -37_000.0])
def test_exchange_failure_before_and_after_hold_limit(laptop_offset):
    p = Pipeline(laptop_offset)

    # A. 교환 정상: 기존 위험(w-1 구역 안) 발생, 경보
    health, _, raised, alerts = _run(p, {"w-1": INSIDE, "w-2": OUTSIDE})
    assert raised == [("w-1",)] and len(alerts) == 1
    assert health.system_health is SystemHealth.NOMINAL
    for _ in range(17):
        health, _, step = p.tick({"w-1": INSIDE, "w-2": OUTSIDE})
    assert health.system_health is SystemHealth.NOMINAL
    assert health.lag_ms == pytest.approx(PROCESSING_MS, abs=1)  # 보정 lag (raw lag 는 시계 차이만큼 틀림)

    # B. 노트북이 응답하지 않는다(연결은 살아 있음). 유지 상한(60s) 전에는 나이 든 추정으로 계속 판정
    p.laptop_answers = False
    for _ in range(300):  # 30s
        health, states, step = p.tick({"w-1": INSIDE, "w-2": OUTSIDE})
        assert _resolved(step) == []
    assert p.sync.health_field(True) is not None  # 나이 든 추정과 커진 불확실성을 보고
    assert health.system_health is SystemHealth.NOMINAL
    assert all(s.time_reference for s in states)
    health, _, raised, alerts = _run(p, {"w-1": INSIDE, "w-2": INSIDE})  # 신규 진입
    assert raised == [("w-2",)] and len(alerts) == 1

    # C. 유지 상한 초과: offset null, UNSAFE_UNKNOWN. 관측 상태는 gap 기준 그대로, 신규 진입은 계속 잡고 해소는 막는다
    for _ in range(320):  # 32s 더
        health, states, step = p.tick({"w-1": INSIDE, "w-2": INSIDE})
    assert p.sync.health_field(True) is None
    assert health.system_health is SystemHealth.UNSAFE_UNKNOWN
    assert HealthReason.CLOCK_OFFSET_UNKNOWN in health.reasons and health.lag_ms is None
    assert all(s.observation_state is ObservationState.OBSERVED and not s.time_reference for s in states)
    health, _, raised, alerts = _run(p, {"w-1": INSIDE, "w-2": INSIDE, "w-3": INSIDE})  # 신규 진입
    assert raised == [("w-3",)] and len(alerts) == 1
    for _ in range(50):  # w-1 이 구역을 나가 5s(해소 유지 3s 초과): 해소 확정 금지
        health, _, step = p.tick({"w-1": OUTSIDE, "w-2": INSIDE, "w-3": INSIDE})
        assert _resolved(step) == []
    w1 = next(s for s in p.tracker.active() if s.world_ids == ("w-1",))
    assert w1.verification is Verification.UNVERIFIABLE

    # D. 교환 재개 → 시계 차이를 다시 알면 관측으로 해소를 확인한다
    p.laptop_answers = True
    resolved = []
    for _ in range(40):
        health, _, step = p.tick({"w-1": OUTSIDE, "w-2": INSIDE, "w-3": INSIDE})
        resolved += _resolved(step)
    assert health.system_health is SystemHealth.NOMINAL
    assert resolved == [("w-1",)]
    assert {s.world_ids for s in p.tracker.active()} == {("w-2",), ("w-3",)}


def _break_time_reference(p, state):
    """절대 시각을 믿을 수 없는 상태로 만든다. 돌려주는 값은 기대하는 Health 원인."""
    if state == "inconsistent":  # 응답 중단 + 노트북 시계 forward step: 유지 중인 추정이 촬영 시각과 맞지 않는다
        p.laptop_answers = False
        p.world.laptop_offset_ms += 5000
        return HealthReason.CLOCK_OFFSET_INCONSISTENT
    if state == "hold_expired":  # 연결은 살아 있고 응답만 유지 상한(60s)을 넘김
        p.laptop_answers = False
        for _ in range(610):
            p.tick({"w-1": INSIDE})
        return HealthReason.CLOCK_OFFSET_UNKNOWN
    if state == "link_lost":  # 제어 채널 실제 단절(영상은 계속)
        p.lose_link()
        return HealthReason.CLOCK_OFFSET_UNKNOWN
    raise AssertionError(state)


@pytest.mark.parametrize("state", ["inconsistent", "hold_expired", "link_lost"])
def test_no_resolution_while_time_reference_is_untrusted(state):
    """절대 시각을 믿을 수 없는 모든 상태에서 해소 0건·ALERT_CLEAR 0건, 신규 진입 경보는 유지, 복구 뒤 해소."""
    p = Pipeline(0.0)
    for _ in range(10):
        _, _, step = p.tick({"w-1": INSIDE})
    assert len(p.tracker.active()) == 1
    reason = _break_time_reference(p, state)

    resolved, clears, raised, alerts = [], [], [], []
    for k in range(50):  # w-1 이 구역 밖에서 5s(해소 유지 3s 초과) 관측, 1s 뒤 w-2 신규 진입
        positions = {"w-1": OUTSIDE, "w-2": INSIDE if k >= 10 else OUTSIDE}
        health, states, step = p.tick(positions)
        assert health.system_health is SystemHealth.UNSAFE_UNKNOWN and reason in health.reasons
        assert all(s.observation_state is ObservationState.OBSERVED and not s.time_reference for s in states)
        resolved += _resolved(step)
        clears += [s for s in step.local_signals if isinstance(s, AlertClear)]
        raised += _raised(step)
        alerts += _alerts(step)
    assert resolved == [] and clears == []
    assert raised == [("w-2",)] and len(alerts) == 1
    w1 = next(s for s in p.tracker.active() if s.world_ids == ("w-1",))
    assert w1.verification is Verification.UNVERIFIABLE

    # 복구: 교환 재개(필요하면 재접속) → 시간 대응이 맞으면 유효 관측으로 해소 유지 시간을 다시 채운 뒤 해소
    p.restore_link()
    resolved, clears = [], []
    for _ in range(50):
        health, _, step = p.tick({"w-1": OUTSIDE, "w-2": INSIDE})
        resolved += _resolved(step)
        clears += [s for s in step.local_signals if isinstance(s, AlertClear)]
    assert health.system_health is SystemHealth.NOMINAL
    assert resolved == [("w-1",)] and len(clears) == 1
    assert [s.world_ids for s in p.tracker.active()] == [("w-2",)]


@pytest.mark.parametrize("step_ms", [5000.0, 60_000.0])
@pytest.mark.parametrize("outside_ticks", [1, 10])
def test_laptop_forward_step_detected_by_exchange_does_not_skip_clear_hold(step_ms, outside_ticks):
    """노트북 시계가 앞으로 뛴 직후 교환이 먼저 점프를 감지해도, 해소 유지 시간(3s)을 점프 폭만큼 건너뛰지 않는다."""
    p = Pipeline(0.0)
    _run(p, {"w-1": INSIDE}, 10)
    assert len(p.tracker.active()) == 1
    for _ in range(outside_ticks):  # 구역 밖 관측으로 해소 확인 시작
        _, _, step = p.tick({"w-1": OUTSIDE})
        assert _resolved(step) == []
    while not p.sync.request_due():  # 다음 틱에 교환이 일어나는 시점까지
        _, _, step = p.tick({"w-1": OUTSIDE})
        assert _resolved(step) == []
    generation = p.sync.generation
    p.world.laptop_offset_ms += step_ms

    health, _, step = p.tick({"w-1": OUTSIDE})  # 이 틱의 교환이 점프를 감지하고 offset 을 다시 맞춘다
    assert p.sync.generation == generation + 1
    assert health.system_health is SystemHealth.NOMINAL
    assert _resolved(step) == [] and _clears(step) == []

    resolved_at = None
    for k in range(1, 60):  # 점프 뒤 관측으로 유지 시간을 처음부터 다시 채운다
        _, _, step = p.tick({"w-1": OUTSIDE})
        if _resolved(step):
            assert _resolved(step) == [("w-1",)] and len(_clears(step)) == 1
            resolved_at = k
            break
    assert resolved_at is not None and 29 <= resolved_at <= 31  # 약 3s (10 Hz)


@pytest.mark.parametrize("step_ms", [-2000.0, -60_000.0, 2000.0])
def test_tier0_wall_step_keeps_decision_loop_and_events(step_ms):
    p = Pipeline(300.0)
    for _ in range(10):
        _, _, step = p.tick({"w-1": INSIDE, "w-2": OUTSIDE})
    assert len(p.tracker.active()) == 1

    p.world.tier0_wall_err_ms += step_ms
    raised, alerts, resolved = [], [], []
    positions = {"w-1": INSIDE, "w-2": OUTSIDE}
    for k in range(int(abs(step_ms) / 100) + 50):  # 고정 구간이 끝나고도 5s 더
        if k == 5:
            positions = {"w-1": INSIDE, "w-2": INSIDE}  # 역행 직후 신규 진입
        _, _, step = p.tick(positions)
        raised += _raised(step)
        alerts += _alerts(step)
        resolved += _resolved(step)
    assert p.decision_times == sorted(p.decision_times)  # 판정 시각은 앞으로만
    [change] = p.decision.epoch_changes
    # 직전 판정 시각 대비 역행 폭(그 사이 지난 한 주기 남짓만큼 작다). 앞으로 뛰면 0
    assert max(-step_ms, 0) - 200 <= change.held_ms <= max(-step_ms, 0)
    assert raised == [("w-2",)] and len(alerts) == 1
    assert resolved == []
    assert {s.world_ids for s in p.tracker.active()} == {("w-1",), ("w-2",)}
    est = p.sync.estimate()  # 시계 차이는 새 기준점으로 옮겨져 있다
    assert est.offset_ms == pytest.approx(300.0 - step_ms, abs=2)


def test_responder_output_is_used_by_pipeline():
    """파이프라인 시험의 노트북 응답도 실제 응답 함수로 만든다(시뮬레이터 exchange 가 이 함수를 쓴다)."""
    assert sim.build_clock_sync_response is build_clock_sync_response


def test_generation_change_blocks_each_event_once():
    """세대가 바뀌면 이벤트마다 첫 확인 한 번만 False. 새로 본 이벤트는 막지 않는다."""

    class Always:
        def verifiable(self, key, world_ids, world, now_ms):
            return True

    generation = [0]
    v = ClockAwareVerification(lambda: ClockOffset(0.0, 10.0), base=Always(), generation_source=lambda: generation[0])
    w = WorldState.from_payload(
        {"t_latest_global": 1_000, "calibration_version": "c", "scene_version": "s", "tracks": []}
    )
    assert v.verifiable("a", (), w, 1_100) and v.verifiable("b", (), w, 1_100)
    generation[0] += 1
    assert not v.verifiable("a", (), w, 1_200) and not v.verifiable("b", (), w, 1_200)
    assert v.verifiable("a", (), w, 1_300) and v.verifiable("b", (), w, 1_300)
    assert v.verifiable("c", (), w, 1_300)  # 세대가 바뀐 뒤 처음 본 이벤트
