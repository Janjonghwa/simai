"""운영 중 모은 2D 관절·로컬 트랙으로 백그라운드 후보 보정을 만드는 실행기 (명세 3.2, 3.12, 4.5, S15P31A101-345).

- 관제는 직전 보정으로 바로 시작하고, Tier 0 perception(`Observation2DFrame`)이 이미 만든 카메라별 2D 관절·로컬 트랙을
  고리 버퍼(`LiveJointBuffer`)에 모은다. 따로 녹화하거나 관절을 다시 추출하지 않는다.
- 버퍼는 시간 길이(`max_duration_ms`), 카메라당 프레임 수(`max_frames_per_camera`), 전체 표본 수(`max_total_samples`),
  추정 메모리 바이트(`max_bytes`) 상한을 두어 메모리가 끝없이 늘지 않게 한다.
- 특정 카메라가 틀어졌다는 신호(`DRIFTED`, -346)를 받으면 `reset_camera(camera_id)` 로 그 카메라의 옛 관측만 비운다.
- 후보 보정 실행기(`LiveCandidateCalibrationRunner`)는 Tier 0 실시간 판정 스레드를 막지 않도록 별도 `spawn` 프로세스,
  낮은 OS 우선순위(`BELOW_NORMAL_PRIORITY_CLASS` / `nice`), 단일 스레드 BLAS 설정, 시간 상한(`timeout_s`),
  자원 우선순위 게이트(`AdmissionGate` / `can_run`)를 둔다.
- 주기마다 버퍼 스냅샷을 떠서 겹치지 않는 fit / holdout 구간으로 나누고, 최소 데이터 기준(`SessionDataCriteria`)을 확인한다.
  먼저 회전만 다시 맞추는 빠른 길(`realign_rotation`)을 시험하고, 부족하면 전체 회차 보정(`adjust_session`, World 유지)을
  돌린다. 같은 holdout 구간에서 현재 활성 보정보다 의미 있게 좋아진(`CandidateImprovementCriteria`) 후보만
  `propose_calibration(document, report)` 콜백으로 넘긴다.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
import copy
from dataclasses import asdict, dataclass, field, replace
import datetime as dt
import math
import multiprocessing as mp
import os
import queue as queue_mod
import threading
import time
from typing import Any

import numpy as np
import psutil
from scipy.spatial.transform import Rotation

from simai_ai.calibration.bundle import PersonObservation
from simai_ai.calibration.criteria import CalibrationEvidence, ScaleBasis, ScaleEstimate, judge_calibration
from simai_ai.calibration.document import CameraCalibration, build_calibration_doc
from simai_ai.calibration.extrinsics.session import (
    DEFAULT_DATA_CRITERIA,
    PROVIDER_VERSION,
    SessionDataCriteria,
    adjust_session,
    camera_changes,
    check_data_sufficiency,
    evaluate_calibration,
    session_rig,
)
from simai_ai.calibration.extrinsics.session_bootstrap import rotation_candidates
from simai_ai.calibration.extrinsics.session_tracks import (
    SessionTracks,
    associate_session,
    bundle_observations,
    split_session_tracks,
)
from simai_ai.calibration.recalibration import (
    RecalibrationConfig,
    realign_rotation,
)

DEFAULT_RECALIBRATION_CONFIG = RecalibrationConfig()
from simai_ai.geometry.interpolation import KeypointSample, interpolate_camera
from simai_ai.geometry.rig import CameraRig
from simai_ai.gpu.admission import AdmissionGate
from simai_ai.providers.contract import check_calibration
from simai_ai.tracking.cross_view import LocalView
from simai_ai.tracking.local_config import tracker_config
from simai_ai.tracking.local_tracker import (
    CameraDetections,
    CameraLocalTracker,
    Observation2DFrame,
    PoseDetection,
)
from simai_ai.tracking.time_model import require_ms

LIVE_CANDIDATE_REPORT_FORMAT = "simai.live_candidate_calibration"
LIVE_CANDIDATE_REPORT_VERSION = "0.1.0"

STATUS_PROPOSED = "PROPOSED"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
STATUS_NOT_CONVERGED = "NOT_CONVERGED"
STATUS_NO_MEANINGFUL_IMPROVEMENT = "NO_MEANINGFUL_IMPROVEMENT"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_DEFERRED = "DEFERRED"
STATUS_FAILED = "FAILED"

MODE_FAST_ROTATION = "fast_rotation"
MODE_FULL_SESSION = "full_session"

_BYTES_PER_SAMPLE_ESTIMATE = 640
_BYTES_PER_FRAME_OVERHEAD = 192


@dataclass(frozen=True)
class LiveBufferConfig:
    """운영용 2D 관절·로컬 트랙 고리 버퍼 설정."""

    max_duration_ms: int = 300_000
    """최근 보관 시간 상한 (기본 5분 = 300,000 ms). 1단계 곡선 측정에서 2분에 연관 포화, 5분에 기하·높이 검산이 안정됨."""
    max_frames_per_camera: int = 3_600
    """카메라당 최대 보관 프레임 수."""
    max_total_samples: int = 20_000
    """전체 카메라 합산 최대 보관 KeypointSample 수."""
    max_bytes: int = 32 * 1024 * 1024
    """버퍼 추정 메모리 상한 (기본 32 MiB)."""
    sample_period_ms: int = 300
    """공통 시각 보간 주기 (session 보정과 같은 300 ms)."""
    gap_split_ms: int = 2_000
    """같은 카메라 연속 프레임 간격이 이 값을 넘으면 새 연속 구간으로 나누고 보간을 끊는다."""

    def __post_init__(self) -> None:
        if self.max_duration_ms < 1_000:
            raise ValueError("max_duration_ms 는 1000 이상이어야 한다")
        if self.max_frames_per_camera < 1:
            raise ValueError("max_frames_per_camera 는 1 이상이어야 한다")
        if self.max_total_samples < 1:
            raise ValueError("max_total_samples 는 1 이상이어야 한다")
        if self.max_bytes < 4_096:
            raise ValueError("max_bytes 는 4096 이상이어야 한다")
        if self.sample_period_ms < 1:
            raise ValueError("sample_period_ms 는 1 이상이어야 한다")
        if self.gap_split_ms < self.sample_period_ms:
            raise ValueError("gap_split_ms 는 sample_period_ms 이상이어야 한다")


DEFAULT_LIVE_BUFFER_CONFIG = LiveBufferConfig()


@dataclass(frozen=True)
class _BufferedFrame:
    camera_id: str
    stream_epoch: int
    frame_sequence: int
    estimated_capture_time_ms: int
    segment_id: int
    untracked_count: int
    samples: tuple[tuple[str, KeypointSample], ...]
    approx_bytes: int


class LiveJointBuffer:
    """Tier 0 perception 출력을 최근 N분 동안 카메라별로 담는 스레드 안전 고리 버퍼."""

    def __init__(
        self,
        config: LiveBufferConfig = DEFAULT_LIVE_BUFFER_CONFIG,
        *,
        backend_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._config = config
        self._backend_factory = backend_factory
        self._lock = threading.Lock()
        self._frames: dict[str, deque[_BufferedFrame]] = defaultdict(deque)
        self._trackers: dict[str, CameraLocalTracker] = {}
        self._segment_ids: dict[str, int] = defaultdict(int)
        self._last_meta: dict[str, tuple[int, int, int]] = {}  # cid -> (epoch, seq, capture_ms)
        self._total_samples: int = 0
        self._total_bytes: int = 0
        self._newest_time_ms: int | None = None

    @property
    def config(self) -> LiveBufferConfig:
        return self._config

    def reset_camera(self, camera_id: str) -> int:
        """특정 카메라의 버퍼 관측과 로컬 추적 상태를 비운다 (DRIFTED 신호 수신 시 호출)."""
        if not camera_id or not isinstance(camera_id, str):
            raise ValueError("camera_id 는 비어 있지 않은 문자열이어야 한다")
        with self._lock:
            q = self._frames.pop(camera_id, None)
            removed_frames = len(q) if q is not None else 0
            if q is not None:
                for bf in q:
                    self._total_samples -= len(bf.samples)
                    self._total_bytes -= bf.approx_bytes
            self._trackers.pop(camera_id, None)
            self._last_meta.pop(camera_id, None)
            self._segment_ids[camera_id] = self._segment_ids.get(camera_id, 0) + 1
            self._recompute_newest_locked()
            return removed_frames

    def clear(self) -> None:
        """모든 카메라의 버퍼를 비운다."""
        with self._lock:
            self._frames.clear()
            self._trackers.clear()
            self._last_meta.clear()
            self._segment_ids.clear()
            self._total_samples = 0
            self._total_bytes = 0
            self._newest_time_ms = None

    def append_frame(self, frame: Observation2DFrame) -> bool:
        """Tier 0 `CameraPerceptionPipeline.process_frame()` 이 만든 `Observation2DFrame` 을 버퍼에 넣는다."""
        if not isinstance(frame, Observation2DFrame):
            raise TypeError("frame 은 Observation2DFrame 이어야 한다")
        cid = frame.camera_id
        t_ms = int(frame.estimated_capture_time_ms)
        epoch = int(frame.stream_epoch)
        seq = int(frame.frame_sequence)
        for det in frame.detections:
            if len(det.detection.keypoints_2d) != 17:
                raise ValueError(f"{cid}: COCO17 은 관절 17개여야 한다")

        with self._lock:
            prev = self._last_meta.get(cid)
            if prev is not None:
                prev_epoch, prev_seq, prev_t = prev
                if epoch == prev_epoch and seq <= prev_seq:
                    return False
                if epoch != prev_epoch or t_ms < prev_t or (t_ms - prev_t) > self._config.gap_split_ms:
                    self._segment_ids[cid] += 1
            seg_id = self._segment_ids[cid]
            self._last_meta[cid] = (epoch, seq, t_ms)

            samples: list[tuple[str, KeypointSample]] = []
            for det in frame.detections:
                tid = f"s{seg_id}_{det.local_track_id}"
                sample = KeypointSample(
                    cid,
                    t_ms,
                    frame.timestamp_source,
                    frame.timestamp_uncertainty_ms,
                    det.detection.keypoints_2d,
                    {"stream_epoch": epoch, "frame_sequence": seq},
                )
                samples.append((tid, sample))

            approx_bytes = _BYTES_PER_FRAME_OVERHEAD + len(samples) * _BYTES_PER_SAMPLE_ESTIMATE
            bf = _BufferedFrame(
                camera_id=cid,
                stream_epoch=epoch,
                frame_sequence=seq,
                estimated_capture_time_ms=t_ms,
                segment_id=seg_id,
                untracked_count=int(frame.untracked_count),
                samples=tuple(samples),
                approx_bytes=approx_bytes,
            )
            self._frames[cid].append(bf)
            self._total_samples += len(samples)
            self._total_bytes += approx_bytes
            if self._newest_time_ms is None or t_ms > self._newest_time_ms:
                self._newest_time_ms = t_ms
            self._enforce_limits_locked()
            return True

    def append_detection_row(self, row: Mapping[str, Any]) -> bool:
        """원시 검출 행(`keypoints.jsonl` 행 형식)을 카메라별 `CameraLocalTracker` 로 추적해 버퍼에 넣는다."""
        cid = str(row["camera_id"])
        epoch = int(row["stream_epoch"])
        seq = int(row["frame_sequence"])
        t_ms = int(row["estimated_capture_time_ms"])
        dets = tuple(
            PoseDetection(
                tuple(d["bbox"]),
                float(d["score"]),
                tuple(tuple(k) if k is not None else None for k in d["keypoints_2d"]),
            )
            for d in row.get("detections", ())
        )
        if any(len(d.keypoints_2d) != 17 for d in dets):
            raise ValueError(f"{cid}: session COCO17은 17개 관절이어야 한다")

        with self._lock:
            prev = self._last_meta.get(cid)
            need_new_tracker = cid not in self._trackers
            if prev is not None:
                prev_epoch, prev_seq, prev_t = prev
                if epoch == prev_epoch and seq <= prev_seq:
                    return False
                if epoch != prev_epoch or t_ms < prev_t or (t_ms - prev_t) > self._config.gap_split_ms:
                    need_new_tracker = True
            if need_new_tracker:
                kwargs = {} if self._backend_factory is None else {"backend_factory": self._backend_factory}
                self._trackers[cid] = CameraLocalTracker(cid, tracker_config(), **kwargs)
            tracker = self._trackers[cid]

        obs_frame = tracker.update(
            CameraDetections(
                cid,
                epoch,
                seq,
                t_ms,
                str(row["timestamp_source"]),
                row.get("timestamp_uncertainty_ms"),
                dets,
            )
        )
        if obs_frame is None:
            return False
        return self.append_frame(obs_frame)

    def _pop_oldest_camera_frame_locked(self, cid: str) -> None:
        q = self._frames.get(cid)
        if not q:
            return
        old = q.popleft()
        self._total_samples -= len(old.samples)
        self._total_bytes -= old.approx_bytes
        if not q:
            self._frames.pop(cid, None)

    def _recompute_newest_locked(self) -> None:
        newest: int | None = None
        for q in self._frames.values():
            if q:
                t = q[-1].estimated_capture_time_ms
                if newest is None or t > newest:
                    newest = t
        self._newest_time_ms = newest

    def _enforce_limits_locked(self) -> None:
        if self._newest_time_ms is not None:
            cutoff = self._newest_time_ms - self._config.max_duration_ms
            for cid in list(self._frames):
                q = self._frames[cid]
                while q and q[0].estimated_capture_time_ms < cutoff:
                    self._pop_oldest_camera_frame_locked(cid)

        for cid in list(self._frames):
            q = self._frames.get(cid)
            while q and len(q) > self._config.max_frames_per_camera:
                self._pop_oldest_camera_frame_locked(cid)

        while (
            self._total_samples > self._config.max_total_samples
            or self._total_bytes > self._config.max_bytes
        ) and self._frames:
            oldest_cid = min(
                self._frames,
                key=lambda c: self._frames[c][0].estimated_capture_time_ms,
            )
            self._pop_oldest_camera_frame_locked(oldest_cid)

        self._recompute_newest_locked()

    def summary(self) -> dict[str, Any]:
        """현재 버퍼에 담긴 프레임·표본·메모리·시간 범위 요약."""
        with self._lock:
            return self._summary_locked()

    def _summary_locked(self) -> dict[str, Any]:
        cam_counts = {cid: len(q) for cid, q in sorted(self._frames.items()) if q}
        all_times = [bf.estimated_capture_time_ms for q in self._frames.values() for bf in q]
        sample_times = [
            s.estimated_capture_time
            for q in self._frames.values()
            for bf in q
            for _, s in bf.samples
        ]
        frame_windows = _contiguous_windows(all_times, self._config.gap_split_ms)
        windows = [
            [w_lo, w_hi]
            for w_lo, w_hi in frame_windows
            if any(w_lo <= t_s <= w_hi for t_s in sample_times)
        ]
        if all_times:
            lo, hi = min(all_times), max(all_times)
            span: list[int] | None = [int(lo), int(hi)]
            dur_s = round((hi - lo) / 1000.0, 3)
        else:
            span = None
            dur_s = 0.0
        return {
            "window_span_ms": span,
            "duration_s": dur_s,
            "camera_frame_counts": cam_counts,
            "total_frames": sum(cam_counts.values()),
            "total_samples": int(self._total_samples),
            "approx_bytes": int(self._total_bytes),
            "max_bytes": int(self._config.max_bytes),
            "max_duration_ms": int(self._config.max_duration_ms),
            "max_frames_per_camera": int(self._config.max_frames_per_camera),
            "max_total_samples": int(self._config.max_total_samples),
            "segment_count": len(windows),
            "sample_windows_ms": windows,
        }

    def to_session_tracks(self, *, sample_period_ms: int | None = None) -> SessionTracks | None:
        """버퍼 스냅샷을 공통 시각 보간된 `SessionTracks` 로 변환한다. 추적된 표본이 없으면 None."""
        period = int(sample_period_ms if sample_period_ms is not None else self._config.sample_period_ms)
        if period < 1:
            raise ValueError("sample_period_ms 는 1 이상이어야 한다")
        with self._lock:
            frames_snapshot = [bf for q in self._frames.values() for bf in q]
            gap_split_ms = self._config.gap_split_ms

        samples: dict[tuple[str, str], list[KeypointSample]] = defaultdict(list)
        untracked = 0
        for bf in frames_snapshot:
            untracked += bf.untracked_count
            for tid, sample in bf.samples:
                samples[(bf.camera_id, tid)].append(sample)
        if not samples:
            return None

        for key in samples:
            samples[key].sort(key=lambda s: s.estimated_capture_time)
        times = {k: [s.estimated_capture_time for s in seq] for k, seq in samples.items()}
        all_ts = [t_s for ts in times.values() for t_s in ts]
        all_frame_ts = [bf.estimated_capture_time_ms for bf in frames_snapshot]
        frame_windows = _contiguous_windows(all_frame_ts, gap_split_ms)
        tick_ranges: list[tuple[int, int]] = []
        active_windows: list[list[int]] = []
        for w_lo, w_hi in frame_windows:
            in_w = [t_s for t_s in all_ts if w_lo <= t_s <= w_hi]
            if in_w:
                tick_ranges.append((min(in_w), max(in_w)))
                active_windows.append([int(w_lo), int(w_hi)])
        if not tick_ranges:
            tick_ranges = [(min(all_ts), max(all_ts))]

        ticks: list[tuple[int, tuple[LocalView, ...]]] = []
        drops: dict[str, int] = defaultdict(int)
        for r_lo, r_hi in tick_ranges:
            for t in range(r_lo, r_hi + 1, period):
                views: list[LocalView] = []
                for key, seq in samples.items():
                    idx = bisect_left(times[key], t)
                    view, rejected = interpolate_camera(seq[max(0, idx - 1) : idx + 1], t)
                    for d in rejected:
                        drops[str(d.reason)] += 1
                    if view is not None:
                        views.append(LocalView(*key, view))
                ticks.append((t, tuple(views)))

        rec: dict[str, Any] = {
            "local_tracks": len(samples),
            "untracked_detections": untracked,
            "ticks": len(ticks),
            "interpolation_drops": dict(drops),
            "sample_period_ms": period,
            "tracker": "live_buffer",
            "joint_source": "observed",
        }
        if active_windows:
            rec["sample_windows_ms"] = active_windows
        return SessionTracks(tuple(ticks), rec)


def _contiguous_windows(timestamps_ms: Sequence[int], gap_split_ms: int) -> list[list[int]]:
    if not timestamps_ms:
        return []
    ordered = sorted(set(int(t) for t in timestamps_ms))
    windows: list[list[int]] = []
    start = prev = ordered[0]
    for t in ordered[1:]:
        if t - prev > gap_split_ms:
            windows.append([start, prev])
            start = t
        prev = t
    windows.append([start, prev])
    return windows


@dataclass(frozen=True)
class CandidateImprovementCriteria:
    """현재 활성 보정 대비 후보 보정의 '의미 있는 개선' 판정 기준 (1단계 곡선 측정 근거).

    1단계 10/29 녹화 측정 결과:
    - 보정 전(`cal-walkshm`) holdout: 최저 카메라(cam_D) 쌍 통과율 평균 12.42%, 3+ 융합률 19.10%, 4대 융합률 0.00%,
      전체 재투영 오차 중앙값 3.492 px / p95 12.983 px (D 제외 3대 위주 13,344 관측).
    - 2분~전체 수렴 후 holdout: 최저 카메라(cam_D) 쌍 통과율 평균 51.4~51.9%(+39%p), 3+ 융합률 74.1~76.2%(+55%p),
      4대 융합률 34.3~36.8%(+35%p), 재투영 오차 중앙값 3.91~4.08 px / p95 14.9~15.3 px (4대 융합 20,000+ 관측).
    - 포화 구간(2분~전체)의 창 선택 흔들림(noise floor)은 4대 융합률 ±1.5%p, 쌍 통과율 ±0.5%p, 재투영 중앙값 ±0.15 px.
    따라서 흔들림의 2배 이상인 +3.0%p(4대 융합 + 최저 카메라 쌍 통과 동시 개선) 또는 +5.0%p(단일 지표 개선),
    또는 융합 감소 없이 재투영 중앙값 0.25 px 이상 감소를 의미 있는 개선 기준으로 삼는다.
    """

    min_fusion_4_gain: float = 0.03
    """Gate A: holdout 4대 융합률 최소 상승폭 (+3.0%p)."""
    min_worst_pair_pass_gain: float = 0.03
    """Gate A 동반 조건: 최저 카메라 쌍 통과율 평균 최소 상승폭 (+3.0%p)."""
    min_single_camera_pair_gain: float = 0.05
    """Gate B1: 최저 카메라 쌍 통과율 평균 단독 개선 최소폭 (+5.0%p)."""
    min_fusion_3plus_gain: float = 0.05
    """Gate B2: holdout 3대 이상 융합률 최소 상승폭 (+5.0%p)."""
    min_reproj_median_reduction_px: float = 0.25
    """Gate C: 융합률 저하 없이 전체 재투영 오차 중앙값이 줄어드는 최소폭 (0.25 px)."""
    max_fusion_3plus_drop: float = 0.02
    """안전 게이트: holdout 3+ 융합률 허용 최대 하락폭 (2.0%p)."""
    max_fusion_4_drop: float = 0.02
    """안전 게이트: holdout 4대 융합률 허용 최대 하락폭 (2.0%p)."""
    max_worst_pair_pass_drop: float = 0.02
    """안전 게이트: 최저 카메라 쌍 통과율 평균 허용 최대 하락폭 (2.0%p)."""
    max_reproj_median_increase_px: float = 1.0
    """안전 게이트: 새 카메라가 융합될 때 전체 재투영 중앙값 허용 최대 증가폭 (1.0 px)."""
    max_reproj_p95_increase_px: float = 3.5
    """안전 게이트: 새 카메라가 융합될 때 전체 재투영 p95 허용 최대 증가폭 (3.5 px)."""
    max_holdout_reproj_median_px: float = 6.0
    """안전 게이트: 후보 holdout 전체 재투영 중앙값 절대 상한 (6.0 px)."""
    max_holdout_reproj_p95_px: float = 20.0
    """안전 게이트: 후보 holdout 전체 재투영 p95 절대 상한 (20.0 px)."""
    max_anchor_residual_cm: float = 5.0
    """안전 게이트: World 정렬 기준 카메라 중심 잔차 최대 허용치 (5.0 cm)."""


DEFAULT_IMPROVEMENT_CRITERIA = CandidateImprovementCriteria()


@dataclass(frozen=True)
class LiveCandidateConfig:
    """백그라운드 후보 보정 실행기 설정."""

    interval_ms: int = 60_000
    """주기적 실행 간격 (기본 60초)."""
    timeout_s: float = 180.0
    """백그라운드 프로세스 계산 시간 상한 (초)."""
    use_subprocess: bool = True
    """별도 spawn 프로세스에서 낮은 우선순위로 실행할지 여부."""
    worker_nice: int = 10
    """POSIX nice 증가분 (Windows 에서는 BELOW_NORMAL_PRIORITY_CLASS 로 매핑)."""
    rounds: int = 3
    """회차 보정(`adjust_session`) 교대 연관/조정 라운드 수 (2 또는 3)."""
    block_ticks: int = 10
    """단일 연속 버퍼일 때 fit/holdout 교대 블록 크기 (기본 10시각 = 3초)."""
    try_fast_rotation_first: bool = True
    """회전만 다시 맞추는 `realign_rotation` 빠른 길을 먼저 시도할지 여부."""
    fast_pair_pass_threshold: float = 0.48
    """빠른 길 대상 후보로 볼 카메라의 fit 쌍 통과율 평균 임계값."""
    fast_min_rotation_deg: float = 0.15
    """빠른 길이 의미 있는 회전 보정으로 채택할 최소 회전각(도)."""
    fast_max_holdout_median_px: float = 3.5
    """빠른 길 채택 시 대상 카메라의 holdout 재투영 오차 중앙값 상한 (렌즈/위치 오차가 남았으면 전체 session 으로 넘김)."""
    data_criteria: SessionDataCriteria = DEFAULT_DATA_CRITERIA
    improvement_criteria: CandidateImprovementCriteria = DEFAULT_IMPROVEMENT_CRITERIA
    recalibration_config: RecalibrationConfig = DEFAULT_RECALIBRATION_CONFIG


DEFAULT_LIVE_CANDIDATE_CONFIG = LiveCandidateConfig()


@dataclass(frozen=True)
class CandidateRunResult:
    """후보 보정 1회 실행 결과."""

    status: str
    proposed: bool
    mode: str | None
    calibration: Mapping[str, Any] | None
    report: dict[str, Any]


def summarize_holdout_metrics(eval_metrics: Mapping[str, Any]) -> dict[str, Any]:
    """`evaluate_calibration` 결과를 카메라별 쌍 통과율·융합률·재투영 오차 요약으로 정리한다."""
    pairs = eval_metrics.get("pairs") or {}
    cam_pairs: dict[str, list[float]] = defaultdict(list)
    cam_pairs_strict: dict[str, list[float]] = defaultdict(list)
    pair_ratios: dict[str, float] = {}
    for k, v in sorted(pairs.items()):
        ratio = float(v.get("pass_ratio", 0.0))
        strict_ratio = float(v.get("pass_strict_ratio", 0.0))
        pair_ratios[k] = round(ratio, 4)
        parts = k.split("/")
        if len(parts) == 2:
            for cid in parts:
                cam_pairs[cid].append(ratio)
                cam_pairs_strict[cid].append(strict_ratio)
    per_cam_mean = {cid: round(float(np.mean(vals)), 4) for cid, vals in sorted(cam_pairs.items()) if vals}
    per_cam_strict_mean = {
        cid: round(float(np.mean(vals)), 4) for cid, vals in sorted(cam_pairs_strict.items()) if vals
    }
    worst_cam = min(per_cam_mean, key=per_cam_mean.get) if per_cam_mean else None
    worst_mean = per_cam_mean[worst_cam] if worst_cam is not None else 0.0
    d_pairs = {k: round(float(v.get("pass_ratio", 0.0)), 4) for k, v in sorted(pairs.items()) if "cam_D" in k}
    d_mean = round(float(np.mean(list(d_pairs.values()))), 4) if d_pairs else None
    reproj = eval_metrics.get("reprojection_error_px") or {}
    overall = reproj.get("overall") or {}
    return {
        "pair_pass_ratio": pair_ratios,
        "per_camera_pair_pass_mean": per_cam_mean,
        "per_camera_pair_strict_mean": per_cam_strict_mean,
        "worst_camera_id": worst_cam,
        "worst_camera_pair_pass_mean": round(float(worst_mean), 4),
        "d_pair_pass_ratio": d_pairs,
        "d_pair_pass_mean": d_mean,
        "fusion_3plus_ratio": round(float(eval_metrics.get("fusion_3plus_ratio") or 0.0), 4),
        "fusion_4_ratio": round(float(eval_metrics.get("fusion_4_ratio") or 0.0), 4),
        "view_fusion_3plus_ratio": round(float(eval_metrics.get("view_fusion_3plus_ratio") or 0.0), 4),
        "view_fusion_4_ratio": round(float(eval_metrics.get("view_fusion_4_ratio") or 0.0), 4),
        "largest_cluster_3plus_4view_ratio": round(
            float(eval_metrics.get("largest_cluster_3plus_4view_ratio") or 0.0), 4
        ),
        "largest_cluster_4_4view_ratio": round(
            float(eval_metrics.get("largest_cluster_4_4view_ratio") or 0.0), 4
        ),
        "reproj_overall_median_px": overall.get("median"),
        "reproj_overall_p95_px": overall.get("p95"),
        "reproj_overall_observations": int(overall.get("observations") or 0),
        "reproj_by_camera": {
            cid: {
                "median": stats.get("median"),
                "p95": stats.get("p95"),
                "observations": int(stats.get("observations") or 0),
            }
            for cid, stats in sorted(reproj.items())
            if cid != "overall" and isinstance(stats, Mapping)
        },
    }


def evaluate_improvement(
    before_summary: Mapping[str, Any],
    after_summary: Mapping[str, Any],
    *,
    world_alignment: Mapping[str, Any] | None,
    criteria: CandidateImprovementCriteria = DEFAULT_IMPROVEMENT_CRITERIA,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """현재 활성 보정(`before_summary`)과 후보 보정(`after_summary`)의 같은 holdout 지표를 비교해 채택 여부를 판정한다."""
    before_cids = set((before_summary.get("per_camera_pair_pass_mean") or {}).keys())
    after_cids = set((after_summary.get("per_camera_pair_pass_mean") or {}).keys())
    all_cids = sorted(before_cids | after_cids)
    per_cam_delta = {
        cid: round(
            float((after_summary.get("per_camera_pair_pass_mean") or {}).get(cid, 0.0))
            - float((before_summary.get("per_camera_pair_pass_mean") or {}).get(cid, 0.0)),
            4,
        )
        for cid in all_cids
    }
    # 보정 전 가장 약했던 카메라가 보정 뒤 얼마나 개선되었는지도 함께 본다.
    target_worst_cid = before_summary.get("worst_camera_id")
    if target_worst_cid and target_worst_cid in per_cam_delta:
        target_cam_gain = per_cam_delta[target_worst_cid]
    else:
        target_cam_gain = max(per_cam_delta.values(), default=0.0)

    delta_f3 = round(
        float(after_summary.get("fusion_3plus_ratio") or 0.0)
        - float(before_summary.get("fusion_3plus_ratio") or 0.0),
        4,
    )
    delta_f4 = round(
        float(after_summary.get("fusion_4_ratio") or 0.0)
        - float(before_summary.get("fusion_4_ratio") or 0.0),
        4,
    )
    delta_worst = round(
        float(after_summary.get("worst_camera_pair_pass_mean") or 0.0)
        - float(before_summary.get("worst_camera_pair_pass_mean") or 0.0),
        4,
    )
    eff_pair_gain = round(max(delta_worst, target_cam_gain), 4)

    med_before = before_summary.get("reproj_overall_median_px")
    med_after = after_summary.get("reproj_overall_median_px")
    p95_before = before_summary.get("reproj_overall_p95_px")
    p95_after = after_summary.get("reproj_overall_p95_px")
    delta_med = (
        round(float(med_after) - float(med_before), 3)
        if med_before is not None and med_after is not None
        else None
    )
    delta_p95 = (
        round(float(p95_after) - float(p95_before), 3)
        if p95_before is not None and p95_after is not None
        else None
    )
    obs_before = int(before_summary.get("reproj_overall_observations") or 0)
    obs_after = int(after_summary.get("reproj_overall_observations") or 0)

    delta = {
        "fusion_3plus_ratio": delta_f3,
        "fusion_4_ratio": delta_f4,
        "worst_camera_pair_pass_mean": delta_worst,
        "target_weak_camera_pair_gain": round(float(target_cam_gain), 4),
        "per_camera_pair_pass_mean": per_cam_delta,
        "reproj_overall_median_px": delta_med,
        "reproj_overall_p95_px": delta_p95,
        "reproj_observations": obs_after - obs_before,
    }

    rejected: list[str] = []
    if not world_alignment or not world_alignment.get("usable"):
        rejected.append("World 정렬 결과가 사용 불가(usable=False)")
    else:
        max_res = world_alignment.get("anchor_center_residual_max_cm")
        if max_res is not None and float(max_res) > criteria.max_anchor_residual_cm:
            rejected.append(
                f"World 기준 카메라 중심 잔차 최대값 {float(max_res):.2f} cm > {criteria.max_anchor_residual_cm:.2f} cm"
            )

    if delta_f3 < -criteria.max_fusion_3plus_drop:
        rejected.append(f"holdout 3+ 융합률 하락 {delta_f3 * 100:.2f}%p < -{criteria.max_fusion_3plus_drop * 100:.2f}%p")
    if delta_f4 < -criteria.max_fusion_4_drop:
        rejected.append(f"holdout 4대 융합률 하락 {delta_f4 * 100:.2f}%p < -{criteria.max_fusion_4_drop * 100:.2f}%p")
    if delta_worst < -criteria.max_worst_pair_pass_drop:
        rejected.append(
            f"holdout 최저 카메라 쌍 통과율 하락 {delta_worst * 100:.2f}%p < -{criteria.max_worst_pair_pass_drop * 100:.2f}%p"
        )

    if med_after is None or p95_after is None:
        rejected.append("holdout 재투영 오차 관측 없음")
    else:
        if float(med_after) > criteria.max_holdout_reproj_median_px:
            rejected.append(
                f"holdout 재투영 중앙값 {float(med_after):.3f} px > {criteria.max_holdout_reproj_median_px:.3f} px"
            )
        if float(p95_after) > criteria.max_holdout_reproj_p95_px:
            rejected.append(
                f"holdout 재투영 p95 {float(p95_after):.3f} px > {criteria.max_holdout_reproj_p95_px:.3f} px"
            )
        if delta_med is not None and delta_med > criteria.max_reproj_median_increase_px:
            rejected.append(
                f"holdout 재투영 중앙값 증가 +{delta_med:.3f} px > +{criteria.max_reproj_median_increase_px:.3f} px"
            )
        if delta_p95 is not None and delta_p95 > criteria.max_reproj_p95_increase_px:
            rejected.append(
                f"holdout 재투영 p95 증가 +{delta_p95:.3f} px > +{criteria.max_reproj_p95_increase_px:.3f} px"
            )

    triggered: list[str] = []
    if delta_f4 >= criteria.min_fusion_4_gain and eff_pair_gain >= criteria.min_worst_pair_pass_gain:
        triggered.append(
            f"GATE_A_FUSION4_AND_PAIR_GAIN(fusion_4=+{delta_f4 * 100:.1f}%p, pair=+{eff_pair_gain * 100:.1f}%p)"
        )
    if eff_pair_gain >= criteria.min_single_camera_pair_gain:
        triggered.append(f"GATE_B1_WORST_CAMERA_PAIR_GAIN(+{eff_pair_gain * 100:.1f}%p)")
    if delta_f3 >= criteria.min_fusion_3plus_gain:
        triggered.append(f"GATE_B2_FUSION_3PLUS_GAIN(+{delta_f3 * 100:.1f}%p)")
    if (
        delta_med is not None
        and delta_med <= -criteria.min_reproj_median_reduction_px
        and delta_f4 >= 0.0
        and delta_worst >= 0.0
    ):
        triggered.append(f"GATE_C_REPROJ_MEDIAN_REDUCTION({delta_med:.3f}px)")

    if not triggered:
        rejected.append(
            "개선 폭 미달 ("
            f"Δfusion_4={delta_f4 * 100:+.2f}%p, "
            f"Δpair={eff_pair_gain * 100:+.2f}%p, "
            f"Δfusion_3+={delta_f3 * 100:+.2f}%p, "
            f"Δreproj_med={0.0 if delta_med is None else delta_med:+.3f}px)"
        )

    accepted = len(rejected) == 0 and len(triggered) > 0
    decision = {
        "accepted": accepted,
        "triggered_gates": triggered,
        "rejected_reasons": rejected,
        "criteria": asdict(criteria),
    }
    return delta, decision


def _collect_rotation_observations(
    fit_tracks: SessionTracks,
    rig: CameraRig,
    target_cid: str,
) -> list[PersonObservation]:
    """`fit_tracks` 에서 기준 카메라(2대 이상)와 대상 카메라(`target_cid`)의 `PersonObservation` 목록을 모은다."""
    observations: list[PersonObservation] = []
    # 1) 전체 연관에서 이미 3대 이상(대상 + 기준 2대 이상) 묶인 클러스터
    full_frames = associate_session(fit_tracks, rig)
    matched_ticks: set[int] = set()
    for obs in bundle_observations(full_frames):
        if target_cid not in obs.views or len(obs.views) < 3:
            continue
        matched_ticks.add(obs.t)
        observations.append(obs)

    # 2) 대상 카메라가 틀어져 전체 연관에서 빠진 시각 중, 기준 카메라 2대 이상 클러스터가 단 하나뿐이고
    #    대상 카메라에도 사람이 단 한 명뿐인 시각은 사람 대응이 유일하므로 표본에 더한다.
    ref_frames = {f.t: f for f in associate_session(fit_tracks, rig, exclude=(target_cid,))}
    for t, views in fit_tracks.ticks:
        if t in matched_ticks:
            continue
        target_views = [v for v in views if v.camera_id == target_cid]
        if len(target_views) != 1:
            continue
        rf = ref_frames.get(t)
        if rf is None:
            continue
        ref_clusters = [c for c in rf.clusters if len(c.views) >= 2 and not c.identity_uncertain]
        if len(ref_clusters) != 1:
            continue
        merged_views = {v.camera_id: v.view.keypoints for v in ref_clusters[0].views}
        merged_views[target_cid] = target_views[0].view.keypoints
        observations.append(PersonObservation(ref_clusters[0].association_id, t, merged_views))
    return observations


def try_fast_rotation_path(
    fit_tracks: SessionTracks,
    holdout_tracks: SessionTracks,
    previous: Mapping[str, Any],
    *,
    measured_camera_heights: Mapping[str, float],
    created_at: int,
    fit_before_summary: Mapping[str, Any],
    holdout_before_summary: Mapping[str, Any],
    config: LiveCandidateConfig = DEFAULT_LIVE_CANDIDATE_CONFIG,
) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    """작은 회전 틀어짐용 빠른 길(`realign_rotation`).

    가장 쌍 통과율이 낮은 카메라부터 회전만 다시 맞춰 보고, holdout 에서 의미 있는 개선과 낮은 재투영 오차를
    동시에 달성하면 전체 `session` 번들 조정 없이 즉시 후보 문서를 만든다.
    """
    rig = session_rig(previous)
    old_by_id = {c["camera_id"]: c for c in previous["cameras"]}
    per_cam_fit = fit_before_summary.get("per_camera_pair_pass_mean") or {}
    candidates_order = sorted(
        rig.cameras,
        key=lambda cid: float(per_cam_fit.get(cid, 0.0)),
    )
    attempts: list[dict[str, Any]] = []
    for target_cid in candidates_order:
        fit_pair_mean = float(per_cam_fit.get(target_cid, 0.0))
        if fit_pair_mean >= config.fast_pair_pass_threshold and len(attempts) > 0:
            break
        ref_cams = {cid: cam for cid, cam in rig.cameras.items() if cid != target_cid}
        if len(ref_cams) < 2:
            continue
        observations = _collect_rotation_observations(fit_tracks, rig, target_cid)
        pose = realign_rotation(previous, target_cid, observations)
        if pose is None:
            ref_frames_list = associate_session(fit_tracks, rig, exclude=(target_cid,))
            poses, _ = rotation_candidates(fit_tracks, ref_frames_list, previous, target_cid)
            if poses:
                cand_doc_pnp, _ = poses[0]
                cam_pnp = next(c for c in cand_doc_pnp["cameras"] if c["camera_id"] == target_cid)
                pose = (cam_pnp["R"], cam_pnp["t"])
        rot_delta_deg: float | None = None
        if pose is not None:
            R_new, t_new = pose
            R_old = np.asarray(old_by_id[target_cid]["R"], dtype=float)
            rot_delta_deg = round(
                float(np.degrees(Rotation.from_matrix(np.asarray(R_new, dtype=float) @ R_old.T).magnitude())),
                4,
            )
        attempt_rec: dict[str, Any] = {
            "target_camera_id": target_cid,
            "fit_pair_pass_mean_before": round(fit_pair_mean, 4),
            "observation_count": len(observations),
            "status": "CONVERGED" if pose is not None else "INSUFFICIENT_CORRESPONDENCES",
            "rotation_delta_deg": rot_delta_deg,
            "accepted": False,
        }
        if pose is None or rot_delta_deg is None or rot_delta_deg < config.fast_min_rotation_deg:
            attempt_rec["reject_reason"] = (
                f"pose={'ok' if pose is not None else 'None'}, rotation_delta_deg={rot_delta_deg}"
            )
            attempts.append(attempt_rec)
            continue

        R_new, t_new = pose
        # 카메라 중심(World 위치)은 그대로 두고 회전 R 과 t = -R C 만 갱신한 후보 문서 생성
        cand_cameras: list[CameraCalibration] = []
        for c in previous["cameras"]:
            cid = c["camera_id"]
            reproj_px = c.get("reprojection_error_px") or {}
            if cid == target_cid:
                cand_cameras.append(
                    CameraCalibration(
                        camera_id=cid,
                        image_size=(int(c["image_size"][0]), int(c["image_size"][1])),
                        K=c["K"],
                        R=R_new,
                        t=t_new,
                        distortion_model=c["distortion"]["model"],
                        distortion_coeffs=c["distortion"]["coeffs"],
                        reprojection_median_px=reproj_px.get("median"),
                        reprojection_p95_px=reproj_px.get("p95"),
                    )
                )
            else:
                cand_cameras.append(
                    CameraCalibration(
                        camera_id=cid,
                        image_size=(int(c["image_size"][0]), int(c["image_size"][1])),
                        K=c["K"],
                        R=c["R"],
                        t=c["t"],
                        distortion_model=c["distortion"]["model"],
                        distortion_coeffs=c["distortion"]["coeffs"],
                        reprojection_median_px=reproj_px.get("median"),
                        reprojection_p95_px=reproj_px.get("p95"),
                    )
                )

        cam_contracts = [c.to_contract() for c in cand_cameras]
        temp_doc = {**copy.deepcopy(dict(previous)), "cameras": cam_contracts}
        holdout_after_raw = evaluate_calibration(holdout_tracks, temp_doc)
        holdout_after_summary = summarize_holdout_metrics(holdout_after_raw)
        target_holdout_stats = (holdout_after_summary.get("reproj_by_camera") or {}).get(target_cid) or {}
        target_holdout_med = target_holdout_stats.get("median")
        target_holdout_p95 = target_holdout_stats.get("p95")
        attempt_rec["target_holdout_median_px"] = target_holdout_med
        attempt_rec["target_holdout_p95_px"] = target_holdout_p95

        if target_holdout_med is None or float(target_holdout_med) > config.fast_max_holdout_median_px:
            attempt_rec["reject_reason"] = (
                f"대상 카메라 holdout 재투영 중앙값 {target_holdout_med} px > {config.fast_max_holdout_median_px} px "
                "(렌즈·위치 동시 조정 필요)"
            )
            attempts.append(attempt_rec)
            continue

        # 대상 카메라의 재투영 오차 기록을 holdout 평가 값으로 갱신
        cand_cameras = [
            replace(
                cam,
                reprojection_median_px=target_holdout_med,
                reprojection_p95_px=target_holdout_p95,
            )
            if cam.camera_id == target_cid
            else cam
            for cam in cand_cameras
        ]
        cam_contracts = [c.to_contract() for c in cand_cameras]

        anchor_cids = sorted(ref_cams.keys())
        world_alignment = {
            "usable": True,
            "mode": "fixed_centers_rotation_only",
            "anchor_cameras": anchor_cids,
            "rotation_deg": 0.0,
            "translation_cm": 0.0,
            "scale": 1.0,
            "anchor_center_residuals_cm": {cid: 0.0 for cid in anchor_cids},
            "anchor_center_residual_mean_cm": 0.0,
            "anchor_center_residual_max_cm": 0.0,
            "height_scale_check_after": {"scale_after": 1.0, "max_spread": 0.03},
            "height_residuals_m": None,
        }
        delta, decision = evaluate_improvement(
            holdout_before_summary,
            holdout_after_summary,
            world_alignment=world_alignment,
            criteria=config.improvement_criteria,
        )
        attempt_rec["holdout_delta"] = delta
        attempt_rec["improvement_decision"] = decision
        if not decision["accepted"]:
            attempt_rec["reject_reason"] = "; ".join(decision["rejected_reasons"])
            attempts.append(attempt_rec)
            continue

        evidence = CalibrationEvidence(
            scale_estimates=(
                ScaleEstimate(
                    ScaleBasis.MEASURED_CAMERA_HEIGHT,
                    1.0,
                    sample_count=len(cand_cameras),
                ),
            )
        )
        expected_ids = [c["camera_id"] for c in previous["cameras"]]
        provider_source = str(previous.get("provider_source") or "G1_AUTO")
        judgement = judge_calibration(
            provider_source,
            cam_contracts,
            evidence,
            expected_camera_ids=expected_ids,
        )
        stamp = dt.datetime.fromtimestamp(created_at / 1000, dt.UTC).strftime("%Y%m%d-%H%M%S")
        doc = build_calibration_doc(
            calibration_version=f"cal-fastrot-{stamp}-{created_at % 1000:03d}z",
            provider_source=provider_source,
            provider_version=PROVIDER_VERSION,
            created_at=created_at,
            cameras=cand_cameras,
            judgement=judgement,
            fallback_scale_basis=ScaleBasis.MEASURED_CAMERA_HEIGHT,
            site_id=(previous.get("coordinate_frame") or {}).get("site_id"),
            world_axes_source=previous.get("world_axes_source"),
        )
        errors = check_calibration(doc)
        if errors:
            attempt_rec["reject_reason"] = f"계약 검증 실패: {errors}"
            attempts.append(attempt_rec)
            continue

        attempt_rec["accepted"] = True
        attempts.append(attempt_rec)
        return doc, {
            "accepted": True,
            "target_camera_id": target_cid,
            "attempts": attempts,
            "holdout_after": holdout_after_summary,
            "holdout_delta": delta,
            "improvement_decision": decision,
            "world_alignment": world_alignment,
            "camera_changes": camera_changes(previous, doc, measured_camera_heights),
        }

    return None, {"accepted": False, "attempts": attempts}


def _lower_process_priority(nice_delta: int = 10) -> dict[str, Any]:
    """워커 프로세스의 OS 우선순위를 낮추고 BLAS 스레드를 1개로 제한한다 (명세 3.2, 3.12)."""
    for env_key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[env_key] = "1"
    proc = psutil.Process(os.getpid())
    applied = None
    try:
        if os.name == "nt" and hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            applied = "BELOW_NORMAL_PRIORITY_CLASS"
        else:
            current = proc.nice()
            proc.nice(current + max(1, int(nice_delta)))
            applied = f"nice+{nice_delta}"
    except Exception as exc:  # pragma: no cover - 권한 제한 환경 방어
        applied = f"unchanged:{exc}"
    return {"pid": os.getpid(), "priority": applied}


def execute_candidate_cycle(
    tracks: SessionTracks | None,
    buffer_summary: Mapping[str, Any],
    previous: Mapping[str, Any],
    *,
    measured_camera_heights: Mapping[str, float],
    input_provenance: Mapping[str, Any],
    created_at: int,
    config: LiveCandidateConfig = DEFAULT_LIVE_CANDIDATE_CONFIG,
    worker_info: Mapping[str, Any] | None = None,
) -> CandidateRunResult:
    """스냅샷 `SessionTracks` 로 데이터 기준 점검 → 빠른 길(`realign_rotation`) → 전체 회차 보정(`adjust_session`) → holdout 비교를 수행한다."""
    require_ms("created_at", created_at)
    t_wall0 = time.perf_counter()
    t_cpu0 = time.process_time()
    expected_camera_ids = [c["camera_id"] for c in previous["cameras"]]

    base_report: dict[str, Any] = {
        "format": LIVE_CANDIDATE_REPORT_FORMAT,
        "format_version": LIVE_CANDIDATE_REPORT_VERSION,
        "created_at": created_at,
        "status": STATUS_INSUFFICIENT_DATA,
        "mode": None,
        "proposed": False,
        "previous_version": str(previous["calibration_version"]),
        "candidate_version": None,
        "elapsed_wall_s": 0.0,
        "elapsed_cpu_s": 0.0,
        "worker": dict(worker_info) if worker_info is not None else {"pid": os.getpid(), "priority": "in_process"},
        "buffer_summary": dict(buffer_summary),
        "split": None,
        "data_sufficiency": None,
        "holdout": {"before": None, "after": None, "delta": None},
        "world_alignment": None,
        "camera_changes": None,
        "improvement_decision": None,
        "fast_path": None,
        "failure_reason": None,
    }

    if tracks is None or len(tracks.ticks) < 2:
        empty_tracks = SessionTracks((), {})
        suff = check_data_sufficiency(
            empty_tracks, empty_tracks, (), expected_camera_ids, config.data_criteria
        )
        base_report["data_sufficiency"] = suff
        base_report["failure_reason"] = "최소 데이터 기준 미달: " + "; ".join(suff["reasons"])
        base_report["elapsed_wall_s"] = round(time.perf_counter() - t_wall0, 3)
        base_report["elapsed_cpu_s"] = round(time.process_time() - t_cpu0, 3)
        return CandidateRunResult(STATUS_INSUFFICIENT_DATA, False, None, None, base_report)

    fit_tracks, holdout_tracks, split_record = split_session_tracks(tracks, block_ticks=config.block_ticks)
    base_report["split"] = split_record

    # 데이터 총량 선점검: fit/holdout 시각 수 자체가 미달이면 무거운 연관을 생략한다.
    fit_active = int(split_record["fit_active_ticks"])
    hold_active = int(split_record["holdout_active_ticks"])
    if (
        len(fit_tracks.ticks) < config.data_criteria.min_fit_ticks
        or fit_active < config.data_criteria.min_fit_active_ticks
        or len(holdout_tracks.ticks) < config.data_criteria.min_holdout_ticks
        or hold_active < config.data_criteria.min_holdout_active_ticks
    ):
        suff = check_data_sufficiency(
            fit_tracks, holdout_tracks, (), expected_camera_ids, config.data_criteria
        )
        base_report["data_sufficiency"] = suff
        base_report["failure_reason"] = "최소 데이터 기준 미달: " + "; ".join(suff["reasons"])
        base_report["elapsed_wall_s"] = round(time.perf_counter() - t_wall0, 3)
        base_report["elapsed_cpu_s"] = round(time.process_time() - t_cpu0, 3)
        return CandidateRunResult(STATUS_INSUFFICIENT_DATA, False, None, None, base_report)

    # 현재 활성 보정 기준 fit / holdout 평가
    fit_before_raw = evaluate_calibration(fit_tracks, previous)
    holdout_before_raw = evaluate_calibration(holdout_tracks, previous)
    fit_before_summary = summarize_holdout_metrics(fit_before_raw)
    holdout_before_summary = summarize_holdout_metrics(holdout_before_raw)
    base_report["holdout"]["before"] = holdout_before_summary

    # 1) 작은 회전 틀어짐용 빠른 길(`realign_rotation`)을 먼저 시험한다.
    if config.try_fast_rotation_first:
        fast_doc, fast_info = try_fast_rotation_path(
            fit_tracks,
            holdout_tracks,
            previous,
            measured_camera_heights=measured_camera_heights,
            created_at=created_at,
            fit_before_summary=fit_before_summary,
            holdout_before_summary=holdout_before_summary,
            config=config,
        )
        base_report["fast_path"] = {
            "accepted": fast_info["accepted"],
            "attempts": fast_info["attempts"],
        }
        if fast_doc is not None and fast_info["accepted"]:
            init_obs = bundle_observations(associate_session(fit_tracks, session_rig(fast_doc)))
            suff = check_data_sufficiency(
                fit_tracks, holdout_tracks, init_obs, expected_camera_ids, config.data_criteria
            )
            if suff["usable"]:
                base_report["status"] = STATUS_PROPOSED
                base_report["mode"] = MODE_FAST_ROTATION
                base_report["proposed"] = True
                base_report["candidate_version"] = str(fast_doc["calibration_version"])
                base_report["data_sufficiency"] = suff
                base_report["holdout"]["after"] = fast_info["holdout_after"]
                base_report["holdout"]["delta"] = fast_info["holdout_delta"]
                base_report["world_alignment"] = fast_info["world_alignment"]
                base_report["camera_changes"] = fast_info["camera_changes"]
                base_report["improvement_decision"] = fast_info["improvement_decision"]
                base_report["elapsed_wall_s"] = round(time.perf_counter() - t_wall0, 3)
                base_report["elapsed_cpu_s"] = round(time.process_time() - t_cpu0, 3)
                return CandidateRunResult(STATUS_PROPOSED, True, MODE_FAST_ROTATION, fast_doc, base_report)

    # 2) 전체 회차 보정(`adjust_session`, 직전 = 현재 활성 보정, World 유지) 실행
    outcome = adjust_session(
        fit_tracks,
        previous,
        measured_camera_heights=measured_camera_heights,
        input_provenance=input_provenance,
        created_at=created_at,
        rounds=config.rounds,
        holdout_tracks=holdout_tracks,
        data_criteria=config.data_criteria,
    )
    if (
        outcome.calibration is None
        and config.rounds == 3
        and len(outcome.report.get("rounds") or ()) >= 2
        and outcome.report["rounds"][1].get("status") == "CONVERGED"
    ):
        outcome_r2 = adjust_session(
            fit_tracks,
            previous,
            measured_camera_heights=measured_camera_heights,
            input_provenance=input_provenance,
            created_at=created_at,
            rounds=2,
            holdout_tracks=holdout_tracks,
            data_criteria=config.data_criteria,
        )
        if outcome_r2.calibration is not None:
            outcome = outcome_r2
    s_rep = outcome.report
    suff = s_rep["data_sufficiency"]
    base_report["data_sufficiency"] = suff
    base_report["mode"] = MODE_FULL_SESSION
    base_report["world_alignment"] = s_rep.get("world_alignment")
    base_report["camera_changes"] = s_rep.get("camera_changes")

    if not suff["usable"]:
        base_report["status"] = STATUS_INSUFFICIENT_DATA
        base_report["failure_reason"] = str(s_rep.get("failure_reason") or "최소 데이터 기준 미달")
        base_report["elapsed_wall_s"] = round(time.perf_counter() - t_wall0, 3)
        base_report["elapsed_cpu_s"] = round(time.process_time() - t_cpu0, 3)
        return CandidateRunResult(STATUS_INSUFFICIENT_DATA, False, MODE_FULL_SESSION, None, base_report)

    # 현재 활성 보정(previous)과 후보 보정(outcome)을 정확히 같은 holdout_tracks 위에서 비교한다.
    # (previous 의 렌즈가 FIXED_LENS 가 아니었으면 holdout_before_summary 는 실제 활성 보정 기준이다)
    holdout_after_summary = summarize_holdout_metrics(s_rep["holdout"]["after"])
    base_report["holdout"]["after"] = holdout_after_summary
    delta, decision = evaluate_improvement(
        holdout_before_summary,
        holdout_after_summary,
        world_alignment=s_rep.get("world_alignment"),
        criteria=config.improvement_criteria,
    )
    base_report["holdout"]["delta"] = delta
    base_report["improvement_decision"] = decision

    if outcome.calibration is None:
        base_report["status"] = STATUS_NOT_CONVERGED
        base_report["failure_reason"] = str(s_rep.get("failure_reason") or "회차 보정 미수렴")
        base_report["elapsed_wall_s"] = round(time.perf_counter() - t_wall0, 3)
        base_report["elapsed_cpu_s"] = round(time.process_time() - t_cpu0, 3)
        return CandidateRunResult(STATUS_NOT_CONVERGED, False, MODE_FULL_SESSION, None, base_report)

    if not decision["accepted"]:
        base_report["status"] = STATUS_NO_MEANINGFUL_IMPROVEMENT
        base_report["candidate_version"] = str(outcome.calibration["calibration_version"])
        base_report["failure_reason"] = "의미 있는 개선 기준 미달: " + "; ".join(decision["rejected_reasons"])
        base_report["elapsed_wall_s"] = round(time.perf_counter() - t_wall0, 3)
        base_report["elapsed_cpu_s"] = round(time.process_time() - t_cpu0, 3)
        return CandidateRunResult(
            STATUS_NO_MEANINGFUL_IMPROVEMENT, False, MODE_FULL_SESSION, None, base_report
        )

    base_report["status"] = STATUS_PROPOSED
    base_report["proposed"] = True
    base_report["candidate_version"] = str(outcome.calibration["calibration_version"])
    base_report["elapsed_wall_s"] = round(time.perf_counter() - t_wall0, 3)
    base_report["elapsed_cpu_s"] = round(time.process_time() - t_cpu0, 3)
    return CandidateRunResult(STATUS_PROPOSED, True, MODE_FULL_SESSION, outcome.calibration, base_report)


def _subprocess_worker_entry(
    payload: Mapping[str, Any],
    result_queue: Any,
) -> None:
    """별도 spawn 프로세스 진입점: 우선순위를 낮추고 후보 보정을 계산해 큐에 넣는다."""
    try:
        worker_info = _lower_process_priority(int(payload.get("worker_nice", 10)))
        if payload.get("simulate_sleep_s"):
            time.sleep(float(payload["simulate_sleep_s"]))
        res = execute_candidate_cycle(
            payload["tracks"],
            payload["buffer_summary"],
            payload["previous"],
            measured_camera_heights=payload["measured_camera_heights"],
            input_provenance=payload["input_provenance"],
            created_at=payload["created_at"],
            config=payload["config"],
            worker_info=worker_info,
        )
        result_queue.put(("ok", res))
    except Exception as exc:  # pragma: no cover - 자식 프로세스 예외 전달
        result_queue.put(("err", f"{type(exc).__name__}: {exc}"))


class LiveCandidateCalibrationRunner:
    """운영 중 모은 관절 버퍼로 백그라운드 후보 보정을 주기적으로 실행하고 콜백으로 넘기는 실행기."""

    def __init__(
        self,
        buffer: LiveJointBuffer,
        active_calibration: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
        *,
        measured_camera_heights: Mapping[str, float],
        input_provenance: Mapping[str, Any],
        propose_calibration: Callable[[Mapping[str, Any], dict[str, Any]], Any] | None = None,
        config: LiveCandidateConfig = DEFAULT_LIVE_CANDIDATE_CONFIG,
        admission_gate: AdmissionGate | None = None,
        can_run: Callable[[], bool] | None = None,
        clock_ms: Callable[[], int] | None = None,
        adopt_proposed_as_active: bool = True,
    ) -> None:
        self._buffer = buffer
        self._active_source = active_calibration
        self._active_doc: Mapping[str, Any] | None = (
            None if callable(active_calibration) else copy.deepcopy(dict(active_calibration))
        )
        self._heights = dict(measured_camera_heights)
        self._provenance = dict(input_provenance)
        self._propose_cb = propose_calibration
        self._config = config
        self._admission_gate = admission_gate
        self._can_run = can_run
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._adopt_proposed = adopt_proposed_as_active
        self._last_run_at_ms: int | None = None
        self._history: list[CandidateRunResult] = []
        self._simulate_sleep_s: float = 0.0

    @property
    def config(self) -> LiveCandidateConfig:
        return self._config

    @property
    def history(self) -> tuple[CandidateRunResult, ...]:
        return tuple(self._history)

    def current_calibration(self) -> Mapping[str, Any]:
        if callable(self._active_source):
            return self._active_source()
        assert self._active_doc is not None
        return self._active_doc

    def set_active_calibration(self, calibration: Mapping[str, Any]) -> None:
        problems = check_calibration(calibration)
        if problems:
            raise ValueError(f"활성 보정이 계약에 맞지 않는다: {problems}")
        self._active_doc = copy.deepcopy(dict(calibration))

    def maybe_run(self, *, now_ms: int | None = None) -> CandidateRunResult | None:
        """마지막 실행 시각으로부터 `config.interval_ms` 이상 지났을 때만 후보 보정을 실행한다."""
        ts = int(now_ms if now_ms is not None else self._clock_ms())
        if self._last_run_at_ms is not None and (ts - self._last_run_at_ms) < self._config.interval_ms:
            return None
        return self.run_once(now_ms=ts)

    def run_once(self, *, now_ms: int | None = None) -> CandidateRunResult:
        """버퍼 스냅샷을 떠서 후보 보정을 1회 실행하고, 의미 있게 좋아진 경우에만 콜백을 호출한다."""
        ts = int(now_ms if now_ms is not None else self._clock_ms())
        require_ms("now_ms", ts)
        self._last_run_at_ms = ts
        previous = copy.deepcopy(dict(self.current_calibration()))
        buf_summary = self._buffer.summary()

        # 명세 3.2, 3.12: Tier 0 실시간 판정 자원 우선순위 확인
        if self._admission_gate is not None:
            adm = self._admission_gate.check()
            if not adm.admit or adm.blocks("live_candidate_calibration"):
                res = self._deferred_result(
                    ts, previous, buf_summary, f"AdmissionGate 보류: {adm.reason}"
                )
                self._history.append(res)
                return res
        if self._can_run is not None and not self._can_run():
            res = self._deferred_result(ts, previous, buf_summary, "Tier 0 부하로 인해 can_run() 이 실행을 보류함")
            self._history.append(res)
            return res

        tracks = self._buffer.to_session_tracks(sample_period_ms=self._buffer.config.sample_period_ms)
        if self._config.use_subprocess:
            res = self._run_in_subprocess(tracks, buf_summary, previous, ts)
        else:
            res = execute_candidate_cycle(
                tracks,
                buf_summary,
                previous,
                measured_camera_heights=self._heights,
                input_provenance=self._provenance,
                created_at=ts,
                config=self._config,
            )

        if res.proposed and res.calibration is not None:
            if self._adopt_proposed and not callable(self._active_source):
                self._active_doc = copy.deepcopy(dict(res.calibration))
            if self._propose_cb is not None:
                self._propose_cb(res.calibration, res.report)

        self._history.append(res)
        return res

    def _deferred_result(
        self,
        created_at: int,
        previous: Mapping[str, Any],
        buf_summary: Mapping[str, Any],
        reason: str,
    ) -> CandidateRunResult:
        rep: dict[str, Any] = {
            "format": LIVE_CANDIDATE_REPORT_FORMAT,
            "format_version": LIVE_CANDIDATE_REPORT_VERSION,
            "created_at": created_at,
            "status": STATUS_DEFERRED,
            "mode": None,
            "proposed": False,
            "previous_version": str(previous["calibration_version"]),
            "candidate_version": None,
            "elapsed_wall_s": 0.0,
            "elapsed_cpu_s": 0.0,
            "worker": None,
            "buffer_summary": dict(buf_summary),
            "split": None,
            "data_sufficiency": None,
            "holdout": {"before": None, "after": None, "delta": None},
            "world_alignment": None,
            "camera_changes": None,
            "improvement_decision": None,
            "fast_path": None,
            "failure_reason": reason,
        }
        return CandidateRunResult(STATUS_DEFERRED, False, None, None, rep)

    def _run_in_subprocess(
        self,
        tracks: SessionTracks | None,
        buf_summary: Mapping[str, Any],
        previous: Mapping[str, Any],
        created_at: int,
    ) -> CandidateRunResult:
        t0 = time.perf_counter()
        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        payload = {
            "tracks": tracks,
            "buffer_summary": dict(buf_summary),
            "previous": previous,
            "measured_camera_heights": self._heights,
            "input_provenance": self._provenance,
            "created_at": created_at,
            "config": self._config,
            "worker_nice": self._config.worker_nice,
            "simulate_sleep_s": self._simulate_sleep_s,
        }
        proc = ctx.Process(target=_subprocess_worker_entry, args=(payload, queue), daemon=True)
        proc.start()
        item: tuple[str, Any] | None = None
        timed_out = False
        deadline = t0 + float(self._config.timeout_s)
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = queue.get(timeout=min(0.1, remaining))
                break
            except queue_mod.Empty:
                if not proc.is_alive():
                    try:
                        item = queue.get_nowait()
                    except queue_mod.Empty:
                        pass
                    break

        if timed_out:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)
                if proc.is_alive():
                    proc.kill()
                    proc.join(timeout=1.0)
            elapsed = round(time.perf_counter() - t0, 3)
            rep: dict[str, Any] = {
                "format": LIVE_CANDIDATE_REPORT_FORMAT,
                "format_version": LIVE_CANDIDATE_REPORT_VERSION,
                "created_at": created_at,
                "status": STATUS_TIMEOUT,
                "mode": None,
                "proposed": False,
                "previous_version": str(previous["calibration_version"]),
                "candidate_version": None,
                "elapsed_wall_s": elapsed,
                "elapsed_cpu_s": None,
                "worker": {"pid": proc.pid, "priority": "terminated_on_timeout"},
                "buffer_summary": dict(buf_summary),
                "split": None,
                "data_sufficiency": None,
                "holdout": {"before": None, "after": None, "delta": None},
                "world_alignment": None,
                "camera_changes": None,
                "improvement_decision": None,
                "fast_path": None,
                "failure_reason": f"백그라운드 후보 보정 시간 상한 초과 ({elapsed:.2f}s > {self._config.timeout_s:.2f}s)",
            }
            return CandidateRunResult(STATUS_TIMEOUT, False, None, None, rep)

        proc.join(timeout=2.0)
        if item is not None:
            tag, value = item
            if tag == "ok":
                return value
            err_msg = str(value)
        else:
            err_msg = f"워커 프로세스 비정상 종료 (exitcode={proc.exitcode})"

        elapsed = round(time.perf_counter() - t0, 3)
        rep = {
            "format": LIVE_CANDIDATE_REPORT_FORMAT,
            "format_version": LIVE_CANDIDATE_REPORT_VERSION,
            "created_at": created_at,
            "status": STATUS_FAILED,
            "mode": None,
            "proposed": False,
            "previous_version": str(previous["calibration_version"]),
            "candidate_version": None,
            "elapsed_wall_s": elapsed,
            "elapsed_cpu_s": None,
            "worker": {"pid": proc.pid, "exitcode": proc.exitcode},
            "buffer_summary": dict(buf_summary),
            "split": None,
            "data_sufficiency": None,
            "holdout": {"before": None, "after": None, "delta": None},
            "world_alignment": None,
            "camera_changes": None,
            "improvement_decision": None,
            "fast_path": None,
            "failure_reason": err_msg,
        }
        return CandidateRunResult(STATUS_FAILED, False, None, None, rep)


__all__ = [
    "DEFAULT_IMPROVEMENT_CRITERIA",
    "DEFAULT_LIVE_BUFFER_CONFIG",
    "DEFAULT_LIVE_CANDIDATE_CONFIG",
    "LIVE_CANDIDATE_REPORT_FORMAT",
    "LIVE_CANDIDATE_REPORT_VERSION",
    "MODE_FAST_ROTATION",
    "MODE_FULL_SESSION",
    "STATUS_DEFERRED",
    "STATUS_FAILED",
    "STATUS_INSUFFICIENT_DATA",
    "STATUS_NO_MEANINGFUL_IMPROVEMENT",
    "STATUS_NOT_CONVERGED",
    "STATUS_PROPOSED",
    "STATUS_TIMEOUT",
    "CandidateImprovementCriteria",
    "CandidateRunResult",
    "LiveBufferConfig",
    "LiveCandidateCalibrationRunner",
    "LiveCandidateConfig",
    "LiveJointBuffer",
    "evaluate_improvement",
    "execute_candidate_cycle",
    "summarize_holdout_metrics",
    "try_fast_rotation_path",
]
