"""PPE-04: 촬영 칸 진행과 실제 시간 만료. 합성 입력·검출, 실제 gate·판정·REPLAY 경로."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from simai_ai.gpu.admission import AdmissionGate, AdmissionPolicy, LoadWindow, marker_section
from simai_ai.ppe.state import PpeJudge
from simai_ai.risk import (
    FixedClock, HelmetRequiredRule, LifecycleTracker, Origin, PpePolicy, PpeStateStore,
    RiskEngine, VestRequiredRule, WorldState,
)

from .test_association_state import HELMET, NO_HELMET, NO_VEST, VEST, feed, person
from .test_service_loop import make_loop, shared


def delayed_stream(tmp_path, *, lag_ms=520, phase_ms=100, cameras=1, candidates=None):
    loop, source, _gate, detector, out, clock = make_loop(
        candidates=(NO_HELMET(), NO_VEST()) if candidates is None else candidates)
    start = 1_760_342_400_000 + phase_ms
    marker = tmp_path / "tier0.live"
    gate = loop.gate = AdmissionGate(marker, AdmissionPolicy(), wall_s=lambda: clock.t / 1000,
                                    monotonic=lambda: (clock.t - start) / 1000)
    cams = tuple(f"cam{i + 1}" for i in range(cameras))
    samples = {cid: [] for cid in cams}
    infer = detector.infer

    def record(image):
        cid = next(cid for cid, value in source.value[1].items() if value is image)
        samples[cid].append((clock.t, source.value[0]["frames"][cid]["capture_time_ms"]))
        return infer(image)

    detector.infer = record
    for elapsed in range(0, 24_000, 50):
        clock.t = start + elapsed + 30
        if elapsed % 200 == 0:
            source.value = shared(start + elapsed - lag_ms,
                                  {cid: elapsed // 200 + 1 for cid in cams}, cams=cams,
                                  produced=start + elapsed + 20)
        if elapsed % 5000 == 0:
            load = LoadWindow()
            load.add(lag_ms + 20)
            marker.write_text(json.dumps({"started": True, "gpu_priority": marker_section(
                phase="running", reserve={"state": "OK"}, load=load.take(),
                written_at_ms=clock.t, pid=1)}), encoding="utf-8")
            os.utime(marker, (clock.t / 1000, clock.t / 1000))
        loop.step()
    assert gate.stats()["opened"] == 1 and gate.stats()["resets"] == {}
    assert loop.counts["wait_exceeded"] == 0
    return loop, source, detector, out, clock, samples


@pytest.mark.parametrize("lag_ms", [0, 520, 580])
@pytest.mark.parametrize("phase_ms", [0, 100, 400])
@pytest.mark.parametrize("cameras", [1, 4])
def test_allowed_delays_accumulate_without_wall_time_closing_capture_bins(tmp_path, lag_ms, phase_ms, cameras):
    loop, _source, _detector, out, _clock, samples = delayed_stream(
        tmp_path, lag_ms=lag_ms, phase_ms=phase_ms, cameras=cameras)
    [observed] = out.messages[-1]["payload"]["people"]
    assert observed["helmet"]["state"] == observed["vest"]["state"] == "NOT_WORN"
    assert loop.judge.dropped == {}
    for rows in samples.values():
        assert rows and all(b[0] - a[0] >= 500 for a, b in zip(rows, rows[1:]))
        bins = [capture // 500 for _, capture in rows]
        assert len(bins) == len(set(bins))


@pytest.mark.parametrize("candidates,expected_rules", [
    ((NO_HELMET(), NO_VEST()), {"R9", "R11"}), ((), set()),
])
def test_delayed_states_feed_real_store_and_replay_lifecycle(tmp_path, candidates, expected_rules):
    _loop, _source, _detector, out, _clock, _samples = delayed_stream(tmp_path, candidates=candidates)
    payload = json.loads((Path(__file__).resolve().parents[3] /
                          "contracts/examples/world_state.json").read_text(encoding="utf-8"))["payload"]
    clock = FixedClock(out.messages[0]["sent_at"])
    store = PpeStateStore()
    engine = RiskEngine([HelmetRequiredRule(), VestRequiredRule()], clock)
    lifecycle = LifecycleTracker(origin=Origin.REPLAY, clock=clock, gateway_id="tier0-01")
    policy = PpePolicy(version="ppe-delay-test", work_id=7, effective_from_ms=clock.now_ms() - 1000,
                       helmet="REQUIRED", vest="REQUIRED")
    raised = set()
    for message in out.messages:
        assert store.offer(message)
        clock.set(message["sent_at"])
        [observed] = message["payload"]["people"]
        track = dict(payload["tracks"][0], t_last_valid=observed["capture_time_ms"], frame_age_ms=0)
        world = WorldState.from_payload(dict(payload, t_latest_global=observed["capture_time_ms"], tracks=[track]))
        evaluation = engine.evaluate(world, [], Origin.REPLAY, ppe_policy=policy, ppe=store.snapshot())
        result = lifecycle.apply(evaluation, world, config_version="cfg-delay-test")
        raised.update(t.state.rule_id for t in result.transitions if t.transition == "RAISED")
        assert not result.local_signals and not evaluation.alert_candidates()
    assert raised == expected_rules


@pytest.mark.parametrize("candidates,state", [((HELMET(), VEST()), "WORN"),
                                           ((NO_HELMET(), NO_VEST()), "NOT_WORN")])
@pytest.mark.parametrize("blocked", [False, True])
def test_frozen_capture_clock_still_expires_at_real_ten_seconds(tmp_path, candidates, state, blocked):
    loop, source, detector, out, clock, _samples = delayed_stream(tmp_path, candidates=candidates)
    loop.publish_period_ms = 0  # 10초 경계를 송신 주기와 분리해 확인한다.
    loop.step()  # 마지막 정기 송신 뒤 접수한 프레임까지 반영한 만료 시각을 사용한다.
    [observed] = out.messages[-1]["payload"]["people"]
    assert observed["helmet"]["state"] == observed["vest"]["state"] == state
    expiry = observed["helmet"]["expires_at_ms"]
    last_seen = observed["capture_time_ms"]
    calls = len(detector.calls)
    source.value = None
    loop.gate = SimpleNamespace(check=lambda: SimpleNamespace(live=True, admit=not blocked, code="OVER_BUDGET"))
    clock.t = expiry
    loop.step()
    assert out.messages[-1]["payload"]["people"][0]["helmet"]["state"] == state
    clock.t = expiry + 1
    loop.step()
    [expired] = out.messages[-1]["payload"]["people"]
    assert expired["helmet"]["state"] == expired["vest"]["state"] == "UNKNOWN"
    clock.t = expiry + 1000
    loop.step()
    assert out.messages[-1]["payload"]["people"][0]["helmet"]["state"] == "UNKNOWN"
    clock.t = last_seen + loop.judge.cfg.track_retention_ms + 1
    loop.step()
    assert out.messages[-1]["payload"]["people"] == [] and len(detector.calls) == calls


def test_capture_watermark_does_not_rewind_or_reopen_closed_bins():
    judge = PpeJudge()
    feed(judge, "cam1", 1100, [person("w1")], [HELMET()])
    judge.evaluate(3000, capture_time_ms=1600)
    closed = judge.last_closed_bin
    judge.evaluate(3500, capture_time_ms=1000)
    assert judge.last_closed_bin == closed
    feed(judge, "cam2", 1400, [person("w1")], [NO_HELMET()])
    assert judge.dropped["LATE_FRAME"] == 1


def test_near_boundary_camera_conflict_is_combined_before_closing():
    judge = PpeJudge()
    feed(judge, "cam1", 1510, [person("w1")], [HELMET()])
    judge.evaluate(2100, capture_time_ms=1510)
    feed(judge, "cam2", 1450, [person("w1")], [NO_HELMET()])
    feed(judge, "cam2", 1520, [person("w1")], [NO_HELMET()])
    views = judge.evaluate(2500, capture_time_ms=2100)
    assert judge.dropped == {} and views["w1"]["helmet"].state == "UNKNOWN"
    assert views["w1"]["helmet"].reason == "CROSS_CAMERA_CONFLICT"
