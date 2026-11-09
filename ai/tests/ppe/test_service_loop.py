"""PPE 서비스 주기(services.ppe.loop, S15P31A101-507): Tier 0 입력 공유 문서 → 검출 → 누적 → ppe_state → outbox.

가짜 검출기·가짜 접수 문으로 돈다(GPU·torch 없음). 나온 메시지는 계약(ppe_state 1.0.0)과 스키마 밖 규칙으로 검사한다.
"""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # ai/ (services 패키지는 설치하지 않는다)

from services.ppe.loop import PpeLoop, frame_observations  # noqa: E402
from simai_ai.io.avatar_channel import LatestChannelReader, LatestChannelWriter  # noqa: E402
from simai_ai.risk import (  # noqa: E402
    FixedClock, HelmetRequiredRule, LifecycleTracker, Origin, PpePolicy, PpeStateStore,
    RiskEngine, VestRequiredRule, WorldState,
)

from .test_association_state import HELMET, IMAGE, NO_HELMET, NO_VEST, keypoints  # noqa: E402
from .test_message import out_of_schema_violations, validator  # noqa: E402,F401 - 픽스처

SHA = "e373a104805765417502e34d21586be2607126bbdd8499b935896ca333eb103c"
PROVENANCE = {"frames": {"source": "LIVE", "camera_ids": ["cam1", "cam2"]},
              "tracks": {"source": "TIER0_LIVE", "provider_version": "tier0-avatar-1"}, "is_upper_bound": False}
W, H = IMAGE


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Gate:
    def __init__(self):
        self.admission = SimpleNamespace(live=True, admit=True, code="OK")

    def check(self):
        return self.admission


class Detector:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = []

    def infer(self, image):
        assert image.shape == (H, W, 3) and image.dtype == np.uint8
        self.calls.append(image.shape)
        return list(self.candidates)


class Outbox:
    def __init__(self, ok=True):
        self.messages, self.ok = [], ok

    def put(self, message):
        self.messages.append(message)
        return self.ok


class Source:
    def __init__(self):
        self.value = None

    def latest(self, *, predicate=None):
        return self.value


def shared(t, seqs, *, cams=("cam1",), hold=False, identity="HIGH", rev=2, produced=None, obs_time=None):
    """Tier 0 _share_avatar 가 쓰는 모양(simai.avatar-input@1)의 문서와 카메라별 영상."""
    frames, per_camera = {}, []
    for cid in cams:
        frame_ref = {"stream_epoch": 3, "frame_sequence": seqs[cid]}
        frames[cid] = {"array": f"image_{cid}", "capture_time_ms": t, "frame_ref": frame_ref}
        per_camera.append({"camera_id": cid, "local_track_id": f"{cid}-17", "bbox": [450, 250, 550, 620],
                           "keypoints_2d": keypoints(500, 300), "joint_layout": "COCO17",
                           "estimated_capture_time": t if obs_time is None else obs_time, "frame_ref": frame_ref})
    doc = {"format": "simai.avatar-input@1", "produced_at_ms": t + 20 if produced is None else produced,
           "world": {"t": t, "input_provenance": PROVENANCE,
                     "tracks": [{"world_id": "w-0012", "identity_revision": rev, "identity_confidence": identity,
                                 "avatar_update_hold": hold, "per_camera": per_camera}]},
           "frames": frames}
    images = {cid: np.zeros((H, W, 3), np.uint8) for cid in cams}
    return doc, images


def make_loop(candidates=(HELMET(), NO_VEST()), **kw):
    source, gate, det, out, clock = Source(), Gate(), Detector(candidates), Outbox(), Clock(0)
    loop = PpeLoop(source, gate, det, out, source_id="tier0-01", stream_epoch=1_760_342_000_000,
                   model_version="T1-v4-ALL-seed20261021-e08-e373a1048057", checkpoint_sha256=SHA, clock_ms=clock, **kw)
    return loop, source, gate, det, out, clock


def run(loop, source, clock, start, frames, *, step_ms=250, cams=("cam1",), **kw):
    """step_ms 마다 새 프레임 하나씩 공유하고 한 주기를 돈다."""
    for i in range(frames):
        t = start + i * step_ms
        clock.t = t + 30
        source.value = shared(t, {c: i + 1 for c in cams}, cams=cams, **kw)
        loop.step()


def test_frame_observations_keep_same_frame_people_and_mark_held_tracks_uncertain():
    doc, _ = shared(1000, {"cam1": 5}, hold=True)
    obs = frame_observations(doc, "cam1", IMAGE)
    assert (obs.camera_id, obs.stream_epoch, obs.frame_sequence, obs.capture_time_ms) == ("cam1", 3, 5, 1000)
    [p] = obs.persons
    assert (p.world_id, p.identity_revision, p.local_track_id, p.identity_confidence) == ("w-0012", 2, "cam1-17",
                                                                                          "IDENTITY_UNCERTAIN")
    other, _ = shared(1000, {"cam1": 5}, obs_time=960)  # 관측이 영상과 다른 프레임이면 쓰지 않는다
    assert frame_observations(other, "cam1", IMAGE).persons == ()


def test_steady_helmet_becomes_worn_and_messages_pass_contract(validator):  # noqa: F811
    loop, source, _gate, det, out, clock = make_loop()
    run(loop, source, clock, 1_760_342_400_000, 20)
    assert out.messages, "보낸 메시지가 없다"
    for m in out.messages:
        errors = sorted(e.message for e in validator.iter_errors(m))
        assert not errors and not out_of_schema_violations(m), errors
    assert [m["payload"]["state_sequence"] for m in out.messages] == list(range(len(out.messages)))
    last = out.messages[-1]["payload"]
    assert last["checkpoint_sha256"] == "sha256:" + SHA and last["input_provenance"] == PROVENANCE
    [person] = last["people"]
    assert person["world_id"] == "w-0012" and person["identity_revision"] == 2
    assert person["helmet"]["state"] == "WORN"
    assert person["vest"]["state"] == "NOT_WORN"
    assert len(det.calls) == 10  # 250 ms 마다 새 프레임이지만 카메라당 2 Hz(500 ms)로만 추론


def test_same_frame_is_inferred_once():
    loop, source, _gate, det, _out, clock = make_loop()
    source.value = shared(1000, {"cam1": 1})
    for i in range(5):
        clock.t = 1030 + i * 600
        loop.step()
    assert len(det.calls) == 1


def test_frame_waiting_longer_than_150ms_is_dropped():
    loop, source, _gate, det, _out, clock = make_loop()
    source.value = shared(1000, {"cam1": 1}, produced=1000)
    clock.t = 1151
    loop.step()
    assert det.calls == [] and loop.counts["wait_exceeded"] == 1


def test_closed_gate_stops_inference_but_keeps_reporting_until_evidence_expires():
    loop, source, gate, det, out, clock = make_loop()
    run(loop, source, clock, 1_760_342_400_000, 20)
    inferred = len(det.calls)
    gate.admission = SimpleNamespace(live=True, admit=False, code="OVER_BUDGET")
    run(loop, source, clock, 1_760_342_405_000, 60)
    assert len(det.calls) == inferred and loop.counts["gate_OVER_BUDGET"] == 60
    assert out.messages[-1]["payload"]["people"][0]["helmet"]["state"] == "UNKNOWN"  # 10초 만료


def test_not_live_sends_nothing():
    loop, source, gate, det, out, clock = make_loop()
    gate.admission = SimpleNamespace(live=False, admit=True, code="NOT_LIVE")
    run(loop, source, clock, 1000, 5)
    assert out.messages == [] and det.calls == []


def test_no_message_before_first_tier0_input():
    loop, _source, _gate, _det, out, clock = make_loop()
    clock.t = 5000
    loop.step()
    assert out.messages == [] and loop.counts["no_input"] == 1


def test_failed_outbox_write_still_advances_sequence():
    loop, source, _gate, _det, out, clock = make_loop()
    out.ok = False
    run(loop, source, clock, 1000, 6)
    assert [m["payload"]["state_sequence"] for m in out.messages] == list(range(len(out.messages)))
    assert loop.counts["publish_failed"] == len(out.messages) > 1


def test_two_cameras_each_limited_to_2hz():
    loop, source, _gate, det, _out, clock = make_loop()
    run(loop, source, clock, 1000, 8, cams=("cam1", "cam2"))
    assert len(det.calls) == 8  # 2초 동안 카메라 2대 × 2 Hz


def tick_stream(*, period_ms=200, phase_ms=0, cameras=1, candidates=None, duration_ms=10_000,
                capture_jitter_ms=(0,)):
    """50ms 주기로 최신 입력을 다시 읽는다. 모델·시계만 합성하고 추론 시작과 촬영 시각을 따로 기록한다."""
    loop, source, gate, det, out, clock = make_loop(
        candidates=(NO_HELMET(), NO_VEST()) if candidates is None else candidates)
    start = 1_760_342_400_000 + phase_ms
    cams = tuple(f"cam{i + 1}" for i in range(cameras))
    samples = {cid: [] for cid in cams}
    infer = det.infer

    def record(image):
        cid = next(cid for cid, value in source.value[1].items() if value is image)
        capture = source.value[0]["frames"][cid]["capture_time_ms"]
        samples[cid].append((clock.t, capture))
        return infer(image)

    det.infer = record
    for elapsed in range(0, duration_ms, 50):
        if elapsed % period_ms == 0:
            sequence = elapsed // period_ms
            capture = start + elapsed + capture_jitter_ms[sequence % len(capture_jitter_ms)]
            source.value = shared(capture, {c: sequence + 1 for c in cams}, cams=cams,
                                  produced=start + elapsed + 20)
        clock.t = start + elapsed + 30
        loop.step()
    return loop, out, samples


@pytest.mark.parametrize("period_ms", [200, 250, 500])
@pytest.mark.parametrize("phase_ms", [0, 150, 400])
@pytest.mark.parametrize("cameras", [1, 4])
def test_shared_input_reaches_not_worn_without_exceeding_inference_rate(period_ms, phase_ms, cameras):
    """-567: 기본5Hz와500ms선택이 엇갈려도 실제 추론은2Hz 이하, 미착용6칸은 온전히 쌓인다."""
    loop, out, samples = tick_stream(period_ms=period_ms, phase_ms=phase_ms, cameras=cameras)
    [person] = out.messages[-1]["payload"]["people"]
    assert person["helmet"]["state"] == person["vest"]["state"] == "NOT_WORN"
    for camera_samples in samples.values():
        assert all(b[0] - a[0] >= 500 for a, b in zip(camera_samples, camera_samples[1:]))
        bins = [capture // loop.judge.cfg.bin_ms for _, capture in camera_samples]
        assert len(bins) == len(set(bins)), "같은 카메라·촬영 칸을 두 번 추론했다"
    assert loop.judge.dropped == {}


@pytest.mark.parametrize("phase_ms", [0, 150, 400])
def test_capture_jitter_still_accumulates_explicit_not_worn(phase_ms):
    _loop, out, samples = tick_stream(phase_ms=phase_ms, cameras=4, capture_jitter_ms=(-20, 0, 20))
    [person] = out.messages[-1]["payload"]["people"]
    assert person["helmet"]["state"] == person["vest"]["state"] == "NOT_WORN"
    for camera_samples in samples.values():
        assert all(b[0] - a[0] >= 500 for a, b in zip(camera_samples, camera_samples[1:]))


@pytest.mark.parametrize("kwargs", [{"period_ms": 1000}, {"candidates": ()}])
def test_missing_bins_or_detections_do_not_become_not_worn(kwargs):
    _loop, out, _samples = tick_stream(**kwargs)
    for message in out.messages:
        [person] = message["payload"]["people"]
        assert person["helmet"]["state"] == person["vest"]["state"] == "UNKNOWN"


@pytest.mark.parametrize("candidates,expected_rules", [
    ((NO_HELMET(), NO_VEST()), {"R9", "R11"}),
    ((), set()),
])
def test_5hz_states_feed_real_ppe_store_and_r9_r11_lifecycle(candidates, expected_rules):
    """생산된 메시지를 실제 Tier 0 저장소·규칙·생명주기에 넣는다. REPLAY라 실제 알림은 없다."""
    _loop, out, _samples = tick_stream(cameras=4, candidates=candidates)
    example = Path(__file__).resolve().parents[3] / "contracts/examples/world_state.json"
    world_payload = json.loads(example.read_text(encoding="utf-8"))["payload"]
    start = out.messages[0]["sent_at"]
    clock = FixedClock(start)
    store = PpeStateStore()
    engine = RiskEngine([HelmetRequiredRule(), VestRequiredRule()], clock)
    tracker = LifecycleTracker(origin=Origin.REPLAY, clock=clock, gateway_id="tier0-01")
    policy = PpePolicy(version="ppe-test", work_id=7, effective_from_ms=start - 1000,
                       helmet="REQUIRED", vest="REQUIRED")
    raised = set()
    for message in out.messages:
        assert store.offer(message)
        now = message["sent_at"]
        clock.set(now)
        track = dict(world_payload["tracks"][0], t_last_valid=now, frame_age_ms=0)
        world = WorldState.from_payload(dict(world_payload, t_latest_global=now, tracks=[track]))
        evaluation = engine.evaluate(world, [], Origin.REPLAY, ppe_policy=policy, ppe=store.snapshot())
        result = tracker.apply(evaluation, world, config_version="cfg-test")
        raised.update(t.state.rule_id for t in result.transitions if t.transition == "RAISED")
        assert not result.local_signals and not evaluation.alert_candidates()
    assert raised == expected_rules


def test_does_not_infer_a_second_frame_in_the_same_capture_bin():
    loop, source, _gate, det, _out, clock = make_loop()
    source.value = shared(1000, {"cam1": 1})
    clock.t = 1030
    loop.step()
    # 처리 간격이 지났어도 같은 촬영 칸의 다른 프레임은 GPU에 다시 넣지 않는다.
    source.value = shared(1200, {"cam1": 2}, produced=1620)
    clock.t = 1630
    loop.step()
    assert len(det.calls) == 1


def test_rate_limit_uses_inference_clock_instead_of_capture_or_utc_clock():
    monotonic = Clock(0)
    loop, source, _gate, det, _out, clock = make_loop(monotonic_ms=monotonic)
    source.value = shared(1000, {"cam1": 1})
    clock.t = 1030
    loop.step()
    source.value = shared(1600, {"cam1": 2})
    clock.t = 1630
    monotonic.t = 499
    loop.step()
    assert len(det.calls) == 1  # 촬영·UTC가 600ms 진행해도 실제 추론 시작 간격은 아직 499ms다.
    monotonic.t = 500
    loop.step()
    assert len(det.calls) == 2


def test_rate_limit_includes_time_spent_building_observations(monkeypatch):
    monotonic = Clock(0)
    loop, source, _gate, det, _out, clock = make_loop(monotonic_ms=monotonic)
    observed = []

    def slow_first_observation(*args):
        if not observed:
            monotonic.t += 100
        observed.append(monotonic.t)
        return frame_observations(*args)

    monkeypatch.setattr("services.ppe.loop.frame_observations", slow_first_observation)
    source.value = shared(1000, {"cam1": 1})
    clock.t = 1030
    loop.step()  # 실제 추론 시작은 관측을 만든 뒤인 단조 시각100ms다.
    source.value = shared(1600, {"cam1": 2})
    clock.t = 1630
    monotonic.t = 500
    loop.step()
    assert len(det.calls) == 1  # 이전 step 시작부터는500ms여도 실제 추론 시작부터는400ms다.
    monotonic.t = 600
    loop.step()
    assert len(det.calls) == 2


def test_reads_real_shared_file_written_by_tier0_writer(tmp_path):
    path = tmp_path / "avatar-input.npz"
    writer = LatestChannelWriter(path, period_s=0.01, clock_ms=lambda: 1020)
    doc, images = shared(1000, {"cam1": 1})
    frames = {"cam1": SimpleNamespace(image=images["cam1"], estimated_capture_time=1000,
                                      frame_ref={"stream_epoch": 3, "frame_sequence": 1})}
    try:
        writer.offer({"world": doc["world"]}, frames)
        reader = LatestChannelReader(path)
        for _ in range(200):
            if reader.latest() is not None:
                break
            time.sleep(0.01)
    finally:
        writer.close()
    loop, _source, _gate, det, out, clock = make_loop()
    loop.source = reader
    clock.t = 1030
    loop.step()
    assert len(det.calls) == 1 and out.messages and out.messages[0]["payload"]["people"][0]["world_id"] == "w-0012"


@pytest.mark.parametrize("people", [65])
def test_more_than_64_people_skips_the_message(people):
    loop, source, _gate, _det, out, clock = make_loop(candidates=())
    doc, images = shared(1000, {"cam1": 1})
    track = doc["world"]["tracks"][0]
    doc["world"]["tracks"] = [{**track, "world_id": f"w-{i:04d}",
                               "per_camera": [{**track["per_camera"][0], "local_track_id": f"l-{i}",
                                               "keypoints_2d": keypoints(40 + i * 34, 300)}]}
                              for i in range(people)]
    source.value = (doc, images)
    clock.t = 1030
    loop.step()
    assert out.messages == [] and loop.counts["too_many_people"] == 1
