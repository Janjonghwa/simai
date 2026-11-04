"""Extract and cache COPE Re-ID embeddings for 10/29 track_replay regression.

Reads detection cache (cache-s2), runs MultiCameraLocalTracker to assign the exact
local_track_id per frame, decodes selected H.264 GOPs/keyframes from manifest.json
in memory (never saving raw frames or crops to disk), extracts 1280-D COPE features
on GPU/CPU, and writes a compact numerical NPZ cache for deterministic track_replay.
"""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
import gzip
import json
from pathlib import Path
import sys
import time
from typing import Any

_AI_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _AI_ROOT / "src"
for _p in (str(_AI_ROOT), str(_SRC_ROOT)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
_VENV_SITE = _AI_ROOT / ".venv" / "Lib" / "site-packages"
if _VENV_SITE.is_dir() and str(_VENV_SITE) not in sys.path:
    sys.path.append(str(_VENV_SITE))

import av  # noqa: E402
import numpy as np  # noqa: E402

from simai_ai.reid.collector import (  # noqa: E402
    LocalTrackFeature,
    LocalTrackFeatureCache,
    estimate_orientation_bin_2d,
)
from simai_ai.reid.embedder import COPEConfig, COPEEmbedder  # noqa: E402
from simai_ai.reid.visibility import estimate_part_visibility  # noqa: E402
from simai_ai.tracking.local_config import tracker_config  # noqa: E402
from simai_ai.tracking.local_tracker import (  # noqa: E402
    CameraDetections,
    MultiCameraLocalTracker,
    PoseDetection,
)

ANNEXB = b"\x00\x00\x00\x01"
CAMERAS = ("cam_A", "cam_B", "cam_C", "cam_D")


def _crop_person_rgb(
    frame_rgb: np.ndarray,
    bbox_xyxy: tuple[float, float, float, float],
) -> np.ndarray | None:
    h, w = frame_rgb.shape[:2]
    x1 = max(0, min(w - 1, int(round(bbox_xyxy[0]))))
    y1 = max(0, min(h - 1, int(round(bbox_xyxy[1]))))
    x2 = max(x1 + 1, min(w, int(round(bbox_xyxy[2]))))
    y2 = max(y1 + 1, min(h, int(round(bbox_xyxy[3]))))
    if (x2 - x1) < 24 or (y2 - y1) < 64:
        return None
    return np.ascontiguousarray(frame_rgb[y1:y2, x1:x2])


def _keypoints_to_array(keypoints_2d: tuple[tuple[float, float, float] | None, ...]) -> np.ndarray:
    arr = np.zeros((17, 3), dtype=np.float64)
    for idx, kp in enumerate(keypoints_2d[:17]):
        if kp is not None and len(kp) >= 3:
            arr[idx, 0] = float(kp[0])
            arr[idx, 1] = float(kp[1])
            arr[idx, 2] = float(kp[2])
    return arr


def _track_camera_detections(
    cache_path: Path,
    cam_id: str,
    *,
    min_det_score: float = 0.35,
    min_vis_score: float = 0.25,
) -> tuple[dict[int, list[dict[str, Any]]], set[int]]:
    """Run 2D local tracker on cached detections and return all valid observations by frame_sequence
    plus the set of priority frame_sequences (first 2 appearances or >1200ms gap of any local_track_id).
    """
    rows: list[tuple[dict[str, Any], list[Any]]] = []
    with gzip.open(cache_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("dets") is None:
                continue
            rows.append((r["h"], r["dets"]))
    rows.sort(key=lambda x: int(x[0]["frame_sequence"]))

    tracker = MultiCameraLocalTracker(tracker_config())
    count_by_lid: dict[str, int] = defaultdict(int)
    last_ts_by_lid: dict[str, int] = {}
    obs_by_seq: dict[int, list[dict[str, Any]]] = defaultdict(list)
    priority_seqs: set[int] = set()

    for h, dets in rows:
        seq = int(h["frame_sequence"])
        ts_ms = int(h["estimated_capture_time_ms"])
        pd = tuple(
            PoseDetection(
                tuple(float(v) for v in b),  # type: ignore[arg-type]
                float(s),
                tuple(tuple(float(x) for x in k) if k is not None else None for k in kps),  # type: ignore[misc]
            )
            for b, s, kps in dets
        )
        cd = CameraDetections(
            camera_id=cam_id,
            stream_epoch=int(h["stream_epoch"]),
            frame_sequence=seq,
            estimated_capture_time_ms=ts_ms,
            timestamp_source=str(h["timestamp_source"]),
            timestamp_uncertainty_ms=(
                None
                if h.get("timestamp_uncertainty_ms") is None
                else float(h["timestamp_uncertainty_ms"])
            ),
            detections=pd,
        )
        obs_frames = tracker.update_batch([cd])
        if not obs_frames:
            continue
        obs_frame = obs_frames[0]
        for td in obs_frame.detections:
            det = td.detection
            if det.bbox is None or float(det.score) < min_det_score:
                continue
            x1, y1, x2, y2 = det.bbox
            if (x2 - x1) < 24.0 or (y2 - y1) < 64.0:
                continue
            kps_arr = _keypoints_to_array(det.keypoints_2d)
            vis = estimate_part_visibility(kps_arr, det.bbox)
            if vis.visibility_ratio() < min_vis_score:
                continue
            lid = str(td.local_track_id)
            seen_cnt = count_by_lid[lid]
            prev_ts = last_ts_by_lid.get(lid)
            is_priority = seen_cnt < 2 or prev_ts is None or (ts_ms - prev_ts) >= 1200
            count_by_lid[lid] = seen_cnt + 1
            if is_priority:
                priority_seqs.add(seq)
                last_ts_by_lid[lid] = ts_ms
            orient_bin = estimate_orientation_bin_2d(det.keypoints_2d, det.bbox, num_bins=8)
            obs_by_seq[seq].append(
                {
                    "camera_id": cam_id,
                    "local_track_id": lid,
                    "frame_sequence": seq,
                    "timestamp_ms": ts_ms,
                    "bbox": (float(x1), float(y1), float(x2), float(y2)),
                    "score": float(det.score),
                    "visibility": vis,
                    "orientation_bin": orient_bin,
                }
            )
    return obs_by_seq, priority_seqs


def build_replay_cache(
    *,
    recording_dir: Path,
    detection_cache_dir: Path,
    out_npz_path: Path,
    cope_repo_root: Path,
    cope_weights_path: Path,
    clip_weights_path: Path,
    device: str = "cuda",
    interval_ms: int = 600,
    batch_size: int = 32,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    embedder = COPEEmbedder(
        COPEConfig(
            cope_root=cope_repo_root,
            weights_path=cope_weights_path,
            clip_path=clip_weights_path,
            device=device,
            batch_size=batch_size,
        )
    )
    cache = LocalTrackFeatureCache()
    manifest = json.loads((recording_dir / "manifest.json").read_text(encoding="utf-8"))

    stats_by_cam: dict[str, dict[str, Any]] = {}
    total_features = 0

    for cam_entry in manifest["cameras"]:
        cam_id = str(cam_entry["camera_id"])
        if cam_id not in CAMERAS:
            continue
        cam_t0 = time.perf_counter()
        det_cache_path = detection_cache_dir / f"{cam_id}.jsonl.gz"
        obs_by_seq, priority_seqs = _track_camera_detections(det_cache_path, cam_id)

        pending_crops: list[np.ndarray] = []
        pending_meta: list[dict[str, Any]] = []
        decoded_target_frames = 0
        cam_features = 0
        last_extracted_ts: dict[str, int] = {}

        def _flush_batch() -> None:
            nonlocal cam_features, total_features
            if not pending_crops:
                return
            embs = embedder.embed(pending_crops)
            for idx, meta in enumerate(pending_meta):
                vis = meta["visibility"]
                quality = float(
                    np.clip(0.55 * float(meta["score"]) + 0.45 * float(vis.visibility_ratio()), 0.0, 1.0)
                )
                feat = LocalTrackFeature(
                    camera_id=meta["camera_id"],
                    local_track_id=str(meta["local_track_id"]),
                    frame_sequence=int(meta["frame_sequence"]),
                    capture_time_ms=int(meta["timestamp_ms"]),
                    embedding=embs[idx].astype(np.float32, copy=False),
                    visibility=vis,
                    quality_score=quality,
                    orientation_bin=meta["orientation_bin"],
                    bbox_xyxy=meta["bbox"],
                )
                cache.put(feat, keep_timeline=True)
                cam_features += 1
                total_features += 1
            pending_crops.clear()
            pending_meta.clear()

        for seg in cam_entry["segments"]:
            codec = json.loads((recording_dir / seg["codec_info"]["path"]).read_text(encoding="utf-8"))
            prefix = b"".join(ANNEXB + base64.b64decode(codec[k]) for k in ("sps", "pps") if codec.get(k))
            video_path = recording_dir / seg["video"]["path"]
            frames_path = recording_dir / seg["frames"]["path"]

            # Group rows by GOP (starting at each keyframe)
            gops: list[list[dict[str, Any]]] = []
            current_gop: list[dict[str, Any]] = []
            with open(frames_path, encoding="utf-8") as lines_fh:
                for line in lines_fh:
                    row = json.loads(line)
                    if row["header"].get("keyframe") and current_gop:
                        gops.append(current_gop)
                        current_gop = []
                    current_gop.append(row)
            if current_gop:
                gops.append(current_gop)

            with open(video_path, "rb") as video_fh:
                for gop in gops:
                    if not gop:
                        continue
                    # Find which frames in this GOP need extraction:
                    # 1. The keyframe (gop[0]) if it has observations
                    # 2. Any priority frame (new track / >1.2s gap) in this GOP
                    target_seqs_in_gop: set[int] = set()
                    kf_seq = int(gop[0]["header"]["frame_sequence"])
                    if kf_seq in obs_by_seq:
                        target_seqs_in_gop.add(kf_seq)
                    max_target_idx = 0
                    for idx_in_gop, row in enumerate(gop):
                        fseq = int(row["header"]["frame_sequence"])
                        if fseq in priority_seqs and fseq in obs_by_seq:
                            target_seqs_in_gop.add(fseq)
                            max_target_idx = max(max_target_idx, idx_in_gop)
                    if not target_seqs_in_gop:
                        continue

                    ctx = av.CodecContext.create("h264", "r")
                    ctx.thread_type = "SLICE"
                    for idx_in_gop in range(max_target_idx + 1):
                        row = gop[idx_in_gop]
                        fseq = int(row["header"]["frame_sequence"])
                        video_fh.seek(int(row["offset"]))
                        payload = video_fh.read(int(row["size"]))
                        raw_bytes = (prefix + payload) if idx_in_gop == 0 else payload
                        try:
                            pkt = av.Packet(raw_bytes)
                            pics = list(ctx.decode(pkt))
                            if idx_in_gop == max_target_idx and not pics:
                                pics = list(ctx.decode(None))
                        except Exception:  # noqa: BLE001
                            break
                        if fseq not in target_seqs_in_gop or not pics or pics[0].is_corrupt:
                            continue
                        metas = [
                            m
                            for m in obs_by_seq.get(fseq, ())
                            if (
                                m["local_track_id"] not in last_extracted_ts
                                or (m["timestamp_ms"] - last_extracted_ts[m["local_track_id"]]) >= interval_ms
                                or fseq in priority_seqs
                            )
                        ]
                        if not metas:
                            continue
                        decoded_target_frames += 1
                        rgb = pics[0].to_ndarray(format="rgb24")
                        for meta in metas:
                            crop = _crop_person_rgb(rgb, meta["bbox"])
                            if crop is None:
                                continue
                            last_extracted_ts[meta["local_track_id"]] = int(meta["timestamp_ms"])
                            pending_crops.append(crop)
                            pending_meta.append(meta)
                            if len(pending_crops) >= batch_size:
                                _flush_batch()
                        del rgb

        _flush_batch()
        cam_elapsed = time.perf_counter() - cam_t0
        stats_by_cam[cam_id] = {
            "priority_frames": len(priority_seqs),
            "decoded_target_frames": decoded_target_frames,
            "extracted_features": cam_features,
            "elapsed_s": round(cam_elapsed, 2),
        }
        print(
            f"[{cam_id}] priority_frames={len(priority_seqs)} decoded={decoded_target_frames} "
            f"features={cam_features} elapsed={cam_elapsed:.1f}s",
            flush=True,
        )

    cache.save_npz(out_npz_path)
    total_elapsed = time.perf_counter() - t0
    summary = {
        "out_npz_path": str(out_npz_path),
        "total_features": total_features,
        "interval_ms": interval_ms,
        "device": embedder.device,
        "elapsed_s": round(total_elapsed, 2),
        "cameras": stats_by_cam,
    }
    print(f"Saved {total_features} features to {out_npz_path} in {total_elapsed:.1f}s", flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build COPE Re-ID NPZ cache for 10/29 replay.")
    parser.add_argument(
        "--recording",
        type=Path,
        default=Path(
            "C:/Users/SSAFY/Desktop/simai/S15P31A101/ai/experiments/recordings/"
            "rec-20261029-015512z-lobby-railing-pose"
        ),
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path("C:/Users/SSAFY/simai-local/track-413/cache-s2"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("C:/Users/SSAFY/simai-local/reid-eval/recording1029_reid_cache.npz"),
    )
    parser.add_argument(
        "--cope-root",
        type=Path,
        default=Path("C:/Users/SSAFY/simai-local/reid/cope"),
    )
    parser.add_argument(
        "--cope-weights",
        type=Path,
        default=Path("C:/Users/SSAFY/simai-local/reid/weights/cope_msmt17.pth"),
    )
    parser.add_argument(
        "--clip-weights",
        type=Path,
        default=Path("C:/Users/SSAFY/simai-local/reid/clip-cache/ViT-B-16.pt"),
    )
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--interval-ms", type=int, default=600)
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()

    build_replay_cache(
        recording_dir=args.recording,
        detection_cache_dir=args.cache,
        out_npz_path=args.out,
        cope_repo_root=args.cope_root,
        cope_weights_path=args.cope_weights,
        clip_weights_path=args.clip_weights,
        device=args.device,
        interval_ms=args.interval_ms,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
