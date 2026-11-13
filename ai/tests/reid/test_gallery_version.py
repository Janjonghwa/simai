"""TrackGallery.version: 항목을 바꿀 수 있는 호출마다 늘어 World Track 의 tick 안 유사도 재사용을 무효화한다(S15P31A101-663)."""

import numpy as np

from simai_ai.reid.gallery import GalleryConfig, ReidGalleryStore, TrackGallery


def _vec(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 1.0, 64).astype(np.float32)


def test_version_grows_on_every_add_and_similarity_follows_entries():
    g = TrackGallery("w-1", GalleryConfig(max_entries_per_slot=1))
    assert g.version == 0
    q = _vec(9)
    assert g.similarity(q) is None
    g.update(embedding=_vec(1), camera_id="cam1", t_ms=1, orientation_bin=0)
    first = (g.version, g.similarity(q))
    assert g.update(embedding=_vec(2), camera_id="cam1", t_ms=2, orientation_bin=0)  # 같은 슬롯 교체
    assert g.version > first[0]
    assert g.similarity(q) != first[1]
    before = g.version
    assert not g.update(embedding=_vec(3), camera_id="cam2", t_ms=3, quality=0.01)  # 품질 미달로 거절돼도
    assert g.version > before  # 올린다(다시 계산할 뿐 결과는 같다)


def test_merge_bumps_keep_gallery_and_drops_the_other():
    store = ReidGalleryStore()
    store.observe("w-1", _vec(1), timestamp_ms=1, camera_id="cam1")
    store.observe("w-2", _vec(2), timestamp_ms=1, camera_id="cam2")
    keep, drop = store.get("w-1"), store.get("w-2")
    version = keep.version
    store.merge_tracks("w-1", "w-2", 5)
    assert store.get("w-1") is keep and keep.version > version
    assert store.get("w-2") is None and drop is not None
