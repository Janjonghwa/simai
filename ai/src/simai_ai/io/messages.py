"""I0 프레임 메시지의 내부 형식 (명세 12.7, 3.9 촬영 시각 계약).

I0 정식 메시지 스키마는 S15P31A101-20 이 만든다. 그 전까지 필드 이름은 명세 12.7 의 프레임 헤더 예시 그대로 두고,
-20 이 머지되면 이 dataclass 를 그 스키마로 검증한다.

- `(camera_id, stream_epoch, frame_sequence)` 가 프레임의 유일 식별자다.
- stream_epoch 는 카메라 스트림이 재연결될 때마다 늘어난다. 새 epoch 는 코덱 정보 → 키프레임 순서로 시작한다.
- 시각은 UTC int64 ms 다. 헤더와 압축 프레임은 같은 메시지에 실려 오므로 둘을 따로 다루지 않는다.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from simai_ai.providers.base import TIMESTAMP_SOURCES
from simai_ai.tracking.time_model import require_ms

# contracts/common/defs.schema.json 의 camera_id 형식
_CAMERA_ID = re.compile(r"[A-Za-z0-9_-]+")
_INT64_MAX = 2**63 - 1

# 같은 epoch 안의 프레임을 디코더 출력과 다시 묶는 열쇠 (stream_epoch, frame_sequence)
FrameKey = tuple[int, int]


def _require_count(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}: 0 이상 정수여야 한다 (받은 값 {value!r})")
    return value


def _require_camera_id(camera_id: str) -> str:
    if not isinstance(camera_id, str) or not _CAMERA_ID.fullmatch(camera_id):
        raise ValueError(f"camera_id: 영문·숫자·_·- 로 된 문자열이어야 한다 (받은 값 {camera_id!r})")
    return camera_id


def _require_int64_ms(name: str, value: int) -> int:
    if require_ms(name, value) > _INT64_MAX:
        raise ValueError(f"{name}: int64 범위를 넘는다 (받은 값 {value})")
    return value


@dataclass(frozen=True)
class I0FrameHeader:
    """압축 프레임 앞에 붙는 헤더 (명세 12.7 예시와 같은 필드).

    source_pts·pts_timebase 는 카메라 스트림의 PTS 와 그 단위("1/90000")다. 모르면 None.
    timestamp_uncertainty_ms 를 모르면 None 으로 둔다(0 으로 채우지 않는다).
    """

    camera_id: str
    stream_epoch: int
    frame_sequence: int
    estimated_capture_time_ms: int
    timestamp_source: str
    keyframe: bool
    timestamp_uncertainty_ms: float | None = None
    source_pts: int | None = None
    pts_timebase: str | None = None

    def __post_init__(self) -> None:
        _require_camera_id(self.camera_id)
        _require_count("stream_epoch", self.stream_epoch)
        _require_count("frame_sequence", self.frame_sequence)
        _require_int64_ms("estimated_capture_time_ms", self.estimated_capture_time_ms)
        if self.timestamp_source not in TIMESTAMP_SOURCES:
            raise ValueError(f"timestamp_source {self.timestamp_source!r} 는 {TIMESTAMP_SOURCES} 중 하나여야 한다")
        if not isinstance(self.keyframe, bool):
            raise TypeError(f"keyframe: bool 이어야 한다 (받은 값 {self.keyframe!r})")
        u = self.timestamp_uncertainty_ms
        if u is not None and (isinstance(u, bool) or not isinstance(u, (int, float)) or not 0 <= u < float("inf")):
            raise ValueError(f"timestamp_uncertainty_ms: 0 이상의 유한한 값이어야 한다 (받은 값 {u!r})")

    @property
    def key(self) -> FrameKey:
        return (self.stream_epoch, self.frame_sequence)


@dataclass(frozen=True)
class I0Frame:
    """헤더 + 압축 프레임(H.264 접근 단위 등) 한 건. 노트북은 디코딩하지 않고 그대로 싣는다."""

    header: I0FrameHeader
    payload: bytes


@dataclass(frozen=True)
class I0CodecInfo:
    """재접속 직후 키프레임보다 먼저 오는 코덱 정보 (명세 12.7).

    codec 은 코덱 종류("h264" 등), sps·pps 는 H.264 파라미터 집합이다(다른 코덱이면 비울 수 있다).
    """

    camera_id: str
    stream_epoch: int
    codec: str
    pts_timebase: str | None = None
    sps: bytes = b""
    pps: bytes = b""

    def __post_init__(self) -> None:
        _require_camera_id(self.camera_id)
        _require_count("stream_epoch", self.stream_epoch)
        if not isinstance(self.codec, str) or not self.codec:
            raise ValueError(f"codec: 빈 문자열이 아닌 문자열이어야 한다 (받은 값 {self.codec!r})")


I0Message = I0Frame | I0CodecInfo


def codec_info_from_contract(meta: Mapping[str, Any]) -> I0CodecInfo:
    """codec_info 계약(contracts/messages/codec_info) → I0CodecInfo. SPS·PPS 는 base64 → 바이트(시작 코드 없이).

    I0 영상 수신(services/tier0 live_input)과 녹화 재생(sources.RecordingReplay)이 같이 쓴다.
    """
    try:
        sps = base64.b64decode(meta["sps"], validate=True)
        pps = base64.b64decode(meta["pps"], validate=True)
    except (binascii.Error, ValueError, TypeError) as e:
        raise ValueError("codec_info sps·pps 가 base64 가 아니다") from e
    return I0CodecInfo(camera_id=meta["camera_id"], stream_epoch=meta["stream_epoch"], codec=meta["codec"],
                       pts_timebase=meta.get("pts_timebase"), sps=sps, pps=pps)


def frame_header_from_contract(header: Mapping[str, Any]) -> I0FrameHeader:
    """frame_header 계약 → I0FrameHeader. 촬영 시각·불확실성은 헤더 값 그대로다(채우지 않는다)."""
    return I0FrameHeader(
        camera_id=header["camera_id"],
        stream_epoch=header["stream_epoch"],
        frame_sequence=header["frame_sequence"],
        estimated_capture_time_ms=header["estimated_capture_time_ms"],
        timestamp_source=header["timestamp_source"],
        keyframe=header["keyframe"],
        timestamp_uncertainty_ms=header.get("timestamp_uncertainty_ms"),
        source_pts=header.get("source_pts"),
        pts_timebase=header.get("pts_timebase"),
    )


def frame_from_contract(header: Mapping[str, Any], data: bytes) -> I0Frame:
    """frame_header 계약 + 압축 프레임 → I0Frame."""
    return I0Frame(frame_header_from_contract(header), bytes(data))
