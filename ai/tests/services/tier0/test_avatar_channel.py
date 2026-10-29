"""Tier 0 공유 출력의 디스크 정체·실패는 호출자를 막지 않는다."""

import threading
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from simai_ai.io.avatar_channel import LatestChannelReader, LatestChannelWriter
from simai_ai.providers.base import Frame

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.tier0.perception import Perception
from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection
from tests.hmr.multiview_scene import make_rig


def test_blocked_disk_keeps_only_latest_and_offer_does_not_wait(tmp_path):
    writer = LatestChannelWriter(tmp_path / "latest.npz")
    entered, release = threading.Event(), threading.Event()
    calls = []
    def blocked(metadata, frames):
        calls.append(metadata)
        entered.set()
        release.wait(3)
    writer._write = blocked
    try:
        writer.offer({"t": 1}, {})
        assert entered.wait(1)
        start = time.monotonic()
        for t in range(2, 1002):
            writer.offer({"t": t}, {})
        assert time.monotonic() - start < .5
        assert writer._pending[0] == {"t": 1001}
        assert writer.dropped == 999
        writer._lock.acquire()
        try:
            assert not writer.offer({}, {})
        finally:
            writer._lock.release()
    finally:
        release.set()
        writer.close()


def test_atomic_snapshot_roundtrip_and_disk_failure(tmp_path):
    path = tmp_path / "latest.npz"
    writer = LatestChannelWriter(path)
    image = np.full((3, 4, 3), 42, np.uint8)
    frame = Frame("cam1", 123, "FILE", {"stream_epoch": 1, "frame_sequence": 2}, image=image)
    try:
        writer._write({"world": {"t": 123}}, {"cam1": frame})
        doc, arrays = LatestChannelReader(path).latest()
        assert doc["frames"]["cam1"]["capture_time_ms"] == 123
        np.testing.assert_array_equal(arrays["cam1"], image)
        assert doc["world"] == {"t": 123}
        failed = threading.Event()
        def fail(*args):
            failed.set()
            raise OSError("disk full")
        writer._write = fail
        assert writer.offer({}, {}) and failed.wait(1)
        deadline = time.monotonic() + 1
        while not writer.errors and time.monotonic() < deadline:
            time.sleep(.001)
        assert writer.errors == 1
        assert LatestChannelReader(path).latest()[0]["world"] == {"t": 123}
    finally:
        writer.close()


def test_perception_shares_exact_frame_observation_and_world_id_without_changing_payload():
    rig = make_rig()
    cid = "cam_A"
    image = Frame(cid, 1000, "FILE", {"stream_epoch": 1, "frame_sequence": 42}, image=np.zeros((2, 2, 3), np.uint8))
    det = PoseDetection((0., 0., 1., 1.), .9, (None,) * 17)
    obs = Observation2DFrame(cid, 1, 42, 1000, "FILE", 1., (TrackedDetection("local-1", det),))
    calls = []
    perception = Perception.__new__(Perception)
    perception.rig = rig
    perception.feed = SimpleNamespace(avatar_frames={cid: image}, avatar_observations={cid: obs})
    perception.avatar_writer = SimpleNamespace(offer=lambda *args: calls.append(args))
    frame = SimpleNamespace(tracks=[SimpleNamespace(local_keys=[(cid, "local-1")])])
    payload = {"tracks": [{"world_id": "w-1", "identity_revision": 3, "pelvis_xyz": None, "ground_anchor_xyz": None}],
               "input_provenance": {"frames": {"source": "FILE", "dataset_id": "recording"}, "is_upper_bound": True}}
    perception._share_avatar(frame, payload, 1000)
    metadata, frames = calls[0]
    track = metadata["world"]["tracks"][0]
    assert track["world_id"] == "w-1" and track["identity_revision"] == 3
    assert track["pelvis_xyz"] is None and track["ground_anchor_xyz"] is None
    assert track["per_camera"][0]["frame_ref"] == image.frame_ref
    assert track["per_camera"][0]["keypoints_2d"] == [None] * 17
    assert "per_camera" not in payload["tracks"][0]
    assert frames[cid] is image
    calls.clear()
    perception._share_avatar(frame, payload, 1101)
    assert calls[0][0]["world"]["tracks"][0]["per_camera"] == []  # 오래된 영상 연결 금지
