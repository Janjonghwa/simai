"""PyAV(FFmpeg) H.264 디코더: 기본 CPU, 선택 NVDEC(hwaccel cuda) (명세 5.1 디코딩과 추론 분리, 12.7).

decoder.Decoder 구현 하나로 두 경우를 다 맡는다. 다른 점은 hwaccel 하나뿐이다.

- `PyAvDecoder()`: FFmpeg 소프트웨어 디코더(CPU). Tier 0 기본(LIVE·파일·노트북 E2E·시험).
- `NvdecDecoder()` = `PyAvDecoder(hwaccel="cuda")`: FFmpeg 의 CUDA hwaccel(NVDEC). 소프트웨어로 몰래 떨어지지 않게
  allow_software_fallback=False 로 연다. 만들 때 장치를 한 번 열어 보고, 못 열면 DecoderUnavailable 이다
  (Tier 0 는 그때 시작하지 않는다. 조용히 CPU 로 바꾸지 않는다).

입력은 I0 압축 프레임(H.264 접근 단위, Annex B, 12.7)이다. SPS·PPS 는 codec_info 로 따로 오고(시작 코드 없이) 카메라가
키프레임마다 다시 싣는다는 보장이 없어서, 키프레임 앞에 시작 코드와 함께 붙여 넣는다(다시 넣어도 디코더는 같은 값으로 본다).

**열쇠 묶기(G3).** 디코더는 지연·재정렬 때문에 입력 하나가 바로 출력 하나로 나오지 않는다. 입력마다 패킷 pts 에 일련번호를
넣고 번호 → 열쇠(stream_epoch, frame_sequence) 표를 둔다. FFmpeg 는 출력 그림에 그 그림을 만든 패킷의 pts 를 돌려준다.
출력 pts 가 없거나 표에 없으면 그 그림은 버리고 센다(unmatched). 어느 프레임인지 추측해 시각을 붙이지 않는다.

**실패.** FFmpeg 오류는 모두 DecodeError 다. CameraIngest 가 그 카메라를 다시 초기화하고 다음 키프레임까지 기다린다.
configure 는 예외를 내지 않는다: 디코더를 열지 못하면 그 사실을 들고 있다가 decode 마다 DecodeError 를 낸다
(CameraIngest 의 코덱 상태가 반쯤 바뀐 채로 남지 않게).

**깨진 그림.** FFmpeg 는 비트스트림 오류가 있어도 빈 곳을 메워(error concealment) 그림을 낸다. 메운 그림의 관절은 관측이
아니므로 쓰지 않는다: err_detect=explode 로 오류를 DecodeError 로 올리고(그 카메라는 다음 키프레임까지 기다린다), 그래도
손상 표시(is_corrupt)가 붙어 나온 그림은 버리고 센다(corrupt). 깨진 그림 출력 플래그(output_corrupt)는 켜지 않는다.

출력 그림은 numpy BGR 배열(H×W×3 uint8, ultralytics·OpenCV 입력 형식)이다. NVDEC 그림도 시스템 메모리로 내려 같은
형식으로 낸다(장치에 둔 채 넘기는 것은 처리 시간 실측 뒤 판단, -183).

**색 변환(-212).** BGR 변환은 configure 마다 하나 만든 swscale 문맥(VideoReformatter)으로 한다. `frame.to_ndarray(format=...)` 는
그림마다 새 문맥을 만들어 swscale 초기화·스레드 묶음 생성을 매 그림 치르고, NVDEC 출력(nv12)은 그 비용이 커서 그림 한 장이
CPU 디코딩보다 느렸다(docs/design/tier0-nvdec-throughput.md). 변환 스레드는 convert_threads 개로 묶는다(0 은 보이는 CPU 전부라
Pod 224 코어에서 카메라마다 수백 스레드가 된다).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

from simai_ai.io.decoder import DecodedPicture, DecodeError, Decoder
from simai_ai.io.messages import FrameKey, I0CodecInfo

log = logging.getLogger("simai_ai.io.pyav_decoder")

ANNEXB_START = b"\x00\x00\x00\x01"
DEFAULT_MAX_PENDING = 64  # 그림 없이 디코더 안에 남을 수 있는 입력 번호 수(넘으면 오래된 번호를 잊는다)
DEFAULT_CONVERT_THREADS = 4  # 카메라 하나의 색 변환 스레드(노트북 실측 4 ≈ 자동, 근거는 설계 문서). Pod 값은 c3 변형으로 다시 잰다


class DecoderUnavailable(RuntimeError):
    """디코더를 열 수 없다(PyAV 없음, hwaccel 장치 없음)."""


def _import_av() -> Any:
    try:
        import av
    except ImportError as e:  # pragma: no cover - dev·tier0 설치에는 있다
        raise DecoderUnavailable("PyAV(av) 가 없다(extras io)") from e
    return av


class PyAvDecoder(Decoder):
    """카메라 한 대의 PyAV H.264 디코더. hwaccel 이 None 이면 CPU, "cuda" 면 NVDEC."""

    def __init__(
        self,
        *,
        hwaccel: str | None = None,
        device: str | None = None,
        output_format: str = "bgr24",
        max_pending: int = DEFAULT_MAX_PENDING,
        convert_threads: int = DEFAULT_CONVERT_THREADS,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending 은 1 이상이다")
        if convert_threads < 0:
            raise ValueError("convert_threads 는 0(자동) 이상이다")
        self._av = _import_av()
        self.hwaccel = hwaccel
        self.device = device
        self.output_format = output_format
        self.max_pending = max_pending
        self.convert_threads = convert_threads
        self._ctx: Any = None
        self._reformatter: Any = None
        self._codec: I0CodecInfo | None = None
        self._open_error: str | None = None
        self._prefix = b""
        self._next_pts = 0
        self._pending: OrderedDict[int, FrameKey] = OrderedDict()
        self.unmatched = 0  # 열쇠를 찾지 못해 버린 그림
        self.corrupt = 0  # 손상 표시가 붙어 버린 그림
        self.forgotten = 0  # max_pending 을 넘겨 잊은 입력 번호
        if hwaccel is not None:
            available = self._hw_types()
            if hwaccel not in available:
                raise DecoderUnavailable(f"FFmpeg 에 hwaccel {hwaccel!r} 가 없다(있는 것: {sorted(available)})")
            try:
                self._new_context()  # 장치를 한 번 열어 본다(GPU·드라이버가 없으면 여기서 실패)
            except DecoderUnavailable:
                raise
            except Exception as e:  # noqa: BLE001 - FFmpeg·드라이버 오류 종류가 많다
                raise DecoderUnavailable(f"hwaccel {hwaccel!r} 장치를 열지 못했다: {type(e).__name__}: {e}") from e

    def _hw_types(self) -> set[str]:
        try:
            from av.codec.hwaccel import hwdevices_available
        except ImportError as e:  # pragma: no cover - PyAV 14 미만
            raise DecoderUnavailable("이 PyAV 에는 hwaccel 이 없다(av>=14)") from e
        return set(hwdevices_available())

    def _new_context(self) -> Any:
        av = self._av
        kwargs: dict[str, Any] = {}
        if self.hwaccel is not None:
            from av.codec.hwaccel import HWAccel

            kwargs["hwaccel"] = HWAccel(device_type=self.hwaccel, device=self.device, allow_software_fallback=False)
        ctx = av.CodecContext.create("h264", "r", **kwargs)
        ctx.thread_type = "SLICE"  # 프레임 단위 스레드는 출력이 스레드 수만큼 늦는다(지연 우선)
        ctx.flags = int(av.codec.context.Flags.low_delay)  # 깨진 그림 출력(output_corrupt)은 켜지 않는다
        ctx.options = {"err_detect": "explode"}  # 비트스트림 오류를 메우지 않고 오류로(모듈 설명)
        if self.hwaccel is not None and not getattr(ctx, "is_hwaccel", True):
            raise DecoderUnavailable(f"hwaccel {self.hwaccel!r} 로 열리지 않았다")
        return ctx

    @property
    def configured(self) -> I0CodecInfo | None:
        return self._codec

    def configure(self, codec: I0CodecInfo) -> None:
        self.reset()
        self._codec = codec
        if codec.codec.upper() != "H264":
            self._open_error = f"H.264 만 디코딩한다(받은 코덱 {codec.codec!r})"
            log.error("%s: %s", codec.camera_id, self._open_error)
            return
        self._prefix = b"".join(ANNEXB_START + nal for nal in (codec.sps, codec.pps) if nal)
        try:
            self._reformatter = self._av.video.reformatter.VideoReformatter()
            self._ctx = self._new_context()
        except Exception as e:  # noqa: BLE001 - decode 가 DecodeError 로 알린다(모듈 설명)
            self._open_error = f"디코더를 열지 못했다: {type(e).__name__}: {e}"
            log.error("%s: %s", codec.camera_id, self._open_error)

    def decode(self, payload: bytes, key: FrameKey, keyframe: bool) -> list[DecodedPicture]:
        if self._codec is None:
            raise DecodeError("configure 전에 decode 를 불렀다")
        if self._ctx is None:
            raise DecodeError(self._open_error or "디코더가 열려 있지 않다")
        data = self._prefix + payload if keyframe else payload
        pts = self._next_pts
        self._next_pts += 1
        self._pending[pts] = key
        while len(self._pending) > self.max_pending:
            self._pending.popitem(last=False)
            self.forgotten += 1
        try:
            packet = self._av.Packet(data)
            packet.pts = pts
            frames = self._ctx.decode(packet)
            return self._pictures(frames)
        except DecodeError:
            raise
        except Exception as e:  # noqa: BLE001 - av.FFmpegError 와 변환 오류를 모두 디코딩 실패로
            self._pending.pop(pts, None)
            raise DecodeError(f"{key}: {type(e).__name__}") from e

    def _pictures(self, frames: Any) -> list[DecodedPicture]:
        out: list[DecodedPicture] = []
        for frame in frames:
            key = None if frame.pts is None else self._pending.pop(frame.pts, None)
            if key is None:
                self.unmatched += 1  # 어느 입력의 그림인지 모른다: 시각을 붙이지 않고 버린다
                continue
            if frame.is_corrupt:
                self.corrupt += 1  # 메운 그림은 관측으로 쓰지 않는다
                continue
            image = self._reformatter.reformat(frame, format=self.output_format, threads=self.convert_threads)
            out.append(DecodedPicture(key, image.to_ndarray()))
        return out

    def flush(self) -> list[DecodedPicture]:
        if self._ctx is None:
            return []
        try:
            out = self._pictures(self._ctx.decode(None))
        except Exception as e:  # noqa: BLE001
            raise DecodeError(f"flush: {type(e).__name__}") from e
        finally:
            # 입력 끝을 알린 디코더는 다시 쓸 수 없다. 같은 코덱으로 새로 연다(다음 입력은 키프레임부터)
            self._pending.clear()
            codec = self._codec
            self._ctx = None
            if codec is not None:
                self.configure(codec)
        return out

    def reset(self) -> None:
        self._ctx = None
        self._reformatter = None
        self._codec = None
        self._open_error = None
        self._prefix = b""
        self._pending.clear()


class NvdecDecoder(PyAvDecoder):
    """NVDEC(FFmpeg CUDA hwaccel) 디코더. GPU·드라이버가 없거나 이미지가 video 기능을 열지 않으면 DecoderUnavailable."""

    def __init__(self, *, device: str | None = None, output_format: str = "bgr24",
                 max_pending: int = DEFAULT_MAX_PENDING, convert_threads: int = DEFAULT_CONVERT_THREADS) -> None:
        super().__init__(hwaccel="cuda", device=device, output_format=output_format, max_pending=max_pending,
                         convert_threads=convert_threads)
