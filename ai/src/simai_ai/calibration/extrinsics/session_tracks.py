"""회차의 모든 사람을 로컬 추적하고 Tier 0와 같은 보간·연관으로 묶는다.

탈락한 view도 ticks에 남는다. 보정이 바뀐 라운드는 원본 ticks 전체로 다시 연관한다.
이 ID는 보정용 대응 ID이며 World ID나 사람의 정체성을 뜻하지 않는다.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from simai_ai.calibration.bundle import PersonObservation
from simai_ai.calibration.extrinsics.keypoints import check_header
from simai_ai.calibration.extrinsics.sync import repair_timelines
from simai_ai.geometry import distortion
from simai_ai.geometry.interpolation import KeypointSample, interpolate_camera
from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.geometry.triangulation import triangulate_normalized_many, triangulate_point
from simai_ai.tracking.cross_view import AssociationFrame, CrossViewAssociator, LocalView
from simai_ai.tracking.local_config import tracker_config
from simai_ai.tracking.local_tracker import CameraDetections, CameraLocalTracker, PoseDetection

_MIN_DEPTH = 1e-6


@dataclass(frozen=True)
class SessionTracks:
    ticks: tuple[tuple[int, tuple[LocalView, ...]], ...]
    record: Mapping[str, Any]


def _window_index(t_ms: int, windows: Sequence[tuple[int, int]]) -> int | None:
    for idx, (lo, hi) in enumerate(windows):
        if lo <= t_ms <= hi:
            return idx
    return None


def track_session(header, rows, *, sample_period_ms=300, backend_factory=None) -> SessionTracks:
    """추출한 모든 검출 → 카메라별 기존 Local Tracker → 공통 시각.

    backend_factory는 합성 시험에서 백엔드 의존성을 주입할 때 쓴다.
    """
    check_header(header)
    if header.get("joint_layout") != "COCO17":
        raise ValueError("session은 COCO17 관측을 받는다")
    if sample_period_ms < 1:
        raise ValueError("sample_period_ms는 양의 정수다")
    raw_windows = header.get("sample_windows_ms") or ()
    windows = tuple((int(w[0]), int(w[1])) for w in raw_windows if len(w) == 2)
    repaired, timeline = repair_timelines(rows)
    trackers = {}
    last_window: dict[str, int | None] = {}
    samples = defaultdict(list)
    untracked = 0
    for row in sorted(repaired, key=lambda r: (r["camera_id"], r["stream_epoch"], r["frame_sequence"])):
        cid = row["camera_id"]
        raw_t = int(row.get("estimated_capture_time_ms_original", row["estimated_capture_time_ms"]))
        w_idx = _window_index(raw_t, windows) if windows else None
        if cid not in trackers or (windows and last_window.get(cid) != w_idx):
            kwargs = {} if backend_factory is None else {"backend_factory": backend_factory}
            trackers[cid] = CameraLocalTracker(cid, tracker_config(), **kwargs)
            last_window[cid] = w_idx
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
            tid = f"w{w_idx}_{det.local_track_id}" if w_idx is not None else det.local_track_id
            samples[(cid, tid)].append(KeypointSample(
                cid, frame.estimated_capture_time_ms, frame.timestamp_source,
                frame.timestamp_uncertainty_ms, det.detection.keypoints_2d,
                {"stream_epoch": frame.stream_epoch, "frame_sequence": frame.frame_sequence},
            ))
    if not samples:
        raise ValueError("회차에 추적된 사람이 없다")
    times = {k: [s.estimated_capture_time for s in v] for k, v in samples.items()}
    lo = min(ts[0] for ts in times.values())
    hi = max(ts[-1] for ts in times.values())
    tick_ranges: list[tuple[int, int]] = []
    active_windows: list[list[int]] = []
    if windows:
        all_ts = [t_s for ts in times.values() for t_s in ts]
        for w_lo, w_hi in windows:
            in_w = [t_s for t_s in all_ts if w_lo <= t_s <= w_hi]
            if in_w:
                tick_ranges.append((min(in_w), max(in_w)))
                active_windows.append([int(w_lo), int(w_hi)])
    if not tick_ranges:
        tick_ranges = [(lo, hi)]
    ticks, drops = [], defaultdict(int)
    for r_lo, r_hi in tick_ranges:
        for t in range(r_lo, r_hi + 1, sample_period_ms):
            views = []
            for key, seq in samples.items():
                idx = bisect_left(times[key], t)
                view, rejected = interpolate_camera(seq[max(0, idx - 1):idx + 1], t)
                for d in rejected:
                    drops[str(d.reason)] += 1
                if view is not None:
                    views.append(LocalView(*key, view))
            ticks.append((t, tuple(views)))
    rec: dict[str, Any] = {
        "timeline": timeline, "local_tracks": len(samples), "untracked_detections": untracked,
        "ticks": len(ticks), "interpolation_drops": dict(drops),
        "sample_period_ms": sample_period_ms, "tracker": "tracktrack", "joint_source": "observed",
    }
    if active_windows:
        rec["sample_windows_ms"] = active_windows
    return SessionTracks(tuple(ticks), rec)


def split_session_tracks(
    tracks: SessionTracks, *, block_ticks: int = 10
) -> tuple[SessionTracks, SessionTracks, dict[str, Any]]:
    """공통 시각 시퀀스를 겹치지 않는 교대 구간(fit / holdout)으로 나눈다."""
    if block_ticks < 1:
        raise ValueError("block_ticks는 1 이상이어야 한다")
    windows = tracks.record.get("sample_windows_ms") or ()
    fit_ticks: list[tuple[int, tuple[LocalView, ...]]] = []
    holdout_ticks: list[tuple[int, tuple[LocalView, ...]]] = []
    fit_windows: list[list[int]] = []
    holdout_windows: list[list[int]] = []
    if len(windows) >= 2:
        method = "sample_windows_alternating"
        parsed = [(int(w[0]), int(w[1])) for w in windows]
        for idx, (w_lo, w_hi) in enumerate(parsed):
            target = fit_windows if idx % 2 == 0 else holdout_windows
            target.append([w_lo, w_hi])
        for t, views in tracks.ticks:
            w_idx = _window_index(t, parsed)
            if w_idx is None or w_idx % 2 == 0:
                fit_ticks.append((t, views))
            else:
                holdout_ticks.append((t, views))
    else:
        method = "contiguous_blocks_alternating"
        n = len(tracks.ticks)
        step = max(1, min(block_ticks, max(1, n // 2)))
        for b_idx, start in enumerate(range(0, n, step)):
            chunk = tracks.ticks[start:start + step]
            if not chunk:
                continue
            span = [int(chunk[0][0]), int(chunk[-1][0])]
            if b_idx % 2 == 0:
                fit_ticks.extend(chunk)
                fit_windows.append(span)
            else:
                holdout_ticks.extend(chunk)
                holdout_windows.append(span)
    fit_set = {t for t, _ in fit_ticks}
    holdout_set = {t for t, _ in holdout_ticks}
    overlap = len(fit_set & holdout_set)
    fit_rec = {**dict(tracks.record), "ticks": len(fit_ticks), "split_role": "fit", "sample_windows_ms": fit_windows}
    hold_rec = {
        **dict(tracks.record), "ticks": len(holdout_ticks), "split_role": "holdout",
        "sample_windows_ms": holdout_windows,
    }
    split_record = {
        "method": method,
        "window_count": len(fit_windows) + len(holdout_windows),
        "fit_window_count": len(fit_windows),
        "holdout_window_count": len(holdout_windows),
        "fit_windows_ms": fit_windows,
        "holdout_windows_ms": holdout_windows,
        "fit_ticks": len(fit_ticks),
        "holdout_ticks": len(holdout_ticks),
        "fit_active_ticks": sum(1 for _, vs in fit_ticks if len({v.camera_id for v in vs}) >= 2),
        "holdout_active_ticks": sum(1 for _, vs in holdout_ticks if len({v.camera_id for v in vs}) >= 2),
        "overlap_ticks": overlap,
    }
    return SessionTracks(tuple(fit_ticks), fit_rec), SessionTracks(tuple(holdout_ticks), hold_rec), split_record


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


def _usable_kp(kp: Any, min_conf: float) -> bool:
    return kp is not None and len(kp) >= 3 and all(math.isfinite(float(v)) for v in kp[:3]) and kp[2] >= min_conf


def _project_point(cam: RigCamera, X: np.ndarray) -> np.ndarray:
    Xc = cam.to_camera(X.reshape(1, 3))[0]
    if Xc[2] <= _MIN_DEPTH:
        return np.array([math.nan, math.nan])
    xn = Xc[:2] / Xc[2]
    xd = distortion.distort(xn.reshape(1, 2), cam.distortion_model, cam.distortion_coeffs)[0]
    return xd @ cam.K[:2, :2].T + cam.K[:2, 2]


def _project_points(cam: RigCamera, X: np.ndarray) -> np.ndarray:
    Xc = cam.to_camera(X.reshape(-1, 3))
    out = np.full((len(Xc), 2), math.nan, dtype=float)
    front = Xc[:, 2] > _MIN_DEPTH
    if not front.any():
        return out
    xn = Xc[front, :2] / Xc[front, 2:3]
    xd = distortion.distort(xn, cam.distortion_model, cam.distortion_coeffs)
    out[front] = xd @ cam.K[:2, :2].T + cam.K[:2, 2]
    return out


def reprojection_metrics(
    frames: Sequence[AssociationFrame], rig: CameraRig, *, min_confidence: float = 0.3,
    observation_errors: dict | None = None,
) -> dict[str, Any]:
    """연관된 다중 카메라 클러스터에서 관절을 삼각측량해 카메라별 재투영 오차(중앙값·p95)를 잰다."""
    by_cam: dict[str, list[float]] = {cid: [] for cid in sorted(rig.cameras)}
    grouped: dict[tuple[str, ...], list[list[tuple[float, float]]]] = defaultdict(list)
    grouped_keys = defaultdict(list)
    for frame, cluster in ((f, c) for f in frames for c in f.clusters
                           if len(c.views) >= 2 and not c.identity_uncertain):
        local_views = {v.camera_id: v for v in cluster.views}
        counts = {len(v.view.keypoints) for v in cluster.views}
        for j in range(max(counts, default=0)):
            views = {
                cid: (float(kps[j][0]), float(kps[j][1]))
                for cid, v in local_views.items()
                for kps in (v.view.keypoints,)
                if cid in rig.cameras and _usable_kp(kps[j], min_confidence)
            }
            if len(views) < 2:
                continue
            ids = tuple(sorted(views))
            grouped[ids].append([views[cid] for cid in ids])
            if observation_errors is not None:
                grouped_keys[ids].append([(frame.t, cid, local_views[cid].local_track_id, j) for cid in ids])
    for ids, rows in grouped.items():
        cams = [rig.cameras[cid] for cid in ids]
        pixels = np.asarray(rows, dtype=float)
        norm = np.stack([cam.normalize(pixels[:, i]) for i, cam in enumerate(cams)], axis=1)
        X = triangulate_normalized_many(cams, norm)
        valid = np.isfinite(X).all(axis=1)
        if not valid.any():
            continue
        X_valid = X[valid]
        px_valid = pixels[valid]
        for i, (cid, cam) in enumerate(zip(ids, cams, strict=True)):
            proj = _project_points(cam, X_valid)
            errs = np.linalg.norm(proj - px_valid[:, i], axis=1)
            by_cam[cid].extend(float(e) for e in errs[np.isfinite(errs)])
            if observation_errors is not None:
                keys = [row[i] for row, keep in zip(grouped_keys[ids], valid, strict=True) if keep]
                observation_errors.update((key, float(e)) for key, e in zip(keys, errs, strict=True) if math.isfinite(e))
    all_err = [e for errs in by_cam.values() for e in errs]
    out: dict[str, Any] = {}
    for cid, errs in sorted(by_cam.items()):
        arr = np.asarray(errs, dtype=float)
        out[cid] = {
            "median": round(float(np.median(arr)), 3) if len(arr) else None,
            "p95": round(float(np.percentile(arr, 95)), 3) if len(arr) else None,
            "observations": len(errs),
        }
    all_arr = np.asarray(all_err, dtype=float)
    out["overall"] = {
        "median": round(float(np.median(all_arr)), 3) if len(all_arr) else None,
        "p95": round(float(np.percentile(all_arr, 95)), 3) if len(all_arr) else None,
        "observations": len(all_err),
    }
    return out


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
