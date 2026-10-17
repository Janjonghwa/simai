"""시험용 H.264 영상(사람 이미지 없음)을 그때그때 만들고 디코딩해 센다."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import av


def make_video(path: Path, frames: int = 60, gop: int = 10, fps: int = 25, size: tuple[int, int] = (160, 96)) -> Path:
    """막대 무늬 H.264 영상. 확장자가 .h264 면 Annex B raw, .mp4 면 avcC 컨테이너."""
    fmt = "h264" if path.suffix == ".h264" else None
    with av.open(str(path), "w", format=fmt) as out:
        stream = out.add_stream("libx264", rate=fps)
        stream.width, stream.height = size
        stream.pix_fmt = "yuv420p"
        stream.options = {"g": str(gop), "keyint_min": str(gop), "bf": "0", "preset": "ultrafast", "sc_threshold": "0"}
        w, h = size
        for i in range(frames):
            frame = av.VideoFrame(w, h, "yuv420p")
            row = bytearray([30]) * frame.planes[0].line_size
            x = (i * 3) % w
            row[x : x + 8] = bytes([200]) * len(row[x : x + 8])
            frame.planes[0].update(bytes(row) * h)
            for plane in frame.planes[1:]:
                plane.update(bytes([128]) * plane.buffer_size)
            frame.pts = i
            frame.time_base = Fraction(1, fps)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    return path



def count_decoded(path: Path) -> int:
    with av.open(str(path), format="h264") as c:
        return sum(1 for _ in c.decode(video=0))
