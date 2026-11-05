"""Tier 0 공유 출력의 디스크 정체·실패는 호출자를 막지 않는다."""

import threading
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from simai_ai.io.avatar_channel import LatestChannelReader, LatestChannelWriter, avatar_channel_path
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
        assert writer._pending[0]["t"] == 1001
        assert isinstance(writer._pending[0]["produced_at_ms"], int)
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
    perception._share_avatar(SimpleNamespace(tracks=None), payload, 1101)
    assert calls == []  # 5 Hz(200 ms) 주기 전에는 트랙·스냅샷 조립 자체를 생략한다
    perception._share_avatar(frame, payload, 1200)
    assert calls[0][0]["world"]["tracks"][0]["per_camera"] == []  # 오래된 영상 연결 금지
    assert calls[0][0]["calibration"] is metadata["calibration"]  # 같은 rig 는 보정 직렬화 재사용
    new_rig = make_rig()
    perception.associator = SimpleNamespace(replace_rig=lambda _r: None)
    perception.manager = SimpleNamespace(input_provenance={})
    perception.replace_rig(new_rig, {"source": "FILE"})
    calls.clear()
    perception._share_avatar(frame, payload, 1300)  # 보정 교체 직후 tick 은 200 ms 전이어도 즉시 공유
    assert len(calls) == 1 and calls[0][0]["calibration"] is not metadata["calibration"]


def test_avatar_channel_path_prefers_explicit_then_shm_then_state_dir(tmp_path):
    shm = tmp_path / "shm"
    shm.mkdir()
    root = tmp_path / "root"
    assert avatar_channel_path({"SIMAI_AVATAR_CHANNEL": str(tmp_path / "custom.npz")}, root, shm_dir=shm) == tmp_path / "custom.npz"
    assert avatar_channel_path({}, root, shm_dir=shm) == shm / "simai-avatar-input.npz"
    assert avatar_channel_path({"SIMAI_ROOT": str(root)}, shm_dir=tmp_path / "missing-shm") == root / "state/avatar-input.npz"


def test_reader_predicate_skips_loading_image_arrays_when_rejected(tmp_path):
    path = tmp_path / "latest.npz"
    writer = LatestChannelWriter(path)
    image = np.full((64, 64, 3), 7, np.uint8)
    frame = Frame("cam1", 500, "FILE", {"stream_epoch": 1, "frame_sequence": 9}, image=image)
    try:
        writer._write({"world": {"t": 500}}, {"cam1": frame})
        raw = bytearray(path.read_bytes())
        # ZIP 앞쪽의 metadata 엔트리는 두고 뒤쪽의 image_0.npy 본문 바이트를 망가뜨려 이미지 읽기가 일어나면 실패하게 만든다.
        idx = raw.rindex(b"image_0.npy")
        corrupt_pos = max(0, idx - 512)
        raw[corrupt_pos:corrupt_pos + 64] = b"\xff" * 64
        corrupted = tmp_path / "corrupted.npz"
        corrupted.write_bytes(raw)
        reader = LatestChannelReader(corrupted)
        doc, images = reader.latest(predicate=lambda d: d["world"]["t"] > 500)
        assert doc["world"]["t"] == 500 and images == {}
        doc_ok, images_ok = LatestChannelReader(path).latest(predicate=lambda d: d["world"]["t"] == 500)
        assert doc_ok["world"]["t"] == 500
        np.testing.assert_array_equal(images_ok["cam1"], image)
    finally:
        writer.close()
