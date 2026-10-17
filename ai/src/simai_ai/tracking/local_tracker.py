"""카메라별 Local Tracker 와 observation_2d (명세 5.1 추적, 5.2 추적 3계층 1층, 수정 7, 요구사항 FR-57).

YOLO26x-pose 묶음 추론 결과(카메라 ID, 프레임 식별자, 촬영 시각, 박스·신뢰도·keypoint)를 카메라별 독립 추적기에
넣고 카메라 내 트랙 ID 를 붙여 `observation_2d` 를 낸다.

- 추론 모델은 공유하지만 **ID 공간·시간 기록·추적 상태는 카메라마다 분리**한다. 카메라마다 추적기 인스턴스,
  ID 발급 카운터(`{camera_id}-t{n}`), 마지막 처리 프레임·촬영 시각을 따로 둔다. 한 카메라의 입력이 다른 카메라의
  상태를 바꾸지 않는다.
- 카메라 안에서는 프레임이 (stream_epoch, frame_sequence)와 촬영 시각 순서로 들어와야 한다. 이미 처리한 프레임보다
  오래된 프레임은 추적기에 넣지 않고 버린 수만 센다(명세 5.1 프레임 최신성: 오래된 결과로 상태를 갱신하지 않음).
- 촬영 시각 간격이 `max_frame_gap_ms` 를 넘으면 그 카메라의 추적기를 새로 만든다. 추적기의 `track_buffer` 는
  프레임 수라서 긴 공백 뒤 엉뚱한 연결을 막기 위함이다. 새 추적기의 트랙은 새 ID 를 받는다(ID 를 다시 쓰지 않음).
  잘못 이어 붙인 ID 보다 갈라진 ID 가 덜 위험하다(명세 5.2).
- 출력 bbox·keypoint 는 **검출 원본 값**이다(추적기 칼만 상태 박스가 아님). 추적기가 이번 프레임에 트랙을 붙이지
  않은 검출(확정 전 새 트랙, 저신뢰 미연결)은 `observation_2d` 에 넣지 않고 수만 센다(트랙 ID 를 지어내지 않음).
- 카메라 내 트랙 ID 는 사람 정체성의 정본이 아니다. 카메라 간 연결(-34)·World Track(-37)은 이 모듈 범위 밖이고
  `world_id` 는 null 로 낸다.

추적기 구현은 `LocalTrackBackend` 로 감싼다. 기본은 Ultralytics 추적기(`tracking.ultralytics_local`, extras
`detection`)이며 이 모듈 자체는 표준 라이브러리만 쓴다. 시각은 UTC int64 ms 이고 현재 시각을 읽지 않는다.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from simai_ai.providers.base import TIMESTAMP_SOURCES
from simai_ai.tracking.local_config import TrackerConfig, TrackerKind, tracker_config
from simai_ai.tracking.time_model import require_ms

BBox = tuple[float, float, float, float]
"""픽셀 [x1, y1, x2, y2], 원본 해상도 기준 (contracts common bbox_xyxy)"""
Keypoint = tuple[float, float, float] | None
"""[u, v, confidence] 또는 None(검출되지 않음). joint_layout 순서 (contracts common keypoint_2d)"""

DEFAULT_MAX_FRAME_GAP_MS = 2000
"""초기값. 같은 카메라의 촬영 시각 간격이 이보다 크면 추적기를 새로 만든다 (G2·A0 에서 조정)"""

# contracts/common/defs.schema.json 의 joint_layout 중 관절 수가 정해진 것. MHR_NATIVE 는 길이를 검사하지 않는다
JOINT_COUNTS: Mapping[str, int] = {"COCO17": 17, "HALPE26": 26, "MHR70": 70}
JOINT_LAYOUTS = ("COCO17", "HALPE26", "MHR70", "MHR_NATIVE")

_CAMERA_ID = re.compile(r"[A-Za-z0-9_-]+")


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}: 유한한 수여야 한다 (받은 값 {value!r})")
    return float(value)


def _count(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}: 0 이상 정수여야 한다 (받은 값 {value!r})")
    return value


def require_camera_id(camera_id: Any) -> str:
    if not isinstance(camera_id, str) or not _CAMERA_ID.fullmatch(camera_id):
        raise ValueError(f"camera_id: 영문·숫자·_·- 로 된 문자열이어야 한다 (받은 값 {camera_id!r})")
    return camera_id


# --------------------------------------------------------------------------- 입력


@dataclass(frozen=True)
class PoseDetection:
    """사람 검출 한 건 (YOLO26-pose 결과 한 행).

    reid_feature 는 BoT-SORT·TrackTrack 의 ReID(`with_reid: true`, 검출기 특징)를 켰을 때만 쓴다.
    """

    bbox: BBox
    score: float
    keypoints_2d: tuple[Keypoint, ...]
    reid_feature: tuple[float, ...] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if len(self.bbox) != 4:
            raise ValueError(f"bbox: [x1, y1, x2, y2] 4개여야 한다 (받은 값 {self.bbox!r})")
        x1, y1, x2, y2 = (_finite("bbox", v) for v in self.bbox)
        if x2 < x1 or y2 < y1:
            raise ValueError(f"bbox: x2 ≥ x1, y2 ≥ y1 이어야 한다 (받은 값 {self.bbox!r})")
        object.__setattr__(self, "bbox", (x1, y1, x2, y2))
        score = _finite("score", self.score)
        if not 0 <= score <= 1:
            raise ValueError(f"score: 0~1 이어야 한다 (받은 값 {self.score!r})")
        object.__setattr__(self, "score", score)
        kps: list[Keypoint] = []
        for i, kp in enumerate(self.keypoints_2d):
            if kp is None:
                kps.append(None)
                continue
            if len(kp) != 3:
                raise ValueError(f"keypoints_2d[{i}]: [u, v, confidence] 또는 None 이어야 한다 (받은 값 {kp!r})")
            u, v, c = (_finite(f"keypoints_2d[{i}]", x) for x in kp)
            kps.append((u, v, c))
        object.__setattr__(self, "keypoints_2d", tuple(kps))
        if self.reid_feature is not None:
            feat = tuple(_finite("reid_feature", x) for x in self.reid_feature)
            if not feat:
                raise ValueError("reid_feature: 비어 있으면 None 으로 둔다")
            object.__setattr__(self, "reid_feature", feat)


@dataclass(frozen=True)
class CameraDetections:
    """묶음 추론 결과 중 카메라 한 대·프레임 하나 (명세 12.7 프레임 식별자, 3.9 촬영 시각 계약).

    프레임 식별자와 촬영 시각은 I0 프레임 헤더 값을 그대로 쓴다. timestamp_uncertainty_ms 를 모르면 None 이다
    (0 으로 채우지 않는다). 사람이 없으면 detections 는 빈 튜플이다(프레임은 처리됨).
    """

    camera_id: str
    stream_epoch: int
    frame_sequence: int
    estimated_capture_time_ms: int
    timestamp_source: str
    timestamp_uncertainty_ms: float | None
    detections: tuple[PoseDetection, ...] = ()

    def __post_init__(self) -> None:
        require_camera_id(self.camera_id)
        _count("stream_epoch", self.stream_epoch)
        _count("frame_sequence", self.frame_sequence)
        require_ms("estimated_capture_time_ms", self.estimated_capture_time_ms)
        if self.timestamp_source not in TIMESTAMP_SOURCES:
            raise ValueError(f"timestamp_source {self.timestamp_source!r} 는 {TIMESTAMP_SOURCES} 중 하나여야 한다")
        u = self.timestamp_uncertainty_ms
        if u is not None and not 0 <= _finite("timestamp_uncertainty_ms", u):
            raise ValueError(f"timestamp_uncertainty_ms: 0 이상이어야 한다 (받은 값 {u!r})")
        object.__setattr__(self, "detections", tuple(self.detections))

    @property
    def frame_key(self) -> tuple[int, int]:
        return (self.stream_epoch, self.frame_sequence)

    @classmethod
    def from_header(cls, header: Any, detections: Sequence[PoseDetection]) -> CameraDetections:
        """I0 프레임 헤더(`simai_ai.io.I0FrameHeader` 와 같은 속성)와 그 프레임의 검출로 만든다."""
        return cls(
            camera_id=header.camera_id,
            stream_epoch=header.stream_epoch,
            frame_sequence=header.frame_sequence,
            estimated_capture_time_ms=header.estimated_capture_time_ms,
            timestamp_source=header.timestamp_source,
            timestamp_uncertainty_ms=header.timestamp_uncertainty_ms,
            detections=tuple(detections),
        )


# --------------------------------------------------------------------------- 출력


@dataclass(frozen=True)
class TrackedDetection:
    """카메라 내 트랙 ID 가 붙은 검출. detection 은 검출 원본 그대로다."""

    local_track_id: str
    detection: PoseDetection


@dataclass(frozen=True)
class Observation2DFrame:
    """카메라 한 대·프레임 하나의 observation_2d (contracts observation_batch 의 frame_observation).

    untracked_count: 검출됐지만 이번 프레임에 트랙이 붙지 않아 detections 에서 뺀 수.
    """

    camera_id: str
    stream_epoch: int
    frame_sequence: int
    estimated_capture_time_ms: int
    timestamp_source: str
    timestamp_uncertainty_ms: float | None
    detections: tuple[TrackedDetection, ...]
    untracked_count: int = 0

    def to_contract(self) -> dict[str, Any]:
        """observation_batch payload.frames[] 한 항목. 촬영 시각 불확실성을 모르면 계약을 채울 수 없어 오류다."""
        if self.timestamp_uncertainty_ms is None:
            raise ValueError(
                f"{self.camera_id} ({self.stream_epoch}, {self.frame_sequence}): timestamp_uncertainty_ms 를 몰라 "
                "observation_2d 를 만들 수 없다 (0 으로 채우지 않는다)"
            )
        return {
            "camera_id": self.camera_id,
            "stream_epoch": self.stream_epoch,
            "frame_sequence": self.frame_sequence,
            "estimated_capture_time_ms": self.estimated_capture_time_ms,
            "timestamp_source": self.timestamp_source,
            "timestamp_uncertainty_ms": self.timestamp_uncertainty_ms,
            "detections": [
                {
                    "local_track_id": t.local_track_id,
                    "bbox": list(t.detection.bbox),
                    "score": t.detection.score,
                    "keypoints_2d": [list(kp) if kp is not None else None for kp in t.detection.keypoints_2d],
                    "world_id": None,
                }
                for t in self.detections
            ],
        }


def observation_batch_payload(
    frames: Sequence[Observation2DFrame], *, calibration_version: str, joint_layout: str = "COCO17"
) -> dict[str, Any]:
    """observation_batch 의 payload (contracts/messages/observation_batch.schema.json).

    봉투(msg_id, sent_at, source_id)는 I1 송신 쪽이 붙인다. 관절 배열 길이가 joint_layout 과 맞는지 검사한다
    (스키마로 표현할 수 없는 규칙, contracts/README.md).
    """
    if not frames:
        raise ValueError("frames 가 비어 있다 (계약은 1개 이상)")
    if not isinstance(calibration_version, str) or not calibration_version:
        raise ValueError("calibration_version 이 비어 있다")
    if joint_layout not in JOINT_LAYOUTS:
        raise ValueError(f"joint_layout {joint_layout!r} 는 {JOINT_LAYOUTS} 중 하나여야 한다")
    expected = JOINT_COUNTS.get(joint_layout)
    if expected is not None:
        for f in frames:
            for t in f.detections:
                if len(t.detection.keypoints_2d) != expected:
                    raise ValueError(
                        f"{f.camera_id} {t.local_track_id}: keypoint {len(t.detection.keypoints_2d)}개, "
                        f"{joint_layout} 는 {expected}개"
                    )
    return {
        "calibration_version": calibration_version,
        "joint_layout": joint_layout,
        "frames": [f.to_contract() for f in frames],
    }


# --------------------------------------------------------------------------- 추적기


class LocalTrackBackend(Protocol):
    """추적기 구현 하나(카메라 하나 전용). 프레임 입력 없이 검출 배열만 받는다."""

    def update(
        self,
        boxes: Sequence[BBox],
        scores: Sequence[float],
        features: Sequence[Sequence[float]] | None = None,
    ) -> list[tuple[int, int]]:
        """한 프레임을 넣고 이번 프레임에 갱신된 트랙의 (추적기 내부 트랙 ID, 검출 인덱스) 목록을 돌려준다."""
        ...

    def live_track_ids(self) -> tuple[frozenset[int], frozenset[int]]:
        """(추적 중, 잃음) 트랙의 추적기 내부 ID. 제거된 트랙은 들어 있지 않다."""
        ...


BackendFactory = Callable[[TrackerConfig], LocalTrackBackend]


def ultralytics_backend_factory(config: TrackerConfig) -> LocalTrackBackend:
    """기본 백엔드: Ultralytics 추적기 (extras `detection`). ultralytics 는 이때 처음 import 한다."""
    from simai_ai.tracking.ultralytics_local import UltralyticsTrackBackend

    return UltralyticsTrackBackend(config)


@dataclass(frozen=True)
class CameraTrackerState:
    """카메라 한 대의 추적 상태 요약 (시험·health 용).

    tracked_ids / lost_ids: 한 번이라도 observation_2d 로 낸 트랙 중 지금 추적 중 / 잃은(재연결 대기) 것.
    issued_ids: 이 카메라에서 발급한 카메라 내 트랙 ID 수(추적기를 새로 만들어도 이어서 센다).
    resets: 촬영 시각 공백으로 추적기를 새로 만든 횟수. rejected_frames: 순서가 어긋나 버린 프레임 수.
    """

    camera_id: str
    tracker_kind: TrackerKind
    frames_processed: int
    last_capture_time_ms: int | None
    last_frame_key: tuple[int, int] | None
    tracked_ids: tuple[str, ...]
    lost_ids: tuple[str, ...]
    issued_ids: int
    resets: int
    rejected_frames: int
    untracked_detections: int


class CameraLocalTracker:
    """카메라 한 대의 Local Tracker. 추적기 인스턴스·ID 발급·시간 기록을 이 카메라 안에만 둔다."""

    def __init__(
        self,
        camera_id: str,
        config: TrackerConfig,
        backend_factory: BackendFactory = ultralytics_backend_factory,
        max_frame_gap_ms: int = DEFAULT_MAX_FRAME_GAP_MS,
    ) -> None:
        self.camera_id = require_camera_id(camera_id)
        if isinstance(max_frame_gap_ms, bool) or not isinstance(max_frame_gap_ms, int) or max_frame_gap_ms <= 0:
            raise ValueError(f"max_frame_gap_ms: 양의 정수 ms 여야 한다 (받은 값 {max_frame_gap_ms!r})")
        self.config = config
        self.max_frame_gap_ms = max_frame_gap_ms
        self._factory = backend_factory
        self._backend = backend_factory(config)
        self._ids: dict[int, str] = {}
        self._issued = 0
        self._frames = 0
        self._last_capture: int | None = None
        self._last_key: tuple[int, int] | None = None
        self._resets = 0
        self._rejected = 0
        self._untracked = 0

    def _is_stale(self, frame: CameraDetections) -> bool:
        if self._last_key is None or self._last_capture is None:
            return False
        return frame.frame_key <= self._last_key or frame.estimated_capture_time_ms < self._last_capture

    def _reset(self) -> None:
        self._backend = self._factory(self.config)
        self._ids.clear()
        self._resets += 1

    def _local_id(self, backend_id: int) -> str:
        local = self._ids.get(backend_id)
        if local is None:
            self._issued += 1
            local = f"{self.camera_id}-t{self._issued}"
            self._ids[backend_id] = local
        return local

    def update(self, frame: CameraDetections) -> Observation2DFrame | None:
        """프레임 하나를 넣는다. 이미 처리한 프레임보다 오래됐으면 넣지 않고 None 을 돌려준다."""
        if frame.camera_id != self.camera_id:
            raise ValueError(f"{self.camera_id} 추적기에 {frame.camera_id} 프레임을 넣었다")
        if self._is_stale(frame):
            self._rejected += 1
            return None
        if (
            self._last_capture is not None
            and frame.estimated_capture_time_ms - self._last_capture > self.max_frame_gap_ms
        ):
            self._reset()

        dets = frame.detections
        features = None
        if self.config.with_reid and dets:
            if any(d.reid_feature is None for d in dets):
                raise ValueError(f"{self.camera_id}: with_reid 설정인데 reid_feature 가 없는 검출이 있다")
            features = [d.reid_feature for d in dets]
        pairs = self._backend.update([d.bbox for d in dets], [d.score for d in dets], features)

        seen_tracks: set[int] = set()
        by_index: dict[int, int] = {}
        for backend_id, det_index in pairs:
            if not 0 <= det_index < len(dets) or det_index in by_index or backend_id in seen_tracks:
                raise RuntimeError(f"{self.camera_id}: 추적기 출력이 검출과 1:1 이 아니다 ({backend_id}, {det_index})")
            seen_tracks.add(backend_id)
            by_index[det_index] = backend_id
        tracked = tuple(TrackedDetection(self._local_id(by_index[i]), dets[i]) for i in sorted(by_index))
        # 제거된 트랙의 대응표는 지운다(추적기 내부 ID 는 다시 쓰이지 않지만 표가 끝없이 자라지 않게)
        alive_tracked, alive_lost = self._backend.live_track_ids()
        alive = alive_tracked | alive_lost
        for backend_id in [b for b in self._ids if b not in alive]:
            del self._ids[backend_id]

        untracked = len(dets) - len(tracked)
        self._frames += 1
        self._untracked += untracked
        self._last_capture = frame.estimated_capture_time_ms
        self._last_key = frame.frame_key
        return Observation2DFrame(
            camera_id=frame.camera_id,
            stream_epoch=frame.stream_epoch,
            frame_sequence=frame.frame_sequence,
            estimated_capture_time_ms=frame.estimated_capture_time_ms,
            timestamp_source=frame.timestamp_source,
            timestamp_uncertainty_ms=frame.timestamp_uncertainty_ms,
            detections=tracked,
            untracked_count=untracked,
        )

    def state(self) -> CameraTrackerState:
        alive_tracked, alive_lost = self._backend.live_track_ids()
        return CameraTrackerState(
            camera_id=self.camera_id,
            tracker_kind=self.config.kind,
            frames_processed=self._frames,
            last_capture_time_ms=self._last_capture,
            last_frame_key=self._last_key,
            tracked_ids=tuple(sorted(self._ids[b] for b in alive_tracked if b in self._ids)),
            lost_ids=tuple(sorted(self._ids[b] for b in alive_lost if b in self._ids)),
            issued_ids=self._issued,
            resets=self._resets,
            rejected_frames=self._rejected,
            untracked_detections=self._untracked,
        )


class MultiCameraLocalTracker:
    """카메라별 독립 Local Tracker 묶음. 묶음 추론 결과를 카메라별 추적기에 나눠 넣는다.

    camera_ids 를 주면 그 카메라만 받고(모르는 카메라는 오류), 주지 않으면 처음 보는 카메라마다 추적기를 만든다.
    """

    def __init__(
        self,
        config: TrackerConfig | None = None,
        *,
        camera_ids: Sequence[str] | None = None,
        backend_factory: BackendFactory = ultralytics_backend_factory,
        max_frame_gap_ms: int = DEFAULT_MAX_FRAME_GAP_MS,
    ) -> None:
        self.config = config if config is not None else tracker_config()
        self._factory = backend_factory
        self._max_gap = max_frame_gap_ms
        self._fixed = camera_ids is not None
        self._trackers: dict[str, CameraLocalTracker] = {}
        for cam in camera_ids or ():
            if cam in self._trackers:
                raise ValueError(f"camera_ids 에 {cam} 가 두 번 있다")
            self._trackers[cam] = self._new(cam)

    def _new(self, camera_id: str) -> CameraLocalTracker:
        return CameraLocalTracker(camera_id, self.config, self._factory, self._max_gap)

    def tracker(self, camera_id: str) -> CameraLocalTracker:
        t = self._trackers.get(camera_id)
        if t is None:
            if self._fixed:
                raise KeyError(f"등록되지 않은 카메라: {camera_id}")
            t = self._trackers[camera_id] = self._new(camera_id)
        return t

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(self._trackers)

    def update_batch(self, batch: Sequence[CameraDetections]) -> list[Observation2DFrame]:
        """묶음 추론 결과를 넣는다. 들어온 순서대로 처리하고, 순서가 어긋나 버린 프레임은 결과에서 빠진다."""
        out = []
        for frame in batch:
            obs = self.tracker(frame.camera_id).update(frame)
            if obs is not None:
                out.append(obs)
        return out

    def state(self, camera_id: str) -> CameraTrackerState:
        return self._trackers[camera_id].state()

    def states(self) -> dict[str, CameraTrackerState]:
        return {cam: t.state() for cam, t in self._trackers.items()}
