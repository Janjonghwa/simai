"""조각을 재인코딩 없이 mp4 로 포장한다 (S15P31A101-668).

ffmpeg 의 `-c copy` 와 같다: 녹화 세그먼트(.h264 Annex B)의 압축 프레임을 받은 그대로 mp4 먹서에 넣는다(먹서가 Annex B → avcC 로 바꾼다).
프레임 시각은 고정 fps 가 아니라 사이드카의 촬영 시각에서 온다: PTS·DTS(ms) = 촬영 시각 − 조각 시작, 길이 = 다음 프레임과의 차(마지막은 조각 끝까지).
그래서 프레임이 빠져도 뒤 프레임이 밀리지 않는다. C110·녹화는 B 프레임이 없어 디코딩 순서 = 표시 순서라 DTS = PTS 다.
moov 를 앞에 두어(+faststart) 서명 주소를 <video> 가 바로 재생한다.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import av

from simai_recorder.upload.plan import Chunk

MS = Fraction(1, 1000)


def mux_chunk(chunk: Chunk, out_path: Path) -> None:
    """chunk 를 out_path(mp4)로 쓴다. 템플릿 스트림(SPS/PPS)은 같은 세그먼트 파일 앞부분을 읽어 얻는다."""
    pts = chunk.pts_ms()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(chunk.paths.h264), format="h264") as source, chunk.paths.h264.open("rb") as h264:
        template = source.streams.video[0]
        with av.open(str(out_path), "w", format="mp4", options={"movflags": "+faststart"}) as out:
            stream = out.add_stream_from_template(template)
            stream.time_base = MS
            for k, row in enumerate(chunk.frames):
                h264.seek(row.offset)
                payload = h264.read(row.size)
                if len(payload) != row.size:
                    raise ValueError(f"{chunk.paths.h264}: offset {row.offset} 에서 {row.size} 바이트를 읽지 못했다")
                packet = av.Packet(payload)
                packet.stream = stream
                packet.time_base = MS
                packet.pts = packet.dts = pts[k]
                end = pts[k + 1] if k + 1 < len(pts) else chunk.end_ms - chunk.start_ms
                packet.duration = max(end - pts[k], 1)
                packet.is_keyframe = row.keyframe
                out.mux(packet)


def sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
