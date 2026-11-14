"""압축 프레임 입력: 실제 RTSP 카메라와, 카메라 없이 시험하는 로컬 파일·합성 스트림 (같은 인터페이스).

모든 입력은 디코딩하지 않은 H.264 접근 단위(Annex B)를 패킷 하나씩 낸다(명세 12.7).

- RtspSource: Tapo C110 RTSP over TCP. 카메라당 세션 하나만 연다(명세 3.3 RTSP 접속 규칙).
- FileSource: 로컬 영상 파일(H.264 raw·MP4·MKV)을 실시간 속도로 흘려 카메라처럼 쓴다.
- SyntheticSource: libx264 로 움직이는 무늬를 만들어 카메라처럼 낸다(사람 이미지 없음).
- FaultySource: 어떤 입력이든 감싸 지연·끊김·재접속 실패를 흉내 낸다.

끊김·접속 실패는 SourceError 로 알린다. read() 가 None 을 돌려주면 입력이 정상으로 끝난 것이다(파일 끝).
"""

from __future__ import annotations

import math
import random
import struct
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from simai_recorder.annexb import NAL_IDR, first_vcl_type
from simai_recorder.clock import Clock

RTP_VIDEO_TIMEBASE = Fraction(1, 90000)


class SourceError(Exception):
    """접속 실패 또는 스트림 끊김. 녹화기는 다시 접속하고 stream_epoch 를 올린다."""


@dataclass(frozen=True)
class StreamInfo:
    """접속 직후 알 수 있는 스트림 정보. 모르는 값은 None (지어내지 않는다)."""

    codec: str
    width: int
    height: int
    pts_timebase: Fraction | None
    nominal_fps: float | None = None
    profile: str | None = None
    extradata: bytes | None = None


@dataclass(frozen=True)
class SourcePacket:
    """H.264 접근 단위 하나 (Annex B). pts 는 pts_timebase 단위, 모르면 None.

    camera_time_us: RTSP 의 RTCP Sender Report 로 환산한 이 프레임의 카메라 시각(UTC µs, FFmpeg PRFT). SR 을 받기 전이나
      SR 이 없는 카메라·파일·합성 입력은 None (지어내지 않는다). C110 은 이 값이 흔들려 촬영 시각으로 쓰지 않는다(명세 3.9).
      gateway/rtsp_probe 가 RTCP 시각 유무와 일관성을 잴 때만 쓴다.
    sender_report: 이 패킷 바로 앞에 새 RTCP Sender Report 가 왔다.
    """

    data: bytes
    pts: int | None
    keyframe: bool
    camera_time_us: int | None = None
    sender_report: bool = False


class PacketSource(ABC):
    kind: str  # manifest 의 cameras[].source.kind: RTSP / FILE / SYNTHETIC

    @abstractmethod
    def open(self) -> StreamInfo:
        """스트림을 (다시) 연다. 실패하면 SourceError."""

    @abstractmethod
    def read(self) -> SourcePacket | None:
        """다음 패킷. 끊기면 SourceError, 정상 끝이면 None."""

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        """manifest 에 남길 입력 설명. 비밀 값(RTSP 계정)은 넣지 않는다."""


# --------------------------------------------------------------------------- PyAV 공통


def _timebase_string(tb: Fraction) -> str:
    return f"1/{tb.denominator}"


def _camera_time_us(packet: Any) -> int | None:
    """FFmpeg 이 RTCP SR 로 환산해 붙인 카메라 시각(AV_PKT_DATA_PRFT 의 wallclock, UTC µs). 없으면 None."""
    if not packet.has_sidedata("prft"):
        return None
    raw = bytes(packet.get_sidedata("prft"))
    return struct.unpack_from("=q", raw)[0] if len(raw) >= 8 else None


class _PyAvDemuxer:
    """PyAV 컨테이너의 첫 비디오 스트림을 디코딩 없이 패킷으로 읽는다. avcC(MP4·MKV)는 Annex B 로 바꾼다."""

    def __init__(self, container: Any) -> None:
        import av.bitstream

        self.container = container
        self.stream = container.streams.video[0]
        ctx = self.stream.codec_context
        if ctx.name != "h264":
            raise SourceError(f"H.264 가 아니다: {ctx.name}")
        extradata = bytes(ctx.extradata) if ctx.extradata else None
        self._bsf = None
        if extradata and extradata[:1] == b"\x01":
            self._bsf = av.bitstream.BitStreamFilterContext("h264_mp4toannexb", self.stream)
        tb = self.stream.time_base
        # pts_timebase 는 "1/N" 형식이다. 분자가 1 이 아니면 RTP 비디오 시계(1/90000)로 바꾼다.
        self._rescale = tb is not None and tb.numerator != 1
        self.pts_timebase: Fraction | None = None if tb is None else (RTP_VIDEO_TIMEBASE if self._rescale else tb)
        self._tb = tb
        rate = self.stream.average_rate or self.stream.guessed_rate
        self.info = StreamInfo(
            codec="H264",
            width=ctx.width,
            height=ctx.height,
            pts_timebase=self.pts_timebase,
            nominal_fps=float(rate) if rate else None,
            profile=ctx.profile,
            extradata=extradata,
        )
        self._packets = self._iterate()

    def _iterate(self) -> Iterator[SourcePacket]:
        for packet in self.container.demux(self.stream):
            if packet.size == 0:
                continue
            for p in self._bsf.filter(packet) if self._bsf else (packet,):
                pts = p.pts
                if pts is not None and self._rescale:
                    pts = round(pts * self._tb / RTP_VIDEO_TIMEBASE)
                data = bytes(p)
                # 패킷 플래그가 빠져도 IDR 슬라이스면 키프레임이다
                yield SourcePacket(data=data, pts=pts, keyframe=bool(p.is_keyframe) or first_vcl_type(data) == NAL_IDR,
                                   camera_time_us=_camera_time_us(p), sender_report=p.has_sidedata("rtcp_sr"))

    def next(self) -> SourcePacket | None:
        return next(self._packets, None)

    def close(self) -> None:
        self.container.close()


# --------------------------------------------------------------------------- RTSP


@dataclass(frozen=True)
class RtspConfig:
    host: str
    path: str = "stream1"
    port: int = 554
    user: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    open_timeout_s: float = 10.0
    read_timeout_s: float = 5.0

    def url(self, with_credentials: bool) -> str:
        auth = ""
        if with_credentials and self.user:
            auth = f"{urllib.parse.quote(self.user, safe='')}:{urllib.parse.quote(self.password, safe='')}@"
        return f"rtsp://{auth}{self.host}:{self.port}/{self.path}"


class RtspSource(PacketSource):
    """Tapo C110 RTSP over TCP. 열린 세션은 하나뿐이며, 다시 열기 전에 반드시 닫는다(동시 접속 최대 2, 명세 3.3)."""

    kind = "RTSP"

    def __init__(self, config: RtspConfig) -> None:
        self._cfg = config
        self._demux: _PyAvDemuxer | None = None

    def open(self) -> StreamInfo:
        import av

        self.close()
        try:
            container = av.open(
                self._cfg.url(with_credentials=True),
                options={"rtsp_transport": "tcp"},
                timeout=(self._cfg.open_timeout_s, self._cfg.read_timeout_s),
            )
            self._demux = _PyAvDemuxer(container)
        except (av.FFmpegError, OSError) as e:
            self.close()
            raise SourceError(f"RTSP 접속 실패 {self._cfg.url(with_credentials=False)}: {type(e).__name__}") from None
        return self._demux.info

    def read(self) -> SourcePacket | None:
        import av

        if self._demux is None:
            raise SourceError("열리지 않았다")
        try:
            packet = self._demux.next()
        except (av.FFmpegError, OSError) as e:
            raise SourceError(f"RTSP 수신 끊김: {type(e).__name__}") from None
        if packet is None:
            raise SourceError("RTSP 스트림이 끝났다")
        return packet

    def close(self) -> None:
        if self._demux is not None:
            try:
                self._demux.close()
            finally:
                self._demux = None

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "url": self._cfg.url(with_credentials=False)}


# --------------------------------------------------------------------------- 로컬 파일


class FileSource(PacketSource):
    """로컬 영상 파일을 카메라처럼 낸다. realtime 이면 PTS(없으면 fps) 간격대로 기다린다.

    끊긴 뒤 다시 열면 처음으로 돌아가지 않고 이어서 낸다. 그래서 재접속 직후 키프레임이 아닌 패킷이 올 수 있고,
    녹화기는 다음 키프레임까지 버린다. loop 이면 파일 끝에서 처음으로 돌아가 PTS 를 이어 붙인다.
    """

    kind = "FILE"

    def __init__(self, path: Path | str, clock: Clock, realtime: bool = True, loop: bool = False,
                 fps: float | None = None) -> None:
        self._path = Path(path)
        # epoch 전체 파일을 지정하던 기존 재생 설정은 회전한 나머지 파일까지 이어 읽는다.
        self._parts = [self._path]
        if self._path.suffix == ".h264" and self._path.stem.startswith("epoch_") and "_part_" not in self._path.stem:
            self._parts.extend(sorted(self._path.parent.glob(f"{self._path.stem}_part_*.h264")))
        self._part_index = 0
        self._clock = clock
        self._realtime = realtime
        self._loop = loop
        self._fps = fps
        self._demux: _PyAvDemuxer | None = None
        self._pts_offset = 0
        self._last_pts: int | None = None
        self._index = 0
        self._paced = 0  # 이번 연결에서 낸 패킷 수 (PTS 가 없을 때 속도 기준)
        self._t0: float | None = None
        self._pts0: int | None = None

    def _open_container(self) -> None:
        import av

        try:
            container = av.open(str(self._parts[self._part_index]), format="h264" if self._path.suffix in (".h264", ".264") else None)
        except (av.FFmpegError, OSError) as e:
            raise SourceError(f"파일을 열 수 없다 {self._path}: {e}") from None
        self._demux = _PyAvDemuxer(container)

    def open(self) -> StreamInfo:
        if self._demux is None:
            self._open_container()
        assert self._demux is not None
        # 다시 열면 속도 기준을 지금으로 옮긴다. 끊긴 동안 밀린 프레임을 한꺼번에 내지 않는다.
        self._t0 = self._pts0 = None
        self._paced = 0
        info = self._demux.info
        if self._fps is not None:
            info = StreamInfo(**{**info.__dict__, "nominal_fps": self._fps})
        return info

    def _frame_step(self) -> int | None:
        if self._demux is None:
            return None
        fps = self._fps or self._demux.info.nominal_fps
        tb = self._demux.pts_timebase
        if not fps or tb is None:
            return None
        return max(1, round(1 / (fps * tb)))

    def read(self) -> SourcePacket | None:
        if self._demux is None:
            raise SourceError("열리지 않았다")
        packet = self._demux.next()
        has_next_part = self._part_index + 1 < len(self._parts)
        if packet is None and (has_next_part or (self._loop and self._index > 0)):
            self._part_index = self._part_index + 1 if has_next_part else 0
            step = self._frame_step() or 0
            if self._last_pts is not None:
                self._pts_offset = self._last_pts + step
            self._demux.close()
            self._open_container()
            packet = self._demux.next()
            if packet is not None and packet.pts is not None and self._last_pts is not None:
                self._pts_offset -= packet.pts
        if packet is None:
            return None
        if packet.pts is not None:
            packet = SourcePacket(packet.data, packet.pts + self._pts_offset, packet.keyframe)
            self._last_pts = packet.pts
        self._pace(packet)
        self._index += 1
        self._paced += 1
        return packet

    def _pace(self, packet: SourcePacket) -> None:
        if not self._realtime:
            return
        now = self._clock.monotonic()
        if self._t0 is None:
            self._t0, self._pts0 = now, packet.pts
            return
        tb = self._demux.pts_timebase if self._demux else None
        if packet.pts is not None and self._pts0 is not None and tb is not None:
            due = self._t0 + float((packet.pts - self._pts0) * tb)
        else:
            due = self._t0 + self._paced / (self._fps or self._demux.info.nominal_fps or 25.0)
        self._clock.sleep(due - now)

    def close(self) -> None:
        # 다시 열 때 이어서 내도록 컨테이너는 끝(release)에서만 닫는다
        pass

    def release(self) -> None:
        if self._demux is not None:
            self._demux.close()
            self._demux = None

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "file_name": self._path.name, "realtime": self._realtime, "loop": self._loop}


# --------------------------------------------------------------------------- 합성 스트림


class SyntheticSource(PacketSource):
    """움직이는 막대 무늬를 libx264 로 인코딩해 카메라처럼 낸다. 사람 이미지는 없다.

    프레임 i 의 실제 촬영 시각은 첫 open 시각 + i/fps 이다(capture_time_ms). 끊긴 동안의 프레임은 건너뛴다
    (카메라는 계속 찍지만 노트북이 받지 못한 것). 다시 열면 인코더를 새로 만들어 키프레임부터 낸다.
    """

    kind = "SYNTHETIC"

    def __init__(self, clock: Clock, width: int = 320, height: int = 180, fps: float = 25.0,
                 keyframe_interval_s: float = 2.0) -> None:
        if width % 2 or height % 2:
            raise ValueError("width·height 는 짝수여야 한다 (yuv420p)")
        self._clock = clock
        self._w, self._h, self._fps = width, height, fps
        self._gop = max(1, round(keyframe_interval_s * fps))
        self._encoder: Any = None
        self._t0_mono: float | None = None
        self._t0_ms: int | None = None
        self._next = 0
        self._pending: list[SourcePacket] = []

    @property
    def pts_timebase(self) -> Fraction:
        return RTP_VIDEO_TIMEBASE

    def _pts(self, index: int) -> int:
        return round(index * 90000 / self._fps)

    def capture_time_ms(self, pts: int) -> float:
        """시험용 정답: pts 프레임의 실제 촬영 시각(UTC ms)."""
        assert self._t0_ms is not None
        return self._t0_ms + pts / 90.0

    def open(self) -> StreamInfo:
        import av

        now = self._clock.monotonic()
        if self._t0_mono is None:
            self._t0_mono, self._t0_ms = now, self._clock.now_ms()
        else:
            self._next = max(self._next, math.ceil((now - self._t0_mono) * self._fps))
        enc = av.CodecContext.create("libx264", "w")
        enc.width, enc.height, enc.pix_fmt = self._w, self._h, "yuv420p"
        enc.time_base = RTP_VIDEO_TIMEBASE
        enc.framerate = Fraction(self._fps).limit_denominator(1000)
        enc.options = {"g": str(self._gop), "bf": "0", "tune": "zerolatency", "preset": "ultrafast"}
        enc.open()
        self._encoder = enc
        self._pending = []
        return StreamInfo(codec="H264", width=self._w, height=self._h, pts_timebase=RTP_VIDEO_TIMEBASE,
                          nominal_fps=self._fps, profile=enc.profile or None)

    def _frame(self, index: int) -> Any:
        import av

        frame = av.VideoFrame(self._w, self._h, "yuv420p")
        y = frame.planes[0]
        row = bytearray([40]) * y.line_size
        x = (index * 4) % self._w
        row[x : x + 16] = bytes([220]) * len(row[x : x + 16])
        y.update(bytes(row) * self._h)
        for plane in frame.planes[1:]:
            plane.update(bytes([128]) * plane.buffer_size)
        frame.pts = self._pts(index)
        frame.time_base = RTP_VIDEO_TIMEBASE
        return frame

    def read(self) -> SourcePacket | None:
        if self._encoder is None:
            raise SourceError("열리지 않았다")
        while not self._pending:
            index = self._next
            self._next += 1
            assert self._t0_mono is not None
            self._clock.sleep(self._t0_mono + index / self._fps - self._clock.monotonic())
            for p in self._encoder.encode(self._frame(index)):
                self._pending.append(SourcePacket(bytes(p), p.pts, bool(p.is_keyframe)))
        return self._pending.pop(0)

    def close(self) -> None:
        self._encoder = None
        self._pending = []

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "width": self._w, "height": self._h, "fps": self._fps}


# --------------------------------------------------------------------------- 장애 흉내


@dataclass(frozen=True)
class FaultPlan:
    """입력 장애 흉내.

    jitter_ms: 패킷마다 0~jitter_ms 균등 지연(seed 고정).
    stalls: 연결마다 n 번째 패킷 앞에서 멈출 ms ({n: ms}).
    disconnect_after: k 번째 연결은 이 수만큼 패킷을 낸 뒤 끊긴다(연결 순서대로, 다 쓰면 더 끊기지 않음).
    failed_reopens: 끊긴 뒤 다시 열 때 실패하는 횟수.
    """

    jitter_ms: float = 0.0
    stalls: Mapping[int, float] = field(default_factory=dict)
    disconnect_after: tuple[int, ...] = ()
    failed_reopens: int = 0
    seed: int = 0

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> FaultPlan:
        return cls(
            jitter_ms=float(d.get("jitter_ms", 0)),
            stalls={int(k): float(v) for k, v in dict(d.get("stalls", {})).items()},
            disconnect_after=tuple(int(x) for x in d.get("disconnect_after", ())),
            failed_reopens=int(d.get("failed_reopens", 0)),
            seed=int(d.get("seed", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"jitter_ms": self.jitter_ms, "stalls": {str(k): v for k, v in self.stalls.items()},
                "disconnect_after": list(self.disconnect_after), "failed_reopens": self.failed_reopens,
                "seed": self.seed}


class FaultySource(PacketSource):
    def __init__(self, inner: PacketSource, plan: FaultPlan, clock: Clock) -> None:
        self._inner = inner
        self._plan = plan
        self._clock = clock
        self._rng = random.Random(plan.seed)
        self._connections = 0
        self._count = 0
        self._failures_left = 0

    @property
    def kind(self) -> str:  # type: ignore[override]
        return self._inner.kind

    @property
    def inner(self) -> PacketSource:
        return self._inner

    def open(self) -> StreamInfo:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise SourceError("다시 열기 실패(흉내)")
        info = self._inner.open()
        self._connections += 1
        self._count = 0
        return info

    def read(self) -> SourcePacket | None:
        k = self._connections - 1
        limit = self._plan.disconnect_after[k] if k < len(self._plan.disconnect_after) else None
        if limit is not None and self._count >= limit:
            self._failures_left = self._plan.failed_reopens
            raise SourceError("끊김(흉내)")
        packet = self._inner.read()
        delay = self._plan.stalls.get(self._count, 0.0)
        if self._plan.jitter_ms:
            delay += self._rng.uniform(0, self._plan.jitter_ms)
        self._clock.sleep(delay / 1000)
        self._count += 1
        return packet

    def close(self) -> None:
        self._inner.close()

    def describe(self) -> dict[str, Any]:
        return {**self._inner.describe(), "faults": self._plan.to_dict()}


def release(source: PacketSource) -> None:
    """녹화가 끝났을 때 입력을 완전히 닫는다."""
    source.close()
    target = source.inner if isinstance(source, FaultySource) else source
    if isinstance(target, FileSource):
        target.release()
