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
  엔진, yolo_detector)에 묶음으로 넣고 MultiCameraLocalTracker 로 카메라 내 트랙 ID 를 붙인다. 영상 수신·디코딩 조립은
  live_input.LiveInput 이 한다(S15P31A101-182). 녹화 재생(file 입력)도 같은 DetectorFeed 를 쓴다(file_input, -215).
  검출·로컬추적(detect_once)은 10 Hz 판정 루프(judge_lock) 밖에서 별도 주기(tier0-detect, 기본 20 Hz)로 돌아 카메라당
  12 FPS 이상 관측을 카메라별 유계 대기열에 쌓고, 판정 루프(Perception.step → poll)는 대기열에 쌓인 관측만 꺼내 보간·융합에
  쓴다(S15P31A101-383).

공통 시각(Perception.choose_t, time_model.choose_fusion_time): 융합에 넣을 카메라의 최신 촬영 시각 중 가장 이른 값이다
(docs/design/tier0-triangulation.md: 외삽하지 않으므로 넣을 카메라 모두에 그 시각 이후 프레임이 있어야 한다). 멈춘 카메라
(camera_stall_ms 초과)와, 수신 중이어도 t_latest_global 보다 fusion_max_lag_ms 넘게 늦은 카메라는 빼서 한 카메라의 정지·
지연이 전체를 멈추거나 늦추지 않게 한다(docs/design/tier0-fusion-time.md). 뺀 늦은 카메라는 그 tick 관측에 들어가지 않고
(트랙 cameras 에 없다) Perceived.lagging·Perception.lagging·lagging_ticks 에 남고, 판정의 감시 입력(R7)은 이 카메라를
정상 관측 카메라로 세지 않는다. World Track 시각은 늘기만 하므로 직전보다
늘지 않으면 그 tick 은 인지 결과를 내지 않는다(같은 관측을 두 번 내지 않는다).

카메라 시각 상태(CameraClock)의 sync_valid 는 동기화 측정 모듈이 아직 없어 True 로 둔다(SIM 은 시각 오차가 없다.
LIVE 동기화 무효 판정은 남은 일). 현재 시각은 주입받는다(now_ms).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
import queue
import threading
from typing import Any, Protocol

from simai_ai.calibration.drift import CameraObservation
from simai_ai.contact import ContactEstimator, SurfaceSet
from simai_ai.geometry.rig import CameraRig
from simai_ai.io import ResultGate, SimObservationFrameSource
from simai_ai.providers.base import Frame
from simai_ai.tracking.cross_view import AssociationFrame, CrossViewAssociator, LocalTrackletBuffer
from simai_ai.tracking.diagnostics import TrackingDiagnostics, window_log_line
from simai_ai.tracking.stage_timing import StageTiming
from simai_ai.tracking.local_tracker import (
    CameraDetections,
    MultiCameraLocalTracker,
    Observation2DFrame,
    PoseDetection,
    TrackedDetection,
)
from simai_ai.tracking.time_model import (
    CameraClock,
    FusionTime,
    TimeModelConfig,
    choose_fusion_time,
    compute_t_latest_global,
)
from simai_ai.tracking.world_state import world_state_payload
from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackConfig, WorldTrackFrame, WorldTrackManager

log = logging.getLogger("services.tier0.perception")

DEFAULT_ARRIVAL_LAG_MS = 60
DEFAULT_MAX_BACKLOG_MS = 500
DEFAULT_MAX_QUEUED_OBSERVATIONS_PER_CAMERA = 8
DEFAULT_AVATAR_SHARE_PERIOD_S = 0.2


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
            else:
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


@dataclass
class DetectorFeedStats:
    """실제 경로 검출 단계 집계(표시 파일·로그. health 계약에 자리가 없는 값)."""

    batches: int = 0
    frames: int = 0  # 검출기에 넣은 프레임
    detector_errors: int = 0  # 검출기가 예외를 낸 묶음
    frames_lost_to_errors: int = 0  # 그 묶음의 프레임(관측을 내지 못함)
    mismatched: int = 0  # 검출 결과의 카메라·프레임 식별자·촬영 시각이 넣은 프레임과 달라 버린 결과
    stale_results: int = 0  # 이미 반영한 결과보다 촬영 시각이 늦지 않아 버린 결과(ResultGate)
    dropped_observations: int = 0  # 판정 소비가 밀려 카메라별 관측 대기열 상한을 넘겨 버린 오래된 관측


def _ids(frame: Frame) -> tuple[str, int | None, int | None, int]:
    ref = frame.frame_ref
    return frame.camera_id, ref.get("stream_epoch"), ref.get("frame_sequence"), frame.estimated_capture_time


class DetectorFeed:
    """실제 경로: 추론 대상 프레임 → 검출기 → 카메라별 Local Tracker.

    tickets: 지금 추론할 프레임 묶음(I0Ingest.next_batch 등, InferenceTicket 이나 Frame). connected: 카메라 입력이 관측을
    낼 수 있는지(R7·health 의 카메라 상태). on_close: 닫을 때 부를 함수(영상 수신 정지 등).

    - 검출·로컬추적(detect_once)은 판정 lock 밖에서 돌고, 결과 Observation2DFrame 은 카메라별 유계 대기열에 넣는다.
    - 판정 루프(Perception.step → poll)는 대기열에 쌓인 관측만 꺼내 보간·다중 카메라 융합에 쓴다.
    - 검출기가 예외를 내면 그 묶음은 관측 없이 버리고 센다(판정 tick 은 이어 간다. 지난 관측을 다시 내지 않는다).
    - 검출 결과는 넣은 프레임과 카메라·(stream_epoch, frame_sequence)·촬영 시각이 같아야 한다. 다르면 버린다
      (검출기가 시각을 바꾸거나 순서를 섞으면 다른 프레임에 시각이 붙는다, G3).
    - 오래된 프레임의 뒤늦은 결과로 LIVE 상태를 갱신하지 않는다(명세 5.1, ResultGate).
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
        on_close: Callable[[], None] | None = None,
        provenance: Mapping[str, Any] | None = None,
        max_queued_per_camera: int = DEFAULT_MAX_QUEUED_OBSERVATIONS_PER_CAMERA,
        async_mode: bool = False,
        reid_collector: Any = None,
    ) -> None:
        if max_queued_per_camera < 1:
            raise ValueError("max_queued_per_camera 는 1 이상이다")
        self._camera_ids = tuple(camera_ids)
        self._camera_order = {c: i for i, c in enumerate(self._camera_ids)}
        self._tickets = tickets
        self._detector = detector
        self._tracker = tracker
        self._connected = connected
        self._source = source
        self._on_close = on_close
        self._provenance = None if provenance is None else dict(provenance)  # 녹화 재생: dataset_id 포함(-215)
        self._gate = ResultGate()
        self._max_queued_per_camera = max_queued_per_camera
        self.async_mode = async_mode
        self.reid_collector = reid_collector
        self._reid_queue: queue.Queue[tuple[Frame, Observation2DFrame]] = queue.Queue(maxsize=4)
        self._reid_stop = threading.Event()
        self._reid_worker: threading.Thread | None = None
        self.reid_submission_dropped = 0
        if reid_collector is not None:
            self._reid_worker = threading.Thread(target=self._submit_reid, name="tier0-reid-crop", daemon=True)
            self._reid_worker.start()
        self._lock = threading.Lock()
        self._detect_lock = threading.Lock()
        self._queued_observations: dict[str, deque[Observation2DFrame]] = {
            c: deque() for c in self._camera_ids
        }
        self._in_flight = 0
        self._stepped = False
        self.avatar_frames: dict[str, Frame] = {}
        self.avatar_observations: dict[str, Observation2DFrame] = {}
        self.avatar_enabled = False
        self.camera_streams: dict[str, CameraObservation] = {}
        self.stats = DetectorFeedStats()
        self.timing = StageTiming()
        self.people = 0
        self.observe_decoded: Callable[[Frame, CameraDetections], Any] | None = None

    @property
    def source(self) -> str:
        return self._source

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return self._camera_ids

    def pending_observations(self) -> int:
        """검출·로컬추적을 마치고 판정 루프가 아직 꺼내 가지 않은 관측 수."""
        with self._lock:
            return sum(len(q) for q in self._queued_observations.values())

    def reid_submission_status(self) -> dict[str, int]:
        return {"queued": self._reid_queue.qsize(), "dropped": self.reid_submission_dropped}

    def drained(self) -> bool:
        """진행 중인 검출 묶음이 없고 대기 중인 관측도 모두 소비됐다."""
        with self._lock:
            return self._in_flight == 0 and all(len(q) == 0 for q in self._queued_observations.values())

    def detect_once(self) -> int:
        """추론 대기열에서 한 묶음을 꺼내 검출·Local Tracker 를 돌리고 유계 대기열에 넣는다(판정 lock 밖)."""
        with self._detect_lock:
            with self._lock:
                self._in_flight += 1
            try:
                with self.timing.measure("frame_dequeue", lambda: self.people):
                    batch = [getattr(t, "frame", t) for t in self._tickets()]
                if not batch:
                    with self._lock:
                        self._stepped = True
                    return 0
                streams: dict[str, CameraObservation] = {}
                for frame in batch:
                    shape = getattr(frame.image, "shape", ())
                    if len(shape) >= 2:
                        streams[frame.camera_id] = CameraObservation(
                            frame.camera_id, image_size=(int(shape[1]), int(shape[0])))
                with self._lock:
                    if streams:
                        self.camera_streams = {**self.camera_streams, **streams}
                    self.stats.batches += 1
                    self.stats.frames += len(batch)
                try:
                    with self.timing.measure("detect_trt", lambda: self.people):
                        results = list(self._detector.detect(batch))
                        self.people = max((len(r.detections) for r in results), default=0)
                except Exception:  # noqa: BLE001 - 검출기 오류가 판정 루프를 멈추지 않는다(모듈 설명)
                    with self._lock:
                        self.stats.detector_errors += 1
                        self.stats.frames_lost_to_errors += len(batch)
                        err_count = self.stats.detector_errors
                        self._stepped = True
                    if err_count == 1 or err_count % 100 == 0:
                        log.exception("검출기 오류 %d회째: 이 묶음(%d장)은 관측 없이 버린다", err_count, len(batch))
                    return len(batch)
                if len(results) != len(batch):
                    with self._lock:
                        self.stats.mismatched += len(batch)
                        self._stepped = True
                    log.error("검출 결과 수(%d)가 넣은 프레임 수(%d)와 다르다: 묶음을 버린다", len(results), len(batch))
                    return len(batch)
                accepted: list[CameraDetections] = []
                mismatched = 0
                stale = 0
                for frame, result in zip(batch, results, strict=True):
                    got = (result.camera_id, result.stream_epoch, result.frame_sequence, result.estimated_capture_time_ms)
                    if got != _ids(frame):
                        mismatched += 1
                        continue
                    if not self._gate.admit(result.camera_id, result.estimated_capture_time_ms):
                        stale += 1
                        continue
                    accepted.append(result)
                    if self.observe_decoded is not None:
                        try:
                            self.observe_decoded(frame, result)
                        except Exception:
                            log.exception("배경 프레임 전달 실패(판정은 계속한다)")
                with self.timing.measure("local_track_2d", self.people):
                    observations = self._tracker.update_batch(accepted) if accepted else []
                avatar_f: dict[str, Frame] = {}
                avatar_o: dict[str, Observation2DFrame] = {}
                need_images = (self.avatar_enabled or self.reid_collector is not None) and bool(observations)
                images = {_ids(f): f for f in batch} if need_images else {}
                if self.avatar_enabled and observations:
                    for obs in observations:
                        key = (obs.camera_id, obs.stream_epoch, obs.frame_sequence, obs.estimated_capture_time_ms)
                        if key in images:
                            avatar_f[obs.camera_id] = images[key]
                            avatar_o[obs.camera_id] = obs
                with self._lock:
                    self.stats.mismatched += mismatched
                    self.stats.stale_results += stale
                    if avatar_f:
                        self.avatar_frames = {**self.avatar_frames, **avatar_f}
                        self.avatar_observations = {**self.avatar_observations, **avatar_o}
                    for obs in observations:
                        q = self._queued_observations.setdefault(obs.camera_id, deque())
                        if len(q) >= self._max_queued_per_camera:
                            q.popleft()
                            self.stats.dropped_observations += 1
                        q.append(obs)
                    self._stepped = True
                # 관측을 먼저 공개해 보조 크롭 준비가 현재 판정을 지연시키지 않게 한다.
                if self.reid_collector is not None and observations:
                    for obs in observations:
                        key = (obs.camera_id, obs.stream_epoch, obs.frame_sequence, obs.estimated_capture_time_ms)
                        frame_obj = images.get(key)
                        if frame_obj is not None:
                            try:
                                self._reid_queue.put_nowait((frame_obj, obs))
                            except queue.Full:
                                # 오래된 프레임 참조를 먼저 해제하고 최신 작업을 보존한다.
                                try:
                                    self._reid_queue.get_nowait()
                                    self._reid_queue.task_done()
                                except queue.Empty:
                                    pass
                                else:
                                    self.reid_submission_dropped += 1
                                self._reid_queue.put_nowait((frame_obj, obs))
                return len(batch)
            finally:
                with self._lock:
                    self._in_flight -= 1

    def _submit_reid(self) -> None:
        while not self._reid_stop.is_set():
            try:
                frame, obs = self._reid_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self.timing.measure("reid_submit", len(obs.detections)):
                    self.reid_collector.submit_observation(frame, obs)
            except Exception:
                log.debug("Re-ID 특징 수집 요청 실패(판정은 계속한다)", exc_info=True)
            finally:
                self._reid_queue.task_done()

    def poll(self, now_ms: int) -> list[Observation2DFrame]:
        if not self.async_mode:
            with self._lock:
                need_step = not self._stepped and all(len(q) == 0 for q in self._queued_observations.values())
            if need_step:
                self.detect_once()
        with self._lock:
            self._stepped = False
            out: list[Observation2DFrame] = []
            for q in self._queued_observations.values():
                out.extend(q)
                q.clear()
        if len(out) > 1:
            out.sort(key=lambda o: (
                o.estimated_capture_time_ms,
                self._camera_order.get(o.camera_id, len(self._camera_order)),
                o.frame_sequence,
            ))
        return out

    def camera_connected(self, camera_id: str) -> bool:
        return self._connected(camera_id)

    def provenance(self) -> dict[str, Any]:
        if self._provenance is not None:
            return dict(self._provenance)
        return {"source": self._source, "camera_ids": list(self._camera_ids)}

    def close(self) -> None:
        self._reid_stop.set()
        while True:
            try:
                self._reid_queue.get_nowait()
                self._reid_queue.task_done()
            except queue.Empty:
                break
        if self._reid_worker is not None:
            self._reid_worker.join(timeout=1.0)
        if self.reid_collector is not None:
            try:
                self.reid_collector.close()
            except Exception:  # noqa: BLE001
                pass
        if self._on_close is not None:
            self._on_close()


@dataclass(frozen=True)
class Perceived:
    """한 tick 의 인지 결과. payload 는 world_state payload(config_version 은 판정 루프가 그 tick 의 묶음으로 넣는다)."""

    t: int
    frame: WorldTrackFrame
    payload: dict[str, Any]
    t_latest_global: int
    lagging: tuple[str, ...] = ()  # 늦어서 이 tick 융합 시각 계산과 관측에서 뺀 카메라(FusionTime.lagging)


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
        avatar_writer: Any = None,
        observe_association: Callable[[AssociationFrame], None] | None = None,
        observe_frame: Callable[[Observation2DFrame], None] | None = None,
        feature_lookup: Any = None,
        roi_enabled: bool = True,
        roi_margin_m: float = 3.0,
        roi_min_people: int = 4,
    ) -> None:
        missing = [c for c in feed.camera_ids if c not in rig.cameras]
        if missing:
            raise ValueError(f"보정 {rig.calibration_version} 에 없는 입력 카메라: {missing}")
        self.rig = rig
        self.timing = StageTiming()
        self.people = 0
        self.roi_enabled = roi_enabled
        self.roi_margin_m = roi_margin_m
        self.roi_min_people = roi_min_people
        self.roi_zones = ()
        self.roi_filtered = {c: 0 for c in feed.camera_ids}
        self.feed = feed
        self.scene_version = scene_version
        self.time_config = time_config
        self.buffer = LocalTrackletBuffer()
        self.associator = CrossViewAssociator(rig)
        lookup = feature_lookup
        if lookup is None:
            collector = getattr(feed, "reid_collector", None)
            # Re-ID 가 실제로 켜졌을 때만 특징을 넘긴다. 꺼졌는데 넘기면 world_track 이 Re-ID 켜짐처럼 분할을 억제해
            # 꺼짐/켬 비교의 기하 기준선이 달라진다(S15P31A101-472)
            if collector is not None and getattr(collector, "active", False):
                lookup = getattr(collector, "lookup", None)
        self.manager = WorldTrackManager(
            WorldIdIssuer(session),
            world_config,
            input_provenance=input_provenance,
            feature_lookup=lookup,
        )
        self.contact = contact or ContactEstimator()
        self.avatar_writer = avatar_writer
        self._last_avatar_t: int | None = None
        self._avatar_calibration_rig: CameraRig | None = None
        self._avatar_calibration_doc: dict[str, Any] | None = None
        self.observe_association = observe_association
        self.observe_frame = observe_frame
        if isinstance(feed, DetectorFeed):
            feed.avatar_enabled = avatar_writer is not None
        self._lock = threading.Lock()  # 카메라 상태(health 가 다른 스레드에서 읽는다)
        self._last_capture: dict[str, int | None] = {c: None for c in feed.camera_ids}
        self._frames_since: dict[str, int] = {c: 0 for c in feed.camera_ids}
        self.dropped_frames = 0
        self.lagging_ticks: dict[str, int] = {c: 0 for c in feed.camera_ids}  # 늦어서 뺀 인지 tick 수(누계)
        # 마지막으로 인지 결과를 낸 tick 에서 늦어서 뺀 카메라. 새 tick 이 날 때까지 유지한다(판정의 감시 입력이 읽는다: 수신
        # 중이어도 융합에 쓰지 못한 카메라는 R7 의 정상 관측 카메라로 세지 않는다)
        self.lagging: tuple[str, ...] = ()
        self._excluded_cameras: frozenset[str] = frozenset()
        # 추적 계측(S15P31A101-413): 판정에 쓰지 않는 관찰자. 상태 파일 status.tracking_diagnostics 와 주기 요약 로그(촬영 시각 30 s)
        self.diagnostics = TrackingDiagnostics()

    @property
    def excluded_cameras(self) -> tuple[str, ...]:
        """DRIFTED·STREAM_CHANGED 등으로 새 보정 활성화(재세팅) 전까지 3D 융합에서 영구 제외한 카메라."""
        return tuple(sorted(self._excluded_cameras))

    def exclude_cameras(
        self,
        camera_ids: Sequence[str] | set[str] | frozenset[str],
        *,
        min_remaining: int = 2,
    ) -> tuple[str, ...]:
        """판정 lock 안. 지정한 카메라를 새 보정 활성화 전까지 3D 융합·관절 게이트·R7 정상 감시에서 제외한다.
        제외 후 남는 카메라가 min_remaining(기본 2대) 미만이면 제외하지 않고 보호된 카메라 목록을 돌려준다."""
        if isinstance(camera_ids, str):
            raise TypeError("camera_ids 는 카메라 ID 모음이어야 한다")
        valid = [c for c in sorted(set(camera_ids)) if c in self.rig.cameras]
        current = set(self._excluded_cameras)
        total = len(self.rig.cameras)
        newly_excluded: list[str] = []
        protected: list[str] = []
        for cid in valid:
            if cid in current:
                continue
            if total - (len(current) + 1) < min_remaining:
                protected.append(cid)
                continue
            current.add(cid)
            newly_excluded.append(cid)
        if newly_excluded:
            self._excluded_cameras = frozenset(current)
            log.warning(
                "틀어짐/스트림 변경 카메라를 3D 융합에서 제외한다(재세팅 전까지 복귀 안 함): %s",
                ", ".join(sorted(self._excluded_cameras)),
            )
        if protected:
            log.warning(
                "제외 후 남는 카메라가 %d대 미만이라 3D 융합 제외를 보류한다(감시 저하): %s",
                min_remaining,
                ", ".join(protected),
            )
        return tuple(protected)

    def replace_rig(self, rig: CameraRig, provenance: Mapping[str, Any]) -> None:
        """판정 lock 안의 tick 경계 전용. World Track·접지·입력 버퍼는 유지한다(-344)."""
        self.associator.replace_rig(rig)
        self.manager.input_provenance = dict(provenance)
        self.rig = rig
        self._excluded_cameras = frozenset()
        self._last_avatar_t = None
        self._avatar_calibration_rig = None
        self._avatar_calibration_doc = None

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

    def choose_t(self) -> FusionTime | None:
        """공통 시각과 늦어서 뺀 카메라(모듈 설명). 모르면 None."""
        clocks = self.camera_clocks()
        if self._excluded_cameras:
            active = tuple(c for c in clocks if c.camera_id not in self._excluded_cameras)
            if any(c.last_capture is not None for c in active):
                clocks = active
        return choose_fusion_time(clocks, self.time_config)

    def ingest(self, frames: Sequence[Observation2DFrame]) -> None:
        for f in frames:
            if f.camera_id not in self._last_capture:
                self.dropped_frames += 1
                continue
            self.buffer.add(f)
            if self.observe_frame is not None:
                try:
                    self.observe_frame(f)
                except Exception:
                    log.exception("후보 보정 관절 공급 실패(판정은 계속한다)")
            with self._lock:
                prev = self._last_capture[f.camera_id]
                if prev is None or f.estimated_capture_time_ms > prev:
                    self._last_capture[f.camera_id] = f.estimated_capture_time_ms
                self._frames_since[f.camera_id] += 1

    def step(self, now_ms: int, surfaces: SurfaceSet, sync_invalid: Sequence[str] = ()) -> Perceived | None:
        """입력을 받아 공통 시각 t 의 World Track·접지·world_state payload 를 만든다. 새 시각이 없으면 None."""
        with self.timing.measure("observation_poll", lambda: self.people):
            self.ingest(self.feed.poll(now_ms))
        fusion = self.choose_t()
        t_latest = self.t_latest_global()
        last = self.manager.last_t
        if fusion is None or t_latest is None or (last is not None and fusion.t <= last):
            self._observe(lambda d: d.observe_idle())
            return None
        t = fusion.t
        if fusion.lagging and fusion.lagging != self.lagging:
            log.info("늦은 카메라를 융합에서 뺀다(t_latest_global 보다 %d ms 넘게 늦음): %s",
                     self.time_config.fusion_max_lag_ms, ", ".join(fusion.lagging))
        self.lagging = fusion.lagging
        for c in fusion.lagging:
            self.lagging_ticks[c] += 1
        views, drops = self.buffer.views_at(t)
        self._observe(lambda d: (d.observe_tick(t, fusion.lagging), d.observe_views(views, drops)))
        self.people = max((sum(v.camera_id == c for v in views) for c in self.feed.camera_ids), default=0)
        # 혼잡할 때만 거른다(-574): 사람이 적으면 비용이 작고, 거르면 구역으로 다가오는 사람의 추적이 늦어
        # R5 접근 경보가 늦다(시나리오 시험 r1_enter_exit 등). 기준은 한 카메라에 보인 최대 인원
        if self.roi_enabled and self.people >= self.roi_min_people:
            from simai_ai.tracking.roi_filter import filter_views, zone_envelope

            polygons = zone_envelope(tuple(z.polygon_xy for z in self.roi_zones if z.is_active(t)))
            with self.timing.measure("roi_filter", self.people):
                views, counts = filter_views(views, self.rig, polygons, self.roi_margin_m)
            for camera_id, count in counts.items():
                self.roi_filtered[camera_id] += count
        excluded = tuple(sorted(set(sync_invalid) | self._excluded_cameras))
        with self.timing.measure("cross_view_2d_triangulation", self.people):
            assoc = self.associator.associate(
                t,
                views,
                exclude_cameras=excluded,
                prior_groups=self.manager.temporal_prior_groups(t, self.associator.config.temporal_prior_ttl_ms),
                timing=self.timing,
                timing_people=self.people,
            )
        self._observe(lambda d: d.observe_association(assoc))
        if self.observe_association is not None:
            try:
                self.observe_association(assoc)
            except Exception:
                log.exception("보정 감시 표본 공급 실패(판정은 계속한다)")
        self.buffer.prune(t)
        with self.timing.measure("world_track", self.people):
            frame = self.manager.update(assoc, self.rig, excluded)
        collector = getattr(self.feed, "reid_collector", None)
        if collector is not None and hasattr(collector, "update_sampling"):
            collector.update_sampling(frame)
        self._observe(lambda d: d.observe_world(frame, self.manager.diag))
        self._observe(lambda d: d.window_due(t) and log.info("%s", window_log_line(d.take_window(t))))
        with self.timing.measure("contact", self.people):
            contacts = self.contact.update(frame, surfaces)
        payload = world_state_payload(
            frame, t_latest, self.scene_version, contacts=contacts.contacts(), time_config=self.time_config
        )
        if self.avatar_writer is not None:
            try:
                self._share_avatar(frame, payload, t_latest)
            except Exception:  # 보조 출력 실패가 판정 경로에 전파되지 않는다
                log.warning("아바타 입력 공유 실패", exc_info=False)
        return Perceived(t=t, frame=frame, payload=payload, t_latest_global=t_latest, lagging=fusion.lagging)

    def _observe(self, fn: Callable[[TrackingDiagnostics], Any]) -> None:
        """계측 실패가 판정 경로에 전파되지 않는다."""
        try:
            fn(self.diagnostics)
        except Exception:  # noqa: BLE001
            log.warning("추적 계측 실패(판정은 계속한다)", exc_info=True)

    def _share_avatar(self, frame, payload, t_latest) -> None:
        period_s = float(getattr(self.avatar_writer, "period_s", DEFAULT_AVATAR_SHARE_PERIOD_S))
        period_ms = int(round(period_s * 1000))
        last_t = getattr(self, "_last_avatar_t", None)
        if last_t is not None and 0 <= t_latest - last_t < period_ms:
            return
        images = dict(getattr(self.feed, "avatar_frames", {}))
        observations = getattr(self.feed, "avatar_observations", {})
        tracks = []
        for snapshot, track in zip(frame.tracks, payload["tracks"], strict=True):
            per_camera = []
            for cid, local_id in snapshot.local_keys:
                obs, image = observations.get(cid), images.get(cid)
                if obs is None or image is None or abs(t_latest - image.estimated_capture_time) > 100:
                    continue
                for detection in obs.detections:
                    if detection.local_track_id == local_id:
                        d = detection.detection
                        per_camera.append({"camera_id": cid, "local_track_id": str(local_id),
                                           "bbox": list(d.bbox), "keypoints_2d": list(d.keypoints_2d),
                                           "joint_layout": "COCO17", "estimated_capture_time": obs.estimated_capture_time_ms,
                                           "frame_ref": dict(image.frame_ref)})
            avatar_hold = bool(
                getattr(snapshot, "avatar_update_hold", False)
                or track.get("identity_confidence") == "IDENTITY_UNCERTAIN"
            )
            tracks.append({
                **track,
                "per_camera": per_camera,
                "avatar_update_hold": avatar_hold,
                "hold_appearance_update": avatar_hold,
            })
        rig = self.rig
        calibration = getattr(self, "_avatar_calibration_doc", None)
        if calibration is None or getattr(self, "_avatar_calibration_rig", None) is not rig:
            cameras = [{"camera_id": cid, "K": cam.K.tolist(), "R": cam.R.tolist(), "t": cam.t.tolist(),
                        "image_size": list(cam.image_size),
                        "distortion": {"model": cam.distortion_model, "coeffs": list(cam.distortion_coeffs)}}
                       for cid, cam in rig.cameras.items()]
            calibration = {"calibration_version": rig.calibration_version, "provider_source": rig.provider_source,
                           "state": rig.state, "coordinate_frame": {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"},
                           "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA", "cameras": cameras}
            self._avatar_calibration_rig = rig
            self._avatar_calibration_doc = calibration
        provenance = dict(payload["input_provenance"])
        provenance["tracks"] = {"source": "TIER0_LIVE", "provider_version": "tier0-avatar-1"}
        offered = self.avatar_writer.offer({"world": {"t": t_latest, "calibration_version": rig.calibration_version,
                                                      "coordinate_frame": calibration["coordinate_frame"], "tracks": tracks,
                                                      "input_provenance": provenance}, "calibration": calibration}, images)
        if offered is not False:
            self._last_avatar_t = t_latest
