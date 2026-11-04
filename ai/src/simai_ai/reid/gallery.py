"""트랙별 다시점·방향 외형 갤러리와 DORMANT 보관 관리 (S15P31A101-463).

규칙:
- 특징 벡터는 작업 세션 동안 메모리에만 보관하며 외부 저장소나 파일로 내보내지 않는다.
- 시계(`timestamp_ms`, `now_ms`)는 모두 호출자가 주입한다(직접 시각 조회 금지).
- 트랙마다 `(camera_id, orientation_bin)` 슬롯별로 특징을 분산 보관해 한 카메라·한 자세가
  갤러리 전체를 덮어쓰지 않게 한다.
- 얼굴 보조 임베딩이 있을 때는 점수를 상향 보조만 하고, 얼굴이 없거나 작을 때 점수를 깎지 않는다.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .embedder import l2_normalize_rows
from .visibility import PartVisibility, PartWeights, co_visibility_confidence


def quantize_orientation_bin(angle_rad: float | None, num_bins: int = 8) -> int | None:
    """라디안 각도(-pi~pi 또는 0~2pi)를 0..num_bins-1 정수 방향 bin 으로 양자화한다."""
    if num_bins < 1:
        raise ValueError(f"num_bins 는 1 이상이어야 합니다: {num_bins}")
    if angle_rad is None or not math.isfinite(angle_rad):
        return None
    two_pi = 2.0 * math.pi
    wrapped = float(angle_rad) % two_pi
    bin_idx = int((wrapped / two_pi) * num_bins)
    return min(num_bins - 1, max(0, bin_idx))


@dataclass(frozen=True)
class GalleryConfig:
    """트랙 외형 갤러리 설정."""

    max_entries_per_track: int = 16
    max_entries_per_slot: int = 2
    num_orientation_bins: int = 8
    min_quality_score: float = 0.20
    replace_quality_margin: float = 0.05
    min_co_visibility: float = 0.35
    similarity_mode: str = "max"  # "max" | "mean"
    dormant_ttl_ms: int = 30_000
    face_aux_weight: float = 0.20
    max_face_entries_per_track: int = 4
    part_weights: PartWeights = field(default_factory=PartWeights)

    def __post_init__(self) -> None:
        if self.max_entries_per_track < 1:
            raise ValueError("max_entries_per_track 는 1 이상이어야 합니다.")
        if self.max_entries_per_slot < 1:
            raise ValueError("max_entries_per_slot 는 1 이상이어야 합니다.")
        if self.similarity_mode not in {"max", "mean"}:
            raise ValueError(f"지원하지 않는 similarity_mode 입니다: {self.similarity_mode}")
        if self.dormant_ttl_ms < 0:
            raise ValueError("dormant_ttl_ms 는 0 이상이어야 합니다.")
        if not (0.0 <= self.face_aux_weight <= 1.0):
            raise ValueError("face_aux_weight 는 0~1 사이여야 합니다.")


@dataclass(frozen=True)
class GalleryEntry:
    """트랙 갤러리의 단일 관측 특징 항목."""

    embedding: Any
    timestamp_ms: int
    camera_id: str
    orientation_bin: int | None = None
    visibility: PartVisibility = field(default_factory=PartVisibility.all_visible)
    quality_score: float = 1.0
    face_embedding: Any | None = None

    @property
    def slot_key(self) -> tuple[str, int | None]:
        return (self.camera_id, self.orientation_bin)


@dataclass(frozen=True)
class GallerySimilarityResult:
    """질의 관측과 트랙 갤러리 사이의 유사도 계산 결과."""

    track_id: str
    score: float
    max_similarity: float
    mean_similarity: float
    weighted_max_similarity: float
    same_camera_max: float | None
    cross_camera_max: float | None
    face_similarity: float | None
    best_co_visibility: float
    matched_entries: int


class TrackGallery:
    """단일 World/로컬 트랙의 다시점 외형·얼굴 특징 갤러리."""

    def __init__(self, track_id: str, config: GalleryConfig | None = None) -> None:
        self.track_id = track_id
        self.config = config or GalleryConfig()
        self._entries: list[GalleryEntry] = []
        self._face_entries: list[tuple[int, float, Any]] = []  # (timestamp_ms, quality, embedding)
        self.state: str = "ACTIVE"
        self.last_updated_ms: int | None = None
        self.dormant_since_ms: int | None = None

    @property
    def entries(self) -> tuple[GalleryEntry, ...]:
        return tuple(self._entries)

    @property
    def face_count(self) -> int:
        return len(self._face_entries)

    def __len__(self) -> int:
        return len(self._entries)

    def mark_dormant(self, now_ms: int) -> None:
        if self.state != "DORMANT":
            self.state = "DORMANT"
            self.dormant_since_ms = int(now_ms)

    def mark_active(self, now_ms: int) -> None:
        self.state = "ACTIVE"
        self.dormant_since_ms = None
        if self.last_updated_ms is None or now_ms > self.last_updated_ms:
            self.last_updated_ms = int(now_ms)

    def is_expired(self, now_ms: int) -> bool:
        if self.state != "DORMANT" or self.dormant_since_ms is None:
            return False
        return (int(now_ms) - self.dormant_since_ms) > self.config.dormant_ttl_ms

    def add_observation(
        self,
        embedding: Any,
        *,
        timestamp_ms: int,
        camera_id: str,
        orientation_bin: int | None = None,
        orientation_rad: float | None = None,
        visibility: PartVisibility | None = None,
        quality_score: float = 1.0,
        face_embedding: Any | None = None,
    ) -> bool:
        """트랙 갤러리에 새 관측 특징을 추가하거나 슬롯/용량 규칙에 따라 교체한다."""
        import numpy as np

        vis = visibility if visibility is not None else PartVisibility.all_visible()
        if not vis.any_visible or quality_score < self.config.min_quality_score:
            return False

        normed = l2_normalize_rows(embedding).reshape(-1)
        if float(np.linalg.norm(normed)) <= 1e-12:
            return False

        if orientation_bin is None and orientation_rad is not None:
            orientation_bin = quantize_orientation_bin(orientation_rad, self.config.num_orientation_bins)
        elif orientation_bin is not None:
            orientation_bin = int(orientation_bin) % self.config.num_orientation_bins

        normed_face: Any | None = None
        if face_embedding is not None:
            f_arr = l2_normalize_rows(face_embedding).reshape(-1)
            if float(np.linalg.norm(f_arr)) > 1e-12:
                normed_face = f_arr
                self._add_face_entry(int(timestamp_ms), float(quality_score), normed_face)

        entry = GalleryEntry(
            embedding=normed,
            timestamp_ms=int(timestamp_ms),
            camera_id=str(camera_id),
            orientation_bin=orientation_bin,
            visibility=vis,
            quality_score=float(quality_score),
            face_embedding=normed_face,
        )

        slot = entry.slot_key
        slot_indices = [i for i, e in enumerate(self._entries) if e.slot_key == slot]

        added = False
        if len(slot_indices) >= self.config.max_entries_per_slot:
            weakest_idx = min(
                slot_indices,
                key=lambda i: (round(self._entries[i].quality_score, 4), self._entries[i].timestamp_ms),
            )
            weakest = self._entries[weakest_idx]
            if (
                entry.quality_score >= weakest.quality_score
                or (
                    entry.timestamp_ms >= weakest.timestamp_ms
                    and (weakest.quality_score - entry.quality_score) <= self.config.replace_quality_margin
                )
            ):
                self._entries[weakest_idx] = entry
                added = True
        else:
            if len(self._entries) >= self.config.max_entries_per_track:
                evict_idx = self._select_eviction_index()
                self._entries[evict_idx] = entry
            else:
                self._entries.append(entry)
            added = True

        if added:
            self.mark_active(int(timestamp_ms))
        return added

    def _select_eviction_index(self) -> int:
        """전체 용량이 가득 찼을 때 가장 과밀한 슬롯(또는 카메라)의 최저 품질·최구형 항목 인덱스를 고른다."""
        slot_counts = Counter(e.slot_key for e in self._entries)
        max_slot_count = max(slot_counts.values())
        if max_slot_count > 1:
            candidates = [i for i, e in enumerate(self._entries) if slot_counts[e.slot_key] == max_slot_count]
        else:
            cam_counts = Counter(e.camera_id for e in self._entries)
            max_cam_count = max(cam_counts.values())
            candidates = [i for i, e in enumerate(self._entries) if cam_counts[e.camera_id] == max_cam_count]

        return min(
            candidates,
            key=lambda i: (round(self._entries[i].quality_score, 4), self._entries[i].timestamp_ms),
        )

    def _add_face_entry(self, timestamp_ms: int, quality_score: float, face_vec: Any) -> None:
        if len(self._face_entries) < self.config.max_face_entries_per_track:
            self._face_entries.append((timestamp_ms, quality_score, face_vec))
            return
        weakest_idx = min(
            range(len(self._face_entries)),
            key=lambda i: (round(self._face_entries[i][1], 4), self._face_entries[i][0]),
        )
        if quality_score >= self._face_entries[weakest_idx][1] or timestamp_ms >= self._face_entries[weakest_idx][0]:
            self._face_entries[weakest_idx] = (timestamp_ms, quality_score, face_vec)

    def compute_similarity(
        self,
        query_embedding: Any,
        *,
        query_visibility: PartVisibility | None = None,
        query_camera_id: str | None = None,
        query_face_embedding: Any | None = None,
        mode: str | None = None,
    ) -> GallerySimilarityResult:
        """질의 특징과 이 트랙 갤러리 항목들 사이의 최대·평균 유사도 및 보조 얼굴 점수를 계산한다."""
        import numpy as np

        sim_mode = mode or self.config.similarity_mode
        if sim_mode not in {"max", "mean"}:
            raise ValueError(f"지원하지 않는 similarity_mode 입니다: {sim_mode}")

        if not self._entries:
            return GallerySimilarityResult(
                track_id=self.track_id,
                score=0.0,
                max_similarity=0.0,
                mean_similarity=0.0,
                weighted_max_similarity=0.0,
                same_camera_max=None,
                cross_camera_max=None,
                face_similarity=None,
                best_co_visibility=0.0,
                matched_entries=0,
            )

        q_vec = l2_normalize_rows(query_embedding).reshape(-1)
        if float(np.linalg.norm(q_vec)) <= 1e-12:
            return GallerySimilarityResult(
                track_id=self.track_id,
                score=0.0,
                max_similarity=0.0,
                mean_similarity=0.0,
                weighted_max_similarity=0.0,
                same_camera_max=None,
                cross_camera_max=None,
                face_similarity=None,
                best_co_visibility=0.0,
                matched_entries=0,
            )

        sims: list[float] = []
        weighted_sims: list[float] = []
        co_vis_list: list[float] = []
        same_cam_sims: list[float] = []
        cross_cam_sims: list[float] = []

        for entry in self._entries:
            if query_visibility is not None:
                co_vis = co_visibility_confidence(
                    entry.visibility,
                    query_visibility,
                    weights=self.config.part_weights,
                )
            else:
                co_vis = 1.0

            if co_vis < self.config.min_co_visibility:
                continue

            cos_sim = float(np.dot(entry.embedding, q_vec))
            cos_sim = max(-1.0, min(1.0, cos_sim))
            sims.append(cos_sim)
            weighted_sims.append(cos_sim * co_vis)
            co_vis_list.append(co_vis)
            if query_camera_id is not None:
                if entry.camera_id == query_camera_id:
                    same_cam_sims.append(cos_sim)
                else:
                    cross_cam_sims.append(cos_sim)

        if not sims:
            return GallerySimilarityResult(
                track_id=self.track_id,
                score=0.0,
                max_similarity=0.0,
                mean_similarity=0.0,
                weighted_max_similarity=0.0,
                same_camera_max=None,
                cross_camera_max=None,
                face_similarity=None,
                best_co_visibility=0.0,
                matched_entries=0,
            )

        max_sim = max(sims)
        mean_sim = sum(sims) / len(sims)
        weighted_max = max(weighted_sims)
        best_co_vis = max(co_vis_list)
        base_score = max_sim if sim_mode == "max" else mean_sim

        face_sim: float | None = None
        if query_face_embedding is not None and self._face_entries:
            qf = l2_normalize_rows(query_face_embedding).reshape(-1)
            if float(np.linalg.norm(qf)) > 1e-12:
                face_sim = max(float(np.dot(f_vec, qf)) for _, _, f_vec in self._face_entries)
                face_sim = max(-1.0, min(1.0, face_sim))

        final_score = base_score
        if face_sim is not None and face_sim > base_score:
            final_score = base_score + self.config.face_aux_weight * (face_sim - base_score)

        return GallerySimilarityResult(
            track_id=self.track_id,
            score=float(final_score),
            max_similarity=float(max_sim),
            mean_similarity=float(mean_sim),
            weighted_max_similarity=float(weighted_max),
            same_camera_max=max(same_cam_sims) if same_cam_sims else None,
            cross_camera_max=max(cross_cam_sims) if cross_cam_sims else None,
            face_similarity=face_sim,
            best_co_visibility=float(best_co_vis),
            matched_entries=len(sims),
        )

    def similarity(
        self,
        query_embedding: Any,
        *,
        camera_id: str | None = None,
        query_visibility: PartVisibility | None = None,
        face_embedding: Any | None = None,
        mode: str | None = None,
    ) -> float | None:
        """질의 임베딩과의 대표 유사도(점수). 매칭되는 항목이 없으면 None."""
        res = self.compute_similarity(
            query_embedding,
            query_visibility=query_visibility,
            query_camera_id=camera_id,
            query_face_embedding=face_embedding,
            mode=mode,
        )
        if res.matched_entries == 0:
            return None
        return float(res.score)

    def update(
        self,
        *,
        embedding: Any,
        camera_id: str,
        t_ms: int,
        quality: float = 1.0,
        visibility: float | PartVisibility = 1.0,
        orientation_bin: int | None = None,
        face_embedding: Any | None = None,
    ) -> bool:
        """`add_observation` 의 간편 별칭 (스칼라 가시성 또는 PartVisibility 모두 허용)."""
        vis_obj = (
            visibility
            if isinstance(visibility, PartVisibility)
            else PartVisibility.all_visible()
        )
        return self.add_observation(
            embedding,
            timestamp_ms=int(t_ms),
            camera_id=str(camera_id),
            orientation_bin=orientation_bin,
            visibility=vis_obj,
            quality_score=float(quality),
            face_embedding=face_embedding,
        )


class ReidGalleryStore:
    """작업 세션 동안 여러 트랙의 외형 갤러리와 DORMANT 수명을 메모리에서 관리한다."""

    def __init__(self, config: GalleryConfig | None = None) -> None:
        self.config = config or GalleryConfig()
        self._galleries: dict[str, TrackGallery] = {}

    @property
    def track_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._galleries))

    def get(self, track_id: str) -> TrackGallery | None:
        return self._galleries.get(track_id)

    def get_or_create(self, track_id: str, now_ms: int | None = None) -> TrackGallery:
        gallery = self._galleries.get(track_id)
        if gallery is None:
            gallery = TrackGallery(track_id, self.config)
            if now_ms is not None:
                gallery.mark_active(now_ms)
            self._galleries[track_id] = gallery
        return gallery

    def remove(self, track_id: str) -> None:
        self._galleries.pop(track_id, None)

    def merge(self, keep_track_id: str, drop_track_id: str, now_ms: int) -> None:
        self.merge_tracks(keep_track_id, drop_track_id, now_ms)

    def observe(
        self,
        track_id: str,
        embedding: Any,
        *,
        timestamp_ms: int,
        camera_id: str,
        orientation_bin: int | None = None,
        orientation_rad: float | None = None,
        visibility: PartVisibility | None = None,
        quality_score: float = 1.0,
        face_embedding: Any | None = None,
    ) -> bool:
        gallery = self._galleries.get(track_id)
        if gallery is None:
            gallery = TrackGallery(track_id, self.config)
            self._galleries[track_id] = gallery
        return gallery.add_observation(
            embedding,
            timestamp_ms=timestamp_ms,
            camera_id=camera_id,
            orientation_bin=orientation_bin,
            orientation_rad=orientation_rad,
            visibility=visibility,
            quality_score=quality_score,
            face_embedding=face_embedding,
        )

    def mark_dormant(self, track_id: str, now_ms: int) -> None:
        gallery = self._galleries.get(track_id)
        if gallery is not None:
            gallery.mark_dormant(now_ms)

    def mark_active(self, track_id: str, now_ms: int) -> None:
        gallery = self._galleries.get(track_id)
        if gallery is not None:
            gallery.mark_active(now_ms)

    def prune_expired(self, now_ms: int) -> list[str]:
        """DORMANT 보관 시간을 넘긴 트랙 갤러리를 메모리에서 지우고 제거된 ID 목록을 돌려준다."""
        expired = [tid for tid, g in self._galleries.items() if g.is_expired(now_ms)]
        for tid in expired:
            del self._galleries[tid]
        return sorted(expired)

    def merge_tracks(self, keep_track_id: str, drop_track_id: str, now_ms: int) -> None:
        """트랙 병합 시 drop_track_id 의 갤러리 항목들을 keep_track_id 로 합치고 제거한다."""
        if keep_track_id == drop_track_id:
            return
        drop_g = self._galleries.pop(drop_track_id, None)
        if drop_g is None:
            return
        keep_g = self._galleries.get(keep_track_id)
        if keep_g is None:
            keep_g = TrackGallery(keep_track_id, self.config)
            self._galleries[keep_track_id] = keep_g
        for entry in drop_g.entries:
            keep_g.add_observation(
                entry.embedding,
                timestamp_ms=entry.timestamp_ms,
                camera_id=entry.camera_id,
                orientation_bin=entry.orientation_bin,
                visibility=entry.visibility,
                quality_score=entry.quality_score,
                face_embedding=entry.face_embedding,
            )
        keep_g.mark_active(now_ms)

    def rank_candidates(
        self,
        query_embedding: Any,
        *,
        candidate_track_ids: Iterable[str] | None = None,
        query_visibility: PartVisibility | None = None,
        query_camera_id: str | None = None,
        query_face_embedding: Any | None = None,
        include_dormant: bool = True,
        now_ms: int | None = None,
        mode: str | None = None,
    ) -> list[GallerySimilarityResult]:
        """기하 게이트를 통과한 후보 트랙들에 대해 외형 유사도를 계산해 점수 내림차순으로 반환한다."""
        if now_ms is not None:
            self.prune_expired(now_ms)

        ids = list(candidate_track_ids) if candidate_track_ids is not None else list(self._galleries)
        results: list[GallerySimilarityResult] = []
        for tid in ids:
            gallery = self._galleries.get(tid)
            if gallery is None:
                continue
            if not include_dormant and gallery.state == "DORMANT":
                continue
            if now_ms is not None and gallery.is_expired(now_ms):
                continue
            res = gallery.compute_similarity(
                query_embedding,
                query_visibility=query_visibility,
                query_camera_id=query_camera_id,
                query_face_embedding=query_face_embedding,
                mode=mode,
            )
            results.append(res)

        results.sort(key=lambda r: (r.score, r.max_similarity, r.mean_similarity), reverse=True)
        return results

    def clear(self) -> None:
        """세션 종료 시 메모리의 모든 특징을 비운다."""
        self._galleries.clear()
