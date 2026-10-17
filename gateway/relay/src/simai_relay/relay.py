"""카메라별 RTSP 수신과 I0 촬영 시각 헤더 (명세 12.7 I0 프레임 식별, 3.9 촬영 시각 추정, 3.3 RTSP 접속 규칙).

카메라마다 스레드 하나가 입력 하나를 연다(RTSP 세션 1개, over TCP). 압축 프레임(H.264 접근 단위)을 디코딩하지 않고
받은 그대로, 프레임마다 I0 frame_header 를 붙여 그 카메라의 CameraSink 로 넘긴다. 카메라 사이에 공유하는 잠금·큐가
없으므로 한 카메라가 멈추거나(수신 정지·전송 지연) 끊겨도 다른 카메라는 계속 돈다.

- stream_epoch: 접속에 성공할 때마다 하나씩 늘린다(재접속 = 새 epoch). frame_sequence 는 epoch 안에서 0 부터.
- 새 epoch 는 codec_info 를 먼저 넘기고 키프레임부터 프레임을 넘긴다. 키프레임 전에 온 패킷은 버리고 개수를 센다.
- 촬영 시각은 녹화 도구와 같은 추정기(simai_recorder.capture_time.ReceiveTimeEstimator)로, 헤더는 같은 함수
  (simai_recorder.i0)로 만든다. 녹화 파일과 LIVE 헤더가 같은 규칙을 따른다.
- 비밀 값: RTSP 계정은 입력(RtspSource) 안에만 있다. 로그·상태에는 camera_id 와 입력 설명(계정 없음)만 쓴다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import Any

from simai_recorder.capture_time import ReceiveTimeEstimator
from simai_recorder.clock import Clock
from simai_recorder.i0 import build_codec_info, build_frame_header, pts_timebase_string
from simai_recorder.recorder import CameraSpec
from simai_recorder.sources import PacketSource, SourceError, StreamInfo, release

from simai_relay.link_stats import CameraLinkStats
from simai_relay.sink import CameraSink, I0CodecInfo, I0Frame, SinkFactory

log = logging.getLogger("simai_relay")

DEFAULT_RECONNECT_DELAYS_S = (1.0, 2.0, 5.0)


class CameraRelay:
    """카메라 한 대의 수신 루프. run() 은 멈출 때까지 돌아오지 않는다(스레드 하나에서 돈다).

    initial_epoch: 첫 접속의 stream_epoch. 중계 프로세스를 다시 띄울 때 이어서 쓰려면 준다(기본 0).
    """

    def __init__(self, spec: CameraSpec, sink: CameraSink, clock: Clock, stop: threading.Event,
                 deadline: float | None = None, reconnect_delays_s: Sequence[float] = DEFAULT_RECONNECT_DELAYS_S,
                 initial_epoch: int = 0) -> None:
        if initial_epoch < 0:
            raise ValueError("initial_epoch 는 0 이상이다")
        self.spec = spec
        self.sink = sink
        self.stats = CameraLinkStats(spec.camera_id)
        self._clock = clock
        self._stop = stop
        self._deadline = deadline
        self._delays = tuple(reconnect_delays_s) or (1.0,)
        self._next_epoch = initial_epoch
        self.error: BaseException | None = None

    @property
    def camera_id(self) -> str:
        return self.spec.camera_id

    def _should_stop(self) -> bool:
        return self._stop.is_set() or (self._deadline is not None and self._clock.monotonic() >= self._deadline)

    def _wait(self, seconds: float) -> None:
        end = self._clock.monotonic() + seconds
        while not self._should_stop() and (left := end - self._clock.monotonic()) > 0:
            self._clock.sleep(min(left, 0.2))

    def _event(self, level: int, event: str, **fields: Any) -> None:
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        log.log(level, "%s %s %s", self.camera_id, event, detail)

    def run(self) -> None:
        source = self.spec.source
        self._event(logging.INFO, "start", source=source.describe())
        try:
            self._loop(source)
        except BaseException as e:  # 스레드 밖으로 알리고 다른 카메라는 계속 돈다
            self.error = e
            # 우리 코드가 만든 SourceError 문장만 쓴다. 그 밖의 예외 문장에는 주소 같은 값이 섞일 수 있어 종류만 남긴다.
            self._event(logging.ERROR, "error", kind=type(e).__name__)
        finally:
            self.stats.on_disconnected()
            try:
                release(source)
            finally:
                self.sink.close()
                self._event(logging.INFO, "closed")

    def _loop(self, source: PacketSource) -> None:
        failures = 0
        while not self._should_stop():
            try:
                info = source.open()
            except SourceError as e:
                delay = self._delays[min(failures, len(self._delays) - 1)]
                failures += 1
                self.stats.on_open_failed()
                self._event(logging.WARNING, "open_failed", detail=str(e), retry_in_s=delay)
                self._wait(delay)
                continue
            failures = 0
            epoch = self._next_epoch
            self._next_epoch += 1
            self.stats.on_connected(epoch)
            self._event(logging.INFO, "connected", stream_epoch=epoch, width=info.width, height=info.height,
                        nominal_fps=info.nominal_fps)
            try:
                outcome = self._relay_epoch(source, info, epoch)
            finally:
                source.close()
                self.stats.on_disconnected()
            if outcome != "disconnected":
                break
            self._wait(self._delays[0])

    def _relay_epoch(self, source: PacketSource, info: StreamInfo, epoch: int) -> str:
        cid = self.camera_id
        model = self.spec.capture_time
        estimator = ReceiveTimeEstimator(model, info.pts_timebase)  # epoch 마다 새로 (PTS 원점이 바뀐다)
        timebase = pts_timebase_string(info)
        sequence: int | None = None  # None: codec_info·키프레임 대기
        dropped = 0
        while True:
            if self._should_stop():
                self._event(logging.INFO, "stopped", stream_epoch=epoch, frames=sequence or 0)
                return "stopped"
            try:
                packet = source.read()
            except SourceError as e:
                self._event(logging.WARNING, "disconnected", stream_epoch=epoch, detail=str(e),
                            frames=sequence or 0, dropped_before_keyframe=dropped if sequence is None else 0)
                return "disconnected"
            if packet is None:
                self._event(logging.INFO, "eof", stream_epoch=epoch, frames=sequence or 0)
                return "eof"
            received_at_ms = self._clock.now_ms()
            if sequence is None:
                codec = build_codec_info(cid, info, epoch, packet.data) if packet.keyframe else None
                if codec is None:
                    dropped += 1
                    continue
                self.sink.send_codec_info(I0CodecInfo(codec[0]))
                self.stats.on_stream_started(codec[0], dropped)
                self._event(logging.INFO, "stream_started", stream_epoch=epoch, dropped_before_keyframe=dropped)
                sequence = 0
            t = estimator.estimate(received_at_ms, packet.pts)
            header = build_frame_header(cid, epoch, sequence, packet, timebase, model, t, received_at_ms)
            self.stats.on_frame(header)
            self.sink.send_frame(I0Frame(header, packet.data))
            sequence += 1


class RelaySession:
    """카메라 여러 대를 동시에 중계한다. 카메라마다 독립 스레드와 독립 CameraSink(연결)."""

    def __init__(self, cameras: Sequence[CameraSpec], sink_factory: SinkFactory, clock: Clock,
                 reconnect_delays_s: Sequence[float] = DEFAULT_RECONNECT_DELAYS_S) -> None:
        ids = [c.camera_id for c in cameras]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("cameras 는 비어 있지 않고 camera_id 가 겹치지 않아야 한다")
        self._clock = clock
        self.stop_event = threading.Event()
        self._cameras = cameras
        self._sink_factory = sink_factory
        self._delays = reconnect_delays_s
        self.relays: list[CameraRelay] = []
        self._threads: list[threading.Thread] = []

    def start(self, duration_s: float | None = None) -> None:
        if self._threads:
            raise RuntimeError("이미 시작했다")
        deadline = self._clock.monotonic() + duration_s if duration_s is not None else None
        self.relays = [CameraRelay(c, self._sink_factory(c.camera_id), self._clock, self.stop_event, deadline,
                                   self._delays) for c in self._cameras]
        self._threads = [threading.Thread(target=r.run, name=f"relay-{r.camera_id}", daemon=True)
                         for r in self.relays]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> bool:
        """스레드가 모두 끝나면 True. 입력 read() 가 막혀 있으면 그 입력의 읽기 시간 제한(RTSP 5초)만큼 걸린다."""
        end = None if timeout is None else self._clock.monotonic() + timeout
        for t in self._threads:
            while t.is_alive():
                left = None if end is None else end - self._clock.monotonic()
                if left is not None and left <= 0:
                    return False
                t.join(0.2 if left is None else min(0.2, left))
        return True

    @property
    def alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def heartbeat_cameras(self) -> list[dict[str, Any]]:
        """laptop_heartbeat payload.cameras (카메라별 RTSP 상태·epoch·마지막 순번·도착 간격 최대)."""
        return [r.stats.heartbeat_entry() for r in self.relays]

    def snapshots(self) -> list[dict[str, Any]]:
        return [r.stats.snapshot() for r in self.relays]

    def errors(self) -> dict[str, str]:
        return {r.camera_id: type(r.error).__name__ for r in self.relays if r.error is not None}
