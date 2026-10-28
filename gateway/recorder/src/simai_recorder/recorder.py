"""카메라 4대 동시 녹화 (명세 15.3 시나리오 데이터셋, 12.7 I0 프레임 식별, 3.9 촬영 시각 계약).

카메라마다 스레드 하나가 입력 하나를 연다(RTSP 세션 1개, 명세 3.3). 받은 압축 프레임을 디코딩하지 않고 세그먼트에 쓰고,
프레임마다 I0 프레임 헤더를 사이드카에 남긴다.

- stream_epoch: 접속에 성공할 때마다 하나씩 늘린다(재접속 = 새 epoch). frame_sequence 는 epoch 안에서 0 부터 센다.
- 새 epoch 는 키프레임부터 기록한다. 그 전에 온 패킷은 버리고 개수를 events.jsonl 에 남긴다.
- 끊기면 정해진 간격으로 다시 접속한다. 녹화 시간이 끝나거나 stop 이 걸리면 멈춘다.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from simai_recorder.capture_time import CaptureTimeModel, ReceiveTimeEstimator
from simai_recorder.clock import Clock
from simai_recorder.i0 import build_codec_info, build_frame_header, pts_timebase_string
from simai_recorder.segment import SegmentWriter
from simai_recorder.sources import PacketSource, SourceError, StreamInfo, release

SESSION_FILE = "session.json"
EVENTS_FILE = "events.jsonl"
SESSION_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
CAMERA_ID = re.compile(r"[A-Za-z0-9_-]+")


@dataclass
class CameraSpec:
    camera_id: str
    source: PacketSource
    capture_time: CaptureTimeModel = field(default_factory=CaptureTimeModel)
    firmware: str | None = None

    def __post_init__(self) -> None:
        if not CAMERA_ID.fullmatch(self.camera_id):
            raise ValueError(f"camera_id {self.camera_id!r}: 영문·숫자·_·- 만 쓴다")


@dataclass
class SessionSpec:
    """녹화 세션 설정. session.json(→ manifest)에 그대로 남는 값들이다. 비밀 값은 들지 않는다."""

    session_id: str
    scenario: str
    cameras: Sequence[CameraSpec]
    storage_uri: str
    consent: dict[str, Any] | None = None
    description: str = ""
    calibration_version: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not SESSION_ID.fullmatch(self.session_id):
            raise ValueError(f"session_id {self.session_id!r}: 영문 소문자·숫자·- 만 쓴다")
        ids = [c.camera_id for c in self.cameras]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("cameras 는 비어 있지 않고 camera_id 가 겹치지 않아야 한다")


class CameraRecorder:
    """카메라 한 대의 녹화 루프. run() 은 녹화가 끝날 때까지 돌아오지 않는다(스레드 하나에서 돈다)."""

    def __init__(self, spec: CameraSpec, session_dir: Path, clock: Clock, stop: threading.Event,
                 deadline: float | None, reconnect_delays_s: Sequence[float] = (1.0, 2.0, 5.0)) -> None:
        self.spec = spec
        self.camera_dir = session_dir / spec.camera_id
        self._clock = clock
        self._stop = stop
        self._deadline = deadline
        self._delays = tuple(reconnect_delays_s) or (1.0,)
        self._events: TextIO | None = None
        self.error: BaseException | None = None

    def _should_stop(self) -> bool:
        return self._stop.is_set() or (self._deadline is not None and self._clock.monotonic() >= self._deadline)

    def _event(self, event: str, **fields: Any) -> None:
        assert self._events is not None
        row = {"t_ms": self._clock.now_ms(), "camera_id": self.spec.camera_id, "event": event, **fields}
        self._events.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._events.flush()

    def _wait(self, seconds: float) -> None:
        end = self._clock.monotonic() + seconds
        while not self._should_stop() and (left := end - self._clock.monotonic()) > 0:
            self._clock.sleep(min(left, 0.2))

    def run(self) -> None:
        self.camera_dir.mkdir(parents=True, exist_ok=False)
        source = self.spec.source
        with (self.camera_dir / EVENTS_FILE).open("x", encoding="utf-8", newline="\n") as self._events:
            try:
                self._loop(source)
            except BaseException as e:  # 스레드 밖으로 알리고 다른 카메라는 계속 녹화한다
                self.error = e
                self._event("error", detail=f"{type(e).__name__}: {e}")
            finally:
                release(source)
                self._event("closed")

    def _loop(self, source: PacketSource) -> None:
        epoch, failures = 0, 0
        while not self._should_stop():
            try:
                info = source.open()
            except SourceError as e:
                delay = self._delays[min(failures, len(self._delays) - 1)]
                self._event("open_failed", detail=str(e), retry_in_s=delay)
                failures += 1
                self._wait(delay)
                continue
            failures = 0
            self._event("connected", stream_epoch=epoch, width=info.width, height=info.height,
                        nominal_fps=info.nominal_fps)
            try:
                outcome = self._record_epoch(source, info, epoch)
            finally:
                source.close()
            epoch += 1
            if outcome != "disconnected":
                break
            self._wait(self._delays[0])

    def _record_epoch(self, source: PacketSource, info: StreamInfo, epoch: int) -> str:
        model = self.spec.capture_time
        estimator = ReceiveTimeEstimator(model, info.pts_timebase, info.nominal_fps)
        writer: SegmentWriter | None = None
        timebase = pts_timebase_string(info)
        dropped = 0
        try:
            while True:
                if self._should_stop():
                    self._event("stopped", stream_epoch=epoch, frames=writer.frame_count if writer else 0)
                    return "stopped"
                try:
                    packet = source.read()
                except SourceError as e:
                    self._event("disconnected", stream_epoch=epoch, detail=str(e),
                                frames=writer.frame_count if writer else 0)
                    return "disconnected"
                if packet is None:
                    self._event("eof", stream_epoch=epoch, frames=writer.frame_count if writer else 0)
                    return "eof"
                received_at_ms = self._clock.now_ms()
                if writer is None:
                    codec = build_codec_info(self.spec.camera_id, info, epoch, packet.data) if packet.keyframe else None
                    if codec is None:
                        dropped += 1
                        continue
                    writer = SegmentWriter(self.camera_dir, codec[0], prefix_parameter_sets=codec[1])
                    self._event("segment_started", stream_epoch=epoch, dropped_before_keyframe=dropped)
                t = estimator.estimate(received_at_ms, packet.pts)
                if estimator.broken_since == writer.frame_count:  # 깨진 PTS 시각선 → 이 프레임부터 프레임 시계(capture_time.py)
                    self._event("pts_timeline_broken", stream_epoch=epoch, frame_sequence=writer.frame_count,
                                clock=estimator.mode)
                header = build_frame_header(self.spec.camera_id, epoch, writer.frame_count, packet, timebase,
                                            model, t, received_at_ms)
                writer.write(header, packet.data)
        finally:
            if estimator.raised:  # 단조 보장으로 올린 프레임(S15P31A101-316): 프레임마다 남기지 않고 epoch 끝에 요약 한 줄
                self._event("capture_time_raised", stream_epoch=epoch, frames=estimator.raised,
                            max_raise_ms=estimator.max_raise_ms)
            if writer is not None:
                writer.close()


def record_session(spec: SessionSpec, out_root: Path, clock: Clock, duration_s: float | None,
                   stop: threading.Event | None = None, recorded_by: dict[str, Any] | None = None,
                   reconnect_delays_s: Sequence[float] = (1.0, 2.0, 5.0)) -> Path:
    """카메라를 모두 동시에 녹화하고 manifest.json 을 쓴다. 세션 디렉터리를 돌려준다."""
    from simai_recorder.manifest import write_manifest

    session_dir = out_root / spec.session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    stop = stop or threading.Event()
    started_at_ms = clock.now_ms()
    session: dict[str, Any] = {
        "session_id": spec.session_id,
        "scenario": spec.scenario,
        "description": spec.description,
        "calibration_version": spec.calibration_version,
        "consent": spec.consent,
        "storage_uri": spec.storage_uri,
        "notes": spec.notes,
        "recorded_by": recorded_by or {},
        "requested_duration_s": duration_s,
        "started_at_ms": started_at_ms,
        "ended_at_ms": None,
        "cameras": [
            {"camera_id": c.camera_id, "source": c.source.describe(), "firmware": c.firmware,
             "capture_time_model": c.capture_time.to_dict()}
            for c in spec.cameras
        ],
    }
    _write_json(session_dir / SESSION_FILE, session)

    deadline = clock.monotonic() + duration_s if duration_s is not None else None
    recorders = [CameraRecorder(c, session_dir, clock, stop, deadline, reconnect_delays_s) for c in spec.cameras]
    threads = [threading.Thread(target=r.run, name=f"rec-{r.spec.camera_id}", daemon=True) for r in recorders]
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(0.2)
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join()
    session["ended_at_ms"] = clock.now_ms()
    session["camera_errors"] = {r.spec.camera_id: repr(r.error) for r in recorders if r.error is not None}
    _write_json(session_dir / SESSION_FILE, session)
    write_manifest(session_dir)
    return session_dir


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
