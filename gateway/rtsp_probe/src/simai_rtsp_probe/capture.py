"""RTSP 수신 측정 루프와 동시 접속 시험 (명세 3.3 카메라 사양·RTSP 접속 규칙).

수신은 녹화 도구·영상 중계와 같은 입력(simai_recorder.sources.RtspSource, PyAV, RTSP over TCP, 디코딩 없음)으로 한다.
그래서 측정값이 운영 수신 경로의 값과 같다. 짧은 원본 녹화는 녹화 도구의 세그먼트 형식(epoch_0000.h264·frames.jsonl·
codec_info.json, 같은 함수로 만든 I0 헤더)으로 남겨 `simai_recorder.segment.read_segment` 로 다시 읽을 수 있다.

측정은 다시 접속하지 않는다. 끊기면 거기서 멈추고 보고서에 남긴다(끊김 자체가 측정 결과다).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

from simai_recorder.capture_time import CaptureTimeModel, ReceiveTimeEstimator
from simai_recorder.clock import Clock
from simai_recorder.i0 import build_codec_info, build_frame_header, pts_timebase_string
from simai_recorder.segment import SegmentWriter
from simai_recorder.sources import PacketSource, SourceError, SourcePacket, StreamInfo

from simai_rtsp_probe.report import Observation


@dataclass
class Measurement:
    info: StreamInfo | None = None
    observations: list[Observation] = field(default_factory=list)
    ended_by: str = "not_started"  # completed / disconnected / stream_ended / open_failed / stopped
    error: str | None = None
    recording: dict[str, Any] | None = None


class _ShortRecording:
    """측정 시작 뒤 첫 키프레임부터 seconds 동안 녹화 도구 세그먼트 형식으로 쓴다(epoch 0 하나)."""

    def __init__(self, camera_dir: Path, camera_id: str, seconds: float, info: StreamInfo) -> None:
        self.camera_dir, self.camera_id, self.seconds, self.info = camera_dir, camera_id, seconds, info
        self.model = CaptureTimeModel()
        self.estimator = ReceiveTimeEstimator(self.model, info.pts_timebase)
        self.timebase = pts_timebase_string(info)
        self.writer: SegmentWriter | None = None
        self.started_mono: float | None = None
        self.done = False
        self.dropped_before_keyframe = 0

    def feed(self, packet: SourcePacket, received_at_ms: int, mono: float) -> None:
        if self.done:
            return
        if self.writer is None:
            codec = build_codec_info(self.camera_id, self.info, 0, packet.data) if packet.keyframe else None
            if codec is None:
                self.dropped_before_keyframe += 1
                return
            self.writer = SegmentWriter(self.camera_dir, codec[0], prefix_parameter_sets=codec[1])
            self.started_mono = mono
        assert self.started_mono is not None
        if mono - self.started_mono >= self.seconds:
            self.close()
            return
        t = self.estimator.estimate(received_at_ms, packet.pts)
        header = build_frame_header(self.camera_id, 0, self.writer.frame_count, packet, self.timebase, self.model, t,
                                    received_at_ms)
        self.writer.write(header, packet.data)

    def close(self) -> None:
        self.done = True
        if self.writer is not None:
            self.writer.close()

    def summary(self, root: Path) -> dict[str, Any]:
        if self.writer is None:
            return {"frames": 0, "files": None, "note": "키프레임(SPS/PPS 포함)을 받지 못해 녹화하지 않았다",
                    "dropped_before_keyframe": self.dropped_before_keyframe}
        p = self.writer.paths
        return {"frames": self.writer.frame_count, "dropped_before_keyframe": self.dropped_before_keyframe,
                "files": {k: str(v.relative_to(root)).replace("\\", "/")
                          for k, v in (("h264", p.h264), ("frames", p.frames), ("codec_info", p.codec_info))}}


def measure(source: PacketSource, clock: Clock, seconds: float, *, record_dir: Path | None = None,
            camera_id: str = "cam", record_seconds: float = 0.0, stop: threading.Event | None = None) -> Measurement:
    """source 를 한 번 열어 seconds 동안 받는다. 닫기는 늘 한다(카메라 세션을 남기지 않는다, 명세 3.3)."""
    m = Measurement()
    recording: _ShortRecording | None = None
    try:
        try:
            m.info = source.open()
        except SourceError as e:
            m.ended_by, m.error = "open_failed", str(e)
            return m
        if record_dir is not None and record_seconds > 0:
            recording = _ShortRecording(record_dir / camera_id, camera_id, record_seconds, m.info)
        on_packet = None if recording is None else recording.feed
        received = _receive_open(source, m.info, clock, seconds, stop=stop, on_packet=on_packet)
        m.observations, m.ended_by, m.error = received.observations, received.ended_by, received.error
    finally:
        source.close()
        if recording is not None:
            recording.close()
            assert record_dir is not None
            m.recording = recording.summary(record_dir)
    return m


def concurrency_trials(make_source: Callable[[], PacketSource], clock: Clock, max_sessions: int, seconds: float,
                       open_gap_s: float = 1.0, rest_s: float = 2.0) -> list[dict[str, Any]]:
    """같은 카메라에 세션 1개, 2개, … max_sessions 개를 차례로 열어 동시에 받아 본다(C110 은 2세션까지, 명세 3.3).

    시도마다 세션을 open_gap_s 간격으로 하나씩 열고, 열린 세션 모두를 seconds 동안 동시에 받은 뒤 전부 닫는다.
    다음 시도 전에 rest_s 쉰다(카메라가 세션을 정리할 시간).
    """
    trials = []
    for n in range(1, max_sessions + 1):
        sources: list[PacketSource] = []
        sessions: list[dict[str, Any]] = []
        infos: list[StreamInfo | None] = []
        try:
            for i in range(n):
                src = make_source()
                try:
                    infos.append(src.open())
                    sources.append(src)
                    sessions.append({"session": i + 1, "opened": True})
                except SourceError as e:
                    src.close()
                    sessions.append({"session": i + 1, "opened": False, "error": str(e)})
                if i + 1 < n:
                    clock.sleep(open_gap_s)
            results: list[Measurement | None] = [None] * len(sources)

            def run(k: int) -> None:
                try:
                    results[k] = _receive_open(sources[k], infos[k], clock, seconds)
                except Exception as e:  # 한 세션의 예기치 않은 오류가 시험 전체를 멈추지 않게 남긴다
                    results[k] = Measurement(info=infos[k], ended_by="error", error=type(e).__name__)

            threads = [threading.Thread(target=run, args=(k,), daemon=True) for k in range(len(sources))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            for src in sources:
                src.close()
        opened = [s for s in sessions if s["opened"]]
        for s, r in zip(opened, results):
            assert r is not None
            obs = r.observations
            span = obs[-1].received_mono_s - obs[0].received_mono_s if len(obs) >= 2 else 0.0
            s.update(frames=len(obs), fps_by_arrival=round((len(obs) - 1) / span, 2) if span > 0 else None,
                     ended_by=r.ended_by, error=r.error)
        receiving = sum(1 for s in opened if (s.get("frames") or 0) > 0 and s.get("ended_by") == "completed")
        trials.append({"requested_sessions": n, "opened": len(opened), "received_whole_time": receiving,
                       "sessions": sessions})
        if n < max_sessions:
            clock.sleep(rest_s)
    return trials


def _receive_open(source: PacketSource, info: StreamInfo | None, clock: Clock, seconds: float,
                  stop: threading.Event | None = None,
                  on_packet: Callable[[SourcePacket, int, float], None] | None = None) -> Measurement:
    """이미 연 source 에서 seconds 동안 받는다(닫지 않는다). 받은 시각은 패킷을 받은 직후에 잰다."""
    m = Measurement(info=info, ended_by="completed")
    deadline = clock.monotonic() + seconds
    while True:
        if stop is not None and stop.is_set():
            m.ended_by = "stopped"
            break
        if clock.monotonic() >= deadline:
            break
        try:
            packet = source.read()
        except SourceError as e:
            m.ended_by, m.error = "disconnected", str(e)
            break
        if packet is None:
            m.ended_by = "stream_ended"
            break
        mono, now_ms = clock.monotonic(), clock.now_ms()
        m.observations.append(Observation(received_mono_s=mono, received_at_ms=now_ms, pts=packet.pts,
                                          keyframe=packet.keyframe, size=len(packet.data),
                                          camera_time_us=packet.camera_time_us, sender_report=packet.sender_report))
        if on_packet is not None:
            on_packet(packet, now_ms, mono)
    return m


def max_concurrent(trials: list[dict[str, Any]]) -> int | None:
    """모든 세션이 끝까지 받은 가장 큰 시도의 세션 수. 하나도 없으면 None."""
    ok = [t["requested_sessions"] for t in trials if t["received_whole_time"] == t["requested_sessions"]]
    return max(ok) if ok else None
