"""여러 카메라 동시 중계: 한 카메라가 멈춰도(수신 정지·전송 막힘) 다른 카메라는 계속 돈다. 실제 시계로 약 1.5초."""

from __future__ import annotations

import threading
import time

import pytest
from simai_recorder.clock import SystemClock
from simai_recorder.recorder import CameraSpec
from simai_recorder.sources import PacketSource, SourceError, SourcePacket, StreamInfo, SyntheticSource

from simai_relay.relay import RelaySession
from simai_relay.sink import I0Frame, MemorySink


class StallingSource(PacketSource):
    """패킷 몇 개를 낸 뒤 read() 가 막힌다(카메라 응답 없음). release 되면 끊김으로 끝난다."""

    kind = "SYNTHETIC"

    def __init__(self, inner: SyntheticSource, after: int, release: threading.Event) -> None:
        self._inner, self._after, self._release = inner, after, release
        self._count = 0

    def open(self) -> StreamInfo:
        return self._inner.open()

    def read(self) -> SourcePacket | None:
        if self._count >= self._after:
            self._release.wait()
            raise SourceError("응답 없음(흉내)")
        self._count += 1
        return self._inner.read()

    def close(self) -> None:
        self._inner.close()

    def describe(self) -> dict:
        return {"kind": self.kind}


class BlockingSink(MemorySink):
    """프레임 몇 개를 받은 뒤 send_frame 이 막힌다(전송 막힘)."""

    def __init__(self, camera_id: str, after: int, release: threading.Event) -> None:
        super().__init__(camera_id)
        self._after, self._release = after, release

    def send_frame(self, record: I0Frame) -> None:
        if len(self.frames) >= self._after:
            self._release.wait()
        super().send_frame(record)


def test_one_camera_stall_does_not_block_others():
    clock = SystemClock()
    release = threading.Event()

    def synth() -> SyntheticSource:
        return SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)

    cameras = [
        CameraSpec("cam_A", StallingSource(synth(), after=5, release=release)),  # 카메라 수신이 멈춤
        CameraSpec("cam_B", synth()),
        CameraSpec("cam_C", synth()),                                            # 전송이 막힘
    ]
    sinks: dict[str, MemorySink] = {}

    def factory(camera_id: str) -> MemorySink:
        sinks[camera_id] = BlockingSink(camera_id, 3, release) if camera_id == "cam_C" else MemorySink(camera_id)
        return sinks[camera_id]

    session = RelaySession(cameras, factory, clock, reconnect_delays_s=(0.2,))
    session.start()
    try:
        time.sleep(1.5)
        b = sinks["cam_B"].frames
        assert len(b) >= 25, f"cam_B 가 밀렸다: {len(b)} 프레임"
        assert [f.header["frame_sequence"] for f in b] == list(range(len(b)))
        snap_b = session.snapshots()[1]
        assert snap_b["rtsp"] == "CONNECTED" and snap_b["camera_to_gateway_gap"]["max_ms"] < 200
        assert len(sinks["cam_A"].frames) == 5
        assert len(sinks["cam_C"].frames) == 3
        entries = {e["camera_id"]: e for e in session.heartbeat_cameras()}
        assert entries["cam_B"]["last_frame_sequence"] >= 24
        assert entries["cam_A"]["last_frame_sequence"] == 4  # 멈춘 카메라는 순번이 더 늘지 않는다
    finally:
        session.stop()
        release.set()
        assert session.join(timeout=10)
    assert session.errors() == {}
    assert all(s.closed for s in sinks.values())


def test_session_rejects_duplicate_camera_ids():
    clock = SystemClock()
    cams = [CameraSpec("cam_A", SyntheticSource(clock)), CameraSpec("cam_A", SyntheticSource(clock))]
    with pytest.raises(ValueError):
        RelaySession(cams, MemorySink, clock)
