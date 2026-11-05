"""PPE 서비스 주기(services.ppe.loop, S15P31A101-507): Tier 0 입력 공유 문서 → 검출 → 누적 → ppe_state → outbox.

가짜 검출기·가짜 접수 문으로 돈다(GPU·torch 없음). 나온 메시지는 계약(ppe_state 1.0.0)과 스키마 밖 규칙으로 검사한다.
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # ai/ (services 패키지는 설치하지 않는다)

from services.ppe.loop import PpeLoop, frame_observations  # noqa: E402
from simai_ai.io.avatar_channel import LatestChannelReader, LatestChannelWriter  # noqa: E402

from .test_association_state import HELMET, IMAGE, NO_VEST, keypoints  # noqa: E402
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
    assert person["vest"]["state"] in ("NOT_WORN", "UNKNOWN")
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
