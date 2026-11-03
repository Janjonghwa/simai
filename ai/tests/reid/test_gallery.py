"""트랙별 다시점(카메라·방향 bin) 외형 갤러리, 교체 규칙, DORMANT 보관 시간 시험 (S15P31A101-463)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from simai_ai.reid import (
    GalleryConfig,
    PartVisibility,
    ReidGalleryStore,
    TrackGallery,
    quantize_orientation_bin,
)


def test_quantize_orientation_bin() -> None:
    assert quantize_orientation_bin(0.0, num_bins=8) == 0
    assert quantize_orientation_bin(math.pi / 2.0, num_bins=8) == 2
    assert quantize_orientation_bin(math.pi, num_bins=8) == 4
    assert quantize_orientation_bin(-math.pi / 4.0, num_bins=8) == 7
    assert quantize_orientation_bin(None, num_bins=8) is None
    assert quantize_orientation_bin(float("nan"), num_bins=8) is None


def test_track_gallery_slot_replacement_and_diversity_eviction() -> None:
    cfg = GalleryConfig(
        max_entries_per_track=4,
        max_entries_per_slot=2,
        num_orientation_bins=8,
        min_quality_score=0.2,
    )
    gallery = TrackGallery("W-0001", cfg)

    # cam_A, bin 0 에 품질 0.5, 0.8 두 개 추가
    assert gallery.add_observation([1.0, 0.0], timestamp_ms=1000, camera_id="cam_A", orientation_bin=0, quality_score=0.5)
    assert gallery.add_observation([0.9, 0.1], timestamp_ms=1100, camera_id="cam_A", orientation_bin=0, quality_score=0.8)
    assert len(gallery) == 2

    # 같은 슬롯(cam_A, bin 0)에 품질 0.9 가 들어오면 품질 0.5 항목을 교체한다 (개수는 2 유지)
    assert gallery.add_observation([0.8, 0.2], timestamp_ms=1200, camera_id="cam_A", orientation_bin=0, quality_score=0.9)
    assert len(gallery) == 2
    qualities_cam_a = sorted(e.quality_score for e in gallery.entries if e.camera_id == "cam_A")
    assert qualities_cam_a == [0.8, 0.9]

    # cam_B(bin 2), cam_C(bin 4) 추가로 총 4개(최대 용량 도달)
    assert gallery.add_observation([0.0, 1.0], timestamp_ms=1300, camera_id="cam_B", orientation_bin=2, quality_score=0.7)
    assert gallery.add_observation([0.2, 0.8], timestamp_ms=1400, camera_id="cam_C", orientation_bin=4, quality_score=0.75)
    assert len(gallery) == 4

    # 새 카메라 cam_D(bin 6)가 들어오면 가장 과밀한 슬롯(cam_A, bin 0: 2개)의 낮은 품질(0.8) 항목을 내보내고
    # 카메라 4대(cam_A, cam_B, cam_C, cam_D) 다양성을 유지한다.
    assert gallery.add_observation([0.5, 0.5], timestamp_ms=1500, camera_id="cam_D", orientation_bin=6, quality_score=0.85)
    assert len(gallery) == 4
    cameras = {e.camera_id for e in gallery.entries}
    assert cameras == {"cam_A", "cam_B", "cam_C", "cam_D"}


def test_gallery_similarity_modes_and_face_upward_only_boost() -> None:
    cfg = GalleryConfig(similarity_mode="max", face_aux_weight=0.25, min_co_visibility=0.35)
    gallery = TrackGallery("W-0002", cfg)

    gallery.add_observation(
        [1.0, 0.0, 0.0],
        timestamp_ms=1000,
        camera_id="cam_A",
        orientation_bin=0,
        visibility=PartVisibility.all_visible(),
        quality_score=0.9,
        face_embedding=[1.0, 0.0],
    )
    gallery.add_observation(
        [0.0, 1.0, 0.0],
        timestamp_ms=1100,
        camera_id="cam_B",
        orientation_bin=2,
        visibility=PartVisibility.all_visible(),
        quality_score=0.9,
    )

    q_emb = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    res_max = gallery.compute_similarity(q_emb, query_camera_id="cam_C", mode="max")
    assert res_max.max_similarity == pytest.approx(1.0)
    assert res_max.mean_similarity == pytest.approx(0.5)
    assert res_max.score == pytest.approx(1.0)
    assert res_max.same_camera_max is None
    assert res_max.cross_camera_max == pytest.approx(1.0)

    res_mean = gallery.compute_similarity(q_emb, query_camera_id="cam_A", mode="mean")
    assert res_mean.score == pytest.approx(0.5)
    assert res_mean.same_camera_max == pytest.approx(1.0)
    assert res_mean.cross_camera_max == pytest.approx(0.0)

    # 얼굴 유사도(1.0)가 외형 평균 유사도(0.5)보다 높으면 보조 가중치(0.25)만큼 상향한다.
    res_face_boost = gallery.compute_similarity(
        q_emb,
        query_face_embedding=[1.0, 0.0],
        mode="mean",
    )
    assert res_face_boost.face_similarity == pytest.approx(1.0)
    assert res_face_boost.score == pytest.approx(0.5 + 0.25 * (1.0 - 0.5))

    # 얼굴 유사도가 외형 점수보다 낮아도 외형 점수를 깎지 않는다.
    res_face_low = gallery.compute_similarity(
        q_emb,
        query_face_embedding=[0.0, 1.0],
        mode="max",
    )
    assert res_face_low.face_similarity == pytest.approx(0.0)
    assert res_face_low.score == pytest.approx(1.0)


def test_gallery_store_dormant_ttl_and_merge() -> None:
    cfg = GalleryConfig(dormant_ttl_ms=10_000)
    store = ReidGalleryStore(cfg)

    store.observe("W-0001", [1.0, 0.0], timestamp_ms=1_000, camera_id="cam_A")
    store.observe("W-0002", [0.0, 1.0], timestamp_ms=1_000, camera_id="cam_B")

    store.mark_dormant("W-0001", now_ms=2_000)
    # 11,500ms (경과 9,500ms <= 10,000ms): 아직 유지
    assert store.prune_expired(now_ms=11_500) == []
    assert "W-0001" in store.track_ids

    # 12,001ms (경과 10,001ms > 10,000ms): 만료 제거
    expired = store.prune_expired(now_ms=12_001)
    assert expired == ["W-0001"]
    assert store.track_ids == ("W-0002",)

    # 병합 시험: W-0003 을 W-0002 로 병합
    store.observe("W-0003", [1.0, 0.0], timestamp_ms=13_000, camera_id="cam_C")
    store.merge_tracks("W-0002", "W-0003", now_ms=13_100)
    assert store.track_ids == ("W-0002",)
    g2 = store.get("W-0002")
    assert g2 is not None and len(g2) == 2

    ranked = store.rank_candidates([1.0, 0.0], now_ms=13_200)
    assert len(ranked) == 1
    assert ranked[0].track_id == "W-0002"
    assert ranked[0].max_similarity == pytest.approx(1.0)

    store.clear()
    assert store.track_ids == ()
