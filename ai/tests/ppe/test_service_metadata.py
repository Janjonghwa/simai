"""실제 NPZ reader에서 PPE 영상 읽기 선별과 검출 직전 재검사를 확인한다."""

import json

import numpy as np
import pytest

from simai_ai.io.avatar_channel import LatestChannelReader
from simai_ai.io.channel_timing import ChannelTiming

from .test_service_loop import Clock, make_loop, shared


def snapshot(path, t=1000, seq=1, *, cams=("cam1",), produced=None):
    doc, images = shared(t, dict.fromkeys(cams, seq), cams=cams, produced=produced)
    arrays = {doc["frames"][cid]["array"]: image for cid, image in images.items()}
    np.savez(path, metadata=np.frombuffer(json.dumps(doc).encode(), dtype=np.uint8), **arrays)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    loop, _, gate, detector, out, clock = make_loop()
    loop.source = LatestChannelReader(tmp_path / "shared.npz")
    accessed = []
    getitem = np.lib.npyio.NpzFile.__getitem__
    def spy(z, key):
        accessed.append(key)
        return getitem(z, key)
    monkeypatch.setattr(np.lib.npyio.NpzFile, "__getitem__", spy)
    return loop, gate, detector, out, clock, accessed


@pytest.mark.parametrize("reason", ["duplicate", "rate", "bin", "stale"])
def test_ineligible_snapshot_reads_only_metadata_and_does_not_consume_frame(setup, reason):
    loop, _, detector, out, clock, accessed = setup
    snapshot(loop.source.path)
    clock.t = 1030
    if reason == "duplicate":
        loop._last_frame["cam1"] = (3, 1)
    elif reason == "rate":
        loop._last_infer_at["cam1"] = 1020
    elif reason == "bin":
        loop._last_bin["cam1"] = (3, 2)
    else:
        clock.t = 1171  # produced=1020 + 151ms
    previous = [dict(d) for d in (loop._last_frame, loop._last_infer_at, loop._last_bin)]
    loop.step()
    assert accessed == ["metadata"] and not detector.calls
    assert previous == [loop._last_frame, loop._last_infer_at, loop._last_bin]
    assert out.messages  # 선별로 검출을 생략해도 상태 송신은 진행한다.
    assert loop.counts["metadata_rejected"] == 1


def test_rate_limited_snapshot_can_be_retried_after_interval_without_new_file(setup):
    loop, _, detector, _, clock, accessed = setup
    mono = Clock(200)
    loop.monotonic_ms = mono
    loop._last_infer_at["cam1"] = 0
    snapshot(loop.source.path)
    clock.t = 1030
    loop.step()
    assert accessed == ["metadata"]
    mono.t = 500
    loop.step()
    assert accessed == ["metadata", "metadata", "image_cam1"]
    assert len(detector.calls) == 1 and loop._last_frame["cam1"] == (3, 1)


def test_one_eligible_camera_allows_images_and_duplicate_camera_is_not_inferred(setup):
    loop, _, detector, _, clock, accessed = setup
    snapshot(loop.source.path, cams=("cam1", "cam2"))
    loop._last_frame["cam1"] = (3, 1)
    clock.t = 1030
    loop.step()
    assert accessed == ["metadata", "image_cam1", "image_cam2"]
    assert len(detector.calls) == 1 and loop._last_frame["cam2"] == (3, 1)
    assert loop.counts["metadata_rejected"] == 0


def test_new_epoch_is_not_mistaken_for_previous_snapshot(setup):
    loop, _, detector, _, clock, accessed = setup
    snapshot(loop.source.path)
    loop._last_frame["cam1"] = (2, 1)
    loop._last_bin["cam1"] = (2, 2)
    clock.t = 1030
    loop.step()
    assert accessed == ["metadata", "image_cam1"] and len(detector.calls) == 1
    assert loop._last_frame["cam1"] == (3, 1)


def test_rejected_snapshots_still_publish_expired_unknown_state(setup):
    loop, _, detector, out, clock, accessed = setup
    for i in range(8):
        t = 1000 + i * 500
        snapshot(loop.source.path, t, i + 1)
        clock.t = t + 30
        loop.step()
    assert out.messages[-1]["payload"]["people"][0]["helmet"]["state"] == "WORN"
    accessed.clear()
    clock.t += 11_000
    loop.step()
    assert accessed == ["metadata"] and len(detector.calls) == 8
    assert out.messages[-1]["payload"]["people"][0]["helmet"]["state"] == "UNKNOWN"


@pytest.mark.parametrize("age,expected", [(150, 1), (151, 0)])
def test_image_read_cost_is_still_checked_at_150_ms_boundary(setup, monkeypatch, age, expected):
    loop, _, detector, _, clock, _ = setup
    snapshot(loop.source.path, produced=1000)
    clock.t = 1000
    getitem = np.lib.npyio.NpzFile.__getitem__
    def slow_image(z, key):
        result = getitem(z, key)
        if key.startswith("image"):
            clock.t = 1000 + age
        return result
    monkeypatch.setattr(np.lib.npyio.NpzFile, "__getitem__", slow_image)
    loop.step()
    assert len(detector.calls) == expected
    assert bool(loop._last_frame) == bool(expected)


def test_gate_change_while_loading_images_stops_inference_without_consuming_frame(setup, monkeypatch):
    loop, gate, detector, out, clock, _ = setup
    snapshot(loop.source.path)
    clock.t = 1030
    getitem = np.lib.npyio.NpzFile.__getitem__
    def close_gate(z, key):
        result = getitem(z, key)
        if key.startswith("image"):
            gate.admission.admit = False
        return result
    monkeypatch.setattr(np.lib.npyio.NpzFile, "__getitem__", close_gate)
    loop.step()
    assert not detector.calls and not loop._last_frame
    assert out.messages


def test_camera_stage_timing_uses_separate_clock_and_records_serial_wait(setup):
    loop, _, detector, _, clock, _ = setup
    diag = Clock(0.0)
    loop.timing = ChannelTiming(clock=diag)
    snapshot(loop.source.path, cams=("cam1", "cam2"))
    clock.t = 1030
    infer = detector.infer
    def measured(image):
        result = infer(image)
        diag.t += .020
        return result
    detector.infer = measured
    loop.step()
    rows = {(r["stage"], r["camera"]): r for r in loop.diagnostics()["loop"]["series"]}
    assert len(detector.calls) == 2
    assert rows["camera_inference", "cam1"]["ms"]["p95"] == 20
    assert rows["camera_batch_wait", "cam2"]["ms"]["p95"] == 20
    assert rows["camera_source_age_at_infer", "cam2"]["ms"]["p95"] == 10  # produced=1020
    assert {"camera_observations", "camera_gate", "camera_association_ingest", "camera_inferred"} <= {
        stage for stage, _ in rows}
    assert loop._last_infer_at == {"cam1": 1030, "cam2": 1030}
