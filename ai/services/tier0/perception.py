"""Tier 0 인지 단계 조립: 입력 → (검출 → 카메라별 추적) → 카메라 간 연결 → World Track → 접지 → world_state (명세 5.1·5.2).

판정·추정 로직은 기능 패키지에 있다(ai/CONVENTION.md). 여기서는 순서와 시각만 정한다.

입력(FrameFeed) 두 가지:
- SimObservationFeed: `python -m sim.run` 출력의 2D keypoint 를 넣는다(GPU 없음). 검출기와 Local Tracker 의 대역이다:
  시뮬레이터가 붙인 카메라 내 트랙 ID 를 Local Tracker 출력으로 쓰고(tests/tracking/sim_world_track.py 와 같은 변환),
  검출 신뢰도는 보인 관절 신뢰도 평균이다. 촬영 시각은 장면 시작을 anchor_wall_ms 에 맞춰 Tier 0 벽시계로 옮긴다
  (재시작한 프로세스도 같은 anchor 면 같은 장면 시각을 이어 간다). 프레임은 촬영 시각 + arrival_lag_ms 가 지나야 '도착'
  하고, 도착한 지 max_backlog_ms 가 넘은 프레임은 넣지 않는다(재시작 직후 지난 장면을 몰아 처리하지 않는다, 5.1 최신성).
  결과는 SIM 상한이다(input_provenance.is_upper_bound = true).
- DetectorFeed: 실제 경로. 추론 대상 프레임(I0 수신·디코딩·최신성 정책을 거친 InferenceTicket)을 검출기(YOLO26-pose
  엔진, yolo_detector)에 묶음으로 넣고 MultiCameraLocalTracker 로 카메라 내 트랙 ID 를 붙인다.

공통 시각(Perception.choose_t): 수신 중인 카메라(최신 촬영 시각이 t_latest_global 에서 camera_stall_ms 안)의 최신 촬영
시각 중 가장 이른 값이다(docs/design/tier0-triangulation.md: 외삽하지 않으므로 모든 유효 카메라에 그 시각 이후 프레임이
있어야 한다). 멈춘 카메라는 빼서 한 카메라 정지가 전체를 멈추지 않게 한다. World Track 시각은 늘기만 하므로 직전보다
늘지 않으면 그 tick 은 인지 결과를 내지 않는다(같은 관측을 두 번 내지 않는다).

카메라 시각 상태(CameraClock)의 sync_valid 는 동기화 측정 모듈이 아직 없어 True 로 둔다(SIM 은 시각 오차가 없다.
LIVE 동기화 무효 판정은 남은 일). 현재 시각은 주입받는다(now_ms).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from simai_ai.contact import ContactEstimator, SurfaceSet
from simai_ai.geometry.rig import CameraRig
from simai_ai.io import SimObservationFrameSource
from simai_ai.providers.base import Frame
from simai_ai.tracking.cross_view import CrossViewAssociator, LocalTrackletBuffer
from simai_ai.tracking.local_tracker import (
    CameraDetections,
    MultiCameraLocalTracker,
    Observation2DFrame,
    PoseDetection,
    TrackedDetection,
)
from simai_ai.tracking.time_model import CameraClock, TimeModelConfig, compute_t_latest_global
from simai_ai.tracking.world_state import world_state_payload
from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackConfig, WorldTrackFrame, WorldTrackManager

log = logging.getLogger("services.tier0.perception")

DEFAULT_ARRIVAL_LAG_MS = 60
DEFAULT_MAX_BACKLOG_MS = 500


class FrameFeed(Protocol):
    """Local Tracker 출력(observation_2d) 단계까지 낸 입력."""

    @property
    def source(self) -> str: ...

    @property
    def camera_ids(self) -> tuple[str, ...]: ...

    def poll(self, now_ms: int) -> list[Observation2DFrame]:
        """지금까지 도착해 아직 내지 않은 프레임(카메라별 촬영 시각 순)."""
        ...

    def camera_connected(self, camera_id: str) -> bool:
        """그 카메라 입력이 연결돼 있는지(R7 의 카메라 상태). SIM 은 늘 True."""
        ...

    def provenance(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


def _score(detection: Any) -> float:
    confs = [kp[2] for kp in detection.keypoints_2d if kp is not None and kp[2] > 0]
    return min(1.0, max(0.0, sum(confs) / len(confs))) if confs else 0.0


def sim_frame_to_observation(frame: Frame, shift_ms: int) -> Observation2DFrame:
    """SIM 2D keypoint 프레임 → observation_2d(검출·Local Tracker 대역). 촬영 시각은 shift_ms 만큼 옮긴다."""
    dets = []
    for d in frame.detections or ():
        if d.local_track_id is None:
            continue  # 트랙 ID 가 없는 검출은 지어내지 않고 뺀다(Local Tracker 와 같은 규칙)
        dets.append(TrackedDetection(d.local_track_id, PoseDetection(tuple(d.bbox), _score(d), tuple(d.keypoints_2d))))
    ref = frame.frame_ref
    return Observation2DFrame(
        camera_id=frame.camera_id,
        stream_epoch=int(ref["stream_epoch"]),
        frame_sequence=int(ref["frame_sequence"]),
        estimated_capture_time_ms=frame.estimated_capture_time + shift_ms,
        timestamp_source=frame.timestamp_source,
        timestamp_uncertainty_ms=frame.timestamp_uncertainty_ms,
        detections=tuple(dets),
    )


class SimObservationFeed:
    """시뮬레이터 출력 → observation_2d. 장면 시작 시각을 anchor_wall_ms 에 맞춘다(모듈 설명)."""

    def __init__(
        self,
        run_dir: Path | str,
        *,
        anchor_wall_ms: int,
        arrival_lag_ms: int = DEFAULT_ARRIVAL_LAG_MS,
        max_backlog_ms: int = DEFAULT_MAX_BACKLOG_MS,
        camera_ids: Sequence[str] | None = None,
    ) -> None:
        if arrival_lag_ms < 0 or max_backlog_ms <= 0:
            raise ValueError("arrival_lag_ms 는 0 이상, max_backlog_ms 는 양수다")
        self._source = SimObservationFrameSource(run_dir, camera_ids)
        frames = list(self._source.frames())
        if not frames:
            raise ValueError(f"{run_dir}: SIM 관측 프레임이 없다")
        start = min(f.estimated_capture_time for f in frames)
        self.shift_ms = anchor_wall_ms - start
        self.end_wall_ms = max(f.estimated_capture_time for f in frames) + self.shift_ms
        self._frames = [sim_frame_to_observation(f, self.shift_ms) for f in frames]
        self._i = 0
        self._arrival = arrival_lag_ms
        self._backlog = max_backlog_ms
        self.stale_skipped = 0

    @property
    def source(self) -> str:
        return "SIM"

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return self._source.camera_ids

    @property
    def finished(self) -> bool:
        return self._i >= len(self._frames)

    def poll(self, now_ms: int) -> list[Observation2DFrame]:
        out = []
        while self._i < len(self._frames):
            f = self._frames[self._i]
            arrived = f.estimated_capture_time_ms + self._arrival
            if arrived > now_ms:
                break
            self._i += 1
            if now_ms - arrived > self._backlog:
                self.stale_skipped += 1  # 재시작 전·멈춘 동안의 장면: 몰아서 처리하지 않는다
                continue
            out.append(f)
        return out

    def camera_connected(self, camera_id: str) -> bool:
        return camera_id in self.camera_ids

    def provenance(self) -> dict[str, Any]:
        return self._source.provenance()

    def close(self) -> None:
        return None


class PoseDetector(Protocol):
    """디코딩된 프레임 묶음 → 카메라별 검출(YOLO26-pose 엔진 등)."""

    def detect(self, frames: Sequence[Frame]) -> list[CameraDetections]: ...


class DetectorFeed:
    """실제 경로: 추론 대상 프레임 → 검출기 → 카메라별 Local Tracker.

    tickets: 지금 추론할 프레임 묶음(I0Ingest.next_batch 등, InferenceTicket 이나 Frame). connected: 카메라 입력 연결 여부.
    """

    def __init__(
        self,
        *,
        camera_ids: Sequence[str],
        tickets: Callable[[], Sequence[Any]],
        detector: PoseDetector,
        tracker: MultiCameraLocalTracker,
        connected: Callable[[str], bool] = lambda _cam: True,
        source: str = "LIVE",
    ) -> None:
        self._camera_ids = tuple(camera_ids)
        self._tickets = tickets
        self._detector = detector
        self._tracker = tracker
        self._connected = connected
        self._source = source

    @property
    def source(self) -> str:
        return self._source

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return self._camera_ids

    def poll(self, now_ms: int) -> list[Observation2DFrame]:
        batch = [getattr(t, "frame", t) for t in self._tickets()]
        if not batch:
            return []
        return self._tracker.update_batch(self._detector.detect(batch))

    def camera_connected(self, camera_id: str) -> bool:
        return self._connected(camera_id)

    def provenance(self) -> dict[str, Any]:
        return {"source": self._source, "camera_ids": list(self._camera_ids)}

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class Perceived:
    """한 tick 의 인지 결과. payload 는 world_state payload(config_version 은 판정 루프가 그 tick 의 묶음으로 넣는다)."""

    t: int
    frame: WorldTrackFrame
    payload: dict[str, Any]
    t_latest_global: int


@dataclass(frozen=True)
class CameraSnapshot:
    camera_id: str
    last_capture: int | None
    frames_since: int  # 직전 take_camera_stats 뒤 처리한 프레임 수
    connected: bool


class Perception:
    """인지 단계 묶음. 판정 루프 스레드에서만 step 을 부른다. 카메라 상태 조회는 다른 스레드에서 해도 된다."""

    def __init__(
        self,
        rig: CameraRig,
        feed: FrameFeed,
        *,
        session: str,
        scene_version: str,
        input_provenance: Mapping[str, Any],
        time_config: TimeModelConfig = TimeModelConfig(),
        world_config: WorldTrackConfig | None = None,
        contact: ContactEstimator | None = None,
    ) -> None:
        missing = [c for c in feed.camera_ids if c not in rig.cameras]
        if missing:
            raise ValueError(f"보정 {rig.calibration_version} 에 없는 입력 카메라: {missing}")
        self.rig = rig
        self.feed = feed
        self.scene_version = scene_version
        self.time_config = time_config
        self.buffer = LocalTrackletBuffer()
        self.associator = CrossViewAssociator(rig)
        self.manager = WorldTrackManager(WorldIdIssuer(session), world_config, input_provenance=input_provenance)
        self.contact = contact or ContactEstimator()
        self._lock = threading.Lock()  # 카메라 상태(health 가 다른 스레드에서 읽는다)
        self._last_capture: dict[str, int | None] = {c: None for c in feed.camera_ids}
        self._frames_since: dict[str, int] = {c: 0 for c in feed.camera_ids}
        self.dropped_frames = 0

    def camera_clocks(self) -> tuple[CameraClock, ...]:
        with self._lock:
            return tuple(CameraClock(c, t, True) for c, t in sorted(self._last_capture.items()))

    def take_camera_stats(self) -> list[CameraSnapshot]:
        """카메라별 마지막 촬영 시각과 직전 호출 뒤 처리한 프레임 수(health fps 재료). 수는 0 으로 되돌린다."""
        with self._lock:
            out = [
                CameraSnapshot(c, self._last_capture[c], self._frames_since[c], self.feed.camera_connected(c))
                for c in sorted(self._last_capture)
            ]
            for c in self._frames_since:
                self._frames_since[c] = 0
            return out

    def t_latest_global(self) -> int | None:
        return compute_t_latest_global(self.camera_clocks())

    def choose_t(self) -> int | None:
        """공통 시각: 수신 중인 카메라의 최신 촬영 시각 중 가장 이른 값(모듈 설명). 모르면 None."""
        with self._lock:
            times = [t for t in self._last_capture.values() if t is not None]
        if not times:
            return None
        latest = max(times)
        fresh = [t for t in times if latest - t <= self.time_config.camera_stall_ms]
        return min(fresh)

    def ingest(self, frames: Sequence[Observation2DFrame]) -> None:
        for f in frames:
            if f.camera_id not in self._last_capture:
                self.dropped_frames += 1
                continue
            self.buffer.add(f)
            with self._lock:
                prev = self._last_capture[f.camera_id]
                if prev is None or f.estimated_capture_time_ms > prev:
                    self._last_capture[f.camera_id] = f.estimated_capture_time_ms
                self._frames_since[f.camera_id] += 1

    def step(self, now_ms: int, surfaces: SurfaceSet, sync_invalid: Sequence[str] = ()) -> Perceived | None:
        """입력을 받아 공통 시각 t 의 World Track·접지·world_state payload 를 만든다. 새 시각이 없으면 None."""
        self.ingest(self.feed.poll(now_ms))
        t = self.choose_t()
        t_latest = self.t_latest_global()
        last = self.manager.last_t
        if t is None or t_latest is None or (last is not None and t <= last):
            return None
        views, _drops = self.buffer.views_at(t)
        assoc = self.associator.associate(t, views, exclude_cameras=tuple(sync_invalid))
        self.buffer.prune(t)
        frame = self.manager.update(assoc, self.rig, tuple(sync_invalid))
        contacts = self.contact.update(frame, surfaces)
        payload = world_state_payload(
            frame, t_latest, self.scene_version, contacts=contacts.contacts(), time_config=self.time_config
        )
        return Perceived(t=t, frame=frame, payload=payload, t_latest_global=t_latest)
