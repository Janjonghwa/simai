"""카메라별 압축 패킷 링 버퍼 (명세 5.1 이벤트 클립 링 버퍼, 11.5 원본 영상 비저장).

Tier 0 는 노트북이 보낸 I0 압축 프레임(H.264 접근 단위, 12.7)을 디코딩 경로와 별도로 이 버퍼에도 넣는다.
버퍼는 **메모리에만** 있고 디스크·외부로 쓰는 코드가 없다. 덮어써진(밀려난) 영상은 사라진다.

- 단위는 GOP(키프레임 하나 + 뒤따르는 프레임)다. 버퍼의 맨 앞은 늘 키프레임이라, 잘라낸 클립은 키프레임부터
  시작해 디코딩 없이 그대로 재다중화할 수 있다.
- 보관 구간: 가장 최근 촬영 시각에서 window_ms(기본 60초)를 덮는 가장 짧은 GOP 묶음. 다음 GOP 의 키프레임이 이미
  창 시작보다 앞이면 맨 앞 GOP 를 버린다. 그래서 창 시작보다 앞선 영상은 최대 GOP 하나만큼만 남는다.
- 메모리 상한: 카메라별 바이트 상한(max_bytes_per_camera)을 넘으면 시간과 상관없이 맨 앞 GOP 부터 버린다.
- 새 stream_epoch 는 코덱 정보 → 키프레임 순서로 시작한다(12.7). 코덱 정보가 없거나 키프레임 전 프레임, 끝난 epoch,
  늦게 온 프레임은 버린다. frame_sequence 빈칸(손실) 뒤에는 참조가 끊겼으므로 다음 키프레임까지 버린다.
- 시각은 헤더의 촬영 시각(estimated_capture_time_ms, UTC ms)이다. 버퍼는 현재 시각을 직접 읽지 않는다
  (expire 는 호출자가 주입 시계로 읽은 시각을 넘긴다).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from simai_ai.io.messages import I0CodecInfo, I0Frame, I0Message


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name}: 양의 정수여야 한다 (받은 값 {value!r})")
    return value


@dataclass(frozen=True)
class RingBufferConfig:
    """링 버퍼 설정. window_ms 는 명세 5.1 의 '최근 60초', 바이트 상한은 C110 약 1.1 Mbps × 60초의 여유분."""

    window_ms: int = 60_000
    max_bytes_per_camera: int = 24 * 1024 * 1024

    def __post_init__(self) -> None:
        _positive_int("window_ms", self.window_ms)
        _positive_int("max_bytes_per_camera", self.max_bytes_per_camera)


@dataclass(frozen=True)
class BufferedPacket:
    """버퍼에 든 압축 프레임 하나. payload 는 받은 그대로(디코딩·재인코딩하지 않음)."""

    stream_epoch: int
    frame_sequence: int
    capture_time_ms: int
    keyframe: bool
    payload: bytes = field(repr=False)
    timestamp_source: str | None = None
    timestamp_uncertainty_ms: float | None = None

    @property
    def size(self) -> int:
        return len(self.payload)


@dataclass(frozen=True)
class ClipSegment:
    """같은 코덱 정보로 이어지는 패킷 묶음. 첫 패킷은 키프레임이다."""

    codec: I0CodecInfo
    packets: tuple[BufferedPacket, ...]

    def __post_init__(self) -> None:
        if not self.packets or not self.packets[0].keyframe:
            raise ValueError("ClipSegment 는 키프레임으로 시작해야 한다")


@dataclass(frozen=True)
class CameraCut:
    """한 카메라에서 잘라낸 구간. segments 가 비어 있으면 그 구간 영상이 버퍼에 없다.

    truncated_start: 요청 시작 이전(또는 같은 시각)의 키프레임이 버퍼에 없어 요청보다 늦게 시작한다.
    truncated_end: 자를 때 버퍼의 최신 촬영 시각이 요청 끝에 못 미쳤다(카메라 정지·끊김).
    """

    camera_id: str
    requested_start_ms: int
    requested_end_ms: int
    segments: tuple[ClipSegment, ...]
    truncated_start: bool
    truncated_end: bool

    @property
    def packets(self) -> tuple[BufferedPacket, ...]:
        return tuple(p for s in self.segments for p in s.packets)

    @property
    def empty(self) -> bool:
        return not self.segments

    @property
    def start_capture_ms(self) -> int | None:
        return self.segments[0].packets[0].capture_time_ms if self.segments else None

    @property
    def end_capture_ms(self) -> int | None:
        return self.segments[-1].packets[-1].capture_time_ms if self.segments else None

    @property
    def frame_count(self) -> int:
        return sum(len(s.packets) for s in self.segments)

    @property
    def has_gaps(self) -> bool:
        """구간 안에 빠진 프레임(frame_sequence 빈칸, 손실 뒤 키프레임까지 버린 프레임)이 있는지."""
        for seg in self.segments:
            for a, b in zip(seg.packets, seg.packets[1:]):
                if b.frame_sequence != a.frame_sequence + 1:
                    return True
        return False

    @property
    def stream_epochs(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(s.codec.stream_epoch for s in self.segments))


@dataclass
class _Gop:
    codec: I0CodecInfo
    packets: list[BufferedPacket]
    size: int = 0

    @property
    def start_ms(self) -> int:
        return self.packets[0].capture_time_ms

    @property
    def end_ms(self) -> int:
        return self.packets[-1].capture_time_ms


@dataclass
class CameraRingStats:
    """버린 이유별 개수와 현재 보관량(지표·시험용)."""

    accepted: int = 0
    dropped_no_codec: int = 0
    dropped_waiting_keyframe: int = 0
    dropped_stale_epoch: int = 0
    dropped_late: int = 0
    evicted_by_window: int = 0  # 창을 벗어나 버린 GOP 수
    evicted_by_memory: int = 0  # 바이트 상한으로 버린 GOP 수
    sequence_gaps: int = 0


class CameraRing:
    """카메라 한 대의 링 버퍼. 수신 스레드(push)와 클립 작업(cut)이 동시에 불러도 된다."""

    def __init__(self, camera_id: str, config: RingBufferConfig) -> None:
        self.camera_id = camera_id
        self._config = config
        self._lock = threading.Lock()
        self._gops: deque[_Gop] = deque()
        self._bytes = 0
        self._epoch: int | None = None
        self._codec: I0CodecInfo | None = None
        self._last_sequence: int | None = None
        self._open: _Gop | None = None  # 지금 뒤에 이어 붙일 수 있는 GOP (빈칸·epoch 변경 뒤에는 None)
        self._newest_ms: int | None = None
        self.stats = CameraRingStats()

    # ------------------------------------------------------------------ 입력

    def on_codec_info(self, info: I0CodecInfo) -> None:
        if info.camera_id != self.camera_id:
            raise ValueError(f"camera_id 가 다르다: {info.camera_id!r} != {self.camera_id!r}")
        with self._lock:
            if self._epoch is not None and info.stream_epoch < self._epoch:
                return  # 끝난 epoch 의 코덱 정보
            if info == self._codec:
                return  # 같은 코덱 정보가 다시 왔다: 열린 GOP 를 끊지 않는다
            if info.stream_epoch != self._epoch:
                self._last_sequence = None  # frame_sequence 는 epoch 마다 새로 센다
            self._epoch = info.stream_epoch
            self._codec = info
            self._open = None  # 새 epoch·코덱 변경: 다음 키프레임부터 새 GOP

    def on_frame(self, frame: I0Frame) -> bool:
        """프레임 하나를 넣는다. 버퍼에 들어갔으면 True."""
        h = frame.header
        if h.camera_id != self.camera_id:
            raise ValueError(f"camera_id 가 다르다: {h.camera_id!r} != {self.camera_id!r}")
        with self._lock:
            if self._epoch is None or h.stream_epoch > self._epoch or self._codec is None:
                self.stats.dropped_no_codec += 1  # 새 epoch 는 코덱 정보가 먼저 와야 한다
                return False
            if h.stream_epoch < self._epoch:
                self.stats.dropped_stale_epoch += 1
                return False
            if self._last_sequence is not None and h.frame_sequence <= self._last_sequence:
                self.stats.dropped_late += 1
                return False
            if self._last_sequence is not None and h.frame_sequence != self._last_sequence + 1:
                self.stats.sequence_gaps += 1
                self._open = None  # 참조가 끊겼다: 다음 키프레임까지 버린다
            self._last_sequence = h.frame_sequence
            packet = BufferedPacket(h.stream_epoch, h.frame_sequence, h.estimated_capture_time_ms, h.keyframe,
                                    bytes(frame.payload), h.timestamp_source, h.timestamp_uncertainty_ms)
            if h.keyframe:
                self._open = _Gop(self._codec, [])
                self._gops.append(self._open)
            elif self._open is None:
                self.stats.dropped_waiting_keyframe += 1
                return False
            self._open.packets.append(packet)
            self._open.size += packet.size
            self._bytes += packet.size
            if self._newest_ms is None or packet.capture_time_ms > self._newest_ms:
                self._newest_ms = packet.capture_time_ms
            self.stats.accepted += 1
            self._evict_locked(self._newest_ms - self._config.window_ms)
            return True

    # ------------------------------------------------------------------ 버리기

    def _pop_front_locked(self) -> None:
        gop = self._gops.popleft()
        self._bytes -= gop.size
        if gop is self._open:
            self._open = None

    def _evict_locked(self, cutoff_ms: int) -> None:
        # 창: 다음 GOP 가 창 시작을 덮으면 맨 앞 GOP 는 필요 없다. 통째로 창 밖인 GOP 도 버린다.
        while self._gops and (
            self._gops[0].end_ms < cutoff_ms or (len(self._gops) >= 2 and self._gops[1].start_ms <= cutoff_ms)
        ):
            self._pop_front_locked()
            self.stats.evicted_by_window += 1
        # 메모리 상한: 시간과 상관없이 맨 앞부터 버린다(열린 GOP 하나가 상한을 넘으면 그것도 버리고 다음 키프레임을 기다린다)
        while self._gops and self._bytes > self._config.max_bytes_per_camera:
            self._pop_front_locked()
            self.stats.evicted_by_memory += 1

    def expire(self, now_ms: int) -> None:
        """영상이 멈춰도 창 밖 영상을 남기지 않는다: 마지막 프레임이 now_ms - window_ms 보다 앞인 GOP 를 버린다."""
        with self._lock:
            cutoff = now_ms - self._config.window_ms
            while self._gops and self._gops[0].end_ms < cutoff:
                self._pop_front_locked()
                self.stats.evicted_by_window += 1

    def clear(self) -> None:
        with self._lock:
            self._gops.clear()
            self._bytes = 0
            self._open = None

    # ------------------------------------------------------------------ 읽기

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def newest_capture_ms(self) -> int | None:
        with self._lock:
            return self._newest_ms if self._gops else None

    @property
    def oldest_capture_ms(self) -> int | None:
        with self._lock:
            return self._gops[0].start_ms if self._gops else None

    def packets(self) -> tuple[BufferedPacket, ...]:
        with self._lock:
            return tuple(p for g in self._gops for p in g.packets)

    def cut(self, start_ms: int, end_ms: int) -> CameraCut:
        """[start_ms, end_ms] 촬영 구간을 덮는 패킷을 키프레임부터 잘라 돌려준다(복사본, 버퍼는 그대로).

        시작은 start_ms 이전(같은 시각 포함)의 마지막 키프레임이다. 그런 키프레임이 없으면 버퍼의 첫 키프레임부터다.
        끝은 촬영 시각이 end_ms 이하인 마지막 패킷이다.
        """
        if end_ms < start_ms:
            raise ValueError(f"end_ms({end_ms}) < start_ms({start_ms})")
        with self._lock:
            gops = list(self._gops)
            newest = self._newest_ms
            first = 0
            for i, g in enumerate(gops):
                if g.start_ms <= start_ms:
                    first = i
                else:
                    break
            truncated_start = not gops or gops[first].start_ms > start_ms
            segments: list[ClipSegment] = []
            for g in gops[first:]:
                if g.start_ms > end_ms:
                    break
                chosen = tuple(p for p in g.packets if p.capture_time_ms <= end_ms)
                if segments and segments[-1].codec is g.codec:
                    segments[-1] = ClipSegment(g.codec, segments[-1].packets + chosen)
                else:
                    segments.append(ClipSegment(g.codec, chosen))
            truncated_end = newest is None or newest < end_ms
        return CameraCut(self.camera_id, start_ms, end_ms, tuple(segments), truncated_start, truncated_end)


class PacketRingBuffer:
    """카메라 여러 대의 링 버퍼. I0Ingest 와 같은 메시지(I0Frame·I0CodecInfo)를 받는다."""

    def __init__(self, camera_ids: Sequence[str], config: RingBufferConfig = RingBufferConfig()) -> None:
        if not camera_ids:
            raise ValueError("camera_ids 가 비었다")
        self.config = config
        self.camera_ids = tuple(camera_ids)
        self._rings = {c: CameraRing(c, config) for c in self.camera_ids}

    def camera(self, camera_id: str) -> CameraRing:
        try:
            return self._rings[camera_id]
        except KeyError:
            raise ValueError(f"모르는 camera_id: {camera_id!r}") from None

    def receive(self, message: I0Message) -> None:
        if isinstance(message, I0Frame):
            self.camera(message.header.camera_id).on_frame(message)
        elif isinstance(message, I0CodecInfo):
            self.camera(message.camera_id).on_codec_info(message)
        else:
            raise TypeError(f"I0 메시지가 아니다: {type(message).__name__}")

    def receive_many(self, messages: Iterable[I0Message]) -> None:
        for m in messages:
            self.receive(m)

    def expire(self, now_ms: int) -> None:
        for ring in self._rings.values():
            ring.expire(now_ms)

    def total_bytes(self) -> int:
        return sum(r.size_bytes for r in self._rings.values())
