"""I0 기록 만들기: codec_info 와 frame_header (명세 12.7, contracts/messages/codec_info·frame_header).

녹화 도구(recorder.py)와 노트북 영상 중계(gateway/relay)가 같은 함수로 만든다. 그래서 녹화 파일의 헤더와
LIVE 로 보내는 헤더가 같은 규칙(epoch·순번·PTS·촬영 시각 계약)을 따른다.
"""

from __future__ import annotations

from typing import Any

from simai_recorder.annexb import b64, has_parameter_sets, parameter_sets
from simai_recorder.capture_time import CaptureTime, CaptureTimeModel
from simai_recorder.sources import SourcePacket, StreamInfo

DEFAULT_PTS_TIMEBASE = "1/90000"


def pts_timebase_string(info: StreamInfo) -> str:
    """frame_header·codec_info 의 pts_timebase. 모르면 RTP 비디오 시계(1/90000)로 적는다(source_pts 는 그때 null)."""
    return f"1/{info.pts_timebase.denominator}" if info.pts_timebase else DEFAULT_PTS_TIMEBASE


def build_codec_info(camera_id: str, info: StreamInfo, epoch: int,
                     keyframe: bytes) -> tuple[dict[str, Any], bool] | None:
    """첫 키프레임(과 스트림 extradata)에서 codec_info 를 만든다. SPS/PPS 가 없으면 None (지어내지 않는다).

    두 번째 값은 키프레임 안에 SPS/PPS 가 없어서 단독 디코딩하려면 앞에 붙여야 하는지 여부다.
    """
    sps, pps = parameter_sets(keyframe, info.extradata)
    if sps is None or pps is None:
        return None
    codec: dict[str, Any] = {"camera_id": camera_id, "stream_epoch": epoch, "codec": info.codec}
    if info.profile:
        codec["profile"] = info.profile
    codec.update(sps=b64(sps), pps=b64(pps), pts_timebase=pts_timebase_string(info),
                 width=info.width, height=info.height)
    if info.nominal_fps:
        codec["nominal_fps"] = info.nominal_fps
    return codec, not has_parameter_sets(keyframe)


def build_frame_header(camera_id: str, epoch: int, frame_sequence: int, packet: SourcePacket, timebase: str,
                       model: CaptureTimeModel, t: CaptureTime, received_at_ms: int) -> dict[str, Any]:
    """I0 frame_header 하나. 원본 수신 시각·PTS 를 함께 남겨 고정 지연이 정해지면 다시 계산할 수 있게 한다(3.9)."""
    return {
        "camera_id": camera_id,
        "stream_epoch": epoch,
        "frame_sequence": frame_sequence,
        "source_pts": packet.pts,
        "pts_timebase": timebase,
        "estimated_capture_time_ms": t.estimated_capture_time_ms,
        "timestamp_source": model.timestamp_source,
        "timestamp_uncertainty_ms": t.timestamp_uncertainty_ms,
        "keyframe": packet.keyframe,
        "received_at_ms": received_at_ms,
    }
