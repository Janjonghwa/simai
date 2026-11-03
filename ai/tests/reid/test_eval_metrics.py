"""오프라인 Re-ID 유사도 분포 및 ROC-AUC 지표 계산 단위 시험 (S15P31A101-463)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

AI_DIR = Path(__file__).resolve().parents[2]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from experiments.reid_eval.metrics import (
    CropObservationRecord,
    distribution_summary,
    evaluate_observation_records,
    roc_auc_score,
    threshold_operating_points,
)
from simai_ai.reid import PartVisibility


def _unit_vec(dim: int, idx_a: int, idx_b: int, weight_b: float) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[idx_a] = 1.0
    v[idx_b] = weight_b
    return v / float(np.linalg.norm(v))


def test_roc_auc_score_basic() -> None:
    assert roc_auc_score([0.8, 0.9, 0.95], [0.1, 0.2, 0.3]) == 1.0
    assert roc_auc_score([0.1, 0.2], [0.8, 0.9]) == 0.0
    assert roc_auc_score([0.5, 0.5], [0.5, 0.5]) == 0.5
    assert roc_auc_score([], [0.1, 0.2]) is None


def test_distribution_and_operating_points() -> None:
    empty = distribution_summary([])
    assert empty["count"] == 0
    assert empty["p50"] is None

    summary = distribution_summary([0.2, 0.4, 0.6, 0.8, 1.0])
    assert summary["count"] == 5
    assert summary["p50"] == 0.6

    op = threshold_operating_points([0.75, 0.82, 0.91], [0.20, 0.31, 0.44])
    assert op["best_youden_j"] == 1.0
    assert op["best_youden_threshold"] is not None


def test_evaluate_observation_records_separates_same_and_diff_person() -> None:
    vis_full = PartVisibility.all_visible()
    records: list[CropObservationRecord] = []
    obs_id = 0

    # 두 사람(w-1, w-2)이 두 카메라(cam_A, cam_B)에서 0s, 2s, 4s, 6s 에 관측됨
    for step, ts_ms in enumerate((1000, 3000, 5000, 7000)):
        for cam_idx, cam_id in enumerate(("cam_A", "cam_B")):
            emb_p1 = _unit_vec(16, 0, 1 + step, 0.08 * (cam_idx + 1))
            emb_p2 = _unit_vec(16, 8, 9 + step, 0.08 * (cam_idx + 1))
            records.append(
                CropObservationRecord(
                    obs_id=obs_id,
                    camera_id=cam_id,
                    frame_sequence=step * 50,
                    timestamp_ms=ts_ms,
                    local_track_id=f"{cam_id}_t1",
                    world_id="w-1",
                    bbox_xyxy=(100.0, 100.0, 220.0, 420.0),
                    det_score=0.92,
                    visibility=vis_full,
                    face_min_side_px=45.0,
                    face_passes_gate=True,
                    p_score=0.88,
                    embedding=emb_p1,
                )
            )
            obs_id += 1
            records.append(
                CropObservationRecord(
                    obs_id=obs_id,
                    camera_id=cam_id,
                    frame_sequence=step * 50,
                    timestamp_ms=ts_ms,
                    local_track_id=f"{cam_id}_t2",
                    world_id="w-2",
                    bbox_xyxy=(500.0, 110.0, 620.0, 430.0),
                    det_score=0.90,
                    visibility=vis_full,
                    face_min_side_px=32.0,
                    face_passes_gate=False,
                    p_score=0.85,
                    embedding=emb_p2,
                )
            )
            obs_id += 1

    res = evaluate_observation_records(records)
    assert res["total_crops"] == 16
    assert res["auc"]["overall"] == 1.0
    assert res["auc"]["same_camera"] == 1.0
    assert res["auc"]["cross_camera"] == 1.0
    assert "cam_A-cam_B" in res["by_camera_pair"]
