"""회차의 모든 사람을 로컬 추적하고 Tier 0와 같은 보간·연관으로 묶는다.

탈락한 view도 ticks에 남는다. 보정이 바뀐 라운드는 원본 ticks 전체로 다시 연관한다.
이 ID는 보정용 대응 ID이며 World ID나 사람의 정체성을 뜻하지 않는다.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from simai_ai.calibration.bundle import PersonObservation
from simai_ai.calibration.extrinsics.keypoints import check_header
from simai_ai.calibration.extrinsics.sync import repair_timelines
from simai_ai.geometry.interpolation import KeypointSample, interpolate_camera
from simai_ai.geometry.rig import CameraRig
from simai_ai.tracking.cross_view import AssociationFrame, CrossViewAssociator, LocalView
from simai_ai.tracking.local_config import tracker_config
from simai_ai.tracking.local_tracker import CameraDetections, CameraLocalTracker, PoseDetection


@dataclass(frozen=True)
class SessionTracks:
    ticks: tuple[tuple[int, tuple[LocalView, ...]], ...]
    record: Mapping[str, Any]


def track_session(header, rows, *, sample_period_ms=300, backend_factory=None) -> SessionTracks:
    """추출한 모든 검출 → 카메라별 기존 Local Tracker → 공통 시각.

    backend_factory는 합성 시험에서 백엔드 의존성을 주입할 때 쓴다.
    """
    check_header(header)
    if header.get("joint_layout") != "COCO17":
        raise ValueError("session은 COCO17 관측을 받는다")
    if sample_period_ms < 1:
        raise ValueError("sample_period_ms는 양의 정수다")
    repaired, timeline = repair_timelines(rows)
    trackers = {}
    samples = defaultdict(list)
    untracked = 0
    for row in sorted(repaired, key=lambda r: (r["camera_id"], r["stream_epoch"], r["frame_sequence"])):
        cid = row["camera_id"]
        if cid not in trackers:
            kwargs = {} if backend_factory is None else {"backend_factory": backend_factory}
            trackers[cid] = CameraLocalTracker(cid, tracker_config(), **kwargs)
        dets = tuple(
            PoseDetection(tuple(d["bbox"]), d["score"], tuple(
                tuple(k) if k is not None else None for k in d["keypoints_2d"]
            )) for d in row["detections"]
        )
        if any(len(d.keypoints_2d) != 17 for d in dets):
            raise ValueError(f"{cid}: session COCO17은 17개 관절이어야 한다")
        frame = trackers[cid].update(CameraDetections(
            cid, row["stream_epoch"], row["frame_sequence"], row["estimated_capture_time_ms"],
            row["timestamp_source"], row.get("timestamp_uncertainty_ms"), dets,
        ))
        if frame is None:
            continue
        untracked += frame.untracked_count
        for det in frame.detections:
            samples[(cid, det.local_track_id)].append(KeypointSample(
                cid, frame.estimated_capture_time_ms, frame.timestamp_source,
                frame.timestamp_uncertainty_ms, det.detection.keypoints_2d,
                {"stream_epoch": frame.stream_epoch, "frame_sequence": frame.frame_sequence},
            ))
    if not samples:
        raise ValueError("회차에 추적된 사람이 없다")
    times = {k: [s.estimated_capture_time for s in v] for k, v in samples.items()}
    lo = min(ts[0] for ts in times.values())
    hi = max(ts[-1] for ts in times.values())
    ticks, drops = [], defaultdict(int)
    for t in range(lo, hi + 1, sample_period_ms):
        views = []
        for key, seq in samples.items():
            idx = bisect_left(times[key], t)
            view, rejected = interpolate_camera(seq[max(0, idx - 1):idx + 1], t)
            for d in rejected:
                drops[str(d.reason)] += 1
            if view is not None:
                views.append(LocalView(*key, view))
        ticks.append((t, tuple(views)))
    return SessionTracks(tuple(ticks), {
        "timeline": timeline, "local_tracks": len(samples), "untracked_detections": untracked,
        "ticks": len(ticks), "interpolation_drops": dict(drops),
        "sample_period_ms": sample_period_ms, "tracker": "tracktrack", "joint_source": "observed",
    })


def associate_session(tracks: SessionTracks, rig: CameraRig, *, exclude=()):
    """운영 기본 CrossViewConfig를 그대로 쓴다. 임계값을 넓히지 않는다.

    다중 카메라 관측이 없는 빈 시각을 그대로 넘기면 CrossViewAssociator._previous가
    비워져 시간 연속 대응 ID가 끊기므로, 활성 카메라가 2대 이상인 시각만 연관한다.
    """
    associator = CrossViewAssociator(rig)
    return tuple(
        associator.associate(t, views, exclude_cameras=exclude)
        for t, views in tracks.ticks
        if len({v.camera_id for v in views if v.camera_id not in exclude}) >= 2
    )


def bundle_observations(frames: Sequence[AssociationFrame]) -> tuple[PersonObservation, ...]:
    return tuple(
        PersonObservation(c.association_id, f.t, {v.camera_id: v.view.keypoints for v in c.views})
        for f in frames for c in f.clusters if len(c.views) >= 2 and not c.identity_uncertain
    )


def association_metrics(frames: Sequence[AssociationFrame]) -> dict[str, Any]:
    pairs = defaultdict(lambda: {"candidates": 0, "passed": 0, "passed_strict": 0, "rejects": defaultdict(int)})
    counts = defaultdict(int)
    total_views = fused3_views = fused4_views = 0
    ticks_4view = largest_3plus_4view = largest_4_4view = 0
    for f in frames:
        for p in f.pairs:
            key = "/".join(sorted((p.a[0], p.b[0])))
            pairs[key]["candidates"] += 1
            pairs[key]["passed"] += int(p.feasible)
            pairs[key]["passed_strict"] += int(p.feasible and not p.marginal)
            if p.reject:
                pairs[key]["rejects"][str(p.reject)] += 1
        for c in f.clusters:
            n = len(c.views)
            counts[n] += 1
            total_views += n
            fused3_views += n if n >= 3 else 0
            fused4_views += n if n >= 4 else 0
        cams = {v.camera_id for c in f.clusters for v in c.views}
        if len(cams) >= 4 and f.clusters:
            ticks_4view += 1
            best_n = max(len(c.views) for c in f.clusters)
            largest_3plus_4view += int(best_n >= 3)
            largest_4_4view += int(best_n >= 4)
    multi = sum(n for k, n in counts.items() if k >= 2)
    return {
        "pairs": {
            k: {
                **v,
                "rejects": dict(v["rejects"]),
                "pass_ratio": v["passed"] / v["candidates"],
                "pass_strict_ratio": v["passed_strict"] / v["candidates"],
            }
            for k, v in sorted(pairs.items())
        },
        "cluster_counts": dict(sorted(counts.items())),
        "fusion_3plus_ratio": sum(n for k, n in counts.items() if k >= 3) / multi if multi else None,
        "fusion_4_ratio": counts[4] / multi if multi else None,
        "view_fusion_3plus_ratio": fused3_views / total_views if total_views else None,
        "view_fusion_4_ratio": fused4_views / total_views if total_views else None,
        "ticks_4view": ticks_4view,
        "largest_cluster_3plus_4view_ratio": largest_3plus_4view / ticks_4view if ticks_4view else None,
        "largest_cluster_4_4view_ratio": largest_4_4view / ticks_4view if ticks_4view else None,
        "denominator": "pair=모든 교차 카메라 후보(다른 사람 포함); fusion=2대 이상 클러스터; view_fusion=전체 view; 4view=4대 모두 관측된 시각의 최대 클러스터",
    }
