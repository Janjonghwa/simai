"""COPE 외형·얼굴 보조 특징 추출 오프라인 평가 및 GPU 벤치마크 실행기 (S15P31A101-463).

1단계(오프라인 특징 추출·성능 검증):
1. `rec-20261029-015512z-lobby-railing-pose` 녹화(4대, 약 9분) + `-413` 검출 캐시(`cache-s2`)를
   Tier 0 `MultiCameraLocalTracker` + `Perception` 으로 재생해 관측별 로컬 트랙 ID 와 World ID 를 매핑한다.
   - 참조 World ID: 회차 보정(`calib-session-1029-v2`) + `SplitSuppressingManager(radius_m=0.6, require_owner=False)`
     (2인 구간 사람 수 일치율 93.7%)
   - 기준선 World ID: `E0_baseline`(`cal-fastrot-20261030-011907-266z`, 9분 동안 ID 149개 분할)
2. 녹화 H.264 키프레임(`keyframe=true`, 2.0 s 간격, 카메라당 270프레임)만 오프셋 탐색으로 디코딩해
   메모리 안에서만 사람 박스 crop 을 잘라 COPE 1280-D 임베딩·PSS 점수·신체 부위 가시성·얼굴 게이트 통과율을 추출한다.
   - **원본 프레임·사람 crop 이미지는 디스크나 저장소에 절대 저장하지 않는다(명세 11.5).**
3. 추가 녹화(`session11/rec-calwalk`, 4대 H.264)에서도 주기 샘플링 프레임에 YOLO26x-pose 검출 + COPE 임베딩을 돌려
   교차 검증 지표를 산출한다.
4. `E0_baseline` 의 분할·재진입 트랙 조각(fragment)들을 COPE `TrackGallery` 로 매칭했을 때
   같은 사람 대 다른 사람 트랙 재연결 분리도(ROC-AUC, Top-1 정확도)를 측정한다.
5. 노트북 GPU(RTX 4050)에서 배치 크기(1·8·16)별 COPE 추론 지연시간과 VRAM 사용량을 측정한다.
6. 결과(`metrics.json`, `summary.md`, `run_record.json`)를 `C:/Users/SSAFY/simai-local/reid-eval/` 에 기록한다.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np

AI_DIR = Path(__file__).resolve().parents[2]
for _p in (AI_DIR / "src", AI_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
_venv_sp = AI_DIR / ".venv" / "Lib" / "site-packages"
if _venv_sp.is_dir() and str(_venv_sp) not in sys.path:
    sys.path.append(str(_venv_sp))

from experiments.reid_eval.benchmark_gpu import benchmark_cope_embedder
from experiments.reid_eval.metrics import (
    CropObservationRecord,
    distribution_summary,
    evaluate_observation_records,
    roc_auc_score,
)
from experiments.track_replay.replay_tracking import CachedFeed, SplitSuppressingManager, load_cache
from services.tier0.perception import Perception
from simai_ai.contact import SurfaceSet
from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.reid import (
    COPEConfig,
    COPEEmbedder,
    FaceConfig,
    FaceEmbedder,
    GalleryConfig,
    PartWeights,
    TrackGallery,
    create_face_embedder,
    estimate_face_region_from_pose,
    estimate_part_visibility,
)
from simai_ai.tracking.local_config import tracker_config
from simai_ai.tracking.local_tracker import MultiCameraLocalTracker
from simai_ai.tracking.time_model import TimeModelConfig
from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackConfig

REPO = Path(__file__).resolve().parents[3]
SCHEMA_VERSION = "2.2.0"
ANNEXB = b"\x00\x00\x00\x01"

DEFAULT_SESSION_1029 = Path(
    "C:/Users/SSAFY/Desktop/simai/S15P31A101/ai/experiments/recordings/rec-20261029-015512z-lobby-railing-pose"
)
DEFAULT_CACHE_1029 = Path("C:/Users/SSAFY/simai-local/track-413/cache-s2")
DEFAULT_CALIB_REF = Path("C:/Users/SSAFY/simai-local/calib-session-1029-v2/calibration.json")
DEFAULT_CALIB_BASE = Path("C:/Users/SSAFY/simai-local/calib-session-1029-v2/calibration.json")
DEFAULT_CALWALK_DIR = Path("C:/Users/SSAFY/simai-local/session11/rec-calwalk")
DEFAULT_CALWALK_CALIB = Path("C:/Users/SSAFY/simai-local/session11/lobby-bundle-rotfix/calibration.json")
DEFAULT_YOLO_WEIGHTS = Path("C:/Users/SSAFY/simai-local/weights/yolo26x-pose.pt")
DEFAULT_OUT_DIR = Path("C:/Users/SSAFY/simai-local/reid-eval")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False).stdout.strip()


def _gpu_info() -> dict[str, Any]:
    out = {"model": "", "driver": "", "cuda": "", "provider": "LOCAL", "shared_with_tier0": False}
    try:
        row = (
            subprocess.run(
                ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            .stdout.strip()
            .splitlines()
        )
        if row:
            out["model"], out["driver"] = [v.strip() for v in row[0].split(",")][:2]
        head = subprocess.run(["nvidia-smi"], capture_output=True, text=True, check=False, timeout=15).stdout
        if "CUDA Version:" in head:
            out["cuda"] = head.split("CUDA Version:")[1].split()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _bbox_iou(box_a: Sequence[float], box_b: Sequence[float]) -> float:
    x1 = max(float(box_a[0]), float(box_b[0]))
    y1 = max(float(box_a[1]), float(box_b[1]))
    x2 = min(float(box_a[2]), float(box_b[2]))
    y2 = min(float(box_a[3]), float(box_b[3]))
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, float(box_a[2]) - float(box_a[0])) * max(0.0, float(box_a[3]) - float(box_a[1]))
    area_b = max(0.0, float(box_b[2]) - float(box_b[0])) * max(0.0, float(box_b[3]) - float(box_b[1]))
    denom = area_a + area_b - inter
    return inter / denom if denom > 0.0 else 0.0


def _crop_bgr_to_rgb(
    frame_bgr: np.ndarray,
    bbox_xyxy: Sequence[float],
    *,
    min_side_px: int = 24,
) -> np.ndarray | None:
    h_img, w_img = frame_bgr.shape[:2]
    x1 = max(0, min(w_img - 1, int(round(float(bbox_xyxy[0])))))
    y1 = max(0, min(h_img - 1, int(round(float(bbox_xyxy[1])))))
    x2 = max(x1 + 1, min(w_img, int(round(float(bbox_xyxy[2])))))
    y2 = max(y1 + 1, min(h_img, int(round(float(bbox_xyxy[3])))))
    if (x2 - x1) < min_side_px or (y2 - y1) < min_side_px:
        return None
    crop_bgr = frame_bgr[y1:y2, x1:x2]
    return cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)


def replay_world_assignments(
    cache: Mapping[str, list[tuple[dict, list]]],
    calibration: Path,
    *,
    suppress_split: bool = False,
    session_tag: str = "reidref",
    pretracked_polls: dict[int, list[Any]] | None = None,
) -> tuple[
    dict[tuple[str, int], list[tuple[tuple[float, float, float, float], str]]],
    dict[tuple[str, str], list[tuple[int, str]]],
    dict[str, str],
    dict[str, Any],
    dict[int, list[Any]],
]:
    """캐시를 로컬 트래커와 Perception 에 흘려 프레임별 로컬 트랙 ID 와 World ID 배정을 복원한다."""
    rig = CameraRig.from_provider(FileCalibrationProvider(calibration))
    tracker = MultiCameraLocalTracker(tracker_config(), camera_ids=sorted(cache))
    feed = CachedFeed(cache, tracker, {})

    frame_local_boxes: dict[tuple[str, int], list[tuple[tuple[float, float, float, float], str]]] = {}
    recorded_polls: dict[int, list[Any]] = {} if pretracked_polls is None else pretracked_polls
    orig_poll = feed.poll

    def capturing_poll(now_ms: int):
        if pretracked_polls is not None:
            return list(pretracked_polls.get(now_ms, ()))
        obs_list = orig_poll(now_ms)
        recorded_polls[now_ms] = obs_list
        for obs in obs_list:
            frame_local_boxes[(obs.camera_id, obs.frame_sequence)] = [
                (tuple(float(v) for v in td.detection.bbox), td.local_track_id)
                for td in obs.detections
            ]
        return obs_list

    feed.poll = capturing_poll  # type: ignore[method-assign]

    time_cfg = TimeModelConfig()
    world_cfg = WorldTrackConfig.from_time_model(time_cfg)
    perception = Perception(
        rig,
        feed,
        session=session_tag,
        scene_version="replay",
        input_provenance=feed.provenance(),
        time_config=time_cfg,
        world_config=world_cfg,
    )
    if suppress_split:
        perception.manager = SplitSuppressingManager(
            WorldIdIssuer(session_tag),
            world_cfg,
            input_provenance=feed.provenance(),
            radius_m=0.6,
            require_owner=False,
        )

    key_timeline: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    alias_parent: dict[str, str] = {}
    surfaces = SurfaceSet()
    now = feed.first_arrival
    end = feed.last_arrival + 1000
    ticks = 0
    seen_world_ids: set[str] = set()

    while now <= end:
        p = perception.step(now, surfaces)
        if p is not None:
            ticks += 1
            for s in p.frame.tracks:
                seen_world_ids.add(s.world_id)
                for al in s.aliases:
                    alias_parent[al] = s.world_id
                if s.updated and s.local_keys:
                    for cam_id, ltid in s.local_keys:
                        key_timeline[(cam_id, ltid)].append((p.t, s.world_id))
        now += 100

    def resolve_canonical(wid: str) -> str:
        visited: set[str] = set()
        cur = wid
        while cur in alias_parent and cur not in visited:
            visited.add(cur)
            cur = alias_parent[cur]
        return cur

    canonical_map = {wid: resolve_canonical(wid) for wid in seen_world_ids | set(alias_parent)}
    resolved_timeline: dict[tuple[str, str], list[tuple[int, str]]] = {
        k: [(t_ms, canonical_map.get(wid, wid)) for t_ms, wid in pairs]
        for k, pairs in key_timeline.items()
    }
    summary = {
        "calibration_version": rig.calibration_version,
        "suppress_split": suppress_split,
        "ticks": ticks,
        "issued_world_ids": len(seen_world_ids),
        "canonical_world_ids": len(set(canonical_map.values())),
    }
    return frame_local_boxes, resolved_timeline, canonical_map, summary, recorded_polls


def _lookup_world_id(
    timeline: Sequence[tuple[int, str]],
    capture_time_ms: int,
    *,
    max_gap_ms: int = 1_500,
) -> str | None:
    if not timeline:
        return None
    best_wid: str | None = None
    best_dt = max_gap_ms + 1
    for t_ms, wid in timeline:
        dt = abs(t_ms - capture_time_ms)
        if dt < best_dt:
            best_dt = dt
            best_wid = wid
    return best_wid if best_dt <= max_gap_ms else None


def decode_keyframes_and_extract_records(
    session_dir: Path,
    cache: Mapping[str, list[tuple[dict, list]]],
    frame_local_boxes: Mapping[tuple[str, int], list[tuple[tuple[float, float, float, float], str]]],
    ref_timeline: Mapping[tuple[str, str], list[tuple[int, str]]],
    base_timeline: Mapping[tuple[str, str], list[tuple[int, str]]],
    embedder: COPEEmbedder,
    face_embedder: FaceEmbedder,
) -> tuple[list[CropObservationRecord], dict[int, str], dict[str, Any]]:
    """녹화 키프레임을 오프셋 탐색으로 디코딩해 메모리 내에서 COPE 임베딩과 관측 메타데이터를 추출한다."""
    import av

    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    cache_by_cam_seq: dict[tuple[str, int], tuple[dict[str, Any], list[Any]]] = {}
    for cam_id, rows in cache.items():
        for h, dets in rows:
            cache_by_cam_seq[(cam_id, int(h["frame_sequence"]))] = (h, dets)

    pending_meta: list[dict[str, Any]] = []
    pending_crops: list[np.ndarray] = []
    decoded_keyframes = 0

    for cam_entry in manifest["cameras"]:
        cam_id = cam_entry["camera_id"]
        if cam_id not in cache:
            continue
        for seg in cam_entry["segments"]:
            codec = json.loads((session_dir / seg["codec_info"]["path"]).read_text(encoding="utf-8"))
            prefix = b"".join(ANNEXB + base64.b64decode(codec[k]) for k in ("sps", "pps") if codec.get(k))
            video_path = session_dir / seg["video"]["path"]
            frames_path = session_dir / seg["frames"]["path"]

            with open(video_path, "rb") as video_fh, open(frames_path, encoding="utf-8") as lines_fh:
                for line in lines_fh:
                    row = json.loads(line)
                    header = row["header"]
                    if not header.get("keyframe"):
                        continue
                    fseq = int(header["frame_sequence"])
                    cached = cache_by_cam_seq.get((cam_id, fseq))
                    if cached is None:
                        continue
                    _, dets = cached
                    if not dets:
                        continue

                    video_fh.seek(int(row["offset"]))
                    payload = video_fh.read(int(row["size"]))
                    ctx = av.CodecContext.create("h264", "r")
                    ctx.thread_type = "SLICE"
                    try:
                        pkt = av.Packet(prefix + payload)
                        pics = list(ctx.decode(pkt)) + list(ctx.decode(None))
                    except Exception:  # noqa: BLE001
                        continue
                    if not pics or pics[0].is_corrupt:
                        continue

                    frame_bgr = pics[0].to_ndarray(format="bgr24")
                    decoded_keyframes += 1
                    cap_ms = int(header["estimated_capture_time_ms"])
                    tracked_boxes = frame_local_boxes.get((cam_id, fseq), [])

                    for det_idx, (box, score, kps) in enumerate(dets):
                        if float(score) < 0.30:
                            continue
                        crop_rgb = _crop_bgr_to_rgb(frame_bgr, box)
                        if crop_rgb is None:
                            continue

                        # 로컬 트랙 ID 매칭 (IoU 최대)
                        ltid = f"untracked_{fseq}_{det_idx}"
                        best_iou = 0.5
                        for t_box, cand_ltid in tracked_boxes:
                            iou = _bbox_iou(box, t_box)
                            if iou > best_iou:
                                best_iou = iou
                                ltid = cand_ltid

                        ref_wid = _lookup_world_id(ref_timeline.get((cam_id, ltid), ()), cap_ms)
                        base_wid = _lookup_world_id(base_timeline.get((cam_id, ltid), ()), cap_ms)
                        vis = estimate_part_visibility(kps, bbox_xyxy=box)
                        face_cand = estimate_face_region_from_pose(
                            kps,
                            person_bbox_xyxy=box,
                            config=face_embedder.config,
                        )

                        pending_crops.append(crop_rgb)
                        pending_meta.append(
                            {
                                "camera_id": cam_id,
                                "frame_sequence": fseq,
                                "timestamp_ms": cap_ms,
                                "local_track_id": ltid,
                                "world_id": ref_wid,
                                "base_world_id": base_wid,
                                "bbox_xyxy": (
                                    float(box[0]),
                                    float(box[1]),
                                    float(box[2]),
                                    float(box[3]),
                                ),
                                "det_score": float(score),
                                "visibility": vis,
                                "face_min_side_px": None if face_cand is None else face_cand.min_side_px,
                                "face_passes_gate": False if face_cand is None else face_cand.passes_size_gate,
                            }
                        )

    t_emb0 = time.perf_counter()
    emb_feats, emb_scores = embedder.embed_with_scores(pending_crops)
    embed_elapsed_s = round(time.perf_counter() - t_emb0, 3)

    # 메모리 해제를 위해 원본 crop 리스트를 즉시 비운다.
    pending_crops.clear()

    records: list[CropObservationRecord] = []
    base_world_by_obs: dict[int, str] = {}
    for obs_id, (meta, feat_vec, p_sc) in enumerate(zip(pending_meta, emb_feats, emb_scores, strict=True)):
        if meta["base_world_id"] is not None:
            base_world_by_obs[obs_id] = meta["base_world_id"]
        records.append(
            CropObservationRecord(
                obs_id=obs_id,
                camera_id=meta["camera_id"],
                frame_sequence=meta["frame_sequence"],
                timestamp_ms=meta["timestamp_ms"],
                local_track_id=meta["local_track_id"],
                world_id=meta["world_id"],
                bbox_xyxy=meta["bbox_xyxy"],
                det_score=meta["det_score"],
                visibility=meta["visibility"],
                face_min_side_px=meta["face_min_side_px"],
                face_passes_gate=meta["face_passes_gate"],
                p_score=float(p_sc),
                embedding=feat_vec,
            )
        )

    extract_stats = {
        "decoded_keyframes": decoded_keyframes,
        "extracted_crops": len(records),
        "crops_with_ref_world_id": sum(1 for r in records if r.world_id is not None),
        "crops_with_base_world_id": len(base_world_by_obs),
        "cope_embed_elapsed_s": embed_elapsed_s,
    }
    return records, base_world_by_obs, extract_stats


def evaluate_fragment_reassociation(
    records: Sequence[CropObservationRecord],
    base_world_by_obs: Mapping[int, str],
) -> dict[str, Any]:
    """`E0_baseline` 에서 쪼개진 World ID 조각(fragment)들을 COPE 특징으로 재연결할 때의 분리도를 평가한다."""
    frag_obs: dict[str, list[CropObservationRecord]] = defaultdict(list)
    for r in records:
        bwid = base_world_by_obs.get(r.obs_id)
        if bwid is not None and r.world_id is not None:
            frag_obs[bwid].append(r)

    # 조각(fragment)마다 참조 World ID(실제 사람) 다수결을 구한다.
    frag_person: dict[str, str] = {}
    frag_mean_emb: dict[str, np.ndarray] = {}
    frag_time_span: dict[str, tuple[int, int]] = {}
    frag_cameras: dict[str, set[str]] = {}

    for bwid, obs_list in frag_obs.items():
        counts = Counter(r.world_id for r in obs_list if r.world_id is not None)
        if not counts:
            continue
        person_id, top_cnt = counts.most_common(1)[0]
        if top_cnt / len(obs_list) < 0.8:
            continue  # 두 사람이 섞인 조각은 평가 기준에서 제외
        frag_person[bwid] = str(person_id)
        mat = np.stack([r.embedding for r in obs_list], axis=0).astype(np.float32)
        mean_v = mat.mean(axis=0)
        norm = float(np.linalg.norm(mean_v))
        frag_mean_emb[bwid] = mean_v / norm if norm > 1e-8 else mean_v
        ts = [r.timestamp_ms for r in obs_list]
        frag_time_span[bwid] = (min(ts), max(ts))
        frag_cameras[bwid] = {r.camera_id for r in obs_list}

    frag_ids = sorted(frag_person.keys(), key=lambda k: frag_time_span[k][0])
    same_person_frag_sims: list[float] = []
    diff_person_frag_sims: list[float] = []

    for i in range(len(frag_ids)):
        fa = frag_ids[i]
        t0_a, t1_a = frag_time_span[fa]
        for j in range(i + 1, len(frag_ids)):
            fb = frag_ids[j]
            t0_b, _ = frag_time_span[fb]
            # 시간 간격 30초 이내의 조각 쌍(동시 2+2 분할 또는 단기 가림 후 재등장)만 평가
            if t0_b - t1_a > 30_000:
                continue
            sim = float(np.dot(frag_mean_emb[fa], frag_mean_emb[fb]))
            if frag_person[fa] == frag_person[fb]:
                same_person_frag_sims.append(sim)
            else:
                diff_person_frag_sims.append(sim)

    # 시간순 DORMANT/ACTIVE 갤러리 재연결 시뮬레이션:
    # 새 fragment 가 등장했을 때 기존에 관측된 각 사람 갤러리와의 유사도로 Top-1 매칭
    person_galleries: dict[str, TrackGallery] = {}
    top1_queries = 0
    top1_hits_max = 0
    top1_hits_mean = 0

    for bwid in frag_ids:
        true_person = frag_person[bwid]
        obs_list = frag_obs[bwid]
        if len(person_galleries) >= 2 and true_person in person_galleries:
            # 조각의 첫 관측으로 기존 갤러리들에 질의
            first_obs = obs_list[0]
            scores_max: dict[str, float] = {}
            scores_mean: dict[str, float] = {}
            for pid, gal in person_galleries.items():
                if len(gal) == 0:
                    continue
                res = gal.compute_similarity(
                    first_obs.embedding,
                    query_visibility=first_obs.visibility,
                    query_camera_id=first_obs.camera_id,
                    mode="max",
                )
                scores_max[pid] = res.max_similarity
                scores_mean[pid] = res.mean_similarity
            if len(scores_max) >= 2 and true_person in scores_max:
                top1_queries += 1
                if max(scores_max, key=lambda k: scores_max[k]) == true_person:
                    top1_hits_max += 1
                if max(scores_mean, key=lambda k: scores_mean[k]) == true_person:
                    top1_hits_mean += 1

        gal = person_galleries.setdefault(
            true_person,
            TrackGallery(true_person, GalleryConfig(max_entries_per_track=16, max_entries_per_slot=2)),
        )
        for r in obs_list:
            cx = 0.5 * (r.bbox_xyxy[0] + r.bbox_xyxy[2])
            orient_bin = int(cx // 288) % 8
            gal.add_observation(
                r.embedding,
                timestamp_ms=r.timestamp_ms,
                camera_id=r.camera_id,
                orientation_bin=orient_bin,
                visibility=r.visibility,
                quality_score=r.p_score,
            )

    return {
        "evaluated_fragments": len(frag_ids),
        "unique_reference_persons": len(set(frag_person.values())),
        "same_person_fragment_sim": distribution_summary(same_person_frag_sims),
        "diff_person_fragment_sim": distribution_summary(diff_person_frag_sims),
        "fragment_pair_auc": roc_auc_score(same_person_frag_sims, diff_person_frag_sims),
        "new_fragment_top1_queries": top1_queries,
        "new_fragment_top1_accuracy_max": round(top1_hits_max / top1_queries, 4) if top1_queries else None,
        "new_fragment_top1_accuracy_mean": round(top1_hits_mean / top1_queries, 4) if top1_queries else None,
    }


def evaluate_calwalk_recording(
    calwalk_dir: Path,
    yolo_weights: Path,
    embedder: COPEEmbedder,
    face_embedder: FaceEmbedder,
    *,
    frame_stride: int = 25,
    max_sampled_steps: int = 60,
) -> dict[str, Any]:
    """`session11/rec-calwalk` H.264 녹화(4대)에서 주기 샘플링 프레임으로 COPE 카메라 간·시간 간 유사도를 평가한다."""
    import av
    from ultralytics import YOLO

    cameras = ["cam_A", "cam_B", "cam_C", "cam_D"]
    h264_paths = {cam: calwalk_dir / cam / "epoch_0000.h264" for cam in cameras}
    if not all(p.is_file() for p in h264_paths.values()) or not yolo_weights.is_file():
        return {"available": False}

    yolo = YOLO(str(yolo_weights))
    pending_crops: list[np.ndarray] = []
    pending_meta: list[dict[str, Any]] = []

    for cam_id, h264_path in h264_paths.items():
        sampled = 0
        with av.open(str(h264_path), format="h264") as cont:
            for frame_idx, frame in enumerate(cont.decode(video=0)):
                if frame_idx % frame_stride != 0:
                    continue
                sampled += 1
                if sampled > max_sampled_steps:
                    break
                frame_bgr = frame.to_ndarray(format="bgr24")
                results = yolo.predict(frame_bgr, imgsz=1280, half=True, verbose=False)
                if not results:
                    continue
                r0 = results[0]
                boxes = getattr(r0, "boxes", None)
                if boxes is None or len(boxes) == 0:
                    continue
                kps_all = r0.keypoints.data.tolist() if r0.keypoints is not None else [None] * len(boxes)
                # calwalk 는 1인 보정 걷기가 주류이나 프레임 내 검출이 여럿이면 가장 점수가 높은 주 검출과 부 검출을 기록
                det_rows = list(zip(boxes.xyxy.tolist(), boxes.conf.tolist(), kps_all, strict=True))
                det_rows.sort(key=lambda x: -float(x[1]))
                timestamp_ms = int((frame_idx / 25.0) * 1000.0)
                for rank_idx, (box, conf, kps) in enumerate(det_rows):
                    if float(conf) < 0.35:
                        continue
                    crop_rgb = _crop_bgr_to_rgb(frame_bgr, box)
                    if crop_rgb is None:
                        continue
                    vis = estimate_part_visibility(kps, bbox_xyxy=box)
                    face_cand = estimate_face_region_from_pose(
                        kps,
                        person_bbox_xyxy=box,
                        config=face_embedder.config,
                    )
                    # 단일 주 보행자(rank 0, 프레임에 1명만 검출된 경우)를 world_id="w-calwalk-main" 으로 표기
                    is_single = len([d for d in det_rows if float(d[1]) >= 0.35]) == 1
                    wid = "w-calwalk-main" if is_single and rank_idx == 0 else None
                    pending_crops.append(crop_rgb)
                    pending_meta.append(
                        {
                            "camera_id": cam_id,
                            "frame_sequence": frame_idx,
                            "timestamp_ms": timestamp_ms,
                            "local_track_id": f"{cam_id}_r{rank_idx}",
                            "world_id": wid,
                            "bbox_xyxy": (float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                            "det_score": float(conf),
                            "visibility": vis,
                            "face_min_side_px": None if face_cand is None else face_cand.min_side_px,
                            "face_passes_gate": False if face_cand is None else face_cand.passes_size_gate,
                        }
                    )

    emb_feats, emb_scores = embedder.embed_with_scores(pending_crops)
    pending_crops.clear()

    records: list[CropObservationRecord] = []
    for obs_id, (meta, feat_vec, p_sc) in enumerate(zip(pending_meta, emb_feats, emb_scores, strict=True)):
        records.append(
            CropObservationRecord(
                obs_id=obs_id,
                camera_id=meta["camera_id"],
                frame_sequence=meta["frame_sequence"],
                timestamp_ms=meta["timestamp_ms"],
                local_track_id=meta["local_track_id"],
                world_id=meta["world_id"],
                bbox_xyxy=meta["bbox_xyxy"],
                det_score=meta["det_score"],
                visibility=meta["visibility"],
                face_min_side_px=meta["face_min_side_px"],
                face_passes_gate=meta["face_passes_gate"],
                p_score=float(p_sc),
                embedding=feat_vec,
            )
        )

    eval_res = evaluate_observation_records(records)
    return {
        "available": True,
        "session_dir": calwalk_dir.as_posix(),
        "frame_stride": frame_stride,
        "evaluation": eval_res,
    }


def build_summary_markdown(metrics: Mapping[str, Any]) -> str:
    """평가 결과 JSON 을 사람이 읽기 쉬운 한국어 Markdown 표 요약으로 변환한다."""
    main_eval = metrics["main_recording_1029"]["evaluation"]
    frag_eval = metrics["main_recording_1029"]["fragment_reassociation"]
    calwalk_eval = metrics.get("calwalk_session11", {})
    bench = metrics["gpu_benchmark"]

    dist = main_eval["distributions"]
    auc = main_eval["auc"]
    op = main_eval["operating_points"]

    lines = [
        "# Re-ID 1단계: COPE 외형·얼굴 보조 특징 오프라인 평가 요약 (S15P31A101-463)",
        "",
        "## 1. 실행 환경 및 입력",
        f"- **모델**: COPE (`ViT-B/16 CLIP + CV/Prompt + Part-Map`, 출력 차원 `{metrics['model']['embedding_dim']}`, 입력 `256×128`)",
        f"- **가중치**: `{metrics['model']['cope_weights_path']}` (`{metrics['model']['cope_weights_sha256'][:23]}...`)",
        f"- **디바이스**: `{bench['gpu_name']}` (`{bench['device']}`)",
        f"- **주 평가 녹화**: `rec-20261029-015512z-lobby-railing-pose` (4대 카메라, 키프레임 `{metrics['main_recording_1029']['extract_stats']['decoded_keyframes']}`개, 추출 사람 crop `{main_eval['total_crops']}`개)",
        f"- **교차 검증 녹화**: `session11/rec-calwalk` (4대 카메라, 추출 사람 crop `{calwalk_eval.get('evaluation', {}).get('total_crops', 0)}`개)",
        "- **개인정보·저장 규칙**: 원본 프레임 및 사람 crop 이미지는 디스크·저장소에 한 장도 저장하지 않음 (`metrics.json`, `summary.md`, `run_record.json` 만 기록)",
        "",
        "## 2. 같은 사람 vs 다른 사람 코사인 유사도 분포 (`rec-20261029-015512z`)",
        "",
        "| 구분 | 표본 쌍 수 | 평균 (mean) | 표준편차 (std) | p10 | p25 | 중앙값 (p50) | p75 | p90 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    rows_spec = [
        ("같은 사람 (동일 카메라, 1.5~20s 간격)", dist["same_person_local_same_cam"]),
        ("같은 사람 (전체 World ID 기준)", dist["same_person_world_all"]),
        ("같은 사람 (서로 다른 카메라 간, ≤2.5s)", dist["same_person_cross_cam"]),
        ("다른 사람 (전체 동시 관측)", dist["diff_person_all"]),
        ("다른 사람 (동일 카메라 내 동시 관측)", dist["diff_person_same_cam"]),
        ("다른 사람 (서로 다른 카메라 간 동시 관측)", dist["diff_person_cross_cam"]),
    ]
    for label, d in rows_spec:
        lines.append(
            f"| {label} | {d['count']} | {d['mean']} | {d['std']} | {d['p10']} | {d['p25']} | **{d['p50']}** | {d['p75']} | {d['p90']} |"
        )

    lines.extend(
        [
            "",
            "## 3. 분리도 (ROC-AUC) 및 최적 문턱값 동작점",
            "",
            "| 평가 구간 | ROC-AUC | 최적 문턱값 (Youden J) | Youden J (TPR − FPR) |",
            "|---|---:|---:|---:|",
            f"| 전체 (같은 사람 vs 다른 사람) | **{auc['overall']}** | {op['overall']['best_youden_threshold']} | {op['overall']['best_youden_j']} |",
            f"| 동일 카메라 내 (Same-Camera) | **{auc['same_camera']}** | {op['same_camera']['best_youden_threshold']} | {op['same_camera']['best_youden_j']} |",
            f"| 카메라 간 (Cross-Camera) | **{auc['cross_camera']}** | {op['cross_camera']['best_youden_threshold']} | {op['cross_camera']['best_youden_j']} |",
            f"| 공통 가시 부위 충분 (`co_visibility ≥ 0.65`) | **{auc['high_covisibility_ge_065']}** | - | - |",
            f"| 부분 가림 (`co_visibility < 0.65`) | **{auc['low_covisibility_lt_065']}** | - | - |",
            "",
            "## 4. 카메라별 가시성·얼굴 게이트·분리도",
            "",
            "| 카메라 | Crop 수 | 평균 PSS 점수 | 전신 가시 비율 | 몸통 가시 비율 | 얼굴 게이트 통과율 (`≥40px`·정면) | 얼굴 크기 중앙값 (px) | 동일 카메라 AUC |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    for cam_id, cinfo in main_eval["by_camera"].items():
        lines.append(
            f"| `{cam_id}` | {cinfo['crop_count']} | {cinfo['mean_p_score']} | {cinfo['full_body_visible_ratio']} | "
            f"{cinfo['torso_visible_ratio']} | {cinfo['face_gate_pass_ratio']} | {cinfo['face_size_px_p50']} | **{cinfo['auc']}** |"
        )

    lines.extend(
        [
            "",
            "## 5. 카메라 쌍별 교차 유사도 및 분리도",
            "",
            "| 카메라 쌍 | 같은 사람 평균 (p50) | 다른 사람 평균 (p50) | 평균 마진 (Same − Diff) | Cross-Cam ROC-AUC |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for pair_key, pinfo in main_eval["by_camera_pair"].items():
        s_sim = pinfo["same_person_sim"]
        d_sim = pinfo["diff_person_sim"]
        lines.append(
            f"| `{pair_key}` | {s_sim['mean']} ({s_sim['p50']}) [n={s_sim['count']}] | "
            f"{d_sim['mean']} ({d_sim['p50']}) [n={d_sim['count']}] | **{pinfo['mean_margin']}** | **{pinfo['auc']}** |"
        )

    gsim = main_eval["gallery_simulation"]
    lines.extend(
        [
            "",
            "## 6. 갤러리 매칭(`max` vs `mean`) 및 `E0_baseline` 분할 트랙 재연결 모의실험",
            "",
            f"- **주요 World Track 갤러리 Top-1 매칭 (`{gsim['evaluated_queries']}`회 질의)**:",
            f"  - `max` 유사도 Top-1 정확도: **`{gsim['top1_accuracy_max']}`** (중앙 마진 `{gsim['margin_max_p50']}`)",
            f"  - `mean` 유사도 Top-1 정확도: **`{gsim['top1_accuracy_mean']}`** (중앙 마진 `{gsim['margin_mean_p50']}`)",
            f"- **`E0_baseline` 분할 조각(`{frag_eval['evaluated_fragments']}`개 fragment) 재연결 평가**:",
            f"  - 같은 사람 조각 간 유사도 중앙값: **`{frag_eval['same_person_fragment_sim']['p50']}`** (평균 `{frag_eval['same_person_fragment_sim']['mean']}`)",
            f"  - 다른 사람 조각 간 유사도 중앙값: **`{frag_eval['diff_person_fragment_sim']['p50']}`** (평균 `{frag_eval['diff_person_fragment_sim']['mean']}`)",
            f"  - 조각 간 재연결 ROC-AUC: **`{frag_eval['fragment_pair_auc']}`**",
            f"  - 신규 조각 등장 시 기존 갤러리 Top-1 매칭 정확도 (`{frag_eval['new_fragment_top1_queries']}`회): `max={frag_eval['new_fragment_top1_accuracy_max']}`, `mean={frag_eval['new_fragment_top1_accuracy_mean']}`",
            "",
            "## 7. 노트북 GPU (RTX 4050) 추론 지연시간 및 VRAM 사용량",
            "",
            f"- 모델 초기 로드 시간: `{bench['load_time_ms']} ms`, 가중치 상주 VRAM: `{bench['model_allocated_mb']} MB`, 최대 피크 VRAM: **`{bench['vram_peak_mb']} MB`**",
            "",
            "| 배치 크기 | 추론 p50 (ms) | 추론 p95 (ms) | 추론 p99 (ms) | Crop 당 p50 (ms) | 처리량 (crops/s) | PSS 포함 p50 (ms) | 피크 할당 VRAM (MB) |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    for bs_key, binfo in bench["batches"].items():
        em = binfo["embed_ms"]
        pm = binfo["embed_with_pss_ms"]
        lines.append(
            f"| **{bs_key}** | {em['p50']} | {em['p95']} | {em['p99']} | **{binfo['embed_per_crop_p50_ms']}** | "
            f"{binfo['embed_throughput_crops_per_s']} | {pm['p50']} | {binfo['vram_peak_allocated_mb']} |"
        )

    if calwalk_eval.get("available"):
        cw_eval = calwalk_eval["evaluation"]
        cw_dist = cw_eval["distributions"]
        cw_auc = cw_eval["auc"]
        lines.extend(
            [
                "",
                "## 8. 교차 검증: `session11/rec-calwalk` (11차 세션 녹화, 다인 포함)",
                "",
                f"- 추출 crop 수: `{cw_eval['total_crops']}`개 (4대 카메라, `{calwalk_eval['frame_stride']}`프레임 간격)",
                f"- 동일 카메라 같은 사람 유사도 중앙값(p50): **`{cw_dist['same_person_local_same_cam']['p50']}`** (평균 `{cw_dist['same_person_local_same_cam']['mean']}`, n={cw_dist['same_person_local_same_cam']['count']})",
                f"- 동일 카메라 다른 사람(동시 검출) 유사도 중앙값(p50): **`{cw_dist['diff_person_same_cam']['p50']}`** (평균 `{cw_dist['diff_person_same_cam']['mean']}`, n={cw_dist['diff_person_same_cam']['count']})",
                f"- 분리도(ROC-AUC): 전체 **`{cw_auc['same_camera']}`** (공가시성 `≥0.65` 구간 **`{cw_auc['high_covisibility_ge_065']}`**, 부분 가림 `<0.65` 구간 **`{cw_auc['low_covisibility_lt_065']}`**)",
            ]
        )

    lines.append("")
    return "\n".join(lines)


def write_run_record(
    out_dir: Path,
    metrics: Mapping[str, Any],
    *,
    cope_weights: Path,
    started_at_ms: int,
    finished_at_ms: int,
    owner: str = "Jonghwa Park",
) -> dict[str, Any]:
    """`contracts/experiments/run_record.schema.json` 형식의 실험 기록을 생성하고 스키마 검증한다."""
    commit = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--", "ai"))
    try:
        freeze = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        freeze = ""
    if not freeze:
        freeze = subprocess.run(
            ["uv", "pip", "freeze", "--python", sys.executable],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    env_hash = "sha256:" + hashlib.sha256(freeze.encode()).hexdigest()

    main_eval = metrics["main_recording_1029"]["evaluation"]
    frag_eval = metrics["main_recording_1029"]["fragment_reassociation"]
    bench = metrics["gpu_benchmark"]
    script_path = Path(__file__).resolve()

    flat_metrics: dict[str, Any] = {
        "total_crops_1029": main_eval["total_crops"],
        "same_person_same_cam_sim_p50": main_eval["distributions"]["same_person_local_same_cam"]["p50"],
        "same_person_cross_cam_sim_p50": main_eval["distributions"]["same_person_cross_cam"]["p50"],
        "diff_person_all_sim_p50": main_eval["distributions"]["diff_person_all"]["p50"],
        "diff_person_cross_cam_sim_p50": main_eval["distributions"]["diff_person_cross_cam"]["p50"],
        "roc_auc_overall": main_eval["auc"]["overall"],
        "roc_auc_same_camera": main_eval["auc"]["same_camera"],
        "roc_auc_cross_camera": main_eval["auc"]["cross_camera"],
        "roc_auc_high_covisibility": main_eval["auc"]["high_covisibility_ge_065"],
        "gallery_top1_accuracy_max": main_eval["gallery_simulation"]["top1_accuracy_max"],
        "gallery_top1_accuracy_mean": main_eval["gallery_simulation"]["top1_accuracy_mean"],
        "fragment_reassociation_auc": frag_eval["fragment_pair_auc"],
        "fragment_top1_accuracy_max": frag_eval["new_fragment_top1_accuracy_max"],
        "cope_b1_p50_ms": bench["batches"]["1"]["embed_ms"]["p50"],
        "cope_b8_p50_ms": bench["batches"]["8"]["embed_ms"]["p50"],
        "cope_b16_p50_ms": bench["batches"]["16"]["embed_ms"]["p50"],
    }

    metrics_path = out_dir / "metrics.json"
    summary_path = out_dir / "summary.md"

    rec: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"reid463-cope-eval-{commit[:8]}",
        "experiment_id": "VAL-01",
        "jira_key": "S15P31A101-463",
        "owner": owner,
        "git_commit": commit,
        "branch": branch,
        "dirty_worktree": dirty,
        "env_hash": env_hash,
        "model_version": "COPE ViT-B/16 CLIP (cope_msmt17.pth, 1280-D, 256x128) + YOLO26x-pose cache-s2",
        "weights_hash": _sha256_file(cope_weights),
        "config": {
            "path": script_path.relative_to(REPO).as_posix(),
            "sha256": _sha256_file(script_path),
            "overrides": {
                "batch_sizes": [1, 8, 16],
                "input_size_hw": [256, 128],
                "min_face_size_px": 40,
            },
        },
        "input_provenance": {
            "frames": {
                "source": "FILE",
                "dataset_id": "recording-dev_test/rec-20261029-015512z-lobby-railing-pose",
                "camera_ids": ["cam_A", "cam_B", "cam_C", "cam_D"],
            },
            "calibration": {
                "source": "G1_AUTO",
                "calibration_version": metrics["main_recording_1029"]["reference_replay"]["calibration_version"],
            },
            "is_upper_bound": False,
        },
        "gpu": _gpu_info(),
        "started_at": started_at_ms,
        "finished_at": finished_at_ms,
        "status": "SUCCEEDED",
        "experiment_state": "BASELINE",
        "metrics": flat_metrics,
        "performance": {
            "stage_latency_ms": bench["stage_latency_ms"],
            "vram_peak_mb": bench["vram_peak_mb"],
            "throughput": bench["batches"]["8"]["embed_throughput_crops_per_s"],
        },
        "results": [
            {
                "kind": "REPORT",
                "uri": f"local://laptop/{metrics_path.resolve().as_posix()}",
                "sha256": _sha256_file(metrics_path),
            },
            {
                "kind": "REPORT",
                "uri": f"local://laptop/{summary_path.resolve().as_posix()}",
                "sha256": _sha256_file(summary_path),
            },
        ],
        "notes": "Re-ID 1단계(S15P31A101-463): COPE 외형 특징 추출 모듈 오프라인 유사도 분포·분리도(ROC-AUC) 평가 및 RTX 4050 배치별 지연시간 측정. 원본/crop 이미지는 저장하지 않음.",
    }

    try:
        import jsonschema
        from referencing import Registry, Resource

        contracts_dir = REPO / "contracts"
        registry = Registry()
        target_schema = None
        for p in contracts_dir.rglob("*.schema.json"):
            sc = json.loads(p.read_text(encoding="utf-8"))
            registry = registry.with_resource(sc["$id"], Resource.from_contents(sc))
            if p.name == "run_record.schema.json":
                target_schema = sc
        if target_schema is not None:
            validator = jsonschema.Draft202012Validator(target_schema, registry=registry)
            errors = [e.message for e in validator.iter_errors(rec)]
            if errors:
                raise ValueError(f"run_record.schema.json 검증 실패: {errors[:3]}")
    except ImportError:
        pass

    run_record_path = out_dir / "run_record.json"
    run_record_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session-1029", type=Path, default=DEFAULT_SESSION_1029)
    ap.add_argument("--cache-1029", type=Path, default=DEFAULT_CACHE_1029)
    ap.add_argument("--calib-ref", type=Path, default=DEFAULT_CALIB_REF)
    ap.add_argument("--calib-base", type=Path, default=DEFAULT_CALIB_BASE)
    ap.add_argument("--calwalk-dir", type=Path, default=DEFAULT_CALWALK_DIR)
    ap.add_argument("--yolo-weights", type=Path, default=DEFAULT_YOLO_WEIGHTS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--from-existing-metrics", action="store_true")
    args = ap.parse_args(argv)

    if args.from_existing_metrics:
        metrics_path = args.out / "metrics.json"
        metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        summary_path = args.out / "summary.md"
        summary_path.write_text(build_summary_markdown(metrics_payload), encoding="utf-8")
        finished_at_ms = int(metrics_path.stat().st_mtime * 1000)
        started_at_ms = finished_at_ms - int(float(metrics_payload.get("elapsed_total_s", 1.0)) * 1000)
        write_run_record(
            args.out,
            metrics_payload,
            cope_weights=Path(metrics_payload["model"]["cope_weights_path"]),
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
        )
        print(f"기존 metrics.json 으로부터 summary.md 및 run_record.json 갱신 완료: {args.out}", flush=True)
        return 0

    started_at_ms = int(time.time() * 1000)
    t_wall0 = time.perf_counter()

    print("[1/5] RTX 4050 COPE 배치(1·8·16) 지연시간 및 VRAM 벤치마크 측정 중...", flush=True)
    bench = benchmark_cope_embedder(batch_sizes=(1, 8, 16), device=args.device)
    print(
        f"      완료: device={bench['device']} ({bench['gpu_name']}), "
        f"b1 p50={bench['batches']['1']['embed_ms']['p50']}ms, "
        f"b8 p50={bench['batches']['8']['embed_ms']['p50']}ms, "
        f"b16 p50={bench['batches']['16']['embed_ms']['p50']}ms, "
        f"peak VRAM={bench['vram_peak_mb']}MB",
        flush=True,
    )

    print("[2/5] 10/29 녹화 검출 캐시(cache-s2) 추적 재생 매핑 중...", flush=True)
    cache = load_cache(args.cache_1029)
    frame_local_boxes, ref_timeline, _, ref_summary, recorded_polls = replay_world_assignments(
        cache,
        args.calib_ref,
        suppress_split=True,
        session_tag="reidref",
    )
    _, base_timeline, _, base_summary, _ = replay_world_assignments(
        cache,
        args.calib_base,
        suppress_split=False,
        session_tag="reidbase",
        pretracked_polls=recorded_polls,
    )
    print(
        f"      완료: 참조 World ID {ref_summary['canonical_world_ids']}개, "
        f"기준선(E0) World ID {base_summary['issued_world_ids']}개",
        flush=True,
    )

    print("[3/5] 10/29 녹화 키프레임 디코딩 및 COPE 특징·가시성·얼굴 게이트 평가 중...", flush=True)
    cope_cfg = COPEConfig.from_env(device=args.device, batch_size=16, input_format="rgb")  # crop 은 RGB(S15P31A101-477)
    embedder = COPEEmbedder(cope_cfg)
    face_embedder = create_face_embedder(FaceConfig.from_env(min_face_size_px=40))

    records_1029, base_world_by_obs, extract_stats = decode_keyframes_and_extract_records(
        args.session_1029,
        cache,
        frame_local_boxes,
        ref_timeline,
        base_timeline,
        embedder,
        face_embedder,
    )
    eval_1029 = evaluate_observation_records(records_1029, part_weights=PartWeights())
    frag_1029 = evaluate_fragment_reassociation(records_1029, base_world_by_obs)
    print(
        f"      완료: crop {eval_1029['total_crops']}개, "
        f"Overall AUC={eval_1029['auc']['overall']}, "
        f"Same-Cam AUC={eval_1029['auc']['same_camera']}, "
        f"Cross-Cam AUC={eval_1029['auc']['cross_camera']}, "
        f"Fragment Reassoc AUC={frag_1029['fragment_pair_auc']}",
        flush=True,
    )

    print("[4/5] 11차 세션 녹화(session11/rec-calwalk) 교차 검증 중...", flush=True)
    calwalk_res = evaluate_calwalk_recording(
        args.calwalk_dir,
        args.yolo_weights,
        embedder,
        face_embedder,
        frame_stride=25,
        max_sampled_steps=60,
    )
    if calwalk_res.get("available"):
        cw_crops = calwalk_res["evaluation"]["total_crops"]
        print(f"      완료: rec-calwalk crop {cw_crops}개 추출 및 분석", flush=True)
    else:
        print("      건너뜀: rec-calwalk 또는 가중치 경로 없음", flush=True)

    print(f"[5/5] 결과 파일 기록 중 ({args.out})...", flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    elapsed_total_s = round(time.perf_counter() - t_wall0, 2)

    metrics_payload: dict[str, Any] = {
        "ticket": "S15P31A101-463",
        "elapsed_total_s": elapsed_total_s,
        "model": {
            "name": "COPE (ViT-B/16 CLIP + CV/Prompt + Part-Map)",
            "embedding_dim": embedder.embedding_dim,
            "input_size_hw": list(cope_cfg.input_size_hw),
            "cope_weights_path": cope_cfg.weights_path.as_posix(),
            "cope_weights_sha256": _sha256_file(cope_cfg.weights_path),
            "clip_weights_path": cope_cfg.clip_path.as_posix(),
            "clip_weights_sha256": _sha256_file(cope_cfg.clip_path),
            "face_backend_available": face_embedder.available,
            "min_face_size_px": face_embedder.config.min_face_size_px,
        },
        "gpu_benchmark": bench,
        "main_recording_1029": {
            "session_id": "rec-20261029-015512z-lobby-railing-pose",
            "extract_stats": extract_stats,
            "reference_replay": ref_summary,
            "baseline_replay": base_summary,
            "evaluation": eval_1029,
            "fragment_reassociation": frag_1029,
        },
        "calwalk_session11": calwalk_res,
    }

    metrics_path = args.out / "metrics.json"
    summary_path = args.out / "summary.md"
    metrics_path.write_text(json.dumps(metrics_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md = build_summary_markdown(metrics_payload)
    summary_path.write_text(summary_md, encoding="utf-8")

    finished_at_ms = int(time.time() * 1000)
    write_run_record(
        args.out,
        metrics_payload,
        cope_weights=cope_cfg.weights_path,
        started_at_ms=started_at_ms,
        finished_at_ms=finished_at_ms,
    )
    print(f"모든 평가 및 기록 완료 ({elapsed_total_s}s): {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
