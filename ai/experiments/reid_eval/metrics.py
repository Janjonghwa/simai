"""Re-ID 유사도 분포, 분리도(ROC-AUC), 카메라별·카메라쌍별 분석 및 갤러리 모의 평가 (S15P31A101-463)."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from simai_ai.reid import (
    GalleryConfig,
    PartVisibility,
    PartWeights,
    TrackGallery,
    co_visibility_confidence,
)


@dataclass(frozen=True)
class CropObservationRecord:
    """오프라인 평가용 단일 사람 crop 특징·메타데이터 기록 (이미지는 저장하지 않음)."""

    obs_id: int
    camera_id: str
    frame_sequence: int
    timestamp_ms: int
    local_track_id: str
    world_id: str | None
    bbox_xyxy: tuple[float, float, float, float]
    det_score: float
    visibility: PartVisibility
    face_min_side_px: float | None
    face_passes_gate: bool
    p_score: float
    embedding: np.ndarray


def roc_auc_score(pos_scores: Sequence[float], neg_scores: Sequence[float]) -> float | None:
    """양성(같은 사람) 점수와 음성(다른 사람) 점수로 ROC-AUC 를 계산한다 (Mann-Whitney U 통계량)."""
    n_pos = len(pos_scores)
    n_neg = len(neg_scores)
    if n_pos == 0 or n_neg == 0:
        return None

    combined = [(float(s), 1) for s in pos_scores] + [(float(s), 0) for s in neg_scores]
    combined.sort(key=lambda x: x[0])

    rank_sum_pos = 0.0
    n_total = len(combined)
    i = 0
    while i < n_total:
        j = i + 1
        while j < n_total and combined[j][0] == combined[i][0]:
            j += 1
        # 1-based 평균 순위: ((i + 1) + j) / 2
        avg_rank = 0.5 * ((i + 1) + j)
        pos_in_tie = sum(label for _, label in combined[i:j])
        rank_sum_pos += pos_in_tie * avg_rank
        i = j

    u_stat = rank_sum_pos - (n_pos * (n_pos + 1)) / 2.0
    auc = u_stat / (n_pos * n_neg)
    return round(float(max(0.0, min(1.0, auc))), 4)


def distribution_summary(values: Sequence[float]) -> dict[str, Any]:
    """수치 목록의 평균·표준편차·분위수(p10, p25, p50, p75, p90) 요약을 만든다."""
    if not values:
        return {
            "count": 0,
            "mean": None,
            "std": None,
            "min": None,
            "p10": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "p90": None,
            "max": None,
        }
    arr = np.asarray(values, dtype=np.float64)
    q10, q25, q50, q75, q90 = np.percentile(arr, [10, 25, 50, 75, 90])
    return {
        "count": int(arr.size),
        "mean": round(float(np.mean(arr)), 4),
        "std": round(float(np.std(arr)), 4),
        "min": round(float(np.min(arr)), 4),
        "p10": round(float(q10), 4),
        "p25": round(float(q25), 4),
        "p50": round(float(q50), 4),
        "p75": round(float(q75), 4),
        "p90": round(float(q90), 4),
        "max": round(float(np.max(arr)), 4),
    }


def threshold_operating_points(
    pos_scores: Sequence[float],
    neg_scores: Sequence[float],
    thresholds: Sequence[float] = (0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75),
) -> dict[str, Any]:
    """문턱값별 TPR(재현율), FPR(오탐률), 정밀도, F1 과 최적 동작점을 계산한다."""
    if not pos_scores or not neg_scores:
        return {"points": [], "best_youden_threshold": None}

    pos_arr = np.asarray(pos_scores, dtype=np.float64)
    neg_arr = np.asarray(neg_scores, dtype=np.float64)
    points: list[dict[str, float]] = []
    best_th = thresholds[0]
    best_j = -1.0

    for th in thresholds:
        tp = int(np.sum(pos_arr >= th))
        fn = int(pos_arr.size - tp)
        fp = int(np.sum(neg_arr >= th))
        tn = int(neg_arr.size - fp)
        tpr = tp / max(1, tp + fn)
        fpr = fp / max(1, fp + tn)
        j = tpr - fpr
        if j > best_j:
            best_j = j
            best_th = th
        points.append(
            {
                "threshold": round(float(th), 3),
                "tpr": round(float(tpr), 4),
                "fpr": round(float(fpr), 4),
                "youden_j": round(float(j), 4),
            }
        )

    return {
        "points": points,
        "best_youden_threshold": round(float(best_th), 3),
        "best_youden_j": round(float(best_j), 4),
    }


def evaluate_observation_records(
    records: Sequence[CropObservationRecord],
    *,
    same_cam_min_gap_ms: int = 1_500,
    same_cam_max_gap_ms: int = 20_000,
    cross_cam_max_gap_ms: int = 2_500,
    simultaneous_gap_ms: int = 400,
    part_weights: PartWeights | None = None,
) -> dict[str, Any]:
    """추출된 관측 특징 목록으로 같은 사람 대 다른 사람 유사도 분포·AUC·카메라별 차이를 분석한다."""
    weights = part_weights or PartWeights()
    n = len(records)
    if n == 0:
        return {"total_crops": 0}

    embs = np.stack([r.embedding for r in records], axis=0).astype(np.float32)

    same_local_sims: list[float] = []
    same_world_all_sims: list[float] = []
    same_cross_cam_sims: list[float] = []
    diff_all_sims: list[float] = []
    diff_same_cam_sims: list[float] = []
    diff_cross_cam_sims: list[float] = []

    high_covis_pos: list[float] = []
    high_covis_neg: list[float] = []
    low_covis_pos: list[float] = []
    low_covis_neg: list[float] = []

    cam_same_sims: dict[str, list[float]] = defaultdict(list)
    cam_diff_sims: dict[str, list[float]] = defaultdict(list)
    pair_same_sims: dict[str, list[float]] = defaultdict(list)
    pair_diff_sims: dict[str, list[float]] = defaultdict(list)

    # 1. 같은 카메라·같은 로컬 트랙 (시간 간격 [same_cam_min_gap_ms, same_cam_max_gap_ms])
    by_local: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, r in enumerate(records):
        by_local[(r.camera_id, r.local_track_id)].append(idx)

    for (cam, _ltid), idxs in by_local.items():
        for a_pos in range(len(idxs)):
            i = idxs[a_pos]
            ri = records[i]
            for b_pos in range(a_pos + 1, len(idxs)):
                j = idxs[b_pos]
                rj = records[j]
                dt = abs(rj.timestamp_ms - ri.timestamp_ms)
                if dt < same_cam_min_gap_ms:
                    continue
                if dt > same_cam_max_gap_ms:
                    break
                sim = float(np.dot(embs[i], embs[j]))
                same_local_sims.append(sim)
                cam_same_sims[cam].append(sim)
                if ri.world_id is not None and ri.world_id == rj.world_id:
                    same_world_all_sims.append(sim)
                covis = co_visibility_confidence(ri.visibility, rj.visibility, weights)
                if covis >= 0.65:
                    high_covis_pos.append(sim)
                else:
                    low_covis_pos.append(sim)

    # 2. 카메라 간 같은 사람 (같은 World ID, 시간차 <= cross_cam_max_gap_ms)
    by_world: dict[str, list[int]] = defaultdict(list)
    for idx, r in enumerate(records):
        if r.world_id is not None:
            by_world[r.world_id].append(idx)

    for _wid, idxs in by_world.items():
        for a_pos in range(len(idxs)):
            i = idxs[a_pos]
            ri = records[i]
            for b_pos in range(a_pos + 1, len(idxs)):
                j = idxs[b_pos]
                rj = records[j]
                if ri.camera_id == rj.camera_id:
                    continue
                dt = abs(rj.timestamp_ms - ri.timestamp_ms)
                if dt > cross_cam_max_gap_ms:
                    continue
                sim = float(np.dot(embs[i], embs[j]))
                same_cross_cam_sims.append(sim)
                same_world_all_sims.append(sim)
                c1, c2 = sorted((ri.camera_id, rj.camera_id))
                pair_same_sims[f"{c1}-{c2}"].append(sim)
                covis = co_visibility_confidence(ri.visibility, rj.visibility, weights)
                if covis >= 0.65:
                    high_covis_pos.append(sim)
                else:
                    low_covis_pos.append(sim)

    # 3. 다른 사람 (동시 관측: 같은 프레임의 서로 다른 검출 또는 동시 활성 서로 다른 World ID)
    # 시간순 인덱스로 simultaneous_gap_ms 안의 쌍만 검사한다.
    sorted_indices = sorted(range(n), key=lambda k: records[k].timestamp_ms)
    for pos_a in range(n):
        i = sorted_indices[pos_a]
        ri = records[i]
        for pos_b in range(pos_a + 1, n):
            j = sorted_indices[pos_b]
            rj = records[j]
            dt = rj.timestamp_ms - ri.timestamp_ms
            if dt > simultaneous_gap_ms:
                break

            is_diff_person = False
            if ri.camera_id == rj.camera_id and ri.frame_sequence == rj.frame_sequence and ri.local_track_id != rj.local_track_id:
                # 같은 카메라 같은 프레임의 두 검출은 물리적으로 반드시 서로 다른 사람이다.
                is_diff_person = True
            elif (
                ri.world_id is not None
                and rj.world_id is not None
                and ri.world_id != rj.world_id
                and ri.local_track_id != rj.local_track_id
            ):
                is_diff_person = True

            if not is_diff_person:
                continue

            sim = float(np.dot(embs[i], embs[j]))
            diff_all_sims.append(sim)
            covis = co_visibility_confidence(ri.visibility, rj.visibility, weights)
            if covis >= 0.65:
                high_covis_neg.append(sim)
            else:
                low_covis_neg.append(sim)

            if ri.camera_id == rj.camera_id:
                diff_same_cam_sims.append(sim)
                cam_diff_sims[ri.camera_id].append(sim)
            else:
                diff_cross_cam_sims.append(sim)
                c1, c2 = sorted((ri.camera_id, rj.camera_id))
                pair_diff_sims[f"{c1}-{c2}"].append(sim)

    # 4. 카메라별 요약
    cameras = sorted({r.camera_id for r in records})
    by_camera: dict[str, Any] = {}
    for cam in cameras:
        cam_recs = [r for r in records if r.camera_id == cam]
        full_vis = sum(1 for r in cam_recs if r.visibility.head and r.visibility.torso and r.visibility.legs)
        torso_vis = sum(1 for r in cam_recs if r.visibility.torso)
        face_pass = sum(1 for r in cam_recs if r.face_passes_gate)
        face_sizes = [r.face_min_side_px for r in cam_recs if r.face_min_side_px is not None]
        pos_c = cam_same_sims.get(cam, [])
        neg_c = cam_diff_sims.get(cam, [])
        by_camera[cam] = {
            "crop_count": len(cam_recs),
            "unique_local_tracks": len({r.local_track_id for r in cam_recs}),
            "mean_p_score": round(float(np.mean([r.p_score for r in cam_recs])), 4) if cam_recs else None,
            "full_body_visible_ratio": round(full_vis / len(cam_recs), 4) if cam_recs else 0.0,
            "torso_visible_ratio": round(torso_vis / len(cam_recs), 4) if cam_recs else 0.0,
            "face_gate_pass_ratio": round(face_pass / len(cam_recs), 4) if cam_recs else 0.0,
            "face_size_px_p50": round(float(np.median(face_sizes)), 2) if face_sizes else None,
            "same_person_sim": distribution_summary(pos_c),
            "diff_person_sim": distribution_summary(neg_c),
            "auc": roc_auc_score(pos_c, neg_c),
        }

    # 5. 카메라 쌍별 요약
    all_pair_keys = sorted(set(pair_same_sims) | set(pair_diff_sims))
    by_camera_pair: dict[str, Any] = {}
    for pk in all_pair_keys:
        pos_p = pair_same_sims.get(pk, [])
        neg_p = pair_diff_sims.get(pk, [])
        pos_sum = distribution_summary(pos_p)
        neg_sum = distribution_summary(neg_p)
        mean_gap = (
            round(pos_sum["mean"] - neg_sum["mean"], 4)
            if pos_sum["mean"] is not None and neg_sum["mean"] is not None
            else None
        )
        by_camera_pair[pk] = {
            "same_person_sim": pos_sum,
            "diff_person_sim": neg_sum,
            "mean_margin": mean_gap,
            "auc": roc_auc_score(pos_p, neg_p),
        }

    # 6. 갤러리 매칭(max 대 mean) 시뮬레이션
    gallery_sim = _simulate_track_galleries(records)

    pos_primary = same_world_all_sims if same_world_all_sims else same_local_sims
    return {
        "total_crops": n,
        "cameras": cameras,
        "distributions": {
            "same_person_local_same_cam": distribution_summary(same_local_sims),
            "same_person_world_all": distribution_summary(same_world_all_sims),
            "same_person_cross_cam": distribution_summary(same_cross_cam_sims),
            "diff_person_all": distribution_summary(diff_all_sims),
            "diff_person_same_cam": distribution_summary(diff_same_cam_sims),
            "diff_person_cross_cam": distribution_summary(diff_cross_cam_sims),
        },
        "auc": {
            "overall": roc_auc_score(pos_primary, diff_all_sims),
            "same_camera": roc_auc_score(same_local_sims, diff_same_cam_sims),
            "cross_camera": roc_auc_score(same_cross_cam_sims, diff_cross_cam_sims),
            "high_covisibility_ge_065": roc_auc_score(high_covis_pos, high_covis_neg),
            "low_covisibility_lt_065": roc_auc_score(low_covis_pos, low_covis_neg),
        },
        "operating_points": {
            "overall": threshold_operating_points(pos_primary, diff_all_sims),
            "same_camera": threshold_operating_points(same_local_sims, diff_same_cam_sims),
            "cross_camera": threshold_operating_points(same_cross_cam_sims, diff_cross_cam_sims),
        },
        "by_camera": by_camera,
        "by_camera_pair": by_camera_pair,
        "gallery_simulation": gallery_sim,
    }


def _simulate_track_galleries(records: Sequence[CropObservationRecord]) -> dict[str, Any]:
    """다인 구간에서 World Track 갤러리(max 대 mean 유사도) Top-1 매칭 정확도와 마진을 측정한다."""
    world_counts: dict[str, int] = defaultdict(int)
    for r in records:
        if r.world_id is not None:
            world_counts[r.world_id] += 1

    # 충분히 오래 관측된 주요 World Track (최소 20회 이상 관측)만 대상으로 평가한다.
    major_tracks = [wid for wid, cnt in sorted(world_counts.items(), key=lambda x: -x[1]) if cnt >= 20]
    if len(major_tracks) < 2:
        return {"evaluated_queries": 0, "top1_accuracy_max": None, "top1_accuracy_mean": None}

    major_set = set(major_tracks)
    filtered = [r for r in records if r.world_id in major_set]
    filtered.sort(key=lambda r: (r.timestamp_ms, r.camera_id))

    galleries: dict[str, TrackGallery] = {
        wid: TrackGallery(wid, GalleryConfig(max_entries_per_track=16, max_entries_per_slot=2))
        for wid in major_tracks
    }

    queries = 0
    hits_max = 0
    hits_mean = 0
    margins_max: list[float] = []
    margins_mean: list[float] = []

    for r in filtered:
        assert r.world_id is not None
        # 모든 주요 트랙 갤러리에 최소 2개 이상 표본이 쌓인 뒤부터 질의 평가한 후 갱신한다.
        if all(len(g) >= 2 for g in galleries.values()):
            scores_max: dict[str, float] = {}
            scores_mean: dict[str, float] = {}
            for wid, g in galleries.items():
                res = g.compute_similarity(
                    r.embedding,
                    query_visibility=r.visibility,
                    query_camera_id=r.camera_id,
                    mode="max",
                )
                scores_max[wid] = res.max_similarity
                scores_mean[wid] = res.mean_similarity

            best_max_wid = max(scores_max, key=lambda k: scores_max[k])
            best_mean_wid = max(scores_mean, key=lambda k: scores_mean[k])
            queries += 1
            if best_max_wid == r.world_id:
                hits_max += 1
            if best_mean_wid == r.world_id:
                hits_mean += 1

            other_max = max(v for k, v in scores_max.items() if k != r.world_id)
            other_mean = max(v for k, v in scores_mean.items() if k != r.world_id)
            margins_max.append(scores_max[r.world_id] - other_max)
            margins_mean.append(scores_mean[r.world_id] - other_mean)

        # 관측 위치를 해시해 의사 방향 bin 으로 넣어 다양한 시점 슬롯을 채운다.
        cx = 0.5 * (r.bbox_xyxy[0] + r.bbox_xyxy[2])
        orient_bin = int(cx // 288) % 8
        galleries[r.world_id].add_observation(
            r.embedding,
            timestamp_ms=r.timestamp_ms,
            camera_id=r.camera_id,
            orientation_bin=orient_bin,
            visibility=r.visibility,
            quality_score=r.p_score,
        )

    return {
        "major_world_tracks": major_tracks,
        "evaluated_queries": queries,
        "top1_accuracy_max": round(hits_max / queries, 4) if queries else None,
        "top1_accuracy_mean": round(hits_mean / queries, 4) if queries else None,
        "margin_max_p50": round(float(np.median(margins_max)), 4) if margins_max else None,
        "margin_mean_p50": round(float(np.median(margins_mean)), 4) if margins_mean else None,
    }
