"""카메라 한 대 녹화 루프. FakeClock 으로 실제로 기다리지 않고 결정적으로 돌린다."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from videos import count_decoded

from simai_recorder.annexb import NAL_PPS, NAL_SPS, START_CODE, nal_type, split_annexb
from simai_recorder.capture_time import CaptureTimeModel
from simai_recorder.clock import FakeClock
from simai_recorder.recorder import CameraRecorder, CameraSpec
from simai_recorder.segment import camera_segments, read_frames, read_segment
from simai_recorder.sources import (
    FaultPlan,
    FaultySource,
    FileSource,
    PacketSource,
    SourcePacket,
    StreamInfo,
    SyntheticSource,
)


def _record(tmp_path: Path, source: PacketSource, clock: FakeClock, seconds: float | None = None,
            model: CaptureTimeModel | None = None) -> CameraRecorder:
    spec = CameraSpec("cam_A", source, model or CaptureTimeModel())
    deadline = clock.monotonic() + seconds if seconds is not None else None
    rec = CameraRecorder(spec, tmp_path, clock, threading.Event(), deadline, reconnect_delays_s=(1.0, 2.0))
    rec.run()
    assert rec.error is None
    return rec


def _events(tmp_path: Path) -> list[dict]:
    return [json.loads(x) for x in (tmp_path / "cam_A" / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def _headers(tmp_path: Path) -> list[list[dict]]:
    return [[r["header"] for r in read_frames(p.frames)] for p in camera_segments(tmp_path / "cam_A")]


def test_reconnect_starts_new_epoch_from_keyframe(tmp_path):
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)
    source = FaultySource(synth, FaultPlan(disconnect_after=(30,), failed_reopens=1), clock)
    _record(tmp_path, source, clock, seconds=4.0)

    kinds = [e["event"] for e in _events(tmp_path)]
    assert kinds[:4] == ["connected", "segment_started", "disconnected", "open_failed"]
    assert kinds.count("connected") == 2 and kinds[-2:] == ["stopped", "closed"]

    epochs = _headers(tmp_path)
    assert [h[0]["stream_epoch"] for h in epochs] == [0, 1]
    assert len(epochs[0]) == 30
    for headers in epochs:
        assert headers[0]["keyframe"] is True
        assert [h["frame_sequence"] for h in headers] == list(range(len(headers)))
        for h in headers:
            assert h["timestamp_source"] == "RECEIVE_ESTIMATED"
            # FakeClock 은 지연이 없으므로 추정 촬영 시각이 실제 촬영 시각과 같다
            assert abs(h["estimated_capture_time_ms"] - synth.capture_time_ms(h["source_pts"])) <= 1
    # 끊긴 동안(재접속 대기 1s + 실패 후 1s) 카메라가 찍은 프레임은 받지 못했다
    lost_ms = (epochs[1][0]["source_pts"] - epochs[0][-1]["source_pts"]) / 90
    assert lost_ms >= 2000


def test_file_resume_mid_gop_drops_until_keyframe(tmp_path, mp4_video):
    clock = FakeClock()
    source = FaultySource(FileSource(mp4_video, clock), FaultPlan(disconnect_after=(13,)), clock)
    _record(tmp_path, source, clock)

    started = [e for e in _events(tmp_path) if e["event"] == "segment_started"]
    assert [e["dropped_before_keyframe"] for e in started] == [0, 7]  # 13~19번 패킷(키프레임 20 전)
    epochs = _headers(tmp_path)
    assert [len(h) for h in epochs] == [13, 40]
    assert all(h[0]["keyframe"] for h in epochs)
    assert _events(tmp_path)[-2]["event"] == "eof"

    # 세그먼트 파일만으로 디코딩되고, 사이드카의 (offset, size) 로 프레임을 그대로 꺼낼 수 있다
    for paths, headers in zip(camera_segments(tmp_path / "cam_A"), epochs, strict=True):
        assert count_decoded(paths.h264) == len(headers)
        codec, frames = read_segment(paths)
        assert codec["stream_epoch"] == headers[0]["stream_epoch"]
        payloads = [p for _, p in frames]
        assert len(payloads) == len(headers)
        assert all(split_annexb(p) for p in payloads)  # MP4 avcC 를 Annex B 로 바꿨다


def test_headers_and_codec_info_match_i0_contract(tmp_path, mp4_video, contract_validator):
    clock = FakeClock()
    _record(tmp_path, FileSource(mp4_video, clock), clock)
    for paths in camera_segments(tmp_path / "cam_A"):
        codec, frames = read_segment(paths)
        contract_validator("codec_info", codec)
        for header, _ in frames:
            contract_validator("frame_header", header)


def test_raw_h264_without_pts_leaves_source_pts_null(tmp_path, raw_video):
    clock = FakeClock()
    model = CaptureTimeModel(latency_ms=10)
    _record(tmp_path, FileSource(raw_video, clock, fps=25), clock, model=model)
    (headers,) = _headers(tmp_path)
    assert len(headers) == 60
    assert all(h["source_pts"] is None for h in headers)
    assert all(h["timestamp_uncertainty_ms"] == model.uncertainty_no_pts_ms for h in headers)
    assert all(h["estimated_capture_time_ms"] == h["received_at_ms"] - 10 for h in headers)
    # 25 fps 속도로 흘렸다
    assert headers[-1]["received_at_ms"] - headers[0]["received_at_ms"] == 59 * 40


class _StripParameterSets(PacketSource):
    """키프레임에 SPS/PPS 를 싣지 않고 extradata 로만 주는 카메라 흉내 (RTSP SDP sprop-parameter-sets 와 같은 경우)."""

    kind = "SYNTHETIC"

    def __init__(self, inner: SyntheticSource) -> None:
        self._inner = inner
        self._pending: SourcePacket | None = None

    @staticmethod
    def _split(p: SourcePacket) -> tuple[bytes, SourcePacket]:
        nals = split_annexb(p.data)
        params = b"".join(START_CODE + n for n in nals if nal_type(n) in (NAL_SPS, NAL_PPS))
        body = b"".join(START_CODE + n for n in nals if nal_type(n) not in (NAL_SPS, NAL_PPS))
        return params, SourcePacket(body, p.pts, p.keyframe)

    def open(self) -> StreamInfo:
        info = self._inner.open()
        extradata, self._pending = self._split(self._inner.read())
        return StreamInfo(**{**info.__dict__, "extradata": extradata})

    def read(self) -> SourcePacket | None:
        if self._pending is not None:
            p, self._pending = self._pending, None
            return p
        return self._split(self._inner.read())[1]

    def close(self) -> None:
        self._inner.close()

    def describe(self) -> dict:
        return {"kind": self.kind}


def test_parameter_sets_from_extradata_are_prefixed(tmp_path):
    clock = FakeClock()
    _record(tmp_path, _StripParameterSets(SyntheticSource(clock, width=64, height=48)), clock, seconds=1.0)

    (paths,) = camera_segments(tmp_path / "cam_A")
    rows = list(read_frames(paths.frames))
    assert rows[0]["offset"] > 0  # 파일 앞에 SPS/PPS 를 붙였다
    codec, frames = read_segment(paths)
    assert codec["sps"] and codec["pps"]
    first_payload = next(frames)[1]
    assert {nal_type(n) for n in split_annexb(first_payload)}.isdisjoint({NAL_SPS, NAL_PPS})  # 받은 그대로
    assert count_decoded(paths.h264) == len(rows)


def test_stop_event_ends_recording(tmp_path):
    clock = FakeClock()
    stop = threading.Event()
    synth = SyntheticSource(clock, width=64, height=48)

    class StopAfter(FaultySource):
        def read(self):
            if self._count == 10:
                stop.set()
            return super().read()

    spec = CameraSpec("cam_A", StopAfter(synth, FaultPlan(), clock))
    rec = CameraRecorder(spec, tmp_path, clock, stop, deadline=None)
    rec.run()
    (headers,) = _headers(tmp_path)
    assert len(headers) == 11
    assert _events(tmp_path)[-2]["event"] == "stopped"


class _BundleJumpPts(PacketSource):
    """C110 처럼 PTS 를 13장 묶음마다 400 ms 번갈아 당기는 입력 흉내 (S15P31A101-315). 영상·수신 간격은 그대로다."""

    kind = "SYNTHETIC"

    def __init__(self, inner: SyntheticSource) -> None:
        self._inner = inner
        self._n = 0

    def open(self) -> StreamInfo:
        return self._inner.open()

    def read(self) -> SourcePacket | None:
        p = self._inner.read()
        if p is None:
            return None
        shift = 36000 if (self._n // 13) % 2 else 0  # 400 ms (1/90000)
        self._n += 1
        return SourcePacket(p.data, p.pts + 90000 - shift, p.keyframe)

    def close(self) -> None:
        self._inner.close()

    def describe(self) -> dict:
        return {"kind": self.kind}


def test_broken_pts_timeline_is_recorded_with_frame_clock(tmp_path, contract_validator):
    """깨진 PTS 시각선이면 그 프레임부터 프레임 시계로 매기고 events.jsonl 에 남긴다. 원본 source_pts 는 그대로 쓴다."""
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)
    _record(tmp_path, _BundleJumpPts(synth), clock, seconds=10.0)

    (headers,) = _headers(tmp_path)
    switch = [e for e in _events(tmp_path) if e["event"] == "pts_timeline_broken"]
    assert switch == [{**switch[0], "stream_epoch": 0, "frame_sequence": 13, "clock": "frame_clock"}]
    assert headers[14]["source_pts"] - headers[12]["source_pts"] == 2 * 3600 - 36000  # 원본 PTS 는 바꾸지 않는다
    times = [h["estimated_capture_time_ms"] for h in headers[13:]]
    assert all(b > a for a, b in zip(times, times[1:]))
    for h in headers:
        contract_validator("frame_header", h)
        assert abs(h["estimated_capture_time_ms"] - synth.capture_time_ms(h["frame_sequence"] * 3600)) <= 1
    assert not [e for e in _events(tmp_path) if e["event"] == "capture_time_raised"]  # 뒤로 간 값이 없었다


class _EarlyPts(PacketSource):
    """정해진 패킷의 PTS 만 120 ms 일찍 찍는다(깨짐 기준 150 ms 안). 영상·수신 간격은 그대로다."""

    kind = "SYNTHETIC"

    def __init__(self, inner: SyntheticSource, early: tuple[int, ...]) -> None:
        self._inner, self._early, self._n = inner, early, 0

    def open(self) -> StreamInfo:
        return self._inner.open()

    def read(self) -> SourcePacket | None:
        p = self._inner.read()
        if p is None:
            return None
        self._n += 1
        return SourcePacket(p.data, p.pts - (10800 if self._n - 1 in self._early else 0), p.keyframe)

    def close(self) -> None:
        self._inner.close()

    def describe(self) -> dict:
        return {"kind": self.kind}


def test_capture_time_raise_is_recorded_once_per_epoch(tmp_path, contract_validator):
    """PTS 식 촬영 시각이 뒤로 가면 직전 + 1 ms 로 올리고 불확실성을 올린 양만큼 키운다. events.jsonl 에는 epoch 끝에
    capture_time_raised(올린 프레임 수·최대 올림량) 한 줄만 남긴다(S15P31A101-316)."""
    clock = FakeClock()
    synth = SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0)
    _record(tmp_path, _EarlyPts(synth, early=(20, 30)), clock, seconds=3.0)

    (headers,) = _headers(tmp_path)
    times = [h["estimated_capture_time_ms"] for h in headers]
    assert all(b > a for a, b in zip(times, times[1:]))
    assert [h["frame_sequence"] for h in headers if h["timestamp_uncertainty_ms"] != 20.0] == [20, 30]
    assert headers[20]["timestamp_uncertainty_ms"] == 20.0 + 81 and times[20] == times[19] + 1
    for h in headers:
        contract_validator("frame_header", h)
    raised = [e for e in _events(tmp_path) if e["event"] == "capture_time_raised"]
    assert raised == [{**raised[0], "stream_epoch": 0, "frames": 2, "max_raise_ms": 81}]


def test_camera_segments_supports_large_utc_ms_stream_epoch(tmp_path):
    """EpochStore 처럼 4자리를 넘는 UTC ms stream_epoch 도 숫자 순서로 찾는다(S15P31A101-554)."""
    from simai_recorder.segment import SegmentWriter

    cam_dir = tmp_path / "cam_A"
    for epoch in (9, 1_762_000_000_002, 1_762_000_000_001):
        w = SegmentWriter(
            cam_dir,
            {
                "type": "codec_info",
                "camera_id": "cam_A",
                "stream_epoch": epoch,
                "codec": "H264",
                "width": 64,
                "height": 48,
                "sps": "AAAA",
                "pps": "BBBB",
                "started_at_ms": 1_762_000_000_000,
            },
            prefix_parameter_sets=False,
        )
        w.close()

    segs = camera_segments(cam_dir)
    assert [s.h264.name for s in segs] == [
        "epoch_0009.h264",
        "epoch_1762000000001.h264",
        "epoch_1762000000002.h264",
    ]

