"""비동기 Re-ID 특징 수집기(AsyncReidCollector) 및 캐시(LocalTrackFeatureCache) 단위 시험 (S15P31A101-463)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

import numpy as np
import pytest

from simai_ai.reid.collector import (
    AsyncReidCollector,
    LocalTrackFeature,
    LocalTrackFeatureCache,
    ReidCollectorConfig,
    estimate_orientation_bin_2d,
)
from simai_ai.reid.visibility import PartVisibility
from simai_ai.tracking.local_tracker import (
    Observation2DFrame,
    PoseDetection,
    TrackedDetection,
)


def _unit_vec(seed: int, dim: int = 1280) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / float(np.linalg.norm(v))


@dataclass
class _FakeFrame:
    camera_id: str
    estimated_capture_time_ms: int
    image: np.ndarray
    frame_ref: dict[str, int]


class _FakeEmbedder:
    def __init__(self, dim: int = 1280) -> None:
        self.dim = dim
        self.calls: list[int] = []

    def embed_with_scores(self, crops: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        n = len(crops)
        self.calls.append(n)
        embs = np.stack([_unit_vec(100 + i, self.dim) for i in range(n)], axis=0)
        scores = np.full((n,), 0.85, dtype=np.float32)
        return embs, scores


def _full_body_kps(x1: float, y1: float, x2: float, y2: float) -> tuple[tuple[float, float, float], ...]:
    cx = 0.5 * (x1 + x2)
    h = y2 - y1
    pts: list[tuple[float, float, float]] = []
    for idx in range(17):
        frac = 0.1 + 0.8 * (idx / 16.0)
        pts.append((cx, y1 + frac * h, 0.9))
    return tuple(pts)


def test_feature_cache_live_and_timeline_lookup_and_npz_roundtrip(tmp_path: Path) -> None:
    cache = LocalTrackFeatureCache(feature_ttl_ms=5_000)
    f1 = LocalTrackFeature(
        camera_id="cam_A",
        local_track_id="cam_A-t1",
        frame_sequence=10,
        capture_time_ms=1_000,
        embedding=_unit_vec(1),
        visibility=PartVisibility.all_visible(),
        quality_score=0.8,
        orientation_bin=2,
        bbox_xyxy=(100.0, 100.0, 200.0, 360.0),
    )
    f2 = LocalTrackFeature(
        camera_id="cam_A",
        local_track_id="cam_A-t1",
        frame_sequence=20,
        capture_time_ms=2_000,
        embedding=_unit_vec(2),
        visibility=PartVisibility.all_visible(),
        quality_score=0.9,
        orientation_bin=3,
        bbox_xyxy=(110.0, 100.0, 210.0, 360.0),
    )
    cache.put(f1, keep_timeline=True)
    cache.put(f2, keep_timeline=True)

    # Timeline lookup at t=1500 returns f1; at t=2500 returns f2; at t=8000 (past TTL) returns None
    got_1500 = cache.lookup("cam_A", "cam_A-t1", now_ms=1_500)
    assert got_1500 is not None and got_1500.frame_sequence == 10
    got_2500 = cache.lookup("cam_A", "cam_A-t1", now_ms=2_500)
    assert got_2500 is not None and got_2500.frame_sequence == 20
    assert cache.lookup("cam_A", "cam_A-t1", now_ms=8_000) is None

    npz_path = tmp_path / "reid_cache.npz"
    cache.save_npz(npz_path)
    loaded = LocalTrackFeatureCache.load_npz(npz_path, feature_ttl_ms=5_000)
    got_loaded = loaded.lookup("cam_A", "cam_A-t1", now_ms=1_500)
    assert got_loaded is not None
    assert got_loaded.frame_sequence == 10
    assert got_loaded.orientation_bin == 2
    assert np.allclose(got_loaded.embedding, f1.embedding, atol=1e-3)


def test_collector_disabled_by_config_and_missing_weights_fallback(tmp_path: Path) -> None:
    disabled_col = AsyncReidCollector(ReidCollectorConfig(enabled=False))
    assert not disabled_col.active
    st = disabled_col.status()
    assert st["state"] == "disabled"
    assert st["reason"] == "config_disabled"
    assert disabled_col.lookup("cam_A", "cam_A-t1", 1_000) is None
    disabled_col.close()

    missing_col = AsyncReidCollector(
        ReidCollectorConfig(
            enabled=True,
            cope_root=tmp_path / "nonexistent_cope",
            weights_path=tmp_path / "nonexistent.pth",
        )
    )
    assert not missing_col.active
    st_missing = missing_col.status()
    assert st_missing["state"] == "disabled"
    assert st_missing["reason"] is not None
    missing_col.close()


def test_collector_async_batch_queue_and_cooldown() -> None:
    fake_emb = _FakeEmbedder()
    col = AsyncReidCollector(
        ReidCollectorConfig(
            enabled=True,
            batch_size=4,
            update_interval_ms=750,
            min_det_score=0.35,
            min_bbox_width_px=24,
            min_bbox_height_px=64,
            min_visibility_ratio=0.25,
        ),
        embedder=fake_emb,
    )
    try:
        assert col.active
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        frame = _FakeFrame("cam_A", 1_000, img, {"stream_epoch": 1, "frame_sequence": 1})
        good_bbox = (100.0, 80.0, 200.0, 320.0)
        tiny_bbox = (10.0, 10.0, 20.0, 40.0)
        obs = Observation2DFrame(
            camera_id="cam_A",
            stream_epoch=1,
            frame_sequence=1,
            estimated_capture_time_ms=1_000,
            timestamp_source="RTCP_SR",
            timestamp_uncertainty_ms=5.0,
            detections=(
                TrackedDetection(
                    local_track_id="cam_A-t1",
                    detection=PoseDetection(good_bbox, 0.9, _full_body_kps(*good_bbox)),
                ),
                TrackedDetection(
                    local_track_id="cam_A-t2",
                    detection=PoseDetection(tiny_bbox, 0.9, _full_body_kps(*tiny_bbox)),
                ),
            ),
        )
        enq = col.submit_observation(frame, obs)
        assert enq == 1
        assert col.dropped_low_quality == 1

        # Immediate re-submission within cooldown (t=1200 < 1000 + 750) is skipped
        obs_cooldown = Observation2DFrame(
            camera_id="cam_A",
            stream_epoch=1,
            frame_sequence=2,
            estimated_capture_time_ms=1_200,
            timestamp_source="RTCP_SR",
            timestamp_uncertainty_ms=5.0,
            detections=(
                TrackedDetection(
                    local_track_id="cam_A-t1",
                    detection=PoseDetection(good_bbox, 0.9, _full_body_kps(*good_bbox)),
                ),
            ),
        )
        assert col.submit_observation(frame, obs_cooldown) == 0
        assert col.dropped_cooldown == 1

        deadline = time.monotonic() + 2.0
        feat = None
        while time.monotonic() < deadline:
            feat = col.lookup("cam_A", "cam_A-t1", 1_200)
            if feat is not None:
                break
            time.sleep(0.02)
        assert feat is not None
        assert feat.camera_id == "cam_A"
        assert feat.local_track_id == "cam_A-t1"
        assert feat.embedding.shape == (1280,)
        assert pytest.approx(float(np.linalg.norm(feat.embedding)), abs=1e-5) == 1.0
    finally:
        col.close()


def test_estimate_orientation_bin_2d() -> None:
    b0 = estimate_orientation_bin_2d(None, (10.0, 50.0, 110.0, 250.0), image_width_px=800, num_bins=8)
    b7 = estimate_orientation_bin_2d(None, (700.0, 50.0, 780.0, 250.0), image_width_px=800, num_bins=8)
    assert b0 == 0
    assert b7 == 7
