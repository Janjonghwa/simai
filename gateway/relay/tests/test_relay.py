"""카메라 한 대 수신 루프: I0 계약, stream_epoch, 키프레임 시작, 촬영 시각, 끊김 측정.

FakeClock 으로 실제로 기다리지 않고 결정적으로 돈다. 입력은 합성 스트림(PyAV 메모리 인코딩)과 로컬 파일이다.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

import av
import pytest
from simai_recorder.capture_time import CaptureTimeModel
from simai_recorder.clock import FakeClock
from simai_recorder.recorder import CameraSpec
from simai_recorder.sources import (
    FaultPlan,
    FaultySource,
    FileSource,
    PacketSource,
    SourcePacket,
    StreamInfo,
    SyntheticSource,
)

from simai_relay.link_stats import CameraLinkStats
from simai_relay.relay import CameraRelay
from simai_relay.sink import I0CodecInfo, I0Frame, MemorySink


def _relay(source: PacketSource, clock: FakeClock, seconds: float | None = None,
           model: CaptureTimeModel | None = None) -> tuple[CameraRelay, MemorySink]:
    sink = MemorySink("cam_A")
    deadline = clock.monotonic() + seconds if seconds is not None else None
    relay = CameraRelay(CameraSpec("cam_A", source, model or CaptureTimeModel()), sink, clock, threading.Event(),
                        deadline, reconnect_delays_s=(1.0, 2.0))
    relay.run()
    assert relay.error is None
    assert sink.closed
    return relay, sink


def _epochs(sink: MemorySink) -> list[tuple[dict, list[I0Frame]]]:
    """기록을 epoch 별로 묶는다. codec_info 가 늘 그 epoch 의 프레임보다 먼저 와야 한다."""
    groups: list[tuple[dict, list[I0Frame]]] = []
    for record in sink.records:
        if isinstance(record, I0CodecInfo):
            groups.append((record.codec_info, []))
        else:
            assert groups, "codec_info 전에 프레임이 왔다"
            groups[-1][1].append(record)
    return groups


def test_records_follow_i0_contract_and_order(contract_validator):
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)
    _, sink = _relay(synth, clock, seconds=1.95)

    [(codec, frames)] = _epochs(sink)
    contract_validator("codec_info", codec)
    assert codec["stream_epoch"] == 0 and codec["pts_timebase"] == "1/90000" and codec["nominal_fps"] == 25
    assert len(frames) == 50
    assert frames[0].header["keyframe"] is True
    assert [f.header["frame_sequence"] for f in frames] == list(range(50))
    for f in frames:
        h = f.header
        contract_validator("frame_header", h)
        assert h["camera_id"] == "cam_A" and h["stream_epoch"] == codec["stream_epoch"]
        assert h["timestamp_source"] == "RECEIVE_ESTIMATED" and h["timestamp_uncertainty_ms"] == 20.0
        assert isinstance(h["estimated_capture_time_ms"], int) and isinstance(h["received_at_ms"], int)
        # FakeClock 은 전송 지연이 없으므로 추정 촬영 시각이 실제 촬영 시각과 같다
        assert abs(h["estimated_capture_time_ms"] - synth.capture_time_ms(h["source_pts"])) <= 1
    assert sum(f.header["keyframe"] for f in frames) == 2

    # 압축 프레임을 받은 그대로 넘긴다: 이어 붙이면 그대로 디코딩된다
    decoder = av.CodecContext.create("h264", "r")
    decoded = sum(len(decoder.decode(av.Packet(f.data))) for f in frames) + len(decoder.decode(None))
    assert decoded == 50


def test_reconnect_increments_stream_epoch(contract_validator):
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)
    source = FaultySource(synth, FaultPlan(disconnect_after=(20, 15), failed_reopens=1), clock)
    relay, sink = _relay(source, clock, seconds=6.0)

    groups = _epochs(sink)
    assert [codec["stream_epoch"] for codec, _ in groups] == [0, 1, 2]
    assert [len(frames) for _, frames in groups[:2]] == [20, 15]
    for codec, frames in groups:
        contract_validator("codec_info", codec)
        assert frames[0].header["keyframe"] is True
        assert {f.header["stream_epoch"] for f in frames} == {codec["stream_epoch"]}
        assert [f.header["frame_sequence"] for f in frames] == list(range(len(frames)))
    snap = relay.stats.snapshot()
    assert snap["connects"] == 3 and snap["open_failures"] == 2 and snap["stream_epoch"] == 2
    assert relay.stats.heartbeat_entry()["stream_epoch"] == 2


def test_resume_mid_gop_starts_from_keyframe(mp4_video):
    clock = FakeClock()
    source = FaultySource(FileSource(mp4_video, clock), FaultPlan(disconnect_after=(13,)), clock)
    relay, sink = _relay(source, clock)

    groups = _epochs(sink)
    assert [len(frames) for _, frames in groups] == [13, 40]  # 13~19번 패킷(키프레임 20 전)은 버림
    assert all(frames[0].header["keyframe"] for _, frames in groups)
    assert relay.stats.snapshot()["dropped_before_keyframe"] == 7


def test_initial_epoch_is_kept():
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25)
    sink = MemorySink("cam_A")
    relay = CameraRelay(CameraSpec("cam_A", synth), sink, clock, threading.Event(), clock.monotonic() + 0.5,
                        initial_epoch=7)
    relay.run()
    assert {f.header["stream_epoch"] for f in sink.frames} == {7}


def test_camera_to_gateway_gap_and_receive_delay(contract_validator, contract_example):
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25)
    source = FaultySource(synth, FaultPlan(stalls={10: 200}), clock)  # 10번 프레임이 200ms 늦게 도착
    relay, sink = _relay(source, clock, seconds=1.95)

    gap = relay.stats.snapshot()["camera_to_gateway_gap"]
    assert gap["threshold_ms"] == 60.0
    assert gap["count"] == 1 and gap["max_ms"] == 240.0
    assert gap["pts_gap_count"] == 0 and gap["pts_interval_max_ms"] == 40.0  # 카메라는 다 보냈다
    delay = relay.stats.snapshot()["receive_delay_ms"]
    assert delay["max"] == 200.0 and delay["p50"] == 0.0
    late = sink.frames[10].header
    assert late["received_at_ms"] - late["estimated_capture_time_ms"] == 200

    # laptop_heartbeat payload.cameras 항목: 직전 하트비트 이후 도착 간격 최대
    entry = relay.stats.heartbeat_entry()
    assert entry == {"camera_id": "cam_A", "rtsp": "DISCONNECTED", "stream_epoch": 0,
                     "last_frame_sequence": 49, "camera_to_gateway_gap_max_ms": 240.0,
                     "source_kind": "SYNTHETIC"}  # FaultySource 는 안쪽 입력 종류를 그대로 알린다(S15P31A101-724)
    heartbeat = contract_example("laptop_heartbeat")
    heartbeat["payload"]["cameras"] = [entry, relay.stats.heartbeat_entry()]
    contract_validator("laptop_heartbeat", heartbeat)
    assert "camera_to_gateway_gap_max_ms" not in heartbeat["payload"]["cameras"][1]  # 창을 비웠고 새 프레임이 없다


@pytest.mark.parametrize(("kind", "expected"), [("RTSP", "RTSP"), ("FILE", "FILE"), ("SYNTHETIC", "SYNTHETIC"),
                                                ("other", None), (None, None)])
def test_heartbeat_entry_source_kind(kind, expected, contract_validator, contract_example) -> None:
    """S15P31A101-724: 중계가 연 입력 종류를 laptop_heartbeat cameras[].source_kind 로 싣는다. 모르는 값은 싣지 않는다."""
    entry = CameraLinkStats("cam_A", source_kind=kind).heartbeat_entry()
    assert entry.get("source_kind") == expected
    heartbeat = contract_example("laptop_heartbeat")
    heartbeat["payload"]["cameras"] = [entry]
    contract_validator("laptop_heartbeat", heartbeat)


class ListSource(PacketSource):
    """미리 만든 패킷을 fps 간격으로 낸다. 끝나면 None."""

    kind = "SYNTHETIC"

    def __init__(self, info: StreamInfo, packets: Sequence[SourcePacket], clock: FakeClock) -> None:
        self._info, self._packets, self._clock = info, list(packets), clock
        self._i = 0

    def open(self) -> StreamInfo:
        return self._info

    def read(self) -> SourcePacket | None:
        if self._i >= len(self._packets):
            return None
        self._clock.sleep(0.04)
        self._i += 1
        return self._packets[self._i - 1]

    def close(self) -> None:
        pass

    def describe(self) -> dict:
        return {"kind": self.kind}


def test_pts_gap_counts_frames_the_camera_never_sent():
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25)
    info = synth.open()
    packets = [synth.read() for _ in range(30)]
    del packets[15]  # 카메라가 보내지 않은 프레임: 도착 간격은 정상, PTS 간격만 80ms
    relay, sink = _relay(ListSource(info, packets, clock), clock)

    gap = relay.stats.snapshot()["camera_to_gateway_gap"]
    assert gap["pts_gap_count"] == 1 and gap["pts_interval_max_ms"] == 80.0
    assert gap["count"] == 0 and gap["max_ms"] == 40.0
    assert len(sink.frames) == 29


def test_no_pts_uses_receive_time_with_wider_uncertainty(contract_validator):
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25)
    info = synth.open()
    packets = [SourcePacket(p.data, None, p.keyframe) for p in (synth.read() for _ in range(10))]
    no_pts = StreamInfo(**{**info.__dict__, "pts_timebase": None})
    model = CaptureTimeModel(latency_ms=30)
    _, sink = _relay(ListSource(no_pts, packets, clock), clock, model=model)

    for f in sink.frames:
        h = f.header
        contract_validator("frame_header", h)
        assert h["source_pts"] is None and h["pts_timebase"] == "1/90000"
        assert h["estimated_capture_time_ms"] == h["received_at_ms"] - 30
        assert h["timestamp_uncertainty_ms"] == 45.0


def test_broken_pts_timeline_switches_to_frame_clock_per_epoch(contract_validator, caplog):
    """C110 묶음 점프 PTS(13장마다 400 ms 번갈아): 헤더 촬영 시각이 뒤로 가지 않고, epoch 마다 한 번 WARNING 을 남긴다
    (S15P31A101-315). 재접속한 epoch 는 PTS 식으로 다시 시작해 다시 감지한다. 원본 source_pts·received_at_ms 는 그대로다."""
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)
    info = synth.open()
    packets = []
    for i in range(300):
        p = synth.read()
        shift = 36000 if (i // 13) % 2 else 0  # 400 ms (1/90000)
        packets.append(SourcePacket(p.data, p.pts + 90000 - shift, p.keyframe))
    source = FaultySource(ListSource(info, packets, clock), FaultPlan(disconnect_after=(120,)), clock)
    with caplog.at_level("WARNING", logger="simai_relay"):
        _, sink = _relay(source, clock)

    epochs = _epochs(sink)
    assert len(epochs) == 2
    warnings = [r.getMessage() for r in caplog.records if "pts_timeline_broken" in r.getMessage()]
    assert len(warnings) == 2 and "stream_epoch=0 frame_sequence=13 clock=frame_clock" in warnings[0]
    for _, frames in epochs:
        times = [f.header["estimated_capture_time_ms"] for f in frames]
        assert all(b >= a for a, b in zip(times, times[1:]))
        for f in frames:
            contract_validator("frame_header", f.header)
            assert f.header["estimated_capture_time_ms"] <= f.header["received_at_ms"]
    first = epochs[0][1]
    assert first[14].header["source_pts"] - first[12].header["source_pts"] == 2 * 3600 - 36000


def test_capture_time_raise_is_summarized_once_per_epoch(contract_validator, caplog):
    """PTS 가 한 장 일찍 찍히면(깨짐 기준 150 ms 안이라 PTS 식 그대로) 촬영 시각이 직전보다 뒤로 간다. 직전 + 1 ms 로 올리고
    불확실성을 올린 양만큼 키운다. 로그는 프레임마다가 아니라 epoch 끝에 한 줄이다(S15P31A101-316): 앞 60장 안에서 25 ms 이하로만
    올렸으면 INFO(접속 직후 몰림과 같은 경우), 25 ms 넘게 올렸거나 60장 뒤에도 올렸으면 WARNING."""
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)
    info = synth.open()
    early = {20: 5400, 60: 10800, 170: 5400}  # 패킷 번호: 당긴 PTS(60 ms → 21 ms 올림, 120 ms → 81 ms 올림)
    packets = []
    for i in range(200):
        p = synth.read()
        packets.append(SourcePacket(p.data, p.pts - early.get(i, 0), p.keyframe))
    # 연결 0: 패킷 0~39, 연결 1: 50~79(키프레임부터), 연결 2: 100~199. 패킷 170 은 연결 2 의 70장째
    source = FaultySource(ListSource(info, packets, clock), FaultPlan(disconnect_after=(40, 40)), clock)
    with caplog.at_level("INFO", logger="simai_relay"):
        _, sink = _relay(source, clock)

    epochs = _epochs(sink)
    assert len(epochs) == 3
    for _, frames in epochs:
        times = [f.header["estimated_capture_time_ms"] for f in frames]
        assert all(b > a for a, b in zip(times, times[1:]))
        for f in frames:
            contract_validator("frame_header", f.header)
    first = epochs[0][0]["stream_epoch"]
    raised = [f.header for _, frames in epochs for f in frames if f.header["timestamp_uncertainty_ms"] != 20.0]
    assert [(h["stream_epoch"] - first, h["frame_sequence"], h["timestamp_uncertainty_ms"]) for h in raised] == [
        (0, 20, 41.0), (1, 10, 101.0), (2, 70, 41.0)]
    lines = [(r.levelname, r.getMessage()) for r in caplog.records if "capture_time_raised" in r.getMessage()]
    assert [level for level, _ in lines] == ["INFO", "WARNING", "WARNING"]
    assert [m.split("capture_time_raised ")[1].split(" ", 1)[1] for _, m in lines] == [
        "frames=1 max_raise_ms=21", "frames=1 max_raise_ms=81", "frames=1 max_raise_ms=21"]
