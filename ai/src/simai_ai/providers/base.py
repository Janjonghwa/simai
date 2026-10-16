"""입력 대체 계약의 Provider 인터페이스 (명세 20.4, 5.1 입력 추상화).

상류 입력(프레임, 보정, World 상태)은 이 인터페이스로만 받는다. 지금은 대체 구현(DATASET_GT, SIM,
MANUAL_PNP 등)으로 개발하고, 상류가 준비되면 설정만 바꿔 실제 구현(G1_AUTO, TIER0_LOG)으로 바꾼다.

- CalibrationProvider·WorldTrackProvider 출력은 `contracts/providers/*.schema.json` 문서 그대로의
  dict(JSON 호환)다. 스키마 생성 타입(S15P31A101-24)이 생기면 별칭만 그 타입으로 바꾼다.
- FrameSource 의 Frame 은 I0 정식 메시지 스키마가 아직 없어(S15P31A101-20 보류) 이 모듈의 임시 형식이다.
  필드 이름과 의미는 명세 3.9 촬영 시각 계약과 `world_tracks.schema.json` 의 `camera_observation`
  (camera_id, frame_ref, estimated_capture_time, timestamp_source, timestamp_uncertainty_ms)에 맞췄다.
  -20 에서 I0 스키마가 정해지면 그 스키마에 맞춘다.
- 시각은 UTC int64 ms, World 는 미터·오른손·+Z 상방이다. 모르는 값은 비운다(None/null).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# contracts/providers/calibration.schema.json 문서 한 개
CalibrationDoc = dict[str, Any]
# contracts/providers/world_tracks.schema.json 문서 한 개 (JSONL 한 줄)
WorldTracksDoc = dict[str, Any]
# common/defs.schema.json#/$defs/input_provenance 의 frames / calibration / tracks 조각
FramesProvenance = dict[str, Any]
CalibrationProvenance = dict[str, Any]
TracksProvenance = dict[str, Any]

# common/defs.schema.json 의 enum 과 같은 값
FRAME_SOURCES = ("LIVE", "FILE", "SIM", "DATASET")
TIMESTAMP_SOURCES = ("CAMERA_STREAM", "RECEIVE_ESTIMATED", "FILE", "SIM")


@dataclass(frozen=True)
class Detection2D:
    """2D keypoint 단계 입력 한 건 (시뮬레이터가 검출 단계를 건너뛰고 넣는 경우, 명세 5.1).

    keypoints_2d 는 joint_layout 순서의 [u, v, confidence] 또는 None(검출되지 않음)이다.
    local_track_id 는 카메라 내 트랙 ID 이며 사람 정체성의 정본이 아니다(명세 20.3).
    """

    bbox: tuple[float, float, float, float]
    joint_layout: str
    keypoints_2d: tuple[tuple[float, float, float] | None, ...]
    local_track_id: str | None = None


@dataclass(frozen=True)
class Frame:
    """카메라 한 대의 한 촬영 시각 입력 (명세 3.9 촬영 시각 계약).

    image 와 detections 중 정확히 하나를 채운다. image 의 형식(예: HxWx3 BGR 배열)은 구현이 정하고
    이 패키지는 그 내용을 해석하지 않는다(numpy 등에 의존하지 않기 위함).
    frame_ref 는 `world_tracks.schema.json` camera_observation.frame_ref 와 같다:
    LIVE·녹화는 {stream_epoch, frame_sequence}, 파일·데이터셋은 {file_uri, frame_index}.
    """

    camera_id: str
    estimated_capture_time: int
    timestamp_source: str
    frame_ref: Mapping[str, Any]
    timestamp_uncertainty_ms: float | None = None
    image: Any = None
    detections: Sequence[Detection2D] | None = None


class FrameSource(ABC):
    """카메라 ID + 촬영 시각 + 프레임(또는 2D keypoint)을 같은 형식으로 낸다 (명세 5.1, 20.4)."""

    @property
    @abstractmethod
    def source(self) -> str:
        """frame_source: LIVE, FILE, SIM, DATASET."""

    @property
    @abstractmethod
    def camera_ids(self) -> tuple[str, ...]:
        """이 입력에 들어 있는 카메라 ID."""

    @abstractmethod
    def frames(self) -> Iterator[Frame]:
        """프레임을 낸다. 카메라별로 estimated_capture_time 이 줄어들지 않는다."""

    def provenance(self) -> FramesProvenance:
        """소비 결과물의 input_provenance.frames 에 그대로 넣을 조각."""
        return {"source": self.source, "camera_ids": list(self.camera_ids)}


class CalibrationProvider(ABC):
    """카메라별 K·왜곡·R·t(OpenCV, World → 카메라)·영상 크기·보정 상태·스케일 근거를 낸다 (명세 4장, 20.4)."""

    @abstractmethod
    def current(self) -> CalibrationDoc:
        """지금 쓰는 보정 한 버전. `calibration.schema.json` 문서 그대로."""

    def provenance(self) -> CalibrationProvenance:
        """소비 결과물의 input_provenance.calibration 에 그대로 넣을 조각."""
        doc = self.current()
        return {"source": doc["provider_source"], "calibration_version": doc["calibration_version"]}


class WorldTrackProvider(ABC):
    """촬영 시각별 사람 목록을 낸다 (명세 5.2, 20.4). Tier 0 가 위치의 정본이며 대체 구현도 같은 의미를 지킨다."""

    @property
    @abstractmethod
    def source(self) -> str:
        """track_source: TIER0_LOG, TIER0_LIVE, DATASET_GT, SIM. 출력 문서의 provider_source 와 같다."""

    @property
    @abstractmethod
    def provider_version(self) -> str:
        """출력 문서의 provider_version 과 같다."""

    @abstractmethod
    def frames(self) -> Iterator[WorldTracksDoc]:
        """시각 순서(t 증가)로 `world_tracks.schema.json` 문서를 낸다."""

    def provenance(self) -> TracksProvenance:
        """소비 결과물의 input_provenance.tracks 에 그대로 넣을 조각."""
        return {"source": self.source, "provider_version": self.provider_version}
