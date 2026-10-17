"""입력: FrameSource, I0 수신, 녹화·시뮬레이터 입력 (명세 5.1 입력 추상화·프레임 최신성 정책, 12.7 I0).

- `messages`: I0 프레임 헤더·코덱 정보 내부 형식 (12.7 필드 이름 그대로, 정식 스키마는 S15P31A101-20)
- `decoder`: 디코더 인터페이스, NVDEC 자리, 시험용 FakeDecoder
- `freshness`: 카메라별 latest-frame-wins 대기열, SKIPPED_BY_POLICY/NETWORK_LOSS 기록, frame_age_ms·queue_wait_ms
- `ingest`: I0 수신 → 디코딩 → 대기열 (stream_epoch, 키프레임, frame_sequence 빈칸)
- `sources`: FrameSource 구현 (I0, 시뮬레이터 출력, 녹화 파일. 녹화 manifest 는 계약 스키마로 검증해 읽는다)
"""

from simai_ai.io.decoder import DecodedPicture, DecodeError, Decoder, FakeDecoder, FakeImage, NvdecDecoder
from simai_ai.io.freshness import (
    CameraFreshnessStats,
    FrameDisposition,
    FrameRecord,
    FreshnessConfig,
    InferenceTicket,
    LatestFrameScheduler,
    ResultGate,
    SkipReason,
)
from simai_ai.io.ingest import CameraIngest, I0Ingest
from simai_ai.io.messages import I0CodecInfo, I0Frame, I0FrameHeader, I0Message
from simai_ai.io.sources import (
    I0FrameSource,
    Recording,
    SimObservationFrameSource,
    VideoFileFrameSource,
    load_recording_manifest,
    merge_by_capture_time,
    recording_manifest_errors,
    recording_manifest_rule_errors,
    recordings_from_manifest,
)

__all__ = [
    "CameraFreshnessStats",
    "CameraIngest",
    "DecodeError",
    "DecodedPicture",
    "Decoder",
    "FakeDecoder",
    "FakeImage",
    "FrameDisposition",
    "FrameRecord",
    "FreshnessConfig",
    "I0CodecInfo",
    "I0Frame",
    "I0FrameHeader",
    "I0FrameSource",
    "I0Ingest",
    "I0Message",
    "InferenceTicket",
    "LatestFrameScheduler",
    "NvdecDecoder",
    "Recording",
    "ResultGate",
    "SimObservationFrameSource",
    "SkipReason",
    "VideoFileFrameSource",
    "load_recording_manifest",
    "merge_by_capture_time",
    "recording_manifest_errors",
    "recording_manifest_rule_errors",
    "recordings_from_manifest",
]
