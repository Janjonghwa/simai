"""H.264 비트스트림 다루기: Annex B NAL 나누기, SPS/PPS 꺼내기 (명세 12.7 코덱 정보).

녹화 도구는 디코딩하지 않는다. 코덱 정보(SPS/PPS)를 I0 codec_info 형식으로 남기고, 세그먼트 파일이 혼자서도
디코딩되도록 첫 키프레임에 SPS/PPS 가 없으면 앞에 붙이는 데만 쓴다.
"""

from __future__ import annotations

import base64
import struct

START_CODE = b"\x00\x00\x00\x01"
NAL_IDR = 5
NAL_SPS = 7
NAL_PPS = 8


def _nal_starts(data: bytes) -> list[tuple[int, int]]:
    """(시작 코드 위치, NAL 첫 바이트 위치) 목록. 4바이트 시작 코드는 앞의 0 을 시작 코드에 넣는다."""
    out = []
    i = data.find(b"\x00\x00\x01")
    while i >= 0:
        sc = i - 1 if i > 0 and data[i - 1] == 0 else i
        out.append((sc, i + 3))
        i = data.find(b"\x00\x00\x01", i + 3)
    return out


def split_annexb(data: bytes) -> list[bytes]:
    """Annex B 바이트열 → NAL 단위 목록(시작 코드 제외). 3바이트·4바이트 시작 코드를 모두 받는다."""
    starts = _nal_starts(data)
    ends = [sc for sc, _ in starts[1:]] + [len(data)]
    return [data[nal:end] for (_, nal), end in zip(starts, ends, strict=True) if end > nal]


def first_vcl_type(data: bytes) -> int | None:
    """첫 슬라이스 NAL 의 타입(1~5). 앞쪽 SPS/PPS/SEI 만 건너뛰므로 프레임 전체를 훑지 않는다."""
    i = data.find(b"\x00\x00\x01")
    while 0 <= i < len(data) - 3:
        t = data[i + 3] & 0x1F
        if 1 <= t <= 5:
            return t
        i = data.find(b"\x00\x00\x01", i + 3)
    return None


def is_annexb(data: bytes) -> bool:
    return data.startswith(b"\x00\x00\x01") or data.startswith(START_CODE)


def nal_type(nal: bytes) -> int:
    return nal[0] & 0x1F if nal else -1


def parameter_sets_from_annexb(data: bytes) -> tuple[bytes | None, bytes | None]:
    """Annex B 바이트열에서 첫 SPS·PPS 를 꺼낸다. 없으면 None."""
    sps = pps = None
    for nal in split_annexb(data):
        t = nal_type(nal)
        if t == NAL_SPS and sps is None:
            sps = nal
        elif t == NAL_PPS and pps is None:
            pps = nal
    return sps, pps


def parameter_sets_from_avcc(extradata: bytes) -> tuple[bytes | None, bytes | None]:
    """MP4·MKV 의 avcC extradata 에서 첫 SPS·PPS 를 꺼낸다."""
    if len(extradata) < 7 or extradata[0] != 1:
        return None, None
    i = 5
    n_sps = extradata[i] & 0x1F
    i += 1
    sps = pps = None
    for _ in range(n_sps):
        (ln,) = struct.unpack(">H", extradata[i : i + 2])
        sps = sps or extradata[i + 2 : i + 2 + ln]
        i += 2 + ln
    n_pps = extradata[i]
    i += 1
    for _ in range(n_pps):
        (ln,) = struct.unpack(">H", extradata[i : i + 2])
        pps = pps or extradata[i + 2 : i + 2 + ln]
        i += 2 + ln
    return sps, pps


def parameter_sets(keyframe: bytes, extradata: bytes | None) -> tuple[bytes | None, bytes | None]:
    """키프레임 안의 SPS/PPS 를 먼저 쓰고, 없으면 스트림 extradata(Annex B 또는 avcC)에서 찾는다."""
    sps, pps = parameter_sets_from_annexb(keyframe)
    if (sps is None or pps is None) and extradata:
        e_sps, e_pps = (
            parameter_sets_from_annexb(extradata) if is_annexb(extradata) else parameter_sets_from_avcc(extradata)
        )
        sps, pps = sps or e_sps, pps or e_pps
    return sps, pps


def has_parameter_sets(data: bytes) -> bool:
    types = {nal_type(n) for n in split_annexb(data)}
    return NAL_SPS in types and NAL_PPS in types


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
