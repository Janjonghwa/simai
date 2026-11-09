"""관제 중계 동시 녹화(S15P31A101-554): 송신과 동시 세그먼트 저장, manifest 검증, 녹화본 재생, 느린 디스크·여유 공간 하한."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from simai_recorder.clock import FakeClock, SystemClock
from simai_recorder.manifest import validate_manifest, verify
from simai_recorder.recorder import CameraSpec
from simai_recorder.segment import SegmentWriter, camera_segments, read_segment
from simai_recorder.sources import (
    FaultPlan,
    FaultySource,
    FileSource,
    PacketSource,
    SourcePacket,
    StreamInfo,
    SyntheticSource,
)

from simai_relay import cli
from simai_relay.config import load_cameras
from simai_relay.recording import RelayRecorder, recording_factory
from simai_relay.relay import CameraRelay, EpochStore, RelaySession
from simai_relay.sink import I0CodecInfo, I0Frame, MemorySink


class _PacedListSource(PacketSource):
    """미리 만든 패킷을 짧은 실제 시간 간격으로 흘린다."""

    kind = "SYNTHETIC"

    def __init__(self, info: StreamInfo, packets: list[SourcePacket], interval_s: float = 0.002) -> None:
        self._info = info
        self._packets = packets
        self._interval_s = interval_s
        self._idx = 0

    def open(self) -> StreamInfo:
        return self._info

    def read(self) -> SourcePacket | None:
        if self._idx >= len(self._packets):
            return None
        if self._interval_s > 0:
            time.sleep(self._interval_s)
        pkt = self._packets[self._idx]
        self._idx += 1
        return pkt

    def close(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "width": self._info.width, "height": self._info.height, "fps": self._info.nominal_fps}


def test_concurrent_send_and_record_with_reconnect_and_manifest(tmp_path: Path, mp4_video: Path, contract_validator):
    """합성(재접속 포함)·파일 입력을 동시에 중계 송신하면서 녹화하고 manifest 스키마·해시를 검증한다."""
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)
    faulty_synth = FaultySource(synth, FaultPlan(disconnect_after=(25,)), clock)
    file_src = FileSource(mp4_video, clock)

    cams = [
        CameraSpec("cam_A", faulty_synth),
        CameraSpec("cam_B", file_src),
    ]
    rec_dir = tmp_path / "live-20261109-140000"
    recorder = RelayRecorder(rec_dir, cams, clock, requested_duration_s=2.5)
    recorder.start()

    # EpochStore(13자리 UTC ms stream_epoch)와 함께 돌려 큰 epoch 번호도 세그먼트·manifest 가 처리하는지 확인
    epochs = EpochStore(tmp_path / "epochs.json")
    sinks: dict[str, MemorySink] = {}

    def inner_factory(cid: str) -> MemorySink:
        sinks[cid] = MemorySink(cid)
        return sinks[cid]

    wrapped_factory = recording_factory(inner_factory, recorder)

    relays = [
        CameraRelay(
            cams[0],
            wrapped_factory("cam_A"),
            clock,
            threading.Event(),
            deadline=clock.monotonic() + 2.5,
            reconnect_delays_s=(0.5,),
            epoch_allocator=lambda: epochs.reserve("cam_A", clock.now_ms()),
        ),
        CameraRelay(
            cams[1],
            wrapped_factory("cam_B"),
            clock,
            threading.Event(),
            deadline=None,
            epoch_allocator=lambda: epochs.reserve("cam_B", clock.now_ms()),
        ),
    ]
    for r in relays:
        r.run()
        assert r.error is None

    manifest = recorder.close()
    assert recorder.manifest_errors == []
    assert validate_manifest(manifest) == []
    assert verify(rec_dir) == []
    contract_validator("recording_manifest", manifest)
    assert manifest["consent"] == {
        "contains_people": True,
        "purpose": "dev_test",
        "retention_until": "2027-01-31",
    }
    assert manifest["recorded_by"]["tool"] == "gateway/relay"

    # cam_A: 재접속 1회 → 세그먼트 2개, 송신한 프레임과 녹화된 프레임이 정확히 일치
    segs_a = camera_segments(rec_dir / "cam_A")
    assert len(segs_a) == 2
    recorded_a: list[tuple[dict[str, Any], bytes]] = []
    for seg in segs_a:
        codec, frame_iter = read_segment(seg)
        contract_validator("codec_info", codec)
        for hdr, payload in frame_iter:
            contract_validator("frame_header", hdr)
            recorded_a.append((hdr, payload))
    assert len(recorded_a) == len(sinks["cam_A"].frames)
    for (hdr, payload), sent in zip(recorded_a, sinks["cam_A"].frames, strict=True):
        assert hdr == sent.header
        assert payload == sent.data

    # cam_B: MP4 파일 60프레임 그대로 송신·녹화
    segs_b = camera_segments(rec_dir / "cam_B")
    assert len(segs_b) == 1
    _, frames_b = read_segment(segs_b[0])
    recorded_b = list(frames_b)
    assert len(recorded_b) == 60 == len(sinks["cam_B"].frames)


def test_recorded_session_replays_via_file_and_directory_config(tmp_path: Path, mp4_video: Path, contract_validator):
    """중계가 녹화한 세그먼트(.h264 파일 직접 지정 및 세션/카메라 폴더 지정)를 simai-relay kind='file' 로 다시 재생한다."""
    clock = FakeClock()
    cams = [
        CameraSpec("cam_A", FileSource(mp4_video, clock)),
        CameraSpec("cam_B", SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)),
    ]
    rec_dir = tmp_path / "rec-session"
    recorder = RelayRecorder(rec_dir, cams, clock)
    recorder.start()

    for cam in cams:
        sink = recorder.sink_for(cam.camera_id, MemorySink(cam.camera_id))
        deadline = clock.monotonic() + 1.0 if cam.camera_id == "cam_B" else None
        CameraRelay(cam, sink, clock, threading.Event(), deadline).run()

    manifest = recorder.close()
    assert validate_manifest(manifest) == []

    seg_a_h264 = camera_segments(rec_dir / "cam_A")[0].h264
    replay_cfg = tmp_path / "replay.toml"
    replay_cfg.write_text(
        f"""
[[cameras]]
camera_id = "cam_A"
kind = "file"
file = '{seg_a_h264.as_posix()}'
fps = 25
realtime = true

[[cameras]]
camera_id = "cam_B"
kind = "file"
file = '{rec_dir.as_posix()}'
fps = 25
realtime = true
""",
        encoding="utf-8",
    )

    replay_cams = load_cameras(replay_cfg, clock, {})
    assert [c.camera_id for c in replay_cams] == ["cam_A", "cam_B"]

    replayed: dict[str, MemorySink] = {}
    for spec in replay_cams:
        ms = MemorySink(spec.camera_id)
        replayed[spec.camera_id] = ms
        relay = CameraRelay(spec, ms, clock, threading.Event(), deadline=None)
        relay.run()
        assert relay.error is None

    assert len(replayed["cam_A"].frames) == 60
    assert len(replayed["cam_B"].frames) == 26
    for ms in replayed.values():
        assert isinstance(ms.records[0], I0CodecInfo)
        contract_validator("codec_info", ms.records[0].codec_info)
        for f in ms.frames:
            contract_validator("frame_header", f.header)
            assert f.header["timestamp_source"] == "FILE"
            assert f.header["timestamp_uncertainty_ms"] == 20.0


def test_slow_writer_does_not_reduce_sent_frame_count(tmp_path: Path, caplog):
    """디스크 쓰기가 느려 녹화 큐가 가득 차도 중계 송신 프레임 수는 줄지 않고 녹화 프레임만 키프레임 단위로 버린다."""
    prep_clock = FakeClock()
    synth = SyntheticSource(prep_clock, width=64, height=48, fps=25, keyframe_interval_s=0.4)  # 10프레임마다 키프레임
    info = synth.open()
    packets = [synth.read() for _ in range(40)]
    assert all(p is not None for p in packets)
    pkt_list = [p for p in packets if p is not None]

    # 1) 녹화 없이 송신한 기준 프레임 수
    clock = SystemClock()
    base_sink = MemorySink("cam_A")
    CameraRelay(CameraSpec("cam_A", _PacedListSource(info, pkt_list, interval_s=0.001)), base_sink, clock, threading.Event(), None).run()
    assert len(base_sink.frames) == 40

    # 2) 느린 writer (프레임당 25ms 지연) + 작은 녹화 큐(3프레임)로 동시 녹화
    class SlowSegmentWriter(SegmentWriter):
        def write(self, header: dict[str, Any], payload: bytes) -> None:
            time.sleep(0.025)
            super().write(header, payload)

    rec_dir = tmp_path / "slow-rec"
    cam_spec = CameraSpec("cam_A", _PacedListSource(info, pkt_list, interval_s=0.001))
    recorder = RelayRecorder(
        rec_dir,
        [cam_spec],
        clock,
        max_queue_frames=3,
        writer_factory=SlowSegmentWriter,
    )
    recorder.start()
    sent_sink = MemorySink("cam_A")
    wrapped_sink = recorder.sink_for("cam_A", sent_sink)

    t0 = time.monotonic()
    with caplog.at_level("WARNING", logger="simai_relay.recording"):
        relay = CameraRelay(cam_spec, wrapped_sink, clock, threading.Event(), None)
        relay.run()
    send_elapsed_s = time.monotonic() - t0

    # 송신은 느린 디스크(40 * 25ms = 1.0s)를 기다리지 않고 즉시 끝났으며 프레임 수가 줄지 않는다
    assert len(sent_sink.frames) == len(base_sink.frames) == 40
    assert send_elapsed_s < 0.5

    manifest = recorder.close()
    snap = recorder.snapshot()
    assert snap["dropped_frames"] > 0
    assert snap["dropped_overflow"] > 0
    assert snap["recorded_frames"] > 0
    assert snap["recorded_frames"] + snap["dropped_frames"] == 40
    assert any("녹화 큐가 가득 차" in r.getMessage() for r in caplog.records)

    # 프레임을 중간에 버렸어도 세그먼트 무결성(sequence_contiguous, first_is_keyframe)과 manifest 검증은 통과한다
    assert validate_manifest(manifest) == []
    assert verify(rec_dir) == []


def test_disk_free_space_below_limit_stops_recording_while_sending_continues(tmp_path: Path, mp4_video: Path, caplog):
    """디스크 여유 공간이 하한 아래로 떨어지면 경고를 남기고 녹화만 중단하며 중계 송신은 끝까지 계속한다."""
    clock = FakeClock()
    cam_spec = CameraSpec("cam_A", FileSource(mp4_video, clock))
    rec_dir = tmp_path / "low-disk-rec"
    min_free = 20 * (1024**3)
    checks = 0

    def fake_free_bytes(_path: Path) -> int:
        nonlocal checks
        checks += 1
        # start() 에서 1번 + 처음 10프레임 동안은 30 GB, 그 뒤부터 10 GB(하한 미달)
        return 30 * (1024**3) if checks <= 11 else 10 * (1024**3)

    recorder = RelayRecorder(
        rec_dir,
        [cam_spec],
        clock,
        min_free_bytes=min_free,
        free_bytes_fn=fake_free_bytes,
    )
    recorder.start()
    sent_sink = MemorySink("cam_A")
    wrapped_sink = recorder.sink_for("cam_A", sent_sink)

    with caplog.at_level("WARNING", logger="simai_relay.recording"):
        relay = CameraRelay(cam_spec, wrapped_sink, clock, threading.Event(), None)
        relay.run()

    manifest = recorder.close()
    snap = recorder.snapshot()

    # 송신은 60프레임 모두 나갔다
    assert len(sent_sink.frames) == 60
    # 녹화는 10프레임만 저장하고 디스크 하한으로 멈췄다
    assert recorder.disk_low is True
    assert snap["recorded_frames"] == 10
    assert snap["dropped_disk_low"] == 50
    assert snap["dropped_frames"] == 50
    assert any("디스크 여유 공간이 하한 아래라 녹화를 중단한다" in r.getMessage() for r in caplog.records)
    assert validate_manifest(manifest) == []
    assert verify(rec_dir) == []


def test_cli_run_with_record_dir_creates_valid_manifest(tmp_path: Path):
    """simai-relay run --record-dir 종료 시 manifest.json 이 생성되고 검증을 통과한다."""
    cfg = tmp_path / "cameras.toml"
    cfg.write_text(
        """
[[cameras]]
camera_id = "cam_A"
kind = "synthetic"
width = 64
height = 48
fps = 25
""",
        encoding="utf-8",
    )
    rec_dir = tmp_path / "live-20261109-141000"
    rc = cli.main(["run", "--config", str(cfg), "--seconds", "0.4", "--record-dir", str(rec_dir)])
    assert rc == 0
    manifest_path = rec_dir / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert validate_manifest(manifest) == []
    assert verify(rec_dir) == []
    assert manifest["cameras"][0]["frame_count"] >= 5
