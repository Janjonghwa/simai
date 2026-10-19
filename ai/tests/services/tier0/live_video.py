"""LIVE 입력 시험 도구 (S15P31A101-182): 시험 안에서 만든 실제 H.264 바이트와 I0 송신 상대.

- 영상: 프레임 번호를 화소 칸(4 x 4 = 16비트, 밝음 1·어두움 0)으로 새긴 H.264 를 PyAV(libx264)로 그때그때 만든다.
  사람 이미지는 없고 파일로 쓰지 않는다(커밋하지 않음). 디코딩한 그림에서 번호를 다시 읽어, 그림에 붙은 헤더(촬영 시각·
  frame_sequence)가 그 그림의 것인지 본다(G3 결합 오류 검사).
- SPS·PPS 는 접근 단위에서 빼서 codec_info 로만 보낸다(카메라가 키프레임마다 다시 싣는다는 보장이 없는 경우를 흉내).
  그래서 디코더가 codec_info 의 SPS·PPS 를 쓰지 않으면 디코딩이 안 된다.
- I0 송신 상대(VideoSender): video_ingest 의 바이트 형식 그대로 TLS 로 보낸다(gateway/relay 와 같은 기준 바이트).
"""

from __future__ import annotations

import base64
import re
import socket
import ssl
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.tier0 import video_ingest as vi  # noqa: E402

GRID = 4  # GRID x GRID 칸 = 16비트
SIZE = (256, 144)
_NAL_START = re.compile(re.escape(bytes([0, 0, 1])))
START = b"\x00\x00\x00\x01"


def _yuv(index: int, size: tuple[int, int]) -> np.ndarray:
    w, h = size
    y = np.full((h, w), 16, np.uint8)
    cw, ch = w // GRID, h // GRID
    for bit in range(GRID * GRID):
        if (index >> bit) & 1:
            r, c = divmod(bit, GRID)
            y[r * ch:(r + 1) * ch, c * cw:(c + 1) * cw] = 235
    uv = np.full((h // 2, w), 128, np.uint8)  # yuv420p: U(h/4 x w) + V(h/4 x w) 를 이어 붙인 모양
    return np.vstack([y, uv])


def read_index(image: np.ndarray) -> int:
    """디코딩한 BGR 그림에서 프레임 번호를 읽는다."""
    gray = image.mean(axis=2) if image.ndim == 3 else image
    h, w = gray.shape
    cw, ch = w // GRID, h // GRID
    value = 0
    for bit in range(GRID * GRID):
        r, c = divmod(bit, GRID)
        cell = gray[r * ch + ch // 4:(r + 1) * ch - ch // 4, c * cw + cw // 4:(c + 1) * cw - cw // 4]
        if cell.mean() > 128:
            value |= 1 << bit
    return value


def _nals(au: bytes) -> list[bytes]:
    idx = [m.start() for m in _NAL_START.finditer(au)]
    out = []
    for j, s in enumerate(idx):
        e = idx[j + 1] if j + 1 < len(idx) else len(au)
        n = au[s + 3:e]
        if j + 1 < len(idx) and n.endswith(b"\x00"):
            n = n[:-1]  # 다음 4바이트 시작 코드의 앞 0
        out.append(n)
    return out


@dataclass(frozen=True)
class Encoded:
    sps: bytes
    pps: bytes
    size: tuple[int, int]
    frames: tuple[tuple[bytes, bool], ...]  # (Annex B 접근 단위, 키프레임)


def encode(indices: list[int], *, gop: int = 10, size: tuple[int, int] = SIZE, fps: int = 25) -> Encoded:
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height = size
    enc.pix_fmt = "yuv420p"
    enc.time_base = Fraction(1, fps)
    enc.framerate = Fraction(fps)
    enc.options = {"g": str(gop), "keyint_min": str(gop), "bf": "0", "preset": "ultrafast", "sc_threshold": "0",
                   "crf": "12", "tune": "zerolatency"}
    raw: list[tuple[bytes, bool]] = []
    for n, index in enumerate(indices):
        frame = av.VideoFrame.from_ndarray(_yuv(index, size), format="yuv420p")
        frame.pts = n
        frame.time_base = enc.time_base
        raw += [(bytes(p), bool(p.is_keyframe)) for p in enc.encode(frame)]
    raw += [(bytes(p), bool(p.is_keyframe)) for p in enc.encode(None)]
    assert len(raw) == len(indices)
    first = _nals(raw[0][0])
    sps = next(n for n in first if n[0] & 0x1F == 7)
    pps = next(n for n in first if n[0] & 0x1F == 8)
    frames = tuple((b"".join(START + n for n in _nals(au) if n[0] & 0x1F not in (6, 7, 8)), key) for au, key in raw)
    return Encoded(sps, pps, size, frames)


def codec_meta(camera_id: str, epoch: int, enc: Encoded) -> dict:
    return {"camera_id": camera_id, "stream_epoch": epoch, "codec": "H264", "profile": "High",
            "sps": base64.b64encode(enc.sps).decode(), "pps": base64.b64encode(enc.pps).decode(),
            "pts_timebase": "1/90000", "width": enc.size[0], "height": enc.size[1], "nominal_fps": 25}


def frame_meta(camera_id: str, epoch: int, seq: int, capture_ms: int, keyframe: bool, *,
               source: str = "SIM", uncertainty: float = 4.0) -> dict:
    return {"camera_id": camera_id, "stream_epoch": epoch, "frame_sequence": seq,
            "estimated_capture_time_ms": capture_ms, "timestamp_source": source,
            "timestamp_uncertainty_ms": uncertainty, "source_pts": seq * 3600, "pts_timebase": "1/90000",
            "keyframe": keyframe}


class VideoSender:
    """노트북 중계 한 카메라 연결(시험 상대). 서버 인증서 확인은 이 시험에서 끈다(서버 쪽 시험, gateway/relay 가 본다)."""

    def __init__(self, port: int, camera_id: str, token: str) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.camera_id = camera_id
        self.tls = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5), server_hostname=None)
        self.tls.sendall(vi.encode(vi.HELLO, {"protocol": vi.PROTOCOL, "camera_id": camera_id, "laptop_id": "laptop-01",
                                              "device_token": token}))
        reply = vi.read_record(self.tls, max_data=0)
        assert reply is not None and reply[0] == vi.HELLO_ACK and reply[1] == {"ok": True}, reply

    def codec(self, meta: dict) -> None:
        self.tls.sendall(vi.encode(vi.CODEC_INFO, meta))

    def frame(self, meta: dict, data: bytes) -> None:
        self.tls.sendall(vi.encode(vi.FRAME, meta, data))

    def raw(self, kind: int, meta: dict, data: bytes = b"") -> None:
        self.tls.sendall(vi.encode(kind, meta, data))

    def close(self) -> None:
        try:
            self.tls.close()
        except OSError:
            pass
