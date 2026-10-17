"""노트북–Pod 시계 차이 추정 시험 (명세 3.9, 6.1, FR-139, 부록 D-G0-12).

참 시각을 가진 가상 세계에서 Tier 0 단조·벽시계와 노트북 시계를 따로 움직여 교환을 흉내 낸다.
설계 허용치(docs/design/tier0-clock-offset.md):
- 모든 경우 |추정 offset - 참 offset| <= uncertainty_ms (구간 보장).
- 대칭 경로 오차 <= 1ms(양자화), 지터 시나리오(고정 시드) 오차 <= 5ms.
- 보정한 T_lag 의 오차 <= uncertainty_ms + 1ms(반올림).
"""

import copy
import json
import math
import random
from pathlib import Path

import pytest

from simai_ai.tracking.clock_sync import (
    ClockOffsetEstimate,
    ClockSyncConfig,
    ClockSyncMessageError,
    ResponseOutcome,
    Tier0Clock,
    Tier0ClockSync,
    build_clock_sync_request,
    build_clock_sync_response,
    compute_offset_ms,
    compute_round_trip_ms,
    laptop_clock_offset_field,
)
from simai_ai.tracking.system_health import CalibrationState, HealthReason, SystemHealth, evaluate_health
from simai_ai.tracking.time_model import CameraClock, LagStatus, estimate_lag

T0 = 1_760_342_400_000
EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"


class World:
    """참 시각과 세 시계. 시각은 참 경과(ns) 하나로 움직인다."""

    def __init__(self, laptop_offset_ms: float = 0.0, laptop_drift_ppm: float = 0.0, tier0_wall_err_ms: float = 0.0):
        self.elapsed_ns = 0
        self.mono_base = 7_000_000_000_000
        self.laptop_offset_ms = laptop_offset_ms
        self.laptop_drift_ppm = laptop_drift_ppm
        self.tier0_wall_err_ms = tier0_wall_err_ms

    def advance(self, ms: float) -> None:
        self.elapsed_ns += round(ms * 1_000_000)

    @property
    def true_ms(self) -> float:
        return T0 + self.elapsed_ns / 1_000_000

    def tier0_mono(self) -> int:
        return self.mono_base + self.elapsed_ns

    def tier0_wall(self) -> int:
        return math.floor(self.true_ms + self.tier0_wall_err_ms)

    def laptop_float(self) -> float:
        drift = self.laptop_drift_ppm * 1e-6 * (self.elapsed_ns / 1_000_000)
        return self.true_ms + self.laptop_offset_ms + drift

    def laptop_ms(self) -> int:
        return math.floor(self.laptop_float())


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def _ids():
    n = 0

    def new():
        nonlocal n
        n += 1
        return f"00000000-0000-4000-8000-{n:012x}"

    return new


def make(world: World, config: ClockSyncConfig = ClockSyncConfig()):
    clock = Tier0Clock(world.tier0_mono, world.tier0_wall)
    channel = Recorder()
    sync = Tier0ClockSync(clock, channel, source_id="tier0-01", config=config, new_uuid=_ids())
    return clock, channel, sync


def exchange(world, sync, channel, up_ms=20.0, hold_ms=1.0, down_ms=20.0):
    sync.send_request()
    request = channel.sent[-1]
    world.advance(up_ms)
    t2 = world.laptop_ms()
    world.advance(hold_ms)
    t3 = world.laptop_ms()
    response = build_clock_sync_response(request, t2_ms=t2, t3_ms=t3, msg_id=_ids()(), source_id="laptop-01")
    world.advance(down_ms)
    return sync.handle_response(response)


def true_offset(world: World, clock: Tier0Clock) -> float:
    """참 offset = 노트북 시계 - Tier 0 시계(현재 기준점의 UTC)."""
    return world.laptop_float() - clock.to_utc_ms(world.tier0_mono())


def check_bound(world, clock, est: ClockOffsetEstimate) -> float:
    err = est.offset_ms - true_offset(world, clock)
    assert abs(err) <= est.uncertainty_ms, (err, est)
    assert est.uncertainty_ms >= est.round_trip_ms / 2
    return err


# --- 공식 ---


def test_formula_matches_contract_definition():
    req = json.loads((EXAMPLES / "clock_sync_request.json").read_text(encoding="utf-8"))["payload"]
    resp = json.loads((EXAMPLES / "clock_sync_response.json").read_text(encoding="utf-8"))["payload"]
    t1, t2, t3 = req["t1_ms"], resp["t2_ms"], resp["t3_ms"]
    t4 = t1 + 20
    assert compute_offset_ms(t1, t2, t3, t4) == ((t2 - t1) + (t3 - t4)) / 2
    assert compute_round_trip_ms(t1, t2, t3, t4) == (t4 - t1) - (t3 - t2) == 19


# --- 고정 offset, 비대칭, 지터 ---


@pytest.mark.parametrize("offset", [0.0, 250.0, -1234.0, 37_000.0])
def test_fixed_offset_symmetric_path(offset):
    world = World(laptop_offset_ms=offset)
    clock, channel, sync = make(world)
    for _ in range(5):
        assert exchange(world, sync, channel).outcome is ResponseOutcome.ACCEPTED
        world.advance(1000)
    est = sync.estimate()
    assert abs(check_bound(world, clock, est)) <= 1.0
    assert est.round_trip_ms == pytest.approx(40.0, abs=1.0)
    assert est.sample_count == 5


def test_asymmetric_delay_error_within_uncertainty():
    world = World(laptop_offset_ms=100.0)
    clock, channel, sync = make(world)
    exchange(world, sync, channel, up_ms=5, down_ms=45)
    est = sync.estimate()
    err = check_bound(world, clock, est)
    assert err == pytest.approx((5 - 45) / 2, abs=1.0)  # 비대칭의 절반이 오차, 구간이 그것을 덮는다


def test_jitter_uses_min_round_trip_sample():
    rng = random.Random(160)
    world = World(laptop_offset_ms=-420.0, laptop_drift_ppm=20.0)
    clock, channel, sync = make(world)
    worst = 0.0
    for _ in range(60):
        up = 15 + rng.expovariate(1 / 20)
        down = 15 + rng.expovariate(1 / 20)
        assert exchange(world, sync, channel, up_ms=up, hold_ms=rng.uniform(0, 3), down_ms=down).outcome == "ACCEPTED"
        est = sync.estimate()
        worst = max(worst, abs(check_bound(world, clock, est)))
        world.advance(1000 - up - down)
    est = sync.estimate()
    assert abs(check_bound(world, clock, est)) <= 5.0
    assert est.round_trip_ms < 40  # 바닥 30ms 근처 표본을 골랐다
    assert sync.estimator.laptop_jumps == 0
    assert worst <= 40


# --- 노트북 시계 점프 ---


@pytest.mark.parametrize("step", [500.0, -300.0, 5000.0, -60_000.0])
def test_laptop_clock_jump_resets_window(step):
    world = World(laptop_offset_ms=10.0)
    clock, channel, sync = make(world)
    for _ in range(20):
        exchange(world, sync, channel, up_ms=10, down_ms=10)  # 왕복 짧은 옛 표본
        world.advance(1000)
    world.laptop_offset_ms += step
    result = exchange(world, sync, channel, up_ms=40, down_ms=40)  # 왕복이 더 긴 새 표본
    assert result.outcome is ResponseOutcome.ACCEPTED and result.laptop_jump
    est = sync.estimate()
    check_bound(world, clock, est)
    assert est.sample_count == 1
    assert est.offset_ms == pytest.approx(10.0 + step, abs=1.5)


def test_drift_does_not_trigger_false_jump():
    world = World(laptop_offset_ms=5.0, laptop_drift_ppm=80.0)
    clock, channel, sync = make(world)
    exchange(world, sync, channel, up_ms=2, down_ms=2)  # 아주 좁은 첫 표본
    for _ in range(58):
        world.advance(1000)
        exchange(world, sync, channel, up_ms=30, down_ms=30)
        check_bound(world, clock, sync.estimate())
    assert sync.estimator.laptop_jumps == 0


# --- Tier 0 벽시계 점프·역행 ---


@pytest.mark.parametrize("step", [1000.0, -2000.0])
def test_tier0_wall_jump_rebases_without_losing_samples(step):
    world = World(laptop_offset_ms=50.0)
    clock, channel, sync = make(world)
    for _ in range(3):
        exchange(world, sync, channel)
        world.advance(1000)
    world.tier0_wall_err_ms += step
    before = clock.epoch
    est = sync.estimate()  # now() 가 점프를 보고 기준점을 다시 잡는다
    assert clock.epoch == before + 1
    assert est.sample_count == 3
    check_bound(world, clock, est)
    assert est.offset_ms == pytest.approx(50.0 - step, abs=1.0)


def test_tier0_wall_jump_during_exchange_keeps_round_trip():
    world = World(laptop_offset_ms=0.0)
    clock, channel, sync = make(world)
    sync.send_request()
    request = channel.sent[-1]
    world.advance(20)
    t2 = world.laptop_ms()
    response = build_clock_sync_response(request, t2_ms=t2, t3_ms=t2, msg_id=_ids()(), source_id="laptop-01")
    world.tier0_wall_err_ms -= 3000  # 응답이 오는 사이 Tier 0 벽시계가 거꾸로 감
    world.advance(20)
    result = sync.handle_response(response)
    assert result.outcome is ResponseOutcome.ACCEPTED
    assert result.sample.round_trip_ms == pytest.approx(40.0)
    est = sync.estimate()
    check_bound(world, clock, est)
    assert est.offset_ms == pytest.approx(3000.0, abs=1.0)


def test_small_wall_jitter_is_absorbed_and_time_never_goes_back():
    world = World()
    clock, _, _ = make(world)
    seen = [clock.now().utc_ms]
    for err in (-10, 20, -30, 5):
        world.tier0_wall_err_ms = err
        world.advance(1)
        seen.append(clock.now().utc_ms)
    assert clock.epoch == 0
    assert seen == sorted(seen)


def test_monotonic_going_back_is_an_error():
    world = World()
    clock, _, _ = make(world)
    clock.now()
    world.elapsed_ns -= 1
    with pytest.raises(ValueError, match="단조"):
        clock.now()


# --- 표본 없음·오래됨·끊김 ---


def test_no_samples_means_null():
    world = World(laptop_offset_ms=100.0)
    _, _, sync = make(world)
    assert sync.estimate() is None
    assert sync.health_field(True) is None
    assert sync.capture_clock(True) is None


def test_late_exchange_holds_aged_estimate_until_limit_then_null():
    """연결은 살아 있고 교환만 늦다: 유지 상한까지 마지막 추정을 표류만큼 넓혀 쓰고, 넘으면 비운다."""
    world = World(laptop_offset_ms=30.0, laptop_drift_ppm=90.0)
    clock, channel, sync = make(world)
    for _ in range(3):
        exchange(world, sync, channel)
        world.advance(1000)
    fresh = sync.estimate()
    world.advance(ClockSyncConfig().offset_hold_ms - 1000)  # 마지막 표본 나이 = offset_hold_ms
    aged = sync.estimate()
    assert aged is not None
    check_bound(world, clock, aged)  # 표류 90ppm × 60s = 5.4ms 를 넓힌 불확실성이 덮는다
    assert aged.uncertainty_ms == pytest.approx(fresh.uncertainty_ms + 100e-6 * 59_000, abs=0.01)
    assert aged.estimated_at == fresh.estimated_at
    assert sync.health_field(True) == aged.to_contract()  # 연결이 살아 있으면 나이 든 추정을 보고
    world.advance(1)
    assert sync.estimate() is None
    assert sync.health_field(True) is None
    assert len(sync.estimator) == 0  # 창(= 유지 상한) 밖 표본은 estimate() 가 지운다


@pytest.mark.parametrize("step", [10.0, -10.0, 20.0, -20.0])
def test_small_laptop_step_inside_overlap_stays_within_uncertainty(step):
    """좁은 옛 표본 + 넓은 새 표본 사이의 작은 노트북 시계 점프: 점프로 알아채지 못해도 불확실성이 참값을 덮는다."""
    world = World()
    clock, channel, sync = make(world)
    exchange(world, sync, channel, up_ms=2, down_ms=2)  # 왕복 4ms
    world.advance(1000)
    world.laptop_offset_ms += step
    result = exchange(world, sync, channel, up_ms=20, down_ms=20)  # 왕복 40ms, 구간이 옛 표본과 겹친다
    assert result.outcome is ResponseOutcome.ACCEPTED and not result.laptop_jump
    est = sync.estimate()
    assert est.sample_count == 2 and est.round_trip_ms == pytest.approx(4.0)
    check_bound(world, clock, est)
    for _ in range(5):  # 이후에도 넓은 표본만 와도 계속 덮는다
        world.advance(1000)
        exchange(world, sync, channel, up_ms=20, down_ms=20)
        check_bound(world, clock, sync.estimate())


def test_link_lost_clears_estimate_and_pending():
    world = World()
    _, channel, sync = make(world)
    exchange(world, sync, channel)
    world.advance(500)
    assert not sync.request_due()
    sync.send_request()
    late_request = channel.sent[-1]
    sync.link_lost()
    assert sync.estimate() is None
    assert sync.request_due()  # 재접속하면 바로 보낸다
    response = build_clock_sync_response(late_request, t2_ms=T0, t3_ms=T0, msg_id=_ids()(), source_id="laptop-01")
    assert sync.handle_response(response).outcome is ResponseOutcome.UNKNOWN_EXCHANGE


def test_health_field_is_null_when_gateway_link_down():
    world = World(laptop_offset_ms=-12.0)
    _, channel, sync = make(world)
    exchange(world, sync, channel)
    assert sync.health_field(True) is not None
    assert sync.health_field(False) is None
    assert sync.capture_clock(False) is None
    assert laptop_clock_offset_field(None, True) is None


# --- 응답 거르기 ---


def _response(request, t2, t3):
    return build_clock_sync_response(request, t2_ms=t2, t3_ms=t3, msg_id=_ids()(), source_id="laptop-01")


def test_response_filters():
    world = World()
    _, channel, sync = make(world)

    sync.send_request()
    req = channel.sent[-1]
    world.advance(10)
    resp = _response(req, world.laptop_ms(), world.laptop_ms())
    world.advance(10)
    assert sync.handle_response(resp).outcome is ResponseOutcome.ACCEPTED
    assert sync.handle_response(resp).outcome is ResponseOutcome.UNKNOWN_EXCHANGE  # 중복

    sync.send_request()
    req = channel.sent[-1]
    world.advance(ClockSyncConfig().request_timeout_ms + 1)
    assert sync.handle_response(_response(req, T0, T0)).outcome is ResponseOutcome.TIMED_OUT

    sync.send_request()
    req = copy.deepcopy(channel.sent[-1])
    req["payload"]["t1_ms"] += 1
    assert sync.handle_response(_response(req, T0, T0)).outcome is ResponseOutcome.T1_MISMATCH

    sync.send_request()
    resp = _response(channel.sent[-1], T0, T0)
    resp["payload"]["t3_ms"] = T0 - 1
    assert sync.handle_response(resp).outcome is ResponseOutcome.INVALID

    sync.send_request()
    req = channel.sent[-1]
    world.advance(200)
    assert sync.handle_response(_response(req, T0, T0 + 101)).outcome is ResponseOutcome.HOLD_TOO_LONG

    sync.send_request()  # 왕복 20ms 인데 노트북 처리 시간이 80ms: 교환 중 노트북 시계가 뛴 경우
    req = channel.sent[-1]
    world.advance(20)
    assert sync.handle_response(_response(req, T0, T0 + 80)).outcome is ResponseOutcome.NEGATIVE_ROUND_TRIP

    assert sync.handle_response({"msg_type": "clock_sync_response"}).outcome is ResponseOutcome.INVALID
    assert sync.handle_response({"msg_type": "health", "payload": {}}).outcome is ResponseOutcome.INVALID
    assert sync.estimator.__len__() == 1


def test_pending_requests_are_bounded():
    world = World()
    config = ClockSyncConfig(max_pending=3)
    _, channel, sync = make(world, config)
    for _ in range(5):
        sync.send_request()
    first = channel.sent[0]
    assert sync.handle_response(_response(first, T0, T0)).outcome is ResponseOutcome.UNKNOWN_EXCHANGE
    assert sync.handle_response(_response(channel.sent[-1], T0, T0)).outcome is ResponseOutcome.ACCEPTED


def test_failed_send_does_not_leave_pending():
    class Broken:
        def send(self, message):
            raise ConnectionError("끊김")

    world = World()
    sync = Tier0ClockSync(Tier0Clock(world.tier0_mono, world.tier0_wall), Broken(), source_id="tier0-01")
    with pytest.raises(ConnectionError):
        sync.send_request()
    assert sync.request_due()


# --- 노트북 응답 함수 ---


def test_responder_echoes_request_and_rejects_bad_input():
    req = build_clock_sync_request(
        exchange_id="7d3f0c2e-5b1a-4c8e-9f6d-2a1b3c4d5e6f", t1_ms=T0, msg_id=_ids()(), source_id="tier0-01"
    )
    resp = build_clock_sync_response(req, t2_ms=T0 + 9, t3_ms=T0 + 10, msg_id=_ids()(), source_id="laptop-01")
    assert resp["payload"] == {
        "exchange_id": req["payload"]["exchange_id"],
        "t1_ms": T0,
        "t2_ms": T0 + 9,
        "t3_ms": T0 + 10,
    }
    assert resp["sent_at"] == T0 + 10 and resp["source_id"] == "laptop-01"
    with pytest.raises(ClockSyncMessageError):
        build_clock_sync_response(req, t2_ms=T0 + 10, t3_ms=T0 + 9, msg_id=_ids()(), source_id="laptop-01")
    with pytest.raises(ClockSyncMessageError):
        build_clock_sync_response({**req, "msg_type": "health"}, t2_ms=T0, t3_ms=T0, msg_id=_ids()(), source_id="x")
    with pytest.raises(ClockSyncMessageError):
        build_clock_sync_response(req, t2_ms=float(T0), t3_ms=T0, msg_id=_ids()(), source_id="x")


# --- 계약 ---


def test_messages_and_health_field_match_contracts(contract_errors):
    world = World(laptop_offset_ms=-12.5)
    _, channel, sync = make(world)
    sync.send_request()
    request = channel.sent[-1]
    assert contract_errors("clock_sync_request", request) == []
    world.advance(21)
    t2 = world.laptop_ms()
    world.advance(1)
    response = build_clock_sync_response(
        request, t2_ms=t2, t3_ms=world.laptop_ms(), msg_id=_ids()(), source_id="laptop-01"
    )
    assert contract_errors("clock_sync_response", response) == []
    world.advance(21)
    assert sync.handle_response(response).outcome is ResponseOutcome.ACCEPTED

    health = json.loads((EXAMPLES / "health.json").read_text(encoding="utf-8"))
    health["payload"]["laptop_clock_offset"] = sync.health_field(True)
    assert health["payload"]["laptop_clock_offset"] is not None
    assert contract_errors("health", health) == []
    health["payload"]["laptop_clock_offset"] = sync.health_field(False)
    assert contract_errors("health", health) == []


# --- T_lag 보정 (명세 6.1) ---

CAMS = ("cam1", "cam2", "cam3", "cam4")


@pytest.mark.parametrize(("laptop_offset", "true_lag"), [(300.0, 150), (-500.0, 150), (0.0, 650), (1200.0, 2100)])
def test_lag_correction_end_to_end(laptop_offset, true_lag):
    """노트북 시계로 붙은 촬영 시각과 Tier 0 시계의 t_risk_now 로 T_lag 를 보정한다."""
    rng = random.Random(int(laptop_offset) + true_lag)
    world = World(laptop_offset_ms=laptop_offset)
    clock, channel, sync = make(world)
    for _ in range(10):
        exchange(world, sync, channel, up_ms=15 + rng.expovariate(0.1), down_ms=15 + rng.expovariate(0.1))
        world.advance(500)

    capture = world.laptop_ms()  # 최신 프레임의 촬영 시각(노트북 시계)
    world.advance(true_lag)
    t_risk_now = clock.now().utc_ms
    offset = sync.capture_clock(True)
    lag = estimate_lag(t_risk_now, capture, offset)
    assert lag.status is LagStatus.CORRECTED
    assert lag.raw_lag_ms == pytest.approx(true_lag - laptop_offset, abs=2)  # 보정 전은 시계 차이만큼 틀린다
    assert abs(lag.lag_ms - true_lag) <= lag.uncertainty_ms + 1

    cams = [CameraClock(c, capture, True) for c in CAMS]
    report = evaluate_health(cams, CalibrationState.CALIBRATED, True, True, t_risk_now, capture_clock=offset)
    assert report.lag_ms == lag.lag_ms and report.raw_lag_ms == lag.raw_lag_ms
    expected = SystemHealth.NOMINAL if true_lag + lag.uncertainty_ms <= 600 else SystemHealth.UNSAFE_UNKNOWN
    if 600 < true_lag + lag.uncertainty_ms <= 2000:
        expected = SystemHealth.DEGRADED
    assert report.system_health is expected

    sync.link_lost()  # 노트북이 끊기면 시계 차이를 모른다
    report = evaluate_health(
        cams, CalibrationState.CALIBRATED, False, True, t_risk_now, capture_clock=sync.capture_clock(False)
    )
    assert report.lag_ms is None
    assert HealthReason.CLOCK_OFFSET_UNKNOWN in report.reasons
    assert HealthReason.GATEWAY_LINK_LOST in report.reasons
